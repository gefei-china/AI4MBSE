# -*- coding: utf-8 -*-
"""知识库路由分片：术语表与别名覆盖。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分，勿手工编辑。"""
from routers.knowledge_parts.shared import *


@router.get("/api/knowledge/glossary")
def glossary_list(kind: str = "", keyword: str = "", active: str = "",
                  conn=Depends(db_session), user=Depends(current_user)):
    """词典映射列表：kind 过滤（entity/predicate/prop_key/intent）、关键词、启停。"""
    cond, args = ["1=1"], []
    if kind:
        cond.append("kind=?"); args.append(kind)
    if keyword:
        cond.append("(user_term LIKE ? OR canonical_term LIKE ?)")
        args.extend([f"%{keyword}%", f"%{keyword}%"])
    if active in ("0", "1"):
        cond.append("active=?"); args.append(int(active))
    rows = conn.execute(
        f"SELECT * FROM glossary WHERE {' AND '.join(cond)} ORDER BY kind, user_term LIMIT 500",
        args).fetchall()
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM glossary GROUP BY kind")}
    return {"items": [dict(r) for r in rows], "counts": counts}


@router.post("/api/knowledge/glossary")
def glossary_add(body: dict, conn=Depends(db_session),
                 user=Depends(require_any_permission(WRITE_PERMS))):
    """新增映射：user_term → canonical_term（kind: entity/predicate/prop_key；默认小写存储）。"""
    ut = (body.get("user_term") or "").strip()
    ct = (body.get("canonical_term") or "").strip()
    kind = (body.get("kind") or "entity").strip()
    if not ut or not ct:
        return JSONResponse({"error": "user_term/canonical_term 不能为空"}, 400)
    if kind not in ("entity", "predicate", "prop_key", "intent"):
        return JSONResponse({"error": "kind 须为 entity/predicate/prop_key/intent"}, 400)
    try:
        # 结构化词条：类型专属字段（数据类型/单位/定义域/值域/方向）随 provenance 落库（SKOS/本体约束式管理）
        prov = {"reason": body.get("reason") or "", "confidence": body.get("confidence") or None}
        for k in ("data_type", "unit", "domain", "range", "direction", "entity_type", "definition",
                  "lang", "match_type"):
            if body.get(k):
                prov[k] = body.get(k)
        # R2 锚点校验（2026-08-30）：词典=本体词法层（SKOS altLabel）——规范名应锚定本体概念
        anchored = True
        anchor_hint = ""
        if kind == "entity":
            hit = conn.execute("SELECT 1 FROM ontology_types WHERE name=? AND type_kind='entity'", (ct,)).fetchone()
            if not hit:
                hit = conn.execute("SELECT 1 FROM entities WHERE LOWER(name)=? AND status!='deprecated' LIMIT 1", (ct.lower(),)).fetchone()
            anchored = bool(hit)
            if not anchored:
                anchor_hint = f"规范名「{ct}」未命中本体类或已入库实体——词条将标记为游离（unanchored），归一结果游离于 Schema 之外"
        elif kind == "predicate":
            hit = conn.execute("SELECT 1 FROM ontology_types WHERE name=? AND type_kind='relation'", (ct,)).fetchone()
            anchored = bool(hit)
            if not anchored:
                anchor_hint = f"规范名「{ct}」未命中本体关系类型——词条将标记为游离（unanchored）"
        if not anchored:
            prov["unanchored"] = True
        conn.execute(
            "INSERT INTO glossary (user_term, canonical_term, kind, suggested_by, provenance) "
            "VALUES (?,?,?,?,?)",
            (ut.lower(), ct, kind, "manual", json.dumps(prov, ensure_ascii=False)))
        conn.commit()
    except Exception as e:
        # 幂等回退：user_term UNIQUE 冲突 → 更新既有词条（保留 R2 锚点语义）
        dup = conn.execute("SELECT id FROM glossary WHERE user_term=?", (ut.lower(),)).fetchone()
        if not dup:
            return JSONResponse({"error": f"写入失败：{e}"}, 400)
        conn.execute(
            "UPDATE glossary SET canonical_term=?, kind=?, suggested_by=?, provenance=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (ct, kind, "manual", json.dumps(prov, ensure_ascii=False), dup["id"]))
        conn.commit()
        audit(_actor(user), "glossary_update", f"词典更新(幂等) {kind}: {ut.lower()} → {ct}", conn=conn)
        return {"ok": True, "term": ut.lower(), "canonical": ct, "kind": kind,
                "anchored": anchored, "hint": anchor_hint, "updated": True}
    audit(_actor(user), "glossary_add", f"词典新增 {kind}: {ut.lower()} → {ct}" + ("" if anchored else "【游离】"), conn=conn)
    return {"ok": True, "term": ut.lower(), "canonical": ct, "kind": kind,
            "anchored": anchored, "hint": anchor_hint}


@router.post("/api/knowledge/glossary/impact-preview")
def glossary_impact_preview(body: dict, conn=Depends(db_session)):
    """影响面预检（只读）：新词条保存后将命中多少存量数据（回填预估）+ 重复/链式归一警告。
    扫描对象按 kind 分流：entity→entities.name；predicate→relations.relation_type；prop_key→entities.properties JSON key。"""
    ut = (body.get("user_term") or "").strip().lower()
    ct = (body.get("canonical_term") or "").strip()
    kind = (body.get("kind") or "entity").strip()
    if not ut or not ct:
        return JSONResponse({"error": "user_term/canonical_term 不能为空"}, 400)
    warnings = []
    # 1) 词典内预检：重复 / 无意义 / 链式归一
    dup_rows = conn.execute("SELECT id, canonical_term FROM glossary WHERE user_term=?", (ut,)).fetchall()
    if dup_rows:
        warnings.append(f"词典中已存在 {len(dup_rows)} 条「{ut}」映射（如 {dup_rows[0]['canonical_term']}）——保存将重复，建议先删除或改名")
    if ut == ct.lower():
        warnings.append("词条与标准形相同——归一无意义，建议取消")
    chain = conn.execute("SELECT COUNT(*) c FROM glossary WHERE canonical_term=? AND user_term!=?", (ct, ut)).fetchone()
    if chain and chain["c"]:
        warnings.append(f"标准形「{ct}」同时是 {chain['c']} 条其他词条的词条名——存在链式归一，命中时以先入库者优先")
    # 2) 存量命中扫描（status!='deprecated'；跨分支副本计入，回填任务执行时按 branch 去重）
    detail = {}
    if kind == "entity":
        row = conn.execute(
            "SELECT COUNT(*) c, "
            "SUM(CASE WHEN sysml_version_id>0 THEN 1 ELSE 0 END) sysml_n, "
            "SUM(CASE WHEN name=? THEN 1 ELSE 0 END) canon_n "
            "FROM entities WHERE status!='deprecated' AND LOWER(name)=?", (ct, ut)).fetchone()
        hits = row["c"] or 0
        detail = {"entity_name_hits": hits,
                  "sysml_channel": row["sysml_n"] or 0,
                  "doc_channel": hits - (row["sysml_n"] or 0),
                  "already_canonical": row["canon_n"] or 0}
        if detail["already_canonical"]:
            warnings.append(f"已有 {detail['already_canonical']} 条实体名即「{ct}」——它们是归一目标本体，不计入回填")
    elif kind == "predicate":
        row = conn.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN relation_type=? THEN 1 ELSE 0 END) canon_n "
            "FROM relations WHERE LOWER(relation_type)=?", (ct, ut)).fetchone()
        hits = row["c"] or 0
        detail = {"relation_hits": hits, "already_canonical": row["canon_n"] or 0}
        if detail["already_canonical"]:
            warnings.append(f"已有 {detail['already_canonical']} 条关系谓词即「{ct}」——不计入回填")
    else:  # prop_key：Python 侧解析 properties JSON key（SQLite JSON1 不保证可用）
        hits = 0
        canon_n = 0
        for r in conn.execute("SELECT properties FROM entities WHERE status!='deprecated' AND properties LIKE ?",
                              ('%' + ut + '%',)).fetchall():
            try:
                props = json.loads(r["properties"] or "{}")
            except Exception:
                continue
            for k in props.keys():
                if k.lower() == ut:
                    hits += 1
                elif k == ct:
                    canon_n += 1
        detail = {"prop_key_hits": hits, "already_canonical": canon_n}
        if detail["already_canonical"]:
            warnings.append(f"已有 {canon_n} 个属性 key 即「{ct}」——不计入回填")
    return {"kind": kind, "impact_total": hits, "detail": detail, "warnings": warnings}


