"""多智能体三项升级：API 端到端验证（if 分支/结构化 state/运行轨迹）。"""
import json
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


PASS = []


def chk(name, cond, info=""):
    PASS.append(bool(cond))
    print(("✅" if cond else "❌"), name, ("| " + info if info else ""))


# ── 1. 保存含 if 分支的 DAG ──
dag = {
    "nodes": [
        {"id": "n1", "type": "tool", "label": "知识检索",
         "config": {"tool": "graph_retrieve", "arguments": {"query": "宽带通信卫星 载荷"}}},
        {"id": "n2", "type": "if", "label": "图谱命中?",
         "config": {"expression": "{{n1.data.result}} contains 图谱 0 条"}},
        {"id": "n3", "type": "agent", "label": "知识问答",
         "config": {"agent": "knowledge_qa", "query": "回答问题：{{n1.content}}"}},
        {"id": "n4", "type": "llm", "label": "无命中兜底",
         "config": {"prompt": "图谱未命中：{{n1.content}}"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n3", "when": "false"},
        {"source": "n2", "target": "n4", "when": "true"},
    ],
}
r = api("/api/studio/agent-flows", method="POST",
        body={"name": "条件分支验证流程", "description": "if 分支测试", **dag})
fid = r.get("id")
chk("保存含 if 的流程", bool(fid), f"id={fid}")

# ── 2. 运行（definition 传入，if 分支应跳过 n3 执行 n4）──
r = api(f"/api/studio/agent-flows/{fid}/run", method="POST", body={"definition": dag})
chk("运行状态", r.get("status") in ("completed", "partial"), f"status={r.get('status')}")
res = r.get("results", {})
n2 = res.get("n2", {})
n3 = res.get("n3", {})
n4 = res.get("n4", {})
chk("if 节点求值 data.value=true", n2.get("data", {}).get("value") is True, f"value={n2.get('data',{}).get('value')}")
chk("false 分支 n3 被跳过", n3.get("status") == "skipped", f"status={n3.get('status')}")
chk("true 分支 n4 执行", n4.get("status") == "done", f"status={n4.get('status')}")
chk("skipped 列表标记", "n3" in r.get("skipped", []), str(r.get("skipped")))
chk("结构化引用 {{n1.data.tool}}", res.get("n1", {}).get("data", {}).get("tool") == "graph_retrieve",
    str(res.get("n1", {}).get("data", {}).get("tool")))

# ── 3. 运行轨迹落库 ──
runs = api("/api/studio/flow-runs")
chk("运行历史列表", len(runs) >= 1, f"{len(runs)} 条")
latest = runs[0]
chk("历史含本次运行", latest.get("flow_name") == "条件分支验证流程" and latest.get("node_count") == 4,
    f"flow={latest.get('flow_name')} nodes={latest.get('node_count')} status={latest.get('status')}")
rid = latest.get("id")
detail = api(f"/api/studio/flow-runs/{rid}")
chk("运行详情含 steps", len(detail.get("steps", [])) == 4, f"steps={len(detail.get('steps', []))}")
step2 = next((s for s in detail.get("steps", []) if s.get("node_id") == "n2"), {})
chk("step 记录 if data.value", step2.get("data", {}).get("value") is True, str(step2.get("data", {})))

# ── 4. 清理 ──
api(f"/api/studio/agent-flows/{fid}", method="DELETE")
api(f"/api/studio/flow-runs/{rid}", method="DELETE")
chk("清理流程与运行记录", True)

print()
print("通过:", sum(1 for p in PASS if p), "/", len(PASS))
sys.exit(0 if all(PASS) else 1)
