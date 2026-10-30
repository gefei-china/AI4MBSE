# -*- coding: utf-8 -*-
"""意图理解摘要：把「路由判定」翻译成用户能读懂的一句话。

## 为什么单独成模块

用户反馈（2026-10-27）：

> 「意图识别环节总是匹配到具体的 agent，不能提供更好理解的内容吗？比如对于用户意图的
>   分析与总结 —— 用户到底想要什么，不清楚的可以通过澄清卡确认」

现场是：界面上「意图识别」这一行只显示 `→ 智源对接agent（HIL 2）`。这是**内部实现名**，
用户关心的是"你到底理解成什么了、我要的东西对不对"，而不是我们把它路由给了哪个 Agent。

## 设计纪律（三条）

1. **不新增 LLM 调用**：目标是"把已经算出来的东西说人话"。
   - `goal` 优先取 `task_decompose` 已产出的 `slots.goal`（建模类意图本来就有）；
   - 没有 slots 时**忠实复述用户原话的关键句**（剥 `@提及` / `#标签` 后的首句），
     不做同义改写 —— 改写就有幻觉风险，而"复述"永远是安全的、且永远不会比原文更错。
   - 这条纪律是有代价的（复述不如概括精炼），但换来的是：**每一轮对话都不多一次
     LLM 往返**。如果哪天证明它值得，再加可选的精炼开关，而不是先付出延迟。

2. **不给"把握"造假**：`basis` 只写**系统真的用过的判据**（路由来源 / 置信度 / 命中实体），
   `route` 字符串本身也如实透出，未知取值原样显示，不做美化。

3. **"待确认"只报硬缺失，不猜测**：默认判据只有一条**通用**规则 ——
   **过短且无承接的输入**（≤ `short_input_chars` 且没有上文可承接），此时才说
   "具体对象/要求没讲清"。**不做**"某类意图就必须带某个槽位"这类平台特判：
   那会把正常请求误判成缺信息（本仓已被"硬套阈值"坑过多次）。

## 与澄清卡的关系

本模块**不自己弹卡**（弹卡是"打断用户"，必须有节制、且只能由既有闸门决定）。
它只把 `missing` 作为**提示**交出去：

- `stream.py` 把它放进 `意图识别` 的 stage 事件 → 用户随时能看到"我是这么理解的"；
- 既有澄清卡（`clarify` / `clarify_ask`）触发时，把 `brief` 一并带上 →
  卡片顶部能展示"我的理解"，用户对照着改，而不是对着一串意图名猜。
"""

from __future__ import annotations

import re

# 剥掉「@某人」「#标签」「/技能」这类控制前缀 —— 它们是给系统的指令，不是用户想表达的内容
_CTRL_RE = re.compile(r"(?<![\w])[@#/][\w\u4e00-\u9fa5\-:.]+")
_WS_RE = re.compile(r"\s+")
# 句子边界：中英文句末标点 + 换行
_SENT_END = re.compile(r"[。！？!?\n；;]")

GOAL_MAX = 48          # 一句话上限（超出按语义边界截断）
DEFAULT_SHORT = 8      # 判定"输入过短"的默认阈值（字符数）

# 路由来源 → 人话。键**必须**覆盖 `agent/intent.py` 里真实出现过的全部取值
# （门禁 `verify_intent_brief.py` 会静态提取 `_done/_rule_done` 的第二参数与
#  `route = "..."` 赋值，逐个比对本表 —— 漏一个就会在界面上显示成裸英文 key，
#  实测漏过 `llm_first_weak`：用户看到「llm_first_weak」这种字符串等于没解释）。
ROUTE_TEXT = {
    # 规则层
    "rule": "命中意图规则（关键词/术语）",
    "rule_scored": "命中意图规则（多个关键词竞争打分后取最高）",
    "explain": "按「解释这类问题」的固定规则",
    # 语义 / 融合
    "semantic": "语义相似（与某个意图的示例说法接近）",
    "vector": "向量相似",
    "fused": "关键词与语义两路互证",
    "fused_conflict": "关键词与语义两路不一致",
    # 模型判定
    "llm": "模型判断",
    "llm_first": "模型判断（意图由模型主判）",
    "llm_first_weak": "模型判断，但把握不大",
    "llm_weak": "模型判断，但把握不大",
    "llm_conflict": "模型判断与语义结果不一致",
    "llm_history": "结合上文由模型判断",
    # 上下文
    "inherit": "承接上一轮的话题",
    "cache": "命中相同问题的历史判定",
    # 规则 + 模型复核（B4-5）
    "rule_llm_confirmed": "意图规则命中，且经模型复核确认",
    "rule_llm_vetoed": "意图规则命中，但被模型复核否决",
    "rule_llm_skipped": "意图规则命中（未做模型复核）",
    # 无信号
    "none": "没有任何明确信号",
    "chat": "没有识别出明确任务（按闲聊处理）",
}


