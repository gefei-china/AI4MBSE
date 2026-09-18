"""repositories 包：SQL 收拢层（P2）。

原则：
- 每个功能域一个 repo，继承 BaseRepo，只做数据访问
- 路由层不得出现裸 SQL，一律通过 repo 调用
- 依赖方向：repositories → core，不依赖 routers/agent/llm

域映射：
- DashboardRepo   → 仪表盘（跨表聚合统计）
- UserRepo        → 用户/角色（roles/users）
- ConversationRepo→ 对话（conversations/messages/feedback）
- KnowledgeRepo   → 知识库（entities/relations/ontology_types）
- BranchRepo      → 分支（branches/merge_requests）
- StudioRepo      → 定制化中心（prompts/skills/mcp/tools/rules/flows）
- LlmRepo         → LLM 配置（llm_providers）
- MetaRepo        → 历史/反馈/审计/数据源/设置/文档
"""
from repositories.base import BaseRepo
from repositories.dashboard_repo import DashboardRepo
from repositories.user_repo import UserRepo
from repositories.conversation_repo import ConversationRepo
from repositories.knowledge_repo import KnowledgeRepo
from repositories.branch_repo import BranchRepo
from repositories.studio_repo import StudioRepo
from repositories.llm_repo import LlmRepo
from repositories.meta_repo import MetaRepo

__all__ = [
    "BaseRepo",
    "DashboardRepo",
    "UserRepo",
    "ConversationRepo",
    "KnowledgeRepo",
    "BranchRepo",
    "CommitRepo",
    "StudioRepo",
    "LlmRepo",
    "MetaRepo",
]
