"""P2 端到端验证（API 层）：并行执行 + Manager(contract_net) 多 Agent 协作"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def api(path, body=None, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw[:200]}


# 1) 并行流程：3 个无依赖节点（同层并行）
flow_p = {
    "name": "【P2验证】并行流程",
    "description": "同层并行执行验证",
    "nodes": [
        {"id": "n1", "type": "llm", "label": "任务A", "config": {"prompt": "分析需求A"}},
        {"id": "n2", "type": "llm", "label": "任务B", "config": {"prompt": "分析需求B"}},
        {"id": "n3", "type": "llm", "label": "任务C", "config": {"prompt": "分析需求C"}},
        {"id": "n4", "type": "llm", "label": "汇总", "config": {"prompt": "汇总 {{n1.content}} {{n2.content}} {{n3.content}}"}},
    ],
    "edges": [
        {"source": "n1", "target": "n4"},
        {"source": "n2", "target": "n4"},
        {"source": "n3", "target": "n4"},
    ],
}
r = api("/api/studio/agent-flows", flow_p, "POST")
fid = r.get("id")
chk("保存并行流程", bool(fid))
r = api(f"/api/studio/agent-flows/{fid}/run", {"payload": {}}, "POST")
chk("并行流程 completed", r.get("status") == "completed", f"→ {r.get('status')}")
chk("标记并行模式", r.get("parallel") is True, f"→ parallel={r.get('parallel')}")
chk("3 个同层节点全部执行", all((r.get("results") or {}).get(nid, {}).get("status") == "done" for nid in ["n1", "n2", "n3"]),
    f"→ {[(nid, (r.get('results') or {}).get(nid, {}).get('status')) for nid in ['n1','n2','n3']]}")
chk("汇总节点正常", (r.get("results") or {}).get("n4", {}).get("status") == "done")
chk("并行有检查点", (r.get("checkpoint_count") or 0) == 4, f"→ {r.get('checkpoint_count')}")

# 2) Manager 协作流程（contract_net：招标→投标→授标）
flow_m = {
    "name": "【P2验证】Manager协作流程",
    "description": "合同网协议多 Agent 协作",
    "nodes": [
        {"id": "n1", "type": "tool", "label": "知识检索", "config": {"tool": "graph_retrieve", "arguments": {"query": "卫星通信"}}},
        {"id": "n2", "type": "orchestrator", "label": "Manager", "config": {
            "workers": "design,review", "task": "基于 {{n1.content}} 设计卫星通信方案", "strategy": "contract_net"}},
        {"id": "n3", "type": "llm", "label": "总结", "config": {"prompt": "Manager 结论：{{n2.content}}，请总结"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n3"},
    ],
}
r = api("/api/studio/agent-flows", flow_m, "POST")
fid2 = r.get("id")
chk("保存 Manager 流程", bool(fid2))
r = api(f"/api/studio/agent-flows/{fid2}/run", {"payload": {}}, "POST")
chk("Manager 流程 completed", r.get("status") == "completed", f"→ {r.get('status')}")
n2 = (r.get("results") or {}).get("n2", {})
chk("orchestrator 节点执行", n2.get("status") == "done", f"→ {n2.get('status')}")
chk("授标 Winner 存在", bool(n2.get("data", {}).get("winner")), f"→ winner={n2.get('data',{}).get('winner')}")
chk("投标数 >= 2", len(n2.get("data", {}).get("bids", [])) >= 2, f"→ bids={n2.get('data',{}).get('bids')}")
chk("内容含授标说明", "授标" in (n2.get("content") or ""), f"→ {(n2.get('content') or '')[:60]}")
chk("下游引用 Manager 输出", (r.get("results") or {}).get("n3", {}).get("status") == "done")

# 3) AI 生成支持 orchestrator 节点（Mock 模式 schema 校验）
import os
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
sys.path.insert(0, ".")
from ai_copilot import FlowCopilot, NODE_SCHEMA
chk("NODE_SCHEMA 含 orchestrator", "orchestrator" in NODE_SCHEMA)
g = FlowCopilot().generate("组织一个多 Agent 团队协作完成方案设计", deep_thinking=False)
chk("AI 生成（Mock）正常", g.get("ok"))

# 清理
api(f"/api/studio/agent-flows/{fid}", None, "DELETE")
api(f"/api/studio/agent-flows/{fid2}", None, "DELETE")
import sqlite3
conn = sqlite3.connect("mbse.db")
conn.execute("DELETE FROM flow_checkpoints WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id IN (?,?))", (fid, fid2))
conn.execute("DELETE FROM flow_run_steps WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id IN (?,?))", (fid, fid2))
conn.execute("DELETE FROM flow_conversations WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id IN (?,?))", (fid, fid2))
conn.execute("DELETE FROM flow_working_memory WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id IN (?,?))", (fid, fid2))
conn.execute("DELETE FROM flow_runs WHERE flow_id IN (?,?)", (fid, fid2))
conn.commit()
conn.close()
print()
print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
