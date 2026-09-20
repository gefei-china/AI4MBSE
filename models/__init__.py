"""统一 Pydantic 模型层（P1-2 Models 外置）。

routers/ 不再内嵌任何 BaseModel 定义，一律 from models import ...。
迁移铁律：只搬不重构——字段/类型/默认值/校验与原 routers 定义 100% 一致。
"""
from .config import StaticConfigIn, RuntimeConfigIn
from .llm import ProviderIn, ProviderKeyIn
from .monitor import AlertRuleIn
from .reports import ReportExportIn, ReportSaveIn
from .conversation import ConvIn, ChatIn, RenameIn, FlowRunIn
from .meta import DocMetaIn
from .glossary import GlossaryIn, DomainReviewIn
from .knowledge import (
    EntityIn,
    BatchReviewIn,
    GraphNodeIn,
    GraphEdgeIn,
    OntologyTypeIn,
    V2GExtractIn,
    V2GConfirmIn,
    V2GRejectIn,
    V2GUpdateIn,
    SysMLIn,
    MergeIn,
    RetrieveIn,
)
from .studio import (
    PromptIn,
    SkillIn,
    MCPIn,
    ToolIn,
    AgentIn,
    AgentToolIn,
    A2AIn,
    EventSubIn,
)
from .user import RoleIn, UserIn, UserStatusIn, DepartmentIn
from .project import ProjectIn

__all__ = [
    "StaticConfigIn", "RuntimeConfigIn",
    "ProviderIn", "ProviderKeyIn",
    "AlertRuleIn",
    "ReportExportIn", "ReportSaveIn",
    "ConvIn", "ChatIn", "RenameIn", "FlowRunIn",
    "DocMetaIn",
    "GlossaryIn", "DomainReviewIn",
    "EntityIn", "BatchReviewIn", "GraphNodeIn", "GraphEdgeIn", "OntologyTypeIn",
    "V2GExtractIn", "V2GConfirmIn", "V2GRejectIn", "V2GUpdateIn", "SysMLIn", "MergeIn", "RetrieveIn",
    "PromptIn", "SkillIn", "MCPIn", "ToolIn", "AgentIn", "AgentToolIn", "A2AIn", "EventSubIn",
    "RoleIn", "UserIn", "UserStatusIn", "DepartmentIn",
    "ProjectIn",
]
