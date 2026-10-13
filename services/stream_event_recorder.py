"""SSE 关键事件落库（批次二 #11）。

**为什么需要**：`stream.py` 里有 88 处 `yield {"type": ...}`，事件**只到前端即丢**
⇒ 排障时无法回放"当时到底发生了什么"。生产库实测**无任何编排事件表**
（`sqlite_master` 里只有 `flow_event_subscriptions` / `alert_events`，都不是编排事件）。

**为什么落在这层（路由）而不是 `execute_stream` 里**：
`execute_stream` 是生成器、事件在 88 处分散 yield，**没有单一收口**；
编排子任务走 `_evq` 队列但**只带子任务事件**，主路径的事件不经它。
路由层 `chat_stream` 的 `async for raw in _frames` 是**真正唯一的收口**，
且此处事件已归一为 `event: <type>\ndata: <json>\n\n`（`core/sse.to_sse_frame`），
解析格式稳定。**改一处 vs 改 88 处**，且不碰生成器语义。

**只落 stage / tool 两类（不做全量）**：
- `token`（逐字增量）与 `reasoning`（**CoT 增量，同样逐字**）是**高频流**，
  一次真实建模可达数百上千帧 ⇒ 全量落库会把表撑爆，且真正排障时没人会逐字读 CoT。
- `stage`（阶段推进 21 处）/ `tool`（工具调用 6 处）是**低频、高信息量**，
  恰好回答"这次编排走了哪些阶段、调了哪些工具、结果如何"。
- 其余类型（agent/done/error/subtask/...）由各自已有的表覆盖
  （`agent_tasks` / `tool_call_logs` / `messages`），**不重复落**。

**失败绝不阻断流**：`record()` 整体 try/except 吞异常（与本仓"观测旁路"原则一致），
落库失败只丢观测数据，不影响用户拿结果。
"""
import json
import re
import time

# 只落这两类（低频高信息量）；刻意不含 token/reasoning —— 见模块 docstring
PERSIST_TYPES = ("stage", "tool")

# 帧解析：event: <type>\ndata: <json>\n\n（core/sse.to_sse_frame 的输出格式）
_EV_RE = re.compile(r"^event:\s*([a-z_]+)\s*\ndata:\s*(.*)$", re.S)


def parse_frame(raw):
    """SSE 帧 → (type, payload dict)；不是可解析帧则返回 (None, None)。

    纯函数、零 IO —— 可被门禁直接断言，不必起服务。
    ⚠️ **不能因解析失败就丢事件**：前端仍会收到原帧（转发不受影响），
    这里返回 None 只表示"不落库"，不是"不转发"。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None, None
    # 只要第一帧（多帧场景下逐帧调用即可）
    head = raw.split("\n\n", 1)[0]
    m = _EV_RE.match(head.strip() + "\n")
    if not m:
        return None, None
    et, data = m.group(1), m.group(2).strip()
    try:
        payload = json.loads(data)
    except Exception:
        payload = {"_raw": data[:500]}
    if not isinstance(payload, dict):
        payload = {"_raw": data[:500]}
    return et, payload


def should_persist(etype, payload=None):
    """该事件是否值得落库（白名单 + 体积上限）。"""
    if etype not in PERSIST_TYPES:
        return False
    if payload is not None:
        try:
            # 单帧 data 超过 4KB 的不落（stage/tool 本不该这么大；超了说明是异常内容）
            if len(json.dumps(payload, ensure_ascii=False)) > 4096:
                return False
        except Exception:
            return False
    return True


def record(conn, conversation_id, raw, run_id=0, task_key="", max_per_run=None):
    """落一帧。返回是否真落库（False = 未落/落库失败）。

    `conn` 由调用方给（本仓观测层惯例：不自行开连接，避免与主流程抢 sqlite 写锁）。
    异常一律吞 —— 观测失败不得影响用户拿结果。
    """
    try:
        et, payload = parse_frame(raw)
        if not should_persist(et, payload):
            return False
        cur = conn.execute(
            "INSERT INTO orch_stream_events "
            "(conversation_id, run_id, task_key, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (int(conversation_id or 0), int(run_id or 0), str(task_key or "")[:64],
             et, json.dumps(payload, ensure_ascii=False)[:4000],
             time.strftime("%Y-%m-%d %H:%M:%S")))
        if max_per_run:
            # 单次编排事件数兜底（防异常循环把表撑爆）——超限就不再写
            n = conn.execute(
                "SELECT COUNT(*) FROM orch_stream_events WHERE conversation_id=? AND run_id=?",
                (int(conversation_id or 0), int(run_id or 0))).fetchone()[0]
            if n > max_per_run:
                conn.execute(
                    "DELETE FROM orch_stream_events WHERE conversation_id=? AND run_id=? "
                    "AND id NOT IN (SELECT id FROM orch_stream_events "
                    "WHERE conversation_id=? AND run_id=? ORDER BY id DESC LIMIT ?)",
                    (int(conversation_id or 0), int(run_id or 0),
                     int(conversation_id or 0), int(run_id or 0), int(max_per_run)))
        return cur.rowcount > 0
    except Exception:
        return False


def read_run_events(conn, conversation_id, run_id):
    """按 id 升序取某次编排落下的事件（排障回放用）。"""
    try:
        return conn.execute(
            "SELECT id, event_type, task_key, payload_json, created_at "
            "FROM orch_stream_events WHERE conversation_id=? AND run_id=? ORDER BY id",
            (int(conversation_id or 0), int(run_id or 0))).fetchall()
    except Exception:
        return []
