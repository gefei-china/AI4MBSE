"""写路径：能力创建与清单更新（版本记录、依赖索引联动）。"""
import json

from plugin_system.store.base import (
    EDITABLE_STATUSES,
    STATUS_LABELS,
    _ensure_plugin_dir,
    _now,
    is_admin,
    is_market_admin,
)
from plugin_system.store.dependencies import _sync_deps_safe
from plugin_system.store.queries import get_plugin


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
