# -*- coding: utf-8 -*-
"""编排 partial 补齐决策 —— 纯函数模块（2026-10-22，米爸「是否重跑需要一个健全的机制」）。

为什么需要这个模块
------------------
编排收尾时 `orchestrated_status=partial` 只是个**标签**，它承载着至少四种完全不同的病因，
而四种病因的"可重跑性"完全相反。一刀切重跑 = 对其中一半纯烧钱。

真实数据取证（会话 9587，6 个子任务，2026-10-22）：
  · t1 partial → risks.advice 全是「请用户明确方法论 / 提交本体工程师补充」
    ⇒ 缺的是**用户输入**，重跑必然得到同一份结果；
  · t2 partial → 「因无原始需求文档」⇒ 同上，blocked；
  · t3 partial → 「因**工具轮次耗尽**未执行校验」⇒ 执行中断，**换一次就能补**；
  · t5 partial → 「**校验器未运行**」⇒ 有靶子的缺口，**补跑目标明确**；
  · t4/t6 full  ⇒ t6 正文却写着「本轮只完成了一部分，没有全部通过」⇒ **自报不可靠，双向**。

⇒ 结论：**「是否重跑」不取决于 partial 有多严重，取决于它有没有「靶子」。**

标杆依据（详见 docs/编排partial补齐决策机制-对标调研与方案-20261022.md）
--------------------------------------------------------------------
· Claude Code：`stop_reason` 七值 → 五种处置。精髓是「先分类病因，再决定动作」；
  `max_tokens` 不是终止而是**三级恢复**（escalate → inject continue → give up，上限 3 次）；
  **Stop hook 有最终否决权**（返回 block 则 loop 不许结束）。
· LangGraph：pending writes ledger ⇒ resume **只重跑失败分支**，已完成一律复用。
· OpenAI Agents SDK：命中 `max_turns` **不许静默**，要返回标明 partial 的结构化结果。
· Anthropic：harness = budget + brake + **witness**；**agent 失败模式有 1/3 是 verification 问题**
  ——「模型不调工具了」≠「任务完成了」。
· AutoGPT/Devin 教训：run until done → **run within limits**（每个维度都要有天花板）。

本模块三件事（全部纯函数、零 IO、可单测、可变异自证）
----------------------------------------------
1. `classify_partial()` —— 四类病因判定，**判据优先级：外部锚 > 自报**；
2. `decide_fill()`     —— 四道闸（子任务次数/run 次数/token 预算/墙钟 + 空转）；
3. `fill_prompt()` / `gate_summary()` —— 补跑注入指令 + 收尾闸文案。

为什么 fail-safe 方向是「不补」
------------------------------
无靶重跑比不重跑危险得多：前者烧钱且可能把一份**可用**的产出改坏（误差复利），
后者用户补一句话即可恢复。故任何输入异常/证据不足 ⇒ 判 `quality`（不补）。
"""
import re

__all__ = [
    "CAUSE_TRANSIENT", "CAUSE_GAP", "CAUSE_BLOCKED", "CAUSE_QUALITY",
    "CAUSE_VALUES", "FILLABLE_CAUSES", "CAUSE_LABEL",
    "classify_partial", "decide_fill", "fill_prompt", "gate_summary",
    "summarize_causes", "DEFAULT_MAX_PER_TASK", "DEFAULT_MAX_RUN", "DEFAULT_MAX_TASKS",
]

# ── 病因常量 ────────────────────────────────────────────────────────────────
CAUSE_TRANSIENT = "transient"   # 执行中断：超时/限流/5xx/轮次耗尽 → 换一次大概率必成
CAUSE_GAP = "gap"               # 客观缺口：未校验/产物缺失/未落盘 → 有靶子，知道补什么
CAUSE_BLOCKED = "blocked"       # 阻塞：缺用户输入/需拍板/需外部定义 → 重跑一百次也一样
CAUSE_QUALITY = "quality"       # 质量不足：仅评审低分，无客观缺口 → 无靶，重跑是随机扰动

CAUSE_VALUES = (CAUSE_TRANSIENT, CAUSE_GAP, CAUSE_BLOCKED, CAUSE_QUALITY)
#: 只有这两类**有靶子**，才允许自动补跑（D2：quality 不补、D4：blocked 转人工）
FILLABLE_CAUSES = (CAUSE_TRANSIENT, CAUSE_GAP)

