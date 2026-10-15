# -*- coding: utf-8 -*-
"""P2-1 评估（二）：不一致根因定位 —— 公理分组剔除 + pellet realize 二分。

方法：to_owl → rdflib N-Triples → 按「公理类别」剔除 → pellet realize --ignore-imports
      全量 inconsistent；剔除某类后变 consistent ⇒ 冲突在该类；再组内逐条定位。
只读，不写库。离线：--ignore-imports。
"""
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

conn = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

from ontology_owl import to_owl  # noqa: E402
from services.swrl_engine import ensure_jvm_home  # noqa: E402

jre = ensure_jvm_home()
xml, _ = to_owl(conn)

import owlready2  # noqa: E402
from rdflib import Graph  # noqa: E402

world = owlready2.World()
world.get_ontology("http://mbse/onto/eval").load(fileobj=io.BytesIO(xml.encode("utf-8")))
g = world.as_rdflib_graph()
nt_path = os.path.join(tempfile.gettempdir(), "_p21_bisect.nt")
g.serialize(destination=nt_path, format="nt")

raw = io.open(nt_path, encoding="utf-8").read().splitlines()
# 丢弃 imports / ontology 元数据行（离线无关）
lines = [l for l in raw if l.strip() and "owl:imports" not in l and "owl:Ontology" not in l]

OWL2 = "http://www.w3.org/2002/07/owl#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

CHAR_OPS = ("TransitiveProperty", "SymmetricProperty", "AsymmetricProperty",
            "ReflexiveProperty", "IrreflexiveProperty", "FunctionalProperty",
            "InverseFunctionalProperty")


def classify(l: str) -> str:
    if "minQualifiedCardinality" in l or f"{OWL2}cardinality" in l or "onProperty" in l:
        return "cardinality_restriction"
    if "disjointWith" in l:
        return "disjointWith"
    if f"<{RDFS}domain>" in l or f"<{RDFS}range>" in l:
        return "domain_range"
    if f"<{RDFS}subClassOf>" in l:
        return "subClassOf"
    if any(f"<{OWL2}{c}>" in l for c in CHAR_OPS):
        return "property_characteristics"
    return "type_declaration"


groups = {}
for l in lines:
    groups.setdefault(classify(l), []).append(l)

pellet_dir = os.path.join(os.path.dirname(owlready2.__file__), "pellet")
cp = os.pathsep.join(os.path.join(pellet_dir, j) for j in os.listdir(pellet_dir) if j.endswith(".jar"))
java = os.path.join(jre, "bin", "java.exe") if jre else "java"


def check(ls, tag) -> str:
    """跑 pellet realize；返回 consistent / inconsistent / error。"""
    p_ = os.path.join(tempfile.gettempdir(), f"_p21_case_{tag}.nt")
    io.open(p_, "w", encoding="utf-8").write("\n".join(ls) + "\n")
    r = subprocess.run([java, "-Xmx1000M", "-cp", cp, "pellet.Pellet", "realize",
                        "--loader", "Jena", "--input-format", "N-Triples",
                        "--infer-prop-values", "--ignore-imports", p_],
                       capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if "consistent" in out and "inconsistent" not in out:
        res = "consistent"
    elif "inconsistent" in out or r.returncode != 0:
        res = "inconsistent" if "inconsistent" in out else f"error(rc={r.returncode})"
    else:
        res = f"unknown(rc={r.returncode})"
    os.remove(p_)
    return res


print(f"[groups] 总行数 {len(lines)}")
for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print(f"   {k}: {len(v)} 行")

_t = time.perf_counter()
full = check(lines, "full")
print(f"\n[full] 全量（已去 imports）: {full}（{(time.perf_counter()-_t):.1f}s）")

# 逐类剔除 → 哪类剔除后转 consistent
for k in sorted(groups):
    rest = [l for l in lines if classify(l) != k]
    _t = time.perf_counter()
    res = check(rest, "drop_" + k)
    print(f"[drop] 剔除 {k}（剩 {len(rest)} 行）: {res}（{(time.perf_counter()-_t):.1f}s）")
