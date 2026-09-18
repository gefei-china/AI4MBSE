# -*- coding: utf-8 -*-
"""知识库路由分片：图谱边更新/合并/视图/路径/搜索/导出。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分，勿手工编辑。"""
from routers.knowledge_parts.shared import *


@router.put("/api/knowledge/graph/edges/{eid}")
def graph_update_edge(eid: int, body: dict, conn=Depends(db_session),
                      user=Depends(require_any_permission(WRITE_PERMS))):
    """修正关系：更新关系类型 / 属性（候选 → 修正留痕，重新进入审核流）。"""
    branch = body.get("branch") or "dev"
    g = _release_guard(conn, branch)
    if g:
        return JSONResponse({"error": g}, 400)
    rt = (body.get("relation_type") or "").strip()
    if not rt:
        return JSONResponse({"error": "relation_type required"}, 400)
    # P1-6（2026-09-07）：修正关系同样过本体校验——①关系名 canonical 化（防废弃
    # 中文名经修正路径回流，与 sysml_importer/create_relation 同口径）；②domain/range
    # 类型约束（validate_edge）。修正后重新进入审核流（status='candidate'）。
    from core.relmap import canonical
    _c = canonical(rt)
    if _c is None:
        return JSONResponse({"error": f"关系类型已废弃且无 canonical 映射: {rt}"}, 400)
    rt = _c
    row = conn.execute(
        "SELECT r.source_id, r.target_id, s.entity_type stype, t.entity_type ttype "
        "FROM relations r LEFT JOIN entities s ON s.id=r.source_id AND s.branch=? "
        "LEFT JOIN entities t ON t.id=r.target_id AND t.branch=? WHERE r.id=?",
        (branch, branch, eid)).fetchone()
    if not row:
        return JSONResponse({"error": "edge not found"}, 404)
    from ontology_semantics import GraphStore
    errs = GraphStore(conn).validator.validate_edge(row["stype"], rt, row["ttype"])
    if errs:
        return JSONResponse({"error": "; ".join(errs)}, 400)
    conn.execute("UPDATE relations SET relation_type=?, properties=?, status='candidate', reviewed_by='', reviewed_at='' WHERE id=?",
                 (rt, json.dumps(body.get("properties") or {}, ensure_ascii=False), eid))
    _log_graph_edit(conn, "edge_update", str(eid), {"relation_type": rt}, _actor(user))
    audit(_actor(user), "relation_update", f"修正关系 #{eid}: {rt}", conn=conn)
    # 分支版本管理：图谱边手动保存提交打点（kind=manual，失败仅打印不阻断）
    try:
        row = conn.execute("SELECT source_id, target_id FROM relations WHERE id=?", (eid,)).fetchone()
        if row:
            CommitRepo(conn).create_commit(
                branch, "manual", f"图谱边更新：{row['source_id']}--{rt}--{row['target_id']}",
                {"relations": [eid]},
                {"id": eid, "source_id": row["source_id"], "target_id": row["target_id"],
                 "relation_type": rt})
    except Exception as e:
        print(f"[commit_repo] manual 提交打点失败（不阻断业务）: {e}")
    return {"ok": True}


@router.post("/api/knowledge/entities/{entity_id}/merge")
def merge_entity(entity_id: str, body: dict, conn=Depends(db_session),
                 user=Depends(require_any_permission(WRITE_PERMS))):
    """实体合并（审核工作流）：候选重复实体合并到目标实体——
    按全局逻辑 id 操作（对齐 review_entity 语义，兼容 release/dev 版本行）：
    关联边重定向 → 自环/重复边去重 → 删除源实体全部版本行。"""
    target_id = (body.get("target_id") or "").strip()
    branch = body.get("branch") or "dev"
    g = _release_guard(conn, branch)
    if g:
        return JSONResponse({"error": g}, 400)
    if not target_id:
        return JSONResponse({"error": "target_id 不能为空"}, 400)
    if target_id == entity_id:
        return JSONResponse({"error": "不能合并到自身"}, 400)
    if not conn.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone():
        return JSONResponse({"error": "源实体不存在"}, 404)
    if not conn.execute("SELECT id FROM entities WHERE id=?", (target_id,)).fetchone():
        return JSONResponse({"error": "目标实体不存在"}, 404)
    # P2（2026-09-07）entity_aliases 写入侧：合并后源实体的本名/别名都归到目标实体，
    # mention 原文在检索时仍可召回 canonical（别名可重放，不随源实体删除而丢失）。
    _src_row = conn.execute("SELECT name, branch, source_doc FROM entities WHERE id=? LIMIT 1",
                            (entity_id,)).fetchone()
    _tgt_row = conn.execute("SELECT name FROM entities WHERE id=? LIMIT 1",
                            (target_id,)).fetchone()
    # 关联边重定向（全部分支版本）
    conn.execute("UPDATE relations SET source_id=? WHERE source_id=?", (target_id, entity_id))
    conn.execute("UPDATE relations SET target_id=? WHERE target_id=?", (target_id, entity_id))
    # 去重：自环 + 同一 (源,目标,关系类型) 每分支只保留最小 id
    conn.execute("DELETE FROM relations WHERE source_id=target_id")
    conn.execute("""DELETE FROM relations WHERE id NOT IN
                    (SELECT MIN(id) FROM relations GROUP BY branch, source_id, target_id, relation_type)""")
    # 删除源实体全部版本行
    conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))
    try:
        from entity_resolver import write_entity_alias as _wea
        if _src_row and _tgt_row and (_src_row["name"] or "").strip() != (_tgt_row["name"] or "").strip():
            _wea(conn, target_id, _src_row["name"],
                 branch=_src_row["branch"] or branch,
                 source_doc=_src_row["source_doc"] or "",
                 source_type="entity_merge", created_by=_actor(user))
        conn.execute("UPDATE entity_aliases SET entity_id=? WHERE entity_id=?", (target_id, entity_id))
    except Exception as _ae:
        print(f"[entity_merge] 写 entity_aliases 跳过（不阻断合并）: {_ae}")
    _log_graph_edit(conn, "entity_merge", entity_id, {"target_id": target_id}, _actor(user))
    audit(_actor(user), "entity_merge", f"实体合并: {entity_id} → {target_id}@{branch}", conn=conn)
    return {"ok": True}