def _clean(text: str) -> str:
    t = _CTRL_RE.sub(" ", str(text or ""))
    return _WS_RE.sub(" ", t).strip()


# ── 元对话壳识别（2026-10-27 实测踩到）───────────────────────────────────────
# 澄清续答的文本是**系统构造**的，形如：
#   「【澄清补充】用户在待澄清状态下直接补充了以下信息（请据此继续，无需再确认）：
#     问题「…」→ 回答：…」
# 它描述的是"用户答复了澄清卡"这件**系统事件**，不是用户想要什么。
# 实测（conv 9685）：goal 直接取到这句话 ⇒ 界面上显示
#   「理解：【澄清补充】用户在待澄清状态下直接补充了以下信息…」= 等于没解释，还误导。
#
# 判据只认**结构性特征**（方括号系统标记 / 元对话主语），不认某一句具体文案 ——
# 壳的措辞会随版本变，而"谁在说话"不会变。
# 命中时**宁可不显示 goal**（前端回退到归类行），也不显示一个错误的理解。
_META_SHELL_RE = re.compile(r"^\s*(?:【[^】]{1,24}】|用户[已在]|请据此|以下信息|问题[「\"])")


def _looks_meta_shell(text: str) -> bool:
    return bool(_META_SHELL_RE.match(str(text or "")))


