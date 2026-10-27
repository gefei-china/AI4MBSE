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
from web_tools import WEB_TOOL_NAMES as _WEB_TOOL_NAMES   # 联网搜索/网页抓取（2026-10-17）
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

# P0-2：工具结果展示上限（SSE 事件与落库共用；超出截断并标记 truncated，前端可展开全文/复制）
# ★ 2026-10-27（截断清单 P1-B2）：6000 → 8000，与回填给模型的上限（_TOOL_MODEL_CAP）**取同值**。
#   原「展示 6000 / 给模型 3000」的双口径会让人误以为模型看到的就是 6000；
#   统一后"落库-展示-回填"三处同一把尺，排查时不必再换算。
_TOOL_RESULT_CAP = 8000

# ── 2026-10-10 主对话任务类型标识（P0：让 llm.thinking_policy 管得到主对话）────────
# ★ **为什么需要它**：`_intent` 此前只在辅助调用点传（如 `routing` / `doc_summary`），
#   **主对话（ReAct 工具轮 / 收尾生成）全部没传** ⇒ `openai_compat` 里 `_who` 为空
#   ⇒ 分流表**管不到它**。而真库实测这条路径恰恰是耗时第一：
#   n=1856、思考占 completion **49.3%**、累计 **495 分钟**、最长 **4017s**
#   （id=8188 `reasoning/completion=99.2%`）⇒ **最该管的路恰好没被接管**。
#
# ⚠️ **为什么用「任务类型」而不是业务 intent**：`intent` 变量在这两条路径上表达的是
#   **业务意图**（如 `sysml_modeling` / `impact_analysis`），
#   而思考分流的判据是**任务形态**（要不要调工具、正文多长、思考占比多少）
#   —— 同一个业务意图下「探针轮」和「收尾生成轮」形态完全不同。
#   混用会把"同一意图下所有轮次"绑成一个思考策略，粒度错位。
#   ⇒ 这里定义的是**轮次角色**，与业务 intent 正交，可独立分流。
#
# 语义（为何这样分）：
#   PROBE  = 带 tools 的探针轮（决定调不调工具）—— 决策性质，思考收益低
#   REPAIR = 收尾修复轮（非流式独有：带修复工具改已知错误）
#   ANSWER = 最终生成轮（流式收尾 / 无工具直出）—— 正文长，思考收益高
CHAT_TASK_PROBE = "chat_probe"
CHAT_TASK_REPAIR = "chat_repair"
CHAT_TASK_ANSWER = "chat_answer"
# ★ 2026-10-17（米爸「harness 持续工作」）：EVAL = 自主续跑的完成度评估轮
#   （对齐 Claude Code /goal 的 evaluator：模型想停时由它决定放行还是打回）。
#   形态与 PROBE 同类（非流式、无 tools、输出短），但分流意义不同：它是**决策轮**，
#   错杀它 = 多跑一轮续跑（烧钱），故单独一档，便于将来按档位调思考策略。
CHAT_TASK_EVAL = "chat_eval"


# ── 2026-10-10 收尾原因透传（消除"是哪个上限"的歧义）──────────────────────
# 米爸提问："提示工具调用已达上限，是单个工具上限还是所有工具调用之和？"
# ⇒ 要答清楚这个问题，前提是**收尾时知道到底撞了哪道闸**。
# 本工程有四道闸（各自独立，不是同一个，也不是"调用次数之和"）：
#   · MAX_TOOLS_PER_ROUND=3  单轮内工具**并发**上限（loop_protocol）
#   · max_tool_rounds=12     ReAct **轮次**上限
#   · max_failed_rounds=3    **失败轮**上限（成功轮不计数）
#   · token 预算 60000       累计 token 上限
def _lg_stop_reason(lg):
    """取 LoopGuard 记录的真实停机原因；取不到 ⇒ ''（调用方据此走通用文案）。"""
    try:
        return str(getattr(lg, "stop_reason", "") or "")
    except Exception:
        return ""


def _lg_failed_rounds(lg) -> int:
    try:
        return int(getattr(lg, "failed_rounds", 0) or 0)
    except Exception:
        return 0


def _lg_limit_of(lg, rounds_limit: int) -> int:
    """按stop_reason 返回对应的**上限值**（用于提示文案里的具体数字）。"""
    reason = _lg_stop_reason(lg)
    if reason == "failed":
        try:
            return int(getattr(lg, "max_failed_rounds", 0) or 0)
        except Exception:
            return 0
    if reason == "token_budget":
        try:
            return int(getattr(lg, "tokens_used", 0) or 0)
        except Exception:
            return 0
    return int(rounds_limit or 0)

