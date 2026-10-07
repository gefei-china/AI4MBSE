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


# ══════════════════════════════════════════════════════════════════════════
# P0-5（2026-10-03 整改）：强制鉴权收口
#
# 依据《架构-可扩展性-稳定性整体评估与标杆对标》§4.1 / §6 P0-5 实测三条漏洞：
#   D1 身份可自报 —— `X-User-Id` 请求头自报身家即被采信 ⇒ 任意人冒充任意用户；
#      叠加 `auth.enforce_login=False` 与 `users` 表无口令列 ⇒ 匿名请求一路 200。
#   D2 token 无效仍回落 —— 旧实现「token 查得到就用，查不到继续读 X-User-Id」，
#      于是**随手伪造一个 session token 就能把 X-User-Id 通道重新打开**：
#      形式上加了锁，实际上留了窗。这条靠读代码极易漏判（分支互斥没做完），
#      已锁进 tools/verify/verify_auth_enforce.py 的 I2 不变式。
#   D3 RBAC 对匿名放行 —— `require_permission` 里 `if user is None: return user`。
#
# 对标 **Dify**（2026 平台综述：工作区多租户 + RBAC）：身份只有两条来路 ——
# 会话凭据（浏览器）或 API Key（服务端），均不可伪造，且**不存在"弱凭据兜底强凭据"
# 的回落路径**。本修复按该语义收口，三件事：
#   ① 强凭据**排他**：带了 session token 就只看 token，失败即失败，绝不回落；
#   ② X-User-Id 由 `auth.trust_user_id_header` 显式开关托管（默认 True=兼容期，
#      上线 checklist 置 False —— 改配置就完成加固，无需再动代码）；
#   ③ enforce_login=True 时无身份 → 401，此时 require_permission 的匿名分支
#      不可达 ⇒ RBAC 自动闭合（不必另加开关，避免"两个开关语义打架"）。
#
# ⚠️ 变异锚点（勿改名）：以下 `_MUTATION_GUARD_*` / `_mut_*` 供自检脚本注入错误写法做
#    变异自证。删除或改名会让 tools/verify/verify_auth_enforce.py 直接判 VACUOUS。
# ══════════════════════════════════════════════════════════════════════════

_MUTATION_GUARD_REJECT_FALLBACK = True      # 变异锚点：禁止"token 无效→回落 X-User-Id"
_MUTATION_GUARD_ANON_PASSTHROUGH = True     # 变异锚点：非强制态匿名仍放行（脚本兼容）

_USER_SQL = """SELECT u.*, r.name AS role_name, r.type AS role_type, r.permissions AS role_permissions
               FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?"""


def _auth_cfg() -> dict:
    try:
        from core.config import get as _cfg_get
        v = _cfg_get("auth", default={})
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _user_payload(row) -> dict:
    """users+roles 行 → 统一用户载荷（permissions 由 role_permissions JSON 解析）。"""
    u = dict(row)
    try:
        u["permissions"] = json.loads(u.get("role_permissions") or "{}")
    except Exception:
        u["permissions"] = {}
    return u


def _spoof_guard(request: Request, reason: str, extra: str = "") -> None:
    """P0-5：身份伪造/失效嫌疑留痕 —— **只留痕，不改变控制流**。

    为什么必须是旁路：认证失败路径通常以 HTTPException 结束，`db_session` 会整体
    ROLLBACK，若审计写在同事务里会被一并回滚 —— 恰恰最该留的证据没了。此处照
    `routers/auth.py:_audit_auth` 的先例：审计写失败也不得阻断认证主流程。
    """
    try:
        from core.audit import audit
        peer = ""
        try:
            peer = (request.client.host if request and request.client else "") or ""
        except Exception:
            peer = ""
        audit("可疑请求（未认证）", "auth_identity_reject",
              f"{reason} source_ip={peer} {extra}".strip(), "failed")
    except Exception as e:  # pragma: no cover
        try:
            import logging
            logging.getLogger("mbse.auth").warning("身份校验告警写入失败：%s", str(e)[:120])
        except Exception:
            pass


def _session_user(conn, token: str):
    """按会话 token 解析用户；返回 user dict | None（无效/过期/表缺失均 None，不抛异常）。"""
    try:
        import datetime as _dt
        sess = conn.execute(
            "SELECT user_id, expires_at FROM auth_sessions WHERE token=?", (token,)).fetchone()
    except sqlite3.OperationalError:
        # auth_sessions 由 routers/auth.py 懒建（首次登录才 CREATE TABLE IF NOT EXISTS）。
        # 尚未有人登录的环境若在此抛异常 ⇒ 全站 500。**安全性与可用性冲突时，这条只能
        # 让可用性赢**：表都没有 = 没有任何会话可伪造，返回无身份与"查不到"等价。
        return None
    if not sess:
        return None
    exp = sess["expires_at"] or ""
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if exp < now:                      # 字符串比较前提：expires_at 与 now 同为 localtime 口径
        return None
    row = conn.execute(_USER_SQL, (sess["user_id"],)).fetchone()
    return _user_payload(row) if row else None


