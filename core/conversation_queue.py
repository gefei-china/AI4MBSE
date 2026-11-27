# -*- coding: utf-8 -*-
"""同会话**串行轮次队列**（2026-11-26）。

背景：chat/stream 只有进程级全局闸（8 路），没有会话级互斥；前端带附件连续发送时
会 abort 旧 SSE 再起新流，而 abort 只断浏览器这一侧，服务端旧任务照跑 ——
用户看到的是「两条指令同时执行」，且两轮还会撞同一个 finalize_key。

本模块提供最小判据：
  · 同一 conv 同时只允许一轮 `begin()` 成功；其余请求必须排入队尾；
  · 前一轮 `end()` 后由调用方 `claim_next()` 取下一票，**保持 1 SSE : 1 轮**；
  · 未开始的票可 `cancel()` 撤回；已进入执行的票不可撤回（走 /interrupt）。

纪律：
  - 队列是**进程内存**，不写 messages —— 撤回前不产生任何库内用户消息，
    避免改变 finalize_key 的 `MAX(user id)` 基线污染正在跑的那一轮。
  - 带锁、有界、带 TTL；服务重启后队列自然清空，前端可据 GET /queue 重建显示。
  - 不接 LLM、不碰 DB，纯状态机，便于判据级单测。
"""
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

MAX_PER_CONV = 10
MAX_CONVS = 256
TTL_SEC = 600


@dataclass
class Turn:
    ticket_id: str
    conv_id: int
    user_key: str
    body: dict
    message: str
    created_at: float

    def view(self):
        return {
            "ticket_id": self.ticket_id,
            "conv_id": self.conv_id,
            "message": self.message[:120],
            "created_at": self.created_at,
        }


_LOCK = threading.RLock()
_QUEUES = defaultdict(deque)          # conv_id -> deque[Turn]
_ACTIVE = {}                         # conv_id -> ticket_id（None=直连任务）

# 持久化开关：判据级单测可关掉，避免污染真库；生产默认开。
_PERSIST = True


def set_persistence(on: bool) -> None:
    """仅供测试/离线判据使用：关闭后队列只留内存。"""
    global _PERSIST
    _PERSIST = bool(on)


def _db():
    from database import get_db
    return get_db()


def _persist_insert(t: Turn) -> None:
    if not _PERSIST:
        return
    try:
        c = _db()
        try:
            c.execute("""INSERT OR REPLACE INTO conversation_turn_queue
                (ticket_id, conversation_id, user_key, payload, message, created_at)
                VALUES (?,?,?,?,?,?)""",
                (t.ticket_id, t.conv_id, t.user_key,
                 json.dumps(t.body, ensure_ascii=False), t.message, t.created_at))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass    # 持久化失败不阻断排队主流程（队列内存态仍可用）


def _persist_remove(ticket_id: str) -> None:
    if not _PERSIST:
        return
    try:
        c = _db()
        try:
            c.execute("DELETE FROM conversation_turn_queue WHERE ticket_id=?", (ticket_id,))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


def restore() -> int:
    """启动时把库里未开始的票恢复进内存；已出队的票在 claim 时已删行。"""
    with _LOCK:
        _QUEUES.clear()
        _ACTIVE.clear()
        if not _PERSIST:
            return 0
        n = 0
        try:
            c = _db()
            try:
                rows = c.execute("""SELECT * FROM conversation_turn_queue
                                    ORDER BY created_at ASC""").fetchall()
            finally:
                c.close()
            now = time.time()
            for r in rows:
                created = float(r["created_at"] or 0)
                if now - created > TTL_SEC:
                    _persist_remove(r["ticket_id"])
                    continue
                try:
                    body = json.loads(r["payload"] or "{}")
                except Exception:
                    body = {}
                t = Turn(str(r["ticket_id"]), int(r["conversation_id"]),
                         str(r["user_key"] or ""), body, str(r["message"] or ""), created)
                _QUEUES[t.conv_id].append(t)
                n += 1
        except Exception:
            pass
        return n


def _purge_expired(conv_id, now=None):
    now = now or time.time()
    q = _QUEUES.get(conv_id)
    if not q:
        return
    while q and (now - q[0].created_at) > TTL_SEC:
        t = q.popleft()
        _persist_remove(t.ticket_id)
    if not q:
        _QUEUES.pop(conv_id, None)


