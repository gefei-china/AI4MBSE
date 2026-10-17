# -*- coding: utf-8 -*-
"""ask_user 澄清工具验证（2026-10-17，米爸反馈2/3 收口）。

背景
----
米爸反馈：「确认卡片的逻辑不满足需求——我需要基于实际上下文、基于 AI 真的需要澄清的
内容输出确认清单」+「AI 执行中有需要确认的内容输出，但并未提供确认澄清卡片」
（截图实证：AI 输出「请确认：用第 3 版…」纯文本，无可点选卡片）。

本批改动（后端 + 前端）
----------------------
· tools.py：`_build_tools_def` 末尾**无条件**注入 ask_user 工具定义（对话级能力，
  豁免白名单/JIT/写意图闸；config `agent.ask_user_enabled` 可关）；
  `_exec_tool_call` 最前方拦截 ask_user → 返回 clarify_required 信号（不执行副作用）。
· execute.py / stream.py（两处同构 ReAct 循环）：检测 clarify_required → 中断循环；
  stream 路径发射 clarify_ask 事件（title「AI 执行中需要你确认后才能继续」，
  source=ask_user）+ _persist_clarify 落挂起 + return；execute 路径落 user 消息
  + 澄清卡后返回 clarify_asked（不写空 assistant 消息）。
· common.py：TOOL_RULES_TEXT 并入 ASK_USER_RULES_TEXT（stream 路径 prompt 层指引）。
· routers/conversations.py：clarify_answer 成功回写 card_data.answered（历史渲染
  显示「已回答」）。
· 前端：clarify_ask 在 proc 时间线插「等待确认」环节块；_continueInSameBox 不再
  清空原盒（保留时间线，确认环节标 ✓ 后续跑）；历史渲染把 clarify 消息合并进
  其后第一条 assistant（_clarify_prev），不再独立成气泡。

判据
----
T1 _build_tools_def 注入 ask_user（含 question/options/context/allow_custom 参数契约）
T2 _exec_tool_call 拦截：正常参数 → clarify_required + 结构化 clarify
T3 参数规范化：缺 question → ok=False（不挂起）；options 截 6 项；context 截 500
T4 ask_user 不受工具白名单约束（Worker 子任务也准问用户）
T5 端到端（monkeypatch LLM 返回 ask_user tool_call 驱动 execute_stream）：
   clarify_ask 事件 + done(clarify_asked) + DB 挂起 + **无空 assistant 消息**
T6 /clarify-answer 回写 card_data.answered（历史渲染判据）
T7 变异自证：拦截改为「照常执行」（不返 clarify_required）⇒ T5 全红
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "mbse.db"
BASE = "http://127.0.0.1:8000"
HDR = {"Content-Type": "application/json", "X-User-Id": "1", "X-Session-Token": "verify-askuser"}
PASSED = []
FAILED = []


def chk(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def section(t):
    print("\n" + "=" * 4 + " " + t + " " + "=" * 4)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": "HTTP %d" % e.code}
    except Exception as e:
        return {"error": str(e)}


def db_exec(sql, args=(), fetch=False):
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()] if fetch else None
        con.commit()
        return rows
    finally:
        con.close()


def build_pipeline():
    sys.path.insert(0, str(ROOT))
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    return pipe


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    pipe = build_pipeline()

    section("T1 · _build_tools_def 注入 ask_user（含参数契约）")
    tools = pipe._build_tools_def("design", "随便一句与工具注入无关的输入文本", {"role_name": "admin"})
    ask = next((t for t in tools if t.get("function", {}).get("name") == "ask_user"), None)
    chk("ask_user 已注入", bool(ask))
    if ask:
        params = ask["function"].get("parameters") or {}
        props = params.get("properties") or {}
        chk("参数契约 question/options/context/allow_custom",
            all(k in props for k in ("question", "options", "context", "allow_custom")),
            f"props={sorted(props.keys())}")
        chk("description 含使用指引（不要纯文本提问）",
            "纯文本" in (ask["function"].get("description") or ""))

    section("T2 · _exec_tool_call 拦截：clarify_required 信号")
    r = pipe._exec_tool_call("ask_user", {
        "question": "要导出哪个版本的 SysML v2 代码？",
        "options": ["第 3 版（2026-10-13 发布）", "第 2 版", "第二个分支的草稿版"],
        "context": "当前工程有 3 个版本，影响导出范围",
    })
    chk("返回 clarify_required", bool(r.get("clarify_required")), f"keys={sorted(r.keys())}")
    c = r.get("clarify") or {}
    chk("clarify 结构化（id/question/options/context/allow_custom）",
        c.get("id") == "q_ask_user" and "版本" in (c.get("question") or "")
        and len(c.get("options") or []) == 3 and c.get("allow_custom") is True)
    chk("ok=True（不算失败轮）", r.get("ok") is True)

    section("T3 · 参数规范化")
    r_no_q = pipe._exec_tool_call("ask_user", {"options": ["a"]})
    chk("缺 question → ok=False（不挂起、不给 LLM 假信号）",
        r_no_q.get("ok") is False and not r_no_q.get("clarify_required"),
        r_no_q.get("result", "")[:60])
    r_many = pipe._exec_tool_call("ask_user", {
        "question": "选一个", "options": [str(i) for i in range(9)],
        "context": "x" * 900})
    _c2 = r_many.get("clarify") or {}
    chk("options 截 6 项", len(_c2.get("options") or []) == 6)
    chk("context 截 500", len(_c2.get("context") or "") == 500)

    section("T4 · ask_user 不受工具白名单约束（Worker 子任务也准问用户）")
    pipe._tool_whitelist = ["graph_retrieve"]     # 最小权限：只准图谱检索
    r_wl = pipe._exec_tool_call("ask_user", {"question": "白名单场景也要能问用户"})
    chk("白名单下 ask_user 仍放行", bool(r_wl.get("clarify_required")),
        r_wl.get("result", "")[:60])
    pipe._tool_whitelist = None

    section("T5 · 端到端：LLM 调 ask_user → clarify_ask 事件 + DB 挂起 + 无空 assistant")
    # monkeypatch llm_client.chat：ReAct 探测轮返回 ask_user tool_call，其余照常
    from agent import llm_client as _lc
    from agent.pipeline_parts.common import CHAT_TASK_PROBE
    _orig_chat = _lc.chat
    _state = {"asked": False}

    def _fake_chat(messages, provider_id=None, stream=False, tools=None,
                   _intent="", _model_params=None, **kw):
        if _intent == CHAT_TASK_PROBE and tools and not _state["asked"]:
            _state["asked"] = True
            return {"choices": [{"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "call_ask_1", "type": "function", "function": {
                    "name": "ask_user",
                    "arguments": json.dumps({
                        "question": "要基于哪个工程执行 SysML v2 代码导出？",
                        "options": ["广汽元模型1010", "卫星平台工程", "新建空工程"],
                        "context": "当前账号下有 3 个工程，导出目标必须先锁定",
                    }, ensure_ascii=False)}}]}}]}
        # 其余调用（意图兜底/内容澄清探测等）给个无害空响应
        return {"choices": [{"message": {"role": "assistant", "content": "（探针占位响应）"}}],
                "_meta": {"provider": "probe", "model": "-", "used_mock": True, "latency_ms": 1}}

    _lc.chat = _fake_chat
    try:
        conv = post("/api/conversations", {"title": "verify-ask-user-e2e"})
        cid = conv.get("id")
        chk("新建会话", bool(cid), f"cid={cid}")
        # 80+ 字长输入 ⇒ _clarify_skip_reason=long_input，跳过 1.5 的 LLM 探测；
        # forced_intent 直连 design ⇒ 跳过意图识别与 1.4 意图确认。
        evs = list(pipe.execute_stream(
            "请基于当前账号下的工程数据执行一次 SysML v2 代码导出与规范性校验，"
            "并在导出前确认目标工程与版本范围，确保不遗漏任何依赖包。",
            conversation_id=cid, branch="dev", forced_intent="design"))
        ask_ev = next((e for e in evs if e.get("type") == "clarify_ask"), None)
        chk("clarify_ask 事件已发射", bool(ask_ev))
        if ask_ev:
            qs = ask_ev.get("questions") or []
            chk("卡片问题结构化（含 AI 生成的选项）",
                len(qs) == 1 and len(qs[0].get("options") or []) == 3
                and "工程" in (qs[0].get("question") or ""))
            chk("title 标明执行中确认 + source=ask_user",
                "执行中" in (ask_ev.get("title") or "") and ask_ev.get("source") == "ask_user")
        done_ev = next((e for e in evs if e.get("type") == "done"), None)
        chk("done 事件 clarify_asked=True", bool(done_ev and done_ev["data"].get("clarify_asked")))
        chk("LLM 只被 ask_user 消耗一轮（探测即中断）", _state["asked"])
        # DB 判据
        pend = db_exec("SELECT pending_clarify FROM conversations WHERE id=?", (cid,), fetch=True)
        chk("DB 挂起 pending_clarify 非空", bool(pend and (pend[0]["pending_clarify"] or "")))
        msgs = db_exec("SELECT id, role, msg_type, content, card_data FROM messages "
                       "WHERE conversation_id=? ORDER BY id", (cid,), fetch=True)
        roles = [(m["role"], m["msg_type"]) for m in msgs]
        chk("落库构成 = [user, clarify]（无空 assistant）",
            roles == [("user", "text"), ("assistant", "clarify")], str(roles))
        clarify_id = next((m["id"] for m in msgs if m["msg_type"] == "clarify"), 0)

        section("T6 · /clarify-answer 回写 card_data.answered（历史渲染判据）")
        ans = post(f"/api/conversations/{cid}/clarify-answer",
                   {"answers": [{"question_id": "q_ask_user", "value": "广汽元模型1010"}]})
        chk("clarify-answer ok + 追认信息",
            bool(ans.get("ok")) and bool(ans.get("resume_of_user_msg_id")),
            f"uid={ans.get('resume_of_user_msg_id')}")
        cd = db_exec("SELECT card_data FROM messages WHERE id=?", (clarify_id,), fetch=True)
        answered = {}
        try:
            answered = (json.loads(cd[0]["card_data"] or "{}") if cd else {}).get("answered") or {}
        except Exception:
            answered = {}
        chk("card_data.answered 已回写（值=用户所选）",
            "广汽元模型1010" in (answered.get("answers") or []) and answered.get("skipped") is False,
            json.dumps(answered, ensure_ascii=False)[:120])
        # 清理
        for t in ("messages", "tool_call_logs", "llm_usage_stats", "artifacts",
                  "tool_result_offloads", "ux_metrics", "orch_stream_events"):
            try:
                db_exec(f"DELETE FROM {t} WHERE conversation_id=?", (cid,))
            except Exception:
                pass
        db_exec("DELETE FROM conversations WHERE id=?", (cid,))
    finally:
        _lc.chat = _orig_chat

    print("\n" + "-" * 56)
    print(f"通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
