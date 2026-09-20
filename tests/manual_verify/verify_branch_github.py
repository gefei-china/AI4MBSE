"""KB分支 GitHub 对标增强验证（P0-1/P0-2/P0-3/P1-1/P1-2 回归）。

覆盖：
1. P1-1 状态机：draft → open → merged/closed，closed 可 reopen；
   draft 不可 approve；驳回必填意见 ≥5 字
2. P0-1 冲突重算：MR open 期间目标分支被更新 → approve 返回 409 conflict_changed
   （含 added 清单），解决后可再通过（GitHub mergeability 持续计算）
3. P0-2 冲突完整性：属性 key 并集（源新增 key 也报冲突）+ delete_modify
   （删除 vs 修改）冲突类型与 keep_delete/keep_modify 决策
4. P1-2 双父指针：merge 提交含 source_branch/source_head_commit；
   分支列表 ahead/behind；diff mode=merge-base 三点式
5. P0-3 操作人真实化：created_by 来自 _actor（匿名兜底「王工」），不再硬编码「李工」

运行前提：本地 8000 服务已加载新代码；在项目根目录执行：
    .venv/Scripts/python.exe tests/manual_verify/verify_branch_github.py
"""
import httpx
import json
import sqlite3
import sys

BASE = "http://127.0.0.1:8000"
c = httpx.Client(timeout=20)
total = passed = 0
TEST_BRANCH = "personal/gh-test"
TEST_BRANCH2 = "personal/gh-test2"   # P0-2 专用（迁移语义下 MR1 合并后源分支实体已迁空）
P_ENT = "TST-GH-P1"    # property 冲突 / 状态机 / 冲突重算测试实体
DM_ENT = "TST-GH-DM"   # delete_modify 测试实体


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


def db_exec(sql, params=()):
    conn = sqlite3.connect("mbse.db")
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def db_one(sql, params=()):
    conn = sqlite3.connect("mbse.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else None


def set_props(branch, eid, props: dict):
    """sqlite 直接改某分支某实体的 properties JSON（测试注入，与 verify_branch_v2 同法）。"""
    cur = db_one("SELECT properties FROM entities WHERE id=? AND branch=?", (eid, branch))
    p = json.loads(cur["properties"]) if cur and cur["properties"] else {}
    p.update(props)
    db_exec("UPDATE entities SET properties=? WHERE id=? AND branch=?",
            (json.dumps(p, ensure_ascii=False), eid, branch))


def get_props(branch, eid):
    row = db_one("SELECT properties, status FROM entities WHERE id=? AND branch=?", (eid, branch))
    return (json.loads(row["properties"]) if row and row["properties"] else {},
            row["status"] if row else None)


# ═════ 0. 幂等清理（上次中断残留）═════
_conn = sqlite3.connect("mbse.db")
_cur = _conn.cursor()
_cur.execute("DELETE FROM relations WHERE branch=?", (TEST_BRANCH,))
_cur.execute("DELETE FROM entities WHERE branch=?", (TEST_BRANCH,))
_cur.execute("DELETE FROM relations WHERE source_id IN (?,?) OR target_id IN (?,?)",
             (P_ENT, DM_ENT, P_ENT, DM_ENT))
_cur.execute("DELETE FROM entities WHERE id IN (?,?)", (P_ENT, DM_ENT))
_cur.execute("DELETE FROM merge_requests WHERE source_branch=? OR target_branch=?", (TEST_BRANCH, TEST_BRANCH))
_cur.execute("DELETE FROM knowledge_commits WHERE branch=? OR source_branch=?",
             (TEST_BRANCH, TEST_BRANCH))
_cur.execute("DELETE FROM branches WHERE name IN (?,?)", (TEST_BRANCH, TEST_BRANCH2))
_conn.commit()
_conn.close()

# ═════ 1. 准备：dev 两个测试实体（sqlite 注入，绕过必填属性校验）+ fork 个人分支 ════
for eid, name, spec in ((P_ENT, "GH验证-属性冲突", "A0"), (DM_ENT, "GH验证-删除修改", "B0")):
    db_exec("""INSERT INTO entities (id, name, entity_type, properties, status, branch, source_type)
               VALUES (?, ?, '部件', ?, 'candidate', 'dev', 'manual')""",
            (eid, name, json.dumps({"spec": spec}, ensure_ascii=False)))
chk("setup: dev 建测试实体", get_props("dev", P_ENT)[0].get("spec") == "A0"
    and get_props("dev", DM_ENT)[0].get("spec") == "B0")
sc, r = api("POST", "/api/branches", json={
    "name": TEST_BRANCH, "branch_type": "personal", "parent_branch": "dev"})
chk("setup: fork 个人分支", sc == 200 or "已存在" in str(r), f"sc={sc} {str(r)[:80]}")

# 通过真实 API 等值更新一次节点（无实质差异、不产生冲突，但产生 manual 提交 → head_commit 有值，
# 供 P1-2 双父指针 source_head_commit 落库）
sc, r = api("PUT", f"/api/knowledge/graph/nodes/{P_ENT}", json={
    "name": "GH验证-属性冲突", "entity_type": "部件", "properties": {"spec": "A0"}, "x": 0, "y": 0, "branch": TEST_BRANCH})
chk("setup: 个人分支等值更新产生提交链", sc == 200 and r.get("ok"), f"sc={sc} {str(r)[:80]}")

# ═════ 2. P0-3 操作人真实化 + P1-1 草稿 ════
sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": TEST_BRANCH, "target_branch": "dev",
    "title": "GitHub 对标验证-状态机", "draft": True})
