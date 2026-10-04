# -*- coding: utf-8 -*-
"""知识库路由分片：实例抽取/V2G/三元组/摄取/SysML/Profile。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.knowledge_parts.shared import *


@router.post("/api/knowledge/instances/preview")
def instances_preview(body: dict, conn=Depends(db_session)):
    """根据 source 生成 Turtle 三元组 + 推理验证报告（不入库）。
    source: r2rml | csv | manual | nlp
    """
    import json as _json
    src = body.get("source", "r2rml")
    triples = []     # [(s, p, o)]  主语-谓词-宾语
    inst_count = 0
    rel_count = 0
    from core import ns as _ns
    # P0-1：实例命名空间统一到 core.ns（原来是第三套 inst#，与本体/图库都不通）
    BASE_T = _ns.NS_ENT

    def _local(name):
        return _ns.local(_ns.loc_key(name))

    def _uri(name):
        return BASE_T + _local(name)

    if src == "r2rml":
        # R2RML 简化映射：entities → 实例三元组；relations → 关系三元组
        ents = [dict(r) for r in conn.execute("SELECT * FROM entities WHERE status!='deprecated'").fetchall()]
        rels = [dict(r) for r in conn.execute("SELECT * FROM relations WHERE status!='deprecated'").fetchall()]
        for e in ents:
            uri = _uri(e["id"])
            et = e.get("entity_type") or ""
            if et:
                triples.append((uri, "a", f"ex:{_local(et)}"))
            props = {}
            try:
                props = _json.loads(e.get("properties") or "{}")
            except Exception:
                pass
            for k, v in (props or {}).items():
                triples.append((uri, f"ex:{_local(k)}", str(v)))
            inst_count += 1
        for r in rels:
            uri_s = _uri(r["source_id"])
            uri_t = _uri(r["target_id"])
            rt = r.get("relation_type") or "relatedTo"
            triples.append((uri_s, f"ex:{_local(rt)}", uri_t))
            rel_count += 1
    elif src == "csv":
        # CSV 表头映射到本体属性：每行一个实例
        import csv as _csv, io as _io
        text = body.get("csv", "")
        reader = _csv.DictReader(_io.StringIO(text))
        rows = [r for r in reader if r and any(v.strip() for v in r.values() if v)]
        # 实体类型：显式 entity_type 列，否则第一行类型
        known_ents = {r["name"] for r in conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
        for row in rows:
            row_id = row.get("id") or row.get("name") or f"IMP-{inst_count+1}"
            uri = _uri(row_id)
            et = row.get("entity_type") or row.get("type") or ""
            if et and et in known_ents:
                triples.append((uri, "a", f"ex:{_local(et)}"))
            else:
                triples.append((uri, "a", "ex:Thing"))
            for k, v in row.items():
                if not v or k in ("id", "entity_type", "type"):
                    continue
                triples.append((uri, f"ex:{_local(k)}", str(v)))
            inst_count += 1
    elif src == "manual":
        items = body.get("items", [])
        if isinstance(items, str):
            try: items = _json.loads(items)
            except Exception: items = []
        known_ents = {r["name"] for r in conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
        for it in items:
            uri = _uri(it.get("id") or f"MAN-{inst_count+1}")
            et = it.get("entity_type") or ""
            if et and et in known_ents:
                triples.append((uri, "a", f"ex:{_local(et)}"))
            else:
                triples.append((uri, "a", "ex:Thing"))
            for k, v in (it.get("properties") or {}).items():
                triples.append((uri, f"ex:{_local(k)}", str(v)))
            inst_count += 1
    elif src == "nlp":
        # NLP 简化抽取：基于 ontology 类型词在文本中匹配 + 上下文
        text = body.get("text", "")
        known_ents = [r["name"] for r in conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()]
        # 类型名按长度倒序，避免短词先匹配
        known_ents.sort(key=len, reverse=True)
        matched = set()
        for et in known_ents:
            if et in text and et not in matched:
                matched.add(et)
                uri = _uri(f"NLP-{_local(et)}")
                triples.append((uri, "a", f"ex:{_local(et)}"))
                snippet = text[:60].replace('"', '\\"')
                triples.append((uri, "ex:inferredFrom", f'"{snippet}..."'))
                inst_count += 1
    else:
        return JSONResponse({"error": f"unknown source: {src}"}, 400)

    # 生成 Turtle：按主语分组；主/谓/宾 URI 用 ex: 前缀；宾语字符串用引号
    lines = []
    lines.append("@prefix ex: <http://www.xingwang.mbse/inst#> .")
    lines.append("@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .")
    lines.append("@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .")
    lines.append("")
    from collections import defaultdict
    by_s = defaultdict(list)
    for s, p, o in triples:
        by_s[s].append((p, o))

    def _t(obj, is_subject=False):
        """把对象格式化为 Turtle 字面量/URI。
        is_subject=True 时只输出短前缀（主语作为块头）。"""
        u = str(obj)
        if u.startswith(BASE_T):
            short = u[len(BASE_T):]  # 截掉 BASE 前缀
            return "ex:" + short
        if u.startswith("ex:"):
            return u
        # 普通字符串 → 引号 + 转义
        return '"' + u.replace('\\', '\\\\').replace('"', '\\"') + '"'

    for s, plist in by_s.items():
        s_label = _t(s).replace('ex:', '')  # 主语短名
        # 主语行：包含第一个谓-宾
        p0, o0 = plist[0]
        if p0 == "a":
            lines.append(f"ex:{s_label} a {_t(o0)} ;")
        else:
            lines.append(f"ex:{s_label} {_t(p0)} {_t(o0)} ;")
        # 后续行
        for p, o in plist[1:]:
            if p == "a":
                continue
            lines.append(f"    {_t(p)} {_t(o)} ;")
        # 最后行换分号为句点
        if lines[-1].endswith(" ;"):
            lines[-1] = lines[-1][:-2] + " ."
        else:
            lines.append(" .")
    turtle = "\n".join(lines)

    # P0-2：可执行推理（分类/传递/一致性，见 ontology_reasoning.py）
    from ontology_reasoning import run_reasoning
    rr = run_reasoning(conn, triples, inst_count, rel_count)
    return {
        "source": src,
        "turtle": turtle,
        "stats": {"triples": len(triples), "instances": inst_count, "relations": rel_count},
        "reasoning": rr["checks"],
        "inferred": rr["inferred"],
    }


@router.post("/api/knowledge/graph/bulk")
def graph_bulk_create(body: dict, conn=Depends(db_session),
                      user=Depends(require_any_permission(WRITE_PERMS))):
    """KB-P4：批量实例化（SysML 导入 / LLM 抽取入图谱），逐项过本体校验。

    body = {nodes:[{id,name,entity_type,properties}], edges:[{source_id,target_id,relation_type,props}]}
    """
    from ontology_semantics import GraphStore
    result = GraphStore(conn).bulk_create(
        body.get("nodes", []), body.get("edges", []), body.get("branch", "dev"))
    audit(_actor(user), "graph_bulk", f"图谱批量实例化: 成功 {result['created_count']} / 拒绝 {result['rejected_count']}", conn=conn)
    return result


@router.post("/api/knowledge/v2g/extract")
def v2g_extract(body: V2GExtractIn, conn=Depends(db_session), user=Depends(current_user),
                mode: str = ""):
    """O-1 Step1：按查询命中 chunks 抽取候选实体/关系（LLM 受本体 schema 约束）。

    doc_id 指定时限定在该文档的 chunks 内抽取（上传后自动抽取 → chunk↔entity 溯源指向本文档）。
    P1-1 编排已上提 Service。

    `mode=async`（P0-C）：LLM 抽取为十秒~分钟级，可提交后台作业立即返回 job_id。
    """
    from services.knowledge_service import KnowledgeService
    if (mode or "").strip().lower() in ("async", "1", "true", "yes"):
        import hashlib
        from core import job_queue as jq
        _job = jq.submit(conn, "v2g_extract",
                         {"query": body.query, "chunk_ids": body.chunk_ids,
                          "top_k": body.top_k, "doc_id": body.doc_id,
                          "actor": _actor(user)},
                         job_key="v2g_extract:%s" % (hashlib.md5(
                             (body.query or "").encode("utf-8")).hexdigest()[:12]),
                         reuse_terminal=False)
        return {"ok": True, "async": True, "job_id": _job.get("job_id"),
                "job_status": _job.get("status"), "deduped": _job.get("deduped", False),
                "poll": "/api/jobs/%d" % int(_job.get("job_id") or 0)}
    return KnowledgeService(conn).v2g_extract(
        body.query, body.chunk_ids, body.top_k, body.doc_id, actor=_actor(user))


@router.get("/api/knowledge/v2g/candidates")
def v2g_candidates(batch_id: Optional[str] = None, limit: int = 100,
                   page: int = 0, conn=Depends(db_session)):
    """待审候选列表（附带来源 chunk 原文，治理中心溯源面板用，chunk_content 截断 300 字）。

    P3 容量管理：支持分页——page>0 时返回 {items,total,page,limit}（按需加载），
    page=0 保持返回数组（兼容既有调用）。
    """
    from vector2graph import list_candidates
    offset = (page - 1) * limit if page > 0 else 0
    items, total = list_candidates(conn, batch_id, limit, offset)
    for c in items:
        c["chunk_content"] = ""
        if c.get("chunk_id"):
            row = conn.execute(
                "SELECT content FROM document_chunks WHERE id=?", (c["chunk_id"],)).fetchone()
            if row:
                c["chunk_content"] = (row["content"] or "")[:300]
    if page > 0:
        return {"items": items, "total": total, "page": page, "limit": limit}
    return items


@router.post("/api/knowledge/v2g/batches/{batch_id}/clear")
def v2g_batch_clear(batch_id: str, conn=Depends(db_session), user=Depends(current_user)):
    """P3 容量管理：清理批次——仅允许无待审候选的批次（删除候选记录，已入库实体/溯源不受影响）。"""
    from vector2graph import clear_batch
    result = clear_batch(conn, batch_id)
    if result["skipped"] > 0:
        return JSONResponse({"error": f"该批次仍有 {result['skipped']} 条待审候选，请先处理后再清理"}, 400)
    audit(_actor(user), "v2g_batch_clear", f"清理抽取批次 {batch_id}: 删除 {result['deleted']} 条候选", conn=conn)
    return {"ok": True, "deleted": result["deleted"]}


@router.get("/api/knowledge/v2g/batches")
def v2g_batches(conn=Depends(db_session)):
    """抽取批次聚合（治理中心批次卡：batch 维度统计 + 来源文档 + 时间）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_batches()


