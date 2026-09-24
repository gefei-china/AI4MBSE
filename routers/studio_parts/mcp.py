# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：MCP 服务器管理与探测。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/mcp-servers")
def list_mcp_servers(conn=Depends(db_session)):
    return StudioRepo(conn).list_mcp_servers()


@router.post("/api/studio/mcp-servers")
def create_mcp_server(body: MCPIn, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).create_mcp_server(
        body.name, body.endpoint, json.dumps(body.tools, ensure_ascii=False),
        body.transport, body.command, json.dumps(body.args, ensure_ascii=False),
        json.dumps(body.env, ensure_ascii=False))
    audit(audit_user(user), "mcp_create", f"添加MCP服务器: {body.name}", conn=conn)
    return {"ok": True}


@router.put("/api/studio/mcp-servers/{sid}")
def update_mcp_server(sid: int, body: MCPIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    if not repo.get_mcp_server(sid):
        return JSONResponse({"error": "MCP server not found"}, 404)
    repo.update_mcp_server(
        sid, body.name, body.endpoint, json.dumps(body.tools, ensure_ascii=False),
        body.transport, body.command, json.dumps(body.args, ensure_ascii=False),
        json.dumps(body.env, ensure_ascii=False))
    audit(audit_user(user), "mcp_update", f"更新MCP服务器#{sid}: {body.name}", conn=conn)
    return {"ok": True}


@router.delete("/api/studio/mcp-servers/{sid}")
def delete_mcp_server(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    srv = repo.get_mcp_server(sid)
    if not srv:
        return JSONResponse({"error": "MCP server not found"}, 404)
    if srv.get("builtin"):
        return JSONResponse({"error": "内置 MCP 服务器禁止删除，可「编辑」修改配置"}, 400)
    repo.delete_mcp_server(sid)
    audit(audit_user(user), "mcp_delete", f"删除MCP服务器#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/mcp-servers/{sid}/enable")
def enable_mcp_server(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """启用 MCP 服务器（停用后不再注入 Agent、不参与能力发现）。"""
    repo = StudioRepo(conn)
    if not repo.get_mcp_server(sid):
        return JSONResponse({"error": "MCP server not found"}, 404)
    repo.set_mcp_enabled(sid, 1)
    audit(audit_user(user), "mcp_enable", f"启用MCP服务器#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/mcp-servers/{sid}/disable")
def disable_mcp_server(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """停用 MCP 服务器（停用后不再注入 Agent、不参与能力发现）。"""
    repo = StudioRepo(conn)
    if not repo.get_mcp_server(sid):
        return JSONResponse({"error": "MCP server not found"}, 404)
    repo.set_mcp_enabled(sid, 0)
    audit(audit_user(user), "mcp_disable", f"停用MCP服务器#{sid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/mcp-servers/{sid}/test")
def test_mcp_server(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """MCP 连通性测试（P0 平台化）：HTTP/SSE 握手 initialize + tools/list，自动落库工具清单。

    stdio 模式不自动拉起子进程（安全考量），仅校验命令可解析并提示手动验证。
    """
    import time
    import httpx
    repo = StudioRepo(conn)
    srv = repo.get_mcp_server(sid)
    if not srv:
        return JSONResponse({"error": "MCP server not found"}, 404)

    t0 = time.time()
    transport = srv.get("transport", "sse")
    if transport in ("sse", "http"):
        url = srv["endpoint"].rstrip("/")
        if not url.startswith("http"):
            url = "http://" + url
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "ai4mbse", "version": "1.0"},
                },
            }
            with httpx.Client(timeout=8) as client:
                r = client.post(url, json=payload, headers={"Content-Type": "application/json"})
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}: {r.text[:120]}")
                init_data = r.json()
                # tools/list
                tools_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                r2 = client.post(url, json=tools_payload, headers={"Content-Type": "application/json"})
                tools = []
                if r2.status_code == 200:
                    tr = r2.json()
                    tools = [t.get("name") for t in (tr.get("result", {}).get("tools") or [])]
            latency = int((time.time() - t0) * 1000)
            repo.update_mcp_status(sid, "online", latency, json.dumps(tools, ensure_ascii=False))
            audit(audit_user(user), "mcp_test", f"MCP#{sid} 连通: {len(tools)} 工具, {latency}ms", conn=conn)
            return {"ok": True, "status": "online", "latency_ms": latency,
                    "tools": tools, "protocol": init_data.get("result", {}).get("protocolVersion", "")}
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            repo.update_mcp_status(sid, "offline", latency)
            audit(audit_user(user), "mcp_test", f"MCP#{sid} 失败: {str(e)[:100]}", conn=conn)
            return JSONResponse({"ok": False, "status": "offline", "error": str(e)[:200]}, 502)
    else:
        # stdio：不自动拉起（安全），校验命令存在性
        import shutil
        cmd = (srv.get("command") or "").split()[0] if srv.get("command") else ""
        ok = bool(cmd) and shutil.which(cmd) is not None
        status = "online" if ok else "offline"
        repo.update_mcp_status(sid, status, 0)
        audit(audit_user(user), "mcp_test", f"MCP#{sid} stdio 校验: {cmd} {'found' if ok else 'missing'}", conn=conn)
        return {"ok": ok, "status": status, "note": f"stdio 命令校验: {cmd or '(未配置)'}"}


@router.post("/api/studio/mcp-servers/{sid}/discover")
def discover_mcp_server(sid: int, conn=Depends(db_session), user=Depends(current_user)):
    """全量动态发现：initialize 握手 → 能力协商 → 三类原语 list → 落库。

    返回协议版本/能力声明/工具/资源/提示模板清单，供前端展示与后续调用。
    """
    from mcp_health import discover_server
    repo = StudioRepo(conn)
    srv = repo.get_mcp_server(sid)
    if not srv:
        return JSONResponse({"error": "MCP server not found"}, 404)
    result = discover_server(srv, repo=repo)
    audit(audit_user(user), "mcp_discover", f"MCP#{sid} 动态发现: {result.get('status')} "
          f"{len(result.get('tools') or [])}工具/{len(result.get('resources') or [])}资源/"
          f"{len(result.get('prompts') or [])}提示词", conn=conn)
    if not result.get("ok"):
        return JSONResponse(result, 502)
    return result


@router.post("/api/studio/mcp-servers/check-all")
def check_all_mcp_servers(conn=Depends(db_session), user=Depends(current_user)):
    from mcp_health import check_all
    repo = StudioRepo(conn)
    results = check_all(repo)
    audit(audit_user(user), "mcp_check_all", f"批量巡检: {results['online']}/{results['checked']} 在线", conn=conn)
    return results


@router.get("/api/studio/mcp-servers/{sid}/read-resource")
def read_mcp_resource(sid: int, uri: str, conn=Depends(db_session)):
    from mcp_client import MCPClient
    repo = StudioRepo(conn)
    srv = repo.get_mcp_server(sid)
    if not srv:
        return JSONResponse({"error": "MCP server not found"}, 404)
    if srv.get("transport", "sse") not in ("sse", "http"):
        return JSONResponse({"error": "stdio 资源读取暂不支持"}, 400)
    client = MCPClient(endpoint=srv["endpoint"], transport=srv.get("transport", "sse"))
    return client.read_resource(uri)


@router.get("/api/studio/mcp-servers/{sid}/get-prompt")
def get_mcp_prompt(sid: int, name: str, conn=Depends(db_session)):
    """提示模板原语（prompts/get）。"""
    from mcp_client import MCPClient
    repo = StudioRepo(conn)
    srv = repo.get_mcp_server(sid)
    if not srv:
        return JSONResponse({"error": "MCP server not found"}, 404)
    if srv.get("transport", "sse") not in ("sse", "http"):
        return JSONResponse({"error": "stdio 提示模板获取暂不支持"}, 400)
    client = MCPClient(endpoint=srv["endpoint"], transport=srv.get("transport", "sse"))
    return client.get_prompt(name)
