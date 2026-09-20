"""P0 后端验证：编排沉淀/复用 + 记忆读取/沉淀（临时数据，验证后清理）。"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))) if sys.argv and sys.argv[0] else os.getcwd()
sys.path.insert(0, ROOT)
from agent import AgentPipeline
from database import get_db

pipe = AgentPipeline()
pipe._load_db_agents()
pipe._mem_deposit_count = 0

# ── 1. P0-1 编排沉淀 ──
plan = [
    {"key": "t1", "title": "需求分析", "agent": "requirement_analysis", "deps": [],
     "context": "解析宽带载荷需求", "expected_output": "条目化需求清单"},
    {"key": "t2", "title": "方案设计", "agent": "design", "deps": ["t1"],
     "context": "基于需求生成方案", "expected_output": "多方案架构设计"},
]
fid = pipe._save_planner_flow("先做需求分析再生成方案设计", plan, "design")
print("[1] _save_planner_flow ->", fid)
conn = get_db()
row = conn.execute("SELECT * FROM agent_flows WHERE id=?", (fid,)).fetchone()
print("[2] 落库:", dict(row) if row else None)
conn.close()

# ── 2. P0-1 复用（发布后语义匹配执行）──
conn = get_db()
conn.execute("UPDATE agent_flows SET status='published' WHERE id=?", (fid,))
conn.commit()
conn.close()
reused = pipe._try_reuse_planner_flow("先做需求分析，再生成方案设计", "design")
print("[3] 复用:", {"flow_id": reused.get("flow_id"), "flow_name": reused.get("flow_name"),
                    "content_len": len(reused.get("content") or ""),
                    "plan": reused.get("plan")} if reused else None)

# ── 3. P0-3 记忆读取 ──
from memory_service import MemoryService
conn = get_db()
MemoryService.deposit(conn, "design", "SysML v2 视图必须按 BDD/UC/ACT 分独立包生成（建模规范）",
                      "experience", source="verify")
conn.close()
hint = pipe._build_memory_hint("生成建模视图", "design")
print("[4] 记忆读取:", (hint[:80] + "...") if hint else "(无命中)")

# ── 4. P0-3 记忆沉淀（每会话限 2 次）──
pipe._deposit_session_memory("测试", "这是一段足够长的用于验证记忆沉淀机制的建模经验内容，包含可复用的领域知识。", "design")
pipe._deposit_session_memory("测试2", "第二段用于验证沉淀频率限制的建模经验内容。", "design")
pipe._deposit_session_memory("测试3", "第三段不应被沉淀（超过每会话2次限制）的内容。", "design")
conn = get_db()
cnt = conn.execute("SELECT COUNT(*) c FROM agent_memory WHERE source='verify' OR content LIKE '%验证记忆沉淀机制%'").fetchone()["c"]
conn.close()
print("[5] 沉淀条数(期望1-2):", cnt)

# ── 清理 ──
conn = get_db()
conn.execute("DELETE FROM agent_flows WHERE id=?", (fid,))
conn.execute("DELETE FROM agent_memory WHERE source='verify' OR content LIKE '%验证记忆沉淀机制%' OR content LIKE '%建模规范%'")
conn.commit()
conn.close()
print("CLEANED")