@router.get("/api/knowledge/reflow/stats")
def reflow_stats(conn=Depends(db_session)):
    """FR-KG-12 补 G13：回流知识候选统计（按来源分组）。

    统计 v2g_candidates 中 properties 含 "reflow":true 的候选（对话/影响分析/模型评审
    自动回流生成），按 source_doc 前缀分组：
    conv- → 对话 / impact- → 影响分析 / review- → 模型评审 / 其它 → 其他。
    返回 {"total": N, "by_source": {conversation, impact, review, other}}。
    """
    rows = conn.execute(
        "SELECT source_doc, COUNT(*) AS n FROM v2g_candidates "
        "WHERE properties LIKE '%\"reflow\": true%' OR properties LIKE '%\"reflow\":true%' "
        "GROUP BY source_doc").fetchall()
    by_source = {"conversation": 0, "impact": 0, "review": 0, "other": 0}
    total = 0
    for r in rows:
        src = (r["source_doc"] or "")
        if src.startswith("conv-"):
            by_source["conversation"] += r["n"]
        elif src.startswith("impact-"):
            by_source["impact"] += r["n"]
        elif src.startswith("review-"):
            by_source["review"] += r["n"]
        else:
            by_source["other"] += r["n"]
        total += r["n"]
    return {"total": total, "by_source": by_source}


