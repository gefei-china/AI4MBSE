# -*- coding: utf-8 -*-
"""P0-1 真实并发压测：同步 vs 异步 SSE 端点的**线程占用**对比（2026-10-04）。

**为什么必须实测而不靠推理**：把 `def` 改成 `async def` 的收益是"消费侧不占线程池
线程"，这是个可以推理的结论，但推理容易漏掉"端点体本身在 event loop 上跑同步 SQLite"
这类反向代价。**只有压测能给出真实数字。**

**测法（尽量少受其他因素干扰）**：
  1) 同一个进程内起 uvicorn，分别挂两个等价端点：
       /bench/sync  —— 同步 def + 同步 iter_with_heartbeat（改造前的形态）
       /bench/async —— async def + 异步 iter_with_heartbeat_async（改造后的形态）
     两个端点的**事件源完全相同**（每帧 sleep 的同步生成器），唯一差别是消费侧走哪儿。
  2) 各并发压 N 个长连接，**按线程名**统计服务端线程构成。
  3) ⚠️ **判据不是"总线程数"** —— 第一版压测就栽在这里：异步版也是"每连接 1 个自建
     daemon 线程"，所以**两版总线程数几乎相同**（实测 62 vs 62），据此下结论必然是
     "没区别"。真正的差别是**占的是哪套线程**：
       · 同步版占用 **anyio 线程池**（线程名 `AnyIO worker thread`）——
         那是**全站共享**的 40 个配额，占满后所有同步端点（含与 SSE 无关的普通读接口）一起排队；
       · 异步版占用**自建 daemon 线程**（线程名 `sse-source-async`）—— 不参与 anyio 竞争。
     ⇒ 判据 = `anyio_pool_used`。同步版应 ≈ N，异步版应 ≈ 0。

**顺带**：首轮压测还抓出异步包装器一个真实缺陷 —— 连接被取消、event loop 已关闭后，
worker 线程仍调 `call_soon_threadsafe` ⇒ `RuntimeError: Event loop is closed` 持续刷屏。
已修（stop 事件 + 捕获 RuntimeError）。

用法：
    .venv\\Scripts\\python.exe -X utf8 tools/verify/bench_sse_concurrency.py
环境变量：BENCH_N（并发数，默认 30）BENCH_HOLD（保持秒数）BENCH_DELAY（每帧阻塞秒）
"""
import asyncio
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ANYIO_NAMES = ("AnyIO worker thread", "AnyIO worker thread N")


def blocking_source(n_frames, delay, tag):
    """模拟 LLM 阻塞：每帧之间 sleep（等价于一次慢 LLM 调用）。"""
    for i in range(n_frames):
        time.sleep(delay)
        yield "event: token\ndata: {\"i\":%d,\"tag\":\"%s\"}\n\n" % (i, tag)
    yield "event: done\ndata: {}\n\n"


async def run_case(kind, n_clients, n_frames, delay, hold):
    """起 n_clients 个长连接，保持 hold 秒，统计峰值占用。"""
    import httpx
    from core.sse import iter_with_heartbeat, iter_with_heartbeat_async

    url = "http://127.0.0.1:8791/bench/%s" % kind
    limits = httpx.Limits(max_connections=n_clients + 10, max_keepalive_connections=n_clients + 10)
    got = {"frames": 0, "err": None}

    async def one(client, idx):
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    got["err"] = "HTTP %s" % resp.status_code
                    return
                async for _ in resp.aiter_lines():
                    got["frames"] += 1
        except Exception as e:                      # noqa: BLE001
            got["err"] = "%s: %s" % (type(e).__name__, str(e)[:80])

    async with httpx.AsyncClient(timeout=hold + 30, limits=limits) as client:
        tasks = [asyncio.create_task(one(client, i)) for i in range(n_clients)]
        await asyncio.sleep(hold)                     # 保持连接，观察峰值
        alive = sum(1 for t in tasks if not t.done())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return alive, got


