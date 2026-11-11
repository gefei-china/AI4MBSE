# -*- coding: utf-8 -*-
"""流水层 FTS5 全文检索（2026-11-11，Claude 四层记忆架构对标 P0-2）。

## 为什么必须有这一层
Anthropic《The components of agent memory》把 `transcripts (a searchable event log)`
定为**唯一 ground truth**，Dreaming 的全部纠错能力都建立在"能拿流水去查笔记"上。
本仓此前 FTS5 只覆盖图谱三表（`fts_search.py`），`messages` / `tool_call_logs` /
`query_trace` 只能靠 `LIKE`——在 1.5 万行流水上既慢又无法做短语/子串检索。
缺了它，即使写了 `core/memory_dream.py` 也拿不到证据。

## 设计
- 一张虚表 `transcript_fts`：kind / ref_id UNINDEXED / title / body，
  `tokenize='trigram'`（中文子串友好，<3 字符走 LIKE 兜底）；
- 三张源表各挂 AFTER INSERT / UPDATE / DELETE 触发器，**不改任何写路径**；
  UPDATE 触发器先删旧再插新（避免既有 `knowledge_fts` 那种重复累积）；
- 长文本截断到 4000 字符：trigram 对 8000 字符工具结果建索引会显著膨胀，
  而证据检索只需要命中位置，不需要全文入索引（原文仍在 `tool_result_offloads`）；
- `ensure_transcript_fts` 幂等；`rebuild` 默认按规模决定是否全量重建。

## 不做什么
不索引 embedding / 二进制 / `card_data`：那些不是可判读的流水文本。
不自动改任何写路径，触发器是唯一同步机制。
"""
from __future__ import annotations

_FTS = "transcript_fts"
_BODY_CAP = 4000

