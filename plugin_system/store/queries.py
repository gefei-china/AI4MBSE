"""读路径：单行/列表查询、DTO 组装、grant 可见性过滤。"""
import json

from plugin_system.manifest import PLUGIN_TYPES  # 权威类型枚举
from plugin_system.store.base import (
    EDITABLE_STATUSES,
    SCOPE_LABELS,
    STATUS_LABELS,
    is_admin,
    is_market_admin,
)


def get_plugin(conn, plugin_id, include_removed=False):
    sql = "SELECT * FROM plugins WHERE plugin_id=?"
    if not include_removed:
        sql += " AND status!='removed'"
    row = conn.execute(sql, (plugin_id,)).fetchone()
    return dict(row) if row else None


def get_plugin_by_name(conn, name):
    row = conn.execute("SELECT * FROM plugins WHERE name=? AND status!='removed'", (name,)).fetchone()
    return dict(row) if row else None


def install_counts(conn) -> dict:
    """真实安装数批量聚合：plugin_id → COUNT(*)。

    2026-09-17 P2：`plugins.install_count` 计数器在历史迁移与派生记录路径下会漂移
    （实测 78 条中 15 条与 plugin_installs 实际行数不符），对外一律以本表为准。
    一次查询聚合全表，避免逐行 COUNT 的 N+1。
    """
    out = {}
    for r in conn.execute(
            "SELECT plugin_id, COUNT(*) AS n FROM plugin_installs GROUP BY plugin_id"):
        out[r["plugin_id"]] = int(r["n"])
    return out


def _sync_install_count(conn, plugin_id):
    """把计数列重算为 plugin_installs 的实际行数（单一事实来源）。

    2026-09-17 P2-2：安装/卸载不再对 install_count 做 ±1 增减 ——
    该计数在「派生记录」（set_install_enabled 会为作者/系统生成行）与历史迁移
    回填的场景下必然漂移（实测真实库 78 条中 15 条与行数不符），而市场列表的
    `ORDER BY install_count DESC` 与卡片展示都读它。改为每次写操作后按
    COUNT(*) 重算，使列与真实行数恒等；对外展示另由 install_counts() 兜底。
    """
    conn.execute(
        "UPDATE plugins SET install_count="
        "(SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?) WHERE plugin_id=?",
        (plugin_id, plugin_id))


def plugin_dto(row, user=None):
    """行 → API 输出（含安装/启用状态、版本历史、审核记录）。"""
    d = dict(row)
    try:
        d["manifest"] = json.loads(d.get("manifest_json") or "{}")
    except Exception:
        d["manifest"] = {}
    d.pop("manifest_json", None)
    d["status_label"] = STATUS_LABELS.get(d.get("status"), d.get("status"))
    d["installed"] = False
    d["enabled"] = None
    if user and user.get("id"):
        inst = user.get("_installs") or {}
        if d["plugin_id"] in inst:
            d["installed"] = True
            d["enabled"] = bool(inst[d["plugin_id"]]["enabled"])
    # 归属与权限标记（2026-09-16 新增）
    #   前端据此按权限渲染动作，避免"按钮看得见、点了才 403/409"的体验断层。
    #   口径与 routers.plugins._author_or_admin / store.soft_delete 保持一致。
    d["is_builtin"] = (d.get("author_id") or 0) == 0
    d["is_mine"] = bool(user and user.get("id") and d.get("author_id") == user.get("id"))
    _adm = is_admin(user)
    _mkt = is_market_admin(user)
    d["is_admin"] = bool(_adm)
    d["is_market_admin"] = bool(_mkt)
    # 编辑/删除（2026-09-17 P1 对齐）：已上架（public）能力的内容修改与删除
    # 已收归市场管理员（update_plugin / soft_delete 守卫同口径），
    # 作者只对非 public 的自有能力看到编辑/删除入口。
    if d.get("scope") == "public":
        d["can_edit"] = bool(_mkt)
        d["can_delete"] = bool(_mkt)
    else:
        d["can_edit"] = bool(d["is_mine"] or _adm)
        d["can_delete"] = bool(d["is_mine"] or _adm)
    d["editable_status"] = d.get("status") in EDITABLE_STATUSES

    # ── 可见范围维度（P1-7 解耦，2026-09-16）──
    # 前端据此渲染三个**互不牵连**的动作：
    #   发布/取消发布（status） · 申请上架/撤回上架（scope） · 启用/停用（installs.enabled）
    _sc = d.get("scope") or "personal"
    _st = d.get("status") or "draft"
    _owner = bool(d["is_mine"] or _adm)
    d["scope_label"] = SCOPE_LABELS.get(_sc, _sc)
    d["is_public"] = (_sc == "public")
    d["pending_share"] = (_sc == "pending_public")
    # 上架申请状态（2026-09-17 双轨合并）：供「我的能力」卡片渲染
    # 「已提交审核 / 已驳回」徽标。此前 DTO 无此字段，前端恒走 else 分支 ——
    # 申请中/被驳回的条目一律显示「⬆ 分享」，用户误以为没提交成功。
    d["share_status"] = ("submitted" if _sc == "pending_public"
                         else "rejected" if _st == "rejected" else "")
    # 发布：草稿/驳回 → 已发布（免审核，不动 scope）
    d["can_publish_self"] = bool(_owner and not d["is_builtin"] and _st in ("draft", "rejected"))
    # 取消发布：已发布且未上架（已上架的须先撤回，避免市场里留"半可用"条目）
    d["can_unpublish_self"] = bool(_owner and not d["is_builtin"]
                                   and _st == "published" and _sc == "personal")
    # 申请上架：仅「已发布 + 私有」才有意义
    d["can_share"] = bool(_owner and not d["is_builtin"]
                          and _sc == "personal" and _st == "published")
    # 撤回上架申请（2026-09-17 P1-1 新增）：作者侧，与 cancel_share 守卫同口径。
    #   此前申请提交后无撤回入口 —— 管理员不在场时审批链条无出口。
    d["can_cancel_share"] = bool(_owner and _sc == "pending_public")
    # 撤回上架（2026-09-17 P1 收权）：仅市场管理员，与 withdraw_share 守卫同口径
    d["can_withdraw_share"] = bool(_mkt and _sc == "public")
    # 审核上架：市场管理员 + 处于申请中（与 review_share 守卫同口径）
    d["can_review_share"] = bool(_mkt and _sc == "pending_public")
    # ── 安装数（2026-09-17 P2 修复：改为实时 COUNT(*)）──
    #   此前直接读 plugins.install_count 计数器。该列在历史迁移（回填系统级安装、
    #   旧表导入）与「派生个人记录不计数」等路径下漂移，实测 78 条中 15 条失真。
    #   计数器仍保留（供排序降级/兼容），但对外输出以 plugin_installs 真实行数为准。
    _cnt = (user or {}).get("_install_counts") if isinstance(user, dict) else None
    if _cnt is not None:
        d["install_count"] = int(_cnt.get(d["plugin_id"], 0))
    return d


