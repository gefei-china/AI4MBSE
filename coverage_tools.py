# -*- coding: utf-8 -*-
"""覆盖性分析工具组（SRS-GN-CO，2026-09-20）：确定性、只读、可复现。

设计原则（用户拍板的分层架构）：
- 计算**写死**：覆盖率/矩阵/断链是可审计数字（SRS 业务规则：必须记录分析范围、模型版本、规则版本），
  同一份数据两次运行结果一致，评审可对拍 —— 不允许 LLM 即兴算数。
- 灵活性在 Agent 层：本模块只提供薄原语（4 个工具），编排、解读、补全建议由 Agent + Skill 完成。

数据口径（规则版本 RULE_VERSION = "cov-v1.0"，随结果返回以便追溯）：
- 需求实体：entity_type LIKE '%需求%'（真实库实证：需求/系统需求/利益相关方需求/单元需求）
- 架构元素：其余 reviewed 实体（系统/分系统/部件/功能/接口等，类型名自由，不做枚举假设）
- 追溯关系（relations 表，status='reviewed'）：
    SATISFIES / ALLOCATED_TO / VERIFIED_BY = 直接覆盖
    DERIVES = 派生覆盖（需求→需求，标记但不计直接覆盖）
    CONTAINS/FLOW_TO/CONNECTS/COMMANDS/DEPENDS_ON/OBSERVES = 结构关系，不参与覆盖判定
- 维度纪律：branch × project_id 二维切（多项目库一维切会掩盖跨项目混数据）
- 只读：side_effect=read，全部查询走传入连接，不写任何表

工具清单：
- coverage_matrix    需求×架构覆盖矩阵 + 覆盖率 + 三类异常（SRS-GN-CO-XQJG）
- trace_chain_check  需求→架构→验证 端到端追溯链连通性（SRS-GN-CO-JKJH）
- scene_coverage     场景/用例实体链路覆盖检查（SRS-GN-CO-CJGK）
- gap_summary        缺项分类 + 统计 + 风险分级（SRS-GN-CO-QXFG）
"""
import json
import sqlite3

RULE_VERSION = "cov-v1.0"
DIRECT_COVER = ("SATISFIES", "ALLOCATED_TO", "VERIFIED_BY")
DERIVE_COVER = ("DERIVES",)
TRACE_REL_TYPES = DIRECT_COVER + DERIVE_COVER

COVERAGE_TOOL_NAMES = (
    "coverage_matrix",
    "trace_chain_check",
    "scene_coverage",
    "gap_summary",
)


def _detect_project_col(conn) -> str | None:
    """entities 表是否含 project_id 列（旧库可能没有；缺失则该维度退化为不过滤）。"""
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(entities)").fetchall()]
        return "project_id" if "project_id" in cols else None
    except Exception:
        return None


def _load_graph(conn, branch: str | None, project_id: str | None) -> dict:
    """加载 reviewed 实体 + 追溯关系（只读）。返回 {entities, relations, meta}。"""
    pcol = _detect_project_col(conn)
    ents_sql = ("SELECT id, name, entity_type, status, branch"
                + (", project_id" if pcol else "") + " FROM entities WHERE status='reviewed'")
    args: list = []
    if branch:
        ents_sql += " AND branch=?"
        args.append(branch)
    if pcol and project_id:
        ents_sql += " AND project_id=?"
        args.append(project_id)
    entities = [dict(r) for r in conn.execute(ents_sql, args).fetchall()]
    ent_ids = {e["id"] for e in entities}

    rel_sql = ("SELECT id, source_id, target_id, relation_type, branch"
               + (", project_id" if pcol else "") + " FROM relations WHERE status='reviewed'")
    rargs: list = []
    if branch:
        rel_sql += " AND branch=?"
        rargs.append(branch)
    if pcol and project_id:
        rel_sql += " AND project_id=?"
        rargs.append(project_id)
    relations = [dict(r) for r in conn.execute(rel_sql, rargs).fetchall()]
    # 关系端点必须落在实体集内（跨维度关系不进分析，避免把其他项目的边算进来）
    relations = [r for r in relations
                 if r["relation_type"] in TRACE_REL_TYPES
                 and r["source_id"] in ent_ids and r["target_id"] in ent_ids]

    return {
        "entities": entities,
        "relations": relations,
        "meta": {
            "branch": branch or "all",
            "project_id": project_id or "all",
            "rule_version": RULE_VERSION,
            "entity_count": len(entities),
            "trace_relation_count": len(relations),
            "project_column": bool(pcol),
        },
    }


