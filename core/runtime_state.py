"""运行时进程级状态：优雅关闭 + 全局并发闸 + 单请求墙钟预算（2026-10-30）。

━━━ 为什么需要这个模块 ━━━
三项稳定性缺口都缺同一个东西：**一个进程级、跨请求可见的状态载体**。
    · 优雅关闭：`lifespan` 收尾时要知道"还有哪些流没结束"，否则只能干等到超时；
    · 并发背压：per-agent `Semaphore` 只管编排分支（`stream.py:754`），
      会话直答路径**无任何上限** ⇒ N 个并发长任务 = N 个 daemon 线程；
    · 墙钟预算：现有 `auto_continue_deadline_s` 是"自动续跑"的闸、
      `subtask_idle_timeout_s` 是**子任务**级，**整轮无上限**（实测 run 跑到 918s 仍算正常）。

━━━ 为什么是进程内状态（而不是 Redis） ━━━
本工程是**单进程部署**（`uvicorn.run(app, ...)` 无 `workers`）+ SQLite 单写者。
单进程下进程内状态就是**正确**的载体；引 Redis 只为"将来可能多进程"是过早优化，
且违反本仓"单机可跑"的约束。若将来确实要多进程，本模块的全部状态都需要外置 ——
`docs/运行时稳定性优化-行业调研与实施方案-20261030.md` §4 记录了多进程的三个失真点。

━━━ 三个使用约定（都踩过坑，不是风格问题）━━━
1. **并发闸必须在生成器内部持有**：写在端点函数里 `async with gate(): return
   StreamingResponse(gen())`，函数一 return 信号量就释放了，**流的整个生命周期根本没被限流**
   （行业原话："Hold the semaphore inside the generator. Otherwise your limiter is
   measuring the wrong thing."）。
2. **draining 用 threading.Event 而非 asyncio.Event**：`lifespan` 在主 loop 上，
   但读它的是**跑在 daemon 线程里的同步生成器**（`core/sse.py` 的 worker），
   asyncio.Event 跨线程读不安全。
3. **信号量按 loop 惰性重建**：`asyncio.Semaphore` 首次 await 时绑定 loop，
   跨 loop 复用抛 "Task got Future attached to a different loop"。
   服务是单 loop，但测试/脚本会反复新建 loop ⇒ 必须每次校验。
"""
import asyncio
import threading
import time
from contextlib import asynccontextmanager

__all__ = [
    "init", "is_draining", "begin_drain", "drain_elapsed",
    "stream_gate", "inflight_count", "compute_deadline", "deadline_left",
    "set_thread_deadline", "thread_deadline_left", "cap_timeout",
    "stats", "reset_for_test",
]

# ── 在途流计数（线程安全：async 主协程写、daemon 线程读）──────────────
_inflight = 0
_inflight_peak = 0
_inflight_lock = threading.Lock()

# ── 关闭中标志 ────────────────────────────────────────────────────
#: 置位后：新请求快速失败；在途请求继续跑到各自的 deadline 或自然结束。
_draining = threading.Event()
_drain_started_at = 0.0

# ── 全局并发闸（惰性创建，绑定当前 loop）─────────────────────────────
_gate = None
_gate_loop = None

# ── 配置缓存（`init()` 写入；读侧一律走 `_cfg()` 带默认值兜底）─────────
# 默认值取法：
#   · max_concurrent_streams=8 —— 单进程 + SQLite 单写者。每个流占 1 个 daemon 线程
#     且内部还要开编排线程池（max_workers=3）⇒ 8 路已是 8+24 线程量级；
#   · queue_wait_s=2.0 —— 给 2 秒排队窗口再拒，交互式场景比"立即 429"友好，
#     又不至于让调用方以为"卡住了"；
#   · chat_deadline_s=900 —— 实测编排批次 avg 918s、p-max 1155s。900 是"绝大多数
#     批次能跑完"与"异常批次不无限占资源"的折中；超时不是 kill，是收尾保留产出；
#   · shutdown_drain_s=30 —— 与行业常见的 20~45s 区间一致；超过 45s 容易被上层
#     （K8s terminationGracePeriod / nginx proxy_read_timeout）直接 SIGKILL。
_DEFAULTS = {
    "max_concurrent_streams": 8,
    "queue_wait_s": 2.0,
    "chat_deadline_s": 900,
    "shutdown_drain_s": 30,
}
_cfg_cache = dict(_DEFAULTS)


