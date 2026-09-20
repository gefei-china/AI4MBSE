# -*- coding: utf-8 -*-
"""知识库路由分片：本体类型/属性/元数据。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分，勿手工编辑。"""
from routers.knowledge_parts.shared import *


@router.get("/api/knowledge/ontology/check")
def ontology_check(conn=Depends(db_session), user=Depends(current_user)):
    """本体前置检查：AI 建模/抽取前是否已定义实体+关系类型。返回 {ready, missing}。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).ontology_readiness()


@router.get("/api/knowledge/ontology/schema-summary")
def ontology_schema_summary(conn=Depends(db_session), user=Depends(current_user)):
    """本体 Schema 摘要（AI 建模会话页前置预览：实体/关系/属性类型清单）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).ontology_schema_summary()


@router.post("/api/knowledge/ontology/impact-preview")
def ontology_impact_preview(body: dict = None, conn=Depends(db_session),
                            user=Depends(current_user)):
    """2026-09-14 本体编辑影响预览（只读，不写库）。

    body = 编辑后的完整类型字段（同 OntologyTypeIn）+ 可选 tid（编辑时传，新增不传）。
    返回 {severity: red|yellow|green, auto_fix, rename, usage, violations, violation_count, summary}：
    - red  ：改名且有实例引用（L1 内联迁移将在保存事务内自动执行，auto_fix=true）
    - yellow：新约束（dom/range/基数/xsd/白名单）下存在存量违例，需人工裁决
    - green ：无实例影响
    """
    body = body or {}
    from services.ontology_migration import impact_preview as _preview
    try:
        return _preview(conn, body)
    except Exception as e:
        return JSONResponse({"error": f"影响预览失败: {e}"}, 500)


@router.get("/api/knowledge/ontology")
def get_ontology(conn=Depends(db_session)):
    return KnowledgeRepo(conn).list_ontology_types()


@router.post("/api/knowledge/ontology/types")
def ontology_add_type(body: OntologyTypeIn, conn=Depends(db_session),
                      user=Depends(require_any_permission(WRITE_PERMS))):
    from datetime import datetime as _dt
    if body.type_kind not in ("entity", "relation", "attribute"):
        return JSONResponse({"error": "type_kind 必须是 entity|relation|attribute"}, 400)
    dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (body.name,)).fetchone()
    if dup:
        return JSONResponse({"error": f"本体类型已存在: {body.name}"}, 400)
    errs = _validate_ont_parent(conn, body.parent_id, body.type_kind)
    if errs:
        return JSONResponse({"error": "; ".join(errs)}, 400)
    errs = _normalize_relation_domain_range(conn, body)
    if errs:
        return JSONResponse({"error": "; ".join(errs)}, 400)
    # P0-1：IRI 主轴——空则按策略自动生成；显式指定则校验唯一
    from ontology_semantics import make_iri
    iri = (body.iri or "").strip()
    if not iri:
        iri = make_iri(conn, body.name)
    else:
        dup_iri = conn.execute("SELECT id FROM ontology_types WHERE iri=?", (iri,)).fetchone()
        if dup_iri:
            return JSONResponse({"error": f"IRI 已存在: {iri}"}, 400)
    conn.execute(
        "INSERT INTO ontology_types (name, type_kind, parent_id, properties, constraints, description, icon, color, iri, profile_source, profile_ref, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.name, body.type_kind, body.parent_id or None,
         json.dumps(body.properties, ensure_ascii=False),
         json.dumps(body.constraints, ensure_ascii=False),
         body.description, body.icon or "", body.color or "#185FA5", iri,
         body.profile_source or "", body.profile_ref or "", _actor(user)))
    tid = conn.execute("SELECT id FROM ontology_types WHERE name=?", (body.name,)).fetchone()["id"]
    conn.commit()
    # FR-KG-4 补 G7：变更留痕（before 空，after 为插入后完整行）
    _log_ont_change(conn, tid, "add", {},
                    dict(conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone() or {}), user)
    # 2026-09-08 版本语义收敛：编辑态（新增/更新/删除/导入/蓝图应用）只写变更留痕（ontology_change_logs），
    # 版本号（ontology_versions）仅在发布（/version/publish、/version/release）时生成并升级——未发布不升版本
    audit(_actor(user), "ontology_add", f"本体新增类型: {body.name} ({body.type_kind})", conn=conn)
    return {"ok": True, "id": tid}


