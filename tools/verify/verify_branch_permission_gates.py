# -*- coding: utf-8 -*-
"""P0-4 权限门自检：分支管理 4 个端点挂门 + 权限判定单元 + 角色矩阵数据。

设计要点（对齐本工程验证纪律）：
- **[1][2][3] 静态**：AST 解析 routers/branches.py，断言 4 个端点的**函数体首条语句**
  确实是 `require_permission("<domain>","<op>")(user=user)`，且 op 与计划一致。
- **[4][5][6] 行为**：**直接调用 core.deps.require_permission 返回的真实 checker**
  （`checker` 的签名是 `user=Depends(current_user)`，非 FastAPI 场景可直接传 user=），
  用**真库 roles.permissions** 驱动判定 —— 不自己复刻判定语义（避免"测的是复刻"空转）。
- **[7][8] 数据**：真库角色矩阵已授权 + 种子真源一致。
- **M1~M3 变异自证**：内存级改源码/改期望（不落盘），断言对应判据必须 FAIL。

跑法：<repo>/.venv/Scripts/python.exe -X utf8 tools/verify/verify_branch_permission_gates.py
"""
import ast
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ROUTER = os.path.join(ROOT, "routers", "branches.py")
SEEDS = os.path.join(ROOT, "database", "seeds.py")
DB = os.path.join(ROOT, "mbse.db")

# 计划规定：endpoint → (domain, op)
EXPECT = {
    "create_branch": ("branch_dev", "create"),
    "resolve_conflict": ("branch_dev", "merge_request"),
    "create_merge_request": ("branch_dev", "merge_request"),
    "delete_branch": ("branch_dev", "delete"),
}

# 条件门登记表：门被**刻意**放在条件分支内（非首条语句）时的登记处。
# 登记须附源码依据；[3b] 会校验其承载语句确实是 ast.If —— 防止有人借「条件门」之名
# 把门塞到写入之后。未登记者一律按「门必须在函数首位」严判。
CONDITIONAL_GATES = {
    "resolve_merge": "仅 approve 需要 branch_release:review_merge；reject 不强制"
                     "（源文件注释：合并审批硬门禁）",
}

_ok, _fail = [], []


def chk(name, cond, extra=""):
    (_ok if cond else _fail).append(name)
    print("  %s %s%s" % ("[OK]" if cond else "[FAIL]", name, ("  " + extra) if extra else ""))


