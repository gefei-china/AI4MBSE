"""MCP-D1：MCP Server 健康巡检与动态发现核心逻辑。

- check_server(srv)：单服务器轻量探测（initialize 握手），复用 StudioRepo 落库状态/延迟/错误
- discover_server(srv)：全量动态发现（initialize + tools/list + resources/list + prompts/list）
- 后台巡检：start_health_loop() 启动 daemon 线程，按 interval_sec 轮询全部 sse/http 服务器

安全边界：仅对 HTTP(S) endpoint 发起 JSON-RPC 请求；stdio 不自动拉起子进程。
"""
import json
import threading
import time


def _norm_transport(t: str | None) -> str:
    """传输方式归一（★ B5-4：前端 `streamable_http` vs 后端 `http` 三方枚举不一致）。

    本模块有 **4 处**用 `transport in ("sse","http")` 做分支判断，
    而前端下拉给的默认值是 `streamable_http` ⇒ 这些判断会全部走不进来
    （表现为"界面配好了，健康巡检/动态发现却说不自动拉起"）。
    归一后与 `MCPClient.__init__` 用同一张映射表，避免两处各写一份。
    ⚠️ 兜底分支也**统一小写**：库里若存了 `'SSE'`，不归一就会在
    `in ("sse","http")` 处落空（大小写敏感）—— 同"枚举不一致"族的第4种表现。
    """
    return {"streamable_http": "http", "streamablehttp": "http",
            "https": "http", "": "http"}.get((t or "").strip().lower(),
                                             (t or "sse").strip().lower())


def _make_client(srv: dict):
    from mcp_client import MCPClient
    # ★ B5-2：带上认证头（否则健康探测对需认证的 MCP 永远 offline）
    _h = {}
    try:
        import json as _j
        _h = _j.loads(srv.get("headers") or "{}") or {}
    except Exception:
        _h = {}
    return MCPClient(
        endpoint=srv.get("endpoint", ""),
        transport=_norm_transport(srv.get("transport")),
        timeout=int(srv.get("timeout") or 15),
        headers=_h if isinstance(_h, dict) else {},
    )


def check_server(srv: dict, repo=None):
    """轻量健康探测：initialize 握手 → 更新状态/延迟/last_error。返回 (ok, detail)。"""
    import time as _t
    t0 = _t.time()
    transport = _norm_transport(srv.get("transport"))
    sid = srv["id"]
    if transport in ("sse", "http"):
        try:
            client = _make_client(srv)
            client.initialize(force=True)
            latency = int((_t.time() - t0) * 1000)
            if repo is not None:
                repo.update_mcp_status(sid, "online", latency)
            return True, {"status": "online", "latency_ms": latency,
                          "protocol": client.protocol_version}
        except Exception as e:
            latency = int((_t.time() - t0) * 1000)
            if repo is not None:
                repo.update_mcp_error(sid, f"{type(e).__name__}: {str(e)[:150]}")
            return False, {"status": "offline", "latency_ms": latency, "error": str(e)[:200]}
    return False, {"status": "unknown", "note": f"transport {transport} 仅校验命令不自动拉起"}


def discover_server(srv: dict, repo=None):
    """全量动态发现：initialize + 三原语 list → 落库。返回结果 dict。"""
    t0 = time.time()
    transport = _norm_transport(srv.get("transport"))
    sid = srv["id"]
    if transport not in ("sse", "http"):
        note = f"transport {transport} 不自动拉起子进程，跳过动态发现"
        return {"ok": False, "note": note}
    try:
        client = _make_client(srv)
        handshake = client.initialize(force=True)
        tools = client.list_tools()
        resources, prompts = [], []
        # 按能力声明按需拉取（MCP 规范：capabilities 中以键存在即表示支持，值可为空 dict）
        caps = client.capabilities or {}
        if "resources" in caps:
            try:
                resources = client.list_resources()
            except Exception:
                resources = []
        if "prompts" in caps:
            try:
                prompts = client.list_prompts()
            except Exception:
                prompts = []
        latency = int((time.time() - t0) * 1000)
        if repo is not None:
            repo.update_mcp_discovery(
                sid, "online", latency,
                json.dumps([t.get("name") for t in tools], ensure_ascii=False),
                json.dumps(caps, ensure_ascii=False),
                handshake.get("protocolVersion", ""),
                json.dumps(handshake.get("serverInfo", {}), ensure_ascii=False),
                json.dumps(resources, ensure_ascii=False),
                json.dumps(prompts, ensure_ascii=False),
            )
        return {
            "ok": True, "status": "online", "latency_ms": latency,
            "protocol": handshake.get("protocolVersion", ""),
            "server_info": handshake.get("serverInfo", {}),
            "capabilities": caps,
            "tools": [t.get("name") for t in tools],
            "resources": resources,
            "prompts": prompts,
        }
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        if repo is not None:
            repo.update_mcp_error(sid, f"{type(e).__name__}: {str(e)[:150]}")
        return {"ok": False, "status": "offline", "latency_ms": latency, "error": str(e)[:200]}


def check_all(repo):
    """批量巡检全部 sse/http 服务器。返回汇总。"""
    results = {"checked": 0, "online": 0, "offline": 0, "details": []}
    for srv in repo.rows("SELECT * FROM mcp_servers"):
        if _norm_transport(srv.get("transport")) not in ("sse", "http"):
            continue
        ok, detail = check_server(srv, repo=repo)
        results["checked"] += 1
        results["online" if ok else "offline"] += 1
        detail["id"] = srv["id"]
        detail["name"] = srv.get("name", "")
        results["details"].append(detail)
    return results


def start_health_loop(repo_factory, interval_sec: float = 300.0):
    """启动后台健康巡检 daemon 线程（main.py startup 调用）。"""
    def _loop():
        while True:
            try:
                conn = repo_factory()
                check_all(conn)
            except Exception:
                pass
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, name="mcp-health-loop", daemon=True)
    t.start()
    return t