# 2026-09-17 S3：回填给模型的工具结果上限（远小于展示上限，防长工具结果反复占用输入 token）
# ★ 2026-10-27（截断清单 P1-B1）：3000 → 8000。
#   实测代价：超 3000 即触发 offload（结果落库 + 只回一个引用块），模型必须再调
#   `tool_result_fetch` 才能看到全文 —— conv 9639 一轮里 fetch 5 次、9640 有 4 次，
#   占该轮工具调用的 16% / 13%，**每次都是纯往返开销**（且 fetch 结果可能再撞另一道闸）。
#   8000 覆盖"结构化 MCP 信封（单段 32K 字节分页除外）+ 常规查询结果"，
#   真正超长的仍走 offload（可重读，不丢数据）。
_TOOL_MODEL_CAP = 8000
# 2026-09-17 S3：上传资料注入 prompt 的字符上限（att_text 不在 _apply_context_budget 裁剪范围内）
# ★ 2026-10-27（截断清单 P2-B5）：4000 → 8000。实测一份 8 章文档在 4000 处只到第 3 章，
#   第 5/6/7 章条款整段不在上下文（"20 条 ERROR 只核实了 3 条"同源）。
#   章节提纲（_ATT_OUTLINE_CAP）已能告知"还有哪些章"，正文上限同步放大后开篇细节也补齐。
_ATT_INJECT_CAP = 8000
# ★ 2026-11-13（米爸反馈「上传附件做需求分析，仍未完整读取」）：**章节提纲注入上限**。
#   背景（截图实证）：注入侧只取 att_blocks[:4] 再砍到 4000 字符 ⇒ 一份 8 章文档
#   模型只读到第 1~3 章和 4.1，**第 5/6/7 章条款整段不在上下文里**
#   （同一症状还表现为「20 条 ERROR 只核实了 3 条」——后 15 条同样不在上下文）。
#   ⇒ 全文解析已在 #51 修好（2500 字/块 ×≤40 块），但**注入侧这一道闸才是真凶**。
#   分层注入方案（正文仍受 _ATT_INJECT_CAP 封顶，控制 token）：
#     ① 正文首部   ≤ _ATT_INJECT_CAP 字符（原有行为，保证开篇细节不丢）
#     ② **章节提纲** ≤ _ATT_OUTLINE_CAP 字符（新增：让模型知道全文有哪些章、每章讲什么，
#        从而能"知道第 5 章存在"并据此定向索取，而不是误以为文档到 4.1 就结束了）
#     ③ 全文       进工具/召回通道（retrieval_att，供语义召回与按需读取）
_ATT_OUTLINE_CAP = 2000

# ★ 2026-11-13（#56）：`read_attachment_section` 回填给模型的**专用字符上限**。
#   通用上限 `_TOOL_MODEL_CAP`（现 8000）对「取整章正文」仍不适用：超长章节可达数万字符
#   （doc 848 第 5 章实测 6024 字符，个别条款页更远超此数）⇒ 每次取章都被 offload 成引用块 ⇒ 模型只拿到
#   一个 id（再去调 tool_result_fetch 要全文还会被 RBAC 拦）⇒ **模型看到的是空**
#   ⇒ 误判"第 5/6/7 章正文读不到"（实测原话「全部返回空内容」）。
#   本工具的语义就是"把这一章的正文交给模型"⇒ 截断会直接毁掉用途 ⇒ 给足整章上限。
#   取值 20000 = 约 1.2 万 token，覆盖超长章节（一个 20 字符的章号可能带几十页条款）。
_ATT_SECTION_MODEL_CAP = 20000

