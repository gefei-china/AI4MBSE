# -*- coding: utf-8 -*-
"""P0-2 分支保护规则配置化：自检脚本。

判据分层
  [0] 静态一致性（单一真源 / 三处使用点 / 端点注册顺序 / 迁移装配）
  [1] 规则矩阵（纯函数：类型默认 × 名称兜底 × 分支级覆盖）
  [2] 零回归（内置 release/dev/personal 的既有保护语义不变）
  [3] 分支级覆盖（验收①：解锁后守卫放行）
  [4] fail-safe（规则 JSON 损坏 → 保持保护，不放开）
  [5] 直写门（副本上 check_writable 的放行/拦截）
  [6] 端点端到端（副本：规则配置 200/400/403 + 审计留痕；删分支放行/拒绝）
  [7] 变异自证（M1 去掉类型默认 / M2 去掉名称兜底 / M3 改为 fail-open）

⚠️ 全部写操作只发生在**库副本**（SQLite backup API 保真快照）上，真库零写入。
"""
import json
import os
import sqlite3
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import core.branch_rules as br                                   # noqa: E402
from core.deps import require_permission                          # noqa: E402
from fastapi import HTTPException                                 # noqa: E402
from fastapi.responses import JSONResponse                        # noqa: E402
from repositories.branch_repo import BranchRepo                    # noqa: E402
from routers.branches import (update_branch_protection,           # noqa: E402
                              delete_branch)

REAL = os.path.join(ROOT, "mbse.db")
COPY = os.path.join(ROOT, "tmp", "p02_verify.db")

_ok = []
_fail = []


def chk(name, cond, extra=""):
    (_ok if cond else _fail).append(name)
    print("  [%s] %s%s" % ("OK" if cond else "FAIL", name, ("  " + str(extra)) if extra else ""))


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def snapshot(src, dst):
    """SQLite backup API 保真快照（⚠️ shutil.copy2 会丢 WAL 中未 checkpoint 的改动）。"""
    if os.path.exists(dst):
        os.remove(dst)
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    d.close()
    s.close()


