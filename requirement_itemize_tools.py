# -*- coding: utf-8 -*-
"""`requirement_itemize` —— 需求条目化 + 需求视图骨架生成。

为什么一个工具做两件事（这是刻意的设计）
--------------------------------------
N1（需求结构化）的产物是条目数据 `items[]`，但**条目本身不是模型**——
要进 SysML 必须变成 `requirement def` + 关系（refine/derive/satisfy）。
本工具一次产出两份，让 N1 的输出**直接可被 N3 消费**，中间不需要再转一次：

  ① `items`      —— 条目数据（供追溯矩阵、覆盖性分析、报告）
  ② `req_view_code` —— **需求视图骨架代码**（`package` + stakeholder + requirement + 关系）

职责边界（重要，别越界）
------------------------
* 本工具**只生成需求视图**，不生成结构/行为/视图 —— 那是 N3 其他 `view_type` 的活。
* `satisfy`（需求→设计元素）**不在这里生成**：此时还没有 design 元素，
  强行生成会引用不存在的元素（实测报`Must be a valid feature`）。
  satisfy 分配由 **N2 骨架生成后 / N5 追溯核验时**补，本工具只留 `satisfy_target` 字段供后续引用。

    ┌─ 需求条目化 ──► items[]（数据）
    │                req_view_code（需求视图：stakeholder/requirement/refine/derive）
    │                          │
    └──────────────────────────┴──► N3 视图展开（structure/usecase/activity…）
                                        │
                                        ▼N2/N5 补 satisfy 分配（因为需要 design 元素）
"""
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)

CATEGORIES = ["functional", "performance", "interface", "constraint"]
CAT_CODE = {"functional": "F", "performance": "P", "interface": "I", "constraint": "C"}

# 模糊词（实测来自 requirement_quality.FUZZY_WORDS 的同款判据）
FUZZY = ("良好", "优秀", "快速", "高效", "友好", "灵活", "尽量", "尽可能", "适当",
         "合理", "显著", "大幅", "一定程度", "若干", "某些")
# 量纲词（出现数值+单位 ⇒ 性能类）
# ⚠️ **只收录含中文/明确多字符的单位**。实测踩过：收录单字母 `C`/`s`/`A`/`V`/`W` 后，
#   「通过 CAN 总线通信」会因含 C、A 而被判成 performance ⇒ 接口需求全被误分类。
#   单位要么用中文（分钟/秒/℃），要么用带边界的正则（km/h、percent）。
PERF_HINT = ("℃", "分钟", "秒", "毫秒", "百分比", "年", "个月", "天", "小时",
             "km/h", "km", "kg", "mAh", "Wh", "rpm", "N·m", "kW", "MW")
# 数值 + 单位的严格正则（ASCII 单位须带边界，避免命中普通英文单词）
_PERF_RE = re.compile(
    r"\d+\s*(℃|度|分钟|min|秒|s\b|km/h|km|kg|mAh|Wh|rpm|N·m|kW|MW|W\b|V\b|A\b|%)",
    re.I)
# 接口类线索
IFACE_HINT = ("接口", "通信", "协议", "信号", "总线", "CAN", "LIN", "UART",
              "SPI", "I2C", "OBD", "UDS", "API", "信号量", "针脚")

_SENT_SPLIT = re.compile(r"(?<=[。；！？\n])|(?<=[.;])\s+")


# ── 正文清洗：只留"需求条目"，剥掉代码/表格/标题/汇报（2026-11-10 真机实测）──────
# 为什么必须有这一层：后置处理拿到的是 **Agent 的整篇正文**，里面混着
#   SysML 代码块（`satisfy heatPumpCop by vehicleThermal;`）、注释分隔线
#   （`// ===== 需求实例与满足关系 =====`）、进度汇报（"**待办完成情况**：…共 5 项"）。
# 实测（run 990653）不做清洗的后果：154 行落库里有 `GAP-112 satisfy heatPumpCop by…`、
#   `REQ-F026 // ==== 需求实例…` —— **把代码当成了需求条目**，
#   31 项"需求"里大半是代码行，可建模率 21.5% 这个数字毫无意义。
# 判据口径（全部确定性，可复核）：
#   · ``` 围栏内一律丢弃（代码块整体不是需求陈述）
#   · 行首 `//` / `# ` / `--` 注释、`doc /*`、`attribute `、`requirement `、`satisfy `、
#     `part `、`package `、`private import` —— SysML 语法行
#   · 以 `;` 结尾且含 ASCII 标识符的语句行
#   · markdown 标题（`##`）、表格行（`|`）、分隔线（`---`）、纯符号行
#   · 汇报/元信息（"本轮已完成""待办完成情况""共 N 项""耗时""通过校验器"）
# 剩余内容才交给 _SENT_SPLIT 切句 —— **清洗在前、切句在后**，顺序不能反。
_CODE_LINE = re.compile(
    r"^\s*(?://|#(?!#)|--|doc\s*/\*|attribute\s|requirement\s|satisfy\s|part\s|"
    r"package\s|private\s+import|import\s|action\s|port\s|connection\s|ref\s)", re.I)