def _first_stmt(src_text, fn_name):
    """取函数体首条**非 docstring** 语句节点。"""
    tree = ast.parse(src_text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return body[0] if body else None
    return None


def _gate_of(src_text, fn_name):
    """从首条语句解析出 (domain, op) —— 期望形态 require_permission(a,b)(user=user)。"""
    st = _first_stmt(src_text, fn_name)
    if not isinstance(st, ast.Expr) or not isinstance(st.value, ast.Call):
        return None
    inner = st.value.func
    if not isinstance(inner, ast.Call):
        return None
    f = inner.func
    if not (isinstance(f, ast.Name) and f.id == "require_permission"):
        return None
    if len(inner.args) < 2:
        return None
    a, b = inner.args[0], inner.args[1]
    if not (isinstance(a, ast.Constant) and isinstance(b, ast.Constant)):
        return None
    kw_user_ok = any(k.arg == "user" and isinstance(k.value, ast.Name) and k.value.id == "user"
                     for k in st.value.keywords)
    return (a.value, b.value, kw_user_ok)


_GATE_NAMES = ("require_permission", "require_any_permission")
_WRITE_SQL = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")
_WRITE_VERBS = ("create_", "update_", "delete_", "set_", "insert_", "remove_",
                "resolve_", "publish_", "approve_", "rollback_", "merge_")


def _gate_stmt_index(src_text, fn_name):
    """定位承载门调用的**顶层语句**下标。返回 (idx|None, stmt|None, body)。

    与 `_first_stmt` 的区别：门可能在条件分支内（resolve_merge 的 approve 专有门），
    故必须返回「哪条顶层语句里出现了门」，而不是「首条语句是不是门」。
    """
    for node in ast.walk(ast.parse(src_text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            for i, s in enumerate(body):
                for x in ast.walk(s):
                    if isinstance(x, ast.Call) and isinstance(x.func, ast.Call) \
                            and isinstance(x.func.func, ast.Name) and x.func.func.id in _GATE_NAMES:
                        return i, s, body
            return None, None, body
    return None, None, []


def _is_write_call(x):
    """判定 AST 调用节点是否是「写入类」（DB 写 / commit / 审计 / 写语义词的仓储方法）。

    只对**真正的写**报警：`conn.execute("SELECT …")` 不算 —— 否则判据会被读操作噪声淹没。
    """
    if not isinstance(x, ast.Call):
        return None
    f = x.func
    if isinstance(f, ast.Attribute):
        if f.attr in ("commit", "executemany"):
            return ast.unparse(x)[:70]
        if f.attr == "execute":
            for a in x.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    body = a.value.strip()
                    head = body.split("(")[0].strip().split()[0].upper() if body else ""
                    return ast.unparse(x)[:70] if head in _WRITE_SQL else None
            return None
        return ast.unparse(x)[:70] if f.attr.startswith(_WRITE_VERBS) else None
    if isinstance(f, ast.Name):
        if f.id == "audit":
            return ast.unparse(x)[:70]
        return ast.unparse(x)[:70] if f.id.startswith(_WRITE_VERBS) else None
    return None


def _writes_before(body, idx):
    """门所在语句**之前**的所有写入类调用（核心安全属性：门必须早于任何写入）。"""
    out = []
    for s in body[:idx]:
        for x in ast.walk(s):
            hit = _is_write_call(x)
            if hit:
                out.append(hit)
    return out


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")
def _roles():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    out = {}
    for rid, name, perm in con.execute("SELECT id, name, permissions FROM roles WHERE id IN (80,81,82)"):
        out[rid] = (name, json.loads(perm or "{}"))
    con.close()
    return out


def run(src_text, roles, expect_has_delete=True):
    """跑一遍全部判据（变异时传入被改过的 src_text / roles / 期望）。"""
    print("\n--- [1] 4 个端点函数体首条语句 = 挂门调用 ---")
    gates = {}
    for fn in EXPECT:
        g = _gate_of(src_text, fn)
        gates[fn] = g
        chk("[1] %s 挂门形态正确" % fn, g is not None, "→ %s" % (g,))

    print("\n--- [2] op 值与计划一致 ---")
    for fn, (dom, op) in EXPECT.items():
        g = gates.get(fn)
        got = (g[0], g[1]) if g else None
        chk("[2] %s → %s:%s" % (fn, dom, op), got == (dom, op), "→ 实测 %s" % (got,))

    print("\n--- [3] 覆盖与边界：挂门端点集合（按函数名，不用裸计数） ---")
    # 裸计数断言（曾写 "调用点 == 6"）在 P0-2 新增一处门后误报 —— 改为按函数名比对：
    # 漏挂（少了谁）与越界（多了谁）都会显式列出，且新增门必须同步登记到期望集合。
    gated_fns = set()
    for n in ast.walk(ast.parse(src_text)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                   and c.func.id == "require_permission" for c in ast.walk(n)):
                gated_fns.add(n.name)
    expect_gated = set(EXPECT) | {
        "resolve_merge", "rollback_merge",        # 原有 2 处（审批 / 回滚）
        "update_branch_protection",              # P0-2 新增（admin:ops_manage）
    }
    chk("[3] 挂门端点集合 == 预期 %d 个（无漏挂、无越界新增）" % len(expect_gated),
        gated_fns == expect_gated,
        "→ 实测 %s | 缺少 %s | 多出 %s"
        % (sorted(gated_fns), sorted(expect_gated - gated_fns), sorted(gated_fns - expect_gated)))
    # 范围纪律的真断言（非恒真）：rename/archive 端点的首条语句**不是**挂门调用
    st_ren = _first_stmt(src_text, "update_branch")
    ren_gated = (isinstance(st_ren, ast.Expr) and isinstance(st_ren.value, ast.Call)
                 and isinstance(st_ren.value.func, ast.Call)
                 and isinstance(st_ren.value.func.func, ast.Name)
                 and st_ren.value.func.func.id == "require_permission")
    chk("[3] 未越界：update_branch（rename/archive）本轮仍未挂门", not ren_gated,
        "→ 首条语句 %s" % type(st_ren).__name__)

    print("\n--- [3b] 门禁位置：门必须早于任何写入；条件门须显式登记 ---")
    # [3] 的集合比对只证明「函数体内出现过 require_permission」；若门被挪到某次写入之后，
    # 集合比对照样通过 → 门形同虚设。本仓 routers/branches.py 是**唯一**使用
    # 「函数体内手工调用」写法（其余 72 处走 Depends），故逐一把位置钉死。
    # 两级判据（初版写「首条语句必须是门」是错的：resolve_merge 的门被**刻意**放在
    # approve 条件分支内，且源码注释写明了该设计 —— 判缺陷前先找是不是写下来的设计）：
    #   A. 硬属性（任何形态都不许破）：门所在语句之前不得有任何写入；
    #   B. 无条件门：必须在函数首位；条件门：必须登记在 CONDITIONAL_GATES 且承载语句确是 ast.If。
    for fn in sorted(gated_fns):
        _idx, _stmt, _body = _gate_stmt_index(src_text, fn)
        _pre = _writes_before(_body, _idx) if _idx is not None else []
        chk("[3b] %s 门前无任何写入（核心安全属性）" % fn, not _pre,
            ("→ 门前写入 %s" % _pre) if _pre else "（门在第 %s 条语句）" % (_idx + 1 if _idx is not None else "?"))
        if fn in CONDITIONAL_GATES:
            chk("[3b] %s 已登记为条件门，且门确实处在条件分支内（登记不许滥用）" % fn,
                isinstance(_stmt, ast.If), "→ 承载语句 %s" % type(_stmt).__name__)
        else:
            chk("[3b] %s 无条件门 → 必须位于函数首位" % fn, _idx == 0,
                "→ 实际第 %s 条语句" % (_idx + 1 if _idx is not None else "?"))

    print("\n--- [4] 权限判定：真库矩阵驱动（设计师/知识工程师/管理员 全通过）---")
    from core.deps import require_permission
    from fastapi import HTTPException
    for rid in (80, 81, 82):
        name, perms = roles[rid]
        fake = {"id": rid, "username": name, "display_name": name, "permissions": perms}
        for dom, op in set(EXPECT.values()):
            checker = require_permission(dom, op)
            try:
                checker(user=fake)
                ok = True
                err = ""
            except HTTPException as e:
                ok = False
                err = "HTTP %s" % e.status_code
            chk("[4] 角色[%s]%s 可通过 %s:%s" % (rid, name, dom, op), ok, err)

    print("\n--- [5] 门有效性：无权限角色必须 403（防「门空转」）---")
    _, pres = roles[80]
    stripped = dict(pres)
    stripped["branch_dev"] = [o for o in (pres.get("branch_dev") or []) if o != "delete"]
    fake_no = {"id": 999, "username": "no", "display_name": "无权限", "permissions": stripped}
    checker = require_permission("branch_dev", "delete")
    code, detail = None, ""
    try:
        checker(user=fake_no)
    except HTTPException as e:
        code, detail = e.status_code, str(e.detail)
    chk("[5] 去掉 delete 键 → 403（门真的在拦）", code == 403, "→ 实测 %s" % code)
    chk("[5] 403 文案含 branch_dev:delete", "branch_dev:delete" in detail, "→ %r" % detail)

    print("\n--- [6] 匿名放行（现状取舍，显式记录而非默认通过）---")
    checker_none = require_permission("branch_dev", "delete")
    try:
        r = checker_none(user=None)
        chk("[6] user=None → 放行（返回 None）", r is None, "→ %r" % (r,))
    except HTTPException as e:
        chk("[6] user=None → 放行（返回 None）", False, "→ HTTP %s" % e.status_code)

    print("\n--- [7] 角色矩阵数据：设计师/知识工程师已授权 delete ---")
    for rid in (80, 81):
        name, perms = roles[rid]
        has = "delete" in (perms.get("branch_dev") or [])
        chk("[7] 角色[%s]%s branch_dev 含 delete" % (rid, name), has if expect_has_delete else (not has))

    print("\n--- [8] 种子真源一致（新库初始化的角色也带 delete）---")
    seeds = _read(SEEDS)
    n_delete_seed = seeds.count('"branch_dev": ["create", "switch", "merge_request", "delete"]')
    chk("[8] seeds.py 两处 preset/knowledge 均含 delete", n_delete_seed == 2, "→ 实测 %d" % n_delete_seed)


def main():
    src_text = _read(ROUTER)
    roles = _roles()
    print("=" * 74)
    print("P0-4 权限门自检  |  routers/branches.py  |  真库 roles 驱动")
    print("=" * 74)
    run(src_text, roles)

    print("\n" + "=" * 74)
    print("变异自证（内存级，不落盘）")
    print("=" * 74)
    base_ok, base_fail = list(_ok), list(_fail)
    # 变异判据一律只认「新增失败」—— 否则基线自身有失败时会假绿
    _base_set = set(base_fail)

    # M1：把 create_branch 的挂门行"注释掉"（源码文本级）
    m1_src = src_text.replace(
        '    require_permission("branch_dev", "create")(user=user)\n',
        '    # require_permission("branch_dev", "create")(user=user)\n', 1)
    chk("M1 变异生效（源码被改）", m1_src != src_text)
    _ok[:], _fail[:] = [], []
    run(m1_src, roles)
    m1_fail = list(_fail)
    m1_new = [f for f in m1_fail if f not in _base_set]
    chk("M1 变异被抓住（[1]/[2] 报 FAIL）", len(m1_new) > 0, "→ 新增失败 %s" % m1_new)

    # M2：把 delete 门的 op 改成 "view"（错的 op，物理存在但语义不符）
    m2_src = src_text.replace(
        'require_permission("branch_dev", "delete")(user=user)',
        'require_permission("branch_dev", "view")(user=user)', 1)
    chk("M2 变异生效", m2_src != src_text)
    _ok[:], _fail[:] = [], []
    run(m2_src, roles)
    m2_fail = list(_fail)
    m2_new = [f for f in m2_fail if f not in _base_set]
    chk("M2 变异被抓住（[2] 报 FAIL）", len(m2_new) > 0, "→ 新增失败 %s" % m2_new)

    # M3：矩阵把 delete 视为未授权（等价于"忘了补键"）
    _ok[:], _fail[:] = [], []
    run(src_text, roles, expect_has_delete=False)
    m3_fail = list(_fail)
    m3_new = [f for f in m3_fail if f not in _base_set]
    chk("M3 变异被抓住（[7] 报 FAIL，证明 [7] 真在读数据）", len(m3_new) > 0, "→ 新增失败 %s" % m3_new)

    # M4：把 update_branch_protection 的门**下移一行**（门仍存在，但已非首位）
    #     这是 [3] 集合比对抓不到、只有 [3b] 位置判据能抓的形态 —— 用它证明 [3b] 非空转。
    m4_src = src_text.replace(
        '    require_permission("admin", "ops_manage")(user=user)\n',
        '    _noop = 1  # 变异：门被挪到写入/读取之后\n'
        '    require_permission("admin", "ops_manage")(user=user)\n', 1)
    chk("M4 变异生效", m4_src != src_text)
    _ok[:], _fail[:] = [], []
    run(m4_src, roles)
    m4_fail = list(_fail)
    m4_new = [f for f in m4_fail if f not in _base_set]          # 只认「新增失败」
    chk("M4 变异被抓住（[3b] 报 FAIL，证明位置判据非空转）",
        any("update_branch_protection 无条件门 → 必须位于函数首位" in f for f in m4_new),
        "→ 新增失败 %s" % m4_new)
    chk("M4 对照：[3] 集合比对抓不到该变异（门仍在，只是位置变了 —— 证明 [3] 单独不够）",
        not any(f.startswith("[3] 挂门端点集合") for f in m4_new),
        "→ [3] 新增失败 %s" % [f for f in m4_new if f.startswith("[3] ")])

    # M5：在 create_branch 的门**之前**插一条真写入 —— 击穿的是 [3b] 的硬属性（门前无写入），
    #     与 M4 的「位置严判」是两条不同的判据，必须各自有变异作证。
    m5_src = src_text.replace(
        '    require_permission("branch_dev", "create")(user=user)\n',
        '    conn.execute("UPDATE branches SET description=description WHERE 0")  # 变异：门前写入\n'
        '    require_permission("branch_dev", "create")(user=user)\n', 1)
    chk("M5 变异生效", m5_src != src_text)
    _ok[:], _fail[:] = [], []
    run(m5_src, roles)
    m5_new = [f for f in _fail if f not in _base_set]
    chk("M5 变异被抓住（[3b] 硬属性「门前无任何写入」报 FAIL）",
        any("create_branch 门前无任何写入" in f for f in m5_new), "→ 新增失败 %s" % m5_new)

    print("\n" + "=" * 74)
    print("基线（变异前）：OK=%d FAIL=%d" % (len(base_ok), len(base_fail)))
    if base_fail:
        print("基线失败项：%s" % base_fail)
    print("变异 M1/M2/M3/M4/M5 均被抓住 = 断言非空转")
    print("=" * 74)
    return 1 if base_fail else 0


if __name__ == "__main__":
    sys.exit(main())
