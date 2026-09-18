"""Glossary 术语归一化 / 查询 Trace / domain review 队列 API（P0-1 / P2-2 / P2-3）。

行业对齐：CloudCanal 实体关联 + AWS Metadata Filtering + 可解释路由。
- /api/glossary*：术语表 CRUD（米爸在 Studio 维护，新增歧义不用改代码）
- /api/knowledge/trace*：查询全链路（query→归一化→意图→域→命中→输出）
- /api/knowledge/domain-reviews*：低置信度 domain 分类人工确认队列
"""
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user
from core.audit import audit
from models import GlossaryIn, DomainReviewIn

router = APIRouter(tags=["术语·Trace·域审核"])


def _glog(conn, cid: str, action: str, detail: str, operator: str = "", reason: str = ""):
    """P0 治理闭环：词典概念变更留痕（对标 TBX 变更控制 / PoolParty 历史）。"""
    try:
        conn.execute(
            "INSERT INTO glossary_changelog (concept_id, action, detail, reason, operator) "
            "VALUES (?,?,?,?,?)", (cid, action, detail[:500], (reason or "")[:300], operator))
    except Exception:
        pass


# ── Glossary CRUD ──
@router.get("/api/glossary")
def glossary_list(domain: Optional[str] = None, active_only: int = 1, conn=Depends(db_session)):
    """术语列表。domain 过滤（None=全部）；active_only=1 只返回启用。"""
    sql = "SELECT * FROM glossary"
    conds, params = [], []
    if active_only:
        conds.append("active=1")
    if domain and domain != "all":
        conds.append("domain=?")
        params.append(domain)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY domain, LENGTH(user_term) DESC"
    rows = conn.execute(sql, params).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# 2026-09-07：旧归一映射表 glossary 已于 R1 合并后归档（active 全置 0），写入功能废弃。
# 保留读：用于追溯「当年合并进来哪些词」。写操作一律拒绝，避免绕过概念层产生双写源。
_DEPRECATED_WRITE_MSG = (
    "归一映射（旧表 glossary）已于 2026-09-07 并入概念层，写入功能已废弃。"
    "请改用 /api/glossary/concepts 与 /api/glossary/concepts/{cid}/terms。"
)


@router.post("/api/glossary")
def glossary_create_deprecated():
    return JSONResponse({"error": _DEPRECATED_WRITE_MSG, "code": "GONE"}, 410)


@router.put("/api/glossary/{gid}")
def glossary_update_deprecated(gid: int):
    return JSONResponse({"error": _DEPRECATED_WRITE_MSG, "code": "GONE"}, 410)


@router.delete("/api/glossary/{gid}")
def glossary_delete_deprecated(gid: int):
    return JSONResponse({"error": _DEPRECATED_WRITE_MSG, "code": "GONE"}, 410)


@router.post("/api/glossary/resolve")
def glossary_resolve(body: dict, conn=Depends(db_session)):
    """调试端点：输入任意文本，返回归一化结果 + 强制意图/域 + 命中术语。"""
    from glossary import GlossaryMatcher
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text 必填"}, 400)
    res = GlossaryMatcher(conn).resolve(text)
    return {
        "original": res["original"],
        "normalized": res["normalized"],
        "force_intent": res["force_intent"],
        "force_domain": res["force_domain"],
        "boost": res["boost"],
        "hits": [{"user_term": h["user_term"], "canonical_term": h["canonical_term"],
                  "domain": h["domain"], "intent": h.get("intent", ""),
                  "concept_id": h.get("concept_id", ""),
                  "concept_status": h.get("concept_status", ""),
                  "replaced_by": h.get("replaced_by", ""),
                  "maps_to_class": h.get("maps_to_class", ""),
                  "maps_to_inst": h.get("maps_to_inst", "")} for h in res["hits"]],
        "deprecated_hits": res.get("deprecated_hits", []),
    }


# ── 概念层（P0-3/P0-4）：概念-术语双栏维护 ──

_CONCEPT_STATUSES = ("candidate", "approved", "deprecated", "retired")


def _homonym_map(conn) -> dict:
    """同形异义检测：同一 (term, lang) 指向多个概念。返回 {term: concept_count}。"""
    rows = conn.execute(
        "SELECT term, COUNT(DISTINCT concept_id) AS n FROM glossary_terms "
        "GROUP BY term, lang HAVING n > 1").fetchall()
    return {r["term"]: r["n"] for r in rows}