# 2026-09-17 S4（C3=按意图只注入命中 Agent）：流式路径注入「命中 Agent 角色块」的字符上限。
# 背景：SSE 路径此前**完全不消费** agents.system_prompt（硬编码一句通用角色），
#   导致库里 20 个 Agent 的定制（合计 44,762 字）在主流式链路上不生效 —— 用户配了 Agent 却看不到效果。
# ★ 2026-10-27（截断清单 P1-C3）：1200 → 8000，且 **execute / stream 两路径统一取本值**。
#   原状态：execute 传 0（不截断）、stream 传 1200 ⇒ **同一个 Agent 在两条路径上行为不同**，
#   而这恰好是"试跑看着好好的、对话里效果差"的隐藏原因之一。
#   8000 的取值依据（真库 agents.system_prompt 实测）：30 个 Agent 里 29 个 ≤ 7,981 字符
#   ⇒ 8000 = 对现有资产"实际不裁"，同时保留对异常超长 prompt 的防御。
#   注意：`_build_role_block` 的超限处置是**段级折叠**（只折可裁段「视图规范参考」，
#   约束/输出示例/职责永不腰斩），不是字符腰斩 —— 见 prompt.py 该方法文档。
_AGENT_ROLE_CAP = 8000

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
#:
#: ★ P4（2026-10-26）**让实现符合本注释声明的原则**。
#:   此前 `skill_prompt` 依赖 `user_input`（每轮可能变）却排在第 3 位，把它之后的
#:   `memory` / `model_context` / `project_memory` / `slots` / `user_ctx` 一起拖成缓存 miss
#:   —— 与本段声明的「按易变度升序」直接矛盾。
#:
#:   实测依据（生产库 conv=9571，8 个不同 query，逐块 md5）：
#:     · 静态区 8 块 **全部恒定**
#:     · 动态区**只有 `skill_prompt` 会变**（6/8 唯一值，均 1,016 tok）
#:     · `memory` 80 tok / `model_context` 226 tok / `retrieval` 11 tok 逐个恒定
#:   ⇒ `skill_prompt` 是**唯一真实缓存断点**，它前面的恒定量才是可救回的部分。
#:
#:   DeepSeek / Anthropic 的 prompt cache 都是**前缀完整匹配**：断点之后一律不计命中
#:   ⇒ 把 `skill_prompt` 押后，「memory(80) + model_context(226) ≈ 306 tok」由"每轮白扔"
#:   变为"可命中"。收益有限（system 约 6.7%）但**零语义代价**：只换顺序，不删内容。
#:
#:   ⚠️ 顺序契约由 tools/verify/verify_prompt_cache_order.py 锁定（含变异自证）：
#:     「易变块必须排在稳定块之后」。新增块时按同一原则归位，否则该门禁翻红。
PROMPT_DYNAMIC_KEYS = (
    # ── 相对稳定（同会话内基本不变）──
    "intent_line",     # L3 当前意图（依赖 intent）
    "model_code_req",  # L3 建模输出要求 + L0 硬约束卡（依赖 intent；建模/非建模差异巨大）
    "memory",          # L4 长期记忆（依赖 user_input，但 core 层无条件常驻 ⇒ 实测恒定）
    "model_context",   # L4 建模上下文（依赖 branch + conversation ⇒ 同会话稳定）
    "project_memory",  # L4 项目级记忆（依赖 user_input + conversation）
    "user_ctx",        # L4 用户上下文
    # ── 每轮可能变（必须押后：断点之后的内容一律无法命中缓存）──
    "template",        # L4 提示词实验室模板（依赖 intent + user_input）
    "slots",           # L4 任务拆解（本轮 LLM 产出）
    "skill_prompt",    # L4 技能渐进式披露（依赖 user_input ⇒ **实测唯一断点**，均 1,016 tok）
    "skill_block",     # L4 显式指定技能正文（依赖本轮 skill_name）
    "report",          # L4 报告模板（本轮）
    "attachment",      # L4 用户上传资料（本轮附件）
    "retrieval",       # L4 检索到的互联数据（本轮检索）
)

#: 拼装顺序（**唯一真源**）：可缓存静态区 → 动态区。
PROMPT_BLOCK_ORDER = PROMPT_CACHEABLE_KEYS + PROMPT_DYNAMIC_KEYS

#: 分界线标记（等价 Claude Code 的 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`）。
#: ⚠️ 仅作**代码层标记**（供 tools/verify/verify_prompt_layering.py 判定层归属），
#: **不拼进 prompt 文本**（省 token，也避免给模型引入无意义噪声）。
PROMPT_LAYER_BOUNDARY = "── SYSTEM_PROMPT_DYNAMIC_BOUNDARY ──"


