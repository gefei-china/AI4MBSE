"""最终正文的「中间版本代码收敛」（2026-11-02，米爸反馈）。

需求原话：「输出的代码块总是包括多份数据，视图也是，逻辑上只需要承载最后确定的代码，
不应该把中间环节的代码放在这里，如果需要在中间环节呈现，应该在其他部分展示」。

── 问题形态（真库实证，conv 9999 / msg 5689，32308 字）────────────────────
自动编排的各子任务交付物**各自带一份完整 SysML 模型代码**（t1 修正版 v1 / t2 修正版 v2 /
t3 修正版片段 v3），汇总（LLM 整合或拼接降级）原样带入最终正文 ⇒ 用户在最终输出里看到
3 份 ```sysml 代码块 + 3 段重复的诊断章节。中间版本在**子任务执行卡**里本就可见
（`_procS.subOutput` → `card.liveOutput`），最终正文重复粘贴只增加噪音。

── 修法 ────────────────────────────────────────────────────────────────
对最终正文做一次**呈现层收敛**：同一语言族内、判定为「同一交付物的演进版本」的
围栏代码块，只保留**最后一份**（= 最后确定的版本），早期版本替换为一行指引
（指向子任务执行卡与下文最终版）。**一字代码不删**——只是不再在正文重复展示。

判据（宁可漏判、不可误杀）：
  · 完全相同（空白归一化后）⇒ 重复，收敛；
  · SysML 族（sysml/kerml…）：**根 package 名相同** ⇒ 同一模型的迭代版本，收敛；
    （package 名是 SysML 交付物的身份标识；不同视图/不同模型不会同名）
  · 其它语言：相似度 ≥ 0.60（difflib）或一方被另一方包含 ⇒ 演进版本，收敛；
  · 仅一块、或判据不命中 ⇒ 原样保留，绝不误杀合法的不同代码块。

链式判定：t1≈t2、t2≈t3 但 t1 vs t3 相似度可能不足 ⇒ 从后往前走，
每个早期块与「保留块」或「相邻块」比较，相邻链可传递。

挂钩点（三处，见各自调用注释）：
  1. workflows/planner.py::_summarize_plan —— LLM 整合输出后 + 拼接降级后；
  2. agent/pipeline_parts/stream.py —— 流式路径收尾（reflection/质量门禁之后、
     流式推送与 _ensure_sysml_blocks 之前；reflection 可能整体重写正文，必须最后再收一次）；
  3. 非流式路径经 run_planner_plan → _summarize_plan 已覆盖，无需另挂。
"""
import logging
import re
from difflib import SequenceMatcher

# ★★ 2026-11-19：本模块此前**一个 logger 都没有**，三处 `except Exception` 全部
#   `return 原文` 静默 —— `_SIM_OTHER` 未定义导致的 NameError 就是这样被藏了三个月
#   （用户反复反馈「代码重复」，每轮都在调判据，而函数根本没跑通）。
#   ⇒ 纪律：呈现层可以 fail-open（正文原样交付），但**必须留痕**。
_logger = logging.getLogger("mbse.final_content")