#: 前端/文案展示用中文名（避免各处各写一份）
CAUSE_LABEL = {
    CAUSE_TRANSIENT: "执行中断",
    CAUSE_GAP: "客观缺口",
    CAUSE_BLOCKED: "待用户输入",
    CAUSE_QUALITY: "质量不足",
}

#: 默认闸门参数（D3：子任务重跑上限 2 次，第 3 次转人工）
DEFAULT_MAX_PER_TASK = 2
DEFAULT_MAX_RUN = 5        # 与 orch_checkpoint._MAX_ATTEMPTS 同源口径
DEFAULT_MAX_TASKS = 2      # 一轮补救最多补几个（控墙钟与成本）

# ── 判据正则（全部基于真实数据取样，不是拍脑袋）────────────────────────────
# transient：执行层中断。**必须**能命中「轮次耗尽」——会话 9587 t3 正是此因。
_RE_TRANSIENT = re.compile(
    r"轮次耗尽|工具轮次(已)?(耗尽|用尽|超限)|预算耗尽|token\s*预算|"
    r"超时|timed?\s*out|rate[\s_-]?limit|限流|调用频率|"
    r"连接(失败|中断|重置|被拒)|服务不可用|网关超时|"
    r"\b(429|500|502|503|504)\b|overloaded|try\s*again",
    re.I)

# gap：有靶子的缺口。"未校验/校验器未运行"是最强信号 —— 重跑目标明确（跑一次 validate）。
# ⚠️ `未(生成|产出)…` 必须允许中间夹修饰语：真实数据里有
#    「未产出**任何可校验的**视图代码」（会话 4772 t3），写死 `未产出(代码|模型)`
#    会漏判成 quality ⇒ 该补的不补（实测全库漏判）。
_RE_GAP = re.compile(
    r"未(执行|运行|跑|做|完成|通过)(校验|验证|validate|检查)|"
    r"校验器未(运行|执行)|未经校验|校验未(运行|执行|通过)|"
    r"待校验|缺少校验|没有(跑|做|执行)(校验|验证)|"
    r"未落盘|未(生成|产出|创建|输出).{0,16}(交付物|代码|模型|文件|文档|视图)|"
    r"未产出任何|未到达校验环节",
    re.I)

# blocked（advice 侧）：祈使句让"用户/客户/业务方"去做什么 ⇒ 缺外部输入，补跑无用。
# ⚠️ 输入方**不止"用户"**：全库实测（2026-10-22）真实措辞集中在
#    「请客户确认」「请客户方提供」「由业务方补充」「与客户逐项确认」「需人工确认(L1)」——
#    只写"用户/您"会漏掉绝大多数（7 个有摘要的 quality 里 5 个其实是 blocked）。
_RE_BLOCKED_ADVICE = re.compile(
    r"请(用户|您|客户|甲方|业主|业务方|需求方|产品经理|提供|确认|明确|补充|给出|提交|对齐)|"
    r"(由|向|与|找).{0,12}(负责人|设计师|工程师|专业|团队|总体|业务方|需求方|客户|甲方)"
    r".{0,10}(明确|确认|复核|提供|补充|对齐)|"
    r"(需|待|要)\s*(用户|客户|人工|甲方)|"
    r"补充.{0,14}(文档|规格书|资料|区间|清单|定义|取值|判据|信息)|"
    r"提交.{0,10}(补充|定义|清单|工程师)|"
    r"明确(方案数|指标|口径|边界|准则)",
    re.I)

# blocked（正文侧）：缺输入的自述。
_RE_BLOCKED_TEXT = re.compile(
    r"未绑定.{0,12}(方法论|规约|规范|标准)|"
    r"因无.{0,12}(文档|资料|输入|规格书|数据)|"
    r"未在上下文中提供|无法获取|缺失需补充|需(要)?(用户)?(对齐|确认|拍板)|"
    r"禁止.{0,8}自行(编造|假定)|"
    # ↓ 2026-10-22 全库实测补充：这三类措辞在真实数据里占 blocked 的多数
    r"需人工确认|人工确认|待人工|留空待(补|确认)|待确认|"
    r"(客户|用户|业务方|需求方).{0,8}(数据|名单|信息|字段).{0,4}未提供|"
    r"输入缺失待补|不可得",
    re.I)

