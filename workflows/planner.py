"""FlowPlannerMixin：编排规划（_summarize_plan/run_planner_plan/_planner_core/_run_task/_delegate_result/_score_bid）。"""
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from .tools import ToolExecutor

# ★ 2026-11-04：计划**语义**校验（阶段序/完备性/覆盖性/路由声明）。
#   与 `TaskQueue.validate_plan`（结构层：环/重复 key）**正交**，两层都要有：
#   结构层答"能不能跑"，本层答"跑得对不对"。详见 core/plan_guard.py 模块头。
from core.plan_guard import (
    load_pipeline_config,
    mentioned_from_stage_hint,
    render_violations,
    validate_pipeline_plan,
)


def _dep_closure(dep_keys: set, plan: list, _guard: int = 32) -> set:
    """把直接依赖展开成**传递闭包**（含自身）。`_guard` 防环形 deps 死循环。

    plan 元素形如 {"task_key": "t2", "deps": ["t1"]}；缺失/非法 deps 一律忽略。
    """
    by_key = {}
    for t in (plan or []):
        if not isinstance(t, dict):
            continue
        k = str(t.get("task_key") or "")
        if not k:
            continue
        ds = t.get("deps") or []
        if isinstance(ds, str):
            try:
                ds = json.loads(ds) or []
            except Exception:
                ds = []
        by_key[k] = [str(d) for d in ds if d]
    out = set(dep_keys)
    frontier = set(dep_keys)
    for _ in range(max(1, _guard)):
        nxt = set()
        for k in frontier:
            for d in by_key.get(k, []):
                if d not in out:
                    out.add(d)
                    nxt.add(d)
        if not nxt:
            break
        frontier = nxt
    return out


def _upstream_digest_cap() -> int:
    """上游交付物摘要的字符预算（`delegation.upstream_digest_chars`，默认 900）。

    ★ 2026-11-02：原实现硬编码 `[:400]` —— 实测两个问题：
      ① 切在 ```sysml 代码块中间 ⇒ 下游拿到半份代码 + 半份正文，两半都不可用；
      ② 切在「## 结论」之前 ⇒ 只留过程、丢掉结论，下游据此把上游判定为"缺失"。
    为什么是 900 而不是更大：Anthropic 的实证是子 Agent 只回 1~2k token 摘要；
    摘要的作用是"让下游判断要不要取全文"，不是"把上游正文搬过来"。
    需要全文时走 offload + `tool_result_fetch`（调用点已给引用行）。
    """
    try:
        from core import config as _cfg
        v = int(_cfg.get("delegation", "upstream_digest_chars", 900) or 900)
        return max(200, min(4000, v))
    except Exception:
        return 900


def _protocol_digest(sm) -> str:
    """把子任务的**协议摘要**压成几行「结论级」信息（无协议返回 ""）。

    行业依据：OpenAI Agents SDK / Anthropic 的 handoff 都强调「传结论，不传推理」。
    本仓的协议里恰好已有 struct 化的结论槽（claims[].kind + subject），
    比让下游从正文里自己找结论更可靠 —— 且 subject 已被用于跨子任务冲突检测。
    """
    if not isinstance(sm, dict) or not sm:
        return ""
    parts = []
    st = sm.get("status")
    one = str(sm.get("summary") or "").strip()
    if st or one:
        parts.append("  状态=%s%s" % (st or "?", ("；成果：" + one[:200]) if one else ""))
    cl = sm.get("claims") or []
    if cl:
        _K = {"fact": "已核实", "inference": "推断", "to_verify": "待验证"}
        try:
            _c = sorted(cl, key=lambda c: {"fact": 0, "inference": 1,
                                           "to_verify": 2}.get(str((c or {}).get("kind")),
                                                               "to_verify"))
        except Exception:
            _c = cl
        parts.append("  关键结论：" + "；".join(
            "【%s】%s%s" % (_K.get(str((x or {}).get("kind") or "to_verify"), "待验证"),
                           (str(x.get("subject")) + "：") if (x or {}).get("subject") else "",
                           str((x or {}).get("text") or "")[:120])
            for x in _c[:4] if isinstance(x, dict)))
    rk = sm.get("risks") or []
    if rk:
        parts.append("  风险：" + "；".join(
            "[%s]%s" % ((r or {}).get("level") or "?", str((r or {}).get("title") or "")[:60])
            for r in rk[:3] if isinstance(r, dict)))
    ar = sm.get("artifacts") or []
    if ar:
        parts.append("  产物：" + "；".join(
            "%s《%s》" % ((a or {}).get("kind") or "-", str((a or {}).get("title") or "")[:40])
            for a in ar[:4] if isinstance(a, dict)))
    ms = sm.get("missing") or []
    if ms:
        parts.append("  自报缺口：" + "；".join(str(x)[:50] for x in ms[:4]))
    return "\n".join(parts)