_META_LINE = re.compile(
    r"(本轮已完成|待办完成情况|共\s*\d+\s*项|耗时\s*\d|已通过官方校验器|"
    r"可直接导入建模工具|词法\s*\d+\s*错|校验通过|已入库|已生成|已完成\s*\d)")
# 元信息行（实测噪声）：`分类：功能（能效指标）｜优先级：Must｜来源：整车用户`
# 这类是**条目的属性标签**，本身不是需求陈述 ⇒ 丢弃（它的价值已在 category/source_ref 列里）。
_ATTR_LINE = re.compile(
    # 允许前导列表符号（`*说明：…` 实测形态）—— 判据要在剥前缀**之前**用，
    # 所以正则自己得容忍前导符号，不能依赖"先剥再判"的顺序。
    r"^\s*[\-*+•#]*\s*(分类|类别|优先级|来源|依据|出处|编号|版本|状态|类型|说明|备注|注)\s*[：:]")
_ASCII_STMT = re.compile(r"^[\w\s:.]*[A-Za-z_][\w]*\s*;\s*$")


def clean_requirement_text(text: str) -> str:
    """剥掉代码/表格/标题/汇报，只留可能是"需求陈述"的行。见上方判据说明。"""
    out, in_fence = [], False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line:
            continue
        # markdown 结构行
        if line.startswith("#") or line.startswith("|") or set(line) <= set("-=*_ "):
            continue
        # SysML/代码/注释行
        if _CODE_LINE.match(line):
            continue
        # 纯 ASCII 语句（`satisfy x by y;` 这类即使没被上面命中也要挡）
        if _ASCII_STMT.match(line):
            continue
        # 汇报/元信息
        if _META_LINE.search(line):
            continue
        # 元信息行整体丢弃
        if _ATTR_LINE.match(line):
            continue
        # 去掉列表前缀 / 加粗 / 「第 N 条 ·」条目编号标记，保留正文。
        # ⚠️ 必须**分两次** sub：一次 sub 里的 alternation 只能命中一个位置，
        #   而 `*第 5 条 · xxx` 需要"先剥 *、再剥 第5条·"两步（实测踩过）。
        line = re.sub(r"^[\-*+•#]+\s*", "", line)
        line = re.sub(r"^第\s*\d+\s*条\s*[·.、:)）]?\s*", "", line)
        line = re.sub(r"^\d+[.、)）]\s*", "", line)
        line = line.replace("**", "").strip()
        # 剥完可能只剩编号残留（如 "第 5 条"），过短的已在下面被 len 挡掉
        if len(line) >= 4:
            out.append(line)
    return "\n".join(out)


def _as_text(v):
    return str(v or "").strip()


def _looks_perf(s):
    return any(u in s for u in PERF_HINT) or bool(_PERF_RE.search(s))


def _looks_iface(s):
    return any(k in s for k in IFACE_HINT)


def _classify(s):
    """确定性分类：接口线索 > 性能线索 > 约束线索 > 默认功能。"""
    if _looks_iface(s):
        return "interface"
    if _looks_perf(s):
        return "performance"
    if re.search(r"(不得|禁止|必须|应|shall|must|shall not)", s):
        return "constraint"
    return "functional"


