# -*- coding: utf-8 -*-
"""pipeline_parts 公共依赖：所有 Mixin 共享的模块级导入与常量。

由 tools/split_pipeline.py 生成（源：agent/pipeline.py 头部）。方法体引用的
模块级名字全部集中在此，Mixin 通过 `from .common import *` 引入。
"""
import os
import re
import json
import time
import uuid
import logging

# 2026-09-17 S4：pipeline_parts 统一日志器（此前这些模块只 print/静默，运行时异常容易查无痕迹）
logger = logging.getLogger("mbse.agent.pipeline")

from database import get_db, db_conn
from knowledge_engine import VectorEngine, QueryRouter
from llm import llm_client
from core.config import STATIC_DIR
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES
from agent.definition import AgentDefinition
from agent.registry import AgentRegistry
from agent.intent import IntentRouter
from agent.rag import GraphRAG, ConflictDetector
from agent.utils import (
    _citations_payload,
    _extract_code_blocks,
    _archive_impact_analysis,
    _archive_sysml_version,
    _archive_artifacts,
)

# P0-2：工具结果展示上限（SSE 事件与落库共用；超出截断并标记 truncated）
_TOOL_RESULT_CAP = 6000

# 2026-09-17 S3：回填给模型的工具结果上限（远小于展示上限，防长工具结果反复占用输入 token）
_TOOL_MODEL_CAP = 3000
# 2026-09-17 S3：上传资料注入 prompt 的字符上限（att_text 不在 _apply_context_budget 裁剪范围内）
_ATT_INJECT_CAP = 4000

# 2026-09-17 S4（C3=按意图只注入命中 Agent）：流式路径注入「命中 Agent 角色块」的字符上限。
# 背景：SSE 路径此前**完全不消费** agents.system_prompt（硬编码一句通用角色），
#   导致库里 20 个 Agent 的定制（合计 44,762 字）在主流式链路上不生效 —— 用户配了 Agent 却看不到效果。
# 取值依据（实测 agents.system_prompt 长度）：核心 Agent 设计 476 / 评审 1011 / 影响 707 /
#   需求分析 392 / 报告 862 / 知识问答 766 / 闲聊 839 字 → 1200 字可容纳核心 Agent 的完整指令；
#   9 个「视图生成」Agent 为 2,500~7,981 字（它们的完整 prompt 由非流式 execute 路径消费），
#   在流式路径按 1200 字截断，代价 ≤ 约 300 token/请求，换取 Agent 定制真正生效。
_AGENT_ROLE_CAP = 1200

# ── P0-3 流式输出节奏（2026-09-17）：消除「一次性瞬发一大段」 ──────────────
# 速率取值依据：
#   · 中文朗读约 4–5 字/秒，阅读约 5–8 字/秒（人眼跟随的下限参考）
#   · 主流 LLM 客户端（ChatGPT / Claude）中文输出的实测观感约 50–150 字符/秒
#     —— 该区间即模型原生 token 输出速度，兜底伪流式对齐它才不会有「假」感
#   · 低于 ~15 字符/秒 明显「挤牙膏」；高于 ~300 字符/秒 观感接近瞬发
# 本组常量仅用于「文本已完整拿到、无法真流式」的兜底路径。
_STREAM_CHARS_PER_SEC = 45.0     # 理想速率（类人节奏基准）
_STREAM_MAX_CPS = 150.0          # 速率上限：防超长文在「总时长上限」内被压成瞬发
_STREAM_CHUNK = 24               # 切片粒度：中文 1 字 ≈ 1 token，24 字符 ≈ 1–2 个 SSE 事件
_STREAM_MAX_DRAIN_SEC = 60.0     # 总排空上限（秒）：长文加速但不拖尾数分钟


def iter_stream_chunks(text, chunk_size=None, chars_per_sec=None,
                       max_drain_sec=None, max_cps=None):
    """把「已完整拿到」的文本按类人节奏切片产出，替代 `for i in range(0, len(t), 24)` 式瞬发。

    仅用于无法真流式的兜底路径（编排汇总、工具探测轮直出、事后补吐的增量文本等）。
    能从 LLM 逐 token 取增量的路径应直接转发增量，不要套本函数，否则平白增加时延。

    三重节奏约束（对间隔取交集）：
      1) 理想间隔 —— chunk_size / chars_per_sec（默认 24/45 ≈ 0.53s，类人节奏）；
      2) 总时长上限 —— 整段在 max_drain_sec 内排空，超出则等比加速（长文不拖尾）；
      3) 速率上限 —— 间隔不低于 chunk_size / max_cps（默认 24/150 ≈ 0.16s），
         防止超长文在 (2) 的加速下退化回「一次性瞬发」。
    首个切片立即产出（首 token 时延 ≈ 0），末片之后不再 sleep（无收尾空等）。

    2026-09-17 实测校正：初版仅用「理想速率 + cap=5s」，实测 6169 字符 5.4s 吐完
    （1149 字符/秒，远超行业观感），故补 (3) 速率上限，并把 cap 上调至 60s。
    """
    if not text:
        return
    ck = int(chunk_size or _STREAM_CHUNK) or _STREAM_CHUNK
    cps = float(chars_per_sec or _STREAM_CHARS_PER_SEC) or _STREAM_CHARS_PER_SEC
    mcps = float(max_cps or _STREAM_MAX_CPS) or _STREAM_MAX_CPS
    cap = float(_STREAM_MAX_DRAIN_SEC if max_drain_sec is None else max_drain_sec)
    total = len(text)
    n_chunks = (total + ck - 1) // ck
    interval = ck / cps
    if cap > 0 and n_chunks > 1:
        interval = min(interval, cap / (n_chunks - 1))
    if mcps > 0:
        interval = max(interval, ck / mcps)
    for idx, i in enumerate(range(0, total, ck)):
        if idx and interval > 0:
            time.sleep(interval)
        yield text[i:i + ck]


__all__ = [
    'os', 're', 'json', 'time', 'uuid', 'logging', 'logger',
    'get_db', 'db_conn', 'VectorEngine', 'QueryRouter', 'llm_client', 'STATIC_DIR',
    'exec_file_tool', '_FILE_TOOL_NAMES', 'exec_report_tool', '_REPORT_TOOL_NAMES',
    'AgentDefinition', 'AgentRegistry', 'IntentRouter', 'GraphRAG', 'ConflictDetector',
    '_citations_payload', '_extract_code_blocks', '_archive_impact_analysis',
    '_archive_sysml_version', '_archive_artifacts', '_TOOL_RESULT_CAP',
    '_TOOL_MODEL_CAP', '_ATT_INJECT_CAP', '_AGENT_ROLE_CAP',
    '_STREAM_CHARS_PER_SEC', '_STREAM_MAX_CPS', '_STREAM_CHUNK', '_STREAM_MAX_DRAIN_SEC',
    'iter_stream_chunks',
]