@router.put("/api/knowledge/v2g/candidates/{cid}")
def v2g_update(cid: int, body: V2GUpdateIn, conn=Depends(db_session), user=Depends(current_user)):
    """治理中心：编辑单条候选（改名称/类型/属性，重新校验与消歧打标，仍为 pending）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_update(
        cid, body.name, body.entity_type, body.properties, actor=_actor(user))


@router.post("/api/knowledge/v2g/confirm")
def v2g_confirm(body: V2GConfirmIn, conn=Depends(db_session),
                user=Depends(require_permission("kb_review", "confirm"))):
    """O-1 Step2：人工确认候选 → 图谱入库（本体校验）+ chunk↔entity 溯源链接。
    P1-1 编排已上提 Service。P0-C：dup_action 消歧前移（skip/align/create）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_confirm(
        body.batch_id, body.selected_ids, actor=_actor(user), dup_action=body.dup_action or "create",
        triple_only=body.triple_only)


@router.get("/api/knowledge/v2g/gate")
def v2g_gate_status(conn=Depends(db_session), user=Depends(current_user)):
    """入库发布门禁（P0）：返回门禁开关/来源配置/目标分支。"""
    from services.ingest_gate import status as _gate_status
    return _gate_status()


@router.post("/api/knowledge/v2g/gate/evaluate")
def v2g_gate_evaluate(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """门禁评估：给定来源/置信度，返回是否需强制待审。body: {source_type, confidence}。"""
    from services.ingest_gate import evaluate
    try:
        conf = float(body.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return evaluate(body.get("source_type", "ai_generated"), conf)


@router.post("/api/knowledge/v2g/submit-review")
def v2g_submit_review(body: dict, conn=Depends(db_session),
                      user=Depends(require_permission("kb_review", "confirm"))):
    """优化一/二：V2 生成的候选「提交审核」→ 置 review_status=submitted + 触发融合闸。

    body: {batch_id?, selected_ids?}。默认候选待审核；提交后进入审核队列。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_submit_review(
        body.get("batch_id"), body.get("selected_ids"), actor=_actor(user))


@router.post("/api/knowledge/v2g/reject")
def v2g_reject(body: V2GRejectIn, conn=Depends(db_session),
               user=Depends(require_permission("kb_review", "confirm"))):
    """S4：批量驳回候选（人工确认不符合本体/业务要求，标记 rejected 保留审计）。
    P1-1 编排已上提 Service。reason 为治理中心驳回原因（单条/批量留痕）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_reject(
        body.candidate_ids, body.reason or "", actor=_actor(user))


@router.post("/api/knowledge/v2g/fuse")
def v2g_fuse(body: dict, conn=Depends(db_session),
             user=Depends(require_permission("kb_review", "confirm"))):
    """写前融合闸：手动触发批次/选中候选融合（auto_merge / review_queue / new_entity 分流）。

    body: {batch_id?, selected_ids?, run_quality?}；融合自动路径已内置于 confirm。
    返回融合统计 + 质量闸结果（batch 模式默认抽检）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_fuse(
        body.get("batch_id"), body.get("selected_ids"),
        run_quality=body.get("run_quality", True), actor=_actor(user))


@router.get("/api/knowledge/v2g/fuse/status")
def v2g_fuse_status(batch_id: Optional[str] = None, limit: int = 50,
                    conn=Depends(db_session)):
    """融合闸审计状态：action 汇总 + 最近明细 + 当前配置（治理中心展示）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_fuse_status(batch_id=batch_id, limit=limit)


@router.put("/api/knowledge/v2g/fuse/config")


# ── 知识治理全链路优化·三元组统一审核队列（优化三）────────────────


@router.get("/api/knowledge/triples/review-queue")
def triples_review_queue(status: str = "pending", limit: int = 200,
                         conn=Depends(db_session), user=Depends(current_user)):
    """三元组统一审核队列（S-P-O 视角，替代实体/关系双队列）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).triple_review_queue(status=status, limit=limit)


@router.get("/api/knowledge/triples/{triple_id}/provenance")
def triples_provenance(triple_id: str, conn=Depends(db_session), user=Depends(current_user)):
    """P0-5 三元组溯源：单条三元组的完整证据链。

    链路：来源文档 → 原文 chunk（预览）→ 审批记录 → 落图对象（实体/关系回链）→ SysML 版本。
    """
    t = conn.execute("SELECT * FROM triples WHERE triple_id=?", (triple_id,)).fetchone()
    if not t:
        raise HTTPException(status_code=404, detail=f"三元组不存在: {triple_id}")
    d = dict(t)
    out = {
        "triple_id": triple_id,
        "subject": {"id": d.get("subject_id"), "name": d.get("subject_name")},
        "predicate": d.get("predicate"),
        "object": {"id": d.get("object_id") or "", "value": d.get("object_value"),
                   "type": d.get("object_type")},
        "confidence": d.get("confidence"),
        "status": d.get("status"),
        # 来源层：文档 + 原文 chunk
        "source": {
            "doc": d.get("source_doc") or "",
            "chunk_id": d.get("source_chunk") or "",
            "chunk_preview": "",
            "source_type": d.get("source_type") or "",
            "sysml_version_id": d.get("sysml_version_id") or 0,
        },
        # 审批层
        "review": {
            "decision": d.get("review_decision") or "",
            "note": d.get("review_note") or "",
            "reviewed_by": d.get("reviewed_by") or "",
            "reviewed_at": d.get("reviewed_at") or "",
            "created_by": d.get("created_by") or "",
            "created_at": d.get("created_at") or "",
        },
        # 落图层：graph_stored + 回链对象
        "graph": {"stored": bool(d.get("graph_stored")),
                  "entity_id": d.get("graph_entity_id") or "",
                  "relation_id": d.get("graph_relation_id") or "",
                  "entity_name": "", "relation_label": ""},
    }
    # chunk 原文预览（document_chunks.id 匹配）
    try:
        if d.get("source_chunk"):
            ck = conn.execute(
                "SELECT content, section, document_id FROM document_chunks WHERE id=?",
                (int(d["source_chunk"]),)).fetchone()
            if ck:
                out["source"]["chunk_preview"] = (ck["content"] or "")[:300]
                out["source"]["chunk_section"] = ck["section"] or ""
                doc = conn.execute("SELECT filename FROM documents WHERE id=?",
                                   (ck["document_id"],)).fetchone() if ck["document_id"] else None
                if doc:
                    out["source"]["doc_filename"] = doc["filename"]
    except (ValueError, TypeError):
        pass
    # 落图对象名称
    try:
        if d.get("graph_entity_id"):
            e = conn.execute("SELECT name, entity_type FROM entities WHERE id=?",
                             (d["graph_entity_id"],)).fetchone()
            if e:
                out["graph"]["entity_name"] = f"{e['name']}（{e['entity_type']}）"
        if d.get("graph_relation_id"):
            r = conn.execute(
                "SELECT r.relation_type, s.name sname, t2.name tname FROM relations r "
                "JOIN entities s ON s.id=r.source_id JOIN entities t2 ON t2.id=r.target_id "
                "WHERE r.id=?", (int(d["graph_relation_id"]),)).fetchone() if str(
                    d["graph_relation_id"]).isdigit() else None
            if r:
                out["graph"]["relation_label"] = f"{r['sname']} —{r['relation_type']}→ {r['tname']}"
    except Exception:  # noqa: BLE001 溯源展示尽力而为，不影响主返回
        pass
    return out


@router.post("/api/knowledge/triples/{triple_id}/review")
def triples_review(triple_id: str, body: dict, conn=Depends(db_session),
                   user=Depends(require_permission("kb_review", "confirm"))):
    """三元组审核：body {decision: approved|rejected, note?}。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).triple_review(
        triple_id, body.get("decision", "approved"), body.get("note", ""), actor=_actor(user))


@router.post("/api/knowledge/triples/batch-review")
def triples_batch_review(body: dict, conn=Depends(db_session),
                         user=Depends(require_permission("kb_review", "confirm"))):
    """三元组批量审核：body {triple_ids: [...], decision, note?}。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).triple_batch_review(
        body.get("triple_ids", []), body.get("decision", "approved"),
        body.get("note", ""), actor=_actor(user))


@router.get("/api/knowledge/triples/stats")
def triples_stats_route(conn=Depends(db_session), user=Depends(current_user)):
    """三元组生命周期统计。

    ⚠️ 2026-09-22 修复：此前重构搬走函数体后遗留**悬空装饰器**——本 GET 路由错误地
    注册到了下方 triples_commit（写操作）上，导致 GET /stats 每次被调用都会触发
    三元组落图 commit（副作用 GET，治理页每次打开都会误触发一次）。恢复委托 shared.triples_stats。
    """
    from routers.knowledge_parts.shared import triples_stats as _stats_impl
    return _stats_impl(conn, user)


@router.post("/api/knowledge/triples/commit")
def triples_commit(conn=Depends(db_session),
                   user=Depends(require_permission("kb_review", "confirm"))):
    """三元组唯一图写入源：把已通过(approved)且未落图的三元组反写为 entities/relations（构图）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).triple_commit_graph(actor=_actor(user))

