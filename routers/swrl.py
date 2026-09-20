# -*- coding: utf-8 -*-
"""P1-①/②（2026-09-11）SWRL 规则管理 API。

设计依据：[SWRL-SPARQL-时态与提示词注入-标准版方案](../../评审报告/SWRL-SPARQL-时态与提示词注入-标准版方案-20260911.md) §4-§5

路由（冻结契约，前端 swrl_editor.html 直接消费）：
- GET    /api/swrl/dependency          依赖状态（owlready2 / jpype）
- GET    /api/swrl/rules               规则列表
- POST   /api/swrl/rules               新建规则
- PUT    /api/swrl/rules/{id}          更新规则（含启用/停用）
- DELETE /api/swrl/rules/{id}          删除规则
- POST   /api/swrl/rules/{id}/toggle   切换启用/停用
- POST   /api/swrl/seed-templates      写入 BUILTIN_RULE_TEMPLATES
- POST   /api/swrl/run                 运行推理（mode=off|warn|enforce）
- GET    /api/swrl/facts               推理事实列表
- POST   /api/swrl/facts/{id}/accept   接受一条事实（is_accepted=1）
- POST   /api/swrl/facts/{id}/reject   拒绝一条事实（删除）
- POST   /api/swrl/facts/clear         清空暂存事实
"""
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.deps import db_session

router = APIRouter(prefix="/api/swrl", tags=["SWRL 规则"])


# ── Pydantic 入参模型 ────────────────────────────────────────────────
class RuleCreate(BaseModel):
    name: str
    body: str
    head: str
    comment: str = ""
    priority: int = 0
    is_active: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    body: Optional[str] = None
    head: Optional[str] = None
    comment: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


class RunRequest(BaseModel):
    mode: str = "warn"          # off | warn | enforce
    rule_ids: Optional[list] = None
    clear_accepted: bool = False


# ── 依赖状态 ────────────────────────────────────────────────────────
@router.get("/dependency")
def dependency():
    from services.swrl_engine import dependency_status
    return dependency_status()


# ── 规则 CRUD ────────────────────────────────────────────────────────
@router.get("/rules")
def list_rules(conn=Depends(db_session)):
    cur = conn.execute(
        "SELECT id, name, comment, body, head, priority, is_active, "
        "created_at, last_executed_at, last_inferred_count "
        "FROM swrl_rules ORDER BY priority DESC, id"
    )
    return {"rules": [dict(r) for r in cur.fetchall()]}


@router.post("/rules")
def create_rule(body: RuleCreate, conn=Depends(db_session)):
    from services.swrl_engine import SWRLEngine
    eng = SWRLEngine(conn)
    # 解析 body 中的 "->" 写法
    from services.swrl_engine import parse_rule_text
    body_text = body.body
    head_text = body.head
    if not head_text:
        parsed = parse_rule_text(body_text)
        if parsed:
            body_text, head_text = parsed
    rule_id = eng.register_rule(
        name=body.name,
        body=body_text,
        head=head_text,
        comment=body.comment,
        priority=body.priority,
        is_active=body.is_active,
    )
    return {"ok": True, "rule_id": rule_id, "body": body_text, "head": head_text}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleUpdate, conn=Depends(db_session)):
    fields, params = [], []
    for k, v in body.dict(exclude_none=True).items():
        fields.append(f"{k}=?")
        params.append(v)
    if not fields:
        return {"ok": True, "affected": 0}
    params.append(rule_id)
    cur = conn.execute(f"UPDATE swrl_rules SET {','.join(fields)} WHERE id=?", params)
    conn.commit()
    return {"ok": True, "affected": cur.rowcount}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, conn=Depends(db_session)):
    cur = conn.execute("DELETE FROM swrl_rules WHERE id=?", (rule_id,))
    conn.commit()
    return {"ok": True, "affected": cur.rowcount}


@router.post("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, conn=Depends(db_session)):
    cur = conn.execute("SELECT is_active FROM swrl_rules WHERE id=?", (rule_id,))
    row = cur.fetchone()
    if not row:
        return JSONResponse({"error": "rule not found"}, 404)
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE swrl_rules SET is_active=? WHERE id=?", (new_val, rule_id))
    conn.commit()
    return {"ok": True, "is_active": bool(new_val)}


@router.post("/seed-templates")
def seed_templates(conn=Depends(db_session)):
    from services.swrl_engine import seed_builtin_templates
    added = seed_builtin_templates(conn)
    return {"ok": True, "added": added}


# ── 推理 ────────────────────────────────────────────────────────────
@router.post("/run")
def run_reasoning(req: RunRequest, conn=Depends(db_session)):
    from services.swrl_engine import SWRLEngine
    eng = SWRLEngine(conn)
    try:
        eng.load()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)
    summary = eng.run(
        mode=req.mode,
        rule_ids=req.rule_ids,
        clear_accepted=req.clear_accepted,
    )
    return summary.to_dict()


# ── 推理事实管理 ────────────────────────────────────────────────────
@router.get("/facts")
def list_facts(
    rule_id: Optional[int] = None,
    accepted: Optional[int] = None,
    limit: int = 200,
    conn=Depends(db_session),
):
    from services.swrl_engine import SWRLEngine
    eng = SWRLEngine(conn)
    return {"facts": eng.list_facts(rule_id=rule_id, accepted=accepted, limit=limit)}


@router.post("/facts/{fact_id}/accept")
def accept_fact(fact_id: int, conn=Depends(db_session)):
    from services.swrl_engine import SWRLEngine
    eng = SWRLEngine(conn)
    return {"ok": True, "affected": eng.accept_fact(fact_id)}


@router.post("/facts/{fact_id}/reject")
def reject_fact(fact_id: int, conn=Depends(db_session)):
    from services.swrl_engine import SWRLEngine
    eng = SWRLEngine(conn)
    return {"ok": True, "affected": eng.reject_fact(fact_id)}


@router.post("/facts/clear")
def clear_facts(accepted_only: bool = False, conn=Depends(db_session)):
    sql = "DELETE FROM inferred_facts"
    if not accepted_only:
        sql += " WHERE is_accepted=0"
    cur = conn.execute(sql)
    conn.commit()
    return {"ok": True, "affected": cur.rowcount}
