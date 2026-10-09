"""migrate_v23_dehardcode_skill_rounds —把 skill 正文里硬编码的「轮次 3」改成相对表述。

## 触发原因（2026-10-09 实测）

上一轮把 `max_tool_rounds` 从 3 提到 6（通用修复，见 execute.py 同位置注释），
但 **8 个视图 skill + 骨架 skill 的正文里写着按旧值 3 写的规划**：

```
**本视图的代码请在第 1 轮就产出**（工具调用轮次上限为 3，耗尽即中断）
- 轮次预算分配：第 1 轮 = 查标准库（可选）+ 产出全部视图代码；第 2 轮 = validate；第 3 轮 = 按诊断修复。
- **不要逐个成员反复查**（实测：连查 6 次会耗尽轮次，导致一行代码都没产出）。
```

⇒ 模型按 3 轮规划，但实际给了 6 轮
⇒ 第 4~6 轮才想产出时它已"越过"正文描述的节奏 ⇒ 行为错乱。
实测后果：**骨架只产出 448 字符的探针代码**（`port def 'A接口'` 那种对照实验），
**activity/ibd 产出雷同**。

## 修法（不写死数字 = 单一真源）

★ **运行时真值由 `skills.py` 注入**（本轮新增）：
```
工具轮次预算（运行时真值，共 N 轮）：请在前 2 轮内产出代码，剩余轮次用于 validate 与修复。
```
⇒ 配置改了，注入值自动跟着变，**skill 正文不再需要知道数字**。

⇒ 本脚本把正文里的具体数字改成**相对表述**（"前几轮""剩余轮次"），
   消除"配置与文档漂移"这个根因。

## 判据（出口断言）

① 8 个视图 skill + 骨架 skill 正文里**不再出现**「轮次上限为 3」「第 3 轮」这类硬编码
② 改写后仍保留"尽早产出"的**纪律本身**（不能把指引一起删掉）
③ 运行时注入存在（`skills.py` 含 `max_tool_rounds` 的注入行）
④ 注入的提示里**不含**任何硬编码轮次数字

用法：
```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v23_dehardcode_skill_rounds.py --dry-run
./.venv/Scripts/python.exe -X utf8 tools/migrate_v23_dehardcode_skill_rounds.py
```
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

VIEW_SKILLS = [
    "sysml_view_generation_requirement", "sysml_view_generation_structure",
    "sysml_view_generation_usecase", "sysml_view_generation_activity",
    "sysml_view_generation_ibd", "sysml_view_generation_sequence",
    "sysml_view_generation_state", "sysml_view_generation_parameter",
    "sysml_skeleton_generation_guide",
]

#: 旧值 → 相对表述（保留纪律，去掉具体数字）
REWRITES = [
    # activity / structure 等视图 skill 里的通用句式
    ("**本视图的代码请在第 1 轮就产出**（工具调用轮次上限为 3，耗尽即中断）：",
     "**本视图的代码请尽早产出**（工具调用轮次有限，耗尽即中断；"
     "实际轮数以运行时注入的「工具轮次预算」为准）："),
    ("- **不要逐个成员反复查**（实测：连查 6 次会耗尽轮次，导致一行代码都没产出）。",
     "- **不要逐个成员反复查**（实测：反复查会耗尽轮次，导致一行代码都没产出）。"),
    ("   - 轮次预算分配：第 1 轮 = 查标准库（可选）+ 产出全部视图代码；"
     "第 2 轮 = validate；第 3 轮 = 按诊断修复。",
     "   - 轮次预算分配：**先查标准库（可选）并立即产出全部视图代码**，"
     "再 validate，最后按诊断修复。（具体轮次数见运行时注入的预算）"),
    # 骨架 skill
    ("   **轮次上限 3**，超出即上报人工。",
     "   **轮次有限**，超出即上报人工（实际轮次数见运行时注入的预算）。"),
]

#: 判红用的模式（正文里不该再出现这些）
FORBIDDEN = [
    r"轮次上限为\s*\d+",
    r"轮次上限\s*\d+",
    r"第\s*3\s*轮\s*=",
    r"第\s*[0-9]\s*轮\s*=\s*第",
]


def run(conn, args) -> int:
    print("=" * 72)
    print("① 改动前：各skill 的硬编码轮次表述")
    print("=" * 72)
    rows = {}
    for nm in VIEW_SKILLS:
        r = conn.execute("SELECT id, content FROM skills WHERE name=?",
                         (nm,)).fetchone()
        if not r:
            print(f"  [SKIP] {nm} 不存在")
            continue
        rows[nm] = dict(r)
        hits = [m.group(0) for m in
                re.finditer(r".{0,20}轮次.{0,30}", r["content"] or "")]
        print(f"  {nm:36} {len(hits)} 处")
        for h in hits[:2]:
            print(f"      {h.strip()[:72]}")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    print("\n" + "=" * 72)
    print("② 改写")
    print("=" * 72)
    changed = 0
    for nm, r in rows.items():
        body = r["content"] or ""
        new = body
        for old, rep in REWRITES:
            if old in new:
                new = new.replace(old, rep)
        if new != body:
            conn.execute("UPDATE skills SET content=? WHERE id=?",
                         (new, r["id"]))
            changed += 1
            print(f"  [改写] {nm}（{len(body)} → {len(new)}）")
    conn.commit()
    print(f"  共改写 {changed} 个 skill")

    print("\n" + "=" * 72)
    print("③ 出口断言")
    print("=" * 72)
    problems = []
    # ① 正文不再有硬编码轮次
    for nm in rows:
        body = conn.execute("SELECT content FROM skills WHERE name=?",
                            (nm,)).fetchone()[0] or ""
        bad = [p for p in FORBIDDEN if re.search(p, body)]
        if bad:
            problems.append(f"{nm}: 仍有硬编码轮次 {bad}")
            print(f"  [FAIL] {nm:36} 残留={bad}")
    print(f"  [{'OK  ' if not any('仍有硬编码' in p for p in problems) else 'FAIL'}]"
          f" 9 个 skill 正文均无硬编码轮次数字")

    # ② 纪律本身还在（不能把指引删掉）
    for nm in rows:
        body = conn.execute("SELECT content FROM skills WHERE name=?",
                            (nm,)).fetchone()[0] or ""
        if "轮次" not in body:
            problems.append(f"{nm}: 轮次纪律被误删")
    print(f"  [{'OK  ' if not any('纪律被误删' in p for p in problems) else 'FAIL'}]"
          f" 轮次纪律表述仍在（未误删指引）")

    # ③ 运行时注入存在
    src = open(os.path.join(ROOT, "agent", "pipeline_parts", "skills.py"),
               encoding="utf-8").read()
    has_inject = "max_tool_rounds" in src and "工具轮次预算（运行时真值" in src
    print(f"  [{'OK  ' if has_inject else 'FAIL'}] skills.py 含运行时注入"
          f"（单一真源）")
    if not has_inject:
        problems.append("skills.py 缺少运行时轮次注入")

    # ④ 注入提示里不含硬编码数字
    m = re.search(r"工具轮次预算（运行时真值[^）]*）", src)
    inj = m.group(0) if m else ""
    clean = not re.search(r"\d+\s*轮", inj)
    print(f"  [{'OK  ' if clean else 'FAIL'}] 注入文案无硬编码轮次：{inj[:50]}")
    if not clean:
        problems.append("注入文案含硬编码轮次")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("  -", p)
        return 1
    print("\n✅ skill 正文不再硬编码轮次，预算真值由运行时注入")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from database import db_conn
    with db_conn() as conn:
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())
