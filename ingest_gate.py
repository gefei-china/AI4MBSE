# -*- coding: utf-8 -*-
"""SHACL 入库门禁（P1-1）。

调研结论：OWL 基数被违反 → 推理机判"本体不一致"（核弹）；SHACL 违反 →
报"这条数据不合规"（手术刀）。所以数据校验走 SHACL，OWL 只承载推理公理。

模式（settings 表 key=shacl_gate_mode）：
- off    ：不校验
- warn   ：违规记录到 shacl_findings，流程继续（默认；先观察 2 周违规分布）
- enforce：违规阻断入库/审批，数据留在待办队列

依赖：rdflib + pyshacl（项目 .venv）。缺失时优雅降级为 off 并标注 reason。
"""
import json
import logging
import time

logger = logging.getLogger(__name__)

GATE_MODE_KEY = "shacl_gate_mode"

# 模块级形状缓存：本体很少变，避免每次审批都重新导出+解析
_shapes_cache = {"text": None, "graph": None, "ts": 0.0, "sig": ""}
_SHAPES_TTL = 300  # 秒


def gate_mode(conn) -> str:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (GATE_MODE_KEY,)).fetchone()
        m = (row["value"] if row else "") or "warn"
        return m if m in ("off", "warn", "enforce") else "warn"
    except Exception:
        return "warn"


def set_gate_mode(conn, mode: str) -> bool:
    if mode not in ("off", "warn", "enforce"):
        return False
    try:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (GATE_MODE_KEY, mode))
        conn.commit()
        return True
    except Exception:
        try:  # 老表无 upsert 语法时
            conn.execute("UPDATE settings SET value=? WHERE key=?", (mode, GATE_MODE_KEY))
            if conn.total_changes:
                conn.execute("INSERT INTO settings (key, value) VALUES (?,?)", (GATE_MODE_KEY, mode))
            conn.commit()
        except Exception:
            pass
        return True


def _shapes_graph(conn):
    """SHACL 形状图（带签名缓存：本体类型数量变化即失效）。"""
    from ontology_semantics import OntologyValidator
    _row = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),'') FROM ontology_types").fetchone()
    sig = f"{_row[0]}:{_row[1]}"   # 含约束/基数变更时间：PUT 约束后缓存立即失效
    if _shapes_cache["graph"] is not None and _shapes_cache["sig"] == sig \
            and time.time() - _shapes_cache["ts"] < _SHAPES_TTL:
        return _shapes_cache["graph"]
    text = OntologyValidator(conn).shacl_export()
    if not text.strip():
        return None
    from rdflib import Graph
    g = Graph()
    g.parse(data=text, format="turtle")
    _shapes_cache.update({"text": text, "graph": g, "ts": time.time(), "sig": sig})
    return g


