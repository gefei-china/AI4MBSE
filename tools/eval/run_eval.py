# -*- coding: utf-8 -*-
"""跑 P1-6 检索评测集并出指标（run_eval）。

配套 tools/eval/build_evalset.py 生成的 tmp/p21/evalset.json。

## 指标口径（重要：分两层，别混着看）
文档检索被拆成两段漏斗，只报一个总分看不出该调哪一段：

    P 层（召回池）：hybrid_search 的 RRF 排序（**关掉重排**）
        → pool_recall@k 回答"候选池里到底有没有正确答案"
        → 池里没有 = 调大 rag.recall_k / top_k 才有用
    F 层（注入前）：retrieve() 返回的 chunk_hits（**重排已生效**）
        → recall@k / MRR / nDCG@10 回答"最终给出的顺序好不好"
        → 池里有但 F 层没有 = 重排把对的挤下去了，调 rerank_max_candidates / 重排策略

这个分解正是决定 `top_k` 与 `rerank_max_candidates` 该往哪调的判据。

## 三道纪律（都已实测踩过）
1. **必须比对语料指纹**：图谱改写会让 graph_cases 的 gold 实体消失、路由标签失效。
   实测同 query 在语料变更前后 route 由 graph/0.88 变 vector/0.0 —— 不比对就会把
   "语料变了"误读成"参数调坏了"。
2. **不污染线上统计**：评测会调 retrieve()，而它会写 query_routing_stats。
   本脚本把 `QueryRouter.record` 打成 no-op，评测 query 不进线上路由日志。
3. **必须用项目 venv 跑**：managed python 无 httpx → 链路静默降级
   （embedding 回落 bigram、LLM 重排回落 Mock），测的是另一条链路的分数。
   本脚本启动时自检 httpx，缺失直接拒跑。

用法：
    "<repo>/.venv/Scripts/python.exe" -X utf8 tools/eval/run_eval.py --tag baseline
    ... --subset doc --limit 5
    ... --tag after --baseline tmp/p21/eval_report_baseline.json
"""
import argparse
import io
import json
import math
import os
import statistics
import sys
import time
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

ap = argparse.ArgumentParser()
ap.add_argument("--set", default=os.path.join(REPO, "tmp", "p21", "evalset.json"))
ap.add_argument("--tag", default="run")
ap.add_argument("--subset", default="all", choices=["all", "doc", "graph", "neg"])
ap.add_argument("--limit", type=int, default=0, help="每域取前 N 条（调试用）")
ap.add_argument("--pool", type=int, default=0, help="召回池宽度（默认=rag.recall_k）")
ap.add_argument("--baseline", default="", help="上轮报告 JSON，用于出对比")
ap.add_argument("--out", default="", help="报告输出路径")
ap.add_argument("--no-pool", action="store_true", help="跳过 P 层召回池测量（省一轮检索，约减半耗时）")
ap.add_argument("--control", action="store_true",
                help="附带负对照：把 doc 域 gold 标签在各例之间轮转后重算指标。"
                     "若轮转后的分数与真实分数相近 → 指标没在测对齐关系，结果是假绿。")
ap.add_argument("--cfg", action="append", default=[], metavar="GROUP.KEY=VALUE",
                help="临时覆写生效配置（可重复，如 --cfg rag.hyde_enabled=false）。"
                     "只改本进程内存，不落盘、不动生产配置 → 用于 A/B 单参数对照。")
ARGS = ap.parse_args()

# ── 纪律 3：解释器自检（缺失 httpx 会静默降级，必须拒跑）────────────────────
try:
    import httpx  # noqa: F401
except Exception as e:
    print("⛔ 当前解释器缺 httpx（%s）→ 检索链路会静默降级为 bigram/Mock，分数无意义。" % e)
    print('   请改用："%s\\.venv\\Scripts\\python.exe" -X utf8 tools/eval/run_eval.py' % REPO)
    sys.exit(2)

from agent.rag import GraphRAG
from database import get_db
from knowledge_engine import QueryRouter, hybrid_search