# ── 三元组物理清理（2026-09-12 治理）：候选/未审残留=死数据，预览→确认删除 ──
_CLEAN_STATUSES = ("pending", "reviewed", "rejected", "deprecated")


@router.post("/api/knowledge/triples/cleanup")
def triples_cleanup(body: dict, conn=Depends(db_session),
                    user=Depends(require_permission("kb_review", "confirm"))):
    """物理删除三元组残留（候选/待审/驳回/废弃）。
    body: {statuses?: [...], confirm: bool, limit?: int}
    - confirm=false → dry-run，返回将删清单与统计，不删除；
    - confirm=true  → 物理删除并逐条审计。已审批(approved)不参与，保护图库。"""
    statuses = [x for x in (body.get("statuses") or ["pending", "reviewed", "rejected"]) if x in _CLEAN_STATUSES]
    if not statuses:
        raise HTTPException(400, "非法的 status 集合（支持 pending/reviewed/rejected/deprecated）")
    limit = max(1, min(int(body.get("limit") or 0) or 200, 500))
    ph = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT triple_id, subject_name, predicate, object_value, status, graph_stored "
        f"FROM triples WHERE status IN ({ph}) ORDER BY id", statuses).fetchall()
    total = len(rows)
    sample = [dict(r) for r in rows[:limit]]
    by_status = {st: sum(1 for r in rows if r["status"] == st) for st in statuses}
    if not body.get("confirm"):
        return {"ok": True, "dry_run": True, "total": total, "by_status": by_status,
                "sample": sample, "hint": f"将物理删除 {total} 条未审/废弃残留，核实后以 confirm=true 执行"}
    actor = _actor(user)
    deleted = 0
    for r in rows:
        conn.execute("DELETE FROM triples WHERE triple_id=?", (r["triple_id"],))
        deleted += 1
    audit(actor, "triples_cleanup", f"物理删除三元组残留 {deleted} 条 (status={statuses})", conn=conn)
    conn.commit()
    return {"ok": True, "deleted": deleted, "by_status": by_status}


@router.delete("/api/knowledge/triples/{triple_id}")
def triples_delete_one(triple_id: str, conn=Depends(db_session),
                       user=Depends(require_permission("kb_review", "confirm"))):
    """单条删除三元组。保护已审批(approved)或已构图(graph_stored)：须先处置（废弃/清理）再删，避免误删审计事实。"""
    row = conn.execute("SELECT status, graph_stored FROM triples WHERE triple_id=?", (triple_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "triple not found"}, 404)
    if row["status"] == "approved" or row["graph_stored"]:
        return JSONResponse({"error": "该三元组已审批或已构图，不可直接删除（治理请走清理）"}, 400)
    conn.execute("DELETE FROM triples WHERE triple_id=?", (triple_id,))
    audit(_actor(user), "triple_delete", f"物理删除三元组 {triple_id}", conn=conn)
    conn.commit()
    return {"ok": True}