MR1 = r.get("id")
chk("P1-1: draft 创建成功（status=draft）", sc == 200 and r.get("ok"), f"id={MR1} sc={sc} {str(r)[:100]}")

sc, mrs = api("GET", "/api/branches/merge-requests")
mr1 = next((m for m in mrs if m.get("id") == MR1), {})
chk("P1-1: 草稿状态落库为 draft", mr1.get("status") == "draft", f"status={mr1.get('status')}")
chk("P0-3: created_by 来自 _actor（非硬编码李工）",
    mr1.get("created_by") not in (None, "", "李工"), f"created_by={mr1.get('created_by')}")
chk("P1-1: title 字段落库", mr1.get("title") == "GitHub 对标验证-状态机", f"title={mr1.get('title')}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve", json={"action": "approve"})
chk("P1-1: draft 状态 approve 被拦截（仅 open 可通过）",
    sc == 400 and "open" in r.get("error", ""), f"sc={sc} {str(r)[:80]}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/open")
chk("P1-1: 草稿转正式评审（draft→open）", sc == 200 and r.get("ok"), f"sc={sc} {str(r)[:80]}")

# ═════ 3. P1-1 驳回意见校验 + closed/reopen ════
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve", json={"action": "reject", "review_note": ""})
chk("P1-1: 驳回空意见被拦截", sc == 400 and "意见" in r.get("error", ""), f"sc={sc} {str(r)[:80]}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve", json={"action": "reject", "review_note": "太短"})
chk("P1-1: 驳回意见 <5 字被拦截", sc == 400, f"sc={sc}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve",
            json={"action": "reject", "review_note": "需求覆盖不全，冲突解决后重新提交"})
chk("P1-1: 驳回带意见成功（open→closed）", sc == 200 and r.get("action") == "closed", f"sc={sc} {str(r)[:80]}")

sc, mrs = api("GET", "/api/branches/merge-requests")
mr1 = next((m for m in mrs if m.get("id") == MR1), {})
chk("P1-1: 驳回意见落库（review_note）", "需求覆盖不全" in (mr1.get("review_note") or ""), f"review_note={mr1.get('review_note')}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve", json={"action": "approve"})
chk("P1-1: closed 状态 approve 被拦截", sc == 400, f"sc={sc}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/reopen")
chk("P1-1: closed 重开（reopen→open）", sc == 200 and r.get("action") == "open", f"sc={sc} {str(r)[:80]}")

