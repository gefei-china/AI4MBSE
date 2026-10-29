# -*- coding: utf-8 -*-
"""编排运行 Registry —— liveness 判据与孤儿回收（P0-2 第一步，2026-10-03）。

**为什么先做回收，而不是直接上完整 resume**
    LangGraph（checkpointer + `interrupt`/`resume`）与 Temporal（事件溯源 + 自动重投）
    给的是完整语义，但它们**都以"能识别哪些运行已经死掉"为前提**。本工程现状恰恰卡在这里：
    评估 §4.2 实测 `agent_tasks` 60 个批次里 **16 个残留在 planned/ready/blocked**，
    最老 4.3 天无人管 —— 既没被标记失败，也没有任何地方能看见它们，
    用户侧表现就是流水线一直在转圈；连带的麻烦还有：前台 SubTask 面板永远停在"执行中"。

    所以第一步只做三件**不改任何执行路径**的事（对现有链路零行为影响、零回归风险）：
      ① 定义 liveness：非终态 + 超过 TTL 没有 `updated_at` 推进 = 孤儿；
      ② 启动时显式失败化：`failed` + 写明 error + 保住上下文（可重试）；
      ③ 可观测：孤儿数量、最老年龄进 `/api/monitor`，把"隐性悬挂"变成看得见的指标。

    ⚠️ 诚实的边界：这一步**不是**自动 resume（那要有独立的 worker 队列，属 P0-1/P0-2 第二步）。
    它把"永远转圈"变成"明确失败 + 一键重跑"，并让"要不要做 worker"这个决策有数据支撑。

**时间口径（重要）**
    `agent_tasks.updated_at` 默认值是 `CURRENT_TIMESTAMP` → SQLite **UTC**；
    判据里必须用 `datetime('now')`（同样 UTC），**不可用 `datetime('now','localtime')`** ——
    本机时区为 UTC+8，用错口径会让 TTL 实际偏移 8 小时（要么误杀在跑的任务，要么永不回收）。
"""
import json

# 非终态：这些状态的任务在正常情况下应当持续推进；一旦长时间不动就是孤儿
ACTIVE_STATUSES = ("planned", "ready", "running", "blocked")
TERMINAL_STATUSES = ("done", "failed", "canceled")

_ORPHAN_REASON = ("编排中断：超过 %.0f 分钟无任何进展，判定为孤儿运行"
                  "（进程重启或执行线程丢失），已保留上下文可重跑")


def _ttl(cfg=None):
    try:
        from core import config as _c
        return int((cfg or _c).get("runtime", "orphan_ttl_s", 1800) or 1800)
    except Exception:
        return 1800


def find_orphans(conn, ttl_s: int | None = None):
    """返回超过 TTL 仍未推进的非终态任务行。

    仅读不改 —— 供端点与自检脚本使用；真正的改写只在 `reap_orphans` 里发生。
    """
    ttl = _ttl() if ttl_s is None else int(ttl_s)
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    sql = (f"SELECT id, run_id, conversation_id, task_key, title, status, updated_at, agent_id "
           f"FROM agent_tasks WHERE status IN ({marks}) "
           f"AND updated_at < datetime('now', ?) ORDER BY updated_at ASC")
    try:
        return list(conn.execute(sql, (*ACTIVE_STATUSES, "-%d seconds" % ttl)).fetchall())
    except Exception:
        return []     # 表不存在/列缺失：当作没有孤儿（不阻断启动）


def orphan_summary(conn, ttl_s: int | None = None) -> dict:
    """孤儿概览（用于可观测端点）：总数、涉及批次、最老年龄。"""
    rows = find_orphans(conn, ttl_s)
    if not rows:
        return {"orphan_tasks": 0, "orphan_runs": 0, "oldest_age_s": 0,
                "ttl_s": _ttl() if ttl_s is None else int(ttl_s)}
    try:
        oldest = conn.execute(
            "SELECT CAST((julianday('now') - julianday(MIN(updated_at))) * 86400 AS INTEGER) "
            "FROM agent_tasks WHERE status IN ('planned','ready','running','blocked') "
            "AND updated_at < datetime('now', ?)",
            ("-%d seconds" % (_ttl() if ttl_s is None else int(ttl_s)),)).fetchone()
        age = int(oldest[0]) if oldest and oldest[0] is not None else 0
    except Exception:
        age = 0
    return {"orphan_tasks": len(rows),
            "orphan_runs": len({r["run_id"] for r in rows}),
            "oldest_age_s": max(0, age),
            "ttl_s": _ttl() if ttl_s is None else int(ttl_s)}