def _verifiability(s):
    """返回 (是否可验证, 原因)。不可验证的条目必须进 gaps，不许混进 items。

    ⚠️ 实测踩过：可验证动词表最初只含「能够/可以/应能/支持/实现/提供/完成」，
    结果「系统应通过 CAN 总线与整车控制器通信」被判**不可验证**扔进 gaps
    —— 但接口需求完全可验证（可通过总线测试验收）。
    ⇒ 动词表必须覆盖**接口/安全/约束类**的常见动作词。
    """
    reasons = []
    # ★★ 2026-11-10（真机实测）：**反问/澄清句不是需求**。
    #   实测：Agent 走澄清流程时正文是"⏸ 暂停等你确认：有 3 处关键口径缺失…
    #   请确认按哪种口径推进？本轮已完成：共 5 项。" —— 它含"验证"、含数字"3/5"，
    #   于是被旧判据**判成可验证**，进而被当作 5 条需求条目落库，
    #   把"可建模率"这个指标污染成毫无意义（分母全是澄清话术）。
    #   判据：需求是**陈述句**。以问号收尾、或以疑问词开头、或带显式澄清标记的，
    #   一律进 gaps（这正是"需求需要澄清"的语义，不是"需求已就绪"）。
    if re.search(r"[？?]\s*$", s) or re.match(
            r"^(是否|哪些|哪个|哪种|如何|怎么|为什么|为何|能否|可否|请确认|请说明|请补充|"
            r"需确认|待确认|本轮已完成|本轮已)", s):
        reasons.append("疑问/澄清句式（需求应为可验收的陈述句）")
    elif re.search(r"(请确认|等你确认|需要你确认|待你确认|暂停等你确认|口径缺失|请选择)", s):
        reasons.append("含澄清标记（本轮是追问而非需求产出）")
    if any(f in s for f in FUZZY):
        reasons.append("含模糊词（" + "、".join(
            f for f in FUZZY if f in s) + "）")
    if re.search(r"(良好|优秀|快速|高效|灵活|方便)", s) and not _looks_perf(s):
        reasons.append("形容词无量化判据")
    # 可验证动作词：功能 + 接口 + 安全 + 约束四类
    if not re.search(r"[0-9０-９]", s) and not re.search(
            r"(能够|可以|应能|支持|实现|提供|完成|满足|符合|"
            r"通信|交互|传输|发送|接收|连接|集成|预留|"
            r"检测|监测|报警|告警|预警|保护|联锁|追溯|"
            r"不得|禁止|必须|应当)", s):
        reasons.append("无量化指标且无可验证动词")
    return (len(reasons) == 0, "；".join(reasons))


# ── 验收标准（acceptance_criteria）确定性抽取 ───────────────────────────────
# 为什么必须在条目化阶段就产出（而不是留给下游 LLM 补）：
#   真库实测（2026-11-10）74 条需求实体的 properties 键全集只有
#   `{kind, req_id, priority}` —— **验收标准填充率 0/24、来源 0/24**。
#   Agent prompt 里明明承诺了 Given-When-Then，但没有任何结构化出口承接它，
#   于是"需求不可验收"成为甲方验收的直接卡点（下游 satisfy/verify 无判据可依）。
# 口径：本函数保证**每条进入 items 的需求都带一条可执行的验收标准**；
#   给不出验收标准的必须回到 gaps（而不是带着空验收标准进 items 混过去）。
#   —— 这条"要么有验收标准、要么是 gap"正是 P0-1 的判据，可被门禁直接断言。
_AC_MEASURE = re.compile(
    # ⚠️ 单位必须**长的排前面**：实测 `800 Ah` 被 `A` 抢先匹配 ⇒ 抽成 `800 A`，
    #   这不是"漏抽"而是**抽错**（写进模型的验收标准变成错误量纲），比缺失更糟。
    r"([0-9０-９]+(?:\.[0-9]+)?\s*(?:km/h|m/s|N·m|mAh|kWh|Wh|Ah|MPa|kPa|kN|Hz|dB|"
    r"℃|度|分钟|小时|毫秒|秒|天|个月|年|"
    r"kg|km|mm|m|rpm|kW|MW|W|V|A|%|％))", re.I)
_AC_COMPLY = re.compile(
    # 贪婪（`{2,60}` 而非 `{2,40}?`）：实测非贪婪只吃到 2 字符 ⇒
    #   「符合 ISO 11898 的要求」被抽成「IS」。标准名必须整段吃进来才可核对。
    r"(?:满足|符合|依据|参照|按照)\s*[《【\"']?([^，。；,;\n》】\"']{2,60}?)"
    r"(?:[》】\"']?\s*(?:的)?\s*(?:要求|标准|规范|条款|规定)?\s*$|[，。；,;\n]|$)")
