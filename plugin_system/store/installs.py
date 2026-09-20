"""安装/授权/启停/软删除：plugin_installs 与 plugins.status 的写路径。"""
from plugin_system.store.base import (
    STATUS_LABELS,
    _now,
    is_admin,
    is_market_admin,
)
from plugin_system.store.dependencies import _sync_legacy_safe
from plugin_system.store.queries import _sync_install_count, get_plugin


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
