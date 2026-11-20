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
           "looks_awaiting_permission", "has_continue_cue", "clarify_from_verdict",
           "default_progress_verdict", "build_degrade_notice",
           "merge_round_content", "_code_blocks", "_ngram_sim", "_ngram_dice",
           "strip_json_blocks", "is_content_repeating"]

#: 前端 proc 时间线环节名（11-pipeline.js onStageEvent 按此渲染 🔄 块）
AUTO_CONTINUE_STAGE = "自主续跑"
AUTO_EVAL_STAGE = "任务完成度评估"

#: 评估器 system prompt。核心是**保守判据**：无法判断 ⇒ done=true（不续跑）。
EVAL_SYS = (
    "你是任务完成度评估器（evaluator）。唯一职责：判断 AI 是否已完整完成用户的原始请求。"
    "只依据给定信息判断，不臆测未提供的事实。\n"
    "判定规则：\n"
    "1. 用户请求中的每项要求都已满足，或剩余工作只是可选润色/增强 ⇒ done=true；\n"
    "   ⚠️「可选润色/增强」**仅限**：措辞、排版、补充举例说明。\n"
    "   **任何需要调用工具才能完成的事都不算可选增强**（如查数据、跑校验、导出文件、"
    "删除对象、提交变更）—— 那属于规则 2，必须判 done=false。\n"
    "2. 存在用户请求范围内、明确尚未执行的下一步 ⇒ done=false，"
    '并给出具体的一句话 next_step 与 remaining 待办列表；\n'
    "3. **必须由用户拍板才能继续** ⇒ done=false 且 needs_user=true，"
    "并给出 question（一句话问清要选什么）与 options（2-6 个候选，取自答复里的方案原文），"
    "**同时必须给 needs_kind 二选一**：\n"
    "   · ``\"user_only\"`` —— 这条信息**只有用户有**（如工况口径、业务规则、"
    "用户偏好、外部系统的取值），不问就一定做错；\n"
    "   · ``\"has_default\"`` —— 有**合理默认值**（按上下文/惯例/最常见选择即可推进），"
    "问了只是让用户多点一次；此时必须给 default（你打算按什么默认值做）。\n"
    "   ⚠️ 拿不准选哪个时倾向 ``has_default`` —— 做了再让用户改，比停下来等更好。\n"
    "4. 信息不足、无法判断，或请求本身是一次性问答/闲聊 ⇒ done=true"
    "（宁可把决定权交还用户，也不擅自续跑）。\n"
    "⚠️ 判「needs_user」的门槛要严：只有**继续做下去会做错事/做无用功**时才算。"
    "「先查一下再决定」这类**自己能查清的**不属此列（那是 next_step，不是 needs_user）。\n"
    "\n"
    "【硬信号：任一条命中 ⇒ done=false，且不受规则 1 的「可选增强」例外保护】\n"
    "a. 答复把该做的事写成**建议/可以/下一步应/若需要请**等**未执行的动作**"
    "（例：「建议下一步执行校验」——校验没做，就是没完成）；\n"
    "b. 答复把工作**转交**给其他 Agent / 其他系统 / 用户自己去操作"
    "（例：「请转交对应 Agent 处理」——它没做，就是没完成）；\n"
    "c. 答复以征求意见 / 等拍板结尾，而该问题**用现有工具就能自行查明**"
    "（那是 next_step，不是 needs_user）；\n"
    "d. 答复自述**尚未校验 / 尚未验证 / 未测试**（自己说没验证，就是没完成）。\n"
    "\n"
    "【反向澄清：以下情形**不**算未完成，别把它误判成硬信号】\n"
    "e. 当用户请求本身就是「检查 / 评审 / 评估 / 判定 / 比对」某份内容时，"
    "答复里的「不合格 / 不通过 / 待核实 / 建议修改 / 缺属性 / 需回查」等，"
    "都是**判定结论本身**（用户要的恰恰就是这个结论），**不是** AI 自己欠下的活"
    " ⇒ 此时只要判定已给出、依据已写出，就判 done=true。\n"
    "   只有「AI 自己还得再做一步」（去查、去跑校验、去导出、去写库、去生成文件）"
    "才算未完成 —— 这与规则 1「需要调用工具才能完成」同义，不是新例外。\n"
    "   ⚠️ 反过来：用户若要求「检查**并写入 / 并修复**」，那「尚未写入 / 尚未修复」"
    "仍属未完成（硬信号照旧适用）。\n"
    "\n"
    "只输出一行 JSON，禁止任何其他文字：\n"
    '{"done": true}\n'
    '或 {"done": false, "next_step": "下一步做什么（一句话，具体到对象）", '
    '"remaining": ["待办1", "待办2"]}\n'
    '或 {"done": false, "needs_user": true, "needs_kind": "user_only", '
    '"question": "要用户选什么？", "options": ["方案A", "方案B"]}\n'
    '或 {"done": false, "needs_user": true, "needs_kind": "has_default", '
    '"question": "按哪个口径做？", "default": "按最常见的 X 口径", '
    '"options": ["方案A", "方案B"]}'
)