# 围栏代码块：```lang\n ... ```（非贪婪；正文是模型/模板生成的 markdown，无嵌套围栏场景）
_FENCE_RE = re.compile(r"```([\w.+\-]*)[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)

# SysML/KernelML 语言标签归一族（大小写不敏感）
_SYSML_LANGS = {"sysml", "sysmlv2", "sysml_v2", "sysmlv1", "kerml", "kerml2", "kermlv2"}

# 早于该长度的小片段不参与收敛（一两行的片段重复无伤大雅，误杀风险 > 收益）
_MIN_BLOCK_CHARS = 400

# 其它语言的「演进版本」相似度阈值。
# ⚠️ 实测教训（本模块自测 case4）：不同 SysML 模型因**模板化样板**（同类 comment/part 写法）
# 相似度可以 >0.9 ⇒ SysML 族**绝不用相似度兜底**（签名不同就是不同交付物）；
# 其它语言样板化同样普遍，门槛取 0.75 高位，且要求同语言标签。
# ⚠️⚠️ 2026-11-19：**这个常量自 2026-08-xx 上线起就从未被定义过**。
#   上面这句注释写了「门槛取 0.75」，但赋值漏了 ⇒ `_is_interim` 在
#   `family not in (sysml, plain)` 时抛 NameError ⇒ 被
#   `dedupe_interim_code_blocks` 外层的 `except Exception: return text, 0`
#   静默吞掉 ⇒ **代码块去重一次都没真正跑过**。
#   实锤：msg#5036(text×2)→ 收敛 0 处（本该收敛 2 处），
#         msg#5135(sysml×7)→ 正常收敛 6 处（sysml 分支不走这行，所以看不出问题）。
#   这就是「同一功能修了多次仍复现」的真因：判据没问题，函数没跑通。
#   静态发现工具：tools/verify/find_undefined_names.py（已接入门禁）。
_SIM_OTHER = 0.75

_NOTE = ("> ▦ 中间版本代码已收敛，此处不再重复展示：最终确定版见下文代码块；"
         "各中间版本完整内容可在上方对应子任务执行卡中查看。")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _lang_family(lang: str) -> str:
    l = (lang or "").strip().lower()
    if l in _SYSML_LANGS:
        return "sysml"
    return l or "plain"


def _sysml_signature(code: str) -> str:
    """SysML 交付物身份：首个 `package X` / `package 'X'`；否则首个 `xxx def Y`。

    同名 ⇒ 视为同一模型的迭代版本（判据来自需求方语义：正文只承载最后确定的代码）。

    ★★ 2026-11-19（**这是"同一份模型出现两份"的第二个根因**）：
      原实现只认**带引号**的写法 —— `package\\s+'([^']+)'`。
      但本系统生成的 SysML 大量是**不带引号**的（`package CabinCoolingActivity {`，
      真库 #6979 两份块首行完全相同、长度 6848/6569）。
      带引号判据对它们恒返回 `''` ⇒ `_is_interim` 走 `bool(se and sl and se == sl)`
      ⇒ 空签名 ⇒ **永远判"不是同一模型"** ⇒ 两份都留下。
      ⇒ 现在两种写法都认（引号优先，因为「元素名用单引号」是平台规范；
        无引号时用标识符，这是 SysML v2 的合法写法，不能因为"不规范"就当作不存在）。
    """
    m = (re.search(r"\bpackage\s+'([^']+)'", code)
         or re.search(r'\bpackage\s+"([^"]+)"', code)
         or re.search(r"\bpackage\s+([A-Za-z_][\w.]*)", code))
    if m:
        return "pkg:" + m.group(1)
    m = (re.search(r"\b(?:part|requirement|action|attribute|port|interface|use\s+case|"
                   r"activity|state|constraint|item|connection)\s+def\s+'([^']+)'", code)
         or re.search(r"\b(?:part|requirement|action|attribute|port|interface|use\s+case|"
                      r"activity|state|constraint|item|connection)\s+def\s+([A-Za-z_][\w]*)",
                      code))
    if m:
        return "def:" + m.group(1)
    return ""


def _ngram_sim(a: str, b: str, n: int = 3) -> float:
    """字符 3-gram Jaccard 相似度（去空白后），范围 0~1。

    ★★ 2026-11-19：**统一度量**。此前本文件用 `SequenceMatcher`（`_similar`），
      而 `agent/auto_continue.py` 用 3-gram Jaccard —— 同一语义两套度量，必然错位：
      真库 #6581 两份 json 块 Jaccard = **0.876**（明显是同一份的重述），
      但 SequenceMatcher 对 8000 字文本给出 < 0.75 ⇒ dedupe 判"不同"⇒ 两份都留。
      （本仓纪律：同名语义只允许一个判据出处。这里选定 Jaccard，因为
        auto_continue 已用它做过完整标定：重述 0.85~0.96、无关 0.018~0.024，
        分离度远好于 SequenceMatcher 在长文本上的表现。）
    ⚠️ 分母用并集（Jaccard），不是 Dice —— 与 auto_continue 的 `_ngram_sim` 同实现。
    """
    x = " ".join(str(a or "").split())
    y = " ".join(str(b or "").split())
    if len(x) < n or len(y) < n:
        return 1.0 if x == y else 0.0
    ga = {x[i:i + n] for i in range(len(x) - n + 1)}
    gb = {y[i:i + n] for i in range(len(y) - n + 1)}
    return len(ga & gb) / float(len(ga | gb) or 1)


def _similar(a: str, b: str) -> float:
    # 长文本全量比对太贵：截前 2 万字符（版本比对足够）
    return SequenceMatcher(None, a[:20000], b[:20000]).ratio()


def _is_interim(earlier: str, later: str, family: str) -> bool:
    """earlier 是否为 later 的中间版本。判据刻意保守：宁可漏判，不可误杀。"""
    ne, nl = _norm(earlier), _norm(later)
    if not ne or not nl:
        return False
    if ne == nl:
        return True                      # 完全相同（空白归一化后）
    if len(ne) < _MIN_BLOCK_CHARS or len(nl) < _MIN_BLOCK_CHARS:
        return False
    if ne in nl or nl in ne:
        return True                      # 一方被另一方包含 = 子集/超集演进
    if family == "sysml":
        # SysML 族：签名（根 package/def）相同 ⇒ 同一模型的迭代版本。
        # ⚠️ 不做相似度兜底——不同模型的样板结构相似度可以极高（自测 case4 实证）。
        se, sl = _sysml_signature(earlier), _sysml_signature(later)
        return bool(se and sl and se == sl)
    if family == "plain":
        return False                     # 无标签块最保守：只认相同/包含
    # ★ 2026-11-19：改用 3-gram Jaccard（与 auto_continue 同一度量）。
    #   原用 `_similar`（SequenceMatcher）：真库 #6581 两份 json 块 Jaccard=0.876
    #   却被 SequenceMatcher 判 <0.75 ⇒ 两份都留。长文本上 SequenceMatcher 会因
    #   整串对齐而给出偏低分数，Jaccard 对"同一份东西重述"更敏感。
    return _ngram_sim(ne, nl) >= _SIM_OTHER


_CN_NUM = "一二三四五六七八九十"


def _cn_to_int(s: str) -> int:
    """中文序号 → 整数（只覆盖标题里真实会出现的 1~99）。解析不了返回 0。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_NUM.index(a) + 1 if a in _CN_NUM else 0
        # ⚠️ `b in _CN_NUM` 对空串恒为 True（`"" in "一二三"` == True），
        #   必须显式判 `b` 非空 —— 否则「二十」会被算成 2*10+1=21（本轮自测抓到的真 bug）。
        ones = _CN_NUM.index(b) + 1 if (b and b in _CN_NUM) else 0
        return tens * 10 + ones
    return (_CN_NUM.index(s) + 1) if s in _CN_NUM else 0


def _int_to_cn(n: int) -> str:
    """整数 → 中文序号（1~99）。"""
    if n <= 0:
        return ""
    if n <= 10:
        return _CN_NUM[n - 1]
    if n < 20:
        return "十" + (_CN_NUM[n - 11] if n > 10 else "")
    tens, ones = divmod(n, 10)
    return _CN_NUM[tens - 1] + "十" + (_CN_NUM[ones - 1] if ones else "")


# 中文序号标题：`## 一、标题` / `### 二. 标题` / `#### 三 标题`
_ORD_HEAD_RE = re.compile(
    r"^(#{2,6})[ \t]*([一二三四五六七八九十]+)[ \t]*([、.．)）][ \t]*)(?=\S)", re.M)


def normalize_ordinal_headings(text: str):
    """把**因拼接而重复/回退**的中文序号标题，重排为一条连续序号。返回 (new_text, changed)。

    ★ 2026-11-04（真库实证 conv 990408 / msg 5761）：
      自动编排汇总时若"整合"退化成拼接，正文就是 3 份报告首尾相接 ——
      每份各自从「一、」开始，于是出现
        `## 一、结论先行 … ## 六、风险` → `## 二、本次实际做了什么` → `## 一、结论先行`（重开）
      读者看到「二」排在「六」后面、且「一」出现两次，完全失去章节层次。

    ── 判据（刻意保守：**只在已经坏掉的文档上动手**）────────────────────
      · 按标题层级（`##`/`###`…）**分组独立**处理（不同层级各有自己的序号体系）；
      · 组内序号**严格单调递增** ⇒ 视为正常文档，**一字不动**（返回原文、changed=0）；
      · 出现**回退或重复**（后一个 ≤ 前一个）⇒ 判定为拼接痕迹 ⇒ 该组整体重排为 1..n；
      · **只改序号本身**，标题文字、层级符号 `#`、分隔符 `、` 全部原样保留；
      · 只有 1 个带序号标题 ⇒ 不动（无从判断，且改了没意义）。

    ⚠️ 为什么不用"输出侧去重标题"而用"重排序号"：
      重复的标题文字往往**内容并不相同**（是不同子任务的不同结论），删掉会丢信息；
      而序号回退才是"读者认知错乱"的真正来源。**改序号不删内容**。
    """
    s = str(text or "")
    if not s:
        return s, 0
    hits = list(_ORD_HEAD_RE.finditer(s))
    if len(hits) < 2:
        return s, 0
    # 按层级分组（保持出现顺序）
    groups = {}
    for m in hits:
        groups.setdefault(m.group(1), []).append(m)
    out, pos, changed = [], 0, 0
    for level, ms in groups.items():
        if len(ms) < 2:
            continue
        nums = [_cn_to_int(m.group(2)) for m in ms]
        if any(n <= 0 for n in nums):
            continue
        # 严格单调递增 ⇒ 正常文档，不动
        if all(nums[i] > nums[i - 1] for i in range(1, len(nums))):
            continue
        # 回退/重复 ⇒ 重排
        for i, m in enumerate(ms, start=1):
            new = _int_to_cn(i)
            if new == m.group(2):
                continue
            out.append(s[pos:m.start(2)])
            out.append(new)
            pos = m.end(2)
            changed += 1
    if not out:
        return s, 0
    out.append(s[pos:])
    return "".join(out), changed


def close_open_fences(text: str) -> str:
    """闭合正文末尾**未配对**的 ``` 围栏。已配对则**一字不动**地原样返回。

    ★ 2026-11-04（真库实证 conv 990408 / msg 5761，21432 字，围栏 13 个=奇数）：
      流式连接中断时，正文可能正好停在代码块中间 ⇒ 随后追加的
      「⏹ 连接已中断」提示直接**落进代码块内部** ⇒ 用户看到提示被当成代码高亮，
      后续正文也被吞进代码块。

    ⚠️ 只在**确实存在未配对**时补一个闭合围栏；已配对的一律不动 ——
      本函数在**呈现层**收尾处调用，多加一个 ``` 会让正常正文多出围栏。
    """
    s = str(text or "")
    if "```" not in s:
        return s
    n, pos = 0, 0
    while True:
        i = s.find("```", pos)
        if i < 0:
            break
        n += 1
        pos = i + 3
    if n % 2 == 0:          # 已配对 ⇒ 原样返回（不改一个字）
        return s
    return s + "\n```\n"


# ══════════════════════════════════════════════════════════════════════════════
# 段级收敛：多次 attempt 把「同一结论」讲了几遍（2026-11-19，msg#7039 实证）
#
# 现象：21,864 字回复里「## 结论：17 项待办…」出现 **3 遍**（措辞各不相同）。
# 实测：三次 attempt 两两相似度只有 **0.39 / 0.45**（长度比 3.05 / 0.90）
#   ⇒ **任何相似度判据都抓不到**（0.39 远低于 0.75 门槛）——
#     这就是为什么前两轮"调阈值"完全无效：判据方向错了。
# 而它们的形态是**确定**的：一份回复只会有一个「结论」；出现两个以上就是拼接痕迹。
#
# 判据（刻意窄，只认结构性重复）：
#   · 找 `## 结论/总结/综上…` 型二级标题；
#   · 数量 ≤1 ⇒ 一字不动（正常文档）；
#   · 数量 ≥2 ⇒ 保留**最长**的一段（信息量最大），其余替换为一行指引；
#   · 只动 `##` 级（`#` 是文档标题、`###` 是子项，都不参与）；
#   · 还要求「起手式重合 ≥6 字」或「正文相似 ≥ _SEC_RESTATE_SIM」——
#     避免把"分主题的两个结论"误当重复（正常文档允许有两个结论）。
# ══════════════════════════════════════════════════════════════════════════════

#: 「结论型」二级标题（一份正常回复只会有一个）
_CONCL_HEAD_RE = re.compile(
    r"^##[ \t]*(?:结论|总体结论|总结|小结|综上|结论先行|最终结论)", re.M)
#: `##` 级标题行（切段边界；不含 `#` / `###`）
_H2_RE = re.compile(r"^##[ \t]", re.M)

#: 段级「同一结论重述」的正文相似度门槛（比块级 0.75 低：段更长、模板成分更多）
_SEC_RESTATE_SIM = 0.55

_SECTION_NOTE = ("\n> ▦ 本节为同一结论的重复陈述，已收敛；完整版见上文，"
                 "原内容可在同轮执行过程卡中查看。\n")


def collapse_restated_sections(text: str):
    """收敛重复的「结论型」小节。返回 (new_text, collapsed_count)。

    ⚠️ 只在**确定是拼接痕迹**时动手；任何异常 fail-open（正文原样交付），
       但调用方必须留痕（本仓纪律：静默兜底 = bug）。
    """
    try:
        s = str(text or "")
        heads = list(_CONCL_HEAD_RE.finditer(s))
        if len(heads) < 2:
            return s, 0
        bounds = [m.start() for m in _H2_RE.finditer(s)]
        secs = []
        for h in heads:
            nxt = next((b for b in bounds if b > h.start()), len(s))
            nl = s.find("\n", h.start())
            head_txt = s[h.start():(nl if nl > 0 else len(s))]
            secs.append({"start": h.start(), "end": nxt,
                         "text": s[h.start():nxt], "head": head_txt})
        best = max(range(len(secs)), key=lambda i: len(secs[i]["text"]))

        def _lead(t):
            return re.sub(r"^##[ \t]*(?:结论|总体结论|总结|小结|综上|结论先行|最终结论)"
                          r"[：: \t]*", "", t).strip()

        drop = set()
        for i in range(len(secs)):
            if i == best:
                continue
            a, b = _lead(secs[i]["head"]), _lead(secs[best]["head"])
            same_lead = False
            for k in range(min(len(a), len(b)), 5, -1):
                if a[:k] == b[:k]:
                    same_lead = True
                    break
            sim = _similar(_norm(secs[i]["text"]), _norm(secs[best]["text"]))
            if same_lead or sim >= _SEC_RESTATE_SIM:
                drop.add(i)
        if not drop:
            return s, 0
        out, pos = [], 0
        for i, sec in enumerate(secs):
            if i not in drop:
                continue
            out.append(s[pos:sec["start"]])
            out.append(_SECTION_NOTE)
            pos = sec["end"]
        out.append(s[pos:])
        return "".join(out), len(drop)
    except Exception as _e:                              # noqa: BLE001
        _logger.warning(
            "collapse_restated_sections 失败（正文原样交付）：%s", str(_e)[:200])
        return text, 0


def block_fingerprints(text: str) -> set:
    """正文里所有围栏块的「内容指纹」集合（去空白后取前 2000 字哈希）。"""
    import hashlib
    out = set()
    for _lang, body in _FENCE_RE.findall(str(text or "")):
        n = _norm(body)
        if len(n) < _MIN_BLOCK_CHARS:
            continue
        out.add(hashlib.md5(n[:2000].encode("utf-8")).hexdigest())
    return out


def carry_over_unique_blocks(prev: str, cur: str) -> str:
    """把 prev 里**cur 没有**的围栏块补到 cur 末尾（续跑替换时防丢交付物）。

    背景：续跑第 2 轮常常**重新写一份完整回复**（不含上一轮那个提示词块），
    直接 `return cur` 会把用户真正要的成品丢掉。实测 msg#7039：
    attempt2 有提示词块、attempt3 没有 ⇒ 若纯替换，提示词就没了。

    ⚠️ 「有没有」必须用 `_is_interim`（同族 + 相似度）判定，**不能用 md5**：
       实测 #7039 的两份提示词只差 **2 个字**（2268 vs 2266），md5 完全不同
       ⇒ 按 md5 判会判成"cur 里没有"⇒ 补回去 ⇒ 又是两份提示词。
       这正是「同一语义用两套判据必然错位」的又一次现场。
    """
    try:
        cur_blocks = [b for _l, b in _FENCE_RE.findall(str(cur or ""))
                      if len(_norm(b)) >= _MIN_BLOCK_CHARS]
        carry = []
        for lang, body in _FENCE_RE.findall(str(prev or "")):
            n = _norm(body)
            if len(n) < _MIN_BLOCK_CHARS:
                continue
            fam = _lang_family(lang)
            dup = False
            for cb in cur_blocks:
                if _is_interim(n, _norm(cb), fam):
                    dup = True
                    break
            if not dup and any(_is_interim(n, _norm(cb[1]), fam) for cb in carry):
                dup = True
            if dup:
                continue
            carry.append((lang or "", body.rstrip()))
        if not carry:
            return cur
        return (str(cur).rstrip() + "\n\n"
                + "## 上一轮产出（本轮未重复生成，原样保留）\n\n"
                + "\n\n".join("```%s\n%s\n```" % (lg, b) for lg, b in carry)
                + "\n")
    except Exception as _e:                              # noqa: BLE001
        _logger.warning(
            "carry_over_unique_blocks 失败（返回 cur）：%s", str(_e)[:200])
        return cur


def dedupe_interim_code_blocks(text: str):
    """收敛最终正文中的中间版本代码块。返回 (new_text, removed_count)。

    设计约束：
      · 绝不抛异常（调用点都在编排收尾主链上，呈现层失败不能影响交付）；
      · 每个语言族独立处理；族内只保留最后一块，早期块按判据替换为指引行；
      · 替换不改变块外任何文本（章节标题/摘要原样保留 —— 需求只针对代码块）。
    """
    try:
        s = str(text or "")
        if "```" not in s:
            return s, 0
        blocks = list(_FENCE_RE.finditer(s))
        if len(blocks) < 2:
            return s, 0
        # 分语言族（保序）
        groups = {}
        for i, m in enumerate(blocks):
            groups.setdefault(_lang_family(m.group(1)), []).append(i)
        # ★ 2026-11-19（真库 #2780 实证）：`_MIN_BLOCK_CHARS=400` 会把**完全相同**的
        #   短块也放过 —— 那份消息里 `part def 载荷` 段 267 字**原样重复 3 遍**，
        #   三份都不 eligible ⇒ 一处都没收敛。
        #   ⇒ 「空白归一后完全相同」本身就是**确定重复**，不该受长度门槛保护；
        #      只有"相似但不完全相同"才需要 400 字门槛（防误杀合法的小片段对照）。
        #   下限 40 字：`package P {}` 这种 12 字占位**不是**重复问题（真库 #7039 就有），
        #   收敛它反而会删掉模型随手写的空包声明。
        _norm_cache = {i: _norm(blocks[i].group(2)) for i in range(len(blocks))}

        def _eligible(i, idxs):
            n = _norm_cache[i]
            if len(n) >= _MIN_BLOCK_CHARS:
                return True
            if len(n) < 40:
                return False
            return any(j != i and _norm_cache[j] == n for j in idxs)

        drop = {}   # block index -> note text
        for fam, idxs in groups.items():
            eligible = [i for i in idxs if _eligible(i, idxs)]
            if len(eligible) < 2:
                continue
            keep = eligible[-1]          # 最后一块 = 最后确定的版本
            for j in range(len(eligible) - 2, -1, -1):
                cur = eligible[j]
                nxt = eligible[j + 1]
                ref = blocks[keep].group(2)
                if _is_interim(blocks[cur].group(2), ref, fam) or \
                   _is_interim(blocks[cur].group(2), blocks[nxt].group(2), fam):
                    drop[cur] = _NOTE
        if not drop:
            return s, 0
        out, pos, n = [], 0, 0
        for i, m in enumerate(blocks):
            if i in drop:
                out.append(s[pos:m.start()])
                out.append(drop[i])
                pos = m.end()
                n += 1
        out.append(s[pos:])
        return "".join(out), n
    except Exception as _e:                              # noqa: BLE001
        _logger.warning(
            "dedupe_interim_code_blocks 失败（正文原样交付）：%s", str(_e)[:200])
        return text, 0