_AC_PLAIN = re.compile(
    # 无量纲指标（COP / 效率 / 比值 / 次数）——实测「热泵 COP 不低于 2.0」曾被判成 gap，
    #   而它完全可测（读表比值）。判据：**比较词 + 数值**，有无单位都算可测。
    r"(不低于|不高于|不小于|不大于|不少于|不超过|至少|最多|达到|≥|≤|高于|低于)\s*"
    r"([0-9０-９]+(?:\.[0-9]+)?)")
_AC_VERB = re.compile(
    r"(支持|实现|提供|完成|能够|可以|应能|具备|"
    r"通信|交互|传输|发送|接收|连接|集成|预留|"
    r"检测|监测|报警|告警|预警|保护|联锁|追溯)")


def _acceptance_criteria(s):
    """从一条需求文本推出可执行验收标准。

    返回 `(text, kind)`；给不出时返回 `("", "")` —— 上游据此把它判成 gap。

    三类判据（按可执行强度排序，**取最强的那个**，不是按出现顺序）：
      · `measure`    有数值+单位（或明确的比较词+数值，如 COP≥2.0）⇒ 可直接实测比对
      · `compliance` 明确引用外部标准 ⇒ 按标准逐条核
      · `demo`       只有动作词 ⇒ 只能功能演示（弱，但**仍比空着强**：
                     它把"怎么算通过"从无变成了"跑一遍看结果"）
    """
    t = _as_text(s)
    if not t:
        return "", ""
    m = _AC_MEASURE.search(t)
    if m:
        val = m.group(1).strip()
        # 取数值前的定语作指标名（"蓄电池容量不低于 800 Ah" → "蓄电池容量"）
        head = t[:m.start()].strip(" ，,、。;；")
        head = re.sub(r"(不低于|不高于|不小于|不大于|不少于|不超过|达到|至少|最多|以上|以下|"
                      r"应|需|应当|必须|为|是|的)$", "", head).strip(" ，,、。;；")
        metric = head[-14:] if head else "该指标"
        return ("实测「%s」达到 %s（按实测记录比对，偏差不得超出规格）" % (metric, val), "measure")
    m = _AC_PLAIN.search(t)
    if m:
        head = t[:m.start()].strip(" ，,、。;；")
        metric = head[-14:] if head else "该指标"
        return ("实测「%s」%s %s（无量纲指标，按实测记录比对）"
                % (metric, m.group(1), m.group(2)), "measure")
    m = _AC_COMPLY.search(t)
    if m:
        std = _as_text(m.group(1)).strip('《》【】"\' ，。；')
        std = re.sub(r"(的)?(要求|标准|规范|条款|规定)$", "", std).strip()
        if len(std) >= 2:
            return ("逐条核对「%s」的适用条款，全部符合即通过" % std[:40], "compliance")
    m = _AC_VERB.search(t)
    if m:
        return ("功能演示：执行「%s」相关用例，观察到预期行为即通过（附操作记录）"
                % m.group(1), "demo")
    return "", ""


def _camel(name):
    """把中文/任意文本转成合法的 ASCII 标识符片段。"""
    parts = re.findall(r"[A-Za-z0-9]+", name or "")
    if parts:
        return "".join(p[:1].upper() + p[1:] for p in parts) or "Req"
    return "Req"


