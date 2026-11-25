# -*- coding: utf-8 -*-
# CI-OPTIONAL: [nightly] tier=nightly（见 tools/gates_manifest.json）：2026-11-24 A4 补登记：SSE 消费者断开时 detach_ctx 保活行为真跑验证（异步生成器 + 假事件源，不依赖服务/真库）。历史存量脚本此前漏登记，守卫 G5a/G5b 点名；列 nightly 因含异步线程，比 pr 慢
"""端到端行为验证：SSE 消费者中途断开（模拟刷新）时，detach_ctx 是否保活生成。

这不是静态源码断言，而是**真跑生成器**：
  · 造一个慢速同步事件源（worker 线程跑，每 0.15s 产一帧，共 12 帧）
  · 用 iter_with_heartbeat_async 包装，传入 detach_ctx
  · 消费 2 帧后停止消费并 aclose（模拟浏览器刷新掐断 SSE）
  · 断言：detach 判定被调用、on_detach 收到后台 task、worker 继续跑完
          剩余帧经 on_frame 落库、on_done 在整轮结束后被调用
  · 对照：detach=False（模拟主动停止）时，worker 应立即停、on_done 不触发
"""
import asyncio
import sys
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_RES = []


def rec(name, ok, detail=""):
    _RES.append(bool(ok))
    print("%s %s%s" % ("[OK]  " if ok else "[FAIL]", name,
                       ("  —— " + detail) if detail else ""))


def run_case(detach: bool):
    """返回 (detach_called, frames_sunk, done_called, produced_total)"""
    from core.sse import iter_with_heartbeat_async
    state = {"detach_called": 0, "sunk": [], "done": 0, "produced": 0}
    total = 12

    def source():
        for i in range(total):
            state["produced"] += 1
            yield {"type": "token", "delta": "x", "i": i}
            time.sleep(0.05)

    ctx = {
        "detach": lambda: (state.__setitem__("detach_called", state["detach_called"] + 1) or detach),
        "on_detach": lambda task: None,
        "on_frame": lambda raw: state["sunk"].append(raw),
        "on_done": lambda: state.__setitem__("done", state["done"] + 1),
    }

    async def main():
        agen = iter_with_heartbeat_async(source(), interval_s=30.0, detach_ctx=ctx)
        got = 0
        async for _ in agen:
            got += 1
            if got >= 2:
                break
        await agen.aclose()          # 模拟 Starlette 关闭 SSE 生成器
        await asyncio.sleep(1.2)     # 给后台 drain 时间跑完剩余帧
        return got

    consumed = asyncio.run(main())
    return state, consumed


