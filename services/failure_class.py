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

# ── 复用既有瞬态词表（不重造第二份）────────────────────────────────────────
try:
    from tool_resilience import RETRYABLE_KEYWORDS, is_retryable_error
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
    "output_rejected", "business", "interrupted", "internal",
)

#: 明确**不可重试**的类别（其余均可重试，见模块头"internal 归可重试"的取舍说明）。
NON_RETRYABLE = frozenset({"permission", "input_invalid", "output_rejected", "business"})

# ── 永久失败特征词（小写匹配；顺序有讲究：先判权限再判参数）─────────────────
_PERMISSION_KW = (
    "permission_denied", "permission denied", "permission_error",
    "permissiondenied", "forbidden", "403", "401", "unauthorized",
    "无权限", "权限不足", "权限被拒", "没有权限", "not allowed",
    # ★ 2026-10-30（P0-2）：**委派最小权限**的白名单拒绝。
    #   真库取证：90 条失败行的文案是「工具「x」不在本次子任务授予的工具白名单内
    #   （最小权限，Worker ⊆ Supervisor），已拒绝执行」，旧词表零命中 ⇒ 全落兜底
    #   `internal`（可重试）⇒ 每次重试都撞同一堵墙，白烧一整轮子 Agent。
    #   `tools.py` 的拒绝分支注释早就写明这个缺口（当时只补了审计事件、没补词表）。
    #   ⚠️ 用"工具白名单内"而非"白名单"：后者会误伤"文档白名单已放宽"这类信息性文案。
    "工具白名单内", "不在本次子任务授予",
)
_INPUT_KW = (
    "invalid argument", "invalid input", "invalid parameter", "bad request",
    "missing required", "missing parameter", "schema", "validation error",
    # ⚠️ 4xx 是**请求本身有问题**，重发同一请求必然同样失败 —— 归入永久失败。
    #   实测本仓高频形态：`LLM API 400: {'error': {'message': ... 'invalid_request_error'}}`。
    #   不加这一条时它会落到兜底 `internal`（可重试）⇒ 白重试一次完整 LLM 调用。
    "400", "invalid_request", "bad_request", "unprocessable",
    "参数错误", "参数缺失", "参数非法", "缺少参数", "不合法的参数",
    # ★ 2026-10-30（P0-2）：真库取证出来的三个同族漏判（行数见模块头表格）：
    #   ① `INVALID_ARGUMENTS`（下划线形态）—— 旧词表只有带空格的 `invalid argument`；
    #   ② `缺少必填参数` —— 旧词表有 `缺少参数`，但它不是前者的连续子串（差"必填"两字）；
    #   ③ `不能为空` —— 文件/参数校验的高频形态。
    "invalid_argument", "invalid_arguments", "缺少必填参数", "不能为空",
    "必填参数", "缺少必要参数",
)
_OUTPUT_KW = (
    "schema_truncated", "parse_error", "json decode", "unexpected token",
    "协议校验", "输出不合规", "格式错误",
)
_BUSINESS_KW = (
    "no data", "nothing to do", "not found", "empty result",
    "无数据", "没有数据", "无可用", "不存在", "为空",
    # ★ 2026-10-30（P0-2）：工具已注册但**没有执行器**（配置缺失）。
    #   真库 10 行。重试不会凭空长出一个执行器 ⇒ 永久失败。
    "未配置执行器",
)
#: 停滞/超时/孤儿运行（本仓编排侧的真实形态，取自真库 54 条 failed 的全量聚类）
_TIMEOUT_KW = (
    "timeout", "无产出", "硬上限", "停滞", "无任何进展", "stalled", "deadline",
    # ★ 2026-10-30（P0-2）：`无输出` 与 `无产出` 同义（真库 36 行，旧判 transient）。
    #   两者都可重试 ⇒ **不改变重试行为**，只把归因从"网络瞬态"纠正为"停滞"。
    "无输出",
)
_INTERRUPTED_KW = (
    "孤儿运行", "进程重启", "执行线程丢失", "interrupted", "orphan",
)


def classify(error_text, ok=True):
    """错误文本 → 分类名（`ok=True` 表示成功，直接返回空串）。

    判定顺序（**顺序即优先级**，调换会误判）：
      1. 成功 / 空文本 → `""`
      2. `permission` → 在判 timeout **之前**。因为权限拒绝常伴随"调用超时/未返回"，
         若先判 timeout 会把永久失败误归为可重试 —— 那样本次改动等于没做。
      3. `interrupted` → 在 timeout 之前，但**在 permission 之后**：
         孤儿运行文案里含"超时"语义但根因是进程重启，单独归类。
      4. `timeout` → 停滞/硬上限。
      5. `input_invalid` / `output_rejected` / `business` → 其余永久失败。
      6. `transient` → 命中 `tool_resilience` 的瞬态词表（连接类/5xx/429）。
      7. 兜底 `internal`（可重试，见模块头取舍）。
    """
    if ok:
        return ""
    text = str(error_text or "")
    low = text.lower()
    if not low.strip():
        # 有失败标志但无文本：算内部错误（可重试），而不是永久失败。
        return "internal"

    # 2) 权限（最先判，见函数注释）
    for kw in _PERMISSION_KW:
        if kw in low:
            return "permission"

    # 3) 孤儿运行/进程重启（先于 timeout：文案含"无任何进展"，根因不同）
    for kw in _INTERRUPTED_KW:
        if kw in low:
            return "interrupted"

    # 4) 超时/停滞
    for kw in _TIMEOUT_KW:
        if kw in low:
            return "timeout"

    # 5) 其余永久失败
    for kw in _INPUT_KW:
        if kw in low:
            return "input_invalid"
    for kw in _OUTPUT_KW:
        if kw in low:
            return "output_rejected"
    for kw in _BUSINESS_KW:
        if kw in low:
            return "business"

    # 6) 瞬态外部故障（复用工具层词表）
    if is_retryable_error(text):
        return "transient"

    # 7) 兜底
    return "internal"


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