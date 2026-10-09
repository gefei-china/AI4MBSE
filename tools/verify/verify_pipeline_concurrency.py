# -*- coding: utf-8 -*-
"""verify_pipeline_concurrency —— 防「多用户并发下Pipeline 状态互相覆盖」的门禁。

## 背景（2026-10-09，米爸确认「支持多用户并发」）

`AgentPipeline` 把请求级中间产物挂在 `self` 上（19 个 `self._xxx` 字段）。
对话入口原为 `from agent import agent`（**模块级单例**）⇒ 全部用户全部请求共用一个实例。

### 实测数据（`tmp/probe_leak/probe_concurrency.py`，8 线程 + barrier 放大竞态）

| 模式 | 标记被覆盖 | ctx 归属错误 |
|---|---|---|
| 模块级单例 | **7/8** | **7/8** |
| 每请求新建 | **0/8** | **0/8** |

⇒ 支持并发**必须**每请求新建。

★ **关键认知（此前被忽略）**：`reset_request_state`（上一轮为此加的门禁）
  只解决**顺序执行**下的"上一轮残留"，
  **解决不了并发**—— A 写进去的字段会被 B 的入口清零抹掉，
  A 收尾取到 None ⇒ 本次产出丢失。
  ⇒ **入口清零不能替代每请求新建**，两者是不同维度的问题。

### 为什么成本可以接受

`AgentPipeline.__init__` 实测 **0.001 ms**（只建 5 个轻量对象）；
`_load_db_agents()` ≈25 ms/请求，而这本来就每个请求都要跑（无缓存白拿）
⇒ 额外成本 ≈0.001 ms，相对一次 LLM 调用（3~60 s）可忽略。

## 判据

A1 · **路由层已改造**：`routers/conversations.py` 用 `get_pipeline()`，
     且**没有任何裸用模块级 `agent.execute*`**（源码级扫描）
A2 · **实例独立性**：`get_pipeline()` 连调两次必须返回不同实例
     （防"工厂函数被改成返回单例"）
A3 · **调用点在函数体内**：实例必须在 handler / 生成器**体内**取，
     提到模块级就等于退回单例（AST 定位 + 行号晚于 def）
A4 · **并发实测**：8 线程 barrier 探针，共享单例**必须**观测到覆盖
     （证明这个风险是真实的，不是理论推演）；新建必须 0 覆盖
A5 · **模块级 `agent` 仍保留**（既有脚本/测试依赖它），不破坏向后兼容

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_pipeline_concurrency.py
"""
from __future__ import annotations

import ast
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONV_PY = os.path.join(_ROOT, "routers", "conversations.py")

_fails: list[str] = []
_passes: list[str] = []


def _ok(msg: str) -> None:
    _passes.append(msg)
    print(f"[PASS] {msg}")


def _bad(msg: str) -> None:
    _fails.append(msg)
    print(f"[FAIL] {msg}")


# ────────────��──────── A1 · 路由层已改造 ─────────────────────────
def _a1_router_uses_factory() -> None:
    with open(CONV_PY, encoding="utf-8") as f:
        src = f.read()
        tree = ast.parse(src, filename=CONV_PY)

    # 裸用模块级单例（`agent.execute(...)` / `agent.execute_stream(...)`）
    import re
    bare = re.findall(r"(?<![\w.])agent\.(?:execute|execute_stream)\s*\(", src)
    if bare:
        _bad(f"A1 仍有 {len(bare)} 处裸用模块级 `agent.execute*`（并发下会互相覆盖）")
    else:
        _ok("A1 无裸用模块级 `agent.execute*`")

    if "from agent import get_pipeline" in src:
        _ok("A1 已导入 get_pipeline")
    else:
        _bad("A1 未导入 get_pipeline")

    n_factory = src.count("get_pipeline()")
    if n_factory >= 2:
        _ok(f"A1 调用点齐全（get_pipeline() 共 {n_factory} 处：chat + SSE）")
    else:
        _bad(f"A1 get_pipeline() 只有 {n_factory} 处调用，"
             f"chat 与 SSE 两个入口应各有一处")


# ───────────────────────── A2 · 实例独立性 ─────────────────────────
def _a2_instances_independent() -> None:
    try:
        from agent import get_pipeline
    except Exception as exc:                # noqa: BLE001
        _bad(f"A2 导入 get_pipeline 失败：{type(exc).__name__}: {exc}")
        return
    a = get_pipeline()
    b = get_pipeline()
    if a is b:
        _bad("A2 get_pipeline() 两次返回**同一实例** ⇒ 等于单例，并发不安全")
    else:
        _ok("A2 get_pipeline() 两次返回独立实例")

    t0 = time.perf_counter()
    for _ in range(30):
        get_pipeline()
    ms = (time.perf_counter() - t0) / 30 * 1000
    # 阈值来自实测（0.001ms）；给足余量只为拦住"构造意外变重"，不是卡性能
    if ms < 50:
        _ok(f"A2 构造成本可接受（{ms:.3f} ms/次，实测基准 0.001 ms）")
    else:
        _bad(f"A2 构造成本 {ms:.1f} ms/次，超出预期（50ms）"
             f"⇒ 每请求新建会变成性能负担，需重新评估")


