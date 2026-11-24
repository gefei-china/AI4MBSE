# -*- coding: utf-8 -*-
"""刷新/离开浏览器时的**断连续跑**（detached stream）——第二档方案的服务端内核。

## 解决什么

现状（改动前）：一轮生成与 SSE 连接**强绑定**。`core/sse.py` 在客户端断连时
把事件源 `close()`，`execute_stream` 收到 GeneratorExit → 任务被掐断、只留部分固化。
用户刷新一次页面，正在跑的编排/建模就废了，只能从头重发。

本模块把"断连"拆成两种语义：

  * **主动停止**：前端先 `POST /interrupt` 再 abort ⇒ 服务端有停止标记 ⇒
    照旧停生成器（**绝不让已停止的任务在后台继续跑**，这是安全底线）。
  * **刷新 / 暂时离开**：没有停止标记 ⇒ 视为"人还会回来" ⇒
    把内层生成器转给**后台 asyncio 任务排空**，LLM/工具继续跑、帧继续落 `run_frames`；
    页面重载后凭游标调 `/subscribe` 从断点续显（纯读帧、不重跑流程）。
    若 `detach_grace_s`（默认 60s）内没有任何订阅端认领，后台任务 `aclose()` 生成器
    并正常收口（此时行为退回改动前，避免用户真关页面后白烧 token）。

## 与既有设施的分工（不要重复造轮子）

  * `run_frames` / `run_sessions`：**唯一真源**，帧游标与重放全在那里；
  * `GET /api/runs/{id}/subscribe`：已有的带游标续传端点，本模块不改其协议；
  * `run_stream.tail_max_s`：订阅端单次 tail 时长 —— 与这里的 detach_grace_s
    是两个不同方向的计时器，别混：前者管"订阅端愿意等多久"，后者管"没人订阅时后台还能活多久"。

## 为什么状态放内存而不是 DB

单进程部署（`uvicorn.run` 无 workers，见 core/runtime_state.py）；后台 task 持着
生成器对象与事件循环，本就无法跨进程共享。DB 只存 run 状态供**跨重载**探测，
内存 registry 只管**本进程内**这一次断连的处置。两者互补，不冲突。

## 三条不变量（改动前先读）

1. **停止标记优先于脱离**：`should_detach(user_stopped=True)` 恒为 False；
   且 `/interrupt` 会主动 `close_by_conv()` 关闭已脱离的流 —— 覆盖"先断连、
   后点停止"的竞态。已停止的任务在任何顺序下都不会在后台继续跑。
2. **游标只前进不倒退**：续显用 `parse_last_event_id` 解析的 seq，客户端从
   `id:` 行拿到"最后一个已渲染帧"，断点续传从 `seq+1` 开始（由既有
   `prepare_frame_replay` 保证）；重连时 `touch()` 续 activity 时钟。
3. **无人认领必收口**：activity 超过 grace 必须 `task.cancel()` → 生成器
   finally 里 `aclose()` → 正常收口，绝不允许孤儿生成器永久占 worker 线程继续烧 token。
"""
from __future__ import annotations

import time
from typing import Dict, Optional

__all__ = ["should_detach", "DetachedRegistry", "get_registry"]


def should_detach(user_stopped: bool, enabled: bool = True, run_id: int = 0) -> bool:
    """**纯函数**：断连时该不该转后台续跑。

    判据刻意收敛成三个参数，便于离线穷举（见 verify 脚本）：

      * user_stopped —— 前端先发了 `/interrupt`（主动停止）。**最高优先级**，
        True 一律返回 False：已停止的任务绝不允许在后台继续跑。
      * enabled      —— 配置开关 `session_resume.enabled`。
      * run_id       —— 帧日志 run 号；0 表示没建 run（帧日志关闭/异常），
        没有续显真源就没必要续跑，退回原行为。
    """
    if user_stopped:
        return False
    if not enabled:
        return False
    return int(run_id or 0) > 0