def current_user(request: Request, conn=Depends(db_session)):
    """当前登录用户识别：**会话 token 唯一强身份**（P0-5 整改）。

    判定顺序（严格串行，弱凭据**永不**兜底强凭据）：
      ① `X-Session-Token`（auth_sessions 表，routers/auth.py 签发）→ 命中即用；
         **带了 token 却无效 ⇒ 直接判无身份**，不再读 X-User-Id（堵死 D2）。
      ② 仅在**完全没带 token** 且 `auth.trust_user_id_header=True` 时读 `X-User-Id`
         （兼容期：前端/自检脚本平滑迁移；上线置 False 即关闭）。
      ③ `auth.enforce_login=True` 且走到这里仍无身份 → 401。此时 require_permission
         的匿名分支不可达 ⇒ RBAC 自动闭合（D1/D3 一并解决）。

    返回 {id, username, display_name, role_id, role_name, role_type, permissions} | None。
    FastAPI 依赖缓存（use_cache）保证与路由 conn 共用同一请求级连接。
    """
    cfg = _auth_cfg()
    enforce = bool(cfg.get("enforce_login"))
    trust_uid = bool(cfg.get("trust_user_id_header", True))

    # ① 强凭据优先且排他
    sess_token = (request.headers.get("X-Session-Token") or "").strip()
    if sess_token:
        u = _session_user(conn, sess_token)
        if u:
            return u
        _mut_reject_fallback = _MUTATION_GUARD_REJECT_FALLBACK
        if _mut_reject_fallback:
            # ⚠️ 2026-10-03 P0-5/D2：这里是**整段修复的核心**。
            #    旧实现在此处不做任何处理，继续往下走到 X-User-Id 分支 ——
            #    于是「伪造任意 token + 真实 user_id」即可冒充登录。
            #    现在的语义：token 无效 = 认证失败完整性，不再给第二条路。
            _spoof_guard(request, "session_token_invalid_or_expired")
            if enforce:
                raise HTTPException(status_code=401, detail="会话无效或已过期，请重新登录")
            return None

    # ② 弱凭据兜底（仅兼容期）
    uid = request.headers.get("X-User-Id")
    if uid and trust_uid:
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            try:
                row = conn.execute(_USER_SQL, (uid,)).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row:
                if enforce:
                    # 强制态下即便 trust 开着，也不得用自报 id 通过任何 RBAC 端点
                    raise HTTPException(status_code=401, detail="请使用会话令牌登录（X-User-Id 不可用于正式环境）")
                return _user_payload(row)
    elif uid and not trust_uid:
        _spoof_guard(request, "user_id_header_not_trusted", f"uid={str(uid)[:32]}")

    # ③ 无身份
    if enforce:
        raise HTTPException(status_code=401, detail="未登录（auth.enforce_login 已开启）")
    return None


def require_permission(domain: str, op: str):
    """权限依赖工厂：要求当前用户拥有 domain:op 权限（角色权限矩阵）。

    - 匿名请求 → 见下方说明；
    - permissions 中 admin 域列表非空 = 超级用户 → 放行；
    - 否则 domain 存在且 op ∈ permissions[domain] → 放行；其余 → 403。
    返回值仍为 current_user 用户信息，端点可继续用作审计归属。
    """
    def checker(user=Depends(current_user)):
        if user is None:
            # P0-5/D3：匿名放行**只在非强制态**成立。
            # enforce_login=True 时 current_user 已先抛 401 ⇒ 本分支不可达，
            # RBAC 因此自动闭合 —— 不需要第二个开关（两个开关的语义漂移比漏洞更难查）。
            _mut_anon_passthrough = _MUTATION_GUARD_ANON_PASSTHROUGH
            if _mut_anon_passthrough:
                return user
            raise HTTPException(status_code=401, detail="未登录")
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


# ══════════════════════════════════════════════════════════════════════════
# P0-6（2026-10-06）：Agent 工具链的权限判定内核
#
# 为什么要单独抽出来（对标《Agent 生产化Harness 对照核查》P0-1）：
#   实测 `grep require_permission` → routers/ 51 处、**agent/ 0 处**。
#   即「HTTP 端点有 RBAC，Agent 调工具这条链完全没有」——
#   用户提一句话 → LLM 选工具 → `_exec_tool_call()` 直接执行，无任何权限判定。
#   25 个 active read 类工具（file_read / graph_retrieve / sys_query_users …）
#   全部无门，这正是文章讲的「实习生用 Agent 读了他无权查看的文档」那个形态。
#
# 为什么不复用 require_permission 本身：它是 **FastAPI 依赖工厂**
# （内含 `Depends(current_user)`，只能挂在路由签名上），
# 而工具链需要的是**普通函数**（user 由业务侧从请求上下文拿）。
# 所以这里把它的**判定语义原样抽成纯函数**，两边共用同一份规则——
# 避免「HTTP 一套、Agent 另一套」这种同族漂移（本工程已吃过多次）。
# ══════════════════════════════════════════════════════════════════════════

