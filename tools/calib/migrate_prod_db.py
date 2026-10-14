# -*- coding: utf-8 -*-
"""生产库迁移补跑（**需人工确认后执行**）—— 2026-10-14 链路验收发现的真问题。

## 为什么要这个脚本（这是本会话最该被记住的一条）
链路验收（`tools/calib/verify_link_acceptance.py`）报出 **L0 环境层红**：
**生产库从未跑过 `init_db`**。迁移代码一直存在
（`database/schema.py` / `database/migrations/columns.py`），
但迁移**只在服务启动时触发** —— 而服务启动路径没人走。

实测生产库缺失：
| 对象 | 状态 | 后果（对应能力**在真实环境从未生效**） |
|---|---|---|
| `orch_stream_events` 表 | **缺表** | 批次二 #11「编排关键事件可回放」 |
| `hil_confirmations.idem_key` 列 | **缺列** | 批次二 #06「写操作幂等」 |
| `tool_call_logs.run_id/task_key/failure_kind` | **缺 3 列** | 批次二 #12「工具调用可归因到编排」 |

⚠️ **这三项能力的门禁全绿** —— 因为门禁跑在**干净库**上（干净库有这些表/列），
而生产库没有。⇒ 这是本仓第 N 次「门禁存在≠门禁在起作用」的变种：
**这次是"门禁验的是另一个库"**。
⇒ 也是「静态字符串判据对作用域错误天生无能」的同族：判据说"代码里有这段迁移"，
   没说"生产库已经迁移过"。

## 执行方式（**必须人工确认**）
    # ① 先备份（脚本自动做，落在 backups/）
    ./.venv/Scripts/python.exe -X utf8 tools/calib/migrate_prod_db.py --dry-run
    # ② 确认无误后再真跑
    ./.venv/Scripts/python.exe -X utf8 tools/calib/migrate_prod_db.py --apply

## 安全设计
1. **默认 `--dry-run`**：只报告缺什么，不写任何东西；
2. **自动备份**：`backups/p1-<日期>-pre-migrate/mbse.db`（WAL 库用 backup()，不用 copy2）；
3. **只做加法**：`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN`，
   **不删表、不删列、不改数据**；
4. **跑完自检**：重新读列信息确认补齐。
"""
import argparse
import datetime
import os
import shutil
import sqlite3
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PROD_DB = os.path.join(ROOT, "mbse.db")

# 批次二/三新增的对象（**只列这些，不做全库体检** —— 目的是补已知缺口，不是重新初始化）
EXPECT_TABLES = ("orch_stream_events", "subtask_artifacts")
EXPECT_COLUMNS = (
    ("hil_confirmations", "idem_key", "TEXT DEFAULT ''"),
    ("tool_call_logs", "run_id", "INTEGER DEFAULT 0"),
    ("tool_call_logs", "task_key", "TEXT DEFAULT ''"),
    ("tool_call_logs", "failure_kind", "TEXT DEFAULT ''"),
    ("agent_tasks", "summary_json", "TEXT"),
    ("agent_tasks", "expected_output", "TEXT"),
)


def inspect_prod():
    conn = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    try:
        have_tables = {r[0] for r in
                       conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        miss_t = [t for t in EXPECT_TABLES if t not in have_tables]
        miss_c = []
        for tbl, col, _ in EXPECT_COLUMNS:
            if tbl not in have_tables:
                continue
            cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % tbl)}
            if col not in cols:
                miss_c.append((tbl, col))
        return miss_t, miss_c
    finally:
        conn.close()


def backup():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst_dir = os.path.join(ROOT, "backups", "p1-pre-migrate-%s" % ts)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "mbse.db")
    s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)                      # ★ WAL 库必须用 backup()
    d.close()
    s.close()
    return dst


def apply_migrations():
    """只跑「建表 + 补列」，不跑 seed、不跑其它迁移。"""
    done = []
    # ① 缺表
    from database.schema import init_schema
    init_schema()
    done.append("建表（init_schema，幂等 IF NOT EXISTS）")
    # ② 缺列
    from database.migrations.columns import _migrate_columns
    conn = sqlite3.connect(PROD_DB)
    try:
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()
    done.append("补列（_migrate_columns）")
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行迁移（默认只报告）")
    ap.add_argument("--dry-run", action="store_true", help="只报告（默认行为）")
    a = ap.parse_args()

    print("=" * 70)
    print("生产库迁移补跑 —— %s" % PROD_DB)
    print("=" * 70)
    if not os.path.exists(PROD_DB):
        print("生产库不存在，退出。")
        return 1

    miss_t, miss_c = inspect_prod()
    print("\n缺表：%s" % (miss_t or "无"))
    print("缺列：%s" % (", ".join("%s.%s" % x for x in miss_c) or "无"))

    if not miss_t and not miss_c:
        print("\n生产库已是最新，无需迁移。")
        return 0

    print("\n⇒ 这些缺失导致对应能力**在真实环境从未生效**：")
    for t in miss_t:
        print("   · 表 %s 缺失（批次二 #11 编排事件可回放）" % t)
    for tbl, col in miss_c:
        tag = ("批次二 #06 写操作幂等" if col == "idem_key" else
               "批次二 #12 工具调用可归因" if col in ("run_id", "task_key", "failure_kind") else "")
        print("   · 列 %s.%s 缺失%s" % (tbl, col, ("（%s）" % tag) if tag else ""))

    if not a.apply:
        print("\n[dry-run] 未做任何改动。确认无误后执行：")
        print("    ./.venv/Scripts/python.exe -X utf8 tools/calib/migrate_prod_db.py --apply")
        return 0

    print("\n[apply] ① 备份中…")
    dst = backup()
    print("        备份 → %s（%.1f MB）"
          % (os.path.relpath(dst, ROOT), os.path.getsize(dst) / 1048576.0))
    print("[apply] ② 执行迁移…")
    for d in apply_migrations():
        print("        %s" % d)

    print("[apply] ③ 自检…")
    mt2, mc2 = inspect_prod()
    if mt2 or mc2:
        print("        **仍未补齐**：缺表 %s / 缺列 %s" % (mt2 or "无", mc2 or "无"))
        return 1
    print("        生产库已补齐 ✅")
    print("\n提示：迁移只改**结构**（建表/加列），不触碰任何业务数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
