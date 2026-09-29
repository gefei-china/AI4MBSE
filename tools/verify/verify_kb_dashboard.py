# -*- coding: utf-8 -*-
"""知识看板自检（P0 口径 + P1 IA + P2 质量 + P3 消费/性能/验收）。

设计纪律（本仓约定）：
  · **禁止写死数量**：期望值一律从"另一个端点"或"只读直查库"推出，写成内部一致性断言。
  · 需要服务在跑（127.0.0.1:8000）；本脚本只读，不写库（persist 由被测端点自身完成）。

用法：
    python tools/verify/verify_kb_dashboard.py [--base http://127.0.0.1:8000] [--branch personal]

覆盖的修复项：
    C1 同屏口径打架   → 跨端点同 scope 同指标数值相等
    C3 追溯链断裂不可见 → 红线含 chunk_trace 且带 drill
    C4 覆盖率假绿      → 分母＝模块派生的 SysML 元素类型数
    C5 fallback 噪声   → 返回词不含编排标记与黑名单词
    C7 常量空洞面板    → 前端未启用时收起（静态断言）
    P2 未落地不充数    → alias/dedup_accuracy/eval_precision 必须 value=None（库直查证明表为空）
    P2 快照不造趋势    → persist 后有值指标落快照、无值指标不落
    P3 验收同源        → acceptance[].value 恒等于 metrics[metric_key].value
    P3 性能可对账      → RT/慢查询分位与只读直查库的结果一致
"""
import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--branch", default="personal")
args = ap.parse_args()

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))


