# -*- coding: utf-8 -*-
"""主↔子上下文传递的压缩与引用层（2026-11-02，米爸「三个改进点」）。

## 行业依据（本轮调研，落到本仓的三条）

| 来源 | 结论 | 本模块对应 |
|---|---|---|
| Anthropic《Effective Context Engineering》 | write / select / compress / isolate 四操作；子 Agent 探索数万 token，**只回 1-2k token 摘要** | `smart_excerpt`（compress 不切代码）；协议摘要优先（select） |
| Google ADK | working context 是「compiled view」；artifacts **按名引用，不内联**；「Share memory by communicating, don't communicate by sharing memory」 | `plan_subtask_attachments`（未声明 → 清单 + 取回指引，不内联） |
| Claude Code subagent | 子上下文由**显式 allow-list** 组装，不是继承；「想传全部以防万一」⇒ 说明任务边界不清 | 同上（默认不倾倒全量附件） |
| OpenAI Agents SDK handoff | 边界是**契约**：schema 暴露给模型、运行时本地校验；校验失败「reject, don't repair」，带错误重试一次 | `services/subtask_protocol` 的容错 + 单次补格式 |

## 为什么「硬截 400 字」是错的

`build_subtask_context` 原实现 `str(result)[:400]`：
· 可能切在 ```sysml 代码块中间 ⇒ 下游拿到半份代码 + 半份正文，**两半都不可用**；
· 也可能切在结论之前 ⇒ 只留过程、丢掉结论（`_head_tail_clip` 早已在同一仓里
  以「头 60% + 尾 40%」解决过同类问题，但子任务快照这一路没跟上）。

本模块给出**围栏安全 + 头尾采样**的通用截断，两个调用点共用同一真源。
"""
from __future__ import annotations

import re

#: 围栏代码块（与 agent/final_content.py 同口径；语言标签可选）
_FENCE_RE = re.compile(r"```[\w.+\-]*[ \t]*\r?\n.*?\r?\n[ \t]*```", re.S)

#: 截断标记（下游看得见"这里少了东西"，并知道去哪里取）
MARK_MID = "\n…（中间已省略 {n} 字符，完整内容可用 tool_result_fetch 取回）…\n"
MARK_TAIL = "\n…（以下内容已省略 {n} 字符，完整内容可用 tool_result_fetch 取回）…\n"


def _fence_spans(text: str) -> list:
    """返回所有围栏代码块在 text 中的 [start, end) 区间。"""
    return [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]


def _safe_cut(text: str, pos: int, spans: list, forward: bool = False) -> int:
    """把切点挪到**围栏外**。

    · forward=False（头切点）：落在围栏内 ⇒ 挪到该围栏**起点**（宁可少给正文，
      也不给半份代码）；
    · forward=True（尾起点）：落在围栏内 ⇒ 挪到该围栏**末尾**（不从代码中间开始读）。
    """
    for s, e in spans:
        if s < pos < e:
            return e if forward else s
    return pos


def _prose_gaps(text: str, spans: list) -> list:
    """围栏之外的连续文本区间（"散文段"）。"""
    gaps, prev = [], 0
    for s, e in spans:
        if s > prev:
            gaps.append((prev, s))
        prev = max(prev, e)
    if prev < len(text):
        gaps.append((prev, len(text)))
    return gaps


def _tail_slice(text: str, spans: list, budget: int) -> str:
    """从尾部往前累加**散文段**，直到 budget 用尽；代码块整段跳过。

    为什么跳过而不是截进去：半份代码对下游零价值，还会让下游误以为模型写崩了。
    为什么继续往前取：结论/风险常紧跟在代码块**之前**（"## 结论" 在 ``` 之上），
    只取最后一个 gap 会刚好把结论丢掉（本函数第一版就是这么错的）。
    拼接处用 `⟪…⟫` 明示"这里有内容被跳过"，不制造内容连续性的错觉。
    """
    if budget <= 0:
        return ""
    gaps = _prose_gaps(text, spans)
    picked, used = [], 0
    for a, b in reversed(gaps):
        if used >= budget:
            break
        seg = text[a:b]
        take = min(len(seg), budget - used)
        if take <= 0:
            continue
        picked.append(seg[len(seg) - take:])
        used += take
    if not picked:
        return ""
    # picked 是从尾往前的顺序 ⇒ 反转后按正序拼接；段之间用占位标记提示跳过
    return "\n⟪…（中间含代码块/正文，已省略）…⟫\n".join(reversed(picked))


