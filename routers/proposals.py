# -*- coding: utf-8 -*-
"""P1-3 配置提案 API：失败轨迹 → 修订提议（只提议，不自动应用）。

## 产品口径（2026-10-18 自进化调研路线图 P1-3）
RefineGate 从「内容评审」泛化为「配置评审」：读 P1-2 归因器聚出的首坏步
模式，按规则注册表提议 prompt/skill/配置修订（GEPA 式反思改写）。

**铁律：只提议，不自动应用。** 自动改生产配置是 misevolution（进化成错）的
直接通道——每个提案必须经人裁决（applied/rejected 留痕），"应用"是人在设置页
改配置（现有配置 API）。本路由与引擎、repo 三层合起来也没有任何写配置的路径。

## P1-4 增量（2026-10-19）：沙箱 A/B + HIL 晋升
配置键类提案多一条**安全通道**：先影子评估（对照组 vs 实验组各跑 CI 门禁集，
差分判回归，评估期间零生产副作用）⇒ verdict=pass 才可发起晋升 ⇒ 进
hil_confirmations ⇒ 人批准后才经 core/config_promotion 真改配置并落
config_history 台账（可回滚，回滚同样过人）。非配置键目标（instrumentation/
skill）不走该通道：人工修订后 decide=applied 留痕即可。

端点：
    GET    /api/proposals                     提案队列（status 过滤 + 统计）
    POST   /api/proposals/generate            跑规则（读失败轨迹）→ 新提案入队
    POST   /api/proposals/{id}/decide         裁决（applied/rejected + 备注，留痕）
    POST   /api/proposals/{id}/sandbox        发起沙箱 A/B 评估（异步作业）
    GET    /api/proposals/{id}/sandbox-runs   该提案的评估历史
    GET    /api/sandbox/runs/{run_id}         单次评估详情（轮询用）
    POST   /api/proposals/{id}/promote        发起晋升（须沙箱 pass ⇒ 入 HIL 队列）
"""
from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from core.audit import audit, audit_user
from core.deps import current_user, db_session

router = APIRouter(tags=["配置提案"])


@router.get("/api/proposals")
def list_proposals(status: str = "", limit: int = 100, conn=Depends(db_session)):
    """提案队列（status 空 = 全部）。含分状态计数，供看板徽标。"""
    from core import config_proposal as _cp
    from repositories.proposal_repo import ProposalRepo
    repo = ProposalRepo(conn)
    return {"ok": True,
            "proposals": _cp.list_proposals(conn, status=status, limit=limit),
            "counts": repo.count_by_status(),
            "statuses": list(_cp.PROPOSAL_STATUSES)}


@router.post("/api/proposals/generate")
def generate_proposals(conn=Depends(db_session), user=Depends(current_user)):
    """跑规则：读当前窗口的首坏步模式 → 提议修订 → 入队。

    返回 {created, skipped, proposals, patterns_summary}：
    · created = 本次新入队；skipped = 指纹已存在（pending 重复或已决不重开）；
    · 只读失败轨迹 + 只写提案队列——**不写任何配置**（应用由人裁决后手动改）。
    """
    from core import config_proposal as _cp
    try:
        out = _cp.generate(conn)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
    if not out.get("enabled", True):
        return {"ok": True, "enabled": False, "created": 0, "skipped": 0, "proposals": []}
    audit(user, "config_proposal_generate",
          "新建 %d 条提案（跳过 %d 条重复/已决）" % (out.get("created", 0), out.get("skipped", 0)),
          conn=conn)
    return {"ok": True, **out}


@router.post("/api/proposals/{proposal_id}/decide")
def decide_proposal(proposal_id: int,
                    body: dict = Body(default={}),
                    conn=Depends(db_session), user=Depends(current_user)):
    """裁决提案：applied / rejected + 备注。

    ⚠️ 本端点**不执行任何配置修改**——"applied" 只是记录"人已经去改了"这个
    事实（审计留痕），配置的实际变更是设置页那条链路。终态不重开。
    """
    from core import config_proposal as _cp
    decision = str((body or {}).get("decision") or "").strip()
    note = str((body or {}).get("note") or "").strip()
    out = _cp.decide(conn, proposal_id, decision, note=note,
                     decided_by=audit_user(user) or "manual")
    if not out.get("ok"):
        code = 404 if "不存在" in str(out.get("error", "")) else 400
        return JSONResponse({"ok": False, "error": out.get("error")}, code)
    audit(user, "config_proposal_decide",
          "提案 #%s → %s（%s）" % (proposal_id, decision, note or "无备注"), conn=conn)
    return {"ok": True, "proposal": out["proposal"]}


