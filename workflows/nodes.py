"""FlowNodesMixin：各类型节点执行器（tool/agent/skill/mcp/if/code/http/webhook/iteration/knowledge/pubsub/orchestrator/debate/react/planner/reflection）。"""
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from .tools import ToolExecutor


class FlowNodesMixin:
    """FlowExecutor 节点执行器拆分（解耦拆分：原 workflows.py 单体类方法）。"""

    def _exec_tool(self, label: str, cfg: dict, results: dict, payload: dict, conn, blackboard: dict | None = None) -> dict:
        """真实工具执行：arguments 支持模板引用上游输出/黑板。"""
        tool_name = cfg.get("tool", label)
        arguments = cfg.get("arguments") or {}
        if isinstance(arguments, dict):
            arguments = {k: self._render(str(v), results, payload, blackboard) for k, v in arguments.items()}
        branch = cfg.get("branch", "dev")
        # M5：HIL 强制——节点声明 hil_level=L2 且工具为写操作 → 进入人工确认队列（不直接执行）
        if cfg.get("hil_level") == "L2":
            from hil_service import HILService
            if HILService.is_write_tool(tool_name):
                conf_id = (getattr(self, "_hil_queued", {}) or {}).get(label)
                if not conf_id and conn is not None:
                    conf = HILService.queue_confirmation(
                        conn, label, tool_name, arguments,
                        preview=f"Agent 请求执行写操作 {tool_name}，参数：{json.dumps(arguments, ensure_ascii=False)[:300]}",
                        run_id=int(getattr(self, "run_id", 0) or 0))
                    conf_id = conf["id"] if conf else None
                if conf_id:
                    return {
                        "type": "tool", "node": label, "tool": tool_name, "status": "pending_hil",
                        "content": f"写操作「{tool_name}」已进入人工确认队列（确认单 #{conf_id}），确认后生效。",
                        "hil_confirmation_id": conf_id,
                        "data": {"tool": tool_name, "args": arguments, "hil_pending": True,
                                 "confirmation_id": conf_id},
                    }
        executor = ToolExecutor(conn)
        r = executor.execute(tool_name, arguments, branch)
        return {
            "type": "tool", "node": label, "tool": tool_name, "status": r.get("ok") and "done" or "error",
            "content": r.get("result", ""), "ok": r.get("ok"),
            "data": {"tool": tool_name, "args": arguments, "result": r.get("result", "")[:1000], "ok": bool(r.get("ok"))},
        }

    def _exec_agent(self, label: str, cfg: dict, results: dict, payload: dict, conn=None, blackboard: dict | None = None) -> dict:
        """子 Agent 节点：调用 AgentPipeline 执行指定 Agent（forced_intent）。

        P1 记忆：subscribe 注入黑板产物 → 检索长期记忆（agent_memory）注入 → 执行 → 沉淀记忆。
        """
        from agent import AgentPipeline
        intent = cfg.get("agent", "") or None
        query = self._render(cfg.get("query", ""), results, payload, blackboard)
        if not query:
            query = cfg.get("query", "") or "执行该 Agent 的默认任务"
        # P1：订阅黑板产物
        subs = cfg.get("subscribe") or []
        if isinstance(subs, str):
            subs = [x.strip() for x in subs.split(",") if x.strip()]
        if subs:
            parts = []
            for k in subs:
                v = (blackboard or {}).get(k)
                if v is not None:
                    parts.append(f"[黑板 {k}] {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}")
            if parts:
                query = "订阅的共享上下文：\n" + "\n".join(parts) + "\n\n任务：\n" + query
        provider_id = cfg.get("provider_id")
        pipe = AgentPipeline()
        pipe._load_db_agents()
        # M1：长期记忆检索（语义相关度 + 时间衰减，替代旧「最近5条」）
        mem_hint = ""
        mem_hits = 0
        if conn is not None:
            try:
                from memory_service import MemoryService
                rows = MemoryService.search(conn, intent or label, query, top_k=5)
                mem_hits = len(rows)
                if rows:
                    mem_hint = "（你的历史经验）\n" + "\n".join(
                        f"- [{r.get('mem_type', 'fact')}] {r['content'][:200]}" for r in rows)
            except Exception:
                pass
        if mem_hint:
            query = mem_hint + "\n\n任务：\n" + query
        out = pipe.execute(query, 0, "dev", provider_id, [], dry_run=True, forced_intent=intent)
        llm = out.get("llm", {})
        retr = out.get("retrieval", {})
        # M1：记忆评估沉淀（默认开启；LLM 提炼 or 规则阈值，content 截断防膨胀）
        mem_deposited = 0
        if conn is not None and not cfg.get("memorize") == False:
            try:
                from memory_service import MemoryService
                content = (out.get("content") or "").strip()
                mem_id = MemoryService.maybe_deposit(conn, intent or label, content, query)
                mem_deposited = 1 if mem_id else 0
            except Exception:
                pass
        # M6：Skill 自动沉淀（产出含方法论信号词时 LLM 评估 → skills 草稿，人工审核发布）
        skill_deposited = ""
        if conn is not None and not cfg.get("memorize") == False:
            try:
                from skill_deposit import SkillDepositor
                sk = SkillDepositor.maybe_deposit(conn, intent or label, query, out.get("content") or "")
                if sk:
                    skill_deposited = sk["name"]
            except Exception:
                pass
        # P0-5：技能使用反馈采集（画布 agent 节点路径；execute 非流式不自动采集，此处补采）
        try:
            if getattr(pipe, "_last_skill_hits", None):
                pipe._record_skill_feedback(run_id=int(getattr(self, "run_id", 0) or 0),
                                            intent=intent or out.get("intent", ""),
                                            output_content=out.get("content") or "")
        except Exception:
            pass
        return {
            "type": "agent", "node": label,
            "agent": intent or out.get("intent", ""),
            "status": "done",
            "content": out.get("content", ""),
            "llm": llm, "retrieval": retr,
            "data": {
                "intent": intent or out.get("intent", ""),
                "provider": llm.get("provider", "-"), "used_mock": llm.get("used_mock", True),
                "retrieval": {k: retr.get(k) for k in ("source", "graph_count", "vector_count")},
                "memory_hits": mem_hits, "memory_deposited": mem_deposited,
                "skill_deposited": skill_deposited,
            },
        }

    def _exec_skill(self, label: str, cfg: dict, results: dict, payload: dict, conn) -> dict:
        """Skill 节点：注入 published Skill 的指令正文（真实内容）。

        并行路径 worker 线程 conn=None 时自建连接查询（只读，用完关闭），
        保证并行执行也能拿到技能内容（曾出现「未找到该 Skill 内容」缺陷）。
        """
        skill_name = cfg.get("skill", "")
        content = ""
        try:
            from database import get_db
            own = conn is None
            c = conn or get_db()
            try:
                row = c.execute(
                    "SELECT content, description, version FROM skills WHERE name=? AND status!='draft'",
                    (skill_name,)).fetchone()
                if row:
                    content = row["content"] or ""
            finally:
                if own:
                    c.close()
        except Exception:
            pass
        return {
            "type": "skill", "node": label, "skill": skill_name, "status": "done",
            "content": content, "note": "Skill 指令已注入，供下游 LLM/Agent 节点消费" if content else "未找到该 Skill 内容",
            "data": {"skill": skill_name, "content_length": len(content)},
        }

    def _exec_mcp(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """MCP 节点：调用端点工具（JSON-RPC）。"""
        endpoint = cfg.get("endpoint", "")
        tool_name = cfg.get("tool", "")
        arguments = cfg.get("arguments") or {}
        if isinstance(arguments, dict):
            arguments = {k: self._render(str(v), results, payload, blackboard) for k, v in arguments.items()}
        if not endpoint or not tool_name:
            return {"type": "mcp", "node": label, "status": "error", "content": "缺少 endpoint/tool 配置", "data": {}}
        from mcp_client import MCPClient
        client = MCPClient(endpoint, cfg.get("transport", "sse"))
        r = client.call_tool(tool_name, arguments)
        return {
            "type": "mcp", "node": label, "tool": tool_name, "status": r.get("ok") and "done" or "error",
            "content": json.dumps(r, ensure_ascii=False)[:2000],
            "data": {"tool": tool_name, "endpoint": endpoint, "result": json.dumps(r, ensure_ascii=False)[:1000]},
        }

    def _exec_if(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """条件分支节点：expression 渲染求值 → data.value=true/false，驱动边 when 路由。"""
        expr = cfg.get("expression", "")
        rendered = self._render(expr, results, payload, blackboard)
        val = self._eval_cond(rendered)
        return {
            "type": "if", "node": label, "status": "done",
            "content": "true" if val else "false",
            "expression": expr,
            "data": {"value": val, "expression": rendered or expr},
        }

    # ── D3/D5：代码节点（D5 升级：沙箱执行，进程隔离+超时+受限环境，支持 python/js）──
    def _exec_code(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """代码节点：在安全沙箱中执行内联 Python/JS 代码（D5：进程隔离+超时+受限环境）。

        cfg:
        - language: 语言（python | js，默认 python）
        - code: 源码；代码内通过 _in 读取输入、_out 字典写入输出
        - inputs: {键: 模板值}，先经 {{节点.字段}}/{{blackboard.键}} 渲染后注入 _in
        - timeout: 超时秒数（默认 10，防死循环）
        输出统一 {content, data.outputs}，供下游节点模板引用。
        """
        from sandbox import run_code
        language = str(cfg.get("language") or "python").strip().lower()
        code = cfg.get("code") or ""
        if not code.strip():
            return {"type": "code", "node": label, "status": "error", "content": "代码为空", "data": {}}
        inputs = cfg.get("inputs") or {}
        if isinstance(inputs, str):
            try:
                inputs = json.loads(inputs)
            except Exception:
                inputs = {}
        in_vals = {}
        for k, v in (inputs or {}).items():
            if isinstance(v, str):
                v = self._render(v, results, payload, blackboard)
            in_vals[k] = v
        timeout = float(cfg.get("timeout") or 10)
        r = run_code(code, language=language, inputs=in_vals, timeout=timeout)
        if not r.get("ok"):
            return {"type": "code", "node": label, "status": "error",
                    "content": f"代码执行失败: {r.get('error')}",
                    "data": {"error": str(r.get("error"))[:300], "inputs": in_vals}}
        return {"type": "code", "node": label, "status": "done", "content": r.get("content"),
                "data": {"outputs": r.get("outputs") or {}, "inputs": in_vals}}

    # ── D3：HTTP 节点 ──
    def _exec_http(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """HTTP 节点：调用外部 HTTP 接口（与 http_tool_executor 响应约定一致）。

        cfg:
        - method: GET/POST/PUT/PATCH/DELETE（默认 GET）
        - url: 完整地址（支持 {{节点.字段}} / {{blackboard.键}} 模板）
        - headers/body/query: JSON 对象，字符串值支持模板渲染
        - timeout: 超时秒数（默认 15）
        - data_path: 响应 data 提取路径（默认 data，缺失回退整体）
        """
        import httpx
        from http_tool_executor import _parse_response
        method = str(cfg.get("method") or "GET").upper()
        url = self._render(str(cfg.get("url") or ""), results, payload, blackboard)
        if not url.strip():
            return {"type": "http", "node": label, "status": "error", "content": "缺少 url 配置", "data": {}}
        headers = {}
        for k, v in (cfg.get("headers") or {}).items():
            headers[k] = self._render(str(v), results, payload, blackboard) if isinstance(v, str) else v
        body = cfg.get("body")
        if isinstance(body, str):
            body = self._render(body, results, payload, blackboard)
        elif isinstance(body, dict):
            body = {k: (self._render(str(v), results, payload, blackboard) if isinstance(v, str) else v)
                    for k, v in body.items()}
        params = cfg.get("query")
        if isinstance(params, dict):
            params = {k: (self._render(str(v), results, payload, blackboard) if isinstance(v, str) else v)
                      for k, v in params.items()}
        timeout = float(cfg.get("timeout") or 15)
        _kwargs = {"headers": headers or None, "timeout": timeout}
        if body is not None:
            _kwargs["json"] = body
        if params:
            _kwargs["params"] = params
        try:
            resp = getattr(httpx, method.lower())(url, **_kwargs)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return {"type": "http", "node": label, "status": "error",
                    "content": f"HTTP {e.response.status_code}: {url}", "data": {"status": e.response.status_code}}
        except Exception as e:
            return {"type": "http", "node": label, "status": "error",
                    "content": f"调用失败: {str(e)[:200]}", "data": {"error": str(e)[:200]}}
        parsed = _parse_response(resp.text, str(cfg.get("data_path") or "data"), 2000)
        return {
            "type": "http", "node": label, "status": parsed.get("ok") and "done" or "error",
            "content": parsed.get("result", ""),
            "data": {"method": method, "url": url, "result": parsed.get("result", "")[:1000]},
        }

    # ── D11 A2A 协议互通：webhook 出站回调节点（8.2 API 网关层「事件回调」）──
    def _exec_webhook(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """Webhook 节点：向外部系统发送标准化 A2A 回调消息。

        cfg:
        - url: 目标端点（支持 {{节点.字段}} / {{blackboard.键}} 模板）
        - method: GET/POST/PUT（默认 POST）
        - payload: 发送内容（文本或 JSON，支持模板；默认自动包装为 A2A message）
        - secret: 可选 HMAC-SHA256 签名密钥（消息体签名，放 X-A2A-Signature 头）
        - headers: 附加请求头（字符串值支持模板）
        - timeout: 超时秒数（默认 10）
        发送体为标准化 A2A 消息：{protocol,version,kind,message:{id,kind,contents,context},sender:{name,type,role}}。
        """
        import httpx
        import hashlib
        import hmac as hmac_mod
        import uuid as uuid_mod
        url = self._render(str(cfg.get("url") or ""), results, payload, blackboard).strip()
        if not url:
            return {"type": "webhook", "node": label, "status": "error", "content": "缺少 url 配置", "data": {}}
        method = str(cfg.get("method") or "POST").upper()
        raw = self._render(str(cfg.get("payload") or ""), results, payload, blackboard)
        parsed = raw
        try:
            if raw.strip().startswith(("[", "{")):
                parsed = json.loads(raw)
        except Exception:
            parsed = raw
        body = {
            "protocol": "a2a", "version": "0.1", "kind": "message",
            "message": {
                "id": "msg-" + uuid_mod.uuid4().hex[:12],
                "kind": "json" if not isinstance(parsed, str) else "text",
                "contents": [parsed] if not isinstance(parsed, str) else [{"text": parsed}],
                "context": {"run_id": getattr(self, "run_id", 0) or 0, "node_id": label},
            },
            "sender": {"name": label, "type": "flow", "role": "workflow-node"},
        }
        headers = {"Content-Type": "application/json"}
        for k, v in (cfg.get("headers") or {}).items():
            headers[k] = self._render(str(v), results, payload, blackboard) if isinstance(v, str) else str(v)
        secret = str(cfg.get("secret") or "").strip()
        data_str = json.dumps(body, ensure_ascii=False)
        if secret:
            headers["X-A2A-Signature"] = hmac_mod.new(secret.encode(), data_str.encode(), hashlib.sha256).hexdigest()
        timeout = float(cfg.get("timeout") or 10)
        try:
            if method == "GET":
                resp = httpx.get(url, params=body, headers=headers, timeout=timeout)
            else:
                resp = httpx.request(method, url, content=data_str, headers=headers, timeout=timeout)
            ok = 200 <= resp.status_code < 300
            return {"type": "webhook", "node": label, "status": "done" if ok else "error",
                    "content": f"回调 {resp.status_code}: {resp.text[:200]}",
                    "data": {"method": method, "url": url, "status_code": resp.status_code,
                             "response": resp.text[:1000]}}
        except Exception as e:
            return {"type": "webhook", "node": label, "status": "error",
                    "content": f"回调失败: {str(e)[:200]}",
                    "data": {"method": method, "url": url, "error": str(e)[:200]}}

    # ── D11 A2A 事件回调：节点完成/出错/运行完成时向订阅的 webhook_url 发射标准化事件 ──
    def _emit_event(self, conn, run_id: int, node_id: str, node_label: str,
                    node_type: str, event_type: str, payload: dict) -> None:
        """查询 flow_event_subscriptions 匹配订阅并 POST 事件（失败不阻断主流程）。

        匹配规则：status='active' + event_type 精确匹配 + (run_id=0 全局 或 =run_id)
        + (node_id='' 全部节点 或 =node_id)。成功更新 last_status_code，失败标记 status=failed。
        """
        if not run_id:
            return
        import hashlib
        import hmac as hmac_mod
        import uuid as uuid_mod
        import httpx
        try:
            subs = []
            if conn is not None:
                rows = conn.execute(
                    "SELECT * FROM flow_event_subscriptions WHERE status='active' AND event_type=? "
                    "AND (run_id=0 OR run_id=?) AND (node_id='' OR node_id=?)",
                    (event_type, run_id, node_id or "")).fetchall()
                subs = [dict(r) for r in rows]
            if not subs:
                return
            body = {
                "protocol": "a2a", "version": "0.1", "kind": "event",
                "event": {"id": "evt-" + uuid_mod.uuid4().hex[:12], "type": event_type,
                          "run_id": run_id, "node_id": node_id, "node_label": node_label,
                          "node_type": node_type, "payload": payload,
                          "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                "sender": {"name": "mbse-workflow", "type": "flow", "role": "orchestrator"},
            }
            data_str = json.dumps(body, ensure_ascii=False)
            for s in subs:
                try:
                    headers = {"Content-Type": "application/json"}
                    if s.get("secret"):
                        headers["X-A2A-Signature"] = hmac_mod.new(
                            s["secret"].encode(), data_str.encode(), hashlib.sha256).hexdigest()
                    r = httpx.post(s["webhook_url"], content=data_str, headers=headers, timeout=5)
                    if conn is not None:
                        conn.execute("UPDATE flow_event_subscriptions SET last_event_at=CURRENT_TIMESTAMP, "
                                     "last_status_code=? WHERE id=?", (r.status_code, s["id"]))
                except Exception as e:
                    if conn is not None:
                        conn.execute("UPDATE flow_event_subscriptions SET status='failed', last_error=? "
                                     "WHERE id=?", (str(e)[:200], s["id"]))
                if conn is not None:
                    conn.commit()  # 订阅状态即时落库（跨请求可见）
        except Exception:
            pass  # 事件回调失败绝不阻断运行主流程

    # ── D3：迭代节点（foreach 语义）──
    def _exec_iteration(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """迭代节点：对 items 逐项执行 subflow 子流程（复用 _exec_node 分发）。

        cfg:
        - items: 列表（JSON 数组 / {{节点.字段}} 引用数组 / 逗号分隔字符串）
        - subflow: 子流程节点 id 列表（逗号分隔；节点须在本流程 nodes 中定义）
        每轮迭代将当前项写入黑板 __item / __index，子节点可用模板 {{blackboard.__item}} 引用。
        """
        items = cfg.get("items") or []
        if isinstance(items, str):
            rendered = self._render(items, results, payload, blackboard)
            try:
                items = json.loads(rendered)
            except Exception:
                try:
                    import ast
                    items = ast.literal_eval(rendered)
                except Exception:
                    items = [x.strip() for x in rendered.split(",") if x.strip()]
        if isinstance(items, dict):
            items = list(items.items())
        subflow = cfg.get("subflow") or []
        if isinstance(subflow, str):
            subflow = [x.strip() for x in subflow.split(",") if x.strip()]
        if not isinstance(items, list):
            return {"type": "iteration", "node": label, "status": "error",
                    "content": "items 需为数组", "data": {}}
        if not items:
            return {"type": "iteration", "node": label, "status": "done", "content": "无迭代项",
                    "data": {"count": 0}}
        if not subflow:
            return {"type": "iteration", "node": label, "status": "error",
                    "content": "未配置 subflow", "data": {}}
        nodes = getattr(self, "_nodes", []) or []
        rounds = []
        for idx, item in enumerate(items):
            iter_bb = {**(blackboard or {}), "__item": item, "__index": idx}
            round_out = {}
            for nid in subflow:
                sub_node = next((n for n in nodes if n.get("id") == nid), None)
                if sub_node is None:
                    round_out[nid] = {"status": "error", "content": f"子流程节点不存在: {nid}"}
                    continue
                try:
                    sub_out = self._exec_node(sub_node, {**results}, payload, None, iter_bb)
                except Exception as e:
                    sub_out = {"status": "error", "content": str(e)[:200]}
                round_out[nid] = {"status": sub_out.get("status"),
                                  "content": str(sub_out.get("content", ""))[:300]}
            rounds.append({"index": idx, "item": str(item)[:200], "nodes": round_out})
        lines = [f"第{r['index'] + 1}轮（项 {r['item']}）: "
                 + "; ".join(f"{k}={v['status']}" for k, v in r["nodes"].items()) for r in rounds]
        content = "\n".join(lines)
        if len(content) > 2000:
            content = content[:2000] + "…(已截断)"
        return {"type": "iteration", "node": label, "status": "done", "content": content,
                "data": {"count": len(items), "rounds": rounds}}

    # ── D3：知识节点 ──
    def _exec_knowledge(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """知识节点：知识库混合检索（图谱实体 + 向量片段，降级兼容）。"""
        query = self._render(str(cfg.get("query") or ""), results, payload, blackboard)
        if not query.strip():
            return {"type": "knowledge", "node": label, "status": "error", "content": "缺少 query 配置", "data": {}}
        branch = cfg.get("branch") or "dev"
        try:
            top_k = max(1, min(int(cfg.get("top_k") or 5), 20))
        except Exception:
            top_k = 5
        from agent import GraphRAG
        r = GraphRAG().retrieve(query, branch)
        hits = (r.get("chunk_hits") or r.get("entities") or [])[:top_k]
        names = [h.get("name") or str(h.get("content", ""))[:60] for h in hits]
        graph_n, vector_n = r.get("graph_count", 0), r.get("vector_count", 0)
        content = f"图谱 {graph_n} 条 / 向量 {vector_n} 条，Top{len(names)}：{names}"
        return {
            "type": "knowledge", "node": label, "status": "done", "content": content,
            "data": {"query": query, "branch": branch, "top_k": top_k, "hits": names,
                     "graph_count": graph_n, "vector_count": vector_n},
        }

    # ── D7 多 Agent 发布-订阅：共享消息池（MetaGPT 模式——发布者写标准化消息，订阅者按 topic 认领）──
    def _msg_pool(self, run_id: int) -> dict:
        """并行路径（conn=None）兜底内存消息池：run_id → {topic: [消息]}，线程锁保护。

        注意：实例属性名用 _msg_pool_data，避免与方法名 _msg_pool 冲突（hasattr 会命中方法本身）。
        """
        import threading
        if not hasattr(self, "_msg_pool_lock"):
            self._msg_pool_lock = threading.Lock()
        if not hasattr(self, "_msg_pool_data"):
            self._msg_pool_data = {}
        with self._msg_pool_lock:
            return self._msg_pool_data.setdefault(run_id, {})

    def _msg_next_seq(self, conn, run_id: int) -> int:
        try:
            if conn is not None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 AS s FROM agent_messages WHERE run_id=?", (run_id,)).fetchone()
                return int(row["s"]) if row and row["s"] else 1
        except Exception:
            pass
        return 1

    def _exec_pubsub(self, label: str, cfg: dict, results: dict, payload: dict, conn,
                     blackboard: dict | None = None) -> dict:
        """发布-订阅通信节点（MetaGPT 共享消息池模式）。

        cfg:
        - op: publish | subscribe（操作类型，默认 publish）
        - topic: 主题（支持 {{}} 模板引用）
        - payload: publish 时发布的内容（文本或 JSON，支持模板引用）
        - write_key: subscribe 认领后写入黑板的 key（配合下游 {{blackboard.key}} / llm subscribe 使用）
        消息池双通道：conn 可用（串行）落库 agent_messages 并即时 commit；conn=None（并行 worker）
        用实例内存池（跨层顺序保证发布先于订阅可见）。订阅采用「认领」语义——每条消息只被首个订阅者消费。
        """
        run_id = getattr(self, "run_id", 0) or 0
        op = (str(cfg.get("op") or "publish").strip().lower())
        if op not in ("publish", "subscribe"):
            op = "publish"
        topic = self._render(str(cfg.get("topic") or ""), results, payload, blackboard).strip()
        if not topic:
            return {"type": "pubsub", "node": label, "status": "error", "content": "缺少 topic 配置", "data": {}}

        # ── 发布：写入共享消息池（topic + 标准化内容）──
        if op == "publish":
            raw = self._render(str(cfg.get("payload") or ""), results, payload, blackboard)
            parsed = raw
            try:
                if raw.strip().startswith(("[", "{")):
                    parsed = json.loads(raw)
            except Exception:
                parsed = raw
            if isinstance(parsed, str):
                content_json = json.dumps({"text": parsed}, ensure_ascii=False)
            else:
                content_json = json.dumps(parsed, ensure_ascii=False)
            seq = self._msg_next_seq(conn, run_id)
            if conn is not None:
                try:
                    conn.execute(
                        "INSERT INTO agent_messages (run_id, seq, topic, content_json, publisher_node, claimed) "
                        "VALUES (?,?,?,?,?,0)", (run_id, seq, topic, content_json, label))
                    conn.commit()  # 发布即提交：后续节点/订阅者立即可见
                except Exception as e:
                    return {"type": "pubsub", "node": label, "status": "error",
                            "content": f"发布失败: {str(e)[:120]}", "data": {}}
            else:
                pool = self._msg_pool(run_id)
                pool.setdefault(topic, []).append(
                    {"seq": seq, "content": parsed, "publisher": label, "claimed": False})
            return {"type": "pubsub", "node": label, "status": "done",
                    "content": f"已发布消息到主题「{topic}」（seq {seq}）",
                    "data": {"op": "publish", "topic": topic, "seq": seq, "payload": parsed}}

        # ── 订阅：认领主题下首个未认领消息（认领即消费，不重复处理）──
        msg = None
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT id, seq, content_json FROM agent_messages "
                    "WHERE run_id=? AND topic=? AND claimed=0 ORDER BY seq LIMIT 1",
                    (run_id, topic)).fetchone()
                if row:
                    conn.execute("UPDATE agent_messages SET claimed=1, subscriber_node=? WHERE id=?",
                                 (label, row["id"]))
                    conn.commit()
                    try:
                        content = json.loads(row["content_json"] or "{}")
                    except Exception:
                        content = {"text": row["content_json"]}
                    msg = {"seq": row["seq"], "content": content}
            except Exception as e:
                return {"type": "pubsub", "node": label, "status": "error",
                        "content": f"订阅失败: {str(e)[:120]}", "data": {}}
        else:
            import threading
            pool = self._msg_pool(run_id)
            lock = getattr(self, "_msg_pool_lock")  # 认领须原子：读+标记在同一把锁内，防并行订阅者重复消费
            with lock:
                for m in pool.get(topic, []):
                    if not m["claimed"]:
                        m["claimed"] = True
                        m["subscriber"] = label
                        msg = {"seq": m["seq"], "content": m["content"]}
                        break
        if msg is None:
            return {"type": "pubsub", "node": label, "status": "done",
                    "content": f"主题「{topic}」无待认领消息",
                    "data": {"op": "subscribe", "topic": topic, "claimed": False}}
        content = msg["content"]
        text = content.get("text") if isinstance(content, dict) else str(content)
        # 认领内容注入黑板（key 取 write_key，默认 topic）——下游 llm/agent 节点可订阅或模板引用
        wkey = (str(cfg.get("write_key") or topic)).strip()
        if blackboard is not None and wkey:
            blackboard[wkey] = content
        return {"type": "pubsub", "node": label, "status": "done",
                "content": str(text) if text is not None else str(content)[:2000],
                "data": {"op": "subscribe", "topic": topic, "seq": msg["seq"], "claimed": True,
                         "write_key": wkey, "message": content}}

    # ── P2 多 Agent 协同：orchestrator（Manager）节点 ──
    def _exec_orchestrator(self, label: str, cfg: dict, results: dict, payload: dict, conn, blackboard: dict | None = None) -> dict:
        """Manager 节点：调度多个 Worker Agent（strategy: contract_net | parallel | sequential）。

        contract_net（合同网简化）：Manager 发布任务招标 → 各 Worker 出方案 → 启发式评分授标 → 汇总。
        """
        from agent import AgentPipeline
        workers = cfg.get("workers") or []
        if isinstance(workers, str):
            workers = [x.strip() for x in workers.split(",") if x.strip()]
        task = self._render(cfg.get("task", cfg.get("query", "")), results, payload, blackboard)
        if not task:
            task = "执行该团队协作任务"
        strategy = cfg.get("strategy", "contract_net")

        # ── D9 分层式多级管理：sub_flow 子工作流递归（Manager 委派给子流程，父/子 run 关联成 Supervisor 树）──
        sub_flow_id = cfg.get("sub_flow")
        if sub_flow_id:
            depth = getattr(self, "_sub_depth", 0)
            if depth >= 5:
                return {"type": "orchestrator", "node": label, "status": "error",
                        "content": f"子流程嵌套超过 5 层，终止（当前深度 {depth}）", "data": {}}
            try:
                from repositories.studio_repo import StudioRepo
                _sf = StudioRepo(conn).get_agent_flow(int(sub_flow_id)) if conn else None
                if not _sf:
                    return {"type": "orchestrator", "node": label, "status": "error",
                            "content": f"子流程 #{sub_flow_id} 不存在", "data": {}}
                sub_nodes = json.loads(_sf["nodes"] or "[]")
                sub_edges = json.loads(_sf["edges"] or "[]")
                sub_payload: dict = {}
                raw_in = cfg.get("sub_flow_inputs") or {}
                if isinstance(raw_in, str):
                    try:
                        raw_in = json.loads(raw_in)
                    except Exception:
                        raw_in = {}
                if isinstance(raw_in, dict):
                    for k, v in raw_in.items():
                        sub_payload[k] = self._render(str(v), results, payload, blackboard) \
                            if isinstance(v, str) else v
                sub_exec = type(self)(conn)  # 拆包后避免循环导入：type(self)==FlowExecutor
                sub_exec._sub_depth = depth + 1  # 递归深度链：新实例继承 +1
                sub_r = sub_exec.run(sub_nodes, sub_edges, sub_payload, conn, persist=True,
                                     flow_id=int(sub_flow_id),
                                     flow_name=(_sf.get("name") or "子流程"),
                                     parent_run_id=self.run_id or 0)
                _order = sub_r.get("order") or []
                _summary = (f"执行顺序 {' → '.join(str(x) for x in _order) or '空'} · "
                            f"{len(_order)} 节点 · 状态 {sub_r.get('status')}"
                            f" · 耗时 {sub_r.get('total_latency_ms') or 0}ms")
                return {"type": "orchestrator", "node": label, "status": "done",
                        "content": f"子流程「{_sf.get('name') or '#'+str(sub_flow_id)}」执行完成"
                                   f"（run {sub_r.get('run_id')}，状态 {sub_r.get('status')}）\n"
                                   f"子流程摘要：{_summary}",
                        "data": {"strategy": "sub_flow", "sub_flow_id": int(sub_flow_id),
                                 "sub_run_id": sub_r.get("run_id"),
                                 "sub_status": sub_r.get("status")}}
            except Exception as e:
                return {"type": "orchestrator", "node": label, "status": "error",
                        "content": f"子流程执行失败: {str(e)[:120]}", "data": {}}

        pipe = AgentPipeline()
        pipe._load_db_agents()

        # P0：委派协议透传（context/expected_output/tools 白名单 → 各 Worker 最小权限）
        wl = cfg.get("tools") or None
        if isinstance(wl, str):
            wl = [x.strip() for x in wl.split(",") if x.strip()]
        _ctx = str(cfg.get("context") or "").strip()
        _exp = str(cfg.get("expected_output") or "").strip()
        worker_task = task
        if _ctx:
            worker_task += f"\n\n任务上下文（只读，勿编造上下文外事实）：\n{_ctx[:1500]}"
        if _exp:
            worker_task += f"\n\n期望输出/完成标准：\n{_exp[:800]}"

        def _run_worker(w):
            try:
                out = pipe.execute(worker_task, 0, "dev", None, [], dry_run=True,
                                   forced_intent=w, tools_whitelist=wl)
                return {"worker": w, "content": (out.get("content") or ""), "ok": True}
            except Exception as e:
                return {"worker": w, "content": f"执行失败: {e}", "ok": False}

        # M3：Worker 并行执行（ThreadPoolExecutor + 每 worker 独立连接，避免 SQLite 连接竞争）
        # P0：worker 超时护栏——超时标记失败并继续其余 worker（部分成功）
        bids = []
        if workers:
            if strategy in ("parallel", "contract_net") and len(workers) > 1:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTTimeout
                from core import config as _cfg
                _wt = int(_cfg.get("delegation", "worker_timeout_s", 120))
                try:
                    with ThreadPoolExecutor(max_workers=min(len(workers), 4)) as pool:
                        _futs = [pool.submit(_run_worker, w) for w in workers]
                        bids = []
                        for _f in _futs:
                            try:
                                bids.append(_f.result(timeout=_wt))
                            except _FTTimeout:
                                bids.append({"worker": "?", "content": f"Worker 执行超时(>{_wt}s)", "ok": False})
                            except Exception as _e:
                                bids.append({"worker": "?", "content": f"Worker 异常: {_e}", "ok": False})
                except Exception:
                    bids = [_run_worker(w) for w in workers]
            else:
                bids = [_run_worker(w) for w in workers]
        if strategy == "contract_net" and bids:
            for b in bids:
                b["score"] = self._score_bid(b["content"], b["worker"])
            best = max(bids, key=lambda b: b.get("score", 0))
            summary = (f"Manager 发布任务招标，{len(bids)} 个 Worker 投标，"
                       f"授标给「{best['worker']}」（评分 {best['score']}）\n\n"
                       f"中标方案（{best['worker']}）：\n{best['content'][:800]}\n\n"
                       f"—— 其他投标 ——\n" +
                       "\n".join(f"[{b['worker']} 评分{b.get('score',0)}] {b['content'][:200]}" for b in bids if b is not best))
            return {
                "type": "orchestrator", "node": label, "status": "done", "content": summary,
                "data": {"strategy": strategy, "workers": workers,
                         "bids": [{"worker": b["worker"], "score": b.get("score", 0)} for b in bids],
                         "winner": best["worker"]},
            }
        if strategy == "parallel" and bids:
            parts = [f"[{b['worker']}] {b['content']}" for b in bids]
            return {
                "type": "orchestrator", "node": label, "status": "done",
                "content": "\n\n".join(parts),
                "data": {"strategy": strategy, "workers": workers, "count": len(bids)},
            }
        # sequential / 兜底
        return {
            "type": "orchestrator", "node": label, "status": "done",
            "content": "\n\n".join(f"[{b['worker']}] {b['content']}" for b in bids) or "无 Worker 配置",
            "data": {"strategy": strategy, "workers": workers},
        }

    # ── D8 多 Agent 分布式协调：debate（投票/议价协商）节点 ──
    def _exec_debate(self, label: str, cfg: dict, results: dict, payload: dict,
                     blackboard: dict | None = None) -> dict:
        """分布式协商节点：平等 Agent 通过投票/议价达成共识（无 Manager 单点故障）。

        cfg:
        - mode: vote | price（默认 vote）
        - proposal: 议题（支持 {{}} 模板引用）
        - sources: 观点来源节点 id（逗号分隔；为空则取所有已 done 节点输出）
        - options: vote 模式候选选项（逗号分隔；为空按观点文本分组计票）
        - threshold: 共识阈值（默认 0.6，最高得票比例 >= 阈值判为共识）
        - rounds: 协商轮次（当前单轮聚合，字段预留后续多轮修正）

        观点解析：
        - vote：内容子串命中候选选项计票；未命中记「弃权」
        - price：内容提取首个数字作为出价；收敛价=最低价（中标），极差率(极差/中位)<=20% 视为收敛
        """
        import re as _re
        mode = (str(cfg.get("mode") or "vote")).strip().lower()
        if mode not in ("vote", "price"):
            mode = "vote"
        proposal = self._render(str(cfg.get("proposal") or "议题"), results, payload, blackboard).strip()
        try:
            threshold = float(cfg.get("threshold") or 0.6)
        except Exception:
            threshold = 0.6
        options = cfg.get("options") or ""
        if isinstance(options, str):
            options = [x.strip() for x in options.split(",") if x.strip()]
        srcs = cfg.get("sources") or ""
        if isinstance(srcs, str):
            srcs = [x.strip() for x in srcs.split(",") if x.strip()]
        if not srcs:
            srcs = [nid for nid, out in results.items() if out.get("status") == "done"]
        positions = []
        for s in srcs:
            out = results.get(s) or {}
            content = (out.get("content") or "").strip() or "(该节点无输出)"
            # 优先取结构化观点（code 节点 data.outputs.stance / llm/agent 的 text），避免 JSON 包装污染文本
            _outputs = out.get("data", {}).get("outputs") or {}
            if isinstance(_outputs, dict):
                for _k in ("stance", "position", "text", "answer"):
                    if _outputs.get(_k):
                        content = str(_outputs[_k])
                        break
            elif _outputs:
                content = str(_outputs)
            positions.append({"source": s, "stance": content[:400]})
        if not positions:
            return {"type": "debate", "node": label, "status": "done",
                    "content": f"协商议题「{proposal}」：无可用观点来源",
                    "data": {"mode": mode, "proposal": proposal, "consensus": False, "positions": []}}

        if mode == "vote":
            tally: dict = {}
            for p in positions:
                if options:
                    vote = None
                    for o in options:
                        if o and o in p["stance"]:
                            vote = o
                            break
                    p["vote"] = vote or "弃权"
                else:
                    # 无候选选项：按观点文本分组计票（同文本视为同一立场）
                    p["vote"] = p["stance"][:40] or "弃权"
                tally[p["vote"]] = tally.get(p["vote"], 0) + 1
            total = len(positions)
            top = max(tally.items(), key=lambda kv: kv[1]) if tally else ("", 0)
            agreement = round(top[1] / total, 3) if total else 0.0
            consensus = bool(top[0] != "弃权" and agreement >= threshold)
            decision = top[0] if consensus else ""
            dissents = [p for p in positions if p["vote"] != top[0]] if top[0] else positions
            content = (f"协商议题：{proposal}\n计票：{json.dumps(tally, ensure_ascii=False)}（{total} 票）\n"
                       f"最高得票「{top[0]}」比例 {agreement:.0%}，阈值 {threshold:.0%}\n"
                       f"结论：{'达成共识 → 决策 = ' + decision if consensus else '未达成共识'}"
                       + (("\n分歧来源：" + "、".join(f"{p['source']}（{p['vote']}）" for p in dissents))
                          if (dissents and not consensus) else ""))
            return {"type": "debate", "node": label, "status": "done", "content": content,
                    "data": {"mode": mode, "proposal": proposal, "tally": tally, "total": total,
                             "agreement": agreement, "threshold": threshold, "consensus": consensus,
                             "decision": decision, "positions": positions}}

        # price 议价模式
        for p in positions:
            m = _re.search(r"(\d+(?:\.\d+)?)", p["stance"])
            p["price"] = float(m.group(1)) if m else None
        bids = [p["price"] for p in positions if p["price"] is not None]
        if not bids:
            return {"type": "debate", "node": label, "status": "done",
                    "content": f"议价议题「{proposal}」：无有效出价",
                    "data": {"mode": mode, "proposal": proposal, "consensus": False, "positions": positions}}
        converged = min(bids)
        med = sorted(bids)[len(bids) // 2]
        spread = round((max(bids) - min(bids)) / med, 3) if med else 0.0
        consensus = spread <= 0.2
        content = (f"议价议题：{proposal}\n"
                   f"出价：{json.dumps([{'来源': p['source'], '出价': p['price']} for p in positions], ensure_ascii=False)}\n"
                   f"收敛价（最低中标）：{converged}，极差率 {spread:.0%}\n"
                   f"结论：{'议价收敛' if consensus else '出价分歧过大，未收敛'}")
        return {"type": "debate", "node": label, "status": "done", "content": content,
                "data": {"mode": mode, "proposal": proposal, "bids": bids, "converged_price": converged,
                         "spread": spread, "consensus": consensus, "positions": positions}}

    # ── M3：ReAct 节点 —— 思考(Thought)→行动(Action)→观察(Observation) 循环执行器 ──
    def _exec_react(self, label: str, cfg: dict, results: dict, payload: dict, conn,
                    blackboard: dict | None = None) -> dict:
        """ReAct 推理执行器：LLM 驱动多步工具求解。

        cfg:
        - goal: 任务目标（支持 {{节点.字段}} / {{blackboard.键}}）
        - max_steps: 最大循环步数（默认 5）
        - tools: 可用工具（逗号分隔，默认全部内置工具）
        - agent: 可选，思考由指定 Agent 体系驱动（forced_intent）
        - provider_id / branch

        Mock/无 key 环境：LLM 返回普通文本无法解析 JSON → 直接视为最终答案（单步，确定性保持）。
        """
        from llm import llm_client
        goal = self._render(cfg.get("goal", cfg.get("query", "")), results, payload, blackboard)
        if not goal:
            goal = "执行该任务并给出结论"
        max_steps = max(1, min(int(cfg.get("max_steps", 5) or 5), 10))
        tool_names = cfg.get("tools") or "graph_retrieve,conflict_check,impact_analyze,validate,entity_create"
        if isinstance(tool_names, str):
            tool_names = [x.strip() for x in tool_names.split(",") if x.strip()]
        tool_desc = "\n".join(f"- {t}" for t in tool_names)
        system = (
            "你是 ReAct 推理执行器。对目标执行「思考→行动→观察」循环，直到可以给出最终答案。\n"
            "行动类型：\n"
            f"1. tool：调用工具，action_input 形如 {{\"tool\":\"工具名\",\"arguments\":{{参数JSON}}}}\n"
            "   可用工具：\n" + tool_desc + "\n"
            "2. finish：给出最终答案，action_input 形如 {\"answer\":\"最终答案\"}\n"
            "每步只输出一个 JSON 对象：{\"thought\":\"推理\",\"action\":\"tool|finish\",\"action_input\":{...}}，不要其他文字。"
        )
        steps = []
        obs = ""
        executor = ToolExecutor(conn)
        t0 = time.time()
        final_answer = ""
        resp = {}
        for i in range(1, max_steps + 1):
            msgs = [{"role": "system", "content": system},
                    {"role": "user", "content": f"目标：{goal}\n\n已观察到的信息：\n{obs or '（无）'}"}]
            resp = llm_client.chat(msgs, provider_id=cfg.get("provider_id"), _intent="react_step")
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            raw = msg.get("content") or ""
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                # 非 JSON（Mock/降级）→ 当前内容即答案
                final_answer = raw.strip()
                steps.append({"step": i, "thought": "", "action": "finish", "answer": final_answer[:300]})
                break
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}
            thought = data.get("thought", "")
            action = data.get("action", "finish")
            ai = data.get("action_input", {}) if isinstance(data.get("action_input", {}), dict) else {}
            if action in ("finish", "final") or not action:
                final_answer = str(ai.get("answer") or thought or "").strip()
                steps.append({"step": i, "thought": thought, "action": "finish", "answer": final_answer[:300]})
                break
            if action == "tool":
                tname = ai.get("tool", "")
                args = ai.get("arguments") or {}
                if tname in tool_names:
                    try:
                        r = executor.execute(tname, args if isinstance(args, dict) else {}, cfg.get("branch", "dev"))
                        ob = str(r.get("result", ""))[:400]
                    except Exception as e:
                        ob = f"工具执行失败: {str(e)[:150]}"
                else:
                    ob = f"未知工具「{tname}」，请从可用工具中选择"
            else:
                ob = "无法识别的行动类型，请直接 finish"
            steps.append({"step": i, "thought": thought, "action": action, "tool": ai.get("tool", ""), "observation": ob})
            obs = (obs + f"\n[步骤{i}] {thought}\n观察：{ob}").strip()[-1500:]
        if not final_answer:
            final_answer = obs[-800:] if obs else goal + "（未能得出最终结论）"
        meta = resp.get("_meta", {})
        return {
            "type": "react", "node": label, "status": "done",
            "content": final_answer,
            "steps": steps, "step_count": len(steps),
            "provider": meta.get("provider", "-"), "model": meta.get("model", "-"),
            "used_mock": meta.get("used_mock", True),
            "data": {
                "goal": goal, "step_count": len(steps),
                "steps": steps[:10],
                "provider": meta.get("provider", "-"), "used_mock": meta.get("used_mock", True),
            },
            "latency_ms": int((time.time() - t0) * 1000),
        }

    # ── M3：Planner-Executor 节点 —— LLM 计划分解 → 任务队列 DAG 执行 → 汇总 ──
    def _exec_planner(self, label: str, cfg: dict, results: dict, payload: dict, conn,
                      blackboard: dict | None = None) -> dict:
        """Planner-Executor：团队任务「计划→分解→并行执行→汇总」。

        cfg:
        - goal: 团队目标（支持模板引用）
        - agents: 可用 Agent 池（逗号分隔；空则计划器自主分配）
        - max_tasks: 计划任务上限（默认 6）
        - provider_id
        - parallel: true 时就绪任务并行执行（默认 false 串行，更稳）

        执行轨迹落 agent_tasks（依赖拓扑 + 结构化 handoff），失败不中断整链（blocked 可人工介入）。
        Mock/无 key 环境：LLM 无法输出计划 JSON → 降级为单一 Agent 直跑（确定性保持）。
        """
        from llm import llm_client
        from task_queue import TaskQueue
        goal = self._render(cfg.get("goal", cfg.get("query", "")), results, payload, blackboard)
        if not goal:
            goal = "执行该团队任务并汇总结果"
        agents = cfg.get("agents") or []
        if isinstance(agents, str):
            agents = [x.strip() for x in agents.split(",") if x.strip()]
        pool_txt = "、".join(agents) if agents else "由你从可用 Agent 中自主选择"
        max_tasks = max(1, min(int(cfg.get("max_tasks", 6) or 6), 12))
        t0 = time.time()

        # 1) LLM 计划生成（结构化 JSON，失败降级）
        # P1b-2 委派协议结构化（对齐 multi-agent-optimization.md P0-1）：子任务携带 context/expected_output
        plan = []
        prompt = (
            "你是任务规划器。把目标分解为可并行/串行执行的子任务清单，只输出 JSON：\n"
            '{"tasks": [{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
            ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
            ' "context": "该子任务必需的关键事实/输入（从目标中提取，供子 Agent 直接使用，100字内）",'
            ' "expected_output": "该子任务的完成标准（可验证的交付物描述，50字内）"}]}\n'
            f"可用 Agent 池：{pool_txt}\n"
            f"要求：任务数 1~{max_tasks} 个；每个任务只交付一个明确成果；有依赖关系的用 deps 表达；"
            f"context 必须给足子 Agent 独立执行的必需事实（不自带父上下文）；不要输出其他文字。\n"
            f"目标：{goal}"
        )
        try:
            resp = llm_client.chat([{"role": "user", "content": prompt}], provider_id=cfg.get("provider_id"),
                                   _intent="planner")
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            raw = msg.get("content") or ""
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                data = json.loads(m.group(0))
                plan = [t for t in (data.get("tasks") or []) if isinstance(t, dict) and t.get("key")]
        except Exception:
            plan = []

        # 2) 降级：计划解析失败 → 单 Agent 直跑
        if not plan:
            from agent import AgentPipeline
            pipe = AgentPipeline()
            pipe._load_db_agents()
            direct = agents[0] if agents else "requirement_analysis"
            try:
                out = pipe.execute(goal, 0, "dev", cfg.get("provider_id"), [], dry_run=True, forced_intent=direct)
                content = (out.get("content") or "").strip()
            except Exception as e:
                content = f"执行失败: {str(e)[:150]}"
            return {
                "type": "planner", "node": label, "status": "done", "content": content,
                "degraded": True, "plan": [], "data": {"degraded": True, "direct_agent": direct,
                                                       "provider": (out.get("llm") or {}).get("provider", "-")},
                "latency_ms": int((time.time() - t0) * 1000),
            }

        # 3) 计划落库 + DAG 执行
        run_id = int(getattr(self, "run_id", 0) or 0)
        executed = []
        if conn is not None:
            try:
                TaskQueue.clear_run(conn, run_id)
                TaskQueue.create_plan(conn, run_id, plan, assigned_by="planner")
                parallel = str(cfg.get("parallel", "false")).lower() == "true"
                guard = 0
                while TaskQueue.pending_count(conn, run_id) > 0 and guard < 200:
                    guard += 1
                    ready = TaskQueue.ready_tasks(conn, run_id)
                    if not ready:
                        # 死锁/失败依赖：剩余任务置 blocked
                        for tk in TaskQueue.tasks(conn, run_id):
                            if tk["status"] in ("planned", "ready"):
                                TaskQueue.block(conn, tk["id"], "依赖任务失败，无法推进")
                        break
                    if parallel and len(ready) > 1:
                        from concurrent.futures import ThreadPoolExecutor
                        with ThreadPoolExecutor(max_workers=min(len(ready), 3)) as pool:
                            results_list = list(pool.map(lambda tk: self._run_task(tk, goal, conn), ready))
                    else:
                        results_list = [self._run_task(tk, goal, conn) for tk in ready]
                    for tk, rr in zip(ready, results_list):
                        tk_res, meta, ok, err = rr
                        if ok:
                            TaskQueue.complete(conn, tk["id"], tk_res, meta,
                                               latency_ms=int(meta.get("latency_ms", 0)))
                        else:
                            TaskQueue.fail(conn, tk["id"], err)
                        TaskQueue.release_deps(conn, run_id, tk["task_key"])
                        executed.append({"key": tk["task_key"], "title": tk["title"],
                                         "agent": tk["agent_id"], "ok": ok})
            except Exception as e:
                executed.append({"error": str(e)[:150]})

        # 4) 汇总
        summ = TaskQueue.summary(conn, run_id) if conn is not None else {"counts": {}, "total": 0, "items": []}
        done_items = [it for it in summ["items"] if it.get("status") == "done"]
        fail_items = [it for it in summ["items"] if it.get("status") in ("failed", "blocked")]
        summary = (f"Planner 计划 {summ['total']} 个子任务，完成 {len(done_items)}，"
                   f"失败/阻塞 {len(fail_items)}。\n\n")
        for it in summ["items"]:
            status_zh = {"done": "✅完成", "failed": "❌失败", "blocked": "⚠️阻塞", "ready": "待执行"}.get(it.get("status"), it.get("status", ""))
            res = (it.get("result") or "")[:200]
            summary += f"- [{it.get('task_key')}] {it.get('title')}（{it.get('agent_id') or '-'}）{status_zh}"
            if res:
                summary += f"\n  {res}"
            if it.get("error"):
                summary += f"\n  错误：{it['error']}"
            summary += "\n"
        meta_resp = {}
        return {
            "type": "planner", "node": label, "status": "done",
            "content": summary,
            "plan": plan, "task_summary": summ, "degraded": False,
            "data": {
                "plan": plan[:max_tasks], "task_total": summ["total"],
                "task_done": len(done_items), "task_failed": len(fail_items),
                "tasks": [{"key": it.get("task_key"), "title": it.get("title"),
                           "agent": it.get("agent_id"), "status": it.get("status")} for it in summ["items"]],
            },
            "latency_ms": int((time.time() - t0) * 1000),
        }

    def _exec_reflection(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        target = cfg.get("target", "")
        criteria = cfg.get("criteria", "输出是否完整、合理、符合要求")
        target_out = results.get(target) if target else None
        if not target_out:
            return {"type": "reflection", "node": label, "status": "error",
                    "content": f"目标节点 {target or '未配置'} 无输出", "data": {}, "latency_ms": 0}
        tgt_content = str(target_out.get("content") or target_out.get("error") or "")[:2000]
        t0 = time.time()
        # T1：复用公共评审（_evaluate_content 同时供 RefineGate 使用，行为与画布 reflection 一致）
        ev = self._evaluate_content(tgt_content, criteria)
        meta = ev.get("_meta", {})
        score = ev.get("score", 0)
        issues = ev.get("issues", [])
        passed = ev.get("passed", score >= 60)
        return {
            "type": "reflection", "node": label, "target": target,
            "status": "done", "content": f"评分 {score}/100 · {'通过' if passed else '未通过'} · 问题 {len(issues)} 项",
            "score": score, "issues": issues,
            "provider": meta.get("provider", "-"), "model": meta.get("model", "-"),
            "used_mock": meta.get("used_mock", True),
            "data": {"score": score, "passed": passed, "issues": issues[:10],
                     "advice": ev.get("advice", ""), "target": target,
                     "provider": meta.get("provider", "-"), "used_mock": meta.get("used_mock", True)},
            "latency_ms": int((time.time() - t0) * 1000),
        }

    @staticmethod
    def _clip_for_eval(text: str) -> str:
        """评审输入裁剪：**配置化上限 + 头尾采样**（复用 planner 的 `_head_tail_clip`）。

        为什么不再用 `[:2000]`（2026-09-20 conv 372 实测 —— **第 6 层截断，也是最隐蔽的一层**）：
        `[:2000]` 是**只留头**的硬上限，而汇总报告已长到 22,409 字符 → 评审只看得到
        「一、需求分析」为止，于是判「t2/t3 无实质内容」，**而那两节确实存在**。
        更隐蔽的是：报告被修得越长，评审看到的**比例**越小，gap 描述随报告结构漂移
        （「1.2 节末尾」→「2.2 节之后」→「只到 2.1」），极易误导成"报告被截断"而去调输出上限。
        """
        try:
            from core import config as _cfg
            cap = int(_cfg.get("refine", "eval_in_chars", 24000) or 24000)
        except Exception:                                          # noqa: BLE001
            cap = 24000
        try:
            from workflows.planner import FlowPlannerMixin
            return FlowPlannerMixin._head_tail_clip(text, cap)
        except Exception:                                          # noqa: BLE001
            t = str(text or "")
            return t if (cap <= 0 or len(t) <= cap) else t[:cap]

    @staticmethod
    def _evaluate_content(content: str, criteria: str = "输出是否完整、合理、符合要求") -> dict:
        """T1 公共评审函数：LLM 按标准对输出评分，返回 {score, passed, issues, advice, _meta}。

        供画布 reflection 节点（_exec_reflection）与会话编排 RefineGate 共用，
        保证「画布/会话」评审行为一致。LLM 异常/解析失败 → score=0（不抛异常，确定性保持）。
        """
        from llm import llm_client
        if not content:
            return {"score": 0, "passed": False, "issues": ["目标无输出"], "advice": "", "_meta": {}}
        tgt = FlowNodesMixin._clip_for_eval(content)
        prompt = (
            f"你是质量评审 Agent。对以下输出按标准评估，只输出 JSON："
            f'{{"score": 0-100 的整数, "passed": true/false, "issues": ["问题1", ...], "advice": "改进建议"}}\n\n'
            f"评估标准：{criteria}\n\n被评审输出：\n{tgt}"
        )
        try:
            # 评审输出是**短 JSON**（score/issues/advice）；显式给一个小上限：
            # 一是不与放大的评审输入争 context_window（超窗会让整轮评审降级为 score=0），
            # 二是防止模型"话多"把 JSON 冲散。
            resp = llm_client.chat([{"role": "user", "content": prompt}], max_tokens=1024)
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            raw = msg.get("content") or ""
            m = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(m.group(0)) if m else {"score": 0, "passed": False, "issues": ["解析失败"]}
            score = int(data.get("score", 0))
            issues = data.get("issues") or []
            passed = bool(data.get("passed", score >= 60))
            meta = resp.get("_meta", {})
            return {"score": score, "passed": passed, "issues": issues,
                    "advice": data.get("advice", ""), "_meta": meta}
        except Exception:
            return {"score": 0, "passed": False, "issues": ["评审解析失败"], "advice": "", "_meta": {}}

    # ── 运行轨迹落库（P0：先建 run 占位 → 检查点落库 → 结束写 steps）──
