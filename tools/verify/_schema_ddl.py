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


# ══════════════════════════════════════════════════════════════════════════
# 迁移加列（2026-11-25 补：`ddl_for` 只覆盖 CREATE TABLE，**不含迁移列**）
#
# 实测缺口：`conversations.pinned_at` 不在 `schema.py` 的 CREATE TABLE 里，
# 而是 `migrations/columns.py` 的 `_add("conversations", "pinned_at", ...)` 补的。
# 夹具只用 `ddl_for` ⇒ 缺该列 ⇒ `conversation_repo.list_conversations` 的 SELECT
# 引用它 ⇒ `no such column: c.pinned_at` ⇒ 门禁**异常退出**（不是断言失败，更难归因）。
#
# ⇒ "与产品同源"必须包含**两条来源**：CREATE TABLE + 迁移加列。
#    `ddl_statements_for(table)` 返回要按序执行的全部语句；`ddl_for` 保持原语义
#    （只有 CREATE TABLE），不动既有调用方。
# ══════════════════════════════════════════════════════════════════════════

MIGRATIONS = os.path.join(ROOT, "database", "migrations", "columns.py")


def migrated_columns(table: str) -> list:
    """`migrations/columns.py` 里对该表调用过 `_add(table, col, ddl)` 的 [(col, ddl)]。

    用 AST 收集（不按正则扫文本）：`_add` 的调用分布在条件分支里，
    正则会把注释里的示例也算进来。
    """
    import ast
    try:
        src = open(MIGRATIONS, encoding="utf-8").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (getattr(fn, "attr", "") or "")
        if name != "_add":
            continue
        args = node.args
        if len(args) < 3:
            continue
        try:
            t = ast.literal_eval(args[0])
            col = ast.literal_eval(args[1])
            ddl = ast.literal_eval(args[2])
        except (ValueError, SyntaxError):
            continue
        if t == table and isinstance(col, str) and isinstance(ddl, str):
            out.append((col, ddl))
    # 同名去重保序（迁移里可能在不同批次里重复 _add 同一列）
    seen, uniq = set(), []
    for col, ddl in out:
        if col not in seen:
            seen.add(col)
            uniq.append((col, ddl))
    return uniq


def ddl_statements_for(table: str) -> list:
    """夹具建该表需要的**全部**语句：`[CREATE TABLE, ALTER ADD COLUMN ...]`。

    用法（与手抄相比唯一的变化就是循环执行）：
        for _s in ddl_statements_for("conversations"):
            conn.execute(_s)

    ⚠️ 必须过滤掉 CREATE TABLE 里**已有**的列：产品的 `_add` 自身有
      `if column not in cols` 存在性检查（所以重复 _add 是幂等的），
      而这里直接拼 ALTER 会撞 `duplicate column name`（本轮实测：
      `conversations.project_id` 两边都有 ⇒ drizzle 崩在第二条语句上）。
    """
    stmts = [ddl_for(table)]
    have = set(columns_of(table))
    for col, ddl in migrated_columns(table):
        if col in have:
            continue
        stmts.append('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, col, ddl))
    return stmts


def columns_full(table: str) -> list:
    """产品**实际**拥有的列 = CREATE TABLE 列 + 迁移列（夹具列集判据用这个）。"""
    cols = columns_of(table)
    for col, _ddl in migrated_columns(table):
        if col not in cols:
            cols.append(col)
    return cols
