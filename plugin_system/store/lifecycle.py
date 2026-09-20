"""status（可用性）状态机与 scope（可见范围）治理动作。

两类维度正交：transition 只治理 status；publish_self / unpublish_self 亦只动 status，
apply_share / review_share / withdraw_share 只动 scope。
"""
from plugin_system.store.base import (
    STATUS_LABELS,
    _SELF_PUBLISHABLE_FROM,
    _is_owner,
    _now,
    can_transition,
    is_market_admin,
)
from plugin_system.store.dependencies import _sync_legacy_safe
from plugin_system.store.queries import get_plugin


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