@router.post("/api/proposals/{proposal_id}/sandbox")
def sandbox_proposal(proposal_id: int, body: dict = Body(default={}),
                     conn=Depends(db_session), user=Depends(current_user)):
    """发起沙箱 A/B 评估（P1-4）：影子配置 vs 活配置各跑 CI 门禁集，差分判回归。

    异步：评估分钟级，提交即返回 {run_id, job_id}，轮询
    GET /api/sandbox/runs/{run_id} 看状态/结论。评估期间**零生产副作用**
    （门禁子进程用每臂独立临时库 + 配置副本；影子层只进实验组子进程）。

    body.scope：full（默认，全 CI 门禁集）| referenced（仅静态引用该配置的
    门禁，快速预检）。
    """
    from core import sandbox as _sandbox
    scope = str((body or {}).get("scope") or "").strip()
    out = _sandbox.start_run(conn, proposal_id, scope)
    if not out.get("ok"):
        code = 404 if "不存在" in str(out.get("error", "")) else 400
        return JSONResponse({"ok": False, "error": out.get("error")}, code)
    run = out["run"]
    # 作业队列执行（长任务既有通道：提交即返回 + 进度可查 + 租约保活）
    from core import job_queue as jq
    job = jq.submit(conn, "config_sandbox", {"run_id": int(run["id"])},
                    job_key="config_sandbox:%d" % int(run["id"]),
                    reuse_terminal=False)
    audit(user, "config_sandbox_start",
          "提案 #%s 沙箱评估 run#%s（scope=%s）→ job#%s"
          % (proposal_id, run["id"], run.get("scope"), job.get("job_id")), conn=conn)
    return {"ok": True, "run": run, "job_id": job.get("job_id"),
            "note": "评估进行中（分钟级）；轮询 GET /api/sandbox/runs/%s" % run["id"]}


@router.get("/api/proposals/{proposal_id}/sandbox-runs")
def sandbox_runs_of(proposal_id: int, limit: int = 20,
                    conn=Depends(db_session), user=Depends(current_user)):
    """某提案的沙箱评估历史（含 verdict/summary，最新在前）。"""
    from core import sandbox as _sandbox
    runs = _sandbox.list_runs(conn, proposal_id=proposal_id, limit=limit)
    return {"ok": True, "runs": runs, "verdicts": list(_sandbox.VERDICTS)}


@router.get("/api/sandbox/runs/{run_id}")
def sandbox_run_detail(run_id: int, conn=Depends(db_session),
                       user=Depends(current_user)):
    """单次沙箱评估详情（轮询终点）：status/verdict/summary_json/job_id。"""
    from core import sandbox as _sandbox
    run = _sandbox.get_run(conn, run_id)
    if not run:
        return JSONResponse({"ok": False, "error": "评估运行不存在"}, 404)
    return {"ok": True, "run": run}


@router.post("/api/proposals/{proposal_id}/promote")
def promote_proposal(proposal_id: int, conn=Depends(db_session),
                     user=Depends(current_user)):
    """发起晋升（P1-4）：沙箱 pass ⇒ 入 HIL 待批准队列；**人批准后才真改配置**。

    前置（缺一不可，fail-closed）：提案 pending、目标是已登记配置键、
    存在与提案当前目标一致的 verdict=pass 沙箱结论。
    本端点只写 HIL 队列，不写任何配置；执行在 HIL decide 的 approve 分支
    （core/config_promotion.execute_confirmation）。
    """
    from core import config_promotion as _promo
    out = _promo.request_promotion(conn, proposal_id,
                                   user=(user or {}).get("username", "") if user else "")
    if not out.get("ok"):
        return JSONResponse({"ok": False, "error": out.get("error")}, 400)
    audit(user, "config_promotion_request",
          "提案 #%s 发起晋升 → HIL 确认单 #%s（沙箱 run#%s）"
          % (proposal_id, out.get("confirmation_id"),
             (out.get("sandbox_run") or {}).get("id")), conn=conn)
    return {"ok": True, "confirmation_id": out.get("confirmation_id"),
            "note": "已入 HIL 待批准队列；批准后配置才真正变更（可回滚）"}
