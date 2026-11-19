# -*- coding: utf-8 -*-
"""帧日志存储（方案B P0/P1）——**断线续传的单一真源**。

## 这个模块解决什么

现状：一轮生成与一条 HTTP 连接**强绑定**（`core/sse.py` 的"用户走了就停 LLM"）。
断线后用户能拿到的只有"已固化落库的正文"，而**固化是节流/结尾行为**，中间那段
真正在屏幕上滚过的内容没有任何服务端副本。于是：
  · 服务被强杀 → 用户看过的输出全部蒸发（真库 conv 999999207 事故）；
  · 刷新/换设备 → 只能看到落库那一刻的快照，看不到"当时滚到哪"；
  · 排障 → `orch_stream_events` 只存 stage/tool，token 流无法回溯。

本模块把**每一帧**（含 token / reasoning，合帧后）按 `(run_id, seq)` 落成有序日志，
使"重连后从游标接着显示"成为一次**纯读取**，不需要重跑生成、不产生第二次副作用。

## 三条不变量（改这个文件前先读）

1. **seq 由服务端单调分配、与帧写入同事务**（`append_frames`）。
   分开提交会在崩溃点留下"序号已推进但帧没落库"的洞 ⇒ 重连端以为拿全了，实际丢帧。
2. **seq 的语义是"客户端已渲染到第几帧"，不是"库里第几行"**。
   合帧（把连续 token 合成一帧）会让**库里的 seq 出现空洞** —— 这是**允许**的：
   空洞的内容已经被合并进后面那个更大的 seq 里，`seq > cursor` 仍能完整覆盖。
   所以 `feed()` 对**每一帧都返回它分配到的 seq**（供 SSE `id:` 行使用），
   即使该帧随后被合并进下一帧。否则客户端游标会落后于内容 ⇒ 重连时重复显示。
3. **失败绝不阻断主链路**。本模块任何异常都只把 tee 标成 dead 并返回 0，
   与 `stream_event_recorder` 的观测旁路原则一致（拿不到帧日志是遗憾，不是故障）。

## 与 `orch_stream_events` 的分工

| 表 | 目的 | 内容 | 保留 |
|---|---|---|---|
| `orch_stream_events` | 排障"发生了什么" | stage/tool（白名单，无 token） | 200/run，超限丢最旧 |
| `run_frames` | **重放**"生成到哪个字" | 全类型（token/reasoning 合帧） | 帧数上限，超限置 truncated |

刻意不合并：前者是"低频高信息密度的事件台账"，后者是"高频有序的字节流"，
合表会让 token 淹没事件、也会让重放受白名单限制。
"""
from __future__ import annotations

import json
import os
import time

TERMINAL_STATUS = ("done", "error", "cancelled")

# 可合帧的增量类型：payload 里 `delta` 是**纯追加**语义，合并后渲染等价。
# progress 刻意不合（它是离散提示，合并会把多条系统提示挤成一条）。
COALESCE_TYPES = ("token", "reasoning")

# 单帧合并上限（字符）。太小 ⇒ 帧数多、写库频繁；太大 ⇒ 重连时一次吐出几万字、
# 前端首屏卡顿。1200 是"约一屏"的量级。
COALESCE_MAX_CHARS = 1200


# ── 1. 运行会话（控制台） ──────────────────────────────────────────────

