# -*- coding: utf-8 -*-
"""记忆周期调度（P0-2，2026-11-27）。

## 为什么需要（原方案 P0-2）
`MemoryService.run_maintenance()` 与 `memory_dream.dream()` 此前**都只有手动入口**
（管理端点 / 运维脚本）。后果是两条独立的"静默停摆"：

- **维护**：不写新记忆 ⇒ 永不清理（`deposit()` 里那次 `maintain_every` 触发也就永不发生）；
- **做梦**：候选包只有人点「预览」才有，"错笔记被纠正/近重复被合并"全靠人想起来。

所以本模块只做一件事：**到点往作业队列里提交作业**。判定与执行全在别处
（`services/job_handlers.py` 的 `memory_maintain` / `memory_dream`），本模块不碰数据。

## 三条设计判据（都是踩过的坑反过来写的）
1. **不以内存状态判"跑过没有"**：进程重启后内存归零，照内存判会**每次重启都补跑一次**。
   判据取 `job_jobs` 里的历史（`kind=... ORDER BY id DESC`）—— 那是唯一跨进程可信的事实。
2. **周期由"上次尝试时刻"推**，不是"定时器等 6 小时"：守护线程每 `check_interval_s`（默认 10 分钟）
   醒一次，只做一次 SELECT 判断题。好处是崩溃/关机错过的时间窗会在下次醒来时**立刻补上**。
3. **失败也算"尝试过"**：`MAX(created_at)` 不过滤 status。否则一个一直失败的作业会被
   每 10 分钟重投一次，把队列刷成噪声（真正该看的是 `job_jobs.error`，不是重投频率）。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("mbse.memory")

_state = {
    "thread": None,
    "stop": threading.Event(),
    "interval_s": 600,
    "enabled": False,
    "ticks": 0,
    "last": None,
    "submitted": 0,
}

#: 这两个 kind 必须由 `services/job_handlers.KINDS` 注册，否则提交等于往队列里丢垃圾。
KINDS = ("memory_maintain", "memory_dream")


def _cfg(key: str, default):
    try:
        from core import config as _cfg
        return _cfg.get("memory", key, default)
    except Exception as exc:  # noqa: BLE001
        # 配置读不到就用默认值，但必须留痕：静默回落会让"调度周期被配置改没了"看不出来
        logger.warning("memory-scheduler 读配置失败（用默认 %r）：%s: %s",
                       default, type(exc).__name__, str(exc)[:120])
        return default


def _conn():
    from database import get_db
    return get_db()


def _last_attempt(conn, kind: str):
    """最近一次**尝试**时刻（epoch 秒）；从未跑过 → None。

    ⚠️ `job_jobs.created_at` 是 `time.strftime` 写的**本地时间**（与 `agent_memory.created_at`
      的 UTC 是两套口径，别混着比 —— 本模块只用本地时间这一种）。
    """
    try:
        row = conn.execute(
            "SELECT MAX(created_at) FROM job_jobs WHERE kind=?", (kind,)).fetchone()
        ts = (row[0] if row else "") or ""
        if not ts:
            return None
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory-scheduler 解析上次尝试时刻失败（按'从未跑过'处理）：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        return None


def _last_attempt_day(conn, kind: str) -> str:
    """最近一次尝试的本地日期串（YYYY-MM-DD）；无记录 → 空串。"""
    try:
        row = conn.execute(
            "SELECT MAX(created_at) FROM job_jobs WHERE kind=?", (kind,)).fetchone()
        ts = (row[0] if row else "") or ""
        return ts[:10]
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory-scheduler 读上次日期失败：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        return ""


def _inflight(conn, kind: str) -> bool:
    """是否已有在途作业（queued/running）—— 防同一 kind 叠投。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM job_jobs WHERE kind=? AND status IN ('queued','running')",
            (kind,)).fetchone()
        return bool(row and int(row[0] or 0) > 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory-scheduler 查在途作业失败（按'无在途'处理）：%s: %s",
                       type(exc).__name__, str(exc)[:120])
        return False


