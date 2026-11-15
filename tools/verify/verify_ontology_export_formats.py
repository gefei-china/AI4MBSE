# -*- coding: utf-8 -*-
"""门禁：本体导出的**三种 RDF 语法**必须都合法、且**语义等价**（2026-11-15）。

## 为什么需要它（真机抓出的三个真 bug，全都在这个门禁的射程内）

本仓的 OWL 导出长期只有 `to_owl`（RDF/XML）；Turtle 与 JSON-LD 是后补的。
**三种语法此前从没被真正的 RDF 解析器验证过全量输出**，于是：

| # | bug | 症状 | 发现方式 |
|---|---|---|---|
| 1 | `to_turtle` 的 `_uri` 直接拼 `:局部名`，而 IRI 里有 `G/T值` 这类名字 | 局部名含 `/` ⇒ PN_LOCAL 非法 ⇒ **整份 Turtle BadSyntax** | rdflib 解析全量输出 |
| 2 | 给 `skos:altLabel` 插行时没给 `rdfs:label` 补 `;`，也没声明 `@prefix skos:` | 非法 Turtle（缺分隔符 + 未定义前缀） | rdflib |
| 3 | JSON-LD 顶层同时写 `@id` + `@graph` | 构成**命名图** ⇒ 普通 Graph 解析得 **0 条**三元组（"看起来有内容"但读出来是空） | rdflib 解析计数 |

⇒ 结论：**"导出没报错" ≠ "导出是合法 RDF"**。必须有解析器级别的判据。

## 判据（三条，都是"能红"的）

A. 三种格式都能被 rdflib 解析（不抛异常）；
B. 关键事实在三种格式里**都在**：BFO 锚点 / skos:altLabel / owl:TransitiveProperty /
   owl:SymmetricProperty / xsd 数据类型 / 量纲单位；
C. 三种格式的三元组数量**同量级**（差异 ≤ 25%）——防"某一路悄悄丢了半个图谱"
   （差异容忍是因为 RDF/XML 与 JSON-LD 对 `@type` 多值、注释的承载方式不同）。

## 运行

    python tools/verify/verify_ontology_export_formats.py

rc=0 全绿；rc=1 有红项；rc=2 SKIP（无 rdflib 或无 mbse.db）。
"""
from __future__ import annotations

import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")

_PASS = 0
_FAIL = 0
_FAILED = []


