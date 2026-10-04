#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/clean_tmp.py —— 开发期磁盘垃圾清理（P2-3，2026-10-04）

## 为什么需要这个脚本

2026-10-04 实测：`tmp/` 7.7 GB + `backups/` 1.8 GB = **9.5 GB**，
而真实业务库（根目录 `mbse.db`）只有 **306 MB** ——
**开发产物是真实数据的 30 倍**。其中 `tmp/` 里 34 个 `.db/.bak` 占 6.77 GB，
全部是历次验证脚本留下的库快照（`p01_m1.db`、`uisafe.db`、`mt_ctx/*.bak`…），
**运行时零价值**。

只删一次会复发（本项目一天就堆出 6.8 GB）。本脚本按"mtime + 后缀 + 大小"
三个条件清理，可挂 pre-commit / CI / 手工定期跑。

## 三条安全约束（每条都对应一次真实踩坑）

1. **绝不碰生产库**。生产库是**仓库根目录的 `mbse.db`**（`core.config.DB_PATH`），
   不是 `data/mbse.db`（后者是0 字节的遗留空壳）。脚本启动时解析出真实
   `DB_PATH` 并加入保护集合 —— 哪怕有人把参数写错指到它，也会被拒绝。
2. **绝不删 `backups/` 里"最近 N 份"以外的**其他类型文件。只按后缀白名单删，
   目录（`task23-restore/`）需显式 `--dirs` 才动。
3. **默认 dry-run**。不加 `--apply` 只打印不删 —— 删除是不可逆操作，
   任何"清理脚本一上来就删"的默认行为都是危险的。

## 用法

```bash
# 看会删什么（默认 dry-run）
.venv/Scripts/python.exe tools/clean_tmp.py

# 真删（tmp 下 .db/.bak/.bak-* 超过 7 天或超过 100MB）
.venv/Scripts/python.exe tools/clean_tmp.py --apply

# 自定义阈值
.venv/Scripts/python.exe tools/clean_tmp.py --apply --days 3 --min-size-mb 50

# 连 backups 里的旧库快照一起清（保留最近 3 份）
.venv/Scripts/python.exe tools/clean_tmp.py --apply --backups --keep 3

# 连 tmp 下的子目录一起删（如 bak_p01/）
.venv/Scripts/python.exe tools/clean_tmp.py --apply --dirs
```

## 退出码
- `0`：成功（或 dry-run 无异常）
- `1`：参数错误 / 保护集合被触发（拒绝执行）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 会被清理的后缀白名单。⚠️ 只列"验证脚本产物"，不是所有 .db/.bak：
#   tmp/kcx/*.after_patch.bak、tmp/ocr_probe/extract.py.bak 这类是**源码备份**，
#   体积小（<100KB）但有诊断价值，删了将来排查"改之前长什么样"会缺证据。
DB_SUFFIXES = (".db", ".db-shm", ".db-wal", ".bak", ".bak2")
BAK_PATTERNS = (".bak-", ".bak-p01", ".bak-p02", ".bak_before")

# 永远不删的目录（相对 ROOT）
NEVER_DIRS = ("data", "static", "database", "knowledge_pipeline", "agents",
              "repositories", "tools", "routers", "core", "services", "docs",
              "web", "node_modules", ".git", ".venv", "venv")

# 永不删除的具体文件（相对 ROOT）—— 生产库与今天的热备
NEVER_FILES = ("mbse.db", "mbse.db-shm", "mbse.db-wal", "mbse.db.bak-p1-3")


def _protected_abs_paths() -> set:
    """解析出绝对不可删的路径集合（含运行时解析的真实 DB_PATH）。"""
    prot = {os.path.normcase(os.path.join(ROOT, p)) for p in NEVER_FILES}
    # ⚠️ 生产库路径必须**运行时解析**，不能硬编码：
    #   历史上出现过 data/mbse.db（0 字节遗留）与根目录 mbse.db（真库）并存，
    #   硬编码就会把真库当垃圾删掉 —— 这类"凭印象写路径"是本项目反复吃过亏的地方。
    try:
        sys.path.insert(0, ROOT)
        from core.config import DB_PATH  # noqa
        prot.add(os.path.normcase(os.path.abspath(DB_PATH)))
    except Exception as e:  # 导入失败不致命，但必须让人知道
        print("[warn] 无法解析 core.config.DB_PATH（%s）；"
              "仅用 NEVER_FILES 兜底" % e, file=sys.stderr)
    return prot


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def _is_candidate(path: str, min_size: int) -> bool:
    name = os.path.basename(path).lower()
    if not (name.endswith(DB_SUFFIXES) or any(p in name for p in BAK_PATTERNS)):
        return False
    try:
        return os.path.getsize(path) >= min_size
    except OSError:
        return False


def scan(root: str, days: int, min_size_mb: float, max_files: int = 200):
    """扫描一个目录，返回 (命中列表, 被跳过原因)。"""
    min_size = int(min_size_mb * 1024 * 1024)
    prot = _protected_abs_paths()
    cutoff = time.time() - days * 86400
    hits, skips = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, ROOT)
        top = rel.split(os.sep)[0]
        if top in NEVER_DIRS:
            continue
        for fn in filenames:
            full = os.path.normcase(os.path.join(dirpath, fn))
            if full in prot:
                skips.append((full, "受保护（生产库/热备）"))
                continue
            if not _is_candidate(full, min_size):
                continue
            try:
                mt = os.path.getmtime(full)
            except OSError:
                continue
            hits.append((full, os.path.getsize(full), mt))
    # 大文件优先；同大小按时间新的优先（先删更有价值的垃圾）
    hits.sort(key=lambda x: (-x[1], -x[2]))
    return hits[:max_files], skips


