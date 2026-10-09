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
from dataclasses import dataclass

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


# ════════════════════════════════════════════════════════════════════════════
# P1-28（2026-10-02）：system_prompt **块内容**的单一真源
#
# P1-26 收敛了「拼装顺序」；本节收敛「块内容」。实测两条路径**已漂移两处**：
#   · `role`        execute 不截断（`cap=0`） / stream 截断（`cap=_AGENT_ROLE_CAP`）
#   · `tool_rules`  **仅 stream 有**（execute 整块缺失）
#   · `skill_block` execute 带 references/examples/scripts/白名单披露 / stream 只有正文
#
# ⚠️ 这不是"未来的漂移风险"，而是**当下的功能不一致**：`ExecuteMixin.execute` 还被
#   Studio「Agent 试跑」（`routers/studio_parts/agents.py:311`）与
#   工作流 LLM 节点（`workflows/nodes.py:88` / `:676`）消费，且 `dry_run=True` **不跳过**
#   本段拼装 ⇒「试跑看到的 Agent」≠「对话里的 Agent」（取证见 docs §25.2 补注 / §29.9）。
#
# 本批纪律 —— **零行为变动**：
#   ① 差异一律**显式参数化**（`role_cap` / `include_tool_rules` / `content_cap` /
#      `include_resources`），**不抹平**；"要不要统一"是另一个决策（需质量 A/B）。
#   ② 等价性由 `tools/verify/verify_prompt_blocks.py` 证明：它从**旧源码 AST** 抽出原来的
#      dict 表达式就地执行作为基线，与新函数输出逐字节比对（不是手抄一份判据）。
# ════════════════════════════════════════════════════════════════════════════

#: 工具使用约束文案（**唯一真源**；此前只内联在 `stream.py` 的 blocks dict 里）
TOOL_RULES_TEXT = ("工具使用约束：仅调用完成当前任务所必需的工具，一次最多调用 2 个；"
                   "工具返回与任务无关、结果为空或已足够作答时，直接基于已有信息回答，"
                   "禁止反复/连环调用工具。\n")

#: 流式技能正文截断上限（`skill_block` 的 `content_cap`）。**流式路径专用**：
#: 技能正文可能很长，而流式请求每轮都带 ⇒ 按 4000 字控 token（execute 路径不截断）。
_SKILL_CONTENT_CAP = 4000


@dataclass
class PromptBlocksCtx:
    """两条路径共有的块构造入参（**唯一真源**的输入契约）。

    凡"由调用方按各自上下文算好、只用来填块"的值一律走这里（如 `att_text` / `context_text`
    —— 它们的**算法本身**两条路径仍有差异，属既定范围，本批不动，见 §29.9）。
    凡"块构造内部才有的差异"一律用末尾三个参数**显式**表达。
    """
    agent_def: object
    intent: str
    hil_level: str
    user_input: str
    user: object
    branch: str
    conversation_id: object
    slots: object
    user_ctx: str
    att_text: str
    context_text: str
    report_prompt: str
    skill_block: str = ""
    #: `_build_role_block` 的字符上限：0 = 不截断（execute 原行为）/ `_AGENT_ROLE_CAP`（stream）。
    role_cap: int = 0
    #: 是否注入 `tool_rules`：execute 原本**没有**这一块，stream 有 ⇒ 显式保留差异。
    include_tool_rules: bool = False


def build_slots_block(slots) -> str:
    """「【任务拆解（P1 结构化）】」块（**唯一真源**；两条路径此前逐字重复同一段 f-string）。

    `slots` 为空 → 返回 `""`（由 `assemble_system_prompt` 跳过，不留多余空行）。
    """
    if not slots:
        return ""
    return ("【任务拆解（P1 结构化）】\n目标：%s\n实体：%s\n约束：%s\n范围：%s\n"
            % (slots.get("goal") or "-",
               "、".join(slots.get("entities") or []) or "-",
               "；".join(slots.get("constraints") or []) or "-",
               json.dumps(slots.get("scope") or {}, ensure_ascii=False) if slots.get("scope") else "-"))


