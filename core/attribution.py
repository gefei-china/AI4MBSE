# -*- coding: utf-8 -*-
"""P1-2 步骤级归因器：failure_kind + usage_stats 联合标注步骤 GOOD/BAD。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P1-2）
═══════════════════════════════════════════════════════════════
AgentEvolver（通义 2025-11 开源）三机制里的 Self-Attributing：**逐步 GOOD/BAD
归因**，过程+结果双通道奖励，训练步数 −55~67%。本系统的现状是「归因只到会话、
不到步骤」：`routers/monitor.py` 的 error_profile 是**计数**（该会话失败了几次、
权重几分），回答不了「**哪一步**开始坏的、坏在哪」——而步骤级原料早已全部落库：
llm_usage_stats.call_failed/failure_kind（10593 行里 269 条失败带 7 类 kind）、
tool_call_logs.ok/failure_kind（1419 行里 273 条失败）、flow_run_steps.status、
外加结局证据 agent_tasks/orch_checkpoints/flow_runs。缺的只是归因器。

═══════════════════════════════════════════════════════════════
设计纪律（与 core/badcase、core/lifecycle、P1-1 trace2dataset 同构）
═══════════════════════════════════════════════════════════════
1. 步骤源注册表：新证据源 = 登记一个 StepSourceSpec（一条只读 SQL，统一输出
   列），引擎零改动。当前 3 个步骤源 = 3 类原子动作：
     llm_call   LLM 调用（call_failed=1 ⇒ BAD，failure_kind 即损坏类型）
     tool_call  工具调用（ok=0 ⇒ BAD；kind 历史为空，见下条）
     flow_step  流水线节点（status ∈ 坏状态集 ⇒ BAD；kind 取 status 本身）
2. **步骤 ≠ 任务**：agent_tasks / orch_checkpoints / flow_runs 是**结局证据**
   （verdict 输入），不是步骤——把失败任务也当步骤会双重归因（任务坏通常只是
   其内部某一步坏的镜像）。
3. 首坏步 + 级联：只把结局归因到**最早偏离**的步骤；其后 BAD 步标记为级联
   （cascade=症状非原因）。patterns 聚合**只聚首坏步**——否则一次根因会被
   它的级联效应放大成 N 个"高频问题"。
4. 三态 verdict，不掩饰证据缺口：
     clean               无坏步骤、无坏结局
     attributed          有坏步骤（可归因到首坏步）
     unattributed_failure 结局坏但零坏步骤 —— instrumentation 缺口（失败发生在
                          已埋点的步骤之外，或步骤根本没记），如实呈现。
5. kind 覆盖率诚实统计：tool_call_logs.failure_kind 列存在但**历史全空**
   （写入点曾把分类器嵌套调用两次，内层返回的 kind 串被外层 ok=True 吃掉，
   2026-10-18 随本模块修复，见 agent/pipeline_parts/tools.py）。归因器对空
   kind 记 'unknown' 并统计 known/unknown 覆盖率——消费方（P1-3 提案器）
   必须知道哪些归因有类型、哪些只有位置。
6. 配置驱动：取材/聚合口径走 core.config 的 attribution 组（六键），模块内
   不写死阈值；门禁用变异自证口径真被消费。
7. 只读：本模块**不含任何写语句**（门禁静态断言）；所有查询按 conversation_id
   参数化。

与 P1-1 的关系：trace2dataset 是**供料环**（生产证据 → 评测集），本模块是
**诊断环**（哪一步坏 → 首坏步模式）；P1-3 配置提案器消费 collect_patterns
的输出提议 prompt/skill 修订（只提议不自动应用）。

时间口径：SQLite created_at 存 UTC，窗口 cutoff 按本地时区算（同 P1-1），
90 天窗口下 ≤14h 的时区 skew 无关紧要；窗口过滤在引擎侧做（SQL 常量 + Python
比较），同秒并列的确定性次序见 SOURCE_RANK。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

# ★ 2026-10-29：归因取材必须排除探针/验证噪声。
#   为什么在这里不可省：归因 → 首坏步模式 → config_proposal → 配置提案，
#   是一条**决策链**。探针一旦混入取材，`fake_tool`（126 次 / 112 失败）
#   会被识别成"高频首坏步"，进而产出针对假工具的配置提案。
#   实测（不过滤时）：tool_call 来源里 fake_tool 稳居 actor 首位。
#   口径真源：core/probe_mark.probe_where()（本仓已有，勿另写一份）。
from core.probe_mark import probe_where as _probe_where

#: 拼在 `WHERE ...` 之后的探针过滤片段（空别名版）
_PROBE = _probe_where()

# ── 产品契约常量（面板/API/门禁三方共用，勿在调用处另写字面量）───────────
LABEL_GOOD = "GOOD"
LABEL_BAD = "BAD"

VERDICT_CLEAN = "clean"                 # 无坏步骤、无坏结局
VERDICT_ATTRIBUTED = "attributed"       # 有坏步骤（可归因到首坏步）
VERDICT_UNATTRIBUTED = "unattributed_failure"   # 结局坏但零坏步骤（证据缺口）

VERDICTS = (VERDICT_CLEAN, VERDICT_ATTRIBUTED, VERDICT_UNATTRIBUTED)

#: 同秒并列时的确定性次序。SQLite created_at 只到秒、跨表 id 不可比 ⇒ 并列
#: 时按源次序（llm → tool → flow）打破平局。**声明即契约**：门禁断言排序纯
#: 函数确定；并列全坏时"谁是第一"本无额外信息，确定性比选错更不可接受。
SOURCE_RANK = {"llm_call": 0, "tool_call": 1, "flow_step": 2}

#: flow_step 的"好"状态集（显式枚举）。不在好集也不在坏集的状态 ⇒ 判 GOOD
#: 但打 unknown_status 信号——新状态词出现时不许静默混过（覆盖率可见）。
_FLOW_STEP_OK = ("done", "completed", "success", "skipped")
_FLOW_STEP_BAD = ("failed", "error", "timeout", "aborted", "interrupted")

#: 结局证据的"坏"状态集（各表实测词汇表见门禁 D 段；新状态出现时改这里）
_TASK_BAD = ("failed", "error")              # agent_tasks（真库：done/failed）
_CKPT_BAD = ("failed",)                      # orch_checkpoints（done/executing/failed）
_FLOW_RUN_BAD = ("failed", "partial", "error", "aborted")   # flow_runs（completed/running）

#: 配置默认值（与 core.config DEFAULT_CONFIG.attribution 一致；此处仅兜底，
#: 正常路径全部走配置——门禁变异自证）
_SETTINGS_DEFAULTS = {
    "enabled": True,
    "window_days": 90,
    "min_occurrences": 2,
    "max_patterns": 50,
    "max_steps": 500,
    "session_scan_limit": 200,
}


def _in_clause(values) -> str:
    """模块常量 → SQL IN 字面量（值全部来自本文件常量，非用户输入）。"""
    return "(" + ",".join("'%s'" % v for v in values) + ")"


# ── 步骤源注册表 ──────────────────────────────────────────────────────
# 每个源是一条只读 SQL，统一输出九列（引擎零特判，新源 = 登记一条）：
#   step_key   源内稳定键（'llm_call:10293'，溯源 + 排序平局裁决）
#   at         created_at（判据面时间；flow_step 继承其 run 的开始时间）
#   ok         1=GOOD / 0=BAD（SQL 侧判完，Python 不再猜）
#   kind       失败类型（llm=failure_kind；tool=failure_kind 或空；flow=status）
#   actor      执行体（provider/model、tool_name、node_label）
#   intent     意图（flow_step 无意图列，留空）
#   run_id     编排运行（归到某次编排的子分组键）
#   task_key   子任务键（llm=sub_task_key；tool=task_key）
#   meta_json  抽取器自定义元数据（SQL 内 json_object 构造）


@dataclass(frozen=True)
class StepSourceSpec:
    name: str
    label: str
    table: str
    sql: str          # 按 conversation_id 取材（完整取证，不过窗口）
    enum_sql: str     # 窗口内会话枚举（patterns 用；返回 cid, last_at）
    desc: str


_SPECS = [
    StepSourceSpec(
        name="llm_call", label="LLM 调用", table="llm_usage_stats",
        desc="LLM 调用步：call_failed=1 ⇒ BAD，failure_kind 即损坏类型"
             "（真库 269 条失败：other/balance/quota/conn/http_4xx/timeout/no_api_key）",
        sql="""
            SELECT 'llm_call:' || id AS step_key, created_at AS at,
                   CASE WHEN COALESCE(call_failed, 0) = 1 THEN 0 ELSE 1 END AS ok,
                   COALESCE(failure_kind, '') AS kind,
                   COALESCE(provider_name, '') || '/' || COALESCE(model_name, '') AS actor,
                   COALESCE(intent, '') AS intent,
                   COALESCE(run_id, 0) AS run_id,
                   COALESCE(sub_task_key, '') AS task_key,
                   json_object('failure_msg', substr(COALESCE(failure_msg, ''), 1, 200),
                               'finish_reason', COALESCE(finish_reason, ''),
                               'retry_count', COALESCE(retry_count, 0),
                               'fallback_used', COALESCE(fallback_used, 0),
                               'used_mock', COALESCE(used_mock, 0),
                               'latency_ms', COALESCE(latency_ms, 0)) AS meta_json
            FROM llm_usage_stats WHERE conversation_id = ?""" + _PROBE + """
        """,
        enum_sql="""
            SELECT conversation_id AS cid, MAX(created_at) AS last_at
            FROM llm_usage_stats
            WHERE conversation_id > 0 AND created_at >= ?""" + _PROBE + """
            GROUP BY conversation_id
        """),
    StepSourceSpec(
        name="tool_call", label="工具调用", table="tool_call_logs",
        desc="工具调用步：ok=0 ⇒ BAD。⚠️ failure_kind 历史全空（写入点双重调用 "
             "bug，2026-10-18 修复后新失败才有类型）——空 kind 记 unknown 并计入 "
             "覆盖率，不许伪装成有类型",
        sql="""
            SELECT 'tool_call:' || id AS step_key, created_at AS at,
                   COALESCE(ok, 1) AS ok,
                   COALESCE(failure_kind, '') AS kind,
                   COALESCE(tool_name, '') AS actor,
                   COALESCE(intent, '') AS intent,
                   COALESCE(run_id, 0) AS run_id,
                   COALESCE(task_key, '') AS task_key,
                   json_object('call_kind', COALESCE(call_kind, ''),
                               'tool_type', COALESCE(tool_type, ''),
                               'latency_ms', COALESCE(latency_ms, 0),
                               'result_head', substr(COALESCE(result, ''), 1, 200)) AS meta_json
            FROM tool_call_logs WHERE conversation_id = ?""" + _PROBE + """
        """,
        enum_sql="""
            SELECT conversation_id AS cid, MAX(created_at) AS last_at
            FROM tool_call_logs
            WHERE conversation_id > 0 AND created_at >= ?""" + _PROBE + """
            GROUP BY conversation_id
        """),
    StepSourceSpec(
        name="flow_step", label="流水线节点", table="flow_run_steps",
        desc="流水线节点步：status ∈ 坏状态集 ⇒ BAD（kind 取 status 本身）； "
             "at 继承 flow_runs.created_at（节点无时间列，粒度是 run 不是 node）； "
             "经 flow_runs.conversation_id 挂到会话（真库当前 0 条挂接，声明覆盖）",
        sql="""
            SELECT 'flow_step:' || s.id AS step_key,
                   COALESCE(r.created_at, '') AS at,
                   CASE WHEN s.status IN %s THEN 0 ELSE 1 END AS ok,
                   CASE WHEN s.status IN %s THEN COALESCE(s.status, '') ELSE '' END AS kind,
                   COALESCE(NULLIF(s.node_label, ''), s.node_id, '') AS actor,
                   '' AS intent,
                   COALESCE(s.run_id, 0) AS run_id,
                   '' AS task_key,
                   json_object('node_type', COALESCE(s.node_type, ''),
                               'status', COALESCE(s.status, ''),
                               'seq', COALESCE(s.seq, 0),
                               'latency_ms', COALESCE(s.latency_ms, 0)) AS meta_json
            FROM flow_run_steps s JOIN flow_runs r ON r.id = s.run_id
            WHERE r.conversation_id = ?
        """ % (_in_clause(_FLOW_STEP_BAD), _in_clause(_FLOW_STEP_BAD)),
        enum_sql="""
            SELECT r.conversation_id AS cid, MAX(r.created_at) AS last_at
            FROM flow_run_steps s JOIN flow_runs r ON r.id = s.run_id
            WHERE r.conversation_id > 0 AND r.created_at >= ?
            GROUP BY r.conversation_id
        """),
]


# ── 结局证据（verdict 输入，不是步骤——避免双重归因）────────────────────
@dataclass(frozen=True)
class OutcomeSpec:
    name: str
    sql: str
    desc: str


_OUTCOME_SPECS = [
    OutcomeSpec("agent_task",
                "SELECT COUNT(*) AS n FROM agent_tasks WHERE conversation_id = ? "
                "AND status IN %s" % _in_clause(_TASK_BAD),
                "编排子任务失败终态（done/failed 实测；failed/error 计入）"),
    OutcomeSpec("orch_run",
                "SELECT COUNT(*) AS n FROM orch_checkpoints WHERE conversation_id = ? "
                "AND phase IN %s" % _in_clause(_CKPT_BAD),
                "编排运行 failed 终态（done/executing/failed 实测）"),
    OutcomeSpec("flow_run",
                "SELECT COUNT(*) AS n FROM flow_runs WHERE conversation_id = ? "
                "AND status IN %s" % _in_clause(_FLOW_RUN_BAD),
                "流水线运行非成功终态（completed/running 实测；partial 计入）"),
]


# ── 配置（core.config attribution 组；默认值与 DEFAULT_CONFIG 双登记）──
def get_settings() -> dict:
    """取材/聚合口径。缺配置时回落默认值（与 DEFAULT_CONFIG 一致，不是第二套真值）。"""
    from core import config as _config
    out = {}
    for key, default in _SETTINGS_DEFAULTS.items():
        if key == "enabled":
            out[key] = _config.as_bool("attribution", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("attribution", key, default))
        except Exception:
            out[key] = default
    return out


def _marker_signals(meta: dict) -> dict:
    """步骤元数据 → 质量信号（**不翻标签**：mock/截断/重试/回落是信号不是失败）。"""
    sig = {}
    if meta.get("used_mock"):
        sig["mock"] = True
    if meta.get("finish_reason") == "length":
        sig["truncated"] = True
    if int(meta.get("retry_count") or 0) > 0:
        sig["retried"] = True
    if meta.get("fallback_used"):
        sig["fallback"] = True
    st = meta.get("status")
    if st and st not in _FLOW_STEP_OK and st not in _FLOW_STEP_BAD:
        sig["unknown_status"] = True      # 新状态词：判 GOOD 但不静默
    return sig


def _row_to_step(row) -> dict:
    """统一列 → 步骤 dict（加 label/source/source_id/signals，纯转换）。"""
    d = dict(row)
    key = str(d.get("step_key") or "")
    d["step_key"] = key          # 保留下钻标识（patterns 的 sample_steps 要溯源到源行）
    src, _, sid = key.partition(":")
    d["source"] = src
    try:
        d["source_id"] = int(sid)
    except (TypeError, ValueError):
        d["source_id"] = 0
    d["ok"] = bool(d.get("ok"))
    d["label"] = LABEL_BAD if not d["ok"] else LABEL_GOOD
    d["kind"] = (d.get("kind") or "").strip()
    d["at"] = d.get("at") or ""
    d["actor"] = d.get("actor") or ""
    d["intent"] = d.get("intent") or ""
    try:
        d["run_id"] = int(d.get("run_id") or 0)
    except (TypeError, ValueError):
        d["run_id"] = 0
    d["task_key"] = d.get("task_key") or ""
    meta = d.pop("meta_json", None)
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except Exception:
            meta = {"raw": meta[:200]}
    d["meta"] = meta if isinstance(meta, dict) else {}
    d["signals"] = _marker_signals(d["meta"])
    return d


def _in_window(at: str, cutoff: str) -> bool:
    """证据时间 ≥ 窗口下沿。空时间戳视为窗外（无法证明新鲜的证据不取，同 P1-1）。"""
    return bool(at) and at >= cutoff


def _cutoff(window_days) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - int(window_days) * 86400))


# ── 纯函数（可矩阵验证；门禁 A 段逐条打）──────────────────────────────
def order_steps(steps) -> list:
    """(at, source_rank, source_id) 字典序。同秒并列的平局裁决见 SOURCE_RANK。"""
    return sorted(steps, key=lambda s: (s.get("at") or "",
                                        SOURCE_RANK.get(s.get("source") or "", 99),
                                        int(s.get("source_id") or 0)))


def attribute_steps(steps) -> tuple:
    """有序步骤 → (带 seq/is_first_bad/after_first_bad 标记的副本, 首坏 seq)。

    首坏步 = 序列里第一个 BAD；其后所有 BAD 都是级联（after_first_bad）——
    级联是根因的镜像，聚合时必须分开计数，否则一次根因被放大成 N 个问题。
    """
    out, first_bad = [], None
    for i, s in enumerate(steps):
        s = dict(s)
        s["seq"] = i + 1
        if first_bad is None and s.get("label") == LABEL_BAD:
            first_bad = s["seq"]
        out.append(s)
    for s in out:
        s["is_first_bad"] = (s["seq"] == first_bad)
        # 级联标记只打 BAD 步（docstring 契约：其后所有 BAD 都是级联）。
        # GOOD 步即使发生在首坏之后也不打——2026-10-19 门禁 A5 实证修正：
        # 原实现对 seq>first_bad 的全部步骤打标（含 GOOD），消费方
        # _tally_steps 只在 BAD 分支读取才未出错，但标记语义与文档漂移。
        s["after_first_bad"] = (first_bad is not None and s["seq"] > first_bad
                                and s.get("label") == LABEL_BAD)
    return out, first_bad


def verdict_of(bad_count: int, outcome_bad: int) -> str:
    """三态 verdict：坏步骤可归因；只有坏结局 ⇒ 证据缺口，不许伪装成 clean。"""
    if bad_count > 0:
        return VERDICT_ATTRIBUTED
    if outcome_bad > 0:
        return VERDICT_UNATTRIBUTED
    return VERDICT_CLEAN


def _tally_steps(steps) -> dict:
    c = {"total": len(steps), "good": 0, "bad": 0, "bad_first": 0, "bad_cascade": 0,
         "mock": 0, "truncated": 0, "retried": 0, "fallback": 0, "unknown_status": 0}
    for s in steps:
        if s.get("label") == LABEL_BAD:
            c["bad"] += 1
            if s.get("is_first_bad"):
                c["bad_first"] += 1
            elif s.get("after_first_bad"):
                c["bad_cascade"] += 1
        else:
            c["good"] += 1
        for k in ("mock", "truncated", "retried", "fallback", "unknown_status"):
            if s.get("signals", {}).get(k):
                c[k] += 1
    return c


# ── 取材（只读） ──────────────────────────────────────────────────────
def _fetch_steps(conn, conversation_id, cutoff: Optional[str] = None) -> tuple:
    """按会话取步骤（cutoff 非空时只留窗口内）。缺表源记 skipped，不连坐。"""
    steps, skipped = [], {}
    for spec in _SPECS:
        try:
            rows = conn.execute(spec.sql, (int(conversation_id),)).fetchall()
        except sqlite3.Error as e:
            skipped[spec.name] = "%s: %s" % (type(e).__name__, e)
            continue
        for r in rows:
            s = _row_to_step(r)
            if cutoff and not _in_window(s["at"], cutoff):
                continue
            steps.append(s)
    return steps, skipped


def _outcome_evidence(conn, conversation_id) -> dict:
    """结局证据：各源坏终态计数（缺表记 0 不炸——老库没有 orch_checkpoints 也跑）。"""
    out = {}
    for spec in _OUTCOME_SPECS:
        try:
            out[spec.name] = int(conn.execute(spec.sql, (int(conversation_id),)).fetchone()[0])
        except sqlite3.Error:
            out[spec.name] = 0
    return out


def attribute_session(conn, conversation_id, *, settings: Optional[dict] = None) -> dict:
    """单会话步骤级归因（完整取证：不过窗口——排查时要看全部历史）。

    max_steps 截断取舍：超限时保留**最早**的 max_steps 条。首坏步在序列头部，
    被丢的尾部是级联；截断事实写进结果（truncated=True + dropped=n），
    排查者知道"后面还有但没看"。
    """
    cfg = settings or get_settings()
    raw, skipped = _fetch_steps(conn, conversation_id)
    dropped = 0
    if len(raw) > int(cfg["max_steps"]):
        ordered = order_steps(raw)
        raw, dropped = ordered[:int(cfg["max_steps"])], len(ordered) - int(cfg["max_steps"])
    steps, first_bad_seq = attribute_steps(order_steps(raw))
    counts = _tally_steps(steps)
    outcome = _outcome_evidence(conn, conversation_id)
    outcome_bad = sum(outcome.values())
    first_bad = next((s for s in steps if s.get("is_first_bad")), None)
    return {
        "conversation_id": int(conversation_id),
        "steps": steps,
        "counts": counts,
        "first_bad": first_bad,
        "first_bad_seq": first_bad_seq,
        "outcome": outcome,
        "outcome_bad": outcome_bad,
        "verdict": verdict_of(counts["bad"], outcome_bad),
        "truncated": dropped > 0,
        "dropped_steps": dropped,
        "skipped_sources": skipped,
    }


def collect_patterns(conn, *, settings: Optional[dict] = None) -> dict:
    """跨会话首坏步模式聚合（P1-3 提案器的读取面 + 监控看板）。

    口径：
    - 只聚合**首坏步**（级联是症状，见 attribute_steps）；
    - 模式键 = (source, intent, kind, actor)——kind 为空的工具失败聚成
      unknown 模式，覆盖率单独统计，消费方据此判断置信度；
    - unattributed_sessions：结局坏但零坏步骤的会话——instrumentation 缺口，
      监控要可见（失败却没被任何埋点看见，比失败本身更危险）。
    """
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"enabled": False, "first_bad_patterns": [], "kind_coverage": {},
                "sessions_scanned": 0}
    cutoff = _cutoff(cfg["window_days"])
    # 1. 窗口内会话枚举（每源一条 GROUP BY；索引 created_at 可用）
    cids = {}
    for spec in _SPECS:
        try:
            for r in conn.execute(spec.enum_sql, (cutoff,)):
                cid = int(r["cid"])
                last = r["last_at"] or ""
                if cid not in cids or last > cids[cid]:
                    cids[cid] = last
        except sqlite3.Error:
            continue
    # 2. 最近活跃优先，scan_limit 封顶（防全表回放）
    ordered_cids = sorted(cids, key=lambda c: (cids[c], c), reverse=True)[:int(cfg["session_scan_limit"])]
    patterns, kind_stat = {}, {"bad_steps": 0, "known_kind": 0, "unknown_kind": 0}
    totals = {"total": 0, "good": 0, "bad": 0, "bad_first": 0, "bad_cascade": 0}
    scanned = with_bad = 0
    unattributed, skipped_all = [], {}
    for cid in ordered_cids:
        steps, skipped = _fetch_steps(conn, cid, cutoff=cutoff)
        skipped_all.update(skipped)
        if not steps:
            continue
        scanned += 1
        steps, _fb = attribute_steps(order_steps(steps))
        counts = _tally_steps(steps)
        for k in ("total", "good", "bad", "bad_first", "bad_cascade"):
            totals[k] += counts[k]
        for s in steps:
            if s.get("label") != LABEL_BAD:
                continue
            kind_stat["bad_steps"] += 1
            if s["kind"]:
                kind_stat["known_kind"] += 1
            else:
                kind_stat["unknown_kind"] += 1
            if not s.get("is_first_bad"):
                continue
            with_bad += 1
            key = (s["source"], s["intent"], s["kind"], s["actor"])
            p = patterns.setdefault(key, {"source": s["source"], "intent": s["intent"],
                                          "kind": s["kind"], "actor": s["actor"],
                                          "count": 0, "conversations": set(),
                                          "first_seen": "", "last_seen": "",
                                          "sample_steps": []})
            p["count"] += 1
            p["conversations"].add(cid)
            p["first_seen"] = min(p["first_seen"] or s["at"], s["at"])
            p["last_seen"] = max(p["last_seen"], s["at"])
            if len(p["sample_steps"]) < 3:
                p["sample_steps"].append(s["step_key"])
        # 无坏步骤的会话才需要查结局（ unattributed 判据）；坏步骤会话已可归因
        if counts["bad"] == 0:
            outcome_bad = sum(_outcome_evidence(conn, cid).values())
            if outcome_bad > 0:
                unattributed.append(cid)
    known_pct = (round(100.0 * kind_stat["known_kind"] / kind_stat["bad_steps"], 1)
                 if kind_stat["bad_steps"] else None)
    out_patterns = sorted(patterns.values(),
                          key=lambda p: (-p["count"], p["source"], p["actor"]))
    out_patterns = [dict(p, conversations=len(p["conversations"]))
                    for p in out_patterns
                    if p["count"] >= int(cfg["min_occurrences"])][:int(cfg["max_patterns"])]
    return {
        "enabled": True,
        "window_cutoff": cutoff,
        "sessions_scanned": scanned,
        "sessions_with_first_bad": with_bad,
        "first_bad_patterns": out_patterns,
        "kind_coverage": dict(kind_stat, known_pct=known_pct),
        "label_totals": totals,
        "unattributed_sessions": {"count": len(unattributed),
                                  "samples": unattributed[:10]},
        "skipped_sources": skipped_all,
    }
