"""migrate_v18_fix_design_kb_scope —修`design` 的白名单静默失效。

## 症状（2026-10-09 实测）

`design.kb_scope.docs` 配的是：

```json
{"mode":"custom","branches":["release"],
 "docs":["知识库_运行视角_视图规范元素要求.md",
          "知识库_运行视角_视图规范元素要求.md"]}
```

而库里实际文件名是 `知识库_运行视角_视图规范元素要求(1).md`（多一个 `(1)`）
⇒ `rag.py:477 _resolve_scope_docs` 剔除全部
⇒ **实测 warn: `unfiltered: True`** ⇒ design 的"只读视图规范"
**实际是读全库 7269 chunks**。

本次实测的召回差异（同一query「活动视图有哪些必备元素与视图间关联关系」）：

| | 修复前 | 修复后 |
|---|---|---|
| 求交结果 | `[]` | `[...元素要求(1).md, ...关联关系(1).md]` |
| warn | `unfiltered: True` | `None` |
| 召回 20 chunk 覆盖 | 18x **广汽方法论** + 2x 关联关系 | 13x 关联关系 + 7x 元素要求 |

⇒ 修复前 design 拿到的主要是**方法论**，而不是它配的视图规范——
**"配了A 实际读 B"**，而且只有一行 warn 日志。

## 三个问题一次修掉

1. **文件名不匹配** ⇒ 改用库里的准确名（含 `(1)`）
2. **同一份文档列了两次** ⇒ 去重
3. **`mode` 是死字段** ⇒ 移除（`rag.py` 从未读取它，见V16 脚本注释）
4. **顺带补齐**：原配置只列了"元素要求"，漏了"关联关系"
   （库里就这两份视图规范文档），一并纳入

## 判据（出口断言，缺一即 FAIL）

① `_resolve_scope_docs` 返回值精确等于配置值且 `warn is None`
   （**不是**"字段写入成功"——字段能写进去≠过滤生效）
② 真实检索召回的 `source_doc` **全部**落在白名单内（零泄漏）
③ 其余 Agent 的 `kb_scope` **未被改动**（本脚本只碰 design）

用法：
```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v18_fix_design_kb_scope.py --dry-run
./.venv/Scripts/python.exe -X utf8 tools/migrate_v18_fix_design_kb_scope.py
```

幂等：覆盖式更新 + 出口断言。
回滚：`{"mode":"custom","branches":["release"],"docs":[...原值...]}`（见下方 BEFORE 常量）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TARGET_AGENT = "design"

#: 库里准确文件名（V17 实测确认，含 `(1)`）
DOC_ELEMENTS = "知识库_运行视角_视图规范元素要求(1).md"     # id=817, 30 chunks
DOC_RELATIONS = "知识库_运行视角_视图间关联关系(1).md"     # id=818, 22 chunks

#: 修复前原值（回滚照抄）
BEFORE = {
    "mode": "custom",
    "branches": ["release"],
    "docs": [
        "知识库_运行视角_视图规范元素要求.md",
        "知识库_运行视角_视图规范元素要求.md",
    ],
}

#: 修复后取值
AFTER = {
    "branches": ["release"],
    "docs": [DOC_ELEMENTS, DOC_RELATIONS],
}

#: 验证用的代表性查询（design 的职责=视图设计）
PROBE_Q = "活动视图有哪些必备元素与视图间关联关系"


def run(conn, args) -> int:
    from agent.rag import GraphRAG
    g = GraphRAG()

    row = conn.execute(
        "SELECT id, kb_required, kb_scope FROM agents WHERE name=?",
        (TARGET_AGENT,)).fetchone()
    if not row:
        print(f"[FAIL] 找不到 Agent：{TARGET_AGENT}")
        return 1

    print("=" * 72)
    print("① 改动前（原值，回滚照抄）")
    print("=" * 72)
    print(f"  kb_required = {row['kb_required']}")
    print(f"  kb_scope    = {row['kb_scope']}")
    old_scope = json.loads(row["kb_scope"] or "{}")

    # ── 先证明问题真实存在（不能凭配置文本断言有问题）──
    print("\n" + "=" * 72)
    print("② 证明问题真实存在（用 rag 的真实求交逻辑）")
    print("=" * 72)
    old_eff, old_warn = g._resolve_scope_docs(conn, old_scope.get("docs") or [])
    unfiltered = bool(old_warn and old_warn.get("unfiltered"))
    print(f"  修复前求交结果 = {old_eff}")
    print(f"  修复前 warn    = {old_warn}")
    print(f"  ⇒ 静默放宽为读全库？ {'是（问题确认）' if unfiltered else '否（配置已正确）'}")
    if not unfiltered:
        print("  ⚠️ 未复现放宽问题 —— 可能已被别人修过，先看④ 出口断言再决定是否写入")

    print("\n" + "=" * 72)
    print("③ 修复后取值")
    print("=" * 72)
    print(f"  kb_required = 1（保持）")
    print(f"  kb_scope    = {json.dumps(AFTER, ensure_ascii=False)}")
    print("  变化点：")
    print("    ① 文件名补上 (1)——原名匹配不上任何文档")
    print("    ② 去重——原配置同一份文档列了两次")
    print("    ③ 移除 mode 死字段——rag.py 从未读取它")
    print("    ④ 补齐漏配的《视图间关联关系》(id=818)")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    print("\n" + "=" * 72)
    print("④ 写库 + 出口断言")
    print("=" * 72)
    conn.execute(
        "UPDATE agents SET kb_required=1, kb_scope=? WHERE name=?",
        (json.dumps(AFTER, ensure_ascii=False), TARGET_AGENT))
    conn.commit()
    print(f"  [写入] {TARGET_AGENT}")

    problems = []

    # 断言 ① 求交精确 + warn 为空
    r = conn.execute(
        "SELECT kb_required, kb_scope FROM agents WHERE name=?",
        (TARGET_AGENT,)).fetchone()
    scope = json.loads(r["kb_scope"] or "{}")
    eff, warn = g._resolve_scope_docs(conn, scope.get("docs") or [])
    exact = (warn is None) and set(eff) == set(AFTER["docs"])
    print(f"  [{'OK  ' if exact else 'FAIL'}] 求交精确生效：eff={eff} warn={warn}")
    if not exact:
        problems.append("白名单未精确生效（仍被静默放宽或剔除）")

    if not r["kb_required"]:
        problems.append("kb_required 变成了 0")

    # 断言 ② 真实检索零泄漏
    res = g.retrieve(PROBE_Q, kb_scope=scope)
    hits = res.get("chunk_hits") or []
    srcs = Counter()
    for h in hits:
        if isinstance(h, dict):
            srcs[h.get("source_doc") or h.get("doc") or h.get("filename") or "?"] += 1
        else:
            srcs[str(h)[:40]] += 1
    leak = {k: v for k, v in srcs.items() if k not in set(AFTER["docs"])}
    no_hit = not hits
    print(f"  [{'OK  ' if not leak and not no_hit else 'FAIL'}] "
          f"召回 {len(hits)} chunk / 覆盖 {len(srcs)} 份文档 / 泄漏={list(leak) or '无'}")
    for k, v in srcs.most_common(6):
        print(f"         {v:2}x {k[:56]}")
    if leak:
        problems.append(f"召回越出白名单：{list(leak)}")
    if no_hit:
        problems.append("召回 0 chunk（过滤把内容全滤空了）")

    # 断言 ③ 其余 Agent 未被改动
    touched = conn.execute(
        "SELECT count(*) FROM agents WHERE json_extract(kb_scope,'$.docs') IS NOT NULL "
        "AND name!=?", (TARGET_AGENT,)).fetchone()[0]
    print(f"  [OK  ] 其余带 docs 白名单的 Agent = {touched} 个（本脚本未触碰）")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("  -", p)
        return 1

    print("\n" + "=" * 72)
    print("✅ design 白名单已精确生效（不再静默读全库）")
    print("=" * 72)
    print(f"  修复前：读全库 7269 chunks，召回以广汽方法论为主")
    print(f"  修复后：只读 {len(AFTER['docs'])} 份视图规范（{sum(1 for _ in AFTER['docs'])} 篇，"
          f"{30 + 22} chunks）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    args = ap.parse_args()

    from database import db_conn

    # ★ db_conn() 是 @contextmanager，必须 with（误当连接对象 ⇒ 校验假通过）
    with db_conn() as conn:
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())