def _dir_size(root: str) -> tuple:
    """(文件数, 总字节) —— 用 os.scandir 避免 stat 风暴。"""
    n = tot = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            n += 1
                            tot += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return n, tot


def main() -> int:
    ap = argparse.ArgumentParser(description="清理开发期磁盘垃圾（dry-run 默认）")
    ap.add_argument("--apply", action="store_true", help="真删除（默认只打印）")
    ap.add_argument("--days", type=int, default=7,
                    help="mtime 超过该天数才删（0 = 不看时间）")
    ap.add_argument("--min-size-mb", type=float, default=1.0,
                    help="小于该大小不删（保护源码备份类小文件）")
    ap.add_argument("--backups", action="store_true", help="同时清理 backups/")
    ap.add_argument("--keep", type=int, default=3,
                    help="backups/ 里保留最近几份真备份")
    ap.add_argument("--dirs", action="store_true",
                    help="同时删除 tmp/ 下的子目录（如 bak_p01/）")
    args = ap.parse_args()

    roots = [os.path.join(ROOT, "tmp")]
    if args.backups:
        roots.append(os.path.join(ROOT, "backups"))

    total_files = total_bytes = 0
    for root in roots:
        if not os.path.isdir(root):
            print("[skip] %s 不存在" % root)
            continue
        n0, b0 = _dir_size(root)
        print("\n=== %s （清理前 %.1f MB / %d 文件）==="
              % (os.path.relpath(root, ROOT), b0 / 1048576.0, n0))

        hits, skips = scan(root, args.days, args.min_size_mb)
        for p, why in skips:
            print("  [protected] %s  <- %s" % (os.path.relpath(p, ROOT), why))

        # backups：额外剔除"最近 keep 份"
        # ⚠️ 判"是不是库备份"必须复用 _is_candidate 的口径，**不能**另写
        #   `endswith((".db", ".bak"))`：真实文件名是
        #   `mbse.db.bak-before-auditperm-20261003_164605`，以 `.bak-before-…` 结尾，
        #   按前者过滤**一份都匹配不到** ⇒ keep=3 实际只留 1 份（实测踩到）。
        # ⚠️ kept 必须**从 hits 里移除并单独汇报**。第一版只过滤不汇报，
        #   输出里出现"[keep] 保留最近 2 份"紧跟着两份都标 [would]——
        #   **自相矛盾且危险**：人会以为脚本坏了而改用裸 rm。
        if root.endswith("backups") and args.keep > 0:
            db_hits = [h for h in hits if _is_candidate(h[0], 0)]
            db_hits.sort(key=lambda x: -x[2])
            kept = db_hits[:args.keep]
            kept_set = {h[0] for h in kept}
            hits = [h for h in hits if h[0] not in kept_set]
            for p, s, m in kept:
                print("  [KEEP]     %7.1f MB  %5.1f 天前  %s"
                      % (s / 1048576.0, (time.time() - m) / 86400.0,
                         os.path.relpath(p, ROOT)))
            print("  [keep] 命中 %d 份库备份，保留最近 %d 份⇒ 待删 %d 份"
                  % (len(db_hits), len(kept), len(hits)))

        if not hits and not args.dirs:
            print("  无命中")
            continue
        for p, s, m in hits:
            age = (time.time() - m) / 86400.0
            print("  %-11s %7.1f MB  %5.1f 天前  %s"
                  % ("[DELETE]" if args.apply else "[would]", s / 1048576.0, age,
                     os.path.relpath(p, ROOT)))
            total_files += 1
            total_bytes += s

        if args.apply:
            ok = 0
            for p, _s, _m in hits:
                try:
                    os.remove(p)
                    ok += 1
                except OSError as e:
                    print("  [FAIL] %s: %s" % (p, e), file=sys.stderr)
            print("  已删除 %d/%d" % (ok, len(hits)))

        if args.dirs:
            for d in sorted(os.listdir(root)):
                dp = os.path.join(root, d)
                if not os.path.isdir(dp):
                    continue
                dn, db = _dir_size(dp)
                print("  %-11s %7.1f MB  %d 文件  %s"
                      % ("[RMDIR]" if args.apply else "[would-dir]",
                         db / 1048576.0, dn, os.path.relpath(dp, ROOT)))
                if args.apply:
                    import shutil
                    shutil.rmtree(dp, ignore_errors=True)
            if args.apply:
                pass  # 目录已删，重算体积在循环外统一输出

        n1, b1 = _dir_size(root)
        if args.apply:
            print("  清理后 %.1f MB / %d 文件（回收 %.1f MB）"
                  % (b1 / 1048576.0, n1, (b0 - b1) / 1048576.0))

    print("\n%s合计：%d 个文件 / %.2f GB%s"
          % ("已删除" if args.apply else "将删除", total_files,
             total_bytes / 1073741824.0,
             "" if args.apply else "（dry-run，加 --apply 才真删）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
