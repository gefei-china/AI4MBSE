# -*- coding: utf-8 -*-
"""生成报告 2.0 变更影响分析演示：release 真实图谱 → v2 引擎（value 变更）→ 证据/重测/工作量/决策 → 登记会话产物。"""
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

from services.impact_engine import (analyze_graph_v2, attach_evidence,
                                    build_retest_plan, decide_recommendation)

out = []

# 1) 会话：找「变更影响分析」，无则建
conv = conn.execute("SELECT id FROM conversations WHERE title='变更影响分析' AND status!='archived'").fetchone()
if conv:
    conv_id = conv["id"]
    out.append(f"session: reuse #{conv_id}")
else:
    cur = conn.execute("INSERT INTO conversations (title, intent) VALUES ('变更影响分析', 'change')")
    conv_id = cur.lastrowid
    out.append(f"session: created #{conv_id}")

# 2) release 真实图谱基线 + 变更源解析
from services.impact_engine import build_baseline
bb = build_baseline(conn, "巡飞弹", depth=3, direction="both")
if not bb.get("ok", True) or bb.get("error"):
    out.append(f"BASELINE_FAIL: {json.dumps(bb, ensure_ascii=False)[:300]}")
    conn.close()
    open("_gen_out.txt", "w", encoding="utf-8").write("\n".join(out))
    sys.exit(1)
nodes, edges, source = bb["graph"]["nodes"], bb["graph"]["edges"], bb["source"]
out.append(f"baseline: nodes={len(nodes)} edges={len(edges)} source={source.get('name')}")

# 3) v2 引擎（value 参数变更：体现 CPM 传播语义）+ 证据 + 重测/工作量 + 决策
card = analyze_graph_v2(nodes, edges, source, depth=3, direction="both",
                        change_type="value", change_desc="推进剂加注量与巡飞速度参数调整（性能优化，涉及质量/功耗预算）")
attach_evidence(conn, card)
card = build_retest_plan(nodes, edges, card)
card["decisions"] = decide_recommendation(card)

lv = card["impact_levels"]
ra = card["risk_analysis"]
chg = card["change"]
rp = card["retest_plan"]
dec = card["decisions"]
out.append(f"v2: change={chg['type']} direct={card['direct_count']} indirect={card['indirect_count']} "
           f"coverage={ra['coverage']} paths={card['path_count']} retest={rp['total']} "
           f"decision={dec['recommendation']} evidence={len(card.get('evidence') or [])}")

# 4) 报告 sections（八层结构，与前端 saveImpactReportToCenter 同构）
esc = lambda s: str(s or "").replace("|", "/").replace("\n", " ")
src = card["change_source"]
src_name = src.get("name", "变更对象")
affected = [n for n in card["impact_nodes"] if n.get("impact") != "source"]
direct = [n for n in affected if n.get("impact") == "direct"]
ep = card.get("element_profile") or {}

direct_rows = "\n".join(
    f"| {esc(n['name'])} | {esc(n['type'])} | {n.get('score','')} | "
    f"{'🔴 高' if n['level']=='high' else ('🟠 中' if n['level']=='mid' else '🔵 低')} |" for n in direct[:12])
matrix_rows = "\n".join(
    f"| {esc(n['name'])} | {esc(n['type'])} | {n.get('score','')} | {n.get('depth','')} | "
    f"{'直接' if n.get('impact')=='direct' else '间接'} |" for n in affected[:15])
prof_cnt = {"absorber": 0, "carrier": 0, "multiplier": 0}
for p in ep.values():
    if p["profile"] in prof_cnt:
        prof_cnt[p["profile"]] += 1
mults = "、".join(p["name"] for p in sorted(ep.values(), key=lambda x: -x["out_w"])
                  if p["profile"] == "multiplier")[:6] or "无"
rt_rows = "\n".join(
    f"| {esc(it['name'])} | {esc(it['type'])} | {esc(it['reason'])}"
    f"{'（经由 ' + esc(it['via']) + '）' if it.get('via') else ''} | {it.get('score') if it.get('score') is not None else '-'} |"
    for it in rp["items"])
ef_rows = "\n".join(f"| {r['label']} | {r['count']} |  |  |" for r in card["effort_estimate"])
ev_lines = "\n".join(
    f"- **{esc(e['element'])}**（影响度 {e.get('score','')}）："
    + "、".join(f"{h['source_doc']}（{h['score']}）" for h in (e.get("hits") or []))
    for e in (card.get("evidence") or []))
risk_lines = "\n\n".join(
    f"> [!警告] **{r['title']}**：{r.get('desc','')} → {r.get('advice','')}" for r in ra.get("risks") or [])

