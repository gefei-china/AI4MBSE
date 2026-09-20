# -*- coding: utf-8 -*-
"""知识治理指标与发布（P1-3 / P1-4 / P1-7）。

职责：
1. 健康度指标（P1-7，7 项 + 扩展）：SHACL 违规、追溯覆盖度、孤立率、
   映射覆盖率、未决候选、低置信占比、弃用残留（+ 同形异义 / 镜像水位）
2. 本体发布快照（P1-3）：版本冻结 OWL + SHACL 产物，支撑"内核本体 + 领域包"
3. 双库一致性（P1-4）：图库镜像同步水位（graph_sync_state）

阈值口径（与方案文档 3.3 P1-7 一致）：
- 追溯覆盖度 ≥90%（goal ≥95%）
- 孤立率 <5%
- 映射覆盖率 ≥80%
- 弃用残留 =0
"""
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 需求类实体判定：entity_type 以"需求"结尾（系统需求/子系统需求/单元需求/…）
_REQ_SUFFIX = "需求"
# 验证/满足类关系（追溯覆盖度口径；SATISFIES 为满足、VERIFIED_BY 为验证）
# P1-2 归一（2026-09-06）：补 SATISFIES——数据全部用英文标准名，此前口径
# 漏计 SATISFIES 链导致追溯覆盖度被低估；中文为历史数据防御。
_VERIFY_RELS = {"VERIFIED_BY", "VERIFIES", "VERIFY", "SATISFIES",
                "满足", "验证", "验证用例"}


def _status(value, ok=None, warn=None, lower_better=False):
    """指标分级：ok（绿）/ warn（黄）/ alert（红）。lower_better=True 时值越小越好。"""
    if value is None:
        return "warn"
    if lower_better:
        if value == 0:
            return "ok"
        return "warn" if (warn is not None and value <= warn) else "alert"
    if ok is not None and value >= ok:
        return "ok"
    if warn is not None and value >= warn:
        return "warn"
    return "alert"


