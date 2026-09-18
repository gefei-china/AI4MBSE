# -*- coding: utf-8 -*-
"""创建 triple_commit.py：三元组唯一图写入源（入库链重构核心）。

设计：
1. stage_candidates_as_triples(conn, cands, batch_id, operator)
   —— 把 pending 候选物化为「待审三元组」（不写 entities/relations）：
     * 实体候选 → (S, type, T) + 各属性字面三元组
     * 关系候选 → (S, predicate, O) 关系三元组（端点为候选名）
   —— subject_id 用候选自身 id，name 用候选名；关系端点用 name（落图时解析到实体 id）。

2. commit_approved_triples(conn, limit, operator)
   —— 把 status='approved' AND graph_stored=0 的三元组反写为 entities/relations（构图）。
     * Phase1 实体：type 三元组(object_value=类型) 建实体 + 字面属性补入 properties；收集 name/type→entity_id
     * Phase2 关系：关系三元组(object_type='entity') 解析 src/tgt name→entity_id → GraphStore.create_edge
     * 幂等（graph_stored 标记 + entity/relation 唯一性判重），完成后置 graph_stored=1 并提交打点。

实体/关系表仍是构图事实源（图谱可视化/检索/推理读取不变）；写入时机收敛为「三元组审核通过后」。
"""
import json
import logging
import re
import uuid

logger = logging.getLogger(__name__)


def _loc(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5]", "_", str(name or "").strip())


def stage_candidates_as_triples(conn, cands, batch_id: str | None = None,
                                operator: str = "知识工程师", linked_chunks: set | None = None) -> dict:
    """候选 → 待审三元组（不写实体/关系）。cands 为 v2g_candidates rows。"""
    from triple_store import _add_triple
    staged = {"entities": 0, "relations": 0, "triples": 0}
    for c in cands:
        etype = str(c["entity_type"] or "")
        name = str(c["entity_name"] or "")
        if not name:
            continue
        subj_id = str(c["id"])
        props = {}
        try:
            props = json.loads(c["properties"] or "{}") if c["properties"] else {}
        except Exception:
            props = {}
        src_doc = str(c["source_doc"] or "")
        svid = int(c["sysml_version_id"] or 0)
        if etype == "关系候选":
            src_n = str(c["rel_source"] or "")
            tgt_n = str(c["rel_target"] or "")
            rel = str(c["rel_type"] or "")
            if not (src_n and tgt_n and rel):
                parts = name.split("--")
                if len(parts) >= 3:
                    src_n, rel, tgt_n = parts[0].strip(), parts[1].strip(), "--".join(parts[2:]).strip()
            # P0-5 防御（2026-09-06）：stage 同样走关系名 canonical 单点事实来源，
            # 废弃名（执行/属于/…）无映射 → 跳过（源候选保留，由人工裁决后重新进入）。
            if src_n and tgt_n and rel:
                from core.relmap import canonical as _rel_canon
                _c = _rel_canon(rel)
                if _c is None:
                    staged["skipped_deprecated"] = staged.get("skipped_deprecated", 0) + 1
                    continue
                rel = _c
            if src_n and tgt_n and rel:
                _add_triple(conn, src_n, src_n, rel, tgt_n, tgt_n, "entity",
                            confidence=float(c["confidence"] or 1.0), status="pending",
                            source_doc=src_doc,
                            source_chunk=str(c["chunk_id"] or ""),
                            source_type="ai_generated" if svid else "vector",
                            sysml_version_id=svid, created_by=operator)
                staged["relations"] += 1
                staged["triples"] += 1
            continue
        # 实体候选 → type + 属性三元组
        _add_triple(conn, subj_id, name, "type", etype, etype, "literal",
                    confidence=float(c["confidence"] or 1.0), status="pending",
                    source_doc=src_doc,
                    source_chunk=str(c["chunk_id"] or ""),
                    source_type="ai_generated" if svid else "vector",
                    sysml_version_id=svid, created_by=operator)
        staged["entities"] += 1
        staged["triples"] += 1
        for k, v in (props or {}).items():
            if v is None or v == "":
                continue
            _add_triple(conn, subj_id, name, str(k), subj_id, str(v), "literal",
                        confidence=float(c["confidence"] or 1.0), status="pending",
                        source_doc=src_doc,
                        source_chunk=str(c["chunk_id"] or ""),
                        source_type="ai_generated" if svid else "vector",
                        sysml_version_id=svid, created_by=operator)
            staged["triples"] += 1
        if c["chunk_id"] and linked_chunks is not None:
            linked_chunks.add(int(c["chunk_id"]))
    conn.commit()
    return staged


