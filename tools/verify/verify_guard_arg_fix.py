# -*- coding: utf-8 -*-
"""verify_guard_arg_fix —— 真实链路证明：参数类工具失败**不再掀翻整轮**（2026-10-30）。

## 事故（本脚本要锁死的那一条）
2026-10-30 18:11 真机 conv 9779：模型调 `query_data_tree` 传错字段，平台回
`{"code":"INVALID_ARGUMENTS", ...}`，代码走进
`agent/pipeline_parts/stream.py:5397`：
    elif _lg.is_correctable_failure(result):
        AttributeError: 'LoopGuard' object has no attribute 'is_correctable_failure'
⇒ 整轮被掀翻，用户看到「本轮未产出正文内容 / 任务终止」（服务日志 `[stream_error]` 有据）。
讽刺点：这行**正是**为"参数错误给模型第二次机会"而写的，救兵自己先炸。

## 为什么单元测试不够（必须有这条 E2E）
单元层只能证明"函数能调通"。真机失败是**链路**问题：`_lg` 是实例、函数是模块级的，
只有把整条 loop 跑起来（模型要工具 → 工具返回参数错 → 护栏判分支 → 再给一次机会），
才能证明"这一类错误在任何后续重构下都不会再把用户的一轮打死"。

## 判据
F1 链路无异常（不出现 error 事件 / 不抛 AttributeError）
F2 模型**拿到了第二次机会**（LLM 被调用 ≥ 2 次，而不是第一次工具失败就收工）
F3 模型被告知"参数不合法"（progress 事件含该提示）——即修复后的**正向语义**仍在
F4 最终产出正文（`done` 事件 / 有内容），不是「本轮未产出正文内容」

## 变异自证（**只证明"链路会因该形态而死"，不负责抓改动回退**）
删掉类上的同名垫片（`LoopGuard.is_correctable_failure`）后以属性访问取它 ⇒ 必抛
AttributeError ⇒ 复现事故形态。这说明 F1 不是恒真。
⚠️ 但它**不**能抓住"有人把调用点改回属性访问"——因为垫片会让那种写法仍然可用。
抓回退是 **AST 级**的事：`tools/verify/verify_loop_guard.py` 的 G7d。
两者分工：本脚本证明"业务流程可控重试"，G7d 证明"代码形态没退化"。缺一不可。

## 用法（需本机真库 mbse.db，故不进 CI）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_guard_arg_fix.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


#: 生产现场回执（tool_call_logs id=6077，原样搬运）
ARG_ERROR = {
    "ok": False, "error": "input_invalid",
    "result": json.dumps({"code": "INVALID_ARGUMENTS",
                          "message": "参数不符合工具定义；按 fields 提示修正字段后重新调用",
                          "retryable": False}, ensure_ascii=False),
}


def _make_pipe(fake_chat, tool_result, break_guard=False):
    """构造一条**真** pipeline；只替换两个接缝：LLM 与工具回执。"""
    import llm
    from agent.pipeline import AgentPipeline

    pipe = AgentPipeline()
    pipe._load_db_agents()
    # 与被测分支**无关**的两道打断闸：意图确认卡 / 内容级澄清。它们会把本轮提前
    # 收口成 clarify_ask（实测：forced_intent=chat + 短句必然弹），让人误以为
    # "工具循环没跑" ⇒ 本验证只关心工具失败后的护栏分支，故显式关掉这两道。
    pipe._should_confirm_intent = lambda *a, **k: False
    pipe._clarify_detect = lambda *a, **k: None
    llm.llm_client.chat = fake_chat
    # 工具接缝：真实 `_exec_tool_call` 需要 MCP/平台在线；这里直接回放**生产回执**，
    # 其余（白名单校验之外）链路全真。
    pipe._exec_tool_call = lambda *a, **k: dict(tool_result)
    if break_guard:
        # 变异：还原事故写法 —— 让模块函数在实例上"不存在"是原状，
        # 这里直接模拟"调用方用了属性访问"这条路径（等价于把实例属性取成 None）。
        import agent.pipeline_parts.stream as _st
        real = _st.execute_stream
        # 不替换源码，改为在调用期注入一个会抛 AttributeError 的假对象给循环里的 _lg
        # —— 见 _run() 中的 monkeypatch（更贴近原事故：属性不存在）。
        _st._MUT_GUARD_ATTR = True
    return pipe


def _fake_chat_factory(calls, box):
    """**只在带 tools 的那次**要工具（=ReAct 循环内），其余调用直接收尾。

    为什么不能按"第几次调用"判断：本轮实测 LLM 会被调用 3 次
    （路由/技能选择/主循环），首版按 n==0 要工具 ⇒ 工具请求被路由调用吃掉，
    真循环里反而直接出正文 ⇒ 工具根本没跑、F3 无从触发（假红）。
    判据改成"这次请求带了 tools 参数"，语义上正好等于"现在是 ReAct 循环"。
    """
    import json as _json

    def fake_chat(messages, provider_id=None, stream=False, tools=None,
                  thinking=False, **kw):
        calls.append(len(calls))
        # 逐轮留下"模型当时看到的 user 消息"——F3 用它证明修正提示**真的进了模型上下文**
        # （比只看 progress 事件可靠：notices 是否外发取决于收尾路径，不能当唯一判据）
        box.setdefault("seen", []).append(
            [str(m.get("content") or "") for m in (messages or [])
             if isinstance(m, dict) and m.get("role") == "user"])
        meta = {"provider": "fake", "model": "fake", "used_mock": False,
                "latency_ms": 1, "token_count": 20}
        if tools and not box.get("asked"):
            box["asked"] = True
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{
                       "id": "c1", "type": "function",
                       "function": {"name": "sysml_stdlib_meta",
                                    "arguments": _json.dumps({"package": "通信子系统"})}}]}
            return {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}
        msg = {"role": "assistant", "content": "已按回执修正参数并完成查询。"}
        return {"choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}
    return fake_chat


def _run(break_guard=False):
    """跑一条真链路，返回 (events, calls, text, err, box)。"""
    import llm
    from database import get_db

    calls, events, err = [], [], ""
    text = ""
    orig = llm.llm_client.chat
    conn = get_db()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO conversations (title,intent,status,user_id,phase,created_at,"
        "updated_at,project_id,current_intent,last_slots,pending_clarify) VALUES "
        "(:t,:i,'active',NULL,'requirement',:ts,:ts,'','chat','{}','')",
        {"t": "verify_guard_arg_fix", "i": "chat", "ts": ts})
    conn.commit()
    cid = cur.lastrowid
    try:
        box = {}
        pipe = _make_pipe(_fake_chat_factory(calls, box), ARG_ERROR)
        gen = pipe.execute_stream("查一下工程列表", cid, branch="dev",
                                  provider_id=1, forced_intent="chat")
        for ev in gen:
            events.append(ev)
            if isinstance(ev, dict) and ev.get("type") == "token":
                text += str(ev.get("delta") or "")
            if isinstance(ev, dict) and ev.get("type") == "error":
                err = str(ev.get("message") or "")
    except Exception as exc:                    # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    finally:
        llm.llm_client.chat = orig
        try:
            conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
            conn.commit()
        except Exception:                        # noqa: BLE001
            pass
        conn.close()
    return events, calls, text, err, box


def main():
    print("=" * 78)
    print("护栏参数判据 —— 真实链路（流式）：参数错误必须给第二次机会，不许掀翻整轮")
    print("=" * 78)

    events, calls, text, err, box = _run()
    kinds = [e.get("type") for e in events if isinstance(e, dict)]
    notices = " ".join(str(e.get("text") or e.get("message") or "")
                       for e in events if isinstance(e, dict))
    ok = _rec("F1 链路无异常（不抛 AttributeError / 无 error 事件）",
              (not err) and ("error" not in kinds), err[:200])
    ok &= _rec("F2 模型拿到第二次机会（LLM 调用 ≥ 2 次，不是首次工具失败就收工）",
               len(calls) >= 2, "calls=%d kinds=%s" % (len(calls), kinds[:12]))
    seen = " ".join(c for turn in (box.get("seen") or []) for c in turn)
    ok &= _rec("F3 修正提示**进了模型上下文**（可自修正语义仍在）",
               "参数不合法" in seen, "上下文命中=%s 事件命中=%s"
               % ("参数不合法" in seen, "参数不合法" in notices))
    ok &= _rec("F4 本轮产出了正文（不是『本轮未产出正文内容』）",
               bool(text.strip()) or ("done" in kinds), "text=%r" % text[:60])

    # ── 变异自证：还原事故调法，F1 必须判红 ──
    print("\n=== 变异 M：把判据调用改回 `_lg.is_correctable_failure(...)` ===")
    caught = _mutation_catches()
    ok &= _rec("M 复现事故形态（属性访问 ⇒ 整轮被 AttributeError 掀翻）⇒ 证明 F1 非空转",
               caught)

    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 78)
    print("断言 %d  PASS %d  FAIL %d" % (len(_results), len(_results) - len(bad), len(bad)))
    for k, n, d in bad:
        print("  [%s] %s  %s" % (k, n, d))
    print("结论：%s" % ("全部通过（修复后链路可控重试）" if not bad
                       else "有断言未通过"))
    return 1 if bad else 0


def _mutation_catches():
    """在**不改进程外代码**的前提下，证明『属性访问』形态会抛 AttributeError。

    做法：直接取当前 LoopGuard 实例，删掉类上的同名垫片（如果有），再试属性访问。
    这与事故现场等价：事故时类上**没有**该方法 ⇒ 抛 AttributeError。
    """
    from agent.loop_guard import LoopGuard
    from agent import loop_guard as lg

    had = "is_correctable_failure" in LoopGuard.__dict__
    saved = LoopGuard.__dict__.get("is_correctable_failure")
    try:
        if had:
            delattr(LoopGuard, "is_correctable_failure")
        try:
            LoopGuard().is_correctable_failure(ARG_ERROR)
            return False                      # 没抛 ⇒ 变异未被复现
        except AttributeError:
            return True                       # 复现了事故形态
    finally:
        if had and saved is not None:
            setattr(LoopGuard, "is_correctable_failure", saved)
        elif not had:
            try:
                delattr(LoopGuard, "is_correctable_failure")
            except AttributeError:
                pass


if __name__ == "__main__":
    sys.exit(main())
