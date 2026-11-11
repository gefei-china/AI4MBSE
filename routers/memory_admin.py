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


# ───────────────────────── 离线 Dreaming（P0-1，2026-11-11）─────────────────────────
# 设计取舍（与 `core/memory_dream.py` 的硬纪律一致）：
#   1. preview 只读：不写任何表、不调 LLM，产出**独立候选包**；
#   2. apply 必须**显式带 fingerprint**，服务端重跑一次 dream 比对 —— 输入变了就拒绝。
#      这既防"预览后库里又被写入导致按旧判断落库"，也防客户端回传被篡改的候选包；
#   3. apply **只接受 memory_ids 白名单**，逐条人工挑选，不提供"全部应用"；
#   4. 落库只改 verify_state / evidence_ref，**不改 content**（影子语义，可回退）。
@router.get("/api/memory/dream/preview")
def dream_preview(note_limit: int = 80, include_wiki: int = 0, include_index: int = 0,
                  conn=Depends(db_session), user=Depends(current_user)):
    """只读对账：返回候选包摘要 + 可选 wiki / 索引。

    ⚠️ 候选里 `contradicted` = 流水里有反证（需人工判断是否真错），
    `verified` = 流水关键词重合且无否定。两者都**不自动生效**。
    """
    from core.memory_dream import dream
    _limit = max(10, min(int(note_limit), 300))
    pkg = dream(conn, note_limit=_limit)
    cands = []
    for c in pkg.get("candidates") or []:
        if c.get("action") == "correct":
            cands.append({"memory_id": c.get("memory_id"), "state": c.get("state"),
                          "claim": c.get("claim"), "evidence_ref": c.get("evidence_ref"),
                          "reason": c.get("reason"), "confidence": c.get("confidence")})
    out = {"fingerprint": pkg["fingerprint"], "input_sha256": pkg["input_sha256"],
           "created_at": pkg["created_at"], "stats": pkg["stats"],
           "metrics": pkg["metrics"], "candidates": cands,
           # ★ 必须回显：fingerprint 含 note_ids，故 apply 必须用**同一个扫描窗口**
           #   重跑才能比对。不回显的话调用方无从知道该传什么，实测踩过
           #   "preview 传 300、apply 用默认 80 ⇒ fingerprint 永远不一致"。
           "note_limit": _limit,
           "read_only": True,
           "note": "候选需人工逐条挑选后调 /api/memory/dream/apply；不会自动生效"}
    if include_wiki:
        out["wiki"] = {"pages": pkg["wiki"]["pages"],
                       "dangling_links": pkg["wiki"]["dangling_links"],
                       "wiki_chars": pkg["wiki"]["wiki_chars"],
                       "source_chars": pkg["wiki"]["source_chars"]}
    if include_index:
        out["index"] = {"lines": pkg["index"]["lines"], "bytes": pkg["index"]["bytes"],
                        "text": pkg["index"]["text"]}
    return out


@router.post("/api/memory/dream/apply")
def dream_apply(body: dict, conn=Depends(db_session), user=Depends(current_user),
                _u=Depends(require_permission("memory", "manage"))):
    """HIL 审批后落库：只把白名单里的 `correct` 候选写成 verify_state。

    body: {"fingerprint": "...", "memory_ids": [1,2], "note_limit": 80,
           "approved_by": "可选"}
    `note_limit` **必须与 preview 时一致**（默认 80）：fingerprint 含被扫描的
    笔记 id 集合，窗口不同则指纹必然不一致。响应里回显 `note_limit_used` 便于排查。
    拒绝条件（任一）：无 fingerprint / fingerprint 与当前重跑结果不一致 /
    memory_ids 为空 / 候选里没有该 id 的 correct 项。
    """
    from core.memory_dream import dream, apply_candidates
    fp = str((body or {}).get("fingerprint") or "").strip()
    ids = (body or {}).get("memory_ids") or []
    try:
        ids = [int(x) for x in ids]
    except Exception:
        return JSONResponse({"error": "memory_ids 必须是整数数组"}, 400)
    if not fp:
        return JSONResponse({"error": "缺少 fingerprint（先调 /dream/preview）"}, 400)
    if not ids:
        return JSONResponse({"error": "memory_ids 不能为空：不提供「全部应用」"}, 400)
    # 必须用与 preview 相同的扫描窗口重跑（fingerprint 含 note_ids，
    # 窗口不同则指纹必然不一致 ⇒ 调用方会莫名其妙一直被拒）。
    _limit = max(10, min(int((body or {}).get("note_limit") or 80), 300))
    pkg = dream(conn, note_limit=_limit)
    if pkg["fingerprint"] != fp:
        return JSONResponse({"error": "输入已变化（fingerprint 不一致），请重新预览后再审批",
                             "current_fingerprint": pkg["fingerprint"],
                             "note_limit_used": _limit,
                             "hint": "apply 必须带与 preview 相同的 note_limit"}, 409)
    picked = [c for c in pkg["candidates"]
              if c.get("action") == "correct" and c.get("memory_id") in ids]
    if not picked:
        return JSONResponse({"error": "所选 id 在本次候选中没有可落库项"}, 400)
    pkg["candidates"] = picked
    res = apply_candidates(conn, pkg, hil_approved=True,
                           approved_by=str((body or {}).get("approved_by") or
                                           audit_user(user) or ""))
    audit(audit_user(user), "memory_dream_apply",
          "Dreaming 落库 %d 条（fingerprint=%s，ids=%s）" %
          (res.get("applied", 0), fp[:12], ids[:20]), conn=conn)
    return {"ok": res.get("ok"), "applied": res.get("applied"),
            "reason": res.get("reason"), "fingerprint": fp,
            "skipped_ids": [i for i in ids if i not in {c.get("memory_id") for c in picked}]}