def build_skill_block(skill, *, content_cap: int = 0, include_resources: bool = True):
    """构造「【指定技能：…】」块（**唯一真源**）。返回 `(block, allowed_tools)`。

    差异**显式参数化**（见模块头 P1-28）：
      · `content_cap` 0 = 不截断（execute 原行为）/ `_SKILL_CONTENT_CAP`（stream 原行为）
      · `include_resources` True = 追加 references/examples/scripts/工具白名单披露（execute）
        / False = 只给正文（stream 原行为）

    ⚠️ `allowed_tools` 由**调用方**决定是否设为权威白名单：
      `execute.py` 会设 `_skill_forced` + `_skill_allowed_tools`；**`stream.py` 当前不设**
      （`_skill_forced` 全仓只在 `execute.py:220` 赋值）—— 该差异**如实保留、不在本批静默统一**。
      另注：stream 的 SQL 只 `SELECT name, content, description`，**根本没查** references/
      examples/scripts/allowed_tools ⇒ 即便给它 `include_resources=True` 也拿不到数据。
      两条均登记于 §29.9，属**独立缺陷**，需单独验证与 A/B。
    """
    if not skill:
        return "", set()
    content = skill.get("content") or ""
    if content_cap and len(content) > content_cap:
        content = content[:content_cap]
    block = "【指定技能：%s（必须遵循其完整指令）】\n%s" % (skill.get("name"), content)
    _at = []
    if include_resources:
        _rr = [str(r) if isinstance(r, str) else str(r.get("title") or r.get("path") or r)
               for r in (skill.get("references") or [])]
        _ee = [str(e) if isinstance(e, str) else str(e.get("title") or e.get("path") or e)
               for e in (skill.get("examples") or [])]
        _ss = [str(x) for x in (skill.get("scripts") or [])]
        if _rr:
            block += "\n📄 参考文档（需要时按需读取）：%s" % "；".join(_rr[:8])
        if _ee:
            block += "\n📝 示例（需要时按需读取）：%s" % "；".join(_ee[:8])
        if _ss:
            block += "\n⚙ 脚本（需要时执行）：%s" % "；".join(_ss[:8])
        _at = [str(t) for t in (skill.get("allowed_tools") or []) if str(t)]
        if _at:
            block += "\n🔒 工具白名单（仅可调用）：%s" % ", ".join(_at)
    return block + "\n", set(_at)


def build_prompt_blocks(pipe, ctx: PromptBlocksCtx) -> dict:
    """构造两条路径共有的块集合 —— **内容的唯一真源**（P1-28）。

    `pipe` 是 pipeline 实例（提供 `_build_*` / `_team_roster_block` 等方法；这些方法的实现
    本就共用，本函数只负责"用哪些、按什么参数调"）。
    返回 dict 交由 `assemble_system_prompt` 按 `PROMPT_BLOCK_ORDER` 拼装 —— 顺序不在这里定。
    """
    # ⚠️ `cap` **只在 >0 时传**：让本处与旧代码的**调用形态逐字一致**
    #    （execute 原为 `_build_role_block(agent_def)`、stream 原为 `(agent_def, cap=_AGENT_ROLE_CAP)`）。
    #    这样"零行为变动"不依赖"我知道默认值是 0"这个隐含前提 —— 等价性可被逐字节证明。
    _role_kw = {"cap": ctx.role_cap} if ctx.role_cap else {}
    blocks = {
        # ── L2 身份层 ──
        "role": "%s\n" % pipe._build_role_block(ctx.agent_def, **_role_kw),
        "tools": "可用工具：%s。\n" % (", ".join(ctx.agent_def.tools) or "无（纯问答直出）"),
        "roster": pipe._team_roster_block(ctx.agent_def),
        # ── L1 全局静态 ──
        "ontology": pipe._build_ontology_hint(),
        "boundary": pipe._build_boundary_hint(),
        "output_rules": pipe._build_output_rules(),
        "citation_rules": pipe._build_citation_rules(),
        # ── 分界线（PROMPT_LAYER_BOUNDARY）以下为动态区 ──
        # ── L3 会话级 ──
        "intent_line": "当前意图：%s（Agent: %s，HIL 人机协作级别：%s）。\n"
                       % (ctx.intent, ctx.agent_def.name, ctx.hil_level),
        "model_code_req": pipe._build_model_code_req(ctx.intent, ctx.agent_def),
        # ── L4 每轮级 ──
        "skill_prompt": pipe._build_skill_prompt(ctx.intent, ctx.user_input, ctx.user),
        "skill_block": ctx.skill_block or "",
        "template": pipe._build_prompt_template(ctx.intent, ctx.user_input, ctx.user),
        "memory": pipe._build_memory_hint(ctx.user_input, ctx.intent, ctx.user),
        "model_context": pipe._build_model_context(ctx.branch, ctx.conversation_id,
                                                   ctx.user_input),
        "project_memory": pipe._build_project_memory(user_input=ctx.user_input,
                                                     conversation_id=ctx.conversation_id),
        "slots": build_slots_block(ctx.slots),
        "user_ctx": ctx.user_ctx or "",
        "attachment": pipe._build_attachment_block(ctx.att_text),
        "retrieval": "检索到的互联数据：\n%s" % ctx.context_text,
        "report": ("\n\n%s" % ctx.report_prompt) if ctx.report_prompt else "",
    }
    if ctx.include_tool_rules:
        blocks["tool_rules"] = TOOL_RULES_TEXT
    return blocks


