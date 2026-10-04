# -*- coding: utf-8 -*-
"""异步作业队列 API（2026-10-04，P0-C 最后一公里）。

## 端点
    GET  /api/jobs/kinds                可提交的业务作业类型 + 必填 payload 字段
    POST /api/jobs                提交作业（立即返回 job_id，不等待执行）
    GET  /api/jobs/{job_id}             查状态/进度/结果
    POST /api/jobs/{job_id}/cancel      取消（只对未开始或已失活的生效）
    GET  /api/jobs                     列表（按 kind/status 过滤，供监控页）
    GET  /api/jobs/stats                队列概览（各状态计数 + 最老在途年龄）

## 为什么单独开一组端点，而不是把 job_id 塞进原端点的返回体
原端点（上传/工程入库…）保留**同步语义**为默认，只在请求显式要求异步时返回
`{job_id, status:'queued'}`。理由：
  ① 前端既有调用方（static/js/mods/20-docs.js 等）零改动即可继续工作；
  ② "提交口"与"作业台"分离后，任何业务端点都能挂队列，不必每个都自带轮询端点；
  ③ 排查时"这个 job 到底是谁提的"只有一个答案（job_jobs 表 + payload 里的业务键）。

## 权限
读（查/列表/统计）走 `current_user`（登录即可见，符合既有只读端点口径）；
写（提交/取消）走 `config:write` 同档 —— 提交会**真的触发写库与 embedding 调用**，
匿名可提交等于把"跑入库"的算力对外开放。这与 routers/orchestration.py 的
resume 端点是同一档处理，理由相同。
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_any_permission
from core import config as _cfg

router = APIRouter(tags=["异步作业队列"])

# 作业提交会真的写库 + 调embedding → 与编排 resume 同档（写类，非只读）
JOB_WRITE_PERMS = [("config", "write"), ("kb_review", "modify"), ("kb_ontology", "edit")]

import json as _json


def _limit_default(default: int = 50, cap: int = 200) -> int:
    return max(1, min(int(default or 50), cap))


@router.get("/api/jobs/kinds")
def job_kinds(user=Depends(current_user)):
    """可提交的作业类型（前端"提交作业"面板据此渲染表单，避免硬编码 kind 字符串）。"""
    from core.job_handlers import describe
    return {"items": describe()}


@router.get("/api/jobs/stats")
def jobs_stats(conn=Depends(db_session), user=Depends(current_user)):
    """队列概览：按状态计数 + 最老在途作业的创建时间（判断是否积压）。"""
    from core import job_queue as jq
    stats = jq.queue_stats(conn)
    cfg = {"worker_enabled": _cfg.as_bool("jobs", "worker_enabled", True),
           "worker_count": int(_cfg.get("jobs", "worker_count", 1) or 1)}
    return {"stats": stats, "config": cfg,
            "note": "progress/stage 仅供展示；判定是否完成一律看 status"}


@router.get("/api/jobs")
def list_jobs(conn=Depends(db_session), user=Depends(current_user),
              kind: str = "", status: str = "",
              limit: int = Query(50, ge=1, le=200)):
    """作业列表（按 kind/status 过滤）。⚠️ 不返回 payload/result 全量，避免刷屏。"""
    where, args = [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if status:
        where.append("status=?")
        args.append(status)
    sql = ("SELECT id, job_key, kind, status, progress, stage, attempt, max_attempts, "
           "error, worker_id, created_at, updated_at FROM job_jobs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(_limit_default(limit, 200))
    rows = conn.execute(sql, args).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/jobs")
def submit_job(body: dict, conn=Depends(db_session),
               user=Depends(require_any_permission(JOB_WRITE_PERMS))):
    """提交一个作业，**立即返回** `{job_id, status, deduped}`。

    body = {kind, payload?, job_key?, max_attempts?, reuse_terminal?}

    ⚠️ `reuse_terminal` 默认 **False**：只有 queued/running 才去重，
    终态一律新建作业。理由见 core/job_queue.submit 的注释 ——
    幂等判据必须锚定「当次在途操作」，锚成「历史上做过」会把重试功能永久焊死。
    """
    from core import job_queue as jq
    from core.job_handlers import KINDS
    kind = (body or {}).get("kind") or ""
    if kind not in KINDS:
        return JSONResponse(
            {"error": "未知作业类型: %s" % (kind or "(空)"),
             "available": sorted(KINDS)}, 400)
    payload = (body or {}).get("payload") or {}
    if not isinstance(payload, dict):
        return JSONResponse({"error": "payload 必须是对象"}, 400)
    res = jq.submit(
        conn, kind, payload,
        job_key=(body or {}).get("job_key") or "",
        max_attempts=int((body or {}).get("max_attempts") or 3),
        reuse_terminal=bool((body or {}).get("reuse_terminal", False)))
    res["kind"] = kind
    if res.get("deduped"):
        res["hint"] = "同一业务键的在途作业已存在，未重复入队（如需重跑请等它结束或换 job_key）"
    else:
        res["poll"] = "/api/jobs/%d" % int(res.get("job_id") or 0)
    return res


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """查单个作业：状态 + 进度 + 结果（result JSON 反序列化，坏值降级为原文）。"""
    from core import job_queue as jq
    job = jq.get(conn, job_id)
    if not job:
        return JSONResponse({"error": "作业不存在", "job_id": job_id}, 404)
    out = {k: v for k, v in job.items() if k not in ("payload", "result")}
    out["payload"] = _safe_json(job.get("payload"), {})
    out["result"] = _safe_json(job.get("result"), {})
    out["terminal"] = (job.get("status") in jq.TERMINAL)
    out["lease_expired"] = jq.lease_expired(job) if job.get("status") == "running" else None
    out["cancelable"] = bool(job.get("status") == "queued"
                            or (job.get("status") == "running" and out["lease_expired"]))
    return out


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, conn=Depends(db_session),
               user=Depends(require_any_permission(JOB_WRITE_PERMS))):
    """取消作业。**只对未开始或租约已过期的生效** ——运行中的拒绝：
    强杀会留下半个副作用（候选表/三元组写一半），比取消失败更糟。"""
    from core import job_queue as jq
    res = jq.cancel(conn, job_id)
    if not res.get("ok"):
        return JSONResponse(res, 409)
    return res


def _safe_json(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return _json.loads(raw)
    except Exception:
        return {"raw": str(raw)[:4000]}