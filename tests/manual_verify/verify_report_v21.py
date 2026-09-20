# -*- coding: utf-8 -*-
"""报告优化项验证（S1/S2/C3/C2/D1 契约）：路径合理性判读、基线元数据、处置动作数据契约 + 全量回归。"""
import json
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ = os.path.dirname(BASE)
sys.path.insert(0, PROJ)

_results = []
def chk(name, ok, detail=None):
    _results.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  -> {json.dumps(detail, ensure_ascii=False, default=str)[:400]}"))

from services.impact_engine import analyze_graph_v2, baseline_meta  # noqa: E402

# ── 合成图谱：结构链 + 追溯链 + 混合链，验证 S1 三种判读 ──
N = [
    {"id": "E1", "name": "巡飞弹", "entity_type": "系统"},
    {"id": "E2", "name": "动力分系统", "entity_type": "分系统"},
    {"id": "E3", "name": "活塞发动机", "entity_type": "设备"},
    {"id": "R1", "name": "推力需求", "entity_type": "单元需求"},
    {"id": "T1", "name": "相关文档条目", "entity_type": "文档"},
    {"id": "T2", "name": "追溯条目", "entity_type": "文档"},
]
E = [
    {"source_id": "E1", "target_id": "E2", "relation_type": "CONTAINS"},
    {"source_id": "E2", "target_id": "E3", "relation_type": "CONTAINS"},
    {"source_id": "R1", "target_id": "E2", "relation_type": "满足"},          # 需求→满足→元素（rev 传播，语义成立）
    {"source_id": "T1", "target_id": "E3", "relation_type": "REFERENCES"},   # 纯追溯链
    {"source_id": "T2", "target_id": "T1", "relation_type": "TRACE"},
]
card = analyze_graph_v2(N, E, {"id": "E1", "name": "巡飞弹", "entity_type": "系统"},
                        depth=4, direction="both", change_type="delete", change_desc="重组动力分系统")
paths = {(tuple(p["node_ids"])): p for p in card["path_list"]}

# A. S1 路径合理性判读
p_struct = paths.get(("E1", "E2"))
chk("A1 S1：结构链（CONTAINS fwd）→ reasonable",
    p_struct and p_struct["plausibility"] == "reasonable", p_struct)
p_rev = paths.get(("E1", "E2", "R1"))
chk("A2 S1：需求满足链（满足 rev 逆向）→ reasonable（语义成立，不因逆向误判）",
    p_rev and p_rev["plausibility"] == "reasonable", p_rev)
p_trace = None
card_t = analyze_graph_v2(N, E + [{"source_id": "T3", "target_id": "T2", "relation_type": "TRACE"}],
                          {"id": "T3", "name": "追溯源", "entity_type": "文档"},
                          depth=4, direction="both", change_type="delete")
for p in card_t["path_list"]:
    if tuple(p["node_ids"]) == ("T3", "T2", "T1"):
        p_trace = p
chk("A3 S1：纯追溯链（TRACE+REFERENCES 全链）→ trace（仅提示不构成必然影响）",
    p_trace and p_trace["plausibility"] == "trace", p_trace)
p_mixed = paths.get(("E1", "E2", "E3", "T1", "T2"))
chk("A4 S1：混合链（结构+追溯）→ reasonable_partial",
    p_mixed and p_mixed["plausibility"] == "reasonable_partial", p_mixed)
chk("A5 S1：全路径均携带 plausibility/reason 字段",
    all(("plausibility" in p and "plausibility_reason" in p) for p in card["path_list"]),
    len(card["path_list"]))

# B. C2 处置动作数据契约（前端 actionFor 消费 impact_nodes 的 type+level）
aff = {n["id"]: n for n in card["impact_nodes"]}
chk("B1 C2 契约：受影响元素含 type+level（处置动作映射输入）",
    all(("type" in n and "level" in n) for n in card["impact_nodes"]), list(aff)[:3])

# C. S2/C3 基线元数据
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
c = conn.cursor()
for stmt in [
    "CREATE TABLE branches (name TEXT, branch_type TEXT, status TEXT)",
    "CREATE TABLE ontology_versions (id INTEGER PRIMARY KEY, version_label TEXT, status TEXT, created_at TEXT)",
    "CREATE TABLE ontology_types (name TEXT, status TEXT)",
    "CREATE TABLE entities (id TEXT, name TEXT, entity_type TEXT, status TEXT, branch TEXT)",
]:
    c.execute(stmt)
c.execute("INSERT INTO branches VALUES ('release','release','active')")
c.execute("INSERT INTO ontology_versions VALUES (1,'V2.1','released','2026-09-16 08:00:00')")
c.execute("INSERT INTO ontology_types VALUES ('系统','released')")
c.execute("INSERT INTO entities VALUES ('E1','巡飞弹','系统','reviewed','release')")
c.execute("INSERT INTO entities VALUES ('G1','孤儿元素','未注册类型','reviewed','release')")  # 悬空
bm = baseline_meta(conn, ["release"])
chk("C1 S2：baseline_meta 分支/版本号正确",
    bm["branch"] == "release" and bm["version_label"] == "V2.1", bm)
chk("C2 C3：悬空实例扫描=1（真实一致性数据）", bm["dangling_instances"] == 1, bm)
conn.close()

# D. 向后兼容：v1 字段齐全
chk("D1 向后兼容：impact_nodes/impact_edges/depth_stats/risk_matrix/path_list 齐全",
    all(k in card for k in ("impact_nodes", "impact_edges", "depth_stats", "risk_matrix", "path_list")),
    list(card))

# E. 语义收紧（2026-09-16 第二轮）：纯追溯可达 → trace_hint 提示，不计入影响
card_t2 = analyze_graph_v2(N, E, {"id": "E1", "name": "巡飞弹", "entity_type": "系统"},
                           depth=4, direction="both", change_type="delete")
th_ids = {str(n["id"]) for n in (card_t2.get("trace_hint_nodes") or [])}
imp_ids = {str(n["id"]) for n in card_t2["impact_nodes"]}
chk("E2 语义：主图无纯追溯可达 → trace_hint 为空、影响统计不变",
    not th_ids and "T1" in imp_ids and "T2" in imp_ids, {"th": list(th_ids)})
th_ids_t = {str(n["id"]) for n in (card_t.get("trace_hint_nodes") or [])}
imp_ids_t = {str(n["id"]) for n in card_t["impact_nodes"]}
chk("E3 语义：纯追溯可达节点不进 impact_nodes、进 trace_hint（含 reason）",
    th_ids_t >= {"T1", "T2"} and not ({"T1", "T2"} & imp_ids_t)
    and all(n.get("reason") for n in card_t["trace_hint_nodes"]),
    {"th": list(th_ids_t)})
th_e = {(str(e["from"]), str(e["to"])) for e in (card_t.get("trace_hint_edges") or [])}
chk("E4 语义：追溯提示边为追溯链真实边（T3→T2、T2→T1）",
    {("T3", "T2"), ("T2", "T1")} <= th_e, th_e)
passed = sum(1 for _, ok in _results if ok)
print(f"\n{'='*52}\n{passed}/{len(_results)} PASS")
sys.exit(0 if passed == len(_results) else 1)
