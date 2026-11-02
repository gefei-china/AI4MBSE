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
    # ★ 2026-10-17（米爸 Q1 DB 层收口）：澄清续答的**单轮化**两个转呈字段。
    #   由 /clarify-answer 生成、前端原样带回 —— 后端不信任客户端自行编造 id，
    #   只认 clarify-answer 当时从本会话查出来的那一条。
    resume_of_user_msg_id: Optional[int] = 0   # 触发本轮澄清的那条 user 消息 id（追加目标）
    resume_append: Optional[str] = ""          # 要追加进该 user 消息的澄清补充文本（不含"原请求"复述）
    # ★ 2026-10-31（会话入口「本轮临时调整」）：**只作用于本次请求**的推理参数覆盖。
    #   为什么单独开一组字段而不复用 provider_id 的 model_params：
    #     用户的心智是"这一轮我想多想一点/看长一点"，而 model_params 是**该模型的全局配置**；
    #     把临时意图写进全局配置 ⇒ 调一次之后所有会话都变了（最难发现的副作用）。
    #   语义：留空 = 不覆盖（沿用模型配置，零行为漂移）；有值 = 本轮压过模型配置。
    #   优先级：本轮覆盖 > Agent 级 model_params > provider 级 > 模型默认。
    o_think: Optional[str] = ""       # 思考强度：模型默认|关闭|低|中|高（空=不覆盖）
    o_context: Optional[int] = 0      # 本轮上下文窗口（tokens，0=不覆盖）
    o_max_tokens: Optional[int] = 0   # 本轮输出上限（tokens，0=不覆盖）
    o_temperature: Optional[float] = None  # 本轮温度（None=不覆盖；0 是合法值）


class RenameIn(BaseModel):
    title: str


class FlowRunIn(BaseModel):
    payload: Optional[str] = ""     # 用户输入/意图文本，注入流程 payload
    branch: Optional[str] = None
