"""P0 完整链路验证：monkeypatch planner 返回合法 plan，驱动 _stream_orchestrated_flow 全路径
（subtask 实时事件 / 编排沉淀 / 记忆沉淀 / done 卡片结构）。落库后清理测试消息与流程。"""
import sys, os, json
sys.path.insert(0, os.getcwd())
import llm as llm_mod
from llm import llm_client

_orig_chat = llm_client.chat
def _fake_chat(messages, provider_id=None, stream=False, tools=None, thinking=False, **kw):
    if kw.get("_intent") == "planner":
        plan = {"tasks": [
            {"key": "t1", "title": "需求分析", "agent": "requirement_analysis", "deps": [], "task_type": "agent",
             "context": "解析宽带载荷需求", "expected_output": "条目化需求清单"},
            {"key": "t2", "title": "方案设计", "agent": "design", "deps": ["t1"], "task_type": "agent",
             "context": "基于需求生成方案", "expected_output": "多方案架构设计"}]}
        return {"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}],
                "_meta": {"used_mock": True, "provider": "fake-planner"}}
    if kw.get("_intent") == "plan_summary":
        return {"choices": [{"message": {"content": "# 自动编排汇总\n已完成需求分析与方案设计"}}],
                "_meta": {"used_mock": True, "provider": "fake-summary"}}
    return _orig_chat(messages, provider_id=provider_id, stream=stream, tools=tools, thinking=thinking, **kw)
llm_client.chat = _fake_chat

from agent.pipeline import AgentPipeline
from database import get_db

pipe = AgentPipeline()
pipe._load_db_agents()
pipe._mem_deposit_count = 0

cid = 1  # 复用会话 1（验证后清理该轮消息）
events = list(pipe.execute_stream("先做需求分析，再生成方案设计，最后输出报告", cid, dry_run=False))

subs = [e for e in events if e.get("type") == "subtask"]
print("[1] subtask 事件数(期望4=2任务×run/done):", len(subs))
for s in subs[:2]:
    print("    run:", s.get("key"), s.get("status"), "agent:", s.get("agent"), "deps:", s.get("deps"))
for s in subs[2:]:
    print("    done:", s.get("key"), s.get("status"), "latency_ms:", s.get("latency_ms"))

done = [e for e in events if e.get("type") == "done"]
card = done[0]["data"]["card"] if done else {}
print("[2] card.orchestrated:", card.get("orchestrated"), "| degraded:", card.get("degraded"))
print("[3] card.plan:", card.get("plan"))
print("[4] card.saved_flow_id:", card.get("saved_flow_id"), "| name:", card.get("saved_flow_name"))
print("[5] exec.subtasks 数:", len(((card.get("exec") or {}).get("subtasks") or [])))

# 记忆沉淀检查
conn = get_db()
mem = conn.execute("SELECT mem_type, content FROM agent_memory WHERE agent_id='design' AND source!='verify' ORDER BY id DESC LIMIT 2").fetchall()
print("[6] design 记忆沉淀:", [dict(r) for r in mem])
# 清理：本轮消息 + 沉淀流程
fid = card.get("saved_flow_id")
if fid:
    conn.execute("DELETE FROM agent_flows WHERE id=?", (fid,))
    print("[7] 清理流程 #", fid)
conn.execute("DELETE FROM messages WHERE conversation_id=? AND id>(SELECT COALESCE(MAX(id)-2,0) FROM messages WHERE conversation_id=?)", (cid, cid))
conn.commit()
conn.close()
print("CLEANED")
