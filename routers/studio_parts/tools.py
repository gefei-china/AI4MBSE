# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：工具 CRUD/启停。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/tools")
def list_tools(conn=Depends(db_session)):
    """工具注册表列表（TR-P1/P2/P3：schema/版本/副作用/风险 + MCP 健康 + 使用统计）。"""
    repo = StudioRepo(conn)
    items = repo.list_tools()
    # 2026-09-17 第二刀：补插件维度字段（plugin_id / scope / share_status）。
    #   工具列表来自旧表 tools，无插件标识 —— 而「分享到市场」只能作用于 plugins 表的条目。
    #   此前前端只能按 name 反查，链路脆弱且与能力中心语义割裂；这里直接把插件标识挂上，
    #   卡片即可复用与技能/MCP 完全同一套分享入口（/api/plugins/{plugin_id}/share|unshare）。
    plugin_map = {}
    try:
        for r in repo.rows("SELECT plugin_id, name, manifest_json, scope, status FROM plugins "
                           "WHERE type='tool' AND status!='removed'"):
            try:
                m = json.loads(r.get("manifest_json") or "{}")
            except Exception:
                m = {}
            zh = ((m.get("label") or {}).get("zh_CN") or "")
            for k in (r.get("name"), zh):
                if k:
                    plugin_map[k] = r
    except Exception:
        pass
    # MCP 服务器在线状态（健康检查）
    mcp_status = {r["id"]: r["status"] for r in repo.rows("SELECT id, status FROM mcp_servers")}
    # TR-P3c：使用统计（调用次数/成功数/最近调用/从未使用标记）
    usage = {}
    try:
        for r in repo.rows(
            "SELECT tool_name, COUNT(*) cnt, COALESCE(SUM(ok),0) ok_cnt, MAX(created_at) last_at "
            "FROM tool_call_logs GROUP BY tool_name"):
            usage[r["tool_name"]] = {"count": r["cnt"], "ok_count": r["ok_cnt"], "last_at": r["last_at"]}
    except Exception:
        pass
    for t in items:
        try:
            t["input_schema"] = json.loads(t.get("input_schema") or "{}")
        except Exception:
            t["input_schema"] = {}
        try:
            t["allowed_roles"] = json.loads(t.get("allowed_roles") or "[]")
        except Exception:
            t["allowed_roles"] = []
        u = usage.get(t["name"]) or {}
        t["usage_count"] = u.get("count", 0)
        t["usage_ok"] = u.get("ok_count", 0)
        t["last_used_at"] = u.get("last_at", "")
        t["never_used"] = u.get("count", 0) == 0
        t["health"] = "ok"
        if t.get("source") == "mcp" and t.get("mcp_server_id") in mcp_status:
            t["health"] = mcp_status[t["mcp_server_id"]]
        # 插件维度：有映射才给分享入口（未纳入 plugins 表的工具无处可分享）
        pr = plugin_map.get(t.get("name"))
        if pr:
            t["plugin_id"] = pr["plugin_id"]
            t["scope"] = pr.get("scope") or "personal"
            t["share_status"] = ("submitted" if pr.get("scope") == "pending_public"
                                 else "rejected" if pr.get("status") == "rejected"
                                 else "")
    return items


@router.post("/api/studio/tools")
def create_tool(body: ToolIn, conn=Depends(db_session), user=Depends(current_user)):
    """新增工具（TR-P1 维护入口）。name 唯一；内置工具名保留。"""
    repo = StudioRepo(conn)
    name = body.name.strip()
    if not name:
        return JSONResponse({"error": "工具名必填"}, 400)
    if repo.get_tool_by_name(name):
        return JSONResponse({"error": f"工具已存在: {name}"}, 409)
    if name in ToolRegistry.BUILTINS:
        return JSONResponse({"error": f"{name} 是内置工具名，禁止同名注册"}, 400)
    if body.side_effect not in ("read", "write", "destructive"):
        return JSONResponse({"error": "side_effect 仅支持 read/write/destructive"}, 400)
    # 构建 config JSON（HTTP 工具专属字段）
    _cfg = {}
    if body.method: _cfg["method"] = body.method
    if body.url: _cfg["url"] = body.url
    if body.param_in: _cfg["param_in"] = body.param_in
    if body.timeout: _cfg["timeout"] = body.timeout
    if body.headers: _cfg["headers"] = body.headers
    sid = repo.create_tool(
        name, body.description or "", body.source or "local",
        json.dumps(body.input_schema or {}, ensure_ascii=False),
        body.version or "v1.0", body.side_effect or "read",
        body.risk_level or "low", body.owner or "",
        allowed_roles=json.dumps(body.allowed_roles or [], ensure_ascii=False),
        retry_policy=json.dumps(body.retry_policy or {}, ensure_ascii=False),
        fallback_to=(body.fallback_to or "").strip(),
        config=json.dumps(_cfg, ensure_ascii=False),
    )
    audit(audit_user(user), "tool_create", f"注册工具: {name} (side_effect={body.side_effect})", conn=conn)
    return {"ok": True, "id": sid}