def assemble_system_prompt(blocks: dict, audit: bool = True) -> str:
    """按 `PROMPT_BLOCK_ORDER` 拼装 system_prompt —— **顺序的唯一真源**。

    两条路径（非流式 `execute.py` / 流式 `stream.py`）都调用本函数，只负责收集各自的块
    内容（传 dict），不再各自维护拼接顺序（此前各写一份 → 改一处漏一处、口径漂移）。

    空块（`None` / `""`）跳过，不留多余空行。未登记在 `PROMPT_BLOCK_ORDER` 的 key 会被
    **忽略** —— 该情形（新增块忘了分层归类）由 tools/verify/verify_prompt_layering.py 抓。

    ★ P0-2（2026-10-26）**分块 token 归因**：
    本函数**本来就按块遍历** `PROMPT_BLOCK_ORDER`，块边界是已知量 ⇒ 在此记录每块 token
    **零额外切分成本**（不需要重新分词、不需要二次分块）。归因落 `context_audit` 表，
    `trace_id` 与 `llm_usage_stats.trace_id` 同源 ⇒ 可按 trace 聚合「这轮 token 花在哪」。

    动机：`llm_usage_stats` 只记整包 prompt_tokens（生产 chat_probe 均值 12,511 / 峰值
    64,552 token），**无分块归因** ⇒ 历史上两次真实损失（总闸静默空转、流式路径腰斩
    82% prompt）都因"看不见"而延迟发现。本模块是后续所有上下文调优的**前提**。

    `audit=False` 可关闭单次记录（配置 `context_audit.enabled=False` 亦全局关闭）。
    归因**异常一律吞掉** —— 观测绝不阻断主流程。
    """
    out = []
    for key in PROMPT_BLOCK_ORDER:
        val = blocks.get(key) or ""
        if val:
            out.append(val)
    if audit:
        try:
            from agent.context_audit import record_blocks
            record_blocks(blocks, phase="system")
        except Exception:
            pass
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

#: ★ 2026-10-17（米爸反馈3）：ask_user 使用指引（prompt 层；工具 description 是主契约）。
#: 为什么两层都写：实测 LLM 对"放在 description 里的软约束"遵循率不稳定，
#: 而 system prompt 里的显式规则遵循率高得多。execute 路径不注入 tool_rules 块，
#: 靠工具 description 生效（零行为变动，见 P1-28 的显式差异化决策）。
ASK_USER_RULES_TEXT = (
    "需要用户确认时：执行中若缺少关键信息（未指定工程/版本/范围等且无法合理推断）、"
    "有多个合理方案需要用户选择、或动作影响面大需要拍板 —— 调用 **ask_user** 工具"
    "（question 结合当前上下文具体化 + options 给 2-6 个基于上下文的候选），"
    "调用后流程会暂停并弹出可点选的确认卡片，用户回答后自动继续。"
    "⚠️ 不要用纯文本写『请确认：……』然后自行结束——那不会生成确认卡片；"
    "信息足够时直接执行，不要为确认而确认。\n")

#: ★ 2026-10-17（米爸「harness 持续工作」）：持续工作模式指引（prompt 层）。
#: 为什么需要：完成度评估器只能兜住「模型以为自己完成了」；如果模型**计划上**就只做
#: 一步（「我先查列表，结果如下，需要我继续吗」），评估器看到的就是一个完整的单步交付。
#: ⇒ 必须在 system prompt 层把「多步任务要一口气做完」写成显式规则——
#:   Claude Code 的同款机制：loop 由 stop_reason 驱动，模型不被鼓励中途停下来问。
AUTOPILOT_RULES_TEXT = (
    "持续工作模式：你是可长时间自主工作的 harness 智能体，接到任务后持续工作直至完成。\n"
    "· 任务未完成时**直接继续下一步**（调用工具或产出内容），"
    "禁止用『需要我继续吗 / 要我接着做吗 / 是否继续』这类问句停下来等人吩咐；\n"
    "· 每步产出后自检是否已覆盖用户请求的全部要求，未覆盖就继续，不要分批交付；\n"
    "· 仅当真正被阻塞（缺关键信息、需用户授权/拍板、遇不可解错误）时才 ask_user 或收尾说明阻塞原因。\n")

