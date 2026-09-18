"""AgentPipeline：编排管道——意图识别 → 技能/工具按需注入 → LLM 执行 → 结构化产物（card/report/sysml/stream）。"""
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
from .definition import AgentDefinition
from .registry import AgentRegistry
from .intent import IntentRouter
from .rag import GraphRAG, ConflictDetector

# P0-2：工具结果展示上限（SSE 事件与落库共用；超出截断并标记 truncated，前端可展开全文/复制）
_TOOL_RESULT_CAP = 6000

from .utils import (  # noqa: F401  （P1-5 拆分：纯函数移至 agent/utils.py，re-export 兼容旧引用）
    _citations_payload,
    _extract_code_blocks,
    _archive_impact_analysis,
    _archive_sysml_version,
    _archive_artifacts,
)
from .pipeline_parts.session import SessionMixin
from .pipeline_parts.skills import SkillMixin
from .pipeline_parts.orchestration import OrchestrMixin
from .pipeline_parts.memory import MemoryMixin
from .pipeline_parts.prompt import PromptMixin
from .pipeline_parts.tools import ToolMixin
from .pipeline_parts.execute import ExecuteMixin
from .pipeline_parts.stream import StreamMixin
from .pipeline_parts.cards import CardMixin
from .pipeline_parts.history import HistoryMixin
from .pipeline_parts.context import ContextMixin
from .pipeline_parts.common import *  # noqa: F401,F403  模块级名字 re-export 兼容旧引用


class AgentPipeline(SessionMixin, SkillMixin, OrchestrMixin, MemoryMixin, PromptMixin, ToolMixin, ExecuteMixin, StreamMixin, CardMixin, HistoryMixin, ContextMixin):
    """Agent execution pipeline: intent → retrieve → generate → validate → confirm → merge."""

    def __init__(self):
        self.router = IntentRouter()
        self.rag = GraphRAG()
        self.conflict = ConflictDetector()
        self.registry = AgentRegistry()  # P0-3: Agent 定义注册表（工具绑定 / HIL 分级）
        self._retry_policy_cache = {}    # D2：工具重试策略缓存

    def _load_db_agents(self, user=None) -> None:
        """P0 平台化：从 DB 加载可配置 Agent（registry + 意图关键词），DB 优先、内置兜底。

        每次执行前调用（agents 表可能被工坊管理界面修改），读操作不落写事务。

        P1-6（2026-09-16）：「安装即可消费」在路由层的两处生效——
          · 已停用/已卸载的能力，其 Agent 不再注册意图关键词（此前只按 agents.status 过滤，
            能力中心停用后仍能在路由层命中原 Agent）
          · 插件侧 Agent（无旧表承载）一并纳入路由，关键词取自 manifest.intent_keywords

        P1-8 用户隔离（2026-09-17）：新增 user 参数并透传到 registry.load_from_db ——
        路由池按当前用户判定可消费性，他人私有的 Agent 不再进入本用户的路由池。
        user=None（内部调用/CLI）退化为仅内置能力。
        """
        try:
            from database import get_db
            conn = get_db()
            try:
                self.registry.load_from_db(conn, user)
                rows = conn.execute(
                    "SELECT name, display_name, description, intent_keywords FROM agents WHERE status='active'"
                ).fetchall()
                sem_idx = []
                seen = set()
                for r in rows:
                    if not self.registry.has_intent(r["name"]):
                        continue      # 已被能力中心停用 → 不参与路由
                    try:
                        kws = json.loads(r["intent_keywords"] or "[]")
                    except Exception:
                        kws = []
                    self.router.register_keywords(r["name"], kws)
                    # 缺口-1：语义索引文本 = 展示名 + 描述 + 关键词（语义兜底路由用）
                    sem_idx.append({
                        "name": r["name"],
                        "text": f"{r['display_name'] or r['name']} {r['description'] or ''} {' '.join(kws)}",
                    })
                    seen.add(r["name"])
                # 插件侧 Agent（无旧表承载）：按 manifest 关键词注册
                for _n, _m in self.registry.plugin_agents().items():
                    if _n in seen:
                        continue
                    _kws = _m.get("intent_keywords") or []
                    self.router.register_keywords(_n, _kws)
                    sem_idx.append({
                        "name": _n,
                        "text": f"{_m.get('display_name') or _n} "
                                f"{_m.get('system_prompt') or ''} {' '.join(_kws)}",
                    })
                self.router.set_semantic_index(sem_idx)
            finally:
                conn.close()
        except Exception:
            pass  # DB 不可用时降级内置 DEFINITIONS
