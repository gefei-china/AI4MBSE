# -*- coding: utf-8 -*-
"""SysML v1/v2 视图域：/api/sysml/views/*

- GET  /api/sysml/views/types  → 视图类型清单（v1 九图 + v2 视图族 + 知识库映射）
- GET  /api/sysml/views        → 按类型投影图谱为 ViewModel（?type=BDD&branch=dev）
- POST /api/sysml/views/check  → 视图检查（类型正确性 + 覆盖完整性）+ 布局质量存档
                                 （body.layout_metrics 可选：前端布局引擎算出的
                                  crossings/edge_length_sum/score，落 view_layout_checks 表）

消费侧只读：不传 branch 时默认消费已发布(release)分支。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.deps import db_session
from database.connection import get_db
import view_generator as vg

router = APIRouter(tags=["SysML视图"])


@router.get("/api/sysml/views/types")
def view_types():
    return {"ok": True, "types": vg.list_view_types()}


@router.get("/api/sysml/views")
def get_view(type: str = Query("BDD", description="视图类型：BDD/IBD/REQ/UC/ACT/SEQ/STM/PAR/PKG/TRACE"),
             branch: Optional[str] = Query(None, description="分支；不传默认消费 release 已发布分支"),
             project_id: Optional[str] = None,
             conn=Depends(db_session)):
    vm = vg.generate_view(type, branch, project_id)
    return {"ok": True, **vm}


class ViewCheckBody(BaseModel):
    type: str = Field("BDD", description="视图类型")
    branch: Optional[str] = Field(None, description="分支；空=release 默认")
    project_id: Optional[str] = None
    layout_metrics: Optional[dict] = Field(
        None,
        description="布局质量指标 {crossings, edge_length_sum, score, layout_engine?}；"
                    "由前端布局引擎计算后回传，存在即落库存档（SRS 布局优化验收证据）")


@router.post("/api/sysml/views/check")
def check_view(body: ViewCheckBody):
    """视图检查：类型正确性 + 覆盖完整性（后端判据）+ 布局质量回传存档（前端指标）。"""
    result = vg.check_view(body.type, body.branch, body.project_id)
    result["layout_saved"] = False

    lm = body.layout_metrics or {}
    if lm.get("crossings") is not None or lm.get("score") is not None:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO view_layout_checks "
                "(view_type, branch, node_count, edge_count, missing_kinds, "
                " crossings, edge_length_sum, score, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (body.type, body.branch or "", result["stats"]["node_count"],
                 result["stats"]["edge_count"], ",".join(result["check"]["missing"]),
                 int(lm.get("crossings") or 0), float(lm.get("edge_length_sum") or 0),
                 float(lm.get("score") or 0), "{}"))
            conn.commit()
            result["layout_saved"] = True
        except Exception as e:  # 兜底必须留痕：存档失败不阻断校验结果返回
            result["layout_saved"] = False
            result["layout_save_error"] = str(e)
    return result
