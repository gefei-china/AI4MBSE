# -*- coding: utf-8 -*-
"""知识库路由分片：实体/标签/提交/分类/评审。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分，勿手工编辑。"""
from routers.knowledge_parts.shared import *


@router.get("/api/knowledge/entities")
def list_entities(status: Optional[str] = None, branch: Optional[str] = None,
                  search: Optional[str] = None, project_id: Optional[str] = None,
                  # P0-④（2026-09-11）时态参数：默认 is_current=1（行为不变）
                  as_of: Optional[str] = None, range_start: Optional[str] = None,
                  range_end: Optional[str] = None,
                  conn=Depends(db_session)):
    """知识库实体列表（兼容旧 API + 新时态 API）

    旧调用（不带 as_of/range_xx） → 返回 is_current=1 的实体（行为不变）
    新调用：
    - as_of=2026-01-01 → 时点查询
    - range_start=2026-01-01&range_end=2026-06-01 → 区间查询
    """
    return _temporal_list_entities(conn, status, branch, search, project_id,
                                   as_of, range_start, range_end)


def _temporal_list_entities(conn, status, branch, search, project_id,
                            as_of, range_start, range_end):
    """P0-④ 时态感知的实体列表（内部函数）"""
    from ontology_semantics import temporal_query_filter
    extra_where, extra_params = temporal_query_filter(as_of, range_start, range_end)
    repo = KnowledgeRepo(conn)
    if not extra_where:
        # 旧路径：行为完全不变
        return repo.list_entities(status, branch, search, project_id)
    # 新路径：拼时态过滤
    return repo.list_entities_temporal(status, branch, search, project_id,
                                       extra_where, extra_params,
                                       as_of=as_of, range_start=range_start, range_end=range_end)


def _entity_type_check(conn, etype: str):
    """KB API 直写路径的本体类型门禁（2026-09-14）。

    图谱页入口（POST /api/knowledge/graph/nodes）本就有 GraphStore.validate_node 校验，
    但 POST/PUT /api/knowledge/entities 直写入口此前不校验 entity_type——任意字符串
    都能落库上图（ENT-TEST 测试残留「载荷」即经此路径进图）。本函数与图谱入口同一把尺子：
    类型必须在本体声明（含子类/抽象类型口径，与 validate_node 第一分支一致）。
    返回 None=通过；str=错误文案。空类型放行（历史行为，仅拦「写了但未声明」）。
    """
    etype = (etype or "").strip()
    if not etype:
        return None
    from ontology_semantics import GraphStore
    v = GraphStore(conn).validator
    if etype not in v._load_types():
        known = v.entity_types()
        if known:
            return f"实体类型 '{etype}' 不在本体中（可用: {', '.join(known[:8])}）——请先在本体模型声明该类型"
        return "本体未配置实体类型，请先在本体模型声明"
    return None


@router.get("/api/knowledge/entities/{eid}/history")
def entity_history(eid: str, branch: Optional[str] = "dev",
                   conn=Depends(db_session)):
    """P0-④ 实体历史版本：返回该 (id, branch) 的全部时态版本（含已退役）。

    按 valid_from 倒序返回：最新在前。
    """
    return KnowledgeRepo(conn).list_entity_history(eid, branch)


@router.get("/api/knowledge/entities/{eid}/at")
def entity_as_of(eid: str, as_of: str, branch: Optional[str] = None,
                 conn=Depends(db_session)):
    """P0-④ 实体时点查询：as_of 时刻该实体是什么状态。

    用于「去年我们有多少设备」类时态查询。
    """
    repo = KnowledgeRepo(conn)
    ent = repo.get_entity_as_of(eid, as_of, branch)
    if not ent:
        return JSONResponse({"error": f"实体 {eid} 在 {as_of} 时不存在或已退役"}, 404)
    return ent


