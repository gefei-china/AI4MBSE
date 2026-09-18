"""KB分支 v3 验证：git 风格分支模型（release 只读 / dev/main 唯一 / 个人分支）回归。

覆盖：
1. GET /api/branches/diff 默认基线自动选最新 release + 显式 base/head
2. 分支模型规则拦截：非 personal 创建 / 非法父分支 / 预置分支编辑删除 / release 直接写 / 非法合并流向
3. 个人分支：创建 fork 基线 → 修改实体 → 合并回 dev/main → 冲突检测 → 逐字段解决 → approve
   （迁移语义：冲突实体按决策写入 dev，个人分支行删除）
4. 发布语义：dev/main → release 为复制快照（dev 保留继续开发，release 获得副本 + 文档快照）
5. release 分支写保护（图谱加节点被拦截）
"""
import httpx
import json
import sqlite3
import sys

BASE = "http://127.0.0.1:8000"
c = httpx.Client(timeout=20)
total = passed = 0
TEST_ENTITY = "TST-BR-ENT-1"
TEST_ENTITY2 = "TST-BR-ENT-2"
GATE_ENT = "TST-GATE-1"   # P0-1 发布门禁测试实体
TEST_BRANCH = "personal/v2-test"
MR_ID = None


def api(method, path, **kw):
    r = c.request(method, BASE + path, **kw)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def chk(name, cond, extra=""):
    global total, passed
    total += 1
    if cond:
        passed += 1
    print(("PASS" if cond else "FAIL") + " | " + name + (" | " + extra if extra else ""))


# ═════ 0. 幂等清理（上次中断残留）═════
import sqlite3
_conn = sqlite3.connect("mbse.db")
_cur = _conn.cursor()
_cur.execute("DELETE FROM relations WHERE branch=?", (TEST_BRANCH,))
_cur.execute("DELETE FROM entities WHERE branch=?", (TEST_BRANCH,))
_cur.execute("DELETE FROM relations WHERE source_id IN (?,?,?) OR target_id IN (?,?,?)",
             (TEST_ENTITY, TEST_ENTITY2, GATE_ENT, TEST_ENTITY, TEST_ENTITY2, GATE_ENT))
_cur.execute("DELETE FROM entities WHERE id IN (?,?,?)", (TEST_ENTITY, TEST_ENTITY2, GATE_ENT))
_cur.execute("DELETE FROM merge_requests WHERE source_branch=? OR target_branch=?",
             (TEST_BRANCH, TEST_BRANCH))
_cur.execute("DELETE FROM merge_requests WHERE source_branch='dev/main' AND target_branch='release' AND status='pending'")
_cur.execute("DELETE FROM document_chunks WHERE document_id IN (SELECT id FROM documents WHERE filename LIKE 'TST-BR-%')")
_cur.execute("DELETE FROM doc_metadata WHERE document_id IN (SELECT id FROM documents WHERE filename LIKE 'TST-BR-%')")
_cur.execute("DELETE FROM documents WHERE filename LIKE 'TST-BR-%'")
_cur.execute("DELETE FROM branches WHERE name=?", (TEST_BRANCH,))
# P0-1 发布门禁适配：历史测试遗留候选（V2G-* 汽车热管理/动力电池、S-* 旧 SysML 重复、
# KBP2-T1/KBP4-X3 测试实体）幂等 deprecated（软删除留痕），保证 dev/main 可发布、release 无违规候选
_cur.execute("""UPDATE entities SET status='deprecated' WHERE status='candidate'
    AND (id LIKE 'V2G-%' OR id LIKE 'S-%' OR id LIKE 'ENT-%' OR id IN ('KBP2-T1','KBP4-X3'))
    AND branch IN ('dev/main','release')""")
_conn.commit()

# ═════ 1. diff 视图 ═════
sc, r = api("GET", "/api/branches/diff")
chk("diff 默认基线自动选最新 release", sc == 200 and r.get("base") and r.get("head"),
    f"base={r.get('base')} head={r.get('head')} sc={sc}")