def _split(graph: dict) -> tuple[list, list]:
    """实体二分：需求侧（entity_type 含'需求'）/ 架构侧（其余）。"""
    reqs = [e for e in graph["entities"] if "需求" in (e["entity_type"] or "")]
    arch = [e for e in graph["entities"] if "需求" not in (e["entity_type"] or "")]
    return reqs, arch


def _requirement_id_set(reqs: list) -> set:
    return {e["id"] for e in reqs}


def _adjacency(relations: list) -> dict:
    """无向邻接表：id -> [(neighbor_id, rel_type, rel_id)]。"""
    adj: dict = {}
    for r in relations:
        adj.setdefault(r["source_id"], []).append((r["target_id"], r["relation_type"], r["id"]))
        adj.setdefault(r["target_id"], []).append((r["source_id"], r["relation_type"], r["id"]))
    return adj


def _coverage_cells(graph: dict, reqs: list, arch: list) -> dict:
    """覆盖单元计算：需求 e × 架构元素 a，来源关系类型 + 关系 id（可追溯）。"""
    arch_ids = {e["id"] for e in arch}
    req_ids = _requirement_id_set(reqs)
    cells: dict = {}
    for r in graph["relations"]:
        s, t, rt = r["source_id"], r["target_id"], r["relation_type"]
        pair = None
        if s in req_ids and t in arch_ids:
            pair = (s, t, rt)
        elif t in req_ids and s in arch_ids:
            pair = (t, s, rt)
        if pair:
            key = (pair[0], pair[1])
            cells.setdefault(key, []).append({"rel_type": pair[2], "rel_id": r["id"]})
    return cells


def _tool_frame(graph: dict, tool: str) -> dict:
    """统一返回框架：分析范围 + 规则版本（SRS 业务规则：可复现、可追溯）。"""
    return {
        "tool": tool,
        "scope": graph["meta"],
        "generated_by": "coverage_tools.py (deterministic, read-only)",
    }


# ────────────────────────── 工具 1：需求架构覆盖矩阵（XQJG） ──────────────────────────

