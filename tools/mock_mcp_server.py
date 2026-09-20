"""D1 前端闭环验证用 Mock MCP Server（JSON-RPC over HTTP POST）。

支持 initialize / tools/list / tools/call / resources/list / resources/read /
prompts/list / prompts/get / ping。用法：python tools/mock_mcp_server.py [port]
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "search_entity", "description": "按名称检索知识实体",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "gen_sysml_view", "description": "生成 SysML 视图",
     "inputSchema": {"type": "object", "properties": {"view": {"type": "string"}}}},
]
RESOURCES = [
    {"uri": "mock://kb/overview", "name": "知识库概览", "description": "当前分支实体统计"},
    {"uri": "mock://rules/sysml", "name": "SysML 建模规则", "description": "平台建模规范"},
]
PROMPTS = [
    {"name": "review_sysml", "description": "按 OMG 标准评审 SysML v2 代码", "arguments": []},
    {"name": "generate_bdd", "description": "生成块定义图 BDD", "arguments": []},
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}})
            return
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "Mock-建模", "version": "1.0.0"},
                "instructions": "Mock server for D1 frontend closed-loop verification.",
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name", "")
            result = {"content": [{"type": "text", "text": f"Mock 工具 {name} 已执行"}]}
        elif method == "resources/list":
            result = {"resources": RESOURCES}
        elif method == "resources/read":
            uri = (req.get("params") or {}).get("uri", "")
            result = {"contents": [{"type": "text", "text": f"[Mock 资源] {uri}：共 116 实体 / 32 候选 / 5 已评审"}]}
        elif method == "prompts/list":
            result = {"prompts": PROMPTS}
        elif method == "prompts/get":
            name = (req.get("params") or {}).get("name", "")
            result = {"description": name, "messages": [{"role": "user", "content": {"type": "text", "text": "请评审以下 SysML v2 代码…"}}]}
        elif method == "ping":
            result = {}
        else:
            self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            return
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _send(self, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19001
    print(f"Mock MCP server listening on 127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
