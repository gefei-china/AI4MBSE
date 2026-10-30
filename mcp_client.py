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


def _parse_jsonrpc_body(resp) -> dict:
    """解析 JSON-RPC 响应体，**同时吃两种封装**（★ B5-4 真流量抓到）。

    MCP 的 Streamable HTTP 传输（协议 2025-03-26 起）允许服务端用两种方式回包：
      · `application/json`      → 裸 JSON，本文件原先只认这一种；
      · `text/event-stream`    → SSE 帧：`event: message\\ndata: {…}\\n\\n`。

    原实现无条件 `resp.json()`，于是**平台自己的内置网关恒定 offline** ——
    实测 `POST /mcp` 返回 `content_type=text/event-stream`、握手数据完全正确，
    客户端却抛 `Expecting value: line 1 column 1`（把 `event: message` 当JSON 解析）。
    这正是"服务端好好的、客户端自己把自己判死"的典型。

    ⚠️ 判据纪律：这是**响应封装格式**的兼容，不是"两种协议"——
    解析成功后仍要校验拿到的是 JSON-RPC 响应（`jsonrpc`/`result`/`error`），
    否则一个碰巧返回 `data:` 开头的 HTML 错误页会被当成"成功但空结果"。
    """
    raw = resp.text or ""
    ctype = (resp.headers.get("content-type") or "").lower()
    # 快路径：确实是 JSON 就直接解
    if "application/json" in ctype and not raw.lstrip().startswith(("event:", "data:")):
        return resp.json()
    text = raw.strip()
    if text.startswith("{") or text.startswith("["):
        return resp.json()
    # SSE 封装：取第一条 `data:` 的载荷（JSON-RPC 单响应场景）
    if "text/event-stream" in ctype or text.startswith(("event:", "data:")):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and ("result" in obj or "error" in obj
                                                   or "jsonrpc" in obj):
                        return obj
        # 只有 event:/注释，没有可解析的 data → 明确报错，别返回空 dict 假装成功
        raise ValueError("SSE 响应中未找到可解析的 JSON-RPC data 帧")
    # 都不是：给出可诊断的信息（含前若干字符，避免把 HTML 错误页当成 JSON 报错）
    raise ValueError(f"MCP 响应无法解析（content-type={ctype or 'unknown'}，body 前 120 字：{raw[:120]!r}）")


def is_loopback_endpoint(endpoint: str) -> bool:
    """endpoint 是否指向本机回环（127.0.0.1 / localhost / ::1 / 0.0.0.0）。

    ★ 2026-10-16 实证：机器/进程环境里带 `HTTP_PROXY`（企业代理、或本机
    agent 沙箱代理）时，httpx 默认 `trust_env=True` 会把**回环请求也塞给代理**，
    而代理对 `/mcp` 这类路径往往另行路由或拒绝 ⇒ 平台自己的内置 MCP
    （http://127.0.0.1:8000/mcp）被误判 offline（WinError 10061 / 502），
    表现为"领域能力工具全消失"——与服务端本身好不好毫无关系。

    回环流量永远不该过代理：本机服务走公网出口既绕路又大概率被策略挡。
    探测与真实调用同一判定（`_post` 与 studio 的 test 端点共用）。
    """
    try:
        from urllib.parse import urlparse
        u = endpoint if "://" in endpoint else "http://" + endpoint
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


