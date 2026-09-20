"""P0-A/P0-B/P0-C 验证：关系审核队列 + v2g 确认消歧前移（临时数据，验证后清理）。

- P0-A：关系候选确认 → relations(candidate) → /api/knowledge/graph/edges/review 列表
  → 单条 review confirm / PUT 修正后重新 candidate
- P0-C：confirm_candidates 消歧前移 dup_action（align 对齐合并 / skip 跳过 / create 强制新建）
"""
import sys, os, json, time, uuid
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from database import get_db
from ontology_semantics import GraphStore
from vector2graph import confirm_candidates

BASE = "http://127.0.0.1:8000"
PFX = "TST-ABC-"


def http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def ins_cand(conn, name, etype, props=None, match="", ms="none", cid=None,
             rel_source="", rel_target="", rel_type=""):
    cur = conn.execute(
        "INSERT INTO v2g_candidates (batch_id, chunk_id, source_doc, entity_name, entity_type, properties,"
        " status, confidence, matching_status, match_entity_id, rel_source, rel_target, rel_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{PFX}BATCH", cid, f"{PFX}doc.md", name, etype,
         json.dumps(props or {}, ensure_ascii=False), "pending", 0.9, ms, match,
         rel_source, rel_target, rel_type))
    conn.commit()
    return cur.lastrowid


conn = get_db()
ok_all = True

def check(name, cond, extra=""):
    global ok_all
    flag = "PASS" if cond else "FAIL"
    if not cond:
        ok_all = False
    print(f"[{flag}] {name}" + (f"  {extra}" if extra else ""))

# ── 幂等清理：清掉上次崩溃残留（实体 id 可为 V2G- 前缀 → 按 name 一并清理）──
conn.execute("DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM entities WHERE id LIKE ? OR name LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM v2g_candidates WHERE batch_id=?", (f"{PFX}BATCH",))
conn.commit()

gs = GraphStore(conn)

# ════ P0-C：确认入库消歧前移 ════
print("\n== P0-C: 消歧前移 dup_action ==")
# 1) 已有实体 E1（模拟图谱中已存在的实体）
e1 = f"{PFX}E1"
ok, res = gs.create_node(e1, f"{PFX}卫星甲", "卫星系统", {}, "dev/main")
check("建已有实体 E1", ok, str(res))
n_before = conn.execute("SELECT COUNT(*) FROM entities WHERE name LIKE ?", (f"{PFX}%",)).fetchone()[0]

# 测试 chunk（溯源验证；document_id 用已有文档行满足外键）
doc = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()
check("存在测试用文档行", doc is not None, str(doc))
chk = conn.execute(
    "INSERT INTO document_chunks (document_id, content, source_doc, linked_entity_ids)"
    " VALUES (?, '测试片段', ?, '[]')", (doc["id"], f"{PFX}doc.md")).lastrowid
conn.commit()

# 2) 重复候选 → align：不建新实体，chunk 溯源指向 E1
c1 = ins_cand(conn, f"{PFX}卫星甲", "卫星系统", match=e1, ms="dup_high", cid=chk)
r = confirm_candidates(conn, selected_ids=[c1], dup_action="align")
check("align: 计为已入库", r["confirmed"] == 1, str(r["confirmed"]))
check("align: 无新建实体", len(r["created_nodes"]) == 0)
check("align: aligned 返回 E1", len(r["aligned"]) == 1 and r["aligned"][0]["entity_id"] == e1)
row = conn.execute("SELECT linked_entity_ids FROM document_chunks WHERE id=?", (chk,)).fetchone()
linked = json.loads(row["linked_entity_ids"] or "[]") if row else []
check("align: chunk 溯源指向 E1", e1 in linked, str(linked))

# 3) 重复候选 → skip：不入库，标记 rejected（消歧跳过留痕）
c2 = ins_cand(conn, f"{PFX}卫星甲", "卫星系统", match=e1, ms="dup_high")
r = confirm_candidates(conn, selected_ids=[c2], dup_action="skip")
check("skip: 计为跳过", len(r["skipped"]) == 1)
cand = conn.execute("SELECT status, reject_reason FROM v2g_candidates WHERE id=?", (c2,)).fetchone()
check("skip: 候选标记 rejected 留痕", cand["status"] == "rejected" and "消歧跳过" in (cand["reject_reason"] or ""))