@router.get("/api/knowledge/tags")
def list_kb_tags(branch: Optional[str] = "", published_only: int = 0, conn=Depends(db_session)):
    """知识库文档标签列表（V2.3 会话窗口-设置知识库）：来自已接入文档，用于 @标签 输入提示。

    文档全局化：文件管理从分支体系抽离为全局资产，标签/文件面板不再按分支或
    published_only 过滤（参数保留接收兼容旧前端，忽略过滤）。
    """
    sql = "SELECT filename, file_type, parse_status, branch FROM documents"
    sql += " ORDER BY parse_status='completed' DESC, created_at DESC LIMIT 200"
    rows = conn.execute(sql).fetchall()
    return [
        {"name": r["filename"], "type": r["file_type"] or "", "status": r["parse_status"] or "pending",
         "branch": r["branch"] or ""}
        for r in rows
    ]


@router.get("/api/knowledge/entities/{eid}/branch-status")
def entity_branch_status(eid: str, conn=Depends(db_session), user=Depends(current_user)):
    """实体在各分支的流转状态（图谱详情「跨分支状态」提示，只读）。"""
    rows = conn.execute(
        "SELECT branch, status, reviewed_at FROM entities WHERE id=? AND status!='deprecated'",
        (eid,)).fetchall()
    return {"entity_id": eid,
            "branches": {r["branch"]: {"status": r["status"],
                                        "reviewed_at": r["reviewed_at"] or ''} for r in rows}}


@router.get("/api/knowledge/entities/{entity_id}")
def get_entity(entity_id: str, conn=Depends(db_session)):
    repo = KnowledgeRepo(conn)
    ent = repo.get_entity(entity_id)
    if not ent:
        return JSONResponse({"error": "Not found"}, 404)
    rels = repo.list_relations_of(entity_id)
    result = dict(ent)
    result["relations"] = rels
    # 分支版本管理 FR-KG-11：实体修改历史（changes 中含该实体的提交，id DESC）
    result["commit_history"] = CommitRepo(conn).get_entity_commit_history(entity_id)
    return result


@router.get("/api/knowledge/commits")
def knowledge_commits(branch: Optional[str] = "", kind: Optional[str] = "",
                      page: int = 0, limit: int = 15, conn=Depends(db_session)):
    """分支版本管理：提交记录列表（按 branch/kind 过滤，id DESC）。

    page>0 返回 {items,total,page,limit}（分页）；page=0 返回纯列表（兼容既有调用）。
    """
    return CommitRepo(conn).get_commits(branch or "", kind or "", page=page, size=limit)


@router.get("/api/knowledge/commits/{cid}")
def knowledge_commit_detail(cid: int, conn=Depends(db_session)):
    """分支版本管理：单条提交详情（changes/snapshot 解析 + 变更对象名称 enriched）。"""
    commit = CommitRepo(conn).get_commit(cid)
    if not commit:
        return JSONResponse({"error": "commit not found"}, 404)
    return commit


@router.get("/api/knowledge/publish-logs")
def knowledge_publish_logs(conn=Depends(db_session)):
    """分支版本管理 Task4：发布清单——按版本号（version_label）聚合发布记录，published_at DESC。

    每项 = 一次发布：{version_label, published_at(该次发布时间), entity_count, commit_id,
    entities:[{id,name}]（该版本发布的实体清单，取自该 version_label 的发布日志行）}。
    version_label 为空的存量历史日志（无版本概念）聚合为「历史发布」一并展示。
    """
    rows = conn.execute(
        "SELECT id, entity_id, name, version_label, published_at, commit_id "
        "FROM knowledge_publish_logs WHERE action='publish' "
        "ORDER BY published_at DESC, id DESC").fetchall()
    agg = {}
    order = []
    for r in rows:
        label = r["version_label"] or "历史发布"
        if label not in agg:
            agg[label] = {"version_label": label, "published_at": r["published_at"],
                          "entity_count": 0, "commit_id": r["commit_id"], "entities": []}
            order.append(label)
        item = agg[label]
        item["entity_count"] += 1
        item["entities"].append({"id": r["entity_id"], "name": r["name"] or ""})
        if item["commit_id"] is None and r["commit_id"] is not None:
            item["commit_id"] = r["commit_id"]
    return [agg[label] for label in order]


