# -*- coding: utf-8 -*-
"""变更影响分析引擎（FR-CIA-1/4）：图谱依赖网络上的 BFS 多级传播计算 + 沙箱变更模拟。

- analyze_graph：给定图谱（节点/边）+ 变更源，做参数化 BFS 传播，产出影响节点/边、
  影响度评分（关系权重 × 层衰减）、直接/间接、深度/广度结构化统计、规则风险分析。
- simulate：沙箱预演——在内存副本上应用变更（modify/delete/add），重算影响，
  输出 before/after 对比 + diff（新增/解除/影响度变化/风险），全程不写正式图谱表。
- SCENES：内置多层级依赖演示场景（FR-CIA-4 验证用），可离线演示 N 层传播与变更预演。

与 agent/pipeline.py::_card_impact 共享同一算法（pipeline 已重构复用 analyze_graph）。
"""
from copy import deepcopy
import datetime as _dt

# ── 影响权重表（行业实践默认：组合/实现 > 流/依赖 > 泛化/引用 > 关联；中英文兼容）──
REL_WEIGHT = {
    "组合": 1.0, "实现": 1.0, "分配": 1.0, "流": 0.7, "依赖": 0.7,
    "泛化": 0.4, "引用": 0.4, "关联": 0.2, "派生": 1.0, "包含": 1.0,
    "满足": 1.0, "验证": 0.7, "追溯": 0.4, "连接": 0.2, "冲突": 0.2,
    "CONTAINS": 1.0, "COMPOSITION": 1.0, "IMPLEMENTS": 1.0,
    "REALIZES": 1.0, "ASSIGNS": 1.0, "SATISFIES": 1.0,
    "DERIVES": 1.0, "ALLOCATED_TO": 1.0,
    "FLOW": 0.7, "DEPENDS": 0.7, "DEPENDENCY": 0.7, "USES": 0.7,
    "FLOW_TO": 0.7, "DEPENDS_ON": 0.7, "VERIFIED_BY": 0.7,
    "GENERALIZATION": 0.4, "REFERENCE": 0.4, "REFERENCES": 0.4,
    "TRACE": 0.4, "CONNECTS": 0.2, "ASSOCIATED_WITH": 0.2,
    "ASSOCIATION": 0.2, "CONFLICTS": 0.2,
}
ALPHA = 0.6  # 层衰减（越远影响越小）
# 实体类型 → 工程领域（跨领域影响广度统计）
DOMAIN_MAP = {
    "系统": "系统结构", "卫星系统": "系统结构", "卫星平台": "系统结构", "系统元素": "系统结构",
    "电源分系统": "电源与能源", "姿轨控分系统": "姿轨控", "测控分系统": "测控", "热控分系统": "热控",
    "有效载荷": "有效载荷", "通信载荷": "有效载荷", "载荷": "有效载荷", "转发器": "有效载荷",
    "天线": "有效载荷", "相控阵天线": "有效载荷", "功率放大器": "有效载荷", "变频器": "有效载荷",
    "滤波器": "有效载荷", "TWTA": "有效载荷", "部件": "有效载荷",
    "地面段": "地面段", "信关站": "地面段", "测控站": "地面段", "用户段": "地面段",
    "用户终端": "用户段", "通信链路": "链路", "上行链路": "链路", "下行链路": "链路",
    "需求": "需求", "利益相关方需求": "需求", "系统需求": "需求", "子系统需求": "需求", "单元需求": "需求",
    "功能": "功能", "用例": "用例", "利益相关方": "利益相关方", "验证活动": "验证与测试",
    "指标": "指标", "组件": "结构",
}


def _rw(t):
    """关系类型 → 影响权重（未命中取 0.5）。"""
    return REL_WEIGHT.get((t or "").upper(), 0.5)