@router.get("/api/glossary/concepts")
def concepts_list(status: str = "", domain: str = "", keyword: str = "",
                  limit: int = 200, conn=Depends(db_session)):
    """概念列表（含术语数、映射、同形异义标记）。"""
    sql = ("SELECT c.*, (SELECT COUNT(*) FROM glossary_terms t WHERE t.concept_id=c.concept_id) "
           "AS term_count FROM glossary_concepts c")
    conds, params = [], []
    if status and status != "all":
        conds.append("c.concept_status=?")
        params.append(status)
    if domain and domain != "all":
        conds.append("c.domain=?")
        params.append(domain)
    if keyword:
        # P1 检索增强：概念名/定义/ID 之外，同义词、英文对照、缩写也可命中（对标术语在线多叫法检索）
        conds.append("(c.pref_label LIKE ? OR c.definition LIKE ? OR c.concept_id LIKE ? "
                     "OR EXISTS (SELECT 1 FROM glossary_terms t WHERE t.concept_id=c.concept_id AND t.term LIKE ?))")
        params.extend([f"%{keyword}%"] * 4)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY c.concept_status, c.pref_label LIMIT ?"
    params.append(min(limit, 1000))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    homo = _homonym_map(conn)
    en_map = {}
    for t in conn.execute("SELECT concept_id, term FROM glossary_terms WHERE lang='en' "
                          "AND term_kind!='abbr' ORDER BY LENGTH(term) DESC").fetchall():
        en_map.setdefault(t["concept_id"], t["term"])   # 取最长英文词条（避免把缩写当对照）
    # 上位概念名：列表直接带出，省掉前端二次请求
    label_map = {r["concept_id"]: r["pref_label"] for r in conn.execute(
        "SELECT concept_id, pref_label FROM glossary_concepts")}
    for r in rows:
        r["homonym"] = r["pref_label"] in homo
        r["english"] = en_map.get(r["concept_id"], "")
        r["intent"] = r.get("intent") or ""
        r["boost"] = r.get("boost") or 1.0
        # ISO 704/25964 扩展字段兜底（老行迁移前无此列时 None → ''）
        for k in ("context", "note", "source", "broader", "related"):
            r[k] = r.get(k) or ""
        r["broader_label"] = label_map.get(r["broader"], "") if r["broader"] else ""
    domains = [r["domain"] for r in conn.execute(
        "SELECT DISTINCT domain FROM glossary_concepts WHERE domain IS NOT NULL AND domain!='' "
        "ORDER BY domain").fetchall()]
    cand = conn.execute("SELECT COUNT(*) FROM glossary_concepts WHERE concept_status='candidate'").fetchone()[0]
    return {"items": rows, "count": len(rows), "domains": domains, "pending": cand}


@router.get("/api/glossary/concepts/{cid}")
def concept_detail(cid: str, conn=Depends(db_session)):
    """概念详情（含术语表、弃用影响、同形异义）。"""
    c = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
    if not c:
        return JSONResponse({"error": "概念不存在"}, 404)
    terms = [dict(r) for r in conn.execute(
        "SELECT * FROM glossary_terms WHERE concept_id=? ORDER BY term_kind, term", (cid,))]
    # 弃用影响（调研 G4）：实例/关系仍在用该概念的任一术语
    term_vals = [t["term"] for t in terms]
    impact = []
    if term_vals:
        ph = ",".join("?" * len(term_vals))
        for tbl, idcol, namecol, kind in (
                ("entities", "id", "name", "entity"),
                ("relations", "id", "relation_type", "relation")):
            try:
                rows = conn.execute(
                    f"SELECT {idcol} AS id, {namecol} AS name FROM {tbl} "
                    f"WHERE status!='deprecated' AND name IN ({ph}) LIMIT 50",
                    term_vals).fetchall()
                impact.extend([{**dict(r), "kind": kind} for r in rows])
            except Exception:
                pass
    homo = _homonym_map(conn)
    concept = dict(c)
    concept["intent"] = concept.get("intent") or ""
    concept["boost"] = concept.get("boost") or 1.0
    ens = [t["term"] for t in terms if t.get("lang") == "en" and t["term_kind"] != "abbr"]
    en = max(ens, key=len) if ens else ""
    concept["english"] = en
    # ISO 25964 / SKOS：narrower 不落库，由 broader 反向推导（避免双向维护不一致）
    concept["narrower"] = [r["concept_id"] for r in conn.execute(
        "SELECT concept_id FROM glossary_concepts WHERE broader=? AND concept_id!=?",
        (cid, cid))]
    concept["context"] = concept.get("context") or ""
    concept["note"] = concept.get("note") or ""
    concept["source"] = concept.get("source") or ""
    concept["broader"] = concept.get("broader") or ""
    concept["related"] = concept.get("related") or ""
    return {"concept": concept, "terms": terms, "impact": impact,
            "homonym": homo.get(c["pref_label"], 0)}


@router.post("/api/glossary/concepts")
def concept_create(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """新建概念。concept_id 由规范词确定性派生（C-loc_key），改名不变 ID。"""
    from core.ns import loc_key as _loc_key   # 2026-09-09：migrate_p1_semantic 已归档，改用等价实现
    label = (body.get("pref_label") or "").strip()
    if not label:
        return JSONResponse({"error": "pref_label 必填"}, 400)
    cid = (body.get("concept_id") or "").strip() or ("C-" + _loc_key(label))
    if conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone():
        return JSONResponse({"error": f"概念已存在: {cid}"}, 409)
    status = body.get("concept_status") or "candidate"
    if status not in _CONCEPT_STATUSES:
        return JSONResponse({"error": f"非法状态: {status}"}, 400)
    # ISO 704/25964：概念关系校验（broader / related 必须指向已存在的概念，且不能自指）
    broader = (body.get("broader") or "").strip()
    if broader and not conn.execute(
            "SELECT 1 FROM glossary_concepts WHERE concept_id=?", (broader,)).fetchone():
        return JSONResponse({"error": f"上位概念不存在: {broader}"}, 400)
    related = [x.strip() for x in re.split(r"[,，;；]+", str(body.get("related") or ""))
               if x.strip()]
    for rid in related:
        if not conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?", (rid,)).fetchone():
            return JSONResponse({"error": f"相关概念不存在: {rid}"}, 400)
    conn.execute(
        "INSERT INTO glossary_concepts (concept_id, pref_label, definition, domain, "
        "concept_status, maps_to_class, maps_to_prop, maps_to_inst, created_by, "
        "intent, boost, context, note, source, broader, related) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, label, (body.get("definition") or "").strip(),
         body.get("domain") or "unknown", status,
         body.get("maps_to_class") or "", body.get("maps_to_prop") or "",
         body.get("maps_to_inst") or "", "王工",
         (body.get("intent") or "").strip(), body.get("boost") or 1.0,
         (body.get("context") or "").strip(), (body.get("note") or "").strip(),
         (body.get("source") or "").strip(), broader, ",".join(related)))
    # 首选术语
    conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                 "term_status, source) VALUES (?,?,?,?,?,?)",
                 (cid, label, "zh", "preferred", "preferred", "manual"))
    # 英文对照（可选）
    en = (body.get("english") or "").strip()
    if en:
        conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                     "term_status, source) VALUES (?,?,?,?,?,?)",
                     (cid, en, "en", "synonym", "admitted", "manual"))
    conn.commit()
    _actor = getattr(user, "username", "") or "王工"
    _glog(conn, cid, "add", f"新建概念「{label}」[{cid}]（{status}）", _actor)
    conn.commit()
    audit(_actor, "concept_create", f"新建概念: {label} [{cid}]", conn=conn)
    return {"ok": True, "concept_id": cid}


