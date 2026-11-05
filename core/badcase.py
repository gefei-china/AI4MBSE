# -*- coding: utf-8 -*-
"""P0-3（2026-10-18《自进化 AI 平台调研评估》整改第三项）：Bad Case 收集器。

闭环的中间环：**监控（已有判据面）→ Bad Case 台账 → 告警（alert_rules）**。
- 上游判据面：llm_usage_stats（call_failed/finish_reason='length'）与 feedback
  （modify/wrong/corrected 负向反馈）——都是既有真实数据，不新造采集管道；
- 下游告警：`badcase_open_count` 指标（core/alert_evaluator.GLOBAL_METRICS），
  复用既有 alert_rules/alert_events，**不造第二套告警机制**。

设计纪律（与 lifecycle / asset_audit 一致）：
- 检测器注册表：新判据面 = 注册一个 SQL（统一输出列），引擎零改动；
- 收集**默认 dry-run**（apply=False 不动库）——自进化「只建议不自动执行」，
  入库必须经人工确认（POST /api/monitor/badcases/collect {apply:true}）；
- 去重累加：同 kind 同 subject 复现只 bump occurrences（唯一索引兜底），
  不插新行；重复收集同一窗口不双计（只认 last_seen_at 之后的新事件）；
- 阈值/窗口全部走 core.config badcase 组，无模块内硬编码常量。
"""
from dataclasses import dataclass
from typing import Callable, List, Optional

from core.statemachine import get as _sm_get
# 反馈词表单一真源（2026-10-29）：负反馈类型/严重度**不在本模块另存一份**。
from core.feedback_types import (
    NEGATIVE_QUALITY,
    NEGATIVE_REVIEW_TYPES,
    NEGATIVE_SEVERITY,
)
# 告警状态单一真源（2026-10-29）：未终结状态的取值域同样不在本模块另写一份。
from core.alert_state import open_statuses_sql

# #18 统一状态机：bad_case 三态机的唯一定义在 core/statemachine.py，
# 本文件的所有 status 写入（triage 人工流转 / 复现重开）都必须过它校验 ——
# 包括「人工结论」：resolve/ignore 之后只有 reopen 边能回 open，不存在绕过。
_BC = _sm_get("bad_case")

# ── 检测器注册表 ────────────────────────────────────────────────
# 每个检测器是一条只读 SQL，统一输出五列：
#   src_id   源行 id（llm_usage_stats.id / feedback.id）——事件水位的一半
#   subject  去重主体（同 kind 同 subject 视为同一坏样本主题）
#   seen_at  事件时间（判据面时间，非收集时间）
#   title    一句话摘要（人读）
#   detail   证据原文（入库时按 badcase.max_detail_chars 截断）
# severity 由 spec 声明（triage 可改）。
# ⚠️ 为什么需要 src_id：同一次会话的两次失败可能落在**同一秒**（burst），
#   只靠时间戳的严格大于会把它们判为「已计过」而漏计——新鲜度判据必须是
#   (seen_at, src_id) 二元组（源行 id 表内唯一，天然有序）。
@dataclass(frozen=True)
class DetectorSpec:
    kind: str
    label: str
    severity: str
    sql: str


def _sql_llm_failed(include_probes: bool) -> str:
    probe = "" if include_probes else "AND (conversation_id > 0 OR run_id > 0)"
    return f"""
        SELECT id AS src_id,
               CASE WHEN conversation_id > 0 THEN 'conv:' || conversation_id
                    WHEN run_id > 0 THEN 'run:' || run_id
                    ELSE 'stat:' || id END AS subject,
               created_at AS seen_at,
               'LLM 调用失败·' || COALESCE(NULLIF(failure_kind, ''), 'unknown') AS title,
               COALESCE(NULLIF(failure_msg, ''), '(无失败信息)') AS detail
        FROM llm_usage_stats
        WHERE call_failed = 1 AND created_at >= datetime('now', ?) {probe}
    """


