"""D2：工具执行容错——重试（指数退避）+ 降级回退链。

设计：
- 重试仅对「连接类失败」生效（timeout/连接失败/5xx/429），保证幂等安全：
  请求未发出或响应未收到时重试不会造成副作用重复；业务级错误（4xx/工具逻辑错误）不重试。
- 重试策略按工具配置（tools.retry_policy JSON），默认关闭（max_retries=0），
  仅外部集成类工具（mcp/http）参与，内置本地工具直接放行。
- 降级链：主工具失败且重试耗尽 → 自动切换到 fallback_to 指定工具，返回带 degraded_from 标记。
"""
import json
import re
import time

# 可重试的失败特征（小写匹配）
RETRYABLE_KEYWORDS = (
    "timeout", "timed out", "connection", "network", "getaddrinfo",
    "refused", "reset", "unreachable", "bad gateway", "service unavailable",
    "gateway timeout", "too many requests", "5xx", "503", "502", "504", "429",
)

DEFAULT_POLICY = {
    "max_retries": 1,          # 默认最多重试 1 次（保守，防重复副作用）
    "backoff_base_ms": 500,    # 首次退避基数
    "backoff_multiplier": 2,   # 指数倍数
    "retry_on": [],            # 额外可重试错误关键字（业务方可按需扩展）
}

MAX_BACKOFF_MS = 10000


def parse_policy(raw: str) -> dict:
    try:
        p = json.loads(raw or "{}")
    except Exception:
        p = {}
    if not isinstance(p, dict):
        p = {}
    merged = dict(DEFAULT_POLICY)
    merged.update({k: v for k, v in p.items() if k in DEFAULT_POLICY})
    merged["max_retries"] = max(0, int(merged["max_retries"]))
    return merged


def is_retryable_error(msg: str, policy: dict | None = None) -> bool:
    """判断错误消息是否为可重试的瞬态失败。"""
    low = str(msg or "").lower()
    if not low:
        return False
    # HTTP 状态码精确匹配（5xx / 429）：如 "HTTP 500: url"、"HTTP 503"
    if re.search(r"http\s+5\d\d|http\s+429", low):
        return True
    for kw in RETRYABLE_KEYWORDS:
        if kw in low:
            return True
    for kw in (policy or {}).get("retry_on", []) or []:
        if str(kw).lower() in low:
            return True
    return False


def retry_exec(fn, policy: dict, error_fn=None) -> tuple:
    """带指数退避的重试执行器。

    fn() 返回标准化结果 {ok, result, is_error, ...}；error_fn 可附加错误分类。
    返回 (最终结果, 实际重试次数, 是否发生重试)。
    """
    max_retries = int(policy.get("max_retries", 0))
    base = int(policy.get("backoff_base_ms", 500))
    mult = float(policy.get("backoff_multiplier", 2))
    if max_retries <= 0:
        return fn(), 0, False

    attempts = 0
    while True:
        r = fn()
        if r.get("ok"):
            return r, attempts, attempts > 0
        if attempts >= max_retries:
            return r, attempts, attempts > 0
        msg = (r.get("result") or "") + " " + (error_fn(r) if error_fn else "")
        if not is_retryable_error(msg, policy):
            return r, attempts, attempts > 0
        attempts += 1
        delay_ms = min(base * (mult ** (attempts - 1)), MAX_BACKOFF_MS)
        time.sleep(delay_ms / 1000.0)