# ───────────────────── A3 · 调用点在函数体内 ─────────────────────
def _a3_call_site_inside_function() -> None:
    with open(CONV_PY, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=CONV_PY)

    # 找所有 `get_pipeline()` 调用点，定位其所在函数
    call_lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "get_pipeline"):
            call_lines.append(node.lineno)

    if not call_lines:
        _bad("A3 未找到 get_pipeline() 调用点")
        return

    # 收集所有函数定义的行号区间
    fn_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            fn_ranges.append((node.lineno, end, node.name))

    outside = []
    for ln in call_lines:
        hit = [r for r in fn_ranges if r[0] <= ln <= r[1]]
        if not hit:
            outside.append(ln)
    if outside:
        _bad(f"A3 有 {len(outside)} 处 get_pipeline() 在**模块级**（行 {outside}）"
             f"⇒ 提到模块级等于退回单例，并发不安全")
    else:
        names = [r[2] for r in fn_ranges if any(r[0] <= ln <= r[1] for ln in call_lines)]
        _ok(f"A3 全部调用点在函数体内（{set(names)}）")


# ───────────────────── A4 · 并发实测（双向） ─────────────────────
def _run_concurrency_probe(use_new: bool, n: int = 8):
    from agent import AgentPipeline
    shared = AgentPipeline()
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i: int):
        p = AgentPipeline() if use_new else shared
        marker = f"REQ-{i}-CODE"
        p._sysml_last_pass_code = marker
        p._sysml_last_checked_code = marker
        p._mem_ctx = {"conversation_id": 1000 + i, "user": f"u{i}"}
        # ★ 全部写完再统一读——放大竞态窗口
        barrier.wait()
        time.sleep(0.05)
        results[i] = (marker, p._sysml_last_pass_code, p._mem_ctx)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lost = sum(1 for m, g, _ in results if m != g)
    wrong_ctx = sum(1 for m, g, c in results if c and c.get("user") != f"u{m.split('-')[1]}")
    return lost, wrong_ctx


def _a4_concurrency_probe() -> None:
    try:
        lost_s, ctx_s = _run_concurrency_probe(use_new=False)
        lost_n, ctx_n = _run_concurrency_probe(use_new=True)
    except Exception as exc:                # noqa: BLE001
        _bad(f"A4 并发探针异常：{type(exc).__name__}: {exc}")
        return

    print(f"       共享单例：覆盖 {lost_s}/8，ctx 归属错 {ctx_s}/8")
    print(f"       每请求新建：覆盖 {lost_n}/8，ctx 归属错 {ctx_n}/8")

    # 共享单例**必须**观测到覆盖 —— 否则说明这个风险不存在，A5 的门禁也就没必要
    if lost_s > 0:
        _ok(f"A4 风险真实存在：共享单例 {lost_s}/8 被覆盖"
            f"（证明门禁不是防不存在的问题）")
    else:
        _bad("A4 共享单例未观测到覆盖 —— 若真如此，说明风险不成立，"
             "应重新评估是否需要每请求新建（本探针可能失效）")

    if lost_n == 0 and ctx_n == 0:
        _ok("A4 每请求新建：0 覆盖、0 ctx 归属错误（并发安全）")
    else:
        _bad(f"A4 每请求新建仍不安全：覆盖 {lost_n}/8，ctx 归属错 {ctx_n}/8")


# ───────────────────── A5 · 向后兼容 ─────────────────────
def _a5_backward_compat() -> None:
    try:
        import agent as A
    except Exception as exc:                # noqa: BLE001
        _bad(f"A5 导入 agent 失败：{exc}")
        return
    if hasattr(A, "agent"):
        _ok("A5 模块级 `agent` 仍保留（既有脚本/测试向后兼容）")
    else:
        _bad("A5 模块级 `agent` 被删了 ⇒ 破坏大量既有脚本/测试")


def main() -> int:
    print("=" * 72)
    print("verify_pipeline_concurrency — 多用户并发下 Pipeline 隔离门禁")
    print("=" * 72)
    _a1_router_uses_factory()
    _a2_instances_independent()
    _a3_call_site_inside_function()
    _a4_concurrency_probe()
    _a5_backward_compat()
    print("\n" + "=" * 72)
    print(f"结果：{len(_passes)} PASS / {len(_fails)} FAIL")
    if _fails:
        for m in _fails:
            print(f"  - {m}")
        print("FAIL")
        return 1
    print("✅ 门禁通过：并发下 Pipeline 状态隔离")
    return 0


if __name__ == "__main__":
    sys.exit(main())