def tick(conn=None, now: float | None = None) -> dict:
    """判一次"到点没有"，到点则提交作业。返回本次提交明细（供端点/门禁断言）。

    幂等：同一时刻重复调用不会重复提交（在途检查 + job_key 家族去重）。
    """
    own = False
    if conn is None:
        conn = _conn()
        own = True
    out = {"maintain": None, "dream": None, "skipped": []}
    try:
        from core import job_queue as jq
        # 前置：kind 必须在注册表里，否则作业永远 queued（静默挂死）
        _kinds = ()
        try:
            from services.job_handlers import KINDS as _JK
            _kinds = tuple(_JK)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory-scheduler 读 handler 注册表失败（跳过 kind 校验）：%s: %s",
                           type(exc).__name__, str(exc)[:120])
            _kinds = ()
        if _kinds and not set(KINDS) <= set(_kinds):
            out["skipped"].append("handler 未注册：%s" % (
                sorted(set(KINDS) - set(_kinds)),))
            return out
        now = time.time() if now is None else float(now)

        # ── ① 维护：每 maintain_interval_h 小时 ──
        _iv_h = max(1, int(_cfg("maintain_interval_h", 6) or 6))
        _last = _last_attempt(conn, "memory_maintain")
        _due = (_last is None) or ((now - _last) >= _iv_h * 3600)
        if _due and not _inflight(conn, "memory_maintain"):
            out["maintain"] = jq.submit(
                conn, "memory_maintain", {"lease_s": 900},
                job_key="memory_maintain:%s" % time.strftime("%Y%m%d%H", time.localtime(now)),
                reuse_terminal=False)
        elif not _due:
            out["skipped"].append("maintain 未到点（上次 %s，周期 %dh）" % (
                time.strftime("%m-%d %H:%M", time.localtime(_last)) if _last else "-", _iv_h))

        # ── ② 做梦预览：每日一次（本地时刻 ≥ maintain_dream_hour；<0 关闭）──
        _dh = int(_cfg("maintain_dream_hour", 3))
        if _dh < 0:
            out["skipped"].append("dream 已关闭（maintain_dream_hour<0）")
        else:
            _lt = time.localtime(now)
            _today = time.strftime("%Y-%m-%d", _lt)
            if _lt.tm_hour < _dh:
                out["skipped"].append("dream 未到点（%02d:00 后）" % _dh)
            elif _last_attempt_day(conn, "memory_dream") == _today:
                out["skipped"].append("dream 今日已跑")
            elif not _inflight(conn, "memory_dream"):
                out["dream"] = jq.submit(
                    conn, "memory_dream",
                    {"persist_wiki": True, "lease_s": 900},  # 只读对账 + 落 markdown 页
                    job_key="memory_dream:%s" % _today, reuse_terminal=False)
        if out["maintain"] or out["dream"]:
            _state["submitted"] += 1
        _state["ticks"] += 1
        _state["last"] = out
        return out
    except Exception as e:  # 调度器绝不能让主进程崩，但必须留痕
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        _state["last"] = out
        print("[memory-scheduler] 判定异常（不中断）：%s" % out["error"], flush=True)
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                # 关连接失败本身无害（sqlite3 重复 close 是 no-op），但**不留白**：
                # 静默兜底账本把它记为一条 debt，而这条 debt 没有任何真实风险。
                logger.debug("memory-scheduler 关闭连接失败（忽略）：%s", exc)


def start(check_interval_s: int = 600) -> bool:
    """启动守护线程（幂等）。返回是否**本次**真的启动了新线程。"""
    _state["enabled"] = bool(_cfg("maintain_scheduler_enabled", True))
    if not _state["enabled"]:
        print("[startup] 记忆周期调度未启用（memory.maintain_scheduler_enabled=off）",
              flush=True)
        return False
    t = _state["thread"]
    if t and t.is_alive():
        return False
    _state["interval_s"] = max(60, int(check_interval_s or 600))
    _state["stop"].clear()

    def _loop():
        while not _state["stop"].wait(_state["interval_s"]):
            try:
                tick()
            except Exception as e:  # tick 自己已兜底，这里只作最后一道
                print("[memory-scheduler] 循环异常（不中断）：%s" % str(e)[:160], flush=True)

    th = threading.Thread(target=_loop, daemon=True, name="memory-scheduler")
    th.start()
    _state["thread"] = th
    print("[startup] 记忆周期调度已启动（每 %ss 判定一次；维护 %sh 一次，做梦每日 %s 点后）"
          % (_state["interval_s"], _cfg("maintain_interval_h", 6),
             _cfg("maintain_dream_hour", 3)), flush=True)
    return True


def stop() -> None:
    _state["stop"].set()


def status() -> dict:
    th = _state["thread"]
    return {"enabled": _state["enabled"],
            "running": bool(th and th.is_alive()),
            "check_interval_s": _state["interval_s"],
            "ticks": _state["ticks"],
            "submitted": _state["submitted"],
            "last": dict(_state["last"] or {}),
            "kinds": list(KINDS),
            "promise": "只提交作业，不写记忆；落库仍需 HIL 批准"}
