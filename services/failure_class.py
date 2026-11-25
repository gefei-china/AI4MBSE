# -*- coding: utf-8 -*-
"""子任务失败分类（★ 2026-10-13，B1-6）。

**为什么需要**（对标取证）：`agent/pipeline_parts/stream.py::_worker` 的重试判据原本
只有 `if retry_left <= 0` —— **不看失败原因**。后果是**权限拒绝、参数非法、输出不合格
这类"重试也不会变"的永久失败，也会白白重试一次**，而每个子任务实测 344~1155 秒、
还要烧一次完整的子 Agent LLM 调用。

**分类维度**（对齐文章《企业级多 Agent 架构》§RESILIENCE 的四类失败 + 本仓实际形态）：

| 类 | 含义 | 可重试 |
|---|---|---|
| `timeout` | 超时/停滞/硬上限 | ✅ |
| `transient` | 瞬态外部故障（连接、5xx、429） | ✅ |
| `permission` | 权限/授权被拒 | ❌ 永久 |
| `input_invalid` | 输入或参数不合法 | ❌ 永久 |
| `output_rejected` | 模型输出不合格（协议校验失败） | ❌ 永久（重试同样 prompt 大概率同样输出） |
| `business` | 业务规则拒绝（如"无数据可算"） | ❌ 永久 |
| `needs_user` | 人在回路中断（ask_user / 写前确认），等用户回答 | ❌ 永久（2026-11-20 新增，见下） |
| `internal` | 未归类的内部错误 | ✅ 默认当瞬态（保守：宁可多重试一次，也不把瞬态错判成永久） |

⚠️ **`internal` 归为可重试是刻意的**：分类器的职责是"避免确定的永久失败被无谓重试"，
而不是"精准预测"。把不确定的归到可重试侧，最坏后果是多花一次调用；
归到不可重试侧，最坏后果是**瞬态故障被永久放弃**——后者严重得多。

**复用而非重造**：瞬态关键词表直接引用 `tool_resilience.RETRYABLE_KEYWORDS` 与
`is_retryable_error()`（该模块是本仓既有事实源，工具层重试已在用）。
**不另写一份关键词表** —— 两份词表必然漂移，而漂移的后果是"工具层判可重试、编排层判不可重试"。

**真库口径（2026-10-12 只读，`agent_tasks` 54 条 failed 的 error 全量聚类）**：
    46 × '编排中断：超过 30 分钟无任何进展，判定为孤儿运行（进程重启或执行线程丢失）…'
     7 × 'timeout'
     1 × 'timeout（240s 无产出）'
⇒ 现有历史错误**全部落在 `timeout` / `interrupted`** 两类，判据对存量 100% 覆盖。
`interrupted`（孤儿运行）单列一类：它是"进程重启导致的失活"，**与 timeout 不同源**
（一个是逻辑停滞、一个是宿主挂了），单独归类便于运维区分。

**运行**：`python -c "from services.failure_class import classify; print(classify('timeout'))"`
"""
import re
from core.domain.failure_class import _BUSINESS_KW, _INPUT_KW, _INTERRUPTED_KW, _NEEDS_USER_KW, _OUTPUT_KW, _PERMISSION_KW, _TIMEOUT_KW, classify  # noqa: F401  (P1 收敛：下沉域层，旧路径不再是第二份实现)

# ── 复用既有瞬态词表（不重造第二份）────────────────────────────────────────
try:
    from core.tool_resilience import RETRYABLE_KEYWORDS, is_retryable_error
except Exception:      # pragma: no cover - tool_resilience 不可用时的兜底
    RETRYABLE_KEYWORDS = (
        "timeout", "timed out", "connection", "network", "getaddrinfo",
        "refused", "reset", "unreachable", "bad gateway", "service unavailable",
        "gateway timeout", "too many requests", "5xx", "503", "502", "504", "429",
    )

    def is_retryable_error(msg, policy=None):
        low = str(msg or "").lower()
        if not low:
            return False
        for kw in RETRYABLE_KEYWORDS:
            if kw in low:
                return True
        return False

