# -*- coding: utf-8 -*-
"""自主续跑（auto-continue）—— 纯函数模块（2026-10-17，米爸「harness 持续工作」需求）。

为什么需要这个模块
------------------
米爸反馈：「每执行一轮都会结束，让我继续下达指令才会进行下一步。作为一款可以长时间
工作的 harness 平台，这一点必须支持。」

标杆调研结论（2026-10-17）：
· Claude Code（Agent SDK）：ReAct 循环**由模型自己决定何时停**（不再请求工具即收尾）；
  对长任务用 `/goal` —— 模型每次想停时，由**评估器（evaluator）**按目标判据决定放行
  还是打回重做，直到目标达成或轮次上限。
· Devin：Plan→Act→Observe→Correct 递归循环；失败模式是「loop of death」
  （同一修复反复失败）与「cost with no ceiling」（无上限烧钱）。
· AutoGPT/BabyAGI 四大失败模式：① 无完成度判据的无限任务生成；② 记忆汤；
  ③ 误差复利；④ 无上限成本。沉淀下来的设计铁律：
  **「run until done」→「run within limits」——循环的每个维度都要有天花板。**

本模块把三件事做成纯函数（调用方只做 IO，判定零副作用、可单测/可变异自证）：
1. **完成度评估**的消息构造与判据解析（对齐 /goal evaluator）；
2. **续跑决策** `decide()` —— 次数/墙钟/token 预算/空转四道闸，全部 fail-safe 向「停」；
3. **续跑指令** `continuation_prompt()` —— 注入 messages 驱动下一轮 ReAct。

为什么 fail-safe 方向是「停止」：无约束续跑比提前停止危险得多——前者烧钱且可能死循环，
后者用户补一句「继续」即可恢复。评估器故障/解析失败/结论不完整 ⇒ 一律不续跑。

为什么空转检测用「规范化后完全相同」判据：AutoGPT 失败模式①的对策。
相等即停（宁可早停一晚续），包含即停会把「分析 A」/「分析 A 的依赖」误判为空转。
"""
import json
import re

__all__ = ["EVAL_SYS", "AUTO_CONTINUE_STAGE", "AUTO_EVAL_STAGE",
           "build_eval_messages", "parse_verdict", "normalize_step",
           "is_spinning", "decide", "continuation_prompt", "tools_brief"]

#: 前端 proc 时间线环节名（11-pipeline.js onStageEvent 按此渲染 🔄 块）
AUTO_CONTINUE_STAGE = "自主续跑"
AUTO_EVAL_STAGE = "任务完成度评估"

#: 评估器 system prompt。核心是**保守判据**：无法判断 ⇒ done=true（不续跑）。
EVAL_SYS = (
    "你是任务完成度评估器（evaluator）。唯一职责：判断 AI 是否已完整完成用户的原始请求。"
    "只依据给定信息判断，不臆测未提供的事实，不苛求锦上添花的润色。\n"
    "判定规则：\n"
    "1. 用户请求中的每项要求都已满足，或剩余工作只是可选润色/增强 ⇒ done=true；\n"
    "2. 存在用户请求范围内、明确尚未执行的下一步 ⇒ done=false，"
    '并给出具体的一句话 next_step 与 remaining 待办列表；\n'
    "3. 信息不足、无法判断，或请求本身是一次性问答/闲聊 ⇒ done=true"
    "（宁可把决定权交还用户，也不擅自续跑）。\n"
    "只输出一行 JSON，禁止任何其他文字：\n"
    '{"done": true}\n'
    '或 {"done": false, "next_step": "下一步做什么（一句话，具体到对象）", '
    '"remaining": ["待办1", "待办2"]}'
)

#: 评估器可见的答复截断（控 token：判完成度不需要全文）
_EVAL_ANSWER_CAP = 2000
#: 工具清单每条结果截断
_EVAL_TOOL_RESULT_CAP = 120
#: 工具清单最多带几条（防 exec_tools 很长时评估 prompt 膨胀）
_EVAL_TOOLS_MAX = 12


def build_eval_messages(user_input: str, tools_brief: str, answer: str,
                        agent_name: str = "") -> list:
    """构造评估器消息（system + user 两条，无工具、非流式由调用方发）。"""
    who = f"（执行 Agent：{agent_name}）" if agent_name else ""
    user = (
        f"【用户原始请求】\n{user_input or '（空）'}\n\n"
        f"【AI 已执行的工具调用】\n{tools_brief or '（本轮未调用工具）'}\n\n"
        f"【AI 的最终答复】\n{(answer or '')[:_EVAL_ANSWER_CAP]}\n\n"
        f"请判定{who}用户的原始请求是否已被完整完成。只输出 JSON。"
    )
    return [{"role": "system", "content": EVAL_SYS},
            {"role": "user", "content": user}]


