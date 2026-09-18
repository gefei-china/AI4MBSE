"""AI 设计工坊 · 统一插件体系 REST API（P0）。

对齐设计方案 §5.4 API 设计（前缀 /api/plugins）：
- 市场列表 / 个人空间 / 审核流 / 范围分配 / 安装卸载 / 启停 / 沙箱执行 / 审计
权限模型（§4.1，2026-09-17 P1 拆分修订）：
- 作者：个人插件的 CRUD/发布/提交（申请上架）
- 市场管理员（ai_studio:market_admin 或 admin 域）：上架审核、grant、
  公共能力全局启停、撤回上架、已上架能力改删、审计查询、审核策略
  —— 创作者（ai_studio:publish）不再自动享有市场治理权（store.is_market_admin）
- 普通用户：浏览公共市场（按 grant 过滤）+ 安装/卸载 + 个人级启停（仅影响自己）

连接注入：统一 Depends(db_session)（与 core.deps.current_user 共用同一请求级连接，
成功提交/异常回滚），匿名请求（无 X-User-Id）向后兼容。
"""
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from core.deps import current_user, db_session, require_permission
from plugin_system import store
from plugin_system.manifest import validate_manifest

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


def _as_admin(user):
    """管理员判定：admin 域权限非空 或 ai_studio:publish。

    2026-09-16：收敛到 plugin_system.store.is_admin 单一真理源。
    此前 store（set_enabled/soft_delete）与 router 各持一份判定、口径不一，
    造成"能编辑却不能启停"的矛盾。
    """
    return store.is_admin(user)


def _author_or_admin(plugin, user):
    if not user:
        return False
    if user.get("id") == plugin.get("author_id"):
        return True
    return _as_admin(user)


def _client_ip(request: Request):
    return request.client.host if request.client else ""


# ─────────────────────────── 市场与设置 ───────────────────────────
@router.get("")
def list_plugins(ptype: str = "", q: str = "", sort: str = "hot",
                 conn: sqlite3.Connection = Depends(db_session),
                 user=Depends(current_user)):
    """公共市场列表（grant 授权过滤 + 当前用户安装状态）。"""
    return {"items": store.list_market(conn, user, ptype, q, sort)}


@router.get("/mine")
def my_plugins(kind: str = "", q: str = "",
               conn: sqlite3.Connection = Depends(db_session),
               user=Depends(current_user)):
    """我的插件：作者=我的全部 + 安装副本。"""
    return {"items": store.list_mine(conn, user, kind, q)}


@router.get("/pending")
def pending(conn: sqlite3.Connection = Depends(db_session),
            user=Depends(require_permission("ai_studio", "market_admin"))):
    """待审核队列（市场管理员）。"""
    return {"items": store.pending_reviews(conn)}


@router.get("/settings")
def plugin_settings(conn: sqlite3.Connection = Depends(db_session), user=Depends(current_user)):
    row = conn.execute(
        "SELECT value, description FROM settings WHERE key='plugin_review_policy'").fetchone()
    policy = row["value"] if row else "forced"
    return {"review_policy": policy,
            "review_policy_desc": row["description"] if row else ""}


@router.put("/settings")
def update_plugin_settings(body: dict,
                           request: Request,
                           conn: sqlite3.Connection = Depends(db_session),
                           user=Depends(require_permission("ai_studio", "market_admin"))):
    policy = (body or {}).get("review_policy")
    if policy not in ("forced", "auto"):
        raise HTTPException(400, "review_policy 必须为 forced/auto")
    conn.execute("UPDATE settings SET value=? WHERE key='plugin_review_policy'", (policy,))
    store.log_audit(conn, user, "system.settings", "update", {"review_policy": policy}, _client_ip(request))
    conn.commit()
    return {"ok": True, "review_policy": policy}


# ─────────────────────────── 创建 / 详情 / 更新 / 删除 ───────────────────────────
@router.post("")
def create_plugin(body: dict,
                  request: Request,
                  conn: sqlite3.Connection = Depends(db_session),
                  user=Depends(current_user)):
    """创建个人插件。body: {manifest: {...}, skill_md?: str, mcp_server?: {...}}"""
    if not user or not user.get("id"):
        raise HTTPException(401, "未登录，无法创建插件")
    manifest = (body or {}).get("manifest") or {}
    ok, errors, normalized = validate_manifest(manifest)
    if not ok:
        raise HTTPException(400, "manifest 校验失败：" + "; ".join(errors))
    ok, plugin, err = store.create_plugin(conn, normalized, user)
    if not ok:
        raise HTTPException(409, err)
    # 载荷落盘：SKILL.md / server.json
    skill_md = (body or {}).get("skill_md")
    if skill_md:
        store.save_skill_md(plugin["plugin_id"], skill_md)
    mcp_server = (body or {}).get("mcp_server")
    if mcp_server:
        store.save_server_json(plugin["plugin_id"], mcp_server)
    store.log_audit(conn, user, plugin["plugin_id"], "create",
                    {"version": normalized["version"], "type": normalized["type"]}, _client_ip(request))
    conn.commit()
    return {"ok": True, "plugin": store.plugin_dto(plugin, user)}


