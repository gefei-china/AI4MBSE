"""LLM 侧熔断器（P1-4，2026-10-06）。

**为什么需要（依据 docs/Agent生产化Harness对照核查-20261006.md §4）**：
文章把「熔断」与状态持久化、重试队列、监控告警、心跳检测、资源隔离并列为可靠层的 5 件事。
实测本仓可靠层已有 4.5 件（`orch_checkpoint` / `llm` 重试分类 / `alert_evaluator` /
`resume_stale_s` 心跳），**缺的正是熔断** —— `grep -ri circuit` 全仓零命中。

**与已有重试的区别（这是最容易混淆的点，必须说清）**：
- 重试（`_chat_with_retry`，2026-09-24 已有）：**单次调用内部**的「失败→退避→再试」，
  上限 3 次。解决「瞬时抖动」。
- 熔断（本模块）：**跨调用**的「连续失败 N 次 → 一段时间内不再打这个下游 → 半开试探」。
  解决「**下游持续不可用**」。
  ⇒ 没有熔断时，上游持续 429/503 的后果是：每个请求都要付满3 次退避等待
  （0.5s + 1s + …）才失败，**延迟被放大、调用配额被白耗**，而失败原因始终不透明。

**为什么不复用 `knowledge_pipeline/embedder.py:95-107` 的配额熔断**：
那是有先例的好实现，但它是 **embedding 专用**（认httpx.HTTPStatusError + quota 字样），
且是「一次性封锁时间戳」——不统计失败次数、不区分错误类型。
本模块做成**通用三态 + 按 provider 维度计数**，两者并存不冲突：
配额耗尽（确定性错误）仍由 embedder 侧处理，本模块管「连续可重试失败」。

**三态语义**：
- CLOSED（正常）：放行，记录成功/失败；
- OPEN（熔断中）：**直接拒绝，不再打下游**，返回结构化原因；
- HALF_OPEN（试探）：封锁期结束后**只放一个**探针请求 ——
  成功 ⇒ 回 CLOSED（下游已恢复）；失败 ⇒ 回 OPEN（重新封锁，计时重来）。
"""
import threading
import time

__all__ = ["CircuitBreaker", "get_breaker", "breaker_status", "reset_all"]


class _State:
    """单个 provider 的熔断状态（实例内持有，不跨进程共享）。"""

    __slots__ = ("fail_count", "opened_at", "probe_inflight")

    def __init__(self):
        self.fail_count = 0
        self.opened_at = 0.0
        self.probe_inflight = False


