# -*- coding: utf-8 -*-
"""编排持久执行 API（2026-10-03，P0-2 第二步）：检查点查询 + 断点续跑。

## 端点
    GET  /api/orchestration/resumable                 可恢复的编排批次清单
    GET  /api/orchestration/runs/{run_id}             单个批次的检查点 + 任务快照
    GET  /api/orchestration/runs/{run_id}/trace       观测性 trace：span 树 + per-node 成本聚合
    POST /api/orchestration/runs/{run_id}/resume      断点续跑（SSE，事件协议同 /chat/stream）
    POST /api/orchestration/runs/{run_id}/rerun       单点重跑（指定 task_key + 传递下游清结果，再走 resume）

## 为什么 resume 必须走**直达通道**而不是重发消息
「再发一遍用户描述」看着等价，实际不是同一件事：
  ① 会重新跑一遍意图识别与复杂度判定（启发式，结论可能不同 → 走单 Agent 直行）；
  ② 会再调一次 planner LLM，得到**不同的任务拆分**，旧任务的结果无法按 key 复用；
  ③ 可能触发内容级澄清，把执行拦回去。
所以 resume 的唯一依据是 `run_id` + 检查点，见 `agent/pipeline_parts/stream.py` 的直达分支。

## 四道闸门（缺一不可）
 ⓪ **归属闸门**（★ B5-5，2026-10-14 补）：批次经`conversation_id` 挂在会话上，
     读/写都必须先校验该会话归属（复用 `conv_visible_to` 单一事实源）。
     此前三个端点**一个都没有**；而 resume 上那个恒 403 的错权限域恰好把洞挡住了 ——
     放宽权限若不同步补这道门，等于当场打开越权续跑/读取他人会话的洞。
  ① **存活闸门**：心跳未超时说明还有活进程在跑，拒绝接管（否则两进程同时执行同一任务，
     写类工具会产生重复实体）。判定在 SQL 内完成（SQLite CURRENT_TIMESTAMP 是 UTC，
     Python datetime.now() 是本地时间，取回 Python 做差会凭空多 8 小时）。
  ② **次数闸门**：resume 次数上限 5 —— 防「恢复 → 再崩 → 再恢复」自愈死循环。
  ③ **幂等闸门**：已 `done` 的任务绝不重跑；`failed` 默认不自动重跑（可能副作用已落一半），
     需显式 `?include_failed=true`。
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_permission
from core import config as _cfg

router = APIRouter(tags=["编排持久执行"])


def _stale_default(conn) -> int:
    try:
        return int(_cfg.get("orchestration", "resume_stale_s", 1800) or 1800)
    except Exception:
        return 1800


def _own_run(conn, run_id: int, user) -> dict:
    """★ B5-5（2026-10-14）：按**会话归属**校验编排批次的读/写权限。

    ## 为什么必须加这道门（此前三个端点一个都没有）
    `orch_checkpoints` 行经`conversation_id` 挂在会话上，但本文件原先**从不校验该会话归属**：
    `get_run(run_id)` 只要猜到 run_id 就能读出 `user_input` 原文、检查点与全部任务标题；
    `resumable?conversation_id=<别人的>` 同理。⇒ 任何登录用户可枚举他人会话内容。

    ## 一个必须记住的教训：**此前的 403 恰好掩盖了这个洞**
    `resume` 端点挂着 `require_permission("config","write")`，而**三个角色的
    `permissions` 里都没有 `config` 域**（实测 role 80/81 的 config 键为 None）⇒
    所有登录用户点「继续编排」恒 403。安全上看似"门关着"，实际是
    **"门焊死了 + 旁边两个门敞着"**。若只把 `config:write` 放宽了事、不补归属校验，
    就等于当场把这个 IDOR 打开。⇒ **放宽权限与补归属校验必须同一处改**。

    口径与 `routers/conversations.py::_own_conv` 对齐（复用 `conv_visible_to` 单一事实源）：
    不可见即 404（不泄露"该 id 存在"）。
    """
    from fastapi import HTTPException
    from agent.orch_checkpoint import load as _ck_load
    from repositories.conversation_repo import conv_visible_to
    ck = _ck_load(conn, run_id)
    if not ck:
        raise HTTPException(status_code=404, detail="编排批次不存在")
    conv_id = int(ck.get("conversation_id") or 0)
    if conv_id:
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        row = dict(row) if row is not None else None
        if not conv_visible_to(row, user):
            raise HTTPException(status_code=404, detail="编排批次不存在")
    return ck


@router.get("/api/orchestration/resumable")
def list_resumable(conn=Depends(db_session), user=Depends(current_user),
                   conversation_id: int = 0,
                   limit: int = Query(10, ge=1, le=50)):
    """列出可恢复的编排批次（供前端在会话顶部显示"上次编排中断，是否继续"）。

    ★ B5-5：显式传 `conversation_id` 时**必须校验归属**（出参含 `user_input` 原文）。
    """
    from agent.orch_checkpoint import resumable
    if conversation_id:
        from fastapi import HTTPException
        from repositories.conversation_repo import conv_visible_to
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        row = dict(row) if row is not None else None
        if not conv_visible_to(row, user):
            raise HTTPException(status_code=404, detail="会话不存在")
    return {"items": resumable(conn, conversation_id=conversation_id, limit=limit,
                               stale_s=_stale_default(conn)),
            "stale_s": _stale_default(conn)}


@router.get("/api/orchestration/runs/{run_id}")
def get_run(run_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """单个编排批次：检查点 + 任务快照 + 是否可恢复（含不可恢复的原因）。"""
    from agent.orch_checkpoint import load, prepare_resume, stale_seconds, TERMINAL_PHASES
    ck = _own_run(conn, run_id, user)   # ★ B5-5：归属校验（此前完全没有）
    _ = load   # 保留原导入语义（下方仍直接用 ck）
    if not ck:
        return JSONResponse({"error": "检查点不存在", "run_id": run_id}, 404)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, task_key, title, agent_id, status, error, latency_ms, seq, deps "
            "FROM agent_tasks WHERE run_id=? ORDER BY seq, id", (run_id,)).fetchall()]
    except Exception:
        rows = []
    dec = prepare_resume(conn, run_id, stale_s=_stale_default(conn))
    return {"checkpoint": {k: v for k, v in ck.items() if k not in ("plan",)},
            "phase_terminal": (ck.get("phase") or "") in TERMINAL_PHASES,
            "stale_s": round(stale_seconds(conn, run_id), 1),
            "tasks": rows,
            "resumable": bool(dec.get("ok")), "resume_block_reason": dec.get("reason"),
            "resume_mode": dec.get("mode") or "", "resume_detail": dec.get("detail", ""),
            "requeue_ids": dec.get("requeue_ids") or [], "reuse_ids": dec.get("reuse_ids") or []}


# NOTE: 写端点为何要权限门 —— 与 ux_metrics 相反：resume 会真实触发 LLM 调用与**写库/写图谱**，
#       匿名可调用等于把编排的重跑能力对外开放。故走 `config:write` 同档 discriminator。
#
# ── 2026-10-20（Task #43）：观测性 trace ──────────────────────────────────────────
# 为什么单独一个 GET：编排排障的核心问题是「55s 花在哪、每个节点成本多少」——
#   orch_stream_events 是事件流（无父子结构、无起止对，算不出 latency），
#   agent_tasks 只有子任务维度（没有 plan/summary/reflection/views）。
#   span 树（services/orch_span.py 落库）正是补这一格的：开/关时刻对 + meta 成本。
# 读端点与 get_run 同档：**归属门必须先过**（span 的 name/meta 含 task_key 与错误详情，
#   等同批次内容的一部分）；无 span 的老批次返回 200 + 空树 + note（不是错误——
#   "没有 trace"只说明该批次产生于本功能上线之前）。
@router.get("/api/orchestration/runs/{run_id}/trace")
def get_run_trace(run_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """单个编排批次的 span 树 + per-node 成本聚合（只读）。"""
    _own_run(conn, run_id, user)   # ★ B5-5：归属校验（与 get_run/resume/rerun 同档）
    from services.orch_span import run_trace
    return run_trace(conn, run_id)


@router.post("/api/orchestration/runs/{run_id}/rerun")
def rerun_tasks(run_id: int, body: dict = None, conn=Depends(db_session),
                user=Depends(current_user)):
    """单点重跑（2026-10-20，对标 LangGraph 局部重放的最小版）。

    语义：指定 task_key（如 t3）⇒ 该任务**连同其全部传递下游**（deps 依赖闭包）
    清除结果与状态，随后调用方走既有 resume——resume 会把非 done 的任务重排队、
    done 的幂等复用，从而实现「只重跑出问题的那一段，其余复用」。

    为什么下游必须联动：下游交付物基于**旧上游结果**生成；只清单点会让下游
    复用一份基于过期输入的结果——那不是重跑，是数据造假。

    终态检查点（phase=done/failed）会被回退为 executing 并重置 attempt——
    重跑是用户显式动作，应当获得全新的恢复额度。

    ⚠️ 本端点**只清状态不触发执行**——执行统一走 resume 的直达通道
    （重发消息会重新规划，task_key 对不上，见文件头「为什么 resume 必须走直达通道」）。
    """
    import json as _json
    from agent.orch_checkpoint import load as _ck_load, PHASE_EXECUTING, TERMINAL_PHASES
    _own_run(conn, run_id, user)
    require_permission("ai_chat", "generate")(user=user)

    body = body or {}
    wanted = [str(k).strip() for k in (body.get("task_keys") or []) if str(k).strip()]
    if not wanted:
        return JSONResponse({"error": "task_keys 不能为空（指定要重跑的 task_key，如 [\"t3\"]）"}, 400)
    include_downstream = bool(body.get("include_downstream", True))

    rows = [dict(r) for r in conn.execute(
        "SELECT id, task_key, deps, status FROM agent_tasks WHERE run_id=? ORDER BY seq, id",
        (int(run_id),)).fetchall()]
    if not rows:
        return JSONResponse({"error": "该批次没有任务记录"}, 404)
    by_key = {str(r["task_key"]): r for r in rows}

    missing = [k for k in wanted if k not in by_key]
    if missing:
        return JSONResponse({"error": f"task_key 不存在：{','.join(missing)}"}, 404)

    # 传递下游闭包：deps 里引用了重跑集合成员的任务，一并重跑（BFS）
    rerun_keys = set(wanted)
    if include_downstream:
        changed = True
        while changed:
            changed = False
            for r in rows:
                k = str(r["task_key"])
                if k in rerun_keys:
                    continue
                try:
                    deps = _json.loads(r.get("deps") or "[]")
                except Exception:
                    deps = []
                if any(str(d) in rerun_keys for d in deps):
                    rerun_keys.add(k)
                    changed = True

    # 清结果与状态（status='ready' ∈ REQUEUE_STATUSES ⇒ resume 必然重排队）
    cleared = 0
    for r in rows:
        if str(r["task_key"]) in rerun_keys:
            conn.execute(
                "UPDATE agent_tasks SET status='ready', result='', summary_json=NULL, "
                "error='', updated_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
            cleared += 1
    # 终态检查点回退：done/failed ⇒ executing，attempt 清零（用户显式动作给全新额度）
    ck_reset = False
    ck = _ck_load(conn, int(run_id))
    if ck and (ck.get("phase") or "") in TERMINAL_PHASES:
        conn.execute(
            "UPDATE orch_checkpoints SET phase=?, attempt_count=0, updated_at=CURRENT_TIMESTAMP "
            "WHERE run_id=?", (PHASE_EXECUTING, int(run_id)))
        ck_reset = True
    conn.commit()
    from core.audit import audit, audit_user
    audit(audit_user(user), "orchestration_rerun",
          "批次 #%s 单点重跑：%s（含下游联动=%s，清 %d 个任务）"
          % (run_id, ",".join(sorted(rerun_keys)), include_downstream, cleared), conn=conn)
    return {"ok": True, "run_id": run_id,
            "rerun_keys": sorted(rerun_keys),
            "cleared": cleared,
            "downstream_added": sorted(rerun_keys - set(wanted)),
            "checkpoint_reset": ck_reset,
            "next": "调用 POST /api/orchestration/runs/{run_id}/resume 执行重跑"}


@router.post("/api/orchestration/runs/{run_id}/resume")
def resume_run(run_id: int, conn=Depends(db_session),
               include_failed: bool = False, user=Depends(current_user)):
    """断点续跑：只补跑未完成子任务，已完成的结果直接复用（SSE 事件协议与 /chat/stream 同构）。

    include_failed=true 时连 `failed` 任务一并重跑 —— **仅在确认无副作用或已清理后使用**。
    """
    import json as _json
    from starlette.responses import StreamingResponse
    from agent.orch_checkpoint import prepare_resume, apply_resume, touch
    # ── 归属门（★ B5-5，必须**先于**权限门）────────────────────────────────────
    #   权限域从 `config:write`（无人持有）改为 `ai_chat:generate`（设计师/知识工程师均持有）
    #   之后，若不同步补上归属校验，就等于**当场打开了越权续跑他人会话**的洞
    #   （原先那个恒 403 的错域，恰好把这个洞挡住了）。
    #   ⇒ 权限门放宽与归属门补齐必须成对出现，缺一即等于埋洞。
    _own_run(conn, run_id, user)
    # ── 权限门（与其他写端点的**行内调用**写法保持一致）──────────────────────────
    # 为什么这里必须有门：resume 会真实触发 LLM 调用与写库/写图谱，匿名可调用等于把
    # 编排重跑能力对外开放。这与遥测类端点（UX 埋点刻意不加门）的处理恰好相反。
    #
    # ★ B5-5 **门必须选目标用户角色真实持有的域**：原实现挂 `config:write`，而本仓三个
    #   种子角色的 `permissions` 里**根本没有 `config` 键**（实测 role 80/81 的
    #   `p.get("config") is None`）⇒ 判定恒走"domain 不存在"分支 ⇒
    #   **任何登录用户点「继续编排」恒 403**；前端又只弹一个 toast 就移除提示条
    #   ⇒ 用户侧表现为「点了没反应、后续流程一点不走」（米爸 2026-10-14 报的正是这个）。
    #   正确域：`ai_chat:generate` —— 续跑的本质就是"继续这次 AI 对话"，
    #   与 `/chat/stream`（本身不挂额外门、只靠 `_own_conv` 归属隔离）同档。
    #
    # ⚠️⚠️ **不要写成 `_err = require_permission(...)(user=user); if _err:`**——
    #   `require_permission` 的 checker 是**失败抛 HTTPException、成功 return user**，
    #   成功时返回的是**用户字典（恒为真值）** ⇒ 那个 `if _err` **永远为真**
    #   ⇒ **改对权限域之后仍然是 403**（实测：域换成 ai_chat:generate 后，
    #   归属者调用依旧返回 `{"error":"无权限"}`）。
    #   ⇒ 本仓其余 7 处行内调用（`routers/branches.py`）都是**直接调用、不接返回值**，
    #      这里必须与它们一致；靠异常表达失败，交给 FastAPI 异常处理器转 403。
    require_permission("ai_chat", "generate")(user=user)

    stale = _stale_default(conn)
    dec = prepare_resume(conn, run_id, include_failed=include_failed, stale_s=stale)
    _REASON_CN = {
        "no_checkpoint": "该批次没有检查点记录（可能产生于本功能上线之前）",
        "still_alive": "该批次仍在运行中（心跳未超时），为避免重复执行已拒绝恢复",
        "attempt_exhausted": "该批次已重试过上限次数，不再自动恢复（请重新发起编排）",
        "no_tasks": "该批次没有任务记录，无法恢复",
        "nothing_to_do": "没有需要重跑的任务（全部已完成，或剩余任务按策略跳过）",
    }
    if not dec.get("ok"):
        reason = str(dec.get("reason") or "")
        msg = _REASON_CN.get(reason.split(":")[0], reason or "不可恢复")
        return JSONResponse({"error": msg, "run_id": run_id, "reason": reason,
                             "reuse_ids": dec.get("reuse_ids") or []}, 409)

    ck = dec.get("checkpoint") or {}
    conv_id = int(ck.get("conversation_id") or 0)
    mode = dec.get("mode") or "tasks"
    if mode == "tasks":
        applied = apply_resume(conn, dec)
        if not applied:
            return JSONResponse({"error": "任务重排队失败，未触发执行", "run_id": run_id}, 500)
    else:
        # C-1 只重做汇总：子任务全 done，**一行任务都不动** —— 这正是省下整轮任务 LLM 费用之处
        applied = 0
    touch(conn, run_id, "executing")

    from agent import AgentPipeline

    def event_stream():
        yield "retry: 3000\n\n"
        yield ": connected\n\n"
        _client_gone = False
        _frames = None
        try:
            from core.sse import iter_with_heartbeat, snapshot as _snap, degrade_delta as _delta
            from llm import llm_client as _singleton
            _base = _snap(getattr(_singleton, "stats", {}) or {})
            agent = AgentPipeline()
            _frames = iter_with_heartbeat(
                agent.execute_stream("", conv_id, ck.get("branch") or "dev", None, [],
                                     resume_run_id=int(run_id),
                                     scope=None, user=None),
                # P1-2：15 s 心跳 —— 汇总重做也要几十秒，期间必须有帧否则网关会掐连接
                interval_s=15.0,
            )
            for raw in _frames:
                yield raw
            if not _client_gone:
                d = _delta(_base, _snap(getattr(_singleton, "stats", {}) or {}))
                if d.get("mock_calls") or d.get("fallback_calls"):
                    yield ("event: degraded\ndata: %s\n\n"
                           % _json.dumps({"reason": "resume", "detail": d}, ensure_ascii=False))
        except GeneratorExit:
            _client_gone = True
            try:
                if _frames is not None:
                    _frames.close()
            except Exception:
                pass
            raise
        except Exception as _e:
            if not _client_gone:
                yield f"event: error\ndata: {_json.dumps({'message': str(_e)[:200]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
