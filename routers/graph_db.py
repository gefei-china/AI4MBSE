"""图数据库（pyoxigraph/Fuseki/Neo4j）消费侧 API（星网 ArcR-5/6，P2 消费侧打通）。

- GET  /api/graph-db/stats   图库规模统计（三元组/命名图/后端）
- POST /api/graph-db/query   SPARQL 查询（只读，{query, limit}）
- POST /api/graph-db/sync    手动触发物化消费链（triples graph_stored=0 → 图库）
- GET  /api/graph-db/export  导出 N-Quads（Jena/Fuseki/neosemantics 互导）→ 返回文件路径
- GET  /api/graph-db/status  开关与后端配置状态（前端看板用）

安全：默认只读（query/sync/export 均需已登录用户）；SPARQL 查询仅允许 SELECT（禁 UPDATE/INSERT/DELETE）。
"""
from fastapi import APIRouter, Depends, HTTPException

from core import config
from core.deps import current_user
from core.audit import audit

router = APIRouter(tags=["图数据库"])


def _writer():
    return __import__("graph_db", fromlist=["get_writer"]).get_writer(
        enabled=True,
        backend=config.get("graph_db", "backend", "pyoxigraph"),
        store_path=config.get("graph_db", "path", ""),
        use_memory=config.get("graph_db", "use_memory", False),
        endpoint=config.get("graph_db", "endpoint", ""),
        username=config.get("graph_db", "username", ""),
        password=config.get("graph_db", "password", ""),
        neo4j_uri=config.get("graph_db", "neo4j_uri", ""),
        neo4j_user=config.get("graph_db", "neo4j_user", ""),
        neo4j_password=config.get("graph_db", "neo4j_password", ""),
    )


@router.get("/api/graph-db/status")
def graph_db_status(user: dict = Depends(current_user)):
    """开关与后端状态（enabled 由后端配置决定；writer 按 backend 实例化）。"""
    enabled = config.as_bool("graph_db", "enabled")
    writer = _writer()
    try:
        st = writer.stats() if enabled else {}
        return {"ok": True, "enabled": enabled,
                "backend": config.get("graph_db", "backend", "pyoxigraph"),
                "endpoint": config.get("graph_db", "endpoint", ""),
                "stats": st}
    finally:
        writer.close()


@router.get("/api/graph-db/stats")
def graph_db_stats(user: dict = Depends(current_user)):
    if not config.as_bool("graph_db", "enabled"):
        raise HTTPException(400, "图数据库未启用（graph_db.enabled=false）")
    writer = _writer()
    try:
        return {"ok": True, "stats": writer.stats()}
    finally:
        writer.close()


@router.post("/api/graph-db/query")
def graph_db_query(body: dict, user: dict = Depends(current_user)):
    """SPARQL 只读查询（仅允许 SELECT 开头，防注入写操作）。"""
    if not config.as_bool("graph_db", "enabled"):
        raise HTTPException(400, "图数据库未启用（graph_db.enabled=false）")
    q = str((body or {}).get("query") or "").strip()
    if not q:
        raise HTTPException(400, "query 不能为空")
    if not q.upper().startswith("SELECT"):
        raise HTTPException(403, "仅允许 SELECT 查询（只读）")
    limit = int((body or {}).get("limit") or 50)
    limit = max(1, min(limit, 1000))
    writer = _writer()
    try:
        rows = writer.query_sparql(q, limit=limit)
        audit(user.get("username", ""), "graph_db_query",
              f"SPARQL {q[:120]} → {len(rows)} 行")
        return {"ok": True, "rows": rows, "count": len(rows)}
    finally:
        writer.close()


@router.post("/api/graph-db/sync")
def graph_db_sync(user: dict = Depends(current_user)):
    """手动触发物化消费链（triples 已审核未入图 → 图库）。"""
    if not config.as_bool("graph_db", "enabled"):
        raise HTTPException(400, "图数据库未启用（graph_db.enabled=false）")
    from database import get_db
    from graph_db import sync_pending_triples
    conn = get_db()
    writer = _writer()
    try:
        res = sync_pending_triples(conn, writer)
        audit(user.get("username", ""), "graph_db_sync", f"synced={res['synced']}")
        return {"ok": True, **res, "stats": writer.stats()}
    finally:
        writer.close()
        conn.close()


@router.post("/api/graph-db/nlquery")
def graph_db_nlquery(body: dict, user: dict = Depends(current_user)):
    """自然语言图问答（NL → SPARQL 自动翻译，只读）。"""
    if not config.as_bool("graph_db", "enabled"):
        raise HTTPException(400, "图数据库未启用（graph_db.enabled=false）")
    from graph_db_tools import _nlquery
    res = _nlquery(body or {})
    if not res.get("ok"):
        raise HTTPException(422, res.get("error") or "图问答失败")
    audit(user.get("username", ""), "graph_db_nlquery",
          f"{str((body or {}).get('query') or '')[:80]} → {res.get('count', 0)} 行")
    return res


@router.get("/api/graph-db/export")
def graph_db_export(user: dict = Depends(current_user)):
    """导出 N-Quads（Jena Fuseki / neosemantics 互导）。"""
    if not config.as_bool("graph_db", "enabled"):
        raise HTTPException(400, "图数据库未启用（graph_db.enabled=false）")
    import os
    from core import config as cfg
    export_dir = cfg.get("graph_db", "export_path", "data/rdf_export")
    os.makedirs(export_dir, exist_ok=True)
    path = os.path.join(export_dir, "graph_export.nq")
    writer = _writer()
    try:
        n = writer.export_nq(path)
        audit(user.get("username", ""), "graph_db_export", f"{n} 条四元组 → {path}")
        return {"ok": True, "path": path, "quads": n}
    finally:
        writer.close()
