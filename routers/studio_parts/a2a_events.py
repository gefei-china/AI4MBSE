# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：A2A 协议与事件订阅。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


@router.get("/api/a2a/agent-card")
def a2a_agent_card(conn=Depends(db_session)):
    """A2A 身份卡（Agent Card）：对外暴露本平台身份与能力，供外部 Agent 发现互通。

    字段对齐 A2A agent card 规范：name/description/url/skills（能力清单）。
    """
    skills = StudioRepo(conn).rows("SELECT name, description FROM skills WHERE status='published' LIMIT 20")
    return {
        "name": "智源 MBSE 工作流平台",
        "description": "网络总体 MBSE 设计 Agent 工作流平台——支持流程编排、多 Agent 协作、A2A 标准化消息互通",
        "url": "/api/a2a/agent-card",
        "version": "0.1",
        "capabilities": ["flow_orchestration", "multi_agent", "pubsub", "a2a_messages", "event_callback"],
        "skills": [{"name": s["name"], "description": s.get("description") or ""} for s in skills],
        "a2a_endpoints": ["POST /api/a2a/messages"],
    }


@router.post("/api/a2a/messages")
def a2a_inbound(body: A2AIn, conn=Depends(db_session), user=Depends(current_user)):
    """A2A 入站接口：接收外部 Agent 标准化消息 → 写入共享消息池（agent_messages）。

    下游 pubsub 订阅节点可按 topic 认领消费，实现跨系统 Agent 互通。
    返回 {accepted, message_id, seq}（A2A message 约定）。
    """
    # 解析 topic / content：标准 A2A message 结构优先，退化到直接字段
    topic = body.topic or "external"
    sender_name, sender_type, sender_role = "external", "external", "agent"
    content = body.content
    if body.message:
        m = body.message
        sender = body.sender or {}
        sender_name = str(sender.get("name") or "external")
        sender_type = str(sender.get("type") or "external")
        sender_role = str(sender.get("role") or "agent")
        contents = m.get("contents") or []
        ctx = m.get("context") or {}
        if ctx.get("topic"):
            topic = str(ctx["topic"])
        if isinstance(contents, list) and contents:
            first = contents[0]
            content = first.get("text") if isinstance(first, dict) else first
    if content is None:
        content = "（无内容）"
    if isinstance(content, (dict, list)):
        content_json = json.dumps(content, ensure_ascii=False)
    else:
        content_json = json.dumps({"text": str(content)}, ensure_ascii=False)
    run_id = int(body.run_id or 0)
    seq = 0
    try:
        row = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM agent_messages WHERE run_id=?", (run_id,)).fetchone()
        seq = int(row["n"])
        conn.execute(
            "INSERT INTO agent_messages (run_id, seq, topic, content_json, publisher_node, claimed, "
            "sender_name, sender_type, sender_role) VALUES (?,?,?,?,?,0,?,?,?)",
            (run_id, seq, topic, content_json, "a2a-inbound", sender_name, sender_type, sender_role))
        conn.commit()
    except Exception as e:
        return JSONResponse({"error": f"写入消息池失败: {str(e)[:120]}"}, 500)
    audit(audit_user(user), "a2a_inbound", f"A2A 入站消息: topic={topic} sender={sender_name}", conn=conn)
    return {"accepted": True, "protocol": "a2a", "version": "0.1",
            "message_id": f"a2a-{uuid.uuid4().hex[:12]}", "seq": seq, "run_id": run_id, "topic": topic}


@router.get("/api/studio/event-subscriptions")
def list_event_subscriptions(run_id: int | None = None, conn=Depends(db_session)):
    """事件订阅列表（可按 run_id 过滤；run_id=0 返回全局订阅）。"""
    repo = StudioRepo(conn)
    if run_id is not None:
        return repo.rows("SELECT * FROM flow_event_subscriptions WHERE run_id=? OR run_id=0 ORDER BY id",
                         (int(run_id),))
    return repo.rows("SELECT * FROM flow_event_subscriptions ORDER BY id")


@router.post("/api/studio/event-subscriptions")
def create_event_subscription(body: EventSubIn, conn=Depends(db_session), user=Depends(current_user)):
    """注册事件订阅：执行器在匹配事件（节点完成/出错/运行完成）时向 webhook_url POST 标准化 A2A 事件。"""
    if body.event_type not in ("node_done", "node_error", "run_completed"):
        return JSONResponse({"error": "event_type 必须是 node_done|node_error|run_completed"}, 400)
    if not body.webhook_url.strip():
        return JSONResponse({"error": "webhook_url 必填"}, 400)
    conn.execute(
        "INSERT INTO flow_event_subscriptions (run_id, node_id, event_type, webhook_url, secret) "
        "VALUES (?,?,?,?,?)",
        (int(body.run_id or 0), body.node_id or "", body.event_type, body.webhook_url.strip(),
         body.secret or ""))
    conn.commit()
    sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    audit(audit_user(user), "event_sub_create", f"注册事件订阅#{sid}: {body.event_type} → {body.webhook_url[:60]}", conn=conn)
    return {"ok": True, "id": sid}


@router.delete("/api/studio/event-subscriptions/{sid}")
def delete_event_subscription(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    conn.execute("DELETE FROM flow_event_subscriptions WHERE id=?", (sid,))
    conn.commit()
    audit(audit_user(user), "event_sub_delete", f"删除事件订阅#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/event-subscriptions/{sid}/toggle")
def toggle_event_subscription(sid: int, conn=Depends(db_session)):
    """暂停/激活订阅（active ↔ paused）。"""
    row = conn.execute("SELECT status FROM flow_event_subscriptions WHERE id=?", (sid,)).fetchone()
    if not row:
        return JSONResponse({"error": "订阅不存在"}, 404)
    new = "paused" if row["status"] == "active" else "active"
    conn.execute("UPDATE flow_event_subscriptions SET status=? WHERE id=?", (new, sid))
    conn.commit()
    return {"ok": True, "id": sid, "status": new}
