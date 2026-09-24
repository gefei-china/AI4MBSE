# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：插件市场。

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.studio_parts.shared import *


@router.get("/api/studio/market")
def market_list(kind: str = "all", q: str = "", category: str = "", sort: str = "hot",
                conn=Depends(db_session), user=Depends(current_user)):
    """公共插件市场聚合列表（数据源：plugins 表 scope=public，市场统一后单一链路）。

    - sort=hot 置顶+安装数优先（默认）；sort=new 最新发布优先
    - 返回项含 installed（当前用户是否已安装），供市场卡片显示「已安装」状态
    """
    if kind not in ("all", *_VALID_MARKET_KIND):
        return JSONResponse({"error": f"kind 必须是 all|{'|'.join(_VALID_MARKET_KIND)}"}, 400)
    if sort not in ("hot", "new"):
        return JSONResponse({"error": "sort 必须是 hot|new"}, 400)
    ptype = "" if kind == "all" else kind
    rows = plugin_store.list_market(conn, user, ptype=ptype, q=q, sort=sort)
    out = [_plugin_to_market_dto(r, user) for r in rows]
    if category:
        out = [x for x in out if x.get("category") == category]
    return {"items": out, "total": len(out)}


@router.get("/api/studio/market/categories")
def market_categories(kind: str = "all", conn=Depends(db_session), user=Depends(current_user)):
    """公共市场可用分类聚合（数据源：plugins 表）。"""
    if kind not in ("all", *_VALID_MARKET_KIND):
        return JSONResponse({"error": f"kind 必须是 all|{'|'.join(_VALID_MARKET_KIND)}"}, 400)
    ptype = "" if kind == "all" else kind
    cats: dict = {}
    for r in plugin_store.list_market(conn, user, ptype=ptype, q=""):
        c = r.get("category")
        if c:
            cats[str(c)] = cats.get(str(c), 0) + 1
    return {"items": sorted(cats.items(), key=lambda x: (-x[1], str(x[0])))}


@router.get("/api/studio/plugins/mine")
def plugins_mine(kind: str = "all", q: str = "", conn=Depends(db_session), user=Depends(current_user)):
    """我的插件统一列表（数据源：plugins 表 作者=我 + 我安装的副本）。"""
    if kind not in ("all", *_VALID_MARKET_KIND):
        return JSONResponse({"error": f"kind 必须是 all|{'|'.join(_VALID_MARKET_KIND)}"}, 400)
    items = plugin_store.list_mine(conn, user, kind if kind != "all" else "", q)
    return {"items": items, "total": len(items)}