def open_db(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=8000")
    return con


def code_of(res):
    """端点返回 JSONResponse 时取其 status_code + body；其余视为 200/原值。"""
    if isinstance(res, JSONResponse):
        try:
            return res.status_code, json.loads(res.body.decode("utf-8"))
        except Exception:
            return res.status_code, {}
    return 200, res


print("=" * 78)
print("P0-2 分支保护规则配置化 · 自检")
print("=" * 78)

# ══════════════ [0] 静态一致性 ══════════════
print("\n--- [0] 静态一致性 ---")
src_b = read(os.path.join(ROOT, "routers", "branches.py"))
src_s = read(os.path.join(ROOT, "routers", "knowledge_parts", "shared.py"))
src_sch = read(os.path.join(ROOT, "database", "schema.py"))
src_col = read(os.path.join(ROOT, "database", "migrations", "columns.py"))
src_mi = read(os.path.join(ROOT, "database", "migrations", "__init__.py"))
src_repo = read(os.path.join(ROOT, "repositories", "branch_repo.py"))

chk("[0a] branches.py 三处守卫均委托 branch_rules",
    "branch_rules.effective_rules(branch)" in src_b
    and "branch_rules.check_renamable(conn, name)" in src_b
    and "branch_rules.check_deletable(conn, name)" in src_b)
chk("[0b] PROTECTED 硬编码集合已退场（单一真源）",
    "if name in PROTECTED" not in src_b and "PROTECTED = {" not in src_b)
chk("[0c] 保护规则端点注册早于 {name:path} 通配路由（否则被吞）",
    src_b.index('"/api/branches/{name:path}/protection"')
    < src_b.index('@router.put("/api/branches/{name:path}")'))
chk("[0d] 保护规则端点有权限门 admin:ops_manage",
    'require_permission("admin", "ops_manage")(user=user)' in src_b)
chk("[0e] 保护规则变更写审计 branch_protection_update",
    '"branch_protection_update"' in src_b)
chk("[0f] shared._release_guard 已改为读规则",
    "return check_writable(conn, branch)" in src_s)
chk("[0g] schema DDL 含 protection_rules 列",
    "protection_rules TEXT DEFAULT '{}'" in src_sch)
chk("[0h] columns.py 幂等加列 + 回填函数齐备",
    '_add("branches", "protection_rules"' in src_col
    and "def _migrate_branch_protection" in src_col)
chk("[0i] 迁移已导出并在 init_db 调用",
    "_migrate_branch_protection" in src_mi and "_migrate_branch_protection(conn)" in src_sch)
chk("[0j] 仓储写方法 set_protection_rules 存在",
    "def set_protection_rules" in src_repo)

# ══════════════ [1] 规则矩阵（纯函数） ══════════════
print("\n--- [1] 规则矩阵（纯函数，无 DB） ---")
cases = [
    # (名称, 类型, 覆盖 JSON, 期望)
    ("release", "release", None, {"writable": False, "deletable": False, "renamable": False, "required_reviews": 1}),
    ("dev", "dev", None, {"writable": True, "deletable": False, "renamable": False, "required_reviews": 0}),
    ("personal", "personal", None, {"writable": True, "deletable": False, "renamable": False}),
    ("personal/x", "personal", None, {"writable": True, "deletable": True, "renamable": True}),
    ("local-1", "local", None, {"writable": True, "deletable": True, "renamable": True}),
    ("main", "", None, {"writable": True, "deletable": True, "renamable": True}),
]
all_ok = True
for name, btype, ov, exp in cases:
    got = br.effective_rules({"name": name, "branch_type": btype,
                              "protection_rules": ov or ""})
    bad = {k: (exp[k], got[k]) for k in exp if got[k] != exp[k]}
    all_ok = all_ok and not bad
    print("     %-12s type=%-9s → %s %s" % (name, btype or "-",
                                            {k: got[k] for k in exp}, bad or ""))
chk("[1a] 6 组（名称×类型）规则矩阵全部符合预期", all_ok)
chk("[1b] 未登记分支按名称推断：release/x 视为 release 类型",
    br.effective_rules({"name": "release/x", "branch_type": ""})["writable"] is False)

# ══════════════ [2] 零回归 ══════════════
print("\n--- [2] 零回归（既有保护语义不变） ---")
con_real_ro = sqlite3.connect("file:%s?mode=ro" % REAL, uri=True)
con_real_ro.row_factory = sqlite3.Row
rows = {r["name"]: dict(r) for r in con_real_ro.execute("SELECT * FROM branches")}
con_real_ro.close()
rules_real = {n: br.effective_rules(r) for n, r in rows.items()}
chk("[2a] 真库 release：只读 + 不可删 + 不可改名 + 需 1 次评审",
    rules_real.get("release", {}).get("writable") is False
    and rules_real["release"]["deletable"] is False
    and rules_real["release"]["renamable"] is False
    and rules_real["release"]["required_reviews"] == 1)
chk("[2b] 真库 dev：可写但不可删/不可改名",
    rules_real.get("dev", {}).get("writable") is True
    and rules_real["dev"]["deletable"] is False
    and rules_real["dev"]["renamable"] is False)
chk("[2c] 真库 personal（内置）：可写但不可删/不可改名",
    rules_real.get("personal", {}).get("writable") is True
    and rules_real["personal"]["deletable"] is False
    and rules_real["personal"]["renamable"] is False)
chk("[2d] 真库已回填显式规则（3 条均非空）",
    all(str(r.get("protection_rules") or "").strip() not in ("", "{}") for r in rows.values()))

# ══════════════ [3] 分支级覆盖 ══════════════
print("\n--- [3] 分支级覆盖（验收①：解锁后守卫放行） ---")
dev_unlocked = br.effective_rules({"name": "dev", "branch_type": "dev",
                                   "protection_rules": '{"deletable": true}'})
chk("[3a] dev 显式 deletable=true → 生效 deletable 变 True",
    dev_unlocked["deletable"] is True)
chk("[3b] 覆盖只影响指定键（writable 未传 → 保持类型默认 True）",
    dev_unlocked["writable"] is True and dev_unlocked["renamable"] is False)
partial = br.effective_rules({"name": "personal", "branch_type": "personal",
                              "protection_rules": '{"renamable": true}'})
chk("[3c] personal 只解锁 renamable → deletable 仍为 False（内置兜底未被连带放开）",
    partial["renamable"] is True and partial["deletable"] is False)

# ══════════════ [4] fail-safe ══════════════
print("\n--- [4] fail-safe（规则损坏不放开保护） ---")
bad_json = br.effective_rules({"name": "personal", "branch_type": "personal",
                               "protection_rules": "{not a json"})
chk("[4a] JSON 损坏 → 忽略覆盖、保持内置兜底（personal 仍不可删）",
    bad_json["deletable"] is False and bad_json["renamable"] is False)
bad_type = br.effective_rules({"name": "release", "branch_type": "release",
                               "protection_rules": '["not","a","dict"]'})
chk("[4b] 非 dict 结构 → 同样忽略（release 仍只读）",
    bad_type["writable"] is False)
chk("[4c] parse_rules 对空值/异常输入返回 {}",
    br.parse_rules("") == {} and br.parse_rules("{bad") == {} and br.parse_rules(None) == {})

# ══════════════ [5] 直写门 ══════════════
print("\n--- [5] 直写门（副本 check_writable） ---")
snapshot(REAL, COPY)
copy = open_db(COPY)
for nm, want in [("release", False), ("release/x", False), ("dev", True),
                 ("personal", True), ("", True)]:
    msg = br.check_writable(copy, nm)
    if want:
        chk("[5-%s] check_writable(%r) 放行" % (nm or "空", nm), msg is None, msg or "")
    else:
        chk("[5-%s] check_writable(%r) 拦截" % (nm or "空", nm), msg is not None)

# ══════════════ [6] 端点端到端（副本） ══════════════
print("\n--- [6] 端点端到端（副本；真库零写入） ---")
perms = {r["id"]: json.loads(r["permissions"] or "{}")
         for r in copy.execute("SELECT id, permissions FROM roles")}
designer = {"id": 80, "username": "designer", "display_name": "设计师", "permissions": perms.get(80, {})}
admin = {"id": 82, "username": "admin", "display_name": "系统管理员", "permissions": perms.get(82, {})}
chk("[6-pre] 角色矩阵前置：设计师无 ops_manage / 管理员有",
    "ops_manage" not in (designer["permissions"].get("admin") or [])
    and "delete" in (designer["permissions"].get("branch_dev") or [])
    and bool(admin["permissions"].get("admin")))

# 造 3 条一次性分支（无子分支/无实体/无 MR → 可删性只看保护规则）
for nm, btype, rl in [("verify-ok", "personal", ""),
                      ("verify-lock", "personal", '{"deletable": false}'),
                      ("verify-devt", "dev", '{"deletable": true}')]:
    copy.execute("INSERT INTO branches (name, branch_type, parent_branch, status, protection_rules) "
                 "VALUES (?,?,?,?,?)", (nm, btype, "", "active", rl))
copy.commit()

# 6a 规则配置：管理员 200 + 审计
res = update_branch_protection("verify-ok", {"rules": {"required_reviews": 2, "renamable": False}},
                              conn=copy, user=admin)
c, b = code_of(res)
copy.commit()
chk("[6a] 规则配置（管理员）→ 200 且返回生效规则",
    c == 200 and b.get("rules", {}).get("required_reviews") == 2,
    "code=%s rules=%s" % (c, b.get("rules")))
aud = copy.execute("SELECT COUNT(*) FROM audit_logs WHERE event_type='branch_protection_update'"
                   " AND branch='verify-ok'").fetchone()[0]
chk("[6a2] 规则变更写审计（含分支归属）", aud == 1, "audit 行数=%s" % aud)

# 6b 规则配置：非法值/未知键 400
c1, b1 = code_of(update_branch_protection("verify-ok", {"rules": {"required_reviews": -1}},
                                          conn=copy, user=admin))
c2, b2 = code_of(update_branch_protection("verify-ok", {"rules": {"foo": 1}},
                                          conn=copy, user=admin))
c3, b3 = code_of(update_branch_protection("verify-ok", {"rules": {"writable": "yes"}},
                                          conn=copy, user=admin))
chk("[6b] 非法输入三类均 400（负数 / 未知键 / 类型错）",
    c1 == 400 and c2 == 400 and c3 == 400, (c1, c2, c3))

# 6c 规则配置：非管理员 403（设计师 admin 域为空、无 ops_manage）
try:
    update_branch_protection("verify-ok", {"rules": {"deletable": True}}, conn=copy, user=designer)
    c4 = 200
except HTTPException as e:
    c4 = e.status_code
chk("[6c] 规则配置（设计师）→ 403（保护规则属安全配置）", c4 == 403, "code=%s" % c4)

# 6d 规则配置：不存在的分支 404
c5, _ = code_of(update_branch_protection("no-such-branch", {"rules": {"deletable": True}},
                                         conn=copy, user=admin))
chk("[6d] 目标分支不存在 → 404", c5 == 404, "code=%s" % c5)

# 6e 删分支：deletable 默认 true → 放行；显式 false → 拒绝；dev 型解锁 → 放行
c6, b6 = code_of(delete_branch("verify-ok", conn=copy, user=designer))
copy.commit()
left = copy.execute("SELECT COUNT(*) FROM branches WHERE name='verify-ok'").fetchone()[0]
chk("[6e] verify-ok（personal，deletable 默认 true）→ 真删成功",
    c6 == 200 and b6.get("ok") is True and left == 0, "code=%s left=%s" % (c6, left))
c7, b7 = code_of(delete_branch("verify-lock", conn=copy, user=admin))
chk("[6f] verify-lock（显式 deletable=false）→ 400 拒绝",
    c7 == 400 and "不可删除" in str(b7.get("error", "")), "code=%s err=%s" % (c7, b7.get("error")))
c8, b8 = code_of(delete_branch("verify-devt", conn=copy, user=admin))
copy.commit()
left2 = copy.execute("SELECT COUNT(*) FROM branches WHERE name='verify-devt'").fetchone()[0]
chk("[6g] verify-devt（dev 型 + 显式解锁 deletable）→ 放行（验收①端到端）",
    c8 == 200 and b8.get("ok") is True and left2 == 0, "code=%s left=%s" % (c8, left2))

# 6h 内置三条分支在任何情况下仍不可删（真库同名行，副本上验证守卫）
for nm in ("release", "dev", "personal"):
    msg = br.check_deletable(copy, nm)
    chk("[6h-%s] 内置分支默认不可删" % nm, msg is not None, msg or "(放行了!)")

# 6i 改名守卫（内置不可改名）
chk("[6i] 内置分支默认不可改名（release/dev/personal）",
    all(br.check_renamable(copy, nm) is not None for nm in ("release", "dev", "personal")))
chk("[6j] 自定义分支可改名",
    br.check_renamable(copy, "personal/x") is None)

# ══════════════ [7] 变异自证 ══════════════
print("\n--- [7] 变异自证（断言本身必须能被变异击穿） ---")
# M1：清空类型默认 → release 应变可写 → 判据 [2a]/[5-release] 必须失效
orig_type = br.TYPE_RULES
try:
    br.TYPE_RULES = {}
    m1_writable = br.effective_rules({"name": "release", "branch_type": "release"})["writable"]
finally:
    br.TYPE_RULES = orig_type
chk("[M1] 清空 TYPE_RULES → release 变可写（证明 [1a]/[2a]/[5-release] 非空转）",
    m1_writable is True)

# M2：清空名称兜底 → 内置 personal 应变可删 → 判据 [2c] 必须失效
orig_prot = br.PROTECTED_NAMES
try:
    br.PROTECTED_NAMES = frozenset()
    m2_del = br.effective_rules({"name": "personal", "branch_type": "personal"})["deletable"]
finally:
    br.PROTECTED_NAMES = orig_prot
chk("[M2] 清空 PROTECTED_NAMES → 内置 personal 变可删（证明 [2c]/[3c]/[6h] 非空转）",
    m2_del is True)

# M3：改成 fail-open（parse_rules 无脑放行）→ 损坏 JSON 下保护被放开 → 判据 [4a] 必须失效
orig_parse = br.parse_rules
try:
    br.parse_rules = lambda raw: {"writable": True, "deletable": True, "renamable": True}
    m3_del = br.effective_rules({"name": "personal", "branch_type": "personal",
                                 "protection_rules": "{not a json"})["deletable"]
finally:
    br.parse_rules = orig_parse
chk("[M3] 改为 fail-open → 损坏 JSON 下 protection 被放开（证明 [4a] 非空转）",
    m3_del is True)

# ══════════════ 汇总 ══════════════
copy.close()
print("\n" + "=" * 78)
print("基线通过 %d / 失败 %d%s" % (len(_ok), len(_fail),
                                   ("  失败项: " + " | ".join(_fail)) if _fail else ""))
print("=" * 78)
sys.exit(1 if _fail else 0)