# ═════ 4. P0-1 冲突重算：open 期间目标分支被更新 ════
set_props("dev", P_ENT, {"spec": "A1-dev-静默修改"})   # dev 侧静默修改，MR 存量清单仍是 0 冲突
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve", json={"action": "approve"})
chk("P0-1: 目标分支静默更新后 approve 返回 409 conflict_changed",
    sc == 409 and r.get("code") == "conflict_changed" and (r.get("added") or r.get("removed")),
    f"sc={sc} {str(r)[:150]}")
added = r.get("added") or []
chk("P0-1: 409 携带新增冲突清单（entity/field 定位）",
    any(a.get("entity_id") == P_ENT and a.get("field") == "spec" for a in added), f"added={added}")

sc, r = api("GET", f"/api/branches/merge-requests/{MR1}/conflicts")
chk("P0-1: 冲突清单已刷新（unresolved=1）且 conflict_type=property",
    r.get("unresolved") == 1 and r["conflicts"][0].get("conflict_type") == "property",
    f"unresolved={r.get('unresolved')}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve-conflict",
            json={"entity_id": P_ENT, "field": "spec", "pick": "source"})
chk("P0-1: 以源分支为准解决新冲突", sc == 200 and r.get("ok"), f"sc={sc} {str(r)[:80]}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve", json={"action": "approve"})
chk("P0-1: 冲突解决后重新 approve 成功（merged）", sc == 200 and r.get("action") == "merged", f"sc={sc} {str(r)[:100]}")
props, _ = get_props("dev", P_ENT)
chk("P0-1: 合并后 dev 采用源分支值（source 决策生效）", props.get("spec") == "A0", f"dev spec={props.get('spec')}")

# ═════ 5. P1-2 双父指针 ════
mc = db_one("""SELECT id, source_branch, source_head_commit FROM knowledge_commits
               WHERE kind='merge' AND branch='dev' AND source_branch=? ORDER BY id DESC LIMIT 1""",
            (TEST_BRANCH,))
chk("P1-2: merge 提交记录源分支（双父指针 source_branch）",
    mc is not None and mc.get("source_branch") == TEST_BRANCH, f"commit={mc}")
chk("P1-2: merge 提交记录源头提交（source_head_commit）",
    mc is not None and mc.get("source_head_commit"), f"source_head_commit={mc and mc.get('source_head_commit')}")

# ═════ 6. P0-2 key 并集 + delete_modify（新 fork 分支：迁移语义下 MR1 合并后源分支已迁空）═════
sc, r = api("POST", "/api/branches", json={
    "name": TEST_BRANCH2, "branch_type": "personal", "parent_branch": "dev"})
chk("setup: fork 第二个个人分支", sc == 200 or "已存在" in str(r), f"sc={sc} {str(r)[:80]}")
set_props("dev", DM_ENT, {})                                # 清理上次运行残留
db_exec("UPDATE entities SET status='candidate' WHERE id=? AND branch='dev'", (DM_ENT,))
set_props("dev", P_ENT, {"spec": "A0"})
db_exec("UPDATE entities SET properties=? WHERE id=? AND branch='dev'",
        (json.dumps({"spec": "A0"}, ensure_ascii=False), P_ENT))
set_props(TEST_BRANCH2, P_ENT, {"gh_new_key": "v-new"})     # 源新增 key（dev 侧没有）→ 并集应报冲突
db_exec("UPDATE entities SET status='deprecated' WHERE id=? AND branch='dev'", (DM_ENT,))
set_props(TEST_BRANCH2, DM_ENT, {"spec": "B1-personal-修改"})  # 源修改 + 目标删除 → delete_modify

sc, r = api("POST", "/api/branches/merge-requests", json={
    "source_branch": TEST_BRANCH2, "target_branch": "dev"})