# kind / 源表 / title 表达式 / body 表达式 / 全量重建 SELECT
_SOURCES = {
    "messages": {
        "kind": "messages",
        "table": "messages",
        "title": "'message#' || NEW.id || ' ' || COALESCE(NEW.role,'')",
        "body": "SUBSTR(COALESCE(NEW.content,''),1,%d)" % _BODY_CAP,
        "rows": ("SELECT id, 'message#' || id || ' ' || COALESCE(role,'') AS title, "
                 "SUBSTR(COALESCE(content,''),1,%d) AS body FROM messages" % _BODY_CAP),
        "like_cols": ("content",),
    },
    "tool_logs": {
        "kind": "tool_logs",
        "table": "tool_call_logs",
        "title": "'tool#' || NEW.id || ' ' || COALESCE(NEW.tool_name,'')",
        "body": ("COALESCE(NEW.tool_name,'') || ' ' || "
                 "SUBSTR(COALESCE(NEW.arguments,''),1,800) || ' ' || "
                 "SUBSTR(COALESCE(NEW.result,''),1,%d)" % _BODY_CAP),
        "rows": ("SELECT id, 'tool#' || id || ' ' || COALESCE(tool_name,'') AS title, "
                 "COALESCE(tool_name,'') || ' ' || SUBSTR(COALESCE(arguments,''),1,800) "
                 "|| ' ' || SUBSTR(COALESCE(result,''),1,%d) AS body "
                 "FROM tool_call_logs" % _BODY_CAP),
        "like_cols": ("tool_name", "arguments", "result"),
    },
    "query_trace": {
        "kind": "query_trace",
        "table": "query_trace",
        "title": "'trace#' || NEW.id || ' ' || COALESCE(NEW.route,'')",
        "body": ("SUBSTR(COALESCE(NEW.query_raw, NEW.query, ''),1,600) || ' ' || "
                 "SUBSTR(COALESCE(NEW.normalized,''),1,%d)" % _BODY_CAP),
        "rows": ("SELECT id, 'trace#' || id || ' ' || COALESCE(route,'') AS title, "
                 "SUBSTR(COALESCE(query_raw, query, ''),1,600) || ' ' || "
                 "SUBSTR(COALESCE(normalized,''),1,%d) AS body "
                 "FROM query_trace" % _BODY_CAP),
        "like_cols": ("query", "query_raw", "normalized"),
    },
}


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def ensure_transcript_fts(conn, rebuild=None) -> dict:
    """幂等建虚表 + 触发器。返回可机读的状态（不静默）。

    :param rebuild: True 强制全量重建；False 只建结构；None 按规模自动
                    （索引为空且三表总行数 ≤ 50000 时重建）。
    """
    out = {"created": False, "rebuilt": False, "rows": 0,
           "needs_rebuild": False, "skipped": []}
    missing = [s["table"] for s in _SOURCES.values() if not _table_exists(conn, s["table"])]
    if missing:
        out["skipped"] = missing
        return out

    before = _table_exists(conn, _FTS)
    conn.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS} USING fts5(
        kind, ref_id UNINDEXED, title, body, tokenize='trigram')""")
    out["created"] = not before

    for spec in _SOURCES.values():
        kind, table = spec["kind"], spec["table"]
        for op, evt in (("ai", "INSERT"), ("au", "UPDATE"), ("ad", "DELETE")):
            tname = f"tfts_{table}_{op}"
            conn.execute(f"DROP TRIGGER IF EXISTS {tname}")
            if op == "ad":
                body = (f"DELETE FROM {_FTS} WHERE kind='{kind}' "
                        f"AND ref_id=OLD.id;")
            elif op == "au":
                body = (f"DELETE FROM {_FTS} WHERE kind='{kind}' AND ref_id=OLD.id; "
                        f"INSERT INTO {_FTS} (kind, ref_id, title, body) VALUES "
                        f"('{kind}', NEW.id, {spec['title']}, {spec['body']});")
            else:
                body = (f"INSERT INTO {_FTS} (kind, ref_id, title, body) VALUES "
                        f"('{kind}', NEW.id, {spec['title']}, {spec['body']});")
            conn.execute(f"CREATE TRIGGER {tname} AFTER {evt} ON {table} "
                         f"BEGIN {body} END")

    n_idx = conn.execute(f"SELECT COUNT(*) FROM {_FTS}").fetchone()[0]
    n_src = sum(conn.execute(f"SELECT COUNT(*) FROM {s['table']}").fetchone()[0]
                for s in _SOURCES.values())
    do_rebuild = (rebuild is True) or (rebuild is None and n_idx == 0 and 0 < n_src <= 50000)
    if do_rebuild:
        rebuild_transcript_fts(conn)
        out["rebuilt"] = True
    elif n_idx != n_src:
        out["needs_rebuild"] = True
    out["rows"] = conn.execute(f"SELECT COUNT(*) FROM {_FTS}").fetchone()[0]
    conn.commit()
    return out


def rebuild_transcript_fts(conn) -> int:
    """全量重建（初始化 / 索引损坏 / 触发器曾缺失后的补齐）。"""
    conn.execute(f"DELETE FROM {_FTS}")
    n = 0
    for spec in _SOURCES.values():
        conn.execute(
            f"INSERT INTO {_FTS} (kind, ref_id, title, body) "
            f"SELECT '{spec['kind']}', id, title, body FROM ({spec['rows']})")
        n += conn.execute(
            f"SELECT COUNT(*) FROM {_FTS} WHERE kind='{spec['kind']}'").fetchone()[0]
    conn.commit()
    return n


def search_transcripts(conn, query: str, limit: int = 20) -> list:
    """按短语检索流水。短词/零命中回退 LIKE（与 fts_search.py 同款口径）。"""
    q = (query or "").strip()
    if not q:
        return []
    rows = []
    if len(q) >= 3 and _table_exists(conn, _FTS):
        phrase = '"' + q.replace('"', '""') + '"'
        try:
            rows = [dict(r) for r in conn.execute(
                f"SELECT kind, ref_id, title, "
                f"SUBSTR(body,1,200) AS snippet FROM {_FTS} "
                f"WHERE {_FTS} MATCH ? ORDER BY rank LIMIT ?",
                (phrase, int(limit))).fetchall()]
        except Exception:
            rows = []
    if rows:
        return rows
    pat = "%" + q + "%"
    for spec in _SOURCES.values():
        if not _table_exists(conn, spec["table"]):
            continue
        cond = " OR ".join("COALESCE(%s,'') LIKE ?" % c for c in spec["like_cols"])
        args = [pat] * len(spec["like_cols"]) + [int(limit) - len(rows)]
        if len(rows) >= int(limit):
            break
        try:
            got = conn.execute(
                f"SELECT id, created_at FROM {spec['table']} WHERE {cond} LIMIT ?",
                tuple(args)).fetchall()
        except Exception:
            continue
        for r in got:
            rows.append({"kind": spec["kind"], "ref_id": r[0],
                         "title": "%s#%s" % (spec["kind"], r[0]),
                         "snippet": "(LIKE 兜底)"})
    return rows[:int(limit)]


def fts_status(conn) -> dict:
    """供门禁/监控对账：索引行数必须等于三张源表行数之和。"""
    if not _table_exists(conn, _FTS):
        return {"exists": False}
    by_kind = {r[0]: r[1] for r in conn.execute(
        f"SELECT kind, COUNT(*) FROM {_FTS} GROUP BY kind")}
    src = {s["kind"]: conn.execute(
        f"SELECT COUNT(*) FROM {s['table']}").fetchone()[0] for s in _SOURCES.values()
        if _table_exists(conn, s["table"])}
    return {"exists": True, "by_kind": by_kind, "sources": src,
            "aligned": all(by_kind.get(k, 0) == v for k, v in src.items())}
