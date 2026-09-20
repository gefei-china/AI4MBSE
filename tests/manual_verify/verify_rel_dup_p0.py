"""P0-A/P0-B/P0-C 验证：关系候选消歧 + 审核队列重复簇聚合 + 关系去重合并（临时数据，验证后清理）。

- P0-A：_disambiguate_rel 同三元组检测；confirm_candidates 关系候选 dup_action 三态（skip/align/create）
- P0-B：list_entities(status=candidate) 同名同类型聚合 dup_count/dup_ids/dup_keep_id；
        list_relations_for_review 同三元组聚合
- P0-C：merge_relation 属性/来源合并 + dup deprecated 留痕 + 新接口 /api/knowledge/graph/edges/merge
"""
import sys, os, json, time, uuid
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from database import init_db, get_db  # init_db 幂等迁移（补齐 v2g_candidates 新列）
from ontology_semantics import GraphStore
from vector2graph import confirm_candidates, _disambiguate_rel, extract_candidates
from entity_resolver import merge_relation
from repositories.knowledge_repo import KnowledgeRepo

init_db()  # 幂等建表+迁移
conn = get_db()
BASE = "http://127.0.0.1:8000"
PFX = "TST-RDP0-"

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

# ── 幂等清理 ──
conn.execute("DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM entities WHERE id LIKE ? OR name LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM v2g_candidates WHERE batch_id LIKE ?", (f"{PFX}BATCH%",))
conn.commit()

gs = GraphStore(conn)
repo = KnowledgeRepo(conn)

# ════ P0-A：_disambiguate_rel 同三元组检测 ════
print("\n== P0-A: 关系候选消歧检测 ==")
a = f"{PFX}A"; b = f"{PFX}B"; c = f"{PFX}C"
gs.create_node(a, f"{PFX}转发器", "转发器", {}, "dev/main")
gs.create_node(b, f"{PFX}REQ-BC-001", "需求", {}, "dev/main")
gs.create_node(c, f"{PFX}载荷", "载荷", {}, "dev/main")
eid_exist = gs.create_edge(a, b, "满足", {"auto": True}, "dev", source_doc=f"{PFX}doc1.md")
check("建已有边 转发器-满足->REQ-BC-001", eid_exist[0], str(eid_exist))
ms, rid = _disambiguate_rel(conn, f"{PFX}转发器", f"{PFX}REQ-BC-001", "满足")
check("同三元组检测 → dup_high", ms == "dup_high" and rid == str(eid_exist[1]), f"{ms}/{rid}")
ms2, rid2 = _disambiguate_rel(conn, f"{PFX}转发器", f"{PFX}载荷", "满足")
check("不同三元组 → none", ms2 == "none" and rid2 == "", f"{ms2}/{rid2}")
ms3, _ = _disambiguate_rel(conn, f"{PFX}转发器", f"{PFX}REQ-BC-001", "派生")
check("同端点不同关系 → none", ms3 == "none", ms3)
ms4, _ = _disambiguate_rel(conn, f"{PFX}不存在的源", f"{PFX}REQ-BC-001", "满足")
check("端点未入库 → none", ms4 == "none", ms4)

# ════ P0-A：confirm_candidates 关系候选 dup_action 三态 ════
print("\n== P0-A: 关系候选确认三态 ==")
def ins_rel_cand(ms_tag, rel_id, batch):
    cur = conn.execute(
        "INSERT INTO v2g_candidates (batch_id, chunk_id, source_doc, entity_name, entity_type, properties,"
        " rel_type, rel_source, rel_target, status, confidence, rel_matching_status, rel_match_rel_id)"
        " VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
        (batch, 0, f"{PFX}doc.md", f"{PFX}转发器 --满足-- {PFX}REQ-BC-001", "关系候选",
         "{}", "满足", f"{PFX}转发器", f"{PFX}REQ-BC-001", 0.85, ms_tag, rel_id))
    conn.commit()
    return cur.lastrowid

c_skip = ins_rel_cand("dup_high", str(eid_exist[1]), f"{PFX}BATCH-SKIP")
r = confirm_candidates(conn, selected_ids=[c_skip], dup_action="skip")
st = conn.execute("SELECT status, reject_reason FROM v2g_candidates WHERE id=?", (c_skip,)).fetchone()
n_skip = conn.execute("SELECT COUNT(*) FROM relations WHERE source_id=? AND relation_type='满足' AND target_id=?",
                      (a, b)).fetchone()[0]
