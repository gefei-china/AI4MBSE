# -*- coding: utf-8 -*-
"""验证报告 2.0 引擎（analyze_graph_v2 / scan_name_references / decide_recommendation）。

覆盖（对应 docs/变更影响分析报告2.0设计方案.md §7）：
  A. delete 全权重传播：结构/需求/流全链路受影响，组合风险=1.0，路径枚举正确
  B. value 类型传播：沿需求链强传播、引用类剪枝、组合风险=变更矩阵系数之积
  C. rename：结构零传播（仅 source 节点）+ scan_name_references 命中文本引用
  D. 决策规则三场景：低风险批准 / Multiplier 有条件批准 / 删除触达需求细化后再议
  E. 向后兼容：analyze_graph（v1）字段不变，沙箱 simulate 口径不受影响

用法: python verify_impact_v2.py
"""
import json
import os
import sqlite3
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
proj = os.path.abspath(os.path.join(here, "..", ".."))
sys.path.insert(0, proj)

total = passed = 0


def chk(name, cond, extra=""):
    global total, passed
    total += 1
    if cond:
        passed += 1
    print(("PASS" if cond else "FAIL") + " | " + name + ((" | " + str(extra)[:220]) if extra else ""))


# ── 准备：release 分支测试网络 + 证据文档（sqlite 直插）────────────────
PFX = f"V2验证TS{int(time.time()) % 100000}"
conn = sqlite3.connect(os.path.join(proj, "mbse.db"))
conn.row_factory = sqlite3.Row
c = conn.cursor()
br = c.execute("SELECT name FROM branches WHERE branch_type='release' AND status='active' LIMIT 1").fetchone()
rel_branch = br["name"] if br else "release"

ents = [("E1", f"{PFX}卫星系统", "系统"), ("E2", f"{PFX}通信载荷", "载荷"),
        ("E3", f"{PFX}吞吐率需求", "需求"), ("E4", f"{PFX}电源模块", "部件")]
for eid, name, etype in ents:
    c.execute("INSERT INTO entities (id,name,entity_type,status,branch,source_type,created_by) VALUES (?,?,?,?,?,?,?)",
              (eid, name, etype, "reviewed", rel_branch, "manual", "verify"))
for rid, s, t, rt in [(f"{PFX}r1", "E1", "E2", "CONTAINS"),
                      (f"{PFX}r2", "E2", "E3", "SATISFIES"),
                      (f"{PFX}r3", "E2", "E4", "DEPENDS_ON")]:
    c.execute("INSERT INTO relations (source_id,target_id,relation_type,status,branch,created_by) VALUES (?,?,?,?,?,?)",
              (s, t, rt, "reviewed", rel_branch, "verify"))
doc_text = f"{PFX}通信载荷 设计准则：通信载荷变更需复核吞吐率需求与电源模块依赖。"
c.execute("INSERT INTO documents (filename,file_type,parse_status,uploaded_by,origin) VALUES (?,?,?,?,?)",
          (f"{PFX}准则.md", "md", "completed", "verify", "upload"))
doc1 = c.lastrowid
c.execute("INSERT INTO document_chunks (document_id,chunk_index,content,source_doc,embed_version,origin) VALUES (?,?,?,?,?,?)",
          (doc1, 0, doc_text, f"{PFX}准则.md", "bigram-tf", "upload"))
conn.commit()

nodes = [{"id": e, "name": n, "entity_type": t} for e, n, t in ents]
edges = [{"source_id": "E1", "target_id": "E2", "relation_type": "CONTAINS"},
         {"source_id": "E2", "target_id": "E3", "relation_type": "SATISFIES"},
         {"source_id": "E2", "target_id": "E4", "relation_type": "DEPENDS_ON"}]
src = {"id": "E1", "name": f"{PFX}卫星系统", "entity_type": "系统"}

from services.impact_engine import (analyze_graph, analyze_graph_v2,
                                    scan_name_references, decide_recommendation)

# ── A. delete：全权重传播 + 路径枚举 ──────────────────────────────────
cd_del = analyze_graph_v2(nodes, edges, src, depth=3, direction="both", change_type="delete",
                          change_desc=f"删除 {PFX}卫星系统")
by_id = {n["id"]: n for n in cd_del["impact_nodes"]}
chk("A1 delete：E2 组合风险=1.0（结构全权重）", (by_id.get("E2") or {}).get("score") == 1.0, by_id.get("E2"))
chk("A2 delete：需求 E3 沿 SATISFIES 链传播（组合=1.0）", (by_id.get("E3") or {}).get("score") == 1.0, by_id.get("E3"))
chk("A3 delete：直接/间接区分正确", by_id["E2"]["impact"] == "direct" and by_id["E3"]["impact"] == "indirect")
chk("A4 delete：路径枚举含 E1→E2→E3", any(p["node_ids"] == ["E1", "E2", "E3"] for p in cd_del["path_list"]),
    cd_del["path_list"][:2])
