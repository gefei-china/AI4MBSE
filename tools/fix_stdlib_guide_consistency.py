"""修「技能强制要求调用一个已 disabled 的工具」的一致性缺陷（2026-11-07 米爸第 17 轮）。

## 问题（真库实证）
`sysml_stdlib_meta` 在 `tools` 表里是 **status='disabled'**
⇒ `ToolExecutor` 直接返回 `{'ok': False, 'result': '未知工具: sysml_stdlib_meta'}`。

但它在三个地方仍被**强制要求**调用：
1. 技能 `sysml_stdlib_reference_guide`(id=118, published, 绑给 architecture_skeleton /
   view_expansion —— 正是产码两阶段) 的正文：「生成任何属性/类型引用前**先查标准库**」
   + 步骤 1「调 `sysml_stdlib_meta`」；description 也是同一句。
2. `agent/pipeline_parts/v2_constraints.py` 的工具协议第 2 步：
   「sysml_stdlib_meta —— 可选，最多 1~2 次」还教了参数名。

⇒ 每次产码都注入「先查标准库」的指令，模型去调一个不存在的工具
⇒ 白花工具轮次 + 拿到「未知工具」后可能转入臆造成员名。
这正是米爸观察到的「**每次都会查标准库**」的机制来源。

## 为什么工具保持 disabled（不改工具状态）
`sysml_stdlib_tools.py` 文件头与实测记录：该工具占工具调用 **32%（13/41）**，
且 **7 个包声明了却索引到 0 个成员**（假阴性极贵——查了也拿不到事实）。
历史上因此禁用。本次只让"说明书与实物一致"，不动工具状态。

## 修法（与既有处理口径一致）
上一轮对「智源对接agent」的 `sysml_stdlib_meta` 引用也是这么修的：
**如实说明停用 + 指明替代依据**，而不是留着诱导。
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")
SKILL_ID = 118

NEW_CONTENT = """---
name: sysml_stdlib_reference_guide
description: SysML 标准库使用规范：查询工具当前**已停用**，成员事实一律以校验器诊断为准，禁止臆造。
triggers: [标准库, kernel, 标准库查询, 元类]
category: AI建模
author: MBSE建模节点体系(V3-V10)
version: v2.0
---
## 重要变更（2026-11-07）
标准库查询工具 `sysml_stdlib_meta` **当前已停用**（实测占工具调用 32%、
且 7 个包索引到 0 个成员，假阴性代价高）。调用它会直接返回「未知工具」。

**因此：不要再尝试调用它。** 需要标准库成员事实时按下面走。

## 正确做法
1. **先写你不确定的部分 → 用校验器验证**：调 `sysml_v2_validate` 拿三路诊断
   （词法/语法/语义），以诊断结果为准修正，而不是先查库。
2. 校验器报「未定义 / 找不到」类语义错时，才说明该成员名确实有问题；
   此时**换用你确定存在的通用元类**（如 `Part` / `Port` / `AttributeValue`），
   或如实说明「该成员未能核实」。
3. 如需引用具体量纲/单位成员（ISQ 系），**优先用模型里已声明过的**；
   确实需要新成员时，按本体扩展流程（`methodology_profile_load` 的本体约束段）处理。

## 硬纪律
- **禁止臆造成员名**。臆造会产生 `An attribute must be typed by attribute definitions`
  之类语义错，且会连锁污染后续视图。
- **查不到就说查不到**：如实说明「未能核实标准库成员」，不要换个"看起来对"的名字硬写。
- **不要为了查库消耗工具轮次**：工具预算应花在「产出代码 → 校验 → 修复」这条主链上。
"""

NEW_V2_STEP = """  2) ~~sysml_stdlib_meta~~ —— **已停用（2026-11-07）**：该工具 status='disabled'，
     调用会返回「未知工具」，**不要再调**。标准库成员事实一律以第 4 步
     `sysml_v2_validate` 的语义诊断为准；拿不到就如实说明「未能核实」，禁止臆造。
"""


def main(apply: bool = False) -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    s = c.execute("SELECT id,name,status,version,length(content) ln FROM skills "
                  "WHERE id=?", (SKILL_ID,)).fetchone()
    print("技能 %s: status=%s version=%s len=%s" %
          (s["id"], s["status"], s["version"], s["ln"]))
    tool = c.execute("SELECT status FROM tools WHERE name='sysml_stdlib_meta'").fetchone()
    print("工具 sysml_stdlib_meta status =", tool["status"] if tool else "(不存在)")
    if not apply:
        print("（预演，未写库）")
        c.close()
        return 0

    # ① 技能正文 + description + version
    c.execute("UPDATE skills SET content=?, description=?, version='v2.0', "
              "updated_at=CURRENT_TIMESTAMP WHERE id=?",
              (NEW_CONTENT,
               "SysML 标准库使用规范：查询工具当前已停用，成员事实以校验器诊断为准，禁止臆造",
               SKILL_ID))
    print("  技能正文已更新（%d → %d 字）" % (s["ln"], len(NEW_CONTENT)))

    # ② v2_constraints.py 的工具协议第 2 步
    p = os.path.join(ROOT, "agent", "pipeline_parts", "v2_constraints.py")
    src = open(p, encoding="utf-8").read()
    old = """  2) sysml_stdlib_meta —— 可选，**最多 1~2 次**，且**参数名只有 name / package**
     · 查包成员用   package="StandardViewDefinitions"    ← 不是 name，也不是 package_name
     · 查单个成员用 name="Part"
     · 查全部包清单 **不传参数**即可；传 name="__packages__" 是错的
"""
    if old in src:
        src = src.replace(old, NEW_V2_STEP, 1)
        open(p, "w", encoding="utf-8").write(src)
        print("  v2_constraints.py 工具协议第 2 步已改为「已停用」")
    else:
        print("  ⚠️ v2_constraints.py 未匹配到原文（可能已被改过），需人工核对")
    c.commit()

    # ③ 校验
    r = c.execute("SELECT version,length(content) ln FROM skills WHERE id=?",
                  (SKILL_ID,)).fetchone()
    print("  终态: version=%s len=%s" % (r["version"], r["ln"]))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
