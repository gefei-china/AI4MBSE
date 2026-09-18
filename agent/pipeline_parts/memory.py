# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：记忆/模型上下文/用户上下文/报告素材构建。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分，勿手工编辑方法体。"""
from .common import *


class MemoryMixin:
    """记忆/模型上下文/用户上下文/报告素材构建。"""

    def _build_memory_hint(self, user_input: str, intent: str, user=None) -> str:
        """检索长期记忆（agent_memory，语义相关+时间衰减）→ 组装注入块。

        仅返回命中内容；异常/无命中返回空串（不阻断主流程）。记忆只作参考对齐，
        Prompt 硬约束「不得虚构扩展」，防止把记忆当事实检索结果引用。
        """
        try:
            from memory_service import MemoryService
            from database import get_db
            conn = get_db()
            try:
                agent_id = intent or "chat"
                query = f"{user_input} {intent}"
                rows = MemoryService.search(conn, agent_id, query, top_k=4)
                if not rows:
                    return ""
                lines = [f"- [{r.get('mem_type', 'fact')} | 相关度 {r.get('score', 0):.2f}] {str(r.get('content') or '')[:160]}"
                         for r in rows]
                return "【长期记忆（跨会话经验，仅供对齐，不得虚构扩展或作为事实来源引用）】\n" + "\n".join(lines) + "\n"
            finally:
                conn.close()
        except Exception:
            return ""

    # ── P0：建模上下文注入（工作记忆：当前模型状态，MBSE 特有）──
    def _build_model_context(self, branch="", conversation_id=0):
        """当前建模状态工作记忆（只读参考）：活跃实体/视图/最近变更/会话产物。

        数据全部来自现有表（entities/graph_views/impact_analyses/artifacts），零新增表；
        异常/无数据返回空串不阻断。预算 ≤ model_context_chars（默认 800）。
        """
        from core import config as _cfg
        from database import get_db
        max_chars = int(_cfg.get("context", "model_context_chars", 800))
        try:
            conn = get_db()
            lines = []
            try:
                # 活跃实体（当前分支，非 raw/废弃）
                ents = conn.execute(
                    "SELECT name FROM entities WHERE branch=? AND status NOT IN ('raw_chunk','deprecated') "
                    "ORDER BY created_at DESC, id DESC LIMIT 8", (branch,)).fetchall()
                ent_total = conn.execute(
                    "SELECT COUNT(*) c FROM entities WHERE branch=? AND status NOT IN ('raw_chunk','deprecated')",
                    (branch,)).fetchone()["c"] or 0
                if ents:
                    names = "、".join(str(r["name"])[:14] for r in ents)
                    lines.append(f"活跃实体：{names}{' 等' if ent_total > len(ents) else ''}（共 {ent_total} 个）")
                # 视图
                views = conn.execute(
                    "SELECT name FROM graph_views WHERE branch=? ORDER BY id DESC LIMIT 5", (branch,)).fetchall()
                if views:
                    lines.append(f"视图：{'、'.join(str(r['name'])[:12] for r in views)}（{len(views)} 个）")
                # 最近变更影响分析（快照可追溯）
                impacts = conn.execute(
                    "SELECT change_source, title, created_at FROM impact_analyses ORDER BY id DESC LIMIT 3").fetchall()
                if impacts:
                    _il = []
                    for r in impacts:
                        src = str(r["change_source"] or r["title"] or "")[:16]
                        if src:
                            _il.append(f"{src}（{str(r['created_at'] or '')[:16]}）")
                    if _il:
                        lines.append("最近变更：\n" + "\n".join("  · " + i for i in _il))
                # 会话产物
                if conversation_id:
                    arts = conn.execute(
                        "SELECT kind, title FROM artifacts WHERE conversation_id=? ORDER BY id DESC LIMIT 3",
                        (conversation_id,)).fetchall()
                    if arts:
                        lines.append("当前会话产物：" + "、".join(
                            f"{r['kind']}《{str(r['title'] or '')[:12]}》" for r in arts))
            finally:
                conn.close()
            if not lines:
                return ""
            block = "【当前建模上下文（工作记忆，只读参考）】\n" + f"分支：{branch or '-'}\n" + "\n".join(lines) + "\n"
            return block[:max_chars]
        except Exception:
            return ""

    def _build_project_memory(self, project_id: str = "") -> str:
        """项目级持久记忆（Project Constitution）：规范/基线/决策/经验注入 system prompt 防漂移。

        对齐 Codex durable project memory / Claude Code CLAUDE.md——每会话注入项目宪法，
        约束模型遵守既定规范与设计基线。默认注入当前默认项目（会话未关联项目时）。
        预算 project_memory_chars（默认 600）截尾保头（规范/基线优先）；无数据/异常返回空串不阻断。
        """
        try:
            from database import get_db
            conn = get_db()
            try:
                # 动态开关/预算（settings 表，管理界面可调；缺省 开启 / 600 字符）
                _cfg_rows = {r["key"]: r["value"] for r in conn.execute(
                    "SELECT key, value FROM settings WHERE key IN ('memory.project_memory_enabled','memory.project_memory_chars')"
                ).fetchall()}
                if str(_cfg_rows.get("memory.project_memory_enabled", "1")).lower() not in ("1", "true", "yes", "on"):
                    return ""
                max_chars = int(_cfg_rows.get("memory.project_memory_chars", "600") or 600)
                pid = project_id or ""
                if not pid:
                    row = conn.execute(
                        "SELECT value FROM settings WHERE key='default_project_id'").fetchone()
                    pid = row["value"] if row else "project-satnet-broadband"
                if not pid:
                    return ""
                rows = conn.execute(
                    "SELECT category, title, content FROM project_memories "
                    "WHERE project_id=? AND enabled=1 ORDER BY id", (pid,)).fetchall()
            finally:
                conn.close()
            if not rows:
                return ""
            parts = ["【项目规范基线（Project Constitution，AI 必须遵守，不得违背已定设计基线）】"]
            for r in rows:
                content = (r["content"] or "").strip()
                if not content:
                    continue
                parts.append(f"· [{r['category']}] {r['title']}：{content}")
            block = "\n".join(parts) + "\n"
            return block[:max_chars]
        except Exception:
            return ""

    def _deposit_session_memory(self, user_input: str, content: str, intent: str) -> None:
        """会话产出记忆沉淀：LLM 提炼 or 规则降级；每会话限 2 次防噪音。

        仅主会话（非 dry_run）调用；dry_run 子任务路径由 nodes._exec_agent 自行沉淀。
        """
        try:
            if not content or not content.strip():
                return
            if getattr(self, "_mem_deposit_count", 0) >= 2:
                return
            from memory_service import MemoryService
            from database import get_db
            conn = get_db()
            try:
                mem_id = MemoryService.maybe_deposit(conn, intent or "chat", content, user_input)
                if mem_id:
                    self._mem_deposit_count = getattr(self, "_mem_deposit_count", 0) + 1
            finally:
                conn.close()
        except Exception:
            pass

    def _record_skill_feedback(self, run_id=0, intent="", output_content="", error=None) -> None:
        """P0-5 技能使用反馈采集：本次命中技能按输出成功/失败更新 use_stats，失败滚动备注。

        对齐 Hermes「技能在使用中自我改进」：反馈数据供 SkillImprover 周期修订触发。
        规则判定（不依赖 LLM），异常静默。
        """
        hits = self._last_skill_hits or []
        if not hits:
            return
        ok = bool((output_content or "").strip()) and not error
        note = None
        if not ok:
            note = (str(error or "")[:150] if error
                    else ("无输出" if not (output_content or "").strip() else "执行失败"))
        try:
            from database import db_conn
            with db_conn() as conn:
                # 可观测：技能调用日志（call_kind='skill'，与工具日志统一落库，失败不阻断）
                _agent = getattr(self, "_tool_agent_ctx", None) or {}
                _summary = str(output_content or "")[:2000] if ok else (note or "执行失败")
                for name in hits[:10]:
                    try:
                        conn.execute(
                            "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, call_kind, "
                            "arguments, result, ok, latency_ms, conversation_id) "
                            "VALUES (?,?,?,?,?,?,?,?,0,?)",
                            (intent or "", _agent.get("agent", ""), name, "skill", "skill",
                             "{}", _summary, 1 if ok else 0, int(run_id or 0)))
                    except Exception:
                        pass
                for name in hits[:10]:
                    row = conn.execute(
                        "SELECT use_stats, feedback_notes FROM skills WHERE name=? AND status='published'",
                        (name,)).fetchone()
                    if not row:
                        continue
                    try:
                        st = json.loads(row["use_stats"] or "{}")
                    except Exception:
                        st = {}
                    uses = int(st.get("uses", 0)) + 1
                    succ = int(st.get("success", 0)) + (1 if ok else 0)
                    st.update({"uses": uses, "success": succ, "fail": uses - succ,
                               "last_used": time.strftime("%Y-%m-%d %H:%M:%S")})
                    if ok:
                        conn.execute("UPDATE skills SET use_stats=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                                     (json.dumps(st, ensure_ascii=False), name))
                    else:
                        try:
                            notes = json.loads(row["feedback_notes"] or "[]")
                        except Exception:
                            notes = []
                        notes.append({"ts": st["last_used"], "run_id": int(run_id or 0),
                                      "intent": intent or "", "note": note})
                        conn.execute(
                            "UPDATE skills SET use_stats=?, feedback_notes=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                            (json.dumps(st, ensure_ascii=False), json.dumps(notes[-5:], ensure_ascii=False), name))
        except Exception:
            pass

    def _build_report_material(self, report_type: str, retrieval: dict) -> str:
        """SP-R：类型化报告素材——变更影响（BFS 影响图）/ 预评审（校验清单）结构化数据块。

        返回 markdown 文本，追加到报告上下文；异常/无数据返回空串（不阻断生成）。
        """
        try:
            if report_type == "impact":
                card = self._card_impact(retrieval)
                src = card.get("change_source")
                src_txt = src.get("name") if isinstance(src, dict) else "-"
                lines = [f"变更源：{src_txt}",
                         f"直接影响 {card.get('direct_count', 0)} 条 / 间接 {card.get('indirect_count', 0)} 条"
                         f"（深度 {card.get('depth', 1)} 层 · {card.get('direction', 'both')}）："]
                # 影响程度分级
                _lv = card.get("impact_levels") or {}
                lines.append(f"影响程度：高 {_lv.get('high', 0)} / 中 {_lv.get('mid', 0)} / 低 {_lv.get('low', 0)}")
                # 深度统计（影响深度维度）
                _ds = card.get("depth_stats") or {}
                if _ds:
                    _dparts = [f"第{k}层(直接{d['direct']}/间接{d['indirect']})"
                               for k, d in sorted(_ds.items(), key=lambda kv: int(kv[0]))]
                    lines.append("分深度传播：" + "，".join(_dparts))
                # 广度统计（影响广度维度：类型分布 + 跨领域）
                _ts = card.get("type_stats") or {}
                if _ts:
                    lines.append("受影响元素类型：" + "，".join(f"{t}×{c}" for t, c in _ts.items()))
                _doms = card.get("domain_stats") or {}
                if _doms:
                    lines.append("跨领域影响：" + "，".join(f"{d}×{o['count']}" for d, o in _doms.items()))
                _ra = card.get("risk_analysis") or {}
                for r in (_ra.get("risks") or [])[:5]:
                    lines.append(f"- [{r.get('level', '')}] {r.get('title', '')}：{r.get('desc', '')} → {r.get('advice', '')}")
                for n in (card.get("impact_nodes") or [])[:40]:
                    lines.append(f"- [{n.get('impact', '')}] {n.get('name', '')}（{n.get('type', '')}）深度 {n.get('depth', 0)}"
                                 f" 影响度 {n.get('score', 0)}（{n.get('level', '')}）")
                if card.get("warning"):
                    lines.append(f"⚠ 提示：{card['warning']}")
                return "【变更影响分析素材】\n" + "\n".join(lines)
            if report_type == "review":
                card = self._card_review(retrieval)
                lines = [f"评审评分 {card.get('score', 0)}/100 · 结论：{card.get('conclusion', '-')}",
                         f"元素 {card.get('total_elements', 0)} · 已评审 {card.get('reviewed', 0)} · 候选 {card.get('candidates', 0)} · 冲突 {card.get('syntax_errors', 0)}"]
                for it in (card.get("issues") or [])[:30]:
                    lines.append(f"- [{it.get('level', '')}] {it.get('type', '')}：{it.get('desc', '')} → 修复：{it.get('fix', '')}")
                return "【预评审素材】\n" + "\n".join(lines)
        except Exception:
            return ""
        return ""

    def _build_user_context(self, user) -> str:
        """P2 用户上下文注入（文章：用户/业务/企业知识三类上下文）：
        身份/角色进 prompt，让同一技能因人而异；无身份时返回空串。"""
        if not user:
            return ""
        parts = [f"当前用户：{user.get('display_name') or user.get('username') or ''}"]
        if user.get("role_name"):
            parts.append(f"角色：{user['role_name']}")
        if user.get("department"):
            parts.append(f"部门：{user['department']}")
        if user.get("workspace"):
            parts.append(f"工作空间：{user['workspace']}")
        return "【用户上下文】\n" + "\n".join(parts) + "\n"
