# -*- coding: utf-8 -*-
"""知识库路由分片：本体版本链/快照/发布/校验/绑定/导入导出。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.knowledge_parts.shared import *


@router.get("/api/knowledge/ontology/validate")
def ontology_validate(instances: int = 0, scope: Optional[str] = None, conn=Depends(db_session)):
    """方案 D（2026-08-29）：本体一致性校验接口化。

    返回结构化问题清单（2026-09-23 晚起 12 条规则）：循环继承 / 悬空 parent / 孤立类 /
    关系缺 dom-range / dom-range 悬空 / 重名 / 属性缺适用类型 / 属性适用类型悬空 /
    一侧多声明（info）/ **声明 vs 存量边** / 实例类型未注册 / 存量实例不满足约束。

    · `instances=1`：附带**逐实例**校验（xsd/白名单/端点/基数）——较慢，故不默认；
      逐实例明细来自 `services.ontology_migration` 的既有校验器，不重写。
    · `scope=<类型名>`：只校验该类型（配合 instances 用，缩小扫描面）。
    · 报告带 `degraded`：任何扫描被跳过的原因都会列出来（安静地少查 = 假绿）。
    """
    if instances:
        return _ontology_check(conn, instances=True, scope=scope)
    return _ontology_check(conn)


@router.get("/api/knowledge/ontology/changelog")
def ontology_changelog(type_id: Optional[int] = None, conn=Depends(db_session)):
    """FR-KG-4 补 G7：本体类型变更历史（按类型过滤，倒序；before/after 解析为 dict）。

    只读接口，无需权限；无 type_id 返回全部类型的最近 200 条（2026-09-02：顶部「📜 变更历史」全局入口）。
    """
    if not type_id:
        rows = conn.execute(
            "SELECT * FROM ontology_change_logs ORDER BY id DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ontology_change_logs WHERE type_id=? ORDER BY id DESC", (type_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("before", "after"):
            try:
                d[k] = json.loads(d[k] or "{}")
            except Exception:
                d[k] = {}
        out.append(d)
    return out


@router.get("/api/knowledge/ontology/version")
def ontology_version(conn=Depends(db_session)):
    """本体整体版本（SemVer）：当前版本 + 版本链历史（倒序，含发布状态/快照统计/兼容性/diff）。

    只读接口，无需权限；无版本记录时返回基线 v1.0.0。

    2026-09-08 版本语义收敛：current 优先返回 **active 已发布版本**（消费基线）——
    编辑态只写变更留痕不升版本号；附带 dirty（最新变更留痕晚于基线发布时间 → 有未发布变更）
    与 pending（未发布变更条数），供顶栏徽章/版本历史展示「 vX.Y.Z ·有未发布变更 」。
    """
    rows = conn.execute(
        "SELECT * FROM ontology_versions ORDER BY id DESC LIMIT 50").fetchall()
    out = [dict(r) for r in rows]
    # 2026-09-02 P1：released 版本附 diff 摘要（新增/删除/域值域变更）
    for o in out:
        if o.get("status") == "released":
            o["diff"] = _snapshot_diff(conn, o["id"])
    # current：active released 优先 → 最新 released → 旧语义最新行（存量草稿兼容）→ 基线占位
    cur = next((o for o in out if o.get("active")), None) \
        or next((o for o in out if o.get("status") == "released"), None) \
        or (out[0] if out else None) \
        or {"version_label": "v1.0.0", "major": 1, "minor": 0, "patch": 0,
            "status": "released", "released_at": "", "released_by": ""}
    # 未发布变更检测：变更留痕晚于 current 基线发布时刻（从未发布 → 所有留痕均计未发布）
    try:
        released_at = cur.get("released_at") or ""
        if released_at:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ontology_change_logs WHERE created_at > ?",
                (released_at,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM ontology_change_logs").fetchone()
        pending = int(row["n"] or 0) if row else 0
    except Exception:
        pending = 0
    return {"current": cur, "history": out,
            "dirty": pending > 0, "pending": pending}


@router.get("/api/knowledge/ontology/version/{vid}/snapshot")
def ontology_version_snapshot(vid: int, conn=Depends(db_session)):
    """2026-09-02 P1：查看指定版本的完整快照数据（只读）。

    released 版本返回其不可变快照（ontology_version_snapshots）；未发布（草稿）版本返回
    当前 ontology_types 在该版本写入时刻不可得 → 提示草稿无快照。
    """
    v = conn.execute("SELECT * FROM ontology_versions WHERE id=?", (vid,)).fetchone()
    if not v:
        return JSONResponse({"error": "版本不存在"}, 404)
    if v["status"] != "released":
        return JSONResponse({"error": f"版本 {v['version_label']} 尚未发布，无快照数据（发布后生成不可变快照）"}, 400)
    rows = conn.execute(
        "SELECT type_id AS id, name, type_kind, parent_id, properties, constraints, "
        "description, icon, color FROM ontology_version_snapshots WHERE version_id=? "
        "ORDER BY type_kind, name", (vid,)).fetchall()
    if not rows:
        # 快照治理上线前的存量发布（status=released 但无快照）
        return {"ok": True, "empty": True, "version": dict(v), "types": [],
                "counts": {"entity": 0, "relation": 0, "attribute": 0},
                "note": f"版本 {v['version_label']} 发布于快照治理上线前，无不可变快照数据；"
                        f"可发布当前数据生成新版本（含快照）后查看"}
    types = [dict(r) for r in rows]
    return {"ok": True, "version": dict(v), "types": types,
            "counts": {"entity": sum(1 for t in types if t["type_kind"] == "entity"),
                       "relation": sum(1 for t in types if t["type_kind"] == "relation"),
                       "attribute": sum(1 for t in types if t["type_kind"] == "attribute")}}


@router.post("/api/knowledge/ontology/version/publish")
def ontology_version_publish(body: dict = None, conn=Depends(db_session),
                             user=Depends(require_any_permission(WRITE_PERMS))):
    """2026-09-02 发布语义对齐：主动发布当前编辑数据为新版本（进入版本库，设为消费基线）。

    区别于原 release（把历史日志标记 released）：publish 对当前 ontology_types 生成
    一个全新版本行 + 不可变快照，版本历史即"发布产物列表"。流程：
    ① 一致性检查 high 拦截（防带病发布）
    ② 变更判定（对比上一带快照的已发布版本）：删除/域值域变更 → major（破坏性）；
       新增 → minor；无变更 → 拒绝
    ③ 生成新版本行（change_type=publish）+ 全量快照 + compatible 判定 + active=1
    """
    from datetime import datetime
    # ① 一致性检查（**待发布数据**口径：编辑态即将成为新快照，不能拿旧快照体检 —— 决策 D2 配套）
    chk = _ontology_check(conn, rows=_ont_edit_rows(conn))
    if chk["high"] > 0:
        tops = [i["message"] for i in chk["issues"] if i["severity"] == "high"][:5]
        return JSONResponse({"error": f"一致性检查存在 {chk['high']} 个高危问题，禁止发布：{'；'.join(tops)}"
                                      f"（请在本体页「🩺 一致性检查」按 fix 提示修复后重试）"}, 400)

    cur = conn.execute("SELECT id, name, type_kind, parent_id, properties, constraints, "
                       "description, icon, color, iri FROM ontology_types").fetchall()
    cur_names = {(r["name"], r["type_kind"]) for r in cur}
    added, removed, dom_changed = [], [], 0
    # 2026-09-02 发布说明：body.summary（用户填写，将展示在版本历史/changelog）；未填时自动按 diff 汇总
    body = body or {}
    user_note = str(body.get("summary") or "").strip()
    # 上一带快照的已发布版本（prev 基准；无快照的存量 released 无法对比 → 跳过）
    prev_v = conn.execute(
        "SELECT id, major, minor, patch, version_label FROM ontology_versions "
        "WHERE status='released' AND id IN (SELECT version_id FROM ontology_version_snapshots) "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if prev_v:
        prev_rows = conn.execute(
            "SELECT name, type_kind, constraints FROM ontology_version_snapshots WHERE version_id=?",
            (prev_v["id"],)).fetchall()
        prev_names = {(r["name"], r["type_kind"]) for r in prev_rows}
        removed = sorted({n for n, k in prev_names - cur_names})
        added = sorted({n for n, k in cur_names - prev_names})
        dom_changed = _count_dom_range_changed(prev_rows, cur)
        if not removed and not added and not dom_changed:
            return JSONResponse({"error": f"当前数据与已发布版本 {prev_v['version_label']} 无差异，"
                                          f"无变更可发布（先编辑本体再发布）"}, 400)
        if removed or dom_changed:
            major, minor, patch = prev_v["major"] + 1, 0, 0
            compatible = 0
        else:
            major, minor, patch = prev_v["major"], prev_v["minor"] + 1, 0
            compatible = 1
    else:
        # 存量 released 均无快照 / 无 released：首个快照版本——版本号承接最新版本行
        #（patch+1，不回退 v1.0.0），内容视为全新基线
        latest = conn.execute(
            "SELECT major, minor, patch FROM ontology_versions ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            major, minor, patch = latest["major"], latest["minor"], latest["patch"] + 1
        else:
            major, minor, patch = 1, 0, 0
        compatible = 1
    # 发布说明：用户填写优先；空则自动汇总 diff 为可读描述（changelog 实践）
    auto_parts = []
    if added:
        head = "、".join(added[:5]) + ("…" if len(added) > 5 else "")
        auto_parts.append(f"新增 {len(added)} 类型：{head}")
    if removed:
        head = "、".join(removed[:5]) + ("…" if len(removed) > 5 else "")
        auto_parts.append(f"删除 {len(removed)} 类型：{head}")
    if dom_changed:
        auto_parts.append(f"{dom_changed} 个类型定义域/值域变更")
    summary = user_note or ("；".join(auto_parts) if auto_parts else f"发布当前数据（{len(cur)} 类型）")
    if user_note:
        summary = f"{user_note}（变更：{'；'.join(auto_parts) if auto_parts else '首次发布'}）"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actor = _actor(user)
    label = f"v{major}.{minor}.{patch}"
    dup = conn.execute("SELECT id FROM ontology_versions WHERE version_label=?", (label,)).fetchone()
    if dup:
        major, minor, patch = major, minor, patch + 1  # 避免与既有版本号冲突
        label = f"v{major}.{minor}.{patch}"
    conn.execute(
        "INSERT INTO ontology_versions (version_label, major, minor, patch, change_type, summary, "
        "operator, status, released_at, released_by, snapshot_count, compatible, active, snapshot_created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, major, minor, patch, "publish", summary, actor,
         "released", now, actor, 0, compatible, 0, ""))
    vid = conn.execute("SELECT id FROM ontology_versions WHERE version_label=?", (label,)).fetchone()["id"]
    for r in cur:
        conn.execute(
            "INSERT INTO ontology_version_snapshots "
            "(version_id, type_id, name, type_kind, parent_id, properties, constraints, description, icon, color, iri) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vid, r["id"], r["name"], r["type_kind"], r["parent_id"],
             r["properties"] or "{}", r["constraints"] or "{}",
             r["description"] or "", r["icon"] or "", r["color"] or "#185FA5", r["iri"] or ""))
    conn.execute(
        "UPDATE ontology_versions SET snapshot_count=?, snapshot_created_at=?, active=1 WHERE id=?",
        (len(cur), now, vid))
    conn.execute("UPDATE ontology_versions SET active=0 WHERE id<>? AND status='released'", (vid,))
    conn.commit()
    # 2026-09-14 发布挂钩（L2 迁移链路）：为本批未处理变更留痕 + 历史悬空实例生成迁移计划。
    # 只生成计划（pending），不自动执行——前端发布弹窗展示摘要，dry-run 确认后 apply。
    migration_plan_id = 0
    migration_plan = {"ops": 0, "affected": 0}
    try:
        from services.ontology_migration import build_plan, save_plan
        _plan = build_plan(conn)
        if _plan["ops"]:
            migration_plan_id = save_plan(conn, _plan, version_id=vid, user=user)
            if migration_plan_id:
                migration_plan = {"ops": len(_plan["ops"]), "affected": _plan["affected_total"]}
    except Exception as _e:  # 计划生成失败不阻断发布（迁移可后续手动补建）
        migration_plan = {"ops": 0, "affected": 0, "error": str(_e)}
    audit(actor, "ontology_version_publish",
          f"发布当前数据 → {label}（快照 {len(cur)} 类型，"
          f"{'兼容·自动跟随' if compatible else '⚠️ 破坏性需迁移确认'}）"
          + (f"；迁移计划 #{migration_plan_id}（{migration_plan.get('ops', 0)} op）" if migration_plan_id else ""),
          conn=conn)
    return {"ok": True,
            "version": dict(conn.execute("SELECT * FROM ontology_versions WHERE id=?", (vid,)).fetchone()),
            "snapshot": {"types": len(cur), "compatible": compatible,
                         "check": _check_brief(chk)},
            "diff": {"added": added if prev_v else list(cur_names), "removed": removed if prev_v else [],
                     "dom_changed": dom_changed if prev_v else 0},
            "migration_plan_id": migration_plan_id, "migration_plan": migration_plan}


@router.post("/api/knowledge/ontology/version/activate")
def ontology_version_activate(body: dict = None, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """2026-09-02 P1 回滚：把 active 消费指针切换到指定已发布版本（别名切换，快照不删）。

    行业实践：版本工件不可变，回滚 = active 指向旧版本（消费侧立即读旧快照）。
    """
    body = body or {}
    vid = body.get("version_id")
    if not vid:
        return JSONResponse({"error": "version_id 必填"}, 400)
    row = conn.execute(
        "SELECT * FROM ontology_versions WHERE id=? AND status='released'", (int(vid),)).fetchone()
    if not row:
        return JSONResponse({"error": "仅已发布版本可作为消费基线（先发布）"}, 404)
    if row["active"]:
        return {"ok": True, "version": dict(row), "already_active": True}
    conn.execute("UPDATE ontology_versions SET active=0 WHERE status='released'")
    conn.execute("UPDATE ontology_versions SET active=1 WHERE id=?", (row["id"],))
    conn.commit()
    audit(_actor(user), "ontology_version_activate",
          f"消费基线切换（回滚）→ {row['version_label']}", conn=conn)
    return {"ok": True, "version": dict(conn.execute(
        "SELECT * FROM ontology_versions WHERE id=?", (row["id"],)).fetchone()),
        "already_active": False}


@router.post("/api/knowledge/ontology/version/release")
def ontology_version_release(body: dict = None, conn=Depends(db_session),
                             user=Depends(require_any_permission(WRITE_PERMS))):
    """发布本体版本（2026-09-02 升级为快照治理）：指定版本（缺省=当前最新）→ released 稳定基线。

    P0 快照机制（D1-D3 采纳）：
    ① 一致性检查：high 级问题拦截发布（防带病发布）
    ② 全量快照 ontology_types → ontology_version_snapshots（版本工件不可变，只写不改）
    ③ 兼容性判定：对比上一已发布快照——有删除/改名/域值域变更 → compatible=0（major 需迁移），否则 1（自动跟随）
    ④ active 指针切换到本版本（消费侧以 active 为准）；旧 active 清零
    幂等：已发布版本重复发布直接返回当前状态。
    """
    from datetime import datetime
    body = body or {}
    vid = body.get("version_id")
    if vid is not None:
        row = conn.execute("SELECT * FROM ontology_versions WHERE id=?", (int(vid),)).fetchone()
        if not row:
            return JSONResponse({"error": "版本不存在"}, 404)
    else:
        row = conn.execute("SELECT * FROM ontology_versions ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return JSONResponse({"error": "暂无本体版本可发布（提交本体变更后自动生成版本）"}, 400)
    if row["status"] == "released":
        return {"ok": True, "version": dict(row), "already_released": True}

    # ① 一致性检查：high 拦截（D1 采纳）——**待发布数据**口径（决策 D2 配套，同 publish）
    chk = _ontology_check(conn, rows=_ont_edit_rows(conn))
    if chk["high"] > 0:
        tops = [i["message"] for i in chk["issues"] if i["severity"] == "high"][:5]
        return JSONResponse({"error": f"一致性检查存在 {chk['high']} 个高危问题，禁止发布：{'；'.join(tops)}"
                                      f"（请在本体页「🩺 一致性检查」按 fix 提示修复后重试）"}, 400)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actor = _actor(user)
    # ② 全量快照（事务）
    src = conn.execute("SELECT id, name, type_kind, parent_id, properties, constraints, description, icon, color "
                       "FROM ontology_types").fetchall()
    for r in src:
        conn.execute(
            "INSERT OR IGNORE INTO ontology_version_snapshots "
            "(version_id, type_id, name, type_kind, parent_id, properties, constraints, description, icon, color) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], r["id"], r["name"], r["type_kind"], r["parent_id"],
             r["properties"] or "{}", r["constraints"] or "{}",
             r["description"] or "", r["icon"] or "", r["color"] or "#185FA5"))
    # ③ 兼容性判定：对比上一已发布快照
    prev = conn.execute(
        "SELECT version_id, name, type_kind FROM ontology_version_snapshots "
        "WHERE version_id IN (SELECT id FROM ontology_versions WHERE status='released')").fetchall()
    compatible = 1
    if prev:
        prev_names = {(p["name"], p["type_kind"]) for p in prev}
        cur_names = {(r["name"], r["type_kind"]) for r in src}
        removed = prev_names - cur_names                       # 删除 = 破坏性
        renamed = cur_names & prev_names                       # 重名且 type_kind 相同视为同名（改名=旧名消失+新名出现，已含在 removed/added）
        # 改名检测：新增名 且 删除名 数量对不上 → 保守判不兼容（删除已覆盖）；域值域变更单独判
        dom_changed = _snapshot_dom_range_diff(conn, prev, src)
        if removed or dom_changed:
            compatible = 0
    conn.execute(
        "UPDATE ontology_versions SET status='released', released_at=?, released_by=?, "
        "snapshot_count=?, compatible=?, snapshot_created_at=?, active=1 WHERE id=?",
        (now, actor, len(src), compatible, now, row["id"]))
    # ④ active 指针：其他已发布版本清零（回滚=切 active，快照保留）
    conn.execute("UPDATE ontology_versions SET active=0 WHERE id<>? AND status='released'", (row["id"],))
    conn.commit()
    audit(actor, "ontology_version_release",
          f"本体版本发布: {row['version_label']}（{row['change_type']}）快照 {len(src)} 类型"
          f"{'· 兼容（自动跟随）' if compatible else '· ⚠️ 破坏性变更（需迁移确认）'}", conn=conn)
    return {"ok": True,
            "version": dict(conn.execute("SELECT * FROM ontology_versions WHERE id=?", (row["id"],)).fetchone()),
            "snapshot": {"types": len(src), "compatible": compatible,
                         "check": _check_brief(chk)},
            "already_released": False}


@router.get("/api/knowledge/ontology/graph")
def ontology_graph(conn=Depends(db_session)):
    """KB-P4：本体图谱视图（类型层：EntityType 节点 + RelationType 约束边）。消费侧读已发布快照。"""
    from ontology_semantics import OntologyValidator
    return OntologyValidator(conn, rows=_active_ont_rows(conn)).to_graph()


# ── 2026-09-14 本体变更 → 实例迁移计划端点（L2 链路，见 docs/本体变更实例影响分析与自动迁移方案.md）──

@router.get("/api/knowledge/ontology/migrations")
def ontology_migrations_list(conn=Depends(db_session), user=Depends(current_user)):
    """迁移计划列表（按 plan 倒序；含 pending 统计，供版本历史/治理面板消费）。"""
    from services.ontology_migration import list_plans
    return list_plans(conn)


@router.post("/api/knowledge/ontology/migrations/build")
def ontology_migrations_build(body: dict = None, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """手动补建迁移计划（历史悬空实例补迁 / 发布挂钩失败后重试）。幂等：同留痕同 op 不重复。

    body.target_prefix 可选：只处理目标名以该前缀开头的 op（verify 脚本隔离 / 按类型名族补迁）。
    """
    from services.ontology_migration import build_plan, save_plan
    body = body or {}
    plan = build_plan(conn, target_prefix=str(body.get("target_prefix") or ""))
    if not plan["ops"]:
        return {"ok": True, "plan_id": 0, "ops": 0, "affected": 0,
                "note": "无待迁移实例影响（无未处理留痕、无悬空实例）"}
    plan_id = save_plan(conn, plan, version_id=0, user=user)
    if not plan_id:
        return {"ok": True, "plan_id": 0, "ops": 0, "affected": 0,
                "note": "检出的 op 均已存在于既有计划（幂等去重，未重复生成）"}
    audit(_actor(user), "ontology_migration_build",
          f"手动生成实例迁移计划 #{plan_id}（{len(plan['ops'])} op / 影响 {plan['affected_total']} 条）", conn=conn)
    return {"ok": True, "plan_id": plan_id, "ops": len(plan["ops"]), "affected": plan["affected_total"]}


@router.post("/api/knowledge/ontology/migrations/{plan_id}/dry-run")
def ontology_migrations_dry_run(plan_id: int, conn=Depends(db_session),
                                user=Depends(require_any_permission(WRITE_PERMS))):
    """迁移计划预演：逐 op 重算预计行数 + 抽样 ≤10，不写任何业务表。"""
    from services.ontology_migration import dry_run
    r = dry_run(conn, plan_id)
    if not r.get("ok"):
        return JSONResponse(r, 400)
    audit(_actor(user), "ontology_migration_dry_run",
          f"迁移计划 #{plan_id} 预演：{r['ops']} op / 影响 {r['affected_total']} 条", conn=conn)
    return r


@router.post("/api/knowledge/ontology/migrations/{plan_id}/apply")
def ontology_migrations_apply(plan_id: int, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """确认执行迁移计划：单事务逐 op 回写实例数据；失败整体回滚并标记 failed。"""
    from services.ontology_migration import apply as apply_plan
    r = apply_plan(conn, plan_id, user=user)
    if not r.get("ok"):
        return JSONResponse(r, 400)
    n = len(r.get("applied") or [])
    audit(_actor(user), "ontology_migration_apply",
          f"迁移计划 #{plan_id} 已执行：{n} op（" + "；".join(
            f"{a['op']}:{a['target']} {a['note']}" for a in (r.get("applied") or [])[:8]) + "）", conn=conn)
    return r


@router.get("/api/knowledge/ontology/schema")
def ontology_schema(conn=Depends(db_session)):
    """KB-P4：本体 Schema 文本（供 Agent 语义层注入 system prompt）。消费侧读已发布快照。"""
    from ontology_semantics import OntologyValidator
    return {"schema": OntologyValidator(conn, rows=_active_ont_rows(conn)).schema_text()}


@router.get("/api/knowledge/ontology/shacl")
def ontology_shacl(type_id: Optional[int] = None, conn=Depends(db_session)):
    """P0-3：本体约束 → SHACL 形状（Turtle，W3C 标准，PySHACL/Jena 兼容，含子类继承）。

    2026-09-02 四轮调整：type_id 可选——节点级视图只输出所选类型及其直接语境的形状。
    """
    from ontology_semantics import OntologyValidator
    from fastapi.responses import Response
    rows = _active_ont_rows(conn)
    if type_id:
        rows = _filter_ont_rows_for_type(rows, type_id)
    return Response(OntologyValidator(conn, rows=rows).shacl_export(), media_type="text/turtle")


@router.get("/api/knowledge/ontology/binding")
def ontology_binding(branch: str = "", conn=Depends(db_session)):
    """A6 数据绑定视图（对齐 Playground Data Binding）：每个实体类型的实例数/来源/状态分布。
    默认统计「全分支 · 非废弃实例」合计（本体为全局共享 Schema）；传入具体 branch 时仅统计该分支。"""
    fil, params = "", []
    if branch and branch not in ("all", "global"):
        fil = " AND e.branch=?"
        params = [branch]
    rows = conn.execute("""
        SELECT e.entity_type, COUNT(*) AS total,
               SUM(CASE WHEN e.status='reviewed' THEN 1 ELSE 0 END) AS reviewed,
               SUM(CASE WHEN e.status='candidate' THEN 1 ELSE 0 END) AS candidate,
               COUNT(DISTINCT e.source_doc) AS source_docs,
               COUNT(DISTINCT e.branch) AS branches
        FROM entities e WHERE e.status!='deprecated'""" + fil + " GROUP BY e.entity_type", params).fetchall()
    types = conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()
    stat = {r["entity_type"]: dict(r) for r in rows}
    out = []
    for t in types:
        tn = t["name"]
        s = stat.get(tn) or {"entity_type": tn, "total": 0, "reviewed": 0, "candidate": 0,
                             "source_docs": 0, "branches": 0}
        out.append({"entity_type": tn, "scope": branch if branch and branch not in ("all", "global") else "all_branches", **s})
    return {"bindings": out, "total_entities": sum(o["total"] for o in out),
            "ontology_type_count": len(out)}


@router.get("/api/knowledge/ontology/export")
def ontology_export(fmt: str = "owl", type_id: Optional[int] = None, conn=Depends(db_session)):
    """O-2：导出本体为 W3C 标准序列化。
    fmt=owl → OWL/RDF-XML（供 Protégé 使用）
    fmt=ttl → Turtle（RDF 三元组文本语法，W3C 标准）
    2026-09-02 四轮调整：type_id 可选——节点级 OWL 视图只导出所选类型及其直接语境。
    """
    from ontology_owl import to_owl, to_turtle
    from fastapi.responses import Response
    rows = _active_ont_rows(conn)   # 2026-09-02 消费侧以已发布快照为准
    if type_id:
        rows = _filter_ont_rows_for_type(rows, type_id)
    if fmt == "ttl":
        text, warns = to_turtle(conn, rows=rows)
    else:
        xml, warns = to_owl(conn, rows=rows)
        text = xml
    # 方案 A3（2026-08-29）：导出完整性警告随响应头返回（X-Ont-Warnings），前端可 toast 提示
    headers = {"Content-Disposition": f"attachment; filename=ontology.{'ttl' if fmt == 'ttl' else 'owl'}"}
    if warns:
        # HTTP 头仅允许 latin-1：中文警告须 URL 编码（同 doc_download Content-Disposition 教训）
        from urllib.parse import quote as _q
        headers["X-Ont-Warnings"] = _q(f"{len(warns)}; " + " | ".join(warns[:3])[:400])
    return Response(text, media_type="text/turtle" if fmt == "ttl" else "application/rdf+xml",
                    headers=headers)


@router.post("/api/knowledge/ontology/import")
def ontology_import(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """O-2：导入本体（OWL/RDF-XML 或 Turtle），幂等：同名类型跳过。

    body.fmt: owl (默认) | ttl
    body.owl / body.content / body.ttl: 导入文本
    """
    from ontology_owl import from_owl, from_turtle
    fmt = (body.get("fmt") or "owl").lower()
    raw = body.get("content") or body.get("owl") or body.get("ttl") or ""
    if not raw:
        return JSONResponse({"error": "导入内容不能为空"}, 400)
    if fmt == "ttl":
        result = from_turtle(raw, conn)
    else:
        result = from_owl(raw, conn)
    # 本体版本：OWL 导入新增类型 = minor 递增（仅实际新增才递增）
    # 方案 D：导入后自动一致性校验，结果随响应返回
    try:
        vrows = conn.execute("SELECT id, name, parent_id FROM ontology_types").fetchall()
        vby = {str(r["id"]): r for r in vrows}
        result["warnings"] = [f"悬空父类：{r['name']}（parent {r['parent_id']} 不存在）"
                              for r in vrows if r["parent_id"] and str(r["parent_id"]) not in vby]
    except Exception:
        pass
    # 2026-09-08 版本语义收敛：OWL 导入属编辑态，只留 audit/变更记录，不升版本号（发布时统一升级）
    audit(_actor(user), "ontology_import", f"OWL 导入: 新增 {result['imported']} / 跳过 {result['skipped']}", conn=conn)
    return result
