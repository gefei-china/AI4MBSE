"""纯判据单一真源：classify（P1 收敛，2026-11-25）。

从 `services/failure_class.py` 下沉：原调用方 `core/` 反向依赖上层（tools / services）属方向错误。
本模块零业务依赖；原路径保留 re-export，旧 import 拿到同一对象。
"""

import re


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




_NEEDS_USER_KW = (
    "pending_clarify:", "pending_confirm:", "等待用户澄清", "等待人工确认",
    "流程暂停，等待用户回答", "已向用户发起确认", "等待用户回答后继续",
)
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
    # ★ 2026-11-26（F1 存量覆盖）：`LIKE or GLOB pattern too complex` —— SQLite 对
    #   查询模式复杂度有硬上限，**同一个查询重发必然同样失败** ⇒ 永久失败。
    #   原来落 `internal`（可重试）⇒ 白重试一次完整检索。归 input_invalid 后不再重试。
    #   （措辞按 SQLite 原文，只匹配这一族，不影响其他错误。）
    "pattern too complex",
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
_TIMEOUT_KW = (
    "timeout", "无产出", "硬上限", "停滞", "无任何进展", "stalled", "deadline",
    # ★ 2026-10-30（P0-2）：`无输出` 与 `无产出` 同义（真库 36 行，旧判 transient）。
    #   两者都可重试 ⇒ **不改变重试行为**，只把归因从"网络瞬态"纠正为"停滞"。
    "无输出",
    # ★ 2026-11-26（F1 存量覆盖）：`子任务未产生输出` —— 与"无输出"同义但措辞不同，
    #   原来落到 `internal` 兜底（internal 也是可重试，故**不改变重试行为**，
    #   只把归因从"未知内部错误"纠正为"停滞无产出"，排障时能直接看出该查哪一环）。
    "未产生输出",
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

    # 1b) 等用户输入（★ 2026-11-20）：必须先于 timeout / internal 判定。
    #     `pending_*` 文案里常带"确认/暂停"，若落到 internal 会被重试，
    #     于是模型第二次改写成"请确认"散文 —— 卡片永远出不来。
    for kw in _NEEDS_USER_KW:
        if kw in low:
            return "needs_user"

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
