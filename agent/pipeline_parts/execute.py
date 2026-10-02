# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：主执行入口 execute（非流式全流程）。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。

⚠️⚠️ 路径活跃度（2026-10-02 P1-26 审计）——**本文件是非流式「冷路径」**：
  · 唯一入口 `routers/conversations.py:193` 的 `POST /api/conversations/{id}/chat`；
  · **前端不调用它**：`static/js` 全仓只有一处 chat 请求，指向 `/chat/stream`；
  · 生产真实请求走 `stream.py::execute_stream`（`stream.py:1020`，主路径）。
  ⇒ **面向用户行为/质量的改动（prompt / 检索 / 落库 / 记忆等）必须在 `stream.py` 验证**；
    只改本文件对真实用户行为**无影响**。P1-24/P1-25 的 system_prompt 顺序优化正是踩了这个坑
    （改的就是本文件，流式主路径完全未生效 —— 已由 P1-26 收敛为单一真源根治，见 docs §24/§25）。
  ⇒ 已知与主路径的功能差异（**若将来启用非流式 API，需先补齐**，逐项核实于 P1-26 审计）：
    `_deposit_session_memory`（会话记忆沉淀）、`_ensure_sysml_blocks`（SysML 代码块补全）、
    `_record_skill_feedback`（技能反馈）、`_save_planner_flow`（编排留痕）、
    `_is_continuation_input`（续作输入判定）、`_archive_impact_analysis`（影响分析产物落库）；
    编排入口亦为两套实现（本文件 `_try_orchestrate`/`_finish_orchestrated`
    vs 主路径 `_stream_orchestrated_flow`）。
