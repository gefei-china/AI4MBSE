"""Agent 定义（P0-4 可配置化）：名称/描述/工具绑定/技能/HIL 级别/知识库依赖。"""
import os
import re
import json
import time
import uuid
from database import get_db, db_conn
from knowledge_engine import VectorEngine, QueryRouter  # P1: 双引擎底座
from llm import llm_client
from core.config import STATIC_DIR
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES  # 基础通用文件操作工具
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES  # 基础通用报告导出工具


class AgentDefinition:
    """Agent 定义（P0-4 可配置化）：名称/描述/工具绑定/技能/HIL 级别/知识库依赖。

    hil_level 对齐方案 HIL 三级分级：
    - L0 Direct 纯问答直出不打断
    - L1 Review 生成草稿/候选预览 + 一键采纳/丢弃
    - L2 Confirm 写库/对外生效逐条确认队列（FR-HIL-1 强制）
    """

    def __init__(self, name, description, tools=None, hil_level="L0",
                 kb_required=False, skill=None, model_hint="", kb_scope=None, system_prompt=None,
                 agent_role="sub", intent_name=""):
        self.name = name
        self.description = description
        self.tools = tools or []
        self.hil_level = hil_level
        self.kb_required = kb_required
        self.skill = skill
        self.model_hint = model_hint
        # KB-S: Agent 级知识库消费范围 {"mode":"all_release"|"custom","branches":[],"docs":[]}
        # mode=all_release（默认/空）：仅已发布(release)分支；mode=custom：按 branches/docs 过滤
        self.kb_scope = kb_scope or {}
        # SP-O：角色化系统提示词（内置角色 prompt；DB 自定义 system_prompt 优先覆盖）
        self.system_prompt = system_prompt
        # 主/子 Agent 团队：main=团队负责人（可设置子 Agent 成员）| sub=团队成员（两级封顶）
        self.agent_role = agent_role
        # DB 意图标识（agents.name；self.name 为 display_name，查库/路由用 intent_name）
        self.intent_name = intent_name or name

    def to_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "hil_level": self.hil_level,
            "kb_required": self.kb_required,
            "kb_scope": self.kb_scope,
            "system_prompt": self.system_prompt,
            "skill": self.skill,
            "model_hint": self.model_hint,
            "agent_role": self.agent_role,
            "intent_name": self.intent_name,
        }

