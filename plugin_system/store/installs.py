"""安装/授权/启停/软删除：plugin_installs 与 plugins.status 的写路径。"""
from plugin_system.store.base import (
    STATUS_LABELS,
    _now,
    is_admin,
    is_builtin_row,
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
    # 关于 user_id=0 的说明（2026-09-30 核实，请勿「顺手修正」这里）：
    #   plugin_installs.user_id=0 是**存储层的系统级安装标记**（全用户可消费），不是登录身份 ——
    #   users 表最小 id=1，current_user 永远返回真实用户（id>=1），所以这行真值判断对真实调用方无害。
    #   曾一度认为它是缺陷（Task#23 恢复脚本自己构造 {'id':0} 调用 install 被 409），那是脚本的
    #   特殊调用约定、不是生产路径。放宽它等于白白拆掉一道登录闸门，故维持原样。
    if not user or not user.get("id"):
        return False, "未登录，无法安装"
    conn.execute(
        """INSERT OR IGNORE INTO plugin_installs (user_id, plugin_id, version, enabled)
           VALUES (?,?,?,?)""",
        (user["id"], plugin_id, p["current_version"], 1 if enabled else 0))
    # 2026-09-17 P2-2：重算而非 +1（幂等，重复安装不会虚增）
    _sync_install_count(conn, plugin_id)
    conn.commit()
    # 2026-09-29（数据流转审计 · 断层2）：装回时同步恢复旧表可用性。
    #   此前只有"下架方向"的同步（uninstall/set_enabled/soft_delete → False），
    #   全局卸载（删系统级行，同步桥已把旧表置非 active）后重新安装，旧表仍是
    #   非 active → registry.load_from_db 继续排除 → **装回了 AI 也用不了**（假修复）。
    #   enabled=True 的安装把旧表写回 active，闭环才真正合上；重复写回幂等无害。
    if enabled:
        _sync_legacy_safe(conn, plugin_id, True)
        conn.commit()
    return True, None


def _bound_agent_names(conn, plugin_id):
    """查哪些 Agent 绑定了该插件（agent_tools.tool_type='plugin'）。

    2026-09-30（用户第 10 轮）：Agent「绑定插件」机制已整体移除
    （见 agent/registry.py 中 load_from_db 的说明），`tool_type='plugin'` 不会再新增，
    故本函数**恒返回空列表**。保留函数本体是为了：
      ① 不打断 `_bind_note` 的调用链（卸载/删除路径共用它）；
      ② 若历史库中仍有残留行，卸载提示仍能如实反映（不隐藏影响面）。
    待确认全库无 plugin 绑定残留后，可与 `_bind_note` 一并删除。
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT a.display_name FROM agent_tools t "
            "JOIN agents a ON a.id=t.agent_id "
            "WHERE t.tool_type='plugin' AND t.tool_name=? AND a.status='active' "
            "ORDER BY a.display_name", (plugin_id,)).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _bind_note(conn, plugin_id):
    """卸载/删除提示尾注：有 Agent 绑定时如实告知影响面（数据流转审计 2026-09-29）。"""
    names = _bound_agent_names(conn, plugin_id)
    if not names:
        return ""
    head = "；".join(names[:3]) + (" 等" if len(names) > 3 else "")
    return ("注意：%d 个 Agent 绑定了该能力（%s），卸载后这些 Agent 的相关调用将失效，"
            "重新安装即恢复。" % (len(names), head))


def uninstall(conn, plugin_id, user):
    """卸载：移除"我名下"的安装记录。

    2026-09-16 修复：此前不校验是否真有记录 —— 内置能力（系统级安装 user_id=0）
    对任何用户都不存在个人记录，却仍返回成功并错误地递减 install_count，
    造成"提示已卸载、刷新又在"的假操作。现如实反馈。

    2026-09-29（数据流转审计 · 断层1）：「installed」判定含系统级行（user_id=0，
    「我安装的」因此显示已安装、前端给出卸载按钮），但本函数只删本人行 ——
    系统预装条目点卸载**必败**（"该能力不是你安装的"），按钮与 API 口径打架。
    用户已明确"目前的插件先不标记内置，允许卸载"。修法：
      · 本人行存在 → 删本人行（个人卸载，原语义不变）
      · 本人无行但存在系统级行：
          - 管理员 → 删系统级行（**全局卸载**）+ 同步桥切断旧表 → 运行时真实失效（闭环）
          - 普通用户 → 惰性派生本人 enabled=0 行（与个人停用同效），并**如实**告知
            "已为你停用；全局卸载需管理员"—— 不做假成功
    """
    if not user or not user.get("id"):
        return False, "未登录"
    p = get_plugin(conn, plugin_id)
    # 内置守卫：BUILTIN_MARKING_ENABLED=False（用户指示"先不标记内置"）时恒不触发；
    # 恢复内置口径后重新生效，保留为安全阀。
    if p and is_builtin_row(p):
        return False, "内置能力属系统组成部分，不支持卸载（如需暂时关闭请用「停用」）"
    cur = conn.execute("DELETE FROM plugin_installs WHERE user_id=? AND plugin_id=?", (user["id"], plugin_id))
    if cur.rowcount:
        # 2026-09-17 P2-2：重算而非 -1 —— 此前删的是「派生记录」却让计数掉档。
        _sync_install_count(conn, plugin_id)
        conn.commit()
        # 同步桥：个人卸载不全局切断旧表（其他人/系统级安装仍在用），仅当系统级行也已不存在时收口
        _sys_left = conn.execute("SELECT 1 FROM plugin_installs WHERE user_id=0 AND plugin_id=?", (plugin_id,)).fetchone()
        if not _sys_left:
            _sync_legacy_safe(conn, plugin_id, False)
            conn.commit()
        return True, _bind_note(conn, plugin_id)
    # ── 断层1修复：本人无行，但可能存在系统级安装行 ──
    sysrow = conn.execute(
        "SELECT id FROM plugin_installs WHERE user_id=0 AND plugin_id=?", (plugin_id,)).fetchone()
    if sysrow:
        if is_admin(user):
            # 断层4（2026-09-29 实测）：此前只删 user_id=0 行，但其他用户/历史遗留的
            # 个人安装行仍在 → list_mine 的 EXISTS(...,user_id IN (?,0)) 仍判 installed=true
            # → 管理员全局卸载成功后，UI 按钮不变、用户以为"没生效"。
            # 语义上"全局卸载 = 对所有人生效"，故连带清理该插件的全部残余安装行
            # （含历史孤儿行），使 consumed/installed 两个口径同时收口。
            purged = conn.execute(
                "DELETE FROM plugin_installs WHERE plugin_id=?", (plugin_id,)).rowcount
            _sync_install_count(conn, plugin_id)
            conn.commit()
            # 同步桥：全局卸载 → 旧表置不可用 → registry.load_from_db 排除 → 消费真实失效
            _sync_legacy_safe(conn, plugin_id, False)
            conn.commit()
            note = _bind_note(conn, plugin_id)
            if purged > 1:
                note += ("（同时清理了 %d 条历史安装记录）" % (purged - 1))
            return True, ("已全局卸载（该能力为系统预装，对所有用户生效）" + note)
        # 普通用户：派生个人停用行（语义同「停用」），如实提示，不做假成功
        conn.execute(
            "INSERT INTO plugin_installs (user_id, plugin_id, version, enabled) VALUES (?,?,?,0)",
            (user["id"], plugin_id, (p or {}).get("current_version") or ""))
        _sync_install_count(conn, plugin_id)
        conn.commit()
        return True, ("该能力为系统预装，已为你停用（全局卸载需管理员操作）")
    # ── 断层4b：本人无行、系统级行也没了，但仍有**他人/历史孤儿**安装行 ──
    #   典型成因：此前全局卸载只删 user_id=0，留下孤儿个人行；此后该条目在
    #   list_mine 里仍被判 installed=true，按钮点了却回"不是你安装的"（死锁）。
    #   管理员可清理这些残留，使 UI 口径与真实状态一致。
    orphan = conn.execute(
        "SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=? AND user_id NOT IN (0,?)",
        (plugin_id, user["id"])).fetchone()[0]
    if orphan:
        if is_admin(user):
            conn.execute(
                "DELETE FROM plugin_installs WHERE plugin_id=? AND user_id NOT IN (0,?)",
                (plugin_id, user["id"]))
            _sync_install_count(conn, plugin_id)
            conn.commit()
            _sync_legacy_safe(conn, plugin_id, False)
            conn.commit()
            return True, ("已清理 %d 条历史残留安装记录（该能力当前为下架状态）" % orphan)
        return False, ("该能力已下架，但列表中残留了历史安装记录；"
                       "请联系管理员清理（或直接「停用」）")
    return False, "该能力不是你安装的"


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
    # 2026-09-29（P0 修复，口径三处归一）：
    #   ① 内置判定改用 is_builtin_row —— 此前该函数**完全没有**内置拦截
    #      （uninstall 有、soft_delete 无），且旧口径 author_id==0 会连带覆盖
    #      历史迁移条目；本次按用户要求"目前的插件先不标记内置"，
    #      只有真正的平台 legacy 种子（55 条）被拦。
    #   ② "无主的个人能力"（author_id=0 且非内置，实测 19 条：历史迁移 18 + 本地创建 1）
    #      此前 is_author 恒 False（没有用户 id 为 0），管理员虽可删但 DTO 不给按钮；
    #      现明确：这类条目属"未认领"，管理员可删，普通用户不可删。
    if is_builtin_row(p):
        return False, "内置能力属系统组成部分，不支持删除（如需暂时关闭请用「停用」）"
    is_author = bool(user and user.get("id") and
                     int(p["author_id"] or 0) == int(user["id"]))
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
