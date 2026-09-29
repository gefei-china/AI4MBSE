# -*- coding: utf-8 -*-
"""知识看板聚合端点：口径唯一来源的出口（P0 口径统一 → P1/P2/P3 扩展）。

## 为什么要有这个端点

`#kb-a` 原先由前端拼三个端点 —— `stats`（带分支）+ `overview`（全局）+ `lifecycle`（全局）
—— 同一屏三套口径互相打架。2026-09-26 实测：已评审 61（stats@personal）vs
122+61（lifecycle 全局）、关系 83 vs 249、已废弃 2 vs 6。

本端点一次返回**全部**看板指标，每个指标带**六要素**
（`key` / `name` / `value`+`unit` / `scope` / `status` + `target`/`threshold`/`detail`/`drill`），
**前端只渲染不计算**；阈值判定复用 `governance._status`（唯一实现）。

## 分组（P1 信息架构的四簇）

| 簇 | 内容 | 面向 |
|---|---|---|
| `governance` | 治理健康度 9 项（复用 governance.compute_metrics）+ 变更闭环 | 知识工程师 / 评审人 |
| `quality` | 数据质量：类型覆盖两级分母、别名、消歧、属性、陈旧、本体漂移 | 治理管理员 |
| `consumption` | 知识消费：双引擎路由、知识获取 P/R/F1（读 eval_reports） | 产品 / 甲方 |
| `performance` | 性能与运维：RT 分位、慢查询、服务健康、审计活跃度 | 运维 / 甲方验收 |

另有 `acceptance`：**甲方 7.1 与 PRD §2.2 的逐条验收对照**（是 metrics 的**视图**，
通过 `metric_key` 引用同一口径，不重复计算）。

⚠️ 口径纪律：**本文件不写任何统计 SQL**，一律委派 `metrics_core`。
"""
import datetime

from routers.knowledge_parts.shared import *  # noqa: F401,F403

import metrics_core
from metrics_core import _status

# 作用域图例（前端把它渲染成指标卡上的 scope 角标 tooltip）
SCOPE_LEGEND = {
    "release": "权威基线（release 分支）——AI 建模只消费它，决定「AI 看到的知识有多准」",
    "branch": "工作分支（personal/dev）——知识工程师作业面，决定「今天该审什么」",
    "global": "全局资产（文档/分块不按分支切分）——恒为全局口径",
}

# 治理健康度 9 项的作用域（governance.compute_metrics 全部是全局口径）
_GOV_SCOPE = "global"
# 治理健康度各项的下钻入口（每项都必须能一键到修复面）
_GOV_DRILL = {
    "shacl_violations": {"page": "kb-b", "title": "去治理中心处理违规"},
    "trace_coverage": {"page": "kb-d", "title": "去知识图谱查看需求追溯链"},
    "orphan_rate": {"page": "kb-d", "title": "去知识图谱查看孤立节点", "kind": "graph_isolated"},
    "mapping_coverage": {"page": "kb-c", "title": "去本体模型补概念映射"},
    "pending_candidates": {"page": "kb-a", "title": "在本页实体浏览按「候选」过滤",
                           "kind": "entity_status", "value": "candidate"},
    "low_confidence": {"page": "kb-b", "title": "去治理中心复核低置信三元组"},
    "deprecated_residue": {"page": "kb-c", "title": "去本体模型清理弃用术语"},
    "homonym_terms": {"page": "kb-c", "title": "去术语词典做同形异义消歧"},
    "mirror_sync": {"page": "kb-a", "title": "在图数据库面板执行「同步入图」"},
}

_WINDOW_DAYS = {"7d": 7, "30d": 30, "all": 3650}

# 北极星卡（首屏）：健康分 + 这 3 个指标（均已在 metrics 中有唯一口径）
NORTHSTAR_KEYS = ["pending_candidates", "release_unreviewed", "chunk_trace"]