def _coverage_matrix(conn, arguments: dict) -> dict:
    graph = _load_graph(conn, arguments.get("branch"), arguments.get("project_id"))
    reqs, arch = _split(graph)
    cells = _coverage_cells(graph, reqs, arch)

    covered = sorted({k[0] for k in cells})
    req_ids = _requirement_id_set(reqs)
    arch_ids = {e["id"] for e in arch}

    # 异常一：未覆盖需求（行全空）
    uncovered_reqs = [
        {"id": e["id"], "name": e["name"], "entity_type": e["entity_type"], "branch": e["branch"]}
        for e in reqs if e["id"] not in covered]

    # 异常二：无需求依据的架构元素（列全空，且不参与任何追溯关系）
    touched = {k[1] for k in cells} | {
        n for r in graph["relations"] for n in (r["source_id"], r["target_id"])
        if r["source_id"] in req_ids or r["target_id"] in req_ids
        for n in ([r["target_id"]] if r["source_id"] in req_ids else [r["source_id"]])}
    baseless_arch = [
        {"id": e["id"], "name": e["name"], "entity_type": e["entity_type"], "branch": e["branch"]}
        for e in arch if e["id"] not in touched]

    # 异常三：异常追溯关系（端点悬空 = 断链；自环）
    ent_ids = {e["id"] for e in graph["entities"]}
    all_reviewed = [dict(r) for r in conn.execute(
        "SELECT id, source_id, target_id, relation_type, branch"
        + (", project_id" if graph["meta"]["project_column"] else "")
        + " FROM relations WHERE status='reviewed'").fetchall()]
    if graph["meta"]["branch"] != "all":
        all_reviewed = [r for r in all_reviewed if r["branch"] == graph["meta"]["branch"]]
    abnormal = []
    for r in all_reviewed:
        if r["relation_type"] not in TRACE_REL_TYPES:
            continue
        if r["source_id"] == r["target_id"]:
            abnormal.append({"rel_id": r["id"], "kind": "self_loop", "detail": f"{r['source_id']} -> {r['target_id']}"})
        elif r["source_id"] not in ent_ids or r["target_id"] not in ent_ids:
            abnormal.append({"rel_id": r["id"], "kind": "dangling",
                             "detail": f"{r['source_id']} -> {r['target_id']}（端点不在 reviewed 实体集）"})

    matrix = []
    for e in reqs:
        row_cells = {k[1]: v for k, v in cells.items() if k[0] == e["id"]}
        matrix.append({
            "req": {"id": e["id"], "name": e["name"], "type": e["entity_type"]},
            "covered_by": [
                {"arch_id": a, "rel": v[0]["rel_type"], "rel_id": v[0]["rel_id"],
                 "arch_name": next((x["name"] for x in arch if x["id"] == a), a)}
                for a, v in sorted(row_cells.items())],
        })

    total_req = len(reqs)
    covered_cnt = len([e for e in reqs if e["id"] in covered])
    derive_only = len([e for e in reqs
                       if e["id"] in covered
                       and all(v["rel_type"] in DERIVE_COVER
                               for k, vs in cells.items() if k[0] == e["id"] for v in vs)])
    out = _tool_frame(graph, "coverage_matrix")
    out.update({
        "ok": True,
        "summary": {
            "requirement_total": total_req,
            "arch_element_total": len(arch),
            "covered_requirements": covered_cnt,
            "coverage_rate": round(covered_cnt / total_req, 4) if total_req else None,
            "derive_only_requirements": derive_only,
        },
        "matrix": matrix,
        "uncovered_requirements": uncovered_reqs,
        "baseless_arch_elements": baseless_arch,
        "abnormal_relations": abnormal,
        "arch_type_distribution": _type_dist(arch),
    })
    return out


def _type_dist(items: list) -> list:
    d: dict = {}
    for e in items:
        d[e["entity_type"]] = d.get(e["entity_type"], 0) + 1
    return sorted([{"entity_type": k, "count": v} for k, v in d.items()], key=lambda x: -x["count"])


# ────────────────────── 工具 2：端到端追溯链连通性（JKJH） ──────────────────────

def _trace_chain_check(conn, arguments: dict) -> dict:
    graph = _load_graph(conn, arguments.get("branch"), arguments.get("project_id"))
    reqs, arch = _split(graph)
    req_ids = _requirement_id_set(reqs)
    ent_by_id = {e["id"]: e for e in graph["entities"]}
    adj = _adjacency(graph["relations"])

    want_req = (arguments.get("req_name") or "").strip()
    chains = []
    for e in reqs:
        if want_req and want_req not in e["name"] and want_req != e["id"]:
            continue
        # BFS：需求 → 架构（SATISFIES/ALLOCATED_TO）→ 验证（VERIFIED_BY）
        seen = {e["id"]}
        frontier = [(e["id"], 0, None)]
        reached_arch, reached_verify, path_hops = [], [], 0
        while frontier:
            nid, hops, via = frontier.pop(0)
            path_hops = max(path_hops, hops)
            for (nb, rt, rid) in adj.get(nid, []):
                if nb in seen:
                    continue
                seen.add(nb)
                if rt == "VERIFIED_BY":
                    reached_verify.append({"id": nb, "name": ent_by_id.get(nb, {}).get("name", nb), "via_rel": rid})
                if nb not in req_ids:
                    reached_arch.append({"id": nb, "name": ent_by_id.get(nb, {}).get("name", nb),
                                         "type": ent_by_id.get(nb, {}).get("entity_type", "?"), "via_rel": rt})
                frontier.append((nb, hops + 1, rid))
        chains.append({
            "req": {"id": e["id"], "name": e["name"], "type": e["entity_type"]},
            "reachable_arch_count": len(reached_arch),
            "reached_verification": bool(reached_verify),
            "max_hops": path_hops,
            "sample_targets": reached_arch[:5],
            "verify_targets": reached_verify[:5],
        })

    verified = len([c for c in chains if c["reached_verification"]])
    out = _tool_frame(graph, "trace_chain_check")
    out.update({
        "ok": True,
        "summary": {
            "requirement_total": len(chains),
            "with_arch_link": len([c for c in chains if c["reachable_arch_count"] > 0]),
            "with_verification": verified,
            "verification_rate": round(verified / len(chains), 4) if chains else None,
        },
        "chains": chains,
    })
    return out


