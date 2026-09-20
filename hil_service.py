"""M5 HIL 人机协作强制：L2 写操作确认队列（未确认不生效）。

背景：HIL 分级此前仅写入 prompt 文本，靠 LLM 自觉；本模块在执行路径强制——
- is_write_tool：识别写类工具（entity_create 等），非写工具不受影响。
- queue_confirmation：L2 上下文发起写操作 → 生成待确认记录（payload 完整参数快照）。
- approve/reject：人工决策（approve 后调用方按 payload 真正执行；reject 丢弃）。
- pending：供前端确认队列面板轮询。

与 agent_tasks 解耦：hil_confirmations 独立表，支持 conversation / flow 两种场景。
"""
import json
from datetime import datetime

# 写类工具白名单（其余默认只读）。新增写工具在此登记。
WRITE_TOOLS = {"entity_create", "relation_create", "entity_update", "entity_delete", "relation_delete"}
# 幂等 ID 命名空间（防生成重复确认）
_PREVIEW_LEN = 400


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
        """
        try:
            if not conn:
                return None
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
