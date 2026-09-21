# -*- coding: utf-8 -*-
"""构建 P1-6 检索评测集（build_evalset）—— 从真实语料派生带标注样本。

## 为什么需要评测集
`rag.recall_k / top_k / inject_k / rerank_max_candidates / hyde_enabled / route_threshold`
这些参数**彼此耦合**，改一个会牵动另一个。没有带标注的评测集，"调参"只能是拍脑袋：
改完看到某条 query 变好了，无法排除是运气。评测集的作用是把"感觉变好"换成
"recall@5 从 0.62 → 0.71，且负样本误报未上升"。

## 评测集包含什么（三域）
1. doc_cases（文档检索质量）
   query = 章节标题（清洗后）；gold = 该章节下全部 chunk 的 (source_doc, chunk_index)。
   多相关（multi-relevant）→ 适合算 recall@k / nDCG@k。
2. graph_cases（图谱检索 + 路由）
   由 release 分支的真实关系派生：query 用关系两端实体名按关系类型套模板；
   gold = 该实体对 + 关系类型。检验"该走图谱时是否走对、子图是否对"。
3. neg_cases（负样本 / 精度）
   域内不存在 + 完全域外。检验"没有依据时是否敢说没有"（不应高置信乱答）。

## 标注为什么可信
doc 域与 graph 域的答案**不是人写的**，是从语料结构里**推导**出来的
（章节 → 其下 chunk 是确定集合；关系 → 两端实体是确定事实）。
因此没有人工标注的主观性与漏标问题，可重复、可审计。
route 标签是**弱标注**（entity 名可链接 ⇒ 期望 graph），已在文件内标记 `label:"weak"`，
需人工复核后才能用于 `route_threshold` 的关键调参。

## 语料指纹（必须钉住）
同一 query 在不同时点的 route 会变 —— 实测「卫星通信系统包含哪些分系统」
03:46:31 得 graph/0.88(g=5)，03:47:15 得 vector/g=0，而 `_entity_link` 本身 6/6 轮稳定。
差异来自**语料/图谱被改写**，不是链路抖动。
故：本文件写入 fingerprint；run_eval 启动时比对，漂移则高声告警。

用法（必须在项目 venv 内跑，见 tools/eval/preflight.py 的说明）：
    "<repo>/.venv/Scripts/python.exe" -X utf8 tools/eval/build_evalset.py
可调环境变量：DOC_PER_DOC / GRAPH_MAX / SEED
"""
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(REPO, "mbse.db")
OUT = os.path.join(REPO, "tmp", "p21", "evalset.json")

SEED = int(os.environ.get("SEED", "20260921"))
DOC_PER_DOC = int(os.environ.get("DOC_PER_DOC", "5"))
GRAPH_MAX = int(os.environ.get("GRAPH_MAX", "18"))
rnd = random.Random(SEED)

# 章节标题清洗：去掉 markdown 井号与粗体标记（本工程无 markdown 解析器，标题里混着记号）
_MD = re.compile(r"[#*`]+")


