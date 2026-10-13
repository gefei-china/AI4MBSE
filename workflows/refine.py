"""P0-1 反思闭环：RefineGate（汇总 → 评审 → 修订 → 复评）。

对齐 DeepAgent「感知-规划-行动-记忆-反思」：自动编排在子任务汇总与交付之间
插入质量评审，未达标自动修订，全程无人工介入。评审复用画布 reflection 节点
的公共函数 FlowNodesMixin._evaluate_content，保证画布/会话行为一致。

降级：LLM Mock/无 key/解析失败 → 返回原始汇总 + degraded=True（确定性保持）。
"""
import json
from core import config as _cfg

DEFAULT_CRITERIA = (
    "1) 完整性：是否覆盖全部子任务交付物；2) 一致性：是否存在内部矛盾或与交付物冲突；"
    "3) 事实性：是否混入交付物之外的虚构内容；4) 风险标注：交付物中的风险/待确认项是否被显式标注。"
)


class RefineGate:
    """编排汇总结果质量门：首轮通过即零额外开销；不通过按 issues/advice 修订后复评。"""

    @staticmethod
    def _pass_score() -> int:
        return int(_cfg.get("refine", "pass_score", 70))

    @staticmethod
    def _max_rounds() -> int:
        return int(_cfg.get("refine", "max_rounds", 2))

    @staticmethod
    # ⚠️ 观测口径提醒（2026-10-13 复核）：`llm_usage_stats` 里 `plan_eval`
    #   计数很低（真库 1 次）**不代表评审没跑** —— ① `_intent="plan_eval"`
    #   标注是 2026-10-02（commit 42cf293）才上线的，此前 13 次真评审
    #   （11/14 条有真分数：score 25~72、rounds 1~3、provider=deepseek-v4-flash）
    #   都没进这张表；
    #   ② 标注上线之后又恰好没有多子任务编排流量。
    #   ⇒ **判据要看 messages.card_data 的 `reflection` 字段，不是这张表的计数**。
    @staticmethod
    # ⚠️ 观测口径提醒（2026-10-13 复核）：`llm_usage_stats` 里 `plan_eval`
    #   计数很低（真库 1 次）**不代表评审没跑** —— ① `_intent="plan_eval"`
    #   标注是 2026-10-02（commit 42cf293）才上线的，此前 11 条真评审
    #   （score 25~72、rounds 1~3、provider=deepseek-v4-flash）都没进这张表；
    #   ② 标注上线之后又恰好没有多子任务编排流量。
    #   ⇒ **判据要看 messages.card_data 的 `reflection` 字段，不是这张表的计数**。
    @staticmethod
    def _enabled() -> bool:
        return _cfg.as_bool("refine", "enabled", True)

    # ── 2026-09-20：修订环节的四个上限改为**配置驱动**（此前是散在代码里的硬编码）──────
    # 为什么必须改：`orch_content = _ref.get("content") or orch_content` —— **用户最终看到的
    # 报告正文就是修订输出**，所以这四处上限才是"报告有多长"的真正决定者。
    # 会话 369 门禁报「t2/t3 各节内容未展开」即由此而来（修订模型只拿到每个交付物 400 字符、
    # 待修订报告前 6000 字符，手上没有料可展开）。
    @staticmethod
    def _item_chars() -> int:
        """修订时可引用：**单个**交付物字符数（原硬编码 400）。"""
        return int(_cfg.get("refine", "item_chars", 1200) or 1200)

    @staticmethod
    def _items_total_chars() -> int:
        """修订时可引用：交付物**合计**字符数（原硬编码 2000）。"""
        return int(_cfg.get("refine", "items_total_chars", 6000) or 6000)

    @staticmethod
    def _report_in_chars() -> int:
        """修订时可读：**待修订报告**字符数（原硬编码 6000，且只留头）。

        2026-09-20 conv 371：从 12000 提到 **24000** —— 报告实际长到 19011 字符后，
        12000 的头尾采样会把第五/六/七章整段省略，评审据此报「章节被引用但不可见」。
        下界口径：必须 >= `max_tokens` 能产出的字符数（实测 ≈2.9 字符/token，8000 → ~23000）。
        """
        return int(_cfg.get("refine", "report_in_chars", 24000) or 24000)

    @staticmethod
    def _max_tokens() -> int:
        """修订**输出**上限（token，原硬编码 3000）。"""
        return int(_cfg.get("refine", "max_tokens", 8000) or 8000)

    @staticmethod
    def _clip(text: str, cap: int) -> str:
        """与汇总环节共用同一套**头尾采样**（`FlowPlannerMixin._head_tail_clip`）。

        ⚠️ 原实现是 `str(content)[:6000]`（**只留头**）—— 而报告的「总体结论与后续建议」
        按汇总提示词的要求落在**末尾**，于是修订模型根本看不到结论，自然也保不住它。
        降级：planner 不可导入时退化为只留头（保持"不抛异常"）。
        """
        try:
            from workflows.planner import FlowPlannerMixin
            return FlowPlannerMixin._head_tail_clip(text, cap)
        except Exception:                                  # noqa: BLE001
            t = str(text or "")
            return t if (cap <= 0 or len(t) <= cap) else t[:cap]

    @staticmethod
    def _partial_note(done_items, agg_status):
        """agg_status=partial 时生成人工复核建议（不自动重派，避免循环；缺口标注缺失交付物）。"""
        if agg_status != "partial":
            return None
        _missing = [it.get("task_key") for it in (done_items or [])
                    if (it.get("summary") or {}).get("status") in ("partial", "failed")]
        _suffix = f"（缺口：{','.join(_missing)}）" if _missing else ""
        return "编排部分完成，建议人工复核缺失交付物" + _suffix

    @staticmethod
    def _with_pt(issues, done_items, agg_status):
        """issues 追加人工复核建议（仅 agg_status=partial 时）；passed 判定不受影响。"""
        _pt = RefineGate._partial_note(done_items, agg_status)
        if _pt:
            issues = list(issues) + [_pt]
        return issues

    @staticmethod
    def _conflicts(done_items):
        """★ 2026-10-14（批次三 #15）：评审前的**确定性结论冲突检测**。

        为什么必须有这一步（取证见 `services/subtask_protocol.py::detect_conflicts` 头注）：
        评审输入原本只有**拼接后的**报告 —— 两个子 Agent 对同一事实给了相反说法时，
        综合报告会把它们揉成一句看似自洽的话，**评审拿不到"这是 t2 说的、那是 t3 说的"**。

        返回 `(conflicts, stats)`：
          · conflicts  非空 ⇒ 把条目文本拼进评审 criteria（评审必须显式裁决）；
          · stats.verdict == "no-data" ⇒ **不得声称"已检查无冲突"**（无 claim 可判）。
        ⚠️ 任何异常都降级为「无冲突 + 不可判定」，**绝不阻断编排**。
        """
        try:
            from services import subtask_protocol as _sp
            _sums = []
            for it in (done_items or []):
                _sm = it.get("summary") if isinstance(it, dict) else None
                if isinstance(_sm, dict):
                    _sums.append(_sm)
            _cf = _sp.detect_conflicts(_sums)
            return _cf, _sp.conflict_stats(_sums)
        except Exception:
            return [], {"claims_total": 0, "claims_anchored": 0,
                        "subjects": 0, "subtasks_with_claims": 0, "verdict": "error"}

    @classmethod
    def run(cls, report: str, goal: str = "", done_items: list = None,
            plan: list = None, provider_id=None, criteria: str = DEFAULT_CRITERIA,
            agg_status: str = "full") -> dict:
        """对汇总报告执行评审→修订闭环。

        参数：
          report     子任务汇总结果（_summarize_plan 输出）
          goal       原始用户目标（修订 prompt 用）
          done_items 子任务交付物列表 [{task_key,title,agent_id,result}]
          plan       计划（可选，评审完整性参考）
          agg_status 编排汇总三态（Task 10）：full/partial/failed；partial 时不自动重派
                     （避免循环），仅做质量评审 + 在返回 issues 中追加人工复核建议
        返回：
          {content, score, passed, rounds, issues, advice, llm, degraded}
        """
        if not cls._enabled():
            return {"content": report, "score": None, "passed": True, "rounds": 0,
                    "issues": cls._with_pt([], done_items, agg_status),
                    "advice": "", "llm": None, "degraded": False,
                    # ⚠️ 与主路径**同键**（本仓老教训：两条路径字段不齐 ⇒ 前端/日志误判）
                    "conflicts": {"count": 0, "stats": {"verdict": "skipped"},
                                  "items": []}}
        if not report or not str(report).strip():
            return {"content": report or "", "score": None, "passed": True, "rounds": 0,
                    "issues": cls._with_pt([], done_items, agg_status),
                    "advice": "", "llm": None, "degraded": False,
                    "conflicts": {"count": 0, "stats": {"verdict": "skipped"},
                                  "items": []}}
        from workflows.nodes import FlowNodesMixin
        _ic = cls._item_chars()
        items_txt = "\n".join(
            f"[{it.get('task_key')}] {it.get('title')}（{it.get('agent_id') or '-'}）\n"
            + cls._clip(it.get('result') or '', _ic)
            for it in (done_items or [])[:20])
        # ★ 2026-10-14（批次三 #15）：评审前先做**确定性冲突检测**，把命中项
        #   写进评审标准 —— 评审必须显式裁决，不能让它在"综合报告"里被揉平。
        #   ⚠️ 未命中**不追加任何文字**（提示词逐字节不变 ⇒ 零回归）；
        #   ⚠️ stats.verdict != "checkable" 时**不谎称"已检查"**，只把口径带给调用方。
        _conflicts, _cstats = cls._conflicts(done_items)
        _eval_criteria = criteria
        if _conflicts:
            try:
                from services.subtask_protocol import format_conflicts as _fmt
                _eval_criteria = (criteria + "\n5) ⚠️**结论冲突裁决（最高优先级）**：以下子任务"
                                  "对同一对象给出了互斥结论，必须在 issues 里**逐条指出采纳哪一侧"
                                  "及理由**，不得回避或含糊带过：\n" + _fmt(_conflicts, limit=5))
            except Exception:
                _eval_criteria = criteria
        content = report
        ev = {"score": 0, "passed": False, "issues": [], "advice": "", "_meta": {}}
        rounds = 0
        try:
            for round_i in range(1, cls._max_rounds() + 2):  # 首评 + 最多 max_rounds 次修订
                rounds = round_i
                ev = FlowNodesMixin._evaluate_content(content, _eval_criteria)
                if ev.get("passed") or ev.get("score", 0) >= cls._pass_score():
                    break
                if round_i > cls._max_rounds():
                    break
                content = cls._refine(content, goal, items_txt, ev, provider_id)
            _issues = list(ev.get("issues") or [])[:10]
            # ★ 冲突项**无条件**进 issues（不依赖 LLM 是否照做）——
            #   确定性检出的事实不该依赖另一个模型"记得写出来"。
            if _conflicts:
                try:
                    from services.subtask_protocol import format_conflicts as _fmt2
                    _issues = _issues + _fmt2(_conflicts, limit=3).split("\n")
                except Exception:
                    pass
            return {
                "content": content,
                "score": ev.get("score"),
                "passed": bool(ev.get("passed", ev.get("score", 0) >= cls._pass_score())),
                "rounds": rounds,
                "issues": cls._with_pt(_issues, done_items, agg_status),
                "advice": ev.get("advice", ""),
                # ★ 冲突口径随结果一并返回（card_data 可直接展示，排障不必翻日志）
                "conflicts": {"count": len(_conflicts), "stats": _cstats,
                              "items": [{"subject": c.get("subject"), "kind": c.get("kind"),
                                         "reason": c.get("reason"),
                                         "claims": c.get("claims")} for c in _conflicts[:5]]},
                "llm": {"provider": (ev.get("_meta") or {}).get("provider", "-"),
                        "model": (ev.get("_meta") or {}).get("model", "-"),
                        "used_mock": (ev.get("_meta") or {}).get("used_mock", True)},
                "degraded": False,
            }
        except Exception:
            return {"content": report, "score": None, "passed": True, "rounds": 0,
                    "issues": cls._with_pt([], done_items, agg_status),
                    "advice": "", "llm": None, "degraded": True,
                    "conflicts": {"count": 0, "stats": {"verdict": "error"}, "items": []}}

    @staticmethod
    def _refine(content: str, goal: str, items_txt: str, ev: dict, provider_id=None) -> str:
        """按评审意见修订报告（可引用子任务交付物补证据，禁止虚构）。失败返回原文。"""
        from llm import llm_client
        issues = "\n".join(f"- {i}" for i in (ev.get("issues") or [])[:10]) or "- 无明确问题"
        advice = ev.get("advice", "")
        prompt = (
            "你是 MBSE 团队任务汇总修订专家。根据评审意见修订以下汇总报告：\n"
            "1) 只按 issues/advice 修订，可引用子任务交付物补充证据；2) 禁止虚构交付物之外的事实；"
            "3) 保留原有分节结构与风险/待确认标注；4) 直接输出修订后的完整报告正文，不要输出 JSON。\n\n"
            f"原始目标：{str(goal)[:200]}\n\n"
            f"评审意见（issues）：\n{issues}\n\n"
            f"改进建议（advice）：{advice}\n\n"
            f"可引用交付物：\n{RefineGate._clip(items_txt, RefineGate._items_total_chars())}\n\n"
            f"待修订报告：\n{RefineGate._clip(str(content), RefineGate._report_in_chars())}"
        )
        try:
            # 2026-09-17 S4：修订环节加输出上限 —— 实测 plan_refine 平均 completion 7,736，
            # 是全流程最贵的输出。上限取 3000（而非 1500）：本环节产出的是**用户可见的报告正文**，
            # 而采纳判定很宽（下方 `len(text.strip()) > 40` 即采纳），截断文本会被当成正常结果。
            # 3000 相比均值仍省 60%+，需要更省时再下调。
            # 2026-09-20：改为 `refine.max_tokens`（默认 **8000**，对齐当前默认 provider 天花板 8192）。
            # 判据（会话 369）：门禁报「报告在末尾被截断」= 输出被切 → 上调本项；
            # 报「交付物/某节未展开」= **输入**没料 → 上调 item_chars / items_total_chars / report_in_chars。
            resp = llm_client.chat([{"role": "user", "content": prompt}], provider_id=provider_id,
                                   max_tokens=RefineGate._max_tokens(), _intent="plan_refine")
            text = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
            if text and len(text.strip()) > 40:
                return text.strip()
        except Exception:
            pass
        return content
