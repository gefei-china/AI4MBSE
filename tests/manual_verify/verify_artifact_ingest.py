# -*- coding: utf-8 -*-
"""验证 AI 产物收编资料库全链路（2026-09-15，见 docs/AI产物收编资料库方案.md）。

覆盖：
  A. 显式收编：report 产物 → ingest-artifact → documents.origin/source_artifact_id/
     knowledge_category + chunks.origin 向量化落库
  B. 查重门禁：不同产物相同内容 → 409 needs_confirm（相似度≥0.92）→ override 放行
  C. 时效取代：同产物再次收编 → 旧文档 doc_metadata.superseded_by=新文档 id
  D. 来源筛选：GET /api/documents?origin=ai_generated 只含收编文档
  E. 数据基础：收编 chunks origin='ai_generated'（消费隔离 SQL 的数据前提）

用法: python verify_artifact_ingest.py   （需服务已运行；VERIFY_BASE 可换端口）
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
PFX = f"收编验证TS{int(time.time()) % 100000}"

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
dbp = os.path.join(here, "..", "..", "mbse.db")
conn = sqlite3.connect(dbp)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ── 准备：会话 + report 产物（sections 结构化）＋ 相同内容的第二个产物 ──
row = c.execute("SELECT id FROM conversations ORDER BY id LIMIT 1").fetchone()
if row:
    conv_id = row["id"]
else:
    c.execute("INSERT INTO conversations (title) VALUES (?)", ("verify",))
    conv_id = c.lastrowid

_sections = [{"heading": "概述", "body": f"{PFX} 链路预算分析：EIRP 62 dBW，G/T 21 dB/K，雨衰按 ITU-R P.618 取 3.2 dB。"},
             {"heading": "结论", "body": f"{PFX} 全链路余量 4.5 dB，满足可用度 99.9% 要求。"}]
c.execute("""INSERT INTO artifacts (conversation_id, message_id, kind, title, preview_type,
             preview_content, meta, source, created_by) VALUES (?,?,?,?,?,?,?,?,?)""",
          (conv_id, 0, "report", f"{PFX}链路预算报告", "markdown", "",
           json.dumps({"sections": _sections, "summary": f"{PFX} 链路预算分析报告",
                       "report_type": "analysis"}, ensure_ascii=False),
           "conversation", "verify"))
art1 = c.lastrowid
c.execute("""INSERT INTO artifacts (conversation_id, message_id, kind, title, preview_type,
             preview_content, meta, source, created_by) VALUES (?,?,?,?,?,?,?,?,?)""",
          (conv_id, 0, "report", f"{PFX}同内容副本", "markdown", "",
           json.dumps({"sections": _sections, "summary": f"{PFX} 链路预算分析报告",
                       "report_type": "analysis"}, ensure_ascii=False),
           "conversation", "verify"))
art2 = c.lastrowid
conn.commit()
chk("准备：report 产物 ×2 已插入", bool(art1 and art2), (art1, art2))

# ── A. 显式收编 ──────────────────────────────────────────────────────
r1 = api("/api/documents/ingest-artifact",
         {"artifact_id": art1, "knowledge_category": "可复用构件"}, "POST")
chk("收编 report 产物成功", r1.get("ok") is True, r1)
chk("入库管道完成且分块>0", (r1.get("chunk_count") or 0) > 0, r1.get("chunk_count"))
doc1 = r1.get("doc_id") or 0
d1 = c.execute("SELECT origin, source_artifact_id, knowledge_category FROM documents WHERE id=?",
               (doc1,)).fetchone()
chk("documents.origin=ai_generated + source_artifact_id 回写",
    d1 and d1["origin"] == "ai_generated" and d1["source_artifact_id"] == art1, dict(d1) if d1 else None)
chk("knowledge_category 已打标", d1 and d1["knowledge_category"] == "可复用构件",
    d1["knowledge_category"] if d1 else None)
nchunks = c.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=? AND origin='ai_generated'",
                    (doc1,)).fetchone()[0]
chk("chunks 已向量化且 origin=ai_generated", nchunks > 0, nchunks)

# ── D. 来源筛选 ──────────────────────────────────────────────────────
docs_ai = api("/api/documents?origin=ai_generated")
chk("列表 origin=ai_generated 筛选命中", any(d["id"] == doc1 for d in docs_ai))
chk("筛选结果全部为 AI 生成", all(d.get("origin") == "ai_generated" for d in docs_ai))
docs_up = api("/api/documents?origin=upload")
chk("列表 origin=upload 不含收编文档", all(d["id"] != doc1 for d in docs_up))

# ── B. 查重门禁（不同产物、相同内容）────────────────────────────────
r2 = api("/api/documents/ingest-artifact", {"artifact_id": art2}, "POST")
chk("相同内容收编 → 409 needs_confirm", r2.get("needs_confirm") is True, r2)
chk("查重提示相似文档与相似度", r2.get("similar_doc_id") == doc1 and (r2.get("similarity") or 0) >= 0.9, r2)
r2b = api("/api/documents/ingest-artifact",
          {"artifact_id": art2, "override": True}, "POST")
chk("override 覆盖收编成功", r2b.get("ok") is True, r2b)

# ── C. 时效取代（同产物再次收编）────────────────────────────────────
r3 = api("/api/documents/ingest-artifact", {"artifact_id": art1}, "POST")
chk("同产物再次收编 → 直接放行（版本升级语义）", r3.get("ok") is True, r3)
doc3 = r3.get("doc_id") or 0
sup = c.execute("SELECT superseded_by FROM doc_metadata WHERE document_id=?", (doc1,)).fetchone()
chk("旧文档 superseded_by 指向新文档", sup and sup["superseded_by"] == doc3,
    dict(sup) if sup else None)

# ── 清理（测试文档/chunks/产物/落盘副本；audit 留痕保留）────────────
for did in (doc1, doc3, (r2b.get("doc_id") or 0)):
    if did:
        c.execute("DELETE FROM document_chunks WHERE document_id=?", (did,))
        c.execute("DELETE FROM doc_metadata WHERE document_id=?", (did,))
        c.execute("DELETE FROM documents WHERE id=?", (did,))
        fn = c.execute("SELECT filename FROM documents WHERE id=?", (did,)).fetchone()
c.execute("DELETE FROM documents WHERE id IN (?,?,?)", (doc1, doc3, (r2b.get("doc_id") or 0)))
c.execute("DELETE FROM artifacts WHERE id IN (?,?)", (art1, art2))
conn.commit()
# 清理落盘副本（data/uploads/{doc_id}_{safe}）
try:
    for did in (doc1, doc3, (r2b.get("doc_id") or 0)):
        if not did:
            continue
        for f in os.listdir(os.path.join(here, "..", "..", "data", "uploads")):
            if f.startswith(f"{did}_"):
                os.remove(os.path.join(here, "..", "..", "data", "uploads", f))
except Exception:
    pass
conn.close()
print("\n已清理测试数据（audit 留痕保留供审计）")

print(f"\n结果: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
