# -*- coding: utf-8 -*-
"""P2-2 失败驱动的任务自生成（curriculum）：失败证据 → 任务提案（只提议）。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P2 第二项）
═══════════════════════════════════════════════════════════════
行业参照：EnvBuilder / AgentGym / AgentEvolver Self-Questioning——agent 自己
造课程（curriculum）：从自身失败里长出针对弱点的练习任务，而不是等人喂题。
本系统到此为止有的零件：P0-3 坏样本台账（bad_cases）、P1-1 trace→评测集
（已知样本的搬运）、P1-2 首坏步模式（诊断）、P1-3 配置提案（改配置的建议）。
缺的一环：**把失败证据变成「该练什么任务」的建议**——诊断环到练习环之间
没有消费者。评测集只覆盖「已经发生过的会话」，弱区（某个执行体反复错、
某类失败从无负例）恰恰没有对应练习。

本模块补上这一环，且守住同 P1-3 的铁律：**只提议，不自动执行**。
自动造任务并自动执行 = 让系统自己给自己布置并跑分——没有证据表明这安全
（misevolution 的滑坡正是内部循环自我强化），故任务提案必须经人裁决
（accepted/rejected 留痕）；"accepted" 只记录"人认领了这条任务"这一事实，
任务的真正创建/执行走人所在的那条链路（评测集构建、编排批次）。

═══════════════════════════════════════════════════════════════
设计纪律（与 core/config_proposal、core/badcase 同构）
═══════════════════════════════════════════════════════════════
1. 提议规则注册表：新规则 = 登记一个 TaskRuleSpec（name/label/desc +
   match 纯函数）。match(evidence, ctx) → 0..n 个草案；规则内零 I/O
   （所有证据由 generate 侧一次读齐后传入——规则不该自己摸库）。
2. 四条初始规则按证据强度分级：
   · weakness_repeat      首坏步模式复发 ≥min_pattern_count ⇒ 提议针对该
                          (源,意图,类型,执行体) 的回归巩固任务
   · badcase_recurrence   台账里 open 且 occurrences ≥min_badcase_occurrences
                          的坏样本 ⇒ 提议复现/加固任务（坏样本是现成考题）
   · kind_coverage_gap    失败类型覆盖率 <kind_coverage_min ⇒ 提议补分类
                          任务（归不到原因的失败练不了——先补可监控性，
                          红线 #4：任何"优化"不得降低证据链完整度）
   · unattributed_gap     结局坏但零坏步的会话 ≥unattributed_min ⇒ 提议补
                          埋点任务（看不见的失败比失败更危险）
3. 指纹去重：fingerprint = sha1(rule|task_kind|weakness)。**弱点身份必须
   进指纹**——同一规则下不同弱点（如 5 个不同执行体的复发）指纹相同会
   互相当成重复只入队一条，其余证据凭空丢失（P1-3 真库实测踩过的坑：
   created=3 skipped=5）。已决不重开；弱点复发数变化不改指纹（同一弱点
   的任务不该重复入队，证据更新由人重新 generate 前先看队列表）。
4. 三态状态机：pending → accepted | rejected（终态不重开，与 #18 状态机
   纪律一致——decide 是唯一转移且必须过人）。
5. 诚实证据：没有失败证据 ⇒ created=0（空窗口不是"没问题"，更不是
   "该造点任务凑数"）；每条提案的 evidence_json 必须能回溯到具体
   pattern/坏样本行（sample_steps / kind+subject / 会话样本）。
6. 难度估计是保守纯函数：只按复发次数分档（low/medium/high）——没有
   任何"任务一定很难"的先验，不装。
7. 只读失败证据：读 attribution.collect_patterns（其本身只读）+ bad_cases
   表；写只经 repositories/curriculum_repo.py（任务提案队列 CRUD），
   **不创建任何任务、不改任何配置**。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

# ── 产品契约常量（API/门禁/前端三方共用，勿在调用处另写字面量）─────────────
TASK_PENDING = "pending"
TASK_ACCEPTED = "accepted"
TASK_REJECTED = "rejected"
TASK_STATUSES = (TASK_PENDING, TASK_ACCEPTED, TASK_REJECTED)
TASK_TERMINAL = (TASK_ACCEPTED, TASK_REJECTED)

#: 任务种类（提案的"该练什么"分类；消费侧按 kind 决定怎么落地）
TASK_KINDS = ("regression_hardening", "badcase_reproduction",
              "failure_classification", "instrumentation")
#: 难度档位（保守纯函数分档，无先验）
DIFFICULTIES = ("low", "medium", "high")


#: 决策合法性（纯函数）：只有 pending 可决；已决是终态，不重开
def can_decide(status: str) -> bool:
    return (status or "") == TASK_PENDING


#: 配置默认值（与 core.config DEFAULT_CONFIG.curriculum 一致；此处仅兜底，
#: 正常路径全部走配置——门禁变异自证）
_SETTINGS_DEFAULTS = {
    "enabled": True,
    "min_pattern_count": 2,        # 首坏步模式复发下限（<此值不值得单独立任务）
    "min_badcase_occurrences": 3,  # 坏样本复发下限（台账 occurrences）
    "kind_coverage_min": 0.5,      # 失败类型覆盖率下沿（红线 #4 可监控性）
    "unattributed_min": 1,         # 结局坏零坏步会话数下限
    "max_proposals": 20,
}


@dataclass(frozen=True)
class TaskRuleSpec:
    """任务提议规则：一份失败证据 → 0..n 个任务草案。

    match 是纯函数：入参只有 (evidence, ctx)——
      evidence 一次读齐的证据包：
        {"patterns":      attribution.collect_patterns 的返回值,
         "badcases":      [bad_cases 行（status='open'）...],
         "kind_coverage": patterns.kind_coverage（便捷副本）,
         "unattributed_count": patterns.unattributed_sessions.count}
      ctx     配置口径（min_pattern_count / min_badcase_occurrences /
               kind_coverage_min / unattributed_min / max_proposals）
    返回草案列表；每项 {"task_kind", "title", "weakness", "suggestion",
    "difficulty", "evidence"}。返回空列表 = 不命中。
    """
    name: str
    label: str
    desc: str
    match: Callable[[dict, dict], list]


def difficulty_of(count: int) -> str:
    """复发次数 → 难度档（保守纯函数：只按证据量分档，无任务先验）。

    ≥8 次复发 = high（稳定复现，值得专门立任务）；≥3 = medium；
    其余 low。没有"某类任务天生难"的说法——难度只反映证据强度。
    """
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 8:
        return "high"
    if n >= 3:
        return "medium"
    return "low"


def task_fingerprint(rule: str, task_kind: str, weakness: str) -> str:
    """任务指纹：同规则同种类同**弱点身份** ⇒ 同指纹（去重判据）。

    弱点身份（pattern_key / kind|subject / 指标名）必须纳入：否则同一规则下
    的不同弱点会指纹相同互相去重，只入队一条，其余证据凭空丢失（P1-3 的
    教训：模式身份不进指纹 ⇒ created=3 skipped=5）。已决不重开：终态行
    指纹相同 ⇒ 冲突 ⇒ 不再入队。
    """
    raw = "|".join([str(rule), str(task_kind), str(weakness)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _pattern_key(pat: dict) -> str:
    """弱点身份：(源,意图,类型,执行体) 四元组——与 P1-3 同口径。"""
    return "|".join([str(pat.get("source") or ""), str(pat.get("intent") or ""),
                     str(pat.get("kind") or ""), str(pat.get("actor") or "")])


def _pattern_evidence(pat: dict) -> dict:
    """pattern → evidence_json（溯源：哪个模式、什么证据促成了这条任务）。"""
    return {
        "type": "first_bad_pattern",
        "source": pat.get("source"), "intent": pat.get("intent"),
        "kind": pat.get("kind"), "actor": pat.get("actor"),
        "count": pat.get("count"), "conversations": pat.get("conversations"),
        "first_seen": pat.get("first_seen"), "last_seen": pat.get("last_seen"),
        "sample_steps": (pat.get("sample_steps") or [])[:3],
    }


def _weakness_repeat(evidence: dict, ctx: dict) -> list:
    """首坏步模式复发 ⇒ 回归巩固任务（针对该弱点的练习/负例构造）。"""
    out = []
    for pat in (evidence.get("patterns") or {}).get("first_bad_patterns") or []:
        count = int(pat.get("count") or 0)
        if count < int(ctx["min_pattern_count"]):
            continue
        out.append({
            "task_kind": "regression_hardening",
            "title": "回归巩固：%s·%s·%s（%s）首坏复发 %d 次"
                     % (pat.get("source") or "?", pat.get("kind") or "无类型",
                        pat.get("actor") or "?", pat.get("intent") or "无意图", count),
            "weakness": _pattern_key(pat),
            "suggestion": (
                "针对 (源=%s, 意图=%s, 类型=%s, 执行体=%s) 的首坏步复发 %d 次"
                "（跨 %s 个会话，%s → %s）构造回归任务：把该失败场景固化为"
                "评测负例（可参照 P1-1 trace2dataset 的 negative 档口径抽取），"
                "并验证对应 prompt/skill/工具路径修订后不再复发。样本步骤：%s"
                % (pat.get("source"), pat.get("intent"), pat.get("kind") or "unknown",
                   pat.get("actor"), count, pat.get("conversations"),
                   pat.get("first_seen"), pat.get("last_seen"),
                   ", ".join((pat.get("sample_steps") or [])[:3]) or "无")),
            "difficulty": difficulty_of(count),
            "evidence": _pattern_evidence(pat),
        })
    return out


def _badcase_recurrence(evidence: dict, ctx: dict) -> list:
    """台账 open 且复发的坏样本 ⇒ 复现/加固任务（坏样本是现成考题）。

    status 判据在规则内自带（纵深防御）：collect_evidence 的 SQL 已过滤
    open，但纯函数的正确性不依赖调用方预过滤——直喂 resolved 行也不产任务。
    """
    out = []
    for bc in evidence.get("badcases") or []:
        if (bc.get("status") or "") != "open":
            continue
        occ = int(bc.get("occurrences") or 0)
        if occ < int(ctx["min_badcase_occurrences"]):
            continue
        kind, subject = bc.get("kind") or "", bc.get("subject") or ""
        out.append({
            "task_kind": "badcase_reproduction",
            "title": "坏样本复现：%s·%s 复发 %d 次（%s）"
                     % (kind, subject, occ, bc.get("severity") or "?"),
            "weakness": "%s|%s" % (kind, subject),
            "suggestion": (
                "坏样本台账（P0-3）中 %s·%s 仍 open 且复发 %d 次"
                "（%s → %s，严重度 %s）。把该坏样本的触发条件整理成可复现的"
                "评测任务（输入=当时场景，期望=不重犯），修订后经评测集验证。"
                "标题：%s"
                % (kind, subject, occ, bc.get("first_seen_at"),
                   bc.get("last_seen_at"), bc.get("severity"),
                   (bc.get("title") or "")[:80])),
            "difficulty": difficulty_of(occ),
            "evidence": {"type": "bad_case", "kind": kind, "subject": subject,
                         "occurrences": occ, "severity": bc.get("severity"),
                         "status": bc.get("status"),
                         "first_seen_at": bc.get("first_seen_at"),
                         "last_seen_at": bc.get("last_seen_at"),
                         "title": bc.get("title")},
        })
    return out


def _kind_coverage_gap(evidence: dict, ctx: dict) -> list:
    """失败类型覆盖率不足 ⇒ 补分类任务（先可监控，后练习；红线 #4）。

    诚实分母：bad_steps=0（空窗口）时 known_pct 是 None——**空窗口不是 0%
    覆盖率**（misevolution 的教训）：没有失败就没有分类缺口，不许凑一条
    「覆盖率不足」的任务出来。

    量纲口径：attribution 的 known_pct 是 0-100 百分数，配置
    kind_coverage_min 是 0-1 分数（与 misevolution/attribution 各组一致）——
    比较前统一换算，杜绝「33.3% >= 0.5 恒真 ⇒ 规则永不触发」这类量纲坑
    （门禁 A6 三条断言锁死两个方向 + 空窗口）。
    """
    kc = evidence.get("kind_coverage") or {}
    bad_steps = int(kc.get("bad_steps") or 0)
    if bad_steps <= 0:
        return []
    try:
        pct = float(kc.get("known_pct") or 0.0)      # 0-100 百分数
    except (TypeError, ValueError):
        pct = 0.0
    frac = pct / 100.0                                # 统一到 0-1 与配置比较
    if frac >= float(ctx["kind_coverage_min"]):
        return []
    return [{
        "task_kind": "failure_classification",
        "title": "失败分类补全：kind 覆盖率 %.1f%%（下沿 %.0f%%）"
                 % (pct, float(ctx["kind_coverage_min"]) * 100),
        "weakness": "kind_coverage",
        "suggestion": (
            "全窗口失败类型覆盖率 known=%s/unknown=%s（%.1f%%）低于下沿"
            " %.0f%%——unknown 失败只能归到位置、归不到原因，练不了也改不了。"
            "任务：对 unknown 的失败文本补 failure_kind 分类（扩 "
            "services/failure_class 词表或补埋点分支），补完重跑 P1-2 归因"
            "验证覆盖率越线。这是红线 #4（可监控性保持）的直接要求：任何"
            "「优化」不得降低证据链完整度。"
            % (kc.get("known_kind"), kc.get("unknown_kind"), pct,
               float(ctx["kind_coverage_min"]) * 100)),
        "difficulty": "medium",
        "evidence": {"type": "kind_coverage", **kc},
    }]


def _unattributed_gap(evidence: dict, ctx: dict) -> list:
    """结局坏但零坏步 ⇒ 补埋点任务（看不见的失败比失败更危险）。"""
    n = int(evidence.get("unattributed_count") or 0)
    if n < int(ctx["unattributed_min"]):
        return []
    samples = ((evidence.get("patterns") or {}).get("unattributed_sessions")
               or {}).get("samples") or []
    return [{
        "task_kind": "instrumentation",
        "title": "埋点补全：%d 个会话结局坏但零坏步" % n,
        "weakness": "step_coverage",
        "suggestion": (
            "归因器（P1-2）发现 %d 个会话结局证据坏（agent_tasks/orch_"
            "checkpoints/flow_runs 有失败终态）但步骤序列零坏——失败发生在"
            "已埋点的三类步骤之外，或根本没记。任务：对这些会话补步骤埋点"
            "（或补判据面），补后重跑归因直到 unattributed 归零。看不见的"
            "失败比失败本身更危险：不补埋点，任何练习任务都建在流沙上。"
            "样本会话：%s" % (n, ", ".join(str(s) for s in samples[:5]) or "无")),
        "difficulty": "high" if n >= 5 else "medium",
        "evidence": {"type": "unattributed_sessions", "count": n,
                     "samples": samples[:5]},
    }]


_RULES = [
    TaskRuleSpec(
        name="weakness_repeat", label="复发弱点 → 回归巩固任务",
        desc="首坏步模式复发 ≥min_pattern_count ⇒ 提议针对该 (源,意图,类型,执行体) 的回归巩固任务",
        match=_weakness_repeat),
    TaskRuleSpec(
        name="badcase_recurrence", label="复发坏样本 → 复现任务",
        desc="bad_cases 中 open 且 occurrences ≥min_badcase_occurrences ⇒ 提议复现/加固任务",
        match=_badcase_recurrence),
    TaskRuleSpec(
        name="kind_coverage_gap", label="失败类型覆盖率不足 → 补分类任务",
        desc="kind 覆盖率 <kind_coverage_min ⇒ 提议补 failure_kind 分类（红线 #4 可监控性）",
        match=_kind_coverage_gap),
    TaskRuleSpec(
        name="unattributed_gap", label="结局坏零坏步 → 补埋点任务",
        desc="unattributed 会话数达阈值 ⇒ 提议补步骤埋点（每批只出一条：全局事实）",
        match=_unattributed_gap),
]


def get_settings() -> dict:
    """取材口径。缺配置时回落默认值（与 DEFAULT_CONFIG 一致，不是第二套真值）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if key == "enabled":
            out[key] = _config.as_bool("curriculum", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("curriculum", key, default))
        except Exception:
            out[key] = default
    return out


