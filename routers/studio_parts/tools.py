# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：工具 CRUD/启停。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


def _builtin_handler_index() -> dict:
    """内置工具名 → 真正执行它的那段代码（精确名部分）。

    ⚠️ 为什么必须有这张表：tools 表对内置工具的 config 恒为 **空** —— 内置工具由代码直接执行，
    本来就没有「URL / 鉴权」这类可配项。而工具详情页过去只展示 config，于是**看起来一片空白**，
    被误读成「没用」，18 个内置工具因此被删（2026-09-30）。这里把「谁执行」显式说出来，
    让「空 config」不再是「没内容」的同义词。

    来源只取执行器自己读的那几处事实源，不臆造；每处都标注了它在运行时 if 链里的行号。
    """
    idx = {}
    try:
        for _n, _d in (ToolRegistry.BUILTINS or {}).items():
            if _d.get("handler"):
                idx[_n] = _d["handler"].strip()
    except Exception:
        pass
    # sys_query_* —— 真实入口 agent/pipeline_parts/tools.py:554 按前缀路由，不经 ToolExecutor
    try:
        from sysadmin_tools import TOOLS as _SYS_TOOLS
        for _n, _d in (_SYS_TOOLS or {}).items():
            _fn = _d.get("fn")
            _fnname = getattr(_fn, "__name__", "") if _fn else ""
            idx[_n] = ("sysadmin_tools." + _fnname) if _fnname else "sysadmin_tools.exec_tool"
    except Exception:
        pass
    # 覆盖性分析 5 件 —— tools.py:568 按**名集合**路由（五个里只有 coverage_matrix 带 coverage_ 前缀，
    # 用前缀匹配会漏掉另外 4 个）。这里照抄同一个集合，⚠️别「优化」成前缀匹配。
    for _n in ("coverage_matrix", "trace_chain_check", "scene_coverage", "gap_summary", "modeling_coverage"):
        idx[_n] = "coverage_tools.exec_tool"
    idx["mbse_pull_ingest"] = "agent/pipeline_parts/tools.py:304 进程内直通入库（不走 HTTP，避免自环死锁）"
    return idx


_BUILTIN_HANDLERS = _builtin_handler_index()

# 按名字**前缀**路由的工具簇（同一簇共用一个 exec_tool 入口）
_PREFIX_ROUTES = (
    ("graph_db_", "graph_db_tools.exec_tool"),
    ("sysml_v2_", "sysml_check_tools.exec_tool"),   # 进程内直通 checker.jar，不走 HTTP
    ("zhiyuan_", "zhiyuan_client.exec_tool"),       # ⚠️仅当 kind 不为 http 时才走到这里，见 _tool_channel
)


def _code_handler(name: str) -> str:
    """进程内代码执行入口：先查精确名，再按前缀兜底；查不到返回 ''（不猜）。"""
    _n = (name or "").strip()
    if not _n:
        return ""
    _h = _BUILTIN_HANDLERS.get(_n)
    if _h:
        return _h
    for _pre, _mod in _PREFIX_ROUTES:
        if _n.startswith(_pre):
            return _mod
    return ""


