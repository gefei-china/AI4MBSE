# -*- coding: utf-8 -*-
"""AI 记忆管理 API（2026-10-02）：设置页「AI 记忆」面板的后端契约。

## 为什么要有它
`agent_memory`（跨会话长期记忆）此前**只有写入没有落点** —— 用户在界面上看不到 AI 记住了什么，
也无法删除。标杆三家（ChatGPT 设置→个性化→记忆、Claude 展示可编辑摘要 + 删除对话即删派生记忆、
Gemini）都有查看/删除/关闭。本模块补上「可见 + 可删」的最小能力（评估报告 §4 的 P0 项）。

## 端点
    GET    /api/memory/list              列表 + 统计（筛选 agent_id/tier/mem_type/q/含已归档）
    GET    /api/memory/export            导出用户记忆（JSON，数据可携带）
    DELETE /api/memory/{mid}             删除单条（**真删**，用户行使被遗忘权）
    POST   /api/memory/{mid}/forget      软删单条（forgotten=1，可恢复）
    POST   /api/memory/{mid}/restore     恢复软删
    POST   /api/memory/purge-forgotten   清除全部已软删记忆

## 权限口径（与同页一致）
照 `routers/intent_samples.py` 的既定纪律：**写端点不加权限门**，与同一块设置页上的
`PUT /api/system/config/static`（`routers/config.py`，无权限依赖）保持一致 ——
否则会出现「改配置能点、删记忆 403」的割裂。所有写操作保留 audit 留痕；
若后续要给设置页统一加门，应与 config 端点一并加（不要只加这一处）。

## ⚠️ 无用户维度隔离
`agent_memory` 无 user 列（实测 `scope_type` 仅 `''`/`project`）⇒ 本模块操作的是**全局记忆**。
多用户部署前必须补 user 维度（见仓储层 docstring 与评估报告 §4）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_permission
from core.audit import audit, audit_user
from repositories.memory_admin_repo import MemoryAdminRepo

router = APIRouter(tags=["AI 记忆管理"])


@router.get("/api/memory/list")
def list_memories(agent_id: str = "", tier: str = "", mem_type: str = "", q: str = "",
                  include_forgotten: int = 0, limit: int = 200, offset: int = 0,
                  conn=Depends(db_session), user=Depends(current_user)):
    """记忆列表 + 统计（页面顶部徽章与筛选器用它，避免前端多次请求）。

    D2-b（2026-10-07）：补 `user=Depends(current_user)`。
    ⚠️ **这不是"顺手加个参数"** —— 此前本端点**无任何身份依赖**，
    任何登录用户都能列出**全库**记忆（含他人偏好类内容）。
    过滤在 `MemoryAdminRepo._viewer_clause` 内按**已认证身份**强制执行，
    **不接受前端传参**（改 URL 也拿不到别人的）。
    """
    return MemoryAdminRepo(conn).list(
        agent_id=agent_id or None, tier=tier or None, mem_type=mem_type or None,
        q=q or None, include_forgotten=1 if include_forgotten else 0,
        limit=max(1, min(limit, 1000)), offset=max(0, offset), viewer=user)


@router.get("/api/memory/export")
def export_memories(agent_id: str = "", conn=Depends(db_session),
                    user=Depends(current_user)):
    """导出记忆为 JSON（数据可携带，合规配套）。

    D2-b：**泄露面最大的端点**（一次调用导出全库）⇒ 强制身份过滤。
    admin 看全量；普通用户只导出自己的 user 记忆 + 公共记忆。
    """
    items = MemoryAdminRepo(conn).export_all(agent_id or None, viewer=user)
    return {"count": len(items), "items": items}


@router.delete("/api/memory/{mid}")
def delete_memory(mid: int, conn=Depends(db_session), user=Depends(current_user),
                  _u=Depends(require_permission("memory", "manage"))):
    """**真删**单条记忆（用户行使被遗忘权；不可恢复）。

    D2-b：补 `memory:manage` 权限门 + **按人过滤**。
    ⚠️ 此前无门 ⇒ 任何登录用户可删任意记忆（含他人偏好）。
    过滤口径与 list/export 一致：普通用户只能删自己的 user 记忆；
    admin 可删全部（运维职责）。
    """
    repo = MemoryAdminRepo(conn)
    row = repo.get(mid)
    if not row:
        return JSONResponse({"error": "记忆不存在"}, 404)
    if not repo._can_touch(row, user):
        return JSONResponse({"error": "无权操作该记忆（仅可操作自己的记忆）"}, 403)
    repo.hard_delete(mid)
    audit(audit_user(user), "memory_delete",
          "删除记忆#%d(%s): %s" % (mid, row.get("agent_id") or "", (row.get("content") or "")[:40]),
          conn=conn)
    return {"ok": True, "id": mid}


@router.post("/api/memory/{mid}/forget")
def forget_memory(mid: int, conn=Depends(db_session), user=Depends(current_user),
                  _u=Depends(require_permission("memory", "manage"))):
    """软删（forgotten=1）：检索跳过、可 restore。D2-b：同 delete 加门 + 按人过滤。"""
    repo = MemoryAdminRepo(conn)
    row = repo.get(mid)
    if not row:
        return JSONResponse({"error": "记忆不存在或已归档"}, 404)
    if not repo._can_touch(row, user):
        return JSONResponse({"error": "无权操作该记忆（仅可操作自己的记忆）"}, 403)
    if not repo.soft_delete(mid):
        return JSONResponse({"error": "记忆不存在或已归档"}, 404)
    audit(audit_user(user), "memory_forget", "归档记忆#%d" % mid, conn=conn)
    return {"ok": True, "id": mid}


@router.post("/api/memory/{mid}/restore")
def restore_memory(mid: int, conn=Depends(db_session), user=Depends(current_user),
                   _u=Depends(require_permission("memory", "manage"))):
    """恢复软删记忆。D2-b：同 delete 加门 + 按人过滤。"""
    repo = MemoryAdminRepo(conn)
    row = repo.get(mid)
    if not row:
        return JSONResponse({"error": "记忆不存在或未归档"}, 404)
    if not repo._can_touch(row, user):
        return JSONResponse({"error": "无权操作该记忆（仅可操作自己的记忆）"}, 403)
    if not repo.restore(mid):
        return JSONResponse({"error": "记忆不存在或未归档"}, 404)
    audit(audit_user(user), "memory_restore", "恢复记忆#%d" % mid, conn=conn)
    return {"ok": True, "id": mid}


@router.post("/api/memory/purge-forgotten")
def purge_forgotten(conn=Depends(db_session), user=Depends(current_user),
                    _u=Depends(require_permission("memory", "manage"))):
    """清除全部已软删（forgotten=1）记忆，返回清除条数。

    ⚠️ D2-b：**这是全局破坏性操作**（不可逆、影响所有人），故**限 admin**
    ——普通用户即使有 `memory:manage` 也不放行，避免误清他人归档。
    """
    if not MemoryAdminRepo._is_admin(user):
        return JSONResponse({"error": "清理全部已归档记忆仅限管理员"}, 403)
    n = MemoryAdminRepo(conn).hard_delete_forgotten()
    audit(audit_user(user), "memory_purge", "清除已归档记忆 %d 条" % n, conn=conn)
    return {"ok": True, "deleted": n}