class CircuitBreaker:
    """按 provider 维度的三态熔断器（线程安全）。

    参数语义：
    - `fail_threshold`：连续失败多少次后 OPEN；
    - `reset_sec`：OPEN 后封锁多少秒，之后进入 HALF_OPEN 试探；
    - `success_threshold`：HALF_OPEN 中连续成功多少次才完全恢复（本实现取 1，符合"试探一次"直觉）。

    ⚠️ **`_now` 可注入是为了可测性**（2026-10-06 实测踩到）：
    封锁期原本硬编码 `max(1.0, reset_sec)`（生产合理：1 秒以下的熔断毫无意义），
    但这让 HALF_OPEN 逻辑**无法用秒级测试验证**（要等 ≥1 秒），
    门禁就只能测 CLOSED 态⇒ 半开路径实际处于"写了但从没跑到"的状态，
    正是本工程反复出现的「代码在、没接线」同族问题。
    做法：生产仍保留下界，但测试通过 `_now` 注入假时钟，**不牺牲生产约束**。
    """

    def __init__(self, name: str, fail_threshold: int = 5, reset_sec: float = 60.0,
                 min_reset_sec: float = 1.0):
        self.name = name
        self.fail_threshold = max(1, int(fail_threshold or 1))
        self.reset_sec = max(float(min_reset_sec), float(reset_sec or 1.0))
        self._min_reset_sec = float(min_reset_sec)
        self._lock = threading.Lock()
        self._states = {}          # provider_key -> _State
        self._tripped = {}         # provider_key -> 最近一次熔断原因（供 UI/日志）

    def _t(self) -> float:
        """当前时间。**仅测试可覆写**（子类化或直接改实例属性）。"""
        return time.time()

    # ── 状态查询 ────────────────────────────────────────────────
    def _state(self, key: str) -> _State:
        st = self._states.get(key)
        if st is None:
            st = _State()
            self._states[key] = st
        return st

    def status(self, key: str = "") -> dict:
        """返回熔断状态快照（供 `/api/monitor` 与告警使用）。

        ⚠️ **两种形态刻意统一为「都返回 {key: {...}}」**（2026-10-06 实测修）：
        最初实现是「传 key 返回该 key 的详情、不传返回全部」，
        于是 `status("p1")["phase"]` 报KeyError，而 `status()["p1"]["phase"]` 正常
        —— 同一函数两种形状，调用方必须知道自己传没传参，是典型的隐式契约陷阱。
        现在统一：`status(k)["k"]["phase"]` 与 `status()["k"]["phase"]` 同义。
        """
        with self._lock:
            keys = [key] if key else list(self._states)
            out = {}
            now = self._t()
            for k in keys:
                st = self._states.get(k)
                if not st:
                    continue
                if st.opened_at and (now - st.opened_at) >= self.reset_sec:
                    phase = "half_open"
                elif st.opened_at:
                    phase = "open"
                else:
                    phase = "closed"
                out[k] = {
                    "phase": phase,
                    "fail_count": st.fail_count,
                    "remaining_sec": max(0.0, round(self.reset_sec - (now - st.opened_at), 1))
                    if st.opened_at else 0.0,
                    "last_reason": self._tripped.get(k, ""),
                }
            return out

    # ── 闸门 ──────────────────────────────────────────────────
    def allow(self, key: str) -> tuple:
        """请求前问闸。返回 `(allowed: bool, reason: str)`。

        ⚠️ **HALF_OPEN 只放一个探针**：用 `probe_inflight` 标志占位，
        否则封锁期一过，所有并发请求会一起冲向下游 —— 那不是试探，是第二次雪崩。
        """
        with self._lock:
            # ⚠️ 只读路径**不建条目**（2026-10-06 实测修）：原先用 `self._state(key)`
            # 会让「只是问一次闸」的 provider 也被塞进 `_states`，
            # 于是 `status()` 里出现大量 fail_count=0 的健康 provider —— 虚报状态，
            # 监控面板会误以为"这些下游被熔断过"。
            st = self._states.get(key)
            now = self._t()
            if st is None or not st.opened_at:
                return True, ""
            if (now - st.opened_at) >= self.reset_sec:
                #封锁期已过 → HALF_OPEN，放**一个**探针
                if not st.probe_inflight:
                    st.probe_inflight = True
                    return True, "half_open_probe"
                return False, ("熔断半开试探中（已有探针在飞），本次请求不再打下游"
                               f"（provider={key}）")
            remain = self.reset_sec - (now - st.opened_at)
            return False, (f"LLM 熔断中：provider={key} 连续失败已被熔断，"
                           f"约 {remain:.0f}s 后试探恢复（本次调用未打下游）")

    def record_success(self, key: str) -> None:
        """成功 ⇒ 清零失败计数；若刚从 HALF_OPEN 恢复则关闭熔断。"""
        with self._lock:
            st = self._state(key)
            was_open = bool(st.opened_at)
            st.fail_count = 0
            st.probe_inflight = False
            if was_open:
                st.opened_at = 0.0
                self._tripped.pop(key, None)

    def record_failure(self, key: str, reason: str = "") -> None:
        """失败 ⇒ 累加计数；达阈值则 OPEN。"""
        with self._lock:
            st = self._state(key)
            st.probe_inflight = False
            st.fail_count += 1
            if st.opened_at:
                # 已在熔断（含半开试探失败）⇒ 重新计时，继续封锁
                st.opened_at = self._t()
                return
            if st.fail_count >= self.fail_threshold:
                st.opened_at = self._t()
                self._tripped[key] = (reason or "")[:200]

    def reset(self, key: str = "") -> None:
        """人工复位（运维/告警恢复后调用）。"""
        with self._lock:
            keys = [key] if key else list(self._states)
            for k in keys:
                self._states.pop(k, None)
                self._tripped.pop(k, None)


# ── 全局单例（按 provider 维度共用一个） ──────────────────────
_GLOBAL: dict = {}
_GLOBAL_LOCK = threading.Lock()


def get_breaker() -> CircuitBreaker:
    """取全局熔断器实例。阈值从 `core.config` 的 `llm` 分组读，缺省5 次 / 60秒。

    ⚠️ 阈值读的是**生效配置**（`config.get`，不是 DEFAULT_CONFIG）——
    否则改配置面板不生效（见 MEMORY「改配置模板 ≠ 改生效值」）。
    """
    fail_n, reset_s = 5, 60.0
    try:
        from core.config import get as _cfg_get
        fail_n = int(_cfg_get("llm", "circuit_breaker_fail_threshold", 5) or 5)
        reset_s = float(_cfg_get("llm", "circuit_breaker_reset_sec", 60) or 60)
    except Exception:
        pass
    with _GLOBAL_LOCK:
        br = _GLOBAL.get("main")
        if br is None or br.fail_threshold != max(1, fail_n) or br.reset_sec != max(1.0, reset_s):
            # 阈值变了 ⇒ 重建（顺带清掉旧计数，避免旧阈值下的失败计数污染新阈值）
            br = CircuitBreaker("llm", fail_threshold=fail_n, reset_sec=reset_s)
            _GLOBAL["main"] = br
        return br


def breaker_status(key: str = "") -> dict:
    try:
        return get_breaker().status(key)
    except Exception as e:
        return {"error": str(e)[:120]}


def reset_all() -> None:
    try:
        get_breaker().reset()
    except Exception:
        pass