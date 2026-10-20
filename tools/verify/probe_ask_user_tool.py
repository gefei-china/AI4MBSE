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
T8 变异自证（2026-11-12，米爸反馈3 复发修；**内存孪生，不写生产文件**）：
   · M1 删 stream.py 轮次循环的 `if _clarify_req: break` ⇒ 连问复现
     ⇒ T5b/T5c/T5d 全红（背景：conv 9571 实证同一 Agent 7 连问、
       conv 9553 实证 24 连问，各 3s/次、措辞各异——clarify break 只断
       工具 for 循环，轮次 while 无退出检查 ⇒ LLM 每轮重探同一上下文
       换措辞再问）；
   · M2 删 clarify 分支的 tool done 事件 yield ⇒ 前端节点永久停"执行中"
     ⇒ T5d 红、T5b/T5c 仍绿（证明两处修复各自独立可证、互不掩盖）；
   · T8e 源码级锁定 execute.py（冷路径，本门禁无 e2e 驱动）同构修复在位。
   旧门禁的假 LLM **只问一次**，对上述 bug 天然失明 ⇒ 本版改为每轮都问
   （复现真实形态），并以「探测次数=1 / ask_user 留痕=1 / done 事件存在」
   三条断言锁定。
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
    # ★ 2026-11-12：假 LLM **每轮都问、且措辞逐轮不同** —— 复现 conv 9571/9553 的
    #   真实失败形态（LLM 重探同一上下文后换措辞再问；相同参数会被 LoopGuard
    #   去重短路，反而测不到重问路径）。旧版"只问一次"对该 bug 天然失明。
    from agent import llm_client as _lc
    from agent.pipeline_parts.common import CHAT_TASK_PROBE
    _orig_chat = _lc.chat
    _state = {"probe_n": 0}

    def _fake_chat(messages, provider_id=None, stream=False, tools=None,
                   _intent="", _model_params=None, **kw):
        if _intent == CHAT_TASK_PROBE and tools:
            _state["probe_n"] += 1
            _n = _state["probe_n"]
            return {"choices": [{"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "call_ask_%d" % _n, "type": "function", "function": {
                    "name": "ask_user",
                    "arguments": json.dumps({
                        "question": "要基于哪个工程执行 SysML v2 代码导出？（第 %d 次确认）" % _n,
                        "options": ["广汽元模型1010", "卫星平台工程", "新建空工程"],
                        "context": "当前账号下有 3 个工程，导出目标必须先锁定",
                    }, ensure_ascii=False)}}]}}]}
        # 其余调用（意图兜底/内容澄清探测/检索重排等）给个无害占位响应。
        # ⚠️ content 必须是**合法 JSON**：rag_rerank 等旁路会 json.loads(content)，
        #    非 JSON 会炸出 JSONDecodeError 告警（还顺带触发 rag_rerank 自身的
        #    logging 格式化 bug——4 个参数对 3 个占位符，纯噪音）。
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}],
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
        # ★ 2026-11-12（反馈3 复发修）：三条锁定"澄清只问一次"的断言
        chk("T5b 澄清后不再重探（probe 调用=1）", _state["probe_n"] == 1,
            f"probe_n={_state['probe_n']}（>1 即轮次循环缺 clarify 退出，重库 7/24 连问同 bug）")
        ask_done_evs = [e for e in evs if e.get("type") == "tool"
                        and e.get("name") == "ask_user" and e.get("status") == "done"]
        chk("T5d ask_user 节点有 done 事件（不停在'执行中'）", len(ask_done_evs) == 1,
            f"done 事件数={len(ask_done_evs)}")
        ask_logs = db_exec("SELECT COUNT(*) AS n FROM tool_call_logs WHERE conversation_id=? "
                           "AND tool_name='ask_user'", (cid,), fetch=True)
        chk("T5c ask_user 留痕=1（不重复记日志）", bool(ask_logs) and ask_logs[0]["n"] == 1,
            f"n={ask_logs[0]['n'] if ask_logs else 'N/A'}")
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

    # ─────────────────────────────────────────────────────────────────────────
    section("T8 · 变异自证（内存孪生，不写生产文件）：两处修复各删一次 ⇒ T5b/T5c/T5d 必须全红")
    # 为什么需要这一段：T5 的三条锁定断言本身也可能是"假绿"（修复写错但断言
    # 恰好也过）。变异自证 = 把"错写法"注入**内存孪生**跑同一条 e2e，证明
    # 断言对错写法**必然红**（承 verify_orch_span.py M 段纪律，全程不落盘）。
    # 方法：exec 编译变异后的 stream.py 源码进独立命名空间（预置 __package__
    # 供 `from .common import *` 解析），派生
    #   TwinPipeline(TwinStreamMixin, AgentPipeline)
    # ⇒ execute_stream 走变异体、其余 11 个 Mixin 走真实实现。
    # 分工：M1 证"轮次循环退出"（T5b/T5c/T5d）；M2 证"done 事件"（仅 T5d），
    # 且 M2 下 T5b/T5c 仍绿 ⇒ 两修复互不掩盖、各自独立可证。
    _stream_src = (ROOT / "agent" / "pipeline_parts" / "stream.py").read_text(encoding="utf-8")
    _M1_ANCHOR = ("                    if _clarify_req:\n"
                  "                        break\n"
                  "                    if _skip_calls:")
    _M1_MUT = "                    if _skip_calls:"
    _M2_ANCHOR = ('                                yield {"type": "tool", "status": "done", "name": tname,\n'
                  '                                       "ok": True, "result": str(result.get("result") or ""),\n'
                  '                                       "error": "", "truncated": False,\n'
                  '                                       "latency_ms": int((time.time() - _t0) * 1000),\n'
                  '                                       "call_id": _call_id, "skipped": False,\n'
                  '                                       "skip_reason": "", "agent": _ctx_agent,\n'
                  '                                       "skill": _ctx_skill, "depth": 2 if _ctx_skill else 1,\n'
                  '                                       "mcp": _mcp_of(tname)}\n')
    _M2_MUT = "                                pass  # T8-M2 变异：删 done 事件（模拟修复前形态）\n"

    def _twin_pipeline(mut_src, tag):
        _ns = {"__name__": "agent.pipeline_parts.stream_%s" % tag,
               "__package__": "agent.pipeline_parts",
               "__file__": "<stream_%s>" % tag}
        exec(compile(mut_src, "<stream_%s>" % tag, "exec"), _ns)
        return type("TwinPipeline_%s" % tag, (_ns["StreamMixin"], type(pipe)), {})

    def _mut_e2e(tag, mut_src):
        """孪生管道上跑一次与 T5 同形的 e2e，返回 (probe_n, ask_done_evs, ask_logs)。"""
        _tp = _twin_pipeline(mut_src, tag)()
        from agent import llm_client as _lc2
        _orig2 = _lc2.chat
        _st = {"probe_n": 0}

        def _fake2(messages, provider_id=None, stream=False, tools=None,
                   _intent="", _model_params=None, **kw):
            if _intent == CHAT_TASK_PROBE and tools:
                _st["probe_n"] += 1
                _n = _st["probe_n"]
                return {"choices": [{"message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": "call_ask_%d" % _n, "type": "function", "function": {
                        "name": "ask_user",
                        "arguments": json.dumps({
                            "question": "要基于哪个工程执行 SysML v2 代码导出？（第 %d 次确认）" % _n,
                            "options": ["广汽元模型1010", "卫星平台工程", "新建空工程"],
                            "context": "当前账号下有 3 个工程，导出目标必须先锁定",
                        }, ensure_ascii=False)}}]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "{}"}}],
                    "_meta": {"provider": "probe", "model": "-", "used_mock": True, "latency_ms": 1}}

        _lc2.chat = _fake2
        _cid = None
        try:
            _conv = post("/api/conversations", {"title": "verify-ask-user-mut-%s" % tag})
            _cid = _conv.get("id")
            _evs = list(_tp.execute_stream(
                "请基于当前账号下的工程数据执行一次 SysML v2 代码导出与规范性校验，"
                "并在导出前确认目标工程与版本范围，确保不遗漏任何依赖包。",
                conversation_id=_cid, branch="dev", forced_intent="design"))
            _dn = len([e for e in _evs if e.get("type") == "tool"
                       and e.get("name") == "ask_user" and e.get("status") == "done"])
            _rows = db_exec("SELECT COUNT(*) AS n FROM tool_call_logs WHERE conversation_id=? "
                            "AND tool_name='ask_user'", (_cid,), fetch=True)
            return _st["probe_n"], _dn, (_rows[0]["n"] if _rows else 0)
        finally:
            _lc2.chat = _orig2
            if _cid:
                for t in ("messages", "tool_call_logs", "llm_usage_stats", "artifacts",
                          "tool_result_offloads", "ux_metrics", "orch_stream_events"):
                    try:
                        db_exec(f"DELETE FROM {t} WHERE conversation_id=?", (_cid,))
                    except Exception:
                        pass
                db_exec("DELETE FROM conversations WHERE id=?", (_cid,))

    _m1 = _stream_src.replace(_M1_ANCHOR, _M1_MUT)
    chk("M1a 变异锚点命中（轮次循环 clarify 退出行）", _m1 != _stream_src)
    if _m1 != _stream_src:
        _pn, _dn, _ln = _mut_e2e("m1", _m1)
        chk("M1b 删轮次退出 ⇒ T5b 抓住（不再'只问一次'）", _pn > 1,
            f"probe_n={_pn}（>1 即复现 conv 9571/9553 连问形态）")
        chk("M1c 删轮次退出 ⇒ T5c 抓住（ask_user 留痕爆炸）", _ln > 1, f"logs={_ln}")
        chk("M1d 删轮次退出 ⇒ T5d 抓住（done 事件≠1）", _dn != 1, f"done={_dn}")

    _m2 = _stream_src.replace(_M2_ANCHOR, _M2_MUT)
    chk("M2a 变异锚点命中（clarify 分支 done 事件 yield）", _m2 != _stream_src)
    if _m2 != _stream_src:
        _pn2, _dn2, _ln2 = _mut_e2e("m2", _m2)
        chk("M2b 删 done 事件 ⇒ T5d 抓住（前端节点会停'执行中'）", _dn2 == 0, f"done={_dn2}")
        chk("M2c 删 done 事件不误伤'只问一次'（T5b/T5c 仍绿 ⇒ 两修复互不掩盖）",
            _pn2 == 1 and _ln2 == 1, f"probe_n={_pn2}, logs={_ln2}")

    # execute.py（冷路径，本门禁无 e2e 驱动）—— 源码级锁定同构修复在位：
    # 并行调用场景若缺此 break ⇒ tool_results 非空 ⇒ 回填 pmsg ⇒ ask_user 的
    # tool_call 悬空 ⇒ 下一轮探测 HTTP 400（协议硬约束）。
    _exe_src = (ROOT / "agent" / "pipeline_parts" / "execute.py").read_text(encoding="utf-8")
    chk("T8e execute.py 同构修复在位（轮次循环 clarify 退出）",
        _exe_src.count("if _clarify_req:\n                        break") == 1,
        "防回归：并行调用悬空 tool_call ⇒ 下轮 400")

    print("\n" + "-" * 56)
    print(f"通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
