"""分支管理优化：API 层端到端验证（创建/编辑/删除/合并）。
注意：DB 查询一律重新建立连接，避免 SQLite 连接快照导致误报。
"""
import json
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def api(path, body=None, method="GET"):
    if method == "GET" and body is not None:
        method = "POST"
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw[:200]}


def dbq(sql, params=()):
    """每次新连接查询（避开快照）。"""
    conn = sqlite3.connect("mbse.db")
    try:
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


PASS = []


def chk(name, cond, info=""):
    PASS.append(bool(cond))
    print(("✅" if cond else "❌"), name, ("| " + info if info else ""))


# ── 1. 创建分支（含校验）──
r = api("/api/branches", {"name": "dev/opt-demo", "branch_type": "dev",
                          "parent_branch": "release", "description": "分支优化验证"})
chk("创建分支 dev/opt-demo", r.get("ok"), str(r))

r = api("/api/branches", {"name": "dev/opt-demo"})
chk("重名创建被拦截", "已存在" in r.get("error", ""), str(r))

r = api("/api/branches", {"name": "bad name!@#", "branch_type": "dev"})
chk("非法名称被拦截", "只能包含" in r.get("error", ""), str(r))

r = api("/api/branches", {"name": "dev/opt-xx", "branch_type": "unknown"})
chk("非法类型被拦截", "类型" in r.get("error", ""), str(r))

r = api("/api/branches", {"name": "dev/opt-badparent", "branch_type": "dev", "parent_branch": "no-exist"})
chk("不存在父分支被拦截", "父分支" in r.get("error", ""), str(r))

# ── 2. 在分支上建实体（graph API 带 branch）──
r = api("/api/knowledge/graph/nodes", method="POST",
        body={"name": "优化测试实体", "entity_type": "部件", "branch": "dev/opt-demo",
              "properties": {"band": "V", "throughput": "2Gbps"}})
node_id = r.get("id")
chk("在 dev/opt-demo 建实体", bool(node_id), str(r)[:120])

# ── 3. 编辑分支（改名 + 级联同步）──
r = api("/api/branches/dev/opt-demo", method="PUT",
        body={"name": "dev/opt-demo-renamed", "branch_type": "dev",
              "parent_branch": "release", "description": "已改名", "status": "active"})
chk("编辑分支改名成功", r.get("ok"), str(r))

rows = dbq("SELECT branch FROM entities WHERE id=?", (node_id,))
chk("实体 branch 级联同步", rows and rows[0][0] == "dev/opt-demo-renamed",
    f"branch={rows[0][0] if rows else None}")

r = api("/api/branches/release", method="PUT", body={"name": "release/v1.3"})
chk("受保护分支不可改名", "受保护" in r.get("error", ""), str(r))

# ── 4. 审批前：非空分支删除被拦截 ──
r = api("/api/branches/dev/opt-demo-renamed", method="DELETE")
chk("非空分支删除被拦截", "实体" in r.get("error", "") or "合并" in r.get("error", ""), str(r))

r = api("/api/branches/dev/main", method="DELETE")
chk("受保护分支删除被拦截", "受保护" in r.get("error", ""), str(r))

# ── 5. 合并请求：创建 + 重复拦截 + 校验 ──
r = api("/api/branches/merge-requests", method="POST",
        body={"source_branch": "dev/opt-demo-renamed", "target_branch": "release"})
chk("创建合并请求", r.get("ok"), f"conflicts={r.get('conflicts')} src_entities={r.get('src_entities')}")
mr_id = r.get("id")

r = api("/api/branches/merge-requests", method="POST",
        body={"source_branch": "dev/opt-demo-renamed", "target_branch": "release"})
chk("重复 pending MR 被拦截", "待审批" in r.get("error", ""), str(r))

r = api("/api/branches/merge-requests", method="POST",
        body={"source_branch": "dev/opt-demo-renamed", "target_branch": "dev/opt-demo-renamed"})
chk("自合并被拦截", "不能相同" in r.get("error", ""), str(r))

r = api("/api/branches/merge-requests", method="POST",
        body={"source_branch": "dev/opt-demo-renamed", "target_branch": "not-exist-branch"})
chk("不存在目标分支被拦截", "不存在" in r.get("error", ""), str(r))

# ── 6. 审批合并 → 实体迁移 ──
r = api(f"/api/branches/merge-requests/{mr_id}/resolve", method="POST", body={"action": "approve"})
chk("审批通过", r.get("ok"), str(r))

rows = dbq("SELECT branch FROM entities WHERE id=?", (node_id,))
chk("实体迁移到目标分支", rows and rows[0][0] == "release", f"branch={rows[0][0] if rows else None}")

rows = dbq("SELECT merge_detail FROM merge_requests WHERE id=?", (mr_id,))
detail = json.loads(rows[0][0]) if rows and rows[0][0] else {}
chk("merge_detail 记录合并结果", detail.get("moved", 0) >= 1, str(detail))

r = api(f"/api/branches/merge-requests/{mr_id}/resolve", method="POST", body={"action": "reject"})
chk("已处理 MR 二次审批被拦截", "已处理" in r.get("error", ""), str(r))

# ── 7. 删除分支（审批后源分支已空 → 可删；连带清 MR）──
r = api("/api/branches/dev/opt-demo-renamed", method="DELETE")
chk("合并后源分支可删除", r.get("ok"), str(r))

# ── 8. 删除 MR（pending/rejected 可删，approved 不可删）──
r = api("/api/branches/merge-requests", method="POST",
        body={"source_branch": "dev/knowledge-jul", "target_branch": "dev/main"})
mid = r.get("id")
chk("再建一个 MR", bool(mid), str(r))
r = api(f"/api/branches/merge-requests/{mid}", method="DELETE")
chk("删除 pending MR 成功", r.get("ok"), str(r))

print()
print("通过:", sum(1 for p in PASS if p), "/", len(PASS))
sys.exit(0 if all(PASS) else 1)