@router.put("/api/knowledge/ontology/types/{tid}")
def ontology_update_type(tid: int, body: OntologyTypeIn, conn=Depends(db_session),
                         user=Depends(require_any_permission(WRITE_PERMS))):
    from datetime import datetime
    # FR-KG-4 补 G7：变更留痕需要变更前快照（先取整行，不存在 → 404）
    before = conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone()
    if not before:
        return JSONResponse({"error": "not found"}, 404)
    errs = _validate_ont_parent(conn, body.parent_id, body.type_kind, exclude_id=tid)
    if errs:
        return JSONResponse({"error": "; ".join(errs)}, 400)
    # 走查修复（2026-08-29）：PUT 改名重名检查（POST 有查，PUT 此前漏查）
    # 2026-09-02：去掉 type_kind 条件——name 已建全局 UNIQUE 索引（idx_ontology_types_name），预检语义须一致
    dup2 = conn.execute("SELECT id FROM ontology_types WHERE name=? AND id!=?",
                        (body.name, tid)).fetchone()
    if dup2:
        return JSONResponse({"error": f"同名类型已存在: {body.name}"}, 400)
    # P1-8（2026-09-07）：relation domain/range 旧写法归一进 allowed_values 并校验
    # （替代原先只校验不归一的双轨逻辑，SHACL/推理只读 allowed_values 单一事实源）
    errs = _normalize_relation_domain_range(conn, body)
    if errs:
        return JSONResponse({"error": "; ".join(errs)}, 400)
    # B/C 增强（2026-08-29）：Data Properties 域绑定与 Disjoint With 引用校验（防悬空）
    if body.type_kind == "attribute":
        ent_names = {r["name"] for r in conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
        bad = sorted({c for c in (body.constraints.get("domain_classes") or []) if c not in ent_names})
        if bad:
            return JSONResponse({"error": f"Domain 绑定指向不存在的实体类型: {'、'.join(bad)}"}, 400)
    if body.type_kind in ("entity", "relation"):
        ent_names = {r["name"] for r in conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
        bad = sorted({c for c in (body.constraints.get("disjoint_with") or []) if c not in ent_names})
        if bad:
            return JSONResponse({"error": f"Disjoint With 指向不存在的实体类型: {'、'.join(bad)}"}, 400)
    # R2a：推理性质白名单校验（transitive/symmetric/asymmetric/reflexive/functional/inverse_functional）
    if body.constraints.get("characteristics"):
        _OWL_CHARS = {"transitive", "symmetric", "asymmetric", "reflexive", "functional", "inverse_functional"}
        _badc = sorted({c for c in body.constraints["characteristics"] if c not in _OWL_CHARS})
        if _badc:
            return JSONResponse({"error": f"非法推理性质: {'、'.join(_badc)}（支持: 传递/对称/反对称/自反/函数型/反函数型）"}, 400)
    # R2a：推理性质白名单校验（transitive/symmetric/asymmetric/reflexive/functional/inverse_functional）
    if body.constraints.get("characteristics"):
        _OWL_CHARS = {"transitive", "symmetric", "asymmetric", "reflexive", "functional", "inverse_functional"}
        _badc = sorted({c for c in body.constraints["characteristics"] if c not in _OWL_CHARS})
        if _badc:
            return JSONResponse({"error": f"非法推理性质: {'、'.join(_badc)}（支持: 传递/对称/反对称/自反/函数型/反函数型）"}, 400)
    # R4：关系端点基数 card_src/card_tgt 校验（SHACL 基数输入）
    for _ck in ("card_src", "card_tgt"):
        _cd = body.constraints.get(_ck)
        if not _cd:
            continue
        if not isinstance(_cd, dict):
            return JSONResponse({"error": f"{_ck} 须为 {{'min','max'}} 对象"}, 400)
        for _fk in ("min", "max"):
            _fv = _cd.get(_fk)
            if _fv is None or _fv == "":
                continue
            try:
                _iv = int(_fv)
            except (TypeError, ValueError):
                return JSONResponse({"error": f"{_ck}.{_fk} 须为非负整数（0=不限）"}, 400)
            if _iv < 0:
                return JSONResponse({"error": f"{_ck}.{_fk} 不能为负"}, 400)
        _mn, _mx = _cd.get("min"), _cd.get("max")
        if (_mn is not None and _mn != "" and _mx is not None and _mx != "" and int(_mn) > int(_mx)):
            return JSONResponse({"error": f"{_ck} 的 min 不能大于 max（min={_mn} max={_mx}）"}, 400)
    # P0-1：IRI 处理——显式指定校验唯一；空则自动生成（改名联动：自动生成的 IRI 跟随 name，用户自定义 IRI 保留）
    from ontology_semantics import make_iri, slugify, get_ontology_meta
    old_iri = before["iri"] or ""
    new_iri = (body.iri or "").strip()
    if new_iri:
        dup_iri = conn.execute("SELECT id FROM ontology_types WHERE iri=? AND id!=?", (new_iri, tid)).fetchone()
        if dup_iri:
            return JSONResponse({"error": f"IRI 已存在: {new_iri}"}, 400)
    else:
        ns = get_ontology_meta(conn).get("namespace") or ""
        auto_slug = (ns + slugify(before["name"])) if ns else slugify(before["name"])
        if old_iri and before["name"] != body.name and old_iri == auto_slug:
            # 自动生成的 IRI → 改名联动重新生成（冲突追加 _2/_3）
            new_iri = make_iri(conn, body.name,
                               taken={r["iri"] for r in conn.execute(
                                   "SELECT iri FROM ontology_types WHERE iri!='' AND id!=?", (tid,)).fetchall()})
        else:
            new_iri = old_iri
    # P1-8：生命周期——status 白名单（draft/review/released/deprecated），空=不变更
    _ONT_STATUS = ("draft", "review", "released", "deprecated")
    if body.status is not None and body.status not in _ONT_STATUS:
        return JSONResponse({"error": f"非法 status: {body.status}（支持: {'/'.join(_ONT_STATUS)}）"}, 400)
    new_status = body.status or before["status"] or "released"
    conn.execute(
        "UPDATE ontology_types SET name=?, type_kind=?, parent_id=?, properties=?, constraints=?, description=?, icon=?, color=?, iri=?, "
        "profile_source=?, profile_ref=?, updated_at=?, updated_by=?, status=?, replaced_by=?, deprecated_note=? WHERE id=?",
        (body.name, body.type_kind, body.parent_id or None,
         json.dumps(body.properties, ensure_ascii=False),
         json.dumps(body.constraints, ensure_ascii=False),
         body.description, body.icon or "", body.color or "#185FA5", new_iri,
        body.profile_source or "", body.profile_ref or "",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _actor(user),
        new_status, body.replaced_by or "", body.deprecated_note or "", tid))
    # 2026-09-14 L1 内联迁移（见 docs/本体变更实例影响分析与自动迁移方案.md）：
    # 改名属无歧义破坏性变更 → 实例/关系/属性键在同一事务内按旧名批量替换（幂等 SQL），
    # 与本体修改原子提交，杜绝"改名后实例悬空"。迁移数随变更留痕与响应返回。
    migrated = {"entities": 0, "relations": 0, "prop_keys": 0}
    if (before["name"] or "") != body.name:
        if before["type_kind"] == "entity":
            _cur = conn.execute(
                "UPDATE entities SET entity_type=? WHERE entity_type=? AND status!='deprecated'",
                (body.name, before["name"]))
            migrated["entities"] = _cur.rowcount or 0
        elif before["type_kind"] == "relation":
            _cur = conn.execute(
                "UPDATE relations SET relation_type=? WHERE relation_type=?",
                (body.name, before["name"]))
            migrated["relations"] = _cur.rowcount or 0
        elif before["type_kind"] == "attribute":
            # 属性类型改名 → 实例 properties JSON 键跟随（LIKE 预筛 + Python 精确改名，规避 json1 路径转义坑）
            _pat = "%" + before["name"].replace("%", r"\%").replace("_", r"\_") + "%"
            for _er in conn.execute(
                    "SELECT id, branch, properties FROM entities "
                    "WHERE properties LIKE ? ESCAPE '\\' AND status!='deprecated'",
                    (_pat,)).fetchall():
                _po = _er["properties"]
                try:
                    _po = json.loads(_po or "{}")
                except Exception:
                    continue
                if not isinstance(_po, dict) or before["name"] not in _po:
                    continue
                _po[body.name] = _po.pop(before["name"])
                conn.execute("UPDATE entities SET properties=? WHERE id=? AND branch=?",
                             (json.dumps(_po, ensure_ascii=False), _er["id"], _er["branch"]))
                migrated["prop_keys"] += 1
        if before["type_kind"] == "entity":
            # 实体类型改名联动：关系 allowed_values.src/tgt 与属性 domain_classes 中
            # 对该实体类型名的引用同步改名（防本体侧悬空引用 → bad_dom_range 高危）
            for _r in conn.execute(
                    "SELECT id, constraints FROM ontology_types "
                    "WHERE type_kind IN ('relation','attribute') AND constraints LIKE ?",
                    ("%" + before["name"] + "%",)).fetchall():
                try:
                    _c = json.loads(_r["constraints"] or "{}")
                except Exception:
                    continue
                if not isinstance(_c, dict):
                    continue
                _changed = False
                _av = _c.get("allowed_values")
                if isinstance(_av, dict):
                    for _side in ("src", "tgt"):
                        if isinstance(_av.get(_side), list) and before["name"] in _av[_side]:
                            _av[_side] = [body.name if x == before["name"] else x for x in _av[_side]]
                            _changed = True
                elif isinstance(_av, list) and before["name"] in _av:
                    _c["allowed_values"] = [body.name if x == before["name"] else x for x in _av]
                    _changed = True
                _dc = _c.get("domain_classes")
                if isinstance(_dc, list) and before["name"] in _dc:
                    _c["domain_classes"] = [body.name if x == before["name"] else x for x in _dc]
                    _changed = True
                if _changed:
                    conn.execute("UPDATE ontology_types SET constraints=? WHERE id=?",
                                 (json.dumps(_c, ensure_ascii=False), _r["id"]))
    conn.commit()
    # FR-KG-4 补 G7：变更留痕（before 为更新前完整行，after 为更新后完整行；L1 迁移数附在 after._migration）
    _after_row = dict(conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone() or {})
    if migrated["entities"] or migrated["relations"] or migrated["prop_keys"]:
        _after_row["_migration"] = migrated
    _log_ont_change(conn, tid, "update", dict(before), _after_row, user)
    # 2026-09-08 版本语义收敛：编辑态只留变更记录，不升版本号（发布时由 /version/publish 按 diff 定 SemVer）
    # 字段级本体变更审计（类/对象属性/数据属性：哪个字段 旧→新），with 业务同事务
    _s = lambda v: ('∅' if v is None else str(v))
    _ochg = []
    try:
        _ab = dict(before or {})
        _af = dict(conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone() or {})
        for _k in ("name", "type_kind", "description", "icon", "color", "status"):
            if str(_ab.get(_k) or "") != str(_af.get(_k) or ""):
                _ochg.append(_k + ":" + _s(_ab.get(_k)) + "→" + _s(_af.get(_k)))
        for _k in ("properties", "constraints"):
            _pb, _pf = _ab.get(_k), _af.get(_k)
            try: _pb = json.loads(_pb) if isinstance(_pb, str) else (_pb or {})
            except Exception: _pb = {}
            try: _pf = json.loads(_pf) if isinstance(_pf, str) else (_pf or {})
            except Exception: _pf = {}
            if not isinstance(_pb, dict): _pb = {}
            if not isinstance(_pf, dict): _pf = {}
            for _x in sorted(set(list(_pb.keys())) | set(list(_pf.keys()))):
                if _pb.get(_x) != _pf.get(_x):
                    _ochg.append(_k + "." + _x + ":" + _s(_pb.get(_x)) + "→" + _s(_pf.get(_x)))
    except Exception:
        _ochg = []
    _odiff = ("；" + "；".join(_ochg[:20])) if _ochg else ""
    if migrated["entities"] or migrated["relations"] or migrated["prop_keys"]:
        _odiff += f"；L1实例迁移: 实体×{migrated['entities']} 关系×{migrated['relations']} 属性键×{migrated['prop_keys']}"
    audit(_actor(user), "ontology_update", f"本体更新类型#{tid}: {body.name}{_odiff}", conn=conn)
    return {"ok": True, "migrated_instances": migrated["entities"],
            "migrated_relations": migrated["relations"], "migrated_prop_keys": migrated["prop_keys"]}


@router.get("/api/knowledge/ontology/types/{tid}")
def ontology_get_type(tid: int, conn=Depends(db_session)):
    """S3：单个本体类型详情（前端右栏/编辑表单回填）。"""
    row = conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    d = dict(row)
    for k in ("properties", "constraints"):
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except Exception:
            d[k] = {}
    return d


@router.get("/api/knowledge/ontology/meta")
def ontology_get_meta(conn=Depends(db_session)):
    """P0-1：本体元信息（命名空间 / IRI 策略 / 默认前缀），前端 IRI 生成与设置面板消费。"""
    from ontology_semantics import get_ontology_meta
    return get_ontology_meta(conn)


@router.put("/api/knowledge/ontology/meta")
def ontology_update_meta(body: dict = None, conn=Depends(db_session),
                         user=Depends(require_any_permission(WRITE_PERMS))):
    """P0-1：更新本体元信息（命名空间 / IRI 生成策略），影响后续新建实体 IRI。"""
    from ontology_semantics import get_ontology_meta, DEFAULT_NAMESPACE
    body = body or {}
    ns = str(body.get("namespace") or "").strip()
    strategy = str(body.get("iri_strategy") or "hash-name").strip()
    prefix = str(body.get("default_prefix") or "").strip()
    if strategy not in ("hash-name", "uuid", "user-supplied"):
        return JSONResponse({"error": "iri_strategy 必须是 hash-name|uuid|user-supplied"}, 400)
    if ns and not (ns.startswith("http://") or ns.startswith("https://")):
        return JSONResponse({"error": "namespace 必须是 http(s):// 开头的 URI"}, 400)
    conn.execute(
        "UPDATE ontology_meta SET namespace=?, iri_strategy=?, default_prefix=?, updated_at=CURRENT_TIMESTAMP WHERE id=1",
        (ns or DEFAULT_NAMESPACE, strategy, prefix))
    conn.commit()
    audit(_actor(user), "ontology_meta",
          f"更新本体元信息：namespace={ns or DEFAULT_NAMESPACE}, strategy={strategy}", conn=conn)
    return {"ok": True}


@router.get("/api/knowledge/ontology/types/{tid}/usage")
def ontology_get_usage(tid: int, conn=Depends(db_session)):
    """P0-1：本体类型反向引用（Usage）——聚合谁在引用该类型。

    - as_parent : 以该类型为父类型的子类型（层级继承）
    - as_source : 允许该类型作为来源的关系
    - as_target : 允许该类型作为目标的关系
    - instances : 以该类型实例化的实体数（全分支 · 非废弃）
    """
    row = conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    name = row["name"]
    children = [{"id": r["id"], "name": r["name"], "type_kind": r["type_kind"]}
                for r in conn.execute(
                    "SELECT id, name, type_kind FROM ontology_types WHERE parent_id=? AND id!=?",
                    (tid, tid)).fetchall()]
    as_source, as_target = [], []
    for r in conn.execute(
            "SELECT id, name, constraints FROM ontology_types WHERE type_kind='relation'").fetchall():
        try:
            cons = json.loads(r["constraints"] or "{}")
        except Exception:
            cons = {}
        av = cons.get("allowed_values") or {}
        srcs, tgts = av.get("src") or [], av.get("tgt") or []
        if isinstance(srcs, str): srcs = [srcs]
        if isinstance(tgts, str): tgts = [tgts]
        if name in srcs:
            as_source.append({"id": r["id"], "name": r["name"]})
        if name in tgts:
            as_target.append({"id": r["id"], "name": r["name"]})
    instances = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
        (name,)).fetchone()[0]
    return {"type": {"id": row["id"], "name": name, "type_kind": row["type_kind"]},
            "as_parent": children, "as_source": as_source, "as_target": as_target,
            "instances": instances}


@router.delete("/api/knowledge/ontology/types/{tid}")
def ontology_delete_type(tid: int, force: bool = False, conn=Depends(db_session),
                         user=Depends(require_any_permission(WRITE_PERMS))):
    # R1=B（2026-09-01）：数据集成移除，R2RML 联动检查已删
    # FR-KG-4 补 G7：删除留痕需要删除前整行快照（不存在 → 404）
    row = conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    # FR-KG-4 补 G7：受影响实例数 = entities 中 entity_type=该类型名 且未废弃（幂等 SQL）
    affected = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
        (row["name"],)).fetchone()[0]
    # 走查修复（2026-08-29）：结构引用检查——子类引用 / 对象属性 dom-range 引用（此前缺失 → 删父类致子类 parent 悬空断链）
    blockers = []
    children = conn.execute("SELECT name FROM ontology_types WHERE parent_id=?", (tid,)).fetchall()
    if children:
        blockers.append("子类型 ×%d（%s）" % (len(children), "、".join(c["name"] for c in children[:5])))
    rel_refs = []
    for r in conn.execute("SELECT id, name, constraints FROM ontology_types WHERE type_kind='relation'").fetchall():
        try:
            c = json.loads(r["constraints"] or "{}")
        except Exception:
            c = {}
        if c.get("domain") == row["name"] or c.get("range") == row["name"]:
            rel_refs.append(r["name"])
    if rel_refs:
        blockers.append("对象属性 dom/range 引用 ×%d（%s）" % (len(rel_refs), "、".join(rel_refs[:5])))
    if blockers and not force:
        return JSONResponse(
            {"error": "该类型存在结构引用，直接删除将导致断链（可用 force=true 级联：子类提升为根、关系引用清空）",
             "blockers": blockers, "affected_instances": affected}, 409)
    if blockers and force:
        conn.execute("UPDATE ontology_types SET parent_id=NULL WHERE parent_id=?", (tid,))
        for r in conn.execute("SELECT id, constraints FROM ontology_types WHERE type_kind='relation'").fetchall():
            try:
                c = json.loads(r["constraints"] or "{}")
            except Exception:
                c = {}
            changed = False
            if c.get("domain") == row["name"]:
                c["domain"] = ""; changed = True
            if c.get("range") == row["name"]:
                c["range"] = ""; changed = True
            if changed:
                conn.execute("UPDATE ontology_types SET constraints=? WHERE id=?", (json.dumps(c, ensure_ascii=False), r["id"]))
        audit(_actor(user), "ontology_delete_force",
              f"强制删除类型#{tid}: {row['name']}（子类提升为根 · 关系 dom/range 置空）", conn=conn)
    conn.execute("DELETE FROM ontology_types WHERE id=?", (tid,))
    conn.commit()
    # FR-KG-4 补 G7：变更留痕（before 为删除前完整行，after 空）
    _log_ont_change(conn, tid, "delete", dict(row), {}, user)
    # 2026-09-08 版本语义收敛：删除属编辑态，只留变更记录；破坏性升级在发布时由 diff 判定（removed→major）
    audit(_actor(user), "ontology_delete", f"本体删除类型#{tid}: {row['name']}", conn=conn)
    return {"ok": True, "affected_instances": affected}