@router.put("/api/studio/tools/{tid}")
def update_tool(tid: int, body: ToolIn, conn=Depends(db_session), user=Depends(current_user)):
    """编辑工具元数据（内置工具可编辑描述/schema/重试/降级，不可改名）。

    版本/副作用/风险/负责人/允许角色由注册来源（内置迁移/脚本注册）管理，
    编辑页不提供修改，避免 UI 误改分类元数据。
    """
    repo = StudioRepo(conn)
    t = repo.get_tool(tid)
    if not t:
        return JSONResponse({"error": "Tool not found"}, 404)
    # 构建 config JSON（HTTP 工具专属字段）——合并式更新：在原 config 基础上覆盖表单字段，
    # 保留表单不覆盖的键（body/data_path/defaults/query/max_response 等），避免 UI 编辑丢配置
    try:
        _prev = json.loads(t["config"]) if isinstance(t.get("config"), str) and t["config"].strip() else (t.get("config") or {})
    except Exception:
        _prev = {}
    _cfg = dict(_prev) if isinstance(_prev, dict) else {}
    if body.method: _cfg["method"] = body.method
    if body.url: _cfg["url"] = body.url
    if body.param_in: _cfg["param_in"] = body.param_in
    if body.timeout: _cfg["timeout"] = body.timeout
    if body.headers is not None: _cfg["headers"] = body.headers
    repo.update_tool(
        tid, body.description or "", json.dumps(body.input_schema or {}, ensure_ascii=False),
        retry_policy=json.dumps(body.retry_policy or {}, ensure_ascii=False),
        fallback_to=(body.fallback_to or "").strip(),
        config=json.dumps(_cfg, ensure_ascii=False),
    )
    audit(audit_user(user), "tool_update", f"更新工具#{tid}: {t['name']}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/tools/{tid}/disable")
def disable_tool(tid: int, conn=Depends(db_session), user=Depends(current_user)):
    """停用工具（生命周期：disabled = 标记不删除，Agent 停止消费，可重新启用）。"""
    repo = StudioRepo(conn)
    t = repo.get_tool(tid)
    if not t:
        return JSONResponse({"error": "Tool not found"}, 404)
    refs = repo.agent_tool_refs(t["name"])
    note = ""
    if refs:
        note = f"（{len(refs)} 个 Agent 仍绑定，将不再可用）"
    repo.set_tool_status(tid, "disabled")
    audit(audit_user(user), "tool_disable", f"停用工具#{tid}: {t['name']}{note}", conn=conn)
    return {"ok": True, "note": note}


@router.post("/api/studio/tools/{tid}/enable")
def enable_tool(tid: int, conn=Depends(db_session), user=Depends(current_user)):
    """重新启用工具（disabled/inactive → active）。"""
    repo = StudioRepo(conn)
    if not repo.get_tool(tid):
        return JSONResponse({"error": "Tool not found"}, 404)
    repo.set_tool_status(tid, "active")
    audit(audit_user(user), "tool_enable", f"启用工具#{tid}", conn=conn)
    return {"ok": True}


@router.delete("/api/studio/tools/{tid}")
def delete_tool(tid: int, conn=Depends(db_session), user=Depends(current_user)):
    """删除工具：内置工具禁止删除；被 Agent 绑定禁止删除（先解绑）。"""
    repo = StudioRepo(conn)
    t = repo.get_tool(tid)
    if not t:
        return JSONResponse({"error": "Tool not found"}, 404)
    if t["source"] == "builtin":
        return JSONResponse({"error": "内置工具禁止删除，可「停用」使其停止被消费"}, 400)
    refs = repo.agent_tool_refs(t["name"])
    if refs:
        return JSONResponse({"error": f"仍有 {len(refs)} 个 Agent 绑定该工具，请先解绑再删除"}, 409)
    repo.delete_tool(tid)
    audit(audit_user(user), "tool_delete", f"删除工具#{tid}: {t['name']}", conn=conn)
    return {"ok": True}
