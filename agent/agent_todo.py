# -*- coding: utf-8 -*-
"""持续自主执行：待办（todo）状态机 + 完成判定 + 障碍分类 —— **纯函数层，零 IO**。

为什么独立成文件：
  · 本模块是《持续自主执行能力-最终方案设计》的**正确性证明**，不接生产链路；
  · 与 `agent/auto_continue.py` 同构分工：该文件管「评估器说什么」，本文件管
    「**客观判据**是什么」——完成与否由可执行验收决定，不由模型自述决定；
  · 零 IO ⇒ 判据可单测、可证伪（D9：杜绝假阳性/假阴性）。

三块能力（与方案 P0 编号一一对应）：
  1. todo 状态机        —— `TodoItem` / `transition()` / `validate_transition()`
  2. 完成判定（验收驱动） —— `todo_gate()`：P0-1 的判定内核
  3. 障碍分类            —— `classify_obstacle()`：P0-5 的恢复矩阵内核

⚠️ 本文件**不写数据库、不调 LLM、不读配置**。落库/执行/配置读取属生产接线
   （见方案 §五 施工顺序），接线时本文件作为被调用的判定内核复用。

对齐依据（逐字核实，见方案 §三）：
  · Claude Code best practices —— "Give Claude something it can run that comes
    back pass or fail"（⇒ 完成 = 验收通过）
  · 同源 —— "Only mark as passing after careful testing"（⇒ 验收未过不得置完成）
  · 同源 —— "Maintain single active task"（⇒ 至多一个 in_progress）
  · MAST arXiv:2503.13657 —— FM-1.5 该停不停 12.4% > FM-3.1 过早终止 6.20%
    （⇒ 强制闸必须有次数上限，双向都要管）
"""

from __future__ import annotations

__all__ = [
    "TODO_STATUSES",
    "TERMINAL_STATUSES",
    "VERIFY_MANUAL",
    "TodoItem",
    "TransitionError",
    "transition",
    "validate_transition",
    "todo_gate",
    "classify_obstacle",
    "OBSTACLE_KINDS",
    "RECOVERY_ACTIONS",
]

#: 官方四态生命周期 + 本工程必需的 blocked。deleted 是本工程扩展（撤销误建的待办）。
TODO_STATUSES = ("pending", "in_progress", "completed", "blocked", "deleted")

#: 终态：进入后不再自动流转（completed 可因新证据被打回 pending，见 validate_transition）。
TERMINAL_STATUSES = ("completed", "deleted")

#: verify 字段的兜底值：允许写「人工确认」表示**不可自动验收**的项。
#: 依据方案 D-2：强制才有约束力，但留兜底避免不可验证任务永久卡死。
VERIFY_MANUAL = "人工确认"

#: 允许的状态迁移表。刻意**不含** completed → in_progress 的自动回退：
#: 打回必须由显式的"重开"动作触发（transition(..., to="pending")）。
_TRANSITIONS = {
    "pending": {"in_progress", "blocked", "deleted", "completed"},
    "in_progress": {"completed", "blocked", "pending", "deleted"},
    "blocked": {"in_progress", "pending", "deleted"},
    "completed": {"pending", "deleted"},
    "deleted": set(),
}


class TransitionError(ValueError):
    """非法状态迁移。携带 enough 信息供上层直接展示给用户。"""


class TodoItem:
    """一条待办。刻意用 **dataclass-like 轻结构**（不引 dataclass 依赖，保持零 IO 纯模块）。

    :param subject: 待办标题（具体到对象，如"校验包 P-001 的 SysML v2 语法"）
    :param status:   TODO_STATUSES 之一
    :param verify:   验收方式（可执行描述）；completed 时**必须**非空
    :param verify_result: 验收回执（completed 时**必须**非空且表示通过）
    :param blocked_by: 依赖说明
    :param note:     备注
    """

    __slots__ = ("subject", "status", "verify", "verify_result", "blocked_by", "note")

    def __init__(self, subject, status="pending", verify="", verify_result="",
                 blocked_by="", note=""):
        self.subject = subject
        self.status = status
        self.verify = verify
        self.verify_result = verify_result
        self.blocked_by = blocked_by
        self.note = note

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "status": self.status,
            "verify": self.verify,
            "verify_result": self.verify_result,
            "blocked_by": self.blocked_by,
            "note": self.note,
        }

    def __repr__(self):  # pragma: no cover - 调试用
        return f"<TodoItem {self.status}: {self.subject}>"


