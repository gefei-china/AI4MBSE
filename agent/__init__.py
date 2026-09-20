"""Agent orchestration engine - intent recognition, GraphRAG, pipeline execution.

解耦拆分（2026-08）：原单体 3482 行 → 包结构，对外导入契约不变（agent/AgentPipeline/AgentRegistry/IntentRouter/GraphRAG/ConflictDetector/AgentDefinition）：
- definition.py AgentDefinition（Agent 定义）
- registry.py   AgentRegistry（意图 → Agent 定义注册表）
- intent.py     IntentRouter（三层混合意图识别）
- rag.py        GraphRAG + ConflictDetector（图谱检索 / 冲突检测）
- pipeline.py   AgentPipeline（编排管道）
"""
from .definition import AgentDefinition
from .registry import AgentRegistry
from .intent import IntentRouter
from .rag import GraphRAG, ConflictDetector
from .pipeline import AgentPipeline

# 兼容原单体模块级名称（外部脚本/测试可能按 agent.xxx 访问）
from database import get_db, db_conn  # noqa: E402
from knowledge_engine import VectorEngine, QueryRouter  # noqa: E402
from llm import llm_client  # noqa: E402
from core.config import STATIC_DIR  # noqa: E402
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES  # noqa: E402
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES  # noqa: E402

# Global instance
agent = AgentPipeline()
