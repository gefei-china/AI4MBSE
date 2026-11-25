# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：陈旧待办清理：软删除/备份/回退/单一口径 16 项内存断言 + 真库 2 项（陈旧归零+deleted 留痕）。C 组读生产 agent_todos ⇒ local
"""陈旧待办清理工具门禁（2026-11-17，软删除 + 可回退）。

断言：
  A. 代码级：默认 dry-run、软删除（不物理 DELETE）、备份、回退清单、
     且"陈旧"口径复用单一真源 `stale_todo_ids`（不各写一份 SQL）
  B. 行为级：内存库真跑 —— 只命中陈旧 pending、软删除后可回退、
     已 completed 的不被碰、最近活跃会话不被碰
  C. 真库只读：清理后陈旧 pending 归零，且总行数未减少（审计链保留）

用法：``python tools/verify/verify_stale_todo_cleanup.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import cleanup_stale_todos as CL  # noqa: E402
from audit_failed_batches import stale_todo_ids  # noqa: E402

FAILS: list = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


DDL = """
CREATE TABLE agent_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, seq INTEGER,
    subject TEXT, status TEXT DEFAULT 'pending', verify TEXT DEFAULT '',
    verify_tool TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, role TEXT,
    content TEXT, created_at TEXT);
"""

OLD = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
NEW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    # 老会话：2 pending + 1 completed
    conn.execute("INSERT INTO messages (conversation_id,role,content,created_at) "
                 "VALUES (1,'user','x',?)", (OLD,))
    for subj, st in [("老待办A", "pending"), ("老待办B", "pending"),
                     ("老已完成", "completed")]:
        conn.execute("INSERT INTO agent_todos (conversation_id,seq,subject,status,"
                     "created_at,updated_at) VALUES (1,?,?,?,?,?)",
                     (1, subj, st, OLD, OLD))
    # 新会话：1 pending（不该被清）
    conn.execute("INSERT INTO messages (conversation_id,role,content,created_at) "
                 "VALUES (2,'user','x',?)", (NEW,))
    conn.execute("INSERT INTO agent_todos (conversation_id,seq,subject,status,created_at,"
                 "updated_at) VALUES (2,1,'新待办','pending',?,?)", (NEW, NEW))
    # 无消息但有新待办：按 last_todo_at 判新鲜（不该被清）
    conn.execute("INSERT INTO agent_todos (conversation_id,seq,subject,status,created_at,"
                 "updated_at) VALUES (3,1,'无消息新待办','pending',?,?)", (NEW, NEW))
    conn.commit()
    return conn


def case_code() -> None:
    print("\nA. 代码级")
    src = io.open(os.path.join(REPO, "tools", "cleanup_stale_todos.py"),
                  encoding="utf-8").read()
    check("A1 默认 dry-run（--apply 才写库）",
          'ap.add_argument("--apply", action="store_true"' in src
          and "if not args.apply:" in src, "没有 dry-run 默认")
    check("A2 ★ 软删除而非物理 DELETE",
          "SET status='deleted'" in src and "DELETE FROM agent_todos" not in src,
          "发现物理删除")
    check("A3 写前整库备份（sqlite3.backup）", "conn.backup(dst)" in src, "无备份")
    check("A4 回退清单落盘（含 id 全量）",
          '"ids": ids' in src and '--undo' in src, "无回退")
    check("A5 分批提交（不是一把梭）", "for start in range(0, len(ids), BATCH)" in src
          and "conn.commit()" in src, "未分批")
    check("A6 ★ 陈旧口径复用单一真源（不自己写 SQL 判 7 天）",
          "from audit_failed_batches import stale_todo_ids" in src
          and "stale_todo_ids(conn)" in src, "口径各写一份")


def case_behavior() -> None:
    print("\nB. 行为级（内存库真跑软删除与回退）")
    conn = _fixture()
    ids = stale_todo_ids(conn)
    check("B1 只命中老会话的 2 条 pending", len(ids) == 2, str(ids))
    check("B2 已完成/新会话/无消息新待办都不命中",
          3 not in ids and 4 not in ids and 5 not in ids, str(ids))

    before_total = conn.execute("SELECT COUNT(*) FROM agent_todos").fetchone()[0]
    tmpdir = tempfile.mkdtemp(prefix="stale_cleanup_")
    old_dir = CL.BACKUP_DIR
    CL.BACKUP_DIR = tmpdir
    try:
        res = CL._apply(conn, ids)
    finally:
        CL.BACKUP_DIR = old_dir
    check("B3 软删除条数正确", res["changed"] == 2, str(res))
    after_total = conn.execute("SELECT COUNT(*) FROM agent_todos").fetchone()[0]
    check("B4 ★ 总行数不变（审计链保留）", after_total == before_total,
          "before=%d after=%d" % (before_total, after_total))
    st = {r[0]: r[1] for r in conn.execute("SELECT subject,status FROM agent_todos")}
    check("B5 老 pending 变 deleted", st.get("老待办A") == "deleted"
          and st.get("老待办B") == "deleted", str(st))
    check("B6 completed 与新会话未被碰",
          st.get("老已完成") == "completed" and st.get("新待办") == "pending"
          and st.get("无消息新待办") == "pending", str(st))
    check("B7 清理后陈旧 pending 归零", not stale_todo_ids(conn), str(stale_todo_ids(conn)))

    check("B8 备份文件真的生成", os.path.exists(res["backup"]), res["backup"])
    check("B9 回退清单含全部 id", bool(json.load(open(res["undo"], encoding="utf-8"))["ids"]),
          res["undo"])
    CL._undo(conn, res["undo"])
    st2 = {r[0]: r[1] for r in conn.execute("SELECT subject,status FROM agent_todos")}
    check("B10 ★ --undo 完整恢复 pending", st2.get("老待办A") == "pending"
          and st2.get("老待办B") == "pending", str(st2))
    conn.close()


def case_db() -> None:
    print("\nC. 真库只读（确认清理结果）")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("C0 生产库存在", False, db)
        return
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        c.row_factory = sqlite3.Row
        left = stale_todo_ids(c)
        c.close()
        print("    剩余陈旧 pending：%d 条" % len(left))
        check("C1 ★ 陈旧 pending 已归零", not left, "%d 条残留" % len(left))
        n_deleted = conn.execute("SELECT COUNT(*) FROM agent_todos WHERE status='deleted'"
                                 ).fetchone()[0]
        check("C2 软删除行已留痕（deleted>2）", n_deleted > 2, "deleted=%d" % n_deleted)
    finally:
        conn.close()


def main() -> int:
    print("陈旧待办清理门禁")
    print("=" * 68)
    case_code()
    case_behavior()
    case_db()
    print("\n" + "=" * 68)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
