"""FlowPlannerMixin：编排规划（_summarize_plan/run_planner_plan/_planner_core/_run_task/_delegate_result/_score_bid）。"""
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from .tools import ToolExecutor


def build_subtask_context(tk: dict, goal: str, done_items: list = None) -> str:
    """T7 子任务上下文快照：任务定义 + 前置结果摘要 + 交付规范（隔离窗口最小充分上下文）。

    供 _run_task 与 _stream_orchestrated_flow 复用，保证会话编排/画布路径行为一致。
    仅注入与任务相关的上游已完成交付物摘要（各 ≤400 字），控制上下文增长。
    """
    cfg = tk.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg) or {}
        except Exception:
            cfg = {}
    parts = []
    parts.append(f"- 任务定义：key={tk.get('task_key')} / title={tk.get('title') or '-'}")
    exp = tk.get("expected_output") or cfg.get("expected_output") or ""
    if exp:
        parts.append(f"- 期望产出：{str(exp)[:200]}")
    deps = tk.get("deps") or []
    if isinstance(deps, str):
        try:
            deps = json.loads(deps) or []
        except Exception:
            deps = []
    dep_keys = {str(d) for d in deps}
    pre = [it for it in (done_items or []) if it.get("task_key") in dep_keys and it.get("result")]
    if pre:
        parts.append("- 上游交付物摘要：")
        for it in pre:
            parts.append(f"  · {it.get('task_key')}（{it.get('title') or '-'}）：{str(it.get('result'))[:400]}")
    parts.append("- 交付规范：输出需自包含；仅基于给定上下文，禁止虚构；风险/待确认项需显式标注")
    return "\n".join(parts)


