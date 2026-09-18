"""缺口B：MCP 工具真实执行客户端。

对 HTTP/SSE transport 的 MCP Server 发起 JSON-RPC tools/call 调用，
返回真实工具执行结果（不再是 catalog/inspect 的声明层）。

安全边界：
- 仅发起 HTTP(S) JSON-RPC 请求（不拉起 stdio 子进程，防任意代码执行）
- 超时 15s，失败返回结构化错误供 LLM 兜底
- 响应体截断（防大响应打爆 context）
"""
import json
import time


class MCPClient:
    """轻量 MCP 客户端：initialize 握手 + 三类原语（Tools/Resources/Prompts）。

    - tools/list（探测） + tools/call（执行）
    - resources/list + resources/read（资源注入上下文）
    - prompts/list + prompts/get（预定义提示模板）

    超时与截断上限默认取系统配置（core.config 的 mcp 分组），可实例化时覆盖。
    握手结果（capabilities/serverInfo/protocolVersion）缓存于实例，供动态发现落库。
    """

    def __init__(self, endpoint: str, transport: str = "sse",
                 timeout: int | None = None, max_response: int | None = None):
        from core import config
        self.endpoint = endpoint
        self.transport = transport
        self.timeout = timeout if timeout is not None else int(config.get("mcp", "timeout", 15))
        self.max_response = max_response if max_response is not None else int(config.get("mcp", "max_response", 4000))
        self._id = 0
        self._handshake = None          # initialize 结果缓存（未握手为 None）
        self.capabilities = {}          # 服务端能力声明
        self.server_info = {}           # serverInfo
        self.protocol_version = ""

    def _post(self, method: str, params: dict, timeout: int | None = None) -> dict:
        import httpx
        if self.transport not in ("sse", "http"):
            raise ValueError(f"MCP transport '{self.transport}' 暂不支持真实调用（仅 sse/http）")
        url = self.endpoint.rstrip("/")
        if not url.startswith("http"):
            url = "http://" + url
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params,
        }
        resp = httpx.post(url, json=payload, headers={"Content-Type": "application/json"},
                          timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.json()

    def initialize(self, force: bool = False) -> dict:
        """initialize 握手：协商 protocolVersion 并读取 capabilities/serverInfo。

        握手结果缓存，force=True 时强制重新握手。
        """
        if self._handshake is not None and not force:
            return self._handshake
        data = self._post("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "ai4mbse", "version": "1.0"},
        }, timeout=min(self.timeout, 8))
        result = data.get("result") or {}
        self.protocol_version = result.get("protocolVersion", "")
        self.capabilities = result.get("capabilities", {}) or {}
        self.server_info = result.get("serverInfo", {}) or {}
        self._handshake = {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities,
            "serverInfo": self.server_info,
            "instructions": result.get("instructions", ""),
        }
        return self._handshake

    def list_tools(self) -> list:
        """tools/list：返回 [{name, description, inputSchema}]。"""
        data = self._post("tools/list", {})
        result = data.get("result") or data
        return result.get("tools", [])

    def list_resources(self) -> list:
        """resources/list：返回 [{uri, name, description, mimeType}]。"""
        data = self._post("resources/list", {})
        result = data.get("result") or data
        return result.get("resources", [])

    def read_resource(self, uri: str, max_len: int | None = None) -> dict:
        """resources/read：读取资源内容。

        返回 {ok, result, is_error, latency_ms}——ok=False 时 result 为错误描述。
        """
        t0 = time.time()
        try:
            data = self._post("resources/read", {"uri": uri})
            latency = int((time.time() - t0) * 1000)
            result = data.get("result") or {}
            contents = result.get("contents", [])
            texts = []
            for item in contents if isinstance(contents, list) else [contents]:
                if isinstance(item, dict):
                    texts.append(str(item.get("text") or json.dumps(item, ensure_ascii=False)))
                else:
                    texts.append(str(item))
            limit = max_len or self.max_response
            return {
                "ok": data.get("error") is None,
                "result": ("\n".join(texts) or "")[:limit],
                "is_error": data.get("error") is not None,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return {"ok": False, "result": f"资源读取失败: {str(e)[:200]}", "is_error": True, "latency_ms": latency}

    def list_prompts(self) -> list:
        """prompts/list：返回 [{name, description, arguments}]。"""
        data = self._post("prompts/list", {})
        result = data.get("result") or data
        return result.get("prompts", [])

    def get_prompt(self, name: str, arguments: dict | None = None, max_len: int | None = None) -> dict:
        """prompts/get：获取预定义提示模板（含 messages）。"""
        t0 = time.time()
        try:
            data = self._post("prompts/get", {"name": name, "arguments": arguments or {}})
            latency = int((time.time() - t0) * 1000)
            result = data.get("result") or {}
            limit = max_len or self.max_response
            return {"ok": True, "result": json.dumps(result, ensure_ascii=False)[:limit],
                    "is_error": False, "latency_ms": latency}
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return {"ok": False, "result": f"提示模板获取失败: {str(e)[:200]}", "is_error": True, "latency_ms": latency}

    def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """tools/call：真实执行工具，返回标准化结果。

        返回 {ok, result, is_error, latency_ms}——ok=False 时 result 为错误描述。
        """
        t0 = time.time()
        try:
            data = self._post("tools/call", {"name": tool_name, "arguments": arguments or {}})
            latency = int((time.time() - t0) * 1000)
            result = data.get("result") or {}
            content = result.get("content", [])
            is_error = bool(result.get("isError")) or data.get("error") is not None
            # 抽取文本内容（content 可能是 [{type:"text",text:...}] 或纯文本）
            texts = []
            for item in content if isinstance(content, list) else [content]:
                if isinstance(item, dict):
                    texts.append(str(item.get("text", json.dumps(item, ensure_ascii=False))))
                else:
                    texts.append(str(item))
            raw = "\n".join(texts) or json.dumps(result, ensure_ascii=False)[: self.max_response]
            return {
                "ok": not is_error,
                "result": raw[: self.max_response],
                "is_error": is_error,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return {
                "ok": False,
                "result": f"MCP 调用失败: {str(e)[:200]}",
                "is_error": True,
                "latency_ms": latency,
            }
