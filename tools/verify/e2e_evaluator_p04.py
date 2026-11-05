# -*- coding: utf-8 -*-
"""真机验证：P0-4 评估器硬信号（4 条**真实**历史样本 + 真实 LLM）。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.3 / §6 第 4 步。

为什么必须用真实样本：方案里最重要的一条实证就是「只改 prompt 只修好 1/2」——
  这个结论来自这 4 条真实样本。改了 prompt 之后必须**用同一批样本重跑**，
  否则等于换了一把尺子量同一个东西。

样本（真实库 messages 表）：
  · 3985 —— 动作降级成建议（「建议下一步执行校验」）→ 期望由 **P0-1 拦**（prompt 修不好）
  · 2731 —— 推给别的 Agent（「请转交对应 Agent」）  → 期望 P0-4 **修好**（done=False）
  · 3970 —— 伪阻断二选一                             → 期望保持 done=False（不退化）
  · 3173 —— 真该问（对照）                           → 期望保持 done=False（不退化）

⚠️ 本脚本**不改生产文件**，只用 `agent.auto_continue.EVAL_SYS` 的当前值
   （P0-4 已把它改好）去跑真实 LLM。

用法：
  python tools/verify/e2e_evaluator_p04.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


SAMPLE_IDS = [3985, 2731, 3970, 3173]


def _load_samples():
    """从真实库取出 (样本 id, 该轮的 user 请求, 该轮 assistant 答复)。"""
    from core.config import DB_PATH
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    out = []
    for mid in SAMPLE_IDS:
        r = c.execute(
            "SELECT id, conversation_id, role, content FROM messages WHERE id=?",
            (mid,)).fetchone()
        if not r:
            out.append({"id": mid, "ok": False, "reason": "样本不存在"})
            continue
        conv = r["conversation_id"]
        # 该 assistant 消息之前最近的一条 user 消息 = 本轮原始请求
        u = c.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND role='user' "
            "AND id<? ORDER BY id DESC LIMIT 1", (conv, mid)).fetchone()
        out.append({"id": mid, "ok": True, "conv": conv,
                    "user_input": (u["content"] if u else "")[:1500],
                    "answer": r["content"] or ""})
    c.close()
    return out


def run():
    print("=" * 74)
    print("真机验证：P0-4 评估器硬信号（4 条真实样本 + 真实 LLM）")
    print("=" * 74)

    from agent import auto_continue as _ac
    from llm import llm_client
    from agent.pipeline_parts.common import CHAT_TASK_EVAL

    # ── 0. 硬信号确实进了 prompt ──
    hard = ["建议下一步", "转交", "自行查明", "尚未校验"]
    check("0", "EVAL_SYS 含四条硬信号",
          all(k in _ac.EVAL_SYS for k in hard),
          f"缺失：{[k for k in hard if k not in _ac.EVAL_SYS]}")
    check("0", "「可选增强」已收窄（仅措辞/排版/举例）",
          "仅限" in _ac.EVAL_SYS and "措辞" in _ac.EVAL_SYS)
    check("0", "明确「需调用工具的不算可选增强」",
          "任何需要调用工具才能完成的事都不算可选增强" in _ac.EVAL_SYS)

    samples = _load_samples()
    got = {}
    for s in samples:
        if not s["ok"]:
            check("1", f"样本 #{s['id']} 可读取", False, s.get("reason", ""))
            continue
        check("1", f"样本 #{s['id']} 可读取",
              bool(s["answer"]) and bool(s["user_input"]),
              f"answer={len(s['answer'])} user={len(s['user_input'])}")

        msgs = _ac.build_eval_messages(s["user_input"], "（本轮真实工具史未取）",
                                       s["answer"], "通用问答")
        try:
            resp = llm_client.chat(msgs, provider_id=None, stream=False,
                                   _intent=CHAT_TASK_EVAL)
            text = (((resp or {}).get("choices") or [{}])[0]
                    .get("message", {}).get("content") or "")
        except Exception as e:
            check("1", f"样本 #{s['id']} 真实 LLM 评估", False, repr(e))
            continue
        v = _ac.parse_verdict(text)
        got[s["id"]] = v
        print(f"  （info）#{s['id']} 评估器结论 = "
              f"{json.dumps(v, ensure_ascii=False)[:150] if v else 'None（解析失败）'}")
        check("1", f"样本 #{s['id']} 返回可解析结论", v is not None, str(text)[:120])

    # ── 2. ★ 2731：转交他人 ⇒ 必须 done=False（P0-4 的核心目标）──
    v = got.get(2731)
    check("2", "★#2731（转交他人）⇒ done=False", v is not None and not v.get("done"),
          json.dumps(v, ensure_ascii=False) if v else "None")

    # ── 3. 不退化：3970 / 3173 仍 done=False ──
    for mid in (3970, 3173):
        v = got.get(mid)
        check("3", f"# {mid} 未退化（仍 done=False）",
              v is not None and not v.get("done"),
              json.dumps(v, ensure_ascii=False) if v else "None")

    # ── 4. ★ 如实登记：#3985 由 prompt 修不好（方案已实证 1/2）──
    #    这里**不断言它必须修好**——那会与已知实证矛盾。
    #    而是记录它的实际结果，并断言「P0-1 能拦住它」（下面第 5 组）。
    v = got.get(3985)
    done_3985 = bool(v.get("done")) if v else None
    print(f"  （info）#3985 评估器结论 done={done_3985} "
          f"（预期可能仍为 True —— 方案已实证 prompt 修不好这一类）")

    # ── 5. ★ 3985 类的兜底：P0-1 待办闸必须能拦住 ──
    #    构造「建议下一步执行校验」对应的待办状态（校验未完成）
    from agent import agent_todo as _at
    items = [
        _at.TodoItem("给出建议方案", status="completed", verify="人工确认",
                     verify_result="PASS"),
        _at.TodoItem("执行校验", status="pending", verify="sysmlv2_check"),
    ]
    g = _at.todo_gate(items, has_todos=True)
    check("5", "★★#3985 类由 P0-1 待办闸拦住（done=False）",
          g["done"] is False and g["fallback"] is False,
          json.dumps(g, ensure_ascii=False)[:180])

    # ── 6. 汇总纠正率（如实登记，不美化）──
    fixed = sum(1 for mid in (2731,) if got.get(mid) and not got[mid].get("done"))
    total_targets = 1
    print(f"\n  （info）P0-4 目标样本纠正：{fixed}/{total_targets}"
          f"（2731 转交他人）")
    print("  （info）3985 类（建议当完成）**不承诺由 prompt 修复** —— "
          "方案已实证仅 1/2，由 P0-1/P0-2 兜住")


def main():
    run()
    print("\n" + "-" * 74)
    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if not r[2])
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
