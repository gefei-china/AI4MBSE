"""P0-4 / D2：给 `skills` / `tools` 补治理元数据列（owner / eval_set / last_reviewed）。

为什么本批做：文章「团队维护 Skills」的硬前提 —— 没有 owner 就没人负责修订，
没有 eval_set 就没法把 Skill 挂到 Golden Set，没有 last_reviewed 就判不出「闲置」。

三处真源必须同时对齐，缺一即判红：
  ① 新库 DDL：`database/schema.py` 的 CREATE TABLE skills / tools
  ② 老库迁移：`database/migrations/columns.py::_migrate_columns` 的幂等 _add
  ③ 真库事实：`mbse.db` 的 PRAGMA table_info

纪律：
  · 默认 dry-run；`--apply` 才写，且写前用 `sqlite3.Connection.backup()` 全库备份。
  · 只加列、不回填业务值（owner/eval_set 空 = 未纳管，属如实状态，不编造负责人）。
  · 不建引用新列的索引；顺序判据常驻（本工程踩过「先建索引后补列」）。
  · 留痕含 before/after 列清单、行数、备份路径与**单步回滚 SQL**。
  · 变异自证：DDL / 迁移脚本 / 判据各喂一次篡改输入，必须翻红。

跑法：./.venv/Scripts/python.exe -X utf8 tools/_p0_d2_metadata_columns.py [--apply]
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from database.migrations.columns import add_column_if_missing  # noqa: E402

DB = REPO / "mbse.db"
BACKUPS = REPO / "backups"
SCHEMA_PY = REPO / "database" / "schema.py"
COLUMNS_PY = REPO / "database" / "migrations" / "columns.py"
OUT = REPO / "docs" / "评测" / "p0-4-d2-metadata-20261115.json"

# (表, 列, DDL)；tools.owner 由更早的迁移提供，不重复加。
TARGETS = (("skills", "owner", "TEXT DEFAULT ''"),
           ("skills", "eval_set", "TEXT DEFAULT ''"),
           ("skills", "last_reviewed", "TEXT DEFAULT ''"),
           ("tools", "eval_set", "TEXT DEFAULT ''"),
           ("tools", "last_reviewed", "TEXT DEFAULT ''"))

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def cols_of(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def ddl_of(src, table):
    m = re.search(r'CREATE TABLE IF NOT EXISTS %s \(.*?\)"""' % table, src, re.S)
    return m.group(0).replace(')"""', ")") if m else ""


def ddl_has_columns(schema_src, table, colnames):
    """纯判据：从 schema.py 抽 DDL 到内存库执行，逐列检查。变异测试直接改它的输入。"""
    ddl = ddl_of(schema_src, table)
    if not ddl:
        return False
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(ddl)
        got = set(cols_of(mem, table))
        return set(colnames) <= got
    except sqlite3.Error:
        return False
    finally:
        mem.close()


def migration_has_all(columns_src):
    """纯判据：5 条 _add 都在，且 tools.owner 的既有 _add 还在（不许误删）。"""
    need = ['_add("%s", "%s"' % (t, c) for t, c, _ in TARGETS]
    return (all(n in columns_src for n in need)
            and '_add("tools", "owner"' in columns_src)


def index_order_ok(columns_src):
    """引用新列的索引必须排在补列之后；当前一条都没有 ⇒ 通过。"""
    new_cols = {c for _, c, _ in TARGETS}
    first_add = min(columns_src.find('_add("%s", "%s"' % (t, c))
                    for t, c, _ in TARGETS)
    for m in re.finditer(r"CREATE INDEX[^)]*?ON\s+\w+\(([^)]*)\)", columns_src):
        refs = {x.strip().strip('`') for x in m.group(1).split(",")}
        if refs & new_cols and m.start() < first_add:
            return False
    return first_add > 0


