"""M5 HIL 人机协作强制：L2 写操作确认队列（未确认不生效）。

背景：HIL 分级此前仅写入 prompt 文本，靠 LLM 自觉；本模块在执行路径强制——
- is_write_tool：识别写类工具（entity_create 等），非写工具不受影响。
- queue_confirmation：L2 上下文发起写操作 → 生成待确认记录（payload 完整参数快照）。
- approve/reject：人工决策（approve 后调用方按 payload 真正执行；reject 丢弃）。
- pending：供前端确认队列面板轮询。

与 agent_tasks 解耦：hil_confirmations 独立表，支持 conversation / flow 两种场景。

★ 2026-10-13（文章 §06 幂等）：本模块此前只有一句悬空注释
  「# 幂等 ID 命名空间（防生成重复确认）」—— **它描述的常量并不存在**，
  `queue_confirmation` 是**无条件 INSERT**，同一请求被重复提交会产生多条待确认记录。
  真库实证（2026-10-13 只读）：`hil_confirmations` 6 行里 **id 13/14 是完全重复的一对**
  （agent_id / action / payload 全同）⇒ 已经在产生重复确认。
  现改为按**幂等键**去重，键的构造见 `idempotency_key()`。
"""
import hashlib
import json
from datetime import datetime

# 写类工具白名单（其余默认只读）。新增写工具在此登记。
WRITE_TOOLS = {"entity_create", "relation_create", "entity_update", "entity_delete", "relation_delete"}

#: ★ 幂等键的**命名空间前缀**（原为悬空注释，2026-10-13 落实为真实常量）。
#: 前缀的作用：让不同来源的确认单不撞键 —— 同一 action+payload 在
#: 「编排子任务暂存」与「L2 强制拦截」两条路径下是不同的业务意图，不该互相当成重复。
IDEM_NS_CONFIRM = "hilq"

#: 幂等窗口（小时）：只在这个窗口内去重。
#: ⚠️ **不设永久唯一约束**的理由：确认单是"待办"，人工可能隔很久才处理；
#:   永久去重会让"用户昨天拒绝、今天想重新提交同一操作"被静默吞掉（返回那条已拒绝的旧记录）——
#:   那比产生重复确认更糟（用户永远做不成事，且看不出原因）。
#:   窗口内的重复才是"同一次操作的重复提交"，这是真正要挡的那类。
IDEM_WINDOW_HOURS = 24

_PREVIEW_LEN = 400


def idempotency_key(agent_id: str, action: str, payload: dict,
                    run_id: int = 0, task_key: str = "") -> str:
    """★ 构造写操作的幂等键（文章 §06）。

    键的构成（每段都对应"两次提交是否应当视为同一次操作"的判断）：
      · `agent` —— 谁发起的。不同 Agent 对同一工具的调用是**不同意图**（多 Agent 协作场景常见）。
      · `action` —— 调哪个工具。
      · `payload` —— 参数**规范化后**的哈希。规范化 = `sort_keys=True` + 紧凑分隔符，
        否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会算出两个键，而它们显然是同一次操作。
      · `run` + `task` —— **刻意纳入**：编排子任务暂存（`store_write_request` 路径）里
        run/task 是溯源必需，去重窗口内的同一 run 同一 task 同一 payload 视为重复；
        而跨 run 的同参调用（两个批次都试图建同一个实体）**不该去重** ——
        那可能确实是两次独立决策，拦掉会让第二批批次"看起来成功但什么都没做"。
    """
    try:
        _p = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"))
    except Exception:
        _p = str(payload or "")
    _raw = "|".join([IDEM_NS_CONFIRM, str(agent_id or ""), str(action or ""),
                     _p, str(int(run_id or 0)), str(task_key or "")])
    return hashlib.sha1(_raw.encode("utf-8")).hexdigest()[:24]


def _json_canonical(payload: dict) -> str:
    """payload → 规范化 JSON 字符串（用于**SQL 层**比对，见 queue_confirmation）。

    ⚠️ 必须与 `idempotency_key` 用**同一套规范化规则**，否则两处口径漂移。
    """
    try:
        return json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    except Exception:
        return str(payload or "")


