"""仪表盘域：/api/dashboard"""
from fastapi import APIRouter, Depends

from core.deps import db_session, current_user
from repositories.dashboard_repo import DashboardRepo

router = APIRouter(tags=["仪表盘"])


@router.get("/api/dashboard")
def dashboard(conn=Depends(db_session), u=Depends(current_user)):
    """工作台聚合：KPI / 待办 / 资产 / 最近对话 / 当前用户最近操作轨迹。"""
    return DashboardRepo(conn).dashboard_stats(actor_name=(u or {}).get("display_name"))
