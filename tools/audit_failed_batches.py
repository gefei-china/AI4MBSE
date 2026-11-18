# -*- coding: utf-8 -*-
"""failed 批次与陈旧待办的**只读盘点**（2026-11-17，零写入、零 LLM、零服务依赖）。

为什么需要它：`orch_checkpoints.phase='failed'` 44 条、`agent_todos.status='pending'` 431 条，
这两个数字长期挂在台账上没人能解释 —— 本工具把它们拆成**可决策的清单**：
每一条都给出「它是什么 / 建议怎么处置」，处置动作全部留给人工执行（本工具**绝不写库**）。

产出两组表：
  一、failed 批次：逐条分类（已交付 / 进程死亡 / 真失败 / 无子任务）+ 建议动作
  二、pending 待办：按会话聚合 + 会话最后活跃时间 + 陈旧标记

分类口径（与 verify_failed_batch_resume.py C 组同源，**只读不写**）：
  · 全 done        ⇒ 子任务全完成但协议摘要聚合为 partial/failed ⇒ phase=failed。
                       2026-11-18 实证 43 批中 41 批有汇总消息、卡片 agg!=full、
                       0 批 summary_written=0 ⇒ **不是收尾崩溃**，是状态语义降级
  · interrupted    ⇒ 进程死亡，本可续跑（显式 include_failed=true）
  · internal/timeout ⇒ 真失败，不该重投
  · 无子任务        ⇒ 空批次

用法：``python tools/audit_failed_batches.py [--json 输出路径]``
退出码：0=正常 / 2=无生产库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")

STALE_TODO_DAYS = 7          # pending 待办超过这么多天没有会话活动 ⇒ 标"陈旧"


def _q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def classify_batch(conn, run_id: int) -> tuple:
    """单批次分类：返回 (类别, 任务统计, 建议动作)。"""
    rows = _q(conn, "SELECT status, COALESCE(error_class,'') ec FROM agent_tasks "
                    "WHERE run_id=?", (run_id,))
    if not rows:
        return "无子任务", {}, "无需处置（空批次）；可考虑清理台账"
    sts, ecs = {}, {}
    for r in rows:
        sts[r["status"]] = sts.get(r["status"], 0) + 1
        if r["ec"]:
            ecs[r["ec"]] = ecs.get(r["ec"], 0) + 1
    if "internal" in ecs or "timeout" in ecs:
        return "真失败", {"tasks": sts, "errors": ecs}, "不重投；按病因修代码后再人工重试"
    if "interrupted" in ecs:
        return "进程死亡", {"tasks": sts, "errors": ecs}, (
            "可续跑：POST /api/orchestration/runs/%d/resume?include_failed=true" % run_id)
    if set(sts) == {"done"}:
        return "已交付", {"tasks": sts, "errors": ecs}, (
            "非崩溃：摘要聚合 partial/failed 使 phase=failed，汇总与卡片已如实标注；"
            "不重投（summary 已写，重投=重复消息）；改 phase 须人工评估")
    return "其他", {"tasks": sts, "errors": ecs}, "人工定性"


def audit_failed_batches(conn) -> list:
    out = []
    for ck in _q(conn, "SELECT run_id, conversation_id, phase, attempt_count, "
                       "summary_written, created_at, updated_at, "
                       "substr(user_input,1,40) ui FROM orch_checkpoints "
                       "WHERE phase='failed' ORDER BY updated_at"):
        cat, extra, action = classify_batch(conn, int(ck["run_id"]))
        out.append({"run_id": ck["run_id"], "conversation_id": ck["conversation_id"],
                    "attempt_count": ck["attempt_count"],
                    "summary_written": ck["summary_written"],
                    "created_at": ck["created_at"], "updated_at": ck["updated_at"],
                    "user_input": ck["ui"], "category": cat, "detail": extra,
                    "action": action})
    return out


def audit_stale_todos(conn) -> dict:
    """pending 待办按会话聚合 + 会话最后活跃时间。"""
    rows = _q(conn, """
        SELECT t.conversation_id cid, COUNT(*) n, MIN(t.created_at) first_at,
               MAX(COALESCE(t.updated_at, t.created_at)) last_todo_at,
               (SELECT MAX(created_at) FROM messages m WHERE m.conversation_id=t.conversation_id)
                   last_msg_at,
               SUM(t.verify_tool != '') with_tool
        FROM agent_todos t WHERE t.status='pending'
        GROUP BY t.conversation_id ORDER BY n DESC""")
    today = datetime.now()
    for r in rows:
        _last = r.get("last_msg_at") or r.get("last_todo_at") or ""
        try:
            _d = datetime.strptime(_last[:19], "%Y-%m-%d %H:%M:%S")
            r["idle_days"] = round((today - _d).total_seconds() / 86400.0, 1)
        except Exception:
            r["idle_days"] = None
        r["stale"] = bool(r["idle_days"] is not None and r["idle_days"] >= STALE_TODO_DAYS)
    return {"conversations": rows,
            "total_pending": sum(r["n"] for r in rows),
            "stale_conversations": sum(1 for r in rows if r["stale"]),
            "stale_pending": sum(r["n"] for r in rows if r["stale"])}


def stale_todo_ids(conn, days: int = STALE_TODO_DAYS) -> list:
    """待清理的 pending 待办 id —— **唯一口径**，盘点与清理都必须用它。

    ★ 2026-11-17：清理工具若自己写一遍"7 天没活动"的 SQL，迟早与盘点
      工具漂移（本仓同族：同一语义两处各写一份，最后对不上）。
      这里直接消费 `audit_stale_todos` 的判定结果，只把会话级结论展开成行级 id。
    """
    info = audit_stale_todos(conn)
    cids = [int(r["cid"]) for r in info["conversations"] if r["stale"]]
    if not cids:
        return []
    ph = ",".join("?" * len(cids))
    return [int(r[0]) for r in conn.execute(
        "SELECT id FROM agent_todos WHERE status='pending' AND conversation_id IN (%s) "
        "ORDER BY conversation_id, COALESCE(seq, id), id" % ph, cids).fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="同时把结果写成 JSON 文件")
    ap.add_argument("--limit", type=int, default=50, help="每组最多打印多少行")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("无生产库：%s" % DB)
        return 2
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row

    failed = audit_failed_batches(conn)
    todos = audit_stale_todos(conn)
    conn.close()

    print("=" * 78)
    print("一、orch_checkpoints.phase='failed' 批次盘点（%d 条，只读）" % len(failed))
    print("=" * 78)
    import collections
    by_cat = collections.Counter(f["category"] for f in failed)
    for k, v in by_cat.most_common():
        print("  %-8s %d 条" % (k, v))
    print()
    print("  %-7s %-8s %-9s %-5s %-11s %s" % ("run", "类别", "attempt", "sw", "updated", "建议动作"))
    for f in failed[:args.limit]:
        print("  %-7d %-8s %-9d %-5s %-11s %s"
              % (f["run_id"], f["category"], f["attempt_count"],
                 f["summary_written"], (f["updated_at"] or "")[:10], f["action"][:60]))

    print()
    print("=" * 78)
    print("二、agent_todos.status='pending' 盘点（%d 条 / %d 个会话，只读）"
          % (todos["total_pending"], len(todos["conversations"])))
    print("=" * 78)
    print("  陈旧（会话无活动 ≥%d 天）：%d 个会话 / %d 条待办"
          % (STALE_TODO_DAYS, todos["stale_conversations"], todos["stale_pending"]))
    print()
    print("  %-10s %-5s %-5s %-11s %-9s %s" % ("conv", "条数", "带工具", "最后活跃", "闲置天", "标记"))
    for r in todos["conversations"][:args.limit]:
        print("  %-10d %-5d %-5d %-11s %-9s %s"
              % (r["cid"], r["n"], r["with_tool"], (r.get("last_msg_at") or "")[:10],
                 r["idle_days"] if r["idle_days"] is not None else "?",
                 "陈旧" if r["stale"] else ""))
    print()
    print("说明：本工具只读。任何清理/修正都须人工确认后另行执行（本仓纪律：")
    print("      盘点 ≠ 授权删除；agent_todos 是审计链的一部分）。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"failed_batches": failed, "stale_todos": todos}, f,
                      ensure_ascii=False, indent=1)
        print("已写出：%s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