def analyze_graph(nodes: list, edges: list, source: dict,
                  depth: int = 3, direction: str = "both",
                  boost_ids: list | None = None) -> dict:
    """从 source 在依赖网络上做 BFS 多级传播（复用 pipeline._card_impact 核心算法）。

    - nodes: [{id, name, entity_type(或 type), status}]；edges: [{source_id, target_id, relation_type}]
    - depth: 最大层数（0=不限）；direction: both|up|down（up 沿 target_id→source_id 传播）
    - boost_ids: 变更升级元素集合——这些元素出发的边权重 ×1.3（沙箱 modify 预演用）
    返回与 _card_impact 一致的完整结果（含深度/广度统计 + 规则风险分析，不含 LLM 建议）。
    """
    ent_of = {}
    for n in nodes:
        ent_of.setdefault(str(n.get("id")), n)
    rel_map = {}
    for r in edges:
        rel_map.setdefault(str(r.get("source_id")), []).append(r)
        rel_map.setdefault(str(r.get("target_id")), []).append(r)
    sid = str(source.get("id"))
    boost = set(str(i) for i in (boost_ids or []))
    alpha = ALPHA
    # BFS 首达去重（避免环回/源节点重复出现）
    visited = {sid: (0, 0.0)}
    queue = [(sid, 0, 0.0)]
    impact_nodes, impact_edges = [], []
    while queue:
        nid, d, score = queue.pop(0)
        if depth > 0 and d >= depth:
            continue
        ent = ent_of.get(nid)
        if ent:
            impact_nodes.append({
                "id": nid, "name": ent.get("name", nid),
                "type": ent.get("entity_type") or ent.get("type", ""),
                "depth": d,
                "impact": "source" if d == 0 else ("direct" if d == 1 else "indirect"),
                "score": round(score, 3),
                "level": "high" if score >= 0.7 else ("mid" if score >= 0.4 else "low"),
            })
        for r in rel_map.get(nid, []):
            out = str(r.get("source_id")) == nid
            if direction == "down" and not out:
                continue
            if direction == "up" and out:
                continue
            next_id = str(r.get("target_id")) if out else str(r.get("source_id"))
            if next_id in visited:
                continue
            w = _rw(r.get("relation_type"))
            if out and nid in boost:      # 变更升级：该元素出发的传播权重增强
                w *= 1.3
            if not out and nid in boost:  # 上游同样增强（双向敏感）
                w *= 1.3
            nscore = w * (alpha ** d)
            visited[next_id] = (d + 1, nscore)
            impact_edges.append({
                "from": r.get("source_id"), "to": r.get("target_id"),
                "type": r.get("relation_type"),
                "impact": "direct" if d == 0 else "indirect",
                "score": round(nscore, 3),
            })
            queue.append((next_id, d + 1, nscore))
    # 深度边界裁剪后，过滤两端不在影响集内的边（如最外层节点的入边），
    # 保证 impact_nodes/impact_edges 在图谱渲染时严格一致（Cytoscape 不允许悬空边）
    node_ids = {str(n["id"]) for n in impact_nodes}
    impact_edges = [e for e in impact_edges
                    if str(e.get("from")) in node_ids and str(e.get("to")) in node_ids]
    direct_count = sum(1 for n in impact_nodes if n["impact"] == "direct")
    indirect_count = sum(1 for n in impact_nodes if n["impact"] == "indirect")
    affected = [n for n in impact_nodes if n["impact"] != "source"]
    impact_levels = {
        "high": sum(1 for n in affected if n["level"] == "high"),
        "mid": sum(1 for n in affected if n["level"] == "mid"),
        "low": sum(1 for n in affected if n["level"] == "low"),
    }
    # 深度统计（影响深度：各层直接/间接分布）
    depth_stats = {}
    for n in affected:
        d = depth_stats.setdefault(n["depth"], {"direct": 0, "indirect": 0, "nodes": []})
        d["direct" if n["impact"] == "direct" else "indirect"] += 1
        d["nodes"].append(n["name"])
    max_depth = max([n["depth"] for n in affected], default=0)
    # 广度统计（类型分布 / 关系类型分布 / 跨领域影响）
    type_stats = {}
    for n in affected:
        type_stats[n["type"]] = type_stats.get(n["type"], 0) + 1
    rel_type_stats = {}
    for e in impact_edges:
        rel_type_stats[e["type"]] = rel_type_stats.get(e["type"], 0) + 1
    domain_stats = {}
    for n in affected:
        dom = DOMAIN_MAP.get(n["type"], n["type"] or "其他")
        domain_stats.setdefault(dom, {"count": 0, "nodes": []})
        domain_stats[dom]["count"] += 1
        domain_stats[dom]["nodes"].append(n["name"])
    # 规则风险分析（LLM 建议由调用方补充）
    top_risk = sorted(affected, key=lambda n: -(n.get("score") or 0))[:5]
    coverage = round(len(affected) / max(len(nodes), 1) * 100, 1)
    risks = []
    if top_risk:
        risks.append({
            "level": "high" if top_risk[0]["level"] == "high" else "mid",
            "title": f"高风险节点：{top_risk[0]['name']}（影响度 {top_risk[0]['score']}）",
            "desc": (f"变更将沿 {len(rel_type_stats)} 类关系传播 {max_depth} 层，"
                     f"共影响 {len(affected)} 个元素，占图谱 {coverage}%；"
                     f"直接 {direct_count} / 间接 {indirect_count}，高影响 {impact_levels['high']} 个。"),
            "advice": f"优先复核 {top_risk[0]['name']} 及其同层元素，评估是否触发级联变更。",
        })
    return {
        "ok": True,
        "change_source": source,
        "impact_nodes": impact_nodes,
        "impact_edges": impact_edges,
        "direct_count": direct_count,
        "indirect_count": indirect_count,
        "impact_levels": impact_levels,
        "depth_stats": depth_stats,
        "type_stats": type_stats,
        "rel_type_stats": rel_type_stats,
        "domain_stats": domain_stats,
        "risk_analysis": {"risks": risks, "top_risk": top_risk, "coverage": coverage,
                          "max_depth": max_depth},
        "depth": depth,
        "direction": direction,
        # 2026-09-15：use_vector 不再硬编码占位——由调用方 attach_evidence 按实际证据置位
        "params": {"depth": depth, "direction": direction,
                   "relation_types": [], "reference_sources": [], "use_vector": False},
    }


def attach_evidence(conn, card: dict, top_n: int = 6, per_hit: int = 2) -> dict:
    """混合溯源（FR-CIA-1）：受影响 Top 元素 → 向量库（document_chunks）文档证据。

    图谱依赖网络是主证据源；图谱覆盖不足（知识库不完整）时，用向量库检索
    相关文档作为补充佐证（需求 a)"实现混合溯源"）。对影响度最高的 top_n 个
    受影响元素各检索 per_hit 条 chunk 命中，写入 card["evidence"]：
      [{element, type, score, hits:[{source_doc, score, snippet, origin}]}]
    并真实置位 card["params"]["use_vector"]（有证据=True）。无 conn/无命中时静默跳过。
    """
    try:
        from knowledge_pipeline import search_chunks
        affected = [n for n in (card.get("impact_nodes") or []) if n.get("impact") != "source"]
        top = sorted(affected, key=lambda n: -(n.get("score") or 0))[:top_n]
        evidence = []
        for n in top:
            name = (n.get("name") or "").strip()
            if not name:
                continue
            try:
                hits = search_chunks(conn, name, top_k=per_hit) or []
            except Exception:
                hits = []
            if hits:
                evidence.append({
                    "element": name, "type": n.get("type", ""), "score": n.get("score"),
                    "hits": [{"source_doc": h.get("source_doc", ""),
                              "score": h.get("score"),
                              "snippet": (h.get("content") or "")[:160],
                              "origin": h.get("origin") or "upload"} for h in hits],
                })
        card["evidence"] = evidence
        card["params"] = card.get("params") or {}
        card["params"]["use_vector"] = bool(evidence)
        card["params"]["evidence_count"] = sum(len(e["hits"]) for e in evidence)
    except Exception:
        pass
    return card


