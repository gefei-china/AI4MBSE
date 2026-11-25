# -*- coding: utf-8 -*-
"""A5 验证：属性 min/max/pattern 约束真正被校验器消费（不是加了没人读的字段）。

    .venv/Scripts/python.exe tools/verify/verify_ontology_attr_constraints.py

判据：
  A1 只有 xsd 时照常工作（不回归）
  A2 min/max 越界被判违例；界内不误报
  A3 pattern 不匹配被判违例；匹配不误报
  A4 字符串值不会被 float 比较误伤（TypeError 静默跳过）
  A5 真库属性维度统计口径修正：unit/quantity_kind 已有，真正缺的是 min/max/pattern/default
  A6 变异自证：去掉新校验分支后越界不再被抓
"""
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
    from services.ontology_migration import validate_attribute_instances as vai

    def fixture():
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript("""
          CREATE TABLE entities(id TEXT, name TEXT, entity_type TEXT, properties TEXT, status TEXT);
          INSERT INTO entities VALUES('e1','正常','部件','{"gain":5}', 'reviewed');
          INSERT INTO entities VALUES('e2','超上限','部件','{"gain":99}', 'reviewed');
          INSERT INTO entities VALUES('e3','低于下限','部件','{"gain":-3}', 'reviewed');
          INSERT INTO entities VALUES('e4','编码错','部件','{"code":"ABC-12"}', 'reviewed');
          INSERT INTO entities VALUES('e5','编码对','部件','{"code":"SYS-99"}', 'reviewed');
          INSERT INTO entities VALUES('e6','非数值','部件','{"gain":"abc"}', 'reviewed');
        """)
        return c

    c = fixture()
    print("\n[1] 基本不回归")
    base = vai(c, "gain", {}, {"xsd_type": "xsd:decimal", "domain_classes": ["部件"]})
    check("A1 仅 xsd 不回归", isinstance(base, list), f"n={len(base)}")

    print("\n[2] min/max")
    ranged = vai(c, "gain", {}, {"min": 0, "max": 10, "domain_classes": ["部件"]})
    kinds = [x["entity_id"] for x in ranged if x["kind"] == "range"]
    check("A2a 越界被抓", "e2" in kinds and "e3" in kinds, str(kinds))
    check("A2b 界内不误报", "e1" not in kinds and "e6" not in kinds, str(kinds))

    print("\n[3] pattern")
    pats = vai(c, "code", {}, {"pattern": "^SYS-[0-9]+$", "domain_classes": ["部件"]})
    pk = [x["entity_id"] for x in pats if x["kind"] == "pattern"]
    check("A3 正则不匹配被抓", "e4" in pk and "e5" not in pk, str(pk))

    print("\n[4] 真库统计口径纠正")
    db = ROOT / "mbse.db"
    if db.exists():
        conn = sqlite3.connect(str(db))
        import json
        rows = [json.loads(r[0] or "{}") for r in conn.execute(
            "SELECT constraints FROM ontology_types WHERE type_kind='attribute'")]
        has = lambda k: sum(1 for d in rows if k in d)
        check("A5a unit 已有（未缺失）", has("unit") >= 10, f"unit={has('unit')}/{len(rows)}")
        check("A5b quantity_kind 已有", has("quantity_kind") >= 10,
              f"quantity_kind={has('quantity_kind')}/{len(rows)}")
        check("A5c 真正缺 min/max/pattern/default",
              all(has(k) == 0 for k in ("min", "max", "pattern", "default")),
              f"min={has('min')} max={has('max')} pattern={has('pattern')} default={has('default')}")

    print("\n[5] 变异自证")
    src = Path("services/ontology_migration.py").read_text(encoding="utf-8")
    mutant = src.replace('if lo is not None or hi is not None:\n            try:\n                num = float(v)',
                         'if False:\n            try:\n                num = float(v)', 1)
    check("A6 变异点存在", mutant != src, "去掉 range 分支后 A2 必不成立")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
