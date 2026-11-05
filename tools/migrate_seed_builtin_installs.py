# -*- coding: utf-8 -*-
"""补齐「平台内置」插件的系统级可用标记 —— 让绑定过的工具真的能被注入（2026-10-28）。

## 为什么必须做（conv 9737 实证，两轮真机定位）

我把 `zhiyuan_sysmlv2_import/gen/check` 绑给「智源对接agent」后，**它们仍然没被注入**：

    _build_tools_def 实调 ⇒ 12 个工具，zhiyuan_sysmlv2_import -> MISSING

追到 `plugin_system/store/visibility.py:consumable_filter`：

| 插件 | author_name | 安装记录 | 判定 |
|---|---|---|---|
| `com.mbse.local.tool.sysml-v2-validate` | 本地创建 | `(user_id=0, enabled=1)` | ✅ 可消费 |
| `com.zhiyuan.legacy.tool.zhiyuan-sysmlv2-import` | **平台内置** | **无** | ❌ 被剔除 |
| `...zhiyuan-sysmlv2-gen` / `-check` | 平台内置 | 无 | ❌ 被剔除 |

判定分支原文（2026-09-29「卸载收窄」）：

    if inst_on is None:
        if (author_name or "") != "平台内置":
            out.add(pid)      # fail-open
        # author_name == '平台内置' ⇒ 视为"被管理员全局卸载" ⇒ 排除

⇒ **维度不足**：「从未安装过」与「被全局卸载」在数据上完全不可区分
（都是 `inst_on=None + author_name='平台内置'`），于是从未装过的内置能力被一并判死。
后果不是"少个工具"，而是：绑定动作看起来成功、运行时候选里没有它 ⇒
模型只能走次优路径（逐元素 create + 猜类型名）⇒ 整轮失败。
这正是用户问的「标杆平台早就成功了，为啥我们做不到」的机制答案之一。

## 修法（不放松任何闸）

只补**系统级可用标记**（`plugin_installs` 的 `user_id=0, enabled=1`）——
这是本仓既有语义「系统级 user_id=0 是内置能力的默认可用标记」。
真要停用仍有效：把该行置 `enabled=0` ⇒ `inst_on==0` ⇒ 明确停用分支照旧排除。

    python tools/migrate_seed_builtin_installs.py --dry
    python tools/migrate_seed_builtin_installs.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, "mbse.db")

#: 只处理这三类**确实需要出现在候选里**的内置能力（不批量放开全库）：
#: 智源直连写/生成/校验三件套 —— 它们是「把 SysML v2 写进平台」的最短路径。
TARGET_PREFIXES = ("com.zhiyuan.legacy.tool.zhiyuan-sysmlv2-",)


def main() -> int:
    dry = "--dry" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT plugin_id, name, scope, author_id, author_name, status "
            "FROM plugins WHERE status='published'").fetchall()
        todo = []
        for r in rows:
            if not any(str(r["plugin_id"]).startswith(p) for p in TARGET_PREFIXES):
                continue
            inst = conn.execute(
                "SELECT user_id, enabled FROM plugin_installs WHERE plugin_id=?",
                (r["plugin_id"],)).fetchall()
            if any(int(x["user_id"]) == 0 and int(x["enabled"]) == 1 for x in inst):
                print("已可用，跳过:", r["plugin_id"])
                continue
            todo.append((r["plugin_id"], r["name"], [tuple(x) for x in inst]))
        print("待补系统级可用标记:", len(todo))
        for pid, nm, inst in todo:
            print("  +", pid, "|", nm, "| 现有安装记录:", inst or "（无）")
        if dry or not todo:
            print("（--dry 或无需变更，未写库）")
            return 0
        for pid, _nm, inst in todo:
            # 已有同 user_id=0 但 enabled=0 的 ⇒ 改成 1（尊重"曾存在过"）；否则新增
            has0 = any(u == 0 for u, _e in inst)
            if has0:
                conn.execute("UPDATE plugin_installs SET enabled=1 "
                             "WHERE plugin_id=? AND user_id=0", (pid,))
            else:
                conn.execute(
                    "INSERT INTO plugin_installs (plugin_id, user_id, enabled, installed_at) "
                    "VALUES (?,0,1,datetime('now'))", (pid,))
        conn.commit()
        print("写入完成。校验：")
        for pid, _nm, _i in todo:
            r = conn.execute("SELECT user_id, enabled FROM plugin_installs WHERE plugin_id=?",
                             (pid,)).fetchall()
            print("  ", pid, "->", [tuple(x) for x in r])
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
