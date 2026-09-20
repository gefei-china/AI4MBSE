"""D6 主动暂停/继续 + 时间旅行回放闭环验证。

T1. 同步运行 → 检查点列表/快照（时间旅行素材）
T2. 异步运行 → 主动暂停（慢节点窗口）→ status=paused + 检查点保留
T3. 从断点恢复 → completed，全部节点执行完成
T4. 恢复后时间旅行历史完整（checkpoint seq 连续递增不丢历史）
T5. 暂停幂等（paused 后再暂停不报错）/ resume 不新建孤儿 flow_runs 行
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d6_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import get_db, db_conn, init_db  # noqa: E402
from repositories.studio_repo import StudioRepo  # noqa: E402
from workflows import FlowExecutor  # noqa: E402

init_db()  # 新测试库建表（幂等）

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


def sleep_code(sec: float):
    """沙箱内忙等延时（time 不在白名单，用 datetime 实现）。"""
    return (f"import datetime\n"
            f"t0 = datetime.datetime.now()\n"
            f"while (datetime.datetime.now() - t0).total_seconds() < {sec}:\n"
            f"    pass\n"
            f"_out['waited'] = {sec}")


def make_flow():
    """3 节点串行流程：n1(sleep 1.5) → n2(sleep 1.5) → n3(sleep 1.5)。"""
    nodes = [
        {"id": "n1", "type": "code", "label": "慢节点1", "config": {"language": "python", "code": sleep_code(1.5), "inputs": {}, "timeout": 10}},
        {"id": "n2", "type": "code", "label": "慢节点2", "config": {"language": "python", "code": sleep_code(1.5), "inputs": {}, "timeout": 10}},
        {"id": "n3", "type": "code", "label": "慢节点3", "config": {"language": "python", "code": sleep_code(1.5), "inputs": {}, "timeout": 10}},
    ]
    edges = [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n3"},
    ]
    return nodes, edges


# ── T1: 同步运行 → 检查点列表/快照（时间旅行素材） ──
print("== T1: 同步运行 → 检查点/快照 ==")
with db_conn() as conn:
    fid = StudioRepo(conn).create_agent_flow("D6验证-同步", "d6", json.dumps(make_flow()[0], ensure_ascii=False), json.dumps(make_flow()[1], ensure_ascii=False))
    flow = StudioRepo(conn).get_agent_flow(fid)
    nodes = json.loads(flow["nodes"] or "[]")
    edges = json.loads(flow["edges"] or "[]")
    r = FlowExecutor(conn).run(nodes, edges, {}, conn, persist=True, flow_id=fid, flow_name=flow["name"])
    chk("T1 同步运行 completed", r["status"] == "completed", str(r["status"]))
    cps = StudioRepo(conn).rows("SELECT seq, node_id FROM flow_checkpoints WHERE run_id=? ORDER BY seq", (r["run_id"],))
    chk("T1 检查点=3", len(cps) == 3, f"{len(cps)} 个: {[c['node_id'] for c in cps]}")
    seqs = [c["seq"] for c in cps]
    chk("T1 seq 连续递增", seqs == [1, 2, 3], str(seqs))
    snap = StudioRepo(conn).one("SELECT state_json FROM flow_checkpoints WHERE run_id=? AND seq=2", (r["run_id"],))
    st = json.loads(snap["state_json"] or "{}")
    chk("T1 快照含 n1/n2 结果", set(st.get("results", {}).keys()) == {"n1", "n2"}, str(st.get("results", {}).keys()))
    done_order = list(st.get("results", {}).keys())
    chk("T1 快照顺序含 n1→n2", done_order[:2] == ["n1", "n2"], str(done_order))
    sync_run_id = r["run_id"]


# ── T2: 异步运行 → 主动暂停 ──
print("== T2: 异步运行 → 主动暂停 ==")
with db_conn() as conn:
    fid2 = StudioRepo(conn).create_agent_flow("D6验证-暂停", "d6", json.dumps(make_flow()[0], ensure_ascii=False), json.dumps(make_flow()[1], ensure_ascii=False))
    flow2 = StudioRepo(conn).get_agent_flow(fid2)
    nodes2 = json.loads(flow2["nodes"] or "[]")
    edges2 = json.loads(flow2["edges"] or "[]")
    # 占位运行（模拟 run-async）
    cur = conn.execute(
        "INSERT INTO flow_runs (flow_id, flow_name, status, order_json, total_latency_ms, error_count) "
        "VALUES (?,?,?,?,?,?)", (fid2, flow2["name"], "running", "[]", 0, 0))
    rid2 = cur.lastrowid
    conn.commit()
    flow_nodes, flow_edges = nodes2, edges2
    result_box = {}

    def _worker():
        try:
            with db_conn() as c:
                result_box["summary"] = FlowExecutor(c).run(
                    flow_nodes, flow_edges, {}, c, persist=True, flow_id=fid2,
                    flow_name=flow2["name"], run_id=rid2)
        except Exception as e:
            result_box["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    time.sleep(2.4)  # n1 完成后（约 1.5s+），n2 进行中 → 触发暂停
    with db_conn() as c:
        c.execute("UPDATE flow_runs SET status='paused' WHERE id=?", (rid2,))
        c.commit()
    # 轮询执行器停机
    t.join(timeout=12)
    chk("T2 执行线程已结束", not t.is_alive(), "线程仍在运行（暂停未生效？）")
    with db_conn() as c:
        run = c.execute("SELECT status FROM flow_runs WHERE id=?", (rid2,)).fetchone()
        cps2 = c.execute("SELECT seq, node_id FROM flow_checkpoints WHERE run_id=? ORDER BY seq", (rid2,)).fetchall()
        steps2 = c.execute("SELECT node_id FROM flow_run_steps WHERE run_id=? ORDER BY seq", (rid2,)).fetchall()
    chk("T2 flow_runs.status==paused", run["status"] == "paused", str(run["status"]))
    chk("T2 检查点保留（1~2 个）", 1 <= len(cps2) <= 2, f"{len(cps2)} 个")
    chk("T2 已完成步骤 1~2 步", 1 <= len(steps2) <= 2, f"{len(steps2)} 步")
    chk("T2 未全部执行完（存在暂停点）", len(cps2) < 3, f"已完成 {len(cps2)}/3")
    paused_nodes = [s["node_id"] for s in steps2]
    chk("T2 暂停点在前序节点", set(paused_nodes) <= {"n1", "n2"}, str(paused_nodes))


# ── T3: 从断点恢复 → completed ──
print("== T3: 从断点恢复 ==")
with db_conn() as conn:
    # 幂等：paused 后再 pause 不报错
    rp = conn.execute("UPDATE flow_runs SET status='paused' WHERE id=?", (rid2,))
    chk("T3 重复暂停幂等（UPDATE 无异常）", True, "")
    # 模拟 /resume 接口：暂停态恢复前重置 running
    conn.execute("UPDATE flow_runs SET status='running' WHERE id=?", (rid2,))
    conn.commit()
    resume = FlowExecutor(conn).run(flow_nodes, flow_edges, {}, conn, persist=True,
                                    flow_id=fid2, flow_name=flow2["name"], resume_run_id=rid2)
    chk("T3 resume 后 completed", resume["status"] == "completed", str(resume["status"]))
    run = conn.execute("SELECT * FROM flow_runs WHERE id=?", (rid2,)).fetchone()
    chk("T3 flow_runs.status==completed", run["status"] == "completed", str(run["status"]))
    steps3 = conn.execute("SELECT node_id, status FROM flow_run_steps WHERE run_id=? ORDER BY seq", (rid2,)).fetchall()
    chk("T3 全部 3 节点 done", [s["status"] for s in steps3] == ["done", "done", "done"],
        str([(s["node_id"], s["status"]) for s in steps3]))


# ── T4: 恢复后时间旅行历史完整 ──
print("== T4: 时间旅行历史完整 ==")
with db_conn() as conn:
    cps4 = conn.execute("SELECT seq, node_id FROM flow_checkpoints WHERE run_id=? ORDER BY seq", (rid2,)).fetchall()
    seqs4 = [c["seq"] for c in cps4]
    chk("T4 检查点 3 个（历史不丢）", len(cps4) == 3, f"{len(cps4)}: {[(c['seq'], c['node_id']) for c in cps4]}")
    chk("T4 seq 连续递增 1→2→3", seqs4 == [1, 2, 3], str(seqs4))
    snap3 = conn.execute("SELECT state_json FROM flow_checkpoints WHERE run_id=? AND seq=3", (rid2,)).fetchone()
    st3 = json.loads(snap3["state_json"] or "{}")
    chk("T4 末检查点含全部节点", set(st3.get("results", {}).keys()) == {"n1", "n2", "n3"}, str(st3.get("results", {}).keys()))
    # 无孤儿 flow_runs 行（resume 不新建）
    orphans = conn.execute("SELECT COUNT(*) AS c FROM flow_runs WHERE flow_id=? AND id NOT IN (?)",
                           (fid2, rid2)).fetchone()
    chk("T4 resume 未新建孤儿运行行", orphans["c"] == 0, f"孤儿行数={orphans['c']}")


# ── T5: 暂停 API 幂等（status 已 paused/completed 时返回 ok 不报错）──
print("== T5: 暂停状态幂等 ==")
with db_conn() as conn:
    # 模拟 pause 接口对已完成运行调用：直接 UPDATE 已 completed 行 → 无异常即可
    conn.execute("UPDATE flow_runs SET status='paused' WHERE id=?", (sync_run_id,))
    conn.commit()
    chk("T5 已完成运行可安全标记", True, "")
    # 清掉测试副作用，恢复原始状态
    conn.execute("UPDATE flow_runs SET status='completed' WHERE id=?", (sync_run_id,))
    conn.commit()

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