def check(name, ok, detail=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [PASS] %s" % name)
    else:
        _FAIL += 1
        _FAILED.append("%s :: %s" % (name, detail))
        print("  [FAIL] %s :: %s" % (name, detail))


def main():
    try:
        import rdflib
    except ImportError:
        print("SKIP: rdflib 未安装（本门禁需要真正的 RDF 解析器）")
        return 2
    if not os.path.exists(DB):
        print("SKIP: mbse.db 不存在（本门禁读真库 schema）")
        return 2

    import ontology_owl as O

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM ontology_types").fetchall()
    if not rows:
        print("SKIP: 本体类型为空")
        return 2

    print("=" * 78)
    print("门禁：本体导出三格式（RDF/XML / Turtle / JSON-LD）合法性与语义等价")
    print("口径：真库 ontology_types 全量 %d 条" % len(rows))
    print("=" * 78)

    # ── A. 三格式都能被解析 ────────────────────────────────────────────────
    print("\n--- A. 解析合法性（rdflib）---")
    outs = {}
    xml, _ = O.to_owl(con, rows=rows)
    outs["owl"] = ("xml", xml)
    _t = O.to_turtle(con, rows=rows)
    outs["ttl"] = ("turtle", _t[0] if isinstance(_t, tuple) else _t)
    _j = O.to_jsonld(con, rows=rows)
    outs["jsonld"] = ("json-ld", _j[0] if isinstance(_j, tuple) else _j)

    graphs = {}
    for k, (fmt, text) in outs.items():
        g = rdflib.Graph()
        try:
            g.parse(data=text, format=fmt)
            graphs[k] = g
            check("A-%s 可被 rdflib 解析（%s）" % (k, fmt), len(g) > 0, "三元组=%d" % len(g))
        except Exception as e:
            check("A-%s 可被 rdflib 解析（%s）" % (k, fmt), False, "%s: %s" % (type(e).__name__, str(e)[:200]))
            graphs[k] = None

    if any(v is None for v in graphs.values()):
        print("\n结果：FAIL —— 有格式无法解析，后续等价性判据无法进行")
        con.close()
        return _summary()

    # ── B. 关键事实三路都在 ────────────────────────────────────────────────
    print("\n--- B. 关键事实三路齐备 ---")
    SP = "http://www.w3.org/2004/02/skos/core#"
    OWL = "http://www.w3.org/2002/07/owl#"
    RDFS = "http://www.w3.org/2000/01/rdf-schema#"
    BFO = "http://purl.obolibrary.org/obo/"

    def _has(g, pred=None, obj_type=None, substr=None):
        """在图中找：给定谓词的三元组 / 给定 rdf:type 的实例 / 含某子串的字面量。"""
        if pred:
            return any(True for _ in g.triples((None, rdflib.URIRef(pred), None)))
        if obj_type:
            return any(True for _ in g.triples((None, rdflib.RDF.type, rdflib.URIRef(obj_type))))
        if substr:
            return any(substr in str(o) for _s, _p, o in g)
        return False

    facts = [
        ("BFO 锚点（subClassOf → obo/BFO_）", lambda g: _has(g, substr=BFO)),
        ("skos:altLabel 别名", lambda g: _has(g, pred=SP + "altLabel")),
        ("owl:TransitiveProperty 传递公理", lambda g: _has(g, obj_type=OWL + "TransitiveProperty")),
        ("owl:SymmetricProperty 对称公理", lambda g: _has(g, obj_type=OWL + "SymmetricProperty")),
        ("量纲单位（dBm / MHz 等）", lambda g: _has(g, substr="dBm") or _has(g, substr="MHz")),
        ("rdfs:subClassOf 层级", lambda g: _has(g, pred=RDFS + "subClassOf")),
    ]
    for label, fn in facts:
        for k, g in graphs.items():
            check("%s · %s" % (label, k), fn(g), "该事实在 %s 里缺失" % k)

    # ── C. 三元组数量同量级 ───────────────────────────────────────────────
    print("\n--- C. 三格式规模同量级（≤25% 偏差）---")
    counts = {k: len(g) for k, g in graphs.items()}
    print("    计数: %s" % counts)
    lo, hi = min(counts.values()), max(counts.values())
    check("C 三格式三元组数同量级（max/min = %.3f ≤ 1.25）" % (hi / float(lo or 1)),
          hi <= lo * 1.25, "counts=%s" % counts)

    # ── D. 变异自证：把 Turtle 的 PN_LOCAL 守卫关掉 ⇒ A-ttl 必须红 ────────
    print("\n--- D. 变异自证（证明判据有鉴别力）---")
    import re as _re
    _src_p = os.path.join(_ROOT, "ontology_owl.py")
    _raw = open(_src_p, "rb").read()
    _text = _raw.decode("utf-8")
    _anchor = 'if re.match(r"^[A-Za-z0-9_\\-.%]+$", local):'
    if _anchor in _text:
        try:
            # 变异：让守卫**恒真**（即恢复成"永远用 :local"，含 / 时必非法）
            _mut = _text.replace(_anchor, 'if True:', 1)
            open(_src_p, "wb").write(_mut.encode("utf-8"))
            import importlib
            importlib.reload(O)
            _t2 = O.to_turtle(con, rows=rows)
            _t2 = _t2[0] if isinstance(_t2, tuple) else _t2
            _g2 = rdflib.Graph()
            _broke = False
            try:
                _g2.parse(data=_t2, format="turtle")
            except Exception:
                _broke = True
            check("D 变异自证：关掉 PN_LOCAL 守卫 ⇒ Turtle 解析必须失败",
                  _broke, "变异后仍解析通过 —— 说明该断言是空转（没真覆盖到带 / 的名字）")
        finally:
            open(_src_p, "wb").write(_raw)          # 逐字节还原
            import importlib
            importlib.reload(O)
            check("D 变异源文件已逐字节还原", open(_src_p, "rb").read() == _raw, "")
    else:
        check("D 变异锚点存在（PN_LOCAL 守卫）", False, "锚点未找到，变异无法执行")

    con.close()
    return _summary()


def _summary():
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d" % (_PASS, _FAIL))
    if _FAILED:
        print("红项：")
        for x in _FAILED:
            print("  -", x)
    print("=" * 78)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
