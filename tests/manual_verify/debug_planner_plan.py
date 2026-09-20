# -*- coding: utf-8 -*-
"""调试：monkeypatch llm_client.chat 打印 _planner_core 实际计划响应。"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import llm
from llm import llm_client as _lc

_orig = _lc.chat
def _patched(messages, **kw):
    r = _orig(messages, **kw)
    if kw.get("_intent") == "planner":
        msg = (r.get("choices") or [{}])[0].get("message", {}) or {}
        print(">>> planner raw:", (msg.get("content") or "")[:200])
    return r
_lc.chat = _patched

from workflows import FlowExecutor
from agent.pipeline import AgentPipeline

goal = "先梳理宽带通信卫星需求，再完成系统设计，最后生成评审报告"
r = FlowExecutor().run_planner_plan(goal=goal, agents=["requirement_analysis", "design"],
                                    max_tasks=4, parallel=True, provider_id=None)
print("degraded =", r.get("degraded"))
data = r.get("data") or {}
print("reflection =", data.get("reflection"))
print("contentLen =", len(r.get("content") or ""))
_lc.chat = _orig
