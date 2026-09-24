# -*- coding: utf-8 -*-
"""知识库路由分片：FTS/统计/生命周期/覆盖度/总览。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.knowledge_parts.shared import *


@router.get("/api/knowledge/fts")
def knowledge_fts(q: str, kinds: str = "", limit: int = 50,
                  conn=Depends(db_session), user=Depends(current_user)):
    """P1-5 全文检索（FTS5 trigram）：跨实体/关系/三元组的子串匹配 + LIKE 兜底。

    kinds 可选逗号分隔子集（如 'entities,relations'）；mode=fts|like（兜底标记）。
    """
    from fts_search import fts_query
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()] or None
    return fts_query(conn, q, kinds=kind_list, limit=max(1, min(limit, 200)))


@router.get("/api/knowledge/stats")
def knowledge_stats(branch: str = "", conn=Depends(db_session)):
    repo = KnowledgeRepo(conn)
    stats = {}
    for s in ["reviewed", "candidate", "raw_chunk", "deprecated"]:
        stats[s] = repo.count_by_status(s, branch)
    stats["total_relations"] = repo.count_relations(branch)
    stats["total_docs"] = repo.count_documents(branch)
    return stats


@router.get("/api/knowledge/lifecycle")
def knowledge_lifecycle(conn=Depends(db_session)):
    """P0-4 生命周期分布：原始切片 → 候选 → 审校 → 入库 → 发布 → 废弃（FR-KG-8）。

    - published：release 分支且 published_at 非空 = 已进入权威基线（被 AI 消费）
    - reviewed：已评审但未发布（dev 分支等）
    - candidate / raw_chunk / deprecated：按状态行数
    - 附带按来源（source_type）与按知识类别（P0-3）的分布，供总览页聚合
    """
    def _cnt(where, params=()):
        return conn.execute(f"SELECT COUNT(*) FROM entities WHERE {where}", params).fetchone()[0]

    lifecycle = {
        "raw_chunk": _cnt("status='raw_chunk'"),
        "candidate": _cnt("status='candidate'"),
        "reviewed": _cnt("status='reviewed' AND NOT (branch='release' AND published_at != '')"),
        "published": _cnt("branch='release' AND status='reviewed' AND published_at != ''"),
        "deprecated": _cnt("status='deprecated'"),
    }
    # 按来源分布（排除 deprecated）
    src_rows = conn.execute(
        "SELECT COALESCE(NULLIF(source_type,''),'manual') AS src, COUNT(*) AS n FROM entities "
        "WHERE status!='deprecated' GROUP BY src ORDER BY n DESC").fetchall()
    # 按知识类别分布（reviewed 去重）
    cat_rows = conn.execute(
        "SELECT COALESCE(NULLIF(knowledge_category,''),'未分类') AS cat, COUNT(DISTINCT id) AS n "
        "FROM entities WHERE status='reviewed' GROUP BY cat ORDER BY n DESC").fetchall()
    return {"lifecycle": lifecycle, "total": sum(lifecycle.values()),
            "by_source": [dict(r) for r in src_rows],
            "by_category": [dict(r) for r in cat_rows],
            "published_records": conn.execute(
                "SELECT COUNT(*) FROM knowledge_publish_logs").fetchone()[0]}


@router.get("/api/knowledge/coverage")
def knowledge_coverage(conn=Depends(db_session)):
    """FR-KG-13 补 G6：知识完整度三指标，指导增量抽取优先级。

    - type_coverage：本体实体类型实例覆盖率——covered=该类型在非废弃实体中有实例；
      empty_types=无 reviewed 实例的类型清单（建议优先抽取）；coverage_rate=covered/total
    - chunk_linked：文档 chunk 未链接实体比例（linked_entity_ids 非空且非 '[]' 视为已链接）
    - fallback_topics：近 30 天检索 fallback（vector/mixed/no_hit）query 词频 TOP20
    """
    # 1. 本体实体类型实例覆盖率
    types = conn.execute(
        "SELECT name, type_kind FROM ontology_types WHERE type_kind='entity' ORDER BY name").fetchall()
    total_types = len(types)
    covered = {r["entity_type"] for r in conn.execute(
        "SELECT DISTINCT entity_type FROM entities WHERE status!='deprecated'")} if total_types else set()
    covered_types = sum(1 for t in types if t["name"] in covered)
    reviewed = {r["entity_type"] for r in conn.execute(
        "SELECT DISTINCT entity_type FROM entities WHERE status='reviewed'")} if total_types else set()
    empty_types = [{"name": t["name"], "type_kind": t["type_kind"]}
                   for t in types if t["name"] not in reviewed]
    coverage_rate = round(covered_types / total_types, 4) if total_types else 0

    # 2. 文档 chunk 链接率（linked_entity_ids 非空且非 '[]' 视为已链接）
    total_chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
    linked_chunks = conn.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE linked_entity_ids IS NOT NULL "
        "AND TRIM(linked_entity_ids) != '' AND linked_entity_ids != '[]'").fetchone()[0]
    unlinked_ratio = round((total_chunks - linked_chunks) / total_chunks, 4) if total_chunks else 0

    # 3. 检索 fallback 主题词频（近 30 天）
    return {
        "type_coverage": {"total_types": total_types, "covered_types": covered_types,
                          "coverage_rate": coverage_rate, "empty_types": empty_types},
        "chunk_linked": {"total_chunks": total_chunks, "linked_chunks": linked_chunks,
                         "unlinked_ratio": unlinked_ratio},
        "fallback_topics": _fallback_topics(conn),
    }


@router.get("/api/knowledge/overview")
def knowledge_overview(conn=Depends(db_session)):
    """FR-KG-8/11 补 G5：知识库总览一次聚合（kb-a 首屏）。

    - kpi：实体/关系/文档总数 + 待评审数（v2g pending 候选 + 实体 candidate + 关系 candidate）
    - review_queue：三类待评审队列（各前 5 条；v2g 用 list_candidates，实体/关系原生 SQL）
    - source_dist：实体来源分布（前 8 项）
    - lifecycle：复用 knowledge_lifecycle 口径（避免两处统计漂移）
    - graph_thumb：图谱缩略（reviewed 实体按逻辑 id 去重 top 60 + reviewed 关系数）
    """
    from vector2graph import list_candidates
    repo = KnowledgeRepo(conn)

    # 1. KPI
    ent_cand_total = repo.scalar("SELECT COUNT(*) FROM entities WHERE status='candidate'")
    rel_cand_total = repo.scalar("SELECT COUNT(*) FROM relations WHERE status='candidate'")
    v2g_total = repo.scalar("SELECT COUNT(*) FROM v2g_candidates WHERE status='pending'")
    kpi = {
        "entities": repo.scalar("SELECT COUNT(*) FROM entities WHERE status!='deprecated'"),
        "relations": repo.scalar("SELECT COUNT(*) FROM relations WHERE status!='deprecated'"),
        "documents": repo.count_documents(),
        "pending_review": v2g_total + ent_cand_total + rel_cand_total,
    }

    # 2. 待评审队列（三类各前 5 条；v2g 复用 list_candidates 后过滤 pending，口径与"待评审"语义一致）
    v2g_all, _ = list_candidates(conn, None, 50, 0)
    v2g_items = [{"id": c["id"], "name": c.get("entity_name") or "",
                  "entity_type": c.get("entity_type") or ""}
                 for c in v2g_all if c.get("status") == "pending"][:5]
    ent_items = [dict(r) for r in conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE status='candidate' "
        "ORDER BY created_at DESC LIMIT 5").fetchall()]
    rel_rows = [dict(r) for r in conn.execute(
        "SELECT id, relation_type, source_id, target_id FROM relations WHERE status='candidate' "
        "ORDER BY id DESC LIMIT 5").fetchall()]
    ids = set()
    for r in rel_rows:
        ids.add(r["source_id"])
        ids.add(r["target_id"])
    names = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for e in conn.execute(f"SELECT id, name FROM entities WHERE id IN ({ph})", list(ids)):
            names[e["id"]] = e["name"]
    for r in rel_rows:
        r["source_name"] = names.get(r["source_id"], "")
        r["target_name"] = names.get(r["target_id"], "")
    review_queue = {
        "v2g": {"total": v2g_total, "items": v2g_items},
        "entities": {"total": ent_cand_total, "items": ent_items},
        "relations": {"total": rel_cand_total, "items": rel_rows},
    }

    # 3. 来源分布（前 8 项，空 source_type 归为 manual）
    #    注意 GROUP BY 用表达式而非别名：表恰有 source_type 列，GROUP BY source_type 会被解析为原始列，
    #    导致 '' 与 'manual' 分成两组显示两个 manual
    source_dist = [dict(r) for r in conn.execute(
        "SELECT COALESCE(NULLIF(source_type,''),'manual') AS source_type, COUNT(*) AS n FROM entities "
        "WHERE status!='deprecated' GROUP BY COALESCE(NULLIF(source_type,''),'manual') "
        "ORDER BY n DESC LIMIT 8").fetchall()]

    # 4. 生命周期（复用 knowledge_lifecycle 逻辑）
    lifecycle = knowledge_lifecycle(conn=conn)

    # 5. 图谱缩略（reviewed 实体按逻辑 id 去重，release 版本优先）
    thumb = [dict(r) for r in conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE status='reviewed' "
        "GROUP BY id ORDER BY (branch='release') DESC, created_at DESC LIMIT 60").fetchall()]
    graph_thumb = {"entities": thumb,
                   "relations": repo.scalar("SELECT COUNT(*) FROM relations WHERE status='reviewed'")}

    return {"kpi": kpi, "review_queue": review_queue, "source_dist": source_dist,
            "lifecycle": lifecycle, "graph_thumb": graph_thumb}
