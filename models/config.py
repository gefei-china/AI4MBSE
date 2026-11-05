"""系统配置域模型（自 routers/config.py 原样搬移，P1-2 Models 外置）。"""
from typing import Any, Dict

from pydantic import BaseModel


class StaticConfigIn(BaseModel):
    """静态配置更新：{"zhiyuan.base_url": "http://...", "mcp.timeout": "20", ...}"""

    updates: Dict[str, Any]


class RuntimeConfigIn(BaseModel):
    """动态配置更新：{"file_auto_extract_enabled": "1", "default_project_id": "..."}

    合法键以 tools/verify/verify_settings_data_audit.py 审计结果为准（有真实消费点）；
    2026-10-23 已移除 query_route / graph_confidence_threshold 等 5 个零消费点键。"""

    updates: Dict[str, Any]
