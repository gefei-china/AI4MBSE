# -*- coding: utf-8 -*-
"""团队定义加载 + 团队模式路由决策（C1/C4/C5，2026-10-07）。

背景（实测依据，非推测）
------------------------
改team 前：`_resolve_team()` 只返回主 Agent 的**意图名**，编排时主 Agent 手上一��
`system_prompt` / `capabilities` / `intent_keywords` 全部丢失；而 `team_forced`
**无条件**绕过 `_needs_orchestration`，于是问「你好」也会跑一次完整编排。
更糟的是planner 的「可用 Agent 池」只拿到意图名（`_orch_pool_desc_lines` 的
`- intent` 兜底分支），14 个成员里8 个是「XX视图生成」，近义词无法区分。

真实语料实测（36 条唯一 query，见 docs/主Agent提示词反推草案-团队定义驱动编排-20261007.md）：
    指标                2-gram 词法    LLM 语义路由
    单成员定向命中       14/16=88%     13/16=81%
    越界请求拦截          4/5 =80%      5/5 =100%
    多阶段识别为编排      0/2 = 0%      2/2 =100%
⇒ 结论：**词法与 LLM 互补，不是替代**。词法强命中零开销且准确，LLM 负责词法
  分不开的近义成员（结构视图 vs 需求视图）与「是否多交付物」。

因此路由采用四档（`decide`）：
    ① chat   —— 短噪声/问候/纯寒暄（长度 < 6 且无成员关键词）→ 走通用直答，不消耗编排
    ② direct —— 词法强命中（score ≥ 2.0 且领先次优 ≥ 60%）→ 定向单成员直行
    ③ llm    —— 其余交给 LLM 语义裁决（成员 / __ORCH__ / 不支持）
    ④ reject —— LLM 判不支持 **或** LLM 失败回落词法后仍无命中 → 明确提示，
                但**必须带上团队能力清单 + 「去掉团队选择」出路**（不能是死胡同）
缺陷修复（实测暴露）：
    · 「你好」曾被判成"团队不支持" ⇒ 档①先把噪声摘出去；
    · LLM 输出带前后缀导致 JSON 解析失败 2/36 ⇒ `_parse_route` 容错 + 回落词法。
"""
from __future__ import annotations

import json
import re

# 词法强命中阈值：单个 intent_keywords 命中即 3.0，故 ≥2.0 意味着「至少命中一个触发词
# 或多个描述 2-gram 重合」。该值由 tools/verify/verify_team_defined_orchestration.py
# 的基线断言守护，不是拍脑袋填的。
LEX_STRONG = 2.0
# top1 相对 top2 的最小领先幅度，低于此值视为「分不清」→ 交LLM 裁决。
LEX_MARGIN = 0.6
# 短于此长度且无成员关键词 → 噪声/问候（档①）。与 `_needs_orchestration` 的 len<6 口径一致。
NOISE_LEN = 6

_LLM_ROUTE_PROMPT = """你是智能体团队的能力路由器。给定团队成员定义，判断用户请求应由谁处理。

规则：
1) 若请求明显超出所有成员能力（如写诗、测算项目成本、查工程包结构树/文件列表），
   输出 {"agent":null,"reason":"团队暂不支持该能力"}；
2) 若只有一个成员能胜任，输出该成员 name；
3) 若需要多个成员协作或存在多个交付物，输出 {"agent":"__ORCH__","reason":"..."}；
4) agent 字段必须**逐字取自**成员 name 列表，不得臆造。

只输出 JSON，不要任何其他文字。

团队成员：
{roster}"""


