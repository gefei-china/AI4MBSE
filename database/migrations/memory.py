# -*- coding: utf-8 -*-
"""记忆层迁移（2026-11-11，Claude 四层记忆架构对标 P0/P1）。

三件事，一次幂等迁移：
1. `agent_memory` 补 evidence / verify 列，让"笔记"能指回底层流水（P0-3）；
2. `v_memory_notes` 统一只读视图，把 8 类分裂台账收敛成一套字段（P1-3）；
3. 建 `transcript_fts` 流水全文索引（P0-2），供 Dreaming 拿 ground truth 对账。

纪律：
- 列补齐用 PRAGMA 检测，老库/夹具不报错；
- 视图用 `DROP VIEW + CREATE VIEW`，每次 init_db 刷新结构（表结构变了不会留旧视图）；
- 只 UNION **确实存在**的表：干净库/单表夹具也能建出可用视图，不静默吞异常；
- FTS 建表结果写进返回 dict，`skipped` 明确列出缺失源表。
"""
from __future__ import annotations


def _cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except Exception:
        return set()


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _pick(cols: set, candidates: tuple, default: str = "''") -> str:
    """列存在性守卫：返回第一个可用列，否则返回常量。"""
    for c in candidates:
        if c in cols:
            return "COALESCE(CAST(%s AS TEXT),'')" % c
    return default


# 8 类台账 → 统一字段 (kind, subject, claim, evidence_ref, status, confidence, created_at)
_NOTE_SOURCES = (
    ("memory", "agent_memory",
     ("mem_topic", "mem_type"), ("content",),
     ("evidence_ref", "trace_id"), ("verify_state", "tier"), ("source_credibility", "relevance")),
    ("intent_sample", "intent_samples",
     ("hit_intent",), ("text",), ("trace_id", "source"), ("status",), ("hit_conf",)),
    ("badcase", "bad_cases",
     ("kind",), ("subject", "detail"), ("last_src_id",), ("status",), ("occurrences",)),
    ("eval_queue", "online_eval_queue",
     ("intent", "route"), ("query", "query_raw"), ("query_trace_id", "trace_id"), ("status",), ("top_score",)),
    ("config_proposal", "config_proposals",
     ("config_key", "origin"), ("rationale", "detail"), ("source_feedback_ids",), ("status",), ("confidence",)),
    ("todo", "agent_todos",
     ("task_key",), ("title", "content"), ("run_id",), ("status",), ("machine_evidence",)),
    ("interrupt", "orch_interrupts",
     ("run_id",), ("instruction", "reason"), ("run_id",), ("status",), ("created_at",)),
    ("trace", "query_trace",
     ("intent", "route"), ("query_raw", "query"), ("id",), ("score_kind",), ("top_score",)),
)


def _migrate_memory_layer(conn) -> dict:
    """幂等。返回的 dict 供门禁/启动日志核对，异常不静默。"""
    out = {"columns_added": [], "view_parts": [], "fts": {}, "skipped": []}

    # ── 1. agent_memory evidence / verify ────────────────────────────────
    if not _table_exists(conn, "agent_memory"):
        out["skipped"].append("agent_memory 不存在")
    else:
        cols = _cols(conn, "agent_memory")
        for name, decl in (
            # NULL 有含义：未评估 ≠ 1.0。故 source_credibility 不给 DEFAULT。
            ("evidence_ref", "TEXT DEFAULT ''"),
            ("verify_state", "TEXT DEFAULT 'unverifiable'"),
            ("evidence_note", "TEXT DEFAULT ''"),
            ("source_credibility", "REAL"),
        ):
            if name not in cols:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN %s %s" % (name, decl))
                out["columns_added"].append(name)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_am_verify_state "
                     "ON agent_memory(verify_state)")

    # ── 2. v_memory_notes 统一视图 ───────────────────────────────────────
    parts = []
    for kind, table, subj, claim, evid, status, conf in _NOTE_SOURCES:
        if not _table_exists(conn, table):
            out["skipped"].append("视图跳过 %s（表不存在）" % table)
            continue
        c = _cols(conn, table)
        parts.append(
            "SELECT '%s' AS kind, %s AS subject, %s AS claim, %s AS evidence_ref, "
            "%s AS status, %s AS confidence, %s AS created_at FROM %s" % (
                kind,
                _pick(c, subj), _pick(c, claim), _pick(c, evid), _pick(c, status),
                _pick(c, conf, "0"),
                _pick(c, ("created_at", "updated_at"), "CURRENT_TIMESTAMP"),
                table))
        out["view_parts"].append(kind)
    if parts:
        conn.execute("DROP VIEW IF EXISTS v_memory_notes")
        conn.execute("CREATE VIEW v_memory_notes AS " + " UNION ALL ".join(parts))

    # ── 3. transcript_fts（P0-2）──────────────────────────────────────────
    try:
        from transcript_fts import ensure_transcript_fts
        out["fts"] = ensure_transcript_fts(conn)
    except Exception as exc:
        # 不静默：记进返回，让启动日志/门禁看得见；FTS 失败不影响主库可用。
        out["skipped"].append("transcript_fts: %s: %s" % (type(exc).__name__, exc))
    conn.commit()
    return out
