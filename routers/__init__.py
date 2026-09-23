"""API 路由层：按功能域拆分，main.py 仅负责装配。

每个子模块定义独立 APIRouter，URL 契约冻结（与重构前完全一致）。
新增功能域 = 新建子模块 + 在本文件注册 + main.py include_router 一行，
主文件不再随功能增长而膨胀。

依赖方向：routers → core / database / agent / llm（单向，禁止反向）。
"""
from .auth import router as auth_router
from .dashboard import router as dashboard_router
from .users import router as users_router
from .conversations import router as conversations_router
from .knowledge import router as knowledge_router
from .branches import router as branches_router
from .studio import router as studio_router
from .llm import router as llm_router
from .meta import router as meta_router
from .projects import router as projects_router
from .views import router as views_router
from .config import router as config_router
from .glossary import router as glossary_router
from .glossary_io import router as glossary_io_router
from .monitor import router as monitor_router
from .reports import router as reports_router
from .artifacts import router as artifacts_router
from .impact import router as impact_router
from .sysml_versions import router as sysml_versions_router
from .plugins import router as plugins_router
from .graph_db import router as graph_db_router
from .graph_workspace import router as graph_workspace_router
from .governance import router as governance_router
from .swrl import router as swrl_router  # P1-①/②（2026-09-11）SWRL 规则管理
from .sparql import router as sparql_router  # P2-①/②（2026-09-11）SPARQL 1.1 Endpoint
from .data_sources import router as data_sources_router  # P0-4（2026-09-20）数据源注册管理
from .coverage import router as coverage_router  # SRS-GN-CO（2026-09-20）覆盖性分析呈现端点
from .doc_folders import router as doc_folders_router  # 2026-09-21 文档目录树（基于文件的管理 P0-b）

__all__ = [
    "dashboard_router",
    "users_router",
    "conversations_router",
    "knowledge_router",
    "branches_router",
    "studio_router",
    "llm_router",
    "meta_router",
    "projects_router",
    "views_router",
    "config_router",
    "glossary_router",
    "glossary_io_router",
    "monitor_router",
    "reports_router",
    "artifacts_router",
    "impact_router",
    "sysml_versions_router",
    "plugins_router",
    "graph_db_router",
    "graph_workspace_router",
    "governance_router",
    "swrl_router",  # P1-①/②（2026-09-11）SWRL 规则管理
    "sparql_router",  # P2-①/②（2026-09-11）SPARQL 1.1 Endpoint
    "data_sources_router",  # P0-4（2026-09-20）数据源注册管理
    "coverage_router",  # SRS-GN-CO（2026-09-20）覆盖性分析呈现端点
    "doc_folders_router",  # 2026-09-21 文档目录树
]