def collect_evidence(conn) -> dict:
    """一次读齐失败证据（只读）。规则不自己摸库——证据包在这里统一装配。"""
    from core import attribution as _attr
    patterns = {}
    try:
        patterns = _attr.collect_patterns(conn) or {}
    except Exception:
        patterns = {}                    # 归因器异常 ⇒ 无模式证据（不连坐坏样本）
    badcases = []
    try:
        rows = conn.execute(
            "SELECT kind, subject, title, severity, occurrences, status, "
            "first_seen_at, last_seen_at FROM bad_cases WHERE status='open' "
            "ORDER BY occurrences DESC, id DESC LIMIT 200").fetchall()
        badcases = [dict(r) for r in rows]
    except Exception:
        badcases = []                    # 表未建（老库）⇒ 无坏样本证据
    return {
        "patterns": patterns,
        "badcases": badcases,
        "kind_coverage": patterns.get("kind_coverage") or {},
        "unattributed_count": (patterns.get("unattributed_sessions")
                               or {}).get("count", 0),
    }


def propose_from_evidence(evidence: dict, *, settings: Optional[dict] = None) -> list:
    """失败证据 → 任务草案列表（纯函数：无 I/O、无落库，门禁 A 段逐条打）。

    排序确定性：(difficulty 档位倒序, count 倒序, rule, weakness)——同输入
    同输出。全局事实类规则（kind_coverage_gap / unattributed_gap）天然每批
    只出一条（按弱点身份去重由指纹兜底）。
    """
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return []
    ctx = {
        "min_pattern_count": int(cfg["min_pattern_count"]),
        "min_badcase_occurrences": int(cfg["min_badcase_occurrences"]),
        "kind_coverage_min": float(cfg["kind_coverage_min"]),
        "unattributed_min": int(cfg["unattributed_min"]),
        "max_proposals": int(cfg["max_proposals"]),
    }
    drafts = []
    for rule in _RULES:
        try:
            out = rule.match(evidence, ctx) or []
        except Exception:
            out = []                      # 单条规则异常不连坐其它规则
        for d in out:
            d = dict(d)
            d["rule"] = rule.name
            d["fingerprint"] = task_fingerprint(rule.name, d.get("task_kind", ""),
                                                d.get("weakness", ""))
            # 排序用（不落库）：复发量 = evidence 里的 count/occurrences
            ev = d.get("evidence") or {}
            d["_weight"] = int(ev.get("count") or ev.get("occurrences") or 0)
            drafts.append(d)
    _diff_rank = {d: i for i, d in enumerate(
        ("high", "medium", "low"))}
    drafts.sort(key=lambda d: (_diff_rank.get(d.get("difficulty"), 9),
                               -d.get("_weight", 0), d["rule"],
                               d.get("weakness", "")))
    return drafts[:int(cfg["max_proposals"])]