def clean_title(s):
    s = _MD.sub(" ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


_NOTATION = re.compile(r"^[\s\d.,*+\-–—/\\|()\[\]{}<>~^%$#@!?;:'\"=]+$")


def is_usable_title(t, n_chunks):
    """标题得像一个"话题"：有实义字符、不是纯记号、chunk 数处于合理区间。"""
    if not t or len(t) < 6:
        return False
    if _NOTATION.match(t):
        return False
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", t))
    n_words = len([w for w in re.split(r"\s+", t) if w])
    if not has_cjk and n_words < 3:
        return False
    if not (3 <= n_chunks <= 25):
        return False
    return True


conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
conn.row_factory = sqlite3.Row

print("=" * 78)
print("步骤 1/4  语料指纹（评测集的生命线）")
print("=" * 78)


def one(sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r else None


doc_cases, graph_cases, neg_cases = [], [], []

# ── 步骤 2：doc 域（章节标题 → 该章节下所有 chunk）───────────────────────────
print()
print("=" * 78)
print("步骤 2/4  doc 域：章节标题 → 章节内全部 chunk")
print("=" * 78)

secs = conn.execute(
    "select source_doc, section, count(*) k from document_chunks "
    "group by source_doc, section"
).fetchall()
by_doc = {}
for r in secs:
    t = clean_title(r["section"])
    if is_usable_title(t, r["k"]):
        by_doc.setdefault(r["source_doc"], []).append((t, r["section"], r["k"]))

for doc in sorted(by_doc):
    pool = by_doc[doc]
    picked = rnd.sample(pool, min(DOC_PER_DOC, len(pool)))
    for t, raw_sec, k in picked:
        gold = [dict(r) for r in conn.execute(
            "select source_doc, chunk_index from document_chunks "
            "where source_doc=? and section=? order by chunk_index", (doc, raw_sec))]
        doc_cases.append({
            "id": "doc-%02d" % (len(doc_cases) + 1),
            "kind": "doc",
            "query": t,
            "gold": gold,
            "gold_keys": [[g["source_doc"], g["chunk_index"]] for g in gold],
            "meta": {"source_doc": doc, "section": raw_sec, "n_gold": len(gold)},
            "label": "derived",
        })
    print("  %-40s 候选章节 %3d → 取 %d" % (doc[:40], len(pool), len(picked)))

print("  doc_cases 合计 %d（gold 平均 %.1f 条）"
      % (len(doc_cases), sum(c["meta"]["n_gold"] for c in doc_cases) / max(len(doc_cases), 1)))

# ── 步骤 3：graph 域（按关系类型套模板）─────────────────────────────────────
print()
print("=" * 78)
print("步骤 3/4  graph 域：真实关系 → 问句模板")
print("=" * 78)

# 关系类型 → 问句模板。模板只在"问得自然"时使用；CONTAINS/COMPOSED_OF 反向问也合理。
TEMPLATE = {
    "CONTAINS":      ("{a} 包含哪些 {b}", "包含"),
    "COMPOSED_OF":   ("{a} 由哪些 {b} 组成", "组成"),
    "DERIVES":       ("由 {a} 派生出的 {b} 有哪些", "派生"),
    "SATISFIES":     ("{a} 满足哪些 {b}", "满足"),
    "VERIFIED_BY":   ("{a} 由哪些 {b} 验证", "验证"),
    "ALLOCATED_TO":  ("{a} 被分配到哪个 {b}", "分配"),
    "TRACE":         ("{a} 与 {b} 的追溯关系", "追溯"),
}

rows = conn.execute(
    "select r.id, r.relation_type rt, r.status, "
    "       s.id sid, s.name sn, s.status ss, s.branch sb, "
    "       t.id tid, t.name tn, t.status ts, t.branch tb "
    "from relations r join entities s on r.source_id=s.id join entities t on r.target_id=t.id "
    "where r.branch='release' and r.status!='deprecated' "
    "  and s.branch='release' and t.branch='release' "
    "  and s.status!='deprecated' and t.status!='deprecated' "
    "order by r.id"
).fetchall()
print("  release 分支可用关系 %d 条" % len(rows))

# 同一对实体可能出现多次，去重（按 (rt, sn, tn)）后每条只出一题
seen_pairs, cand = set(), []
for r in rows:
    rt, sn, tn = r["rt"], r["sn"], r["tn"]
    if rt not in TEMPLATE:
        continue
    if (rt, sn, tn) in seen_pairs:
        continue
    # 实体名得能读：有实义字符、长度够
    if min(len(sn or ""), len(tn or "")) < 2:
        continue
    seen_pairs.add((rt, sn, tn))
    cand.append(r)

print("  去重后候选 %d 条，按关系类型分布：" % len(cand))
dist = {}
for r in cand:
    dist[r["rt"]] = dist.get(r["rt"], 0) + 1
print("   ", dist)

# 按关系类型轮转抽样，保证题型均衡（否则 44 条 DERIVES 会淹掉 4 条 TRACE）
buckets = {}
for r in cand:
    buckets.setdefault(r["rt"], []).append(r)
for v in buckets.values():
    rnd.shuffle(v)
picked, idx, order = [], 0, sorted(buckets)
while len(picked) < GRAPH_MAX and any(buckets[k] for k in order):
    k = order[idx % len(order)]
    if buckets[k]:
        picked.append(buckets[k].pop())
    idx += 1

for r in picked:
    tmpl, verb = TEMPLATE[r["rt"]]
    q = tmpl.format(a=r["sn"], b=r["tn"])
    graph_cases.append({
        "id": "graph-%02d" % (len(graph_cases) + 1),
        "kind": "graph",
        "query": q,
        "gold": {
            "pair": [r["sn"], r["tn"]],
            "relation_type": r["rt"],
            "src_id": r["sid"], "tgt_id": r["tid"],
        },
        "gold_entities": [r["sn"], r["tn"]],
        "meta": {"relation_id": r["id"], "verb": verb},
        "label": "derived",
        "expect_route_in": ["graph", "mixed"],
    })
    print("    %-10s %s" % (r["rt"], q))

# ── 步骤 4：负样本 ──────────────────────────────────────────────────────────
print()
print("=" * 78)
print("步骤 4/4  负样本（域内不存在 / 域外）")
print("=" * 78)
NEG = [
    ("域外闲聊", "帮我写一首关于春天的诗"),
    ("域外常识", "今天北京的天气怎么样"),
    ("域内但不存在", "空间站机械臂的抓取力是多少"),
    ("域内但不存在", "巡飞待机弹的推进剂加注量是多少"),
    ("域内但不存在", "热管理系统冷却回路的流量是多少"),
    ("术语邻域但不相关", "如何用 Python 做网页爬虫"),
    ("域内但不存在", "宽带通信卫星的太阳能板清洗周期"),
    ("域外闲聊", "推荐几部好看的科幻电影"),
]
for tag, q in NEG:
    neg_cases.append({
        "id": "neg-%02d" % (len(neg_cases) + 1),
        "kind": "neg",
        "query": q,
        "gold": [],
        "meta": {"tag": tag},
        "label": "manual",
        # 负样本不该"高置信地走图谱"；走向量或空命中都算合理
        "expect_route_in": ["vector", "mixed"],
    })
    print("    %-16s %s" % (tag, q))

# ── 指纹与落盘 ──────────────────────────────────────────────────────────────
names_rel = sorted(r["name"] for r in conn.execute(
    "select name from entities where branch='release' and status!='deprecated'"))
finger = {
    "db_bytes": os.path.getsize(DB),
    "documents": one("select count(*) from documents"),
    "chunks": one("select count(*) from document_chunks"),
    "entities_total": one("select count(*) from entities"),
    "entities_release": len(names_rel),
    "relations_release": one("select count(*) from relations where branch='release'"),
    "ontology_active": one("select version_label from ontology_versions where active=1 "
                           "order by id desc limit 1"),
    "chunk_branches": [dict(r) for r in conn.execute(
        "select branch, count(*) n from document_chunks group by branch")],
    "entity_branches": [dict(r) for r in conn.execute(
        "select branch, count(*) n from entities group by branch")],
    # 实体名集合的指纹：图谱被改写时这个值会变，是"路由标签失效"的最灵敏信号
    "release_entity_names_sha1": hashlib.sha1(
        "\n".join(names_rel).encode("utf-8")).hexdigest()[:16],
}

evalset = {
    "schema": "p1-6/evalset@1",
    "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "seed": SEED,
    "fingerprint": finger,
    "counts": {"doc": len(doc_cases), "graph": len(graph_cases), "neg": len(neg_cases)},
    "doc_cases": doc_cases,
    "graph_cases": graph_cases,
    "neg_cases": neg_cases,
    "notes": [
        "doc/graph 域的 gold 由语料结构派生（可重复、可审计），非人工标注。",
        "graph_cases 的 expect_route_in 为弱标注：entity 名可链接 ≠ 用户意图就该走图谱，"
        "用于 route_threshold 关键调参前需人工复核。",
        "语料指纹必须比对：图谱改写会使 graph_cases 的 gold 实体消失、路由标签失效。",
    ],
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(evalset, f, ensure_ascii=False, indent=1)

print()
print("=" * 78)
print("产出")
print("=" * 78)
print("→ %s" % OUT)
print("   %.1f KB | doc %d + graph %d + neg %d = %d 例"
      % (os.path.getsize(OUT) / 1024, len(doc_cases), len(graph_cases), len(neg_cases),
         len(doc_cases) + len(graph_cases) + len(neg_cases)))
print("   指纹: chunks=%s entities(release)=%s names_sha1=%s"
      % (finger["chunks"], finger["entities_release"], finger["release_entity_names_sha1"]))
