"""对话域 Service：跨表编排上提（P1-1 层抽象）。

上提点：会话内运行工作流（conversation_flow_run）—— 跨 ConversationRepo + StudioRepo
+ FlowExecutor + 消息落库 + 审计，属典型跨表编排；薄 CRUD（建/删/改名）留在 router。
"""
import json
import time

from fastapi.responses import JSONResponse

from .base import BaseService


class ConversationService(BaseService):
    """会话编排：流程运行闭环（HIL 确认后触发）。"""

    def run_flow_in_conversation(self, conv_id: int, fid: int, payload_text: str,
                                 user=None, conn=None) -> dict:
        """会话内运行工作流：执行 FlowExecutor → 结果作为 assistant 消息落库（msg_type='flow_result'）。

        与原 routers/conversations.py:conversation_flow_run 逻辑等价（P1-1 原样上提）。
        返回 dict 响应；错误以 {"error": msg} 形式返回（router 层转 JSONResponse）。
        """
        from repositories.conversation_repo import ConversationRepo
        from repositories.studio_repo import StudioRepo

        c = conn or self.conn
        repo = self.repo(ConversationRepo)
        conv = repo.get_conversation(conv_id)
        if not conv:
            return {"error": "会话不存在"}
        flow = self.repo(StudioRepo).get_agent_flow(fid)
        if not flow:
            return {"error": "流程不存在"}
        nodes = json.loads(flow["nodes"] or "[]")
        edges = json.loads(flow["edges"] or "[]")
        if not nodes:
            return {"error": "流程为空，无可执行节点"}
        # 执行 DAG（复用工坊 FlowExecutor，结果结构化）
        from workflows import FlowExecutor
        t0 = time.time()
        payload = {"text": (payload_text or conv.get("title") or "")[:2000]}
        result = FlowExecutor(c).run(nodes, edges, payload, c,
                                     persist=True, flow_id=fid, flow_name=flow["name"])
        latency = int((time.time() - t0) * 1000)
        # 组装可读摘要：按拓扑序拼接各节点结果
        order = result.get("order") or []
        results = result.get("results") or {}
        steps = []
        for nid in order:
            r = results.get(nid) or {}
            label = next((n.get("label") or n.get("id") for n in nodes if n.get("id") == nid), nid)
            status = r.get("status", "?")
            content = (r.get("result") or r.get("content") or "")[:400]
            steps.append({"node": label, "status": status, "result": content})
        status = result.get("status", "completed")
        summary = f"流程「{flow['name']}」执行{'成功' if status == 'completed' else '（部分完成）'}：{len(order)} 个节点，"
        summary += f"{sum(1 for s in steps if s['status'] == 'done')} 成功，耗时 {latency}ms"
        # 落库 assistant 消息（msg_type='flow_result'，前端特殊渲染）
        cur = c.execute(
            "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?,?,?,?,?)",
            (conv_id, "assistant", summary, "flow_result",
             json.dumps({"flow_id": fid, "flow_name": flow["name"], "status": status,
                         "steps": steps, "latency_ms": latency, "run_id": result.get("run_id")},
                        ensure_ascii=False)))
        msg_id = cur.lastrowid
        c.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
        c.commit()
        from core.audit import audit
        audit("王工", "flow_run_chat",
              f"会话#{conv_id} 运行流程#{fid}: {flow['name']} → {status}", conn=c)
        return {"ok": True, "message_id": msg_id, "summary": summary, "status": status,
                "steps": steps, "latency_ms": latency, "run_id": result.get("run_id"),
                "flow_name": flow["name"]}
