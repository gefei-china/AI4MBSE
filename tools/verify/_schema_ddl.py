# -*- coding: utf-8 -*-
"""从产品 DDL 抽出建表语句，供门禁夹具**直接取用**（不再手抄）。

## 为什么需要它（2026-11-25 实测）

`verify_orch_span` 与 `verify_replan_p1` 各自把 `orch_run_spans` 的 CREATE TABLE
**手抄**进临时库夹具。产品侧后来给这张表加了 `trace_id`（B1 可观测，靠
`migrations/columns.py` 的 `_add` 补列），夹具没跟上 ⇒ 带 trace_id 的 INSERT
直接崩 `no column named trace_id`。

这次漂移的后果是"门禁崩"（还算幸运）。**反向漂移更危险**：夹具多一列、产品少一列，
门禁全绿而产品运行时崩。手抄的 fixture 与产品 DDL 之间**没有任何机制保证一致**。

⇒ 夹具一律 `conn.execute(ddl_for("表名"))`，与 `database/schema.py` 同源。
守卫判据：`verify_orch_span` 的 S1e（tools/ 下不得再出现手抄该表的 DDL）。

用法：
    from _schema_ddl import ddl_for
    conn.execute(ddl_for("orch_run_spans"))
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA = os.path.join(ROOT, "database", "schema.py")


def _schema_text() -> str:
    with open(SCHEMA, encoding="utf-8") as f:
        return f.read()


def ddl_for(table: str) -> str:
    """返回 `database/schema.py` 里该表的 CREATE TABLE 语句（含 IF NOT EXISTS）。

    找不到就**抛错**：宁可让夹具当场红，也不要拿一句空 SQL 建出空表
    （那会让后续所有断言在"表不存在/缺列"上以别的方式失败，根因看不出来）。
    """
    src = _schema_text()
    m = re.search(r"CREATE TABLE IF NOT EXISTS\s+%s\s*\(" % re.escape(table), src)
    if not m:
        raise LookupError("database/schema.py 里没有表 %r 的 CREATE TABLE 语句" % table)
    i = m.start()
    # 从首个 '(' 起做括号配对，找到与之匹配的 ')'（跳过字符串字面量里的括号）
    j = src.index("(", m.start())
    depth, k, n = 0, j, len(src)
    while k < n:
        ch = src[k]
        if ch in "'\"":
            q = ch
            k += 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise LookupError("表 %r 的 CREATE TABLE 括号未闭合" % table)


def columns_of(table: str) -> list:
    """从 DDL 粗取列名（去掉注释与约束行），用于「夹具列集 == 产品列集」类判据。"""
    body = ddl_for(table)
    body = re.sub(r"--[^\n]*", "", body)
    body = body[body.index("(") + 1:body.rindex(")")]
    out = []
    for seg in body.split(","):
        seg = seg.strip()
        if not seg:
            continue
        head = seg.split()[0].strip('"`[]')
        if head.upper() in ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"):
            continue
        out.append(head)
    return out
