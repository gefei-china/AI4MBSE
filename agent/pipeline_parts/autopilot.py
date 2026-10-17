# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：自主续跑（auto-continue）—— 完成度评估器与配置读取。

与纯函数模块 `agent/auto_continue.py` 的分工：
  · auto_continue.py —— 判定逻辑（消息构造/判据解析/四道闸/续跑指令），零 IO，可单测；
  · 本文件 —— 唯一的 IO（config 读取 + llm_client 调用）与 pipeline 方法入口。

为什么评估器是**独立的一次非流式调用**而不是让 ReAct 循环自己决定：
  Claude Code /goal 的实证结论——模型「想停」和「该停」是两件事，让模型自己判断
  完成度，它会在多步任务的第一步后就收尾（米爸反馈的原话：「每执行一轮都会结束」）。
  评估器把「停不停」从主模型的自由裁量变成**显式判据**。

调用契约（stream.py / execute.py 同构两处）：
  verdict = self._evaluate_completion(...)      # None = 失败 ⇒ 调用方按不续跑处理
  cont, why = _ac.decide(verdict, used=..., max_used=..., seen_steps=...,
                         deadline_left_s=..., budget_left=...)
  if cont: messages.append(user: continuation_prompt(...)); 继续 ReAct
"""
from .common import *


class AutoPilotMixin:
    """自主续跑的配置读取 + LLM 完成度评估（IO 层）。"""

    def _auto_continue_cfg(self) -> dict:
        """读自主续跑三配置。读失败 ⇒ 全关（fail-safe：配置异常时不启动自主循环）。"""
        try:
            from core import config as _cfg
            return {
                "enabled": _cfg.as_bool("agent", "auto_continue_enabled", True),
                "max": max(0, int(_cfg.get("agent", "auto_continue_max", 3) or 0)),
                # 墙钟上限：0/负 = 不设（仍受次数/token 闸约束）
                "deadline_s": float(_cfg.get("agent", "auto_continue_deadline_s", 600) or 0),
            }
        except Exception:
            return {"enabled": False, "max": 0, "deadline_s": 0}

    def _evaluate_completion(self, *, user_input, exec_tools, answer, provider_id,
                             agent_name="", model_params=None, guard=None):
        """LLM 完成度评估（Claude Code /goal evaluator 同款思路）。

        :returns: verdict dict 或 None（None = 评估失败/无结论 ⇒ 调用方按「不续跑」处理）。
        ⚠️ 本方法**绝不抛异常**：评估是增强能力，它的失败只会让我们少续跑一轮，
          绝不能把主流程带崩（任何异常都吞掉返 None）。
        """
        try:
            from llm import llm_client
            from agent import auto_continue as _ac
            from agent.pipeline_parts.common import CHAT_TASK_EVAL
            msgs = _ac.build_eval_messages(user_input, _ac.tools_brief(exec_tools),
                                           answer, agent_name)
            resp = llm_client.chat(msgs, provider_id=provider_id, stream=False,
                                   _intent=CHAT_TASK_EVAL, _model_params=model_params)
            if guard is not None:
                try:
                    from agent.loop_guard import tokens_of as _tok_of
                    guard.add_tokens(_tok_of(resp))
                except Exception:
                    pass
            text = (((resp or {}).get("choices") or [{}])[0]
                    .get("message", {}).get("content") or "")
            return _ac.parse_verdict(text)
        except Exception:
            return None
