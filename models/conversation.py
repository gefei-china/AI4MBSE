"""对话域模型（自 routers/conversations.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class ConvIn(BaseModel):
    title: str
    intent: Optional[str] = ""
    # 2026-09-24：任务归属项目（左侧「项目 → 新建任务」发起时显式带上）。
    # ⚠️ 2026-09-28（多工程 P0-2）语义变更：**留空 = 无工程会话**（合法：知识检索/问答不读写工程），
    # 后端**不再**回落 `settings.default_project_id`（全局单行、不分标签页，多工程并发会串归属）。
    # 「当前工程」由前端按页面级状态显式带上。
    project_id: Optional[str] = None


class ConvProjectIn(BaseModel):
    """会话归属变更（收敛入口）：无工程会话 → 归入项目；空串 = 解除归属。"""
    project_id: Optional[str] = ""


class ChatIn(BaseModel):
    message: str
    provider_id: Optional[int] = None
    attachments: Optional[list] = []  # V2.3: [{url, filename, size, is_image}]
    forced_intent: Optional[str] = ""  # ⚠️ 编排内部直连指定 Agent 用（workflows/nodes·planner）。**不再由前端发送**
                                        #   —— 2026-10-16 起执行主体唯一由 `team`（主 Agent）承载，见 R1
    intent_override: Optional[str] = ""  # 澄清改选：只改判本轮意图，**不改变执行主体**（执行主体永远是所选主 Agent）
    skill_name: Optional[str] = ""     # 快捷指定 Skill：强制注入技能指令
    branch: Optional[str] = None       # AI 建模输出侧工作分支（默认 dev；release 只读不可作工作分支）
    team: Optional[str] = ""           # ★ 执行主体唯一入口：所选主 Agent（AI 会话页「工作流」下拉）→ 其负责意图识别/拆解/计划/分派/汇总；
                                        #   **无需子 Agent**——主 Agent 可直接绑定 skill/工具/MCP 自主完成任务（2026-10-16 R2）
    scope_id: Optional[int] = None    # 建模范围：引用已保存范围（文件管理→建模范围，全局分词不分分支）
    scope: Optional[dict] = None      # 或内联范围 {"mode":"doc|fragment_group|chunk","doc_names":[...],"fragment_text":"...","chunk_ids":[...]}
    scope_ids: Optional[list] = []    # 多个已保存建模范围 id（可多选合并，文档取并集）


class RenameIn(BaseModel):
    title: str


class FlowRunIn(BaseModel):
    payload: Optional[str] = ""     # 用户输入/意图文本，注入流程 payload
    branch: Optional[str] = None
