# -*- coding: utf-8 -*-
"""P2-2 失败驱动的任务自生成 API：失败证据 → 任务提议（只提议，不自动执行）。

## 产品口径（2026-10-18 自进化调研路线图 P2 第二项）
EnvBuilder / AgentGym / AgentEvolver Self-Questioning 式自造课程：读失败证据
（P1-2 首坏步模式 + P0-3 坏样本台账 + 失败类型覆盖率）→ 提议「该练什么
任务」。与 P1-3 配置提案同构：规则注册表 + 指纹去重 + 三态裁决。

**铁律：只提议，不自动执行。** 自动造任务并自动执行 = 内部循环自我强化
（misevolution 滑坡形态）——每个任务提案必须经人裁决（accepted/rejected
留痕）；"accepted" 只记录"人认领了这条任务"，任务的真正创建/执行走人所在
链路（评测集构建、编排批次）。本路由与引擎、repo 三层合起来也没有任何
创建任务的路径（门禁静态断言）。

## P2-1 教训的沿用（端点失败必须显式）
misevolution 监控端点曾因采集异常裸奔 500（冷库冒烟实测）——提案/监控类
端点的失败必须 ok:False + error 可见，不 500、不静默。generate 同样处理。

端点：
    GET    /api/curriculum               任务提案队列（status 过滤 + 统计）
    POST   /api/curriculum/generate      跑规则（读失败证据）→ 新提案入队
    POST   /api/curriculum/{id}/decide   裁决（accepted/rejected + 备注，留痕）
"""
from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from core.audit import audit, audit_user
from core.deps import current_user, db_session

router = APIRouter(tags=["任务提案"])


@router.get("/api/curriculum")
def list_proposals(status: str = "", limit: int = 100, conn=Depends(db_session)):
    """任务提案队列（status 空 = 全部）。含分状态计数，供看板徽标。"""
    from core import curriculum as _cu
    from repositories.curriculum_repo import CurriculumRepo
    try:
        repo = CurriculumRepo(conn)
        return {"ok": True,
                "proposals": _cu.list_proposals(conn, status=status, limit=limit),
                "counts": repo.count_by_status(),
                "statuses": list(_cu.TASK_STATUSES),
                "task_kinds": list(_cu.TASK_KINDS)}
    except Exception as e:                                   # noqa: BLE001
        # P2-1 教训（misevolution 端点冷库实测 500）：读端点的失败必须显式
        # ok:False + error——看板整块渲染失败比一个错误横幅糟糕得多
        return {"ok": False,
                "error": "curriculum 队列读取失败: %s: %s" % (type(e).__name__, e)}


@router.post("/api/curriculum/generate")
def generate_proposals(conn=Depends(db_session), user=Depends(current_user)):
    """跑规则：读当前窗口的失败证据 → 提议练习任务 → 入队。

    返回 {created, skipped, proposals, evidence_summary}：
    · created = 本次新入队；skipped = 指纹已存在（pending 重复或已决不重开）；
    · 只读失败证据 + 只写任务提案队列——**不创建任何任务**（人认领后走人的
      链路）。没有失败证据 ⇒ created=0（空窗口不是"没问题"，更不是凑数）。
    """
    from core import curriculum as _cu
    try:
        out = _cu.generate(conn)
    except Exception as e:                                   # noqa: BLE001
        # P2-1 教训：提案端点的失败必须显式可见，不 500、不静默
        return JSONResponse({"ok": False,
                             "error": "curriculum 生成失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    if not out.get("enabled", True):
        return {"ok": True, "enabled": False, "created": 0, "skipped": 0,
                "proposals": []}
    audit(user, "curriculum_generate",
          "新建 %d 条任务提案（跳过 %d 条重复/已决）"
          % (out.get("created", 0), out.get("skipped", 0)), conn=conn)
    return {"ok": True, **out}


@router.post("/api/curriculum/{item_id}/decide")
def decide_proposal(item_id: int,
                    body: dict = Body(default={}),
                    conn=Depends(db_session), user=Depends(current_user)):
    """裁决任务提案：accepted / rejected + 备注。

    ⚠️ 本端点**不创建任何任务**——"accepted" 只是记录"人认领了这条任务"这个
    事实（审计留痕），任务的真正创建/执行走人所在链路。终态不重开。
    """
    from core import curriculum as _cu
    decision = str((body or {}).get("decision") or "").strip()
    note = str((body or {}).get("note") or "").strip()
    try:
        out = _cu.decide(conn, item_id, decision, note=note,
                         decided_by=audit_user(user) or "manual")
    except Exception as e:                                   # noqa: BLE001
        # 同 list 端点纪律：裁决异常显式失败，不裸 500
        return JSONResponse({"ok": False,
                             "error": "curriculum 裁决失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    if not out.get("ok"):
        code = 404 if "不存在" in str(out.get("error", "")) else 400
        return JSONResponse({"ok": False, "error": out.get("error")}, code)
    audit(user, "curriculum_decide",
          "任务提案 #%s → %s（%s）" % (item_id, decision, note or "无备注"),
          conn=conn)
    return {"ok": True, "proposal": out["proposal"]}
