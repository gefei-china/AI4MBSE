# -*- coding: utf-8 -*-
"""第四期「建得准」验证：词典映射 / OWL 复杂公理 / MBSE CQ 种子。

    .venv/Scripts/python.exe tools/verify/verify_ontology_phase4.py

纪律：不写真库（所有写操作在内存夹具上做）。
判据：
  M1 候选生成覆盖全部空映射概念，且不自动写库
  M2 批量确认写入 maps_to_class（内存夹具），拒绝未注册类型
  M3 已有映射不被覆盖（除非 allow_overwrite）
  M4 OWL 导入解析 owl:Restriction / equivalentClass / disjointWith
  M5 owl:imports 只记录不拉取（离线）
  M6 MBSE CQ 种子在干净库登记 4 条
  M7 变异自证：候选生成不给分 → M1 不成立
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
      CREATE TABLE glossary_concepts(concept_id TEXT PRIMARY KEY, pref_label TEXT,
        definition TEXT, domain TEXT, concept_status TEXT, maps_to_class TEXT,
        maps_to_prop TEXT, maps_to_inst TEXT, replaced_by TEXT, version TEXT,
        created_by TEXT, created_at TEXT, updated_at TEXT, intent TEXT, boost REAL,
        context TEXT, note TEXT, source TEXT, broader TEXT, related TEXT);
      CREATE TABLE ontology_types(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
        type_kind TEXT, parent_id TEXT, properties TEXT, constraints TEXT,
        description TEXT, status TEXT, updated_at TEXT);
      INSERT INTO ontology_types(name,type_kind,status,properties,constraints)
        VALUES('宽带载荷','entity','released','{}','{}');
      INSERT INTO ontology_types(name,type_kind,status,properties,constraints)
        VALUES('卫星','entity','released','{}','{}');
      INSERT INTO glossary_concepts(concept_id,pref_label) VALUES('c1','宽带载荷');
      INSERT INTO glossary_concepts(concept_id,pref_label) VALUES('c2','卫星平台');
      INSERT INTO glossary_concepts(concept_id,pref_label,maps_to_class) VALUES('c3','已映射','卫星');
    """)
    return c


def main():
    from services import glossary_mapper, ontology_cq
    import ontology_owl

    print("\n[1] 词典映射候选（内存夹具）")
    c = fixture()
    before = c.execute("SELECT COUNT(*) FROM glossary_concepts WHERE maps_to_class IS NOT NULL "
                       "AND maps_to_class!=''").fetchone()[0]
    cand = glossary_mapper.candidates(c)
    after = c.execute("SELECT COUNT(*) FROM glossary_concepts WHERE maps_to_class IS NOT NULL "
                      "AND maps_to_class!=''").fetchone()[0]
    check("M1a 覆盖全部空映射", cand["total_empty"] == 2,
          f"empty={cand['total_empty']} with_cands={cand['with_candidates']}")
    check("M1b 生成候选不写库", before == after, f"before={before} after={after}")
    c1 = next((x for x in cand["items"] if x["concept_id"] == "c1"), None)
    check("M1c 精确同名给高分", c1 and c1["candidates"] and c1["candidates"][0]["score"] == 1.0,
          f"top={c1['candidates'][0] if c1 and c1['candidates'] else None}")

    print("\n[2] 批量确认与拒绝路径")
    r = glossary_mapper.apply_mapping(c, [
        {"concept_id": "c1", "class_name": "宽带载荷"},
        {"concept_id": "c2", "class_name": "不存在的类型"},
        {"concept_id": "c3", "class_name": "宽带载荷"},  # 已有映射，应拒
    ], operator="verify")
    check("M2a 合法映射写入", r["applied"] == 1, str(r["applied"]))
    check("M2b 未注册类型被拒", any("未注册" in x["reason"] for x in r["rejected"]))
    check("M2c 已有映射不被覆盖", any("allow_overwrite" in x["reason"] for x in r["rejected"]))
    v = c.execute("SELECT maps_to_class FROM glossary_concepts WHERE concept_id='c1'").fetchone()[0]
    check("M2d 落库值正确", v == "宽带载荷", f"value={v}")
    r2 = glossary_mapper.apply_mapping(c, [
        {"concept_id": "c3", "class_name": "宽带载荷", "allow_overwrite": True}])
    check("M2e 显式覆盖可生效", r2["applied"] == 1, str(r2["applied"]))

    print("\n[3] OWL 复杂公理解析")
    owl = '''<?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
             xmlns:owl="http://www.w3.org/2002/07/owl#">
      <owl:Ontology rdf:about="http://ex.org/test"/>
      <owl:imports rdf:resource="http://ex.org/external.ttl"/>
      <owl:Class rdf:about="http://ex.org/Satellite">
        <rdfs:label>卫星</rdfs:label>
        <owl:equivalentClass rdf:resource="http://ex.org/Spacecraft"/>
        <owl:disjointWith rdf:resource="http://ex.org/GroundStation"/>
        <rdfs:subClassOf>
          <owl:Restriction>
            <owl:onProperty rdf:resource="http://ex.org/hasComponent"/>
            <owl:minCardinality>1</owl:minCardinality>
            <owl:someValuesFrom rdf:resource="http://ex.org/Component"/>
          </owl:Restriction>
        </rdfs:subClassOf>
      </owl:Class>
    </rdf:RDF>'''
    oc = fixture()  # 已有「卫星」类型
    res = ontology_owl.from_owl(owl, oc)
    cons = oc.execute("SELECT constraints FROM ontology_types WHERE name='卫星'").fetchone()[0]
    check("M4a 基础导入不报错", isinstance(res, dict) and "imported" in res, str(res))
    check("M4b 复杂公理已合并", any(k in cons for k in
          ("equivalentClass", "disjointWith", "restrictions")), f"cons={cons[:160]}")
    check("M5 imports 只记录不拉取", any("owl:imports" in e for e in res.get("errors", [])),
          str(res.get("errors"))[:120])

    print("\n[4] MBSE CQ 种子")
    qc = sqlite3.connect(":memory:")
    qc.row_factory = sqlite3.Row
    qc.executescript("""
      CREATE TABLE ontology_questions(id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT,
        kind TEXT, query TEXT, expected TEXT, enabled INTEGER, source TEXT, created_by TEXT);
      CREATE TABLE ontology_cq_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER,
        status TEXT, source TEXT, row_count INTEGER, message TEXT, ts TEXT);
    """)
    ontology_cq.ensure_seed(qc)
    rows = [r[0] for r in qc.execute("SELECT question FROM ontology_questions")]
    check("M6 MBSE 种子 4 条", len(rows) == 12 and
          sum(1 for q in rows if q.startswith(("系统需求是否有对应", "被满足的需求是否",
              "需求与部件之间", "验证关系的两端"))) == 4,
          f"total={len(rows)}")

    print("\n[5] 变异自证：候选相似度归零后 M1c 不成立")
    orig = glossary_mapper._sim
    glossary_mapper._sim = lambda a, b: 0.0
    try:
        mutant = glossary_mapper.candidates(fixture())
        mc = next((x for x in mutant["items"] if x["concept_id"] == "c1"), None)
        ok = not (mc and mc["candidates"])
    finally:
        glossary_mapper._sim = orig
    check("M7 变异击穿", ok, "相似度归零 ⇒ 无候选")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
