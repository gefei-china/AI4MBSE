# -*- coding: utf-8 -*-
"""事件驱动底座（2026-11-01，Super-agent 落地 P0）。

对标 DeepSeek Super-agent 的三个领域概念，但**只取本工程需要的最小子集**：
  · `Trigger`  —— 不可变事件（稳定 ID ⇒ 幂等）
  · `Thread`   —— 同一业务对象下的事件时间序
  · `WorkItem` —— 被授权的工作单元（承载授权快照）

⚠️ **这里不引 DSH 代码，也不建第二套编排**。
Super-agent 原文的 5 个模块里，本工程已有 6 个等价实现（见评估报告 §2.1），
唯独缺"事件驱动"这一层 ⇒ 本包只补这一层，其余一律复用既有模块：
  · 意图路由 → `agent/intent.py`（不在本包重复实现 Slow Path）
  · 门控/授权 → `services/tool_hook_service.py` + `services/hil_policy.py`
  · 作业执行 → `core/job_queue.py`（不在本包造队列）
  · 看门狗 → `core/orch_supervisor.py` + `agent/loop_guard.py`

模块清单：
  · `model.py`    —— 纯函数判定内核（零 IO，可变异自证）
  · `sources.py`  —— 来源注册表（来源契约）
  · `repo.py`     —— 落库（thin data access）
  · `service.py`  —— 编排（receive → 准入 → 工单）
"""

from core.events.model import (  # noqa: F401
    Trigger, WorkItem, Admission, AdmissionVerdict,
    derive_trigger_id, derive_thread_key, derive_workitem_id,
    build_trigger, admit, make_workitem,
    decide_workitem_transition, is_terminal,
    decide_handoff, build_handoff_context,
    WORKITEM_TRANSITIONS, TERMINAL_WORKITEM_STATES,
    OBSERVE_MODE, EXECUTE_MODE, sanitize_text, stable_json,
)
from core.events.sources import (  # noqa: F401
    SourceSpec, SourceRegistry, registry, get_registry,
)

__all__ = [
    "Trigger", "WorkItem", "Admission", "AdmissionVerdict",
    "derive_trigger_id", "derive_thread_key", "derive_workitem_id",
    "build_trigger", "admit", "make_workitem",
    "decide_workitem_transition", "is_terminal",
    "decide_handoff", "build_handoff_context",
    "WORKITEM_TRANSITIONS", "TERMINAL_WORKITEM_STATES",
    "OBSERVE_MODE", "EXECUTE_MODE", "sanitize_text", "stable_json",
    "SourceSpec", "SourceRegistry", "registry", "get_registry",
]