def compute_metrics(conn, run_shacl: bool = False) -> dict:
    """健康度指标计算。run_shacl=True 时现场跑一次 SHACL 门禁（约 1s）。"""
    items = []

    # 1. SHACL 违规密度（默认读最近一次门禁结果；run_shacl 现场跑）
    viol_n, last_run = None, ""
    if run_shacl:
        try:
            from ingest_gate import check_triples
            res = check_triples(conn, record=False)
            viol_n = len(res["violations"]) if not res["skipped"] else None
            last_run = f"现场校验 conforms={res['conforms']}"
        except Exception as e:
            logger.warning("metrics SHACL run failed: %s", e)
    if viol_n is None:
        try:
            row = conn.execute("SELECT COUNT(*) n, MAX(created_at) t FROM shacl_findings "
                               "WHERE severity='Violation' AND created_at > datetime('now','-7 days')"
                               ).fetchone()
            viol_n, last_run = row["n"], f"近7天记录 @{row['t'] or '无'}"
        except Exception:
            viol_n = None
    items.append({"key": "shacl_violations", "name": "SHACL 违规（近7天）",
                  "value": viol_n, "unit": "条", "threshold": "warn 模式下仅记录，发布前须=0",
                  "status": _status(viol_n, lower_better=True, warn=5), "note": last_run})

    # 2. 追溯覆盖度：有验证/满足链的需求 / 全部需求
    # P1-2 口径修正（2026-09-06）：需求既可以是链的 source（需求 TRACE/DERIVES 出边），
    # 也可以是 target（功能/部件 SATISFIES 指向需求、VERIFIED_BY 指向需求）——
    # 此前只统计 source 侧，需求被满足的链路全部漏计，覆盖度被系统性低估。
    req_total = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status!='deprecated' AND entity_type LIKE ?",
        (f"%{_REQ_SUFFIX}",)).fetchone()[0]
    ph = ",".join("?" * len(_VERIFY_RELS))
    req_covered = conn.execute(
        f"SELECT COUNT(*) FROM entities e WHERE e.status!='deprecated' AND e.entity_type LIKE ? "
        f"AND (EXISTS (SELECT 1 FROM relations r WHERE r.source_id=e.id AND r.status!='deprecated' "
        f"AND r.relation_type IN ({ph})) "
        f"OR EXISTS (SELECT 1 FROM relations r WHERE r.target_id=e.id AND r.status!='deprecated' "
        f"AND r.relation_type IN ({ph})))",
        (f"%{_REQ_SUFFIX}", *_VERIFY_RELS, *_VERIFY_RELS)).fetchone()[0]
    coverage = round(req_covered / req_total * 100, 1) if req_total else None
    items.append({"key": "trace_coverage", "name": "追溯覆盖度",
                  "value": coverage, "unit": "%", "threshold": "≥90%（目标 ≥95%）",
                  "detail": f"{req_covered}/{req_total} 条需求有验证/满足链（source 或 target 侧）",
                  "status": _status(coverage, ok=90, warn=75)})

    # 3. 孤立率：无任何关系的实体占比
    ent_total = conn.execute("SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
    ent_isolated = conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE e.status!='deprecated' AND NOT EXISTS "
        "(SELECT 1 FROM relations r WHERE (r.source_id=e.id OR r.target_id=e.id) "
        "AND r.status!='deprecated')").fetchone()[0]
    orphan = round(ent_isolated / ent_total * 100, 1) if ent_total else None
    items.append({"key": "orphan_rate", "name": "孤立节点率",
                  "value": orphan, "unit": "%", "threshold": "<5%",
                  "detail": f"{ent_isolated}/{ent_total} 个实体无关系",
                  "status": _status(orphan, ok=3, warn=5, lower_better=True)})

    # 4. 映射覆盖率：已批准概念有本体映射的比例
    c_total = conn.execute("SELECT COUNT(*) FROM glossary_concepts WHERE "
                           "concept_status IN ('approved','candidate')").fetchone()[0]
    c_mapped = conn.execute(
        "SELECT COUNT(*) FROM glossary_concepts WHERE concept_status IN ('approved','candidate') "
        "AND (maps_to_class!='' OR maps_to_prop!='' OR maps_to_inst!='')").fetchone()[0]
    mapping = round(c_mapped / c_total * 100, 1) if c_total else None
    items.append({"key": "mapping_coverage", "name": "概念映射覆盖率",
                  "value": mapping, "unit": "%", "threshold": "≥80%",
                  "detail": f"{c_mapped}/{c_total} 个概念已映射本体",
                  "status": _status(mapping, ok=80, warn=50)})

    # 5. 未决候选（审核积压）
    tri_pending = conn.execute("SELECT COUNT(*) FROM triples WHERE status IN "
                               "('pending','submitted')").fetchone()[0]
    v2g_pending = 0
    try:
        v2g_pending = conn.execute("SELECT COUNT(*) FROM v2g_candidates WHERE "
                                   "status='pending'").fetchone()[0]
    except Exception:
        pass
    items.append({"key": "pending_candidates", "name": "未决候选（审核积压）",
                  "value": tri_pending + v2g_pending, "unit": "条",
                  "threshold": "持续增长需扩审核产能",
                  "detail": f"triples {tri_pending} + v2g {v2g_pending}",
                  "status": "ok" if tri_pending + v2g_pending < 50 else "warn"})

    # 6. 低置信占比（approved 三元组中 confidence<0.7）
    tri_approved = conn.execute("SELECT COUNT(*) FROM triples WHERE status='approved'").fetchone()[0]
    tri_low = conn.execute("SELECT COUNT(*) FROM triples WHERE status='approved' "
                           "AND confidence < 0.7").fetchone()[0]
    low_ratio = round(tri_low / tri_approved * 100, 1) if tri_approved else 0.0
    items.append({"key": "low_confidence", "name": "低置信三元组占比",
                  "value": low_ratio, "unit": "%", "threshold": "<10%",
                  "detail": f"{tri_low}/{tri_approved} 条 approved <0.7",
                  "status": _status(low_ratio, ok=5, warn=10, lower_better=True)})

    # 7. 弃用术语残留（deprecated 概念的词仍被实体/关系名使用）
    dep_terms = [r["term"] for r in conn.execute(
        "SELECT t.term FROM glossary_terms t JOIN glossary_concepts c "
        "ON c.concept_id=t.concept_id WHERE c.concept_status='deprecated'")]
    residue = 0
    if dep_terms:
        ph2 = ",".join("?" * len(dep_terms))
        residue = conn.execute(
            f"SELECT (SELECT COUNT(*) FROM entities WHERE status!='deprecated' AND name IN ({ph2})) "
            f"+ (SELECT COUNT(*) FROM relations WHERE status!='deprecated' AND relation_type IN ({ph2}))",
            (*dep_terms, *dep_terms)).fetchone()[0]
    items.append({"key": "deprecated_residue", "name": "弃用术语残留",
                  "value": residue, "unit": "处", "threshold": "=0",
                  "status": _status(residue, lower_better=True, warn=0)})

    # 扩展 8：同形异义（同一词指多概念）
    homo = conn.execute("SELECT COUNT(*) n FROM (SELECT term FROM glossary_terms "
                        "GROUP BY term, lang HAVING COUNT(DISTINCT concept_id)>1)").fetchone()[0]
    items.append({"key": "homonym_terms", "name": "同形异义词",
                  "value": homo, "unit": "个", "threshold": "需人工消歧",
                  "status": "ok" if homo == 0 else "warn"})

    # 扩展 9：镜像水位（P1-4）
    ms = mirror_state(conn)
    items.append({"key": "mirror_sync", "name": "图库镜像水位",
                  "value": ms["lag"], "unit": "条",
                  "threshold": "lag>0 需增量物化",
                  "detail": ms["detail"],
                  "status": "ok" if ms["lag"] == 0 else "warn"})

    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "gate_mode": _gate_mode(conn), "items": items,
            "summary": {
                "ok": sum(1 for i in items if i["status"] == "ok"),
                "warn": sum(1 for i in items if i["status"] == "warn"),
                "alert": sum(1 for i in items if i["status"] == "alert"),
            }}