#: 分类时只看摘要正文 + risks 的前 N 字（分类不需要全文；防止超长文本拖慢正则）
_SCAN_CAP = 4000
#: evidence 单条截断
_EVID_CAP = 110


def _text_of(item: dict) -> str:
    """把子任务的可判读文本拼起来（摘要正文 + risks 的标题/说明/建议 + 原结果）。

    为什么 risks.advice 必须计入：会话 9587 t1 的 `summary` 正文只说"雷达域本体类型缺失需补充"，
    但 `risks[].advice` 写着"请用户明确本项目方法论" —— **缺用户输入的决定性证据在 advice 里**，
    只看正文会把它误判成 gap（可补）从而白跑一次。
    """
    if not isinstance(item, dict):
        return ""
    parts = []
    s = item.get("summary")
    if isinstance(s, dict):
        parts.append(str(s.get("summary") or ""))
        for r in (s.get("risks") or [])[:8]:
            if isinstance(r, dict):
                parts.append(str(r.get("title") or ""))
                parts.append(str(r.get("desc") or ""))
                parts.append(str(r.get("advice") or ""))
    parts.append(str(item.get("result") or ""))
    parts.append(str(item.get("error") or ""))
    t = "\n".join(x for x in parts if x)
    return t[:_SCAN_CAP]


def _advice_of(item: dict) -> str:
    """只取 risks[].advice —— blocked 判据的主战场（祈使句集中在这里）。"""
    s = (item or {}).get("summary")
    if not isinstance(s, dict):
        return ""
    out = []
    for r in (s.get("risks") or [])[:8]:
        if isinstance(r, dict):
            out.append(str(r.get("advice") or ""))
    return "\n".join(out)[:_SCAN_CAP]


def _artifacts_of(item: dict):
    s = (item or {}).get("summary")
    if not isinstance(s, dict):
        return []
    a = s.get("artifacts")
    return a if isinstance(a, list) else []


def _missing_of(item: dict) -> list:
    """取 `summary.missing` —— 模型**显式声明的缺口清单**。

    为什么它是最强判据：这是模型自己逐条列出的"缺什么"，
    比任何正则都可靠（真库实测：22 个 partial 全写了 risks，但只有 3 个写了 missing ——
    而这 3 个的 missing 内容如「模型校验结果（未执行 sysml_v2_validate）」
    正是 `gap` 的完美证据，正则反而要靠"未执行校验"这种措辞去猜）。
    协议侧见 services/subtask_protocol.py:395（仅 status=partial 时保留）。
    """
    s = (item or {}).get("summary")
    if not isinstance(s, dict):
        return []
    m = s.get("missing")
    if not isinstance(m, list):
        return []
    return [str(x).strip() for x in m if str(x).strip()][:10]


#: missing 条目里出现「外部输入方 + 其提供物」⇒ 这不是自己能补的缺口。
# 实测（会话 3987 t1）：missing = ['客户提供的利益相关方名单', '客户具体车型信息', …]
#   —— 全是客户侧输入，补跑一百次也拿不到 ⇒ 必须判 blocked 而不是 gap。
_RE_BLOCKED_SUBJ = re.compile(
    r"(客户|用户|甲方|业务方|需求方|业主)(提供|的|具体|名单|信息|车型|资料|数据|确认)")


def _snip(t: str) -> str:
    """证据摘录：压空白 + 截断，供 stage/reason 透传与前端展示。"""
    return re.sub(r"\s+", " ", str(t or "")).strip()[:_EVID_CAP]


