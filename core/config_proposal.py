# -*- coding: utf-8 -*-
"""P1-3 配置提案器：RefineGate 从「内容评审」泛化为「配置评审」。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P1-3）
═══════════════════════════════════════════════════════════════
GEPA（2025-07）/ AgentEvolver 的 Self-Attributing 之后是**反思改写**：读失败
轨迹 → 提议 prompt/skill/配置修订。本系统的 RefineGate（workflows/refine.py）
只评审「内容」（汇总报告质量分 + 修订闭环）；失败轨迹一侧，P1-2 归因器已把
会话打成步骤序列并聚出**首坏步模式**（collect_patterns），但没有任何消费者
把模式变成「该改什么」的建议——诊断环到行动环之间断了一环。

本模块补上这一环，且守住一条铁律：**只提议，不自动应用**。
自动改生产配置是 misevolution（进化成错）的直接通道——提议必须经人裁决
（applied/rejected 留痕），应用由人在设置页改配置（现有配置 API），本模块
与 repo 层没有任何写配置的路径（门禁静态断言）。

═══════════════════════════════════════════════════════════════
设计纪律（与 core/badcase、core/lifecycle、P1-1/P1-2 同构）
═══════════════════════════════════════════════════════════════
1. 提议规则注册表：新规则 = 登记一个 ProposalRuleSpec（name/label/desc +
   match 纯函数）。match(pattern, ctx) → 0..n 个草案；规则内不做 I/O
   （当前配置值由 ctx.config 快照带入——拍脑袋写死 proposed_value 等于
   没读现场）。
2. 五条初始规则按「证据强度」分级：
   · llm_quota_fallback   配额/余额/无密钥类失败 → 提议配置 llm.fallback_provider_id
   · llm_transient_retry  超时/连接类失败 → 提议上调 llm.retry_times（现值+1）
   · prompt_skill_revision 复发失败模式（更高下限）→ 提议修订对应 (意图,执行体)
                           的 prompt/skill——GEPA 式反思改写的本体
   · tool_kind_gap        工具失败无类型（kind 空）→ 提议补埋点/扩分类词表
   · unattributed_gap     结局坏但零坏步 → 提议补埋点（失败发生在埋点之外）
3. 指纹去重：fingerprint = sha1(rule|section|key|proposed_value|模式身份)。
   同一提案不重复入队；**模式身份（源/意图/类型/执行体）必须进指纹**——否则
   同一规则下的不同模式（如 5 个不同工具的 unknown 失败）会互相当成重复只
   入队一条，其余证据凭空丢失（真库实测踩到：created=3 skipped=5）；已决
   （applied/rejected）不重开；现场配置变化导致 proposed 变化 ⇒ 指纹变 ⇒
   可重新提议（闭环不卡死）。
4. 三态提案状态机：pending → applied | rejected（终态，已决不重开；
   与 #18 状态机纪律一致——decide 是唯一转移且必须过人）。
5. LLM 反思改写（GEPA 式，可选）：llm_draft=True 时把「模式证据 + 规则
   草案」交给 LLM 改写成更具体的 rationale；Mock/无 key/异常 → 回落规则
   模板（确定性保持，同 RefineGate 降级房规）。默认 False——先证明模板
   提案可用，再开 LLM 增强。
6. 配置驱动：口径走 core.config 的 config_proposal 组（七键），模块内不
   写死阈值；门禁用变异自证口径真被消费。
7. 只读失败轨迹：读 P1-2 collect_patterns（其本身只读）；写只经
   repositories/proposal_repo.py（提案队列 CRUD），**绝不写配置**。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

# ── 产品契约常量（API/门禁/前端三方共用，勿在调用处另写字面量）─────────────
PROPOSAL_PENDING = "pending"
PROPOSAL_APPLIED = "applied"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_STATUSES = (PROPOSAL_PENDING, PROPOSAL_APPLIED, PROPOSAL_REJECTED)
PROPOSAL_TERMINAL = (PROPOSAL_APPLIED, PROPOSAL_REJECTED)

#: 决策合法性（纯函数）：只有 pending 可决；已决是终态，不重开
def can_decide(status: str) -> bool:
    return (status or "") == PROPOSAL_PENDING


#: 配置默认值（与 core.config DEFAULT_CONFIG.config_proposal 一致；此处仅兜底，
#: 正常路径全部走配置——门禁变异自证）
_SETTINGS_DEFAULTS = {
    "enabled": True,
    "min_occurrences": 2,
    "prompt_min_occurrences": 4,
    "unattributed_min": 1,
    "max_proposals": 20,
    "llm_draft": False,
    "max_rationale_chars": 500,
}


@dataclass(frozen=True)
class ProposalRuleSpec:
    """提议规则：一条首坏步模式 → 0..n 个修订草案。

    match 是纯函数：入参只有 (pattern, ctx)——
      pattern  collect_patterns 的一条首坏步模式（source/intent/kind/actor/
                count/conversations/first_seen/last_seen/sample_steps）
      ctx      {"min_occurrences", "prompt_min_occurrences", "unattributed_min",
                "kind_coverage", "unattributed_count", "config"}
                （config 是现场配置快照 {section: {key: value}}，规则内零 I/O）
    返回草案列表；每项 {"target_section", "target_key", "current_value",
    "proposed_value", "rationale"}。返回空列表 = 不命中。
    """
    name: str
    label: str
    desc: str
    match: Callable[[dict, dict], list]


def _llm_quota_fallback(pattern: dict, ctx: dict) -> list:
    """配额/余额/无密钥类失败 ⇒ 提议配置备用 provider（当前=0 即未配置）。"""
    if pattern.get("source") != "llm_call":
        return []
    if pattern.get("kind") not in ("balance", "quota", "no_api_key"):
        return []
    if int(pattern.get("count") or 0) < int(ctx["min_occurrences"]):
        return []
    cur = (ctx.get("config", {}).get("llm", {}) or {}).get("fallback_provider_id", "")
    return [{
        "target_section": "llm",
        "target_key": "fallback_provider_id",
        "current_value": str(cur),
        "proposed_value": "配置一个可用的备用 provider id（当前 %s = 未配置/失效）" % cur,
        "rationale": (
            "主 provider 连续 %d 次配额/余额/密钥类失败（跨 %d 个会话，最近 %s）。"
            "这类失败重试无意义——要么等额度恢复，要么切备用 provider。"
            "当前 llm.fallback_provider_id=%s，建议在设置页选定可用备用 provider。"
            % (pattern.get("count"), pattern.get("conversations"),
               pattern.get("last_seen"), cur)),
    }]


def _llm_transient_retry(pattern: dict, ctx: dict) -> list:
    """超时/连接类失败 ⇒ 提议上调重试次数（现值+1，封顶 5）。"""
    if pattern.get("source") != "llm_call":
        return []
    if pattern.get("kind") not in ("timeout", "conn", "transient", "http_5xx",
                                   "network", "http_502", "http_503"):
        return []
    if int(pattern.get("count") or 0) < int(ctx["min_occurrences"]):
        return []
    cur = (ctx.get("config", {}).get("llm", {}) or {}).get("retry_times", 2)
    try:
        cur_i = int(cur)
    except (TypeError, ValueError):
        cur_i = 2
    proposed = min(cur_i + 1, 5)
    return [{
        "target_section": "llm",
        "target_key": "retry_times",
        "current_value": str(cur),
        "proposed_value": str(proposed),
        "rationale": (
            "网络/超时类失败 %d 次（%s，跨 %d 个会话）——瞬时故障多打一次往往就过。"
            "当前 llm.retry_times=%s，提议上调到 %s（封顶 5：重试有成本，"
            "且熔断器 circuit_breaker_fail_threshold 仍在兜底）。"
            % (pattern.get("count"), pattern.get("kind"),
               pattern.get("conversations"), cur, proposed)),
    }]


def _prompt_skill_revision(pattern: dict, ctx: dict) -> list:
    """复发失败模式 ⇒ 提议修订对应 (意图, 执行体) 的 prompt/skill（GEPA 本体）。

    下限比配置类高一倍：改提示词影响面大于改一个配置键——低频模式不值得
    冒这个险，让配置/埋点提案先去处理。
    """
    if not pattern.get("intent") or not pattern.get("actor"):
        return []
    if int(pattern.get("count") or 0) < int(ctx["prompt_min_occurrences"]):
        return []
    return [{
        "target_section": "skill",
        "target_key": "%s/%s" % (pattern.get("intent"), pattern.get("actor")),
        "current_value": "<当前 prompt/skill 版本>",
        "proposed_value": "针对该 (意图, 执行体) 的复发失败模式修订 prompt/skill",
        "rationale": (
            "同一 (意图=%s, 执行体=%s) 的首坏步复发 %d 次（跨 %d 个会话，%s → %s）——"
            "单点配置/重试解释不了稳定复发，更可能是提示词或技能定义让该执行体"
            "系统性地走错。建议review其 prompt/skill 后修订（只提议，应用由人裁决）。"
            % (pattern.get("intent"), pattern.get("actor"), pattern.get("count"),
               pattern.get("conversations"), pattern.get("first_seen"),
               pattern.get("last_seen"))),
    }]


def _tool_kind_gap(pattern: dict, ctx: dict) -> list:
    """工具失败无类型（kind 空）⇒ 提议补埋点/扩分类词表。"""
    if pattern.get("source") != "tool_call" or pattern.get("kind"):
        return []
    if int(pattern.get("count") or 0) < int(ctx["min_occurrences"]):
        return []
    kc = ctx.get("kind_coverage") or {}
    return [{
        "target_section": "instrumentation",
        "target_key": "tool_call_logs.failure_kind",
        "current_value": "空（unknown）",
        "proposed_value": "为未分类的工具失败补类型：扩 services/failure_class 词表或补埋点",
        "rationale": (
            "工具 %s 的无类型失败首坏 %d 次。全窗口 kind 覆盖率 known=%s/unknown=%s"
            "（%s%%）——unknown 失败只能归到位置、归不到原因，提案器与人都看不清"
            "该修什么。建议对 sample_steps 的 failure 文本补分类（词表或专分支）。"
            % (pattern.get("actor"), pattern.get("count"), kc.get("known_kind"),
               kc.get("unknown_kind"), kc.get("known_pct"))),
    }]


def _unattributed_gap(pattern: dict, ctx: dict) -> list:
    """结局坏但零坏步（unattributed）⇒ 提议补埋点。

    这条不依赖单条 pattern（它由 ctx.unattributed_count 驱动，对每个 pattern
    最多产一条）——埋点缺口是全局事实，挂在每个模式上会重复入队，故只在
    遍历第一个模式时产出（generate 侧用 seen 集合兜底）。
    """
    if int(ctx.get("unattributed_count") or 0) < int(ctx["unattributed_min"]):
        return []
    return [{
        "target_section": "instrumentation",
        "target_key": "step_coverage",
        "current_value": "%s 个会话结局坏但零坏步" % ctx.get("unattributed_count"),
        "proposed_value": "为结局坏但零坏步的会话补步骤埋点（失败发生在已埋点步骤之外）",
        "rationale": (
            "归因器发现 %s 个会话结局证据坏（agent_tasks/orch_checkpoints/flow_runs"
            " 有失败终态）但步骤序列零坏——失败发生在已埋点的三类步骤之外，或根本没记。"
            "看不见的失败比失败本身更危险：建议对这些会话补埋点后重跑归因。"
            % ctx.get("unattributed_count")),
    }]


_RULES = [
    ProposalRuleSpec(
        name="llm_quota_fallback", label="LLM 配额类失败 → 备用 provider",
        desc="balance/quota/no_api_key 类 LLM 失败 ⇒ 提议配置 llm.fallback_provider_id",
        match=_llm_quota_fallback),
    ProposalRuleSpec(
        name="llm_transient_retry", label="LLM 瞬时类失败 → 重试上调",
        desc="timeout/conn/transient 类 LLM 失败 ⇒ 提议上调 llm.retry_times（现值+1 封顶 5）",
        match=_llm_transient_retry),
    ProposalRuleSpec(
        name="prompt_skill_revision", label="复发失败模式 → prompt/skill 修订",
        desc="同一 (意图,执行体) 首坏步复发 ≥prompt_min_occurrences ⇒ 提议修订其 prompt/skill",
        match=_prompt_skill_revision),
    ProposalRuleSpec(
        name="tool_kind_gap", label="工具失败无类型 → 埋点/词表",
        desc="kind 为空的工具失败首坏 ⇒ 提议扩 failure_class 词表或补埋点",
        match=_tool_kind_gap),
    ProposalRuleSpec(
        name="unattributed_gap", label="结局坏零坏步 → 埋点补全",
        desc="unattributed 会话数达阈值 ⇒ 提议补步骤埋点（全局事实，每批只出一条）",
        match=_unattributed_gap),
]


def get_settings() -> dict:
    """取材/提议口径。缺配置时回落默认值（与 DEFAULT_CONFIG 一致，不是第二套真值）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if key in ("enabled", "llm_draft"):
            # 布尔键走 as_bool（容忍配置里存 "false"/"0" 字符串；与 attribution 同口径）
            out[key] = _config.as_bool("config_proposal", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("config_proposal", key, default))
        except Exception:
            out[key] = default
    return out