# ── --cfg 单参数覆写（A/B 用）：只改本进程内存，不落盘 ──────────────────────
_CFG_APPLIED = []
if ARGS.cfg:
    from core import config as _cfgmod
    _cfgmod.reload()                      # 确保 _CONFIG 已按三层合并初始化
    for item in ARGS.cfg:
        assert "=" in item and "." in item.split("=")[0], f"--cfg 格式应为 GROUP.KEY=VALUE：{item}"
        path, raw = item.split("=", 1)
        sec, key = path.strip().split(".", 1)
        cur = _cfgmod.get(sec, key, None)
        if isinstance(cur, bool):
            new = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            new = int(raw)
        elif isinstance(cur, float):
            new = float(raw)
        else:
            new = raw
        _cfgmod._CONFIG.setdefault(sec, {})[key] = new
        _CFG_APPLIED.append((path.strip(), cur, new))
    print("⚙ 本次覆写（仅本进程）：" + "；".join(f"{p}: {o} → {n}" for p, o, n in _CFG_APPLIED))

# ── 纪律 2：评测不写线上路由日志 ────────────────────────────────────────────
_orig_record = QueryRouter.record
QueryRouter.record = lambda *a, **k: None

evalset = json.load(open(ARGS.set, encoding="utf-8"))
print("=" * 78)
print("P1-6 检索评测  tag=%s" % ARGS.tag)
print("=" * 78)
print("评测集 : %s（%s，seed=%s）" % (os.path.basename(ARGS.set), evalset["built_at"], evalset["seed"]))
print("构成   : doc %d / graph %d / neg %d"
      % (evalset["counts"]["doc"], evalset["counts"]["graph"], evalset["counts"]["neg"]))

# ── 纪律 1：语料指纹比对 ────────────────────────────────────────────────────
conn0 = get_db()
now_finger = {}
try:
    import hashlib
    names_rel = sorted(r["name"] for r in conn0.execute(
        "select name from entities where branch='release' and status!='deprecated'"))
    now_finger = {
        "chunks": conn0.execute("select count(*) from document_chunks").fetchone()[0],
        "entities_release": len(names_rel),
        "release_entity_names_sha1": hashlib.sha1(
            "\n".join(names_rel).encode("utf-8")).hexdigest()[:16],
    }
finally:
    conn0.close()

old = evalset["fingerprint"]
drift = []
if old.get("chunks") != now_finger["chunks"]:
    drift.append("chunks %s → %s" % (old.get("chunks"), now_finger["chunks"]))
if old.get("release_entity_names_sha1") != now_finger["release_entity_names_sha1"]:
    drift.append("release 实体名集合 %s → %s（图谱已被改写）"
                 % (old.get("release_entity_names_sha1"), now_finger["release_entity_names_sha1"]))
if old.get("entities_release") != now_finger["entities_release"]:
    drift.append("release 实体数 %s → %s" % (old.get("entities_release"), now_finger["entities_release"]))

print()
if drift:
    print("⚠️  语料指纹漂移 —— 本轮分数与建集时不同源，解读必须谨慎：")
    for d in drift:
        print("     • %s" % d)
    print("    建议：重建评测集（build_evalset.py）后再对比。")
else:
    print("✅ 语料指纹一致（chunks=%s, release 实体名 sha1=%s）"
          % (now_finger["chunks"], now_finger["release_entity_names_sha1"]))

cfg_rag = {}
try:
    from core import config as _cfg
    for k in ("recall_k", "top_k", "inject_k", "rerank_enabled", "rerank_max_candidates",
              "hyde_enabled", "route_threshold", "retrieval_weight", "bm25_weight"):
        cfg_rag[k] = _cfg.get("rag", k)
except Exception as e:
    print("   （读配置失败：%s）" % e)
POOL = ARGS.pool or int(cfg_rag.get("recall_k") or 20)
print("生效配置 : recall_k=%s top_k=%s inject_k=%s rerank=%s(max=%s) hyde=%s route_thr=%s"
      % (cfg_rag.get("recall_k"), cfg_rag.get("top_k"), cfg_rag.get("inject_k"),
         cfg_rag.get("rerank_enabled"), cfg_rag.get("rerank_max_candidates"),
         cfg_rag.get("hyde_enabled"), cfg_rag.get("route_threshold")))
print("召回池宽度 : %d（P 层口径）" % POOL)

rag = GraphRAG()

# ── P 层：关掉重排拿纯 RRF 排序（用于"池里有没有"）────────────────────────
import services.rag_rerank as _rr
_orig_rerank = _rr.llm_rerank


def _pool_rank(query):
    _rr.llm_rerank = lambda q, hits, top_k=5, max_candidates=8, timeout=25: hits
    try:
        conn = get_db()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                hy = hybrid_search(conn, query, top_k=max(POOL, 50), branches=None)
            return hy["hits"], hy
        finally:
            conn.close()
    finally:
        _rr.llm_rerank = _orig_rerank


