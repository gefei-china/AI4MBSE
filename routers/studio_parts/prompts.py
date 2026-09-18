# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：提示词管理。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/prompts")
def list_prompts(conn=Depends(db_session)):
    return StudioRepo(conn).list_prompts()


@router.post("/api/studio/prompts")
def create_prompt(p: PromptIn, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).create_prompt(p.name, p.scenario, p.content, json.dumps(p.variables), p.version)
    audit(audit_user(user), "prompt_create", f"创建提示词: {p.name}", conn=conn)
    return {"ok": True}


@router.put("/api/studio/prompts/{pid}")
def update_prompt(pid: int, p: PromptIn, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).update_prompt(pid, p.name, p.scenario, p.content, json.dumps(p.variables), p.version)
    audit(audit_user(user), "prompt_update", f"更新提示词#{pid}", conn=conn)
    return {"ok": True}


@router.delete("/api/studio/prompts/{pid}")
def delete_prompt(pid: int, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).delete_prompt(pid)
    audit(audit_user(user), "prompt_delete", f"删除提示词#{pid}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/prompts/{pid}/publish")
def publish_prompt(pid: int, conn=Depends(db_session), user=Depends(current_user)):
    StudioRepo(conn).publish_prompt(pid)
    audit(audit_user(user), "prompt_publish", f"发布提示词#{pid}", conn=conn)
    return {"ok": True}
