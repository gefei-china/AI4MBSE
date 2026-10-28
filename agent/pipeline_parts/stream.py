# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：流式执行：编排流 SSE / 直接流式 execute_stream。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。

⚠️ 路径活跃度（2026-10-02 P1-26 审计）——**本文件是「主路径」，生产唯一入口**：
  · `routers/conversations.py:208` 的 `POST /api/conversations/{id}/chat/stream` → `execute_stream`；
  · 前端 `static/js` 全仓**只有这一处** chat 请求（`11-pipeline.js` / `12-chatsend.js`）；
  · 编排子任务也走本文件（`stream.py:156` / `:295` 调 `execute_stream(query, 0, ...)`）。
  ⇒ **面向用户行为/质量的改动（prompt / 检索 / 落库 / 记忆 / 编排）请以本路径为准做验证**；
    对等文件 `execute.py`（非流式）是冷路径，前端不调用（见其文件头）。
  ⇒ system_prompt 的**顺序**已由 `common.assemble_system_prompt` 收敛为单一真源，
    两条路径只收集块内容 —— 改 prompt 结构时**不要再在本文件内联拼接**（有守护脚本拦截）。
"""
from .common import *
from core.audit import audit, audit_user
from . import tool_offload as _tool_offload  # P1-4：大工具结果 offload（引用块+按需重读）
import queue as _queue  # 编排子任务事件的**实时**转发通道（见 _stream_orchestrated_flow 的 _evq）
from contextlib import nullcontext  # P0-b：_turn_cm 无 tracer 时的零开销占位


def _gates_safe():
    """执行闸生效值快照（**诊断用，永不抛**）。

    ★ 2026-10-28：本函数存在的唯一理由是"诊断信息不得带崩主流程"。
      第一次实现直接在卡片字典里写 `_gates_snapshot()`（函数名还写错了），
      实测 conv 9728：落库阶段 `NameError` ⇒ 整条消息的卡片装配中断，
      只能走 `_persist_partial_stream` 兜底存正文、卡片退化成 `[]`。
      ⇒ 诊断数据必须包在 try 里，取不到就给 None。
    """
    try:
        return gates_snapshot()
    except Exception:
        return None


def _gen_stage_name(intent, agent_def) -> str:
    """「生成」环节的 stage 名（2026-10-20 用户反馈：纯问答轮也显示「生成与校验」）。

    ★口径：环节名必须忠实反映该轮实际发生的事。「校验」指 SysML v2 校验器
    闭环（sysml_v2_validate），只有产码轮才真会跑；knowledge_qa / chat 等纯问答轮
    也叫「生成与校验」就是名不副实——用户由此合理怀疑环节是写死的（同批反馈里
    「知识库检索节点是不是写死的」是同一类困惑：环节名与实际内容对不上）。

    判定（与产码判定同一事实来源）：
      1) intent 在产码白名单（编排多阶段合成 intent 时的兜底）；
      2) 或 agent_def 绑定了 sysml_v2_validate（PromptMixin._is_v2_code_agent，
         单一事实来源 = agent_tools 绑定，新建产码 Agent 自动纳入）。
    任一命中 -> 「生成与校验」；否则 -> 「生成汇总」。
    """
    try:
        if str(intent or "") in _CODE_STAGE_INTENTS:
            return "生成与校验"
        from agent.pipeline_parts.prompt import PromptMixin as _PM
        if agent_def is not None and _PM._is_v2_code_agent(agent_def):
            return "生成与校验"
    except Exception:
        return "生成与校验"     # 判定失败回旧行为（宁可保守，不改名）
    return "生成汇总"


#: 产码意图白名单（编排多阶段合成 intent 时的兜底；细则以 2) 的绑定判据为准）
_CODE_STAGE_INTENTS = {
    "design", "architecture_skeleton", "view_expansion",
    "model_validation_repair", "multi_design", "sysml_view_generation",
}


def _turn_cm(tracer, turn_no):
    """P0-b（2026-10-25）M2：ReAct 轮的 turn span 上下文管理器。

    为什么用 with 包整轮而不是开闭配对：循环体有 10+ 个出口（break×6 /
    continue×2 / 异常上抛），逐个补 close 必然漏（同 _stream_orchestrated_traced
    "三类出口逐个补 finally 必然漏"的判据）；with 的 __exit__ 在
    break/continue/GeneratorExit（生成器被关闭）三条路径上都保证闭 span。
    无 tracer（编排子任务流/关闭开关）⇒ nullcontext，零开销。
    """
    if tracer is None:
        return nullcontext()
    return tracer.turn(turn_no)


def _best_partial(full_content, llm_content, orch_acc, streamed_acc) -> str:
    """中断时**该固化哪段**：返回"能最完整代表用户已看到内容"的那一份。

    ★ 2026-10-27（用户反馈「中途执行失败后之前输出的内容没有任何展示」）：
    判据不是"最新一轮写了什么"，而是**"用户屏幕上到底出现过什么"**。三个来源各有盲区：

    | 来源 | 覆盖 | 盲区 |
    |---|---|---|
    | `_full_content + llm_content` | 跨续跑轮的完整产出 | 续跑复位/补充指令重置时，**前一段**已流出的内容不在其中 |
    | `_orch_acc`（编排路径 token） | 编排流的逐字产出 | 直行路径为空 |
    | `_streamed_acc`（本轮所有 token） | **真的发给过用户的每一个字** | 含被重置掉的过程性叙述（但那正是用户看到的东西）|

    所以取**长度更大者**（而非固定优先级）：短的那个必然是被清空/被截断的记录，
    长的是更完整的现场。实测病灶（conv 9629 15:34:39「写入工程」轮）：前两源皆空
    （旧代码 llm_content 在流循环结束后才赋值 ⇒ 中途异常时为空），只有固化 0 条。
    """
    _norm = (((full_content + "\n\n" + llm_content) if full_content else llm_content)
             or "".join(orch_acc or [])
             or "")
    _streamed = (streamed_acc[0] if streamed_acc else "") or ""
    return _streamed if len(_streamed) > len(_norm) else _norm


def _write_op_detail(result_text: str) -> str:
    """从写操作回执里抽一句人可读摘要（供收尾提示）。

    ⚠️ 刻意**不抽业务字段**（dataId / status 等）：不同 MCP 后端的字段名不统一
    （`operationId` / `semanticStatus` / `status` …），硬抽字段名必然漏或错。
    ⇒ 只取**有界长度的原文片段**，让模型自己读出成功标识。
    """
    _t = str(result_text or "").strip()
    if not _t:
        return ""
    return (_t[:180] + "…") if len(_t) > 180 else _t


def _is_write_tool(pipe, name: str) -> bool:
    """判断一个工具是否是**写类**（含 MCP 工具）。

    - 本地工具：查 `tools.side_effect`（write / destructive）。
    - MCP 工具：**不在 tools 表** ⇒ `_tool_side_effect` 恒返回 "read" ⇒ 必须另行判定；
      用 `agent.mcp_tool_meta.mcp_write_group`（按工具名动词族识别，零硬编码）。
    """
    try:
        _se = pipe._tool_side_effect(name)
    except Exception:
        _se = "read"
    if _se in ("write", "destructive"):
        return True
    try:
        from agent.mcp_tool_meta import mcp_write_group
        return bool(mcp_write_group(name))
    except Exception:
        return False


class StreamMixin:
    """流式执行：编排流 SSE / 直接流式 execute_stream。"""

    def _stream_orchestrated_traced(self, user_input, conversation_id, branch, intent, agent_def,
                                    hil_level, kb_tags, attachments, slots, user, provider_id,
                                    team_forced: bool = False, stage_hint=None,
                                    resume_run_id: int = 0, team_def: dict | None = None) -> iter:
        """观测性 trace 包装层（2026-10-20，Task #43；与 _stream_orchestrated_flow 同签名）。

        为什么包一层而不把 flush 写进 body：body 有**三类出口**（正常 done /
        非法计划早退 / 异常上抛）且内部 20+ 处 yield —— 逐个补 finally 必然漏。
        wrapper 用 `yield from` + finally 把「span 落库」收敛成**一处**，
        且对三个调用点只是换个方法名。

        ⚠️ body 的方法名**不能改**（必须留在 `_stream_orchestrated_flow`）：
        三个门禁按名做 AST/源码断言 —— verify_orch_run_id_scope 的 C1 要求
        `run_id` 的局部赋值在其函数体内；verify_orch_reliability /
        verify_multiturn_context 对它做 src_of + 变异孪生驱动。改名 = 一次
        打破三道门禁（实测过它们的判据后才选此结构）。

        span 生命周期：body 内开/关（内存收集器）→ 本层流尾 flush（独立连接、
        一次事务）。flush 失败只丢观测数据，绝不影响用户拿结果（观测旁路铁律）；
        开关 orch_span.enabled=False 时全链路零开销。
        """
        from services.orch_span import SpanCollector
        _spans = SpanCollector()
        try:
            yield from self._stream_orchestrated_flow(
                user_input, conversation_id, branch, intent, agent_def,
                hil_level, kb_tags, attachments, slots, user, provider_id,
                team_forced=team_forced, stage_hint=stage_hint,
                resume_run_id=resume_run_id, team_def=team_def, _spans=_spans)
        finally:
            try:
                from database import get_db as _span_gdb
                _sc = _span_gdb()
                try:
                    _spans.flush(_sc)
                    _sc.commit()
                finally:
                    _sc.close()
            except Exception:
                pass

    def _stream_orchestrated_flow(self, user_input, conversation_id, branch, intent, agent_def,
                                  hil_level, kb_tags, attachments, slots, user, provider_id,
                                  team_forced: bool = False, stage_hint=None,
                                  resume_run_id: int = 0, team_def: dict | None = None,
                                  _spans=None) -> iter:
        """P0-1 真流式编排：计划生成 → 子任务逐个串行流式执行（实时推送思考/工具/文本增量）
        → LLM 汇总 → 落库 → done。

        解决"编排 53s 无事件一次性输出"问题：每个子任务执行时实时转发其 token/reasoning/tool 事件，
        前端可看到完整工程执行过程（需求分析 Agent → 方案设计 Agent → …逐个进行）。
        team_forced: 团队模式——主 Agent（团队负责人）负责意图识别/任务拆分/计划制定/任务分派/内容整合输出，
        planner prompt 注入团队负责人职责视角。
        stage_hint: P0-4（2026-09-24）——多阶段意图序列（`IntentRouter.detect_multi`）。非空时作为
        **高优先阶段序约束**注入 planner prompt，使 plan 顺序贴合用户表述；此前该信号仅发 SSE 供展示
        （识别出 ['requirement_analysis','design'] 却不驱动任何决策）。
        2026-09-25：元素形式为 `"{意图}（{该阶段原句}）"` —— 带上原句后 planner 才知道每阶段
        具体做什么（只给意图名会丢失对象/范围）。
        resume_run_id: P0-2 持久执行（2026-10-03）——非 0 时**从既有检查点恢复**而非重新规划：
          计划不再问 LLM（避免第二次模型调用给出不同拆分）、既有 `agent_tasks` 里
          已 `done` 的任务直接复用结果，只补跑没做完的（幂等，防重复写实体）。
          与 LangGraph checkpointer 同语义：同一张图 + 恢复的状态，而非"再跑一次"。
        team_def: C2（2026-10-07）—— 完整团队定义。team_forced 时把主 Agent system_prompt
          （拆解规则 + 八视图依赖顺序）与成员名册（capabilities + 触发词）注入 planner prompt。
          此前只注入「可用 Agent 池」的意图名，8 个「XX视图生成」近义成员无法区分。
        _spans: Task #43（2026-10-20）观测性 trace 的内存 span 收集器 —— 由
          `_stream_orchestrated_traced` 包装层创建、并在流尾负责 flush；本方法只负责
          开/关。**None 时全部插桩退化为 no-op**（门禁孪生驱动/直调本方法的路径
          不受影响；这也是插桩全部写成 `if _spans` 守卫的原因）。
        """
        from llm import llm_client
        from task_queue import TaskQueue
        from agent import AgentPipeline as _AP
        from database import db_conn, get_db
        import time as _t
        t0 = _t.time()
        # P0-7（2026-09-30）：run_id 此前直接用 conversation_id，`clear_run` 于是每轮把**上一轮计划
        # 整批删掉**（实测会话 514：run=514 的 5 行被新一轮 6 行覆盖）→ 计划无历史、不可审计。
        # 现改为独立的编排批次号（下面落计划时就地分配），归属会话记在 agent_tasks.conversation_id。
        run_id = 0
        # Task #43（2026-10-20）观测性 trace：span 内存 id（0=未开/收集器关闭）。
        # 在函数顶部初始化 ⇒ 任何提前出口（含降级直行）引用都不会 NameError。
        _run_sid = 0
        _plan_sid = 0
        reflection_meta = None   # P0-1（T3）：反思闭环评审轨迹（降级分支默认 None）
        goal = user_input or "执行该团队任务并汇总结果"
        # Task 10：汇总消费结构化摘要——编排三态（full/partial/failed），降级/单 Agent 直行恒 full
        _agg_status = "full"
        # P1-2：run 级预算（函数级默认值，降级/单 Agent 直行分支同样可用，供 done 事件 run_budget）
        _max_tasks = getattr(self, "_ORCH_MAX_TASKS", 6)
        run_token_budget = getattr(self, "_orch_token_budget", getattr(self, "_ORCH_TOKEN_BUDGET", 200000))
        run_tokens = 0
        budget_hit = False
        executed_count = 0
        # 1) 规划提示
        # 2026-09-23：此处原有写死的「知识库检索 done/retrieved=False」阶段事件。它无条件出现、
        # 与真实检索无关（编排模式下子任务的 stage 事件不外传，见子任务事件转发只收
        # token/reasoning/tool/done），会让用户看到一个从未真正执行过的「知识库检索·未引用」环节。
        # 现不再预发该阶段：检索真实性由各子任务按其 Agent 的 kb_required 决定。
        yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "run"}
        yield {"type": "progress", "kind": "progress",
               "delta": "（自动编排）检测到多 Agent 协作需求，正在规划子任务…"}
        conn = get_db()
        skill_pool_txt = ""
        try:
            rows = conn.execute("SELECT name, description, dependencies FROM skills WHERE status='published'").fetchall()
            if rows:
                lines = []
                for r in rows:
                    try:
                        deps = json.loads(r["dependencies"] or "[]")
                        dn = ",".join((d.get("name") if isinstance(d, dict) else str(d)) for d in deps)
                    except Exception:
                        dn = ""
                    lines.append(f"- {r['name']}: {(r['description'] or '')[:60]}{'（依赖: ' + dn + '）' if dn else ''}")
                skill_pool_txt = "\n可用技能（已发布，任务可带 skill 字段指定执行技能）：\n" + "\n".join(lines) + "\n"
        except Exception:
            pass
        # Task 5：编排候选池动态化——discover 动态池（带各 Agent 专长描述）替换旧固定 _ORCH_AGENTS
        # 委派候选收敛（主/子 Agent 团队）：主 Agent 编排时仅团队成员 + 自身可被委派
        pool_lines = self._orch_pool_desc_lines(conn)
        team_members = self._team_member_intents(agent_def)
        if team_members:
            _self_intent = getattr(agent_def, "intent_name", None) or agent_def.name
            allowed = set(team_members) | {_self_intent}
            pool_lines = [l for l in pool_lines
                          if l[2:].split(":")[0].strip() in allowed]
        # ★ 2026-10-13（B1-4）：补齐 `expected_output` / `tools` / `skills` 三个字段。
        #   背景（对标取证）：实测 `agent_tasks.expected_output` **222 行里只有 1 行有值（0.5%）**，
        #   而 DDL 有列、`create_plan` 会落库、`build_subtask_context` 会把它作为
        #   「期望产出」注入子任务 prompt —— **整条消费链都在，只缺 planner 让 LLM 产出它**。
        #   根因不是漏落库，是**主路径的 plan_prompt 只列了 5 个字段**，
        #   而非流式的 `workflows/planner.py:227-240` 列了 8 个 ⇒ 两套 prompt 漂移。
        #   主路径是前端唯一入口（`routers/conversations.py::chat_stream`），
        #   所以 99.5% 的任务实际没有完成标准。
        #   ⚠️ 字段顺序与措辞对齐非流式版，避免两套语义再漂。
        plan_prompt = (
            "你是任务规划器。把目标分解为可并行/串行执行的子任务清单，只输出 JSON：\n"
            '{"tasks": [{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
            ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
            ' "context": "该任务必需的上下文/输入引用(精简事实，可空)",'
            ' "expected_output": "该子任务的完成标准（可验证的交付物描述，50字内）",'
            ' "config": {"required": true},'
            ' "tools": ["允许的工具白名单(可空，空=继承该 Agent 默认绑定)"],'
            ' "skills": ["指定技能(可空)"]}]}\n'
            f"可用 Agent 池：\n" + "\n".join(pool_lines) + "\n"
            "task_type 可选 agent（走 Agent 完整管线）或 react（多步思考-工具求解）或 llm（纯生成）。\n"
            f"{skill_pool_txt}"
            f"要求：任务数 1~{self._ORCH_MAX_TASKS} 个；每个任务只交付一个明确成果；"
            "有依赖关系的用 deps 表达；context 只写该任务必需的事实与输入引用（上下文隔离，避免膨胀）；"
            "**每个任务都要写 expected_output**（下游据此把完成标准注入子任务，缺失即失去验收依据）；"
            "config.required 标注该任务产出是否为达成目标所必需（true=必需，false=可选/增强，"
            "required 全完成即可提前收敛收尾）；"
            "不要输出其他文字。\n"
            f"目标：{goal}"
        )
        if team_forced:
            # 团队模式：主 Agent（团队负责人）统一调度视角——意图识别 → 拆解 → 计划 → 分派 → 汇总
            # C2（2026-10-07）：拆解规则取 team_def.system_prompt **全文**。
            # 此前是 `agent_def.system_prompt[:300]` —— 主 Agent 的 prompt 补齐后长1354B，
            # 300 字截断恰好切在「拆解规则」中间（规则 1-2 保留、3-6 与输出格式全丢），
            # 而规则 3（生成≠校验）、5（不虚构能力）正是编排最需要的那几条。
            # 仍保留 8000B 上限防御异常超长 prompt（2026-10-27 截断清单 P2-C2：4000 → 8000；
            #   真库最大 agents.system_prompt = 7,981 字符 ⇒ 8000 对现有资产实际不裁）。
            _lead_sp = ((team_def or {}).get("system_prompt") or agent_def.system_prompt or "").strip()[:8000]
            # 成员名册（capabilities + 触发词）——近义成员（结构视图 vs 需求视图）的唯一区分依据
            _team_roster = ""
            if team_def:
                try:
                    from agent import team_router as _tr
                    _team_roster = _tr.roster_block(team_def)
                except Exception:
                    _team_roster = ""
            plan_prompt = (
                "你是主 Agent 团队负责人（" + (agent_def.name or intent) + "）。\n"
                "你的完整职责链路：\n"
                "① 意图识别：先判断用户任务的真实意图与目标，不要机械复述用户原文；\n"
                "② 任务拆分与计划制定：把目标拆解为清晰子任务并制定执行计划；\n"
                "③ 任务分派：把子任务分派给可用 Agent 池中的成员执行（可并行/串行，用 deps 表达依赖）；\n"
                "④ 内容整合输出：所有子任务完成后，汇总校验成员交付物，输出结构完整、结论清晰的最终答复。\n"
                + (f"你的团队定位：{_lead_sp}\n" if _lead_sp else "") +
                "现在开始：只输出 JSON 计划，格式如下：\n"
                '{"tasks": [{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
                ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
                ' "context": "该任务必需的上下文/输入引用(精简事实，可空)",'
                ' "config": {"required": true}}]}\n'
                f"可用 Agent 池：\n" + "\n".join(pool_lines) + "\n"
                + ((f"团队成员名册（agent 字段必须**逐字取自**下列 name）：\n{_team_roster}\n")
                   if _team_roster else "") +
                "task_type 可选 agent / react / llm。\n"
                f"要求：任务数 1~{self._ORCH_MAX_TASKS} 个；每个任务只交付一个明确成果；不要输出其他文字。\n"
                f"目标：{goal}"
            )
        # P0-4（2026-09-24）：multi_intent 从「仅展示」升级为「驱动计划」——
        # 用户的多阶段表述（如「先做需求分析，再输出 SysML 视图」）此前只发 SSE 事件供前端展示，
        # 对 plan 生成零影响（实测：识别出 ['requirement_analysis','design'] 却仍由 planner 自由发挥）。
        # 现把阶段序列作为**高优先阶段序约束**注入 → plan 的任务顺序与 deps 与之对齐（同阶段可并行）。
        # 注意：在构造完 plan_prompt 之后追加，不进 f-string 内部（避免与 JSON 示例花括号冲突）。
        # P0-6（2026-09-30）：会话既有产物摘要 —— 让 planner 知道"这个会话已经有什么"。
        # 实测（会话 514 第 2 轮，tmp/mt_ctx/probe_planner_digest_ab.py）：
        #   无摘要 → 「梳理现有需求数据模型…差距分析」（从零盘点，事实词命中 1 = 只有"参与者"）；
        #   补摘要 → 「在既有 SysML 需求模型 v0.1 上补充参与者信息需求定义」
        #            （context 引用 IRRequirement/ir1~ir4/需求图，事实词命中 9）。
        # 无产物会话 digest 为 "" → 此处完全不加段落（提示词逐字节不变）。追加而非插进
        # plan_prompt 的 f-string 内部：摘要可能含 { }，与 JSON 示例花括号无冲突但可读性更差。
        _digest = ""
        try:
            from agent.session_artifacts import build_digest
            _digest = build_digest(conn, conversation_id)
        except Exception:
            _digest = ""
        if _digest:
            plan_prompt += (
                "\n" + _digest + "\n"
                "若「本会话既有产物」非空且与本目标相关：任务必须**在既有产物上做增量** —— "
                "title 写明增量的对象（如「在既有需求模型 v0.1 上补充参与者」），"
                "context 写清引用哪个既有版本/元素；禁止规划「从零分析/重新建立」类任务。\n")
        if stage_hint:
            plan_prompt += (
                "\n★ 用户明确要求按以下**阶段顺序**执行，请让 plan 的任务顺序与 deps 与该序列对齐"
                "（同一阶段的任务可并行；某阶段在本目标下确实无意义可跳过，但不得乱序）："
                + " → ".join(str(x) for x in stage_hint)
            )
        plan = []
        # ★ 2026-10-20（Task #43 观测性 trace）：plan span —— 覆盖「检查点恢复 /
        #   planner 调用 / 负载感知分派」整段。在恢复分支**之前**开：两条路径
        #   （resume 复用计划 vs 新规划）走同一 span，source 在 meta 里区分。
        _plan_sid = _spans.open("plan", "任务规划") if _spans else 0
        # ── P0-2 持久执行：恢复分支（resume_run_id 非 0 时**不调用 planner**）─────────────
        # 为什么必须绕开 planner：resume 的核心是"按原计划继续"。若再问一次 LLM，
        # 得到的拆分几乎必然不同（temperature + 会话摘要已变），于是旧 run 里已 done 的
        # 任务与新 plan 对不上 —— 复用结果的前提（task_key 一致）直接失效。
        _resume_ck = None
        if resume_run_id:
            try:
                from agent.orch_checkpoint import load as _ck_load
                _resume_ck = _ck_load(conn, int(resume_run_id))
            except Exception:
                _resume_ck = None
            if _resume_ck:
                # 用检查点里记录的坐标系覆盖入参：确保这一轮写的是**同一个分支**、
                # 调的是**同一个 provider**（半程换模型会让前半轮的交付物风格/口径断裂）
                branch = _resume_ck.get("branch") or branch
                try:
                    _rp = int(_resume_ck.get("provider_id") or 0)
                    if _rp > 0:
                        provider_id = _rp
                except Exception:
                    pass
                if not (user_input or "").strip() and (_resume_ck.get("user_input") or ""):
                    user_input = _resume_ck["user_input"]
                    goal = user_input
                # 把检查点里保真的原始计划作为 truthy 的 plan，使流程走"已有计划"分支；
                # 下方 step 3 见 _resume_ck 即跳过 create_plan（任务行已在库里）
                plan = list(_resume_ck.get("plan") or [])
                yield {"type": "stage", "name": "检查点恢复", "status": "done"}
                yield {"type": "progress", "kind": "progress",
                       "delta": "（断点续跑）已从检查点恢复编排批次 #%s，复用已完成子任务，"
                                "仅补跑未完成部分…" % resume_run_id}
        if _resume_ck and not plan:
            # 检查点存在但计划为空（异常数据）：退回常规规划，不让用户卡在空计划
            _resume_ck = None
        if not _resume_ck:
            try:
                resp = llm_client.chat([{"role": "user", "content": plan_prompt}], provider_id=provider_id, _intent="planner")
                raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
                data = _AP._parse_json_block(raw) if raw else None
                plan = [t for t in ((data or {}).get("tasks") or []) if isinstance(t, dict) and t.get("key")]
            except Exception:
                plan = []

        # ★ 2026-10-13（B1-7）：**负载感知分派** —— planner LLM 选好 Agent 名之后，
        #   用确定性打分器补做两件事：
        #     ① 打分**留痕**（assign_score / load_snapshot 落库）—— 此前这两列 0/222 有值，
        #        导致"这次为什么派给它"**无法回答**（文章工程要点③要的正是这个）；
        #     ② **只在候选能力等价时**按负载做择一 —— planner 已经做了语义选择，
        #        确定性打分器不该推翻它（实测若强行改派，准确率反而低于 planner）。
        #   ⚠️ 只改派的条件极严：**多个候选的专长匹配度并列最高**（差值 < 1e-9）。
        #      不满足 ⇒ 保留 planner 的选择，仅记录分数。
        #   ⚠️ 权重与匹配口径的标定实测见 `services/assignment.py` 模块 docstring
        #      （含"原词表只覆盖 12% 真实词条"与"bigram 降级 embedding 无区分度"两条证据）。
        #   开关 `agent_assignment.enabled` 关闭 ⇒ 完全退回改动前行为（不计算、不改派）。
        try:
            from core.config import get as _cfg_aa
            _aa_on = bool(_cfg_aa("agent_assignment", "enabled", True))
        except Exception:
            _aa_on = True
        if _aa_on and plan:
            try:
                from services.assignment_integration import _assign_load_aware
                plan = _assign_load_aware(plan)
            except Exception:
                # 分派增强失败**不得影响编排**（它只是择优与留痕，不是门禁）
                pass
        # plan span 收口（幂等：非法计划早退路径的 error 收口先到则以那次为准）。
        # status 语义 = 步骤是否正常完成：拿到非空计划 ok，空计划 error（规划失败）。
        if _spans and _plan_sid:
            _spans.close(_plan_sid, "ok" if plan else "error",
                         {"source": "checkpoint" if _resume_ck else "planner",
                          "tasks": len(plan)})

        # 2) 降级：计划失败 → 单 Agent 流式直行
        if not plan:
            direct = "requirement_analysis"
            try:
                sub = type(self)()  # 拆包后避免循环导入：type(self)==AgentPipeline
                sub._load_db_agents(user)
                _sub = sub.execute_stream(goal, 0, branch, provider_id, attachments or [],
                                          forced_intent=direct, user=user, dry_run=True)
                _sub_content = ""
                for ev in _sub:
                    _et = ev.get("type")
                    if _et == "token":
                        _sub_content += ev.get("delta") or ""
                        yield ev
                    elif _et in ("reasoning", "progress"):
                        # #10 拆型：系统进度提示独立为 progress 事件，透传规则与原 reasoning 相同
                        yield ev
                    elif _et == "tool":
                        ev = dict(ev); ev["name"] = f"t1:{ev.get('name','')}"
                        yield ev
                    elif _et == "done":
                        if not _sub_content:
                            _sub_content = ((ev.get("data") or {}).get("content") or "")
                orch_content = _sub_content or "（编排降级：单 Agent 直行完成）"
                degraded = True
                orch_tasks = []
                subtasks = []
                done_items = []   # 降级直行：无子任务交付物（供下方 _ensure_sysml_blocks 引用）
                saved_flow_id = None
            except Exception as e:
                orch_content = f"编排执行失败: {str(e)[:150]}"
                degraded = True
                orch_tasks = []
                subtasks = []
                done_items = []
                saved_flow_id = None
        else:
            # 3) 落计划 + 串行执行（子任务逐个流式）
            degraded = False
            try:
                if _resume_ck:
                    # P0-2 resume：**不重建计划**。任务行（含已 done 的结果）已在库里，
                    # 直接沿用原批次号；此时 `ready_tasks` 只会挑出未完成任务 → 天然幂等。
                    run_id = int(resume_run_id)
                    # ★ Task #43：run span（根）—— run_id 落定后才能开（span 按批次号归档）。
                    if _spans:
                        _spans.bind(run_id, int(conversation_id or 0))
                        _run_sid = _spans.open("run", "run #%d" % run_id,
                                               meta={"resume": True})
                    try:
                        from agent.orch_checkpoint import touch as _ck_touch, bump_attempt as _ck_bump
                        _ck_bump(conn, run_id)
                        _ck_touch(conn, run_id, "executing")
                    except Exception:
                        pass
                else:
                    # P0-7：分配独立批次号（新号无残留，故不再 clear_run —— 那正是覆盖历史的原因）
                    run_id = TaskQueue.new_run_id(conn)
                    # ★ Task #43：run span（根）—— 新批次在此开根 span，其余五类
                    #   （plan/task/summary/reflection/views）全部挂在它下面。
                    if _spans:
                        _spans.bind(run_id, int(conversation_id or 0))
                        _run_sid = _spans.open("run", "run #%d" % run_id)
                    # ★ 2026-10-13（B1-8）：落计划前检依赖环。
                    #   `create_plan(strict=True)` 会对非法图抛 ValueError（环 / 自环 / 空 key / 重复 key）。
                    #   此处**不吞异常**：非法计划继续往下跑就是"静默全阻塞"（ready_tasks 恒空 →
                    #   主循环 break → 界面上什么都没发生），那正是本条要修的病。
                    #   但也不让它裸抛给用户看栈——落一条 blocked 任务行，让这次编排
                    #   **明确终止且可在库里查到原因**（status=blocked + error），而不是静默消失。
                    #   降级路径与门禁脚本可用 strict=False 显式取旧行为。
                    try:
                        TaskQueue.create_plan(conn, run_id, plan, assigned_by="session",
                                              conversation_id=int(conversation_id or 0))
                    except ValueError as _vde:
                        try:
                            conn.execute(
                                "INSERT INTO agent_tasks (run_id, task_key, title, agent_id, task_type,"
                                " config, deps, status, assigned_by, seq, error, conversation_id)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (run_id, "_plan_invalid", "计划依赖图非法", "", "agent",
                                 "{}", "[]", "blocked", "session", 0, str(_vde)[:500],
                                 int(conversation_id or 0)))
                            conn.commit()
                        except Exception:
                            pass
                        # ★ Task #43：非法计划早退 —— plan/run 两个 span 以 error 收口
                        #   （不依赖 flush 的未闭合兜底：那条路径 status 虽也是 error，
                        #    但带不上具体原因，排障时"为什么错"仍要翻事件流）。
                        if _spans:
                            if _plan_sid:
                                _spans.close(_plan_sid, "error", {"reason": "plan_invalid"})
                            if _run_sid:
                                _spans.close(_run_sid, "error", {"reason": "plan_invalid"})
                        yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "done"}
                        yield {"type": "error",
                               "data": "计划依赖图存在环或结构非法，本次编排已中止：%s" % _vde}
                        return
                    # P0-2 持久执行：落检查点 —— 崩溃后据此重进这张图（写失败静默，旁路不阻断编排）
                    try:
                        from agent.orch_checkpoint import save as _ck_save
                        _ck_save(conn, run_id, conversation_id=int(conversation_id or 0),
                                 phase="planned", user_input=user_input or goal or "",
                                 intent=str(intent or ""), branch=str(branch or ""),
                                 provider_id=int(provider_id or 0),
                                 skill_name=str(getattr(self, "_ck_skill", "") or ""),
                                 team=str(getattr(self, "_ck_team", "") or ""), plan=plan)
                    except Exception:
                        pass
                # P0-c：编排 run 号纳入链路上下文 —— 本 run 内所有子任务的 LLM 调用可聚合
                try:
                    from core.audit import bind_trace
                    bind_trace(run_id=int(run_id or 0))
                except Exception:
                    pass
            except Exception:
                pass
            done_items, orch_tasks = [], []
            subtasks = []   # P0-2：子任务轨迹（实时事件 + card_data.exec 持久化）
            guard = 0
            # ★ P4：提前收敛标记（汇总段据此显式标注"主动放弃"≠ partial；
            #   初值 False ⇒ 默认路径逐字不变）。
            _early_stopped = False
            _es_cancel_keys = []
            # ★ P4：M4 巡检停手标记（S9 语义：预计耗时超时间预算 ⇒ 停，不重画）。
            _m4_stopped = False
            # ── Task 7：执行层并发与可靠性（per-agent 信号量 / 子任务超时 / 自动重试 / run 预算护栏）──
            import threading as _th
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac, wait as _wait
            def _submit_with_ctx(pool, fn, *args):
                """P0-c：ThreadPoolExecutor.submit 不会继承 ContextVar —— 显式 copy_context，
                否则子任务线程里发生的 LLM 调用会丢掉 conversation_id/run_id/trace_id 归属。"""
                import contextvars as _cvx
                _ctx = _cvx.copy_context()
                return pool.submit(lambda *a: _ctx.run(fn, *a), *args)
            _max_workers = getattr(self, "_ORCH_MAX_WORKERS", 3)        # 全局并发 worker 上限（每批）
            _timeout = getattr(self, "_ORCH_SUBTASK_TIMEOUT", 120)      # 子任务 wall-clock 超时（秒，旧口径）
            # P0-7：超时判据改为「停滞为主 + 硬上限兜底」，两者均接 config（现场可调，不必改代码）
            try:
                from core import config as _ocfg
                _idle_timeout = int(_ocfg.get("delegation", "subtask_idle_timeout_s", 150) or 150)
                _hard_timeout = int(_ocfg.get("delegation", "subtask_timeout_s", _timeout) or _timeout)
            except Exception:
                _idle_timeout, _hard_timeout = 150, int(_timeout or 120)
            _max_retries = getattr(self, "_ORCH_MAX_RETRIES", 1)        # 自动重试次数上限
            _backoff = getattr(self, "_ORCH_RETRY_BACKOFF", 1)          # 重试退避基数（秒）
            # ★ P1 replanning（2026-10-21，v2 §7 P1）：T1 补跑开关 + 每 key 补跑记账。
            #   `_t1_retried` 是**进程内**集合（防"补跑→再失败→再补跑"）：
            #   resume 语义下 failed 是终态、不再派发，进程重启后无需跨进程持久
            #   （与无进展检测同口径：谱系级判定活在单次编排内）。
            #   读取失败按默认 True（配置不可用时不改变 P1 默认行为）。
            #   ⚠️ 可选依赖一律走「导入成功标志位」而非 `X = None` 兜底 ——
            #   后者踩 e2e G0 别名遮蔽判据（import 绑定后被赋值覆盖仍当模块用）；
            #   2026-10-21 同批修掉 _worker 的 _fc 同型历史 hit（B1-6 遗留）。
            try:
                from core import config as _rcfg
                _retry_once = bool(_rcfg.get("replanning", "retry_once", True))
            except Exception:
                _retry_once = True
            _t1_retried = set()          # 本次编排已 DAG 级补跑过的 task_key
            # ★ P2：重画尝试台账 `{key: [失败指纹, ...]}`（进程内）。
            #   为什么必须有它：T2-轻是**同 key 原地重派**（UPDATE 而非追行），
            #   所以一个 key 在库里**永远只有一行** —— 从 `agent_tasks` 快照读
            #   失败历史，读到的永远是"当前这一次"，无进展检测（同因第 2 次）
            #   形同虚设。台账记的是"本次编排 we've 试过几遍、每次的失败原因"。
            #   口径同 `_t1_retried`：谱系级判定活在单次编排内（resume 下
            #   failed 是终态不再派发，进程重启后不会重复触发）。
            _redraw_hist = {}
            try:
                from services import replan_signal as _rs
                _RS_OK = True
            except Exception:
                _RS_OK = False           # 信号模块不可用 ⇒ T1 退化为旧行为（不补跑）
            try:
                from services import failure_class as _fc_main
                _FC_MAIN_OK = True
            except Exception:
                _FC_MAIN_OK = False
            # ★ P2 replanning（2026-10-21，v2 §4.10）：差量重画的模块与开关。
            #   `_redraw_rounds` 记录**本次编排已重画过几轮**（进程内，与 _t1_retried
            #   同口径：谱系级判定活在单次编排内；resume 语义下 failed 是终态不再派发）。
            #   读取失败一律取**保守值**（enabled=False / no_progress_stop=True）：
            #   配置不可用时行为退化为 P1 原逻辑，绝不"因读不到配置就开重画"。
            try:
                from services import replan_redraw as _rr
                _RR_OK = True
            except Exception:
                _RR_OK = False
            _redraw_rounds = 0
            try:
                _rp_enabled = bool(_rcfg.get("replanning", "enabled", False))
                _rp_on_stall = bool(_rcfg.get("replanning", "redraw_on_stall", True))
                _rp_light = bool(_rcfg.get("replanning", "redraw_light", True))
                _rp_heavy = bool(_rcfg.get("replanning", "heavy_redraw", False))
                _rp_skip = bool(_rcfg.get("replanning", "skip_leaf", False))
                _rp_noprog = bool(_rcfg.get("replanning", "no_progress_stop", True))
                _rp_rounds = int(_rcfg.get("replanning", "max_redraw_rounds", 1) or 1)
                _rp_ratio = float(_rcfg.get("replanning", "redraw_expand_ratio", 1.5) or 1.5)
            except Exception:
                _rp_enabled = _rp_heavy = _rp_skip = False
                _rp_light = _rp_on_stall = _rp_noprog = True
                _rp_rounds, _rp_ratio = 1, 1.5
            # ★ P3 replanning（2026-10-21，v2 §4.7/§4.8）：DAG 级用户指令 + 确认卡。
            #   可选依赖同样走「导入成功标志位」（同 P1/P2 的 _RS_OK/_RR_OK 纪律）。
            #   `_directive_seen` = 本次编排已评估过的指令指纹（进程内）：
            #   同一条指令在多个迭代边界被反复看到时**只评估一次** ——
            #   否则「补一句『改成表格』」会在每个迭代都重派一遍所有目标任务。
            #   口径同 `_t1_retried`/`_redraw_hist`：谱系级判定活在单次编排内。
            try:
                from services import replan_directive as _rdir
                _RDIR_OK = True
            except Exception:
                _RDIR_OK = False
            try:
                from services import replan_confirm as _rcfm
                _RCFM_OK = True
            except Exception:
                _RCFM_OK = False
            _directive_seen = set()
            try:
                _rp_uidir = bool(_rcfg.get("replanning", "user_directive", False))
                _rp_confirm = bool(_rcfg.get("replanning", "user_confirm_redraw", False))
                _rp_cttl = int(_rcfg.get("replanning", "confirm_ttl_sec", 60) or 60)
                _rp_touch = int(_rcfg.get("replanning", "directive_max_touch", 3) or 3)
                _rp_llm = bool(_rcfg.get("replanning", "llm_redraw", False))
            except Exception:
                _rp_uidir = _rp_confirm = _rp_llm = False
                _rp_cttl, _rp_touch = 60, 3
            # ★ P4 replanning（2026-10-21，v2 §4.4）：健康巡检 + 提前收敛 + 三动作。
            #   可选依赖同款「导入成功标志位」纪律；配置读取失败一律取**保守值**
            #   （health_probe/early_stop/llm_actions 全关或锁 replace）——
            #   绝不"因读不到配置就开巡检/放开动作"（与 P2 的 enabled=False 兜底同族）。
            #   `_hp_*` 是巡检节奏的进程内水位（done 计数 + 上次巡检时刻 +
            #   滑动耗时窗口），口径同 `_t1_retried`/`_redraw_hist`：谱系级判定
            #   活在单次编排内（resume 语义下新 run 重新起算）。
            try:
                from services import replan_health as _rh
                _RH_OK = True
            except Exception:
                _RH_OK = False           # 巡检模块不可用 ⇒ 整体退化为 P3 行为
            try:
                _rp_hp = bool(_rcfg.get("replanning", "health_probe", False))
                _rp_hpllm = bool(_rcfg.get("replanning", "health_probe_llm", False))
                _rp_early = bool(_rcfg.get("replanning", "early_stop", False))
                _rp_pe_tasks = int(_rcfg.get("replanning", "probe_every_tasks", 3) or 3)
                _rp_pe_sec = int(_rcfg.get("replanning", "probe_every_sec", 60) or 60)
                _rp_pe_budget = int(_rcfg.get("replanning", "probe_time_budget_sec", 1800) or 0)
                _rp_llm_acts = tuple(
                    x.strip().lower() for x in
                    str(_rcfg.get("replanning", "llm_actions", "replace") or "replace")
                    .split(",") if x.strip()) or ("replace",)
            except Exception:
                _rp_hp = _rp_hpllm = _rp_early = False
                _rp_pe_tasks, _rp_pe_sec, _rp_pe_budget = 3, 60, 1800
                _rp_llm_acts = ("replace",)
            _hp_done_mark = 0              # 上次巡检时的 done_items 水位
            _hp_last_ts = _t.time()        # 上次巡检时刻（M4/节奏用）
            _hp_t0 = _t.time()             # 编排主循环起点（M4 的 run 耗时基准）
            _done_lats = []                # done 任务实际耗时（ms，M4 滑动窗口）

            def _write_decision(source, decision, *, round_no=0, gates=None,
                                tokens=0, latency_ms=0):
                """P4：重规划决策记录落 `replan_decisions`（v2 §4.6，真库 SQL 聚合用）。

                与 replan span **同源同点**（span 管时序/成本，本表管可聚合的决策
                语义）。旁路铁律：写失败只丢记录不阻断编排； signals 归一化各类
                decision 的形状（failure/stall/redraw 是信号 dict 列表，
                directive 是 basis 列表，health 是 S10 信号列表）。
                """
                try:
                    _sigs = decision.get("signals")
                    if isinstance(_sigs, list) and _sigs and isinstance(_sigs[0], dict):
                        _sig_txt = ["%s:%s%s" % (s.get("id"), s.get("key"),
                                                 (":" + str(s.get("detail") or "")[:40])
                                                 if s.get("detail") else "")
                                    for s in _sigs]
                    elif isinstance(_sigs, list):
                        _sig_txt = [str(s)[:60] for s in _sigs]
                    else:
                        _sig_txt = [str(x)[:60] for x in (decision.get("basis") or [])]
                    conn.execute(
                        "INSERT INTO replan_decisions (conversation_id, run_id, "
                        "source, round, signals, level, action, reason, gates, "
                        "metrics, tokens, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (int(conversation_id or 0), int(run_id or 0),
                         str(source or "")[:40], int(round_no or 0),
                         json.dumps(_sig_txt, ensure_ascii=False),
                         str(decision.get("level") or "")[:20],
                         str(decision.get("action") or "")[:20],
                         str(decision.get("reason") or "")[:500],
                         json.dumps(gates or {}, ensure_ascii=False),
                         json.dumps(decision.get("metrics") or {},
                                    ensure_ascii=False)[:2000],
                         int(tokens or 0), int(latency_ms or 0)))
                    conn.commit()
                except Exception:
                    pass
            # per-agent 信号量：按 registry.discover 的 max_concurrency 建（缺省 2；无 discover 数据 → 不限流）
            agent_sems = {}
            try:
                for _d in self.registry.discover(conn):
                    _mc = int(_d.get("max_concurrency") or 2)
                    agent_sems[_d["intent"]] = _th.Semaphore(max(1, _mc))
            except Exception:
                agent_sems = {}

            def _run_subtask(tk):
                """子任务真实执行（T8 受控上下文 + 事件收集 + token 记账）。返回 9 元组
                （tkey, ttitle, ok, sub_res, evs, err, latency_ms, tok_used, summary_proto）。

                token 记账：优先取 done 事件 data.meta/data.llm.token_count（llm/ 层回填）；
                无则按输出文本 len//2 估算兜底（与 llm/ 层 Mock 估算口径一致）。

                Task 9（隔离命名空间 + 摘要协议收发）：
                - 命名空间：sub._ns_ctx=f"{run_id}:{tkey}"（供后续检索/记忆过滤使用）；
                  execute_stream 签名无 kb_scope 参数（检索 scope 由 Agent 定义 kb_scope 决定），
                  故 kb_scope 透传跳过，仅把 config.kb_scope 存为 sub._ns_kb_scope 供后续消费。
                - 附件按需注入：config.attachments 白名单优先（仅传 run 级附件中匹配项）；
                  config 未声明 → 传全部 run 级附件（保持旧行为，兼容存量编排）。
                - 摘要协议：结果 dict→validate / 纯文本→normalize_text 兼容解析；
                  sanitized 协议结构写回 agent_tasks.summary_json（独立连接容错 UPDATE）；
                  协议 status=failed → ok=False；done_items 附带 summary 供 Task 10 汇总消费。
                """
                tkey = tk.get("task_key") or "t"
                ttitle = tk.get("title") or tkey
                # P0-c：子任务标识进链路上下文（本线程独立 context，不污染兄弟子任务）
                try:
                    from core.audit import bind_trace
                    bind_trace(sub_task_key=tkey)
                except Exception:
                    pass
                _hb[tkey] = _t.time()   # P0-7：活性心跳（置位即表示"已启动"，worker 的停滞超时据此判定）
                cfg = tk.get("config") or {}
                if isinstance(cfg, str):
                    try:
                        cfg = json.loads(cfg) or {}
                    except Exception:
                        cfg = {}
                # Task 9：附件按需注入——config.attachments 白名单优先；未声明时传全部（兼容存量）
                _cfg_att = cfg.get("attachments")
                if isinstance(_cfg_att, list) and _cfg_att:

                    def _att_id(a):
                        if isinstance(a, str):
                            return a
                        if isinstance(a, dict):
                            return str(a.get("name") or a.get("filename") or a.get("id") or a)
                        return str(a)

                    _whitelist = {str(x) for x in _cfg_att}
                    _sub_att = [a for a in (attachments or []) if _att_id(a) in _whitelist]
                else:
                    _sub_att = attachments or []
                query = cfg.get("query") or f"{ttitle}\n团队目标：{goal}"
                # T7：任务上下文快照（任务定义 + 前置依赖结果摘要 + 交付规范）
                try:
                    from workflows.planner import build_subtask_context
                    _tctx = build_subtask_context(tk, goal, list(done_items),
                                              artifact_digest=_digest,
                                              offload_map=dict(_orch_offloads))
                    if _tctx:
                        query = f"[任务上下文快照]\n{_tctx}\n\n[任务]\n{query}"
                except Exception:
                    pass
                # P0-6（2026-09-30）：planner 写的 context 此前在流式路径**只落库不消费**
                # （`agent_tasks.context` 有值、子任务 query 里没有；非流式 `_run_task`
                # 早就注入了，见 workflows/planner.py）。不同步补上，"在既有产物上做增量"
                # 这条指令就到不了执行者手上 —— 与"摘要只进 planner"是两件事。
                _plan_ctx = str(tk.get("context") or cfg.get("context") or "").strip()
                if _plan_ctx:
                    query += f"\n\n任务上下文（只读，勿编造上下文外事实）：\n{_plan_ctx[:2000]}"
                # ★ 2026-10-13（批次二 P2-2）：**把交付协议契约注入子任务 query**。
                #   真库实证（2026-10-12 只读）：`summary_json` 的
                #   `artifacts`/`evidence`/`risks` 三数组**非空率 0%**（180 行全空）——
                #   而 `services/subtask_protocol.py::validate` 会**按 schema 校验**这三个数组。
                #   根因不是校验太松，而是**没有任何地方要求子 Agent 输出这个格式**：
                #   全仓搜 `schema_version` 只在 registry 的协议版本协商里出现，
                #   子 Agent 的 prompt 里**根本没有这段契约** ⇒ 模型只能返回纯文本，
                #   于是永远走 `normalize_text`（三数组恒空）。**要一个字段有值，
                #   得先有人要求它有值。**
                #   ⚠️ 措辞刻意保留"正文在前、JSON 在最后"：直接让模型只吐 JSON 会毁掉
                #     面向用户的可读交付物（`sub_res` 是要展示和汇总的正文）。
                #   ⚠️ 开关可关（关 = 回到改动前行为），避免协议要求干扰个别纯生成型子任务。
                try:
                    from core.config import get as _cfg_sp
                    _sp_on = bool(_cfg_sp("subtask_protocol", "inject_contract", True))
                except Exception:
                    _sp_on = True
                if _sp_on:
                    query += (
                        "\n\n【交付格式】先用自然语言给出完整正文（用户要看的成果），"
                        "然后**另起一段**，以单个 JSON 对象结尾（用于结构化归档，勿把 JSON 混进正文）：\n"
                        '{"schema_version":1,"status":"full|partial|failed",'
                        '"summary":"一句话成果","artifacts":[{"kind":"document|model|code|view|report|other",'
                        '"title":"产物标题","ref":"产物定位或名称"}],'
                        '"evidence":[{"source":"graph|vector|attachment","ref":"引用位置","basis":"verified|derived|asserted"}],'
                        '"risks":[{"level":"high|mid|low","title":"风险名","desc":"描述","advice":"缓解建议"}],'
                        '"claims":[{"text":"结论一句话","kind":"fact|inference|to_verify",'
                        '"subject":"这条结论讲的对象/指标名"}],'
                        '"confidence":0.0}\n'
                        "规则：artifacts 列出**本次真实产出的可交付物**（无则 []）；"
                        "evidence 列出支撑结论的依据，并给每条标 basis："
                        "**verified**=有可复核引用（图谱实体/需求 id/校验器输出/附件路径）、"
                        "**derived**=由已验证事实推出、**asserted**=无引用的直接断言（最不可靠）；"
                        "没查证过的**一律写 asserted**，不要为显得可信而标 verified；"
                        "★ claims 列出本次任务得出的**关键结论**（给人看的，不是日志）："
                        "kind 三档 —— **fact**=第三方可独立复核（引了具体 id/路径/校验器输出）、"
                        "**inference**=由若干 fact 推出、**to_verify**=你**明确知道还没验证**"
                        "（如「推测上游接口可能返回 3 个字段，但未查证」）；"
                        "**subject 必填**且要写对象名（如「转发器接口字段数」），"
                        "系统据此检出多个子 Agent 对同一对象是否给出了互斥取值；"
                        "拿不准该归哪一档就写 to_verify，**诚实标注不会被扣分**；"
                        "risks 用 level/title/desc/advice 四字段"
                        "（level 取 high|mid|low，title 是一句话风险名，desc 是描述，"
                        "advice 是缓解建议）——**字段名必须是这四个**，写错会被校验器丢弃；"
                        # ★ 2026-10-22：missing[] 从"顺带一提"升为**硬约束**。
                        # 它是补齐判定器的**第一判据**（agent/partial_policy.py 的 classify_partial
                        # 优先读它，而不是从正文里正则猜"有没有说没校验"）。
                        # 真库实测：写了的 missing 如「模型校验结果（未执行 sysml_v2_validate）」
                        # 能直接判成"客观缺口、可自动补"；没写就只能靠猜，猜错方向就是
                        # 该补的不补 / 不该补的白补 token。
                        "**status=partial 或 failed 时必须写 missing[]**，逐条列出缺什么"
                        "（缺的产物 / 数据 / 步骤 / 结论）；"
                        "missing 与 risks 的分工：**missing=缺什么，risks=已有什么风险及缓解建议**，"
                        "两者都要写、不要互相替代。"
                        # ★ 关键区分：缺"自己能补的"与缺"要外部给的"处置完全不同
                        # （前者自动补跑，后者补跑一百次也一样，转人工）。所以要求模型
                        # 把两类都如实写进 missing —— 判定器会按条目里的主语分流。
                        "特别注意：缺**自己就能补的**（如某一步校验没跑、某个产物没落盘）"
                        "与缺**需要客户/用户/外部提供的**（如名单、口径、指标区间、基线数据）"
                        "都必须如实写进 missing，不要因为「怕显得没做完」而省略。"
                    )
                skill_name2 = cfg.get("skill") or None
                evs, sub_res, sub_has_token = [], "", False
                tok_used = 0
                _t0 = _t.time()

                def _cancel_probe(task_id):
                    """#19 协作式中止探针：**每次开独立连接**查一次 status。

                    ⚠️ **必须独立连接**：主路径的 `conn` 可能正持有未提交事务
                       （写操作暂存），在它上面再查会：
                       ① 读到未提交数据 ② 长期占用连接 ⇒ 与 B1-3 修过的
                       "悬挂事务"同型。⇒ 宁可多一次短连接，也不碰主连接。
                    ⚠️ 任何异常都当"未取消"（fail-safe 方向 = 继续跑，
                       因为误判成取消会**丢掉用户要的东西**）。
                    """
                    try:
                        _c = get_db()
                        try:
                            return bool(TaskQueue.is_cancel_requested(_c, task_id))
                        finally:
                            _c.close()
                    except Exception:
                        return False

                try:
                    sub = type(self)()  # 拆包后避免循环导入：type(self)==AgentPipeline
                    sub._load_db_agents(user)
                    # T8：编排子任务写操作受控上下文（_exec_tool_call 据此暂存写请求，汇总后统一确认）
                    sub._orch_subtask = True
                    sub._tool_run_ctx = int(run_id or 0)
                    sub._tool_task_key = tkey
                    # ★ P0-3（2026-10-27）：子任务**沿用外层 trace**（不新开 ——
                    #   否则一次编排会碎成 N 个 trace，整轮成本无法聚合）。
                    #   两级取值：外层实例 → 当前 ContextVar（子任务线程可能拿不到实例属性）。
                    try:
                        from core.audit import trace_context as _tc3
                        sub._tool_trace_id = (getattr(self, "_tool_trace_id", "")
                                              or str((_tc3() or {}).get("trace_id") or ""))
                    except Exception:
                        sub._tool_trace_id = getattr(self, "_tool_trace_id", "") or ""
                    # Task 9：隔离命名空间 run:tkey + kb_scope 预留
                    # （execute_stream 无 kb_scope 参数 → 跳过透传；存属性供后续检索/记忆过滤消费）
                    sub._ns_ctx = f"{run_id}:{tkey}"
                    if isinstance(cfg.get("kb_scope"), dict) and cfg.get("kb_scope"):
                        sub._ns_kb_scope = dict(cfg["kb_scope"])
                    try:
                        # 共享产物工作区：把上游交付物引用挂到子任务实例上——
                        # tools.py 构建 tools_def 定稿时据此注入 tool_result_fetch（供下游取全文）
                        if _orch_offloads:
                            sub._orch_result_offloads = dict(_orch_offloads)
                        for ev in sub.execute_stream(query, 0, branch, provider_id, _sub_att,
                                                     forced_intent=tk.get("agent_id") or None,
                                                     skill_name=skill_name2, user=user, dry_run=True):
                            _hb[tkey] = _t.time()   # P0-7：任何产出（token/reasoning/tool）都算"活着"
                            # ★ 2026-10-14（#19 健壮性）：**协作式中止探针**。
                            #   每收到一个产出就问一次"这个任务被取消了吗"，命中即 `break`
                            #   —— 正在飞行的那一次 LLM 调用会跑完，但**后续步骤全部跳过**
                            #   ⇒ 从"永远卡住"变成"最多多等一轮"。
                            #   ⚠️ 探针自带 try（`is_cancel_requested` 内部也不抛）：
                            #     它跑在**产出热路径**上，抛异常就成了新的崩溃源。
                            try:
                                if _cancel_probe(tk["id"]):
                                    _hb.pop(tkey, None)
                                    break
                            except Exception:
                                pass
                            _et = ev.get("type")
                            if _et == "token":
                                sub_has_token = True
                                sub_res += ev.get("delta") or ""
                                # ★ 2026-10-20（用户报"过程性内容全堆在最下面"）：token 必须
                                #   带 key 才会被前端归到对应子任务卡（11-pipeline.js 的
                                #   handleSSE：ev.key 有值 → subOutput[tkey] 实时输出区；
                                #   无值 → 最终正文区）。reasoning/tool 两支早已注入归属
                                #   （V2.6 / name 前缀），唯独 token 漏了 ⇒ 所有子任务的
                                #   实时产出都流进底部正文，"过程与结果糊成一片"——正是
                                #   2026-09-26 注释里声称已解决的旧问题在 token 支上的残留。
                                ev = dict(ev); ev["key"] = tkey
                                evs.append(ev)
                                _evq.put(ev)          # 实时转发（就地输出）
                            elif _et in ("reasoning", "progress"):
                                # #10 拆型：progress（系统提示）与 reasoning 同路透传；
                                # key 标记对 notice 无害（procAddSystemNotice 以文案为键）
                                ev = dict(ev); ev["key"] = tkey   # V2.6：子任务思考归属标记（前端归组展示）
                                evs.append(ev)
                                _evq.put(ev)
                            elif _et == "tool":
                                e2 = dict(ev); e2["name"] = f"{tkey}:{e2.get('name','')}"
                                evs.append(e2)
                                _evq.put(e2)
                            elif _et == "done":
                                ddata = ev.get("data") or {}
                                if not sub_res:
                                    sub_res = ddata.get("content") or ""
                                _m = ddata.get("meta") or ddata.get("llm") or {}
                                if isinstance(_m, dict):
                                    _tc = _m.get("token_count")
                                    if isinstance(_tc, (int, float)) and _tc > 0:
                                        tok_used = int(_tc)
                        ok = bool(sub_res.strip()) or sub_has_token
                        if not tok_used:
                            tok_used = max(len(str(sub_res)) // 2, 0)   # 估算兜底：len(输出文本)//2
                        # Task 9：摘要协议收发——结果 dict→validate；纯文本（旧格式）→normalize_text 兼容解析
                        _summary_proto = None
                        try:
                            from services.subtask_protocol import validate as _sp_validate, \
                                normalize_text as _sp_normalize, \
                                extract_protocol_json as _sp_extract
                            _s0 = str(sub_res or "").strip()
                            # ★ 2026-10-13：改用 `extract_protocol_json`（能处理「正文 + 尾部 JSON」）。
                            #   原写法只试 `startswith("{")`，对"正文在前"的形态恒为 False
                            #   ⇒ 注入契约后仍拿不到三数组（实测踩到的真缺口）。
                            _parsed = _sp_extract(_s0)
                            if isinstance(_parsed, dict):
                                _pr = _sp_validate(_parsed)
                                _summary_proto = _pr.get("sanitized")
                            else:
                                _summary_proto = _sp_normalize(sub_res)
                            # 协议状态 → 子任务成功标志：failed→fail（现状 tk_ok 判定保留兼容）
                            if _summary_proto and _summary_proto.get("status") == "failed":
                                ok = False
                        except Exception:
                            _summary_proto = None
                        # 协议结构写回 agent_tasks.summary_json（独立连接容错 UPDATE，参照 T7 token 写回）
                        if _summary_proto:
                            try:
                                _c9 = get_db()
                                try:
                                    _c9.execute("UPDATE agent_tasks SET summary_json=? WHERE id=?",
                                                (json.dumps(_summary_proto, ensure_ascii=False), tk["id"]))
                                    _c9.commit()
                                finally:
                                    _c9.close()
                            except Exception:
                                pass
                        # ★ 2026-10-13（批次二 P2-1/P2-2）：把协议三数组**物化**到 subtask_artifacts。
                        #   为什么必须物化（真库实证，2026-10-12 只读）：
                        #     · `subtask_artifacts` **0 行** —— 此前**只有** 写操作暂存
                        #       （`store_write_request`，tools.py:544）会写它，而那条路径要求
                        #       `is_write_tool` 命中 5 个硬编码名字，真库 10 个 write 工具里
                        #       只有 5 个在白名单 ⇒ 交付类产物**从无路径进表**；
                        #     · `summary_json` 的 `artifacts/evidence/risks` 三数组
                        #       **非空率 0%**（180 行全空）—— 协议结构写了，但**没有第二份可检索的实体化数据**，
                        #       汇总段只能读 summary 文本，下游无法按产物/证据做检索与统计。
                        #   物化后：① 交付物从"只在 JSON 里"变成"库里有行、可查询"；
                        #   ② 汇总/审计能按 kind 聚合，不必解 JSON。
                        #   ⚠️ 只物化**非空**的数组项；`store()` 内部已对非 dict 做容错。
                        #   ⚠️ 物化失败**不得影响子任务成败**（它只是可观测层，不是门禁）。
                        if _summary_proto:
                            try:
                                _arts = []
                                # 开关：materialize 关 ⇒ 不物化（只留在 summary_json）
                                from core.config import get as _cfg_mat
                                _mat_on = bool(_cfg_mat("subtask_protocol", "materialize", True))
                                for _k, _kind, _title_keys in (("artifacts", "artifact", ("title", "ref", "name")),
                                                       ("evidence", "evidence", ("source", "ref", "title")),
                                                       ("risks", "risk", ("title", "desc", "advice"))):
                                    if not _mat_on:
                                        break
                                    for _item in (_summary_proto.get(_k) or []):
                                        if isinstance(_item, dict):
                                            # ⚠️ 三类的"标题字段"不同：artifacts 用 title，
                                            #   evidence 用 source、risks 用 issue
                                            #   （见 subtask_protocol.SUMMARY_SCHEMA 的 required）。
                                            #   只取 title ⇒ evidence/risks 物化出**空标题行**，
                                            #   库里有行但没法看 ⇒ 等于没物化。按序取第一个非空。
                                            # ⚠️ 变量名**不能叫 `_t`**：本模块顶层有
                                            #   `import time as _t`，一旦在函数里赋值 `_t`，
                                            #   Python 会把它当成**函数局部变量** ⇒ 同一函数内
                                            #   所有 `_t.time()` 变成"未绑定" ⇒
                                            #   UnboundLocalError（实测被 verify_orch_reliability 抓到）。
                                            #   这里统一用 `_t_title`。
                                            _t_title = ""
                                            for _tk in _title_keys:
                                                if str(_item.get(_tk) or "").strip():
                                                    _t_title = str(_item.get(_tk))[:80]
                                                    break
                                            _arts.append({"kind": _kind,
                                                          "title": _t_title,
                                                          "content_json": _item})
                                        elif isinstance(_item, str) and _item.strip():
                                            _arts.append({"kind": _kind, "title": _item[:80],
                                                          "content_json": {"text": _item}})
                                # ★ 2026-10-14（批次三 #17）：给 report/requirement/doc
                                #   三类交付物做**结构校验**并**随产物落库**。
                                #   现状（取证）：`KIND_VALUES` 声明了 4 类，但**只有 sysml
                                #   走 checker.jar** ⇒ 报告/需求/文档格式对不对全凭模型自觉。
                                #   ⚠️ **只挂诊断不阻断**：物化是可观测层，不是门禁；
                                #     校验器自身异常一律降级（degraded=True, checked=0）。
                                #   ⚠️ `kind='artifact'`（本仓自造的一类）**不参与** ——
                                #     它是 artifacts[] 的容器名，不是协议里的 kind。
                                #
                                # ★ 2026-10-14 米爸定位纠正后的修正（**通用性**）：
                                #   `content_json` 里只有协议字段（{kind,title,ref}），
                                #   **没有正文** ⇒ 校验器会拿到空内容并判「内容为空」红。
                                #   那个红是**假红**：模型没按协议给结构化字段是常态，
                                #   但正文一直都在（`sub_res`）。
                                #   ⇒ 把 `sub_res`（子任务正文）作为**回落正文**传进去。
                                #   效果：模型配合协议时校验协议字段；**不配合时校验正文**
                                #   —— 后者与模型无关，是真正的工程能力。
                                try:
                                    from services.artifact_validator import (
                                        validate_artifact as _av)
                                    _body_fallback = str(sub_res or "")
                                    for _a in _arts:
                                        if _a.get("kind") != "artifact":
                                            continue
                                        _cj = _a.get("content_json") or {}
                                        _ak = str(_cj.get("kind") or "")
                                        # 优先用协议里的正文字段；没有就回落到子任务正文
                                        _av_src = _cj
                                        if not any(str(_cj.get(k) or "").strip()
                                                   for k in ("content", "text", "body",
                                                             "markdown", "value")):
                                            _av_src = dict(_cj)
                                            if _body_fallback.strip():
                                                _av_src["content"] = _body_fallback
                                        _av_res = _av(_ak, _av_src)
                                        if int(_av_res.get("checked") or 0) > 0:
                                            _a["content_json"] = dict(
                                                _cj, structure_check=_av_res)
                                except Exception:
                                    pass
                                if _arts:
                                    from services.artifact_materializer import store as _art_store
                                    _c10 = get_db()
                                    try:
                                        # ⚠️ 用闭包变量 `run_id`（不是 `_run_id`——
                                        #   后者在本函数里不存在，会被 except 静默吞掉，
                                        #   表现为"物化永远不生效"且无任何日志）。
                                        _art_store(_c10, int(run_id or 0), tkey, _arts)
                                    finally:
                                        _c10.close()
                            except Exception:
                                pass
                        # ★ 2026-10-20（共享产物工作区·方案1）：完整交付物落 offload 共享区，
                        #   供下游子任务按 id 取全文（替代"只传 400 字摘要"的口径）。
                        #   阈值 800：太短的交付物直接内联进摘要即可，不值得一条引用。
                        _off_id = 0
                        if ok and sub_res and len(sub_res) > 800:
                            try:
                                from agent.pipeline_parts import tool_offload as _toff
                                _off_id = _toff.save_offload(
                                    "task_result:%s" % tkey, sub_res, int(conversation_id or 0))
                            except Exception:
                                _off_id = 0      # 落库失败不阻断编排（退化为纯摘要口径）
                        return tkey, ttitle, ok, sub_res, evs, "", int((_t.time() - _t0) * 1000), tok_used, _summary_proto, _off_id
                    except Exception as e:
                        return tkey, ttitle, False, "", [], str(e)[:150], int((_t.time() - _t0) * 1000), 0, None, 0
                    finally:
                        sub._orch_subtask = False
                except Exception as e:
                    return tkey, ttitle, False, "", [], str(e)[:150], int((_t.time() - _t0) * 1000), 0, None, 0

            def _worker(tk, task_pool):
                """Task 7 执行包装：per-agent 信号量限流 + 子任务超时 + 自动重试。

                信号量语义：acquire 在 worker 线程进入时执行；release 只在本 worker 线程
                自然结束（成功 / 超时 / 重试耗尽返回）时执行——超时任务的后台子任务线程
                不中断、继续在后台运行直至自然结束（期间占用 task_pool 线程）；额度随
                worker 返回即释放，保证后续批次不被超时任务阻塞。
                """
                # ★ 2026-10-13（B1-6）：失败分类器（局部 import，与本文件既有惯例一致）。
                #   包在 try 里：分类器不可用时**必须退回旧行为**（照重试），不能因此中断编排。
                #   ⚠️ 2026-10-21（P1）改写法：原 `_fc = None` 兜底踩了 e2e G0 的
                #   「别名遮蔽」判据（import 绑定后被同作用域赋值覆盖 + 仍当模块用）——
                #   改为**导入成功标志位** `_FC_OK`：`_fc` 全程只有 import 一个绑定，
                #   兜底走标志位，G0 判据（顺序敏感的最近绑定）自然通过。
                try:
                    from services import failure_class as _fc
                    _FC_OK = True
                except Exception:
                    _FC_OK = False
                tkey = tk.get("task_key") or "t"
                ttitle = tk.get("title") or tkey
                _t0 = _t.time()
                # ★ Task #43：task span —— 覆盖「claim + 信号量等待 + 全部重试」。
                #   挂在 run span 下（闭包读 _run_sid，主循环派发时早已开好）。
                #   worker 跑在 _exec_pool 线程里 ⇒ 收集器必须线程安全（内部有锁）。
                _tsid = (_spans.open("task", tkey, parent=_run_sid,
                                     meta={"agent": tk.get("agent_id"),
                                           "title": str(ttitle)[:80]})
                         if _spans else 0)
                # ★ 2026-10-13（B1-3）：写入 `running` 态。
                #   为什么在**worker 内**而不是主循环批量写：主循环拿到的是 `ready` 全量
                #   （可能 6 个而只有 3 个并发槽），批量标 running 会把**尚未开始**的任务
                #   也标成"执行中"—— 崩溃后 resume 分不清"从未开始"与"跑到一半"，
                #   反而制造了比现在更糟的失真。worker 真正被调度即开始执行，此刻标才对。
                #   `claim` 自带 `WHERE status IN ('planned','ready')` 前置条件：
                #   并发下同一任务不会被标两次，且幂等（重复调用不报错）。
                #   ⚠️ resume 侧**已经能正确处理 running**（`orch_checkpoint.policy_classify`：
                #   有活进程 ⇒ `owned` 拒接管；无主 ⇒ `requeue`），此前只是从没被写入过。
                try:
                    TaskQueue.claim(conn, tk["id"])
                except Exception:
                    # 标 running 失败**不得阻断执行**（它只是可观测性，不是门禁）
                    pass
                sem = agent_sems.get(tk.get("agent_id"))
                if sem is not None:
                    sem.acquire()   # 进入该 Agent 并发额度（同 Agent 并发 ≤ max_concurrency）
                try:
                    retry_left = _max_retries
                    last_res = None
                    while True:
                        fut = _submit_with_ctx(task_pool, _run_subtask, tk)
                        # P0-7（2026-09-30）：判据由「固定 wall-clock」改为「停滞为主 + 硬上限兜底」。
                        # 实测（会话 514）：t1 到 182s 仍在正常吐 token，却在 241s 被判 failed、产出
                        # 丢弃 → 最终答复退化成「（计划已执行，但无成功交付物）」。按"是否还在产出"
                        # 判才对得上语义：真卡死（无事件）快速失败，活跃生成不误杀。
                        _stall = ""
                        while True:
                            _done, _ = _wait([fut], timeout=2.0)
                            if _done:
                                break
                            _now = _t.time()
                            _last = _hb.get(tkey) or 0
                            if _last and (_now - _last) > _idle_timeout:
                                _stall = "timeout（%ds 无产出）" % _idle_timeout
                                break
                            if (_now - _t0) > _hard_timeout:
                                _stall = ("timeout（线程池排队超 %ds）" % _hard_timeout if not _last
                                          else "timeout（超过 %ds 硬上限）" % _hard_timeout)
                                break
                        if not _stall:
                            last_res = fut.result()
                            if last_res[2]:
                                return last_res
                        else:
                            # 超时：不等待线程（后台继续运行），标记 error 含 "timeout"
                            last_res = (tkey, ttitle, False, "", [], _stall,
                                        int((_t.time() - _t0) * 1000), 0, None)
                        # ★ 2026-10-13（B1-6）：**先分类、再决定要不要重试**。
                        #   改前判据只有 `if retry_left <= 0`，不看失败原因 ⇒ 权限拒绝、参数非法、
                        #   输出不合格这类"重试也不会变"的永久失败也白白重试一次，
                        #   而单子任务实测 344~1155 s，还要再烧一次完整的子 Agent LLM 调用。
                        #   分类器 `services/failure_class.py` 复用 `tool_resilience` 的瞬态词表
                        #   （不另造第二份 —— 两份词表必然漂移）。
                        #   ⚠️ `_fc` 为 None（分类器不可用）时**按旧行为照重试**：
                        #   拿不到分类能力时保守多试一次，优于直接放弃任务。
                        if _FC_OK:
                            _ecls = _fc.classify(last_res[5], ok=bool(last_res[2]))
                            _retry_ok = _fc.is_retryable_class(_ecls)
                        else:
                            _ecls, _retry_ok = "", True
                        if retry_left <= 0 or not _retry_ok:
                            if last_res[5] and _ecls:
                                last_res = last_res[:5] + (
                                    "%s［%s］" % (str(last_res[5])[:130], _ecls),) + last_res[6:]
                            return last_res
                        # 自动重试：retry_count+1 写回 agent_tasks（容错：独立连接，失败不阻断）
                        retry_left -= 1
                        tk["retry_count"] = int(tk.get("retry_count") or 0) + 1
                        try:
                            _c2 = get_db()
                            try:
                                _c2.execute("UPDATE agent_tasks SET retry_count=? WHERE id=?",
                                            (tk["retry_count"], tk["id"]))
                                _c2.commit()
                            finally:
                                _c2.close()
                        except Exception:
                            pass
                        # 指数退避（2026-10-13：原为线性 `_backoff * (max_retries - retry_left)`）。
                        # ⚠️ 用指数而非线性不是为了"更优雅"——重试场景下等待时间成倍增长能显著降低
                        #   对瞬态故障源的二次冲击；而 `_max_retries` 实测只有 1，退避总时长几乎不变，
                        #   真正生效的是「等更久一点再打第二次」这个行为差异。
                        _t.sleep(_backoff * (2 ** (_max_retries - retry_left)))
                finally:
                    if sem is not None:
                        sem.release()   # 仅本 worker 线程自然结束时释放
                    # ★ Task #43：task span 收口。放在 semaphore 的 finally 里 ——
                    #   worker 的**全部出口**（成功 / 重试耗尽 / 不可重试失败 /
                    #   异常上抛）都经过这里，不会漏；异常时 last_res 为 None ⇒
                    #   记 error（与 flush 的未闭合兜底同口径，但这里能带上 attempts）。
                    if _spans and _tsid:
                        _w_meta = {}
                        try:
                            _w_meta = {"tokens": int(last_res[7] or 0) if last_res else 0,
                                       "latency_ms": int(last_res[6] or 0) if last_res else 0,
                                       "retry_count": int(tk.get("retry_count") or 0),
                                       "attempts": max(1, _max_retries - retry_left + 1)}
                            if last_res and last_res[5]:
                                _w_meta["error"] = str(last_res[5])[:120]
                        except Exception:
                            pass
                        _spans.close(_tsid, "ok" if (last_res and last_res[2]) else "error",
                                     _w_meta)

            while TaskQueue.pending_count(conn, run_id) > 0 and guard < 200:
                guard += 1
                # P0-2 持久执行：主循环心跳（节流 5 s —— 主循环每 50 ms 转一圈，不节流会把
                # SQLite 单写者打满；节流后即使任务执行 20 分钟也只有几百次 UPDATE）
                try:
                    from agent.orch_checkpoint import touch as _ck_hb
                    _ck_hb(conn, run_id, None, every_s=5.0)
                except Exception:
                    pass
                # Task 7：run 级预算护栏——已执行任务数 ≥ _ORCH_MAX_TASKS 或累计 token ≥ 预算
                # → 停止派新任务；未执行任务标记 blocked 后 break（汇总时记为 partial）
                if executed_count >= _max_tasks or run_tokens >= run_token_budget:
                    budget_hit = True
                    try:
                        for _tk in TaskQueue.tasks(conn, run_id):
                            if _tk["status"] in ("planned", "ready"):
                                TaskQueue.block(conn, _tk["id"], "预算超限")
                    except Exception:
                        pass
                    yield {"type": "progress", "kind": "progress",
                           "delta": "（自动编排）达到运行预算限制（任务数或 token），未派发子任务已标记，提前进入汇总…"}
                    break
                # ── P3 replanning（2026-10-21，v2 §4.7）：DAG 级用户指令消费 ──
                #   **挂在主循环迭代边界**（预算护栏之后、`ready_tasks` 之前）——
                #   与 followup.py 同款纪律：只在轮次边界消费，不打断正在执行的
                #   tool_call/子任务（DecodingAI 的教训：即时注入会 corrupt 配对）。
                #   ⚠️ 为什么用 `peek` 语义而非 `pop`：**不能抢**单 Agent 的
                #   follow-up 队列（那是"重启一轮 tool 循环"的语义）；DAG 级指令
                #   只**读取**（`_fup_dag_peek`），让两条通道各司其职（v2 §4.7 C1）。
                #   纪律：① `user_directive` 默认关 ⇒ 整段 no-op（零行为变更）；
                #        ② 观测旁路铁律：整段 try/except，异常只丢记录不阻断编排；
                #        ③ 评估不重画：T0 忽略、T2-light/heavy 走原地重派（UPDATE），
                #           T3 才交 LLM 整图重画（`llm_redraw` 默认关）。
                if _RDIR_OK and _rp_uidir and _rp_enabled and conversation_id:
                    try:
                        from agent import followup as _fup_dag
                        # ★ 只取**未被单 Agent 消费过**的条目：`pop` 会清空队列，
                        #   这里不能调；用 stats 判存在性 + pop 会在无指令时误清。
                        #   口径：只在"队列非空"时读，读完**不消费**（C1 分工）。
                        if _fup_dag.pending_count(conversation_id):
                            _d_tasks = TaskQueue.tasks(conn, run_id)
                            _d_pend = _rdir.pending_tasks(_d_tasks)
                            # ★ 只取**未被单 Agent 消费过**的条目：`pop` 会清空队列，
                            #   这里不能调；`peek_text` 是只读快照（见 followup.py）。
                            #   口径：只在"队列非空"时读，读完**不消费**（C1 分工）。
                            _d_text = _fup_dag.peek_text(conversation_id) or ""
                            # 影响评估（C1/C2/C3 判据内建于 should_evaluate）
                            _d_gate = _rdir.should_evaluate(
                                _d_text, enabled=True,
                                is_resume=bool(resume_run_id),
                                has_pending_tasks=bool(_d_pend))
                            if _d_gate.get("evaluate"):
                                _d_fp = "%s|%d" % (_d_text[:120], len(_d_tasks))
                                if _d_fp in _directive_seen:
                                    pass                      # 同指令已评估过 ⇒ 不重复评估
                                else:
                                    _directive_seen.add(_d_fp)
                                    _t_dd0 = _t.time()
                                    _d_as = _rdir.assess_impact(
                                        _d_text, _d_tasks,
                                        goal="",
                                        max_touch=_rp_touch)
                                    _d_ms = int((_t.time() - _t_dd0) * 1000)
                                    # 决策留痕：每次评估（含 T0）都落 span
                                    if _spans:
                                        _dd_sid = _spans.open(
                                            "replan", "directive-decide",
                                            parent=_run_sid,
                                            meta={"pending": len(_d_pend)})
                                        if _dd_sid:
                                            _spans.close(_dd_sid, "ok",
                                                         _rdir.directive_meta(
                                                             _d_as, latency_ms=_d_ms))
                                    # ★ P4：决策记录落 replan_decisions（与 span 同点）
                                    _write_decision("directive", _d_as,
                                                    latency_ms=_d_ms)
                                    _d_lvl = str(_d_as.get("level") or "")
                                    if _d_lvl in (_rdir.LEVEL_LIGHT,
                                                  _rdir.LEVEL_HEAVY):
                                        # T2-light/heavy：原地重派（UPDATE，**不追行** ——
                                        #   同 key 追行会让 ready_tasks 派发两次，
                                        #   理由同 P2 的 X-a 断言）。
                                        _d_applied = 0
                                        for _tk2 in _d_pend:
                                            _kk2 = str(_tk2.get("task_key") or "")
                                            if _kk2 not in (_d_as.get("target_keys") or []):
                                                continue
                                            try:
                                                _nd_ctx = _rdir.retask_context(
                                                    _tk2.get("context") or "",
                                                    _d_text, level=_d_lvl)
                                                TaskQueue.retry(conn,
                                                                int(_tk2["id"]))
                                                conn.execute(
                                                    "UPDATE agent_tasks SET context=?, "
                                                    "retry_count=COALESCE(retry_count,0)+1, "
                                                    "error='' WHERE id=?",
                                                    (_nd_ctx, int(_tk2["id"])))
                                                _t1_retried.add(_kk2)
                                                _d_applied += 1
                                            except Exception:
                                                pass
                                        if _d_applied:
                                            conn.commit()
                                            yield {"type": "reasoning",
                                                   "kind": "progress",
                                                   "delta": "（自动编排）按用户中途指令"
                                                            "重派 %d 个任务：%s"
                                                            % (_d_applied, _d_text[:60])}
                                            for _k6 in (_d_as.get("target_keys") or []):
                                                yield {"type": "subtask", "key": _k6,
                                                       "status": "run",
                                                       "title": "按用户指令重派",
                                                       "agent": "", "deps": [],
                                                       "retry_count": 1}
                                            if _RS_OK:
                                                _pd = _rs.plan_revised_payload(
                                                    {"action": "replan",
                                                     "level": _d_lvl,
                                                     "reason": _d_as.get("reason") or ""},
                                                    run_id=run_id)
                                                if _pd:
                                                    _pd["updates"] = list(
                                                        _d_as.get("target_keys") or [])
                                                    yield _pd
                                    elif (_d_lvl == _rdir.LEVEL_REDRAW and _rp_llm
                                          and _RR_OK):
                                        # T3：交 LLM 整图重画（**默认关**）。
                                        #   走 LLM 通道真实生成替代子图，再交给
                                        #   `plan_llm_redraw` 过**同一套 Q1~Q5 质量门** ——
                                        #   门禁不通过 ⇒ T4 停手，绝不"尽力而为落库"。
                                        #   ⚠️ 动作范围由 `llm_actions` 配置说了算
                                        #   （P4）：默认 "replace"（P3 行为逐字不变）；
                                        #   放开 skip/insert 后由解析层 + 计划层 +
                                        #   Q3 三道兜底（见 plan_llm_redraw docstring）。
                                        # ★ T3 的 replace 范围基准 = **全部非终态任务**
                                        #   （不只是 `target_keys`，也不只是
                                        #   `pending_tasks`）。
                                        #   理由：T3 的语义是"推翻任务划分重做
                                        #   整图"（v2 §4.10），**被替代的对象由
                                        #   LLM 自行决定**（它产出哪些替代任务，
                                        #   就替代哪些）。若这里传一个"编排侧猜
                                        #   出来的"窄集合（如只传 target_keys），
                                        #   范围锁定（`lineage.from ∈ failed`）
                                        #   就会把 LLM 的合法替代判成"越界" ——
                                        #   表现为"llm_redraw 开了但永远 T4，
                                        #   且日志里看不出是范围基准给窄了"。
                                        #   ⚠️ done 任务**不在**范围内：不变量 1
                                        #   （done 永不重跑）。
                                        _d_fail = []
                                        for _t3f in (_d_tasks or []):
                                            _k3f = str(_t3f.get("task_key") or "")
                                            _s3f = str(_t3f.get("status") or "")
                                            if _k3f and _s3f not in ("done", "failed",
                                                                   "cancelled",
                                                                   "canceled"):
                                                _d_fail.append(_k3f)
                                        if not _d_fail:
                                            _d_fail = list(_d_as.get("target_keys") or [])
                                        _d_raw = ""
                                        _d_perr = ""
                                        try:
                                            _d_tpl, _d_hint = _rdir.llm_redraw_prompt(
                                                goal or "", _d_tasks,
                                                directive=_d_text,
                                                allow_actions=_rp_llm_acts)
                                            _d_resp = llm_client.chat(
                                                [{"role": "user", "content": _d_tpl}],
                                                provider_id=provider_id,
                                                _intent="planner")
                                            _d_raw = ((_d_resp.get("choices") or [{}])[0]
                                      .get("message", {}) or {}).get("content", "")
                                            _d_data = (_AP._parse_json_block(_d_raw)
                                                       if _d_raw else None)
                                            _d_raw = (json.dumps(_d_data, ensure_ascii=False)
                                                      if isinstance(_d_data, dict)
                                                      else (_d_raw or ""))
                                        except Exception as e:
                                            _d_perr = ("LLM 调用失败：%s" % e)[:120]
                                        _d_dec = _rr.plan_llm_redraw(
                                            _d_tasks, _d_fail, llm_raw=_d_raw,
                                            parse=_rdir.parse_llm_plan,
                                            enabled=True, round_no=1,
                                            directive=_d_text, goal=goal or "",
                                            parse_error=_d_perr,
                                            allow_actions=_rp_llm_acts,
                                            validate=TaskQueue.validate_plan)
                                        # ★ P4：T3 决策记录（span + replan_decisions）。
                                        #   P3 期 T3 只有应用没有决策 span（评估的
                                        #   directive-decide 覆盖不到"画了什么"）——
                                        #   G6 纪律下 action≠none 的决策必须留痕。
                                        _d3_ms = 0
                                        if _spans:
                                            try:
                                                _d3_sid = _spans.open(
                                                    "replan", "t3-decide",
                                                    parent=_run_sid,
                                                    meta={"failed": len(_d_fail)})
                                                if _d3_sid:
                                                    _d3_meta = _rs.decision_meta(
                                                        _d_dec, round_no=1) \
                                                        if _RS_OK else {
                                                            "action": _d_dec.get("action"),
                                                            "level": _d_dec.get("level"),
                                                            "reason": _d_dec.get("reason"),
                                                            "signals": [],
                                                            "round": 1, "tokens": 0,
                                                            "latency_ms": 0}
                                                    try:
                                                        _d3_meta["gates"] = {
                                                            g: ("pass" if v else "fail")
                                                            for g, v in
                                                            (_d_dec.get("gates") or {}).items()
                                                            if g in ("Q1", "Q2", "Q3",
                                                                     "Q4", "Q5")}
                                                    except Exception:
                                                        pass
                                                    _spans.close(_d3_sid, "ok", _d3_meta)
                                                    _write_decision(
                                                        "t3_redraw", _d_dec,
                                                        gates=_d3_meta.get("gates") or {})
                                            except Exception:
                                                pass
                                        if _d_dec.get("action") == "replan":
                                            # ★ 落库顺序**先改下游 deps 再 create_plan**
                                            #   （同 P2 重量级）：deps 指向的新 key 此刻
                                            #   还不存在，但 `UPDATE` 不校验外键，而
                                            #   `create_plan` 会过滤"计划外 key" ——
                                            #   反序会把刚写的 t2_r1 依赖当场丢掉。
                                            _d_ins = 0
                                            try:
                                                # ★ P4：先 cancel 被 skip 的任务
                                                #   （LLM 判"其产出对下游非关键"）——
                                                #   顺序在 deps 更新**之前**：cancel
                                                #   不校验外键，而 kept 的 deps 已由
                                                #   计划层移除被跳 key，无悬空边。
                                                for _sk3 in (_d_dec.get("skip") or []):
                                                    for _t3s in (_d_tasks or []):
                                                        if (str(_t3s.get("task_key") or "")
                                                                == str(_sk3)):
                                                            TaskQueue.cancel(
                                                                conn, int(_t3s["id"]),
                                                                "计划修订：%s"
                                                                % str((_d_dec.get(
                                                                    "skip_reason") or {}
                                                                ).get(_sk3)
                                                                    or "输入非关键")[:200])
                                                            break
                                                for _kt3 in (_d_dec.get("kept") or []):
                                                    _kk3 = str(_kt3.get("key")
                                                              or _kt3.get("task_key")
                                                              or "")
                                                    if not _kk3:
                                                        continue
                                                    conn.execute(
                                                        "UPDATE agent_tasks SET deps=? "
                                                        "WHERE run_id=? AND task_key=? "
                                                        "AND status IN ('planned','ready')",
                                                        (json.dumps([str(d) for d in
                                                                     (_kt3.get("deps") or [])],
                                                                    ensure_ascii=False),
                                                         run_id, _kk3))
                                                conn.commit()
                                                TaskQueue.create_plan(
                                                    conn, run_id,
                                                    _d_dec.get("new_tasks") or [],
                                                    assigned_by="replanner",
                                                    conversation_id=conversation_id)
                                                _d_ins = len(_d_dec.get("new_tasks") or [])
                                            except Exception as _d_ex:
                                                # ⚠️ 不静默吞：观测旁路铁律允许"不阻断编排"，
                                                #   但**必须留痕** —— 否则"T3 开了却没落库"
                                                #   这类故障在日志里完全不可见（排障时
                                                #   只能靠猜）。
                                                _d_ins = 0
                                                try:
                                                    print("[replan] T3 落库失败：%s"
                                                          % str(_d_ex)[:160], flush=True)
                                                except Exception:
                                                    pass
                                            if _RS_OK:
                                                # ★ P4：插入（insert）任务单列 ——
                                                #   修订卡要能回答"多了什么"。
                                                #   ⚠️ config 可能是 dict（解析层
                                                #   产出）或 JSON 串（库行形状），
                                                #   两种都要认。
                                                _d_ins_keys = []
                                                for _x3 in (_d_dec.get("new_tasks")
                                                            or []):
                                                    _c3 = _x3.get("config")
                                                    if isinstance(_c3, str):
                                                        try:
                                                            _c3 = json.loads(_c3 or "{}")
                                                        except Exception:
                                                            _c3 = {}
                                                    _lin3 = (_c3 or {}).get("lineage") \
                                                        if isinstance(_c3, dict) else None
                                                    if (isinstance(_lin3, dict)
                                                            and str(_lin3.get("action")
                                                                    or "") == "insert"):
                                                        _d_ins_keys.append(
                                                            str(_x3.get("key") or ""))
                                                _pd3 = _rs.plan_revised_payload(
                                                    {"action": "replan",
                                                     "level": _rdir.LEVEL_REDRAW,
                                                     "reason": _d_dec.get("reason") or ""},
                                                    run_id=run_id)
                                                if _pd3:
                                                    _pd3["updates"] = list(_d_fail)
                                                    _pd3["added"] = [
                                                        str(x.get("key") or "")
                                                        for x in (_d_dec.get("new_tasks")
                                                                  or [])]
                                                    _pd3["inserted"] = _d_ins_keys
                                                    _pd3["skipped"] = [
                                                        str(k) for k in
                                                        (_d_dec.get("skip") or [])]
                                                    yield _pd3
                                            yield {"type": "reasoning", "kind": "progress",
                                                   "delta": "（自动编排）T3 整图重画："
                                                            "新增 %d 个替代任务（%s）"
                                                            % (_d_ins, _d_dec.get("reason")
                                                               or "")[:50]}
                                        else:
                                            # 质量门不过 ⇒ T4 停手（保持原计划，不落库）
                                            yield {"type": "reasoning", "kind": "progress",
                                                   "delta": "（自动编排）T3 重画被拦：%s"
                                                            % (_d_dec.get("reason") or "")[:80]}
                    except Exception:
                        pass
                # ── P4 replanning（2026-10-21，v2 §4.4）：健康巡检 + 提前收敛 ──
                #   挂点与 P3 指令消费同款：**主循环迭代边界**（ready_tasks 之前）——
                #   本批任务的产出刚全部落库，是"计划还健康吗/目标达成了吗"的
                #   天然检查点；不打断任何执行中的东西（本段运行时无 in-flight）。
                #   ⚠️ 三条纪律：
                #   ① 总闸 `replanning.enabled` + `health_probe`/`early_stop`
                #      分闸，默认全关 ⇒ 本段整体 no-op（**零行为变更**，P3 行为逐字保持）；
                #   ② 巡检只产信号不直接动作（S10 ⇒ T2-light 精修，仍走原地 UPDATE）；
                #   ③ 观测旁路铁律：整段 try/except，异常只丢记录不阻断编排。
                if _RH_OK and _rp_enabled and (_rp_hp or _rp_early):
                    try:
                        _hp_now = _t.time()
                        _hp_tasks = TaskQueue.tasks(conn, run_id)
                        # ── A. 提前收敛（先于巡检：它可能直接终结循环）──
                        if _rp_early:
                            _es_dec = _rh.early_stop_plan(_hp_tasks, enabled=True)
                            if _es_dec.get("action") == "early_stop":
                                # 决策留痕（span + replan_decisions，与 P1~P3 同点）
                                _es_rec = {"action": "early_stop", "level": "T0",
                                           "reason": _es_dec.get("reason") or "",
                                           "signals": []}
                                if _spans:
                                    try:
                                        _es_sid = _spans.open(
                                            "replan", "early-stop",
                                            parent=_run_sid,
                                            meta={"cancel": len(
                                                _es_dec.get("cancel_keys") or [])})
                                        if _es_sid:
                                            _spans.close(
                                                _es_sid, "ok",
                                                _rh.health_meta(_es_rec))
                                    except Exception:
                                        pass
                                _write_decision("early_stop", _es_rec)
                                # 取消剩余非 required 任务 + 轨迹记账（前端可见）
                                _es_cancel = [str(k) for k in
                                              (_es_dec.get("cancel_keys") or [])]
                                _early_stopped = True
                                _es_cancel_keys = list(_es_cancel)
                                for _tk4 in _hp_tasks:
                                    if (str(_tk4.get("task_key") or "") in _es_cancel
                                            and str(_tk4.get("status") or "")
                                            in ("planned", "ready")):
                                        TaskQueue.cancel(
                                            conn, int(_tk4["id"]),
                                            "目标已达成提前收敛（主动放弃非必需任务）")
                                        orch_tasks.append({
                                            "key": _tk4.get("task_key"),
                                            "title": _tk4.get("title"),
                                            "agent": _tk4.get("agent_id"),
                                            "status": "skipped", "latency_ms": 0,
                                            "retry_count": int(
                                                _tk4.get("retry_count") or 0),
                                            "summary_status": ""})
                                        subtasks.append({
                                            "key": _tk4.get("task_key"),
                                            "title": _tk4.get("title"),
                                            "agent": _tk4.get("agent_id") or "",
                                            "deps": [], "status": "skipped",
                                            "latency_ms": 0,
                                            "error": "目标已达成提前收敛（主动放弃）",
                                            "output": None,
                                            "retry_count": int(
                                                _tk4.get("retry_count") or 0),
                                            "summary_status": ""})
                                yield {"type": "reasoning", "kind": "progress",
                                       "delta": "（自动编排）目标已达成提前收敛："
                                                "必需产出全部完成，主动放弃 %d 个"
                                                "非必需任务（%s）"
                                                % (len(_es_cancel),
                                                   ",".join(_es_cancel[:5]))}
                                for _k7 in _es_cancel:
                                    yield {"type": "subtask", "key": _k7,
                                           "status": "skipped",
                                           "title": "主动放弃（目标已达成）",
                                           "agent": "", "deps": [],
                                           "retry_count": 0}
                                if _RS_OK:
                                    _pe4 = _rs.plan_revised_payload(
                                        {"action": "early_stop", "level": "T0",
                                         "reason": _es_dec.get("reason") or ""},
                                        run_id=run_id)
                                    if _pe4:
                                        _pe4["skipped"] = _es_cancel
                                        yield _pe4
                                break
                        # ── B. 健康巡检（S10；M4 停手优先于精修）──
                        if _rp_hp and _rh.probe_due(
                                len(done_items) - _hp_done_mark,
                                _hp_now - _hp_last_ts,
                                every_tasks=_rp_pe_tasks, every_sec=_rp_pe_sec):
                            _hp_done_mark = len(done_items)
                            _hp_last_ts = _hp_now
                            _hp_remain = sum(
                                1 for _t5 in _hp_tasks
                                if str(_t5.get("status") or "") in ("planned", "ready"))
                            _hp_left = (_rp_pe_budget - (_hp_now - _hp_t0)) \
                                if _rp_pe_budget > 0 else 0
                            # LLM 版巡检（health_probe_llm，默认关；+1 次轻量调用）
                            _hp_verdict = None
                            if _rp_hpllm:
                                try:
                                    _hp_tpl, _ = _rh.llm_health_prompt(
                                        goal or "", _hp_tasks,
                                        done_summaries=[
                                            str((it.get("summary") or {}).get(
                                                "summary") or "")[:120]
                                            for it in done_items[-5:]])
                                    _hp_resp = llm_client.chat(
                                        [{"role": "user", "content": _hp_tpl}],
                                        provider_id=provider_id, _intent="planner")
                                    _hp_raw = ((_hp_resp.get("choices") or [{}])[0]
                                               .get("message", {}) or {}).get(
                                                   "content", "")
                                    _hp_verdict, _hp_perr = _rh.parse_llm_health(
                                        _hp_raw)
                                    if _hp_perr:
                                        _hp_verdict = None   # 不可解释 ⇒ 不用
                                except Exception:
                                    _hp_verdict = None
                            _hp_dec = _rh.health_probe(
                                done_items, _hp_tasks, enabled=True,
                                window=_rp_pe_tasks,
                                latencies_ms=list(_done_lats[-max(3, _rp_pe_tasks):]),
                                remaining_tasks=_hp_remain,
                                budget_left_sec=_hp_left,
                                llm_verdict=_hp_verdict)
                            # 决策留痕（含 none —— "为什么不动图"同等重要）
                            #   ⚠️ replan_decisions 的写入**不依赖** span 开关 ——
                            #   span 可关（观测降级），决策记录是 SQL 聚合的数据源，
                            #   两者生命周期不同（G6 完整性 vs 观测成本）。
                            if _spans:
                                try:
                                    _hp_sid = _spans.open(
                                        "replan", "health-decide",
                                        parent=_run_sid,
                                        meta={"remaining": _hp_remain})
                                    if _hp_sid:
                                        _spans.close(_hp_sid, "ok",
                                                     _rh.health_meta(_hp_dec))
                                except Exception:
                                    pass
                            _write_decision("health", _hp_dec)
                            _hp_act = str(_hp_dec.get("action") or "none")
                            if _hp_act == "stop":
                                # M4 ⇒ S9 语义：该停，不重画。block 剩余任务后
                                # break（与预算护栏同口径，汇总如实记 partial）。
                                _m4_stopped = True
                                for _tk5 in _hp_tasks:
                                    if str(_tk5.get("status") or "") in ("planned",
                                                                         "ready"):
                                        TaskQueue.block(
                                            conn, int(_tk5["id"]),
                                            "预计耗时超时间预算（M4 巡检）")
                                yield {"type": "reasoning", "kind": "progress",
                                       "delta": "（自动编排）健康巡检 M4：%s"
                                                % (_hp_dec.get("reason") or "")[:100]}
                                break
                            elif _hp_act == "refine":
                                # S10 ⇒ T2-light 原地精修：把健康发现注入**剩余**
                                # 任务的 context（UPDATE，不追行、不改拓扑）。
                                _hp_applied = 0
                                for _tk6 in _hp_tasks:
                                    if str(_tk6.get("status") or "") not in ("planned",
                                                                             "ready"):
                                        continue
                                    try:
                                        _hp_ctx = str(_tk6.get("context") or "")
                                        _hp_note = (
                                            "\n【重规划 r1·巡检精修】%s。请按此调整"
                                            "后续执行重点。" %
                                            (_hp_dec.get("reason") or "")[:300])
                                        conn.execute(
                                            "UPDATE agent_tasks SET context=? "
                                            "WHERE id=?",
                                            ((_hp_ctx + _hp_note)[:1000],
                                             int(_tk6["id"])))
                                        _hp_applied += 1
                                    except Exception:
                                        pass
                                if _hp_applied:
                                    conn.commit()
                                    yield {"type": "reasoning", "kind": "progress",
                                           "delta": "（自动编排）健康巡检 S10：%s"
                                                    % (_hp_dec.get("reason") or "")[:100]}
                                    if _RS_OK:
                                        _pe5 = _rs.plan_revised_payload(
                                            {"action": "replan",
                                             "level": _rh.LEVEL_LIGHT,
                                             "reason": _hp_dec.get("reason") or ""},
                                            run_id=run_id)
                                        if _pe5:
                                            _pe5["updates"] = [
                                                str(t.get("task_key") or "")
                                                for t in _hp_tasks
                                                if str(t.get("status") or "")
                                                in ("planned", "ready")]
                                            yield _pe5
                                    continue
                    except Exception:
                        pass
                ready = TaskQueue.ready_tasks(conn, run_id)
                if not ready:
                    # ★ P1 replanning（2026-10-21，v2 §4.6）：收敛点决策记录。
                    #   有 pending 但无 ready ⇒ 剩余任务被 failed 的失效传播堵死。
                    #   P1 无重画能力（T2 差量重画属 P2，v2 §7），此处**恒 action=none**，
                    #   但 S5/S6/S7 信号与"为什么不重画"必须留痕（G6）。
                    if _RS_OK:
                        try:
                            _t_dec0 = _t.time()
                            _stall_tasks = TaskQueue.tasks(conn, run_id)
                            _stall_dec = _rs.decide_on_stall(done_items, _stall_tasks)
                            _dec_ms = int((_t.time() - _t_dec0) * 1000)
                            if _spans:
                                _pend = sum(1 for _t2 in _stall_tasks
                                            if _t2["status"] in ("planned", "ready"))
                                _rsid = _spans.open("replan", "stall-decide",
                                                    parent=_run_sid,
                                                    meta={"pending": _pend})
                                if _rsid:
                                    _spans.close(_rsid, "ok",
                                                 _rs.decision_meta(_stall_dec,
                                                                   latency_ms=_dec_ms))
                            # ★ P4：决策记录落 replan_decisions（与 span 同点）
                            _write_decision("stall", _stall_dec,
                                            latency_ms=_dec_ms)
                        except Exception:
                            pass
                    # ── P2 replanning（2026-10-21，v2 §4.10）：差量重画 ──
                    #   放在 P1 留痕之后、P0-3 全量 block 之前：**先试着局部修图，
                    #   修不了才走原来的"全部 block + break"**（原逻辑一行未改，
                    #   是本段 return/continue 之后的 fallback）。
                    #   ⚠️ 三条纪律：
                    #   ① 总闸 `replanning.enabled` 默认 False ⇒ `_redraw_dec` 恒 none，
                    #      本段整体是 no-op ⇒ **零行为变更**（P1 的默认行为逐字保持）；
                    #   ② 观测/救援旁路铁律：整段 try/except，异常只丢记录不阻断编排；
                    #   ③ 重画成功 ⇒ `continue`（下一轮 ready_tasks 自然出现新任务），
                    #      **不改循环结构**（v1 §4.3 纪律）。
                    _redraw_dec = None
                    if _RR_OK and _rp_enabled and _rp_on_stall:
                        try:
                            _rd_tasks = TaskQueue.tasks(conn, run_id)
                            _failed_rows = [t for t in _rd_tasks
                                            if str(t.get("status") or "") == "failed"]
                            if _failed_rows:
                                _t_rd0 = _t.time()
                                # 失败指纹历史（无进展检测用）。
                                #   ⚠️ **必须排除正在被判定的那一行**：历史表来自
                                #   当前 agent_tasks 快照，而失败任务本身就在快照里 ——
                                #   若把它算进历史，首次失败就会"自己匹配自己"
                                #   ⇒ 每次都误判无进展 ⇒ T2 永不生效（2026-10-21
                                #   行为探针 B/C 场景实测抓到的真 bug）。
                                #   正确口径：历史 = **别的**失败行（含同 key 的上一次
                                #   失败痕迹，由 T1 补跑写回的 retry_count 区分）。
                                _fp_hist = {}
                                for _t3 in _rd_tasks:
                                    _k3 = str(_t3.get("task_key") or "")
                                    if str(_t3.get("status") or "") == "failed" and _k3:
                                        try:
                                            _fp = "%s|%s" % (
                                                str(_t3.get("error_class") or ""),
                                                _rs.norm_error_text(
                                                    _t3.get("error") or ""))
                                            _fp_hist.setdefault(_k3, []).append(_fp)
                                        except Exception:
                                            pass
                                _cur_fail = None
                                if _rp_noprog and _failed_rows:
                                    _f0 = _failed_rows[0]
                                    _ck = str(_f0.get("task_key") or "")
                                    _cfp = "%s|%s" % (
                                        str(_f0.get("error_class") or ""),
                                        _rs.norm_error_text(_f0.get("error") or "")
                                        if _RS_OK else str(_f0.get("error") or ""))
                                    # 剔除自身这一次（末位同值项），再并入
                                    # **进程内重画台账**（同 key 原地重派 ⇒ 库快照
                                    # 只有一个 key 一行，历史必须来自台账）。
                                    _hist = list(_fp_hist.get(_ck) or [])
                                    if _hist and _hist[-1] == _cfp:
                                        _hist.pop()
                                    _hist.extend(list(_redraw_hist.get(_ck) or []))
                                    _fp_hist[_ck] = _hist
                                    _cur_fail = (_ck, str(_f0.get("error_class") or ""),
                                                 str(_f0.get("error") or ""))
                                _redraw_dec = _rr.plan_redraw(
                                    _rd_tasks,
                                    [str(t.get("task_key") or "") for t in _failed_rows],
                                    reason_by_key={str(t.get("task_key") or ""):
                                                   str(t.get("error") or "")[:200]
                                                   for t in _failed_rows},
                                    round_no=_redraw_rounds + 1,
                                    enabled=True,
                                    light=_rp_light and not _rp_heavy,
                                    skip_leaf=_rp_skip,
                                    max_rounds=_rp_rounds,
                                    expand_ratio=_rp_ratio,
                                    failed_history=_fp_hist,
                                    current_fail=_cur_fail,
                                    validate=TaskQueue.validate_plan)
                                _rd_ms = int((_t.time() - _t_rd0) * 1000)
                                # 决策记录（含 action=none 的"为什么不重画"）
                                _rd_gates = {}
                                if _spans:
                                    _rd_sid = _spans.open("replan", "redraw-decide",
                                                          parent=_run_sid,
                                                          meta={"failed": len(_failed_rows),
                                                                "round": _redraw_rounds + 1})
                                    if _rd_sid:
                                        _rd_meta = _rs.decision_meta(_redraw_dec,
                                                                    round_no=_redraw_rounds + 1,
                                                                    latency_ms=_rd_ms) \
                                            if _RS_OK else {"action": _redraw_dec.get("action"),
                                                            "level": _redraw_dec.get("level"),
                                                            "reason": _redraw_dec.get("reason"),
                                                            "signals": [], "round": _redraw_rounds + 1,
                                                            "tokens": 0, "latency_ms": _rd_ms}
                                        try:
                                            _rd_meta["gates"] = {g: ("pass" if v else "fail")
                                                                 for g, v in
                                                                 (_redraw_dec.get("gates") or {}).items()
                                                                 if g in ("Q1", "Q2", "Q3", "Q4", "Q5")}
                                        except Exception:
                                            pass
                                        _spans.close(_rd_sid, "ok", _rd_meta)
                                        _rd_gates = _rd_meta.get("gates") or {}
                                # ★ P4：决策记录落 replan_decisions（与 span 同点）
                                _write_decision("redraw", _redraw_dec,
                                                round_no=_redraw_rounds + 1,
                                                gates=_rd_gates, latency_ms=_rd_ms)

                                _rd_act = str(_redraw_dec.get("action") or "none")
                                if _rd_act == "replan":
                                    # ── P3 确认卡（v2 §4.8，默认关）──
                                    #   开启 `user_confirm_redraw` 时，重画**执行前**
                                    #   暂停并等用户应答；超时/非法 ⇒ 维持原计划继续。
                                    #   ⚠️ 默认关 ⇒ 本段整体 no-op（P2 行为逐字不变）。
                                    if _RCFM_OK and _rp_confirm and conversation_id:
                                        try:
                                            _cp = _rcfm.build_payload(
                                                _redraw_dec, run_id=run_id,
                                                round_no=_redraw_rounds + 1,
                                                ttl_sec=_rp_cttl)
                                            _cid6 = _rcfm.open_(conversation_id, _cp,
                                                                  ttl_sec=_rp_cttl)
                                            if _cid6:
                                                yield dict(_cp, confirm_id=_cid6)
                                                _rs6 = _rcfm.wait_for(
                                                    conversation_id, poll_s=1.0)
                                                if str(_rs6.get("action") or "") \
                                                        != _rcfm.ACTION_APPROVE:
                                                    # 维持原计划：不改图、不推进轮次，
                                                    #   只把"曾有一次重画被否决/超时"落留痕。
                                                    yield {"type": "reasoning",
                                                           "kind": "progress",
                                                           "delta": "（自动编排）重画未执行：%s"
                                                                    % (_rs6.get("reason") or "")}
                                                    _rd_act = "none"
                                                    _redraw_dec = None
                                                else:
                                                    yield {"type": "reasoning",
                                                           "kind": "progress",
                                                           "delta": "（自动编排）用户已批准重画"}
                                        except Exception:
                                            pass
                                if _rd_act == "replan":
                                    _applied = 0
                                    if _rp_light and not _rp_heavy:
                                        # T2-轻：**原地重派**（UPDATE，不追行 ——
                                        #   agent_tasks 对 (run_id, task_key) 无唯一约束，
                                        #   同 key 追行会让 ready_tasks 派发两次）。
                                        for _u in (_redraw_dec.get("updates") or []):
                                            try:
                                                TaskQueue.retry(conn, int(_u.get("id")))
                                                conn.execute(
                                                    "UPDATE agent_tasks SET context=?, "
                                                    "retry_count=COALESCE(retry_count,0)+1, "
                                                    "error='' WHERE id=?",
                                                    (str(_u.get("context") or "")[:1000],
                                                     int(_u.get("id"))))
                                                _t1_retried.add(str(_u.get("key") or ""))
                                                # 台账记账：把这次失败指纹存下来，
                                                # 使"同因第 2 次 ⇒ T4"在同 key 重派下真正可判
                                                _uk = str(_u.get("key") or "")
                                                try:
                                                    _redraw_hist.setdefault(_uk, []).append(
                                                        "%s|%s" % (
                                                            str(_failed_rows[0].get("error_class") or "")
                                                            if _failed_rows else "",
                                                            _rs.norm_error_text(
                                                                str(_failed_rows[0].get("error") or ""))
                                                            if _RS_OK else ""))
                                                except Exception:
                                                    pass
                                                _applied += 1
                                            except Exception:
                                                pass
                                        conn.commit()
                                    else:
                                        # T2-重：新 key 追行 + 下游 deps 重映射。
                                        #   顺序**先改 deps 再 create_plan**：deps 指向的
                                        #   新 key 此刻还不存在，但 `UPDATE` 不校验外键，
                                        #   而 `create_plan` 会过滤"计划外 key" —— 反序
                                        #   会把刚写的 t2_r1 依赖当场丢掉。
                                        try:
                                            for _kt in (_redraw_dec.get("kept") or []):
                                                _kk = str(_kt.get("key")
                                                          or _kt.get("task_key") or "")
                                                _nd = [str(d) for d in (_kt.get("deps") or [])]
                                                if not _kk:
                                                    continue
                                                conn.execute(
                                                    "UPDATE agent_tasks SET deps=? "
                                                    "WHERE run_id=? AND task_key=? "
                                                    "AND status IN ('planned','ready')",
                                                    (json.dumps(_nd, ensure_ascii=False),
                                                     run_id, _kk))
                                            conn.commit()
                                            TaskQueue.create_plan(
                                                conn, run_id,
                                                _redraw_dec.get("new_tasks") or [],
                                                assigned_by="replanner",
                                                conversation_id=conversation_id)
                                            _applied = len(_redraw_dec.get("new_tasks") or [])
                                        except Exception:
                                            _applied = 0
                                    if _applied:
                                        _redraw_rounds += 1
                                        # 用户可见：reasoning 进度 + subtask 重派轨迹
                                        yield {"type": "reasoning", "kind": "progress",
                                               "delta": "（自动编排）执行中重规划 %s：%s"
                                                        % (_redraw_dec.get("level"),
                                                           _redraw_dec.get("reason"))}
                                        for _k5 in (_redraw_dec.get("retry_keys") or []):
                                            yield {"type": "subtask", "key": _k5,
                                                   "status": "run",
                                                   "title": "重规划后重派", "agent": "",
                                                   "deps": [], "retry_count": 1}
                                        if _RS_OK:
                                            _pe = _rs.plan_revised_payload(_redraw_dec,
                                                                          run_id=run_id)
                                            if _pe:
                                                _pe["updates"] = [str(u.get("key") or "")
                                                          for u in (_redraw_dec.get("updates") or [])]
                                                _pe["new_tasks"] = [str(t.get("key") or "")
                                                                   for t in (_redraw_dec.get("new_tasks") or [])]
                                                _pe["dropped"] = [str(k) for k in
                                                                  (_redraw_dec.get("dropped") or [])]
                                                yield _pe
                                        continue        # 下一轮 ready_tasks 自然派发
                                elif _rd_act == "skip":
                                    # T1.5：叶子失效任务主动跳过（决策留痕，不改终态 ——
                                    #   failed 是终态，skip 只是"明确标记"而非状态变更）
                                    yield {"type": "reasoning", "kind": "progress",
                                           "delta": "（自动编排）%s" % (_redraw_dec.get("reason") or "")}
                        except Exception:
                            pass
                    # P0-3 补（2026-09-19，端到端取证）：被阻塞的子任务**必须进入轨迹**。
                    # 此前只 `block()` 不记账 → 「t2 失败 ⇒ t3/t4 阻塞」在卡片上完全不可见
                    # （plan 只剩已执行的 2 条，状态仍报 full）。此处与 budget_hit 分支同口径
                    # 补记 status="partial"（前端 `.rc` 子任务行按 partial 渲染为「部分完成」）。
                    for tk in TaskQueue.tasks(conn, run_id):
                        if tk["status"] in ("planned", "ready"):
                            TaskQueue.block(conn, tk["id"], "依赖任务失败，无法推进")
                            _rt = int(tk.get("retry_count") or 0)
                            orch_tasks.append({"key": tk.get("task_key"), "title": tk.get("title"),
                                               "agent": tk.get("agent_id"), "status": "partial",
                                               "latency_ms": 0, "retry_count": _rt,
                                               "summary_status": "partial"})
                            subtasks.append({"key": tk.get("task_key"), "title": tk.get("title"),
                                             "agent": tk.get("agent_id") or "", "deps": [],
                                             "status": "partial", "latency_ms": 0,
                                             "error": "依赖任务失败，无法推进",
                                             # ★ F-3：输入不展示；本步未执行故 output=None。
                                             "output": None,
                                             "retry_count": _rt, "summary_status": "partial"})
                    break
                # T8：并行执行全部就绪任务（≤_ORCH_MAX_WORKERS）；worker 只执行子管道收集事件，
                # 主线程统一落库 + SSE 转发。Task 7：worker 内再经 task_pool 独立线程跑子任务以支持超时。
                _deps_map = {}
                for tk in ready:
                    tkey = tk.get("task_key") or "t"
                    ttitle = tk.get("title") or tkey
                    _deps = tk.get("deps") or []
                    if isinstance(_deps, str):
                        try:
                            _deps = json.loads(_deps) or []
                        except Exception:
                            _deps = []
                    _deps_map[tkey] = [str(d) for d in _deps]
                    yield {"type": "subtask", "key": tkey, "status": "run", "title": ttitle,
                           "agent": tk.get("agent_id") or "", "deps": _deps_map[tkey],
                           # P1：补跑重派时该值 >0 ⇒ 前端 run 态即可见重试徽章
                           # （v1 §3.2「T1 用户可见性 = subtask 事件 retry_count+1」）
                           "retry_count": int(tk.get("retry_count") or 0)}
                    yield {"type": "agent", "status": "run", "name": tkey, "display_name": ttitle,
                           "intent": intent, "hil_level": hil_level}
                _exec_pool = _TPE(max_workers=min(len(ready), _max_workers))
                _task_pool = _TPE(max_workers=min(len(ready), _max_workers))
                # 2026-09-26（用户要求）：子任务内容**就地实时输出**，不再"最后统一输出"。
                #  旧实现：worker 把 token/reasoning/tool 攒进 `evs`，主循环在 future **完成后**才
                #  `for ev in evs: yield ev` —— 子任务执行期间界面一片空白，完成瞬间一次性涌出，
                #  用户看到的就是"所有子任务的总结堆在最后"。现在 worker 边产边入队，主循环边收边吐，
                #  事件自身的 `key`/`name` 前缀（`tkey:`）保证前端把它们归到**对应子任务的卡片**下。
                _evq = _queue.Queue()
                _hb = {}   # P0-7：子任务活性心跳表 {task_key: 最近产出时刻}（_worker 停滞超时用）
                _orch_offloads = {}   # 共享产物工作区：{task_key: (offload_id, 字符数)}（下游按 id 取全文）

                def _drain():
                    """取出队列里**已到达**的子任务事件；把**连续的 token 合并成一条**再吐。

                    2026-09-26（用户同意"按建议优化"）：实时转发让事件量随 token 数线性上涨
                    （实测一段两阶段编排有 89 条 token 事件）。前端已有 rAF 合帧，但**传输量**还在。
                    这里在服务端先合一次帧：同一 `(key, round)` 的连续 token 合并 `delta`，
                    协议与前端**都不用改**（前端本来就是"累加 delta 到正文"），延迟上限 = 主循环的
                    50ms 空转窗口（见下方 sleep），肉眼不可辨。
                    ⚠️ 只合并**签名相同**的连续 token：带 key/round 的事件混在一起时若跨签名合并，
                    会把某个子任务的内容算到另一个头上。
                    """
                    _now = []
                    try:
                        while True:
                            _now.append(_evq.get_nowait())
                    except _queue.Empty:
                        pass
                    _buf = None
                    for _e in _now:
                        if _e.get("type") == "token":
                            _sig = (_e.get("key"), _e.get("round"))
                            if _buf is None:
                                _buf = dict(_e); _buf["_sig"] = _sig
                            elif _buf["_sig"] == _sig:
                                _buf["delta"] = (_buf.get("delta") or "") + (_e.get("delta") or "")
                            else:
                                _buf.pop("_sig", None); yield _buf
                                _buf = dict(_e); _buf["_sig"] = _sig
                        else:
                            if _buf is not None:
                                _buf.pop("_sig", None); yield _buf; _buf = None
                            yield _e
                    if _buf is not None:
                        _buf.pop("_sig", None); yield _buf

                try:
                    _futs = {_submit_with_ctx(_exec_pool, _worker, tk, _task_pool): tk for tk in ready}
                    _pending = set(_futs)
                    while _pending:
                        # ① 先把已入队的子任务事件实时吐出去（合帧后吐；这是"就地输出"的关键）
                        for _e in _drain():
                            yield _e
                        # ② 再收已完成的 future；本轮没有完成项就短睡让出 CPU
                        _done_now = [f for f in _pending if f.done()]
                        if not _done_now:
                            _t.sleep(0.05)
                            continue
                        for _fut in _done_now:
                            _pending.discard(_fut)
                            tk = _futs[_fut]
                            tkey, ttitle, tk_ok, sub_res, evs, _t_err, _t_lat, _tok, _summary, _off_id = _fut.result()
                            if _off_id:
                                # 共享产物工作区：run 级 {task_key: (offload_id, 字符数)}，
                                # 后续派发的下游子任务在上下文快照里拿到引用（见 build_subtask_context）
                                _orch_offloads[tkey] = (_off_id, len(str(sub_res or "")))
                            # ⚠️ 这里**不再** `for ev in evs: yield ev`（那些事件已由 ① 实时吐出；
                            #    再吐一遍会重复渲染）。`evs` 仍随返回值带出，仅供调试/兼容。
                            if tk_ok:
                                TaskQueue.complete(conn, tk["id"], sub_res or "（子任务已产出流式内容）",
                                                   {"summary": sub_res[:300], "score": len(sub_res)},
                                                   latency_ms=_t_lat)
                                # Task 9：上游快照携带协议摘要（partial 以 summary.status 标记；Task 10 汇总消费）
                                done_items.append({"task_key": tkey, "title": ttitle,
                                                   "agent_id": tk.get("agent_id"), "result": sub_res,
                                                   "status": "done",
                                                   "summary": _summary,
                                                   "result_offload_id": _off_id or 0,
                                                   "proto_status": (_summary or {}).get("status")})
                                # ★ P4：M4 滑动窗口的耗时样本（只收 done 的 ——
                                #   失败任务的耗时长尾含超时，会把"平均耗时"抬成
                                #   噪声；口径同基线报告的 latency 统计）。
                                try:
                                    _done_lats.append(int(_t_lat or 0))
                                except Exception:
                                    pass
                            else:
                                TaskQueue.fail(conn, tk["id"], _t_err or "子任务未产生输出")
                                # ★ P1 replanning（2026-10-21，v2 §4.6）：任务失败点
                                #   判定 + 决策记录。每次失败落一条 kind=replan span
                                #   （**含 action=none** —— "为什么不补跑"与"为什么补跑"
                                #   同等重要，对标 OpenAI DR 停止原因显式 log）；T1 命中
                                #   则同 key 补跑一次（S3'：timeout/transient/internal，
                                #   排除 interrupted —— 真库 50/58 失败是 reap_stuck_runs
                                #   孤儿标记、run 已终止归 resume，补跑无意义）。
                                #   观测旁路铁律：整段 try/except，失败只丢记录不阻断。
                                if _RS_OK:
                                    try:
                                        _t_dec0 = _t.time()
                                        _ecls_m = (_fc_main.classify(_t_err, ok=False)
                                                   if _FC_MAIN_OK else "internal")
                                        _dec = _rs.decide_on_failure(
                                            tkey, _t_err, _ecls_m,
                                            retry_once=_retry_once,
                                            already_t1=(tkey in _t1_retried))
                                        if (_dec.get("action") == "retry"
                                                and _dec.get("retry_keys")):
                                            # 同 key 补跑一次：failed→ready + retry_count+1。
                                            # release_deps **不调**（任务未完成）；下轮迭代
                                            # ready_tasks 自然重新派发（done/failed 事件带
                                            # 新 retry_count ⇒ 前端徽章可见，v1 §3.2 口径）。
                                            TaskQueue.retry(conn, tk["id"])
                                            try:
                                                # ★ 原子自增而非读-改-写：worker 与主循环
                                                #   共享同一 tk dict（ready_tasks 返 dict、
                                                #   按引用进 future），读到的值通常已同步，
                                                #   但该共享是**隐式**契约 —— 原子 SQL 让
                                                #   计数不依赖它（实测：3 连败场景
                                                #   worker 重试×2 + T1×1 = retry_count 3）。
                                                conn.execute(
                                                    "UPDATE agent_tasks SET retry_count="
                                                    "COALESCE(retry_count,0)+1 WHERE id=?",
                                                    (tk["id"],))
                                                conn.commit()
                                            except Exception:
                                                pass
                                            _t1_retried.add(tkey)
                                            yield {"type": "progress", "kind": "progress",
                                                   "delta": "（自动编排）%s 失败为可重试类（%s），自动补跑一次"
                                                   % (ttitle, _ecls_m)}
                                        _dec_ms = int((_t.time() - _t_dec0) * 1000)
                                        if _spans:
                                            _rsid = _spans.open(
                                                "replan", "t1-decide:%s" % tkey,
                                                parent=_run_sid, meta={"task": tkey})
                                            if _rsid:
                                                _spans.close(
                                                    _rsid, "ok",
                                                    _rs.decision_meta(_dec, latency_ms=_dec_ms))
                                        # ★ P4：决策记录落 replan_decisions（与 span 同点）
                                        _write_decision("t1_fail", _dec,
                                                        latency_ms=_dec_ms)
                                    except Exception:
                                        pass
                            # Task 7：子任务 token 记账写回 agent_tasks.token_count + run 累计
                            if _tok:
                                try:
                                    conn.execute("UPDATE agent_tasks SET token_count=? WHERE id=?",
                                                 (_tok, tk["id"]))
                                    conn.commit()
                                except Exception:
                                    pass
                                run_tokens += _tok
                            executed_count += 1
                            TaskQueue.release_deps(conn, run_id, tkey)
                            # Task 14：子任务元数据（重试次数 + 协议摘要 status）供前端徽章展示
                            _rc = int(tk.get("retry_count") or 0)
                            _sm_status = (_summary or {}).get("status") if isinstance(_summary, dict) else None
                            # 2026-09-26：**给用户看的一句话摘要**（与上面喂给下游的 `summary` 分开）。
                            #  原先前端直接把 `summary.summary`（= 子 Agent 输出原文前 N 字）显示在卡片上，
                            #  而那段开头是"一、对上游意图识别结果的承接"这类**内部交接语**，还被硬截 60 字。
                            #  这里只换"展示那份"，`done_items` 里的协议摘要原样不动（下游承接靠它）。
                            try:
                                from services.subtask_protocol import ui_summary as _ui_sum
                                _ui_summary = _ui_sum(sub_res)
                            except Exception:
                                _ui_summary = ""
                            orch_tasks.append({"key": tkey, "title": ttitle,
                                               "agent": tk.get("agent_id"),
                                               "status": "done" if tk_ok else "failed",
                                               "latency_ms": _t_lat,
                                               "token_count": _tok,
                                               "retry_count": _rc,
                                               "summary": _summary if isinstance(_summary, dict) else None,
                                               "ui_summary": _ui_summary,
                                               "summary_status": _sm_status})
                            # P0-2：子任务轨迹实时事件（done/failed）+ 轨迹持久化
                            # ★ E-1（2026-10-14 米爸问「步骤节点有显示具体执行内容、
                            #   输入输出吗」）：补 `input` / `output` 两个字段。
                            #   实测（会话 9360 / run535，5 个子任务）：
                            #     · 完整产出 `agent_tasks.result` 合计 **36,696 字**
                            #       （t1=9021 / t2=9900 / t3=6176 / t4=7015 / t5=4584）
                            #     · 而 subtasks 只带了 `ui_summary` ⇒ 界面**仅展示 214 字（0.6%）**
                            #     · 输入 `agent_tasks.context` 合计 357 字，**完全没进 card_data**
                            #   ⇒ 中间层「每个子任务自己产出了什么」既没进 card_data、也没进正文，
                            #     用户看不到执行过程，也无法把正文内容回溯到具体环节。
                            #   修法：把 input/output 一并带上（tk 来自 `SELECT *`，两列都在）。
                            #   ⚠️ `output` **不截断** —— 用户明确要求"点开能看完整产出"；
                            #      截断会让"查看完整产出"变成看不全，反而不如不给。
                            #      代价是 card_data 变大（实测单条约 +7~10KB），SQLite 无压力。
                            subtasks.append({"key": tkey, "title": ttitle, "agent": tk.get("agent_id") or "",
                                             "deps": _deps_map.get(tkey, []),
                                             "status": "done" if tk_ok else "failed",
                                             # 落库这份也带用户向摘要：否则**刷新历史会话**时前端只能回退到
                                             # 内部交接语（老数据显示"承接上游…"会让人以为没改）
                                             "ui_summary": _ui_summary,
                                             # ★ F-3：只传输出。米爸明确「只显示输出结果，输入暂不
                                             #   展示（工具执行除外）」⇒ 不再往 card_data 塞 input。
                                             #   （E-1 那版传了 input，已被本条覆盖。）
                                             "output": (sub_res or "").strip() or None,
                                             "latency_ms": _t_lat, "error": _t_err or None,
                                             "token_count": _tok,
                                             "retry_count": _rc,
                                             "summary": _summary if isinstance(_summary, dict) else None,
                                             "summary_status": _sm_status})
                            yield {"type": "subtask", "key": tkey, "status": "done" if tk_ok else "failed",
                                   "title": ttitle, "agent": tk.get("agent_id") or "",
                                   "latency_ms": _t_lat, "error": _t_err or None,
                                   "token_count": _tok,
                                   "retry_count": _rc,
                                   "summary": _summary if isinstance(_summary, dict) else None,
                                   # ⚠️ 时间线卡片读的是**这个事件**（不是 orch_tasks）——2026-09-26 首版把
                                   #    ui_summary 只加在 orch_tasks 上，真机验收时事件里根本没这个键（实测踩到）。
                                   #    展示用"面向用户的一句"，协议 summary 同时保留给下游承接。
                                   "ui_summary": _ui_summary,
                                   # ★ F-3：实时事件同样只带 output（输入不展示）——
                                   #   否则实时播放与刷新后历史路径的数据集不一致。
                                   "output": (sub_res or "").strip() or None,
                                   "summary_status": _sm_status}
                            yield {"type": "agent", "status": "done", "name": tkey, "display_name": ttitle,
                                   "intent": intent, "hil_level": hil_level}
                    # 收尾 drain：最后一个子任务可能在"检测到完成"之后仍有事件入队（极小竞态），
                    # 不吐干净会丢尾部输出 —— 用户抱怨的"放在最后统一输出"不能变成"干脆不输出"。
                    # （同样走 _drain() 合帧，避免收尾又冒出一串单 token 事件）
                    for _e in _drain():
                        yield _e
                finally:
                    # Task 7：task_pool 不等待超时后台线程（wait=False）；exec_pool 线程均已返回
                    try:
                        _task_pool.shutdown(wait=False)
                    except Exception:
                        pass
                    try:
                        _exec_pool.shutdown(wait=True)
                    except Exception:
                        pass
            # Task 7：预算护栏——未执行任务在汇总轨迹中记为 partial（含 blocked 原因）
            if budget_hit:
                try:
                    for _tk in TaskQueue.tasks(conn, run_id):
                        if _tk["status"] == "blocked" and (_tk.get("error") or "") == "预算超限":
                            orch_tasks.append({"key": _tk.get("task_key"), "title": _tk.get("title"),
                                               "agent": _tk.get("agent_id"),
                                               "status": "partial", "latency_ms": 0,
                                               "retry_count": int(_tk.get("retry_count") or 0),
                                               "summary_status": "partial"})
                            subtasks.append({"key": _tk.get("task_key"), "title": _tk.get("title"),
                                             "agent": _tk.get("agent_id") or "", "deps": [],
                                             "status": "partial", "latency_ms": 0, "error": "预算超限",
                                             # ★ F-3：输入不展示；预算超限未执行故 output=None。
                                             "output": None,
                                             "retry_count": int(_tk.get("retry_count") or 0),
                                             "summary_status": "partial"})
                except Exception:
                    pass
            # 4) 汇总（P1-1 总结节点）
            # P0-2：phase=summarizing —— 区分"任务执行中崩"与"汇总中崩"：
            #   后者所有子任务都已 done，**不该重跑任务**，只需重做汇总。
            try:
                from agent.orch_checkpoint import touch as _ck_sm
                _ck_sm(conn, run_id, "summarizing")
            except Exception:
                pass
            # T8：编排汇总前批量挂写操作确认队列——子任务暂存的写请求统一转人工确认（失败不阻断汇总）
            _queued_write = 0
            try:
                from services.artifact_materializer import batch_queue_confirmations
                _qconn = get_db()
                try:
                    _queued_write = batch_queue_confirmations(
                        _qconn, run_id, conversation_id=int(conversation_id or 0))
                finally:
                    _qconn.close()
            except Exception:
                _queued_write = 0
            if _queued_write:
                yield {"type": "progress", "kind": "progress",
                       "delta": f"（自动编排）{_queued_write} 个写操作进入人工确认队列，确认后统一生效"}
            # Task 10：汇总消费结构化摘要——agg_status 三态聚合（全 full → full；有 partial/failed → partial）
            # + 构造汇总输入：优先用协议 summary（summary 文本 + artifacts 标题 + evidence）组织每节；
            #   无 summary 的子任务（旧数据）回退原 result 文本（兼容）。失败不阻断汇总。
            _agg_status, _missing_keys, _sum_items = "full", [], list(done_items)
            try:
                from services import subtask_protocol as _sp
                _summaries = []
                for _it in done_items:
                    _sm = _it.get("summary")
                    if isinstance(_sm, dict) and _sm.get("status") in _sp.STATUS_VALUES:
                        _summaries.append(_sm)
                    else:  # 旧数据（无协议 summary）→ normalize_text 兼容解析
                        try:
                            # ★ 2026-10-13（B1-2）：传入该任务的**执行状态**，
                            #   让 normalize_text 能把"已知失败但正文非空"也判 failed。
                            #   _it.get("status") 是 agent_tasks.status 的同名字段（done_items 元素）。
                            _summaries.append(_sp.normalize_text(
                                _it.get("result"), task_status=_it.get("status")))
                        except Exception:
                            _summaries.append(dict(_sp.DEFAULT_SUMMARY))  # 保守：视作 partial
                if _summaries:
                    _agg_status = _sp.summarize_status(_summaries)
                _missing_keys = [_it["task_key"] for _it in done_items
                                 if (isinstance(_it.get("summary"), dict)
                                     and _it["summary"].get("status") in ("partial", "failed"))]
                _sum_items = []
                for _it in done_items:
                    _sm = _it.get("summary")
                    if isinstance(_sm, dict) and str(_sm.get("summary") or "").strip():
                        _sec = _sm.get("summary")
                        _arts = _sm.get("artifacts") or []
                        if _arts:
                            _sec += "\n交付物：" + "；".join(
                                f"{a.get('kind')}《{a.get('title')}》({a.get('ref')})" for a in _arts[:10])
                            # ★ 2026-10-14（#17）：把**结构体检未通过**的交付物显式带出来。
                            #   目的与 basis/claims 一致：**呈现而非过滤** —— 用户要知道
                            #   哪份报告"没有结论段"、哪条需求"不可测"，而不是收到一份
                            #   看起来完整、实则没法验收的东西。
                            #   ⚠️ 校验器不可用（degraded / checked=0）时**一个字都不加**。
                            try:
                                from services.artifact_validator import (
                                    validate_artifact as _av2)
                                _weak = []
                                # ★ 2026-10-14 通用性修正：协议里的 artifact 条目
                                #   **没有正文字段**（只有 kind/title/ref）⇒
                                #   直接送进校验器会**每条都报「内容为空」**（假红）。
                                #   ⇒ 回落到该子任务的**完整交付物正文**。
                                for _a in _arts[:10]:
                                    _ak2 = str(_a.get("kind") or "")
                                    _src2 = _a
                                    if not any(str(_a.get(k) or "").strip()
                                               for k in ("content", "text", "body",
                                                         "markdown", "value")):
                                        _src2 = dict(_a)
                                        _bd = str(_it.get("result") or "").strip()
                                        if _bd:
                                            _src2["content"] = _bd
                                    _r2 = _av2(_ak2, _src2)
                                    if int(_r2.get("checked") or 0) > 0 and not _r2.get("ok"):
                                        # ⚠️ 「无正文可校验」是**不可判定**，不是缺陷 ——
                                        #   不得报成"结构体检未通过"（那会让用户误以为格式错）
                                        _iss = _r2.get("issues") or []
                                        if _iss and "**内容为空**" not in _iss[0]:
                                            _weak.append("%s《%s》：%s"
                                                         % (_ak2, _a.get("title"), _iss[0]))
                                if _weak:
                                    _sec += ("\n⚠️ 交付物结构体检未通过（格式问题，不影响内容）："
                                             + "；".join(_weak[:3]))
                            except Exception:
                                pass
                        _evs = _sm.get("evidence") or []
                        if _evs:
                            # ★ 2026-10-13 §08：按 basis 分档呈现，**不给引用的排在前面**。
                            #   目的不是"过滤掉低可信的"（那是拿未经验证的标签决定内容去留），
                            #   而是让读者**一眼看出哪些结论可回查、哪些是模型断言**。
                            #   顺序：verified → derived → asserted；组内保持原序。
                            _ORD = {"verified": 0, "derived": 1, "asserted": 2}
                            _ev_sorted = sorted(
                                _evs, key=lambda e: _ORD.get(str(e.get("basis") or "asserted"), 2))
                            _sec += "\n证据：" + "；".join(
                                "%s:%s" % (e.get("source"), e.get("ref"))
                                + ("[%s]" % e.get("basis") if e.get("basis") else "[无basis]")
                                for e in _ev_sorted[:10])
                            # 分档小结：让"有 N 条无引用断言"这类风险**在汇总里显形**，
                            # 而不是埋进 JSON 里等人自己去翻。
                            _nb = sum(1 for e in _evs
                                      if str(e.get("basis") or "asserted") == "asserted")
                            if _nb and _nb >= len(_evs):
                                _sec += ("\n⚠️ 上述依据均为**模型断言（无可复核引用）**，"
                                         "请勿直接作为验收依据")
                            elif _nb:
                                _sec += "\n（其中 %d 条为无引用断言）" % _nb
                        _rks = _sm.get("risks") or []
                        if _rks:
                            _sec += "\n风险/待确认：" + "；".join(
                                f"[{r.get('level')}]{r.get('title')}" for r in _rks[:5])
                        # ★ 2026-10-14（批次三 #16）：关键结论分档呈现。
                        #   目的与 evidence 的 basis 一致：**呈现而非过滤** ——
                        #   `to_verify` 是模型主动声明"这条我没底"，把它显式带出来，
                        #   用户才知道哪几条不能直接拿去验收。
                        #   ⚠️ 无 claims 时**一字不加**（提示词/正文逐字节不变）。
                        _cls_ = _sm.get("claims") or []
                        if _cls_:
                            _KLAB = {"fact": "已核实", "inference": "推断",
                                     "to_verify": "待验证"}
                            _ORDC = {"fact": 0, "inference": 1, "to_verify": 2}
                            _cs = sorted(_cls_, key=lambda c: _ORDC.get(
                                str(c.get("kind") or "to_verify"), 2))
                            _sec += "\n关键结论：" + "；".join(
                                "%s%s" % (
                                    "【%s】" % _KLAB.get(str(c.get("kind") or "to_verify"),
                                                        "待验证"),
                                    (("[%s] " % c["subject"]) if c.get("subject") else "")
                                    + str(c.get("text") or "")[:80])
                                for c in _cs[:6])
                            _ntv = sum(1 for c in _cls_
                                       if str(c.get("kind") or "to_verify") == "to_verify")
                            if _ntv:
                                _sec += "\n（其中 %d 条为**待验证**结论，引用前请自行核实）" % _ntv
                        # 2026-09-19 conv 370 实测：原实现**用摘要替换**完整交付物 → 汇总只看到
                        # ~250 字符/子任务（`plan_summary` 实测输入仅 1,072 tokens，而 t1/t2/t3
                        # 交付物原文合计 12,998 字符）→ 报告只能如实写「交付物在约束条件处截断」。
                        # 摘要是**结构化索引**（状态/风险/证据/交付物清单），不该取代交付物正文 ——
                        # 改为「摘要 + 完整交付物」，超长由 `_head_tail_clip` 按预算统一头尾采样。
                        _body = str(_it.get("result") or "").strip()
                        _mit = dict(_it)
                        _mit["result"] = (_sec + ("\n\n【完整交付物】\n" + _body if _body else ""))
                        _sum_items.append(_mit)
                    else:
                        _sum_items.append(_it)  # 兼容旧数据：回退原 result 文本
            except Exception:
                _agg_status, _missing_keys, _sum_items = "full", [], list(done_items)
            # ── P0-3 补（2026-09-19，端到端取证）：**未完成子任务必须反映到聚合状态** ──
            # 上一段只统计 `done_items`（成功交付物）→ 失败/阻塞的子任务被整体忽略。
            # 实测（会话 360，4 子任务）：t2 超时两次 → failed，t3/t4 依赖失败 → blocked，
            # 卡片却报 `orchestrated_status="full"`、`quality_gate_gaps=[]` ——
            # 与「失败被记成成功」完全同类，只是发生在「子任务失败」这一支而非「反思未通过」那一支。
            # 此处以本轮 `orch_tasks`（done/failed/partial 全量）为准做「只降不升」修正，
            # 并把未完成子任务并入缺失清单 → 汇总文本与卡片徽章同步可见。
            _st_all = [str(t.get("status") or "") for t in orch_tasks]
            if _st_all:
                if any(s == "failed" for s in _st_all):
                    _hard = "partial" if any(s == "done" for s in _st_all) else "failed"
                elif any(s == "partial" for s in _st_all):
                    _hard = "partial"
                else:
                    _hard = "full"
                _rank = {"full": 2, "partial": 1, "failed": 0}
                if _rank.get(_hard, 2) < _rank.get(_agg_status, 2):
                    _agg_status = _hard
                # ⚠️ 循环变量**必须**避开 `_t`：本函数顶部有 `import time as _t`（L26），
                # 下方还用 `_t.time()` 算 latency。曾用 `for _t in orch_tasks` 把 `_t` 重绑成
                # 任务字典 → 函数尾部 `_t.time()` 抛 AttributeError: 'dict' object has no attribute 'time'
                # → 被外层 except 兜住静默回落单 Agent（编排跑了 543s 却落单 Agent 卡片）。
                # 实测 2026-09-19（会话 362）；静态守卫 = tools/verify/verify_orchestration_e2e.py 的 G0
                # （`--static-only`，live/check-only 每次自动先跑）。
                # ★ P4：`skipped`（提前收敛主动放弃）**不算缺失** —— 它是
                # "没必要做"，不是"没做成"（early_stop 的设计核心，见上）。
                for _tk in orch_tasks:
                    if (_tk.get("status") not in ("done", "skipped")
                            and _tk.get("key") not in _missing_keys):
                        _missing_keys.append(_tk.get("key"))
            # ★ Task #43：summary span —— 覆盖「三态聚合构造 + LLM 汇总 + 预算标注」。
            #   这是单次 LLM 调用里输入最大的一段（_summarize_plan 吃全部交付物），
            #   慢/贵主要都在这里，没有 span 就只能从事件流秒表反推。
            #   parent=_run_sid：后段（summary/reflection/views）在时间上被 run span
            #   覆盖（run 收口于最末 done yield 前）⇒ 挂 run 下，total_ms=plan+run
            #   才等于端到端墙钟；与 run 并列成根会把后段双算一遍（e2e 实测 777.8s
            #   vs 实际 648.3s）。
            _sum_sid = _spans.open("summary", "汇总", parent=_run_sid) if _spans else 0
            try:
                from workflows import FlowExecutor as _FE
                if len(done_items) >= 2:
                    orch_content = _FE()._summarize_plan(_sum_items, plan, provider_id)
                else:
                    orch_content = "\n\n".join(
                        f"## {it.get('title')}\n{(it.get('result') or '').strip()}" for it in done_items) \
                        or "（计划已执行，但无成功交付物）"
            except Exception:
                # 汇总不可用（异常）→ 拼接降级。2026-09-20：原硬编码 `[:400]` 会把每个交付物
                # 砍到 **400 字符**（比正常路径的 500 还狠），与"交付物必须带结论"直接冲突。
                # 改为复用正常路径同一套预算（`_summarize_item_budget` + 头尾采样），
                # 保证「LLM 汇总失败」这条降级路的交付质量不塌方。
                try:
                    from workflows.planner import FlowPlannerMixin as _FPM
                    _fb_cap = _FPM._summarize_item_budget(len(done_items))
                    _fb_clip = _FPM._head_tail_clip
                except Exception:
                    _fb_cap, _fb_clip = 1600, None
                if _fb_clip:
                    orch_content = "\n\n".join(
                        f"## {it.get('title')}\n{_fb_clip(it.get('result') or '', _fb_cap)}"
                        for it in done_items)
                else:
                    # 仅当 `workflows.planner` 整体不可导入（近乎不可达）时走到这里。
                    # 仍复用同一预算值，**不留任何独立的硬编码小上限**（原先此处是 `[:400]`，
                    # 比正常路径 500 还狠 —— 留着就会成为下一轮"交付物缺结论"的隐患源）。
                    _fbc = _fb_cap if _fb_cap > 0 else 10 ** 9
                    orch_content = "\n\n".join(
                        f"## {it.get('title')}\n{(it.get('result') or '')[:_fbc]}" for it in done_items)
            # Task 7：预算受限 → 汇总内容标注 partial（前端可识别「部分子任务未执行」）
            if budget_hit:
                orch_content += "\n\n（因预算限制部分子任务未执行）"
            # ★ P4：提前收敛 ⇒ 汇总内容显式标注（"主动战略放弃"≠"没做完"——
            #   与 partial 的区分是 early_stop 的设计核心：用户要知道剩下的
            #   任务是**没必要做**，不是**没做成**）。
            if _early_stopped:
                orch_content += ("\n\n（目标已达成提前收敛：必需产出全部完成，"
                                 "剩余 %d 个非必需任务主动放弃）"
                                 % len(_es_cancel_keys))
            # ★ P4：M4 巡检停手 ⇒ 与预算护栏同口径标注（S9 语义）。
            if _m4_stopped:
                orch_content += ("\n\n（健康巡检：预计剩余耗时超时间预算，"
                                 "未派发子任务已标记，提前进入汇总）")
            if _spans and _sum_sid:
                _spans.close(_sum_sid, "ok", {"items": len(done_items),
                                              "agg_status": _agg_status})
            # Task 10：agg_status != full → 显式标注「编排部分完成」+ reasoning 事件（状态同时写入 card_data）
            # ★ 2026-10-20（用户报"编排大量失败"，实查会话 9565）：**partial ≠ 缺失/失败**。
            #   旧文案把 `_missing_keys`（= 自报 partial + failed 的并集）一律写成
            #   「N 个交付物缺失/失败」——真库实测 t1—t5 全部自报 partial
            #   （交付物**已产出**：artifacts 齐全、正文完整存库，只是存在未验证环节：
            #   乱码未挂接 / 未调校验器 / 知识库未命中），质量评审 LLM 照抄头部文案，
            #   写出"编排显示 t1—t5 五个交付物缺失/失败"的缺口报告——交付物明明都在，
            #   用户看到的就是"编排大量失败"。措辞必须区分三态：failed 才叫失败，
            #   partial 是"部分完成（存在未验证环节）"，文案与 card_data 同口径。
            # ═══════════════════════════════════════════════════════════════════
            # ★ 2026-10-22（米爸「是否重跑需要一个健全的机制」）：
            #   partial **病因分类 + 自动补齐**（默认开）。
            #
            # 为什么不能"partial 就重跑"（真数据取证，会话 9587 的 6 个子任务）：
            #   · t1/t2 自报 partial，但 risks.advice 是「请用户明确方法论 / 补充规格书」
            #     ⇒ 缺的是**用户输入**，重跑必然得到同一份结果（blocked，不补）；
            #   · t3/t5 自报 partial，真实原因是「工具轮次耗尽，sysml_v2_validate 一次未调用」
            #     ⇒ 执行中断 + **有靶子**（补跑只需跑一次校验），能改变结果（transient，补）。
            #   ⇒ 判准不是"partial 多严重"，而是**"有没有靶子"**。6 个里只需补 2 个，
            #     省掉 4 个无效重跑的 token。
            #
            # 标杆：Claude Code「stop_reason 七值 → 五种处置」（先分类再动作）+ Stop hook 否决权；
            #       LangGraph pending writes（已完成一律复用）；AutoGPT boundedness（每维有天花板）。
            # 四道闸：子任务 ≤2 次（D3）/ run ≤5 次 / token 预算 / 墙钟 + 空转。
            # 整段 try 兜住：补齐失败只丢补齐，**绝不阻断汇总**。
            # ═══════════════════════════════════════════════════════════════════
            _fill_verdicts, _fill_filled, _fill_patch = [], [], {}
            try:
                from core.config import get as _cfg_fl
                from agent import partial_policy as _pp
                _fl_on = True
                try:
                    _fl_on = bool(_cfg_fl("agent", "fill_enabled", True))
                except Exception:
                    _fl_on = True
                if _agg_status != "full" and _fl_on:
                    # ① 收集未完成子任务：done_items 里自报非 full 的 ＋ orch_tasks 里没跑完的
                    _inc, _seen_k = [], set()
                    for _it in done_items:
                        _sm_f = _it.get("summary")
                        _st_f = (_sm_f.get("status") if isinstance(_sm_f, dict) else None) \
                                or _it.get("proto_status")
                        if _st_f in ("partial", "failed"):
                            _inc.append(_it)
                            _seen_k.add(str(_it.get("task_key") or ""))
                    for _t_f in orch_tasks:
                        _k_f = str(_t_f.get("key") or "")
                        if str(_t_f.get("status") or "") not in ("done", "skipped") and _k_f not in _seen_k:
                            _inc.append({"key": _k_f, "title": _t_f.get("title"),
                                         "status": _t_f.get("status")})
                    # ② 分类（判据优先级：外部锚 > 自报）
                    _fill_verdicts = [_pp.classify_partial(x) for x in _inc
                                      if (x.get("key") or x.get("task_key"))]
                    if _fill_verdicts:
                        _fc = _pp.summarize_causes(_fill_verdicts)
                        # ★ evidence_thin：无 missing 也无 risks 的项，其分类是**保守兜底**
                        #   而非确有证据。必须显式说出来 —— 否则用户会以为"系统查过了，
                        #   确实只是质量不足"，实际是"判据不够，只能按最安全的口径判"。
                        _thin = sum(1 for _v_t in _fill_verdicts
                                    if _v_t.get("evidence_thin"))
                        yield {"type": "progress", "kind": "progress",
                               "delta": ("（自动编排）未完成子任务病因分类：执行中断 %d / 客观缺口 %d / "
                                         "待用户输入 %d / 质量不足 %d%s"
                                         % (_fc["transient"], _fc["gap"],
                                            _fc["blocked"], _fc["quality"],
                                            ("（其中 %d 项无 missing/risks，按最保守口径判定）"
                                             % _thin) if _thin else ""))}
                    # ③ 决策（四道闸 + 下游覆盖闸）
                    try:
                        _mp_t = int(_cfg_fl("agent", "fill_max_per_task", 2))
                    except Exception:
                        _mp_t = 2
                    try:
                        _mx_t = int(_cfg_fl("agent", "fill_max_tasks", 2))
                    except Exception:
                        _mx_t = 2
                    # 先取任务行：既供决策算"下游覆盖"，也供后面补跑复用（只查一次）
                    _rows_f = {}
                    try:
                        for _t_r in TaskQueue.tasks(conn, run_id):
                            _rows_f[str(_t_r.get("task_key") or "")] = _t_r
                    except Exception:
                        _rows_f = {}
                    # ★ 下游覆盖闸（真机实测 2026-10-22）：t2「生成 BDD 骨架」自报 partial
                    #   「本轮未调用校验器」，而 t3 的任务定义就是「运行 sysml_v2_validate 并修复」——
                    #   t2 的"未校验"是**编排设计上交给下游的一步**，补跑 t2 = 重复劳动
                    #   （实测单次 250s + 全量 token，产出还与 t3 重复）。
                    #   只对 gap 生效：transient（中断）即使有下游也要补，否则下游仍拿不到产出。
                    _covered_f = set()
                    try:
                        for _k_c, _t_c in _rows_f.items():
                            if str(_t_c.get("status") or "") != "done":
                                continue
                            try:
                                _deps_c = json.loads(_t_c.get("deps") or "[]")
                            except Exception:
                                _deps_c = []
                            for _d_c in (_deps_c or []):
                                _covered_f.add(str(_d_c))
                    except Exception:
                        _covered_f = set()
                    # ★ 子任务级 attempt 记账（2026-10-22，D5 落地，**零表结构改动**）：
                    #   直接复用既有 `agent_tasks.retry_count` —— 该字段已存在且 replanning(T1)
                    #   就在用它。语义统一为「这个子任务总共被重试过几次」：
                    #     · 好处：补齐与 replanning 共享同一个天花板，不会出现
                    #       「replanning 补 1 次 + 补齐再补 2 次」的合计超限；
                    #     · 代价：若 replanning 已补过，本轮的补齐次数会相应减少 —— 这正是收敛的本意。
                    _used_per_task = {}
                    for _k_u, _t_u in _rows_f.items():
                        try:
                            _used_per_task[_k_u] = int(_t_u.get("retry_count") or 0)
                        except Exception:
                            _used_per_task[_k_u] = 0
                    _dec_f = _pp.decide_fill(_fill_verdicts, used_run=0,
                                             max_per_task=_mp_t, max_tasks=_mx_t,
                                             used_per_task=_used_per_task,
                                             covered=_covered_f)
                    _fill_stop = _dec_f.get("stop") or ""
                    _plan_f = _dec_f.get("fill") or []
                    # 被下游覆盖/闸门挡下的，也要在进度里说清（P5：不许静默）
                    for _s_f in (_dec_f.get("skip") or []):
                        if "下游" in str(_s_f.get("reason") or ""):
                            yield {"type": "progress", "kind": "progress",
                                   "delta": "（自动编排）%s 不补：%s"
                                            % (_s_f.get("key"), _s_f.get("reason"))}
                    if _fill_stop:
                        yield {"type": "progress", "kind": "progress",
                               "delta": "（自动编排）不执行自动补齐：%s" % _fill_stop}
                    if _plan_f:
                        yield {"type": "progress", "kind": "progress",
                               "delta": "（自动编排）自动补齐 %d 项：%s"
                                        % (len(_plan_f), "、".join(str(v.get("key")) for v in _plan_f))}
                        # ④ 执行（复用 _run_subtask —— 唯一保证"补跑与首跑同构"的路径：
                        #    同样的上下文快照/协议注入/token 记账/DB 写回）
                        try:
                            _dl_f = _t.time() + float(_cfg_fl("agent", "fill_deadline_s", 300))
                        except Exception:
                            _dl_f = _t.time() + 300.0
                        _pool_f = None
                        try:
                            from concurrent.futures import ThreadPoolExecutor as _TPE2
                            _pool_f = _TPE2(max_workers=1)
                        except Exception:
                            _pool_f = None
                        for _v_f in _plan_f:
                            _k_f2 = str(_v_f.get("key") or "")
                            _tk_f = _rows_f.get(_k_f2)
                            if not _tk_f or _pool_f is None:
                                continue
                            _rem = _dl_f - _t.time()
                            if _rem <= 8:
                                yield {"type": "progress", "kind": "progress",
                                       "delta": "（自动编排）补齐墙钟已到，剩余 %s 转人工"
                                                % _k_f2}
                                break
                            # escalate 升级序列（对齐 Claude Code 的
                            #   escalate → inject continue → give up，上限 3 次）：
                            #   第 1 次原参补跑；第 2 次起在指令里**明确要求更换策略**。
                            #   为什么需要：同一参数重跑第 2 次，成功率不会更高 ——
                            #   真机实测 t2 连续两轮都因「工具轮次耗尽」中断，第二次若不换策略
                            #   （比如先把 validate 提前到第一步），结果还是一样。
                            _rc_f = 0
                            try:
                                _rc_f = int(_tk_f.get("retry_count") or 0)
                            except Exception:
                                _rc_f = 0
                            _rnd_f = _rc_f + 1
                            _esc_f = _rnd_f >= 2
                            try:
                                _c_f = _tk_f.get("config")
                                if isinstance(_c_f, str):
                                    _c_f = json.loads(_c_f or "{}")
                                _c_f = dict(_c_f or {})
                                _bq = str(_c_f.get("query") or "")
                                _c_f["query"] = ((_bq + "\n\n") if _bq else "") + \
                                    _pp.fill_prompt(_v_f, _rnd_f, escalate=_esc_f)
                                _tk_f["config"] = _c_f
                            except Exception:
                                pass
                            # ★ 补跑记账**前置**（fail-safe 向停）：先 +1 再跑。
                            #   若放在成功后，一旦补跑异常/超时就不计数 ⇒ 同一任务可被无限重试。
                            #   与 replanning(T1) 的 retry_count 同源，共享 2 次天花板。
                            try:
                                conn.execute(
                                    "UPDATE agent_tasks SET retry_count=COALESCE(retry_count,0)+1, "
                                    "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_tk_f["id"],))
                                conn.commit()
                            except Exception:
                                pass
                            _r_f = None
                            try:
                                _fut_f = _pool_f.submit(_run_subtask, _tk_f)
                                _r_f = _fut_f.result(timeout=_rem)
                            except Exception as _fe:
                                # 超时/异常 ⇒ 协作式取消（子任务在下个事件点 break，
                                # 正解见 _cancel_probe；硬杀线程不安全）
                                try:
                                    TaskQueue.cancel(conn, _tk_f["id"],
                                                     "自动补齐超时或异常")
                                except Exception:
                                    pass
                                yield {"type": "progress", "kind": "progress",
                                       "delta": "（自动编排）补齐 %s 未完成（%s），已取消"
                                                % (_k_f2, type(_fe).__name__)}
                                continue
                            # 解包：**实际 10 元组**（函数 docstring 写的 9 元已过时，
                            # 末尾 off_id 是后加的）——按实现解包，别信注释
                            try:
                                (_rk, _rt, _rok, _rres, _revs, _rerr,
                                 _rlat, _rtok, _rsum, _roff) = _r_f
                            except Exception:
                                continue
                            if not _rok:
                                yield {"type": "progress", "kind": "progress",
                                       "delta": "（自动编排）补齐 %s 仍未成功：%s"
                                                % (_k_f2, str(_rerr or "")[:90])}
                                continue
                            # ui_summary 与首跑同源（services.subtask_protocol.ui_summary），
                            # 否则卡片文案仍是旧的中断自述（实测踩到，见 _fill_patch 注释）
                            try:
                                from services.subtask_protocol import ui_summary as _ui_sum2
                                _ui_f = _ui_sum2(_rres)
                            except Exception:
                                _ui_f = ""
                            try:
                                TaskQueue.complete(conn, _tk_f["id"],
                                                   _rres or "（自动补齐产出）",
                                                   {"summary": str(_rres)[:300],
                                                    "score": len(str(_rres or ""))},
                                                   latency_ms=_rlat)
                            except Exception:
                                pass
                            _fill_patch[_k_f2] = {
                                "task_key": _k_f2, "title": _rt,
                                "agent_id": _tk_f.get("agent_id"),
                                "result": _rres, "status": "done",
                                "summary": _rsum,
                                "result_offload_id": _roff or 0,
                                "proto_status": ((_rsum or {}).get("status")
                                                 if isinstance(_rsum, dict) else None),
                                # ★ 2026-10-22 端到端实测发现的展示不一致：
                                #   前端徽章读 `summary_status`、文案读 `ui_summary`。
                                #   只更新前者 ⇒ 出现「徽章 full + 文案还写着『本轮只完成了一部分』」
                                #   （实测消息 4780 的 t2 正是如此）。三处必须同源更新。
                                "ui_summary": _ui_f,
                            }
                            _fill_filled.append(_k_f2)
                            # 实时更正那张子任务卡（否则补齐期间卡片仍显示旧的 partial 徽章/文案，
                            # 要等最终 done 重渲染才变 —— 用户会以为"补了个寂寞"）。
                            # ⚠️ yield 不放进 try（吞掉 GeneratorExit 会让流无法正常关闭）。
                            yield {"type": "subtask", "key": _k_f2, "status": "done",
                                   "title": _rt, "agent": _tk_f.get("agent_id"),
                                   "latency_ms": _rlat,
                                   "token_count": _rtok,
                                   "retry_count": int(_tk_f.get("retry_count") or 0) + 1,
                                   "summary_status": ((_rsum or {}).get("status")
                                                      if isinstance(_rsum, dict) else ""),
                                   "ui_summary": _ui_f}
                            yield {"type": "progress", "kind": "progress",
                                   "delta": "（自动编排）补齐 %s 完成（第 %d 次补跑%s，%d 字）"
                                            % (_k_f2, _rnd_f,
                                               "，已升级策略" if _esc_f else "",
                                               len(str(_rres or "")))}
                        if _pool_f is not None:
                            try:
                                _pool_f.shutdown(wait=False)
                            except Exception:
                                pass
                    # ⑤ 并回 + 重算三态（**只升不降**：补齐是改善，补坏了不背锅）
                    if _fill_patch:
                        for _i_p, _it_p in enumerate(done_items):
                            _hit_p = _fill_patch.get(str(_it_p.get("task_key") or ""))
                            if _hit_p:
                                done_items[_i_p] = _hit_p
                        for _t_p in orch_tasks:
                            _hit_p = _fill_patch.get(str(_t_p.get("key") or ""))
                            if _hit_p:
                                _t_p["status"] = "done"
                                _t_p["proto_status"] = _hit_p.get("proto_status")
                                # ★ 三处同源更新（端到端实测 2026-10-22）：
                                #   summary（协议原文）/ ui_summary（卡片文案）/ summary_status（徽章）
                                #   —— 少更任何一个都会出现「徽章说完成、文案说没走完」的自相矛盾。
                                if isinstance(_hit_p.get("summary"), dict):
                                    _t_p["summary"] = _hit_p["summary"]
                                    _t_p["summary_status"] = _hit_p.get("proto_status")
                                if _hit_p.get("ui_summary"):
                                    _t_p["ui_summary"] = _hit_p["ui_summary"]
                        # 硬约束（P5：命中上限/仍有缺口**不许静默**）：
                        #   只要 orch_tasks 里还有没跑完的、或 done_items 里还有非 full 的，
                        #   聚合就不能是 full —— 否则就是"失败被记录成成功"的翻版。
                        try:
                            from services import subtask_protocol as _sp_p
                            _sm_p = []
                            for _it_q in done_items:
                                _s_q = _it_q.get("summary")
                                if isinstance(_s_q, dict) and _s_q.get("status") in _sp_p.STATUS_VALUES:
                                    _sm_p.append(_s_q)
                                else:
                                    _sm_p.append(_sp_p.normalize_text(
                                        _it_q.get("result"), task_status=_it_q.get("status")))
                            _agg_new = _sp_p.summarize_status(_sm_p) if _sm_p else _agg_status
                            _st_p = [str(t.get("status") or "") for t in orch_tasks]
                            _has_unfin = any(s not in ("done", "skipped") for s in _st_p)
                            _has_sum_unfull = any(
                                (str(((t.get("summary_status") or "") if t.get("summary_status")
                                      else "")) in ("partial", "failed"))
                                for t in orch_tasks)
                            if _agg_new == "full" and (_has_unfin or _has_sum_unfull):
                                _agg_new = "partial"
                            _rk_p = {"full": 2, "partial": 1, "failed": 0}
                            if _rk_p.get(_agg_new, 2) > _rk_p.get(_agg_status, 2):
                                _agg_status = _agg_new
                            _missing_keys = [k for k in _missing_keys
                                             if k not in set(_fill_filled)]
                            if _agg_status == "full":
                                _missing_keys = []
                        except Exception:
                            pass
                # ⑥ 清空队列里补齐期间堆积的子任务事件（无消费者时会积压；不清理
                #    会让后续 _drain 把补齐过程当"首跑实时输出"重放一遍）
                try:
                    while True:
                        _evq.get_nowait()
                except Exception:
                    pass
            except Exception as _fe2:
                yield {"type": "progress", "kind": "progress",
                       "delta": "（自动编排）自动补齐未执行（%s），按原状态汇总"
                                % type(_fe2).__name__}

            if _agg_status != "full" and done_items:
                _failed_keys = [str(t.get("task_key") or "") for t in orch_tasks
                                if str(t.get("status") or "") == "failed"]
                _partial_keys = [k for k in _missing_keys if k not in _failed_keys]
                _parts = []
                if _failed_keys:
                    _parts.append("%d 个子任务失败（无交付物）：%s"
                                  % (len(_failed_keys), ",".join(_failed_keys)))
                if _partial_keys:
                    _parts.append("%d 个子任务自报部分完成（交付物已产出，存在未验证环节）：%s"
                                  % (len(_partial_keys), ",".join(_partial_keys)))
                _gap_txt = ("，".join(_parts)) if _parts \
                    else "聚合状态 %s，但无失败/部分完成子任务明细" % _agg_status
                # ★ 2026-10-22（P0-b/P5）：把**病因分类前置到汇总开头**。
                #   为什么必须前置：现状是缺口散落在卡片 bullet 里，正文却读起来像"全部完成"——
                #   这是「失败被记录成成功」的**文案版**。用户会按 full 的理解去用这份汇总。
                #   对齐 OpenAI「命中上限不许静默」+ Claude Code 的 Stop hook 否决权。
                _cause_txt = ""
                try:
                    from agent import partial_policy as _pp2
                    if _fill_verdicts:
                        _cause_txt = _pp2.gate_summary(_fill_verdicts,
                                                       filled=_fill_filled)
                except Exception:
                    _cause_txt = ""
                if _cause_txt:
                    orch_content = ("（编排状态：%s——%s）\n%s\n\n"
                                    % (_agg_status, _gap_txt, _cause_txt)) + orch_content
                else:
                    orch_content = ("（编排状态：%s——%s）\n\n"
                                    % (_agg_status, _gap_txt)) + orch_content
                # ★ D4（2026-10-22）：blocked 类**自动生成澄清清单**，但不打断汇总
                #   （打断会让用户看不到已完成的部分）。清单只列"需要用户提供什么"，
                #   重跑无法解决 —— 对齐 Claude Code 对 `refusal` 的处置：不重试，直接说明。
                _ask_txt = ""
                try:
                    _filled_set = set(_fill_filled)
                    _blk = [v for v in _fill_verdicts
                            if str(v.get("cause")) == "blocked"
                            and str(v.get("key")) not in _filled_set]
                    if _blk:
                        _ask_txt = "；".join(
                            "%s：%s" % (v.get("key"),
                                        str(v.get("evidence") or v.get("hint") or "")[:70])
                            for v in _blk[:4])
                except Exception:
                    _ask_txt = ""
                yield {"type": "progress", "kind": "progress",
                       "delta": (f"（自动编排）编排状态 {_agg_status}：{_gap_txt}"
                                 + (f"；待你补充：{_ask_txt}" if _ask_txt else ""))}
            # P0-1（T3）：反思闭环——汇总后质量评审 → 未达标修订（复用画布 reflection 公共评审）
            reflection_meta = None
            if done_items:
                yield {"type": "progress", "kind": "progress",
                       "delta": "（自动编排）对汇总结果进行质量评审…"}
                from workflows.refine import RefineGate
                # Task 10：agg_status 传入 RefineGate——partial 时不自动重派（避免循环），仅评审 + 追加人工复核建议
                # ★ Task #43：reflection span —— 质量评审是一次独立 LLM 调用（评分 +
                #   冲突检测 + 可能的修订轮），成本与耗时都要能单独归因。
                #   parent=_run_sid 同 summary：run 的时间窗覆盖后段，挂 run 下防双算。
                _ref_sid = _spans.open("reflection", "质量评审", parent=_run_sid) if _spans else 0
                _ref = RefineGate.run(orch_content, goal, done_items, plan, provider_id, agg_status=_agg_status)
                reflection_meta = {
                    "enabled": True,
                    "rounds": _ref.get("rounds", 0),
                    "score": _ref.get("score"),
                    "passed": bool(_ref.get("passed", True)),
                    "issues": _ref.get("issues", []),
                    "advice": _ref.get("advice", ""),
                    "provider": (_ref.get("llm") or {}).get("provider", "-"),
                    "degraded": bool(_ref.get("degraded", False)),
                    # ★ 2026-10-14（批次三 #15）：结论冲突口径随 card_data 落库，
                    #   排障不必翻日志。⚠️ 含 stats.verdict —— 它区分
                    #   「检出 0 冲突」与「压根没数据可判」，两者**不可混为一谈**。
                    "conflicts": _ref.get("conflicts") or {"count": 0, "items": []},
                }
                if reflection_meta.get("score") is not None:
                    yield {"type": "progress", "kind": "progress", "delta": (
                        f"（自动编排）质量评审：{reflection_meta['score']}/100 · "
                        f"{'通过' if reflection_meta['passed'] else '未通过，已自动修订 ' + str(reflection_meta['rounds']) + ' 轮'}")}
                # ★ 冲突：**先于**评分事件告知用户（这是需要人来裁决的事，
                #   埋在分数后面会被忽略）。⚠️ stats.verdict 非 checkable 时
                #   只报口径、不说"已检查无冲突"——那是本仓反复栽过的假绿。
                _cftxt = ""
                try:
                    _cfm = reflection_meta.get("conflicts") or {}
                    _n = int(_cfm.get("count") or 0)
                    if _n > 0:
                        _cftxt = "检出 %d 处子任务结论冲突（同一对象给出互斥取值），已在评审中要求裁决" % _n
                    else:
                        _vd = ((_cfm.get("stats") or {}).get("verdict"))
                        if _vd == "no-data":
                            _cftxt = "结论冲突检测：本次子任务未产出可判定的结构化结论（跳过，非「已检查无冲突」）"
                        elif _vd == "insufficient":
                            _cftxt = "结论冲突检测：仅 1 个子任务提供结论，无法比对（跳过）"
                except Exception:
                    _cftxt = ""
                if _cftxt:
                    yield {"type": "progress", "kind": "progress",
                           "delta": "（自动编排）" + _cftxt}
                orch_content = _ref.get("content") or orch_content
                # ★ P4（2026-10-21 探针 C7 实测修复）：反思修订会**整体重写**
                #   orch_content ⇒ 上面在汇总后追加的**执行事实类标注**
                #   （提前收敛 / M4 停手 / 预算受限）会被揉掉。这些标注描述的是
                #   "执行发生了什么"，不是"报告怎么写" —— 被修订轮抹掉等于把
                #   early_stop 的用户可见性弄丢（用户看不到"剩余任务是主动放弃
                #   而非没做成"，正是本设计要防的误读）。
                #   ⇒ 在 reflection 之后**幂等重追加**（先判重，防反思未修订时
                #   出现两份同样的标注）。
                if budget_hit and "因预算限制部分子任务未执行" not in orch_content:
                    orch_content += "\n\n（因预算限制部分子任务未执行）"
                if _early_stopped and "目标已达成提前收敛" not in orch_content:
                    orch_content += ("\n\n（目标已达成提前收敛：必需产出全部完成，"
                                     "剩余 %d 个非必需任务主动放弃）"
                                     % len(_es_cancel_keys))
                if _m4_stopped and "健康巡检" not in orch_content:
                    orch_content += ("\n\n（健康巡检：预计剩余耗时超时间预算，"
                                     "未派发子任务已标记，提前进入汇总）")
                # reflection span 收口：status = 评审步骤是否正常完成（ok）；
                #   通过与否是**业务结论**，进 meta（score/passed/rounds/conflicts）。
                if _spans and _ref_sid:
                    _spans.close(_ref_sid, "ok",
                                 {"score": reflection_meta.get("score"),
                                  "passed": reflection_meta.get("passed"),
                                  "rounds": reflection_meta.get("rounds"),
                                  "degraded": reflection_meta.get("degraded"),
                                  "conflicts": int(((reflection_meta.get("conflicts") or {})
                                                    .get("count") or 0))})
            # P0-3（2026-09-19）：质量门禁回接状态聚合——reflection 未通过必须**降级可见**。
            # 取证：会话 351 的 reflection 已判 passed=false / score=62，子任务交付物里也明确写着
            # 「未产出代码、请先确认缺口」，但 orchestrated_status 仍是 full、前端徽章显示
            # 「正常完成」→ 失败被记录成成功，用户无法观测。并入既有三态后，前端徽章（按
            # full/partial/failed 分支渲染）**零改动**即可见，卡片警示也自动生效。
            _gate_gaps = []   # P0-3：质量门禁缺口——先无条件初始化，保证卡片字段「两条路径同键」
            if reflection_meta:
                try:
                    from services import subtask_protocol as _sp2
                    _agg_status, _gate_gaps = _sp2.apply_quality_gate(_agg_status, reflection_meta)
                except Exception:
                    _gate_gaps = []
                if _gate_gaps:
                    _missing_keys = list(_missing_keys) + _gate_gaps
                    orch_content = "（编排质量门禁：" + "；".join(_gate_gaps) + "）\n\n" + orch_content
                    yield {"type": "progress", "kind": "progress",
                           "delta": "（自动编排）编排质量门禁：" + "；".join(_gate_gaps)}
            # P0-1：编排成功后沉淀为可复用工作流（draft，前端可另存/画布编辑/发布）
            saved_flow_id = None
            if done_items:
                saved_flow_id = self._save_planner_flow(goal, plan, intent)

        # 5) 汇总内容流式推送
        #    P0-3（2026-09-17）：原 `range(0, len, 24)` 无节流切片 → 几十毫秒内把整段灌给前端，
        #    前端表现为「突然一大段」。改用 iter_stream_chunks 按 45 字符/秒 的类人节奏产出。
        # P0-7：原话术**无条件**说"N 个执行完成" —— 有 failed/blocked/partial 也照说"完成"，
        # 用户/审计会误以为全部成功（实测会话 514：t1 failed、t2~t6 blocked，仍显示"6 个完成"）。
        _n_bad = sum(1 for _ot in orch_tasks if str(_ot.get("status") or "done") != "done")
        _done_wording = f"{len(orch_tasks)} 个子任务执行完成" + (
            f"（其中 {_n_bad} 个未成功）" if _n_bad else "")
        yield {"type": "progress", "kind": "progress",
               "delta": f"（自动编排）{_done_wording if not degraded else '单 Agent 直行完成'}，汇总最终结论…"}
        for _chunk in iter_stream_chunks(orch_content):
            yield {"type": "token", "delta": _chunk}
        yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "done"}
        # 6) SysML 视图（自动编排 LLM 摘要可能丢弃子任务交付的 SysML v2 代码 → 先补回再投影）
        # ★ Task #43：views span —— SysML 代码补回 + 视图投影（后者含解析与布局，实测是
        #   编排尾段不可忽略的一段；没有 span 时"视图生成等了多久"无可归因）。
        #   parent=_run_sid 同 summary/reflection：尾段在 run 时间窗内，挂 run 下。
        _views_sid = _spans.open("views", "SysML 视图", parent=_run_sid) if _spans else 0
        _pre_sysml_len = len(orch_content)
        orch_content = self._ensure_sysml_blocks(orch_content, done_items)
        # 6.1) P0-3：补回的内容是「事后拼接」的，必须增量补吐，否则前端只看到摘要，
        #      SysML 代码块要等 done 重渲染才出现（体验上等于「又突然冒出一大段」）。
        if len(orch_content) > _pre_sysml_len:
            for _chunk in iter_stream_chunks(orch_content[_pre_sysml_len:]):
                yield {"type": "token", "delta": _chunk}
        sysml_views = self._gen_sysml_views(orch_content, intent, user_input)
        if _spans and _views_sid:
            try:
                _v_n = len((sysml_views or {}).get("views") or {}) if isinstance(sysml_views, dict) else 0
            except Exception:
                _v_n = 0
            _spans.close(_views_sid, "ok", {"views": _v_n})
        # 7) 落库 + done
        llm_info = {"provider": "自动编排", "model": "-", "used_mock": False,
                    "latency_ms": int((_t.time() - t0) * 1000)}
        yield {"type": "stage", "name": "写入会话", "status": "run"}
        # P0-2 持久执行：编排收尾 → phase=done / failed。此后该 run 不再进入"可恢复"清单。
        # ★ 2026-10-13（B1-5）：终态 phase 由 `_agg_status` **推导**，不再无条件写 done。
        #   背景（真库实证，口径 2026-10-12 只读）：run 531 `phase='done'`、`summary_written=1`，
        #   而它的 4 条 `agent_tasks` **全是 failed** —— phase 与任务状态**口径分裂**。
        #   危害：phase 是 resume 与「可恢复批次清单」的第一道判据，
        #   一个"子任务全败"的批次被标成 done ⇒ 运维在清单里看不到它，也**不会去 resume**，
        #   失败被静默吞掉。
        #   映射：full→done；partial/failed→failed。
        #   ⚠️ 为什么 partial 也算 failed 终态：`failed` 在 `TERMINAL_PHASES` 里（终态、不再可 resume），
        #   而 partial 批次的问题在**子任务**层，resume 本来也只重排未完成任务；
        #   若把 partial 标成 done 则彻底不可见，标成 failed 至少能在清单/监控里被看到。
        #   `degraded`（降级直行、无子任务）保持 done —— 那条路径没有子任务失败可言。
        try:
            from agent.orch_checkpoint import finish as _ck_fin
            _final_phase = "failed" if (not degraded and _agg_status in ("partial", "failed")) else "done"
            _ck_fin(conn, run_id, _final_phase)
        except Exception:
            pass
        card_data = json.dumps({
            "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            "orchestrated": True, "degraded": degraded,
            "team": (agent_def.intent_name or agent_def.name) if team_forced else None,  # 团队模式：主 Agent 团队负责人
            "orchestrated_status": _agg_status,   # Task 10：编排汇总三态（full/partial/failed）
            "run_id": run_id,   # ★ 2026-10-20：单点重跑入口依赖（前端编排卡「↻ 重跑子任务」）
            "quality_gate_gaps": _gate_gaps,      # P0-3：质量门禁缺口说明（与 orchestration.py 同键）
            # ★ 2026-10-22：病因分类 + 补齐结果。与 orchestration.py 同名同义。
            #   `fill_causes`：四类病因与证据（failed 徽章只说明"没成"，这里说明"为什么没成、
            #   能不能补"）；`fill_filled`：本轮真实补齐成功的 task_key（前端可打「已自动补齐」标）。
            "fill_causes": [{"key": v.get("key"), "cause": v.get("cause"),
                             "evidence": v.get("evidence"),
                             "fillable": bool(v.get("fillable")),
                             # 证据是否充分：False 表示"无 missing 无 risks，按保守口径判的"，
                             # 前端可据此显示"待复核"而不是当成确定结论。
                             "evidence_thin": bool(v.get("evidence_thin")),
                             "missing": v.get("missing") or []}
                            for v in (_fill_verdicts or [])],
            "fill_filled": list(_fill_filled or []),
            "run_budget": {"used_tokens": run_tokens, "budget": run_token_budget,
                           "hit": budget_hit, "executed": executed_count, "max_tasks": _max_tasks},
            "plan": orch_tasks, "sysml_views": sysml_views,
            "source": "none", "confidence": 0, "graph_count": 0, "vector_count": 0,
            "used_mock": llm_info["used_mock"], "provider": llm_info["provider"],
            "saved_flow_id": saved_flow_id,   # P0-1：编排沉淀的工作流 id（前端「另存为流程」）
            "saved_flow_name": f"自动编排·{(goal or '')[:18]}" if saved_flow_id else None,
            "reflection": reflection_meta,    # P0-1（T3）：反思闭环评审轨迹（评分/修订轮次/问题）
            # ★ 2026-10-14：**主路径的能力口径**（与复用路径的 `template_reuse` 对称）。
            #   前端据此区分「本次真执行了子任务，结构化能力已应用」与「模板复用，未应用」。
            #   `conflict_verdict` 直接透出 RefineGate 的三态口径
            #   （no-data / insufficient / checkable）—— **绝不把"未检出"说成"已检查无冲突"**。
            "capability_scope": {
                "mode": "subtasks_executed",
                "subtasks_executed": True,
                "structure_checks_applied": True,
                "conflict_verdict": ((reflection_meta or {}).get("conflicts") or {}
                                    .get("stats", {})).get("verdict") if reflection_meta else None,
            },
            "exec": {"reasoning": f"自动编排：{len(orch_tasks)} 个子任务由多 Agent 协作执行",
                     "agent": agent_def.name, "tools": [],
                     "subtasks": subtasks},   # P0-2：子任务轨迹持久化（历史消息还原）
        }, ensure_ascii=False)
        # P0-3：编排产出记忆沉淀（LLM 提炼 or 规则降级，每会话限 2 次）
        self._deposit_session_memory(user_input, orch_content, intent)
        # P0-5：技能使用反馈采集（流式编排路径）
        self._record_skill_feedback(conversation_id=int(conversation_id or 0), intent=intent, output_content=orch_content)
        with db_conn() as conn2:
            # 2026-09-29：user 消息已在 execute_stream 入口落库，此处只插 assistant（防重复）
            # ⚠️ 2026-10-21：编排**子任务**是以 `conversation_id=0` 嵌套调用本方法的
            #   （见 `execute_stream(query, 0, ...)` 两处调用）。若子任务内部也走了
            #   编排路径，这里会用 0 去插 `messages` —— 而 `messages.conversation_id`
            #   有指向 `conversations(id)` 的外键 ⇒ **必然 FOREIGN KEY 失败**，
            #   表现为"编排流式失败，回落单 Agent 直行"，且**子任务的编排产出
            #   整段丢失**（不落 messages、不写 summary_written）。
            #   口径：会话号无效 ⇒ 不写主会话消息（子任务本就不该污染主会话历史）。
            #   这与"子任务不重开 trace、沿用外层"（同处注释）是同一条纪律。
            if not conversation_id:
                # 无效会话号：跳过消息落库，但**仍要**标记 summary_written ——
                #   否则 resume 会误判"可只重做汇总"⇒ 同一份汇总出现两次。
                #   ⚠️ **不能 return**：本方法后续还要 yield stage/agent/done 事件，
                #   提前 return 会让子任务的编排事件整段消失（前端停在"执行中"）。
                if run_id:
                    try:
                        from agent.orch_checkpoint import mark_summary_written as _ck_msw
                        _ck_msw(conn2, run_id)
                    except Exception:
                        pass
                msg_id = 0
            else:
                cur = conn2.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", orch_content, "text", card_data)
                )
                msg_id = cur.lastrowid
                _archive_artifacts(conn2, conversation_id, msg_id, card_data, orch_content, intent)
                conn2.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, intent=? WHERE id=?",
                              (intent, conversation_id))
                # C-1（2026-10-04）：「汇总产物已落库」标记**必须与消息 INSERT 同事务**。
                #   分两次提交的话，中途崩溃会留下「消息在、标记 0」→ resume 误判"可只重做汇总"
                #   → 同一份汇总在会话里出现两次。故写在同一个 with 块内，由 db_conn 统一提交。
                if run_id:
                    try:
                        from agent.orch_checkpoint import mark_summary_written as _ck_msw
                        _ck_msw(conn2, run_id)
                    except Exception:
                        pass
                # Audit log
# P0-b（2026-10-03 审计差距评估）：审计写入统一走 core.audit.audit() ——
#   此前此处直插 audit_logs 只有 5 列，绕过溯源上下文（IP/UA/request_id）与哈希链，
#   且归属写死「王工」（不可信）。改由统一入口写入，与其余 200+ 条审计同一口径。
                audit(audit_user(user), "llm_chat",
                      f"会话#{conversation_id} · 自动编排（{len(orch_tasks)}子任务）· 意图:{intent}", conn=conn2)
        try:
            conn.close()
        except Exception:
            pass
        yield {"type": "stage", "name": "写入会话", "status": "done"}
        # V2.5：主 Agent（团队负责人）收尾事件——修前缺失致前端「子智能体：团队」停留"执行中"
        yield {"type": "agent", "status": "done", "name": agent_def.name,
               "display_name": agent_def.name, "intent": intent, "hil_level": hil_level}
        # ★ Task #43：run span（根）收口。status=ok 表示**流程正常走完**（步骤口径）；
        #   业务三态（full/partial/failed）与降级进 meta —— 与 card_data.
        #   orchestrated_status 同源，排障时一眼对上是"编排本身失败"还是"部分子任务没成"。
        if _spans and _run_sid:
            _spans.close(_run_sid, "ok",
                         {"agg_status": _agg_status, "degraded": bool(degraded),
                          "tasks": len(orch_tasks), "tokens": run_tokens,
                          "budget_hit": bool(budget_hit), "executed": executed_count})
        yield {"type": "done", "data": {
            "message_id": msg_id, "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            # B2-11（2026-10-13）：把编排批次号带给路由层 —— 事件落库靠它与
            # agent_tasks / tool_call_logs 关联（此前 done 不带 run_id，
            # 落下来的事件无法归到某次编排，等于又一批孤岛数据）。
            "orch_run_id": int(run_id or 0),
            "content": orch_content, "report": None, "card": json.loads(card_data),
            "sysml_views": sysml_views, "llm": llm_info, "orchestrated": True,
            "plan": orch_tasks, "degraded": degraded,
            "run_budget": {"used_tokens": run_tokens, "budget": run_token_budget,
                           "hit": budget_hit, "executed": executed_count, "max_tasks": _max_tasks},
            "retrieval": {"source": "none", "route": "none", "confidence": 0,
                          "graph_count": 0, "vector_count": 0, "attachment_used": False},
        }}

    def _stream_orchestrated(self, orch: dict, user_input: str, conversation_id, intent: str,
                             agent_def, hil_level: str, kb_tags: list, attachments: list,
                             slots: dict, user, run_id: int = 0) -> iter:
        # ⚠️ `run_id` 是**本方法的显式参数**（默认 0），不是外层闭包的变量 ——
        #   本方法与 `_stream_orchestrated_flow` 是**平级方法**，看不到对方的局部变量。
        #   （2026-10-13 真实建模验证抓到：最初直接在函数体里写 `run_id`，
        #    走"命中已发布沉淀流程"这条复用路径时崩，报 `name 'run_id' is not defined`，
        #    **14 个离线门禁全绿也没抓到** —— 它们只查"源码里有没有这个字符串"，
        #    不查它在该函数作用域内能不能解析。**静态字符串判据对作用域错误天生无能。**）
        #
        # ⚠️⚠️ **复用流程（`reused`）本来就不传**：那条路径用的是**预发布流程模板**，
        #   本次没有 create_plan、没有 agent_tasks 行 ⇒ 客观上不存在批次号，
        #   此时 `orch_run_id` 恒为 0。**这是事实，不是 bug** ——
        #   但绝不能为了"让它有值"而引用一个不存在的变量（那正是原缺陷）。
        #   真要让它有批次号，得在复用路径上补 create_plan，属独立议题。
        """P0-1 编排路径流式产出：子任务过程（agent 事件）→ 汇总 tokens → 落库 → done。

        与主路径落库段保持一致（card 含 orchestrated/plan），前端按 stage/agent/token 现有渲染直接可视化。
        """
        # 2026-09-23：此处原有写死的「知识库检索 done/retrieved=False」阶段事件。它无条件出现、
        # 与真实检索无关（编排模式下子任务的 stage 事件不外传，见子任务事件转发只收
        # token/reasoning/tool/done），会让用户看到一个从未真正执行过的「知识库检索·未引用」环节。
        # 现不再预发该阶段：检索真实性由各子任务按其 Agent 的 kb_required 决定。
        yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "run"}
        orch_tasks = ((orch.get("data") or {}).get("tasks")) or []
        # ★ B5-6（A-1）：复用路径**也要产出 subtasks 轨迹**。
        #   根因：`_stream_orchestrated`（复用）与 `_stream_orchestrated_flow`（真实执行）
        #   各写一份 card_data，而**只有后者带 `exec.subtasks`**
        #   ⇒ 复用路径落库的消息 `subtasks=[]`
        #   ⇒ 前端 `06-cards.js: if(subs.length)` 判空 → **整块不渲染**
        #   ⇒ 用户看到「有计划编导、有一堆子智能体状态，但没有任何子任务卡片」，
        #   且**刷新后实时 timeline 全丢**（那只是 DOM，从未落库）。
        #   实测（真库 conv 9360）：msg3983（真实执行）subtasks 3 条 /
        #   msg3985（复用）**0 条**，而 run535 实际跑5 个子任务、产出 3.6 万字符。
        #   ⇒ 数据在 `agent_tasks` 表里躺着，界面一条都看不到。
        #   本路径不逐个执行子任务，故 status 统一取 `orch_tasks` 自带值（缺省 done），
        #   **不伪造** latency/token —— 模板复用的口径本就`template_reuse`（见 capability_scope）。
        _subtasks = []
        for _i, _tk in enumerate(orch_tasks):
            _st = str(_tk.get("status") or "done")
            _subtasks.append({
                "key": _tk.get("key") or ("t%d" % (_i + 1)),
                "title": _tk.get("title") or _tk.get("key") or ("t%d" % (_i + 1)),
                "agent": _tk.get("agent") or "",
                # ★ 复用路径没有真实执行耗时/ token，**显式留空而不是填 0** ——
                #   填 0 会被前端 `(s.latency_ms/1000).toFixed(1)` 渲染成 "⏱ 0.0s"，
                #   看起来像"瞬间完成"，是**假数据**；留空则前端整段不显示（正确）。
                "latency_ms": _tk.get("latency_ms") or None,
                "token_count": _tk.get("token_count") or None,
                "deps": _tk.get("deps") or [],
                "status": "done" if _st == "done" else ("run" if _st == "running" else _st),
                "summary": _tk.get("summary") or "",
                "error": _tk.get("error") or "",
                # ★ F-3：复用路径（模板声明）**没有真实执行** ⇒ output 恒为 None。
                #   前端据此不渲染结果块，而不是渲染空块或编造内容
                #   （与 latency_ms/token_count 留空的理由同源：不许造假数据）。
                "output": _tk.get("output") or None,
                # 标记来源，便于前端与后续排查区分"真跑过"与"模板声明"
                "from_template": True,
            })
        for tk in orch_tasks:
            tkey = tk.get("key") or "t"
            ttitle = tk.get("title") or tkey
            yield {"type": "agent", "status": "run", "name": tkey, "display_name": ttitle,
                   "intent": intent, "hil_level": hil_level}
            yield {"type": "agent", "status": "done", "name": tkey, "display_name": ttitle,
                   "intent": intent, "hil_level": hil_level}
        orch_content = orch.get("content") or ""
        _n_bad = sum(1 for _ot in orch_tasks if str(_ot.get("status") or "done") != "done")
        _d = ("（自动编排）复杂任务已分解为 %d 个子任务执行完成%s，汇总最终结论…"
              % (len(orch_tasks), ("（其中 %d 个未成功）" % _n_bad) if _n_bad else ""))
        yield {"type": "progress", "kind": "progress", "delta": _d}
        # P0-3（2026-09-17）：无节流切片 → 类人节奏产出（本路径不做事后补块，保持原行为）
        for _chunk in iter_stream_chunks(orch_content):
            yield {"type": "token", "delta": _chunk}
        yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "done"}
        # SP-R/视图联动：编排汇总内容含 SysML → 投影视图（需求图/BDD 等会话内预览）
        sysml_views = self._gen_sysml_views(orch_content, intent, user_input)
        llm_info = {"provider": (orch.get("llm") or {}).get("provider", "自动编排"), "model": "-",
                    "used_mock": bool((orch.get("llm") or {}).get("used_mock", False)),
                    "latency_ms": orch.get("latency_ms", 0)}
        yield {"type": "stage", "name": "写入会话", "status": "run"}
        card_data = json.dumps({
            "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            "orchestrated": True, "degraded": bool(orch.get("degraded")),
            "plan": [{"key": t.get("key"), "title": t.get("title"), "agent": t.get("agent"),
                      "status": t.get("status")} for t in orch_tasks],
            "sysml_views": sysml_views,
            "source": "none", "confidence": 0, "graph_count": 0, "vector_count": 0,
            "used_mock": llm_info["used_mock"], "provider": llm_info["provider"],
            # ★ 2026-10-14（#19 之外的通用性项）：**复用路径的能力盲区显式化**。
            #   这条路径只播报 `agent run/done` 占位事件、**不执行子任务** ⇒
            #   挂在子任务上的结构化能力（claims 结论分档 / 结论冲突检测 /
            #   交付物结构校验）**在这里完全不会发生**，而用户看不出区别。
            #   ⇒ 落一条明确标记，前端可据此提示「本次为流程模板复用，
            #     未执行子任务，故未做结论分档/冲突检测/交付物结构校验」。
            #   ⚠️ 这**不是**缺陷标记（复用是刻意的产品语义），是**口径说明**：
            #     避免用户把"没检出冲突"误解成"检查过且无冲突"。
            "capability_scope": {
                "mode": "template_reuse",
                "subtasks_executed": False,
                "structure_checks_applied": False,
                "note": "本次命中已发布流程模板，未执行子任务；"
                        "结论分档/冲突检测/交付物结构校验均未应用",
            },
            "exec": {"reasoning": f"自动编排：复杂任务分解为 {len(orch_tasks)} 个子任务由多 Agent 协作执行",
                     "agent": agent_def.name, "tools": [],
                     # ★ B5-6：与真实执行路径（:1409）字段集对齐 ——
                     #   此前此处的 exec 只有 3 个键，缺 subtasks ⇒ 前端整块不渲染。
                     "subtasks": _subtasks},
        }, ensure_ascii=False)
        with db_conn() as conn:
            # 2026-09-29：user 消息已在 execute_stream 入口落库，此处只插 assistant（防重复）
            cursor = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                (conversation_id, "assistant", orch_content, "text", card_data)
            )
            msg_id = cursor.lastrowid
            _archive_artifacts(conn, conversation_id, msg_id, card_data, orch_content, intent)
            conn.execute(
                "UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, intent=? WHERE id=?",
                (intent, conversation_id)
            )
            # Audit log
# P0-b（2026-10-03 审计差距评估）：审计写入统一走 core.audit.audit() ——
#   此前此处直插 audit_logs 只有 5 列，绕过溯源上下文（IP/UA/request_id）与哈希链，
#   且归属写死「王工」（不可信）。改由统一入口写入，与其余 200+ 条审计同一口径。
            audit(audit_user(user), "llm_chat",
                  f"会话#{conversation_id} · 自动编排（{len(orch_tasks)}子任务）· 意图:{intent}", conn=conn)
            # 话题打标：落库后立即打标（含刚插入的当前轮消息）
            try:
                self._tag_topics(conn, conversation_id)
            except Exception:
                pass
        yield {"type": "stage", "name": "写入会话", "status": "done"}
        # V2.5：主 Agent（团队负责人）收尾事件——修前缺失致前端「子智能体：团队」停留"执行中"
        yield {"type": "agent", "status": "done", "name": agent_def.name,
               "display_name": agent_def.name, "intent": intent, "hil_level": hil_level}
        yield {"type": "done", "data": {
            "message_id": msg_id, "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            "orch_run_id": int(run_id or 0),   # B2-11：同上
            "content": orch_content, "report": None, "card": json.loads(card_data),
            "sysml_views": sysml_views, "llm": llm_info, "orchestrated": True,
            "plan": orch_tasks, "degraded": bool(orch.get("degraded")),
            "retrieval": {"source": "none", "route": "none", "confidence": 0,
                          "graph_count": 0, "vector_count": 0, "attachment_used": False},
        }}

    def _persist_partial_stream(self, conversation_id, content, reason="error", dry_run=False):
        """2026-09-29（用户五轮反馈4）：流式异常/客户端断开时，把已生成内容固化落库。

        此前 except 分支只 yield error 事件：浏览器里看得到的内容在库中 0 条，
        刷新即蒸发（"打开页面之前输出的内容不见了"）。现按 reason 补中断说明后缀，
        并与前端停止路径（/messages/partial）去重 —— 最后一条消息已是固化消息则跳过。
        """
        if dry_run or not conversation_id:
            return
        text = (content or "").strip()
        # ★ 2026-10-27（用户反馈「中途失败后之前输出的内容没有任何展示」）：
        #   断点固化是**低频且关键**的事件（一轮最多一次），必须留痕。
        #   原实现只有"写库失败"才 print —— 于是"**跳过**固化"（内容为空/去重命中）
        #   完全不可见，排查时只能靠猜（本轮为此多花了数轮才定位到 GeneratorExit 未注入）。
        #   一行日志换"为什么没存下来"这句话能被直接回答，值得。
        #   ⚠️ 必须 `flush=True`：服务以 `main.py > logs/xxx.log` 运行时 stdout 是**块缓冲**，
        #   不加 flush 这些行会一直躺在缓冲区里（本轮排查为此白跑两轮：以为"没走到固化"，
        #   其实只是没刷出来）。真正需要被看见的告警一律 flush。
        print(f"[_persist_partial_stream] reason={reason} conv={conversation_id} "
              f"候选字符={len(text)}", flush=True)
        if len(text) < 4:
            print("[_persist_partial_stream] 跳过：内容过短（<4 字），不生成空消息",
                  flush=True)
            return
        suffix = {
            "stop": "\n\n> ⏹ 已停止生成（以上为已生成内容）",
            "error": "\n\n> ⚠️ 输出因网络错误中断（以上为已生成内容，可直接输入\"重试\"继续）",
        }.get(reason, "\n\n> ⚠️ 输出中断（以上为已生成内容）")
        try:
            with db_conn() as conn:
                last = conn.execute(
                    "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
                    (conversation_id,)).fetchone()
                if last and last["role"] == "assistant":
                    _lc = last["content"] or ""
                    # ── 去重（2026-10-10 修）────────────────────────────────
                    # ★ **实测根因（会话 9335/9336）**：库里出现**两条几乎一样的 assistant 消息**
                    #   （9336: id=3887 len=2185 / id=3888 len=2206，后者多一个「⏹ 已停止生成」后缀，
                    #     剥离后缀后**逐字相同**）。
                    #   原因：**正常完成时已落过一条**，随后用户点"停止"触发 GeneratorExit
                    #   ⇒ 又走固化再写一条 ⇒ 两条内容相同。
                    #   ⚠️ 原去重只认「以上为已生成内容」这个后缀，
                    #   而**正常落库的那条没有后缀** ⇒ 判不出来 ⇒ 双写。
                    #⇒ 改为**按内容比对**（更本质的判据）：
                    #   后一条 = 前一条去掉中断后缀 ⇒ 同一段内容被写了两遍。
                    if "以上为已生成内容" in _lc:
                        return   # 前端停止路径已固化 → 不双写
                    if _lc.strip() and _lc.strip() == text.strip():
                        return   # ★ 正常落库已写过同样内容 → 不重复写
                    if text.strip() and text.strip().startswith(_lc.strip()[:200]) \
                            and len(_lc.strip()) >= 200:
                        # 前一条是本内容的**前缀**（截断后补写）⇒ 同样不重复写
                        return
                conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", text + suffix, "text", "{}"))
                conn.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (conversation_id,))
        except Exception as _persist_err:
            # 固化失败不掩盖原始异常，但**必须留痕**（原来 `pass` 静默：
            # 去重/写入这类缺陷会完全不可见，2026-10-10 的重复落库就是这样漏掉的）。
            print(f"[_persist_partial_stream] 固化失败（reason={reason}）：{_persist_err!r}",
                  flush=True)

    def _clarify_finish_events(self, conversation_id, user_input, intent, branch, attachments,
                               forced_intent, skill_name, team, clarify_req,
                               source="ask_user", title="❓ AI 执行中需要你确认后才能继续",
                               content="", exec_card=None):
        """澄清收口（生成器）：落挂起 + 归档已取回文档 + 发 clarify_ask 卡 + 发 done。

        ★ 2026-10-27：抽出来的唯一理由是**第二处调用点**——评估器判定
        `needs_user` 时也要走同一条收口（否则"需要你拍板"只能留在正文里，
        用户得自己另起一轮，正是米爸反馈的现象）。
        抽方法而不是复制粘贴：这段含"先归档再等回答"这条**踩过坑的**纪律
        （澄清会 `return`，走不到 persist 的 `_archive_artifacts` ⇒ 已取回文件丢失），
        复制一份必然在下一次修改时漂移。

        调用方约定：`yield from` 之后必须 `return`（本方法已发 done，主流程收尾）。
        """
        _iqs = [dict(clarify_req)]
        _mid = self._persist_clarify(conversation_id, user_input, intent, branch, attachments,
                                     forced_intent, skill_name, team, _iqs,
                                     content=content, card_data=exec_card)
        # 澄清不是失败：已经拿到的文件必须先落库，再等用户回答。
        try:
            if getattr(self, "_tool_sysml_docs", None):
                with db_conn() as _c_ar:
                    _archive_artifacts(_c_ar, conversation_id, _mid or 0,
                                       "{}", "", intent,
                                       tool_docs=self._tool_sysml_docs)
                    try:
                        from agent import async_jobs as _aj3
                        _aj3.link_archived(_c_ar, conversation_id, self._tool_sysml_docs)
                    except Exception:
                        pass
                self._tool_sysml_docs = []
        except Exception:
            pass
        yield {"type": "clarify_ask", "questions": _iqs, "intent": intent,
               "title": title,
               "message": str(user_input)[:200], "source": source}
        yield {"type": "done", "data": {
            "ok": True, "clarify_asked": True, "intent": intent,
            "content": "AI 执行中需要你确认（详见澄清卡片，回答后将继续）",
            "msg_type": "clarify", "usage": {}, "questions": _iqs,
            "message_id": _mid or 0, "clarify_source": source}}

    def _hil_confirm_finish_events(self, conversation_id, intent, confirm_req,
                                   content="", exec_card=None):
        """★ 2026-10-28（写前确认 HITL）收口：发确认卡事件 + done，结束本次流。

        ## 与 `clarify_required` 的关系：**同构但不同源**
        - 同构：都中断本轮 → 发一个"等你"的事件 → 落痕迹 → done → 用户处理完**另起一轮**；
        - 不同源：澄清的状态在 `conversations.pending_clarify`（单张），
          写前确认的状态在 `hil_confirmations`（**多张单**，每张一个确认单）。

        ## 为什么发**独立事件类型**而不是塞进工具结果文本
        现状前端是从工具结果文本里**正则抠**确认单号（`/确认单\\s*#(\\d+)/`，
        `static/js/mods/11-pipeline.js:465`）⇒ 后端改一个字就失效。
        ⇒ 新事件 `hil_confirm` 自带 `confirmation_id` / `tool` / `arguments` / `preview`，
        前端直接消费（老前端无此分支 ⇒ 静默忽略，**不破坏现有消费方**）。

        ⚠️ 此刻**尚未发生任何副作用**（统一闸在执行之前拦截）⇒ 事件文案必须让人敢批。
        """
        _mid = self._persist_hil_confirm(conversation_id, intent, confirm_req,
                                         content=content, card_data=exec_card)
        yield {"type": "hil_confirm",
               "confirmation_id": int((confirm_req or {}).get("id") or 0),
               "tool": (confirm_req or {}).get("tool") or "",
               "tool_type": (confirm_req or {}).get("tool_type") or "",
               "level": (confirm_req or {}).get("level") or "confirm",
               "match_key": (confirm_req or {}).get("match_key") or "",
               "kind": (confirm_req or {}).get("kind") or "",
               "preview": (confirm_req or {}).get("preview") or "",
               "arguments": (confirm_req or {}).get("arguments") or {},
               "batch_count": int((confirm_req or {}).get("batch_count") or 1),
               "batch_merged": bool((confirm_req or {}).get("batch_merged")),
               "conversation_id": int(conversation_id or 0),
               "timeout_at": (confirm_req or {}).get("timeout_at") or "",
               "title": "⏸ 写操作待人工确认",
               "message": "批准后才会真正执行；在此之前不会产生任何副作用。"}
        yield {"type": "done", "data": {
            "ok": True, "hil_confirm": True, "intent": intent,
            "content": "写操作已进入人工确认队列（批准后才会执行）",
            "msg_type": "hil_confirm", "usage": {},
            "confirmation_id": int((confirm_req or {}).get("id") or 0),
            "message_id": _mid or 0}}

    def execute_stream(self, user_input, conversation_id, branch="dev", provider_id=None,
                       attachments=None, forced_intent=None, skill_name=None, user=None, dry_run=False,
                       team=None, scope_id=None, scope_ids=None, scope=None, resume_run_id: int = 0,
                       resume_of_user_msg_id: int = 0, resume_append: str = "", _tracer=None):
        """流式执行（V2.3.2 优化：AI 输出流式 + 执行过程可观测）。生成器逐段产出 SSE 事件：

        {"type":"stage","name":..., "status":"run|done"}   右侧智能体执行状态
        {"type":"agent","status":"run|done",...}           子智能体执行（路由到的 Agent）
        {"type":"reasoning","delta":...}                    思考过程（LLM reasoning 增量）
        {"type":"progress","delta":...}                     系统进度提示（#10 拆型：原混在 reasoning 里仅靠 kind 区分）
        {"type":"tool","status":"run|done",...}             工具调用结果
        {"type":"token","delta":...}                        LLM 增量文本
        {"type":"done","data":{...}}                        最终结果（与 execute 返回结构一致）
        {"type":"error","message":...}
        dry_run: 编排子任务流式执行时置 True（不落库，仅产出事件）。
        team: 团队模式（AI 会话页「工作流」下拉）——主 Agent 团队负责人统一调度：
        意图识别/任务拆分/计划制定/任务分派（委派候选收敛到团队成员）/内容整合输出。
        _tracer: P0-b 会话级 ConversationTracer（/chat/stream 入口创建）。**显式传参**
        而非全局查找：execute_stream 的同步生成器整体跑在 SSE 心跳 daemon 线程
        （core/sse.iter_with_heartbeat_async），ContextVar 不跨线程继承，深层挂载点
        （tool/rag/llm）经 set_current_tracer 在本线程落地后才能被 current_tracer() 拿到。
        编排子任务流不传（_tracer=None）⇒ 全部打点退化为 no-op。
        """
        attachments = attachments or []
        # ★ 2026-11-13（#56）：**会话级附件继承** —— 附件是会话资源，不必每轮重传。
        #   实测根因（会话 9571）：用户 02:49 上传 doc 848，04:26/04:41 两轮追问
        #   「再次分析上传附件的内容」时请求里 **attachments=[]**
        #   ⇒ `_att_sections` 为空 ⇒ `read_attachment_section` 工具压根没进 tools_def
        #   ⇒ 模型既没正文也没取正文的工具 ⇒ 只能重复「读不到第 5/6/7 章」。
        #   继承必须在**入口**做（不能等到附件解析点）：编排分支在附件解析**之前**
        #   就 return 了，只在解析点继承 ⇒ 编排路径永远继承不到。
        #   ⚠️ 落库仍用**显式附件**（`_att_explicit`）——继承的附件不写进本轮 user 消息，
        #   否则每次追问都堆一份相同附件（历史膨胀 + 前端附件栏重复显示）。
        _att_explicit = list(attachments)
        attachments = self._inherit_session_attachments(conversation_id, attachments)
        # P1-30（2026-10-09）：**请求级瞬态状态统一清零**（与 execute.py 同源同处）。
        # ★ 位置纪律：**必须早于本方法内任何 `self.X =` 赋值**（`self._mem_ctx = ...`），
        #   否则清零会抹掉本次请求刚设好的上下文。
        #   根因与实测证据见 `common.reset_request_state` 的 docstring。
        reset_request_state(self)
        # P0-b（M2）：把会话 tracer 落入**本线程** ContextVar —— 执行流跑在 SSE 心跳
        # daemon 线程，入口线程 set 的值不继承；深层挂载点（M3 tool / M4 rag / M5 llm）
        # 全部经 current_tracer() 取，取不到即 no-op（降级安全）。
        if _tracer is not None:
            try:
                from services.orch_span import set_current_tracer as _set_ct
                _set_ct(_tracer)
            except Exception:
                pass   # 观测装配失败绝不阻断主流程（不静默：span 缺失在 monitor 页可见）
        # 2026-09-29：user 消息在流开始即落库（原在流收尾与 assistant 一并 INSERT）。
        # 修「流式进行中库中 0 条消息 → 切页再切回，selectConv 渲染空态覆盖 #stream-ai 现场」；
        # 也顺带修复澄清路径（clarify_ask 提前 return）user 消息从未落库的缺口。
        # 收尾块只插 assistant（三处已同步去除 user INSERT）；dry_run=True（编排子任务流式）不落库。
        # ⚠️ resume_run_id 非 0 时**必须跳过**：恢复不是新一轮对话，`user_input` 传的是空串
        # （真实输入存在检查点里，只用于重建 goal）——照插会在会话里留下一条空 user 消息。
        # 这是批次 2 自查时才发现的缺陷：入口落库是无条件的，而恢复路径本不该产生新轮次。
        # ★ 2026-10-17（米爸 Q1 DB 层收口）：`resume_of_user_msg_id` 非 0 = 澄清续答 ——
        #   同样不新起 user 轮次，改为把澄清补充**追加到触发本轮澄清的那条 user 消息**上
        #   （一轮 = 一条 user 消息；AI 的澄清卡与续答输出同属该轮，刷新后与直播一致）。
        if not dry_run and not resume_run_id and not resume_of_user_msg_id:
            try:
                with db_conn() as _conn0:
                    _conn0.execute(
                        "INSERT INTO messages (conversation_id, role, content, msg_type, card_data, attachments) VALUES (?,?,?,?,NULL,?)",
                        (conversation_id, "user", user_input, "text", json.dumps(_att_explicit, ensure_ascii=False))
                    )
                    _conn0.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (conversation_id,))
            except Exception:
                pass   # 入口落库失败不阻断流：收尾 done 事件仍会到达，避免整轮白跑
        elif not dry_run and not resume_run_id and resume_of_user_msg_id and conversation_id:
            # 追认追加：目标 user 消息必须同会话同角色，防跨会话/错角色写入
            try:
                with db_conn() as _connR:
                    _cur = _connR.execute(
                        "UPDATE messages SET content = COALESCE(content,'') || ? "
                        "WHERE id=? AND conversation_id=? AND role='user'",
                        (resume_append or "", int(resume_of_user_msg_id), int(conversation_id)))
                    _hit = _cur.rowcount or 0
                    if _hit:
                        _connR.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                       (conversation_id,))
                if not _hit:
                    raise RuntimeError(f"追认目标 user 消息不存在（id={resume_of_user_msg_id}）")
            except Exception as _re:
                # 兜底：追不上（消息被删/跨会话）就按老行为落一条新 user 消息 ——
                # 内容不丢，只是轮次退回"新起一条"（可视化为两条 user 气泡）。
                print(f"[resume-append] 追加失败，回退新 user 消息：{_re!r}")
                try:
                    with db_conn() as _conn0:
                        _conn0.execute(
                            "INSERT INTO messages (conversation_id, role, content, msg_type, card_data, attachments) VALUES (?,?,?,?,NULL,?)",
                            (conversation_id, "user", user_input, "text", json.dumps(_att_explicit, ensure_ascii=False))
                        )
                except Exception:
                    pass
        # 记忆作用域上下文（对齐 mem0）：本会话的 conversation_id + 当前用户，供记忆读写取作用域
        self._mem_ctx = {"conversation_id": conversation_id, "user": user}
        # P0-c（2026-10-03 评估）：开启链路上下文 —— 本次请求内所有 LLM 调用自动带上
        # conversation_id / trace_id（llm/_record_usage 消费），使用量可从会话下钻到单次调用。
        # 编排子任务会以 conversation_id=0 嵌套调用本方法 —— 此时**不重开** trace，沿用外层
        # （否则每颗子任务都会生成新 trace_id，整轮成本无法聚合）。
        try:
            from core.audit import begin_trace, bind_trace, trace_context as _tctx_ex
            _exist = _tctx_ex()
            _trace = begin_trace(conversation_id=int(conversation_id or 0)) \
                if (conversation_id or not _exist.get("trace_id")) else _exist
        except Exception:
            _trace = {}
        # ★ P0-3（2026-10-27）：把本轮 trace_id 落到实例上，供 `_log_tool_call` 写进
        #   `tool_call_logs.trace_id`。**这是任务级追溯的 JOIN 键** ——
        #   `llm_usage_stats` 早就按 trace_id 聚合，而工具调用日志此前只有
        #   `run_id`（主对话路径恒 0，因为单 Agent 直行没有编排批次）⇒ 两套 id 不可 JOIN，
        #   回答不了「这一轮对话调了哪些能力、花了多少钱」。
        #   取值与 LLM 侧**同源**（同一个 `begin_trace` 产物，不是另造一个 id）。
        self._tool_trace_id = str((_trace or {}).get("trace_id") or "")
        # ★ 请求 3（2026-10-27）：单 Agent 直行路径的 run_id 归正。
        #   此前 run_id 只由 `_stream_orchestrated_flow`（line 482）赋值 —— 那是编排批次号；
        #   主对话（单 Agent 直行）从不赋值 `self._tool_run_ctx` ⇒ `_log_tool_call` 读到的恒 0
        #   （走查 P0-3 记为"设计如此"）。但"设计如此"埋两雷：
        #     ① 直行路径的工具调用无法按 run 归批（只能靠 trace_id，而 run_id / trace_id 是两套体系）；
        #     ② 任何按 run_id 做统计/分组的看板都会把绝大部分流量（单 Agent 直行）漏掉 ⇒ 误导性空图。
        #   彻底修法：把"一轮对话"明确定义为一个 run。编排路径已有 run（批次号），直行路径在此
        #   就地分配一个 run_id（与编排同源的 TaskQueue.new_run_id），供 `_log_tool_call` 收口写入。
        #   ⚠️ 仅顶层轮次分配：嵌套子 Agent（conversation_id=0，复用外层 trace）**不重分配**，
        #   以免覆盖父编排已设的 run_id（见 _stream_orchestrated_flow:831 给 sub._tool_run_ctx 赋值）。
        if conversation_id:
            try:
                from task_queue import TaskQueue
                from database import db_conn as _dbc
                with _dbc() as _c:
                    self._tool_run_ctx = int(TaskQueue.new_run_id(_c) or 0)
            except Exception:
                self._tool_run_ctx = 0
        else:
            self._tool_run_ctx = 0

        self._mem_project_id_cache = None   # 每次执行清缓存：缓存只在本请求内有效，防跨会话串味
        # ★ P0-4（2026-10-26）：RAG 侧记忆召回结果（单一真源用）。**每轮必须重置** ——
        #   否则本轮未走检索时，会消费到**上一轮**的 memory_hits（跨轮串味）。
        self._rag_memory_hits = []
        try:
            # V2.4 会话内执行过程持久化：累计思考/工具调用 → 写入 card_data.exec（前端历史消息还原）
            exec_reasoning = []
            # ★ 2026-10-28（用户反馈「不用给出统一的思考过程的输出」）：
            #   **系统进度提示与模型 CoT 分两条通道**。
            #   此前 7 处系统提示（收到补充指令/判断需要调工具/工具结果弱相关/
            #   重复调用收敛/写操作已记入/harness 持续工作）都 append 进 exec_reasoning
            #   ⇒ 落库后前端统一渲染成一个大块「💭 思考过程」（06-cards.js:20），
            #   用户看到的就是"一堆统一思考过程"，而不是归到对应执行环节。
            #   现在：真 CoT 进 exec_reasoning（心智链），系统提示进 exec_notices（带 kind），
            #   前端按 kind 收编到对应环节。
            exec_notices = []
            exec_tools = []
            card_data = "{}"  # dry_run=True（编排子任务流式）不落库，done 事件仍引用 → 兜底空卡
            # 2026-09-29（用户五轮反馈4）：流式部分内容固化 —— llm_content 提前初始化（异常路径引用），
            # _orch_acc 收集编排路径的 token（直行路径已由 llm_content 承担）。
            llm_content = ""
            # ★★ 2026-10-27（用户反馈「中途执行失败后之前输出的内容没有任何展示」）：
            #   `_streamed_acc` = **本轮真的发给过用户的正文**，只增不减、跨自主续跑轮不复位。
            #   为什么不复用 llm_content：`llm_content` 在半途会被两处**清空**
            #     · 每轮续跑起点（`llm_content = ""`，续跑要干净起步）；
            #     · 最终生成前收到补充指令时（`llm_content = ""`，强制按新指令重答）。
            #   而这两处清空**之前**用户已经在屏幕上看到过内容；若失败只按 llm_content 固化，
            #   那些内容照样蒸发（"第 2 轮失败"就是这种形态）。
            #   ⚠️ 用单槽 list 是为了可变：except / GeneratorExit 分支在**闭包外**也要读到它。
            _streamed_acc = [""]
            # ★ 2026-10-17（米爸「harness 持续工作」）：跨自主续跑轮的完整产出。早初始化是为了异常/断开路径（下方 except）引用不炸。
            _full_content = ""
            _orch_acc = []

            def _yield_collect(src, acc):
                """包装编排子生成器：顺手收集 token 增量，供异常/断开时固化已生成内容。"""
                for _ev in src:
                    if isinstance(_ev, dict) and _ev.get("type") == "token":
                        acc.append(_ev.get("delta") or "")
                    yield _ev
            self._load_db_agents(user)  # P0 平台化：DB 驱动 Agent 注册表（P1-8：按用户隔离）
            self._last_skill_hits = []
            # 团队模式：校验主 Agent（团队负责人）。C1/C3：此处**不再**直接置 team_forced=True
            # —— 是否编排改由团队定义路由决策（`_team_route_decision`）在意图识别之后判定，
            # 否则问「你好」也会跑一次完整 Planner-Executor。
            team_forced = False
            team_intent = None
            team_def = None
            team_decision = None
            team_direct = False
            # ★ 2026-10-17：澄清探测的 quick_pick 判据取**用户入口原值**（intent_override），
            #   不是被 team/R1 覆写后的 forced_intent。覆写后的值恒非空 ⇒ `_clarify_skip_reason`
            #   对每个带 team 的请求都返回 quick_pick ⇒ 内容级澄清（阶段 1.5）在 UI 路径
            #   **恒被跳过**（2026-10-16 R1 引入的回归：选主 Agent ≠ 用户说明了建模内容）。
            #   注意与 `_persist_clarify` 的分工：那里存覆写后的值（续答定向用），这里只看入口。
            _clarify_gate_intent = forced_intent
            if team:
                team_def = self._team_definition(team)
                team_intent = self._resolve_team(team)
                if not team_intent:
                    yield {"type": "error", "message": (
                        f"智能体团队不存在或未启用：{team}，请到「Agent 管理」创建主 Agent 并启用后重试")}
                    return
                # ★ 2026-10-16（米爸决策 R1+R2）：**主 Agent 优先于 forced_intent**。
                #   原写法 `forced_intent = forced_intent or team_intent` 让 forced_intent 压过主 Agent，
                #   而 forced_intent 会话由前端隐藏 select（quick-agent）从 localStorage 恢复 ⇒
                #   选「智源对接agent」却被 residual requirement_analysis 劫持（会话 9367 / 消息 4029 实测）。
                #   现在：用户显式选了主 Agent，就以主 Agent 为准，forced_intent 一律丢弃。
                #   ⚠️ forced_intent **参数本身必须保留** —— 编排内部
                #   （workflows/nodes.py:88、planner.py:278、stream.py:267/536 分派子 Agent）
                #   仍靠它直连指定 Agent；此处只改"用户入口"这条路径的优先级。
                if forced_intent and forced_intent != team_intent:
                    # ★ 2026-10-28：补 kind=progress —— 这是**系统告知**不是模型思考。
                    #   不带 kind 时前端走 procAddThinking（"AI 思考"框），
                    #   用户根本看不到"为什么是这个执行主体"（用户反馈③的成因之一）。
                    _subj_note = (f"（执行主体）已按你选择的主 Agent「{team_intent}」执行；"
                                  f"忽略外部传入的定向值 {forced_intent}")
                    exec_notices.append({"kind": "progress", "text": _subj_note})
                    yield {"type": "reasoning", "delta": _subj_note, "kind": "progress"}
                    forced_intent = None
                else:
                    forced_intent = team_intent
                # ★ 2026-10-16 R1：把所选主 Agent **落到会话**，供后续任何未显式带 team 的
                #   请求回读（澄清续答/换标签页/新接口忘传都不再静默掉回通用 Agent）。
                self._save_main_agent(conversation_id, team_intent)
            elif conversation_id and int(conversation_id) > 0:
                # ★ 2026-10-16 R1：请求未带 team ⇒ 回读会话绑定。命中则按它执行并**明示**，
                #   让"为什么是这个 Agent"始终可解释（不静默继承）。
                _bound = self._load_main_agent(conversation_id)
                if _bound:
                    team_intent = _bound
                    forced_intent = forced_intent or _bound
                    # ★ 2026-10-28（用户反馈③「我只是输入一段V2代码让AI校验，
                    #   他自动读取了智源的工程数据」）：用户看到的"自动用智源"，
                    #   真因是**本会话绑定的主 Agent 覆盖了意图**，而这条说明
                    #   原来以纯 reasoning 形式发出去 ⇒ 落进"AI 思考"框，用户看不到。
                    #   现在：① 走 progress 通道（界面可见）；② 进 exec_notices 落库；
                    #   ③ 结构化写进意图识别 stage 的 exec_subject 字段（下一段代码）。
                    _subj_note = (f"（执行主体）本次未指定主 Agent，已按本会话绑定的"
                                  f"「{_bound}」继续执行；如需更换，请在输入框上方重新选择主 Agent")
                    exec_notices.append({"kind": "progress", "text": _subj_note})
                    self._exec_subject_note = _subj_note
                    yield {"type": "reasoning", "delta": _subj_note, "kind": "progress"}
            # ── 阶段 1：意图识别（V2.7：无论是否指定智能体，一律执行识别——
            # 指定智能体只作"执行 Agent 定向"，不再跳过识别；目标/槽位/澄清/多意图都依赖识别结果）──
            yield {"type": "stage", "name": "意图识别", "status": "run"}
            # P0-1：Glossary 归一化（带 conn 的 detect 优先术语归一化）
            # P0-2：轻量 DST——读取会话级当前意图，无信号时继承
            dst = self._load_conversation_dst(conversation_id)
            # P0-5（2026-09-30）：历史联合召回 —— 取最近几条**用户原话**，只供识别的低置信段
            #   使用（**不喂规则层**：实测历史里的强特异词会以 0.95 劫持路由，详见
            #   IntentRouter._sanitize_history）。开关 intent.history_recall。
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
            # P0-4（2026-09-19）：显式定向（团队/指定 Agent）覆盖语义识别结果时**不静默**。
            # 取舍：显式选择优先（用户选了团队就该按团队走），但差异必须可见——否则
            # 「识别到 A、实际跑 B」用户永远发现不了（会话 351 卡片的 confidence=0 即此形态：
            # 既无置信度、也无任何"为何是该意图"的说明）。
            # P0 和解（2026-10-23 米爸反馈"意图识别与推荐不一致，感觉很不好"）：
            # 不再把系统内部的两裁判分歧用技术措辞推给用户（"两者不一致，
            # 如非所愿请去掉团队/Agent 指定后重发"——读起来像在怪用户）。
            # 显式选择本就优先（行为不变），识别意见降级为一句中性备注，
            # 分歧细节照常进 meta/日志（可观测不减）。
            if forced_intent and _detected and forced_intent != _detected:
                yield {"type": "reasoning", "delta": (
                    f"已按你选择的「{forced_intent}」执行"
                    + (f"（识别意见：{_detected}，仅供编排参考）" if _detected != "chat" else ""))}
            # Task 11 多意图增强（可选项）：多阶段指令 → 附加 multi_intent 序列事件（供前端展示/编排衔接）
            _multi_intent = self.router.detect_multi(user_input)
            # 2026-09-25：阶段序约束**带上每个阶段在说什么**（`{意图}（原句）`）。
            # 修前只传意图名序列（如 "requirement_analysis → design"），planner 只能知道"要哪些阶段"，
            # 不知道各阶段具体干什么（用户原句里的对象/范围全丢了）→ 生成的子任务描述容易泛化。
            # tasks 为空时回落到 sequence（保持旧行为，不因新字段缺失而退化）。
            _stage_hint = None
            if _multi_intent:
                _stage_hint = [f"{t['intent']}（{t['text']}）" for t in (_multi_intent.get("tasks") or [])] \
                              or _multi_intent["sequence"]
            if _multi_intent:
                yield {"type": "multi_intent", "sequence": _multi_intent["sequence"],
                       "raw_subtasks": _multi_intent["raw_subtasks"]}
            # ── C3/C4/C5：团队定义驱动路由（在意图识别之后、取agent_def 之前） ──
            # 为什么必须在这里：路由需要 `_detected`（C4：识别结果要参与决策，
            # 此前 team 模式下被forced_intent 直接覆盖丢弃），而下游全部按 `intent` 取
            # agent_def ⇒ 必须在取 agent_def 之前改写 intent，否则改了不生效。
            if team_def:
                team_decision = self._team_route_decision(
                    user_input, team_def, provider_id=provider_id, detected_intent=_detected or "")
                _act = (team_decision or {}).get("action")
                if _act == "reject":
                    # 明确提示不支持 —— 必须带能力清单与出路，不返回死胡同
                    _msg = self._team_unsupported_message(team_def,
                                                          (team_decision or {}).get("reason"))
                    yield {"type": "reasoning", "delta": f"（团队路由）{_msg}"}
                    yield {"type": "stage", "name": "团队路由", "status": "skip",
                           "reason": (team_decision or {}).get("reason")}
                    yield {"type": "done", "data": {
                        "ok": False, "intent": intent, "team": team_def.get("intent"),
                        "team_decision": team_decision,
                        "content": _msg, "message_id": 0}}
                    return
                if _act == "chat":
                    # 短噪声/问候：走通用直答，不编排、不定向团队成员
                    intent = "chat"
                    forced_intent = None
                    team_direct = True
                elif _act == "direct" and (team_decision or {}).get("target"):
                    # 定向到团队内最相近/命中的成员直行（仍不编排）
                    intent = team_decision["target"]
                    forced_intent = intent
                    team_direct = True
                    if _detected and _detected != intent:
                        yield {"type": "reasoning", "delta": (
                            f"（团队路由）已定向到团队成员「{team_decision['target']}」"
                            f"（{team_decision.get('reason') or ''}）；"
                            f"意图识别结果为 {_detected}")}
                    else:
                        yield {"type": "reasoning", "delta": (
                            f"（团队路由）已定向到团队成员「{team_decision['target']}」"
                            f"（{team_decision.get('reason') or ''}）")}
                elif _act == "orchestrate":
                    team_forced = True
                else:
                    # ★ 2026-10-16 R2：主 Agent 无成员 → **由本体自主执行**（不是错误，也不静默）。
                    #   改前这里是纯 `team_forced = False`，无任何提示 ⇒ 用户选了主 Agent 却像没选。
                    #   现在把 decide 备好的 notice 显式说出来，并保持 team_direct=True ——
                    #   它必须**拦住**下游 `_needs_orchestration`（否则会另起 Planner-Executor，
                    #   换一个执行主体，与"由所选主 Agent 执行"矛盾）。
                    team_forced = False
                    team_direct = True
                    _notice = (team_decision or {}).get("notice") or ""
                    if _notice:
                        yield {"type": "reasoning", "delta": f"（主 Agent 自主执行）{_notice}"}
                agent_def = self.registry.get(intent)
            else:
                agent_def = self.registry.get(intent)
            hil_level = agent_def.hil_level
            # ★ 2026-10-28（写前确认 HITL）修断链①：**stream 路径从不把 hil_level 赋给 self**
            #   ⇒ `common.py:644` 每请求清成 None 后无人再写 ⇒
            #   `getattr(self,"_hil_level","L0") == "L2"` **恒 False** ⇒ L2 闸永不触发。
            #   （真库实证：agents 表里唯一 L2 的 model_release 从未被强制过。）
            #   现在赋上：统一闸用它作**下限**（只收紧不放宽，见 hil_policy.escalate），
            #   动作级档位仍由策略表决定（行业依据 Suntec：不要给 agent 整体分等级，
            #   要给每个动作分等级 —— agent 级只能当"地板"，不能当唯一判据）。
            self._hil_level = hil_level
            effective_provider = provider_id or self.registry.get_provider_id(intent)  # 优化2
            # ★ P1-b（2026-10-10）Agent 级模型参数（含 thinking_mode）——
            #   `agents.model_params` 此前是**死配置**（有列、有前端表单，无消费点）。
            #   取一次供本方法所有 chat 调用复用；取不到一律 {} ⇒ 零行为漂移。
            try:
                _agent_mp = self.registry.get_model_params(intent)
            except Exception:
                _agent_mp = {}
            # ── P0-2 断点续跑（2026-10-03）：resume 绕过"意图识别之后的一切前置判定" ──────
            # 为什么必须在这里插队：内容级澄清（`_clarify_detect`）、复杂度判定（`_needs_orchestration`）、
            # 沉淀流程复用（`_try_reuse_planner_flow`）都是**启发式**的。续跑时用户可能一句话都不输入，
            # 这些判定会走向完全不同的分支（甚至直接回落单 Agent 重跑）——那就不是"续跑"而是"重跑"。
            # 结论：resume 是一条**直达通道**，唯一依据是 run_id + 检查点。
            if resume_run_id and not dry_run:
                _ckr = None
                try:
                    from database import get_db as _gdb
                    from agent.orch_checkpoint import load as _ck_load2
                    _c0 = _gdb()
                    try:
                        _ckr = _ck_load2(_c0, int(resume_run_id))
                    finally:
                        try:
                            _c0.close()
                        except Exception:
                            pass
                except Exception:
                    _ckr = None
                if not _ckr:
                    yield {"type": "error", "message": "编排检查点不存在或已被清理，无法恢复该批次"}
                    yield {"type": "done", "data": {"ok": False, "intent": intent,
                                                    "content": "该编排批次不可恢复（无检查点记录）"}}
                    return
                _r_intent = (_ckr.get("intent") or intent or "")
                try:
                    _r_agent = self.registry.get(_r_intent)
                except Exception:
                    _r_agent = agent_def
                _r_text = (_ckr.get("user_input") or user_input or "")
                yield from _yield_collect(self._stream_orchestrated_traced(
                    _r_text, conversation_id, (_ckr.get("branch") or branch), _r_intent,
                    _r_agent, getattr(_r_agent, "hil_level", hil_level),
                    IntentRouter.extract_kb_tags(_r_text), attachments, None, user,
                    int(_ckr.get("provider_id") or effective_provider or 0) or None,
                    resume_run_id=int(resume_run_id)), _orch_acc)
                return
            # P1 意图结构化拆解 + P2 用户上下文
            # P0-2：槽位跨轮合并——上轮槽位为底，本轮新值覆盖（entities/constraints 并集）
            # P1-4（2026-10-01）：带上 conversation_id/user_input —— 换话题时丢弃历史槽位
            slots = self._merge_slots(dst["slots"], self.task_decompose(user_input, intent),
                                      conversation_id=conversation_id, user_input=user_input)
            user_ctx = self._build_user_context(user)
            kb_tags = IntentRouter.extract_kb_tags(user_input)
            # 控制标签（#工程数据/#知识库）只作触发信号，不并入检索词
            _kb_query_tags = [t for t in kb_tags if t not in ("工程数据", "知识库")]
            kb_hint = f" + {' + '.join('#' + t for t in _kb_query_tags)}" if _kb_query_tags else ""
            req_scope, scope_att, effective_kb_scope = self._resolve_req_scope(scope_ids, scope_id, scope, agent_def)
            # SP-R：报告类型预识别（模型分析/变更影响/预评审/自定义大纲）
            report_type = None
            if intent == "report_generation":
                try:
                    from report_generator import report_generator as _rg
                    report_type = _rg.detect_report_type(user_input)
                except Exception:
                    report_type = None
            _intent_meta = self.router.get_last_meta()
            # 2026-09-26 意图样本池**采集入口**：把这条真实用户输入记进候选池（弱标注=系统当时的判定）。
            # 为什么落在这一行：意图/路由已定、且**尚未开始慢操作**（编排/生成），采集只占一次 INSERT；
            #   也不必等整轮跑完 —— 中途失败或被用户中断的输入，同样是有价值的真实样本。
            # 只记 suggested、绝不自动 confirmed：评测只吃人工确认（见 intent_sample_repo 注释）。
            # ⚠️ 2026-09-26 补：**编排子任务不采集** —— worker 跑子任务时也走本入口（query 是内部构造的
            #   `[任务上下文快照]…`），实测被采进池 8 条内部文本，污染了"真实用户说法"样本。
            #   `_orch_subtask` 正是 worker 打的标记（见本文件 _worker 内 `sub._orch_subtask = True`）。
            try:
                if not getattr(self, "_orch_subtask", False):
                    from repositories.intent_sample_repo import collect as _collect_sample
                    _collect_sample(user_input, intent=_detected or intent,
                                    route=_intent_meta.get("route", ""),
                                    conf=float(_intent_meta.get("confidence") or 0))
            except Exception:
                pass
            if _multi_intent:
                _intent_meta["multi_intent"] = _multi_intent["sequence"]
            # ★ 2026-10-27（用户："意图识别环节总是匹配到具体的 agent，不能提供更好理解的内容吗"）：
            #   stage 事件里原本只带 `agent`（内部实现名），用户看到的是 `→ 智源对接agent（HIL 2）`——
            #   那是"我们把它路由给了谁"，不是"我们理解成什么"。这里补一份**人能读懂的意图理解**
            #   （goal/need/basis/missing），前端折叠态显示「理解：…」，展开可看判据与待确认点。
            #   ⚠️ 位置纪律：必须在 `slots` 算完之后（goal 直接复用 task_decompose 的结果，零额外调用）。
            _brief = None
            try:
                from core import config as _cfg_brief
                if bool(_cfg_brief.get("intent", "brief_enabled", True)):
                    from agent import intent_brief as _ib
                    _brief = _ib.build(
                        user_input, intent=intent, agent_name=agent_def.name,
                        slots=slots, meta=_intent_meta,
                        label=getattr(self, "_INTENT_LABEL", {}).get(intent, ""))
            except Exception:
                _brief = None      # 摘要只是可读性增强，任何异常都不得影响主流程
            # 挂到实例上：落库（阶段 4 的 card_data.exec）与后续卡片组装都要用，
            #   而那段代码离这里 1500 行 —— 用实例属性比依赖局部变量跨段存活更可靠。
            self._last_intent_brief = _brief
            # ★ 2026-10-28（用户反馈③）：把「执行主体从哪来」**结构化**下发。
            #   背景：会话若绑定过主 Agent，它会覆盖意图路由（R1 决策），
            #   但此前只在 reasoning 里说一句（还掉进"AI 思考"框看不见）⇒
            #   用户看到"我只是让校验代码，怎么用上智源了"。
            #   现在前端可在「意图识别」这一行直接显示：本轮主体 + 来源（路由/会话绑定/手动选择）。
            _exec_subject = {
                "name": agent_def.name,
                "source": ("manual" if team else ("bound" if getattr(self, "_exec_subject_note", "") else "route")),
                "note": getattr(self, "_exec_subject_note", "") or "",
            }
            yield {"type": "stage", "name": "意图识别", "status": "done", "intent": intent,
                   "agent": agent_def.name, "hil_level": hil_level,
                   "route": _intent_meta["route"], "confidence": _intent_meta["confidence"],
                   "brief": _brief,
                   "exec_subject": _exec_subject}
            # P0-1 置信度三级决策：中置信（弱语义/继承/LLM<0.85）→ 澄清提示（不阻塞执行，供前端改选重发）
            # V2.7：指定智能体时同样给出提示（识别结果与执行 Agent 不一致时如实说明）
            if _intent_meta["needs_clarification"]:
                yield {"type": "clarify", "intent": intent, "confidence": _intent_meta["confidence"],
                       "route": _intent_meta["route"], "message": str(user_input),
                       "detected": _detected if forced_intent and _detected != intent else None,
                       "brief": _brief,
                       "candidates": self._clarify_candidates(intent)}
            # ── 承接轮判定（2026-10-17 米爸反馈，实证会话 9446）──
            #  上一轮走过 澄清→作答（answered 卡存在）且本轮是短输入 ⇒ 用户在回应
            #  AI 上一轮的邀请补充（「您可以直接告我分支名」→「取主分支最新版本数据」），
            #  不是开新话题。此时两道澄清门全跳过、直接执行：Agent 持有完整上下文，
            #  自行理解「用户在回答我上一轮的问题」。否则又被 1.4 固定意图清单
            #  「我不确定你想做哪件事」拦住，上下文生生断裂（米爸截图现象）。
            try:
                _clarify_followup = self._is_clarify_followup(conversation_id, user_input)
            except Exception:
                _clarify_followup = False
            if _clarify_followup:
                yield {"type": "reasoning", "delta": (
                    "（承接上轮）检测到你在补充上一轮的关键信息，已跳过澄清确认，"
                    "结合上下文继续执行")}
            # ── 阶段 1.4（2026-09-26）：**意图确定不了 → 停下来问**，不要自己硬选一个（用户要求）──
            #  与上面那条 `clarify`（细条，"先按猜的跑、你可改选重发"）的本质区别：这里**不执行**。
            #  复用内容级澄清的选择题卡（选项 + 其他/自定义 + 答完续跑），因为"不硬选"的完整机制
            #  （落挂起 → /clarify-answer → 带【澄清补充】续跑）后端已有，不另造一套状态。
            #  放在 `_clarify_detect` **之前**：意图都没定，谈"建模信息够不够"没有意义。
            # ★ 2026-10-17（米爸反馈「确认卡还是系统默认内容」）：承接轮不弹卡（上行）；
            #   确需确认时卡片内容改由 LLM 基于上下文动态生成（`_llm_intent_confirm_questions`：
            #   LLM 认为意图已明确 ⇒ 连卡都不弹；生成失败 ⇒ 兜底固定列表版）。
            try:
                _need_iq = (not _clarify_followup) and self._should_confirm_intent(user_input, _intent_meta)
            except Exception:
                _need_iq = False
            if _need_iq:
                # P0-5：把本次语义意见带进候选排序（只影响顺序，不构成路由决定）
                _iqs = None
                _llm_skip_to = ""
                try:
                    _llm_r = self._llm_intent_confirm_questions(user_input, intent, conversation_id)
                except Exception:
                    _llm_r = None
                if _llm_r and _llm_r[0] == "skip":
                    # LLM 结合上下文判定：意图其实明确（多为承接轮误触发）⇒ 不弹卡，直接执行
                    _llm_skip_to = _llm_r[1]
                    yield {"type": "reasoning", "delta": (
                        f"（意图确认）结合上下文判断你的事情已明确（{_llm_skip_to}），不再打断确认，继续执行")}
                elif _llm_r and _llm_r[0] == "ask":
                    _iqs = _llm_r[1]
                if not _iqs and not _llm_skip_to:
                    _iqs = self._intent_confirm_questions(user_input, intent,
                                                          _intent_meta.get("sem_alt"))
                if _iqs:
                    _mid = self._persist_clarify(conversation_id, user_input, intent, branch, attachments,
                                                 forced_intent, skill_name, team, _iqs)
                    yield {"type": "clarify_ask", "questions": _iqs, "intent": intent,
                           "title": "❓ 需要你确认几个关键信息",
                           "message": str(user_input)[:200]}
                    yield {"type": "done", "data": {
                        "ok": True, "clarify_asked": True, "intent": intent,
                        "content": "需要你确认后才能继续（详见澄清卡片）", "msg_type": "clarify",
                        "usage": {}, "questions": _iqs, "message_id": _mid or 0}}
                    return
            # ── 阶段 1.5：内容级澄清（信息不清晰 → 选择题确认，回答后续答；打断本次执行）──
            try:
                _clarify_qs = (None if _clarify_followup else self._clarify_detect(
                    user_input, intent, effective_provider,
                    forced_intent=_clarify_gate_intent, skill_name=skill_name))
            except Exception:
                _clarify_qs = None
            if _clarify_qs:
                _mid = self._persist_clarify(conversation_id, user_input, intent, branch, attachments,
                                             forced_intent, skill_name, team, _clarify_qs)
                yield {"type": "clarify_ask", "questions": _clarify_qs, "intent": intent,
                       "message": str(user_input)[:200]}
                yield {"type": "done", "data": {
                    "ok": True, "clarify_asked": True, "intent": intent,
                    "content": "需要您确认建模信息（详见澄清卡片，回答后将继续）", "msg_type": "clarify",
                    "usage": {}, "questions": _clarify_qs, "message_id": _mid or 0}}
                return
            # 子智能体执行：路由到的 Agent 开始执行
            yield {"type": "agent", "status": "run", "name": agent_def.name,
                   "display_name": agent_def.name, "intent": intent, "hil_level": hil_level}

            # ── P0-1：自动编排（真流式：子任务逐个执行并实时推送；简单任务回落单 Agent 直行）──
            # 复杂度判定 + 附件透传（带附件时仅规则信号可触发；附件为核心依据，优先单 Agent 直行）
            _orch = None
            if team_forced and not dry_run:
                # 团队模式：主 Agent（团队负责人）编排——意图识别/拆解/计划/分派/汇总
                # （走到这里说明 `_team_route_decision` 判为 orchestrate，不再是无条件编排）
                self._save_conversation_dst(conversation_id, intent, slots)
                yield from _yield_collect(self._stream_orchestrated_traced(
                    user_input, conversation_id, branch, intent, agent_def, hil_level,
                    kb_tags, attachments, slots, user, effective_provider, team_forced=True,
                    stage_hint=_stage_hint, team_def=team_def), _orch_acc)
                return
            if not forced_intent and not team_direct and self._needs_orchestration(user_input, intent,
                                                              has_attachments=bool(attachments),
                                                              multi=_multi_intent):
                # P0-1 复用：已发布 planner_auto 沉淀流程语义命中 → 直接执行（省重新规划）
                reused = self._try_reuse_planner_flow(user_input, intent)
                if reused and not dry_run:
                    self._save_conversation_dst(conversation_id, intent, slots)
                    yield {"type": "progress", "kind": "progress",
                           "delta": f"（自动编排）命中已发布沉淀流程「{reused.get('flow_name') or ''}」，按流程模板执行子任务…"}
                    reused["data"] = {"tasks": reused.get("plan") or []}
                    # ⚠️ **不传 run_id**：复用流程走的是「已发布流程模板」，
                    #   `reused` 是**预先算好的** orch（`_try_reuse_planner_flow` 返回），
                    #   本次**没有** create_plan、没有 agent_tasks 行 ⇒ 压根不存在批次号。
                    #   `execute_stream` 里也没有 `run_id` 局部变量（真 run_id 是
                    #   `_stream_orchestrated_flow` 的局部变量）—— 在这里传 `run_id=`
                    #   会把 NameError 从被调方挪到调用方，**等于没修**。
                    yield from _yield_collect(self._stream_orchestrated(
                        reused, user_input, conversation_id, intent, agent_def, hil_level,
                        kb_tags, attachments, slots, user), _orch_acc)
                    return
                try:
                    # P0-2 DST：编排流式前落会话意图状态（子管道不写主会话状态）
                    if not dry_run:
                        self._save_conversation_dst(conversation_id, intent, slots)
                    yield from _yield_collect(self._stream_orchestrated_traced(
                        user_input, conversation_id, branch, intent, agent_def, hil_level,
                        kb_tags, attachments, slots, user, effective_provider,
                        stage_hint=_stage_hint), _orch_acc)
                    return
                except Exception as _orch_exc:
                    # P0-3 补（2026-09-19，端到端取证）：**编排中途失败不得静默回退**。
                    # 此前是裸 `except Exception: _orch = None` —— 子任务已全部执行（用户已看到 subtask
                    # 事件）却在此处被吞掉，随后**回落到单 Agent 重跑一遍**并落库单 Agent 卡片：
                    # 用户看到的执行过程与落库轨迹互相矛盾，且日志/事件里没有任何痕迹。
                    # 实测（会话 361）：3 个子任务全 done、日志 5 次 sysml-check，而落库卡片的键集是
                    # 单 Agent 的（含 citations/submitted_by）→ 判据：**卡片键集就是落库路径的指纹**。
                    # 现改为：打完整 traceback（便于定位真因）+ 向前端发可见提示，再走原有单 Agent 兜底。
                    try:
                        import traceback as _tb
                        print(f"[orch] 编排流式失败，回落单 Agent 直行："
                              f"{type(_orch_exc).__name__}: {_orch_exc}\n{_tb.format_exc()}", flush=True)
                    except Exception:
                        pass
                    yield {"type": "progress", "kind": "progress", "delta": (
                        "（自动编排）编排执行中断（" + type(_orch_exc).__name__ +
                        "），已回落为单 Agent 直行重新作答；上方子任务轨迹仅供排查，最终结论以本次答复为准")}
                    _orch = None

            # 闭环：上传资料解析 + 工作流匹配
            att_blocks, att_parsed, att_skipped = self._load_attachment_text(attachments)
            # P2 视觉通道（2026-09-21）：与 execute.py 同构 —— 开关 + provider 能力双判据，
            # 不具备则留痕降级（原因进 att_vision → attachments_info.vision），不静默丢弃。
            att_images, att_vision = self._prepare_attachment_vision(attachments, effective_provider)
            if att_vision.get("skipped"):
                att_skipped = list(att_skipped) + list(att_vision["skipped"])
            if att_images:
                att_blocks = [f"【图片附件】{'、'.join(att_vision.get('loaded') or [])}"
                              "（图像内容已随本条消息一并提供，请直接查看）"] + att_blocks
            # 2026-09-17 S3：注入上限收到 _ATT_INJECT_CAP（att_text 排在检索段之前，不受 _apply_context_budget 裁剪）
            att_text = ("\n\n".join(att_blocks[:4]))[:_ATT_INJECT_CAP] if att_blocks else ""   # 注入 prompt（限量）
            retrieval_att = "\n\n".join(att_blocks) if att_blocks else "" # 完整全文（供附件召回）
            # ★ 2026-11-13（米爸反馈「上传附件做需求分析，仍未完整读取」，截图实证只读到
            #   第 4.1、第 5/6/7 章条款整段不在上下文）：上方 att_text 注入侧只取前 4 块再
            #   砍到 4000 字符，而 retrieval_att（完整全文）**只喂 RAG 召回**、且附件默认
            #   不触发检索（should_retrieve 门控）⇒ 后半段章节**从来没进过 prompt**。
            #   这不是解析问题（#51 已把全文解析出来），是**注入问题**。
            #   分层注入（用户选定方案）：正文首部（保 token） + **章节提纲**（补完整性认知）
            #   + 全文留在召回/工具通道。提纲的价值不是"多给字"，而是让模型**知道全文有哪些
            #   章**，从而不会把"我只看到前 4 章"当成文档全貌（这正是截图里模型自述的误判）。
            _att_outline = self._attachment_outline(att_blocks) if att_blocks else ""
            # ★ 2026-11-13（#56）：**待取回章节清单** —— 把"哪几章正文没读到"变成**确定性事实**
            #   交给模型，而不是让它自己猜。只给提纲时，模型分不清"哪些章已在上下文里"、
            #   哪些还没读 ⇒ 它会保守地说"第 5/6/7 章正文缺失"却**不确定该不该调工具**
            #   ⇒ 回头找用户要原文（实测截图原话）。
            #   判据见 `_attachment_pending_sections`（章节区间 vs 注入边界，确定性）。
            _att_pending = self._attachment_pending_sections(att_blocks) if att_blocks else []
            if _att_outline:
                att_text = (
                    att_text +
                    "\n\n【上传资料·章节提纲（全文共 %d 字，以下为已识别的章节标题——"
                    "**你只看到了上面那份正文的前段**，后续章节的条款正文尚未注入，"
                    "**请用 read_attachment_section 工具按章节号自行取回**，"
                    "不要因为正文里没有就认为文档不含该章、更不要向用户索要原文）】\n%s"
                    % (len(retrieval_att), _att_outline)
                )
                if _att_pending:
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
                        % "、".join(_att_pending)
                    )
            # ★ 2026-11-13（#55）：建章节索引 + 缓存全文，供 read_attachment_section 取章。
            #   上一版提纲只解决「模型知道有第5/6/7 章」，没解决「它拿不到」⇒ 实测模型
            #   只能反问用户「请把第5、6、7 章正文贴进来」。此处把「按需取回」的能力接上。
            self._index_attachment_sections(att_blocks)
            att_parsed_info = {"parsed": att_parsed, "skipped": att_skipped[:5]}
            if att_vision.get("images"):
                att_parsed_info["vision"] = att_vision   # 留痕：图片到底进没进模型、为什么
            matched_flows = self._match_flows(user_input + (" " + att_text[:500] if att_text else ""))

            # ── 阶段 2：知识检索（#标签 + 附件前 600 字符并入）──
            # 按需检索：#标签主动引用 / Agent kb_required 配置 二选一才检索
            # V3：上传附件不再默认触发知识库检索（附件仍作「优先依据」注入 prompt）
            should_retrieve = (bool(req_scope and req_scope.get('ok')) or bool(kb_tags) or bool(agent_def.kb_required)
                               or (intent == "report_generation" and report_type in ("impact", "review")))
            # ★ 2026-11-11：stage 事件**移到 should_retrieve 判定之后**。
            #   此前 `yield {"type":"stage","name":"知识库检索","status":"run"}` 是**无条件**的
            #   （在 if 之前）⇒ 即使本轮根本没检索（`route_reason="kb_optional"`），
            #   用户仍会看到一个「知识库检索·未引用」的幽灵环节——
            #   这正是米爸反馈"为啥每次都有查询知识库的过程"的直接来源。
            #   口径：**没真检索就不要出现这个环节**（不是隐藏结果，而是根本不宣称做了）。
            if should_retrieve:
                yield {"type": "stage", "name": "知识库检索", "status": "run"}
                retrieve_query = user_input + kb_hint
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
            # ★ P0-4（2026-10-26）：把 RAG 侧已召回的 memory_hits 挂到 pipeline 实例，
            #   供 `_build_memory_hint` 消费（**单一真源**：记忆 recall 层不再二次查库）。
            #   必须在 `_build_context` 之前赋值 —— 两者消费同一份召回结果。
            self._rag_memory_hits = retrieval.get("memory_hits") or []
            if should_retrieve:
                context_text = self._build_context(retrieval, retrieve_query)
                if req_scope and req_scope.get('ok'):
                    context_text = ('（本次建模范围已锁定 {n} 篇文档，片段证据优先，仅基于范围内数据检索）\n'.format(n=len(effective_kb_scope.get('docs') or [])) + context_text)
            elif att_text:
                context_text = "（本次仅基于上传资料与模型知识回答，未检索知识库——可在输入加 #工程数据 / #知识库 标签主动引用，或由 Agent 配置启用知识库依赖）"
            else:
                context_text = "（未引用知识库——可在输入加 #工程数据 / #知识库 标签主动引用，或由 Agent 配置启用知识库依赖）"
            # P0 需求质量分析（流式路径同注入）
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
            # ★ 2026-10-16（米爸反馈「为啥每次都有知识库检索这个环节」· 补齐漏改的一处）：
            #   `done` 事件原先**无条件发射**，于是 should_retrieve=False 时前端
            #   `onStageEvent` 仍会 `procAddStage('知识库检索','done','未引用')`
            #   ⇒ 用户每轮都看到一个"完成了但其实没做"的幽灵环节。
            #   注意这与 2108-2113 修的是**同一处语义的两半**：那一轮只把 `run` 移进了
            #   if，`done` 忘了一起移 ⇒ 幽灵环节依旧（"同一个量在多个环节各写一份必对不上"）。
            #   另两处路径（本文件 69-72 编排规划、1507-1510 编排复用）早已整段移除，
            #   只剩主直行路径漏改 —— 三处各写一份，改两处漏一处是必然。
            #   口径：没真检索就**一个事件都不发**（不是隐藏结果，而是根本不宣称做了）。
            if should_retrieve:
                yield {"type": "stage", "name": "知识库检索", "status": "done",
                       "retrieved": should_retrieve, "route": retrieval.get("route", "none"),
                       "route_reason": retrieval.get("route_reason", ""),
                       "kb_scope_warn": retrieval.get("kb_scope_warn")}   # KB-S：白名单失效告警（供前端/抓包）
            # KB-S（2026-09-19）：Agent 文档白名单失效 → 显式提示。否则表现为「Agent 配了知识库依赖
            # 却检索恒为空」，调用方无法区分「库里没有」与「白名单写错了」（实测 4887 块规范恒不可见）。
            _kbw = retrieval.get("kb_scope_warn")
            if _kbw:
                yield {"type": "progress", "kind": "progress", "delta": (
                    "（知识库范围）Agent 配置的文档白名单有 "
                    f"{len(_kbw.get('missing') or [])} 篇不存在，已自动剔除"
                    + ("；因全部失效，本次已暂放宽为不限文档" if _kbw.get("unfiltered") else "")
                    + "。建议到「能力中心 → Agent 管理」核对该 Agent 的知识库范围配置")}
            # ★ 2026-10-27（真机 E2E 暴露）：**技能预匹配**必须发生在此处（reasoning/skill 事件之前）。
            #   旧顺序：skill 事件 yield（旧 4165~4172）→ `build_prompt_blocks`（旧 4254）才调
            #   `_build_skill_prompt` ⇒ `_last_skill_hits` 恒为空 ⇒ 前端「🧩 技能执行」卡永远空白。
            #   修法：**提前算一次 + 缓存复用**（`_build_skill_prompt` 内置本轮缓存）——
            #   随后 `build_prompt_blocks` 命中同一缓存 ⇒ 不重复匹配、不重复 offload
            #   （offload 会写 `skill_body_offloads` 表，重复做会留垃圾行）。
            #   ⚠️ 只算一次是硬要求：匹配里含 LLM 兜底（`_llm_pick_skills`），算两次 = 多烧一次 LLM。
            #
            # ★ 状态起点（`reset_skill_state`）**同步前移到此处**：
            #   它必须早于预匹配，否则 `_skill_forced` 还是上一轮的值 ⇒
            #   ① 缓存键错（`_skill_forced` 参与键）⇒ 复用失效 ⇒ 匹配算两次；
            #   ② `_skill_allowed_tools` 被 reset 清零 ⇒ 预匹配并出的白名单丢失。
            #   前移安全性：它只重置两个实例属性（`_skill_forced` / `_skill_allowed_tools`），
            #   且本函数内 3619~4165 之间无任何依赖这两个属性的逻辑（已核）。
            reset_skill_state(self, skill_name)
            try:
                self._build_skill_prompt(intent, user_input, user)
            except Exception:
                pass
            # 思考过程：检索决策 + 自动触发技能（真实执行信息，普通模型不流 reasoning 时也可见）
            if should_retrieve:
                route = retrieval.get("route", "none")
                _r_delta = (f"（{agent_def.name}）完成知识检索：图谱 {retrieval['graph_count']} 条 / "
                            f"向量 {retrieval['vector_count']} 条（路由 {route}），将基于检索到的互联数据组织回答。")
            else:
                _r_delta = (f"（{agent_def.name}）本次未触发知识检索（纯问答/无 # 标签），直接基于专业知识与上传资料生成回答。")
            if self._last_skill_hits:
                _r_delta += f" 自动触发技能：{'、'.join(self._last_skill_hits)}。"
                # V2.5：技能执行环节独立事件（前端「🧩 技能执行」卡，命中技能与触发说明可见）
                # ★ 2026-11-11 补层级字段（depth/agent/mcp），供前端三级展示归组。
                # ⚠️ 2026-10-27 真机 E2E 暴露：此处**不能**引用 `_ctx_agent` ——
                #   它在后面的工具循环段才定义（同一文件另一处 skill 事件已注明此坑）。
                #   本分支此前**从未被执行过**（`_last_skill_hits` 恒空 ⇒ 整块跳过），
                #   所以这个前向引用一直潜伏着；预匹配修好后才暴露 ⇒
                #   直接报 `cannot access local variable '_ctx_agent'`。
                #   与同文件另一处保持一致：直接取 `agent_def.name`（值等价，无耦合）。
                yield {"type": "skill", "status": "done", "names": list(self._last_skill_hits),
                       "agent": (agent_def.name or "-"), "depth": 1,
                       "mcp": None,
                       "note": "技能指令已注入生成阶段（触发词命中，指导本回答的组织方式与工具白名单）"}
            exec_reasoning.append(_r_delta)
            yield {"type": "reasoning", "delta": _r_delta}

            # ── 阶段 3：LLM 流式生成 ──
            yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "run"}
            # 快捷指定 Skill：强制注入该技能完整指令
            skill_block = ""
            # 通用报告服务：report_generation 意图 → 分节模板结构化生成
            # SP-R：类型化报告素材增强（变更影响 → BFS 影响图；预评审 → 校验清单）+ 类型模板
            report_prompt = ""
            if intent == "report_generation":
                try:
                    from report_generator import report_generator as _rg
                    if report_type in ("impact", "review"):
                        _mat = self._build_report_material(report_type, retrieval)
                        if _mat:
                            context_text = context_text + "\n\n" + _mat
                    _sections = _rg.sections_for(report_type, user_input)
                    report_prompt = _rg.build_prompt(user_input[:80], context_text,
                                                     sections=_sections, report_type=report_type)
                except Exception:
                    report_prompt = ""
            # ★ 2026-10-08 修**顺序 bug**（与 execute.py 同源）：
            #   `_tool_conv_ctx` 原在下方 1710 行才赋值，而技能装配
            #   （`build_prompt_blocks` → `_build_skill_prompt` → `offload_body`）
            #   在 1656 行就要读它取会话 id ⇒ 永远读到**上一轮**的值。
            #   后果：长 skill 正文的 offload 挂错会话，`skill_body_fetch` 取回时 id 对不上
            #   ⇒ 多轮/多会话连续调用时产出互相串扰（实测两个视图产出 md5 完全相同）。
            self._tool_conv_ctx = conversation_id
            # ★ 2026-10-27（会话 9629）：**异步任务跨轮续跑必须发生在 prompt 组装之前**。
            #   ⚠️ 踩过的坑（本轮实测）：最初把它写在 ReAct 循环起点（messages 组装之后），
            #   结果 `_async_notes` 要么还没算出来、要么被下一轮的初始化清空，
            #   注入到 system prompt 的永远是空列表 ⇒ 模型完全不知道"上次的导出已经跑完"，
            #   于是照样从零 PREVIEW/EXPORT（探针实测：仍重新提交了一次导出）。
            #   顺序铁律：先跑续跑 → 再拼 prompt → 再进循环。
            self._tool_sysml_docs = []
            self._tool_sysml_docs_seen = set()
            self._tool_doc_pages = {}
            self._async_notes = []
            try:
                from agent import async_jobs as _aj0
                _ajr0 = _aj0.resume_jobs(self, conversation_id)
                self._async_notes = list(_ajr0.get("notes") or [])
                for _d0 in (_ajr0.get("docs") or []):
                    _dk0 = _d0["text"][:200]
                    if _dk0 not in self._tool_sysml_docs_seen:
                        self._tool_sysml_docs_seen.add(_dk0)
                        self._tool_sysml_docs.append(_d0)
            except Exception:
                self._async_notes = []
            # P1-29（2026-10-02）：状态起点统一 —— 此前本路径**不设** `_skill_forced`，
            # 导致「显式指定技能」的白名单授权在对话主路径上不生效（`skills.py:153/163`
            # 会按"未强制"重置并只并集*自动命中*技能的白名单）；且 `_skill_allowed_tools`
            # 无每轮清零，全局单例下会跨轮残留。
            # ★ 2026-10-27：`reset_skill_state(self, skill_name)` 已**前移**到本文件
            #   「技能预匹配」处（reasoning 之前）—— 若在此再调一次，会把预匹配并出的
            #   `_skill_allowed_tools` 清零 ⇒ 白名单丢失 ⇒ 命中技能的独占式白名单失效。
            #   （`reset_skill_state` 的语义是"每轮状态起点"，前移后位置更正确。）
            if skill_name:
                # P1-29：读取入口统一为 `load_forced_skill`（StudioRepo `SELECT *` 含
                # references/examples/scripts/allowed_tools）。此前本处用只查 3 列的裸 SQL
                # ⇒ 渐进披露**无数据可用**。`content_cap` 仍是流式侧的有意差异（控 token）；
                # `require_content=True` 保留"正文为空不注入"的原行为。
                skill_block, _at = load_forced_skill(skill_name,
                                                     content_cap=_SKILL_CONTENT_CAP,
                                                     include_resources=True,
                                                     require_content=True)
                if _at:
                    self._skill_allowed_tools = _at
                if skill_block:
                    # V2.5：手动指定技能也发独立事件（前端「🧩 技能执行」卡可见）
                    # ★ 2026-11-11 补层级字段。注意此处**早于** `_ctx_agent` 的定义
                    #   （在技能加载段，而 _ctx_agent 在工具循环段），故此处直接取
                    #   `agent_def.name` 而不引用 `_ctx_agent` —— 值等价，但避免了
                    #   "前向引用" 的隐性耦合（门禁 I2 正是抓到这处漏了字段）。
                    yield {"type": "skill", "status": "done", "names": [skill_name],
                           "agent": (agent_def.name or "-"), "depth": 1, "mcp": None,
                           "note": "指定技能指令已注入生成阶段（完整指令强制遵循）"}
                    pass
            # P1-28（2026-10-02）：**块内容**收敛到 `common.build_prompt_blocks`（唯一真源）。
            # 本路径（流式对话 = 前端唯一入口）与 execute 路径的差异一律**显式参数化**：
            #   · role_cap=_AGENT_ROLE_CAP  按 S4 截断 Agent 角色块（控 token；execute 不截断）
            #   · include_tool_rules=True   本路径注入工具使用约束（execute 原本没有该块）
            # ⚠️ 不要"顺手"把这两项改成与 execute 一致 —— 会改变主路径产出（§29.9）。
            system_prompt = assemble_system_prompt(build_prompt_blocks(self, PromptBlocksCtx(
                agent_def=agent_def, intent=intent, hil_level=hil_level,
                user_input=user_input, user=user, branch=branch,
                conversation_id=conversation_id, slots=slots, user_ctx=user_ctx,
                att_text=att_text, context_text=context_text, report_prompt=report_prompt,
                skill_block=skill_block, role_cap=_AGENT_ROLE_CAP, include_tool_rules=True,
            )))
            # P2 视觉通道：图片以多模态 content 块随 user 消息下发（provider 层原样透传）
            _user_content = user_input
            if att_images:
                _user_content = [{"type": "text", "text": user_input}] + att_images
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _user_content},
            ]
            # 闭环：会话历史注入（v2 话题感知：当前话题原文 + 语义拉回 + 分话题摘要）
            if conversation_id:
                hist = self._load_history(conversation_id, cur_input=user_input)
                if hist:
                    messages = [messages[0]] + hist + messages[1:]
            # ★ 2026-10-27（会话 9629）：把「系统托管的异步任务」事实注入 system prompt。
            #   为什么必须注入而不是靠历史对话：实测模型**每轮都从零重来**
            #   （重查工程→分支→版本→PREVIEW→EXPORT），把整轮预算耗在重复劳动上，
            #   而它真正该做的是"接着上次那个 taskId 往下走"。历史里虽有 taskId，
            #   但模型不会主动把它当现场；系统陈述一句话，就等于给了它确定的现场。
            #   注意：这些是**系统实测事实**（DB + 真 STATUS 调用），不是模型推断。
            if getattr(self, "_async_notes", None):
                messages[0]["content"] += (
                    "\n\n【系统·异步任务现场（实测，非推断）】\n"
                    + "\n".join("- " + str(n) for n in self._async_notes)
                    + "\n以上结论来自系统对任务状态的真实查询，请以此为准；"
                      "不要凭记忆或推断宣称「已归档/已存入产物库」。")
            # ★ 2026-10-27（用户质疑"前面已确定工程，是不是上下文遗漏了"——实测确认会丢）：
            #   把**会话级坐标**（工程/分支/版本/目标元素）作为独立块注入，与历史消息解耦。
            #   为什么不能靠历史：`_load_history` 只取当前话题最近 8 条 + 3000 字符预算，
            #   用户"基于这个工程 XXX"这类**锚点句**一旦滑出窗口就永久丢失（只能靠语义回捞，
            #   而当前 embedding 404 降级 bigram，召回不可靠）。conv 9629 就是实例：
            #   msg 4869 定工程 → msg 4899 提问，相隔 17 条，锚点已在窗口外。
            try:
                from agent import conv_anchors as _ca2
                from database import db_conn as _cadb2
                with _cadb2() as _cac2:
                    _anc = _ca2.load_anchors(_cac2, conversation_id)
                _ab = _ca2.render_block(_anc)
                if _ab:
                    messages[0]["content"] += _ab
            except Exception:
                pass
            # 2026-09-29（用户五轮反馈1）：续作短语（重试/继续）→ 显式续作指令。
            #  此前 AI 表现为"不知道之前的内容"：carry 词表没有"重试"→ 话题切新段，
            #  当前话题原文只剩两字；且被终止那轮的产出未落库（尾部 except 现已固化）。
            #  双保险：话题承接（history.py carry）+ 这里把"衔接上文"写成硬指令。
            if self._is_continuation_input(user_input):
                messages[0]["content"] += (
                    "\n\n【续作指令】用户本条输入是续作短语（如\"重试/继续\"），不是新任务。"
                    "请把上方会话历史当作进行中的任务现场：先根据历史判断此前进行到哪一步、已有产出是什么，"
                    "然后接着往下推进（或按语义重做失败的部分）；禁止当作全新请求从零开始，"
                    "也不要大段复述已完成内容，直接给出衔接后的增量产出。")
            # 2026-09-17 S3：SSE 路径此前完全无上下文预算（_apply_context_budget 仅 execute 路径调用）
            try:
                messages[0]["content"] = self._apply_context_budget(messages[0]["content"], context_text, len(messages))
            except Exception:
                pass
            # P1-2（2026-09-24）：总闸——分段预算之和可超窗，发送前算总账（预算内零开销）
            try:
                # 传 provider_id ⇒ 上限按**该 provider 的真实窗口**自适应
                # （2026-11-11：原为硬编码 10000，而生产 provider 均为 128K，
                #  10K 上限会把 13.7k 的正常请求误判为超窗并强行压缩）
                messages = self._apply_total_budget(messages, context_text,
                                                     provider_id=effective_provider)
            except Exception:
                pass
            # UX规范 D1（2026-10-03）压缩三件套：总闸触发过 → 透传 compact 事件，
            # 前端时间线渲染「上下文已压缩」系统标记（可展开详情 + 用量指示器 + 追问恢复入口）。
            # 压缩只发生在 prompt 组装层，历史原文全在库——故"回退"语义 = 引导追问恢复，不伪造撤销。
            _cmp_info = getattr(self, "_last_compaction", None)
            if _cmp_info:
                try:
                    yield {"type": "compact", **_cmp_info}
                except Exception:
                    pass
                self._last_compaction = None
            # 缺口B：工具判定——非流式探测 LLM 是否需要工具（Mock 无 tool_calls 时跳过，保持原流式）
            # 优化1：注入观测上下文
            self._tool_intent_ctx = {"intent": intent}
            self._tool_agent_ctx = {"agent": agent_def.name}
            # `_tool_conv_ctx` 已在 prompt 装配**之前**设好（见上方注释：原在此处赋值太晚，
            # 导致技能 offload 读到上一轮会话 id）—— 此处保留一次幂等赋值以防
            # 未来有人在中间插入分支时依赖它。
            self._tool_conv_ctx = conversation_id
            # P0-6（2026-10-06）：透传当前用户给工具链权限闸（与 execute.py 同口径）。
            # stream 是**生产主路径**（execute 是冷路径），权限闸必须在这里也生效，
            # 否则「生产走 stream、自检走 execute」会给出虚假的安全结论。
            self._tool_user = user
            tools_def = self._build_tools_def(intent, user_input, user)
            # ★ 2026-10-17（米爸「harness 持续工作」）：自主续跑（auto-continue）就绪。
            #   为什么需要外层循环：模型「想停」和「该停」是两件事——没有完成度评估器时，
            #   多步任务模型常在第一步后就收尾等用户吩咐（米爸原话：「每执行一轮都会结束，
            #   让我继续下达指令才会进行下一步」）。方案对齐 Claude Code /goal：每轮工作结束后
            #   由**完成度评估器**判定放行还是打回；四道闸（次数/墙钟/token/空转）全部
            #   fail-safe 向「停」（AutoGPT「无完成任务生成/无上限成本」教训的沉淀）。
            #   判定纯函数在 agent/auto_continue.py（decide/is_spinning/parse_verdict，可单测），
            #   IO（config 读取 + LLM 评估调用）在 AutoPilotMixin（pipeline_parts/autopilot.py）。
            from agent import auto_continue as _ac
            _auto_continues = 0
            _ac_cfg = self._auto_continue_cfg()
            _ac_seen_steps = []
            # ★ 2026-10-28：澄清延后计数（见评估点"待办未做完时先别问人"）。
            #   `_clarify_defer_n` 上限 2 次、`_clarify_defer_rem` 记上次剩余项数
            #   （要求确实在减少才允许继续延后，否则照常转卡）。
            _clarify_defer_n = 0
            _clarify_defer_rem = None
            # ★ 2026-10-17（米爸反馈4）：工具结果里取回的完整 SysML 文档暂存。
            #   ⚠️ 2026-10-27 起**初始化已前移到 prompt 组装之前**（与异步续跑一起），
            #   这里不再重复置空 —— 否则会把刚续跑取回的文档清掉（本轮实测踩过）。
            _ac_deadline = ((time.time() + _ac_cfg["deadline_s"])
                            if (_ac_cfg["enabled"] and _ac_cfg["deadline_s"] > 0) else None)
            _tools_def_full = tools_def   # 每轮续跑恢复完整工具集（offload 注入/预算闸会改它）
            tool_injected = False
            llm_content = ""
            # ★ 2026-10-27：**连续重复调用**计数（跨 ReAct 轮累计、真执行即归零）。
            #   用于"重复调用收敛闸"，见 round 末尾 `_dup_streak >= 2` 处注释。
            #   初值必须在**外层循环之外**：跨轮累计才有意义（每轮归零就永远打不到 2）。
            _dup_streak = 0
            # P0-2：多轮 ReAct——探测→执行工具→观察回填→再探测，直到无 tool_calls 或达上限（防死循环）
            # ★ 2026-10-09：3 → 6 且可配置，**与 execute.py 同源同值**。
            #   实测依据见 execute.py 同位置注释：3 轮预算在
            #   「查标准库 → 取回技能正文 → 生成」这条典型序列下会在生成前耗尽。
            # ★★ 2026-10-09（米爸纠正语义，与 execute.py **同源**）：
            #   6 → 12 且拆出**失败轮预算**——成功推进不扣，只有失败轮才扣。
            #   旧实现把成功轮也计入总轮数，成功推进被失败轮"连坐"截断。
            #   ⚠️ 必须与 execute.py 的默认值**一致**（tools/verify/verify_tool_round_budget.py 会判）。
            #   ★ 2026-10-17（米爸反馈1）：12 → 100。截图实证：模型因「工具调用次数已用尽」
            #     中断正常工作（FINALIZE_HINT 收尾）。语义分工：成功推进本就不扣失败预算，
            #     100 只是防死循环的**绝对天花板**（+ 空转检测/token 预算/自主续跑四道闸兜底），
            #     正常长任务（先查标准库→取技能→生成→校验→修复）不再被 12 轮掐断。
            try:
                from core import config as _cfg
                max_tool_rounds = int(_cfg.get("agent", "max_tool_rounds", 100))
                max_failed_rounds = int(_cfg.get("agent", "max_failed_rounds", 3))
                # ★ 2026-10-17（米爸反馈1）：token 预算 60000 → 30 万（可配）。
                #   100 轮的合法长任务 6 万必提前触闸 —— 中断点从轮次挪到 token 等于没修。
                _lg_budget = int(_cfg.get("agent", "loop_token_budget", 300000) or 300000)
                _lg_retries = max(0, int(_cfg.get("agent", "tool_call_retries", 3) or 0))
                _lg_retry_wait = float(_cfg.get("agent", "tool_retry_timeout_s", 30) or 0)
            except Exception:
                max_tool_rounds, max_failed_rounds = 100, 3
                _lg_budget, _lg_retries, _lg_retry_wait = 300000, 3, 30.0
            # 2026-10-04：接入统一护栏（与 execute.py 的非流式 loop **同源**）。
            # 为什么必须同源：这两段是同一套 ReAct 语义的两种实现，此前只有编排路径
            # 有 token 预算，重复检测与工具超时两边都没有 ⇒ 补一边就会留下新的不对称。
            from agent.loop_guard import LoopGuard as _LoopGuard
            # P0-①/P0-②（2026-10-07）协议护栏：并行 tool_calls 配对 + 截断参数拒绝。
            # 与 execute.py **同源**（同一模块）—— 两条路径是同一套 ReAct 语义的两种实现，
            # 协议层缺陷漏一条路径就是线上 HTTP 400（同 loop_guard 立规时的判断）。
            from agent import loop_protocol as _lproto
            _lg = _LoopGuard(token_budget=_lg_budget, tool_timeout_s=120.0,
                             max_failed_rounds=max_failed_rounds, tool_retries=_lg_retries,
                             retry_timeout_s=_lg_retry_wait)
            # ★ 2026-10-27（真机 E2E，conv 9698）：本轮**已成功执行的写操作**清单。
            #   放在 while **外**（跨自主续跑轮累积）—— 写操作一旦执行成功就**一直成立**，
            #   不该因为进入下一续跑轮而被"忘掉"（那正是模型误判「尚未写入」的成因之一）。
            _write_ops = []
            # ★ 2026-10-28（写前确认 HITL）：本轮是否命中"写前确认闸"。
            #   形状刻意对齐 `clarify_required`（`_clarify_req`）⇒ ReAct 循环与外层收口
            #   用**同一套**中断协议（检出即 break → 发事件 → 落挂起 → 等人 → 新一轮）。
            _hil_confirm_req = None
            # ★ P0-2（2026-10-28）harness 强制闸的**连续阻断计数**。
            #   放在 while **外**（跨轮累积）—— 闸的语义是"连续 N 次没通过验收
            #   就强制放行"，计数必须跨轮连续，否则每轮都从 0 数 ⇒ 永不触顶
            #   ⇒ 会变成 FM-1.5「该停不停」12.4% 的温床。
            _harness_blocks = 0
            # ★ 无进展检测（真机实测驱动）：记录上轮 remaining 与连续无变化次数。
            #   只靠次数上限 8 会连续拦截 8 次且待办毫无变化，耗时 ≈2.3 分钟而结果相同 —— 必须提前放行。
            _harness_prev_rem = []
            _harness_stall = 0
            # ★ 2026-10-17（米爸「harness 持续工作」）：自主续跑外层循环。
            #   round 1 = 原始请求；round 2+ = 评估器判定「未完成」放行的续跑。
            #   _lg（LoopGuard）**故意在循环外创建**：token 预算跨轮共享（config 既定语义），
            #   失败轮预算与去重缓存同样跨轮保持——宁可早停，不放开火烧（boundedness 铁律）。
            while True:
                # ── 每轮状态复位（续跑轮必须干净起步，否则带上轮半成品）──
                tool_injected = False
                llm_content = ""
                _ctx_skill = None           # None = 未命中技能（工具直接挂 agent 下）
                _clarify_req = None
                _rnd = -1
                _prev_tool_idx = []
                _fu_budget = 3              # 续跑轮同样限 3 次补充（与首轮同口径）
                _fu_used = 0
                tools_def = _tools_def_full  # 恢复完整工具集（offload 注入/预算闸会改它）
                # ★ P1-1（2026-10-26）：**语义缓存（流式路径）**。
                #
                # 问题（实测）：`semantic_cache` 只在 execute.py:477/795 使用，stream.py **0 处引用**；
                #   而生产 SSE 入口（routers/conversations.py:391）走 execute_stream
                #   ⇒ **主对话完全不享受语义缓存**（chat_probe 占真实 token 的 51%）。
                #   而且原闸门 `not tools_def` **恒为 False**（chat 恒注入 core_keep + ask_user）
                #   ⇒ 该特性在两条路径上**从未命中过一次**，是彻底的死代码。
                #
                # 位置纪律（踩过两次坑）：必须在 **ReAct 之前**、且**不能**依赖 `not tool_injected`——
                #   实测 `tool_injected` 在 ReAct 探测轮无 tool_calls 时也会被置 True
                #   （stream.py 探测轮分支 `tool_injected = True`）⇒ 放在生成段前的判定恒为假。
                #   故：命中直接**短路整轮 ReAct**（tools_def=None + tool_injected=True），
                #   让后续落库/done/审计/citations 全部沿用原路径（零分叉，不做早期 return）。
                #
                # 闸门：intent ∈ {chat, knowledge_qa} · 无附件 · 无澄清 · 非 dry_run/resume。
                #   ⚠️ 不再要求 `not tools_def`：安全性由**写入侧**保证（只有「本轮没调任何工具、
                #   无附件、无澄清」的纯问答答案才入库）⇒ 命中天然是"无需工具"的结论。
                _cache_answer = None
                try:
                    from core import config as _cfg_sc
                    _sc_on = _cfg_sc.as_bool("semantic_cache", "enabled", False)
                except Exception:
                    _sc_on = False
                if (bool(_sc_on) and intent in ("chat", "knowledge_qa")
                        and not att_blocks and not _clarify_req
                        and not dry_run and not resume_run_id and not resume_of_user_msg_id):
                    try:
                        from semantic_cache import SemanticCache
                        from database import get_db
                        _cc = get_db()
                        try:
                            _cache_answer = SemanticCache.get(_cc, user_input)
                        finally:
                            _cc.close()
                    except Exception:
                        _cache_answer = None      # 缓存不可用 ⇒ 如实走真实生成
                if _cache_answer:
                    # 按类人节奏"假装流式"（前端体验与真实流一致，不是啪一下一大段）
                    # ★ 2026-10-27（断点保护）：**先赋值、后吐字**。
                    #   原写法是「先 for 吐完 → 再 llm_content = ...」，中途被 close
                    #   （客户端断开）时 llm_content 还是空 ⇒ 固化拿到 0 字符 ⇒ 用户看到的
                    #   内容一条都不落库（真库 conv 9663 实证：reason=stop 候选字符=0）。
                    llm_content = _cache_answer
                    _streamed_acc[0] += _cache_answer
                    for _ck in iter_stream_chunks(_cache_answer):
                        yield {"type": "token", "delta": _ck}
                    tool_injected = True       # 让下方生成段跳过（答案已就绪）
                    tools_def = None           # 让 ReAct 立即 break（不再调工具）
                    try:
                        yield {"type": "reasoning", "delta": "（语义缓存命中）",
                               "round": _rnd + 1, "phase": "llm"}
                    except Exception:
                        pass
                    try:
                        from agent.context_audit import record_blocks
                        record_blocks({}, phase="semantic_cache",
                                      extra={"cache_hit": 1,
                                             "cache_answer_chars": len(_cache_answer)})
                    except Exception:
                        pass
                if _auto_continues:
                    # 续跑轮清掉上轮 SysML 校验缓存：否则本轮 prose 答复会把**同一份代码**
                    #   再次拼进全量产出（_ensure_sysml_from_tools 的兜底追加是非幂等跨轮的）。
                    self._sysml_last_pass_code = None
                    self._sysml_last_checked_code = None
                # ★ 2026-11-11：三级展示的**层级上下文**（Agent → Skill → 工具/MCP）。
                #   为什么必须由后端回填：前端拿到的 tool 事件只有 {name, arguments, call_id}，
                #   **没有任何"这是谁调的"的字段** ⇒ 前端只能把所有 agent/skill/tool 平铺到
                #   同一层（实测 `11-pipeline.js` 的 procAddAgent/procAddSkill/procAddTool
                #   各自 procUpsert 顶层）。这不是前端的问题，是协议没传。
                #   契约（前端按此渲染三级）：
                #     agent 事件 → depth=0，作为容器
                #     skill 事件 → depth=1，挂在 agent 下
                #     tool  事件 → depth=2，挂���命中的 skill 下；无 skill 命中时挂 agent 下
                #   `mcp` 字段留给将来：`tools.mcp_server_id` 目前**全库为空**（0 条），
                #   故 L3 实际是「本地工具」；协议先留位，MCP 接入后无需改结构。
                _ctx_agent = agent_def.name or "-"
                _ctx_skill = None            # None = 未命中技能（工具直接挂 agent 下）
                # ★ 2026-10-17（米爸反馈3）：ask_user 人在回路澄清信号（None=未触发）。
                #   LLM 调 ask_user ⇒ _exec_tool_call 返回 clarify_required ⇒ 中断循环，
                #   由下方（follow-up 消费前）发射 clarify_ask + 落挂起 + return。
                _clarify_req = None

                def _mcp_of(tool_name):
                    """工具→MCP 服务器名（用于三级展示第3 层的标签）。

                    ★ 数据现状（2026-11-11 实测）：`tools.mcp_server_id` **全库 41 个工具全为空**
                    ⇒ 当前所有工具都是本地工具，`mcp` 恒为 None。
                    协议**先留位**：将来接入 MCP 时，这里会自动返回服务器名，
                    前端渲染「工具名（MCP:xxx）」**无需再改协议**。
                    查不到一律返回 None，**不得抛异常**（层级信息不该拖垮主链路）。
                    """
                    if not tool_name:
                        return None
                    try:
                        conn = get_db()
                        try:
                            row = conn.execute(
                                "SELECT m.name FROM tools t LEFT JOIN mcp_servers m "
                                "ON m.id = t.mcp_server_id "
                                "WHERE t.name = ? AND t.status = 'active'", (tool_name,)
                            ).fetchone()
                            return (row["m"] if row else None) or None
                        finally:
                            conn.close()
                    except Exception:
                        return None

                _rnd = 0  # 工具轮次计数（供 reasoning 事件 round/phase 标识；无工具直出时保持 0）
                # 2026-09-17 S3：上一轮已回填的 tool 消息下标（下轮仅清空其 content，不删除消息）
                _prev_tool_idx = []
                # ── P1-②（2026-10-07）follow-up 队列（第3 批阶段 1）──
                #★ 循环头消费 follow-up，而不是循环尾：消费点在**轮次边界**，
                #   此时上一轮 messages 已完整（含 tool_result），不会破坏 tool_call 配对。
                #   （DecodingAI 的教训：即时注入会 corrupt 正在执行的 tool_call。）
                # ★ 为什么用 `while True` + `_rnd` 计数而**不是** `for _rnd in range(...)`：
                #   follow-up 要求"重启一轮"，而 for 无法重置计数。循环体内部**零改动**
                #   （`_rnd` 仍是 0-based，事件里的 round= `_rnd+1` 语义不变）。
                # ★ 为什么有 `_fu_budget`：用户可能狂点补充。每消费一次就重置轮次，
                #   无上限 ⇒ 无限循环 + 无限 LLM 调用。限 3 次（够用且可控）。
                # ★ 为什么队列按conversation_id 隔离而非挂self：agent 是**模块级单例**
                #   （agent/__init__.py:25），挂实例 = 跨会话互相看到对方的补充（数据泄漏）。
                from agent import followup as _fup
                _FOLLOWUP_PREFIX = ("\n\n【用户在生成过程中补充了新指令】"
                                    "以下内容由用户在上一轮回答生成期间追加，"
                                    "请与上文结合处理；若与上文结论冲突，以上条补充为准：\n")
                _fu_budget = 3
                _fu_used = 0
                _rnd = -1
                while True:
                    _fu = ""
                    if (conversation_id and _fu_budget > 0
                            and _fup.pending_count(conversation_id)):
                        _fu = _fup.pop(conversation_id) or ""
                    if _fu:
                        _fu_budget -= 1
                        _fu_used += 1
                        messages.append({"role": "user",
                                         "content": _FOLLOWUP_PREFIX + _fu})
                        _fu_delta = f"（{agent_def.name}）收到生成期间的补充指令，重新规划：{_fu[:60]}"
                        exec_notices.append(({"kind": "progress", "text": _fu_delta}))
                        yield {"type": "reasoning", "delta": _fu_delta, "kind": "progress",
                               "round": _rnd + 2, "phase": "followup"}
                        # ★ 重置轮次：新指令值得完整的工具轮次预算（否则它只能拿到剩 1~2 轮）
                        _rnd = -1
                    _rnd += 1
                    if _rnd >= max_tool_rounds:
                        break
                    if not tools_def:
                        break
                    # 预算耗尽 ⇒ 停止继续调工具，让模型基于已有信息直出（护栏三闸之一）
                    if not _lg.budget_left():
                        # ★★ 2026-10-28（用户反馈「提示工具调用已达上限，为啥调用这么多次」）：
                        #   实测 conv 9726：真实停机原因是 **token 预算耗尽**
                        #   （stop_reason='token_budget'，316,885 / 300,000），
                        #   但模型在最终回答里写成「本轮工具调用已达上限」——**归因错误**。
                        #   根因链（真机数据）：
                        #     · 本分支只 yield 一条 stage（发给前端），**不进 messages**
                        #       ⇒ 模型不知道停机原因，只看到"工具没了" ⇒ 自己猜
                        #     · 本文件又没有调用 `loop_protocol.finalize_messages`，
                        #       所以已经有精确文案的 `finalize_hint_ex(reason='budget')`
                        #       在主路径上**一次都没生效**
                        #   后果：用户拿着"调用次数上限"去调配置也调不动
                        #   （真正该看的是 loop_token_budget / 上下文膨胀）。
                        #   修法：把**真实原因**同时给两端 ——
                        #     ① `exec_notices`（界面可见，带具体数字）
                        #     ② `messages`（模型可见，明确禁止它再归因为"次数上限"）
                        _budget_note = (
                            f"（工具预算耗尽）本轮 token 预算已用尽"
                            f"（已用 {_lg.tokens_used} / 上限 {_lg.token_budget or '不限'}），"
                            f"不是工具调用次数上限。请基于已有信息给出最终结论，"
                            f"并在结论中如实说明「因 token 预算耗尽而停止」，"
                            f"不要写成「工具调用次数已达上限」。")
                        exec_notices.append(({"kind": "progress", "text": _budget_note}))
                        try:
                            messages.append({"role": "user", "content": _budget_note})
                        except Exception:
                            pass
                        yield {"type": "stage", "name": "工具预算耗尽，停止继续调用",
                               "status": "done", "detail": _lg.stats()}
                        tools_def = None
                        break
                    # ★ 失败轮预算：**只有失败轮才扣**（成功的正常推进不限制）
                    if not _lg.failed_rounds_left():
                        # 同上：失败轮上限 ≠ 调用总数上限，必须把真实原因说清
                        _fail_note = (
                            f"（工具失败轮耗尽）连续工具失败轮已达上限"
                            f"（{_lg.failed_rounds} / {_lg.max_failed_rounds} 次；成功轮不计数），"
                            f"不是工具调用次数上限。请基于已有信息给出最终结论，"
                            f"并如实说明是「失败次数达上限」而不是「调用次数达上限」。")
                        exec_notices.append(({"kind": "progress", "text": _fail_note}))
                        try:
                            messages.append({"role": "user", "content": _fail_note})
                        except Exception:
                            pass
                        yield {"type": "stage", "name": "失败轮预算耗尽，停止继续调用",
                               "status": "done", "detail": _lg.stats()}
                        tools_def = None
                        break
                    with _turn_cm(_tracer, _rnd + 1):   # P0-b：ReAct 轮 span（M2 挂载）
                        probe = llm_client.chat(messages, provider_id=effective_provider, stream=False,
                                               tools=tools_def, _intent=CHAT_TASK_PROBE, _model_params=_agent_mp)
                        _lg.add_llm(probe)
                        pmsg = probe["choices"][0]["message"]
                        ptool_calls = pmsg.get("tool_calls") or []
                        # V2.5：探测轮思考透传（模型决定是否调用工具的推理过程，非流式 message.reasoning_content）
                        _p_think = str(pmsg.get("reasoning_content") or "").strip()
                        if _p_think:
                            exec_reasoning.append(_p_think)
                            yield {"type": "reasoning", "delta": _p_think, "round": _rnd + 1, "phase": "probe"}
                        if not ptool_calls:
                            p_content = pmsg.get("content") or ""
                            if p_content:
                                tool_injected = True
                                # P0-3（2026-09-17）：探测轮是非流式拿到的完整回复，此处按类人节奏补吐
                                # ★ 2026-10-27（断点保护）：**先赋值、后吐字**（同 4391 处）。
                                #   这是本轮最常命中的路径：模型"直接作答"（不调工具）时，
                                #   内容已完整在手，却先按 45 字/秒慢慢吐 ⇒ 用户随时可能在这
                                #   几秒里断开/关页 ⇒ 原写法此时 llm_content 仍为空
                                #   ⇒ 断点固化判 `len<4` 跳过 ⇒ 屏幕上明明有字，刷新全没。
                                llm_content = p_content
                                _streamed_acc[0] += p_content
                                for _chunk in iter_stream_chunks(p_content):
                                    yield {"type": "token", "delta": _chunk}
                            break
                        tool_injected = True
                        results = []
                        # 思考过程：Agent 决策需要调用哪些工具（规划）
                        plan = "、".join((tc.get("function") or {}).get("name", "") for tc in ptool_calls[:3])
                        _p_delta = f"（{agent_def.name}）判断需要调用工具补充上下文，计划调用：{plan}。开始执行工具…（第 {_rnd + 1} 轮）"
                        exec_notices.append(({"kind": "progress", "text": _p_delta}))
                        yield {"type": "reasoning", "delta": _p_delta, "kind": "progress", "round": _rnd + 1, "phase": "plan"}
                        weak_count = 0  # P0-按需工具：本轮弱相关结果数（全弱 → 收敛，禁止连环调用）
                        _round_had_ok = False   # 本轮是否有可用工具结果（失败轮预算判据）
                        #── P0-①（2026-10-07）并行调用配对：切成「执行队列 / 未执行队列」────
                        # ⚠️ 不能删掉上限（那是失控护栏），必须为未执行的尾部补结果，
                        #    否则 pmsg 带全部 tool_calls 而 results 只有 3 笔 ⇒ 第 4 笔起悬空
                        #    ⇒ 下一轮 HTTP 400（tool_use ids without tool_result blocks）。
                        #    详见 agent/loop_protocol.py 模块 docstring 缺陷①。
                        _exec_calls, _skip_calls = _lproto.split_calls(ptool_calls)
                        for _ti, tc in enumerate(_exec_calls):
                            fn = tc.get("function") or {}
                            tname = fn.get("name", "")
                            # UX规范·硬规则1（2026-10-02）：一动作一节点 —— 每笔调用携带稳定 call_id
                            # （协议 tool_call id；缺失时用 名称#轮次-序号 兜底），前端以它为键复用节点，
                            # 同名并发两笔调用各自成卡、不再互相覆盖。
                            _call_id = str(tc.get("id") or f"{tname}#{_rnd + 1}-{_ti}")
                            # ── P0-②（2026-10-07）截断参数：拒绝执行 + 明确告知重发 ──
                            # ⚠️ 必须先判别再解析：`except: targs={}` 会让**空参数照常执行**，
                            #    既可能误用工具默认行为，又掩盖了"模型输出被截断"这个真因。
                            #    `is_truncated_args` 区分「非法 JSON=截断」与「合法空=正常请求」，避免误杀。
                            if _lproto.is_truncated_args(fn):
                                _tr = _lproto.truncated_result(tname)
                                yield {"type": "tool", "status": "done", "name": tname,
                                       "ok": False, "result": _tr["result"],
                                       "error": "args_truncated", "call_id": _call_id,
                                       "truncated": True,
                                       "agent": _ctx_agent, "skill": _ctx_skill,
                                       "depth": 2 if _ctx_skill else 1, "mcp": _mcp_of(tname)}
                                results.append({"tool_call_id": tc.get("id", ""), "role": "tool",
                                                "name": tname,
                                                "content": json.dumps(_tr, ensure_ascii=False)})
                                continue
                            try:
                                targs = json.loads(fn.get("arguments") or "{}")
                            except Exception:
                                # 走到这里说明 `is_truncated_args` 判错了（非截断但仍解析失败）
                                # ⇒ 保守当截断处理，**不**以空参数执行。
                                _tr = _lproto.truncated_result(tname)
                                results.append({"tool_call_id": tc.get("id", ""), "role": "tool",
                                                "name": tname,
                                                "content": json.dumps(_tr, ensure_ascii=False)})
                                continue
                            # 工具调用结果：执行前 → 执行后（流式透传；结果全量返回，前端可展开/复制）
                            yield {"type": "tool", "status": "run", "name": tname, "arguments": targs,
                                   "call_id": _call_id,
                                   # ★ 2026-11-11 三级展示层级（详见 _ctx_agent 处注释）
                                   "agent": _ctx_agent, "skill": _ctx_skill,
                                   "depth": 2 if _ctx_skill else 1, "mcp": _mcp_of(tname)}
                            _t0 = time.time()   # P0-1 工具耗时：run→done 计时
                            # 2026-10-04：重复调用短路（同工具同参数已成功跑过 ⇒ 回喂既有结果）。
                            # ⚠️ 必须发生在 yield "run" **之前**：否则前端会先亮起一个节点，
                            #    然后既没有真实执行也没有 done 事件 ⇒ 节点永久停在"执行中"。
                            _dup = _lg.probe_dup(tname, targs)
                            if _dup:
                                _lg.dup_short_circuits += 1
                                _dup_streak += 1      # ★ 2026-10-27：连续重复计数（见 round 末收敛闸）
                                # ★ 2026-10-28：回喂体统一由 LoopGuard 生成
                                #   （含"这是重放、不要据此判不存在"的说明；与 execute.py 同源）
                                result = _lg.replay_payload(tname, targs, _dup)
                            else:
                                _dup_streak = 0       # ★ 真执行 ⇒ 有新进展，连续计数归零
                                result = _lg.call(self._exec_tool_call, tname, targs)
                                # ★ 2026-10-17：ask_user 人在回路澄清 —— 记录信号并中断 ReAct 循环
                                #   （不再喂 tool_result、不再继续探测：等用户回答后续跑）
                                if isinstance(result, dict) and result.get("clarify_required"):
                                    _clarify_req = result.get("clarify") or {}
                                    # ★ 2026-11-12（米爸反馈3 复发，conv 9571 实证 7 连问 /
                                    #   conv 9553 实证 24 连问）：**必须补 done 事件**。
                                    #   此前 break 发生在 yield "tool done" **之前** ⇒ 前端
                                    #   ask_user 节点永远停在"执行中"（用户截图 8 个节点各卡
                                    #   ~750s「调用中...」）。澄清不是失败，节点必须正常收口——
                                    #   否则"看起来有 8 个没跑完的工具"比"1 个已完成的确认"
                                    #   更让人怀疑系统状态。
                                    yield {"type": "tool", "status": "done", "name": tname,
                                           "ok": True, "result": str(result.get("result") or ""),
                                           "error": "", "truncated": False,
                                           "latency_ms": int((time.time() - _t0) * 1000),
                                           "call_id": _call_id, "skipped": False,
                                           "skip_reason": "", "agent": _ctx_agent,
                                           "skill": _ctx_skill, "depth": 2 if _ctx_skill else 1,
                                           "mcp": _mcp_of(tname)}
                                    break
                                # ★ 2026-10-28（写前确认 HITL）：写操作被确认闸拦下 ⇒ 中断本轮。
                                #   与 `clarify_required` **同构**：不喂 tool_result、不继续探测
                                #   （否则模型会继续发写请求 ⇒ 弹出一堆确认单）。
                                #   ⚠️ 必须先补 done 事件：否则前端该工具节点永远停在"执行中"
                                #   （与 2026-11-12 澄清 7 连问那次同源的问题）。
                                if isinstance(result, dict) and result.get("confirm_required"):
                                    _hil_confirm_req = result.get("confirm") or {}
                                    yield {"type": "tool", "status": "done", "name": tname,
                                           "ok": True, "result": str(result.get("result") or ""),
                                           "error": "", "truncated": False,
                                           "latency_ms": int((time.time() - _t0) * 1000),
                                           "call_id": _call_id, "skipped": False,
                                           "skip_reason": "", "agent": _ctx_agent,
                                           "skill": _ctx_skill, "depth": 2 if _ctx_skill else 1,
                                           "mcp": _mcp_of(tname)}
                                    break
                                _r0 = str(result.get("result") or "")
                                if result.get("ok") and (result.get("weak")
                                                         or ("未检索到" in _r0)
                                                         or ("无新冲突" in _r0)):
                                    _lg.undo_dup_count(tname, targs)
                            _t_ok = bool(result.get("ok"))
                            _r_full = str(result.get("result") or "")
                            # ★ 2026-10-27（真机 E2E 实测，conv 9698）：**记录已成功执行的写操作**。
                            #   现象：模型在第 11 轮触发「连续重复调用收敛闸」被强制退出循环，
                            #   进入最终生成时**看漏**了 messages 里 `create_model_elements`
                            #   的成功回执（status=SUCCEEDED），于是在最终回复里断言
                            #   「尚未写入平台」——**与事实相反**（平台直连回读证实已建成）。
                            #   ⇒ 收尾时把成功写操作**显式列出**（见 `loop_protocol.write_ops_digest`）。
                            #   判据：① `ok=True`（只列成功的，失败由各自错误回执承载）；
                            #        ② 该工具确实是写类（本地 `side_effect` + MCP 动词族）。
                            if _t_ok and _is_write_tool(self, tname):
                                _write_ops.append({
                                    "name": tname,
                                    "detail": _write_op_detail(_r_full),
                                })
                            # ★ 2026-10-27（用户质疑"前面已确定工程，是不是上下文遗漏了"——
                            #   实测确认**会丢**）：把本轮工具回执里出现的
                            #   工程/分支/版本/元素**落成会话锚点**，下一轮不依赖历史窗口。
                            #   机制缺口：历史只取当前话题最近 8 条 + 3000 字符预算，
                            #   更早的靠语义回捞（现 bigram 降级）⇒ conv 9629 里用户
                            #   msg 4869「基于这个工程广汽方法论建模1022」落在窗口外，
                            #   当时没丢只是因助手回复里反复复述了工程名（运气）。
                            try:
                                from agent import conv_anchors as _ca
                                from database import db_conn as _cadb
                                with _cadb() as _cac:
                                    _ca.record_from_tool(_cac, conversation_id, _r_full, targs)
                            except Exception:
                                pass
                            # ★ 2026-10-27（会话 9629）：**提交即托管**。
                            #   工具一旦返回"异步任务已受理/运行中"（结果里带 taskId + status），
                            #   立刻把任务登记进 async_external_jobs —— 责任从 LLM 转到系统：
                            #   下一轮开头自动查状态、完成则自动分页读取并归档。
                            #   为什么必须由系统记：实测模型从不跨轮复用 taskId，
                            #   每轮重查工程/分支/版本并重新提交导出（白烧整轮预算）。
                            try:
                                from agent import async_jobs as _aj
                                _ajob = _aj.detect_job(_r_full)
                                if _ajob:
                                    from database import db_conn as _adb
                                    # ★ 2026-10-27：变量名**必须**不能叫 `_ac` ——
                                    #   本函数里 `_ac` 是 `agent.auto_continue` 的模块别名
                                    #   （4295 行 `from agent import auto_continue as _ac`），
                                    #   而 `with ... as _ac` 会把它在**整个函数作用域**里覆盖成
                                    #   数据库连接 ⇒ 之后 8 处 `_ac.decide/AUTO_EVAL_STAGE/
                                    #   continuation_prompt/...` 全部 AttributeError。
                                    #   潜伏条件：同一轮里既检测到异步任务、又走到自主续跑评估
                                    #   （智源导出任务 + 长任务，正是本项目的常见组合）。
                                    with _adb() as _ac_conn:
                                        _aj.record(_ac_conn, conversation_id,
                                                   _mcp_of(tname) or "", tname, _ajob)
                            except Exception:
                                pass
                            # ★ 2026-10-17（米爸反馈4）：识别工具结果里的完整 SysML 文档
                            #   （智源导出 READ 的 JSON 信封 / 裸文本源码），persist 时归档。
                            _doc_hit = None
                            try:
                                from agent.utils import collect_tool_sysml_doc as _ctd
                                _doc = _ctd(getattr(self, "_tool_doc_pages", {}), tname, _r_full)
                                if _doc:
                                    _dk = _doc["text"][:200]
                                    if _dk not in self._tool_sysml_docs_seen:
                                        self._tool_sysml_docs_seen.add(_dk)
                                        self._tool_sysml_docs.append(_doc)
                                        _doc_hit = _doc
                            except Exception:
                                pass
                            # ★ 会话流环节块（米爸方案A1）：取回即告知——「已自动存入产物」，
                            #   点击产物面板可看全文。yield 必须在 try/except 之外。
                            if _doc_hit:
                                _dsz = len(_doc_hit.get("text") or "")
                                yield {"type": "stage", "name": "取回模型文件",
                                       "status": "done",
                                       "detail": "{}（{}）已自动存入产物".format(
                                           _doc_hit.get("title") or "模型文件",
                                           "{:.1f}KB".format(_dsz / 1024) if _dsz >= 1024
                                           else "{}B".format(_dsz))}
                            # ★ 失败轮预算判据（与 execute.py 同源）：本轮是否有可用结果
                            # ★ 2026-10-28（conv 9736 实证）：**参数类错误不算失败轮**。
                            #   那一轮 3 次把 elementTypes 传成不存在的类型名，平台回
                            #   INVALID_ARGUMENTS + recovery.action=CORRECT_ARGUMENTS
                            #   ⇒ 这是"按清单改就能对"的信息性结果，却被当工具坏了扣满
                            #   失败轮 ⇒ 整轮判死，建包/写入一次都没尝试。
                            #   ⇒ 判据抽成纯函数 `is_correctable_failure`（两路共用）。
                            if _t_ok and not (result.get("weak")
                                              or ("未检索到" in _r_full)
                                              or ("无新冲突" in _r_full)):
                                _round_had_ok = True
                            elif _lg.is_correctable_failure(result):
                                # 可修正的参数错误：本轮不算失败（给模型改参数的机会）
                                _round_had_ok = True
                                _fix_hint = (
                                    "⚠️ 上一次调用是**参数不合法**（不是工具坏了）："
                                    "请按回执里给出的字段/取值清单改正后重试，"
                                    "**不要**因为这一次失败就放弃该步骤。")
                                exec_notices.append(({"kind": "progress", "text": _fix_hint}))
                                try:
                                    messages.append({"role": "user", "content": _fix_hint})
                                except Exception:
                                    pass
                            _t_trunc = len(_r_full) > _TOOL_RESULT_CAP
                            _t_result = _r_full[:_TOOL_RESULT_CAP]
                            _e_full = str(result.get("error") or "")
                            _t_error = _e_full[:_TOOL_RESULT_CAP]
                            _t_ms = int((time.time() - _t0) * 1000)   # P0-1：毫秒耗时
                            # 2026-10-04：把护栏判定回显到事件里 —— 用户看到的必须是
                            # "这一步因重复调用被跳过"，而不是一个看起来正常执行成功的节点
                            # （否则重复调用在界面上完全不可见，用户以为模型白跑了几次）。
                            _skipped = bool(result.get("_dup")) or bool(result.get("_t_timeout"))
                            _skip_why = ("dup_short_circuit" if result.get("_dup")
                                         else "tool_timeout" if result.get("_t_timeout") else "")
                            exec_tools.append({"name": tname, "ok": _t_ok,
                                               "arguments": targs, "result": _t_result,
                                               "error": _t_error, "truncated": _t_trunc,
                                               "elapsed_ms": _t_ms,
                                               "skipped": _skipped, "skip_reason": _skip_why})
                            yield {"type": "tool", "status": "done", "name": tname,
                                   "ok": _t_ok, "result": _t_result, "error": _t_error,
                                   "truncated": _t_trunc, "latency_ms": _t_ms,
                                   "call_id": _call_id,
                                   "skipped": _skipped, "skip_reason": _skip_why,
                                   # ★ 2026-11-11 三级展示层级（与上方 run 事件同源）
                                   "agent": _ctx_agent, "skill": _ctx_skill,
                                   "depth": 2 if _ctx_skill else 1, "mcp": _mcp_of(tname)}
                            _weak = bool(result.get("weak")) or ("未检索到" in _t_result) or ("无新冲突" in _t_result)
                            if _weak:
                                weak_count += 1
                            # 2026-09-17 S3：回填给模型的结果单独封顶（此前用未截断的原始结果，长结果被后续每轮重发）
                            # P1-4（2026-10-02）：封顶升级为 offload——全文落库，模型侧换引用块（可寻址召回）
                            _t_model = str(result.get("result") or "")
                            # ★ 2026-11-13（#56 端到端实证）：`read_attachment_section` 用**专用上限**。
                            #   通用上限 `_TOOL_MODEL_CAP=3000` 对它**天然不适用**：
                            #   整章正文动辄 5000~7000 字符（第 5 章实测 6024）⇒ 每次取章都被
                            #   offload 成引用块 ⇒ 模型只拿到一个 id ⇒ 它再去调 tool_result_fetch
                            #   要全文（而那又被 RBAC 拦）⇒ 最终**看到的是空**
                            #   ⇒ 得出"第 5/6/7 章正文读不到"的错误结论（实测原话：
                            #   「全部返回空内容」——其实工具已返回全文，是回填环节丢的）。
                            #   本工具的语义就是"把这一章的正文给模型" ⇒ 断章取义地截断
                            #   会直接毁掉它的用途 ⇒ 给足整章上限，且**不 offload**。
                            _t_cap = _ATT_SECTION_MODEL_CAP if tname == "read_attachment_section" else _TOOL_MODEL_CAP
                            _t_content, _offloaded = _tool_offload.model_side_content(
                                tname, result, getattr(self, "_tool_conv_ctx", 0), cap=_t_cap)
                            if _offloaded:
                                _tool_offload.ensure_fetch_tool(tools_def, getattr(self, "_tool_whitelist", None))
                            if _weak:
                                # 弱相关引导：明确告知 LLM 结果不可用，禁止继续调用工具连环试探
                                _t_content["hint"] = "该工具结果与当前任务弱相关（或未命中实质内容），请直接基于已有信息回答，不要再调用其他工具。"
                            results.append({
                                "tool_call_id": tc.get("id", ""),
                                "role": "tool",
                                "name": tname,
                                "content": json.dumps(_t_content, ensure_ascii=False),
                            })
                        # ── P0-①（2026-10-07）为**未执行**的尾部调用补结果 ──
                        # pmsg 带的是**全部** ptool_calls，若不给尾部补 tool_result，
                        # 第 4 笔起永久悬空 ⇒ 下一轮 HTTP 400（protocol 硬约束）。
                        # ⚠️ 必须**在 `if results:` 之前**追加：全被截断时 results 也要非空，
                        #    否则整轮 tool_calls 一个结果都没有，配对同样缺失。
                        # ★ 2026-11-12（米爸反馈3，conv 9571 实证 7 连问 / conv 9553 实证 24 连问）：
                        #   clarify 已记录 ⇒ **立即退出轮次循环，不再补 skip、不再回填 pmsg**。
                        #   两个原因缺一不可：
                        #   ① 协议：ask_user 的 tool_call 没有配对 tool_result，回填 pmsg 即悬空
                        #      ⇒ 下一轮探测 HTTP 400；
                        #   ② 防重问：results 为空时 `if results:` 整块跳过 ⇒ messages 不更新
                        #      ⇒ 下一轮 LLM 看到**同一份上下文** ⇒ 换措辞再问一遍（真库实测
                        #      3s/次、最多烧到 max_tool_rounds=100 轮，每轮还多一条工具留痕）。
                        if _clarify_req:
                            break
                        if _skip_calls:
                            _skip_res = _lproto.skipped_results(_skip_calls)
                            for _sr in _skip_res:
                                _sfn = _sr.get("name") or "-"
                                yield {"type": "tool", "status": "done", "name": _sfn,
                                       "ok": False, "result": "本轮并发已达上限，未执行",
                                       "error": "round_tool_limit", "call_id": _sr.get("tool_call_id"),
                                       "skipped": True, "skip_reason": "round_tool_limit",
                                       "agent": _ctx_agent, "skill": _ctx_skill,
                                       "depth": 2 if _ctx_skill else 1, "mcp": _mcp_of(_sfn)}
                            results.extend(_skip_res)
                        # 工具结果回填后进入下一轮（观察 → 再思考）
                        if results:
                            # 2026-09-17 S3：上一轮 tool 内容换为省略标记
                            # 不可删除该消息——OpenAI 兼容协议要求 tool 与 assistant.tool_calls 成对出现
                            # ★ 2026-11-13（#57）：**取回的章节正文不可省略**。
                            #   原实现把上一轮所有 tool 结果清空 ⇒ 模型下一轮看到"上下文里没有
                            #   第 5/6/7 章正文" ⇒ 又去调 read_attachment_section 取一遍
                            #   ⇒ 实测同一轮内取章 40+ 次（5→6→7→5.3→6.1→7.1→… 循环），
                            #   每次都把整章正文再灌一遍上下文 ⇒ 上下文爆炸 + 60s+ 耗时。
                            #   判据：章节正文是模型**唯一**的正文来源、且**不可再生**
                            #   （清掉就只能重取）⇒ 保留；其他工具结果照旧省略（它们可再生）。
                            for _pi in _prev_tool_idx:
                                if 0 <= _pi < len(messages) and messages[_pi].get("role") == "tool":
                                    if messages[_pi].get("name") == "read_attachment_section":
                                        continue      # ← 章节正文：保留，不省略
                                    messages[_pi]["content"] = json.dumps(
                                        {"ok": True, "result": "",
                                         "note": "前序工具结果已省略——其结论已体现在后续推理中"},
                                        ensure_ascii=False)
                            _prev_tool_idx = []
                            messages.append(pmsg)
                            messages.extend(results)
                            for _k in range(len(messages) - len(results), len(messages)):
                                _prev_tool_idx.append(_k)
        # P0-按需工具：本轮工具结果全弱相关 → 收敛（跳出循环走兜底流式生成，避免连环调用）
                            # ⚠️ 分母用**执行数**（`_exec_calls`）而非 `len(results)`：
                            #    P0-① 为未执行尾部补了 skipped 结果，计入分母会让本该收敛的场景
                            #    变成"不收敛"，也可能让本该继续的场景误收敛。
                            # ★ 只有**失败轮**才扣失败预算（成功推进不限制，米爸 2026-10-09）
                            _lg.charge_failed_round(_round_had_ok)
                            # ★★ 2026-10-27（用户「为啥不能自己持续运行完成任务」的第二根因）：
                            #   **连续重复调用的收敛闸**。
                            #
                            #   实测（conv 9629 16:50 那轮）：5 次真调用后，模型又**连续 5 次**
                            #   发出**完全相同**的 `query_data_tree`；`probe_dup` 每次短路回喂
                            #   同一结果（`ok=True`），于是：
                            #     · `charge_failed_round(_round_had_ok=True)` ⇒ **不计失败轮**
                            #       ⇒ `max_failed_rounds` 这道闸完全不响；
                            #     · 轮次预算 100 也远未到；
                            #     · 每轮 prompt 约 28k ⇒ **5 轮烧掉约 14 万 token**（近半个预算）
                            #   ⇒ 最终以「工具预算耗尽（307,505 / 300,000）」收场，
                            #     而它其实只做了 5 件不同的事。
                            #
                            #   判据：**同一调用被连续短路 2 次**（= 第 3 次仍是它）就注入硬提示，
                            #   连续 3 次直接收敛（交给下方兜底流式生成，基于已有信息作答）。
                            #   为什么用"连续"而不是"累计"：多步任务里合法地回头重查同一个工具
                            #   是常见的；只有**中间没有任何新进展**的连续重复才是空转。
                            if _dup_streak >= 2:
                                # ★ 2026-10-28（conv 9738 实证）：**不再直接终止循环**。
                                #   真机现场：模型已经定位到目标包（2115385083629469696）、
                                #   也用对了导入通道的措辞，但把**同一个 parentId 查了 3 次**
                                #   （纯勘查、无新信息）⇒ 旧逻辑 break ⇒ 直接进最终生成
                                #   ⇒ 交付里写着"下一步我会执行校验+导入"，本轮一次没做。
                                #   判"空转"是对的，**处置错了**：空转时应把模型
                                #   **强行切换执行模式**（禁止再查、立即做剩余关键动作），
                                #   只有连续两次强指令仍不动才收敛终止。
                                _dup_redirect = getattr(self, "_dup_redirect_n", 0)
                                if _dup_redirect < 2:
                                    self._dup_redirect_n = _dup_redirect + 1
                                    _dup_streak = 0        # 归零：给它机会按新指令推进
                                    _d_delta = (
                                        f"（{agent_def.name}）已连续发出**完全相同**的工具调用"
                                        f"（同一节点查了多次，结果不会变化）⇒ **停止勘查，"
                                        f"立即切换到执行模式**。")
                                    _dup_cmd = (
                                        "【禁止重复勘查】你刚刚把同一个查询重复了多次，信息已经足够。\n"
                                        "现在**不要再查任何已查过的节点**，直接按剩余待办执行：\n"
                                        "· 有代码未校验 ⇒ 立即 `sysml_v2_validate`（硬错必须清零）；\n"
                                        "· 需要把整段 SysML v2 写进某个包 ⇒ 立即 `zhiyuan_sysmlv2_import`"
                                        "（`vc=\"<branchId>,0\"`、`text=<源码>`、"
                                        "`targetPackageDataId=<包 id>`），**一次调用**完成；\n"
                                        "· 写完立刻 `query_data_tree(parentId=目标包)` **回读确认**，"
                                        "再向用户报结果。\n"
                                        "若确实缺一项信息才能继续，只允许**换工具/换参数问一次**，"
                                        "并在答复里写明缺什么。")
                                    messages.append({"role": "user", "content": _dup_cmd})
                                    exec_notices.append(({"kind": "progress", "text": _d_delta}))
                                    yield {"type": "reasoning", "delta": _d_delta,
                                           "kind": "progress", "round": _rnd + 1, "phase": "plan"}
                                    yield {"type": "stage", "name": "重复调用收敛",
                                           "status": "done",
                                           "detail": f"连续 {_dup_streak or 2} 次相同调用："
                                                     f"已注入「停止勘查、立即执行」强指令"
                                                     f"（第 {_dup_redirect + 1}/2 次）"}
                                    continue      # ← 回循环顶，让它按强指令继续做
                                _d_delta = (
                                    f"（{agent_def.name}）已多次被要求停止重复勘查仍未推进，"
                                    f"本轮收敛：改为基于已有信息组织回答。")
                                exec_notices.append(({"kind": "progress", "text": _d_delta}))
                                yield {"type": "reasoning", "delta": _d_delta, "kind": "progress",
                                       "round": _rnd + 1, "phase": "plan"}
                                yield {"type": "stage", "name": "重复调用收敛",
                                       "status": "done",
                                       "detail": f"连续 {_dup_streak} 次相同调用已被短路，"
                                                 f"强指令后仍无推进，停止空转（避免烧穿 token 预算）"}
                                break
                            if weak_count == len(_exec_calls) and weak_count > 0:
                                _w_delta = f"（{agent_def.name}）本轮工具结果均与任务弱相关，不再继续调用工具，直接基于已有信息组织回答。"
                                exec_notices.append(({"kind": "progress", "text": _w_delta}))
                                yield {"type": "reasoning", "delta": _w_delta, "kind": "progress", "kind": "progress", "round": _rnd + 1, "phase": "plan"}
                                break
                # ★ 2026-10-28（写前确认 HITL）写前确认收口 —— 落痕迹 + 发确认卡 + 结束本次流。
                #   位置同样选在「最终生成之前」：ReAct 循环已因 confirm_required 中断，
                #   继续生成只会空转（而且会让模型在"没拿到批准"的情况下编造"已执行"）。

                def _interim_exec_card():
                    """**生成前**收口用的执行卡片（确认/澄清共用，唯一一份）。

                    ★ 2026-10-28（conv 9739 实证）：第一版在两处各写了一份同构字典，
                      而且都直接索引了 llm_info 的 used_mock —— 但 llm_info 是在
                      **最终生成之后**（5525 行附近）才有值的。本收口发生在它之前
                      ⇒ 抛 `cannot access local variable 'llm_info'` ⇒
                      **整轮流中断**，用户连澄清卡都拿不到（比不修更糟）。
                      ⇒ 抽成一个函数、所有可选字段用 `.get`/局部兜底；
                        两处调用（hil / clarify）共用同一份，杜绝再次错位。
                    """
                    # ⚠️ 闭包读外层 `llm_info` 必须 try/except NameError：
                    #   它在本收口点**可能尚未赋值**（那是本函数存在的全部理由）。
                    #   `locals().get(...)` 在这里是错的 —— 函数内 locals() 只含**本函数**
                    #   的局部变量，永远拿不到外层未赋值的名字（第一版就写错了）。
                    try:
                        _li = llm_info if isinstance(llm_info, dict) else {}
                    except NameError:
                        _li = {}
                    _ret = retrieval if isinstance(retrieval, dict) else {}
                    try:
                        _cit = _citations_payload(_ret.get("chunk_hits"))
                    except Exception:
                        _cit = []
                    return {
                        "intent": intent, "agent": agent_def.name,
                        "hil_level": hil_level, "kb_tags": kb_tags,
                        "skill_hits": self._last_skill_hits, "slots": slots,
                        "source": _ret.get("source"), "confidence": _ret.get("confidence"),
                        "graph_count": _ret.get("graph_count"),
                        "vector_count": _ret.get("vector_count"),
                        "used_mock": _li.get("used_mock"),
                        "provider": _li.get("provider"),
                        "citations": _cit,
                        "exec": {
                            "reasoning": "".join(exec_reasoning)[:8000] or None,
                            "notices": (exec_notices[:60] or None),
                            "agent": agent_def.name,
                            "tools": exec_tools,
                            "brief": getattr(self, "_last_intent_brief", None) or None,
                            "gates": _gates_safe(),
                        },
                    }

                if _hil_confirm_req:
                    # ★ 2026-10-28：把**本轮已产出/已做过什么**一并落库，别只留一句占位语。
                    #   真机 9733：校验跑完、时序图代码已生成，但历史里只剩
                    #   「⏸ 写操作需要人工确认」⇒ 用户无从判断该不该批准（见 session.py 注释）。
                    _hil_content = (
                        "⏸ **写操作待你确认**：%s\n\n"
                        "本轮已完成：见下方执行过程（工具回执/思考）；"
                        "批准后我会立即执行该写入并**回读确认**，再向你汇报结果。\n\n"
                        "> 待确认动作：`%s`（确认单 #%s）"
                        % (str((_hil_confirm_req or {}).get("title") or "需人工批准后执行"),
                           str((_hil_confirm_req or {}).get("tool") or "?"),
                           str((_hil_confirm_req or {}).get("id") or "?")))
                    _hil_card = _interim_exec_card()
                    yield from self._hil_confirm_finish_events(
                        conversation_id, intent, _hil_confirm_req,
                        content=_hil_content, exec_card=_hil_card)
                    return
                # ★ 2026-10-17（米爸反馈3）：ask_user 澄清收口 —— 落挂起 + 发射卡片 + 结束本次流。
                #   位置选在「最终生成之前」：此时 ReAct 循环已中断，继续生成只会空转。
                #   与 1.4/1.5 共用 _persist_clarify + clarify_ask 协议（前端同一套卡片/续跑机制）。
                if _clarify_req:
                    # ★ 2026-10-27：收口逻辑抽到 `_clarify_finish_events`（两处共用，见该方法注释）
                    # ★ 2026-10-28（conv 9735 实测）：**把本轮做过什么一起落库**。
                    #   那轮已经跑完定位、生成时序图代码、`sysml_v2_validate` 返回 0 错误，
                    #   却只留一句「需要您确认建模信息后继续」（12 字）⇒ 用户盲确认。
                    #   ⇒ content 列出工具脉络 + 校验回执；card 带完整 exec（含代码与参数）。
                    _clr_names = [str(t.get("name") or "") for t in (exec_tools or [])]
                    _clr_pass = [t for t in (exec_tools or [])
                                 if str(t.get("name") or "") in ("sysml_v2_validate", "sysml_v2_lint")
                                 and bool(t.get("ok"))]
                    _clr_lines = [
                        "⏸ **暂停等你确认**：%s" % (
                            str((_clarify_req or {}).get("question") or "需要你拍板后才能继续")),
                        "",
                        "本轮已完成：共 %d 次工具调用（%s）。"
                        % (len(_clr_names),
                           "、".join(dict.fromkeys(_clr_names))[:200] or "无"),
                    ]
                    if _clr_pass:
                        _clr_lines.append(
                            "校验回执：`%s` 已跑通（%s）"
                            % (_clr_pass[-1].get("name"),
                               str(_clr_pass[-1].get("result") or "")[:120].replace("\n", " ")))
                    _clr_lines.append("")
                    _clr_lines.append("> 生成的设计代码与全部工具回执见下方「执行过程」；"
                                      "回答后我会继续。")
                    _clr_card = _interim_exec_card()
                    yield from self._clarify_finish_events(
                        conversation_id, user_input, intent, branch, attachments,
                        forced_intent, skill_name, team, _clarify_req,
                        content="\n".join(_clr_lines), exec_card=_clr_card)
                    return
                # P1-② 补救点（2026-10-07）：循环已 break、**最终生成之前**再消费一次 follow-up。
                # ★ 为什么循环头不够（实测踩到）：循环头只覆盖「工具执行中 / 探测轮等待中」
                #   两种时机；而用户**最常见的补充时机是"正在看最终回答流式输出"** ——
                #   那时循环早已 break，队列永远等不到下一轮 ⇒ follow-up 静默失效。
                #   实测证据：桩生效（run=3/skipped=3）但 follow-up 未被消费。
                # ★ 为什么放在最终生成**之前**而不是之后：之后追加等于"再答一遍"，
                #   用户会看到两段互相矛盾的答案；之前追加 = 用补充指令重塑这一次的最终回答。
                if conversation_id and _fu_budget > 0 and _fup.pending_count(conversation_id):
                    _fu2 = _fup.pop(conversation_id) or ""
                    if _fu2:
                        _fu_budget -= 1
                        _fu_used += 1
                        messages.append({"role": "user",
                                         "content": _FOLLOWUP_PREFIX + _fu2})
                        _fu2_delta = (f"（{agent_def.name}）生成最终回答前收到补充指令，"
                                      f"已并入本轮：{_fu2[:60]}")
                        exec_notices.append(({"kind": "progress", "text": _fu2_delta}))
                        yield {"type": "reasoning", "delta": _fu2_delta, "kind": "progress",
                               "round": _rnd + 1, "phase": "followup"}
                        # ★ 有正文也照样重来一次最终生成：补充必须真正影响答案，
                        #   否则用户看到的是"完全没理会我的补充"。
                        #   重来一次仍不带 tools（下方 chat 调用本就不传 tools）。
                        # ★ 用  而不是新标志位：
                        #   下方两个分支（tool_injected / not tool_injected）都以
                        #   llm_content 为准，清空即重新生成一次，无需新增状态。
                        llm_content = ""
                # P1-①（2026-10-07）轮次用尽收尾。
                # ★ 此处**早就在做「无 tools 收尾」**（`chat(messages, stream=True)` 不传 tools）——
                #   方案 §3.1 说"stream 只兜底空正文"属实，但那个兜底本身**就是**收尾调用。
                #   真正的缺口是：**没告诉模型工具次数已用尽** ⇒ 模型可能以为对话被截断，
                #   给出残缺结论或重复已说过的内容。⇒ 这里补一条明确的收尾指令。
                # ★ 2026-10-08：与 execute.py 同源修复——收尾判据从「正文为空」升级为
                #   「**没有代码块**」（产码 Agent）。原判据 `not llm_content`
                #   会被模型的一句过程叙述骗过：
                #     「标准库确认 Parts::Port 均存在。现在生成结构视图…」（76 字）
                #   ⇒ 非空 ⇒ 收尾跳过 ⇒ 轮次耗尽时**一行代码都没产出**
                #   （实测 N3 视图展开 25% 无产出，根因即此）。
                #   `_is_v2_code_agent` 是既有单一事实来源，不新增 intent 白名单。
                _expect_code = bool(self._is_v2_code_agent(agent_def))
                if tool_injected and _lproto.lacks_deliverable(llm_content,
                                                                 expect_code=_expect_code):
                    # 追加提示走**副本**（finalize_messages 内部 list(messages) 复制）——
                    # 主流程的 messages 后续还要写卡片/落库，不能被收尾提示污染。
                    # ★ 2026-10-09：与 execute.py **同源**——把「已 offload 但没取回」
                    #   的正文附进收尾（通用补缺，非视图专属）。
                    _extra = ""
                    try:
                        from agent.pipeline_parts import skill_body_on_demand as _sbd
                        _extra = _sbd.unfetched_bodies(
                            getattr(self, "_skill_body_offloads", None))
                    except Exception:
                        _extra = ""
                    # ★ 2026-10-10：把**真实触发原因**传给收尾提示
                    #   （米爸问"是单个工具上限还是所有之和"——答案取决于撞的是哪道闸）。
                    #   `_lg.stop_reason` 是 LoopGuard 记录的真实原因（rounds/failed/token_budget），
                    #   拿不到就传空⇒ 沿用通用文案（逐字兼容）。
                    _fin_msgs = _lproto.finalize_messages(
                        messages, extra_context=_extra,
                        reason=_lg_stop_reason(_lg), used=_lg_failed_rounds(_lg),
                        limit=_lg_limit_of(_lg, max_tool_rounds),
                        write_ops=_write_ops)

                    # ★★ 2026-10-09（与 execute.py 同源，米爸纠正后补齐）：
                    #   收尾轮原来一律 `stream=True` 且**不传 tools**
                    #   ⇒ 实测 v6 报出78 个语法错后，收尾只写了 28 token
                    #   ⇒ **「报了错没人修」在前端真实路径同样成立**。
                    #   而**不能**直接给这个 stream 调用塞 tools：
                    #   `stream=True` 返回的是**生成器**，没有配套的工具执行循环
                    #   ⇒ provider 返回 tool_calls 而**无人消费** ⇒ 停摆。
                    #   ⇒ 正解：**先用一个非流式探针**把工具跑掉（SSE 事件照常 yield），
                    #      再用**更新后的 messages** 做流式收尾。
                    #   判据复用 `_lproto.repair_tools_needed`（只在 `判据：block` 时给工具）。
                    try:
                        _repair_msgs = None
                        if tools_def and _lproto.repair_tools_needed(messages):
                            _repair_msgs = _repair_tools = None
                            from agent.pipeline_parts.execute import _repair_tool_subset
                            _repair_tools = _repair_tool_subset(tools_def)
                            if _repair_tools:
                                _repair_msgs = messages
                                # 上限用 2：只给「改一遍 + 复校」的机会，不放开成新ReAct 循环
                                for _ri in range(2):
                                    if not _lg.failed_rounds_left():
                                        break
                                    _probe = llm_client.chat(
                                        _repair_msgs, provider_id=effective_provider,
                                        stream=False, tools=_repair_tools,
                                        _intent=CHAT_TASK_REPAIR, _model_params=_agent_mp)
                                    _pm = (_probe["choices"][0]["message"])
                                    _pcalls = _pm.get("tool_calls") or []
                                    if not _pcalls:
                                        break                      # 无工具调用 ⇒ 收尾
                                    _repair_msgs = list(_repair_msgs) + [_pm]
                                    _got_ok = False
                                    for _tc in _pcalls:
                                        _fn = _tc.get("function") or {}
                                        _tn = _fn.get("name", "")
                                        try:
                                            _ta = json.loads(_fn.get("arguments") or "{}")
                                        except Exception:
                                            _ta = {}
                                        yield {"type": "tool", "status": "run", "name": _tn,
                                               "arguments": _ta, "call_id": _tc.get("id", ""),
                                               "round": _rnd + 1, "phase": "repair",
                                               "agent": _ctx_agent, "skill": _ctx_skill,
                                               "depth": 2 if _ctx_skill else 1, "mcp": _mcp_of(_tn)}
                                        _res = _lg.call(self._exec_tool_call, _tn, _ta)
                                        if _res.get("ok"):
                                            _got_ok = True
                                        yield {"type": "tool", "status": "done", "name": _tn,
                                               "result": str(_res.get("result") or "")[:2000],
                                               "ok": bool(_res.get("ok")),
                                               "call_id": _tc.get("id", ""),
                                               "round": _rnd + 1, "phase": "repair",
                                               "agent": _ctx_agent, "skill": _ctx_skill,
                                               "depth": 2 if _ctx_skill else 1, "mcp": _mcp_of(_tn)}
                                        _repair_msgs.append({
                                            "tool_call_id": _tc.get("id", ""), "role": "tool",
                                            "name": _tn,
                                            "content": json.dumps(
                                                {"ok": _res.get("ok"),
                                                 "result": _res.get("result", "")},
                                                ensure_ascii=False)})
                                    _lg.charge_failed_round(_got_ok)
                    except Exception as _re:
                        # 修复循环任何异常都**不得**让整个流式请求失败
                        try:
                            yield {"type": "reasoning",
                                   "delta": f"（收尾修复轮跳过：{type(_re).__name__}）",
                                   "round": _rnd + 1, "phase": "repair"}
                        except Exception:
                            pass
                        _repair_msgs = None
                    if _repair_msgs:
                        _fin_msgs = _lproto.finalize_messages(_repair_msgs,
                                                               extra_context=_extra,
                                                               write_ops=_write_ops)
                    chunks = llm_client.chat(_fin_msgs, provider_id=effective_provider, stream=True,
                                            _intent=CHAT_TASK_ANSWER, _model_params=_agent_mp)
                    for chunk in chunks:
                        for delta in self._extract_stream_deltas(chunk):
                            # V2.5：工具轮后的最终生成同样透传思考（原分支只取 content，丢思考内容）
                            if delta.get("reasoning"):
                                exec_reasoning.append(delta["reasoning"])
                                yield {"type": "reasoning", "delta": delta["reasoning"],
                                       "round": _rnd + 1, "phase": "llm"}
                            if delta.get("content"):
                                llm_content += delta["content"]
                                _streamed_acc[0] += delta["content"]
                                yield {"type": "token", "delta": delta["content"]}
                # ★★★ 2026-10-27（真机 E2E 实测，conv 9698）：**已成功写操作的事实注入**。
                #   现象：`create_model_elements` 回执 `status=SUCCEEDED`（平台侧直连回读证实
                #   新包确实建成、total 1→2），但模型最终回复断言「**尚未写入平台**……
                #   因轮次用尽，写操作没有执行」——**与事实相反**。
                #   机制：ReAct 已累积 11 轮、messages 极长 ⇒ 模型在收尾时**看漏**了那条回执；
                #   而收尾提示（`finalize_messages`）只在 `lacks_deliverable` 为真时才走，
                #   本次模型**产出了长回复** ⇒ 收尾提示**根本没触发** ⇒ 无任何机制纠正它。
                #   ⇒ 修法：**在最终生成之前**把已成功写操作作为一条 user 消息追加进 messages
                #      （不依赖收尾分支 ⇒ 两条路径都覆盖）。
                #   ⚠️ 只在**确有成功写操作**时追加 ⇒ 无写操作的场景**逐字节不变**（零回归面）。
                #   ⚠️ 这只纠正「虚构未完成」；「虚构已完成」由工具自身回执 + 技能纪律承担。
                _wdigest = ""
                try:
                    _wdigest = _lproto.write_ops_digest(_write_ops)
                except Exception:
                    _wdigest = ""
                if _wdigest:
                    messages = list(messages) + [
                        {"role": "user", "content": _wdigest}]
                    try:
                        _w_delta = ("（已成功执行的写操作已记入上下文）"
                                    + "、".join(str(o.get("name") or "") for o in _write_ops))
                        exec_notices.append(({"kind": "progress", "text": _w_delta}))
                        yield {"type": "reasoning", "delta": _w_delta, "kind": "progress",
                               "round": _rnd + 1, "phase": "plan"}
                    except Exception:
                        pass
                # llm_client.chat(stream=True) 返回生成器；FORCE_MOCK 下 MockLLM 也走 _stream
                if not tool_injected:
                    chunks = llm_client.chat(messages, provider_id=effective_provider, stream=True,
                                            _intent=CHAT_TASK_ANSWER, _model_params=_agent_mp)
                    # ★★ 2026-10-27（用户反馈「中途执行失败后之前输出的内容没有任何展示」）
                    #   ——**断点保护的真实缺口就在这里**，必须逐字看完本注释再改。
                    #
                    #   旧实现：
                    #       full = []
                    #       for chunk in chunks:
                    #           ... full.append(delta["content"])
                    #               yield {"type":"token", "delta": ...}   ← 已经发给用户了
                    #       llm_content = "".join(full)                    ← **循环结束后**才赋值
                    #   ⇒ 流**中途**抛异常（Provider 断流 / 网关 reset / 解析异常）时：
                    #       · 用户屏幕上已有 N 个 token（真实可见）；
                    #       · 但 `llm_content` 仍是 ""；
                    #       · except 分支算出的 `_partial` 因此为空；
                    #       · `_persist_partial_stream` 的 `len(text) < 4` 直接跳过 ⇒ **库里 0 条**
                    #     ⇒ 刷新/切回会话= 一个字都不剩。真库实证（conv 9629 15:34:39「写入工程」）：
                    #       `llm_usage_stats` 有 4 次调用（末次 22,707 prompt / 929 completion，
                    #       内容确实生成过），而 `messages` **0 条 assistant**、
                    #       `orch_stream_events` **0 条** —— 用户看到的东西全部蒸发。
                    #
                    #   为什么另一条分支（工具轮后的生成）没这个毛病：它写的是
                    #       `llm_content += delta["content"]`（**增量赋值**，异常前已累积）。
                    #   ⇒ 两条分支累积方式不一致，本身就是缺陷。现统一为增量累积。
                    #
                    #   额外引入 `_streamed_acc`：**只增不减**地记录"本轮真的发给过用户的正文字符"。
                    #   为什么不直接用 llm_content：续跑（auto-continue）每轮开头会把
                    #   `llm_content = ""` 复位，而用户此前**已经看到过**上一轮的内容；
                    #   失败时若只看 llm_content，那些内容照样会丢（尤其是"第 2 轮失败"这种形态）。
                    #   `_streamed_acc` 不复位、只做失败兜底，与正常落库的 `_full_content` 互不干扰。
                    for chunk in chunks:
                        for delta in self._extract_stream_deltas(chunk):
                            # 思考过程（reasoning_content）与正文（content）分流推送
                            if delta.get("reasoning"):
                                exec_reasoning.append(delta["reasoning"])
                                yield {"type": "reasoning", "delta": delta["reasoning"],
                                       "round": _rnd + 1, "phase": "llm"}
                            if delta.get("content"):
                                llm_content += delta["content"]
                                _streamed_acc[0] += delta["content"]
                                yield {"type": "token", "delta": delta["content"]}
                st = getattr(llm_client, "stats", {})
                if _cache_answer:
                    # 缓存命中：**不谎报**成真实 provider 调用（含 0 延迟、0 token）
                    llm_info = {
                        "provider": "semantic-cache",
                        "model": "-",
                        "used_mock": False,
                        "latency_ms": 0,
                        "tokens": {"prompt": 0, "completion": 0},
                        "cache_hit": True,
                    }
                else:
                    llm_info = {
                        "provider": st.get("last_provider", "未配置"),
                        "model": "-",
                        "used_mock": bool(st.get("last_used_mock", True)),
                        "latency_ms": st.get("last_latency_ms", 0),
                        # V2.6：token 统计透传（前端执行收口条展示）
                        "tokens": {"prompt": st.get("last_prompt_tokens", 0),
                                   "completion": st.get("last_completion_tokens", 0)},
                    }
                # 2026-09-20：正文无代码时用工具层缓存补回，并**增量补吐**（补回内容是事后拼接的，
                # 不补吐则前端只看到结论、代码要等 done 重渲染才出现）。
                _pre_sysml_len = len(llm_content)
                llm_content = self._ensure_sysml_from_tools(llm_content)
                if len(llm_content) > _pre_sysml_len:
                    for _chunk in iter_stream_chunks(llm_content[_pre_sysml_len:]):
                        yield {"type": "token", "delta": _chunk}
                # ★ P1-1（2026-10-26）：**纯问答写入语义缓存**（与 execute.py:790 同口径）。
                #   此前 stream **从不写缓存** ⇒ 即便开了开关，主对话也永远建不起缓存。
                #   闸门与读取侧一致，另加两条：
                #     · `not exec_tools` —— 本轮真调过工具 ⇒ 答案含**实时数据**，
                #       不能当"可复用结论"缓存（否则下次返回过期事实，比慢更糟）。
                #     · `not _cache_answer` —— 本身来自缓存的不回写（否则 hit_count /
                #       时间戳被反复刷新，LRU 淘汰失真）。
                #   ⚠️ 位置在 `_ensure_sysml_from_tools` **之后**：缓存必须与用户最终看到的
                #     正文逐字节一致，否则命中时少一段代码。
                if (not _cache_answer and llm_content
                        and intent in ("chat", "knowledge_qa")
                        and not att_blocks and not _clarify_req
                        and not exec_tools
                        and not dry_run):
                    try:
                        from core import config as _cfg_scw
                        if _cfg_scw.as_bool("semantic_cache", "enabled", False):
                            from semantic_cache import SemanticCache
                            from database import get_db
                            _ccw = get_db()
                            try:
                                SemanticCache.put(_ccw, user_input, llm_content)
                            finally:
                                _ccw.close()
                    except Exception:
                        pass      # 缓存写入失败绝不能影响已生成的回答
                # ★ 2026-10-17（米爸「harness 持续工作」）：完成度评估 + 续跑决策。
                #   触发门三重（全满足才评估）：① 配置开且非 dry_run；② 本轮**实际调用过工具**
                #   （exec_tools 非空——纯问答轮没有「剩余步骤」可言，且省评估器成本）；
                #   ③ 续跑次数未满。评估器本身绝不抛异常（_evaluate_completion 内部吞），
                #   判据解析失败 ⇒ verdict=None ⇒ decide 返不续跑（fail-safe 向停）。
                # ★ 2026-10-22（米爸反馈3「不要遇到点问题就终止」）：
                #   闸② 补一个**续跑意图**例外 —— 用户明确说「继续/接着/go on」时，
                #   "要不要继续"恰恰是唯一值得评估的问题，却因这一轮没调工具而被整条跳过
                #   （实测会话 9587：4777「继续」→ 4778 只走了 chat_probe，
                #     llm_usage_stats 里**没有任何 CHAT_TASK_EVAL 调用** ⇒ 续跑机制等于没启动）。
                #   ⇒ 续跑意图轮强制评估一次（成本：+1 次小调用），其余纯问答轮维持原样不评估。
                _cont_input = False
                try:
                    _cont_input = bool(self._is_continuation_input(user_input))
                except Exception:
                    _cont_input = False
                # ★★ 2026-10-27（用户两问）：「为啥不自己跑完」+「要澄清为啥不弹卡片」
                #
                #   实测现场（conv 9629，用户「继续写入工程」）：该轮 `tool_call_logs`
                #   **零条 tool**（模型只写了一大段说明就收尾），而
                #   `_is_continuation_input("继续写入工程")` = **False**（它要求整句是裸续作短语）
                #   ⇒ 旧闸门 `(exec_tools or _cont_input)` **两个都不满足** ⇒ 评估器没跑
                #   ⇒ 模型那句"下一轮我先用名称检索「测试D」"没人执行，用户只能自己再发一句。
                #
                #   ⇒ 补两个**更宽**的触发信号（都只用于"要不要评估"，不改主路径行为）：
                #     · `_cont_cue`：用户这轮在说"接着干"（继续/接着/往下/按上面…开头即可）；
                #     · `_awaiting`：**答复以征求许可/等拍板结束** —— 这是最该评估的一类：
                #       任务明明没做完，只是模型停下来问了一句。旧闸门把它整类排除了。
                #   为什么要包含"没调工具"的轮次：本仓旧注释的前提是"纯问答轮没有剩余步骤"，
                #   但实测最常见的失败恰恰是「模型把剩余步骤写进了正文、却没执行」。
                try:
                    _cont_cue = bool(_ac.has_continue_cue(user_input))
                except Exception:
                    _cont_cue = False
                _awaiting = False
                try:
                    _awaiting = bool(llm_content) and bool(_ac.looks_awaiting_permission(llm_content))
                except Exception:
                    _awaiting = False
                if (_ac_cfg["enabled"] and not dry_run
                        and (exec_tools or _cont_input or _cont_cue or _awaiting)
                        and _auto_continues < _ac_cfg["max"]):
                    # 本轮产出并进全量（落库/done/前端历史用全量；评估器只看本轮+工具史）
                    if llm_content:
                        # ★ 2026-10-28（conv 9736 实证）：**去重式合并**，不再无脑累加。
                        #   真机：一条消息 37,620 字符、7 个代码块 md5 全同 —— 用户报
                        #   「生成代码中还是存在多份相同的内容」。同一任务的续跑轮
                        #   模型每轮都重写完整报告，逐轮追加就堆成 7 份。
                        _full_content = _ac.merge_round_content(_full_content, llm_content)
                    _verdict = self._evaluate_completion(
                        user_input=user_input, exec_tools=exec_tools, answer=llm_content,
                        provider_id=effective_provider, agent_name=agent_def.name,
                        model_params=_agent_mp, guard=_lg)
                    # ★ 2026-10-27：评估器判「必须用户拍板」⇒ **直接变成澄清卡片**。
                    #   这就是「需要澄清为啥不弹卡片、非要终止后让我另起一轮」的机制答案：
                    #   以前只有"模型主动调用 ask_user 工具"才弹卡，而模型习惯把问题写在正文里
                    #   （提示词写了"不要用纯文本请确认"，实测遵循率不稳）。
                    #   现在由**评估器**兜住：它读到"先删掉再重来，还是留着？"这类问题，
                    #   给出 needs_user + question + options ⇒ 这里复用 ask_user 的收口
                    #   （落 pending_clarify + 发 clarify_ask 卡），用户在卡片上点一下就续跑。
                    # ★ P1-2（2026-10-28）`has_default` ⇒ **不转卡**，转"按默认推进"。
                    #   区分两种 needs_user：
                    #     · user_only  —— 信息只有用户有（问就一定做错）⇒ 转卡（现状）
                    #     · has_default—— 有合理默认值 ⇒ 按默认推进 + 答复里标注假设
                    #   实测背景：6/89 次评估落在 needs_user，全转卡 ⇒ 用户感知"停"；
                    #   其中一部分（如"两个口径"）任选一个都能推进。
                    #   ⚠️ 必须在 clarify_from_verdict **之前**转换 ——
                    #      clarify_from_verdict 对 has_default 返回 None，若不先转，
                    #      这条结论会被完全丢弃（既不打回卡也不续跑 = 静默结束）。
                    # ★ 2026-10-28（conv 9732 实测）：**待办未做完时，先别问人**。
                    #   真机现象：评估器给出 needs_user（"是否按当前方案校验后写入"），
                    #   系统立刻打回澄清卡片 ⇒ 用户被问一次，而**只读的校验一步都没做**。
                    #   违反用户原话「完成校验后写入，写入前确认」：确认是写入门，
                    #   不该把校验也挡在门外。
                    #   ⚠️ 判定一律走 `_ac.defer_clarify`（纯函数，**唯一实现**）——
                    #     本处只接收结果，不自己写"怎么覆盖 verdict"的分支
                    #     （否则又成了"同一语义两处各写一份"，本仓已被门禁判红过一次）。
                    #   ⚠️ 顺序（2026-10-28 修）：**先过待办闸，再判要不要问人**。
                    #     待办闸是评估器结论的上位约束，"done 与否"必须先定；
                    #     此前待办闸排在澄清块之后 ⇒ 本处引用未赋值的 `_tg` ⇒
                    #     真机 conv 9734 抛 `cannot access local variable '_tg'`、
                    #     整轮流中断（连评估环节都没跑到）。
                    _verdict, _tg = self._apply_todo_gate(_verdict, conversation_id)
                    if not _tg.get("fallback"):
                        try:
                            yield {"type": "stage", "name": "待办验收闸",
                                   "status": "done",
                                   "detail": _tg.get("reason") or ""}
                        except Exception:
                            pass
                    _verdict, _defer_note, _clarify_defer_n, _clarify_defer_rem = \
                        _ac.defer_clarify(_verdict, _tg,
                                          n=_clarify_defer_n,
                                          last_rem=_clarify_defer_rem)
                    if _defer_note:
                        try:
                            yield {"type": "stage", "name": _ac.AUTO_EVAL_STAGE,
                                   "status": "done", "detail": _defer_note}
                        except Exception:
                            pass
                    try:
                        _dp = _ac.default_progress_verdict(_verdict)
                    except Exception:
                        _dp = None
                    if _dp:
                        _verdict = _dp
                        try:
                            yield {"type": "stage", "name": _ac.AUTO_EVAL_STAGE,
                                   "status": "done",
                                   "detail": "该问题有合理默认值 —— 按默认推进，"
                                             "并在答复中标注假设（不打断你）"}
                        except Exception:
                            pass
                    try:
                        _cq = _ac.clarify_from_verdict(_verdict)
                    except Exception:
                        _cq = None
                    if _cq:
                        try:
                            yield {"type": "stage", "name": _ac.AUTO_EVAL_STAGE,
                                   "status": "done",
                                   "detail": "需要用户拍板后才能继续 —— 已转为澄清卡片"}
                        except Exception:
                            pass
                        # ⚠️ 不能用 `break` 交给下方 `if _clarify_req:`：那块在
                        #   **最终生成之前**（4822 行），而本处是生成之后 ⇒ break 会跳过它、
                        #   直接进 persist，卡片永远发不出去（本轮实测踩到的位置陷阱）。
                        #   故这里直接复用同一收口方法（生成器），发完 done 即 return。
                        yield from self._clarify_finish_events(
                            conversation_id, user_input, intent, branch, attachments,
                            forced_intent, skill_name, team, _cq,
                            source="auto_continue",
                            title="❓ AI 需要你在两个方案间拍板（评估器判定）",
                            content=(llm_content or ""),
                            exec_card=_interim_exec_card())
                        return
                    _deadline_left = (_ac_deadline - time.time()) if _ac_deadline else None
                    # ★ P1-1（2026-10-28）预算降级阈值（配置化，默认 0.7）+ 硬停线（0.95）。
                    #   ★ 2026-10-28 二次修正（conv 9730 实证）：到 degrade 阈值**不再停**，
                    #   而是「收紧续跑」—— 只做剩余关键步骤；只有到硬停线或真耗尽才停。
                    #   原语义在 86% 就停，而当时还剩 14% ≈ 2~3 轮，任务差一步没做完。
                    try:
                        from core import config as _cfg_dr
                        _degrade_ratio = float(_cfg_dr.get(
                            "agent", "budget_degrade_ratio", 0.7) or 0.0)
                        _hard_stop_ratio = float(_cfg_dr.get(
                            "agent", "budget_hard_stop_ratio", 0.95) or 0.0)
                    except Exception:
                        _degrade_ratio = 0.0
                        _hard_stop_ratio = 0.0
                    # ★ P0-1（2026-10-28）验收驱动：待办闸是评估器的**上位约束**。
                    #   语义（fallback / done / next_step 怎么算）**只在 autopilot
                    #   的 _apply_todo_gate 写一份** —— 两处同构接线共用它，
                    #   绝不各写一份（各写一份必然错位）。
                    #   fallback（无待办 / 开关关）⇒ 原样沿用评估器结论
                    #     （逐字节兼容 D-3，不影响 98.4% 只读问答）。
                    #   实测依据：msg 3985「建议下一步执行校验」被评估器判 done=true，
                    #   且只改提示词纠正不了（1/2）⇒ 必须有客观清单可查。
                    #   ⚠️ 2026-10-28：待办闸已**前移**到澄清转换之前（见上方同段注释）——
                    #     那里已应用过一次，此处**不得再应用一遍**（重复应用会把
                    #     澄清延后已经算好的 next_step 又覆盖回原值，语义互相打架）。
                    #     下方 `_cont/_why` 直接用前面那份 `_verdict` / `_tg`。
                    # ★ 2026-10-28（conv 9732）：**待办未做完时先别问人**已在前方处理。
                    _cont, _why = _ac.decide(
                        _verdict, used=_auto_continues, max_used=_ac_cfg["max"],
                        seen_steps=_ac_seen_steps, deadline_left_s=_deadline_left,
                        budget_left=_lg.budget_left(),
                        budget_ratio=_lg.budget_ratio(),
                        degrade_ratio=_degrade_ratio,
                        hard_stop_ratio=_hard_stop_ratio)
                    # ★ P1-1（2026-10-28）降级收尾：把「待续清单」**写进答复**。
                    #   为什么必须写：budget 闸本来就会停，但停得"无声" ⇒
                    #   用户看到的是"突然断了"（实测 11/89 次）。
                    #   写清单后变成"分段推进"，用户知道还差什么、怎么继续。
                    #   ⚠️ 2026-10-28 二次修正（conv 9730 实证）：**必须同时写进
                    #      `_full_content`** —— 本轮末尾有 `if _full_content: llm_content =
                    #      _full_content`，只往 `llm_content` 追加会被静默覆盖掉。
                    #      那次真机：清单确实生成了，用户一个字也没看到（含"回复继续"指引）。
                    if (not _cont) and ("降级收尾" in str(_why)):
                        try:
                            _rem = (_verdict or {}).get("remaining") or []
                            _nt = _ac.build_degrade_notice(
                                _rem, _lg.budget_ratio(), _hard_stop_ratio or _degrade_ratio)
                            if _nt:
                                llm_content = ((llm_content + _nt) if llm_content else _nt)
                                # 告知类文本（待续清单）**必须追加**，不能走产物去重
                                # （它不是"同一份产物的重述"，合并会被判成重复而吞掉）
                                _full_content = ((_full_content + _nt)
                                                 if _full_content else _nt)
                        except Exception:
                            pass
                    # 评估发生过就透传（无论结论）——用户看得到「为什么停 / 为什么继续」
                    try:
                        yield {"type": "stage", "name": _ac.AUTO_EVAL_STAGE,
                               "status": "done", "detail": _why}
                    except Exception:
                        pass
                    if _cont:
                        _auto_continues += 1
                        _ac_seen_steps.append(str(_verdict.get("next_step") or ""))
                        # 续跑 = 崭新的一轮 ReAct：本轮答复 + 续跑指令并进 messages。
                        # ⚠️ assistant 消息必须带 content（部分 provider 拒绝空 content）。
                        messages.append({"role": "assistant", "content": llm_content})
                        _next_instr = _ac.continuation_prompt(
                            user_input, _verdict, _auto_continues)
                        # ★ 2026-10-28：预算吃紧时**追加收紧指令**（"收紧续跑"）。
                        #   只说"继续"会让模型又去开新检索 ⇒ 一轮就把最后额度烧完；
                        #   必须明确"只做剩余关键步骤、禁止新开查询"。
                        if "收紧续跑" in str(_why):
                            _next_instr += (
                                "\n\n【预算吃紧】token 预算已用 %.0f%%，"
                                "只做上面这一步（以及它的直接后继），"
                                "**不要再新开工程检索/包树遍历/大范围查询**；"
                                "若该步需要用户确认，就把确认点讲清楚并停下。"
                                % (_lg.budget_ratio() * 100))
                        messages.append({"role": "user", "content": _next_instr})
                        _ac_delta = f"（harness 持续工作）{_why}"
                        exec_notices.append(({"kind": "progress", "text": _ac_delta}))
                        # ★ type=progress（#10 拆型：系统提示独立事件类型，不再混入 reasoning）
                        #   + kind=progress（B2-10 兼容层：老消费方按 kind 分流仍生效）。
                        #   这是**系统进度提示**不是模型 CoT，必须走 procAddSystemNotice
                        #   通道，不能混进"AI 思考"框。
                        yield {"type": "progress", "delta": _ac_delta, "kind": "progress",
                               "round": _auto_continues, "phase": "autopilot"}
                        try:
                            yield {"type": "stage", "name": _ac.AUTO_CONTINUE_STAGE,
                                   "status": "done",
                                   "detail": f"第 {_auto_continues}/{_ac_cfg['max']} 次："
                                             f"{str(_verdict.get('next_step') or '')[:80]}"}
                        except Exception:
                            pass
                        continue   # ← 外层循环顶：状态复位后开启下一轮 ReAct
                # ★ P0-2（2026-10-28）harness 级强制闸：**最终生成之后、persist 之前**。
                #   对齐官方 Stop hook："the turn doesn't end until the typecheck passes"
                #   —— 由 harness 执行，**不依赖模型配合**。
                #
                #   ⚠️ 位置很关键（第一版插错了，py_compile 直接报
                #      "'continue' not properly in loop"）：
                #      本闸必须在 `break` **之前**（即仍在 while@4533 体内）——
                #      break 之后就已经跳出循环，continue 会变成语法错误。
                #      而 break 之前 = 尚未收口 = 正是"拦住不让结束"的时机。
                #
                #   语义（唯一一份，IO 在 autopilot._harness_gate）：
                #     ① 有未通过验收的待办 ⇒ **不结束本轮**，把失败明细回灌、继续 ReAct
                #     ② 连续阻断达上限（默认 8）⇒ 强制放行，且**如实告知「未完成」**
                #     ③ 无待办 / 开关关 ⇒ 完全不介入（逐字节兼容 D-3）
                try:
                    _hgate = self._harness_gate(conversation_id, _harness_blocks,
                                             prev_remaining=_harness_prev_rem,
                                             stall_streak=_harness_stall)
                except Exception:
                    _hgate = {"block": False, "feedback": "", "force_release": False,
                              "notice": "", "remaining": [], "blocks_used": _harness_blocks,
                              "reason": "harness 闸异常 ⇒ 不介入", "failures": []}
                if _hgate.get("block"):
                    _harness_blocks = int(_hgate.get("blocks_used") or 0)
                    _harness_prev_rem = list(_hgate.get("remaining") or [])
                    _harness_stall = int(_hgate.get("stall_streak") or 0)
                    # 本轮产出并进全量（被拦下的这轮内容也要保留）
                    if llm_content:
                        _full_content = _ac.merge_round_content(_full_content, llm_content)
                    try:
                        yield {"type": "stage", "name": "验收未通过 · 继续工作",
                               "status": "done",
                               "detail": _hgate.get("reason") or ""}
                    except Exception:
                        pass
                    # 回灌失败明细 ⇒ 驱动下一轮 ReAct（相当于官方 Stop hook 的 exit code 2）
                    messages.append({"role": "assistant", "content": llm_content})
                    messages.append({"role": "user", "content": _hgate.get("feedback") or ""})
                    try:
                        yield {"type": "progress",
                               "delta": "（harness 验收闸）%s" % (_hgate.get("reason") or ""),
                               "kind": "progress", "round": _harness_blocks,
                               "phase": "harness"}
                    except Exception:
                        pass
                    continue   # ← 回外层循环顶：状态复位后开启下一轮 ReAct
                if _hgate.get("force_release"):
                    # ★ 强制放行：**必须如实告知「未完成」**，不得伪装成完成
                    _harness_blocks = int(_hgate.get("blocks_used") or 0)
                    _notice = _hgate.get("notice") or ""
                    if _notice:
                        # ⚠️ 2026-10-28：与降级清单同源的坑 —— 只写 `llm_content`
                        #   会被下面的 `llm_content = _full_content` 覆盖掉。
                        #   凡"收尾告知"必须两处都写（用户可见的那一份才是有效的那一份）。
                        llm_content = ((llm_content + "\n\n" + _notice)
                                       if llm_content else _notice)
                        _full_content = ((_full_content + "\n\n" + _notice)
                                         if _full_content else _notice)
                    try:
                        yield {"type": "stage", "name": "验收闸强制放行",
                               "status": "done",
                               "detail": _hgate.get("reason") or ""}
                    except Exception:
                        pass
                break               # ← 外层循环出口：收口（落库/done 只走一次）
            # 落库/done 用跨轮全量产出（续跑轮内容已在评估点并进 _full_content）
            if _full_content:
                llm_content = _full_content
            # ★ 2026-10-27（会话 9629 msg 4892 实测）：**工具调用标记泄漏清洗**。
            #   那一轮正文末尾出现 `<｜｜DSML｜｜ invoke name="export_sysml_v2">` +
            #   一串参数残骸（`STATUS` / `2114956963811217410` / `1` / `100`）。
            #   成因：模型把工具调用写成了正文，协议层没解析到 tool_calls ⇒
            #   ① 那些调用**根本没执行**（随后模型自述"工具额度已用尽"）；
            #   ② 机器标记原样呈现给用户。
            #   这里从不漏给用户看；同时留痕，便于统计"模型不走标准 tool_calls"的比例。
            try:
                from agent.utils import strip_leaked_tool_markup as _slm
                llm_content, _leak_n = _slm(llm_content)
                if _leak_n:
                    self._leaked_tool_markup = _leak_n
                    try:
                        yield {"type": "stage", "name": "协议自愈",
                               "status": "done",
                               "detail": "清理了 %d 处泄漏的工具调用标记（未被执行）" % _leak_n}
                    except Exception:
                        pass
            except Exception:
                pass
            # SysML v2 视图联动：LLM 输出含 SysML 代码 → 解析并投影各视图 ViewModel（会话内即时预览，不落库）
            sysml_views = self._gen_sysml_views(llm_content, intent, user_input)
            yield {"type": "stage", "name": _gen_stage_name(intent, agent_def), "status": "done"}
            # 子智能体执行完成（携带 intent/hil_level，供前端状态面板一致展示）
            yield {"type": "agent", "status": "done", "name": agent_def.name,
                   "display_name": agent_def.name, "intent": intent, "hil_level": hil_level}

            # ── 阶段 4：落库（dry_run=True 时跳过，供编排子任务流式执行不落库）──
            yield {"type": "stage", "name": "写入会话", "status": "run"}
            msg_id = 0
            if not dry_run:
                # P0-3：主会话产出记忆沉淀（LLM 提炼 or 规则降级，每会话限 2 次）
                self._deposit_session_memory(user_input, llm_content, intent)
                # P0-5：技能使用反馈采集（主会话直行路径）
                self._record_skill_feedback(conversation_id=int(conversation_id or 0), intent=intent, output_content=llm_content)
                card_data = json.dumps({
                    "intent": intent,
                    "agent": agent_def.name,
                    "hil_level": hil_level,
                    "kb_tags": kb_tags,
                    "skill_hits": self._last_skill_hits,
                    "slots": slots,
                    "source": retrieval["source"],
                    "confidence": retrieval["confidence"],
                    "graph_count": retrieval["graph_count"],
                    "vector_count": retrieval["vector_count"],
                    "used_mock": llm_info["used_mock"],
                    "provider": llm_info["provider"],
                    "citations": _citations_payload(retrieval.get("chunk_hits")),  # 问答可解释性：[n] 引用来源
                    # V2.4 会话内执行过程持久化（前端历史消息按序还原思考/子智能体/工具调用）
                    "exec": {
                        # 2026-10-27（截断清单 P2-D1）：4000 → 8000。exec.reasoning 是历史消息
                        #   还原"当时怎么想的"的唯一载体，4000 处腰斩后回看只剩开头。
                        "reasoning": "".join(exec_reasoning)[:8000] or None,
                        # ★ 2026-10-28：系统进度提示单独落库（带 kind），
                        #   前端据此**收编到对应执行环节**，而不是堆成一个
                        #   统一的「💭 思考过程」大块（用户明确反馈过这点）。
                        #   与 reasoning 分开存 = 心智链与系统告知不混。
                        "notices": (exec_notices[:60] or None),
                        "agent": agent_def.name,
                        "tools": exec_tools,
                        # ★ 2026-10-27：意图理解摘要随消息持久化 —— 刷新后历史还原路径
                        #   （06-cards.procBlocksHtml）据此重建「我理解你要的…」那一行。
                        #   不存的话：刷新前看得到、刷新后消失，同一份数据两套呈现
                        #   （本仓已因此被反馈过多次）。
                        "brief": getattr(self, "_last_intent_brief", None) or None,
                        # ★ 2026-10-28（用户报「我明明把两个开关都开了」但跑起来当关的用）：
                        #   把**本轮真正读到的闸门值**随消息落库。
                        #   此前"开没开"只存在于配置文件里，用户无法在会话中核对 ⇒
                        #   界面下拉框看起来是"开启"（改完没点保存 / 被别的保存覆盖）
                        #   而服务端读到的是 False，双方各信一半，排障必然跑偏。
                        #   现在只要看这条卡片就知道"这一轮是按开还是按关跑的"。
                        #   ⚠️ 必须 fail-safe：这是诊断信息，取不到就给 None，
                        #   **绝不能**因为它抛异常而把整条消息的落库带崩
                        #   （本行第一版写成未定义的 `_gates_snapshot()`，
                        #     实测 conv 9728 直接 `name is not defined` ⇒
                        #     落库中断、只能走 _persist_partial_stream 兜底存正文，
                        #     卡片退化成 `[]`）。
                        "gates": _gates_safe(),
                    },
                    "sysml_views": sysml_views,
                    "submitted_by": audit_user(user) if isinstance(user, dict) else "会话发起人",
                    **self._build_rich_card(intent, retrieval, branch, user_input, provider_id),
                }, ensure_ascii=False)
                with db_conn() as conn:
                    # 2026-09-29：user 消息已在 execute_stream 入口落库，此处只插 assistant（防重复）
                    cursor = conn.execute(
                        "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                        (conversation_id, "assistant", llm_content, self._get_card_type(intent), card_data)
                    )
                    msg_id = cursor.lastrowid
                    # ★ 2026-10-17（米爸反馈4）：工具结果取回的 SysML 文档一并归档
                    _archive_artifacts(conn, conversation_id, msg_id, card_data, llm_content, intent,
                                       tool_docs=getattr(self, "_tool_sysml_docs", None))
                    # ★ 2026-10-27：归档完成后**回填任务行的 artifact_id**，否则
                    #   `open_jobs`（判据 artifact_id=0）下一轮会把同一任务再读一遍并
                    #   生成重复产物（每轮 message_id 不同，幂等键挡不住）。
                    try:
                        from agent import async_jobs as _aj2
                        _aj2.link_archived(conn, conversation_id,
                                           getattr(self, "_tool_sysml_docs", None))
                    except Exception:
                        pass
                    self._tool_sysml_docs = []      # 已登记，防重复归档
                    self._tool_doc_pages = {}       # 分页累积状态同步清零
                    # CIA：变更影响分析记录（FR-CIA-3 每次分析结果快照落库，可追溯）
                    _archive_impact_analysis(conn, conversation_id, msg_id, card_data, user_input)
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
                    # 话题打标：落库后立即打标（含刚插入的当前轮消息）
                    try:
                        self._tag_topics(conn, conversation_id)
                    except Exception:
                        pass
            yield {"type": "stage", "name": "写入会话", "status": "done"}

            # 通用报告服务：结构化 Report（SP-R 透传报告类型）
            report = None
            if intent == "report_generation":
                try:
                    from report_generator import report_generator as _rg
                    report = _rg.structure(user_input[:80], llm_content, report_type=report_type)
                except Exception:
                    report = None
            # P0-2 DST：回写会话级意图状态（非 dry_run 验证运行）
            if not dry_run:
                self._save_conversation_dst(conversation_id, intent, slots)
            # P1-②（2026-10-07）本轮收口：丢弃未消费的 follow-up。
            # ★ 为什么必须清：队列跨请求存在。若本轮正常结束但还剩着（例如用户在
            #   done 之后、连接关闭之前又投了一条），**下一轮会莫名多出一段补充**，
            #   表现为"AI 突然提起我上一轮之后才说的话"。⇒ 每轮结束即清。
            if conversation_id:
                try:
                    _fup.drop(conversation_id)
                except Exception:
                    pass
            _intent_meta = self.router.get_last_meta()
            yield {"type": "done", "data": {
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
                # ★ 2026-10-17（米爸「harness 持续工作」）：本轮请求内自主续跑次数
                #   （0 = 一轮收口；>0 = 评估器放行了 N 次续跑，前端/排查可追溯）。
                "auto_continues": _auto_continues,
            }}
        except GeneratorExit:
            # 2026-09-29（用户五轮反馈4）：客户端断开（停止按钮/关页/网络断）→ 已流出内容固化落库。
            #  此前直接 raise，浏览器里看得到的已生成内容一刷新就没了（库中 0 条）。
            #  去重：用户点停止时前端会先走 /messages/partial 固化 → 这里查最后一条消息，
            #  已是"部分固化"消息则跳过，避免双写。
            # ★ 2026-10-17（harness 持续工作）：部分固化带**跨续跑轮全量**——
            #   自主续跑轮的内容已并进 _full_content，只固化 llm_content（末轮）
            #   会把前几轮的产出丢掉（用户在停止前明明看得到）。
            # ★ 2026-10-27：再与 `_streamed_acc` 取**更长者** —— 它是"用户真的看到过"的下界。
            #   只看 llm_content/_full_content 会漏掉「被续跑复位/被补充指令重置掉的上一段」
            #   （用户已经看到了，却被当成"没生成过"）。
            self._persist_partial_stream(conversation_id,
                                         _best_partial(_full_content, llm_content,
                                                       _orch_acc, _streamed_acc),
                                         "stop", dry_run=dry_run)
            # P1-②：断连同样要清 follow-up 队列（否则下次接着这个会话时
            #  会消费到"上一轮被中断时没处理完的补充指令"）。
            if conversation_id:
                try:
                    _fup.drop(conversation_id)
                except Exception:
                    pass
            raise
        except Exception as e:
            # 2026-09-29（用户五轮反馈4）：网络错误等异常 → 已生成内容同样固化（带中断说明后缀），
            #  刷新后"之前输出的内容"仍在，不再整体蒸发。
            # ★ 2026-10-17：同 GeneratorExit 分支——带跨续跑轮全量（见上方注释）。
            # ★★ 2026-10-27（用户反馈「中途失败后之前输出的内容没有任何展示」）：
            #   **这里就是那个 bug 的落点**。旧实现只取 llm_content/_full_content/_orch_acc，
            #   而 llm_content 在旧代码里是「流循环结束后才赋值」⇒ 中途抛异常时它是空串
            #   ⇒ 固化判据 `len(text) < 4` 直接跳过 ⇒ 库里 0 条 ⇒ 刷新全丢。
            #   （真库 conv 9629「写入工程」轮：llm_usage 4 次调用、messages 0 条 assistant。）
            #   现在三件事一起兜：① 流循环内**增量**累积 llm_content（根治）；
            #   ② `_streamed_acc` 记录"真的流出去过什么"；③ 取两者更长者固化。
            self._persist_partial_stream(conversation_id,
                                         _best_partial(_full_content, llm_content,
                                                       _orch_acc, _streamed_acc),
                                         "error", dry_run=dry_run)
            yield {"type": "error", "message": str(e)}