def new_run(conn, conversation_id, meta=None, owner_pid=None):
    """开一个 run 会话，返回 run_id（SQLite 自增主键，天然唯一）。

    ⚠️ **不复用 `TaskQueue.new_run_id()`**：那是 `MAX(agent_tasks.run_id)+1`，
    而直答路径根本不写 `agent_tasks` ⇒ 两个并发直答轮会拿到同一个号、撞本表主键
    ⇒ 后一轮静默无帧（症状是"重连看不到任何内容"，极难归因）。

    开新 run 前顺手做两件清理（都在同一连接、失败静默）：
      · **同会话的僵尸 run**：`status='running'` 但 `owner_pid` 不是本进程 ⇒ 那是
        上一个进程被杀留下的（帧日志还在、生成早没了）。不清理会让订阅端以为
        "还在跑"从而白等 `tail_max_s`，也让 UI 说"仍在运行"——与事实不符。
      · **超量历史**：只保留最近 `keep` 个（含帧），防表无界增长。
    """
    pid = int(owner_pid if owner_pid is not None else os.getpid())
    cid = int(conversation_id or 0)
    try:
        conn.execute(
            "UPDATE run_sessions SET status='cancelled', cancel_reason='stale_owner', "
            "ended_at=? WHERE conversation_id=? AND status='running' AND owner_pid<>?",
            (_now(), cid, pid))
    except Exception:
        pass
    cur = conn.execute(
        "INSERT INTO run_sessions (conversation_id, status, owner_pid, heartbeat_at, meta_json) "
        "VALUES (?, 'running', ?, ?, ?)",
        (cid, pid, _now(), _dumps(meta or {})))
    conn.commit()
    rid = int(cur.lastrowid or 0)
    try:
        prune_runs(conn, cid, 20)
    except Exception:
        pass
    return rid


def prune_runs(conn, conversation_id, keep=20):
    """只保留该会话最近 `keep` 个 run（含其帧）。`running` 的永不删。"""
    if int(keep or 0) <= 0:
        return 0
    try:
        rows = conn.execute(
            "SELECT run_id FROM run_sessions WHERE conversation_id=? AND status<>'running' "
            "ORDER BY run_id DESC LIMIT -1 OFFSET ?",
            (int(conversation_id or 0), int(keep))).fetchall()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return 0
        for rid in ids:
            conn.execute("DELETE FROM run_frames WHERE run_id=?", (rid,))
            conn.execute("DELETE FROM run_sessions WHERE run_id=?", (rid,))
        conn.commit()
        return len(ids)
    except Exception:
        return 0


def finish_run(conn, run_id, status, cancel_reason="", message_id=0, align_message=True):
    """收口一个 run：写终态 +（可选）把本轮落库消息与 run 对齐。

    `align_message=True` 时按 `message_id` 回填 `messages.run_id/last_seq` ——
    这样"落库消息 ↔ 帧日志"只需一处写，不必改 2~3 处消息插入路径
    （任何一处漏写都会造成"消息有、run 关联空"的半成品）。
    """
    st = str(status or "done")
    if st not in TERMINAL_STATUS:
        st = "done"
    try:
        row = conn.execute("SELECT last_seq, frame_count, truncated FROM run_sessions "
                           "WHERE run_id=?", (int(run_id),)).fetchone()
        last_seq = int((row["last_seq"] if row else 0) or 0)
        conn.execute(
            "UPDATE run_sessions SET status=?, cancel_reason=?, ended_at=?, message_id=?, "
            "heartbeat_at=? WHERE run_id=?",
            (st, str(cancel_reason or "")[:120], _now(), int(message_id or 0),
             _now(), int(run_id)))
        if align_message and int(message_id or 0) > 0:
            conn.execute(
                "UPDATE messages SET run_id=?, last_seq=? WHERE id=? AND COALESCE(run_id,0)=0",
                (int(run_id), last_seq, int(message_id)))
        conn.commit()
        return True
    except Exception:
        return False


def touch_run(conn, run_id):
    """刷新心跳（判活/孤儿回收用；节流由调用方决定）。"""
    try:
        conn.execute("UPDATE run_sessions SET heartbeat_at=? WHERE run_id=?",
                     (_now(), int(run_id)))
        conn.commit()
        return True
    except Exception:
        return False


def get_run(conn, run_id):
    """取 run 会话行（dict）；不存在返回 None。"""
    try:
        row = conn.execute("SELECT * FROM run_sessions WHERE run_id=?", (int(run_id),)).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        return None


