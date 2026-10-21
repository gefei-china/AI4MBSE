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
from core.audit import audit, audit_user
from . import tool_offload as _tool_offload  # P1-4：大工具结果 offload（补模型侧封顶缺口）

#: 收尾轮「保留修复工具」时**只给这些**（2026-10-09）。
#: 为什么收窄：收尾轮的目的是「把已知错误改掉」，不需要 graph_retrieve /
#: sysml_stdlib_meta 之类查资料的工具 —— 给了反而会诱发新一轮查资料
#: （实测 80% 轮次就花在查资料上）。
#:
#: ⚠️ **仅作用于非流式路径（execute.py）**。`stream.py` 的收尾是
#:   `chat(..., stream=True)` 单次生成，**没有配套的工具执行循环**
#:   （tools 要被执行必须再走一轮 `for _rnd in range(max_tool_rounds)`）。
#:   在那里塞 tools 只会让provider 返回 tool_calls 而**无人消费** ⇒ 停摆。
#:   ⇒ stream 路径要修需配套加"收尾轮工具执行循环"，属独立改动，本次**不做**。
_REPAIR_TOOL_NAMES = ("sysml_v2_autofix", "sysml_v2_lint", "sysml_v2_validate")


def _repair_tool_subset(tools_def):
    """从本轮工具清单里挑出修复类工具；一个都没有 ⇒ 返回 None（等价改动前行为）。"""
    try:
        out = [t for t in (tools_def or [])
               if ((t.get("function") or {}).get("name")) in _REPAIR_TOOL_NAMES]
        return out or None
    except Exception:
        return None


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
        # ★ 2026-11-13（#56）：**会话级附件继承**（与 stream.py 同源同口径，见那里的根因说明）。
        #   追问轮请求里 attachments=[] ⇒ 章节索引为空 ⇒ read_attachment_section 工具
        #   压根没进 tools_def ⇒ 模型既无正文也无取正文的通道。
        #   落库用 `_att_explicit`（显式附件），继承的不写进本轮 user 消息。
        _att_explicit = list(attachments)
        attachments = self._inherit_session_attachments(conversation_id, attachments)
        # P1-30（2026-10-09）：**请求级瞬态状态统一清零** —— 本行是 N3 产出雷同的根因修复。
        # 对话走模块级单例 Pipeline（`routers/conversations.py: from agent import agent`），
        # 此前只有 `_skill_allowed_tools` 一个字段被清；`tools.py` 写的
        # `_sysml_last_pass_code` 等字段**跨请求残留** ⇒ `cards.py` 兜底把上一次请求的
        # 代码当成本次产出交付（现象即"叙述是新的、代码是旧的"）。
        # ⚠️ 位置纪律：**必须早于本方法内任何 `self.X =` 赋值**，否则会抹掉本次请求
        #   刚设好的上下文（`_mem_ctx` / `_tool_conv_ctx`）。
        #   下方保留的 `reset_skill_state` 只管显式技能授权，管不到这些字段。
        reset_request_state(self)
        # 记忆作用域上下文（对齐 mem0）：本会话的 conversation_id + 当前用户，供记忆读写取作用域
        self._mem_ctx = {"conversation_id": conversation_id, "user": user}
        # P0-c（2026-10-03 评估）：开启链路上下文（与流式主路径同一口径）
        try:
            from core.audit import begin_trace
            _trace = begin_trace(conversation_id=int(conversation_id or 0))
        except Exception:
            _trace = {}

        self._mem_project_id_cache = None   # 每次执行清缓存：缓存只在本请求内有效，防跨会话串味
        self._tool_whitelist = tools_whitelist or None
        self._load_db_agents(user)  # P0 平台化：DB 驱动 Agent 注册表（P1-8：按用户隔离）
        self._last_skill_hits = []
        # 团队模式：校验主 Agent（团队负责人）。C1/C3：不再无条件 team_forced=True，
        # 改由团队定义路由决策（与 stream.py 同一判据 `agent/team_router.decide`）。
        team_forced = False
        team_intent = None
        team_def = None
        team_decision = None
        team_direct = False
        # ★ 2026-10-17：澄清探测的 quick_pick 判据取**用户入口原值**（intent_override），
        #   不是被 team/R1 覆写后的 forced_intent（覆写后恒非空 ⇒ 内容级澄清恒被跳过，
        #   见 stream.py 同处注释）。与 `_persist_clarify` 存覆写后的值分工不冲突。
        _clarify_gate_intent = forced_intent
        if team:
            team_def = self._team_definition(team)
            team_intent = self._resolve_team(team)
            if not team_intent:
                return {"error": f"智能体团队不存在或未启用：{team}，请到「Agent 管理」创建主 Agent 并启用后重试",
                        "content": "", "intent": "chat", "agent": team, "team": team}
            # ★ 2026-10-16（米爸决策 R1+R2）：**主 Agent 优先于 forced_intent**（与 stream.py:1777 同构）。
            #   原写法 `forced_intent or team_intent` 让前端隐藏 select 的残留值压过用户所选主 Agent
            #   ⇒ 选「智源对接agent」却以「需求分析Agent」作答（会话 9367 实测）。
            #   forced_intent 参数本身保留（编排内部直连子 Agent 仍依赖它），只反转入口优先级。
            if forced_intent and forced_intent != team_intent:
                forced_intent = None
            else:
                forced_intent = team_intent
            # ★ 2026-10-16 R1：落到会话，供后续未显式带 team 的请求回读（同构 stream.py）
            self._save_main_agent(conversation_id, team_intent)
        elif conversation_id and int(conversation_id) > 0:
            # ★ 2026-10-16 R1：未带 team ⇒ 回读会话绑定（非流式路径同构）
            _bound = self._load_main_agent(conversation_id)
            if _bound:
                team_intent = _bound
                forced_intent = forced_intent or _bound
                try:
                    print(f"[main-agent] 会话#{conversation_id} 未指定主 Agent，"
                          f"按绑定「{_bound}」执行", flush=True)
                except Exception:
                    pass
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
        # ── C3/C4/C5：团队定义驱动路由（与 stream.py 同一判据，须在取 agent_def 之前）──
        if team_def:
            team_decision = self._team_route_decision(
                user_input, team_def, provider_id=provider_id, detected_intent=_detected or "")
            _act = (team_decision or {}).get("action")
            if _act == "reject":
                _msg = self._team_unsupported_message(team_def, (team_decision or {}).get("reason"))
                try:
                    print(f"[team] 不支持：{(team_decision or {}).get('reason')}"
                          f"（conversation={conversation_id}）", flush=True)
                except Exception:
                    pass
                return {"ok": False, "error": "", "intent": intent,
                        "content": _msg, "team": team_def.get("intent"),
                        "team_decision": team_decision, "team_unsupported": True}
            if _act == "chat":
                intent = "chat"
                forced_intent = None
                # 显式标记：team 直答路径**不得**再进自动编排分支。
                # 不依赖 `_ORCH_EXCLUDE_INTENTS` 黑名单兜底（那是巧合而非契约：
                # 若日后有人把 chat 从黑名单移走，问「你好」又会触发编排）。
                team_direct = True
            elif _act == "direct" and (team_decision or {}).get("target"):
                intent = team_decision["target"]
                forced_intent = intent
                team_direct = True
            elif _act == "orchestrate":
                team_forced = True
            else:
                # ★ 2026-10-16 R2：主 Agent 无成员 → 由本体自主执行（同构 stream.py:1871）。
                #   team_direct=True 必须拦住下游自动编排，否则会换执行主体。
                team_forced = False
                team_direct = True
                _notice = (team_decision or {}).get("notice") or ""
                if _notice:
                    try:
                        print(f"[team] {team_intent}：{_notice}", flush=True)
                    except Exception:
                        pass
        agent_def = self.registry.get(intent)
        hil_level = agent_def.hil_level
        self._hil_level = hil_level  # M5：HIL 分级——L2 时写工具进入人工确认队列
        # 优化2：Agent 指定 LLM provider 优先（显式 provider_id > Agent 绑定 > 全局默认）
        effective_provider = provider_id or self.registry.get_provider_id(intent)
        # ★ P1-b（2026-10-10）Agent 级模型参数（含 thinking_mode）——
        #   `agents.model_params` 此前是**死配置**（有列、有前端表单，无消费点）。
        #   取一次供本方法所有 chat 调用复用；取不到一律 {} ⇒ 零行为漂移。
        try:
            _agent_mp = self.registry.get_model_params(intent)
        except Exception:
            _agent_mp = {}
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
                    forced_intent=_clarify_gate_intent, skill_name=skill_name)
            except Exception:
                _clarify_qs = None
            if _clarify_qs:
                self._persist_clarify(conversation_id, user_input, intent, branch, attachments,
                                      forced_intent, skill_name, team, _clarify_qs)
                return {"ok": True, "clarify_asked": True, "intent": intent,
                        "content": "需要您确认建模信息（详见澄清卡片，回答后将继续）",
                        "msg_type": "clarify", "questions": _clarify_qs}
        if team_forced and not dry_run:
            # 团队模式：主 Agent（团队负责人）编排——意图识别/拆解/计划/分派/汇总
            _orch = self._try_orchestrate_team(user_input, team_intent, effective_provider,
                                               attachments=attachments, conversation_id=conversation_id,
                                               team_def=team_def)
            if _orch is not None:
                self._save_conversation_dst(conversation_id, intent, slots)
                return self._finish_orchestrated(_orch, user_input, conversation_id, intent,
                                                 agent_def, hil_level, kb_tags, attachments, branch, slots,
                                                 team=team_intent, user=user)
        elif not forced_intent and not team_direct and not dry_run:
            _orch = self._try_orchestrate(user_input, intent, effective_provider, attachments=attachments,
                                          conversation_id=conversation_id)
            if _orch is not None:
                # P0-2 DST：编排前落会话意图状态（编排执行走独立子管道，主会话状态在此保存）
                self._save_conversation_dst(conversation_id, intent, slots)
                return self._finish_orchestrated(_orch, user_input, conversation_id, intent,
                                                 agent_def, hil_level, kb_tags, attachments, branch, slots,
                                                 user=user)

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
        # ★ 2026-11-13（#55）：与 stream.py 同构——建章节索引，供 read_attachment_section
        #   按需取回缺失章节正文（模型知道有第 5/6/7 章却取不到 ⇒ 只能反问用户要原文）。
        self._index_attachment_sections(att_blocks)
        # ★ 2026-11-13（#56）：与 stream.py 同构——**提纲层 + 待取回章节清单**。
        #   ⚠️ 此前 execute.py 冷路径**只有正文首部、连提纲都没有** ⇒ 走冷路径的调用方
        #   （/api/conversations/{id}/chat 等非流式入口）模型连"文档有第 5/6/7 章"都不知道。
        _att_outline_c = self._attachment_outline(att_blocks) if att_blocks else ""
        if _att_outline_c:
            att_text = (
                att_text +
                "\n\n【上传资料·章节提纲（全文共 %d 字，以下为已识别的章节标题——"
                "**你只看到了上面那份正文的前段**，后续章节的条款正文尚未注入，"
                "**请用 read_attachment_section 工具按章节号自行取回**，"
                "不要因为正文里没有就认为文档不含该章、更不要向用户索要原文）】\n%s"
                % (len(retrieval_att), _att_outline_c)
            )
            _att_pending_c = self._attachment_pending_sections(att_blocks) if att_blocks else []
            if _att_pending_c:
                att_text = (
                    att_text +
                    "\n【**你尚未读到正文的章节（必须自行取回）**：第 %s 章】\n"
                    "以上章节的**条款正文不在你的上下文里**（只有标题/目录条目）。\n"
                    "取法：对每一章调用**一次** read_attachment_section，"
                    "section 传**该章的章号**（如 section=\"5\"）——"
                    "**一次就会返回该章的全部子节正文**（如 5.1~5.5 全部），"
                    "**不要逐个小节重复调用**（那会撑爆上下文且拿不到整章）。\n"
                    "你**必须**把上面列出的章都取回后再完成任务；"
                    "**不要**因为上下文里没有就回答「该章正文缺失」，"
                    "也**不要**要求用户重新上传或粘贴原文。"
                    % "、".join(_att_pending_c)
                )
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
        #★ 2026-10-08修**顺序 bug**：`_tool_conv_ctx` 原先在下方 322 行才赋值，
        #   而技能装配（`build_prompt_blocks` → `_build_skill_prompt` → `offload_body`）
        #   在 287 行就要读它取会话 id ⇒ **永远读到上一轮的值**（首轮读到 0）。
        #   后果：长 skill 正文的 offload 落库挂错会话，
        #   `skill_body_fetch` 取回时 id对不上 ⇒ 多视图连续调用时产出互相串扰
        #   （实测 usecase 与 activity 产出 md5 完全相同、ibd 与 sequence 同样）。
        #   ⇒ 会话上下文必须**先于**任何 prompt 装配建立。
        self._tool_conv_ctx = conversation_id
        # P1-29（2026-10-02）：状态起点统一 —— 每轮**无条件清零**。
        # 对话走全局单例（`routers/conversations.py:14`），只"有技能时才赋值"会让上一轮的
        # 白名单残留到本轮。
        reset_skill_state(self, skill_name)
        if skill_name:
            # P1-29：读取入口统一为 `load_forced_skill`（StudioRepo 含全部列 ⇒ 资源披露有数据）。
            # 本路径参数 = 原行为**逐字节保留**：正文不截断、披露资源、**正文为空也注入**
            # （`require_content=False`，与流式路径的 `True` 是有意差异，见 common 的说明）。
            skill_block, _at = load_forced_skill(skill_name, content_cap=0,
                                                 include_resources=True,
                                                 require_content=False)
            if _at:
                # D4：指定技能的白名单为**权威限制**（配合 `_skill_forced=True`，
                # 自动路由不得重置或并集）。「设不设白名单」是**调用方职责**，故留在调用点。
                self._skill_allowed_tools = _at
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
        # `_tool_conv_ctx` 已在 prompt 装配**之前**设好（见上方注释：原在此处赋值太晚，
        # 导致技能 offload 读到上一轮会话 id）—— 此处不再重复赋值。
        # P0-6（2026-10-06）：把当前用户透给工具链，供 `_exec_tool_call` 的权限闸判定。
        # 为什么必须透：`execute()` 签名里就有 user（current_user 依赖产物），
        # 但此前从未传到工具执行层 ⇒ 权限判定拿不到身份。
        self._tool_user = user
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
        # ★ 2026-10-17（米爸「harness 持续工作」）：自主续跑计数先声明——
        #   cache-hit 分支不走续跑（恒 0），但下方返回 dict 两分支共用，须先有定义。
        _auto_continues = 0
        if _cache_hit:
            llm_response = {"choices": [{"message": {"role": "assistant", "content": _cache_hit}}],
                            "_meta": {"provider": "semantic-cache", "model": "-", "used_mock": False, "latency_ms": 0}}
            msg = llm_response["choices"][0]["message"]
            llm_content = _cache_hit
        else:
            llm_response = llm_client.chat(messages, provider_id=effective_provider,
                                           tools=tools_def or None, _intent=CHAT_TASK_PROBE, _model_params=_agent_mp)
            msg = llm_response["choices"][0]["message"]
            llm_content = msg.get("content") or ""
            # ★ 2026-10-09：3 → 6，且**改为可配置**（两侧同源，见 stream.py）。
            #   实测依据（N3 视图展开 6/8 产出雷同的根因链）：
            #     模型典型序列 = 查标准库(2 轮) + 取回技能正文(1 轮) + 生成(1 轮)
            #     ⇒ 3 轮预算**必然在"生成"之前耗尽**，
            #     收尾轮又不给工具 ⇒ 手里只有「未找到包 X」这类失败信息
            #     ⇒ 只能凭记忆写 ⇒ 交出上一轮残留代码。
            #   ⚠️ 这是**通用配置**（任何"先查资料再产出"的 Agent 都受益），
            #     不是给视图开特例；上限仍存在，防死循环。
            #
            # ★★ 2026-10-09（米爸纠正语义）：**成功的正常调用不该被限制**。
            #   旧实现把 `range(max_tool_rounds)` 当**总轮次硬上限**，
            #   成功轮与失败轮**一并计数** ⇒ 成功推进被失败轮"连坐"截断。
            #   实测八视图 41 轮里 **29 轮（71%）是成功推进**，
            #   v1 的 6 轮里只有 3 轮是真推进 —— 却和"查 6 次都查不到"付同样代价。
            #   ⇒ 现在拆成**两道闸**（见下方 `_guard.failed_rounds_left()`）：
            #     · `max_tool_rounds`   → **绝对硬上限**（防死循环），6 放宽到 12
            #     · `max_failed_rounds` → **失败轮预算**，只有失败轮才扣（默认 3）
            #   成功轮再多也不扣预算 ⇒ 正常推进不被限制。
            try:
                from core import config as _cfg
                # ★ 2026-10-17（米爸反馈1）：12 → 100（与 stream.py 同步，一致性门禁会判）。
                #   100 是防死循环的绝对天花板；正常成功推进本就不扣失败轮预算。
                max_tool_rounds = int(_cfg.get("agent", "max_tool_rounds", 100))
                max_failed_rounds = int(_cfg.get("agent", "max_failed_rounds", 3))
                # ★ 2026-10-17（米爸反馈1）：token 预算 60000 → 30 万（与 stream.py 同步）。
                _lg_budget = int(_cfg.get("agent", "loop_token_budget", 300000) or 300000)
                _lg_retries = max(0, int(_cfg.get("agent", "tool_call_retries", 3) or 0))
                _lg_retry_wait = float(_cfg.get("agent", "tool_retry_timeout_s", 30) or 0)
            except Exception:
                max_tool_rounds, max_failed_rounds = 100, 3
                _lg_budget, _lg_retries, _lg_retry_wait = 300000, 3, 30.0
            # 2026-10-04：接入统一护栏（与编排路径同源，见 agent/loop_guard.py）。
            # 原先这条路径只有轮次上限/token 上限/重复检测/工具超时，
            # 而编排路径有前三者 —— 同一套 ReAct 语义的两处实现护栏不对称。
            from agent.loop_guard import LoopGuard, tokens_of as _tokof
            # P0-①/P0-②（2026-10-07）协议护栏，与 stream.py **同源**。
            from agent import loop_protocol as _lproto
            _guard = LoopGuard(
                token_budget=_lg_budget,
                tool_timeout_s=float(getattr(self, "_LOOP_TOOL_TIMEOUT", 120.0)),
                max_failed_rounds=max_failed_rounds, tool_retries=_lg_retries,
                retry_timeout_s=_lg_retry_wait)
            # ★ 2026-10-17（米爸反馈3）：ask_user 人在回路澄清信号（None=未触发）。
            #   LLM 调 ask_user ⇒ _exec_tool_call 返回 clarify_required ⇒ 中断循环，
            #   由下方统一落挂起 + 返回 clarify_asked（与 1.5 内容级澄清同构）。
            _clarify_req = None
            # ★★ 2026-10-17（米爸「harness 持续工作」）：自主续跑外层 while（与 stream.py 同构）。
            #   round 1 = 原始请求；round 2+ = 完成度评估器判定「未完成」放行的续跑。
            #   _guard **跨轮共享**（token 预算/失败轮预算/去重缓存同口径）——
            #   AutoGPT boundedness 铁律：循环每个维度都要有天花板，宁可早停不放开火烧。
            from agent import auto_continue as _ac
            _ac_cfg = self._auto_continue_cfg()
            _ac_seen_steps = []       # 已放行的 next_step（空转检测）
            # ★ 2026-10-17（米爸反馈4）：工具结果取回的 SysML 文档暂存（persist 时归档）
            self._tool_sysml_docs = []
            self._tool_sysml_docs_seen = set()
            _ac_tools_hist = []       # 全程工具调用史（评估器输入，tools_brief 内截断）
            _ac_tool_n = 0            # 本轮实际执行的工具调用数（评估触发门）
            _ac_deadline = ((time.time() + _ac_cfg["deadline_s"])
                            if (_ac_cfg["enabled"] and _ac_cfg["deadline_s"] > 0) else None)
            _ac_full = ""             # 跨轮全量产出（非流式路径，收口并回 llm_content）
            while True:
                # ── 每轮状态复位（续跑轮必须干净起步）──
                _clarify_req = None
                _ac_tool_n = 0
                if _auto_continues:
                    # 续跑轮清掉上轮 SysML 校验缓存：_ensure_sysml_from_tools 的
                    #   兜底追加是非幂等跨轮的，不清会把同一份代码重复拼进产出。
                    self._sysml_last_pass_code = None
                    self._sysml_last_checked_code = None
                for _round in range(max_tool_rounds):
                    tool_calls = msg.get("tool_calls") or []
                    if not tool_calls:
                        break
                    # 预算耗尽 ⇒ 不再调工具，直接让模型基于已有信息作答
                    if not _guard.budget_left():
                        llm_response = llm_client.chat(messages, provider_id=effective_provider,
                                                      tools=None, _intent=CHAT_TASK_ANSWER, _model_params=_agent_mp)
                        msg = llm_response["choices"][0]["message"]
                        llm_content = msg.get("content") or ""
                        break
                    # ★★ 失败轮预算（2026-10-09，米爸纠正语义）：**只有失败轮才扣**。
                    #   成功的正常推进**不限制** —— 旧实现把成功轮也计入总轮数上限，
                    #   导致"查 6 次都查不到"与"正常推进 6 轮"付同样代价（连坐）。
                    if not _guard.failed_rounds_left():
                        llm_response = llm_client.chat(messages, provider_id=effective_provider,
                                                      tools=None, _intent=CHAT_TASK_ANSWER, _model_params=_agent_mp)
                        msg = llm_response["choices"][0]["message"]
                        llm_content = msg.get("content") or ""
                        break
                    tool_results = []
                    _round_had_ok = False      # 本轮是否有工具拿到可用结果（失败预算判据）
                    _weak_n = 0  # P0-按需工具：本轮弱相关结果数（全弱 → 收敛，禁止连环调用）
                    # ── P0-①（2026-10-07）并行调用配对 / P0-② 截断参数拒绝 ──
                    # 与 stream.py **同源**（同一模块 agent/loop_protocol.py）：
                    # 这两条路径是同一套 ReAct 语义的两种实现，协议层缺陷漏一条就是线上 400。
                    # ⚠️ 不能删掉上限（失控护栏），必须为未执行尾部补 tool_result。
                    _exec_calls, _skip_calls = _lproto.split_calls(tool_calls)
                    for tc in _exec_calls:
                        fn = tc.get("function") or {}
                        tname = fn.get("name", "")
                        # P0-②：参数被 max_tokens 截断 ⇒ 拒绝执行（不以降级的空参数执行）
                        if _lproto.is_truncated_args(fn):
                            _tr = _lproto.truncated_result(tname)
                            # ★ 2026-10-09 修 P0：原写 `_tool_results.append`，
                            #   而本函数声明的是 `tool_results`（无下划线）⇒ NameError。
                            #   触发后果：整轮崩在收尾前，**零产出 + 零落库**，
                            #   前端表现为「点了没反应」，且日志只有一行 NameError。
                            #   修法：三处统一append 到 `tool_results`——本分支是
                            #   「跳过执行」，仍必须为该 tool_call_id 回喂结果，
                            #   否则悬空 tool_calls 会让下一轮请求直接 400。
                            tool_results.append({
                                "tool_call_id": tc.get("id", ""),
                                "role": "tool", "name": tname,
                                "content": json.dumps(_tr, ensure_ascii=False)})
                            continue
                        try:
                            targs = json.loads(fn.get("arguments") or "{}")
                        except Exception:
                            # `is_truncated_args` 判漏时的保守兜底：**不**以空参数执行
                            _tr = _lproto.truncated_result(tname)
                            tool_results.append({
                                "tool_call_id": tc.get("id", ""),
                                "role": "tool", "name": tname,
                                "content": json.dumps(_tr, ensure_ascii=False)})
                            continue
                        # 重复调用短路：同(工具,参数) 已成功跑过 ⇒ 回喂既有结果，不再跑一遍。
                        # 为什么这条是省钱闸不是提速闸：模型陷入循环时最典型的表现就是
                        # 同参数反复调同一工具，每次都真执行一遍。
                        _dup = _guard.probe_dup(tname, targs)
                        if _dup:
                            _guard.dup_short_circuits += 1
                            _prev = _guard.last_result_for(tname, targs)
                            tool_results.append({
                                "tool_call_id": tc.get("id", ""),
                                "role": "tool", "name": tname,
                                "content": json.dumps(
                                    {"ok": True, "result": _prev,
                                     "hint": "该工具的相同参数已执行过（这是第 %d 次），直接采用既有结果，"
                                             "不要再重复调用同一工具同一参数。" % _dup},
                                    ensure_ascii=False)})
                            continue
                        result = _guard.call(self._exec_tool_call, tname, targs)
                        # ★ harness 持续工作：计入全程工具史（完成度评估器的输入）
                        _ac_tool_n += 1
                        _ac_tools_hist.append({"name": tname, "ok": bool(result.get("ok")),
                                               "result": str(result.get("result") or "")})
                        # ★ 2026-10-17：ask_user 人在回路澄清 —— 记录信号并中断 ReAct 循环
                        #   （不再喂 tool_result、不再继续探测：等用户回答后续跑）
                        if isinstance(result, dict) and result.get("clarify_required"):
                            _clarify_req = result.get("clarify") or {}
                            break
                        _t_res = str(result.get("result") or "")
                        # ★ 2026-10-17（米爸反馈4）：识别工具结果里的完整 SysML 文档
                        #   （智源导出 READ 的 JSON 信封 / 裸文本源码），persist 时归档。
                        try:
                            from agent.utils import extract_sysml_doc_from_result as _esd
                            _doc = _esd(tname, _t_res)
                            if _doc:
                                _dk = _doc["text"][:200]
                                if _dk not in self._tool_sysml_docs_seen:
                                    self._tool_sysml_docs_seen.add(_dk)
                                    self._tool_sysml_docs.append(_doc)
                        except Exception:
                            pass
                        _weak = bool(result.get("weak")) or ("未检索到" in _t_res) or ("无新冲突" in _t_res)
                        # ⚠️ 弱结果在**调用之后**才判定得出，无法预传给 call()，
                        #   故在此回填：把刚计入的去重计数撤回，否则"工具本来就查不到东西"
                        #   会在第 2~3 次被误判成重复调用而掩盖真实的"查不到"信号。
                        if result.get("ok") and not _weak:
                            # ★ 真实拿到可用结果 ⇒ 本轮是**成功推进**，不扣失败预算
                            _round_had_ok = True
                        if _weak and result.get("ok"):
                            _guard.undo_dup_count(tname, targs)
                        if _weak:
                            _weak_n += 1
                            _t_content = {"ok": result.get("ok"), "result": result.get("result", ""),
                                          "hint": "该工具结果与当前任务弱相关（或未命中实质内容），请直接基于已有信息回答，不要再调用其他工具。"}
                        else:
                            # P1-4（2026-10-02）：补上模型侧封顶——此前此处**全量回填**（无 _TOOL_MODEL_CAP），
                            # 长结果被后续每轮重发；现与 stream 同一 helper：>cap 自动 offload + 引用块
                            # ★ 2026-11-13（#56）：与 stream.py 同构——取整章正文用**专用上限**，
                            #   否则整章（5000~7000 字符）被 offload 成引用块 ⇒ 模型看到的是空
                            #   ⇒ 误判"该章正文读不到"。详见 common._ATT_SECTION_MODEL_CAP。
                            _t_cap_c = _ATT_SECTION_MODEL_CAP if tname == "read_attachment_section" else _TOOL_MODEL_CAP
                            _t_content, _offloaded = _tool_offload.model_side_content(
                                tname, result, getattr(self, "_tool_conv_ctx", 0), cap=_t_cap_c)
                            if _offloaded:
                                _tool_offload.ensure_fetch_tool(tools_def, getattr(self, "_tool_whitelist", None))
                        tool_results.append({
                            "tool_call_id": tc.get("id", ""),
                            "role": "tool",
                            "name": tname,
                            "content": json.dumps(_t_content, ensure_ascii=False),
                        })
                    # ── P0-①（2026-10-07）为**未执行**的尾部调用补结果 ──
                    # msg 带的是**全部** tool_calls，不补尾部 ⇒ 第 4 笔起悬空 ⇒ 下一轮 400。
                    # ⚠️ 必须在 `if not tool_results:` **之前**：
                    #    全被截断/跳过时 tool_results 也要非空，否则一个结果都没有。
                    # ★ 2026-11-12（与 stream.py 同源；conv 9571 实证 7 连问 / conv 9553 实证 24 连问）：
                    #   clarify 已记录 ⇒ 立即退出轮次循环。下方 `if not tool_results: break`
                    #   只覆盖「ask_user 是本轮唯一调用」；**并行调用**时 tool_results 非空
                    #   ⇒ 会走到 messages.append(msg) ⇒ ask_user 的 tool_call 悬空
                    #   ⇒ 下一轮探测 HTTP 400（协议硬约束）。
                    if _clarify_req:
                        break
                    if _skip_calls:
                        tool_results.extend(_lproto.skipped_results(_skip_calls))
                    if not tool_results:
                        break
                    # ★★ 判定本轮成败，**只有失败轮才扣失败预算**（2026-10-09）。
                    #   `_round_had_ok` 在执行段累加（见上），比事后解析 result 文本可靠。
                    #   失败 = 本轮**实际执行**的工具全部没拿到可用结果（ok=0 / 超时 / 空结果）。
                    #   ⚠️「模型没调用工具」不算失败（那是正常收敛，由上面的 `if not tool_results` 处理）。
                    #   ⚠️ 全被跳过（参数截断）时 `_round_had_ok` 保持 False ⇒ 该轮扣预算 ——
                    #   这是**故意的**：截断意味着模型没把参数写出来，重试同样会截断。
                    _guard.charge_failed_round(_round_had_ok)
                    # 工具结果回填后继续生成（观察 → 下一轮思考/工具或最终回答）
                    messages.append(msg)
                    messages.extend(tool_results)
                    # P0-按需工具：本轮工具结果全弱相关 → 收敛（不注入工具做最终生成，避免连环调用）
                    # ⚠️ 分母用**执行数**而非 len(tool_results)：P0-① 补的 skipped 结果
                    #    计入分母会让收敛判定失真（同stream.py 的处理）。
                    if _weak_n == len(_exec_calls) and _weak_n > 0:
                        llm_response = llm_client.chat(messages, provider_id=effective_provider,
                                                      tools=None, _intent=CHAT_TASK_ANSWER, _model_params=_agent_mp)
                        msg = llm_response["choices"][0]["message"]
                        llm_content = msg.get("content") or ""
                        _guard.add_tokens(_tokof(llm_response))
                        break
                    llm_response = llm_client.chat(messages, provider_id=effective_provider,
                                                   tools=tools_def or None, _intent=CHAT_TASK_PROBE, _model_params=_agent_mp)
                    _guard.add_tokens(_tokof(llm_response))   # 2026-10-04：按累计量判预算（见 loop_guard 注释）
                    msg = llm_response["choices"][0]["message"]
                    if msg.get("content"):
                        llm_content = msg["content"]
                # P1-①（2026-10-07）轮次用尽收尾：append 一条「已达上限」提示 + 补一次**无 tools** 调用。
                # ★ 与 stream.py **同源**（同一模块的 `should_finalize`/`finalize_messages`）。
                # ★ execute.py 此前**完全没有收尾** ⇒ 模型连续 3 轮都要工具时
                #   返回 content 长度 = 0（前端显示空白对话）。
                # ★ 为什么用轮次计数而不用"msg 里有 tool_calls"：轮次用尽的最后一轮
                #   必然带 tool_calls，用它当条件会对"已给正文+仍要工具"重复收尾。
                # ★ 2026-10-08：产码 Agent（绑了 sysml_v2_validate）传 expect_code=True
                #   —— 收尾判据从「正文是否为空」升级为「**有没有代码块**」。
                #   实测 N3 视图展开 25% 无产出，根因正是模型只写一句
                #   「现在生成结构视图…」就把轮次耗尽，`not llm_content` 为假 ⇒ 收尾跳过。
                #   `_is_v2_code_agent` 是既有单一事实来源（绑了 validate 工具即产码），
                #   **不新增 intent 白名单**（那会与 register 的规则漂移）。
                _expect_code = bool(self._is_v2_code_agent(agent_def))
                if not _clarify_req and _lproto.should_finalize(msg, llm_content, _round + 1, max_tool_rounds,
                                           expect_code=_expect_code):
                    try:
                        # ★ 2026-10-09：把「已 offload 但模型始终没取回」的正文附进收尾。
                        #   实测 N3 视图展开 6/8 产出雷同的根因：模型把工具预算花在
                        #   查标准库上（拿到的是"未找到包 X"这种失败信息），
                        #   收尾轮又**不给工具** ⇒ 视图规范正文还在 offload 里没取回
                        #   ⇒ 只能凭记忆写 ⇒ 交出上一轮残留的代码。
                        #   ⚠️ 这是**通用补缺**（任何"正文 offload 了但没进 messages"
                        #   的场景都受益），不是给某个视图打补丁。
                        _extra = ""
                        try:
                            from agent.pipeline_parts import skill_body_on_demand as _sbd
                            _extra = _sbd.unfetched_bodies(
                                getattr(self, "_skill_body_offloads", None))
                        except Exception:
                            _extra = ""
                        _fm = _lproto.finalize_messages(messages, extra_context=_extra)
                        # ★ 2026-10-09：收尾轮**按需保留修复工具**。
                        #   实测八视图：v6 在 `sysml_v2_validate` 报出**78 个语法错**之后，
                        #   收尾轮是 `tools=None` 的纯文本调用 ⇒ 模型只能解释一句，
                        #   `completion_tokens=28` —— **写不出 15k 字符的修复代码**，
                        #   `sysml_v2_autofix` 实测**一次都没被调用过**。
                        #   ⇒ 「报了错没人修」的直接原因就是这一行的 `tools=None`。
                        #   判据（`_lproto.repair_tools_needed`）：
                        #   · 上一轮 validate 判据为 **block（有硬错，必须修）** → 给修复工具
                        #   · 若是 report（仅语义错，不阻断）→ 保持原行为（tools=None）
                        #   · 非产码任务 / 无校验结果 → 保持原行为（**零行为漂移**）
                        _repair_tools = None
                        try:
                            if _lproto.repair_tools_needed(messages):
                                _repair_tools = _repair_tool_subset(tools_def)
                        except Exception:
                            _repair_tools = None
                        _resp = llm_client.chat(_fm, provider_id=effective_provider,
                                                tools=_repair_tools, _intent=CHAT_TASK_REPAIR, _model_params=_agent_mp)
                        _guard.add_tokens(_tokof(_resp))
                        llm_content = _resp["choices"][0]["message"].get("content") or llm_content
                    except Exception:
                        # 收尾失败不得让整个请求失败（至少已有工具结果可展示）
                        llm_content = llm_content or ""
                # P2：纯问答且无工具调用 → 写入语义缓存（供后续相似查询直返）
                if intent in ("chat", "knowledge_qa") and not att_blocks \
                        and not _clarify_req and not msg.get("tool_calls") and llm_content:
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
                # ★★ 2026-10-17（米爸「harness 持续工作」）：完成度评估 + 续跑决策（与 stream.py 同构）。
                #   触发门：① 配置开且非 dry_run；② 本轮实际调用过工具（_ac_tool_n>0：
                #   纯问答轮没有「剩余步骤」可言，还省评估器成本）；③ clarify 未触发——
                #   澄清优先，等用户回答，绝不自动续；④ 续跑次数未满。评估器绝不抛异常，
                #   判据解析失败 ⇒ verdict=None ⇒ decide 返不续跑（fail-safe 向停）。
                if (_ac_cfg["enabled"] and not dry_run and _ac_tool_n > 0
                        and not _clarify_req and _auto_continues < _ac_cfg["max"]):
                    # 本轮产出并进全量（收口/落库用全量；评估器只看本轮+工具史）
                    if llm_content:
                        _ac_full = ((_ac_full + "\n\n" + llm_content) if _ac_full else llm_content)
                    _verdict = self._evaluate_completion(
                        user_input=user_input, exec_tools=_ac_tools_hist, answer=llm_content,
                        provider_id=effective_provider, agent_name=agent_def.name,
                        model_params=_agent_mp, guard=_guard)
                    _deadline_left = (_ac_deadline - time.time()) if _ac_deadline else None
                    _cont, _why = _ac.decide(
                        _verdict, used=_auto_continues, max_used=_ac_cfg["max"],
                        seen_steps=_ac_seen_steps, deadline_left_s=_deadline_left,
                        budget_left=_guard.budget_left())
                    if _cont:
                        _auto_continues += 1
                        _ac_seen_steps.append(str(_verdict.get("next_step") or ""))
                        # 续跑 = 崭新一轮 ReAct：本轮答复 + 续跑指令并进 messages。
                        # ⚠️ assistant 消息必须带 content（部分 provider 拒绝空 content）。
                        messages.append({"role": "assistant", "content": llm_content})
                        messages.append({"role": "user",
                                         "content": _ac.continuation_prompt(
                                             user_input, _verdict, _auto_continues)})
                        llm_response = llm_client.chat(messages, provider_id=effective_provider,
                                                       tools=tools_def or None,
                                                       _intent=CHAT_TASK_PROBE, _model_params=_agent_mp)
                        _guard.add_tokens(_tokof(llm_response))
                        msg = llm_response["choices"][0]["message"]
                        llm_content = msg.get("content") or ""
                        continue     # ← 外层循环顶：复位后开启下一轮 ReAct
                break                # ← 外层循环出口：收口只走一次
            # 收口：跨轮全量产出并回 llm_content（落库/返回/dry_run 校验用全量）
            if _ac_full:
                llm_content = _ac_full
        # ★ 2026-10-17（米爸反馈3）：ask_user 澄清收口 —— 落 user 消息 + 澄清卡后直接返回。
        #   **不能**落进下面的常规落库段：那会写一条 llm_content 可能为空的 assistant
        #   消息（UI 上多一个空气气泡），且 pending_clarify 没挂起（用户无法作答）。
        if _clarify_req:
            _iqs = [dict(_clarify_req)]
            _mid = 0
            if not dry_run and conversation_id:
                try:
                    with db_conn() as _cc:
                        _cc.execute(
                            "INSERT INTO messages (conversation_id, role, content, msg_type, attachments) "
                            "VALUES (?,?,?,?,?)",
                            (conversation_id, "user", user_input, "text",
                             json.dumps(attachments or [], ensure_ascii=False)))
                except Exception:
                    pass
                _mid = self._persist_clarify(conversation_id, user_input, intent, branch,
                                             attachments or [], forced_intent, skill_name,
                                             team, _iqs) or 0
            return {"ok": True, "clarify_asked": True, "intent": intent,
                    "content": "AI 执行中需要你确认（详见澄清卡片，回答后将继续）",
                    "msg_type": "clarify", "questions": _iqs,
                    "message_id": _mid, "clarify_source": "ask_user"}
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
                    (conversation_id, "user", user_input, "text", json.dumps(_att_explicit, ensure_ascii=False))
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
                # ★ 2026-10-17（米爸反馈4）：工具结果取回的 SysML 文档一并归档
                _archive_artifacts(conn, conversation_id, msg_id, card_data, llm_content, intent,
                                   tool_docs=getattr(self, "_tool_sysml_docs", None))
                self._tool_sysml_docs = []      # 已登记，防重复归档
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
# P0-b（2026-10-03 审计差距评估）：审计写入统一走 core.audit.audit() ——
#   此前此处直插 audit_logs 只有 5 列，绕过溯源上下文（IP/UA/request_id）与哈希链，
#   且归属写死「王工」（不可信）。改由统一入口写入，与其余 200+ 条审计同一口径。
                audit(audit_user(user), "llm_chat",
                      f"会话#{conversation_id} · 意图:{intent} · Agent:{agent_def.name} · 来源:{retrieval['source']}",
                      conn=conn)
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
                # ★ B5-11：口径修正 —— 附件全文注入（att_parsed）也算"已使用"。
                #   原口径只算 attachment_hits（语义命中片段），而上传附件的主路径是
                #   **全文注入**（_load_attachment_text → att_text，不走 hit 通道）
                #   ⇒ 实测附件内容被模型逐字引用（📎标注）时该字段仍 False，误导排查。
                "attachment_used": bool(retrieval.get("attachment_hits")) or (att_parsed_info.get("parsed", 0) > 0),
                "citations": _citations_payload(retrieval.get("chunk_hits")),  # 问答可解释性：[n] 引用来源
            },
            "attachments_info": {**att_parsed_info, "consumed": len(retrieval.get("attachment_hits") or [])},
            "matched_flows": matched_flows,
            "quality_report": quality_report,
            # ★ harness 持续工作：本次请求自主续跑次数（0=一次跑完未续）
            "auto_continues": _auto_continues,
        }