def baseline_meta(conn, branches=None) -> dict:
    """分析基线元数据（S2/C3，2026-09-16）：分支、已发布本体版本、轻量一致性扫描（悬空实例计数）。

    供影响报告第 2 章"基线一致性检查"回填真实数据，报告头声明分析基线可复算。
    """
    if branches is None:
        branches = [r["name"] for r in conn.execute(
            "SELECT name FROM branches WHERE branch_type='release' AND status='active'").fetchall()] or ["release"]
    in_sql = ",".join("?" * len(branches))
    version_label, version_at = "", ""
    try:
        v = conn.execute(
            "SELECT version_label, created_at FROM ontology_versions "
            "WHERE status='released' ORDER BY id DESC LIMIT 1").fetchone()
        if v:
            version_label, version_at = (v["version_label"] or ""), (v["created_at"] or "")
    except Exception:
        pass
    dangling = 0
    try:
        dangling = conn.execute(
            f"SELECT COUNT(*) FROM entities e WHERE e.status!='deprecated' AND e.branch IN ({in_sql}) "
            "AND e.entity_type NOT IN (SELECT name FROM ontology_types WHERE status!='deprecated')",
            branches).fetchone()[0]
    except Exception:
        pass
    return {"branch": "+".join(branches), "version_label": version_label, "version_at": version_at,
            "dangling_instances": dangling,
            "checked_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def build_baseline(conn, change_source_name: str, depth: int = 3,
                   direction: str = "both") -> dict:
    """为沙箱预演构建真实图谱基线（FR-CIA-4，2026-09-15 补齐"真实模型预演"缺口）。

    与 _card_impact 同口径：release 已发布分支的 entities/relations 依赖网络 +
    变更源解析（精确名 → 双向包含；歧义返回候选）。不启用 LLM 抽取（前端从
    分析卡直接传元素名，无歧义场景）。
    返回 {ok, graph:{nodes,edges}, source, depth, direction} 或
         {ok: False, code: SOURCE_AMBIGUOUS|NO_CHANGE_SOURCE, reason, candidates}
    """
    kb_branches = [r["name"] for r in conn.execute(
        "SELECT name FROM branches WHERE branch_type='release' AND status='active'").fetchall()]
    if not kb_branches:
        kb_branches = ["release"]
    in_sql = ",".join("?" * len(kb_branches))
    entities = [dict(e) for e in conn.execute(
        f"SELECT id, name, entity_type, status FROM entities "
        f"WHERE status!='deprecated' AND branch IN ({in_sql})", kb_branches).fetchall()]
    relations = [dict(r) for r in conn.execute(
        f"SELECT r.*, e1.name as src_name, e2.name as tgt_name FROM relations r "
        f"JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
        f"WHERE r.status!='deprecated' AND r.branch IN ({in_sql})", kb_branches).fetchall()]
    if not entities:
        return {"ok": False, "code": "NO_CHANGE_SOURCE",
                "reason": "release 已发布分支无模型数据（先入库发布后再预演）"}
    name = (change_source_name or "").strip().lower()
    if not name:
        return {"ok": False, "code": "NO_CHANGE_SOURCE", "reason": "缺少变更源名称"}
    exact = [e for e in entities if (e.get("name") or "").lower() == name]
    if len(exact) == 1:
        source = exact[0]
    elif len(exact) > 1:
        return {"ok": False, "code": "SOURCE_AMBIGUOUS",
                "reason": f"匹配到 {len(exact)} 个同名候选变更源",
                "candidates": [{"id": e["id"], "name": e["name"], "type": e["entity_type"]}
                               for e in exact[:10]]}
    else:
        partial = [e for e in entities
                   if (e.get("name") or "").lower()
                   and (name in e["name"].lower() or e["name"].lower() in name)]
        if len(partial) == 1:
            source = partial[0]
        elif len(partial) > 1:
            return {"ok": False, "code": "SOURCE_AMBIGUOUS",
                    "reason": f"匹配到 {len(partial)} 个候选变更源",
                    "candidates": [{"id": e["id"], "name": e["name"], "type": e["entity_type"]}
                                   for e in partial[:10]]}
        else:
            return {"ok": False, "code": "NO_CHANGE_SOURCE",
                    "reason": f"图谱中未找到变更源: {change_source_name}"}
    return {"ok": True, "graph": {"nodes": entities, "edges": relations},
            "source": source, "depth": depth, "direction": direction,
            "baseline": baseline_meta(conn, kb_branches)}


def simulate(graph: dict, source: dict, changes: list,
             depth: int = 3, direction: str = "both") -> dict:
    """沙箱变更预演（FR-CIA-4）：内存副本应用变更 → 重算影响 → before/after 对比。

    graph: {nodes, edges, title?}；changes: [{op: modify|delete|add, target, new_value?, node?, edge?}]
    - modify：变更升级——目标元素出发的传播权重增强（影响度上升）
    - delete：删除目标元素及关联边（解除下游影响）
    - add：新增元素+边（出现新影响）
    全程不写 entities/relations 正式表；返回 {before, after, comparison}。
    """
    before = analyze_graph(graph.get("nodes", []), graph.get("edges", []),
                           source, depth, direction)
    nodes = deepcopy(graph.get("nodes", []))
    edges = deepcopy(graph.get("edges", []))
    boost_ids, applied = [], []
    for ch in changes or []:
        op = (ch or {}).get("op", "")
        target = (ch or {}).get("target", "")
        if op == "modify":
            boost_ids.append(target)
            applied.append(ch)
        elif op == "delete":
            nodes = [n for n in nodes if str(n.get("id")) != str(target)]
            edges = [e for e in edges
                     if str(e.get("source_id")) != str(target) and str(e.get("target_id")) != str(target)]
            applied.append(ch)
        elif op == "add":
            if ch.get("node") and ch.get("edge"):
                nodes.append(ch["node"])
                edges.append(ch["edge"])
                applied.append(ch)
    after = analyze_graph(nodes, edges, source, depth, direction, boost_ids=boost_ids)

    b_map = {n["id"]: n for n in before["impact_nodes"] if n["impact"] != "source"}
    a_map = {n["id"]: n for n in after["impact_nodes"] if n["impact"] != "source"}
    added = [n for nid, n in a_map.items() if nid not in b_map]
    removed = [n for nid, n in b_map.items() if nid not in a_map]
    degree = []
    for nid in b_map:
        if nid in a_map:
            dlt = round(a_map[nid]["score"] - b_map[nid]["score"], 3)
            if abs(dlt) >= 0.001:
                degree.append({"id": nid, "name": a_map[nid]["name"], "type": a_map[nid]["type"],
                               "before": b_map[nid]["score"], "after": a_map[nid]["score"], "delta": dlt})
    degree.sort(key=lambda x: -abs(x["delta"]))
    # 风险提示
    risk = []
    hi_added = [n for n in added if n.get("level") == "high"]
    if hi_added:
        risk.append({"level": "high",
                     "title": f"新增 {len(hi_added)} 个高影响节点",
                     "desc": "；".join(f"{n['name']}（{n['type']}，影响度 {n['score']}）" for n in hi_added[:3]),
                     "advice": "评估新增影响是否触发需求/接口变更，必要时启动 CCB 评审。"})
    if removed:
        risk.append({"level": "mid",
                     "title": f"解除 {len(removed)} 个影响（删除类变更）",
                     "desc": "；".join(n["name"] for n in removed[:5]),
                     "advice": "确认删除目标未被组合/分配关系强依赖，避免影响评估失真。"})
    if degree:
        up = sum(1 for d in degree if d["delta"] > 0)
        risk.append({"level": "mid",
                     "title": f"{len(degree)} 个元素影响度变化（{up} 升 / {len(degree)-up} 降）",
                     "desc": "；".join(f"{d['name']} {d['before']}→{d['after']}" for d in degree[:4]),
                     "advice": "关注影响度上升元素，复核其变更承受裕度。"})
    comparison = {
        "added": added, "removed": removed, "degree_changes": degree[:10],
        "risk": risk,
        "summary": {
            "before_direct": before.get("direct_count", 0),
            "before_indirect": before.get("indirect_count", 0),
            "after_direct": after.get("direct_count", 0),
            "after_indirect": after.get("indirect_count", 0),
            "changes": len(applied),
        },
    }
    return {"before": before, "after": after, "comparison": comparison}