def _tool_channel(t: dict, cfg: dict) -> dict:
    """判定工具的执行通道 —— 回答「点了调用，到底谁去干活」。

    ⚠️ 判定顺序必须逐条对齐运行时的 if 链（agent/pipeline_parts/tools.py:480 起）：
        MCP(:480) → kind='http'(:500) → 进程内代码(:524 起)
      顺序反了通道就标错。实测两个坑：
        ① 智源工具 source='zhiyuan' 但 **kind='http'** → 实际被 :500 的 http 执行器接走，
           :530 那条 zhiyuan_client 分支对这些工具到不了（若哪天有人清掉 kind，两侧就会漂移）；
        ② 「内置」不写在 source 里 —— source 实际是 builtin / coverage / graph_db / mbse 等多种取值，
           真正的共同点是「执行入口在代码里」，别拿 source=='builtin' 当内置判据。
    """
    _name = t.get("name") or ""
    _src = (t.get("source") or "").strip().lower()
    _kind = (t.get("kind") or "").strip().lower()
    if _src == "mcp" or t.get("mcp_server_id"):
        return {
            "channel": "mcp",
            "channel_label": "MCP 服务",
            "handler": "mcp_client.MCPClient.call_tool",
            "handler_note": "按所属 MCP Server 的端点发起 JSON-RPC 调用（端点在「MCP 服务器」列表维护）。",
            "config_required": True,
        }
    if _kind == "http":
        # config 里出现 {cfg:xxx} 占位符 = 引用系统配置（如智源 base_url/token），
        # 这类工具在表单里改 URL 是没用的，必须到系统配置那层去改 —— 说清楚，别让人白改。
        _raw = json.dumps(cfg, ensure_ascii=False) if isinstance(cfg, dict) else str(cfg or "")
        _placeholder = "{cfg:" in _raw
        return {
            "channel": "http",
            "channel_label": "HTTP 接口",
            "handler": "http_tool_executor.exec_http_tool",
            "handler_note": ("按本页「HTTP 接口配置」发起请求。⚠️ 该工具的 URL/鉴权写成 {cfg:...} 占位符，"
                             "实际取值来自「系统配置」，改这里的输入框不会生效。" if _placeholder else
                             "按本页「HTTP 接口配置」的 URL / Method / 参数映射发起请求。"),
            "config_required": True,
        }
    _h = _code_handler(_name)
    if _h:
        return {
            "channel": "builtin-code",
            "channel_label": "内置能力（代码执行）",
            "handler": _h,
            "handler_note": ("由平台代码直接执行，无需填写 URL / 鉴权 / 端点等配置项；"
                             "下方展示的是它接收的参数契约。"),
            "config_required": False,
        }
    return {
        "channel": "unbound",
        "channel_label": "未识别执行通道",
        "handler": "",
        "handler_note": "既非内置代码、也不是 HTTP/MCP 集成，运行时将报「未知工具」。请补充 HTTP 配置或删除该条目。",
        "config_required": True,
    }


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
        # 执行通道：告诉前端「这个工具由谁执行、要不要配置」—— 内置工具无需配置也必须说清楚，
        # 否则页面只剩一片空白会被当成无用条目（见 _builtin_handler_index 的说明）
        try:
            _raw = t.get("config")
            _cfg = json.loads(_raw or "{}") if isinstance(_raw, str) else (_raw or {})
        except Exception:
            _cfg = {}
        t.update(_tool_channel(t, _cfg if isinstance(_cfg, dict) else {}))
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
    # P0-3（2026-09-30）：名字合法性前置校验 —— 运行时会把它当 OpenAI function.name 发出去，
    #   协议要求 ^[a-zA-Z0-9_-]{1,64}$；不合法的名字不会报错，而是被**静默剔除**
    #   （agent/pipeline_parts/tools.py:232），注册完显示成功、实际永远调不动。能在入口拦就别留到运行时。
    _nerr = check_tool_name(name)
    if _nerr:
        return JSONResponse({"error": _nerr}, 400)
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


@router.get("/api/studio/tools/{tid}/refs")
def tool_refs(tid: int, conn=Depends(db_session)):
    """工具影响面：哪些在用 Agent 绑定了它（供**停用/删除之前**如实告知，把拍板权交回给用户）。

    ⚠️ 为什么单独开一个 GET：停用是不可逆感的强操作，而内置工具停用后依赖它的 Agent 是**静默失效**
    —— 既不报错也不提示。2026-09-30 那次 18 个内置工具被误清，起点就是「页面看着是空的、以为没用」。
    确认框要能说出「会影响谁」，就必须先有一路只读查询，不能靠前端猜。
    """
    repo = StudioRepo(conn)
    t = repo.get_tool(tid)
    if not t:
        return JSONResponse({"error": "Tool not found"}, 404)
    rows = repo.rows(
        "SELECT DISTINCT a.id, a.name, a.display_name, a.status FROM agent_tools at "
        "JOIN agents a ON a.id=at.agent_id "
        "WHERE at.tool_name=? AND at.enabled=1 ORDER BY a.display_name", (t["name"],))
    return {
        "name": t["name"],
        "source": t.get("source") or "",
        "builtin": bool(t.get("source") == "builtin" or (t.get("builtin") or 0) == 1),
        "count": len(rows),
        "refs": rows,
    }


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
