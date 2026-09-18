"""D9 分层式多级管理（sub_flow 递归 + parent_run_id 父子关联 + 深度保护）闭环验证。

T1. 父子 run 关联：父流程 orchestrator 委派子流程 → 子 run.parent_run_id==父 run.id，
    orchestrator 返回 sub_run_id，下游节点可引用
T2. 层级树结构：tree API 同款查询逻辑 → 根=父，children[0]=子
T3. 三层嵌套：P→C→D，父/子/孙 run 链式关联，_sub_depth 继承 +1
T4. 深度保护：7 层链，第 6 层 orchestrator 报「超过 5 层」，第 7 层不产生 run
T5. 子流程输入透传：sub_flow_inputs 模板渲染 → 子流程 payload 收到父级值
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d9_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402
from repositories.studio_repo import StudioRepo  # noqa: E402
from workflows import FlowExecutor  # noqa: E402

init_db()

PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  → {detail}")


def code_node(nid, inputs, code, label=None):
    return {"id": nid, "type": "code", "label": label or f"代码{nid}",
            "config": {"language": "python", "timeout": 10, "inputs": inputs, "code": code}}


def orch_node(nid, sub_flow=None, inputs=None, label=None):
    cfg = {"strategy": "contract_net", "task": "执行委派任务", "workers": ""}
    if sub_flow is not None:
        cfg["sub_flow"] = sub_flow
    if inputs:
        cfg["sub_flow_inputs"] = inputs
    return {"id": nid, "type": "orchestrator", "label": label or "委派", "config": cfg}


def create_flow(conn, name, nodes, edges=None):
    fid = StudioRepo(conn).create_agent_flow(
        name, "d9", json.dumps(nodes, ensure_ascii=False),
        json.dumps(edges or [], ensure_ascii=False))
    return fid


def get_run(conn, rid):
    return conn.execute("SELECT * FROM flow_runs WHERE id=?", (rid,)).fetchone()


def tree_node(conn, rid):
    """与 routers/studio.py tree API 同款递归查询逻辑。"""
    r = conn.execute("SELECT * FROM flow_runs WHERE id=?", (rid,)).fetchone()
    if not r:
        return {}
    kids = conn.execute("SELECT * FROM flow_runs WHERE parent_run_id=? ORDER BY id", (rid,)).fetchall()
    return {"id": r["id"], "flow_id": r["flow_id"], "status": r["status"],
            "parent_run_id": r["parent_run_id"],
            "children": [tree_node(conn, k["id"]) for k in kids]}


# ── T1+T2: 父子 run 关联 + 层级树结构 ──
print("== T1/T2: 父子 run 关联 + 树结构 ==")
with db_conn() as conn:
    c_nodes = [
        code_node("c1", {}, "_out['v'] = '子流程产出'"),
        code_node("c2", {"x": "{{c1.data.v}}"}, "_out['ok'] = _in.get('x') == '子流程产出'"),
    ]
    c_fid = create_flow(conn, "子流程C", c_nodes)
    p_nodes = [
        orch_node("n1", sub_flow=c_fid),
        code_node("n2", {"sid": "{{n1.data.sub_run_id}}", "s": "{{n1.data.sub_status}}"},
                  "_out['sid'] = int(str(_in.get('sid','0')) or 0)\n_out['s'] = str(_in.get('s',''))",
                  label="校验子流程"),
    ]
    p_edges = [{"from": "n1", "to": "n2"}]
    p_fid = create_flow(conn, "父流程P", p_nodes, p_edges)
    p_flow = StudioRepo(conn).get_agent_flow(p_fid)
    r = FlowExecutor(conn).run(json.loads(p_flow["nodes"]), json.loads(p_flow["edges"]), {},
                               conn, persist=True, flow_id=p_fid, flow_name=p_flow["name"])
    pr = get_run(conn, r["run_id"])
    chk("T1 父 run 已创建", pr is not None and pr["status"] in ("completed", "partial"))
    c_run = conn.execute("SELECT * FROM flow_runs WHERE parent_run_id=?", (r["run_id"],)).fetchall()
    chk("T1 子 run 存在且 parent_run_id 关联", len(c_run) == 1 and c_run[0]["flow_id"] == c_fid,
        f"child runs={[(x['id'], x['flow_id']) for x in c_run]}")
    # orchestrator 节点返回内容
    steps = conn.execute("SELECT * FROM flow_run_steps WHERE run_id=? AND node_id='n1'", (r["run_id"],)).fetchone()
    chk("T1 orchestrator done + 含子流程信息",
        steps and steps["status"] == "done" and "子流程" in (steps["content"] or "")
        and f"run {c_run[0]['id']}" in (steps["content"] or ""),
        f"content={steps and steps['content'][:80]}")
    chk("T1 下游节点引用 sub_run_id",
        r.get("order") and r["order"][-1] == "n2",
        f"order={r.get('order')}")
    t = tree_node(conn, r["run_id"])
    chk("T2 树结构：根=父、children[0]=子",
        t.get("flow_id") == p_fid and len(t.get("children", [])) == 1
        and t["children"][0]["flow_id"] == c_fid,
        json.dumps(t, ensure_ascii=False)[:120])

# ── T3: 三层嵌套（Manager→Supervisor→Worker）──
print("== T3: 三层嵌套深度链 ==")
with db_conn() as conn:
    d_nodes = [code_node("d1", {}, "_out['v'] = '底层产出'")]
    d_fid = create_flow(conn, "底层D", d_nodes)
    c_nodes = [orch_node("c1", sub_flow=d_fid), code_node("c2", {"v": "{{c1.data.sub_run_id}}"},
                                                          "_out['v'] = int(str(_in.get('v','0')) or 0)")]
    c_fid = create_flow(conn, "中层C", c_nodes, [{"from": "c1", "to": "c2"}])
    p_nodes = [orch_node("n1", sub_flow=c_fid)]
    p_fid = create_flow(conn, "顶层P", p_nodes)
    p_flow = StudioRepo(conn).get_agent_flow(p_fid)
    r = FlowExecutor(conn).run(json.loads(p_flow["nodes"]), json.loads(p_flow["edges"]), {},
                               conn, persist=True, flow_id=p_fid, flow_name=p_flow["name"])
    c_run = get_run(conn, r["run_id"])
    # C 的 run = orchestrator n1 的 sub_run_id
    n1 = conn.execute("SELECT * FROM flow_run_steps WHERE run_id=? AND node_id='n1'", (r["run_id"],)).fetchone()
    sub_rid = int((json.loads(n1["data"] or "{}") or {}).get("sub_run_id", 0)) if n1 else 0
    c_run = get_run(conn, sub_rid)
    d_runs = conn.execute("SELECT * FROM flow_runs WHERE parent_run_id=?", (sub_rid,)).fetchall()
    chk("T3 中层 run 存在且 parent=顶层", c_run is not None and c_run["parent_run_id"] == r["run_id"],
        f"mid={sub_rid}")
    chk("T3 底层 run 存在且 parent=中层",
        len(d_runs) == 1 and d_runs[0]["flow_id"] == d_fid,
        f"leaf={[(x['id'], x['flow_id'], x['parent_run_id']) for x in d_runs]}")
    t = tree_node(conn, r["run_id"])
    chk("T3 树结构三层",
        t.get("flow_id") == p_fid and len(t.get("children", [])) == 1
        and t["children"][0]["flow_id"] == c_fid
        and len(t["children"][0].get("children", [])) == 1
        and t["children"][0]["children"][0]["flow_id"] == d_fid,
        json.dumps(t, ensure_ascii=False)[:200])

# ── T4: 深度保护（7 层链，第 6 层超限，第 7 层不执行）──
print("== T4: 递归深度保护 ==")
with db_conn() as conn:
    fids = []
    n = 7
    for i in range(1, n + 1):
        if i < n:
            nodes = [orch_node("o", sub_flow=None)]  # 占位，稍后补指向
        else:
            nodes = [code_node("leaf", {}, "_out['v'] = 'leaf'")]
        fids.append(create_flow(conn, f"F{i}", nodes))
    # 重新设置 F1..F6 的 sub_flow 指向 F{i+1}
    for i in range(1, n):
        nodes = [orch_node("o", sub_flow=fids[i])]
        StudioRepo(conn).update_agent_flow(fids[i - 1], f"F{i}", "d9",
                                           json.dumps(nodes, ensure_ascii=False), "[]", "v1", "draft")
    before = conn.execute("SELECT COUNT(*) c FROM flow_runs").fetchone()["c"]
    f1 = StudioRepo(conn).get_agent_flow(fids[0])
    r = FlowExecutor(conn).run(json.loads(f1["nodes"]), json.loads(f1["edges"]), {},
                               conn, persist=True, flow_id=fids[0], flow_name=f1["name"])
    after = conn.execute("SELECT COUNT(*) c FROM flow_runs").fetchone()["c"]
    new_runs = after - before
    leaf_runs = conn.execute("SELECT COUNT(*) c FROM flow_runs WHERE flow_id=?", (fids[-1],)).fetchone()["c"]
    chk("T4 第 7 层（最深）未产生 run", leaf_runs == 0, f"leaf_runs={leaf_runs}")
    chk("T4 本次仅新增 F1..F6 共 6 个 run", new_runs == 6, f"new_runs={new_runs} (before={before} after={after})")
    # 第 6 层 F6 的 orchestrator 节点应报深度超限
    f6_run = conn.execute("SELECT id FROM flow_runs WHERE flow_id=?", (fids[5],)).fetchone()
    f6_err = None
    if f6_run:
        f6_err = conn.execute("SELECT * FROM flow_run_steps WHERE run_id=? AND node_id='o'",
                              (f6_run["id"],)).fetchone()
    chk("T4 第 6 层 orchestrator 报「超过 5 层」",
        f6_err and f6_err["status"] == "error" and "超过 5 层" in (f6_err["content"] or ""),
        f"step={f6_err and (f6_err['status'], f6_err['content'][:60])}")

# ── T5: 子流程输入透传（模板渲染）──
print("== T5: sub_flow_inputs 模板透传 ==")
with db_conn() as conn:
    c_nodes = [
        code_node("c1", {"t": "{{payload.task}}"}, "_out['got'] = str(_in.get('t',''))"),
    ]
    c_fid = create_flow(conn, "输入子流程", c_nodes)
    p_nodes = [orch_node("n1", sub_flow=c_fid, inputs={"task": "{{payload.task}}"})]
    p_fid = create_flow(conn, "输入父流程", p_nodes)
    p_flow = StudioRepo(conn).get_agent_flow(p_fid)
    r = FlowExecutor(conn).run(json.loads(p_flow["nodes"]), json.loads(p_flow["edges"]),
                               {"task": "天基网络需求分析"}, conn, persist=True,
                               flow_id=p_fid, flow_name=p_flow["name"])
    n1 = conn.execute("SELECT * FROM flow_run_steps WHERE run_id=? AND node_id='n1'", (r["run_id"],)).fetchone()
    sub_rid = int((json.loads(n1["data"] or "{}") or {}).get("sub_run_id", 0)) if n1 else 0
    c1 = conn.execute("SELECT * FROM flow_run_steps WHERE run_id=? AND node_id='c1'", (sub_rid,)).fetchone()
    d1 = (json.loads(c1["data"] or "{}") or {}) if c1 else {}
    got = (d1.get("outputs") or {}).get("got") if isinstance(d1.get("outputs"), dict) else d1.get("got")
    chk("T5 子流程 payload 收到父级模板值", got == "天基网络需求分析",
        f"got={got!r} c1_exists={c1 is not None} step_data={d1}")

print(f"\nD9 验证结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