def validate_transition(item: TodoItem, to_status: str) -> str:
    """校验一次状态迁移是否合法，**不修改** item。

    :returns: 空字符串 = 合法；非空 = 人类可读的拒绝理由。
    :raises TransitionError: 仅当 to_status 根本不在 TODO_STATUSES 时抛出
            （未知状态是**调用方 bug**，不是业务拒绝，必须响）。
    """
    if to_status not in TODO_STATUSES:
        # 未知状态 ⇒ 静默忽略会变成"我写错了但系统没反应"，必须响。
        raise TransitionError(f"未知待办状态：{to_status!r}（合法：{', '.join(TODO_STATUSES)}）")
    if item.status == to_status:
        return ""                                   # 幂等：同态迁移不算错
    if to_status not in _TRANSITIONS.get(item.status, set()):
        return f"不允许的状态迁移：{item.status} → {to_status}"

    # ★ 核心约束：置 completed 必须「有验收方式 + 验收已通过」。
    #   依据官方 "Only mark as passing after careful testing"。
    if to_status == "completed":
        if not (item.verify or "").strip():
            return "置为 completed 前必须填 verify（验收方式）；不可自动验收的写「人工确认」"
        if not (item.verify_result or "").strip():
            return "置为 completed 前必须填 verify_result（验收回执）"
    # blocked 必须说明卡在哪，否则上层无法给出可执行的恢复动作。
    if to_status == "blocked" and not (item.blocked_by or item.note or "").strip():
        return "置为 blocked 必须说明阻塞原因（blocked_by 或 note）"
    return ""


def transition(item: TodoItem, to_status: str, *, verify_result=None,
               blocked_by=None, note=None) -> TodoItem:
    """执行一次状态迁移（校验通过才改）。

    :param verify_result: 传入则覆盖 item.verify_result（用于登记验收回执）
    :param blocked_by:    传入则覆盖
    :param note:          传入则覆盖
    :returns: 同一个 item（就地修改，便于链式）
    :raises TransitionError: 非法迁移 / 违反 completed 前置条件
    """
    if verify_result is not None:
        item.verify_result = verify_result
    if blocked_by is not None:
        item.blocked_by = blocked_by
    if note is not None:
        item.note = note
    reason = validate_transition(item, to_status)
    if reason:
        raise TransitionError(reason)
    item.status = to_status
    return item


def todo_gate(items, *, has_todos=None) -> dict:
    """**验收驱动的完成判定**（方案 P0-1 的判定内核）。

    判定优先级（严格按此顺序，官方依据见模块 docstring）：
      ① 有 todo 且**全部** completed（每项 verify_result 非空）⇒ done=True
      ② 有 todo 且存在未完成 ⇒ done=False，next_step = **第一个** pending 项
      ③ 无 todo（简单任务）⇒ 退回旧路径（由 LLM 评估器判），本函数只标记 fallback

    :param items:    TodoItem 序列（可传 dict，会被转成 TodoItem）
    :param has_todos: 显式声明本次会话**是否存在 todo**。None = 由 items 非空推断。
                      用途：**空清单**与"没有 todo"必须区分 —— 前者是有 todo
                      但全被删了（⇒ 未完成），后者是没有 todo（⇒ 走旧路径）。
    :returns: ``{"done": bool, "next_step": str, "remaining": [str],
                 "fallback": bool, "reason": str}``
    """
    norm = []
    for it in (items or []):
        norm.append(it if isinstance(it, TodoItem) else TodoItem(**{
            k: v for k, v in dict(it).items()
            if k in TodoItem.__slots__
        }))

    if has_todos is None:
        has_todos = bool(norm)

    # ③ 无 todo ⇒ 退回旧路径（逐字节兼容现有 98.4% 只读问答）
    if not has_todos:
        return {"done": False, "next_step": "", "remaining": [],
                "fallback": True, "reason": "无 todo ⇒ 退回 LLM 评估器（旧路径）"}

    live = [t for t in norm if t.status != "deleted"]

    # ① 全部 completed 且验收回执齐全 ⇒ 完成
    if live and all(t.status == "completed" and (t.verify_result or "").strip()
                    for t in live):
        return {"done": True, "next_step": "", "remaining": [],
                "fallback": False, "reason": f"全部 {len(live)} 项已通过验收"}

    # ② 存在未完成 ⇒ 未完成，next_step 指向第一个 pending
    pending = [t for t in live if t.status == "pending"]
    first = pending[0] if pending else (live[0] if live else None)
    remaining = [t.subject for t in live if t.status != "completed"]
    if first is not None:
        ns = (f"完成待办「{first.subject}」"
              + (f"（验收方式：{first.verify}）" if (first.verify or "").strip() else ""))
    else:
        ns = "清理未完成待办"
    return {"done": False, "next_step": ns, "remaining": remaining,
            "fallback": False, "reason": f"{len(remaining)}/{len(live)} 项未完成"}