def commit_approved_triples(conn, operator: str = "知识工程师", limit: int = 500) -> dict:
    """已审核(approved)且未落图(graph_stored=0)的三元组 → 反写 entities/relations。"""
    from ontology_semantics import GraphStore
    gs = GraphStore(conn)
    from triple_store import triple_id as _tid  # noqa: F401
    approved = conn.execute(
        "SELECT * FROM triples WHERE status='approved' AND graph_stored=0 ORDER BY id LIMIT ?",
        (limit,)).fetchall()
    if not approved:
        return {"processed": 0, "entities": 0, "relations": 0}
    # Phase1：实体（type 三元组）→ create_node，收集 (name,type)→eid
    name_to_id = {}
    created_entities = []
    created_relations = []
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for t in approved:
            if t["predicate"] != "type":
                continue
            ename = t["subject_name"]
            etype = t["object_value"] or "实体"   # type 三元组 object_value = 实体类型
            # 判重：同名同类型已存在 → 复用
            exist = conn.execute(
                "SELECT id FROM entities WHERE name=? AND entity_type=? AND status!='deprecated' LIMIT 1",
                (ename, etype)).fetchone()
            if exist:
                eid = exist["id"]
            else:
                nid = f"V2G-{uuid.uuid4().hex[:8]}"
                # P0-5 修正（2026-09-06）：建实体时预组装同主体已审属性三元组。
                # 原实现传空 props，而属性在 Phase2 才消费 → 有必填属性的本体类
                # （如 [天线] 缺少必填属性）全部落图失败，属时序颠倒的真 bug。
                ent_props = {}
                for t2 in approved:
                    if (t2["predicate"] != "type" and t2["object_type"] == "literal"
                            and t2["subject_name"] == ename):
                        ent_props[str(t2["predicate"])] = t2["object_value"]
                ok, res = gs.create_node(nid, ename, etype, ent_props, "personal",
                                         source_doc=t["source_doc"] or "",
                                         source_type=t["source_type"] or "vector",
                                         created_by=operator)
                if not ok:
                    logger.warning("triple_commit 建实体失败: %s %s", ename, res)
                    continue
                if t["sysml_version_id"]:
                    conn.execute("UPDATE entities SET sysml_version_id=? WHERE id=? AND branch='personal'",
                                 (t["sysml_version_id"], nid))
                eid = nid
                created_entities.append(nid)
            name_to_id.setdefault(ename, eid)
            conn.execute(
                "UPDATE triples SET subject_id=?, graph_entity_id=?, graph_stored=1 WHERE id=?",
                (eid, eid, t["id"]))
        # 属性三元组：补实体 properties
        for t in approved:
            if t["predicate"] == "type":
                continue
            if t["object_type"] != "literal":
                continue
            eid = name_to_id.get(t["subject_name"]) or t["subject_id"] or ""
            if not eid:
                # 尝试按 subject_id（即候选id）查
                row = conn.execute(
                    "SELECT graph_stored FROM triples WHERE id=? AND predicate='type' AND subject_id=?",
                    (t["id"], t["subject_id"])).fetchone() if False else None
                continue
            ent = conn.execute("SELECT properties FROM entities WHERE id=? AND status!='deprecated' LIMIT 1",
                               (eid,)).fetchone()
            if not ent:
                continue
            prop = {}
            try:
                prop = json.loads(ent["properties"] or "{}") if ent["properties"] else {}
            except Exception:
                prop = {}
            prop[str(t["predicate"])] = t["object_value"]
            conn.execute("UPDATE entities SET properties=? WHERE id=?", (json.dumps(prop, ensure_ascii=False), eid))
            conn.execute("UPDATE triples SET graph_entity_id=?, graph_stored=1 WHERE id=?", (eid, t["id"]))
        # Phase2：关系三元组
        for t in approved:
            if t["object_type"] != "entity":
                continue
            src_id = name_to_id.get(t["subject_name"]) or ""
            tgt_id = name_to_id.get(t["object_value"]) or ""
            # P0-5 补（2026-09-06）：跨批次解析——两端实体在**先前批次**已落图时
            # name_to_id（仅本批）查不到 → 关系被静默跳过且永远无法落图。
            # 回退按名查 personal 分支非弃用实体（取最新；同名唯一性由上游判重保障）。
            if not src_id:
                row = conn.execute(
                    "SELECT id FROM entities WHERE name=? AND branch='personal' AND status!='deprecated' "
                    "ORDER BY id DESC LIMIT 1", (t["subject_name"],)).fetchone()
                src_id = row["id"] if row else ""
            if not tgt_id:
                row = conn.execute(
                    "SELECT id FROM entities WHERE name=? AND branch='personal' AND status!='deprecated' "
                    "ORDER BY id DESC LIMIT 1", (t["object_value"],)).fetchone()
                tgt_id = row["id"] if row else ""
            if not src_id or not tgt_id:
                continue
            ok, res = gs.create_edge(src_id, tgt_id, t["predicate"], {"auto": True}, "personal",
                                     source_doc=t["source_doc"] or "", created_by=operator)
            if ok:
                # P0-5 落图回链：三元组 ↔ 关系行双向可追溯
                conn.execute("UPDATE triples SET graph_entity_id=?, graph_relation_id=?, graph_stored=1 WHERE id=?",
                             (src_id, str(res), t["id"]))
                created_relations.append(t["id"])
        # 提交打点（图谱变更）
        try:
            from repositories.commit_repo import CommitRepo
            changes, snap = {}, {}
            if created_entities:
                changes["entities"] = created_entities
                ph = ",".join("?" * len(created_entities))
                snap["entities"] = [dict(r) for r in conn.execute(
                    f"SELECT id, name, entity_type, status FROM entities WHERE id IN ({ph})",
                    created_entities).fetchall()]
            CommitRepo(conn).create_commit(
                "personal", "import",
                f"三元组统一审核落地 {len(created_entities)} 实体/{len(created_relations)} 关系",
                changes, snap, actor=operator)
        except Exception as e:  # noqa: BLE001
            logger.warning("triple_commit 提交打点失败（不阻断）: %s", e)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        logger.error("triple_commit 异常，已回滚: %s", e)
        return {"processed": len(approved), "entities": 0, "relations": 0, "error": str(e)}
    # P0-①：落图后 best-effort 触发发布门禁（draft_flow=False 时 no-op，保持发布手动现状）
    try:
        if approved and any(int(t["sysml_version_id"] or 0) > 0 for t in approved):
            from services.ingest_gate import submit_ingest_batch
            gate = submit_ingest_batch(conn, batch_id="triple-batch", source_type="ai_generated",
                                       operator=operator, detail="三元组落图批次发布待审")
            if gate and gate.get("enabled"):
                logger.info("triple_commit 发布门禁：status=%s mr_id=%s",
                            gate.get("status"), gate.get("mr_id"))
    except Exception as _ge:  # noqa: BLE001
        logger.warning("triple_commit 发布门禁触发失败（不阻断落图）: %s", _ge)
    return {"processed": len(approved), "entities": len(created_entities),
            "relations": len(created_relations)}