# ── 词法工具（纯函数，便于单测与变异自证） ────────────────────────────
def _zh_grams(s: str) -> set:
    """中文 2-gram + 英文单词（与 registry.discover 的词法口径近似）。"""
    out = set()
    for w in re.findall(r"[a-zA-Z]{2,}", (s or "").lower()):
        out.add(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", s or ""):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def lexical_score(member: dict, query: str, q_tokens: set | None = None) -> tuple:
    """成员-查询词法匹配分。

    权重口径（按**特异性**加权，而非"每个关键词同价"）：
      · intent_keywords 命中：权重 = 1.0 + 0.5 × min(len(kw), 8)
        —— 实测缺陷：同价权重下「生成需求视图」里 2字通用词「需求」（需求分析Agent 也有）
        与 4 字专有词「需求视图」同价，top1 相对 top2 领先不足 60% ⇒ 被 margin 判成
        "分不清"而白扔一次 LLM 调用。按长度加权后专有词权重约为通用词的 2 倍。
      · display_name + capabilities + description 的 2-gram 重合：每个 0.4
        （上限 8 个 = 3.2，避免长 description 靠字数堆分）。
    返回 (score, hit_keywords)。
    """
    qt = q_tokens if q_tokens is not None else _zh_grams(query)
    score = 0.0
    hits = []
    for k in (member.get("intent_keywords") or []):
        k = str(k or "")
        if k and k in query:
            score += 1.0 + 0.5 * min(len(k), 8)
            hits.append(k)
    meta = " ".join([member.get("display_name") or "", member.get("description") or "",
                     " ".join(member.get("capabilities") or [])])
    score += min(len(qt & _zh_grams(meta)), 8) * 0.4
    return score, hits


def is_noise(query: str, members: list) -> bool:
    """档①判定：短噪声/问候（且不含任何成员触发词）。

    ⚠️ 必须带「不含触发词」这一条件：短 query 也可能是有效指令（如「生成需求视图」7字，
    或更短的「需求视图」）。只按长度判会把这些误杀成chat。
    """
    t = (query or "").strip()
    if not t:
        return True
    if len(t) >= NOISE_LEN:
        return False
    for m in members or []:
        for k in (m.get("intent_keywords") or []):
            if str(k or "") and str(k) in t:
                return False
        # capabilities 里出现完整业务词也算有信号
        for c in (m.get("capabilities") or []):
            if len(str(c)) >= 4 and str(c) in t:
                return False
    return True


# 多交付物/多阶段表达（档⓪）。口径对齐 `_needs_orchestration` 的规则信号，
# 但**只取其中明确表示"多个交付物"的部分**：顿号/并列连词清单一律算多阶段，
# 「并」单独出现不算（"需求并方案"过短且歧义大，交给 LLM 判）。
_MULTI_DELIVERY_RES = (
    re.compile(r"先.{0,8}(再|然后|接着)"),
    re.compile(r"(然后|接着|最后).{0,4}(做|生成|输出|给出|汇总)"),
    re.compile(r"(分别|同时).{0,6}(并|且|进行)"),
    re.compile(r"(需求|方案).{0,10}(设计|架构).{0,10}(报告|评审)"),
    # ⚠️ (设计|分析)与(报告|评审)之间**必须有非「报告/评审」的字**才算两个动作。
    #   反例（实测）：「请生成一份关于宽带通信系统的**分析报告**」—— 「分析报告」是
    #   一个**词组**（单一交付物）；允许紧邻匹配会把它误升级为编排。
    #   正例：「先做需求分析，再出报告」—— 中间隔「，再出」，仍能命中。
    re.compile(r"(设计|分析)[^报评]{1,10}(报告|评审)"),
    # 阶段连接词 + 交付动作：「…并输出影响报告」「…然后进行校验」。
    # ⚠️ 两类必须区分，否则误伤单一交付物（实测）：
    #   反例1「请生成一份关于宽带通信系统的分析报告」——「并」并不出现，是**报告本身**
    #         一个交付物，若把「生成…报告」当多交付物就会把单交付物误升级为编排；
    #   反例2「在既有需求模型基础上，补充涉众与参与者信息的追溯关系定义」——单交付物，
    #         不得因句中「并」而升级。
    # 所以只认「阶段连接词 + 另一个**不同类型**的交付动作」：
    #   生成/输出 → 报告类 ✔；连接词 → 校验/评审类 ✔（两个不同交付物）
    #   「生成报告」单连用 ✘（一个交付物）
    re.compile(r"(然后|接着|之后|随后|再|并|同时).{0,20}?(进行)?(校验|评审|检查|验证)"),
    re.compile(r"(校验|评审|检查|验证).{0,20}?(然后|接着|之后|随后|再|并|同时)"),
    re.compile(r"(输出|导出|附上|给出).{0,6}(报告|文档|汇报)"),
    re.compile(r"(生成|产出).{0,10}(报告|文档).{0,12}(并|然后|接着|再).{0,10}(校验|评审|检查|验证|输出)"),
)


def _multi_delivery(query: str) -> bool:
    """是否含多交付物/多阶段表达。纯函数，可单测。

    ⚠️ 边界：短噪声已在档①被摘出，此处只看结构信号。
    """
    t = (query or "").strip()
    if not t:
        return False
    return any(r.search(t) for r in _MULTI_DELIVERY_RES)


# ── 团队定义加载（C1） ────────────────────────────────────────────────
def load_team_definition(conn, team: str) -> dict | None:
    """加载完整团队定义（不只 name）。非法/未启用返回 None。

    返回 {"intent","display_name","agent_role","system_prompt","capabilities",
          "members":[{name, display_name, description, capabilities, intent_keywords,
                      hil_level}]}
    —— 与旧 `_resolve_team()` 只回 name 的区别：编排 prompt 现在拿得到主 Agent 的
    system_prompt（拆解规则）与每个成员的capabilities（近义成员区分依据）。
    """
    if not team or not str(team).strip():
        return None
    key = str(team).strip()
    try:
        row = conn.execute(
            "SELECT id, name, display_name, agent_role, status, system_prompt, capabilities "
            "FROM agents WHERE name=? OR display_name=?", (key, key)).fetchone()
        if not row:
            return None
        if row["agent_role"] != "main" or row["status"] != "active":
            return None

        def _lj(v):
            try:
                return json.loads(v or "[]")
            except Exception:
                return []

        members = []
        for m in conn.execute(
            """SELECT a.name, a.display_name, a.description, a.capabilities,
                      a.intent_keywords, a.hil_level, a.status
               FROM agent_team_members tm JOIN agents a ON a.id = tm.sub_agent_id
               WHERE tm.main_agent_id=? AND tm.enabled=1 ORDER BY tm.id""", (row["id"],)
        ).fetchall():
            if m["status"] != "active":
                continue
            members.append({
                "name": m["name"], "display_name": m["display_name"],
                "description": m["description"] or "",
                "capabilities": _lj(m["capabilities"]),
                "intent_keywords": _lj(m["intent_keywords"]),
                "hil_level": m["hil_level"] or "L0",
            })
        return {"intent": row["name"], "display_name": row["display_name"],
                "agent_role": row["agent_role"], "system_prompt": row["system_prompt"] or "",
                "capabilities": _lj(row["capabilities"]), "members": members}
    except Exception:
        return None


def roster_block(team_def: dict, max_desc: int = 60) -> str:
    """团队名册文本（注入 planner prompt / LLM 路由 prompt）。"""
    lines = []
    for m in (team_def or {}).get("members") or []:
        caps = "/".join([str(c) for c in (m.get("capabilities") or [])[:4]])
        kws = "/".join([str(k) for k in (m.get("intent_keywords") or [])[:5]])
        bits = [f"- {m['name']}（{m.get('display_name') or m['name']}，{m.get('hil_level') or 'L0'}）"]
        if m.get("description"):
            bits.append(m["description"][:max_desc])
        if caps:
            bits.append("专长：" + caps)
        if kws:
            bits.append("触发词：" + kws)
        lines.append(" ".join(bits))
    return "\n".join(lines)


def capability_summary(team_def: dict) -> str:
    """团队能力清单（用于「不支持」时告诉用户团队能做什么，避免死胡同）。"""
    return "；".join([m.get("display_name") or m["name"]
                     for m in (team_def or {}).get("members") or []])


# ── LLM 语义裁决（档③） ───────────────────────────────────────────────
def _parse_route(raw: str) -> dict:
    """容错解析 LLM 路由结果。

    实测缺陷：LLM 偶发输出带前后缀/代码围栏（2/36），严格 json.loads 直接抛
    ⇒ 这里剥离围栏与首个 `{`..`}` 区间；解析失败返回 {}（由调用方回落词法）。
    """
    if not raw:
        return {}
    txt = str(raw).strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
    try:
        d = json.loads(txt)
        return d if isinstance(d, dict) else {}
    except Exception:
        pass
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(txt[i:j + 1])
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def llm_route(query: str, team_def: dict, provider_id=None, llm_client=None) -> dict:
    """LLM 语义裁决。返回 {agent, reason, ok}；ok=False 表示 LLM 不可用/解析失败。

    绝不抛异常：任何失败都降级为 ok=False，由调用方回落词法（档④）。
    """
    try:
        if llm_client is None:
            from llm import llm_client as _c
            llm_client = _c
        prompt = _LLM_ROUTE_PROMPT.replace("{roster}", roster_block(team_def))
        resp = llm_client.chat(
            [{"role": "user", "content": f"{prompt}\n\n用户请求：{(query or '')[:600]}"}],
            provider_id=provider_id, _intent="routing")
        raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
        d = _parse_route(raw)
        if not d or ("agent" not in d and d.get("agent") is None):
            return {"agent": None, "reason": "", "ok": False}
        return {"agent": d.get("agent"), "reason": str(d.get("reason") or ""), "ok": True}
    except Exception:
        return {"agent": None, "reason": "", "ok": False}


# ── 路由决策主入口（C3/C4/C5） ────────────────────────────────────────
def decide(user_input: str, team_def: dict, provider_id=None, llm_client=None,
           detected_intent: str = "") -> dict:
    """团队模式路由决策。返回 dict：

        {"action": "chat"|"direct"|"orchestrate"|"reject",
         "target": <成员 name 或 None>,
         "reason": str, "source": "noise"|"lexical"|"llm"|"lexical_fallback",
         "score": float}

    action 语义（供stream.py / execute.py 分支）：
        chat        → 走通用直答（chat意图），不编排、不定向团队成员
        direct      → 定向 team_def.members[target] 单成员直行（仍不编排）
        orchestrate → 团队编排（走 _try_orchestrate_team / _stream_orchestrated_flow）
        reject      → 明确提示团队不支持（调用方须带能力清单 + 出路）

    detected_intent（C4）：全局意图识别的结果。命中团队成员时直接采纳——
    否则团队模式会把「明明识别成 knowledge_qa」也强行编排，识别结果形同丢弃。
    """
    members = (team_def or {}).get("members") or []
    q = (user_input or "").strip()
    if not members:
        # 团队无成员：无从路由 → 直行（由主Agent 自己处理），不编排、不拒。
        return {"action": "direct", "target": None, "reason": "团队无成员，主 Agent 直行",
                "source": "lexical", "score": 0.0}

    # 档① 噪声/问候
    if is_noise(q, members):
        return {"action": "chat", "target": None, "reason": "短噪声/问候，走通用直答",
                "source": "noise", "score": 0.0}

    # C4：全局意图识别命中团队成员 → 直接采纳（不花 LLM）
    names = {m["name"]: m for m in members}
    if detected_intent and detected_intent in names:
        return {"action": "direct", "target": detected_intent,
                "reason": f"意图识别命中团队成员「{detected_intent}」",
                "source": "lexical", "score": 9.0}

    # ── 档⓪：多交付物信号 → 直接编排（先于词法强命中） ──
    # 实测缺陷：「对巡飞弹做参数变更影响分析……输出影响报告」被词法强命中 impact 抢先
    # 判为单成员直行，而它明显是 impact + report 两个交付物 ⇒ 违反草案拆解规则 1。
    # 与 `_needs_orchestration` 的多步/并列连词口径一致；命中即编排，不受词法抢占。
    if _multi_delivery(q):
        return {"action": "orchestrate", "target": None,
                "reason": "检测到多交付物/多阶段表达（先判断是否需要多任务）",
                "source": "lexical", "score": 0.0}

    # 档② 词法强命中
    qt = _zh_grams(q)
    scored = sorted(((lexical_score(m, q, qt)[0], m["name"]) for m in members),
                    key=lambda x: -x[0])
    top = scored[0][0] if scored else 0.0
    second = scored[1][0] if len(scored) > 1 else 0.0
    if top >= LEX_STRONG and (second <= 0 or (top - second) / max(top, 1e-6) >= LEX_MARGIN):
        return {"action": "direct", "target": scored[0][1],
                "reason": f"词法强命中「{scored[0][1]}」（score={top:.1f}）",
                "source": "lexical", "score": top}

    # 档③ LLM 语义裁决
    r = llm_route(q, team_def, provider_id=provider_id, llm_client=llm_client)
    if r.get("ok"):
        agent = r.get("agent")
        if agent == "__ORCH__":
            return {"action": "orchestrate", "target": None,
                    "reason": r.get("reason") or "多交付物/多成员协作", "source": "llm", "score": top}
        if agent and agent in names:
            return {"action": "direct", "target": agent,
                    "reason": r.get("reason") or f"LLM 判定由「{agent}」处理",
                    "source": "llm", "score": top}
        return {"action": "reject", "target": None,
                "reason": r.get("reason") or "超出团队能力域", "source": "llm", "score": top}

    # 档④ LLM 不可用 → 回落词法：有命中就定向最相近成员，否则明确不支持
    if top >= LEX_STRONG * 0.5 and scored[0][0] > 0:
        return {"action": "direct", "target": scored[0][1],
                "reason": f"LLM 不可用，回落词法最相近成员「{scored[0][1]}」（score={top:.1f}）",
                "source": "lexical_fallback", "score": top}
    return {"action": "reject", "target": None,
            "reason": "LLM 不可用且词法无命中，无法判定归属", "source": "lexical_fallback",
            "score": top}


def unsupported_message(team_def: dict, reason: str) -> str:
    """「不支持」提示文案：必须带能力清单 + 出路（不能是死胡同）。"""
    return (
        f"当前团队暂不支持该请求（{reason or '超出团队能力域'}）。\n"
        f"本团队可处理：{capability_summary(team_def)}\n"
        "建议：① 换成上述能力范围内的任务；② 去掉「智能体团队」选择，由通用入口直接提问。"
    )