@router.post("/api/knowledge/reconcile")
def knowledge_reconcile(body: dict = None, conn=Depends(db_session),
                        user=Depends(require_permission("kb_review", "confirm"))):
    """写后调和 Nightly Job：规则预处理（Blocking 化消歧）+ 冲突检测。

    由外部 cron/平台定时调度触发（与写前融合闸互补）；幂等，可重复执行。
    `body.async=true`（P0-C）：万级实体时该作业为分钟级，可提交后台执行。
    """
    from services.knowledge_service import KnowledgeService
    if bool((body or {}).get("async")):
        from core import job_queue as jq
        _job = jq.submit(conn, "knowledge_reconcile", {"actor": _actor(user)},
                         job_key="knowledge_reconcile:adhoc", reuse_terminal=False)
        return {"ok": True, "async": True, "job_id": _job.get("job_id"),
                "job_status": _job.get("status"), "deduped": _job.get("deduped", False),
                "poll": "/api/jobs/%d" % int(_job.get("job_id") or 0)}
    return KnowledgeService(conn).knowledge_reconcile(actor=_actor(user))


@router.get("/api/knowledge/chunks/{chunk_id}/linked")
def chunk_linked_entities(chunk_id: int, conn=Depends(db_session)):
    """O-1：chunk 溯源——查看该分块已入库关联实体（一键追溯）。"""
    row = conn.execute("SELECT linked_entity_ids FROM document_chunks WHERE id=?", (chunk_id,)).fetchone()
    ids = json.loads(row["linked_entity_ids"] or "[]") if row else []
    ents = []
    for eid in ids:
        e = conn.execute("SELECT id, name, entity_type, status FROM entities WHERE id=?", (eid,)).fetchone()
        if e:
            ents.append(dict(e))
    return {"chunk_id": chunk_id, "entities": ents}


@router.post("/api/knowledge/ingest/stage")
def ingest_stage(body: SysMLIn, conn=Depends(db_session), user=Depends(current_user)):
    """R1 统一入库闸门：AI 建模产物/SysML 文本/XML/手工 JSON → 候选暂存（不入库）。

    [R3 2026-09-01] 外部工程 SysML 导入暂缓：前端已隐藏 xml/text 入口，本端点保留兼容；
    AI 建模 JSON 来源（source=json）仍为现行路径。恢复外部导入时重新开放 xml/text。

    与 sysml/import（直写物化，已标记 deprecated）的本质区别：任何来源的建模输出
    都必须先经本闸门 —— 词典归一(_normalize_mentions) → 锚点/本体预校验
    (OntologyValidator.validate_node) → 消歧打标(_disambiguate_many/_disambiguate_rel)
    → 写入 v2g_candidates 候选（source_doc 留痕来源），再由人工确认（v2g/confirm，
    支持 dup_action 消歧前移）后才物化入图。入库留痕 source_type=ai_generated。
    """
    from sysml_importer import sysml_to_candidates
    src = (body.source or "text").lower()
    if src not in ("text", "json", "xml"):
        return JSONResponse({"error": f"不支持的来源类型: {src}（支持 text/json/xml）"}, 400)
    label = (body.model_name or "AI 建模").strip() or "AI 建模"
    result = sysml_to_candidates(conn, body.content, source=src, model_name=label)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, 400)
    result["gate"] = "staged"  # 语义标记：已入候选，未入图
    result["hint"] = "已生成候选（未入库）——请在候选确认弹窗审核后「确认入库」"
    audit(_actor(user), "ingest_stage", f"统一闸门暂存 {label}: 实体候选{result['node_count']} 关系候选{result['edge_count']} 拒绝{len(result.get('rejected') or [])}", conn=conn)
    return result


@router.post("/api/knowledge/sysml/import")
def sysml_import(body: SysMLIn, conn=Depends(db_session), user=Depends(current_user)):
    """O-3：SysML V2 模型导入——解析 → GraphStore 实例化（本体校验）→ 批次/映射落库。

    [DEPRECATED] 直写物化路径已收敛：请改用 POST /api/knowledge/ingest/stage（统一闸门，
    先候选后确认）。本路由保留以兼容历史调用与已归档导入记录回看。
    """
    from sysml_importer import import_sysml
    result = import_sysml(conn, body.source, body.content, model_name=body.model_name)
    audit(_actor(user), "sysml_import", f"SysML 导入 {body.model_name or body.source}: {result['status']} 实体{result['entity_count']} 关系{result['relation_count']}", conn=conn)
    return result


@router.get("/api/knowledge/sysml/imports")
def sysml_imports(limit: int = 20, conn=Depends(db_session)):
    from sysml_importer import list_imports
    return list_imports(conn, limit)


@router.get("/api/knowledge/sysml/imports/{batch_id}")
def sysml_import_detail(batch_id: str, conn=Depends(db_session)):
    from sysml_importer import get_import
    detail = get_import(conn, batch_id)
    if not detail:
        return JSONResponse({"error": "batch not found"}, 404)
    return detail


@router.get("/api/knowledge/profile/export")
def profile_export(fmt: str = "1x", conn=Depends(db_session)):
    """导出本体为 SysML Profile（fmt=1x → .profile XMI；fmt=v2 → .kerml），供建模工具加载。"""
    from sysml_profile import to_profile_1x, to_profile_v2
    from fastapi.responses import Response
    if fmt == "1x":
        return Response(to_profile_1x(conn), media_type="application/xml",
                        headers={"Content-Disposition": "attachment; filename=mbse-ontology.profile"})
    if fmt == "v2":
        return Response(to_profile_v2(conn), media_type="text/plain",
                        headers={"Content-Disposition": "attachment; filename=mbse-ontology.kerml"})
    return JSONResponse({"error": f"不支持的导出格式: {fmt}（1x | v2）"}, 400)


