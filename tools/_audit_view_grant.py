"""一次性授权脚本：给「设计师 / 知识工程师」补审计只读查看权限 audit:view。

背景（2026-10-03）：P0-a 给 /api/audit 挂了 admin:audit_view 硬门禁，
而本平台 roles 里没有任何账号是系统管理员（实测 users 9/9 均为设计师 role_id=80、admin=[]），
导致「登录了反而 403、不登录却能看全量」，审计页与分支页审计 Tab 双双空白。
本脚本把 audit:view 补进 preset 角色，使审计查看回归可用，同时保留权限矩阵这一显式口径
（日后要收紧，只需从角色 permissions 里摘掉 audit:view，无需改代码）。

幂等：重复执行不会追加重复项；已含 audit:view 的角色跳过。
用法：
    ./.venv/Scripts/python.exe -X utf8 tools/_audit_view_grant.py            # dry-run（只打印）
    ./.venv/Scripts/python.exe -X utf8 tools/_audit_view_grant.py --apply    # 落库
    ./.venv/Scripts/python.exe -X utf8 tools/_audit_view_grant.py --verify   # 只校验
"""
import json
import sqlite3
import sys

DB = "mbse.db"
TARGET_ROLES = {80: "设计师", 81: "知识工程师"}
OP = "view"
DOMAIN = "audit"


def _load(perms_json):
    try:
        p = json.loads(perms_json or "{}")
    except Exception:
        p = {}
    return p if isinstance(p, dict) else {}


def main():
    apply = "--apply" in sys.argv
    verify_only = "--verify" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, permissions FROM roles WHERE id IN (%s)"
        % ",".join("?" * len(TARGET_ROLES)), list(TARGET_ROLES)).fetchall()

    changed = []
    for r in rows:
        p = _load(r["permissions"])
        ops = p.get(DOMAIN) or []
        if OP in ops:
            print(f"[skip ] role {r['id']} {r['name']}: 已含 {DOMAIN}:{OP} -> {ops}")
            continue
        if not verify_only:
            ops = list(ops) + [OP]
            p[DOMAIN] = ops
            if apply:
                conn.execute("UPDATE roles SET permissions=? WHERE id=?",
                             (json.dumps(p, ensure_ascii=False), r["id"]))
        changed.append((r["id"], r["name"], ops))
        print(f"[{'apply' if apply else 'plan '}] role {r['id']} {r['name']}: "
              f"{DOMAIN}:{r['permissions'] and (_load(r['permissions']).get(DOMAIN) or [])} -> {ops}")

    if apply and changed:
        conn.commit()
        print(f"\n已落库 {len(changed)} 个角色")
    elif not apply and changed and not verify_only:
        print("\n（dry-run，未落库；加 --apply 生效）")

    # 校验：全库角色 audit 域现状
    print("\n=== 校验：roles 的 audit 域 ===")
    for r in conn.execute("SELECT id, name, permissions FROM roles ORDER BY id"):
        p = _load(r["permissions"])
        if p.get("admin") or p.get(DOMAIN) or r["id"] in TARGET_ROLES:
            print(f"  {r['id']:>3} {r['name']:<10} admin={p.get('admin')} {DOMAIN}={p.get(DOMAIN)}")
    conn.close()


if __name__ == "__main__":
    main()