#: 障碍类型 → 恢复动作。键与方案 §4.4 矩阵一一对应。
OBSTACLE_KINDS = ("transient", "auth", "not_found", "precondition",
                  "capability", "user_only")

#: 各类障碍的恢复动作。**只有 user_only 允许问人** —— 其余四类现有工具都能自解决。
RECOVERY_ACTIONS = {
    "transient":    "retry",          # 重试（已有）
    "auth":         "refresh_retry",  # 刷新凭据 → 重试一次
    "not_found":    "repath",         # 换路径：list/search 先定位
    "precondition": "refetch_replay", # 重新取坐标 → 重放
    "capability":   "downgrade",      # 降级：换等价工具或缩小范围
    "user_only":    "ask_user",       # 唯一合法停下
}

#: 错误文本特征 → 障碍类型。**顺序敏感**：越靠前越具体，先匹配。
_OBSTACLE_PATTERNS = (
    ("auth", (
        "401", "403", "unauthorized", "forbidden", "stale_read",
        "凭据", "鉴权", "认证失败", "权限不足", "token 过期",
        "access denied", "permission denied",
    )),
    ("not_found", (
        "404", "not found", "不存在", "找不到", "无此", "已删除",
        "invalid id", "无效 id",
    )),
    ("precondition", (
        "version mismatch", "stale", "版本过期", "父容器已变", "已被修改",
        "conflict", "412",
    )),
    ("capability", (
        "not supported", "unsupported", "不支持", "无此能力", "cannot ",
        "unknown tool",
    )),
    ("transient", (
        "timeout", "timed out", "502", "503", "504", "connection",
        "网络", "超时", "临时", "temporarily", "rate limit", "429",
    )),
)


def classify_obstacle(error_text: str) -> dict:
    """把一次失败分类成障碍类型 + 恢复动作（方案 P0-5 的内核）。

    :param error_text: 错误文本（异常消息 / 工具返回的失败原因）
    :returns: ``{"kind": str, "action": str, "ask_user_allowed": bool,
                 "matched": str}``；kind ∈ OBSTACLE_KINDS
    """
    t = str(error_text or "").lower()
    if not t.strip():
        return {"kind": "transient", "action": "retry",
                "ask_user_allowed": False, "matched": ""}
    for kind, pats in _OBSTACLE_PATTERNS:
        for p in pats:
            if p.lower() in t:
                return {"kind": kind, "action": RECOVERY_ACTIONS[kind],
                        "ask_user_allowed": (kind == "user_only"),
                        "matched": p}
    # 未命中任何特征 ⇒ 保守当 transient（可重试），**不**当 user_only
    #   —— 误判成 user_only 会造成"可自解决却问人"，正是要消灭的失败模式。
    return {"kind": "transient", "action": "retry",
            "ask_user_allowed": False, "matched": ""}
