# -*- coding: utf-8 -*-
"""澄清上下文化验证（2026-10-17，米爸反馈「确认卡还是系统默认内容」收口）。

背景
----
米爸反馈（会话 9446 实证，附截图）：「我最新发起一次会话，澄清确认卡还是会有
系统默认的内容，应该是没有基于上下文分析提供准确的确认信息」。

实证还原（DB 会话 9446）：
  1) 用户「导出广汽元模型1010的sysml v2源码」→ AI 执行中调 ask_user 弹出
     **上下文相关**确认卡（「用哪个版本、什么范围？」+ 版本3/2/1 选项）✓
  2) 用户选「版本 3 + 整工程导出」→ AI 续跑失败，明确邀请「下一轮先补全
     分支→版本标识」「您若方便，也可以直接告我该工程下分支的名称」
  3) 用户答「取主分支最新版本数据」（9 字，对邀请的直接承接）→ 却被阶段 1.4
     「我不确定你想让你做哪件事」+ 10 个固定系统意图拦住 —— 上下文生生断裂。

本批改动（三层防线）
------------------
· session.py `_is_clarify_followup`：**零成本结构检测**——本会话有过
  澄清→作答（answered 卡）且本轮是短输入（≤25 字）⇒ 判定为承接补充，
  两道澄清门（1.4/1.5）全跳过，Agent 基于完整上下文自行理解。
· session.py `_llm_intent_confirm_questions`：**LLM 上下文判断 + 卡片动态化**——
  真要触发 1.4 时，LLM 结合「用户输入 + 最近几轮上下文 + 可选意图清单」判断：
  意图已明确（多为承接轮误触发）⇒ 返回 skip，连卡都不弹；确需拍板 ⇒ 生成
  针对本次场景的问题与选项（选项中文名须映射回系统意图 key，保证续答可路由）；
  失败/Mock ⇒ 返回 None，调用方兜底固定列表版（确定性可回归）。
· stream.py 接线：承接轮跳过两道门 + reasoning 明示；1.4 主路径走 LLM 版，
  兜底固定列表版；1.5 承接轮同样跳过。
· config：`intent.clarify_followup_max_chars`（默认 25）、
  `intent.confirm_llm_dynamic`（默认 True，设置页可配）。

判据
----
A1 answered 卡 + 本轮短 user（卡后唯一）⇒ 承接 True（复现 9446 形态）
A2 长输入（>25 字）⇒ False（新任务不该被误放行……哦不，是不被误判承接）
A3 无 answered 卡 ⇒ False
A4 卡后有多条 user（中间开过新话题）⇒ False
A5 卡后无 assistant 产出（答完还没续跑）⇒ False
A6 无 conversation_id ⇒ False
B1 LLM 判 need=false + 合法 intent ⇒ ("skip", intent)（误触发纠正）
B2 LLM 判 need=false + 非法 intent key ⇒ None（不敢放行）
B3 LLM 判 need=true + 选项可映射 ⇒ ("ask", [针对性卡片])
B4 LLM 选项中文名无法映射（<2 有效）⇒ None（调用方兜底固定列表）
B5 LLM 抛异常 ⇒ None（兜底）
B6 开关 confirm_llm_dynamic=False ⇒ None（可回退旧行为）
B7 LLM 返回空/非 JSON ⇒ None（兜底）
C1 端到端 9446 形态：answered 卡 + 短输入 ⇒ **不弹 clarify_ask**、放行执行
C2 端到端无承接 + LLM 判 skip ⇒ 不弹卡 + reasoning 明示
C3 端到端无承接 + LLM 判 ask ⇒ 弹 clarify_ask + 卡片为上下文相关内容
C4 变异自证由 verify 主流程外的手动步骤承担（见 commit message）
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "mbse.db"
BASE = "http://127.0.0.1:8000"
HDR = {"Content-Type": "application/json", "X-User-Id": "1", "X-Session-Token": "verify-ctx"}
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
        with urllib.request.urlopen(req, timeout=180) as r:
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
    return AgentPipeline()


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    pipe = build_pipeline()
    _test_convs = []

    def _mk_conv(title="verify-ctx"):
        r = post("/api/conversations", {"title": title})
        cid = r.get("id")
        if cid:
            _test_convs.append(cid)
        return cid

    # ═══ A 组：承接检测（结构判据，DB 实测）═══
    section("A 组 · _is_clarify_followup 承接检测（零成本防线）")

    # A1：9446 形态 = answered 澄清卡 + assistant 续跑产出 + 本轮短 user（卡后唯一）
    cid_a1 = _mk_conv("verify-ctx-a1")
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a1, "user", "导出广汽元模型1010的sysml v2源码", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a1, "assistant", "需要您确认建模信息后继续", "clarify",
             json.dumps({"questions": [{"id": "q_ask_user", "question": "用哪个版本？",
                                        "options": ["版本3", "版本2"]}],
                         "answered": {"at": "2026-10-17 12:29:05",
                                      "answers": ["版本 3（最新）+ 整工程导出"],
                                      "skipped": False}}, ensure_ascii=False)))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a1, "assistant", "## 导出未完成…下一轮先补全分支→版本标识，"
            "您若方便也可以直接告我分支的名称。", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a1, "user", "取主分支最新版本数据", "text", None))
    r_a1 = pipe._is_clarify_followup(cid_a1, "取主分支最新版本数据")
    chk("A1 9446 形态 ⇒ 承接 True", r_a1 is True, f"got={r_a1}")

    # A2：长输入 ⇒ False（长输入更像新任务，25 字阈值取自 config）
    chk("A2 长输入（>25 字）⇒ False",
        pipe._is_clarify_followup(cid_a1, "请帮我把这个工程的所有分支和版本信息都完整列出来对比一下哪个最新") is False)

    # A3：无 answered 卡（普通会话）⇒ False
    cid_a3 = _mk_conv("verify-ctx-a3")
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a3, "user", "你好", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a3, "assistant", "你好，有什么可以帮你？", "text", None))
    chk("A3 无 answered 卡 ⇒ False",
        pipe._is_clarify_followup(cid_a3, "查一下") is False)

    # A4：卡后有多条 user（中间开过新话题）⇒ False
    cid_a4 = _mk_conv("verify-ctx-a4")
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a4, "assistant", "需要您确认建模信息后继续", "clarify",
             json.dumps({"questions": [{"id": "q", "question": "x", "options": ["a"]}],
                         "answered": {"at": "t", "answers": ["a"], "skipped": False}},
                        ensure_ascii=False)))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)", (cid_a4, "assistant", "续跑产出", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)", (cid_a4, "user", "换个话题：今天天气如何", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)", (cid_a4, "assistant", "晴天", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)", (cid_a4, "user", "取主分支", "text", None))
    chk("A4 卡后开过新话题（多条 user）⇒ False",
        pipe._is_clarify_followup(cid_a4, "取主分支") is False)

    # A5：卡后无 assistant 产出（答完还没续跑）⇒ False
    cid_a5 = _mk_conv("verify-ctx-a5")
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_a5, "assistant", "需要您确认建模信息后继续", "clarify",
             json.dumps({"questions": [{"id": "q", "question": "x", "options": ["a"]}],
                         "answered": {"at": "t", "answers": ["a"], "skipped": False}},
                        ensure_ascii=False)))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)", (cid_a5, "user", "取主分支", "text", None))
    chk("A5 卡后无 assistant 产出 ⇒ False",
        pipe._is_clarify_followup(cid_a5, "取主分支") is False)

    # A6：无 conversation_id ⇒ False
    chk("A6 无 conversation_id ⇒ False",
        pipe._is_clarify_followup(0, "取主分支最新版本数据") is False)

    # ═══ B 组：LLM 动态化卡片（monkeypatch llm_client.chat）═══
    section("B 组 · _llm_intent_confirm_questions 上下文判断 + 动态卡片")
    # _db_intents 由 _load_db_agents 填充（懒加载，构造时不注册）——B 组直调方法前先触发，
    # 与线上 execute_stream 的调用顺序一致（1790 行加载 → 2040 行澄清门）
    pipe._load_db_agents()
    _n_intents = len([k for k in (getattr(pipe.router, "_db_intents", {}) or {}) if k != "chat"])
    chk("前置：_db_intents 已注册（候选清单非空）", _n_intents > 0, f"n={_n_intents}")
    from llm import llm_client as _lc      # llm/__init__.py 的单例实例（session.py 同源）
    _orig_chat = _lc.chat

    def _patch(ret_obj):
        def _f(messages, provider_id=None, stream=False, tools=None,
               _intent="", _model_params=None, **kw):
            if isinstance(ret_obj, Exception):
                raise ret_obj
            return ret_obj
        _lc.chat = _f

    def _restore():
        _lc.chat = _orig_chat

    def _llm_resp(payload):
        return {"choices": [{"message": {"role": "assistant",
                                         "content": json.dumps(payload, ensure_ascii=False)}}],
                "_meta": {"provider": "probe", "model": "-", "used_mock": True}}

    # B1：need=false + 合法 intent ⇒ skip
    _patch(_llm_resp({"need": False, "intent": "design"}))
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    chk("B1 need=false + 合法 intent ⇒ ('skip', intent)",
        _r is not None and _r[0] == "skip" and _r[1] == "design", f"got={_r}")

    # B2：need=false + 非法 intent key ⇒ None（不敢放行）
    _patch(_llm_resp({"need": False, "intent": "not_a_real_intent"}))
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    chk("B2 need=false + 非法 intent ⇒ None", _r is None, f"got={_r}")

    # B3：need=true + 针对性卡片 + 选项可映射
    _patch(_llm_resp({"need": True, "questions": [{
        "id": "q1",
        "question": "你提到「取主分支最新版本数据」——是要继续上一轮「广汽元模型1010」"
                    "的 SysML v2 源码导出吗？用哪个分支的哪个版本？",
        "options": ["设计/建模：继续导出广汽元模型1010，主分支+最新版本",
                    "设计/建模：改为先建 SysML v2 模型再导出",
                    "知识问答：只是了解分支与版本的关系"],
        "allow_custom": True}]}))
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    _ok = (_r is not None and _r[0] == "ask" and len(_r[1]) == 1)
    if _ok:
        _q = _r[1][0]
        _ok = ("广汽元模型1010" in (_q.get("question") or "")     # 上下文相关（非模板）
               and len(_q.get("options") or []) == 3
               and len(_q.get("option_intents") or []) == 3       # 全部可映射
               and _q.get("allow_custom") is True)
    chk("B3 need=true ⇒ ('ask', [上下文相关卡片]) + 选项可映射", _ok, f"got={_r}")

    # B4：选项中文名无法映射（<2 有效）⇒ None
    _patch(_llm_resp({"need": True, "questions": [{
        "id": "q1", "question": "随便问", "options": ["完全无关的选项A", "完全无关的选项B"],
        "allow_custom": True}]}))
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    chk("B4 选项无法映射（<2 有效）⇒ None（调用方兜底固定列表）", _r is None, f"got={_r}")

    # B5：LLM 抛异常 ⇒ None
    _patch(RuntimeError("provider down"))
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    chk("B5 LLM 异常 ⇒ None（兜底）", _r is None, f"got={_r}")

    # B6：开关关闭 ⇒ None（可回退旧行为）。monkeypatch config.get（不写真实配置文件）
    _patch(_llm_resp({"need": True, "questions": [{
        "id": "q1", "question": "x", "options": ["设计/建模：a", "知识问答：b"],
        "allow_custom": True}]}))
    from core import config as _cfg
    _orig_get = _cfg.get

    def _get_off(section, key=None, default=None):
        if section == "intent" and key == "confirm_llm_dynamic":
            return False
        return _orig_get(section, key, default)

    _cfg.get = _get_off
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    _cfg.get = _orig_get
    chk("B6 confirm_llm_dynamic=False ⇒ None（回退固定列表）", _r is None, f"got={_r}")

    # B7：空/非 JSON ⇒ None
    _patch({"choices": [{"message": {"role": "assistant", "content": "我不是 JSON"}}]})
    _r = pipe._llm_intent_confirm_questions("取主分支最新版本数据", "chat", cid_a1)
    chk("B7 非 JSON 响应 ⇒ None（兜底）", _r is None, f"got={_r}")

    _restore()

    # ═══ C 组：端到端 execute_stream ═══
    section("C 组 · 端到端 execute_stream（9446 场景复现）")

    def _mk_fake_llm(confirm_payload):
        """意图兜底给 chat/conf0（触发 1.4 判定），澄清探测按 confirm_payload 走。"""
        def _f(messages, provider_id=None, stream=False, tools=None,
               _intent="", _model_params=None, **kw):
            _sys = ""
            for _m in messages:
                if _m.get("role") == "system":
                    _sys = _m.get("content") or ""
                    break
            if "意图澄清模块" in _sys:
                return {"choices": [{"message": {"role": "assistant",
                        "content": json.dumps(confirm_payload, ensure_ascii=False)}}],
                        "_meta": {"provider": "probe", "model": "-", "used_mock": True}}
            if "建模澄清助手" in _sys:
                return {"choices": [{"message": {"role": "assistant",
                        "content": json.dumps({"need": False}, ensure_ascii=False)}}]}
            # 其他（意图兜底/生成）：给无害空响应
            return {"choices": [{"message": {"role": "assistant",
                    "content": "（探针占位：已放行执行）"}}],
                    "_meta": {"provider": "probe", "model": "-", "used_mock": True}}
        return _f

    # C1：9446 形态（承接轮）⇒ 不弹卡、放行执行
    cid_c1 = _mk_conv("verify-ctx-c1")
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_c1, "user", "导出广汽元模型1010的sysml v2源码", "text", None))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_c1, "assistant", "需要您确认建模信息后继续", "clarify",
             json.dumps({"questions": [{"id": "q_ask_user", "question": "用哪个版本？",
                                        "options": ["版本3", "版本2"]}],
                         "answered": {"at": "t", "answers": ["版本 3（最新）+ 整工程导出"],
                                      "skipped": False}}, ensure_ascii=False)))
    db_exec("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (cid_c1, "assistant", "## 导出未完成…下一轮先补全分支→版本标识。", "text", None))
    _lc.chat = _mk_fake_llm({"need": False, "intent": "design"})
    try:
        evs = list(pipe.execute_stream("取主分支最新版本数据",
                                       conversation_id=cid_c1, branch="dev"))
    finally:
        _restore()
    _ask = next((e for e in evs if e.get("type") == "clarify_ask"), None)
    _agent_run = next((e for e in evs if e.get("type") == "agent"
                       and e.get("status") == "run"), None)
    chk("C1 承接轮 ⇒ 不弹 clarify_ask（不再被固定意图清单拦住）", _ask is None)
    chk("C1 承接轮 ⇒ 放行执行（agent run 事件出现）", _agent_run is not None)
    _carry = any("承接上轮" in str((e.get("delta") or "")) for e in evs
                 if e.get("type") == "reasoning")
    chk("C1 承接判定对用户可见（reasoning 明示）", _carry)

    # C2：无承接 + LLM 判 skip ⇒ 不弹卡 + reasoning 明示
    cid_c2 = _mk_conv("verify-ctx-c2")
    _lc.chat = _mk_fake_llm({"need": False, "intent": "design"})
    try:
        evs2 = list(pipe.execute_stream("帮我弄一下那个工程的版本",
                                        conversation_id=cid_c2, branch="dev"))
    finally:
        _restore()
    _ask2 = next((e for e in evs2 if e.get("type") == "clarify_ask"), None)
    chk("C2 LLM 判 skip ⇒ 不弹卡（误触发被纠正）", _ask2 is None)
    _skip_note = any("结合上下文判断" in str((e.get("delta") or "")) for e in evs2
                     if e.get("type") == "reasoning")
    chk("C2 skip 判定对用户可见（reasoning 明示）", _skip_note)

    # C3：无承接 + LLM 判 ask ⇒ 弹上下文相关卡片
    cid_c3 = _mk_conv("verify-ctx-c3")
    _patch(_llm_resp({"need": True, "questions": [{
        "id": "q1",
        "question": "你提到「工程的版本」——是要导出广汽元模型1010 的 SysML v2 源码吗？"
                    "该工程有 3 个已发布版本，用哪个？",
        "options": ["设计/建模：导出广汽元模型1010，版本3（最新）+整工程",
                    "设计/建模：导出广汽元模型1010，版本2+整工程",
                    "知识问答：只是了解版本情况，不导出"],
        "allow_custom": True}]}))
    # _patch 已装（B 组同款 fake），注意 C3 的 execute_stream 里意图兜底也走同一 fake，
    # 但意图兜底的 system 不含「意图澄清模块」⇒ 落到空响应分支，不影响判定。
    try:
        evs3 = list(pipe.execute_stream("帮我弄一下那个工程的版本",
                                        conversation_id=cid_c3, branch="dev"))
    finally:
        _restore()
    _ask3 = next((e for e in evs3 if e.get("type") == "clarify_ask"), None)
    _ok3 = bool(_ask3)
    if _ok3:
        _qs = _ask3.get("questions") or []
        _ok3 = (len(_qs) == 1 and "广汽元模型1010" in (_qs[0].get("question") or "")
                and len(_qs[0].get("options") or []) == 3
                and len(_qs[0].get("option_intents") or []) == 3)
    chk("C3 LLM 判 ask ⇒ 弹上下文相关卡片（问题点名上下文 + 选项可映射）", _ok3,
        f"ask={bool(_ask3)}")
    _pend3 = db_exec("SELECT pending_clarify FROM conversations WHERE id=?", (cid_c3,), fetch=True)
    chk("C3 DB 挂起 pending_clarify 非空", bool(_pend3 and (_pend3[0]["pending_clarify"] or "")))

    # ═══ 清理 ═══
    section("清理测试会话")
    for _cid in _test_convs:
        for _t in ("messages", "tool_call_logs", "llm_usage_stats", "artifacts",
                   "tool_result_offloads", "ux_metrics", "orch_stream_events"):
            try:
                db_exec(f"DELETE FROM {_t} WHERE conversation_id=?", (_cid,))
            except Exception:
                pass
        db_exec("DELETE FROM conversations WHERE id=?", (_cid,))
    print(f"  已清理 {len(_test_convs)} 个测试会话")

    print("\n" + "=" * 60)
    print(f"总计：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("失败项：")
        for _f in FAILED:
            print("  - " + _f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