def _cfg(key):
    """读配置项。**任何异常都退回默认值** —— 本模块处在请求入口，
    不能因为配置读不到就让所有对话 500。"""
    try:
        from core import config as _c
        v = _c.get("runtime", key, _DEFAULTS.get(key))
        return _DEFAULTS.get(key) if v is None else v
    except Exception:
        return _DEFAULTS.get(key)


def init():
    """从配置刷新缓存（lifespan 启动时调一次；运行中改配置需重启才生效，
    与 `config.get` 的既有语义一致）。"""
    for k in _DEFAULTS:
        _cfg_cache[k] = _cfg(k)
    return dict(_cfg_cache)


# ── 关闭中 ────────────────────────────────────────────────────────
def is_draining():
    return _draining.is_set()


def begin_drain():
    """进入关闭中。可重复调用（幂等）。"""
    global _drain_started_at
    if not _draining.is_set():
        _drain_started_at = time.monotonic()
        _draining.set()


def drain_elapsed():
    """已进入关闭状态多少秒（未关闭=0）。"""
    return (time.monotonic() - _drain_started_at) if _draining.is_set() else 0.0


# ── 并发闸 ────────────────────────────────────────────────────────
def _get_gate():
    """取当前 loop 上的信号量（loop 变了就重建 —— 见模块头约定 3）。"""
    global _gate, _gate_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _gate is None or _gate_loop is not loop:
        _gate = asyncio.Semaphore(int(_cfg_cache["max_concurrent_streams"]))
        _gate_loop = loop
    return _gate


class GateBusy(Exception):
    """并发闸排队超时（调用方据此返 429 / 发 error 帧）。"""

    def __init__(self, wait_s, limit):
        self.wait_s = wait_s
        self.limit = limit
        super().__init__("并发已达上限（%s 路），排队 %ss 未获得名额" % (limit, wait_s))


@asynccontextmanager
async def stream_gate():
    """获取一个流式并发名额。**必须包住整个生成器的产出过程**（见约定 1）。

    用法（生成器内部）：
        async with stream_gate():
            async for frame in frames:
                yield frame

    排队超过 `queue_wait_s` ⇒ 抛 `GateBusy`，由调用方转成 429/error 帧。
    为什么是"有限排队 + 快速失败"而不是"无限排队"：
      排队会让 p99 雪崩（所有人都在等），且 SSE 一旦迟迟不产帧就被上游网关切断；
      快速失败至少让调用方知道该重试。
    """
    global _inflight, _inflight_peak
    wait_s = float(_cfg_cache["queue_wait_s"])
    gate = _get_gate()
    try:
        await asyncio.wait_for(gate.acquire(), timeout=wait_s)
    except asyncio.TimeoutError:
        raise GateBusy(wait_s, int(_cfg_cache["max_concurrent_streams"]))
    with _inflight_lock:
        _inflight += 1
        if _inflight > _inflight_peak:
            _inflight_peak = _inflight
    try:
        yield
    finally:
        with _inflight_lock:
            _inflight -= 1
        gate.release()


def inflight_count():
    with _inflight_lock:
        return _inflight


# ── 墙钟预算 ──────────────────────────────────────────────────────
def compute_deadline(budget_s=None):
    """算出一个绝对 deadline（`time.time()` 口径，可跨线程/跨模块传）。

    为什么用**绝对时间戳**而不是"剩余秒数"：
      剩余秒数在传递链上每过一层就要重算，任一层忘了减就会放大预算；
      绝对时间戳是幂等的，谁读都是同一个答案（gRPC deadline / Temporal
      `start_to_close` 都是这个思路）。
    """
    b = float(_cfg_cache["chat_deadline_s"] if budget_s is None else budget_s)
    return time.time() + b if b > 0 else 0.0


def deadline_left(deadline_ts):
    """距 deadline 还剩几秒。`0.0` / `None` / 负数 ⇒ 0.0（表示"不限"或"已到"，
    调用方需自行区分：只有 >0 才是真有限预算）。"""
    try:
        d = float(deadline_ts or 0)
    except Exception:
        return 0.0
    if d <= 0:
        return 0.0
    return max(0.0, d - time.time())