def itemize(text, domain="", source_ref="", project="EVTM"):
    """核心：自然语言需求 → (items, gaps, req_view_code, stats)"""
    # ★ 清洗在前、切句在后（顺序不能反）：不清洗会把 SysML 代码当需求条目。
    raw = clean_requirement_text(text)
    items, gaps = [], []
    seen_text = {}
    used_names = set()          # ⚠️ 已用标识符集合 —— 防止中文名转ASCII 后撞名

    for seg in _SENT_SPLIT.split(raw):
        s = _as_text(seg)
        if len(s) < 4:                      # 太短，多半是标题或噪声
            continue
        ok, why = _verifiability(s)
        if not ok:
            gaps.append({"text": s, "reason": why, "need_clarification": True})
            continue
        # ★ 验收标准：给不出就不许进 items（"要么有验收标准、要么是 gap"）
        ac, ac_kind = _acceptance_criteria(s)
        if not ac:
            gaps.append({"text": s, "reason": "无法导出可执行验收标准（缺量化指标/标准引用/可演示动作）",
                         "need_clarification": True})
            continue
        if s in seen_text:                  # 完全重复
            gaps.append({"text": s, "reason": "与第 %d 条重复" % seen_text[s],
                         "need_clarification": False})
            continue
        cat = _classify(s)
        rid = "REQ-%s%03d" % (CAT_CODE[cat], len(items) + 1)
        # ⚠️ 标识符**必须唯一**。实测踩过：中文需求名转ASCII 后常只剩数字
        #   （「电池温差不超过 5℃」→ `Req5`、「热泵 COP…2.0」→ `Req2`）⇒ 多条同名 requirement。
        #   规则：`Req` + 语义片段；片段为空或已占用时，用 `reqId` 数字后缀兜底。
        base = _camel(s)[:48]
        name = ("Req" + base) if base and ("Req" + base) not in used_names else None
        if name is None:
            seq = len(items) + 1
            name = "Req%s_%02d" % (base if base else "Item", seq)
            while name in used_names:                # 极端情况：仍撞名则加后缀
                name += "x"
        used_names.add(name)
        seen_text[s] = len(items) + 1
        items.append({
            "id": rid,
            "text": s,
            "category": cat,
            "subject": "",                 # 由 N3 补（需要知道哪个 part 是主体）
            "source_ref": source_ref,
            "acceptance_criteria": ac,
            "ac_kind": ac_kind,
            "ambiguous": bool(re.search(r"(等|若干|某些|适当|尽量)", s)),
            "sysml_name": name,             # 需求视图里的 requirement 名（保证唯一）
        })

    code = _build_view(items, project)
    _ac_filled = sum(1 for i in items if _as_text(i.get("acceptance_criteria")))
    stats = {"sentences": len(items) + len(gaps), "items": len(items),
             "gaps": len(gaps),
             "ac_filled": _ac_filled,
             "ac_rate": round(_ac_filled / max(1, len(items)), 3),
             "by_category": {c: sum(1 for i in items if i["category"] == c)
                             for c in CATEGORIES}}
    return items, gaps, code, stats


def _esc(text):
    """doc /* */ 里不能出现 */ ，转义。"""
    return _as_text(text).replace("*/", "*/")


def _build_view(items, project):
    """生成需求视图骨架：package + stakeholder + requirement + refine 关系。

    ⚠️ **刻意不生成 satisfy** —— 此时无design 元素，生成必然报
       `Must be a valid feature`（实测）。satisfy 由 N2/N5 补。
    """
    L = []
    L.append("package %s_Requirements {" % project)
    L.append("  //需求视图骨架（由 requirement_itemize 生成）")
    L.append("  // 约定：satisfy 分配待design 元素确定后由 N2/N5 补充")
    L.append("  private import ScalarValues::*;")
    L.append("")
    # stakeholder（按条目 subject 聚合；空则给默认占位）
    subs = sorted({i["subject"] for i in items if i.get("subject")})
    if subs:
        L.append("  // ---------- 需求主体 ----------")
        for s in subs:
            L.append("  part def %s;" % _camel(s))
        L.append("")
    L.append("  // ---------- 需求条目 ----------")
    for i in items:
        nm = i["sysml_name"]
        L.append("  requirement %s {" % nm)
        L.append("    doc /* %s */" % _esc(i["text"]))
        L.append('    attribute reqId = "%s";' % i["id"])
        L.append('    attribute category = "%s";' % i["category"])
        # 验收标准进模型（可查询、可导出）。用 attribute 而不是 verify usage：
        # 实测（2026-11-10，checker.jar）`verify ReqXxx;` 写在 package 层是**语法错**
        # （mismatched input 'verify'），而 `attribute acceptanceCriteria = "..."` n_hard=0。
        # verify 关系要等 N5 有验证用例元素后再补（与 satisfy 同一口径）。
        _ac = _as_text(i.get("acceptance_criteria"))
        if _ac:
            L.append('    attribute acceptanceCriteria = "%s";' % _esc(_ac))
        L.append("  }")
    # 派生/细化关系：**只在有明确依据时生成**
    # ⚠️ 实测踩过：早期版本按「同 category 就细化」⇒ 6 条无 subject 的需求全挂到第1 条上，
    #   等于**凭空断言了 5 条不存在的派生关系**。
    #   正确口径：细化必须有依据 —— ① 同 subject（同主体下的性能指标细化于主需求）
    #                ② 文本显式含「细分/细化/其中/之一」等关系词。
    #   否则**不生成关系**，由 N3/N5 依建模判断补。
    REL_HINT = ("细分", "细化", "其中", "之一", "进一步", "子要求", "包括", "分为")
    by = {}
    for i in items:
        sub = i.get("subject") or ""
        if sub:                                # 有主体才可能构成细化
            by.setdefault(sub, []).append(i)
    rel = []
    for sub, lst in by.items():
        if len(lst) < 2:
            continue
        base = lst[0]
        for other in lst[1:]:
            if any(h in other["text"] for h in REL_HINT):
                rel.append((other, base))
    if rel:
        L.append("")
        L.append("  // ---------- 细化关系（:> = refines，有明确依据才生成）----------")
        for o, b in rel:
            L.append("  requirement %s :> %s;" % (o["sysml_name"], b["sysml_name"]))
    L.append("}")
    L.append("  // 注：细化关系仅在「同主体 + 关系词」时生成；"
             "无依据的关系请由 N3/N5 依建模判断补。")
    return "\n".join(L)