def _sql_llm_truncated(include_probes: bool) -> str:
    probe = "" if include_probes else "AND (conversation_id > 0 OR run_id > 0)"
    return f"""
        SELECT id AS src_id,
               CASE WHEN conversation_id > 0 THEN 'conv:' || conversation_id
                    ELSE 'stat:' || id END AS subject,
               created_at AS seen_at,
               '输出被截断（finish_reason=length）' AS title,
               '模型 ' || COALESCE(model_name, '?') || ' 输出达到长度上限被截断' AS detail
        FROM llm_usage_stats
        WHERE finish_reason = 'length' AND created_at >= datetime('now', ?) {probe}
    """


_SQL_FEEDBACK = """
    SELECT id AS src_id,
           'msg:' || message_id AS subject,
           created_at AS seen_at,
           '用户负向反馈·' || COALESCE(NULLIF(quality, ''), feedback_type) AS title,
           COALESCE(NULLIF(context, ''), '(无补充说明)') AS detail
    FROM feedback
    WHERE (feedback_type IN ({review_types})
           OR COALESCE(quality, '') IN ({quality_types}))
      AND created_at >= datetime('now', ?)
""".format(
    review_types=",".join("'%s'" % x for x in NEGATIVE_REVIEW_TYPES),
    quality_types=",".join("'%s'" % x for x in NEGATIVE_QUALITY),
)

# 负向反馈分级：wrong/corrected 是明确的质量证词 ⇒ high；modify / down ⇒ medium
# ⚠️ 2026-10-29：词表改为从单一真源取（core/feedback_types.py），不再在本文件另写一份
#    ——原先本文件与 core/online_eval.py 各存一份 NEGATIVE 词表，两份必然漂移。
_FB_SEVERITY = dict(NEGATIVE_SEVERITY)

# ── 告警未处置（2026-10-29）──
# 动机：生产库 328 条告警 status **全是 'open'**，一条没被处理；其中
#   「BadCase 积压（open≥5）」这条规则本身在报"坏例没人处理"——
#   **告警只在响，没有变成可追踪的待办**。本判据把未处置告警纳入坏例主题，
#   于是"该修什么"有地方可查。
# 取未终结状态（open/acked/triaged）：fixed/verified 视为已闭环，不再是坏例。
_SQL_ALERT = """
    SELECT id AS src_id,
           'alert:' || rule_id AS subject,
           COALESCE(NULLIF(last_seen_at, ''), created_at) AS seen_at,
           '告警未处置·' || rule_name AS title,
           '指标 ' || metric || ' 实测 ' || actual || '（判据 ' || operator || ' ' || threshold || '），'
               || '已命中 ' || COALESCE(hit_count, 1) || ' 次' AS detail
    FROM alert_events
    WHERE level IN ('critical', 'error', 'high')
      AND status IN ({open_statuses})
      AND COALESCE(created_at, '') >= datetime('now', ?)
""".format(open_statuses=open_statuses_sql())


def _detectors(include_probes: bool) -> List[DetectorSpec]:
    """判据面清单。新来源（如工具执行失败、评审驳回）在此注册即可，引擎不改。"""
    return [
        DetectorSpec("llm_call_failed", "LLM 调用失败", "high",
                     _sql_llm_failed(include_probes)),
        DetectorSpec("user_negative_feedback", "用户负向反馈", "medium",
                     _SQL_FEEDBACK),
        DetectorSpec("llm_truncated", "输出截断", "low",
                     _sql_llm_truncated(include_probes)),
        DetectorSpec("alert_unhandled", "告警未处置", "high", _SQL_ALERT),
    ]


def _row_get(row, key, default=None):
    """sqlite3.Row / dict / tuple 三态取值（ BaseRepo 与裸连接混用时的兼容层）。"""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[key] if key in keys else default


