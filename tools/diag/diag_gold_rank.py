# -*- coding: utf-8 -*-
"""金标名次分布诊断：`recall_k` 到底该设多大（数据驱动，不凭直觉）。

## 为什么需要它

`recall_k` 是本项目最容易"凭感觉调"的参数。它同时决定：
  · 每路（BM25 / 向量）召回宽度
  · 后续送进 RRF 融合的候选规模

调太小 ⇒ 金标（正确答案所在 chunk）在名次外被截断，**后面所有环节都救不回来**
（LLM 重排、多样性配额、注入窗口都是在"已召回的候选"里挑）。
调太大 ⇒ 白算 + 噪声候选挤占融合池。

⇒ 正确做法是**看金标名次分布**，按分位数定，而不是拍一个整数。

## 本脚本做什么

对 `eval_inject_k_chat_ab.py` 的 17 条用例：
  1. 定位金标 chunk（包含该用例 `ctx_anchor` 的 chunk，必须唯一 1~3 个）
  2. 算它在 **BM25 单路** 与 **混合融合** 里的名次（宽搜索到 500，看真实名次）
  3. 输出分位数（p50 / p80 / p90 / max）
  4. 模拟 `recall_k ∈ {20,30,40,60,100}`：金标能否进融合池、能否进最终前 5

## 判据

`covered@K` = 金标落入「融合池前 K 名」的用例比例。
这是 `recall_k` 的上界参考：融合池前 K 之外的，注入窗口再大也看不到。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/diag/diag_gold_rank.py
"""
import os
import sys
import statistics

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import importlib.util
import sqlite3

# 复用对话级 A/B 的用例表（ctx_anchor 才是判依据用的严格锚点）
_spec = importlib.util.spec_from_file_location(
    "_ab", os.path.join(_ROOT, "tools", "eval", "eval_inject_k_chat_ab.py"))
_ab = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_ab)
except SystemExit:
    raise
CASES = _ab.CASES


