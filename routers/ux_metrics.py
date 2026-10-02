# -*- coding: utf-8 -*-
"""UX 埋点 API（2026-10-03）：前端行为事件上报 + 指标聚合（评估规范 §11）。

## 端点
    POST /api/ux-metrics           批量上报（单批 ≤50，事件名白名单）
    GET  /api/ux-metrics/summary   聚合比率（days 参数，默认 7）

## 权限纪律（与 intent_samples/memory_admin 同口径）
写端点不加权限门 —— 遥测若 403 会静默丢数据（前端根本不弹错），加了等于没加；
且埋点不含敏感内容（事件名 + 会话 id + 短 detail）。聚合端点同样开放。
"""
from fastapi import APIRouter, Depends

from core.deps import db_session
from repositories.ux_metrics_repo import UxMetricsRepo

router = APIRouter(tags=["UX 埋点"])


@router.post("/api/ux-metrics")
def track_events(payload: dict, conn=Depends(db_session)):
    """批量上报。payload = {"events": [{event, conversation_id, detail}]}。

    恒返回 ok（遥测失败对前端不可见、不重试 —— 静默丢批是既定纪律）。
    """
    n = UxMetricsRepo(conn).insert_batch((payload or {}).get("events") or [])
    return {"ok": True, "inserted": n}


@router.get("/api/ux-metrics/summary")
def metrics_summary(days: int = 7, conn=Depends(db_session)):
    """§11 六指标聚合（任务完成率 / 中断率 / 澄清命中率 / 滚动抢夺守护 / 确认闸门转化）。"""
    return UxMetricsRepo(conn).summary(days)