#: ★ 2026-10-17（米爸方案A1）：产物自动归档说明——**纯追加进 tool_rules**（与
#: AUTOPILOT_RULES_TEXT 同模式，所有 agent 生效）。作用：
#: 1) 告知模型「工具取回的完整模型文件平台会自动存入产物」，杜绝 9446 那种
#:    「我没有写入能力，无法保存」的自怨式收尾；
#: 2) 明确模型正文无需复述全文（平台已归档），提示用户去产物面板查看即可。
ARTIFACT_AUTO_RULES_TEXT = (
    "产物自动归档：工具执行结果中的完整 SysML/KerML 模型文件"
    "（如智源导出返回的 .sysml 全文）由平台**自动识别并存入产物库**。\n"
    "· 你**不需要**也无法自行把文件落盘——不要说『我没有写入能力/无法保存文件』，"
    "平台已经替你保存；\n"
    "· 会话流会显示『📄 取回模型文件 …已自动存入产物』环节，提醒用户到产物面板查看"
    "或下载全文即可，你的回答里只需给出分析结论，**不要**整段复述文件全文。\n"
    "· ★ 用户明确要求『存入产物/写入产物文档/保存代码』时：若内容在工具结果里且"
    "未被自动归档（如分段展示的片段），调用 **save_artifact** 工具"
    "（参数 title/content，kind 可选 code|document|sysml|report）显式登记进产物库，"
    "然后告知用户已保存、到产物面板查看。**禁止**只在文字里说『已存入产物』"
    "而不实际调用工具——那是不可能成立的。\n")

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