# ── 线程级 deadline 传播（让**每次 LLM/HTTP 调用**都受剩余墙钟约束）──────────
# 为什么需要（2026-10-30 实测，本轮最重要的一处发现）：
#   墙钟只在"主循环轮次边界"检查是**不够的** —— 单次 LLM 调用可以一口气阻塞几十秒：
#   实测 conv 9771（墙钟 8s、真实耗时 61s）卡在 `planner` 的 LLM 调用
#   **56,978 ms**（span `status=error, note=unclosed_at_flush`），而它在
#   编排主循环**之前**执行 ⇒ 循环里的检查根本没机会跑。
#   行业做法（gRPC deadline / Temporal `start_to_close_timeout`）是**把 deadline 往下传**，
#   让每一层把自己的超时压到"剩余预算"以内 —— 这里用线程局部变量实现：
#     pipeline 生成器整体跑在 SSE 的 daemon 线程，LLM 调用也在同一线程，
#     所以 `threading.local()` 天然就是"请求作用域"（无需跨线程传递）。
_TLS = threading.local()


def set_thread_deadline(deadline_ts):
    """在当前线程登记本轮 deadline（`execute_stream` 入口调用一次）。

    `<=0` 表示**不限预算** ⇒ 清掉登记（而不是存一个 0）。
    这个区分是必需的：详见 `thread_deadline_left` 的说明。
    """
    try:
        _t = float(deadline_ts or 0.0)
    except Exception:
        _t = 0.0
    if _t > 0:
        _TLS.deadline_ts = _t
    else:
        try:
            del _TLS.deadline_ts
        except AttributeError:
            pass


def thread_deadline_left():
    """当前线程的剩余预算（秒）。

    :return: `None` = **未登记**（不限预算，调用方保持原行为）；
             浮点值 = 已登记，`0.0` 表示**已到点**。

    ⚠️ 必须区分 `None` 与 `0.0`（本轮实测踩过）：
      首版让两者都返回 `0.0`，于是 `cap_timeout` 的 `if left <= 0: return base`
      把"已过期"当成了"未登记" ⇒ 过期时反而返回原始 180s，
      等于墙钟对已到点的调用**完全失效**（自证时暴露：剩 1s → 0，已过期 → 180）。
    """
    ts = getattr(_TLS, "deadline_ts", None)
    if ts is None:
        return None
    return deadline_left(ts)


def cap_timeout(base_s, floor_s=3.0):
    """把一次调用的超时压到剩余墙钟以内。

    :param base_s: 该调用原本的超时（如 `llm.http_timeout_s` = 180）
    :param floor_s: 下限（秒）。**不能压到很小**——压到 0.5s 会让所有调用必然失败、
                   连"收尾用的那句总结"都产不出来。3s 是"够发一次短请求"的经验值。
    :return: 实际应使用的超时。未登记 deadline ⇒ 原样返回 `base_s`（零行为变更）。

    ⚠️ 返回 0 的语义：**剩余预算已耗尽**。调用方应据此直接放弃该次调用，
       而不是拿 0 去发请求（httpx 的 timeout=0 会被当成"立即超时"，
       行为上等价，但错误信息会误导成"网络问题"）。
    """
    left = thread_deadline_left()
    if left is None:
        return float(base_s)      # 未登记 ⇒ 保持原值（脚本/测试/非会话路径零影响）
    if left < floor_s:
        return 0.0                # 已到点或不足下限 ⇒ 明确告知"没预算了"
    return min(float(base_s or left), left)


# ── 观测 ──────────────────────────────────────────────────────────
def stats():
    return {
        "draining": is_draining(),
        "drain_elapsed_s": round(drain_elapsed(), 1),
        "inflight": inflight_count(),
        "inflight_peak": _inflight_peak,
        "gate_limit": int(_cfg_cache["max_concurrent_streams"]),
        "queue_wait_s": float(_cfg_cache["queue_wait_s"]),
        "chat_deadline_s": float(_cfg_cache["chat_deadline_s"]),
        "shutdown_drain_s": float(_cfg_cache["shutdown_drain_s"]),
    }


def reset_for_test():
    """仅测试用：把进程级状态清回初始（生产路径**不得**调用）。"""
    global _inflight, _inflight_peak, _gate, _gate_loop
    with _inflight_lock:
        _inflight = 0
        _inflight_peak = 0
    _draining.clear()
    _gate = None
    _gate_loop = None
    _cfg_cache.update(_DEFAULTS)
    # 线程局部也要清：否则同一测试进程里上一用例登记的 deadline
    # 会漏进下一用例（表现为"未登记却读到剩余预算"）。
    try:
        del _TLS.deadline_ts
    except AttributeError:
        pass
