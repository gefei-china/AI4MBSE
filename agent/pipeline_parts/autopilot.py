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

    # ── P0-2（2026-10-28）harness 级强制闸 ───────────────────────
    # 方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.3.2 / §6 第 2 步。
    # 对齐官方 Stop hook："the turn doesn't end until the typecheck passes" ——
    # 由 **harness** 执行，**不依赖模型配合**。
    #
    # 判据只有一处、IO 只有一处 —— 与 P0-1 同样的纪律（不各写一份）。

    def _harness_cfg(self) -> dict:
        """读 harness 闸配置（全部默认关/兼容值 ⇒ 不改变现有行为）。"""
        try:
            from core import config as _cfg
            return {
                "enabled": _cfg.as_bool("agent", "harness_enabled", False),
                "max_blocks": max(0, int(_cfg.get("agent", "harness_max_blocks", 8) or 0)),
                # 无进展检测：同一批 remaining 连续几次没变 ⇒ 提前放行（默认 2）
                "max_stall": max(1, int(_cfg.get("agent", "harness_max_stall", 2) or 2)),
            }
        except Exception:
            return {"enabled": False, "max_blocks": 0, "max_stall": 2}

    def _run_verify(self, verify: str) -> dict:
        """执行一条待办的验收。

        :returns: ``{"ok": bool, "detail": str, "kind": str}``
          · kind="manual" ⇒ 人工验收，**不自动执行**（ok=True，但标注需人工确认）
          · kind="tool"  ⇒ 调该工具，按返回判成败
        ⚠️ 绝不抛异常 —— 验收失败**只是不通过**，不是崩溃。
        """
        from agent import harness_gate as _hg
        spec = _hg.parse_verify_spec(verify)
        if spec["kind"] != "tool":
            return {"ok": True, "kind": "manual",
                    "detail": "人工验收（不可自动执行，视为已确认）"}
        try:
            res = self._exec_tool_call(spec["tool"], spec["args"])
        except Exception as e:
            return {"ok": False, "kind": "tool",
                    "detail": "验收工具「%s」执行异常：%r" % (spec["tool"], e)}
        ok = bool((res or {}).get("ok"))
        detail = str((res or {}).get("result") or "")[:200]
        return {"ok": ok, "kind": "tool",
                "detail": ("验收工具「%s」通过：%s" % (spec["tool"], detail)
                           if ok else
                           "验收工具「%s」未通过：%s" % (spec["tool"], detail))}

    def _harness_gate(self, conversation_id: int, blocks_used: int,
                      prev_remaining=None, stall_streak: int = 0) -> dict:
        """跑一次 harness 强制闸判定 + 实际执行 verify。

        :param blocks_used: 已连续阻断次数
        :param prev_remaining: **上一轮**的 remaining（用于无进展检测）
        :param stall_streak: 已连续"无进展"次数
        :returns: ``{"block": bool, "feedback": str, "force_release": bool,
                     "notice": str, "remaining": [...], "blocks_used": int,
                     "reason": str, "failures": [...], "stall_streak": int,
                     "stalled": bool}``

        ★ 无进展检测（真机实测驱动，2026-10-28）：
          首版只有次数上限 8，真机跑出「原地打转 8 次 × 17 秒 ≈ 2.3 分钟，
          结果与立刻放行相同」—— 那是 MAST FM-1.5 的活样本。
          现在比对「本轮 remaining 与上轮是否完全相同」：
          连续 max_stall（默认 2）次没变 ⇒ **提前强制放行**并如实说卡在哪。
        """
        from agent import harness_gate as _hg
        cfg = self._harness_cfg()
        gate = self._todo_gate(conversation_id)
        # ★ 无进展：本轮 remaining 与上轮完全相同 ⇒ stall_streak + 1
        cur_rem = list((gate or {}).get("remaining") or [])
        try:
            prev = list(prev_remaining or [])
        except Exception:
            prev = []
        cur_streak = (int(stall_streak or 0) + 1) if (prev and prev == cur_rem) else 0

        decision = _hg.harness_decide(gate, blocks_used, cfg["max_blocks"],
                                      harness_enabled=cfg["enabled"],
                                      stall_streak=cur_streak,
                                      max_stall=cfg.get("max_stall", 2))
        if not decision["block"]:
            notice = ""
            if decision.get("force_release"):
                notice = _hg.build_force_release_notice(
                    decision["remaining"], decision["blocks_used"],
                    decision["max_blocks"])
            # ⚠️ force_release 必须**透传** decision 的值 ——
            #   写死 False 会让"已达上限"这种情况被当成普通放行
            #   （真机 E2E 实测抓到：notice 里有「达到上限 8」但 force_release=False）。
            return {"block": False, "feedback": "",
                    "force_release": bool(decision.get("force_release")),
                    "notice": notice, "remaining": decision["remaining"],
                    "blocks_used": decision["blocks_used"],
                    "reason": decision["reason"], "failures": [],
                    "stall_streak": cur_streak,
                    "stalled": bool(decision.get("stalled"))}

        # ★ 该拦 ⇒ 实际跑一遍未完成待办的 verify（判据是**执行结果**，不是 LLM 判断）
        failures = []
        try:
            from agent.todo_store import list_todos
            from database import db_conn
            with db_conn() as conn:
                rows = list_todos(conn, conversation_id)
        except Exception as e:
            rows = []
            failures.append("读待办失败：%r" % (e,))
        for r in rows:
            if r.get("status") != "pending":
                continue
            vr = self._run_verify(r.get("verify") or "")
            if not vr.get("ok"):
                failures.append("%s —— %s" % (r.get("subject") or "?", vr.get("detail") or ""))
        if not failures:
            # ★ 所有 verify 都过了 ⇒ 待办其实已完成（可能是刚被模型置完成）
            #   ⇒ 不该拦，重新判一次。
            gate2 = self._todo_gate(conversation_id)
            decision2 = _hg.harness_decide(gate2, blocks_used, cfg["max_blocks"],
                                           harness_enabled=cfg["enabled"],
                                           stall_streak=cur_streak,
                                           max_stall=cfg.get("max_stall", 2))
            if not decision2["block"]:
                notice = ""
                if decision2.get("force_release"):
                    notice = _hg.build_force_release_notice(
                        decision2["remaining"], decision2["blocks_used"],
                        decision2["max_blocks"])
                return {"block": False, "feedback": "",
                        "force_release": bool(decision2.get("force_release")),
                        "notice": notice, "remaining": decision2["remaining"],
                        "blocks_used": decision2["blocks_used"],
                        "reason": "重判后已通过验收：" + decision2["reason"],
                        "failures": [], "stall_streak": 0,
                        "stalled": bool(decision2.get("stalled"))}
        feedback = _hg.build_block_feedback(
            decision["remaining"], decision["blocks_used"] + 1,
            decision["max_blocks"], failures)
        return {"block": True, "feedback": feedback, "force_release": False,
                "notice": "", "remaining": decision["remaining"],
                "blocks_used": decision["blocks_used"] + 1,
                "reason": decision["reason"], "failures": failures,
                "stall_streak": cur_streak, "stalled": False}