# ══════════════════════════════════════════════════════════════════
# 内置多层级依赖演示场景（FR-CIA-4 沙箱预演用，独立于正式图谱）
# 层级：电源分系统 → 平台/载荷 → 设备 → 链路/需求 → 验证（≥5 层）
# ══════════════════════════════════════════════════════════════════
SIM_GRAPH = {
    "nodes": [
        {"id": "PWR-001", "name": "电源分系统", "entity_type": "电源分系统", "status": "reviewed"},
        {"id": "PLT-001", "name": "卫星平台", "entity_type": "卫星平台", "status": "reviewed"},
        {"id": "SAT-001", "name": "宽带通信卫星", "entity_type": "卫星系统", "status": "reviewed"},
        {"id": "PAY-001", "name": "通信有效载荷", "entity_type": "通信载荷", "status": "reviewed"},
        {"id": "AMP-001", "name": "功率放大器", "entity_type": "功率放大器", "status": "reviewed"},
        {"id": "ENT-002", "name": "转发器", "entity_type": "转发器", "status": "reviewed"},
        {"id": "ANT-001", "name": "多波束天线", "entity_type": "天线", "status": "reviewed"},
        {"id": "LINK-UL", "name": "Ka上行链路", "entity_type": "上行链路", "status": "reviewed"},
        {"id": "LINK-DL", "name": "Ka下行链路", "entity_type": "下行链路", "status": "reviewed"},
        {"id": "GS-001", "name": "主信关站", "entity_type": "信关站", "status": "reviewed"},
        {"id": "GS-NET", "name": "地面业务网络", "entity_type": "地面段", "status": "reviewed"},
        {"id": "UT-001", "name": "用户终端", "entity_type": "用户终端", "status": "reviewed"},
        {"id": "REQ-SYS-010", "name": "宽带接入系统需求", "entity_type": "系统需求", "status": "reviewed"},
        {"id": "REQ-SUB-030", "name": "链路预算子系统需求", "entity_type": "子系统需求", "status": "reviewed"},
        {"id": "REQ-UNT-041", "name": "EIRP单元需求", "entity_type": "单元需求", "status": "reviewed"},
        {"id": "VER-001", "name": "链路预算验证", "entity_type": "验证活动", "status": "reviewed"},
        {"id": "FUN-001", "name": "宽带接入功能", "entity_type": "功能", "status": "reviewed"},
        {"id": "UC-001", "name": "用户接入互联网", "entity_type": "用例", "status": "reviewed"},
    ],
    "edges": [
        {"source_id": "SAT-001", "target_id": "PLT-001", "relation_type": "CONTAINS"},
        {"source_id": "SAT-001", "target_id": "PAY-001", "relation_type": "CONTAINS"},
        {"source_id": "PLT-001", "target_id": "PWR-001", "relation_type": "CONTAINS"},
        {"source_id": "PAY-001", "target_id": "AMP-001", "relation_type": "CONTAINS"},
        {"source_id": "PAY-001", "target_id": "ENT-002", "relation_type": "CONTAINS"},
        {"source_id": "PAY-001", "target_id": "ANT-001", "relation_type": "CONTAINS"},
        {"source_id": "PAY-001", "target_id": "PWR-001", "relation_type": "DEPENDS_ON"},
        {"source_id": "AMP-001", "target_id": "LINK-DL", "relation_type": "FLOW_TO"},
        {"source_id": "ENT-002", "target_id": "LINK-UL", "relation_type": "FLOW_TO"},
        {"source_id": "ANT-001", "target_id": "LINK-DL", "relation_type": "FLOW_TO"},
        {"source_id": "LINK-DL", "target_id": "GS-001", "relation_type": "CONNECTS"},
        {"source_id": "LINK-DL", "target_id": "UT-001", "relation_type": "CONNECTS"},
        {"source_id": "GS-001", "target_id": "GS-NET", "relation_type": "CONNECTS"},
        {"source_id": "SAT-001", "target_id": "REQ-SYS-010", "relation_type": "SATISFIES"},
        {"source_id": "PAY-001", "target_id": "REQ-SUB-030", "relation_type": "SATISFIES"},
        {"source_id": "AMP-001", "target_id": "REQ-UNT-041", "relation_type": "SATISFIES"},
        {"source_id": "REQ-UNT-041", "target_id": "VER-001", "relation_type": "VERIFIED_BY"},
        {"source_id": "FUN-001", "target_id": "PAY-001", "relation_type": "ALLOCATED_TO"},
        {"source_id": "FUN-001", "target_id": "UC-001", "relation_type": "REALIZES"},
    ],
}

SCENES = {
    "pwr_power_down": {
        "title": "电源分系统输出功率下调（12kW→10kW）",
        "source_id": "PWR-001",
        "depth": 5,
        "direction": "both",
        "graph": SIM_GRAPH,
        "changes": [
            {"op": "modify", "target": "PWR-001", "attr": "输出功率", "new_value": "10kW"},
            {"op": "delete", "target": "AMP-001", "attr": "功率放大器", "new_value": ""},
        ],
        "change_desc": "① 电源分系统输出功率 12kW→10kW（变更升级，下游供电裕度收紧）；"
                       "② 删除功率放大器（解除其链路/需求影响）。",
    },
    "pwr_power_up": {
        "title": "电源输出功率上调 + 新增高功率功放",
        "source_id": "PWR-001",
        "depth": 5,
        "direction": "both",
        "graph": SIM_GRAPH,
        "changes": [
            {"op": "modify", "target": "PWR-001", "attr": "输出功率", "new_value": "15kW"},
            {"op": "add", "target": "PA-002",
             "node": {"id": "PA-002", "name": "新增强放器", "entity_type": "功率放大器", "status": "reviewed"},
             "edge": {"source_id": "PAY-001", "target_id": "PA-002", "relation_type": "CONTAINS"}},
        ],
        "change_desc": "① 电源输出功率上调至 15kW；② 新增一台高功率放大器（新增影响分支）。",
    },
}


def get_scene(scene_id: str) -> dict | None:
    """取内置演示场景（含变更源对象化 + 变更描述）。"""
    sc = SCENES.get(scene_id)
    if not sc:
        return None
    nodes = sc["graph"].get("nodes", [])
    source = next((n for n in nodes if n.get("id") == sc["source_id"]), None)
    if not source:
        return None
    out = dict(sc)
    out["source"] = source
    return out