@router.get("/api/memory/dream/wiki")
def dream_wiki(topic: str = "", conn=Depends(db_session), user=Depends(current_user)):
    """编译后的记忆 wiki 页面（L2→L3）。不带 topic 时返回页名清单，避免一次吐全文。"""
    from core.memory_dream import dream, load_wiki_page
    # 优先读落盘版本（`persist` 过就是同一份），没有则现编译
    if topic:
        md = load_wiki_page(topic)
        if md:
            return {"topic": topic, "markdown": md, "exists": True, "source": "disk"}
        pages = dream(conn)["wiki"]["pages"]
        return {"topic": topic, "markdown": pages.get(topic, ""),
                "exists": topic in pages, "source": "compiled"}
    pages = dream(conn)["wiki"]["pages"]
    return {"topics": sorted(pages), "count": len(pages),
            "note": "带 ?topic=<页名> 取该页 markdown"}


@router.post("/api/memory/dream/persist")
def dream_persist(conn=Depends(db_session), user=Depends(current_user),
                  _u=Depends(require_permission("memory", "manage"))):
    """把当前编译出的 wiki 页落盘到 `data/memory_wiki/`，返回写入文件清单。"""
    from core.memory_dream import dream, persist_wiki
    pkg = dream(conn)
    res = persist_wiki(pkg["wiki"])
    audit(audit_user(user), "memory_dream_persist",
          "记忆 wiki 落盘 %d 页（fingerprint=%s）" %
          (res.get("written", 0), pkg["fingerprint"][:12]), conn=conn)
    return {"ok": res.get("errors") == 0, "fingerprint": pkg["fingerprint"],
            "written": res.get("written"), "errors": res.get("errors"),
            "dir": res.get("dir"), "files": (res.get("files") or [])[:50]}


@router.get("/api/memory/notes")
def memory_notes(kind: str = "", status: str = "", q: str = "", limit: int = 100,
                 conn=Depends(db_session), user=Depends(current_user)):
    """统一 notes 视图（P1-3）：把 8 类分裂台账按同一套字段读出来。

    `v_memory_notes` 是只读视图；本端点只做过滤与截断，不写。
    """
    sql = "SELECT * FROM v_memory_notes WHERE 1=1"
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if status:
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND claim LIKE ?"
        args.append("%" + q + "%")
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": "视图不可用：%s" % exc}, 503)
    return {"count": len(rows), "items": rows}


@router.get("/api/memory/transcript/search")
def transcript_search(q: str, limit: int = 20, conn=Depends(db_session),
                      user=Depends(current_user)):
    """L1 流水检索（P0-2）。trigram 命中优先，短词自动走 LIKE 兜底。"""
    if not (q or "").strip():
        return {"error": "q 不能为空", "items": []}
    from transcript_fts import search_transcripts
    return {"q": q, "items": search_transcripts(conn, q, limit=max(1, min(int(limit), 50)))}


@router.post("/api/memory/retention/sweep")
def retention_sweep(older_than_days: int = 90, conn=Depends(db_session),
                    user=Depends(current_user)):
    """只读盘点超龄流水（P2）。**不写库**；要真归档须再调 /apply 并带 confirm。"""
    from core.memory_retention import sweep
    return sweep(conn, older_than_days=max(1, int(older_than_days)))


@router.post("/api/memory/retention/apply")
def retention_apply(body: dict, conn=Depends(db_session), user=Depends(current_user),
                    _u=Depends(require_permission("memory", "manage"))):
    """按计划把超龄行归档到 transcript_archive 再从主表移除（可 restore）。

    body: {"confirm": true, "older_than_days": 90}
    未带 confirm=true 一律拒绝 —— 这是对 L1 底账的写操作。
    """
    from core.memory_retention import sweep, archive_then_delete_expired
    if not (body or {}).get("confirm"):
        return JSONResponse({"error": "L1 底账写操作需 confirm=true"}, 400)
    days = int((body or {}).get("older_than_days") or 90)
    plan = sweep(conn, older_than_days=days)
    if plan.get("skipped"):
        return JSONResponse({"error": "部分流水表不可读", "skipped": plan["skipped"]}, 503)
    out = {}
    for table in (plan.get("tables") or {}):
        out[table] = archive_then_delete_expired(conn, table=table, days=days)
    total = sum(v.get("removed", 0) for v in out.values())
    audit(audit_user(user), "memory_retention_apply",
          "流水冷归档 %d 行（>%d 天）" % (total, days), conn=conn)
    return {"ok": True, "removed": total, "detail": out}