@router.get("/{pid}")
def plugin_detail(pid: str,
                  conn: sqlite3.Connection = Depends(db_session),
                  user=Depends(current_user)):
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")
    # P1-8 用户隔离（2026-09-17）：可见性 = 公开 / 作者 / 管理员 / 我已安装。
    # 此前无任何可见性校验，任何登录用户都能按 id 读到他人私有能力的详情；
    # 且 versions 里带 manifest_json —— 原有那句 d.pop("manifest") 拦不住
    # 私有能力的 system_prompt / content 被直接读走。
    # 不可见时统一按"不存在"处理，不泄露该能力是否存在。
    _installed = bool(user and user.get("id") and conn.execute(
        "SELECT 1 FROM plugin_installs WHERE user_id=? AND plugin_id=?",
        (user["id"], pid)).fetchone())
    if not (p["scope"] == "public" or _author_or_admin(p, user) or _installed):
        raise HTTPException(404, "插件不存在")
    d = store.plugin_dto(p, user)
    d["versions"] = store.list_versions(conn, pid)
    d["reviews"] = store.list_reviews(conn, pid)
    d["grants"] = store.list_grants(conn, pid)
    d["skill_md"] = store.read_skill_md(pid)
    d["mcp_server"] = store.read_server_json(pid)
    return d


@router.put("/{pid}")
def update_plugin(pid: str,
                  body: dict,
                  request: Request,
                  conn: sqlite3.Connection = Depends(db_session),
                  user=Depends(current_user)):
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")
    # 首道守卫（2026-09-17 P1）：作者 / 系统管理员 / （public 时）市场管理员；
    # public 的最终裁决在 store.update_plugin（作者与设计师被拒，市场管理员放行）。
    if not (user and (user.get("id") == p.get("author_id")
                      or store.is_admin(user)
                      or (p["scope"] == "public" and store.is_market_admin(user)))):
        raise HTTPException(403, "仅作者或管理员可编辑该插件")
    manifest = (body or {}).get("manifest") or {}
    ok, errors, normalized = validate_manifest(manifest)
    if not ok:
        raise HTTPException(400, "manifest 校验失败：" + "; ".join(errors))
    ok2, err2 = store.update_plugin(conn, pid, normalized, user)
    if not ok2:
        raise HTTPException(409, err2)
    if (body or {}).get("skill_md"):
        store.save_skill_md(pid, body["skill_md"])
    if (body or {}).get("mcp_server"):
        store.save_server_json(pid, body["mcp_server"])
    store.log_audit(conn, user, pid, "update",
                    {"version": normalized["version"]}, _client_ip(request))
    conn.commit()
    return {"ok": True, "plugin": store.plugin_dto(store.get_plugin(conn, pid), user)}


@router.delete("/{pid}")
def delete_plugin(pid: str,
                  request: Request,
                  conn: sqlite3.Connection = Depends(db_session),
                  user=Depends(current_user)):
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")
    ok, err = store.soft_delete(conn, pid, user)
    if not ok:
        raise HTTPException(403, err)
    store.log_audit(conn, user, pid, "delete", {}, _client_ip(request))
    conn.commit()
    return {"ok": True}


# ─────────────────────────── 生命周期：发布 / 提交 / 审核 ───────────────────────────
@router.post("/{pid}/publish")
def publish_personal(pid: str,
                     request: Request,
                     conn: sqlite3.Connection = Depends(db_session),
                     user=Depends(current_user)):
    """发布（自用）：让能力可被 AI 消费。**免审核，不改可见范围。**

    P1-7（2026-09-16）解耦：此前本接口走 draft→submitted 审核，而审核通过又会强制
    scope='public' —— 「我自己要用」被迫付出「公开给全团队」的代价。
    现在「发布」只动 status；要分享给团队请另行调用 /{pid}/share。
    """
    ok, err = store.publish_self(conn, pid, user)
    if not ok:
        raise HTTPException(409, err)
    p = store.get_plugin(conn, pid) or {}
    store.log_audit(conn, user, pid, "publish", {"mode": "self"}, _client_ip(request))
    conn.commit()
    return {"ok": True, "status": "published", "scope": p.get("scope", "personal"),
            "note": "已发布，可被 AI 消费；如需全团队可用，请调用 /share 申请上架"}