# ── 计算说明（calc）：随指标下发，前端 ⓘ hover 展示 ──
# 约定写法：来源表/函数 → 过滤条件 → 公式/取值方式 →（若为未落地/未评测）如实注明
_CALC = {
    # ── 治理红线（scope=release 固定） ──
    "release_unreviewed":
        "来源：entities 表。条件：branch ∈ 全部 release 分支 且 status NOT IN ('reviewed','deprecated')。"
        "取值：COUNT(*)（不做去重，逐行计数）。期望恒为 0 —— AI 建模只消费 release，"
        "非 0 表示 AI 会读到未审核数据。",
    "merge_gate_pending":
        "来源：merge_requests + entities。遍历 status ∈ (draft, open) 且 target 为 release 的请求，"
        "对每个 source_branch 调用 repositories/branch_repo.pending_review_entities() 得未评审实体数并求和。"
        "与「提交合并请求时的拦截」是同一函数，保证闸门口径一致。",
    "chunk_trace":
        "来源：document_chunks。已链接 = linked_entity_ids 非 NULL、TRIM 后不为空且不为 '[]'。"
        "公式：覆盖率 = 已链接数 ÷ 总 chunk 数。未链接 ⇒ 抽取结果无来源，追溯链断裂。",
    # ── 治理健康度（复用 governance.compute_metrics，阈值三色同一实现） ──
    "shacl_violations":
        "来源：governance.compute_metrics → SHACL 门禁结果（近 7 天）。取值：违规条数。"
        "点面板右上有「刷新（含 SHACL）」会现场重跑一次门禁。阈值：warn 模式下仅记录，发布前须 =0。",
    "trace_coverage":
        "来源：需求类实体 + 关系表。口径：以「验证/满足」类关系（source 或 target 任一侧命中）"
        "连通的需求数 ÷ 需求总数。阈值 ≥90%（目标 ≥95%）。",
    "orphan_rate":
        "来源：实体与关系表。公式：无任何关系连接的实体数 ÷ 实体总数。阈值 <5%。"
        "孤立节点通常是抽取碎片或漏连边，需在知识图谱里清理/补边。",
    "mapping_coverage":
        "来源：概念（术语/本体）与映射配置。公式：已配置映射的概念数 ÷ 概念总数。阈值 ≥80%。",
    "pending_candidates":
        "来源：triples(status=candidate) + v2g_candidates(status=pending)。取值：两者相加。"
        "阈值本身不是硬线，但持续增长说明审核产能不足。",
    "low_confidence":
        "来源：已通过（approved）三元组。公式：confidence < 0.7 的条数 ÷ approved 总数。阈值 <10%。",
    "deprecated_residue":
        "来源：本体类型表。取值：status 为 deprecated/obsolete 且仍被引用的术语数。期望 =0。",
    "homonym_terms":
        "来源：术语词典。取值：同形异义词条数（一个词对应多个本体类型，需人工消歧）。期望 =0。",
    "mirror_sync":
        "来源：图库镜像状态（mirror_state）。取值：主库与镜像之间的未同步变更条数（lag）。"
        "lag>0 需执行增量物化。",
    "merge_requests_pending":
        "来源：merge_requests。取值：status ∈ (draft, open) 的请求条数。期望 =0。",
    "ai_review_rate":
        "来源：v2g_candidates。公式：（已离开 pending 的候选数）÷ 候选总数 ×100%。"
        "优先只统计 source_type='ai_model' 的通道；若库内无该标记则退化为全部候选（口径见 threshold）。"
        "当前库 348 条全 pending ⇒ 0%（PRD §2.2 目标 100%）。",
    # ── 数据质量 ──
    "type_coverage_sysml":
        "分母：sysml_ast.NODE_KINDS 归并后的中文实体类型数（14 类，代表「规范约束源」）。"
        "分子：entities 中该类型存在实例（status≠deprecated）的类型数。公式 = 分子 ÷ 分母 ×100%。"
        "⚠️ 不用 ontology_types 的 entity 类型总数做分母 —— 那里面有 25 个是领域业务类型，"
        "会把覆盖率撑成 93% 的假绿（旧口径 26/28）。",
    "type_coverage_domain":
        "分母：ontology_types 中 type_kind='entity' 且不属于上述 SysML 元素类型的领域业务类型数。"
        "分子：其中有实例（status≠deprecated）的类型数。",
    "fallback_topics":
        "来源：query_routing_stats（近 N 天）。条件：route ∈ (vector, mixed) 或 reason 含 no_hit。"
        "处理：先**整条丢弃内部编排查询**（含「[任务上下文快照]/子任务：/期望输出：」等标记），"
        "再做中英文分词并按停用词 + 内部提示词 + 通用词三级过滤，取词频 TOP N。"
        "值越小说明知识覆盖越充分。",
    "alias_coverage":
        "来源：entity_aliases 与 entities。公式：有别名记录的实体数 ÷ 实体总数。"
        "⚠️ 当前 entity_aliases **0 行** ⇒ 未落地，value=None（不报 0%，否则会被误读为「做得差」）。",
    "dedup_queue":
        "来源：entity_dup_candidates。取值：全部状态条数（待处理 = pending + candidate）。期望 =0。",
    "dedup_accuracy":
        "需要人工标注的消歧评测集才能计算。⚠️ 当前 entity_dup_candidates **0 行**、无标注 ⇒ 未评测，"
        "value=None。注意：队列为空 ≠ 准确率高，两者是不同的事。",
    "ontology_required_attrs":
        "来源：ontology_types.constraints。公式：定义了 constraints.required（必填属性）的实体类型数 ÷ "
        "实体类型总数 ×100%。⚠️ 当前 0/28 ⇒ 属性完整度**无从评估**（这条指标就是把「无法评估」暴露出来）。",
    "attr_filled_rate":
        "来源：entities.properties。公式：properties 非空（非 NULL、非 ''、非 '{}'）的实体数 ÷ 实体总数。"
        "这是本体未定义必填属性时的**兜底口径**，不代表字段级完整度。",
    "staleness":
        "来源：entities.created_at。公式：创建时间早于 N 天前的实体数 ÷ 实体总数。"
        "⚠️ entities 表**无 updated_at 列**，故只能近似为「长期未新增」而非「长期未修改」（口径已注明）。",
    "ontology_drift":
        "来源：entities × ontology_types。取值：孤儿类型数（实体引用了本体中不存在的类型）"
        "+ 被替换/弃用类型（replaced_by 非空或 status 为 deprecated）仍被引用的实体数。期望 =0。",
    # ── 知识消费 ──
    "engine_total":
        "来源：query_routing_stats。取值：累计检索次数（每次检索自动落库）。",
    "engine_graph_share":
        "来源：knowledge_engine.QueryRouter().stats()。公式：route='graph' 的次数 ÷ 三路线总次数 ×100%。"
        "图路线置信最高、延迟最低，占比越高说明知识底座越可用。",
    "engine_vector_share":
        "来源：同 QueryRouter 统计。公式：route='vector' 的次数 ÷ 三路线总次数 ×100%。"
        "纯向量路线给不出图置信，占比过高 ⇒ 大量请求不可解释。",
    "eval_recall_structured":
        "来源：eval_reports 最新一行的 detail JSON → summary.graph.entity_recall（**评测脚本实测，非估算**）。"
        "对应甲方 7.1(c)「结构化召回 ≥95%」。",
    "eval_recall_semi":
        "来源：eval_reports 最新一行的 detail JSON → summary.doc.recall@5（文档侧 top-5 召回）。"
        "对应甲方 7.1(d)「半/非结构化召回 ≥75%」。",
    "eval_precision":
        "⚠️ eval_reports 的 entity_precision / f1 列值为 **0.0 = 未计算**（评测脚本只算了召回），"
        "不是「精确率为 0」。故 value=None，避免把未计算误读为极差。",
    "eval_negative_control":
        "来源：eval_reports detail → negative_control.recall@5。做法：把 gold 轮转错位后重算，"
        "分数应归零 —— 用于证明指标确实在测「对齐关系」，而不是普遍虚高。",
    "eval_age_days":
        "公式：当前日期 − eval_reports 最新一行的 created_at（天）。>30 天说明评测结论已陈旧。",
    # ── 性能与运维 ──
    "engine_rt_p50":
        "来源：query_routing_stats.latency_ms 全量样本。公式：把延迟升序排列后取第 50 百分位"
        "（在 Python 内计算：SQLite 无 percentile 函数）。对应甲方 7.1(a)「API ≤500ms」。",
    "engine_rt_p95":
        "来源/算法同 p50，取第 95 百分位（看长尾）。升序数组索引 = floor(n × 0.95)。",
    "slow_query_share":
        "来源：query_routing_stats。公式：latency_ms > 2000 的条数 ÷ 总条数 ×100%。"
        "⚠️ 直接用**计数**相除，不先取整 rate 再乘 100（否则会双重取整，280/471 显示 59.5% 而真值 59.4%）。",
    "service_health":
        "来源：metrics_core.service_health() —— 真实探活：SQLite 连通、pyoxigraph 是否可导入、"
        "Fuseki 端口(3030) 是否可连、各 LLM provider 的 status 字段。"
        "公式：状态为 online/active/enabled/ok 的组件数 ÷ 组件总数 ×100%。"
        "与 /api/ops/metrics 的 topology 是同一实现（不写两份）。",
    "audit_activity":
        "来源：audit_logs。取值：created_at 近 7 天的行数（另附近 30 天与累计）。",
    "llm_activity":
        "来源：llm_usage_stats。取值：created_at 近 7 天的行数（另附近 30 天与累计）。",
}