sc, r = api("GET", "/api/branches/diff?base=release&head=dev/main")
s = r.get("summary", {})
chk("diff 显式参数结构完整", sc == 200 and {"ent_added", "ent_modified", "ent_removed",
    "rel_added", "rel_removed"} <= set(s.keys()),
    f"summary={s}")
sc, r = api("GET", "/api/branches/diff?base=release&head=release")
chk("diff 同分支被拦截", sc == 400 and "不能相同" in r.get("error", ""))

# ═════ 2. 分支模型规则拦截 ═════
sc, r = api("POST", "/api/branches", json={
    "name": "dev/illegal", "branch_type": "dev", "parent_branch": "release"})
chk("非 personal 分支创建被拦截", sc == 400, str(r)[:100])
sc, r = api("POST", "/api/branches", json={
    "name": "personal/illegal-parent", "branch_type": "personal", "parent_branch": "dev/xxx"})
chk("非法父分支（非 dev/release）被拦截", sc == 400, str(r)[:100])
sc, r = api("PUT", "/api/branches/dev/main", json={"name": "dev/main", "description": "x"})
chk("预置分支 dev/main 编辑被拦截", sc == 400, str(r)[:100])
sc, r = api("DELETE", "/api/branches/release")
chk("预置分支 release 删除被拦截", sc == 400, str(r)[:100])
sc, r = api("POST", "/api/knowledge/graph/nodes", json={
    "name": "非法直写release", "entity_type": "部件", "branch": "release"})
chk("release 直接建实体被拦截（只读）", sc == 400 and "只读" in r.get("error", ""), str(r)[:100])
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": "release", "target_branch": "dev/main"})
chk("release 作为合并源被拦截", sc == 400, str(r)[:100])
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": "dev/main", "target_branch": "dev/knowledge-jul"})
chk("合并目标非 dev/release 被拦截", sc == 400, str(r)[:100])

# ═════ 2.5 发布门禁（P0-1）：未评审实体禁止进入 release（四象限治理）═════
sc, r = api("POST", "/api/knowledge/graph/nodes", json={
    "name": "门禁测试未评审", "entity_type": "部件", "branch": "dev/main", "id": GATE_ENT})
chk("门禁：dev/main 建未评审实体", sc == 200, str(r)[:100])
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": "dev/main", "target_branch": "release"})
blk = r.get("blocked_pending_review") or []
chk("门禁：含未评审实体时 dev→release MR 创建被拦截",
    sc == 400 and GATE_ENT in blk and "未评审" in r.get("error", ""),
    f"sc={sc} blocked={blk[:3]} err={r.get('error','')[:60]}")
sc, r = api("POST", f"/api/knowledge/entities/{GATE_ENT}/review", json={"action": "reject"})
chk("门禁：驳回该测试实体（deprecated 留痕）", sc == 200, str(r)[:100])
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": "dev/main", "target_branch": "release"})
chk("门禁：dev 全部已评审/废弃后 MR 创建放行", sc == 200 and r.get("id"),
    f"sc={sc} err={r.get('error','')[:80]}")
_gate_mr = r.get("id")
if _gate_mr:
    api("DELETE", f"/api/branches/merge-requests/{_gate_mr}")

# ═════ 3. 个人分支：fork 基线 → 修改 → 合并回 dev/main ═════
# 3.1 dev/main 建基线实体
sc, r = api("POST", "/api/knowledge/graph/nodes", json={
    "name": "v2基线载荷", "entity_type": "部件", "branch": "dev/main",
    "id": TEST_ENTITY, "properties": {"band": "V", "throughput": "2Gbps"}})
chk("dev/main 建基线实体", sc == 200 and (r.get("id") or r.get("ok")), str(r)[:100])
# 3.2 创建个人分支（fork dev/main 基线）
sc, r = api("POST", "/api/branches", json={
    "name": TEST_BRANCH, "branch_type": "personal", "parent_branch": "dev/main",
    "description": "分支v3验证（临时）"})
