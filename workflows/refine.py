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
        """修订时可读：**待修订报告**字符数（原硬编码 6000，且只留头）。"""
        return int(_cfg.get("refine", "report_in_chars", 12000) or 12000)

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
                    "advice": "", "llm": None, "degraded": False}
        if not report or not str(report).strip():
            return {"content": report or "", "score": None, "passed": True, "rounds": 0,
                    "issues": cls._with_pt([], done_items, agg_status),
                    "advice": "", "llm": None, "degraded": False}
        from workflows.nodes import FlowNodesMixin
        _ic = cls._item_chars()
        items_txt = "\n".join(
            f"[{it.get('task_key')}] {it.get('title')}（{it.get('agent_id') or '-'}）\n"
            + cls._clip(it.get('result') or '', _ic)
            for it in (done_items or [])[:20])
        content = report
        ev = {"score": 0, "passed": False, "issues": [], "advice": "", "_meta": {}}
        rounds = 0
        try:
            for round_i in range(1, cls._max_rounds() + 2):  # 首评 + 最多 max_rounds 次修订
                rounds = round_i
                ev = FlowNodesMixin._evaluate_content(content, criteria)
                if ev.get("passed") or ev.get("score", 0) >= cls._pass_score():
                    break
                if round_i > cls._max_rounds():
                    break
                content = cls._refine(content, goal, items_txt, ev, provider_id)
            return {
                "content": content,
                "score": ev.get("score"),
                "passed": bool(ev.get("passed", ev.get("score", 0) >= cls._pass_score())),
                "rounds": rounds,
                "issues": cls._with_pt((ev.get("issues") or [])[:10], done_items, agg_status),
                "advice": ev.get("advice", ""),
                "llm": {"provider": (ev.get("_meta") or {}).get("provider", "-"),
                        "model": (ev.get("_meta") or {}).get("model", "-"),
                        "used_mock": (ev.get("_meta") or {}).get("used_mock", True)},
                "degraded": False,
            }
        except Exception:
            return {"content": report, "score": None, "passed": True, "rounds": 0,
                    "issues": cls._with_pt([], done_items, agg_status),
                    "advice": "", "llm": None, "degraded": True}

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
