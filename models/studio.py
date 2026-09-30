"""AI 设计工坊域模型（自 routers/studio.py 原样搬移，P1-2 Models 外置）。"""
from typing import List, Optional

from pydantic import BaseModel


class PromptIn(BaseModel):
    name: str
    scenario: Optional[str] = ""
    content: str
    variables: Optional[List[str]] = []
    version: Optional[str] = "v1"


class SkillIn(BaseModel):
    name: str
    description: Optional[str] = ""
    skill_type: Optional[str] = "text2json"  # text2json | tool_call | llm_score | package
    triggers: Optional[List[str]] = []
    category: Optional[str] = ""
    content: Optional[str] = ""   # SKILL.md 指令正文
    frontmatter: Optional[str] = "{}"
    # SK-RL：注册表生命周期 + 角色权限
    dependencies: Optional[list] = []        # [{"name","min_version"}] 或 ["name"]；发布时校验依赖存在且已发布
    allowed_roles: Optional[List[str]] = []  # 允许调用角色（空=全部角色）
    # D4：分层结构 + 工具白名单
    allowed_tools: Optional[List[str]] = []  # 工具白名单（skill 触发时限制可调用工具，空=不限制）
    references: Optional[list] = []          # 参考文档清单（渐进披露）
    examples: Optional[list] = []            # 示例清单
    scripts: Optional[list] = []             # 脚本清单


class MCPIn(BaseModel):
    name: str
    endpoint: str
    tools: Optional[List[str]] = []
    transport: Optional[str] = "sse"   # sse | stdio | http
    command: Optional[str] = ""
    args: Optional[List[str]] = []
    env: Optional[dict] = {}


class ToolIn(BaseModel):
    """TR-P1：工具注册表条目（行业标准：schema/版本/副作用/风险/负责人）。"""
    name: str
    description: Optional[str] = ""
    source: Optional[str] = "local"          # local | builtin | mcp
    input_schema: Optional[dict] = {}        # JSON Schema（function calling 参数定义）
    version: Optional[str] = "v1.0"
    side_effect: Optional[str] = "read"      # read | write | destructive
    risk_level: Optional[str] = "low"        # low | medium | high
    owner: Optional[str] = ""
    allowed_roles: Optional[List[str]] = []  # TR-P3：允许调用角色（空=全部角色）
    retry_policy: Optional[dict] = {}        # D2：重试策略 {"max_retries","backoff_base_ms","backoff_multiplier","retry_on"}
    fallback_to: Optional[str] = ""          # D2：降级工具名（失败自动切换）
    # HTTP 工具专属字段（写入 config JSON）
    method: Optional[str] = ""               # GET | POST | PUT | DELETE | PATCH
    url: Optional[str] = ""                  # HTTP 端点 URL
    param_in: Optional[dict] = {}            # 字段映射 {"city":"path","units":"query"}
    timeout: Optional[int] = 15              # 超时秒数
    headers: Optional[dict] = {}             # 请求头 {"Authorization":"Bearer ..."}


class AgentIn(BaseModel):
    name: str
    display_name: str
    description: Optional[str] = ""
    system_prompt: Optional[str] = ""
    model_provider_id: Optional[int] = None
    model_params: Optional[dict] = {}
    hil_level: Optional[str] = "L0"
    kb_required: Optional[bool] = False
    kb_scope: Optional[dict] = None  # KB-S：None=不更新（保留既有值）；{}=清空；{mode,branches,docs}=自定义范围
    intent_keywords: Optional[List[str]] = []
    icon: Optional[str] = "🤖"
    status: Optional[str] = "active"
    version: Optional[str] = "v1.0.0"
    # Task 14：Agent 能力元数据（DB 列已就绪，Task 12 起 SQL 未写，本次补齐）
    capabilities: Optional[List[str]] = []        # 专长标签（registry._CAPABILITIES_ENUM 枚举词表 + 自定义短词）
    input_schema: Optional[dict] = {}             # 输入 JSON Schema
    output_schema: Optional[dict] = {}            # 输出 JSON Schema
    max_concurrency: Optional[int] = 2            # 最大并发子任务数（1-10）
    protocol_range: Optional[str] = ">=1,<3"      # A2A 协议版本范围
    agent_role: Optional[str] = "sub"             # main=主Agent（团队负责人，可设置子Agent成员）| sub=子Agent（团队成员）


class AgentToolIn(BaseModel):
    tool_type: str  # skill | mcp | tool | plugin
    tool_name: str
    # 2026-09-30 移除 params：原"工具默认入参"字段全仓只有写入、零处读取，且 agent_tools.params 列已删。


class A2AIn(BaseModel):
    """A2A 入站消息：支持两种形态——
    1) 直接字段：topic/content（快捷写入消息池）；
    2) 标准 A2A message：{protocol,version,message:{id,kind,contents},sender:{name,type,role}}。
    """
    protocol: Optional[str] = "a2a"
    version: Optional[str] = "0.1"
    message: Optional[dict] = None
    sender: Optional[dict] = None
    topic: Optional[str] = "external"
    content: Optional[object] = None
    run_id: Optional[int] = 0


class EventSubIn(BaseModel):
    run_id: Optional[int] = 0            # 0=全局订阅（任意 run 触发）
    node_id: Optional[str] = ""          # ''=全部节点；否则仅匹配该节点 id
    event_type: str = "node_done"        # node_done | node_error | run_completed
    webhook_url: str
    secret: Optional[str] = ""
