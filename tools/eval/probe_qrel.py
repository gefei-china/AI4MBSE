# -*- coding: utf-8 -*-
"""_graph_confidence 相关性因子（qrel）可行性探测。

问题：误路由例「宽带通信卫星的太阳能板清洗周期」graph conf 0.9——_graph_confidence
三因子（命中覆盖/关系/类型）与 query 无关。
探测：对评测集 graph 18 例（正，期望走 graph）与 neg 8 例（负，期望不自信走 graph），
计算 query-bigram 在命中实体文本中的覆盖率（qrel），看两组分布是否可分。
可分 → 加因子有意义；不可分 → 诚实放弃（相关性判断不属于置信度层）。
"""
import io
import json
import contextlib
import sys

sys.path.insert(0, r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")
import os
os.chdir(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")

from agent.rag import GraphRAG

def bigrams(s):
    s = (s or "").replace(" ", "").replace("，", "").replace("？", "")
    return {s[i:i+2] for i in range(max(len(s)-1, 0))} or {s}

def qrel(query, graph_results):
    """query bigram 被 top5 命中实体（name+type）文本覆盖的比例。"""
    q = bigrams(query)
    if not q:
        return 0.0
    corpus = bigrams("".join(
        str(e.get("name") or "") + str(e.get("entity_type") or "")
        for e in (graph_results or [])[:5]))
    return len(q & corpus) / len(q)

rag = GraphRAG()
ev = json.load(open(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/tmp/p21/evalset.json", encoding="utf-8"))

rows = []
for kind in ("graph", "neg"):
    for c in ev.get(f"{kind}_cases") or []:
        q = c["query"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = rag.retrieve(q, branch="dev")
        gr = r.get("entities") or []
        conf = float(r.get("confidence") or 0)
        rows.append({"kind": kind, "q": q[:20], "route": r.get("route"),
                     "conf": round(conf, 3), "n_gr": len(gr), "qrel": round(qrel(q, gr), 3)})

print(f"{'域':4} {'route':7} {'conf':6} {'qrel':6} {'n_gr':4}  query")
for r in rows:
    print(f"{r['kind']:4} {str(r['route']):7} {r['conf']:<6} {r['qrel']:<6} {r['n_gr']:<4}  {r['q']}")

for kind in ("graph", "neg"):
    qs = [r["qrel"] for r in rows if r["kind"] == kind]
    cs = [r["conf"] for r in rows if r["kind"] == kind]
    if qs:
        print(f"\n{kind}: qrel min/avg/max = {min(qs):.3f}/{sum(qs)/len(qs):.3f}/{max(qs):.3f}"
              f"  | conf min/avg/max = {min(cs):.3f}/{sum(cs)/len(cs):.3f}/{max(cs):.3f}")

# 可分性粗判：graph 的 qrel 下界 vs neg 的 qrel 上界
gq = sorted(r["qrel"] for r in rows if r["kind"] == "graph")
nq = sorted(r["qrel"] for r in rows if r["kind"] == "neg")
if gq and nq:
    print(f"\n可分性：graph qrel 最小 {gq[0]:.3f} vs neg qrel 最大 {nq[-1]:.3f} → "
          f"{'可分' if gq[0] > nq[-1] else ('部分重叠' if gq[0] <= nq[-1] else '?')}")
