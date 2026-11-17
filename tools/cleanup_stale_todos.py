# -*- coding: utf-8 -*-
"""清理陈旧 pending 待办（2026-11-17，用户明确授权）。

## 做什么

把"会话已无活动 ≥7 天"的 `agent_todos.status='pending'` 行改为 `status='deleted'`。
口径**不用本文件自己写**：直接复用 `tools/audit_failed_batches.py::stale_todo_ids`，
避免盘点与清理两处各自实现"陈旧"判定、最后数字对不上。

## 为什么是软删除

  · `list_todos` 默认排除 `deleted` ⇒ 不再进 harness 判定、不再挡闸、不再被自动续跑误捞；
  · `todo_gate_for_conv` 用 `include_deleted=True` 保留"这个会话曾经有过待办"的事实
    ⇒ 不会退化成"删光待办就自动完成"的后门；
  · 物理 DELETE 会永久丢掉审计链（agent_todos 是派发当时的存证），**绝不采用**。

## 安全护栏

  1. 默认 **dry-run**，只有显式 `--apply` 才写库；
  2. 写库前用 `sqlite3.backup()` 整库备份（不是复制文件，避免 WAL 下不一致）；
  3. 把被改的 id 全量写进 JSON，可用 `--undo <that.json>` 一键恢复；
  4. 分批提交（每批 50），每批后核对行数；
  5. 只动 `status='pending'` 且命中统一口径的行，其它状态一律不碰。

用法：
  python tools/cleanup_stale_todos.py                      # 只看会改哪些
  python tools/cleanup_stale_todos.py --apply              # 执行软删除
  python tools/cleanup_stale_todos.py --undo backups/stale_todos_xxx.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

from audit_failed_batches import stale_todo_ids  # noqa: E402

DB = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
BACKUP_DIR = os.path.join(REPO, "backups")
BATCH = 50


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _backup(conn, path: str) -> None:
    """sqlite3.backup() 在 WAL 模式下也能得到一致快照。"""
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)
    finally:
        dst.close()


def _preview(conn, ids: list) -> None:
    print("=" * 70)
    print("DRY-RUN 预览（未写库）")
    print("=" * 70)
    print("命中待办：%d 条" % len(ids))
    if not ids:
        return
    ph = ",".join("?" * min(len(ids), 900))
    rows = conn.execute(
        "SELECT conversation_id, status, COUNT(*) n, MIN(created_at) a, MAX(created_at) b "
        "FROM agent_todos WHERE id IN (%s) GROUP BY conversation_id, status "
        "ORDER BY n DESC LIMIT 15" % ph, ids[:900]).fetchall()
    print("  %-12s %-10s %-6s %-20s %s" % ("conv", "status", "条数", "最早", "最晚"))
    for r in rows:
        print("  %-12s %-10s %-6s %-20s %s" % (r[0], r[1], r[2], str(r[3])[:19], str(r[4])[:19]))
    print("…（仅显示前 15 组）" if len(rows) >= 15 else "")


def _apply(conn, ids: list) -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = _stamp()
    db_bak = os.path.join(BACKUP_DIR, "mbse_before_stale_cleanup_%s.db" % stamp)
    undo_path = os.path.join(BACKUP_DIR, "stale_todos_%s.json" % stamp)
    _backup(conn, db_bak)
    print("整库备份 -> %s" % db_bak)
    with open(undo_path, "w", encoding="utf-8") as f:
        json.dump({"created_at": datetime.now().isoformat(timespec="seconds"),
                   "db": DB, "ids": ids, "action": "pending -> deleted",
                   "backup_db": db_bak}, f, ensure_ascii=False, indent=1)
    print("回退清单 -> %s（--undo 可一键恢复）" % undo_path)

    done = 0
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        ph = ",".join("?" * len(chunk))
        cur = conn.execute(
            "UPDATE agent_todos SET status='deleted', updated_at=CURRENT_TIMESTAMP "
            "WHERE status='pending' AND id IN (%s)" % ph, chunk)
        conn.commit()
        done += cur.rowcount
        print("  已软删除 %d/%d" % (done, len(ids)))
    return {"changed": done, "undo": undo_path, "backup": db_bak}


def _undo(conn, path: str) -> None:
    d = json.load(open(path, encoding="utf-8"))
    ids = [int(x) for x in d.get("ids") or []]
    if not ids:
        print("回退清单为空，无需操作")
        return
    # 只恢复当前仍是 deleted 的行，避免覆盖用户 afterwards 的其它操作
    done = 0
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        ph = ",".join("?" * len(chunk))
        cur = conn.execute("UPDATE agent_todos SET status='pending' "
                           "WHERE status='deleted' AND id IN (%s)" % ph, chunk)
        conn.commit()
        done += cur.rowcount
    print("已恢复 %d/%d 条（只动仍为 deleted 的行）" % (done, len(ids)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行软删除（默认只预览）")
    ap.add_argument("--undo", default="", help="从回退清单恢复 pending 状态")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("无生产库：%s" % DB)
        return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        if args.undo:
            if args.apply:
                print("--undo 与 --apply 不能同时使用")
                return 2
            _undo(conn, args.undo)
            return 0
        ids = stale_todo_ids(conn)
        if not args.apply:
            _preview(conn, ids)
            print("\n要执行请加 --apply（软删除，保留审计链，可 --undo 回退）")
            return 0
        if not ids:
            print("无陈旧待办，无需清理")
            return 0
        res = _apply(conn, ids)
        left = stale_todo_ids(conn)
        print("\n结果：软删除 %d 条；剩余陈旧 pending %d 条" % (res["changed"], len(left)))
        if left:
            print("!! 仍有残留，需复查（不要直接删库）")
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