def _ctx(text: str, m, span: int = 26) -> str:
    """取命中点的**上下文窗口**，而不是命中片段本身。

    为什么必须带上下文：正则命中常常只有 3~4 个字（如 `请用户`、`耗尽`），
    单看这几个字读者无法判断发生了什么（实测会话 9587：t1 的证据只剩"请用户"）。
    带前后 26 字后是「请用户明确本项目方法论（OMG SysML v2 / …」，一眼可判。
    """
    if not m or not text:
        return ""
    a, b = max(0, m.start() - span), min(len(text), m.end() + span)
    s = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def _has_high_blocked_risk(item: dict) -> bool:
    """risks 里是否存在 high 级别、且建议指向"缺外部输入"的条目。

    用途：同样是可补（transient/gap），**带外部门槛的排后面补**——
    先补那些不依赖别人拍板的，一轮预算才不会被门槛项吃掉。
    """
    s = (item or {}).get("summary")
    if not isinstance(s, dict):
        return False
    for r in (s.get("risks") or [])[:8]:
        if not isinstance(r, dict):
            continue
        if str(r.get("level") or "").lower() != "high":
            continue
        if _RE_BLOCKED_ADVICE.search(str(r.get("advice") or "")):
            return True
    return False


def classify_partial(item: dict) -> dict:
    """单个未完成子任务 → 病因分类（四类之一）+ 证据 + 是否可补。

    :param item: 子任务记录，宽容接受多种形态（有/无 `summary`、有/无 `result`）：
                 ``{key|task_key, title, status, summary:{status,summary,risks,artifacts},
                    result, error, error_kind}``
    :returns: ``{"key","title","cause","causes_all","evidence","fillable",
                 "blocked_hint","hint"}``

    判据优先级（**先分类再动作**，对齐 Claude Code 的 stop_reason 分派）：
        1. ``transient``：执行层中断（错误码/轮次耗尽/超时/限流）—— 换一次就能成；
        2. ``gap``：有靶子的缺口（未校验/未落盘/未产出）—— 知道补什么；
        3. ``blocked``：缺用户输入（advice 是祈使句 / 正文自述缺资料）；
        4. ``quality``：以上皆无 —— **证据不足一律判此**（fail-safe 向「不补」）。

    为什么 transient 排在 blocked 之前：会话 9587 t3 同时有"轮次耗尽"（可补）与
    "既有架构边界未提供"（缺输入）。重跑**至少能跑完那一步校验**，消掉一个 high risk，
    所以判 transient 更符合「能不能改变结果」这个真正的判准。
    """
    item = item if isinstance(item, dict) else {}
    key = str(item.get("key") or item.get("task_key") or "")
    title = str(item.get("title") or key)
    text = _text_of(item)
    advice = _advice_of(item)
    _sm_d = item.get("summary")
    _risks_n = len(_sm_d.get("risks") or []) if isinstance(_sm_d, dict) else 0

    hits = []
    evidence = ""
    hint = ""

    # ── ⓪ missing（模型显式声明的缺口清单）—— **最强判据，先于一切文本猜测** ──
    #   为什么排第一：它是模型逐条列出的"缺什么"，比"从正文里猜它有没有说没校验"可靠得多。
    #   真库实测（会话 3987 t1）：missing = ['客户提供的利益相关方名单', '客户具体车型信息', …]
    #   ⇒ 全是外部输入 ⇒ 必须判 blocked；而同批 3987 t4 的 missing 里有
    #     「模型校验结果（未执行 sysml_v2_validate）」⇒ 自己能补 ⇒ gap。
    _ms = _missing_of(item)
    if _ms:
        _mtxt = "；".join(_ms)
        if _RE_BLOCKED_SUBJ.search(_mtxt) or _RE_BLOCKED_TEXT.search(_mtxt):
            hits.append(CAUSE_BLOCKED)
            evidence = _snip("缺失清单（需外部输入）：" + _mtxt)
            if not hint:
                hint = "缺少必要的外部输入（%s），无法自行推进。" % _mtxt[:120]
        else:
            hits.append(CAUSE_GAP)
            evidence = _snip("缺失清单：" + _mtxt)
            if not hint:
                hint = ("上一轮自报缺失 %d 项：%s。请逐项补齐并给出可复核结果，"
                        "不要重做已有的分析与正文。" % (len(_ms), _mtxt[:120]))

    # ── ① transient：错误码优先（硬证据），其次文本 ──
    ek = str(item.get("error_kind") or "").strip().lower()
    if ek in ("timeout", "rate_limit", "rate-limit", "connection", "network",
              "transient", "overloaded", "internal", "502", "503", "504"):
        hits.append(CAUSE_TRANSIENT)
        evidence = f"工具层错误类型 {ek}"
        hint = "上一轮因临时性故障中断，请从断点继续完成剩余步骤，不要重复已完成的内容。"
    else:
        m = _RE_TRANSIENT.search(text)
        if m:
            hits.append(CAUSE_TRANSIENT)
            evidence = _snip(_ctx(text, m))
            hint = ("上一轮因临时性中断未走完，请从断点继续：先补跑未完成的"
                    "校验/生成步骤，再给出结论。")

    # ── ② gap：有靶子的缺口 ──
    mg = _RE_GAP.search(text)
    if mg:
        hits.append(CAUSE_GAP)
        if not evidence:
            evidence = _snip(_ctx(text, mg))
        if not hint:
            hint = ("上一轮存在未闭环的客观缺口。请优先执行对应的校验/产出动作并给出"
                    "可复核结果，不要重做已有的分析与正文。")

    # ── ③ blocked：缺外部输入 ──
    mb_adv = _RE_BLOCKED_ADVICE.search(advice)
    mb_txt = _RE_BLOCKED_TEXT.search(text)
    mb = mb_adv or mb_txt
    if mb:
        hits.append(CAUSE_BLOCKED)
        if not evidence:
            evidence = _snip(_ctx(advice if mb_adv else text, mb))

    # ── ④ 兜底 ──
    if not hits:
        hits.append(CAUSE_QUALITY)

    cause = hits[0]
    arts = _artifacts_of(item)
    if cause == CAUSE_GAP and not hint:
        hint = ("上一轮存在未闭环的客观缺口，请优先补齐校验/产物并给出可复核结果，"
                "不要重做已有正文。")
    if cause == CAUSE_BLOCKED:
        if not hint:
            hint = "缺少必要的外部输入（用户口径/资料/本体定义），无法自行推进。"
    if cause == CAUSE_QUALITY:
        if not hint:
            hint = "无客观缺口，仅质量/完整度不理想；建议由用户决定是否重做。"

    # 已有交付物 + 可补 ⇒ 说明"改"而不是"重做"，用于 prompt
    return {
        "key": key,
        "title": title,
        "cause": cause,
        "causes_all": hits,
        "evidence": evidence or "（无明确客观证据）",
        "fillable": cause in FILLABLE_CAUSES,
        "blocked_hint": _has_high_blocked_risk(item),
        "hint": hint,
        "artifacts": len(arts),
        # ★ 证据充分度（2026-10-22 加）：既没有 missing 清单、也没有 risks，
        #   且正文很短 ⇒ 这个分类**很可能是判据不够，而不是真的没缺口**。
        #   为什么不改 cause：宁可标"证据不足"让人复核，也不要为了显得有据就乱补。
        #   消费方（进度事件/前端）应把它显示出来，而不是藏起来。
        "evidence_thin": (not _ms) and _risks_n == 0,
        "missing": _ms,
    }


