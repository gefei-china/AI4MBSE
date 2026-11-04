# -*- coding: utf-8 -*-
"""证明：跨轮省略**不再毁掉可寻址性**，子任务的会话级工具**不再无上下文**（2026-10-30）。

## 事故形态（用户原话：「每次经过人工确认后，就会无辜终止」）
人工确认（clarify-answer）→ 续跑进编排 → 子任务以 `conversation_id=0` 跑 →
从第 3 轮起，模型看到的所有工具结果都是
`{"ok":true,"result":"","note":"前序工具结果已省略"}` ——**明细与 offload 编号一起没了**。
模型原话：「本轮所有读接口回执均为『前序工具结果已省略』——没有拿到任何一个可核验的元素明细」。
随后它只能猜编号（真机 `tool_call_logs` id=6238：`offload #2608 不存在`，而 #2608 是
**下一笔**调用的编号）+ 重查 + `todo_write` 报「无会话上下文」⇒ 失败轮撞 3 次上限 ⇒ 强制收尾。

## 为什么必须真链路（单元测不出来）
两处都是**装配期行为**：
  · 省略发生在 ReAct 循环里，判据依赖"第几轮 + 消息下标"；
  · `session_conv_id` 的价值在于它**真的被传进 `_tool_conv_ctx`**，进而影响 `todo_write`。
只测"函数能返回 dict"证明不了接线，而这次事故正是接线错误。

## 判据
A1 【保留上一轮】第 2 轮起，上一轮的工具体原文**完好**（含 20000 字的正文标记）
A2 【可寻址】第 3 轮时更早轮次的工具体被省略，但**带 offload_id / head / 重读指引**
A3 【能重读】用 A2 给出的 offload_id 调 `tool_result_fetch` 能取回含正文标记的全文
A4 【子任务归属】`conversation_id=0 + session_conv_id=<真会话>` 下，`todo_write` 成功建待办
A5 【变异自证】把 `_KEEP_TOOL_ROUNDS` 置 0（= 修复前逐轮抹空）⇒ A1 必须判红

## 用法（需本机真库 + 真 pipeline；不调外部 LLM）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_tool_result_continuity.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []
#: 正文标记：出现在大文件里，用来判断"模型到底还看不看得到原文"
MARK = "CONTINUITY_MARK_7f3a"


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _mk_big_file(n_chars=20000):
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="continuity_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(MARK + "\n" + ("x" * (n_chars - len(MARK) - 1)))
    return path


def _tool_msgs(messages):
    """取出本轮 messages 里的 tool 消息（按出现顺序）。"""
    return [m for m in (messages or []) if isinstance(m, dict) and m.get("role") == "tool"]


def _run(read_path, keep_rounds=None, session_conv_id=None, ask_todo=False):
    """跑一条真链路，返回每一轮 LLM 看到的 messages 快照。

    keep_rounds: None=用模块默认；其它值=临时改写（变异用）
    session_conv_id: None=不传（子任务修复前形态）；否则传该值
    """
    import llm
    from database import get_db
    from agent.pipeline_parts import stream as _st

    calls, rounds, err, box = [], [], "", {}
    orig_chat = llm.llm_client.chat
    orig_keep = _st._KEEP_TOOL_ROUNDS
    conn = get_db()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO conversations (title,intent,status,user_id,phase,created_at,"
        "updated_at,project_id,current_intent,last_slots,pending_clarify) VALUES "
        "(:t,:i,'active',NULL,'requirement',:ts,:ts,'','chat','{}','')",
        {"t": "verify_tool_continuity", "i": "chat", "ts": ts})
    conn.commit()
    cid = cur.lastrowid
    # ⚠️ 必须**在 conn 建好之后**取基线：第一版放在函数开头（那时 conn 还没建），
    #   异常被 `except` 吞成 0 ⇒ 判据退化成"数全历史 conv=0 行" ⇒ 恒红 1480。
    #   这类"兜底把判据变成另一个问题"的写法比直接崩更危险，注释留作警示。
    _max_log_id = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM tool_call_logs").fetchone()[0]

    if keep_rounds is not None:
        _st._KEEP_TOOL_ROUNDS = int(keep_rounds)

    def chat(messages, provider_id=None, stream=False, tools=None, thinking=False, **kw):
        n = len(calls)
        calls.append(n)
        # 快照：每个 LLM 轮次看到的 messages（深拷贝，避免后续被原地改写污染判据）
        try:
            rounds.append(json.loads(json.dumps(messages or [], ensure_ascii=False, default=str)))
        except Exception:                       # noqa: BLE001
            rounds.append([dict(m) if isinstance(m, dict) else m for m in (messages or [])])
        meta = {"provider": "fake", "model": "fake", "used_mock": False,
                "latency_ms": 1, "token_count": 20}
        if not tools:
            return {"choices": [{"message": {"role": "assistant", "content": "收尾"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}
        # ⚠️ 按"**已发出的工具轮数**"计数，不能按 `len(calls)`：
        #   循环之前还有 CHAT_TASK_PROBE 等若干次不带 tools 的调用，
        #   用总调用序号会让"3 轮工具"实际只发生 1 轮（第一版就栽在这里：
        #   断言拿到 0 条 tool 消息，看着像"省略逻辑没生效"）。
        if ask_todo and box.get("todo_done") != 1:
            box["todo_done"] = 1
            call = {"name": "todo_write",
                    "arguments": {"action": "create", "subject": "验证子任务会话归属",
                                  "verify": "人工确认"}}
        elif box.get("tool_round", 0) < 3:
            box["tool_round"] = box.get("tool_round", 0) + 1
            call = {"name": "file_read", "arguments": {"path": read_path}}
        else:
            return {"choices": [{"message": {"role": "assistant", "content": "收尾"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c%d" % n, "type": "function",
                               "function": {"name": call["name"],
                                            "arguments": json.dumps(call["arguments"],
                                                                    ensure_ascii=False)}}]}
        return {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}

    todo_res = []
    try:
        from agent import AgentPipeline
        pipe = AgentPipeline()
        pipe._load_db_agents()
        pipe._should_confirm_intent = lambda *a, **k: False
        pipe._clarify_detect = lambda *a, **k: None
        llm.llm_client.chat = chat
        kwargs = {}
        if session_conv_id is not None:
            kwargs["session_conv_id"] = session_conv_id
        # 子任务形态：conversation_id 恒 0（约定：不落 messages）
        conv_arg = 0 if session_conv_id is not None else cid
        # ⚠️ 子任务形态必须带 `dry_run=True`（与 stream.py 里真实的孩子调用一致）：
        #   否则收尾会以 conversation_id=0 往 messages 插行 ⇒ FOREIGN KEY 约束失败，
        #   而异常发生在**收尾**，表现为"todo 事件全空"，很容易被误读成"归属没修好"。
        gen = pipe.execute_stream("读取该文件内容并核对", conv_arg, branch="dev",
                                  provider_id=1, forced_intent="chat",
                                  dry_run=(session_conv_id is not None), **kwargs)
        for ev in gen:
            if ev.get("type") == "tool" and ev.get("name") == "todo_write":
                todo_res.append(ev)
    except Exception as exc:                    # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        llm.llm_client.chat = orig_chat
        _st._KEEP_TOOL_ROUNDS = orig_keep

    # 子任务形态下 todo 行落在父会话 ⇒ 一并清理
    try:
        conn.execute("DELETE FROM agent_todos WHERE conversation_id IN (?,?)", (cid, session_conv_id or -1))
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
        conn.commit()
    except Exception:                            # noqa: BLE001
        pass
    conn.close()
    # B1 判据：本轮**新产生**的工具留痕里，有没有落到 conversation_id=0 的
    stray = 0
    try:
        _c2 = get_db()
        stray = _c2.execute(
            "SELECT COUNT(*) FROM tool_call_logs WHERE id>? AND conversation_id=0",
            (_max_log_id,)).fetchone()[0]
        _c2.close()
    except Exception:                            # noqa: BLE001
        stray = -1
    return {"cid": cid, "rounds": rounds, "err": err, "todo": todo_res, "stray": stray}


def _tool_content(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    try:
        return json.dumps(c, ensure_ascii=False)
    except Exception:                            # noqa: BLE001
        return str(c)


def main():
    print("=" * 78)
    print("工具结果跨轮连续性 / 子任务会话归属")
    print("=" * 78)
    if not os.path.exists((os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))):
        _rec("前置：mbse.db 存在", False, "无库 ⇒ 跳过（本门禁只在本机有意义）")
        print("\n结论：SKIP")
        return 0

    path = _mk_big_file()
    try:
        # ── A1/A2/A3：保留上一轮 + 可寻址省略 + 能重读 ──
        print("\n=== A1-A3 跨轮省略的可寻址性（保留 1 轮）===")
        got = _run(path, keep_rounds=1)
        rounds = got["rounds"]
        _rec("A0 链路跑通（LLM 轮次 ≥ 3）", len(rounds) >= 3,
             "轮次=%d err=%s" % (len(rounds), got["err"][:120]))
        if len(rounds) < 3:
            raise SystemExit(2)

        # ⚠️ 用"含 tool 消息的轮次"定位，不用绝对下标：循环前有 CHAT_TASK_PROBE
        #   等空轮，绝对下标会把探针轮当成"第 1 轮"（第一版就是这么错的）。
        tool_rounds = [i for i, r in enumerate(rounds) if _tool_msgs(r)]
        _rec("A0b 发生工具轮次 ≥ 3", len(tool_rounds) >= 3, "工具轮=%s" % tool_rounds)
        if len(tool_rounds) < 3:
            raise SystemExit(2)
        # 第 2 个工具轮次应该看到**上一轮的原文**
        r1_tools = _tool_msgs(rounds[tool_rounds[1]])
        # ⚠️ 判据必须同时看"有没有正文"和"有没有被省略标记"：
        #   省略后的 stub **也带 head**（前 400 字），而正文标记就在文件开头 ⇒
        #   只看 `MARK in 内容` 会在"已省略"时照样通过（第一版 A5 就是这样假红的）。
        #   真正的区别是：保留窗口内是**完整原文**，窗口外是**省略件**。
        ok1 = (bool(r1_tools)
               and any(MARK in _tool_content(m) for m in r1_tools)
               and not any("已省略" in _tool_content(m) for m in r1_tools))
        _rec("A1 第 2 轮仍能看到上一轮原文（正文标记在位）", ok1,
             "tool 消息 %d 条，首条 %d 字" % (len(r1_tools),
                                             len(_tool_content(r1_tools[0])) if r1_tools else 0))

        # 第 3 个工具轮次应该看到"省略但可寻址"
        r2_tools = _tool_msgs(rounds[tool_rounds[2]])
        stub = None
        for m in r2_tools:
            c = _tool_content(m)
            if "已省略" in c:
                stub = m
                break
        ok2 = False
        detail2 = "未找到省略后的工具体"
        oid = 0
        if stub is not None:
            try:
                d = json.loads(_tool_content(stub))
            except Exception:                    # noqa: BLE001
                d = {}
            oid = int(d.get("offload_id") or 0)
            head = str(d.get("head") or "")
            ok2 = bool(oid) and bool(head) and "tool_result_fetch" in _tool_content(stub)
            detail2 = "offload_id=%s head=%d字" % (oid or "缺失", len(head))
        _rec("A2 第 3 轮省略件带 offload_id + head + 重读指引", ok2, detail2)

        ok3 = False
        if oid:
            from agent.pipeline_parts import tool_offload as _to
            txt = _to.fetch_offload(oid)
            ok3 = MARK in (txt or "")
        _rec("A3 按 offload_id 能取回全文（正文标记在位）", ok3, "id=%s" % oid)

        # ── A4：子任务会话归属（todo_write 不再无上下文）──
        print("\n=== A4 子任务（conversation_id=0）的会话级工具归属 ===")
        got2 = _run(path, ask_todo=True, session_conv_id=12345)
        res_txt = ""
        if got2["todo"]:
            res_txt = str(got2["todo"][-1].get("result") or "")
        ok4 = "无会话上下文" not in res_txt and bool(res_txt) and not got2["todo"][-1].get("error")
        _rec("A4 session_conv_id 让 todo_write 拿到会话（不再报无上下文）", ok4,
             (res_txt[:120] or "无 todo 事件；err=%s" % got2["err"][:100]))
        # B1：会话级写入**全部**要归属父会话，不能只有 tool 日志对、技能反馈还落 0
        #     （本轮实测：技能反馈仍用 `int(conversation_id or 0)` ⇒ 2 行落在 conv=0）
        _rec("B1 子任务运行期间无任何留痕落到 conv=0", got2["stray"] == 0,
             "落到 conv=0 的行数=%s（-1=查询异常）" % got2["stray"])

        # ── A5：变异自证（KEEP=0 ⇒ 第 2 轮就已抹空）──
        print("\n=== A5 变异自证（KEEP_TOOL_ROUNDS=0 = 修复前逐轮抹空）===")
        got3 = _run(path, keep_rounds=0)
        _tr3 = [i for i, r in enumerate(got3["rounds"]) if _tool_msgs(r)]
        if len(_tr3) >= 2:
            r1b = _tool_msgs(got3["rounds"][_tr3[1]])
            erased = bool(r1b) and any("已省略" in _tool_content(m) for m in r1b)
            _rec("A5 KEEP=0 时第 2 轮就已省略（证明 A1 非空转）", erased,
                 "首条 %d 字" % (len(_tool_content(r1b[0])) if r1b else 0))
        else:
            _rec("A5 变异", False, "轮次不足，无法自证")
    finally:
        try:
            os.unlink(path)
        except Exception:                        # noqa: BLE001
            pass

    print("\n" + "=" * 78)
    fails = [r for r in _results if r[0] == FAIL]
    print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