def _citation_footer(context_text: str) -> str:
    """★ B5-10：在检索数据**尾部**就近复述引用规范，并列出本次可用的编号。

    为什么必须放在这里（而不是只靠 blocks 第 7 位的 `citation_rules`）：
      ① 原指令与数据**相距 12 个 block**（skill_prompt / template / memory /
         model_context / slots / attachment 全夹在中间），稀释效应明显；
      ② 实测 4 条真实消息正文 `[n]` 标注 **0/4** —— 软约束"若引用请标"下，
         LLM 输出结构化建模说明时**不标没有任何代价**。
      ⇒ 让指令与数据零距离，并把编号**直接列出来供照抄**。

    编号上限与 `agent/utils.py::_citations_payload` **同源于 `rag.inject_k`**，
    这样「prompt 里有多少个【来源n】」与「界面有多少个可点条目」永远一致，
    不会再出现"后 5 条编号在 prompt 里不存在"那种错位。

    `context_text` 里没有【来源n】时返回 ""（不留多余空行）。
    """
    if not context_text or "【来源" not in context_text:
        return ""
    # 从正文里数出实际注入了几条【来源n】（与 _build_context 的注入同源）
    n = len(re.findall(r"【来源\d+】", context_text))
    if n <= 0:
        return ""
    if n == 1:
        return ("\n\n【引用提示】以上数据含【来源1】；"
                "凡用到它的内容，请在对应论述末尾标注 [1]。\n")
    return ("\n\n【引用提示】以上数据含【来源1】～【来源%d】；"
            "凡用到其中任一条，请在对应论述末尾标注对应编号（如 [1] 或 [2,3]）；"
            "未使用到的来源不要标注。\n" % n)


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
    _role_text = pipe._build_role_block(ctx.agent_def, **_role_kw)
    # ★ P0-7（2026-10-26）：role 块内「工具手册段」按需裁剪。
    #   实测（生产快照 + 真实链路）：`智源对接agent` 的 system_prompt 是
    #   **14,538 字符 / 8,995 tok，其中 14,531 字符（100%）是 58 个 MCP 工具的手册**，
    #   非工具叙述仅 7 字符；其余 16 个 Agent 手册占比全为 0%。
    #   而上轮已把 MCP 工具按需暴露到 8 个 ⇒ role 里 50 个工具的手册对模型不可用，纯噪音；
    #   MCP server 离线时更是 100% 死重（生产当前 state）。
    #   默认**关闭**（改模型可见内容，风险高于纯注入量优化，需生产验证后再开）。
    #   兜底完备：开关关 / 无手册段 / 暴露集为空 / 切分不自洽 ⇒ 逐字节原样返回。
    try:
        blocks_role_text = pipe._apply_role_manual_filter(_role_text, ctx) \
            if hasattr(pipe, "_apply_role_manual_filter") else _role_text
    except Exception:
        blocks_role_text = _role_text
    blocks = {
        # ── L2 身份层 ──
        "role": "%s\n" % blocks_role_text,
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
        # ★ B5-10：引用规范**在数据尾部就近复述**。
        #   原实现只在 blocks 第 7 位放一次 `citation_rules`，而带【来源n】的
        #   检索块排第 19 位 —— 中间隔 12 个 block（skill/template/memory/slots…），
        #   指令与数据相距太远，实测正文 [n] 标注 0/4。
        #   ⇒ 在数据末尾再放一句"就地可抄"的提示，并把**本次真实可用编号**列出来
        #     （编号上限与 `_citations_payload` 同源于 rag.inject_k，两侧不会错位）。
        "retrieval": "检索到的互联数据：\n%s%s" % (
            ctx.context_text,
            _citation_footer(ctx.context_text),
        ),
        "report": ("\n\n%s" % ctx.report_prompt) if ctx.report_prompt else "",
    }
    if ctx.include_tool_rules:
        # ★ 2026-10-17：ask_user 指引并进 tool_rules 块（stream 路径可见）。
        #   execute 路径无 tool_rules 块（P1-28 刻意保留的差异），靠工具 description。
        # ★ 2026-10-17（harness 持续工作）：AUTOPILOT_RULES_TEXT **纯追加在末尾**——
        #   旧值（TOOL_RULES + ASK_USER）保持为前缀，verify_prompt_blocks 的 B 组
        #   等价判据（除 tool_rules 外逐字节 + tool_rules 旧值纯追加）因此不被破坏。
        blocks["tool_rules"] = (TOOL_RULES_TEXT + ASK_USER_RULES_TEXT
                                + AUTOPILOT_RULES_TEXT + ARTIFACT_AUTO_RULES_TEXT)
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
    # ★ P0-3（2026-10-27）：本轮 trace_id —— 与 `llm_usage_stats.trace_id` **同源同值**，
    #   是"这一轮对话调了哪些能力、花了多少钱"的唯一 JOIN 键。
    #   ⚠️ **必须清零**：Pipeline 是全局单例，不清 ⇒ 上一轮的 trace 会被写进本轮的
    #   工具调用日志，追溯结果指向**别人的那一轮**（比留空更糟：看起来有值、但是错的）。
    pipe._tool_trace_id = ""
    pipe._skill_allowed_tools = None     # 显式技能授权（见 reset_skill_state 的历史注释）
    pipe._skill_forced = False
    pipe._hil_level = None               # HIL 分级（人工确认队列的判定依据）
    # ── 技能注入：本次绑了哪些技能、正文被 offload 到哪里 ──
    pipe._last_skill_hits = []           # 本次命中的技能（门禁与前端展示都读它）
    pipe._skill_body_offloads = []       # ★ 本次 offload 的正文 id（不清 ⇒ 收尾注入上一轮的正文）
    # ── 记忆作用域：mem0 式读写的作用域 ──
    pipe._mem_ctx = None
    pipe._mem_project_id_cache = None    # 每次执行清缓存：缓存只在本请求内有效，防跨会话串味
    # ★ 2026-10-27：补登记 `_rag_memory_hits`（P0-4 引入、当轮漏登记）。
    #   它虽在 stream.execute_stream 入口会被置 `[]`，但**登记缺失是门禁能发现的唯一信号** ——
    #   一旦有人把入口那行删掉，跨轮串味（消费到上一轮的 memory_hits）将无人拦。
    #   `verify_request_state_reset` 的 A2 扫全部 `self.X =` 赋值点与本清单的差集，正是为此。
    pipe._rag_memory_hits = []
    # ── ★ SysML 交付兜底缓存（本条即"产出雷同"的根因字段）──
    pipe._sysml_last_checked_code = None
    pipe._sysml_last_pass_code = None
    # ── 编排 / 会话治理的瞬态标记 ──
    pipe._orch_error = None
    pipe._last_clarify_skip = ""
    pipe._slot_merge_stats = {}
    pipe._last_compaction = None
    pipe._hook_warn = ""
    # ★ 2026-11-13（#55）：本轮上传附件的**全文与章节索引**，供
    #   `read_attachment_section` 工具按需取回缺失章节正文。
    #   ⚠️ **必须清零**（安全红线）：AgentPipeline 是全局单例，不清就会让
    #   本轮用户读到**上一轮别的用户上传的附件内容**（跨用户数据泄漏）。
    pipe._att_sections = []
    pipe._att_full_text = ""
    # ★ 2026-11-13（#57）：已取回章节记忆（{章号: 正文}）——请求级瞬态。
    #   不清 ⇒ 下一轮会拿到**上一轮别的用户**的章节正文 = 跨用户数据泄漏（安全红线）。
    pipe._att_fetched = {}
    # ★ 2026-10-27（会话 9629）：工具结果分页读取的累积状态（键=文件名/taskId）。
    #   run()/execute() 入口都会重新置 {}，但**必须在此登记**——单例复用下若无登记，
    #   一旦入口那行被删，上一轮的分片会与本轮同名文件拼在一起（跨轮串味、拼出错文件）。
    #   与 `_tool_sysml_docs` 同生命周期：persist 后清零。
    pipe._tool_doc_pages = {}
    # ★ 2026-10-27：异步任务续跑的「系统现场」陈述（注入 system prompt 用）。
    #   不清 ⇒ 上一轮的"任务仍在跑"会被写进本轮，模型据此拒干活（跨轮串味）。
    pipe._async_notes = []
    # ★ 2026-10-27：本轮清理掉的泄漏工具标记处数（协议自愈留痕，供观测统计）。
    pipe._leaked_tool_markup = 0


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
    'exec_file_tool', '_FILE_TOOL_NAMES', '_WEB_TOOL_NAMES', 'exec_report_tool', '_REPORT_TOOL_NAMES',
    'AgentDefinition', 'AgentRegistry', 'IntentRouter', 'GraphRAG', 'ConflictDetector',
    '_citations_payload', '_extract_code_blocks', '_archive_impact_analysis',
    '_archive_sysml_version', '_archive_artifacts', '_TOOL_RESULT_CAP',
    '_TOOL_MODEL_CAP', '_ATT_INJECT_CAP', '_ATT_OUTLINE_CAP', '_ATT_SECTION_MODEL_CAP', '_AGENT_ROLE_CAP',
    '_STREAM_CHARS_PER_SEC', '_STREAM_MAX_CPS', '_STREAM_CHUNK', '_STREAM_MAX_DRAIN_SEC',
    'iter_stream_chunks',
    # P1-26：system_prompt 分层拼装（顺序单一真源，两条路径共用）
    'PROMPT_CACHEABLE_KEYS', 'PROMPT_DYNAMIC_KEYS', 'PROMPT_BLOCK_ORDER',
    'PROMPT_LAYER_BOUNDARY', 'assemble_system_prompt',
    # P1-28：system_prompt 块内容单一真源（两条路径共用；差异显式参数化）
    'TOOL_RULES_TEXT', 'ASK_USER_RULES_TEXT', 'AUTOPILOT_RULES_TEXT',
    'ARTIFACT_AUTO_RULES_TEXT', '_SKILL_CONTENT_CAP', 'PromptBlocksCtx',
    'build_slots_block', 'build_skill_block', 'build_prompt_blocks',
    # P1-29：显式指定技能的注入（两路径统一入口 + 每轮状态起点）
    'reset_skill_state', 'load_forced_skill',
    # 请求级瞬态状态清零点（execute / stream 两路径统一调用，防单例跨请求泄漏）
    'reset_request_state',
    # P1-4：会话槽位治理（纯函数，便于单测与变异自证）
    'SLOT_CLARIFY_MARK', 'SLOT_ORIGINAL_MARK', 'TOPIC_CARRY_WORDS',
    'extract_original_request', 'norm_slot_item', 'merge_slot_items', 'is_topic_switch',
    # 2026-10-10 修复：轮次角色常量与失败轮预算辅助函数（2026-10-10 批次新增，
    #   ⚠️ 此前漏加进 __all__ ⇒ `from .common import *` 拿不到 ⇒ execute.py/stream.py
    #   运行时 NameError: CHAT_TASK_PROBE is not defined ⇒ **全部对话一轮即挂**
    #   （实测 2026-10-10 22:53 起所有会话失败，见会话 9336 msg 3895/3896）。
    #   教训：新增 common.py 顶层名字必须同步 __all__；常驻守卫 tools/verify/verify_common_exports.py
    # 2026-10-18 续修：CHAT_TASK_EVAL 又漏了一次（autopilot.py 显式 import 兜着没炸，
    #   但守卫 [1] import * 语义判红）—— 同族漏网第二次，正说明守卫不能只靠"记得"。
    'CHAT_TASK_PROBE', 'CHAT_TASK_REPAIR', 'CHAT_TASK_ANSWER', 'CHAT_TASK_EVAL',
    '_lg_failed_rounds', '_lg_limit_of', '_lg_stop_reason',
]
