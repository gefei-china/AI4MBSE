# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：MCP 服务器管理与探测。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


def _srv_headers(srv: dict) -> dict:
    """从 mcp_servers 行取认证头（★ B5-2）。

    解析失败**不阻断**（按无 header 处理）—— 配置坏了应让探测/调用报 401，
    而不是在读配置的瞬间抛一个更让人困惑的异常。
    """
    try:
        h = json.loads(srv.get("headers") or "{}")
    except Exception:
        return {}
    return {str(k): str(v) for k, v in h.items() if k and v} if isinstance(h, dict) else {}


def _mask_headers(srv: dict) -> dict:
    """★ B5-2：MCP 出参**脱敏** —— 只回 key 名与长度，绝不回 header 值。

    为什么必须脱敏：`headers` 里放的是 `Authorization: Bearer …`，
    而 `GET /api/studio/mcp-servers` 是**任何登录用户都能调**的列表接口
    ⇒ 不脱敏等于把每个 MCP 的凭据广播给所有用户。
    ⚠️ 这是「新增/编辑时能用、列表时不能回显」的常见形态（与 token 类配置同纪律），
    前端编辑框应显示"已配置 N 个 header"并允许逐项覆盖重填。
    """
    import json as _json
    out = {}
    try:
        h = _json.loads(srv.get("headers") or "{}")
    except Exception:
        h = {}
    for k, v in (h or {}).items():
        s = str(v or "")
        out[k] = {"set": bool(s), "len": len(s)}
    return out


@router.get("/api/studio/mcp-servers")
def list_mcp_servers(conn=Depends(db_session)):
    items = StudioRepo(conn).list_mcp_servers()
    # ★ B5-2：出参脱敏（headers 值绝不出现在列表接口）
    for it in (items or []):
        if isinstance(it, dict):
            it["headers_masked"] = _mask_headers(it)
            it.pop("headers", None)
            # ★ B5-3：附真实调用统计 —— 让「这个 MCP 到底有没有被用过」**可见**。
            #   口径必须用 `audit_logs.event_type='mcp_tool_call'`：
            #   ⚠️ `tool_call_logs` 记的是 **Agent 路径**的工具调用，
            #      **MCP 网关自己的调用不落在那里** —— 我先前用错表得出
            #      「内置 MCP 零调用」的结论，实测审计里有 22 次（2026-09-24）。
            #   ⇒ 「有没有被用」这个问题，**问错了表就得到相反答案**。
            #   ⚠️ 口径踩过的坑（两次，都会给出"看起来对"的错数字）：
            #     ① `tool_call_logs` 记的是 **Agent 路径**的工具调用，
            #        **MCP 网关自己的调用不落在那里** ⇒ 用它查得到「零调用」的错误结论
            #        （实测 `audit_logs` 里明明有 22 次，2026-09-24）。
            #     ② 审计 `detail` 实际格式是「[MCP供给] graphrag_search 成功」，
            #        **不含服务器名** ⇒ 用服务器名 `LIKE '%name%'` **恒为 0**
            #        （又一个"看起来在起作用、实则恒假"的判据）。
            #   ⇒ 正确口径：`audit_logs` 里 event_type='mcp_tool_call' 的**全局计数**。
            #     ⚠️ **GAP**：审计尚未按服务器维度区分（detail 里没有服务器标识），
            #     所以这是"平台 MCP 网关被调用过多少次"，**不能**归因到某个具体服务器。
            #     要精确归因需给审计 detail 加服务器标识 —— 已记入待办，不在这里猜。
            try:
                _n, _last = conn.execute(
                    "SELECT COUNT(*), MAX(created_at) FROM audit_logs "
                    "WHERE event_type='mcp_tool_call'").fetchone()
                it["call_total"] = _n or 0
                it["call_last"] = _last or ""
                it["call_scope"] = "platform_gateway_all"
            except Exception:
                it["call_total"] = 0
                it["call_last"] = ""
                it["call_scope"] = "unavailable"
    return items


