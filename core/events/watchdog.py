# -*- coding: utf-8 -*-
"""事件域看门狗（2026-11-01，P1-5）—— 工单停滞检测与留痕。

━━━ 为什么不是"新建第五套守护"（先说不做什么）━━━
本工程已有五套守护（`main.py` lifespan 全部拉起）：
  ① 周期告警评估器    —— 全局 LLM/成本/登录/审计指标（300s）
  ② 编排自动重投守护  —— 失活编排批次自愈（300s）
  ③ 异步作业队列 worker —— 长任务租约/心跳/回收（2s）
  ④ MCP 健康巡检线程  —— MCP server 握手探测（300s）
  ⑤ BM25 索引预热     —— 启动后台预热
⇒ **已有能力覆盖了"进程挂了/编排死了/作业卡了/MCP 掉了"，本模块不重复造**。

本模块只补它们**都没覆盖**的一个缺口：
  ⑥ **事件域工单停滞** —— 一条事件已 admitted 并建了工单，但工单一直停在 open，
     没有任何人推进它。这在现有守护下**完全不可见**：
     编排守护只看 `agent_tasks`（编排批次），作业队列只看 `job_jobs`，
     告警评估器只看全局指标 —— 三者都不看 `ev_workitems`。
  ⇒ 表现就是"系统接到了事件、建了工单，然后**没人管**"——
    与用户报过的"流程没发起"是同型问题，只是更隐蔽（因为它确实发起了，只是没人接）。

━━━ 为什么默认只报不自动推进（与既有守护的口径一致）━━━
  · `core/orch_supervisor.py` 的自动重投 **默认 off**（会真调 LLM，代价真实）；
  · 本模块的自动推进同样**默认 off**（会真写图谱，副作用不可逆）。
  ⇒ 默认行为 = **只留痕 + 让面板可见**，人决定要不要推进。

━━━ 判定口径（纯函数，可单测）━━━
"停滞" = 工单处于 open/running（非终态）**且** updated_at 距今超过阈值。
⚠️ **只判非终态**：终态（done/failed/stopped）不算停滞，否则每次扫描都会把它们报一遍。
⚠️ **running 也算**：一个 running 的工单若长期无心跳，同样是"卡住了"。

⚠️ 与 `core/orch_supervisor.py` 的时区坑同源：
SQLite 的 CURRENT_TIMESTAMP 是 **UTC**，Python `datetime.now()` 是本地时间（UTC+8）。
⇒ 时间差**一律在 SQL 内用 julianday 算**，绝不取回 Python 做差。
"""

from __future__ import annotations

import json
import threading
import time

__all__ = [
    "STUCK_STATES", "stuck_candidates", "mark_stuck", "run_once",
    "start_watchdog", "stop_watchdog", "watchdog_status",
]

# 非终态 = 可能停滞的状态（终态不会被报）
STUCK_STATES = ("open", "running")

_state = {"thread": None, "stop": threading.Event(), "runs": 0, "last_at": 0.0,
          "last_found": 0, "config": {"enabled": False, "interval_s": 300,
                                     "stale_s": 1800}}


def _stuck_sql(conn, stale_s: int):
    """查停滞工单。

    ⚠️ 时间比较**在 SQL 内**用 julianday 完成（julianday('now') 也是 UTC，
    与 CURRENT_TIMESTAMP 同源 ⇒ 差值正确，不需要转换）。
    """
    sql = (
        "SELECT id, title, source, state, mode, actor, conversation_id, created_at "
        "FROM ev_workitems WHERE state IN (?,?) "
        "AND (julianday('now') - julianday(COALESCE(updated_at, created_at))) * 86400 > ? "
        "ORDER BY created_at ASC LIMIT 100"
    )
    return [dict(r) for r in conn.execute(sql, (STUCK_STATES[0], STUCK_STATES[1],
                                                int(stale_s))).fetchall()]


def stuck_candidates(conn, stale_s: int = 1800) -> list:
    """列出停滞工单（**只读**，供面板与告警使用）。"""
    return _stuck_sql(conn, stale_s)