_CALC_FALLBACK = "⚠️ 该指标尚未登记计算说明（请在 dashboard.py 的 _CALC 中补充）"


def _with_calc(flat: dict) -> dict:
    """把计算说明注入每个指标（单一来源；未登记即显式提示，不静默留空）。"""
    for k, m in flat.items():
        m["calc"] = _CALC.get(k, _CALC_FALLBACK)
    return flat


def _metric(key, name, value, unit, scope, target, status,
            threshold="", detail="", drill=None, layer=""):
    """看板指标六要素构造器——所有指标必须经此产出，保证字段齐备。"""
    return {"key": key, "name": name, "value": value, "unit": unit, "scope": scope,
            "target": target, "status": status, "threshold": threshold,
            "detail": detail, "drill": drill or {}, "layer": layer}


def _na_metric(key, name, unit, scope, target, reason, drill=None):
    """未落地/未评测指标：value=None（前端显示 "—"），状态 warn。

    ⚠️ 纪律：基础表为空时**不得报 0%** —— 0% 会被读成"做得差"，
    而事实是"这项能力还没跑起来"，两者的处置动作完全不同。
    """
    return _metric(key, name, None, unit, scope, target, "warn",
                   threshold="基础表为空 / 无评测集 → 指标未产出（不是 0%）",
                   detail=reason, drill=drill, layer="未落地")