@router.get("/api/knowledge/graph/views")
def graph_views_list(branch: Optional[str] = None, conn=Depends(db_session)):
    b = branch or "dev"
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, branch, config, builtin, created_by, created_at FROM graph_views "
        "WHERE branch=? OR builtin=1 ORDER BY id DESC", (b,)).fetchall()]
    for r in rows:
        try:
            r["config"] = json.loads(r["config"] or "{}")
        except Exception:
            r["config"] = {}
    return rows


@router.post("/api/knowledge/graph/views")
def graph_views_create(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """保存命名子图：当前筛选（类型/状态/布局/来源）组合存为可复用视图。"""
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, 400)
    branch = body.get("branch") or "dev"
    config = body.get("config") or {}
    conn.execute("INSERT INTO graph_views (name, branch, config, created_by) VALUES (?,?,?,?)",
                 (name, branch, json.dumps(config, ensure_ascii=False), _actor(user)))
    audit(_actor(user), "graph_view_save", f"保存子图: {name}@{branch}", conn=conn)
    return {"ok": True}


@router.delete("/api/knowledge/graph/views/{vid}")
def graph_views_delete(vid: int, conn=Depends(db_session), user=Depends(current_user)):
    row = conn.execute("SELECT builtin FROM graph_views WHERE id=?", (vid,)).fetchone()
    if not row:
        return JSONResponse({"error": "view not found"}, 404)
    if row["builtin"]:
        return JSONResponse({"error": "内置子图不可删除"}, 400)
    conn.execute("DELETE FROM graph_views WHERE id=?", (vid,))
    audit(_actor(user), "graph_view_del", f"删除子图 #{vid}", conn=conn)
    return {"ok": True}


@router.get("/api/knowledge/graph/views/modules")
def graph_view_modules(branch: Optional[str] = None, status: Optional[str] = None,
                       conn=Depends(db_session)):
    """模块/来源文档子图分组 + 计数（后端单一事实源，前端消费此接口渲染下拉）。

    计数基于指定状态视图的实体（与前端当前状态筛选一致，数据量变化即实时反映）。
    """
    from subgraph_views import module_groups, doc_groups
    b = branch or "dev"
    repo = KnowledgeRepo(conn)
    ents = repo.graph_entities(b, None, status or "all")
    onts = repo.list_ontology_types()
    return {
        "modules": module_groups(onts, [e["entity_type"] for e in ents]),
        "docs": doc_groups(ents),
    }


@router.get("/api/knowledge/graph/export")
def graph_export(branch: Optional[str] = None, fmt: str = "json", conn=Depends(db_session)):
    """KB-P2：图谱导出（json / graphml 互操作）。"""
    b = branch or "dev"
    repo = KnowledgeRepo(conn)
    ents = repo.graph_entities(b)
    rels = repo.graph_relations(b)
    if fmt == "graphml":
        import xml.etree.ElementTree as ET
        root = ET.Element("graphml", xmlns="http://graphml.graphdrawing.org/xmlns")
        g = ET.SubElement(root, "graph", edgedefault="directed")
        for e in ents:
            n = ET.SubElement(g, "node", id=e["id"])
            d = ET.SubElement(n, "data", key="name")
            d.text = e["name"]
        for r in rels:
            ed = ET.SubElement(g, "edge",
                               source=str(r["source_id"]), target=str(r["target_id"]))
            d = ET.SubElement(ed, "data", key="type")
            d.text = r["relation_type"]
        xml = ET.tostring(root, encoding="unicode")
        from fastapi.responses import Response
        return Response(xml, media_type="application/xml")
    return {"entities": ents, "relations": rels, "count": len(ents) + len(rels)}