@router.post("/api/knowledge/commits/{cid}/revert")
def revert_commit(cid: int, body: dict = None, conn=Depends(db_session),
                  user=Depends(require_permission("branch_release", "review_merge"))):
    """分支版本管理 Task5：提交级回滚（仅 release 发布分支的提交）。

    body: {preview: boolean}（默认 false；true 只计算变更预览不执行）
    基于提交 changes + snapshot 反向操作（release 分支行）：
    - snapshot 有该 id 且当前已软删 → 恢复为提交时状态；
    - snapshot 无该 id（提交时新增/来自源分支）→ 置 deprecated（软删，留审计）。
    执行成功后生成 kind='rollback' 提交（head_commit 前移）并写审计。
    """
    body = body or {}
    result = CommitRepo(conn).revert_commit(cid, preview=bool(body.get("preview", False)),
                                            actor=_actor(user))
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, result.get("code", 400))
    if not result.get("preview_mode"):
        audit(_actor(user), "commit_revert",
              f"回滚提交 #{cid}: 实体 {result['reverted']['entities']} / "
              f"关系 {result['reverted']['relations']}", conn=conn)
    return {"ok": True, **result}


@router.post("/api/knowledge/entities")
def create_entity(ent: EntityIn, conn=Depends(db_session),
                  user=Depends(require_any_permission(WRITE_PERMS))):
    g = _release_guard(conn, ent.branch or "dev")
    if g:
        return JSONResponse({"error": g}, 400)
    # 本体类型门禁（2026-09-14）：未声明类型禁止直写入库
    terr = _entity_type_check(conn, ent.entity_type)
    if terr:
        return JSONResponse({"error": terr}, 400)
    # P0-1 消歧提示（不阻断）：创建前查询同名/相似实体，随响应返回 dup_warning 供前端确认
    warns = _entity_dup_warning(conn, ent.name)
    KnowledgeRepo(conn).create_entity(
        ent.id, ent.name, ent.entity_type,
        json.dumps(ent.properties, ensure_ascii=False), ent.branch,
        knowledge_category=ent.knowledge_category or "", created_by=_actor(user))
    audit(_actor(user), "entity_create", f"创建实体: {ent.name}", conn=conn, branch=ent.branch or "dev")
    if warns:
        return {"ok": True, "dup_warning": warns}
    return {"ok": True}


@router.put("/api/knowledge/entities/{entity_id}")
def update_entity(entity_id: str, ent: EntityIn, conn=Depends(db_session),
                  user=Depends(require_any_permission(WRITE_PERMS))):
    b = ent.branch or "dev"
    g = _release_guard(conn, b)
    if g:
        return JSONResponse({"error": g}, 400)
    # 本体类型门禁（2026-09-14）：改类到未声明类型禁止（与创建同口径）
    terr = _entity_type_check(conn, ent.entity_type)
    if terr:
        return JSONResponse({"error": terr}, 400)
    # 属性变更审计（2026-09-14 历史页维度补全）：此前 PUT 更新完全无审计留痕，
    # 历史页看不到"改了哪些属性"。与 graph_node_update 同格式（字段: 旧→新），前端可解析 diff
    _old = KnowledgeRepo(conn).get_entity(entity_id, b) or {}
    _diffs = []
    try:
        _op = _old.get("properties") if isinstance(_old.get("properties"), dict) else {}
        try:
            _op = json.loads(_old.get("properties") or "{}") if isinstance(_old.get("properties"), str) else (_op or {})
        except Exception:
            _op = {}
        _keys = set(list((_op or {}).keys()) + list((ent.properties or {}).keys()))
        for _k in list(_keys)[:20]:
            _ov, _nv = (_op or {}).get(_k), (ent.properties or {}).get(_k)
            if str(_ov or '') != str(_nv or ''):
                _diffs.append(f"属性.{_k}: {_ov if _ov is not None else '∅'}→{(_nv[:30] + '…') if _nv and len(str(_nv)) > 30 else (_nv if _nv is not None else '∅')}")
        if _old.get("name") and ent.name != _old.get("name"):
            _diffs.insert(0, f"名称: {_old.get('name')}→{ent.name}")
        if _old.get("entity_type") and ent.entity_type != _old.get("entity_type"):
            _diffs.insert(0, f"类型: {_old.get('entity_type')}→{ent.entity_type}")
    except Exception:
        pass
    KnowledgeRepo(conn).update_entity(
        entity_id, ent.name, ent.entity_type,
        json.dumps(ent.properties, ensure_ascii=False), b,
        knowledge_category=ent.knowledge_category or "")
    audit(_actor(user), "entity_update",
          f"更新实体: {ent.name} ({entity_id})" + ("；" + "；".join(_diffs[:20]) if _diffs else ""),
          conn=conn, branch=b)
    return {"ok": True}