@router.post("/{pid}/unpublish")
def unpublish_personal(pid: str,
                       request: Request,
                       conn: sqlite3.Connection = Depends(db_session),
                       user=Depends(current_user)):
    """取消发布：回到草稿，AI 不再可消费（已上架的须先撤回上架）。"""
    ok, err = store.unpublish_self(conn, pid, user)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, "unpublish", {}, _client_ip(request))
    conn.commit()
    return {"ok": True, "status": "draft"}


@router.post("/{pid}/share")
def share_to_market(pid: str,
                    request: Request,
                    conn: sqlite3.Connection = Depends(db_session),
                    user=Depends(current_user)):
    """申请上架公共市场：scope personal → pending_public（需审核）。

    **只改可见范围，不改 status** —— 审核期间能力照常可用，
    这与旧流程（status=submitted，审核期能力消失）是本质差别。
    """
    ok, err = store.apply_share(conn, pid, user)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, "share_apply", {}, _client_ip(request))
    conn.commit()
    return {"ok": True, "scope": "pending_public",
            "note": "已提交上架申请，等待管理员审核（审核期间能力照常可用）"}


@router.post("/{pid}/unshare")
def withdraw_from_market(pid: str,
                         request: Request,
                         conn: sqlite3.Connection = Depends(db_session),
                         user=Depends(current_user)):
    """按可见范围分派两种「撤回」（2026-09-17 P1-1 修复）：

      · scope='pending_public' → **撤回上架申请**（cancel_share，作者侧，免审核）
        此前作者提交申请后没有任何撤回入口，只能等管理员裁决，
        管理员不在场时等同「用户旅程级准死锁」。
      · scope='public'         → **撤回上架**（withdraw_share，仅市场管理员）
        status 不变 —— 能力对作者侧仍可用，只是别人看不到、装不上了。

    作者不可直接下架已上架条目，请提交下架申请给市场管理员。
    """
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")
    if p.get("scope") == "pending_public":
        ok, err = store.cancel_share(conn, pid, user)
        act = "share_cancel"
        ret = {"ok": True, "scope": "personal",
               "note": "已撤回上架申请（能力照常可用，可随时重新申请）"}
    else:
        ok, err = store.withdraw_share(conn, pid, user)
        act = "share_withdraw"
        ret = {"ok": True, "scope": "personal"}
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, act, {}, _client_ip(request))
    conn.commit()
    return ret


@router.post("/{pid}/submit")
def submit_share(pid: str,
                 request: Request,
                 conn: sqlite3.Connection = Depends(db_session),
                 user=Depends(current_user)):
    """提交共享 = 申请上架公共市场（P1-7 后与 /share 同义，保留以兼容旧前端调用）。

    只动可见范围（personal → pending_public），**不影响能力可用性**。
    """
    ok, err = store.apply_share(conn, pid, user)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, "submit", {"mode": "share"}, _client_ip(request))
    conn.commit()
    return {"ok": True, "scope": "pending_public",
            "note": "已提交上架申请，等待管理员审核（审核期间能力照常可用）"}


@router.post("/{pid}/review")
def review_plugin(pid: str,
                  body: dict,
                  request: Request,
                  conn: sqlite3.Connection = Depends(db_session),
                  user=Depends(require_permission("ai_studio", "market_admin"))):
    """审核。按对象分派（P1-7 解耦后，审核只服务于「上架」）：

      · scope='pending_public' → 上架审核：approve → scope=public / reject → scope=personal
      · status='submitted'     → 历史「发布需审核」链路（兼容旧数据）：approve → published

    两者的关键差异：上架审核**不改 status** —— 审核期间与驳回后，能力对我始终可用。
    """
    action = (body or {}).get("action")
    comment = (body or {}).get("comment", "")
    review_type = (body or {}).get("review_type", "combined")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action 必须为 approve/reject")
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")

    # ① 上架审核（新语义，主路径）
    if p["scope"] == "pending_public":
        ok, err = store.review_share(conn, pid, user,
                                     approve=(action == "approve"), comment=comment)
        if not ok:
            raise HTTPException(409, err)
        store.log_audit(conn, user, pid, "share_review",
                        {"action": action, "comment": comment}, _client_ip(request))
        conn.commit()
        return {"ok": True, "review_kind": "share", "status": p["status"],
                "scope": "public" if action == "approve" else "personal"}

    # ② 历史发布审核（兼容）
    if p["status"] != "submitted":
        raise HTTPException(409, "该能力不在待审核状态")
    target = "published" if action == "approve" else "rejected"
    ok, err = store.transition(conn, pid, target, user, comment, review_type)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, "review",
                    {"action": action, "comment": comment, "review_type": review_type}, _client_ip(request))
    conn.commit()
    return {"ok": True, "review_kind": "publish", "status": target}