def decide_fill(verdicts, *, used_per_task=None, used_run=0,
                max_per_task=DEFAULT_MAX_PER_TASK, max_run=DEFAULT_MAX_RUN,
                max_tasks=DEFAULT_MAX_TASKS, budget_left=True,
                deadline_left_s=None, seen=None, covered=None) -> dict:
    """纯决策：这一轮补救要补哪些子任务。

    四道闸（全部对齐 AutoGPT boundedness 教训 —— 每个维度都要有天花板）：
      ① 子任务次数闸 ``max_per_task``（D3：默认 2 次，第 3 次转人工）；
      ② run 级次数闸 ``max_run``（与 ``orch_checkpoint._MAX_ATTEMPTS`` 同口径）；
      ③ token 预算闸 ``budget_left``；
      ④ 墙钟闸 ``deadline_left_s``。
      ＋ 空转闸：同一子任务连续两次同一病因 ⇒ 停（防"补→崩→补"死循环）。
      ＋ **下游覆盖闸**：``gap`` 类且该子任务有已 done 的下游 ⇒ 不补。

    :param verdicts: `classify_partial` 的结果列表
    :param used_per_task: ``{task_key: 已补次数}``
    :param used_run: 本 run 已补轮次
    :param seen: ``{task_key: [历史 cause...]}`` 空转检测用
    :param covered: 缺口**已被下游覆盖**的 task_key 集合（调用方按 deps 计算）
    :returns: ``{"fill":[...], "skip":[{key,cause,reason}], "stop":"", "used":n}``

    fail-safe：任何异常/空输入 ⇒ 不补（``fill=[]``）。

    为什么需要下游覆盖闸（真机实测，2026-10-22 端到端）：
        编排里 t2「生成 BDD 骨架」自报 partial「本轮未调用校验器，代码尚未验证」，
        而 t3 的任务定义就是**「对增量 BDD 骨架运行 sysml_v2_validate 并修复至校验通过」**。
        ⇒ t2 的"未校验"根本不是缺口，是**编排设计上交给下游的一步**。
        不加这道闸会去补跑 t2（实测单次 250s + 全量 token），得到与 t3 重复的产物。
        ⚠️ 只对 ``gap`` 生效：``transient``（执行中断）即使有下游也要补 ——
        下游拿不到中断的上游产出，补了才有东西可消费。
    """
    out = {"fill": [], "skip": [], "stop": "", "used": 0}
    try:
        vs = [v for v in (verdicts or []) if isinstance(v, dict)]
        if not vs:
            return out
        used_per_task = used_per_task if isinstance(used_per_task, dict) else {}
        seen = seen if isinstance(seen, dict) else {}
        covered = covered if isinstance(covered, (set, frozenset, list, tuple)) else ()
        covered = set(str(x) for x in covered)

        # 全局闸：任一条命中即全停（省得逐个判）
        if used_run >= int(max_run):
            out["stop"] = f"补齐轮次已达上限（{max_run} 次）"
            return out
        if not budget_left:
            out["stop"] = "token 预算已耗尽，停止补齐"
            return out
        if deadline_left_s is not None and deadline_left_s <= 0:
            out["stop"] = "补齐墙钟时限已到"
            return out

        # 候选：只有可补病因
        cands, nonfill = [], []
        for v in vs:
            if v.get("fillable"):
                cands.append(v)
            else:
                nonfill.append(v)

        for v in nonfill:
            c = v.get("cause") or CAUSE_QUALITY
            if c == CAUSE_BLOCKED:
                reason = "缺少外部输入，重跑无法改变结果，转人工澄清"
            else:
                reason = "无客观缺口，重跑只是随机扰动（可能导致误差复利）"
            out["skip"].append({"key": v.get("key"), "cause": c, "reason": reason,
                                "title": v.get("title")})

        # 排序：无外部门槛的优先（blocked_hint=False 先），同类保持原序
        cands.sort(key=lambda v: (1 if v.get("blocked_hint") else 0,))

        remaining = max(0, int(max_run) - int(used_run))
        for v in cands:
            k = str(v.get("key") or "")
            if len(out["fill"]) >= min(int(max_tasks), remaining):
                out["skip"].append({"key": k, "cause": v.get("cause"),
                                    "reason": "本轮补齐名额已满（max_tasks=%d）" % max_tasks,
                                    "title": v.get("title")})
                continue
            n = int(used_per_task.get(k) or 0)
            if n >= int(max_per_task):
                out["skip"].append({"key": k, "cause": v.get("cause"),
                                    "reason": "该子任务已补 %d 次，达上限（%d 次），转人工"
                                              % (n, max_per_task),
                                    "title": v.get("title")})
                continue
            hist = seen.get(k) or []
            if hist and str(hist[-1]) == str(v.get("cause")):
                out["skip"].append({"key": k, "cause": v.get("cause"),
                                    "reason": "与上次补齐同一病因，判定空转，停止补齐",
                                    "title": v.get("title")})
                continue
            # 下游覆盖闸（只对 gap）：缺口本就是"编排设计上交给下游的一步"，再补即重复劳动
            if str(v.get("cause")) == CAUSE_GAP and k in covered:
                out["skip"].append({"key": k, "cause": v.get("cause"),
                                    "reason": "该缺口已由下游子任务覆盖（下游已完成），不重复补跑",
                                    "title": v.get("title")})
                continue
            out["fill"].append(v)

        out["used"] = len(out["fill"])
    except Exception as e:                      # 决策器绝不抛异常（fail-safe 向「不补」）
        return {"fill": [], "skip": [], "stop": f"补齐决策异常，按不补处理：{type(e).__name__}",
                "used": 0}
    return out