def proposal_fingerprint(rule: str, section: str, key: str, proposed: str,
                         pattern_key: str = "") -> str:
    """提案指纹：同规则同目标同提议值同**模式身份** ⇒ 同指纹（去重判据）。

    pattern_key = (source, intent, kind, actor) 的拼接——**必须纳入**：否则
    同一规则下的不同模式（如 5 个不同工具的 unknown 失败）会指纹相同互相去重，
    只入队一条，其余证据凭空丢失（真库实测踩到：created=3 skipped=5）。
    带上模式身份后，每个可独立裁决（reviewer 可以只驳回自己不关心的那个）。
    已决不重开：终态行指纹相同 ⇒ 冲突 ⇒ 不再入队。
    现场配置变化 ⇒ proposed 变 ⇒ 指纹变 ⇒ 可重新提议（闭环不卡死）。
    """
    raw = "|".join([str(rule), str(section), str(key), str(proposed), str(pattern_key)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _config_snapshot() -> dict:
    """现场配置快照（只读）。规则据此写 current_value——没读现场的提案是拍脑袋。"""
    from core import config as _config
    return {"llm": {"fallback_provider_id": _config.get("llm", "fallback_provider_id", 0),
                    "retry_times": _config.get("llm", "retry_times", 2)}}


def propose_from_patterns(patterns: dict, *, settings: Optional[dict] = None,
                          config_snapshot: Optional[dict] = None) -> list:
    """首坏步模式 → 修订草案列表（纯函数：无 I/O、无落库，门禁 A 段逐条打）。

    排序确定性：(count 降序, rule, target_key)——同输入同输出。
    unattributed_gap 是全局事实：每批只出一条（seen_rules 兜底），否则 N 个
    模式会产出 N 条相同埋点提案把队列淹掉。
    """
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return []
    ctx = {
        "min_occurrences": int(cfg["min_occurrences"]),
        "prompt_min_occurrences": int(cfg["prompt_min_occurrences"]),
        "unattributed_min": int(cfg["unattributed_min"]),
        "kind_coverage": patterns.get("kind_coverage") or {},
        "unattributed_count": (patterns.get("unattributed_sessions") or {}).get("count", 0),
        "config": config_snapshot if config_snapshot is not None else _config_snapshot(),
    }
    drafts, seen_rules = [], set()
    for pat in patterns.get("first_bad_patterns") or []:
        for rule in _RULES:
            if rule.name == "unattributed_gap" and rule.name in seen_rules:
                continue
            try:
                out = rule.match(pat, ctx) or []
            except Exception:
                out = []                      # 单条规则异常不连坐其它规则
            for d in out:
                d = dict(d)
                d["rule"] = rule.name
                pat_key = "|".join([str(pat.get("source") or ""), str(pat.get("intent") or ""),
                                    str(pat.get("kind") or ""), str(pat.get("actor") or "")])
                d["fingerprint"] = proposal_fingerprint(
                    rule.name, d.get("target_section", ""), d.get("target_key", ""),
                    d.get("proposed_value", ""), pat_key)
                # 排序与 LLM 增补用；下划线开头 = 不落库（repo.insert 剥离）
                d["_pattern_count"] = int(pat.get("count") or 0)
                d["_pattern"] = pat
                drafts.append(d)
            if out:
                seen_rules.add(rule.name)
    drafts.sort(key=lambda d: (-d.get("_pattern_count", 0), d["rule"],
                               d.get("target_key", "")))
    return drafts[:int(cfg["max_proposals"])]


def _llm_rationale(rule_label: str, pattern: dict, draft: dict, settings: dict) -> Optional[str]:
    """GEPA 式反思改写：LLM 把「模式证据 + 规则草案」改写成更具体的 rationale。

    失败/Mock/无 key/超短输出 → None（调用方回落规则模板）。默认不开
    （config_proposal.llm_draft=False）——先证明模板提案可用再开增强。
    """
    if not settings.get("llm_draft"):
        return None
    try:
        from llm import llm_client
        prompt = (
            "你是 MBSE 平台的配置评审专家。下面是一条**首坏步失败模式**和一条"
            "**规则草案**。请把草案的 rationale 改写得更具体可执行：指出先改什么、"
            "怎么验证（给出可观测判据）、有什么风险。只输出改写后的 rationale 正文，"
            "不要输出 JSON，不要复述模式。\n\n"
            f"规则：{rule_label}\n"
            f"失败模式：source={pattern.get('source')} intent={pattern.get('intent')} "
            f"kind={pattern.get('kind')} actor={pattern.get('actor')} "
            f"count={pattern.get('count')} conversations={pattern.get('conversations')}\n"
            f"草案：{draft.get('target_section')}.{draft.get('target_key')} "
            f"{draft.get('current_value')} → {draft.get('proposed_value')}\n"
            f"规则模板 rationale：{draft.get('rationale')}\n"
        )
        resp = llm_client.chat([{"role": "user", "content": prompt}],
                               max_tokens=600, _intent="config_proposal")
        text = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
        text = text.strip()
        if len(text) >= 40:
            return text[:int(settings["max_rationale_chars"])]
    except Exception:
        pass
    return None


def generate(conn, *, settings: Optional[dict] = None) -> dict:
    """跑规则 → 落库（经 proposal_repo，只写提案队列，绝不写配置）。

    返回 {created, skipped, proposals, patterns_summary, llm_drafted}。
    created = 本次新入队；skipped = 指纹已存在（pending 重复或已决不重开）。
    """
    from repositories.proposal_repo import ProposalRepo
    from core import attribution as _attr
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"enabled": False, "created": 0, "skipped": 0, "proposals": []}
    repo = ProposalRepo(conn)
    patterns = _attr.collect_patterns(conn)
    drafts = propose_from_patterns(patterns, settings=cfg)
    created, skipped, llm_drafted = [], 0, 0
    for d in drafts:
        # LLM 反思改写（可选）：enrich rationale，失败回落模板
        if cfg.get("llm_draft"):
            rule_label = next((r.label for r in _RULES if r.name == d["rule"]), d["rule"])
            enriched = _llm_rationale(rule_label, d.get("_pattern") or {}, d, cfg)
            if enriched:
                d = dict(d, rationale=enriched)
                llm_drafted += 1
        row, is_new = repo.insert_proposal(d)
        if is_new:
            created.append(row)
        else:
            skipped += 1
    return {"enabled": True, "created": len(created), "skipped": skipped,
            "proposals": created, "llm_drafted": llm_drafted,
            "patterns_summary": {
                "sessions_scanned": patterns.get("sessions_scanned", 0),
                "sessions_with_first_bad": patterns.get("sessions_with_first_bad", 0),
                "unattributed": (patterns.get("unattributed_sessions") or {}).get("count", 0)}}


def decide(conn, proposal_id: int, decision: str, note: str = "",
           decided_by: str = "manual") -> dict:
    """裁决提案（pending → applied | rejected）。**不碰任何配置**——应用是人
    在设置页做的事，这里只留痕。已决（终态）拒绝再决。"""
    from repositories.proposal_repo import ProposalRepo
    if decision not in PROPOSAL_TERMINAL:
        return {"ok": False, "error": "不支持的决策: %s（可选: %s）"
                % (decision, "/".join(PROPOSAL_TERMINAL))}
    repo = ProposalRepo(conn)
    row = repo.get_proposal(proposal_id)
    if not row:
        return {"ok": False, "error": "提案不存在"}
    if not can_decide(row.get("status")):
        return {"ok": False, "error": "提案已决（%s），终态不重开" % row.get("status")}
    return repo.decide_proposal(proposal_id, decision, note, decided_by)


def list_proposals(conn, status: str = "", limit: int = 100) -> list:
    """提案队列（status 空 = 全部；limit 封顶防长尾淹掉看板）。"""
    from repositories.proposal_repo import ProposalRepo
    return ProposalRepo(conn).list_proposals(status=status, limit=limit)
