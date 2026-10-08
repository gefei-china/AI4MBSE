# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：团队编排池、多 Agent 编排触发与 Planner 流程复用。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *
from core.audit import audit, audit_user


class OrchestrMixin:
    """团队编排池、多 Agent 编排触发与 Planner 流程复用。"""

    # ── 编排触发的「可编排意图」边界 ──────────────────────────────
    # 2026-10-02（P1-12）：原 `_needs_orchestration` 用**硬编码 6 意图白名单**限定编排触发，
    # 用户新建的专家 Agent 无法参与自动编排。改为「读 agents 表里 enabled 且 role=sub 的
    # Agent 意图名」，只有两类**明确不编排**的意图才硬排除（黑名单），其余交给后续规则信号 /
    # 多意图 / LLM 复杂度判定。这样编排触发跟着 Agent 配置走，而不是跟着代码常量走。
    # 为什么这两类是黑名单：chat = 通用入口（本身就该直答）；system_mgmt = 系统数据查询
    #（单任务）；requirement_quality = 单交付物质量分析（execute 里有专门双通道，不编排）。
    _ORCH_EXCLUDE_INTENTS = ("chat", "system_mgmt", "requirement_quality")
    # 旧白名单 → 兜底常量：agents 表查询失败 / 结果为空时回退，保证行为不塌方。
    _ORCH_WHITELIST_FALLBACK = (
        "requirement_analysis", "design", "impact", "review", "report_generation", "knowledge_qa")

    def _orchestrable_from_rows(self, rows) -> set:
        """纯函数：从 agent 行列表推导可编排意图集合（空则回退旧白名单）。

        与 DB 读取解耦，便于单测/变异自证。语义：rows 里 role=sub 的 name 集合，排除黑名单；
        结果为空（查询失败 / 表空）→ 回退旧固定白名单，保证编排触发行为不塌方。
        """
        _intents = {r["name"] for r in rows} - set(self._ORCH_EXCLUDE_INTENTS)
        return _intents or set(self._ORCH_WHITELIST_FALLBACK)

    def _orchestrable_intents(self) -> set:
        """可编排意图 = enabled 且 role=sub 的 Agent 意图名，排除入口/系统/单交付物类。

        查 agents 表（role=sub 的才是可被委派的专家 Agent；role=main 是团队负责人，不作为
        编排触发意图）。查询失败或结果为空 → 回退旧固定白名单（与历史行为一致，不塌方）。
        """
        from database import get_db
        try:
            _conn = get_db()
            try:
                _rows = _conn.execute(
                    "SELECT name FROM agents WHERE status='active' AND agent_role='sub'"
                ).fetchall()
            finally:
                _conn.close()
        except Exception:
            _rows = []
        return self._orchestrable_from_rows(_rows)

    def _orch_pool(self, conn, domain: str = "") -> list:
        """Task 5：编排候选池意图名列表（动态化：discover → fallback 旧池 → 内置常量兜底）。

        优先级：
        1) self.registry.discover(conn, domain) 的 intent 列表——discover 仅返回 enabled/active Agent
           （含未配置能力元数据 capabilities 空的存量 Agent）→ 默认行为与旧固定池等价；
        2) discover 为空时回退 self.registry.fallback_pool(domain)（Task 4 旧固定池，按 domain 宽松过滤）；
        3) 仍为空回退内置 _ORCH_AGENTS 拆分列表。
        """
        try:
            discovered = self.registry.discover(conn, domain) if conn is not None else []
        except Exception:
            discovered = []
        if discovered:
            return [d["intent"] for d in discovered if d.get("intent")]
        try:
            fb = self.registry.fallback_pool(domain)
        except Exception:
            fb = []
        if fb:
            return list(fb)
        return [x.strip() for x in self._ORCH_AGENTS.split(",") if x.strip()]

    def _orch_pool_desc_lines(self, conn, domain: str = "") -> list:
        """Task 5：编排候选池 prompt 行（带各 Agent 专长描述；无元数据仅意图名）。

        行格式：`- requirement_analysis: 需求分析Agent（专长：需求拆解/追溯）`；
        有显示名但无 capabilities 时 `- intent: 显示名`；无任何元数据时仅 `- intent`。
        供 _stream_orchestrated_flow / 下游 planner 的「可用 Agent 池」提示词拼装。
        """
        try:
            _by_intent = {d.get("intent"): d
                          for d in (self.registry.discover(conn, domain) if conn is not None else [])
                          if d.get("intent")}
        except Exception:
            _by_intent = {}
        lines = []
        for intent in self._orch_pool(conn, domain):
            d = _by_intent.get(intent)
            if d:
                _name = d.get("name") or intent
                _caps = [str(c) for c in (d.get("capabilities") or []) if str(c)]
                if _caps:
                    lines.append(f"- {intent}: {_name}（专长：{'/'.join(_caps)}）")
                else:
                    lines.append(f"- {intent}: {_name}")
            else:
                lines.append(f"- {intent}")
        return lines

    def _team_roster_block(self, agent_def) -> str:
        """主 Agent 团队名册注入：agent_role='main' 时，把团队成员（名/职责/专长）写入 system prompt。

        轻量联动（主/子 Agent 团队）：子 Agent 独立运行不受影响；主 Agent 作为团队负责人
        运行时可看到团队名册，便于把任务分解委派给成员并汇总。
        """
        try:
            if getattr(agent_def, "agent_role", None) != "main":
                return ""
            _intent = getattr(agent_def, "intent_name", None) or agent_def.name
            with db_conn() as _conn:
                row = _conn.execute("SELECT id FROM agents WHERE name=?", (_intent,)).fetchone()
                if not row:
                    return ""
                from repositories.agent_repo import AgentRepo as _AR
                members = _AR(_conn).list_team_members(row["id"])
            if not members:
                return "\n【团队协作】你是团队负责人，当前团队暂无成员。可向用户说明，并建议到「Agent 管理」配置团队成员后重试。\n"
            lines = []
            for m in members:
                caps = [str(c) for c in (m.get("capabilities") or []) if str(c)]
                cap_txt = "；".join(caps[:5]) if caps else ""
                lines.append(f"- {m.get('display_name') or m.get('name')}（{m.get('name')}"
                             f"{'，专长：' + cap_txt if cap_txt else ''}）")
            return ("\n【团队协作】你是团队负责人，可将任务分解并委派给以下团队成员执行，"
                    "再由你汇总校验结果：\n" + "\n".join(lines) + "\n")
        except Exception:
            return ""

    def _team_member_intents(self, agent_def) -> list:
        """主 Agent 团队成员意图名（委派候选收敛；非主 Agent 返回空列表=不限制）。"""
        try:
            if getattr(agent_def, "agent_role", None) != "main":
                return []
            _intent = getattr(agent_def, "intent_name", None) or agent_def.name
            with db_conn() as _conn:
                row = _conn.execute("SELECT id FROM agents WHERE name=?", (_intent,)).fetchone()
                if not row:
                    return []
                from repositories.agent_repo import AgentRepo as _AR
                return _AR(_conn).team_member_intents(row["id"])
        except Exception:
            return []

    def _resolve_team(self, team: str) -> str | None:
        """校验团队（主 Agent）存在且启用，返回主 Agent 意图名；非法返回 None。

        ⚠️ 只返回 name，**不返回团队定义**——编排 prompt 拿不到 system_prompt /
        capabilities /各成员的 intent_keywords ⇒ planner 只能看到一串意图名，
        8 个「XX视图生成」近义成员无法区分（实测词法把「冷却回路建模」判成
        requirement_analysis）。C1 起改用 `_team_definition()` 取完整定义。
        保留本方法仅为向后兼容（多处调用点只校验合法性）。
        """
        if not team or not str(team).strip():
            return None
        try:
            with db_conn() as _conn:
                row = _conn.execute(
                    "SELECT name, agent_role, status FROM agents WHERE name=? OR display_name=?",
                    (str(team).strip(), str(team).strip())).fetchone()
                if not row:
                    return None
                if row["agent_role"] != "main" or row["status"] != "active":
                    return None
                return row["name"]
        except Exception:
            return None

    # ── C1：完整团队定义（主 Agent prompt + 成员能力/触发词） ──────────
    def _team_definition(self, team: str) -> dict | None:
        """返回完整团队定义（含 system_prompt 与成员 capabilities/keywords），非法返回 None。

        与 `_resolve_team` 的区别：不再只回name。编排的拆解质量依赖主 Agent 的
        system_prompt（拆解规则、八视图依赖顺序）与成员 capabilities（近义区分），
        两者在此之前全部丢失。
        """
        from agent import team_router as _tr
        if not team or not str(team).strip():
            return None
        try:
            with db_conn() as _conn:
                return _tr.load_team_definition(_conn, team)
        except Exception:
            return None

    def _team_route_decision(self, user_input: str, team_def: dict, provider_id=None,
                             detected_intent: str = "") -> dict:
        """团队模式路由决策（chat / direct / orchestrate / reject）。

        2026-10-07（C3/C4/C5）：改 team 前 `team_forced=True` 无条件编排，问「你好」也跑
        一次完整 Planner-Executor。现按团队定义做四档决策，判据与实测见
        `agent/team_router.decide` docstring。
        """
        from agent import team_router as _tr
        return _tr.decide(user_input, team_def, provider_id=provider_id,
                          detected_intent=detected_intent)

    def _team_unsupported_message(self, team_def: dict, reason: str) -> str:
        """「不支持」提示：必须带团队能力清单 + 出路（不返回死胡同）。"""
        from agent import team_router as _tr
        return _tr.unsupported_message(team_def, reason)

    def _try_orchestrate_team(self, user_input: str, team_intent: str, provider_id=None,
                              attachments=None, conversation_id: int = 0,
                              team_def: dict | None = None) -> dict | None:
        """团队模式编排：主 Agent（团队负责人）分解任务 → 分派团队成员 → 汇总。

        委派候选收敛到「团队成员 + 主 Agent 自身」最小权限池（非全量 Agent 池）；
        无成员的主 Agent 仅自身可被委派。编排异常返回 None（回落单 Agent 直行）。

        C2：`team_def` 非空时把**主 Agent system_prompt + 成员名册（带 capabilities
        与触发词）** 注入 planner 改写prompt。此前pool 只给意图名，planner 无法区分
        「结构视图生成」与「需求视图生成」，也不��道 SysML v2 八视图依赖顺序
        ⇒ 同一 query 两次拆解结果可能不同。
        """
        if not team_intent:
            return None
        try:
            from workflows import FlowExecutor
            from database import get_db
            conn = get_db()
            try:
                from repositories.agent_repo import AgentRepo as _AR
                row = conn.execute("SELECT id FROM agents WHERE name=?", (team_intent,)).fetchone()
                members = _AR(conn).team_member_intents(row["id"]) if row else []
            finally:
                conn.close()
            pool = list(dict.fromkeys(members + [team_intent])) or [team_intent]
            _kw = {}
            if team_def:
                from agent import team_router as _tr
                _blocks = []
                _sp = (team_def.get("system_prompt") or "").strip()
                if _sp:
                    _blocks.append("【主 Agent 拆解规则】\n" + _sp)
                _rb = _tr.roster_block(team_def)
                if _rb:
                    _blocks.append("【团队成员名册（含专长与触发词，agent 字段须逐字取自名册）】\n" + _rb)
                if _blocks:
                    _kw["team_prompt"] = "\n\n".join(_blocks)
            return FlowExecutor().run_planner_plan(
                goal=user_input, agents=self._ORCH_AGENTS, max_tasks=self._ORCH_MAX_TASKS,
                parallel=True, provider_id=provider_id, attachments=attachments, pool=pool,
                conversation_id=conversation_id, **_kw)
        except Exception as e:
            self._orch_error = str(e)[:150]
            return None

    def _needs_orchestration(self, user_input: str, intent: str, forced_intent=None,
                             has_attachments: bool = False, multi=None) -> bool:
        """P0-1 复杂任务判定：多步/多交付物任务 → 自动编排；简单任务维持单 Agent。

        规则信号（先…再…/然后…/分别…并…/需求+设计+报告组合）直接命中；
        否则 LLM 复杂度判定（Mock/无 key 返回 False，确定性保持）。
        带附件时优先单 Agent 直行（附件为本轮核心依据，编排子任务易丢附件）——
        仅规则信号可触发编排，LLM 判定跳过。

        2026-09-25：新增 `multi`（`IntentRouter.detect_multi` 的结果）作为**最高优先规则信号**。
        缺口：上面那串连词规则只认「和/以及/与/且/并」，而用户最常写的是**顿号清单** ——
        「提供一段需求，进行需求分析、方案设计、代码校验」在此处被判为"不需要编排"，
        于是刚识别出的 3 阶段序列**无处可用**（识别出来了却不驱动任何决策）。
        多意图识别本身就是强得多的证据，直接采信，不再重复堆连词正则。
        """
        if forced_intent:
            return False
        # P1-12：可编排意图从「硬编码 6 白名单」改为「agents 表 enabled+role=sub 动态推导」。
        # 语义等价于旧白名单（6 个内置专家 Agent 都是 sub），但消除了写死——新增的 sub 角色
        # Agent 自动进入可编排集合，编排触发跟着 Agent 配置走而非代码常量走。
        if intent not in self._orchestrable_intents():
            return False
        t = (user_input or "").strip()
        # 多意图识别（阶段连词或并列清单，≥2 个不同阶段）→ 直接编排
        if multi and len(multi.get("sequence") or []) >= 2:
            return True
        # 多 Agent 协作表达：「需求分析 + 工程建模」「建模与设计」「需求并方案」——
        # 规则信号（多步表达）→ 一律编排（无论意图/附件）
        if re.search(r"先.{0,8}(再|然后|接着)", t) \
                or re.search(r"(然后|接着|最后).{0,4}(做|生成|输出|给出|汇总)", t) \
                or re.search(r"(分别|同时).{0,6}(并|且|进行)", t) \
                or re.search(r"(需求|方案).{0,10}(设计|架构).{0,10}(报告|评审)|设计.{0,8}(报告|评审)", t):
            return True
        # 多 Agent 协作表达：「需求分析 + 工程建模」「建模与设计」「需求并方案」——
        # 同时含两个 Agent 域关键词 + 并列连词（和/并/以及/与/且）→ 触发编排
        _agent_combos = [
            ("需求分析", "建模"), ("需求分析", "设计"), ("需求分析", "架构"), ("需求分析", "方案"),
            ("需求分析", "工程建模"), ("需求分析", "方案设计"),
            ("需求", "设计"), ("需求", "架构"), ("需求", "方案"), ("需求", "建模"), ("需求", "工程建模"),
            ("分析", "设计"), ("分析", "建模"), ("分析", "方案"), ("分析", "架构"),
            ("方案设计", "需求"), ("设计", "需求"), ("建模", "需求"), ("建模", "分析"),
            ("方案设计", "建模"), ("设计", "建模"),
        ]
        if any(a in t and b in t and any(c in t for c in ("和", "以及", "与", "且", "并"))
               for a, b in _agent_combos):
            return True
        # 噪声拦截（短噪声如"hi/你好"，多 Agent 规则已在上方独立生效）
        if len(t) < 6:
            return False
        # 带附件 → 优先单 Agent 直行（附件注入 prompt 作为优先依据，最可靠）
        # 唯一例外：上述规则信号/多 Agent 已触发 → 返回 True 走编排
        if has_attachments:
            return False
        # LLM 判定仅对复合意图启用（需求分析/方案设计可能含多阶段）；
        # 单交付物意图（评审/影响/报告/知识问答）默认不编排——"生成报告"等单任务不被误判
        if intent not in ("requirement_analysis", "design"):
            return False
        try:
            from llm import llm_client
            resp = llm_client.chat([
                {"role": "system", "content": (
                    "你是任务复杂度判定器。判断用户任务是否需要多 Agent 协作"
                    "（拆分为多个子任务、由不同 Agent 顺序/并行完成，如需求分析→架构设计→冲突预检→报告生成）。"
                    "严格标准：只有当任务明确包含多个不同阶段/多个交付物时才 true；"
                    '单一交付物任务（即使篇幅长）一律 false。只输出 JSON：'
                    '{"orchestrate": true/false, "reason": "一句话理由"}，不要其他文字。')},
                {"role": "user", "content": str(t)[:500]}],
                _intent="complexity")
            raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
            d = self._parse_json_block(raw)
            return bool(d and d.get("orchestrate"))
        except Exception:
            return False

    def _try_orchestrate(self, user_input: str, intent: str, provider_id=None, attachments=None,
                         conversation_id: int = 0) -> dict | None:
        """P0-1 会话入口自动编排：复杂任务 → Planner-Executor（复用 FlowExecutor.run_planner_plan）。
        简单任务 / 带附件且非规则信号 / 编排异常 → 返回 None（回落单 Agent 直行）。
        附件随编排透传（子任务执行时注入，避免丢附件）。"""
        if not self._needs_orchestration(user_input, intent, has_attachments=bool(attachments),
                                        multi=self.router.detect_multi(user_input)):
            return None
        try:
            from workflows import FlowExecutor
            from database import get_db
            # Task 5：编排池动态化——传入 discover 动态池（无 DB 元数据时降级旧固定池，行为不变）
            _pool = self._orch_pool(get_db())
            return FlowExecutor().run_planner_plan(
                goal=user_input, agents=self._ORCH_AGENTS, max_tasks=self._ORCH_MAX_TASKS,
                parallel=True, provider_id=provider_id, attachments=attachments, pool=_pool,
                conversation_id=conversation_id)
        except Exception as e:
            self._orch_error = str(e)[:150]
            return None

    def _finish_orchestrated(self, orch: dict, user_input: str, conversation_id, intent: str,
                             agent_def, hil_level: str, kb_tags: list, attachments: list,
                             branch: str, slots: dict, team: str | None = None,
                             user=None) -> dict:
        """编排路径落库收尾（与 execute 主路径落库段保持一致，dry_run 语义省略：编排仅会话触发）。"""
        llm_content = orch.get("content") or ""
        # SP-R/视图联动：编排汇总内容含 SysML → 投影视图（需求图/BDD 等会话内预览）
        sysml_views = self._gen_sysml_views(llm_content, intent, user_input)
        llm_info = {
            "provider": (orch.get("llm") or {}).get("provider", "自动编排"),
            "model": "-",
            "used_mock": bool((orch.get("llm") or {}).get("used_mock", False)),
            "latency_ms": orch.get("latency_ms", 0),
        }
        tasks = ((orch.get("data") or {}).get("tasks")) or []
        # P0-1：编排成功后沉淀为可复用工作流（draft，供前端另存/画布编辑/发布）
        saved_flow_id = None
        plan_meta = [{"key": t.get("key"), "title": t.get("title"), "agent": t.get("agent"),
                      "status": t.get("status")} for t in tasks]
        try:
            from database import get_db as _gd
            _conn = _gd()
            try:
                # P0-7：按**本轮批次号**读回计划。此前写的是 run_id（流式=会话 id / 非流式=0），
                # 这里却按 conversation_id 读 → 非流式路径恒读空，"沉淀可复用工作流"静默失效。
                _run_id = int(orch.get("run_id") or conversation_id or 0)
                _plan_rows = _conn.execute(
                    "SELECT task_key, title, agent_id, config, deps, status FROM agent_tasks "
                    "WHERE run_id=? ORDER BY seq", (_run_id,)).fetchall()
                _done = [dict(r) for r in _plan_rows if r["status"] == "done"]
                if _done:
                    _goal = user_input
                    _plan_dict = []
                    for r in _plan_rows:
                        try:
                            _cfg = json.loads(r["config"] or "{}")
                        except Exception:
                            _cfg = {}
                        _plan_dict.append({"key": r["task_key"], "title": r["title"],
                                           "agent": r["agent_id"],
                                           "deps": json.loads(r["deps"] or "[]"),
                                           "context": _cfg.get("context", ""),
                                           "expected_output": _cfg.get("expected_output", "")})
                    saved_flow_id = self._save_planner_flow(_goal, _plan_dict, intent)
            finally:
                _conn.close()
        except Exception:
            pass
        # P0-3（2026-09-19）：非流式编排路径补齐 orchestrated_status —— 与流式路径口径对齐。
        # 此前该字段只有流式路径（stream.py::_stream_orchestrated）写，非流式路径卡片缺键，
        # 同一功能两条路径行为不一致（前端徽章无从渲染）。状态由子任务执行状态聚合，
        # 再并入质量门禁（reflection 未通过 → 降级 partial；只降不升）。
        _task_status = [str(t.get("status") or "") for t in plan_meta]
        if _task_status and all(s == "done" for s in _task_status):
            _agg_status = "full"
        elif any(s == "failed" for s in _task_status) and not any(s == "done" for s in _task_status):
            _agg_status = "failed"
        else:
            _agg_status = "partial"
        _orch_reflection = (orch.get("data") or {}).get("reflection")
        try:
            from services import subtask_protocol as _sp
            _agg_status, _gate_gaps = _sp.apply_quality_gate(_agg_status, _orch_reflection)
        except Exception:
            _gate_gaps = []
        card_data = json.dumps({
            "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            "orchestrated": True, "degraded": bool(orch.get("degraded")),
            "orchestrated_status": _agg_status,   # P0-3：三态与流式路径对齐
            "quality_gate_gaps": _gate_gaps,      # P0-3：质量门禁缺口说明（前端可选用）
            "team": team or None,   # 团队模式：主 Agent 团队负责人
            "plan": plan_meta,
            "reflection": (orch.get("data") or {}).get("reflection"),   # P0-1（T4）：反思闭环评审轨迹
            "sysml_views": sysml_views,
            "source": "none", "confidence": 0, "graph_count": 0, "vector_count": 0,
            "used_mock": llm_info["used_mock"], "provider": llm_info["provider"],
            "saved_flow_id": saved_flow_id,   # P0-1：编排沉淀的工作流 id
            "saved_flow_name": f"自动编排·{(user_input or '')[:18]}" if saved_flow_id else None,
            "exec": {"reasoning": f"自动编排：{len(plan_meta)} 个子任务由多 Agent 协作执行",
                     "agent": agent_def.name, "tools": [],
                     "subtasks": plan_meta},  # P0-2：子任务轨迹持久化
        }, ensure_ascii=False)
        # P0-3：编排产出记忆沉淀（LLM 提炼 or 规则降级，每会话限 2 次）
        self._deposit_session_memory(user_input, llm_content, intent)
        # P0-5：技能使用反馈采集（非流式编排路径）
        self._record_skill_feedback(run_id=int(conversation_id or 0), intent=intent, output_content=llm_content)
        msg_id = 0
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, msg_type, card_data, attachments) VALUES (?,?,?,?,NULL,?)",
                (conversation_id, "user", user_input, "text", json.dumps(attachments, ensure_ascii=False))
            )
            cursor = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                (conversation_id, "assistant", llm_content, "text", card_data)
            )
            msg_id = cursor.lastrowid
            _archive_artifacts(conn, conversation_id, msg_id, card_data, llm_content, intent)
            conn.execute(
                "UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, intent=? WHERE id=?",
                (intent, conversation_id)
            )
            # Audit log
