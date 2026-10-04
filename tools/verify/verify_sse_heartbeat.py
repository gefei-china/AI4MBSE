# -*- coding: utf-8 -*-
"""P1-2（SSE 心跳保活）与 P1-3（降级显式化）回归验证 —— 零 LLM、零库写入，可反复跑。

**为什么需要它**（《架构-可扩展性-稳定性整体评估》§4.3 / §6 P1-2、P1-3 实测）：

    P1-2 —— SSE 无心跳、断流不可续：
        流式端点 `def chat_stream` 是**同步生成器**，一次 `next()` 就是一次完整 LLM/工具调用，
        实测单轮编排 344 s / 849 s / 1155 s，等待首 token 期间**零字节输出**。
        nginx `proxy_read_timeout` 默认 60 s、LB idle timeout 常见 60~120 s ⇒ 静默掐断；
        且本工程断流不可续（无 Last-Event-ID），用户看到的就是"回答一半卡住然后报错"。
        标杆做法（OpenAI streaming / Dify workflow SSE / Vercel AI SDK 一致）：
        **周期性注释帧 `: ping\\n\\n` + 首帧 `retry: <ms>`**，回首帧的同时告诉客户端重连节奏。

    P1-3 —— 外部依赖静默降级为 Mock：
        DeepSeek 402 余额不足 / 无 api_key / 调用异常三条路径都**静默回落 Mock**，
        用户看到的是"正常返回"，内容其实是假答案。对照 Manus/Dify：模型不可用或
        回退到非预期实现属**必须可见**的事件，不能让用户拿着假产出当结论。

本脚本锁定的不变式：
    I1 事件源长时间静默 ⇒ 必须按间隔产出心跳帧（不是等业务数据来才动）
    I2 心跳**不破坏**业务帧的顺序与内容（原帧原序、一字节不改）
    I3 事件源抛异常 ⇒ 包装层必须把它**重新抛出**（禁止吞进线程里）
    I4 客户端断开（模拟 StreamingResponse 关闭）⇒ 不得抛 "generator ignored GeneratorExit"
    I5 心跳帧必须是 SSE 注释帧（`: xxx\\n\\n`），不得带 `data:`/业务语义；且不得被
       前端 SSE 解析器误解析（校验 11-pipeline.js:handleSSE 的解析口径即可忽略）
    I6 接线因果：降级累计计数增加 ⇒ 流式必须产出 `event: degraded` 帧
    I7 接线存在性：chat_stream 首帧必须声明 `retry:`，心跳间隔必须 < nginx 默认 60 s

**变异自证（强制）**：把"修复前的写法"还原，脚本必须判 FAIL/VACUOUS。
   工程纪律（skill §6.2）：能抓住旧写法才算数。
   本脚本内置 3 组变异，任一未被抓住 → VACUOUS 且 rc=1。

用法（**裸跑自身即完整口径**）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_sse_heartbeat.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SSE_SRC = os.path.join(ROOT, "core", "sse.py")
CONV_SRC = os.path.join(ROOT, "routers", "conversations.py")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def _load_sse(mutate=None):
    """从源码**就地加载** core/sse.py（可选注入变异）。

    为什么不用常规的 `from core.sse import ...`：那样会拿到 site 缓存的字节码，
    无法做源码级变异；而"手写一份等价实现来测"会与被测代码脱钩（工程教训 2026-09-30，
    曾导致变异测试假绿）。这里必须 exec 真实源码文本。
    """
    src = open(SSE_SRC, encoding="utf-8").read()
    if mutate:
        old, new, anchor = mutate
        assert anchor in src, f"变异锚点丢失：{anchor}"
        src = src.replace(old, new, 1)
    ns = {"__name__": "core.sse"}
    exec(compile(src, "core/sse.py", "exec"), ns)
    return ns


def _slow_source(frames, gap_s=0.35):
    """模拟"长时间思考"的事件源：产完既定帧后 sleep 超过心跳间隔。"""

    def gen():
        for f in frames:
            yield f
        time.sleep(gap_s)          # 静默窗口
        yield "event: late\ndata: {}\n\n"
    return gen()


# ══════════════════ I1：静默期必须心跳 ══════════════════
def t_i1():
    print("\n=== I1 事件源静默 ⇒ 必须产出心跳帧 ===")
    ns = _load_sse()
    frames = ["event: stage\ndata: {}\n\n", "event: token\ndata: {}\n\n"]
    t0 = time.time()
    out = list(ns["iter_with_heartbeat"](_slow_source(frames, 0.5), interval_s=0.05))
    dt = time.time() - t0
    beats = [f for f in out if f.startswith(":")]
    ok = _rec("I1 静默 0.5s/间隔 0.05s 至少产出 3 帧心跳", len(beats) >= 3, f"got={len(beats)}")
    ok &= _rec("I1 心跳未把总时长拖长（≤ 静默+缓冲）", dt < 2.0, f"elapsed={dt:.2f}s")
    return ok


def m_i1():
    """变异：把心跳间隔改成极大值（等价于"没有心跳"的旧写法）→ 心跳必须消失。"""
    print("\n=== 变异 M1：还原『等于没有心跳』的旧写法 ===")
    ns = _load_sse(mutate=("interval_s: float = 15.0", "interval_s: float = 9999.0",
                           "interval_s: float = 15.0"))
    out = list(ns["iter_with_heartbeat"](_slow_source(["event: stage\ndata: {}\n\n"], 0.3)))
    caught = len([f for f in out if f.startswith(":")]) == 0
    return _rec("M1 旧写法复现（心跳消失）→ 证明 I1 非空转", caught, "", VACUOUS)


# ══════════════════ I2：业务帧原序保真 ══════════════════
def t_i2():
    print("\n=== I2 业务帧顺序与内容零失真 ===")
    ns = _load_sse()
    frames = [f"event: t{i}\ndata: {{\"i\":{i}}}\n\n" for i in range(5)]
    out = [f for f in ns["iter_with_heartbeat"](iter(frames), interval_s=999)]
    return _rec("I2 业务帧原序原样输出", out == frames, f"got={len(out)} vs {len(frames)}")


# ══════════════════ I3：异常必须回传 ══════════════════
def t_i3():
    print("\n=== I3 事件源异常 ⇒ 包装层重新抛出（禁止吞进线程）===")

    def gen():
        yield "event: stage\ndata: {}\n\n"
        raise RuntimeError("boom-from-source")

    ns = _load_sse()
    got = []
    raised = None
    try:
        for f in ns["iter_with_heartbeat"](gen(), interval_s=0.05):
            got.append(f)
    except RuntimeError as e:
        raised = e
    return _rec("I3 源异常被重新抛出且已产出的帧不丢",
                raised is not None and "boom-from-source" in str(raised) and len(got) >= 1,
                f"raised={raised} frames={len(got)}")


def m_i3():
    """变异：worker 吃掉异常（不写 box）→ I3 必须抓不到（证明断言盯着这条路径）。"""
    print("\n=== 变异 M3：异常被线程吞掉（旧写法）===")
    ns = _load_sse(mutate=('box["exc"] = e', "e = e  # 变异：吞掉异常", 'box["exc"] = e'))

    def gen():
        yield "event: stage\ndata: {}\n\n"
        raise RuntimeError("boom")

    raised = None
    try:
        list(ns["iter_with_heartbeat"](gen(), interval_s=0.05))
    except RuntimeError as e:
        raised = e
    return _rec("M3 旧写法复现（异常消失）→ 证明 I3 非空转", raised is None, "", VACUOUS)


# ══════════════════ I4：客户端断开不得抛 ignored GeneratorExit ══════════════════
def t_i4():
    print("\n=== I4 客户端断连 ⇒ 干净关闭且事件源被收尾 ===")
    ns = _load_sse()
    closed = {"v": False}

    def gen():
        try:
            for i in range(100000):
                yield f"event: t\ndata: {{\"i\":{i}}}\n\n"
                time.sleep(0.005)
        finally:
            closed["v"] = True       # GeneratorExit → finally 触发 = 源被真正收尾

    def gen_no_guard():              # 无 try/finally 无法观测 → 生成器本身即看得出来
        for i in range(100000):
            yield f"event: t\ndata: {i}\n\n"
            time.sleep(0.005)

    it = ns["iter_with_heartbeat"](gen(), interval_s=0.05)
    next(it)
    err = None
    try:
        it.close()          # 等价于 Starlette 在客户端断开后关闭响应生成器
    except Exception as e:  # noqa: BLE001
        err = e
    time.sleep(0.25)        # 给 worker 线程一个帧边界来做止损
    ok = _rec("I4a close() 不抛异常", err is None, f"err={err}")
    ok &= _rec("I4b 客户端断开后事件源被收尾（GeneratorExit 生效）",
               closed["v"] is True, "源仍在跑 ⇒ 用户关页面后 LLM 继续烧")
    # 反向：未断开时不得误伤（源要能正常跑完）
    out = list(ns["iter_with_heartbeat"](iter(["a\n\n", "b\n\n"]), interval_s=999))
    ok &= _rec("I4c 未断开时不得提前收尾（正常流完整输出）", out == ["a\n\n", "b\n\n"])
    return ok


def m_i4():
    """变异：抽掉 worker 内的止损分支 → 断连后事件源永远不会被收尾（must-fail）。"""
    print("\n=== 变异 M4：断连后不关闭事件源（旧写法）===")
    ns = _load_sse(mutate=("if stop.is_set():", "if False:", "if stop.is_set():"))
    closed = {"v": False}

    def gen():
        try:
            for i in range(100000):
                yield f"event: t\ndata: {i}\n\n"
                time.sleep(0.005)
        finally:
            closed["v"] = True

    it = ns["iter_with_heartbeat"](gen(), interval_s=0.05)
    next(it)
    try:
        it.close()
    except Exception:
        pass
    time.sleep(0.25)
    caught = (closed["v"] is False)   # 旧写法：源没被收尾
    return _rec("M4 旧写法复现（源未被收尾）→ 证明 I4b 非空转", caught, "", VACUOUS)


# ══════════════════ I5：心跳必须是合规注释帧 ══════════════════
def t_i5():
    print("\n=== I5 心跳帧 = SSE 注释帧，且前端解析器会忽略 ===")
    ns = _load_sse()
    out = list(ns["iter_with_heartbeat"](
        _slow_source(["event: stage\ndata: {}\n\n"], 0.3), interval_s=0.05))
    beats = [f for f in out if f.startswith(":")]
    ok = True
    for b in beats:
        ok &= b.startswith(":") and b.endswith("\n\n") and "data:" not in b
    ok = _rec("I5 心跳帧格式合规（`: xxx\\n\\n`，无 data:）", ok, f"sample={beats[:1]}")

    # 前端 handleSSE 解析口径复算（直接复用其规则，避免"看着像就放行"）
    def js_parse(raw):
        ev, data = "message", ""
        for line in raw.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        return ev, data

    for b in beats:
        ev, data = js_parse(b.strip())
        ok &= (data == "")
    ok &= _rec("I5 心跳帧经前端 parser 后 data 为空（不触发任何事件分支）", bool(beats))
    return ok


# ══════════════════ I6 / I7：接线因果与存在性 ══════════════════
def t_i6_i7():
    print("\n=== I6/I7 端点接线：retry 首帧 + degraded 事件 + 间隔合理性 ===")
    src = open(CONV_SRC, encoding="utf-8").read()
    ok = _rec("I7a 首帧声明 SSE 重连间隔 `retry:`", '"retry: 3000' in src or "'retry: 3000" in src)
    ok &= _rec("I7b 使用心跳包装器 iter_with_heartbeat",
               "iter_with_heartbeat" in src and "core.sse" in src)
    ok &= _rec("I7c 心跳间隔 ≤ 15s（< nginx proxy_read_timeout 默认 60s 的 1/4）",
               "interval_s=15.0" in src)
    ok &= _rec("I6a 流收尾补发 `event: degraded`", "event: degraded" in src)
    ok &= _rec("I6b degraded 判据走累计计数差值（非易被并发覆盖的瞬时值）",
               "degrade_delta as _delta" in src and "_degrade_base = _degrade_snapshot()" in src)
    # 断连路径：禁止在 GeneratorExit 后继续 yield
    ok &= _rec("I4b 断连后不再 yield（_client_gone 闸门）", "_client_gone" in src)
    return ok


def t_i6_runtime():
    """I6 运行时因果：直接执行 `core.sse` 的判定函数（源码同一份，零 LLM）。"""
    print("\n=== I6 运行时：降级计数增长 ⇒ degraded 判定为真 ===")
    import re
    src = open(SSE_SRC, encoding="utf-8").read()
    m = re.search(r"\ndef snapshot\(stats: dict\) -> dict:\n(.*?)\ndef iter_with_heartbeat",
                  src, re.S)
    if not m:
        return _rec("抽取 core.sse 判定函数", False, "未匹配到源码段")
    ns = {"max": max, "int": int}
    exec(compile("def snapshot(stats: dict) -> dict:\n" + m.group(1), "<sse-core>", "exec"), ns)

    now = {"mock": 7, "real": 3, "fallback_total": 2}
    ok = _rec("I6a snapshot 只取累计计数",
              ns["snapshot"](now) == {"mock": 7, "real": 3, "fallback": 2})
    d = ns["degrade_delta"](ns["snapshot"]({"mock": 5, "real": 3, "fallback_total": 0}),
                            ns["snapshot"](now))
    ok &= _rec("I6b 差值正确（mock 2 / fallback 2）",
               d == {"mock_calls": 2, "real_calls": 0, "fallback_calls": 2}, f"got={d}")
    d0 = ns["degrade_delta"](ns["snapshot"](now), ns["snapshot"](now))
    ok &= _rec("I6c 无降级不误报（全 0）",
               d0 == {"mock_calls": 0, "real_calls": 0, "fallback_calls": 0}, f"got={d0}")
    # 计数重置/负数防御：不得让 UI 显示负次数
    dn = ns["degrade_delta"](ns["snapshot"]({"mock": 9}), ns["snapshot"]({"mock": 1}))
    ok &= _rec("I6d 计数重置时不为负（取 0）", dn["mock_calls"] == 0, f"got={dn}")
    return ok


def m_i6():
    """变异：让 degraded 判定恒不成立（还原"静默降级"旧行为）→ I6b 必须抓不到差值。"""
    print("\n=== 变异 M6：degrade_delta 恒 0（还原静默降级）===")
    src = open(SSE_SRC, encoding="utf-8").read()
    mutated = src.replace('return {"mock_calls": max(0,', 'return {"mock_calls": 0 * max(0,', 1)
    ns = {}
    exec(compile(mutated, "core/sse.py<mut6>", "exec"), ns)
    d = ns["degrade_delta"]({"mock": 5, "real": 3, "fallback": 0},
                            {"mock": 7, "real": 3, "fallback": 2})
    return _rec("M6 旧写法复现（降级不可见）→ 证明 I6b 非空转",
                d["mock_calls"] == 0, "", VACUOUS)


def t_i8_async():
    """P0-1（2026-10-04）：主 SSE 端点必须是 async，否则长连接会占住全站 anyio 线程池。

    真实压测数字（`tools/verify/bench_sse_concurrency.py`，30 并发、事件源每帧阻塞 0.8s）：
        同步版：anyio 线程池 **+30**（+ 自建 30 = 总 +60）
        异步版：anyio 线程池 **+0**  （+ 自建 30 = 总 +30）
    本仓 anyio 上限 40 ⇒ 同步形态下约 30 个并发 SSE 就打满线程池，
    **连与 SSE 无关的普通读接口也一起排队**。
    """
    print("\n=== I8 P0-1：主 SSE 端点异步化（不占 anyio 线程池）===")
    import inspect
    import os
    conv = os.path.join(ROOT, "routers", "conversations.py")
    src = open(conv, encoding="utf-8").read()
    ok = _rec("I8a 主端点 chat_stream 定义为 async def",
              "async def chat_stream(" in src,
              "同步 def 会让每帧占用一次 anyio 线程池线程")
    try:
        sys.path.insert(0, ROOT)
        from routers.conversations import chat_stream
        ok &= _rec("I8b 运行时确认 iscoroutinefunction", inspect.iscoroutinefunction(chat_stream) is True)
    except Exception as e:
        ok &= _rec("I8b 运行时确认 iscoroutinefunction", False, str(e)[:80])
    ok &= _rec("I8c 端点使用异步心跳包装器", "iter_with_heartbeat_async(" in src)
    ok &= _rec("I8d 消费方式为 async for", "async for raw in _frames:" in src)
    ok &= _rec("I8e 断连收尾用 aclose（async 生成器没有 close）",
               "await _frames.aclose()" in src)
    sse = open(SSE_SRC, encoding="utf-8").read()
    ok &= _rec("I8f 异步包装器已导出", "iter_with_heartbeat_async" in
               (sse.split("__all__")[1].split("]")[0] if "__all__" in sse else ""))
    ok &= _rec("I8g 异步包装器有 stop 事件（loop 关闭后 worker 必须停，否则 RuntimeError 刷屏）",
               "stop = _th.Event()" in sse and "stop.is_set()" in sse and "stop.set()" in sse)
    ok &= _rec("I8h call_soon_threadsafe 的 RuntimeError 被捕获（实测会抛 Event loop is closed）",
               "except RuntimeError" in sse)
    ok &= _rec("I8i 同步版仍保留（flows.py 在用，勿删）", "def iter_with_heartbeat(" in sse)
    return ok


def m_i8():
    """M7：把主端点改回同步 `def` ⇒ I8a/b 必须判红。"""
    print("\n=== 变异 M7：主端点退回同步 def ===")
    mutated = open(os.path.join(ROOT, "routers", "conversations.py"),
                   encoding="utf-8").read().replace(
        "async def chat_stream(", "def chat_stream(", 1)
    ok = ("async def chat_stream(" in
          open(os.path.join(ROOT, "routers", "conversations.py"), encoding="utf-8").read()
          and "async def chat_stream(" not in mutated)
    return _rec("M7 变异被复现（端点已非 async）→ 证明 I8a/b 非空转", ok, "", VACUOUS)


def t_i9_regression():
    """I9 **回归门禁**：包装器必须把 dict 事件序列化成 SSE 帧（补批次 1 的真实回归）。

    2026-10-04 真机验证抓到：批次 1 改用 iter_with_heartbeat 后，端点里的
    `yield f"event: {ev['type']}\\ndata: {json.dumps(ev)}\\n\\n"` 被简化成 `yield raw`，
    而事件源产出的是 **dict** ⇒ Starlette 抛
    `AttributeError: 'dict' object has no attribute 'encode'`
    ⇒ **主功能 /chat/stream 自批次 1 起就是坏的**，直到真机打这个端点才暴露。

    为什么之前没抓到：门禁只喂**字符串源**给包装器（`_slow_source` 产字符串），
    真机 UI 验证又只验了恢复提示条、没打这个端点。
    ⇒ 这条断言的价值是**把"事件源是 dict"这个真实前提写进门禁**。
    """
    print("\n=== I9 回归门禁：dict 事件必须被序列化为 SSE 帧 ===")
    import importlib.util
    spec = importlib.util.spec_from_file_location("_sse_probe", SSE_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    to_frame = mod.to_sse_frame

    ok = _rec("I9a dict 事件 → event/data 两行 SSE 帧",
              to_frame({"type": "token", "delta": "hi"}) == 'event: token\ndata: {"type": "token", "delta": "hi"}\n\n',
              repr(to_frame({"type": "token", "delta": "hi"})))
    ok &= _rec("I9b 已序列化的字符串原样透出（flows.py 链路不变）",
               to_frame("event: ping\ndata: {}\n\n") == "event: ping\ndata: {}\n\n")
    ok &= _rec("I9c bytes 被解码", to_frame(b"event: x\n\n") == "event: x\n\n")
    ok &= _rec("I9d 未知类型不静默丢内容", "data: " in to_frame(123))
    ok &= _rec("I9e 非 ASCII 正确保留（不转义成 \\u）",
               "负载" in to_frame({"type": "token", "delta": "负载"}))

    # 端到端：喂 dict 源给**同步包装器**，断言产出可被 Starlette 直接使用
    def dict_source():
        yield {"type": "stage", "name": "生成与校验", "status": "run"}
        yield {"type": "token", "delta": "你好"}
        yield {"type": "done", "data": {"ok": True}}
    ns = {}
    exec(compile(open(SSE_SRC, encoding="utf-8").read(), SSE_SRC, "exec"), ns)
    out = list(ns["iter_with_heartbeat"](dict_source(), interval_s=5.0))
    ok &= _rec("I9f 同步包装器：dict 源 → 全部产出为 str",
               out and all(isinstance(x, str) for x in out), f"types={[type(x).__name__ for x in out[:3]]}")
    ok &= _rec("I9g 同步包装器：首帧含 event: stage", out and out[0].startswith("event: stage"),
               repr(out[0][:40]) if out else "empty")
    ok &= _rec("I9h 同步包装器：token 帧内容完整", any("你好" in x for x in out))

    # 异步包装器同样要过（真机 async 端点就靠它）
    import asyncio

    async def _run():
        return [x async for x in mod.iter_with_heartbeat_async(dict_source(), interval_s=5.0)]
    out2 = asyncio.run(_run())
    ok &= _rec("I9i 异步包装器：dict 源 → 全部产出为 str",
               out2 and all(isinstance(x, str) for x in out2),
               f"types={[type(x).__name__ for x in out2[:3]]}")
    ok &= _rec("I9j 异步包装器：token 帧内容完整", any("你好" in x for x in out2))
    return ok


def m_i9():
    """M8：还原成"直接透传 dict"（批次 1 的写法）⇒ I9a/f/i 必须判红。"""
    print("\n=== 变异 M8：包装器直接透传 dict（批次 1 的真实写法）===")
    ns = {}
    exec(compile(open(SSE_SRC, encoding="utf-8").read(), SSE_SRC, "exec"), ns)
    orig = ns["to_sse_frame"]
    ns["to_sse_frame"] = lambda item: item          # 变异：不做归一化

    def dict_source():
        yield {"type": "token", "delta": "hi"}
    out = list(ns["iter_with_heartbeat"](dict_source(), interval_s=5.0))
    leaked = [x for x in out if isinstance(x, dict)]
    ok = (len(leaked) > 0) and (orig({"type": "token"}) != {"type": "token"})
    return _rec("M8 变异被复现（dict 泄漏到 SSE 帧）→ 证明 I9a/f 非空转", ok, f"leaked={len(leaked)}", VACUOUS)


def main():
    print("=" * 78)
    print("P1-2 SSE 心跳 / P1-3 降级显式化 / P0-1 SSE 异步化 —— 不变式 + 变异自证")
    print("=" * 78)
    t_i1()
    m_i1()
    t_i2()
    t_i3()
    m_i3()
    t_i4()
    m_i4()
    t_i5()
    t_i6_i7()
    t_i6_runtime()
    m_i6()
    t_i8_async()
    m_i8()
    t_i9_regression()
    m_i9()

    n_fail = sum(1 for k, _, _ in _results if k == FAIL)
    n_vac = sum(1 for k, _, _ in _results if k == VACUOUS)
    print("\n" + "=" * 78)
    print(f"合计 {len(_results)} 项：PASS {len(_results)-n_fail-n_vac} / FAIL {n_fail} / VACUOUS {n_vac}")
    if n_vac:
        print("❌ 存在未被变异复现的断言（空转）")
    print("=" * 78)
    return 1 if (n_fail or n_vac) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