#: 评估器可见的答复截断（控 token：判完成度不需要全文）
_EVAL_ANSWER_CAP = 2000
#: 工具清单每条结果截断
_EVAL_TOOL_RESULT_CAP = 120
#: 工具清单最多带几条（防 exec_tools 很长时评估 prompt 膨胀）
_EVAL_TOOLS_MAX = 12


def strip_json_blocks(text: str) -> str:
    """剥掉 ```json 围栏块（结构化结果是**给系统解析的**，不是"给人看的答复"）。

    ★ 为什么评估器必须先剥再看（2026-11-15 真机实证）：
      `_EVAL_ANSWER_CAP = 2000` 会把答复截到前 2000 字符，而两段式输出下
      这 2000 字符 = 结论摘要（约 480 字）+ **半截 JSON**（正切在对象中间）。
      ⇒ 评估器读到的是「一段散文 + 一堆 JSON 残片」，JSON 残片对它纯属噪音，
      真正的答复（散文结论）反而被挤到只剩开头。
      剥掉 JSON 后再截断，评估器拿到的是**完整的结论摘要**。

    ⚠️ 只剥 ```json：其它语言的代码块**必须保留** —— 那是交付物本身
      （"我已经把代码写出来了"本身就是"做完了"的证据）。
    ⚠️ 剥完若整篇为空（答复本来就是纯 JSON 的旧形态）⇒ 调用方回退用原文，
      不能让评估器什么都看不到。
    """
    out, buf, inside, keep, lang = [], [], False, False, ""
    for line in str(text or "").split("\n"):
        s = line.strip()
        if s.startswith("```"):
            if inside:
                # 围栏正常结束：保留的围栏连标记一起还原，被剥的整块丢弃
                if keep:
                    out.append("```" + lang)
                    out.extend(buf)
                    out.append("```")
                inside = False
                buf = []
                keep = False
                lang = ""
                continue
            lang = s[3:].strip()
            inside = True
            buf = []
            keep = (lang.lower() != "json")
            continue
        if inside:
            buf.append(line)
            continue
        out.append(line)
    # ⚠️ 围栏未闭合（模型被截断）：把残余内容**原样吐出** ——
    #   宁可让评估器多看一点残片，也不能把正文吞掉。
    if inside:
        out.append("```" + lang)
        out.extend(buf)
    return "\n".join(out).strip()


