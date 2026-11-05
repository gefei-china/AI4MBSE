# -*- coding: utf-8 -*-
"""条件重排（`rerank_mode="auto"`）的端到端验收：触发率 / 覆盖 / 耗时 / 零回归。

## 为什么单独有这个脚本

`rerank_mode` 上线后要能回答三个问题，缺一个都不能算验收：

1. **收益**：金标进注入窗口的条数有没有提升？（对比 `off`）
2. **代价**：实际触发率多少、平均耗时多少？（相对 `on` 省了多少）
3. **零回归**：有没有把原本正确的弄坏？（这条最容易被忽略）

## 判据（直接用生产函数，不重写逻辑）

走真实链路：`hybrid_search` → `llm_rerank(mode=auto)` → `_filter_low_conf`
→ `dedupe_and_normalize` → `select_for_injection` → 取前 `inject_k`。

配置通过**进程内改 `cfg.get("rag")` 字典**切换，`finally` 还原，**不写配置文件**
（避免"改过默认值就不生效"的老坑，本轮已踩过一次）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/verify_rerank_auto.py
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import importlib.util
import sqlite3

_spec = importlib.util.spec_from_file_location(
    "_ab", os.path.join(_ROOT, "tools", "eval", "eval_inject_k_chat_ab.py"))
_ab = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_ab)
except SystemExit:
    raise
CASES = _ab.CASES


def main():
    from knowledge_engine import hybrid_search
    from agent.retrieval_diversity import select_for_injection
    from agent.retrieval_dedup import dedupe_and_normalize
    from agent.rag import GraphRAG
    from services.rag_rerank import llm_rerank, needs_rerank, _rerank_mode
    from core import config as cfg

    con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    con.row_factory = sqlite3.Row
    all_chunks = [dict(r) for r in con.execute(
        "SELECT document_id, chunk_index, content FROM document_chunks")]

    golds = []
    for q, anchor, doc, keys in CASES:
        pos = {(c["document_id"], c["chunk_index"]) for c in all_chunks
               if anchor.lower() in (c["content"] or "").lower()}
        if 0 < len(pos) <= 8:
            golds.append((q, anchor, pos))

    inject_k = int(cfg.get("rag", "inject_k", 5) or 5)
    top_k = int(cfg.get("rag", "top_k", 20) or 20)
    rrk = int(cfg.get("rag", "rerank_max_candidates", 10) or 10)
    svc = GraphRAG()
    rag_sec = cfg.get("rag") or {}
    orig_mode = rag_sec.get("rerank_mode")

    print("=" * 94)
    print("条件重排验收（inject_k=%d, top_k=%d, 重排窗口=%d, 用例 %d 条）"
          % (inject_k, top_k, rrk, len(golds)))
    print("=" * 94)

    def _pipe(hits):
        h = list(hits)
        for f in (lambda x: svc._filter_low_conf(x)[0],
                  lambda x: dedupe_and_normalize(x)[0]):
            try:
                h = f(h)
            except Exception:
                pass
        sel, _ = select_for_injection(h, inject_k=inject_k)
        return sel[:inject_k]

    rows = []
    try:
        for q, anchor, pos in golds:
            base = hybrid_search(con, q, top_k=top_k, branches=None).get("hits") or []
            ok_off = bool({(h.get("document_id"), h.get("chunk_index"))
                           for h in _pipe(base)} & pos)

            need, why = needs_rerank(base)
            rag_sec["rerank_mode"] = "auto"
            t0 = time.time()
            try:
                recl = llm_rerank(q, list(base), top_k=top_k, max_candidates=rrk)
            except Exception:
                recl = base
            dt = time.time() - t0
            ok_auto = bool({(h.get("document_id"), h.get("chunk_index"))
                            for h in _pipe(recl)} & pos)
            rag_sec["rerank_mode"] = orig_mode

            rows.append((q, anchor, ok_off, need, why, ok_auto, dt))
    finally:
        rag_sec["rerank_mode"] = orig_mode

    # ── 逐条 ──
    print("%-32s %-6s %-8s %-18s %-8s" % ("问题", "off", "触发", "原因", "auto"))
    print("-" * 94)
    for q, anchor, ok_off, need, why, ok_auto, dt in rows:
        tag = ""
        if ok_auto and not ok_off:
            tag = " ★救回"
        elif ok_off and not ok_auto:
            tag = " ⚠弄坏"
        print("%-32s %-6s %-8s %-18s %-8s%s"
              % (q[:32], "✅" if ok_off else "❌", "是" if need else "·",
                 (why or "-")[:18], "✅" if ok_auto else "❌", tag))

    n_off = sum(1 for r in rows if r[2])
    n_auto = sum(1 for r in rows if r[5])
    n_trigger = sum(1 for r in rows if r[3])
    gained = [r for r in rows if r[5] and not r[2]]
    lost = [r for r in rows if r[2] and not r[5]]
    # 只有触发的那部分才产生耗时
    trigger_times = [r[6] for r in rows if r[3]]
    all_times = [r[6] for r in rows if r[3]]
    avg_when_trigger = (sum(trigger_times) / len(trigger_times)) if trigger_times else 0.0
    avg_overall = (sum(all_times) / len(rows)) if rows else 0.0

    print("-" * 94)
    print("覆盖：off = %d/%d  →  auto = %d/%d"
          % (n_off, len(rows), n_auto, len(rows)))
    print("触发：%d/%d = %.0f%%" % (n_trigger, len(rows), 100.0 * n_trigger / len(rows)))
    print("净变化：+%d 救回 / -%d 弄坏 = %+d"
          % (len(gained), len(lost), len(gained) - len(lost)))
    print("耗时：触发时平均 %.2fs；摊到所有查询 %.2fs/次"
          % (avg_when_trigger, avg_overall))

    print("\n[判据]")
    ok1 = n_auto > n_off
    ok2 = len(lost) == 0
    ok3 = n_trigger < len(rows)
    print("  %s 收益：auto(%d) > off(%d)" % ("✓" if ok1 else "✗", n_auto, n_off))
    print("  %s 零回归：没有弄坏（弄坏 %d 条）" % ("✓" if ok2 else "✗", len(lost)))
    print("  %s 条件生效：不是 always（触发 %d/%d）"
          % ("✓" if ok3 else "✗", n_trigger, len(rows)))
    for q, a, o0, nd, wy, o1, dt in gained:
        print("     救回：%s（%s）" % (q[:34], wy))
    for q, a, o0, nd, wy, o1, dt in lost:
        print("     ⚠弄坏：%s" % q[:34])

    print("\n配置未落盘：rerank_mode=%r（进程内还原）" % orig_mode)
    print("=" * 94)
    sys.exit(0 if (ok1 and ok2 and ok3) else 1)


if __name__ == "__main__":
    main()
