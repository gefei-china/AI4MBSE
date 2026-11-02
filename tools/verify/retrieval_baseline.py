# -*- coding: utf-8 -*-
"""检索质量基线记录器（S2）：跑固定 query 集，输出可对比的指标快照。

为什么要这个脚本：
  没有基线，"改完感觉变好了"就是全部证据。本仓历史上已多次出现
  「看起来生效/看起来失效」其实是判据或口径错（记录在案 8 次）。
  ⇒ 任何检索改动前后都必须用**同一套 query、同一套指标**跑一遍。

指标（**全部基于语料事实，不靠人工打分**）：
  hit@5       top-5 里出现期望文档关键词的比例（近似 recall，判"召回到没"）
  recall@5    期望文档被召回的比例（多期望文档时取平均）
  mrr         首个正确命中的倒数排名均值（判"排得前不前排"）
  reject_acc  域外 query 正确返回空的比例（判"该拒的拒没拒"）
  avg_ms      平均耗时
  degraded    降级态占比

用法：
  # 记录基线（标签自定义，会写 tools/verify/baselines/<tag>.json）
  ./.venv/Scripts/python.exe -X utf8 tools/verify/retrieval_baseline.py --tag before_s3
  # 与已有基线对比
  ./.venv/Scripts/python.exe -X utf8 tools/verify/retrieval_baseline.py --tag after_s3 --compare before_s3
"""
import os
import sys
import json
import time
import argparse
import statistics

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# ── 固定 query 集 ──────────────────────────────────────────────────────────
# 每条：label / 问题类型 / query / 期望命中的文档关键词（任一出现在 source_doc 即算命中）
#        empties 表示"应当拒答"（域外）。
# 类型按实际业务分布设计：枚举(结构化) / 事实(具体段落) / 关系(多跳) / 归纳(全局) / 域外
QUERIES = [
    # ── 技术要求书（项目自身需求，55×2 chunks）──
    ("枚举型", "技术要求书里有哪些信息安全要求", ["847_Print", "Print_面向"], False),
    ("枚举型", "技术要求书对服务品质有哪些性能指标", ["847_Print", "Print_面向"], False),
    ("事实型", "技术要求书里可靠性指标是多少", ["847_Print", "Print_面向"], False),
    ("枚举型", "技术要求书要求交付哪些文档", ["847_Print", "Print_面向"], False),
    # ── ISO 29148 需求工程标准（610 chunks）──
    ("事实型", "ISO 29148 对需求标识符的唯一性有什么规定", ["ISO 29148"], False),
    ("事实型", "ISO 29148 定义的需求质量特性有哪些", ["ISO 29148"], False),
    ("关系型", "需求验证有哪些方法", ["ISO 29148", "Guide to writing"], False),
    # ── KerML 内核语言（2011 chunks）──
    ("事实型", "KerML 中 classifier 的定义是什么", ["KerML"], False),
    ("关系型", "KerML 与 SysML v2 是什么关系", ["KerML", "SysML-v2-AI"], False),
    ("事实型", "KerML 的模型序列化怎么做", ["KerML"], False),
    # ── OMG SysML v2 规范（2606 chunks）──
    ("关系型", "SysML v2 中 part def 与 part usage 的区别", ["OMG Systems Modeling", "SysML-v2-AI"], False),
    ("归纳型", "SysML v2 相比 v1 有哪些主要变化", ["OMG Systems Modeling", "SysML-v2-AI"], False),
    ("事实型", "SysML v2 中 port 的定义是什么", ["OMG Systems Modeling", "SysML-v2-AI"], False),
    ("关系型", "SysML v2 中 action 和 state 有什么区别", ["OMG Systems Modeling", "SysML-v2-AI"], False),
    # ── 需求编写指南（855 chunks）──
    ("归纳型", "需求应该怎么写才规范", ["Guide to writing", "ISO 29148"], False),
    ("关系型", "需求与架构元素之间的追溯关系怎么建立", ["Guide to writing", "ISO 29148"], False),
    # ── SysML v2 AI 建模知识（270 chunks）──
    ("事实型", "SysML v2 词法有哪些硬约束", ["SysML-v2-AI"], False),
    ("归纳型", "KerML 是内核还是 SysML 是内核", ["SysML-v2-AI", "KerML"], False),
    # ── 广汽方法论（180 chunks）──
    ("归纳型", "广汽 MBSE 方法论的整体流程", ["广汽方法论"], False),
    ("事实型", "子系统架构设计怎么做", ["广汽方法论"], False),
    # ── 车辆场景专业知识库（382 chunks，三个域）──
    ("归纳型", "车辆热管理仿真的整体建模思路", ["热管理", "动力经济性", "座舱空气"], False),
    ("事实型", "纯电汽车热管理场景的知识覆盖要求", ["热管理"], False),
    ("事实型", "座舱空气调节仿真的建模场景", ["座舱空气"], False),
    # ── SysML 语法错误手册（101 chunks）──
    ("事实型", "SysML v2 常见的语法错误有哪些", ["语法错误"], False),
    ("事实型", "包名含空格会导致什么错误", ["语法错误"], False),
    # ── 视图规范（52 chunks）──
    ("事实型", "利益相关方需求视图的元素要求", ["视图规范"], False),
    ("关系型", "视图之间的关联关系是什么", ["视图"], False),
    # ── 域外（应当拒答）──
    ("域外", "今天北京天气怎么样", [], True),
    ("域外", "如何用 Python 写一个网页爬虫", [], True),
    ("域外", "卫星太阳能板的清洗周期是多久", [], True),
    ("域外", "红烧肉怎么做", [], True),
    ("域外", "明天股市会涨吗", [], True),
]