def build_eval_messages(user_input: str, tools_brief: str, answer: str,
                        agent_name: str = "") -> list:
    """构造评估器消息（system + user 两条，无工具、非流式由调用方发）。"""
    who = f"（执行 Agent：{agent_name}）" if agent_name else ""
    # ★ 先剥结构化 JSON 再截断（见 strip_json_blocks 的注释）；
    #   剥完为空 ⇒ 回退原文（旧形态纯 JSON 的答复不能被剥成空）。
    _ans = str(answer or "")
    _stripped = strip_json_blocks(_ans)
    _ans = (_stripped or _ans)
    user = (
        f"【用户原始请求】\n{user_input or '（空）'}\n\n"
        f"【AI 已执行的工具调用】\n{tools_brief or '（本轮未调用工具）'}\n\n"
        f"【AI 的最终答复】\n{_ans[:_EVAL_ANSWER_CAP]}\n\n"
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
        # ★ P1-2（2026-10-28）needs_user 二分：区分「只有用户有」与「有默认值可推进」。
        #   背景（实测）：6/89 次评估落在 needs_user ⇒ 全转澄清卡 ⇒ 用户感知"停"。
        #   其中相当一部分其实有合理默认值（如"两个口径"，任选一个都能推进）。
        #   对齐 CISA「审批决策由设计者定」与 D-6 纪律「可独立判断时直接给结论」。
        #
        #   ⚠️ 缺失 needs_kind 时**保守按 user_only**（转卡）——
        #      默认"多问一次"比默认"猜着做"安全；这个方向不为了少弹卡而猜。
        kind = str(obj.get("needs_kind") or "").strip().lower()
        if kind not in ("user_only", "has_default"):
            kind = "user_only"
        dflt = str(obj.get("default") or "").strip()[:200]
        # has_default 但没说默认值 ⇒ 无法"按默认推进" ⇒ 退回 user_only（转卡）
        if kind == "has_default" and not dflt:
            kind = "user_only"
        # 少于 2 个候选做不成"选择题"，卡片会退化成纯输入框 —— 仍可接受（前端有自定义输入）
        return {"done": False, "needs_user": True, "needs_kind": kind,
                "default": dflt, "question": q[:200], "options": opts[:6]}
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


def _ngram_dice(a: str, b: str, n: int = 3) -> float:
    """两段正文的字符 3-gram **多重集 Dice 系数**（去空白后），范围 0~1。

    ★ 为什么空转闸用 Dice 而不是 `_ngram_sim`（集合 Jaccard）——两者盲区相反：
      · 集合 Jaccard：长文本 + 少量新增会被**稀释**（集合语义把重复内容去重）
        ⇒ 实测「重复底稿 + 少量新增」只有 0.72；
      · 多重集 Dice：对「几乎没新增」**敏感**，但中文通用语料会把底噪抬高
        ⇒ 实测同域的另一条长答复也有 0.80。
      ⇒ 两者都要 0.95 以上的阈值才安全，而 Jaccard 在真机重述上是 0.85~0.95
        （卡在阈值边缘），Dice 是 0.97~0.99（稳过）⇒ **空转闸用 Dice**。
      而 `merge_round_content` 的④b 要判的是"是不是同一份产物"（有代码块、块数相同），
        那里 Jaccard 的低底噪更有利 ⇒ **保持 Jaccard 不动**。
      两个判据各用各的度量，**不要混用**（混用就会出现"改了阈值另一个判据失效"）。
    """
    from collections import Counter
    x, y = " ".join(str(a or "").split()), " ".join(str(b or "").split())
    if len(x) < n or len(y) < n:
        return 1.0 if x == y else 0.0
    ca = Counter(x[i:i + n] for i in range(len(x) - n + 1))
    cb = Counter(y[i:i + n] for i in range(len(y) - n + 1))
    inter = sum((ca & cb).values())
    total = sum(ca.values()) + sum(cb.values())
    return (2.0 * inter / total) if total else 0.0


#: 判「正文原地打转」的 Dice 阈值。
#: 标定（真库 msg 6585，2026-11-15 实测）：
#:   同一任务三次重述  dice = 0.9759 / 0.9705 / 0.9923
#:   同域另一条长答复  dice = 0.8009（底噪）
#:   构造「长底稿 + 大段实质新增」dice = 0.8172（这是**必须放行**的一侧）
#: 取 0.95 —— 落在"实质改进"(0.8172)与"真机重述最低值"(0.9705)之间，两侧都有余量。
#: ⚠️ 构造样本是**人为**的（真机没有"实质改进"对照样本）—— 这是本阈值最弱的一环，
#:   门禁 `verify_auto_continue_stop.py` 的 C 组把该限制写进断言。
_REPEAT_SIM = 0.95


def is_content_repeating(cur: str, prev: str, thr: float = _REPEAT_SIM) -> bool:
    """正文级空转：本轮正文与上一轮**高度雷同且长度几乎没变** ⇒ 判定在原地打转。

    ★ 为什么需要它（2026-11-15 真机 conv 999999024 / msg 6585）：
      `is_spinning` 比的是 `next_step`（一句话），而模型每轮换个说法写同一个
      next_step（"补充属性与追溯信息" vs "回查属性完整性"）⇒ 逐字判据抓不到 ⇒
      连续续跑 3 轮，每轮把整份答案重写一遍 ⇒ 用户看到 3 份、预算白烧 3 次。

    ⚠️ 为什么用**复合判据**（Dice + 长度比）而不是单一相似度：
      纯相似度会误伤「实质改进」—— 真正的迭代通常**正文明显变长**（多做了东西）。
      加上"长度比几乎不变"这一条，语义就变成
      **「换了几个字的重述」** 而不是「变短/变长的新内容」。
    ⚠️ 阈值与标定的诚实边界：
      真机只取到三个样本群 ——
        · 同一任务的重述（msg 6585 三份）：dice 0.9759 / 0.9705 / 0.9923；
        · 同域另一条长答复（底噪）：dice 0.8009；
        · 构造的「长底稿 + 大段实质新增」（**必须放行**的一侧）：dice 0.8172。
      **真机没有「实质改进」的对照样本** ⇒ 阈值 0.95 是夹在构造样本(0.8172)与
      真机重述最低值(0.9705)之间取的，靠长度比复合约束再兜一层。
      这是本判据最弱的一环，如实标注。
    """
    c, p = str(cur or "").strip(), str(prev or "").strip()
    if not c or not p:
        return False
    try:
        _ratio = len(c) / float(len(p) or 1)
    except Exception:
        return False
    # 长度差超过 ±5% ⇒ 视为有新内容，不算空转（真机三轮的长度比都在 1±0.5% 内）
    if not (0.95 <= _ratio <= 1.05):
        return False
    try:
        return _ngram_dice(c, p) >= float(thr)
    except Exception:
        return False


def decide(verdict, *, used: int, max_used: int, seen_steps,
           deadline_left_s=None, budget_left: bool = True,
           budget_ratio: float = 0.0, degrade_ratio: float = 0.7,
           hard_stop_ratio: float = 0.95,
           content_prev: str = "", content_cur: str = "",
           content_sim_thr: float = _REPEAT_SIM):
    """纯决策：这一轮工作结束后，要不要自主续跑。

    :returns: ``(continue: bool, reason: str)`` —— reason 供 stage/reasoning 透传。
    四道闸（全部对齐 AutoGPT boundedness 教训）：
      ① 次数闸 ``max_used``；② 墙钟闸 ``deadline_left_s``；
      ③ token 预算闸 ``budget_left``；④ 空转闸 ``is_spinning``。
    ★ P1-1（2026-10-28）第五道闸：**预算收紧 / 降级收尾**——
      · ``budget_ratio >= degrade_ratio`` 但预算**尚未耗尽** ⇒ **继续跑**，
        但降级为"收尾模式"（只做剩余关键步骤，禁开新检索）；理由串含「收紧续跑」。
      · ``budget_ratio >= hard_stop_ratio`` 或预算已耗尽 ⇒ 停，并写待续清单。
      ⚠️ 第一版在 ``degrade_ratio`` 处**直接停**，真机（conv 9730）后果：
        预算用到 86%（阈值 70%）就停，而当时**还剩 14% ≈ 2~3 轮**，
        任务差一步（跑校验器）没做完 ⇒ 用户看到的就是"还是没有持续走下去"。
        停 vs 续的分界应当是"还有没有额度"，而不是"用掉多少比例"。
      ⚠️ 降级优先级**低于**"任务已完成"（已完成就说完成，不必提预算）。
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
    try:
        _dr = float(degrade_ratio or 0.0)
        _hr = float(hard_stop_ratio or 0.0)
        _br = float(budget_ratio or 0.0)
    except Exception:
        _dr, _hr, _br = 0.0, 0.0, 0.0
    # ① 真·耗尽 / 触及硬停线 ⇒ 停（并写待续清单）
    if (not budget_left) or (_hr > 0 and _br >= _hr):
        return False, ("token 预算已用 %.0f%%（硬停线 %.0f%%）⇒ 降级收尾："
                       "不再新开续跑轮，如实列出待续事项"
                       % (_br * 100, _hr * 100))
    ns = str(verdict.get("next_step") or "").strip()
    if not ns:
        return False, "评估结论缺少下一步描述，按不续跑处理"
    if is_spinning(ns, seen_steps):
        return False, "剩余工作与此前步骤相同，判定空转，停止续跑"
    # ⑤ 正文级空转闸（本轮正文 = 上一轮正文，只换了几个字）—— 2026-11-15 新增。
    #   为什么放在这里（预算降级之前）：预算吃紧时"收紧续跑"仍值得试一次，
    #   但如果正文**根本没变**，再跑一轮也是同样的内容 ⇒ 那是纯烧钱，直接停。
    if content_cur and content_prev and is_content_repeating(
            content_cur, content_prev, content_sim_thr):
        return False, ("正文与上一轮高度雷同且长度几乎未变（判定在原地打转），"
                       "停止续跑——继续只会重复同一份内容")
    # ② 预算吃紧但还有额度 ⇒ **续跑**，只是收紧动作面
    if _dr > 0 and _br >= _dr:
        return True, ("token 预算已用 %.0f%%（阈值 %.0f%%）⇒ 收紧续跑："
                      "只做剩余关键步骤，不再新开检索/大查询；下一步：%s"
                      % (_br * 100, _dr * 100, ns[:120]))
    return True, f"任务未完成，自动继续：{ns}"


def build_degrade_notice(remaining, ratio: float, threshold: float = 0.7) -> str:
    """构造"预算降级收尾"的告知（用户可见，附在答复末尾）。

    ★ P1-1（2026-10-28）：把"突然断了"变成"分段推进"。
    必须含三件事（缺一即退化成"又一次硬停"）：
      ① 说明为什么停（预算，不是任务完成）
      ② 列出**还差什么**（用户能据此决定要不要继续）
      ③ 说明**怎么继续**（下一条消息即可）—— 不能只留一个死胡同
    """
    items = [str(x) for x in (remaining or []) if str(x).strip()]
    lines = [
        "",
        "---",
        "⚠️ **本轮已分段收尾**（token 预算已用 %.0f%%，达降级阈值 %.0f%%）。"
        % (ratio * 100, threshold * 100),
        "任务**尚未全部完成**，为保证输出质量先停在这里。",
    ]
    if items:
        lines.append("")
        lines.append("**待续事项**：")
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it}")
    lines.append("")
    lines.append("回复「继续」我接着做（上下文已保留，不必重述需求）。")
    return "\n".join(lines)


def _code_blocks(text: str) -> list:
    """取出文本里的围栏代码块（去掉语言标签），用于判"是否同一份产物"。"""
    out, buf, inside = [], [], False
    for line in str(text or "").split("\n"):
        if line.strip().startswith("```"):
            if inside:
                out.append("\n".join(buf).strip())
                buf = []
                inside = False
            else:
                inside = True
            continue
        if inside:
            buf.append(line)
    return out


def _ngram_sim(a: str, b: str, n: int = 3) -> float:
    """两段正文的字符 3-gram Jaccard 相似度（去空白后），范围 0~1。

    为什么用字符 n-gram 而不是分词/编辑距离：
      · 中文没有分词边界，字符 n-gram 才稳，且**零依赖**（本模块是纯函数模块，不引库）；
      · 编辑距离对 1 万字符是 O(n²)，续跑轮每轮都要判一次，成本不可接受；
      · 实测分离度足够（见 `merge_round_content` 的标定数据）：同一任务的两次重述
        落在 0.85~0.95，而**不同主题**的两段正文只有 0.30 ⇒ 判据有很大的中间地带。
    """
    x, y = " ".join(str(a or "").split()), " ".join(str(b or "").split())
    if len(x) < n or len(y) < n:
        return 1.0 if x == y else 0.0
    ga = {x[i:i + n] for i in range(len(x) - n + 1)}
    gb = {y[i:i + n] for i in range(len(y) - n + 1)}
    return len(ga & gb) / float(len(ga | gb) or 1)


#: 判「同一份产物的又一次重述」的相似度阈值。
#: 标定（真库 msg 6585，2026-11-14）：同一轮任务被续跑 3 次生成的 3 份正文
#:   J(1,2)=0.8768 / J(1,3)=0.8483 / J(2,3)=0.9540；而与该会话无关的另一条长答复只有 0.3034。
#: 取 0.70 —— 命中区间最低值之上还留 0.15 余量，非同类文本（0.30）离得很远。
#: ⚠️ 调这个数之前先跑 `tools/verify/verify_conv9736_write.py` 的 E7~E11（真机样本标定）。
_RESTATE_SIM = 0.70

#: 「重新作答 vs 接着做」的话题重叠下限（`merge_round_content` 判据⑤）。
#: 标定：真机 msg#7039 三次 attempt 两两 3-gram 相似度 = 0.392 / 0.448 / 0.087，
#:   —— 前两个是同一件事的不同措辞（应替换），第三个（与 a1）完全不同话题（不该混）。
#:   取 **0.20**（不是 0.25）：实测 a2~a3 = **0.253**，与 0.25 只差 0.003 ⇒ 门槛贴脸，
#:     再抖一点就漏替换。而"无关"侧实测只有 **0.023**（需求 vs 设计）⇒ 0.20 距无关带
#:     8.7 倍、距真机重述下限 0.053，是这条中间带里最稳的取点。
#:   标定方法：`_ngram_sim`（3-gram Jaccard），与判据④b 同一度量（不混用）。
#: ⚠️ 不要为了"更彻底去重"把它调高：调高会让同一件事的**换措辞重述**变成追加。
_RESTATE_TOPIC_MIN = 0.20


def merge_round_content(prev: str, cur: str, kind: str | None = None) -> str:
    """把**同一任务的续跑轮正文**并进已有正文，但**不许堆出多份相同内容**。

    ★ 2026-11-19 P1-2：`kind` 是上游 `attempt_trace` 在**产生时**打的语义标。
      给它之后就不用再从文本反推「这是续写还是重跑」——
        · `continuation`（截断续写 / 用户说继续）⇒ 语义 **append**，前面的绝不能丢；
        · `retry`（harness 判未完成）⇒ 语义 **replace**，用 cur 替换 prev，
          但把 prev 里 cur 没有的围栏块搬回（carry_over），防丢成品。
      真库 msg#7039 回放：三次相似度仅 0.392/0.448，**旧的文本判据全军覆没**
      （误判为「真新增」而追加 ⇒ 21,864 字里结论讲三遍、代码三份）；
      按 kind 决策后 11,138 字，提示词 + 代码各一份，信息零丢失。
      ⚠️ `kind=None`（旧调用/旧数据）⇒ 完全走原有文本判据，逐字节向后兼容。

    ★ 2026-10-28（conv 9736 实证）：用户报「生成代码中还是存在多份相同的内容」。
      真机核查：那条消息 **37,620 字符**，含 **7 个代码块，md5 完全相同（af683702）**
      —— 是 7 轮"结论先行 + 逐条对账 + 同一份完整代码"被 `\\n\\n` 直接累加。
      拼接策略的错在于：**续跑轮的正文本意是"接着做"**，而模型每轮都会重写一份
      完整报告；把每轮都追加 ⇒ 用户看到 7 份重复，重点被稀释。

    ★ 2026-11-14（conv 999999024 / msg 6585 实证）：**判据④"md5 相同"抓不到真机形态**。
      那次续跑生成的三份正文措辞并不逐字相同（"负载/工况" vs "负载与工况"），
      三个 ```json 块 md5 全不同 ⇒ ④ 不命中 ⇒ 走 ⑤ 追加 ⇒ 用户仍看到 **3 份**
      （29,774 字符，同一份"结论摘要 + 结构化结果"讲了三遍）。
      ⇒ 补判据④b/④c：**同一份产物的重述不一定逐字相同**，用 3-gram 相似度判。
      ⚠️ 为什么不干脆去掉 ④ 只留相似度：md5 相同是**充分**证据（相似度必为 1），
        保留它可让"逐字重复"这条最确定的路零成本先短路。

    判据（纯函数，可单测）：
      ① `cur` 空 ⇒ 保留 `prev`；
      ② `cur` 包含 `prev`（cur 是更完整版）⇒ **取 cur**；
      ③ `prev` 包含 `cur`（cur 只是片段）⇒ **保留 prev**；
      ④ 两边的代码块集合相同（同一份产物重述）⇒ **取 cur**（最新措辞/结论）；
      ④b 代码块**数量相同**且都 ≥1、整体相似度 ≥ `_RESTATE_SIM` ⇒ 取 cur；
      ④c 两边都无代码块、整体相似度 ≥ 0.85 ⇒ 取 cur（纯散文重述，判据更严）；
      ⑤ 其余（确实是新增的不同内容）⇒ 追加，保信息。
    """
    p, c = str(prev or "").strip(), str(cur or "").strip()
    if not c:
        return p
    if not p:
        return c
    if c == p:
        return c
    # ★ P1-2：上游给了语义标就按语义走，不再从文本猜（这是本函数存在的根本理由）。
    if kind:
        try:
            if kind == "continuation":
                # 续写：append，前面的一个字都不能丢（判据与 kind 语义严格对应）
                return p + "\n\n" + c
            if kind == "retry":
                # 重跑：replace + carry（把上一版独有的围栏块搬回，防丢用户要的成品）
                from agent.final_content import carry_over_unique_blocks as _carry_k
                return _carry_k(p, c)
        except Exception:
            pass   # kind 决策失败 ⇒ 落回下方文本判据（fail-safe，不阻断）
    # ② / ③ 包含关系（用规整化后的文本比，避免空白差异误判）
    _np = " ".join(p.split())
    _nc = " ".join(c.split())
    if _nc in _np:
        return p
    if _np in _nc:
        return c
    # ④ 代码块集合相同 ⇒ 同一产物的又一次重述
    _bp, _bc = [], []
    try:
        _bp, _bc = _code_blocks(p), _code_blocks(c)
        if _bp and _bc:
            sp = {b for b in _bp if b}
            sc = {b for b in _bc if b}
            if sp and sc and sp == sc:
                return c
            # 新内容是旧内容的超集（多了代码块）⇒ 取新
            if sc and sc > sp:
                return c
    except Exception:
        pass
    # ④b / ④c 逐字不同但语义同一份（措辞微调）—— 见函数头 2026-11-14 那段标定
    try:
        _sim = _ngram_sim(p, c)
        if _bp and _bc:
            # 有产物：块数相同 = 同一份产物的重述（多一块/少一块都算真有新增，不吞）
            if len(_bp) == len(_bc) and _sim >= _RESTATE_SIM:
                return c
        elif not _bp and not _bc and _sim >= 0.85:
            # 纯散文：没有代码块可对齐，只认"几乎逐句改写"这一种，判据刻意更严
            return c
    except Exception:
        pass
    # ⑤ 自洽完整重述 ⇒ **替换**（而不是追加），但保上一轮独有的块
    #
    # ★★ 2026-11-19（msg#7039 实证，这是"反馈多次仍未解决"的真因）：
    #   三轮续跑各写了一份**完整回复**，但三次措辞差异极大 ——
    #   两两 3-gram 相似度只有 **0.392 / 0.448 / 0.087**，块数 1 / 2 / 1，
    #   ⇒ 判据 ④/④b/④c **全军覆没**（它们都要求"内容高度相似"），落到 ⑥ 追加
    #   ⇒ 21,864 字里「## 结论」讲了三遍、同一份 SysML 代码三份。
    #   ⇒ 这说明"靠相似度判重述"这条路走不通：**同一件事可以用完全不同的措辞讲**。
    #
    #   换判据：不看措辞，看**结构自洽性**。一份完整回复的特征是
    #     「自己带 ≥2 个 markdown 标题 + 篇幅与前一轮同量级」；
    #   而"接着做"的增量输出通常很短、且没有独立标题体系。
    #   命中 ⇒ 判定为"重新作答"⇒ 用 cur **替换** prev（历次 attempt 已在执行过程卡可见），
    #   但把 prev 里 cur **没有的**围栏块补回（防丢用户真正要的成品）。
    try:
        from agent.final_content import carry_over_unique_blocks as _carry
        _n_head = len(re.findall(r"^#{1,4}[ \t]", c, re.M))
        # ⚠️ 三个条件缺一不可，否则会**误吞真正的新内容**：
        #   ① 结构自洽：cur 自带 ≥2 个标题（"接着做第 2 项"的增量通常没有独立标题体系）；
        #   ② 篇幅同量级：cur ≥ prev 的 80%（片段式增量一定更短）；
        #   ③ 话题重叠：3-gram 相似度 ≥ `_RESTATE_TOPIC_MIN`。
        #      这条是**防误吞的主闸**：若第 1 轮讲需求、第 2 轮讲设计（真不同的内容），
        #      相似度会落到 0.2 以下 ⇒ 不替换 ⇒ 走追加（宁可多留，不可丢）。
        #      实测 #7039 三次 attempt 的相似度 0.392/0.448 都远高于 0.25 ⇒ 仍能替换。
        if (_n_head >= 2 and len(c) >= 0.8 * len(p)
                and _ngram_sim(p, c) >= _RESTATE_TOPIC_MIN):
            return _carry(p, c)
    except Exception:
        pass
    return p + "\n\n" + c


def continuation_prompt(user_input: str, verdict: dict, round_no: int) -> str:
    """续跑指令（注入 messages 的下一条 user 消息，驱动 ReAct 再转）。"""
    rem = verdict.get("remaining") or []
    rem_txt = "\n".join(f"- {x}" for x in rem) if rem else "（评估器未列明）"
    # ★ 2026-10-28（conv 9733 实测）：**必须提醒结项**。
    #   真机现场：清单建了 7 条全部对，校验也真跑了，但模型**一条都没标完成**
    #   ⇒ 待办闸始终 7/7 未完成 ⇒ 闸门只能反复判"未完成"，
    #   它并不知道模型其实已经在推进 —— 账实不符比不做清单更糟。
    #   ⇒ 每次续跑都带一句"把刚做完的那条标掉"，把记账成本降到一次工具调用。
    _todo_hint = ""
    try:
        from core import config as _cfg
        if bool(_cfg.as_bool("agent", "todo_enabled", False)) and (rem or verdict.get("next_step")):
            _todo_hint = (
                "\n【结项提醒】如果你**刚完成了**清单里的某一条（有工具回执为证），"
                "立刻调 `todo_write(action=update, id=…, status=\"completed\", "
                "verify_result=\"<回执关键字段>\")` 把它标掉；开工的那条标 `in_progress`。"
                "账实不符（做了没标）会让完成判定一直卡在「未完成」。")
    except Exception:
        _todo_hint = ""
    return (
        f"【自主续跑 · 第 {round_no} 次】系统完成度评估：你的请求尚未完成，现自动继续，无需用户确认。\n"
        f"【用户原始请求】{user_input or '（空）'}\n"
        f"【下一步】{verdict.get('next_step') or ''}\n"
        f"【剩余事项】\n{rem_txt}\n"
        # ★★ 2026-11-19（msg#7039 实证）：原来只写「不要重复已完成的工作，不要复述上文」，
        #   模型仍然**整份重写**了答复 —— 三次续跑各写一份完整报告，
        #   21,864 字里「## 结论」讲了三遍、同一份 SysML 代码三份（用户反馈多次）。
        #   根因：模型把"自动继续"理解成"重新回答一遍"，因为它看不到自己上一轮的输出
        #   在正文里的**位置**，只知道"任务未完成"。
        #   ⇒ 必须把禁令写到**可执行**粒度：禁止重写标题体系、禁止重贴已交付成品、
        #     只允许"接着写"新增部分；并且明确"若要修正上文，只改那一段并说明改了哪里"。
        f"【要求】直接继续执行：调用工具获取信息，或直接产出**本轮新增**内容。\n"
        f"【硬约束 · 不要重复】\n"
        f"  1. 不要重新组织一份完整答复，不要重写标题体系（不要又从一个「## 结论」开头）；\n"
        f"  2. 上一轮已经交付的成品（提示词全文、代码块、清单、表格）**不要再次粘贴**，"
        f"如需引用请写「见上文」。\n"
        f"  3. 只输出这次**新做到**的部分；若发现上文有错，只改那一处并说明「已修正：…」。\n"
        f"  4. 不要复述上文，不要询问用户是否继续。\n"
        f"若确实遇到无法独自解决的问题（缺关键信息/需用户拍板/不可解错误），"
        f"调用 ask_user 工具向用户澄清，或说明阻塞原因后收尾。"
        + _todo_hint
    )


def defer_clarify(verdict, gate, *, n: int = 0, last_rem=None, max_defer: int = 2):
    """**待办未做完时，先别问人** —— 把 needs_user 的提问延后到无可做项。

    ★ 2026-10-28（conv 9732 实测）：评估器判 `needs_user`（"是否按当前方案校验后写入"），
      系统立刻打回澄清卡片 ⇒ 用户被问一次，而**只读的校验一步都没做**。
      这违反用户原话「完成校验后写入，写入前确认」：确认是**写入门**，
      不该把校验也挡在门外。

    :param gate: `todo_gate()` 的返回（含 done / remaining / next_step / fallback）
    :param n:    已经延后过的次数
    :param last_rem: 上次延后时的剩余项数（要求"确实在减少"才允许继续延后）
    :returns: ``(verdict, note, n, last_rem)``
              · 不延后 ⇒ 原样返回 `verdict`、note 为空、计数不变
              · 延后   ⇒ 返回一个"未完成 + 用待办 next_step"的 verdict，note 说明原因
    有界（否则会一直不问）：最多 `max_defer` 次，且剩余项数必须较上次减少。
    """
    try:
        if not isinstance(gate, dict) or gate.get("fallback"):
            return verdict, "", n, last_rem
        if gate.get("done"):
            return verdict, "", n, last_rem
        rem = list(gate.get("remaining") or [])
        if not (rem or gate.get("next_step")):
            return verdict, "", n, last_rem
        if int(n or 0) >= int(max_defer or 0):
            return verdict, "", n, last_rem
        cur = len(rem)
        moving = (last_rem is None) or (cur < int(last_rem))
        if not moving:
            return verdict, "", n, last_rem
        new_v = {"done": False,
                 "next_step": str(gate.get("next_step") or ""),
                 "remaining": rem}
        note = ("待办未完成（%d 项）⇒ 先做完可做项，把需要用户拍板的问题"
                "留到无可做项时再问" % cur)
        return new_v, note, int(n or 0) + 1, cur
    except Exception:
        return verdict, "", n, last_rem


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

    ★ P1-2（2026-10-28）：`needs_kind == "has_default"` ⇒ **不转卡**，返回 None。
      那种情况有合理默认值，应该「按默认推进 + 在答复里标注假设」，
      而不是让用户多点一次。转换逻辑见 `default_progress_verdict()`。
    """
    if not isinstance(verdict, dict) or not verdict.get("needs_user"):
        return None
    if str(verdict.get("needs_kind") or "") == "has_default":
        return None
    q = str(verdict.get("question") or "").strip()
    if not q:
        return None
    opts = [str(x).strip()[:60] for x in (verdict.get("options") or []) if str(x).strip()]
    return {"id": qid, "question": q[:200],
            "options": opts[:6], "allow_custom": True}


def default_progress_verdict(verdict) -> dict | None:
    """`has_default` 类 needs_user ⇒ 转成**按默认推进**的续跑 verdict。

    ★ P1-2（2026-10-28）核心：把"停下来问"换成"带假设往下做"。

    :returns: 续跑 verdict（done=False + next_step），或 None（不是 has_default ⇒ 调用方走原逻辑）

    语义（三件事缺一不可）：
      1. **按默认值推进**（next_step 里写明用什么默认）
      2. **在答复里显式标注假设**（"按 X 默认处理，如需改请说"）—— 用户有知情权
      3. 保留原问题与候选，便于用户事后纠正
    """
    if not isinstance(verdict, dict) or not verdict.get("needs_user"):
        return None
    if str(verdict.get("needs_kind") or "") != "has_default":
        return None
    dflt = str(verdict.get("default") or "").strip()
    if not dflt:
        return None                       # 无默认值 ⇒ 无法推进（上游已退回 user_only）
    q = str(verdict.get("question") or "").strip()
    return {
        "done": False,
        "next_step": ("按默认值继续：%s（原待定问题：%s）。"
                      "请在答复中明确写出「按「%s」处理，如需更改请告知」"
                      % (dflt[:120], q[:80] or "未说明", dflt[:60])),
        "remaining": [("按默认值 %s 推进（原问题：%s）" % (dflt[:80], q[:80]))],
        "_from_has_default": True,
        "_question": q[:200],
        "_options": [str(x).strip()[:60] for x in (verdict.get("options") or [])
                     if str(x).strip()][:6],
    }