@router.get("/api/knowledge/graph/path")
def graph_path(from_id: str, to_id: str, conn=Depends(db_session)):
    """O-4：BFS 最短路径追踪（有向图，按边方向）。返回 {path:[{id,name,entity_type}], hops}。"""
    nodes = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, entity_type FROM entities WHERE status!='deprecated' "
        "ORDER BY (branch='release') ASC, created_at").fetchall()}
    adj = {}
    for r in conn.execute("SELECT source_id, target_id FROM relations WHERE status!='deprecated'").fetchall():
        adj.setdefault(r["source_id"], []).append(r["target_id"])
    if from_id not in nodes or to_id not in nodes:
        return JSONResponse({"error": "节点不存在"}, 404)
    # BFS
    from collections import deque
    q = deque([from_id])
    prev = {from_id: None}
    found = False
    while q and not found:
        cur = q.popleft()
        for nxt in adj.get(cur, []):
            if nxt not in prev:
                prev[nxt] = cur
                if nxt == to_id:
                    found = True
                    break
                q.append(nxt)
    if not found:
        return {"path": [], "hops": -1, "reachable": False}
    path_ids = []
    cur = to_id
    while cur is not None:
        path_ids.append(cur)
        cur = prev[cur]
    path_ids.reverse()
    return {"path": [nodes[i] for i in path_ids], "hops": len(path_ids) - 1, "reachable": True}


@router.get("/api/knowledge/graph/neighbors")
def graph_neighbors(node: str, depth: int = 1, conn=Depends(db_session)):
    """O-4：邻域展开（交互式搜索）——返回该节点 depth 层内子图。"""
    depth = max(1, min(depth, 4))
    nodes = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, entity_type, status, graph_x, graph_y FROM entities WHERE status!='deprecated' "
        "ORDER BY (branch='release') ASC, created_at").fetchall()}
    rels = conn.execute("SELECT * FROM relations WHERE status!='deprecated'").fetchall()
    adj = {}
    edges = []
    for r in rels:
        d = dict(r)
        edges.append(d)
        adj.setdefault(d["source_id"], []).append(d["target_id"])
        adj.setdefault(d["target_id"], []).append(d["source_id"])
    if node not in nodes:
        return JSONResponse({"error": "节点不存在"}, 404)
    visited = {node}
    frontier = [node]
    for _ in range(depth):
        nxt = []
        for f in frontier:
            for nb in adj.get(f, []):
                if nb not in visited and nb in nodes:
                    visited.add(nb)
                    nxt.append(nb)
        frontier = nxt
    sub_edges = [e for e in edges if e["source_id"] in visited and e["target_id"] in visited]
    return {"center": node, "depth": depth,
            "nodes": [nodes[i] for i in visited],
            "edges": sub_edges, "node_count": len(visited)}


@router.get("/api/knowledge/graph/search")
def graph_search(q: str, conn=Depends(db_session)):
    """O-4：搜索高亮——按名称/类型匹配节点（供前端高亮显示）。

    P1-5（2026-09-06）：优先 FTS5 trigram 全文（含 properties 内容命中），
    实体 id 集回退原 LIKE 逻辑，返回结构不变（前端零改动）。
    """
    q = q.strip()
    if not q:
        return {"nodes": [], "edges": []}
    ids = set()
    try:
        from fts_search import fts_query
        fr = fts_query(conn, q, kinds=["entities"], limit=100)
        ids = {r["ref_id"] for r in fr["results"] if r.get("entity")
               and r["entity"]["status"] != "deprecated"}
    except Exception:  # noqa: BLE001  索引异常 → 走原 LIKE 路径
        ids = set()
    if ids:
        ph = ",".join(["?"] * len(ids))
        rows = conn.execute(
            f"SELECT id, name, entity_type, status, graph_x, graph_y FROM entities "
            f"WHERE id IN ({ph}) AND status!='deprecated' "
            f"ORDER BY (branch='release') ASC, created_at LIMIT 50",
            list(ids)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, entity_type, status, graph_x, graph_y FROM entities "
            "WHERE (name LIKE ? OR entity_type LIKE ?) AND status!='deprecated' "
            "ORDER BY (branch='release') ASC, created_at LIMIT 50",
            (f"%{q}%", f"%{q}%")).fetchall()
    seen = {}
    for r in rows:
        seen.setdefault(r["id"], dict(r))
    rows = list(seen.values())
    ids = {r["id"] for r in rows}
    sub_edges = []
    if ids:
        ph = ",".join(["?"] * len(ids))
        sub_edges = [dict(r) for r in conn.execute(
            f"SELECT * FROM relations WHERE status!='deprecated' AND source_id IN ({ph}) AND target_id IN ({ph})",
            list(ids) + list(ids)).fetchall()]
    return {"nodes": [dict(r) for r in rows], "edges": sub_edges, "match_count": len(rows)}