class _Detached:
    """一次断连续跑的运行时句柄。"""

    __slots__ = ("run_id", "conv_id", "task", "agen", "detached_at",
                 "last_activity", "claimed", "reason", "watcher")

    def __init__(self, run_id: int, conv_id: int, task, agen, now: float):
        self.run_id = int(run_id)
        self.conv_id = int(conv_id)
        self.task = task                    # asyncio.Task：后台排空任务
        self.agen = agen                    # 内层 _event_stream_inner 生成器
        self.detached_at = float(now)
        self.last_activity = float(now)     # touch 续时钟；watcher 据此判"无人认领"
        self.claimed = False                # 是否有订阅端认领（touch 后置 True）
        self.reason = ""                    # 收口原因（natural / grace_expired / user_stop）
        self.watcher = None                 # grace 看门狗 task（自然完成时一并 cancel）


class DetachedRegistry:
    """进程内的脱离任务登记表（按 run_id 索引）。

    只做四件事：登记 / 认领（touch）/ 到期收口 / 停止标记。真正的帧落库由原生成器
    继续完成，本类不碰数据库 —— 本模块整体被摘掉时，原有链路逐字节不变。
    """

    def __init__(self):
        self._runs: Dict[int, _Detached] = {}
        self._stopped_convs: Dict[int, float] = {}   # conv_id -> 停止标记时间戳

    # ── 主动停止标记 ──────────────────────────────────────────────────
    def mark_stopped(self, conv_id: int, now: Optional[float] = None) -> None:
        """`/interrupt` 调用时写入。断连判据读它，决定**不**续跑。"""
        self._stopped_convs[int(conv_id or 0)] = float(now if now is not None else time.monotonic())

    def clear_stopped(self, conv_id: int) -> None:
        """新一轮生成开始时清掉上一轮的停止标记，防 30s 内的旧标记误伤新轮。"""
        self._stopped_convs.pop(int(conv_id or 0), None)

    def consume_stopped(self, conv_id: int, window_s: float = 30.0,
                        now: Optional[float] = None) -> bool:
        """读取并**消费**停止标记（一次性，防旧标记误伤下一轮）。

        `window_s` 默认 30s：停止请求与 abort 之间有网络延迟，但不会到分钟级；
        超时未消费的标记自动作废。
        """
        cid = int(conv_id or 0)
        ts = self._stopped_convs.pop(cid, None)
        if ts is None:
            return False
        return (float(now if now is not None else time.monotonic()) - float(ts)) <= float(window_s)

    # ── 脱离任务登记 ──────────────────────────────────────────────────
    def register(self, run_id: int, conv_id: int, task, agen,
                 now: Optional[float] = None) -> _Detached:
        d = _Detached(run_id, conv_id, task, agen,
                      float(now if now is not None else time.monotonic()))
        self._runs[int(run_id)] = d
        return d

    def unregister(self, run_id: int) -> Optional[_Detached]:
        return self._runs.pop(int(run_id), None)

    def get(self, run_id: int) -> Optional[_Detached]:
        return self._runs.get(int(run_id))

    def find_by_conv(self, conv_id: int):
        cid = int(conv_id or 0)
        return [d for d in self._runs.values() if d.conv_id == cid]

    def touch(self, run_id: int, now: Optional[float] = None) -> bool:
        """订阅端认领：续 activity 时钟，取消 60s 收口倒计时。

        `subscribe` 循环每次 poll 都调一次 ⇒ `last_activity` 反映"还有人在看"；
        页面关掉后 subscribe 连接随之结束，时钟停止续写，grace 到点即收口。
        """
        d = self.get(run_id)
        if d is None:
            return False
        d.claimed = True
        d.last_activity = float(now if now is not None else time.monotonic())
        return True

    def claimed(self, run_id: int) -> bool:
        d = self.get(run_id)
        return bool(d and d.claimed)

    def expired_ids(self, grace_s: float, now: Optional[float] = None):
        """返回"activity 已超期"的 run_id（纯查询，供 watcher 收口）。"""
        t = float(now if now is not None else time.monotonic())
        return [rid for rid, d in self._runs.items()
                if (t - d.last_activity) >= float(grace_s)]

    def size(self) -> int:
        return len(self._runs)


_registry = DetachedRegistry()


def get_registry() -> DetachedRegistry:
    return _registry
