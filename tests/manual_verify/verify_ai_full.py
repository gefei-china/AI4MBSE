"""AI 生成完整链路验证：真实 LLM 生成 → 保存 → 运行 → 轨迹 → 历史。
验证数据保留在库中（不清理），作为经过验证的演示流程。
"""
import json
import os
import sys

sys.path.insert(0, ".")
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


print("══════ 完整链路：AI 生成 → 保存 → 运行 → 轨迹 ══════\n")

# ── 1. 真实 LLM 生成 ──
r = api("/api/studio/ai/generate-flow", method="POST",
        body={"prompt": "从需求文本抽取需求条目，做冲突检测，再生成影响分析报告"})
chk("① 真实 LLM 生成（非模板）", r.get("source") == "llm", f"source={r.get('source')} 耗时见上")
chk("② 生成结果含 if 分支", any(n.get("type") == "if" for n in r.get("nodes", [])),
    str([n.get("type") for n in r.get("nodes", [])]))
chk("③ 校验无 error", not r.get("has_error"),
    str([v for v in r.get("validations", []) if v["level"] == "error"]))

# ── 2. 保存（数据兼容：同一 agent_flows 通道）──
flow_name = "【AI验证】需求冲突影响分析流程"
r2 = api("/api/studio/agent-flows", method="POST",
         body={"name": flow_name, "description": r.get("description", "AI 生成验证流程"),
               "nodes": r.get("nodes", []), "edges": r.get("edges", [])})
fid = r2.get("id")
chk("④ 保存成功", bool(fid), f"flow_id={fid}")

# ── 3. 运行（真实执行 + 轨迹落库）──
r3 = api(f"/api/studio/agent-flows/{fid}/run", method="POST")
chk("⑤ 运行 completed", r3.get("status") == "completed", f"status={r3.get('status')} errors={r3.get('errors')}")

# ── 4. 轨迹与历史 ──
runs = api("/api/studio/flow-runs?limit=5")
latest = runs[0] if runs else {}
chk("⑥ 运行历史含本次", latest.get("flow_id") == fid and latest.get("node_count", 0) >= 3,
    f"run_id={latest.get('id')} nodes={latest.get('node_count')} status={latest.get('status')}")
detail = api(f"/api/studio/flow-runs/{latest.get('id')}")
steps = detail.get("steps", [])
chk("⑦ 轨迹逐步结果完整", len(steps) >= 3 and any(s.get("data", {}).get("provider") for s in steps),
    f"steps={len(steps)} 首节点provider={steps[0].get('data', {}).get('provider') if steps else '-'}")

# ── 5. 迭代微调 ──
r4 = api("/api/studio/ai/refine-flow", method="POST",
         body={"prompt": "在流程最后追加一个 LLM 总结节点",
               "definition": {"nodes": r.get("nodes", []), "edges": r.get("edges", [])}})
chk("⑧ 迭代追加 LLM 节点", r4.get("source") == "llm" and r4.get("nodes") and r4["nodes"][-1].get("type") == "llm",
    f"source={r4.get('source')} 末节点={r4.get('nodes', [])[-1].get('type') if r4.get('nodes') else '-'}")

# ── 6. Mock 兜底仍可用（无 LLM 时稳定）──
import os
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
# 服务端进程不受本进程 env 影响；直接验证 Mock 模板逻辑
from ai_copilot import FlowCopilot
mock = FlowCopilot()._mock_generate("帮我做知识问答")
chk("⑨ Mock 兜底确定性生成", mock.get("nodes") and mock["edges"], f"{len(mock.get('nodes', []))} 节点")

print()
print("════════ 验证结论 ════════")
print(f"通过: {sum(1 for p in PASS if p)} / {len(PASS)}")
print(f"保留验证数据: agent_flows id={fid}（{flow_name}）+ flow_runs id={latest.get('id')}")
sys.exit(0 if all(PASS) else 1)
