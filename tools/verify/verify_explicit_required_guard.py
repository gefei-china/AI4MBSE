# -*- coding: utf-8 -*-
"""用户点名交付物不得被 early_stop 取消门禁（2026-11-18，G08 事故）。

真机证据：GoldenSet G08 输入明确要求「打分、评估规范性、给出优化建议」，
planner 把 t3 标成 config.required=false ⇒ early_stop 取消它
（error='目标已达成提前收敛（主动放弃非必需任务）'）⇒ 3/4 done、缺 report 产物。

修法：`protect_explicit_required(plan, goal)` 只加严 required（不降级、
不替 early_stop 决策），流式路径在 plan 解析后立即调用。

用法：``python tools/verify/verify_explicit_required_guard.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from services.replan_health import (  # noqa: E402
    early_stop_plan, protect_explicit_required, _EXPLICIT_DELIVERABLES,
)

FAILS: list = []
N = 0

GOAL_G08 = ("需求分析的实现路径：… 输出结构化的需求条目及需求来源追溯… "
            "大语言模型对需求进行打分、评估规范性，给出优化建议，由设计师决策")

PLAN = [
    {"key": "t1", "title": "解析需求文档并规格化", "expected_output": "需求规格",
     "status": "done", "config": {"required": True}},
    {"key": "t2", "title": "需求条目化与来源追溯", "expected_output": "编号对照表",
     "status": "done", "config": {"required": True}},
    {"key": "t3", "title": "对需求条目评分、评估规范性并给出优化建议",
     "expected_output": "评分与优化建议报告", "status": "ready",
     "config": {"required": False}},
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


def case_pure() -> None:
    print("\nA. 纯函数：只加严、不降级")
    plan = json.loads(json.dumps(PLAN))
    ch = protect_explicit_required(plan, GOAL_G08)
    check("A1 命中用户点名的评分/建议任务", "t3" in ch, str(ch))
    check("A2 t3 被置 required=True", plan[2]["config"]["required"] is True,
          str(plan[2]["config"]))
    check("A3 留痕 required_reason", "用户明确要求" in plan[2]["config"].get("required_reason", ""),
          str(plan[2]["config"]))
    check("A4 原本 required 的任务不被改动",
          plan[0]["config"] == PLAN[0]["config"] and plan[1]["config"] == PLAN[1]["config"],
          "被意外改写")
    plan2 = json.loads(json.dumps(PLAN))
    ch2 = protect_explicit_required(plan2, "生成活动视图的 SysML 代码")
    check("A5 目标无关时不误加严（early_stop 仍可工作）", not ch2, str(ch2))
    check("A6 交付物词表非空且含事故关键词",
          set(("报告", "建议", "评分", "打分")) <= set(_EXPLICIT_DELIVERABLES),
          str(_EXPLICIT_DELIVERABLES))


def case_early_stop() -> None:
    print("\nB. 与 early_stop 联动")
    before = early_stop_plan(json.loads(json.dumps(PLAN)), enabled=True)
    check("B1 修复前 early_stop 会取消 t3（复现事故）",
          before.get("action") == "early_stop" and "t3" in before.get("cancel_keys", []),
          str(before))
    plan = json.loads(json.dumps(PLAN))
    protect_explicit_required(plan, GOAL_G08)
    after = early_stop_plan(plan, enabled=True)
    check("B2 ★ 护栏后 t3 不再被取消", "t3" not in after.get("cancel_keys", []), str(after))
    check("B3 required 全完成前不收敛（保住 report 产物）",
          after.get("action") == "none" and "t3" in after.get("required_keys", []),
          str(after))


def case_code_db() -> None:
    print("\nC. 代码接线 + 真库根因证据")
    s = io.open(os.path.join(REPO, "agent", "pipeline_parts", "stream.py"),
                encoding="utf-8").read()
    check("C1 ★ 流式 plan 解析后立即调用护栏",
          "protect_explicit_required as _per" in s
          and "_per(plan, goal)" in s
          and s.find("_per(plan, goal)") > s.find('_intent="planner"'),
          "未接线或接线位置错误")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("C2 生产库存在", False, db)
        return
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute("SELECT config,error,status FROM agent_tasks "
                      "WHERE run_id=990254 AND task_key='t3'").fetchone()
        if not r:
            check("C2 事故任务 t3 存在", False, "未找到")
            return
        cfg = json.loads(r["config"] or "{}")
        print("    事故任务 t3：status=%s required=%s error=%s"
              % (r["status"], cfg.get("required"), str(r["error"])[:40]))
        check("C2 ★ 事故根因可复现：t3 曾被标 required=false 且被提前收敛取消",
              cfg.get("required") is False
              and "提前收敛" in str(r["error"] or ""), str(dict(r)))
    finally:
        c.close()


def main() -> int:
    print("用户点名交付物保护门禁")
    print("=" * 66)
    case_pure()
    case_early_stop()
    case_code_db()
    print("\n" + "=" * 66)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
