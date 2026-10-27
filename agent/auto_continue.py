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
           "is_spinning", "decide", "continuation_prompt", "tools_brief",
           "looks_awaiting_permission", "has_continue_cue", "clarify_from_verdict"]

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
    "3. **必须由用户拍板才能继续**（有多个合理方案需二选一、动作影响面大需授权、"
    "缺少只有用户才知道的关键信息）⇒ done=false 且 needs_user=true，"
    "并给出 question（一句话问清要选什么）与 options（2-6 个候选，取自答复里的方案原文）；\n"
    "4. 信息不足、无法判断，或请求本身是一次性问答/闲聊 ⇒ done=true"
    "（宁可把决定权交还用户，也不擅自续跑）。\n"
    "⚠️ 判「needs_user」的门槛要严：只有**继续做下去会做错事/做无用功**时才算。"
    "「先查一下再决定」这类**自己能查清的**不属此列（那是 next_step，不是 needs_user）。\n"
    "只输出一行 JSON，禁止任何其他文字：\n"
    '{"done": true}\n'
    '或 {"done": false, "next_step": "下一步做什么（一句话，具体到对象）", '
    '"remaining": ["待办1", "待办2"]}\n'
    '或 {"done": false, "needs_user": true, "question": "要用户选什么？", '
    '"options": ["方案A", "方案B"]}'
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
    # ★ 2026-10-27：`needs_user` —— "必须由用户拍板才能继续"。
    #   这条判定存在的意义：把"模型在正文里问用户"变成**真正的澄清卡片**。
    #   缺失它时，模型只能自言自语"需要你确认…"然后结束（用户看到的现象：
    #   "非要终止后再让我输入确认，新发起一轮对话"）。
    if obj.get("needs_user"):
        q = str(obj.get("question") or "").strip()
        if not q:
            return None          # 要拍板却说不清问什么 ⇒ 结论不完整，按不续跑处理
        opts = obj.get("options")
        opts = ([str(x).strip()[:60] for x in opts if str(x).strip()]
                if isinstance(opts, list) else [])
        # 少于 2 个候选做不成"选择题"，卡片会退化成纯输入框 —— 仍可接受（前端有自定义输入）
        return {"done": False, "needs_user": True, "question": q[:200],
                "options": opts[:6]}
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


# ══════════════════════════════════════════════════════════════════════════════
# 2026-10-27（米爸两问）：「为啥不自己跑完」+「要澄清为啥不弹卡片，非要我另起一轮」
#
# 真库现场（conv 9629，用户「继续写入工程」那一轮）：
#   · `tool_call_logs` **零条 tool**（只有 2 条技能注入）⇒ `exec_tools` 为空；
#   · `_is_continuation_input("继续写入工程")` = **False**（该函数要求整句就是裸续作短语）；
#   ⇒ 自主续跑闸门 `(exec_tools or _cont_input)` **两个都不满足** ⇒ 评估器**没跑**
#   ⇒ 模型那句"下一轮我先用名称检索「测试D」"没人执行，用户只能自己再发一句。
#   而它同一轮结尾写的是"需要你确认一件事：先删掉再重来，还是留着？"——
#   纯文本提问 ⇒ **不产生澄清卡** ⇒ 用户看到的就是"终止了，让我另起一轮确认"。
#
# 两个判据都刻意做成"宽于 `_is_continuation_input`"的**闸门信号**（只用于"要不要评估"），
# 不改后者语义 —— 后者还兼着"注入续作指令"的职责，收窄/放宽都会改变主路径产出。
# ══════════════════════════════════════════════════════════════════════════════

#: 用户输入里的"接着干"信号：以续作动词**开头**即可（不要求整句是裸短语）。
#: 与 `history._is_continuation_input` 的分工：
#:   后者=「整句只是'继续'」⇒ 用于注入【续作指令】；
#:   本函数=「这轮本来就在接着干」⇒ 用于**触发完成度评估**。
#: 例：「继续写入工程」「接着把剩下的建完」「往下做」「按上面的方案落地」都算。
_CONT_CUE_PREFIX = (
    "继续", "接着", "往下", "再往下", "下一步", "按上面", "按上述", "按你说的",
    "按你的建议", "就按", "照这个", "照上述", "重试", "重跑", "再来",
    "continue", "go on", "keep going", "next",
)

