"""用户与角色域：/api/roles, /api/users, /api/permissions/domains

P1/P2 优化：
- 权限域定义源（PERM_DOMAINS）→ 前端权限配置矩阵的唯一权威枚举
- 用户管理补全：删除 / 启停 / 编辑（含 workspace）、重复名友好校验
- 角色管理补全：preset 保护（不可改/删）、删除时引用保护
- 审计归属：从请求头 X-User-Id 识别当前用户（不再写死 admin）
"""
import json
import re as _re
from typing import Optional

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_permission
from repositories.user_repo import UserRepo
from core.audit import audit, audit_user
from models import RoleIn, UserIn, UserStatusIn, DepartmentIn

router = APIRouter(tags=["用户与角色"])

# ── 权限域内置种子（2026-09-01 权限域表化：代码常量降级为种子，运行时读 permission_domains/permission_ops 表；
#    首次建表时写入，之后以表为准。内置种子 builtin=1 不可删除，支持管理员 UI 扩展自定义域/操作项）──
SEED_PERM_DOMAINS = [
    {"key": "ai_chat", "label": "AI 建模对话", "ops": [
        {"key": "view", "label": "查看"}, {"key": "generate", "label": "生成"},
        {"key": "confirm", "label": "确认"}, {"key": "reject", "label": "驳回"}]},
    {"key": "ai_studio", "label": "AI 设计工坊", "ops": [
        {"key": "view", "label": "查看"}, {"key": "edit_prompt", "label": "编辑提示词"},
        {"key": "edit_skill", "label": "编辑技能"}, {"key": "config_mcp", "label": "配置 MCP"},
        {"key": "publish", "label": "发布"}]},
    {"key": "kb_browse", "label": "知识库浏览", "ops": [
        {"key": "view", "label": "查看"}, {"key": "edit_request", "label": "编辑申请"}]},
    {"key": "kb_ontology", "label": "本体管理", "ops": [
        {"key": "view", "label": "查看"}, {"key": "edit", "label": "编辑"},
        {"key": "profile_io", "label": "配置导入导出"}]},
    {"key": "kb_review", "label": "知识评审", "ops": [
        {"key": "view", "label": "查看"}, {"key": "confirm", "label": "确认"},
        {"key": "modify", "label": "修改"}, {"key": "merge", "label": "合并"}]},
    {"key": "branch_dev", "label": "分支开发", "ops": [
        {"key": "create", "label": "创建"}, {"key": "switch", "label": "切换"},
        {"key": "merge_request", "label": "发起合并"}]},
    {"key": "branch_release", "label": "分支发布", "ops": [
        {"key": "view", "label": "查看"}, {"key": "review_merge", "label": "评审合并"},
        {"key": "rollback", "label": "回滚"}]},
    {"key": "report", "label": "报告", "ops": [
        {"key": "view", "label": "查看"}, {"key": "export", "label": "导出"}]},
    {"key": "admin", "label": "系统管理", "ops": [
        {"key": "user_manage", "label": "用户管理"}, {"key": "role_manage", "label": "角色管理"},
        {"key": "audit_view", "label": "审计查看"}, {"key": "integration_config", "label": "集成配置"},
        {"key": "ops_manage", "label": "运维管理"}]},
]

# ── 权限域表读写（2026-09-01 表化：运行时从表读，缺表/空表回退种子）──
def ensure_permission_tables(conn):
    """建表（幂等）+ 首次写入内置种子。返回 True 表示执行了种子填充。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS permission_domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_key TEXT UNIQUE NOT NULL,
        label TEXT NOT NULL,
        builtin INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS permission_ops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_key TEXT NOT NULL REFERENCES permission_domains(domain_key) ON DELETE CASCADE,
        op_key TEXT NOT NULL,
        label TEXT NOT NULL,
        builtin INTEGER DEFAULT 1,
        UNIQUE(domain_key, op_key)
    )""")
    n = conn.execute("SELECT COUNT(*) FROM permission_domains").fetchone()[0]
    if n > 0:
        return False
    for dom in SEED_PERM_DOMAINS:
        conn.execute(
            "INSERT INTO permission_domains (domain_key, label, builtin, sort_order) VALUES (?,?,1,?)",
            (dom["key"], dom["label"], len(SEED_PERM_DOMAINS) - SEED_PERM_DOMAINS.index(dom)))
        for op in dom["ops"]:
            conn.execute(
                "INSERT INTO permission_ops (domain_key, op_key, label, builtin) VALUES (?,?,?,1)",
                (dom["key"], op["key"], op["label"]))
    conn.commit()
    return True