check("skip → 候选 rejected + 不建新边", st["status"] == "rejected" and "消歧跳过" in (st["reject_reason"] or "")
      and n_skip == 1, f"status={st['status']} n={n_skip}")

c_align = ins_rel_cand("dup_high", str(eid_exist[1]), f"{PFX}BATCH-ALIGN")
r = confirm_candidates(conn, selected_ids=[c_align], dup_action="align")
st = conn.execute("SELECT status FROM v2g_candidates WHERE id=?", (c_align,)).fetchone()
n_align = conn.execute("SELECT COUNT(*) FROM relations WHERE source_id=? AND relation_type='满足' AND target_id=?",
                       (a, b)).fetchone()[0]
check("align → 候选 confirmed + 不建新边", st["status"] == "confirmed" and n_align == 1,
      f"status={st['status']} n={n_align} aligned={len(r.get('aligned',[]))}")

c_create = ins_rel_cand("dup_high", str(eid_exist[1]), f"{PFX}BATCH-CREATE")
r = confirm_candidates(conn, selected_ids=[c_create], dup_action="create")
st = conn.execute("SELECT status FROM v2g_candidates WHERE id=?", (c_create,)).fetchone()
n_create = conn.execute("SELECT COUNT(*) FROM relations WHERE source_id=? AND relation_type='满足' AND target_id=?",
                        (a, b)).fetchone()[0]
new_edge = conn.execute(
    "SELECT id, status FROM relations WHERE source_id=? AND relation_type='满足' AND target_id=? AND status='candidate'",
    (a, b)).fetchone()
check("create → 候选 confirmed + 强制新建边(candidate)", st["status"] == "confirmed" and n_create == 2 and new_edge is not None,
      f"status={st['status']} n={n_create}")

# ════ P0-B：实体审核队列重复簇聚合 ════
print("\n== P0-B: 审核队列重复簇聚合 ==")
e_dup1 = f"{PFX}DUP-1"; e_dup2 = f"{PFX}DUP-2"
gs.create_node(e_dup1, f"{PFX}载荷视图", "载荷", {}, "dev/main")   # status=reviewed (create_node 默认)
gs.create_node(e_dup2, f"{PFX}载荷视图", "载荷", {"x": 1}, "dev/main")
conn.execute("UPDATE entities SET status='candidate' WHERE id=?", (e_dup2,))
conn.commit()
ents = repo.list_entities(status="candidate")
dup_row = next((x for x in ents if x["id"] == e_dup2), None)
check("实体候选列表带 dup_count=2", dup_row is not None and dup_row.get("dup_count") == 2,
      str({k: dup_row.get(k) for k in ("dup_count", "dup_keep_id")} if dup_row else None))
check("实体 dup_ids 含两个成员 + keep 建议 reviewed", dup_row is not None
      and set(dup_row.get("dup_ids", [])) == {e_dup1, e_dup2}
      and dup_row.get("dup_keep_id") == e_dup1, str(dup_row.get("dup_ids")))

# ════ P0-B：关系审核队列重复簇聚合 ════
gs.create_edge(a, b, "满足", {"auto": True}, "dev", source_doc=f"{PFX}doc2.md")  # 第二条重复边 (create 分支已建一条)
rels = repo.list_relations_for_review(status="candidate", limit=100)
rel_row = next((x for x in rels if x["source_id"] == a and x["relation_type"] == "满足" and x["target_id"] == b), None)
check("关系候选列表带 dup_count>=2", rel_row is not None and (rel_row.get("dup_count") or 0) >= 2,
      f"dup_count={rel_row.get('dup_count') if rel_row else None} dup_ids={rel_row.get('dup_ids') if rel_row else None}")