def main():
    apply = "--apply" in sys.argv
    schema_src = SCHEMA_PY.read_text(encoding="utf-8")
    columns_src = COLUMNS_PY.read_text(encoding="utf-8")

    # ── [S] 三处真源静态对拍（含变异）──
    for table in ("skills", "tools"):
        need = [c for t, c, _ in TARGETS if t == table]
        ck("S1 schema.py 的 %s 新库 DDL 已带 %s" % (table, "/".join(need)),
           ddl_has_columns(schema_src, table, need))
    ck("S2 columns.py 的 5 条幂等 _add 齐全且 tools.owner 未被误删",
       migration_has_all(columns_src))
    ck("S3 没有排在补列之前的「引用新列」索引", index_order_ok(columns_src))
    ck("M1 变异（新库 DDL 去掉新列）⇒ 判据必须翻红",
       not ddl_has_columns(
           schema_src.replace("        owner TEXT DEFAULT '',             -- 业务负责人（空=未指派）\r\n", "")
                     .replace("        owner TEXT DEFAULT '',             -- 业务负责人（空=未指派）\n", ""),
           "skills", [c for t, c, _ in TARGETS if t == "skills"]))
    ck("M2 变异（迁移脚本少一条 _add）⇒ 判据必须翻红",
       not migration_has_all(columns_src.replace('_add("skills", "owner", "TEXT DEFAULT \'\'")', "", 1)))

    conn = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    before = {t: cols_of(conn, t) for t in ("skills", "tools", "agent_memory")}
    rows_before = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                   for t in ("skills", "tools", "agent_memory", "agents", "conversations")}
    conn.close()
    print("BEFORE skills=%d cols tools=%d cols；rows=%s"
          % (len(before["skills"]), len(before["tools"]), rows_before))
    ck("S4 前置：tools.owner 已存在（既有迁移提供，不重复加）",
       "owner" in before["tools"])
    missing = [(t, c) for t, c, _ in TARGETS if c not in before[t]]
    print("待补列：", missing)

    if not apply:
        print("DRY-RUN（未写盘）；加 --apply 执行备份 + 补列 + 留痕")
        print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0

    # ── 备份（与之前 P0 写库隔开，可单步回滚）──
    BACKUPS.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = BACKUPS / ("mbse.db.bak-before-p0-4-d2-%s.db" % ts)
    src = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    dst = sqlite3.connect(str(bak))
    src.backup(dst)
    nt_src = src.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    nt_dst = dst.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    dst.close()
    src.close()
    ck("B1 全库备份成功且 sqlite_master 对象数一致",
       nt_src == nt_dst and bak.stat().st_size > 0,
       "%s size=%d objects=%d" % (bak.name, bak.stat().st_size, nt_dst))

    # ── 执行补列（复用工程现成的幂等函数，不另写 ALTER）──
    w = sqlite3.connect(str(DB))
    added = []
    try:
        for t, c, ddl in TARGETS:
            if add_column_if_missing(w, t, c, ddl):
                added.append("%s.%s" % (t, c))
        w.commit()
        after = {t: cols_of(w, t) for t in ("skills", "tools")}
        rows_after = {t: w.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                      for t in rows_before}
        ck("A1 补列后 5 个新列全部存在",
           all(c in after[t] for t, c, _ in TARGETS), "实际新增=%s" % added)
        ck("A2 补列未改动任何表行数", rows_after == rows_before,
           "%s -> %s" % (rows_before, rows_after))
        probe = {}
        for t, c, _ in TARGETS:
            probe["%s.%s" % (t, c)] = w.execute(
                "SELECT COUNT(*) FROM %s WHERE `%s` IS NULL OR `%s`=''" % (t, c, c)
            ).fetchone()[0]
        ck("A3 新列默认值生效（全行空串，未编造负责人/评估集）",
           all(v == rows_before[k.split(".")[0]] for k, v in probe.items()), probe)
    finally:
        w.close()

    rollback = [("ALTER TABLE %s DROP COLUMN `%s`;" % (t, c))
                for t, c, _ in TARGETS if "%s.%s" % (t, c) in added]
    out = {
        "schema": "p0/d2-metadata-columns@1",
        "applied_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "targets": [{"table": t, "column": c, "ddl": d} for t, c, d in TARGETS],
        "backup": str(bak), "backup_objects": nt_dst,
        "before": {t: before[t] for t in ("skills", "tools")},
        "after": {t: after[t] for t in ("skills", "tools")},
        "rows_unchanged": rows_before,
        "added": added,
        "rollback_sql": rollback,
        "rollback_note": "单步回滚（SQLite %s 支持 DROP COLUMN）；若库已产生其它写入，"
                         "优先用 backup 整库恢复而不是 DROP COLUMN" % sqlite3.sqlite_version,
        "caveat": "只加列不回填业务值；owner/eval_set 空=未纳管，是如实状态",
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print("WROTE", OUT)
    print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
