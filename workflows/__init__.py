"""Agent 可配置化底座：ToolRegistry 工具注册表 + ToolExecutor 真实执行器 + FlowExecutor DAG 执行引擎。

- ToolRegistry：内置工具处理器 + DB 中 tools/mcp_servers/skills 合并视图。
- ToolExecutor：真实工具执行器——内置工具映射到实际实现；MCP 工具走 JSON-RPC。
- FlowExecutor：DAG 执行引擎（真实执行 + 结构化 state + 条件分支 + 运行轨迹落库）。

解耦拆分（2026-08）：原单体 2538 行 → 包结构，对外导入契约不变：
- tools.py       ToolRegistry + ToolExecutor
- engine.py      FlowExecutor 主类（FlowPlannerMixin/FlowNodesMixin/FlowPersistenceMixin 组合）
- nodes.py       FlowNodesMixin（各类型节点执行器）
- planner.py     FlowPlannerMixin（编排规划）
- persistence.py FlowPersistenceMixin（轨迹/检查点/暂停/告警/黑板落库）
"""
from .tools import ToolRegistry, ToolExecutor
from .nodes import FlowNodesMixin
from .planner import FlowPlannerMixin
from .persistence import FlowPersistenceMixin
from .engine import FlowExecutor

# 全局实例（模块级复用）
tool_registry = ToolRegistry()
