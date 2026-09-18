# -*- coding: utf-8 -*-
"""P1-5：FTS5 全文检索（2026-09-06）。

设计：
1. knowledge_fts —— FTS5 虚表（tokenize='trigram'），中英文**子串**匹配
   （trigram 最短 3 字符；2 字符以下查询走 LIKE 兜底）。
   列：kind(entities|relations|triples) / ref_id(UNINDEXED) / title / body。
2. 同步方式：SQLite 触发器（AFTER INSERT/UPDATE/DELETE）挂在
   entities / relations / triples 上——不改任何写路径，写入自动入索引。
3. fts_query：MATCH 查询（短语引号包裹防语法注入）；
   查询串 <3 字符或零命中时回退 LIKE 兜底，保证短中文词（如"卫星"）可查。
4. fts_rebuild：全量重建（初始化 / 索引损坏修复 / 大批导入后手动触发）。

索引范围：
- entities：name + entity_type + properties 文本（属性内容可检索）
- relations：source 名 + 关系类型 + target 名 + properties
- triples：subject_name + predicate + object_value（知识原子层）
"""
import json
import logging

logger = logging.getLogger(__name__)

_FTS_TABLE = "knowledge_fts"

# 触发器定义：kind / ref 源列 / title 表达式 / body 表达式
_TRIGGERS = {
    "entities": {
        "kind": "entities",
        "ref": "NEW.id",
        "title": "NEW.name",
        "body": "NEW.entity_type || ' ' || COALESCE(NEW.properties, '')",
        "rows": """SELECT id,
                 name AS title,
                 entity_type || ' ' || COALESCE(properties, '') AS body
                 FROM entities""",
    },
    "relations": {
        "kind": "relations",
        "ref": "NEW.id",
        "title": "(SELECT name FROM entities WHERE id=NEW.source_id) || ' —' || NEW.relation_type || '→ ' || (SELECT name FROM entities WHERE id=NEW.target_id)",
        "body": "NEW.relation_type || ' ' || COALESCE(NEW.properties, '')",
        "rows": """SELECT r.id,
                 (SELECT name FROM entities WHERE id=r.source_id) || ' —' || r.relation_type || '→ ' || (SELECT name FROM entities WHERE id=r.target_id) AS title,
                 r.relation_type || ' ' || COALESCE(r.properties, '') AS body
                 FROM relations r""",
    },
    "triples": {
        "kind": "triples",
        "ref": "NEW.id",
        "title": "NEW.subject_name || ' —' || NEW.predicate || '→ ' || NEW.object_value",
        "body": "NEW.subject_name || ' ' || NEW.predicate || ' ' || NEW.object_value",
        "rows": """SELECT id,
                 subject_name || ' —' || predicate || '→ ' || object_value AS title,
                 subject_name || ' ' || predicate || ' ' || object_value AS body
                 FROM triples""",
    },
}


def _fts_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_FTS_TABLE,)).fetchone() is not None


