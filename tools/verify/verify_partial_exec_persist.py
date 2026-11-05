# -*- coding: utf-8 -*-
"""证明：失败/中断轮固化时，**执行过程（exec）随消息落库**（2026-10-30）。

## 为什么要这条 E2E（单元测不出来）
事故形态是**路径不对称**：成功轮把 `exec` 写进 `card_data`，固化路径写死 `"{}"`。
单元层只能证明"`_partial_exec_card()` 能返回一个 dict"，
证明不了"它真的被接到了那条 INSERT 上"——而接线才是当初漏掉的东西。
所以必须跑一条真链路：真 pipeline → 工具调用成功若干次 → 中途抛异常 →
读回**库里那条消息**，断言 `card_data.exec.tools` 有内容。

## 判据
P1 异常被捕获并落库（不抛到调用方，且确实新增了一条 assistant 消息）
P2 **card_data.exec 非空**（`tools` 数量 == 发生过的工具调用数）
P3 说明文案与事实一致：有 exec ⇒ 文案含「已随本条消息保存」；无 exec ⇒ 含「未能留存」
P4 幂等/不误伤：card_data 能正常 `json.loads`（不是字符串拼接的坏 JSON）

## 变异自证
把 `exec_card=` 参数去掉（等价于修复前）⇒ P2 必须判红。

## 用法（需本机真库，不进 CI）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_partial_exec_persist.py
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


def _fake_chat(calls, box):
    """触发点必须落在**工具跑完之后**。

    ⚠️ 第一版按"第 N 次调用"判定 ⇒ 实测炸在 `CHAT_TASK_PROBE`（ReAct 循环之前的探针），
      那一刻 `exec_tools` 还是空的 ⇒ P2 恒红。看起来像"修复没生效"，其实是**夹具打早了**。
       判据改成"**历史消息里已经出现了 tool 结果**"——它精确等价于
       "至少一个工具已经执行过"，与调用次序/探针个数都无关。
    """
    def chat(messages, provider_id=None, stream=False, tools=None, thinking=False, **kw):
        calls.append(len(calls))
        meta = {"provider": "fake", "model": "fake", "used_mock": False,
                "latency_ms": 1, "token_count": 20}
        _saw_tool_result = any(isinstance(m, dict) and m.get("role") == "tool"
                               for m in (messages or []))
        if tools and _saw_tool_result:
            # 工具已跑过 ⇒ 现在炸：模拟"工具都跑完了，收尾阶段崩"
            raise RuntimeError("verify_partial_exec_persist: 模拟中途失败")
        if tools:
            box["asked"] = box.get("asked", 0) + 1
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c%d" % box["asked"], "type": "function",
                                   "function": {"name": "sysml_stdlib_meta",
                                                "arguments": json.dumps(
                                                    {"package": "Pkg%d" % box["asked"]})}}]}
            return {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}
        msg = {"role": "assistant", "content": "完成"}
        return {"choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_meta": meta}
    return chat


def _run(patch_exec_card=True):
    """跑一条真链路。patch_exec_card=False 时把 exec_card 从调用点摘掉（变异）。"""
    import llm
    from database import get_db
    from agent.pipeline_parts.stream import StreamMixin as _SM

    calls, box, err = [], {}, ""
    orig_chat = llm.llm_client.chat
    orig_persist = _SM._persist_partial_stream
    conn = get_db()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO conversations (title,intent,status,user_id,phase,created_at,"
        "updated_at,project_id,current_intent,last_slots,pending_clarify) VALUES "
        "(:t,:i,'active',NULL,'requirement',:ts,:ts,'','view_expansion','{}','')",
        {"t": "verify_partial_exec", "i": "chat", "ts": ts})
    conn.commit()
    cid = cur.lastrowid

    if not patch_exec_card:
        # 变异：把 exec_card 丢掉（= 修复前的行为：card_data 恒 "{}"）
        def _no_card(self, conversation_id, content, reason="error", dry_run=False,
                     force=False, detail="", exec_card=None):
            return orig_persist(self, conversation_id, content, reason=reason,
                                dry_run=dry_run, force=force, detail=detail, exec_card=None)
        _SM._persist_partial_stream = _no_card

    try:
        from agent import AgentPipeline
        pipe = AgentPipeline()
        pipe._load_db_agents()
        pipe._should_confirm_intent = lambda *a, **k: False
        pipe._clarify_detect = lambda *a, **k: None
        llm.llm_client.chat = _fake_chat(calls, box)
        # ⚠️ 必须用 view_expansion：`sysml_stdlib_meta` 只在**产码意图**的工具白名单内。
        #   第一版用 chat ⇒ 工具调用被最小权限**拒掉**（ok=0「不在本次子任务授予的
        #   工具白名单内」）⇒ 走的是"跳过"分支，不进 exec_tools ⇒ P2 恒红。
        #   看起来像"落库没接上"，其实是夹具选了 chat（同 verify_failed_round_budget
        #   注释里记的那个坑：mock 选错工具会伪装成实现缺陷）。
        for _ev in pipe.execute_stream("请检索标准库并生成视图", cid, branch="dev",
                                       provider_id=1, forced_intent="view_expansion"):
            pass
    except Exception as exc:                # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    finally:
        llm.llm_client.chat = orig_chat
        _SM._persist_partial_stream = orig_persist

    row = conn.execute(
        "SELECT id, content, card_data FROM messages "
        "WHERE conversation_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
        (cid,)).fetchone()
    out = {"cid": cid, "err": err, "msg_id": row["id"] if row else 0,
           "content": (row["content"] if row else "") or "",
           "card_data": (row["card_data"] if row else "") or "",
           "tool_calls": len(box.get("asked", 0) and range(box["asked"]) or [])}
    # 清理（不留垃圾会话）
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
        conn.commit()
    except Exception:                        # noqa: BLE001
        pass
    conn.close()
    return out


def _parse(cd):
    try:
        d = json.loads(cd or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return None


def main():
    print("=" * 78)
    print("失败轮固化：执行过程（exec）必须随消息落库 —— 真链路")
    print("=" * 78)

    r = _run(patch_exec_card=True)
    cd = _parse(r["card_data"])
    ex = (cd or {}).get("exec") or {}
    tools = ex.get("tools") or []
    ok = _rec("P1 异常被捕获且落库（有 assistant 消息）", bool(r["msg_id"]),
              "err=%s msg=%s" % (r["err"][:80], r["msg_id"]))
    ok &= _rec("P4 card_data 是合法 JSON（不是坏拼接）", cd is not None,
               str(r["card_data"])[:120])
    ok &= _rec("P2 ★ exec.tools 非空（环节真的存下来了）",
               len(tools) >= 1,
               "tools=%d asked=%s" % (len(tools), r["tool_calls"]))
    ok &= _rec("P3 文案与事实一致（有 exec ⇒ 说『已随本条消息保存』）",
               "已随本条消息保存" in r["content"],
               r["content"][:160].replace("\n", " "))

    print("\n=== 变异 M：把 exec_card 摘掉（= 修复前 card_data 恒 '{}'）===")
    r2 = _run(patch_exec_card=False)
    cd2 = _parse(r2["card_data"])
    ex2 = (cd2 or {}).get("exec") or {}
    ok &= _rec("M 复现事故（无 exec + 文案不再声称已保存）⇒ 证明 P2/P3 非空转",
               not (ex2.get("tools")) and "未能留存" in r2["content"],
               "tools=%d" % len(ex2.get("tools") or []))

    bad = [x for x in _results if x[0] != PASS]
    print("\n" + "=" * 78)
    print("断言 %d  PASS %d  FAIL %d" % (len(_results), len(_results) - len(bad), len(bad)))
    for k, n, d in bad:
        print("  [%s] %s  %s" % (k, n, d))
    print("结论：%s" % ("全部通过" if not bad else "有断言未通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
