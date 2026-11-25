"""M6 自我进化：Skill 自动沉淀（任务产出 → 可复用技能草稿）。

对齐 Hermes "技能自动生成" 机制的最小实现：
- LLM 评估任务产出是否可沉淀为可复用 Skill（name/description/triggers/content 结构化）
- 写入 skills 表 status='draft'、source='ai_deposit'（人工审核后发布，不污染已发布技能）
- Mock/无 key 环境：LLM 无法输出 JSON → 规则降级「不沉淀」。
  Skill 草稿需人工审核，故不像记忆那样规则自动写入（确定性 + 安全）。
"""
import json
import re

# 方法论信号词：命中才触发评估（控制 LLM 调用成本）
SIGNAL_WORDS = ("方法", "步骤", "流程", "模板", "技巧", "规范", "最佳实践",
                "procedure", "method", "step", "template", "checklist")
MIN_LEN = 150


class SkillDepositor:
    """任务产出 → Skill 草稿（status='draft'，人工发布）。"""

    @staticmethod
    def has_signal(content: str) -> bool:
        c = (content or "").lower()
        return len(c) >= MIN_LEN and any(w in c for w in SIGNAL_WORDS)

    @staticmethod
    def maybe_deposit(conn, agent_id: str, task_query: str, task_content: str) -> dict | None:
        """评估并沉淀 Skill 草稿。返回 {name, status:'draft'} 或 None。"""
        content = (task_content or "").strip()
        if not conn or not SkillDepositor.has_signal(content):
            return None
        try:
            from llm import llm_client
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    "你是技能提炼师。判断以下任务产出是否值得沉淀为可复用的 MBSE 技能"
                    "（有清晰的操作步骤/检查清单/方法流程才算）。"
                    "若值得，输出 JSON：{\"worth\": true, \"name\": \"英文技能名\", "
                    "\"description\": \"一句话描述\", \"triggers\": [\"触发词\"], "
                    "\"content\": \"技能指令正文（步骤化，200字内）\"}；"
                    "否则输出 {\"worth\": false}。只输出 JSON。")},
                 {"role": "user", "content": f"任务：{str(task_query)[:200]}\n产出：{content[:1500]}"}],
                _intent="skill_deposit",
            )
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            text = msg.get("content") or ""
            m = re.search(r"\{\s*\"worth\"\s*:\s*(true|false)", text)
            if not m or m.group(1) != "true":
                return None
            nm = re.search(r"\"name\"\s*:\s*\"([^\"]+)\"", text)
            dm = re.search(r"\"description\"\s*:\s*\"([^\"]+)\"", text)
            tm = re.search(r"\"triggers\"\s*:\s*(\[[^\]]*\])", text)
            cm = re.search(r"\"content\"\s*:\s*\"([^\"]+)\"", text)
            name = nm.group(1).strip() if nm else ""
            if not name or len(name) > 60:
                return None
            name = re.sub(r"[^\w\-.]", "_", name)
            desc = dm.group(1)[:200] if dm else "AI 自动沉淀技能"
            triggers = []
            try:
                triggers = json.loads(tm.group(1)) if tm else []
            except Exception:
                triggers = []
            body = cm.group(1) if cm else content[:400]
            # P0-6：技能沉淀写入前威胁扫描（复用 MemoryScanner，命中不落库）
            from core.security_scan import MemoryScanner
            if MemoryScanner.enabled() and not MemoryScanner.scan(body + "\n" + desc)["safe"]:
                return None
            cur = conn.execute(
                "INSERT OR IGNORE INTO skills (name, skill_type, description, content, triggers, "
                "version, status, source) VALUES (?,?,?,?,?,?,?,?)",
                (name, "prompt", desc, body[:2000], json.dumps(triggers[:10], ensure_ascii=False),
                 "v1", "draft", "ai_deposit"))
            conn.commit()
            if cur.rowcount:
                return {"name": name, "status": "draft", "source": "ai_deposit"}
        except Exception:
            pass
        return None