def load_permission_domains(conn) -> list:
    """从表读全部权限域（含操作项）。表缺失/为空时回退种子常量。"""
    try:
        doms = [dict(r) for r in conn.execute(
            "SELECT domain_key AS key, label, builtin, sort_order FROM permission_domains"
            " ORDER BY sort_order DESC, id").fetchall()]
        if not doms:
            return SEED_PERM_DOMAINS
        ops = [dict(r) for r in conn.execute(
            "SELECT domain_key, op_key AS key, label FROM permission_ops ORDER BY id").fetchall()]
        op_map = {}
        for o in ops:
            op_map.setdefault(o["domain_key"], []).append({"key": o["key"], "label": o["label"]})
        for d in doms:
            d["ops"] = op_map.get(d["key"], [])
        return doms
    except Exception:
        return SEED_PERM_DOMAINS


# 规范化：仅保留表内存在的域/操作，丢弃未知键（防止脏数据污染矩阵）
def _sanitize_permissions(raw: Optional[dict], conn) -> dict:
    if not isinstance(raw, dict):
        return {}
    valid_domains = {d["key"]: {o["key"] for o in d["ops"]} for d in load_permission_domains(conn)}
    out = {}
    for dkey, ops in raw.items():
        if dkey not in valid_domains or not isinstance(ops, list):
            continue
        kept = [o for o in ops if o in valid_domains[dkey]]
        if kept:
            out[dkey] = kept
    return out


def _actor(user) -> str:
    """审计归属：当前登录用户显示名；未识别时标记为匿名。（兼容旧名，统一走 core.audit.audit_user）"""
    return audit_user(user)


@router.get("/api/permissions/domains")
def list_permission_domains(conn=Depends(db_session)):
    """权限域定义源（2026-09-01 表化后从 permission_domains/permission_ops 读，含 builtin 标记）。"""
    ensure_permission_tables(conn)
    return {"domains": load_permission_domains(conn)}


# ── 权限域表化管理端点（2026-09-01）：免代码扩展权限模型；内置种子 builtin=1 不可删/不可改键 ──
@router.post("/api/permissions/domains")
def perm_domain_create(body: dict, conn=Depends(db_session),
                       user=Depends(require_permission("admin", "role_manage"))):
    """新增自定义权限域（管理员）。域键唯一；操作项可一并提交。"""
    dkey = str(body.get("key", "")).strip()
    label = str(body.get("label", "")).strip()
    if not dkey or not label:
        return JSONResponse({"error": "域键与显示名必填"}, 400)
    if not _key_ok(dkey):
        return JSONResponse({"error": "域键仅允许小写字母/数字/下划线"}, 400)
    ensure_permission_tables(conn)
    if conn.execute("SELECT 1 FROM permission_domains WHERE domain_key=?", (dkey,)).fetchone():
        return JSONResponse({"error": f"权限域「{dkey}」已存在"}, 400)
    conn.execute("INSERT INTO permission_domains (domain_key, label, builtin, sort_order) VALUES (?,?,0,0)",
                 (dkey, label))
    for op in (body.get("ops") or []):
        okey, olab = str(op.get("key", "")).strip(), str(op.get("label", "")).strip()
        if okey and olab:
            conn.execute("INSERT INTO permission_ops (domain_key, op_key, label, builtin) VALUES (?,?,?,0)",
                         (dkey, okey, olab))
    conn.commit()
    audit(audit_user(user), "perm_domain_create", f"新增权限域 {dkey}:{label}", conn=conn)
    return {"ok": True}


@router.put("/api/permissions/domains/{dkey}")
def perm_domain_update(dkey: str, body: dict, conn=Depends(db_session),
                       user=Depends(require_permission("admin", "role_manage"))):
    """更新权限域显示名（键不可改）。"""
    ensure_permission_tables(conn)
    dom = conn.execute("SELECT * FROM permission_domains WHERE domain_key=?", (dkey,)).fetchone()
    if not dom:
        return JSONResponse({"error": f"权限域「{dkey}」不存在"}, 404)
    label = str(body.get("label", "")).strip()
    if not label:
        return JSONResponse({"error": "显示名必填"}, 400)
    conn.execute("UPDATE permission_domains SET label=?, updated_at=CURRENT_TIMESTAMP WHERE domain_key=?",
                 (label, dkey))
    conn.commit()
    audit(audit_user(user), "perm_domain_update", f"更新权限域 {dkey} 显示名→{label}", conn=conn)
    return {"ok": True}


