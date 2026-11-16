# -*- coding: utf-8 -*-
"""跨语言检索评测器（纯中文查询 → 英文文档）。

＃ 为什么单独一份
`tools/eval/qrels.jsonl` 是从**中文标题**自动生成的（`使用说明是什么？`），
且部分用例的期望文档已下架（doc 848）⇒ 它既不能衡量跨语言，也含必错项。
本评测集是**人工逐条核对过的** 12 条纯中文查询（零英文术语），期望文档是
对应英文规范的 document_id，判据只看文档级命中（不依赖 chunk 标注粒度）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_xling.py
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_xling.py --top-k 20 --json out.json
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")
QRELS = os.path.join(_HERE, "qrels_xling.jsonl")


def load_cases(path=QRELS):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def evaluate(retrieve, cases, top_k=20):
    """retrieve(query) -> list[document_id]（按排名）。返回指标 dict。"""
    rows = []
    for cs in cases:
        want = set(int(x) for x in cs["expect_doc_ids"])
        docs = retrieve(cs["query"])[:top_k]
        rank = next((i for i, d in enumerate(docs, 1) if d in want), None)
        rows.append({"query": cs["query"], "topic": cs.get("topic", ""),
                     "expect": sorted(want), "rank": rank,
                     "hit": rank is not None,
                     "top1": docs[0] if docs else None,
                     "n_docs": len(docs)})

    def rate(k):
        return sum(1 for r in rows if r["rank"] and r["rank"] <= k) / max(len(rows), 1)

    mrr = sum((1.0 / r["rank"]) for r in rows if r["rank"]) / max(len(rows), 1)
    return {"n": len(rows), "rows": rows,
            "recall@1": rate(1), "recall@3": rate(3),
            "recall@5": rate(5), "recall@top": rate(top_k),
            "mrr": mrr}


def doc_ids_of(query, top_k=20):
    """走生产检索链路，返回命中文档 id 列表（按 chunk 出现顺序去重）。"""
    from agent.rag import GraphRAG
    rg = GraphRAG()
    r = rg.retrieve(query, query_raw=query)
    out = []
    for h in (r.get("chunk_hits") or [])[:top_k]:
        d = h.get("document_id")
        if d is not None and int(d) not in out:
            out.append(int(d))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cases = load_cases()
    print("=" * 82)
    print("跨语言检索评测（纯中文查询 → 英文文档）  %d 条" % len(cases))
    print("=" * 82)
    m = evaluate(lambda q: doc_ids_of(q, args.top_k), cases, args.top_k)
    for r in m["rows"]:
        flag = ("✓rank1" if r["rank"] == 1 else
                ("○rank%d" % r["rank"] if r["rank"] else "✗miss"))
        print("  %-34s 期望%-8s %-8s top1=%s"
              % (r["query"][:34], r["expect"], flag, r["top1"]))
    print()
    print("  recall@1=%.3f  recall@3=%.3f  recall@5=%.3f  recall@top=%.3f  MRR=%.3f"
          % (m["recall@1"], m["recall@3"], m["recall@5"], m["recall@top"], m["mrr"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({k: v for k, v in m.items() if k != "rows"}, f,
                      ensure_ascii=False, indent=1)
            f.write("\n")
        print("  → %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
