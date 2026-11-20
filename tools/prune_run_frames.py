# -*- coding: utf-8 -*-
"""帧日志容量维护：按天清理过期 run（连同其帧），可选 VACUUM 回收磁盘。

为什么要单独一个脚本：`services/run_frame_store.prune_runs` 是**按会话**触发的
（该会话再开新轮时才跑一次），长期不用的会话不会自己释放；全局回收需要一个能
一次扫全表的入口，而"删完还要不要 VACUUM"属于运维决策，不适合放进请求路径。

用法：
    .venv/Scripts/python.exe -X utf8 tools/prune_run_frames.py --days 7 --dry-run
    .venv/Scripts/python.exe -X utf8 tools/prune_run_frames.py --days 7
    .venv/Scripts/python.exe -X utf8 tools/prune_run_frames.py --days 7 --vacuum

⚠️ VACUUM 会锁库且耗时与库大小成正比（本库 500MB+ 量级），**只在确认没有在跑的流时执行**；
   参数 `--vacuum` 必须显式给，脚本不会替你决定。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.run_frame_store import KEEP_DAYS  # noqa: E402


def _db_path():
    return os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _has_table(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def main(argv=None):
    ap = argparse.ArgumentParser(description="按天清理帧日志（run_sessions / run_frames）")
    ap.add_argument("--days", type=int, default=KEEP_DAYS, help="保留天数（0=不按天删）")
    ap.add_argument("--vacuum", action="store_true", help="删除后执行 VACUUM 回收磁盘")
    ap.add_argument("--dry-run", action="store_true", help="只统计不删除")
    args = ap.parse_args(argv)

    path = _db_path()
    if not os.path.exists(path):
        print("库不存在：%s" % path)
        return 2
    size0 = _size(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if not (_has_table(conn, "run_sessions") and _has_table(conn, "run_frames")):
            print("库中没有帧日志两表（可能是改动前的库），无需清理")
            return 0
        tot_run = conn.execute("SELECT COUNT(*) FROM run_sessions").fetchone()[0]
        tot_fr = conn.execute("SELECT COUNT(*) FROM run_frames").fetchone()[0]
        print("清理前：run=%d / frames=%d / 库=%.1f MB" % (tot_run, tot_fr, size0 / 1048576.0))
        if int(args.days or 0) <= 0:
            print("--days<=0：不按天删，结束")
            return 0
        old = [int(r[0]) for r in conn.execute(
            "SELECT run_id FROM run_sessions WHERE status<>'running' "
            "AND started_at IS NOT NULL AND started_at < datetime('now', ?)",
            ("-%d days" % int(args.days),)).fetchall()]
        print("待删 run=%d（早于 %d 天，且非 running）" % (len(old), args.days))
        if args.dry_run or not old:
            if args.dry_run:
                print("--dry-run：未删除任何行")
            return 0
        n_fr = 0
        for rid in old:
            n_fr += conn.execute("DELETE FROM run_frames WHERE run_id=?", (rid,)).rowcount or 0
        for rid in old:
            conn.execute("DELETE FROM run_sessions WHERE run_id=?", (rid,))
        conn.commit()
        print("已删：run=%d / frames=%d" % (len(old), n_fr))
        if args.vacuum:
            print("VACUUM 开始（会锁库，耗时与库大小成正比）…")
            conn.execute("VACUUM")
            print("VACUUM 完成")
        size1 = _size(path)
        print("清理后：run=%d / frames=%d / 库=%.1f MB（释放 %.1f MB）"
              % (conn.execute("SELECT COUNT(*) FROM run_sessions").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM run_frames").fetchone()[0],
                 size1 / 1048576.0, (size0 - size1) / 1048576.0))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
