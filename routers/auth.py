"""认证授权（FR-UR-1/FR-UR-2）：IAM 统一身份认证（OAuth2.0 授权码）+ 本地体验模式。

对接文档：《接口对接.pdf》（客户方 IAM-统一身份认证-单点登录 v1.9，格尔 kidaas/koal）。
选型：OAuth2.0 授权码模式 —— ①FR-UR-1 需求原文即「LDAP 或 OAuth2」；②前后端分离架构
原生支持（文档 §5）；③不依赖格尔零信任网关部署（Cookie 方式必须网关代理）。

IAM 端点（base 默认 https://sso-test.chinasatnet.com.cn，authorize 路径以 IDAAS
平台应用配置页为准）：
- 授权:   {base}/authn-api/v5/oauth/authorize?client_id&redirect_uri&response_type=code&state
- 换token: {base}/authn-api/v5/oauth/token  (client_id/client_secret/redirect_uri/
           grant_type=authorization_code/code) → access_token/refresh_token/expires_in
- 用户:   {base}/authn-api/v5/oauth/user-info (Authorization: access_token)
          → {id, displayName, mobile}（附录字段：无角色/部门 —— FR-UR-2 角色用本地映射）
- 后端登出: {base}/idaas/authn-api/v5/logout (accessToken form)

配置（core/config.py auth 段，部署时改）：mode=local|sso、sso_base、client_id、
client_secret、redirect_uri、default_role_id、session_ttl_hours、enforce_login。

现阶段（米爸 2026-09-23）：登录界面已提供、对接代码就绪，但 mode=local 体验模式
**免校验**（任意显示名进入，绑默认角色）——无真实数据，不强制。
"""
import json
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core.config import get
from core.deps import db_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ROLE_FALLBACK = 80  # 设计师：IAM 新用户默认角色（FR-UR-2 本地映射；roles.id=80）


def _auth_cfg() -> dict:
    return get("auth", default={}) or {}


