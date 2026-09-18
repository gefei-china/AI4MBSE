# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：Agent 流程/运行/HIL/规划器/监控/AI 生成。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/agent-flows")
def list_agent_flows(conn=Depends(db_session)):
    return StudioRepo(conn).list_agent_flows()


@router.get("/api/studio/agent-flows/{fid}")
def get_agent_flow(fid: int, conn=Depends(db_session)):
    flow = StudioRepo(conn).get_agent_flow(fid)
    if not flow:
        return JSONResponse({"error": "Not found"}, 404)
    return flow


@router.post("/api/studio/agent-flows")
def save_agent_flow(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    fid = body.get("id")
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "流程名称必填（请在「⚙ 流程设置」中填写）"}, 400)
    nodes = json.dumps(body.get("nodes", []))
    edges = json.dumps(body.get("edges", []))
    version = body.get("version", "v1")
    status = body.get("status", "draft")
    source = body.get("source", "manual")
    if fid:
        if not repo.get_agent_flow(fid):
            return JSONResponse({"error": "Not found"}, 404)
        repo.update_agent_flow(fid, body["name"], body.get("description", ""), nodes, edges, version=version, status=status, source=source)
    else:
        fid = repo.create_agent_flow(
            body["name"], body.get("description", ""), nodes, edges, version=version, status=status, source=source)
    audit(audit_user(user), "flow_save", f"保存Agent流程: {body['name']}#{fid}", conn=conn)
    return {"ok": True, "id": fid}


@router.delete("/api/studio/agent-flows/{fid}")
def delete_agent_flow(fid: int, conn=Depends(db_session), user=Depends(current_user)):
    repo = StudioRepo(conn)
    if not repo.get_agent_flow(fid):
        return JSONResponse({"error": "Not found"}, 404)
    repo.delete_agent_flow(fid)
    audit(audit_user(user), "flow_delete", f"删除Agent流程#{fid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/agent-flows/{fid}/run")
def run_agent_flow(fid: int, body: dict | None = None, conn=Depends(db_session), user=Depends(current_user)):
    """P0-4: DAG 执行引擎——按拓扑序执行流程节点，返回逐步结果供前端可视化。"""
    flow = StudioRepo(conn).get_agent_flow(fid)
    if not flow:
        return JSONResponse({"error": "Not found"}, 404)
    body = body or {}
    # 画布最新编辑优先（definition 传入），否则用 DB 存储
    nodes = json.loads(flow["nodes"] or "[]")
    edges = json.loads(flow["edges"] or "[]")
    if body.get("definition"):
        nodes = body["definition"].get("nodes", nodes)
        edges = body["definition"].get("edges", edges)
    executor = FlowExecutor(conn)
    result = executor.run(nodes, edges, body.get("payload"), conn,
                          persist=True, flow_id=fid, flow_name=flow["name"])
    audit(audit_user(user), "flow_run", f"执行Agent流程#{fid}: {flow['name']} → {result['status']}", conn=conn)
    return {"flow_id": fid, "name": flow["name"], **result}


@router.post("/api/studio/flow-runs/{rid}/pause")
def pause_flow_run(rid: int, conn=Depends(db_session), user=Depends(current_user)):
    """D6 主动暂停：标记运行 paused，执行器在下一节点/层检查点后静默停机（检查点保留，可从断点恢复）。"""
    repo = StudioRepo(conn)
    run = repo.one("SELECT * FROM flow_runs WHERE id=?", (rid,))
    if not run:
        return JSONResponse({"error": "Not found"}, 404)
    if run["status"] in ("completed", "paused"):
        return {"ok": True, "status": run["status"], "note": "运行已结束或已暂停，无需重复操作"}
    conn.execute("UPDATE flow_runs SET status='paused' WHERE id=?", (rid,))
    conn.commit()
    audit(audit_user(user), "flow_pause", f"暂停运行#{rid}: {run.get('flow_name')}", conn=conn)
    return {"ok": True, "status": "paused", "run_id": rid}


