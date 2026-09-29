# -*- coding: utf-8 -*-
"""清理 agent_memory 中「按新提取规则不该存在」的历史残留（2026-09-29）。

背景：提取规则此前把「长」当成「值得记」，导致三类垃圾进了记忆库：
  1. reflow 把**知识图谱实体**当经验写（"IP67防护等级要求：…（来源 test-reg）"），54 条；
  2. rule_agent 把**整篇报告正文**当经验写（"# MBSE 任务汇总最终报告…"）；
  3. 寒暄/自测输入（"你好，我是 MBSE 平台的通用助手…"）。
实测：118 条未遗忘记忆中 71%（84 条）按新规则不合格，其中 47% 从未被检索命中过。

本脚本做什么：用 **MemoryService._extract_worthy()（生产判据，不另写一套）** 逐条复判，
把不合格的标记 `forgotten=1`（**软删，可恢复**）——不物理删除。

安全设计（对齐项目纪律）：
  - **dry-run 默认**：不加 --apply 只打印将要删的清单，不落库；
  - *备份前置**：apply 时先 `sqlite3.backup()`（WAL 安全，不能用 shutil.copy2）；
  - **白名单保护**：访问次数 > `--keep-access` 的条目永不动（"被用过"就是价值证据）；
  - **幂等**：只处理 forgotten=0 的行，重复跑结果一致；
  - **回退**：`--restore <backup.db>` 按 id 把 forgotten 还原为 0。

用法：
    python tools/verify/cleanup_agent_memory_residue.py                 # 预览
    python tools/verify/cleanup_agent_memory_residue.py --apply       # 执行
    python tools/verify/cleanup_agent_memory_residue.py --restore backups/xxx.db
"""
import argparse
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from memory_service import MemoryService  # noqa: E402

DB = os.path.join(_ROOT, "mbse.db")
BAK_DIR = os.path.join(_ROOT, "backups")
MIN_LEN = 40


def _backup():
    os.makedirs(BAK_DIR, exist_ok=True)
    dst = os.path.join(BAK_DIR, f"agent_memory_residue_{time.strftime('%Y%m%d_%H%M%S')}.db")
    src = sqlite3.connect(DB)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src.backup(dst_conn)      # WAL 安全
        finally:
            dst_conn.close()
    finally:
        src.close()
    return dst


def _classify(keep_access: int):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content, source, mem_type, access_count, created_at "
        "FROM agent_memory WHERE forgotten=0 ORDER BY id").fetchall()
    conn.close()
    drop, keep = [], []
    for r in rows:
        ac = int(r["access_count"] or 0)
        if ac > keep_access:            # 白名单：被用过 → 不动
            keep.append((r, f"protected_access={ac}"))
            continue
        ok, why = MemoryService._extract_worthy(r["content"] or "", MIN_LEN)
        (keep if ok else drop).append((r, "" if ok else why))
    return rows, drop, keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行（默认仅预览）")
    ap.add_argument("--keep-access", type=int, default=0,
                    help="访问次数 > 此值的条目永不清理（默认 0，即只清零访问；设 -1 关闭保护）")
    ap.add_argument("--restore", metavar="BACKUP_DB", help="从备份按 id 还原 forgotten=0")
    ap.add_argument("--limit", type=int, default=0, help="最多删多少条（0=不限，建议先小批试）")
    args = ap.parse_args()

    if args.restore:
        if not os.path.exists(args.restore):
            print("备份不存在:", args.restore)
            return 2
        b = sqlite3.connect(args.restore)
        ids = [r[0] for r in b.execute("SELECT id FROM agent_memory WHERE forgotten=1").fetchall()]
        b.close()
        c = sqlite3.connect(DB)
        n = 0
        for i in ids:
            cur = c.execute("UPDATE agent_memory SET forgotten=0 WHERE id=? AND forgotten=1", (i,))
            n += cur.rowcount
        c.commit()
        c.close()
        print(f"已还原 {n} 条（备份含 forgotten=1 共 {len(ids)} 条）")
        return 0

    rows, drop, keep = _classify(args.keep_access)
    print(f"库: {DB}")
    print(f"未遗忘 {len(rows)} 条 → 将清理 {len(drop)} 条 / 保留 {len(keep)} 条")
    if args.keep_access >= 0:
        prot = sum(1 for _, w in keep if str(w).startswith("protected"))
        print(f"  （其中 {prot} 条因 access_count > {args.keep_access} 被白名单保护）")
    import collections
    rc = collections.Counter(w.split(":")[0] for _, w in drop)
    print("清理原因:", dict(rc))
    print("清理条数 by source:", dict(collections.Counter(r["source"] for r, _ in drop)))

    show = drop[:args.limit] if args.limit else drop
    print(f"\n-- 清单（前 {min(len(show), 25)} / 共 {len(show)} 条）--")
    for r, why in show[:25]:
        print(f"  #{r['id']:>4} [{r['source']:<10}] acc={r['access_count']} why={why[:20]:<22} "
              f"{(r['content'] or '')[:56]!r}")
    if len(show) > 25:
        print(f"  ... 其余 {len(show) - 25} 条略")

    if not args.apply:
        print("\n[DRY-RUN] 未落库。确认后加 --apply 执行。")
        return 0

    if not drop:
        print("无待清理条目。")
        return 0
    bak = _backup()
    print(f"\n已备份 → {bak}")
    ids = [r["id"] for r, _ in show]
    c = sqlite3.connect(DB)
    n = 0
    for i in ids:
        n += c.execute("UPDATE agent_memory SET forgotten=1 WHERE id=? AND forgotten=0", (i,)).rowcount
    c.commit()
    c.close()
    # 开新连接复核（旧连接读旧事务视图会假 FAIL）
    c2 = sqlite3.connect(DB)
    left = c2.execute("SELECT COUNT(*) FROM agent_memory WHERE forgotten=0").fetchone()[0]
    c2.close()
    print(f"已软删 {n} 条；库内剩余未遗忘 {left} 条")
    print(f"回退：python {os.path.relpath(__file__, _ROOT)} --restore {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
