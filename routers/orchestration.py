# -*- coding: utf-8 -*-
"""编排持久执行 API（2026-10-03，P0-2 第二步）：检查点查询 + 断点续跑。

## 端点
    GET  /api/orchestration/resumable                 可恢复的编排批次清单
    GET  /api/orchestration/runs/{run_id}             单个批次的检查点 + 任务快照
    POST /api/orchestration/runs/{run_id}/resume      断点续跑（SSE，事件协议同 /chat/stream）

## 为什么 resume 必须走**直达通道**而不是重发消息
「再发一遍用户描述」看着等价，实际不是同一件事：
  ① 会重新跑一遍意图识别与复杂度判定（启发式，结论可能不同 → 走单 Agent 直行）；
  ② 会再调一次 planner LLM，得到**不同的任务拆分**，旧任务的结果无法按 key 复用；
  ③ 可能触发内容级澄清，把执行拦回去。
所以 resume 的唯一依据是 `run_id` + 检查点，见 `agent/pipeline_parts/stream.py` 的直达分支。

## 三道闸门（缺一不可）
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


@router.get("/api/orchestration/resumable")
def list_resumable(conn=Depends(db_session),
                   conversation_id: int = 0,
                   limit: int = Query(10, ge=1, le=50)):
    """列出可恢复的编排批次（供前端在会话顶部显示"上次编排中断，是否继续"）。"""
    from agent.orch_checkpoint import resumable
    return {"items": resumable(conn, conversation_id=conversation_id, limit=limit,
                               stale_s=_stale_default(conn)),
            "stale_s": _stale_default(conn)}


@router.get("/api/orchestration/runs/{run_id}")
def get_run(run_id: int, conn=Depends(db_session)):
    """单个编排批次：检查点 + 任务快照 + 是否可恢复（含不可恢复的原因）。"""
    from agent.orch_checkpoint import load, prepare_resume, stale_seconds, TERMINAL_PHASES
    ck = load(conn, run_id)
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
@router.post("/api/orchestration/runs/{run_id}/resume")
def resume_run(run_id: int, conn=Depends(db_session),
               include_failed: bool = False, user=Depends(current_user)):
    """断点续跑：只补跑未完成子任务，已完成的结果直接复用（SSE 事件协议与 /chat/stream 同构）。

    include_failed=true 时连 `failed` 任务一并重跑 —— **仅在确认无副作用或已清理后使用**。
    """
    import json as _json
    from starlette.responses import StreamingResponse
    from agent.orch_checkpoint import prepare_resume, apply_resume, touch
    # ── 权限门（与其他写端点的**行内调用**写法保持一致）──────────────────────────
    # 为什么这里必须有门：resume 会真实触发 LLM 调用与写库/写图谱，匿名可调用等于把
    # 编排重跑能力对外开放。这与遥测类端点（UX 埋点刻意不加门）的处理恰好相反。
    _err = require_permission("config", "write")(user=user)
    if _err:
        return JSONResponse({"error": str(_err.detail) if hasattr(_err, "detail") else "无权限"}, 403)

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
