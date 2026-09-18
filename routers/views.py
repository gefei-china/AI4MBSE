# -*- coding: utf-8 -*-
"""SysML v1/v2 视图域：/api/sysml/views/*

- GET /api/sysml/views/types   → 视图类型清单（v1 九图 + v2 视图族 + 知识库映射）
- GET /api/sysml/views         → 按类型投影图谱为 ViewModel（?type=BDD&branch=dev）
- POST /api/sysml/views/check  → 视图元素完整性校验（最小输入集）

消费侧只读：不传 branch 时默认消费已发布(release)分支。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from core.deps import db_session
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
