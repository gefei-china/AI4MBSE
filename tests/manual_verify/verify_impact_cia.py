# -*- coding: utf-8 -*-
"""验证变更影响分析三处缺口补齐（2026-09-15，FR-CIA-1/3/4）。

覆盖：
  A. 真实图谱基线：POST /api/impact/baseline（release 分支依赖网络 + 变更源解析，
     含歧义 400 引导）
  B. 沙箱预演真实模型：baseline_graph+source 直通 /api/impact/simulate →
     before/after/comparison + impact_simulations 落库
  C. 混合溯源：services.impact_engine.attach_evidence —— 受影响元素挂向量库文档证据，
     use_vector 真实置位（不再硬编码占位）
  D. 报告 ↔ 分析记录双向关联：link-report 回写 impact_analyses.report_id

用法: python verify_impact_cia.py   （需服务已运行；VERIFY_BASE 可换端口）
"""
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
PFX = f"CIA验证TS{int(time.time()) % 100000}"

total = passed = 0


def chk(name, cond, extra=""):
    global total, passed
    total += 1
    if cond:
        passed += 1
    print(("PASS" if cond else "FAIL") + " | " + name + ((" | " + str(extra)[:200]) if extra else ""))


def api(path, body=None, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw[:300]}


here = os.path.dirname(os.path.abspath(__file__))
proj = os.path.abspath(os.path.join(here, "..", ".."))
dbp = os.path.join(proj, "mbse.db")
conn = sqlite3.connect(dbp)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ── 准备：release 分支 + 测试依赖网络 + 资料库证据文档 ─────────────────
br = c.execute("SELECT name FROM branches WHERE branch_type='release' AND status='active' LIMIT 1").fetchone()
if br:
    rel_branch = br["name"]
else:
    c.execute("INSERT INTO branches (name, branch_type, status, description) VALUES (?,?,?,?)",
              ("release", "release", "active", "verify"))
    rel_branch = "release"
BR = rel_branch
ents = [("E1", f"{PFX}卫星系统", "系统"), ("E2", f"{PFX}通信载荷", "载荷"), ("E3", f"{PFX}吞吐率需求", "需求")]
for eid, name, etype in ents:
    c.execute("""INSERT INTO entities (id, name, entity_type, status, branch, source_type, created_by)
                 VALUES (?,?,?,?,?,?,?)""", (eid, name, etype, "reviewed", BR, "manual", "verify"))
rels = [(f"{PFX}系统包含载荷", "E1", "E2", "CONTAINS"), (f"{PFX}载荷满足需求", "E2", "E3", "SATISFIES")]
for rid, s, t, rt in rels:
    c.execute("""INSERT INTO relations (source_id, target_id, relation_type, status, branch, created_by)
                 VALUES (?,?,?,?,?,?)""", (s, t, rt, "reviewed", BR, "verify"))
doc_text = (f"{PFX} 通信载荷设计准则：吞吐率需求由载荷满足，卫星系统包含通信载荷；"
            f"载荷变更将影响吞吐率需求验证活动与系统级链路预算。")
c.execute("""INSERT INTO documents (filename, file_type, parse_status, chunk_count, uploaded_by, origin)
             VALUES (?,?,?,?,?,?)""", (f"{PFX}设计准则.md", "md", "completed", 1, "verify", "upload"))
doc1 = c.lastrowid
c.execute("""INSERT INTO document_chunks (document_id, chunk_index, content, source_doc, embed_version, origin)
             VALUES (?,?,?,?,?,?)""", (doc1, 0, doc_text, f"{PFX}设计准则.md", "bigram-tf", "upload"))
conn.commit()
chk("准备：release 分支测试网络 + 证据文档就绪", bool(doc1))

# ── A. 真实图谱基线 ──────────────────────────────────────────────────
rb = api("/api/impact/baseline", {"change_source": f"{PFX}卫星系统", "depth": 3, "direction": "both"}, "POST")
chk("baseline：release 分支图谱构建成功", rb.get("ok") is True, rb)
chk("baseline：变更源解析正确", (rb.get("source") or {}).get("id") == "E1", rb.get("source"))
chk("baseline：依赖网络含测试节点", len((rb.get("graph") or {}).get("nodes", [])) >= 3,
    len((rb.get("graph") or {}).get("nodes", [])))

rb2 = api("/api/impact/baseline", {"change_source": PFX}, "POST")
_err2 = rb2.get("error") or rb2.get("detail") or ""   # HTTPException → {"detail": ...}
chk("baseline：歧义变更源 → 400 引导（SOURCE_AMBIGUOUS）",
    bool(_err2) and ("候选" in _err2 or "匹配" in _err2), _err2)