"""
from .common import *


class ExecuteMixin:
    """主执行入口 execute（非流式全流程）。"""

    def execute(self, user_input, conversation_id, branch="dev", provider_id=None, attachments=None, dry_run=False, forced_intent=None, skill_name=None, user=None, tools_whitelist=None, team=None, scope_id=None, scope_ids=None, scope=None):
        """Execute the full agent pipeline.

        attachments: V2.3 会话窗口富输入——[{url, filename, size, is_image}]，落库 user 消息 attachments JSON。
        dry_run: 优化3——验证运行不落库（不写 messages/audit），用于 Agent 运行可观测。
        forced_intent: 多智能体编排——显式指定执行某 Agent（跳过意图识别），供 FlowExecutor agent 节点调用。
        user: P2 当前登录用户（current_user 依赖，含 display_name/role_name/department），用于用户上下文与 Skill 角色过滤。
        tools_whitelist: P0-3 委派最小权限——本次执行仅授予的工具名列表（Worker ⊆ Supervisor）；
        非空时 function calling 只注入白名单工具，且 _exec_tool_call 拒绝白名单外调用。
        team: 团队模式（AI 会话页「工作流」下拉）——主 Agent 团队负责人统一调度：
        意图识别/任务拆分/计划制定/任务分派（委派候选收敛到团队成员）/内容整合输出。
        """
        attachments = attachments or []
        # 记忆作用域上下文（对齐 mem0）：本会话的 conversation_id + 当前用户，供记忆读写取作用域
        self._mem_ctx = {"conversation_id": conversation_id, "user": user}
        self._mem_project_id_cache = None   # 每次执行清缓存：缓存只在本请求内有效，防跨会话串味
        self._tool_whitelist = tools_whitelist or None
        self._load_db_agents(user)  # P0 平台化：DB 驱动 Agent 注册表（P1-8：按用户隔离）
        self._last_skill_hits = []
        # 团队模式：校验主 Agent（团队负责人）→ 定向到主 Agent 并强制编排
        team_forced = False
        team_intent = None
        if team:
            team_intent = self._resolve_team(team)
            if not team_intent:
                return {"error": f"智能体团队不存在或未启用：{team}，请到「Agent 管理」创建主 Agent 并启用后重试",
                        "content": "", "intent": "chat", "agent": team, "team": team}
            forced_intent = forced_intent or team_intent
            team_forced = True
        # Step 1: Intent recognition（P0-3: 6 类意图路由；forced_intent 时跳过识别直接定向）
        # P0-1：Glossary 归一化——带 conn 的 detect 会优先做术语归一化（v2→SysML_V2）
        # P0-2：轻量 DST——读取会话级当前意图，无信号时继承（追问/续写不误判 chat）
        dst = self._load_conversation_dst(conversation_id)
        # P0-5（2026-09-30）：历史联合召回（与 stream.py 同一口径，详见 _sanitize_history）
        _hist = self._load_history_for_intent(conversation_id)
        try:
            with db_conn() as _conn:
                _detected = self.router.detect(user_input, conn=_conn,
                                               prev_intent=dst["intent"] or None,
                                               history=_hist)
        except Exception:
            _detected = self.router.detect(user_input, prev_intent=dst["intent"] or None,
                                           history=_hist)
        intent = forced_intent or _detected
        # P0-4（2026-09-19）：显式定向覆盖语义识别结果时不静默（非流式路径无 SSE，故写日志留痕）
        if forced_intent and _detected and forced_intent != _detected:
            try:
                print(f"[intent] 显式定向 {forced_intent} 覆盖语义识别 {_detected}"
                      f"（conversation={conversation_id}）", flush=True)
            except Exception:
                pass
        agent_def = self.registry.get(intent)
        hil_level = agent_def.hil_level
        self._hil_level = hil_level  # M5：HIL 分级——L2 时写工具进入人工确认队列
        # 优化2：Agent 指定 LLM provider 优先（显式 provider_id > Agent 绑定 > 全局默认）
        effective_provider = provider_id or self.registry.get_provider_id(intent)
        # P1 意图结构化拆解（建模类意图；Mock 环境自动降级 {}）
        # P0-2：槽位跨轮合并——上轮槽位为底，本轮新值覆盖（entities/constraints 并集）
        # P1-4（2026-10-01）：带上 conversation_id/user_input —— 换话题时丢弃历史槽位
        slots = self._merge_slots(dst["slots"], self.task_decompose(user_input, intent),
                                  conversation_id=conversation_id, user_input=user_input)
        user_ctx = self._build_user_context(user)

        # P0-3: #知识库 标签解析（会话主入口知识库消费范围；@ 已改为智能体选择，不进文本）
        kb_tags = IntentRouter.extract_kb_tags(user_input)
        # 控制标签（#工程数据/#知识库）只作触发信号，不并入检索词
        _kb_query_tags = [t for t in kb_tags if t not in ("工程数据", "知识库")]
        kb_hint = f" + {' + '.join('#' + t for t in _kb_query_tags)}" if _kb_query_tags else ""
        req_scope, scope_att, effective_kb_scope = self._resolve_req_scope(scope_ids, scope_id, scope, agent_def)
        # P0-1 会话入口自动编排：复杂任务 → Planner-Executor（简单任务回落单 Agent）
        # 附件透传判定：带附件时仅规则信号可触发编排（附件为核心依据，优先单 Agent 直行）
        _orch = None
        # 内容级澄清：信息不清晰 → 落挂起 + 返回 clarify_asked（前端渲染选择题，回答后续答）
        if not dry_run and conversation_id:
            try:
                _clarify_qs = self._clarify_detect(
                    user_input, intent, effective_provider,
                    forced_intent=forced_intent, skill_name=skill_name)
            except Exception:
                _clarify_qs = None
            if _clarify_qs:
                self._persist_clarify(conversation_id, user_input, intent, branch, attachments,
                                      forced_intent, skill_name, team, _clarify_qs)
                return {"ok": True, "clarify_asked": True, "intent": intent,
                        "content": "需要您确认建模信息（详见澄清卡片，回答后将继续）",
                        "msg_type": "clarify", "questions": _clarify_qs}
        if team_forced and not dry_run:
            # 团队模式：主 Agent（团队负责人）强制编排——意图识别/拆解/计划/分派/汇总
            _orch = self._try_orchestrate_team(user_input, team_intent, effective_provider,
                                               attachments=attachments, conversation_id=conversation_id)
            if _orch is not None:
                self._save_conversation_dst(conversation_id, intent, slots)
                return self._finish_orchestrated(_orch, user_input, conversation_id, intent,
                                                 agent_def, hil_level, kb_tags, attachments, branch, slots,
                                                 team=team_intent)
        elif not forced_intent and not dry_run:
            _orch = self._try_orchestrate(user_input, intent, effective_provider, attachments=attachments,
                                          conversation_id=conversation_id)
            if _orch is not None:
                # P0-2 DST：编排前落会话意图状态（编排执行走独立子管道，主会话状态在此保存）
                self._save_conversation_dst(conversation_id, intent, slots)
                return self._finish_orchestrated(_orch, user_input, conversation_id, intent,
                                                 agent_def, hil_level, kb_tags, attachments, branch, slots)

        # 闭环：上传资料解析 → 文本（优先依据注入 + 检索扩散）
        att_blocks, att_parsed, att_skipped = self._load_attachment_text(attachments)
        # P2 视觉通道（2026-09-21）：图片附件 → 多模态块。
        # 双判据（vision.enabled 开关 + provider 声明视觉能力），任一不满足则**留痕降级**
        # （原因写进 att_vision，随 attachments_info 下发），绝不静默丢弃。
        att_images, att_vision = self._prepare_attachment_vision(attachments, effective_provider)
        if att_vision.get("skipped"):
            att_skipped = list(att_skipped) + list(att_vision["skipped"])
        if att_images:
            # 让模型知道「本条消息带了图」——否则它可能只顺着文本作答（图在 content 数组里但不自知）
            att_blocks = [f"【图片附件】{'、'.join(att_vision.get('loaded') or [])}"
                          "（图像内容已随本条消息一并提供，请直接查看）"] + att_blocks
        att_text = "\n\n".join(att_blocks[:4]) if att_blocks else ""   # 注入 prompt（限量，避免超长）
        retrieval_att = "\n\n".join(att_blocks) if att_blocks else "" # 完整全文（供附件语义/词法召回）
        att_parsed_info = {"parsed": att_parsed, "skipped": att_skipped[:5]}
        if att_vision.get("images"):
            att_parsed_info["vision"] = att_vision   # 留痕：这次图片到底进没进模型、为什么

        # 闭环：基于用户内容匹配已保存工作流（名称/描述/节点标签）
        matched_flows = self._match_flows(user_input + (" " + att_text[:500] if att_text else ""))

        # SP-R：报告类型预识别（模型分析/变更影响/预评审/自定义大纲）——用于强制检索与模板选择
        report_type = None
        if intent == "report_generation":
            try:
                from report_generator import report_generator as _rg
                report_type = _rg.detect_report_type(user_input)
            except Exception:
                report_type = None

        # Step 2: GraphRAG retrieval——按需检索：#标签主动引用 / Agent kb_required 配置 / 类型化报告素材 触发
        # V3：上传附件不再默认触发知识库检索（附件仍作「优先依据」注入 prompt；需查库时加 #知识库 标签）
        should_retrieve = (bool(req_scope and req_scope.get('ok')) or bool(kb_tags) or bool(agent_def.kb_required)
                           or (intent == "report_generation" and report_type in ("impact", "review")))
        if should_retrieve:
            retrieve_query = user_input + kb_hint
            # P1：结构化拆解的实体并入检索词（提升图谱/向量召回）
            slot_ents = [str(e) for e in (slots.get("entities") or []) if str(e)][:5]
            if slot_ents:
                retrieve_query += " " + " ".join(slot_ents)
            if retrieval_att:
                retrieve_query += " " + retrieval_att[:600]
            # P0（2026-09-29）记忆召回：把「已沉淀的经验/决策」作为一路召回源并入检索结果。
            # 项目/意图由 pipeline 侧算好传入（检索侧不重复解析，避免两处取值链漂移）。
            effective_kb_scope = dict(effective_kb_scope or {})
            effective_kb_scope.update(self._memory_recall_scope(intent, user))
            retrieval = self.rag.retrieve(retrieve_query, branch, attachment_text=(retrieval_att + ('\n\n' + scope_att if scope_att else '')) or None,
                                          kb_scope=effective_kb_scope)
        else:
            retrieval = {"source": "none", "route": "none", "route_reason": "kb_optional",
                         "confidence": 0, "entities": [], "relations": [], "vector_docs": [],
                         "chunk_hits": [], "attachment_hits": [], "attachment_used": False,
                         "graph_count": 0, "vector_count": 0,
                         "memory_hits": [], "memory_count": 0}
        if should_retrieve:
            context_text = self._build_context(retrieval, retrieve_query)
        elif att_text:
            context_text = "（本次仅基于上传资料与模型知识回答，未检索知识库——可在输入加 #工程数据 / #知识库 标签主动引用，或由 Agent 配置启用知识库依赖）"
        else:
            context_text = "（未引用知识库——可在输入加 #工程数据 / #知识库 标签主动引用，或由 Agent 配置启用知识库依赖）"

        # P0 需求质量分析：requirement_quality 意图 → 规则+LLM 双通道 → 结构化报告注入
        quality_report = None
        if intent == "requirement_quality":
            try:
                from requirement_quality import build_quality_report
                quality_report = build_quality_report(user_input)
                if quality_report and quality_report.get("issues"):
                    _q_lines = [f"[{it['severity'].upper()}] {it['problem']}｜证据：{it['evidence'][:60]}｜建议：{it['suggestion'][:80]}"
                                for it in quality_report["issues"][:10]]
                    context_text = context_text + "\n\n【需求质量分析结果（供引用，按此输出报告）】\n" + "\n".join(_q_lines)
            except Exception:
                quality_report = None

        # Step 4: LLM call（携带 Agent 定义与 HIL 分级，便于真实 LLM 按分级约束输出）
        # 通用报告服务：report_generation 意图 → SP-R 类型化模板（模型分析/变更影响/预评审/自定义大纲）
        report_prompt = ""
        if intent == "report_generation":
            try:
                from report_generator import report_generator as _rg
                if report_type in ("impact", "review"):
                    _mat = self._build_report_material(report_type, retrieval)
                    if _mat:
                        context_text = context_text + "\n\n" + _mat
                report_prompt = _rg.build_prompt(user_input[:80], context_text,
                                                 sections=_rg.sections_for(report_type, user_input),
                                                 report_type=report_type)
            except Exception:
                report_prompt = ""
        skill_block = ""
        self._skill_forced = bool(skill_name)  # D4：指定技能白名单为权威限制，禁止被自动路由重置/并集
        if skill_name:
            try:
                from repositories.studio_repo import StudioRepo as _SR
                # 用别名导入：execute 内局部 `from database import get_db` 会把函数级
                # get_db 标记为局部变量（Python 编译期作用域），直接调用会 UnboundLocalError
                from database import get_db as _get_db
                conn = _get_db()
                try:
                    sk = _SR(conn).get_skill_by_name(skill_name)
                finally:
                    conn.close()
                if sk:
                    # P1-28（2026-10-02）：块文本收敛到 `common.build_skill_block`（唯一真源）。
                    # 本路径参数 = 原行为**逐字节保留**：正文不截断 + 披露 references/examples/
                    # scripts/工具白名单（流式路径是 content_cap=4000 且不披露 —— 差异显式登记）。
                    skill_block, _at = build_skill_block(sk, content_cap=0, include_resources=True)
                    if _at:
                        # D4：指定技能的白名单为**权威限制**（配合 `_skill_forced=True`，
                        # 自动路由不得重置或并集）。「设不设白名单」是**调用方职责**，故留在本路径：
                        # 流式路径当前不设（`_skill_forced` 全仓只在 execute.py:220 赋值），
                        # 该差异如实保留、不在本批静默统一（§29.9）。
                        self._skill_allowed_tools = _at
            except Exception:
                pass
        # P1-28（2026-10-02）：**块内容**收敛到 `common.build_prompt_blocks`（唯一真源）。
        # 本路径（execute：Studio Agent 试跑 / 工作流 LLM 节点 / 非流式 API）与流式对话的
        # 差异一律**显式参数化**，不抹平：
        #   · role_cap=0                不截断（本路径原行为；流式用 `_AGENT_ROLE_CAP`）
        #   · include_tool_rules=False  本路径原本**没有** tool_rules 块（流式有）
        # ⚠️ 不要"顺手"把这两项改成与流式一致 —— 那会改变本路径产出，是需质量 A/B 的独立决策
        #   （差异清单与后果见 docs §29.9）。
        system_prompt = assemble_system_prompt(build_prompt_blocks(self, PromptBlocksCtx(
            agent_def=agent_def, intent=intent, hil_level=hil_level,
            user_input=user_input, user=user, branch=branch,
            conversation_id=conversation_id, slots=slots, user_ctx=user_ctx,
            att_text=att_text, context_text=context_text, report_prompt=report_prompt,
            skill_block=skill_block, role_cap=0, include_tool_rules=False,
        )))
        # P2 视觉通道：图片以多模态 content 块随 user 消息下发
        # （openai_compat 对 messages 原样透传，故 provider 层无需改动）
        _user_content = user_input
        if att_images:
            _user_content = [{"type": "text", "text": user_input}] + att_images
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _user_content},
        ]
        # P1a-2 上下文预算分区：检索/历史区超限裁剪（防超窗，对齐 Context Spec）
        try:
            _sys = messages[0]["content"]
            _retr_idx = _sys.find("检索到的互联数据：")
            _retr = _sys[_retr_idx:] if _retr_idx != -1 else ""
            _sys_b = self._apply_context_budget(_sys, _retr, len(messages))
            messages[0] = {"role": "system", "content": _sys_b}
        except Exception:
            pass
        # 闭环：会话历史注入（v2 话题感知：当前话题原文 + 语义拉回 + 分话题摘要）
        if conversation_id:
            hist = self._load_history(conversation_id, cur_input=user_input)
            if hist:
                messages = [messages[0]] + hist + messages[1:]
        # 缺口B：工具调用循环（P0-2 升级：真实 LLM function calling 最多 3 轮 ReAct；
        # 每轮执行工具→观察回填→再次生成，直到无 tool_calls 或达上限；Mock 无 tool_calls 时行为不变）
        # 优化1：注入观测上下文（intent/agent/conversation 供 tool_call_logs 落库）
        self._tool_intent_ctx = {"intent": intent}
        self._tool_agent_ctx = {"agent": agent_def.name}
        self._tool_conv_ctx = conversation_id
        # D4：skill 工具白名单最小权限合并——命中 skill 声明了 allowed_tools 时，
        # 与委派白名单求交集（双限制取更严），未委派时直接用 skill 白名单
        try:
            _skill_wl = getattr(self, "_skill_allowed_tools", None)
            if _skill_wl:
                if self._tool_whitelist:
                    self._tool_whitelist = list(set(self._tool_whitelist) & _skill_wl)
                else:
                    self._tool_whitelist = list(_skill_wl)
        except Exception:
            pass
        tools_def = self._build_tools_def(intent, user_input, user)
        # P2：语义缓存——纯问答命中直返（构造等价 LLM 响应，复用后续落库/审计流程）
        _cache_hit = None
        # P2：att_images 非空时强制不命中缓存 —— 语义缓存的键只有 user_input，它看不见图片，
        # 若命中会把「带图的提问」当成纯文本问题直接返回旧答案（图等于白传）。
        if intent in ("chat", "knowledge_qa") and not att_blocks and not att_images and not tools_def:
            try:
                from core import config as _cfg
                if _cfg.as_bool("semantic_cache", "enabled", False):
                    from semantic_cache import SemanticCache
                    from database import get_db
                    _cc = get_db()
                    try:
                        _cache_hit = SemanticCache.get(_cc, user_input)
                    finally:
                        _cc.close()
            except Exception:
                _cache_hit = None
        if _cache_hit:
            llm_response = {"choices": [{"message": {"role": "assistant", "content": _cache_hit}}],
                            "_meta": {"provider": "semantic-cache", "model": "-", "used_mock": False, "latency_ms": 0}}
            msg = llm_response["choices"][0]["message"]
            llm_content = _cache_hit
        else:
            llm_response = llm_client.chat(messages, provider_id=effective_provider, tools=tools_def or None)
            msg = llm_response["choices"][0]["message"]
            llm_content = msg.get("content") or ""
            max_tool_rounds = 3  # P0-2：多轮 ReAct 上限（防死循环）
            for _round in range(max_tool_rounds):
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    break
                tool_results = []
                _weak_n = 0  # P0-按需工具：本轮弱相关结果数（全弱 → 收敛，禁止连环调用）
                for tc in tool_calls[:3]:  # 每轮最多执行 3 个工具
                    fn = tc.get("function") or {}
                    tname = fn.get("name", "")
                    try:
                        targs = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        targs = {}
                    result = self._exec_tool_call(tname, targs)
                    _t_res = str(result.get("result") or "")
                    _weak = bool(result.get("weak")) or ("未检索到" in _t_res) or ("无新冲突" in _t_res)
                    if _weak:
                        _weak_n += 1
                        _t_content = {"ok": result.get("ok"), "result": result.get("result", ""),
                                      "hint": "该工具结果与当前任务弱相关（或未命中实质内容），请直接基于已有信息回答，不要再调用其他工具。"}
                    else:
                        _t_content = {"ok": result.get("ok"), "result": result.get("result", "")}
                    tool_results.append({
                        "tool_call_id": tc.get("id", ""),
                        "role": "tool",
                        "name": tname,
                        "content": json.dumps(_t_content, ensure_ascii=False),
                    })
                if not tool_results:
                    break
                # 工具结果回填后继续生成（观察 → 下一轮思考/工具或最终回答）
                messages.append(msg)
                messages.extend(tool_results)
                # P0-按需工具：本轮工具结果全弱相关 → 收敛（不注入工具做最终生成，避免连环调用）
                if _weak_n == len(tool_results) and _weak_n > 0:
                    llm_response = llm_client.chat(messages, provider_id=effective_provider, tools=None)
                    msg = llm_response["choices"][0]["message"]
                    llm_content = msg.get("content") or ""
                    break
                llm_response = llm_client.chat(messages, provider_id=effective_provider, tools=tools_def or None)
                msg = llm_response["choices"][0]["message"]
                if msg.get("content"):
                    llm_content = msg["content"]
            # P2：纯问答且无工具调用 → 写入语义缓存（供后续相似查询直返）
            if intent in ("chat", "knowledge_qa") and not att_blocks \
                    and not msg.get("tool_calls") and llm_content:
                try:
                    from core import config as _cfg
                    if _cfg.as_bool("semantic_cache", "enabled", False):
                        from semantic_cache import SemanticCache
                        from database import get_db
                        _cc = get_db()
                        try:
                            SemanticCache.put(_cc, user_input, llm_content)
                        finally:
                            _cc.close()
                except Exception:
                    pass
        # P0-2: 透传 LLM 运行元信息（provider / Mock 降级），前端展示「真实/试用 LLM 接入状态」
        llm_meta = llm_response.get("_meta", {})
        llm_info = {
            "provider": llm_meta.get("provider", "未配置"),
            "model": llm_meta.get("model", "-"),
            "used_mock": llm_meta.get("used_mock", True),
            "latency_ms": llm_meta.get("latency_ms", 0),
        }
        # SysML v2 视图联动：LLM 输出含 SysML 代码 → 解析并投影各视图 ViewModel（会话内即时预览，不落库）
        # 2026-09-20：正文无代码时用工具层缓存补回（见 _ensure_sysml_from_tools 的说明）
        llm_content = self._ensure_sysml_from_tools(llm_content)
        sysml_views = self._gen_sysml_views(llm_content, intent, user_input)

        # Step 5: Store messages (exception-safe: rollback+close on any error)
        # 优化3：dry_run 验证运行不落库（messages/audit 均跳过，只返回结果）
        msg_id = 0
        if not dry_run:
            with db_conn() as conn:
                # User message（V2.3: 附件随 user 消息落库 attachments JSON）
                conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data, attachments) VALUES (?,?,?,?,NULL,?)",
                    (conversation_id, "user", user_input, "text", json.dumps(attachments, ensure_ascii=False))
                )
                # Assistant message
                card_data = json.dumps({
                    "intent": intent,
                    "agent": agent_def.name,
                    "hil_level": hil_level,
                    "kb_tags": kb_tags,
                    "skill_hits": self._last_skill_hits,   # P1/P3：自动触发技能（可观测）
                    "slots": slots,                        # P1：结构化任务拆解
                    "source": retrieval["source"],
                    "confidence": retrieval["confidence"],
                    "graph_count": retrieval["graph_count"],
                    "vector_count": retrieval["vector_count"],
                    "used_mock": llm_info["used_mock"],
                    "provider": llm_info["provider"],
                    "citations": _citations_payload(retrieval.get("chunk_hits")),  # 问答可解释性：[n] 引用来源
                    "sysml_views": sysml_views,
                    "submitted_by": ((user or {}).get("display_name") or (user or {}).get("name") or "会话发起人")
                                     if isinstance(user, dict) else "会话发起人",
                    **self._build_rich_card(intent, retrieval, branch, user_input, provider_id),
                }, ensure_ascii=False)
                cursor = conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", llm_content, self._get_card_type(intent), card_data)
                )
                msg_id = cursor.lastrowid
                _archive_artifacts(conn, conversation_id, msg_id, card_data, llm_content, intent)
                # CIA：变更影响分析记录（FR-CIA-3 每次分析结果快照落库，可追溯）
                if intent == "impact":
                    try:
                        from repositories.impact_repo import ImpactRepo
                        _cd = json.loads(card_data)
                        ImpactRepo(conn).create_analysis(
                            title=(_cd.get("change_source") or {}).get("name", user_input[:60]),
                            change_source=(_cd.get("change_source") or {}).get("name", ""),
                            params=_cd.get("params", {}),
                            result=_cd,
                            status="ok" if _cd.get("ok") is not False else "error",
                            error_code=_cd.get("code", ""),
                            conversation_id=conversation_id, message_id=msg_id,
                            created_by="王工")
                    except Exception:
                        pass

                # Update conversation
                conn.execute(
                    "UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, intent=? WHERE id=?",
                    (intent, conversation_id)
                )

                # Audit log
                conn.execute(
                    "INSERT INTO audit_logs (user_name, event_type, detail, result) VALUES (?,?,?,?)",
                    ("王工", "llm_chat", f"会话#{conversation_id} · 意图:{intent} · Agent:{agent_def.name} · 来源:{retrieval['source']}", "success")
                )
                # 话题打标：落库后立即打标（含刚插入的当前轮消息，供展示与下一轮组装复用）
                try:
                    self._tag_topics(conn, conversation_id)
                except Exception:
                    pass

        # dry_run 验证运行也需返回 card（组装但不落库）
        card_data = card_data if not dry_run else json.dumps({
            "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "source": retrieval["source"], "confidence": retrieval["confidence"],
            "graph_count": retrieval["graph_count"], "vector_count": retrieval["vector_count"],
            "citations": _citations_payload(retrieval.get("chunk_hits")),
        }, ensure_ascii=False)
        # 通用报告服务：结构化 Report（前端可下载/复用；SP-R 透传报告类型）
        report = None
        if intent == "report_generation":
            try:
                from report_generator import report_generator as _rg
                report = _rg.structure(user_input[:80], llm_content, report_type=report_type)
            except Exception:
                report = None
        # P0-2 DST：回写会话级意图状态（非 dry_run 验证运行；供下轮无信号继承与槽位合并）
        if not dry_run:
            self._save_conversation_dst(conversation_id, intent, slots)
        _intent_meta = self.router.get_last_meta()
        return {
            "message_id": msg_id,
            "intent": intent,
            "intent_route": _intent_meta["route"],
            "intent_confidence": _intent_meta["confidence"],
            "needs_clarification": _intent_meta["needs_clarification"],
            "agent": agent_def.name,
            "hil_level": hil_level,
            "kb_tags": kb_tags,
            "skill_hits": self._last_skill_hits,
            "slots": slots,
            "content": llm_content,
            "report": report,
            "card": json.loads(card_data),
            "sysml_views": sysml_views,
            "llm": llm_info,
            "retrieval": {
                "source": retrieval["source"],
                "route": retrieval.get("route", retrieval["source"]),
                "confidence": retrieval["confidence"],
                "graph_count": retrieval["graph_count"],
                "vector_count": retrieval["vector_count"],
                "attachment_used": bool(retrieval.get("attachment_hits")),
                "citations": _citations_payload(retrieval.get("chunk_hits")),  # 问答可解释性：[n] 引用来源
            },
            "attachments_info": {**att_parsed_info, "consumed": len(retrieval.get("attachment_hits") or [])},
            "matched_flows": matched_flows,
            "quality_report": quality_report,
        }