def generate(conn, *, settings: Optional[dict] = None) -> dict:
    """跑规则 → 落库（经 curriculum_repo，只写任务提案队列）。

    返回 {enabled, created, skipped, proposals, evidence_summary}。
    created = 本次新入队；skipped = 指纹已存在（pending 重复或已决不重开）。
    **不创建任何任务、不改任何配置**——任务的创建/执行是人认领后的事。
    """
    from repositories.curriculum_repo import CurriculumRepo
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"enabled": False, "created": 0, "skipped": 0, "proposals": []}
    repo = CurriculumRepo(conn)
    evidence = collect_evidence(conn)
    drafts = propose_from_evidence(evidence, settings=cfg)
    created, skipped = [], 0
    for d in drafts:
        row, is_new = repo.insert_proposal(d)
        if is_new:
            created.append(row)
        else:
            skipped += 1
    pats = evidence.get("patterns") or {}
    return {"enabled": True, "created": len(created), "skipped": skipped,
            "proposals": created,
            "evidence_summary": {
                "sessions_scanned": pats.get("sessions_scanned", 0),
                "sessions_with_first_bad": pats.get("sessions_with_first_bad", 0),
                "first_bad_patterns": len(pats.get("first_bad_patterns") or []),
                "open_badcases": len(evidence.get("badcases") or []),
                "kind_coverage": evidence.get("kind_coverage") or {},
                "unattributed": evidence.get("unattributed_count", 0)}}


def decide(conn, item_id: int, decision: str, note: str = "",
           decided_by: str = "manual") -> dict:
    """裁决任务提案（pending → accepted | rejected）。

    **不创建任何任务**——"accepted" 只记录"人认领了这条任务"（审计留痕），
    任务的真正创建/执行走人所在链路。已决（终态）拒绝再决。
    """
    from repositories.curriculum_repo import CurriculumRepo
    if decision not in TASK_TERMINAL:
        return {"ok": False, "error": "不支持的决策: %s（可选: %s）"
                % (decision, "/".join(TASK_TERMINAL))}
    repo = CurriculumRepo(conn)
    row = repo.get_proposal(item_id)
    if not row:
        return {"ok": False, "error": "任务提案不存在"}
    if not can_decide(row.get("status")):
        return {"ok": False, "error": "任务提案已决（%s），终态不重开" % row.get("status")}
    return repo.decide_proposal(item_id, decision, note, decided_by)


def list_proposals(conn, status: str = "", limit: int = 100) -> list:
    """任务提案队列（status 空 = 全部；limit 封顶防长尾淹掉看板）。"""
    from repositories.curriculum_repo import CurriculumRepo
    return CurriculumRepo(conn).list_proposals(status=status, limit=limit)