# ────────────────────── 工具 3：场景链路与工况覆盖（CJGK） ──────────────────────

def _scene_coverage(conn, arguments: dict) -> dict:
    graph = _load_graph(conn, arguments.get("branch"), arguments.get("project_id"))
    ent_by_id = {e["id"]: e for e in graph["entities"]}
    all_rel_sql = ("SELECT id, source_id, target_id, relation_type FROM relations WHERE status='reviewed'"
                   + (" AND branch=?" if graph["meta"]["branch"] != "all" else ""))
    args = [graph["meta"]["branch"]] if graph["meta"]["branch"] != "all" else []
    all_rels = [dict(r) for r in conn.execute(all_rel_sql, args).fetchall()]
    ent_ids = {e["id"] for e in graph["entities"]}
    # 结构关系也纳入场景链检查（CONTAINS/FLOW_TO 等决定链完整性）
    struct_rels = [r for r in all_rels
                   if r["source_id"] in ent_ids and r["target_id"] in ent_ids]
    adj = _adjacency(struct_rels)

    scenes = [e for e in graph["entities"]
              if any(k in (e["entity_type"] or "") for k in ("场景", "用例", "模式", "工况"))]
    report = []
    for sc in scenes:
        seen = {sc["id"]}
        frontier = [sc["id"]]
        linked = []
        while frontier:
            nid = frontier.pop(0)
            for (nb, rt, _rid) in adj.get(nid, []):
                if nb in seen:
                    continue
                seen.add(nb)
                linked.append({"id": nb, "name": ent_by_id.get(nb, {}).get("name", nb),
                               "type": ent_by_id.get(nb, {}).get("entity_type", "?"), "rel": rt})
                frontier.append(nb)
        acts = [x for x in linked if any(k in x["type"] for k in ("活动", "行为", "状态", "功能"))]
        parts = [x for x in linked if any(k in x["type"] for k in ("系统", "分系统", "部件", "组件"))]
        report.append({
            "scene": {"id": sc["id"], "name": sc["name"], "type": sc["entity_type"]},
            "linked_total": len(linked),
            "activities_states": acts[:10],
            "involved_parts": parts[:10],
            "has_activity_chain": bool(acts),
            "has_participant": bool(parts),
        })

    out = _tool_frame(graph, "scene_coverage")
    out.update({
        "ok": True,
        "summary": {
            "scene_total": len(report),
            "with_activity_chain": len([r for r in report if r["has_activity_chain"]]),
            "with_participant": len([r for r in report if r["has_participant"]]),
            "orphan_scenes": [r["scene"]["name"] for r in report if r["linked_total"] == 0],
        },
        "scenes": report,
    })
    return out


# ────────────────────── 工具 4：缺项汇总与风险分级（QXFG） ──────────────────────