def build_subtask_context(tk: dict, goal: str, done_items: list = None,
                          artifact_digest: str = "",
                          offload_map: dict = None, plan: list = None,
                          digest_cap: int | None = None) -> str:
    """T7 子任务上下文快照：任务定义 + 前置结果摘要 + 会话既有产物 + 交付规范（最小充分上下文）。

    供 _run_task 与 _stream_orchestrated_flow 复用，保证会话编排/画布路径行为一致。
    仅注入与任务相关的上游已完成交付物摘要（各 ≤400 字），控制上下文增长。

    artifact_digest（P0-6，2026-09-30）：会话既有产物摘要（agent/session_artifacts.build_digest）。
    为什么必须在这里下发：子任务走的是 `execute_stream(query, 0, ...)` —— **conversation_id=0**，
    `_load_history` 对子任务等于空。所以多轮产物只能由此显式注入，否则子任务只看得见"本轮目标"、
    看不见"会话已经有什么"（实测会话 514：第 2 轮子任务把上一轮建好的需求模型当成不存在）。
    空串 = 不加段落（与改动前逐字节一致）。

    plan（2026-10-29）：全量计划（含各任务 deps）。给了就把依赖展开成**传递闭包**。
    为什么必须展开：旧实现 `dep_keys = {deps 里的直接依赖}`，于是
    t1(需求) → t2(设计) → t3(报告) 这条链上，**t3 只看得见 t2、完全看不见 t1**
    （实测：t3 快照里 grep 不到 t1 的结论）。多阶段任务的末段恰恰最需要开头的需求约束 ——
    「需求分析、方案设计、代码校验」这类多意图输入必然产生长链，全链断头等于把多意图做废。
    plan=None ⇒ 退化为直接依赖（逐字节兼容旧行为）。
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
    # ★ 2026-10-29：依赖传递闭包（只对给了 plan 的调用方生效）。
    if plan:
        dep_keys = _dep_closure(dep_keys, plan)
    # 按计划顺序输出（稳定：避免同一任务两次执行拿到不同顺序的摘要）
    pre = [it for it in (done_items or []) if it.get("task_key") in dep_keys and it.get("result")]
    if pre:
        parts.append("- 上游交付物摘要：")
        _cap = int(digest_cap or _upstream_digest_cap())
        try:
            from agent.context_compress import smart_excerpt as _sx
        except Exception:
            _sx = None
        for it in pre:
            _key = it.get('task_key')
            _title = it.get('title') or '-'
            # ★ 2026-11-02（改进点②）：**协议结论优先**，正文摘要用围栏安全截断。
            #   为什么先给协议：claims/risks/artifacts 是"结论级"结构，
            #   正是下游判断"要不要取全文、能不能直接用"所需的最少信息；
            #   而正文截断只能给到"过程"，还容易切在代码块中间。
            _pd = _protocol_digest(it.get("summary"))
            _body = str(it.get('result') or '')
            if _sx is not None:
                _snippet, _om = _sx(_body, _cap)
                if _om:
                    _snippet += "\n  （正文已省略 %d 字符%s）" % (
                        _om, "，完整内容见下方 offload 引用" if offload_map else "")
            else:
                _snippet = _body[:_cap] + ("…" if len(_body) > _cap else "")
            parts.append(f"  · {_key}（{_title}）：{_snippet}")
            if _pd:
                parts.append(_pd)
        # ★ 2026-10-20（共享产物工作区·方案1）：摘要只有 400 字，下游曾因只看到片段
        #   把上游交付物误判为"缺失/失败"（会话 9565 t1—t5 全部误报）。完整交付物
        #   已由 worker 落 offload 共享区 —— 这里给**按需取全文的引用**（id + 字符数 +
        #   工具调用指引），下游自行决定要不要读全文。offload_map={task_key: (id, len)}。
        _om = offload_map or {}
        for it in pre:
            _oid = _om.get(str(it.get("task_key")))
            if _oid:
                _oid_v, _oid_len = _oid if isinstance(_oid, tuple) else (_oid, 0)
                if _oid_v:
                    parts.append(
                        f"  · 引用：{it.get('task_key')} 完整交付物（约 {_oid_len} 字符）"
                        f"已存档为 offload #{_oid_v} —— 摘要不够判断时，"
                        f"调用工具 tool_result_fetch（参数 {{\"id\": {_oid_v}}}）取全文，"
                        f"不要凭摘要猜测上游结论。")
    parts.append("- 交付规范：输出需自包含；仅基于给定上下文，禁止虚构；风险/待确认项需显式标注")
    if artifact_digest:
        parts.append(str(artifact_digest))
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

        ★ 2026-11-04：切口**按围栏边界对齐** —— 绝不把 ``` 围栏从中间切开。
          为什么：切口落在围栏内部时，裁出的头/尾两段各自带着半个围栏，拼回去 ``` 数量变奇数
          ⇒ 下游 markdown 渲染把后续正文**全吞进代码块**。真库实证 conv 990408 / msg 5761：
          21432 字正文里围栏 13 个（**奇数**），最后一段 ```sysml 没闭合，
          尾部的「连接已中断」提示直接落进了代码块内部。

        ⚠️ **本函数必须零外部依赖**（只用内置字符串操作）：
          `tools/verify/verify_subtask_isolation.py` 的变异自证会把本函数源码
          `exec` 到一个**空命名空间**里执行（那里没有 `re`、没有 `FlowPlannerMixin`）——
          用到模块级名字就会 NameError，门禁自身先崩。
          ⇒ 围栏扫描用 `str.find` 而非 `re.finditer`；对齐逻辑内联，不抽成独立方法。
        """
        t = str(text or "")
        if cap <= 0 or len(t) <= cap:
            return t
        mark = "\n…（中间省略，已按头尾采样保留结论）…\n"
        head = int(cap * 0.6)
        tail = cap - head
        if tail <= 0:
            return t[:cap] + "\n…（超预算已裁剪）"
        # 围栏起点列表（成对：fences[0]-fences[1] 是一个代码块）
        fences, _pos = [], 0
        while True:
            _i = t.find("```", _pos)
            if _i < 0:
                break
            fences.append(_i)
            _pos = _i + 3
        spans = [(fences[k], fences[k + 1]) for k in range(0, len(fences) - 1, 2)]
        # ⚠️ 2026-11-04 实测补丁：**悬空的尾部开围栏**（围栏数为奇数）不在任何配对 span 里。
        #   真库 msg 5761 正是 13 个（奇数）—— 末段 ```sysml 未闭合 ⇒ 若只按配对 span 对齐，
        #   tail 切口会正好落进这段悬空围栏，裁出来仍然不配对（端到端实测抓到的真缺口）。
        #   ⇒ 把悬空开围栏当作"延伸到文末"的 span 一并纳入对齐。
        if len(fences) % 2 == 1:
            spans.append((fences[-1], len(t)))

        def _align(cut, toward_head):
            """把切口移到围栏边界（不切开围栏）。
            toward_head=True  → 回退到围栏开头（用于 head 段末尾）
            toward_head=False → 前移到围栏结尾（用于 tail 段开头）
            ⚠️ 若外扩会让该段变空（围栏紧贴文本端），则**整体保留该围栏** ——
               宁多留不可少留（少留会把结论整段丢掉，I2 门禁守的就是这条）。"""
            for _s, _e in spans:
                if _s < cut < _e:
                    if toward_head:
                        return _s if _s > 0 else _e
                    return _e if _e < len(t) else _s
            return cut

        h_end = _align(head, True)
        t_start = _align(len(t) - tail, False)
        if h_end >= t_start:            # 对齐后两段交叉 ⇒ 退回原切口（不制造新的破坏）
            h_end, t_start = head, len(t) - tail
        out = t[:h_end] + mark + t[t_start:]
        # 最后一道保险：输出围栏数必须为偶数（悬空开围栏在这里被显式闭合）。
        # 只在确为奇数时动手 —— 正常文本一字不加。
        if out.count("```") % 2 == 1:
            out += "\n```\n"
        return out

    def _summarize_plan(self, done_items: list, plan: list, provider_id=None) -> str:
        """P1-1 计划总结节点：多个子任务交付物 → LLM 结构化整合；LLM 不可用/失败 → 拼接降级。

        ★ 2026-11-02（米爸「最终正文只承载最后确定的代码」）：两条出口（LLM 整合 / 拼接降级）
        都过一遍 `dedupe_interim_code_blocks` —— 各子任务交付物**各自带一份完整模型代码**，
        原样进最终正文就是「同一模型的多份版本代码块叠在一起」（真库实证 conv 9999/msg 5689：
        3 份 ```sysml，32308 字）。中间版本在子任务执行卡本就可见，正文收敛为最后确定版。
        """
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
                "5) 末尾给出总体结论与后续建议。"
                "6) 若多个交付物包含**同一模型/同一数据的多个版本代码或视图**，最终报告只保留"
                "最后一个确定版本的完整代码；早期版本用一句话说明演进关系即可，**不要重复粘贴**"
                "（各版本已在执行过程卡的实时输出中可见）。\n\n"
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
                # ★ 2026-11-02：LLM 整合也可能把各版本的代码原样带出来（prompt 只是软约束）⇒ 出口统一收敛
                from agent.final_content import (dedupe_interim_code_blocks as _dedupe,
                                                 normalize_ordinal_headings as _ordfix)
                content, _n = _dedupe(content)
                # ★ 2026-11-04：整合退化成拼接时，各份报告会各自从「一、」重开
                #   （真库 msg 5761：`## 一、结论先行` 出现 2 次、`二` 排在 `六` 之后）
                #   ⇒ 只重排**序号**，不删任何标题文字（判据见函数文档）。
                content, _no = _ordfix(content)
                return content.strip()
        except Exception:
            pass
        # 拼接降级出口：各交付物的完整代码必然全量带入 ⇒ 必须收敛（同上）
        try:
            from agent.final_content import (dedupe_interim_code_blocks as _dedupe,
                                             normalize_ordinal_headings as _ordfix)
            _joined = "\n\n".join(f"## {it.get('title')}\n{(it.get('result') or '').strip()}" for it in done_items)
            _joined, _n = _dedupe(_joined)
            # 拼接降级**必然**产生序号回退（每份交付物自带「一、二、三」）⇒ 这里最需要重排
            _joined, _no = _ordfix(_joined)
            return _joined
        except Exception:
            return "\n\n".join(f"## {it.get('title')}\n{(it.get('result') or '').strip()}" for it in done_items)

    def run_planner_plan(self, goal: str = "", agents=None, max_tasks: int = 6, parallel: bool = True,
                         provider_id=None, run_id: int = 0, conn=None, attachments=None, pool=None,
                         conversation_id: int = 0, team_prompt: str = "",
                         branch: str = "dev", user: dict | None = None,
                         stage_hint: list | None = None) -> dict:
        """P0-1/P1-1/P1-2 Planner-Executor 服务（会话入口自动编排 / Flow planner 节点共用）。

        - LLM 计划生成：目标 → 子任务 DAG（key/title/agent/deps/task_type），
          注入已发布技能池（P1-2：任务可带 skill 字段指定技能）
        - Mock/解析失败 → 单 Agent 直跑降级（确定性保持）
        - agent_tasks DAG 执行（ready 并行 ≤3，失败 blocked 人工介入）
        - 多子任务 LLM 总结（P1-1），降级拼接
        - attachments：会话上传附件随编排透传（子任务执行时注入，避免丢附件）
        - pool（Task 5）：外部注入的编排候选池（如 pipeline._orch_pool 结果）；非空时覆盖 agents
          作为「可用 Agent 池」，向后兼容（不传 = 用 agents/内置旧池）。
        - team_prompt（C2，2026-10-07）：团队模式注入的「主 Agent 拆解规则 + 成员名册」。
          此前 pool 只给意图名，planner 分不清「结构视图生成」与「需求视图生成」，
          也不知道 SysML v2 八视图依赖顺序 ⇒ 拆解质量不可控（同一 query 可能两次不同）。
          为空 = 不注入（向后兼容，非团队模式行为完全不变）。
        返回 {"content","plan","task_summary","degraded","data","llm","latency_ms"}。
        """
        from database import get_db
        own = conn is None
        conn = conn or get_db()
        try:
            return self._planner_core(goal, agents, max_tasks, parallel, provider_id, run_id, conn,
                                      attachments, pool, conversation_id, team_prompt,
                                      branch=branch, user=user, stage_hint=stage_hint)
        finally:
            if own:
                try:
                    conn.close()
                except Exception:
                    pass

    def _planner_core(self, goal: str, agents, max_tasks: int, parallel: bool,
                      provider_id, run_id: int, conn, attachments=None, pool=None,
                      conversation_id: int = 0, team_prompt: str = "",
                      branch: str = "dev", user: dict | None = None,
                      stage_hint: list | None = None) -> dict:
        """Planner-Executor 核心（run_planner_plan 的连接由外层管理）。

        pool（Task 5）：外部传入的编排候选池意图名列表（pipeline._orch_pool 结果），
        非空时覆盖 agents 作为「可用 Agent 池」；None/空 = 用 agents/内置旧池（向后兼容）。
        team_prompt（C2）：团队模式注入的拆解规则与成员名册，追加在计划 prompt 末尾。
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

        # P0-6（2026-09-30）：会话既有产物摘要（只读；会话由调用方显式给 conversation_id）。
        # 注意不要用 run_id 顶替 —— run_id 只是"本轮编排批次号"，会话入口传 0（见 _try_orchestrate）。
        artifact_digest = ""
        if conversation_id:
            try:
                from agent.session_artifacts import build_digest
                artifact_digest = build_digest(conn, conversation_id)
            except Exception:
                artifact_digest = ""

        # 1) LLM 计划生成
        plan = []
        # 注意：JSON 示例花括号须转义（{{ }}），否则被下方 .format(n=...) 当作占位符 → KeyError
        # ★ P4 replanning（2026-10-21）：每个任务标注 `config.required` —— 该任务
        #   产出是否为**达成目标所必需**。这是 early_stop（提前收敛）的唯一判据来源：
        #   required 全 done 且零 failed ⇒ 可放弃剩余非必需任务提前收尾。
        #   ⚠️ 不标注就没有判据 —— early_stop 对无标注图恒不动作（保守：不猜目标边界）。
        prompt = (
            "你是任务规划器。把目标分解为可并行/串行执行的子任务清单，只输出 JSON：\n"
            '{{"tasks": [{{"key": "t1", "title": "子任务描述", "agent": "Agent 名",'
            ' "deps": ["前置任务key列表，无则[]"], "task_type": "agent",'
            ' "context": "子任务必需上下文(精简事实，不复制用户全文，可空)",'
            ' "expected_output": "完成标准/期望输出格式(可空)",'
            ' "config": {{"required": true}},'
            ' "tools": ["允许的工具白名单(可空，空=继承该 Agent 默认绑定)"],'
            ' "skills": ["指定技能(可空)"]}}]}}\n'
            f"可用 Agent 池：{pool_txt}\n"
            "task_type 可选 agent（走 Agent 完整管线）或 react（多步思考-工具求解）或 llm（纯生成）。\n"
            f"{skill_pool_txt}"
            "要求：任务数 1~{n} 个；每个任务只交付一个明确成果；有依赖关系的用 deps 表达；"
            "context 只写该任务必需的事实与输入引用（上下文隔离，避免膨胀）；"
            "config.required 标注该任务产出是否为达成目标所必需（true=必需，false=可选/增强）；"
            "不要输出其他文字。\n"
            f"目标：{goal}"
        ).format(n=max_tasks)
        # P0-6：摘要追加在 `.format()` **之后** —— 摘要里可能含 { }（元素名/JSON 片段），
        # 放进被 format 的模板会 KeyError。无产物 → 不加段落。
        if artifact_digest:
            prompt += (
                "\n" + artifact_digest + "\n"
                "若「本会话既有产物」非空且与本目标相关：任务必须**在既有产物上做增量** —— "
                "title 写明增量的对象（如「在既有需求模型 v0.1 上补充参与者」），"
                "context 写清引用哪个既有版本/元素；禁止规划「从零分析/重新建立」类任务。\n")
        # ★ 2026-10-29：多意图阶段序约束。**与流式路径同口径、同样放在 `.format()` 之后**
        #   （阶段原句可能含 { }，放进被 format 的模板会 KeyError）。
        #   此前只有流式路径注入 stage_hint，非流式（Flow 节点/试跑）没有 ⇒ 同一句
        #   「需求分析、方案设计、代码校验」流式会按 3 阶段排 deps、非流式不会。
        #   stage_hint 为空 ⇒ 不加段落（逐字节回到旧行为）。
        if stage_hint:
            prompt += (
                "\n★ 用户明确要求按以下**阶段顺序**执行，请让 plan 的任务顺序与 deps 与该序列对齐"
                "（同一阶段的任务可并行；某阶段在本目标下确实无意义可跳过，但不得乱序）："
                + " → ".join(str(x) for x in stage_hint))
        # C2（2026-10-07）：团队定义注入。**放在 `.format()` 之后** —— 名册里含中文括号与
        # 可能的 { }，放进被 format 的模板会 KeyError（与 artifact_digest 同理）。
        if team_prompt:
            prompt += "\n" + team_prompt + "\n"
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

        # 3b) 计划**语义**校验（2026-11-04）：阶段序 / 完备性 / 覆盖性 / 路由声明。
        #
        #     ⚠️ 与 `TaskQueue.validate_plan`（结构层：空 key/重复 key/自环/环）**不重叠**。
        #        结构层在 `create_plan` 内跑并抛异常；本层只做语义，且**默认不阻断**。
        #
        #     为什么必须有这一层：`agents.id=160` 的 prompt 写着「阶段顺序固定…不得跳步、
        #     不得倒序」，但全仓没有一处校验 —— 模型真倒序了只会按它给的 deps 执行。
        #     与「名册漂移导致派给零工具 Agent 却照标 done」是同一类病：**约束只在提示词里**。
        #
        #     默认 mode=warn（读 `generate_rules.pipeline_guard_mode`）⇒ 只让违规**可见**；
        #     要收紧成拦截，改那条规则为 `block` 即可，**不必改代码**。
        _plan_guard = None
        _pg_blocked = False
        try:
            from task_queue import resolve_agent_name as _resolve_agent
            _pgcfg = load_pipeline_config(conn)
            _plan_guard = validate_pipeline_plan(
                plan,
                stage_order=_pgcfg.get("stage_order"),
                required_stages=_pgcfg.get("required_stages"),
                mentioned_stages=mentioned_from_stage_hint(stage_hint),
                # 传解析器 ⇒ 才会校验路由；不传就跳过（不猜）
                resolve=lambda _n: _resolve_agent(conn, _n),
                mode=_pgcfg.get("mode"),
            )
            _plan_guard["config_source"] = _pgcfg.get("source")
            if _plan_guard.get("violations"):
                _pg_msg = render_violations(_plan_guard["violations"])
                warnings.append("计划校验：" + _pg_msg)
                logging.getLogger("mbse.planner").warning(
                    "计划语义校验发现 %d 项问题（mode=%s）：%s",
                    len(_plan_guard["violations"]), _plan_guard.get("mode"), _pg_msg)
            _pg_blocked = bool(_plan_guard.get("blocked"))
        except Exception as _e:
            # 兜底必须留痕：校验器自身崩了，绝不能变成"静默无问题"
            logging.getLogger("mbse.planner").warning(
                "计划语义校验异常（已跳过，不影响执行）：%s", str(_e)[:200])
            warnings.append("计划校验器异常，已跳过：%s" % str(_e)[:120])

        if _pg_blocked:
            _errs = [v for v in (_plan_guard or {}).get("violations", [])
                     if v.get("severity") == "error"]
            _err_txt = render_violations(_errs)
            logging.getLogger("mbse.planner").error(
                "计划语义校验未通过（mode=block），已中止执行：%s", _err_txt)
            return {
                "content": ("计划未通过语义校验，已中止执行（mode=block）：\n"
                            + "\n".join("· " + (v.get("message") or "") for v in _errs[:6])
                            + "\n\n把 `generate_rules.pipeline_guard_mode` 改回 `warn` 可先观察后收紧。"),
                "plan": plan, "task_summary": {"counts": {}, "total": 0, "items": []},
                "degraded": True,
                "data": {"degraded": True, "blocked_by": "plan_guard",
                         "warnings": warnings, "plan_guard": _plan_guard},
                "llm": {}, "latency_ms": int((_t.time() - t0) * 1000),
            }

        # 4) DAG 执行（agent_tasks 队列）
        # P0-7（2026-09-30）：会话入口此前恒传 run_id=0 → 所有批次共用 0、各自 clear_run 互相覆盖；
        # 且 `_finish_orchestrated` 按 conversation_id 读回 → 非流式路径**读不到自己的计划**
        # （"沉淀可复用工作流"静默失效）。现：未显式给批次号时分配独立号，显式给（Flow 节点重跑）
        # 才沿用 clear_run 语义，行为向后兼容。
        _given_run_id = int(run_id or 0)
        run_id = _given_run_id or TaskQueue.new_run_id(conn)
        executed = []
        _plan_error = ""   # ★ 2026-11-04：落库/执行中断的真实原因（此前被 except 吞掉）
        _done_items = []   # T7：已完成子任务结果（供依赖任务注入上下文快照）
        try:
            if _given_run_id:
                TaskQueue.clear_run(conn, run_id)
            TaskQueue.create_plan(conn, run_id, plan, assigned_by="session",
                                  conversation_id=int(conversation_id or 0))
            guard = 0
            # ── P4 replanning（2026-11-04）：**与流式路径同一个决策器** ────────────
            #   为什么补在这里：此前 `replanning` 全套只挂在流式主循环
            #   （`agent/pipeline_parts/stream.py`），非流式（Flow 节点 / 试跑 / 直调）
            #   **一行都没有** —— 同一句话走流式会「观察→重评估→调计划」，走非流式不会。
            #   闸门与节奏统一读 `services/replan_loop`（21 个键的唯一出处）；
            #   **应用动作**各自实现（流式发 SSE 事件，这里写回 executed / 返回 data），
            #   那是呈现差异，不是第二套判定。
            _rl = None
            try:
                from services.replan_loop import ReplanLoop, make_decision_writer
                _rl = ReplanLoop(
                    conn=conn, conversation_id=int(conversation_id or 0),
                    run_id=int(run_id or 0), goal=goal, provider_id=provider_id,
                    write_decision=make_decision_writer(
                        conn, conversation_id=int(conversation_id or 0),
                        run_id=int(run_id or 0)))
            except Exception as _e:
                # 兜底必须留痕：决策器不可用 ≠ 本次编排没问题，只是没做重规划。
                logging.getLogger("mbse.planner").warning(
                    "重规划决策器不可用（已跳过重规划，执行不受影响）：%s", str(_e)[:160])
                _rl = None
            _replan_notes: list = []   # 决策摘要（进 data，供审计/前端展示）
            _done_lats: list = []      # done 任务耗时（M4 预算偏离的滑动窗口）
            while TaskQueue.pending_count(conn, run_id) > 0 and guard < 200:
                guard += 1
                # ── P4 边界点：本批产出刚落库，是「计划还健康吗 / 目标达成了吗」
                #    的天然检查点（与流式路径同点，不打断任何 in-flight 任务）──
                if _rl is not None:
                    try:
                        _decs = _rl.decide(_done_items, TaskQueue.tasks(conn, run_id),
                                           latencies_ms=_done_lats)
                    except Exception as _e:
                        _decs = []
                        logging.getLogger("mbse.planner").warning(
                            "重规划决策异常（已跳过本边界）：%s", str(_e)[:160])
                    _stop_loop = False
                    for _d in _decs:
                        _act = str(_d.get("action") or "none")
                        if _act == "early_stop":
                            _ck = set(_d.get("cancel_keys") or [])
                            for _tk2 in TaskQueue.tasks(conn, run_id):
                                if (str(_tk2.get("task_key") or "") in _ck
                                        and str(_tk2.get("status") or "")
                                        in ("planned", "ready")):
                                    TaskQueue.cancel(
                                        conn, int(_tk2["id"]),
                                        "目标已达成提前收敛（主动放弃非必需任务）")
                                    executed.append({"key": _tk2.get("task_key"),
                                                     "title": _tk2.get("title"),
                                                     "agent": _tk2.get("agent_id"),
                                                     "ok": False, "skipped": True})
                            _replan_notes.append({"action": "early_stop",
                                                  "reason": _d.get("reason") or "",
                                                  "skipped": sorted(_ck)})
                            warnings.append("重规划：目标已达成提前收敛，放弃 %d 个非必需任务（%s）"
                                            % (len(_ck), ",".join(sorted(_ck)[:5])))
                            _stop_loop = True
                        elif _act == "stop":
                            for _tk3 in TaskQueue.tasks(conn, run_id):
                                if str(_tk3.get("status") or "") in ("planned", "ready"):
                                    TaskQueue.block(conn, int(_tk3["id"]),
                                                    "预计耗时超时间预算（M4 巡检）")
                            _replan_notes.append({"action": "stop",
                                                  "reason": _d.get("reason") or ""})
                            warnings.append("重规划：M4 时间预算不足，已停止剩余任务")
                            _stop_loop = True
                        elif _act == "refine":
                            # S10 ⇒ T2-light：只改剩余任务的 context（不追行、不改拓扑）。
                            # 文案与流式路径同款，便于日志/审计统一检索。
                            _note = ("\n【重规划 r1·巡检精修】%s。请按此调整后续执行重点。"
                                     % str(_d.get("reason") or "")[:300])
                            _n_touch = 0
                            for _tk4 in TaskQueue.tasks(conn, run_id):
                                if str(_tk4.get("status") or "") not in ("planned", "ready"):
                                    continue
                                try:
                                    conn.execute(
                                        "UPDATE agent_tasks SET context=? WHERE id=?",
                                        ((str(_tk4.get("context") or "") + _note)[:1000],
                                         int(_tk4["id"])))
                                    _n_touch += 1
                                except Exception:
                                    pass
                            if _n_touch:
                                conn.commit()
                            _replan_notes.append({"action": "refine",
                                                  "reason": _d.get("reason") or "",
                                                  "touched": _n_touch})
                            if _n_touch:
                                warnings.append("重规划：巡检发现健康度超阈，已对 %d 个剩余任务"
                                                "追加上下文说明" % _n_touch)
                        else:
                            # action=none 也留痕（「为什么不动」与「为什么动」同等重要），
                            # 但不进 warnings —— "计划健康"不是警告。
                            _replan_notes.append({"action": "none",
                                                  "reason": _d.get("reason") or ""})
                    if _stop_loop:
                        break
                # ★ 早停开时：同批里若既有必需又有非必需任务，本轮**只派必需任务**
                #   （否则可选任务与最后一个必需任务同批跑完 ⇒ 早停永远无事可做，
                #    扇出型计划下这条能力等于没开）。关早停时行为不变。
                ready = TaskQueue.ready_tasks(
                    conn, run_id,
                    defer_optional=bool(_rl is not None and _rl.g.get("early_stop")))
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
                        # 2026-10-29：plan 一并传入 → 依赖按传递闭包展开（长链不再断头）；
                        #   branch/user 透传（此前 branch 硬编码 "dev" ⇒ 非 dev 分支的知识库检索范围错、
                        #   user=None ⇒ 子任务无用户上下文、Skill 角色过滤失效）。
                        # ★ 2026-11-03：改用 `submit_with_ctx` —— 裸 submit 不继承 ContextVar，
                        #   子任务内的 LLM 调用会丢掉 conversation_id/run_id（真库 75 条实证），
                        #   且因 trace_id 为空触发 `begin_trace(0)` 把一次编排拆成多条链。
                        from core.audit import submit_with_ctx as _swc
                        _futs = [_swc(pool, self._run_task, tk, goal, conn, attachments,
                                      list(_done_items), artifact_digest,
                                      plan, branch, user)
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
                    results_list = [self._run_task(tk, goal, conn, attachments, list(_done_items),
                                                   artifact_digest, plan, branch, user) for tk in ready]
                for tk, rr in zip(ready, results_list):
                    tk_res, meta, ok, err = rr
                    if ok:
                        TaskQueue.complete(conn, tk["id"], tk_res, meta,
                                           latency_ms=int(meta.get("latency_ms", 0)))
                        _done_items.append({"task_key": tk["task_key"], "title": tk["title"],
                                            "result": tk_res, "status": "done"})
                        # M4 预算偏离需要真实耗时窗口（与流式路径 _done_lats 同口径）
                        _done_lats.append(int(meta.get("latency_ms", 0) or 0))
                    else:
                        TaskQueue.fail(conn, tk["id"], err)
                    TaskQueue.release_deps(conn, run_id, tk["task_key"])
                    executed.append({"key": tk["task_key"], "title": tk["title"],
                                     "agent": tk["agent_id"], "ok": ok})
        except Exception as e:
            # ★ 2026-11-04：原来只 `executed.append({"error": ...})` 就完了 —— 而 `executed`
            #   既不进 content 也不进 data，于是 `create_plan` 抛的**具体原因**（例如
            #   "计划引用了无法解析的 Agent：xxx"）被彻底吞掉，用户只看到
            #   "（计划已执行，但无成功交付物）" 这句**误导性**文案。
            #   改：留全量到 `_plan_error`，进 content 与 data。日志同时落一条 error。
            _plan_error = str(e)[:400]
            executed.append({"error": _plan_error})
            logging.getLogger("mbse.planner").error("计划落库/执行中断：%s", _plan_error)

        # 5) 汇总 + P1-1 总结节点
        summ = TaskQueue.summary(conn, run_id)
        done_items = [it for it in summ["items"] if it.get("status") == "done"]
        fail_items = [it for it in summ["items"] if it.get("status") in ("failed", "blocked")]
        if len(done_items) >= 2:
            content = self._summarize_plan(done_items, plan, provider_id)
        else:
            content = "\n\n".join(f"## {it.get('title')}\n{(it.get('result') or '').strip()}" for it in done_items) \
                or (("计划未执行 —— %s" % _plan_error) if _plan_error
                    else "（计划已执行，但无成功交付物）")
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
            "run_id": run_id,   # P0-7：把批次号交回调用方（`_finish_orchestrated` 据此读回自己的计划）
            "data": {"plan": plan[:max_tasks], "task_total": summ["total"], "task_done": len(done_items),
                     "task_failed": len(fail_items), "warnings": warnings,
                     "plan_guard": _plan_guard,          # ★ 计划语义校验明细（审计/前端展示）
                     "plan_error": _plan_error or None,  # ★ 落库/执行中断的真实原因（非空即未跑成）
                     # ★ 2026-11-04：重规划决策摘要（与流式路径同源决策器；空列表=本边界未动）
                     "replan": _replan_notes,
                     "reflection": reflection_meta,
                     "tasks": [{"key": it.get("task_key"), "title": it.get("title"),
                                "agent": it.get("agent_id"), "status": it.get("status")} for it in summ["items"]]},
            "llm": {}, "latency_ms": int((_t.time() - t0) * 1000),
        }

    def _run_task(self, tk: dict, goal: str, conn, attachments=None, done_items: list = None,
                  artifact_digest: str = "", plan: list = None, branch: str = "dev",
                  user: dict | None = None) -> tuple:
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
            # 2026-10-29：带 plan ⇒ 依赖按**传递闭包**展开（否则 t1→t2→t3 的链上 t3 看不见 t1）
            _ctx_block = build_subtask_context(tk, goal, done_items, artifact_digest=artifact_digest,
                                               plan=plan)
            if _ctx_block:
                query = f"[任务上下文快照]\n{_ctx_block}\n\n[任务]\n{query}"
            if context:
                query += f"\n\n任务上下文（只读，勿编造上下文外事实）：\n{context[:2000]}"
            if expected_output:
                query += f"\n\n期望输出/完成标准：\n{expected_output[:1000]}"
            # ★ 2026-11-02（改进点①，与流式路径同口径）：附件按需注入。
            #   非流式路径原本也是 `attachments or []` 全量透传 —— 两条路径必须同源，
            #   否则"同一个任务走流式/非流式拿到不同资料"，是更隐蔽的口径分裂。
            _sub_att, _att_manifest = attachments or [], ""
            try:
                from agent.context_compress import plan_subtask_attachments as _plan_att
                from core.config import get as _cfg_am2
                _sub_att, _att_manifest = _plan_att(
                    attachments or [],
                    declared=(cfg.get("attachments")
                              if isinstance(cfg.get("attachments"), list) else None),
                    mode=str(_cfg_am2("delegation", "attachments_mode", "auto") or "auto"))
            except Exception:
                _sub_att, _att_manifest = attachments or [], ""
            if _att_manifest:
                query += "\n\n" + _att_manifest
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
                out = pipe.execute(query, 0, branch, None, _sub_att, dry_run=True,
                                   forced_intent=tk.get("agent_id") or None, skill_name=skill_name,
                                   user=user, tools_whitelist=tools_wl)
                content = (out.get("content") or "").strip()
                llm = out.get("llm") or {}
                meta = self._delegate_result(content, status="done", provider=llm.get("provider", "-"),
                                             used_mock=llm.get("used_mock", True),
                                             latency_ms=int((time.time() - t0) * 1000))
                return content, meta, True, ""
            if ttype == "react":
                # ★ 2026-11-04（react 方案 A）：**委托主链路 Agent 执行**，不再自带工具循环。
                #
                #   旧实现 `self._exec_react(...)` 是「文本 JSON + 正则提取 + 自己拼观察」的
                #   **第二个工具循环**，与主链路 `execute.py` 的原生 tool-calling 循环平行：
                #     · 不读 `agent`（docstring 却声称由它驱动）⇒ 声明的执行主体是假的；
                #     · 不走 `_exec_tool_call` 的 HIL 闸 ⇒ 写类工具可绕过人工确认；
                #     · 靠模型自觉输出 `finish` 或耗尽步数 ⇒ 不写 finish 就白烧步数。
                #   改为委托后，react 只保留它真正独有的两件事：
                #     ① 工具白名单（最小权限，Worker ⊆ Supervisor）；② 轮次上限（受限多步求解）。
                #   依据：docs/react执行器-标杆调研与落地方案-20261104.md。
                from agent import AgentPipeline
                pipe = AgentPipeline()
                pipe._load_db_agents()
                _react_agent = str(tk.get("agent_id") or cfg.get("agent") or "").strip()
                # ⚠️ 不要写 `int(cfg.get("max_steps") or 5)`：`0` 是 falsy ⇒ 显式传 0 会被
                #    静默变成 5（"猜一个默认值"），而调用方本意是"最小轮次"。
                #    显式判空 + 越界夹取，语义才闭合。
                _raw_steps = cfg.get("max_steps")
                if _raw_steps in (None, ""):
                    _react_steps = 5
                else:
                    try:
                        _react_steps = max(1, min(int(_raw_steps), 10))
                    except (TypeError, ValueError):
                        _react_steps = 5
                out = pipe.execute(query, 0, branch, None, _sub_att, dry_run=True,
                                   # 不填 agent ⇒ forced_intent=None ⇒ 走意图识别
                                   # （`create_plan` 注释已明确这是编排的合法用法）
                                   forced_intent=_react_agent or None,
                                   user=user, tools_whitelist=tools_wl,
                                   max_tool_rounds=_react_steps)
                content = (out.get("content") or "").strip()
                llm = out.get("llm") or {}
                meta = self._delegate_result(content, status="done",
                                             provider=llm.get("provider", "-"),
                                             used_mock=llm.get("used_mock", True),
                                             latency_ms=int((time.time() - t0) * 1000))
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
                executor._flow_run_id = int(getattr(self, 'run_id', 0) or 0)  # P0-a：工具调用日志编排归属
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