def _governance_group(conn, run_shacl: bool) -> list:
    """治理健康度（复用 governance.compute_metrics 既有 9 项阈值口径，不重写判定）。"""
    raw = metrics_core.governance_items(conn, run_shacl=run_shacl)
    items = []
    for it in raw.get("items", []):
        items.append({**it, "scope": _GOV_SCOPE,
                      "target": it.get("threshold", ""),
                      "drill": _GOV_DRILL.get(it["key"], {})})
    ph = conn.execute(
        "SELECT COUNT(*) FROM merge_requests WHERE status IN ('draft','open')").fetchone()[0]
    items.append(_metric(
        "merge_requests_pending", "未处理合并请求", ph, "条", "release",
        "= 0", _status(ph, lower_better=True, warn=0),
        threshold="FR-UI-6：看板编辑须经变更申请→评审→合并",
        detail="draft/open 态的合并请求", drill={"page": "branch", "title": "去合并请求处理"}))
    return items


def _quality_group(conn, days: int) -> list:
    """数据质量簇：类型覆盖（两级分母）+ 别名 + 消歧 + 属性 + 陈旧 + 本体漂移 + 建议抽取主题。"""
    tc = metrics_core.type_coverage(conn)
    topics = metrics_core.fallback_topics(conn, days=days, top=20)
    sysml_rate = round(tc["sysml"]["coverage_rate"] * 100, 1)
    dom_rate = round(tc["domain"]["coverage_rate"] * 100, 1)
    s, d = tc["sysml"], tc["domain"]

    alias = metrics_core.alias_coverage(conn)
    dedup = metrics_core.dedup_state(conn)
    gaps = metrics_core.ontology_schema_gaps(conn)
    filled = metrics_core.attr_filled_rate(conn)
    stale = metrics_core.staleness(conn)
    drift = metrics_core.ontology_drift(conn)

    out = [
        _metric("type_coverage_sysml", "SysML 元素类型覆盖率", sysml_rate, "%", "global",
                "≥ 82%（PRD §2.2）", _status(sysml_rate, ok=82, warn=50),
                threshold="分母＝SysML v2 元素类型（sysml_ast.NODE_KINDS 归并 14 类）",
                detail=f"{s['covered_types']}/{s['total_types']} 有实例"
                       f"（已评审 {s['reviewed_types']}）；缺实例："
                       + "、".join(t["name"] for t in s["empty_types"][:8]),
                drill={"page": "kb-a", "title": "在本页实体浏览按「已评审」过滤",
                       "kind": "entity_status", "value": "reviewed"}),
        _metric("type_coverage_domain", "领域扩展类型覆盖率", dom_rate, "%", "global",
                "≥ 80%", _status(dom_rate, ok=80, warn=50),
                threshold="分母＝本体中不属于 SysML 元素类型的领域业务类型",
                detail=f"{d['covered_types']}/{d['total_types']} 有实例（已评审 {d['reviewed_types']}）",
                drill={"page": "kb-c", "title": "去本体模型查看领域类型"}),
        _metric("fallback_topics", "建议抽取主题", len(topics), "个", "global",
                "= 0（知识覆盖充足）", _status(len(topics), lower_better=True, warn=0),
                threshold="检索 fallback 高频业务词；已滤除系统提示词与通用词噪声",
                detail="、".join(f"{t['word']}×{t['count']}" for t in topics[:6]) or "无 fallback 主题",
                drill={"page": "kb-e", "title": "去文档库补充文档"}),
    ]

    # ── P2 新增 6 项 ──
    if alias["landed"]:
        r = round((alias["rate"] or 0) * 100, 1)
        out.append(_metric("alias_coverage", "别名覆盖率（中英文互认）", r, "%", "global",
                           "≥ 90%（PRD §2.2）", _status(r, ok=90, warn=60),
                           detail=f"{alias['entities_with_alias']}/{alias['entities']} 个实体有别名"
                                  f"（别名表 {alias['aliases']} 条）",
                           drill={"page": "kb-c", "title": "去本体模型/术语词典补别名"}))
    else:
        out.append(_na_metric("alias_coverage", "别名覆盖率（中英文互认）", "%", "global",
                              "≥ 90%（PRD §2.2）",
                              "entity_aliases 表 0 行 —— 别名库未落地，PRD §2.2「中英文实体互认率」"
                              "目前无从度量（不是 0%）",
                              drill={"page": "kb-c", "title": "去术语词典建立别名"}))

    out.append(_metric("dedup_queue", "消歧队列深度", dedup["queue_depth"], "条", "global",
                       "= 0", _status(dedup["queue_depth"], lower_better=True, warn=0),
                       detail=f"entity_dup_candidates 共 {dedup['queue_depth']} 条"
                              f"（待处理 {dedup['pending']}）"))
    if dedup["accuracy"] is None:
        out.append(_na_metric("dedup_accuracy", "实体消歧准确率", "%", "global",
                              "≥ 85%（PRD §2.2）",
                              f"无评测集：{dedup['accuracy_note']}",
                              drill={"page": "kb-b", "title": "去治理中心做消歧审核"}))
    else:
        a = round(dedup["accuracy"] * 100, 1)
        out.append(_metric("dedup_accuracy", "实体消歧准确率", a, "%", "global",
                           "≥ 85%（PRD §2.2）", _status(a, ok=85, warn=70)))

    gr = round(gaps["rate"] * 100, 1)
    out.append(_metric(
        "ontology_required_attrs", "本体必填属性约束覆盖率", gr, "%", "global",
        "≥ 50%", _status(gr, ok=50, warn=20),
        threshold="定义了 constraints.required 的实体类型占比 —— 为 0 则「属性完整度」无从评估",
        detail=f"{gaps['types_with_required']}/{gaps['total_types']} 个实体类型定义了必填属性"
               + ("（**全部为空**：属性完整度无从保障）" if not gaps["types_with_required"] else ""),
        drill={"page": "kb-c", "title": "去本体模型补必填属性约束"}))

    fr = round(filled["rate"] * 100, 1)
    out.append(_metric("attr_filled_rate", "实体属性齐备率", fr, "%", "global",
                       "≥ 95%", _status(fr, ok=95, warn=80),
                       threshold="properties 非空占比（本体无必填约束时的兜底口径）",
                       detail=f"{filled['filled']}/{filled['total']} 个实体 properties 非空"))

    sr = round(stale["rate"] * 100, 1)
    out.append(_metric("staleness", f"陈旧度（超 {stale['days']} 天未新增）", sr, "%", "global",
                       "< 20%", _status(sr, lower_better=True, warn=20),
                       threshold="⚠️ entities 表无 updated_at，以 created_at 近似（口径已注明）",
                       detail=f"{stale['stale']}/{stale['total']} 个实体超 {stale['days']} 天未新增"))

    out.append(_metric("ontology_drift", "本体版本漂移", drift["drift"], "处", "global",
                       "= 0", _status(drift["drift"], lower_better=True, warn=0),
                       threshold="孤儿类型（实体引用本体不存在的类型）+ 被替换/弃用类型仍被引用",
                       detail=f"孤儿类型 {drift['orphan_count']} 个；被替换/弃用仍引用 "
                              f"{drift['replaced_in_use']} 条；本体版本 max v{drift['max_version']}"
                              f"（{drift['distinct_versions']} 个版本）"))
    return out


