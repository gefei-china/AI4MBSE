# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：流式执行：编排流 SSE / 直接流式 execute_stream。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *
import queue as _queue  # 编排子任务事件的**实时**转发通道（见 _stream_orchestrated_flow 的 _evq）


class StreamMixin:
    """流式执行：编排流 SSE / 直接流式 execute_stream。"""

    def _stream_orchestrated_flow(self, user_input, conversation_id, branch, intent, agent_def,
                                  hil_level, kb_tags, attachments, slots, user, provider_id,
                                  team_forced: bool = False, stage_hint=None) -> iter:
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
        yield {"type": "stage", "name": "生成与校验", "status": "run"}
        yield {"type": "reasoning", "delta": "（自动编排）检测到多 Agent 协作需求，正在规划子任务…"}
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
        plan_prompt = (
            "你是任务规划器。把目标分解为可并行/串行执行的子任务清单，只输出 JSON：\n"
            '{"tasks": [{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
            ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
            ' "context": "该任务必需的上下文/输入引用(精简事实，可空)"}]}\n'
            f"可用 Agent 池：\n" + "\n".join(pool_lines) + "\n"
            "task_type 可选 agent（走 Agent 完整管线）或 react（多步思考-工具求解）或 llm（纯生成）。\n"
            f"{skill_pool_txt}"
            f"要求：任务数 1~{self._ORCH_MAX_TASKS} 个；每个任务只交付一个明确成果；有依赖关系的用 deps 表达；不要输出其他文字。\n"
            f"目标：{goal}"
        )
        if team_forced:
            # 团队模式：主 Agent（团队负责人）统一调度视角——意图识别 → 拆解 → 计划 → 分派 → 汇总
            _lead_sp = (agent_def.system_prompt or "").strip()[:300]
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
                ' "context": "该任务必需的上下文/输入引用(精简事实，可空)"}]}\n'
                f"可用 Agent 池：\n" + "\n".join(pool_lines) + "\n"
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
        try:
            resp = llm_client.chat([{"role": "user", "content": plan_prompt}], provider_id=provider_id, _intent="planner")
            raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
            data = _AP._parse_json_block(raw) if raw else None
            plan = [t for t in ((data or {}).get("tasks") or []) if isinstance(t, dict) and t.get("key")]
        except Exception:
            plan = []

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
                    elif _et == "reasoning":
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
                # P0-7：分配独立批次号（新号无残留，故不再 clear_run —— 那正是覆盖历史的原因）
                run_id = TaskQueue.new_run_id(conn)
                TaskQueue.create_plan(conn, run_id, plan, assigned_by="session",
                                      conversation_id=int(conversation_id or 0))
            except Exception:
                pass
            done_items, orch_tasks = [], []
            subtasks = []   # P0-2：子任务轨迹（实时事件 + card_data.exec 持久化）
            guard = 0
            # ── Task 7：执行层并发与可靠性（per-agent 信号量 / 子任务超时 / 自动重试 / run 预算护栏）──
            import threading as _th
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac, wait as _wait
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
                    _tctx = build_subtask_context(tk, goal, list(done_items), artifact_digest=_digest)
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
                skill_name2 = cfg.get("skill") or None
                evs, sub_res, sub_has_token = [], "", False
                tok_used = 0
                _t0 = _t.time()
                try:
                    sub = type(self)()  # 拆包后避免循环导入：type(self)==AgentPipeline
                    sub._load_db_agents(user)
                    # T8：编排子任务写操作受控上下文（_exec_tool_call 据此暂存写请求，汇总后统一确认）
                    sub._orch_subtask = True
                    sub._tool_run_ctx = int(run_id or 0)
                    sub._tool_task_key = tkey
                    # Task 9：隔离命名空间 run:tkey + kb_scope 预留
                    # （execute_stream 无 kb_scope 参数 → 跳过透传；存属性供后续检索/记忆过滤消费）
                    sub._ns_ctx = f"{run_id}:{tkey}"
                    if isinstance(cfg.get("kb_scope"), dict) and cfg.get("kb_scope"):
                        sub._ns_kb_scope = dict(cfg["kb_scope"])
                    try:
                        for ev in sub.execute_stream(query, 0, branch, provider_id, _sub_att,
                                                     forced_intent=tk.get("agent_id") or None,
                                                     skill_name=skill_name2, user=user, dry_run=True):
                            _hb[tkey] = _t.time()   # P0-7：任何产出（token/reasoning/tool）都算"活着"
                            _et = ev.get("type")
                            if _et == "token":
                                sub_has_token = True
                                sub_res += ev.get("delta") or ""
                                evs.append(ev)
                                _evq.put(ev)          # 实时转发（就地输出）
                            elif _et == "reasoning":
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
                                normalize_text as _sp_normalize
                            _s0 = str(sub_res or "").strip()
                            _parsed = None
                            if _s0.startswith("{"):
                                try:
                                    _parsed = json.loads(_s0)
                                except Exception:
                                    _parsed = None
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
                        return tkey, ttitle, ok, sub_res, evs, "", int((_t.time() - _t0) * 1000), tok_used, _summary_proto
                    except Exception as e:
                        return tkey, ttitle, False, "", [], str(e)[:150], int((_t.time() - _t0) * 1000), 0, None
                    finally:
                        sub._orch_subtask = False
                except Exception as e:
                    return tkey, ttitle, False, "", [], str(e)[:150], int((_t.time() - _t0) * 1000), 0, None

            def _worker(tk, task_pool):
                """Task 7 执行包装：per-agent 信号量限流 + 子任务超时 + 自动重试。

                信号量语义：acquire 在 worker 线程进入时执行；release 只在本 worker 线程
                自然结束（成功 / 超时 / 重试耗尽返回）时执行——超时任务的后台子任务线程
                不中断、继续在后台运行直至自然结束（期间占用 task_pool 线程）；额度随
                worker 返回即释放，保证后续批次不被超时任务阻塞。
                """
                tkey = tk.get("task_key") or "t"
                ttitle = tk.get("title") or tkey
                _t0 = _t.time()
                sem = agent_sems.get(tk.get("agent_id"))
                if sem is not None:
                    sem.acquire()   # 进入该 Agent 并发额度（同 Agent 并发 ≤ max_concurrency）
                try:
                    retry_left = _max_retries
                    last_res = None
                    while True:
                        fut = task_pool.submit(_run_subtask, tk)
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
                        if retry_left <= 0:
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
                        _t.sleep(_backoff * (_max_retries - retry_left))  # 退避：首次 1s，逐次累乘
                finally:
                    if sem is not None:
                        sem.release()   # 仅本 worker 线程自然结束时释放

            while TaskQueue.pending_count(conn, run_id) > 0 and guard < 200:
                guard += 1
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
                    yield {"type": "reasoning",
                           "delta": "（自动编排）达到运行预算限制（任务数或 token），未派发子任务已标记，提前进入汇总…"}
                    break
                ready = TaskQueue.ready_tasks(conn, run_id)
                if not ready:
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
                           "agent": tk.get("agent_id") or "", "deps": _deps_map[tkey]}
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
                    _futs = {_exec_pool.submit(_worker, tk, _task_pool): tk for tk in ready}
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
                            tkey, ttitle, tk_ok, sub_res, evs, _t_err, _t_lat, _tok, _summary = _fut.result()
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
                                                   "proto_status": (_summary or {}).get("status")})
                            else:
                                TaskQueue.fail(conn, tk["id"], _t_err or "子任务未产生输出")
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
                            subtasks.append({"key": tkey, "title": ttitle, "agent": tk.get("agent_id") or "",
                                             "deps": _deps_map.get(tkey, []),
                                             "status": "done" if tk_ok else "failed",
                                             # 落库这份也带用户向摘要：否则**刷新历史会话**时前端只能回退到
                                             # 内部交接语（老数据显示"承接上游…"会让人以为没改）
                                             "ui_summary": _ui_summary,
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
                                             "retry_count": int(_tk.get("retry_count") or 0),
                                             "summary_status": "partial"})
                except Exception:
                    pass
            # 4) 汇总（P1-1 总结节点）
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
                yield {"type": "reasoning",
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
                    else:  # 旧数据（无协议 summary）→ normalize_text 兼容视为 full
                        try:
                            _summaries.append(_sp.normalize_text(_it.get("result")))
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
                        _evs = _sm.get("evidence") or []
                        if _evs:
                            _sec += "\n证据：" + "；".join(
                                f"{e.get('source')}:{e.get('ref')}" for e in _evs[:10])
                        _rks = _sm.get("risks") or []
                        if _rks:
                            _sec += "\n风险/待确认：" + "；".join(
                                f"[{r.get('level')}]{r.get('title')}" for r in _rks[:5])
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
                for _tk in orch_tasks:
                    if _tk.get("status") != "done" and _tk.get("key") not in _missing_keys:
                        _missing_keys.append(_tk.get("key"))
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
            # Task 10：agg_status != full → 显式标注「编排部分完成」+ reasoning 事件（状态同时写入 card_data）
            if _agg_status != "full" and done_items:
                _gap_txt = f"，缺口：{','.join(_missing_keys)}" if _missing_keys else ""
                orch_content = f"（编排部分完成：{len(_missing_keys)} 个交付物缺失/失败{_gap_txt}）\n\n" + orch_content
                yield {"type": "reasoning",
                       "delta": f"（自动编排）编排部分完成（{_agg_status}）：{len(_missing_keys)} 个交付物缺失/失败"
                                + _gap_txt}
            # P0-1（T3）：反思闭环——汇总后质量评审 → 未达标修订（复用画布 reflection 公共评审）
            reflection_meta = None
            if done_items:
                yield {"type": "reasoning", "delta": "（自动编排）对汇总结果进行质量评审…"}
                from workflows.refine import RefineGate
                # Task 10：agg_status 传入 RefineGate——partial 时不自动重派（避免循环），仅评审 + 追加人工复核建议
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
                }
                if reflection_meta.get("score") is not None:
                    yield {"type": "reasoning", "delta": (
                        f"（自动编排）质量评审：{reflection_meta['score']}/100 · "
                        f"{'通过' if reflection_meta['passed'] else '未通过，已自动修订 ' + str(reflection_meta['rounds']) + ' 轮'}")}
                orch_content = _ref.get("content") or orch_content
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
                    yield {"type": "reasoning",
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
        yield {"type": "reasoning", "delta": f"（自动编排）{_done_wording if not degraded else '单 Agent 直行完成'}，汇总最终结论…"}
        for _chunk in iter_stream_chunks(orch_content):
            yield {"type": "token", "delta": _chunk}
        yield {"type": "stage", "name": "生成与校验", "status": "done"}
        # 6) SysML 视图（自动编排 LLM 摘要可能丢弃子任务交付的 SysML v2 代码 → 先补回再投影）
        _pre_sysml_len = len(orch_content)
        orch_content = self._ensure_sysml_blocks(orch_content, done_items)
        # 6.1) P0-3：补回的内容是「事后拼接」的，必须增量补吐，否则前端只看到摘要，
        #      SysML 代码块要等 done 重渲染才出现（体验上等于「又突然冒出一大段」）。
        if len(orch_content) > _pre_sysml_len:
            for _chunk in iter_stream_chunks(orch_content[_pre_sysml_len:]):
                yield {"type": "token", "delta": _chunk}
        sysml_views = self._gen_sysml_views(orch_content, intent, user_input)
        # 7) 落库 + done
        llm_info = {"provider": "自动编排", "model": "-", "used_mock": False,
                    "latency_ms": int((_t.time() - t0) * 1000)}
        yield {"type": "stage", "name": "写入会话", "status": "run"}
        card_data = json.dumps({
            "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
            "orchestrated": True, "degraded": degraded,
            "team": (agent_def.intent_name or agent_def.name) if team_forced else None,  # 团队模式：主 Agent 团队负责人
            "orchestrated_status": _agg_status,   # Task 10：编排汇总三态（full/partial/failed）
            "quality_gate_gaps": _gate_gaps,      # P0-3：质量门禁缺口说明（与 orchestration.py 同键）
            "run_budget": {"used_tokens": run_tokens, "budget": run_token_budget,
                           "hit": budget_hit, "executed": executed_count, "max_tasks": _max_tasks},
            "plan": orch_tasks, "sysml_views": sysml_views,
            "source": "none", "confidence": 0, "graph_count": 0, "vector_count": 0,
            "used_mock": llm_info["used_mock"], "provider": llm_info["provider"],
            "saved_flow_id": saved_flow_id,   # P0-1：编排沉淀的工作流 id（前端「另存为流程」）
            "saved_flow_name": f"自动编排·{(goal or '')[:18]}" if saved_flow_id else None,
            "reflection": reflection_meta,    # P0-1（T3）：反思闭环评审轨迹（评分/修订轮次/问题）
            "exec": {"reasoning": f"自动编排：{len(orch_tasks)} 个子任务由多 Agent 协作执行",
                     "agent": agent_def.name, "tools": [],
                     "subtasks": subtasks},   # P0-2：子任务轨迹持久化（历史消息还原）
        }, ensure_ascii=False)
        # P0-3：编排产出记忆沉淀（LLM 提炼 or 规则降级，每会话限 2 次）
        self._deposit_session_memory(user_input, orch_content, intent)
        # P0-5：技能使用反馈采集（流式编排路径）
        self._record_skill_feedback(run_id=int(conversation_id or 0), intent=intent, output_content=orch_content)
        with db_conn() as conn2:
            # 2026-09-29：user 消息已在 execute_stream 入口落库，此处只插 assistant（防重复）
            cur = conn2.execute(
                "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                (conversation_id, "assistant", orch_content, "text", card_data)
            )
            msg_id = cur.lastrowid
            _archive_artifacts(conn2, conversation_id, msg_id, card_data, orch_content, intent)
            conn2.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, intent=? WHERE id=?",
                          (intent, conversation_id))
            conn2.execute(
                "INSERT INTO audit_logs (user_name, event_type, detail, result) VALUES (?,?,?,?)",
                ("王工", "llm_chat", f"会话#{conversation_id} · 自动编排（{len(orch_tasks)}子任务）· 意图:{intent}", "success"))
        try:
            conn.close()
        except Exception:
            pass
        yield {"type": "stage", "name": "写入会话", "status": "done"}
        # V2.5：主 Agent（团队负责人）收尾事件——修前缺失致前端「子智能体：团队」停留"执行中"
        yield {"type": "agent", "status": "done", "name": agent_def.name,
               "display_name": agent_def.name, "intent": intent, "hil_level": hil_level}
        yield {"type": "done", "data": {
            "message_id": msg_id, "intent": intent, "agent": agent_def.name, "hil_level": hil_level,
            "kb_tags": kb_tags, "skill_hits": self._last_skill_hits, "slots": slots,
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
                             slots: dict, user) -> iter:
        """P0-1 编排路径流式产出：子任务过程（agent 事件）→ 汇总 tokens → 落库 → done。

        与主路径落库段保持一致（card 含 orchestrated/plan），前端按 stage/agent/token 现有渲染直接可视化。
        """
        # 2026-09-23：此处原有写死的「知识库检索 done/retrieved=False」阶段事件。它无条件出现、
        # 与真实检索无关（编排模式下子任务的 stage 事件不外传，见子任务事件转发只收
        # token/reasoning/tool/done），会让用户看到一个从未真正执行过的「知识库检索·未引用」环节。
        # 现不再预发该阶段：检索真实性由各子任务按其 Agent 的 kb_required 决定。
        yield {"type": "stage", "name": "生成与校验", "status": "run"}
        orch_tasks = ((orch.get("data") or {}).get("tasks")) or []
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
        yield {"type": "reasoning", "delta": _d}
        # P0-3（2026-09-17）：无节流切片 → 类人节奏产出（本路径不做事后补块，保持原行为）
        for _chunk in iter_stream_chunks(orch_content):
            yield {"type": "token", "delta": _chunk}
        yield {"type": "stage", "name": "生成与校验", "status": "done"}
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
            "exec": {"reasoning": f"自动编排：复杂任务分解为 {len(orch_tasks)} 个子任务由多 Agent 协作执行",
                     "agent": agent_def.name, "tools": []},
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
            conn.execute(
                "INSERT INTO audit_logs (user_name, event_type, detail, result) VALUES (?,?,?,?)",
                ("王工", "llm_chat", f"会话#{conversation_id} · 自动编排（{len(orch_tasks)}子任务）· 意图:{intent}", "success")
            )
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
        if len(text) < 4:
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
                if last and last["role"] == "assistant" and "以上为已生成内容" in (last["content"] or ""):
                    return   # 前端停止路径已固化 → 不双写
                conn.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", text + suffix, "text", "{}"))
                conn.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (conversation_id,))
        except Exception:
            pass   # 固化失败不掩盖原始异常

    def execute_stream(self, user_input, conversation_id, branch="dev", provider_id=None,
                       attachments=None, forced_intent=None, skill_name=None, user=None, dry_run=False,
                       team=None, scope_id=None, scope_ids=None, scope=None):
        """流式执行（V2.3.2 优化：AI 输出流式 + 执行过程可观测）。生成器逐段产出 SSE 事件：

        {"type":"stage","name":..., "status":"run|done"}   右侧智能体执行状态
        {"type":"agent","status":"run|done",...}           子智能体执行（路由到的 Agent）
        {"type":"reasoning","delta":...}                    思考过程（LLM reasoning 增量）
        {"type":"tool","status":"run|done",...}             工具调用结果
        {"type":"token","delta":...}                        LLM 增量文本
        {"type":"done","data":{...}}                        最终结果（与 execute 返回结构一致）
        {"type":"error","message":...}
        dry_run: 编排子任务流式执行时置 True（不落库，仅产出事件）。
        team: 团队模式（AI 会话页「工作流」下拉）——主 Agent 团队负责人统一调度：
        意图识别/任务拆分/计划制定/任务分派（委派候选收敛到团队成员）/内容整合输出。
        """
        attachments = attachments or []
        # 2026-09-29：user 消息在流开始即落库（原在流收尾与 assistant 一并 INSERT）。
        # 修「流式进行中库中 0 条消息 → 切页再切回，selectConv 渲染空态覆盖 #stream-ai 现场」；
        # 也顺带修复澄清路径（clarify_ask 提前 return）user 消息从未落库的缺口。
        # 收尾块只插 assistant（三处已同步去除 user INSERT）；dry_run=True（编排子任务流式）不落库。
        if not dry_run:
            try:
                with db_conn() as _conn0:
                    _conn0.execute(
                        "INSERT INTO messages (conversation_id, role, content, msg_type, card_data, attachments) VALUES (?,?,?,?,NULL,?)",
                        (conversation_id, "user", user_input, "text", json.dumps(attachments, ensure_ascii=False))
                    )
                    _conn0.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (conversation_id,))
            except Exception:
                pass   # 入口落库失败不阻断流：收尾 done 事件仍会到达，避免整轮白跑
        # 记忆作用域上下文（对齐 mem0）：本会话的 conversation_id + 当前用户，供记忆读写取作用域
        self._mem_ctx = {"conversation_id": conversation_id, "user": user}
        self._mem_project_id_cache = None   # 每次执行清缓存：缓存只在本请求内有效，防跨会话串味
        try:
            # V2.4 会话内执行过程持久化：累计思考/工具调用 → 写入 card_data.exec（前端历史消息还原）
            exec_reasoning = []
            exec_tools = []
            card_data = "{}"  # dry_run=True（编排子任务流式）不落库，done 事件仍引用 → 兜底空卡
            # 2026-09-29（用户五轮反馈4）：流式部分内容固化 —— llm_content 提前初始化（异常路径引用），
            # _orch_acc 收集编排路径的 token（直行路径已由 llm_content 承担）。
            llm_content = ""
            _orch_acc = []

            def _yield_collect(src, acc):
                """包装编排子生成器：顺手收集 token 增量，供异常/断开时固化已生成内容。"""
                for _ev in src:
                    if isinstance(_ev, dict) and _ev.get("type") == "token":
                        acc.append(_ev.get("delta") or "")
                    yield _ev
            self._load_db_agents(user)  # P0 平台化：DB 驱动 Agent 注册表（P1-8：按用户隔离）
            self._last_skill_hits = []
            # 团队模式：校验主 Agent（团队负责人）→ 定向到主 Agent 并强制编排
            team_forced = False
            team_intent = None
            if team:
                team_intent = self._resolve_team(team)
                if not team_intent:
                    yield {"type": "error", "message": (
                        f"智能体团队不存在或未启用：{team}，请到「Agent 管理」创建主 Agent 并启用后重试")}
                    return
                forced_intent = forced_intent or team_intent
                team_forced = True
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
            if forced_intent and _detected and forced_intent != _detected:
                yield {"type": "reasoning", "delta": (
                    f"（意图定向）已按显式选择执行：{forced_intent}；语义识别结果为 {_detected}，"
                    "两者不一致，如非所愿请去掉团队/Agent 指定后重发")}
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
            agent_def = self.registry.get(intent)
            hil_level = agent_def.hil_level
            effective_provider = provider_id or self.registry.get_provider_id(intent)  # 优化2
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
            yield {"type": "stage", "name": "意图识别", "status": "done", "intent": intent,
                   "agent": agent_def.name, "hil_level": hil_level,
                   "route": _intent_meta["route"], "confidence": _intent_meta["confidence"]}
            # P0-1 置信度三级决策：中置信（弱语义/继承/LLM<0.85）→ 澄清提示（不阻塞执行，供前端改选重发）
            # V2.7：指定智能体时同样给出提示（识别结果与执行 Agent 不一致时如实说明）
            if _intent_meta["needs_clarification"]:
                yield {"type": "clarify", "intent": intent, "confidence": _intent_meta["confidence"],
                       "route": _intent_meta["route"], "message": str(user_input),
                       "detected": _detected if forced_intent and _detected != intent else None,
                       "candidates": self._clarify_candidates(intent)}
            # ── 阶段 1.4（2026-09-26）：**意图确定不了 → 停下来问**，不要自己硬选一个（用户要求）──
            #  与上面那条 `clarify`（细条，"先按猜的跑、你可改选重发"）的本质区别：这里**不执行**。
            #  复用内容级澄清的选择题卡（选项 + 其他/自定义 + 答完续跑），因为"不硬选"的完整机制
            #  （落挂起 → /clarify-answer → 带【澄清补充】续跑）后端已有，不另造一套状态。
            #  放在 `_clarify_detect` **之前**：意图都没定，谈"建模信息够不够"没有意义。
            try:
                _need_iq = self._should_confirm_intent(user_input, _intent_meta)
            except Exception:
                _need_iq = False
            if _need_iq:
                # P0-5：把本次语义意见带进候选排序（只影响顺序，不构成路由决定）
                _iqs = self._intent_confirm_questions(user_input, intent,
                                                      _intent_meta.get("sem_alt"))
                _mid = self._persist_clarify(conversation_id, user_input, intent, branch, attachments,
                                             forced_intent, skill_name, team, _iqs)
                yield {"type": "clarify_ask", "questions": _iqs, "intent": intent,
                       "title": "❓ 我不确定你想做哪件事，请确认",
                       "message": str(user_input)[:200]}
                yield {"type": "done", "data": {
                    "ok": True, "clarify_asked": True, "intent": intent,
                    "content": "意图不确定，需要您确认后再执行（详见澄清卡片）", "msg_type": "clarify",
                    "usage": {}, "questions": _iqs, "message_id": _mid or 0}}
                return
            # ── 阶段 1.5：内容级澄清（信息不清晰 → 选择题确认，回答后续答；打断本次执行）──
            try:
                _clarify_qs = self._clarify_detect(
                    user_input, intent, effective_provider,
                    forced_intent=forced_intent, skill_name=skill_name)
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
                # 团队模式：主 Agent（团队负责人）强制编排——意图识别/拆解/计划/分派/汇总
                self._save_conversation_dst(conversation_id, intent, slots)
                yield from _yield_collect(self._stream_orchestrated_flow(
                    user_input, conversation_id, branch, intent, agent_def, hil_level,
                    kb_tags, attachments, slots, user, effective_provider, team_forced=True,
                    stage_hint=_stage_hint), _orch_acc)
                return
            if not forced_intent and self._needs_orchestration(user_input, intent,
                                                              has_attachments=bool(attachments),
                                                              multi=_multi_intent):
                # P0-1 复用：已发布 planner_auto 沉淀流程语义命中 → 直接执行（省重新规划）
                reused = self._try_reuse_planner_flow(user_input, intent)
                if reused and not dry_run:
                    self._save_conversation_dst(conversation_id, intent, slots)
                    yield {"type": "reasoning",
                           "delta": f"（自动编排）命中已发布沉淀流程「{reused.get('flow_name') or ''}」，按流程模板执行子任务…"}
                    reused["data"] = {"tasks": reused.get("plan") or []}
                    yield from _yield_collect(self._stream_orchestrated(
                        reused, user_input, conversation_id, intent, agent_def, hil_level,
                        kb_tags, attachments, slots, user), _orch_acc)
                    return
                try:
                    # P0-2 DST：编排流式前落会话意图状态（子管道不写主会话状态）
                    if not dry_run:
                        self._save_conversation_dst(conversation_id, intent, slots)
                    yield from _yield_collect(self._stream_orchestrated_flow(
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
                    yield {"type": "reasoning", "delta": (
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
            att_parsed_info = {"parsed": att_parsed, "skipped": att_skipped[:5]}
            if att_vision.get("images"):
                att_parsed_info["vision"] = att_vision   # 留痕：图片到底进没进模型、为什么
            matched_flows = self._match_flows(user_input + (" " + att_text[:500] if att_text else ""))

            # ── 阶段 2：知识检索（#标签 + 附件前 600 字符并入）──
            yield {"type": "stage", "name": "知识库检索", "status": "run"}
            # 按需检索：#标签主动引用 / Agent kb_required 配置 二选一才检索
            # V3：上传附件不再默认触发知识库检索（附件仍作「优先依据」注入 prompt）
            should_retrieve = (bool(req_scope and req_scope.get('ok')) or bool(kb_tags) or bool(agent_def.kb_required)
                               or (intent == "report_generation" and report_type in ("impact", "review")))
            if should_retrieve:
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
            yield {"type": "stage", "name": "知识库检索", "status": "done",
                   "retrieved": should_retrieve, "route": retrieval.get("route", "none"),
                   "route_reason": retrieval.get("route_reason", ""),
                   "kb_scope_warn": retrieval.get("kb_scope_warn")}   # KB-S：白名单失效告警（供前端/抓包）
            # KB-S（2026-09-19）：Agent 文档白名单失效 → 显式提示。否则表现为「Agent 配了知识库依赖
            # 却检索恒为空」，调用方无法区分「库里没有」与「白名单写错了」（实测 4887 块规范恒不可见）。
            _kbw = retrieval.get("kb_scope_warn")
            if _kbw:
                yield {"type": "reasoning", "delta": (
                    "（知识库范围）Agent 配置的文档白名单有 "
                    f"{len(_kbw.get('missing') or [])} 篇不存在，已自动剔除"
                    + ("；因全部失效，本次已暂放宽为不限文档" if _kbw.get("unfiltered") else "")
                    + "。建议到「能力中心 → Agent 管理」核对该 Agent 的知识库范围配置")}
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
                yield {"type": "skill", "status": "done", "names": list(self._last_skill_hits),
                       "note": "技能指令已注入生成阶段（触发词命中，指导本回答的组织方式与工具白名单）"}
            exec_reasoning.append(_r_delta)
            yield {"type": "reasoning", "delta": _r_delta}

            # ── 阶段 3：LLM 流式生成 ──
            yield {"type": "stage", "name": "生成与校验", "status": "run"}
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
            if skill_name:
                try:
                    conn = get_db()
                    row = conn.execute("SELECT name, content, description FROM skills WHERE name=?", (skill_name,)).fetchone()
                    conn.close()
                    if row and row["content"]:
                        skill_block = f"【指定技能：{row['name']}（必须遵循其完整指令）】\n{row['content'][:4000]}\n"
                        # V2.5：手动指定技能也发独立事件（前端「🧩 技能执行」卡可见）
                        yield {"type": "skill", "status": "done", "names": [row["name"]],
                               "note": "指定技能指令已注入生成阶段（完整指令强制遵循）"}
                except Exception:
                    pass
            # P1-26（2026-10-02）：拼接顺序**收敛到 common.assemble_system_prompt**（唯一真源）。
            # 此前本处与 execute.py 各写一份顺序 ⇒ 已发生口径漂移（P1-24/P1-25 只改了 execute.py，
            # 本条**流式主路径未生效**）。分层：L2 身份 → L1 全局静态 →〔分界线〕→ L3 会话级 → L4 每轮级。
            # 2026-09-17 S4（C3=按意图只注入命中 Agent）：角色块用「命中的 agent_def」+ _AGENT_ROLE_CAP 截断。
            system_prompt = assemble_system_prompt({
                # ── L2 身份层（只依赖 agent_def）──
                "role": f"{self._build_role_block(agent_def, cap=_AGENT_ROLE_CAP)}\n",
                "tools": f"可用工具：{', '.join(agent_def.tools) or '无（纯问答直出）'}。\n",
                "tool_rules": ("工具使用约束：仅调用完成当前任务所必需的工具，一次最多调用 2 个；"
                               "工具返回与任务无关、结果为空或已足够作答时，直接基于已有信息回答，禁止反复/连环调用工具。\n"),
                "roster": self._team_roster_block(agent_def),
                # ── L1 全局静态（无参方法，跨 agent/会话完全一致 ⇒ 尽早进入可缓存前缀）──
                "ontology": self._build_ontology_hint(),
                "boundary": self._build_boundary_hint(),
                "output_rules": self._build_output_rules(),
                "citation_rules": self._build_citation_rules(),
                # ════════ SYSTEM_PROMPT_DYNAMIC_BOUNDARY（以下为动态区）════════
                # ── L3 会话级（依赖 intent）──
                "intent_line": f"当前意图：{intent}（Agent: {agent_def.name}，HIL 人机协作级别：{hil_level}）。\n",
                # 问题3：建模类意图强制输出 SysML v2 代码块（含 L0 硬约束卡），供投影视图与「代码/视图」切换
                "model_code_req": self._build_model_code_req(intent, agent_def),
                # ── L4 每轮级（依赖 user_input / 检索 / 建模态）──
                "skill_prompt": self._build_skill_prompt(intent, user_input, user),
                "skill_block": (skill_block if skill_block else ""),
                "template": self._build_prompt_template(intent, user_input, user),
                # P0-3：长期记忆注入（跨会话经验，仅供对齐）
                "memory": self._build_memory_hint(user_input, intent, user),
                # P0：建模上下文注入（当前模型状态工作记忆，MBSE 特有）
                # P0-2（2026-09-19）：传本轮 user_input → 建模上下文改**结构性隔离**
                # （context.model_context_entities='count'：只报「本分支共 N 个」不列实体名；块首带适用范围声明）
                # ⚠️ 「按语义相关性过滤条目」方案经标定实测 dense/bigram 分布重叠、无可用阈值 → 已放弃，别再做
                "model_context": self._build_model_context(branch, conversation_id, user_input),
                # P0 能力：项目级持久记忆注入（Project Constitution，规范/基线防漂移）
                "project_memory": self._build_project_memory(user_input=user_input, conversation_id=conversation_id),
                "slots": (f"【任务拆解（P1 结构化）】\n目标：{slots.get('goal') or '-'}\n"
                          f"实体：{'、'.join(slots.get('entities') or []) or '-'}\n"
                          f"约束：{'；'.join(slots.get('constraints') or []) or '-'}\n"
                          f"范围：{json.dumps(slots.get('scope') or {}, ensure_ascii=False) if slots.get('scope') else '-'}\n" if slots else ""),
                "user_ctx": (user_ctx if user_ctx else ""),
                "attachment": self._build_attachment_block(att_text),
                "retrieval": f"检索到的互联数据：\n{context_text}",
                "report": (f"\n\n{report_prompt}" if report_prompt else ""),
            })
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
                messages = self._apply_total_budget(messages, context_text)
            except Exception:
                pass
            # 缺口B：工具判定——非流式探测 LLM 是否需要工具（Mock 无 tool_calls 时跳过，保持原流式）
            # 优化1：注入观测上下文
            self._tool_intent_ctx = {"intent": intent}
            self._tool_agent_ctx = {"agent": agent_def.name}
            self._tool_conv_ctx = conversation_id
            tools_def = self._build_tools_def(intent, user_input, user)
            tool_injected = False
            llm_content = ""
            # P0-2：多轮 ReAct——探测→执行工具→观察回填→再探测，直到无 tool_calls 或达上限（防死循环）
            max_tool_rounds = 3
            _rnd = 0  # 工具轮次计数（供 reasoning 事件 round/phase 标识；无工具直出时保持 0）
            # 2026-09-17 S3：上一轮已回填的 tool 消息下标（下轮仅清空其 content，不删除消息）
            _prev_tool_idx = []
            for _rnd in range(max_tool_rounds):
                if not tools_def:
                    break
                probe = llm_client.chat(messages, provider_id=effective_provider, stream=False, tools=tools_def)
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
                        for _chunk in iter_stream_chunks(p_content):
                            yield {"type": "token", "delta": _chunk}
                        llm_content = p_content
                    break
                tool_injected = True
                results = []
                # 思考过程：Agent 决策需要调用哪些工具（规划）
                plan = "、".join((tc.get("function") or {}).get("name", "") for tc in ptool_calls[:3])
                _p_delta = f"（{agent_def.name}）判断需要调用工具补充上下文，计划调用：{plan}。开始执行工具…（第 {_rnd + 1} 轮）"
                exec_reasoning.append(_p_delta)
                yield {"type": "reasoning", "delta": _p_delta, "round": _rnd + 1, "phase": "plan"}
                weak_count = 0  # P0-按需工具：本轮弱相关结果数（全弱 → 收敛，禁止连环调用）
                for tc in ptool_calls[:3]:
                    fn = tc.get("function") or {}
                    tname = fn.get("name", "")
                    try:
                        targs = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        targs = {}
                    # 工具调用结果：执行前 → 执行后（流式透传；结果全量返回，前端可展开/复制）
                    yield {"type": "tool", "status": "run", "name": tname, "arguments": targs}
                    _t0 = time.time()   # P0-1 工具耗时：run→done 计时
                    result = self._exec_tool_call(tname, targs)
                    _t_ok = bool(result.get("ok"))
                    _r_full = str(result.get("result") or "")
                    _t_trunc = len(_r_full) > _TOOL_RESULT_CAP
                    _t_result = _r_full[:_TOOL_RESULT_CAP]
                    _e_full = str(result.get("error") or "")
                    _t_error = _e_full[:_TOOL_RESULT_CAP]
                    _t_ms = int((time.time() - _t0) * 1000)   # P0-1：毫秒耗时
                    exec_tools.append({"name": tname, "ok": _t_ok,
                                       "arguments": targs, "result": _t_result,
                                       "error": _t_error, "truncated": _t_trunc,
                                       "elapsed_ms": _t_ms})
                    yield {"type": "tool", "status": "done", "name": tname,
                           "ok": _t_ok, "result": _t_result, "error": _t_error,
                           "truncated": _t_trunc, "latency_ms": _t_ms}
                    _weak = bool(result.get("weak")) or ("未检索到" in _t_result) or ("无新冲突" in _t_result)
                    if _weak:
                        weak_count += 1
                    # 2026-09-17 S3：回填给模型的结果单独封顶（此前用未截断的原始结果，长结果被后续每轮重发）
                    _t_model = str(result.get("result") or "")
                    _t_content = {"ok": result.get("ok"), "result": _t_model[:_TOOL_MODEL_CAP]}
                    if len(_t_model) > _TOOL_MODEL_CAP:
                        _t_content["truncated"] = True
                        _t_content["note"] = "工具结果过长已截断，请基于以上内容作答"
                    if _weak:
                        # 弱相关引导：明确告知 LLM 结果不可用，禁止继续调用工具连环试探
                        _t_content["hint"] = "该工具结果与当前任务弱相关（或未命中实质内容），请直接基于已有信息回答，不要再调用其他工具。"
                    results.append({
                        "tool_call_id": tc.get("id", ""),
                        "role": "tool",
                        "name": tname,
                        "content": json.dumps(_t_content, ensure_ascii=False),
                    })
                # 工具结果回填后进入下一轮（观察 → 再思考）
                if results:
                    # 2026-09-17 S3：上一轮 tool 内容换为省略标记
                    # 不可删除该消息——OpenAI 兼容协议要求 tool 与 assistant.tool_calls 成对出现
                    for _pi in _prev_tool_idx:
                        if 0 <= _pi < len(messages) and messages[_pi].get("role") == "tool":
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
                    if weak_count == len(results) and weak_count > 0:
                        _w_delta = f"（{agent_def.name}）本轮工具结果均与任务弱相关，不再继续调用工具，直接基于已有信息组织回答。"
                        exec_reasoning.append(_w_delta)
                        yield {"type": "reasoning", "delta": _w_delta, "round": _rnd + 1, "phase": "plan"}
                        break
            # 兜底：多轮后仍无正文 → 流式生成（保持非空输出）
            if tool_injected and not llm_content:
                chunks = llm_client.chat(messages, provider_id=effective_provider, stream=True)
                for chunk in chunks:
                    for delta in self._extract_stream_deltas(chunk):
                        # V2.5：工具轮后的最终生成同样透传思考（原分支只取 content，丢思考内容）
                        if delta.get("reasoning"):
                            exec_reasoning.append(delta["reasoning"])
                            yield {"type": "reasoning", "delta": delta["reasoning"],
                                   "round": _rnd + 1, "phase": "llm"}
                        if delta.get("content"):
                            llm_content += delta["content"]
                            yield {"type": "token", "delta": delta["content"]}
            # llm_client.chat(stream=True) 返回生成器；FORCE_MOCK 下 MockLLM 也走 _stream
            if not tool_injected:
                chunks = llm_client.chat(messages, provider_id=effective_provider, stream=True)
                full = []
                for chunk in chunks:
                    for delta in self._extract_stream_deltas(chunk):
                        # 思考过程（reasoning_content）与正文（content）分流推送
                        if delta.get("reasoning"):
                            exec_reasoning.append(delta["reasoning"])
                            yield {"type": "reasoning", "delta": delta["reasoning"],
                                   "round": _rnd + 1, "phase": "llm"}
                        if delta.get("content"):
                            full.append(delta["content"])
                            yield {"type": "token", "delta": delta["content"]}
                llm_content = "".join(full)
            st = getattr(llm_client, "stats", {})
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
            # SysML v2 视图联动：LLM 输出含 SysML 代码 → 解析并投影各视图 ViewModel（会话内即时预览，不落库）
            sysml_views = self._gen_sysml_views(llm_content, intent, user_input)
            yield {"type": "stage", "name": "生成与校验", "status": "done"}
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
                self._record_skill_feedback(run_id=int(conversation_id or 0), intent=intent, output_content=llm_content)
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
                        "reasoning": "".join(exec_reasoning)[:4000] or None,
                        "agent": agent_def.name,
                        "tools": exec_tools,
                    },
                    "sysml_views": sysml_views,
                    "submitted_by": ((user or {}).get("display_name") or (user or {}).get("name") or "会话发起人")
                                     if isinstance(user, dict) else "会话发起人",
                    **self._build_rich_card(intent, retrieval, branch, user_input, provider_id),
                }, ensure_ascii=False)
                with db_conn() as conn:
                    # 2026-09-29：user 消息已在 execute_stream 入口落库，此处只插 assistant（防重复）
                    cursor = conn.execute(
                        "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
                        (conversation_id, "assistant", llm_content, self._get_card_type(intent), card_data)
                    )
                    msg_id = cursor.lastrowid
                    _archive_artifacts(conn, conversation_id, msg_id, card_data, llm_content, intent)
                    # CIA：变更影响分析记录（FR-CIA-3 每次分析结果快照落库，可追溯）
                    _archive_impact_analysis(conn, conversation_id, msg_id, card_data, user_input)
                    conn.execute(
                        "UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, intent=? WHERE id=?",
                        (intent, conversation_id)
                    )
                    conn.execute(
                        "INSERT INTO audit_logs (user_name, event_type, detail, result) VALUES (?,?,?,?)",
                        ("王工", "llm_chat", f"会话#{conversation_id} · 意图:{intent} · Agent:{agent_def.name} · 来源:{retrieval['source']}", "success")
                    )
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
                    "attachment_used": bool(retrieval.get("attachment_hits")),
                    "citations": _citations_payload(retrieval.get("chunk_hits")),  # 问答可解释性：[n] 引用来源
                },
                "attachments_info": {**att_parsed_info, "consumed": len(retrieval.get("attachment_hits") or [])},
                "matched_flows": matched_flows,
                "quality_report": quality_report,
            }}
        except GeneratorExit:
            # 2026-09-29（用户五轮反馈4）：客户端断开（停止按钮/关页/网络断）→ 已流出内容固化落库。
            #  此前直接 raise，浏览器里看得到的已生成内容一刷新就没了（库中 0 条）。
            #  去重：用户点停止时前端会先走 /messages/partial 固化 → 这里查最后一条消息，
            #  已是"部分固化"消息则跳过，避免双写。
            self._persist_partial_stream(conversation_id, llm_content or "".join(_orch_acc),
                                         "stop", dry_run=dry_run)
            raise
        except Exception as e:
            # 2026-09-29（用户五轮反馈4）：网络错误等异常 → 已生成内容同样固化（带中断说明后缀），
            #  刷新后"之前输出的内容"仍在，不再整体蒸发。
            self._persist_partial_stream(conversation_id, llm_content or "".join(_orch_acc),
                                         "error", dry_run=dry_run)
            yield {"type": "error", "message": str(e)}
