# -*- coding: utf-8 -*-
"""P1-2 回填：关系类型的 OWL 2 公理特征（constraints.characteristics）。

## 为什么需要（2026-11-15 审计实测）

真库 13 个关系类型里只有 **2 个**声明了 characteristics（ALLOCATED_TO / TRACE）。
后果：
  · `ontology_reasoning` 的**传递闭包**推断几乎不触发（它优先读本体公理，
    本体没标记就回退到硬编码的 TRANSITIVE_RELATIONS 兜底）⇒ 「A 包含 B、B 包含 C
    ⇒ A 包含 C」这类**本该自动推出**的结论，系统推不出来；
  · OWL 导出缺 `owl:TransitiveProperty` / `owl:SymmetricProperty` 声明
    ⇒ 下游 Protégé / Pellet 也推不出，等于把本体降级成"类名清单"。

## 只声明**语义无歧义**的，宁可少不要错

逐条判断（这是设计判断，不是脚本能推的；故写死在表里并给出理由）：

| 关系 | 声明 | 理由 |
|---|---|---|
| CONTAINS | transitive | 部分-整体传递：A 含 B、B 含 C ⇒ A 含 C（BFO 的 has_part 同理） |
| COMPOSED_OF | transitive | 同上（组成关系） |
| DERIVES | transitive | 需求派生链：系统需求→子系统需求→单元需求，可逐级传递 |
| CONNECTS | symmetric | 连接无方向：A 连 B ⇔ B 连 A（真库当前 0 条边，无存量冲突） |

**刻意不声明**的关系及原因：
  · `SATISFIES` / `VERIFIED_BY` / `REALIZES` —— 跨范畴映射（部件→需求、需求→用例），
    传递性无意义（需求不会被"满足"）且极易推出荒谬结论；
  · `ALLOCATED_TO` / `TRACE` —— 已有声明，不动；
  · `DEPENDS_ON` / `FLOW_TO` —— 传递性在工程上**有争议**（依赖链传递会放大影响面分析），
    无明确业务裁决前不声明；
  · `CONNECTS` 不声明 asymmetric（与 symmetric 互斥，会触发 char_conflict）。

## 用法

    python tools/backfill_ontology_axioms.py            # 预览
    python tools/backfill_ontology_axioms.py --apply    # 写入

## 纪律

1. **幂等**：只**追加**缺失的特性，绝不删已有声明（`--replace` 才整列替换）。
2. **写前查互斥**：symmetric + asymmetric / reflexive + asymmetric 同时出现会被
   `core.ontology_rules.char_conflict` 判为 warn ⇒ 脚本先自检，冲突就当场报错退出。
3. **写完回读**（新连接）。
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

DB = os.path.join(_ROOT, "mbse.db")

#: 关系名 → 要补充的特性（只补不删）
AXIOMS = {
    "CONTAINS":    ["transitive"],
    "COMPOSED_OF": ["transitive"],
    "DERIVES":     ["transitive"],
    "CONNECTS":    ["symmetric"],
}

#: 互斥组合（与 core.ontology_rules.char_conflict 同口径）
_MUTEX = [({"symmetric", "asymmetric"}, "对称与反对称互斥"),
          ({"reflexive", "asymmetric"}, "自反与反对称互斥")]

_VALID = {"transitive", "symmetric", "asymmetric", "reflexive",
          "functional", "inverse_functional"}


def _selfcheck(chars):
    """脚本自身的互斥自检（不依赖后端 400 才发现）。"""
    s = set(chars or [])
    for pair, why in _MUTEX:
        if pair <= s:
            return f"{why}：{sorted(pair)}"
    bad = sorted(s - _VALID)
    if bad:
        return f"非法特性 {bad}（允许: {sorted(_VALID)}）"
    return ""


def run(apply: bool = False, replace: bool = False):
    con = sqlite3.connect(DB, timeout=45)
    con.row_factory = sqlite3.Row
    print("=" * 78)
    print("关系公理特征回填（constraints.characteristics）")
    print("模式：%s" % ("APPLY（写库）" if apply else "DRY-RUN（只预览）"))
    print("=" * 78)

    rels = con.execute(
        "SELECT id, name, constraints FROM ontology_types WHERE type_kind='relation'"
    ).fetchall()
    print("\n[现状] relation 行 %d 条" % len(rels))

    changes, errs = [], []
    for r in rels:
        nm = r["name"]
        try:
            cons = json.loads(r["constraints"] or "{}")
        except Exception:
            cons = {}
        if not isinstance(cons, dict):
            cons = {}
        old = list(cons.get("characteristics") or [])
        if nm not in AXIOMS:
            if old:
                print("  [KEEP] %-14s 已有 %s（不在计划内，保持）" % (nm, old))
            continue
        new = list(AXIOMS[nm]) if replace else (old + [c for c in AXIOMS[nm] if c not in old])
        if new == old:
            print("  [SKIP] %-14s 已是 %s" % (nm, old))
            continue
        e = _selfcheck(new)
        if e:
            errs.append(f"{nm}: {e}")
            print("  [ERR ] %-14s %s" % (nm, e))
            continue
        print("  [PLAN] %-14s %s → %s" % (nm, old, new))
        cons2 = dict(cons)
        cons2["characteristics"] = new
        changes.append((nm, r["id"], json.dumps(cons2, ensure_ascii=False)))

    if errs:
        print("\n结果：FAIL —— 存在互斥/非法声明，未写库")
        con.close()
        return 1
    if not apply:
        print("\n结果：DRY-RUN，计划变更 %d 条（未写库）" % len(changes))
        con.close()
        return 0

    for nm, tid, payload in changes:
        con.execute("UPDATE ontology_types SET constraints=? WHERE id=?", (payload, tid))
    con.commit()
    con.close()

    # 新连接回读（旧连接是快照）
    con2 = sqlite3.connect(DB, timeout=45)
    con2.row_factory = sqlite3.Row
    print("\n[回读自证]（新连接）")
    ok = 0
    for r in con2.execute(
            "SELECT name, constraints FROM ontology_types WHERE type_kind='relation'"):
        c = json.loads(r["constraints"] or "{}")
        ch = c.get("characteristics") or []
        if ch:
            ok += 1
            print("  [OK ] %-14s %s" % (r["name"], ch))
    con2.close()
    print("\n结果：写入 %d 条；有公理声明的关系 %d 个" % (len(changes), ok))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--replace", action="store_true", help="整列替换（默认只追加）")
    a = ap.parse_args()
    return run(apply=a.apply, replace=a.replace)


if __name__ == "__main__":
    sys.exit(main())