def _consumption_group(conn) -> list:
    """消费簇：双引擎路由分布 + 知识获取 P/R/F1（读 eval_reports）。"""
    st = metrics_core.engine_routes(conn)
    routes = st.get("routes", {}) or {}
    total = st.get("total_queries", 0) or 0

    def _route(k):
        return routes.get(k, {}) or {}

    graph_pct = round(_route("graph").get("pct", 0) or 0, 1)
    vector_pct = round(_route("vector").get("pct", 0) or 0, 1)
    ev = metrics_core.eval_quality(conn)

    out = [
        _metric("engine_total", "累计检索次数", total, "次", "global", "—", "ok",
                threshold="每次检索自动落库（query_routing_stats）",
                detail="、".join(f"{k} {_route(k).get('count', 0) or 0} 次"
                                for k in ("graph", "mixed", "vector"))),
        _metric("engine_graph_share", "纯图路线占比", graph_pct, "%", "global",
                "≥ 50%（快与准：图路线又快又准）", _status(graph_pct, ok=50, warn=20),
                threshold="图路线置信最高、延迟最低，占比越高说明知识底座越可用",
                detail=f"平均置信 {_route('graph').get('avg_confidence', 0)} · "
                       f"{_route('graph').get('avg_latency_ms', 0)}ms",
                drill={"page": "kb-a", "title": "查看双引擎明细面板"}),
        _metric("engine_vector_share", "纯向量路线占比", vector_pct, "%", "global",
                "≤ 30%", _status(vector_pct, lower_better=True, warn=30),
                threshold="纯向量路线无法给出图置信，占比过高＝大量请求不可解释",
                detail=f"平均置信 {_route('vector').get('avg_confidence', 0)} · "
                       f"{_route('vector').get('avg_latency_ms', 0)}ms",
                drill={"page": "kb-a", "title": "查看双引擎明细面板"}),
    ]

    if not ev.get("landed"):
        out.append(_na_metric("eval_recall_structured", "知识获取召回·结构化", "%", "global",
                              "≥ 95%（甲方 7.1c）", "eval_reports 无记录"))
        out.append(_na_metric("eval_recall_semi", "知识获取召回·半/非结构化", "%", "global",
                              "≥ 75%（甲方 7.1d）", "eval_reports 无记录"))
    else:
        rs = ev["structured_recall"]
        rq = ev["semi_recall5"]
        out.append(_metric(
            "eval_recall_structured", "知识获取召回·结构化",
            round(rs * 100, 1) if rs is not None else None, "%", "global",
            "≥ 95%（甲方 7.1c）",
            _status(rs * 100, ok=95, warn=75) if rs is not None else "warn",
            threshold=f"来源 eval_reports#{ev['report_id']}（{ev['model_version']}，{ev['age_days']} 天前）",
            detail=f"图谱侧 entity_recall={rs}（{ev['graph_n']} 例）；路由准确率 {ev['route_acc']}；"
                   f"关系命中 {ev['relation_hit']}",
            drill={"page": "kb-b", "title": "去治理中心查看评测报告"}))
        out.append(_metric(
            "eval_recall_semi", "知识获取召回·半/非结构化",
            round(rq * 100, 1) if rq is not None else None, "%", "global",
            "≥ 75%（甲方 7.1d）",
            _status(rq * 100, ok=75, warn=50) if rq is not None else "warn",
            threshold=f"来源 eval_reports#{ev['report_id']}（doc 侧 recall@5）",
            detail=f"文档侧 recall@5={rq}（{ev['doc_n']} 例）；MRR@10={ev['doc_mrr10']}；"
                   f"p50 {ev['doc_p50_ms']}ms"))
        out.append(_na_metric(
            "eval_precision", "知识获取精确率 / F1", "%", "global",
            "≥ 95%（甲方 7.1c）",
            f"eval_reports 中 precision/f1 列为 0.0 —— 是**未计算**（评测脚本只算了召回），"
            f"不是精确率为 0；报告 {ev['report_id']} 的 detail 含 {ev['fingerprint_drift']} 项指纹漂移"))
        out.append(_metric(
            "eval_negative_control", "评测负对照召回（真实性证明）",
            round((ev["negative_control_recall5"] or 0) * 100, 1), "%", "global",
            "= 0%（轮转 gold 后应归零）",
            _status(ev["negative_control_recall5"] or 0, lower_better=True, warn=0),
            threshold="负对照分数应与真实分数差出量级；不为 0 说明指标未在真测对齐关系",
            detail=f"负例 {ev['neg_n']} 例，路由正确率 {ev['neg_route_ok']}，"
                   f"假图自信 {ev['neg_false_graph_confident']} 例"))
        out.append(_metric(
            "eval_age_days", "评测数据新鲜度", ev["age_days"], "天", "global",
            "≤ 30 天", _status(ev["age_days"], lower_better=True, warn=30),
            threshold=f"最近一次评测 {ev['created_at']}",
            detail=f"本体版本 {ev['ontology_active']}；指纹漂移 {ev['fingerprint_drift']} 项"))
    return out