# ── 枚举 ────────────────────────────────────────────────────────────────────
ERROR_CLASSES = (
    "timeout", "transient", "permission", "input_invalid",
    "output_rejected", "business", "interrupted", "internal", "needs_user",
)

#: 明确**不可重试**的类别（其余均可重试，见模块头"internal 归可重试"的取舍说明）。
# ★ 2026-11-20：`needs_user` 加进来 —— 子任务 ask_user/确认闸中断后**再重试只会让
#   模型改写成散文绕开确认**（真机 conv 999999952 的 t5 重试 2 次，最终既没卡片
#   也没暂停语义）。"等用户回答"是终态，不是故障，重试既不合法也不省时。
NON_RETRYABLE = frozenset({"permission", "input_invalid", "output_rejected",
                           "business", "needs_user"})

#: 等用户输入的中断文案（由 stream._run_subtask 统一以 `pending_clarify:` /
#: `pending_confirm:` 前缀写出；额外保留工具回执原文以兼容历史数据）。

# ── 永久失败特征词（小写匹配；顺序有讲究：先判权限再判参数）─────────────────
#: 停滞/超时/孤儿运行（本仓编排侧的真实形态，取自真库 54 条 failed 的全量聚类）




def classify_terminal(status: str, error_text: str) -> str:
    """终态任务的归因**单一真源**（★ 2026-11-15 P0-6）。

    为什么需要：`classify()` 只看自由文本，对「用户取消 / 目标已达成提前收敛 /
    验证收尾」这类**人为或策略性终止**一律兜底成 `internal`（未归类内部错误）——
    语义是错的。真库实测（2026-11-15 全库口径）：`status='cancelled'` 且
    `error_class` 为空的 30 行，`error` 文本重放 `classify()` **100% 落 internal**。
    同时 `agent_tasks.error_class` 此前只在 `TaskQueue.fail()` 写，
    `cancel()` / `cancel_run()` / 用户中断三个出口全不写
    ⇒ 失败/取消的 129 行里 93 行（72.1%）没有归因。

    语义约定（**不新增枚举**，全部落在 `ERROR_CLASSES` 8 类内）：
      - `done` / 其它非终态 ⇒ `""`（成功或仍在跑，不归因）
      - `cancelled`（含旧拼写 `canceled`）⇒ `interrupted`：
        人为/策略性终止，与 `orch_checkpoint.PHASE_INTERRUPTED` 同义；
        不是 `internal` 内部错误。此值**只由 status 决定**，与文本无关。
      - `failed` ⇒ 委托 `classify(error_text, ok=False)`，保留既有词表与优先级。

    ⚠️ 只用于**写终态时**。重试决策仍以 `classify()` 与
    `T1_RETRYABLE_CLASSES` 为准，本批对重试行为零改动。
    ⚠️ 存量回填也走这里：基于当时真实写入的 `error` 文本与 `status`
    **可复现派生**，不是凭空编造；留痕里必须标 `offline_replay`。
    """
    st = str(status or "").strip().lower()
    if st in ("cancelled", "canceled"):
        return "interrupted"
    if st == "failed":
        return classify(error_text, ok=False)
    return ""


def is_retryable_class(cls: str) -> bool:
    """该分类是否值得重试。空串（成功）返回 False。"""
    if not cls:
        return False
    return cls not in NON_RETRYABLE


def describe(cls: str) -> str:
    """分类 → 中文说明（用于日志/事件，避免下游再各写一套映射）。"""
    return {
        "": "成功",
        "timeout": "超时/停滞（可重试）",
        "transient": "瞬态外部故障（可重试）",
        "interrupted": "执行中断/孤儿运行（可重试）",
        "internal": "未归类内部错误（可重试）",
        "permission": "权限被拒（永久，不重试）",
        "input_invalid": "输入/参数不合法（永久，不重试）",
        "output_rejected": "输出不合格（永久，不重试）",
        "business": "业务规则拒绝（永久，不重试）",
    }.get(cls or "", cls or "")