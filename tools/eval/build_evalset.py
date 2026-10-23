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

## P1-1（2026-10-18 自进化调研路线图）：Trace2Dataset
本工具**默认附带**构建 trace 评测集（tmp/p21/trace_evalset.json）——生产证据链
（会话 / HIL 裁决 / 意图样本 / 编排运行 / P0-3 Bad Case 台账）→ 可复用评测样本，
AgentLoop「生产轨迹 → 评测集 → 优化 → 再部署」闭环的供料环。实现见同目录
trace2dataset.py（抽取器注册表 + 指纹 + 漂移拒绝）。`--no-trace` 退回纯检索集
构建（旧行为，输出字节级不变）。
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
    # ── 域外闲聊（语料里没有任何可答内容）──────────────────────────
    ("域外闲聊", "帮我写一首关于春天的诗"),
    ("域外闲聊", "推荐几部好看的科幻电影"),
    ("域外闲聊", "帮我起一个猫咪的名字"),
    ("域外闲聊", "红烧肉怎么做才不腻"),
    ("域外闲聊", "明天中午吃什么比较好"),
    # ── 域外常识/实时信息（语料无法回答，且外部数据也不该由检索答）──
    ("域外常识", "今天北京的天气怎么样"),
    ("域外常识", "现在美元兑人民币汇率多少"),
    ("域外常识", "本周A股大盘收盘点位是多少"),
    ("域外实时", "我现在账户余额还有多少"),
    # ── 域内但不存在（同一领域，语料没有这个具体量）──────────────
    ("域内但不存在", "空间站机械臂的抓取力是多少"),
    ("域内但不存在", "巡飞待机弹的推进剂加注量是多少"),
    ("域内但不存在", "热管理系统冷却回路的流量是多少"),
    ("域内但不存在", "宽带通信卫星的太阳能板清洗周期"),
    ("域内但不存在", "雷达天线阵面的驻波比是多少"),
    ("域内但不存在", "导弹测试靶场的年度经费预算是多少"),
    ("域内但不存在", "某型号电源模块的开关频率上限是多少"),
    ("域内但不存在", "专利号CN123456789A 的权利要求有哪些"),
    # ── 术语邻域但不相关（用了工程术语，语料里没有这条知识）────────
    ("术语邻域但不相关", "如何用 Python 做网页爬虫"),
    ("术语邻域但不相关", "用 Docker 部署一个 Postgres 主从集群"),
    ("术语邻域但不相关", "Rust 怎么实现零拷贝解析"),
    ("术语邻域但不相关", "帮我配置一下 Nginx 的反向代理"),
    # ── 域内但需外部/私有数据（语料本就不该有这些）────────────────
    ("需外部数据", "我们这周的排产计划是什么"),
    ("需外部数据", "当前项目里还剩多少未关闭的缺陷"),
    ("需外部数据", "帮我查一下我上周提交的审批单批了没"),
    ("需外部数据", "现在的备件库存里还有几套动力电池"),
    # ── 域内但时间/版本外（语料只有已发布版本与历史条款）──────────
    ("时间外", "智源平台 v3.0 的需求规格说明有哪些"),
    ("时间外", "GB/T 标准 2027 修订版对接口一致性的要求是什么"),
    ("时间外", "下一版本计划新增哪些建模节点"),
    # ── 跨域拼接（两个域各一半，语料无对应条目）────────────────────
    ("跨域拼接", "把巡飞弹的推进剂参数和汽车热管理器的冷却回路画成一张图"),
    ("跨域拼接", "用 SysML v2 建模一颗卫星的太阳能板清洗流程并给需求追溯矩阵"),
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
        # ── 2026-10-14 补两条（都是实测踩出来的，不是预防性说明）──
        "⚠️ doc_cases 的 gold 是**位置键**(source_doc, chunk_index)。语料一重切，"
        "同一 index 指向的内容就换了 ⇒ 实测旧集 292 条 gold 中 288 条（99%）失效，"
        "而当时只打了「建议重建」的 warning 就继续打分。",
        "⚠️ gold 已按**section 全量块**聚合（不是只取第一块）：一个 section 常跨 5~9 块，"
        "只认第一块会把「命中兄弟块」误判为失败（实测块级 38.6% vs section 级 54.5%，"
        "15.9pp 是判据吃掉的）。",
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

