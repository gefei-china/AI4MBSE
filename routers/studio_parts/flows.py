# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：Agent 记忆 / HIL 确认 / 编排任务 / 用量监控。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。

★ 2026-11-05：**画布工作流（agent_flows / flow-runs / AI 生成流程）相关端点已全部移除**。
  画布链路属早期方案、已明确舍弃（`workflows/nodes.py` 曾自陈「已不再发展」；前端 0 处调用）。
  本文件因此只剩四组与画布无关的能力：
    · `/api/studio/agents/{aid}/memory` —— Agent 长期记忆读写（主链路记忆沉淀的运维面）
    · `/api/studio/hil-confirmations`    —— 人在回路确认单（写操作的唯一闸门，含 config_apply 晋升）
    · `/api/studio/planner/...`          —— 编排子任务重试/裁决（主链路编排的运维面）
    · `/api/studio/monitor/usage`        —— LLM 用量聚合（观测面）
  ⚠️ 编排引擎本体（`workflows/planner.py` / `refine.py` / `nodes.py` / `engine.py::run_planner_plan`）
     属于**主链路**，未随画布移除 —— 详见 docs/画布工作流移除-影响评估-20261105.md。
"""
from routers.studio_parts.shared import *



@router.get("/api/studio/agents/{aid}/memory")
def get_agent_memory(aid: str, conn=Depends(db_session)):
    """该 Agent 的长期记忆（agent_memory）。"""
    rows = StudioRepo(conn).rows(
        "SELECT id, mem_type, content, created_at FROM agent_memory WHERE agent_id=? ORDER BY id DESC LIMIT 50",
        (aid,))
    return rows


@router.post("/api/studio/agents/{aid}/memory")
def add_agent_memory(aid: str, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """写入该 Agent 的长期记忆（手动沉淀：{content, mem_type?, project_id?}）。

    ★ 2026-11-05（S2）：不再写 `scope_type=''`（存量槽）。
      带 project_id → ('project', pid)；不带 → ('agent', aid) 显式共享层。
      召回面等价性：读取侧 `scopes=None` 路径按 `agent_id=?` 取，槽位不影响可见性；
      带 scopes 时 ('agent', aid) 槽在 intent==aid 时命中 —— 与旧兜底要求 agent_id 相等同效。
      唯一变化：不再被**别的项目上下文**误召回（这正是本改造目的）。
    """
    content = (body or {}).get("content", "").strip()
    if not content:
        return JSONResponse({"error": "content 必填"}, 400)
    mem_type = (body or {}).get("mem_type", "fact")
    _pid = str((body or {}).get("project_id") or "").strip()
    _st, _sid = ("project", _pid[:120]) if _pid else ("agent", str(aid)[:120])
    cur = conn.execute(
        "INSERT INTO agent_memory (agent_id, mem_type, content, scope_type, scope_id) "
        "VALUES (?,?,?,?,?)",
        (aid, mem_type, content[:1000], _st, _sid))
    audit(audit_user(user), "agent_memory_write",
          f"写入记忆 {aid}[{_st}:{_sid}]: {content[:30]}", conn=conn)
    return {"ok": True, "id": cur.lastrowid}



@router.get("/api/studio/hil-confirmations")
def list_hil_confirmations(status: str = "pending", limit: int = 50,
                            conn=Depends(db_session),
                            _u=Depends(require_permission("hil", "view"))):
    """P0-6（2026-10-06）：此前**无任何权限门** —— 任何登录用户都能列出全部待批写操作
    （含 payload 里的完整参数）。确认单是写操作的唯一闸门，能看=能批，必须成对管控。
    """
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
def decide_hil_confirmation(cid: int, body: dict | None = None, conn=Depends(db_session),
                            user=Depends(current_user),
                            _u=Depends(require_permission("hil", "approve"))):
    """人工决策：{approve: true/false, decided_by?}。

    2026-09-11 人在回路闭环：approve 且为对话直发确认单（run_id=0）时，
    按确认单 payload 自动执行写操作（HTTP 集成工具，如智源覆盖导入），执行结果回写审计。
    编排确认单（run_id>0）仍由编排执行方按 payload 消费，此处不重复执行。

    P0-6（2026-10-06）两处整改（依据《Agent 生产化 Harness 对照核查》§3.2/§6.2）：
    ① 补 `hil:approve` 权限门——**批准即触发写操作自动执行**，
       此前无门⇒ 任何登录用户可批准任意确认单，等于绕过全部 HIL 意图。
    ② `decided_by` 原硬编码默认值 `"王工"`，且 body 可任意伪造 ⇒
       审计里的「谁批准的」既不真实也不可追责。
       现改为：**优先取已认证用户身份**（display_name → username），
       body 里的 decided_by 仅在**匿名兼容期**兜底。
    """
    from hil_service import HILService
    from core import config_promotion as _promo
    body = body or {}
    # P0-6②：审计归属以**已认证身份**为准，不采信 body 自报。
    _decider = ""
    if user:
        _decider = (user.get("display_name") or user.get("username") or "").strip()
    if not _decider:
        _decider = str(body.get("decided_by") or "")
    # ★ 2026-10-28（写前确认 HITL）：乐观锁 `expected_version` ——
    #   确认单挂起期间平台坐标可能已变（versionId 变了 / 父容器被删），
    #   批准时必须比对，不一致则**不执行**、要求模型重新定位（否则会写到错的地方）。
    r = HILService.decide(conn, cid, bool(body.get("approve")), _decider,
                           expected_version=str(body.get("expected_version") or ""))
    exec_note = ""
    if r.get("status") == "version_mismatch":
        return JSONResponse({
            "error": "确认单已过期：平台坐标与批准时的期望不一致（期望 %s，实际 %s）。"
                     "请重新定位后再试，本次**未执行**。"
                     % (r.get("expected_version"), r.get("payload_version")),
            "status": "version_mismatch", "executed": False}, 409)
    if r.get("status") == "approved":
        row = HILService.get(conn, cid) or {}
        _run_id = int(row.get("run_id") or 0)
        _task_key = str(row.get("task_key") or "")
        action = str(row.get("action") or "")
        # P1-4：配置晋升/回滚确认单——批准即执行（全仓唯一经人批准写配置的入口，
        #   经 core.config.save_override 落 config_history 台账）。带 task_key
        #   （proposal:N / history:N）溯源，故不能只靠"对话直发"判据放行。
        _is_cfg_action = action in (_promo.HIL_ACTION_APPLY, _promo.HIL_ACTION_ROLLBACK)
        if _is_cfg_action or (not _run_id and not _task_key):
            try:
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    payload = json.loads(row.get("payload") or "{}")
            except Exception:
                payload = row.get("payload") or {}
            if _is_cfg_action:
                try:
                    er = _promo.execute_confirmation(conn, row)
                    r["executed"] = bool(er.get("executed"))
                    r["exec_result"] = er
                    if er.get("executed"):
                        exec_note = (" · 配置已变更: %s.%s %s → %s（台账 #%s）"
                                     % (er.get("section", ""), er.get("key", ""),
                                        er.get("old", ""), er.get("new", ""),
                                        er.get("history_id", "")))
                    else:
                        exec_note = " · 配置无变化（%s）" % er.get("reason", "")
                except Exception as e:  # noqa: BLE001
                    r["executed"] = False
                    r["exec_result"] = {"ok": False, "error": str(e)[:300]}
                    exec_note = " · 配置执行失败: %s" % str(e)[:80]
            else:
                _hil_is_http = False
                trow = conn.execute("SELECT kind FROM tools WHERE name=? AND status='active'", (action,)).fetchone()
                if trow and trow["kind"] == "http":
                    _hil_is_http = True
                    from http_tool_executor import exec_http_tool
                    _er = exec_http_tool(action, payload if isinstance(payload, dict) else {})
                    r["executed"] = True
                    r["exec_result"] = {"ok": _er.get("ok"), "result": str(_er.get("result", ""))[:2000]}
                    exec_note = f" · 已自动执行: {'成功' if _er.get('ok') else '失败'}"
                elif action == "save_artifact":
                    # ★ 2026-10-17（米爸能力配置）：本地工具 save_artifact 的批准即执行——
                    #   确认单 payload 就是工具参数（title/content/kind/lang），
                    #   会话上下文取确认单归属会话。此前本地工具批准后死在队列
                    #   （"非 HTTP 集成类型不支持自动执行"），冒烟实测（确认单 #20）。
                    import artifact_tools as _at_hil
                    _er = _at_hil.exec_tool(action, payload if isinstance(payload, dict) else {},
                                            conversation_id=int(row.get("conversation_id") or 0))
                    r["executed"] = True
                    r["exec_result"] = {"ok": _er.get("ok"), "result": str(_er.get("result", ""))[:2000]}
                    exec_note = f" · 已自动执行: {'成功' if _er.get('ok') else '失败'}"
                elif action == "eval_label":
                    # P0-b：在线评测人工标注——批准 ⇒ labeled_confirmed（后续
                    #   promote 才转正）；驳回 ⇒ labeled_rejected（B3 不进 dataset）。
                    #   执行体在 core.online_eval.apply_human_label（与 monitor
                    #   的 label 端点双入口共用，单一事实源）。
                    from core import online_eval as _oe
                    _lr = _oe.apply_human_label(
                        conn, int(payload.get("queue_id") or 0),
                        approve=True,
                        score=int(payload.get("score") or 0),
                        reason=str(payload.get("reason") or ""),
                        actor=_decider)
                    r["executed"] = bool(_lr.get("ok"))
                    r["exec_result"] = _lr
                    exec_note = (" · 标注已生效（%s）" % _lr.get("status", "")
                                 if _lr.get("ok") else
                                 " · 标注执行失败: %s" % _lr.get("error", ""))
                else:
                    # ★ 2026-10-28（写前确认 HITL）：**统一执行器** ——
                    #   旧逻辑在此直接放弃："非 HTTP 集成类型，不支持批准后自动执行"，
                    #   ⇒ **MCP 写操作（智源的全部建模写动作）批准了也不执行**（实测断链）。
                    #   现在统一走 `AgentPipeline._exec_tool_call` —— 与模型调用工具**同一份代码**
                    #   ⇒ MCP / HTTP / local 三类天然全覆盖，权限闸/Hook/寻址链全部复用。
                    try:
                        from hil_service import execute_confirmation as _exec_conf
                        _er = _exec_conf(conn, row, user=user)
                        r["executed"] = bool(_er.get("executed"))
                        r["exec_result"] = _er
                        exec_note = (" · 已自动执行: %s" % ("成功" if _er.get("ok") else "失败"))
                    except Exception as e:  # noqa: BLE001
                        r["executed"] = False
                        r["exec_result"] = {"ok": False, "error": str(e)[:300]}
                        exec_note = " · 执行失败: %s" % str(e)[:80]
        # P0-a：HIL 批准即执行落 tool_call_logs。此前该路径**只写 audit_logs**
        # ⇒「经人批准才发生的工具执行」在能力度量里完全不可见（exec_note 只在
        # 审计文本里）。capability_id=tool:{action}（两个可执行分支的前提都是
        # tools 表有该行；配置晋升类动作不是能力调用，不落）。
        if r.get("executed") and not _is_cfg_action:
            try:
                _er = r.get("exec_result") or {}
                # ★ P0-3（2026-10-27）：HIL 批准后的执行是**独立的 HTTP 请求**
                #   （用户点"批准"那一刻），本身没有对话轮次 ⇒ 用 request_id 作 trace。
                #   它仍保留 run_id/task_key 指向原编排批次，两条线互补而非互相替代。
                try:
                    from core.audit import current_trace_id as _ctid_hil
                    _tid_hil = _ctid_hil(fallback_request=True)
                except Exception:
                    _tid_hil = ""
                # S0（2026-10-28）探针标记（core/probe_mark）
                try:
                    from core.probe_mark import is_probe_tool as _ipt_hil
                    _probe_hil = _ipt_hil(action)
                except Exception:
                    _probe_hil = 0
                conn.execute(
                    "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, call_kind, "
                    "arguments, result, ok, latency_ms, run_id, task_key, capability_id, trace_id, is_probe) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("hil_approve", "", action,
                     "builtin" if action == "save_artifact" else ("http" if _hil_is_http else ""),
                     "tool", "{}", str(_er.get("result", ""))[:2000],
                     1 if _er.get("ok") else 0, 0, _run_id, _task_key, "tool:%s" % action, _tid_hil,
                     # S0（2026-10-28）探针标记（core/probe_mark）
                     _probe_hil))
            except Exception:
                pass
    # P0-b：eval_label 的**驳回**同样要落状态（labeled_rejected，B3：永不进
    # dataset，但留痕可复查"哪条被谁驳回"）。approved 的执行分支在上面。
    if r.get("status") == "rejected":
        _row_r = HILService.get(conn, cid) or {}
        if str(_row_r.get("action") or "") == "eval_label":
            try:
                _pl_r = _row_r.get("payload")
                if not isinstance(_pl_r, dict):
                    _pl_r = json.loads(_row_r or "{}")
            except Exception:
                _pl_r = {}
            from core import online_eval as _oe_r
            _lr = _oe_r.apply_human_label(
                conn, int(_pl_r.get("queue_id") or 0), approve=False,
                score=int(_pl_r.get("score") or 0),
                reason=str(_pl_r.get("reason") or ""), actor=_decider)
            r["executed"] = bool(_lr.get("ok"))
            r["exec_result"] = _lr
    audit(audit_user(user), "hil_decide", f"HIL确认单#{cid} → {r['status']}{exec_note}", conn=conn)
    # ★ 2026-10-28（写前确认 HITL·D-2）：生成**回灌文案** ——
    #   批准执行发生在**原 ReAct 循环之外**（一次独立 HTTP 请求）⇒
    #   **LLM 永远收不到"工具已执行"的回执** ⇒ 它会重发同一操作、或宣称"尚未执行"。
    #   （真机实测 E8：回执 `SUCCEEDED`、平台独立回读证实已建成，
    #     模型却断言「尚未写入平台……因轮次用尽，写操作没有执行」—— **与事实相反**。）
    #   ⇒ 把真实执行结果显式回灌，模型才有事实依据。
    #   前端拿到 `resume_text` 后**重新起一轮** stream（与 `clarify-answer` 同构）。
    try:
        _row_final = HILService.get(conn, cid) or {}
        _act_final = str(_row_final.get("action") or "")
        _conv_final = int(_row_final.get("conversation_id") or 0)
        _orig_input = ""
        if _conv_final:
            try:
                # ⚠️ `fetchone()` 返回的是 **sqlite3.Row**（或 tuple），**没有 `.get()`** ——
                #   2026-10-28 真机 E2E 踩到：写成 `(_u or {}).get("content")` 会抛
                #   `AttributeError`，被下面的 `except` **静默吞掉** ⇒
                #   回灌文案里"原请求复述"**永远为空**（症状：模型丢失上下文，但看不出原因）。
                #   ⇒ 用**位置索引**（本 SELECT 只有 content 一列），两种行类型都支持。
                _u = conn.execute(
                    "SELECT content FROM messages WHERE conversation_id=? AND role='user' "
                    "ORDER BY id DESC LIMIT 1", (_conv_final,)).fetchone()
                _orig_input = str(_u[0] or "") if _u else ""
            except Exception as _oe:
                _orig_input = ""
                print("[hil] ⚠️ 取原请求失败（回灌文案将缺上下文）: %s: %s"
                      % (type(_oe).__name__, str(_oe)[:150]))
        from hil_service import build_resume_text as _brt
        if r.get("status") == "approved":
            r["resume_text"] = _brt(_act_final, r.get("exec_result") or {}, True,
                                    original_input=_orig_input)
        elif r.get("status") == "rejected":
            r["resume_text"] = _brt(_act_final, {}, False,
                                    reason=str(body.get("note") or body.get("reason") or ""),
                                    original_input=_orig_input)
        r["conversation_id"] = _conv_final
    except Exception:
        pass
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


