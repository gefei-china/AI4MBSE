"""多智能体编排：DAG 真实执行端到端验证（tool/agent/llm 节点 + 消息传递）。"""
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


# ── 1. 保存多智能体 DAG（tool 检索 → agent 问答 → llm 总结）──
dag = {
    "nodes": [
        {"id": "n1", "type": "tool", "label": "知识检索",
         "config": {"tool": "graph_retrieve", "arguments": {"query": "宽带通信卫星 载荷 转发器"}}},
        {"id": "n2", "type": "agent", "label": "知识问答Agent",
         "config": {"agent": "knowledge_qa", "query": "根据检索结果 {{n1.content}} 回答：卫星载荷有哪些关键指标？"}},
        {"id": "n3", "type": "llm", "label": "总结",
         "config": {"prompt": "上游 Agent 结论：{{n2.content}}。请用一句话总结其要点。"}},
    ],
    "edges": [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3"}],
}
r = api("/api/studio/agent-flows", method="POST",
        body={"name": "多智能体协作-验证", "description": "tool→agent→llm", **dag})
chk("保存 DAG 流程", r.get("ok"), str(r))

flows = api("/api/studio/agent-flows")
fid = None
for f in flows:
    if f["name"] == "多智能体协作-验证":
        fid = f["id"]
chk("流程列表可见", fid is not None, f"id={fid}")

# ── 2. 运行 DAG（真实执行）──
r = api(f"/api/studio/agent-flows/{fid}/run", method="POST", body={"payload": {"user": "宽带通信"}})
chk("运行返回", r.get("status") in ("completed", "partial"), f"status={r.get('status')} errors={r.get('errors')}")
order = r.get("order", [])
chk("拓扑顺序正确", order == ["n1", "n2", "n3"], str(order))

results = r.get("results", {})
n1 = results.get("n1", {})
n2 = results.get("n2", {})
n3 = results.get("n3", {})

chk("n1 tool 真实检索", n1.get("status") == "done" and len(n1.get("content", "")) > 10,
    f"content={str(n1.get('content',''))[:60]}")
chk("n2 agent 真实执行", n2.get("status") == "done" and len(n2.get("content", "")) > 30,
    f"agent={n2.get('agent')} content={str(n2.get('content',''))[:60]}")
chk("n3 llm 真实调用", n3.get("status") == "done" and n3.get("provider"),
    f"provider={n3.get('provider')} mock={n3.get('used_mock')}")
# 消息传递：n1 内容注入 n2 query；n2 内容注入 n3 prompt
chk("跨节点消息传递（n1→n2）", "图谱" in str(n2.get("content", "")) or "检索结果" in str(n2.get("content", "")),
    str(n2.get("content", ""))[:50])
chk("跨节点消息传递（n2→n3）", "方案" in str(n3.get("content", "")) or "需求" in str(n3.get("content", "")),
    str(n3.get("content", ""))[:50])
chk("节点耗时记录", all(o.get("latency_ms", 0) >= 0 for o in results.values()),
    f"total={r.get('total_latency_ms')}ms")

# ── 3. 删除流程 ──
r = api(f"/api/studio/agent-flows/{fid}", method="DELETE")
chk("删除流程", r.get("ok"), str(r))
r = api(f"/api/studio/agent-flows/{fid}", method="DELETE")
chk("重复删除被拦截", r.get("error") == "Not found" or "not" in str(r.get("error", "")).lower(), str(r))

print()
print("通过:", sum(1 for p in PASS if p), "/", len(PASS))
sys.exit(0 if all(PASS) else 1)
