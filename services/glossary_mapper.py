# -*- coding: utf-8 -*-
"""术语概念 → 本体类型 映射候选（2026-11-24 第四期「建得准」）。

产品纪律：**不自动写入**。系统只生成带相似度与依据的候选清单，
人工逐条确认后才通过 apply_mapping 落 maps_to_class。
理由：order_status=3 这类领域码值无法凭名称推断（文章明确提醒），
映射错了比没映射更危险。
"""
from __future__ import annotations

import json
from typing import Any


def _loads(v: Any, default=None):
    if not v:
        return default if default is not None else []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default if default is not None else []


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _sim(a: str, b: str) -> float:
    """字符 bigram Jaccard（无外部依赖，离线可跑）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return round(len(ga & gb) / len(ga | gb), 3)


def candidates(conn, limit: int = 5) -> dict:
    """为 maps_to_class 为空的概念生成候选（不写库）。

    Returns {total_empty, with_candidates, items:[{concept_id, pref_label, candidates:[...]}]}
    """
    rows = conn.execute(
        "SELECT concept_id, pref_label, definition, domain, maps_to_class "
        "FROM glossary_concepts WHERE maps_to_class IS NULL OR maps_to_class='' "
        "ORDER BY pref_label").fetchall()
    types = conn.execute(
        "SELECT name, type_kind, description, properties FROM ontology_types "
        "WHERE status!='deprecated'").fetchall()
    scored_types = []
    for t in types:
        t = dict(t)
        props = _loads(t.get("properties"), {})
        labels = {t["name"]}
        for k in ("alt_labels", "aliases", "label", "name"):
            v = props.get(k) if isinstance(props, dict) else None
            if isinstance(v, str):
                labels.add(v)
            elif isinstance(v, list):
                labels.update(str(x) for x in v)
        scored_types.append({"name": t["name"], "kind": t["type_kind"],
                             "description": t.get("description") or "", "labels": labels})
    items, covered = [], 0
    for r in rows:
        d = dict(r)
        cands = []
        for t in scored_types:
            best = max((_sim(d["pref_label"], lb) for lb in t["labels"]), default=0.0)
            if best > 0:
                cands.append({"name": t["name"], "kind": t["kind"],
                              "description": t["description"], "score": best})
        cands.sort(key=lambda x: -x["score"])
        cands = cands[:limit]
        if cands:
            covered += 1
        items.append({"concept_id": d["concept_id"], "pref_label": d["pref_label"],
                      "definition": d.get("definition") or "", "domain": d.get("domain") or "",
                      "candidates": cands})
    return {"total_empty": len(rows), "with_candidates": covered, "items": items}


def apply_mapping(conn, pairs: list[dict], operator: str = "system") -> dict:
    """人工确认后批量写入。pairs: [{concept_id, class_name}]。

    校验：概念存在、当前无映射或 allow_overwrite=True、本体类型已注册。
    只写 maps_to_class（不动 maps_to_prop/maps_to_inst），留痕到 note。
    """
    if not isinstance(pairs, list):
        return {"ok": False, "error": "pairs 须为列表"}
    ok, rejected = 0, []
    for p in pairs:
        cid = str((p or {}).get("concept_id") or "").strip()
        cls = str((p or {}).get("class_name") or "").strip()
        allow = bool((p or {}).get("allow_overwrite"))
        row = conn.execute("SELECT pref_label, maps_to_class FROM glossary_concepts WHERE concept_id=?",
                           (cid,)).fetchone()
        if not row:
            rejected.append({"concept_id": cid, "reason": "概念不存在"})
            continue
        if row["maps_to_class"] and not allow:
            rejected.append({"concept_id": cid, "reason": "已有映射，需 allow_overwrite=True"})
            continue
        t = conn.execute("SELECT name FROM ontology_types WHERE name=? AND status!='deprecated'",
                         (cls,)).fetchone()
        if not t:
            rejected.append({"concept_id": cid, "reason": f"本体类型 {cls} 未注册"})
            continue
        conn.execute(
            "UPDATE glossary_concepts SET maps_to_class=?, updated_at=datetime('now'), "
            "note=COALESCE(note,'') || ? WHERE concept_id=?",
            (cls, f" [2026-11-24 映射确认 by {operator}: {cls}]", cid))
        ok += 1
    conn.commit()
    return {"ok": True, "applied": ok, "rejected": rejected,
            "mapped_total": conn.execute(
                "SELECT COUNT(*) FROM glossary_concepts WHERE maps_to_class IS NOT NULL "
                "AND maps_to_class!=''").fetchone()[0]}
