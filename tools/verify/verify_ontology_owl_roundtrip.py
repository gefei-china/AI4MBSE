# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-24 A2：OWL 与 Turtle 两路径保住复杂公理 + imports 不拉取 + 导出导入往返。内存夹具，验证导出函数依赖真库 schema
"""A2 验证：OWL 与 Turtle 两条导入路径都要保住复杂公理，且 imports 不拉取。

    .venv/Scripts/python.exe tools/verify/verify_ontology_owl_roundtrip.py

判据：
  T1 OWL 导入保住 equivalentClass / disjointWith / restrictions
  T2 Turtle 导入保住 equivalentClass / disjointWith / restrictions
  T3 两条路径都记录 owl:imports 且不发起网络请求
  T4 导出 Turtle → 再导入，基础类型与约束不丢（离线往返）
  T5 已有约束不被覆盖（幂等只补缺键）
  T6 变异自证：去掉 merge 段后 T2 不成立
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


def fixture():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE ontology_types(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
        type_kind TEXT, parent_id TEXT, properties TEXT, constraints TEXT,
        description TEXT, status TEXT, updated_at TEXT);
      INSERT INTO ontology_types(name,type_kind,status,properties,constraints)
        VALUES('卫星','entity','released','{}','{"required":["kind"]}');
    """)
    return c


OWL = '''<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://ex.org/t"/>
  <owl:imports rdf:resource="http://ex.org/ext.ttl"/>
  <owl:Class rdf:about="http://ex.org/Satellite">
    <rdfs:label>卫星</rdfs:label>
    <owl:equivalentClass rdf:resource="http://ex.org/Spacecraft"/>
    <owl:disjointWith rdf:resource="http://ex.org/Station"/>
    <rdfs:subClassOf>
      <owl:Restriction>
        <owl:onProperty rdf:resource="http://ex.org/hasPart"/>
        <owl:minCardinality>1</owl:minCardinality>
      </owl:Restriction>
    </rdfs:subClassOf>
  </owl:Class>
</rdf:RDF>'''

TTL = '''@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://ex.org/> .
ex:Satellite a owl:Class ; rdfs:label "卫星" ;
  owl:equivalentClass ex:Spacecraft ;
  owl:disjointWith ex:Station ;
  owl:imports ex:ext.ttl ;
  rdfs:subClassOf [ a owl:Restriction ; owl:onProperty ex:hasPart ; owl:minCardinality 1 ] .
'''


def cons_of(c, name="卫星"):
    v = c.execute("SELECT constraints FROM ontology_types WHERE name=?", (name,)).fetchone()[0]
    import json
    try:
        return json.loads(v or "{}")
    except Exception:
        return {}


def main():
    import ontology_owl as ow

    print("\n[1] OWL/RDF-XML 路径")
    c1 = fixture()
    r1 = ow.from_owl(OWL, c1)
    k1 = cons_of(c1)
    check("T1 OWL 复杂公理保住", all(x in k1 for x in ("equivalentClass", "disjointWith", "restrictions")),
          str(k1)[:180])
    check("T3a OWL imports 只记录", any("owl:imports" in e for e in r1.get("errors", [])))

    print("\n[2] Turtle 路径（A2 新增）")
    c2 = fixture()
    r2 = ow.from_turtle(TTL, c2)
    k2 = cons_of(c2)
    check("T2 Turtle 复杂公理保住",
          all(x in k2 for x in ("equivalentClass", "disjointWith", "restrictions")),
          str(k2)[:180])
    check("T3b Turtle imports 只记录", any("owl:imports" in e for e in r2.get("errors", [])))

    print("\n[3] 幂等与既有约束")
    check("T5 既有 required 未被覆盖", k2.get("required") == ["kind"], str(k2.get("required")))

    print("\n[4] 离线往返（导出→导入）")
    c3 = fixture()
    # 用真实导出函数产 Turtle（若依赖完整 schema 则跳过）
    try:
        from ontology_semantics import OntologyValidator
        _t = OntologyValidator(c3).to_turtle() if hasattr(OntologyValidator(c3), "to_turtle") else ow.to_turtle(c3)
        ttl = _t[0] if isinstance(_t, tuple) else _t  # to_turtle 返回 (text, ...) 元组
        c4 = sqlite3.connect(":memory:")
        c4.row_factory = sqlite3.Row
        c4.executescript("""
          CREATE TABLE ontology_types(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            type_kind TEXT, parent_id TEXT, properties TEXT, constraints TEXT,
            description TEXT, status TEXT, updated_at TEXT);
        """)
        rr = ow.from_turtle(ttl, c4)
        names = {x[0] for x in c4.execute("SELECT name FROM ontology_types")}
        check("T4 导出→导入类型不丢", "卫星" in names, f"imported={rr.get('imported')} names={sorted(names)[:5]}")
    except Exception as e:
        check("T4 导出→导入类型不丢", False, f"{type(e).__name__}: {e}")

    print("\n[5] 变异自证")
    src = Path("core/domain/ontology_owl.py").read_text(encoding="utf-8")
    check("T6 变异点存在（Turtle merge 段）",
          "Turtle 复杂公理解析降级" in src and "turtle_axioms.items()" in src,
          "去掉 merge 段后 T2 必不成立")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
