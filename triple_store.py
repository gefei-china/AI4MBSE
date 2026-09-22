# -*- coding: utf-8 -*-
"""三元组原子存储与审核（知识治理全链路优化 · 优化三）。

以 (subject, predicate, object) 为最小知识单元，替代"实体/关系分离入库
+ 关系等实体先落"的耦合模式，消除入库依赖。复用 vector2graph 候选确认上下文：

- 实体候选  → 类型三元组 (S, a, T) + 各属性三元组 (S, prop, value)
- 关系候选  → 关系三元组 (S, predicate, O)
- 幂等：triple_id = f"{sub}|{pred}|{obj}" UNIQUE，重复写入跳过。
- 审核：pending(待审) → approved(通过) / rejected(驳回)，审核动作以三元组为单位。

本模块只写 triples 原子表；写前融合闸（staging_fuse）、消歧标记沿用既有候选链路。
"""
import json
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)

RDF_TYPE = "a"  # rdf:type 三元组谓词


def _loc_key(name: str) -> str:
    """候选/实体名 → 稳定局部标识（用于三元组 subject/object 定位）。"""
    return re.sub(r"[^A-Za-z0-9_一-龥]", "_", str(name or "").strip())


def triple_id(subject, predicate, obj) -> str:
    """三元组唯一键：sub|pred|obj。相遇/重名场景由调用方决定 sub/obj 幂等键。"""
    return f"{_loc_key(subject)}|{predicate.strip()}|{_loc_key(obj)}"


def _normalize_obj_type(raw: str) -> str:
    res = (raw or "").strip().lower()
    if res in ("entity", "relation"):
        return "entity"
    if res in ("attribute", "attr"):
        return "attribute"
    return "literal"


def _add_triple(conn, subject_id, subject_name, predicate, object_id, object_value,
                object_type="literal", confidence=1.0, status="approved",
                source_doc="", source_chunk="", source_type="vector", sysml_version_id=0,
                created_by="system") -> str:
    """幂等写入单条三元组；返回 triple_id。已存在(approved/reviewed)则跳过。
    P0-5：source_chunk 记录原文级溯源（document_chunks.id），落图回链由构图层回填。"""
    subject_name = str(subject_name or "")
    obj_val = str(object_value if object_value is not None else "")
    # 实体型客体优先用 id 定位标识，无 id 用名
    obj_key = object_id or _loc_key(subject_name) + "_" + object_id  # 占位：正常场景必有 id
    obj = object_id
    key = triple_id(subject_name or subject_id, predicate, obj_val or obj or "")
    if not key:
        return ""
    exists = conn.execute(
        "SELECT id FROM triples WHERE triple_id=?", (key,)).fetchone()
    if exists:
        # P0-5：已存在但缺 chunk 溯源 → 增量补齐（不覆盖已有值）
        if source_chunk:
            conn.execute(
                "UPDATE triples SET source_chunk=? WHERE triple_id=? AND (source_chunk IS NULL OR source_chunk='')",
                (str(source_chunk), key))
        return key
    conn.execute(
        "INSERT INTO triples (triple_id, subject_id, subject_name, subject_type, predicate, "
        "object_id, object_value, object_type, confidence, status, source_doc, source_chunk, "
        "source_type, sysml_version_id, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, subject_id or subject_name, subject_name, "", predicate,
         object_id or "", obj_val, _normalize_obj_type(object_type),
         float(confidence or 1.0), status, source_doc or "", str(source_chunk or ""),
         source_type, int(sysml_version_id or 0), created_by))
    return key


