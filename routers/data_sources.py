# -*- coding: utf-8 -*-
"""数据源注册管理（P0-4 2026-09-20 重建；原实现 R1=B 随数据集成移除删除，pyc 考古恢复口径）。

对齐 SRS：KG-FQ 数据导入（数据库/接口多源接入）+ 数据入图处理。

- GET/POST /api/knowledge/data-sources            → 列表 / 注册（file/db/api 三类）
- PUT/DELETE /api/knowledge/data-sources/{dsid}   → 更新 / 删除（审计留痕，候选不受影响）
- POST .../{dsid}/test                            → 连通性探测（结果写 last_status）
- POST .../{dsid}/preview                         → 样例预览（确认再提交，SRS 导入用例场景 3）
- POST .../{dsid}/ingest                          → 抽取执行：向量管线（腿①）+ v2g 候选进未评审区（腿②）

审计事件：data_source_create / update / delete / test / preview / ingest。
凭据不落库：config 只存 env 变量名，运行时解析（services.datasource_service）。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.deps import db_session, current_user
from core.audit import audit
from models import DataSourceIn
from services.datasource_service import DataSourceService
from routers.knowledge_parts.shared import _actor

router = APIRouter(tags=["知识库"])


def _svc(conn) -> DataSourceService:
    return DataSourceService(conn)


def _require_ds(conn, dsid: int) -> dict:
    ds = DataSourceService(conn)._repo().get(dsid)  # repo 单例经 service 缓存
    if not ds:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return ds


@router.get("/api/knowledge/data-sources")
def list_data_sources(conn=Depends(db_session)):
    """数据源列表（config 解析为 dict）。"""
    return {"ok": True, "items": _svc(conn)._repo().list()}


@router.post("/api/knowledge/data-sources")
def create_data_source(body: DataSourceIn, conn=Depends(db_session),
                       user=Depends(current_user)):
    """注册数据源（type=file/db/api，config=连接配置）。"""
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name 不能为空")
    if body.type not in ("file", "db", "api"):
        raise HTTPException(status_code=400, detail="type 必须是 file/db/api")
    actor = _actor(user)
    dsid = _svc(conn)._repo().insert(body.name.strip(), body.type,
                                     body.config or {}, 1 if body.enabled else 0, actor)
    audit(actor, "data_source_create", f"注册数据源#{dsid}: {body.name}（{body.type}）", conn=conn)
    return {"ok": True, "id": dsid}


@router.put("/api/knowledge/data-sources/{dsid}")
def update_data_source(dsid: int, body: DataSourceIn, conn=Depends(db_session),
                       user=Depends(current_user)):
    """更新数据源（名称/类型/配置/启停）。"""
    _require_ds(conn, dsid)
    if body.type not in ("file", "db", "api"):
        raise HTTPException(status_code=400, detail="type 必须是 file/db/api")
    actor = _actor(user)
    _svc(conn)._repo().update(dsid, body.name.strip(), body.type,
                              body.config or {}, 1 if body.enabled else 0)
    audit(actor, "data_source_update",
          f"更新数据源#{dsid}: {body.name}（{body.type}）enabled={body.enabled}", conn=conn)
    return {"ok": True}


@router.delete("/api/knowledge/data-sources/{dsid}")
def delete_data_source(dsid: int, conn=Depends(db_session), user=Depends(current_user)):
    """删除数据源（已入库候选不受影响，审计留痕）。"""
    _require_ds(conn, dsid)
    actor = _actor(user)
    _svc(conn)._repo().delete(dsid)
    audit(actor, "data_source_delete", f"删除数据源#{dsid}", conn=conn)
    return {"ok": True}


@router.post("/api/knowledge/data-sources/{dsid}/test")
def test_data_source(dsid: int, conn=Depends(db_session), user=Depends(current_user)):
    """连通性探测：file 检查 path；db 试连（只读）；api 试拉。结果写 last_status。"""
    ds = _require_ds(conn, dsid)
    actor = _actor(user)
    ok, msg = _svc(conn).test(ds)
    audit(actor, "data_source_test", f"测试数据源#{dsid}: {ok} → {msg}", conn=conn)
    return {"ok": ok, "message": msg}


@router.post("/api/knowledge/data-sources/{dsid}/preview")
def preview_data_source(dsid: int, limit: Optional[int] = 20,
                        conn=Depends(db_session), user=Depends(current_user)):
    """样例预览：db/api 返回前 N 条记录与列清单；file 返回内容头部。确认后再 ingest。"""
    ds = _require_ds(conn, dsid)
    actor = _actor(user)
    r = _svc(conn).preview(ds, limit=min(limit or 20, 100))
    audit(actor, "data_source_preview",
          f"预览数据源#{dsid}: {'ok' if r.get('ok') else r.get('error', '')[:80]}", conn=conn)
    return r


@router.post("/api/knowledge/data-sources/{dsid}/ingest")
def ingest_data_source(dsid: int, title: Optional[str] = None,
                       conn=Depends(db_session), user=Depends(current_user)):
    """执行抽取：读取源内容 → 记录型文档进向量管线（腿①）→ 同文档 v2g 候选进未评审区（腿②）。

    - enabled=0 拒绝抽取（400）
    - 人工确认候选后才会写入已评审区（v2g_confirm）
    """
    ds = _require_ds(conn, dsid)
    actor = _actor(user)
    try:
        r = _svc(conn).ingest(ds, actor, title or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit(actor, "data_source_ingest",
          f"抽取数据源#{dsid}: 文档 {r['doc_id']} 块 {r['chunk_count']} "
          f"候选 {r['node_count'] + r['edge_count']}（batch {r['batch_id']}）", conn=conn)
    return {"ok": True, **r}
