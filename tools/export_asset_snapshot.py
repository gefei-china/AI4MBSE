# -*- coding: utf-8 -*-
"""导出/查看流水线配置资产快照（`data/bootstrap/pipeline_assets.json`）。

用法
----
    # 从当前库导出快照（改完 agent/tool/skill 后必须跑一次，否则门禁会红）
    ./.venv/Scripts/python.exe tools/export_asset_snapshot.py --apply

    # 只看差异（不写盘）
    ./.venv/Scripts/python.exe tools/export_asset_snapshot.py

    # 看库与快照的漂移
    ./.venv/Scripts/python.exe tools/export_asset_snapshot.py --drift

为什么需要"导出"这一步而不是全自动
----------------------------------
快照是**代码的一部分**（随 git 走）。让它自动跟库同步，就等于"库改了代码自动变"，
那又回到"环境不可重建"。所以：**人改 → 人导出 → 人提交**，门禁负责提醒。
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import get_db                                  # noqa: E402
from database.bootstrap_assets import (                       # noqa: E402
    export_snapshot, snapshot_drift, snapshot_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="流水线配置资产快照导出/漂移检查")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--drift", action="store_true", help="只检查库与快照的漂移")
    args = ap.parse_args()

    conn = get_db()
    try:
        print("=" * 66)
        print("流水线配置资产快照  %s" % snapshot_path())
        print("=" * 66)

        if args.drift:
            d = snapshot_drift(conn)
            print("\n漂移检查：%s（%s）" % ("一致" if d["ok"] else "不一致", d.get("reason") or "-"))
            if d["missing_in_db"]:
                print("  快照有、库无（自举未生效？） %d 个:" % len(d["missing_in_db"]))
                for n in d["missing_in_db"][:15]:
                    print("     -", n)
            if d["missing_in_snapshot"]:
                print("  库有、快照无（**新增了但没导出**） %d 个:" % len(d["missing_in_snapshot"]))
                for n in d["missing_in_snapshot"][:15]:
                    print("     +", n)
            if d["field_diff"]:
                print("  同名字段不一致 %d 处:" % len(d["field_diff"]))
                for n in d["field_diff"][:20]:
                    print("     ~", n)
            return 0 if d["ok"] else 1

        # 预览 / 导出
        stats = export_snapshot(conn, dry_run=not args.apply)
        print("\n导出统计：")
        for k, v in stats.items():
            print("  %-22s %s" % (k, v))
        if args.apply:
            print("\n已写盘：%s" % snapshot_path())
            print("⚠️ 记得提交到 git —— 否则别人/CI 仍然拿不到这些资产。")
        else:
            print("\n（预览模式：未写盘。加 --apply 才写）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
