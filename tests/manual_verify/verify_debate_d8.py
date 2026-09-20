"""D8 多 Agent 分布式协商（投票/议价）闭环验证。

T1. vote 多数共识：3 来源 2:1 → consensus=true, decision=方案A, agreement≈0.667
T2. vote 分裂：A/B/C 各 1 → agreement=0.333<0.6 → consensus=false
T3. price 收敛：100/105/98 → 收敛价 98（最低中标），极差率 0.07<=0.2 → consensus=true
T4. price 不收敛：10/100/1000 → 极差率 9.9>0.2 → consensus=false
T5. sources 留空取全部已执行节点 + 无 options 按观点文本分组计票
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d8_test.db")
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


def run_flow(conn, name, nodes, edges):
    fid = StudioRepo(conn).create_agent_flow(name, "d8", json.dumps(nodes, ensure_ascii=False),
                                             json.dumps(edges, ensure_ascii=False))
    flow = StudioRepo(conn).get_agent_flow(fid)
    return FlowExecutor(conn).run(json.loads(flow["nodes"]), json.loads(flow["edges"]), {},
                                  conn, persist=True, flow_id=fid, flow_name=flow["name"])


def stance_node(nid, text):
    return {"id": nid, "type": "code", "label": f"观点{nid}", "config": {
        "language": "python", "timeout": 10, "inputs": {},
        "code": f"_out['stance'] = '{text}'"}}


def code_node(nid, inputs, code):
    return {"id": nid, "type": "code", "label": f"校验{nid}", "config": {
        "language": "python", "timeout": 10, "inputs": inputs, "code": code}}


# ── T1: vote 多数共识 ──
print("== T1: vote 2:1 达成共识 ==")
with db_conn() as conn:
    nodes = [
        stance_node("n1", "方案A：扩大覆盖范围"),
        stance_node("n2", "方案A：扩大覆盖范围"),
        stance_node("n3", "方案B：压缩频段"),
        {"id": "n4", "type": "debate", "label": "协商", "config": {
            "mode": "vote", "proposal": "选择通信体制方案", "sources": "n1,n2,n3",
            "options": "方案A,方案B", "threshold": "0.6"}},
        code_node("n5", {"d": "{{n4.data.decision}}", "c": "{{n4.data.consensus}}"},
                  "d = str(_in.get('d','')); c = str(_in.get('c','')).lower()\n"
                  "_out['ok'] = (d == '方案A' and c == 'true')\n_out['d'] = d"),
    ]
    edges = [{"source": "n1", "target": "n4"}, {"source": "n2", "target": "n4"},
             {"source": "n3", "target": "n4"}, {"source": "n4", "target": "n5"}]
    r = run_flow(conn, "D8验证-投票共识", nodes, edges)
    chk("T1 运行 completed", r["status"] == "completed", str(r["status"]))
    d = r["results"].get("n4", {}).get("data", {})
    chk("T1 计票 tally", d.get("tally") == {"方案A": 2, "方案B": 1}, str(d.get("tally")))
    chk("T1 共识达成", d.get("consensus") is True, str(d.get("consensus")))
    chk("T1 决策=方案A", d.get("decision") == "方案A", str(d.get("decision")))
    chk("T1 agreement≈0.667", abs((d.get("agreement") or 0) - 0.667) < 0.01, str(d.get("agreement")))
    chk("T1 下游消费决策", r["results"].get("n5", {}).get("data", {}).get("outputs", {}).get("ok") is True,
        str(r["results"].get("n5", {}).get("data", {}).get("outputs", {})))


# ── T2: vote 分裂 ──
print("== T2: vote 三方分裂未达成共识 ==")
with db_conn() as conn:
    nodes = [
        stance_node("n1", "方案A"),
        stance_node("n2", "方案B"),
        stance_node("n3", "方案C"),
        {"id": "n4", "type": "debate", "label": "协商", "config": {
            "mode": "vote", "proposal": "路由选择", "sources": "n1,n2,n3",
            "options": "方案A,方案B,方案C", "threshold": "0.6"}},
    ]
    edges = [{"source": "n1", "target": "n4"}, {"source": "n2", "target": "n4"}, {"source": "n3", "target": "n4"}]
    r = run_flow(conn, "D8验证-投票分裂", nodes, edges)
    d = r["results"].get("n4", {}).get("data", {})
    chk("T2 未达成共识", d.get("consensus") is False, str(d.get("consensus")))
    chk("T2 无决策", d.get("decision") == "", str(d.get("decision")))
    chk("T2 三分票数", d.get("tally") == {"方案A": 1, "方案B": 1, "方案C": 1}, str(d.get("tally")))


# ── T3: price 收敛 ──
print("== T3: price 出价收敛（最低中标）==")
with db_conn() as conn:
    nodes = [
        stance_node("n1", "出价 100 万元"),
        stance_node("n2", "出价 105 万元"),
        stance_node("n3", "出价 98 万元"),
        {"id": "n4", "type": "debate", "label": "议价", "config": {
            "mode": "price", "proposal": "子系统研制成本议价", "sources": "n1,n2,n3"}},
        code_node("n5", {"p": "{{n4.data.converged_price}}", "c": "{{n4.data.consensus}}"},
                  "p = str(_in.get('p','')); c = str(_in.get('c','')).lower()\n"
                  "_out['ok'] = (p == '98.0' and c == 'true')\n_out['p'] = p"),
    ]
    edges = [{"source": "n1", "target": "n4"}, {"source": "n2", "target": "n4"},
             {"source": "n3", "target": "n4"}, {"source": "n4", "target": "n5"}]
    r = run_flow(conn, "D8验证-议价收敛", nodes, edges)
    d = r["results"].get("n4", {}).get("data", {})
    chk("T3 议价收敛", d.get("consensus") is True, str(d.get("consensus")))
    chk("T3 收敛价 98（最低中标）", d.get("converged_price") == 98.0, str(d.get("converged_price")))
    chk("T3 极差率 0.07", d.get("spread") == 0.07, str(d.get("spread")))
    chk("T3 下游消费收敛价", r["results"].get("n5", {}).get("data", {}).get("outputs", {}).get("ok") is True,
        str(r["results"].get("n5", {}).get("data", {}).get("outputs", {})))


# ── T4: price 不收敛 ──
print("== T4: price 出价分歧未收敛 ==")
with db_conn() as conn:
    nodes = [
        stance_node("n1", "出价 10 万元"),
        stance_node("n2", "出价 100 万元"),
        stance_node("n3", "出价 1000 万元"),
        {"id": "n4", "type": "debate", "label": "议价", "config": {
            "mode": "price", "proposal": "成本议价", "sources": "n1,n2,n3"}},
    ]
    edges = [{"source": "n1", "target": "n4"}, {"source": "n2", "target": "n4"}, {"source": "n3", "target": "n4"}]
    r = run_flow(conn, "D8验证-议价分歧", nodes, edges)
    d = r["results"].get("n4", {}).get("data", {})
    chk("T4 未收敛", d.get("consensus") is False, str(d.get("consensus")))
    chk("T4 极差率>0.2", (d.get("spread") or 0) > 0.2, str(d.get("spread")))


# ── T5: sources 留空 + 无 options 按观点文本分组 ──
print("== T5: sources 留空按文本分组计票 ==")
with db_conn() as conn:
    nodes = [
        stance_node("n1", "认同"),
        stance_node("n2", "认同"),
        stance_node("n3", "反对"),
        {"id": "n4", "type": "debate", "label": "协商", "config": {
            "mode": "vote", "proposal": "是否采纳该方案"}},
    ]
    edges = [{"source": "n1", "target": "n4"}, {"source": "n2", "target": "n4"}, {"source": "n3", "target": "n4"}]
    r = run_flow(conn, "D8验证-文本计票", nodes, edges)
    d = r["results"].get("n4", {}).get("data", {})
    chk("T5 文本分组计票", d.get("tally") == {"认同": 2, "反对": 1}, str(d.get("tally")))
    chk("T5 共识达成 决策=认同", d.get("consensus") is True and d.get("decision") == "认同",
        str(d.get("decision")))

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
