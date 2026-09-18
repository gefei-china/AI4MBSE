# -*- coding: utf-8 -*-
"""调试：planner prompt 响应内容。"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from llm import llm_client
from workflows import FlowExecutor
from core import config as _cfg

goal = "先梳理宽带通信卫星需求，再完成系统设计，最后生成评审报告"
fe = FlowExecutor()
pool_txt = "requirement_analysis, design"
skill_pool_txt = ""
prompt = (
    "你是任务规划器。把目标分解为可并行/串行执行的子任务清单，只输出 JSON：\n"
    '{{"tasks": [{{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
    ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
    ' "context": "子任务必需上下文(精简事实，不复制用户全文，可空)",'
    ' "expected_output": "完成标准/期望输出格式(可空)",'
    ' "tools": ["允许的工具白名单(可空，空=继承该 Agent 默认绑定)"],'
    ' "skills": ["指定技能(可空)"]}}]}}\n'
    f"可用 Agent 池：{pool_txt}\n"
    "task_type 可选 agent（走 Agent 完整管线）或 react（多步思考-工具求解）或 llm（纯生成）。\n"
    f"{skill_pool_txt}"
    "要求：任务数 1~4 个；每个任务只交付一个明确成果；有依赖关系的用 deps 表达；"
    "context 只写该任务必需的事实与输入引用（上下文隔离，避免膨胀）；不要输出其他文字。\n"
    f"目标：{goal}"
)
resp = llm_client.chat([{"role": "user", "content": prompt}], _intent="planner")
print("meta:", (resp.get("_meta") or {}).get("provider"), (resp.get("_meta") or {}).get("used_mock"))
msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
print("msg keys:", list(msg.keys()))
raw = msg.get("content") or ""
print("raw[:600]:", raw[:600])
