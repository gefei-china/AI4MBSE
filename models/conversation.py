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
    forced_intent: Optional[str] = ""  # 快捷指定 Agent：跳过意图识别直接定向
    skill_name: Optional[str] = ""     # 快捷指定 Skill：强制注入技能指令
    branch: Optional[str] = None       # AI 建模输出侧工作分支（默认 dev；release 只读不可作工作分支）
    team: Optional[str] = ""           # 团队模式：选择主智能体团队（AI 会话页「工作流」下拉）→ 主智能体负责意图识别/拆解/计划/分派/汇总
    scope_id: Optional[int] = None    # 建模范围：引用已保存范围（文件管理→建模范围，全局分词不分分支）
    scope: Optional[dict] = None      # 或内联范围 {"mode":"doc|fragment_group|chunk","doc_names":[...],"fragment_text":"...","chunk_ids":[...]}
    scope_ids: Optional[list] = []    # 多个已保存建模范围 id（可多选合并，文档取并集）


class RenameIn(BaseModel):
    title: str


class FlowRunIn(BaseModel):
    payload: Optional[str] = ""     # 用户输入/意图文本，注入流程 payload
    branch: Optional[str] = None