def _performance_group(conn) -> list:
    """性能与运维簇：RT 分位 / 慢查询 / 服务健康 / 审计与 LLM 活跃度。"""
    lat = metrics_core.latency_stats(conn)
    slow = metrics_core.slow_queries(conn)
    health = metrics_core.service_health(conn)
    act = metrics_core.activity_stats(conn)
    route_txt = "；".join(f"{k} n={v['n']} p50={v['p50']}ms p95={v['p95']}ms"
                         for k, v in (lat["by_route"] or {}).items())
    slow_txt = "；".join(f"{i['route']} {i['latency_ms']}ms「{i['query'][:24]}」"
                        for i in slow["items"][:3]) or "无"
    offline = [c["name"] for c in health["components"] if c["status"] not in
               ("online", "active", "enabled", "ok")]
    return [
        _metric("engine_rt_p50", "检索延迟 p50", lat["p50"], "ms", "global",
                "< 500 ms（甲方 7.1a：API ≤500ms）",
                _status(lat["p50"], lower_better=True, warn=500),
                threshold="口径：query_routing_stats.latency_ms 分位（Python 内计算，样本全量）",
                detail=f"样本 {lat['n']} 次；{route_txt}"),
        _metric("engine_rt_p95", "检索延迟 p95", lat["p95"], "ms", "global",
                "< 2000 ms", _status(lat["p95"], lower_better=True, warn=2000),
                threshold="长尾看 p95；首屏/API 指标以甲方 7.1(a) 为准",
                detail=f"p90={lat['p90']}ms max={lat['max']}ms"),
        # ⚠️ 直接用计数算百分数：先取整 rate 再 *100 会双重取整（280/471 曾显示 59.5%，真值 59.4%）
        _metric("slow_query_share", "慢查询占比（>2s）",
                round(slow["slow"] * 100 / slow["total"], 1) if slow["total"] else 0.0, "%", "global",
                "≤ 10%", _status(slow["rate"] * 100, lower_better=True, warn=10),
                threshold=f"阈值 {slow['threshold']}ms；全量样本 {slow['total']} 次",
                detail=f"慢查询 {slow['slow']} 条；TOP：{slow_txt}"),
        _metric("service_health", "服务组件健康度",
                round(health["rate"] * 100, 1), "%", "global",
                "= 100%", _status(health["rate"] * 100, ok=100, warn=80),
                threshold="真实探活（SQLite / pyoxigraph / Fuseki 端口 / LLM provider 状态）",
                detail=f"{health['online']}/{health['total']} 在线"
                       + (f"；离线：{'、'.join(offline)}" if offline else "")),
        _metric("audit_activity", f"审计事件（近 {act['days']} 天）", act["audit_recent"], "次",
                "global", "—", "ok",
                threshold="FR-UI-6 / 7.4(e)：用户与大模型交互、上传下载均须留痕",
                detail=f"近 30 天 {act['audit_30d']} 次 / 累计 {act['audit_total']} 次"),
        _metric("llm_activity", f"LLM 调用（近 {act['days']} 天）", act["llm_recent"], "次",
                "global", "—", "ok",
                threshold="llm_usage_stats 真实计数（含 mock 标记）",
                detail=f"近 30 天 {act['llm_30d']} 次 / 累计 {act['llm_total']} 次"),
    ]


