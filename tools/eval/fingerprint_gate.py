# -*- coding: utf-8 -*-
"""语料指纹 + 评测集有效性门禁（2026-10-14 落地）。

═══════════════════════════════════════════════════════════════════════
为什么必须有这个模块（实测教训，不是预防性编程）
═══════════════════════════════════════════════════════════════════════
`tmp/p21/evalset.json` 自带 `fingerprint` 与三条 notes，其中明写
「**语料指纹必须比对**：图谱改写会使 gold 实体消失、路由标签失效」——
**但从没有人比对过**。实测后果：

  记录指纹：documents=16 / chunks=7269 / entities=189
  当前真库：documents=12 / chunks=6727 / entities=189
  ⇒ 292 条 gold 中 **288 条（99%）指向的 chunk 已不含查询标题词**
  （chunk_index 没变，但内容被重切移位了）

**为什么"chunk_index 没变"仍然失效**：gold 用的是**位置键**
（`source_doc` + `chunk_index`）。语料一重切，同一 index 指向的**内容就换了**。
⇒ 位置键当 gold 不稳，**内容派生的语义锚**才稳。

这个模块提供两件事：
  ① `fingerprint(conn)`  —— 取当前语料指纹
  ② `check_gate(...)`   —— 与评测集记录的指纹比对，漂移则**拒绝打分**（非警告）

**纪律**：指纹不匹配的评测集**一律不得用于打分**。
门禁的默认动作是**拒绝**（exit 非0），而不是"打个警告继续跑"——
后者在批量脚本里必然被忽略，等于没有门禁。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys

#: 参与指纹的关键表 → (表名, 字段列表)
_KEY_TABLES = {
    "documents": ["id", "filename", "parse_status"],
    "document_chunks": ["id", "document_id", "chunk_index", "source_doc", "section"],
    "entities": ["id", "name", "entity_type", "branch", "status"],
    "relations": ["id", "source_id", "target_id", "relation_type", "branch", "status"],
}


def fingerprint(conn, with_hashes: bool = True) -> dict:
    """取当前语料指纹。

    ⚠️ 指纹必须包含**内容派生的哈希**（release 实体名集合 sha1）——
    只比行数的话，「删 3 篇文档 + 加3 篇」行数不变但内容全变，仍会被判为"一致"。
    """
    fp = {}
    for name, cols in _KEY_TABLES.items():
        try:
            n = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
        except sqlite3.Error:
            n = None
        fp[name] = n
    if with_hashes:
        fp["release_entity_names_sha1"] = _hash_query(
            conn, "SELECT name FROM entities WHERE branch='release' ORDER BY name")
        fp["chunk_sections_sha1"] = _hash_query(
            conn, "SELECT source_doc, chunk_index, COALESCE(section,'') "
                  "FROM document_chunks ORDER BY source_doc, chunk_index")
    fp["schema_version"] = 2   # 指纹结构版本；结构变了就该重建评测集
    return fp


def _hash_query(conn, sql: str) -> str:
    h = hashlib.sha1()
    try:
        for row in conn.execute(sql):
            h.update(("\x1f".join("" if v is None else str(v) for v in row)).encode("utf-8"))
            h.update(b"\x1e")
    except sqlite3.Error:
        return ""
    return h.hexdigest()[:16]


def compare(expected: dict, actual: dict) -> list:
    """逐键比对，返回差异描述列表（空 = 完全一致）。"""
    diffs = []
    for k in sorted(set(expected) | set(actual)):
        e, a = expected.get(k), actual.get(k)
        if k in ("schema_version",):
            # 指纹结构不同 → 必然无法判定，明确报出而不是跳过
            if e != a:
                diffs.append("%s: 评测集=%r 当前=%r（指纹结构已变，须重建评测集）" % (k, e, a))
            continue
        if e != a:
            diffs.append("%s: 评测集记录=%r 当前=%r" % (k, e, a))
    return diffs


def check_gate(evalset_fp: dict, conn, strict: bool = True) -> bool:
    """指纹门禁。漂移时**拒绝**（strict=True）或仅告警。

    返回 True = 可继续；False = 不可继续。
    ⚠️ strict 时**直接 exit 3**，不返回 —— 让批量脚本无法忽略它。
    """
    actual = fingerprint(conn)
    diffs = compare(evalset_fp or {}, actual)
    if not diffs:
        return True
    msg = ("语料指纹不匹配，评测集已失效，禁止用于打分：\n"
           + "\n".join("  - " + d for d in diffs)
           + "\n\n处置：重建评测集（tools/eval/build_evalset.py 或 build_qrels.py），"
             "不要忽略此告警继续跑 —— 打分结果会看起来正常但已无意义。")
    if strict:
        print("[门禁] " + msg, file=sys.stderr)
        sys.exit(3)
    print("[警告] " + msg, file=sys.stderr)
    return False


def load_json_fp(path: str) -> dict:
    """从 evalset.json 之类文件里取 fingerprint 字段。"""
    with open(path, encoding="utf-8") as f:
        return (json.load(f) or {}).get("fingerprint") or {}