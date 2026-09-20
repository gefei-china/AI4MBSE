# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：意图规则与检索规则。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/rules")
def list_rules(conn=Depends(db_session)):
    return StudioRepo(conn).list_rules()


@router.put("/api/studio/rules/{rule_id}")
def update_rule(rule_id: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).update_rule(rule_id, body["value"])
    audit(audit_user(user), "rule_update", f"更新规则#{rule_id}: {body['value']}", conn=conn)
    return {"ok": True}


@router.get("/api/studio/intent-rules")
def list_intent_rules(conn=Depends(db_session)):
    """意图路由规则列表（id/trigger/intent/weight/enabled/created_at）。"""
    return StudioRepo(conn).rows(
        "SELECT id, trigger, intent, weight, enabled, created_at FROM intent_rules ORDER BY id")


@router.post("/api/studio/intent-rules")
def create_intent_rule(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """创建意图路由规则：trigger 去重（重复 400）；审计 intent_rule_create。"""
    trigger = (body.get("trigger") or "").strip()
    intent = (body.get("intent") or "").strip()
    weight = body.get("weight", 1.0)
    err = _intent_rule_err(trigger, intent, weight)
    if err:
        return JSONResponse({"error": err}, 400)
    weight = float(weight)
    enabled = 1 if body.get("enabled", 1) else 0
    repo = StudioRepo(conn)
    if repo.one("SELECT id FROM intent_rules WHERE trigger=?", (trigger,)):
        return JSONResponse({"error": f"trigger 已存在: {trigger}"}, 400)
    rid = repo.execute(
        "INSERT INTO intent_rules (trigger, intent, weight, enabled, created_by) VALUES (?,?,?,?,?)",
        (trigger, intent, weight, enabled, "王工"))
    audit(audit_user(user), "intent_rule_create", f"创建意图规则: {trigger} → {intent} (weight={weight})", conn=conn)
    _invalidate_intent_rules()
    return {"ok": True, "id": rid}


@router.put("/api/studio/intent-rules/{rid}")
def update_intent_rule(rid: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """更新意图路由规则（body 可改 trigger/intent/weight/enabled）；审计 intent_rule_update。"""
    repo = StudioRepo(conn)
    row = repo.one("SELECT * FROM intent_rules WHERE id=?", (rid,))
    if not row:
        return JSONResponse({"error": "规则不存在"}, 404)
    trigger = (body.get("trigger") if body.get("trigger") is not None else row["trigger"]).strip()
    intent = (body.get("intent") if body.get("intent") is not None else row["intent"]).strip()
    weight = body.get("weight") if body.get("weight") is not None else row["weight"]
    enabled = body.get("enabled") if body.get("enabled") is not None else row["enabled"]
    err = _intent_rule_err(trigger, intent, weight)
    if err:
        return JSONResponse({"error": err}, 400)
    weight = float(weight)
    if repo.one("SELECT id FROM intent_rules WHERE trigger=? AND id<>?", (trigger, rid)):
        return JSONResponse({"error": f"trigger 已存在: {trigger}"}, 400)
    repo.execute(
        "UPDATE intent_rules SET trigger=?, intent=?, weight=?, enabled=? WHERE id=?",
        (trigger, intent, weight, 1 if enabled else 0, rid))
    audit(audit_user(user), "intent_rule_update", f"更新意图规则#{rid}: {trigger} → {intent}", conn=conn)
    _invalidate_intent_rules()
    return {"ok": True}


@router.delete("/api/studio/intent-rules/{rid}")
def delete_intent_rule(rid: int, conn=Depends(db_session), user=Depends(current_user)):
    """删除意图路由规则；审计 intent_rule_delete。"""
    repo = StudioRepo(conn)
    row = repo.one("SELECT id, trigger FROM intent_rules WHERE id=?", (rid,))
    if not row:
        return JSONResponse({"error": "规则不存在"}, 404)
    repo.execute("DELETE FROM intent_rules WHERE id=?", (rid,))
    audit(audit_user(user), "intent_rule_delete", f"删除意图规则#{rid}: {row['trigger']}", conn=conn)
    _invalidate_intent_rules()
    return {"ok": True}
