# -*- coding: utf-8 -*-
"""编排子任务超时的协作式收敛判据（2026-11-27）。

为什么不直接杀线程：Python 无法安全终止正在持有 LLM 流 / DB 事务的线程。
正确做法是落一个"取消"意图，跑在后台线程里的 `_run_subtask` 在每次产出后
经 `TaskQueue.is_cancel_requested` 看到并自行 break —— 正在飞行的那一次调用会跑完，
但后续步骤全部跳过。

本模块把两个动作收成纯函数，便于判据级验证（真库临时实例，零 LLM）：
  · request_timeout_cancel(task_id, reason) —— 用**独立连接**写取消意图
  · was_timeout_cancelled(conn, task_id)    —— 主循环据此避免被 fail() 覆写状态
"""
from services.task_queue import TaskQueue


def request_timeout_cancel(task_id: int, reason: str = "") -> bool:
    """超时时落取消意图；任何异常都当"未取消"（fail-safe：不误改任务状态）。"""
    try:
        from database import get_db
        c = get_db()
        try:
            return bool(TaskQueue.cancel(c, int(task_id), reason=reason or "子任务超时"))
        finally:
            c.close()
    except Exception:
        return False


def was_timeout_cancelled(conn, task_id: int) -> bool:
    """主循环失败分支用：已取消的任务不能再被 fail() 覆写成 failed。"""
    try:
        return bool(TaskQueue.is_cancel_requested(conn, int(task_id)))
    except Exception:
        return False