# ════ P0-C：merge_relation 合并 ════
print("\n== P0-C: 关系去重合并 ==")
keep_id = new_edge["id"] if new_edge else None
check("存在待合并 candidate 边", keep_id is not None, str(keep_id))
if keep_id:
    conn.execute("UPDATE relations SET properties=? WHERE id=?",
                 (json.dumps({"auto": True, "src": "keep"}, ensure_ascii=False), keep_id))
    # 再建一条 candidate 重复边作为 dup（含独有属性/来源）
    dup_id = gs.create_edge(a, b, "满足", {"dup_only": 1, "auto": True}, "dev", source_doc=f"{PFX}doc3.md")[1]
    conn.execute("UPDATE relations SET status='candidate', source_doc=? WHERE id=?",
                 (f"{PFX}doc3.md", dup_id))
    conn.commit()
    mr = merge_relation(conn, keep_id, dup_id, operator="李工")
    check("merge_relation ok", mr.get("ok"), str(mr))
    keep_after = conn.execute("SELECT * FROM relations WHERE id=?", (keep_id,)).fetchone()
    props = json.loads(keep_after["properties"] or "{}")
    dup_after = conn.execute("SELECT status, reviewed_by FROM relations WHERE id=?", (dup_id,)).fetchone()
    check("dup 属性并入 keep", props.get("dup_only") == 1 and props.get("src") == "keep", str(props))
    check("来源文档合并", f"{PFX}doc3.md" in (keep_after["source_doc"] or ""), keep_after["source_doc"])
    check("dup 置 deprecated 留痕", dup_after["status"] == "deprecated" and dup_after["reviewed_by"] == "李工",
          str(dict(dup_after)))

    # ── 审计日志完整性：graph_edit_logs（payload/operator）+ knowledge_commits（kind=merge）──
    glog = conn.execute(
        "SELECT * FROM graph_edit_logs WHERE op='merge_relation' AND node_id=? ORDER BY id DESC LIMIT 1",
        (str(dup_id),)).fetchone()
    check("审计① graph_edit_logs 记录存在", glog is not None, str(glog))
    if glog:
        gpayload = json.loads(glog["payload"] or "{}")
        check("审计① payload 完整（into/props_merged/keep_source）",
              gpayload.get("into") == keep_id
              and set(gpayload.get("props_merged") or []) == {"dup_only"}
              and gpayload.get("keep_source") == f"{PFX}doc.md",  # 合并前 keep 来源
              json.dumps(gpayload, ensure_ascii=False))
        check("审计① operator 留痕", glog["operator"] == "李工", glog["operator"])
    cmit = conn.execute(
        "SELECT * FROM knowledge_commits WHERE kind='merge' AND message LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{dup_id}%",)).fetchone()
    check("审计② knowledge_commits kind=merge 打点", cmit is not None, str(cmit))
    if cmit:
        cchanges = json.loads(cmit["changes"] or "{}")
        csnap = json.loads(cmit["snapshot"] or "{}")
        check("审计② changes 含 keep/dup 边 + created_by 留痕",
              cchanges.get("relations") and set(cchanges["relations"]) == {keep_id, dup_id}
              and cmit["created_by"] == "李工", f"changes={cchanges} by={cmit['created_by']}")
        check("审计② snapshot 含合并摘要", csnap.get("keep_id") == keep_id and csnap.get("dup_id") == dup_id,
              json.dumps(csnap, ensure_ascii=False))
    # 重复合并应报错
    mr2 = merge_relation(conn, keep_id, dup_id, operator="李工")
    check("重复合并被拦截", not mr2.get("ok") and "已废弃" in (mr2.get("error") or ""), str(mr2))

# ════ P0-A：extract_candidates 关系候选新列贯通（Mock 抽取节点名=类型词，打标正确性由 _disambiguate_rel 单测覆盖） ════
print("\n== P0-A: extract_candidates 关系候选消歧字段贯通 ==")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
try:
    res = extract_candidates(conn, f"{PFX}转发器 满足 {PFX}REQ-BC-001", top_k=1, batch_id=f"{PFX}BATCH-EXT")
    rel_cands = [c for c in (res.get("candidates") or []) if c.get("entity_type") == "关系候选"]
    check("抽取关系候选带 rel_matching_status/rel_match_rel_id 字段",
          len(rel_cands) > 0 and all("rel_matching_status" in c and "rel_match_rel_id" in c for c in rel_cands),
          str([(c.get("rel_type"), c.get("rel_matching_status"), c.get("rel_match_rel_id")) for c in rel_cands[:3]]))
finally:
    os.environ.pop("MBSE_LLM_FORCE_MOCK", None)

# ── 清理 ──
conn.execute("DELETE FROM v2g_candidates WHERE batch_id LIKE ?", (f"{PFX}BATCH%",))
conn.execute("DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?", (f"{PFX}%", f"{PFX}%"))
conn.execute("DELETE FROM entities WHERE id LIKE ? OR name LIKE ?", (f"{PFX}%", f"{PFX}%"))
# 审计记录清理（graph_edit_logs / knowledge_commits 仅本次测试产生）
conn.execute("DELETE FROM graph_edit_logs WHERE op='merge_relation' AND payload LIKE ?", (f"%{PFX}%",))
conn.execute("DELETE FROM knowledge_commits WHERE message LIKE ?", (f"%{PFX}%",))
conn.commit()
conn.close()

print("\n" + ("ALL PASS" if ok_all else "SOME FAILED"))
sys.exit(0 if ok_all else 1)
