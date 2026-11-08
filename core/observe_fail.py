"""观测链路失败留痕（2026-11-09）。

为什么需要这个模块
------------------
同一个根因在本工程反复出现：**观测失败被 `except Exception` 吞掉，指标分母
从此不可信**。已实证的三处：

  · `glossary.trace_query`  `except Exception: return 0` —— 写不进 trace 就当没发生
  · `agent/rag.py` 落 trace 的 `except Exception: pass`
  · `agent/rag.py` 落路由统计的 `except Exception: pass`

后果不是"少一条日志"。真实后果是**结论方向反了**：`query_trace` 6530 条算出的
7.24% 零命中率，分母只是"成功写入的行"，真实值只会更高；而看板上它看起来像
"已统计全量"。这与本工程已归档的「0 违规 vs 门禁没跑在日志上不可区分」同族。

三条纪律（缺一条就白做）
------------------------
1. **不冒泡**：`note()` 自身绝不抛 —— 观测失败永远不能阻断主链路。
   这条与"不静默"不冲突：**不抛 ≠ 不留痕**。
2. **不丢计数**：即使日志系统本身坏了，内存计数器照样加一，供 `snapshot()` 读。
   （日志是给人看的，计数是给门禁和看板看的；两者不能互相替代。）
3. **不编造**：没发生失败就返回 0，不返回"看起来健康"的估算值。

消费方
------
· 门禁 `tools/verify/verify_observe_fail_visible.py`：注入必然失败的 conn，
  断言"函数不抛 + 计数加一 + 有 sample"三者同时成立。
· 运维：`snapshot()` 可直接挂到健康检查里。
"""
from __future__ import annotations

import logging
import threading

#: 每个 where 最多留几条样本（完整栈会撑爆内存，也没有消费方）
_MAX_SAMPLES = 3
#: 日志限速：首次必打，之后每 N 次打一次（避免故障时被同一条日志刷满）
_LOG_EVERY = 50

_LOCK = threading.Lock()
_STATS: dict[str, dict] = {}

logger = logging.getLogger("mbse.observe")


def note(where: str, exc: BaseException, **ctx) -> None:
    """记录一次观测失败。**绝不抛异常**。

    参数：
      where —— 失败点标识（如 `trace_query` / `rag.trace` / `rag.route_stat`），
               它同时是计数与限速的键，所以必须是稳定字符串（不要拼进变量值）。
      exc   —— 捕获到的异常对象。
      ctx   —— 附加上下文（如 query 长度、route）。只存进样本，不参与逻辑。
    """
    entry = None
    count = 0
    try:
        with _LOCK:
            st = _STATS.get(where)
            if st is None:
                st = {"count": 0, "first": "", "last": "", "samples": []}
                _STATS[where] = st
            # ★ 先计数、再取消息：`str(exc)` 本身可能抛（异常对象的 __str__ 坏了），
            #   而"计数"是纪律 2 的底线 —— 它不能依赖消息字符串化成功。
            st["count"] += 1
            count = st["count"]
            try:
                msg = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            except Exception:                              # noqa: BLE001
                msg = "%s: <__str__ 失败>" % type(exc).__name__
            st["last"] = msg
            if not st["first"]:
                st["first"] = msg
            if len(st["samples"]) < _MAX_SAMPLES:
                st["samples"].append({"error": msg,
                                      "ctx": {k: str(v)[:120] for k, v in ctx.items()}})
            entry = st
    except Exception:                                  # noqa: BLE001
        # 连计数都做不了（极端情况：内存/线程异常）—— 此时只剩最后一条路：
        # 尽力打印一次。仍然不能抛。
        try:
            logger.error("observe_fail.note 自身失败 where=%s exc=%r", where, exc)
        except Exception:                              # noqa: BLE001
            pass
        return

    # 日志在锁外打：日志 handler 可能很慢（写文件/网络），不能占着锁
    try:
        if count == 1 or count % _LOG_EVERY == 0:
            logger.warning("观测失败 %s（累计 %d 次）：%s ctx=%s",
                           where, count, entry["last"] if entry else "", ctx or "-")
    except Exception:                                  # noqa: BLE001
        pass


def snapshot() -> dict:
    """返回当前累计的失败统计（深拷贝，调用方可随意改）。

    形状：`{where: {"count": n, "first": str, "last": str, "samples": [...]}}`
    """
    with _LOCK:
        return {k: {"count": v["count"], "first": v["first"], "last": v["last"],
                    "samples": [dict(s) for s in v["samples"]]}
                for k, v in _STATS.items()}


def total() -> int:
    """所有失败点合计次数（0 = 没有任何失败，不是"没统计"）。"""
    with _LOCK:
        return sum(v["count"] for v in _STATS.values())


def reset() -> None:
    """清空统计。仅供测试与门禁使用 —— 生产代码不得调用。"""
    with _LOCK:
        _STATS.clear()
