# -*- coding: utf-8 -*-
"""P1-1（2026-10-03 整改）：按身份分桶的请求限流 + 配额位点 —— 进程内滑动窗口。

**为什么需要它**（《架构评估》§4.3 / §6 P1-1 实测）：
    全仓 **0 处** rate_limit / throttle / 配额代码；而 `budget_tokens` 曾实测超支 **35%**
    （设 100 万、实耗 135 万）—— 说明"预算"这个关口当时也没真正生效，后来被置 0 = 完全不限。
    叠加 P0-1（同步阻塞 + anyio 默认线程池上限 40）的现实：**一个用户连点 10 次发送，
    就能把 40 个线程占掉 1/4**，其余用户的普通页面请求开始排队。
    评估给的正确定性是"它不是被设计成不支持，而是**从来没设过闸**"。

**标杆对位**：
    · **Dify**：工作区级配额（成员数 + 调用额度），超限返回明确配额错误而非排队等待；
    · **Anthropic / GitHub API**：响应带 `X-RateLimit-Limit/Remaining/Reset` + `Retry-After`，
      让**客户端知道自己被限了、什么时候可以重试**（比"静默排队"重要得多）；
    · **Manus**：任务级并发上限 + 排队位。
    本实现取第二条的契约 + 第一条的"按身份分桶"，并刻意保留"一次性排空即可恢复"的
    简单语义 —— 在单进程形态下，复杂的令牌桶只会带来难以排障的状态。

**明确的边界（不要指望它做的事）**：
    · 这是**进程内**计数：多实例部署时每个实例各算各的，总量 = 实例数 × 阈值。
      真正的分布式配额需要 Redis（§7.2 已列为"暂不做"，当前真实负载用不上）。
    · 只挡"入口请求"，不约束 worker 内部对 LLM 的调用次数；后者靠 `delegation.max_tasks`
      和 llm 预算共同限制，两者不是一回事（一个用户 ≡ 一次会话申请 ≠ 十个子任务）。
"""
import threading
import time
from collections import defaultdict, deque

__all__ = ["RateLimitMiddleware", "classify", "limiter", "SlidingWindowLimiter"]

_DEFAULTS = {"chat_per_min": 5, "write_per_min": 60, "read_per_min": 300}

# chat 桶：会触发长编排（单轮实测 344~1155 s），必须单独且最严
_CHAT_MARKERS = ("/chat", "/chat/stream", "/orchestrate", "/flows/run", "/eval/run")
_EXEMPT_DEFAULT = ("/api/health", "/api/monitor", "/api/auth", "/static", "/api/ux-metrics")


def classify(path: str, method: str) -> str:
    """请求 → 限流桶：`chat` | `write` | `read`。

    归类的判据不是 HTTP 方法，而是**单次要占用多少线程时间**：chat 桶装的是
    "一次调用要跑几分钟"的端点（编排 / 会话直答 / 流程执行），它们一旦被连点，
    最先被拖垮的是整个线程池（见 P0-1）。所以 `/api/studio/flows/{id}/run`
    这类**带 id 的长执行端点**必须与普通的 POST 分开 —— 只按方法分流会漏掉它们。
    """
    p = ((path or "")[:200]).rstrip("/")
    if any(m in p for m in _CHAT_MARKERS):
        return "chat"
    if p.endswith("/run") or p.endswith("/chat/stream"):
        return "chat"
    return "read" if (method or "GET").upper() in ("GET", "HEAD", "OPTIONS") else "write"