#: 工具链权限矩阵的两个操作位（**op 名不含 domain 前缀** —— `roles.permissions`
#: 的结构是 `{domain: [op, ...]`，domain 是 key、op 是列表项；
#: 早期版本把常量写成 "agent_tool:read" 并整体当 domain 传，
#: 导致 `perms.get("agent_tool:read")` 永远查不到 ⇒ **所有人被拒**（2026-10-06 实测踩到）。
#: - read  —— 读图谱/文件/用户等。缺它 = 越权读（文章那个实习生案例）
#: - write —— 写实体/建概念等。写类另有 HIL 人工确认 + destructive 门兜底，
#:          此处是**第二道锁**（纵深防御：HIL 是流程闸，本项是权限闸）。
TOOL_PERM_DOMAIN = "agent_tool"
TOOL_PERM_READ = "read"
TOOL_PERM_WRITE = "write"

#: 匿名放行的开关语义与 `require_permission` 保持一致：
#: enforce_login=True 时无身份 ⇒ 当前用户在调用侧就已被判 401，
#: 所以本函数的 `user is None` 分支在强制态下不可达 ⇒ RBAC 自动闭合。
def _tool_auth_cfg() -> dict:
    return _auth_cfg()


def has_perm(user, domain: str, op: str) -> bool:
    """判定单个 (domain, op) 权限。**user=None（匿名）时恒为False**。

    规则与 `require_permission` 的 checker 严格一致：
      ① 匿名：仅非强制态放行（强制态下调用方已被 401，这里自然 False）；
      ② `permissions["admin"]` 非空 ⇒ 超级用户放行
         （**空列表不算**——种子数据里所有角色都带 "admin" 键，
           空列表若被当管理员放行，设计师/知识工程师会全部变成超级用户）；
      ③ `domain` 存在且 `op ∈ permissions[domain]` ⇒ 放行。
    """
    cfg = _tool_auth_cfg()
    if user is None:
        return not bool(cfg.get("enforce_login"))
    perms = user.get("permissions") or {}
    if perms.get("admin"):
        return True
    return op in (perms.get(domain) or [])


def tool_side_effect_to_perm(side_effect: str) -> str:
    """工具副作用 → 所需权限位。未知副作用按最严处理（当write）。

    ⚠️ 这里刻意**不把未知当 read**（`_tool_side_effect()` 的查询失败兜底也是 "read"，
    但那是有意的可用性兜底；这里是权限侧，宁可多问一句）。
    与 `tools.side_effect` 的既有取值对齐：read / write / destructive。
    """
    se = (side_effect or "").strip().lower()
    if se == "read":
        return TOOL_PERM_READ
    return TOOL_PERM_WRITE        # write / destructive / 空 / 未知 → 一律按写处理


def check_tool_perm(user, tool_name: str, side_effect: str) -> tuple:
    """Agent 工具链权限闸。返回 `(allowed: bool, reason: str)`。

    - allowed=True  →放行，reason 为空串；
    - allowed=False → 必须拒绝，reason 是**给用户看的中文原因**，
      调用方应把它回喂给 LLM，让模型如实转述（文章原话：
      「Agent 只能老老实实告诉员工你没有权限看这份文档」）。

    ⚠️ **绝不抛异常**：Agent 工具链在 ReAct 循环内，抛异常会打断整个编排。
    拒绝走结构化返回值，这是与 HTTP 层（抛 403）的关键差异。
    """
    need = tool_side_effect_to_perm(side_effect)
    if has_perm(user, TOOL_PERM_DOMAIN, need):
        return True, ""
    return False, (
        f"权限不足：当前用户没有「{TOOL_PERM_DOMAIN}:{need}」权限，无法执行工具「{tool_name}」。"
        f"请提示用户联系管理员为其角色授权 {TOOL_PERM_DOMAIN}:{need} 权限，"
        f"或改用其他方案。（权限必须在工具边界强制执行，不能由模型自行判断）"
    )


def require_any_permission(pairs):
    """权限依赖工厂：任一 (domain, op) 满足即放行（如写操作双角色兼容）。

    admin 域权限列表非空视为超级用户放行；全部不满足 → 403。
    匿名行为与 require_permission 保持一致（非强制态放行，强制态不可达）。
    pairs: [(domain1, op1), (domain2, op2), ...]
    """
    def checker(user=Depends(current_user)):
        if user is None:
            _mut_anon_passthrough = _MUTATION_GUARD_ANON_PASSTHROUGH
            if _mut_anon_passthrough:
                return user
            raise HTTPException(status_code=401, detail="未登录")
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