class MCPClient:
    """轻量 MCP 客户端：initialize 握手 + 三类原语（Tools/Resources/Prompts）。

    - tools/list（探测） + tools/call（执行）
    - resources/list + resources/read（资源注入上下文）
    - prompts/list + prompts/get（预定义提示模板）

    超时与截断上限默认取系统配置（core.config 的 mcp 分组），可实例化时覆盖。
    握手结果（capabilities/serverInfo/protocolVersion）缓存于实例，供动态发现落库。
    """

    def __init__(self, endpoint: str, transport: str = "sse",
                 timeout: int | None = None, max_response: int | None = None,
                 headers: dict | None = None):
        from core import config
        self.endpoint = endpoint
        # ★ B5-4（2026-10-14 真流量抓到）：传输方式**三方枚举不一致**——
        #   前端下拉给的是 `streamable_http`，而 `_post` 只认 `("sse","http")`
        #   ⇒ 用户在界面上选"推荐"项，真实调用直接被 `raise ValueError` 挡掉；
        #   反过来库里存 `http` 的行，下拉里**没有对应 option**，
        #   `select.value='http'` 会被浏览器置空 ⇒ 一次编辑保存就把 transport 写成空串。
        #   （库里 4 台 MCP 有 3 台是 `http`，此前全靠"下拉没匹配项→空值"歪打正着。）
        #   处置：**入口归一**，把各历史叫法都折叠到 `http`，让 `_post` 的白名单是唯一事实源。
        self.transport = {"streamable_http": "http", "streamablehttp": "http",
                          "https": "http", "": "http"}.get((transport or "").strip().lower(),
                                                           (transport or "sse").strip().lower())
        self.timeout = timeout if timeout is not None else int(config.get("mcp", "timeout", 15))
        self.max_response = max_response if max_response is not None else int(config.get("mcp", "max_response", 4000))
        # ★ B5-2（2026-10-14）：认证头。此前 `_post` 的 headers 是**硬编码**的
        #   ⇒ 需要 Bearer 的 MCP **探测能过、真实调用必 401**（"测得通、用不了"）。
        #   现在从构造参数传入（由 mcp_servers.headers 提供）。
        self.headers = {str(k): str(v) for k, v in (headers or {}).items() if k and v}
        self._id = 0
        self._handshake = None          # initialize 结果缓存（未握手为 None）
        self.capabilities = {}          # 服务端能力声明
        self.server_info = {}           # serverInfo
        self.protocol_version = ""

    def _req_headers(self) -> dict:
        """请求头 = 配置的认证头 + Content-Type + Accept。

        ⚠️ `Accept` 同时含两种类型是 MCP Streamable HTTP 的要求
        （只给 application/json 的服务端可能 406）。
        """
        h = dict(self.headers)
        h.setdefault("Content-Type", "application/json")
        h.setdefault("Accept", "application/json, text/event-stream")
        return h

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
        resp = httpx.post(url, json=payload, headers=self._req_headers(),
                          timeout=timeout or self.timeout,
                          # ★ 2026-10-16：回环端点不过代理（否则企业代理/沙箱代理
                          # 会把内置 MCP 误判 offline，见 is_loopback_endpoint 注释）
                          trust_env=not is_loopback_endpoint(url))
        resp.raise_for_status()
        return _parse_jsonrpc_body(resp)

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
            # ★ 2026-10-27（会话 9629 实测，产物库为空的真因之一）：
            #   此前这里是**无标记硬截** `raw[:self.max_response]`。max_response 默认仅 4000，
            #   而结构化信封（如智源 export READ 的 {fileName,content,eof,sizeBytes,...}）
            #   一段就 32KB ⇒ 被切成**非法 JSON**（实测 json.loads 报
            #   "Unterminated string starting at char 242"）。
            #   后果连环：
            #     ① 下游 extract_sysml_doc_from_result 解析失败 ⇒ 交付物不入产物库；
            #     ② `truncated` 标志为 False（比的是 6000 的展示上限）⇒ 没人看得见被截了；
            #     ③ 模型只看到 4000 字符却宣称"全文 80,543 字节已取回并归档"⇒ 假报告。
            #   修法：**截断必须可观测**——把实际长度与是否截断一并回传，
            #   调用方（归档/展示/告警）据此决策，而不是各猜各的。
            _full = len(raw)
            _cut = _full > self.max_response
            return {
                "ok": not is_error,
                "result": raw[: self.max_response],
                "is_error": is_error,
                "latency_ms": latency,
                "truncated": _cut,
                "full_len": _full,
            }
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return {
                "ok": False,
                "result": f"MCP 调用失败: {str(e)[:200]}",
                "is_error": True,
                "latency_ms": latency,
            }