def reap_orphans(conn, ttl_s: int | None = None, dry_run: bool = False, audit=True) -> dict:
    """把孤儿任务**显式失败化**，保住上下文供重跑。

    为什么不直接删除：删除 = 抹掉"这里发生过一次失败"的证据，下次还是不知道为什么卡住。
    为什么保上下文（context/result 一列不动）：重跑失败批次时仍能看到上一轮跑到哪一步。

    :return: {scanned, reaped, dry_run, ttl_s, task_ids, run_ids}
    """
    ttl = _ttl() if ttl_s is None else int(ttl_s)
    rows = find_orphans(conn, ttl)
    out = {"scanned": len(rows), "reaped": 0, "dry_run": bool(dry_run), "ttl_s": ttl,
           "task_ids": [r["id"] for r in rows],
           "run_ids": sorted({r["run_id"] for r in rows})}
    if not rows or dry_run:
        return out

    reason = _ORPHAN_REASON % (ttl / 60.0)
    # ── S5（2026-10-29）：孤儿回收**必须同时写 error_class** ────────────────
    # 实测缺口（真库）：全量 65 条 failed 里 **57 条（87.7%）** 是本函数写的孤儿回收，
    #   而 `error_class` 填充率只有 **2/65 = 3%** ——
    #   于是"编排子任务失败率 20.6%"这个数字无法按类别拆解，看起来像"编排逻辑不可靠"，
    #   实际绝大多数是**进程重启导致的失活**（`failure_class` 早已把它单列为
    #   `interrupted` 并与 `timeout` 区分：一个宿主挂了、一个逻辑停滞）。
    #   ⇒ 分类能力**早就在**（`services/failure_class.py` 且对存量 100% 覆盖），
    #     缺的只是这条 UPDATE 没把结果写进列里 —— 典型的「能力在、没接线」。
    # ⚠️ 不改变任何行为：状态仍是 failed、error 文本不变，只是补一列归因。
    # ⚠️ 分类失败不得阻断回收（归因是旁路）；分类器不可用时留空，**不编造**类别。
    try:
        from services.failure_class import classify as _cls
        _ek = str(_cls(reason, ok=False) or "")
    except Exception:
        _ek = ""
    try:
        for r in rows:
            # ⚠️ 必须**先试带 error_class，失败则退不带**：
            #   极老库可能尚无 `error_class` 列（同 task_queue.fail() 的处理）。
            #   若不带降级，一处 `no such column` 就会让**整批孤儿收不掉**
            #   —— 那比"少一列归因"严重得多（僵尸任务永久堆积）。
            try:
                conn.execute(
                    "UPDATE agent_tasks SET status='failed', error=?, error_class=?,"
                    " updated_at=CURRENT_TIMESTAMP WHERE id=?", (reason, _ek, r["id"]))
            except Exception:
                conn.execute(
                    "UPDATE agent_tasks SET status='failed', error=?,"
                    " updated_at=CURRENT_TIMESTAMP WHERE id=?", (reason, r["id"]))
        conn.commit()
        out["reaped"] = len(rows)
        out["error_class"] = _ek
    except Exception as e:      # noqa: BLE001
        # 回收失败不得阻断启动 —— 但必须留痕，否则"孤儿永远收不掉"会变成又一个静默失败
        try:
            conn.rollback()
        except Exception:
            pass
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        try:
            import logging
            logging.getLogger("mbse.runtime").warning("孤儿回收失败：%s", out["error"])
        except Exception:
            pass
        return out

    if audit:
        try:
            from core.audit import audit as _audit
            _audit("系统", "orphan_reap",
                   "启动时回收孤儿编排任务：%d 条 / %d 个批次（TTL=%ds）"
                   % (len(rows), len(out["run_ids"]), ttl), "success", conn=None)
        except Exception as e:  # pragma: no cover
            try:
                import logging
                logging.getLogger("mbse.runtime").warning("孤儿回收审计写入失败：%s", str(e)[:120])
            except Exception:
                pass
    return out


def active_runs(conn, limit: int = 20) -> list:
    """当前非终态批次概览（含是否已超 TTL）—— `/api/monitor` 用。

    与 `find_orphans` 的区别：这里**不论是否超时**都返回，因为在超时阈值内的一律是正常的、
    正在跑的运行；把它们和孤儿放在同一个视图里，运维才看得出"阈值设得合不合理"。
    """
    ttl = _ttl()
    try:
        rows = conn.execute(
            "SELECT run_id, conversation_id, COUNT(*) AS n, MIN(updated_at) AS oldest, "
            "CAST((julianday('now') - julianday(MIN(updated_at))) * 86400 AS INTEGER) AS age_s, "
            "GROUP_CONCAT(DISTINCT status) AS statuses "
            "FROM agent_tasks WHERE status IN ('planned','ready','running','blocked') "
            "GROUP BY run_id, conversation_id ORDER BY oldest ASC LIMIT ?", (int(limit),)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["is_orphan"] = int(d.get("age_s") or 0) > ttl
        d["ttl_s"] = ttl
        out.append(d)
    return out
