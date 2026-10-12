"""检索质量评测集与评测器（P1 · 全链路优化的"证明基础"）。

══════════════════════════════════════════════════════════════════════
为什么它是 P1 而不是 P2
══════════════════════════════════════════════════════════════════════
行业共识（2026公开基准）：
  *"reaching for semantic or contextual chunking on day one... before there is any
    evaluation harness to prove it helps"*  —— 先有评测集，再动算法。
  *"In every case I measured, recursive-512 plus a reranker beat fancy chunking
    with no reranker."*

本工程此前**没有 qrel 标注集** ⇒「重排/分词/hyde」的收益都无法证明。
`tools/eval/probe_qrel.py` 只是**可行性探测**（算 bigram 覆盖率），不是评测集。

本模块提供：
  ① `build_qrels.py` —— 从真库**自动生成候选标注**（人工只需确认）
  ② 本文件 —— 评测器：recall@k / MRR / nDCG + 分路由置信对比

⚠️ **自动生成的 qrel 是"候选"不是"标注"**：
   它用「hyde 首问→检索自己」自证，逻辑可靠但**仍需人工抽检**
   （脚本会明确标出需核对的条目）。
"""
import argparse
import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
DB = os.path.join(_ROOT, "mbse.db")
QRELS = os.path.join(_ROOT, "tools", "eval", "qrels.jsonl")


def _conn():
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_qrels(path: str = QRELS) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(json.loads(ln))
    return out


def recall_at_k(qrels: list, run: dict, k: int = 5) -> float:
    """recall@k = 命中的相关文档数 / 相关文档总数"""
    tot = hit = 0
    for q in qrels:
        rel = set(q.get("relevant_keys") or q.get("relevant") or [])
        if not rel:
            continue
        got = set((run.get(q["query"]) or [])[:k])
        tot += len(rel)
        hit += len(rel & got)
    return hit / tot if tot else 0.0


def mrr(qrels: list, run: dict, k: int = 10) -> float:
    """MRR@k = 第一个正确结果的倒数排名均值"""
    s = 0.0
    for q in qrels:
        rel = set(q.get("relevant_keys") or q.get("relevant") or [])
        if not rel:
            continue
        for i, cid in enumerate((run.get(q["query"]) or [])[:k], 1):
            if cid in rel:
                s += 1.0 / i
                break
    return s / max(len(qrels), 1)


def ndcg_at_k(qrels: list, run: dict, k: int = 10) -> float:
    """简化 nDCG（二元相关度）"""
    import math
    tot = 0.0
    for q in qrels:
        rel = set(q.get("relevant_keys") or q.get("relevant") or [])
        if not rel:
            continue
        dcg = sum(1.0 / math.log2(i + 1)
                  for i, c in enumerate((run.get(q["query"]) or [])[:k], 1) if c in rel)
        ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rel), k) + 1))
        tot += (dcg / ideal) if ideal else 0.0
    return tot / max(len(qrels), 1)


def _key(h) -> str:
    """从检索结果里取出可与 qrels 对齐的键。

    ⚠️ **实测踩坑（2026-10-10）**：`chunk_hits` 里**没有 `chunk_id`/`id` 字段**，
       它的结构是 `{score, vec_score, bm25_score, hyde_score, document_id,
                      chunk_index, content, section, recall_reason, ...}`
       ⇒ 我最初写的 `h.get("chunk_id") or h.get("id")` **恒为 None**，
         指标全0 却看起来像"检索质量极差"（实为评测器 bug）。
    ⇒ 必须用 **(document_id, chunk_index)** 复合键——它才是块的稳定标识。
    """
    if not isinstance(h, dict):
        return ""
    if h.get("chunk_id"):
        return str(h["chunk_id"])
    did, idx = h.get("document_id"), h.get("chunk_index")
    if did is not None and idx is not None:
        return f"{did}:{idx}"
    return ""


def run_retrieval(qrels: list, top_k: int = 20) -> dict:
    """真跑检索，产出 {query: [key,...]}（⚠️ 会真实调 LLM/embedding）"""
    from agent.rag import GraphRAG
    g = GraphRAG()
    run = {}
    for q in qrels:
        try:
            out = g.retrieve(q["query"])
            run[q["query"]] = [_key(h) for h in (out.get("chunk_hits") or [])[:top_k]]
        except Exception as e:
            run[q["query"]] = []
            print(f"  [WARN] {q['query'][:30]} 失败：{type(e).__name__}")
    return run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--offline", action="store_true",
                    help="只用 qrels 里的 related 顺序当'完美检索'算基线（不调 LLM）")
    a = ap.parse_args()
    qrels = load_qrels()
    print("=" * 70)
    print(f"检索质量评测　qrels={len(qrels)} 条　"
          f"{'（离线基线模式）' if a.offline else '（真实检索模式，会调 LLM）'}")
    print("=" * 70)
    if not qrels:
        print("❌ 还没有 qrels 标注集。先跑：")
        print("   python tools/eval/build_qrels.py --n 80")
        return 1
    if a.offline:
        run = {q["query"]: list(q.get("relevant_keys") or q.get("relevant") or []) for q in qrels}
    else:
        run = run_retrieval(qrels, top_k=a.top_k)
    for k in (1, 3, 5, 10, 20):
        print(f"  recall@{k:<3d} = {recall_at_k(qrels, run, k):.3f}")
    for k in (5, 10, 20):
        print(f"  MRR@{k:<4d}   = {mrr(qrels, run, k):.3f}")
    print(f"  nDCG@10     = {ndcg_at_k(qrels, run, 10):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())