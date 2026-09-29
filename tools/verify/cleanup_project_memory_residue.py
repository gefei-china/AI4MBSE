# -*- coding: utf-8 -*-
"""清理 project_memories 的测试残留（2026-09-29）。

背景（实测取证）：
生产库 `project_memories` 24 条，`project_id` 全为空，`created_by='reflow'`，
内容是「IP67防护等级要求」与「CAN总线通信接口」**交替重复 12 遍** ——
来自 `tests/test_extended_features.py` / `test_glossary_routing.py` 的固定输入
（`reflow_from_text(conn, "系统必须满足IP67防护等级。接口采用CAN总线通信。", source="test-reg")`）。

这不是项目知识，是测试污染。按 P0 记忆召回的纪律 2（project_id 非空才召回），
它们本就进不了正常消费；但留在库里会污染管理界面，且一旦有人"顺手"补默认项目归属，
**测试数据就会变成项目知识**（本工程已有"数据摆放错被误判成配置错"的前车之鉴）。

策略（三条硬纪律）：
1. **先备份再用 OS 级复制**（WAL 库必须用 `sqlite3.backup()`，不能 `shutil.copy2` —— 直拷会丢 WAL 里的数据）。
2. **只删"能证明是测试残留"的行**，判据多重叠加（project_id 空 + created_by=reflow + 标题白名单），
   不做任何 LIKE 猜测式删除。
3. **幂等**：跑第二遍删 0 条；**可回退**：备份文件保留，回退命令写在输出里。

用法：python tools/verify/cleanup_project_memory_residue.py [--apply]
不带 --apply = 只报告不删除（dry-run，默认）。
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")

# 「能证明是测试残留」的判据（三重叠加，全部满足才删）：
# ① project_id 为空（正常项目记忆必须有归属）
# ② created_by = 'reflow'（回流写入）
# ③ 标题属于测试固定输入产生的白名单（**精确匹配**，不用 LIKE）
RESIDUE_TITLES = ("IP67防护等级要求", "CAN总线通信接口")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行删除（默认只报告）")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"✗ 库不存在: {DB}")
        return 1

    # ── 取证 ──
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    marks = ",".join("?" * len(RESIDUE_TITLES))
    where = ("(project_id IS NULL OR project_id='') AND created_by='reflow' "
             f"AND title IN ({marks})")
    rows = c.execute(
        f"SELECT id, project_id, category, title, created_by, created_at FROM project_memories "
        f"WHERE {where} ORDER BY id", RESIDUE_TITLES).fetchall()
    total = c.execute("SELECT COUNT(*) FROM project_memories").fetchone()[0]

    print(f"库: {DB}")
    print(f"project_memories 总数: {total}")
    print(f"命中「测试残留」判据的行: {len(rows)}")
    for r in rows[:30]:
        print(f"  id={r['id']:3d} pid={r['project_id']!r} by={r['created_by']} "
              f"cat={r['category']} {r['title']}")
    if len(rows) > 30:
        print(f"  ... 其余 {len(rows) - 30} 条")

    # ── 安全自证：非残留行必须不受影响 ──
    others = c.execute(
        f"SELECT COUNT(*) FROM project_memories WHERE NOT ({where})", RESIDUE_TITLES).fetchone()[0]
    print(f"\n不匹配判据的行（**不会被删**）: {others}")
    c.close()

    if not rows:
        print("\n✓ 无残留（幂等：可能已清理过）—— 退出")
        return 0

    if not args.apply:
        print("\n[DRY-RUN] 未删除任何行。加 --apply 实际执行。")
        return 0

    # ── 备份（WAL 安全：必须用 sqlite3.backup）──
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(ROOT, "backups", f"project_memories_residue_{ts}.db")
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(bak)
    with dst:
        src.backup(dst)          # ← sqlite3.backup()，不是 shutil.copy2
    dst.close()
    # 备份自证：备份库里也能查到这些行
    b = sqlite3.connect(bak)
    n_bak = b.execute(f"SELECT COUNT(*) FROM project_memories WHERE {where}", RESIDUE_TITLES).fetchone()[0]
    b.close()
    src.close()
    print(f"\n备份: {bak}")
    print(f"备份自证: 备份库中命中 {n_bak} 条（应 == {len(rows)}）")
    if n_bak != len(rows):
        print("✗ 备份校验失败，中止（不删任何数据）")
        return 1

    # ── 执行删除（**新开连接**执行，避免旧事务视图假象）──
    c2 = sqlite3.connect(DB)
    c2.row_factory = sqlite3.Row
    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))
    c2.execute("BEGIN IMMEDIATE")
    try:
        c2.execute(f"DELETE FROM project_memories WHERE id IN ({ph})", ids)
        c2.commit()
    except Exception as e:
        c2.rollback()
        c2.close()
        print(f"✗ 删除失败已回滚: {e}")
        return 1
    c2.close()

    # ── 复核（**再开新连接** —— 纪律：断言"清理干净了"必须开新连接，
    #    否则读到的是删除前的事务视图，会假 FAIL）──
    c3 = sqlite3.connect(DB)
    left = c3.execute(f"SELECT COUNT(*) FROM project_memories WHERE {where}", RESIDUE_TITLES).fetchone()[0]
    now_total = c3.execute("SELECT COUNT(*) FROM project_memories").fetchone()[0]
    others_after = c3.execute(
        f"SELECT COUNT(*) FROM project_memories WHERE NOT ({where})", RESIDUE_TITLES).fetchone()[0]
    c3.close()

    print(f"\n删除 {len(ids)} 条 → 剩余残留 {left}（应 0）")
    print(f"总数 {total} → {now_total}")
    print(f"非残留行 {others} → {others_after}（应不变）")
    ok = (left == 0) and (others_after == others)
    print("✓ 清理完成，非残留数据未受影响" if ok else "✗ 复核不通过，请检查")
    print(f"\n回退命令（如需还原）：\n  copy \"{bak}\" \"{DB}\"")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
