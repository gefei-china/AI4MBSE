# -*- coding: utf-8 -*-
"""Ontology Query Service（2026-11-24 第二期「用得上」）。

文章《从 0 到 1 构建企业级 AI Agent》的运行期主张：
  Agent 调**一个**查询服务，按对象身份拿到属性 + 关系 + 约束，
  而不是靠名称相似度猜、也不是把 schema 全文塞进 prompt。

设计纪律：
  1. 找不到对象返回 found=False，**不编造**（对齐文章「动态判断交规则引擎」）；
  2. 关系遍历深度硬上限 2，防环；同一对象只出现一次；
  3. 权威源是 SQLite（entities/relations/ontology_types），图库是投影，
     不依赖 graph_db.enabled —— 默认关闭时本服务照常可用；
  4. 每条关系带 provenance（来源表），便于 Agent 解释依据。
"""
from __future__ import annotations

import json
from typing import Any

MAX_DEPTH = 2


def _loads(v: Any, default=None):
    if not v:
        return default if default is not None else {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default if default is not None else {}


def _find_entity(conn, obj_id: str, branch: str = "") -> dict | None:
    """按 id 精确 / name 精确 / name LIKE 三级定位，取最新一条。"""
    if not obj_id:
        return None
    conds, params = ["status != 'deprecated'"], []
    # 精确优先：id 或 name
    row = conn.execute(
        "SELECT * FROM entities WHERE (id=? OR name=?) AND status!='deprecated' "
        + ("AND branch=? " if branch else "") + "ORDER BY is_current DESC, created_at DESC LIMIT 1",
        (obj_id, obj_id, branch) if branch else (obj_id, obj_id)).fetchone()
    if row:
        return dict(row)
    # 名称包含（唯一命中才采用，避免歧义）
    rows = conn.execute(
        "SELECT * FROM entities WHERE name LIKE ? AND status!='deprecated' "
        + ("AND branch=? " if branch else "") + "LIMIT 5",
        (f"%{obj_id}%", branch) if branch else (f"%{obj_id}%",)).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    return None


def _type_constraints(conn, type_name: str) -> dict:
    row = conn.execute(
        "SELECT name, type_kind, properties, constraints, parent_id, description "
        "FROM ontology_types WHERE name=? AND status!='deprecated' LIMIT 1", (type_name,)).fetchone()
    if not row:
        return {"registered": False, "name": type_name}
    return {
        "registered": True,
        "name": row["name"],
        "kind": row["type_kind"],
        "parent": row["parent_id"] or "",
        "description": row["description"] or "",
        "properties": _loads(row["properties"]),
        "constraints": _loads(row["constraints"]),
    }


def _relations_of(conn, entity_id: str, branch: str = "") -> list[dict]:
    """取该实体为源或目标的已审关系，附对端名称与本体注册状态。"""
    br = " AND r.branch=?" if branch else ""
    params = [entity_id, entity_id] + ([branch] if branch else [])
    rows = conn.execute(
        "SELECT r.id, r.source_id, r.target_id, r.relation_type, r.properties, r.status "
        "FROM relations r WHERE r.status!='deprecated' "
        f"AND (r.source_id=? OR r.target_id=?)" + br + " LIMIT 100", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        other = d["target_id"] if d["source_id"] == entity_id else d["source_id"]
        nr = conn.execute("SELECT name, entity_type FROM entities WHERE id=? LIMIT 1", (other,)).fetchone()
        out.append({
            "relation_id": d["id"],
            "relation_type": d["relation_type"],
            "direction": "out" if d["source_id"] == entity_id else "in",
            "other_id": other,
            "other_name": nr["name"] if nr else other,
            "other_type": nr["entity_type"] if nr else "",
            "other_registered": bool(_type_constraints(conn, nr["entity_type"]).get("registered")) if nr else False,
            "properties": _loads(d["properties"]),
            "provenance": "relations",
        })
    return out


def _triples_of(conn, entity_id: str, entity_name: str) -> list[dict]:
    """取该实体相关的已审三元组（AI 确认后的增量事实）。"""
    rows = conn.execute(
        "SELECT subject_id, subject_name, predicate, object_id, object_value, object_type "
        "FROM triples WHERE status='approved' AND (subject_id=? OR object_id=?) LIMIT 100",
        (entity_id, entity_id)).fetchall()
    return [{
        "subject": r["subject_name"] or r["subject_id"],
        "predicate": r["predicate"],
        "object": r["object_value"] or r["object_id"],
        "object_type": r["object_type"],
        "provenance": "triples(approved)",
    } for r in rows]


def get_context(conn, obj_id: str, branch: str = "", depth: int = 1) -> dict:
    """按对象身份返回统一业务上下文。

    Returns:
      {found, object:{...}, attributes, relations, triples, type_constraints,
       neighbours:[{id,name,entity_type}], depth, provenance}
      找不到 ⇒ {found: False, query: obj_id}，调用方须回落、不得编造。
    """
    d = max(0, min(int(depth or 1), MAX_DEPTH))
    ent = _find_entity(conn, obj_id, branch)
    if not ent:
        return {"found": False, "query": obj_id, "depth": d,
                "message": "对象不存在，不得据此生成关系或属性"}

    attrs = _loads(ent.get("properties"), {})
    rels = _relations_of(conn, ent["id"], branch)
    tris = _triples_of(conn, ent["id"], ent["name"])
    tc = _type_constraints(conn, ent.get("entity_type") or "")

    neighbours, seen = [], {ent["id"]}
    if d >= 1:
        for r in rels:
            oid = r["other_id"]
            if oid and oid not in seen:
                seen.add(oid)
                neighbours.append({"id": oid, "name": r["other_name"],
                                   "entity_type": r["other_type"],
                                   "via": r["relation_type"], "direction": r["direction"]})
    if d >= 2:
        for nb in list(neighbours):
            for r in _relations_of(conn, nb["id"], branch):
                oid = r["other_id"]
                if oid and oid not in seen:
                    seen.add(oid)
                    neighbours.append({"id": oid, "name": r["other_name"],
                                       "entity_type": r["other_type"],
                                       "via": f"{nb['name']} -{r['relation_type']}->",
                                       "direction": r["direction"]})

    return {
        "found": True,
        "object": {
            "id": ent["id"], "name": ent["name"],
            "entity_type": ent.get("entity_type") or "",
            "branch": ent.get("branch") or "",
            "status": ent.get("status") or "",
            "source_doc": ent.get("source_doc") or "",
        },
        "attributes": attrs,
        "relations": rels,
        "triples": tris,
        "type_constraints": tc,
        "neighbours": neighbours,
        "depth": d,
        "provenance": {"authoritative_source": "SQLite", "graph_mirror": "投影，非权威源"},
    }
