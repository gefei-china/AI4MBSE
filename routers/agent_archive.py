# -*- coding: utf-8 -*-
"""P2-3 Agent 档案库/种群（DGM 式 archive）API：进化历史 → 变体台账 + 死路记忆。

## 产品口径（2026-10-18 自进化调研路线图 P2 第三项）
DGM/AlphaZero 式种群的关键不是「保留最优」，而是**记住死掉的个体**。
本路由把散落的进化历史（asset_change_logs 字段级 diff + agents.version
血缘）收敛成可查询的变体台账，让提案侧能回避已知死路。

**铁律：只记录，不切换生效 agent、不回滚任何配置。** 档案库是记忆，不是手
（回滚过人的铁律同 P1-4）。scan 只 INSERT 新表，refresh 只更新画像与死路标记，
全链路**没有任何 UPDATE agents / config_history 的语句**（门禁静态断言）。

## 诚实边界（承 P2-1）
sandbox_runs / misevolution_checks / bad_cases 都是提案级或会话级，没有
agent_id + version 列——本路由**不猜**评估针对哪个变体。谁知道谁通过
POST /outcome 显式挂载；挂不上就诚实地缺席（画像对应维度为 unknown，
而不是回落 0 或假装干净）。

## P2-1 教训的沿用（端点失败必须显式）
监控/提案类端点曾因采集异常裸奔 500（冷库冒烟实测）——本路由所有端点的
失败一律 ok:False + error 可见，不 500、不静默。

端点：
    GET  /api/agent-archive               变体台账（agent_id/status 过滤 + 统计 + 死路数）
    POST /api/agent-archive/scan          扫描进化历史 → 收敛台账（幂等）
    POST /api/agent-archive/refresh       重算画像与死路判定
    POST /api/agent-archive/{id}/outcome  显式挂载一条评估结论到某变体
"""
from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from core.audit import audit, audit_user
from core.deps import current_user, db_session

router = APIRouter(tags=["Agent档案库"])


@router.get("/api/agent-archive")
def list_variants(agent_id: int = 0, status: str = "", limit: int = 200,
                  conn=Depends(db_session)):
    """变体台账（含分状态计数与死路清单，供看板徽标与提案侧回避查询）。"""
    from core import agent_archive as _aa
    from repositories.agent_archive_repo import AgentArchiveRepo
    try:
        repo = AgentArchiveRepo(conn)
        return {"ok": True,
                "variants": repo.list_variants(
                    agent_id=(int(agent_id) if agent_id else None),
                    status=status, limit=limit),
                "counts": repo.count_by_status(),
                "dead_ends": _aa.query_dead_ends(conn, limit=50),
                "statuses": list(_aa.VARIANT_STATUSES),
                "outcomes": list(_aa.OUTCOMES),
                "dimensions": [{"name": d.name, "label": d.label,
                                "lower_better": d.lower_better}
                               for d in _aa.DIMENSIONS]}
    except Exception as e:                                   # noqa: BLE001
        # P2-1 教训（misevolution 端点冷库实测 500）：读端点失败必须显式
        return {"ok": False,
                "error": "agent 档案库读取失败: %s: %s" % (type(e).__name__, e)}


@router.post("/api/agent-archive/scan")
def scan_archive(conn=Depends(db_session), user=Depends(current_user)):
    """扫描进化历史 → 收敛变体台账（幂等：同指纹只跳过不注水）。

    只读 agents/asset_change_logs + 只写自家两表——**不改 agents、
    不切换生效版本、不回滚配置**。
    """
    from core import agent_archive as _aa
    try:
        out = _aa.scan(conn)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "agent 档案库扫描失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    if not out.get("enabled", True):
        return {"ok": True, "enabled": False, "scanned": 0, "created": 0,
                "skipped": 0, "variants": [], "dead_ends": []}
    audit(user, "agent_archive_scan",
          "扫描进化历史：候选 %s 个变体，新入册 %d，跳过 %d"
          % (out.get("scanned", 0), out.get("created", 0), out.get("skipped", 0)),
          conn=conn)
    return {"ok": True, **out}


@router.post("/api/agent-archive/refresh")
def refresh_archive(conn=Depends(db_session), user=Depends(current_user)):
    """重算全部已入册变体的画像与死路判定。

    死路判定只看**已记录的负向结论数**（variant_outcomes）达
    dead_end_repeat——只看已发生的事实，不推测。
    """
    from core import agent_archive as _aa
    try:
        out = _aa.refresh(conn)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "agent 档案库复算失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    if not out.get("enabled", True):
        return {"ok": True, "enabled": False, "refreshed": 0, "dead_ends": 0}
    if out.get("dead_ends"):
        audit(user, "agent_archive_dead_end",
              "复算后新判死路 %d 个变体" % out["dead_ends"], conn=conn)
    return {"ok": True, **out}


@router.post("/api/agent-archive/{variant_id}/outcome")
def add_outcome(variant_id: int, body: dict = Body(default={}),
                conn=Depends(db_session), user=Depends(current_user)):
    """显式挂载一条评估结论到某变体（档案库的入户口）。

    ⚠️ 为什么必须显式：真实库里 sandbox_runs / misevolution_checks 没有
    agent_id + version 列，档案库**不猜**这次评估针对哪个变体——猜出来的
    血缘比没有血缘更危险。谁知道谁挂载；变体未入册则拒绝（不凭空造历史）。
    """
    from core import agent_archive as _aa
    b = body or {}
    try:
        out = _aa.record_outcome(
            conn, agent_id=int(b.get("agent_id") or 0),
            version=str(b.get("version") or ""),
            outcome=str(b.get("outcome") or ""),
            source=str(b.get("source") or "manual"),
            detail=str(b.get("detail") or ""),
            recorded_by=audit_user(user) or "manual")
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "agent 档案库结局挂载失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    if not out.get("ok"):
        code = 404 if "未入册" in str(out.get("error", "")) else 400
        return JSONResponse({"ok": False, "error": out.get("error")}, code)
    audit(user, "agent_archive_outcome",
          "变体 #%s（%s@%s）挂载结论 %s（来源 %s）"
          % (variant_id, b.get("agent_id"), b.get("version"),
             b.get("outcome"), b.get("source") or "manual"), conn=conn)
    return {"ok": True, "variant": out["variant"]}


@router.get("/api/agent-archive/lineage/{agent_id}")
def get_lineage(agent_id: int, conn=Depends(db_session)):
    """某 agent 的版本血缘台账（谁从谁改来、各自状态与画像摘要）。"""
    from core import agent_archive as _aa
    try:
        return {"ok": True, "agent_id": agent_id,
                "lineage": _aa.lineage(conn, agent_id)}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False,
                "error": "agent 血缘读取失败: %s: %s" % (type(e).__name__, e)}