@router.get("/api/knowledge/glossary/by-type")
def glossary_by_type(type: str = "", conn=Depends(db_session), user=Depends(current_user)):
    """R1（2026-08-30）：反查锚定到某本体类型的词典词条（provenance.entity_type=type）——本体右栏「别名」区数据源。"""
    if not type:
        return {"items": [], "count": 0}
    # SQLite JSON1 不保证可用（与 impact-preview 同因）——Python 侧解析 provenance
    rows = conn.execute("SELECT * FROM glossary WHERE active=1 ORDER BY user_term").fetchall()
    items = []
    for r in rows:
        try:
            prov = json.loads(r["provenance"] or "{}")
        except Exception:
            continue
        if prov.get("entity_type") == type:
            items.append(dict(r))
    return {"items": items, "count": len(items)}


@router.get("/api/knowledge/glossary/by-canonical")
def glossary_by_canonical(term: str = "", kind: str = "", conn=Depends(db_session), user=Depends(current_user)):
    """R3：统计指向某规范名的启用词条数（本体类型改名前的迁移确认数据源）。"""
    if not term:
        return {"count": 0}
    cond, args = ["canonical_term=?"], [term]
    if kind:
        cond.append("kind=?"); args.append(kind)
    row = conn.execute(f"SELECT COUNT(*) c FROM glossary WHERE {' AND '.join(cond)} AND active=1", args).fetchone()
    return {"count": row["c"] or 0}


