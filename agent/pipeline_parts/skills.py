# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：技能池加载、语义匹配与技能提示词构建。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *


class SkillMixin:
    """技能池加载、语义匹配与技能提示词构建。"""

    def _global_skill_pool(self, user=None) -> list:
        """全局技能池（不含绑定技能，供跨 Agent 自动匹配）。

        P0-5 市场统一：除 skills 表技能外，追加 plugins 表 skill/bundle 型插件展开（SKILL.md 指令）。

        P1-6「安装即可消费」（2026-09-16）：两条来源都过可消费判定——
          · skills 表：有插件映射但该插件未安装/已停用/已下架 → 排除
            （无映射的旧体系原生技能不受影响，不会被新体系误伤）
          · plugins 侧：条件由「scope=public 且 published」改为「可消费」，
            使自建技能与市场技能同等可用，且"停用"立即反映到运行时
        """
        try:
            from database import get_db
            conn = get_db()
            out = []
            try:
                from plugin_system import store as _pstore
                try:
                    _keep = _pstore.consumable_filter(conn, user)
                except Exception:
                    _keep = None
                rows = conn.execute(
                    "SELECT * FROM skills WHERE status='published' AND enabled=1").fetchall()
                for r in rows:
                    if _keep is not None and not _keep("skills", r["id"]):
                        continue      # 已停用/未安装的插件化技能 → 不进入技能池
                    d = dict(r)
                    # D4：允许工具白名单 + 渐进披露资源（references/examples/scripts）一并解析
                    for k in ("triggers", "dependencies", "allowed_roles",
                              "allowed_tools", "references", "examples", "scripts"):
                        try:
                            v = json.loads(d.get(k) or "[]")
                            d[k] = v if isinstance(v, list) else []
                        except Exception:
                            d[k] = []
                    out.append(d)
                # 插件侧技能来源：已安装且启用（含系统级）的 skill/bundle 型插件
                try:
                    for pid in _pstore.plugin_ids_of_types(conn, ("skill", "bundle"), user):
                        se = _pstore.skill_entry_from_plugin(conn, pid)
                        if se:
                            out.append(se)
                except Exception:
                    pass
            finally:
                conn.close()
            return out
        except Exception:
            return []

    def _semantic_skill_match(self, user_input: str, skills: list, threshold: float = 0.15) -> list:
        """语义匹配（真 embedding 优先，bigram 降级；复用双条件防误路由）：返回命中技能名列表。"""
        if not skills or not user_input:
            return []
        from semantic import SemanticSearch
        idx = [{"name": s["name"], "text": " ".join(filter(None, [
            s.get("name", ""), s.get("description", ""),
            *[str(t) for t in (s.get("triggers") or [])]]))} for s in skills]
        scored = SemanticSearch().rank(user_input, idx, top_k=2, threshold=0, key="text")
        if not scored:
            return []
        top_score, top = scored[0]
        if top_score < threshold:
            return []
        if len(scored) >= 2 and top_score < scored[1][0] * 1.5:
            return []
        return [top["name"]]

    def _semantic_candidates(self, user_input: str, skills: list, k: int = 5) -> list:
        """语义预筛：返回 top-k 有重叠的技能列表（score>0），供 LLM 兜底只做重排序而非全池挑选。"""
        if not skills or not user_input:
            return []
        from semantic import SemanticSearch
        idx = [{"name": s["name"], "text": " ".join(filter(None, [
            s.get("name", ""), s.get("description", ""),
            *[str(t) for t in (s.get("triggers") or [])]]))} for s in skills]
        scored = SemanticSearch().rank(user_input, idx, top_k=k, threshold=0, key="text")
        return [it["name"] for _, it in scored]

    def _llm_pick_skills(self, user_input: str, candidates: list) -> list:
        """LLM 兜底：从候选技能中批量挑选最匹配的 1-3 个（单次调用；Mock/失败返回空）。"""
        if not candidates:
            return []
        try:
            from llm import llm_client
            cand_txt = "\n".join(
                f"- {s.get('name','')}: {(s.get('description') or '')[:80]}" for s in candidates[:20])
            resp = llm_client.chat([
                {"role": "system", "content": (
                    "你是技能路由器。根据用户任务，从候选技能中挑选最匹配的 1-3 个。"
                    '只输出 JSON 数组字符串，如 ["skill_a","skill_b"]；无匹配输出 []；不要其他文字。')},
                {"role": "user", "content": f"用户任务：{str(user_input)[:500]}\n候选技能：\n{cand_txt}"}],
                _intent="skill_route")
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            names = self._parse_json_block(content)
            if not isinstance(names, list):
                return []
            return [str(n) for n in names if str(n)]
        except Exception:
            return []

    def _build_skill_prompt(self, intent: str, user_input: str, user=None) -> str:
        """P1/P3 Skill 触发路由：渐进式披露（对齐 Anthropic Progressive Disclosure）。

        V4 升级（对齐企业 Agent 文章 Skill Routing 三层模型）：
        - 匹配范围：绑定技能 + 全局已发布技能池（未绑定 Agent 也可自动触发）
        - 三级匹配：triggers 关键词 → bigram 语义（双条件防误路由）→ LLM 批量兜底
        - 角色权限：skill.allowed_roles 非空且当前用户角色不在其中 → 跳过（P2）
        - 命中 → 注入元数据 + 正文摘要（全局命中标注建议绑定）；结果写入 self._last_skill_hits
        """
        bound = self.registry.get_bound_skills(intent)
        bound_names = {s.get("name") for s in bound}
        extra = [s for s in self._global_skill_pool(user) if s["name"] not in bound_names]
        candidates = list(bound) + extra
        if not candidates:
            self._last_skill_hits = []
            return ""
        # P2 角色过滤
        role = (user or {}).get("role_name") or ""
        if role:
            candidates = [s for s in candidates
                          if not (s.get("allowed_roles") or []) or role in (s.get("allowed_roles") or [])]
        # 三级匹配
        hits = set()
        low = str(user_input or "").lower()
        for s in candidates:
            trg = [str(t).lower() for t in (s.get("triggers") or [])]
            if any(t and t in low for t in trg):
                hits.add(s["name"])
        if len(hits) < 3:
            rest = [s for s in candidates if s["name"] not in hits]
            for name in self._semantic_skill_match(user_input, rest):
                hits.add(name)
        llm_rest = [s for s in candidates if s["name"] not in hits]
        if len(hits) < 2 and len(llm_rest) > 0:
            # LLM 兜底只做「语义预筛后」的重排序，避免无关技能被乱选（两段式路由）
            for name in self._llm_pick_skills(user_input,
                                              [s for s in llm_rest if s["name"] in self._semantic_candidates(user_input, llm_rest)]):
                hits.add(name)
        self._last_skill_hits = sorted(hits)
        # D4：本次命中 skill 的工具白名单合并集（有声明才限制；多 skill 命中取并集）
        # 指定技能(skill_name)路径已在 execute 注入权威白名单（_skill_forced=True）→ 不重置、不并集
        if not getattr(self, "_skill_forced", False):
            self._skill_allowed_tools = None
        parts = []
        for s in candidates:
            is_bound = any(s["name"] == b.get("name") for b in bound)
            if s["name"] not in hits:
                if is_bound:  # 未命中绑定技能只给一行描述（省 token）
                    parts.append(f"- {s.get('name','')}：{s.get('desc','') or (s.get('content') or '')[:60]}")
                continue
            at = [str(t) for t in (s.get("allowed_tools") or []) if str(t)]
            if at and not getattr(self, "_skill_forced", False):
                if self._skill_allowed_tools is None:
                    self._skill_allowed_tools = set()
                self._skill_allowed_tools |= set(at)
            meta = (s.get("frontmatter") or "").strip()[:400]
            body = (s.get("content") or "")
            src_tag = "" if is_bound else "（全局匹配·未绑定 Agent，建议绑定）"
            snippet = f"【Skill 已触发：{s.get('name','')}{src_tag}】（正文按需加载，摘要如下）\n"
            if meta:
                snippet += f"元数据：{meta}\n"
            snippet += f"正文摘要：{body[:600]}"
            # D4 渐进披露：仅披露资源清单（文件名/标题），正文按需加载省 token
            res_refs = [str(r) if isinstance(r, str) else str(r.get("title") or r.get("path") or r)
                        for r in (s.get("references") or [])]
            res_ex = [str(e) if isinstance(e, str) else str(e.get("title") or e.get("path") or e)
                      for e in (s.get("examples") or [])]
            res_scr = [str(x) for x in (s.get("scripts") or [])]
            if res_refs:
                snippet += f"\n📄 参考文档（需要时按需读取）：{'；'.join(res_refs[:5])}"
            if res_ex:
                snippet += f"\n📝 示例（需要时按需读取）：{'；'.join(res_ex[:5])}"
            if res_scr:
                snippet += f"\n⚙ 脚本（需要时执行）：{'；'.join(res_scr[:5])}"
            if at:
                snippet += f"\n🔒 工具白名单（仅可调用）：{', '.join(at)}"
            parts.append(snippet)
        return "\n绑定技能：\n" + "\n".join(parts) + "\n" if parts else ""

    _ORCH_AGENTS = "requirement_analysis,design,impact,review,report_generation,knowledge_qa"
    _ORCH_MAX_TASKS = 6
    # Task 7：执行层并发与可靠性配置（类属性默认，均允许实例覆盖）
    _ORCH_MAX_WORKERS = 3            # 每批 ready 并行执行 worker 上限（全局并发）
    # P0-7（2026-09-30）：下面两项**只是兜底**，现场优先读 config
    #   `delegation.subtask_idle_timeout_s`（无产出判超时，默认 150s）
    #   `delegation.subtask_timeout_s`（wall-clock 硬上限，默认 300s）
    # 起因：固定 120s×2 次重试会把「还在正常出 token」的子任务判 failed、产出丢弃（会话 514 实测
    # t1 到 182s 仍在吐 token，241s 被判 timeout）。改判据后此处仅作异常兜底，改阈值请改 config。
    _ORCH_SUBTASK_TIMEOUT = 120      # 子任务 wall-clock 硬上限兜底（秒；被 config 覆盖）
    _ORCH_MAX_RETRIES = 1            # 子任务失败/超时自动重试次数上限
    _ORCH_RETRY_BACKOFF = 1          # 重试退避基数（秒，首次 1s，逐次累乘）
    _ORCH_TOKEN_BUDGET = 200000      # run 级 token 预算默认值（实例可覆盖 _orch_token_budget）
