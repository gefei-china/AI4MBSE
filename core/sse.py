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
import time

__all__ = ["iter_with_heartbeat", "iter_with_heartbeat_async", "to_sse_frame",
           "snapshot", "degrade_delta"]

_SENTINEL = object()


def _stage_of(frame):
    """从 SSE 帧里廉价提取「阶段名」（P1-33，2026-10-10 执行过程可见性）。

    只对 stage/agent 两类事件做解析（其余帧直接返回 None，零开销）——
    token/reasoning 等高频帧不进 JSON 解析，心跳路径绝不拖慢主链路。
    解析失败一律返回 None（心跳绝不因提取失败而中断）。
    """
    try:
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8", "replace")
        if not (isinstance(frame, str) and (frame.startswith("event: stage")
                                            or frame.startswith("event: agent"))):
            return None
        import json as _json
        for line in frame.split("\n"):
            if line.startswith("data: "):
                d = _json.loads(line[6:])
                return d.get("name") or d.get("display_name") or None
    except Exception:
        return None
    return None


def _ping_frame(elapsed_s: float, last_stage) -> str:
    """可见心跳帧：告诉前端「还活着、正卡在哪一步、等了多久」。

    背景（实测 2026-10-10）：LLM 网关/内网 embedding 故障时，单步重试链可达数分钟，
    期间用户只看到无限转圈 —— 注释帧（`: ping`）按协议被客户端忽略，前端**无从展示**。
    改为真事件帧后前端可显示「仍在执行：生成与校验（已等待 96s）」。
    """
    import json as _json
    return "event: ping\ndata: " + _json.dumps(
        {"elapsed_s": round(elapsed_s, 1),
         "last_stage": last_stage or ""},
        ensure_ascii=False) + "\n\n"


# ── 事件归一化：dict → SSE 帧字符串（2026-10-04 补，带血泪教训）──────────────
# 【为什么必须有这个函数】`AgentPipeline.execute_stream` 产出的是 **dict** 事件
# （{"type":"token","delta":...}），而 Starlette 的 `StreamingResponse` 只接受
# str/bytes —— 直接把 dict 丢给它会抛
#   `AttributeError: 'dict' object has no attribute 'encode'`
# 而这**正是批次 1 引入的回归**：改用 iter_with_heartbeat 包装时，
# 端点里的 `yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"` 被简化成了
# `yield raw`，**序列化就此丢失**，而当时的门禁只测包装器本身（喂的是字符串源），
# 真机也没打这个端点 ⇒ 主功能 `/chat/stream` 一直是坏的，直到今天真机验证才暴露。
#
# 【为什么放在包装器里而不是端点里】
# ① 单一真源：两个包装器（同步/异步）都过它，**不可能再丢一次**；
# ② 对已序列化的字符串**原样透出**，flows.py 那条链路行为不变（向后兼容）；
# ③ 可离线单测：给包装器喂 dict 源，断言产出是 `event: X\ndata: {...}\n\n`。
def to_sse_frame(item) -> str:
    """把事件源产出的元素归一化成 SSE 帧字符串。

    - dict → `event: {type}\\ndata: {json}\\n\\n`
    - str/bytes → 原样透出（已由调用方序列化好的）
    - 其它类型 → 退化为 `data: {str}` 的一帧（不静默丢内容）
    """
    if isinstance(item, bytes):
        return item.decode("utf-8", "replace")
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        import json as _json
        et = item.get("type") or "message"
        return "event: %s\ndata: %s\n\n" % (et, _json.dumps(item, ensure_ascii=False))
    return "data: %s\n\n" % str(item)


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


