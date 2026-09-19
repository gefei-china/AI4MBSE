# -*- coding: utf-8 -*-
"""图谱工作区 v2：检索 / 三元组浏览 / 推理执行与物化 API（2026-09-09 P0）。

对齐行业知识图谱平台「检索 + 呈现 + 推理」标准能力：
- GET  /api/knowledge/graph/entity-search   实体自动补全（名称/ID/类型，搜索框数据源）
- GET  /api/knowledge/triples/browse        三元组原子表浏览（S/P/O + 状态 + 关键词 + 分页）
- POST /api/knowledge/reasoning/run         按需执行推理（5 项可勾选：分类/传递/对称/自反/一致性）
- POST /api/knowledge/reasoning/materialize 推断三元组物化入库（status='inferred'，幂等）
- GET  /api/knowledge/reasoning/inferred    已物化推断三元组列表（图谱叠加数据源）

推理内核复用 ontology_reasoning.py（零外部推理器）；物化写入 triples 原子表，
与既有审核/物化消费链（graph_stored）解耦——inferred 状态不进图库镜像，仅供查看与叠加。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from core.deps import db_session, current_user
from core.audit import audit
from core import ns

logger = logging.getLogger(__name__)

router = APIRouter(tags=["图谱工作区"])

REASONING_KINDS = ("classify", "transitive", "symmetric", "reflexive", "consistency")


# ── 检索：实体自动补全 ─────────────────────────────────────────
@router.get("/api/knowledge/graph/entity-search")
def graph_entity_search(q: str = "", branch: str = "", limit: int = 15,
                        conn=Depends(db_session), user=Depends(current_user)):
    """实体自动补全：名称 / ID / 类型模糊匹配，按相关度排序（前缀 > 包含）。"""
    q = (q or "").strip()
    if len(q) < 1:
        return {"ok": True, "items": []}
    limit = max(1, min(int(limit or 15), 50))
    like = f"%{q}%"
    params = [like, like, like]
    br_sql = ""
    if branch:
        br_sql = " AND branch IN (?, 'release')"
        params.append(branch)
    rows = conn.execute(
        f"SELECT id, name, entity_type, status, branch FROM entities "
        f"WHERE status!='deprecated' AND (name LIKE ? OR id LIKE ? OR entity_type LIKE ?)"
        f"{br_sql} LIMIT ?",
        (*params, limit * 4)).fetchall()
    ql = q.lower()

    def _score(r):
        n = (r["name"] or "").lower()
        if n == ql:
            return 0
        if n.startswith(ql):
            return 1
        return 2

    items = sorted((dict(r) for r in rows), key=_score)[:limit]
    return {"ok": True, "items": items, "count": len(items)}


# ── 三元组浏览 ────────────────────────────────────────────────
@router.get("/api/knowledge/triples/browse")
def triples_browse(status: str = "", q: str = "", predicate: str = "",
                   limit: int = 50, offset: int = 0,
                   conn=Depends(db_session), user=Depends(current_user)):
    """三元组原子表浏览：S/P/O 列表 + 状态筛选 + 关键词 + 谓词筛选 + 分页。"""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if status and status != "all":
        where.append("status=?")
        params.append(status)
    if predicate:
        where.append("predicate LIKE ?")
        params.append(f"%{predicate.strip()}%")
    if q:
        where.append("(subject_name LIKE ? OR object_value LIKE ? OR triple_id LIKE ?)")
        like = f"%{q.strip()}%"
        params += [like, like, like]
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM triples {wsql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM triples {wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset)).fetchall()
    # 状态分布（供筛选器徽标）
    dist = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM triples GROUP BY status").fetchall()}
    return {"ok": True, "rows": [dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset, "status_dist": dist}


# ── 推理：按需执行（全库图谱扫描，不落库）─────────────────────────
@router.post("/api/knowledge/reasoning/run")
def reasoning_run(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """执行勾选推理：分类 / 传递 / 对称 / 自反 / 一致性。返回 checks + inferred（不落库）。"""
    kinds = (body or {}).get("kinds") or list(REASONING_KINDS)
    kinds = [k for k in kinds if k in REASONING_KINDS]
    if not kinds:
        raise HTTPException(400, "至少勾选一项推理")
    # 2026-09-14：推理只针对当前分支数据（与图谱视图口径一致）；本体公理为全局 Schema 不受影响
    branch = ((body or {}).get("branch") or "").strip()
    scan_triples = None
    if branch:
        from ontology_reasoning import _load_all_triples as _lat
        scan_triples = _lat(conn, branch)
    from ontology_reasoning import (
        classify_instances, transitive_closure, symmetric_inference,
        reflexive_check, consistency_check, load_owl_axioms, _load_type_hierarchy, _ent_short)

    checks, inferred = [], []

    if "classify" in kinds:
        cls = classify_instances(conn, scan_triples)
        inferred += [{"kind": "classify", "s": c["s"], "p": "a",
                      "o": f"ex:{c['parent_type']}", "via": c["via"]} for c in cls]
        checks.append({"name": "分类推理", "pass": True,
                       "detail": f"已按子类链推断 {len(cls)} 条「子类实例亦属于父类」"
                                 + (f"（如 {_ent_short(cls[0]['s'])} ⇒ {cls[0]['parent_type']}）" if cls else " — 当前无子类实例")})

    if "transitive" in kinds:
        tr = transitive_closure(conn, scan_triples)
        inferred += [{"kind": "transitive", "s": t["s"], "p": t["p"], "o": t["o"], "path": t["path"]}
                     for t in tr]
        checks.append({"name": "传递推理", "pass": True,
                       "detail": f"已推断 {len(tr)} 条间接关系"
                                 + (f"（示例: {tr[0]['path']}）" if tr else " — 无可传递关系")})

    axioms = load_owl_axioms(conn) if ("symmetric" in kinds or "reflexive" in kinds) else {}

    if "symmetric" in kinds:
        sym = symmetric_inference(conn, scan_triples, axioms)
        inferred += [{"kind": "symmetric", "s": t["s"], "p": t["p"], "o": t["o"]} for t in sym]
        ax_n = len(axioms.get("symmetric") or set())
        checks.append({"name": "对称推理", "pass": True,
                       "detail": f"已推断 {len(sym)} 条反向关系"
                                 + (f"（{ax_n} 个对称公理）" if ax_n else " — 本体未标记对称公理")})

    if "reflexive" in kinds:
        rx = reflexive_check(conn, scan_triples, axioms)
        inferred += [{"kind": "reflexive", "s": t["s"], "p": t["p"], "o": t["o"]}
                     for t in (rx.get("inferred") or [])]
        checks.append({"name": "自反推理", "pass": True,
                       "detail": f"已补齐 {len(rx.get('inferred') or [])} 条自环关系"
                                 + (f"（{len(axioms.get('reflexive') or set())} 个自反公理）"
                                    if axioms.get("reflexive") else " — 本体未标记自反公理")})

    if "consistency" in kinds:
        checks += consistency_check(conn, scan_triples)
        # SHACL 全量形状校验（2026-09-14 补全）：手写巡检只覆盖 类型/必填/domain-range，
        # 取值枚举/唯一性/关系基数(min-max)/互斥 由 SHACL 形状（OntologyValidator.shacl_export
        # 从本体生成，与入库门禁同一套形状定义，避免两套校验逻辑漂移）+ pyshacl 引擎补齐。
        # 依赖 pyshacl/rdflib（.venv 已装）；shacl_gate_mode=off 或依赖缺失时优雅跳过。
        from ingest_gate import check_triples
        _shacl = check_triples(conn, record=False)
        if not _shacl.get("skipped"):
            viol = _shacl.get("violations") or []
            checks.append({
                "name": "SHACL 合规",
                "pass": bool(_shacl.get("conforms")),
                "detail": ("全部数据符合本体形状（必填/枚举/唯一/基数/互斥/domain-range）"
                           if _shacl.get("conforms")
                           else f"发现 {len(viol)} 条形状违规（含手写巡检未覆盖的枚举/基数/互斥项）"),
                "errors": [{"ent": v.get("subject") or "",
                            "text": f"{v.get('severity','Violation')}: {v.get('message','')}"
                                    + (f"（路径 {v['path']}）" if v.get('path') else "")}
                           for v in viol[:50]]})

    stats = {}
    for k in ("classify", "transitive", "symmetric", "reflexive"):
        stats[k] = sum(1 for t in inferred if t["kind"] == k)
    # 权威 id→显示名映射（2026-09-14 推理结果人读化）：前端解码 URI/编码 id 全靠它。
    # 覆盖：推断端点 + 传递路径中间节点 + 一致性检查错误项引用的实体。
    names = {}
    # URI 短 id → 原始实体 id 反查（2026-09-14 修「主语显示编号」：loc_key 把 id 里的
    # '-' 归一为 '_'，parse_ent_uri 解出的是归一化 id，按它查实体名必然落空 → 回退显示编号。
    # 反查到原始 id 后，names 同时收录归一化键与原始键，前端两种解码路径都能命中中文名）
    uri2raw = {}
    try:
        for _r in conn.execute("SELECT id FROM entities").fetchall():
            _raw = _r["id"]
            _loc = ns.local(ns.loc_key(_raw))
            uri2raw[ns.NS_ENT + _loc] = _raw
            uri2raw[_loc] = _raw
    except Exception:
        pass

    def _collect_name(x):
        if not x:
            return
        _, eid = ns.parse_ent_uri(str(x))
        eid = eid or str(x)
        raw = uri2raw.get(eid) or eid
        nm = _ent_name(conn, raw) or eid
        if raw not in names:
            names[raw] = nm
        if eid != raw and eid not in names:
            names[eid] = nm

    for t in inferred:
        _collect_name(t.get("s"))
        _collect_name(t.get("o"))
        for seg in str(t.get("path") or "").split("→"):
            if seg.strip():
                _collect_name(seg.strip())
    for c in checks:
        for e in (c.get("errors") or []):
            _collect_name(e.get("ent"))
    audit((user or {}).get("username", ""), "reasoning_run",
          f"kinds={','.join(kinds)} → inferred={len(inferred)}", branch=branch)
    from core import config as _cfg_dm
    return {"ok": True, "checks": checks, "inferred": inferred, "stats": stats, "names": names,
            "branch": branch,
            "direct_merge": _cfg_dm.as_bool("reasoning", "direct_merge", True)}


# ── 推断物化：写 triples 原子表（status='inferred'，幂等）──────────
def _ent_name(conn, eid: str) -> str:
    if not eid:
        return ""
    r = conn.execute("SELECT name FROM entities WHERE id=? LIMIT 1", (eid,)).fetchone()
    return r["name"] if r else ""


@router.post("/api/knowledge/reasoning/direct-merge")
def reasoning_direct_merge(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """简化模式（reasoning.direct_merge=true）：推断结果行级直接并入图库，跳过审核队列。

    复用 materialize → 批次 approve 链路：写暂存 → 生成批次 → 立即幂等并入当前分支；
    批次状态置 approved（审计留痕可追溯，必要时可按批次回查）。
    """
    items = (body or {}).get("inferred") or []
    if not items:
        raise HTTPException(400, "inferred 不能为空")
    branch = ((body or {}).get("branch") or "").strip()
    mat = reasoning_materialize({"inferred": items}, conn=conn, user=user)
    if mat.get("error"):
        raise HTTPException(400, mat["error"])
    cohort_id = mat.get("cohort_id") or 0
    merged = {"entities": [], "relations": []}
    if cohort_id:
        res = reasoning_cohort_approve(cohort_id, {"branch": branch}, conn=conn, user=user)
        merged = {"entities": res.get("entities") or [], "relations": res.get("relations") or []}
    audit((user or {}).get("username", ""), "reasoning_direct_merge",
          f"items={len(items)} written={mat.get('written', 0)} skipped={mat.get('skipped', 0)} "
          f"cohort={cohort_id} branch={branch}", branch=branch)
    return {"ok": True, "written": mat.get("written", 0), "skipped": mat.get("skipped", 0),
            "cohort_id": cohort_id, "branch": branch, **merged}


@router.post("/api/knowledge/reasoning/materialize")
def reasoning_materialize(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """把推理结果物化：①写 triples 原子表（status='inferred'，暂存）；②落一批次进入审核门禁（pending）。

    返回批次 id，前端凭其进入审核队列（approve 幂等并入图库 / reject 驳回）。
    """
    items = (body or {}).get("inferred") or []
    if not items:
        raise HTTPException(400, "inferred 不能为空")
    if len(items) > 5000:
        raise HTTPException(400, "单次物化上限 5000 条")
    from triple_store import triple_id
    who = (user or {}).get("display_name") or (user or {}).get("username") or "system"
    written, skipped = 0, 0
    records = []
    for t in items:
        kind = str(t.get("kind") or "")
        s_uri = str(t.get("s") or "")
        pred = str(t.get("p") or "")
        o_raw = str(t.get("o") or "")
        _, sid = ns.parse_ent_uri(s_uri)
        if not sid:
            skipped += 1
            continue
        sname = _ent_name(conn, sid) or sid
        if pred == "a" or kind == "classify":
            # 类型推断：(S, type, 父类型) —— object 为字面量（类型名）
            p_store = "type"
            o_type = o_raw[3:] if o_raw.startswith("ex:") else o_raw
            oid, oval, otype = "", ns.unlocal(o_type), "literal"
            item_kind, o_key = "entity", oval
        else:
            _, oid = ns.parse_ent_uri(o_raw)
            if not oid:
                skipped += 1
                continue
            p_store = pred
            oval, otype = (_ent_name(conn, oid) or oid), "entity"
            item_kind, o_key = "relation", oid
        key = triple_id(sname or sid, f"inferred:{p_store}", oval or oid)
        exists = conn.execute("SELECT id FROM triples WHERE triple_id=?", (key,)).fetchone()
        if exists:
            skipped += 1
            continue
        via = str(t.get("via") or t.get("path") or "")[:200]
        conn.execute(
            "INSERT INTO triples (triple_id, subject_id, subject_name, subject_type, predicate, "
            "object_id, object_value, object_type, confidence, status, source_doc, source_chunk, "
            "source_type, sysml_version_id, created_by, review_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, sid, sname, kind, f"inferred:{p_store}", oid or "", oval, otype,
             1.0, "inferred", "", "", "reasoning", 0, who, via))
        records.append(_cohort_rec(item_kind, sid, sname, p_store, o_key, oval, otype, kind, via))
        written += 1
    conn.commit()
    cohort_id = 0
    if records:
        # 2026-09-14 行级提交：append_pending=true 时追加到最近一个待审批次（避免单条提交刷出新批次）
        append_to = 0
        if (body or {}).get("append_pending"):
            row = conn.execute(
                "SELECT id FROM reasoning_cohorts WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
            append_to = row["id"] if row else 0
        if append_to:
            cohort_id = append_to
            for rec in records:
                conn.execute(
                    "INSERT INTO reasoning_cohort_items (cohort_id, item_kind, s, p, o, inferred_json) "
                    "VALUES (?,?,?,?,?,?)",
                    (cohort_id, rec["item_kind"], rec["s"], rec["p"], rec["o"],
                     json.dumps(rec, ensure_ascii=False)))
        else:
            cur = conn.execute(
                "INSERT INTO reasoning_cohorts (status, created_by) VALUES ('pending', ?)", (who,))
            cohort_id = cur.lastrowid
            for rec in records:
                conn.execute(
                    "INSERT INTO reasoning_cohort_items (cohort_id, item_kind, s, p, o, inferred_json) "
                    "VALUES (?,?,?,?,?,?)",
                    (cohort_id, rec["item_kind"], rec["s"], rec["p"], rec["o"],
                     json.dumps(rec, ensure_ascii=False)))
        conn.commit()
    audit((user or {}).get("username", ""), "reasoning_materialize",
          f"written={written} skipped={skipped} cohort={cohort_id}")
    return {"ok": True, "written": written, "skipped": skipped,
            "cohort_id": cohort_id, "queued": 1 if records else 0}


def _cohort_rec(item_kind, sid, sname, p_store, o_key, oval, otype, kind, via):
    """归一化一条批次条目（供 reasoning_cohort_items.inferred_json 快照）。"""
    return {"item_kind": item_kind, "s": sid, "s_name": sname, "p": p_store,
            "o": o_key, "o_name": (oval or "") if otype == "entity" else "",
            "o_name_lit": (oval or "") if otype == "literal" else "",
            "kind": kind, "via": via or ""}


def _parse_stat(stat):
    try:
        d = json.loads(stat or "{}") or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_cohort(conn, cohort_id):
    row = conn.execute("SELECT * FROM reasoning_cohorts WHERE id=?", (cohort_id,)).fetchone()
    if not row:
        raise HTTPException(404, "批次不存在")
    return dict(row)


def _cohort_default_branch(conn):
    """推理批次并入（approve）时的落库分支：settings.default_branch，**必须校验分支真实存在**。

    2026-09-20：该键实测曾被改成 `dev/test` —— 一个 branches 表里**不存在**的分支
    （来源：tests/test_api.py 的黑盒 PUT 未还原，updated_at=2026-09-10 09:33:12）。
    而本函数原先只判空、不判存在 → 并入会把实体写进分支表没有的分支，
    结果在分支列表 / 图谱 / 各统计里**全部看不到**（只能靠 SQL 捞），且无任何报错。
    现收紧：值缺失或不在 branches 表中 → 回落 `personal`（写入侧约定的兜底分支，
    与 agent/pipeline_parts/tools.py 的写入工具默认值一致）。
    """
    row = conn.execute("SELECT value FROM settings WHERE key='default_branch'").fetchone()
    v = ((row["value"] if row else "") or "").strip()
    if not v or not conn.execute("SELECT 1 FROM branches WHERE name=?", (v,)).fetchone():
        return "personal"
    return v


def _existing_ent_type(conn, eid):
    row = conn.execute(
        "SELECT entity_type FROM entities WHERE id=? AND status!='deprecated' LIMIT 1", (eid,)).fetchone()
    return row["entity_type"] if row else ""


# ── 推理审核门禁：批次列表 / 明细 / 确认并入 / 驳回 ────────────────
@router.get("/api/knowledge/reasoning/cohorts")
def reasoning_cohort_list(conn=Depends(db_session), user=Depends(current_user)):
    """推理物化批次列表（各批 状态 / 推断数 / 并入实体·关系数 / 创建人 / 时间）。"""
    rows = conn.execute(
        "SELECT id, status, note, stat, created_by, decided_by, decided_at, created_at "
        "FROM reasoning_cohorts ORDER BY id DESC LIMIT 200").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        st = _parse_stat(d.get("stat"))
        d["item_count"] = conn.execute(
            "SELECT COUNT(*) FROM reasoning_cohort_items WHERE cohort_id=?", (d["id"],)).fetchone()[0]
        d["entity_count"] = st.get("entity_count", 0)
        d["relation_count"] = st.get("relation_count", 0)
        out.append(d)
    return {"ok": True, "rows": out}


@router.get("/api/knowledge/reasoning/cohorts/{cohort_id}")
def reasoning_cohort_detail(cohort_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """批次详情：「本次并入」清单 + 批次条目（含行级审核状态，供审核队列单条操作）。"""
    cohort = _load_cohort(conn, cohort_id)
    st = _parse_stat(cohort["stat"])
    items = conn.execute(
        "SELECT id, item_kind, s, p, o, status, note, inferred_json "
        "FROM reasoning_cohort_items WHERE cohort_id=? ORDER BY id", (cohort_id,)).fetchall()
    out_items = []
    for it in items:
        d = dict(it)
        info = _parse_stat(d.pop("inferred_json", "{}"))
        d["s_name"] = (info.get("s_name") or "") or d["s"]
        d["o_name"] = (info.get("o_name") or info.get("o_name_lit") or "") or d["o"]
        d["kind"] = info.get("kind") or ""
        d["via"] = info.get("via") or ""
        d["status"] = d.get("status") or "pending"
        out_items.append(d)
    return {"ok": True, "cohort": cohort,
            "entities": st.get("entities") or [], "relations": st.get("relations") or [],
            "entity_count": st.get("entity_count", 0), "relation_count": st.get("relation_count", 0),
            "items": out_items}


@router.post("/api/knowledge/reasoning/cohorts/{cohort_id}/items/{item_id}/approve")
def reasoning_cohort_item_approve(cohort_id: int, item_id: int, body: dict = None,
                                  conn=Depends(db_session), user=Depends(current_user)):
    """单条并入：把该条推断幂等落入图库，条目置 approved，并入明细追加到批次 stat。

    批次已驳回时也允许单条 rescue（行级决策优先于批次决策）；
    条目已处理（approved/rejected）返回幂等。
    """
    cohort = _load_cohort(conn, cohort_id)
    row = conn.execute(
        "SELECT * FROM reasoning_cohort_items WHERE id=? AND cohort_id=?", (item_id, cohort_id)).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")
    it = dict(row)
    if (it.get("status") or "pending") == "approved":
        return {"ok": True, "idempotent": True}
    if (it.get("status") or "pending") == "rejected":
        raise HTTPException(409, "该条目已忽略，如需并入请先在批次中重新提交")
    branch = ((body or {}).get("branch") or "").strip() or _cohort_default_branch(conn)
    who = (user or {}).get("display_name") or (user or {}).get("username") or "system"
    created_entities, created_relations, merged_ids = _merge_cohort_items(conn, branch, who, [it])
    conn.execute("UPDATE reasoning_cohort_items SET status='approved' WHERE id=?", (item_id,))
    # 并入明细追加到批次 stat（批次后续 approve 时其余条目仍可批量并入）
    st = _parse_stat(cohort["stat"])
    st.setdefault("entities", []).extend(created_entities)
    st.setdefault("relations", []).extend(created_relations)
    st["entity_count"] = len(st.get("entities") or [])
    st["relation_count"] = len(st.get("relations") or [])
    conn.execute("UPDATE reasoning_cohorts SET stat=? WHERE id=?",
                 (json.dumps(st, ensure_ascii=False), cohort_id))
    # 条目全部已决 → 批次状态自动收敛 approved（与单条忽略路径对称）
    left = conn.execute(
        "SELECT COUNT(*) FROM reasoning_cohort_items WHERE cohort_id=? "
        "AND (status IS NULL OR status='pending')", (cohort_id,)).fetchone()[0]
    if cohort["status"] == "pending" and left == 0:
        conn.execute(
            "UPDATE reasoning_cohorts SET status='approved', decided_by=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (who, cohort_id))
    conn.commit()
    _sync_triples_status(conn, merged_ids, "merged")
    conn.commit()
    audit((user or {}).get("username", ""), "reasoning_cohort_item_approve",
          f"cohort={cohort_id} item={item_id} entities={len(created_entities)} relations={len(created_relations)} branch={branch}", branch=branch)
    return {"ok": True, "idempotent": False, "branch": branch,
            "entities": created_entities, "relations": created_relations}


@router.post("/api/knowledge/reasoning/cohorts/{cohort_id}/items/{item_id}/reject")
def reasoning_cohort_item_reject(cohort_id: int, item_id: int, body: dict = None,
                                 conn=Depends(db_session), user=Depends(current_user)):
    """单条忽略：条目置 rejected（不再随批次并入图库）。轻量操作，原因可选填。"""
    cohort = _load_cohort(conn, cohort_id)
    row = conn.execute(
        "SELECT status FROM reasoning_cohort_items WHERE id=? AND cohort_id=?", (item_id, cohort_id)).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")
    if (row["status"] or "pending") == "rejected":
        return {"ok": True, "idempotent": True}
    if (row["status"] or "pending") == "approved":
        raise HTTPException(409, "该条目已并入图库，无法忽略")
    note = ((body or {}).get("note") or "").strip()[:200]
    who = (user or {}).get("display_name") or (user or {}).get("username") or "system"
    conn.execute("UPDATE reasoning_cohort_items SET status='rejected', note=? WHERE id=?", (note, item_id))
    # 批次内条目全部已决 → 批次状态自动收敛（有并入→approved，全忽略→rejected）
    left = conn.execute(
        "SELECT COUNT(*) FROM reasoning_cohort_items WHERE cohort_id=? "
        "AND (status IS NULL OR status='pending')", (cohort_id,)).fetchone()[0]
    if cohort["status"] == "pending" and left == 0:
        any_approved = conn.execute(
            "SELECT COUNT(*) FROM reasoning_cohort_items WHERE cohort_id=? AND status='approved'",
            (cohort_id,)).fetchone()[0]
        conn.execute(
            "UPDATE reasoning_cohorts SET status=?, decided_by=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
            ("approved" if any_approved else "rejected", who, cohort_id))
    conn.commit()
    _sync_triples_status(conn, [item_id], "rejected")
    conn.commit()
    audit((user or {}).get("username", ""), "reasoning_cohort_item_reject",
          f"cohort={cohort_id} item={item_id}")
    return {"ok": True}


def _merge_cohort_items(conn, branch: str, who: str, items) -> tuple:
    """把批次条目幂等落入图库（补缺实体 + 写关系）——批次 approve 与单条 item approve 共用。

    返回 (created_entities, created_relations, merged_item_ids)。
    幂等：按 (source_id,target_id,relation_type,branch) 与 (id,branch) 去重。
    """
    # 1) 收拢待补实体（主体 + 关系型宾语；分类推断主体类型 = 宾语父类型）
    pending_ents = {}   # id -> {"name","etype"}
    rels = []           # {"source_id","source_name","p","target_id","target_name"}
    merged_ids = []
    for it in items:
        d = dict(it)
        info = _parse_stat(d.get("inferred_json"))
        s_id, p = d["s"], d["p"]
        s_name = (info.get("s_name") or "") or s_id
        item_kind = d["item_kind"] or info.get("item_kind")
        if item_kind == "entity":
            pending_ents.setdefault(s_id, {"name": s_name, "etype": (d["o"] or "实体")})
        else:
            o_id, o_name = d["o"], ((info.get("o_name") or "") or d["o"])
            pending_ents.setdefault(s_id, {"name": s_name, "etype": ""})
            pending_ents.setdefault(o_id, {"name": o_name, "etype": ""})
            rels.append({"source_id": s_id, "source_name": s_name, "p": p,
                         "target_id": o_id, "target_name": o_name})
        merged_ids.append(d["id"])

    # 2) 幂等补实体（仅当前分支缺失时插入，status='reviewed' → 并入图即显示）
    created_entities = []
    for eid, meta in pending_ents.items():
        if conn.execute("SELECT 1 FROM entities WHERE id=? AND branch=?",
                        (eid, branch)).fetchone():
            continue
        etype = (meta["etype"] or "") or _existing_ent_type(conn, eid) or "实体"
        name = (meta["name"] or "") or eid
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, properties, status, branch, source_type, "
            "created_by, reviewed_by, reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (eid, name, etype, "{}", "reviewed", branch, "reasoning", who, who))
        created_entities.append({"id": eid, "name": name, "entity_type": etype})

    # 3) 幂等写关系
    created_relations = []
    for r in rels:
        if conn.execute(
                "SELECT 1 FROM relations WHERE source_id=? AND target_id=? AND relation_type=? AND branch=?",
                (r["source_id"], r["target_id"], r["p"], branch)).fetchone():
            continue
        conn.execute(
            "INSERT INTO relations (source_id, target_id, relation_type, status, branch, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (r["source_id"], r["target_id"], r["p"], "reviewed", branch, who))
        created_relations.append({"source_id": r["source_id"], "source": r["source_name"],
                                  "name": r["p"], "predicate": r["p"],
                                  "target_id": r["target_id"], "target": r["target_name"]})
    return created_entities, created_relations, merged_ids


def _sync_triples_status(conn, item_ids: list, status: str) -> None:
    """best-effort 同步 triples 暂存行状态（triples 与批次表解耦，按 triple_id 反查）。

    materialize 时 triple_id = triple_id(s_name, "inferred:{p}", o_value)；
    approve → 'merged'（不再出现在待审列表），reject → 'rejected'。
    """
    from triple_store import triple_id
    for iid in item_ids:
        row = conn.execute(
            "SELECT item_kind, s, p, o, inferred_json FROM reasoning_cohort_items WHERE id=?",
            (iid,)).fetchone()
        if not row:
            continue
        info = _parse_stat(row["inferred_json"])
        s_name = (info.get("s_name") or "") or row["s"]
        if (row["item_kind"] or info.get("item_kind")) == "entity":
            oval = info.get("o_name_lit") or row["o"] or ""
        else:
            oval = (info.get("o_name") or "") or row["o"] or ""
        try:
            key = triple_id(s_name, f"inferred:{row['p']}", oval)
            conn.execute("UPDATE triples SET status=? WHERE triple_id=?", (status, key))
        except Exception:
            continue  # best-effort，不阻塞审核主流程


@router.post("/api/knowledge/reasoning/cohorts/{cohort_id}/approve")
def reasoning_cohort_approve(cohort_id: int, body: dict = None,
                             conn=Depends(db_session), user=Depends(current_user)):
    """确认并入：把该批待审推断幂等落入图库（补缺实体 + 写关系），状态置 approved。

    幂等：按 (source_id,target_id,relation_type,branch) 与 (id,branch) 去重；
    重复 approve 不再重复建实体/关系，直接返回既有并入清单。
    单条驳回过的条目不并入（2026-09-14 行级审核）。
    """
    cohort = _load_cohort(conn, cohort_id)
    if cohort["status"] == "approved":
        st = _parse_stat(cohort["stat"])
        return {"ok": True, "idempotent": True, "branch": "",
                "entities": st.get("entities") or [], "relations": st.get("relations") or [],
                "entity_count": st.get("entity_count", 0),
                "relation_count": st.get("relation_count", 0)}
    if cohort["status"] == "rejected":
        raise HTTPException(409, "该批次已驳回，无法确认并入")
    branch = ((body or {}).get("branch") or "").strip() or _cohort_default_branch(conn)
    who = (user or {}).get("display_name") or (user or {}).get("username") or "system"

    items = conn.execute(
        "SELECT * FROM reasoning_cohort_items WHERE cohort_id=? "
        "AND (status IS NULL OR status!='rejected') ORDER BY id", (cohort_id,)).fetchall()

    created_entities, created_relations, merged_ids = _merge_cohort_items(conn, branch, who, items)

    stat = {"entity_count": len(created_entities), "relation_count": len(created_relations),
            "entities": created_entities, "relations": created_relations}
    conn.execute(
        "UPDATE reasoning_cohorts SET status='approved', stat=?, decided_by=?, decided_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (json.dumps(stat, ensure_ascii=False), who, cohort_id))
    if merged_ids:
        ph = ",".join("?" * len(merged_ids))
        conn.execute(f"UPDATE reasoning_cohort_items SET status='approved' WHERE id IN ({ph})", merged_ids)
    conn.commit()
    _sync_triples_status(conn, merged_ids, "merged")
    conn.commit()
    audit((user or {}).get("username", ""), "reasoning_cohort_approve",
          f"cohort={cohort_id} entities={len(created_entities)} relations={len(created_relations)} branch={branch}", branch=branch)
    return {"ok": True, "idempotent": False, "branch": branch,
            "entities": created_entities, "relations": created_relations,
            "entity_count": len(created_entities), "relation_count": len(created_relations)}


@router.post("/api/knowledge/reasoning/cohorts/{cohort_id}/reject")
def reasoning_cohort_reject(cohort_id: int, body: dict = None,
                            conn=Depends(db_session), user=Depends(current_user)):
    """驳回：body 带 note（≥2 字），状态 rejected，被拒项不再落图。"""
    cohort = _load_cohort(conn, cohort_id)
    if cohort["status"] == "rejected":
        return {"ok": True, "idempotent": True}
    if cohort["status"] == "approved":
        raise HTTPException(409, "该批次已确认并入，无法驳回")
    note = ((body or {}).get("note") or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "驳回原因至少 2 个字")
    who = (user or {}).get("display_name") or (user or {}).get("username") or "system"
    conn.execute(
        "UPDATE reasoning_cohorts SET status='rejected', note=?, decided_by=?, decided_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (note, who, cohort_id))
    conn.commit()
    audit((user or {}).get("username", ""), "reasoning_cohort_reject",
          f"cohort={cohort_id} note={note}")
    return {"ok": True}


@router.get("/api/knowledge/reasoning/inferred")
def reasoning_inferred_list(limit: int = 500,
                            conn=Depends(db_session), user=Depends(current_user)):
    """已物化推断三元组（status='inferred'）——图谱叠加 / 三元组 Tab 数据源。"""
    limit = max(1, min(int(limit or 500), 2000))
    rows = conn.execute(
        "SELECT * FROM triples WHERE status='inferred' ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kind"] = d.get("subject_type") or ""          # 物化时把推理类型写在 subject_type
        d["predicate_raw"] = (d.get("predicate") or "").replace("inferred:", "", 1)
        out.append(d)
    return {"ok": True, "rows": out, "count": len(out)}


# ── P0：节点/边元数据一键追溯（FR-KG-11） ────────────────────

@router.get("/api/entities/{entity_id}/provenance")
def entity_provenance(entity_id: str, conn=Depends(db_session)):
    """节点数据来源一键追溯（5 段视图：实体/文档/切片/版本/审计 + 同源 + 废弃轨迹）。

    返回结构：{
      entity, source_document, source_chunks[≤3], version_chain[],
      audit_trail[≤20], siblings[≤20], deprecated_trace|null
    }
    """
    from services.graph_provenance import GraphProvenanceService
    result = GraphProvenanceService(conn).entity_provenance(entity_id)
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(result, 404)
    return result


@router.get("/api/relations/{relation_id}/provenance")
def relation_provenance(relation_id: int, conn=Depends(db_session)):
    """边数据来源一键追溯（在节点追溯基础上 + source/target 节点引用）。

    返回结构：{
      relation, source_entity, target_entity,
      source_document, source_chunks, version_chain,
      audit_trail, deprecated_trace|null
    }
    """
    from services.graph_provenance import GraphProvenanceService
    result = GraphProvenanceService(conn).relation_provenance(relation_id)
    if "error" in result:
        from fastapi.responses import JSONResponse
        return JSONResponse(result, 404)
    return result