MR2 = r.get("id")
detail = r.get("detail") or []
dm_conf = next((x for x in detail if x.get("entity_id") == DM_ENT), None)
key_conf = next((x for x in detail if x.get("entity_id") == P_ENT and x.get("field") == "gh_new_key"), None)
chk("P0-2: 源新增属性 key 并集报冲突（不再漏检）", key_conf is not None, f"detail={str(detail)[:150]}")
chk("P0-2: delete_modify 冲突被检出（删除 vs 修改）",
    dm_conf is not None and dm_conf.get("conflict_type") == "delete_modify" and dm_conf.get("field") == "__delete__",
    f"dm={dm_conf}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR2}/resolve-conflict",
            json={"entity_id": DM_ENT, "field": "__delete__", "pick": "source"})
chk("P0-2: delete_modify 不接受 source/target/manual pick", sc == 400 and "keep" in r.get("error", ""), f"sc={sc} {str(r)[:80]}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR2}/resolve-conflict",
            json={"entity_id": DM_ENT, "field": "__delete__", "pick": "keep_modify"})
chk("P0-2: keep_modify 决策记录成功", sc == 200 and r.get("ok"), f"sc={sc} {str(r)[:80]}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR2}/resolve-conflict",
            json={"entity_id": P_ENT, "field": "gh_new_key", "pick": "source"})
chk("P0-2: key 并集冲突按 source 解决", sc == 200 and r.get("ok"), f"sc={sc} {str(r)[:80]}")

sc, r = api("POST", f"/api/branches/merge-requests/{MR2}/resolve", json={"action": "approve"})
chk("P0-2: MR2 合并成功", sc == 200 and r.get("action") == "merged", f"sc={sc} {str(r)[:100]}")
props, st = get_props("dev", DM_ENT)
chk("P0-2: keep_modify 后 dev 实体复活且保留修改值", st != "deprecated" and props.get("spec") == "B1-personal-修改",
    f"status={st} spec={props.get('spec')}")
props, _ = get_props("dev", P_ENT)
chk("P0-2: 源新增 key 合并进 dev（key 并集应用）", props.get("gh_new_key") == "v-new", f"gh_new_key={props.get('gh_new_key')}")

# ═════ 7. P1-2 ahead/behind + merge-base diff ════
sc, branches = api("GET", "/api/branches")
pb = next((b for b in branches if b.get("name") == TEST_BRANCH), {})
chk("P1-2: 分支列表注入 ahead/behind 字段",
    isinstance(pb.get("ahead"), int) and isinstance(pb.get("behind"), int), f"ahead={pb.get('ahead')} behind={pb.get('behind')}")
# MR2（gh-test2→dev）合并又给 dev 新增了提交 → gh-test behind≥1（GitHub 语义：基线领先）
chk("P1-2: ahead=0 且基线领先（behind≥1）", pb.get("ahead") == 0 and pb.get("behind", 0) >= 1,
    f"ahead={pb.get('ahead')} behind={pb.get('behind')}")

sc, r = api("GET", "/api/branches/diff?base=dev&head=" + TEST_BRANCH + "&mode=merge-base")
chk("P1-2: merge-base 三点式 diff 正常返回", sc == 200 and r.get("mode") == "merge-base" and "summary" in r, f"sc={sc} {str(r)[:100]}")
sc, r = api("GET", "/api/branches/diff?base=dev&head=" + TEST_BRANCH + "&mode=bogus")
chk("P1-2: 非法 diff mode 被拦截", sc == 400 and "mode" in r.get("error", ""), f"sc={sc}")

# ═════ 8. P1-1 补充：merged 状态不可 reopen / 不可解决冲突 ════
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/reopen")
chk("P1-1: merged 状态 reopen 被拦截", sc == 400, f"sc={sc} {str(r)[:80]}")
sc, r = api("POST", f"/api/branches/merge-requests/{MR1}/resolve-conflict",
            json={"entity_id": P_ENT, "field": "spec", "pick": "target"})
chk("P1-1: merged 状态解决冲突被拦截", sc == 400 and "已处理" in r.get("error", ""), f"sc={sc} {str(r)[:80]}")

print("\n" + "=" * 60)
print(f"结果: {passed}/{total} PASS" + ("" if passed == total else f"  ❌ {total - passed} FAIL"))
sys.exit(0 if passed == total else 1)
