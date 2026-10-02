# -*- coding: utf-8 -*-
"""pipeline_parts 公共依赖：所有 Mixin 共享的模块级导入与常量。

由 tools/split_pipeline.py 从 agent/pipeline.py 头部机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。方法体引用的
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


# ════════════════════════════════════════════════════════════════════════════
# P1-26（2026-10-02）：system_prompt 分层拼装 —— **顺序的单一真源**
#
# 背景：`execute.py`（非流式）与 `stream.py`（流式）**各自内联了一份拼接顺序**（约 95% 相同）。
#   实测已发生口径漂移：P1-24/P1-25 的顺序优化只改了 execute.py，**流式主路径完全未生效**。
#   此处把「顺序」收敛为唯一真源，两条路径只负责收集各自块内容 ⇒ 结构上不可能再漂移。
#
# 分层依据（两条独立证据）：
#   ① Anthropic Claude Code 的 system prompt 用**显式分界线** `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`
#      切开：线上是全局可缓存的静态层（身份/规则/工具），线下是动态层（会话指导/Memory/环境）。
#      并配 `DANGEROUS_uncachedSystemPromptSection(name, compute, reason必填)` 命名约定，
#      让「破坏缓存」在 code review 里可见。
#   ② DeepSeek Context Caching 按**前缀完整匹配**计费（命中 token 约 1/10 价）：
#      动态块一旦排在静态块之前，缓存前缀就在该处断掉，**其后所有内容**（包括跨 agent
#      完全一致的规则块）全部无法命中。
#
# 四层与实测频率（真库 4 个多轮会话逐条回放 IntentRouter.detect）：
#   L2 身份层   依赖 agent_def。**agent 由 intent 选出 ⇒ 变化率 ≈ 意图变化率 56.4%**。
#   L1 全局静态 无参方法（本体/边界/输出/引用规则），跨 agent、跨会话**完全一致**。
#   L3 会话级   依赖 intent（同会话内稳定；意图不变即命中）。
#   L4 每轮级   依赖 user_input / 检索 / 建模态（**几乎每轮都变**，不参与缓存）。
#
# ⚠️ 质量优先取舍：`role`（Agent 身份）保持**最前**。身份是 LLM 的首要上下文，
#   标杆（Claude Code 与主流 agent 框架）一致把身份放静态层最前 —— 不为了缓存把它后置。
#   代价：agent 变化时前缀立即断；这是「身份首位」的既定成本，不靠挪身份来省。
# ════════════════════════════════════════════════════════════════════════════

#: 分界线**以上**的块 key（可缓存区）。元组顺序 = 实际拼装顺序。
PROMPT_CACHEABLE_KEYS = (
    "role",            # L2 Agent 身份（最前，质量优先）
    "tools",           # L2 可用工具
    "tool_rules",      # L2 工具使用约束（仅流式路径注入）
    "roster",          # L2 团队名册（仅主 Agent 非空）
    "ontology",        # L1 本体 schema（无参，读 KB 本体）
    "boundary",        # L1 本体边界说明（无参静态）
    "output_rules",    # L1 输出规范（无参字面量）
    "citation_rules",  # L1 引用规范（无参字面量）
)

#: 分界线**以下**的块 key（动态区）。元组顺序 = 实际拼装顺序（层内按「依赖的易变度」升序，
#: 让相对稳定的块尽量靠前）。
PROMPT_DYNAMIC_KEYS = (
    "intent_line",     # L3 当前意图（依赖 intent）
    "model_code_req",  # L3 建模输出要求 + L0 硬约束卡（依赖 intent；建模/非建模差异巨大）
    "skill_prompt",    # L4 技能渐进式披露（依赖 user_input ⇒ 每轮可能变）
    "skill_block",     # L4 显式指定技能正文（依赖本轮 skill_name）
    "template",        # L4 提示词实验室模板（依赖 intent + user_input）
    "memory",          # L4 长期记忆（依赖 user_input）
    "model_context",   # L4 建模上下文（依赖 branch + conversation + user_input）
    "project_memory",  # L4 项目级记忆（依赖 user_input + conversation）
    "slots",           # L4 任务拆解（本轮 LLM 产出）
    "user_ctx",        # L4 用户上下文
    "attachment",      # L4 用户上传资料（本轮附件）
    "retrieval",       # L4 检索到的互联数据（本轮检索）
    "report",          # L4 报告模板（本轮）
)

#: 拼装顺序（**唯一真源**）：可缓存静态区 → 动态区。
PROMPT_BLOCK_ORDER = PROMPT_CACHEABLE_KEYS + PROMPT_DYNAMIC_KEYS

#: 分界线标记（等价 Claude Code 的 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`）。
#: ⚠️ 仅作**代码层标记**（供 tools/verify/verify_prompt_layering.py 判定层归属），
#: **不拼进 prompt 文本**（省 token，也避免给模型引入无意义噪声）。
PROMPT_LAYER_BOUNDARY = "── SYSTEM_PROMPT_DYNAMIC_BOUNDARY ──"


def assemble_system_prompt(blocks: dict) -> str:
    """按 `PROMPT_BLOCK_ORDER` 拼装 system_prompt —— **顺序的唯一真源**。

    两条路径（非流式 `execute.py` / 流式 `stream.py`）都调用本函数，只负责收集各自的块
    内容（传 dict），不再各自维护拼接顺序（此前各写一份 → 改一处漏一处、口径漂移）。

    空块（`None` / `""`）跳过，不留多余空行。未登记在 `PROMPT_BLOCK_ORDER` 的 key 会被
    **忽略** —— 该情形（新增块忘了分层归类）由 tools/verify/verify_prompt_layering.py 抓。
    """
    out = []
    for key in PROMPT_BLOCK_ORDER:
        val = blocks.get(key) or ""
        if val:
            out.append(val)
    return "".join(out)


# ── P1-4（2026-10-01）：会话槽位（DST slots）三层治理的纯函数 ─────────────
#  实测背景（全部取自生产库，非构造场景）：
#   ① `_merge_slots` 原为「纯并集 + 精确字符串去重」→ 同义不同串（空格/全角半角）绕过去重，
#      且跨轮单调增长、永不衰减：conv=514 累积到 7 实体 / 11 约束 / 351 字符，其中含
#      `先理解用户意图` 与 `需先理解用户意图`、`SysML v2 模型` 与 `SysML v2模型` 等同义副本；
#      对照单轮会话 conv=491 仅 187 字符且干净。
#   ② 污染主源在**上游**（L3）：澄清续答文本是**整段**进 `task_decompose` 的，
#      「问题「…」→ 回答：设计/建模：理解用户意图，SysML v2建模与视图代码」这类**元对话壳**
#      被当成任务描述拆成了"约束" —— 同一段 132 字符文本实测出现 3 次（id 3002/3010/3012）。
#  故三处各配一个纯函数，便于单测与变异自证。
#  ⚠️ 不引入任何**新标定阈值**：话题判据复用 `context.topic_sim_threshold`
#     （本仓曾因新标定阈值踩坑，见 `_build_model_context` 的注释）。
SLOT_CLARIFY_MARK = "【澄清补充】"
SLOT_ORIGINAL_MARK = "原请求："

# 话题承接词（**单一来源**）：`history._tag_topics` 与 `history._is_topic_switch` 共用。
# 承接式追问即使相似度低也不判为换话题（任务失败后用户打"重试"尤甚）。
TOPIC_CARRY_WORDS = ("继续", "接着", "还有", "另外", "再说", "那", "再",
                     "重试", "重跑", "重新", "再来", "retry", "continue")


def extract_original_request(text):
    """P1-4 L3：从澄清续答文本中抽取**用户本意**，其余是元对话壳。

    `routers/conversations.py` 构造的续答文本形如::

        【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：
        问题「…」→ 回答：设计/建模：理解用户意图，SysML v2建模与视图代码

        原请求：优化需求数据，需要支持参与者信息

    只有 `原请求：` 之后是用户本意。**取最后一次出现**（防嵌套引用），
    抽取为空则回退原文 —— 绝不把输入清空（清空会让 task_decompose 静默降级成 {}）。
    """
    if not isinstance(text, str) or not text or SLOT_ORIGINAL_MARK not in text:
        return text
    tail = text.rsplit(SLOT_ORIGINAL_MARK, 1)[-1].strip()
    return tail or text


def norm_slot_item(item):
    """P1-4 L1：槽位条目的**归一化判重键**（只用于比较，不改落库原文）。

    全角→半角（含全角空格 U+3000）→ 只保留字母数字汉字（剔除空白与标点）→ 小写。
    实测可合并：`SysML v2 模型` 与 `SysML v2模型` 归一后同为 `sysmlv2模型`。
    **不做**语义等价 —— `先理解用户意图` 与 `需先理解用户意图` 靠子串规则（见 merge_slot_items）。
    """
    s = item if isinstance(item, str) else str(item)
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    return "".join(ch for ch in "".join(out) if ch.isalnum()).lower()


def merge_slot_items(prev, new, *, max_items=0, normalize=True, substring=True):
    """P1-4 L1+L2：槽位列表合并 —— 归一化去重 + 可选子串合并 + 上限收敛（**本轮优先**）。

    规则：
      · 判重键 = `norm_slot_item`（normalize=False 时用原串）；
      · 键相同 → 丢弃后到者（保留**先到者的原文**，输出稳定）；
      · 子串包含（substring=True）→ 保留信息更全的那个：新项是旧项子串则丢新项，
        旧项是新项子串则**用新项替换旧项**（实测可合并 `先理解用户意图` ⊂ `需先理解用户意图`）；
      · 上限 `max_items > 0` 时：**本轮条目一律保留**，超出的只从**历史**条目里
        由旧到新丢弃（没有历史可丢则保留全部，不牺牲本轮信息）。

    返回 `(list, stats)`；`stats = {"dedup": n, "dropped_cap": n}` 供断言与观测。
    """
    stats = {"dedup": 0, "dropped_cap": 0}
    ns = [x for x in (new or []) if isinstance(x, str) and x.strip()]
    hs = [x for x in (prev or []) if isinstance(x, str) and x.strip()]

    def _key(x):
        return norm_slot_item(x) if normalize else x

    out = []   # [(item, key, is_new)]

    def _push(x, is_new):
        k = _key(x)
        if not k:
            return
        for i, (_, ek, _) in enumerate(out):
            if k == ek or (substring and k in ek):
                stats["dedup"] += 1
                return
            if substring and ek in k:
                out[i] = (x, k, is_new)   # 信息更全者胜（原地替换，保持位置）
                stats["dedup"] += 1
                return
        out.append((x, k, is_new))

    for x in hs:
        _push(x, False)
    for x in ns:
        _push(x, True)

    max_n = int(max_items or 0)
    if max_n > 0 and len(out) > max_n:
        excess = len(out) - max_n
        drop = [i for i, (_, _, is_new) in enumerate(out) if not is_new][:excess]
        if drop:
            ds = set(drop)
            out = [it for i, it in enumerate(out) if i not in ds]
            stats["dropped_cap"] = len(ds)
    return [it[0] for it in out], stats


def is_topic_switch(sim, shared, is_carry, threshold, use_shared=True):
    """P1-4 L2：话题切换判据（**单一来源**，两条调用路径共用阈值与承接词）。

    条件：相似度低于阈值 + 非承接式开头；`use_shared=True` 时再加「无共同 bigram」。
    抽成纯函数是为了让「打标」与「槽位重置」共用同一份判据 —— 两份实现必然漂移。

    `use_shared` 的两条路径为何不同（实测，见 `history._is_topic_switch` docstring）：
      · `_tag_topics` 打标（True，**保持原行为零回归**）：它的段代表向量随对话无限增长
        （实测 7879 个 bigram）→ 该条件恒假 → 它靠「默认偏不切」达成设计意图；
      · `_is_topic_switch` 槽位重置（False）：词袋仅 ~310，仅相似度即 8/8 + 8/8 完全分离，
        加 shared 只会引入漏切（异话题句恰好命中一个泛词即漏）。
    """
    try:
        if float(sim) >= float(threshold) or is_carry:
            return False
        return (not shared) if use_shared else True
    except Exception:
        return False


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
    # P1-26：system_prompt 分层拼装（顺序单一真源，两条路径共用）
    'PROMPT_CACHEABLE_KEYS', 'PROMPT_DYNAMIC_KEYS', 'PROMPT_BLOCK_ORDER',
    'PROMPT_LAYER_BOUNDARY', 'assemble_system_prompt',
    # P1-4：会话槽位治理（纯函数，便于单测与变异自证）
    'SLOT_CLARIFY_MARK', 'SLOT_ORIGINAL_MARK', 'TOPIC_CARRY_WORDS',
    'extract_original_request', 'norm_slot_item', 'merge_slot_items', 'is_topic_switch',
]