# ══════════════ 2026-09-15 报告 2.0：变更类型感知 + CPM-lite（见 docs/变更影响分析报告2.0设计方案.md）══════════════

# 关系类型 → 传播类（structure | requirement | flow | reference；未知归 reference）
REL_CLASS_MAP = {
    "组合": "structure", "包含": "structure", "CONTAINS": "structure", "COMPOSITION": "structure",
    "泛化": "structure", "GENERALIZATION": "structure", "分配": "structure", "ALLOCATED_TO": "structure",
    "实现": "structure", "IMPLEMENTS": "structure", "REALIZES": "structure", "派生": "structure", "DERIVES": "structure",
    "满足": "requirement", "SATISFIES": "requirement", "验证": "requirement", "VERIFIED_BY": "requirement",
    "追溯": "requirement", "TRACE": "requirement",
    "流": "flow", "FLOW": "flow", "FLOW_TO": "flow", "依赖": "flow", "DEPENDS": "flow",
    "DEPENDS_ON": "flow", "DEPENDENCY": "flow", "USES": "flow", "连接": "flow", "CONNECTS": "flow",
    "引用": "reference", "REFERENCE": "reference", "REFERENCES": "reference",
    "关联": "reference", "ASSOCIATION": "reference", "ASSOCIATED_WITH": "reference", "冲突": "reference", "CONFLICTS": "reference",
}

# 变更类型 × 传播类 → 权重系数（0=该类变更不沿此类关系传播；settings key=impact_change_matrix 可覆盖）
CHANGE_MATRIX = {
    "rename":    {"structure": 0.0, "requirement": 0.0, "flow": 0.0, "reference": 0.0},
    "value":     {"structure": 0.3, "requirement": 1.0, "flow": 0.5, "reference": 0.0},
    "interface": {"structure": 0.4, "requirement": 0.3, "flow": 1.0, "reference": 0.0},
    "attribute": {"structure": 0.8, "requirement": 0.5, "flow": 0.5, "reference": 0.2},
    "delete":    {"structure": 1.0, "requirement": 1.0, "flow": 1.0, "reference": 0.4},
}

CHANGE_TYPE_LABELS = {
    "rename": "改名", "value": "参数/值变更", "interface": "接口/关系变更",
    "attribute": "属性/构成变更", "delete": "删除", "structure": "结构敏感性分析",
}

# 路径枚举安全上限（CPM 组合计算指数增长；Pasqual & de Weck 实证传播极少超 4 步）
_MAX_PATHS = 5000
_MAX_DEPTH = 4


# 追溯性关系：仅表示元素间存在追溯/引用，不构成必然影响传导（S1 路径合理性判读，2026-09-16）
_TRACE_RELS = {"TRACE", "REFERENCES", "追溯", "引用", "DERIVED_FROM", "派生自"}


def _judge_path(hops: list) -> tuple:
    """S1 路径合理性判读：存在关系 ≠ 影响必然传播。

    - trace：全链为追溯/引用类关系 → 仅提示关联，不构成必然影响，需人工确认；
    - reasonable_partial：主链为结构/需求/流传播但含追溯段 → 追溯段需人工确认；
    - reasonable：结构/需求/流/依赖关系正向传导 → 影响按路径实际成立。
    """
    rels = [str(h.get("rel") or "").upper() for h in (hops or [])]
    if not rels:
        return "reasonable", ""
    if all(r in _TRACE_RELS for r in rels):
        return "trace", "纯追溯性关联——仅表示元素间存在追溯/引用，不构成必然影响，需人工确认是否实质受影响"
    if any(r in _TRACE_RELS for r in rels):
        return "reasonable_partial", "主链为结构/需求/流传播，含追溯性关联段——追溯段需人工确认"
    return "reasonable", "沿结构/需求/流/依赖关系传播——影响按路径实际成立"


def rel_class(rel_type: str) -> str:
    """关系类型 → 传播类（未知归 reference）。"""
    return REL_CLASS_MAP.get((rel_type or "").upper(), "reference")


def edge_weight(rel_type: str, change_type: str) -> float:
    """变更类型感知的传播权重 = 关系影响权重 × 变更矩阵系数（0 → 该边不传播）。"""
    return round(_rw(rel_type) * CHANGE_MATRIX.get(change_type, CHANGE_MATRIX["attribute"]).get(
        rel_class(rel_type), 0.0), 3)


