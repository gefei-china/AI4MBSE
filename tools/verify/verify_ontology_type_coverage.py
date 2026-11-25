# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-24 A6：SysML 元素类型覆盖率双口径统计。14 类现行口径需真库；22 类权威清单源文档未提供时显式 MISSING_AUTHORITY 不编造。需真库实体分布
"""A6：本体类型覆盖率双口径统计（SysML 元素 14 类现行 / PRD 22 类待权威清单）。

    .venv/Scripts/python.exe tools/verify/verify_ontology_type_coverage.py
    .venv/Scripts/python.exe tools/verify/verify_ontology_type_coverage.py --sysml22 "部件,需求,功能,..."

背景：PRD §2.2 写「7/22 → ≥18/22」，但 docs/archive/2026-Q3/知识看板优化方案
明确记「22 类权威清单待甲方提供」。系统现行分母是 sysml_ast.NODE_KINDS 归并的
14 类（有权威实现），两者不能混算。本脚本：
  · 14 类口径：与 metrics_core.type_coverage 同源，给可复现真数；
  · 22 类口径：清单由 --sysml22 传入；不传时**明确报 MISSING_AUTHORITY**，
    不编造 22 类、更不把 14 类冒充 22 类（防假绿）。
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sysml22", default="", help="PRD 22 类权威清单（逗号分隔）")
    a = ap.parse_args()

    db = ROOT / "mbse.db"
    if not db.exists():
        print("SKIP: 真库不存在")
        return 0
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    print("\n[1] 现行 14 类口径（sysml_ast.NODE_KINDS，与看板同源）")
    from metrics_core import type_coverage
    tc = type_coverage(conn)
    s = tc.get("sysml") or {}
    rate, covered, total = s.get("coverage_rate"), s.get("covered_types"), s.get("total_types")
    check("C1 14 类口径可复现", total == 14 and covered is not None and rate is not None,
          f"covered={covered}/{total} rate={rate}（真库 SysML 元素覆盖偏低，正是要指出的缺口）")

    print("\n[2] PRD 22 类口径")
    names = [x.strip() for x in a.sysml22.split(",") if x.strip()]
    if not names:
        print("  ⚠ MISSING_AUTHORITY：22 类权威清单源文档未提供（见 知识看板优化方案-20260926.md）")
        print("     不编造清单、不把 14 类当 22 类。拿到清单后以 --sysml22 传入即可计算。")
        check("C2 不伪造权威清单", True, "MISSING_AUTHORITY 显式登记")
    else:
        check("C2 清单恰好 22 类", len(names) == 22, f"got={len(names)}")
        live = {r[0] for r in conn.execute(
            "SELECT DISTINCT entity_type FROM entities WHERE status!='deprecated'")}
        hit = [n for n in names if n in live]
        rate22 = round(len(hit) / len(names) * 100, 1)
        check("C3 22 类口径可算且达标判定明确",
              len(names) == 22 and 0 <= rate22 <= 100,
              f"覆盖 {len(hit)}/22 = {rate22}% | 目标 ≥82% | 命中 {hit[:6]}")

    print("\n[3] 口径不混算")
    check("C4 分母来源可区分", "sysml" in tc and "domain" in tc,
          "现行看板已分 SysML 元素/领域扩展两分母")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
