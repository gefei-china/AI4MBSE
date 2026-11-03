#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_trace_inherit.py —— 线程池链路上下文继承门禁（2026-11-03）。

## 事故（真库 75 条 `llm_usage_stats` 实证）

| 字段 | 实测值 | 说明 |
|---|---|---|
| `conversation_id` | **0** | 归属丢失 |
| `run_id` | **0** | 归属丢失 |
| `trace_id` | **有值** | ← 反常：若真丢干净，trace 也该空 |

`trace_id 有值` 正是**空上下文**的指纹：`execute_stream` 里
`begin_trace(...) if (conversation_id or not _exist.get("trace_id")) else _exist`
—— ctx 为空 ⇒ `not ""` 为真 ⇒ **新开一条链**。于是"一次编排沿用一条 trace"
的声明被打破，一次编排碎成 N+1 条链，按会话/按 run 的成本统计全部漏算。

## 根因（两层，都不是"忘了写代码"）

1. **`ContextVar` 不跨线程传播**：`ThreadPoolExecutor.submit` 起的线程拿到空上下文；
2. **包装器写了但没接线**：`stream.py` 原有 `_submit_with_ctx`（实现正确），
   但**只被首跑路径使用**，补跑路径用裸 `pool.submit(...)`；
   `workflows/planner.py` / `workflows/nodes.py` 两处同样是裸 submit。

## 本门禁守什么

A 行为层
  A1 `submit_with_ctx` 完整继承 conversation_id / run_id / trace_id
  A2 裸 submit 确实拿不到（**缺陷复现**，证明 A1 不是空转）
  A3 `inherit_trace` 在空上下文里兜底补回归属
  A4 `inherit_trace` 不覆盖已有的真实值（cur 优先于 override）
  A5 两级嵌套仍继承（编排内还可能再起线程）

B 静态层（防回退 —— 这是本次事故的直接成因）
  B1 三处编排提交点都用 `submit_with_ctx`
  B2 产品代码里**没有裸 `pool.submit(`**（包装器内部实现除外）
  B3 子任务入口用 `inherit_trace`（纵深防御在位）

C 变异自证
  C1 把 `submit_with_ctx` 退化为裸 submit ⇒ A1 必须变红

运行：python tools/verify/verify_trace_inherit.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from core.audit import (bind_trace, clear_trace, inherit_trace,  # noqa: E402
                        submit_with_ctx, trace_context)

_R = []


def ck(name, ok, detail=""):
    _R.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))


def _ctx_in_thread(fn=None):
    """在当前线程的上下文里，把（可选）fn 丢进线程池，返回线程里看到的 ctx。"""
    def _probe():
        if fn:
            fn()
        return dict(trace_context())
    with ThreadPoolExecutor(1) as ex:
        return submit_with_ctx(ex, _probe).result()


