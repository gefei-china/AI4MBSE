# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：P0-0+A4 端到端（2026-11-21）：真发 HTTP 请求走「端点→统一写入口→仓储→回读核验」全链，含 E2b 判别性证据（绕过入口直写仓储能过 ⇒ 证明 E2 的 400 来自新增校验）。用 MBSE_DB_PATH 指向临时空库并在 import core.config 之前设置（脚本内含硬断言，绝不碰生产 mbse.db）。实测 9/9 全绿、3.8s、零 LLM 零外部网络。为何暂不进 pr：需导入全量 app，干净
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端门禁：本体写链路（P0-0 + A4 + P1-8，2026-11-21 新增）。

## 为什么要有它（而不是只靠单测）

本轮改动全部落在「HTTP 端点 → 统一写入口 → 仓储 → 回读核验」这条链上。
单测能证明分类函数算得对，但证明不了**端点真的接上了**。此前踩过的坑：
`_build_tools_def` 里有工具、`tools` 表里有绑定，运行时却因另一道过滤被静默剔除
——「代码存在」与「链路通了」是两件事，必须真发请求。

## 隔离做法（关键安全点）

用环境变量 `MBSE_DB_PATH` 指向**临时空库**，在 `import core.config` **之前**设置
⇒ 39 个 `from core.config import DB_PATH` 的模块全部指向临时库，**绝不碰生产 mbse.db**。
脚本开头有硬断言：若 `core.config.DB_PATH` 不是那个临时文件，立即中止。

## 判据

  E1 未知类型创建 ⇒ 400 且 `stage=type_gate`（类型门禁生效）
  E2 属性违反本体约束 ⇒ 400 且 `stage=validate`（**新增**：改前 create 不校验属性）
  E3 合法创建 ⇒ 200 且 `post.verified=True`，且库里真有一行（执行后核验非装饰）
  E4 合法更新 ⇒ 200 且 `post.verified=True`
  E5 图谱节点更新（另一条写入口）⇒ 200 且 `post.verified=True`
  E6 收窄 domain_classes 后发布 ⇒ `diff.level=major`、`compatible=0`、主版本 +1
     （改前该变更被判 minor/兼容 —— 本门禁的核心回归锚）
  E7 只改 alt_labels 后发布 ⇒ `diff.level=minor`、`compatible=1`
  E8 无变更发布 ⇒ 400「无差异」
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []
PASS, FAIL = "PASS", "FAIL"


def _add(ok, name, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:320]))


def _prepare():
    """临时库 + 种子。返回 (db_path, cleanup)。"""
    tmp = tempfile.mkdtemp(prefix="mbse_e2e_")
    dbp = os.path.join(tmp, "e2e.db")
    # ⚠️ 必须在 import core.config 之前
    os.environ["MBSE_DB_PATH"] = dbp

    import core.config as cfg
    if os.path.abspath(cfg.DB_PATH) != os.path.abspath(dbp):
        raise SystemExit("中止：DB_PATH 未指向临时库（会污染生产库）：%s" % cfg.DB_PATH)

    from database.schema import init_db
    init_db()

    import sqlite3
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    # ⚠️ init_db() 自带种子资产（实测 158 个类型、244 条绑定），所以**不能假定类型为空**，
    #    也不能用"需求/系统"这类通用名（UNIQUE(name) 会撞）。⇒ 自建 E2E_ 前缀类型，
    #    并复用种子里的对象类型作为关系域值，贴近真实数据形态。
    objs = [r["name"] for r in c.execute(
        "SELECT name FROM ontology_types WHERE type_kind='entity' ORDER BY id LIMIT 3")]
    if len(objs) < 2:
        raise SystemExit("中止：种子库里对象类型不足 2 个，无法构造域/值域用例")
    REQ_TYPE = "E2E_必填类型"
    REL_TYPE = "E2E_包含关系"
    types = [
        # 有 required 约束 ⇒ 用于 E2「缺必填应被拒」
        (REQ_TYPE, "entity", "{}", json.dumps({"required": ["req_id"]}, ensure_ascii=False)),
        # 关系类型：domain_classes 用于 E6「收窄定义域 ⇒ MAJOR」
        (REL_TYPE, "relation", "{}", json.dumps(
            {"domain_classes": objs[:2],
             "allowed_values": {"src": objs[:2], "tgt": objs[2:3] or objs[:1]}},
            ensure_ascii=False)),
    ]
    for name, kind, props, cons in types:
        c.execute("INSERT OR IGNORE INTO ontology_types (name, type_kind, properties, constraints, "
                  "description, icon, color) VALUES (?,?,?,?,?,?,?)",
                  (name, kind, props, cons, "", "", "#185FA5"))
    n_types = c.execute("SELECT count(*) FROM ontology_types").fetchone()[0]
    # 一个已发布版本 + 全量快照（publish 的 prev 基准）
    c.execute("INSERT INTO ontology_versions (version_label, major, minor, patch, change_type, "
              "summary, operator, status, released_at, released_by, snapshot_count, "
              "compatible, active, snapshot_created_at) "
              "VALUES ('v1.0.0',1,0,0,'publish','基线','tester','released',"
              "'2026-11-21 00:00:00','tester',?,1,1,'2026-11-21 00:00:00')", (n_types,))
    vid = c.execute("SELECT id FROM ontology_versions WHERE version_label='v1.0.0'").fetchone()["id"]
    for r in c.execute("SELECT id,name,type_kind,parent_id,properties,constraints FROM ontology_types"):
        c.execute("INSERT INTO ontology_version_snapshots "
                  "(version_id, type_id, name, type_kind, parent_id, properties, constraints) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (vid, r["id"], r["name"], r["type_kind"], r["parent_id"],
                   r["properties"], r["constraints"]))
    c.commit()
    c.close()
    return dbp, lambda: shutil.rmtree(tmp, ignore_errors=True), {
        "REQ_TYPE": REQ_TYPE, "REL_TYPE": REL_TYPE, "objs": objs}


