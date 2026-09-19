"""经验回流（R1=B 2026-09-01）：对话/评审/变更影响分析的结论 → 智能体平台记忆/经验知识。

定位（用户决策）：评审与变更结论应成为 Agent 平台的记忆与经验，**不经过数据处理流程**
（v2g 候选确认流已移除）——直接双写：
1. project_memories（项目记忆，category=决策/规范/经验；pipeline 运行时自动注入 Constitution 块）
2. agent_memory（Agent 长期记忆，mem_type=experience；MemoryService 写真 embedding，语义检索）

行业对齐：知识层是"活资产"（Siemens 数字线程 / visuresolutions 从运行中学习）——
每次会话/评审/变更产生的结论不流失，成为下次任务的记忆上下文。
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# 提炼目标：知识类型（对齐本体 entity 类型 + 经验）
KNOWLEDGE_TYPES = ["需求", "部件", "功能", "约束", "接口", "经验", "决策", "风险"]


def _mock_extract_knowledge(text: str) -> list:
    """Mock 降级：规则提炼——含领域关键词的完整句作为候选知识（确定性可回归）。"""
    if not text or len(text.strip()) < 30:
        return []
    sentences = re.split(r"(?<=[。！？；;！?\n])", text)
    out = []
    for s in sentences:
        s = s.strip()
        if len(s) < 15 or len(s) > 300:
            continue
        # 含知识信号词 → 值得沉淀
        if any(k in s for k in ("需求", "约束", "接口", "方案", "采用", "必须", "应", "风险",
                                "决策", "经验", "设计为", "采用", "基于")):
            out.append({
                "name": s[:60],
                "entity_type": _classify(s),
                "properties": {"source_frag": s[:300], "reflow": True},
            })
        if len(out) >= 5:
            break
    return out


def _classify(sentence: str) -> str:
    """规则分类候选知识类型（Mock 降级用）。"""
    if any(k in sentence for k in ("风险", "故障", "失效", "隐患")):
        return "风险"
    if any(k in sentence for k in ("决策", "决定", "选择")):
        return "决策"
    if any(k in sentence for k in ("需求", "要求", "必须", "应 ")):
        return "需求"
    if any(k in sentence for k in ("接口", "连接", "端口")):
        return "接口"
    if any(k in sentence for k in ("经验", "教训", "建议")):
        return "经验"
    if any(k in sentence for k in ("采用", "设计为", "方案", "配置")):
        return "功能"
    return "事实"


def reflow_from_text(conn, text: str, source: str = "conversation", batch_id: str = "",
                    project_id: str = "", agent_id: str = "reflow") -> dict:
    """从一段文本提炼经验知识 → 项目记忆(project_memories) + Agent 长期记忆(agent_memory) 双写。

    R1=B（2026-09-01）：评审/变更结论是智能体平台的记忆/经验知识，不再走 v2g 候选确认流——
    直接写入记忆层（pipeline 运行时自动注入 project_memories Constitution 块）。
    返回 {batch_id, candidates, degraded, memories}。
    """
    if not text or not text.strip():
        return {"batch_id": batch_id, "candidates": 0, "degraded": True}
    import uuid as _u
    batch_id = batch_id or f"reflow-{_u.uuid4().hex[:8]}"
    candidates = []

    # 1) LLM 提炼（真实 LLM 可用时；失败降级规则）
    degraded = True
    try:
        from llm import llm_client
        from agent import AgentPipeline
        resp = llm_client.chat(
            [{"role": "system", "content": (
                "你是 MBSE 知识管理员。从下面的工程对话/分析产出中提炼值得沉淀进知识库的候选知识"
                "（实体：需求/部件/功能/接口/约束；经验/决策/风险）。"
                "只输出 JSON 数组：[{\"name\": \"实体名\", \"entity_type\": \"需求|部件|功能|接口|约束|经验|决策|风险\", "
                "\"note\": \"一句话说明\"}]，最多 5 条；无价值输出 []。不要其他文字。")},
             {"role": "user", "content": str(text)[:4000]}],
            _intent="knowledge_reflow")
        content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        parsed = AgentPipeline._parse_json_block(content)
        if isinstance(parsed, list):
            candidates = [c for c in parsed if isinstance(c, dict) and c.get("name")]
            degraded = False
    except Exception:
        candidates = []

    # 2) 降级：规则提炼
    if not candidates:
        mock = _mock_extract_knowledge(text)
        candidates = [{"name": c["name"], "entity_type": c["entity_type"], "note": "规则提炼"} for c in mock]
        degraded = True

    # 3) 双写记忆层（R1=B）：project_memories（项目经验/决策，pipeline 自动注入）+ agent_memory（Agent 长期经验）
    n = 0
    try:
        from memory_service import MemoryService
        _has_ms = True
    except Exception:
        _has_ms = False
    for c in candidates[:5]:
        et = c.get("entity_type", "事实")
        if et not in ("需求", "部件", "功能", "接口", "约束", "经验", "决策", "风险"):
            et = "事实"
        title = str(c.get("name", ""))[:100]
        note = str(c.get("note", ""))[:200] or "评审/变更结论沉淀"
        content = f"{title}：{note}（来源 {source[:60]}）"
        # 3.1 项目记忆（category 映射：决策→决策；约束→规范；其余→经验）
        cat_map = {"决策": "决策", "约束": "规范", "规范": "规范"}
        category = cat_map.get(et, "经验")
        try:
            conn.execute(
                "INSERT INTO project_memories (project_id, category, title, content, enabled, created_by) "
                "VALUES (?,?,?,?,1,'reflow')",
                (project_id[:80], category, title[:120], content[:500]))
            n += 1
        except Exception as e:
            logger.warning("项目记忆写入失败，跳过（batch=%s）: %s", batch_id, e)
        # 3.2 Agent 长期记忆（experience 类型，MemoryService 写真 embedding）
        if _has_ms:
            try:
                MemoryService.deposit(conn, agent_id, content, mem_type="experience",
                                      source="reflow", mem_topic=category)
            except Exception as e:
                logger.warning("Agent 记忆写入失败，跳过（batch=%s）: %s", batch_id, e)
    conn.commit()
    return {"batch_id": batch_id, "candidates": n, "degraded": degraded,
            "memories": n, "project_id": project_id, "agent_id": agent_id}


def reflow_from_conversation(conn, conversation_id: int, limit_messages: int = 20) -> dict:
    """从最近会话消息提炼知识候选（P2-1 会话级回流）。

    取最近 N 条 user/assistant 消息拼接 → reflow_from_text。
    """
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT ?", (conversation_id, limit_messages)).fetchall()
        if not rows:
            return {"candidates": 0, "error": "无消息"}
        proj = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                            (conversation_id,)).fetchone()
        pid = (proj["project_id"] if proj and proj["project_id"] else "")
        texts = []
        for r in reversed(rows):
            c = (r["content"] or "").strip()
            if len(c) >= 30:
                texts.append(f"{'用户' if r['role'] == 'user' else '分析'}: {c[:600]}")
        return reflow_from_text(conn, "\n".join(texts)[:6000], source=f"conv-{conversation_id}",
                                project_id=pid, agent_id="conversation")
    except Exception as e:
        return {"candidates": 0, "error": str(e)[:150]}


def _fmt_node(n) -> str:
    """影响分析 comparison 节点 → 可读摘要（仅名称，保持单行短促利于规则提炼）。"""
    if isinstance(n, dict):
        return str(n.get("name") or n.get("id", ""))
    return str(n)


def reflow_from_impact(conn, sim_id: int, changes: list | None = None,
                       comparison: dict | None = None, title: str = "",
                       project_id: str = "") -> dict:
    """变更影响分析结论回流（FR-KG-12 补 G13）：模拟结果 → v2g 候选。

    拼接标题 + 变更操作清单 + comparison 关键结论（新增/解除/影响度变化/风险），
    source 记 impact-{sim_id}，供 reflow/stats 按来源分组统计。
    返回 reflow_from_text 结果 {batch_id, candidates, degraded}。
    """
    lines = []
    if title:
        lines.append(f"变更影响分析：{title}")
    for c in (changes or [])[:10]:
        op = c.get("op", "")
        target = c.get("target", "")
        new_value = c.get("new_value", "")
        lines.append(f"变更操作：{op} {target}" + (f" → {new_value}" if new_value else ""))
    cmp_ = comparison or {}
    if cmp_.get("removed"):
        lines.append("受影响解除元素：" + "、".join(_fmt_node(n) for n in cmp_["removed"][:15]))
    if cmp_.get("added"):
        lines.append("新增受影响元素：" + "、".join(_fmt_node(n) for n in cmp_["added"][:15]))
    for d in (cmp_.get("degree_changes") or [])[:10]:
        lines.append(f"影响度变化：{d.get('name', d.get('id', ''))} {d.get('before', '')}→{d.get('after', '')}")
    for r in (cmp_.get("risk") or [])[:5]:
        if isinstance(r, dict):
            lines.append(f"风险提示：{r.get('title', '')}；{r.get('desc', '')}")
        else:
            lines.append(f"风险提示：{r}")
    text = "\n".join(x for x in lines if x and str(x).strip())
    return reflow_from_text(conn, text, source=f"impact-{sim_id}",
                            project_id=project_id, agent_id="impact")


def reflow_from_review(conn, msg_id: int, context: str, fb_type: str = "") -> dict:
    """模型评审结论回流（FR-KG-12 补 G13）：评审修正意见 → v2g 候选。

    取消息原文（assistant 产出）+ 用户评审修正意见拼接，source 记 review-{msg_id}。
    返回 reflow_from_text 结果 {batch_id, candidates, degraded}。
    """
    try:
        row = conn.execute(
            "SELECT role, content, conversation_id FROM messages WHERE id=?", (msg_id,)).fetchone()
        original = (row["content"] if row else "") or ""
        pid = ""
        if row and row["conversation_id"]:
            proj = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                                (row["conversation_id"],)).fetchone()
            if proj and proj["project_id"]:
                pid = proj["project_id"]
        parts = []
        if original.strip():
            parts.append(f"模型产出：{original[:800]}")
        if context and context.strip():
            parts.append(f"评审修正意见（{fb_type or '修正'}）：{context.strip()[:800]}")
        text = "\n".join(parts)
        return reflow_from_text(conn, text, source=f"review-{msg_id}",
                                project_id=pid, agent_id="review")
    except Exception as e:
        return {"candidates": 0, "error": str(e)[:150]}
