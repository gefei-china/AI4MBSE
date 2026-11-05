import sys
sys.path.insert(0, '.')
import knowledge_engine as ke
from database import get_db
from agent.rag import GraphRAG

g = GraphRAG()
conn = get_db()


def probe(q):
    hy = ke.hybrid_search(conn, q, top_k=20, branches=None)
    hits = hy['hits']
    if not hits:
        return dict(n=0, rel_max=0.0, rel_avg=0.0, bm_max=0.0, top5=[])
    rel = [g._relevance_of(h) for h in hits]
    bm = [float(h.get('bm25_score') or 0) for h in hits]
    return dict(n=len(hits), rel_max=round(max(rel), 4),
                rel_avg=round(sum(rel) / len(rel), 4), bm_max=round(max(bm), 2),
                top5=[round(x, 3) for x in sorted(rel, reverse=True)[:5]])


ANS = ["技术要求书里有哪些信息安全要求",
       "ISO 29148 对需求标识符的唯一性有什么规定",
       "KerML 中 classifier 的定义是什么",
       "SysML v2 相比 v1 有哪些主要变化",
       "车辆热管理仿真的整体建模思路",
       "SysML v2 中 part def 与 part usage 的区别",
       "技术要求书对服务品质有哪些性能指标",
       "需求与架构元素之间的追溯关系怎么建立"]
OUT = ["今天北京天气怎么样",
       "如何用 Python 写一个网页爬虫",
       "卫星太阳能板的清洗周期是多久",
       "红烧肉怎么做",
       "明天股市会涨吗",
       "推荐几部好看的电影"]

HDR = "%-38s %-4s %-8s %-8s %-8s %s"
print(HDR % ("query", "n", "rel_max", "rel_avg", "bm_max", "top5 relevance"))
print("-" * 112)
for label, qs in (("切题", ANS), ("域外", OUT)):
    print("[%s]" % label)
    for q in qs:
        p = probe(q)
        print(HDR % (q[:38], p["n"], p["rel_max"], p["rel_avg"], p["bm_max"], p["top5"]))
    print("-" * 112)
