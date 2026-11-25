# -*- coding: utf-8 -*-
"""降级台账（P0：让错误可见）—— 静默兜底的分级、聚合与**执行过程中可见**的载体。

━━━ 为什么要有这个模块（架构评估 P0，2026-11-25）━━━
评估实测：全仓 1980 个 `except Exception` 里 **1361 个是静默吞掉（69%）**，
分布：agent 606/921、core 308/417、services 176/217、routers 206/318、repositories 65/107。

这个数字的后果不是"少几条日志"，而是**失败模式对所有人不可见**：
  · 用户看到"结果怪怪的"，但界面一切正常；
  · 开发者只能靠人工撞（本仓已归档的「图库 enabled 却查不到数据」就是
    `get_writer` 静默降级 Mock 造成的，靠人肉撞出来）；
  · 门禁只看"有没有抛异常"，吞掉的异常在门禁里等同于"没发生"。

━━━ 三条设计纪律 ━━━
1. **绝不抛**：`note()` 自身任何情况下都不向外抛。它不是业务路径的一环，
   不能让"记录失败"变成"新的失败"。（与 `core/observe_fail.py` 同一条纪律。）
   注意：**不抛 ≠ 不留痕** —— 这是本模块存在的全部理由。
2. **分级，而不是一律拉响**：三级语义在下面 `LEVELS` 里写死。一律 critical
   会让告警失去分辨力（本仓已有教训：什么都报 = 什么都不报）。
3. **两套消费者，互不替代**：
   · **全局聚合**（`snapshot()`）→ 运维看板 / 健康端点，回答"系统哪里有毛病"；
   · **请求级 Collector**（`collect()` + `drain()`）→ 会话流，回答"你**这一轮**
     的结果是怎么被降级出来的"。前者按 where 去重合并，后者要保序、逐条可见。

━━━ 为什么用 contextvar 而不是全局标记 ━━━
会话流是**同步生成器**，跑在 `core/sse.py` 为它起的 worker 线程里，而 worker 用
`copy_context()` 继承父请求上下文（该机制已于 2026-11-03 为 trace_id 修过一次）。
⇒ 只要在**创建流之前** set 一个采集器，生成器内部任意深度的 `note()` 都能 append
到同一个对象上，**不需要把 collector 一路当参数传进 623 KB 的 stream.py**。
这是"一处接入、全流生效"的唯一可行姿势。

⚠️ 边界（必须写清楚，避免过度承诺）：
   · 事件源自己新起的**裸线程**（未 copy_context）里的 `note()` 只会进全局聚合，
     不会进本轮采集器 —— 这类事件仍可在看板看到，只是不在这条消息的执行过程里。
   · 全局聚合是**进程内内存**，重启即清零。P0 不落库：db 已 563 MB / 184 张表，
     再加一张高频写表带来的迁移与写锁风险，大于它此刻的收益。持久化留给 P1。

━━━ 消费方 ━━━
· `routers/conversations.py`：chat/stream 建采集器 → 转发循环 `drain()` → SSE `event: degrade`
· `routers/health*`：`snapshot()` 供运维看板与门禁
· `tools/verify/verify_degrade_ledger.py`：注入受控失败，断言三级分类、聚合、drain 增量、
  以及"绝不抛"四条契约
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field, asdict

__all__ = [
    "LEVELS", "LEVEL_RANK", "CRITICAL", "DEGRADED", "COSMETIC",
    "DegradeEvent", "Collector", "collect", "current", "reset_current",
    "note", "drain", "snapshot", "total", "by_level", "reset", "normalize_level",
]

# ── 分级 ──────────────────────────────────────────────────────────────────
# 语义边界（判据不是"我觉得严重不严重"，而是"用户/运维能不能接受这个后果"）：
#   critical —— 主流程已受损，用户拿到的结果**不可信或不可用**（如正文没落库、
#               校验被跳过、写入失败但仍报成功）。必须让人当场看到。
#   degraded —— 功能降级但结果仍可用（如向量检索退化为 BM25、图库退化为内存 Mock、
#               推理服务不可用改用规则）。用户需要知道"这份结果少了什么"。
#   cosmetic —— 纯观测/装饰性失败（写 trace 失败、埋点失败、缩略图没生成）。
#               **允许静默**，但必须计数 —— 否则"没统计"与"零失败"不可区分
#               （本仓已归档的同族教训：0 违规 vs 门禁没跑，在日志上不可区分）。
CRITICAL = "critical"
DEGRADED = "degraded"
COSMETIC = "cosmetic"
LEVELS = (CRITICAL, DEGRADED, COSMETIC)
LEVEL_RANK = {CRITICAL: 0, DEGRADED: 1, COSMETIC: 2}

#: 单个 where 最多留几条样本（完整栈会撑爆内存，也没有消费方）
_MAX_SAMPLES = 3
#: 日志限速：首次必打，之后每 N 次打一次（故障时避免同一条日志刷满磁盘）
_LOG_EVERY = 50
#: 单个采集器最多保留多少条（一轮编排的降级不可能有几百条；
#: 超了说明是循环里的同一个失败在刷屏 ⇒ 合并计数，不再增长列表）
_COLLECTOR_CAP = 40

logger = logging.getLogger("mbse.degrade")


def normalize_level(level) -> str:
    """把任意输入收敛成三档之一。**不抛**：非法值按 degraded 处理并留一条警告。

    为什么不按 cosmetic 兜：非法的 level 多半是调用方写错了。若静默当 cosmetic，
    写错的降级会被当成"无需在意"过滤掉 —— 那正是本模块要消灭的东西。
    """
    s = str(level or "").strip().lower()
    if s in LEVEL_RANK:
        return s
    try:
        logger.warning("degrade.note 收到非法 level=%r，按 degraded 处理", level)
    except Exception:                                   # noqa: BLE001
        pass
    return DEGRADED


@dataclass
class DegradeEvent:
    """一次降级/兜底的**可见记录**。

    字段口径（前端直接展示，不要塞内部调试串）：
      where  —— 稳定标识 `模块.函数`，同一处的多次失败应共用它（用于合并与排行）
      level  —— critical / degraded / cosmetic
      error  —— `类型: 消息`（截断 200 字；不存完整栈，栈在日志里）
      impact —— **人话**的影响描述（"本轮检索只用了关键词，语义召回缺失"）。
                为空时前端退化为显示 where，不编造。
      count  —— 该 where 在本采集器内的累计次数（合并后 >1 说明重复发生）
      ts     —— 首次发生的时间戳（秒）
    """
    where: str
    level: str = DEGRADED
    error: str = ""
    impact: str = ""
    ctx: dict = field(default_factory=dict)
    count: int = 1
    ts: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = round(float(self.ts or 0), 3)
        return d


class Collector:
    """请求级采集器：保序、按 where 合并、支持增量 drain。

    为什么按 where 合并而不是无条件 append：一次 LLM 重试链可能在同一个
    `except` 里连挂 8 次，逐条 append 会让用户的执行过程里出现 8 张一样的卡
    （信息量 0，噪音 100%）。合并成一条带 `×8` 才是人想看的。
    """

    __slots__ = ("_events", "_index", "_cursor", "_lock")

    def __init__(self) -> None:
        self._events: list[DegradeEvent] = []
        self._index: dict[str, int] = {}      # where -> 在 _events 里的下标
        self._cursor = 0                      # drain 游标（已交付条数）
        self._lock = threading.Lock()

    def add(self, ev: DegradeEvent) -> None:
        with self._lock:
            i = self._index.get(ev.where)
            if i is not None:
                # 已存在：累加次数 + 升级级别（同 where 出现过 critical 就按 critical 展示）
                old = self._events[i]
                old.count += ev.count
                if LEVEL_RANK.get(ev.level, 1) < LEVEL_RANK.get(old.level, 1):
                    old.level = ev.level
                # impact/error 取**最新**：同一个 where 的失败原因可能变化
                if ev.impact:
                    old.impact = ev.impact
                if ev.error:
                    old.error = ev.error
                if ev.ctx:
                    old.ctx = ev.ctx
                # 游标回退到该条之前 —— 否则"合并后的更新"再也不会被 drain 出去
                # （前端只会看到第一次的 ×1，看不到后来变成 ×8）
                if i < self._cursor:
                    self._cursor = i
                return
            if len(self._events) >= _COLLECTOR_CAP:
                # 满了：不再新增（保序不变），只把计数算到最后一个，避免内存无界
                if self._events:
                    self._events[-1].count += 1
                return
            self._index[ev.where] = len(self._events)
            self._events.append(ev)

    def drain(self) -> list[dict]:
        """取走**尚未交付**的事件（含合并后的更新），返回 dict 列表。"""
        with self._lock:
            out = [e.to_dict() for e in self._events[self._cursor:]]
            self._cursor = len(self._events)
            return out

    def pending(self) -> int:
        with self._lock:
            return len(self._events) - self._cursor

    def all(self) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in self._events]

    def summary(self) -> dict:
        """收口汇总：按级别计数 + 是否有 critical（前端据此决定要不要醒目提示）。"""
        with self._lock:
            n = {lv: 0 for lv in LEVELS}
            for e in self._events:
                n[e.level if e.level in n else DEGRADED] += e.count
            return {"total": sum(n.values()), "by_level": n,
                    "critical": n[CRITICAL], "where": len(self._events)}


#: 当前请求的采集器（None = 不在采集范围，只进全局聚合）
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("mbse_degrade_collector", default=None)

# ── 全局聚合状态 ──────────────────────────────────────────────────────────
_G_LOCK = threading.Lock()
_G_STATS: dict[str, dict] = {}


def collect() -> Collector:
    """新建并**设为当前**采集器。调用方负责在收尾时 `reset_current()`。

    返回值必须保留引用：`drain()` 需要它（不能只依赖 `current()` ——
    生成器可能跑在另一条线程的 context 里，那条线程上的 `current()` 是副本）。
    """
    c = Collector()
    _CURRENT.set(c)
    return c


def current() -> Collector | None:
    return _CURRENT.get()


def reset_current() -> None:
    _CURRENT.set(None)


def note(where: str, exc: BaseException | None = None, level: str = DEGRADED,
         impact: str = "", **ctx) -> None:
    """登记一次降级/兜底。**绝不抛异常**（纪律 1）。

    :param where: 稳定标识，建议 `模块.函数`，如 `graph_db.get_writer`。
                  它是合并键与排行键 ⇒ **不要把变量值拼进来**（否则每个值一条，永远合并不了）。
    :param exc:   捕获到的异常（可为 None：例如"配置缺失所以走了兜底"这类非异常降级）。
    :param level: 见 LEVELS。
    :param impact: 人话影响描述，前端直接展示。允许为空（不编造）。
    :param ctx:   附加上下文，只进样本，不参与逻辑。

    两条路径同时写：
      · 全局聚合（按 where 合并，供看板/健康端点/门禁）；
      · 当前采集器（若存在，供本轮 SSE 可见）。
    任一路径失败都不影响另一条 —— 计数是底线，日志是可读性。
    """
    lv = normalize_level(level)
    msg = ""
    try:
        if exc is None:
            msg = ""
        else:
            # `str(exc)` 本身可能抛（异常对象的 __str__ 坏了）⇒ 必须单独兜
            try:
                msg = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            except Exception:                           # noqa: BLE001
                msg = "%s: <__str__ 失败>" % type(exc).__name__
    except Exception:                                   # noqa: BLE001
        msg = "<异常消息提取失败>"

    # ── ① 全局聚合 ──
    count = 0
    last = ""
    try:
        now = time.time()
        with _G_LOCK:
            st = _G_STATS.get(where)
            if st is None:
                st = {"count": 0, "level": lv, "first": msg, "last": msg,
                      "last_ts": now, "impact": impact, "samples": []}
                _G_STATS[where] = st
            st["count"] += 1
            count = st["count"]
            st["last"] = msg
            st["last_ts"] = now
            if impact:
                st["impact"] = impact
            # 级别只升不降：出现过 critical 就一直按 critical 报，避免"后来几次是 cosmetic"
            # 把一处真正的严重问题在排行里稀释掉
            if LEVEL_RANK.get(lv, 1) < LEVEL_RANK.get(st.get("level") or DEGRADED, 1):
                st["level"] = lv
            if len(st["samples"]) < _MAX_SAMPLES:
                st["samples"].append({"error": msg, "level": lv,
                                      "ctx": {k: str(v)[:120] for k, v in ctx.items()}})
            last = st["last"]
    except Exception:                                   # noqa: BLE001
        # 连聚合都做不了（极端：内存/线程异常）→ 只剩日志，仍不能抛
        try:
            logger.error("degrade.note 聚合失败 where=%s exc=%r", where, exc)
        except Exception:                               # noqa: BLE001
            pass

    # ── ② 请求级采集器 ──
    try:
        col = _CURRENT.get()
        if col is not None:
            col.add(DegradeEvent(where=where, level=lv, error=msg, impact=impact,
                                 ctx={k: str(v)[:200] for k, v in ctx.items()},
                                 count=1, ts=time.time()))
    except Exception:                                   # noqa: BLE001
        pass

    # ── ③ 限速日志（给人看；锁外打，handler 可能很慢）──
    try:
        if count == 1 or count % _LOG_EVERY == 0:
            logger.warning("降级[%s] %s（累计 %d 次）：%s%s",
                           lv, where, count, last or "-",
                           ("｜影响：" + impact) if impact else "")
    except Exception:                                   # noqa: BLE001
        pass


def drain(col: Collector | None = None) -> list[dict]:
    """取走采集器里尚未交付的事件；`col` 为空时用当前采集器。

    为什么允许显式传 col：会话流的生成器跑在 worker 线程，主协程持有的引用
    才是"同一个对象"。两条路径都支持，调用方不必关心上下文在哪个线程。
    """
    try:
        c = col if col is not None else _CURRENT.get()
        if c is None:
            return []
        return c.drain()
    except Exception:                                   # noqa: BLE001
        return []


def snapshot() -> dict:
    """全局聚合快照（深拷贝）。形状：

    `{"total": n, "by_level": {...}, "where": {where: {"count","level","first","last",
      "last_ts","impact","samples"}}, "worst": [按 级别+次数 排序的前 N 条]}`

    `total == 0` 的含义是**真的零降级**，不是"没统计"（本仓既有纪律：
    这两者在日志上必须可区分 ⇒ 本函数永远返回完整结构，不返回空 dict 表示未知）。
    """
    out_where: dict[str, dict] = {}
    by_level = {lv: 0 for lv in LEVELS}
    total = 0
    try:
        with _G_LOCK:
            for k, v in _G_STATS.items():
                lv = v.get("level") if v.get("level") in LEVEL_RANK else DEGRADED
                out_where[k] = {
                    "count": int(v.get("count") or 0), "level": lv,
                    "first": v.get("first") or "", "last": v.get("last") or "",
                    "last_ts": float(v.get("last_ts") or 0),
                    "impact": v.get("impact") or "",
                    "samples": [dict(s) for s in (v.get("samples") or [])],
                }
                by_level[lv] += int(v.get("count") or 0)
                total += int(v.get("count") or 0)
    except Exception:                                   # noqa: BLE001
        pass
    # 排行：先按级别（critical 最前），同级别按次数降序 —— 看板第一眼就该看到最严重的
    worst = sorted(out_where.items(),
                   key=lambda kv: (LEVEL_RANK.get(kv[1]["level"], 1), -kv[1]["count"]))[:20]
    return {"total": total, "by_level": by_level,
            "where_n": len(out_where),
            "where": out_where,
            "worst": [{"where": k, **v} for k, v in worst]}


def total() -> int:
    """全进程累计降级次数（0 = 没有任何降级，不是"没统计"）。"""
    try:
        with _G_LOCK:
            return sum(int(v.get("count") or 0) for v in _G_STATS.values())
    except Exception:                                   # noqa: BLE001
        return -1        # -1 明确表达"读不到"，绝不返回 0 冒充健康


def by_level() -> dict:
    return snapshot().get("by_level") or {lv: 0 for lv in LEVELS}


def reset() -> None:
    """清空全局与当前采集器。**仅供测试与门禁**，生产代码不得调用。"""
    try:
        with _G_LOCK:
            _G_STATS.clear()
        _CURRENT.set(None)
    except Exception:                                   # noqa: BLE001
        pass
