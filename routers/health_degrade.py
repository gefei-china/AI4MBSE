# -*- coding: utf-8 -*-
"""降级台账只读 API（P0：让错误可见）—— 2026-11-25。

## 为什么需要这组端点
`core/degrade.py` 把 1361 处静默兜底变成**可分级、可计数**的台账，但台账只在
进程内存里 —— 没有读取入口，就等于"记录了但没人看得到"（本仓已归档的同族缺口：
事件底座的产出此前只在 API 里、不做界面就等于没做）。

三档消费方：
  · **执行过程中**（主路径）→ `/api/conversations/{id}/chat/stream` 的 `degrade` 事件，
    由 `routers/conversations.py` 直接转发，不经过本模块（要零延迟、要保序）；
  · **运行监控看板**（人主动来看）→ `GET /api/health/degrade`；
  · **门禁/巡检**（机器定期看）→ 同一端点，`?level=critical` 过滤出"必须处理"的。

## 纪律
· **只读**：本模块不提供写/清空端点。清空会让"降级过"变成"从没降级过"，
  与"0 违规 vs 门禁没跑不可区分"是同一类欺骗。需要清空的只有测试/门禁，
  它们直接调 `core.degrade.reset()`，不走 HTTP。
· **不返回空 dict 表示未知**：`total=0` 明确是"零降级"，"读不到"用 HTTP 500 表达。
· **不鉴权豁免**：与 `/api/monitor/*` 同档，走 `current_user`。台账里含内部标识
  （`where`）与错误消息，不该对匿名开放。
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.deps import current_user
from core import degrade as DG

router = APIRouter(tags=["降级台账"])


@router.get("/api/health/degrade")
def degrade_snapshot(
    level: str = Query("", description="按级别过滤：critical / degraded / cosmetic（空=全部）"),
    limit: int = Query(50, ge=1, le=200, description="最多返回多少条 where 明细"),
    user=Depends(current_user),
):
    """降级台账聚合快照（按 where 合并，含级别与影响描述）。

    返回结构见 `core.degrade.snapshot()`。`worst` 已按「级别优先、次数其次」排序 ——
    看板第一眼就该看到最严重的，而不是最多的。
    """
    try:
        snap = DG.snapshot()
    except Exception as e:                              # noqa: BLE001
        # 读不到 ≠ 零降级：必须让调用方看到错误，而不是一个漂亮的 0
        return JSONResponse({"error": "降级台账读取失败：%s: %s"
                                      % (type(e).__name__, str(e)[:200])},
                            status_code=500)
    lv = (level or "").strip().lower()
    if lv:
        if lv not in DG.LEVEL_RANK:
            return JSONResponse({"error": "非法 level=%r，允许值：%s"
                                          % (level, ", ".join(DG.LEVELS))},
                                status_code=400)
        # 过滤后重算 total/by_level，避免"筛了 critical 却显示全量 total"的口径错位
        wh = {k: v for k, v in (snap.get("where") or {}).items() if v.get("level") == lv}
        total = sum(int(v.get("count") or 0) for v in wh.values())
        by_level = {x: (total if x == lv else 0) for x in DG.LEVELS}
        worst = [{"where": k, **v} for k, v in
                 sorted(wh.items(), key=lambda kv: -int(kv[1].get("count") or 0))[:limit]]
        return {"total": total, "by_level": by_level, "where_n": len(wh),
                "where": wh, "worst": worst, "filtered_by": lv}
    snap["worst"] = (snap.get("worst") or [])[:limit]
    return snap


@router.get("/api/health/degrade/top")
def degrade_top(
    level: str = Query("", description="按级别过滤（空=全部）"),
    n: int = Query(10, ge=1, le=50),
    user=Depends(current_user),
):
    """只要排行（给看板卡片用，体积小、无需前端再排序）。"""
    try:
        snap = DG.snapshot()
    except Exception as e:                              # noqa: BLE001
        return JSONResponse({"error": "降级台账读取失败：%s: %s"
                                      % (type(e).__name__, str(e)[:200])},
                            status_code=500)
    worst = snap.get("worst") or []
    lv = (level or "").strip().lower()
    if lv in DG.LEVEL_RANK:
        worst = [w for w in worst if w.get("level") == lv]
    return {"total": snap.get("total", 0), "by_level": snap.get("by_level", {}),
            "items": worst[:n]}