# ── 配置（core.config badcase 组，禁模块内常量旁路）──────────────
_SETTING_DEFAULTS = {
    "enabled": True,             # 总开关（False 时收集端点返空而非 500）
    "window_days": 30,           # 判据窗口：只扫最近 N 天的坏样本
    "include_probes": False,     # 探测调用（无会话且无 run）是否算坏样本
    "max_detail_chars": 2000,    # 单条 evidence 截断上限
    "reopen_on_recur": True,     # 已 triage 的主题复现时是否重开（告警闭环需要）
}


def get_settings() -> dict:
    """读 badcase 组配置（门禁用 monkeypatch core.config 变异自证真被消费）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTING_DEFAULTS.items():
        if isinstance(default, bool):
            out[key] = _config.as_bool("badcase", key, default)
            continue
        try:
            out[key] = type(default)(_config.get("badcase", key, default))
        except Exception:
            out[key] = default
    return out


def _table_exists(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bad_cases'").fetchone())


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 12] + f"…[{len(s)} chars]"


def collect_bad_cases(conn, *, apply: bool = False, kinds: Optional[List[str]] = None,
                      settings: Optional[dict] = None) -> dict:
    """扫描判据面 → 候选坏样本。

    默认 dry-run（apply=False）：**不写任何库**，只返回候选与「新建/复现」预判，
    供人工确认后再次调用 apply=True 入库（人工关口）。
    返回 {ok, enabled, window_days, dry_run, candidates, plan, summary, generated_at}。
    """
    from datetime import datetime
    cfg = settings or get_settings()
    out = {"ok": True, "enabled": bool(cfg.get("enabled")),
           "window_days": cfg.get("window_days"), "dry_run": not apply,
           "candidates": [], "plan": {"new": 0, "recur": 0, "stale": 0, "reopen": 0},
           "summary": {"by_kind": {}, "total_events": 0}, "generated_at": "",
           # ★ 2026-10-29：判据面级失败**显式上报**（与 online_eval 同口径）。
           #   非空 ⇒ 至少有一个判据面瘫了，"坏例变少"是假象，不可当"变好了"。
           "failures": []}
    if not out["enabled"]:
        return out
    want = [d for d in _detectors(bool(cfg.get("include_probes")))
            if not kinds or d.kind in (kinds or [])]
    window_arg = ("-" + str(int(cfg.get("window_days") or 30)) + " day",)
    cands = []   # [{kind,label,severity,subject,title,detail,events:[seen_at...]}]
    for spec in want:
        try:
            rows = conn.execute(spec.sql, window_arg).fetchall()
        except Exception as e:
            # ★ 2026-10-29：**不再静默跳过**。
            #   原实现 `except: continue` ⇒ 判据面 SQL 一旦写错（少表/少列/SQL 语法错），
            #   该检测器**无声消失**，看板上的坏例数凭空变少，看起来像"最近没有坏例"——
            #   实则整个判据面已经瘫了。这是"静默兜底把错误变成不可见"的典型。
            #   同仓 `core/online_eval.py` 早就立了规矩（模块头："源级异常记入不吞——
            #   静默跳过会让'采样器坏了'长得像'没有负反馈'"），此处此前与它不一致。
            #   现与它同口径：记入 failures，由上层（evolution_report / 端点）暴露。
            out["failures"].append("%s: %s: %s" % (spec.kind, type(e).__name__, str(e)[:160]))
            continue
        groups = {}
        for r in rows:
            subj = _row_get(r, "subject") or ""
            seen = _row_get(r, "seen_at") or ""
            src = int(_row_get(r, "src_id") or 0)
            g = groups.setdefault(subj, {"subject": subj, "title": "", "detail": "",
                                         "events": []})
            g["events"].append((seen, src))
            # 同主题多条事件：title/detail 取最新一条的（按 (seen_at, src_id) 排序取末位）
            if (seen, src) >= (g.get("_last") or ("", 0)):
                g["_last"] = (seen, src)
                g["title"] = _row_get(r, "title") or ""
                g["detail"] = _row_get(r, "detail") or ""
        for g in groups.values():
            g.pop("_last", None)
            evs = sorted(g["events"])
            g["first_seen_at"] = evs[0][0]
            g["last_seen_at"], g["last_src_id"] = evs[-1]
            g["kind"], g["label"] = spec.kind, spec.label
            if spec.kind == "user_negative_feedback":
                # 反馈标题形如「用户负向反馈·wrong」⇒ 按类型分级
                fb = (g["title"] or "").split("·")[-1]
                g["severity"] = _FB_SEVERITY.get(fb, spec.severity)
            else:
                g["severity"] = spec.severity
            cands.append(g)

    # 与库存对照（只读），预判 new / recur / stale / reopen
    existing = {}
    if _table_exists(conn):
        for r in conn.execute("SELECT kind, subject, occurrences, last_seen_at, last_src_id, status "
                              "FROM bad_cases").fetchall():
            existing[(r["kind"], r["subject"])] = (
                int(r["occurrences"] or 0), r["last_seen_at"] or "",
                int(r["last_src_id"] or 0), r["status"] or "open")

    plan = {"new": 0, "recur": 0, "stale": 0, "reopen": 0}
    for g in cands:
        key = (g["kind"], g["subject"])
        occ, last_seen, last_src, status = existing.get(key, (0, "", 0, "open"))
        # 新鲜度：(seen_at, src_id) 严格大于库存水位 —— 同秒 burst 不漏计
        fresh = [ev for ev in g["events"] if not last_seen or ev > (last_seen, last_src)]
        g["occurrences"] = len(fresh)
        g["detail"] = _clip(g["detail"], int(cfg.get("max_detail_chars") or 2000))
        if not existing.get(key):
            g["plan"] = "new"
            plan["new"] += 1
        elif not fresh:
            g["plan"] = "stale"
            plan["stale"] += 1
        else:
            # #18：重开判定过状态机（reopen 边存在才计划重开；库存状态被外部改脏时
            # 按 recur 处理只累加次数，不与 apply 的机检结论漂移）
            st = _BC.normalize(status)
            reopenable = st != "open" and "open" in _BC.targets(st)
            g["plan"] = "reopen" if (reopenable and cfg.get("reopen_on_recur")) else "recur"
            plan[g["plan"]] += 1
        out["summary"]["total_events"] += len(g["events"])

    out["candidates"] = sorted(cands, key=lambda x: (x["first_seen_at"], x["kind"]))
    out["plan"] = plan
    by_kind = {}
    for g in cands:
        k = by_kind.setdefault(g["kind"], {"label": g["label"], "subjects": 0, "events": 0})
        k["subjects"] += 1
        k["events"] += len(g["events"])
    out["summary"]["by_kind"] = by_kind
    out["generated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if apply:
        res = apply_bad_cases(conn, out["candidates"], settings=cfg)
        out["apply_result"] = res
    return out


def apply_bad_cases(conn, candidates: list, *, settings: Optional[dict] = None) -> dict:
    """把候选写入 bad_cases（同 kind 同 subject 只累加，不插新行）。

    幂等语义：仅入库 last_seen_at 之后的**新事件**（delta），重复收集同一窗口
    不会双计 occurrences。resolved/ignored 的主题复现时按 reopen_on_recur 重开。
    """
    cfg = settings or get_settings()
    if not _table_exists(conn):
        return {"ok": False, "error": "bad_cases 表不存在（迁移未执行）",
                "inserted": 0, "updated": 0}
    inserted, updated, reopened = 0, 0, 0
    for g in candidates:
        if not g.get("occurrences"):
            continue    # stale：窗口内无新事件
        row = conn.execute(
            "SELECT id, occurrences, first_seen_at, last_seen_at, last_src_id, status, resolved_by "
            "FROM bad_cases WHERE kind=? AND subject=?", (g["kind"], g["subject"])).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO bad_cases (kind, subject, title, detail, severity, occurrences, "
                "first_seen_at, last_seen_at, last_src_id, status, source) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'open', 'collector')",
                (g["kind"], g["subject"], g["title"], g["detail"], g["severity"],
                 g["occurrences"], g["first_seen_at"], g["last_seen_at"], g["last_src_id"]))
            inserted += 1
            continue
        status = row["status"] or "open"
        keep_by = row["resolved_by"] or ""
        # #18：重开判定与 collect 的 plan 分类**同源**（normalize + targets + 开关，
        # 见 collect_bad_cases 内同一算式）—— 库存状态不在状态域（表被外部改脏）时
        # 按 recur 处理：只累加次数，不覆盖一个自己不认识的 status，也不整批拒绝
        # 连坐无辜的新主题（脏行被机检冻结，triage 同样拒，直至人工修库）。
        st = _BC.normalize(status)
        if st != "open" and "open" in _BC.targets(st) and cfg.get("reopen_on_recur"):
            status, keep_by = "open", ""      # 复现重开：解决后又坏 ⇒ 重新进入告警视野
            reopened += 1
        first = min(row["first_seen_at"] or g["first_seen_at"], g["first_seen_at"])
        conn.execute(
            "UPDATE bad_cases SET occurrences=?, title=?, detail=?, severity=?, "
            "first_seen_at=?, last_seen_at=?, last_src_id=?, status=?, resolved_by=?, "
            "updated_at=datetime('now') WHERE id=?",
            (int(row["occurrences"] or 0) + g["occurrences"], g["title"], g["detail"],
             g["severity"], first, g["last_seen_at"], g["last_src_id"], status, keep_by, row["id"]))
        updated += 1
    conn.commit()
    return {"ok": True, "inserted": inserted, "updated": updated, "reopened": reopened}


def triage_bad_case(conn, case_id: int, status: str, actor: str = "", note: str = "") -> dict:
    """人工关口：open/resolved/ignored 三态流转（只有这里能改 status）。

    #18 统一状态机：成员校验 + 迁移边校验都走 bad_case 机（core/statemachine）——
    非法状态（如 done）拒；合法状态间的非法迁移（如 resolved→resolved 原地流转、
    ignored→ignored）同样拒：人工结论也不允许绕过状态机自说自话。
    """
    if status not in _BC.states:
        return {"ok": False, "error": f"非法状态: {status}（{'/'.join(_BC.states)}）"}
    if not _table_exists(conn):
        return {"ok": False, "error": "bad_cases 表不存在（迁移未执行）"}
    row = conn.execute("SELECT id, status FROM bad_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"Bad Case #{case_id} 不存在"}
    ok, err = _BC.check(row["status"] or "open", status)
    if not ok:
        return {"ok": False, "error": err}
    conn.execute(
        "UPDATE bad_cases SET status=?, resolved_by=?, updated_at=datetime('now') WHERE id=?",
        (status, actor if status != "open" else "", case_id))
    if note:
        conn.execute("UPDATE bad_cases SET detail=detail || ? WHERE id=?",
                     (f"\n[triage:{actor}] {note}", case_id))
    conn.commit()
    return {"ok": True, "id": case_id, "status": status}


def open_badcase_count(conn) -> int:
    """告警指标 badcase_open_count 的取数（缺表 ⇒ 0，不制造假告警）。"""
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM bad_cases WHERE status='open'").fetchone()[0] or 0)
    except Exception:
        return 0


def badcase_summary(conn) -> dict:
    """面板汇总：按状态 × 按 kind。缺表 ⇒ 全 0（老库降级）。"""
    out = {"open": 0, "resolved": 0, "ignored": 0, "by_kind": {}, "total": 0}
    if not _table_exists(conn):
        return out
    for r in conn.execute("SELECT status, COUNT(*) c FROM bad_cases GROUP BY status").fetchall():
        st = r["status"] or "open"
        out[st if st in out else "open"] = int(r["c"] or 0)
        out["total"] += int(r["c"] or 0)
    for r in conn.execute("SELECT kind, COUNT(*) c FROM bad_cases GROUP BY kind").fetchall():
        out["by_kind"][r["kind"]] = int(r["c"] or 0)
    return out