def _acceptance(conn, flat: dict) -> list:
    """甲方 7.1 与 PRD §2.2 的**逐条验收对照**（metrics 的视图，不重复计算）。

    每行通过 `metric_key` 引用同一口径；`value` 恒等于 `flat[metric_key].value`
    （自检脚本有断言）。无实测数据的行 value=None 且注明"需压测立项"，不预置假值。
    """
    def row(key, clause, req, target, metric_key=None, value=None, unit="", status="warn",
            evidence=""):
        if metric_key and metric_key in flat and value is None:
            value = flat[metric_key]["value"]
            unit = unit or flat[metric_key].get("unit", "")
        if status == "auto" and metric_key and metric_key in flat:
            status = flat[metric_key]["status"]
        calc = ""
        if metric_key and metric_key in flat:
            calc = ("本行「实测」直接引用指标 " + metric_key + "，口径完全一致：\n"
                    + (flat[metric_key].get("calc") or ""))
        else:
            calc = ("本行无指标同源（" + (evidence or "无实测数据")
                    + "）—— 属「需专项压测/立项」项，不是从库中算出来的值。")
        return {"key": key, "clause": clause, "requirement": req, "target": target,
                "value": value, "unit": unit, "status": status,
                "evidence": evidence, "metric_key": metric_key or "", "calc": calc}

    ai = metrics_core.ai_review_rate(conn)
    return [
        row("acc_71_a", "甲方 7.1(a)", "轻量查询 RT / 首屏 ≤2s（API ≤500ms）", "≤ 500 ms",
            "engine_rt_p50", status="auto",
            evidence="无 API 级压测；以内部检索 p50 代（口径见 engine_rt_p50.detail）"),
        row("acc_71_b", "甲方 7.1(b)", "重量级 RT/并发 + TPS ≥ 50", "TPS ≥ 50",
            status="warn", evidence="无实测：需专项压测立项"),
        row("acc_71_c", "甲方 7.1(c)", "知识获取 结构化 召回 ≥95%", "≥ 95%",
            "eval_recall_structured", status="auto",
            evidence="eval_reports#6 图谱侧 entity_recall（46 例评测集）"),
        row("acc_71_d", "甲方 7.1(d)", "知识获取 半/非结构化 召回 ≥75%", "≥ 75%",
            "eval_recall_semi", status="auto",
            evidence="eval_reports#6 文档侧 recall@5"),
        row("acc_71_e", "甲方 7.1(e)", "支持 ≥20 人并发", "≥ 20 人",
            status="warn", evidence="无实测：需专项压测立项"),
        row("acc_71_f", "甲方 7.1(f)", "≥10000 元素上下文", "≥ 10000",
            status="warn", evidence="无实测：上下文预算已有裁剪实现（见 tests 裁剪用例）"),
        row("acc_prd_rate", "PRD §2.2", "AI 建模入库审核率 0% → 100%", "= 100%",
            "ai_review_rate", status="auto",
            evidence=f"v2g_candidates {ai['all_total']} 条中 ai_model 通道 "
                     f"{ai['ai_reviewed']}/{ai['scope_total']} 已离 pending"),
        row("acc_prd_alias", "PRD §2.2", "中英文实体互认率 ~30% → ≥90%", "≥ 90%",
            "alias_coverage", status="auto", evidence="别名库未落地（entity_aliases 0 行）"),
        row("acc_prd_types", "PRD §2.2", "本体类型覆盖率 7/22 → ≥18/22（82%）", "≥ 82%",
            "type_coverage_sysml", status="auto",
            evidence="分母改用 SysML v2 元素类型（14 类），口径见该指标 threshold"),
        row("acc_prd_flow", "PRD §2.2", "标注审核流程完整度：五区显式可见", "五区可见",
            status="ok", evidence="治理中心 16-review.js 五区流程条；合并请求跑通 2 次"),
        row("acc_prd_dedup", "PRD §2.2", "实体消歧准确率 → ≥85%", "≥ 85%",
            "dedup_accuracy", status="auto", evidence="无评测集（entity_dup_candidates 0 行）"),
    ]


def _ai_review_metric(conn) -> dict:
    """AI 建模入库审核率（PRD §2.2 目标 100%）——放在治理簇（是治理指标，不是质量指标）。"""
    a = metrics_core.ai_review_rate(conn)
    r = round((a["rate"] or 0) * 100, 1) if a["rate"] is not None else None
    return _metric("ai_review_rate", "AI 建模入库审核率", r, "%", "global",
                   "= 100%（PRD §2.2）",
                   _status(a["rate"] * 100, ok=100, warn=50) if a["rate"] is not None else "warn",
                   threshold="v2g_candidates 中 ai_model 通道离开 pending 的占比"
                             + ("（当前库无 ai_model 标记，退化为全部候选口径）"
                                if not a["scoped_to_ai"] else ""),
                   detail=f"ai_model {a['ai_reviewed']}/{a['ai_total']}；"
                          f"全部候选 {a['all_total']} 条口径 {a['scope_total']}",
                   drill={"page": "kb-a", "title": "在本页实体浏览按「候选」过滤",
                          "kind": "entity_status", "value": "candidate"})


