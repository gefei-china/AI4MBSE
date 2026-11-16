# -*- coding: utf-8 -*-
"""检索侧 A/B：验证软删 852 后，检索结果真的不再命中它。

用法（由外层控制 MBSE_DB_PATH）：
    MBSE_DB_PATH=<db> python tools/_kb_retrieval_ab.py --label A

设计：
  · 用真实 `agent.rag.GraphRAG.retrieve()`（不走旁路复刻），embedding 服务真实调用；
  · 只读副本库（外层用 sqlite3.backup 拷出），不碰真库；
  · 输出 chunk 命中里出现的 document_id 集合，外层对比 A/B 差异。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 探针题：历史真机里命中过 doc 800/852 内容的问题（msg 6674 的原话）
QUERIES = [
    "sysml v2代码中expose的实现逻辑，输出规则",
    "SysML v2 中 part definition 是什么",
    "需求可验证性 Verifiable 的要求与条款出处",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="?")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    print("=" * 76)
    print("检索 A/B  标号=%s  DB=%s" % (args.label, os.path.basename(os.environ.get("MBSE_DB_PATH", ""))))
    print("=" * 76)

    from agent.rag import GraphRAG
    rg = GraphRAG()

    out = {"label": args.label,
           "db": os.environ.get("MBSE_DB_PATH", ""), "queries": []}
    for q in QUERIES:
        try:
            r = rg.retrieve(q, query_raw=q)
        except Exception as e:
            print("  !! %-40s 检索异常 %s: %s" % (q[:40], type(e).__name__, str(e)[:120]))
            out["queries"].append({"q": q, "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
            continue
        hits = r.get("chunk_hits") or []
        docs = []
        for h in hits:
            d = h.get("document_id")
            if d is not None and int(d) not in docs:
                docs.append(int(d))
        top = [(h.get("document_id"), round(float(h.get("vec_score") or 0), 3),
                str(h.get("section") or "")[:26]) for h in hits[:5]]
        print("  Q: %s" % q)
        print("     route=%-8s chunks=%-3d vector=%s graph=%s"
              % (r.get("route"), len(hits), r.get("vector_count"), r.get("graph_count")))
        print("     命中文档 =", docs)
        print("     top5 =", top)
        out["queries"].append({"q": q, "route": r.get("route"),
                               "n_chunks": len(hits), "docs": docs,
                               "top5": top,
                               "sections": [h.get("section") for h in hits]})
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("\n  → %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
