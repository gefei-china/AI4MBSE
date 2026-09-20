"""D7 多 Agent 发布-订阅通信闭环验证（MetaGPT 共享消息池模式）。

T1. DB 消息池（串行路径）：发布落库(claimed=0) → 订阅认领(claimed=1+subscriber) → 内容注入输出/黑板
T2. 完整流程并行路径（内存池）：publish → subscribe(write_key) → code 节点校验注入内容
T3. 认领一次：双订阅者并发，仅首个认领（另一订阅者 claimed=false 无消息）
T4. 模板引用：code 产出 → publish topic/payload 引用 → subscribe 按渲染后 topic 认领
T5. run_id 隔离：不同运行的消息不互通；agent_messages 表/索引存在
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d7_test.db")
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


# ── T1: DB 消息池（串行路径）──
print("== T1: DB 消息池：发布落库 → 订阅认领 ==")
with db_conn() as conn:
    # 表与索引存在性
    tbl = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_messages'").fetchone()
    chk("T1 agent_messages 表存在", bool(tbl), str(tbl))
    idx = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_am_claim'").fetchone()
    chk("T1 topic/claimed 索引存在", bool(idx), str(idx))

    ex = FlowExecutor(conn)
    ex.run_id = 9001  # 模拟串行运行 run_id
    bb = {}
    out_pub = ex._exec_pubsub("发布节点", {"op": "publish", "topic": "report", "payload": "星座需求清单"},
                              {}, {}, conn)
    chk("T1 发布 done", out_pub["status"] == "done", str(out_pub.get("status")))
    row = conn.execute("SELECT * FROM agent_messages WHERE run_id=9001 AND topic='report'").fetchone()
    chk("T1 发布落库 claimed=0", bool(row) and row["claimed"] == 0, str(dict(row) if row else None))
    chk("T1 发布内容标准化", json.loads(row["content_json"])["text"] == "星座需求清单", str(row["content_json"]))

    out_sub = ex._exec_pubsub("订阅节点", {"op": "subscribe", "topic": "report", "write_key": "report"},
                              {}, {}, conn, bb)
    chk("T1 订阅 done", out_sub["status"] == "done", str(out_sub.get("status")))
    chk("T1 认领后内容注入 content", out_sub["content"] == "星座需求清单", str(out_sub["content"]))
    chk("T1 认领标记 claimed=true", out_sub["data"]["claimed"] is True, str(out_sub["data"]))
    row2 = conn.execute("SELECT * FROM agent_messages WHERE run_id=9001 AND topic='report'").fetchone()
    chk("T1 DB claimed=1 + subscriber", row2["claimed"] == 1 and row2["subscriber_node"] == "订阅节点",
        f"claimed={row2['claimed']} sub={row2['subscriber_node']}")
    chk("T1 黑板注入 write_key", bb.get("report") == {"text": "星座需求清单"}, str(bb.get("report")))

    out_sub2 = ex._exec_pubsub("订阅节点2", {"op": "subscribe", "topic": "report"}, {}, {}, conn)
    chk("T1 二次订阅无消息", out_sub2["data"]["claimed"] is False, str(out_sub2["data"]))


# ── T2: 完整流程并行路径（内存池）──
print("== T2: 并行路径：publish → subscribe → code 注入校验 ==")
with db_conn() as conn:
    nodes = [
        {"id": "n1", "type": "pubsub", "label": "发布者", "config": {
            "op": "publish", "topic": "report", "payload": "天基网络需求基线"}},
        {"id": "n2", "type": "pubsub", "label": "订阅者", "config": {
            "op": "subscribe", "topic": "report", "write_key": "report"}},
        {"id": "n3", "type": "code", "label": "校验节点", "config": {
            "language": "python", "timeout": 10,
            "inputs": {"report": "{{n2.content}}"},
            "code": "msg = _in.get('report', '')\n"
                    "_out['ok'] = (msg == '天基网络需求基线')\n"
                    "_out['msg'] = msg"}},
    ]
    edges = [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"}]
    fid = StudioRepo(conn).create_agent_flow("D7验证-并行", "d7",
                                             json.dumps(nodes, ensure_ascii=False),
                                             json.dumps(edges, ensure_ascii=False))
    flow = StudioRepo(conn).get_agent_flow(fid)
    r = FlowExecutor(conn).run(json.loads(flow["nodes"]), json.loads(flow["edges"]), {},
                               conn, persist=True, flow_id=fid, flow_name=flow["name"])
    chk("T2 运行 completed", r["status"] == "completed", str(r["status"]))
    sub = r["results"].get("n2", {})
    chk("T2 订阅节点认领", sub.get("status") == "done" and sub.get("data", {}).get("claimed") is True,
        str(sub))
    n3 = r["results"].get("n3", {})
    chk("T2 code 校验 ok", n3.get("status") == "done" and n3.get("data", {}).get("outputs", {}).get("ok") is True,
        str(n3.get("data", {}).get("outputs")))
    chk("T2 注入内容正确", n3.get("data", {}).get("outputs", {}).get("msg") == "天基网络需求基线",
        str(n3.get("data", {}).get("outputs", {}).get("msg")))


# ── T3: 认领一次（双订阅者并发）──
print("== T3: 认领一次：双订阅者仅首个消费 ==")
with db_conn() as conn:
    nodes = [
        {"id": "n1", "type": "pubsub", "label": "招标发布", "config": {
            "op": "publish", "topic": "task", "payload": "招标任务-姿态控制子系统"}},
        {"id": "n2", "type": "pubsub", "label": "订阅者A", "config": {
            "op": "subscribe", "topic": "task"}},
        {"id": "n3", "type": "pubsub", "label": "订阅者B", "config": {
            "op": "subscribe", "topic": "task"}},
        {"id": "n4", "type": "code", "label": "认领校验", "config": {
            "language": "python", "timeout": 10,
            "inputs": {"s1": "{{n2.data.claimed}}", "s2": "{{n3.data.claimed}}"},
            "code": "s1 = str(_in.get('s1', '')).lower()\n"
                    "s2 = str(_in.get('s2', '')).lower()\n"
                    "_out['one_claimed'] = (s1 == 'true' and s2 == 'false') or (s1 == 'false' and s2 == 'true')\n"
                    "_out['s1'] = s1\n_out['s2'] = s2"}},
    ]
    edges = [{"source": "n1", "target": "n2"}, {"source": "n1", "target": "n3"},
             {"source": "n2", "target": "n4"}, {"source": "n3", "target": "n4"}]
    fid = StudioRepo(conn).create_agent_flow("D7验证-认领一次", "d7",
                                             json.dumps(nodes, ensure_ascii=False),
                                             json.dumps(edges, ensure_ascii=False))
    flow = StudioRepo(conn).get_agent_flow(fid)
    r = FlowExecutor(conn).run(json.loads(flow["nodes"]), json.loads(flow["edges"]), {},
                               conn, persist=True, flow_id=fid, flow_name=flow["name"])
    chk("T3 运行 completed", r["status"] == "completed", str(r["status"]))
    n4 = r["results"].get("n4", {})
    chk("T3 恰一个订阅者认领", n4.get("data", {}).get("outputs", {}).get("one_claimed") is True,
        str(n4.get("data", {}).get("outputs")))
    # 认领者内容完整（认领方拿到招标任务）
    claimed_node = "n2" if r["results"].get("n2", {}).get("data", {}).get("claimed") else "n3"
    claimed_out = r["results"].get(claimed_node, {})
    chk("T3 认领者拿到任务内容", claimed_out.get("content") == "招标任务-姿态控制子系统",
        f"{claimed_node}: {str(claimed_out.get('content'))}")


# ── T4: 模板引用 topic/payload ──
print("== T4: topic/payload 模板引用 ==")
with db_conn() as conn:
    nodes = [
        {"id": "n1", "type": "code", "label": "产出名称", "config": {
            "language": "python", "timeout": 10, "inputs": {},
            "code": "_out['name'] = 'Alpha'\n_out['order_no'] = 'ORD-007'"}},
        {"id": "n2", "type": "pubsub", "label": "发布订单", "config": {
            "op": "publish", "topic": "order/{{n1.data.outputs.name}}",
            "payload": "订单-{{n1.data.outputs.order_no}}"}},
        {"id": "n3", "type": "pubsub", "label": "订阅订单", "config": {
            "op": "subscribe", "topic": "order/Alpha"}},
        {"id": "n4", "type": "code", "label": "订单校验", "config": {
            "language": "python", "timeout": 10,
            "inputs": {"c": "{{n3.content}}"},
            "code": "c = _in.get('c', '')\n_out['ok'] = (c == '订单-ORD-007')\n_out['c'] = c"}},
    ]
    edges = [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"}, {"source": "n3", "target": "n4"}]
    fid = StudioRepo(conn).create_agent_flow("D7验证-模板", "d7",
                                             json.dumps(nodes, ensure_ascii=False),
                                             json.dumps(edges, ensure_ascii=False))
    flow = StudioRepo(conn).get_agent_flow(fid)
    r = FlowExecutor(conn).run(json.loads(flow["nodes"]), json.loads(flow["edges"]), {},
                               conn, persist=True, flow_id=fid, flow_name=flow["name"])
    chk("T4 运行 completed", r["status"] == "completed", str(r["status"]))
    n3 = r["results"].get("n3", {})
    chk("T4 渲染后 topic 匹配认领", n3.get("data", {}).get("claimed") is True, str(n3.get("data")))
    n4 = r["results"].get("n4", {})
    chk("T4 渲染 payload 注入正确", n4.get("data", {}).get("outputs", {}).get("ok") is True,
        str(n4.get("data", {}).get("outputs", {}).get("c")))


# ── T5: run_id 隔离 ──
print("== T5: run_id 隔离 ==")
with db_conn() as conn:
    ex = FlowExecutor(conn)
    ex.run_id = 5001
    ex._exec_pubsub("发布A", {"op": "publish", "topic": "iso", "payload": "运行A的隔离消息"}, {}, {}, conn)
    ex.run_id = 5002
    out_b = ex._exec_pubsub("订阅B", {"op": "subscribe", "topic": "iso"}, {}, {}, conn)
    chk("T5 跨运行不互通", out_b["data"]["claimed"] is False, str(out_b["data"]))
    ex.run_id = 5001
    out_a = ex._exec_pubsub("订阅A", {"op": "subscribe", "topic": "iso"}, {}, {}, conn)
    chk("T5 本运行可认领", out_a["data"]["claimed"] is True and out_a["content"] == "运行A的隔离消息",
        str(out_a.get("data")))
    n_rows = conn.execute("SELECT COUNT(*) AS c FROM agent_messages WHERE run_id=5001").fetchone()["c"]
    chk("T5 消息归属 run_id=5001", n_rows == 1, f"{n_rows} 行")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