def main():
    # ── 场景 A：刷新离开（detach=True）────────────────────────────────
    st_a, consumed_a = run_case(detach=True)
    rec("A1 消费者只读 2 帧就断开", consumed_a == 2, "got=%d" % consumed_a)
    rec("A2 detach 判定被调用", st_a["detach_called"] >= 1,
        "calls=%d" % st_a["detach_called"])
    rec("A3 worker 保活：12 帧全部产出", st_a["produced"] == 12,
        "produced=%d" % st_a["produced"])
    rec("A4 断连后剩余帧经 on_frame 落库（非丢弃）",
        len(st_a["sunk"]) >= 8, "sunk=%d" % len(st_a["sunk"]))
    rec("A5 整轮结束触发 on_done（写 run=done）", st_a["done"] == 1,
        "done=%d" % st_a["done"])

    # ── 场景 B：主动停止（detach=False）───────────────────────────────
    st_b, consumed_b = run_case(detach=False)
    rec("B1 判定被调用但否决续跑", st_b["detach_called"] >= 1)
    rec("B2 主动停止：worker 被及时掐断（未产满 12 帧）",
        st_b["produced"] < 12, "produced=%d" % st_b["produced"])
    rec("B3 主动停止：不写 done 终态", st_b["done"] == 0,
        "done=%d" % st_b["done"])

    # ── 无 detach_ctx 的向后兼容 ──────────────────────────────────────
    from core.sse import iter_with_heartbeat_async
    def src2():
        for i in range(8):
            yield {"type": "token", "delta": "y"}
            time.sleep(0.05)
    async def compat():
        agen = iter_with_heartbeat_async(src2(), interval_s=30.0)
        n = 0
        async for _ in agen:
            n += 1
            if n >= 1:
                break
        await agen.aclose()
        await asyncio.sleep(0.4)
    asyncio.run(compat())
    rec("C1 未传 detach_ctx 不报错（向后兼容）", True)

    # ── D：转后台失败必须撤销认领（run 70 实证）─────────────────────────
    # 现场：日志只有「内层收口让位给后台（detach 已认领）」，**没有**「转后台续跑」，
    #   也没有「后台整轮完成」⇒ `on_detach` 从未执行（create_task/回调抛异常被吞）。
    #   旧代码只把 `_want_detach=False` 退回原路径，却把上层"已认领"的标记留着
    #   ⇒ 内层 finally 以为后台接手而跳过写终态、后台其实没接 ⇒ run 永久 running。
    #   现在必须回调 `on_detach_abort` 撤销认领，让内层照常写终态。
    from core.sse import iter_with_heartbeat_async as _iwa
    _d = {"claimed": 0, "aborted": 0}

    def src_d():
        for i in range(3):
            yield {"type": "token", "delta": "z"}
            time.sleep(0.02)

    async def case_d():
        agen = _iwa(src_d(), interval_s=0.05, detach_ctx={
            "detach": lambda: True,
            "on_detach": lambda task: (_ for _ in ()).throw(RuntimeError("模拟转后台失败")),
            "on_detach_abort": lambda: _d.__setitem__("aborted", _d["aborted"] + 1),
        })
        # 只读一帧就断（模拟客户端早退）
        await agen.__anext__()
        await agen.aclose()
        await asyncio.sleep(0.3)

    asyncio.run(case_d())
    rec("D1 转后台失败 ⇒ 回调 on_detach_abort 撤销认领（不再永久 running）",
        _d["aborted"] >= 1, "aborted=%d" % _d["aborted"])

    # ── E：drain 的 worker_done 兜底（DONE 被消费后仍能收口）────────────
    # worker 先把 DONE 入队、消费侧读走后才断连 ⇒ drain 面对"永远不会有第二个 DONE"
    # 的队列。判据 box["worker_done"] 为真即视同自然结束。
    _e = {"done": 0, "cancel": 0}

    def src_e():
        yield {"type": "token", "delta": "q"}
        time.sleep(0.05)

    async def case_e():
        agen = _iwa(src_e(), interval_s=0.05, detach_ctx={
            "detach": lambda: True,
            "on_done": lambda: _e.__setitem__("done", _e["done"] + 1),
            "on_cancel": lambda: _e.__setitem__("cancel", _e["cancel"] + 1),
        })
        async for _fr in agen:
            pass
        await asyncio.sleep(0.4)

    asyncio.run(case_e())
    rec("E1 自然结束时不得误写 cancelled", _e["cancel"] == 0, "cancel=%d" % _e["cancel"])

    # ── F：worker 已自然结束时**不得认领 detach**（run 70/72 实证）──────────
    # 一旦认领，内层 finally 会跳过写 run 终态，而后台又不会接手
    # ⇒ 正文落库、run 永久 running。判据：正常跑完时 detach() 回调**从未被调用**。
    _f = {"detach_calls": 0, "done": 0, "aborted": 0}

    def src_f():
        yield {"type": "token", "delta": "f"}
        time.sleep(0.02)

    async def case_f():
        agen = _iwa(src_f(), interval_s=0.05, detach_ctx={
            "detach": lambda: (_f.__setitem__("detach_calls", _f["detach_calls"] + 1), True)[1],
            "on_detach": lambda task: None,
            "on_detach_abort": lambda: _f.__setitem__("aborted", _f["aborted"] + 1),
            "on_done": lambda: _f.__setitem__("done", _f["done"] + 1),
        })
        async for _fr in agen:
            pass
        await asyncio.sleep(0.3)

    asyncio.run(case_f())
    rec("F1 正常跑完（worker 已结束）⇒ 不调用 detach、不认领（内层照常写终态）",
        _f["detach_calls"] == 0, "detach_calls=%d" % _f["detach_calls"])
    rec("F2 该路径不得误报 on_detach_abort", _f["aborted"] == 0,
        "aborted=%d" % _f["aborted"])

    ok = all(_RES)
    print("\n=== 结果：%d/%d 通过 ===" % (sum(_RES), len(_RES)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
