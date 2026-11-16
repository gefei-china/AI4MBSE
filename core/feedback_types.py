# -*- coding: utf-8 -*-
"""反馈类型单一真源（2026-10-29，跨前后端枚举治理）。

═══════════════════════════════════════════════════════════════════════
为什么要有这个模块
═══════════════════════════════════════════════════════════════════════
本仓曾把「反馈」这一个语义写在**四个互不相同的取值域**里（实测 2026-10-29）：

| 环节 | 位置 | 取值域 |
|---|---|---|
| 前端渲染/上报 | `static/js/mods/05-markdown.js` | `approve` / `reject` / 其他→'w' |
| 数据模型注释 | `database/schema.py` | `approve -> reject -> modify` |
| 坏例捕获 | `core/badcase.py::_SQL_FEEDBACK` | `modify` / `wrong` / `corrected` |
| 在线采样 | `core/online_eval.py::_FB_NEG_TYPES` | `modify` / `wrong` / `corrected` |

后果（生产库实证）：`feedback` 表 3 条全是 `approve`，**显式负反馈 0 条**；
`online_eval_queue` 0 行 ⇒ 采样器从未采到任何负反馈 ⇒ 进化闭环的"方向信号"整条断掉。

═══════════════════════════════════════════════════════════════════════
语义拆分（本模块的核心）
═══════════════════════════════════════════════════════════════════════
「反馈」其实是**两件不同的事**，此前被塞进同一个 `feedback_type` 字段：

1. **评审动作**（REVIEW）：人审核一份模型产物后做的裁决 ——
   `approve` 采纳 / `reject` 拒绝 / `modify` 要求修订。
   ⇒ 表达的是**流程状态**，不等于"模型答错了"。

2. **质量信号**（QUALITY）：人对**一条回答好不好**的直观评价 ——
   `up` 满意 / `down` 不满意。
   ⇒ 表达的是**质量优劣**，这才是驱动"越用越聪明"的方向信号。

⚠️ 为什么不能图省事把 `reject` 直接加进 badcase 的捕获列表：
   `reject` 是**评审拒绝**（"这份产物我不批"），若当成**用户差评**（"这条回答错了"），
   会把流程性的否决混进质量信号 ⇒ 进化系统会去"修"根本没坏的东西 ⇒ **misevolution**
   （进化成错）。这正是 `core/config_proposal.py` 模块头警告的那种风险。
   ⇒ 两个语义必须**分列存储、分路消费**。

═══════════════════════════════════════════════════════════════════════
消费口径（谁把什么当负反馈）
═══════════════════════════════════════════════════════════════════════
显式负反馈：
  · `modify` / `wrong` / `corrected` —— 用户给了修订意见或明确指出错误（高价值）
  · `down` —— 用户点踩（质量信号，severity=medium）
隐式负反馈（行为，无需用户主动点）：
  · `task_error` —— 任务失败（`ux_metrics`）
  · `interrupt` —— 用户主动打断（`ux_metrics`，通常意味着答得不对味）
  · `clarify_skipped` —— 用户跳过澄清（补充信号）

═══════════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════════
1. **后端任何地方都不得再写字面量**——一律 `from core.feedback_types import ...`。
2. **前端取值必须 ⊆ 后端白名单**：由 `GET /api/feedback-types` 下发，
   前端不硬编码；一致性由 `tests/test_feedback_type_consistency.py` 断言。
3. **未知取值一律拒绝入库**（fail-closed）：原先 `message_feedback` API 对
   `type` 零校验，脏值可落库 ⇒ 污染统计口径。
"""
from typing import Dict, Tuple

# ── 评审动作（对模型产物的流程裁决）──
REVIEW_TYPES: Tuple[str, ...] = ("approve", "reject", "modify")

# ── 用户质量信号（对一条回答优劣的直观评价）──
QUALITY_TYPES: Tuple[str, ...] = ("up", "down")

