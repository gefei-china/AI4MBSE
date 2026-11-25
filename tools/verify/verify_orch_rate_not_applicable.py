# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：编排完成率判据口径：无子任务=不适用（不伪造 rate=0）+ 链标注错配可见。纯内存 9 项实测 0.4s；C 组回放 tmp 报告 ⇒ local
"""编排完成率判据的口径门禁（2026-08-18，G07 事故）。

## 事故

GoldenSet G07（链标注 `orchestrated`）在 live 基线里 `done=0/0 rate=0.000` 判失败。
查实：该例输入只要求**单一交付物**（一张 ACT 活动图），
`_needs_orchestration` 判为不需要编排（其 LLM 复杂度判定器 docstring 明写
"单一交付物任务（即使篇幅长）一律 false"）⇒ 合法走单 Agent 直行 ⇒
会话里没有任何 `agent_tasks`。原判据 `total=0 ⇒ rate=0.0` 把
**"本轮不适用编排"误报成"编排一条都没完成"**。

## 为什么这种情况必须允许

`_needs_orchestration` 对 design/requirement 意图含 **LLM 复杂度判定**
（多意图规则信号之外的一路），同一句输入两次可能给出不同结论。
所以数据集的 `chain` 标签描述的是**派生时源会话的行为**，不是"本次必须是编排"。
把它当硬约束会让基线出现无法消除的抖动。

## 断言

  A. 无子任务 ⇒ 判据不适用（True）且 evidence 明写 no_orchestration
  B. 有子任务 ⇒ 仍按 min 严格判（不能因为 A 而放过真失败）
  C. 真库/真报告：G07 这类"未编排"的用例不再产生 rate=0.000 的假证据

用法：``python tools/verify/verify_orch_rate_not_applicable.py``
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools", "eval"))

import golden_e2e as G  # noqa: E402

FAILS: list = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


DDL = """
CREATE TABLE agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER,
    task_key TEXT, status TEXT DEFAULT 'ready');
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, role TEXT,
    content TEXT);
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, kind TEXT, title TEXT);
"""


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript(DDL)
    return c


def case_behavior() -> None:
    print("\nA. 无子任务 ⇒ 不适用（不再伪造 rate=0）")
    c = _conn()
    ok, ev = G._orch_done_rate(c, 1, {"type": "orchestration_done_rate_min", "min": 0.8})
    check("A1 返回 True（不适用，不算失败）", ok is True, str(ok))
    check("A2 evidence 明写 no_orchestration（可追溯，不是静默通过）",
          "no_orchestration" in ev, ev)
    print("\nB. 有子任务 ⇒ 仍按 min 严格判")
    c.execute("INSERT INTO agent_tasks (conversation_id,task_key,status) VALUES (2,'t1','done')")
    c.execute("INSERT INTO agent_tasks (conversation_id,task_key,status) VALUES (2,'t2','failed')")
    ok2, ev2 = G._orch_done_rate(c, 2, {"type": "orchestration_done_rate_min", "min": 0.8})
    check("B1 1/2 done < min 0.8 ⇒ 仍判失败（不放水）", ok2 is False, ev2)
    check("B2 evidence 给出真实 done/total", "done=1/2" in ev2, ev2)
    c.execute("UPDATE agent_tasks SET status='done' WHERE conversation_id=2")
    ok3, ev3 = G._orch_done_rate(c, 2, {"type": "orchestration_done_rate_min", "min": 0.8})
    check("B3 2/2 达标 ⇒ 通过", ok3 is True, ev3)


def case_mismatch() -> None:
    print("\nD. 链标注错配必须可见（chain_mismatch）")
    c = _conn()
    case = {"id": "G99", "chain": "orchestrated", "intent": "design", "conv_id": 77,
            "checks": [{"type": "assistant_answer_nonempty"}]}
    c.execute("INSERT INTO messages (conversation_id,role,content) VALUES (88,'assistant','x')")
    row = G.score_case(c, case, 88)
    check("D1 未编排但标注 orchestrated ⇒ 打 chain_mismatch",
          "未触发编排" in str(row.get("chain_mismatch") or ""), str(row.get("chain_mismatch")))
    check("D2 不因此判红（不惩罚正确行为）", row.get("passed") is True, str(row.get("passed")))
    c.execute("INSERT INTO agent_tasks (conversation_id,task_key,status) VALUES (88,'t1','done')")
    row2 = G.score_case(c, case, 88)
    check("D3 真编排了 ⇒ 不打错配标记", not row2.get("chain_mismatch"), str(row2.get("chain_mismatch")))


def case_report() -> None:
    print("\nC. 真实报告回放：G07 不再有 rate=0.000 假证据")
    p = os.path.join(REPO, "tmp", "golden_live_report.json")
    if not os.path.exists(p):
        print("    SKIP（无 tmp/golden_live_report.json）")
        return
    d = json.load(io.open(p, encoding="utf-8"))
    for r in d.get("rows") or []:
        for chk in r.get("checks") or []:
            if chk.get("type") == "orchestration_done_rate_min" and "done=0/0" in str(
                    chk.get("evidence") or ""):
                check("C1 报告中不再出现 done=0/0 的假证据", False,
                      "%s: %s" % (r.get("case_id"), chk.get("evidence")))
                return
    check("C1 报告中不再出现 done=0/0 的假证据", True, "未发现")


def main() -> int:
    print("编排完成率口径门禁")
    print("=" * 60)
    case_behavior()
    case_mismatch()
    case_report()
    print("\n" + "=" * 60)
    if FAILS:
        print("结论：%d/%d 通过" % (N - len(FAILS), N))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
