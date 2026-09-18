"""P1 端到端验证（API 层）：共享黑板 / 订阅 / 长期记忆 / 会话记录"""
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


# 1) 黑板协作流程：A 写黑板 → B 订阅黑板 → C 用 {{blackboard.x}}
flow = {
    "name": "【P1验证】黑板协作流程",
    "description": "L1 工作记忆/订阅/长期记忆验证",
    "nodes": [
        {"id": "n1", "type": "llm", "label": "需求梳理", "config": {
            "prompt": "梳理需求：宽带通信卫星需支持 2Gbps 吞吐",
            "write_keys": "requirements"}},
        {"id": "n2", "type": "agent", "label": "方案设计", "config": {
            "agent": "design", "query": "根据需求设计卫星方案",
            "subscribe": "requirements"}},
        {"id": "n3", "type": "llm", "label": "汇总", "config": {
            "prompt": "结合黑板需求 {{blackboard.requirements}} 和方案，输出最终结论"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n3"},
    ],
}
r = api("/api/studio/agent-flows", flow, "POST")
fid = r.get("id")
chk("保存流程", bool(fid), f"→ id={fid}")

r = api(f"/api/studio/agent-flows/{fid}/run", {"payload": {}}, "POST")
rid = r.get("run_id")
chk("运行 completed", r.get("status") == "completed", f"→ {r.get('status')} run={rid}")

# 2) 黑板内容（write_keys 写入）
bb = api(f"/api/studio/memory/blackboard?run_id={rid}")
chk("黑板有 requirements", any(x.get("key") == "requirements" for x in bb), f"→ {[x.get('key') for x in bb]}")
if bb:
    req_item = next((x for x in bb if x.get("key") == "requirements"), None)
    chk("黑板值非空", bool(req_item and str(req_item.get("value", ""))), f"→ {str(req_item.get('value',''))[:40]}")

# 3) n2 订阅生效（agent 输出应包含订阅上下文——检查运行结果 data）
n2 = (r.get("results") or {}).get("n2", {})
chk("订阅节点执行", n2.get("status") == "done", f"→ {n2.get('status')}")
chk("agent 长期记忆沉淀", n2.get("data", {}).get("memory_hits") is not None, f"→ memory_hits={n2.get('data',{}).get('memory_hits')}")

# 4) {{blackboard.requirements}} 模板渲染（n3 prompt 含黑板值 → 结果非空）
n3 = (r.get("results") or {}).get("n3", {})
chk("n3 汇总执行", n3.get("status") == "done", f"→ {n3.get('status')}")
chk("n3 结果非空", bool((n3.get("content") or "").strip()), f"→ {str(n3.get('content',''))[:50]}")

# 5) 编排级会话记录
import sqlite3
conn = sqlite3.connect("mbse.db")
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM flow_conversations WHERE run_id=?", (rid,))
conv_count = c.fetchone()[0]
chk("编排会话已记录", conv_count >= 2, f"→ {conv_count} 条")
c.execute("SELECT COUNT(*) FROM agent_memory WHERE agent_id='design'")
mem_count = c.fetchone()[0]
chk("长期记忆已沉淀(design)", mem_count >= 1, f"→ {mem_count} 条")

# 6) 长期记忆端点：手动读写
r = api("/api/studio/agents/design/memory", {"content": "卫星方案应优先考虑 Ka 频段", "mem_type": "preference"}, "POST")
chk("记忆手动写入", bool(r.get("ok")), f"→ {r.get('id')}")
mem = api("/api/studio/agents/design/memory")
chk("记忆列表读取", isinstance(mem, list) and len(mem) >= 1, f"→ {len(mem)} 条")

# 清理
api(f"/api/studio/agent-flows/{fid}", None, "DELETE")
conn.execute("DELETE FROM flow_checkpoints WHERE run_id=?", (rid,))
conn.execute("DELETE FROM flow_run_steps WHERE run_id=?", (rid,))
conn.execute("DELETE FROM flow_conversations WHERE run_id=?", (rid,))
conn.execute("DELETE FROM flow_working_memory WHERE run_id=?", (rid,))
conn.execute("DELETE FROM flow_runs WHERE id=?", (rid,))
conn.execute("DELETE FROM agent_memory WHERE agent_id='design' AND content LIKE '%验证%'")
conn.commit()
conn.close()
print()
print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