@router.get("/api/knowledge/categories")
def knowledge_categories(group: Optional[str] = None, conn=Depends(db_session)):
    """知识类别列表（设计方法知识/设计资产 子类）。group 过滤大类，空=全部。"""
    q = "SELECT * FROM knowledge_categories"
    params = []
    if group:
        q += " WHERE group_name=?"
        params.append(group)
    q += " ORDER BY sort_order, id"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


@router.put("/api/knowledge/entities/{entity_id}/category")
def knowledge_entity_category(entity_id: str, body: dict, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """给实体打知识类别标签（按全局 id 更新所有分支版本行）。category 空串=清除分类。"""
    category = (body.get("category") or "").strip()
    if category:
        row = conn.execute("SELECT id FROM knowledge_categories WHERE name=?", (category,)).fetchone()
        if not row:
            return JSONResponse({"error": f"知识类别不存在: {category}"}, 400)
    if not conn.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone():
        return JSONResponse({"error": f"实体不存在: {entity_id}"}, 404)
    n = KnowledgeRepo(conn).set_entity_category(entity_id, category)
    audit(_actor(user), "entity_category",
          f"实体打知识类别标签: {entity_id} → {category or '未分类'}", conn=conn)
    return {"ok": True, "entity_id": entity_id, "category": category, "updated_rows": n}


@router.get("/api/knowledge/categories/stats")
def knowledge_category_stats(conn=Depends(db_session)):
    """知识类别统计：各子类实体数（reviewed，dev+release 按 id 去重）与文档数。"""
    cats = conn.execute(
        "SELECT name, group_name FROM knowledge_categories ORDER BY sort_order, id").fetchall()
    ent_rows = conn.execute(
        "SELECT knowledge_category, COUNT(DISTINCT id) AS n FROM entities "
        "WHERE status='reviewed' AND knowledge_category != '' GROUP BY knowledge_category").fetchall()
    ent_map = {r["knowledge_category"]: r["n"] for r in ent_rows}
    doc_rows = conn.execute(
        "SELECT knowledge_category, COUNT(*) AS n FROM documents "
        "WHERE knowledge_category != '' GROUP BY knowledge_category").fetchall()
    doc_map = {r["knowledge_category"]: r["n"] for r in doc_rows}
    out = []
    for c in cats:
        out.append({"name": c["name"], "group": c["group_name"],
                    "entities": ent_map.get(c["name"], 0), "docs": doc_map.get(c["name"], 0)})
    return {"categories": out,
            "total_method": sum(x["entities"] for x in out if x["group"] == "设计方法知识"),
            "total_assets": sum(x["entities"] for x in out if x["group"] == "设计资产")}


@router.post("/api/knowledge/entities/{entity_id}/review")
def review_entity(entity_id: str, body: dict, conn=Depends(db_session),
                  user=Depends(require_permission("kb_review", "confirm"))):
    action = body.get("action", "confirm")  # confirm | reject | modify
    KnowledgeRepo(conn).review_entity(entity_id, action, operator=_actor(user))
    audit(_actor(user), "entity_review", f"审核实体 {entity_id}: {action}", conn=conn)
    return {"ok": True, "action": action}


@router.post("/api/knowledge/entities/batch-review")
def batch_review_entities(body: BatchReviewIn, conn=Depends(db_session),
                          user=Depends(require_permission("kb_review", "confirm"))):
    """S4：批量审核实体（批量通过/批量驳回候选实体）。P1-1 编排已上提 Service。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).batch_review_entities(
        body.entity_ids, body.action, actor=_actor(user))
