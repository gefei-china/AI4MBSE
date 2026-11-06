"""Agent 可配置化底座：工具注册表/执行器 + 编排规划器（画布 DAG 引擎已移除）。

- ToolRegistry：内置工具处理器 + DB 中 tools/mcp_servers/skills 合并视图。
- ToolExecutor：真实工具执行器——内置工具映射到实际实现；MCP 工具走 JSON-RPC。
- FlowExecutor：**编排入口**（`run_planner_plan`），不再是 DAG 执行引擎。

解耦拆分（2026-08）：原单体 2538 行 → 包结构，对外导入契约不变：
- tools.py       ToolRegistry + ToolExecutor
- engine.py      FlowExecutor 主类（FlowPlannerMixin + FlowNodesMixin 组合）
- nodes.py       FlowNodesMixin（**仅剩评审静态方法**，供 RefineGate 复用）
- planner.py     FlowPlannerMixin（编排规划 + 子任务执行）

★ 2026-11-05：**画布工作流已明确舍弃**，`persistence.py`（flow_runs / flow_checkpoints /
  flow_run_steps / 黑板落库）与 `FlowExecutor.run()`（DAG 拓扑执行）已删除。
  主链路编排（会话内的子任务拆解执行）**不依赖**它们：
  走 `run_planner_plan` → `_planner_core` → `_run_task` → `TaskQueue`（agent_tasks）。
"""
from .tools import ToolRegistry, ToolExecutor
from .nodes import FlowNodesMixin
from .planner import FlowPlannerMixin
from .engine import FlowExecutor

# 全局实例（模块级复用）
tool_registry = ToolRegistry()