K_LIST = [1, 3, 5, 10]


def dcg(rels, k=10):
    return sum((1.0 if rels[i] else 0.0) / math.log2(i + 2) for i in range(min(len(rels), k)))


def eval_doc(case):
    q = case["query"]
    gold = {tuple(x) for x in case["gold_keys"]}
    t0 = time.time()
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = rag.retrieve(q, branch="dev")
    t_final = time.time() - t0
    hits = r.get("chunk_hits") or []
    final_keys = [(h.get("source_doc"), h.get("chunk_index")) for h in hits]

    t0 = time.time()
    if ARGS.no_pool:
        pool_hits, t_pool = [], 0.0
    else:
        pool_hits, _ = _pool_rank(q)
        t_pool = time.time() - t0
    pool_keys = [(h.get("source_doc"), h.get("chunk_index")) for h in pool_hits]

    rec = {("recall@%d" % k): (1.0 if gold & set(final_keys[:k]) else 0.0) for k in K_LIST}
    rank = next((i + 1 for i, kk in enumerate(final_keys[:10]) if kk in gold), 0)
    rels = [1.0 if kk in gold else 0.0 for kk in final_keys[:10]]
    idcg = dcg([1.0] * min(len(gold), 10))
    out = {
        "id": case["id"], "kind": "doc", "query": q,
        "n_gold": len(gold),
        "recall": rec,
        "mrr@10": (1.0 / rank) if rank else 0.0,
        "ndcg@10": (dcg(rels) / idcg) if idcg else 0.0,
        "pool_recall": (1.0 if gold & set(pool_keys[:POOL]) else 0.0) if pool_keys else None,
        "n_final": len(final_keys), "n_pool": len(pool_keys),
        "route": r.get("route"),
        "latency_final_ms": int(t_final * 1000), "latency_pool_ms": int(t_pool * 1000),
        "gold_in_pool_rank": next((i + 1 for i, kk in enumerate(pool_keys) if kk in gold), 0),
        # 落库便于离线复算/负对照（不重跑检索也能换标签重打分）
        "final_keys": final_keys,
        "pool_keys": pool_keys[:POOL],
    }
    return out


def eval_graph(case):
    q = case["query"]
    g = case["gold"]
    t0 = time.time()
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = rag.retrieve(q, branch="dev")
    lat = int((time.time() - t0) * 1000)
    ent_names = {e.get("name") for e in (r.get("entities") or [])}
    pair = set(g["pair"])
    # 关系是否命中：返回的关系里存在一对名称等于 gold 两端（方向不限）
    rel_ok = any(
        {x.get("source_name"), x.get("target_name")} == pair
        for x in (r.get("relations") or [])
    )
    return {
        "id": case["id"], "kind": "graph", "query": q,
        "relation_type": g["relation_type"], "gold_pair": g["pair"],
        "route": r.get("route"), "expect_route_in": case["expect_route_in"],
        "route_ok": r.get("route") in case["expect_route_in"],
        "entity_hit": len(pair & ent_names),
        "entity_recall": len(pair & ent_names) / 2.0,
        "relation_hit": rel_ok,
        "confidence": round(float(r.get("confidence") or 0), 3),
        "graph_count": r.get("graph_count"),
        "latency_ms": lat,
    }


def eval_neg(case):
    q = case["query"]
    t0 = time.time()
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = rag.retrieve(q, branch="dev")
    lat = int((time.time() - t0) * 1000)
    hits = r.get("chunk_hits") or []
    conf = float(r.get("confidence") or 0)
    thr = float(cfg_rag.get("route_threshold") or 0.75)
    return {
        "id": case["id"], "kind": "neg", "query": q, "tag": case["meta"]["tag"],
        "route": r.get("route"),
        "route_ok": r.get("route") in case["expect_route_in"],
        "n_hits": len(hits),
        "confidence": round(conf, 3),
        # 负样本的"危险动作"：在图谱上高置信命中（等于用无关实体硬答）
        "false_graph_confident": bool(r.get("route") == "graph" and conf >= thr),
        "latency_ms": lat,
    }