@router.post("/api/knowledge/glossary/migrate")
def glossary_migrate(body: dict, conn=Depends(db_session), user=Depends(require_any_permission(WRITE_PERMS))):
    """R3：本体类型改名 → 批量迁移指向旧名的词典规范名（词法层同步规则）。"""
    frm = (body.get("from_term") or "").strip()
    to = (body.get("to_term") or "").strip()
    if not frm or not to:
        return JSONResponse({"error": "from_term/to_term 必填"}, 400)
    cur = conn.execute(
        "UPDATE glossary SET canonical_term=?, updated_at=CURRENT_TIMESTAMP WHERE canonical_term=? AND kind IN ('entity','predicate','prop_key')",
        (to, frm))
    conn.commit()
    audit(_actor(user), "glossary_migrate", f"本体类型改名词典迁移: {frm} → {to}（{cur.rowcount} 条）", conn=conn)
    return {"ok": True, "migrated": cur.rowcount}


@router.get("/api/knowledge/ontology/alias-coverage")
def ontology_alias_coverage(conn=Depends(db_session), user=Depends(current_user)):
    """R3：本体类别名覆盖度——零别名类型=抽取归一盲区（词法层覆盖检查）。"""
    types = conn.execute("SELECT name, type_kind FROM ontology_types WHERE type_kind IN ('entity','relation')").fetchall()
    covered = set()
    for r in conn.execute("SELECT provenance, canonical_term, kind FROM glossary WHERE active=1"):
        if r["kind"] in ("entity", "predicate"):
            covered.add(r["canonical_term"])
        try:
            et = (json.loads(r["provenance"] or "{}") or {}).get("entity_type")
            if et:
                covered.add(et)
        except Exception:
            pass
    zero = [{"name": t["name"], "kind": t["type_kind"]} for t in types if t["name"] not in covered]
    return {"total": len(types), "covered": len(types) - len(zero), "zero_count": len(zero), "zero_alias": zero}


@router.patch("/api/knowledge/glossary/{gid}")
def glossary_update(gid: int, body: dict, conn=Depends(db_session),
                    user=Depends(require_any_permission(WRITE_PERMS))):
    """更新映射（改规范名 / 启停用）。停用 = 归一暂不命中，保留审计。"""
    cur = conn.execute("SELECT id FROM glossary WHERE id=?", (gid,)).fetchone()
    if not cur:
        return JSONResponse({"error": "词条不存在"}, 404)
    sets, args, log_parts = [], [], []
    if body.get("canonical_term"):
        sets.append("canonical_term=?"); args.append((body["canonical_term"]).strip())
        log_parts.append(f"改名→{body['canonical_term'].strip()}")
    if "active" in body:
        sets.append("active=?"); args.append(1 if body["active"] else 0)
        log_parts.append("启用" if body["active"] else "停用")
    if not sets:
        return JSONResponse({"error": "无可更新字段"}, 400)
    sets.append("updated_at=CURRENT_TIMESTAMP")
    conn.execute(f"UPDATE glossary SET {', '.join(sets)} WHERE id=?", (*args, gid))
    conn.commit()
    audit(_actor(user), "glossary_update", f"词典更新 #{gid}: {'; '.join(log_parts)}", conn=conn)
    return {"ok": True}


@router.delete("/api/knowledge/glossary/{gid}")
def glossary_delete(gid: int, conn=Depends(db_session),
                    user=Depends(require_any_permission(WRITE_PERMS))):
    row = conn.execute("SELECT user_term, canonical_term, kind FROM glossary WHERE id=?", (gid,)).fetchone()
    if not row:
        return JSONResponse({"error": "词条不存在"}, 404)
    conn.execute("DELETE FROM glossary WHERE id=?", (gid,))
    conn.commit()
    audit(_actor(user), "glossary_delete",
          f"词典删除 {row['kind']}: {row['user_term']} → {row['canonical_term']}", conn=conn)
    return {"ok": True}