def get(path):
    with urllib.request.urlopen(args.base + path, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


db = sqlite3.connect("file:" + str(REPO).replace("\\", "/") + "/mbse.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row


def db_one(q, p=()):
    return db.execute(q, p).fetchone()[0]


print("=" * 74)
print("知识看板自检（P0 口径 + P1 IA + P2 质量 + P3 消费/性能/验收）")
print("=" * 74)

dash = get(f"/api/knowledge/dashboard?branch={args.branch}&window=all")
stats_rel = get("/api/knowledge/stats?branch=release")
overview = get("/api/knowledge/overview")
lifecycle = get("/api/knowledge/lifecycle")
coverage = get("/api/knowledge/coverage")
ops = get("/api/ops/metrics")
trend = get(f"/api/knowledge/dashboard/trend?branch={args.branch}")

# ── 1. 指标六要素完整性（不写死指标个数） ──
print("\n[1] 指标六要素完整性")
allm = list(dash["redlines"]) + [m for g in dash["groups"] for m in g["items"]]
SC = {"release", "branch", "global"}
bad = [m.get("key") for m in allm
       if not (m.get("key") and m.get("name") and "value" in m and m.get("unit")
               and m.get("scope") in SC and m.get("status") in {"ok", "warn", "alert"}
               and m.get("target"))]
ck("每个指标都含 key/name/value/unit/scope/status/target", not bad, f"缺项：{bad}")
keys = [m["key"] for m in allm]
ck("指标 key 唯一", len(keys) == len(set(keys)), f"{len(keys)} 项 / 去重 {len(set(keys))}")
# 计算说明（用户要求：说明每个统计指标怎么算出来的）
_miss_calc = [m["key"] for m in allm if not (m.get("calc") or "").strip()]
ck("每个指标都带「计算说明」(calc)", not _miss_calc, f"缺：{_miss_calc}")
_fb = [k for k, m in dash["metrics"].items()
       if "尚未登记计算说明" in (m.get("calc") or "")]
ck("无指标落在 calc 兜底文案上（说明已全部登记，非静默留空）", not _fb, f"兜底：{_fb}")
_short = [k for k, m in dash["metrics"].items() if len(m.get("calc") or "") < 20]
ck("计算说明均有实质内容（长度 ≥20）", not _short, f"过短：{_short}")
ck("未落地指标的 calc 明确写出「未落地 / 未评测 / 未计算」",
   all(any(t in (dash["metrics"][k].get("calc") or "")
           for t in ("未落地", "未评测", "未计算", "无从评估"))
       for k in ["alias_coverage", "dedup_accuracy", "eval_precision"]))
ck("验收对照每行都带计算说明",
   all((a.get("calc") or "").strip() for a in dash["acceptance"]))
ck("有 metric_key 的验收行 calc 显式说明与指标同源",
   all("口径完全一致" in (a.get("calc") or "")
       for a in dash["acceptance"] if a["metric_key"]))
ck("metrics 扁平索引与 redlines+groups 一一对应", set(dash["metrics"]) == set(keys))
ck("summary 计数自洽（ok+warn+alert+na == 指标总数）",
   (dash["summary"]["ok"] + dash["summary"]["warn"] + dash["summary"]["alert"]
    + dash["summary"]["na"]) == len(dash["metrics"]),
   json.dumps(dash["summary"], ensure_ascii=False))
ck("redlines/acceptance 的 key 不与 metrics 冲突（验收是独立视图）",
   not (set(m["key"] for m in dash["acceptance"]) & set(keys)))

# ── 2. 口径一致性（C1） ──
print("\n[2] 口径一致性（C1：同 scope 同指标跨端点必须相等）")
rel = dash["scopes"]["release"]
ck("release.entities.reviewed == stats?branch=release.reviewed",
   rel["entities"]["reviewed"] == stats_rel["reviewed"],
   f'{rel["entities"]["reviewed"]} vs {stats_rel["reviewed"]}')
ck("release.relations.total == stats?branch=release.total_relations",
   rel["relations"]["total"] == stats_rel["total_relations"])
gl = dash["scopes"]["global"]
# 口径修复（2026-09-27）：图谱数据**没有"全局"口径** —— entities 主键 (id, branch)，
# 跨分支 COUNT(*) 是行数累加（实测 183 行 vs 逻辑去重 61，重复 3 倍）。
ck("global 不再暴露裸 entities/relations（避免被当成知识总量）",
   "entities" not in gl and "relations" not in gl,
   f"键={sorted(gl.keys())}")
ck("global.entities_dedup == overview.kpi.entities（同源去重口径）",
   gl["entities_dedup"] == overview["kpi"]["entities"],
   f'{gl["entities_dedup"]} vs {overview["kpi"]["entities"]}')
ck("global.relations_dedup == overview.kpi.relations",
   gl["relations_dedup"] == overview["kpi"]["relations"])
ck("global.documents/chunks 为真全局（不按分支切分）",
   gl["documents"] == overview["kpi"]["documents"])
ck("entities_dedup == 直查库 COUNT(DISTINCT id)",
   gl["entities_dedup"]
   == db_one("SELECT COUNT(DISTINCT id) FROM entities WHERE status!='deprecated'"),
   f'{gl["entities_dedup"]} vs 直查')
ck("relations_dedup == 直查库三元组去重",
   gl["relations_dedup"] == db_one(
       "SELECT COUNT(DISTINCT source_id || relation_type || target_id) FROM relations "
       "WHERE status!='deprecated'"), f'{gl["relations_dedup"]} vs 直查')
ck("entities_rows/relations_rows == 直查库行数（对账项保留）",
   gl["entities_rows"] == db_one("SELECT COUNT(*) FROM entities WHERE status!='deprecated'")
   and gl["relations_rows"] == db_one(
       "SELECT COUNT(*) FROM relations WHERE status!='deprecated'"))
ck("去重值 < 行数（证明此前展示的行数确实在重复计数）",
   gl["entities_dedup"] < gl["entities_rows"]
   and gl["relations_dedup"] < gl["relations_rows"],
   f'实体 {gl["entities_dedup"]}<{gl["entities_rows"]}；关系 {gl["relations_dedup"]}<{gl["relations_rows"]}')
ck("lifecycle 带跨分支行数口径说明（total 不是实体数）",
   "跨分支行数" in lifecycle.get("scope_note", ""))
ck("文档全局口径：stats.total_docs == overview.kpi.documents",
   stats_rel["total_docs"] == overview["kpi"]["documents"])
ck("overview.lifecycle == /api/knowledge/lifecycle.lifecycle（同一实现）",
   overview["lifecycle"]["lifecycle"] == lifecycle["lifecycle"])
# P 面板级合并（2026-09-27）：「知识库总览」+「数据生命周期」→「知识资产总览」，
# 前端只渲染一次来源分布，故必须保证两个字段同源（否则将来仍可能漂移）
ck("overview.source_dist == overview.lifecycle.by_source（同源，前端只渲染一次）",
   overview["source_dist"] == overview["lifecycle"]["by_source"],
   f'{len(overview["source_dist"])} vs {len(overview["lifecycle"]["by_source"])}')
ck("source_dist 键名为 source_type（前端曾误用 s.src → 渲染成 undefined）",
   all("source_type" in s for s in overview["source_dist"]))
# 同口径断言：来源分布是"知识量按来源的切面"，其各段之和必须等于资产 KPI 的实体数（均跨分支去重）。
# 若来源种类数 ≥ limit(8) 则列表被截断，此时只提示不判失败。
_sd_sum = sum(s["n"] for s in overview["source_dist"])
ck("来源分布各段之和 == 资产 KPI 实体数（同口径：跨分支去重）",
   _sd_sum == overview["kpi"]["entities"] or len(overview["source_dist"]) >= 8,
   f'合计 {_sd_sum} vs KPI {overview["kpi"]["entities"]}（来源种类 {len(overview["source_dist"])}）')
ck("coverage.type_coverage_sysml.coverage_rate*100 == dashboard.type_coverage_sysml.value",
   abs(coverage["type_coverage_sysml"]["coverage_rate"] * 100
       - dash["metrics"]["type_coverage_sysml"]["value"]) < 0.05)
ck("coverage.chunk_linked.coverage_rate*100 == dashboard.chunk_trace.value",
   abs(coverage["chunk_linked"]["coverage_rate"] * 100
       - dash["metrics"]["chunk_trace"]["value"]) < 0.05)
_OK = ("online", "active", "enabled", "ok")
_expect_online = sum(1 for t in ops["topology"] if (t.get("status") or "") in _OK)
_expect_total = len(ops["topology"])
ck("service_health 与 /api/ops/metrics.topology 同源（在线数/总数一致）",
   dash["metrics"]["service_health"]["value"] == round(_expect_online * 100 / _expect_total, 1),
   f'{dash["metrics"]["service_health"]["value"]}% vs '
   f'{_expect_online}/{_expect_total}')
ck("service_health 组件数 == 基线组件 + llm_providers 行数（探活口径可对账）",
   _expect_total == 3 + db_one("SELECT COUNT(*) FROM llm_providers"),
   f"topology={_expect_total} 行 llm_providers={db_one('SELECT COUNT(*) FROM llm_providers')}")
ck("service_health.detail 的 x/y 与 topology 一致",
   f"{_expect_online}/{_expect_total}" in dash["metrics"]["service_health"]["detail"])

# ── 3. 红线语义（不写死数量） ──
print("\n[3] 治理红线语义")
rk = {m["key"] for m in dash["redlines"]}
for k, label in [("release_unreviewed", "权威基线未评审数据"),
                 ("merge_gate_pending", "发布闸门预警"),
                 ("chunk_trace", "分块追溯覆盖率")]:
    ck(f"红线包含「{label}」", k in rk)
ck("release_unreviewed == stats?branch=release 的 candidate+raw_chunk",
   dash["metrics"]["release_unreviewed"]["value"]
   == stats_rel["candidate"] + stats_rel["raw_chunk"])
ck("每条红线都带下钻入口", all(m["drill"].get("page") for m in dash["redlines"]))

# ── 4. C4 覆盖率分母 ──
print("\n[4] 类型覆盖率分母（C4）")
from sysml_ast import NODE_KINDS  # noqa: E402
expect_types = len({v[0] for v in NODE_KINDS.values()})
ck("SysML 级分母 == sysml_ast.NODE_KINDS 归并后的类型数",
   coverage["type_coverage_sysml"]["total_types"] == expect_types,
   f"{coverage['type_coverage_sysml']['total_types']} vs {expect_types}")
ck("coverage.type_coverage 兼容键仍含旧四键",
   {"total_types", "covered_types", "coverage_rate", "empty_types"}
   .issubset(set(coverage["type_coverage"].keys())))
ck("两级明细齐备（sysml + domain）",
   "type_coverage_sysml" in coverage and "type_coverage_domain" in coverage)

# ── 5. C5 fallback 噪声过滤 ──
print("\n[5] fallback 主题词噪声过滤（C5）")
import metrics_core  # noqa: E402
words = [t["word"] for t in coverage["fallback_topics"]]
badw = [w for w in words
        if any(n in w for n in metrics_core._INTERNAL_PROMPT_NOISE)
        or any(n in w for n in metrics_core._GENERIC_TERM_NOISE)
        or w in metrics_core.STOP_WORDS]
ck("返回主题词不含内部提示词/通用词/停用词", not badw, f"违规：{badw}")
ck("编排模板标记已定义（整条 query 丢弃机制存在）",
   len(metrics_core._ORCHESTRATION_MARKERS) > 0)

# ── 6. P2 质量指标：未落地必须 None（库直查证明） ──
print("\n[6] P2 数据质量（未落地不得报 0%）")
for k in ["alias_coverage", "dedup_queue", "dedup_accuracy", "ontology_required_attrs",
          "attr_filled_rate", "staleness", "ontology_drift"]:
    ck(f"质量簇含指标 {k}", k in dash["metrics"])
ck("别名表确实为空 → alias_coverage 必须 value=None（不得报 0%）",
   db_one("SELECT COUNT(*) FROM entity_aliases") == 0
   and dash["metrics"]["alias_coverage"]["value"] is None,
   f"库 0 行，接口 value={dash['metrics']['alias_coverage']['value']}")
ck("消歧候选表为空 → dedup_queue=0 且 dedup_accuracy=None（队列空≠准确率高）",
   db_one("SELECT COUNT(*) FROM entity_dup_candidates") == 0
   and dash["metrics"]["dedup_queue"]["value"] == 0
   and dash["metrics"]["dedup_accuracy"]["value"] is None)
ck("本体必填约束 0 个 → ontology_required_attrs 告警（这才是「属性完整度无从评估」的正解）",
   dash["metrics"]["ontology_required_attrs"]["value"] == 0
   and dash["metrics"]["ontology_required_attrs"]["status"] == "alert")
_req_n = sum(1 for r in db.execute(
    "SELECT constraints FROM ontology_types WHERE type_kind='entity'")
    if json.loads(r["constraints"] or "{}").get("required"))
_tot_n = db_one("SELECT COUNT(*) FROM ontology_types WHERE type_kind='entity'")
ck("ontology_required_attrs == 直查「有 required 的类型数 / 全部类型数」*100",
   abs(dash["metrics"]["ontology_required_attrs"]["value"]
       - round(_req_n * 100 / max(1, _tot_n), 1)) < 0.05, f"{_req_n}/{_tot_n}")
ck("陈旧度口径已注明近似（entities 无 updated_at）",
   "updated_at" not in [r["name"] for r in db.execute("PRAGMA table_info(entities)")]
   and "updated_at" in dash["metrics"]["staleness"]["threshold"])
ck("本体漂移：库无孤儿/被替换类型 → drift=0",
   dash["metrics"]["ontology_drift"]["value"] == 0
   and db_one("SELECT COUNT(*) FROM ontology_types WHERE COALESCE(replaced_by,'')!=''") == 0)

# ── 7. P3 消费/性能指标 ──
print("\n[7] P3 消费与性能（与只读直查库对账）")
for k in ["eval_recall_structured", "eval_recall_semi", "eval_precision",
          "eval_negative_control", "eval_age_days", "engine_rt_p50", "engine_rt_p95",
          "slow_query_share", "service_health", "audit_activity", "llm_activity"]:
    ck(f"含指标 {k}", k in dash["metrics"])
lats = sorted(r[0] or 0 for r in db.execute("SELECT latency_ms FROM query_routing_stats"))
if lats:
    p50 = lats[min(len(lats) - 1, int(len(lats) * .5))]
    p95 = lats[min(len(lats) - 1, int(len(lats) * .95))]
    ck("engine_rt_p50 == 直查库 p50", dash["metrics"]["engine_rt_p50"]["value"] == p50,
       f'{dash["metrics"]["engine_rt_p50"]["value"]} vs {p50}')
    ck("engine_rt_p95 == 直查库 p95", dash["metrics"]["engine_rt_p95"]["value"] == p95,
       f'{dash["metrics"]["engine_rt_p95"]["value"]} vs {p95}')
    slow = db_one("SELECT COUNT(*) FROM query_routing_stats WHERE latency_ms>2000")
    ck("slow_query_share == 慢查询数/总数",
       abs(dash["metrics"]["slow_query_share"]["value"] - round(slow * 100 / len(lats), 1)) < 0.05,
       f'slow={slow} n={len(lats)}')
ev = db.execute("SELECT detail FROM eval_reports ORDER BY created_at DESC LIMIT 1").fetchone()
if ev:
    det = json.loads(ev["detail"] or "{}")
    rs = (det.get("summary") or {}).get("graph", {}).get("entity_recall")
    rq = (det.get("summary") or {}).get("doc", {}).get("recall@5")
    ck("eval_recall_structured == eval_reports.detail.summary.graph.entity_recall*100",
       abs(dash["metrics"]["eval_recall_structured"]["value"] - round(rs * 100, 1)) < 0.05,
       f'{dash["metrics"]["eval_recall_structured"]["value"]} vs {round(rs*100,1)}')
    ck("eval_recall_semi == eval_reports.detail.summary.doc.recall@5*100",
       abs(dash["metrics"]["eval_recall_semi"]["value"] - round(rq * 100, 1)) < 0.05)
    ck("负对照召回为 0（证明指标在真测对齐关系，不是假绿）",
       dash["metrics"]["eval_negative_control"]["value"] == 0)
ck("精确率未计算时必须 value=None（eval_reports 列 0.0 ≠ 精确率为 0）",
   db_one("SELECT entity_precision FROM eval_reports ORDER BY created_at DESC LIMIT 1") == 0
   and dash["metrics"]["eval_precision"]["value"] is None,
   f"接口 value={dash['metrics']['eval_precision']['value']}")
ck("审计活跃度 == 直查近 7 天 audit_logs",
   dash["metrics"]["audit_activity"]["value"]
   == db_one("SELECT COUNT(*) FROM audit_logs WHERE created_at>=datetime('now','-7 days')"))
ck("LLM 活跃度 == 直查近 7 天 llm_usage_stats",
   dash["metrics"]["llm_activity"]["value"]
   == db_one("SELECT COUNT(*) FROM llm_usage_stats WHERE created_at>=datetime('now','-7 days')"))

# ── 8. P3 验收对照：同源 & 覆盖率 ──
print("\n[8] 验收对照（甲方 7.1 × PRD §2.2）")
acc = dash["acceptance"]
need_a = sum(1 for a in acc if a["clause"].startswith("甲方 7.1"))
need_p = sum(1 for a in acc if a["clause"].startswith("PRD"))
ck("覆盖甲方 7.1 至少 6 条", need_a >= 6, f"{need_a} 条")
ck("覆盖 PRD §2.2 至少 5 条", need_p >= 5, f"{need_p} 条")
mismatch = [a["key"] for a in acc if a["metric_key"] and a["metric_key"] in dash["metrics"]
            and a["value"] != dash["metrics"][a["metric_key"]]["value"]]
ck("acceptance[].value 恒等于 metrics[metric_key].value（同源，不重复计算）",
   not mismatch, f"不一致：{mismatch}")
ck("有 metric_key 的行都指向真实存在的指标",
   all(a["metric_key"] in dash["metrics"] for a in acc if a["metric_key"]))
ck("无实测数据的行 value=None 且注明原因（不预置假值）",
   all(a["value"] is not None or a["evidence"] for a in acc))
ck("每条都有目标与判定状态", all(a["target"] and a["status"] in {"ok", "warn", "alert"}
                                 for a in acc))

# ── 9. P2 快照与趋势：不造趋势 ──
print("\n[9] 指标快照与趋势（persist）")
before = db_one("SELECT COUNT(*) FROM dashboard_metric_snapshots") if db_one(
    "SELECT COUNT(*) FROM sqlite_master WHERE name='dashboard_metric_snapshots'") else 0
persisted = get(f"/api/knowledge/dashboard?branch={args.branch}&window=all&persist=true")
ck("persist=true 后端点仍正常返回", persisted.get("ok") is True)
after = db_one("SELECT COUNT(*) FROM dashboard_metric_snapshots")
ck("快照表存在且 persist 后有行", after > 0, f"{before} → {after}")
na_keys = [k for k, m in persisted["metrics"].items() if m["value"] is None]
snap_na = [r[0] for r in db.execute(
    "SELECT DISTINCT metric_key FROM dashboard_metric_snapshots")]
ck("未落地指标（value=None）不落快照（避免把 None 当 0 画假趋势）",
   not (set(na_keys) & set(snap_na)), f"未落地 {len(na_keys)} 项")
ck("快照总数 == 有值指标数（按天幂等，不重复膨胀）",
   len(snap_na) == len(persisted["metrics"]) - len(na_keys),
   f"{len(snap_na)} vs {len(persisted['metrics'])-len(na_keys)}")
ck("趋势端点返回结构正确",
   trend.get("ok") is True and isinstance(trend.get("trends"), dict)
   and all(isinstance(v, list) for v in trend["trends"].values()))
ck("趋势点字段齐备（date/value/status）",
   all(("date" in p and "value" in p and "status" in p)
       for v in trend["trends"].values() for p in v))

# ── 10. 反假指标 + 前端契约 ──
print("\n[10] 反假指标与前端契约")
d = json.dumps(dash, ensure_ascii=False) + json.dumps(ops, ensure_ascii=False)
ck("无硬编码假指标（review_score / light_rt / qps / availability 均不出现）",
   all(x not in d for x in ["review_score", "light_rt", "\"qps\"", "availability"]))
ck("northstar.health_score 与 summary 自洽（重算一致）",
   (lambda s: dash["northstar"]["health_score"] == round(
       100 * (s["ok"] + 0.5 * s["warn"]) / max(1, s["ok"] + s["warn"] + s["alert"])))(
       dash["summary"]))
js = (REPO / "static" / "js" / "mods" / "15-kb.js").read_text(encoding="utf-8")
html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
ck("15-kb.js 首屏调用 /api/knowledge/dashboard", "api('/api/knowledge/dashboard" in js)
ck("15-kb.js 不再调用 /api/knowledge/stats（旧分支口径端点已退场）",
   "/api/knowledge/stats" not in js)
ck("P1：kbClusterize / kbSetCluster / kbDrill / kbSpark / kbRenderAcceptance 均存在",
   all(f in js for f in ["function kbClusterize(", "function kbSetCluster(",
                         "function kbDrill(", "function kbSpark(",
                         "function kbRenderAcceptance("]))
# JS 关键声明唯一性护栏：补丁脚本按"下一个顶层 function"做结构化替换时，
# 曾把紧随其后的 const/let 声明一并吞掉（运行时 ReferenceError，node --check 查不出），
# 也曾引入重复 const 声明（编译期错误，整页白屏）。两者都由这一条静态断言拦住。
_JS_IDENT = ["KB_CLUSTERS", "KB_CLUSTER_PANELS", "KB_GOV_TARGET", "KB_DETAIL_PANELS",
             "_kbDetailShown", "_kbClusterCur", "_kbWin"]
_dup, _miss = [], []
for _id in _JS_IDENT:
    _n = len(re.findall(rf"^\s*(?:const|let|var)\s+{re.escape(_id)}\s*=", js, re.M))
    if _n == 0:
        _miss.append(_id)
    elif _n > 1:
        _dup.append(f"{_id}×{_n}")
ck(f"{len(_JS_IDENT)} 个 JS 关键声明齐备且唯一（防重复声明/被吞掉）",
   not _dup and not _miss, f"缺={_miss} 重复={_dup}")
ck("15-kb.js 含明细收起机制（kbApplyDetailPanels/kbToggleDetail/kbDetailCount）",
   all(f"function {f}(" in js for f in ["kbApplyDetailPanels", "kbToggleDetail", "kbDetailCount"]))

ck("P1：四簇 Tab 容器与四簇容器齐备",
   all(x in html for x in ['id="kb-cluster-tabs"', 'data-cluster="gov"',
                           'data-cluster="quality"', 'data-cluster="perf"',
                           'data-cluster="accept"']))
# 面板级合并后的残留检查（合并是"删面板"，必须证明真的没有半删状态）
_js_kb = (REPO / "static" / "js" / "mods" / "15-kb.js").read_text(encoding="utf-8")
ck("前端不再直连 /api/knowledge/lifecycle（与总览同源，防双份口径）",
   "/api/knowledge/lifecycle" not in _js_kb)
ck("已合并面板 #kb-lifecycle 在 index.html / 15-kb.js 中均已清除",
   "kb-lifecycle" not in html and "kb-lifecycle" not in _js_kb)
_reg_ids = set()
for _m2 in re.finditer(r"KB_CLUSTER_PANELS\s*=\s*\{(.*?)\n\};", _js_kb, re.S):
    _reg_ids |= set(re.findall(r"'([A-Za-z0-9_\-]{6,})'", _m2.group(1)))
ck("已合并面板 id 不再出现在 KB_CLUSTER_PANELS 的登记列表中（注释提及不算）",
   "glLzLRHMxaBFAqwAAERmy8" not in _reg_ids, f"登记 {len(_reg_ids)} 个")
ck("面板标题已更名「知识资产总览」", "知识资产总览" in html)
ck("loadLifecycle 保留为合并别名（3 处旧调用点仍有效）",
   "async function loadLifecycle() { return loadKBOverview(); }" in _js_kb)

ck("index.html 含红线/治理簇/图数据库容器（P0 契约不回归）",
   all(x in html for x in ['id="kb-redline"', 'id="kb-gov"',
                           'id="graphdb-panel"', 'id="graphdb-query-box"']))
# JS 语法护栏：15-kb.js 曾因重复 `const` 声明（编译期错误）导致整页白屏
# —— 静态语法检查能在打补丁后立刻拦住，不必等到真实点击。
import subprocess  # noqa: E402
_js_files = ["static/js/mods/15-kb.js"]
try:
    _bad = []
    for _f in _js_files:
        _r = subprocess.run(["node", "--check", str(REPO / _f)],
                            capture_output=True, text=True)
        if _r.returncode != 0:
            _bad.append(f"{_f}: {(_r.stderr or _r.stdout)[:160]}")
    ck(f"{len(_js_files)} 个看板 JS 通过 node --check（语法护栏）", not _bad, str(_bad))
except FileNotFoundError:
    ck("node 可用（不可用则无 JS 语法护栏）", False, "未找到 node")

print("\n" + "=" * 74)
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)