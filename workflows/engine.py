"""FlowExecutor：编排主链路入口（画布 DAG 执行引擎已移除）。

★ 2026-11-05：**画布工作流已明确舍弃**，本模块因此从「DAG 执行引擎」收缩为「编排入口壳」。
  已移除（均只服务画布流程定义 agent_flows）：
    · `run()`            —— 画布 DAG 拓扑执行（分层/并行/条件边/模板渲染）
    · `_layers` / `_run_parallel` / `_topo_sort` / `_render` / `_eval_cond`
    · `_exec_node` / `_exec_llm` / `_prequeue_hil`
    · `FlowPersistenceMixin`（flow_runs / flow_checkpoints / flow_run_steps / 黑板落库）
  保留（**主链路命脉**）：
    · `run_planner_plan()`（来自 FlowPlannerMixin）—— 会话编排唯一入口；
      它走 `_planner_core` → `_run_task` → `TaskQueue`（agent_tasks），
      **不经过**上述任何画布方法。
    · `FlowNodesMixin` 仅剩评审静态方法（`_evaluate_content` 等），供 `workflows/refine.py`
      的 RefineGate 复用 —— 这也是「画布/会话评审口径一致」得以保留的原因。
"""
from .tools import ToolRegistry
from .nodes import FlowNodesMixin
from .planner import FlowPlannerMixin


class FlowExecutor(FlowPlannerMixin, FlowNodesMixin):
    """编排执行器（主链路）。

    契约仅剩 `run_planner_plan(goal=..., agents=..., ...)`：给定目标与候选 Agent 池，
    由 planner 拆解子任务、经 TaskQueue 落 `agent_tasks`、逐个 `_run_task` 执行。
    """

    def __init__(self, conn=None):
        self.registry = ToolRegistry(conn)
        self.run_id = 0