def _render(items, gaps, code, stats, domain):
    L = ["【需求条目化】确定性规则通道（无 LLM 参与）", ""]
    L.append(f"切分 {stats['sentences']} 句 → 条目 {stats['items']} 条 / "
             f"待澄清 {stats['gaps']} 条")
    dist = "、".join(f"{c}:{n}" for c, n in stats["by_category"].items() if n)
    L.append(f"分类分布：{dist or '（无）'}")
    L.append("")
    if items:
        L.append("条目清单（每条均带可执行验收标准）：")
        for i in items:
            L.append(f"  {i['id']:12} [{i['category']:11}] {i['text'][:52]}")
            L.append(f"       ✔ 验收({i.get('ac_kind','-')})：{i.get('acceptance_criteria','')[:80]}")
    if gaps:
        L.append("")
        L.append("⚠ 待澄清（**不可直接建模**，需补量化判据）：")
        for g in gaps[:10]:
            L.append(f"  · {g['text'][:46]}")
            L.append(f"    原因：{g['reason']}")
        if len(gaps) > 10:
            L.append(f"  …… 另有 {len(gaps) - 10} 条")
    L.append("")
    L.append("需求视图骨架（已生成，可直接作为 N3 view_type=requirement 的基础）：")
    L.append("```sysml")
    L.append(code)
    L.append("```")
    L.append("")
    L.append("说明：")
    L.append("  · satisfy（需求→设计元素）**刻意未生成** —— 此时无 design 元素，"
             "生成会报 Must be a valid feature；由 N2/N5 补。")
    L.append("  · 需求项目的 subject（主体）需与架构对照，本工具留空交 N3 判定。")
    if domain:
        L.append(f"  · 领域：{domain}")
    return "\n".join(L)


def _exec(args):
    text = args.get("text") or args.get("raw_text")
    if not _as_text(text):
        return {"ok": False,
                "result": "参数 text 为空：请把待条目化的需求原文整段传入。"}
    domain = _as_text(args.get("domain"))
    source_ref = _as_text(args.get("source_ref"))
    project = _as_text(args.get("project")) or "EVTM"
    project = re.sub(r"[^A-Za-z0-9_]", "", project) or "EVTM"
    try:
        items, gaps, code, stats = itemize(str(text), domain, source_ref, project)
    except Exception as exc:                                       # noqa: BLE001
        return {"ok": False,
                "result": f"需求条目化失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表原文有问题 —— 请如实说明『未能条目化』。"}

    if not items and gaps:
        return {"ok": True,
                "result": ("【需求条目化】未能提取任何可验证条目，"
                           f"{len(gaps)} 句全部待澄清：\n"
                           + "\n".join(f"  · {g['text'][:46]} —— {g['reason']}"
                                       for g in gaps[:10])
                           + "\n\n⚠️ 不要跳过澄清直接建模 —— 不可验证的需求会产出无法验收的模型。"),
                "items": [], "gaps": gaps, "req_view_code": None,
                "stats": stats}

    return {
        "ok": True,
        "result": _render(items, gaps, code, stats, domain),
        "items": items,
        "gaps": gaps,
        "req_view_code": code,
        "stats": stats,
        "itemize_ratio": round(len(items) / max(1, stats["sentences"]), 3),
    }


