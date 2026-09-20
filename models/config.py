"""系统配置域模型（自 routers/config.py 原样搬移，P1-2 Models 外置）。"""
from typing import Any, Dict

from pydantic import BaseModel


class StaticConfigIn(BaseModel):
    """静态配置更新：{"zhiyuan.base_url": "http://...", "mcp.timeout": "20", ...}"""

    updates: Dict[str, Any]


class RuntimeConfigIn(BaseModel):
    """动态配置更新：{"query_route": "graph_first", "default_project_id": "..."}"""

    updates: Dict[str, Any]
