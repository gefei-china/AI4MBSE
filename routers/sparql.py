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


# ── /update 语句白名单（2026-09-22 安全加固）─────────────────────────────
# 背景：/update 原实现把 body 内 SPARQL 原样交给 Fuseki 执行，无语句类型管控；
# 且鉴权依赖可伪造的 X-User-Id（P0 鉴权落地前）→ 一旦 Fuseki 起来等于开放任意图写
# （DROP 可清空整个数据集）。ExtInfR-DD-3 只要求"创建/查询/修改"数据，不要求图管理。
_ALLOWED_UPDATE_OPS = {"INSERT", "DELETE", "LOAD", "CLEAR", "COPY", "MOVE", "ADD"}
# 图/服务管理类一律拒绝：DROP（删图删数据集）、CREATE（建图）不在白名单；
# Prologue 中的 FROM/NAMED 不受影响（只对 UPDATE 的实际操作首词判型）。
import re as _re
_COMMENT_RE = _re.compile(r"#[^\n]*")


def _classify_update(sparql: str) -> str:
    """提取 SPARQL UPDATE 语句的实际操作首词（剥注释与 prologue：PREFIX/BASE 行）。"""
    s = _COMMENT_RE.sub("", sparql or "")
    tokens = s.split()
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i].upper()
        if t == "PREFIX" and i + 1 < n:
            # PREFIX name: <iri...> —— IRI 理论上可含空格（unicode 转义后拆 token），
            # 跳到第一个以 '>' 结尾的 token（首 token 是名称，从 i+2 起找 IRI 收尾）
            j = i + 2
            while j < n and not tokens[j].rstrip().endswith(">"):
                j += 1
            i = j + 1
            continue
        if t == "BASE" and i + 1 < n:
            # BASE <iri> —— 同理跳到 IRI 收尾
            j = i + 1
            while j < n and not tokens[j].rstrip().endswith(">"):
                j += 1
            i = j + 1
            continue
        return t
    return ""


@router.post("/update")
def sparql_update(body: dict, conn=Depends(db_session)):
    from services.sparql_sync import FusekiClient, FusekiUnavailable, FusekiQueryError
    s = body.get("update") or body.get("sparql") or ""
    if not s:
        return JSONResponse({"error": "update 不能为空"}, 400)
    op = _classify_update(s)
    if op not in _ALLOWED_UPDATE_OPS:
        return JSONResponse(
            {"error": f"不允许的 SPARQL 更新操作: {op or '(空)'}",
             "allowed": sorted(_ALLOWED_UPDATE_OPS),
             "hint": "仅允许数据级更新（INSERT/DELETE/LOAD/CLEAR/COPY/MOVE/ADD）；DROP/CREATE 等图管理操作被禁用"},
            403,
        )
    cli = FusekiClient()
    try:
        status = cli.update(s)
        return {"ok": True, "status": status}
    except FusekiUnavailable as e:
        return JSONResponse({"error": "Fuseki 不可达", "detail": str(e)}, 503)
    except FusekiQueryError as e:
        return JSONResponse({"error": "更新失败", "detail": str(e)}, 400)