def latest_run(conn, conversation_id):
    """该会话最近一次 run（/recovery 用来回答"刚才那轮去哪了"）。"""
    try:
        row = conn.execute(
            "SELECT * FROM run_sessions WHERE conversation_id=? ORDER BY run_id DESC LIMIT 1",
            (int(conversation_id or 0),)).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        return None


# ── 2. 帧写入 ──────────────────────────────────────────────────────────

def append_frames(conn, run_id, rows):
    """批量写帧：`rows = [(seq, event_type, payload_json), ...]`。

    **同事务**推进 `last_seq` / `frame_count`（不变量 1）。重复 `(run_id, seq)`
    用 `INSERT OR REPLACE` 覆盖 —— 同一 seq 只会由同一个 tee 写一次，
    真出现重复说明进程内有两个 tee 争同一 run，覆盖比抛错更能保住已产出的内容
    （异常会导致整批丢失，而覆盖只影响一帧）。
    """
    if not rows:
        return 0
    try:
        rid = int(run_id)
        conn.executemany(
            "INSERT OR REPLACE INTO run_frames (run_id, seq, event_type, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            [(rid, int(s), str(t), p, _now()) for (s, t, p) in rows])
        _seqs = [int(s) for (s, _t, _p) in rows]
        _max = max(_seqs)
        conn.execute(
            "UPDATE run_sessions SET last_seq=MAX(COALESCE(last_seq,0), ?), "
            "frame_count=COALESCE(frame_count,0)+? WHERE run_id=?",
            (_max, len(rows), rid))
        conn.commit()
        return len(rows)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def mark_truncated(conn, run_id):
    try:
        conn.execute("UPDATE run_sessions SET truncated=1 WHERE run_id=?", (int(run_id),))
        conn.commit()
        return True
    except Exception:
        return False


def read_frames(conn, run_id, after_seq=0, limit=500):
    """按 seq 升序取 `seq > after_seq` 的帧（重放唯一读取路径）。"""
    try:
        return conn.execute(
            "SELECT seq, event_type, payload, created_at FROM run_frames "
            "WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (int(run_id), int(after_seq or 0), int(limit or 500))).fetchall()
    except Exception:
        return []


# ── 3. 游标解析与帧序列化（纯函数，可离线断言） ─────────────────────────

def parse_last_event_id(value):
    """`"123-45"` → 45；`"45"` → 45；非法 → 0。

    同时接受 SSE 原生的 `{run_id}-{seq}` 与裸 seq：前者是浏览器 `EventSource`
    自动回带的格式，后者便于手工/脚本重放。
    """
    s = str(value or "").strip()
    if not s:
        return 0
    if "-" in s:
        s = s.rsplit("-", 1)[-1].strip()
    try:
        return max(0, int(s))
    except Exception:
        return 0


def format_event_id(run_id, seq):
    return "%d-%d" % (int(run_id or 0), int(seq or 0))


def frame_sse(run_id, seq, event_type, payload_json):
    """库里一帧 → 可直接写给客户端的 SSE 文本（带标准 `id:` 行）。

    `id:` 是 SSE 原生字段；本仓前端用 `fetch+getReader` 而不是 `EventSource`，
    故前端读不到它 —— 但它对任何标准客户端（含 curl / 浏览器调试）都是可用的游标，
    且是"重放接口按 Last-Event-ID 续传"的协议依据。
    """
    try:
        data = payload_json if isinstance(payload_json, str) else _dumps(payload_json)
    except Exception:
        data = "{}"
    return "id: %s\nevent: %s\ndata: %s\n\n" % (
        format_event_id(run_id, seq), event_type or "message", data)


def prepare_frame_replay(last_acked_seq, status, max_seq, tail=True):
    """**纯函数**：由（游标 / run 状态 / 已落库最大 seq）算出回放决策。

    对标编排侧的 `prepare_resume`：决策与执行分离，故可用纯数据把四种状态
    （running / done / error / cancelled）全覆盖，不必起服务、不碰 DB。

    返回：
      mode            replay_terminal | replay_then_tail | tail_only | nothing
      replay          是否需要回放已有帧
      tail            回放后是否继续等待新帧
      resume_from_seq 回放起点（= 客户端游标，**不含**）
      cursor_ahead    客户端游标超过服务端最大 seq（换了 run / 本地脏游标）
      terminal        run 是否已终态
    """
    cur = max(0, int(last_acked_seq or 0))
    mx = max(0, int(max_seq or 0))
    st = str(status or "running")
    terminal = st in TERMINAL_STATUS
    ahead = cur > mx
    if ahead:
        # 游标超前（典型：前端拿着上一轮的 run 号来续传，或本地缓存脏了）。
        # **不能顺水推舟继续 tail** —— 那会让客户端永久错过 1..mx 的内容。
        # 正确处置是"从头回放"（cur 归零），并把事实告诉调用方。
        _mode = ("replay_terminal" if terminal
                 else ("replay_then_tail" if tail else "replay_only"))
        return {"mode": _mode,
                "replay": mx > 0, "tail": (not terminal) and bool(tail),
                "resume_from_seq": 0, "cursor_ahead": True, "terminal": terminal,
                "note": "客户端游标超过服务端最大帧序，已从头回放"}
    need_replay = mx > cur
    if terminal:
        mode = "replay_terminal" if need_replay else "nothing"
        return {"mode": mode, "replay": need_replay, "tail": False,
                "resume_from_seq": cur, "cursor_ahead": False, "terminal": True,
                "note": "任务已结束，仅回放 %d 帧" % max(0, mx - cur) if need_replay
                        else "任务已结束，且客户端已持有全部帧"}
    if need_replay:
        return {"mode": "replay_then_tail" if tail else "replay_only", "replay": True,
                "tail": bool(tail), "resume_from_seq": cur, "cursor_ahead": False,
                "terminal": False,
                "note": "回放 %d 帧%s" % (max(0, mx - cur), "后继续等待新帧" if tail else "")}
    return {"mode": "tail_only" if tail else "nothing", "replay": False,
            "tail": bool(tail), "resume_from_seq": cur, "cursor_ahead": False,
            "terminal": False,
            "note": "无待回放帧" + ("，等待新帧" if tail else "")}


def reconstruct_from_frames(frames):
    """**纯函数**：把帧序列还原成"用户看到的样子"（正文 + 执行过程）。

    这是"重放"和"落库正文"之间的对照器：正常收口时两者应当高度一致
    （差异只能来自节流固化的截断），不一致就说明帧日志漏帧 —— 门禁据此断言。

    `frames` 元素需有 `seq/event_type/payload`（payload 为 JSON 字符串或 dict）。
    """
    content = []
    exec_ = {"agent": "", "subtasks": [], "tools": [], "reasoning": [], "attempts": []}
    cap_reasoning = 8000
    last_seq = 0
    for f in frames or []:
        try:
            et = (f["event_type"] if not isinstance(f, dict) else f.get("event_type")) or ""
            pj = (f["payload"] if not isinstance(f, dict) else f.get("payload"))
            seq = int((f["seq"] if not isinstance(f, dict) else f.get("seq")) or 0)
            p = pj if isinstance(pj, dict) else json.loads(pj or "{}")
        except Exception:
            continue
        last_seq = max(last_seq, seq)
        if not isinstance(p, dict):
            continue
        if et == "token":
            d = p.get("delta") or ""
            if d:
                content.append(d)
        elif et == "reasoning":
            if p.get("kind") != "progress":
                cur_len = sum(len(x) for x in exec_["reasoning"])
                if cur_len < cap_reasoning:
                    exec_["reasoning"].append(p.get("delta") or "")
        elif et == "attempt":
            ai = p.get("index")
            if ai is not None and not any(x.get("index") == ai for x in exec_["attempts"]):
                exec_["attempts"].append({"index": ai, "kind": p.get("kind"),
                                          "chars": p.get("chars", 0)})
        elif et == "agent":
            if p.get("status") == "done":
                nm = p.get("display_name") or p.get("name") or ""
                if nm:
                    exec_["agent"] = str(nm)
        elif et == "subtask":
            k = str(p.get("key") or "")
            if not k:
                continue
            item = next((x for x in exec_["subtasks"] if x.get("key") == k), None)
            if item is None:
                item = {"key": k}
                exec_["subtasks"].append(item)
            for sk in ("title", "status", "agent"):
                if p.get(sk):
                    item[sk] = p.get(sk)
            if p.get("latency_ms"):
                item["latency_ms"] = p.get("latency_ms")
        elif et == "tool":
            cid = str(p.get("call_id") or p.get("name") or "")
            if not cid:
                continue
            t = next((x for x in exec_["tools"] if x.get("_cid") == cid), None)
            if t is None:
                t = {"_cid": cid, "name": p.get("name") or ""}
                exec_["tools"].append(t)
                if len(exec_["tools"]) > 60:
                    exec_["tools"].pop(1)
            if p.get("status") == "done":
                t["ok"] = p.get("ok")
                t["error"] = str(p.get("error") or "")[:200]
                t["arguments"] = p.get("arguments") or t.get("arguments") or {}
                t["result"] = str(p.get("result") or "")[:2000]
                if p.get("latency_ms"):
                    t["elapsed_ms"] = p.get("latency_ms")
            elif p.get("arguments"):
                t["arguments"] = p.get("arguments")
    return {"content": "".join(content), "exec": exec_, "last_seq": last_seq}


# ── 4. FrameTee：把"逐帧转发"变成"逐帧落库" ────────────────────────────

class FrameTee:
    """边转发边记帧：分配 seq / 合帧 / 批量落库 / 超限截断。

    用法（在唯一收口 `async for raw in _frames` 里）::

        tee = FrameTee(run_id, conv_id)
        seq = tee.feed(raw)          # 返回该帧分配到的 seq（0 = 不落/已停）
        if seq: yield "id: %d-%d\\n" % (run_id, seq) + raw
        ...
        tee.close(status="done")

    设计要点：
    · **对每一帧都返回 seq**（含被合帧的 token）—— 客户端游标必须跟上内容，
      否则重连会重复显示（见模块 docstring 不变量 2）。
    · `close()` 幂等；`_dead` 之后 `feed()` 恒返回 0（观测失败即静默）。
    """

    def __init__(self, run_id, conversation_id=0, conn_factory=None, *,
                 enabled=True, max_frames=2000, flush_frames=40,
                 flush_interval_s=0.4, coalesce=True, clock=None):
        self.run_id = int(run_id or 0)
        self.conversation_id = int(conversation_id or 0)
        self.enabled = bool(enabled) and self.run_id > 0
        self.max_frames = max(1, int(max_frames or 2000))
        self.flush_frames = max(1, int(flush_frames or 40))
        self.flush_interval_s = max(0.0, float(flush_interval_s or 0.0))
        self.coalesce = bool(coalesce)
        self._clock = clock or time.monotonic
        self._conn_factory = conn_factory
        self._seq = 0
        self._buf = []            # [(seq, etype, payload_json)]
        self._pending = None      # {"seq":int,"etype":str,"payload":dict,"chars":int}
        self._stored = 0
        self._last_flush = self._clock()
        self._truncated = False
        self._dead = not self.enabled
        self.dropped = 0          # 因上限/异常而未落库的帧数（供收口留痕）

    # -- 对外只读属性 --
    @property
    def last_seq(self):
        return self._seq

    @property
    def truncated(self):
        return self._truncated

    @property
    def alive(self):
        return not self._dead

    # -- 内部 --
    def _next_seq(self):
        self._seq += 1
        return self._seq

    def _close_pending(self):
        p = self._pending
        self._pending = None
        if not p:
            return
        try:
            pj = _dumps(p["payload"])
        except Exception:
            return
        self._buf.append((p["seq"], p["etype"], pj))

    def _flush(self):
        self._close_pending()
        if not self._buf:
            self._last_flush = self._clock()
            return 0
        rows, self._buf = self._buf, []
        self._last_flush = self._clock()
        if self._conn_factory is None:
            self._stored += len(rows)
            return len(rows)
        try:
            with self._conn_factory() as _c:
                n = append_frames(_c, self.run_id, rows)
        except Exception:
            n = 0
        if n:
            self._stored += n
        return n

    def _cap_hit(self):
        return (self._stored + len(self._buf) + (1 if self._pending else 0)) >= self.max_frames

    # -- 主入口 --
    def feed(self, raw):
        """记一帧，返回它分配到的 seq（0 = 本帧不参与重放）。异常一律静默。"""
        if self._dead:
            return 0
        try:
            from services.stream_event_recorder import parse_frame
            et, payload = parse_frame(raw)
            if not et or not isinstance(payload, dict):
                return 0        # 注释帧 / 心跳 / 不可解析帧：无可重放内容
            if self._truncated or self._cap_hit():
                self._truncated = True
                self.dropped += 1
                self._flush()
                if self._conn_factory is not None:
                    try:
                        with self._conn_factory() as _c:
                            mark_truncated(_c, self.run_id)
                    except Exception:
                        pass
                return 0

            if self.coalesce and et in COALESCE_TYPES:
                d = payload.get("delta") or ""
                k = str(payload.get("key") or payload.get("round") or "")
                p = self._pending
                if p and p["etype"] == et and p["key"] == k and p["chars"] < COALESCE_MAX_CHARS:
                    p["payload"]["delta"] = (p["payload"].get("delta") or "") + d
                    p["chars"] += len(d)
                    p["seq"] = self._next_seq()      # ★ 游标跟到最新（不变量 2）
                    self._maybe_flush()
                    return p["seq"]
                self._close_pending()
                seq = self._next_seq()
                self._pending = {"seq": seq, "etype": et, "key": k,
                                 "payload": dict(payload), "chars": len(d)}
                self._maybe_flush()
                return seq

            if et in COALESCE_TYPES:
                # 不合帧但仍是增量类型：按普通帧处理（确保游标与内容一一对应）
                self._close_pending()
                seq = self._next_seq()
                self._buf.append((seq, et, _dumps(payload)))
                self._maybe_flush()
                return seq

            self._close_pending()
            seq = self._next_seq()
            self._buf.append((seq, et, _dumps(payload)))
            self._maybe_flush()
            return seq
        except Exception:
            # 观测失败不得影响用户拿结果；同时也别让它每帧都抛一次
            self._dead = True
            return 0

    def _maybe_flush(self):
        if self._pending and self._pending["chars"] >= COALESCE_MAX_CHARS:
            self._close_pending()
        if len(self._buf) >= self.flush_frames:
            self._flush()
            return
        if self.flush_interval_s and (self._clock() - self._last_flush) >= self.flush_interval_s:
            self._flush()

    def flush(self):
        """强制落盘（终态帧到来 / 收口时调，保证"最后一段 token"不留在内存）。"""
        if self._dead:
            return 0
        try:
            return self._flush()
        except Exception:
            return 0

    def close(self):
        """收口：把残留（含未闭合的 token 合帧）落盘。幂等。"""
        if self._dead:
            return 0
        try:
            n = self._flush()
        except Exception:
            n = 0
        self._dead = True
        return n


# ── 5. 小工具 ─────────────────────────────────────────────────────────

def _now():
    # 与库内其它表一致：`CURRENT_TIMESTAMP` 是 UTC，这里显式用 UTC，避免同表两种时区
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)