async def serve_one(kind, n_clients, n_frames, delay, hold, port):
    """**独立起停**一个只挂单个端点的服务，测完立即关掉。

    为什么必须每轮独立（第二版踩的坑）：anyio 的 worker 线程**空闲不回收**，
    上一轮压测留下的 30 个会被下一轮计入 ⇒ 异步版被测出"anyio 占用 30"的假象，
    结论完全错误。**判据一旦被上一轮污染，数字再漂亮也不能用。**
    """
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from starlette.responses import StreamingResponse
    from core.sse import iter_with_heartbeat, iter_with_heartbeat_async

    app = FastAPI()

    if kind == "sync":
        @app.get("/bench/stream")
        def bench_sync():
            def gen():
                for f in iter_with_heartbeat(blocking_source(n_frames, delay, "sync"),
                                             interval_s=2.0):
                    yield f
            return StreamingResponse(gen(), media_type="text/event-stream")
    else:
        @app.get("/bench/stream")
        async def bench_async():
            async def agen():
                af = iter_with_heartbeat_async(blocking_source(n_frames, delay, "async"),
                                               interval_s=2.0)
                async for f in af:
                    yield f
            return StreamingResponse(agen(), media_type="text/event-stream")

    @app.get("/bench/threads")
    def threads():
        import collections
        names = [t.name for t in threading.enumerate()]
        return {"total": len(names),
                "anyio": sum(1 for n in names if n.startswith("AnyIO")),
                # ⚠️ 两个 startswith 有包含关系（"sse-source" ⊃ "sse-source-async"），
                #    **必须先判长的**，否则 async 线程会被重复计进 sse_sync 里（第一版就踩了）。
                "sse_async": sum(1 for n in names if n.startswith("sse-source-async")),
                "sse_sync": sum(1 for n in names if n.startswith("sse-source")
                                and not n.startswith("sse-source-async"))}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base_url = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                if (await c.get(base_url + "/bench/threads")).status_code == 200:
                    break
        except Exception:
            await asyncio.sleep(0.25)
    else:
        server.should_exit = True
        return None

    async with httpx.AsyncClient(timeout=5) as c:
        base = (await c.get(base_url + "/bench/threads")).json()

    # 压测：起 N 个连接，保持 hold 秒，中途不 cancel（保持"连接活着"这个前提）
    limits = httpx.Limits(max_connections=n_clients + 10, max_keepalive_connections=n_clients + 10)
    got = {"frames": 0, "err": None}

    async def one(client):
        try:
            async with client.stream("GET", base_url + "/bench/stream") as resp:
                if resp.status_code != 200:
                    got["err"] = "HTTP %s" % resp.status_code
                    return
                async for _ in resp.aiter_lines():
                    got["frames"] += 1
        except Exception as e:                      # noqa: BLE001
            got["err"] = "%s: %s" % (type(e).__name__, str(e)[:60])

    async with httpx.AsyncClient(timeout=hold + 60, limits=limits) as client:
        tasks = [asyncio.create_task(one(client)) for _ in range(n_clients)]
        await asyncio.sleep(hold)
        alive = sum(1 for t in tasks if not t.done())
        async with httpx.AsyncClient(timeout=5) as c2:
            snap = (await c2.get(base_url + "/bench/threads")).json()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    server.should_exit = True
    await asyncio.sleep(0.8)
    return {"base": base, "snap": snap, "alive": alive, "got": got}


def main():
    n_clients = int(os.environ.get("BENCH_N", "30"))
    n_frames = int(os.environ.get("BENCH_FRAMES", "40"))
    delay = float(os.environ.get("BENCH_DELAY", "0.5"))
    hold = float(os.environ.get("BENCH_HOLD", "6"))

    async def run_all():
        print("=" * 74)
        print("P0-1 真实并发压测：同步 vs 异步 SSE（各 %d 并发，事件源每帧阻塞 %.1fs，保持 %ds）"
              % (n_clients, delay, hold))
        print("=" * 74)
        print("判据 = **anyio 线程池增量**（那才是全站共享的 40 个配额）")
        print("⚠️ 每轮**独立起停服务**：anyio worker 线程空闲不回收，会污染下一轮的读数。\n")
        rows = {}
        for kind, port in (("async", 8792), ("sync", 8791)):
            r = await serve_one(kind, n_clients, n_frames, delay, hold, port)
            if r is None:
                print("  [%s] 服务未能就绪，跳过" % kind)
                continue
            b, s = r["base"], r["snap"]
            rows[kind] = (s["anyio"] - b["anyio"], s["total"] - b["total"],
                          s["sse_sync"], s["sse_async"], r["alive"], r["got"])
            print("  【%s】存活连接=%d  总线程 +%d  **anyio 池 +%d**  自建 sync 线程=%d  自建 async 线程=%d"
                  % (kind, r["alive"], rows[kind][1], rows[kind][0], rows[kind][2], rows[kind][3]))
            if r["got"].get("err"):
                print("     错误：%s" % r["got"]["err"])
            print()
        if "sync" not in rows or "async" not in rows:
            print("⇒ 两轮未都完成，**不可据此下结论**")
            return 1
        s_pool, a_pool = rows["sync"][0], rows["async"][0]
        print("=" * 74)
        print("  同步版 anyio 池 +%d    异步版 anyio 池 +%d" % (s_pool, a_pool))
        if s_pool >= n_clients * 0.8 and a_pool <= max(2, n_clients * 0.1):
            print("⇒ **验证通过**：同步版每个 SSE 连接占 1 个 anyio 线程（本仓上限 40 ⇒ 约 %d 个并发就打满），"
                  % min(max(s_pool, 1), 40))
            print("   异步版几乎不占 anyio 池（阻塞由自建 daemon 线程承担）⇒ 不再挤占全站同步端点。")
            return 0
        print("⇒ 结论不符合预期（sync +%d / async +%d），不可据此下结论，需排查" % (s_pool, a_pool))
        return 1

    return asyncio.run(run_all())


if __name__ == "__main__":
    sys.exit(main())
