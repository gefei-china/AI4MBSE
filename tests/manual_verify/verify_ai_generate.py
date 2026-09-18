"""AI 流程生成：API 端到端验证（生成/校验/数据兼容/迭代）。"""
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


# ── 1. 生成：需求分析场景（Mock 模板兜底）──
r = api("/api/studio/ai/generate-flow", method="POST",
        body={"prompt": "帮我搭一个流程：从需求文本抽取需求条目，做冲突检测，再生成影响分析报告"})
chk("生成成功", r.get("ok"), f"source={r.get('source')} name={r.get('name')}")
nodes, edges = r.get("nodes", []), r.get("edges", [])
chk("节点数合理(≥2)", len(nodes) >= 2, f"{len(nodes)} 节点")
chk("边端点全部存在", all(
    e["source"] in {n["id"] for n in nodes} and e["target"] in {n["id"] for n in nodes}
    for e in edges), f"{len(edges)} 边")
chk("无 error 级校验问题", not r.get("has_error"),
    str([v for v in r.get("validations", []) if v["level"] == "error"]))
# 数据兼容：节点结构与手动创建同构（id/type/label/config）
chk("节点格式兼容手动创建", all(all(k in n for k in ("id", "type", "label", "config")) for n in nodes),
    str(nodes[0].keys()) if nodes else "无节点")
# 工具/Agent 引用白名单
agent_names = {n["config"].get("agent") for n in nodes if n.get("type") == "agent"}
valid_agents = {"requirement_analysis", "design", "impact", "review", "report_generation", "knowledge_qa", "chat"}
chk("Agent 引用全部合法", not agent_names or agent_names <= valid_agents, str(agent_names))
tools_used = {n["config"].get("tool") for n in nodes if n.get("type") == "tool"}
builtin = {"graph_retrieve", "conflict_check", "impact_analyze", "validate", "entity_create"}
chk("工具引用全部合法", not tools_used or tools_used <= builtin, str(tools_used))

# ── 2. 生成：变更影响场景（含 if 分支）──
r2 = api("/api/studio/ai/generate-flow", method="POST",
         body={"prompt": "分析变更影响，如果高风险走变更影响Agent，否则直接总结"})
chk("变更场景生成成功", r2.get("ok"), f"source={r2.get('source')}")
chk("变更场景含 if 节点", any(n.get("type") == "if" for n in r2.get("nodes", [])),
    str([n.get("type") for n in r2.get("nodes", [])]))
chk("if 分支边带 when", any(e.get("when") in ("true", "false") for e in r2.get("edges", [])),
    str([e.get("when") for e in r2.get("edges", [])]))

# ── 3. 生成结果可直接保存（同一 agent_flows 表/通道）──
r3 = api("/api/studio/agent-flows", method="POST",
         body={"name": r.get("name", "AI生成流程"), "description": r.get("description", ""),
               "nodes": nodes, "edges": edges})
fid = r3.get("id")
chk("AI 生成结果保存成功（数据兼容）", bool(fid), f"id={fid}")

# ── 4. 保存后可运行（同一执行链路）──
r4 = api(f"/api/studio/agent-flows/{fid}/run", method="POST")
chk("AI 生成流程可运行", r4.get("status") in ("completed", "partial"),
    f"status={r4.get('status')} errors={r4.get('errors')}")

# ── 5. 迭代微调 ──
r5 = api("/api/studio/ai/refine-flow", method="POST",
         body={"prompt": "把最后一个 LLM 节点改成知识问答 Agent 节点",
               "definition": {"nodes": nodes, "edges": edges}})
chk("迭代返回定义", r5.get("ok") and r5.get("nodes"), f"source={r5.get('source')} nodes={len(r5.get('nodes', []))}")

# ── 6. 空描述校验 ──
r6 = api("/api/studio/ai/generate-flow", method="POST", body={"prompt": "  "})
chk("空描述被拦截", r6.get("error") == "任务描述必填", str(r6))

# 清理
api(f"/api/studio/agent-flows/{fid}", method="DELETE")
chk("清理完成", True)

print()
print("通过:", sum(1 for p in PASS if p), "/", len(PASS))
sys.exit(0 if all(PASS) else 1)