@router.get("/api/knowledge/dashboard")
def knowledge_dashboard(branch: str = "", window: str = "all", run_shacl: bool = False,
                        persist: bool = False, conn=Depends(db_session)):
    """知识看板一次聚合（唯一口径来源）。

    参数：
    - `branch`：工作分支（前端传 `getCurrentBranch()`）。
    - `window`：`7d|30d|all`，影响 fallback 主题词时间窗。
    - `run_shacl`：true 时现场跑一次 SHACL 门禁（约 1s）。
    - `persist`：true 时把本次指标落 `dashboard_metric_snapshots`（按天幂等）供趋势图。

    返回：`scopes`（三作用域原始计数）/ `drift` / `redlines` / `groups`（四簇）/
    `acceptance`（验收对照）/ `metrics`（扁平索引，供一致性断言）/ `northstar` / `summary`。
    """
    days = _WINDOW_DAYS.get(window, 3650)
    branch = (branch or "").strip()
    rel = metrics_core.release_branches(conn)

    scopes = {
        "release": metrics_core.release_counts(conn),
        "branch": {"name": branch,
                   "entities": metrics_core.entity_counts(conn, branch),
                   "relations": metrics_core.relation_counts_by_status(conn, branch)},
        "global": metrics_core.global_counts(conn),
    }
    drift = metrics_core.branch_drift(conn, branch) if branch else {
        "comparable": False, "reason": "未指定工作分支"}
    redlines = metrics_core.redlines(conn)
    groups = [
        {"key": "governance", "name": "知识治理健康度", "scope": "global",
         "items": _governance_group(conn, run_shacl) + [_ai_review_metric(conn)]},
        {"key": "quality", "name": "数据质量", "scope": "global",
         "items": _quality_group(conn, days)},
        {"key": "consumption", "name": "知识消费", "scope": "global",
         "items": _consumption_group(conn)},
        {"key": "performance", "name": "性能与运维", "scope": "global",
         "items": _performance_group(conn)},
    ]
    flat = {m["key"]: m for m in redlines}
    for g in groups:
        for m in g["items"]:
            flat[m["key"]] = m
    _with_calc(flat)   # 计算说明随指标下发（前端 ⓘ hover 展示）

    scored = [m for m in flat.values() if m["value"] is not None]
    n_ok = sum(1 for m in scored if m["status"] == "ok")
    n_warn = sum(1 for m in scored if m["status"] == "warn")
    n_alert = sum(1 for m in scored if m["status"] == "alert")
    health = round(100 * (n_ok + 0.5 * n_warn) / len(scored)) if scored else None

    if persist:
        try:
            metrics_core.save_snapshot(conn, flat, branch)
        except Exception:
            pass  # 快照失败不影响看板读取（表未建/锁冲突等）

    return {
        "ok": True,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": window,
        "branch": {"current": branch, "release": rel,
                   # ⚠️ db_session 的连接 row_factory=sqlite3.Row，Row **没有 .get()**；
                   # 需要缺省值时必须 dict(row) 包一层（2026-09-26 曾因此让本端点 500）
                   "available": [dict(b) for b in conn.execute(
                       "SELECT name, branch_type, status FROM branches "
                       "ORDER BY branch_type, name").fetchall()]},
        "scope_legend": SCOPE_LEGEND,
        "scopes": scopes,
        "drift": drift,
        "redlines": redlines,
        "groups": groups,
        "acceptance": _acceptance(conn, flat),
        "northstar": {"health_score": health, "keys": NORTHSTAR_KEYS,
                      "scored": len(scored), "na": len(flat) - len(scored)},
        "metrics": flat,
        "summary": {"alert": n_alert, "warn": n_warn, "ok": n_ok,
                    "na": len(flat) - len(scored), "health_score": health},
    }


@router.get("/api/knowledge/dashboard/trend")
def knowledge_dashboard_trend(branch: str = "", keys: str = "", limit: int = 12,
                              conn=Depends(db_session)):
    """指标历史趋势（sparkline 数据源）。

    `keys` 逗号分隔；缺省取北极星 + 红线相关指标。返回 `{key: [{date,value,status}]}`。
    ⚠️ 表 `dashboard_metric_snapshots` 由 `_migrate_dashboard_snapshots` 幂等建立；
    未落快照的指标返回空数组（前端不画线，不伪造趋势）。
    """
    klist = [k.strip() for k in (keys or "").split(",") if k.strip()] or (
        NORTHSTAR_KEYS + ["type_coverage_sysml", "engine_rt_p50", "ontology_required_attrs"])
    return {"ok": True, "branch": (branch or "").strip(),
            "trends": metrics_core.trends(conn, klist, branch, max(1, min(limit, 90)))}