def _ensure_sessions_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            iam_access_token TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))"""
    )


def _issue_session(conn, user_id: int, iam_access_token: str = "") -> dict:
    _ensure_sessions_table(conn)
    ttl_h = int(_auth_cfg().get("session_ttl_hours") or 12)
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=ttl_h)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO auth_sessions (token, user_id, iam_access_token, expires_at) VALUES (?,?,?,?)",
        (token, user_id, iam_access_token, expires))
    return {"token": token, "expires_at": expires}


def _upsert_user(conn, username: str, display_name: str, source: str,
                 role_id: int | None = None, extra: dict | None = None) -> dict:
    """按 username 幂等建档（FR-UR-2：IAM 用户 → 本工程 users/roles 映射）。

    IAM 用户信息字段仅 id/displayName/mobile（无角色）→ 角色取 auth.default_role_id
    （本地映射，管理员可在用户管理调整）；待与 IAM 管理员确认角色字段扩展后可自动映射。
    """
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row:
        conn.execute("UPDATE users SET display_name=?, source=? WHERE id=?",
                     (display_name, source, row["id"]))
        uid = row["id"]
    else:
        rid = role_id or int(_auth_cfg().get("default_role_id") or _ROLE_FALLBACK)
        ws = extra.get("workspace") if extra else None
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role_id, source, workspace, status) "
            "VALUES (?,?,?,?,?,?)",
            (username, display_name, rid, source,
             ws or f"ws-{username}", "active"))
        uid = cur.lastrowid
    u = conn.execute(
        """SELECT u.*, r.name AS role_name, r.type AS role_type, r.permissions AS role_permissions
           FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?""", (uid,)).fetchone()
    return dict(u)


def _public_user(u: dict) -> dict:
    return {k: u.get(k) for k in ("id", "username", "display_name", "department",
                                  "role_id", "role_name", "role_type", "workspace", "source")}


@router.get("/config")
def auth_config():
    """前端启动时取认证配置：mode 与（已配置时）SSO 授权跳转地址。"""
    cfg = _auth_cfg()
    mode = cfg.get("mode") or "local"
    sso_url = None
    if mode == "sso" and cfg.get("client_id"):
        q = urllib.parse.urlencode({
            "client_id": cfg["client_id"],
            "redirect_uri": cfg.get("redirect_uri") or "",
            "response_type": "code",
            "state": secrets.token_urlsafe(8),
        })
        sso_url = f"{cfg.get('sso_base')}/authn-api/v5/oauth/authorize?{q}"
    return {"mode": mode, "sso_authorize_url": sso_url,
            "enforce_login": bool(cfg.get("enforce_login"))}


@router.post("/local-login")
def local_login(body: dict = Body(...), conn=Depends(db_session)):
    """体验模式登录（免校验）：任意显示名进入，绑默认角色 —— 现阶段无真实数据（米爸）。"""
    name = (body.get("display_name") or "").strip()
    if not name:
        raise HTTPException(400, "display_name 必填")
    u = _upsert_user(conn, f"local-{name}", name, source="local")
    sess = _issue_session(conn, u["id"])
    return {"ok": True, "token": sess["token"], "expires_at": sess["expires_at"],
            "user": _public_user(u)}


@router.get("/sso/callback")
def sso_callback(code: str, state: str = "", conn=Depends(db_session)):
    """IAM OAuth2.0 回调：code 换 token → user-info → 本地建档 → 签发会话。"""
    cfg = _auth_cfg()
    if not cfg.get("client_id"):
        raise HTTPException(400, "auth.client_id 未配置（IDAAS 平台应用注册后获取）")
    if not code:
        raise HTTPException(400, "缺少授权码 code")
    # ① code 换 token（文档 §3.3.1 第二步）
    token_ep = f"{cfg.get('sso_base')}/authn-api/v5/oauth/token"
    data = urllib.parse.urlencode({
        "client_id": cfg["client_id"], "client_secret": cfg.get("client_secret") or "",
        "redirect_uri": cfg.get("redirect_uri") or "",
        "grant_type": "authorization_code", "code": code}).encode()
    req = urllib.request.Request(token_ep, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(502, f"IAM 换取 token 失败：{e}")
    access = tok.get("access_token") or ""
    if not access:
        raise HTTPException(502, f"IAM 未返回 access_token：{tok}")
    # ② user-info（文档 §3.3.1 第三步）
    req2 = urllib.request.Request(
        f"{cfg.get('sso_base')}/authn-api/v5/oauth/user-info",
        headers={"Authorization": access})
    try:
        with urllib.request.urlopen(req2, timeout=10) as resp:
            ui = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(502, f"IAM 获取用户信息失败：{e}")
    profile = (ui.get("result") or {}) if isinstance(ui, dict) else {}
    iam_id = str(profile.get("id") or "")
    display = profile.get("displayName") or f"iam-{iam_id}"
    if not iam_id:
        raise HTTPException(502, f"IAM 用户信息缺少 id：{ui}")
    # ③ 本地建档（FR-UR-2）+ 签发会话
    u = _upsert_user(conn, f"iam-{iam_id}", display, source="iam")
    sess = _issue_session(conn, u["id"], iam_access_token=access)
    return {"ok": True, "token": sess["token"], "expires_at": sess["expires_at"],
            "user": _public_user(u)}


@router.get("/me")
def me(request: Request, conn=Depends(db_session)):
    """当前用户（走 current_user 双读：X-Session-Token 优先 / X-User-Id 兼容）。"""
    from core.deps import current_user
    user = current_user(request, conn)
    if not user:
        return {"authenticated": False, "user": None}
    sess = conn.execute(
        "SELECT expires_at FROM auth_sessions WHERE token=?",
        (request.headers.get("X-Session-Token") or "",)).fetchone()
    return {"authenticated": True, "user": _public_user(user),
            "expires_at": sess["expires_at"] if sess else None}


@router.post("/logout")
def logout(request: Request, conn=Depends(db_session)):
    """登出：删本地会话；IAM 模式下 best-effort 调后端登出接口（通知 SSO 全局登出）。"""
    token = request.headers.get("X-Session-Token") or (body := {}).get("token", "")
    # 顺带清理全表过期会话（避免 auth_sessions 无限累积；无独立定时任务，搭车最省）
    conn.execute("DELETE FROM auth_sessions WHERE expires_at < datetime('now','localtime')")
    if not token:
        return {"ok": True}
    row = conn.execute("SELECT * FROM auth_sessions WHERE token=?", (token,)).fetchone()
    if row:
        conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
        iam_tok = row["iam_access_token"]
        if iam_tok:
            cfg = _auth_cfg()
            try:
                data = urllib.parse.urlencode({"accessToken": iam_tok}).encode()
                req = urllib.request.Request(
                    f"{cfg.get('sso_base')}/idaas/authn-api/v5/logout", data=data, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass  # SSO 侧登出失败不阻断本地登出（本地会话已删）
    return {"ok": True}