@router.delete("/api/permissions/domains/{dkey}")
def perm_domain_delete(dkey: str, conn=Depends(db_session),
                       user=Depends(require_permission("admin", "role_manage"))):
    """删除权限域（内置种子禁止删除；删除时校验角色引用避免权限失效无感知）。"""
    ensure_permission_tables(conn)
    dom = conn.execute("SELECT * FROM permission_domains WHERE domain_key=?", (dkey,)).fetchone()
    if not dom:
        return JSONResponse({"error": f"权限域「{dkey}」不存在"}, 404)
    if dom["builtin"]:
        return JSONResponse({"error": "内置权限域禁止删除（种子域为系统基础）"}, 400)
    # 角色引用检查：任何角色权限含该域 → 阻断
    roles = conn.execute("SELECT id, name, permissions FROM roles").fetchall()
    for r in roles:
        try:
            perms = json.loads(r["permissions"] or "{}")
        except Exception:
            continue
        if dkey in perms:
            return JSONResponse(
                {"error": f"角色「{r['name']}」仍包含该域权限，请先调整角色再删除"}, 400)
    conn.execute("DELETE FROM permission_domains WHERE domain_key=?", (dkey,))  # ops 级联删除
    conn.commit()
    audit(audit_user(user), "perm_domain_delete", f"删除权限域 {dkey}", conn=conn)
    return {"ok": True}


@router.post("/api/permissions/domains/{dkey}/ops")
def perm_op_create(dkey: str, body: dict, conn=Depends(db_session),
                   user=Depends(require_permission("admin", "role_manage"))):
    """域内新增操作项。"""
    ensure_permission_tables(conn)
    if not conn.execute("SELECT 1 FROM permission_domains WHERE domain_key=?", (dkey,)).fetchone():
        return JSONResponse({"error": f"权限域「{dkey}」不存在"}, 404)
    okey = str(body.get("key", "")).strip()
    olab = str(body.get("label", "")).strip()
    if not okey or not olab:
        return JSONResponse({"error": "操作键与显示名必填"}, 400)
    if conn.execute("SELECT 1 FROM permission_ops WHERE domain_key=? AND op_key=?", (dkey, okey)).fetchone():
        return JSONResponse({"error": f"操作项「{okey}」已存在"}, 400)
    conn.execute("INSERT INTO permission_ops (domain_key, op_key, label, builtin) VALUES (?,?,?,0)",
                 (dkey, okey, olab))
    conn.commit()
    audit(audit_user(user), "perm_op_create", f"权限域 {dkey} 新增操作项 {okey}", conn=conn)
    return {"ok": True}


@router.delete("/api/permissions/domains/{dkey}/ops/{opkey}")
def perm_op_delete(dkey: str, opkey: str, conn=Depends(db_session),
                   user=Depends(require_permission("admin", "role_manage"))):
    """删除操作项（内置种子禁止删除）。"""
    ensure_permission_tables(conn)
    op = conn.execute("SELECT * FROM permission_ops WHERE domain_key=? AND op_key=?",
                      (dkey, opkey)).fetchone()
    if not op:
        return JSONResponse({"error": "操作项不存在"}, 404)
    if op["builtin"]:
        return JSONResponse({"error": "内置操作项禁止删除（种子为系统基础）"}, 400)
    conn.execute("DELETE FROM permission_ops WHERE domain_key=? AND op_key=?", (dkey, opkey))
    conn.commit()
    audit(audit_user(user), "perm_op_delete", f"权限域 {dkey} 删除操作项 {opkey}", conn=conn)
    return {"ok": True}


re_fullmatch_re = _re.compile(r"^[a-z0-9_]+$")


def _key_ok(s: str) -> bool:
    """域/操作键格式：小写字母/数字/下划线。"""
    return bool(re_fullmatch_re.match(s))


