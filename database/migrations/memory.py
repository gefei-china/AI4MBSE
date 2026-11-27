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
    """列存在性守卫 + **空值回退链**。

    ★ 2026-11-11 真机修正：原实现只取「第一个存在的列」，于是凡该列在存量行里
    为空就整列读不出来 —— 真库实测 `todo` 858 行 subject/claim **全空**（因为
    `task_key`/`title` 根本不存在，应取 `subject`/`note`），`config_proposal`
    subj 全空（应取 `target_key` 而非 `config_key`），`interrupt` claim 10 行里
    只 3 行有值（`instruction` 空时该退到 `reason`）、`eval_queue` 两列全空
    （该表无 query 列，应退到 `sample_reason`）。
    ⇒ 现在把**所有存在**的候选列串成 `COALESCE(NULLIF(TRIM(c1),''), NULLIF(TRIM(c2),''), …)`，
    空串视为"没值"继续往下退，全都为空才落到常量默认值。
    """
    parts = []
    for c in candidates:
        if c in cols:
            parts.append("NULLIF(TRIM(CAST(%s AS TEXT)),'')" % c)
    if not parts:
        return default
    return "COALESCE(%s, %s)" % (", ".join(parts), default)


# 8 类台账 → 统一字段 (kind, subject, claim, evidence_ref, status, confidence, created_at)
# ⚠️ 每个元组的列名元组已按真库 `PRAGMA table_info` 逐个核对（2026-11-11），
#    **多列即回退链**，不是"取第一个存在的"。改它之前先跑一遍
#    `SELECT kind, sum(trim(coalesce(subject,''))<>'') FROM v_memory_notes GROUP BY kind`
#    —— 非空率为 0 就说明映射又错了。
_NOTE_SOURCES = (
    ("memory", "agent_memory",
     ("mem_topic", "mem_type"), ("content",),
     ("evidence_ref", "verify_state"), ("verify_state", "tier"),
     ("source_credibility", "relevance")),
    ("intent_sample", "intent_samples",
     ("hit_intent", "intent"), ("text",), ("source", "created_by"),
     ("status",), ("hit_conf",)),
    ("badcase", "bad_cases",
     ("kind", "subject"), ("detail", "title", "subject"), ("last_src_id",),
     ("status",), ("occurrences",)),
    # 该表**无** query/normalized 列 ⇒ claim 只能退到抽样原因；
    # 要正文得 JOIN query_trace（视图里不做，避免把 6750 行拉成笛卡尔）。
    ("eval_queue", "online_eval_queue",
     ("sample_type", "sample_reason"), ("sample_reason", "reason_detail"),
     ("query_trace_id", "trace_id"), ("status",), ("auto_score",)),
    ("config_proposal", "config_proposals",
     ("target_key", "rule"), ("rationale", "proposed_value"),
     ("source_feedback_ids", "fingerprint"), ("status",), ("current_value",)),
    ("todo", "agent_todos",
     ("subject",), ("note", "subject", "verify_result"), ("run_id",),
     ("status",), ("verify",)),
    ("interrupt", "orch_interrupts",
     ("kind", "run_id"), ("instruction", "reason"), ("run_id",),
     ("kind",), ("cancelled_tasks",)),
    ("trace", "query_trace",
     ("intent", "route"), ("query_raw", "query"), ("id",),
     ("score_kind", "route"), ("top_score",)),
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
            # ── P1-7 双时态（2026-11-27）──────────────────────────────────
            # 为什么需要：现在「新旧口径谁生效」只能靠 superseded_by 一刀切，
            #   且**无法回答"这条结论在 T 时刻是否成立"** —— 召回期只能全用最新，
            #   于是历史问题（"上一版怎么定的"）只能靠相似度猜。
            # valid_to IS NULL = 当前有效；被取代时写 superseded_at（不改 content，可回退）。
            # 存量行回填 created_at：它们本来就"自写入起有效"，语义不失真。
            # ⚠️ 口径：valid_from / valid_to 一律 **UTC**（与 `created_at` 的
            #   CURRENT_TIMESTAMP 同源），**不要**和 `last_accessed_at`/`superseded_at`
            #   的 localtime 混着比较 —— 本表两种口径并存是历史事实，新列只取 UTC 这一种。
            ("valid_from", "TEXT DEFAULT ''"),
            ("valid_to", "TEXT DEFAULT NULL"),
            # P1-6：冷层降级时刻（与 forgotten 解耦后，需要一个"何时进冷层"的时间轴，
            #   否则"冷了多久"无从判断 —— 复活与清理都要它）。
            ("archived_at", "TEXT DEFAULT ''"),
        ):
            if name not in cols:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN %s %s" % (name, decl))
                out["columns_added"].append(name)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_am_verify_state "
                     "ON agent_memory(verify_state)")
        # 存量回填（幂等）：valid_from 为空 ⇒ 用 created_at；valid_to 保持 NULL（= 仍有效）
        if "valid_from" in _cols(conn, "agent_memory"):
            conn.execute(
                "UPDATE agent_memory SET valid_from=COALESCE(NULLIF(TRIM(valid_from),''),"
                "NULLIF(TRIM(created_at),''), datetime('now')) "
                "WHERE valid_from IS NULL OR TRIM(valid_from)=''")
        # 已被取代的旧条（superseded_by>0）⇒ 有效期在取代时刻closed。
        # ⚠️ 只在 superseded_at 有值时回填，避免把"标记了但没记时刻"的行伪造成同一时点。
        _amc = _cols(conn, "agent_memory")
        if "valid_to" in _amc and "superseded_by" in _amc and "superseded_at" in _amc:
            # ⚠️ 2026-11-27 修：原实现直接 `valid_to=superseded_at`，而 superseded_at 是
            #   `datetime('now','localtime')`（本地时）、valid_from/new 写入是 `datetime('now')`（UTC）
            #   ⇒ **同一列混进两种时区**，差 8 小时，且"失效时刻"看起来比"生效时刻"还早。
            #   用 SQLite 的 `'utc'` 修饰符做真实时区换算（依赖本机时区，比硬减 8 小时可移植）。
            conn.execute(
                "UPDATE agent_memory SET valid_to=datetime(superseded_at,'utc') "
                "WHERE COALESCE(superseded_by,0)>0 AND (valid_to IS NULL OR TRIM(valid_to)='') "
                "AND COALESCE(NULLIF(TRIM(superseded_at),''),'')<>''")

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

    # ── 3. memory_events：记忆生命周期事件（P2 周报，2026-11-27）──────────
    # 为什么需要：`agent_memory` 只有**当前状态**（tier/forgotten/archived_at），
    #   答不了"这周有多少条升了 core / 降了冷层 / 被复活 / 被合并" —— 那些是**流动**，
    #   没有事件就只能靠猜。方案里"记忆周报（tier 流动）"卡在这里，不是聚合 API 缺，是**原始数据缺**。
    # 设计：append-only、只记事实与原因串（detail），**不回写 agent_memory**；
    #   表存在性守卫——老库/夹具缺列时写入静默跳过（记事件绝不能挡住维护主链路）。
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT DEFAULT (datetime('now','localtime')),
        kind TEXT NOT NULL,
        memory_id INTEGER DEFAULT 0,
        agent_id TEXT DEFAULT '',
        detail TEXT DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_memev_at ON memory_events(at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_memev_kind ON memory_events(kind, at)")

    # ── 4. transcript_fts（P0-2）──────────────────────────────────────────
    try:
        from adapters.transcript_fts import ensure_transcript_fts
        out["fts"] = ensure_transcript_fts(conn)
    except Exception as exc:
        # 不静默：记进返回，让启动日志/门禁看得见；FTS 失败不影响主库可用。
        out["skipped"].append("transcript_fts: %s: %s" % (type(exc).__name__, exc))
    conn.commit()
    return out