@router.put("/api/glossary/concepts/{cid}")
def concept_update(cid: str, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """更新概念。规则：审批需定义非空（缺定义拦截）；弃用需 replaced_by；改名不动 ID。"""
    row = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
    if not row:
        return JSONResponse({"error": "概念不存在"}, 404)
    cur = dict(row)
    label = (body.get("pref_label") or cur["pref_label"]).strip()
    definition = (body.get("definition") if body.get("definition") is not None
                  else cur["definition"]).strip()
    status = body.get("concept_status") or cur["concept_status"]
    replaced_by = (body.get("replaced_by") if body.get("replaced_by") is not None
                   else cur["replaced_by"]).strip()
    if status not in _CONCEPT_STATUSES:
        return JSONResponse({"error": f"非法状态: {status}"}, 400)
    if status == "approved" and not definition:
        return JSONResponse({"error": "缺定义拦截：定义为空的概念不允许提交审批（ISO 704）"}, 422)
    if status == "deprecated" and not replaced_by:
        return JSONResponse({"error": "弃用拦截：必须指定替代概念 replaced_by"}, 422)
    if replaced_by and not conn.execute(
            "SELECT 1 FROM glossary_concepts WHERE concept_id=?", (replaced_by,)).fetchone():
        return JSONResponse({"error": f"替代概念不存在: {replaced_by}"}, 400)
    # ISO 704/25964：概念关系校验
    broader = (body.get("broader") if body.get("broader") is not None
               else (cur.get("broader") or "")).strip()
    if broader == cid:
        return JSONResponse({"error": "上位概念不能指向自身"}, 400)
    if broader and not conn.execute(
            "SELECT 1 FROM glossary_concepts WHERE concept_id=?", (broader,)).fetchone():
        return JSONResponse({"error": f"上位概念不存在: {broader}"}, 400)
    rel_raw = body.get("related") if body.get("related") is not None else (cur.get("related") or "")
    related = [x.strip() for x in re.split(r"[,，;；]+", str(rel_raw)) if x.strip() and x.strip() != cid]
    for rid in related:
        if not conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?", (rid,)).fetchone():
            return JSONResponse({"error": f"相关概念不存在: {rid}"}, 400)
    conn.execute(
        "UPDATE glossary_concepts SET pref_label=?, definition=?, domain=?, concept_status=?, "
        "maps_to_class=?, maps_to_prop=?, maps_to_inst=?, replaced_by=?, intent=?, boost=?, "
        "context=?, note=?, source=?, broader=?, related=?, "
        "version=version+1, updated_at=CURRENT_TIMESTAMP WHERE concept_id=?",
        (label, definition, body.get("domain") or cur["domain"], status,
         body.get("maps_to_class", cur["maps_to_class"]),
         body.get("maps_to_prop", cur["maps_to_prop"]),
         body.get("maps_to_inst", cur["maps_to_inst"]),
         replaced_by,
         body.get("intent") if body.get("intent") is not None else (cur.get("intent") or ""),
         body.get("boost") if body.get("boost") is not None else (cur.get("boost") or 1.0),
         (body.get("context") if body.get("context") is not None else (cur.get("context") or "")).strip(),
         (body.get("note") if body.get("note") is not None else (cur.get("note") or "")).strip(),
         (body.get("source") if body.get("source") is not None else (cur.get("source") or "")).strip(),
         broader, ",".join(related),
         cid))
    # 英文对照编辑：空则不动；有值则 upsert（同一概念只保留一条 en 主对照）
    if body.get("english") is not None:
        en = (body.get("english") or "").strip()
        old = conn.execute("SELECT id FROM glossary_terms WHERE concept_id=? AND lang='en' "
                           "AND term_kind!='abbr' ORDER BY id LIMIT 1", (cid,)).fetchone()
        if en:
            if old:
                conn.execute("UPDATE glossary_terms SET term=?, source='manual' WHERE id=?",
                             (en, old["id"]))
            else:
                conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, "
                             "term_kind, term_status, source) VALUES (?,?,?,?,?,?)",
                             (cid, en, "en", "synonym", "admitted", "manual"))
        elif old:
            conn.execute("DELETE FROM glossary_terms WHERE id=?", (old["id"],))
    # 规范词变更：同步术语层（旧首选词降级为同义词，保留可召回）
    if label != cur["pref_label"]:
        conn.execute("UPDATE glossary_terms SET term_kind='synonym', term_status='admitted' "
                     "WHERE concept_id=? AND term_kind='preferred'", (cid,))
        conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                     "term_status, source) VALUES (?,?,?,?,?,?)",
                     (cid, label, "zh", "preferred", "preferred", "manual"))
    conn.commit()
    _actor = getattr(user, "username", "") or "王工"
    _reason = (body.get("change_reason") or "").strip()
    # 留痕：状态流转单列一条（含理由），其余变更合并一条
    if status != cur["concept_status"]:
        _glog(conn, cid, "flow",
              f"状态流转 {cur['concept_status']} → {status}"
              + (f"（替代: {replaced_by}）" if status == 'deprecated' and replaced_by else ""),
              _actor, _reason)
    _chg = []
    if label != cur["pref_label"]:
        _chg.append(f"规范词 {cur['pref_label']}→{label}")
    if definition != (cur["definition"] or ""):
        _chg.append("定义更新")
    if (body.get("domain") or cur["domain"]) != cur["domain"]:
        _chg.append(f"域 {cur['domain']}→{body.get('domain')}")
    if body.get("english") is not None:
        _chg.append("英文对照更新")
    if body.get("maps_to_class", cur["maps_to_class"]) != cur["maps_to_class"]:
        _chg.append("本体映射更新")
    if _chg:
        _glog(conn, cid, "update", "；".join(_chg), _actor, _reason)
    conn.commit()
    audit(_actor, "concept_update", f"更新概念: {cid} → {status}", conn=conn)
    return {"ok": True}