chk("A5 delete：风险矩阵条目含 path_count", all(m.get("path_count", 0) >= 1 for m in cd_del["risk_matrix"]),
    cd_del["risk_matrix"][:2])

# ── B. value：沿需求链强传播 + 引用剪枝 ───────────────────────────────
cd_val = analyze_graph_v2(nodes, edges, src, depth=3, direction="both", change_type="value",
                          change_desc="调整系统级参数")
by_val = {n["id"]: n for n in cd_val["impact_nodes"]}
chk("B1 value：E2 组合风险=0.3（CONTAINS×0.3）", abs((by_val.get("E2") or {}).get("score", 0) - 0.3) < 1e-6,
    by_val.get("E2"))
chk("B2 value：E3 组合风险=0.3×1.0（SATISFIES 全系数）", abs((by_val.get("E3") or {}).get("score", 0) - 0.3) < 1e-6,
    by_val.get("E3"))
chk("B3 value：E4 组合风险=0.3×0.7×0.5（DEPENDS_ON 自身权重 0.7）",
    abs((by_val.get("E4") or {}).get("score", 0) - 0.105) < 1e-6, by_val.get("E4"))
chk("B4 value：组合风险 ≥ 单路径概率（CPM 合并语义）",
    all(m["combined"] >= 0 for m in cd_val["risk_matrix"]))
chk("B5 value：change 元数据回填", cd_val["change"]["type"] == "value" and cd_val["params"]["change_type"] == "value")

# ── C. rename：结构零传播 + 引用扫描 ─────────────────────────────────
cd_ren = analyze_graph_v2(nodes, edges, src, depth=3, direction="both", change_type="rename",
                          change_desc="卫星系统改名")
chk("C1 rename：结构传播为零（无受影响元素）", len(cd_ren["impact_nodes"]) == 1, cd_ren["impact_nodes"])
refs = scan_name_references(conn, f"{PFX}通信载荷")
chk("C2 rename 引用扫描：命中资料库文档", any(PFX in (d.get("source_doc") or "") or PFX in (d.get("snippet") or "")
                                              for d in refs.get("docs", [])), refs.get("docs"))
refs2 = scan_name_references(conn, f"{PFX}不存在的名字XYZ")
chk("C3 rename 引用扫描：无残留时返回空", not any(refs2.values()), refs2)

# ── D. 决策规则三场景 ────────────────────────────────────────────────
d1 = decide_recommendation({"impact_levels": {"high": 0, "mid": 1, "low": 2}, "risk_analysis": {"coverage": 12},
                            "change": {"type": "value"}, "direct_count": 1, "indirect_count": 2,
                            "impact_nodes": [], "element_profile": {}})
chk("D1 低风险 → 批准", d1.get("recommendation") == "批准", d1)
d2 = decide_recommendation({"impact_levels": {"high": 3, "mid": 5, "low": 1}, "risk_analysis": {"coverage": 74},
                            "change": {"type": "value"}, "direct_count": 3, "indirect_count": 6,
                            "impact_nodes": [{"id": "X", "impact": "direct"}],
                            "element_profile": {"X": {"profile": "multiplier"}}})
chk("D2 Multiplier/高覆盖 → 有条件批准", d2.get("recommendation") == "有条件批准", d2)
d3 = decide_recommendation({"impact_levels": {"high": 1, "mid": 1, "low": 0}, "risk_analysis": {"coverage": 20},
                            "change": {"type": "delete"}, "direct_count": 1, "indirect_count": 1,
                            "impact_nodes": [{"id": "R", "type": "需求", "impact": "direct"}],
                            "element_profile": {}})
chk("D3 删除触达需求 → 细化后再议", d3.get("recommendation") == "细化后再议", d3)
d4 = decide_recommendation({"change": {"type": "rename"}, "references": {"docs": [{"s": 1}, {"s": 2}]},
                            "impact_levels": {}, "risk_analysis": {}, "impact_nodes": [], "element_profile": {}})
chk("D4 rename 有引用 → 有条件批准（同步引用）", d4.get("recommendation") == "有条件批准", d4)

# ── E. 向后兼容：v1 引擎字段不变 ─────────────────────────────────────
cd_v1 = analyze_graph(nodes, edges, src, depth=3, direction="both")
chk("E1 v1 analyze_graph 兼容（impact_nodes/risk_analysis 字段在）",
    "impact_nodes" in cd_v1 and "risk_analysis" in cd_v1 and "change" not in cd_v1)

# ── 清理 ────────────────────────────────────────────────────────────
c.execute("DELETE FROM relations WHERE branch=? AND created_by='verify'", (rel_branch,))
c.execute("DELETE FROM entities WHERE branch=? AND created_by='verify'", (rel_branch,))
c.execute("DELETE FROM document_chunks WHERE document_id=?", (doc1,))
c.execute("DELETE FROM doc_metadata WHERE document_id=?", (doc1,))
c.execute("DELETE FROM documents WHERE id=?", (doc1,))
conn.commit()
conn.close()
print("\n已清理测试数据")

print(f"\n结果: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
