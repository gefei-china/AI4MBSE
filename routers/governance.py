# -*- coding: utf-8 -*-
"""治理 API（P1-1 门禁模式 / P1-3 发布 / P1-4 水位 / P1-7 指标）。"""
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session

router = APIRouter(tags=["知识治理"])


@router.get("/api/governance/metrics")
def metrics(run_shacl: bool = False, conn=Depends(db_session)):
    """知识治理健康度指标（7 项 + 扩展）。run_shacl=true 现场跑一次 SHACL（约 1s）。"""
    from governance import compute_metrics
    return compute_metrics(conn, run_shacl=run_shacl)


@router.get("/api/governance/gate-mode")
def get_gate_mode(conn=Depends(db_session)):
    from ingest_gate import gate_mode
    return {"mode": gate_mode(conn),
            "hint": "off=不校验 | warn=记录不阻断（默认，先观察2周） | enforce=违规阻断入库"}


@router.put("/api/governance/gate-mode")
def put_gate_mode(body: dict, conn=Depends(db_session)):
    from ingest_gate import set_gate_mode
    mode = (body.get("mode") or "").strip()
    if not set_gate_mode(conn, mode):
        return JSONResponse({"error": "mode 须为 off | warn | enforce"}, 400)
    return {"ok": True, "mode": mode}


@router.post("/api/governance/shacl-check")
def shacl_check(conn=Depends(db_session)):
    """现场全量 SHACL 校验，返回违规清单（不入库 findings）。"""
    from ingest_gate import check_triples
    res = check_triples(conn, record=False)
    # 按违规类型聚合（同一 message 前缀的分布），方便定位系统性问题
    dist = {}
    for v in res.get("violations", []):
        k = f"{v['path']}::{v['message'][:40]}"
        dist[k] = dist.get(k, 0) + 1
    top = sorted(dist.items(), key=lambda x: -x[1])[:10]
    return {"mode": res["mode"], "skipped": res["skipped"], "reason": res.get("reason", ""),
            "conforms": res["conforms"], "total_checked": res["total_checked"],
            "violation_n": len(res["violations"]),
            "violations": res["violations"][:100], "distribution": top}


@router.get("/api/governance/shacl-findings")
def shacl_findings(limit: int = 50, conn=Depends(db_session)):
    rows = conn.execute("SELECT * FROM shacl_findings ORDER BY id DESC LIMIT ?",
                        (min(limit, 500),)).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.post("/api/governance/ontology/release")
def ontology_release(body: dict, conn=Depends(db_session)):
    """本体发布：SemVer 版本冻结 OWL+SHACL。有 SHACL Violation 时拦截。"""
    from governance import release_snapshot
    res = release_snapshot(
        conn, version=(body.get("version") or "").strip(),
        note=body.get("note") or "",
        project_id=body.get("project_id") or "", branch=body.get("branch") or "dev",
        operator=body.get("operator") or "王工")
    if not res.get("ok"):
        return JSONResponse(res, 422)
    return res


@router.get("/api/governance/ontology/snapshots")
def ontology_snapshots(project_id: Optional[str] = None, branch: Optional[str] = None,
                       limit: int = 20, conn=Depends(db_session)):
    from governance import list_snapshots
    return {"items": list_snapshots(conn, project_id or "", branch or "", limit)}


@router.get("/api/governance/ontology/snapshots/{sid}")
def ontology_snapshot_detail(sid: int, conn=Depends(db_session)):
    from governance import get_snapshot
    row = get_snapshot(conn, sid)
    if not row:
        return JSONResponse({"error": "快照不存在"}, 404)
    return row


@router.get("/api/governance/mirror-state")
def mirror_state(branch: str = "dev", conn=Depends(db_session)):
    from governance import mirror_state as _ms
    return _ms(conn, branch)