def write_entity_triples(conn, entity_id, entity_name: str, entity_props: dict,
                         source_doc="", source_chunk="", source_type="vector",
                         sysml_version_id=0, status="approved", created_by="system",
                         entity_type="") -> list:
    """实体候选 → 类型 + 属性三元组。返回写入 triple_id 列表。
    entity_type 非空时类型三元组为 (S, type, entity_type)。"""
    keys = []
    _type_tgt = entity_type or entity_name
    keys.append(_add_triple(conn, entity_id, entity_name, "type", _type_tgt, _type_tgt, "literal",
                            confidence=1.0, status=status,
                            source_doc=source_doc, source_chunk=source_chunk,
                            source_type=source_type,
                            sysml_version_id=sysml_version_id, created_by=created_by))
    for k, v in (entity_props or {}).items():
        if v is None or v == "":
            continue
        keys.append(_add_triple(conn, entity_id, entity_name, str(k),
                                entity_id, str(v), "literal",
                                confidence=1.0, status=status,
                                source_doc=source_doc, source_chunk=source_chunk,
                                source_type=source_type,
                                sysml_version_id=sysml_version_id, created_by=created_by))
    return [k for k in keys if k]


def write_relation_triple(conn, subject_id, subject_name: str, predicate: str,
                          object_id, object_name: str, source_doc="", source_chunk="",
                          source_type="vector", sysml_version_id=0, status="approved",
                          created_by="system") -> str:
    """关系候选 → 关系三元组 (S, pred, O)。"""
    return _add_triple(conn, subject_id, subject_name, predicate, object_id, object_name,
                       "entity", confidence=1.0, status=status,
                       source_doc=source_doc, source_chunk=source_chunk,
                       source_type=source_type,
                       sysml_version_id=sysml_version_id, created_by=created_by)


def _dup_info(conn, d: dict) -> dict:
    """与已入图图谱判重：d 为三元组 dict。返回 {kind, dup_with, score}。
    - triple_repeat：同 (subject_name, predicate, object_value) 关系已存在
    - suspect：subject/object(实体) 名称与已有实体同名或近似(≥0.85)（可对齐）
    - none：新建
    """
    try:
        from entity_resolver import fuzzy_score
    except Exception:
        fuzzy_score = None
    sn = str(d.get("subject_name") or "")
    obj = str(d.get("object_value") or "")
    # 1) 三元组重复（关系型：查 relations 同 (subject, predicate, object)）
    if d.get("object_type") == "entity" and sn and d.get("predicate") and obj:
        row = conn.execute(
            "SELECT r.id, e.name AS subj FROM relations r JOIN entities e ON e.id=r.source_id "
            "WHERE r.relation_type=? AND e.name=? AND r.status!='deprecated' LIMIT 1",
            (d.get("predicate"), sn)).fetchone()
        tgt = conn.execute(
            "SELECT name FROM entities WHERE name=? LIMIT 1", (obj,)).fetchone()
        if row and tgt:
            return {"kind": "triple_repeat", "dup_with": f"{obj}（已存在 {d.get('predicate')}）", "score": 1.0}
    # 2) 实体对齐：subject_name 或 object_value(实体) 与已有实体同名/近似
    cands = []
    if sn:
        cands.append(sn)
    if d.get("object_type") == "entity" and obj:
        cands.append(obj)
    for name in cands:
        if not name:
            continue
        ex = conn.execute(
            "SELECT id, name FROM entities WHERE status!='deprecated' AND name=? LIMIT 1",
            (name,)).fetchone()
        if ex:
            return {"kind": "suspect", "dup_with": ex["name"], "score": 1.0}
        if fuzzy_score:
            hit = conn.execute(
                "SELECT name FROM entities WHERE status!='deprecated' LIMIT 300").fetchall()
            best, bs = None, 0.85
            for h in hit:
                sc = fuzzy_score(name, h["name"])
                if sc > bs:
                    bs, best = sc, h["name"]
            if best:
                return {"kind": "suspect", "dup_with": best, "score": round(bs, 2)}
    return {"kind": "none", "dup_with": "", "score": 0}


