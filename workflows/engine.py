"""FlowExecutor DAG 执行引擎主类（由 FlowPlannerMixin/FlowNodesMixin/FlowPersistenceMixin 组合）。"""
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from .tools import ToolRegistry
from .nodes import FlowNodesMixin
from .planner import FlowPlannerMixin
from .persistence import FlowPersistenceMixin


class FlowExecutor(FlowPlannerMixin, FlowNodesMixin, FlowPersistenceMixin):
    """DAG 执行引擎（真实执行 + 结构化 state + 条件分支）。

    节点类型：
    - llm   : 真实调用 LLM（prompt 支持 {{node_id.字段}} 模板引用上游输出）
    - tool  : 真实执行 ToolExecutor 工具（arguments 支持模板）
    - agent : 调用子 Agent（AgentPipeline，forced_intent 指定）
    - skill : 注入 published Skill 内容
    - mcp   : 调用 MCP 端点工具
    - if    : 条件分支节点（expression 求值 → data.value=true/false，驱动边路由）
    每节点输出统一 {content, data} 结构：content 供 LLM 消费，data 供条件/结构化引用。
    边支持可选条件：when(true/false 分支，if 节点出边) / condition(通用表达式)，
    未满足条件的下游节点标记 skipped 跳过。
    """

    NODE_TYPES = ("llm", "tool", "skill", "mcp", "agent", "if", "orchestrator", "reflection",
                  "code", "http", "iteration", "knowledge", "pubsub", "debate", "webhook")

    def __init__(self, conn=None):
        self.registry = ToolRegistry(conn)
        self.run_id = 0
        self._hil_queued: dict = {}  # M5：并行路径 HIL 预拦截——node_id → 确认单 id

    def run(self, nodes: list, edges: list, payload: Optional[dict] = None, conn=None,
            persist: bool = False, flow_id: int = 0, flow_name: str = "",
            resume_run_id: int | None = None, max_iterations: int = 3, run_id: int = 0,
            parent_run_id: int | None = None):
        """执行整个 DAG（P0：显式状态 + 检查点 + 中断恢复 + 循环回跳）。

        persist=True 且 conn 提供时：每节点后落检查点（flow_checkpoints），结束时写轨迹（flow_runs/steps）。
        resume_run_id: 从该运行的最新检查点恢复（已完成节点复用结果，只执行未完成部分）。
        run_id: M4 异步运行——复用外部已创建的占位运行记录（>0 时不新建 flow_runs，清旧轨迹续写）。
        max_iterations: 循环边（loop:true）回跳最大次数，防死循环。
        """
        payload = payload or {}
        self._nodes = nodes  # D3：供 iteration 节点按 id 查找子流程节点
        # orchestrator/planner 内部集中调度走串行（主线程），避免 worker 线程嵌套 Agent 调用 + 落库连接竞争
        has_orch = any(n.get("type") in ("orchestrator", "planner") for n in nodes)
        if not has_orch and not any(e.get("loop") for e in edges) \
                and not any(e.get("when") or e.get("condition") for e in edges):
            return self._run_parallel(nodes, edges, payload, conn, persist, flow_id, flow_name,
                                      resume_run_id, max_iterations, run_id, parent_run_id)
        order = self._topo_sort(nodes, edges)
        results: dict[str, Any] = {}
        errors = []
        executed: set = set()
        iter_count: dict = {}
        order_done: list = []
        t_start = time.time()
        run_id = run_id or 0
        cp_seq = 0
        resumed: set = set()
        blackboard: dict = {}  # P1：L1 工作记忆（共享黑板，多 Agent 共享）

        if persist and conn is not None:
            try:
                if resume_run_id:
                    run_id = resume_run_id  # D6：恢复运行直接复用原 run_id，不新建占位行（保留检查点时间旅行历史）
                elif run_id and run_id > 0:
                    # M4：复用占位运行（清旧轨迹，防残留）
                    for tbl in ("flow_checkpoints", "flow_working_memory", "flow_conversations", "flow_run_steps"):
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE run_id=?", (run_id,))
                        except Exception:
                            pass
                else:
                    run_id = self._ensure_run(conn, flow_id, flow_name, parent_run_id or 0)
                conn.commit()  # 立即提交占位事务，避免后续 worker/Agent 内部连接被长事务阻塞
            except Exception:
                run_id = 0
            try:
                blackboard = self._load_blackboard(conn, run_id)
            except Exception:
                pass
        if resume_run_id and conn is not None:
            try:
                results, executed, cp_seq, resumed = self._load_checkpoints(conn, resume_run_id)
                run_id = resume_run_id
                blackboard = self._load_blackboard(conn, run_id)
            except Exception:
                pass
        conv_seq = 0
        self.run_id = run_id  # M3：供 planner/orchestrator 节点访问任务队列归属运行
        self._run_flow_name = flow_name or ""  # D12：告警评估的事件归属流程名
        self._prequeue_hil(conn, nodes, run_id)  # M5：全节点 HIL 预拦截（串行/并行路径统一）
        if payload.get("input") and run_id:
            conv_seq += 1
            self._log_conversation(conn, run_id, conv_seq, "user", str(payload["input"])[:2000])

        i = 0
        paused = False  # D6：主动暂停——每节点检查点后轮询 flow_runs.status=='paused'，命中则静默停机保留检查点
        while i < len(order):
            node_id = order[i]
            node = next((n for n in nodes if n.get("id") == node_id), None)
            if node is None:
                errors.append({"node": node_id, "error": "节点不存在"})
                i += 1
                continue
            # 恢复的节点：复用历史结果，跳过执行
            if node_id in resumed:
                order_done.append(node_id)
                i += 1
                continue
            # ── 条件激活判定：任一条入边（上游已执行 + 条件满足）激活该节点；loop 边是回跳控制不参与激活 ──
            in_edges = [e for e in edges if e.get("target") == node_id and not e.get("loop")]
            if in_edges:
                active = False
                for e in in_edges:
                    src = e.get("source")
                    if src not in executed:
                        continue
                    if e.get("when"):
                        val = (results.get(src, {}).get("data") or {}).get("value")
                        if str(val).lower() != str(e["when"]).lower():
                            continue
                    if e.get("condition"):
                        rendered = self._render(str(e["condition"]), results, payload, blackboard)
                        if not self._eval_cond(rendered):
                            continue
                    active = True
                    break
                if not active:
                    results[node_id] = {
                        "type": node.get("type"), "node": node_id, "status": "skipped",
                        "content": "上游条件未满足，节点跳过", "data": {}, "latency_ms": 0,
                    }
                    order_done.append(node_id)
                    cp_seq += 1
                    if run_id:
                        self._safe_cp(conn, run_id, cp_seq, node_id, results)
                    i += 1
                    continue
            # ── 执行节点 ──
            t0 = time.time()
            try:
                out = self._exec_node(node, results, payload, conn, blackboard)
                out["latency_ms"] = int((time.time() - t0) * 1000)
                results[node_id] = out
                executed.add(node_id)
                # P1：write_keys 写入黑板（共享工作记忆）
                if out.get("status") == "done" and run_id:
                    try:
                        self._write_blackboard(conn, run_id, node, out, blackboard)
                    except Exception:
                        pass
                # P1：编排级会话记录（user/agent/llm 关键消息）
                if out.get("status") == "done" and run_id and node.get("type") in ("agent", "llm"):
                    conv_seq += 1
                    self._log_conversation(conn, run_id, conv_seq, node.get("type"),
                                           str(out.get("content", ""))[:2000])
            except Exception as e:  # 单节点失败不中断整链（标记 error 继续）
                out = {
                    "type": node.get("type"), "node": node_id, "status": "error",
                    "error": str(e)[:120], "data": {}, "latency_ms": int((time.time() - t0) * 1000),
                }
                results[node_id] = out
                errors.append({"node": node_id, "error": str(e)[:120]})
            order_done.append(node_id)
            cp_seq += 1
            if run_id:
                self._safe_cp(conn, run_id, cp_seq, node_id, results)
            # D11 事件回调：节点完成/出错时向订阅的 webhook_url 发射标准化事件（失败不阻断）
            if run_id:
                self._emit_event(conn, run_id, node_id,
                                 str(node.get("label") or node.get("name") or node_id),
                                 str(node.get("type") or ""),
                                 "node_done" if out.get("status") == "done" else "node_error",
                                 {"status": out.get("status"),
                                  "content": str(out.get("content", ""))[:500],
                                  "error": str(out.get("error", ""))[:200]})
            # D6：主动暂停检查——检查点已落库后停机，断点保留可恢复
            if run_id and self._check_pause(conn, run_id):
                paused = True
                break
            # ── 循环回跳（P0）：if 节点的 loop 出边，条件命中且未超限 → 重置目标及下游重跑 ──
            if node.get("type") == "if" and out.get("status") == "done":
                val = (out.get("data") or {}).get("value")
                jumped = False
                for e in edges:
                    if e.get("source") != node_id or not e.get("loop"):
                        continue
                    tgt = e.get("target")
                    if tgt not in [n["id"] for n in nodes]:
                        continue
                    when = e.get("when", "false")
                    if str(val).lower() != str(when).lower():
                        continue
                    if iter_count.get(tgt, 0) >= max_iterations:
                        continue
                    iter_count[tgt] = iter_count.get(tgt, 0) + 1
                    idx_tgt = next((k for k, nid in enumerate(order) if nid == tgt), None)
                    if idx_tgt is None:
                        continue
                    for k in range(idx_tgt, len(order)):
                        rid = order[k]
                        results.pop(rid, None)
                        executed.discard(rid)
                        resumed.discard(rid)
                    order_done = [x for x in order_done if x not in order[idx_tgt:]]
                    i = idx_tgt
                    jumped = True
                    break
                if jumped:
                    continue
            i += 1

        summary = {
            "order": order_done,
            "results": results,
            "status": "paused" if paused else ("completed" if not errors else "partial"),
            "errors": errors,
            "total_latency_ms": int((time.time() - t_start) * 1000),
            "skipped": [nid for nid, r in results.items() if r.get("status") == "skipped"],
            "run_id": run_id,
            "checkpoint_count": cp_seq,
            "loop_count": sum(iter_count.values()),
            "paused": paused,
        }
        # D11 事件回调：运行完成/中止时发射（全局与 run 级订阅）
        if run_id:
            self._emit_event(conn, run_id, "", "", "flow",
                             "run_completed",
                             {"status": summary["status"], "error_count": len(errors),
                              "total_latency_ms": summary["total_latency_ms"]})
        if persist and conn is not None and run_id:
            try:
                self._persist_steps(conn, run_id, order_done, results, summary)
            except Exception:
                pass  # 轨迹落库失败不影响运行结果
            # D12 统一监控：运行结束后告警规则评估（命中写 alert_events，失败不阻断）
            try:
                self._evaluate_alerts(conn, run_id, summary)
            except Exception:
                pass
        return summary

    # ── 分层（P2 并行用）：忽略 loop 边，按入度分层 ──
    def _layers(self, nodes: list, edges: list) -> list:
        node_ids = [n.get("id") for n in nodes if n.get("id")]
        in_deg = {nid: 0 for nid in node_ids}
        for e in edges:
            if e.get("loop"):
                continue
            if e.get("source") in in_deg and e.get("target") in in_deg:
                in_deg[e["target"]] += 1
        layers, remaining = [], set(node_ids)
        while remaining:
            layer = [nid for nid in remaining if in_deg[nid] == 0]
            if not layer:  # 理论不可达（无 loop 边无环）；保险
                layer = [next(iter(remaining))]
            layers.append(layer)
            for nid in layer:
                remaining.discard(nid)
                for e in edges:
                    if e.get("loop"):
                        continue
                    if e.get("source") == nid and e.get("target") in remaining:
                        in_deg[e["target"]] -= 1
        return layers

    # ── P2：分层并行执行（纯 DAG；层内 ThreadPoolExecutor 并行，层间串行，保持确定性）──
    def _run_parallel(self, nodes: list, edges: list, payload: dict, conn, persist: bool,
                      flow_id: int, flow_name: str, resume_run_id=None, max_iterations: int = 3,
                      run_id: int = 0, parent_run_id: int | None = None) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        payload = payload or {}
        self._nodes = nodes  # D3：供 iteration 节点按 id 查找子流程节点
        layers = self._layers(nodes, edges)
        results: dict = {}
        errors = []
        executed: set = set()
        order_done: list = []
        t_start = time.time()
        run_id = run_id or 0
        cp_seq = 0
        resumed: set = set()
        blackboard: dict = {}
        if persist and conn is not None:
            try:
                if resume_run_id:
                    run_id = resume_run_id  # D6：恢复运行直接复用原 run_id，不新建占位行（保留检查点时间旅行历史）
                elif run_id and run_id > 0:
                    for tbl in ("flow_checkpoints", "flow_working_memory", "flow_conversations", "flow_run_steps"):
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE run_id=?", (run_id,))
                        except Exception:
                            pass
                else:
                    run_id = self._ensure_run(conn, flow_id, flow_name, parent_run_id or 0)
                conn.commit()  # 立即提交占位事务，避免后续 worker/Agent 内部连接被长事务阻塞
                blackboard = self._load_blackboard(conn, run_id)
            except Exception:
                pass
        if resume_run_id and conn is not None:
            try:
                results, executed, cp_seq, resumed = self._load_checkpoints(conn, resume_run_id)
                run_id = resume_run_id
                blackboard = self._load_blackboard(conn, run_id)
            except Exception:
                pass
        self.run_id = run_id
        self._run_flow_name = flow_name or ""  # D12：告警评估的事件归属流程名
        self._prequeue_hil(conn, nodes, run_id)  # M5：并行路径 HIL 预拦截（主线程落确认单）

        def _exec_one(nid):
            node = next((n for n in nodes if n.get("id") == nid), None)
            if node is None:
                return nid, None, {"type": "", "node": nid, "status": "error", "error": "节点不存在",
                                   "data": {}, "latency_ms": 0}
            in_edges = [e for e in edges if e.get("target") == nid]
            if in_edges and not all(e.get("source") in executed for e in in_edges):
                return nid, node, {"type": node.get("type"), "node": nid, "status": "skipped",
                                   "content": "上游未就绪", "data": {}, "latency_ms": 0}
            t0 = time.time()
            try:
                # worker 线程只计算：不写库（持久化由主线程统一执行，避免请求级事务写锁互斥）
                out = self._exec_node(node, results, payload, None, blackboard)
                out["latency_ms"] = int((time.time() - t0) * 1000)
                return nid, node, out
            except Exception as e:
                return nid, node, {"type": node.get("type"), "node": nid, "status": "error",
                                   "error": str(e)[:120], "data": {},
                                   "latency_ms": int((time.time() - t0) * 1000)}

        paused = False  # D6：主动暂停——每层完成后轮询 flow_runs.status=='paused'，命中则静默停机保留检查点
        for raw_layer in layers:
            layer = [nid for nid in raw_layer if nid not in resumed]
            # D6：resume 场景——被恢复跳过的已完成节点仍计入轨迹（_persist_steps 全量重写）
            order_done.extend(nid for nid in raw_layer if nid in resumed)
            if not layer:
                continue
            with ThreadPoolExecutor(max_workers=4) as pool:
                for nid, node, out in pool.map(_exec_one, layer):
                    results[nid] = out
                    order_done.append(nid)
                    if out.get("status") == "done":
                        executed.add(nid)
                    elif out.get("status") == "error":
                        errors.append({"node": nid, "error": out.get("error", "")})
                    # 主线程统一落库（检查点/黑板），避免 worker 线程与请求级事务互斥
                    cp_seq += 1
                    if run_id:
                        self._safe_cp(conn, run_id, cp_seq, nid, results)
                    # D11 事件回调：节点完成/出错（主线程发射，HTTP 回调不占 worker）
                    if run_id and node:
                        self._emit_event(conn, run_id, nid,
                                         str(node.get("label") or node.get("name") or nid),
                                         str(node.get("type") or ""),
                                         "node_done" if out.get("status") == "done" else "node_error",
                                         {"status": out.get("status"),
                                          "content": str(out.get("content", ""))[:500],
                                          "error": str(out.get("error", ""))[:200]})
                    if node and out.get("status") == "done":
                        try:
                            self._write_blackboard(conn, run_id, node, out, blackboard)
                        except Exception:
                            pass
            # D6：主动暂停检查——本层检查点已落库后停机（层粒度），断点保留可恢复
            if run_id and self._check_pause(conn, run_id):
                paused = True
                break

        summary = {
            "order": order_done,
            "results": results,
            "status": "paused" if paused else ("completed" if not errors else "partial"),
            "errors": errors,
            "total_latency_ms": int((time.time() - t_start) * 1000),
            "skipped": [nid for nid, r in results.items() if r.get("status") == "skipped"],
            "run_id": run_id,
            "checkpoint_count": cp_seq,
            "loop_count": 0,
            "parallel": True,
            "paused": paused,
        }
        # D11 事件回调：运行完成/中止（并行路径）
        if run_id:
            self._emit_event(conn, run_id, "", "", "flow",
                             "run_completed",
                             {"status": summary["status"], "error_count": len(errors),
                              "total_latency_ms": summary["total_latency_ms"]})
        if persist and conn is not None and run_id:
            try:
                self._persist_steps(conn, run_id, order_done, results, summary)
            except Exception:
                pass
            # D12 统一监控：运行结束后告警规则评估（并行路径，失败不阻断）
            try:
                self._evaluate_alerts(conn, run_id, summary)
            except Exception:
                pass
        return summary

    # ── 拓扑排序（Kahn 算法，检测环；loop 边不参与入度，运行时处理）──
    def _topo_sort(self, nodes: list, edges: list) -> list:
        node_ids = [n.get("id") for n in nodes if n.get("id")]
        in_degree = {nid: 0 for nid in node_ids}
        adj: dict[str, list] = {nid: [] for nid in node_ids}
        for e in edges:
            src, tgt = e.get("source"), e.get("target")
            if e.get("loop"):
                continue  # 循环边不进拓扑，避免成环
            if src in in_degree and tgt in in_degree:
                adj[src].append(tgt)
                in_degree[tgt] += 1
        queue = [nid for nid, d in in_degree.items() if d == 0]
        order = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for nxt in adj[nid]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        # 有环时：剩余节点按原声明顺序追加（不丢节点）
        if len(order) < len(node_ids):
            for nid in node_ids:
                if nid not in order:
                    order.append(nid)
        return order

    # ── 模板渲染：{{node_id}} / {{node_id.字段}} / {{payload.键}} / {{state.x}} / {{blackboard.键}} ──
    def _render(self, template, results: dict, payload: dict, blackboard: dict | None = None) -> str:
        if not template:
            return ""
        blackboard = blackboard or {}
        def _rep(m):
            key = m.group(1).strip()
            if key.startswith("blackboard."):
                v = blackboard.get(key[11:])
                if isinstance(v, (dict, list)):
                    try:
                        return json.dumps(v, ensure_ascii=False)
                    except Exception:
                        return str(v)
                return str(v) if v is not None else ""
            if key.startswith("payload."):
                return str(payload.get(key[8:], ""))
            if key.startswith("state."):
                # 显式 State 视图：state.input.x → payload.x；state.results.n1.content → 节点结果
                sub = key[6:]
                if sub.startswith("input."):
                    return str(payload.get(sub[6:], ""))
                if sub.startswith("results."):
                    parts = sub.split(".")
                    cur = results.get(parts[1])
                    if cur is None:
                        return f"{{{{{key}}}}}"
                    for p in parts[2:]:
                        if isinstance(cur, dict):
                            cur = cur.get(p)
                        else:
                            return ""
                    return str(cur) if cur is not None else ""
                return str(payload.get(sub, ""))
            parts = key.split(".")
            cur = results.get(parts[0])
            if cur is None:
                return f"{{{{{key}}}}}"
            if len(parts) == 1:
                return str(cur.get("content", ""))
            for p in parts[1:]:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    return ""
            return str(cur) if cur is not None else ""
        return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", _rep, template)

    # ── 条件表达式求值（渲染后字符串）：left op right / contains / 布尔字面量 ──
    def _eval_cond(self, expr: str) -> bool:
        s = (expr or "").strip().strip('"\'')
        if not s:
            return False
        m = re.match(r"^(.*?)\s+(contains|==|!=|>=|<=|>|<)\s+(.*)$", s)
        if m:
            left, op, right = m.group(1).strip(), m.group(2), m.group(3).strip().strip('"\'')
            if op == "contains":
                return str(left).lower() in str(right).lower() or str(right).lower() in str(left).lower()
            try:
                lf, rf = float(left), float(right)
                return {"==": lf == rf, "!=": lf != rf, ">": lf > rf, ">=": lf >= rf,
                        "<": lf < rf, "<=": lf <= rf}[op]
            except ValueError:
                ls, rs = str(left), str(right)
                return {"==": ls == rs, "!=": ls != rs}.get(op, False)
        if s.lower() in ("true", "yes", "1"):
            return True
        if s.lower() in ("false", "no", "0"):
            return False
        return bool(s)

    # ── 节点执行 ──
    def _exec_node(self, node: dict, results: dict, payload: dict, conn, blackboard: dict | None = None) -> dict:
        ntype = node.get("type", "llm")
        label = node.get("label", node.get("name", node.get("id", "")))
        cfg = node.get("config", {}) or {}
        if ntype == "llm":
            return self._exec_llm(label, cfg, results, payload, blackboard)
        if ntype == "tool":
            return self._exec_tool(label, cfg, results, payload, conn, blackboard)
        if ntype == "agent":
            return self._exec_agent(label, cfg, results, payload, conn, blackboard)
        if ntype == "skill":
            return self._exec_skill(label, cfg, results, payload, conn)
        if ntype == "mcp":
            return self._exec_mcp(label, cfg, results, payload, blackboard)
        if ntype == "if":
            return self._exec_if(label, cfg, results, payload, blackboard)
        if ntype == "orchestrator":
            return self._exec_orchestrator(label, cfg, results, payload, conn, blackboard)
        if ntype == "reflection":
            return self._exec_reflection(label, cfg, results, payload, blackboard)
        if ntype == "react":
            return self._exec_react(label, cfg, results, payload, conn, blackboard)
        if ntype == "planner":
            return self._exec_planner(label, cfg, results, payload, conn, blackboard)
        if ntype == "code":
            return self._exec_code(label, cfg, results, payload, blackboard)
        if ntype == "http":
            return self._exec_http(label, cfg, results, payload, blackboard)
        if ntype == "iteration":
            return self._exec_iteration(label, cfg, results, payload, blackboard)
        if ntype == "knowledge":
            return self._exec_knowledge(label, cfg, results, payload, blackboard)
        if ntype == "pubsub":
            return self._exec_pubsub(label, cfg, results, payload, conn, blackboard)
        if ntype == "debate":
            return self._exec_debate(label, cfg, results, payload, blackboard)
        if ntype == "webhook":
            return self._exec_webhook(label, cfg, results, payload, blackboard)
        return {"type": ntype, "node": label, "status": "skipped", "content": "", "data": {}}

    def _exec_llm(self, label: str, cfg: dict, results: dict, payload: dict, blackboard: dict | None = None) -> dict:
        """真实 LLM 调用：prompt 渲染上游输出/黑板 → llm_client.chat。"""
        from llm import llm_client
        prompt = self._render(cfg.get("prompt", ""), results, payload, blackboard)
        # P1：subscribe 订阅黑板产物注入 prompt
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
                prompt = "订阅的共享上下文：\n" + "\n".join(parts) + "\n\n任务：\n" + prompt
        provider_id = cfg.get("provider_id")
        model = (cfg.get("model") or "").strip() or None
        messages = [{"role": "user", "content": prompt}]
        sys_p = (cfg.get("system_prompt") or "").strip()
        if sys_p:
            messages.insert(0, {"role": "system", "content": sys_p})
        resp = llm_client.chat(messages, provider_id=provider_id, model=model)
        msg = (resp.get("choices") or [{}])[0].get("message", {})
        content = msg.get("content") or ""
        meta = resp.get("_meta", {})
        return {
            "type": "llm", "node": label, "status": "done",
            "content": content, "prompt": prompt[:200],
            "provider": meta.get("provider", "-"), "model": meta.get("model", "-"),
            "used_mock": meta.get("used_mock", True),
            "data": {
                "provider": meta.get("provider", "-"), "model": meta.get("model", "-"),
                "used_mock": meta.get("used_mock", True), "prompt": prompt[:500],
            },
        }

    def _prequeue_hil(self, conn, nodes: list, run_id: int) -> None:
        """M5：HIL L2 写工具预拦截——并行路径 worker 不落库，主线程先统一登记确认单。

        串行路径同样受益：_exec_tool 命中预登记 id 直接返回 pending_hil，避免重复入队。
        """
        from hil_service import HILService
        self._hil_queued = {}
        if conn is None:
            return
        for n in nodes or []:
            if n.get("type") != "tool":
                continue
            cfg = n.get("config") or {}
            tool_name = cfg.get("tool", "")
            if cfg.get("hil_level") == "L2" and HILService.is_write_tool(tool_name):
                args = cfg.get("arguments") or {}
                key = n.get("label") or n.get("name") or n.get("id", "")
                conf = HILService.queue_confirmation(
                    conn, key, tool_name, args,
                    preview=f"Agent 请求执行写操作 {tool_name}，参数：{json.dumps(args, ensure_ascii=False)[:300]}",
                    run_id=run_id)
                if conf:
                    self._hil_queued[key] = conf["id"]

