# -*- coding: utf-8 -*-
"""P2-1 流水台账衰减（2026-11-11）：**只归档，不删除**。

## 一个必须写清的取舍
优化计划里原本写「全局 TTL 覆盖 tool_result_offloads / orch_stream_events」。
实施时否决了「到期 DELETE」这个动作：Anthropic 原文第一条法则是
`永远保留原始底账（Transcripts 是唯一 ground truth）`——Dreaming 的全部纠错
能力都建立在"笔记错了还能回查流水"上。给 L1 加自动 TTL，等于把自愈抓手删掉。

故本模块实现为**冷归档**：
  只读盘点（sweep）→ 生成归档计划 → 显式 confirm 后才把行**完整复制到
  transcript_archive** 再从主表移除 → 任何时刻都能按 archive_id 还原。
  默认 dry-run；不挂任何定时器，不无人值守删底账。

## 纪律
- `sweep(apply=False)` 是纯只读，返回可机读清单（含行数、最老时间、建议动作）；
- `archive_apply(confirm=False)` 一律拒绝（沿用本仓 HIL 门习惯）；
- 归档表存 kind + 原表全部列 JSON，不丢字段；
- 归档后 FTS 触发器会同步删除索引行（索引是派生物，可重建）。
"""
from __future__ import annotations

import json

_TABLES = {
    "tool_result_offloads": ("id", "created_at"),
    "orch_stream_events": ("id", "created_at"),
}
_ARCHIVE = "transcript_archive"


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _ensure_archive(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS transcript_archive (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, ref_id INTEGER NOT NULL,
        payload TEXT NOT NULL, archived_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(kind, ref_id))""")


def sweep(conn, *, older_than_days: int = 90, per_table_limit: int = 1000) -> dict:
    """只读盘点：列出超龄行，不写任何表。"""
    out = {"dry_run": True, "older_than_days": older_than_days,
           "tables": {}, "total": 0, "skipped": []}
    cutoff = "datetime('now','-%d days')" % int(older_than_days)
    for table, (pk, ts) in _TABLES.items():
        if not _table_exists(conn, table):
            out["skipped"].append(table)
            continue
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE %s < %s" % (table, ts, cutoff)
            ).fetchone()[0]
            ids = [r[0] for r in conn.execute(
                "SELECT %s FROM %s WHERE %s < %s ORDER BY %s LIMIT ?" %
                (pk, table, ts, cutoff, pk), (per_table_limit,)).fetchall()]
            oldest = conn.execute("SELECT MIN(%s) FROM %s" % (ts, table)).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            out["skipped"].append("%s: %s" % (table, type(exc).__name__))
            continue
        out["tables"][table] = {"expired": n, "sample_ids": ids, "oldest": oldest,
                                "action": "archive_then_remove"}
        out["total"] += n
    return out


def archive_apply(conn, plan: dict, *, confirm: bool = False) -> dict:
    """把计划里的超龄行**完整复制**到归档表后再从主表移除。

    必须先归档成功才删；未 confirm 或归档表写入失败 ⇒ 一行都不动。
    """
    out = {"ok": False, "archived": 0, "removed": 0, "reason": ""}
    if not confirm:
        out["reason"] = "CONFIRM_REQUIRED"
        return out
    if not plan or not isinstance(plan, dict):
        out["reason"] = "EMPTY_PLAN"
        return out
    _ensure_archive(conn)
    try:
        for table, info in (plan.get("tables") or {}).items():
            if table not in _TABLES or not _table_exists(conn, table):
                continue
            pk = _TABLES[table][0]
            for rid in info.get("sample_ids") or []:
                row = conn.execute("SELECT * FROM %s WHERE %s=?" % (table, pk),
                                   (rid,)).fetchone()
                if row is None:
                    continue
                payload = (dict(row) if hasattr(row, "keys")
                           else {"value": list(row)})
                conn.execute(
                    "INSERT OR IGNORE INTO %s (kind, ref_id, payload) VALUES (?,?,?)" %
                    _ARCHIVE,
                    (table, rid, json.dumps(payload, ensure_ascii=False, default=str)))
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    continue  # 已归档过（幂等），不重复计数但仍可移除
                out["archived"] += 1
                conn.execute("DELETE FROM %s WHERE %s=?" % (table, pk), (rid,))
                out["removed"] += 1
        conn.commit()
        out["ok"] = True
        out["reason"] = "ARCHIVED_THEN_REMOVED"
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        out["reason"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def restore(conn, kind: str, ref_id: int) -> dict:
    """按归档记录还原字段清单（只读，供人工核对/回灌）。"""
    if not _table_exists(conn, _ARCHIVE):
        return {}
    r = conn.execute(
        "SELECT payload FROM %s WHERE kind=? AND ref_id=?" % _ARCHIVE,
        (kind, ref_id)).fetchone()
    if not r:
        return {}
    try:
        return json.loads(r[0])
    except Exception:
        return {}