# ── roles ──
@router.get("/api/roles")
def list_roles(conn=Depends(db_session)):
    return UserRepo(conn).list_roles()


@router.post("/api/roles")
def create_role(role: RoleIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = UserRepo(conn)
    if repo.get_role_by_name(role.name):
        return JSONResponse({"error": f"角色名「{role.name}」已存在"}, 400)
    repo.create_role(role.name, role.description, json.dumps(_sanitize_permissions(role.permissions, conn), ensure_ascii=False))
    audit(_actor(user), "role_create", f"创建角色: {role.name}", conn=conn)
    return {"ok": True}


@router.put("/api/roles/{role_id}")
def update_role(role_id: int, role: RoleIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = UserRepo(conn)
    existing = repo.get_role(role_id)
    if not existing:
        return JSONResponse({"error": "角色不存在"}, 404)
    if existing["type"] == "preset":
        return JSONResponse({"error": "预置角色不可修改，请复制为新角色后调整"}, 400)
    dup = repo.get_role_by_name(role.name)
    if dup and dup["id"] != role_id:
        return JSONResponse({"error": f"角色名「{role.name}」已存在"}, 400)
    repo.update_role(role_id, role.name, role.description,
                     json.dumps(_sanitize_permissions(role.permissions, conn), ensure_ascii=False))
    audit(_actor(user), "role_update", f"更新角色: {role.name}", conn=conn)
    return {"ok": True}


@router.delete("/api/roles/{role_id}")
def delete_role(role_id: int, conn=Depends(db_session), user=Depends(current_user)):
    repo = UserRepo(conn)
    role = repo.get_role(role_id)
    if not role:
        return JSONResponse({"error": "角色不存在"}, 404)
    if role["type"] == "preset":
        return JSONResponse({"error": "预置角色不可删除"}, 400)
    refs = repo.count_users_by_role(role_id)
    if refs > 0:
        return JSONResponse({"error": f"该角色下仍有 {refs} 个用户，请先调整这些用户的角色再删除"}, 400)
    repo.delete_role(role_id)
    audit(_actor(user), "role_delete", f"删除角色: {role['name']}", conn=conn)
    return {"ok": True}


# ── users ──
@router.get("/api/users/me")
def get_current_user_info(u=Depends(current_user)):
    """当前登录用户信息（工作台角色化问候/角色专区等前端消费）。"""
    if not u:
        return {"error": "未登录"}
    return {
        "id": u["id"],
        "username": u["username"],
        "display_name": u["display_name"],
        "department": u.get("department") or "",
        "role_id": u.get("role_id"),
        "role_name": u.get("role_name") or "",
        "role_type": u.get("role_type") or "",
        "workspace": u.get("workspace") or "",
        "permissions": u.get("permissions") or {},   # {domain:[ops]} 角色化布局判定
    }


@router.get("/api/users")
def list_users(conn=Depends(db_session)):
    return UserRepo(conn).list_users()


@router.post("/api/users")
def create_user(user: UserIn, conn=Depends(db_session), u=Depends(current_user)):
    repo = UserRepo(conn)
    if repo.get_user_by_username(user.username):
        return JSONResponse({"error": f"用户名「{user.username}」已存在"}, 400)
    if not user.display_name.strip():
        return JSONResponse({"error": "显示名不能为空"}, 400)
    ws = (user.workspace or "").strip() or f"ws-{user.username}"
    try:
        repo.create_user(user.username, user.display_name, user.department, user.role_id, ws)
    except sqlite3.IntegrityError as e:
        return JSONResponse({"error": f"创建失败：所选角色不存在或已被删除（{str(e)[:60]}）"}, 400)
    audit(_actor(u), "user_create", f"创建用户: {user.display_name}", conn=conn)
    return {"ok": True}


@router.put("/api/users/{user_id}")
def update_user(user_id: int, user: UserIn, conn=Depends(db_session), u=Depends(current_user)):
    repo = UserRepo(conn)
    if not repo.get_user(user_id):
        return JSONResponse({"error": "用户不存在"}, 404)
    ws = (user.workspace or "").strip() or None
    status = user.status if user.status in ("active", "disabled") else None
    try:
        repo.update_user(user_id, user.display_name, user.department, user.role_id, status, ws)
    except sqlite3.IntegrityError as e:
        return JSONResponse({"error": f"更新失败：所选角色不存在或已被删除（{str(e)[:60]}）"}, 400)
    audit(_actor(u), "user_update", f"更新用户: {user.display_name}", conn=conn)
    return {"ok": True}


@router.patch("/api/users/{user_id}/status")
def update_user_status(user_id: int, body: UserStatusIn, conn=Depends(db_session), u=Depends(current_user)):
    """启用 / 停用用户。"""
    repo = UserRepo(conn)
    existing = repo.get_user(user_id)
    if not existing:
        return JSONResponse({"error": "用户不存在"}, 404)
    if body.status not in ("active", "disabled"):
        return JSONResponse({"error": "status 仅支持 active/disabled"}, 400)
    repo.update_user_status(user_id, body.status)
    audit(_actor(u), "user_status", f"用户 {existing['display_name']} → {body.status}", conn=conn)
    return {"ok": True}


@router.delete("/api/users/{user_id}")
def delete_user(user_id: int, conn=Depends(db_session), u=Depends(current_user)):
    """删除用户；禁止删除当前登录用户；有会话/对话引用时拒绝。"""
    repo = UserRepo(conn)
    existing = repo.get_user(user_id)
    if not existing:
        return JSONResponse({"error": "用户不存在"}, 404)
    if u and u.get("id") == user_id:
        return JSONResponse({"error": "不能删除当前登录用户"}, 400)
    refs = repo.count_conversations_by_user(user_id)
    if refs > 0:
        return JSONResponse({"error": f"该用户存在 {refs} 条会话记录，请先处理（或归档）后再删除"}, 400)
    try:
        repo.delete_user(user_id)
    except Exception as e:  # 外键约束等兜底 → 可读提示
        return JSONResponse({"error": f"删除失败：{str(e)[:80]}"}, 400)
    audit(_actor(u), "user_delete", f"删除用户: {existing['display_name']}", conn=conn)
    return {"ok": True}


# ── departments（部门设置：用户归属部门受控词表）──
@router.get("/api/departments")
def list_departments(conn=Depends(db_session)):
    return UserRepo(conn).list_departments()


@router.post("/api/departments")
def create_department(dep: DepartmentIn, conn=Depends(db_session), u=Depends(current_user)):
    repo = UserRepo(conn)
    name = dep.name.strip()
    if not name:
        return JSONResponse({"error": "部门名称不能为空"}, 400)
    if repo.get_department_by_name(name):
        return JSONResponse({"error": f"部门「{name}」已存在"}, 400)
    repo.create_department(name, dep.description or "", int(dep.sort_order or 0))
    audit(_actor(u), "department_create", f"创建部门: {name}", conn=conn)
    return {"ok": True}


@router.put("/api/departments/{dep_id}")
def update_department(dep_id: int, dep: DepartmentIn, conn=Depends(db_session), u=Depends(current_user)):
    repo = UserRepo(conn)
    existing = repo.get_department(dep_id)
    if not existing:
        return JSONResponse({"error": "部门不存在"}, 404)
    name = dep.name.strip()
    if not name:
        return JSONResponse({"error": "部门名称不能为空"}, 400)
    dup = repo.get_department_by_name(name)
    if dup and dup["id"] != dep_id:
        return JSONResponse({"error": f"部门「{name}」已存在"}, 400)
    repo.update_department(dep_id, name, dep.description or "",
                          int(dep.sort_order or 0), dep.status or "active")
    if existing["name"] != name:
        repo.rename_users_department(existing["name"], name)   # 同步用户历史归属
    audit(_actor(u), "department_update", f"更新部门: {name}", conn=conn)
    return {"ok": True}


@router.delete("/api/departments/{dep_id}")
def delete_department(dep_id: int, conn=Depends(db_session), u=Depends(current_user)):
    repo = UserRepo(conn)
    dep = repo.get_department(dep_id)
    if not dep:
        return JSONResponse({"error": "部门不存在"}, 404)
    refs = repo.count_users_by_department_name(dep["name"])
    if refs > 0:
        return JSONResponse({"error": f"该部门下仍有 {refs} 名用户，请先调整这些用户的部门再删除"}, 400)
    repo.delete_department(dep_id)
    audit(_actor(u), "department_delete", f"删除部门: {dep['name']}", conn=conn)
    return {"ok": True}