@router.get("/api/knowledge/sysml/export")
def sysml_export(branch: Optional[str] = "release", model_name: Optional[str] = None,
                 conn=Depends(db_session)):
    """FR-KG-7 补 G11：图谱实例 → SysML 2.x (KerML) 模型文本导出（缺省 release 分支）。

    与 SysML 导入（POST /api/knowledge/sysml/import）构成"图谱↔SysML"双向一致闭环；
    文本可直接用 SysML v2 建模工具加载或另存 .kerml。
    """
    from sysml_profile import to_model_v2
    from fastapi.responses import Response
    ker = to_model_v2(conn, branch=branch or "release",
                      model_name=model_name or "GraphInstanceModel")
    return Response(ker, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="graph-export.sysml.ker"'})


@router.get("/api/knowledge/entity-dups")
def entity_duplicates(conn=Depends(db_session)):
    """O-5：待审消歧候选列表（默认 status=pending 全量，与 fusion/status.pending_pairs 口径一致）。

    Q2 修复：此前 list_dup_candidates 不限 status 且 ORDER BY score DESC LIMIT 50，
    高分 merged 会把低分 pending 挤出前 50，导致「上方 pending_pairs 有数、下方待审=0」。
    现默认只查待审、放开条数上限，并返回 total_pending 供一致计数。
    """
    from entity_resolver import list_dup_candidates
    pending = list_dup_candidates(conn, status='pending', limit=1000)
    total = conn.execute(
        "SELECT COUNT(*) FROM entity_dup_candidates WHERE status='pending'").fetchone()[0]
    return {"duplicates": pending, "total_pending": total}


@router.post("/api/knowledge/entity-dups/detect")
def entity_dup_detect(conn=Depends(db_session)):
    """E-1：触发消歧检测（blocking → 评分 → 三档决策 → 候选落库 + auto-merge）。"""
    from entity_resolver import detect_and_save_candidates
    result = detect_and_save_candidates(conn)
    audit("系统", "entity_dup_detect",
          f"消歧检测: 候选 {result['candidates']} / 自动合并 {result['auto_merged']}", conn=conn)
    return result


@router.post("/api/knowledge/entity-dups/{cid}/review")
def entity_dup_review(cid: int, body: dict, conn=Depends(db_session),
                      user=Depends(require_permission("kb_review", "confirm"))):
    """E-3：审阅候选（confirm=合并 / reject=驳回）。"""
    from entity_resolver import review_candidate
    result = review_candidate(conn, cid, body.get("action", "confirm"))
    if not result.get("ok"):
        return JSONResponse(result, 400)
    audit(_actor(user), "entity_dup_review", f"消歧审阅#{cid}: {body.get('action')}", conn=conn)
    return result


@router.post("/api/knowledge/fusion/preprocess")
def fusion_preprocess(conn=Depends(db_session), user=Depends(require_any_permission(WRITE_PERMS))):
    """工序① 规则预处理（免费）：归一化 + 别名/简全称 + Canopy 粗筛 → 三态出口。

    自动对齐（归一化一致）直接合并留痕；候选对（规则判不了）生成 pending 候选供工序②。
    """
    from entity_resolver import run_rule_preprocess
    result = run_rule_preprocess(conn)
    audit(_actor(user), "fusion_preprocess",
          f"规则预处理: 输入 {result['input_n']} / 自动对齐 {result['auto_aligned']} / 候选对 {result['candidates']} / 跳过 {result['skipped']}",
          conn=conn)
    return result


@router.post("/api/knowledge/fusion/resolve")
def fusion_resolve(body: dict = None, conn=Depends(db_session),
                   user=Depends(require_any_permission(WRITE_PERMS))):
    """工序② LLM 实体解析：仅对规则判不了的候选对（method=canopy, pending）成对判定。

    body = {pair_ids: [..] | None（None=全量 pending canopy 候选）, limit: int}
    LLM 一次输出：消歧 + 对齐 + 冲突裁决 + 理由 + 置信度，写入 evidence 供审核溯源；
    LLM 不可用 → 降级（标记 degraded），人工直接裁决，流程不中断。
    """
    from entity_resolver import resolve_pairs_llm
    body = body or {}
    result = resolve_pairs_llm(conn, pair_ids=body.get("pair_ids"),
                               limit=int(body.get("limit") or 20), operator=_actor(user))
    audit(_actor(user), "fusion_resolve",
          f"LLM 实体解析: 判定 {result['judged']} / 降级 {result['degraded']}", conn=conn)
    return result


@router.get("/api/knowledge/fusion/status")
def fusion_status(conn=Depends(db_session)):
    """融合工作台状态：输入候选 / 规则自动对齐 / 候选对（pending）/ LLM 已判 / 待裁决冲突。"""
    ent_n = conn.execute("SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
    pending_n = conn.execute(
        "SELECT COUNT(*) FROM entity_dup_candidates WHERE status='pending'").fetchone()[0]
    llm_judged = conn.execute(
        "SELECT COUNT(*) FROM entity_dup_candidates WHERE status='pending'"
        " AND evidence LIKE '%llm_verdict%'").fetchone()[0]
    conflict_n = conn.execute(
        "SELECT COUNT(*) FROM knowledge_conflicts WHERE status='pending'").fetchone()[0]
    auto_merged = conn.execute(
        "SELECT COUNT(*) FROM entity_merges WHERE operator='auto' AND status='merged'").fetchone()[0]
    return {"entity_n": ent_n, "pending_pairs": pending_n, "llm_judged": llm_judged,
            "conflict_n": conflict_n, "auto_merged": auto_merged}


@router.post("/api/knowledge/entity-merges/{mid}/rollback")
def entity_merge_rollback(mid: int, conn=Depends(db_session),
                          user=Depends(require_any_permission(WRITE_PERMS))):
    """E-7：撤销合并（恢复 dup、移除补入属性、关系回指）。"""
    from entity_resolver import rollback_merge
    result = rollback_merge(conn, mid)
    if not result.get("ok"):
        return JSONResponse(result, 400)
    audit(_actor(user), "entity_merge_rollback", f"撤销合并#{mid}", conn=conn)
    return result


@router.get("/api/knowledge/entity-merges")
def entity_merges_list(limit: int = 500, conn=Depends(db_session)):
    """E-7：合并历史（供前端审计/撤销）。Q2：返回全量近 500 + total + 关联实体名称。"""
    total = conn.execute("SELECT COUNT(*) FROM entity_merges").fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM entity_merges ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    ids = set()
    for r in rows:
        if r["keep_id"]:
            ids.add(r["keep_id"])
        if r["dup_id"]:
            ids.add(r["dup_id"])
    names = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for n in conn.execute(
                f"SELECT id, name FROM entities WHERE id IN ({ph})", list(ids)).fetchall():
            names[n["id"]] = n["name"]
    items = []
    for r in rows:
        d = dict(r)
        d["keep_name"] = names.get(r["keep_id"], "")
        d["dup_name"] = names.get(r["dup_id"], "")
        items.append(d)
    return {"items": items, "total": total}


@router.post("/api/knowledge/entities/merge")
def entity_merge(body: MergeIn, conn=Depends(db_session),
                 user=Depends(require_any_permission(WRITE_PERMS))):
    """O-5：手动合并实体（属性融合 + 关系重指向 + dup 软删除保留审计）。"""
    from entity_resolver import merge_entities
    result = merge_entities(conn, body.keep_id, body.dup_id)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "合并失败")}, 400)
    audit(_actor(user), "entity_merge", f"实体合并: {body.dup_id} → {body.keep_id}（属性 {len(result.get('merged_props',[]))} 项融合）", conn=conn)
    return result