class HILService:
    """L2 写操作强制确认队列。"""

    @staticmethod
    def is_write_tool(tool_name: str) -> bool:
        return (tool_name or "").strip() in WRITE_TOOLS

    @staticmethod
    def queue_confirmation(conn, agent_id: str, action: str, payload: dict,
                           preview: str = "", conversation_id: int = 0,
                           run_id: int = 0, task_key: str = "") -> dict | None:
        """L2 写操作拦截：写入待确认队列。返回确认记录（含 id）。

        task_key：所属子任务键（默认 ''，既有调用方零改动）；
        供编排子任务写操作暂存批量挂确认队列时溯源（subtask_artifacts 绑定）。

        ★ 2026-10-13（§06 幂等）：**同一次操作被重复提交时返回已有记录，不新建**。
        判据 = `idem_key`（见 `idempotency_key`）+ 窗口（`IDEM_WINDOW_HOURS`）+ 状态 pending。
        ⚠️ 只对 **pending** 去重：已 approved/rejected 的旧记录不再挡新提交
        （否则"上次被拒、这次想重来"会被误判成重复而静默丢弃 —— 用户永远做不成事且看不出原因）。
        ⚠️ 返回的 dict 带 `idempotent_hit` 标记（True=命中已有，False=本次新建），
        供调用方区分；⚠️ 不改变既有返回结构的其他键。
        ⚠️ `idem_key` 列缺失（旧库）时**自动降级为不启用幂等**，行为与改动前一致 ——
        不能因为一个新列没补上就让整个写确认链路挂掉。
        """
        _idem = ""
        try:
            _idem = idempotency_key(agent_id, action, payload, run_id, task_key)
            try:
                conn.execute("SELECT idem_key FROM hil_confirmations LIMIT 1").fetchone()
            except Exception:
                _idem = ""      # 旧库无该列 → 本次不启用幂等
            if _idem:
                try:
                    dup = conn.execute(
                        "SELECT * FROM hil_confirmations WHERE idem_key=? AND status='pending' "
                        "AND created_at >= datetime('now', ?) LIMIT 1",
                        (_idem, "-%d hours" % IDEM_WINDOW_HOURS)).fetchone()
                except Exception:
                    dup = None
                if dup:
                    d = dict(dup)
                    try:
                        d["payload"] = json.loads(d.get("payload") or "{}")
                    except Exception:
                        pass
                    d["idempotent_hit"] = True
                    return d
        except Exception:
            _idem = ""
        try:
            cur = conn.execute(
                "INSERT INTO hil_confirmations (agent_id, conversation_id, run_id, action, payload, preview, status, task_key, idem_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (agent_id or "", conversation_id or 0, run_id or 0, action,
                 json.dumps(payload or {}, ensure_ascii=False),
                 (preview or "")[:_PREVIEW_LEN], "pending", task_key or "", _idem))
            conn.commit()
            out = HILService.get(conn, cur.lastrowid)
            if isinstance(out, dict):
                out["idempotent_hit"] = False
            return out
        except Exception:
            # 极老库无 idem_key 列 ⇒ 退回不写幂等键的插入（**不丢确认单**）
            try:
                cur = conn.execute(
                    "INSERT INTO hil_confirmations (agent_id, conversation_id, run_id, action, payload, preview, status, task_key) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (agent_id or "", conversation_id or 0, run_id or 0, action,
                     json.dumps(payload or {}, ensure_ascii=False),
                     (preview or "")[:_PREVIEW_LEN], "pending", task_key or ""))
                conn.commit()
                return HILService.get(conn, cur.lastrowid)
            except Exception:
                return None

    @staticmethod
    def get(conn, conf_id: int) -> dict | None:
        try:
            row = conn.execute("SELECT * FROM hil_confirmations WHERE id=?", (conf_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["payload"] = json.loads(d.get("payload") or "{}")
            return d
        except Exception:
            return None

    @staticmethod
    def pending(conn, limit: int = 50) -> list:
        try:
            rows = conn.execute(
                "SELECT * FROM hil_confirmations WHERE status='pending' ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.get("payload") or "{}")
                out.append(d)
            return out
        except Exception:
            return []

    @staticmethod
    def decide(conn, conf_id: int, approve: bool, decided_by: str = "") -> dict:
        """人工决策。approve=True → 返回 {'status':'approved','payload':{...}}，调用方执行后才算生效；
        approve=False → 丢弃，返回 {'status':'rejected'}。"""
        row = HILService.get(conn, conf_id)
        if not row:
            return {"status": "not_found", "payload": {}}
        if row["status"] != "pending":
            return {"status": row["status"], "payload": row["payload"]}
        new_status = "approved" if approve else "rejected"
        conn.execute(
            "UPDATE hil_confirmations SET status=?, decided_by=?, decided_at=? WHERE id=?",
            (new_status, decided_by or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), conf_id))
        conn.commit()
        return {"status": new_status, "payload": row["payload"]}