def review_queue(conn, status: str = "pending", limit: int = 200) -> list:
    """三元组审核队列（统一 S-P-O 视角，替代实体/关系双队列）。每条附加 dup_info 判重。"""
    rows = conn.execute(
        "SELECT * FROM triples WHERE status=? ORDER BY id DESC LIMIT ?",
        (status, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["object_type"] == "entity":
            label = f"{d['subject_name']} —{d['predicate']}→ {d['object_value'] or d['object_id']}"
        else:
            label = f"{d['subject_name']} —{d['predicate']}: {d['object_value']}"
        d["label"] = label
        d["dup_info"] = _dup_info(conn, d)
        out.append(d)
    return out


def review_triple(conn, triple_id: str, decision: str, note: str = "", reviewer: str = "system") -> dict:
    """单条三元组审核：decision ∈ approved | rejected。approved 时同步标记 graph_stored 由构图层消费。

    P1-1 SHACL 门禁：approve 前过 SHACL；enforce 模式下与该三元组相关的 Violation
    会阻断通过（数据留在待办，违规已记录 shacl_findings）。warn 模式只记录不阻断。
    """
    if decision == "approved":
        try:
            from ingest_gate import gate_approval
            gate = gate_approval(conn, [triple_id], batch_id=triple_id)
            if not gate["allowed"]:
                blocked = [v for v in gate["violations"]
                           if v["severity"] == "Violation"][:5]
                hint = "; ".join(v["message"][:80] for v in blocked) or "SHACL 违规"
                return {"ok": False, "updated": 0, "triple_id": triple_id,
                        "blocked_by": "shacl_gate",
                        "gate_mode": gate["mode"], "blocking_n": gate["blocking_n"],
                        "note": f"SHACL 门禁阻断（{gate['mode']}）: {hint}"}
        except Exception as _ge:  # noqa: BLE001  门禁故障不阻塞审核主流程
            logger.warning("SHACL 门禁异常，放行并记录: %s", _ge)
    cur = conn.execute(
        "UPDATE triples SET status=?, review_decision=?, review_note=?, reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP "
        "WHERE triple_id=? AND status IN ('pending','submitted')",
        (decision, decision, (note or "")[:200], reviewer, triple_id))
    conn.commit()
    # 2026-09-01：图数据库启用时，审核通过即触发物化消费链（triples → TDB 查询镜像）
    if cur.rowcount > 0 and decision == "approved":
        try:
            from core import config
            if config.as_bool("graph_db", "enabled"):
                from graph_db import sync_pending_triples, get_writer
                _w = get_writer(True, config.get("graph_db", "backend", "pyoxigraph"),
                                config.get("graph_db", "path", ""),
                                config.get("graph_db", "use_memory", False))
                try:
                    sync_pending_triples(conn, _w)
                finally:
                    _w.close()
        except Exception as _ge:  # noqa: BLE001
            logger.warning("审核后图库同步跳过（不影响审核结果）: %s", _ge)
    return {"ok": cur.rowcount > 0, "updated": cur.rowcount, "triple_id": triple_id}


def batch_review(conn, triple_ids: list, decision: str, note: str = "", reviewer: str = "system") -> dict:
    n, blocked = 0, []
    for tid in triple_ids:
        r = review_triple(conn, tid, decision, note, reviewer)
        if r.get("ok"):
            n += 1
        elif r.get("blocked_by") == "shacl_gate":
            blocked.append({"triple_id": tid, "blocking_n": r.get("blocking_n", 0),
                            "note": r.get("note", "")})
    return {"ok": True, "reviewed": n, "decision": decision, "shacl_blocked": blocked}


def stats(conn) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM triples GROUP BY status").fetchall()
    to_store = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE status='approved' AND graph_stored=0").fetchone()[0]
    d = {r["status"]: r["n"] for r in rows}
    d["to_store"] = to_store
    # 2026-09-22 补：已落图数（发布水位，graph_stored=1 即已写入图库镜像）
    d["stored"] = conn.execute(
        "SELECT COUNT(*) FROM triples WHERE status='approved' AND graph_stored=1").fetchone()[0]
    return d


def ingest_triples_batch(conn, subject_id, subject_name, predicate, object_id, object_value,
                         object_type="literal", sysml_version_id=0, batch_label="", operator="system"):
    """便捷入口：新候选确认后按三元组物化（供 submit-review 流程调用）。返回 {written, queue}。"""
    _add_triple(conn, subject_id, subject_name, predicate, object_id, object_value,
                object_type=object_type, confidence=1.0, status="pending",
                source_type="ai_generated" if sysml_version_id else "vector",
                sysml_version_id=sysml_version_id, created_by=operator)
    conn.commit()
    return {"written": 1, "status": "pending"}