def _client():
    """TestClient + 依赖覆盖（请求级连接 / 假管理员）。不进入 lifespan。"""
    from fastapi.testclient import TestClient
    import main
    from core.deps import db_session, current_user

    def _db():
        from database.connection import get_db
        conn = get_db()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    app = main.app
    app.dependency_overrides[db_session] = _db
    app.dependency_overrides[current_user] = lambda: {
        "id": 1, "username": "e2e", "display_name": "E2E",
        "role_id": 1, "role_name": "admin", "role_type": "system",
        "permissions": {"admin": ["read", "write"], "kb_data": ["read", "write"]},
    }
    return TestClient(app)


def run():
    dbp, cleanup, seed = _prepare()
    REQ_TYPE, REL_TYPE = seed['REQ_TYPE'], seed['REL_TYPE']
    try:
        cli = _client()
        import sqlite3
        conn = sqlite3.connect(dbp)
        conn.row_factory = sqlite3.Row

        # E1 未知类型
        r = cli.post("/api/knowledge/entities", json={
            "id": "E-1", "name": "x", "entity_type": "不存在的类型", "properties": {}})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 400 and j.get("stage") == "type_gate",
             "E1 未知类型 ⇒ 400 stage=type_gate", f"{r.status_code} {j}")

        # E2 缺必填（本体 required: req_id）—— 改前 create 路径不校验属性，会 200
        r = cli.post("/api/knowledge/entities", json={
            "id": "E-2", "name": "需求A", "entity_type": REQ_TYPE, "properties": {}})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 400 and j.get("stage") == "validate",
             "E2 缺必填 ⇒ 400 stage=validate", f"{r.status_code} {j}")

        # E2b 判别性证据：**绕过统一入口直写仓储**，同一份缺必填数据是能落库的
        #     ⇒ 证明 E2 的 400 来自新增的校验，而不是别的地方顺手拦掉。
        #     （改前的 POST /entities 正是这条路径：只查类型、不校验属性。）
        from repositories.knowledge_repo import KnowledgeRepo
        direct = None
        dconn = sqlite3.connect(dbp, timeout=30)   # 独立短连接：写完立刻关，避免持锁挡住后续请求
        dconn.row_factory = sqlite3.Row
        try:
            KnowledgeRepo(dconn).create_entity(
                "E-2b", "需求A直写", REQ_TYPE, json.dumps({}), "dev", created_by="e2e")
            dconn.commit()
            direct = dconn.execute("SELECT id FROM entity_versions WHERE id='E-2b' "
                                   "AND branch='dev' AND is_current=1").fetchone()
        except Exception as e:
            direct = "EXC:" + str(e)[:80]
        finally:
            dconn.close()
        _add(direct is not None and not isinstance(direct, str),
             "E2b 直写仓储可绕过校验（证明 E2 的拒绝来自新增校验）",
             f"direct={direct}")

        # E3 合法创建
        r = cli.post("/api/knowledge/entities", json={
            "id": "E-3", "name": "需求B", "entity_type": REQ_TYPE,
            "properties": {"req_id": "R-001"}, "branch": "dev"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok3 = (r.status_code == 200 and j.get("ok") is True
               and (j.get("post") or {}).get("verified") is True)
        row = conn.execute("SELECT name FROM entity_versions WHERE id='E-3' AND branch='dev' "
                           "AND is_current=1").fetchone()
        _add(ok3 and row is not None, "E3 合法创建 ⇒ 200 post.verified + 库里真有行",
             f"{r.status_code} {str(j)[:180]} row={dict(row) if row else None}")

        # E4 合法更新
        r = cli.put("/api/knowledge/entities/E-3", json={
            "id": "E-3", "name": "需求B改", "entity_type": REQ_TYPE,
            "properties": {"req_id": "R-001"}, "branch": "dev"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 200 and (j.get("post") or {}).get("verified") is True,
             "E4 合法更新 ⇒ 200 post.verified", f"{r.status_code} {str(j)[:180]}")

        # E5 图谱节点更新（第二条写入口）
        r = cli.put("/api/knowledge/graph/nodes/E-3", json={
            "name": "需求B改2", "entity_type": REQ_TYPE,
            "properties": {"req_id": "R-001"}, "x": 10, "y": 20, "branch": "dev"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 200 and (j.get("post") or {}).get("verified") is True,
             "E5 图谱节点更新 ⇒ 200 post.verified", f"{r.status_code} {str(j)[:180]}")

        # ── 第二批迁移（2026-11-21）：节点创建/删除、边创建/删除 ──
        # ⚠️ 边用例必须用**关系类型允许的域/值域**（`E2E_包含关系` 的 domain_classes
        #    是种子对象类型的前两个，值域是第三个）。第一次我拿 `E2E_必填类型` 去连边，
        #    被 400 打回 —— 那恰恰证明关系类型门禁是真的在生效，不是摆设。
        SRC_TYPE = seed["objs"][0]
        TGT_TYPE = seed["objs"][2] if len(seed["objs"]) > 2 else seed["objs"][1]

        # E5b 图谱节点创建（主创建路径）——必须 status=reviewed（图库内创建即确认），
        #     与 POST /entities 默认 candidate 进审核队列语义不同，迁移时不能丢。
        r = cli.post("/api/knowledge/graph/nodes", json={
            "id": "G-1", "name": "图谱节点1", "entity_type": SRC_TYPE,
            "properties": {}, "x": 1, "y": 2, "branch": "dev"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        st = conn.execute("SELECT status FROM entities WHERE id='G-1' AND branch='dev'").fetchone()
        _add(r.status_code == 200 and (j.get("post") or {}).get("verified") is True
             and st is not None and st["status"] == "reviewed",
             "E5b 图谱节点创建 ⇒ 200 verified + status=reviewed",
             f"{r.status_code} status={st['status'] if st else None} {str(j)[:150]}")

        # E5b2 第二个节点（作为边的目标）
        r = cli.post("/api/knowledge/graph/nodes", json={
            "id": "G-2", "name": "图谱节点2", "entity_type": TGT_TYPE,
            "properties": {}, "x": 3, "y": 4, "branch": "dev"})
        _add(r.status_code == 200, "E5b2 第二个节点创建（边的目标）", f"{r.status_code}")

        # E5c 边创建（源/目标类型均在关系类型允许域内）
        r = cli.post("/api/knowledge/graph/edges", json={
            "source_id": "G-1", "target_id": "G-2",
            "relation_type": REL_TYPE, "props": {}, "branch": "dev"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        eid = j.get("id")
        _add(r.status_code == 200 and (j.get("post") or {}).get("verified") is True and bool(eid),
             "E5c 边创建 ⇒ 200 verified", f"{r.status_code} eid={eid} {str(j)[:150]}")

        # E5c2 边类型门禁：源/目标类型不在域内必须被拒（负向，证明门禁生效）
        r = cli.post("/api/knowledge/graph/edges", json={
            "source_id": "E-3", "target_id": "G-2",
            "relation_type": REL_TYPE, "props": {}, "branch": "dev"})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 400 and j.get("stage") == "validate",
             "E5c2 域外类型连边 ⇒ 400 stage=validate（关系门禁生效）",
             f"{r.status_code} {str(j)[:180]}")

        # E5d 边删除 ⇒ 反向断言（行必须消失）
        r = cli.delete("/api/knowledge/graph/edges/%s" % eid) if eid else None
        j = r.json() if (r and r.headers.get("content-type", "").startswith("application/json")) else {}
        gone = conn.execute("SELECT 1 FROM relations WHERE id=?", (eid,)).fetchone() if eid else "no-eid"
        _add(r is not None and r.status_code == 200
             and (j.get("post") or {}).get("verified") is True and gone is None,
             "E5d 边删除 ⇒ 200 verified + relations 中已消失",
             f"{r.status_code if r else 'skip'} gone={gone} {str(j)[:150]}")

        # E5e 节点删除 ⇒ 反向断言，且必须**连带删同分支关联边**（这条才是真判据：
        #     只断言实体消失的话，边成了孤儿也能过）。先重建一条边再删源节点。
        cli.post("/api/knowledge/graph/edges", json={
            "source_id": "G-1", "target_id": "G-2",
            "relation_type": REL_TYPE, "props": {}, "branch": "dev"})
        r = cli.delete("/api/knowledge/graph/nodes/G-1?branch=dev")
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        left = conn.execute("SELECT 1 FROM entities WHERE id='G-1' AND branch='dev'").fetchone()
        left_rel = conn.execute("SELECT count(*) FROM relations WHERE branch='dev' AND "
                                "(source_id='G-1' OR target_id='G-1')").fetchone()[0]
        _add(r.status_code == 200 and (j.get("post") or {}).get("verified") is True
             and left is None and left_rel == 0,
             "E5e 节点删除 ⇒ 200 verified + 实体与**关联边**均消失",
             f"{r.status_code} entity={left} rel={left_rel} {str(j)[:150]}")

        # E5f 删不存在的节点 ⇒ 404（不能报成功）
        r = cli.delete("/api/knowledge/graph/nodes/NOT-EXIST-XYZ?branch=dev")
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 404, "E5f 删不存在节点 ⇒ 404（不报假成功）",
             f"{r.status_code} {str(j)[:140]}")

        # E6 收窄 domain_classes ⇒ MAJOR（改前被判 minor/兼容）
        conn.execute("UPDATE ontology_types SET constraints=? WHERE name=?", 
                     (json.dumps({"domain_classes": [seed["objs"][0]], "allowed_values":
                                  {"src": [seed["objs"][0]], "tgt": seed["objs"][2:3] or seed["objs"][:1]}},
                                 ensure_ascii=False), REL_TYPE))
        conn.commit()
        r = cli.post("/api/knowledge/ontology/version/publish", json={"summary": ""})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        d = j.get("diff") or {}
        v = j.get("version") or {}
        _add(r.status_code == 200 and d.get("level") == "major"
             and (j.get("snapshot") or {}).get("compatible") == 0
             and v.get("major") == 2,
             "E6 收窄 domain_classes ⇒ MAJOR + 主版本+1",
             f"{r.status_code} level={d.get('level')} compat={(j.get('snapshot') or {}).get('compatible')} "
             f"ver={v.get('version_label')} reasons={str(d.get('reasons'))[:160]}")

        # E7 只改 alt_labels ⇒ MINOR + 兼容
        conn.execute("UPDATE ontology_types SET constraints=? WHERE name=?", 
                     (json.dumps({"required": ["req_id"], "alt_labels": ["需求项"]},
                                 ensure_ascii=False), REQ_TYPE))
        conn.commit()
        r = cli.post("/api/knowledge/ontology/version/publish", json={"summary": ""})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        d = j.get("diff") or {}
        v = j.get("version") or {}
        _add(r.status_code == 200 and d.get("level") == "minor"
             and (j.get("snapshot") or {}).get("compatible") == 1
             and v.get("minor") == 1,
             "E7 只改标注 ⇒ MINOR + 兼容 + 次版本+1",
             f"{r.status_code} level={d.get('level')} compat={(j.get('snapshot') or {}).get('compatible')} "
             f"ver={v.get('version_label')}")

        # E8 无变更 ⇒ 400 无差异
        r = cli.post("/api/knowledge/ontology/version/publish", json={"summary": ""})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _add(r.status_code == 400 and "无差异" in str(j.get("error", "")),
             "E8 无变更 ⇒ 400 无差异", f"{r.status_code} {str(j)[:160]}")

        conn.close()
    finally:
        cleanup()


def main():
    print("=" * 76)
    print("门禁：本体写链路端到端（P0-0 + A4）——临时库，零生产副作用")
    print("=" * 76)
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        _add(False, "E0 脚本自身执行", f"{type(e).__name__}: {e}")
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("\n" + "=" * 76)
    if n_fail:
        print("结论：未通过（%d 条失败）" % n_fail)
        return 1
    print("结论：全部通过（%d 条）" % len(_results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