def grant_visible(conn, plugin_id, user):
    """授权过滤（设计方案 §4.2 分配范围）：返回是否对 user 可见。

    规则：无 grant 记录 → 默认全员可见（all）；有 grant → 命中任一
    all / team(部门) / role(角色) 即可见。管理员始终可见。
    """
    grants = conn.execute(
        "SELECT * FROM plugin_grants WHERE plugin_id=?", (plugin_id,)).fetchall()
    if not grants:
        return True
    role_name = (user or {}).get("role_name") or ""
    dept = (user or {}).get("department") or ""
    is_admin = bool((user or {}).get("permissions") and (user["permissions"].get("admin")))
    for g in grants:
        if g["target_type"] == "all":
            return True
        if g["target_type"] == "role" and g["target_id"] == role_name:
            return True
        if g["target_type"] == "team" and g["target_id"] == dept:
            return True
    return is_admin


def list_market(conn, user, ptype="", q="", sort="hot"):
    """公共市场列表（published/disabled 的公共插件，按 grant 过滤）。"""
    where = ["scope='public'", "status IN ('published','disabled')"]
    params = []
    if ptype and ptype in PLUGIN_TYPES:
        where.append("type=?")
        params.append(ptype)
    if q:
        where.append("(name LIKE ? OR description LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    sql = ("SELECT * FROM plugins WHERE " + " AND ".join(where))
    if sort == "new":
        sql += " ORDER BY created_at DESC, id DESC"
    else:
        sql += " ORDER BY pinned DESC, install_count DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    # 安装状态批量收集（含系统级 user_id=0）
    # 2026-09-16：内置能力（author_name='平台内置'）已回填系统级安装记录，
    # 使「市场/我安装的」双区模型下内置能力正确显示为已安装。
    # ORDER BY user_id 让系统级(0)先读、个人级后读 —— 个人状态可覆盖系统级。
    installs = {}
    _uid = (user or {}).get("id") if isinstance(user, dict) else None
    if _uid:
        _rows = conn.execute(
            "SELECT plugin_id, enabled FROM plugin_installs WHERE user_id IN (?,0) "
            "ORDER BY user_id", (_uid,)).fetchall()
    else:
        _rows = conn.execute(
            "SELECT plugin_id, enabled FROM plugin_installs WHERE user_id=0").fetchall()
    for r in _rows:
        installs[r["plugin_id"]] = {"enabled": r["enabled"]}
    if isinstance(user, dict):
        user["_installs"] = installs
        user["_install_counts"] = install_counts(conn)
    out = []
    for r in rows:
        d = dict(r)
        if not grant_visible(conn, d["plugin_id"], user):
            continue
        out.append(plugin_dto(d, user))
    return out


def list_mine(conn, user, kind="", q=""):
    """我的能力：我创建的 + 我安装的（含系统级内置）统一视图。

    2026-09-16 修复：原实现只查 author_id=我，「我安装的」形同虚设
    （docstring 声称含安装副本，SQL 却没写），导致前端「我安装的」区为空。
    """
    if not user or not user.get("id"):
        return []
    where = ["status!='removed'",
             "(author_id=? OR EXISTS (SELECT 1 FROM plugin_installs i "
             " WHERE i.plugin_id=plugins.plugin_id AND i.user_id IN (?,0)))"]
    params = [user["id"], user["id"]]
    if kind in PLUGIN_TYPES:
        where.append("type=?")
        params.append(kind)
    if q:
        where.append("(name LIKE ? OR description LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    rows = conn.execute(
        "SELECT * FROM plugins WHERE " + " AND ".join(where) + " ORDER BY id DESC",
        params).fetchall()
    # 安装状态批量收集（含系统级 user_id=0，口径与 list_market 一致）
    installs = {}
    _rows = conn.execute(
        "SELECT plugin_id, enabled FROM plugin_installs WHERE user_id IN (?,0) "
        "ORDER BY user_id", (user["id"],)).fetchall()
    for r in _rows:
        installs[r["plugin_id"]] = {"enabled": r["enabled"]}
    if isinstance(user, dict):
        user["_installs"] = installs
        user["_install_counts"] = install_counts(conn)
    return [plugin_dto(d, user) for d in rows]