def mark_stuck(conn, items: list) -> int:
    """把停滞工单写进 alert_events（留痕 ⇒ 面板可见、可追溯）。

    ⚠️ **不自动推进工单**：推进意味着真的执行动作（写图谱/调 LLM），
    代价真实且副作用不可逆 ⇒ 与 `orch_supervisor` 的 auto_resume 默认 off 同口径。
    ⇒ 这里只做"记录 + 让它可见"，推进由人显式发起。

    去重口径：同一工单在同一天只报一次（靠 alert_events 的既有去重），
    避免每 300s 刷一条同样的告警（那是噪音，比不报更糟）。
    """
    from core.audit import audit
    n = 0
    for it in items:
        try:
            detail = json.dumps({
                "workitem_id": it.get("id"), "source": it.get("source"),
                "state": it.get("state"), "mode": it.get("mode"),
                "actor": it.get("actor"), "title": (it.get("title") or "")[:80],
            }, ensure_ascii=False)
            conn.execute(
                """INSERT INTO alert_events
                     (rule_id, level, message, detail, status, created_at)
                   VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)""",
                ("ev_workitem_stuck", "warn",
                 "事件工单停滞未推进: %s" % (it.get("title") or it.get("id") or "")[:60],
                 detail, "open"))
            n += 1
        except Exception:
            continue
    conn.commit()
    return n


def run_once(conn, stale_s: int = 1800) -> dict:
    """跑一轮扫描。返回本轮结果（供自检与日志）。"""
    items = stuck_candidates(conn, stale_s)
    n = mark_stuck(conn, items) if items else 0
    _state["runs"] += 1
    _state["last_at"] = time.time()
    _state["last_found"] = len(items)
    return {"found": len(items), "marked": n, "runs": _state["runs"]}


def _loop(conn_factory, interval_s: int, stale_s: int) -> None:
    while not _state["stop"].wait(interval_s):
        try:
            with conn_factory() as c:
                r = run_once(c, stale_s)
            if r["found"]:
                print("[watchdog] 事件工单停滞 %d 条（已留痕，未自动推进）" % r["found"],
                      flush=True)
        except Exception as e:
            # 守护自身异常不得静默：留一行日志（对齐 orch_supervisor 的
            # "绝不在守护里吞异常而不留痕迹"）
            print("[watchdog] 事件工单看门狗异常: %s" % str(e)[:160], flush=True)


def start_watchdog(conn_factory, interval_s: int = 300, stale_s: int = 1800,
                   enabled: bool = False) -> bool:
    """启动看门狗线程。**默认不启用**（enabled=False），需显式打开。

    为什么默认 off：
      · 扫库本身有成本（每 300s 一次全表条件查询，可接受）；
      · 但**留痕会产生 alert_events 行** ⇒ 长期开着会积累噪音。
    ⇒ 灰度期建议：先开着看 3~5 天，统计 found 数量，
      若恒为 0 或极少 ⇒ 再考虑接入自动推进。
    """
    _state["config"] = {"enabled": bool(enabled), "interval_s": int(interval_s),
                         "stale_s": int(stale_s)}
    if not enabled:
        return False
    if _state["thread"] and _state["thread"].is_alive():
        return False
    _state["stop"].clear()
    t = threading.Thread(target=_loop, args=(conn_factory, int(interval_s), int(stale_s)),
                         daemon=True, name="ev-watchdog")
    t.start()
    _state["thread"] = t
    return True


def stop_watchdog() -> None:
    _state["stop"].set()


def watchdog_status() -> dict:
    """看门狗状态（供监控页）。"""
    t = _state["thread"]
    return {
        "running": bool(t and t.is_alive()),
        "config": dict(_state["config"]),
        "runs": _state["runs"],
        "last_found": _state["last_found"],
        "last_at": _state["last_at"],
        "note": "默认只留痕不自动推进；推进需人显式发起（与编排 auto_resume 同口径）",
    }