def main():
    from knowledge_engine import _bm25_cache_get, hybrid_search

    con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    con.row_factory = sqlite3.Row

    print("=" * 90)
    print("金标名次分布诊断（recall_k 该设多大）")
    print("=" * 90)

    # ① 定位金标 chunk
    all_chunks = [dict(r) for r in con.execute(
        "SELECT document_id, chunk_index, content FROM document_chunks")]
    golds = []
    bad = []
    for q, anchor, doc, keys in CASES:
        al = anchor.lower()
        hits = {(c["document_id"], c["chunk_index"]) for c in all_chunks
                if al in (c["content"] or "").lower()}
        if not hits:
            bad.append((q, anchor, 0))
        elif len(hits) > 8:
            bad.append((q, anchor, len(hits)))
        else:
            # ★ 金标是**位置集合**而不是单点：库里 847/848 是同一份 PDF 的副本，
            #   同一锚点必然出现在两个 doc 里。**任一副本被检索到都算命中**，
            #   所以取集合内的最优名次，而不是"必须唯一才统计"（那会把 17 条砍成 1 条）。
            golds.append((q, anchor, hits))
    print("\n[0] 金标定位：%d 条可用（金标按「位置集合」计，任一副本命中即可）" % len(golds))
    multi = sum(1 for _, _, hs in golds if len(hs) > 1)
    print("    其中 %d 条锚点命中 >1 个 chunk（847/848 副本所致，属预期）" % multi)
    for q, a, n in bad:
        print("  [WARN] 锚点 %-14s 命中 %d 个 chunk（跳过）" % (a, n))
    if not golds:
        print("无可用用例")
        return

    # ② BM25 单路名次（宽搜 500）
    eng, rows = _bm25_cache_get(con, None, None, None, False)
    id2pos = {r["id"]: (r["document_id"], r["chunk_index"]) for r in rows}
    pos2id = {v: k for k, v in id2pos.items()}

    print("\n[1] BM25 单路名次")
    print("  %-38s %-14s %8s %8s" % ("问题", "金标", "名次", "分数"))
    bm_ranks = []
    for q, anchor, pos in golds:
        hits = eng.search(q, top_k=500)
        rank, score = None, 0.0
        gids = {pos2id.get(p) for p in pos if pos2id.get(p) is not None}
        for i, h in enumerate(hits, 1):
            if h["chunk_id"] in gids:
                rank, score = i, h["score"]
                break
        bm_ranks.append(rank if rank else 10 ** 6)
        tag = "/".join("%s.%s" % p for p in sorted(pos))
        print("  %-38s %-16s %8s %8s"
              % (q[:38], tag[:16], rank if rank else ">500",
                 round(score, 1) if rank else "—"))

    def pct(vals, p):
        v = sorted(vals)
        if not v:
            return 0
        i = min(len(v) - 1, int(round((len(v) - 1) * p)))
        return v[i]

    finite = [r for r in bm_ranks if r < 10 ** 6]
    print("\n  BM25 名次分位：p50=%s p80=%s p90=%s max=%s（未召回 %d 条）"
          % (pct(finite, 0.5), pct(finite, 0.8), pct(finite, 0.9),
             max(finite) if finite else "—", len(bm_ranks) - len(finite)))

    # ③ 混合融合名次
    print("\n[2] 混合融合名次（生产真实路径）")
    print("  %-38s %-14s %8s" % ("问题", "金标", "融合名次"))
    fused_ranks = []
    for q, anchor, pos in golds:
        try:
            r = hybrid_search(con, q, top_k=60, branches=None)
            hits = r.get("hits") or []
        except Exception:
            hits = []
        rank = None
        for i, h in enumerate(hits, 1):
            if (h.get("document_id"), h.get("chunk_index")) in pos:
                rank = i
                break
        fused_ranks.append(rank if rank else 10 ** 6)
        tag = "/".join("%s.%s" % p for p in sorted(pos))
        print("  %-38s %-16s %8s" % (q[:38], tag[:16], rank if rank else ">60"))

    ff = [r for r in fused_ranks if r < 10 ** 6]
    print("\n  融合名次分位：p50=%s p80=%s p90=%s max=%s（未进前 60：%d 条）"
          % (pct(ff, 0.5), pct(ff, 0.8), pct(ff, 0.9),
             max(ff) if ff else "—", len(fused_ranks) - len(ff)))

    # ④ 模拟不同 recall_k 的覆盖
    print("\n[3] 模拟：不同 recall_k 下「金标进融合池前 K」的比例")
    print("  （这是上界：融合池前 K 之外的，注入窗口再大也看不到）")
    print("  %-10s %-16s %-16s" % ("recall_k", "覆盖@5(注入窗口)", "覆盖@20(融合池)"))
    for k in (20, 30, 40, 60, 100):
        # 用 BM25 名次近似：BM25 名次 <= recall_k 才会进融合池
        c5 = sum(1 for r in bm_ranks if r <= 5)
        c20 = sum(1 for r in bm_ranks if r <= k)
        print("  %-10d %-16s %-16s"
              % (k, "%d/%d" % (c5, len(bm_ranks)),
                 "%d/%d = %.0f%%" % (c20, len(bm_ranks),
                                     100.0 * c20 / len(bm_ranks))))

    print("\n[4] 判读")
    print("  · BM25 名次 ≤5 的只有 %d/%d ⇒ 光靠稀疏路，注入 5 条必然漏大部分"
          % (sum(1 for r in bm_ranks if r <= 5), len(bm_ranks)))
    print("  · recall_k 从 20 提到 40，能把 BM25 名次 21~40 的 %d 条纳入候选"
          % sum(1 for r in bm_ranks if 20 < r <= 40))
    print("  · 但纳入 ≠ 注入：还要赢过 RRF 排序 + 多样性配额")
    print("=" * 90)


if __name__ == "__main__":
    main()