def _resolve_project(conn, project: str, conversation_id: int) -> tuple:
    """工程归属：显式参数 > 会话归属 > 空串。返回 `(project_id, note)`。

    ## ⚠️ 2026-11-07 实测踩到：`project` 参数会被模型当成"包名前缀"
    `requirement_itemize` 的 `project` 是**包名**（`package EVTM_Requirements`），
    `requirement_commit` 如果照抄同一个值当"工程 id"，就会把条目挂到一个**不存在的工程**上：
    真机实测模型传了 `project="EVTM"`（规约卡默认包名），于是 3 条条目落到 `project_id='EVTM'`
    —— 又造出一批孤儿归属，且**看起来一切正常**（条目确实入库了）。
    规则：显式值**必须能在 `projects` 里查到**才认，否则回落会话归属并明确告知模型。
    另一条：**不做"猜一个默认工程"的兜底** —— 挂错工程比不挂更难排查
    （追溯链会连到别的项目的设计元素上，而且不报错）。
    """
    p = _as_text(project)
    note = ""
    if p:
        try:
            hit = conn.execute("SELECT 1 FROM projects WHERE id=?", (p,)).fetchone()
        except Exception as exc:                                # noqa: BLE001
            hit = None
            logger.warning("[requirement_commit] 工程校验失败: %s", exc)
        if hit:
            return p, ""
        note = (f"⚠️ 传入的 project=「{p}」不是已存在的工程（它更像包名前缀），"
                f"已忽略，改按会话归属落库。")
    if not conversation_id:
        return "", note
    try:
        row = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                           (int(conversation_id),)).fetchone()
        if row:
            return _as_text(row["project_id"] if hasattr(row, "keys") else row[0]), note
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[requirement_commit] 会话归属读取失败 conv=%s: %s", conversation_id, exc)
    return "", note


