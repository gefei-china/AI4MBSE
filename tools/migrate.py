# -*- coding: utf-8 -*-
"""迁移安全闸的 CLI 入口（P0-3 前置，2026-10-04）。

用法（在仓库根目录）：
    # ① 只看现状，绝不落任何写操作（**默认建议先跑这个**）
    python tools/migrate.py --dry-run

    # ② 迁移：先自动热备（WAL 安全的唯一正确方式），备份失败即拒绝
    python tools/migrate.py --apply

    # ③ 无视备份失败强行迁移（危险，需显式确认）
    python tools/migrate.py --apply --force

为什么需要它：`init_db()` 是唯一迁移入口，一次要建 129 表 + 补列 + 全部 `_migrate_*` +
播种，**中间没有任何闸**。本机已两次踩到：验证脚本误在生产库上跑了一遍迁移；
三份规范入库 13.4 分钟且必须停服务。⇒ 需要一个显式的、可预演的入口。

⚠️ 本脚本**不能替代**服务启动时的 init_db（lifespan 仍走原路径，语义未变）。
它服务的是"人工变更 / 批量导入前 / 升级前"这些需要止损点的场景。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(
        description="迁移安全闸：dry-run 盘点/ 自动热备后迁移 / 失败即拒绝")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只盘点，不落任何写操作")
    g.add_argument("--apply", action="store_true", help="执行迁移（先自动热备）")
    ap.add_argument("--force", action="store_true",
                    help="备份失败时仍强行迁移（危险，默认拒绝）")
    ap.add_argument("--no-backup", action="store_true",
                    help="跳过备份（**不建议**：无回滚点）")
    ap.add_argument("--backup-dir", default=None, help="备份目录（默认 <repo>/backups）")
    ap.add_argument("--tag", default="pre-migrate", help="备份文件名标签")
    args = ap.parse_args()

    from database.schema import safe_init_db, inspect_db_state

    if args.dry_run:
        rep = safe_init_db(dry_run=True)
    else:
        if args.no_backup and not args.force:
            print("⚠️  --no-backup 会移除回滚点；确认请再追加 --force", flush=True)
            return 2
        rep = safe_init_db(dry_run=False, backup=not args.no_backup,
                           backup_dir=args.backup_dir, tag=args.tag,
                           force=args.force)

    print(json.dumps(rep, ensure_ascii=False, indent=2))

    if rep.get("action", "").startswith("aborted"):
        print("\n⛔ 已拒绝迁移：%s" % rep.get("error", ""), flush=True)
        return 1
    if args.dry_run:
        s = rep.get("state") or {}
        print("\n== dry-run 结论：未落任何写操作 ==", flush=True)
        print("   库：%s（%.1f MB，%s 张表，%s 行）"
              % (s.get("path"), s.get("size_mb", 0), s.get("tables", 0), s.get("row_total", 0)),
              flush=True)
        print("   确认无误后执行： python tools/migrate.py --apply", flush=True)
        return 0
    print("\n✅ 迁移完成（%ss）。备份：%s" % (rep.get("elapsed_s"), rep.get("backup") or "无"),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())