today = __import__("datetime").date.today().isoformat()
sections = [
    {"heading": "报告信息", "body": f"| 文档编号 | 版本 | 日期 | 密级 | 编制 |\n| --- | --- | --- | --- | --- |\n| CIA-{today.replace('-','')}-R002 | V2.0 | {today} | 内部 | AI 建模助手 |"},
    {"heading": "摘要", "body": (f"变更类型：{chg['type_label']}。本报告基于 release 已发布分支模型知识图谱（CPM-lite：变更类型传播矩阵 + 全路径枚举 + 组合风险），"
        f"对「{src_name}」的参数变更进行影响分析。直接影响 {card['direct_count']} 个、间接 {card['indirect_count']} 个元素，"
        f"高影响 {lv['high']} 个，覆盖率 {ra['coverage']}%，共枚举 {card['path_count']} 条传播路径。"
        f"决策建议：{dec['recommendation']}。")},
    {"heading": "变更上下文", "body": (f"> [!注意] 变更对象：{esc(src_name)}（{esc(src.get('entity_type',''))}）\n>"
        f"\n> 变更类型：{chg['type_label']}\n> 变更内容：{esc(chg.get('desc',''))}\n>\n> 正式变更须经人工确认（BR-2 人在回路）。")},
    {"heading": "决策建议", "body": (f"**⚖ {dec['recommendation']}**\n\n" +
        "\n".join(f"- 依据：{r}" for r in dec.get("reasons", [])) + "\n" +
        "\n".join(f"- 条件：{c}" for c in dec.get("conditions", [])))},
    {"heading": "直接影响分析", "body": f"| 元素 | 类型 | 组合风险 | 级别 |\n| --- | --- | --- | --- |\n{direct_rows or '无'}"},
    {"heading": "影响拓扑图谱", "graph": {"impact_nodes": card["impact_nodes"], "impact_edges": card["impact_edges"],
        "change_source": src, "impact_levels": lv, "depth": card["depth"], "direction": card["direction"],
        "risk_analysis": ra}},
    {"heading": "风险矩阵（CPM 组合风险）", "body": f"| 元素 | 类型 | 组合风险 | 深度 | 类型 |\n| --- | --- | --- | --- | --- |\n{matrix_rows or '无'}"},
    {"heading": "关键度画像（Eckert）", "body": (f"| 画像 | 数量 |\n| --- | --- |\n| 吸收型 | {prof_cnt['absorber']} |\n| 传递型 | {prof_cnt['carrier']} |\n| 放大型 | {prof_cnt['multiplier']} |\n\n"
        f"变更放大器（Multiplier）：{mults}")},
    {"heading": "重测与工作量", "body": (f"**需重测验证活动（{rp['total']} 项）**\n\n| 验证活动 | 类型 | 受影响原因 | 组合风险 |\n| --- | --- | --- | --- |\n{rt_rows or '无'}\n\n"
        f"**工作量估算（人日人工填写）**\n\n| 任务项 | 涉及数量 | 工作量(人日) | 备注 |\n| --- | --- | --- | --- |\n{ef_rows}")},
    {"heading": "混合溯源证据", "body": ev_lines or "暂无向量库补充证据（图谱依赖网络为主证据源）"},
    {"heading": "风险与建议", "body": risk_lines or "> [!注意] 未发现显著风险"},
    {"heading": "结论", "body": (f"变更影响范围共 {len(affected)} 个元素，需重测验证活动 {rp['total']} 项；"
        f"建议按「{dec['recommendation']}」推进" +
        ("，先完成条件项再提交 CCB。" if dec.get("conditions") else "。"))},
]
md = "\n\n".join(f"## {s['heading']}\n\n{s['body']}" for s in sections if "graph" not in s)

# 5) 幂等登记为会话产物（同标题不重复）
from repositories.artifact_repo import ArtifactRepo
repo = ArtifactRepo(conn)
title = f"变更影响分析报告 · {src_name}（2.0）"
if repo.exists(conv_id, 0, "report", title):
    out.append("artifact: exists (skip)")
else:
    aid = repo.create_artifact(conv_id, 0, "report", title, "markdown", md,
                               {"report_type": "impact", "sections": sections,
                                "summary": sections[1]["body"], "graph": sections[5]["graph"]},
                               source="conversation", created_by="AI 建模助手")
    out.append(f"artifact: created #{aid}")
conn.commit()
conn.close()
open("_gen_out.txt", "w", encoding="utf-8").write("\n".join(out))
print("DONE")
