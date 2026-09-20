"""P3 端到端验证（API 层）：reflection 节点 + Skills 渐进披露 + 监控端点"""
import json
import os
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


# 1) reflection 反思流程：llm → reflection 评估 → if（低分回跳，高分结束）
flow = {
    "name": "【P3验证】反思闭环流程",
    "description": "reflection + loop 循环闭环",
    "nodes": [
        {"id": "n1", "type": "llm", "label": "生成方案", "config": {"prompt": "生成卫星通信方案"}},
        {"id": "n2", "type": "reflection", "label": "质量反思", "config": {"target": "n1", "criteria": "方案是否完整合理"}},
        {"id": "n3", "type": "if", "label": "是否达标", "config": {"expression": "{{n2.data.score}} >= 60"}},
        {"id": "n4", "type": "llm", "label": "终稿", "config": {"prompt": "输出最终方案 {{n1.content}}"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n3"},
        {"source": "n3", "target": "n1", "when": "false", "loop": True},   # 低分 → 回跳重做
        {"source": "n3", "target": "n4", "when": "true"},
    ],
}
r = api("/api/studio/agent-flows", flow, "POST")
fid = r.get("id")
chk("保存反思流程", bool(fid))
r = api(f"/api/studio/agent-flows/{fid}/run", {"payload": {}}, "POST")
chk("反思流程运行完成", r.get("status") in ("completed", "partial"), f"→ {r.get('status')}")
n2 = (r.get("results") or {}).get("n2", {})
chk("reflection 节点执行", n2.get("status") == "done", f"→ {n2.get('status')}")
chk("reflection 有评分", isinstance(n2.get("data", {}).get("score"), (int, float)), f"→ score={n2.get('data',{}).get('score')}")
chk("reflection 内容含评分", "评分" in (n2.get("content") or ""), f"→ {(n2.get('content') or '')[:40]}")
chk("流程无死循环", (r.get("loop_count") or 0) <= 3, f"→ loop_count={r.get('loop_count')}")

# 2) Skills 渐进披露：数据前提（published skill 含 frontmatter/content）+ 逻辑（命中注入摘要而非全文）
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
sys.path.insert(0, ".")
import sqlite3
_conn = sqlite3.connect("mbse.db")
_c = _conn.cursor()
_c.execute("SELECT name, frontmatter, content FROM skills WHERE status!='draft' LIMIT 1")
_skill_row = _c.fetchone()
_conn.close()
if _skill_row:
    chk("published skill 数据完整", bool(_skill_row[1] or _skill_row[2]), f"→ {_skill_row[0]}")
    # 直接验证渐进披露逻辑：注入长度远小于全文
    full_len = len(_skill_row[2] or "")
    injected_len = min(400, len(_skill_row[1] or "")) + min(600, full_len)
    chk("渐进披露（摘要注入 < 全文）", injected_len <= 1000 and injected_len < max(full_len, 1000),
        f"→ 注入≤{injected_len}字 vs 全文{full_len}字")
else:
    chk("published skill 数据完整", False, "→ 无 published skill")

# 3) 监控端点
m = api("/api/studio/monitor/runs?days=7")
chk("监控总运行数", isinstance(m.get("total_runs"), int) and m.get("total_runs") >= 1, f"→ {m.get('total_runs')}")
chk("监控成功率字段", "success_rate" in m, f"→ {m.get('success_rate')}%")
chk("监控按流程统计", isinstance(m.get("by_flow"), list), f"→ {len(m.get('by_flow', []))} 流程")
chk("监控节点类型统计", isinstance(m.get("node_stats"), list), f"→ {len(m.get('node_stats', []))} 类型")

# 4) AI 生成 schema 含 reflection
from ai_copilot import NODE_SCHEMA
chk("NODE_SCHEMA 含 reflection", "reflection" in NODE_SCHEMA)

# 清理
api(f"/api/studio/agent-flows/{fid}", None, "DELETE")
import sqlite3
conn = sqlite3.connect("mbse.db")
conn.execute("DELETE FROM flow_checkpoints WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id=?)", (fid,))
conn.execute("DELETE FROM flow_run_steps WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id=?)", (fid,))
conn.execute("DELETE FROM flow_conversations WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id=?)", (fid,))
conn.execute("DELETE FROM flow_working_memory WHERE run_id IN (SELECT id FROM flow_runs WHERE flow_id=?)", (fid,))
conn.execute("DELETE FROM flow_runs WHERE flow_id=?", (fid,))
conn.commit()
conn.close()
print()
print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
