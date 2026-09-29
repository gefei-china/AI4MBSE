"""项目域模型（自 routers/projects.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class ProjectIn(BaseModel):
    # 2026-09-24：id/code 改为可选 —— 创建项目走前端弹窗（Codex 风格卡片）时只填名称，
    # id（p+时间戳36进制）与 code（默认=名称）由后端自动生成，避免用户理解内部标识。
    id: Optional[str] = None
    name: str
    code: Optional[str] = None
    domain: str = ""
    description: str = ""
    scenario_template_id: Optional[str] = None
    ontology_profile_id: Optional[str] = None
    # ── 建模工具绑定（2026-09-24 泛化：不限于智源，也支持 MagicDraw）──
    # 落库为 projects.tool_binding JSON {"tool","ref","name"}；空 tool = 本地建模。
    # 已支持 tool：zhiyuan（智源，连接器已实现）| magicdraw（Cameo，通道预留：文件导入/SysML v2 API/Cameo 插件）。
    tool: str = ""                  # 建模工具标识（空 = 本地建模，不连外部工具）
    tool_ref: str = ""              # 工具侧工程标识：智源=vc(branchId,queryType)；MagicDraw=TWC 工程ID/工程名
    tool_name: str = ""             # 工具侧工程名（冗余存，供展示与名称核对，防"同名不同工程"）
    # ── 项目数据来源（2026-09-24 用户拍板：项目 = 本地工作空间 / 远端 SSH 连接）──
    # source='local'  → workspace 必填（本地工作空间绝对路径，前端「选本地文件夹」写入）
    # source='remote' → 下列 remote_* 字段构成 SSH 连接（对应「添加 SSH 连接」弹窗）
    source: str = "local"
    workspace: str = ""
    remote_display_name: str = ""   # 显示名称
    remote_host: str = ""           # 主机名（host.com 或 user@host.com）
    remote_port: str = ""           # SSH 端口（可选）
    remote_auth_mode: str = "none"  # none=无身份验证 | key=身份文件
    remote_identity_file: str = ""  # 身份文件路径（auth_mode=key 时必填）