EV = {"doc": eval_doc, "graph": eval_graph, "neg": eval_neg}
todo = [k for k in ("doc", "graph", "neg") if ARGS.subset in ("all", k)]
results, t_start = {}, time.time()
for kind in todo:
    cases = evalset["%s_cases" % kind]
    if ARGS.limit:
        cases = cases[:ARGS.limit]
    print()
    print("=" * 78)
    print("跑 %s 域（%d 例）" % (kind, len(cases)))
    print("=" * 78)
    rows = []
    for i, c in enumerate(cases, 1):
        try:
            row = EV[kind](c)
        except Exception as e:
            import traceback
            row = {"id": c["id"], "kind": kind, "query": c["query"], "error": "%s: %s" % (type(e).__name__, e),
                   "trace": traceback.format_exc()[-500:]}
        rows.append(row)
        if kind == "doc":
            print("  [%2d/%2d] %-34s P=%-3s F@5=%-3s ndcg=%.2f pool#%-3s %sms"
                  % (i, len(cases), row.get("query", "")[:34],
                     row.get("pool_recall"), row["recall"]["recall@5"] if "recall" in row else "-",
                     row.get("ndcg@10", 0), row.get("gold_in_pool_rank"), row.get("latency_final_ms")))
        elif kind == "graph":
            print("  [%2d/%2d] %-40s route=%-6s %s  ent=%s rel=%s"
                  % (i, len(cases), row.get("query", "")[:40], row.get("route"),
                     "✅" if row.get("route_ok") else "❌",
                     row.get("entity_hit"), "✅" if row.get("relation_hit") else "❌"))
        else:
            print("  [%2d/%2d] %-30s route=%-6s hits=%-3s conf=%-5s %s"
                  % (i, len(cases), row.get("query", "")[:30], row.get("route"),
                     row.get("n_hits"), row.get("confidence"),
                     "⚠️ 高置信走图谱" if row.get("false_graph_confident") else ""))
        sys.stdout.flush()
    results[kind] = rows

# ── 汇总 ────────────────────────────────────────────────────────────────────
summary = {}
control = None
if "doc" in results:
    ok = [r for r in results["doc"] if "error" not in r]
    if ok:
        pools = [r["pool_recall"] for r in ok if r["pool_recall"] is not None]
        d = {
            "n": len(ok),
            **{k: round(statistics.mean(r["recall"][k] for r in ok), 3) for k in
               ("recall@1", "recall@3", "recall@5", "recall@10")},
            "mrr@10": round(statistics.mean(r["mrr@10"] for r in ok), 3),
            "ndcg@10": round(statistics.mean(r["ndcg@10"] for r in ok), 3),
            "latency_p50_ms": int(statistics.median(r["latency_final_ms"] for r in ok)),
            "latency_pool_p50_ms": int(statistics.median(r["latency_pool_ms"] for r in ok)),
        }
        if pools:
            # 只有跑了 P 层，这两个"该调哪一段"的判据才有意义
            d["pool_recall@%d" % POOL] = round(statistics.mean(pools), 3)
            # 池里有、最终没有 = 被重排挤掉的比例（重排的"损失率"）
            d["rerank_drop_rate"] = round(
                sum(1 for r in ok if r["pool_recall"] and not r["recall"]["recall@10"]) / len(ok), 3)
            # 池里没有 = 召回宽度不足，调重排/阈值都救不回来。
            # 注意：pool_recall 的"未测量"是 None、"命中池"是 1.0、"池里没有"是 0.0。
            # 判"池里没有"必须显式排掉 None，且不能用 `is False`（0.0 不是 False 单例，
            # 那样写会恒为 0 —— 2026-09-21 实测踩到：跨版本对比时把恒 0 当成"改善了 0.1"）。
            d["recall_miss_rate"] = round(
                sum(1 for r in ok if r["pool_recall"] is not None and not r["pool_recall"])
                / len(ok), 3)
        summary["doc"] = d

        # ── 负对照：把 gold 在例间轮转，指标应塌到接近 0；否则说明指标在空转 ──
        if ARGS.control and len(ok) >= 3:
            golds = []
            id2gold = {c["id"]: {tuple(x) for x in c["gold_keys"]} for c in evalset["doc_cases"]}
            for r in ok:
                golds.append(id2gold.get(r["id"], set()))
            rotated = golds[1:] + golds[:1]
            c_rec = {}
            for k in K_LIST:
                c_rec["recall@%d" % k] = round(statistics.mean(
                    1.0 if rotated[i] & {(kk[0], kk[1]) for kk in ok[i]["final_keys"][:k]} else 0.0
                    for i in range(len(ok))), 3)
            c_mrr = []
            for i, r in enumerate(ok):
                keys = r["final_keys"][:10]
                rank = next((j + 1 for j, kk in enumerate(keys) if (kk[0], kk[1]) in rotated[i]), 0)
                c_mrr.append((1.0 / rank) if rank else 0.0)
            control = {"recall": c_rec, "mrr@10": round(statistics.mean(c_mrr), 3),
                       "note": "gold 轮转一例后的分数；应与真实分数差出量级"}
            summary["doc_control"] = {"n": len(ok), **c_rec,
                                      "mrr@10": control["mrr@10"]}
