# -*- coding: utf-8 -*-
"""标准档案切换 CLI —— 换标准只改 `active_profile` 一个字。

## 为什么要有这个脚本
「支持基于不同标准和规则处理」的落点是**多套标准档案**，而切换必须满足两条：
① **单点写入**：只有 `requirement_checks.switch_profile()` 会写 `active_profile`，
   避免"这里改 JSON、那里改 SQL"两套写法把结构改歪；
② **可自证**：切完立刻回读库（新连接，不用旧快照），打印真正生效的 docs/标准集
   —— 因为"命令跑过了"不等于"配置生效了"（老教训：`taskkill` 失败被管道吞掉、
   旧 sqlite 连接回读到快照）。

## 用法
    python tools/switch_requirement_profile.py --list
    python tools/switch_requirement_profile.py --use incose_only_v1
    python tools/switch_requirement_profile.py --show

退出码 0 = 成功；1 = 失败（档案不存在 / 结构不对 / 回读不一致）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services import requirement_checks as rc   # noqa: E402

DB = os.path.join(_ROOT, "mbse.db")


def _conn():
    # ★ 服务在跑时库是 WAL 模式，写锁可能与服务的写入瞬时撞车。
    #   sqlite3 默认 timeout=5s 太短，实测会抛 `database is locked`（写入本身只要 0.0s，
    #   纯粹是没等到锁）。给足 15s 并显式设 busy_timeout，让"切档案"在服务运行时也能用。
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=15000")
    except Exception:
        pass
    return c


def _raw(c):
    row = c.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                    (rc.PROFILE_KEY,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        return {}


def show(prof):
    print("  生效档案 :", prof.get("active_profile") or prof.get("profile_id") or "(单档案)")
    print("  标准集   :", "、".join(prof.get("standard_ids") or []) or "-")
    print("  检索文档 :")
    for d in (prof.get("docs") or []):
        print("             -", d)
    if not (prof.get("docs") or []):
        print("             (空 ⇒ 不做文档白名单，检索走全库 —— 占位档案会长这样)")
    if prof.get("_active_fallback"):
        print("  ⚠️ active_profile 无效，已回退到第一套（请检查配置）")


def main():
    ap = argparse.ArgumentParser(description="切换需求校验的标准档案")
    ap.add_argument("--list", action="store_true", help="列出全部可用档案")
    ap.add_argument("--use", metavar="PROFILE_ID", help="切换到指定档案")
    ap.add_argument("--show", action="store_true", help="显示当前生效档案（默认动作）")
    a = ap.parse_args()

    c = _conn()
    try:
        raw = _raw(c)
        profs = raw.get("profiles") or {}
        if a.list:
            if not profs:
                print("当前是单档案结构，无 profiles 可列。")
                return 0
            cur = raw.get("active_profile")
            print("可用标准档案（★ = 当前生效）：")
            for pid in sorted(profs):
                one = profs.get(pid) or {}
                mark = "★" if pid == cur else " "
                print(f"  {mark} {pid:<22} docs={len(one.get('docs') or [])}  "
                      f"{'、'.join(one.get('standard_ids') or [])}")
                if one.get("description"):
                    print(f"      {one['description']}")
            return 0

        if a.use:
            try:
                r = rc.switch_profile(c, a.use)
            except sqlite3.OperationalError as e:
                # 不把裸 traceback 丢给用户：区分"锁冲突"与"配置错"，并给出可执行动作
                print("切换失败（数据库暂时被占用）：%r" % (e,))
                print("  服务正在写库时偶发，可重试一次；持续失败请先停服再切。")
                return 1
            if not r.get("ok"):
                print("切换失败：", r.get("error"))
                if r.get("available_profiles"):
                    print("  可选：", "、".join(r["available_profiles"]))
                return 1
            print(f"已切换 → {r['active_profile']}")
        # 回读自证：**新连接**读，不用上面那个（旧连接可能读的是快照）
        c2 = _conn()
        try:
            after = rc.load_profile(c2)
        finally:
            c2.close()
        print("\n回读（新连接）确认：")
        show(after)
        if a.use:
            if (after.get("active_profile") or "") != a.use:
                print("\n✗ 回读不一致：写入的 id 与生效的不符")
                return 1
            exp = (profs.get(a.use) or {}).get("docs") or []
            if sorted(after.get("docs") or []) != sorted(exp):
                print("\n✗ 回读不一致：docs 与档案定义不符")
                return 1
            print("\n✓ 回读一致（active / docs 均与档案定义相符）")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
