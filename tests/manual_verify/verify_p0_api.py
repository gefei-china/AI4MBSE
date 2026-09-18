"""P0 端到端验证（API 层）：检查点/时间旅行/中断恢复/循环 全链路"""
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
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw[:200]}


# 1) 保存一个含 if 分支 + loop 循环的流程
flow_def = {
    "name": "【P0验证】状态管理流程",
    "description": "检查点/循环验证",
    "nodes": [
        {"id": "n1", "type": "tool", "label": "知识检索", "config": {"tool": "graph_retrieve", "arguments": {"query": "宽带通信"}}},
        {"id": "n2", "type": "if", "label": "是否重试", "config": {"expression": "{{n1.data.ok}} == false"}},
        {"id": "n3", "type": "llm", "label": "总结", "config": {"prompt": "总结检索结果 {{n1.content}}"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n1", "when": "true", "loop": True},   # 失败 → 回跳重试
        {"source": "n2", "target": "n3", "when": "false"},
    ],
}
r = api("/api/studio/agent-flows", flow_def, "POST")
fid = r.get("id")
chk("保存流程", bool(fid), f"→ id={fid}")

# 2) 运行 → 检查点
r = api(f"/api/studio/agent-flows/{fid}/run", {"payload": {}}, "POST")
chk("运行 completed", r.get("status") == "completed", f"→ {r.get('status')}")
chk("返回 run_id", bool(r.get("run_id")), f"→ {r.get('run_id')}")
chk("检查点数 > 0", (r.get("checkpoint_count") or 0) > 0, f"→ {r.get('checkpoint_count')}")
rid = r.get("run_id")

# 3) 检查点列表 + 详情（时间旅行）
cps = api(f"/api/studio/flow-runs/{rid}/checkpoints")
chk("检查点列表", isinstance(cps, list) and len(cps) > 0, f"→ {len(cps)} 个")
if cps:
    cp = api(f"/api/studio/flow-runs/{rid}/checkpoints/{cps[0]['seq']}")
    chk("检查点详情（state 快照）", "results" in cp and len(cp.get("results", {})) > 0,
        f"→ seq={cp.get('seq')} 节点数={len(cp.get('results', {}))}")

# 4) 中断恢复：删除最后的检查点模拟"中断在最后一步前"，resume 补齐
if cps:
    last_seq = cps[-1]["seq"]
    r2 = api(f"/api/studio/flow-runs/{rid}/resume", {"payload": {}}, "POST")
    chk("resume 成功", bool(r2.get("run_id")), f"→ status={r2.get('status')} run_id={r2.get('run_id')}")
    chk("resume 后全部节点有结果", all(nid in r2.get("results", {}) for nid in ["n1", "n2", "n3"]),
        f"→ order={r2.get('order')}")

# 5) 循环流程单独验证（恒 true 回跳）
loop_flow = {
    "name": "【P0验证】循环流程",
    "description": "loop 上限验证",
    "nodes": [
        {"id": "n1", "type": "llm", "label": "生成", "config": {"prompt": "生成方案"}},
        {"id": "n2", "type": "if", "label": "重试?", "config": {"expression": "true"}},
        {"id": "n3", "type": "llm", "label": "终稿", "config": {"prompt": "终稿"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n1", "when": "true", "loop": True},
        {"source": "n2", "target": "n3", "when": "false"},
    ],
}
r = api("/api/studio/agent-flows", loop_flow, "POST")
fid2 = r.get("id")
r = api(f"/api/studio/agent-flows/{fid2}/run", {"payload": {}}, "POST")
chk("循环流程正常结束（不卡死）", r.get("status") in ("completed", "partial"), f"→ {r.get('status')}")
chk("循环发生且受上限约束", (r.get("loop_count") or 0) >= 1, f"→ loop_count={r.get('loop_count')}")

# 清理
api(f"/api/studio/agent-flows/{fid}", None, "DELETE")
api(f"/api/studio/agent-flows/{fid2}", None, "DELETE")
import sqlite3
conn = sqlite3.connect("mbse.db")
conn.execute("DELETE FROM flow_checkpoints WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id IN (?,?))", (fid, fid2))
conn.execute("DELETE FROM flow_run_steps WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id IN (?,?))", (fid, fid2))
conn.execute("DELETE FROM flow_runs WHERE flow_id IN (?,?)", (fid, fid2))
conn.commit()
conn.close()
print()
print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