def main():
    print("=" * 92)
    print("线程池链路上下文继承门禁")
    print("=" * 92)

    clear_trace()
    bind_trace(conversation_id=1234, run_id=567, trace_id="T-1")

    # ── A 行为层 ──
    got = _ctx_in_thread()
    ck("A1 submit_with_ctx 完整继承 conversation_id/run_id/trace_id",
       got.get("conversation_id") == 1234 and got.get("run_id") == 567
       and got.get("trace_id") == "T-1", str(got))

    with ThreadPoolExecutor(1) as ex:
        raw = ex.submit(lambda: dict(trace_context())).result()
    ck("A2 裸 submit 拿不到上下文（缺陷复现，证明 A1 非空转）",
       raw == {}, str(raw))

    with ThreadPoolExecutor(1) as ex:
        fixed = ex.submit(lambda: (inherit_trace(sub_task_key="t1",
                                                conversation_id=1234, run_id=567),
                                   dict(trace_context()))[1]).result()
    ck("A3 inherit_trace 在空上下文里兜底补回归属",
       fixed.get("conversation_id") == 1234 and fixed.get("run_id") == 567
       and fixed.get("sub_task_key") == "t1", str(fixed))

    kept = _ctx_in_thread(lambda: inherit_trace(conversation_id=9999,
                                                sub_task_key="t2"))
    ck("A4 inherit_trace 不覆盖已有真实值（cur 优先）",
       kept.get("conversation_id") == 1234 and kept.get("sub_task_key") == "t2",
       str(kept))

    def _mid():
        with ThreadPoolExecutor(1) as ex2:
            return submit_with_ctx(ex2, lambda: dict(trace_context())).result()
    with ThreadPoolExecutor(1) as ex:
        deep = submit_with_ctx(ex, _mid).result()
    ck("A5 两级嵌套仍继承（编排内还会再起线程）",
       deep.get("conversation_id") == 1234 and deep.get("run_id") == 567, str(deep))
    clear_trace()

    # ── B 静态层 ──
    src_audit = (ROOT / "core" / "audit.py").read_text(encoding="utf-8")
    ck("B0 core/audit.py 提供 submit_with_ctx + inherit_trace（唯一实现）",
       "def submit_with_ctx(" in src_audit and "def inherit_trace(" in src_audit)

    files = ["agent/pipeline_parts/stream.py", "workflows/planner.py", "workflows/nodes.py"]
    bare = []
    for rel in files:
        p = ROOT / rel
        if not p.exists():
            ck(f"B1 {rel} 存在", False)
            continue
        src = p.read_text(encoding="utf-8")
        # 裸 submit：`xxx.submit(` 且不是包装器内部的 `pool.submit(lambda`
        for m in re.finditer(r"(\w+)\.submit\(", src):
            line_start = src.rfind("\n", 0, m.start()) + 1
            line = src[line_start:src.find("\n", m.start())]
            if "lambda *a: _ctx.run" in line:
                continue            # 包装器自身实现
            bare.append(f"{rel}: {line.strip()[:70]}")
    ck("B1 三处编排提交点无裸 submit（这是本次事故的直接成因）",
       not bare, "; ".join(bare[:3]))

    src_stream = (ROOT / "agent" / "pipeline_parts" / "stream.py").read_text(encoding="utf-8")
    ck("B2 首跑路径用 _submit_with_ctx",
       "_submit_with_ctx(_exec_pool, _worker" in src_stream)
    ck("B3 补跑路径用 _submit_with_ctx（原来这里是裸 submit）",
       "_submit_with_ctx(_pool_f, _run_subtask" in src_stream)
    ck("B4 子任务入口用 inherit_trace（纵深防御，兜住将来退化的提交方式）",
       "inherit_trace as _inherit" in src_stream)
    src_planner = (ROOT / "workflows" / "planner.py").read_text(encoding="utf-8")
    ck("B5 非流式编排用 submit_with_ctx", "_swc(pool, self._run_task" in src_planner)
    src_nodes = (ROOT / "workflows" / "nodes.py").read_text(encoding="utf-8")
    ck("B6 画布 worker 用 submit_with_ctx", "_swc(pool, _run_worker" in src_nodes)

    # ★ 2026-11-03 追加：**裸 threading.Thread** 的四处请求链路线程。
    #   （第一次修只覆盖了线程池，漏了裸线程 ⇒ 真库 28 条 rag_rerank 孤儿由此产生。
    #     这是同一缺陷族在本轮里的**第三次复发**，故全部静态锁定。）
    src_lg = (ROOT / "agent" / "loop_guard.py").read_text(encoding="utf-8")
    ck("B7 工具超时线程继承上下文（rag_rerank 28 条孤儿的直接来源）",
       "copy_context()" in src_lg and "_tool_ctx.run(exec_fn" in src_lg)
    src_sse = (ROOT / "core" / "sse.py").read_text(encoding="utf-8")
    ck("B8 SSE 同步事件源线程继承上下文（否则整条编排 trace 断链）",
       "copy_context()" in src_sse and ".run(_worker)" in src_sse)
    ck("B9 SSE 异步事件源线程同样继承", src_sse.count("copy_context()") >= 2)
    src_flows = (ROOT / "routers" / "studio_parts" / "flows.py").read_text(encoding="utf-8")
    ck("B10 画布异步流程线程继承上下文",
       "copy_context()" in src_flows and ".run(_worker)" in src_flows)
    src_sup = (ROOT / "core" / "orch_supervisor.py").read_text(encoding="utf-8")
    ck("B11 断点续跑线程显式绑定归属（无常驻上下文可继承）",
       "inherit_trace as _it" in src_sup)

    # 常驻后台循环**不应**继承（会把某次请求的 ctx 固化进循环）
    ck("B12 常驻后台循环未被误改（alert/watchdog/job_queue/mcp_health 保持裸线程）",
       all(k not in (ROOT / f).read_text(encoding="utf-8")
           for f, k in [("core/alert_evaluator.py", "copy_context"),
                        ("core/job_queue.py", "copy_context"),
                        ("mcp_health.py", "copy_context")]))

    # ── C 变异自证 ──
    src = src_audit
    mut = src.replace(
        "    import contextvars as _cv\n    _ctx = _cv.copy_context()\n"
        "    return pool.submit(lambda *a: _ctx.run(fn, *a), *args)",
        "    return pool.submit(fn, *args)  # MUTATION: 不继承上下文", 1)
    if mut == src:
        ck("C0 变异锚点命中（submit_with_ctx 实现仍在）", False,
           "实现变了 ⇒ 本段自证需同步更新，勿留空跑")
    else:
        ns = {"__name__": "_mut_audit"}
        exec(compile(mut, "<mutant>", "exec"), ns)
        bind_trace(conversation_id=1234, run_id=567, trace_id="T-1")
        with ThreadPoolExecutor(1) as ex:
            m_got = ns["submit_with_ctx"](ex, lambda: dict(trace_context())).result()
        clear_trace()
        ck("C1 变异（包装器退化为裸 submit）⇒ 上下文丢失（A1 会变红）",
           m_got == {}, str(m_got))

    fails = [r for r in _R if not r[1]]
    print("=" * 92)
    print(f"断言汇总：{len(_R) - len(fails)}/{len(_R)} 通过")
    for nm, _, dt in fails:
        print("  ✗ " + nm + (f"  | {dt}" if dt else ""))
    print("✅ ALL PASS" if not fails else "❌ 有红项")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
