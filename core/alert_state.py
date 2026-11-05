# -*- coding: utf-8 -*-
"""告警状态机单一真源（2026-10-29）。

═══════════════════════════════════════════════════════════════════════
为什么要有这个模块
═══════════════════════════════════════════════════════════════════════
生产库实测（2026-10-29）：`alert_events` 328 条，**status 全部是 'open'**，
一条都没有被处理过；同三条规则各累积 81 次（同因告警刷屏 = 告警疲劳）。

根因有二：
  ① **没有折叠** —— 每次命中都 INSERT 新行，看板被同因告警淹没；
  ② **只有 ack、没有后续流转** —— ack 之后没有 triaged/fixed/verified，
     于是"这条到底修了没"无记录 ⇒ 告警只是"响"，不是"待办"。

本模块把**状态取值域与合法流转**收到一处（此前 ack 只在
`routers/monitor.py::ack_alert` 与 `core/alert_evaluator.py` 各写一次字面量，
必然漂移）。

═══════════════════════════════════════════════════════════════════════
状态语义
═══════════════════════════════════════════════════════════════════════
    open      —— 刚触发，未阅（webhook 通知成功会自动置 acked）
    acked     —— 已阅/已通知，但尚未归因
    triaged   —— 已归因：知道为什么触发（note 里写结论）
    fixed     —— 已提交修复
    verified  —— 已验证修复生效（指标回落）—— **闭环终点**

「已终结」= fixed / verified。终结的告警不再参与折叠累积
（问题已解决，下次再触发应当是**新事件**，而不是把旧事件计数加一）。

⚠️ 纪律：状态**只能单步前进**（可跳过 triaged，但不可回退）。
   回退会让"已验证"的告警重新变 open，看板噪声回潮，且掩盖真实闭环率。
"""
from typing import Dict, Tuple

#: 全部状态（顺序即推进顺序）
ALERT_STATUSES: Tuple[str, ...] = ("open", "acked", "triaged", "fixed", "verified")

#: 已终结状态（不再参与折叠；下次触发算新事件）
TERMINAL_STATUSES: Tuple[str, ...] = ("fixed", "verified")

#: 未终结状态（折叠时命中的就是这些）
OPEN_STATUSES: Tuple[str, ...] = tuple(s for s in ALERT_STATUSES if s not in TERMINAL_STATUSES)

#: 合法流转：from → 允许到的下一批
#:   允许"跳过中间态"（open 直接到 fixed 也合法：简单问题不必走归因），
#:   但**禁止回退**（任何在序中靠前的状态都不可从靠后的状态进入）。
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "open": ("acked", "triaged", "fixed"),
    "acked": ("triaged", "fixed"),
    "triaged": ("fixed", "verified"),
    "fixed": ("verified",),
    "verified": (),
}


def is_valid_status(status: str) -> bool:
    return (status or "").strip() in ALERT_STATUSES


def is_terminal(status: str) -> bool:
    return (status or "").strip() in TERMINAL_STATUSES


def can_transition(cur: str, nxt: str) -> bool:
    """能否从 cur 推进到 nxt。

    · 目标非法 → False
    · 当前非法 → False（不猜，交由调用方报错）
    · 回退（nxt 在 cur 之前）→ False
    · 已在目标态 → False（幂等重复提交不算非法推进，但也不算推进）
    """
    cur = (cur or "").strip()
    nxt = (nxt or "").strip()
    if cur not in ALERT_STATUSES or nxt not in ALERT_STATUSES:
        return False
    if cur == nxt:
        return False
    if cur not in ALLOWED_TRANSITIONS:
        return False
    if nxt in ALLOWED_TRANSITIONS[cur]:
        return True
    # 允许跳过中间态：只要 nxt 在 cur 之后、且不是回退即可
    if ALERT_STATUSES.index(nxt) > ALERT_STATUSES.index(cur):
        return True
    return False


def open_statuses_sql() -> str:
    """未终结状态的 SQL IN 片段（供折叠查询用，避免各处手写列表）。"""
    return ",".join("'%s'" % s for s in OPEN_STATUSES)