_BASE = os.path.join(_ROOT, "tools", "verify", "baselines")


def _match_doc(source_doc: str, keys: list) -> bool:
    s = str(source_doc or "")
    return any(k in s for k in keys)


def run(tag: str):
    from agent.rag import GraphRAG
    g = GraphRAG()
    rows = []
    for typ, q, keys, expect_empty in QUERIES:
        t = time.time()
        try:
            r = g.retrieve(q)
        except Exception as e:
            rows.append({"type": typ, "query": q, "err": "%s: %s" % (type(e).__name__, e)})
            print("  EXC %-46s %s" % (q[:46], type(e).__name__))
            continue
        ms = int((time.time() - t) * 1000)
        hits = r.get("chunk_hits") or []
        docs = [str(h.get("source_doc") or "") for h in hits]
        rank = 0
        for i, d in enumerate(docs[:5], 1):
            if _match_doc(d, keys):
                rank = i
                break
        lc = r.get("low_conf") or {}
        rows.append({
            "type": typ, "query": q, "expect_empty": expect_empty,
            "route": r.get("route"), "ms": ms,
            "n_hits": len(hits), "top5_docs": docs[:5],
            "hit5": bool(rank), "rank": rank,
            "conf": r.get("confidence"),
            "degraded": bool(lc.get("degraded")),
            "nominal": lc.get("nominal"), "kept": lc.get("kept"), "dropped": lc.get("dropped"),
        })
        flag = "空" if not hits else ("第%d位" % rank if rank else "有命中但不对")
        print("  %-8s %-42s hits=%-3d %-10s %6dms" % (typ, q[:42], len(hits), flag, ms))

    # ── 汇总 ──
    ans = [r for r in rows if r.get("expect_empty") is False]
    rej = [r for r in rows if r.get("expect_empty") is True]
    hit5 = sum(1 for r in ans if r.get("hit5"))
    mrr = statistics.mean([(1.0 / r["rank"]) if r.get("rank") else 0.0 for r in ans]) if ans else 0.0
    rej_ok = sum(1 for r in rej if not r.get("n_hits"))
    summary = {
        "tag": tag,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_queries": len(QUERIES),
        "answerable": len(ans),
        "hit@5": round(hit5 / len(ans), 4) if ans else 0.0,
        "recall@5": round(hit5 / len(ans), 4) if ans else 0.0,
        "mrr": round(mrr, 4),
        "reject_acc": round(rej_ok / len(rej), 4) if rej else 0.0,
        "avg_ms": int(statistics.mean([r["ms"] for r in rows if "ms" in r])) if rows else 0,
        "degraded_ratio": round(sum(1 for r in ans if r.get("degraded")) / len(ans), 3) if ans else 0.0,
        "zero_hit_answerable": sum(1 for r in ans if not r.get("n_hits")),
        "rows": rows,
    }
    os.makedirs(_BASE, exist_ok=True)
    out = os.path.join(_BASE, "%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary, out


def show(s):
    print("\n" + "=" * 78)
    print("基线 [%s]  %s" % (s["tag"], s["ts"]))
    print("=" * 78)
    print("  可答 query        : %d（其中零命中 %d 条）" % (s["answerable"], s["zero_hit_answerable"]))
    print("  hit@5 / recall@5  : %.4f" % s["hit@5"])
    print("  MRR               : %.4f" % s["mrr"])
    print("  拒答准确率        : %.4f" % s["reject_acc"])
    print("  平均耗时          : %d ms" % s["avg_ms"])
    print("  降级态占比        : %.3f" % s["degraded_ratio"])


def compare(a, b):
    """a=基线（旧），b=本次（新）。正数=变好。"""
    print("\n" + "=" * 78)
    print("对比：%s → %s" % (a["tag"], b["tag"]))
    print("=" * 78)
    print("  %-18s %-12s %-12s %s" % ("指标", a["tag"], b["tag"], "变化"))
    for k, better_high in (("hit@5", True), ("mrr", True), ("reject_acc", True),
                           ("avg_ms", False), ("zero_hit_answerable", False),
                           ("degraded_ratio", False)):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            continue
        d = vb - va
        good = (d > 0) if better_high else (d < 0)
        arrow = "↑好" if (d != 0 and good) else ("↓差" if d != 0 else "持平")
        print("  %-18s %-12s %-12s %+.4f  %s" % (k, va, vb, d, arrow))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="current")
    ap.add_argument("--compare", default="")
    a = ap.parse_args()

    print("=" * 78)
    print("检索质量基线 — tag=%s" % a.tag)
    try:
        from core import config as c
        print("  top_k=%s inject_k=%s floor=%s rerank=%s threshold=%s"
              % (c.get("rag", "top_k"), c.get("rag", "inject_k"),
                 c.get("rag", "low_conf_floor"), c.get("rag", "rerank_enabled"),
                 c.get("rag", "route_threshold")))
    except Exception:
        pass
    print("=" * 78)

    s, path = run(a.tag)
    show(s)
    print("\n已写：%s" % path)

    if a.compare:
        old_p = os.path.join(_BASE, "%s.json" % a.compare)
        if os.path.exists(old_p):
            with open(old_p, encoding="utf-8") as f:
                compare(json.load(f), s)
        else:
            print("\n⚠️ 找不到对比基线：%s" % old_p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