# ════════════════════════════════════════════════════════════════════════════
# P1-29（2026-10-02）：**显式指定技能**（`skill_name`）的注入 —— 两条路径统一入口
#
# 背景（三条实测差异，`tools/verify/verify_skill_injection.py` 稳定复现）：
#   ① `_skill_forced` 此前**只在 `execute.py` 赋值** ⇒ 对话主路径（`stream.py`）上该机制
#      **未接线**，`skills.py:153/163` 会按"未强制"重置并**只并集自动命中**技能的白名单
#      ⇒ 用户**显式指定**的技能，其 `allowed_tools` 授权可能不生效。
#      （`_skill_allowed_tools` 的语义是**授权补充**：`tools.py:145` 把它声明的工具**补入**
#      候选；它不做拦截 —— 拦截是 `_tool_whitelist`（委派最小权限）的职责。）
#   ② `stream.py` 的技能 SQL 只查 3 列（`name/content/description`）⇒ references/examples/
#      scripts/allowed_tools **拿不到**，渐进披露在对话路径上无数据可用。
#   ③ `_skill_allowed_tools` 没有"每轮起点清零" ⇒ `routers/conversations.py:14` 用的是
#      **全局单例** `from agent import agent`，实例属性会**跨轮/跨路径残留**
#      （上一轮显式技能的授权被带到下一轮，或 execute 路径的状态漏进流式对话）。
#
# ⚠️ 本组函数**会改变运行时行为**（显式技能的白名单授权真正生效 + 资源披露可用），
#    这是**修复**（对齐 `execute.py` 既有设计意图「指定技能白名单为权威限制，禁止被自动路由
#    重置/并集」），不是纯搬运 ⇒ 与 P1-28 分属两批，证据见 `tools/verify/verify_skill_injection.py`。
# ════════════════════════════════════════════════════════════════════════════

def reset_request_state(pipe) -> None:
    """**请求级瞬态状态的统一清零点**（execute / stream 两路径都必须调用）。

    ── 为什么必须有这个函数（2026-10-09 实测，非推断）──
    对话入口是 `routers/conversations.py` 的 `from agent import agent`
    ⇒ **模块级单例，所有用户、所有请求共用同一个 AgentPipeline 实例**。
    而 Pipeline 把"本次请求的中间产物"挂在 `self` 上（实例属性），
    这些字段**只要入口不清零就会跨请求残留**。

    实测到的真实危害（N3 视图展开 6/8 产出雷同的根因）：
        `tools.py` 在 `sysml_v2_*` 分派里写 `self._sysml_last_pass_code`，
        `cards.py` 的 `_ensure_sysml_from_tools` / `_gen_sysml_views`
        在**正文没代码时**拿它兜底交付 ⇒ 上一个请求的代码被当成本次产出交给用户。
        现象正是此前记录到的"叙述是新的、代码是旧的"。

    ⚠️ 这类泄漏**不会被任何现有门禁抓到**，因为：
      单次跑必绿（首个请求本来就是干净的），只有**同实例连跑**才暴露。
    ⇒ 必须靠"入口无条件清零"这一条不变量 + 专门的门禁守住。

    ── 为什么要"无条件" ──
    与 `reset_skill_state` 同理：写成"if 有值才设"时，脏值恰好是**上一轮留下的**
    ⇒ 条件永远成立 ⇒ 等于没清。清零必须与"本轮是否用到"无关。

    ── 清单怎么定 ──
    **不在这里凭记忆列**，而是扫 `agent/pipeline_parts/*.py` 里所有 `self.X =`
    赋值点，与本函数逐项比对；新增字段必须同步登记
    （门禁 `verify_request_state_reset` 会查差集）。
    """
    # ── 分派状态：本次执行允许调什么 / 由谁调 ──
    pipe._tool_whitelist = None          # 工具白名单
    pipe._tool_conv_ctx = None           # 工具调用日志归属会话（否则日志串会话）
    pipe._tool_user = None               # 工具审计归属用户（跨用户泄漏审计链）
    pipe._tool_intent_ctx = None         # 工具审计归属意图
    pipe._tool_agent_ctx = None          # 工具审计归属 Agent
    pipe._skill_allowed_tools = None     # 显式技能授权（见 reset_skill_state 的历史注释）
    pipe._skill_forced = False
    pipe._hil_level = None               # HIL 分级（人工确认队列的判定依据）
    # ── 技能注入：本次绑了哪些技能、正文被 offload 到哪里 ──
    pipe._last_skill_hits = []           # 本次命中的技能（门禁与前端展示都读它）
    pipe._skill_body_offloads = []       # ★ 本次 offload 的正文 id（不清 ⇒ 收尾注入上一轮的正文）
    # ── 记忆作用域：mem0 式读写的作用域 ──
    pipe._mem_ctx = None
    pipe._mem_project_id_cache = None    # 每次执行清缓存：缓存只在本请求内有效，防跨会话串味
    # ── ★ SysML 交付兜底缓存（本条即"产出雷同"的根因字段）──
    pipe._sysml_last_checked_code = None
    pipe._sysml_last_pass_code = None
    # ── 编排 / 会话治理的瞬态标记 ──
    pipe._orch_error = None
    pipe._last_clarify_skip = ""
    pipe._slot_merge_stats = {}
    pipe._last_compaction = None
    pipe._hook_warn = ""


