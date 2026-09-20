# -*- coding: utf-8 -*-
"""P2-①/② + P3-②（2026-09-11）SPARQL 1.1 Endpoint 代理 + 同步 API + 时态扩展。"""
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.deps import db_session

router = APIRouter(prefix="/api/sparql", tags=["SPARQL 1.1"])


class SyncRequest(BaseModel):
    base_url: Optional[str] = None
    dataset: Optional[str] = None
    project_id: Optional[str] = None
    with_time: bool = True  # P3-①：是否附加 W3C Time Ontology 完整导出


@router.get("/dependency")
def dependency():
    from services.sparql_sync import dependency_status
    return dependency_status()


@router.post("/sync")
def sync(req: SyncRequest, conn=Depends(db_session)):
    if req.with_time:
        from services.temporal_sparql import sync_to_fuseki_with_time
        return sync_to_fuseki_with_time(
            conn,
            base_url=req.base_url or "http://localhost:3030",
            dataset=req.dataset or "mbse",
            project_id=req.project_id,
        )
    from services.sparql_sync import sync_to_fuseki
    return sync_to_fuseki(
        conn,
        base_url=req.base_url or "http://localhost:3030",
        dataset=req.dataset or "mbse",
        project_id=req.project_id,
    )


@router.get("/query")
def sparql_query_get(q: str, as_of: Optional[str] = None, conn=Depends(db_session)):
    return _do_query(q, as_of)


@router.post("/query")
def sparql_query_post(body: dict, conn=Depends(db_session)):
    q = body.get("query") or body.get("sparql") or ""
    as_of = body.get("as_of") or body.get("asOf")
    if not q:
        return JSONResponse({"error": "query 不能为空"}, 400)
    return _do_query(q, as_of)


def _do_query(sparql: str, as_of: Optional[str] = None):
    from services.sparql_sync import FusekiClient, FusekiUnavailable, FusekiQueryError
    if as_of:
        from services.temporal_sparql import asOfProxy
        sparql = asOfProxy(sparql, as_of)
    cli = FusekiClient()
    try:
        return cli.query(sparql)
    except FusekiUnavailable as e:
        return JSONResponse(
            {"error": "Fuseki 不可达", "detail": str(e),
             "hint": "请先运行 `docker-compose -f docker-compose.fuseki.yml up -d` 或调用 POST /api/sparql/sync"},
            503,
        )
    except FusekiQueryError as e:
        return JSONResponse({"error": "查询失败", "detail": str(e)}, 400)


@router.post("/update")
def sparql_update(body: dict, conn=Depends(db_session)):
    from services.sparql_sync import FusekiClient, FusekiUnavailable, FusekiQueryError
    s = body.get("update") or body.get("sparql") or ""
    if not s:
        return JSONResponse({"error": "update 不能为空"}, 400)
    cli = FusekiClient()
    try:
        status = cli.update(s)
        return {"ok": True, "status": status}
    except FusekiUnavailable as e:
        return JSONResponse({"error": "Fuseki 不可达", "detail": str(e)}, 503)
    except FusekiQueryError as e:
        return JSONResponse({"error": "更新失败", "detail": str(e)}, 400)
