# -*- coding: utf-8 -*-
"""知识库路由分片：图谱节点与边 CRUD。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.knowledge_parts.shared import *

# ── 安全迁移改类（2026-09-12）：可迁移目标 = 当前类型祖先 ∪ 各祖先的子树（同父类型族） ──
_COMPOSED_PARENT_RELS = ("COMPOSED_OF", "CONTAINS")

def _type_migratable_types(conn, name: str) -> set:
    """返回某实体类型允许迁移到的类型集合。固定构建参照 root 树：
    祖先链（含自身）的每个节点取整棵子树 → 等价「祖先或同父类型族」。"""
    rows = conn.execute(
        "SELECT name, id, parent_id FROM ontology_types WHERE type_kind='entity'"
    ).fetchall()
    rec = {r["id"]: {"name": r["name"], "parent_id": r["parent_id"]} for r in rows}
    by_name = {r["name"]: r for r in rows}
    if name not in by_name:
        return {name}
    # 祖先链（含自身）
    anc, cur_id, seen = [], by_name[name]["id"], set()
    while cur_id is not None and cur_id not in seen:
        seen.add(cur_id)
        r = rec.get(cur_id)
        if not r:
            break
        anc.append(r["name"])
        cur_id = r["parent_id"]
    # 子映射
    children = {}
    for r in rows:
        pid = r["parent_id"]
        if pid is not None:
            children.setdefault(pid, []).append(r["name"])
    sub_pool = set()
    def _walk(nm):
        sub_pool.add(nm)
        rid = by_name[nm]["id"]
        for c in children.get(rid, []):
            _walk(c)
    # 祖先 ∪ 直接父的整棵子树（同父类型族）；根类型（无父）=自身族
    pid = by_name[name]["parent_id"]
    _walk(rec[pid]["name"] if pid in rec else name)
    return set(anc).union(sub_pool)


def _has_composed_child(conn, branch: str, node_id: str) -> bool:
    """该节点作为组合父（COMPOSED_OF/CONTAINS 的 source）是否存在子实例。"""
    ph = ",".join("?" for _ in _COMPOSED_PARENT_RELS)
    return conn.execute(
        f"SELECT 1 FROM relations WHERE branch=? AND source_id=? AND relation_type IN ({ph}) LIMIT 1",
        (branch, node_id, *_COMPOSED_PARENT_RELS),
    ).fetchone() is not None



@router.get("/api/knowledge/graph")
def knowledge_graph(branch: Optional[str] = None, status: Optional[str] = None,
                    project_id: Optional[str] = None, conn=Depends(db_session)):
    """图谱数据：按分支 + 状态加载（status=reviewed|candidate|all）。

    大图保护：默认只返回已评审（少量干净数据）；status=all 时 LIMIT 200 兜底，
    避免上万节点一次性渲染拖垮前端。
    """
    b = branch or "dev"
    repo = KnowledgeRepo(conn)
    return {
        "entities": repo.graph_entities(b, project_id, status or "reviewed"),
        "relations": repo.graph_relations(b, project_id),
        "status": status or "reviewed",
        "total_entities": repo.count_graph_entities(b, project_id, status or "reviewed"),
    }


@router.get("/api/knowledge/graph/data-properties")
def graph_data_properties(type: Optional[str] = None, conn=Depends(db_session)):
    """返回某实体类型可用的本体数据属性（type_kind='attribute' 且 domain_classes 包含自身或父类）。

    用途：图谱右侧「实例详情」属性编辑器按 Schema 渲染受控 input，避免自由 key-value 污染。

    入参：type=实体类型名（如 Satellite）；不传则返回所有本体属性。
    返回：[{name, type, required, allowed_values, domain_classes, description}]。
    """
    rows = conn.execute(
        "SELECT name, properties, constraints, description FROM ontology_types WHERE type_kind='attribute'"
    ).fetchall()
    result = [_dprop_proj(r) for r in rows]
    if not type:
        return result
    # 收集目标类型及其父类链（subClassOf 通过 parent_id，递归向上）
    chain, cur, seen = [], type, set()
    while cur and cur not in seen:
        seen.add(cur)
        r = conn.execute("SELECT id, parent_id FROM ontology_types WHERE name=? AND type_kind='entity'", (cur,)).fetchone()
        if not r:
            break
        chain.append(cur)
        cur_row = conn.execute("SELECT name FROM ontology_types WHERE id=?", (r["parent_id"],)).fetchone()
        cur = cur_row["name"] if cur_row else None
    chain_set = set(chain)
    out = [d for d in result if any(x in chain_set for x in d["domain_classes"])]
    # 2026-11-15：**合并实体侧的 required**。
    #   `_dprop_proj` 只读属性侧 constraints（全局声明），而"本类必填"写在实体侧
    #   `constraints.required`（如 利益相关方需求: [req_id, priority]）⇒ 不合并的话
    #   前端实例编辑器不显示必填星号/缺项 ⚠（26-grapheditor.js、19-governance.js
    #   都按 `p.required` 渲染），用户填完保存却被 400 拦住 —— 前后端口径不一致。
    #   这里刻意**不吞异常**：字典查不到类型也不会抛（classify 回退为自身），
    #   真出错就该让它响，而不是静默回到"没有必填"。
    from ontology_semantics import OntologyValidator
    _req = set(OntologyValidator(conn)._merged_constraints(type).get("required") or [])
    if _req:
        for d in out:
            if d["name"] in _req:
                d["required"] = True
    return out


@router.post("/api/knowledge/graph/nodes")
def graph_add_node(body: GraphNodeIn, conn=Depends(db_session),
                   user=Depends(require_any_permission(WRITE_PERMS))):
    import uuid as _uuid
    nid = body.id or f"N-{_uuid.uuid4().hex[:8]}"
    g = _release_guard(conn, body.branch or "dev")
    if g:
        return JSONResponse({"error": g}, 400)
    # 审计带属性摘要（2026-09-14 历史页维度补全：新增节点时一并记录带了哪些属性）
    _props_txt = ""
    try:
        _pv = [(k, str(v)) for k, v in (body.properties or {}).items() if str(v).strip()][:6]
        if _pv:
            _props_txt = "属性: " + "、".join(f"{k}={v}" for k, v in _pv)
    except Exception:
        pass
    # ★ 2026-11-21 P0-0：统一写入口（门禁→校验→写入→留痕→执行后核验）。
    #   ⚠️ status="reviewed" 必须显式传：图库内手动创建 = 工程师显式确认行为，
    #   与 API 直写（`POST /entities` 默认 candidate 进审核队列）语义不同，
    #   原实现由 `GraphStore.create_node` 内部写死，迁移时不能丢。
    from core.graph_write_gate import write_entity
    res = write_entity(conn, op="create", entity_id=nid, name=body.name,
                       entity_type=body.entity_type, properties=body.properties,
                       branch=body.branch or "dev", actor=_actor(user),
                       knowledge_category=body.knowledge_category or "",
                       x=body.x, y=body.y, status="reviewed",
                       log_op="node_add", audit_event="graph_node_add",
                       extra_detail=_props_txt)
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error"), "stage": res.get("stage"),
                             "post": res.get("post")},
                            404 if res.get("stage") == "not_found"
                            else (500 if res.get("applied") else 400))
    return {"ok": True, "id": nid, "post": res.get("post")}


@router.put("/api/knowledge/graph/nodes/{nid}")
def graph_update_node(nid: str, body: GraphNodeIn, conn=Depends(db_session),
                      user=Depends(require_any_permission(WRITE_PERMS))):
    """更新节点（版本化 fork 语义）：更新指定分支版本行；
    该分支无版本时自动 fork 现有版本再应用新值（支撑 dev 修改已发布实体）。"""
    repo = KnowledgeRepo(conn)
    branch = body.branch or "dev"
    g = _release_guard(conn, branch)
    if g:
        return JSONResponse({"error": g}, 400)
    if not repo.get_entity(nid):
        return JSONResponse({"error": "node not found"}, 404)
    # 需求2-安全迁移（2026-09-12）：变更类型严格受限——仅允许迁移到祖先/同父类型族，
    # 且已有组合子级（COMPOSED_OF/CONTAINS 父角色）时禁止改类，防止破坏父子层级与关系语义
    old = repo.get_entity(nid, branch)
    old_type = (old or {}).get("entity_type") if old else None
    if old_type and body.entity_type != old_type:
        if _has_composed_child(conn, branch, nid):
            return JSONResponse({"error": f"改类失败：实例 {nid} 已有组合子实例，需先移除子树或改挂后再变更类型"}, 400)
        mig = _type_migratable_types(conn, old_type)
        if body.entity_type not in mig:
            return JSONResponse({"error": f"改类仅允许迁移到祖先或同父类型族：{old_type} →（可迁移: {', '.join(sorted(mig))}）"}, 400)
        # P1-6（2026-09-07）：编辑保存同样新建一样过本体校验——此前 PUT 绕过
    # validate_node，可把受控属性改成白名单外的值/删必填/写非法键（约束漏洞）。
    # ★ 2026-11-21 P0-0：校验与写入交给**统一写入口**（本体校验 + 属性校验 + 写入
    #   + 留痕 + 执行后核验同处一处）。这里只保留**本端点特有的业务规则**（改类迁移约束）。
    #   改前此处自己调 validate_node + repo.update_entity + _log_graph_edit + audit 四件事，
    #   与 /entities 两条路径各自维护，必然漂移（实测：/entities 只查类型不校验属性）。
    _s = lambda v: ('∅' if v is None else str(v))
    _gchg = []
    try:
        _prv = (old or {}).get("properties") or {}
        if isinstance(_prv, str):
            try: _prv = json.loads(_prv)
            except Exception: _prv = {}
        _cur = dict(body.properties or {})
        for _k in sorted(set(list(_prv.keys()) + list(_cur.keys()))):
            if _prv.get(_k) != _cur.get(_k):
                _gchg.append("属性." + _k + ":" + _s(_prv.get(_k)) + "→" + _s(_cur.get(_k)))
        _gname = (old or {}).get("name")
        if _gname is not None and str(_gname) != str(body.name):
            _gchg.insert(0, "名称:" + _s(_gname) + "→" + _s(body.name))
        if old_type and old_type != body.entity_type:
            _gchg.insert(0, "类型:" + _s(old_type) + "→" + _s(body.entity_type))
    except Exception:
        _gchg = []
    _gdiff = "；".join(_gchg[:20])
    from core.graph_write_gate import write_entity
    res = write_entity(conn, op="update", entity_id=nid, name=body.name,
                       entity_type=body.entity_type, properties=body.properties,
                       branch=branch, actor=_actor(user), x=body.x, y=body.y,
                       log_op="node_update", audit_event="graph_node_update",
                       extra_detail=_gdiff)
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error"), "stage": res.get("stage"),
                             "post": res.get("post")},
                            404 if res.get("stage") == "not_found"
                            else (500 if res.get("applied") else 400))
    conn.execute("UPDATE entities SET graph_x=?, graph_y=? WHERE id=? AND branch=?",
                 (body.x, body.y, nid, branch))
    # 分支版本管理：图谱节点手动保存提交打点（kind=manual，失败仅打印不阻断）
    try:
        CommitRepo(conn).create_commit(
            branch, "manual", f"图谱节点更新：{body.name}",
            {"entities": [nid]}, {"id": nid, "name": body.name})
    except Exception as e:
        print(f"[commit_repo] manual 提交打点失败（不阻断业务）: {e}")
    return {"ok": True, "forked": res.get("forked", False), "post": res.get("post")}


@router.delete("/api/knowledge/graph/nodes/{nid}")
def graph_delete_node(nid: str, branch: Optional[str] = "dev", conn=Depends(db_session),
                      user=Depends(require_any_permission(WRITE_PERMS))):
    """删除节点（版本化）：仅删除指定分支版本行 + 同分支关联边；其他分支版本保留。"""
    b = branch or "dev"
    g = _release_guard(conn, b)
    if g:
        return JSONResponse({"error": g}, 400)
    # ★ 2026-11-21 P0-0：统一写入口。删除的**执行后核验是反向断言**（行必须消失），
    #   不能复用"回读到且一致"的语义 —— 否则删除失败也会报成功。
    from core.graph_write_gate import write_entity
    res = write_entity(conn, op="delete", entity_id=nid, branch=b,
                       actor=_actor(user), log_op="node_delete",
                       audit_event="graph_node_delete")
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error"), "stage": res.get("stage"),
                             "post": res.get("post")},
                            404 if res.get("stage") == "not_found"
                            else (500 if res.get("applied") else 400))
    return {"ok": True, "post": res.get("post")}


@router.post("/api/knowledge/graph/edges")
def graph_add_edge(body: GraphEdgeIn, conn=Depends(db_session),
                   user=Depends(require_any_permission(WRITE_PERMS))):
    g = _release_guard(conn, body.branch or "dev")
    if g:
        return JSONResponse({"error": g}, 400)
    # ★ 2026-11-21 P0-0：统一写入口（关系类型门禁与实体同一把尺子 + 执行后核验）。
    from core.graph_write_gate import write_edge
    res = write_edge(conn, op="edge_create", source_id=body.source_id,
                     target_id=body.target_id, relation_type=body.relation_type,
                     props=body.props, branch=body.branch or "dev", actor=_actor(user))
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error"), "stage": res.get("stage"),
                             "post": res.get("post")},
                            404 if res.get("stage") == "not_found"
                            else (500 if res.get("applied") else 400))
    return {"ok": True, "id": res.get("id"), "post": res.get("post")}


@router.delete("/api/knowledge/graph/edges/{eid}")
def graph_delete_edge(eid: int, conn=Depends(db_session),
                      user=Depends(require_any_permission(WRITE_PERMS))):
    """删除边：release 分支只读（先查边分支再拦截），仅删除该边。"""
    row = conn.execute("SELECT branch FROM relations WHERE id=?", (eid,)).fetchone()
    if not row:
        return JSONResponse({"error": "edge not found"}, 404)
    g = _release_guard(conn, row["branch"] or "dev")
    if g:
        return JSONResponse({"error": g}, 400)
    # ★ 2026-11-21 P0-0：统一写入口（删除的核验是反向断言：行必须消失）。
    from core.graph_write_gate import write_edge
    res = write_edge(conn, op="edge_delete", edge_id=eid,
                     branch=row["branch"] or "dev", actor=_actor(user))
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error"), "stage": res.get("stage"),
                             "post": res.get("post")},
                            404 if res.get("stage") == "not_found"
                            else (500 if res.get("applied") else 400))
    return {"ok": True, "post": res.get("post")}


@router.get("/api/knowledge/graph/edges/review")
def graph_edges_review(status: Optional[str] = "candidate", limit: int = 100,
                       page: int = 0, branch: Optional[str] = None,
                       conn=Depends(db_session)):
    """P0-A：关系审核队列列表（按状态分页 + 端点名/端点状态；branch 非空时按分支隔离）。

    关系候选确认入库后 status=candidate（与实体同级治理），在此正式审核：
    confirm → reviewed（进图谱/发布），reject → deprecated（软删留痕）。
    """
    repo = KnowledgeRepo(conn)
    st = (status or "candidate").strip() or "candidate"
    offset = (page - 1) * limit if page > 0 else 0
    items = repo.list_relations_for_review(st, limit, offset, branch or "")
    total = repo.count_relations_by_status(st, branch or "")
    return {"items": items, "total": total, "page": page, "limit": limit, "status": st, "branch": branch or ""}


@router.post("/api/knowledge/graph/edges/{eid}/review")
def graph_review_edge(eid: int, body: dict, conn=Depends(db_session),
                      user=Depends(require_permission("kb_review", "confirm"))):
    """关系审核：confirm → reviewed（审核人/时间留痕）；reject → deprecated。"""
    action = body.get("action", "confirm")
    row = conn.execute("SELECT id, branch FROM relations WHERE id=?", (eid,)).fetchone()
    if not row:
        return JSONResponse({"error": "edge not found"}, 404)
    KnowledgeRepo(conn).review_relation(eid, action, operator=_actor(user))
    audit(_actor(user), "relation_review", f"审核关系 #{eid}: {action}", conn=conn, branch=row["branch"] or "dev")
    return {"ok": True, "action": action}


@router.post("/api/knowledge/graph/edges/merge")
def graph_merge_edge(body: dict, conn=Depends(db_session),
                     user=Depends(require_any_permission(WRITE_PERMS))):
    """P0-C 关系去重合并：dup 边 → keep 边（审核队列一键合并重复三元组）。

    属性/来源文档并入 keep，dup 置 deprecated 留痕；审计 + CommitRepo 打点。
    """
    from entity_resolver import merge_relation
    keep_id = str(body.get("keep_id") or "").strip()
    dup_id = str(body.get("dup_id") or "").strip()
    if not keep_id or not dup_id:
        return JSONResponse({"error": "keep_id/dup_id 不能为空"}, 400)
    try:
        result = merge_relation(conn, int(keep_id), int(dup_id), operator=_actor(user))
    except (ValueError, TypeError):
        return JSONResponse({"error": "keep_id/dup_id 必须为数字"}, 400)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "合并失败")}, 400)
    _mb = conn.execute("SELECT branch FROM relations WHERE id=?", (int(keep_id),)).fetchone()
    audit(_actor(user), "relation_merge", f"关系去重合并: #{dup_id} → #{keep_id}", conn=conn,
          branch=(_mb["branch"] if _mb else "") or "dev")
    return result
