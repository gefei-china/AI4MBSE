# -*- coding: utf-8 -*-
"""P1-1 Trace2Dataset（2026-10-18《自进化 AI 平台调研评估》路线图）：生产证据链 → 评测集。

═══════════════════════════════════════════════════════════════════
为什么需要它（AgentLoop 闭环的供料环，不是又造一个工具）
═══════════════════════════════════════════════════════════════════
行业闭环（阿里云 AgentLoop）原话：**Trace2Dataset：生产轨迹 → 评测集 → 优化 →
再部署**。本系统最强的环是「评估」（189 门禁 + tools/eval 19 件），但它们评估的
是**人的改动**与**检索语料**；生产轨迹（会话、HIL 裁决、意图样本、编排运行、
P0-3 Bad Case 台账）此前没有任何一条通路变成可复用的评测样本 —— 数据飞轮文献
警告的「第三个月停滞：不接回管道的飞轮只是数据坟墓」正是这个形态：llm_usage_stats
10593 条躺在表里，没有任何评估器消费它们。

═══════════════════════════════════════════════════════════════════
设计纪律（与 core/badcase 同构，注册表 + 配置驱动 + 只读）
═══════════════════════════════════════════════════════════════════
1. 抽取器注册表：新证据源 = 注册一个 ExtractorSpec（一条只读 SQL，统一输出列），
   引擎零改动。当前 8 个抽取器覆盖三类证据：
   - 人工裁决（强标注）：intent_confirmed / intent_rejected（标注员确认/驳回的
     意图样本）、hil_approved / hil_rejected（HIL 确认单的人工决定）；
     **pending 的 HIL 没有裁决 ⇒ 永不入集**（无标签的样本是噪声不是证据）；
   - 回归证据（弱标注）：conv_clean（无失败调用、无 open 坏样本、非 Mock 的
     问答对）、flow_completed（跑完的编排流）——「没坏过」不是「正确」的证明，
     消费方只能用它防回归，不能用它证明质量；
   - 已知失败（负样本）：conv_badcase（P0-3 台账里 open 的坏样本会话）、
     flow_failed（失败终态的运行）—— 修好了它才算进步。
2. 标注三档写进每条样本的 label 字段（strong/weak/negative），消费方必须能
   区别对待：把弱标注当强标注用，就是自己骗自己。
3. 窗口/上限/开关全部走 core.config 的 trace2dataset 组，无模块内硬编码阈值。
4. 只读连接取材，本模块**不含任何写语句**（门禁 E 段静态断言）。
5. 指纹纪律（沿用 tools/eval/fingerprint_gate 的教训）：trace 集自带
   trace_fingerprint 与 check_drift —— 生产数据每小时在变，拿旧 trace 集打分
   等于拿失效 gold 自欺；漂移时**拒绝**而不是警告。

产物：tmp/p21/trace_evalset.json（schema "p1-1/trace-evalset@1"）。
由 tools/eval/build_evalset.py 默认附带构建（--no-trace 可退回了旧行为）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# ── 抽取器注册表 ──────────────────────────────────────────────────
# 每个抽取器是一条只读 SQL，统一输出六列：
#   source_id  源行 id（溯源；指纹水位的一半）
#   query      用户侧输入（问句 / 待确认动作 / 流程名）
#   response   系统侧输出（负样本无 response，为空串）
#   gold       机器可判的期望（意图名 / 裁决 / 失败类型；回归集为空串）
#   seen_at    证据时间（判据面时间，非构建时间）
#   meta_json  抽取器自定义元数据（SQL 内 json_object 构造）
#
# kind:    positive（正样本）/ negative（已知失败）/ regression（回归证据）
# label:   strong（人工裁决）/ weak（机器派生）/ negative（已知失败）
# ⚠️ weak ≠ 正确：conv_clean 的全部证据是「没有坏证据」，它只能回答
#   「改动有没有把原来能用的场景改坏」，不能回答「系统有多好」。


@dataclass(frozen=True)
class ExtractorSpec:
    name: str
    kind: str
    label: str
    sql: str
    desc: str


_SPECS = [
    ExtractorSpec(
        name="intent_confirmed", kind="positive", label="strong",
        desc="标注员确认的意图样本（text → intent）：路由评测的强标注金标",
        sql="""
            SELECT id AS source_id, text AS query, '' AS response, intent AS gold,
                   COALESCE(updated_at, created_at) AS seen_at,
                   json_object('intent', intent, 'hit_intent', COALESCE(hit_intent, ''),
                               'hit_route', COALESCE(hit_route, ''),
                               'source', COALESCE(source, ''),
                               'seen_count', COALESCE(seen_count, 0)) AS meta_json
            FROM intent_samples WHERE status = 'confirmed'
        """),
    ExtractorSpec(
        name="intent_rejected", kind="negative", label="strong",
        desc="标注员驳回的意图样本（text → 曾被错认成的 intent）：路由负样本",
        sql="""
            SELECT id AS source_id, text AS query, '' AS response,
                   COALESCE(hit_intent, intent) AS gold,
                   COALESCE(updated_at, created_at) AS seen_at,
                   json_object('intent', intent, 'hit_intent', COALESCE(hit_intent, ''),
                               'note', COALESCE(note, '')) AS meta_json
            FROM intent_samples WHERE status = 'rejected'
        """),
    ExtractorSpec(
        name="hil_approved", kind="positive", label="strong",
        desc="HIL 确认单被人工批准（preview → approved）：动作放行的金标",
        sql="""
            SELECT id AS source_id,
                   COALESCE(NULLIF(preview, ''), action || ' @' || agent_id) AS query,
                   '' AS response, status AS gold,
                   COALESCE(decided_at, created_at) AS seen_at,
                   json_object('agent_id', agent_id, 'action', action,
                               'decided_by', COALESCE(decided_by, ''),
                               'conversation_id', conversation_id, 'run_id', run_id,
                               'payload_head', substr(COALESCE(payload, ''), 1, 200)) AS meta_json
            FROM hil_confirmations WHERE status = 'approved'
        """),
    ExtractorSpec(
        name="hil_rejected", kind="negative", label="strong",
        desc="HIL 确认单被人工驳回（preview → rejected）：动作拦截的金标",
        sql="""
            SELECT id AS source_id,
                   COALESCE(NULLIF(preview, ''), action || ' @' || agent_id) AS query,
                   '' AS response, status AS gold,
                   COALESCE(decided_at, created_at) AS seen_at,
                   json_object('agent_id', agent_id, 'action', action,
                               'decided_by', COALESCE(decided_by, ''),
                               'conversation_id', conversation_id, 'run_id', run_id,
                               'payload_head', substr(COALESCE(payload, ''), 1, 200)) AS meta_json
            FROM hil_confirmations WHERE status = 'rejected'
        """),
    ExtractorSpec(
        name="conv_clean", kind="regression", label="weak",
        desc="干净问答对（无失败调用/无 open 坏样本/非 Mock）：回归集，只防改坏",
        sql="""
            SELECT u.id AS source_id, u.content AS query,
                   (SELECT a.content FROM messages a
                     WHERE a.conversation_id = u.conversation_id
                       AND a.role = 'assistant' AND a.id > u.id
                     ORDER BY a.id LIMIT 1) AS response,
                   '' AS gold,
                   u.created_at AS seen_at,
                   json_object('conversation_id', u.conversation_id) AS meta_json
            FROM messages u
            WHERE u.role = 'user'
              AND NOT EXISTS (SELECT 1 FROM bad_cases b
                               WHERE b.subject = 'conv:' || u.conversation_id
                                 AND b.status = 'open')
              AND NOT EXISTS (SELECT 1 FROM llm_usage_stats s
                               WHERE s.conversation_id = u.conversation_id
                                 AND s.call_failed = 1)
            ORDER BY u.id
        """),
    ExtractorSpec(
        name="conv_badcase", kind="negative", label="negative",
        desc="P0-3 台账里 open 的坏样本会话的用户输入：已知失败，修好才算进步",
        sql="""
            SELECT b.id AS source_id, u.content AS query, '' AS response,
                   b.kind AS gold,
                   u.created_at AS seen_at,
                   json_object('conversation_id', u.conversation_id, 'badcase_id', b.id,
                               'severity', b.severity, 'occurrences', b.occurrences,
                               'badcase_title', b.title) AS meta_json
            FROM bad_cases b JOIN messages u
              ON u.conversation_id = CAST(SUBSTR(b.subject, 6) AS INTEGER)
             AND u.role = 'user'
            WHERE b.subject LIKE 'conv:%' AND b.status = 'open'
            ORDER BY b.id, u.id
        """),
    ExtractorSpec(
        name="flow_completed", kind="regression", label="weak",
        desc="跑完的编排运行（flow_name → 终态指标）：编排层回归证据",
        sql="""
            SELECT id AS source_id, flow_name AS query,
                   'latency_ms=' || COALESCE(total_latency_ms, 0)
                     || ', errors=' || COALESCE(error_count, 0) AS response,
                   status AS gold,
                   created_at AS seen_at,
                   json_object('flow_id', flow_id, 'conversation_id', conversation_id,
                               'order_head', substr(COALESCE(order_json, ''), 1, 200)) AS meta_json
            FROM flow_runs WHERE status = 'completed'
        """),
    ExtractorSpec(
        name="flow_failed", kind="negative", label="negative",
        desc="失败终态的编排运行：已知失败样本",
        sql="""
            SELECT id AS source_id, flow_name AS query,
                   'latency_ms=' || COALESCE(total_latency_ms, 0)
                     || ', errors=' || COALESCE(error_count, 0) AS response,
                   status AS gold,
                   created_at AS seen_at,
                   json_object('flow_id', flow_id, 'conversation_id', conversation_id,
                               'order_head', substr(COALESCE(order_json, ''), 1, 200)) AS meta_json
            FROM flow_runs WHERE status = 'failed'
        """),
]

#: 参与 trace 指纹的关键表 → 字段列表（行数 + 内容哈希双保险，见
#: tools/eval/fingerprint_gate 的教训：只比行数会被「删 3 加 3」骗过）
_TRACE_KEY_TABLES = {
    "conversations": ["id", "title"],
    "messages": ["id", "conversation_id", "role"],
    "feedback": ["id", "message_id", "feedback_type"],
    "llm_usage_stats": ["id", "conversation_id", "call_failed"],
    "hil_confirmations": ["id", "status", "decided_by"],
    "flow_runs": ["id", "status"],
    "intent_samples": ["id", "text", "intent", "status"],
    "bad_cases": ["id", "kind", "subject", "status"],
    "tool_call_logs": ["id"],
}

SCHEMA = "p1-1/trace-evalset@1"
FP_SCHEMA_VERSION = 1


# ── 配置（core.config trace2dataset 组；默认值与 DEFAULT_CONFIG 双登记）──
def get_settings() -> dict:
    """取材口径。缺配置时回落默认值（与 DEFAULT_CONFIG 一致，不是第二套真值）。"""
    from core import config as _cfg
    return {
        "enabled": _cfg.as_bool("trace2dataset", "enabled", True),
        "window_days": int(_cfg.get("trace2dataset", "window_days", 90) or 90),
        "max_per_extractor": int(_cfg.get("trace2dataset", "max_per_extractor", 300) or 300),
        "exclude_mock": _cfg.as_bool("trace2dataset", "exclude_mock", True),
        "mock_markers": str(_cfg.get("trace2dataset", "mock_markers",
                                     "（Mock 回答）,Mock 降级模式") or ""),
        "min_query_chars": int(_cfg.get("trace2dataset", "min_query_chars", 4) or 4),
        "max_response_chars": int(_cfg.get("trace2dataset", "max_response_chars", 600) or 600),
    }


def _marker_list(mock_markers: str) -> List[str]:
    return [m.strip() for m in (mock_markers or "").split(",") if m.strip()]


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "…[%d chars]" % len(s)


def _in_window(seen_at, cutoff: str) -> bool:
    """证据时间 ≥ 窗口下沿才取材。空时间戳视为窗外（无法证明新鲜的证据不取）。"""
    s = "" if seen_at is None else str(seen_at)
    return bool(s) and s >= cutoff


def _is_mock(query: str, response: str, markers: List[str]) -> bool:
    """Mock 降级回答不是系统能力证据（实测 15 条混在真实会话里）。"""
    text = (query or "") + "\n" + (response or "")
    return any(m and m in text for m in markers)


# ── 取材引擎 ──────────────────────────────────────────────────────
def collect_trace_cases(conn, *, settings: Optional[dict] = None,
                        extractors: Optional[List[ExtractorSpec]] = None) -> dict:
    """只读抽取（不动库）。返回 {enabled, candidates, summary}。"""
    cfg = settings or get_settings()
    if not cfg.get("enabled"):
        return {"enabled": False, "candidates": [], "summary": {"skipped_extractors": {}}}
    specs = extractors if extractors is not None else _SPECS
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - int(cfg["window_days"]) * 86400))
    markers = _marker_list(cfg["mock_markers"])
    cases, by_extractor, skipped = [], {}, {}
    skipped_mock = skipped_window = skipped_short = skipped_dup = 0
    for spec in specs:
        try:
            rows = conn.execute(spec.sql).fetchall()
        except sqlite3.Error as e:
            # 老库缺表（bad_cases/flow_runs 未建）⇒ 该证据源记 skipped，不连坐其它源
            skipped[spec.name] = "%s: %s" % (type(e).__name__, e)
            continue
        picked, seen_keys = [], set()
        # 新鲜度优先：窗口内证据可能远超上限，取最近的（评测集要新）
        rows = sorted(rows, key=lambda r: (str(r["seen_at"] or ""), int(r["source_id"] or 0)),
                      reverse=True)
        for r in rows:
            q = r["query"] or ""
            resp = r["response"] or ""
            if not _in_window(r["seen_at"], cutoff):
                skipped_window += 1
                continue
            if len(q.strip()) < int(cfg["min_query_chars"]):
                skipped_short += 1
                continue
            if cfg["exclude_mock"] and _is_mock(q, resp, markers):
                skipped_mock += 1
                continue
            key = (q.strip(), resp.strip())
            if key in seen_keys:      # 同一抽取器内完全相同的样本只留一条
                skipped_dup += 1
                continue
            seen_keys.add(key)
            resp = _clip(resp, int(cfg["max_response_chars"]))
            picked.append({
                "id": "%s-%03d" % (spec.name, len(picked) + 1),
                "extractor": spec.name,
                "kind": spec.kind,
                "label": spec.label,
                "query": q,
                "response": resp,
                "gold": r["gold"] or "",
                "source_table": spec.name.split("_")[0],
                "source_id": r["source_id"],
                "seen_at": r["seen_at"],
                "meta": _safe_json(r["meta_json"]),
            })
            if len(picked) >= int(cfg["max_per_extractor"]):
                break
        by_extractor[spec.name] = len(picked)
        cases.extend(picked)
    return {
        "enabled": True,
        "candidates": cases,
        "summary": {
            "window_cutoff": cutoff,
            "by_extractor": by_extractor,
            "by_kind": _tally(cases, "kind"),
            "by_label": _tally(cases, "label"),
            "total": len(cases),
            "skipped_mock": skipped_mock,
            "skipped_window": skipped_window,
            "skipped_short": skipped_short,
            "skipped_dup": skipped_dup,
            "skipped_extractors": skipped,
        },
    }


def _safe_json(s):
    if not s:
        return {}
    try:
        import json as _json
        v = _json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"raw": str(s)[:200]}


def _tally(cases, key):
    out = {}
    for c in cases:
        out[c[key]] = out.get(c[key], 0) + 1
    return out


# ── 指纹（生产数据每小时在变；旧 trace 集打分 = 失效 gold 自欺）─────
def _hash_query(conn, sql) -> str:
    h = hashlib.sha1()
    try:
        for row in conn.execute(sql):
            h.update(("\x1f".join("" if v is None else str(v) for v in row)).encode("utf-8"))
            h.update(b"\x1e")
    except sqlite3.Error:
        return ""
    return h.hexdigest()[:16]


def trace_fingerprint(conn) -> dict:
    """trace 侧语料指纹（与检索侧 fingerprint_gate 相互独立、互不污染）。"""
    fp = {}
    for name in _TRACE_KEY_TABLES:
        try:
            fp[name] = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
        except sqlite3.Error:
            fp[name] = None
    fp["messages_content_sha1"] = _hash_query(
        conn, "SELECT conversation_id, role, COALESCE(content,'') FROM messages ORDER BY id")
    fp["confirmed_intent_texts_sha1"] = _hash_query(
        conn, "SELECT text, intent FROM intent_samples WHERE status='confirmed' ORDER BY id")
    fp["hil_previews_sha1"] = _hash_query(
        conn, "SELECT id, COALESCE(preview,''), status FROM hil_confirmations ORDER BY id")
    fp["flow_runs_sha1"] = _hash_query(
        conn, "SELECT id, flow_name, status FROM flow_runs ORDER BY id")
    fp["badcase_subjects_sha1"] = _hash_query(
        conn, "SELECT id, subject, status FROM bad_cases ORDER BY id")
    fp["schema_version"] = FP_SCHEMA_VERSION
    return fp


def compare(expected: dict, actual: dict) -> list:
    """逐键比对，返回差异描述列表（空 = 完全一致）。"""
    diffs = []
    for k in sorted(set(expected) | set(actual)):
        e, a = expected.get(k), actual.get(k)
        if k == "schema_version":
            if e != a:
                diffs.append("%s: 评测集=%r 当前=%r（指纹结构已变，须重建）" % (k, e, a))
            continue
        if e != a:
            diffs.append("%s: 评测集记录=%r 当前=%r" % (k, e, a))
    return diffs


def check_drift(expected: dict, conn, strict: bool = True) -> bool:
    """trace 指纹门禁（纪律同 fingerprint_gate）：漂移时**拒绝**而非警告。

    strict=True 时直接 exit 3（让批量脚本无法忽略）；False 时返回 False。
    """
    diffs = compare(expected or {}, trace_fingerprint(conn))
    if not diffs:
        return True
    msg = ("trace 指纹不匹配，评测集已失效，禁止用于打分：\n"
           + "\n".join("  - " + d for d in diffs)
           + "\n\n处置：重建 trace 评测集（build_evalset.py，P1-1 Trace2Dataset），"
             "不要忽略此告警继续跑。")
    if strict:
        print("[门禁] " + msg, file=sys.stderr)
        sys.exit(3)
    print("[警告] " + msg, file=sys.stderr)
    return False


# ── 构建与落盘 ────────────────────────────────────────────────────
def build(conn, *, settings: Optional[dict] = None) -> dict:
    """构建完整 trace 评测集（含指纹）。只读，不写任何库。"""
    cfg = settings or get_settings()
    res = collect_trace_cases(conn, settings=cfg)
    return {
        "schema": SCHEMA,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": trace_fingerprint(conn),
        "settings": cfg,
        "counts": {
            "total": res["summary"].get("total", 0),
            "by_extractor": res["summary"].get("by_extractor", {}),
            "by_kind": res["summary"].get("by_kind", {}),
            "by_label": res["summary"].get("by_label", {}),
        },
        "summary": res["summary"],
        "cases": res["candidates"] if res.get("enabled") else [],
        "notes": [
            "gold/label 由生产证据派生：strong=人工裁决（intent 标注 / HIL 决定），"
            "weak=机器派生（无坏证据 ≠ 正确），negative=已知失败（P0-3 台账 / 失败终态）。",
            "conv_clean 是回归集：只能回答「改动有没有把原来能用的场景改坏」，"
            "不能用来证明系统质量——把弱标注当强标注用就是自己骗自己。",
            "Mock 降级回答已按 trace2dataset.mock_markers 排除：Mock 不是系统能力证据。",
            "trace 指纹必须比对（check_drift）：生产数据持续在变，旧集打分等于失效 gold 自欺。",
        ],
    }


def write(out_path: str, data: dict) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    import json as _json
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=1)
    return out_path


def default_out_path() -> str:
    return os.path.join(REPO, "tmp", "p21", "trace_evalset.json")