@router.get("/api/knowledge/engine-stats")
def engine_stats(conn=Depends(db_session)):
    """双引擎消费统计：图/向量/混合路由占比、平均置信度与延迟、最近查询。"""
    return QueryRouter().stats(conn)


@router.post("/api/knowledge/retrieve")
def engine_retrieve(body: RetrieveIn, conn=Depends(db_session), user=Depends(current_user)):
    """手动检索调试端点：返回图引擎 + 向量引擎双明细与路由决策（P1 双引擎可观测）。

    KB-P1 增强：hybrid=true 时启用 BM25+向量混合检索 + 轻量重排（hits 含 vec/bm25 双分）。
    """
    from agent import GraphRAG
    result = GraphRAG().retrieve(body.query, body.branch)
    # KB-P1：混合检索附加
    if body.hybrid:
        from knowledge_engine import hybrid_search
        hybrid = hybrid_search(conn, body.query, top_k=body.top_k)
        result["hybrid"] = hybrid
        result["hybrid_count"] = len(hybrid["hits"])
    audit(_actor(user), "engine_retrieve", f"检索: {body.query} → route={result['route']} hybrid={body.hybrid}", conn=conn)
    return result


@router.post("/api/knowledge/chunks/search")
def chunk_search(body: RetrieveIn, conn=Depends(db_session)):
    """分块级检索 + 命中预览（KB-P1 升级：hybrid=true 用 BM25+向量混合，默认纯向量）。

    返回 document_chunks 命中片段（score/source_doc/section/chunk_index/content 预览）。
    文档全局化：检索不按分支过滤（向量化数据全局消费；branch 参数保留接收兼容旧前端）。
    """
    branches = None
    # G1（2026-09-21）：默认排除已下线文档；管理侧显式传 include_deprecated=true 才纳入
    _inc_dep = bool(getattr(body, "include_deprecated", False))
    if body.hybrid:
        from knowledge_engine import hybrid_search
        res = hybrid_search(conn, body.query, top_k=body.top_k, branches=branches,
                            include_deprecated=_inc_dep)
        hits = res["hits"]
        return {"query": body.query, "hits": hits, "hit_count": len(hits),
                "bm25_count": res["bm25_count"], "vec_count": res["vec_count"], "mode": "hybrid"}
    from knowledge_pipeline import search_chunks
    hits = search_chunks(conn, body.query, top_k=body.top_k, branches=branches,
                         include_deprecated=_inc_dep)
    return {"query": body.query, "hits": hits, "hit_count": len(hits), "mode": "vector"}


@router.post("/api/knowledge/conflicts/detect")
def conflicts_detect(conn=Depends(db_session), user=Depends(current_user)):
    """检测矛盾事实并写入 knowledge_conflicts（同归一名称/同三元组的属性值冲突）。"""
    from entity_resolver import conflict_detect
    result = conflict_detect(conn)
    audit(_actor(user), "conflict_detect",
          f"冲突检测: 新增 {result['detected']} / 跳过重复 {result['skipped']}", conn=conn)
    return result


@router.get("/api/knowledge/conflicts")
def conflicts_list(status: Optional[str] = None, limit: int = 100,
                   conn=Depends(db_session)):
    """冲突列表（pending/resolved/ignored，两侧对象+属性+值+加权分+证据）。"""
    from entity_resolver import list_conflicts
    return list_conflicts(conn, status=status or None, limit=limit)


@router.post("/api/knowledge/conflicts/{conflict_id}/adjudicate")
def conflicts_adjudicate(conflict_id: int, body: dict = None,
                         conn=Depends(db_session),
                         user=Depends(require_permission("kb_review", "confirm"))):
    """冲突裁决：decision=left|right|ignore（采纳值侧/忽略），两侧属性统一规范值 + 审计留痕。"""
    from entity_resolver import conflict_adjudicate
    decision = (body or {}).get("decision", "")
    result = conflict_adjudicate(conn, conflict_id, decision, operator=_actor(user))
    if result.get("ok"):
        audit(_actor(user), "conflict_adjudicate",
              f"冲突裁决 #{conflict_id}: {decision}"
              + (f"（规范值：{result.get('canonical','')}）" if result.get("canonical") is not None else ""),
              conn=conn)
    else:
        return JSONResponse({"error": result.get("error", "裁决失败")}, 400)
    return result


@router.post("/api/knowledge/ontology/draft")
def ontology_draft(body: dict = None, conn=Depends(db_session),
                   user=Depends(require_any_permission(WRITE_PERMS))):
    """P2-1 从输入生成本体草案候选（ontology_drafts pending，不入库）。

    body = {source_type: "text"|"profile", text/content, format: "1x"|"v2"}
    - text：LLM 提炼 / 规则兜底（"X 是 Y 的子类"等）
    - profile：SysML Profile（1.x XMI / 2.x KerML）结构化解析（原 /api/knowledge/profile/parse 能力）
    """
    from ontology_blueprint import draft_from_text, draft_from_profile
    body = body or {}
    source_type = (body.get("source_type") or "text").lower()
    if source_type == "profile":
        content = (body.get("content") or body.get("text") or "").strip()
        if not content:
            return JSONResponse({"error": "Profile 内容为空：请粘贴 XMI/KerML 内容"}, 400)
        result = draft_from_profile(conn, content, fmt=str(body.get("format") or "1x"),
                                    operator=_actor(user))
    else:
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text 为空：请粘贴业务文档/模型描述文本"}, 400)
        result = draft_from_text(conn, text, operator=_actor(user))
    audit(_actor(user), "ontology_draft",
          f"本体蓝图提取[{source_type}]: 草案 {len(result['drafts'])} 条（batch {result['batch_id']}）",
          conn=conn)
    return result


