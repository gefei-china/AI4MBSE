# -*- coding: utf-8 -*-
"""排查 run_planner_plan 异常。"""
import sys, os, traceback
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from workflows import FlowExecutor

goal = "先梳理宽带通信卫星需求，再完成系统设计，最后生成评审报告"
try:
    r = FlowExecutor().run_planner_plan(goal=goal, agents=["requirement_analysis", "design"],
                                        max_tasks=4, parallel=True, provider_id=None)
    print("OK keys:", list(r.keys()))
    data = r.get("data") or {}
    print("degraded =", r.get("degraded"))
    print("reflection =", data.get("reflection"))
    print("contentLen =", len(r.get("content") or ""))
except Exception:
    traceback.print_exc()
