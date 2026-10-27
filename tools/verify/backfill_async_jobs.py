# -*- coding: utf-8 -*-
"""一次性回填：把历史会话里**已提交但未收尾**的异步任务补登记进 `async_external_jobs`。

## 为什么要回填

「异步任务跨轮续跑」（`agent/async_jobs.py`）是 2026-10-27 才加的，
此前提交的导出任务从未被登记 ⇒ 机制上线后**看不到它们**，用户那几轮白等依旧无解。
本脚本从 `tool_call_logs.result` 里按**同一套判据**（`detect_job`）扫描真实历史，
把仍处于未完成态的任务补进表里；之后每轮开头系统就能自动查状态/自动读取归档。

## 纪律

- 只读 `tool_call_logs`，**不改任何既有表**；只往新表 `async_external_jobs` 插/更新；
- 判据与运行时**完全同源**（直接 import `agent.async_jobs.detect_job`），不另写一套；
- 幂等：重复运行不产生重复行（`UNIQUE(conversation_id, task_id)`）；
- 干跑开关 `--dry-run` 先看清单，确认后再落库。

用法：
    python tools/verify/backfill_async_jobs.py --dry-run
    python tools/verify/backfill_async_jobs.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

from agent import async_jobs as aj  # noqa: E402


def main():
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    aj.ensure_table(conn)

    # 按时间正序扫：同一 taskId 后出现的状态覆盖前面的（SUBMITTED → RUNNING → COMPLETED）
    rows = conn.execute(
        "SELECT id, conversation_id, tool_name, result, created_at FROM tool_call_logs "
        "WHERE result IS NOT NULL AND result != '' AND tool_type='mcp' "
        "AND conversation_id > 0 ORDER BY id").fetchall()
    print("[扫描] 工具日志 %d 条（tool_type=mcp）" % len(rows))

    found = {}
    for r in rows:
        job = aj.detect_job(r["result"])
        if not job:
            continue
        key = (r["conversation_id"], job["task_id"])
        found[key] = {"job": job, "tool": r["tool_name"],
                      "conv": r["conversation_id"], "log_id": r["id"],
                      "at": r["created_at"]}

    print("[识别] 异步任务 %d 个：" % len(found))
    open_cnt = 0
    for (conv, tid), v in sorted(found.items(), key=lambda kv: kv[0][0]):
        st = v["job"]["status"]
        mark = "★待续跑" if st in aj.OPEN_STATES else st
        print("   conv=%s task=%s tool=%s status=%s progress=%s file=%s (log#%s)"
              % (conv, tid, v["tool"], mark, v["job"].get("progress"),
                 v["job"].get("file_name") or "-", v["log_id"]))
        if st in aj.OPEN_STATES:
            open_cnt += 1

    if not dry:
        n = 0
        for (conv, tid), v in found.items():
            before = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE conversation_id=? AND task_id=?" % aj.TABLE,
                (conv, tid)).fetchone()[0]
            aj.record(conn, conv, "", v["tool"], v["job"])
            if not before:
                n += 1
        conn.commit()
        print("[写入] 新增 %d 行（其余为已存在，幂等跳过）" % n)
    else:
        print("[干跑] 未写入；去掉 --dry-run 即落库（其中未完成态 %d 个会被自动续跑）" % open_cnt)

    total = conn.execute("SELECT COUNT(*) FROM %s" % aj.TABLE).fetchone()[0]
    print("[表状态] async_external_jobs 现有 %d 行" % total)
    conn.close()


if __name__ == "__main__":
    main()
