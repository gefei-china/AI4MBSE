#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：本体变更的 SemVer 语义（A4，2026-11-21 新增）。

## 要挡住的是什么

改造前「一次本体编辑该算 MAJOR 还是 MINOR」有**四份**各自为政的实现，
且查的键各不相同，其中 `domain_classes`（定义域）**没有任何一份读到过**：

    shared.py:_snapshot_diff                 —— 内联 _dr，只比 allowed_values
    shared.py:_count_dom_range_changed       —— 只比 allowed_values（名字却叫"定义域/值域"）
    shared.py:_count_constraints_changed     —— 其余全部 ⇒ 一律 MINOR + 标"兼容"
    shared.py:_snapshot_dom_range_diff       —— 查 `domain`/`range`，而数据存 `domain_classes`

实测 7 类破坏性变更中 **6 类被判"兼容"**（收窄 domain_classes、改 xsd_type、
摘 symmetric、新增 required/unique、改 unit）⇒ 下游实例静默变违规、版本号不报警。

## 判据（每条都能判红，见末尾变异自证）

  C1  规则表 13 个判例全部正确（含 6 个曾误判的）
  C2  库里出现的约束键全部登记进 `RULE_TABLE`（未登记键会被 fail-safe 判 MAJOR，
      长期靠 fail-safe 会让发布恒阻塞 ⇒ 必须显式补规则，而不是靠兜底）
  C3  publish / release **两条**版本判定路径都走 `classify_snapshot_change`，
      且**不再调用**旧的三个函数
  C4  `shared.py` 三个兼容包装都委托（源码里能查到 `classify_snapshot_change`）
      —— 防"包装又长出自己的实现"
  C5  OWL 导出真的带版本链（versionIRI / priorVersion / 兼容或不兼容声明），
      且在"不兼容版本"下会切到 `owl:incompatibleWith`
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []
PASS, FAIL = "PASS", "FAIL"


def _add(ok, name, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:320]))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


#: C1 判例表：(名称, 前约束, 后约束, 期望级别)
CASES = [
    ("收窄 domain_classes", {"domain_classes": ["A", "B"]}, {"domain_classes": ["A"]}, "major"),
    ("改 xsd_type", {"xsd_type": "xsd:decimal"}, {"xsd_type": "xsd:string"}, "major"),
    ("收窄 allowed_values", {"allowed_values": {"src": ["x", "y"], "tgt": []}},
     {"allowed_values": {"src": ["x"], "tgt": []}}, "major"),
    ("摘 characteristics(symmetric)", {"characteristics": ["symmetric"]},
     {"characteristics": []}, "major"),
    ("新增 required", {"required": ["a"]}, {"required": ["a", "b"]}, "major"),
    ("新增 unique", {}, {"unique": ["code"]}, "major"),
    ("改 unit dBm→W", {"unit": "dBm"}, {"unit": "W"}, "major"),
    ("基数 max 4→1", {"card_src": {"min": 1, "max": 4}}, {"card_src": {"min": 1, "max": 1}}, "major"),
    ("未登记约束键", {"foo_bar": 1}, {"foo_bar": 2}, "major"),
    ("只改 alt_labels", {"alt_labels": ["甲"]}, {"alt_labels": ["乙"]}, "minor"),
    ("放宽 domain_classes", {"domain_classes": ["A"]}, {"domain_classes": ["A", "B"]}, "minor"),
    ("移除 required", {"required": ["a", "b"]}, {"required": ["a"]}, "minor"),
    ("无变更", {"unit": "dBm"}, {"unit": "dBm"}, "none"),
]


def _row(name, cons):
    import json
    return {"name": name, "type_kind": "entity",
            "constraints": json.dumps(cons, ensure_ascii=False)}


def check_c1():
    try:
        from core.ontology_semver import classify_snapshot_change as cls
    except Exception as e:
        _add(False, "C1 判定模块可导入", e)
        return
    bad = []
    for name, prev, cur, want in CASES:
        got = cls([_row("T", prev)], [_row("T", cur)])["level"]
        if got != want:
            bad.append(f"{name}: 期望 {want} 实得 {got}")
    _add(not bad, f"C1 规则表判例全对（{len(CASES)} 例）", "; ".join(bad[:5]))


def check_c2():
    """约束键覆盖：库里出现的键必须都已登记进规则表。

    两级取数（**为什么需要基线文件**）：`mbse.db` 在 .gitignore 里（571MB），CI 没有真库。
    所以：有真库 → 用真库（最新事实）；无真库 → 用 `baselines/ontology_constraint_keys.json`
    （随版本走的键清单）。两者都没有 ⇒ 判红（不许"看不见就放过"）。
    """
    try:
        from core.ontology_semver import KNOWN_FIELDS
    except Exception as e:
        _add(False, "C2 判定模块可导入", e)
        return
    db = os.path.join(ROOT, "mbse.db")
    src, keys = "", {}
    if os.path.isfile(db):
        c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        try:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT constraints FROM ontology_types").fetchall()
        finally:
            c.close()
        from core.ontology_semver import uncovered_keys
        keys = uncovered_keys(rows)
        src = "真库 mbse.db"
    else:
        base = os.path.join(ROOT, "tools", "verify", "baselines", "ontology_constraint_keys.json")
        if not os.path.isfile(base):
            _add(False, "C2 既无真库也无键基线，无法验证覆盖", base)
            return
        import json
        with open(base, encoding="utf-8") as f:
            keys = {k: v for k, v in (json.load(f).get("keys") or {}).items() if k not in KNOWN_FIELDS}
        src = "基线清单 ontology_constraint_keys.json"
    _add(not keys, "C2 约束键全部已登记（来源：%s）" % src,
         "未登记（会被 fail-safe 判 MAJOR，请补规则表）: " + str(keys))