def iter_with_heartbeat(iterator, interval_s: float = 15.0, comment: str = "ping",
                        visible_ping: bool = False):
    """把阻塞式同步事件源包装成「带心跳」的生成器。

    :param iterator: 原 SSE 帧字符串生成器（`f"event: x\\ndata: ...\\n\\n"`）
    :param interval_s: 无数据超过该秒数即产出一帧心跳；必须**明显小于**链路上最小的
                       idle timeout（推荐 ≤ 15 s，对应 nginx 默认 60 s 的 1/4）
    :param comment: 心跳注释内容（客户端按 SSE 协议忽略）
    :param visible_ping: True 时空闲心跳改为**可见事件帧** `event: ping`（带阶段+耗时，
                         供前端展示"仍在执行"）；False（默认）保持注释帧 `: ping`，
                         flows.py 等既有消费方行为逐字节不变。
    产出：**原帧原序输出**，中间穿插心跳帧。
    """
    q: "queue.Queue" = queue.Queue(maxsize=0)
    box: dict = {}
    stop = threading.Event()
    started = threading.Event()
    _t0 = time.monotonic()
    _last_stage = None

    def _worker():
        # 止损点必须在 **worker 线程内部**：CPython 里对"正在另一个线程栈上执行"的
        # generator 调 close() 会抛 `ValueError: generator already executing`
        #（实测复现过 —— 见 tools/verify/verify_sse_heartbeat.py 的 M4）。
        # 所以由消费侧置 stop 标志，worker 在**刚拿到一帧、源未 running 时**安全地关掉源。
        nonlocal _last_stage
        started.set()
        try:
            for frame in iterator:
                if stop.is_set():
                    try:                        iterator.close()   # 同线程调用：此刻 generator 未在执行，安全
                    except Exception:
                        pass
                    return
                # 2026-10-04：投递前归一化 —— dict 事件必须序列化成 SSE 帧，
                # 否则 Starlette 会抛 `'dict' object has no attribute 'encode'`
                # （这正是批次 1 丢掉端点里那行 json.dumps 造成的回归）
                _sf = to_sse_frame(frame)
                _s = _stage_of(_sf)          # 只解析 stage/agent 帧，其余零开销
                if _s:
                    _last_stage = _s
                q.put((False, _sf))
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
                if visible_ping:
                    yield _ping_frame(time.monotonic() - _t0, _last_stage)
                else:
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