@router.post("/api/glossary/concepts/{cid}/terms")
def term_add(cid: str, body: dict, conn=Depends(db_session)):
    """加术语（同义词/缩写/别名）。同形异义由前端 GET detail 的 homonym 标记提示。"""
    if not conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone():
        return JSONResponse({"error": "概念不存在"}, 404)
    term = (body.get("term") or "").strip()
    if not term:
        return JSONResponse({"error": "term 必填"}, 400)
    kind = body.get("term_kind") or "synonym"
    if kind not in ("preferred", "synonym", "hidden", "abbr", "alias"):
        return JSONResponse({"error": f"非法 term_kind: {kind}"}, 400)
    conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                 "term_status, source, reliability, note, part_of_speech, geography) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (cid, term, body.get("lang") or "zh", kind,
                  body.get("term_status") or "admitted", body.get("source") or "manual",
                  int(body.get("reliability") or 5),
                  (body.get("note") or "").strip(),
                  (body.get("part_of_speech") or "").strip(),
                  (body.get("geography") or "").strip()))
    conn.commit()
    _glog(conn, cid, "terms", f"加术语「{term}」（{kind}）", "王工")
    conn.commit()
    audit("王工", "term_add", f"概念 {cid} 加术语: {term}", conn=conn)
    return {"ok": True}


@router.post("/api/glossary/concepts/{cid}/terms/batch")
def term_batch_add(cid: str, body: dict, conn=Depends(db_session)):
    """批量加术语（R3，2026-09-07）：terms 支持数组或换行/分号分隔字符串。

    body: {terms: "a;b;c" | ["a","b"], term_kind, lang(留空=按中英文自动判定), source}
    返回 {added, skipped, dups:[与其他概念冲突的术语]}
    """
    if not conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone():
        return JSONResponse({"error": "概念不存在"}, 404)
    raw = body.get("terms")
    items = raw if isinstance(raw, list) else re.split(r"[;；\n\r,，]+", str(raw or ""))
    items = [x.strip() for x in items if x and x.strip()]
    if not items:
        return JSONResponse({"error": "terms 为空"}, 400)
    kind = body.get("term_kind") or "synonym"
    if kind not in ("preferred", "synonym", "hidden", "abbr", "alias"):
        return JSONResponse({"error": f"非法 term_kind: {kind}"}, 400)
    src = body.get("source") or "manual"
    added, skipped, dups = 0, 0, []
    for t in items:
        lang = body.get("lang") or ("zh" if any("一" <= ch <= "龥" for ch in t) else "en")
        cur = conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, "
                           "term_kind, term_status, source, reliability, note, part_of_speech, "
                           "geography) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (cid, t, lang, kind, "admitted", src, int(body.get("reliability") or 5),
                            (body.get("note") or "").strip(),
                            (body.get("part_of_speech") or "").strip(),
                            (body.get("geography") or "").strip()))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
        # 同形异义提示：该词已被其他概念占用
        others = conn.execute(
            "SELECT concept_id FROM glossary_terms WHERE LOWER(term)=LOWER(?) AND concept_id!=?",
            (t, cid)).fetchall()
        if others:
            dups.append({"term": t, "concepts": [r["concept_id"] for r in others]})
    conn.commit()
    audit("王工", "term_batch_add", f"概念 {cid} 批量加术语 {added} 条", conn=conn)
    return {"ok": True, "added": added, "skipped": skipped, "dups": dups}