# ── B. 沙箱预演真实模型 ──────────────────────────────────────────────
changes = [{"op": "modify", "target": "E1", "new_value": "载荷配置调整"}]
rs = api("/api/impact/simulate",
         {"baseline_graph": rb.get("graph"), "source": rb.get("source"), "depth": 3,
          "direction": "both", "changes": changes, "title": f"{PFX}真实图谱预演"}, "POST")
chk("simulate：真实基线预演成功", rs.get("ok") is True, rs)
chk("simulate：before/after/comparison 完整", bool(rs.get("before") and rs.get("after") and rs.get("comparison") is not None))
chk("simulate：模拟记录落库（id>0）", (rs.get("id") or 0) > 0, rs.get("id"))
sim_detail = api(f"/api/impact/simulations/{rs.get('id')}")
chk("simulate：详情快照可追溯（含 baseline）", bool(sim_detail.get("baseline_snapshot")), bool(sim_detail.get("baseline_snapshot")))

# ── C. 混合溯源（函数级：attach_evidence）────────────────────────────
sys.path.insert(0, proj)
try:
    from services.impact_engine import attach_evidence
    conn2 = sqlite3.connect(dbp)
    conn2.row_factory = sqlite3.Row
    _card = {"impact_nodes": [
        {"id": "E2", "name": f"{PFX}通信载荷", "type": "载荷", "score": 0.7, "impact": "direct", "level": "high"},
        {"id": "E3", "name": f"{PFX}吞吐率需求", "type": "需求", "score": 0.42, "impact": "indirect", "level": "mid"},
    ]}
    attach_evidence(conn2, _card)
    conn2.close()
    ev = _card.get("evidence") or []
    chk("混合溯源：高影响元素挂上向量库文档证据", any(e["element"] == f"{PFX}通信载荷" for e in ev), ev)
    chk("混合溯源：证据含来源文档与片段", bool(ev and ev[0]["hits"] and ev[0]["hits"][0].get("source_doc")), ev[0] if ev else None)
    chk("混合溯源：use_vector 真实置位（不再硬编码）", (_card.get("params") or {}).get("use_vector") is True,
        _card.get("params"))
finally:
    pass

# ── D. 报告 ↔ 分析记录双向关联 ──────────────────────────────────────
c.execute("""INSERT INTO impact_analyses (title, change_source, params, result, status, conversation_id, created_by)
             VALUES (?,?,?,?,?,?,?)""",
          (f"{PFX}卫星系统", f"{PFX}卫星系统", "{}", "{}", "ok", 0, "verify"))
conn.commit()
analysis_id = c.lastrowid
rep = api("/api/reports", {"title": f"{PFX}影响分析报告", "report_type": "impact",
                           "summary": "verify", "sections": [{"heading": "概述", "body": "verify"}],
                           "source": "conversation", "status": "draft"}, "POST")
chk("报告创建成功（取得 report_id）", rep.get("ok") is True and (rep.get("id") or 0) > 0, rep)
rl = api(f"/api/impact/history/{analysis_id}/link-report", {"report_id": rep.get("id")}, "POST")
chk("link-report 回写成功", rl.get("ok") is True, rl)
row = c.execute("SELECT report_id FROM impact_analyses WHERE id=?", (analysis_id,)).fetchone()
chk("impact_analyses.report_id 已关联（双向追溯闭合）", row and row["report_id"] == rep.get("id"),
    dict(row) if row else None)
hist = api(f"/api/impact/history?q={urllib.parse.quote(PFX)}&limit=5")
chk("history 追溯列表可见且带 report_id", any(h["id"] == analysis_id and h.get("report_id") == rep.get("id") for h in hist))

# ── 清理 ────────────────────────────────────────────────────────────
c.execute("DELETE FROM relations WHERE branch=? AND created_by='verify'", (BR,))
c.execute("DELETE FROM entities WHERE branch=? AND created_by='verify'", (BR,))
c.execute("DELETE FROM document_chunks WHERE document_id=?", (doc1,))
c.execute("DELETE FROM doc_metadata WHERE document_id=?", (doc1,))
c.execute("DELETE FROM documents WHERE id=?", (doc1,))
c.execute("DELETE FROM impact_analyses WHERE id=?", (analysis_id,))
c.execute("DELETE FROM impact_simulations WHERE id=?", (rs.get("id") or 0,))
c.execute("DELETE FROM reports WHERE id=?", (rep.get("id") or 0,))
conn.commit()
conn.close()
print("\n已清理测试数据（audit 留痕保留供审计）")

print(f"\n结果: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