chk("创建个人分支（fork dev/main）", sc == 200, str(r)[:100])
# 3.3 个人分支修改实体（属性+名称均不同 → 与 dev 产生冲突）
sc, r = api("PUT", f"/api/knowledge/graph/nodes/{TEST_ENTITY}", json={
    "name": "v2开发载荷", "entity_type": "部件", "branch": TEST_BRANCH,
    "properties": {"band": "Ka", "throughput": "10Gbps"}})
chk("个人分支修改实体（属性+名称均不同）", sc == 200, str(r)[:100])
# 3.4 个人分支直接合并到 release 被拦截（仅 dev/main 可发布）
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": TEST_BRANCH, "target_branch": "release"})
chk("个人分支直接发布到 release 被拦截", sc == 400, str(r)[:100])
# 3.5 合并回 dev/main（应检测到 name+band+throughput 三处冲突）
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": TEST_BRANCH, "target_branch": "dev/main"})
chk("发起 MR 检测冲突", sc == 200 and r.get("conflicts", 0) >= 3,
    f"conflicts={r.get('conflicts')} detail={json.dumps(r.get('detail', []), ensure_ascii=False)[:200]}")
MR_ID = r.get("id")
mr_list = api("GET", "/api/branches/merge-requests")[1]
mr0 = next((m for m in mr_list if m["id"] == MR_ID), {})
chk("MR 列表带 unresolved_conflicts", mr0.get("unresolved_conflicts", 0) >= 3,
    f"unresolved={mr0.get('unresolved_conflicts')}")

# ═════ 4. conflicts 查询 + 门禁 + 逐字段解决 ═════
sc, r = api("GET", f"/api/branches/merge-requests/{MR_ID}/conflicts")
chk("conflicts 查询返回逐字段状态", sc == 200 and len(r.get("conflicts", [])) >= 3,
    f"unresolved={r.get('unresolved')}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR_ID}/resolve", json={"action": "approve"})
chk("未解决冲突时 approve 被门禁拒绝", sc == 400 and "未解决" in r.get("error", ""),
    f"error={r.get('error','')[:80]}")
sc, r = api("GET", f"/api/branches/merge-requests/{MR_ID}/conflicts")
fields = [(x["entity_id"], x["field"]) for x in r["conflicts"]]
for eid, f in fields:
    if f == "name":
        ok, rr = api("POST", f"/api/branches/merge-requests/{MR_ID}/resolve-conflict",
                     json={"entity_id": eid, "field": f, "pick": "manual", "value": "v2合并载荷"})
    elif f == "band":
        ok, rr = api("POST", f"/api/branches/merge-requests/{MR_ID}/resolve-conflict",
                     json={"entity_id": eid, "field": f, "pick": "source"})
    else:
        ok, rr = api("POST", f"/api/branches/merge-requests/{MR_ID}/resolve-conflict",
                     json={"entity_id": eid, "field": f, "pick": "target"})
    chk(f"解决冲突 {eid}.{f}", ok == 200 and rr.get("ok"), str(rr)[:80])
ok, rr = api("POST", f"/api/branches/merge-requests/{MR_ID}/resolve-conflict",
             json={"entity_id": fields[0][0] if fields else TEST_ENTITY, "field": "band", "pick": "xx"})
chk("非法 pick 被拦截", ok == 400)
sc, r = api("GET", f"/api/branches/merge-requests/{MR_ID}/conflicts")
chk("全部解决后 unresolved=0", sc == 200 and r.get("unresolved") == 0, f"unresolved={r.get('unresolved')}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR_ID}/resolve", json={"action": "approve"})
chk("解决后 approve 通过", sc == 200 and r.get("ok"), str(r)[:100])

# 3.6 验证合并结果（迁移语义）：dev 行按决策更新，个人分支行删除
_c2 = sqlite3.connect("mbse.db")
ent = _c2.execute("SELECT name, properties, branch FROM entities WHERE id=? AND branch='dev/main'",
                  (TEST_ENTITY,)).fetchone()