def fill_prompt(verdict: dict, round_no: int = 1, escalate: bool = False) -> str:
    """补跑注入指令（对齐 Claude Code 的 escalate → inject continue 两级）。

    :param round_no: 第几次补（1 起）
    :param escalate: 是否已升级（第 2 次起为 True）—— 明确告诉模型
                     "上次同参数没成，这次要换策略"，避免"同样参数重来一遍"。

    为什么必须带 `hint`：无靶重跑是 AutoGPT 失败模式①的温床。补跑指令的核心价值
    不是"再试一次"，而是**把缺口本身告诉模型**（缺校验 / 缺产物 / 断在哪一步）。
    """
    v = verdict if isinstance(verdict, dict) else {}
    who = CAUSE_LABEL.get(str(v.get("cause")), "未完成")
    esc = ("\n【升级说明】上一次以相同方式补跑未成功，这说明原做法本身不可行。"
           "本次请**更换策略**（换工具/换分解方式/缩小范围），不要重复上一次的做法。"
           if escalate else "")
    return (
        f"【自动补齐 · 第 {round_no} 次】系统检测到本子任务的产出存在未闭环部分（{who}），现自动补齐，"
        f"无需用户确认。\n"
        f"【子任务】{v.get('title') or v.get('key') or ''}\n"
        f"【缺口证据】{v.get('evidence') or ''}\n"
        f"【本次要求】{v.get('hint') or ''}\n"
        f"【铁律】① 只补缺口，**不要重写已有正文**（已有 {v.get('artifacts') or 0} 份交付物视为有效）；"
        f"② 不要复述上文、不要询问用户是否继续；"
        f"③ 若确认无法独自推进（缺用户口径/缺外部资料/权限不足），"
        f"直接在结果里写明阻塞原因与所需输入，不要编造。{esc}"
    )


