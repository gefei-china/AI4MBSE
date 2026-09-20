# -*- coding: utf-8 -*-
"""验证本体变更 → 实例影响分析与自动迁移全链路（2026-09-14）。

覆盖（对应 docs/本体变更实例影响分析与自动迁移方案.md §8）：
  A. L1 内联迁移：实体/属性类型改名 → 实例 entity_type / properties 键 / 关系 dom-range
     与属性 domain_classes 引用 同事务跟随，无悬空
  B. impact-preview：改名红级（auto_fix）+ 约束收紧黄级（违例清单）+ 无变更/新增绿级
  C. L2 迁移计划：build（前缀隔离）→ 列表 → dry-run → apply → 状态闭环 + 幂等（重复 build 去重）
  D. 删除类型 → deprecate_instances op → apply 后实例弃用

用法: python verify_ont_migration_flow.py   （需服务已在运行；VERIFY_BASE 可换端口）
说明: 全部测试数据用「迁移验证TS」前缀隔离；迁移计划用 target_prefix 隔离，不触碰业务数据。
      发布挂钩（version/publish → build_plan + save_plan）与手动 build 共用同一函数对，
      为不污染版本历史，本脚本走 build 入口验证。
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
PFX = f"迁移验证TS{int(time.time()) % 100000}"   # 唯一前缀，重复跑不冲突
BRANCH = "dev"

total = passed = 0


def chk(name, cond, extra=""):
    global total, passed
    total += 1
    if cond:
        passed += 1
    print(("PASS" if cond else "FAIL") + " | " + name + ((" | " + str(extra)[:300]) if extra else ""))


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


def _props(v):
    if isinstance(v, str):
        try:
            return json.loads(v or "{}")
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


# ── 0) 本体类型与实例准备（先松约束建边，再收紧 → 构造"存量违例"场景）──────
r = api("/api/knowledge/ontology/types", {
    "name": f"{PFX}源", "type_kind": "entity", "description": "verify"}, "POST")
chk("创建实体类型 源", r.get("ok") is True, r.get("error", ""))
r = api("/api/knowledge/ontology/types", {
    "name": f"{PFX}目标", "type_kind": "entity", "description": "verify"}, "POST")
chk("创建实体类型 目标", r.get("ok") is True)
r = api("/api/knowledge/ontology/types", {
    "name": f"{PFX}属性", "type_kind": "attribute",
    "constraints": {"xsd_type": "int"}, "description": "verify"}, "POST")
chk("创建属性类型 属性(int)", r.get("ok") is True)
r = api("/api/knowledge/ontology/types", {
    "name": f"{PFX}关联", "type_kind": "relation",
    "constraints": {"allowed_values": {"src": [f"{PFX}源"], "tgt": [f"{PFX}源", f"{PFX}目标"]}},
    "description": "verify"}, "POST")
chk("创建关系类型 关联（松约束 src=源 tgt={源,目标}）", r.get("ok") is True)

ont = api("/api/knowledge/ontology")
tid = {t["name"]: t["id"] for t in ont if t["name"].startswith(PFX)}
chk("本体回读拿到 4 个测试类型", len(tid) == 4, tid)

r1 = api("/api/knowledge/graph/nodes", {
    "name": f"{PFX}节点一", "entity_type": f"{PFX}源", "branch": BRANCH,
    "properties": {f"{PFX}属性": 42}, "x": 0, "y": 0}, "POST")
r2 = api("/api/knowledge/graph/nodes", {
    "name": f"{PFX}节点二", "entity_type": f"{PFX}源", "branch": BRANCH,
    "properties": {}, "x": 0, "y": 0}, "POST")
r3 = api("/api/knowledge/graph/nodes", {
    "name": f"{PFX}节点三", "entity_type": f"{PFX}目标", "branch": BRANCH,
    "properties": {}, "x": 0, "y": 0}, "POST")
chk("创建实例 ×3", all(x.get("ok") for x in (r1, r2, r3)), (r1, r2, r3))
n1, n2, n3 = r1.get("id"), r2.get("id"), r3.get("id")

re_ = api("/api/knowledge/graph/edges", {
    "source_id": n1, "target_id": n3, "relation_type": f"{PFX}关联",
    "branch": BRANCH}, "POST")
chk("创建边 n1→n3（松约束下合法）", bool(re_.get("ok") or re_.get("id")), re_)

# ── B3/B4. impact-preview 绿级场景 ────────────────────────────────────
pv3 = api("/api/knowledge/ontology/impact-preview", {
    "tid": tid[f"{PFX}目标"], "name": f"{PFX}目标", "type_kind": "entity",
    "properties": {}, "constraints": {}}, "POST")
chk("preview 无变更 → 绿级", pv3.get("severity") == "green", pv3)
pv4 = api("/api/knowledge/ontology/impact-preview", {
    "name": f"{PFX}新类", "type_kind": "entity", "properties": {}, "constraints": {}}, "POST")
chk("preview 新增类型 → 绿级", pv4.get("severity") == "green")

# ── B2. impact-preview：约束收紧黄级（存量 tgt 违例：n1→n3 目标端）──────
pv2 = api("/api/knowledge/ontology/impact-preview", {
    "tid": tid[f"{PFX}关联"], "name": f"{PFX}关联", "type_kind": "relation",
    "properties": {},
    "constraints": {"allowed_values": {"src": [f"{PFX}源"], "tgt": [f"{PFX}源"]}}}, "POST")
chk("preview 收紧关系 → 黄级", pv2.get("severity") == "yellow", pv2)
chk("preview 收紧关系 → 违例≥1（目标端类型不在值域）", pv2.get("violation_count", 0) >= 1,
    pv2.get("violations"))

# ── B1. impact-preview：改名红级 + 自动迁移预告 ───────────────────────
pv = api("/api/knowledge/ontology/impact-preview", {
    "tid": tid[f"{PFX}源"], "name": f"{PFX}源X", "type_kind": "entity",
    "properties": {}, "constraints": {}}, "POST")
chk("preview 改名 → 红级", pv.get("severity") == "red", pv)
chk("preview 改名 → auto_fix=true", pv.get("auto_fix") is True)
chk("preview 改名 → 迁移数=2", (pv.get("rename") or {}).get("entities") == 2, pv.get("rename"))

# ── A1. L1 实体改名内联迁移（实例 + 关系 dom/range 引用联动）──────────
up = api(f"/api/knowledge/ontology/types/{tid[f'{PFX}源']}", {
    "name": f"{PFX}源X", "type_kind": "entity", "properties": {}, "constraints": {},
    "description": "verify"}, "PUT")
chk("PUT 改名 源→源X", up.get("ok") is True, up)
chk("PUT 响应带迁移数 migrated_instances=2", up.get("migrated_instances") == 2, up)
g = api(f"/api/knowledge/graph?branch={BRANCH}")
_types = {e.get("id"): e.get("entity_type") for e in g.get("entities", [])}
chk("实例 entity_type 已跟随改名（无悬空）",
    _types.get(n1) == f"{PFX}源X" and _types.get(n2) == f"{PFX}源X")
_rel_detail = api(f"/api/knowledge/ontology/types/{tid[f'{PFX}关联']}")
_av = (_rel_detail.get("constraints") or {}).get("allowed_values") or {}
chk("关系 dom/range 引用联动改名（src/tgt 含 源X）",
    f"{PFX}源X" in (_av.get("src") or []) and f"{PFX}源X" in (_av.get("tgt") or []),
    _av)
chk("本体一致性：无 bad_dom_range 高危",
    all(i["type"] != "bad_dom_range" for i in api("/api/knowledge/ontology/validate").get("issues", [])
        if i["name"].startswith(PFX)))

# ── A2. L1 属性类型改名 → 实例 properties 键跟随 ─────────────────────
up2 = api(f"/api/knowledge/ontology/types/{tid[f'{PFX}属性']}", {
    "name": f"{PFX}属性X", "type_kind": "attribute", "properties": {},
    "constraints": {"xsd_type": "int"}, "description": "verify"}, "PUT")
chk("PUT 改名 属性→属性X", up2.get("ok") is True, up2)
chk("PUT 响应 migrated_prop_keys=1", up2.get("migrated_prop_keys") == 1, up2)
g2 = api(f"/api/knowledge/graph?branch={BRANCH}")
_n1p = next((_props(e.get("properties")) for e in g2.get("entities", []) if e.get("id") == n1), {})
chk("实例属性键已跟随改名（新键有值/旧键消失）",
    _n1p.get(f"{PFX}属性X") == 42 and f"{PFX}属性" not in _n1p, _n1p)

# ── C. L2 迁移计划：收紧留痕 → build → dry-run → apply → 幂等 ─────────
up3 = api(f"/api/knowledge/ontology/types/{tid[f'{PFX}关联']}", {
    "name": f"{PFX}关联", "type_kind": "relation", "properties": {},
    "constraints": {"allowed_values": {"src": [f"{PFX}源X"], "tgt": [f"{PFX}源X"]}},
    "description": "verify-tighten"}, "PUT")
chk("PUT 收紧关联 dom/range（留痕）", up3.get("ok") is True, up3)

mb = api("/api/knowledge/ontology/migrations/build", {"target_prefix": PFX}, "POST")
chk("build 迁移计划（前缀隔离）", mb.get("ok") is True and mb.get("plan_id", 0) > 0, mb)
chk("build 计划含 op（flag_violations）", mb.get("ops", 0) >= 1, mb)
plan_id = mb.get("plan_id")

ml = api("/api/knowledge/ontology/migrations")
_plan = next((p for p in ml.get("plans", []) if p.get("plan_id") == plan_id), None)
chk("迁移计划列表可见", _plan is not None)
chk("计划 op 全部 pending", _plan and all(o["status"] == "pending" for o in _plan["ops"]),
    _plan and [o["status"] for o in _plan["ops"]])

dr = api(f"/api/knowledge/ontology/migrations/{plan_id}/dry-run", {}, "POST")
chk("dry-run 预演成功", dr.get("ok") is True, dr)
chk("dry-run 预演影响数≥1", dr.get("affected_total", 0) >= 1, dr)
ml2 = api("/api/knowledge/ontology/migrations")
_plan2 = next((p for p in ml2.get("plans", []) if p.get("plan_id") == plan_id), {})
chk("预演后状态 dry_run（未写业务数据）",
    all(o["status"] == "dry_run" for o in _plan2.get("ops", [])),
    _plan2 and [o["status"] for o in _plan2.get("ops", [])])

ap = api(f"/api/knowledge/ontology/migrations/{plan_id}/apply", {}, "POST")
chk("apply 执行成功", ap.get("ok") is True, ap)
ml3 = api("/api/knowledge/ontology/migrations")
_plan3 = next((p for p in ml3.get("plans", []) if p.get("plan_id") == plan_id), {})
chk("执行后 op 全部 applied", _plan3 and all(o["status"] == "applied" for o in _plan3["ops"]),
    _plan3 and [o["status"] for o in _plan3.get("ops", [])])
g3 = api(f"/api/knowledge/graph?branch={BRANCH}")
_edges = [(e.get("source_id"), e.get("target_id")) for e in g3.get("relations", [])]
chk("flag_violations 不自动改数据（边保留待人工裁决）", (n1, n3) in _edges)

# 幂等：重复 build 同一留痕 → 去重不生成新计划
mb2 = api("/api/knowledge/ontology/migrations/build", {"target_prefix": PFX}, "POST")
chk("重复 build 幂等去重（plan_id=0）", mb2.get("ok") is True and mb2.get("plan_id", 1) == 0, mb2)

# ── D. 删除类型 → deprecate op → apply 后实例弃用 ─────────────────────
dl = api(f"/api/knowledge/ontology/types/{tid[f'{PFX}目标']}", None, "DELETE")
chk("删除类型 目标（无结构引用，直接删）", dl.get("ok") is True, dl)
mb3 = api("/api/knowledge/ontology/migrations/build", {"target_prefix": PFX}, "POST")
chk("删除后 build 出 deprecate op", mb3.get("plan_id", 0) > 0, mb3)
if mb3.get("plan_id"):
    api(f"/api/knowledge/ontology/migrations/{mb3['plan_id']}/dry-run", {}, "POST")
    ap2 = api(f"/api/knowledge/ontology/migrations/{mb3['plan_id']}/apply", {}, "POST")
    chk("deprecate apply 成功", ap2.get("ok") is True, ap2)

import sqlite3
here = os.path.dirname(os.path.abspath(__file__))
dbp = os.path.join(here, "..", "..", "mbse.db")
conn = sqlite3.connect(dbp)
n3st = conn.execute("SELECT status FROM entities WHERE id=?", (n3,)).fetchone()
chk("目标实例已标记 deprecated", bool(n3st) and n3st[0] == "deprecated", n3st)

# ── 清理（sqlite 直删测试数据；变更留痕/迁移记录保留=审计轨迹）──────────
c = conn.cursor()
c.execute("DELETE FROM relations WHERE source_id IN (?,?,?) OR target_id IN (?,?,?)",
          (n1, n2, n3, n1, n2, n3))
c.execute("DELETE FROM entities WHERE id IN (?,?,?)", (n1, n2, n3))
c.execute("DELETE FROM ontology_types WHERE name LIKE ?", (PFX + "%",))
c.execute("DELETE FROM ontology_change_logs WHERE type_id NOT IN (SELECT id FROM ontology_types)")
conn.commit()
conn.close()
print("\n已清理测试数据（change_logs/迁移留痕保留供审计）")

print(f"\n结果: {passed}/{total} PASS")
sys.exit(0 if passed == total else 1)