class SlidingWindowLimiter:
    """滑动窗口计数器（线程安全，无后台线程，满则直接排空过期项）。

    为什么不用令牌桶：本工程的请求形态是"极低频但单次极重"，令牌桶的平滑优势用不上，
    反而多一个"为什么现在限流了"的解释成本；滑动窗口的语义对用户最简单：
    "每分钟最多 N 次，第 N+1 次会被拒绝"。
    """

    def __init__(self):
        self._hits = defaultdict(deque)      # key -> deque[timestamp]
        self._lock = threading.Lock()
        self._max_keys = 10000               # 防御：异常来源噪声把字典撑爆

    def hit(self, key: str, limit: int, window_s: int = 60) -> tuple:
        """记一次命中。返回 (allowed: bool, remaining: int, retry_after_s: int, limit: int)。

        类型防御为什么在这里做而不是只靠调用方：`limit` 来自配置（可被面板/配置文件改成
        任意值），一旦出现 None/"abc" 这类脏值，`limit <= 0` 会抛 TypeError ——
        而这条路在**请求入口**上，等于"配错一次限流，全站 500"。
        方向选"放行"而非"拒绝"：限流是保护性设施，**它自己绝不能成为故障源**
        （tools/verify/verify_rate_limit.py 的 I7a 即锁这条）。
        """
        try:
            limit = int(limit)
            window_s = int(window_s)
        except (TypeError, ValueError):
            return True, -1, 0, 0
        if limit <= 0 or window_s <= 0:
            return True, -1, 0, 0      # 阈值/窗口未配置 = 不限
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and (now - q[0]) > window_s:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_s - (now - q[0])) + 1)
                return False, 0, retry, limit
            q.append(now)
            if len(self._hits) > self._max_keys:      # 极端情况下的自保：整体清空
                self._hits.clear()
            return True, max(0, limit - len(q)), 0, limit

    def stats(self) -> dict:
        with self._lock:
            return {"keys": len(self._hits),
                    "entries": sum(len(v) for v in self._hits.values())}


limiter = SlidingWindowLimiter()


def _cfg_limits():
    try:
        from core import config as _c
        return {"chat": int(_c.get("rate_limit", "chat_per_min", _DEFAULTS["chat_per_min"])),
                "write": int(_c.get("rate_limit", "write_per_min", _DEFAULTS["write_per_min"])),
                "read": int(_c.get("rate_limit", "read_per_min", _DEFAULTS["read_per_min"]))}
    except Exception:
        return dict(_DEFAULTS)


def _identity(token: str, uid: str, path: str, method: str) -> str:
    """限流主键：**已登录按用户、匿名为空时按 UA+路径族**（避免把整个内网打成一个桶）。"""
    if uid:
        return "u:%s" % uid[:64]
    if token:
        return "s:%s" % token[:16]
    return "anon"


class RateLimitMiddleware:
    """纯 ASGI 限流中间件（**不用** BaseHTTPMiddleware —— 与既有审计中间件同一理由：
    它会包装响应体为 anyio 流，对本工程 SSE 流式输出有已知干扰）。

    超限行为：返回 429 + `Retry-After` + `X-RateLimit-*`，**不排队**。
    为什么：排在队里 = 用户看着转圈却不知道发生了什么；明确拒绝 + 告知多久可重试，
    客户端才能做出正确反应（这也是 Anthropic/GitHub 的一致做法）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            from core import config as _c
            if not bool(_c.get("rate_limit", "enabled", True)):
                await self.app(scope, receive, send)
                return
            exempt = _c.get("rate_limit", "exempt_paths", None) or _EXEMPT_DEFAULT
        except Exception:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        method = (scope.get("method") or "GET").upper()
        if any(path.startswith(p) for p in exempt):
            await self.app(scope, receive, send)
            return

        from starlette.datastructures import Headers as _H
        hdrs = _H(scope=scope)
        bucket = classify(path, method)
        limit = _cfg_limits().get(bucket, 0)
        key = "%s|%s" % (_identity(hdrs.get("X-Session-Token", ""),
                                   hdrs.get("X-User-Id", ""), path, method), bucket)
        allowed, remaining, retry, used_limit = limiter.hit(key, limit, 60)
        if allowed:
            # 把配额状态回给客户端 —— 让"还剩几次"变成可观测，而不是猜
            async def _send(message):
                if used_limit > 0 and message.get("type") == "http.response.start":
                    from starlette.datastructures import MutableHeaders as _MH
                    mh = _MH(scope=message)
                    mh["X-RateLimit-Limit"] = str(used_limit)
                    mh["X-RateLimit-Remaining"] = str(remaining)
                    mh["X-RateLimit-Bucket"] = bucket
                await send(message)
            await self.app(scope, receive, _send)
            return

        payload = ("{\"error\":\"请求过于频繁，请稍后重试\",\"bucket\":\"%s\","
                   "\"limit\":%d,\"retry_after\":%d}" % (bucket, used_limit, retry))
        body = payload.encode("utf-8")
        await send({"type": "http.response.start", "status": 429,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"retry-after", str(retry).encode()),
                                (b"x-ratelimit-limit", str(used_limit).encode()),
                                (b"x-ratelimit-remaining", b"0"),
                                (b"x-ratelimit-bucket", bucket.encode()),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