@router.post("/api/glossary/concepts/merge")
def concept_merge(body: dict, conn=Depends(db_session)):
    """同形异义合并（R3，2026-09-07）：源概念术语并入目标概念，源转 deprecated + replaced_by。

    body: {source_cid, target_cid}
    规则：术语 INSERT OR IGNORE 迁到目标（lang/term_kind 保留）；源概念置 deprecated，
    replaced_by=目标；源的 maps_to_* 若目标为空则继承。返回迁移统计与影响面。
    """
    src_cid = (body.get("source_cid") or "").strip()
    tgt_cid = (body.get("target_cid") or "").strip()
    if not src_cid or not tgt_cid:
        return JSONResponse({"error": "source_cid 与 target_cid 必填"}, 400)
    if src_cid == tgt_cid:
        return JSONResponse({"error": "不能合并到自身"}, 400)
    src = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (src_cid,)).fetchone()
    tgt = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (tgt_cid,)).fetchone()
    if not src or not tgt:
        return JSONResponse({"error": "源或目标概念不存在"}, 404)
    src, tgt = dict(src), dict(tgt)
    moved, skipped = 0, 0
    for t in conn.execute("SELECT * FROM glossary_terms WHERE concept_id=?", (src_cid,)).fetchall():
        cur = conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, "
                           "term_kind, term_status, source, reliability) VALUES (?,?,?,?,?,?,?)",
                           (tgt_cid, t["term"], t["lang"],
                            "synonym" if t["term_kind"] == "preferred" else t["term_kind"],
                            "admitted" if t["term_kind"] == "preferred" else t["term_status"],
                            f"merge:{src_cid}", t["reliability"] or 5))
        moved += cur.rowcount or 0
        skipped += 0 if cur.rowcount else 1
    conn.execute("DELETE FROM glossary_terms WHERE concept_id=?", (src_cid,))
    # 目标缺映射时继承源的映射
    for col in ("maps_to_class", "maps_to_prop", "maps_to_inst"):
        if not (tgt.get(col) or "").strip() and (src.get(col) or "").strip():
            conn.execute(f"UPDATE glossary_concepts SET {col}=? WHERE concept_id=?",
                         (src[col], tgt_cid))
    conn.execute("UPDATE glossary_concepts SET concept_status='deprecated', replaced_by=?, "
                 "version=version+1, updated_at=CURRENT_TIMESTAMP WHERE concept_id=?",
                 (tgt_cid, src_cid))
    conn.commit()
    _glog(conn, src_cid, "merge", f"合并到「{tgt.get('pref_label') or tgt_cid}」（迁移术语 {moved} 条）", "王工")
    _glog(conn, tgt_cid, "merge", f"并入源概念 {src_cid}（迁移术语 {moved} 条）", "王工")
    conn.commit()
    audit("王工", "concept_merge",
          f"合并概念 {src_cid} → {tgt_cid}（迁移术语 {moved} 条）", conn=conn)
    return {"ok": True, "moved": moved, "skipped": skipped,
            "source_status": "deprecated", "source_replaced_by": tgt_cid}


@router.delete("/api/glossary/terms/{tid}")
def term_delete(tid: int, conn=Depends(db_session)):
    row = conn.execute("SELECT term, concept_id FROM glossary_terms WHERE id=?", (tid,)).fetchone()
    if not row:
        return JSONResponse({"error": "术语不存在"}, 404)
    conn.execute("DELETE FROM glossary_terms WHERE id=?", (tid,))
    _glog(conn, row["concept_id"], "terms", f"删术语「{row['term']}」", "王工")
    conn.commit()
    return {"ok": True}


@router.post("/api/glossary/sync-aliases")
def sync_aliases(conn=Depends(db_session)):
    """entity_aliases（写入缓冲）→ glossary_terms 同步。返回迁移条数。"""
    from migrate_p1_semantic import _loc_key
    n = 0
    aliases = conn.execute("SELECT entity_id, alias_name, source_type FROM entity_aliases "
                           "LIMIT 5000").fetchall()
    for a in aliases:
        ent = conn.execute("SELECT name FROM entities WHERE id=?", (a["entity_id"],)).fetchone()
        if not ent:
            continue
        cid = "I-" + _loc_key(ent["name"])
        conn.execute("INSERT OR IGNORE INTO glossary_concepts (concept_id, pref_label, "
                     "concept_status, maps_to_inst, created_by) VALUES (?,?,?,?, 'sync')",
                     (cid, ent["name"], "approved",
                      f"http://www.xingwang.mbse/ent/{a['entity_id']}"))
        cur = conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, "
                           "term_kind, term_status, source) VALUES (?,?,?,?,?,?)",
                           (cid, a["alias_name"], "zh", "alias", "admitted",
                            f"entity_aliases:{a['source_type']}"))
        n += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    return {"ok": True, "synced": n}