def analyze_graph_v2(nodes: list, edges: list, source: dict, depth: int = 3,
                     direction: str = "both", change_type: str = "attribute",
                     change_desc: str = "") -> dict:
    """报告 2.0 引擎（CPM-lite）：变更类型感知 + 全路径枚举 + 组合风险 + 关键度画像。

    与 analyze_graph（v1）差异：
    - 传播权重 = REL_WEIGHT × CHANGE_MATRIX[change_type][rel_class]（变更类型定传播路径）
    - 路径枚举：从变更源 DFS 全部无环路径（限深 min(depth,4)，路径总数限 _MAX_PATHS）
    - 组合风险 P(target) = 1 - ∏(1 - P_path)（CPM 概率合并公式），替代 v1 的首达衰减分
    - 新增输出：risk_matrix / path_list / element_profile / change
    - 保留 v1 全部字段（impact_nodes/edges/counts/levels/depth_stats/...）向后兼容
    """
    change_type = change_type if change_type in CHANGE_MATRIX else "attribute"
    depth = min(depth, _MAX_DEPTH) if depth and depth > 0 else _MAX_DEPTH
    sid = str(source.get("id"))

    # 邻接表（direction 语义与 v1 一致；权重 0 的边按变更类型剪枝）
    # 2026-09-16 S1：每跳记录关系类型与遍历方向（fwd=沿边方向 / rev=逆向），供路径合理性判读
    rel_map = {}
    for r in edges:
        w = edge_weight(r.get("relation_type"), change_type)
        if w <= 0:
            continue
        s, t = str(r.get("source_id")), str(r.get("target_id"))
        rel_map.setdefault(s, []).append((t, r, w, "fwd"))
        if direction != "down":
            rel_map.setdefault(t, []).append((s, r, w, "rev"))  # 反向遍历（v1 同款：both/up 消费反向）

    # 全路径枚举（DFS 无环；depth=路径边数上限）
    max_len = depth if depth and depth > 0 else _MAX_DEPTH
    all_paths, path_counter = [], {"n": 0}

    def dfs(cur: str, path_ids: list, path_nodes: list, w_prod: float, hops: list):
        if path_counter["n"] >= _MAX_PATHS:
            return
        if len(path_ids) > 1:   # 路径至少 1 条边（排除仅源节点的平凡路径，避免污染 path_list/path_count）
            path_counter["n"] += 1
            all_paths.append({"node_ids": list(path_ids), "node_names": list(path_nodes),
                              "likelihood": round(w_prod, 4), "hops": [dict(h) for h in hops]})
        if len(path_ids) - 1 >= max_len:   # 深度 = 边数（与 v1 BFS「N 跳」口径一致，off-by-one 修复 2026-09-15）
            return
        for nxt, r, w, hdir in rel_map.get(cur, []):
            if nxt in path_ids:      # 环截断（CPM 同款）
                continue
            path_ids.append(nxt)
            path_nodes.append(_node_name(nodes, nxt))
            hops.append({"rel": r.get("relation_type") or "", "dir": hdir})
            dfs(nxt, path_ids, path_nodes, w_prod * w, hops)
            hops.pop()
            path_ids.pop()
            path_nodes.pop()

    dfs(sid, [sid], [_node_name(nodes, sid)], 1.0, [])

    # S1 路径合理性判读（2026-09-16）：存在关系 ≠ 影响必然传播
    for p in all_paths:
        p["plausibility"], p["plausibility_reason"] = _judge_path(p.get("hops") or [])

    # 按目标聚合（2026-09-16 语义收紧）：影响 = 结构/需求/流/依赖链传导（合理传播）；
    # 仅经纯追溯链可达的目标单列为"追溯性关联提示"（trace_hint）——不计入影响统计/评级/重测，
    # 回应"有关系≠有影响"：图谱与结论只呈现真实影响，追溯线索降级为人工确认提示。
    by_target, trace_by_target = {}, {}
    for p in all_paths:
        t = p["node_ids"][-1]
        if t == sid:
            continue
        if p.get("plausibility") == "trace":
            trace_by_target.setdefault(t, []).append(p)
        else:
            by_target.setdefault(t, []).append(p)
    ent_of = {str(n.get("id")): n for n in nodes}

    impact_nodes = [{"id": sid, "name": source.get("name", sid), "type": source.get("entity_type") or source.get("type", ""),
                     "depth": 0, "impact": "source", "score": 1.0, "level": "high"}]
    risk_matrix, max_depth = [], 0
    for t, paths in by_target.items():
        ent = ent_of.get(t) or {}
        combined = 1.0
        for p in paths:
            combined *= (1.0 - min(p["likelihood"], 1.0))
        combined = round(1.0 - combined, 4)
        min_hop = min(len(p["node_ids"]) - 1 for p in paths)
        max_depth = max(max_depth, min_hop)
        level = "high" if combined >= 0.7 else ("mid" if combined >= 0.4 else "low")
        impact_nodes.append({"id": t, "name": ent.get("name", t), "type": ent.get("entity_type") or ent.get("type", ""),
                             "depth": min_hop, "impact": "direct" if min_hop == 1 else "indirect",
                             "score": combined, "level": level})
        risk_matrix.append({"from": sid, "to": t, "combined": combined,
                            "direct": sum(1 for p in paths if len(p["node_ids"]) - 1 == 1),
                            "indirect": sum(1 for p in paths if len(p["node_ids"]) - 1 > 1),
                            "path_count": len(paths)})
    risk_matrix.sort(key=lambda m: -m["combined"])

    # 追溯性关联提示（不构成必然影响，不计入影响统计）：仅经纯追溯链可达的目标
    trace_hint_nodes, trace_hint_edges = [], []
    _seen_th_node, _seen_th_edge = set(), set()
    for t, ps in trace_by_target.items():
        if str(t) in {str(x["id"]) for x in impact_nodes} or str(t) in _seen_th_node:
            continue  # 同时被合理路径命中 → 以合理影响为准
        _seen_th_node.add(str(t))
        ent = ent_of.get(str(t)) or {}
        trace_hint_nodes.append({"id": t, "name": ent.get("name", t),
                                 "type": ent.get("entity_type") or "",
                                 "depth": min(len(p["node_ids"]) - 1 for p in ps),
                                 "reason": "仅通过追溯/引用类关系可达——不构成必然影响，需人工确认"})
    for p in all_paths:
        if p.get("plausibility") != "trace":
            continue
        ids = p["node_ids"]
        for i in range(len(ids) - 1):
            key = (str(ids[i]), str(ids[i + 1]))
            if key in _seen_th_edge:
                continue
            _seen_th_edge.add(key)
            r = next((rr for rr in edges if str(rr.get("source_id")) == key[0]
                      and str(rr.get("target_id")) == key[1]), None)
            trace_hint_edges.append({"from": key[0], "to": key[1],
                                     "type": (r or {}).get("relation_type", "TRACE")})

    # 影响边：路径中实际使用过的边（去重，保留最高权重语义）
    used_edges, seen_e = [], set()
    for p in all_paths:
        for i in range(len(p["node_ids"]) - 1):
            key = (p["node_ids"][i], p["node_ids"][i + 1])
            if key in seen_e:
                continue
            seen_e.add(key)
            r = next((rr for rr in edges if str(rr.get("source_id")) == key[0] and str(rr.get("target_id")) == key[1]), None)
            if r:
                w = edge_weight(r.get("relation_type"), change_type)
                used_edges.append({"from": key[0], "to": key[1], "type": r.get("relation_type"),
                                   "impact": "direct" if p["node_ids"][i] == sid else "indirect",
                                   "score": round(w, 3)})
    impact_edges = used_edges

    # 统计字段（v1 兼容口径，score 换组合风险）
    direct_count = sum(1 for n in impact_nodes if n["impact"] == "direct")
    indirect_count = sum(1 for n in impact_nodes if n["impact"] == "indirect")
    affected = [n for n in impact_nodes if n["impact"] != "source"]
    impact_levels = {"high": sum(1 for n in affected if n["level"] == "high"),
                     "mid": sum(1 for n in affected if n["level"] == "mid"),
                     "low": sum(1 for n in affected if n["level"] == "low")}
    depth_stats = {}
    for n in affected:
        d = depth_stats.setdefault(n["depth"], {"direct": 0, "indirect": 0, "nodes": []})
        d["direct" if n["impact"] == "direct" else "indirect"] += 1
        d["nodes"].append(n["name"])
    type_stats, rel_type_stats, domain_stats = {}, {}, {}
    for n in affected:
        type_stats[n["type"]] = type_stats.get(n["type"], 0) + 1
        dom = DOMAIN_MAP.get(n["type"], n["type"] or "其他")
        domain_stats.setdefault(dom, {"count": 0, "nodes": []})
        domain_stats[dom]["count"] += 1
        domain_stats[dom]["nodes"].append(n["name"])
    for e in impact_edges:
        rel_type_stats[e["type"]] = rel_type_stats.get(e["type"], 0) + 1

    # 元素关键度画像（Eckert 三分类）：out_w vs in_w（全图，非仅影响子图）
    out_w, in_w = {}, {}
    for e in edges:
        w = _rw(e.get("relation_type"))
        out_w[str(e.get("source_id"))] = out_w.get(str(e.get("source_id")), 0.0) + w
        in_w[str(e.get("target_id"))] = in_w.get(str(e.get("target_id")), 0.0) + w
    element_profile = {}
    for n in nodes:
        nid = str(n.get("id"))
        o, i = out_w.get(nid, 0.0), in_w.get(nid, 0.0)
        if o + i <= 0:
            continue
        if o > i * 1.25:
            profile = "multiplier"
        elif i > o * 1.25:
            profile = "absorber"
        else:
            profile = "carrier"
        element_profile[nid] = {"name": n.get("name", nid), "out_w": round(o, 2),
                                "in_w": round(i, 2), "profile": profile}

    top_risk = sorted(affected, key=lambda n: -(n.get("score") or 0))[:5]
    coverage = round(len(affected) / max(len(nodes), 1) * 100, 1)
    risks = []
    if top_risk:
        risks.append({
            "level": "high" if top_risk[0]["level"] == "high" else "mid",
            "title": f"组合风险最高：{top_risk[0]['name']}（P={top_risk[0]['score']}）",
            "desc": (f"变更沿 {len(rel_type_stats)} 类关系传播 {max_depth} 层，共影响 {len(affected)} 个元素"
                     f"（占图谱 {coverage}%）；直接 {direct_count} / 间接 {indirect_count}，高影响 {impact_levels['high']} 个。"),
            "advice": f"优先复核 {top_risk[0]['name']} 的传播路径（合理传播 {len([p for p in all_paths if p['node_ids'][-1]==top_risk[0]['id'] and p.get('plausibility')!='trace'])} 条），评估是否触发级联变更。",
        })

    path_list = sorted(all_paths, key=lambda p: -p["likelihood"])[:50]
    return {
        "ok": True,
        "change": {"type": change_type, "desc": change_desc or "",
                   "type_label": CHANGE_TYPE_LABELS.get(change_type, change_type)},
        "change_source": source,
        "impact_nodes": impact_nodes,
        "impact_edges": impact_edges,
        "direct_count": direct_count,
        "indirect_count": indirect_count,
        "impact_levels": impact_levels,
        "depth_stats": depth_stats,
        "type_stats": type_stats,
        "rel_type_stats": rel_type_stats,
        "domain_stats": domain_stats,
        "risk_analysis": {"risks": risks, "top_risk": top_risk, "coverage": coverage, "max_depth": max_depth},
        "risk_matrix": risk_matrix,
        "path_list": path_list,
        "path_count": path_counter["n"],
        "element_profile": element_profile,
        "trace_hint_nodes": trace_hint_nodes,
        "trace_hint_edges": trace_hint_edges,
        "depth": depth,
        "direction": direction,
        "params": {"depth": depth, "direction": direction, "change_type": change_type,
                   "change_desc": change_desc, "relation_types": [], "reference_sources": [],
                   "use_vector": False},
    }