def gate_summary(verdicts, *, filled=None, skipped=None) -> str:
    """收尾闸文案：把缺口**前置**到汇总开头（对齐 OpenAI「不许静默」+ Stop hook 否决权）。

    为什么必须前置：现状是缺口散落在卡片 bullet 里，正文却读起来像"全部完成" ——
    这是「失败被记录成成功」的**文案版**。一条 partial 的汇总如果开头不写缺口，
    用户会按 full 的理解去用它。

    :returns: 多行说明；无可写内容时返回 ""。
    """
    try:
        vs = [v for v in (verdicts or []) if isinstance(v, dict)]
        if not vs:
            return ""
        filled = filled if isinstance(filled, list) else []
        skipped = skipped if isinstance(skipped, list) else []
        filled_keys = {str(x) for x in filled}

        rows, fixed_rows = [], []
        for v in vs:
            k = str(v.get("key") or "")
            # 脏输入（无 key）直接跳过：曾几何时这里会吐出一行「- （未完成）：」，
            # 白白往汇总开头塞噪音（实测 C4 判据）。缺口说明宁缺勿滥 ——
            # 它是给用户看的，不是日志。
            if not k:
                continue
            label = CAUSE_LABEL.get(str(v.get("cause")), "未完成")
            line = f"- {k}（{label}）：{v.get('evidence') or ''}"
            if k in filled_keys:
                fixed_rows.append(line + " → 已自动补齐，见正文对应章节")
            else:
                rows.append(line)
        parts = []
        if fixed_rows:
            parts.append("【本次已自动补齐 %d 项】\n%s" % (len(fixed_rows), "\n".join(fixed_rows)))
        if rows:
            tail = ""
            if any(str(v.get("cause")) == CAUSE_BLOCKED for v in vs
                   if str(v.get("key")) not in filled_keys):
                tail = "\n以上「待用户输入」项需要你补充口径或资料后才能继续（重跑无法解决）。"
            parts.append("【本次未达要求 %d 项】\n%s%s" % (len(rows), "\n".join(rows), tail))
        return "\n\n".join(parts)
    except Exception:
        return ""


def summarize_causes(verdicts) -> dict:
    """统计四类分布（供 stage 透传/观测，一眼看出"这批 partial 到底是什么病"）。"""
    counts = {c: 0 for c in CAUSE_VALUES}
    for v in (verdicts or []):
        c = str((v or {}).get("cause") or "")
        if c in counts:
            counts[c] += 1
    return counts