class FlowPlannerMixin:
    """FlowExecutor 编排规划器拆分（解耦拆分：原 workflows.py 单体类方法）。"""

    @staticmethod
    def _summarize_item_budget(n_items: int) -> int:
        """汇总输入：**单项**字符预算（本轮总预算按子任务数均分，夹在 floor 与 item 上限之间）。

        2026-09-20 新增。此前 `_summarize_plan` 把每个子任务交付物硬编码截到 **500 字符**，
        而实测交付物 1,260 / 4,000 / 4,000 字符 → 丢弃率 60.3% / 87.5% / 87.5%，
        质量评审据此如实报「交付物不完整」（会话 368）。改为预算化后：
          ① 与配置解耦（`delegation.summary_*`）；② 子任务变多时不会"每个都看不清"；
          ③ `summary_item_max_chars <= 0` = 不限长（整份交付物进汇总，供大上下文模型）。
        """
        from core import config as _cfg
        try:
            item_max = int(_cfg.get("delegation", "summary_item_max_chars", 1600) or 1600)
            total = int(_cfg.get("delegation", "summary_total_chars", 12000) or 12000)
            floor = int(_cfg.get("delegation", "summary_floor_chars", 600) or 600)
        except Exception:
            item_max, total, floor = 1600, 12000, 600
        if item_max <= 0:
            return 0                      # 不限长
        if total <= 0:
            return item_max
        n = max(int(n_items or 1), 1)
        return max(floor, min(item_max, total // n))

    @staticmethod
    def _head_tail_clip(text: str, cap: int) -> str:
        """头尾采样：cap<=0 原样返回；超限保留**头 60% + 尾 40%**（中间标注已省略）。

        为什么不是"只留头"（`_truncate_budget(keep_head=True)` 的语义）：交付物的
        「结论 / 方案对比 / 风险与待确认」在**尾部**，只留头会把结论整段丢掉 —— 会话 368
        的门禁缺口正是这一形态（t2 可见文本止于「给出三个方案」，三个方案的内容与选择结论全丢）。
        """
        t = str(text or "")
        if cap <= 0 or len(t) <= cap:
            return t
        mark = "\n…（中间省略，已按头尾采样保留结论）…\n"
        head = int(cap * 0.6)
        tail = cap - head
        if tail <= 0:
            return t[:cap] + "\n…（超预算已裁剪）"
        return t[:head] + mark + t[-tail:]

    def _summarize_plan(self, done_items: list, plan: list, provider_id=None) -> str:
        """P1-1 计划总结节点：多个子任务交付物 → LLM 结构化整合；LLM 不可用/失败 → 拼接降级。"""
        if len(done_items) < 2:
            return ""
        _cap = self._summarize_item_budget(len(done_items))
        try:
            from llm import llm_client
            items_txt = "\n".join(
                f"[{it.get('task_key')}] {it.get('title')}（{it.get('agent_id') or '-'}）\n"
                + self._head_tail_clip(it.get('result') or '', _cap)
                for it in done_items)
            prompt = (
                "你是 MBSE 团队任务汇总专家。将以下多个已完成子任务的交付物整合成一份结构化最终报告："
                "1) 按逻辑顺序组织（需求→设计→分析→结论）；2) 只基于给定交付物整合，禁止补充虚构事实；"
                "3) 用标题分节；4) 交付物中的风险/待确认/缺失信息必须在报告对应小节显式标注，不得当作已确认事实；"
                "5) 末尾给出总体结论与后续建议。\n\n"
                "子任务交付物：\n" + items_txt)
            # 2026-09-17 S4：汇总环节加输出上限 —— 实测 plan_summary 平均 completion 7,470，
            # 是单任务里最贵的输出之一。上限取 3000（而非更激进的 1500）：本环节产出的是
            # **用户可见的最终报告**，而采纳判定很宽（下方 `len(content) > 80` 即采纳），
            # 截断后的短文本会被当成正常结果 —— 宁可少省一点也不要把报告砍半。
            # 3000 相比均值 7,470 仍省 60%+，需要更省时按 `refine/planner` 场景再下调。
            # 2026-09-20：改为可配置（`delegation.summary_max_tokens`，**默认仍 3000 = 不改行为**）。
            # 判据：若质量评审报「报告在结论前中断」，先升它；若报「交付物不完整」则是**输入**被切
            # （见 `_summarize_item_budget` / `_head_tail_clip`）——两者症状相似、修法不同，别混改。
            try:
                from core import config as _cfg2
                _mt = int(_cfg2.get("delegation", "summary_max_tokens", 3000) or 3000)
            except Exception:
                _mt = 3000
            resp = llm_client.chat([{"role": "user", "content": prompt}],
                                   provider_id=provider_id, max_tokens=_mt, _intent="plan_summary")
            content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
            if content and len(content) > 80:
                return content.strip()
        except Exception:
            pass
        return "\n\n".join(f"## {it.get('title')}\n{(it.get('result') or '').strip()}" for it in done_items)

    def run_planner_plan(self, goal: str = "", agents=None, max_tasks: int = 6, parallel: bool = True,
                         provider_id=None, run_id: int = 0, conn=None, attachments=None, pool=None) -> dict:
        """P0-1/P1-1/P1-2 Planner-Executor 服务（会话入口自动编排 / Flow planner 节点共用）。

        - LLM 计划生成：目标 → 子任务 DAG（key/title/agent/deps/task_type），
          注入已发布技能池（P1-2：任务可带 skill 字段指定技能）
        - Mock/解析失败 → 单 Agent 直跑降级（确定性保持）
        - agent_tasks DAG 执行（ready 并行 ≤3，失败 blocked 人工介入）
        - 多子任务 LLM 总结（P1-1），降级拼接
        - attachments：会话上传附件随编排透传（子任务执行时注入，避免丢附件）
        - pool（Task 5）：外部注入的编排候选池（如 pipeline._orch_pool 结果）；非空时覆盖 agents
          作为「可用 Agent 池」，向后兼容（不传 = 用 agents/内置旧池）。
        返回 {"content","plan","task_summary","degraded","data","llm","latency_ms"}。
        """
        from database import get_db
        own = conn is None
        conn = conn or get_db()
        try:
            return self._planner_core(goal, agents, max_tasks, parallel, provider_id, run_id, conn, attachments, pool)
        finally:
            if own:
                try:
                    conn.close()
                except Exception:
                    pass

    def _planner_core(self, goal: str, agents, max_tasks: int, parallel: bool,
                      provider_id, run_id: int, conn, attachments=None, pool=None) -> dict:
        """Planner-Executor 核心（run_planner_plan 的连接由外层管理）。

        pool（Task 5）：外部传入的编排候选池意图名列表（pipeline._orch_pool 结果），
        非空时覆盖 agents 作为「可用 Agent 池」；None/空 = 用 agents/内置旧池（向后兼容）。
        """
        from llm import llm_client
        from task_queue import TaskQueue
        from agent import AgentPipeline
        import time as _t
        t0 = _t.time()
        goal = goal or "执行该团队任务并汇总结果"
        if agents is None:
            agents = ["requirement_analysis", "design", "impact", "review", "report_generation", "knowledge_qa"]
        if isinstance(agents, str):
            agents = [x.strip() for x in agents.split(",") if x.strip()]
        # Task 5：外部注入的动态编排池优先（覆盖 agents 旧池），保证编排池与 pipeline 动态 discover 一致
        if pool:
            if isinstance(pool, str):
                pool = [x.strip() for x in pool.split(",") if x.strip()]
            if pool:
                agents = list(pool)
        pool_txt = "、".join(agents)
        max_tasks = max(1, min(int(max_tasks or 6), 12))

        # P1-2 技能池注入（已发布技能 + 依赖链）
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

        # 1) LLM 计划生成
        plan = []
        # 注意：JSON 示例花括号须转义（{{ }}），否则被下方 .format(n=...) 当作占位符 → KeyError
        prompt = (
            "你是任务规划器。把目标分解为可并行/串行执行的子任务清单，只输出 JSON：\n"
            '{{"tasks": [{{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
            ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
            ' "context": "子任务必需上下文(精简事实，不复制用户全文，可空)",'
            ' "expected_output": "完成标准/期望输出格式(可空)",'
            ' "tools": ["允许的工具白名单(可空，空=继承该 Agent 默认绑定)"],'
            ' "skills": ["指定技能(可空)"]}}]}}\n'
            f"可用 Agent 池：{pool_txt}\n"
            "task_type 可选 agent（走 Agent 完整管线）或 react（多步思考-工具求解）或 llm（纯生成）。\n"
            f"{skill_pool_txt}"
            "要求：任务数 1~{n} 个；每个任务只交付一个明确成果；有依赖关系的用 deps 表达；"
            "context 只写该任务必需的事实与输入引用（上下文隔离，避免膨胀）；不要输出其他文字。\n"
            f"目标：{goal}"
        ).format(n=max_tasks)
        try:
            resp = llm_client.chat([{"role": "user", "content": prompt}], provider_id=provider_id, _intent="planner")
            raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
            data = AgentPipeline._parse_json_block(raw) if raw else None
            # P0：委派协议字段（context/expected_output/tools/skills）收进 config，随任务落库透传
            plan = []
            for _task in ((data or {}).get("tasks") or []):
                if not isinstance(_task, dict) or not _task.get("key"):
                    continue
                _cfg = dict(_task.get("config") or {})
                for _k in ("context", "expected_output", "tools", "skills"):
                    if _task.get(_k) not in (None, "", [], {}):
                        _cfg[_k] = _task[_k]
                _task["config"] = _cfg
                plan.append(_task)
        except Exception:
            plan = []

        # 2) 降级：计划解析失败 → 单 Agent 直跑
        if not plan:
            pipe = AgentPipeline()
            pipe._load_db_agents()
            direct = agents[0]
            try:
                out = pipe.execute(goal, 0, "dev", provider_id, [], dry_run=True, forced_intent=direct)
                content = (out.get("content") or "").strip()
                llm = out.get("llm") or {}
            except Exception as e:
                content = f"执行失败: {str(e)[:150]}"
                llm = {}
            return {"content": content, "plan": [], "task_summary": {"counts": {}, "total": 0, "items": []},
                    "degraded": True, "data": {"degraded": True, "direct_agent": direct, "warnings": []},
                    "llm": llm, "latency_ms": int((_t.time() - t0) * 1000)}

        # 3) 计划后技能校验（P1-2）
        warnings = []
        for t in plan:
            sk = t.get("skill") or (t.get("config") or {}).get("skill")
            if sk:
                row = conn.execute("SELECT status FROM skills WHERE name=?", (sk,)).fetchone()
                if not row:
                    warnings.append(f"技能 {sk} 不存在（任务 {t.get('key')}）")
                elif row["status"] != "published":
                    warnings.append(f"技能 {sk} 未发布（任务 {t.get('key')}）")

        # 4) DAG 执行（agent_tasks 队列）
        run_id = int(run_id or 0)
        executed = []
        _done_items = []   # T7：已完成子任务结果（供依赖任务注入上下文快照）
        try:
            TaskQueue.clear_run(conn, run_id)
            TaskQueue.create_plan(conn, run_id, plan, assigned_by="session")
            guard = 0
            while TaskQueue.pending_count(conn, run_id) > 0 and guard < 200:
                guard += 1
                ready = TaskQueue.ready_tasks(conn, run_id)
                if not ready:
                    for tk in TaskQueue.tasks(conn, run_id):
                        if tk["status"] in ("planned", "ready"):
                            TaskQueue.block(conn, tk["id"], "依赖任务失败，无法推进")
                    break
                if parallel and len(ready) > 1:
                    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTTimeout
                    from core import config as _cfg
                    _wt = int(_cfg.get("delegation", "worker_timeout_s", 120))
                    with ThreadPoolExecutor(max_workers=min(len(ready), 3)) as pool:
                        # T7：注入已完成的依赖结果摘要（跨线程只读共享 _done_items 快照）
                        _futs = [pool.submit(self._run_task, tk, goal, conn, attachments, list(_done_items))
                                 for tk in ready]
                        results_list = []
                        for _f in _futs:
                            try:
                                results_list.append(_f.result(timeout=_wt))
                            except _FTTimeout:
                                results_list.append(("", {}, False, f"子任务执行超时(>{_wt}s)"))
                            except Exception as _e:
                                results_list.append(("", {}, False, f"{type(_e).__name__}: {str(_e)[:120]}"))
                else:
                    results_list = [self._run_task(tk, goal, conn, attachments, list(_done_items)) for tk in ready]
                for tk, rr in zip(ready, results_list):
                    tk_res, meta, ok, err = rr
                    if ok:
                        TaskQueue.complete(conn, tk["id"], tk_res, meta,
                                           latency_ms=int(meta.get("latency_ms", 0)))
                        _done_items.append({"task_key": tk["task_key"], "title": tk["title"],
                                            "result": tk_res, "status": "done"})
                    else:
                        TaskQueue.fail(conn, tk["id"], err)
                    TaskQueue.release_deps(conn, run_id, tk["task_key"])
                    executed.append({"key": tk["task_key"], "title": tk["title"],
                                     "agent": tk["agent_id"], "ok": ok})
        except Exception as e:
            executed.append({"error": str(e)[:150]})

        # 5) 汇总 + P1-1 总结节点
        summ = TaskQueue.summary(conn, run_id)
        done_items = [it for it in summ["items"] if it.get("status") == "done"]
        fail_items = [it for it in summ["items"] if it.get("status") in ("failed", "blocked")]
        if len(done_items) >= 2:
            content = self._summarize_plan(done_items, plan, provider_id)
        else:
            content = "\n\n".join(f"## {it.get('title')}\n{(it.get('result') or '').strip()}" for it in done_items) \
                or "（计划已执行，但无成功交付物）"
        # T4：反思闭环——汇总后质量评审 → 未达标修订（与流式编排行为一致，复用 RefineGate）
        reflection_meta = None
        if done_items:
            from workflows.refine import RefineGate
            _ref = RefineGate.run(content, goal, done_items, plan, provider_id)
            reflection_meta = {
                "enabled": True, "rounds": _ref.get("rounds", 0), "score": _ref.get("score"),
                "passed": bool(_ref.get("passed", True)), "issues": _ref.get("issues", []),
                "advice": _ref.get("advice", ""),
                "provider": (_ref.get("llm") or {}).get("provider", "-"),
                "degraded": bool(_ref.get("degraded", False)),
            }
            content = _ref.get("content") or content
        # 2026-09-20：与**流式**编排对齐——汇总/反思都可能丢掉子任务交付的 V2 代码，而
        # `_finish_orchestrated` 只投影视图、不做补回（此前非流式路径因此可能整体交不出模型）。
        # 放在反思之后：反思若改写了正文，这里再把代码补回。
        try:
            content = AgentPipeline._ensure_sysml_blocks(content, done_items)
        except Exception:
            pass
        prefix = (f"Planner 计划 {summ['total']} 个子任务，完成 {len(done_items)}，失败/阻塞 {len(fail_items)}。\n"
                  + (("警告：" + "；".join(warnings) + "\n") if warnings else ""))
        return {
            "content": content, "plan": plan, "task_summary": summ, "degraded": False,
            "data": {"plan": plan[:max_tasks], "task_total": summ["total"], "task_done": len(done_items),
                     "task_failed": len(fail_items), "warnings": warnings,
                     "reflection": reflection_meta,
                     "tasks": [{"key": it.get("task_key"), "title": it.get("title"),
                                "agent": it.get("agent_id"), "status": it.get("status")} for it in summ["items"]]},
            "llm": {}, "latency_ms": int((_t.time() - t0) * 1000),
        }

    def _run_task(self, tk: dict, goal: str, conn, attachments=None, done_items: list = None) -> tuple:
        """执行单个计划任务（agent | react | llm | tool），返回 (result, metadata, ok, error)。

        P0 委派协议：config 支持 context/expected_output/tools/skills——
        - context 注入子任务 prompt（上下文隔离，只传必需事实）
        - expected_output 提示完成标准
        - tools 白名单 → 子 Agent 最小权限（Worker ⊆ Supervisor）
        metadata 为结构化 DelegateResult（status/conclusion/evidence/risks/missing_info/next_steps），
        供汇总与下游按 status 判断成功/失败。
        注意：agent_tasks.config 为 TEXT 列（JSON 字符串），此处统一解析为 dict。
        attachments：会话上传附件透传（子任务执行时注入 prompt，避免丢附件）。
        """
        t0 = time.time()
        try:
            ttype = tk.get("task_type", "agent")
            cfg = tk.get("config") or {}
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg) or {}
                except Exception:
                    cfg = {}
            # P0/P1b-2：委派协议五字段（task 标题 + context 必需事实 + expected_output 完成标准）
            # context/expected_output 来源：优先 agent_tasks 独立列（planner 落库），cfg 兜底兼容旧计划
            title = tk.get("title", "")
            context = str(tk.get("context") or cfg.get("context") or "").strip()
            expected_output = str(tk.get("expected_output") or cfg.get("expected_output") or "").strip()
            tools_wl = cfg.get("tools") or None
            if isinstance(tools_wl, str):
                tools_wl = [x.strip() for x in tools_wl.split(",") if x.strip()]
            query = cfg.get("query") or f"{title}\n团队目标：{goal}"
            # T7：任务上下文快照（任务定义 + 前置依赖结果摘要 + 交付规范），注入隔离窗口
            _ctx_block = build_subtask_context(tk, goal, done_items)
            if _ctx_block:
                query = f"[任务上下文快照]\n{_ctx_block}\n\n[任务]\n{query}"
            if context:
                query += f"\n\n任务上下文（只读，勿编造上下文外事实）：\n{context[:2000]}"
            if expected_output:
                query += f"\n\n期望输出/完成标准：\n{expected_output[:1000]}"
            if ttype == "agent":
                from agent import AgentPipeline
                pipe = AgentPipeline()
                pipe._load_db_agents()
                # P1-2：任务 config.skill 指定技能 → 强制注入（仅当技能存在且已发布）
                skill_name = cfg.get("skill") or None
                if skill_name:
                    row = conn.execute("SELECT name FROM skills WHERE name=? AND status='published'", (skill_name,)).fetchone()
                    if not row:
                        skill_name = None
                out = pipe.execute(query, 0, "dev", None, attachments or [], dry_run=True,
                                   forced_intent=tk.get("agent_id") or None, skill_name=skill_name,
                                   tools_whitelist=tools_wl)
                content = (out.get("content") or "").strip()
                llm = out.get("llm") or {}
                meta = self._delegate_result(content, status="done", provider=llm.get("provider", "-"),
                                             used_mock=llm.get("used_mock", True),
                                             latency_ms=int((time.time() - t0) * 1000))
                return content, meta, True, ""
            if ttype == "react":
                out = self._exec_react(tk.get("task_key", "t"), cfg, {}, {}, conn, None)
                content = (out.get("content") or "").strip()
                meta = self._delegate_result(content, status="done",
                                             latency_ms=out.get("latency_ms", int((time.time() - t0) * 1000)))
                return content, meta, True, ""
            if ttype == "llm":
                from llm import llm_client
                resp = llm_client.chat([{"role": "user", "content": query}],
                                       provider_id=cfg.get("provider_id"), _intent="task_llm")
                content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
                meta = self._delegate_result(content, status="done", latency_ms=int((time.time() - t0) * 1000))
                return content, meta, True, ""
            if ttype == "tool":
                executor = ToolExecutor(conn)
                r = executor.execute(cfg.get("tool", "graph_retrieve"), cfg.get("arguments") or {},
                                     cfg.get("branch", "dev"))
                content = str(r.get("result", ""))
                ok = bool(r.get("ok"))
                meta = self._delegate_result(content, status="done" if ok else "failed")
                return content, meta, ok, "" if ok else content
            return "", {}, True, f"未知任务类型 {ttype}"
        except Exception as e:
            return "", {}, False, f"{type(e).__name__}: {str(e)[:150]}"

    @staticmethod
    def _delegate_result(content: str, status: str = "done", provider: str = "-",
                         used_mock: bool = True, latency_ms: int = 0) -> dict:
        """P0-2 结构化委派结果（DelegateResult）：status/conclusion/evidence/risks/missing_info/next_steps。

        规则兜底：结论取交付物文本；risks 从内容中粗提取风险段落关键词所在行。
        上游总结/评审据此判断成功、失败、缺失，不再把猜测当事实。
        """
        content = (content or "").strip()
        risks = []
        for line in content.splitlines()[:60]:
            if any(kw in line for kw in ("风险", "注意", "警告", "⚠", "不确定", "待确认")):
                risks.append(line.strip()[:200])
        return {
            "status": status,
            "conclusion": content[:2000],
            "evidence": [],
            "risks": risks[:5],
            "missing_info": [],
            "next_steps": [],
            "summary": content[:300],
            "provider": provider,
            "used_mock": used_mock,
            "latency_ms": latency_ms,
        }

    @staticmethod
    def _score_bid(content: str, worker: str) -> int:
        """启发式投标评分（Mock/无 LLM 环境可用；内容长度 + 领域关键词）。"""
        if not content:
            return 0
        s = len(content)
        for kw in ("方案", "设计", "考虑", "分析", "需求", "约束", "接口", "实现", "建议", "风险"):
            if kw in content:
                s += 10
        return s

    # ── P3：reflection 反思节点（质量评估，配合循环边实现"行动→评估→改进"闭环）──