# ══════════════════════════════════════════════════════════════════════════
# 异步版心跳包装（P0-1，2026-10-04）—— 让 SSE 长连接**不再占用线程池线程**
# ══════════════════════════════════════════════════════════════════════════
# 【问题：为什么同步 def 的 SSE 端点会拖垮并发】
# 路由写 `def`（同步）时，FastAPI 把整个端点放 anyio **线程池**执行；返回
# `StreamingResponse(同步生成器)` 后，Starlette 用 `iterate_in_threadpool` 消费它 ——
# 每产出一帧都要占用一次线程池线程。本仓实测单轮编排 344~1155 s，而线程池上限 **40**
# （anyio 默认），意味着**约 30 个并发 SSE 就会把线程池打满**，之后所有同步端点
# （包括与 SSE 无关的普通读接口）一起排队 —— 这是 P0-1「同步阻塞执行模型」的
# 最直观后果，也是本次只改 SSE、不动其余 560+ 端点的原因（性价比最高的切口）。
#
# 【解法】
# 端点改 `async def` + 返回 **async** 生成器 ⇒ Starlette 直接 `async for` 消费，
# **不碰线程池**。内部阻塞（LLM 调用）由一个自建 daemon 线程承担，通过
# `loop.call_soon_threadsafe` 把帧投递到 `asyncio.Queue`；主协程只在
# `await queue.get()` 处让出，**空闲时完全不占任何线程**。
# 心跳仍由 `asyncio.wait_for` 的超时分支产出，语义与同步版一致（15 s）。
#
# 【为什么保留同步版】
# ① flows.py 的流式执行仍用同步版（它自己 sleep(1) 轮询，改动面更大、收益相同但风险更高）；
# ② 同步版仍被 verify_sse_heartbeat 的既有断言覆盖，不因新增异步版而失去回归保护。
async def iter_with_heartbeat_async(iterator, interval_s: float = 15.0, comment: str = "ping",
                                    visible_ping: bool = False):
    """把阻塞式同步事件源包装成 **async** 生成器（带心跳）。

    与 `iter_with_heartbeat` 的差别只有一处，但很关键：消费侧不占线程池线程。
    用法：端点写成 `async def` + `return StreamingResponse(agen(), media_type="text/event-stream")`。
    `visible_ping=True` 时空闲心跳为可见事件帧（带阶段+耗时，P1-33），默认注释帧不变。
    """
    import asyncio
    import threading as _th

    q: "asyncio.Queue" = asyncio.Queue()
    loop = asyncio.get_running_loop()
    box: dict = {}
    stop = _th.Event()          # loop 关闭 / 消费端退出时，通知 worker 停止投递
    _DONE = object()
    _t0 = time.monotonic()
    _last_stage = [None]        # list 包装：worker 线程写、主协程读，避免 nonlocal 跨线程歧义

    def _worker():
        """跑在自建 daemon 线程里（不占 anyio 线程池）。"""
        try:
            for frame in iterator:
                if stop.is_set():
                    break
                # 2026-10-04：与同步版同一归一化（单一真源在 to_sse_frame）
                _sf = to_sse_frame(frame)
                _s = _stage_of(_sf)          # 只解析 stage/agent 帧，其余零开销
                if _s:
                    _last_stage[0] = _s
                try:
                    loop.call_soon_threadsafe(q.put_nowait, (False, _sf))
                except RuntimeError:
                    break
        except BaseException as e:      # noqa: BLE001 —— 含 GeneratorExit，必须回传
            box["exc"] = e
        finally:
            # ★★ 2026-10-27（用户反馈「中途执行失败后之前输出的内容没有任何展示」）：
            #   **必须在 worker 线程内关闭事件源**，这是断点保护的最后一环。
            #
            #   旧实现只在下面 async 的 `finally` 里 `iterator.close()`，而那一刻
            #   worker 正阻塞在 `iterator.__next__()`（跑 LLM）里 ⇒ 主线程 close 抛
            #   `ValueError: generator already executing` ⇒ 被 `except: pass` 吞掉
            #   ⇒ **事件源从未被真正关闭**：
            #     · 它停在某个 `yield` 处永远悬着（连 GC 都不会跑，worker 线程还引用着）；
            #     · `execute_stream` 的 `except GeneratorExit`（断点固化）**永不执行**；
            #     · 真库实证（conv 9660）：LLM 已生成 872 tokens、库里 0 条 assistant；
            #       用户报的"输出内容没有任何展示"就是这个。
            #
            #   在这里 close：此刻本线程刚跳出 for，事件源**处于挂起态**（不在执行中），
            #   `close()` 会正常把 `GeneratorExit` 注入到它的 yield 处 ⇒
            #   其 except/finally（固化正文、闭 span、回填 artifact）得以运行。
            try:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(q.put_nowait, (True, _DONE))
            except RuntimeError:
                pass                        # loop 已关，无需收尾

    _th.Thread(target=_worker, name="sse-source-async", daemon=True).start()

    try:
        while True:
            try:
                done, frame = await asyncio.wait_for(q.get(), timeout=interval_s)
            except asyncio.TimeoutError:
                if visible_ping:
                    yield _ping_frame(time.monotonic() - _t0, _last_stage[0])
                else:
                    yield ": %s\n\n" % comment
                continue
            if done:
                break
            yield frame
        exc = box.get("exc")
        if exc is not None and not isinstance(exc, GeneratorExit):
            raise exc
    finally:
        stop.set()             # 先叫停 worker，再关事件源（顺序反了会漏帧）
        # 客户端断开 → 外层关闭本生成器 → 连带把事件源关掉，避免 LLM 继续烧钱。
        # ⚠️ 2026-10-27：这里通常是**尽力而为**（worker 正在 `__next__` 里跑 LLM 时，
        #   本线程 close 会抛 `generator already executing`，被下面吞掉）。
        #   **真正生效的 close 在 `_worker` 的 finally 里**（同一线程、挂起态，必成）。
        #   两处都留：worker 已结束的场景由这里直接关，worker 仍在跑的场景由它自己关。
        try:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