def _strip_meta_shell(text: str) -> str:
    """尽力剥掉元对话壳；剥不掉就返回空串（调用方据此决定不显示 goal）。

    两级：① 复用既有的 `extract_original_request`（取「原请求：」之后）；
         ② 若仍以壳特征开头 ⇒ 认为没有可用的用户本意。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    try:
        from agent.pipeline_parts.common import extract_original_request
        t = str(extract_original_request(t) or "").strip()
    except Exception:
        pass
    if not t or _looks_meta_shell(t):
        return ""
    return t


def _first_sentence(text: str) -> str:
    m = _SENT_END.search(text)
    return text[: m.start()] if m and m.start() > 0 else text


def _clip(text: str, limit: int = GOAL_MAX) -> str:
    """按语义边界截断，不硬切词。

    先找 limit 内最后一个可断处（标点/空格），退得太厉害（< 60% limit）就硬切，
    避免出现"…"比内容还长的怪结果。
    """
    if len(text) <= limit:
        return text
    cut = -1
    for ch in ("，", ",", "、", "；", ";", "。", " ", "："):
        i = text.rfind(ch, 0, limit)
        if i > cut:
            cut = i
    if cut >= int(limit * 0.6):
        return text[:cut].rstrip("，,、；;：") + "…"
    return text[: limit - 1] + "…"


def summarize_input(user_input: str, limit: int = GOAL_MAX) -> str:
    """忠实复述用户原话的关键句（不做同义改写，因此不会引入幻觉）。"""
    t = _clean(user_input)
    if not t:
        return ""
    return _clip(_first_sentence(t), limit)


def route_text(route: str) -> str:
    r = str(route or "").strip()
    if not r:
        return ""
    return ROUTE_TEXT.get(r, r)      # 未知取值原样显示，不猜


def build(user_input: str, *, intent: str, agent_name: str = "",
          slots: dict | None = None, meta: dict | None = None,
          label: str = "", elapsed_hint: str = "") -> dict:
    """组装意图理解摘要。

    返回 dict（全部字段都保证是"可直接展示"的字符串/列表，前端不必再判空）：

        goal     用户想要什么（一句话）
        need     系统把它归到哪一类（人类可读的意图名，不是内部 key）
        agent    执行主体（内部名 —— 只作次要信息，不再当成"理解结果"展示）
        basis    判据（系统真的用过的，按可读性排序）
        missing  待确认点（空列表 = 没发现明显缺口）
        route    原始路由来源（可排查，不做展示主文案）
        source   goal 的来源：slots / input / none
    """
    meta = meta or {}
    slots = slots or {}

    # ── goal ────────────────────────────────────────────────────────────
    # 取值顺序（每一级都必须**先通过壳检测**，否则降级到下一级；全不过则留空）：
    #   ① slots.goal（建模类意图由 task_decompose 产出，最精炼）
    #   ② 用户原话首句（忠实复述，永远为真）
    #   ③ 空 —— 前端回退成「归类 + 判据」，宁可不显示也不显示错误的理解
    goal, source = "", "none"
    g = _strip_meta_shell(slots.get("goal") or "") if slots.get("goal") else ""
    if g:
        goal, source = _clip(g), "slots"
    if not goal:
        raw = _strip_meta_shell(user_input)
        if raw:
            goal = _clip(_first_sentence(_clean(raw)))
            source = "input" if goal else "none"
    # 本轮是不是"在回答澄清卡"（决定前端要不要说明"这轮没有重新分析目标"）
    is_followup = (not goal) and _looks_meta_shell(user_input)

    # ── need（人类可读的归类，而不是内部 key）─────────────────────────────
    need = str(label or "").strip() or str(intent or "").strip()

    # ── basis（只写真的用过的判据）───────────────────────────────────────
    basis = []
    rt = route_text(meta.get("route"))
    if rt:
        basis.append(rt)
    try:
        conf = float(meta.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 0:
        basis.append(f"把握 {int(round(conf * 100))}%")
    ents = slots.get("entities") or []
    if isinstance(ents, (list, tuple)) and ents:
        shown = "、".join(str(x) for x in ents[:4])
        more = f" 等 {len(ents)} 项" if len(ents) > 4 else ""
        basis.append(f"识别到对象：{shown}{more}")
    cons = slots.get("constraints") or []
    if isinstance(cons, (list, tuple)) and cons:
        basis.append(f"附加约束 {len(cons)} 条")
    if meta.get("used_history"):
        basis.append("参考了上文")
    if elapsed_hint:
        basis.append(elapsed_hint)

    # ── missing（只有通用判据，不做意图特判）───────────────────────────────
    missing = []
    try:
        from core import config as _cfg
        short_n = int(_cfg.get("intent", "brief_short_chars", DEFAULT_SHORT) or DEFAULT_SHORT)
        enabled = bool(_cfg.get("intent", "brief_missing_check", True))
    except Exception:
        short_n, enabled = DEFAULT_SHORT, True
    if enabled:
        raw = _clean(user_input)
        # 承接轮（"继续"/"再来一次"/澄清补充）本来就没必要自己讲清目标 —— 不报缺失
        inherited = bool(meta.get("used_history")) or str(meta.get("route") or "") == "inherit"
        if raw and len(raw) <= short_n and not inherited and not slots:
            missing.append("你的具体要求还比较简短，我按最直接的理解执行；如果我理解偏了，直接说一句更具体的就行")

    return {
        "goal": goal,
        "need": need,
        "agent": str(agent_name or ""),
        "basis": basis,
        "missing": missing,
        "route": str(meta.get("route") or ""),
        "source": source,
        # 澄清续答轮：目标来自"用户的答复"而非新请求 ⇒ 不重复分析，也不假装理解了什么
        "followup": is_followup,
    }


def one_line(brief: dict) -> str:
    """折叠态用的一行文案：`理解：<goal>`（供 stage 的 sub 槽位使用）。"""
    b = brief or {}
    g = str(b.get("goal") or "").strip()
    return f"理解：{g}" if g else ""