# 4) 重复候选 → 默认 create：强制新建独立实体
c3 = ins_cand(conn, f"{PFX}卫星甲", "卫星系统", match=e1, ms="dup_high")
r = confirm_candidates(conn, selected_ids=[c3])
check("create(默认): 新建实体", len(r["created_nodes"]) == 1, str(r["created_nodes"]))
n_mid = conn.execute("SELECT COUNT(*) FROM entities WHERE name LIKE ?", (f"{PFX}%",)).fetchone()[0]
check("create: 实体数 +1", n_mid == n_before + 1)

# 5) 无重复候选 → 正常入库
c4 = ins_cand(conn, f"{PFX}天线乙", "天线", ms="none")
r = confirm_candidates(conn, selected_ids=[c4])
check("无重复: 正常建实体", len(r["created_nodes"]) == 1 and len(r["aligned"]) == 0 and len(r["skipped"]) == 0)

# ════ P0-A：关系审核队列 ════
print("\n== P0-A: 关系审核队列 ==")
# 1) 构造两端节点（已在图谱）
ga, gb = f"{PFX}天线丙", f"{PFX}卫星丁"
ok, _ = gs.create_node(ga, ga, "天线", {}, "dev/main")
check("建关系源节点", ok)
ok, _ = gs.create_node(gb, gb, "天线", {}, "dev/main")
check("建关系目标节点", ok)

# 2) 关系候选确认 → relations(candidate)
c5 = ins_cand(conn, "", "关系候选", rel_source=ga, rel_target=gb, rel_type="连接")
r = confirm_candidates(conn, selected_ids=[c5])
check("关系候选确认: 建边", len(r["edges"]) == 1, str(r["edges"]))
eid = None
for row in conn.execute("SELECT id FROM relations WHERE source_id=? AND target_id=?", (ga, gb)).fetchall():
    eid = row["id"]
time.sleep(1.2)  # WAL 跨进程可见性
st, data = http("GET", f"/api/knowledge/graph/edges/review?status=candidate&limit=100")
check("HTTP edges/review 200", st == 200, str(st))
items = data.get("items", [])
hit = [i for i in items if i.get("source_id") == ga and i.get("target_id") == gb]
check("队列包含新关系(源端名)", any(i.get("source_name") == ga for i in hit) or len(hit) > 0, str(hit)[:120])
check("total 计数一致", data.get("total", 0) >= 1, f"total={data.get('total')}")

# 3) 单条审核 confirm → reviewed
st, rv = http("POST", f"/api/knowledge/graph/edges/{eid}/review", {"action": "confirm"})
check("关系审核 confirm ok", st == 200 and rv.get("ok"), str(rv))
time.sleep(1.2)  # WAL 跨进程可见性
rv_row = conn.execute("SELECT status, reviewed_by FROM relations WHERE id=?", (eid,)).fetchone()
check("关系 status=reviewed", rv_row["status"] == "reviewed")
st, data = http("GET", f"/api/knowledge/graph/edges/review?status=candidate&limit=100")
check("审核后不再出现于 candidate 队列", not any(i.get("id") == eid for i in data.get("items", [])))

# 4) PUT 修正 → 重新 candidate（重新进入审核流）
st, rv = http("PUT", f"/api/knowledge/graph/edges/{eid}", {"relation_type": "连接", "branch": "dev/main"})
check("PUT 修正关系 ok", st == 200 and rv.get("ok"), str(rv))
rv_row = conn.execute("SELECT status FROM relations WHERE id=?", (eid,)).fetchone()
check("修正后 status=candidate 重新待审", rv_row["status"] == "candidate")

# ════ 清理 ════
print("\n== 清理 ==")
conn.execute("DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM entities WHERE id LIKE ? OR name LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM v2g_candidates WHERE batch_id=?", (f"{PFX}BATCH",))
conn.execute("DELETE FROM document_chunks WHERE id=?", (chk,))
conn.commit()
print("CLEANED")

print("\n===== 结论:", "ALL PASS ✅" if ok_all else "HAS FAIL ❌", "=====")
sys.exit(0 if ok_all else 1)
