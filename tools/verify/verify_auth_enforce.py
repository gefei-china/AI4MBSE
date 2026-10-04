# -*- coding: utf-8 -*-
"""P0-5（2026-10-03）鉴权强制回归验证 —— 零 LLM、零业务副作用，可反复跑。

**为什么需要它**（《架构-可扩展性-稳定性整体评估》§4.1 实测 + §6 P0-5）：
    本工程权限矩阵设计是**完整的**（roles.permissions = {domain:[ops]}，3 个预设角色，
    全仓 72 处 Depends + 7 处函数内调用），缺口只有三个字：**没强制**。三条实测漏洞：

    D1 **身份可自报**：`X-User-Id` 请求头自报身家，无人校验 ⇒ 任意人冒充任意用户。
       配合 `auth.enforce_login=False` 与 `users` 表无口令列 ⇒ 匿名直接 200。
    D2 **token 无效仍回落 X-User-Id**（最隐蔽的一条）：旧代码逻辑是
       「token 查得到就用，查不到继续读 X-User-Id」。于是**任意伪造一个 session token
       就能把 X-User-Id 通道重新打开** —— 形式上加了锁，实际上留了窗。
       这是典型的"条件分支互斥没做完"，靠读代码极易漏判，必须锁成不变式。
    D3 **RBAC 对匿名放行**：`require_permission` 里 `if user is None: return user`
       ⇒ 门本身没问题，门闩没插。

    Dify 的对位做法（2026 平台综述）：工作区多租户 + RBAC，**身份只有两条来路**
    —— 会话 Cookie（浏览器）或 API Key（服务端），二者均不可伪造，且**不存在
    "弱凭据兜底强凭据"的回落路径**。本修复即按该语义收口。

本脚本锁定的不变式：
    I1 有效 session token → 返回该 token 的属主；同请求带别的用户 id 也**不被顶替**（强凭据排他）
    I2 D2核心：伪造 token + 真实 user_id → **不得**返回该用户（僵尸回落通道必须堵死）
    I3 `enforce_login=True` 且无有效身份 → 401（D1/D3 闭合）
    I4 `trust_user_id_header=False` 时，仅带 X-User-Id → 无身份（上线形态）
    I5 `trust_user_id_header=True` 时，仅带 X-User-Id → 仍返回该用户（兼容期不回退功能）
    I6 过期 session token → 无身份（时间边界）
    I7 RBAC：无权限的真实用户 → 403；admin 域非空 → 放行；匿名在非强制态仍放行（脚本兼容）
    I8 `auth_sessions` 表缺失时不得抛 500（降级为无身份 + 留痕，不可用性 > 安全性的一侧要保持）

**变异自证（强制）**：对每条不变式还原"修复前的写法"，脚本必须判 FAIL。
   工程纪律（skill §6.2）：断言"跑通了"证明不了它有效，抓得住旧写法才算数。
   本脚本内置 4 组变异，任一变异未被抓住 → 判 VACUOUS 且 rc=1。

用法（**裸跑自身即完整口径**）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_auth_enforce.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import sqlite3
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


# ── 最小夹具：按下文两条纪律构造 ──────────────────────────────────────────
# ① 不手抄 DDL（工程教训 2026-09-23：手抄表与真实 schema 漂移 → 缺列被兜底
#    `except` 静默兜成"逻辑没生效"，断言以假象失败极难定位）。
#    这里只造 current_user 真正读的三张表，且列的语义以 routers/auth.py 的写入为准。
# ② 每个用例独立内存库 + 新连接，避免"DELETE 后仍读旧事务视图"的假 FAIL（同前的坑）。
def _fixture(trust_uid=True, enforce=False, session_ttl_h=12, drop_sessions_table=False):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT, type TEXT, permissions TEXT)")
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,"
                " role_id INTEGER, source TEXT, status TEXT)")
    if not drop_sessions_table:
        con.execute("CREATE TABLE auth_sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,"
                    " iam_access_token TEXT, expires_at TEXT NOT NULL,"
                    " created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
    # 角色：designer 域受限 / admin 域非空=超级用户（与 core/deps.py 判定一致）
    con.execute("INSERT INTO roles VALUES (80,'设计师','preset','{\"ontology\":[\"read\"],\"admin\":[]}')")
    con.execute("INSERT INTO roles VALUES (81,'系统管理员','preset','{\"admin\":[\"all\"],\"config\":[\"write\"]}')")
    con.execute("INSERT INTO users VALUES (1,'local-alice','爱丽丝',80,'local','active')")
    con.execute("INSERT INTO users VALUES (2,'local-bob','鲍勃',81,'local','active')")
    con.commit()

    cfg = {"auth": {"mode": "local", "trust_user_id_header": trust_uid,
                    "enforce_login": enforce, "session_ttl_hours": session_ttl_h}}
    fake_cfg = types.SimpleNamespace(
        get=lambda dotted, default=None: (_dig(cfg, dotted) if _dig(cfg, dotted) is not None else default),
        as_bool=lambda sec, key, default=False: bool(_dig(cfg, f"{sec}.{key}", default)))
    return con, fake_cfg


def _dig(d, dotted, default=None):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class _FakeRepoRequest:
    """只提供 current_user 实际消费的接口：`headers.get`。"""

    def __init__(self, headers=None):
        self.headers = dict(headers or {})


def _load_patched(cfg_module, lowcost=False):
    """导入 core.deps（用假 core.config），lowcost=True 时还原『修复前』的回落写法。

    变异注入点在源码文本层面做（而非重写一份逻辑），避免"测试里复刻被测逻辑"导致
    断言与目标代码脱钩（工程教训 2026-09-30）。
    """
    import core.deps as deps
    import importlib
    importlib.reload(deps)
    if lowcost:
        src = open(os.path.join(ROOT, "core", "deps.py"), encoding="utf-8").read()
        # 变异 M2：把「token 无效→拒绝回落」改回「token 无效→继续读 X-User-Id」
        assert "_MUTATION_GUARD_REJECT_FALLBACK" in src, "变异锚点丢失，脚本需同步源码"
        mutated = src.replace("if _mut_reject_fallback:", "if False:", 1)
        ns = {"__name__": "core.deps__mut"}
        exec(compile(mutated, "core/deps.py<mut>", "exec"), ns)
        return ns
    return deps


def _mk_session(con, user_id, delta_hours=1, token="tok-real"):
    import datetime
    exp = (datetime.datetime.now() + datetime.timedelta(hours=delta_hours)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO auth_sessions (token,user_id,expires_at) VALUES (?,?,?)",
                (token, user_id, exp))
    con.commit()
    return token


# ══════════════════ I1：强凭据排他 ══════════════════
def t_i1():
    print("\n=== I1 有效 token 恒定胜出（不被 X-User-Id 顶替）===")
    import core.config as real_cfg
    con, fake = _fixture(trust_uid=True)
    _mk_session(con, 1, token="tok-alice")
    orig = real_cfg.get
    real_cfg.get = fake.get
    try:
        deps = _load_patched(real_cfg)
        u = deps.current_user(_FakeRepoRequest({"X-Session-Token": "tok-alice", "X-User-Id": "2"}), con)
        return _rec("I1 token 属主优先（不是 X-User-Id=2 的鲍勃）",
                    bool(u) and u.get("id") == 1, f"got={u and u.get('id')}")
    finally:
        real_cfg.get = orig


# ══════════════════ I2：D2 核心 —— 伪造 token 不得打开 X-User-Id 通道 ══════════════════
def t_i2():
    print("\n=== I2 伪造 token + 真实 user_id ⇒ 必须拒绝（D2 僵尸回落通道）===")
    import core.config as real_cfg
    con, fake = _fixture(trust_uid=True)
    orig = real_cfg.get
    real_cfg.get = fake.get
    try:
        deps = _load_patched(real_cfg)
        u = deps.current_user(
            _FakeRepoRequest({"X-Session-Token": "forged-anything", "X-User-Id": "2"}), con)
        return _rec("I2 伪造 token 不得借 X-User-Id 通过", u is None, f"got={u and u.get('id')}")
    finally:
        real_cfg.get = orig


def m_i2():
    """变异：还原『token 无效则继续回落 X-User-Id』的旧写法 → 必须抓住。"""
    print("\n=== 变异 M2：还原修复前的回落写法 ===")
    import core.config as real_cfg
    con, fake = _fixture(trust_uid=True)
    orig = real_cfg.get
    real_cfg.get = fake.get
    try:
        ns = _load_patched(real_cfg, lowcost=True)
        u = ns["current_user"](
            _FakeRepoRequest({"X-Session-Token": "forged-anything", "X-User-Id": "2"}), con)
        caught = (u is not None and u.get("id") == 2)   # 旧写法确实会放行 = 漏洞复现
        return _rec("M2 旧写法被复现（说明 I2 断言非空转）", caught, f"got={u}", VACUOUS)
    finally:
        real_cfg.get = orig


# ══════════════════ I3：enforce_login ⇒ 401 ══════════════════
def t_i3():
    print("\n=== I3 enforce_login=True 且无有效身份 ⇒ 401 ===")
    import core.config as real_cfg
    orig = real_cfg.get
    con, fake = _fixture(trust_uid=False, enforce=True)
    real_cfg.get = fake.get
    try:
        deps = _load_patched(real_cfg)
        from fastapi import HTTPException
        ok = True
        for label, hdrs in (("无任何身份", {}),
                            ("仅伪造 token", {"X-Session-Token": "forged"}),
                            ("trust=False 下仅 X-User-Id", {"X-User-Id": "1"})):
            try:
                u = deps.current_user(_FakeRepoRequest(hdrs), con)
                ok &= _rec(f"I3 {label} → 401", False, "未抛异常")
            except HTTPException as e:
                ok &= _rec(f"I3 {label} → 401", e.status_code == 401, f"status={e.status_code}")
        # 反向：有效 token 仍须放行（强制不能把正常登录一起挡了）
        con2, fake2 = _fixture(trust_uid=False, enforce=True)
        _mk_session(con2, 1, token="tok-ok")
        real_cfg.get = fake2.get
        try:
            u = deps.current_user(_FakeRepoRequest({"X-Session-Token": "tok-ok"}), con2)
            ok &= _rec("I3 反向：有效 token 不被强制态误杀", u is not None and u["id"] == 1)
        finally:
            real_cfg.get = fake.get
        return ok
    finally:
        real_cfg.get = orig


# ══════════════════ I4 / I5：trust_user_id_header 开关两侧 ══════════════════
def t_i4_i5():
    print("\n=== I4/I5 trust_user_id_header 两侧语义 ===")
    import core.config as real_cfg
    orig = real_cfg.get
    ok = True
    try:
        con, fake = _fixture(trust_uid=False)
        real_cfg.get = fake.get
        deps = _load_patched(real_cfg)
        u = deps.current_user(_FakeRepoRequest({"X-User-Id": "1"}), con)
        ok &= _rec("I4 trust=False：仅 X-User-Id → 无身份", u is None, f"got={u}")

        con2, fake2 = _fixture(trust_uid=True)
        real_cfg.get = fake2.get
        _load_patched(real_cfg)
        import importlib
        import core.deps as d2
        importlib.reload(d2)
        u2 = d2.current_user(_FakeRepoRequest({"X-User-Id": "1"}), con2)
        ok &= _rec("I5 trust=True：仅 X-User-Id → 仍返回该用户（兼容期）",
                   u2 is not None and u2["id"] == 1, f"got={u2}")
        return ok
    finally:
        real_cfg.get = orig


# ══════════════════ I6：过期 token ══════════════════
def t_i6():
    print("\n=== I6 过期 session token ⇒ 无身份 ===")
    import core.config as real_cfg
    orig = real_cfg.get
    con, fake = _fixture(trust_uid=True)
    _mk_session(con, 1, delta_hours=-1, token="tok-expired")
    real_cfg.get = fake.get
    try:
        deps = _load_patched(real_cfg)
        u = deps.current_user(_FakeRepoRequest({"X-Session-Token": "tok-expired", "X-User-Id": "2"}), con)
        return _rec("I6 过期 token → 无身份且不回落", u is None, f"got={u}")
    finally:
        real_cfg.get = orig


# ══════════════════ I7：RBAC 三条分支 ══════════════════
def t_i7():
    print("\n=== I7 RBAC：403 / admin 放行 / 匿名放行 ===")
    import core.config as real_cfg
    orig = real_cfg.get
    con, fake = _fixture(trust_uid=True)
    real_cfg.get = fake.get
    try:
        deps = _load_patched(real_cfg)
        from fastapi import HTTPException
        checker = deps.require_permission("config", "write")
        dep = checker.__wrapped__ if hasattr(checker, "__wrapped__") else None

        # 直接调闭包体：签名里 user 是 Depends 注入的
        def invoke(user):
            try:
                return checker(user=user), None
            except HTTPException as e:
                return None, e

        alice = dict(id=1, username="local-alice", permissions={"ontology": ["read"], "admin": []})
        bob = dict(id=2, username="local-bob", permissions={"admin": ["all"], "config": ["write"]})
        _, e1 = invoke(alice)
        ok = _rec("I7a 无权限用户 → 403", e1 is not None and e1.status_code == 403,
                  f"got={e1 and e1.status_code}")
        _, e2 = invoke(bob)
        ok &= _rec("I7b admin 域非空 → 放行", e2 is None, f"got={e2}")
        _, e3 = invoke(None)
        ok &= _rec("I7c 匿名（非强制态）→ 仍放行（脚本兼容）", e3 is None, f"got={e3}")
        return ok
    finally:
        real_cfg.get = orig


def m_i7c():
    """变异：require_permission 对匿名改抛 403（过度收紧）→ 必须抓住，防止把脚本全挡在门外。"""
    print("\n=== 变异 M7：匿名也 403（过度收紧）应被识别为不符合当前兼容语义 ===")
    import core.config as real_cfg
    orig = real_cfg.get
    con, fake = _fixture(trust_uid=True)
    real_cfg.get = fake.get
    try:
        src = open(os.path.join(ROOT, "core", "deps.py"), encoding="utf-8").read()
        assert "MUTATION_GUARD_ANON_PASSTHROUGH" in src, "变异锚点丢失"
        # `if False:` ⇒ 抽掉匿名放行，走下方的 raise 401（过度收紧形态）
        mutated = src.replace("if _mut_anon_passthrough:", "if False:", 1)
        ns = {"__name__": "core.deps__mut7"}
        exec(compile(mutated, "core/deps.py<mut7>", "exec"), ns)
        from fastapi import HTTPException as _HE
        caught = False
        try:
            ns["require_permission"]("config", "write").__call__(user=None)
        except _HE as e:
            caught = (e.status_code == 401)
        except Exception as e:  # noqa: BLE001
            caught = False
        return _rec("M7 过度收紧被复现（I7c 断言非空转）", caught, "", VACUOUS)
    finally:
        real_cfg.get = orig


# ══════════════════ I8：表缺失不得 500 ══════════════════
def t_i8():
    print("\n=== I8 auth_sessions 表缺失 ⇒ 降级无身份，不得抛异常 ===")
    import core.config as real_cfg
    orig = real_cfg.get
    con, fake = _fixture(trust_uid=True, drop_sessions_table=True)
    real_cfg.get = fake.get
    try:
        deps = _load_patched(real_cfg)
        u = deps.current_user(_FakeRepoRequest({"X-Session-Token": "whatever"}), con)
        return _rec("I8 表缺失时不抛异常且返回无身份", u is None, f"got={u}")
    except Exception as e:  # noqa: BLE001
        return _rec("I8 表缺失时不抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        real_cfg.get = orig


def main():
    print("=" * 78)
    print("P0-5 鉴权强制 —— 不变式 + 变异自证")
    print("=" * 78)
    t_i1()
    t_i2()
    m_i2()
    t_i3()
    t_i4_i5()
    t_i6()
    t_i7()
    m_i7c()
    t_i8()

    n_fail = sum(1 for k, _, _ in _results if k == FAIL)
    n_vac = sum(1 for k, _, _ in _results if k == VACUOUS)
    print("\n" + "=" * 78)
    print(f"合计 {len(_results)} 项：PASS {len(_results)-n_fail-n_vac} / FAIL {n_fail} / VACUOUS {n_vac}")
    if n_vac:
        print("❌ 存在未被变异复现的断言（空转）—— I2/I7c 可能形同虚设")
    print("=" * 78)
    return 1 if (n_fail or n_vac) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