def ensure_fts(conn) -> None:
    """幂等初始化：虚表 + 全表触发器；虚表为空而主表有数据时自动重建。"""
    conn.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5(
        kind, ref_id UNINDEXED, title, body, tokenize='trigram')""")
    for table, spec in _TRIGGERS.items():
        # 触发器名带表前缀，幂等（DROP IF EXISTS + CREATE）
        for op, evt in (("ai", "INSERT"), ("au", "UPDATE"), ("ad", "DELETE")):
            tname = f"fts_{table}_{op}"
            conn.execute(f"DROP TRIGGER IF EXISTS {tname}")
            if op == "ad":
                body = f"DELETE FROM {_FTS_TABLE} WHERE kind='{spec['kind']}' AND ref_id=OLD.id"
            else:
                body = (f"INSERT INTO {_FTS_TABLE} (kind, ref_id, title, body) "
                        f"VALUES ('{spec['kind']}', {spec['ref']}, {spec['title']}, {spec['body']})")
            conn.execute(f"""CREATE TRIGGER {tname} AFTER {evt} ON {table} BEGIN {body}; END""")
    conn.commit()
    # 主表有数据但索引为空 → 自动重建（首次启用）
    n_idx = conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE}").fetchone()[0]
    if n_idx == 0:
        n_main = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TRIGGERS)
        if n_main > 0:
            logger.info("FTS5 索引为空而主表 %s 行，自动重建", n_main)
            fts_rebuild(conn)


def fts_rebuild(conn) -> int:
    """全量重建索引，返回索引行数。"""
    conn.execute(f"DELETE FROM {_FTS_TABLE}")
    total = 0
    for table, spec in _TRIGGERS.items():
        conn.execute(
            f"INSERT INTO {_FTS_TABLE} (kind, ref_id, title, body) "
            f"SELECT '{spec['kind']}', * FROM ({spec['rows']})")
        total += conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE} WHERE kind='{spec['kind']}'").fetchone()[0]
    conn.commit()
    return total


def _fts_escape(q: str) -> str:
    """MATCH 短语包裹：防 FTS5 语法字符注入（- ^ : 等按字面处理）。"""
    return '"' + q.replace('"', '""') + '"'


def fts_query(conn, q: str, kinds: list | None = None, limit: int = 50) -> dict:
    """全文检索：返回 {results, mode, total}。

    - mode=fts：trigram MATCH（q ≥3 字符）
    - mode=like：LIKE 兜底（q <3 字符或 FTS 零命中——覆盖 2 字中文词）
    - kinds 过滤（可选）：['entities','relations','triples'] 子集
    """
    q = (q or "").strip()
    if not q:
        return {"results": [], "mode": "empty", "total": 0}
    kinds = kinds or list(_TRIGGERS)
    ph = ",".join("?" * len(kinds))
    rows = []
    mode = "fts"
    if len(q) >= 3:
        try:
            rows = conn.execute(
                f"SELECT kind, ref_id, title, body FROM {_FTS_TABLE} "
                f"WHERE {_FTS_TABLE} MATCH ? AND kind IN ({ph}) "
                f"ORDER BY rank LIMIT ?",
                (_fts_escape(q), *kinds, limit)).fetchall()
        except Exception as e:  # noqa: BLE001  MATCH 语法异常 → LIKE 兜底
            logger.warning("FTS MATCH 异常，LIKE 兜底: %s", e)
            rows = []
    if not rows:
        # FTS 零命中或查询串 <3 字符（trigram 最短边界）→ LIKE 兜底
        mode = "like"
        rows = conn.execute(
            f"SELECT kind, ref_id, title, body FROM {_FTS_TABLE} "
            f"WHERE (title LIKE ? OR body LIKE ?) AND kind IN ({ph}) LIMIT ?",
            (f"%{q}%", f"%{q}%", *kinds, limit)).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        # 附带实时状态（索引内不含 status/branch，查询端补齐）
        if d["kind"] == "entities":
            live = conn.execute(
                "SELECT id, name, entity_type, status, branch FROM entities WHERE id=?",
                (d["ref_id"],)).fetchone()
            if live:
                d["entity"] = dict(live)
        elif d["kind"] == "relations":
            live = conn.execute(
                "SELECT id, relation_type, status, source_id, target_id FROM relations WHERE id=?",
                (d["ref_id"],)).fetchone()
            if live:
                d["relation"] = dict(live)
        else:
            live = conn.execute(
                "SELECT triple_id, status FROM triples WHERE id=?", (d["ref_id"],)).fetchone()
            if live:
                d["triple"] = dict(live)
        results.append(d)
    return {"results": results, "mode": mode, "total": len(results)}


def fts_stats(conn) -> dict:
    """索引统计（供治理面板 / 诊断）。"""
    if not _fts_exists(conn):
        return {"enabled": False}
    out = {"enabled": True, "total": conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE}").fetchone()[0]}
    for kind in _TRIGGERS:
        out[kind] = conn.execute(
            f"SELECT COUNT(*) FROM {_FTS_TABLE} WHERE kind=?", (kind,)).fetchone()[0]
    return out
