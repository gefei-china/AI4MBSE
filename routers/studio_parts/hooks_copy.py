# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：工具钩子与制品复制。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/tool-hooks")
def list_tool_hooks(conn=Depends(db_session)):
    return StudioRepo(conn).rows("SELECT * FROM tool_hooks ORDER BY id")


@router.post("/api/studio/tool-hooks")
def create_tool_hook(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    err = _hook_err(body)
    if err:
        return JSONResponse({"error": err}, 400)
    name = body["name"].strip()[:60]
    tool_pattern = body["tool_pattern"].strip()[:60]
    cur = conn.execute(
        "INSERT INTO tool_hooks (name, description, tool_pattern, event, action, condition_args, message, enabled) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, str(body.get("description") or "")[:200], tool_pattern,
         body.get("event") or "pre_tool_use", body.get("action") or "block",
         str(body.get("condition_args") or ""), str(body.get("message") or "")[:200],
         1 if (body or {}).get("enabled", 1) else 0))
    conn.commit()
    audit(audit_user(user), "tool_hook_create", f"新增工具钩子: {name} → {tool_pattern}", conn=conn)
    return {"ok": True, "id": cur.lastrowid}


@router.put("/api/studio/tool-hooks/{hid}")
def update_tool_hook(hid: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    row = conn.execute("SELECT * FROM tool_hooks WHERE id=?", (hid,)).fetchone()
    if not row:
        return JSONResponse({"error": "钩子不存在"}, 404)
    err = _hook_err(body)
    if err:
        return JSONResponse({"error": err}, 400)
    conn.execute(
        "UPDATE tool_hooks SET name=?, description=?, tool_pattern=?, action=?, condition_args=?, message=?, "
        "enabled=? WHERE id=?",
        (body["name"].strip()[:60], str(body.get("description") or "")[:200],
         body["tool_pattern"].strip()[:60], body.get("action") or "block",
         str(body.get("condition_args") or ""), str(body.get("message") or "")[:200],
         1 if (body or {}).get("enabled", 1) else 0, hid))
    conn.commit()
    audit(audit_user(user), "tool_hook_update", f"更新工具钩子#{hid}: {body['name'][:30]}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/tool-hooks/{hid}/enable")
def enable_tool_hook(hid: int, conn=Depends(db_session), user=Depends(current_user)):
    cur = conn.execute("UPDATE tool_hooks SET enabled=1 WHERE id=?", (hid,))
    if cur.rowcount == 0:
        return JSONResponse({"error": "钩子不存在"}, 404)
    conn.commit()
    audit(audit_user(user), "tool_hook_enable", f"启用工具钩子#{hid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/tool-hooks/{hid}/disable")
def disable_tool_hook(hid: int, conn=Depends(db_session), user=Depends(current_user)):
    cur = conn.execute("UPDATE tool_hooks SET enabled=0 WHERE id=?", (hid,))
    if cur.rowcount == 0:
        return JSONResponse({"error": "钩子不存在"}, 404)
    conn.commit()
    audit(audit_user(user), "tool_hook_disable", f"停用工具钩子#{hid}", conn=conn)
    return {"ok": True}


@router.delete("/api/studio/tool-hooks/{hid}")
def delete_tool_hook(hid: int, conn=Depends(db_session), user=Depends(current_user)):
    cur = conn.execute("DELETE FROM tool_hooks WHERE id=?", (hid,))
    if cur.rowcount == 0:
        return JSONResponse({"error": "钩子不存在"}, 404)
    conn.commit()
    audit(audit_user(user), "tool_hook_delete", f"删除工具钩子#{hid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/skills/{sid}/copy")
def copy_skill(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """复制 Skill 数据（含依赖/角色/工具/参考/示例/脚本），名称 = 原名+副本。"""
    repo = StudioRepo(conn)
    row = repo.one("SELECT * FROM skills WHERE id=?", (sid,))
    if not row: return JSONResponse({"error": "Skill not found"}, 404)
    row = dict(row)
    name = _uniq_cast_name(conn, 'skill', f"{row['name']}副本")
    sid2 = repo.create_skill(
        name, row.get('description') or '', row.get('skill_type') or '',
        row.get('triggers') or '[]', row.get('category') or '',
        row.get('content') or '', row.get('frontmatter') or '', row.get('package_path') or '',
        status='draft',
        dependencies=row.get('dependencies') or '[]', allowed_roles=row.get('allowed_roles') or '[]',
        allowed_tools=row.get('allowed_tools') or '[]', references=row.get('references') or '[]',
        examples=row.get('examples') or '[]', scripts=row.get('scripts') or '[]')
    audit(audit_user(user), "skill_copy", f"复制Skill: {name}", conn=conn)
    return {"ok": True, "id": sid2, "name": name}


@router.post("/api/studio/tools/{tid}/copy")
def copy_tool(tid: int, conn=Depends(db_session), user=Depends(current_user)):
    """复制 HTTP 工具注册数据，名称 = 原名+副本。"""
    repo = StudioRepo(conn)
    row = repo.one("SELECT * FROM tools WHERE id=?", (tid,))
    if not row: return JSONResponse({"error": "Tool not found"}, 404)
    row = dict(row)
    name = _uniq_cast_name(conn, 'tool', f"{row['name']}副本")
    tid2 = repo.create_tool(
        name, row.get('description') or '', row.get('source') or '', row.get('input_schema') or '{}',
        row.get('version') or 'v1', row.get('side_effect') or 'read', row.get('risk_level') or 'low',
        row.get('owner') or '', row.get('status') or 'active',
        row.get('allowed_roles') or '[]', row.get('retry_policy') or '{}',
        row.get('fallback_to') or '', row.get('config') or '{}')
    audit(audit_user(user), "tool_copy", f"复制工具: {name}", conn=conn)
    return {"ok": True, "id": tid2, "name": name}


@router.post("/api/studio/mcp-servers/{sid}/copy")
def copy_mcp_server(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """复制 MCP 服务器配置，名称 = 原名+副本。"""
    repo = StudioRepo(conn)
    row = repo.one("SELECT * FROM mcp_servers WHERE id=?", (sid,))
    if not row: return JSONResponse({"error": "MCP not found"}, 404)
    row = dict(row)
    name = row['name'] + '副本'
    repo.create_mcp_server(name, row.get('endpoint') or '', row.get('tools') or '[]',
                           row.get('transport') or 'sse', row.get('command') or '',
                           row.get('args') or '[]', row.get('env') or '{}')
    nsid = repo.one("SELECT id FROM mcp_servers WHERE name=?", (name,))['id']
    audit(audit_user(user), "mcp_copy", f"复制MCP: {name}", conn=conn)
    return {"ok": True, "id": nsid, "name": name}


@router.post("/api/studio/agents/{aid}/copy")
def copy_agent(aid: int, conn=Depends(db_session), user=Depends(current_user)):
    """复制 Agent 数据（含工具绑定、主Agent团队关系），名称 = 原名+副本。"""
    repo = AgentRepo(conn)
    src = repo.get_agent(aid)
    if not src: return JSONResponse({"error": "Agent not found"}, 404)
    name = _uniq_cast_name(conn, 'agent', f"{src['name']}副本")
    data = {
        'name': name, 'display_name': (src.get('display_name') or src.get('name') or '') + '副本',
        'description': src.get('description', ''), 'system_prompt': src.get('system_prompt', ''),
        'model_provider_id': src.get('model_provider_id'), 'model_params': src.get('model_params') or {},
        'hil_level': src.get('hil_level', 'L0'), 'kb_required': src.get('kb_required'),
        'kb_scope': src.get('kb_scope') or {}, 'intent_keywords': src.get('intent_keywords') or [],
        'icon': src.get('icon', '🤖'), 'status': 'active', 'version': src.get('version', 'v1.0.0'),
        'capabilities': src.get('capabilities') or [], 'input_schema': src.get('input_schema') or {},
        'output_schema': src.get('output_schema') or {}, 'max_concurrency': src.get('max_concurrency') or 2,
        'protocol_range': src.get('protocol_range', '>=1,<3'), 'agent_role': src.get('agent_role', 'sub'),
    }
    aid2 = repo.create_agent(data)
    # 关系：复制工具绑定
    for tl in (src.get('tools') or []):
        repo.add_tool(aid2, tl.get('tool_type') or 'tool', tl.get('tool_name') or '', tl.get('params'))
    # 关系：主 Agent 复制其团队成员（指向相同的子 Agent）
    if src.get('agent_role') == 'main':
        repo.set_team(aid2, [m['id'] for m in (src.get('team_members') or [])])
    audit(audit_user(user), "agent_copy", f"复制Agent: {name}", conn=conn)
    return {"ok": True, "id": aid2, "name": name}