def enqueue(conv_id: int, user_key: str, body: dict, message: str = "") -> dict:
    """追加一轮到会话队尾；返回票号与位次（1-based）。有界，超出拒绝。"""
    with _LOCK:
        now = time.time()
        if len(_QUEUES) >= MAX_CONVS and conv_id not in _QUEUES:
            return {"ok": False, "error": "排队会话数已达上限"}
        _purge_expired(conv_id, now)
        q = _QUEUES[conv_id]
        if len(q) >= MAX_PER_CONV:
            return {"ok": False, "error": "本会话排队任务已达 %d 条上限" % MAX_PER_CONV}
        t = Turn(uuid.uuid4().hex[:12], int(conv_id), str(user_key or ""),
                 dict(body or {}), str(message or ""), now)
        q.append(t)
        _persist_insert(t)
        return {"ok": True, "ticket_id": t.ticket_id, "position": len(q),
                "pending": len(q), "item": t.view()}


def pending(conv_id: int) -> list:
    with _LOCK:
        _purge_expired(conv_id)
        return [t.view() for t in (_QUEUES.get(conv_id) or [])]


def cancel(conv_id: int, ticket_id: str, user_key: str = "") -> dict:
    """撤回**未开始**的票。已开始（不在队列中）时返回 started=True，调用方改走 /interrupt。"""
    with _LOCK:
        _purge_expired(conv_id)
        q = _QUEUES.get(conv_id)
        if not q:
            return {"ok": False, "started": bool(_ACTIVE.get(conv_id) is not None),
                    "error": "该任务已开始或不存在"}
        for t in list(q):
            if t.ticket_id == ticket_id:
                q.remove(t)
                if not q:
                    _QUEUES.pop(conv_id, None)
                _persist_remove(ticket_id)
                return {"ok": True, "started": False, "pending": len(q)}
        return {"ok": False, "started": bool(_ACTIVE.get(conv_id) is not None),
                "error": "该任务已开始或不存在"}


def claim_next(conv_id: int) -> dict | None:
    """取队首一轮并出队；调用方拿到后必须开一条**新的** SSE 执行它。"""
    with _LOCK:
        _purge_expired(conv_id)
        q = _QUEUES.get(conv_id)
        if not q:
            return None
        t = q.popleft()
        if not q:
            _QUEUES.pop(conv_id, None)
        # 出队即删行：重启后不会重复执行已开始的轮次（只恢复未开始的票）
        _persist_remove(t.ticket_id)
        return {"ticket_id": t.ticket_id, "body": t.body, "item": t.view()}


def begin(conv_id: int, ticket_id=None) -> bool:
    """占住会话执行位。同会话已有任务在跑 → False（调用方应排队，不得并行）。"""
    with _LOCK:
        if conv_id in _ACTIVE:
            return False
        _ACTIVE[conv_id] = ticket_id or ""
        return True


def end(conv_id: int, ticket_id=None) -> None:
    """释放执行位； ticket_id 不匹配时不释放，防止旧流误放掉新一轮。"""
    with _LOCK:
        cur = _ACTIVE.get(conv_id)
        if cur is None:
            return
        if ticket_id and cur and str(cur) != str(ticket_id):
            return
        _ACTIVE.pop(conv_id, None)


def is_active(conv_id: int) -> bool:
    with _LOCK:
        return conv_id in _ACTIVE


def drop_conv(conv_id: int, conn=None) -> None:
    """会话删除时清掉其排队票（内存态也要跟着级联，否则刷新后仍显示已删会话的任务）。

    ★ 2026-11-27 真机实测：调用方（routers.delete_conversation）此时已在请求级连接上
      开了写事务；这里若再开第二条连接会自己等自己 30s（与 tool_offload.cleanup 同坑）。
      故允许传入 conn 复用请求连接；无 conn 时（测试/后台）才自己开。
    """
    with _LOCK:
        _QUEUES.pop(conv_id, None)
        _ACTIVE.pop(conv_id, None)
    if not _PERSIST:
        return
    own = conn is None
    c = conn or _db()
    try:
        c.execute("DELETE FROM conversation_turn_queue WHERE conversation_id=?", (conv_id,))
        if own:
            c.commit()
    except Exception:
        if own:
            try:
                c.rollback()
            except Exception:
                pass
    finally:
        if own:
            c.close()


def stats() -> dict:
    with _LOCK:
        return {"active": len(_ACTIVE), "queued": sum(len(q) for q in _QUEUES.values()),
                "sessions": len(_QUEUES)}