@router.post("/api/studio/flow-runs/{rid}/resume")
def resume_flow_run(rid: int, body: dict | None = None, conn=Depends(db_session), user=Depends(current_user)):
    """P0 中断恢复：从该运行的最新检查点续跑（已完成节点复用结果，只执行未完成部分）。"""
    repo = StudioRepo(conn)
    run = repo.one("SELECT * FROM flow_runs WHERE id=?", (rid,))
    if not run:
        return JSONResponse({"error": "Not found"}, 404)
    flow = repo.one("SELECT * FROM agent_flows WHERE id=?", (run["flow_id"],)) if run.get("flow_id") else None
    body = body or {}
    nodes = json.loads((flow or {}).get("nodes") or "[]")
    edges = json.loads((flow or {}).get("edges") or "[]")
    if body.get("definition"):
        nodes = body["definition"].get("nodes", nodes)
        edges = body["definition"].get("edges", edges)
    executor = FlowExecutor(conn)
    # D6：恢复前重置状态——执行器节点完成后的 _check_pause 依赖 status!='paused' 继续执行
    if run["status"] == "paused":
        conn.execute("UPDATE flow_runs SET status='running' WHERE id=?", (rid,))
        conn.commit()
    result = executor.run(nodes, edges, body.get("payload"), conn,
                          persist=True, flow_id=run["flow_id"], flow_name=run.get("flow_name") or "",
                          resume_run_id=rid)
    audit(audit_user(user), "flow_resume", f"恢复运行#{rid}: {run.get('flow_name')} → {result['status']}", conn=conn)
    return {"flow_id": run["flow_id"], "name": run.get("flow_name", ""), "resumed_from": rid, **result}


@router.get("/api/studio/flow-runs/{rid}/checkpoints")
def list_flow_checkpoints(rid: int, conn=Depends(db_session)):
    """P0 时间旅行：该运行的全部检查点列表（seq/node_id/时间）。"""
    cps = StudioRepo(conn).rows(
        "SELECT id, seq, node_id, created_at FROM flow_checkpoints WHERE run_id=? ORDER BY seq", (rid,))
    return cps


@router.get("/api/studio/flow-runs/{rid}/checkpoints/{seq}")
def get_flow_checkpoint(rid: int, seq: int, conn=Depends(db_session)):
    """P0 时间旅行：指定检查点的 state 快照（该时刻全部节点结果）。"""
    row = StudioRepo(conn).one(
        "SELECT seq, node_id, state_json, created_at FROM flow_checkpoints WHERE run_id=? AND seq=?",
        (rid, seq))
    if not row:
        return JSONResponse({"error": "Not found"}, 404)
    try:
        state = json.loads(row["state_json"] or "{}")
    except Exception:
        state = {}
    return {"run_id": rid, "seq": row["seq"], "node_id": row["node_id"],
            "created_at": row["created_at"], "results": state.get("results", {})}


@router.get("/api/studio/memory/blackboard")
def get_blackboard(run_id: int, conn=Depends(db_session)):
    """该运行（run_id）的共享黑板内容（flow_working_memory）。"""
    rows = StudioRepo(conn).rows(
        "SELECT key, value_json, mem_type, updated_at FROM flow_working_memory WHERE run_id=? ORDER BY id",
        (run_id,))
    for r in rows:
        try:
            r["value"] = json.loads(r.pop("value_json") or "null")
        except Exception:
            r["value"] = r.pop("value_json")
    return rows


@router.get("/api/studio/agents/{aid}/memory")
def get_agent_memory(aid: str, conn=Depends(db_session)):
    """该 Agent 的长期记忆（agent_memory）。"""
    rows = StudioRepo(conn).rows(
        "SELECT id, mem_type, content, created_at FROM agent_memory WHERE agent_id=? ORDER BY id DESC LIMIT 50",
        (aid,))
    return rows


