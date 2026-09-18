"""插件数据访问层：plugins 8 表 CRUD + 状态机 + 授权过滤（对齐设计方案 §4.4/§5.3）。

状态机（对齐 §4.4）：
    draft → private           个人发布（仅自己可见可用）
    draft/private → submitted 提交共享（进审核流）
    submitted → published     审核通过（公共市场）
    submitted → rejected      审核拒绝（退回 draft）
    published → disabled → enabled   管理员停用/恢复
    published/private → deprecated → removed  下架/软删除（审计保留）
"""
import json
import os
import sqlite3
from datetime import datetime

from core.config import BASE_DIR
from skills.parser import parse_frontmatter  # P2-2：frontmatter 解析唯一实现
from plugin_system.manifest import PLUGIN_TYPES, parse_dependencies  # 权威类型枚举 + 依赖契约解析

# 插件产物目录（SKILL.md / server.json 存放处；zip 上传 P2 解压于此）
PLUGIN_DIR = os.path.join(BASE_DIR, "data", "plugins")

# 允许的状态与目标状态映射（2026-08-31 收敛为 6 态：删除 private/enabled/deprecated 兼容态）
#   草稿 → 提交审核 → 已发布 → 已停用 / 已驳回 / 已删除
TRANSITIONS = {
    # 2026-09-16（P1-7 解耦）：draft/rejected 可直接到 published = 「发布（自用）」，**免审核**。
    # 审核只保留给「上架公共市场」（走 scope，见下方 SHARE_TRANSITIONS）。
    #
    # 2026-09-17（P2-4 校准）：本表是 status 维度的**合法迁移表**（can_transition 据此校验），
    # 而非「调用方清单」。下列边无 transition() 调用方，已逐条标注 —— 评估可达性时勿当实操路径。
    "draft": {"published", "submitted", "rejected", "removed"},
    #          ^published=publish_self  ^submitted/^rejected/^removed=未使用（见下方说明）
    "submitted": {"published", "rejected", "draft", "removed"},
    #             ^published/^rejected=遗留数据审核（/review 兼容分支）
    #             ^draft=unpublish_self「撤回提交」（2026-09-17 P1-1 补：关闭准死锁）
    #             ^removed=未使用
    "rejected": {"draft", "published", "submitted", "removed"},
    #            ^published=publish_self  ^draft/^submitted/^removed=未使用
    "published": {"disabled", "draft", "removed"},
    #             ^disabled=set_enabled 直写（不经 transition）  ^draft=unpublish_self  ^removed=未使用
    "disabled": {"published", "removed"},
    #            ^published=publish_self（自用）/ set_enabled 直写  ^removed=未使用
    "removed": set(),
    # ──────────────────────────────────────────────────────────────────
    # 「未使用」边说明（2026-09-17 双轨合并 + P2-4 校准）：
    #   · * → removed   软删除由 soft_delete() 直接 UPDATE（含清理 installs/scope），
    #                   不经 transition()，故这些边不会被 can_transition 校验。
    #                   removed 仍是事实终态（get_plugin() 默认排除），只是不可经 transition 到达。
    #   · draft|rejected → submitted
    #                   旧「发布需审核」链的入口。双轨合并后新代码不再产生 submitted
    #                   （上架审核统一走 scope=pending_public），保留仅为旧数据兼容。
    #   · draft ↔ rejected
    #                   无任何编辑/审核路径使用；保留以免 rendered 可达图缺失节点，
    #                   但前端不提供该迁移入口。
    # 保留上述边而非删除的原因：可达性分析依赖表结构完整（删 disabled 相关边会使
    # disabled 从 draft 不可达，得出「存在不可达状态」的错误结论）。
    # ──────────────────────────────────────────────────────────────────
}

STATUS_LABELS = {
    "draft": "草稿", "submitted": "待审核", "rejected": "已驳回",
    "published": "已发布", "disabled": "已停用", "removed": "已删除",
}

# ══════════════════════════════════════════════════════════════════════
# 可见范围（scope）与生命周期（status）正交治理（2026-09-16 P1-7）
#
# 问题：此前 transition() 在 target=='published' 时强制 scope='public'，
#   把「我能用」「我发布」「全团队可见」三件事焊死 ——
#   后果是自建能力想被 AI 消费就必须上架公共市场。
#
# 现在拆成三个互不牵连的维度：
#   status                     能力是否可用（draft / published / disabled …）
#   scope                      谁看得见、装得上（personal / pending_public / public）
#   plugin_installs.enabled    我个人用不用它
#
# 动作与维度的对应（一动作只动一个维度）：
#   发布 / 取消发布   → 只动 status（publish_self / unpublish_self）
#   申请上架 / 审核 / 撤回 → 只动 scope（apply_share / review_share / withdraw_share）
#   启用 / 停用       → 只动 plugin_installs.enabled（set_install_enabled）
# ══════════════════════════════════════════════════════════════════════
SCOPES = ("personal", "pending_public", "public")
SCOPE_LABELS = {"personal": "仅自己", "pending_public": "申请上架中", "public": "已上架市场"}
SHARE_TRANSITIONS = {
    "personal": {"pending_public"},            # 申请上架（需审核）
    "pending_public": {"public", "personal"},  # 审核通过 / 驳回
    "public": {"personal"},                    # 撤回上架（免审核）
}

# 可编辑状态（2026-09-16 修复）
#   此前仅 (draft, rejected)，但错误文案已承诺「个人已发布可改」，两者矛盾，
#   导致全部 published 的内置能力（19 个 agent / 23 个 tool …）编辑恒失败 409。
#   submitted（审核中）与 removed（已删除）仍不可编辑：前者冻结待审版本，后者已软删。
EDITABLE_STATUSES = ("draft", "rejected", "published", "disabled")

# 作者可「自行发布（免审核）」的来源状态白名单（2026-09-17 P0 补丁）。
#   刻意排除 submitted —— 待审核态只能由市场管理员经 /review 裁决，
#   否则 publish_self 就成了「作者自批」的后门（submitted→published 在
#   TRANSITIONS 中合法，光查迁移表拦不住）。
_SELF_PUBLISHABLE_FROM = ("draft", "rejected", "disabled")


def is_admin(user):
    """管理员判定（统一口径，供 store 与 router 共用）。

    口径与 routers.plugins._as_admin 一致：admin 域权限非空，或 ai_studio 含 publish。
    2026-09-16 修复：此前 set_enabled / soft_delete 各自用 bool(permissions["admin"])，
    口径更严 —— 设计师（ai_studio 含 publish、admin 为空）点「停用」会被 403，
    与其在编辑/删除上被认可的管理员身份自相矛盾。

    ⚠️ 2026-09-17（P1 权限拆分）起：is_admin 保留给「系统级/内容级」操作
    （编辑/删除他人个人能力、个人插件启停等）；**市场治理动作**
    （上架审核、公共能力全局启停、撤回上架、已上架能力改删、grant）
    一律改用 is_market_admin，创作者（publish）不再自动享有市场治理权。
    """
    if not user:
        return False
    perms = user.get("permissions") or {}
    if perms.get("admin"):
        return True
    return "publish" in (perms.get("ai_studio") or [])