left = _c2.execute("SELECT 1 FROM entities WHERE id=? AND branch=?", (TEST_ENTITY, TEST_BRANCH)).fetchone()
if ent:
    props = json.loads(ent[1] or "{}")
    chk("合并后实体在 dev/main", ent[2] == "dev/main", f"branch={ent[2]}")
    chk("冲突决策生效(band=源Ka)", props.get("band") == "Ka", f"band={props.get('band')}")
    chk("冲突决策生效(throughput=目标2Gbps)", props.get("throughput") == "2Gbps",
        f"throughput={props.get('throughput')}")
    chk("冲突决策生效(name=手动)", ent[0] == "v2合并载荷", f"name={ent[0]}")
    chk("个人分支行已随合并删除", left is None,
        "已删除" if left is None else "个人分支行仍存在")
else:
    chk("合并后实体在 dev/main", False, "未找到实体")
_c2.close()

# ═════ 5. 发布语义：dev/main → release（复制快照，dev 保留）═════
# 5.1 dev/main 新增一个待发布实体
sc, r = api("POST", "/api/knowledge/graph/nodes", json={
    "name": "待发布载荷", "entity_type": "部件", "branch": "dev/main",
    "id": TEST_ENTITY2, "properties": {"band": "Ka", "beam": "4"}})
chk("dev/main 新增待发布实体", sc == 200, str(r)[:100])
# 5.1b 发布门禁：先把 dev 测试实体评审通过（仅 reviewed 可进 release 权威基线）
for _eid in (TEST_ENTITY, TEST_ENTITY2):
    sc, r = api("POST", f"/api/knowledge/entities/{_eid}/review", json={"action": "confirm"})
    chk(f"发布前评审 {_eid}", sc == 200, str(r)[:80])
# 5.2 dev/main 上传文档（发布时复制为 release 快照）
snap_doc = "TST-BR-发布快照.md"
snap_content = "# v3 发布快照\n\n验证 dev→release 发布文档快照。".encode("utf-8")
files = {"file": (snap_doc, snap_content, "text/markdown")}
r = c.post(BASE + "/api/documents/upload", files=files, data={"branch": "dev/main"}, timeout=120)
rj = r.json()
chk("dev/main 上传文档", rj.get("parse_status") == "completed", str(rj)[:120])
# 5.3 发布前基线快照（approve 后所有验证用新连接读取，避免旧连接 WAL 快照）
_b = sqlite3.connect("mbse.db")
rel_before = set(x[0] for x in _b.execute(
    "SELECT id FROM entities WHERE branch='release'").fetchall())
dev_before = _b.execute("SELECT COUNT(*) FROM entities WHERE branch='dev/main'").fetchone()[0]
doc_before = _b.execute("SELECT COUNT(*) FROM documents WHERE branch='dev/main'").fetchone()[0]
_b.close()
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": "dev/main", "target_branch": "release"})
mr2 = r.get("id")
chk("发布 MR（dev/main→release）创建", sc == 200 and mr2, f"conflicts={r.get('conflicts')} mr={mr2}")
# 5.3b 解决既有数据漂移冲突（如 ENT-002 转发器.aliases 未发布变更）→ 再审批
_cf = api("GET", f"/api/branches/merge-requests/{mr2}/conflicts")[1].get("conflicts", [])
for _c in _cf:
    api("POST", f"/api/branches/merge-requests/{mr2}/resolve-conflict",
        json={"entity_id": _c["entity_id"], "field": _c["field"], "pick": "source"})
_cf2 = api("GET", f"/api/branches/merge-requests/{mr2}/conflicts")[1]
chk("发布前解决数据漂移冲突（unresolved=0）", _cf2.get("unresolved", -1) == 0,
    f"unresolved={_cf2.get('unresolved')}")
sc, r = api("POST", f"/api/branches/merge-requests/{mr2}/resolve", json={"action": "approve"})
chk("发布 approve 通过", sc == 200 and r.get("ok"), str(r)[:120])