# ── 查询 Trace（P2-2）──
@router.get("/api/knowledge/trace")
def trace_list(limit: int = 20, query: Optional[str] = None, conn=Depends(db_session)):
    """查询全链路 Trace 列表（最新优先）。query 模糊过滤。"""
    sql = "SELECT * FROM query_trace"
    params = []
    if query:
        sql += " WHERE query LIKE ? OR normalized LIKE ?"
        params = [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["hit_docs"] = json.loads(d.get("hit_docs") or "[]")
        except Exception:
            d["hit_docs"] = []
        try:
            d["detail"] = json.loads(d.get("detail") or "{}")
        except Exception:
            d["detail"] = {}
        out.append(d)
    return {"items": out, "count": len(out)}


# ── domain review 队列（P2-3）──
@router.get("/api/knowledge/domain-reviews")
def domain_reviews(status: Optional[str] = "pending", limit: int = 50, conn=Depends(db_session)):
    """低置信度 domain 分类人工确认队列。status: pending | confirmed | corrected。"""
    sql = "SELECT * FROM domain_review_queue"
    params = []
    if status and status != "all":
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.post("/api/knowledge/domain-reviews/{qid}/review")
def domain_review(qid: int, body: DomainReviewIn, conn=Depends(db_session)):
    """人工确认/修正 domain 分类：confirm → 采纳 suggested_domain；correct → 覆盖新值。"""
    row = conn.execute("SELECT * FROM domain_review_queue WHERE id=?", (qid,)).fetchone()
    if not row:
        return JSONResponse({"error": "队列条目不存在"}, 404)
    final_domain = row["suggested_domain"] if body.action == "confirm" else body.corrected_domain
    if not final_domain:
        return JSONResponse({"error": "correct 时必须指定 corrected_domain"}, 400)
    conn.execute("UPDATE domain_review_queue SET status=?, reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                 ("confirmed" if body.action == "confirm" else "corrected", "王工", qid))
    # 回写 documents + document_chunks
    did = row["document_id"]
    if did:
        conn.execute("UPDATE documents SET domain=?, domain_confidence=0.95 WHERE id=?", (final_domain, did))
        conn.execute("UPDATE document_chunks SET domain=? WHERE document_id=?", (final_domain, did))
    conn.commit()
    audit("王工", "domain_review", f"域审核#{qid}: {row['suggested_domain']} → {final_domain}", conn=conn)
    return {"ok": True, "domain": final_domain}


# ── P0 治理闭环：变更历史 + 批量操作（2026-09-09，对标 PoolParty 工作流/批量 + TBX 变更控制） ──

@router.get("/api/glossary/changelog")
def glossary_changelog(concept_id: str = "", limit: int = 30, conn=Depends(db_session)):
    """概念变更历史（最新在前；concept_id 空=全词典最近动态）。"""
    if concept_id:
        rows = conn.execute(
            "SELECT * FROM glossary_changelog WHERE concept_id=? ORDER BY id DESC LIMIT ?",
            (concept_id, min(limit, 100))).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM glossary_changelog ORDER BY id DESC LIMIT ?",
            (min(limit, 100),)).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/glossary/concepts/batch")
def concepts_batch(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """概念批量操作（PoolParty bulk ops 对标）。

    body: {ids: [cid...], action: 'approve'|'flow'|'domain', target_status?, replaced_by?, domain?, reason?}
    规则与单条一致：approve 缺定义拦截；flow→deprecated 需 replaced_by。
    返回 {ok, done, skipped:[{cid, error}]}
    """
    from core.deps import current_user as _cu  # noqa: F401（显式依赖已生效）
    ids = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip()]
    action = (body.get("action") or "").strip()
    actor = getattr(user, "username", "") or "王工"
    if not ids:
        return JSONResponse({"error": "ids 必填"}, 400)
    done, skipped = 0, []
    if action == "approve":
        for cid in ids:
            row = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
            if not row:
                skipped.append({"cid": cid, "error": "不存在"}); continue
            if not (row["definition"] or "").strip():
                skipped.append({"cid": cid, "error": "缺定义（ISO 704 拦截）"}); continue
            conn.execute("UPDATE glossary_concepts SET concept_status='approved', "
                         "version=version+1, updated_at=CURRENT_TIMESTAMP WHERE concept_id=?", (cid,))
            _glog(conn, cid, "flow", f"状态流转 {row['concept_status']} → approved（批量）", actor,
                  (body.get("reason") or "").strip())
            done += 1
    elif action == "flow":
        tgt = (body.get("target_status") or "").strip()
        if tgt not in _CONCEPT_STATUSES:
            return JSONResponse({"error": f"非法目标状态: {tgt}"}, 400)
        rep = (body.get("replaced_by") or "").strip()
        for cid in ids:
            row = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
            if not row:
                skipped.append({"cid": cid, "error": "不存在"}); continue
            if tgt == "approved" and not (row["definition"] or "").strip():
                skipped.append({"cid": cid, "error": "缺定义（ISO 704 拦截）"}); continue
            if tgt == "deprecated" and not rep:
                skipped.append({"cid": cid, "error": "弃用需替代概念"}); continue
            conn.execute("UPDATE glossary_concepts SET concept_status=?, replaced_by=?, "
                         "version=version+1, updated_at=CURRENT_TIMESTAMP WHERE concept_id=?",
                         (tgt, rep if tgt == "deprecated" else row["replaced_by"], cid))
            _glog(conn, cid, "flow", f"状态流转 {row['concept_status']} → {tgt}（批量）", actor,
                  (body.get("reason") or "").strip())
            done += 1
    elif action == "domain":
        dom = (body.get("domain") or "").strip()
        if not dom:
            return JSONResponse({"error": "domain 必填"}, 400)
        for cid in ids:
            row = conn.execute("SELECT domain FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
            if not row:
                skipped.append({"cid": cid, "error": "不存在"}); continue
            conn.execute("UPDATE glossary_concepts SET domain=?, version=version+1, "
                         "updated_at=CURRENT_TIMESTAMP WHERE concept_id=?", (dom, cid))
            _glog(conn, cid, "update", f"域 {row['domain']}→{dom}（批量）", actor)
            done += 1
    else:
        return JSONResponse({"error": f"未知 action: {action}（approve/flow/domain）"}, 400)
    conn.commit()
    audit(actor, "glossary_batch", f"词典批量 {action}: 成功 {done} / 跳过 {len(skipped)}", conn=conn)
    return {"ok": True, "done": done, "skipped": skipped}


# ── P2-10：AI 建议流·发现池（对标 PoolParty Taxonomy Advisor；自动收集/人工触发 AI/审批入库） ──

_GENERIC_TERMS = {
    "id", "name", "label", "type", "kind", "value", "unit", "desc", "description",
    "note", "status", "state", "version", "stereotype", "ports", "props",
    "source", "target", "parent", "children", "test", "demo", "样例", "测试",
}


def _register_discovery(conn, term: str, context: str = "", source_ref: str = "") -> bool:
    """归一未命中 → 发现池登记（幂等：同词 freq+1；dismissed 不复活；通用词/过短过滤）。"""
    term = str(term or "").strip()
    if not term or len(term) < 2 or term.isdigit():
        return False
    if term.lower() in _GENERIC_TERMS:
        return False
    row = conn.execute("SELECT status, freq FROM glossary_discoveries WHERE term=?", (term,)).fetchone()
    if row:
        if row["status"] == "dismissed":
            return False   # 已忽略的词不再提示
        conn.execute("UPDATE glossary_discoveries SET freq=freq+1, context=?, source_ref=?, "
                     "updated_at=CURRENT_TIMESTAMP WHERE term=?",
                     ((context or "")[:300], (source_ref or "")[:120], term))
        return False
    conn.execute("INSERT INTO glossary_discoveries (term, context, source_ref) VALUES (?,?,?)",
                 (term, (context or "")[:300], (source_ref or "")[:120]))
    return True


@router.get("/api/glossary/discoveries")
def discoveries_list(status: str = "discovered", limit: int = 100, conn=Depends(db_session)):
    """AI 建议流·发现池列表（频次降序；status=discovered/adopted/dismissed/all）。"""
    if status and status != "all":
        rows = conn.execute("SELECT * FROM glossary_discoveries WHERE status=? "
                            "ORDER BY freq DESC, id DESC LIMIT ?", (status, min(limit, 300))).fetchall()
    else:
        rows = conn.execute("SELECT * FROM glossary_discoveries ORDER BY freq DESC, id DESC LIMIT ?",
                            (min(limit, 300),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["suggestion"] = json.loads(d.pop("suggestion_json") or "{}")
        except Exception:
            d["suggestion"] = {}
        out.append(d)
    return {"items": out, "count": len(out)}


@router.post("/api/glossary/discoveries/{did}/suggest")
def discovery_suggest(did: int, conn=Depends(db_session), user=Depends(current_user)):
    """人工触发 AI 预填：LLM 根据候选词+上下文+现有概念，生成规范词/定义/英文/上位/消歧预判。"""
    row = conn.execute("SELECT * FROM glossary_discoveries WHERE id=?", (did,)).fetchone()
    if not row:
        return JSONResponse({"error": "发现记录不存在"}, 404)
    term = row["term"]
    # 现有概念清单（消歧预判 + 上位词候选）
    existing = [(r["concept_id"], r["pref_label"], (r["definition"] or "")[:60])
                for r in conn.execute(
                    "SELECT concept_id, pref_label, definition FROM glossary_concepts "
                    "WHERE concept_status!='retired' ORDER BY pref_label").fetchall()]
    ex_lines = "\n".join(f"- {cid}：{label}（{d}）" for cid, label, d in existing[:60])
    prompt = (
        "你是 MBSE 术语治理助手。以下是建模中出现但词库未收录的候选词，请判断并给出词库建议。\n"
        f"候选词：{term}\n出现上下文：{row['context'] or '（无）'}\n"
        f"现有概念清单：\n{ex_lines or '（空）'}\n\n"
        '只输出一个 JSON 对象，格式：\n'
        '{"is_new_concept": true/false, "synonym_of": "若非新概念，给出其应归属的现有概念ID，否则空串", '
        '"pref_label": "规范词（如候选词是缩写/别名，给出中文规范全称；否则可沿用）", '
        '"definition": "一句完整定义（描述概念并区分于相邻概念，ISO 704）", '
        '"english": "英文对照", "abbr": ["缩写"], "broader": "上位概念的concept_id（从清单选，没有则空串）", '
        '"domain": "领域（简短）", "confidence": 0.0到1.0}\n不要输出任何其他文字。')
    try:
        from llm import llm_client
        # 显式选默认 chat provider（glossary_advisor 意图未绑定 Agent，不传会落 Mock）
        _pv = conn.execute("SELECT id FROM llm_providers WHERE is_default=1 AND status='active' "
                           "AND (model_type='chat' OR model_type IS NULL) ORDER BY priority DESC LIMIT 1").fetchone()
        resp = llm_client.chat(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": f"候选词：{term}"}],
            provider_id=(_pv["id"] if _pv else None),
            _intent="glossary_advisor")
        msg = (resp.get("choices") or [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip()
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            raise ValueError("LLM 未返回 JSON")
        sug = json.loads(m.group(0))
    except Exception as e:
        return JSONResponse({"error": f"AI 预填失败：{e}（可在采纳向导中手工填写）"}, 502)
    sug["is_new_concept"] = bool(sug.get("is_new_concept", True))
    conn.execute("UPDATE glossary_discoveries SET suggestion_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (json.dumps(sug, ensure_ascii=False), did))
    conn.commit()
    return {"ok": True, "suggestion": sug}


@router.post("/api/glossary/discoveries/{did}/adopt")
def discovery_adopt(did: int, body: dict = None, conn=Depends(db_session), user=Depends(current_user)):
    """采纳建议 → 创建概念（candidate，走审批看板）+ 首选术语 + 留痕 + 别名登记。"""
    row = conn.execute("SELECT * FROM glossary_discoveries WHERE id=?", (did,)).fetchone()
    if not row:
        return JSONResponse({"error": "发现记录不存在"}, 404)
    if row["status"] == "adopted":
        return JSONResponse({"error": "该发现已采纳"}, 400)
    body = body or {}
    try:
        sug = json.loads(row["suggestion_json"] or "{}")
    except Exception:
        sug = {}
    label = (body.get("pref_label") or sug.get("pref_label") or row["term"]).strip()
    from core.ns import loc_key as _lk
    cid = "C-" + _lk(label)
    if conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone():
        return JSONResponse({"error": f"概念已存在: {cid}（可直接将发现绑定到该概念）"}, 409)
    definition = (body.get("definition") if body.get("definition") is not None
                  else (sug.get("definition") or "")) .strip()
    conn.execute(
        "INSERT INTO glossary_concepts (concept_id, pref_label, definition, domain, concept_status, "
        "maps_to_class, maps_to_prop, maps_to_inst, created_by, intent, boost, context, note, source, "
        "broader, related) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, label, definition,
         (body.get("domain") or sug.get("domain") or "unknown").strip(),
         "candidate", body.get("maps_to_class") or "", "", "", actor := (getattr(user, "username", "") or "AI建议流"),
         "", 1.0, (sug.get("context") or row["context"] or "")[:200],
         f"来源：AI 建议流（发现频次 {row['freq']}）", row["source_ref"] or "ai_suggest",
         (body.get("broader") or sug.get("broader") or "").strip(), ""))
    conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                 "term_status, source) VALUES (?,?,?,?,?,?)",
                 (cid, label, "zh", "preferred", "preferred", "ai_suggest"))
    en = (body.get("english") if body.get("english") is not None else (sug.get("english") or "")).strip()
    if en:
        conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                     "term_status, source) VALUES (?,?,?,?,?,?)",
                     (cid, en, "en", "synonym", "admitted", "ai_suggest"))
    for a in (sug.get("abbr") or []):
        if str(a).strip():
            conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                         "term_status, source) VALUES (?,?,?,?,?,?)",
                         (cid, str(a).strip(), "en", "abbr", "admitted", "ai_suggest"))
    # 发现词与规范词不同 → 登记为同义词（保证下次归一命中）
    if row["term"] != label:
        conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                     "term_status, source) VALUES (?,?,?,?,?,?)",
                     (cid, row["term"], "zh", "synonym", "admitted", "ai_suggest"))
    conn.execute("UPDATE glossary_discoveries SET status='adopted', updated_at=CURRENT_TIMESTAMP WHERE id=?", (did,))
    _glog(conn, cid, "add", f"AI 建议采纳：新建概念「{label}」（来源发现频次 {row['freq']}）", actor)
    conn.commit()
    audit(actor, "glossary_adopt", f"采纳 AI 建议: {label} [{cid}]", conn=conn)
    return {"ok": True, "concept_id": cid, "pref_label": label,
            "hint": "已创建 candidate 概念——请在「⏳ 待审定」看板补充定义后批准" if not definition else "已创建 candidate 概念"}


@router.post("/api/glossary/discoveries/{did}/dismiss")
def discovery_dismiss(did: int, body: dict = None, conn=Depends(db_session), user=Depends(current_user)):
    """忽略发现（一次性实例名等）：不再提示。body.reason 可选。"""
    row = conn.execute("SELECT id FROM glossary_discoveries WHERE id=?", (did,)).fetchone()
    if not row:
        return JSONResponse({"error": "发现记录不存在"}, 404)
    reason = ((body or {}).get("reason") or "").strip()
    conn.execute("UPDATE glossary_discoveries SET status='dismissed', dismiss_reason=?, "
                 "updated_at=CURRENT_TIMESTAMP WHERE id=?", (reason[:200], did))
    conn.commit()
    return {"ok": True}


@router.delete("/api/glossary/concepts/{cid}")
def concept_delete(cid: str, conn=Depends(db_session), user=Depends(current_user)):
    """删除概念（左树节点删除入口，2026-09-09）。

    治理拦截：①存在下级概念（层级完整性）②被其他弃用概念指定为替代概念（召回链完整）。
    通过后级联清理术语与变更留痕；审批状态不设限（删除本身即强治理动作，留审计）。
    """
    row = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
    if not row:
        return JSONResponse({"error": "概念不存在"}, 404)
    kids = conn.execute("SELECT COUNT(*) FROM glossary_concepts WHERE broader=?", (cid,)).fetchone()[0]
    if kids:
        return JSONResponse({"error": f"存在 {kids} 个下级概念——请先删除它们或改挂其他上位概念"}, 422)
    rep = conn.execute("SELECT COUNT(*) FROM glossary_concepts WHERE replaced_by=?", (cid,)).fetchone()[0]
    if rep:
        return JSONResponse({"error": f"被 {rep} 个弃用概念指定为替代概念——请先修改它们的替代指向"}, 422)
    actor = getattr(user, "username", "") or "王工"
    conn.execute("DELETE FROM glossary_terms WHERE concept_id=?", (cid,))
    conn.execute("DELETE FROM glossary_changelog WHERE concept_id=?", (cid,))
    conn.execute("DELETE FROM glossary_concepts WHERE concept_id=?", (cid,))
    conn.commit()
    audit(actor, "concept_delete", f"删除概念: {row['pref_label']} [{cid}]", conn=conn)
    return {"ok": True, "deleted_terms": True}