def _gate_mode(conn) -> str:
    try:
        from ingest_gate import gate_mode
        return gate_mode(conn)
    except Exception:
        return "unknown"


# ── P1-4 双库一致性 ──────────────────────────────────────────
def mark_synced(conn, branch: str = "dev", commit: str = "", triple_count: int = 0) -> None:
    """物化完成后记录水位。"""
    conn.execute("INSERT INTO graph_sync_state (branch, last_commit, triple_count, synced_at) "
                 "VALUES (?,?,?,datetime('now')) "
                 "ON CONFLICT(branch) DO UPDATE SET last_commit=excluded.last_commit, "
                 "triple_count=excluded.triple_count, synced_at=excluded.synced_at",
                 (branch, commit, triple_count))
    conn.commit()


def mirror_state(conn, branch: str = "dev") -> dict:
    """镜像滞后量：approved 未入图（graph_stored=0）+ 水位时间。"""
    try:
        lag = conn.execute("SELECT COUNT(*) FROM triples WHERE status='approved' "
                           "AND graph_stored=0").fetchone()[0]
        row = conn.execute("SELECT last_commit, triple_count, synced_at FROM graph_sync_state "
                           "WHERE branch=?", (branch,)).fetchone()
        detail = (f"branch={branch} 水位@{row['synced_at'] or '未同步'} "
                  f"(commit={row['last_commit'] or '-'}, {row['triple_count']} 条)" if row
                  else f"branch={branch} 无同步记录")
    except Exception as e:
        lag, detail = None, f"读取失败: {e}"
    return {"branch": branch, "lag": lag, "detail": detail}


# ── P1-3 本体发布快照 ────────────────────────────────────────
def release_snapshot(conn, version: str, note: str = "", project_id: str = "",
                     branch: str = "dev", operator: str = "system") -> dict:
    """发布本体：冻结 OWL Turtle + SHACL 形状到 ontology_snapshots。

    规则：release 前须 SHACL 无 Violation（enforce 语义）；version 需 SemVer 格式。
    """
    import re as _re
    if not _re.match(r"^\d+\.\d+\.\d+$", version or ""):
        return {"ok": False, "error": "version 须为 SemVer（major.minor.patch）"}
    if conn.execute("SELECT 1 FROM ontology_snapshots WHERE project_id=? AND branch=? AND version=?",
                    (project_id, branch, version)).fetchone():
        return {"ok": False, "error": f"版本已存在: {version}"}
    # 发布门禁：先跑 SHACL（enforce 语义——有 Violation 不许发）
    try:
        from ingest_gate import check_triples
        res = check_triples(conn, record=True, batch_id=f"release:{version}")
        if not res["skipped"] and res["violations"]:
            n_v = sum(1 for v in res["violations"] if v["severity"] == "Violation")
            if n_v:
                return {"ok": False, "error": f"发布拦截：SHACL Violation {n_v} 条（治理看板可见分布）",
                        "violations": res["violations"][:20]}
    except ImportError:
        pass  # pyshacl 未装时不拦截发布，但指标页会显示 skipped
    from ontology_owl import to_turtle
    from ontology_semantics import OntologyValidator
    # to_turtle 返回 (text, warnings)（方案 A3）；此前未解包导致发布 INSERT 报
    # "type 'tuple' is not supported"——旧路径走不到这里（总被 SHACL 拦截），P1-2 后首次暴露
    owl_text, owl_warnings = to_turtle(conn)
    shacl_text = OntologyValidator(conn).shacl_export()
    conn.execute("INSERT INTO ontology_snapshots (project_id, branch, version, note, "
                 "owl_text, shacl_text, created_by) VALUES (?,?,?,?,?,?,?)",
                 (project_id, branch, version, note[:300], owl_text, shacl_text, operator))
    # 同步打 ontology_types 版本号（status 保持 released）
    conn.execute("UPDATE ontology_types SET version=version+1 WHERE project_id=? AND branch=?",
                 (project_id, branch))
    conn.commit()
    return {"ok": True, "version": version,
            "owl_bytes": len(owl_text), "shacl_bytes": len(shacl_text)}


def list_snapshots(conn, project_id: str = "", branch: str = "", limit: int = 20) -> list:
    sql = ("SELECT id, project_id, branch, version, note, created_by, created_at, "
           "LENGTH(owl_text) owl_bytes, LENGTH(shacl_text) shacl_bytes "
           "FROM ontology_snapshots")
    conds, params = [], []
    if project_id:
        conds.append("project_id=?"); params.append(project_id)
    if branch:
        conds.append("branch=?"); params.append(branch)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_snapshot(conn, sid: int) -> dict | None:
    row = conn.execute("SELECT * FROM ontology_snapshots WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else None
