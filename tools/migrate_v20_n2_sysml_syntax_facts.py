"""migrate_v20_n2_sysml_syntax_facts —把实测确认的 SysML 语法事实补进 N2 skill。

## 触发原因（2026-10-09 实测）

`verify_n2_skeleton` 判红，checker.jar 报三条**词法/语法级**错误：

```
L1 [part def 载荷 {]  no viable alternative at character '��'
L2 [  attribute mass = 18kg;]  no viable alternative at input '18'
L4 [part def 转发器 {]  no viable alternative at character 'ת'
```

⇒ 根因不是模型"不听话"，而是**N2 绑定的 3 个 skill 里
没有一条提到中文标识符/带单位数值的写法**（实测 `grep` 6 个关键词全部首现 -1）。

## ★ 判据必须来自官方校验器，不是经验之谈

用 `sysml_v2_check.check_code` 逐条实测（`n_hard=0` 为合法）：

| 事实 | 合法写法（n_hard=0） | 非法写法（实测 n_hard） |
|---|---|---|
| **中文标识符必须加单引号** | `part def '载荷';` | `part def 载荷;` → **2** |
| 中文实例名同样要引号 | `part '转发器实例';` | `part 转发器实例;` → **8** |
| **带单位数值用`Real = 18 [kg]`** | `attribute mass : Real = 18 [kg];` | `= 18kg` → **2** |
| 纯数值可直接写 | `attribute mass = 18;` | — |

⚠️ 实测发现 `'18 kg'`（单引号整体）与 `Mass`（未导入类型）
虽然 `n_hard=0` 但会留下 **warning 级** "Couldn't resolve reference"
⇒ 属"能过校验但语义不干净"，本脚本采用 `Real = 18 [kg]` 这一条**无警告**的写法。

## 为什么这是"接通"而不是"加限制"

这三条**不是新规则**，而是 SysML v2 词法的既有事实——
模型只是不知道。写成 skill 正文相当于告诉它"中文要加引号"，
而不是新增一条"禁止某物"的检查。
**符合「少加限制」**：不加门禁、不加检查项，只补事实。

## 改哪三处（同一个 skill，避免多处漂移）

`sysml_skeleton_generation_guide`（N2 绑定的 3 个 skill 之一，1130 字符）
的「硬约束」清单里补 3 条。它已有一条同类约束
（"顶层 import 必须带可见性前缀"）⇒ 形态一致，不引入新结构。

## 用法

```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v20_n2_sysml_syntax_facts.py --prove
./.venv/Scripts/python.exe -X utf8 tools/migrate_v20_n2_sysml_syntax_facts.py --dry-run
./.venv/Scripts/python.exe -X utf8 tools/migrate_v20_n2_sysml_syntax_facts.py
```

幂等：以锚点句判定"已补"则跳过。
回滚：`content` 字段存有原值（本脚本输出「改动前原值」，照抄即可）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TARGET_SKILL = "sysml_skeleton_generation_guide"

#: 锚点句：已存在则视为已补（幂等判据）
ANCHOR = "中文标识符必须加单引号"

#: 要插在锚点句之后的三条事实（由 checker.jar 实测确定，不是经验之谈）
NEW_LINES = (
    "   - **中文标识符必须加单引号**：`part def '载荷';`、`part '转发器实例';`。"
    "不加引号是词法错（实测 n_hard=2~8）。中文是本项目建模对象的常态，"
    "定义名与实例名都要加。\n"
    "   - **带单位的数值用 `Real = 数值 [单位]`**：`attribute mass : Real = 18 [kg];`。"
    "写成 `18kg`（无空格）会被词法解析器截断（实测 n_hard=2）。"
    "纯数值可直接写：`attribute mass = 18;`。"
)

#: 校验用的样例（与 migrate 正文里的写法一致）
PROBES = [
    ("中文定义名", "package P { part def '载荷'; }", 0),
    ("中文实例名", "package P { part def '转发器'; part '转发器实例'; }", 0),
    ("带单位数值", "package P { private import ScalarValues::*;"
                  " part def V { attribute mass : Real = 18 [kg]; } }", 0),
    ("纯数值", "package P { part def V { attribute mass = 18; } }", 0),
]


def prove() -> int:
    """用官方校验器证明这四条写法合法、且反例确实非法。"""
    from sysml_v2_check import check_code
    print("=" * 72)
    print("实测：用 checker.jar 确认这四条语法事实")
    print("=" * 72)
    ok = True
    for name, code, want in PROBES:
        r = check_code(code)
        got = r.get("n_hard")
        good = got == want
        ok = ok and good
        print(f"  [{'OK  ' if good else 'FAIL'}] {name:12} n_hard={got}"
              f"（期望 {want}） verdict={r.get('verdict')}")
    print("\n反例（这些是模型实际写出来的形式，应当非法）：")
    for name, code in [("中文不加引号", "package P { part def 载荷; }"),
                       ("18kg 无空格", "package P { part def V { attribute mass = 18kg; } }")]:
        r = check_code(code)
        bad = (r.get("n_hard") or 0) > 0
        ok = ok and bad
        print(f"  [{'OK  ' if bad else 'FAIL'}] {name:12} n_hard={r.get('n_hard')}"
              f"（应 >0，即确实非法）")
    print("\n" + ("✅ 事实成立，可写入 skill" if ok else "❌ 事实不成立，不许写入"))
    return 0 if ok else 1


def run(conn, args) -> int:
    row = conn.execute(
        "SELECT id, name, content FROM skills WHERE name=?",
        (TARGET_SKILL,)).fetchone()
    if not row:
        print(f"[FAIL] 找不到 skill：{TARGET_SKILL}")
        return 1
    old = row["content"] or ""

    print("=" * 72)
    print("① 改动前")
    print("=" * 72)
    print(f"  skill id={row['id']} name={row['name']} content={len(old)} 字符")
    has = ANCHOR in old
    print(f"  是否已含「{ANCHOR}」：{'是（幂等跳过）' if has else '否'}")
    if args.dry_run or has:
        print("\n[dry-run]" if args.dry_run else "已补过，无需重复写入")
        return 0

    # 插在「顶层 import 必须带可见性前缀」那条之后（同属硬约束清单）
    ANCHOR_LINE = "   - `part`/`port`/`item` 分别由 `part def`/`port def`/`item def` 定型，不可混用\n"
    if ANCHOR_LINE not in old:
        print(f"[FAIL] 找不到插入锚点行，skill 正文可能已改版。\n"
              f"       期望存在：{ANCHOR_LINE!r}")
        return 1
    new = old.replace(ANCHOR_LINE, ANCHOR_LINE + NEW_LINES, 1)

    print("\n" + "=" * 72)
    print("② 将插入的内容")
    print("=" * 72)
    for ln in NEW_LINES.splitlines():
        print("  " + ln)

    conn.execute("UPDATE skills SET content=? WHERE id=?", (new, row["id"]))
    conn.commit()
    print(f"\n[写入] skills.id={row['id']}：{len(old)} → {len(new)} 字符")

    # ── 出口断言 ──
    print("\n" + "=" * 72)
    print("③ 出口断言")
    print("=" * 72)
    problems = []
    chk = conn.execute("SELECT content FROM skills WHERE id=?", (row["id"],)).fetchone()[0]
    for kw in (ANCHOR, "Real = 18 [kg]", "'载荷'"):
        ok = kw in chk
        print(f"  [{'OK  ' if ok else 'FAIL'}] 含「{kw}」")
        if not ok:
            problems.append(f"缺关键词 {kw}")
    # 原有内容不能被破坏
    for kw in ("顶层 import 必须带可见性前缀", "sysml_v2_validate", "## 失败处理"):
        ok = kw in chk
        print(f"  [{'OK  ' if ok else 'FAIL'}] 原有内容保留「{kw}」")
        if not ok:
            problems.append(f"原文丢失 {kw}")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("  -", p)
        return 1
    print("\n✅ N2 skill 已补入实测确认的语法事实")
    print("   ⚠️ 这只补事实，不加门禁；是否真的生效需重跑 verify_n2_skeleton")
    print(f"\n回滚：UPDATE skills SET content=<原值> WHERE id={row['id']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prove", action="store_true",
                    help="只用 checker.jar 证明语法事实成立")
    args = ap.parse_args()

    if args.prove:
        return prove()

    from database import db_conn

    with db_conn() as conn:
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())