# P0-b（2026-10-03 审计差距评估）：审计写入统一走 core.audit.audit() ——
#   此前此处直插 audit_logs 只有 5 列，绕过溯源上下文（IP/UA/request_id）与哈希链，
#   且归属写死「王工」（不可信）。改由统一入口写入，与其余 200+ 条审计同一口径。
            audit(audit_user(user), "llm_chat",
                  f"会话#{conversation_id} · 自动编排（{len(tasks)}子任务）· 意图:{intent}", conn=conn)
            # 话题打标：落库后立即打标（含刚插入的当前轮消息）
            try:
                self._tag_topics(conn, conversation_id)
            except Exception:
                pass
        return {
            "message_id": msg_id, "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            "content": llm_content, "card": json.loads(card_data), "orchestrated": True,
            "plan": tasks, "degraded": bool(orch.get("degraded")),
            "sysml_views": sysml_views,
            "llm": llm_info,
            "retrieval": {"source": "none", "route": "none", "confidence": 0,
                          "graph_count": 0, "vector_count": 0, "attachment_used": False},
            "attachments_info": {"parsed": 0, "skipped": [], "consumed": 0},
            "matched_flows": [],
        }

    # ── P0-1：自动编排结果沉淀为可复用工作流 ──
    def _save_planner_flow(self, goal: str, plan: list, intent: str) -> int | None:
        """把自动编排生成的 plan 归一化为 FlowExecutor 兼容节点/边，写入 agent_flows（draft）。

        - 仅成功编排（done_items≥1，degraded=False 或 plan 非空）调用；
        - 节点 = 子任务（type=agent, config.agent=意图名, query 模板化引用 payload.input + context）；
        - 边 = deps 关系；返回新流程 id；异常/无有效节点返回 None（不阻断主流程）。
        """
        try:
            nodes, edges = [], []
            for t in (plan or []):
                if not isinstance(t, dict) or not t.get("key"):
                    continue
                key = str(t["key"])
                agent_id = t.get("agent") or ""
                title = str(t.get("title") or key)
                ctx = str(t.get("context") or "").strip()
                exp = str(t.get("expected_output") or "").strip()
                query = f"{{{{payload.input}}}}\n子任务：{title}"
                if ctx:
                    query += f"\n任务上下文（只读）：{ctx[:800]}"
                if exp:
                    query += f"\n期望输出：{exp[:500]}"
                nodes.append({"id": key, "type": "agent", "label": title,
                              "config": {"agent": agent_id, "query": query}})
                for dep in (t.get("deps") or []):
                    dep = str(dep)
                    if dep and any(n["id"] == dep for n in nodes):
                        edges.append({"source": dep, "target": key})
            if not nodes:
                return None
            from repositories.studio_repo import StudioRepo
            from database import get_db
            conn = get_db()
            try:
                name = f"自动编排·{(goal or '')[:18]}"
                desc = f"由 AI 建模自动编排沉淀（意图：{intent}，{len(nodes)} 子任务），可在画布中编辑后发布复用"
                fid = StudioRepo(conn).create_agent_flow(
                    name, desc, json.dumps(nodes, ensure_ascii=False),
                    json.dumps(edges, ensure_ascii=False), version="v1", status="draft", source="planner_auto")
                conn.commit()  # BaseRepo.execute 不自动提交，需显式 commit 持久化
                return fid
            finally:
                conn.close()
        except Exception:
            return None

    def _try_reuse_planner_flow(self, user_input: str, intent: str) -> dict | None:
        """复用已发布（published）的 planner_auto 沉淀流程：词法+语义匹配 → FlowExecutor 执行。

        命中返回 {"content","plan","degraded","flow_id","flow_name"}；未命中/异常返回 None
        （调用方回落原 LLM 规划路径）。输入经 {{payload.input}} 模板注入子任务。
        """
        try:
            from database import get_db
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id, name, description, nodes, edges FROM agent_flows "
                    "WHERE source='planner_auto' AND status='published' ORDER BY id DESC LIMIT 50").fetchall()
            finally:
                conn.close()
            if not rows:
                return None
            # 词法分级加权 + 语义精排（对齐 _match_flows：词法主序，语义微调）
            q_tokens = self._flow_tokens(user_input)
            if not q_tokens:
                return None
            lex_scored = []
            for r in rows:
                name = r["name"] or ""
                desc = r["description"] or ""
                qn = q_tokens & self._flow_tokens(name)
                qd = q_tokens & self._flow_tokens(desc)
                score = len(qn) * 3 + len(qd) * 2
                if name and name in user_input:
                    score += 5
                if score >= 2:
                    lex_scored.append((score, r))
            if not lex_scored:
                return None
            lex_scored.sort(key=lambda x: -x[0])
            # 语义精排：词法分同档时用语义微调（仅精排 top5）
            best = None
            try:
                from semantic import rank_items
                sem_items = [{"id": r["id"], "text": f"{r['name'] or ''} {r['description'] or ''}"}
                             for _, r in lex_scored[:5]]
                scored = rank_items(user_input, sem_items, top_k=3, threshold=0.0, key="text", conn=None)
                sem_by_id = {it["id"]: s for s, it in scored}
                cands = []
                for s, r in lex_scored[:5]:
                    cands.append((s + sem_by_id.get(r["id"], 0.0) * 0.5, r))
                cands.sort(key=lambda x: -x[0])
                best = cands[0][1]
            except Exception:
                best = lex_scored[0][1]
            if best is None:
                return None
            try:
                nodes = json.loads(best["nodes"] or "[]")
                edges = json.loads(best["edges"] or "[]")
            except Exception:
                return None
            if not nodes:
                return None
            # 复用 FlowExecutor 执行（agent 节点 → AgentPipeline，payload.input 注入查询）
            from workflows import FlowExecutor
            import time as _t
            t0 = _t.time()
            result = FlowExecutor().run(nodes, edges, payload={"input": user_input},
                                        persist=False, flow_id=best["id"], flow_name=best["name"])
            results = result.get("results") or {}
            done_items = []
            for n in nodes:
                nid = n.get("id")
                out = results.get(nid) or {}
                if out.get("status") == "done":
                    done_items.append({"task_key": nid, "title": n.get("label") or nid,
                                       "agent_id": (n.get("config") or {}).get("agent", ""),
                                       "result": out.get("content") or "", "status": "done"})
            if not done_items:
                return None
            # LLM 汇总（≥2 子任务时；否则拼接）
            if len(done_items) >= 2:
                content = FlowExecutor()._summarize_plan(done_items, nodes, None)
            else:
                content = "\n\n".join(f"## {it['title']}\n{(it['result'] or '').strip()}" for it in done_items)
            plan_meta = [{"key": it["task_key"], "title": it["title"],
                          "agent": it["agent_id"], "status": "done"} for it in done_items]
            return {"content": content, "plan": plan_meta, "degraded": False,
                    "flow_id": best["id"], "flow_name": best["name"],
                    "latency_ms": int((_t.time() - t0) * 1000),
                    "errors": result.get("errors") or []}
        except Exception:
            return None

    # ── P0-3：Agent 长期记忆接入主管线 ──