#: 答复结尾"在征求许可/等拍板"的强标记（出现在末尾片段里即算）
_AWAIT_MARKERS = (
    "需要你确认", "需要您确认", "请你确认", "请您确认", "等你确认", "等您确认",
    "请允许我", "允许我", "要我继续", "需要我继续", "是否继续", "要不要继续",
    "需要你拍板", "需要您拍板", "请你拍板", "需要你决定", "需要你选择",
    "确认后我", "确认后我再", "你点头", "您点头", "请指示", "等你回复", "等您回复",
    "需要你先", "需要您先", "请你先", "请您先",
)

#: 末尾片段里出现"选择型问句"的标记（配合问号判定）
_CHOICE_MARKERS = ("还是", "是否", "要不要", "哪个", "哪一种", "二选一")


def looks_awaiting_permission(answer, tail_chars: int = 0) -> bool:
    """答复是否**在征求许可/等待拍板**（而任务其实没做完，只是停下来了）。

    ★ 2026-10-27 第一版只看"末尾 700 字"，在真实答复上**判不出来**（实测 conv 9629
    msg 4979：4330 字，末尾是"附：待写入的模型代码"整段 ```sysml``` 代码块，
    把真正那句"需要你确认一件事…先删掉再重来，还是留着？"挤到 3500 字之前）。
    ⇒ 改为：**先剥掉围栏代码块**（附录性质，且里面可能合法地含这类词），
      再在剩余**正文**里判。正文里出现强标记就足以说明"它在等你点头"。

    判定任一成立即可：
      ① 正文含强标记（需要你确认 / 请允许我 / 是否继续 / 你点头 …）；
      ② 正文最后一句是问句且含选择型词（还是 / 是否 / 要不要 / 哪个）；
      ③ 传了 `tail_chars>0` 时，只在该长度窗口内判 ①②（调用方可收紧）。

    误判方向的权衡：**宁可多判** —— 误判只会多跑一次评估器（小模型一次调用，
    且评估器自己会说 done=true 而不续跑）；漏判的代价是用户必须自己再发一句
    「继续」，正是本次要修的现象。
    """
    t = str(answer or "").strip()
    if not t:
        return False
    import re as _re
    prose = _re.sub(r"```[\s\S]*?```", "", t)          # 剥围栏代码块（含未闭合的情况）
    prose = _re.sub(r"~~~[\s\S]*?~~~", "", prose)
    if not prose.strip():
        return False
    seg = prose[-max(200, int(tail_chars)):] if tail_chars and tail_chars > 0 else prose
    if any(m in seg for m in _AWAIT_MARKERS):
        return True
    # ② 末尾 400 字内存在「选择型问句」。
    #   ★ 不用"最后一句以问号结尾"：实测 msg 4928 是
    #     「…还是就用现有的平台语法检查…？我倾向前者——语义错在写入前暴露…」
    #     —— 问句后面跟了一句倾向说明，末句就不再以问号结尾 ⇒ 漏判。
    #   判据放宽成"窗口内有问句 + 选择型词"，因为**评估器是最终裁决者**：
    #   多判只会多一次小调用（它自己会说 done=true），漏判才是用户要自己再发一句。
    win = prose[-400:]
    for line in win.replace("！", "。").replace("!", "。").split("。"):
        if ("？" in line or "?" in line) and any(m in line for m in _CHOICE_MARKERS):
            return True
    return False


def has_continue_cue(user_input) -> bool:
    """用户这轮是否在说"接着干"（比裸续作短语更宽，见文件末注释）。

    只用于**触发完成度评估**，不改变任何主路径行为。
    """
    t = str(user_input or "").strip().lower()
    if not t:
        return False
    return any(t.startswith(p) for p in _CONT_CUE_PREFIX)


def clarify_from_verdict(verdict, qid: str = "auto_continue") -> dict | None:
    """把 `{"needs_user": True, ...}` 的评估结论转成**澄清卡的一道题**。

    返回 `{id, question, options, allow_custom}`（前端 `clarifyCardInnerHtml` 认的形状）；
    非 needs_user 结论 ⇒ None（调用方按原逻辑处理）。
    """
    if not isinstance(verdict, dict) or not verdict.get("needs_user"):
        return None
    q = str(verdict.get("question") or "").strip()
    if not q:
        return None
    opts = [str(x).strip()[:60] for x in (verdict.get("options") or []) if str(x).strip()]
    return {"id": qid, "question": q[:200],
            "options": opts[:6], "allow_custom": True}
