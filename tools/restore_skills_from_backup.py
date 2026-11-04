# -*- coding: utf-8 -*-
"""从备份恢复被"资产自举"误覆盖的同名 skill 行（2026-11-04 事故善后）。

── 事故经过（如实记录）────────────────────────────────────────────────────
`database/bootstrap_assets.py::_bootstrap_tables` 第一版用
`UPDATE skills SET ... WHERE name=?` 更新已存在的行。
而 `skills` 表**没有 name 唯一约束**（实测：`行业调研` 5 行、`智源平台管理` 3 行、
`Requirement_Analysis_Pre-check` 4 行）⇒ 该 UPDATE **命中了全部同名行**，
把快照里那一条的内容写进了所有同名行。

实测损失（备份 `backups/mbse.db.bak_planguard_20261104_084822` 对比当前）：
  · `行业调研`     5 行的唯一内容数 3 → **1**（id 131 len1578、id 134 len1722 被覆盖成 1040）
  · `智源平台管理` 3 行的唯一内容数 3 → **1**（id 133 len6043、id 136 len5456 被覆盖成 4958）
  · agents / tools **不受影响**（两表都有 name 唯一约束，不存在同名多行）

代码侧已修：`_bootstrap_tables` 现在对**同名多行一律跳过**（计入 ambiguous），
并要求人工清理同名冲突 —— 自举不再替人裁决"该留哪一条"。

── 本脚本做什么 ─────────────────────────────────────────────────────────
只对**受影响的这几个 id**，把"快照里有、且当前值等于快照值、但在备份中不同"的字段
从备份写回。判据三条同时成立才恢复，避免把 08:48 之后的合法改动冲掉：
  ① 该 id 在备份中存在；
  ② 当前值 == 快照值（证明它是被自举写成的）；
  ③ 备份值 != 当前值（确实需要恢复）。
"""
import argparse
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")
BAK = os.path.join(_ROOT, "backups", "mbse.db.bak_planguard_20261104_084822")
SNAP = os.path.join(_ROOT, "data", "bootstrap", "pipeline_assets.json")

#: 受影响的 id（实测确认：内容被覆盖成快照值）
TARGET_IDS = (131, 134, 133, 136)


def main() -> int:
    ap = argparse.ArgumentParser(description="从备份恢复被误覆盖的同名 skill 行")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--ids", default=",".join(str(i) for i in TARGET_IDS),
                    help="逗号分隔的 skill id")
    args = ap.parse_args()

    import json
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    snap = json.load(open(SNAP, encoding="utf-8"))
    snap_by_name = {r["name"]: r for r in (snap.get("tables") or {}).get("skills") or []}

    if not os.path.isfile(BAK):
        print("备份不存在：%s" % BAK)
        return 2
    bak = sqlite3.connect("file:%s?mode=ro" % BAK, uri=True)
    bak.row_factory = sqlite3.Row
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    plan = []
    for sid in ids:
        b = bak.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
        c = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
        if not b or not c:
            print("  #%s 在备份或当前库中不存在，跳过" % sid)
            continue
        srow = snap_by_name.get(c["name"]) or {}
        fixes = []
        for col in c.keys():
            if col in ("id", "name"):
                continue
            cv, bv = c[col], b[col]
            if cv == bv:
                continue
            sv = srow.get(col, "<不在快照>")
            # ② 当前值必须等于快照值（证明是被自举写的）
            if str(cv) != str(sv):
                continue
            fixes.append((col, bv, cv))
        if fixes:
            plan.append((sid, c["name"], fixes))

    print("=" * 70)
    print("从备份恢复被误覆盖的 skill 行  mode=%s" % ("APPLY" if args.apply else "预览"))
    print("=" * 70)
    if not plan:
        print("\n没有需要恢复的行（可能已恢复过，或当前值与备份一致）。")
        return 0
    for sid, nm, fixes in plan:
        print("\n  #%d %s  需恢复 %d 个字段：" % (sid, nm, len(fixes)))
        for col, bv, cv in fixes:
            print("    · %-16s 备份=%d字符 → 当前=%d字符"
                  % (col, len(str(bv or "")), len(str(cv or ""))))

    if not args.apply:
        print("\n（预览模式：未写库。加 --apply 才执行）")
        return 0

    n = 0
    for sid, nm, fixes in plan:
        sets = ",".join('"%s"=?' % c for c, _, _ in fixes)
        vals = [bv for _, bv, _ in fixes] + [sid]
        conn.execute("UPDATE skills SET %s WHERE id=?" % sets, tuple(vals))
        n += 1
    conn.commit()
    print("\n已恢复 %d 行。" % n)

    # 自证：恢复后唯一内容数应回到备份的水平
    for tgt_name in ("行业调研", "智源平台管理"):
        cur = {r[0] for r in conn.execute(
            "SELECT content FROM skills WHERE name=?", (tgt_name,))}
        bk = {r[0] for r in bak.execute(
            "SELECT content FROM skills WHERE name=?", (tgt_name,))}
        print("  %s：当前唯一内容 %d / 备份 %d  %s"
              % (tgt_name, len(cur), len(bk), "✓" if len(cur) >= len(bk) else "✗ 仍有差异"))
    conn.close()
    bak.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