# ── 负反馈消费口径（badcase / online_eval **共用**，不造第二份词表）──
#: 显式负反馈·评审侧：用户给了修订意见 / 明确指出错误
NEGATIVE_REVIEW_TYPES: Tuple[str, ...] = ("modify", "wrong", "corrected")
#: 显式负反馈·质量侧：用户点踩
NEGATIVE_QUALITY: Tuple[str, ...] = ("down",)
#: 隐式负反馈·强信号（`ux_metrics.event`）：任务失败 / 用户主动打断
#:   —— 用户明确表达"这次没成"，是可靠的负反馈。
NEGATIVE_BEHAVIOR_EVENTS: Tuple[str, ...] = ("task_error", "interrupt")
#: 隐式负反馈·弱信号（`ux_metrics.event`）：用户跳过澄清
#:   —— 只是"回避"，不等于"答错了"，**不参与**在线采样（否则会稀释负反馈信噪比），
#:   仅供 badcase 诊断参考。
WEAK_BEHAVIOR_EVENTS: Tuple[str, ...] = ("clarify_skipped",)

#: 负反馈严重度分级（badcase 用）
#:   wrong / corrected 是明确的质量证词 ⇒ high
#:   modify 是修订意见 ⇒ medium
#:   down 是用户直观差评 ⇒ medium
NEGATIVE_SEVERITY: Dict[str, str] = {
    "wrong": "high",
    "corrected": "high",
    "modify": "medium",
    "down": "medium",
}


def is_negative_feedback(*, review_type: str = "", quality: str = "",
                         event: str = "") -> bool:
    """判定一条记录是否属于"负反馈"（任一通道命中即算）。

    三个通道彼此独立：评审侧修订、质量侧点踩、行为侧失败/打断。
    """
    if (review_type or "") in NEGATIVE_REVIEW_TYPES:
        return True
    if (quality or "") in NEGATIVE_QUALITY:
        return True
    if (event or "") in NEGATIVE_BEHAVIOR_EVENTS:
        return True
    return False


def severity_of(*, review_type: str = "", quality: str = "") -> str:
    """负反馈严重度（不在表内则回落 medium）。"""
    return NEGATIVE_SEVERITY.get((review_type or "").strip(),
                                 NEGATIVE_SEVERITY.get((quality or "").strip(), "medium"))


# ── 反馈处理状态机（2026-11-06 P0：把「将用于改进」变成可追踪状态）──
# 背景：前端点踩后的 toast 承诺「将用于改进」，但 feedback 表没有任何状态列，
#   用户查不到「我那条反馈到哪一步了」，引擎也无从标记「已消费」。
#   ⇒ 状态机是 P0「先诚实」的数据契约，同时是 P1 时间线的数据源。
FEEDBACK_STATES: Tuple[str, ...] = (
    "new",       # 已收到，尚未进入任何消费链路
    "sampled",   # 已进 badcase / online_eval 队列
    "proposed",  # 已产出改进候选（config_proposals）
    "promoted",  # 已入 HIL 待批准队列（尚未生效）
    "verified",  # 配置已生效且效果已判定
    "expired",   # 超过窗口仍未消费
)
FEEDBACK_STATE_LABELS: Dict[str, str] = {
    "new": "待采样",
    "sampled": "已采样",
    "proposed": "已生成改进候选",
    "promoted": "待你批准",
    "verified": "已生效并验证",
    "expired": "已过期未消费",
}
#: 只允许前进（与 alert_disposition 的 forward-only 同款纪律）；expired 是异常终态。
FEEDBACK_STATE_ORDER: Dict[str, int] = {
    s: i for i, s in enumerate(FEEDBACK_STATES[:-1])
}


def can_advance_state(current: str, target: str) -> bool:
    """状态是否允许前进；未知状态一律拒绝（fail-closed）。"""
    if target == "expired":
        return current not in ("promoted", "verified")
    a = FEEDBACK_STATE_ORDER.get(current or "new")
    b = FEEDBACK_STATE_ORDER.get(target)
    return a is not None and b is not None and b > a


def public_catalog() -> dict:
    """下发给前端的取值目录（`GET /api/feedback-types`）。

    前端据此渲染与校验，**不再硬编码**取值集合 —— 这是跨语言一致性的根。
    """
    return {
        "review_types": list(REVIEW_TYPES),
        "quality_types": list(QUALITY_TYPES),
        "negative_review_types": list(NEGATIVE_REVIEW_TYPES),
        "negative_quality": list(NEGATIVE_QUALITY),
        "negative_behavior_events": list(NEGATIVE_BEHAVIOR_EVENTS),
        "weak_behavior_events": list(WEAK_BEHAVIOR_EVENTS),
        "feedback_states": list(FEEDBACK_STATES),
        "feedback_state_labels": dict(FEEDBACK_STATE_LABELS),
    }