def is_market_admin(user):
    """市场治理管理员判定（2026-09-17 P1，权限点拆分）。

    口径：admin 域权限非空（超级用户放行，与 require_permission 同理），
    或 ai_studio 含 **market_admin** 权限点。
    ai_studio:publish（能力创作者）被刻意排除 —— 对齐「市场数据由专门
    管理人员管理」的治理目标：创作者可创作、发布自用、申请上架，
    但不能审核上架、不能全局启停/下架/删除已上架能力、不能分配授权。

    迁移提示：需为承担市场治理的角色在权限矩阵中配置 ai_studio:market_admin；
    未配置前，只有 admin 域用户能执行市场治理动作。
    """
    if not user:
        return False
    perms = user.get("permissions") or {}
    if perms.get("admin"):
        return True
    return "market_admin" in (perms.get("ai_studio") or [])


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_plugin_dir(plugin_id):
    d = os.path.join(PLUGIN_DIR, plugin_id)
    os.makedirs(d, exist_ok=True)
    return d


def can_transition(status, target):
    return target in TRANSITIONS.get(status, set())


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


def create_plugin(conn, normalized, user, scope="personal", status="draft"):
    """创建插件（manifest 已校验）。返回 (ok, plugin, error)。"""
    plugin_id = normalized["id"]
    if get_plugin(conn, plugin_id, include_removed=True):
        return False, None, f"插件 id 已存在: {plugin_id}"
    # 同一作者下 name 唯一（个人空间隔离，不得覆盖公共插件同名）
    exist = conn.execute(
        "SELECT id FROM plugins WHERE name=? AND status!='removed'", (normalized["name"],)).fetchone()
    if exist:
        return False, None, f"插件 name 已被占用: {normalized['name']}（个人空间内唯一）"
    author_name = (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
    author_id = (user or {}).get("id") or 0
    _ensure_plugin_dir(plugin_id)
    conn.execute(
        """INSERT INTO plugins
           (plugin_id, name, namespace, type, scope, status, current_version, manifest_json,
            author_id, author_name, icon, category, description)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (plugin_id, normalized["name"], "personal", normalized["type"], scope, status,
         normalized["version"], json.dumps(normalized, ensure_ascii=False),
         author_id, author_name, normalized.get("icon", ""), _category_of(normalized),
         normalized["description"]))
    conn.execute(
        """INSERT INTO plugin_versions (plugin_id, version, manifest_json, status)
           VALUES (?,?,?,?)""",
        (plugin_id, normalized["version"], json.dumps(normalized, ensure_ascii=False),
         "published" if status in ("published",) else "submitted"))
    conn.commit()
    _sync_deps_safe(conn, plugin_id, normalized)
    return True, get_plugin(conn, plugin_id), None


def _category_of(normalized):
    label = normalized.get("label") or {}
    cat = label.get("category", "")
    return cat or "未分类"


def update_plugin(conn, plugin_id, normalized, user):
    """更新清单（可编辑状态见 EDITABLE_STATUSES；版本号变化则新建版本记录）。

    2026-09-16 修复：
      ① 状态白名单从 (draft, rejected) 放宽到 EDITABLE_STATUSES，
         兑现"个人已发布可改"的原有承诺（此前 published 恒被拒，错误文案自相矛盾）。
      ② 补 store 层权限纵深防御：仅作者或管理员（router 层已有同样守卫）。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "插件不存在"
    # 首道守卫：作者 / 系统管理员 / （public 时）市场管理员 —— 顺序很重要：
    # 市场管理员既非作者也未必满足 is_admin，须在 public 分支前放行，
    # 再由下方 public 专属检查决定其能否直改（作者/设计师在此被拒）。
    if not (user and (user.get("id") == p.get("author_id") or is_admin(user)
                      or (p["scope"] == "public" and is_market_admin(user)))):
        return False, "仅作者或管理员可编辑该能力"
    if p["status"] not in EDITABLE_STATUSES:
        return False, (f"当前状态「{STATUS_LABELS.get(p['status'], p['status'])}」不允许编辑"
                       f"（审核中与已删除不可改）")
    # 2026-09-17（P1，市场治理收权）：已上架（public）能力的内容修改仅市场管理员可直改。
    # 此前作者可直改上架能力的 manifest —— 等于绕过审核向全市场推送新版本。
    # 作者合规路径：请市场管理员撤回上架（scope→personal）→ 正常编辑 →
    # 重新 apply_share 走上架审核。
    if p["scope"] == "public" and not is_market_admin(user):
        return False, ("已上架市场的能力内容修改需市场管理员操作（避免绕过审核向全市场推新版）；"
                       "请联系管理员撤回上架后修改，再重新申请上架审核")
    old_v = p["current_version"]
    new_v = normalized["version"]
    # 版本只能增（同版本原地更新不产生新版本记录；版本号变更需 semver 更大或全新）
    if new_v != old_v:
        exist = conn.execute(
            "SELECT id FROM plugin_versions WHERE plugin_id=? AND version=?", (plugin_id, new_v)).fetchone()
        if exist:
            return False, f"版本 {new_v} 已存在（版本只增不删）"
        conn.execute(
            "INSERT INTO plugin_versions (plugin_id, version, manifest_json, status) VALUES (?,?,?,?)",
            (plugin_id, new_v, json.dumps(normalized, ensure_ascii=False),
             "published" if p["status"] in ("published",) else "submitted"))
    conn.execute(
        """UPDATE plugins SET name=?, type=?, manifest_json=?, current_version=?,
           icon=?, category=?, description=?, updated_at=?
           WHERE plugin_id=?""",
        (normalized["name"], normalized["type"], json.dumps(normalized, ensure_ascii=False),
         new_v, normalized.get("icon", ""), _category_of(normalized),
         normalized["description"], _now(), plugin_id))
    conn.commit()
    _sync_deps_safe(conn, plugin_id, normalized)
    return True, None


# ── P0-2：能力依赖索引（2026-09-16）──
# 依赖契约的解析（parse_dependencies）定义于 plugin_system.manifest —— manifest 是契约层，
# store 是持久层，依赖方向单向，避免循环 import。此处只负责落库与查询。


def _sync_deps_safe(conn, plugin_id: str, manifest: dict):
    """写依赖索引的内层封装：表缺失（未跑迁移的老库）时静默降级，绝不阻断主流程。"""
    try:
        sync_dependencies(conn, plugin_id, manifest)
    except sqlite3.OperationalError:
        pass


def _sync_legacy_safe(conn, plugin_id: str, available: bool):
    """plugins → 旧表可用性同步（同步桥方向 B，2026-09-16）。

    为什么必须有：AI 运行时读的是旧表（skills/tools/mcp_servers/agents/prompts），
    能力中心改的却是 plugins。缺了这一步，就会出现
    「管理员在能力中心下架了能力，AI 照旧调用」的安全隐患。

    - 只动旧表的可用性开关，不碰业务字段
    - 无 legacy 映射的插件（纯插件能力）内部会安全跳过
    - 任何异常一律吞掉：管理动作不应因同步失败而失败
    """
    try:
        from plugin_system.legacy_sync import sync_to_legacy_safe
        sync_to_legacy_safe(conn, plugin_id, available)
    except Exception:
        pass


def sync_dependencies(conn, plugin_id: str, manifest: dict) -> int:
    """按 manifest 重建该插件的依赖边（先删后插，幂等）。返回写入行数。"""
    rows = parse_dependencies(manifest)
    conn.execute("DELETE FROM plugin_dependencies WHERE consumer_id=?", (plugin_id,))
    for r in rows:
        resolved = 0
        if r["provider_id"]:
            hit = conn.execute(
                "SELECT 1 FROM plugins WHERE plugin_id=? AND status!='removed'",
                (r["provider_id"],)).fetchone()
            resolved = 1 if hit else 0
        conn.execute(
            """INSERT OR REPLACE INTO plugin_dependencies
               (consumer_id, provider_id, provider_ref, kind, required, resolved)
               VALUES (?,?,?,?,?,?)""",
            (plugin_id, r["provider_id"], r["provider_ref"], r["kind"], r["required"], resolved))
    conn.commit()
    return len(rows)


def list_dependencies(conn, plugin_id: str) -> list:
    """我依赖谁（安装依赖解析用）。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_dependencies WHERE consumer_id=? ORDER BY kind, id",
        (plugin_id,)).fetchall()]


def list_dependents(conn, provider_id: str, required_only: bool = False) -> list:
    """谁依赖我（卸载前置检查入口）。"""
    sql = "SELECT * FROM plugin_dependencies WHERE provider_id=?"
    if required_only:
        sql += " AND required=1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", (provider_id,)).fetchall()]


def dependency_impact(conn, plugin_id: str) -> dict:
    """停用/卸载前置检查：返回受影响消费者清单。

    - safe=True   无硬依赖，可直接停用
    - safe=False  blocking 非空，须二次确认（UX 层应列出完整 consumer 清单）
    返回结果可直接喂给 services/impact_engine 做可视化。
    """
    blocking, optional = [], []
    for r in list_dependents(conn, plugin_id):
        p = get_plugin(conn, r["consumer_id"])
        item = {"plugin_id": r["consumer_id"],
                "name": (p or {}).get("name") or r["consumer_id"],
                "status": (p or {}).get("status") or "",
                "kind": r["kind"], "provider_ref": r["provider_ref"]}
        (blocking if r["required"] else optional).append(item)
    return {"blocking": blocking, "optional": optional, "safe": not blocking}


def transition(conn, plugin_id, target, user, comment="", review_type="combined"):
    """状态流转（核心守卫）：submitted→published/rejected 写审核记录；其余仅流转。

    P1-7（2026-09-16）解耦：只治理 status（可用性），**不再改写 scope（可见范围）**。
    可见范围的变更走 apply_share / review_share / withdraw_share。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "插件不存在"
    if not can_transition(p["status"], target):
        return False, f"非法状态流转: {p['status']} → {target}"
    reviewer_name = (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
    reviewer_id = (user or {}).get("id") or 0
    if p["status"] == "submitted" and target in ("published", "rejected"):
        conn.execute(
            """INSERT INTO plugin_reviews (plugin_id, version, reviewer_id, reviewer_name,
               action, comment, review_type) VALUES (?,?,?,?,?,?,?)""",
            (plugin_id, p["current_version"], reviewer_id, reviewer_name,
             "approve" if target == "published" else "reject", comment, review_type))
    if target == "published":
        # P1-7 解耦：只改 status。此前这里强制 scope='public'，
        # 使「发布给自己用」被迫等于「上架给全团队」。
        conn.execute("UPDATE plugins SET status='published', updated_at=? WHERE plugin_id=?",
                     (_now(), plugin_id))
        conn.execute(
            "UPDATE plugin_versions SET status='published' WHERE plugin_id=? AND version=?",
            (plugin_id, p["current_version"]))
    else:
        conn.execute("UPDATE plugins SET status=?, updated_at=? WHERE plugin_id=?",
                     (target, _now(), plugin_id))
    conn.commit()
    # 同步桥：仅 published 令旧表可用（AI 运行时据此判定能否调用）
    _sync_legacy_safe(conn, plugin_id, target == "published")
    conn.commit()
    return True, None


# ══════════════════════════════════════════════════════════════════════
# 可见范围治理（P1-7，2026-09-16）—— 与 status（可用性）正交
#
# 五个动作各自只动一个维度，互不牵连：
#   publish_self    发布（自用）     status → published      免审核
#   unpublish_self  取消发布         status → draft
#   apply_share     申请上架         scope  personal→pending_public   需审核
#   review_share    审核上架         scope  pending_public→public|personal
#   withdraw_share  撤回上架         scope  public→personal   免审核
# ══════════════════════════════════════════════════════════════════════

def _is_owner(p, user):
    """作者或管理员（与 routers.plugins._author_or_admin 同口径）。"""
    if not user or not user.get("id"):
        return False
    if p and p.get("author_id") and int(p["author_id"]) == int(user["id"]):
        return True
    return is_admin(user)


def publish_self(conn, plugin_id, user):
    """发布（自用）：让能力可被 AI 消费。**不改可见范围，无需审核**。

    与旧流程的差别：旧 publish 走 draft→submitted 审核，而审核通过又强制上架 ——
    等于「我自己要用」必须付出「公开给全团队」的代价。现在两者彻底分开。

    ⚠️ 2026-09-17（P0 补丁，见《插件管理状态机与缺陷验证》）：
      ① 来源状态白名单 —— 此前只查 TRANSITIONS，而 submitted→published 在表内合法，
         导致作者可「自批」绕过上架审核（线上 plugin_review_policy=forced 形同虚设）。
      ② scope 守卫 —— 此前不校验可见范围，作者可一键 publish 撤销市场管理员的
         全局停用（disabled→published），把市场治理收权整个绕开。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "能力不存在"
    if p["status"] == "published":
        return False, "该能力已是发布状态"
    if p["status"] == "removed":
        return False, "已删除的能力不可发布"
    # ① 来源状态白名单：仅「自己可控的状态」可发布（免审核）
    #    submitted 必须由市场管理员走 /review 裁决，作者不得自批。
    if p["status"] not in _SELF_PUBLISHABLE_FROM:
        return False, (f"当前状态（{STATUS_LABELS.get(p['status'], p['status'])}）不可直接发布；"
                       "待审核能力须由市场管理员裁决")
    # ② 已上架条目的「恢复发布」= 全局解除停用，属市场治理动作，收归市场管理员
    if p["scope"] == "public" and not is_market_admin(user):
        return False, "已上架市场的能力，恢复发布（全局启用）仅市场管理员可操作"
    if not _is_owner(p, user):
        return False, "仅作者或管理员可发布"
    ok, err = transition(conn, plugin_id, "published", user,
                         comment="发布（自用，免审核）", review_type="self")
    if not ok:
        return False, err
    conn.execute("UPDATE plugin_versions SET status='published' WHERE plugin_id=? AND version=?",
                 (plugin_id, p["current_version"]))
    conn.commit()
    return True, None


def unpublish_self(conn, plugin_id, user):
    """取消发布：回到草稿，AI 不再可消费（同步桥写回旧表不可用）。

    2026-09-17（P1-1 修复）：上架申请中（scope='pending_public'）时一并撤下申请。
    此前只改 status，留下 `pending_public + draft` 孤儿态 —— 该组合既无法编辑
    （draft 可编辑但 scope 卡在待审），又让管理员在审核队列里看到一个草稿条目。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "能力不存在"
    # ── 遗留 submitted 态：撤回提交（2026-09-17 P1-1 补，关闭准死锁 #1）────────
    # 双轨合并后新代码不再产生 submitted；但历史数据若停留在此态，作者既不能编辑
    # （EDITABLE_STATUSES 不含 submitted）、也不能撤回，只能等管理员裁决 ——
    # 管理员不在场时等同卡死。现允许作者自行撤回提交，回到 draft 后即可修改。
    if p["status"] == "submitted":
        if not _is_owner(p, user):
            return False, "仅作者或管理员可撤回提交"
        ok, err = transition(conn, plugin_id, "draft", user, comment="撤回提交")
        if not ok:
            return False, err
        conn.commit()
        return True, None
    if p["status"] != "published":
        return False, "当前不是已发布状态"
    if p["scope"] == "public":
        return False, "已上架市场，请先撤回上架再取消发布"
    if not _is_owner(p, user):
        return False, "仅作者或管理员可取消发布"
    ok, err = transition(conn, plugin_id, "draft", user, comment="取消发布")
    if not ok:
        return False, err
    if p["scope"] == "pending_public":
        # 连带撤下上架申请，避免 pending_public + draft 孤儿态
        conn.execute("UPDATE plugins SET scope='personal', updated_at=? WHERE plugin_id=?",
                     (_now(), plugin_id))
        conn.commit()
    return True, None


def cancel_share(conn, plugin_id, user):
    """撤回上架申请：scope pending_public → personal（作者侧，免审核）。

    2026-09-17（P1-1 修复）新增。此前作者提交上架申请后**没有任何撤回入口** ——
    申请一旦提交就只能等管理员裁决，管理员不在场时等同卡死
    （「用户旅程级准死锁」）。status 不变，能力对我始终可用。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "能力不存在"
    if p["scope"] != "pending_public":
        return False, "该能力没有进行中的上架申请"
    if not _is_owner(p, user):
        return False, "仅作者或管理员可撤回上架申请"
    conn.execute("UPDATE plugins SET scope='personal', updated_at=? WHERE plugin_id=?",
                 (_now(), plugin_id))
    conn.execute(
        """INSERT INTO plugin_reviews (plugin_id, version, reviewer_id, reviewer_name,
           action, comment, review_type) VALUES (?,?,?,?,?,?,?)""",
        (plugin_id, p["current_version"], (user or {}).get("id") or 0,
         (user or {}).get("display_name") or (user or {}).get("username") or "未登录",
         "withdraw", "作者撤回上架申请", "share"))
    conn.commit()
    return True, None


def apply_share(conn, plugin_id, user):
    """申请上架公共市场：scope personal → pending_public。

    **只动可见范围，不动 status** —— 申请审核期间能力照常可用。
    旧流程用 status='submitted' 表达待审，会让已在用的能力在审核期"消失"。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "能力不存在"
    if p["scope"] == "public":
        return False, "该能力已上架市场"
    if p["scope"] == "pending_public":
        return False, "已提交上架申请，等待审核"
    if p["status"] != "published":
        return False, "请先「发布」（让能力可用），再申请上架"
    if not _is_owner(p, user):
        return False, "仅作者或管理员可申请上架"
    conn.execute("UPDATE plugins SET scope='pending_public', updated_at=? WHERE plugin_id=?",
                 (_now(), plugin_id))
    conn.execute(
        """INSERT INTO plugin_reviews (plugin_id, version, reviewer_id, reviewer_name,
           action, comment, review_type) VALUES (?,?,?,?,?,?,?)""",
        (plugin_id, p["current_version"], (user or {}).get("id") or 0,
         (user or {}).get("display_name") or (user or {}).get("username") or "未登录",
         "apply", "申请上架公共市场", "share"))
    conn.commit()
    return True, None


def review_share(conn, plugin_id, user, approve=True, comment=""):
    """审核上架申请：pending_public → public（通过）/ personal（驳回）。

    2026-09-17（P1-1 修复）：approve 增加 status 校验。
    此前不校验 status，一份已是 draft 的条目（例如作者在申请期间点了「取消发布」，
    而旧代码未连带撤下申请留下的孤儿态）仍能被审核「复活上架」——市场里出现
    scope=public + status=draft 的矛盾条目，既不可消费又占位在售清单。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "能力不存在"
    if p["scope"] != "pending_public":
        return False, "该能力不在上架审核中"
    if not is_market_admin(user):
        return False, "仅市场管理员可审核上架"
    if approve and p["status"] != "published":
        return False, (f"该能力当前为「{STATUS_LABELS.get(p['status'], p['status'])}」，"
                       "不满足上架条件（上架要求能力处于「已发布」）")
    target = "public" if approve else "personal"
    conn.execute(
        """INSERT INTO plugin_reviews (plugin_id, version, reviewer_id, reviewer_name,
           action, comment, review_type) VALUES (?,?,?,?,?,?,?)""",
        (plugin_id, p["current_version"], (user or {}).get("id") or 0,
         (user or {}).get("display_name") or (user or {}).get("username") or "未登录",
         "approve" if approve else "reject", comment or "", "share"))
    conn.execute("UPDATE plugins SET scope=?, updated_at=? WHERE plugin_id=?",
                 (target, _now(), plugin_id))
    conn.commit()
    return True, None


def withdraw_share(conn, plugin_id, user):
    """撤回上架：public → personal（2026-09-17 P1 起仅市场管理员）。

    status 不变 —— 能力对管理员侧仍可用，只是别人看不到、装不上了。

    收权说明（对齐「市场数据由专门管理人员管理」）：
      · 此前作者可单方面撤回上架 —— 市场条目立即消失，波及所有已安装用户；
      · 现作者不可直接撤回，合规路径：联系市场管理员撤回 →（scope=personal）
        作者可正常编辑/取消发布 → 需要时重新 apply_share 走上架审核。
      · 作者「下架申请」流转（记 plugin_reviews 待管理员裁决）为后续增强，暂未实现。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "能力不存在"
    if p["scope"] != "public":
        return False, "该能力未上架市场"
    if not is_market_admin(user):
        _who = "作者" if (user and user.get("id") and p.get("author_id") and int(p["author_id"]) == int(user["id"])) else "当前用户"
        return False, (f"撤回上架仅市场管理员可操作（{_who}不可直接下架市场条目，"
                       "请提交下架申请给市场管理员）")
    conn.execute("UPDATE plugins SET scope='personal', updated_at=? WHERE plugin_id=?",
                 (_now(), plugin_id))
    conn.commit()
    return True, None


def grant(conn, plugin_id, target_type, target_id, permission):
    """范围分配（管理员）：all/team/role。INSERT OR REPLACE 保证唯一性。"""
    if target_type not in ("all", "team", "role"):
        return False, "target_type 必须为 all/team/role"
    conn.execute(
        """INSERT OR REPLACE INTO plugin_grants (plugin_id, target_type, target_id, permission)
           VALUES (?,?,?,?)""",
        (plugin_id, target_type, target_id, permission))
    conn.commit()
    return True, None


def install(conn, plugin_id, user, enabled=True):
    """安装（复制为个人副本：写 plugin_installs；公共插件 install_count+1）。

    2026-09-17 P2：区分失败原因。此前一律返回「插件不存在或未上架」，市场管理员
    全局停用条目后，用户点安装看到的是「不存在」—— 误以为条目被删，实际只是停用。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "插件不存在"
    if p["scope"] != "public":
        return False, "该能力未上架市场，无法安装"
    if p["status"] == "disabled":
        return False, "该能力已被市场管理员全局停用，暂不可安装"
    if p["status"] != "published":
        return False, (f"该能力当前为「{STATUS_LABELS.get(p['status'], p['status'])}」，"
                       "不满足安装条件（仅已发布的能力可安装）")
    if not user or not user.get("id"):
        return False, "未登录，无法安装"
    conn.execute(
        """INSERT OR IGNORE INTO plugin_installs (user_id, plugin_id, version, enabled)
           VALUES (?,?,?,?)""",
        (user["id"], plugin_id, p["current_version"], 1 if enabled else 0))
    # 2026-09-17 P2-2：重算而非 +1（幂等，重复安装不会虚增）
    _sync_install_count(conn, plugin_id)
    conn.commit()
    return True, None


def uninstall(conn, plugin_id, user):
    """卸载：仅删除"我名下"的安装记录。内置能力直接拒绝。

    2026-09-16 修复：此前不校验是否真有记录 —— 内置能力（系统级安装 user_id=0）
    对任何用户都不存在个人记录，却仍返回成功并错误地递减 install_count，
    造成"提示已卸载、刷新又在"的假操作。现如实反馈。
    补充：内置能力即便存在个人记录（个人启停会派生出记录）也不可卸载 ——
    它是系统组成部分，删了记录仍会被系统级安装撑在「我安装的」里，属假操作。
    """
    if not user or not user.get("id"):
        return False, "未登录"
    p = get_plugin(conn, plugin_id)
    if p and (p.get("author_id") or 0) == 0:
        return False, "内置能力属系统组成部分，不支持卸载（如需暂时关闭请用「停用」）"
    cur = conn.execute("DELETE FROM plugin_installs WHERE user_id=? AND plugin_id=?", (user["id"], plugin_id))
    if not cur.rowcount:
        return False, ("该能力不是你安装的（内置能力属系统组成部分，不支持卸载；"
                       "如需暂时关闭请用「停用」）")
    # 2026-09-17 P2-2：重算而非 -1 —— 此前删的是「派生记录」却让计数掉档，
    #   且从未装过的用户被拒时计数仍可能被扣（真实库实测 5→3）。
    _sync_install_count(conn, plugin_id)
    conn.commit()
    return True, None


def set_enabled(conn, plugin_id, user, enabled):
    """启用/停用：作者对个人插件、管理员对公共插件。

    2026-09-16 修复：admin 判定改用统一口径 is_admin()。
    此前用 bool(permissions["admin"])（更严），导致"设计师"这类
    ai_studio 含 publish 的角色能编辑/删除却唯独不能启停，自相矛盾。

    2026-09-17（P0，市场治理收权）：scope='public' 分支移除 is_author ——
    此前作者可全局停用自己已上架的能力，一人操作即令全市场下架，
    并经同步桥切断所有用户的 AI 运行时调用。市场数据（status）的
    全局启停收归管理员；作者想下架走撤回上架（withdraw_share，另案收权）。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "插件不存在"
    is_author = bool(user and user.get("id") == p["author_id"])
    adm = is_market_admin(user)
    if p["scope"] == "public":
        if not adm:
            return False, "公共插件的全局启停仅市场管理员可操作（作者如需下架请申请撤回上架）"
        conn.execute("UPDATE plugins SET status=?, updated_at=? WHERE plugin_id=?",
                     ("published" if enabled else "disabled", _now(), plugin_id))
    else:
        # 2026-09-16：管理员也应能启停个人插件 —— 同步桥创建的本地能力 author_id=0，
        # 若不放开 admin，这类能力将没有任何人能启停（管理死角）。
        if not (is_author or adm):
            return False, "仅作者或管理员可启停个人插件"
        conn.execute("UPDATE plugins SET status=?, updated_at=? WHERE plugin_id=?",
                     ("published" if enabled else "disabled", _now(), plugin_id))
    conn.commit()
    # 同步桥：全局上/下架写回旧表，否则 AI 运行时照旧调用（安全隐患）
    _sync_legacy_safe(conn, plugin_id, bool(enabled))
    conn.commit()
    return True, None


def set_install_enabled(conn, plugin_id, user, enabled):
    """个人级启停：只影响「我」用不用该能力，不改动能力实体的公共状态。

    2026-09-16 新增。此前 UI 的「停用」直连 set_enabled()，对公共/内置能力
    改的是 plugins.status —— 即全局下架，会波及所有用户。这与「我安装的」
    这一区位的个人语义严重不符，且属危险操作。

    内置能力（系统级 user_id=0 承载安装）没有个人记录，此处惰性派生一条，
    使个人停用得以生效，同时不动系统级记录。
    """
    if not user or not user.get("id"):
        return False, "未登录"
    cur = conn.execute(
        "UPDATE plugin_installs SET enabled=? WHERE user_id=? AND plugin_id=?",
        (1 if enabled else 0, user["id"], plugin_id))
    if not cur.rowcount:
        sysrow = conn.execute(
            "SELECT version FROM plugin_installs WHERE user_id=0 AND plugin_id=?",
            (plugin_id,)).fetchone()
        ver = sysrow["version"] if sysrow else None
        if ver is None:
            # 2026-09-16（P1-6）：自建能力（作者自持，无需"安装"这一步）同样要能停用 ——
            # 此前只认系统级记录并直接返回失败，导致「自己创建的能力无法启停」。
            # 取本体当前版本号派生一条个人记录，语义与"我关掉它"一致。
            #
            # 2026-09-17 扩展：同时放行「系统所有」（author_id=0）—— 历史迁移的私有能力
            # 既无系统级记录、又无作者，此前既不可消费也不可个人停用，形成管理盲区。
            # 规则：作者自持 或 系统所有 → 可派生个人记录；他人私有能力一律拒绝。
            p = get_plugin(conn, plugin_id)
            if not p:
                return False, "插件不存在"
            _aid = p.get("author_id") or 0
            if _aid and int(_aid) != int(user["id"]):
                return False, "尚未安装该能力，无法启停"
            ver = p.get("current_version") or ""
        conn.execute(
            "INSERT INTO plugin_installs (user_id, plugin_id, version, enabled) VALUES (?,?,?,?)",
            (user["id"], plugin_id, ver, 1 if enabled else 0))
        # 2026-09-17 P2-2：派生记录同样改变行数，计数列须同步重算，
        # 否则「个人停用」会静默制造新的计数漂移。
        _sync_install_count(conn, plugin_id)
    conn.commit()
    return True, None


def soft_delete(conn, plugin_id, user):
    """软删除（status='removed'，审计保留，同步桥写旧表不可用）。

    2026-09-17（P0，市场治理收权）：scope='public' 时仅管理员可删 ——
    此前作者可删除自己已上架的能力，status→removed 后经同步桥切断
    所有已安装用户的运行时调用，属"一行代码下架全市场"的越权。
    作者的合规路径：先撤回上架（scope→personal），再删除个人能力。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return False, "插件不存在"
    is_author = bool(user and user.get("id") == p["author_id"])
    adm = is_admin(user)                # 系统/内容级：他人个人能力的删除仍按 is_admin
    mkt = is_market_admin(user)         # 市场级：已上架能力的删除
    if not (is_author or adm or (p["scope"] == "public" and mkt)):
        return False, "仅作者或管理员可删除"
    if p["scope"] == "public" and not mkt:
        return False, ("已上架市场的能力删除需市场管理员操作；"
                       "请先「撤回上架」变回个人能力后再删除，或联系管理员")
    conn.execute("UPDATE plugins SET status='removed', scope='personal', updated_at=?"
                 " WHERE plugin_id=?", (_now(), plugin_id))
    # 2026-09-17 P2-3：连带清理安装记录与可见范围。此前软删只改 status：
    #   ① plugin_installs 行残留 → 「我安装的」区对该条目的 EXISTS 命中仍成立，
    #      出现幽灵条目，且安装计数把已删条目算进去，虚高（真实库 2 组孤儿）；
    #   ② scope 卡在 public → 已删条目仍能被 withdraw_share / 市场查询命中。
    #   已软删的能力不存在消费路径，记录与市场可见性都无保留价值。
    conn.execute("DELETE FROM plugin_installs WHERE plugin_id=?", (plugin_id,))
    conn.commit()
    # 同步桥：删除 = 旧表不可用（保留旧表数据，不做物理删除）
    _sync_legacy_safe(conn, plugin_id, False)
    conn.commit()
    return True, None


def log_audit(conn, user, plugin_id, action, detail=None, ip=""):
    user_name = (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
    user_id = (user or {}).get("id") or 0
    conn.execute(
        """INSERT INTO plugin_audit_logs (user_id, user_name, plugin_id, action, detail_json, ip)
           VALUES (?,?,?,?,?,?)""",
        (user_id, user_name, plugin_id, action, json.dumps(detail or {}, ensure_ascii=False), ip))


def log_call(conn, plugin_id, tool_name, params, status, latency_ms=0, tokens=0):
    conn.execute(
        """INSERT INTO plugin_call_logs (plugin_id, tool_name, params_snapshot, status, latency_ms, tokens)
           VALUES (?,?,?,?,?,?)""",
        (plugin_id, tool_name, json.dumps(params or {}, ensure_ascii=False)[:2000],
         status, latency_ms, tokens))


def pending_reviews(conn):
    """待审列表。

    P1-7（2026-09-16）：审核对象已从「发布」改为「上架」——
      · scope='pending_public'  上架申请（新语义，主路径）
      · status='submitted'      历史/兼容（旧语义：发布需审核）

    2026-09-17（P1-3）：每条附 `review_kind`，供前端区分同一「通过」按钮的两种后果 ——
      · 'share'         上架审核：通过 → scope=public（**条目进入市场**）
      · 'publish_legacy' 旧发布审核：通过 → status=published（**不上架**，仅恢复可用）
    此前两者混装且无标记，管理端渲染成同一个按钮，点击效果不同（P1-3）。
    """
    rows = conn.execute(
        """SELECT * FROM plugins
           WHERE status='submitted' OR scope='pending_public'
           ORDER BY updated_at ASC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["review_kind"] = ("share" if d.get("scope") == "pending_public"
                            else "publish_legacy")
        out.append(d)
    return out


def list_audit(conn, plugin_id="", limit=100):
    sql = "SELECT * FROM plugin_audit_logs"
    params = []
    if plugin_id:
        sql += " WHERE plugin_id=?"
        params.append(plugin_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_versions(conn, plugin_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_versions WHERE plugin_id=? ORDER BY id DESC", (plugin_id,)).fetchall()]


def list_reviews(conn, plugin_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_reviews WHERE plugin_id=? ORDER BY id DESC", (plugin_id,)).fetchall()]


def list_grants(conn, plugin_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_grants WHERE plugin_id=?", (plugin_id,)).fetchall()]


def list_call_logs(conn, plugin_id="", limit=100):
    sql = "SELECT * FROM plugin_call_logs"
    params = []
    if plugin_id:
        sql += " WHERE plugin_id=?"
        params.append(plugin_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def save_server_json(plugin_id, server):
    """MCP 插件的 server.json 落盘（与 base_url/凭证解耦，对齐设计方案 §3.4）。"""
    d = _ensure_plugin_dir(plugin_id)
    path = os.path.join(d, "server.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(server, f, ensure_ascii=False, indent=2)
    return path


def read_server_json(plugin_id):
    path = os.path.join(PLUGIN_DIR, plugin_id, "server.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_skill_md(plugin_id, content):
    d = _ensure_plugin_dir(plugin_id)
    path = os.path.join(d, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return path


def read_skill_md(plugin_id):
    path = os.path.join(PLUGIN_DIR, plugin_id, "SKILL.md")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── P0-5：插件能力 → Agent 运行时展开（市场统一后，插件技能可被对话自动触发）──
_SKILL_JSON_FIELDS = ("triggers", "dependencies", "allowed_roles", "allowed_tools",
                      "references", "examples", "scripts")


def skill_entry_from_plugin(conn, plugin_id):
    """插件行（type=skill/bundle）→ 技能池条目（SKILL.md frontmatter 展开）。

    返回与 agent/pipeline._global_skill_pool 相同结构的 dict（name=plugin_id 保证唯一，
    避免与 skills 表技能重名冲突；label.zh_CN 参与语义匹配文本）。
    无 SKILL.md 或非技能型插件 → None。MCP 型插件由 mcp_entry_from_plugin 处理。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return None
    if p["type"] not in ("skill", "bundle"):
        return None
    md = read_skill_md(plugin_id)
    if not md:
        return None
    fm = parse_frontmatter(md)          # P2-2：统一解析（含 triggers 列表/priority）
    import re as _re
    _m = _re.match(r"^---\s*\n.*?\n---\s*\n?(.*)$", md, _re.S)
    body = _m.group(1).strip() if _m else md.strip()
    try:
        manifest = json.loads(p.get("manifest_json") or "{}")
    except Exception:
        manifest = {}
    label = manifest.get("label") or {}
    desc = fm.get("description") or p.get("description") or label.get("zh_CN") or p.get("name")
    entry = {
        "type": "skill",                         # 与 skills 表条目同构（get_bound_skills 过滤键）
        "name": plugin_id,                       # 唯一键（语义匹配文本含中文名，不影响命中）
        "display_name": label.get("zh_CN") or p.get("name"),
        "plugin_id": plugin_id,
        "source": "plugin",
        "description": desc,
        "triggers": fm.get("triggers") or [],
        "content": body or desc,
        "frontmatter": md[:400],
        "allowed_tools": fm.get("allowed_tools") or [],
        "references": [], "examples": [], "scripts": [],
        "version": p.get("current_version") or "",
        "skill_type": "plugin",
    }
    return entry


def mcp_entry_from_plugin(conn, plugin_id):
    """插件行（type=mcp/bundle）→ MCP 工具条目（server.json 描述）。"""
    p = get_plugin(conn, plugin_id)
    if not p:
        return None
    if p["type"] not in ("mcp", "bundle"):
        return None
    server = read_server_json(plugin_id)
    if not server:
        return None
    return {
        "type": "mcp",
        "name": p["plugin_id"],
        "display_name": p["name"],
        "source": "plugin",
        "desc": f"MCP 插件（{p['name']}）",
        "endpoint": server.get("base_url") or "",
        "transport": server.get("transport") or "streamable_http",
        "tools": server.get("tools") or [],
        "params": {},
    }


# ══════════════════════════════════════════════════════════════════════
# P1-6：可消费性判定 —— 「安装即可消费」（2026-09-16 晚）
#
# 语义：一个能力能否被 AI 运行时消费 = 本体已发布 且 已安装 且 已启用。
#   · 安装来源不分贵贱：系统级(user_id=0，内置能力默认可用) 与 个人级(市场安装/自建)等效
#   · 停用(plugin_installs.enabled=0) 与 全局下架(plugins.status≠published) 都使消费失效
# 这使能力中心的「安装 / 停用」成为运行时的真实开关，而不再只是管理面的标签。
# ══════════════════════════════════════════════════════════════════════

# 历史迁移中表名混用（mcps 与 mcp_servers），反查旧表映射前统一归一化
_LEGACY_TABLE_ALIAS = {"mcps": "mcp_servers"}


def consumable_plugin_ids(conn, user=None, any_user=False) -> set:
    """当前上下文可消费的 plugin_id 集合。

    判定 = 本体已发布(status='published') 且「可用性开关为开」。开关按优先级取：
      ① 安装记录 plugin_installs.enabled —— 公共/内置能力走这条
         （系统级 user_id=0 是内置能力的默认可用标记；个人级是"从市场安装"）
      ② **作者自持**：scope='personal' 且 author_id=本人 —— 自建能力无需"安装"这一步
      ③ 显式停用否决：本人存在 enabled=0 的安装记录 → 一律不可消费
         （这样"停用"对自建能力同样有效，不必依赖 ①）

    ⚠️ 为什么需要 ②：install() 只接受 scope='public'（它表达的是"从市场安装"），
    自建的个人能力永远 install 不成功。若判定只看 installs，自己创建的能力就永远
    不可消费，与"安装即可消费"的语义相悖。

    参数：
      user      有 id → 系统级 ∪ 本人；None → 仅系统级安装记录
      any_user  True → 全局装配模式：任一用户的启用安装，或任一作者的已发布自建能力
                （Agent 意图路由池是全系统共享的，用它）
    """
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    if any_user:
        # 全局装配模式：作者对自己自建能力的显式停用应全局生效
        # （私有能力的唯一控制者就是作者，他关掉就该从全局池消失）
        sql = ("SELECT p.plugin_id, p.author_id, "
               "(SELECT MAX(i.enabled) FROM plugin_installs i WHERE i.plugin_id=p.plugin_id) AS inst_on, "
               "(SELECT COUNT(*) FROM plugin_installs i2 WHERE i2.plugin_id=p.plugin_id "
               "  AND i2.enabled=0 AND p.author_id!=0 AND i2.user_id=p.author_id) AS explicit_off "
               "FROM plugins p WHERE p.status='published'")
        params = ()
    elif uid:
        sql = ("SELECT p.plugin_id, p.author_id, "
               "(SELECT MAX(i.enabled) FROM plugin_installs i "
               "  WHERE i.plugin_id=p.plugin_id AND i.user_id IN (0,?)) AS inst_on, "
               "(SELECT COUNT(*) FROM plugin_installs i2 "
               "  WHERE i2.plugin_id=p.plugin_id AND i2.user_id=? AND i2.enabled=0) AS explicit_off "
               "FROM plugins p WHERE p.status='published'")
        params = (uid, uid)
    else:
        sql = ("SELECT p.plugin_id, p.author_id, "
               "(SELECT MAX(i.enabled) FROM plugin_installs i "
               "  WHERE i.plugin_id=p.plugin_id AND i.user_id=0) AS inst_on, "
               "0 AS explicit_off "
               "FROM plugins p WHERE p.status='published'")
        params = ()
    out = set()
    try:
        for r in conn.execute(sql, params).fetchall():
            pid, author_id, inst_on, explicit_off = r[0], r[1], r[2], r[3]
            if explicit_off:
                continue                        # 本人显式停用 → 否决（优先于一切）
            if inst_on == 1:
                out.add(pid)                    # ① 有启用中的安装记录（系统级/个人级）
                continue
            if not author_id:
                # ③ 系统所有（author_id=0）= 平台提供。
                #    2026-09-17 修复：此前这里没有分支，导致「历史迁移的私有能力」
                #    （author_id=0 + scope=personal + 无任何安装记录）被静默排除出运行时 ——
                #    graph_db_query / graph_db_stats / graph_db_nlquery / mbse_pull_ingest
                #    等 9 条 Agent 绑定因此全部失效。
                #
                #    ⚠️ 关键区分（否则会把"停用/下架"一起废掉）：
                #      inst_on is None → **完全没有安装记录** = 归属不明 → fail-open 放行
                #      inst_on == 0    → 有记录但被关掉 = 明确停用 → 尊重，排除
                if inst_on is None:
                    out.add(pid)
                continue
            if any_user:
                out.add(pid)                    # 全局装配（无用户上下文）：有作者的自建能力视为可用
                continue
            if uid and int(author_id) == int(uid):
                out.add(pid)                    # ② 自建自持：无需"安装"这一步
    except sqlite3.OperationalError:
        return set()          # 未跑迁移的老库：降级为空集（调用方按"无判定"处理）
    return out


def legacy_mapping(conn) -> dict:
    """全量 legacy 映射 {(表, 旧id): plugin_id}。仅含建立了映射的插件。"""
    out = {}
    try:
        rows = conn.execute(
            "SELECT plugin_id, manifest_json FROM plugins WHERE status!='removed'").fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        try:
            rt = (json.loads(r["manifest_json"] or "{}").get("runtime") or {})
        except Exception:
            continue
        tbl = rt.get("legacy_table") or ""
        tbl = _LEGACY_TABLE_ALIAS.get(tbl, tbl)
        lid = rt.get("legacy_id")
        if tbl and lid:
            out[(tbl, lid)] = r["plugin_id"]
    return out


def consumable_filter(conn, user=None, any_user=False):
    """返回判定闭包 (旧表名, 旧id) -> 是否可被运行时消费。

    规则（关键：不误伤旧体系原生能力）：
      · 该记录**没有**插件映射 → 保留（旧体系原生，不归能力中心管）
      · 有映射且插件可消费   → 保留
      · 有映射但插件不可消费 → 排除（未安装 / 已停用 / 已下架）
    同步桥保证映射两侧状态一致，本判定让「停用」对运行时真正生效。
    """
    mapping = legacy_mapping(conn)
    ok = consumable_plugin_ids(conn, user, any_user=any_user)

    def _keep(table: str, lid) -> bool:
        pid = mapping.get((table, lid))
        if not pid:
            return True
        return pid in ok
    return _keep


def plugin_manifest(conn, plugin_id) -> tuple:
    """(插件行, manifest dict)；不存在 → (None, {})。"""
    p = get_plugin(conn, plugin_id)
    if not p:
        return None, {}
    try:
        return p, json.loads(p.get("manifest_json") or "{}")
    except Exception:
        return p, {}


def prompt_entry_from_plugin(conn, plugin_id):
    """插件行（type=prompt）→ 提示词条目（与 prompts 表同构）。

    纯插件提示词的正文来自 manifest.content；未填则返回 None
    （不臆造内容 —— 宁可不可用，也不给运行时注入空模板）。
    """
    p, mf = plugin_manifest(conn, plugin_id)
    if not p or p["type"] != "prompt":
        return None
    content = (mf.get("content") or "").strip()
    if not content:
        return None
    label = mf.get("label") or {}
    return {
        "id": 0,
        "name": label.get("zh_CN") or p.get("name") or plugin_id,
        "scenario": mf.get("category") or "",
        "content": content,
        "plugin_id": plugin_id,
        "source": "plugin",
    }


def agent_entry_from_plugin(conn, plugin_id):
    """插件行（type=agent）→ Agent 定义片段。

    纯插件 Agent 必须自带 system_prompt（manifest.system_prompt）才有意义；
    缺失则返回 None —— 没有角色提示词的 Agent 参与路由只会污染意图分类。
    """
    p, mf = plugin_manifest(conn, plugin_id)
    if not p or p["type"] != "agent":
        return None
    sp = (mf.get("system_prompt") or "").strip()
    if not sp:
        return None
    caps = mf.get("capabilities") or {}
    arr = caps.get("agents") or []
    runtime_name = ""
    if arr and isinstance(arr[0], dict):
        runtime_name = (arr[0].get("name") or "").strip()
    kws = mf.get("intent_keywords") or []
    if not isinstance(kws, list):
        kws = []
    return {
        "name": runtime_name or p["plugin_id"],
        "plugin_id": plugin_id,
        "display_name": (mf.get("label") or {}).get("zh_CN") or p.get("name"),
        "description": p.get("description") or "",
        "system_prompt": sp,
        "tools": [t.get("name") for t in (caps.get("tools") or []) if isinstance(t, dict) and t.get("name")],
        "agent_role": mf.get("agent_role") or "sub",
        # 意图关键词决定该 Agent 能否被语义路由命中；缺省则只能被显式指定
        "intent_keywords": [str(k) for k in kws if str(k).strip()],
        "source": "plugin",
    }


def tool_entry_from_plugin(conn, plugin_id):
    """插件行（type=tool）→ 工具条目。

    仅当工具名存在时返回；`executable` 表示是否具备执行通道——
    有 legacy 映射（落到 tools 表执行器）或 manifest 声明 runtime.endpoint（HTTP 型）才为 True。
    无执行通道的纯元数据工具会被调用方跳过注入，避免诱导 LLM 调用必败工具。
    """
    p, mf = plugin_manifest(conn, plugin_id)
    if not p or p["type"] != "tool":
        return None
    caps = mf.get("capabilities") or {}
    names = [t.get("name") for t in (caps.get("tools") or [])
             if isinstance(t, dict) and t.get("name")]
    if not names:
        return None
    rt = mf.get("runtime") or {}
    has_legacy = bool(rt.get("legacy_table") and rt.get("legacy_id"))
    has_http = bool(str(rt.get("endpoint") or "").strip())
    return {
        "name": names[0],
        "names": names,
        "plugin_id": plugin_id,
        "display_name": (mf.get("label") or {}).get("zh_CN") or p.get("name"),
        "description": p.get("description") or "",
        "side_effect": rt.get("side_effect") or "read",
        "executable": bool(has_legacy or has_http),
        "source": "plugin",
    }


def plugin_ids_of_types(conn, kinds, user=None, any_user=False) -> list:
    """可消费的指定类型插件 ID 列表（供 plugins 侧展开成运行时条目）。"""
    if isinstance(kinds, str):
        kinds = (kinds,)
    ids = consumable_plugin_ids(conn, user, any_user=any_user)
    if not ids or not kinds:
        return []
    qs = ",".join("?" * len(ids))
    ks = ",".join("?" * len(kinds))
    rows = conn.execute(
        "SELECT plugin_id FROM plugins WHERE plugin_id IN (%s) AND type IN (%s)" % (qs, ks),
        tuple(ids) + tuple(kinds)).fetchall()
    return [r["plugin_id"] for r in rows]


def reconcile_integrity(conn) -> dict:
    """一次性完整性校准（幂等，可重复执行）。返回各项修复数量。

    2026-09-17 四刀修复的收尾：本次改造把「软删除连带清理」「计数按行重算」的
    守卫补进了写路径，但**历史遗留的脏数据不会自动消失**。本函数负责把已经产生的
    三类不一致抹平，供启动或运维手动调用（不改业务语义，只让数据自洽）：

      ① 幽灵上架  scope='public' AND status='removed'
         —— 软删旧实现只改 status 不收回可见范围，条目已从 get_plugin() 消失，
            却仍能被市场查询/withdraw_share 命中（真实库 2 条 e2e 残留）。
      ② 孤儿安装  plugin_installs 指向已删除/不存在的插件
         —— 让「我安装的」区出现永远装不上的幽灵条目（真实库 2 组）。
      ③ 计数漂移  plugins.install_count != 实际行数
         —— 市场排序 ORDER BY install_count DESC 与卡片展示都读它（真实库 15 条）。
    """
    ghost = conn.execute(
        "UPDATE plugins SET scope='personal' WHERE scope='public' AND status='removed'").rowcount
    orphan = conn.execute(
        "DELETE FROM plugin_installs WHERE plugin_id IN ("
        "  SELECT i.plugin_id FROM plugin_installs i"
        "  LEFT JOIN plugins p ON p.plugin_id=i.plugin_id"
        "  WHERE p.plugin_id IS NULL OR p.status='removed')").rowcount
    # ③ 全量同步：逐条重算（数量级为插件数，可接受）
    ids = [r["plugin_id"] for r in conn.execute("SELECT plugin_id FROM plugins").fetchall()]
    for pid in ids:
        _sync_install_count(conn, pid)
    conn.commit()
    return {"ghost_scope_fixed": ghost, "orphan_installs_removed": orphan,
            "install_counts_synced": len(ids)}


def count_install_drift(conn) -> int:
    """计数列的失真条数（审计用，只读）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM plugins p WHERE p.install_count != "
        "(SELECT COUNT(*) FROM plugin_installs i WHERE i.plugin_id=p.plugin_id)"
    ).fetchone()[0]