def smart_excerpt(text: str, cap: int, tail_ratio: float = 0.35) -> tuple:
    """结构化截断：返回 (excerpt, omitted_chars)。

    规则（按优先级）：
      ① 不超 cap ⇒ 原样返回，omitted=0（**逐字节不变**，零回归面）；
      ② cap 过小（< 120）⇒ 只取头（避免头尾两段都碎得没用）；
      ③ 否则**头 (1-tail_ratio) + 尾 tail_ratio** —— 结论/风险/建议通常落在尾部
         （与 `workflows/planner.py::_head_tail_clip` 同一判据）；
      ④ 两段都**只在围栏外侧取**：切进 ```sysml 块会把代码劈成不可用的两半，
         这是本函数存在的首要原因。尾部若落在围栏内，**跳过该围栏继续往前取**
         （结论常在代码块之前，否则会刚好丢结论 —— 第一版实测错误）；
      ⑤ 无法安全切 ⇒ 退化为只取头到安全边界（宁短勿裂）。
    """
    s = str(text or "")
    cap = int(cap or 0)
    if cap <= 0 or len(s) <= cap:
        return s, 0
    spans = _fence_spans(s)
    if cap < 120:
        cut = _safe_cut(s, cap, spans)
        if cut <= 0:
            cut = min(cap, len(s))
        return s[:cut] + MARK_TAIL.format(n=len(s) - cut), len(s) - cut

    tail_len = max(0, int(cap * max(0.0, min(0.6, tail_ratio))))
    head_len = cap - tail_len
    head_cut = _safe_cut(s, head_len, spans)
    if head_cut <= 0:
        # 第一个围栏就顶在最前面 ⇒ 头段无可安全内容，改为只取尾（结论优先）
        tail_only = _tail_slice(s, spans, cap)
        if tail_only:
            omitted = max(0, len(s) - len(tail_only))
            return MARK_TAIL.format(n=omitted) + tail_only, omitted
        cut = min(cap, len(s))
        return s[:cut] + MARK_TAIL.format(n=len(s) - cut), len(s) - cut

    head = s[:head_cut]
    tail = _tail_slice(s, spans, tail_len)
    # 尾部内容若整体落在 head 之后 ⇒ 省略量 = len(s) - len(head) - len(tail)（近似）
    omitted = max(0, len(s) - len(head) - len(tail))
    if not tail:
        return head + MARK_TAIL.format(n=omitted), omitted
    return head + MARK_MID.format(n=omitted) + tail, omitted


# ── 附件：默认不内联，给「清单 + 取回指引」 ────────────────────────────────

#: 单个附件在「自动」模式下允许内联的体积上限（字节）。超过即只给清单
AUTO_INLINE_BYTES = 200_000
#: 「自动」模式下允许内联的文本附件**总个数**上限
AUTO_INLINE_COUNT = 2
#: 图片直传上限（视觉通道无法"按需读取"，但也不能无限塞）
AUTO_MAX_IMAGES = 4

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def _is_image(a: dict) -> bool:
    if a.get("is_image"):
        return True
    fn = str(a.get("filename") or a.get("url") or "").lower()
    return fn.endswith(_IMG_EXT)


def _att_name(a) -> str:
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return str(a.get("filename") or a.get("name") or a.get("url") or a.get("id") or "附件")
    return str(a)


def _att_keys(a) -> set:
    """附件的全部可匹配键（兼容 planner 用 filename / name / id / url / doc_id 声明）。"""
    if isinstance(a, dict):
        out = set()
        for k in ("filename", "name", "id", "url", "doc_id"):
            v = a.get(k)
            if v not in (None, ""):
                out.add(str(v))
        return out
    return {str(a)}


