"""P3-②（2026-09-11）时态 SPARQL 扩展：as-of 查询代理。"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

logger = __import__("logging").getLogger(__name__)

_ASOF_VAR = re.compile(r"\?asOf\b", re.IGNORECASE)
_NOW_VAR = re.compile(r"\?now\b", re.IGNORECASE)


def asOfProxy(sparql: str, as_of: str) -> str:
    if not as_of:
        return sparql
    if "T" not in as_of:
        as_of = as_of + "T00:00:00"
    lit = '"' + as_of + '"^^<http://www.w3.org/2001/XMLSchema#dateTime>'
    sparql = _ASOF_VAR.sub(lit, sparql)
    return sparql


def nowProxy(sparql: str, now=None) -> str:
    if now is None:
        from datetime import datetime
        now = datetime.utcnow().isoformat() + "Z"
    if "T" not in now:
        now = now + "T00:00:00"
    lit = '"' + now + '"^^<http://www.w3.org/2001/XMLSchema#dateTime>'
    return _NOW_VAR.sub(lit, sparql)


def build_valid_intervals_ttl(conn) -> str:
    """实体时态 → 严格 N-Triples（W3C Time Ontology，真实 bnode）。"""
    rows = conn.execute(
        "SELECT id, valid_from, valid_to FROM entities "
        "WHERE is_current=1 AND status<>'deprecated' "
        "AND (valid_from IS NOT NULL OR valid_to IS NOT NULL)"
    ).fetchall()
    RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    XSD_DT = "<http://www.w3.org/2001/XMLSchema#dateTime>"
    T_INTERVAL = "<http://www.w3.org/2006/time#Interval>"
    T_INSTANT = "<http://www.w3.org/2006/time#Instant>"
    T_BEGIN = "<http://www.w3.org/2006/time#hasBeginning>"
    T_END = "<http://www.w3.org/2006/time#hasEnd>"
    T_INXSD = "<http://www.w3.org/2006/time#inXSDDateTime>"
    HAS_VALID = "<http://mbse/ontology/hasValidInterval>"
    lines = []
    for i, r in enumerate(rows):
        d = dict(r) if hasattr(r, "keys") else {"id": r[0], "valid_from": r[1], "valid_to": r[2]}
        eid = d.get("id", "")
        if not eid:
            continue
        ent = "<http://mbse/ontology/" + eid + ">"
        inv = "<http://mbse/ontology/" + eid + "/interval>"
        # N-Triples bnode label 只允许 [A-Za-z0-9_]；实体 id 常含 '-中文' 等 → 清洗
        safe = re.sub(r"[^A-Za-z0-9_]", "_", eid)[:20]
        ba = "_:" + safe + "_ba"
        bb = "_:" + safe + "_bb"
        vf = (d.get("valid_from") or "").replace(" ", "T") or "1970-01-01T00:00:00Z"
        vt = (d.get("valid_to") or "").replace(" ", "T") or "9999-12-31T23:59:59Z"
        lines.append(ent + " " + HAS_VALID + " " + inv + " .")
        lines.append(inv + " " + RDF_TYPE + " " + T_INTERVAL + " .")
        lines.append(inv + " " + T_BEGIN + " " + ba + " .")
        lines.append(ba + " " + RDF_TYPE + " " + T_INSTANT + " .")
        lines.append(ba + " " + T_INXSD + " " + '"' + vf + '"^^' + XSD_DT + " .")
        lines.append(inv + " " + T_END + " " + bb + " .")
        lines.append(bb + " " + RDF_TYPE + " " + T_INSTANT + " .")
        lines.append(bb + " " + T_INXSD + " " + '"' + vt + '"^^' + XSD_DT + " .")
    return "\n".join(lines)


def sync_to_fuseki_with_time(
    conn,
    base_url: str = "http://localhost:3030",
    dataset: str = "mbse",
    project_id=None,
    timeout: float = 30.0,
) -> dict:
    import time as _time
    from services.sparql_sync import (  # noqa: F401
        FusekiClient, FusekiUnavailable,
        build_ontology_ttl, build_entities_ttl,
        _build_insert,
        _strip_turtle_prefixes,
        _chunk_lines,
    )
    started = _time.time()
    cli = FusekiClient(base_url=base_url, dataset=dataset)
    if not cli.ping(timeout=5.0):
        return {"ok": False, "error": "Fuseki 不可达: " + base_url,
                "duration_ms": int((_time.time() - started) * 1000)}
    if not cli.dataset_exists():
        if not cli.create_dataset(timeout=timeout):
            return {"ok": False, "error": "无法创建 dataset: " + dataset,
                    "duration_ms": int((_time.time() - started) * 1000)}
    ttl = (
        "@prefix time: <http://www.w3.org/2006/time#> .\n"
        + build_ontology_ttl(conn)
        + "\n" + build_entities_ttl(conn, project_id)
        + "\n" + build_valid_intervals_ttl(conn)
    )
    ntriples = _strip_turtle_prefixes(ttl)
    try:
        cli.update("CLEAR DEFAULT", timeout=timeout)
        for chunk in _chunk_lines(ntriples):
            if chunk.strip():
                cli.update(_build_insert(chunk), timeout=timeout)
    except FusekiUnavailable as e:
        return {"ok": False, "error": "写入失败: " + str(e),
                "duration_ms": int((_time.time() - started) * 1000)}
    return {
        "ok": True,
        "triples": ttl.count("\n") // 5,
        "duration_ms": int((_time.time() - started) * 1000),
        "fuseki_url": base_url,
        "dataset": dataset,
        "has_time_ontology": True,
    }
