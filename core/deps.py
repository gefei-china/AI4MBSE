"""公共 FastAPI 依赖（Dependency Injection 入口）。

P2 起：路由通过 `conn: sqlite3.Connection = Depends(db_session)` 获取
请求级连接，repo 层（repositories/）负责 SQL 收拢。
- 请求成功 → 自动 COMMIT（保持原 db_conn() 写路径语义）
- 请求异常 → 自动 ROLLBACK（审计日志由 audit() 独立提交，不受影响）
测试可替换为临时内存库，实现仓储层隔离。
"""
import json
import sqlite3

from fastapi import Depends, HTTPException, Request

from database import get_db


def db_session():
    """请求级数据库连接：成功提交、异常回滚、结束关闭。"""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _user_payload(row) -> dict:
    """users+roles 行 → 统一用户载荷（permissions 由 role_permissions JSON 解析）。"""
    u = dict(row)
    try:
        u["permissions"] = json.loads(u.get("role_permissions") or "{}")
    except Exception:
        u["permissions"] = {}
    return u


def current_user(request: Request, conn=Depends(db_session)):
    """会话双读（X-Session-Token 优先 / X-User-Id 兼容）识别当前登录用户。

    enforce_login=True（settings auth.enforce_login）：无任何身份头直接 401 —— 正式上线开关；
    False（现阶段）：未认证返回 None，端点自行按匿名/降级处理（兼容体验期与无头脚本）。
    返回 {id, username, display_name, role_id, role_name, role_type, permissions} | None。
    FastAPI 依赖缓存（use_cache）保证与路由 conn 共用同一请求级连接。
    """
    try:
        from core.config import get as _cfg_get
        if bool(_cfg_get("auth.enforce_login")):
            _tok = request.headers.get("X-Session-Token") or ""
            _uid = request.headers.get("X-User-Id") or ""
            if not _tok and not _uid:
                from fastapi import HTTPException
                raise HTTPException(status_code=401, detail="未登录（enforce_login 已开启）")
    except ImportError:
        pass
    # 2026-09-23 FR-UR-1：会话 token 双读 —— X-Session-Token（auth_sessions 表，
    # routers/auth.py 签发）优先；X-User-Id 兼容期保留（前端/脚本平滑迁移）。
    sess_token = request.headers.get("X-Session-Token")
    if sess_token:
        sess = conn.execute(
            "SELECT user_id, expires_at FROM auth_sessions WHERE token=?", (sess_token,)).fetchone()
        if sess and sess["expires_at"] >= __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
            row = conn.execute(
                """SELECT u.*, r.name AS role_name, r.type AS role_type, r.permissions AS role_permissions
                   FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?""",
                (sess["user_id"],),
            ).fetchone()
            if row:
                return _user_payload(row)
    uid = request.headers.get("X-User-Id")
    if not uid:
        return None
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        """SELECT u.*, r.name AS role_name, r.type AS role_type, r.permissions AS role_permissions
           FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?""",
        (uid,),
    ).fetchone()
    if not row:
        return None
    u = dict(row)
    try:
        u["permissions"] = json.loads(u.get("role_permissions") or "{}")
    except Exception:
        u["permissions"] = {}
    return u


def require_permission(domain: str, op: str):
    """权限依赖工厂：要求当前用户拥有 domain:op 权限（角色权限矩阵）。

    - 匿名请求（current_user 为 None，如测试脚本/未登录场景）→ 直接放行，向后兼容；
    - 用户存在时：permissions 中 admin 域权限列表非空视为超级用户 → 放行；
      否则 domain 存在且 op ∈ permissions[domain] → 放行；其余 → 403。
    返回值仍为 current_user 用户信息，端点可继续用作审计归属。
    """
    def checker(user=Depends(current_user)):
        if user is None:
            return user
        perms = user.get("permissions") or {}
        # admin 域权限列表非空才视为超级用户（种子数据中所有角色均带 "admin" 键，
        # 空列表不代表管理员，避免设计师/知识工程师被误判放行）
        if perms.get("admin"):
            return user
        ops = perms.get(domain) or []
        if op in ops:
            return user
        raise HTTPException(status_code=403,
                            detail=f"无权限执行该操作（需要 {domain}:{op} 权限）")
    return checker


def require_any_permission(pairs):
    """权限依赖工厂：任一 (domain, op) 满足即放行（如写操作双角色兼容）。

    匿名请求直接放行；admin 域权限列表非空视为超级用户放行；全部不满足 → 403。
    pairs: [(domain1, op1), (domain2, op2), ...]
    """
    def checker(user=Depends(current_user)):
        if user is None:
            return user
        perms = user.get("permissions") or {}
        # admin 域权限列表非空才视为超级用户（空列表不代表管理员，见 require_permission）
        if perms.get("admin"):
            return user
        for domain, op in pairs:
            ops = perms.get(domain) or []
            if op in ops:
                return user
        need = " 或 ".join(f"{d}:{o}" for d, o in pairs)
        raise HTTPException(status_code=403,
                            detail=f"无权限执行该操作（需要 {need} 权限）")
    return checker