def parse_verdict(text) -> dict | None:
    """解析评估器输出为 verdict。

    :returns: ``{"done": True}`` / ``{"done": False, "next_step": ..., "remaining": [...]}``
              / None（任何形态不符 ⇒ None，调用方按「不续跑」处理，fail-safe）。
    ⚠️ done=false 但缺 next_step ⇒ None：结论不完整就无法驱动续跑，
       硬续等于让模型自由发挥，正是 AutoGPT 失败模式①的温床。
    """
    if not text:
        return None
    t = str(text).strip()
    if t.startswith("```"):                      # 容忍 ```json 围栏
        t = t.strip("`").strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
    except Exception:
        return None
    if not isinstance(obj, dict) or "done" not in obj:
        return None
    if bool(obj.get("done")):
        return {"done": True}
    ns = str(obj.get("next_step") or "").strip()
    if not ns:
        return None
    rem = obj.get("remaining")
    rem = [str(x).strip()[:120] for x in rem if str(x).strip()] if isinstance(rem, list) else []
    return {"done": False, "next_step": ns[:200], "remaining": rem[:10]}


#: 规范化时要剥掉的标点/空白（空转检测对「。/.」差异不敏感）
_STEP_NOISE = "。，、．,.;；:：\n\t “”\"'‘’()（）[]【】"


def normalize_step(s) -> str:
    """步骤描述规范化：小写 + 去标点空白。仅用于空转检测的相等比较。"""
    t = str(s or "").strip().lower()
    for ch in _STEP_NOISE:
        t = t.replace(ch, "")
    return t


def is_spinning(next_step: str, seen_steps) -> bool:
    """空转检测：规范化后与**历史任一步骤完全相同** ⇒ 判定空转。

    为什么只要「完全相同」：这是 AutoGPT「同一修复反复失败」的直接对策。
    误判方向权衡：漏判（继续一轮）只浪费一轮预算；误判（早停）用户补一句「继续」
    即可恢复 ⇒ 判据必须窄，宁漏勿误。
    """
    ns = normalize_step(next_step)
    if not ns:
        return False
    return any(ns == normalize_step(x) for x in (seen_steps or []))


def decide(verdict, *, used: int, max_used: int, seen_steps,
           deadline_left_s=None, budget_left: bool = True):
    """纯决策：这一轮工作结束后，要不要自主续跑。

    :returns: ``(continue: bool, reason: str)`` —— reason 供 stage/reasoning 透传。
    四道闸（全部对齐 AutoGPT boundedness 教训）：
      ① 次数闸 ``max_used``；② 墙钟闸 ``deadline_left_s``；
      ③ token 预算闸 ``budget_left``；④ 空转闸 ``is_spinning``。
    任何输入异常/缺失都走 fail-safe ⇒ ``(False, ...)``。
    """
    if not verdict:
        return False, "完成度评估失败或无结论，按不续跑处理"
    if verdict.get("done"):
        return False, "任务已完成，无需续跑"
    if used >= max_used:
        return False, f"自主续跑次数已达上限（{max_used} 次）"
    if deadline_left_s is not None and deadline_left_s <= 0:
        return False, "自主续跑墙钟时限已到"
    if not budget_left:
        return False, "token 预算已耗尽，停止续跑"
    ns = str(verdict.get("next_step") or "").strip()
    if not ns:
        return False, "评估结论缺少下一步描述，按不续跑处理"
    if is_spinning(ns, seen_steps):
        return False, "剩余工作与此前步骤相同，判定空转，停止续跑"
    return True, f"任务未完成，自动继续：{ns}"


def continuation_prompt(user_input: str, verdict: dict, round_no: int) -> str:
    """续跑指令（注入 messages 的下一条 user 消息，驱动 ReAct 再转）。"""
    rem = verdict.get("remaining") or []
    rem_txt = "\n".join(f"- {x}" for x in rem) if rem else "（评估器未列明）"
    return (
        f"【自主续跑 · 第 {round_no} 次】系统完成度评估：你的请求尚未完成，现自动继续，无需用户确认。\n"
        f"【用户原始请求】{user_input or '（空）'}\n"
        f"【下一步】{verdict.get('next_step') or ''}\n"
        f"【剩余事项】\n{rem_txt}\n"
        f"【要求】直接继续执行：调用工具获取信息，或直接产出内容。"
        f"不要重复已完成的工作，不要复述上文，不要询问用户是否继续。"
        f"若确实遇到无法独自解决的问题（缺关键信息/需用户拍板/不可解错误），"
        f"调用 ask_user 工具向用户澄清，或说明阻塞原因后收尾。"
    )


def tools_brief(exec_tools, per_tool_cap: int = _EVAL_TOOL_RESULT_CAP,
                max_tools: int = _EVAL_TOOLS_MAX) -> str:
    """把 exec_tools 压成评估器可读的简短清单（工具名 + 成败 + 结果开头）。

    为什么截断：评估只需要知道「做了什么、成没成」，不需要全文；
    长清单还会把评估 prompt 撑爆（它每轮都要跑一次，成本敏感）。
    """
    rows = []
    for t in (exec_tools or [])[-max_tools:]:
        nm = str(t.get("name") or "?")
        ok = "成功" if t.get("ok") else "失败"
        r = re.sub(r"\s+", " ", str(t.get("result") or ""))[:per_tool_cap]
        rows.append(f"- {nm}（{ok}）：{r}")
    return "\n".join(rows)