def _load_data_graph(conn, triples: list | None = None):
    """数据图构建。triples=None 时从权威源（entities/relations）+ approved 三元组合并加载。

    IRI 规则与 core.ns / 图库一致：类=ontology#，实例=ent/，rdf:type 标准谓词。
    """
    from rdflib import Graph, URIRef, Literal
    from core import ns

    g = Graph()
    g.bind("ex", ns.NS_ONTOLOGY)
    g.bind("ent", ns.NS_ENT)

    def subj_uri(sid, sname):
        return URIRef(ns.ent_uri(sname, sid))

    if triples is None:
        # 权威源：entities + relations（triples 表是 AI 候选流，为空是常态）
        # 类层级公理并入数据图：SHACL 规范的 sh:class 依赖数据图内 rdfs:subClassOf 做子类闭包
        id2name = {}
        parent_of = {}
        for t in conn.execute("SELECT id, name, parent_id FROM ontology_types").fetchall():
            t = dict(t)
            id2name.setdefault(t["id"], t["name"])
            parent_of[t["name"]] = id2name.get(t["parent_id"]) if t["parent_id"] else None
        for child, parent in parent_of.items():
            if parent:
                g.add((URIRef(ns.class_uri(child)), URIRef(ns.RDFS_NS + "subClassOf"),
                       URIRef(ns.class_uri(parent))))
        for e in conn.execute("SELECT id, entity_type, properties FROM entities "
                              "WHERE status!='deprecated'").fetchall():
            e = dict(e)
            s = subj_uri(e["id"], e["id"])
            if e.get("entity_type"):
                g.add((s, URIRef(ns.RDF_TYPE), URIRef(ns.class_uri(e["entity_type"]))))
            try:
                props = json.loads(e.get("properties") or "{}")
            except Exception:
                props = {}
            for k, v in (props or {}).items():
                if v is None or v == "":
                    continue
                g.add((s, URIRef(ns.pred_uri(str(k), "literal")), Literal(str(v))))
        for r in conn.execute("SELECT source_id, target_id, relation_type FROM relations "
                              "WHERE status!='deprecated'").fetchall():
            r = dict(r)
            g.add((subj_uri(r["source_id"], r["source_id"]),
                   URIRef(ns.pred_uri(str(r["relation_type"] or "relatedTo"), "entity")),
                   subj_uri(r["target_id"], r["target_id"])))
        # approved 三元组并入（覆盖 AI 确认后的增量事实）
        for t in conn.execute(
                "SELECT subject_id, subject_name, predicate, object_id, object_value, object_type "
                "FROM triples WHERE status='approved'").fetchall():
            d = dict(t)
            s = subj_uri(d.get("subject_id") or "", d.get("subject_name") or "")
            pred = str(d.get("predicate") or "").strip()
            otype = str(d.get("object_type") or "literal")
            if pred in ("a", "type", "rdf:type"):
                g.add((s, URIRef(ns.RDF_TYPE),
                       URIRef(ns.class_uri(d.get("object_value") or ""))))
            elif otype == "entity":
                g.add((s, URIRef(ns.pred_uri(pred, "entity")),
                       subj_uri(d.get("object_id") or "", d.get("object_value") or "")))
            else:
                g.add((s, URIRef(ns.pred_uri(pred, "literal")),
                       Literal(str(d.get("object_value") or ""))))
        return g

    # 显式传入三元组列表（门禁单条/批量审批场景）
    for d in triples:
        if not isinstance(d, dict):
            d = dict(d)
        s = subj_uri(d.get("subject_id") or "", d.get("subject_name") or "")
        pred = str(d.get("predicate") or "").strip()
        otype = str(d.get("object_type") or "literal")
        if pred in ("a", "type", "rdf:type"):
            p = URIRef(ns.RDF_TYPE)
            o = URIRef(ns.class_uri(d.get("object_value") or d.get("o_value") or ""))
        elif otype == "entity":
            p = URIRef(ns.pred_uri(pred, "entity"))
            o = subj_uri(d.get("object_id") or "", d.get("object_value") or "")
        else:
            p = URIRef(ns.pred_uri(pred, "literal"))
            o = Literal(str(d.get("object_value") if d.get("object_value") is not None else ""))
        g.add((s, p, o))
    return g