def _node_name(nodes: list, nid: str) -> str:
    for n in nodes:
        if str(n.get("id")) == nid:
            return n.get("name", nid)
    return nid


def scan_name_references(conn, old_name: str, limit: int = 50) -> dict:
    """改名专用：引用扫描（不跑影响传播）——名字是标识的显示层，结构依赖零传播。

    四路扫描：① 需求/元素 properties 与描述文本 ② 资料库 chunks ③ 词典词条 ④ 代码产物。
    返回 {requirements:[{id,name,where,snippet}], docs:[{source_doc,snippet}],
          glossary:[{term}], code:[{title,snippet}]}，供引用更新清单渲染。"""
    out = {"requirements": [], "docs": [], "glossary": [], "code": []}
    if not (old_name or "").strip():
        return out
    like = f"%{old_name}%"
    try:
        # ① 元素 properties/描述文本（需求正文等）
        for r in conn.execute(
                "SELECT id, name, entity_type, substr(properties,1,200) AS props FROM entities "
                "WHERE status!='deprecated' AND (properties LIKE ? OR name LIKE ?) AND name != ? LIMIT ?",
                (like, like, old_name, limit)).fetchall():
            props = r["props"] or ""
            pos = props.find(old_name)
            out["requirements"].append({
                "id": r["id"], "name": r["name"], "entity_type": r["entity_type"],
                "where": "properties 文本", "snippet": props[max(0, pos - 30):pos + len(old_name) + 40] if pos >= 0 else props[:80]})
        # ② 资料库 chunks
        for r in conn.execute(
                "SELECT source_doc, substr(content,1,200) AS content FROM document_chunks "
                "WHERE content LIKE ? LIMIT ?", (like, limit)).fetchall():
            ct = r["content"] or ""
            pos = ct.find(old_name)
            out["docs"].append({"source_doc": r["source_doc"],
                                "snippet": ct[max(0, pos - 30):pos + len(old_name) + 40] if pos >= 0 else ct[:80]})
        # ③ 词典词条
        try:
            for r in conn.execute(
                    "SELECT term, definition FROM glossary WHERE term LIKE ? OR definition LIKE ? LIMIT ?",
                    (like, like, limit)).fetchall():
                out["glossary"].append({"term": r["term"]})
        except Exception:
            pass  # glossary 表结构差异容错
        # ④ 代码产物
        for r in conn.execute(
                "SELECT title, substr(preview_content,1,200) AS content FROM artifacts "
                "WHERE kind='code' AND preview_content LIKE ? LIMIT ?", (like, limit)).fetchall():
            ct = r["content"] or ""
            pos = ct.find(old_name)
            out["code"].append({"title": r["title"],
                                "snippet": ct[max(0, pos - 30):pos + len(old_name) + 40] if pos >= 0 else ct[:80]})
    except Exception:
        pass
    return out