@router.post("/api/studio/market/install")
def market_install(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """安装市场条目（数据源：plugins 表；安装=版本快照引用，计数仅首次+1）。"""
    kind, name = str(body.get("kind", "")), str(body.get("name", "")).strip()
    if kind not in _VALID_MARKET_KIND or not name:
        return JSONResponse({"error": "kind 与 name 必填"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p or p.get("scope") != "public":
        return JSONResponse({"error": f"市场不存在该条目（{kind}:{name}）"}, 404)
    if p.get("status") == "disabled":
        # 2026-09-17 P2-2：停用条目仍列在市场（用于展示「⛔ 已停用」），
        # 此前一律报 404「不存在」，用户误以为条目被删。
        return JSONResponse({"error": "该能力已被市场管理员全局停用，暂不可安装"}, 409)
    if p.get("status") != "published":
        return JSONResponse({"error": f"该能力当前不可安装（状态：{p.get('status')}）"}, 409)
    ok, err = plugin_store.install(conn, p["plugin_id"], user)
    if not ok:
        return JSONResponse({"error": err}, 400)
    audit(audit_user(user), "market_install", f"安装市场插件 {kind}:{name}（{p['plugin_id']}）", conn=conn)
    return {"ok": True, "id": p["plugin_id"]}


@router.post("/api/studio/market/{kind}/{name}/publish")
def market_publish(kind: str, name: str, conn=Depends(db_session),
                   user=Depends(require_permission("ai_studio", "publish"))):
    """私人插件 → 公共市场（兼容入口；2026-09-17 P0-3 修复）。

    语义 = 「发布（自用）」+「申请上架」两步，分别交给出受守卫保护的
    publish_self / apply_share 承担：

      · 此前直调 transition(published|submitted)，**只动 status 不动 scope** ——
        scope 恒留在 personal，条目永远进不了市场；若策略为 forced 还会被锁进
        submitted（不可编辑、无撤回入口），是「用户旅程级准死锁」的根因。
      · 现在不再产生 submitted 状态。上架审核只走 scope（pending_public）链路，
        与 /api/plugins/{pid}/share + /{pid}/review 完全同源、同一套守卫。
    """
    if kind not in _VALID_MARKET_KIND:
        return JSONResponse({"error": "kind 必须是 skill|mcp|tool"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p or p.get("scope") != "personal":
        return JSONResponse({"error": f"私人空间不存在该条目（{kind}:{name}）"}, 404)
    if p.get("status") != "published":
        ok, err = plugin_store.publish_self(conn, p["plugin_id"], user)
        if not ok:
            return JSONResponse({"error": err}, 400)
    ok, err = plugin_store.apply_share(conn, p["plugin_id"], user)
    if not ok:
        return JSONResponse({"error": err}, 400)
    # review_policy=auto 且操作者本身是市场管理员时才免审直上（显式治理决定）；
    # 否则一律进入上架审核队列，避免创作者借 auto 策略自批上架。
    row = conn.execute("SELECT value FROM settings WHERE key='plugin_review_policy'").fetchone()
    if (row["value"] if row else "forced") == "auto" and plugin_store.is_market_admin(user):
        ok, err = plugin_store.review_share(conn, p["plugin_id"], user, approve=True,
                                            comment="免审直上（review_policy=auto）")
        if not ok:
            return JSONResponse({"error": err}, 400)
        plugin_store.log_audit(conn, user, p["plugin_id"], "share_apply_auto", {}, "")
    conn.commit()
    audit(audit_user(user), "market_publish", f"发布并申请上架 {kind}:{name}", conn=conn)
    cur = conn.execute("SELECT scope FROM plugins WHERE plugin_id=?", (p["plugin_id"],)).fetchone()
    return {"ok": True, "scope": (cur["scope"] if cur else "pending_public")}


@router.post("/api/studio/market/{kind}/{name}/unpublish")
def market_unpublish(kind: str, name: str, conn=Depends(db_session),
                     user=Depends(require_permission("ai_studio", "market_admin"))):
    """公共市场条目下架（仅市场管理员；legacy 内置种子禁止下架）。

    2026-09-17 P1-2 修复：改走 scope 语义（withdraw_share），**不再改 status**。
    此前实现是 transition(draft) + 手工置 scope=personal，两个问题：
      · disabled 条目下架必失败 —— TRANSITIONS['disabled'] 不含 draft，
        市场管理员面对「已停用但在售」的条目无路可走（P1-2）；
      · 下架连带把能力打成草稿，作者自己也不能用了，与「撤回上架后能力仍可用」
        的既定语义相矛盾。
    """
    if kind not in _VALID_MARKET_KIND:
        return JSONResponse({"error": "kind 必须是 skill|mcp|tool"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p or p.get("scope") != "public":
        return JSONResponse({"error": f"市场不存在该条目（{kind}:{name}）"}, 404)
    if str(p.get("source_ref") or "").startswith("com.zhiyuan.legacy") or p.get("author_name") == "平台内置":
        return JSONResponse({"error": "内置插件禁止下架（平台预置）"}, 400)
    ok, err = plugin_store.withdraw_share(conn, p["plugin_id"], user)
    if not ok:
        return JSONResponse({"error": err}, 400)
    audit(audit_user(user), "market_unpublish", f"市场下架 {kind}:{name}", conn=conn)
    return {"ok": True, "scope": "personal"}


@router.put("/api/studio/market/{kind}/{name}")
def market_update(kind: str, name: str, body: dict = None, conn=Depends(db_session),
                  user=Depends(require_permission("ai_studio", "market_admin"))):
    """市场管理：编辑市场条目（2026-09-17 P1：仅市场管理员；description/category/version/pinned）。"""
    if kind not in _VALID_MARKET_KIND:
        return JSONResponse({"error": "kind 必须是 skill|mcp|tool"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p or p.get("scope") != "public":
        return JSONResponse({"error": f"市场不存在该条目（{kind}:{name}）"}, 404)
    fields = body or {}
    sets, params = [], []
    m = dict(p.get("manifest") or {})
    if "description" in fields:
        m["description"] = str(fields["description"])[:1024]
        sets.append("description=?")
        params.append(str(fields["description"])[:1024])
    if "category" in fields:
        sets.append("category=?")
        params.append(str(fields["category"]))
        m.setdefault("label", {})["category"] = str(fields["category"])
    if "pinned" in fields:
        sets.append("pinned=?")
        params.append(1 if fields["pinned"] else 0)
    if sets:
        sets.append("manifest_json=?")
        params.append(json.dumps(m, ensure_ascii=False))
        params.append(p["plugin_id"])
        conn.execute(f"UPDATE plugins SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE plugin_id=?",
                     params)
        conn.commit()
    audit(audit_user(user), "market_update", f"编辑市场条目 {kind}:{name}", conn=conn)
    return {"ok": True}


@router.post("/api/studio/market/{kind}/{name}/pin")
def market_toggle_pin(kind: str, name: str, conn=Depends(db_session),
                      user=Depends(require_permission("ai_studio", "market_admin"))):
    """市场管理：置顶/取消置顶（2026-09-17 P1：仅市场管理员）。"""
    if kind not in _VALID_MARKET_KIND:
        return JSONResponse({"error": "kind 必须是 skill|mcp|tool"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p or p.get("scope") != "public":
        return JSONResponse({"error": f"市场不存在该条目（{kind}:{name}）"}, 404)
    new_pin = 0 if p.get("pinned") else 1
    conn.execute("UPDATE plugins SET pinned=? WHERE plugin_id=?", (new_pin, p["plugin_id"]))
    conn.commit()
    audit(audit_user(user), "market_pin", f"置顶切换 {kind}:{name}", conn=conn)
    return {"ok": True, "pinned": new_pin}


@router.post("/api/studio/mine/{kind}/{item_id}/pin")
def mine_toggle_pin(kind: str, item_id: str, conn=Depends(db_session), user=Depends(current_user)):
    """我的插件：私人空间条目置顶/取消置顶（数据源：plugins 表，item_id=plugin_id）。"""
    if kind not in ("skill", "mcp"):
        return JSONResponse({"error": "kind 必须是 skill|mcp"}, 400)
    p = _find_plugin_by_id(conn, str(item_id))
    if not p or p.get("scope") != "personal":
        return JSONResponse({"error": "条目不存在或非私人空间条目"}, 404)
    new_pin = 0 if p.get("pinned") else 1
    conn.execute("UPDATE plugins SET pinned=? WHERE plugin_id=?", (new_pin, p["plugin_id"]))
    conn.commit()
    audit(audit_user(user), "mine_pin", f"我的{kind}#{item_id} 置顶切换", conn=conn)
    return {"ok": True, "pinned": new_pin}


@router.post("/api/studio/share/submit")
def share_submit(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """个人插件提交分享（兼容入口；2026-09-17 P0-3 修复）。

    改为委托 store.apply_share —— 只动可见范围 scope（personal→pending_public），
    不再 transition('submitted')。旧实现是「旧审核链」的入口：把 status 打成
    submitted 却不动 scope，条目既进不了市场、又被锁死不可编辑，且让管理员
    在上架审核队列里看到一条永远不会上架的记录（P1-3 双轨混装）。
    """
    kind = str(body.get("kind", ""))
    name = str(body.get("name", "") or body.get("plugin_id", "")
               or body.get("id", "") or "").strip()
    if kind not in ("skill", "mcp", "tool") or not name:
        return JSONResponse({"error": "kind 与 name|plugin_id 必填（skill|mcp|tool）"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p:
        return JSONResponse({"error": f"未找到插件（{kind}:{name}）"}, 404)
    if p.get("status") != "published":
        ok, err = plugin_store.publish_self(conn, p["plugin_id"], user)
        if not ok:
            return JSONResponse({"error": err}, 400)
    ok, err = plugin_store.apply_share(conn, p["plugin_id"], user)
    if not ok:
        return JSONResponse({"error": err}, 400)
    conn.commit()
    audit(audit_user(user), "share_submit", f"提交分享 {kind}:{name}（上架申请）", conn=conn)
    return {"ok": True, "scope": "pending_public"}


@router.post("/api/studio/share/review")
def share_review(body: dict, conn=Depends(db_session),
                 user=Depends(require_permission("ai_studio", "market_admin"))):
    """管理员审核上架申请（仅市场管理员；2026-09-17 双轨合并）。

    主路径委托 store.review_share —— 只动 scope（pending_public→public|personal）。
    仅对历史遗留的 status='submitted' 数据保留旧 transition 分支，且**approve 只
    解决可用性、不代其上架**（要上架须作者另行提交申请），杜绝「通过发布 = 自动上架」。
    """
    kind = str(body.get("kind", ""))
    name = str(body.get("name", "") or body.get("plugin_id", "")
               or body.get("id", "") or "").strip()
    action = str(body.get("action", ""))
    comment = str(body.get("comment", "") or "")
    if kind not in ("skill", "mcp", "tool") or not name:
        return JSONResponse({"error": "kind 与 name|plugin_id 必填（skill|mcp|tool）"}, 400)
    if action not in ("approve", "reject"):
        return JSONResponse({"error": "action 必须是 approve|reject"}, 400)
    p = _find_plugin_by_display(conn, kind, name)
    if not p:
        return JSONResponse({"error": "插件不存在"}, 404)
    if p.get("scope") == "pending_public":
        ok, err = plugin_store.review_share(conn, p["plugin_id"], user,
                                            approve=(action == "approve"), comment=comment)
    elif p.get("status") == "submitted":
        # 历史数据兼容：仅处置可用性，不改可见范围
        target = "published" if action == "approve" else "rejected"
        ok, err = plugin_store.transition(conn, p["plugin_id"], target, user, comment, "combined")
    else:
        return JSONResponse({"error": "该条目不在待审核状态"}, 409)
    if not ok:
        return JSONResponse({"error": err}, 400)
    conn.commit()
    audit(audit_user(user), "share_review",
          f"{'通过' if action == 'approve' else '驳回'} {kind}:{name}：{comment}", conn=conn)
    return {"ok": True}


@router.get("/api/studio/share/items")
def share_items(kind: str = "all", share_status: str = "", conn=Depends(db_session),
                user=Depends(current_user)):
    """上架申请列表（2026-09-17 双轨合并后统一数据源）。

      · 待审核 = scope='pending_public'（新模型；能力在审核期照常可用）
      · 已驳回 = 最近一次上架审核结论为 reject 且当前未重新申请

    旧实现按 status IN ('submitted','rejected') 查 —— 那是「旧发布审核链」的状态，
    新模型下永远不会产生，导致审核视图恒为空（P1-3）。
    """
    if kind not in ("all", "skill", "mcp", "tool"):
        return JSONResponse({"error": "kind 必须是 all|skill|mcp|tool"}, 400)
    _k = "" if kind == "all" else kind
    pend_rows = conn.execute(
        "SELECT * FROM plugins WHERE status!='removed' AND scope='pending_public'"
        " AND (?='' OR type=?) ORDER BY updated_at DESC", (_k, _k)).fetchall()
    # 最近一次上架审核结论（按时间正序覆盖，取最后一条）；兼容旧链 review_type='combined'
    last = {}
    for r in conn.execute(
            "SELECT plugin_id, action FROM plugin_reviews"
            " WHERE review_type IN ('share','combined') ORDER BY id ASC"):
        last[r["plugin_id"]] = r["action"]
    rej_rows = [r for r in conn.execute(
        "SELECT * FROM plugins WHERE status!='removed' AND scope='personal'"
        " AND (?='' OR type=?) ORDER BY updated_at DESC", (_k, _k)).fetchall()
        if last.get(r["plugin_id"]) == "reject"]
    out = []
    for r in list(pend_rows) + rej_rows:
        d = dict(r)
        try:
            d["manifest"] = json.loads(d.get("manifest_json") or "{}")
        except Exception:
            d["manifest"] = {}
        dto = _plugin_to_market_dto(d, user)
        dto["share_status"] = "submitted" if d.get("scope") == "pending_public" else "rejected"
        out.append(dto)
    if share_status in ("submitted", "rejected"):
        out = [x for x in out if x["share_status"] == share_status]
    return {"items": out, "total": len(out)}


@router.get("/api/studio/share/log")
def share_log(conn=Depends(db_session)):
    """审批记录（数据源：plugin_reviews 审核表，倒序）。"""
    rows = conn.execute(
        "SELECT r.*, p.name AS item_name FROM plugin_reviews r LEFT JOIN plugins p ON p.plugin_id=r.plugin_id"
        " ORDER BY r.id DESC LIMIT 100").fetchall()
    return {"items": [dict(r) for r in rows]}