def commit_requirements(args, conversation_id=0, created_by="", conn=None):
    """`requirement_commit`：把条目化结果写入需求实体表（写操作，走人工确认）。

    为什么与 `requirement_itemize` 分成两个工具（本仓既有口径：query/apply 分离）：
    条目化的**分析**部分（切句、分类、生成视图骨架）是只读的，每轮都跑；
    而**落库**会产生被追溯链引用的稳定编号（REQ-F001…），是有后果的写。
    合成一个工具只有两种结果：要么只读也要逐条确认（确认疲劳），
    要么写库不确认（绕过写前闸）。两者都不可接受。
    """
    from database import get_db
    from repositories.requirement_repo import RequirementRepo

    items = args.get("items")
    gaps = args.get("gaps")
    # 允许"只给原文"——由本工具内部先条目化再落库（免去模型手工搬运 20 条）
    if not items:
        text = args.get("text") or args.get("raw_text")
        if not _as_text(text):
            return {"ok": False,
                    "result": "既没有 items 也没有 text：请传入条目化结果的 items/gaps，"
                              "或传入需求原文由本工具条目化后落库。"}
        try:
            items, gaps, _code, _st = itemize(
                str(text), _as_text(args.get("domain")), _as_text(args.get("source_ref")),
                re.sub(r"[^A-Za-z0-9_]", "", _as_text(args.get("project")) or "EVTM") or "EVTM")
        except Exception as exc:                                # noqa: BLE001
            return {"ok": False,
                    "result": f"条目化失败：{type(exc).__name__}: {exc}。⚠️ 这不代表原文有问题 —— "
                              f"请如实说明『未能条目化』，不要编造条目。"}
    # ★ 2026-11-10（门禁抓出的真缺陷）：`conn` 可传入。
    #   原实现**无条件自己 get_db()** ⇒ 任何调用方传进来的连接（比如后置处理里的
    #   run 连接、门禁的临时库夹具）都被无视，写操作永远落到生产库。
    #   症状极具欺骗性：门禁用临时库跑，"落库成功"却查不到行；而生产库被悄悄写脏。
    #   口径与 `agent/todo_store.handle_todo_write(conn=...)` 一致：
    #   默认 None ⇒ 生产连接（行为不变），显式传入 ⇒ 用给定连接且**不关闭它**（调用方所有）。
    _own_conn = conn is None
    if _own_conn:
        conn = get_db()
    try:
        pid, _pnote = _resolve_project(conn, args.get("project"), conversation_id)
        repo = RequirementRepo(conn)
        # SQLite 单写者：调用方可能正持有一个写事务（编排链路里常见）。
        # 只对**锁冲突**做有限重试（不是对任何异常都重试）——否则"落库失败"会被
        # 伪装成"落库成功"，正是本仓记录过的静默兜底事故。
        _attempt = 0
        while True:
            try:
                res = repo.commit(
                    items or [], gaps or [], project_id=pid,
                    conversation_id=int(conversation_id or 0),
                    version_id=int(args.get("version_id") or 0),
                    source_ref=_as_text(args.get("source_ref")),
                    domain=_as_text(args.get("domain")), created_by=created_by)
                break
            except Exception as _e:                             # noqa: BLE001
                _msg = str(_e).lower()
                if "database is locked" in _msg and _attempt < 3:
                    _attempt += 1
                    time.sleep(0.25 * _attempt)
                    continue
                raise
        st = repo.stats(pid)
        conn.commit()
    except Exception as exc:                                    # noqa: BLE001
        # ⚠️ 不能静默兜底：落库失败若返回"成功"，模型会据此宣称"已入库"，
        #    而实际一条都没有 —— 这正是本仓记录过的"静默丢弃"同型事故。
        return {"ok": False,
                "result": f"需求条目落库失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 请如实说明『未能入库』，不要声称已入库。"}
    finally:
        if _own_conn:      # 只关自己开的连接（调用方传入的连接由调用方关闭）
            try:
                conn.close()
            except Exception:
                pass
    head = (f"【需求入库】工程={pid or '（无工程会话）'}：新增 {res['inserted']} 条 / "
            f"已存在更新 {res['updated']} 条；当前该工程共 "
            f"{st['items']} 条需求、{st['gaps']} 条待澄清（可建模率 {st['itemize_ratio']:.1%}）"
            # ★ P0-1：验收标准覆盖率必须显式回报 —— 它是"需求可验收"的直接判据，
            #   不报出来就没人会发现它掉到 0（真库历史值就是 0/24 而无人察觉）。
            f"；验收标准覆盖 {st.get('ac_filled', 0)}/{st['items']} "
            f"({st.get('ac_rate', 0.0):.1%})")
    if _pnote:
        head = _pnote + "\n" + head
    tail = []
    if res["items"]:
        tail.append("需求编号：" + "、".join(res["items"][:12])
                    + ("…" if len(res["items"]) > 12 else ""))
    if res["gaps"]:
        tail.append("待澄清编号：" + "、".join(res["gaps"][:12])
                    + ("…" if len(res["gaps"]) > 12 else ""))
    if st["items"] and st.get("ac_rate", 0.0) < 1.0:
        head += ("\n⚠️ 有 %d 条需求缺验收标准（不可验收，下游无法判通过/失败）。"
                 "请补齐或用 requirement_itemize 重新条目化。" % (st["items"] - st.get("ac_filled", 0)))
    return {"ok": True, "result": "\n".join([head] + tail),
            "project_id": pid, **res, "stats": st}


def exec_tool(name: str, arguments: dict | None = None, conversation_id: int = 0,
              created_by: str = "") -> dict:
    args = arguments or {}
    if name == "requirement_itemize":
        return _exec(args)
    if name == "requirement_commit":
        return commit_requirements(args, conversation_id=conversation_id, created_by=created_by)
    return {"ok": False, "result": f"未知需求工具: {name}"}


REQ_TOOL_NAMES = ("requirement_itemize", "requirement_commit")


if __name__ == "__main__":
    demo = ("EV 热管理系统需求：系统应支持 30 分钟低温启动，-20℃ 环境下 30 分钟内完成充电与放电准备；"
            "电池工作温度在 15~45℃ 之间；电池温差不超过 5℃；"
            "热管理总能耗不超过整车可用能量的 15%；"
            "热失控预警应至少提前 5 分钟触发保护动作并自动执行；"
            "系统应具有良好的用户体验；热泵 COP 在 -10℃ 时不低于 2.0；"
            "系统应通过 CAN 总线与整车控制器通信。")
    r = _exec({"text": demo, "domain": "thermal_mgmt", "project": "EVTM"})
    print(r["result"])
    print("\n--- 结构化字段 ---")
    for k in ("ok", "itemize_ratio"):
        print(f"  {k} = {r.get(k)}")
    print(f"  items = {len(r.get('items', []))} 条")
    print(f"  gaps  = {len(r.get('gaps', []))} 条")
    print(f"  stats = {r.get('stats')}")
