# -*- coding: utf-8 -*-
"""清理 entities/relations 上的**悬空 project_id 引用**（2026-10-04 架构决策配套）。

## 为什么做（背景链）
1. 米爸拍板：「文档向量库、图库**不需要基于项目隔离，是统一使用的数据底座**」
   ⇒ 严格地说，**图谱数据根本不该挂项目**，`project_id` 在这些表上是历史包袱。
2. 历史包袱的具体形态：种子项目 `project-satnet-broadband`（`database/seeds.py:249` 建的）
   被删除后，`entities` 189 行 / `relations` 249 行仍指向这个**已不存在的 id** ⇒ 悬空引用。
3. 成因已修（`ProjectRepo.detach_project_references`，2026-10-04），**不会产生新孤儿**；
   本脚本只处理**存量**。

## 为什么默认是「清空」而不是「回填到某个项目」
按统一底座语义，"这条数据属于哪个项目"**本身不该有答案**。挂到任何项目都是给共享底座
打上错误的归属标签；清空成空串才是与决策一致的状态。

## 安全设计
- **默认 dry-run**：不加 `--apply` 只报告，不写库。
- **`--apply` 前强制备份**：`backups/pre-orphan-cleanup-<date>.db`（`sqlite3.backup` 在线热备，
  不用 `copy2` —— WAL 库直接拷文件会拿到旧快照）。
- **只碰悬空行**：`project_id` 非空**且**在 `projects` 表里查无此项目，才会被清空。
  合法归属的行一律不动。
- **不改 `entity_versions`**：那是历史快照表，改写历史语义上应谨慎。
  故本脚本**报告但不修改**它，由人决定。

用法：
    python tools/verify/clean_orphan_project_refs.py            # dry-run（默认）
    python tools/verify/clean_orphan_project_refs.py --apply    # 真清理（自动先备份）
"""
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "mbse.db")
APPLY = "--apply" in sys.argv

TABLES = ("entities", "relations")


def _scan(con):
    out = {}
    for t in TABLES:
        rows = con.execute(
            "SELECT COALESCE(project_id,'') pid, COUNT(*) n FROM %s "
            "WHERE COALESCE(project_id,'')<>'' "
            "AND project_id NOT IN (SELECT id FROM projects) GROUP BY 1" % t).fetchall()
        out[t] = {r[0]: r[1] for r in rows}
    return out


def main():
    if not os.path.exists(DB):
        print("找不到 %s" % DB)
        return 1
    con = sqlite3.connect(DB)
    orphans = _scan(con)
    total = sum(sum(v.values()) for v in orphans.values())

    print("=" * 66)
    print("悬空 project_id 引用清理 —— %s" % ("**dry-run（未写库）**" if not APPLY else "**执行清理**"))
    print("=" * 66)
    for t in TABLES:
        print("  %-10s 悬空 %d 行，分布：%s" % (t, sum(orphans[t].values()),
                                          orphans[t] or "（无）"))
    print("  合计 %d 行" % total)
    # entity_versions 只报告不改
    try:
        ev = con.execute(
            "SELECT COUNT(*) FROM entity_versions WHERE COALESCE(project_id,'')<>'' "
            "AND project_id NOT IN (SELECT id FROM projects)").fetchone()[0]
        print("\n  ⚠️ entity_versions（历史快照表）另有 %d 行悬空 —— 本脚本**只报告不修改**，"
              "是否改写历史需人工决定" % ev)
    except Exception as e:
        print("\n  entity_versions 探测失败：%s" % str(e)[:80])

    if total == 0:
        print("\n结论：无悬空引用，无需清理。")
        con.close()
        return 0
    if not APPLY:
        print("\n结论：dry-run 未做任何改动。确认后加 --apply 执行（会自动先备份）。")
        con.close()
        return 0

    # ── 备份（WAL 库唯一安全的在线备份方式）──
    bdir = os.path.join(ROOT, "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bpath = os.path.join(bdir, "pre-orphan-cleanup-%s.db" % stamp)
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(bpath)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print("\n[备份] %s（%.1f MB）" % (bpath, os.path.getsize(bpath) / 1e6))

    n = 0
    for t in TABLES:
        cur = con.execute(
            "UPDATE %s SET project_id='' WHERE COALESCE(project_id,'')<>'' "
            "AND project_id NOT IN (SELECT id FROM projects)" % t)
        n += cur.rowcount or 0
    con.commit()
    left = _scan(con)
    left_total = sum(sum(v.values()) for v in left.values())
    con.close()
    print("[清理] 已清空 %d 行；剩余悬空 %d 行" % (n, left_total))
    print("[回滚] 如需撤销：用备份库替换 mbse.db（先停服务）")
    return 0 if left_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
