# -*- coding: utf-8 -*-
"""follow-up 队列（第3 批 P1-② 阶段 1，2026-10-07）—— 用户中途补充指令的暂存与消费。

对标Pi 的 follow-up：模型**即将退出**时读一次待处理队列，有消息则**重启一轮**，
而不是把消息丢掉或打断当前轮。

═════════════════════════════════════════════════════════════════════════
★ 为什么队列**不能**挂在 `AgentPipeline` 实例上
═════════════════════════════════════════════════════════════════════════
本仓 `agent/__init__.py:25` 是 `agent = AgentPipeline()` —— **模块级单例**，
且 `routers/conversations.py:14` 直接 `from agent import agent` 全局复用。
⇒ 若把队列挂成 `self._pending_followup`，**并发会话之间会互相看到对方的输入**
（甲会话的补充指令被乙会话消费），这是**跨用户数据泄漏**，不是"偶发错乱"。

⇒ 本模块用**模块级字典 + 按 conversation_id 分槽 + 线程锁**：
  · 分槽 ⇒ 会话隔离（同MEMORY「权限隔离必须服务端强制」同族）
  · 锁   ⇒ SSE 在 worker 线程投递、路由在请求线程消费，无锁会丢消息/损坏list
  · 有界 ⇒ 队列无限增长会打爆内存（见`MAX_QUEUE_LEN` / `MAX_CONVS` / `TTL_SEC`）

═════════════════════════════════════════════════════════════════════════
★ 为什么不放在 `messages` 里即时注入（即"steering"）
═════════════════════════════════════════════════════════════════════════
即时注入会**破坏正在执行的 tool_call 序列**（Decoding AI 明确指出：
只在下边界注入）。而 follow-up 只在**轮次边界**消费 ⇒ 不触碰 tool_call。
本批**只做 follow-up，不做 steering**（方案 §4.3阶段 1的取舍）。
"""
import threading
import time

#: 单会话待处理消息条数上限（超出丢弃最旧的 —— 补充指令不是消息队列，
#: 攒几十条说明用户在狂点，前面的已经被后面的覆盖了）
MAX_QUEUE_LEN = 5

#: 最多跟踪多少个会话的队列（**防内存泄漏**：SSE 断连后队列没人消费）
MAX_CONVS = 256

#: 单条待处理指令的字符上限（超长直接拒收：follow-up 是"一句话补充"，
#: 不是文档粘贴；真要改正文应走附件/新消息）
MAX_ITEM_LEN = 2000

#: 待处理指令的存活期（秒）。超时即丢弃 ⇒ 「10 分钟前的补充」不该影响现在这轮。
TTL_SEC = 300.0

_LOCK = threading.Lock()
#: conv_id -> [(入队时刻, 文本), ...]（list 保证 FIFO；append/pop(0) 语义清晰）
_QUEUES: dict = {}


def _now() -> float:
    return time.time()


def push(conv_id: int, text: str) -> bool:
    """把一条 follow-up 投进指定会话的队列。返回是否成功入队。

    幂等/边界：
    - `conv_id <= 0` 或空文本 ⇒ 拒收（避免脏槽位）
    - 超长文本 ⇒ **截断**而非拒收（用户意图明确，截断比丢弃好）
    - 会话数超上限 ⇒ 先清最久未活跃的会话（LRU by 最后入队时间）
    """
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False
    s = (text or "").strip()
    if not s:
        return False
    if len(s) > MAX_ITEM_LEN:
        s = s[:MAX_ITEM_LEN] + "…（已截断）"
    t = _now()
    with _LOCK:
        if cid not in _QUEUES and len(_QUEUES) >= MAX_CONVS:
            # LRU 驱逐：找最后入队时间最早的会话
            try:
                victim = min(_QUEUES, key=lambda k: _QUEUES[k][-1][0] if _QUEUES[k] else 0)
                _QUEUES.pop(victim, None)
            except Exception:
                _QUEUES.clear()
        q = _QUEUES.setdefault(cid, [])
        q.append((t, s))
        while len(q) > MAX_QUEUE_LEN:
            q.pop(0)                     # 丢最旧（list头=最早）
    return True


def pop(conv_id: int) -> str:
    """取出并清空该会话的**全部**待处理指令（多条按入队顺序合并成一段）。

    ★ 为什么不是「取一条」：Pi 的 follow-up 一次读全部 pending input。
      本仓的续答（`_is_continuation_input`）也支持一次性多条补充。
    ★ 为什么合并成一段而不是追加多条 user 消息：连续多条 user 消息
      在部分 provider 上会被判为"连续发言"而报错，合并更稳。
    """
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return ""
    with _LOCK:
        q = _QUEUES.get(cid)
        if not q:
            return ""
        # 顺带清掉过期项（TTL 过滤），避免"很久前的补充"混进这一轮
        cutoff = _now() - TTL_SEC
        fresh = [s for t, s in q if t >= cutoff]
        _QUEUES.pop(cid, None)           # 取完即删⇒ 不会被重复消费
    if not fresh:
        return ""
    if len(fresh) == 1:
        return fresh[0]
    return "\n".join("【补充 %d】%s" % (i + 1, s) for i, s in enumerate(fresh))


def pending_count(conv_id: int) -> int:
    """该会话当前待处理条数（给 SSE 事件/监控看，不消费）。"""
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return 0
    with _LOCK:
        q = _QUEUES.get(cid) or []
        cutoff = _now() - TTL_SEC
        return len([1 for t, _s in q if t >= cutoff])


def drop(conv_id: int) -> None:
    """清空该会话队列（SSE 正常收尾时调用：这一轮结束了，没消费掉的不留给下一轮）。

    ★ 为什么需要：队列是"跨请求"的暂存。若上一轮正常结束但队列里还剩东西
      （例如用户在 done 之后、收口之前又投了一条），下一轮会**莫名多出**一段补充。
"""
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return
    with _LOCK:
        _QUEUES.pop(cid, None)


def stats() -> dict:
    """诊断快照（不消费）。"""
    with _LOCK:
        return {
            "convs": len(_QUEUES),
            "items": sum(len(v) for v in _QUEUES.values()),
            "max_convs": MAX_CONVS,
            "max_queue_len": MAX_QUEUE_LEN,
            "ttl_sec": TTL_SEC,
        }