@router.post("/api/studio/agents/{aid}/memory")
def add_agent_memory(aid: str, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """写入该 Agent 的长期记忆（手动沉淀：{content, mem_type?}）。"""
    content = (body or {}).get("content", "").strip()
    if not content:
        return JSONResponse({"error": "content 必填"}, 400)
    mem_type = (body or {}).get("mem_type", "fact")
    cur = conn.execute(
        "INSERT INTO agent_memory (agent_id, mem_type, content) VALUES (?,?,?)",
        (aid, mem_type, content[:1000]))
    audit(audit_user(user), "agent_memory_write", f"写入记忆 {aid}: {content[:30]}", conn=conn)
    return {"ok": True, "id": cur.lastrowid}


@router.get("/api/studio/monitor/runs")
def monitor_runs(days: int = 7, conn=Depends(db_session)):
    """编排运行监控聚合：总览 + 按流程统计 + 节点类型耗时 + 最近失败归因。"""
    repo = StudioRepo(conn)
    rows = repo.rows(
        "SELECT * FROM flow_runs WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 200",
        (f"-{days} days",))
    total = len(rows)
    ok = sum(1 for r in rows if r.get("status") == "completed")
    avg_ms = int(sum(r.get("total_latency_ms") or 0 for r in rows) / total) if total else 0
    # 按流程聚合
    by_flow: dict = {}
    for r in rows:
        f = by_flow.setdefault(r.get("flow_name") or f"#{r.get('flow_id')}",
                                {"runs": 0, "ok": 0, "total_ms": 0})
        f["runs"] += 1
        f["ok"] += 1 if r.get("status") == "completed" else 0
        f["total_ms"] += r.get("total_latency_ms") or 0
    flow_stats = [{"name": k, **v, "avg_ms": v["total_ms"] // max(v["runs"], 1)}
                  for k, v in by_flow.items()]
    flow_stats.sort(key=lambda x: -x["runs"])
    # 节点类型耗时分布 + 失败归因
    steps = repo.rows(
        "SELECT node_type, status, latency_ms FROM flow_run_steps WHERE run_id IN "
        "(SELECT id FROM flow_runs WHERE created_at >= datetime('now', ?))", (f"-{days} days",))
    type_stats: dict = {}
    failures = []
    for s in steps:
        t = type_stats.setdefault(s.get("node_type") or "?", {"count": 0, "total_ms": 0, "errors": 0})
        t["count"] += 1
        t["total_ms"] += s.get("latency_ms") or 0
        if s.get("status") == "error":
            t["errors"] += 1
            failures.append({"type": s.get("node_type"), "count": 1})
    node_stats = [{"type": k, **v, "avg_ms": v["total_ms"] // max(v["count"], 1)}
                  for k, v in type_stats.items()]
    fail_by_type: dict = {}
    for f in failures:
        fail_by_type[f["type"]] = fail_by_type.get(f["type"], 0) + 1
    return {
        "days": days, "total_runs": total, "success_runs": ok,
        "success_rate": round(ok * 100 / total, 1) if total else 0,
        "avg_latency_ms": avg_ms,
        "by_flow": flow_stats[:10],
        "node_stats": node_stats,
        "failures_by_type": fail_by_type,
    }


@router.post("/api/studio/agent-flows/{fid}/run-async")
def run_agent_flow_async(fid: int, body: dict | None = None, conn=Depends(db_session), user=Depends(current_user)):
    """异步运行：立即返回占位 run_id，后台线程执行 DAG，前端经 /events SSE 订阅进度。

    与同步 /run 的区别：不阻塞 HTTP 请求；Planner/ReAct 等长节点可安全执行。
    """
    flow = StudioRepo(conn).get_agent_flow(fid)
    if not flow:
        return JSONResponse({"error": "Not found"}, 404)
    body = body or {}
    # 占位运行记录（后台线程复用 run_id 续写，见 FlowExecutor.run(run_id=...)）
    cur = conn.execute(
        "INSERT INTO flow_runs (flow_id, flow_name, status, order_json, total_latency_ms, error_count) "
        "VALUES (?,?,?,?,?,?)",
        (fid, flow["name"], "running", "[]", 0, 0))
    run_id = cur.lastrowid
    conn.commit()

    def _worker():
        from database import db_conn
        try:
            with db_conn() as c:
                nodes = json.loads(flow["nodes"] or "[]")
                edges = json.loads(flow["edges"] or "[]")
                if body.get("definition"):
                    nodes = body["definition"].get("nodes", nodes)
                    edges = body["definition"].get("edges", edges)
                executor = FlowExecutor(c)
                executor.run(nodes, edges, body.get("payload"), c, persist=True,
                             flow_id=fid, flow_name=flow["name"], run_id=run_id)
        except Exception as e:
            try:
                with db_conn() as c:
                    c.execute("UPDATE flow_runs SET status='failed', error_count=error_count+1 WHERE id=?",
                              (run_id,))
                    c.commit()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()
    audit(audit_user(user), "flow_run_async", f"异步执行Agent流程#{fid}: {flow['name']}", conn=conn)
    return {"ok": True, "run_id": run_id, "flow_id": fid, "name": flow["name"]}


@router.get("/api/studio/flow-runs/{rid}/events")
def flow_run_events(rid: int):
    """M4：SSE 进度事件流——运行中按检查点（实时落库）推 step 进度，结束推 done。

    前端 EventSource 订阅；运行结束（completed/partial/failed/paused）推 done 后断开。
    运行中 step 数据取自 flow_checkpoints（每节点执行后即时落库，flow_run_steps 仅结束时全量写）。
    """
    def gen():
        last_seq = 0
        for _ in range(600):  # 上限 600s
            try:
                from database import get_db
                c = get_db()
                try:
                    run = c.execute(
                        "SELECT status, total_latency_ms, error_count FROM flow_runs WHERE id=?", (rid,)).fetchone()
                    cps = c.execute(
                        "SELECT seq, node_id FROM flow_checkpoints WHERE run_id=? ORDER BY seq",
                        (rid,)).fetchall()
                finally:
                    c.close()
            except Exception:
                run, cps = None, []
            if run is None:
                yield "event: error\ndata: {\"message\":\"运行不存在\"}\n\n"
                return
            new_cps = [dict(s) for s in cps if s["seq"] > last_seq]
            if new_cps:
                last_seq = max(s["seq"] for s in cps)
                yield f"event: step\ndata: {json.dumps(new_cps, ensure_ascii=False)}\n\n"
            status = run["status"]
            if status in ("completed", "partial", "failed", "paused"):
                yield ("event: done\ndata: " + json.dumps(
                    {"status": status, "run_id": rid,
                     "total_latency_ms": run["total_latency_ms"] or 0,
                     "error_count": run["error_count"] or 0}, ensure_ascii=False) + "\n\n")
                return
            yield "event: ping\ndata: {}\n\n"
            time.sleep(1)
        yield "event: timeout\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/studio/hil-confirmations")
def list_hil_confirmations(status: str = "pending", limit: int = 50, conn=Depends(db_session)):
    from hil_service import HILService
    if status == "pending":
        return HILService.pending(conn, limit=min(limit, 200))
    rows = StudioRepo(conn).rows(
        "SELECT * FROM hil_confirmations WHERE status=? ORDER BY id DESC LIMIT ?", (status, min(limit, 200)))
    for r in rows:
        try:
            r["payload"] = json.loads(r.get("payload") or "{}")
        except Exception:
            r["payload"] = {}
    return rows


@router.post("/api/studio/hil-confirmations/{cid}/decide")
def decide_hil_confirmation(cid: int, body: dict | None = None, conn=Depends(db_session), user=Depends(current_user)):
    """人工决策：{approve: true/false, decided_by?}。

    2026-09-11 人在回路闭环：approve 且为对话直发确认单（run_id=0）时，
    按确认单 payload 自动执行写操作（HTTP 集成工具，如智源覆盖导入），执行结果回写审计。
    编排确认单（run_id>0）仍由编排执行方按 payload 消费，此处不重复执行。
    """
    from hil_service import HILService
    body = body or {}
    r = HILService.decide(conn, cid, bool(body.get("approve")), (body or {}).get("decided_by", "王工"))
    exec_note = ""
    if r.get("status") == "approved":
        row = HILService.get(conn, cid) or {}
        _run_id = int(row.get("run_id") or 0)
        _task_key = str(row.get("task_key") or "")
        if not _run_id and not _task_key:
            # 对话直发：批准即执行（仅 HTTP 集成工具支持自动执行）
            action = str(row.get("action") or "")
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:
                payload = row.get("payload") or {}
            trow = conn.execute("SELECT kind FROM tools WHERE name=? AND status='active'", (action,)).fetchone()
            if trow and trow["kind"] == "http":
                from http_tool_executor import exec_http_tool
                _er = exec_http_tool(action, payload if isinstance(payload, dict) else {})
                r["executed"] = True
                r["exec_result"] = {"ok": _er.get("ok"), "result": str(_er.get("result", ""))[:2000]}
                exec_note = f" · 已自动执行: {'成功' if _er.get('ok') else '失败'}"
            else:
                r["executed"] = False
                r["exec_result"] = {"ok": False, "result": f"工具「{action}」非 HTTP 集成类型，不支持批准后自动执行，请手动操作"}
                exec_note = " · 非 HTTP 工具未自动执行"
    audit(audit_user(user), "hil_decide", f"HIL确认单#{cid} → {r['status']}{exec_note}", conn=conn)
    return r


@router.get("/api/studio/planner/{rid}/tasks")
def planner_tasks(rid: int, conn=Depends(db_session)):
    from task_queue import TaskQueue
    tasks = TaskQueue.tasks(conn, rid)
    for t in tasks:
        try:
            t["deps"] = json.loads(t.get("deps") or "[]")
        except Exception:
            t["deps"] = []
        try:
            t["metadata"] = json.loads(t.get("metadata") or "{}")
        except Exception:
            t["metadata"] = {}
        try:
            t["config"] = json.loads(t.get("config") or "{}")
        except Exception:
            t["config"] = {}
    return tasks


@router.post("/api/studio/planner/tasks/{tid}/retry")
def planner_task_retry(tid: int, conn=Depends(db_session), user=Depends(current_user)):
    """P2 人工重试：blocked/failed 任务 → ready 重新执行。"""
    from task_queue import TaskQueue
    TaskQueue.retry(conn, tid)
    audit(audit_user(user), "task_retry", f"人工重试任务#{tid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/planner/tasks/{tid}/resolve")
def planner_task_resolve(tid: int, body: dict = None, conn=Depends(db_session), user=Depends(current_user)):
    """P2 人工确认完成：blocked 任务 → done，并解锁下游依赖。"""
    from task_queue import TaskQueue
    TaskQueue.resolve(conn, tid, (body or {}).get("note", ""))
    audit(audit_user(user), "task_resolve", f"人工确认任务#{tid}完成", conn=conn)
    return {"ok": True}


@router.get("/api/studio/monitor/usage")
def monitor_usage(days: int = 7, conn=Depends(db_session)):
    repo = StudioRepo(conn)
    rows = repo.rows(
        "SELECT * FROM llm_usage_stats WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1000",
        (f"-{days} days",))
    total_pt = sum(r.get("prompt_tokens") or 0 for r in rows)
    total_ct = sum(r.get("completion_tokens") or 0 for r in rows)
    total_cost = round(sum(r.get("estimated_cost") or 0 for r in rows), 6)
    real_n = sum(1 for r in rows if not r.get("used_mock"))
    mock_n = sum(1 for r in rows if r.get("used_mock"))
    by_provider: dict = {}
    for r in rows:
        key = f"{r.get('provider_name') or '-'} / {r.get('model_name') or '-'}"
        p = by_provider.setdefault(key, {"calls": 0, "tokens": 0, "cost": 0.0, "mock": 0})
        p["calls"] += 1
        p["tokens"] += (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
        p["cost"] += r.get("estimated_cost") or 0
        p["mock"] += 1 if r.get("used_mock") else 0
    by_intent: dict = {}
    for r in rows:
        k = r.get("intent") or "other"
        by_intent.setdefault(k, 0)
        by_intent[k] += 1
    return {
        "days": days, "total_calls": len(rows),
        "real_calls": real_n, "mock_calls": mock_n,
        "mock_rate": round(mock_n * 100 / len(rows), 1) if rows else 0,
        "prompt_tokens": total_pt, "completion_tokens": total_ct, "total_tokens": total_pt + total_ct,
        "estimated_cost": total_cost,
        "by_provider": [{"name": k, **v} for k, v in sorted(by_provider.items(), key=lambda x: -x[1]["tokens"])],
        "by_intent": by_intent,
    }


@router.get("/api/studio/flow-runs")
def list_flow_runs(flow_id: int | None = None, limit: int = 20, conn=Depends(db_session)):
    """最近编排运行记录（时间/流程/状态/耗时/节点数/错误数）。"""
    limit = max(1, min(limit, 100))
    repo = StudioRepo(conn)
    if flow_id:
        rows = repo.rows(
            "SELECT * FROM flow_runs WHERE flow_id=? ORDER BY id DESC LIMIT ?", (flow_id, limit))
    else:
        rows = repo.rows("SELECT * FROM flow_runs ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        try:
            r["node_count"] = len(json.loads(r.get("order_json") or "[]"))
        except Exception:
            r["node_count"] = 0
        r.pop("order_json", None)
    return rows


@router.get("/api/studio/flow-runs/{rid}")
def get_flow_run(rid: int, conn=Depends(db_session)):
    """单次运行详情：总信息 + 逐步结果（含 content/data/provider）。"""
    repo = StudioRepo(conn)
    run = repo.one("SELECT * FROM flow_runs WHERE id=?", (rid,))
    if not run:
        return JSONResponse({"error": "Not found"}, 404)
    try:
        run["order"] = json.loads(run.get("order_json") or "[]")
    except Exception:
        run["order"] = []
    run.pop("order_json", None)
    steps = repo.rows("SELECT * FROM flow_run_steps WHERE run_id=? ORDER BY seq", (rid,))
    for s in steps:
        try:
            s["data"] = json.loads(s.get("data") or "{}")
        except Exception:
            s["data"] = {}
    run["steps"] = steps
    return run


@router.get("/api/studio/flow-runs/{rid}/tree")
def get_flow_run_tree(rid: int, conn=Depends(db_session)):
    """单次运行 + 其全部子运行（递归）组成的层级树，供前端渲染拓扑。"""
    repo = StudioRepo(conn)

    def _node(run_id: int) -> dict:
        r = repo.one("SELECT * FROM flow_runs WHERE id=?", (run_id,))
        if not r:
            return {}
        try:
            order = json.loads(r.get("order_json") or "[]")
        except Exception:
            order = []
        kids = repo.rows("SELECT * FROM flow_runs WHERE parent_run_id=? ORDER BY id", (run_id,))
        return {
            "id": r["id"], "flow_name": r.get("flow_name") or f"run#{r['id']}",
            "flow_id": r.get("flow_id"), "status": r.get("status"),
            "node_count": len(order), "total_latency_ms": r.get("total_latency_ms") or 0,
            "created_at": r.get("created_at"), "parent_run_id": r.get("parent_run_id") or 0,
            "children": [_node(k["id"]) for k in kids],
        }

    return _node(rid)


@router.post("/api/studio/ai/generate-flow")
def ai_generate_flow(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """自然语言 → DAG 流程定义（与手动创建完全同构，校验后返回，前端确认导入）。"""
    from ai_copilot import FlowCopilot
    prompt = (body or {}).get("prompt", "")
    if not prompt:
        return JSONResponse({"error": "任务描述必填"}, 400)
    deep_thinking = bool((body or {}).get("deep_thinking", False))
    result = FlowCopilot(conn).generate(prompt, conn, deep_thinking=deep_thinking)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "生成失败")}, 400)
    audit(audit_user(user), "ai_generate_flow", f"AI 生成流程: {result.get('name')} ({result.get('source')}{'·深度思考' if deep_thinking else ''})", conn=conn)
    return result


@router.post("/api/studio/ai/refine-flow")
def ai_refine_flow(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """追加指令迭代修改已有流程定义（多轮对话：携带 conversation 历史）。"""
    from ai_copilot import FlowCopilot
    prompt = (body or {}).get("prompt", "")
    definition = (body or {}).get("definition") or {}
    if not prompt:
        return JSONResponse({"error": "迭代指令必填"}, 400)
    deep_thinking = bool((body or {}).get("deep_thinking", False))
    conversation = (body or {}).get("conversation") or []
    result = FlowCopilot(conn).refine(prompt, definition, conn,
                                      deep_thinking=deep_thinking, conversation=conversation)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "迭代失败")}, 400)
    audit(audit_user(user), "ai_refine_flow", f"AI 迭代流程: {prompt[:40]}", conn=conn)
    return result