@router.get("/api/knowledge/ontology/drafts")
def ontology_drafts_list(batch_id: str = "", status: str = "pending", limit: int = 100,
                         conn=Depends(db_session)):
    """本体草案列表（按 batch 过滤）。"""
    from ontology_blueprint import list_drafts
    return list_drafts(conn, batch_id=batch_id, status=status or None, limit=limit)


@router.post("/api/knowledge/ontology/drafts/apply")
def ontology_drafts_apply(body: dict = None, conn=Depends(db_session),
                          user=Depends(require_any_permission(WRITE_PERMS))):
    """P2-1 确认应用本体草案：校验（重名/父类型存在）后写 ontology_types，标记 applied。"""
    from ontology_blueprint import apply_drafts
    body = body or {}
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    result = apply_drafts(conn, ids, operator=_actor(user))
    # 2026-09-08 版本语义收敛：蓝图应用属编辑态，只留 audit/变更记录，不升版本号（发布时统一升级）
    audit(_actor(user), "ontology_draft_apply",
          f"本体蓝图应用: {len(result['applied'])} 条"
          + (f"（失败 {len(result['errors'])}）" if result["errors"] else ""),
          conn=conn)
    return result


# ── 工程维度入库（工程归档 → 三元组 → 个人分支图库，2026-09-09） ──

def _project_id_from(body: dict, conn) -> str:
    """body 直接给 project_id；或给 conversation_id 反查所属工程。"""
    pid = (body or {}).get("project_id") or ""
    if pid:
        return pid
    cid = (body or {}).get("conversation_id") or 0
    if cid:
        r = conn.execute("SELECT project_id FROM conversations WHERE id=?", (cid,)).fetchone()
        return (r["project_id"] or "") if r else ""
    return ""


@router.post("/api/knowledge/project-ingest/preview")
def project_ingest_preview(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """工程入库预览（只读）：聚合该工程全部 SysML 版本 → 实体/关系/属性统计 + 与个人分支图库重合预估。"""
    pid = _project_id_from(body, conn)
    if not pid:
        return JSONResponse({"error": "缺少 project_id / conversation_id"}, 400)
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).project_ingest_preview(pid)


@router.post("/api/knowledge/project-ingest/commit")
def project_ingest_commit(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """工程入库提交：逐版本 候选化→融合闸→确认物化入个人分支图库+三元组，写批次记录。二次确认由前端向导承担。

    `body.async=true`（P0-C）：改为提交后台作业，立即返回 `{job_id}`。
    实测该流程为分钟级（逐版本跑 sysml_to_candidates + 融合闸 + 全量落图），
    同步会一直占着 HTTP 连接且断开即前功尽弃。
    ⚠️ 幂等键带 `version_ids` 指纹：换版本集重跑 = 新作业（合法），
       同版本集重复提交 = 复用/新建而不重复执行。
    """
    pid = _project_id_from(body, conn)
    if not pid:
        return JSONResponse({"error": "缺少 project_id / conversation_id"}, 400)
    if bool((body or {}).get("async")):
        import hashlib
        import json as _j
        from core import job_queue as jq
        _vids = (body or {}).get("version_ids") or []
        _fp = hashlib.md5(_j.dumps(sorted(_vids), ensure_ascii=False).encode()).hexdigest()[:8]
        _job = jq.submit(conn, "project_ingest",
                         {"project_id": pid, "version_ids": _vids or None,
                          "target_branch": (body or {}).get("target_branch") or "personal",
                          "actor": _actor(user)},
                         job_key="project_ingest:%s:%s" % (pid, _fp),
                         reuse_terminal=False)
        audit(_actor(user), "project_ingest_async",
              f"工程入库(异步) {pid} → job #{_job['job_id']}", conn=conn)
        return {"ok": True, "async": True, "project_id": pid,
                "job_id": _job.get("job_id"), "job_status": _job.get("status"),
                "deduped": _job.get("deduped", False),
                "poll": "/api/jobs/%d" % int(_job.get("job_id") or 0),
                "hint": "已排队；完成后查 /api/jobs/{job_id} 取 stats 与落图统计"}
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).project_ingest_commit(
        pid, actor=_actor(user),
        version_ids=(body or {}).get("version_ids") or None)


@router.get("/api/knowledge/project-ingest/logs")
def project_ingest_logs(project_id: str = "", limit: int = 20,
                        conn=Depends(db_session), user=Depends(current_user)):
    """工程入库历史（最新在前，含每次入库统计信息）。"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).project_ingest_logs(project_id=project_id, limit=limit)


@router.post("/api/knowledge/sysml/pull-ingest")
def sysml_pull_ingest(body: dict = None, conn=Depends(db_session), user=Depends(current_user)):
    """从智源拉取建模数据 → 直接转三元组 → 存个人分支图库（2026-09-11 拉取流程）。

    后端编排全程直通（候选化→融合闸→自动批准→落图），不产生数据治理审核面板待办；
    回执由 AI 建模对话流渲染。body: {conversation_id?, vc?, package_data_id?, target_branch?}。
    2026-09-24：拉取按工程维度路由——conversation_id 反查工程，vc 优先工程绑定值
    （projects.tool_binding），未绑定则明确报错引导绑定，不再默默拉默认。
    2026-09-24 建模工具适配层：绑定泛化 {tool,ref,name}（zhiyuan/magicdraw），
    tool=magicdraw → TOOL_NOT_READY（MagicDraw 连接器接入前明确报错，不静默走智源）。
    """
    from services.knowledge_service import KnowledgeService
    body = body or {}
    return KnowledgeService(conn).zhiyuan_pull_ingest(
        actor=_actor(user), vc=(body.get("vc") or "").strip(),
        package_data_id=body.get("package_data_id"),
        target_branch=(body.get("target_branch") or "personal"),
        conversation_id=int(body.get("conversation_id") or 0))