if "graph" in results:
    ok = [r for r in results["graph"] if "error" not in r]
    if ok:
        summary["graph"] = {
            "n": len(ok),
            "route_acc": round(sum(1 for r in ok if r["route_ok"]) / len(ok), 3),
            "entity_recall": round(statistics.mean(r["entity_recall"] for r in ok), 3),
            "relation_hit": round(sum(1 for r in ok if r["relation_hit"]) / len(ok), 3),
            "latency_p50_ms": int(statistics.median(r["latency_ms"] for r in ok)),
        }
if "neg" in results:
    ok = [r for r in results["neg"] if "error" not in r]
    if ok:
        summary["neg"] = {
            "n": len(ok),
            "route_ok": round(sum(1 for r in ok if r["route_ok"]) / len(ok), 3),
            "empty_hit_rate": round(sum(1 for r in ok if r["n_hits"] == 0) / len(ok), 3),
            "false_graph_confident": sum(1 for r in ok if r["false_graph_confident"]),
            "latency_p50_ms": int(statistics.median(r["latency_ms"] for r in ok)),
        }

t_all = time.time() - t_start
print()
print("=" * 78)
print("汇总（耗时 %.0fs）" % t_all)
print("=" * 78)
pretty = {
    "doc": ["n", "pool_recall@%d" % POOL, "recall@1", "recall@3", "recall@5", "recall@10",
            "mrr@10", "ndcg@10", "rerank_drop_rate", "recall_miss_rate",
            "latency_p50_ms", "latency_pool_p50_ms"],
    "doc_control": ["n", "recall@1", "recall@5", "recall@10", "mrr@10"],
    "graph": ["n", "route_acc", "entity_recall", "relation_hit", "latency_p50_ms"],
    "neg": ["n", "route_ok", "empty_hit_rate", "false_graph_confident", "latency_p50_ms"],
}
for kind in ("doc", "doc_control", "graph", "neg"):
    if kind not in summary:
        continue
    print("\n[%s]" % kind)
    for k in pretty[kind]:
        if k in summary[kind]:
            print("  %-24s : %s" % (k, summary[kind][k]))

# 与上轮对比
if ARGS.baseline and os.path.exists(ARGS.baseline):
    base = json.load(open(ARGS.baseline, encoding="utf-8"))
    print()
    print("=" * 78)
    print("对比基线 %s" % os.path.basename(ARGS.baseline))
    print("=" * 78)
    print("  %-24s %-10s %-10s %s" % ("指标", "基线", "本轮", "差"))
    for kind in ("doc", "graph", "neg"):
        for k in pretty.get(kind, []):
            b = (base.get("summary", {}).get(kind) or {}).get(k)
            n = (summary.get(kind) or {}).get(k)
            if b is None or n is None:
                continue
            try:
                d = round(float(n) - float(b), 3)
                flag = "" if abs(d) < 1e-9 else ("  ↑" if d > 0 else "  ↓")
                print("  %-24s %-10s %-10s %+g%s" % ("%s.%s" % (kind, k), b, n, d, flag))
            except Exception:
                print("  %-24s %-10s %-10s" % ("%s.%s" % (kind, k), b, n))

report = {
    "schema": "p1-6/eval-report@1",
    "tag": ARGS.tag,
    "when": time.strftime("%Y-%m-%d %H:%M:%S"),
    "evalset": os.path.basename(ARGS.set),
    "evalset_built_at": evalset["built_at"],
    "fingerprint_evalset": old,
    "fingerprint_now": now_finger,
    "fingerprint_drift": drift,
    "config": cfg_rag,
    "cfg_overrides": [{"path": p, "from": o, "to": n} for p, o, n in _CFG_APPLIED],
    "pool_width": POOL,
    "summary": summary,
    "negative_control": control,
    "rows": results,
    "elapsed_s": round(t_all, 1),
}
out = ARGS.out or os.path.join(REPO, "tmp", "p21", "eval_report_%s.json" % ARGS.tag)
with open(out, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print()
print("→ %s" % out)
QueryRouter.record = _orig_record