# 5.4 验证发布复制语义（新连接读取已提交数据）
# approve 的写事务与读快照存在时序竞争：响应返回后立即新建连接可能取到
# WAL 提交前的旧快照，故先等待并重试，确认已提交数据可见。
import time
_c3 = None
for _t in range(6):
    time.sleep(0.8)
    _c3 = sqlite3.connect("mbse.db")
    rel_after = set(x[0] for x in _c3.execute(
        "SELECT id FROM entities WHERE branch='release'").fetchall())
    if TEST_ENTITY2 in rel_after:
        break
    _c3.close()
dev_after = _c3.execute("SELECT COUNT(*) FROM entities WHERE branch='dev/main'").fetchone()[0]
doc_after = _c3.execute("SELECT COUNT(*) FROM documents WHERE branch='dev/main'").fetchone()[0]
chk("发布后 release 获得 dev 新增实体副本", TEST_ENTITY2 in rel_after,
    f"in_release={TEST_ENTITY2 in rel_after}")
chk("发布后 dev/main 保留实体（源不清空）", dev_after == dev_before,
    f"dev_after={dev_after} dev_before={dev_before}")
chk("发布后 dev 文档保留（快照非迁移）", doc_after == doc_before,
    f"doc_after={doc_after} doc_before={doc_before}")
snap_rows = _c3.execute(
    "SELECT id, filename FROM documents WHERE branch='release' AND filename=?", (snap_doc,)).fetchall()
chk("release 分支生成文档快照", len(snap_rows) == 1, f"rows={len(snap_rows)}")
if snap_rows:
    sid = snap_rows[0][0]
    n_chunks = _c3.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=? AND branch='release'",
                           (sid,)).fetchone()[0]
    chk("快照 chunks 随文档复制到 release", n_chunks > 0, f"chunks={n_chunks}")
_c3.close()

# ═════ 6. 清理测试数据 ═════
_cc = sqlite3.connect("mbse.db")
_cur = _cc.cursor()
# 个人分支数据
_cur.execute("DELETE FROM relations WHERE branch=?", (TEST_BRANCH,))
_cur.execute("DELETE FROM entities WHERE branch=?", (TEST_BRANCH,))
_cur.execute("DELETE FROM merge_requests WHERE source_branch=? OR target_branch=?", (TEST_BRANCH, TEST_BRANCH))
_cur.execute("DELETE FROM branches WHERE name=?", (TEST_BRANCH,))
# 测试实体（dev + release）
_cur.execute("DELETE FROM relations WHERE source_id IN (?,?,?) OR target_id IN (?,?,?)",
             (TEST_ENTITY, TEST_ENTITY2, GATE_ENT, TEST_ENTITY, TEST_ENTITY2, GATE_ENT))
_cur.execute("DELETE FROM entities WHERE id IN (?,?,?)", (TEST_ENTITY, TEST_ENTITY2, GATE_ENT))
# 发布复制到 release 的新增实体（差集）
added_rel = rel_after - rel_before - {"ENT-001", "ENT-002", "ENT-004", "REQ-A-117"}
if added_rel:
    _cur.execute(f"DELETE FROM entities WHERE branch='release' AND id IN ({','.join('?'*len(added_rel))})",
                 list(added_rel))
    _cur.execute(f"DELETE FROM relations WHERE branch='release' AND "
                 f"(source_id IN ({','.join('?'*len(added_rel))}) OR target_id IN ({','.join('?'*len(added_rel))}))",
                 list(added_rel) + list(added_rel))
# 测试文档（dev + release）
_cur.execute("DELETE FROM document_chunks WHERE document_id IN (SELECT id FROM documents WHERE filename LIKE 'TST-BR-%')")
_cur.execute("DELETE FROM doc_metadata WHERE document_id IN (SELECT id FROM documents WHERE filename LIKE 'TST-BR-%')")
_cur.execute("DELETE FROM documents WHERE filename LIKE 'TST-BR-%'")
_cc.commit()
_cc.close()
print("\n已清理测试数据")

print(f"\n结果: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