# ── P1-1（2026-10-18 自进化调研路线图）：Trace2Dataset ─────────────────────
# 生产证据链 → 评测集（默认随本工具一起构建；--no-trace 退回纯检索集旧行为）。
# 实现与抽取器注册表在 tools/eval/trace2dataset.py（只读取材，绝不写库）。
if "--no-trace" in sys.argv:
    print("\n（--no-trace：跳过 P1-1 trace 评测集，仅构建检索评测集）")
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import trace2dataset

    print()
    print("=" * 78)
    print("步骤 5/4  P1-1 trace 域：生产证据链 → 评测样本")
    print("=" * 78)
    trace = trace2dataset.build(conn)
    trace_out = trace2dataset.default_out_path()
    trace2dataset.write(trace_out, trace)
    tc = trace["counts"]
    print("  按抽取器: %s" % tc["by_extractor"])
    print("  按标注口径: %s" % tc["by_label"])
    _sk = trace["summary"].get("skipped_extractors") or {}
    if _sk:
        print("  跳过证据源（老库缺表等，不连坐其它源）: %s" % _sk)
    if trace["summary"].get("skipped_mock"):
        print("  排除 Mock 回答 %d 条（Mock 不是系统能力证据）"
              % trace["summary"]["skipped_mock"])
    print("→ %s" % trace_out)
    print("   %.1f KB | %d 例（strong=人工裁决 / weak=回归证据 / negative=已知失败）"
          % (os.path.getsize(trace_out) / 1024, tc["total"]))
    print("   trace 指纹: messages=%s intent_confirmed=%s hil=%s"
          % (trace["fingerprint"].get("messages"),
             trace["fingerprint"].get("intent_samples"),
             trace["fingerprint"].get("hil_confirmations")))

# ── O2（2026-10-23）：评测集版本化 ─────────────────────────────────────────
# 此前产物只在 tmp/p21/（gitignore + 会被清理）⇒ 历史分数不可比、评测集
# 不可 review。规范落点改为 docs/评测/datasets/（随 git，PR 可评审），tmp
# 保留一份副本兼容旧路径；manifest 记 dataset_id。
#
# dataset_id 是**内容寻址**：由"语料指纹 + 证据源指纹 + 样本量"派生 ——
# 同一语料/证据状态重建 ⇒ 同 id（built_at 不参与，重建不改 id）；
# 语料或证据源一变 ⇒ id 变 ⇒ 历史分数自然分段可比。评测报告记 dataset_id
# 后，"这份分数是哪一版题库上跑的"第一次有了机器可查的答案。
DATASETS_DIR = os.path.join(REPO, "docs", "评测", "datasets")


def _dataset_id(finger, trace_finger, n_doc, n_graph, n_neg, trace_total):
    h = hashlib.sha1()
    h.update(str(sorted((finger or {}).items())).encode("utf-8"))
    h.update(str(sorted((trace_finger or {}).items())).encode("utf-8"))
    h.update(("%s|%s|%s|%s" % (n_doc, n_graph, n_neg, trace_total)).encode("utf-8"))
    return h.hexdigest()[:12]


def _publish_datasets(trace):
    """把评测集发布到规范落点 + 写 manifest。返回 dataset_id。"""
    os.makedirs(DATASETS_DIR, exist_ok=True)
    # 副本（字节级一致；tmp 为兼容旧读者的过渡路径）
    import shutil as _shutil
    _shutil.copyfile(OUT, os.path.join(DATASETS_DIR, "evalset.json"))
    d_id = _dataset_id(finger, (trace or {}).get("fingerprint"),
                       len(doc_cases), len(graph_cases), len(neg_cases),
                       ((trace or {}).get("counts") or {}).get("total", 0))
    manifest = {
        "schema": "dataset-manifest@1",
        "dataset_id": d_id,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generator": {"build_evalset": "1", "trace2dataset": "p1-1/trace-evalset@1"},
        "evalset": {"file": "evalset.json", "n_doc": len(doc_cases),
                    "n_graph": len(graph_cases), "n_neg": len(neg_cases),
                    "fingerprint": finger},
        "trace": ({"file": "trace_evalset.json",
                   "counts": (trace or {}).get("counts"),
                   "fingerprint": (trace or {}).get("fingerprint")}
                  if trace else None),
        "note": "dataset_id 内容寻址（语料/证据指纹 + 样本量）；built_at 不参与",
    }
    with open(os.path.join(DATASETS_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    if trace:
        _shutil.copyfile(trace_out, os.path.join(DATASETS_DIR, "trace_evalset.json"))
    print()
    print("→ %s（dataset_id=%s）"
          % (os.path.relpath(DATASETS_DIR, REPO).replace("\\", "/"), d_id))
    print("   评测集已版本化：随 git 可 review；报告消费 manifest.json 取 dataset_id")
    return d_id


_publish_datasets(trace if "--no-trace" not in sys.argv else None)