# ─────────────────────────── 范围 / 安装 / 启停 ───────────────────────────
@router.put("/{pid}/grant")
def set_grant(pid: str,
              body: dict,
              request: Request,
              conn: sqlite3.Connection = Depends(db_session),
              user=Depends(require_permission("ai_studio", "market_admin"))):
    """范围分配（市场管理员）：{target_type: all|team|role, target_id, permission}"""
    target_type = (body or {}).get("target_type")
    target_id = (body or {}).get("target_id", "")
    permission = (body or {}).get("permission", "use")
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")
    ok, err = store.grant(conn, pid, target_type, target_id, permission)
    if not ok:
        raise HTTPException(400, err)
    store.log_audit(conn, user, pid, "grant",
                    {"target_type": target_type, "target_id": target_id}, _client_ip(request))
    conn.commit()
    return {"ok": True, "grants": store.list_grants(conn, pid)}


@router.post("/{pid}/install")
def install_plugin(pid: str,
                   request: Request,
                   conn: sqlite3.Connection = Depends(db_session),
                   user=Depends(current_user)):
    ok, err = store.install(conn, pid, user)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, "install", {}, _client_ip(request))
    conn.commit()
    return {"ok": True}


@router.post("/{pid}/uninstall")
def uninstall_plugin(pid: str,
                     request: Request,
                     conn: sqlite3.Connection = Depends(db_session),
                     user=Depends(current_user)):
    ok, err = store.uninstall(conn, pid, user)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid, "uninstall", {}, _client_ip(request))
    conn.commit()
    return {"ok": True}


@router.put("/{pid}/enabled")
def set_plugin_enabled(pid: str,
                       body: dict,
                       request: Request,
                       conn: sqlite3.Connection = Depends(db_session),
                       user=Depends(current_user)):
    enabled = bool((body or {}).get("enabled", True))
    ok, err = store.set_enabled(conn, pid, user, enabled)
    if not ok:
        raise HTTPException(403, err)
    store.log_audit(conn, user, pid, "enable" if enabled else "disable", {}, _client_ip(request))
    conn.commit()
    return {"ok": True}


@router.put("/{pid}/installed-enabled")
def set_installed_enabled(pid: str,
                          body: dict,
                          request: Request,
                          conn: sqlite3.Connection = Depends(db_session),
                          user=Depends(current_user)):
    """个人级启停（2026-09-16 新增）：只影响当前用户是否使用该能力。

    与 /{pid}/enabled 的区别（对应 UI 两个不同语义的动作）：
      /enabled            全局上架/下架（管理员），改 plugins.status，影响所有人
      /installed-enabled  个人启用/停用，改 plugin_installs.enabled，仅影响自己
    """
    enabled = bool((body or {}).get("enabled", True))
    ok, err = store.set_install_enabled(conn, pid, user, enabled)
    if not ok:
        raise HTTPException(409, err)
    store.log_audit(conn, user, pid,
                    "install_enable" if enabled else "install_disable", {}, _client_ip(request))
    conn.commit()
    return {"ok": True}


# ─────────────────────────── 执行 / 审计 ───────────────────────────
@router.post("/{pid}/run")
def run_plugin(pid: str,
               body: dict,
               conn: sqlite3.Connection = Depends(db_session),
               user=Depends(current_user)):
    """沙箱执行测试（P0 子进程隔离）：{tool?, params?}。执行结果 + 调用日志。"""
    tool = (body or {}).get("tool", "")
    params = (body or {}).get("params") or {}
    p = store.get_plugin(conn, pid)
    if not p:
        raise HTTPException(404, "插件不存在")
    # 执行权限：个人插件仅作者；公共插件需已安装（管理员/作者除外）
    if p["scope"] == "public":
        inst = None
        if user and user.get("id"):
            inst = conn.execute(
                "SELECT 1 FROM plugin_installs WHERE user_id=? AND plugin_id=?",
                (user["id"], pid)).fetchone()
        if not (inst or _as_admin(user) or _author_or_admin(p, user)):
            raise HTTPException(403, "请先安装该插件再执行（或联系管理员）")
    else:
        if not _author_or_admin(p, user):
            raise HTTPException(403, "个人插件仅作者可执行")
    from plugin_system import executor
    result = executor.execute(conn, p, tool, params, user)
    return {"ok": result.get("ok"), "result": result, "latency_ms": result.get("latency_ms")}


@router.get("/{pid}/audit")
def plugin_audit(pid: str,
                 conn: sqlite3.Connection = Depends(db_session),
                 user=Depends(require_permission("ai_studio", "market_admin"))):
    return {"items": store.list_audit(conn, pid)}


@router.get("/{pid}/calls")
def plugin_calls(pid: str,
                 conn: sqlite3.Connection = Depends(db_session),
                 user=Depends(require_permission("ai_studio", "market_admin"))):
    return {"items": store.list_call_logs(conn, pid)}
