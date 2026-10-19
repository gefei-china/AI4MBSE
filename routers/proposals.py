# -*- coding: utf-8 -*-
"""P1-3 配置提案 API：失败轨迹 → 修订提议（只提议，不自动应用）。

## 产品口径（2026-10-18 自进化调研路线图 P1-3）
RefineGate 从「内容评审」泛化为「配置评审」：读 P1-2 归因器聚出的首坏步
模式，按规则注册表提议 prompt/skill/配置修订（GEPA 式反思改写）。

**铁律：只提议，不自动应用。** 自动改生产配置是 misevolution（进化成错）的
直接通道——每个提案必须经人裁决（applied/rejected 留痕），"应用"是人在设置页
改配置（现有配置 API）。本路由与引擎、repo 三层合起来也没有任何写配置的路径。

端点：
    GET    /api/proposals               提案队列（status 过滤 + 统计）
    POST   /api/proposals/generate      跑规则（读失败轨迹）→ 新提案入队
    POST   /api/proposals/{id}/decide   裁决（applied/rejected + 备注，留痕）
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
