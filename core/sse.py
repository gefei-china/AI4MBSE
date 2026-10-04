"""SSE（Server-Sent Events）传输层加固 —— 心跳保活与断流器（P1-2，2026-10-03）。

**为什么需要它**（《架构-可扩展性-稳定性整体评估》§4.3 / §6 P1-2 实测）：
    本工程的流式输出 `/api/conversations/{id}/chat/stream` 是**同步生成器**，
    单次 `next()` 就是一次完整 LLM 调用或工具调用 —— 实测单轮编排 **344 s / 849 s / 1155 s**，
    其中等待 LLM 首 token、等待工具返回期间**一个字节都不产生**。零字节窗口足以让：
      · nginx 反向代理（`proxy_read_timeout` 默认 60 s）判定上游失活并掐断；
      · 企业网关/负载均衡的 idle timeout（常见 60~120 s）静默回收连接；
      · 移动端/休眠标签页被浏览器空闲策略挂起。
    断流的代价在**本工程尤其高**：SSE 断连后不可续流（无 Last-Event-ID 机制），
    用户看到的是"回答到一半卡住 → 报错 → 已生成内容要靠 fallback 才留得住"。

**标杆做法**（OpenAI streaming / Dify workflow SSE / Vercel AI SDK 一致）：
      · 周期性发送**注释帧** `: ping\n\n`（注释帧在 SSE 协议里被客户端忽略，
        不改变事件语义，因此可以无条件穿插，不会污染 `data:` 解析）；
      · 首帧声明 `retry: <ms>`，显式告诉客户端自动重连间隔。
    本模块即按该语义实现，**不改变原有事件流的内容与顺序**。

设计要点：
    1. **必须跨线程**：心跳不能由事件源自己发 —— 源生成器正阻塞在 `next()` 里，
       没人能插进去产帧。因此把源挪到 worker 线程，主线程用 `Queue.get(timeout=)`
       等待；超时即产心跳帧。这是唯一能在不重构上层（仍是同步生成器）的前提下加心跳的办法。
    2. **异常必须回传**：worker 里的异常放到 box 里、在主生成器重新抛出，
       保持"生产者异常 → 调用方看得到"的原语义（静默吞掉 = 把 bug 藏进线程里）。
    3. **客户端断开必须能止损**：`GeneratorExit` 时主动 `close()` 事件源 Generator，
       否则 worker 会继续消费 LLM 直到本轮结束 —— 即"用户关了页面，钱还在烧"。
"""
import queue
import threading

__all__ = ["iter_with_heartbeat", "snapshot", "degrade_delta"]

_SENTINEL = object()


# ── P1-3（2026-10-03 降级显式化）：可测试的纯函数，路由层不做内联算术 ──
# 为什么抽成函数而不是写在 chat_stream 里：降级判定直接决定"这段产出可不可信"，
# 内联就只能靠端到端跑 LLM 才能验证；抽出来后可用纯数据断言（零 LLM、零库），
# 才守得住"静默兜底**必须**显式化"这条纪律。
def snapshot(stats: dict) -> dict:
    """LLM 客户端 stats → 降级判定所需的**累计计数**快照。"""
    s = stats or {}
    return {"mock": int(s.get("mock", 0) or 0),
            "real": int(s.get("real", 0) or 0),
            "fallback": int(s.get("fallback_total", 0) or 0)}


def degrade_delta(before: dict, now: dict) -> dict:
    """两次快照之差 = 该区间内真实发生的降级次数。

    一律取 max(0, …)：`llm_client` 是进程单例，若期间被重建/计数重置，差值会变负 ——
    那时宁可报 0，也不能让用户看到"本次发生 -3 次降级"这种自相矛盾的提示。
    """
    b, n = before or {}, now or {}
    return {"mock_calls": max(0, int(n.get("mock", 0)) - int(b.get("mock", 0))),
            "real_calls": max(0, int(n.get("real", 0)) - int(b.get("real", 0))),
            "fallback_calls": max(0, int(n.get("fallback", 0)) - int(b.get("fallback", 0)))}


def iter_with_heartbeat(iterator, interval_s: float = 15.0, comment: str = "ping"):
    """把阻塞式同步事件源包装成「带心跳」的生成器。

    :param iterator: 原 SSE 帧字符串生成器（`f"event: x\\ndata: ...\\n\\n"`）
    :param interval_s: 无数据超过该秒数即产出一帧心跳；必须**明显小于**链路上最小的
                       idle timeout（推荐 ≤ 15 s，对应 nginx 默认 60 s 的 1/4）
    :param comment: 心跳注释内容（客户端按 SSE 协议忽略）
    产出：**原帧原序输出**，中间穿插 `: {comment}\\n\\n`。
    """
    q: "queue.Queue" = queue.Queue(maxsize=0)
    box: dict = {}
    stop = threading.Event()
    started = threading.Event()

    def _worker():
        # 止损点必须在 **worker 线程内部**：CPython 里对"正在另一个线程栈上执行"的
        # generator 调 close() 会抛 `ValueError: generator already executing`
        #（实测复现过 —— 见 tools/verify/verify_sse_heartbeat.py 的 M4）。
        # 所以由消费侧置 stop 标志，worker 在**刚拿到一帧、源未 running 时**安全地关掉源。
        started.set()
        try:
            for frame in iterator:
                if stop.is_set():
                    try:
                        iterator.close()   # 同线程调用：此刻 generator 未在执行，安全
                    except Exception:
                        pass
                    return
                q.put((False, frame))
        except BaseException as e:  # noqa: BLE001 —— 含 GeneratorExit，必须全部回传
            box["exc"] = e
        finally:
            q.put((True, _SENTINEL))   # 无论正常结束还是异常，都必须给下游一个终点

    _t = threading.Thread(target=_worker, name="sse-source", daemon=True)
    _t.start()
    started.wait(timeout=5)   # 只等线程启动本身，不等它产出数据

    try:
        while True:
            try:
                done, frame = q.get(timeout=interval_s)
            except queue.Empty:
                yield ": %s\n\n" % comment
                continue
            if done:
                break
            yield frame
        exc = box.get("exc")
        if exc is not None:
            # 只在正常迭代到终点后抛；若是 GeneratorExit 说明外层已关闭，无需再抛
            if not isinstance(exc, GeneratorExit):
                raise exc
    finally:
        # 客户端断开 → 外层抛 GeneratorExit 到本生成器 → 置 stop，让 worker 在下一个
        # 帧边界停下（最多滞后一帧，远好于"用户关了页面、LLM 还在烧钱跑到本轮结束"）。
        stop.set()
        # 兜底尝试：若此刻源刚好没在执行，close 直接生效（省掉一帧的滞后）
        try:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
