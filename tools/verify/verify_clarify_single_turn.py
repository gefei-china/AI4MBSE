# -*- coding: utf-8 -*-
# CI-OPTIONAL: A 需服务在跑（BASE=http://127.0.0.1:8000 真实 SSE 链路，2026-10-18 实测打到运行中服务返 HTTP 400）+ 澄清触发依赖真实 LLM 行为
"""D1 回归验证：澄清续答 DB 层单轮化（2026-10-17，米爸 Q1 收口）。

背景
----
米爸 Q1：「AI 会话流中澄清后，会触发新的对话轮次，正常的逻辑应该是在当前 AI 输出轮次
继续输出，而不是新增轮次」。Q2 已修 UI 层（原盒续答，不新起气泡），但 DB 层刷新后仍是
两轮：/chat/stream 入口无条件 INSERT 一条 user 消息（resume_text 当成新用户输入）。

本批改动（后端 + 前端）
----------------------
· /clarify-answer：新增 skip 分支（此前"跳过澄清"不走该接口 ⇒ pending_clarify 永不清除，
  刷新后确认卡复活、下一条消息被当成澄清补充）；返回 append_text + resume_of_user_msg_id
  （追认：本会话最后一条 clarify 卡 + 其之前最近的一条 user 消息）。
· /chat/stream（ChatIn）：新增 resume_of_user_msg_id / resume_append 两个转呈字段。
· execute_stream：resume_of_user_msg_id 非 0 ⇒ 跳过 user INSERT，把 resume_append
  **追加进原 user 消息**（COALESCE(content,'')||?，同会话同角色才写）；追认失败
  （消息被删/跨会话）兜底回落新 INSERT，内容不丢。
· 前端：clarifyAnswerSend / 自由文本分支 / clarifySkipSend 统一走 _continueInSameBox
  并转呈追认信息；pumpStream 请求体带两个新字段。

触发路径说明（2026-10-17 二次校正）
----------------------------------
· 初版用「建模」+team 撞内容级澄清（1.5）不可行：team 覆写 forced_intent 使 1.5 首道门
  即返回 None；「建模」本身是动作词命中 action_kw；内置 agent 关键词 active=0 +
  embedding 502 降级 bigram ⇒ 短输入全部 route=chat/conf=0。
· 二版改撞阶段 1.4 意图确认（短输入+求助暗示词）。★ 但 2026-10-17「确认卡 LLM 动态化」
  上线后此路又不可测：1.4 前新增的 `_llm_intent_confirm_questions` 会让真实 LLM 结合
  上下文判断——若判 need=false（连卡都不弹）或生成选项不足，脚本即崩；且本脚本历史上
  两次被真实 LLM 429 限流搞崩。
· 三版（当前）：**进程内 execute_stream + mock LLM 走 ask_user 路径**——ReAct 探测轮
  返 ask_user tool_call ⇒ 弹上下文相关确认卡（与 4294 真实案例同形态）。优势：
  ① 触发完全可控，不受 LLM 抖动/限流/语义服务状态影响；② 顺带覆盖「AI 执行中主动
  确认 → /clarify-answer → 续跑单轮化」完整链路；③ DB 层单轮化判据与触发路径无关
  （续答入口的 UPDATE 逻辑不关心卡从哪来）。HTTP 仅用于建会话与 /clarify-answer。

判据
----
T0 触发澄清（ask_user 卡）：clarify_ask 事件、卡片点名上下文（工程+版本）、
   title 标明执行中确认 + source=ask_user、done.clarify_asked=True、pending 挂起、
   落 1 条 user 消息
T1 clarify-answer 返回追认信息（answers 路径，问题 id 取自真实挂起状态）：
   resume_of_user_msg_id = 触发澄清的 user 消息
T2 续跑后 DB 单轮：user 消息数不变、原 user 消息 content 含追加文本、新增 assistant 消息
T3 skip 分支：pending_clarify 清空 + 同样返回追认信息（不清空=旧 bug 复发）
T4 追认失败兜底：传不存在的 user_msg_id ⇒ 回落新 user 消息（内容不丢、不静默）
T5 普通发送不受影响：不带追认字段时照旧新起一轮
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "mbse.db"
BASE = "http://127.0.0.1:8000"
HDR = {"Content-Type": "application/json", "X-User-Id": "1", "X-Session-Token": "verify-d1"}
# 触发输入：导出任务（mock LLM 会在 ReAct 探测轮返 ask_user tool_call ⇒ 弹上下文相关
# 确认卡）。★ 2026-10-17 改造：触发路径由「HTTP SSE 撞 1.4 真实 LLM」改为进程内
# execute_stream + mock LLM 走 ask_user —— 真实 LLM 可能判 need=false 连卡都不弹
# （LLM 动态化上线后），且本脚本两次被 429 限流搞崩；ask_user 路径可控且覆盖
# 「AI 执行中主动确认 → 作答 → 续跑单轮化」完整链路（probe_ask_user 已证形态）。
TRIGGER_INPUT = "导出广汽元模型1010的sysml v2源码"
# 干净普通输入：mock 下不触发 ask_user，供 T4/T5 使用
PLAIN_INPUT = "今天天气不错"

_fails: list[str] = []
_passes = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  [PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  — {detail}")


def section(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def post(path: str, body: dict, timeout: int = 240) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers=HDR, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def db_msgs(cid: int) -> list:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, role, content, msg_type FROM messages WHERE conversation_id=? ORDER BY id", (cid,))]
    finally:
        con.close()


# ── mock LLM（ask_user 触发 / 普通生成两态，按 _ask_state 切换）─────────────────
_ask_state = {"asked": False}


def _fake_chat(messages, provider_id=None, stream=False, tools=None,
               _intent="", _model_params=None, **kw):
    """进程内 execute_stream 的 LLM 替身：
    · ReAct 探测轮（CHAT_TASK_PROBE + tools）：首次返 ask_user tool_call（上下文相关
      确认卡），其后返无 tool_calls 的 content（结束循环，正常生成）；
    · 其余调用（意图兜底/澄清探测/生成）：无害占位响应。
    """
    from agent.pipeline_parts.common import CHAT_TASK_PROBE
    if _intent == CHAT_TASK_PROBE and tools and not _ask_state["asked"]:
        _ask_state["asked"] = True
        return {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_ask_1", "type": "function", "function": {
                "name": "ask_user",
                "arguments": json.dumps({
                    "question": "导出「广汽元模型1010」的 SysML v2 源码，用哪个版本、什么范围？",
                    "options": ["版本 3（最新）+ 整工程导出", "版本 3（最新）+ 按包导出（先展开数据树选包）",
                                "版本 2 + 整工程导出", "版本 1 + 整工程导出"],
                    "context": "该工程有 3 个已发布版本：版本 3（2026-10-13 14:09，最新）、"
                               "版本 2（2026-10-13 09:33）、版本 1（2026-10-12 16:42）。",
                }, ensure_ascii=False)}}]}}]}
    return {"choices": [{"message": {"role": "assistant",
                                     "content": "（验证替身）已按你的确认继续执行。"}}],
            "_meta": {"provider": "verify", "model": "-", "used_mock": True}}


def run_stream(cid: int, body: dict) -> list:
    """进程内 execute_stream（mock LLM 已装），返回事件列表。"""
    import sys as _sys
    if str(ROOT) not in _sys.path:
        _sys.path.insert(0, str(ROOT))
    from agent.pipeline import AgentPipeline
    if not hasattr(run_stream, "_pipe"):
        run_stream._pipe = AgentPipeline()
    evs = list(run_stream._pipe.execute_stream(
        body["message"], conversation_id=cid, branch=body.get("branch") or "dev",
        attachments=body.get("attachments"),
        resume_of_user_msg_id=int(body.get("resume_of_user_msg_id") or 0),
        resume_append=body.get("resume_append") or ""))
    return evs


def main() -> int:
    # 进程内 mock LLM（T0/T2/T3-续跑/T4/T5 全部走 execute_stream，不依赖真实 LLM）
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))      # 进程内 import llm / agent 需要项目根在 path
    from llm import llm_client as _lc
    _orig_chat = _lc.chat
    _lc.chat = _fake_chat
    try:
        _run_all()
    finally:
        _lc.chat = _orig_chat     # 任何路径退出都还原 LLM
    section("结果")
    print(f"  通过 {_passes} 项，失败 {len(_fails)} 项")
    for f in _fails:
        print(f"    · {f}")
    if not _fails:
        print("  全部通过 ✓  澄清续答 DB 层单轮化生效")
    return 0 if not _fails else 1


def _run_all() -> None:
    section("T0 · 前置：造一个会触发澄清的会话（ask_user：AI 执行中主动确认，mock LLM）")
    cv = post("/api/conversations", {"title": "verify-d1-single-turn"})
    cid = cv.get("id")
    chk("新建会话", bool(cid), f"cid={cid}")

    _ask_state["asked"] = False
    evs = run_stream(cid, {"message": TRIGGER_INPUT})
    clarify = next((e for e in evs if e.get("type") == "clarify_ask"), None)
    chk("第一轮触发 clarify_ask（AI 执行中主动确认）", bool(clarify),
        f"questions={len((clarify or {}).get('questions') or [])}")
    _q0 = ((clarify or {}).get("questions") or [{}])[0]
    _opts = _q0.get("options") or []
    chk("T0 卡片为上下文相关内容（点名工程+版本，非系统固定清单）",
        "广汽元模型1010" in (_q0.get("question") or "") and len(_opts) >= 3,
        f"question={ (_q0.get('question') or '')[:40]!r } opts={len(_opts)}")
    chk("T0 title 标明执行中确认 + source=ask_user",
        "执行中" in ((clarify or {}).get("title") or "")
        and (clarify or {}).get("source") == "ask_user")
    done0 = next((e for e in evs if e.get("type") == "done"), None)
    chk("T0 done.clarify_asked=True（未写空 assistant 直接挂起）",
        bool(done0 and done0["data"].get("clarify_asked")))

    pend = get(f"/api/conversations/{cid}/messages?limit=1").get("pending_clarify")
    chk("pending_clarify 已挂起", bool(pend))

    msgs = db_msgs(cid)
    users0 = [m for m in msgs if m["role"] == "user"]
    chk("第一轮落 1 条 user 消息", len(users0) == 1, f"user msgs={[m['id'] for m in users0]}")
    u1_id = users0[0]["id"] if users0 else 0

    section("T1 · clarify-answer 返回追认信息（answers 路径，问题 id 取自真实挂起）")
    pend1 = get(f"/api/conversations/{cid}/messages?limit=1").get("pending_clarify") or {}
    qs1 = pend1.get("questions") or []
    chk("T1 前置：挂起状态里取到问题", bool(qs1), f"questions={len(qs1)}")
    _q0 = qs1[0] if qs1 else {}
    _qid = str(_q0.get("id") or "q1")
    _qval = (str(_q0.get("options") or ["按我的描述直接执行"])[0]
             if isinstance(_q0.get("options"), list) and _q0.get("options")
             else "按我的描述直接执行")
    r = post(f"/api/conversations/{cid}/clarify-answer",
             {"answers": [{"question_id": _qid, "value": _qval}]})
    chk("clarify-answer ok", bool(r.get("ok")), f"keys={list(r.keys())}")
    chk("返回 resume_text（给 LLM）", bool(r.get("resume_text")))
    chk("返回 append_text（给 DB，不含原请求复述）",
        bool(r.get("append_text")) and "原请求" not in (r.get("append_text") or ""),
        f"append_text={ (r.get('append_text') or '')[:60]!r }")
    chk("T1 resume_of_user_msg_id = 触发澄清的 user 消息",
        r.get("resume_of_user_msg_id") == u1_id,
        f"got={r.get('resume_of_user_msg_id')} expect={u1_id}")
    chk("pending_clarify 已清空",
        get(f"/api/conversations/{cid}/messages?limit=1").get("pending_clarify") is None)

    section("T2 · 续跑后 DB 单轮（核心判据）")
    _ask_state["asked"] = True      # 续跑不再触发 ask_user，走普通生成
    evs2 = run_stream(cid, {
        "message": r["resume_text"],
        "resume_of_user_msg_id": r["resume_of_user_msg_id"], "resume_append": r["append_text"],
    })
    done2 = next((e for e in evs2 if e.get("type") == "done"), None)
    chk("续跑流正常结束（done 到达）", bool(done2))

    msgs2 = db_msgs(cid)
    users2 = [m for m in msgs2 if m["role"] == "user"]
    asst2 = [m for m in msgs2 if m["role"] == "assistant"]
    chk("T2 user 消息数**不增**（单轮化生效）", len(users2) == 1,
        f"user ids={[m['id'] for m in users2]}（期望仅 [{u1_id}]）")
    u1_after = next((m for m in users2 if m["id"] == u1_id), None)
    chk("T2 原 user 消息 content 含追加的澄清补充",
        bool(u1_after) and "【澄清补充】" in (u1_after["content"] or ""),
        f"content 尾部={ (u1_after['content'] or '')[-60:]!r }" if u1_after else "原消息不见了")
    chk("T2 新增了 assistant 续答消息（AI 输出仍在同一轮内）",
        len(asst2) >= 2, f"assistant ids={[m['id'] for m in asst2]}")
    chk("T2 全流无 error 事件", not any(e.get("type") == "error" for e in evs2))

    section("T3 · skip 分支：清挂起 + 同样追认")
    cv2 = post("/api/conversations", {"title": "verify-d1-skip"})
    cid2 = cv2.get("id")
    _ask_state["asked"] = False
    run_stream(cid2, {"message": TRIGGER_INPUT})
    pend2 = get(f"/api/conversations/{cid2}/messages?limit=1").get("pending_clarify")
    chk("T3 前置：cid2 已挂起澄清", bool(pend2))
    rs = post(f"/api/conversations/{cid2}/clarify-answer", {"skip": True})
    chk("T3 skip 返回 resume_text（不要再提问）",
        bool(rs.get("resume_text")) and "不要再次提问" in (rs.get("resume_text") or ""),
        f"resume_text={ (rs.get('resume_text') or '')[:50]!r }")
    chk("T3 skip 后 pending_clarify 清空（旧 bug：跳过不清挂起）",
        get(f"/api/conversations/{cid2}/messages?limit=1").get("pending_clarify") is None)
    msgs2b = db_msgs(cid2)
    u2_id = next((m["id"] for m in msgs2b if m["role"] == "user"), 0)
    chk("T3 skip 也返回追认 id", rs.get("resume_of_user_msg_id") == u2_id,
        f"got={rs.get('resume_of_user_msg_id')} expect={u2_id}")
    _ask_state["asked"] = True      # 续跑不再触发 ask_user
    run_stream(cid2, {
        "message": rs["resume_text"],
        "resume_of_user_msg_id": rs.get("resume_of_user_msg_id") or 0,
        "resume_append": rs.get("append_text") or "",
    })
    users2b = [m for m in db_msgs(cid2) if m["role"] == "user"]
    chk("T3 skip 续跑后 user 消息数不增", len(users2b) == 1,
        f"user ids={[m['id'] for m in users2b]}")

    section("T4 · 追认失败兜底（传不存在的 user_msg_id）")
    cv3 = post("/api/conversations", {"title": "verify-d1-fallback"})
    cid3 = cv3.get("id")
    _ask_state["asked"] = True
    run_stream(cid3, {"message": PLAIN_INPUT})
    before = len([m for m in db_msgs(cid3) if m["role"] == "user"])
    run_stream(cid3, {"message": "【澄清补充】追认失败兜底测试",
                      "resume_of_user_msg_id": 999999999, "resume_append": "（兜底追加文本）"})
    after = db_msgs(cid3)
    users3 = [m for m in after if m["role"] == "user"]
    chk("T4 追认失败 ⇒ 回落新 user 消息（内容不丢）", len(users3) == before + 1,
        f"user {before} → {len(users3)}")
    chk("T4 兜底消息内容是完整 user_input", any("追认失败兜底测试" in (m["content"] or "")
                                              for m in users3))

    section("T5 · 普通发送不受影响（无追认字段照旧新起一轮）")
    before5 = len([m for m in db_msgs(cid3) if m["role"] == "user"])
    run_stream(cid3, {"message": "再问一个普通问题"})
    after5 = [m for m in db_msgs(cid3) if m["role"] == "user"]
    chk("T5 普通发送照旧新起 user 轮次", len(after5) == before5 + 1,
        f"user {before5} → {len(after5)}")

    # ── 清理本脚本造的会话（真库纪律）──
    section("清理 · 删除本次验证会话")
    con = sqlite3.connect(DB)
    for _cid in (cid, cid2, cid3):
        n = con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (_cid,)).fetchone()[0]
        for t in ("messages", "tool_call_logs", "llm_usage_stats", "artifacts",
                  "tool_result_offloads", "ux_metrics", "orch_stream_events"):
            try:
                con.execute(f"DELETE FROM {t} WHERE conversation_id=?", (_cid,))
            except Exception:
                pass
        con.execute("DELETE FROM conversations WHERE id=?", (_cid,))
        print(f"  会话 {_cid}（{n} 条消息）已删")
    con.commit()
    con.close()


if __name__ == "__main__":
    raise SystemExit(main())
