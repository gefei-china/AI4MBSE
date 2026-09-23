# -*- coding: utf-8 -*-
"""
B1 汇总验证 · 运行时冒烟
用 TestClient 拉起真实 app（含 lifespan → init_db 迁移），验证：
  A. P0-2 路由顺序：PUT /api/branches/{name}/protection 可达（非 404），且挂门（401/403）
  B. P0-2 读取：GET /api/branches 回包含 protection_rules，内置分支已回填
  C. P0-3：GET /api/branches/commits/verify 可达且能给出校验结论（非 404/500）
  D. P0-1：GET /api/knowledge/entities/{eid}/history 与 /at 非 500，且真读版本表
  E. 全站路由无 500（扫一批 GET）
判据：出现 404（路由被通配吞掉）或 500（运行期炸）即 FAIL。
"""
import os
import sys
import json

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from fastapi.testclient import TestClient          # noqa: E402
from main import app                                # noqa: E402

_ok, _fail = [], []


def chk(name, cond, extra=""):
    (_ok if cond else _fail).append(name)
    print("  [%s] %s%s" % ("OK" if cond else "FAIL", name, ("  → " + str(extra)) if extra else ""))


c = TestClient(app)
with c:
    print("\n--- A. P0-2 保护规则端点（路由顺序 + 权限门） ---")
    # 注意：本仓 require_permission 对**匿名**请求是刻意放行的（core/deps.py 明示向后兼容），
    # 故匿名态不能拿 401/403 当判据；正确判据是「路由未被吞（非 404）」+「低权限用户 → 403」。
    r = c.put("/api/branches/dev/protection", json={"writable": False})
    chk("A1 PUT /api/branches/dev/protection 未被 {name:path} 吞掉（非 404）", r.status_code != 404,
        "status=%s" % r.status_code)
    chk("A2 匿名态未被静默写成功（非 200，不产生副作用）", r.status_code != 200,
        "status=%s body=%.70s" % (r.status_code, r.text))

    # 低权限用户（uid=1 wang / 设计师，无 admin:ops_manage）→ 必须 403
    r403 = c.put("/api/branches/dev/protection", json={"rules": {"writable": False}},
                 headers={"X-User-Id": "1"})
    chk("A3 低权限用户（设计师）改保护规则 → 403（门真在拦）", r403.status_code == 403,
        "status=%s body=%.90s" % (r403.status_code, r403.text))
    # 管理员（uid=3 / 系统管理员，含 ops_manage）→ 过门；用不存在的分支名，门后即 404，零写入
    radm = c.put("/api/branches/__no_such_branch__/protection", json={"rules": {"writable": True}},
                 headers={"X-User-Id": "3"})
    chk("A4 管理员（系统管理员）过门后因分支不存在而 404（证明门可通过、且未落任何写入）",
        radm.status_code == 404, "status=%s body=%.70s" % (radm.status_code, radm.text))

    print("\n--- B. P0-2 分支列表含 protection_rules ---")
    rb = c.get("/api/branches")
    ok_b = rb.status_code == 200
    rows = []
    if ok_b:
        body = rb.json()
        rows = body if isinstance(body, list) else (body.get("items") or body.get("data") or [])
    chk("B1 GET /api/branches 200", ok_b, "status=%s" % rb.status_code)
    chk("B2 回包行含 protection_rules 字段",
        bool(rows) and all("protection_rules" in r0 for r0 in rows),
        "行数=%d 键=%s" % (len(rows), sorted(rows[0].keys())[:8] if rows else None))
    inner = [r0 for r0 in rows if r0.get("name") in ("release", "dev", "personal")]
    parsed = []
    for r0 in inner:
        v = r0.get("protection_rules")
        try:
            parsed.append(json.loads(v) if isinstance(v, str) else (v or {}))
        except Exception:
            parsed.append({})
    chk("B3 内置分支 protection_rules 已回填（非空 {}）", bool(parsed) and all(parsed),
        [(r0.get("name"), p) for r0, p in zip(inner, parsed)])

    print("\n--- C. P0-3 提交内容哈希校验端点 ---")
    rv = c.get("/api/branches/commits/verify")
    chk("C1 GET /api/branches/commits/verify 可达（非 404/500）", rv.status_code in (200, 401, 403),
        "status=%s" % rv.status_code)
    if rv.status_code == 200:
        j = rv.json()
        chk("C2 回包含校验结论字段", any(k in j for k in ("ok", "mismatched", "checked", "total")),
            sorted(j.keys())[:8])

    print("\n--- D. P0-1 版本读取链路 ---")
    eid = None
    rl = c.get("/api/knowledge/entities", params={"branch": "dev", "limit": 1})
    if rl.status_code == 200:
        j = rl.json()
        items = j if isinstance(j, list) else (j.get("items") or j.get("data") or [])
        if items:
            eid = items[0].get("id")
    chk("D1 能取到 dev 分支实体（读取链路未断）", bool(eid), "eid=%s status=%s" % (eid, rl.status_code))
    if eid:
        rh = c.get("/api/knowledge/entities/%s/history" % eid, params={"branch": "dev"})
        chk("D2 history 非 500 且返回至少 1 版本", rh.status_code == 200 and len(rh.json()) >= 1,
            "status=%s len=%s" % (rh.status_code, len(rh.json()) if rh.status_code == 200 else "-"))
        h = rh.json() if rh.status_code == 200 else []
        if h:
            ts = h[-1]["valid_from"]
            # 参数名是 as_of（必填），不是 ts；传错名 → 422
            ra = c.get("/api/knowledge/entities/%s/at" % eid, params={"branch": "dev", "as_of": ts})
            chk("D3 /at 历史时刻非 500 且命中版本", ra.status_code == 200 and ra.json() is not None,
                "status=%s as_of=%s" % (ra.status_code, ts))
            chk("D3b /at 参数名契约（as_of 必填，缺参应 422 而非 500）",
                c.get("/api/knowledge/entities/%s/at" % eid, params={"branch": "dev"}).status_code == 422,
                "status=%s" % c.get("/api/knowledge/entities/%s/at" % eid,
                                    params={"branch": "dev"}).status_code)
            chk("D4 history 行带双时态字段（版本表真被读到）",
                all(k in h[0] for k in ("version_no", "valid_from", "is_current")),
                sorted(h[0].keys()))

    print("\n--- E. 全站路由无 500 扫射 ---")
    probes = ["/api/branches", "/api/knowledge/entities?branch=dev", "/api/branches/commits/verify"]
    codes = [(p, c.get(p).status_code) for p in probes]
    chk("E1 探针路由无 5xx", all(not (500 <= s < 600) for _, s in codes), codes)

print("\n" + "=" * 74)
print("B1 运行时冒烟 通过 %d / 失败 %d%s"
      % (len(_ok), len(_fail), ("  失败项: " + " | ".join(_fail)) if _fail else ""))
print("=" * 74)
sys.exit(1 if _fail else 0)
