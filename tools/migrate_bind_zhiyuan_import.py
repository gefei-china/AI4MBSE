# -*- coding: utf-8 -*-
"""把「智源直连写入」三件套绑给「智源对接agent」（2026-10-28）。

## 为什么必须做（conv 9736 真机实证）

用户要求「生成时序图 → 校验 → 写入包 `SD_通信子系统时序图`」，会话以
「连续 3 次工具失败」收尾，**一次写操作都没发生**。根因不是模型不聪明：

| 事实 | 数据 |
|---|---|
| 模型走了哪条路 | `create_model_elements` 逐元素建（要求 5 个 part + 8 个 item + 13 条 message + view） |
| 它需要的枚举 | `get_modeling_capabilities` 返回 **47,229 字符**、`hasMore=true`、`nextOffset=100`、`creatable=50 种` |
| 我们给了它什么 | 回执被 offload 成引用块、模型可见上限 8000 字符 ⇒ **它看不到合法类型清单** |
| 结果 | 3 次猜 `SEQUENCE_VIEW`（能力清单里**根本没有**这个类型）→ `INVALID_ARGUMENTS` ×3 → 失败轮上限 → 整轮强制收尾 |
| 本来有什么 | `zhiyuan_sysmlv2_import(vc="branchId,0", text=<整段源码>, targetPackageDataId=包id)` —— **一次调用写完整个包**，平台自己解析建元素 |

⇒ 最短路径早就存在，但 agent_tools 里**没有绑定它**（实测绑定 10 个内置工具，
不含 gen/check/import）。把最难的路留给模型、把最短的路藏起来，
再在它猜错参数时直接判死 —— 这不是"模型差距"，是**工具集与任务不匹配**。

## 安全性

不新增裸写通道：`zhiyuan_sysmlv2_import` 早已被 `tool_hooks`（id=3，名「智源覆盖导入需确认」）
登记为 `require_confirm`，绑定它只是让它**出现在候选清单**里，
真正执行仍要人批。

## 运行

    python tools/migrate_bind_zhiyuan_import.py          # 应用
    python tools/migrate_bind_zhiyuan_import.py --dry    # 只看差异
"""
from __future__ import annotations

import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, "mbse.db")
AGENT = "智源对接agent"
TOOLS = [
    ("zhiyuan_sysmlv2_gen", "从工程数据生成 SysML v2 源码（读）"),
    ("zhiyuan_sysmlv2_check", "平台侧语法检测（读）"),
    ("zhiyuan_sysmlv2_import", "覆盖导入 SysML v2 文本到平台（写，走人工确认）"),
]


def main() -> int:
    dry = "--dry" in sys.argv
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("select id from agents where name=?", (AGENT,)).fetchone()
        if not row:
            print("✗ 找不到 Agent:", AGENT)
            return 1
        aid = row[0]
        have = {r[0] for r in conn.execute(
            "select tool_name from agent_tools where agent_id=? and tool_type='tool'", (aid,))}
        exist = {r[0] for r in conn.execute("select name from tools")}
        add, skip_exist, missing = [], [], []
        for name, why in TOOLS:
            if name not in exist:
                missing.append(name)
            elif name in have:
                skip_exist.append(name)
            else:
                add.append((name, why))
        print("Agent:", AGENT, "(id=%s)" % aid)
        print("已有绑定:", sorted(have))
        print("本次新增:", [a[0] for a in add] or "（无）")
        if skip_exist:
            print("已绑定跳过:", skip_exist)
        if missing:
            print("⚠️ tools 表无此工具:", missing)
        if dry or not add:
            print("（--dry 或无需变更，未写库）")
            return 0
        for name, _why in add:
            conn.execute(
                "insert into agent_tools (agent_id, tool_type, tool_name, enabled, created_at) "
                "values (?,?,?,1,datetime('now'))", (aid, "tool", name))
        conn.commit()
        after = sorted(r[0] for r in conn.execute(
            "select tool_name from agent_tools where agent_id=? and tool_type='tool'", (aid,)))
        print("写入完成，现绑定:", after)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
