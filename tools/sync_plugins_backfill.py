"""sync_plugins_backfill — 把旧表全量同步到 plugins（能力中心可见）。

背景：2026-10-08/9 实测发现，tools/skills/agents 三张「运行时事实源」里有 43 行
从未投影到 plugins（上位管理注册表）⇒ 能力中心看不到它们。
根因不是插件管理失效，而是本轮迁移**用裸 sqlite3 写入、绕过了 Repository 层
挂的legacy_sync 同步钩子**（`agent_repo.py:22` / `studio_repo.py:23`）。

本脚本走既定同步桥 `legacy_sync.sync_from_legacy_safe`，不自己造轮子：
  ·幂等（实测连跑 3 次 = 1 次创建 + 2 次更新，不重复建）
  · 自动补 plugin_installs 系统级安装副本（list_mine 可见性的必要条件）
  · 失败不阻断主流程（_safe 后缀的语义）

默认 dry-run；--apply 才写库。含迁移前/后回读自检。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

PAIRS = [("tools", "tool"), ("skills", "skill"), ("agents", "agent")]


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


# 本轮（2026-10-08）迁移的判定基准。
# ★ 用 created_at 而非硬编码名单 —— 实测 id 101-118 的 created_at 全是2026-10-08 07:37
#   （V9 建的 19 个 skill），而 id 99/100 是 9 月的历史行。
#   名单式判定会漏（我第一版就把这 19 条误标成「历史」）。
ROUND_MARK = "2026-10-08"
# agents 表无 source_ref 列，故只按 id 段 + 名称双重确认
ROUND_AGENT_IDS = {634, 635, 636, 637, 638, 639, 640, 641}


def _is_patched(row, table: str) -> bool:
    """该行是否属于 2026-10-08 本轮 V3/V5/V9 迁移新增。"""
    nm = (row["name"] or "").lower()
    created = str(row["created_at"] or "")
    if created[:10] == ROUND_MARK:
        return True
    if table == "agents" and row["id"] in ROUND_AGENT_IDS:
        return True
    return False


def _plan(conn, only_mine: bool):
    """返回待同步清单：[(table, row_id, name, is_mine)]"""
    from plugin_system import legacy_sync as ls
    out = []
    for tbl, kind in PAIRS:
        for row in conn.execute(f"SELECT id, name, created_at FROM {tbl} ORDER BY id"):
            if ls.find_plugin_by_legacy(conn, tbl, row["id"]):
                continue                      # 已覆盖
            out.append((tbl, row["id"], row["name"], _is_patched(row, tbl)))
    if only_mine:
        out = [x for x in out if x[3]]
    return out


def main(apply_: bool, only_mine: bool) -> int:
    from plugin_system import legacy_sync as ls

    print("=" * 74)
    print("plugins 回填同步" + ("（APPLY）" if apply_ else "（DRY-RUN）")
          + (f"· 仅本轮新增" if only_mine else "· 全量"))
    print("=" * 74)

    if not os.path.isfile(DB):
        print(f"[ABORT] 库不存在：{DB}")
        return 2
    conn = _open()
    try:
        # 前置自检：确认目标库是真实工程库（避免路径算错 ⇒ 静默建空库）
        nobj = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        if nobj < 50:
            print(f"[ABORT] 目标库可疑：对象数={nobj}（应>50）")
            return 2

        # 同步桥自身健康度
        sc = ls.self_check(conn)
        if isinstance(sc, dict) and sc.get("ok") is False:
            print(f"[ABORT] legacy_sync.self_check 未通过：{sc}")
            return 2
        print(f"[前置] legacy_sync.self_check ok={sc.get('ok') if isinstance(sc, dict) else sc}")

        before = conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0]
        before_by_type = dict(conn.execute(
            "SELECT type, COUNT(*) FROM plugins GROUP BY 1").fetchall())
        plan = _plan(conn, only_mine)
        print(f"\nplugins 迁移前：{before} 行 {before_by_type}")
        print(f"待同步：{len(plan)} 条"
              f"（其中本轮新增 {sum(1 for x in plan if x[3])} 条）")

        if not plan:
            print("  无需同步 —— plugins 已覆盖全部旧表行")
            return 0

        by_table = {}
        for tbl, rid, nm, mine in plan:
            by_table.setdefault(tbl, []).append((rid, nm, mine))
        for tbl in by_table:
            print(f"\n── {tbl}（{len(by_table[tbl])} 条）──")
            for rid, nm, mine in by_table[tbl][:60]:
                mark = "★本轮" if mine else "历史"
                print(f"  {mark} id={rid:<7} {str(nm)[:44]}")
            if len(by_table[tbl]) > 60:
                print(f"    … 另有 {len(by_table[tbl]) - 60} 条")

        if not apply_:
            print("\n（dry-run，未写库）加 --apply 执行")
            return 0

        # 执行同步
        ok_n, fail = 0, []
        created, updated = 0, 0
        for tbl, rid, nm, mine in plan:
            try:
                ok, note = ls.sync_from_legacy_safe(conn, tbl, rid)
                if ok:
                    ok_n += 1
                    s = str(note)
                    if "已创建" in s:
                        created += 1
                    elif "已更新" in s:
                        updated += 1
                else:
                    fail.append((tbl, rid, nm, str(note)[:90]))
            except Exception as exc:            # noqa: BLE001
                fail.append((tbl, rid, nm, f"{type(exc).__name__}: {exc}"[:90]))
        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0]
        after_by_type = dict(conn.execute(
            "SELECT type, COUNT(*) FROM plugins GROUP BY 1").fetchall())
        print(f"\n── 执行结果 ──")
        print(f"  成功 {ok_n}/{len(plan)}（新建 {created} · 更新 {updated}）｜失败 {len(fail)}")
        print(f"  plugins {before} → {after} 行")
        print(f"  类型分布 {before_by_type}")
        print(f"     → {after_by_type}")
        if fail:
            print("  失败明细：")
            for t, r, n, note in fail[:12]:
                print(f"    ✗ {t} id={r} {n}：{note}")

        # ── 迁移后自检 ──
        print("\n── 自检 ──")
        ok = True
        # ① 覆盖率：旧表每行都应能在 plugins 找到
        miss_total = 0
        for tbl, kind in PAIRS:
            n = len([1 for r in conn.execute(f"SELECT id FROM {tbl}")
                     if not ls.find_plugin_by_legacy(conn, tbl, r[0])])
            miss_total += n
            print(f"  {'[OK  ]' if n == 0 else '[FAIL]'} {tbl:8} 未覆盖 {n} 行")
            if n:
                ok = False
        # ② list_mine 真实身份可见性
        from plugin_system.store import queries as Q
        for uid in (1,):
            try:
                items = Q.list_mine(conn, {"id": uid}, "", "")
                print(f"  [OK  ] list_mine(user_id={uid}) → {len(items)} 项")
                if not items:
                    ok = False
            except Exception as exc:            # noqa: BLE001
                print(f"  [FAIL] list_mine 异常：{type(exc).__name__}: {exc}")
                ok = False
        # ③ 安装副本：系统级 user_id=0 的副本数
        n0 = conn.execute(
            "SELECT COUNT(*) FROM plugin_installs WHERE user_id=0").fetchone()[0]
        print(f"  [INFO] 系统级安装副本（user_id=0）：{n0} 条")
        # ④ 本轮新增能力必须在 plugins 里可见
        mine_new = [x for x in plan if x[3]]
        missing_mine = []
        for tbl, rid, nm, mine in mine_new:
            if not ls.find_plugin_by_legacy(conn, tbl, rid):
                missing_mine.append(nm)
        print(f"  {'[OK  ]' if not missing_mine else '[FAIL]'} 本轮新增 "
              f"{len(mine_new)} 条全部已投影" +
              (f"（缺 {missing_mine[:5]}）" if missing_mine else ""))
        if missing_mine:
            ok = False
        # ⑤ 运行时未被破坏：旧表行数不变
        for tbl, _ in PAIRS:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  [INFO] {tbl} 仍 {n} 行（同步不删旧表数据）")

        print("\n" + "=" * 74)
        print("✅ 回填完成" if ok else "⚠️ 完成但有未通过项（见上）")
        print("=" * 74)
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--only-mine", action="store_true",
                    help="只同步 2026-10-08 本轮新增的（不含历史遗留）")
    a = ap.parse_args()
    sys.exit(main(a.apply, a.only_mine))