@router.post("/api/studio/mcp-servers")
def create_mcp_server(body: MCPIn, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).create_mcp_server(
        body.name, body.endpoint, json.dumps(body.tools, ensure_ascii=False),
        body.transport, body.command, json.dumps(body.args, ensure_ascii=False),
        json.dumps(body.env, ensure_ascii=False),
        json.dumps(body.headers or {}, ensure_ascii=False),   # ★ B5-2
        description=body.description or "")                   # ★ B5-4
    audit(audit_user(user), "mcp_create", f"添加MCP服务器: {body.name}", conn=conn)
    return {"ok": True}


@router.put("/api/studio/mcp-servers/{sid}")
def update_mcp_server(sid: int, body: MCPIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    if not repo.get_mcp_server(sid):
        return JSONResponse({"error": "MCP server not found"}, 404)
    # ★ B5-2：`headers` 字段**未提供**时传 None（repo 层区分"未提供"与"显式清空"）
    # ★ B5-4：`tools` / `args` / `env` / `description` 同理 ——
    #   前端编辑弹窗**不掌握** tools（那是探测发现的清单）与 args/env，
    #   若照单全收地写回 `tools=[]` ⇒ **一次编辑保存就把已发现的工具清单清空**，
    #   该 MCP 对 Agent 立刻彻底不可用（实测 dev_ai_mcp 有 4 个工具，保存后归零）。
    #   ⇒ 只有前端**真的填了**才写，未提供的一律保持原值。
    #   description 例外：它就是给人填的，前端总会带（空串 = 用户主动清空）。
    _tools = json.dumps(body.tools, ensure_ascii=False) if body.tools else None
    _args = json.dumps(body.args, ensure_ascii=False) if body.args else None
    _env = json.dumps(body.env, ensure_ascii=False) if body.env else None
    repo.update_mcp_server(
        sid, body.name, body.endpoint, _tools,
        body.transport, body.command or None, _args, _env,
        json.dumps(body.headers, ensure_ascii=False)
        if body.headers is not None else None,
        description=body.description)
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
    # ★ B5-4：与 `mcp_health._norm_transport` 同一张映射表 ——
    #   前端下拉存的是 `streamable_http`，此处若直接拿去比对白名单就会落空。
    from mcp_health import _norm_transport
    from mcp_client import _parse_jsonrpc_body   # ★ B5-4：与真实调用共用同一解析器
    transport = _norm_transport(srv.get("transport"))
    # ★ B5-2：带上该服务器配置的认证头（此前**硬编码只有 Content-Type**
    #   ⇒ 需要 Bearer 的 MCP 必然 401 ⇒ 实测"新增成功但用不了"）。
    #   ⚠️ 解析失败**不阻断**（按无 header 处理）—— 配置坏了不该让探测崩掉。
    _hdr = {"Content-Type": "application/json"}
    try:
        _hs = json.loads(srv.get("headers") or "{}")
        if isinstance(_hs, dict):
            _hdr.update({str(k): str(v) for k, v in _hs.items() if k and v})
    except Exception:
        pass
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
            # MCP Streamable HTTP 服务端普遍要求 Accept 同时含两种类型
            _hdr.setdefault("Accept", "application/json, text/event-stream")
            with httpx.Client(timeout=8) as client:
                r = client.post(url, json=payload, headers=_hdr)
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}: {r.text[:120]}")
                # ★ B5-4：用共用的双封装解析器（此前裸 `r.json()`，
                #   对返回 text/event-stream 的服务端（含**平台自己的内置网关**）必抛
                #   `Expecting value: line 1 column 1` ⇒ 内置 MCP 恒 offline）
                init_data = _parse_jsonrpc_body(r)
                # tools/list
                tools_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                r2 = client.post(url, json=tools_payload, headers=_hdr)
                tools = []
                if r2.status_code == 200:
                    tr = _parse_jsonrpc_body(r2)   # ★ B5-4：同上，tools/list 同样可能是 SSE 封装
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
    client = MCPClient(endpoint=srv["endpoint"], transport=srv.get("transport", "sse"),
                       headers=_srv_headers(srv))          # ★ B5-2
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
    client = MCPClient(endpoint=srv["endpoint"], transport=srv.get("transport", "sse"),
                       headers=_srv_headers(srv))          # ★ B5-2
    return client.get_prompt(name)
