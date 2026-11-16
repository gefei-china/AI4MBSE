"""P0-5 / D3：给 3 个 preset 角色补 `system_config` 授权位（启用门的前置动作）。

为什么要这一步（2026-11-15 真库实测，不是推测）：
  · `users` 表 17 行**全部** role_id=80（设计师 preset），且**没有任何 role_id=82 的账号**；
  · 前端 `35-ctxconfig.js` 的「AI 上下文配置」保存走 `PUT /api/system/config/static`；
  ⇒ 若先挂门再补位，上线当天所有真实用户（含唯一能登录的那批）全部 403，配置页直接报废。

为什么用独立域而不是 admin 域：
  `core/deps.py::require_permission` 把「permissions.admin 列表非空」视为**超级用户**，
  给设计师补一个 admin 权限 = 放行 user_manage / role_manage / ops_manage 等全站管理操作。
  ⇒ 新建 `system_config: [read, write]`，只作用于配置路由。

门的剩余效果（如实披露，别夸大）：
  · custom 角色（permissions 为 NULL/空）与未来新建的未授权角色 ⇒ 403；
  · 匿名在 `auth.enforce_login=False` 下仍按既有兼容态放行（验收不许用匿名判门）；
    要闭合这一口子需把 enforce_login 置 True，属独立的认证策略决策，不在本批。

默认 dry-run；`--apply` 才写。改动只有 roles 表 3 行，逐行原值 + 单步回滚 SQL 全部留痕，
不另做 534 MB 全库备份（P0-4 已有多份全库备份，且本改动可精确逐行回滚）。

跑法：./.venv/Scripts/python.exe -X utf8 tools/_p0_config_permission_grant.py [--apply]
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "mbse.db"
OUT = REPO / "docs" / "评测" / "p0-5-role-grants-20261115.json"
SEEDS = REPO / "database" / "seeds.py"

TARGETS = ("设计师", "知识工程师", "系统管理员")
DOMAIN = "system_config"
OPS = ["read", "write"]

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def seeds_ok(src):
    """纯判据：seeds.py 必须为 3 个 preset 各声明一次授权位。"""
    return src.count('"system_config": ["read", "write"]') == 3


def unchanged(permissions_after, permissions_before):
    """纯判据：补位只加域，原有每个域的值逐键不动（避开 list 不可哈希的 dict_items 比较）。"""
    a = json.loads(permissions_after or "{}")
    b = json.loads(permissions_before or "{}")
    return all(a.get(k) == v for k, v in b.items())


def grants_ok(rows):
    """纯判据：3 个 preset 都带 system_config:read/write（变异测试直接喂篡改输入）。"""
    if len(rows) != len(TARGETS):
        return False
    for r in rows:
        p = json.loads(r["permissions"] or "{}")
        ops = p.get(DOMAIN) or []
        if not ({"read", "write"} <= set(ops)):
            return False
        # 防线：补位不得把非管理员角色变成超级用户
        if r["name"] != "系统管理员" and p.get("admin"):
            return False
    return True


BEFORE_BACKUP = REPO / "backups" / "mbse.db.bak-before-p0-4-memory-20261115_235529.db"


def read_roles(db_path):
    """只读取 3 个 preset 角色。

    ⚠️ 连接必须带 row_factory：本工程已两次踩到 `dict(row)` 在裸连接上抛
    TypeError: cannot convert dictionary update sequence element #0 ...
    （core/online_eval.py:472、本脚本 2026-11-15 首跑）。
    """
    c = sqlite3.connect("file:%s?mode=ro" % Path(db_path).as_posix(), uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(
            "SELECT id, name, type, permissions FROM roles WHERE type='preset' ORDER BY id")]
    finally:
        c.close()


def main():
    apply = "--apply" in sys.argv
    live = read_roles(DB)
    already = any(DOMAIN in json.loads(r["permissions"] or "{}") for r in live)
    if already:
        # 幂等重跑（含「写库已成功、验证步崩溃」的场景）：原值只能从补位前备份取回，
        # 否则回滚 SQL 会指向已补位后的值 = 没有回滚能力。
        if not BEFORE_BACKUP.exists():
            print("FAIL 需要补位前备份才能生成可回滚留痕：%s" % BEFORE_BACKUP)
            return 1
        before_source = BEFORE_BACKUP.name
        original = {r["name"]: r["permissions"] for r in read_roles(BEFORE_BACKUP)}
    else:
        before_source = "live-db(pre-grant)"
        original = {r["name"]: r["permissions"] for r in live}
    rows = [dict(r, permissions=original[r["name"]]) for r in live]
    before = original
    print("BEFORE preset roles:", [(r["id"], r["name"]) for r in rows],
          "| 原值来源:", before_source, "| 已补位:", already)
    ck("G1 真库恰好 3 个 preset 角色且名字符合预期",
       tuple(r["name"] for r in rows) == TARGETS, [r["name"] for r in rows])
    ck("G2 补位前均无 system_config 域（说明这是首次变更）",
       all(DOMAIN not in json.loads(r["permissions"] or "{}") for r in rows))
    ck("G3 补位前两个非管理员角色 admin 为空（不是超级用户）",
       all(not json.loads(r["permissions"] or "{}").get("admin")
           for r in rows if r["name"] != "系统管理员"))

    # seeds.py 新库一致性（文本级 + 变异）
    seeds = SEEDS.read_text(encoding="utf-8")
    ck("G4 seeds.py 的 3 个 preset 都声明了 system_config:read/write", seeds_ok(seeds))
    ck("M1 变异（seeds.py 少声明一处）⇒ 判据必须翻红",
       not seeds_ok(seeds.replace('"system_config": ["read", "write"]', "", 1)))

    plan = []
    for r in rows:
        p = json.loads(r["permissions"] or "{}")
        p[DOMAIN] = list(OPS)
        plan.append({"id": r["id"], "name": r["name"],
                     "old": r["permissions"], "new": json.dumps(p, ensure_ascii=False)})
    ck("G5 计划产物通过 grants_ok（补位后 3 个角色都合规）",
       grants_ok([{"name": p["name"], "permissions": p["new"]} for p in plan]))
    ck("M2 变异（少补一个角色）⇒ grants_ok 必须翻红",
       not grants_ok([{"name": p["name"], "permissions": p["new"]} for p in plan[:-1]]))
    ck("M3 变异（误把设计师的 admin 域填成非空）⇒ grants_ok 必须翻红",
       not grants_ok([{"name": "设计师",
                       "permissions": json.dumps({"system_config": OPS, "admin": ["x"]})},
                      {"name": "知识工程师", "permissions": plan[1]["new"]},
                      {"name": "系统管理员", "permissions": plan[2]["new"]}]))

    if not apply:
        print("DRY-RUN（未写盘）；加 --apply 执行 roles 补位 + 留痕")
        print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0

    w = sqlite3.connect(str(DB))
    w.row_factory = sqlite3.Row
    try:
        if not already:
            for p in plan:
                w.execute("UPDATE roles SET permissions=? WHERE id=?", (p["new"], p["id"]))
            w.commit()
        after = [dict(r) for r in w.execute(
            "SELECT id, name, type, permissions FROM roles WHERE type='preset' ORDER BY id")]
        ck("A1 真库补位后 3 个 preset 都有 system_config:read/write", grants_ok(after))
        ck("A2 除 system_config 外，每个角色的原有权限逐键未变",
           all(unchanged(a["permissions"], before[a["name"]]) for a in after))
        ck("M4 变异（原有域被改掉）⇒ unchanged 必须翻红",
           not unchanged(json.dumps({"ai_chat": ["hacked"]}), '{"ai_chat": ["view"]}'))
        ck("A3 非管理员角色的 admin 域仍为空（没被提成超级用户）",
           all(not json.loads(a["permissions"]).get("admin")
               for a in after if a["name"] != "系统管理员"))
    finally:
        w.close()

    out = {
        "schema": "p0/config-role-grants@1",
        "applied_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "domain": DOMAIN, "ops": OPS,
        "before_source": before_source, "already_granted_at_start": already,
        "reason": "D3：启用配置写权限门前先给 preset 补位，避免真库 17 个设计师账号全员 403",
        "why_not_admin_domain": "require_permission 把 admin 非空视为超级用户；补 admin 会放行全站管理操作",
        "rows": plan,
        "rollback_sql": ["UPDATE roles SET permissions=%s WHERE id=%d;"
                         % ("'" + str(p["old"]).replace("'", "''") + "'", p["id"])
                         for p in plan],
        "caveat": "匿名在 enforce_login=False 下仍按既有兼容态放行；闭合需独立把 enforce_login 置 True",
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print("WROTE", OUT)
    print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
