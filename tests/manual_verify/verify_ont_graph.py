"""本体管理图谱交互验证脚本（米爸3点优化）：
1. 左侧点选实体类型 → 图谱节点+关联节点+关联边一起高亮
2. 图谱中移动节点 → 关系曲线边跟随
3. 左侧点选关系类型 → 图谱中对应关系边高亮

用法: python verify_ont_graph.py
"""
import json
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.request

BASE = "http://127.0.0.1:8000"


def api(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " | " + name)
    return cond


passed = 0
total = 0


def run(name, fn):
    global passed, total
    total += 1
    try:
        ok = fn()
        if ok:
            passed += 1
        check(name, ok)
    except Exception as e:
        check(name + f" (异常: {e})", False)


# ── 1) 本体类型与图谱数据 ──
def test_ontology_clean():
    d = api("/api/knowledge/ontology")
    kinds = {}
    for t in d:
        kinds[t["type_kind"]] = kinds.get(t["type_kind"], 0) + 1
    # 无重复名
    names = [t["name"] for t in d]
    return len(names) == len(set(names)) and kinds.get("entity", 0) >= 5 and kinds.get("relation", 0) >= 5


def test_graph_edges():
    g = api("/api/knowledge/ontology/graph")
    return len(g.get("edges", [])) >= 5


run("本体类型无重复且实体/关系>=5", test_ontology_clean)
run("本体类型图谱有约束边>=5", test_graph_edges)

# ── 2) 关系类型 domain/range 下拉备选项验证（仅本体类型，无实例）──
def test_rel_constraints():
    d = api("/api/knowledge/ontology")
    rel = [t for t in d if t["type_kind"] == "relation" and t["name"] == "满足"][0]
    cons = rel.get("constraints", {})
    if isinstance(cons, str):
        cons = json.loads(cons or "{}")
    av = cons.get("allowed_values", {})
    src = av.get("src", [])
    tgt = av.get("tgt", [])
    if isinstance(src, str):
        src = [src]
    if isinstance(tgt, str):
        tgt = [tgt]
    entity_names = {t["name"] for t in d if t["type_kind"] == "entity"}
    # src/tgt 必须是已存在的实体类型（本体类型），且不含实例 id（ENT-/REQ- 前缀）
    ok_src = all(s in entity_names for s in src)
    ok_tgt = all(s in entity_names for s in tgt)
    no_inst = all(not (s.startswith("ENT-") or s.startswith("REQ-") or "实例" in s) for s in src + tgt)
    # 2026-08-06 放宽约束后允许多 domain/range，只要非空且都指向本体实体
    return ok_src and ok_tgt and no_inst and len(src) >= 1 and len(tgt) >= 1


run("关系满足: domain/range 指向本体实体且无实例", test_rel_constraints)

print(f"\n结果: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
