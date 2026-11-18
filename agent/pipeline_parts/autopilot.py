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
                    guard.add_llm(resp)
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
        """读 harness 闸配置（三态 mode，2026-11-09）。

        mode 语义：
          · ``off``     —— 完全不介入（**逐字节兼容**旧行为：连 todo_gate 都不读）
          · ``shadow``  —— **只判定、不执行任何工具**，把"若开闸会怎样"写
                           ``harness_decisions`` 供观察（零副作用）
          · ``enforce`` —— 真执行只读验收，未通过则拦住收尾

        兼容：未配 ``harness_mode``（空）时按旧 ``harness_enabled`` 推导 ——
              true→enforce / false→off。
        ★ 为什么非要 shadow 能零执行：旧实现里"观察"也走 `_exec_tool_call`，
          而 verify 池里混着写类（save_artifact/import/requirement_commit）与
          ask_user ⇒ 观察期本身会重复写库、主动弹问。观察必须无副作用。
        """
        try:
            from core import config as _cfg
            _mode = str(_cfg.get("agent", "harness_mode", "") or "").strip().lower()
            if _mode not in ("off", "shadow", "enforce"):
                _mode = "enforce" if _cfg.as_bool("agent", "harness_enabled", False) else "off"
            return {
                "mode": _mode,
                "enabled": _mode != "off",
                "shadow": _mode == "shadow",
                "max_blocks": max(0, int(_cfg.get("agent", "harness_max_blocks", 8) or 0)),
                # 无进展检测：同一批 remaining 连续几次没变 ⇒ 提前放行（默认 2）
                "max_stall": max(1, int(_cfg.get("agent", "harness_max_stall", 2) or 2)),
            }
        except Exception:
            return {"mode": "off", "enabled": False, "shadow": False,
                    "max_blocks": 0, "max_stall": 2}

    # ── P0-2a（2026-11-09）观测：shadow 判定落库（零执行，只写只读统计） ──
    def _verify_stats(self, conversation_id: int) -> dict:
        """统计本会话 pending 待办的**验收可执行性**（只 parse，不执行任何工具）。

        这是 shadow 的核心产出：回答"如果开闸，有多少条能真验、多少条会漏"。
        """
        from agent import harness_gate as _hg
        st = {"pending": 0, "manual": 0, "autoready": 0, "guarded": 0, "tools": {}}
        try:
            from agent.todo_store import list_todos
            from database import db_conn
            with db_conn() as conn:
                rows = list_todos(conn, conversation_id)
        except Exception:
            return st
        for r in rows:
            if r.get("status") != "pending":
                continue
            st["pending"] += 1
            sp = _hg.parse_verify_spec(r.get("verify") or "",
                                       r.get("verify_tool"), r.get("verify_args"))
            if sp["kind"] == "tool":
                st["autoready"] += 1
                st["tools"][sp["tool"]] = st["tools"].get(sp["tool"], 0) + 1
            else:
                st["manual"] += 1
                if sp.get("blocked_reason"):
                    st["guarded"] += 1     # 曾被判成 tool 但被护栏挡下（=修好了的隐患）
        return st

    # ── P1（2026-11-17）编排收口「待办验收如实告知」 ──────────────
    # 为什么不是闸（三个方案都否掉之后剩下的唯一正解）：
    #   ① 子任务按会话自闸 ⇒ 查 0 号会话（已修成 mode='subtask' 不适用）；
    #   ② 子任务按父会话自闸 ⇒ 会因**兄弟子任务**的待办被拦（同批互撞、空转烧额度）；
    #   ③ 子任务按 run 自闸 ⇒ 编排批次里**所有子任务共享同一个 run_id**
    #      （实测 conv 999999002 的 17 条待办全是 run=990231）⇒ 与 ② 同病。
    #   ④ 父收口处拦住重跑 ⇒ 与编排自身的 resume/attempt_count 语义重复。
    # ⇒ 折中：收口时对**本批次**（run 维度，`todo_store.run_todos`）的待办做一次
    #   纯 parse 统计（**零执行、不拦**），有未过验收的就在汇总正文**如实告知**。
    #   与 P0-7「不许无条件说 N 个完成」、B1-5「phase 由任务状态推导」同一条纪律：
    #   不许把没做完的说成做完了。
    def build_orch_todo_notice(self, run_id: int) -> tuple:
        """构造编排收口的待办告知文本。

        :returns: ``(notice, stats)``；notice 为空串 = 本批次待办全部完成（或没建过待办）。
        ⚠️ 纯 parse：只统计"会不会被自动验收"，**不执行任何工具、不拦任何流程**。
        """
        if not int(run_id or 0):
            return "", None
        try:
            from agent.todo_store import run_todos as _rt
            with db_conn() as conn:
                rows = _rt(conn, int(run_id))
        except Exception as e:
            # 读失败不得把编排主流程带崩，也不得编一条"全部完成"的假话 ⇒ 静默跳过
            logger.warning("[autopilot] 编排待办统计失败（跳过告知）：%r", e)
            return "", None
        if not rows:
            return "", None
        from agent import harness_gate as _hg
        st = {"total": len(rows), "done": 0, "pending": 0,
              "manual": 0, "autoready": 0, "guarded": 0, "tools": {}}
        auto_items, manual_items = [], []
        for r in rows:
            if str(r.get("status") or "") == "completed":
                st["done"] += 1
                continue
            st["pending"] += 1
            sp = _hg.parse_verify_spec(r.get("verify") or "",
                                       r.get("verify_tool"), r.get("verify_args"))
            if sp["kind"] == "tool":
                st["autoready"] += 1
                st["tools"][sp["tool"]] = st["tools"].get(sp["tool"], 0) + 1
                auto_items.append(r)
            else:
                st["manual"] += 1
                if sp.get("blocked_reason"):
                    st["guarded"] += 1
                manual_items.append(r)
        if not st["pending"]:
            # 全部完成 ⇒ 正文不打扰，但**统计仍落台账**（否则"编排批次收口验收全过率"
            # 这条趋势只有失败样本，分母缺失 ⇒ 指标失真，与本仓"四个环节各写一个数字"同族）
            self._orch_notice_observe(st)
            return "", st
        _lines = ["", "---", "**待办验收未全过（如实告知）**：本批次共 %d 条待办，%d 条未完成验收。"
                  % (st["total"], st["pending"])]
        for r in auto_items[:3]:
            _lines.append("- 待自动验收：「%s」（%s）" % ((r.get("subject") or "?")[:40],
                                                      r.get("verify_tool") or "?"))
        for r in manual_items[:3]:
            _lines.append("- 需人工确认：「%s」" % (r.get("subject") or "?")[:40])
        if len(auto_items) + len(manual_items) > 6:
            _lines.append("- ……其余 %d 条略" % (len(auto_items) + len(manual_items) - 6))
        _lines.append("（这些待办未阻塞本次交付；如需补跑请在编排卡使用「重跑子任务」）")
        # 台账：让"编排批次收口时还有多少没过验收"可统计（与 shadow/subtask 同一张表）
        self._orch_notice_observe(st)
        return "\n".join(_lines), st

    # ── P1 修复（2026-11-18）编排主路径收口：真正执行只读验收 ──────────
    # 为什么需要：三处编排调用（stream.py 的 resume/团队/常规分支）全部在
    # 收口前 `return`，永远到不了 ReAct 尾部 7062 的 harness 闸 ⇒
    # 编排会话**从未被 enforce 闸验收过**。上一轮的 `build_orch_todo_notice`
    # 只做纯 parse 统计（"有多少条能自动验"），**一条都没有真跑**，
    # 所以仍不是"闸"，只是台账。
    # 本方法的边界（刻意的，避免与编排自身语义打架）：
    #   · 只跑**只读白名单**工具（复用 `_run_verify` 的三重护栏），最多 8 条；
    #   · 不另起 ReAct 循环、不改 agent_tasks 状态 —— 重跑/续跑归编排自己的
    #     resume/attempt_count 管，这里只决定"能不能声称完成"；
    #   · shadow/off 模式**零执行**（与 harness 三态语义一致）。
    def orch_enforce_run(self, run_id: int, conversation_id: int = 0) -> str:
        """编排收口执行只读验收；返回要并入交付正文的告知（无失败为空串）。"""
        try:
            from core import config as _cfg
            _mode = str(_cfg.get("agent", "harness_mode", "") or "").strip().lower()
        except Exception:
            _mode = ""
        if _mode != "enforce" or not int(run_id or 0):
            return ""
        try:
            from agent.todo_store import run_todos as _rt
            with db_conn() as conn:
                rows = _rt(conn, int(run_id))
        except Exception as e:
            logger.warning("[autopilot] 编排收口验收读待办失败（跳过）：%r", e)
            return ""
        from agent import harness_gate as _hg
        _cap = 8
        try:
            _cap = max(0, min(20, int(_cfg.get("agent", "harness_max_blocks", 8) or 8)))
        except Exception:
            pass
        # 父编排实例也要有工具归属上下文（_exec_tool_call 的幂等/留痕会读）
        try:
            self._tool_conv_ctx = int(conversation_id or 0)
            self._tool_run_ctx = int(run_id or 0)
        except Exception:
            pass
        pending = [r for r in rows
                   if str(r.get("status") or "") not in ("completed", "deleted")]
        auto, blocked = [], []
        for r in pending:
            _sp = _hg.parse_verify_spec(r.get("verify") or "", r.get("verify_tool"),
                                        r.get("verify_args"))
            if _sp["kind"] != "tool":
                # parse_verify_spec 已把"结构化但非白名单"判成 manual + blocked_reason
                # ⇒ 必须在 continue 之前捞出来，否则这类验收会被静默放过
                if _sp.get("blocked_reason"):
                    blocked.append((r, _sp.get("tool") or "?"))
                continue                       # 纯自然语言验收：本来就不自动执行
            # ★ 写类/交互类工具不能自动执行；_run_verify 会把它们当"人工验收已确认"
            #   返回 ok=True —— 在**闸**的语境里那就是静默放过。这里提前识别并
            #   单独列出，至少让用户看见"这条没被机器验过"。
            if not _hg.verify_tool_allowed(_sp["tool"]):
                blocked.append((r, _sp["tool"]))
            else:
                auto.append(r)
        auto = auto[:_cap]
        failures = []
        for r in auto:
            try:
                vr = self._run_verify(r.get("verify") or "",
                                      r.get("verify_tool"), r.get("verify_args"))
            except Exception as e:
                vr = {"ok": False, "detail": "验收异常：%r" % (e,)}
            if not vr.get("ok"):
                failures.append("「%s」— %s" % ((r.get("subject") or "?")[:40],
                                                str(vr.get("detail") or "")[:120]))
        # 台账：would_block 表示"这套编排的收口验收有没有不通过"
        try:
            self._harness_observe(int(conversation_id or 0), "orch_enforce",
                                  {"reason": "编排收口执行 %d 条只读验收，%d 条未通过"
                                             % (len(auto), len(failures)),
                                   "remaining": failures},
                                  bool(failures),
                                  {"pending": len(rows), "autoready": len(auto),
                                   "manual": max(0, len(rows) - len(auto))})
        except Exception:
            pass
        if not failures and not blocked:
            return ""
        _lines = []
        if failures:
            _lines += ["", "**★ 编排收口只读验收未通过（不得视为完成）**：%d 条"
                       % len(failures)]
            _lines += ["- " + x for x in failures[:5]]
            if len(failures) > 5:
                _lines.append("- ……其余 %d 条略" % (len(failures) - 5))
        if blocked:
            _lines += ["", "**编排收口：%d 条验收工具不在只读白名单，未自动执行**"
                       % len(blocked)]
            for r, tool in blocked[:3]:
                _lines.append("- 「%s」（%s）— 需人工确认，不算机器验收通过"
                              % ((r.get("subject") or "?")[:40], tool))
        _lines.append("（上述待办仍未通过机器验收；请修复后重跑对应子任务）")
        return "\n".join(_lines)

    def _orch_notice_observe(self, st: dict) -> None:
        """把编排收口的待办统计写入 `harness_decisions`（mode='orch_notice'）。

        ⚠️ 观测设施故障不得变成业务故障（与 `_harness_observe` 同纪律）。
        """
        try:
            self._harness_observe(0, "orch_notice",
                                  {"reason": "编排收口：本批次 %d/%d 条待办未过验收"
                                             % (int((st or {}).get("pending") or 0),
                                                int((st or {}).get("total") or 0))},
                                  False, {"pending": (st or {}).get("pending") or 0,
                                          "manual": (st or {}).get("manual") or 0,
                                          "autoready": (st or {}).get("autoready") or 0,
                                          "guarded": (st or {}).get("guarded") or 0,
                                          "tools": (st or {}).get("tools") or {}})
        except Exception:
            pass

    def _harness_observe(self, conversation_id: int, mode: str, decision: dict,
                         would_block: bool, stats: dict, extra: dict = None) -> None:
        """把一次 harness 判定写入 `harness_decisions`（观测设施故障**不得**变成业务故障）。"""
        try:
            import json as _json
            from database import db_conn
            from core.audit import current_trace_id as _ctid_b1
            _tid_b1 = _ctid_b1()
            with db_conn() as conn:
                conn.execute(
                    "INSERT INTO harness_decisions (conversation_id, mode, would_block, "
                    "force_release, reason, remaining_n, pending_n, manual_n, autoready_n, "
                    "guarded_n, tools_json, trace_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                    (int(conversation_id or 0), str(mode),
                     1 if would_block else 0,
                     1 if (decision or {}).get("force_release") else 0,
                     str((decision or {}).get("reason") or "")[:300],
                     len((decision or {}).get("remaining") or []),
                     int(stats.get("pending") or 0), int(stats.get("manual") or 0),
                     int(stats.get("autoready") or 0), int(stats.get("guarded") or 0),
                     _json.dumps((stats or {}).get("tools") or {}, ensure_ascii=False),
                     _tid_b1,
                     ))
                conn.commit()
        except Exception:
            pass

    def _run_verify(self, verify: str, verify_tool=None, verify_args=None) -> dict:
        """执行一条待办的验收。

        :returns: ``{"ok": bool, "detail": str, "kind": str, "origin": str}``
          · kind="manual" ⇒ 人工验收，**不自动执行**（ok=True，但标注需人工确认）
          · kind="tool"  ⇒ 调该工具，按返回判成败
        ⚠️ 绝不抛异常 —— 验收失败**只是不通过**，不是崩溃。

        ★ 2026-11-09（P0-2a）执行前**三重护栏**（生产事故驱动，见 harness_gate 注释）：
          ① `parse_verify_spec` 只对「结构化 + 参数是 JSON 对象」判 tool；
          ② 工具必须在**只读白名单**内（写类/交互类一律不执行）；
          ③ 这里再判一次白名单（纵深防御：即便上层被绕过，也不执行）。
          结果是「漏拦」变多、「乱调工具」归零 —— 这个取舍是刻意的：
          漏拦只是闸不生效（可观测、可改进），乱调会写库/弹问（不可观测、有副作用）。
        """
        from agent import harness_gate as _hg
        spec = _hg.parse_verify_spec(verify, verify_tool, verify_args)
        if spec["kind"] != "tool":
            _why = spec.get("blocked_reason") or spec.get("parse_error") or ""
            return {"ok": True, "kind": "manual", "origin": spec.get("origin", "manual"),
                    "detail": ("人工验收（不可自动执行，视为已确认）"
                               + ("；%s" % _why if _why else ""))}
        # ②③ 执行前的最后一道：非只读工具绝不执行
        if not _hg.verify_tool_allowed(spec["tool"]):
            return {"ok": True, "kind": "manual", "origin": spec.get("origin", ""),
                    "detail": "工具「%s」不在只读白名单 ⇒ 不执行，按人工验收"
                              % spec["tool"]}
        try:
            res = self._exec_tool_call(spec["tool"], spec["args"])
        except Exception as e:
            return {"ok": False, "kind": "tool", "origin": spec.get("origin", ""),
                    "detail": "验收工具「%s」执行异常：%r" % (spec["tool"], e)}
        ok = bool((res or {}).get("ok"))
        detail = str((res or {}).get("result") or "")[:200]
        return {"ok": ok, "kind": "tool", "origin": spec.get("origin", ""),
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
          首版只有次数上限 8，真机跑出「连续 8 次拦截且待办毫无变化 × 17 秒 ≈ 2.3 分钟，
          结果与立刻放行相同」—— 那是 MAST FM-1.5 的活样本。
          现在比对「本轮 remaining 与上轮是否完全相同」：
          连续 max_stall（默认 2）次没变 ⇒ **提前强制放行**并如实说卡在哪。
        """
        from agent import harness_gate as _hg
        cfg = self._harness_cfg()
        # ── mode=off：连 todo_gate 都不读（逐字节兼容旧行为，且省一次查询）──
        if not cfg["enabled"]:
            return {"block": False, "feedback": "", "force_release": False, "notice": "",
                    "remaining": [], "blocks_used": int(blocks_used or 0),
                    "reason": "harness 闸关闭（mode=off）", "failures": [],
                    "stall_streak": 0, "stalled": False,
                    "mode": "off", "shadow": False, "would_block": False}
        # ── 无会话归属：闸**不适用**，且不得伪装成"跑过了、通过了" ────────
        # 真机证据（2026-11-17 核查）：`harness_decisions` 202 条里有 36 条
        #   `conversation_id=0`，reason 一律「无待办 ⇒ 无验收条件，不拦」；
        #   而**同一时刻**父会话 999999002 正有 17 条 pending 待办
        #   （15:09~15:13 建待办，15:11/15:12/15:13 的判定却记成 conv=0、pending=0）。
        # 根因：编排子任务以 `execute_stream(query, 0, session_conv_id=父会话)` 嵌套调用，
        #   `todo_write` 走 `_tool_conv_ctx`（= session_conv_id）把待办写进**父会话**，
        #   而本闸拿的是裸 `conversation_id`(=0) ⇒ 查 0 号会话 ⇒ `list_todos` 首行即
        #   `if not conversation_id: return []` ⇒ 恒空 ⇒ 恒"不拦"。
        #   ⇒ 这与 `stream.py:5380` 注释里「不要再各处写 int(conversation_id or 0)」
        #     是同一类错误（同一语义两个口径），只是漏在了闸这一处。
        # ⚠️ 为什么不改成传 `session_conv_id or conversation_id`：那会让**子任务**
        #   因**父会话**的待办被拦 —— 同一批 siblings 互相阻塞、反复空转烧额度，
        #   语义也不对（子任务的"本轮"不含父会话待办）。
        #   ⇒ 此处判"不适用"，拦截责任在**父会话收口处**（见 verify_harness_real_effect 的
        #     D 组：编排主路径目前提前 return，尚未经过闸 —— 已知缺口，另行处理）。
        if not int(conversation_id or 0):
            _reason = "无会话归属（编排子任务/未传会话号）⇒ 闸不适用，不代表验收通过"
            self._harness_observe(0, "subtask", {"reason": _reason}, False,
                                  {"pending": 0, "manual": 0, "autoready": 0})
            return {"block": False, "feedback": "", "force_release": False, "notice": "",
                    "remaining": [], "blocks_used": int(blocks_used or 0),
                    "reason": _reason, "failures": [],
                    "stall_streak": 0, "stalled": False,
                    "mode": "subtask", "shadow": False, "would_block": False}
        gate = self._todo_gate(conversation_id)
        # ★ 无进展：本轮 remaining 与上轮完全相同 ⇒ stall_streak + 1
        cur_rem = list((gate or {}).get("remaining") or [])
        try:
            prev = list(prev_remaining or [])
        except Exception:
            prev = []
        cur_streak = (int(stall_streak or 0) + 1) if (prev and prev == cur_rem) else 0

        decision = _hg.harness_decide(gate, blocks_used, cfg["max_blocks"],
                                      harness_enabled=True,   # off 已在上面短路
                                      stall_streak=cur_streak,
                                      max_stall=cfg.get("max_stall", 2))

        # ── mode=shadow：**只判定、零执行**（2026-11-09）──────────────────
        # 为什么不能像 enforce 那样先跑 verify 再丢弃结果：
        #   verify 是用 LLM 自由文本解析来的，写类/交互类工具也在池里
        #   ⇒ "只观察"照样会重复写库、ask_user 主动弹问。
        # 这里只做纯 parse 统计（`_verify_stats` 不调用任何工具），
        # 把「若开闸会拦吗 / 有多少条能真验」写 harness_decisions 供观察。
        if cfg["shadow"]:
            _stats = self._verify_stats(conversation_id)
            self._harness_observe(conversation_id, "shadow", decision,
                                  bool(decision.get("block")), _stats)
            return {"block": False, "feedback": "", "force_release": False, "notice": "",
                    "remaining": decision["remaining"],
                    "blocks_used": int(blocks_used or 0),
                    "reason": "shadow（未执行验收）：%s" % (decision.get("reason") or ""),
                    "failures": [], "stall_streak": cur_streak, "stalled": False,
                    "mode": "shadow", "shadow": True,
                    "would_block": bool(decision.get("block")), "stats": _stats}
        if not decision["block"]:
            notice = ""
            if decision.get("force_release"):
                notice = _hg.build_force_release_notice(
                    decision["remaining"], decision["blocks_used"],
                    decision["max_blocks"])
            # ⚠️ force_release 必须**透传** decision 的值 ——
            #   写死 False 会让"已达上限"这种情况被当成普通放行
            #   （真机 E2E 实测抓到：notice 里有「达到上限 8」但 force_release=False）。
            # ★ 2026-11-17：enforce 的"未拦截"回合**也必须落台账**。
            #   原实现只有真拦时才写行 ⇒ 台账里 enforce 行恒为 0，
            #   "拦截率/误拦率"的分母永远缺失，无法回答"enforce 到底有没有在用"。
            #   与 `orch_notice` 全完成也落台账是同一条纪律：分母不能缺。
            self._harness_observe(conversation_id, "enforce", decision, False,
                                  self._verify_stats(conversation_id))
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
            # ★ 2026-11-09：把**结构化验收字段**一路传下去 ——
            #   只传 verify（自然语言）就等于逼解析器去猜工具名，
            #   而猜错在生成环境已经造成过「未知工具: JSON」空转。
            vr = self._run_verify(r.get("verify") or "",
                                  r.get("verify_tool"), r.get("verify_args"))
            if not vr.get("ok"):
                failures.append("%s —— %s" % (r.get("subject") or "?", vr.get("detail") or ""))
        if not failures:
            # ★ 所有 verify 都过了 ⇒ 待办其实已完成（可能是刚被模型置完成）
            #   ⇒ 不该拦，重新判一次。
            gate2 = self._todo_gate(conversation_id)
            decision2 = _hg.harness_decide(gate2, blocks_used, cfg["max_blocks"],
                                           harness_enabled=True,
                                           stall_streak=cur_streak,
                                           max_stall=cfg.get("max_stall", 2))
            if not decision2["block"]:
                notice = ""
                if decision2.get("force_release"):
                    notice = _hg.build_force_release_notice(
                        decision2["remaining"], decision2["blocks_used"],
                        decision2["max_blocks"])
                # 同上：验收实际已过 ⇒ 这是"该拦但重判后放行"的样本，也是分母的一部分
                self._harness_observe(conversation_id, "enforce", decision2, False,
                                      self._verify_stats(conversation_id))
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
        # enforce 也落一条观测（便于与 shadow 段直接对比，而不是靠猜）
        self._harness_observe(conversation_id, "enforce", decision, True,
                              self._verify_stats(conversation_id))
        return {"block": True, "feedback": feedback, "force_release": False,
                "notice": "", "remaining": decision["remaining"],
                "blocks_used": decision["blocks_used"] + 1,
                "reason": decision["reason"], "failures": failures,
                "stall_streak": cur_streak, "stalled": False,
                "mode": "enforce", "shadow": False, "would_block": True}