def reset_skill_state(pipe, skill_name) -> None:
    """显式技能注入的**状态起点**（两路径统一调用，且必须在任何分支之前）。

    每轮无条件重置两项：`_skill_forced = bool(skill_name)`、`_skill_allowed_tools = None`。

    ⚠️ 为什么必须**无条件清零**而不是"有技能时才设"：`_skill_allowed_tools` 是**实例属性**，
    而对话走的是**全局单例**。只在"有白名单时赋值"会让上一轮的值**残留到本轮**
    （表现为"这轮明明没指定技能，却拿到了上一个技能的授权"），且极难复现。
    """
    pipe._skill_forced = bool(skill_name)
    pipe._skill_allowed_tools = None


def load_forced_skill(skill_name, *, content_cap: int = 0, include_resources: bool = True,
                      require_content: bool = False):
    """读取**显式指定**的技能并造块。返回 `(block, allowed_tools)`。

    与 `stream.py` 旧实现相比，读取入口统一为 `StudioRepo.get_skill_by_name`
    （`SELECT *` + JSON 列解析）—— 旧 SQL 只查 3 列，`references/scripts/allowed_tools`
    根本拿不到，披露**无数据可用**。

    ⚠️ `require_content` 是一条**如实保留**的既有差异（不是遗漏）：
      · `execute.py` 传 `False` —— 原行为是 `if sk:`，正文为空**也注入**（其资源披露仍有价值）；
      · `stream.py` 传 `True`  —— 原行为是 `if row and row["content"]:`，正文为空**不注入**。
    两类行为都合理，统一属独立决策 ⇒ 显式参数化，避免"顺手"改掉任一侧。

    任何异常 / 技能不存在 → `("", set())`：**技能取不到不该让整轮对话失败**。
    """
    if not skill_name:
        return "", set()
    try:
        from database import get_db as _gdb
        from repositories.studio_repo import StudioRepo as _SR
        conn = _gdb()
        try:
            sk = _SR(conn).get_skill_by_name(skill_name)
        finally:
            conn.close()
    except Exception:
        return "", set()
    if not sk:
        return "", set()
    if require_content and not sk.get("content"):
        return "", set()
    return build_skill_block(sk, content_cap=content_cap, include_resources=include_resources)


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
    # P1-28：system_prompt 块内容单一真源（两条路径共用；差异显式参数化）
    'TOOL_RULES_TEXT', '_SKILL_CONTENT_CAP', 'PromptBlocksCtx',
    'build_slots_block', 'build_skill_block', 'build_prompt_blocks',
    # P1-29：显式指定技能的注入（两路径统一入口 + 每轮状态起点）
    'reset_skill_state', 'load_forced_skill',
    # 请求级瞬态状态清零点（execute / stream 两路径统一调用，防单例跨请求泄漏）
    'reset_request_state',
    # P1-4：会话槽位治理（纯函数，便于单测与变异自证）
    'SLOT_CLARIFY_MARK', 'SLOT_ORIGINAL_MARK', 'TOPIC_CARRY_WORDS',
    'extract_original_request', 'norm_slot_item', 'merge_slot_items', 'is_topic_switch',
]
