# -*- coding: utf-8 -*-
"""孤儿源副本治理（2026-09-21，D8）。

背景（实测 2026-09-21）：`data/uploads/` 是**只增不减**的目录 ——
`MetaRepo.delete_document` 原先只清 `domain_review_queue` + `documents`，从不碰副本。
结果：34 份副本里 **27 份是孤儿**（`{id}_` 前缀对应的 documents 行早已不存在）。
删除路径已修（`MetaRepo.delete_document` 现同步清副本），本脚本处理**存量**。

⚠️ 安全约定（本仓个人文件/不可逆操作纪律）：
- **默认 dry-run**：只打印清单（`--apply` 才真删）；
- 只处理能解析出 `{数字}_` 前缀、且该 id 不在 documents 里的文件 ——
  任何"看不出属于哪份文档"的文件（含 `.gitkeep`、手工放进去的素材）**一律跳过**；
- 先 `--apply` 逐条打印删了什么，失败即停并保留现场。

用法：
    .venv/Scripts/python.exe -X utf8 tools/cleanup_orphan_uploads.py            # 只出清单
    .venv/Scripts/python.exe -X utf8 tools/cleanup_orphan_uploads.py --apply    # 真删
"""
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO))


def main() -> int:
    apply = "--apply" in sys.argv
    from knowledge_pipeline.ingest import source_copy_dir
    base = source_copy_dir()
    if not os.path.isdir(base):
        print(f"目录不存在：{base}")
        return 0
    con = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    live_ids = {r[0] for r in con.execute("SELECT id FROM documents")}
    con.close()

    files = sorted(os.listdir(base))
    orphan, kept, skipped = [], [], []
    for f in files:
        p = os.path.join(base, f)
        if not os.path.isfile(p):
            skipped.append((f, "非普通文件"))
            continue
        m = re.match(r"^(\d+)_", f)
        if not m:
            skipped.append((f, "无 {id}_ 前缀（不判归属，跳过）"))
            continue
        if int(m.group(1)) in live_ids:
            kept.append(f)
        else:
            orphan.append(f)

    print("=" * 78)
    print(f"源副本目录：{base}")
    print(f"  documents 存活 id 数 : {len(live_ids)}")
    print(f"  副本文件总数         : {len(files)}")
    print(f"  指向存活文档（保留） : {len(kept)}")
    print(f"  孤儿副本（可清理）   : {len(orphan)}")
    print(f"  无法判归属（跳过）   : {len(skipped)}")
    print("=" * 78)
    if orphan:
        total = sum(os.path.getsize(os.path.join(base, f)) for f in orphan)
        print(f"\n孤儿清单（合计 {total / 1024:.1f} KB）：")
        for f in orphan:
            print(f"  [orphan] {f}  ({os.path.getsize(os.path.join(base, f))} B)")
    if skipped:
        print("\n跳过清单：")
        for f, why in skipped:
            print(f"  [skip]   {f}  —— {why}")
    if not orphan:
        print("\n无孤儿副本，无需清理。")
        return 0
    if not apply:
        print("\n（dry-run：未删除任何文件。加 --apply 执行清理。）")
        return 0
    print("\n开始清理（--apply）…")
    removed = 0
    for f in orphan:
        try:
            os.remove(os.path.join(base, f))
            removed += 1
            print(f"  [deleted] {f}")
        except Exception as e:
            print(f"  [FAILED]  {f} —— {e}（停止，保留现场）")
            break
    print(f"\n清理完成：{removed}/{len(orphan)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
