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

    # ── P0-1（2026-10-28）验收驱动的完成判定接线 ──────────────────
    # 方案：docs/持续自主执行能力-最终方案设计-20261028.md §6 第 1 步。
    #
    # 为什么必须是**上位约束**而不是又一个评估器：
    #   实测（msg 3985）评估器把「建议下一步执行校验」判成 done=true，
    #   且**只改提示词纠正不了**（实测纠正率 1/2，3985 仍误判）——
    #   因为评估器没有对照清单可查，只能凭语义猜"校验算不算用户的原始要求"。
    #   ⇒ 必须由 todo_gate() 提供**客观依据**：待办里有「校验」且未通过验收
    #     ⇒ 直接强制 done=False，完全不需要评估器理解语义。
    #
    # ⚠️ 复用纪律：判定逻辑只写这一份，stream.py / execute.py 两处同构调用
    #   **都调这里** —— 绝不各写一份（两处各写一份必然错位）。

    def _todo_gate_enabled(self) -> bool:
        """计划外化是否启用（config `agent.todo_enabled`，**默认关**）。"""
        try:
            from core import config as _cfg
            return bool(_cfg.as_bool("agent", "todo_enabled", False))
        except Exception:
            return False

    def _todo_gate(self, conversation_id: int, conn=None) -> dict:
        """跑待办完成判定。返回 gate 字典（含 fallback 标记）。

        :param conn: 可选数据库连接。**默认 None ⇒ 用生产 `db_conn()`**。
                     显式传入 ⇒ 用给定连接（门禁脚本用独立测试库，不碰生产库）。
        :returns: ``{"done": bool, "next_step": str, "remaining": [...],
                     "fallback": bool, "reason": str}``
                 `fallback=True` ⇒ 无待办 ⇒ 调用方应**原样沿用**评估器结论
                 （逐字节兼容 D-3：不改变现有 98.4% 只读问答的行为）。
        """
        if not self._todo_gate_enabled():
            return {"done": False, "next_step": "", "remaining": [],
                    "fallback": True, "reason": "todo_enabled 关闭 ⇒ 走原评估器路径"}
        try:
            from agent.todo_store import todo_gate_for_conv as _g
            if conn is not None:
                return _g(conn, conversation_id)
            with db_conn() as c:
                return _g(c, conversation_id)
        except Exception as e:
            # ⚠️ 读待办失败**绝不能**把主流程带崩，也**绝不能**误判成"已完成"
            #   ⇒ 退回评估器路径（fail-safe 向"按原样处理"）。
            logger.warning("[autopilot] todo_gate 读取失败，退回评估器路径：%r", e)
            return {"done": False, "next_step": "", "remaining": [],
                    "fallback": True, "reason": "读待办失败 ⇒ 退回评估器路径"}

    def _apply_todo_gate(self, verdict: dict, conversation_id: int,
                          conn=None) -> tuple:
        """把待办闸的结论**应用到**评估器 verdict 上。返回 ``(verdict, gate)``。

        ★ 这个方法存在的唯一理由：**消除重复**。
          stream.py 与 execute.py 两处同构接线，若各写一份「怎么把 gate 结果
          覆盖到 verdict 上」的分支，就是"同一个语义在两处各写一份"——
          必然错位（这与本仓已记录的「四个环节各写一个数字，最后对不上」同族）。

        语义（唯一一份，两处共用）：
          · fallback（无待办 / 开关关）⇒ **原样返回 verdict**（逐字节兼容 D-3）
          · gate.done=True  ⇒ 全部通过验收 ⇒ verdict 改为 done=True（不许续跑）
          · gate.done=False ⇒ 有未完成   ⇒ 强制 done=False + next_step 指向第一个 pending
        """
        try:
            gate = self._todo_gate(conversation_id, conn=conn)
        except Exception:
            gate = {"done": False, "next_step": "", "remaining": [],
                    "fallback": True, "reason": "gate 异常 ⇒ 退回评估器"}
        if gate.get("fallback"):
            return verdict, gate
        if gate.get("done"):
            return {"done": True}, gate
        return {"done": False,
                "next_step": gate.get("next_step") or "继续完成未结待办",
                "remaining": gate.get("remaining") or []}, gate
