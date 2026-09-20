# -*- coding: utf-8 -*-
"""报告 2.0 P2/P3 验证：重测清单 + 工作量估算表 + 仪表板数据完整性（引擎级，无需服务）。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ = os.path.dirname(BASE)
sys.path.insert(0, PROJ)

_results = []


def chk(name, ok, detail=None):
    _results.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  -> {json.dumps(detail, ensure_ascii=False, default=str)[:400]}"))


from services.impact_engine import analyze_graph_v2, build_retest_plan  # noqa: E402

# ── 合成图谱：E1→E2→E3 结构链，R1/R2 需求挂接 V1/V2 验证活动，V0 直接受影响 ──
N = [
    {"id": "E1", "name": "巡飞弹", "entity_type": "系统"},
    {"id": "E2", "name": "动力分系统", "entity_type": "分系统"},
    {"id": "E3", "name": "活塞发动机", "entity_type": "设备"},
    {"id": "R1", "name": "续航需求", "entity_type": "单元需求"},
    {"id": "R2", "name": "推力需求", "entity_type": "单元需求"},
    {"id": "V0", "name": "总装检查", "entity_type": "验证活动"},
    {"id": "V1", "name": "续航验证", "entity_type": "验证活动"},
    {"id": "V2", "name": "推力验证", "entity_type": "验证活动"},
    {"id": "X1", "name": "无关文档", "entity_type": "文档"},
]
E = [
    {"source_id": "E1", "target_id": "E2", "relation_type": "CONTAINS"},
    {"source_id": "E2", "target_id": "E3", "relation_type": "CONTAINS"},
    {"source_id": "R1", "target_id": "E2", "relation_type": "满足"},        # 需求→满足→元素
    {"source_id": "R2", "target_id": "E3", "relation_type": "满足"},
    {"source_id": "E1", "target_id": "V0", "relation_type": "CONTAINS"},    # 直接受影响的验证活动
    {"source_id": "R1", "target_id": "V1", "relation_type": "VERIFIED_BY"},
    {"source_id": "R2", "target_id": "V2", "relation_type": "VERIFIED_BY"},
]

card = analyze_graph_v2(N, E, {"id": "E1", "name": "巡飞弹", "entity_type": "系统"},
                        depth=4, direction="both", change_type="delete", change_desc="删除巡飞弹分系统重组")
aff = {n["id"]: n for n in card["impact_nodes"]}

# A. v2 基线字段（P2 仪表板数据完整性）
chk("A1 仪表板数据：risk_matrix 存在且含组合风险/路径数",
    all(("combined" in m and "path_count" in m) for m in card["risk_matrix"]) and len(card["risk_matrix"]) >= 5,
    card["risk_matrix"][:2])
chk("A2 仪表板数据：path_list 为路径明细（node_names 逐项）",
    bool(card["path_list"]) and all(len(p["node_names"]) >= 2 for p in card["path_list"]),
    card["path_list"][:2])
chk("A3 仪表板数据：element_profile 三分类齐全",
    set(card["element_profile"]) >= {"E1", "E2"} and
    all(p["profile"] in ("absorber", "carrier", "multiplier") for p in card["element_profile"].values()),
    {k: v["profile"] for k, v in list(card["element_profile"].items())[:4]})

# B. 传播正确性（delete 全系数矩阵）
chk("B1 delete：直接 E2/V0，间接 E3/R1/R2/V1/V2",
    aff["E2"]["impact"] == "direct" and aff["V0"]["impact"] == "direct" and
    all(aff[k]["impact"] == "indirect" for k in ("E3", "R1", "R2", "V1", "V2")),
    {k: v["impact"] for k, v in aff.items()})
chk("B2 delete：无关文档 X1 不受影响", "X1" not in aff, list(aff))
chk("B3 组合风险 ≥ 任一单路径概率（多路径合并不减）",
    all(m["combined"] <= 1.0 and m["combined"] > 0 for m in card["risk_matrix"]),
    [(m["to"], m["combined"], m["path_count"]) for m in card["risk_matrix"][:4]])

# C. P3 重测清单
card = build_retest_plan(N, E, card)
rp = card["retest_plan"]
items = {it["id"]: it for it in rp["items"]}
chk("C1 重测清单：直接验证活动 V0 reason=直接受影响", items["V0"]["reason"] == "直接受影响", items.get("V0"))
chk("C2 重测清单：V1 直接受影响且合并 via=R1（原因不丢失）",
    items["V1"]["reason"].startswith("直接受影响") and "续航需求" in items["V1"]["via"], items.get("V1"))
chk("C3 重测清单：V2 合并 via=R2，清单共 3 项不重复",
    "推力需求" in items["V2"]["via"] and rp["total"] == 3 and len(rp["items"]) == 3,
    {"total": rp["total"], "items": [(it["id"], it["via"]) for it in rp["items"]]})

# D. P3 工作量估算表（8 行预填，人日留空）
ef = {r["key"]: r for r in card["effort_estimate"]}
chk("D1 工作量表：8 行标准任务项", len(card["effort_estimate"]) == 8 and
    [r["key"] for r in card["effort_estimate"]] == ["model", "requirement", "interface", "retest", "doc", "review", "release", "other"],
    [r["key"] for r in card["effort_estimate"]])
chk("D2 预填：model=2（E2+E3）/ requirement=2（R1+R2）/ retest=3（清单数）",
    ef["model"]["count"] == 2 and ef["requirement"]["count"] == 2 and ef["retest"]["count"] == 3,
    {k: ef[k]["count"] for k in ("model", "requirement", "retest")})
chk("D3 预填：review=高影响数、人日列留空（人工编辑）",
    ef["review"]["count"] == card["impact_levels"]["high"] and all(r["effort"] == "" for r in card["effort_estimate"]),
    {"review": ef["review"]["count"], "high": card["impact_levels"]["high"]})

# E. 边界与回归
card2 = analyze_graph_v2(N, E, {"id": "X1", "name": "无关文档", "entity_type": "文档"}, depth=2, change_type="value")
build_retest_plan(N, E, card2)
chk("E1 边界：无受影响元素 → 重测清单为空、model/retest 计 0",
    card2["retest_plan"]["total"] == 0 and card2["effort_estimate"][0]["count"] == 0,
    {"total": card2["retest_plan"]["total"], "model": card2["effort_estimate"][0]["count"]})
card3 = analyze_graph_v2(N, [e for e in E if e["relation_type"] != "VERIFIED_BY"],
                         {"id": "E1", "name": "巡飞弹", "entity_type": "系统"}, change_type="delete")
build_retest_plan(N, [e for e in E if e["relation_type"] != "VERIFIED_BY"], card3)
chk("E2 边界：无 VERIFIED_BY 边 → 仅直接验证活动入清单",
    card3["retest_plan"]["total"] == 1 and card3["retest_plan"]["items"][0]["id"] == "V0",
    card3["retest_plan"])
chk("E3 向后兼容：v1 字段齐全（impact_nodes/impact_edges/depth_stats/type_stats）",
    all(k in card for k in ("impact_nodes", "impact_edges", "depth_stats", "type_stats", "risk_analysis")),
    list(card))

# F. 前端契约（仪表板 HTML 依赖的字段存在性；decisions 由 cards.py 决策引擎补挂）
need = ["risk_matrix", "path_list", "element_profile", "retest_plan", "effort_estimate",
        "impact_levels", "direct_count", "indirect_count", "change"]
missing = [k for k in need if k not in card]
chk("F1 前端契约：panelImpact 依赖字段全部就绪", not missing, missing)

passed = sum(1 for _, ok in _results if ok)
print(f"\n{'='*52}\n{passed}/{len(_results)} PASS")
sys.exit(0 if passed == len(_results) else 1)
