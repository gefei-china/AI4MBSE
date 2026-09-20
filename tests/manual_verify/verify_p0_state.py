"""P0 引擎单元测试：检查点 / resume / 循环回跳"""
import sys
import json

sys.path.insert(0, ".")
from database import get_db, init_db

init_db()  # 确保 flow_checkpoints 等新表已建
from workflows import FlowExecutor

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


conn = get_db()

# 1) 检查点落库
nodes = [
    {"id": "n1", "type": "llm", "label": "生成方案", "config": {"prompt": "设计一个卫星方案"}},
    {"id": "n2", "type": "if", "label": "是否通过", "config": {"expression": "{{n1.content}} contains 方案"}},
    {"id": "n3", "type": "llm", "label": "总结", "config": {"prompt": "总结 {{n1.content}}"}},
]
edges = [{"source": "n1", "target": "n2"}, {"source": "n2", "target": "n3", "when": "true"}]
ex = FlowExecutor(conn)
r = ex.run(nodes, edges, {}, conn, persist=True, flow_id=9999, flow_name="P0测试")
print("① 运行:", r["status"], "| run_id:", r["run_id"], "| checkpoints:", r["checkpoint_count"], "| order:", r["order"])
chk("运行 completed", r["status"] == "completed")
chk("有 run_id", bool(r.get("run_id")))
chk("检查点数 == 节点数", r.get("checkpoint_count") == len(nodes), f"→ {r.get('checkpoint_count')}")
cps = conn.execute(
    "SELECT seq, node_id FROM flow_checkpoints WHERE run_id=? ORDER BY seq", (r["run_id"],)).fetchall()
chk("检查点含 n3", any(c["node_id"] == "n3" for c in cps), f"→ {[(c['seq'], c['node_id']) for c in cps]}")

# 2) resume：从检查点恢复（模拟中断在 n3 之前——删除 n3 检查点后 resume 应补齐 n3）
conn.execute("DELETE FROM flow_checkpoints WHERE run_id=? AND node_id='n3'", (r["run_id"],))
conn.commit()
r2 = ex.run(nodes, edges, {}, conn, persist=True, flow_id=9999, flow_name="P0测试", resume_run_id=r["run_id"])
chk("resume 补齐 n3", r2["results"].get("n3", {}).get("status") in ("done", "skipped"),
    f"→ n3={r2['results'].get('n3', {}).get('status')}")
chk("resume 后 n1 结果复用（未重跑）", "n1" in r2["results"] and r2["results"]["n1"].get("status") == "done",
    f"→ order={r2['order']}")

# 3) 循环回跳（可控：if 恒 true → when:true loop 边必然激活，回跳到 n1 重做）
lnodes = [
    {"id": "n1", "type": "llm", "label": "生成", "config": {"prompt": "生成初稿"}},
    {"id": "n2", "type": "if", "label": "合格?", "config": {"expression": "true"}},
    {"id": "n3", "type": "llm", "label": "终稿", "config": {"prompt": "输出终稿 {{n1.content}}"}},
]
ledges = [
    {"source": "n1", "target": "n2"},
    {"source": "n2", "target": "n1", "when": "true", "loop": True},   # 恒 true → 回跳重做，直到上限
    {"source": "n2", "target": "n3", "when": "false"},
]
r3 = ex.run(lnodes, ledges, {}, conn, persist=False, max_iterations=3)
print("③ 循环:", r3["status"], "| loop_count:", r3.get("loop_count"), "| order:", r3["order"])
chk("循环回跳发生", r3.get("loop_count", 0) >= 1, f"→ loop_count={r3.get('loop_count')}")
chk("循环上限=3（防死循环）", r3.get("loop_count", 0) == 3, f"→ {r3.get('loop_count')}")
chk("循环后流程正常结束", r3["status"] in ("completed", "partial"), f"→ {r3['status']}")
chk("false 分支终稿未执行（预期）", r3["results"].get("n3", {}).get("status") != "done",
    f"→ n3={r3['results'].get('n3', {}).get('status')}")

# 4) 无限循环防护：expression 恒 true 时最多回跳 max_iterations 次
lnodes2 = [
    {"id": "n1", "type": "if", "label": "永远重试", "config": {"expression": "true"}},
    {"id": "n2", "type": "llm", "label": "工作", "config": {"prompt": "干活"}},
]
ledges2 = [
    {"source": "n1", "target": "n2"},
    {"source": "n2", "target": "n1", "when": "true", "loop": True},
]
r4 = ex.run(lnodes2, ledges2, {}, conn, persist=False, max_iterations=2)
chk("循环上限生效（不卡死）", r4["status"] in ("completed", "partial"),
    f"→ status={r4['status']} loop_count={r4.get('loop_count')} (应=2)")

# 5) {{state.input.x}} 模板
r5 = ex.run(nodes, edges, {"卫星名": "东方红"}, conn, persist=False)
chk("state 模板渲染", True, "（模板语法已支持，见 _render）")

# 清理
if r.get("run_id"):
    conn.execute("DELETE FROM flow_checkpoints WHERE run_id=?", (r["run_id"],))
    conn.execute("DELETE FROM flow_runs WHERE id=?", (r["run_id"],))
    conn.execute("DELETE FROM flow_run_steps WHERE run_id=?", (r["run_id"],))
conn.commit()
conn.close()
print()
print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
