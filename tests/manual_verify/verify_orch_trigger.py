# -*- coding: utf-8 -*-
"""快速验证：编排触发 + T4 评审闭环（非流式 run_planner_plan）。"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import AgentPipeline

p = AgentPipeline()
p._load_db_agents()
goal = "先梳理宽带通信卫星需求，再完成系统设计，最后生成评审报告"
# 用 pipeline 自身的意图路由（与 execute 一致）
from agent.intent import IntentRouter
from database import get_db
conn = get_db()
ir = IntentRouter()
# 复制 pipeline 注册的关键词/语义索引
try:
    for _i, _meta in getattr(p.registry, "_db_meta", {}).items():
        ir.register_keywords(_i, (_meta.get("intent_keywords") or []))
except Exception:
    pass
intent = ir.detect(goal, conn=conn)
conn.close()
print("intent =", intent)

for it in ("requirement_analysis", "design", intent):
    if not it:
        continue
    need = p._needs_orchestration(goal, it)
    print(f"  needs_orchestration({it}) =", need)
    if need:
        orch = p._try_orchestrate(goal, it, provider_id=None)
        print("  orch keys:", list((orch or {}).keys()))
        if orch:
            data = orch.get("data") or {}
            print("  degraded =", orch.get("degraded"))
            print("  reflection =", data.get("reflection"))
            print("  contentLen =", len(orch.get("content") or ""))
        break