def decide_recommendation(card: dict) -> dict:
    """决策建议规则引擎（可解释，阈值不搞黑盒）：返回 {recommendation, conditions[], reasons[]}。"""
    lv = card.get("impact_levels") or {}
    ra = card.get("risk_analysis") or {}
    coverage = ra.get("coverage") or 0
    change_type = (card.get("change") or {}).get("type", "attribute")
    reasons = []
    # rename：引用更新清单
    refs = card.get("references") or {}
    ref_count = sum(len(v) for v in refs.values()) if isinstance(refs, dict) else 0
    if change_type == "rename":
        if ref_count:
            return {"recommendation": "有条件批准",
                    "conditions": [f"同步更新 {ref_count} 处文本引用（见引用更新清单）"],
                    "reasons": ["改名不改变结构依赖（结构传播为零）", f"发现 {ref_count} 处文本引用旧名"]}
        return {"recommendation": "批准",
                "conditions": [], "reasons": ["改名不改变结构依赖，且未发现文本引用残留"]}
    # 非 rename：结构影响规则
    if lv.get("high", 0) == 0 and coverage < 30:
        reasons.append(f"无高影响元素，覆盖率仅 {coverage}%")
        return {"recommendation": "批准", "conditions": [], "reasons": reasons}
    multi_hit = any((card.get("element_profile") or {}).get(n.get("id"), {}).get("profile") == "multiplier"
                    for n in (card.get("impact_nodes") or []) if n.get("impact") != "source")
    if coverage >= 60 or multi_hit:
        if multi_hit:
            reasons.append("影响链路中存在 Multiplier（变更放大器）元素")
        if coverage >= 60:
            reasons.append(f"影响覆盖率 {coverage}%，波及范围大")
        return {"recommendation": "有条件批准",
                "conditions": ["先在沙箱中预演调整方案", "高风险元素逐项复核后再提交 CCB"], "reasons": reasons}
    if change_type == "delete" and any(n.get("type") in ("需求", "系统需求", "子系统需求", "单元需求")
                                       for n in (card.get("impact_nodes") or [])):
        reasons.append("删除类变更触达需求层")
        return {"recommendation": "细化后再议", "conditions": ["补充需求影响处置方案（变更/放弃/替代）"],
                "reasons": reasons}
    reasons.append(f"影响 {card.get('direct_count', 0) + card.get('indirect_count', 0)} 个元素，覆盖率 {coverage}%")
    return {"recommendation": "提交 CCB 评审", "conditions": [], "reasons": reasons}


# ══════════════ 报告 2.0 P3：重测清单 + 工作量估算表（Wiegers 面向三）══════════════

VERIFY_ENT_TYPES = {"验证活动", "验证用例", "试验", "测试", "V&V", "验证"}
VERIFY_REL_TYPES = {"VERIFIED_BY", "验证"}
# 工作量估算表 8 行（Wiegers 任务清单 MBSE 裁剪版；数值人工编辑，不做伪精确自动估算）
EFFORT_ROWS = [
    ("model",       "模型修改",     {"组件", "分系统", "功能", "模块", "设备", "载荷", "子系统", "系统"}),
    ("requirement", "需求文本更新", {"需求", "约束", "系统需求", "子系统需求", "单元需求"}),
    ("interface",   "接口调整",     {"接口", "端口", "信号", "数据链"}),
    ("retest",      "重测执行",     None),   # count = 重测清单条数
    ("doc",         "文档同步",     {"文档"}),
    ("review",      "评审",         None),   # count = 高影响元素数
    ("release",     "发布",         None),
    ("other",       "其他",         None),
]


def build_retest_plan(nodes: list, edges: list, card: dict) -> dict:
    """重测清单（受影响验证活动）+ 工作量估算表预填，挂到 card["retest_plan"]/["effort_estimate"]。

    清单来源（两级）：
    ① 受影响元素中 entity_type ∈ VERIFY_ENT_TYPES → 直接受影响，必测；
    ② 受影响的需求类元素沿 VERIFIED_BY/验证 反查其验证活动（全图边，不只影响子图）。
    工作量表：按受影响类型分布预填「涉及数量」列，人日列留空人工编辑（R1：结构化清单，非伪精确）。
    """
    affected = [n for n in (card.get("impact_nodes") or []) if n.get("impact") != "source"]
    aff_by_id = {str(n.get("id")): n for n in affected}
    ent_of = {str(n.get("id")): n for n in nodes}
    items, seen = [], set()

    def _add(vid, reason, via_name=""):
        ent = ent_of.get(str(vid)) or {}
        key = str(vid)
        if key in seen or not ent:
            return
        seen.add(key)
        aff = aff_by_id.get(key) or {}
        items.append({"id": key, "name": ent.get("name", vid), "type": ent.get("entity_type") or "",
                      "reason": reason, "via": via_name,
                      "score": aff.get("score"), "level": aff.get("level") or "mid"})

    # ② 先反查：受影响的需求类元素沿验证关系挂接的验证活动（via 映射，供 ① 合并原因）
    via_map = {}
    for e in edges:
        rt = e.get("relation_type") or ""
        if rt not in VERIFY_REL_TYPES:
            continue
        s, t = str(e.get("source_id")), str(e.get("target_id"))
        req, ver = (s, t) if s in aff_by_id else ((t, s) if t in aff_by_id else (None, None))
        if req and (ent_of.get(ver, {}).get("entity_type") or "") in VERIFY_ENT_TYPES:
            via_map.setdefault(ver, []).append(aff_by_id[req].get("name", req))
    # ① 直接受影响的验证活动（合并 via 信息，原因不丢失；via 去重保序——多路径重复命中同一需求）
    for n in affected:
        if (n.get("type") or "") in VERIFY_ENT_TYPES:
            vias = list(dict.fromkeys(via_map.pop(str(n["id"]), [])))
            _add(n["id"], "直接受影响" + ("；同时为受影响需求的验证活动" if vias else ""),
                 "、".join(vias))
    # ② 其余仅经反查命中的验证活动
    for vid, vias in via_map.items():
        _add(vid, "受影响需求的验证活动", "、".join(dict.fromkeys(vias)))
    items.sort(key=lambda x: (0 if x["reason"].startswith("直接") else 1, -(x.get("score") or 0)))

    # 工作量估算表：类型分布预填涉及数量（无自动人日）
    type_stats = card.get("type_stats") or {}
    counts = {}
    for t, c in type_stats.items():
        for row, _, types in [(r[0], r[1], r[2]) for r in EFFORT_ROWS]:
            if types and t in types:
                counts[row] = counts.get(row, 0) + c
    counts["retest"] = len(items)
    counts["review"] = (card.get("impact_levels") or {}).get("high", 0)
    effort = [{"key": k, "label": lbl, "count": counts.get(k, 0), "effort": "", "note": ""}
              for k, lbl, _ in EFFORT_ROWS]
    card["retest_plan"] = {"items": items, "total": len(items)}
    card["effort_estimate"] = effort
    return card
