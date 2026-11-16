# -*- coding: utf-8 -*-
"""画布节点执行器（**已随画布工作流移除**）—— 本模块仅保留评审静态方法。

★ 2026-11-05：画布工作流（`agent_flows` + DAG 执行）已明确舍弃，原有的 18 个节点
  执行器（tool/agent/skill/mcp/if/code/http/webhook/iteration/knowledge/pubsub/
  orchestrator/debate/react/planner/reflection）与事件/黑板辅助全部移除。
  它们在移除前**已无任何生产调用方**：主链路编排走 `workflows/planner.py::_run_task`
  （react/agent 分支自带实现），画布端点与 FlowExecutor.run 均已删除。

  保留原因：`_evaluate_content` 是「画布/会话评审口径一致」的单一实现，
  被 `workflows/refine.py::RefineGate`（主链路反思门）直接调用。
  迁移到别处反而会制造第二份实现 —— 那正是本仓反复出现过的问题。
"""
import json
import re

from .planner import FlowPlannerMixin


class FlowNodesMixin:
    """节点辅助（当前仅评审）：供 RefineGate 复用的确定性评审函数。"""


    @staticmethod
    def _clip_for_eval(text: str) -> str:
        """评审输入裁剪：**配置化上限 + 头尾采样**（复用 planner 的 `_head_tail_clip`）。

        为什么不再用 `[:2000]`（2026-09-20 conv 372 实测 —— **第 6 层截断，也是最隐蔽的一层**）：
        `[:2000]` 是**只留头**的硬上限，而汇总报告已长到 22,409 字符 → 评审只看得到
        「一、需求分析」为止，于是判「t2/t3 无实质内容」，**而那两节确实存在**。
        更隐蔽的是：报告被修得越长，评审看到的**比例**越小，gap 描述随报告结构漂移
        （「1.2 节末尾」→「2.2 节之后」→「只到 2.1」），极易误导成"报告被截断"而去调输出上限。
        """
        try:
            from core import config as _cfg
            cap = int(_cfg.get("refine", "eval_in_chars", 24000) or 24000)
        except Exception:                                          # noqa: BLE001
            cap = 24000
        try:
            from workflows.planner import FlowPlannerMixin
            return FlowPlannerMixin._head_tail_clip(text, cap)
        except Exception:                                          # noqa: BLE001
            t = str(text or "")
            return t if (cap <= 0 or len(t) <= cap) else t[:cap]

    @staticmethod
    def _pass_score() -> int:
        """通过线（`refine.pass_score`，默认 70）。

        ⚠️ 必须在本类自持一份，**不能**去调 `RefineGate._pass_score()`：
           ① `refine.py` 顶部就 import 了本模块（`from workflows.nodes import FlowNodesMixin`）
              ⇒ 反向 import 会成环；
           ② 本轮实测：写成 `FlowNodesMixin._pass_score` 时该属性根本不存在，
              直接抛 `AttributeError`，而当时仍处在新加的 try 里 ⇒
              表现成"评审调用异常"，**评审整体失效**。
              这正是新增 `_diag` 的价值所在：修复前这类错只会显示六个字
              「评审解析失败」，现在能读到 `AttributeError` 与具体属性名。
        配置口径与 RefineGate 完全一致（同一个 `refine.pass_score` 键）。
        """
        try:
            from core import config as _cfg
            return int(_cfg.get("refine", "pass_score", 70) or 70)
        except Exception:                                          # noqa: BLE001
            return 70

    @staticmethod
    def _eval_max_tokens() -> int:
        """评审**输出**上限（P0-7，2026-09-30 会话 514 实测）。

        原先硬编码 `max_tokens=1024` 的假设是"评审输出是短 JSON" —— 但当前 provider 是
        **思考模型**（回包含 `reasoning_content`），**推理与正文共享同一个上限**。
        实测大输入（`eval_in_chars=24000`）下 `reasoning_tokens` 吃到 789/1024，
        留给 JSON 的额度不足 → 正文被 `finish_reason="length"` 从中间截断、闭合 `}` 丢失。
        故改为 config 可调（默认 3072；标定见 `core/config.py` 的 `refine.eval_max_tokens`）。
        """
        try:
            from core import config as _cfg
            return int(_cfg.get("refine", "eval_max_tokens", 3072) or 3072)
        except Exception:                                          # noqa: BLE001
            return 3072

    @staticmethod
    def _parse_eval_json(raw: str) -> dict:
        """从评审回包里取 JSON —— **完整优先、截断兜底**（P0-7，2026-09-30）。

        为什么必须有"截断兜底"：`max_tokens` 不够时正文是**半截 JSON**（有 `{` 无 `}`）→
        原正则 `\\{[\\s\\S]*\\}` 必然匹配不到 → 被硬编码判 0 分「解析失败」，
        **把"模型其实给了分"报成"解析失败"**（方向与 §7.3 的"报告被截断"那类相反）。
        兜底只抽**关键字段**（`score` / `passed`）且**不臆造**：抽不到就返回 `{}`，
        由调用方按"无输出/解析失败"如实标注。截断这件事**不静默** —— 挂一条 issue 提示。
        """
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if m:
            try:
                d = json.loads(m.group(0))
                if isinstance(d, dict):
                    return d
            except Exception:                                      # noqa: BLE001
                pass
        loose = {}
        ms = re.search(r'"score"\s*:\s*(-?\d+)', raw or "")
        if ms:
            loose["score"] = int(ms.group(1))
        mp = re.search(r'"passed"\s*:\s*(true|false)', raw or "", re.I)
        if mp:
            loose["passed"] = mp.group(1).lower() == "true"
        if not loose:
            return {}
        loose["issues"] = ["评审输出被 max_tokens 截断（分数按宽松抽取，未取到 issues/advice）"
                           "，建议上调 refine.eval_max_tokens"]
        return loose

    @staticmethod
    def _evaluate_content(content: str, criteria: str = "输出是否完整、合理、符合要求") -> dict:
        """T1 公共评审函数：LLM 按标准对输出评分，返回 {score, passed, issues, advice, _meta}。

        供画布 reflection 节点（_exec_reflection）与会话编排 RefineGate 共用，
        保证「画布/会话」评审行为一致。LLM 异常/解析失败 → score=0（不抛异常，确定性保持）。
        """
        from llm import llm_client
        if not content:
            return {"score": 0, "passed": False, "issues": ["目标无输出"], "advice": "", "_meta": {}}
        tgt = FlowNodesMixin._clip_for_eval(content)
        prompt = (
            f"你是质量评审 Agent。对以下输出按标准评估，只输出 JSON："
            f'{{"score": 0-100 的整数, "passed": true/false, "issues": ["问题1", ...], "advice": "改进建议"}}\n\n'
            f"评估标准：{criteria}\n\n被评审输出：\n{tgt}"
        )
        try:
            # 评审输出是**短 JSON**（score/issues/advice）；显式给上限（防"话多"把 JSON 冲散）。
            # ⚠️ 上限**不能太小**：思考模型的 reasoning 与正文共享额度，不够就会把 JSON 截断
            #   （见 `_eval_max_tokens` 与 `_parse_eval_json` 的取证）。
            resp = llm_client.chat([{"role": "user", "content": prompt}],
                                   max_tokens=FlowNodesMixin._eval_max_tokens(),
                                   # P0-a（2026-10-02）：补 intent 标注。此前**没传** ⇒ 评审调用与
                                   # 主对话共用 intent='' 桶 ⇒ 按 intent 聚合时两者混在一起，
                                   # "到底是主对话被截断、还是评审被截断"无法归因（P1-27 吃过这个亏）。
                                   _intent="plan_eval")
            _ch = (resp.get("choices") or [{}])[0]
            msg = _ch.get("message", {}) or {}
            raw = msg.get("content") or ""
            _fr = _ch.get("finish_reason") or ""
            data = FlowNodesMixin._parse_eval_json(raw)
            if not data:
                # 区分三种"没取到分"的情形，别把成因混成一个"解析失败"（现场判读要靠它）。
                # 顺序要紧：**空内容优先**于"被截断" —— 空内容时 `finish_reason` 也是 `length`，
                # 先判 length 会把更具体的成因（reasoning 吃满额度）盖掉。
                if not (raw or "").strip():
                    _why = "评审无输出（max_tokens 可能被 reasoning 占满）"
                elif _fr == "length":
                    _why = "解析失败（输出被 max_tokens 截断，未见 score 字段）"
                else:
                    _why = "解析失败"
                # ★★ 2026-10-30（用户「0/100 未通过 · 评审解析失败，这个评审的依据是什么，
                #   看不出来」）：**把「为什么没取到分」的证据一并带回**。
                #   此前只回一句"解析失败" ⇒ 用户与我们**都无从判断**到底是
                #   模型没吐 JSON、被截断、还是 provider 报错。
                #   现在带 raw_head（首 300 字）+ raw_len + finish_reason，
                #   足以区分：模型吐了别的措辞 / 吐了半截 JSON / 干脆空。
                data = {"score": 0, "passed": False, "issues": [_why],
                        "_diag": {"raw_len": len(raw or ""),
                                  "raw_head": (raw or "")[:300],
                                  "finish_reason": _fr,
                                  "max_tokens": FlowNodesMixin._eval_max_tokens(),
                                  "criteria": criteria}}
            score = int(data.get("score", 0))
            issues = data.get("issues") or []
            # ★★ 2026-10-30：**分数与通过线是主判据，模型自报的 passed 只作参考**。
            #   实测（本轮跑同一段报告两次）：score=75 而 passed=false —— 模型自己
            #   给的分数过了线（pass_score=70），它却在 passed 里写 false。
            #   界面于是显示「75/100（通过线 70）未通过」，用户看到的是一条自相矛盾的
            #   结论，且**没有任何办法判断该信哪个**（这正是"看不出评审依据"的另一半）。
            #   取值原则（与本仓"确定性判据优先于模型自报"一致）：以 score 与
            #   pass_score 的比较为准；模型自报与之矛盾时**以分数为准并留痕**，
            #   不静默采信任何一方。
            _m_passed = data.get("passed")
            _by_score = score >= FlowNodesMixin._pass_score()
            passed = _by_score
            _inconsistent = (_m_passed is not None and bool(_m_passed) != _by_score)
            meta = resp.get("_meta", {})
            meta = dict(meta) if isinstance(meta, dict) else {}
            meta.setdefault("finish_reason", _fr)   # 便于现场判读"是被截断还是模型没吐 JSON"
            _diag = data.get("_diag") or {}
            _diag.update({"criteria": criteria,
                          "max_tokens": FlowNodesMixin._eval_max_tokens(),
                          "pass_score": FlowNodesMixin._pass_score()})
            if _inconsistent:
                _diag["inconsistent"] = {
                    "model_passed": bool(_m_passed),
                    "by_score": _by_score,
                    "score": score,
                    "pass_score": FlowNodesMixin._pass_score(),
                }
                # 让用户在 issues 里直接看到这条矛盾，而不是只在 diag 里
                issues = list(issues) + [
                    "评审自相矛盾：模型给出 %d 分（通过线 %d，按分应判%s），"
                    "但自报 passed=%s。已按分数判定；此处如实留痕供人工复核"
                    % (score, FlowNodesMixin._pass_score(),
                       "通过" if _by_score else "未通过", str(bool(_m_passed)).lower())]
            return {"score": score, "passed": passed, "issues": issues,
                    "advice": data.get("advice", ""), "_meta": meta,
                    "criteria": criteria, "_diag": _diag}
        except Exception as e:
            # ★★ 2026-10-30：**异常也必须说出自己是谁**。
            #   原实现 `except Exception: return {... "issues": ["评审解析失败"] ...}`
            #   把异常整个吞掉 —— 真机 msg 5378 因此只留下"评审解析失败"六个字，
            #   而 `reflection.provider` 同时是 `"-"`（说明走的就是这条分支），
            #   可**连是 KeyError 还是超时还是 401 都无从知道**。
            #   现在：异常类名 + 摘要进 issues，并把 type/msg 放进 `_diag` 供卡片展示。
            _et = type(e).__name__
            _em = str(e)[:200]
            return {"score": 0, "passed": False,
                    "issues": ["评审调用异常（%s）：%s" % (_et, _em or "无详情")],
                    "advice": "", "_meta": {},
                    "criteria": criteria,
                    "_diag": {"error_type": _et, "error_msg": _em,
                              "criteria": criteria,
                              "max_tokens": FlowNodesMixin._eval_max_tokens()}}

    # ── 运行轨迹落库（P0：先建 run 占位 → 检查点落库 → 结束写 steps）──