def _gap_summary(conn, arguments: dict) -> dict:
    m = _coverage_matrix(conn, arguments)
    s = _scene_coverage(conn, arguments)
    t = _trace_chain_check(conn, arguments)

    gaps = []
    for r in m.get("uncovered_requirements", []):
        gaps.append({"category": "未覆盖需求", "risk": "high" if "系统需求" in r["entity_type"] else "medium",
                     "id": r["id"], "name": r["name"], "detail": f"类型 {r['entity_type']}，无任何追溯关系",
                     "locate": f"实体 {r['id']}"})
    for r in m.get("baseless_arch_elements", []):
        gaps.append({"category": "无需求依据的架构元素", "risk": "low",
                     "id": r["id"], "name": r["name"], "detail": f"类型 {r['entity_type']}，未关联任何需求",
                     "locate": f"实体 {r['id']}"})
    for r in m.get("abnormal_relations", []):
        gaps.append({"category": "异常追溯关系", "risk": "high",
                     "id": r["rel_id"], "name": f"关系 #{r['rel_id']}", "detail": r["detail"],
                     "locate": f"关系 {r['rel_id']}"})
    for sc in s.get("scenes", []):
        if not sc["has_activity_chain"] and sc["linked_total"] > 0:
            gaps.append({"category": "场景链路断点", "risk": "medium",
                         "id": sc["scene"]["id"], "name": sc["scene"]["name"],
                         "detail": "场景已定义但无活动/状态/功能链",
                         "locate": f"实体 {sc['scene']['id']}"})
        if sc["linked_total"] == 0:
            gaps.append({"category": "孤立场景", "risk": "medium",
                         "id": sc["scene"]["id"], "name": sc["scene"]["name"],
                         "detail": "场景与任何元素均无关系",
                         "locate": f"实体 {sc['scene']['id']}"})

    order = {"high": 0, "medium": 1, "low": 2}
    gaps.sort(key=lambda g: order.get(g["risk"], 9))
    stat: dict = {}
    for g in gaps:
        stat[g["category"]] = stat.get(g["category"], 0) + 1

    out = dict(m["scope"])
    out["tool"] = "gap_summary"
    out.update({
        "ok": True,
        "summary": {
            "gap_total": len(gaps),
            "by_category": stat,
            "coverage_rate": m["summary"]["coverage_rate"],
            "verification_rate": t["summary"]["verification_rate"],
        },
        "gaps": gaps,
        "note": "风险分级规则（cov-v1.0）：high=异常追溯/未覆盖系统需求；medium=未覆盖其他需求/场景断点；low=无需求依据的架构元素。补全建议请结合上下文由 Agent 生成。",
    })
    return out


# ────────────────────────── 入口分发 ──────────────────────────

_HANDLERS = {
    "coverage_matrix": _coverage_matrix,
    "trace_chain_check": _trace_chain_check,
    "scene_coverage": _scene_coverage,
    "gap_summary": _gap_summary,
}


def _resolve_scope(conn, arguments: dict) -> tuple[str | None, str | None]:
    """解析分析范围：显式参数 > settings 默认工程/分支。

    2026-09-20（用户拍板口径）：覆盖性分析的对象是**当前建模工程的数据**，
    不是图谱全库。多工程混布分支上一维切会算出混算数字（实测 dev 分支混了
    巡飞演示 56 + 星网宽带 54），故未显式指定且无默认工程时**拒绝分析**——
    宁可不给数字，不给混算数字。
    返回 (project_id, branch)；project_id 无法解析时抛 ValueError。
    """
    project_id = (arguments.get("project_id") or "").strip() or None
    branch = (arguments.get("branch") or "").strip() or None
    try:
        row = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('default_project_id','default_branch')"
        ).fetchall()
        s = {r["key"]: (r["value"] or "").strip() for r in row}
    except Exception:
        s = {}
    if not project_id:
        project_id = s.get("default_project_id") or None
    if not branch:
        branch = s.get("default_branch") or None
    if not project_id:
        raise ValueError(
            "未设置当前工程（settings.default_project_id 为空），覆盖性分析拒绝全库混算。"
            "请先在界面切换/设置默认工程，或在调用参数中显式指定 project_id。"
        )
    if "project_id" not in [r["name"] for r in conn.execute("PRAGMA table_info(entities)")]:
        raise ValueError("库结构缺 project_id 列，无法按工程隔离分析（旧库请先迁移）。")
    return project_id, branch


def exec_tool(name: str, arguments: dict) -> dict:
    """标准入口（pipeline 前缀路由调用）。只读短连接；结果 JSON 字符串化。"""
    handler = _HANDLERS.get(name)
    if not handler:
        return {"ok": False, "result": f"未知覆盖性工具：{name}（可用：{', '.join(COVERAGE_TOOL_NAMES)}）"}
    try:
        from database import get_db
        conn = get_db()
        try:
            arguments = dict(arguments or {})
            arguments["project_id"], arguments["branch"] = _resolve_scope(conn, arguments)
            out = handler(conn, arguments)
        finally:
            conn.close()
        return {"ok": True, "result": json.dumps(out, ensure_ascii=False)}
    except ValueError as e:
        return {"ok": False, "result": str(e)}
    except Exception as e:
        import traceback
        return {"ok": False, "result": f"覆盖性分析异常: {e} | {traceback.format_exc()[-300:]}"}
