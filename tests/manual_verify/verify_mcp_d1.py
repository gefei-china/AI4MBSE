"""MCP-D1 闭环验证：Mock MCP Server（HTTP JSON-RPC）+ 平台接口全链路。

覆盖：
1. 创建 MCP 服务器 → discover 动态发现（initialize 握手/能力协商/三原语清单落库）
2. resources/read 资源原语调用
3. prompts/get 提示模板原语调用
4. check-all 批量健康巡检
5. 列表返回新增列（capabilities/protocol_version/resources/prompts/last_error）
"""
import os
import sys
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d1_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

import httpx

MOCK_PORT = 19010
APP_PORT = 18010
BASE = f"http://127.0.0.1:{APP_PORT}"

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


class MockMCPHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(ln) or b"{}")
        method = payload.get("method")
        result = None
        error = None
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "mock-mcp-d1", "version": "9.9.9"},
                "instructions": "Mock MCP Server for D1 verification",
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": "mock_tool_a", "description": "工具A", "inputSchema": {"type": "object"}},
                {"name": "mock_tool_b", "description": "工具B", "inputSchema": {"type": "object"}},
            ]}
        elif method == "resources/list":
            result = {"resources": [
                {"uri": "mock://schema/req", "name": "需求Schema", "mimeType": "text/markdown"},
                {"uri": "mock://docs/guide", "name": "建模指南", "mimeType": "text/markdown"},
            ]}
        elif method == "resources/read":
            uri = (payload.get("params") or {}).get("uri", "")
            result = {"contents": [{"uri": uri, "mimeType": "text/markdown",
                                    "text": f"# 资源内容 {uri}\n这是模拟资源正文。"}]}
        elif method == "prompts/list":
            result = {"prompts": [
                {"name": "req_review", "description": "需求评审模板", "arguments": [{"name": "req_id", "required": True}]},
            ]}
        elif method == "prompts/get":
            result = {"description": "需求评审模板",
                      "messages": [{"role": "system", "content": "你是一名需求评审专家，请评审 #{req_id}"}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "mock_tool 执行结果"}], "isError": False}
        else:
            error = {"code": -32601, "message": f"Method not found: {method}"}

        body = {"jsonrpc": "2.0", "id": payload.get("id")}
        if error:
            body["error"] = error
        else:
            body["result"] = result
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock_mcp():
    srv = HTTPServer(("127.0.0.1", MOCK_PORT), MockMCPHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_app():
    import uvicorn
    from main import app
    threading.Thread(target=lambda: uvicorn.run(
        app, host="127.0.0.1", port=APP_PORT, log_level="error"), daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"{BASE}/api/studio/mcp-servers", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("app 启动超时")


def main():
    start_mock_mcp()
    time.sleep(0.3)
    start_app()
    c = httpx.Client(timeout=15, base_url=BASE)

    # 1. 创建 MCP 服务器
    r = c.post("/api/studio/mcp-servers", json={
        "name": "mock-mcp-d1", "endpoint": f"http://127.0.0.1:{MOCK_PORT}",
        "transport": "http", "tools": [], "args": [], "env": {}})
    check("创建 MCP 服务器", r.status_code == 200, r.text)
    servers = c.get("/api/studio/mcp-servers").json()
    sid = next(s["id"] for s in servers if s["name"] == "mock-mcp-d1")

    # 2. discover 动态发现（initialize + 三原语）
    r = c.post(f"/api/studio/mcp-servers/{sid}/discover")
    d = r.json()
    check("discover 状态码", r.status_code == 200, r.text)
    check("discover 握手协议版本", d.get("protocol") == "2025-06-18", str(d.get("protocol")))
    check("discover serverInfo", (d.get("server_info") or {}).get("name") == "mock-mcp-d1", str(d.get("server_info")))
    check("discover 能力协商", set((d.get("capabilities") or {}).keys()) == {"tools", "resources", "prompts"}, str(d.get("capabilities")))
    check("discover 工具清单", len(d.get("tools") or []) == 2, str(d.get("tools")))
    check("discover 资源清单", len(d.get("resources") or []) == 2, str(d.get("resources")))
    check("discover 提示模板", len(d.get("prompts") or []) == 1, str(d.get("prompts")))

    # 3. 列表返回新增列（落库回读）——针对 mock 服务器
    servers = c.get("/api/studio/mcp-servers").json()
    s = next(x for x in servers if x["name"] == "mock-mcp-d1")
    check("列表含 capabilities", s.get("capabilities", "").startswith("{"), str(s.get("capabilities")))
    check("列表含 protocol_version", s.get("protocol_version") == "2025-06-18", s.get("protocol_version"))
    check("列表含 resources 列", s.get("resources", "").startswith("["), s.get("resources"))
    check("列表含 prompts 列", s.get("prompts", "").startswith("["), s.get("prompts"))
    check("列表含 last_discover", bool(s.get("last_discover")), s.get("last_discover"))
    check("列表状态 online", s.get("status") == "online", s.get("status"))

    # 4. resources/read 资源原语
    r = c.get(f"/api/studio/mcp-servers/{sid}/read-resource",
              params={"uri": "mock://schema/req"})
    rd = r.json()
    check("read-resource ok", rd.get("ok") is True and not rd.get("is_error"), r.text)
    check("read-resource 内容", "模拟资源正文" in rd.get("result", ""), rd.get("result", "")[:50])

    # 5. prompts/get 提示模板原语
    r = c.get(f"/api/studio/mcp-servers/{sid}/get-prompt", params={"name": "req_review"})
    pd = r.json()
    check("get-prompt ok", pd.get("ok") is True, r.text)
    check("get-prompt 模板消息", "需求评审专家" in pd.get("result", ""), pd.get("result", "")[:60])

    # 6. check-all 批量健康巡检（mock 服务器必须 online 且协议正确）
    r = c.post("/api/studio/mcp-servers/check-all")
    ca = r.json()
    mock = next((d for d in ca.get("details", []) if d.get("name") == "mock-mcp-d1"), {})
    check("check-all mock 在线", ca.get("checked", 0) >= 1 and mock.get("status") == "online",
          json.dumps(ca, ensure_ascii=False)[:300])
    check("check-all mock 协议", mock.get("protocol") == "2025-06-18", str(mock))

    print(f"\n===== D1 RESULT: PASS={PASS} FAIL={FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
