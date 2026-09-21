# -*- coding: utf-8 -*-
"""量化「LLM 重排」自身的运行间方差 —— 它是 doc 域分数噪声地板的来源。

背景：修掉 `_tokens`（跨进程不确定）后，graph 域三轮完全一致（1.0/0.972/1.0），
但 doc 域 F 层仍漂：recall@3/@5 = 0.85 vs 0.90、MRR = 0.857 vs 0.867，而 P 层
（pool_recall，**不含 LLM**）三轮都是 0.90 —— 差异必然来自 F 层独有的那一环：`llm_rerank`。

本探针：固定 query + 固定 RRF 候选（关掉重排取一次），然后**反复调 llm_rerank**，
数它给出多少种不同的排序。这直接给出"同一输入、同一候选、重排自身的波动"。
"""
import io
import os
import sys
from contextlib import redirect_stdout

REPO = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, REPO)
os.chdir(REPO)

from database import get_db
from knowledge_engine import hybrid_search
import services.rag_rerank as rr

N = int(os.environ.get("N", "5"))
QUERIES = [
    "2.4 C3 - Unambiguous",
    "648 Systems Modeling Language v2.0",
    "7.12.1 Ports Overview",
]

print("=" * 78)
print("LLM 重排自身的重复性（每 query 调 %d 次）" % N)
print("=" * 78)

for q in QUERIES:
    # 1) 固定候选：关掉重排取一次 RRF 排序（这是确定性的，P 层已验证）
    orig = rr.llm_rerank
    rr.llm_rerank = lambda qq, hits, top_k=5, max_candidates=8, timeout=25: hits
    conn = get_db()
    try:
        with redirect_stdout(io.StringIO()):
            hy = hybrid_search(conn, q, top_k=10, branches=None)
    finally:
        conn.close()
        rr.llm_rerank = orig

    base = [(h["source_doc"], h["chunk_index"]) for h in hy["hits"]]
    print()
    print("Q: %s" % q)
    print("   候选（RRF 固定，%d 条）: %s" % (len(base), [b[1] for b in base]))

    # 2) 反复重排同一份候选
    orders = []
    for i in range(N):
        cand = [dict(h) for h in hy["hits"]]          # 每轮给独立副本，避免就地写字段互相污染
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = orig(q, cand, top_k=10, max_candidates=10)
        orders.append(tuple((h["source_doc"], h["chunk_index"]) for h in out))

    uniq = {}
    for o in orders:
        uniq[o] = uniq.get(o, 0) + 1
    print("   重排后出现 %d 种排序（%d 次调用）:" % (len(uniq), N))
    for o, n in uniq.items():
        flag = "← 与 RRF 原始序一致" if o == tuple(base) else ""
        print("     %d× %s %s" % (n, [x[1] for x in o], flag))
    # top-1 与 top-3 的稳定性（评测里最吃这两个）
    t1 = {o[0] for o in orders if o}
    t3 = {o[:3] for o in orders}
    print("   top-1 去重 %d 个 / top-3 去重 %d 个 → %s"
          % (len(t1), len(t3), "稳定" if len(t3) == 1 else "❌ 不稳"))

print()
print("=" * 78)
print("结论解读")
print("=" * 78)
print("若 top-3 出现多种 → 重排自身不稳定，doc 域 recall@3/@5 的 0.85↔0.90 波动即由它造成。")
print("→ 该方差是**分数噪声地板**：小于它的调参差异不可判（与 preflight 的延迟噪声带宽同理）。")