def check_c3():
    src = _read("routers/knowledge_parts/ontology_version.py")
    # 去掉注释行再判"是否还在调用旧函数"，避免注释里的函数名造成假红
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    stale = [fn for fn in ("_count_dom_range_changed(", "_count_constraints_changed(",
                           "_snapshot_dom_range_diff(") if fn in body]
    _add(not stale, "C3 版本判定不再调用旧的三个实现", "仍在调用: " + ", ".join(stale))
    n_import = len(re.findall(r"classify_snapshot_change", body))
    _add(n_import >= 2, "C3b publish 与 release 两条路径都走统一判定",
         f"仅出现 {n_import} 次（应 ≥2：publish + release）")


def check_c4():
    src = _read("routers/knowledge_parts/shared.py")
    missing = []
    for fn in ("_count_dom_range_changed", "_count_constraints_changed", "_snapshot_dom_range_diff"):
        m = re.search(r"def %s\(.*?\n(?=\ndef |\nclass |\Z)" % re.escape(fn), src, re.S)
        seg = m.group(0) if m else ""
        if "classify_snapshot_change" not in seg:
            missing.append(fn)
    _add(not missing, "C4 三个兼容包装均委托单一实现", "未委托: " + ", ".join(missing))


def _ver_db(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE ontology_versions (id INT, version_label TEXT, "
              "compatible INT, status TEXT)")
    for r in rows:
        c.execute("INSERT INTO ontology_versions VALUES (?,?,?,?)", r)
    return c


def check_c5():
    try:
        from ontology_owl import _version_declarations
    except Exception as e:
        _add(False, "C5 版本声明可导入", e)
        return
    base = "http://example.org/ont#"
    # ① 兼容版本：应有 priorVersion + backwardCompatibleWith，无 incompatibleWith
    ok_db = _ver_db([(1, "v1.0.0", 1, "released"), (2, "v1.1.0", 1, "released")])
    d1 = _version_declarations(ok_db, base, "Demo")
    ok1 = (d1.get("version_iri", "").endswith("/Demo/v1.1.0")
           and d1.get("prior_version", "").endswith("/Demo/v1.0.0")
           and "backward_compatible_with" in d1 and "incompatible_with" not in d1)
    _add(ok1, "C5 兼容版本导出 backwardCompatibleWith", str(d1))
    # ② 不兼容版本：必须切到 incompatibleWith
    bad_db = _ver_db([(1, "v1.0.0", 1, "released"), (2, "v2.0.0", 0, "released")])
    d2 = _version_declarations(bad_db, base, "Demo")
    ok2 = "incompatible_with" in d2 and "backward_compatible_with" not in d2
    _add(ok2, "C5b 不兼容版本导出 incompatibleWith", str(d2))
    # ③ 无已发布版本 ⇒ 不编造声明
    none_db = _ver_db([])
    ok3 = _version_declarations(none_db, base, "Demo") == {}
    _add(ok3, "C5c 无版本时不编造声明", str(_version_declarations(none_db, base, "Demo")))


def mutations():
    """变异自证：改坏规则表，判据必须判红。"""
    out = []
    import importlib
    import core.ontology_semver as sem

    # M1：把 xsd_type 从"语义类型类"里摘掉 ⇒ C1 的该判例应变成 minor
    keep = sem.SEMANTIC_TYPE_FIELDS
    try:
        sem.SEMANTIC_TYPE_FIELDS = tuple(f for f in keep if f != "xsd_type")
        got = sem.classify_snapshot_change([_row("T", {"xsd_type": "xsd:decimal"})],
                                           [_row("T", {"xsd_type": "xsd:string"})])["level"]
        out.append(("M1 摘掉 xsd_type 规则 ⇒ 该判例降级为 minor", got != "major", f"实得 {got}"))
    finally:
        sem.SEMANTIC_TYPE_FIELDS = keep

    # M2：从已知键集合里删一个键 ⇒ 未登记键检查应能发现它（反向：把某键从 KNOWN 移除后，
    #     该键应被判为 uncovered）
    keep_known = sem.KNOWN_FIELDS
    try:
        sem.KNOWN_FIELDS = frozenset(k for k in keep_known if k != "unit")
        un = sem.uncovered_keys([{"constraints": '{"unit": "dBm"}'}])
        out.append(("M2 移出已知键 ⇒ 未登记键检查判红", "unit" in un, f"实得 {un}"))
    finally:
        sem.KNOWN_FIELDS = keep_known
    return out


def main():
    print("=" * 76)
    print("门禁：本体变更 SemVer 语义（A4）")
    print("=" * 76)
    check_c1()
    check_c2()
    check_c3()
    check_c4()
    check_c5()

    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _results if r[0] == FAIL)

    print("\n--- 变异自证 ---")
    mut = mutations()
    for name, ok, detail in mut:
        print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  <- " + detail) if detail else ""))
    n_mut_fail = sum(1 for x in mut if not x[1])

    print("\n" + "=" * 76)
    if n_fail or n_mut_fail or not mut:
        print("结论：未通过（常态 %d 条失败 / 变异 %d 组未判红）" % (n_fail, n_mut_fail))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组按预期判红）" % (len(_results), len(mut)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
