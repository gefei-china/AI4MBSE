"""HTTP 接口层验证（8001 新代码实例）：
- GET /api/knowledge/graph/edges/review → dup_count/dup_ids/dup_keep_id
- GET /api/knowledge/entities?status=candidate → dup_count/dup_ids
- GET /api/knowledge/v2g/candidates → 关系候选带 rel_matching_status
- POST /api/knowledge/graph/edges/merge → 新接口
- POST /api/knowledge/entities/{id}/merge → 实体合并（复用，供实体审核队列合并按钮）
"""
import sys, os, json, uuid
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from database import init_db, get_db
from ontology_semantics import GraphStore

BASE = "http://127.0.0.1:8001"
PFX = "TST-HTTP-"

ok_all = True

def check(name, cond, extra=""):
    global ok_all
    flag = "PASS" if cond else "FAIL"
    if not cond:
        ok_all = False
    print(f"[{flag}] {name}" + (f"  {extra}" if extra else ""))

def http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

# ── 构造重复数据（经 8001 服务进程同一 DB） ──
init_db()
conn = get_db()
conn.execute("DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM entities WHERE id LIKE ? OR name LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.commit()
gs = GraphStore(conn)
a, b = f"{PFX}A", f"{PFX}B"
gs.create_node(a, f"{PFX}转发器", "转发器", {}, "dev/main")
gs.create_node(b, f"{PFX}REQ-BC-001", "需求", {}, "dev/main")
# 两条同三元组 candidate 边（重复）
e1 = gs.create_edge(a, b, "满足", {"auto": True}, "dev", source_doc=f"{PFX}d1.md")[1]
e2 = gs.create_edge(a, b, "满足", {"auto": True}, "dev", source_doc=f"{PFX}d2.md")[1]
conn.execute("UPDATE relations SET status='candidate' WHERE id IN (?,?)", (e1, e2))
# 两个同名同类型候选实体（一个 reviewed 一个 candidate）
d1, d2 = f"{PFX}DUP-1", f"{PFX}DUP-2"
gs.create_node(d1, f"{PFX}载荷视图", "载荷", {}, "dev/main")
gs.create_node(d2, f"{PFX}载荷视图", "载荷", {}, "dev/main")
conn.execute("UPDATE entities SET status='candidate' WHERE id=?", (d2,))
conn.commit()
conn.close()

# ── 1. 关系审核列表 dup 聚合 ──
st, data = http("GET", "/api/knowledge/graph/edges/review?status=candidate&limit=50")
items = data.get("items") or []
hit = next((x for x in items if x["source_id"] == a and x["target_id"] == b), None)
check("关系审核列表返回 dup_count/dup_ids/dup_keep_id",
      hit is not None and hit.get("dup_count", 0) >= 2 and len(hit.get("dup_ids", [])) >= 2,
      json.dumps({k: hit.get(k) for k in ("dup_count", "dup_ids", "dup_keep_id")} if hit else None, ensure_ascii=False))

# ── 2. 实体审核列表 dup 聚合 ──
st, data = http("GET", "/api/knowledge/entities?status=candidate")
ent_hit = next((x for x in data if x["id"] == d2), None)
check("实体审核列表返回 dup_count/dup_keep_id",
      ent_hit is not None and ent_hit.get("dup_count") == 2 and ent_hit.get("dup_keep_id") == d1,
      json.dumps({k: ent_hit.get(k) for k in ("dup_count", "dup_ids", "dup_keep_id")} if ent_hit else None, ensure_ascii=False))

# ── 3. 关系合并接口 ──
st, data = http("POST", "/api/knowledge/graph/edges/merge", {"keep_id": e1, "dup_id": e2})
check("POST edges/merge 成功", st in (200, 201) and data.get("ok") and data.get("deprecated") == e2, str(data))
st2, data2 = http("POST", "/api/knowledge/graph/edges/merge", {"keep_id": e1, "dup_id": e2})
check("重复合并拦截", st2 == 400, str(data2))
st3, data3 = http("POST", "/api/knowledge/graph/edges/merge", {"keep_id": "abc", "dup_id": "xyz"})
check("非法 id 拦截", st3 == 400, str(data3))

# ── 3.1 接口层审计完整性：audit_logs（relation_merge）+ graph_edit_logs + knowledge_commits ──
conn2 = get_db()
alog = conn2.execute(
    "SELECT * FROM audit_logs WHERE event_type='relation_merge' AND detail LIKE ? ORDER BY id DESC LIMIT 1",
    (f"%{e2}%",)).fetchone()
check("接口审计 audit_logs 留痕（relation_merge）", alog is not None, str(alog))
glog = conn2.execute(
    "SELECT * FROM graph_edit_logs WHERE op='merge_relation' AND payload LIKE ? ORDER BY id DESC LIMIT 1",
    (f"%\"into\": {e1}%",)).fetchone()
check("接口合并 graph_edit_logs 留痕", glog is not None, str(glog))
cmit = conn2.execute(
    "SELECT * FROM knowledge_commits WHERE kind='merge' AND message LIKE ? ORDER BY id DESC LIMIT 1",
    (f"%{e2}%",)).fetchone()
check("接口合并 knowledge_commits kind=merge 打点", cmit is not None, str(cmit))
conn2.close()

# ── 4. 实体合并接口（审核队列合并按钮复用） ──
st4, data4 = http("POST", f"/api/knowledge/entities/{d2}/merge", {"target_id": d1})
check("实体合并接口可用", st4 in (200, 201) and data4.get("ok"), str(data4))

# ── 5. v2g 候选关系消歧字段贯通 ──
st5, data5 = http("GET", "/api/knowledge/v2g/candidates?limit=20")
rel_c = [c for c in data5 if c.get("entity_type") == "关系候选"]
check("v2g 关系候选带 rel_matching_status/rel_match_rel_id",
      (not rel_c) or all("rel_matching_status" in c and "rel_match_rel_id" in c for c in rel_c),
      f"共 {len(rel_c)} 条关系候选")

# ── 清理 ──
conn = get_db()
conn.execute("DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM entities WHERE id LIKE ? OR name LIKE ?", (f"{PFX}%", f"{PFX}%"))
# 接口层产生的审计/打点记录清理（仅本次测试）
conn.execute("DELETE FROM audit_logs WHERE event_type='relation_merge' AND detail LIKE ?", (f"%{e2}%",))
conn.execute("DELETE FROM graph_edit_logs WHERE op='merge_relation' AND payload LIKE ?", (f"%{e1}%",))
conn.execute("DELETE FROM knowledge_commits WHERE message LIKE ?", (f"%{e2}%",))
conn.commit()
conn.close()

print("\n" + ("HTTP ALL PASS" if ok_all else "HTTP SOME FAILED"))
sys.exit(0 if ok_all else 1)