def _human_size(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    if n >= 1024 * 1024:
        return "%.1fMB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%.0fKB" % (n / 1024.0)
    return "%dB" % n


def plan_subtask_attachments(attachments, declared=None, mode: str = "auto",
                             inline_bytes: int = AUTO_INLINE_BYTES,
                             inline_count: int = AUTO_INLINE_COUNT,
                             max_images: int = AUTO_MAX_IMAGES) -> tuple:
    """决定「本子任务实际拿到哪些附件正文」+「未内联部分的清单文本」。

    返回 (pass_list, manifest_text)。

    设计依据：Google ADK「artifacts 按名引用」+ Claude Code「显式 allow-list，
    不倾倒全量」。**未声明 ≠ 全给** —— 原实现把 run 级附件无差别灌进每个子任务，
    5 个附件 × 每个子任务 8KB ⇒ 与任务无关的噪音挤掉真正的任务上下文。

    模式：
      · declared 非空（planner 显式声明）⇒ **权威白名单**，只传匹配项（语义不变）；
      · mode="all"   ⇒ 旧行为（全量），供排障/兼容；
      · mode="manifest" ⇒ 只给清单，一个都不内联；
      · mode="auto"（默认）⇒ 图片直传（≤max_images）+ 小规模文本全传，
        超规模文本**只给清单**（清单里带 doc_id/文件名/体积 + 取回工具指引）。
    """
    atts = [a for a in (list(attachments or []))]
    if not atts:
        return [], ""

    # ① 显式白名单：权威（保持原语义，逐字节兼容）
    if isinstance(declared, list) and declared:
        wl = {str(x) for x in declared}
        kept = [a for a in atts if _att_keys(a) & wl]
        rest = [a for a in atts if not (_att_keys(a) & wl)]
        mtxt = ""
        if rest:
            mtxt = _manifest(rest, why="未列入本任务的附件白名单")
        return kept, mtxt

    m = str(mode or "auto").strip().lower()
    if m == "all":
        return atts, ""

    imgs = [a for a in atts if _is_image(a)]
    texts = [a for a in atts if not _is_image(a)]

    if m == "manifest":
        return [], _manifest(atts, why="附件正文按需加载（未内联）")

    # auto：图片直传（受上限），文本按规模判定
    keep_imgs = imgs[:max_images]
    spilled_imgs = imgs[max_images:]

    def _small(a) -> bool:
        """体积已知且不大 ⇒ 值得内联。**体积未知一律保守按需**（宁可多一次取回，
        也不要把一份可能几十万字的文档无差别灌进每个子任务）。"""
        if not isinstance(a, dict):
            return False
        try:
            sz = int(a.get("size") or 0)
        except (TypeError, ValueError):
            return False
        return 0 < sz <= inline_bytes

    small_texts = [a for a in texts if _small(a)]
    if len(small_texts) == len(texts) and len(texts) <= inline_count:
        keep_texts: list = texts
        spilled_texts: list = []
    else:
        keep_texts = []
        spilled_texts = texts

    kept = keep_imgs + keep_texts
    spilled = spilled_imgs + spilled_texts
    mtxt = ""
    if spilled:
        mtxt = _manifest(spilled, why="正文未内联（按需加载）",
                         note="需要其中内容时，用 read_attachment_section（按 doc_id/章号）"
                              "或知识库检索取回；**不要凭空假设其内容**。")
    return kept, mtxt


def _manifest(items, why: str = "", note: str = "") -> str:
    """生成附件清单（给模型的可见引用，符合「按名引用而非内联」）。"""
    lines = ["【附件清单（%s）】" % (why or "按需加载")]
    for a in items:
        nm = _att_name(a)
        if not isinstance(a, dict):
            lines.append("- %s" % nm)
            continue
        bits = []
        did = a.get("doc_id")
        if did:
            bits.append("doc_id=%s" % did)
        if a.get("size"):
            bits.append(_human_size(a.get("size")))
        if _is_image(a):
            bits.append("图片")
        elif a.get("parse_status"):
            bits.append("解析=%s" % a.get("parse_status"))
        lines.append("- %s%s" % (nm, ("（" + "，".join(bits) + "）") if bits else ""))
    if note:
        lines.append(note)
    return "\n".join(lines)