def check_triples(conn, triples: list | None = None, batch_id: str = "",
                  record: bool = True, extra_triples: list | None = None) -> dict:
    """SHACL 校验。返回 {mode, skipped, conforms, violations:[...], total_checked}。

    triples=None → 全量权威源（entities/relations + approved 三元组）；
    extra_triples → 待审候选三元组（enforce 门禁必须把它们并入数据图，
    否则"批准违规候选"拦不住——全量图只含 approved，看不到候选）。
    violations 每条：{subject, severity, message, path}。
    record=True 时写入 shacl_findings（供治理看板消费）。
    """
    mode = gate_mode(conn)
    out = {"mode": mode, "skipped": False, "conforms": None,
           "violations": [], "total_checked": 0}
    if mode == "off":
        out["skipped"] = True
        out["reason"] = "gate_mode=off"
        return out
    try:
        import pyshacl  # noqa: F401
    except ImportError:
        out["skipped"] = True
        out["reason"] = "pyshacl 未安装（.venv），门禁降级为 off"
        logger.warning("SHACL gate skipped: pyshacl not installed")
        return out

    shapes = _shapes_graph(conn)
    if shapes is None or len(shapes) == 0:
        out["skipped"] = True
        out["reason"] = "本体形状为空（ontology_types 无 entity 类型）"
        return out

    from pyshacl import validate
    data = _load_data_graph(conn, triples)
    # 候选三元组并入数据图（审批门禁场景：候选还没进权威源，必须显式带上）
    if extra_triples:
        data += _load_data_graph(conn, extra_triples)
    out["total_checked"] = len(data)
    conforms, results_graph, _report_text = validate(
        data_graph=data, shacl_graph=shapes,
        inference="none", abort_on_first=False, allow_warnings=True)
    out["conforms"] = bool(conforms)

    # results_graph 本身就是 rdflib Graph（不要解析人读文本 results_text）
    violations = []
    try:
        from rdflib.namespace import RDF, SH
        rg = results_graph
        for rc in rg.subjects(RDF.type, SH.ValidationResult):
            sev = str(rg.value(rc, SH.resultSeverity) or "")
            msg = str(rg.value(rc, SH.resultMessage) or "")
            focus = rg.value(rc, SH.focusNode)
            path = rg.value(rc, SH.resultPath)
            violations.append({
                "subject": str(focus) if focus is not None else "",
                "severity": sev.split("#")[-1] if sev else "Violation",
                "message": msg[:300],
                "path": str(path).split("#")[-1] if path is not None else "",
            })
    except Exception as e:  # 报告解析失败不影响 conforms 判定
        logger.warning("SHACL report parse failed: %s", e)
    out["violations"] = violations

    if record and violations:
        try:
            for v in violations:
                conn.execute(
                    "INSERT INTO shacl_findings (batch_id, subject, severity, message, gate_mode) "
                    "VALUES (?,?,?,?,?)",
                    (batch_id, v["subject"], v["severity"], v["message"], mode))
            conn.commit()
        except Exception:
            logger.warning("shacl_findings write failed", exc_info=True)
    return out


def gate_approval(conn, triple_ids: list, batch_id: str = "") -> dict:
    """审批通过前的门禁检查。enforce 模式下候选三元组自身产生 Violation → 拒绝。

    候选三元组必须并入数据图（extra_triples）——它们还没进权威源，
    全量图只含 approved 数据，不带候选就永远拦不住违规候选。
    且需带上同主体的全部 pending 候选：审批关系行时，类型行（另一条 pending）
    不带入就没有 rdf:type → 无形状命中 → 违规漏检。
    返回 {allowed: bool, mode, violations, blocking_n}。
    """
    cand_subs, cand_rows = set(), []
    for tid in triple_ids:
        row = conn.execute(
            "SELECT subject_id, subject_name, predicate, object_id, object_value, object_type "
            "FROM triples WHERE triple_id=?", (tid,)).fetchone()
        if not row:
            continue
        cand_rows.append(dict(row))
        cand_subs.add(row["subject_name"] or row["subject_id"] or "")
    if cand_subs:
        ph = ",".join("?" * len(cand_subs))
        for r in conn.execute(
                f"SELECT subject_id, subject_name, predicate, object_id, object_value, object_type "
                f"FROM triples WHERE status IN ('pending','submitted') AND subject_name IN ({ph})",
                list(cand_subs)).fetchall():
            d = dict(r)
            if d not in cand_rows:
                cand_rows.append(d)
    res = check_triples(conn, triples=None, extra_triples=cand_rows,
                        batch_id=batch_id, record=True)
    blocking = 0
    if not res["skipped"]:
        from core import ns
        want = {ns.ent_uri(c["subject_name"] or "", c["subject_id"] or "") for c in cand_rows}
        blocking = sum(1 for v in res["violations"]
                       if v["severity"] == "Violation" and v["subject"] in want)
    return {
        "allowed": res["skipped"] or res["mode"] != "enforce" or blocking == 0,
        "mode": res["mode"],
        "violations": res["violations"],
        "blocking_n": blocking,
        "skipped": res["skipped"],
        "reason": res.get("reason", ""),
    }
