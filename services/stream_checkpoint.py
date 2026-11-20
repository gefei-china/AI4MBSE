# -*- coding: utf-8 -*-
"""流式输出检查点：节流落盘 + 服务重启后固化（2026-11-18）。

解决的具体问题（真库事故 conv 999999207）：
  user 消息已落库、chat/stream 已启动，33 秒后服务被 `taskkill /F` 强制终止。
  进程**没有任何执行收尾代码的机会**：
    · 前端 /messages/partial 来不及调；
    · 后端 GeneratorExit / CancelledError 固化分支不会跑。
  ⇒ 用户已看到的数千字输出全部蒸发，且不可恢复
    （服务端不记 SSE 正文，前端只缓存输入框草稿）。

与既有兜底的关系（不要重复造）：
  · 用户点停止 → 前端调 /messages/partial 固化            —— 已有，保留
  · 客户端断开 → finally 里 _persist_partial_stream 固化  —— 已有，保留
  · **进程被强杀** → 上面两条都不执行 → 本模块兜底        —— 本模块新增

纪律：
  · 所有写操作 fail-open（失败静默），绝不因检查点失败影响主链路；
  · 固化文案必须如实说明是"服务重启导致中断"，不假装正常完成；
  · 去重：若最后一条 assistant 已含同样正文，跳过（防正常完成后残留 checkpoint 双写）。
"""
import json
import time

from database import db_conn


# 节流阈值（下一步可由 config 覆盖；先写死合理值，避免多一处可漂移配置）
MIN_CHARS = 2000        # 每累计 2000 字符至少写一次
MIN_SECONDS = 5.0       # 或每 5 秒写一次（长间隔 token 也能留痕）


class Throttle:
    """一次流内的节流状态。仅内存，进程死了就没了 —— 这正是要靠 DB 兜底的原因。"""

    def __init__(self):
        self.last_n = 0
        self.last_t = time.time()

    def should(self, n: int) -> bool:
        if n < 4:
            return False
        return (n - self.last_n >= MIN_CHARS) or (time.time() - self.last_t >= MIN_SECONDS)

    def mark(self, n: int):
        self.last_n = n
        self.last_t = time.time()


def save(conversation_id: int, content: str, exec_info: dict) -> bool:
    """节流落盘当前已生成的正文与执行过程（幂等 upsert）。失败返回 False 且不抛。"""
    if not conversation_id or not content:
        return False
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO stream_checkpoints (conversation_id, content, exec_json, updated_at) "
                "VALUES (?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "  content=excluded.content, exec_json=excluded.exec_json, "
                "  updated_at=CURRENT_TIMESTAMP",
                (int(conversation_id), str(content),
                 json.dumps(exec_info or {}, ensure_ascii=False, default=str)[:20000]))
            conn.commit()
        return True
    except Exception:
        return False


def clear(conversation_id: int) -> bool:
    """本轮正常收口后删除检查点。失败返回 False 且不抛。"""
    if not conversation_id:
        return False
    try:
        with db_conn() as conn:
            conn.execute("DELETE FROM stream_checkpoints WHERE conversation_id=?",
                         (int(conversation_id),))
            conn.commit()
        return True
    except Exception:
        return False


def recover_all() -> int:
    """服务启动时把所有残留检查点固化成 assistant 消息，返回固化条数。

    ★ 为什么启动时**全部**固化、不做时间过滤：
      服务刚启动 ⇒ 不可能存在"正在跑的流"（所有流都随旧进程结束了）。
      任何残留都只可能是上次进程异常终止留下的。
      单机私有化部署（本项目）不存在双进程并发启动的场景。

    ⚠️ 幂等：最后一条 assistant 若已包含同样正文（正常完成后 clear 失败），跳过不双写。
    """
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT conversation_id, content, exec_json, updated_at "
                "FROM stream_checkpoints ORDER BY updated_at").fetchall()
    except Exception:
        return 0

    n_fixed = 0
    for r in rows or []:
        try:
            cid = int(r["conversation_id"])
            body = str(r["content"] or "").strip()
            if not body:
                clear(cid)
                continue
            with db_conn() as conn:
                last = conn.execute(
                    "SELECT id, content FROM messages WHERE conversation_id=? "
                    "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                if last and last["content"]:
                    prev = (last["content"] or "").strip()
                    # 正常落库的内容是 checkpoint 的前缀/全量 ⇒ 已写过，不重复
                    if body.startswith(prev[:200]) and len(prev) >= 200:
                        clear(cid)
                        continue
                    if prev.startswith(body[:200]) and len(body) >= 200:
                        clear(cid)
                        continue
            # 闭合流式输出里未配对的 ``` 围栏，否则说明文字会落进代码块内部
            try:
                from agent.final_content import close_open_fences
                body = close_open_fences(body)
            except Exception:
                pass
            body += ("\n\n> ⚠️ 输出因服务重启中断（以上为已生成内容，"
                     "可直接重新发送这条消息继续）")
            _card = "{}"
            try:
                _ej = r["exec_json"] or "{}"
                _x = json.loads(_ej) if isinstance(_ej, str) else _ej
                if isinstance(_x, dict) and _x:
                    _card = json.dumps({"exec": _x}, ensure_ascii=False, default=str)
            except Exception:
                pass
            with db_conn() as conn:
                _cur = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
                    "VALUES (?,?,?,?,?)", (cid, "assistant", body, "text", _card))
                conn.commit()
            try:
                from core.write_log import wrote as _wlog
                _wlog("msg_checkpoint_recover", msg_id=_cur.lastrowid,
                      conv_id=cid, chars=len(body))
            except Exception:
                pass
            clear(cid)
            n_fixed += 1
            print("[stream-checkpoint] conv=%s 固化重启前的 %d 字输出" % (cid, len(body)),
                  flush=True)
        except Exception as e:
            print("[stream-checkpoint] conv=%s 固化失败：%s" % (
                r["conversation_id"], str(e)[:120]), flush=True)
    return n_fixed
