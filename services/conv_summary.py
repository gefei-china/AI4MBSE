"""会话摘要（左侧任务列表 hover 信息卡用）。

背景（2026-09-28 用户反馈：「目前的摘要是基于会话内容总结的嘛，感觉像是直接截取了一段会话内容」）：
前端 `03-chat.js::showTaskTip` 原实现是「取最后 1 条消息 → 去掉 markdown 符号 → slice(0,140)」，
即**原文截断**，不是摘要。且它取的是**最后一条**，实测常常是 AI 的收尾语/客套话，
信息密度最低。本模块提供真正的会话概述。

两条路（都不截断）：
  1) **LLM 摘要（主路径）**：把会话压缩成「主题 / 用户意图 / 关键结论」三行式概述。
     经 `llm_client.chat()`（自带重试 + provider 回退 + Mock 兜底）。
  2) **结构化概述（降级路径）**：LLM 不可用（无 key / 强制 Mock / 调用失败）时，
     改用「**首条用户请求**（主题所在）+ **末条 AI 结论**」合成，两者各自按句边界收敛，
     不出现"从句子中间断开"的半句话。

缓存：进程内 LRU（key = conv_id + 消息总数）。会话一有新增消息 msg_count 变化即失效，
无需落库、无需失效通知 —— 也因此**不新增数据列、不动 schema**（避免又一次迁移+存量回填）。
"""
import logging
import re
import threading
import traceback

logger = logging.getLogger("mbse.conv_summary")

# 进程内缓存：{conv_id: {"msg_count": int, "summary": str, "source": str}}
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512

# 摘要输入预算：只取会话首尾若干条，避免长会话把 prompt 撑爆。
# 开头 = 需求背景，结尾 = 结论；中间过程对"这是什么会话"贡献很低。
_HEAD_N = 3
_TAIL_N = 4
_PER_MSG_CHARS = 700
_MAX_TOTAL_CHARS = 4200

_SYS_PROMPT = (
    "你是会话归档助手。请用**中文**为一轮 AI 建模/问答会话写一句概述，供左侧任务列表的悬浮卡展示。\n"
    "要求：\n"
    "1) 只输出一句概述，**不要**任何前后缀、标题、引号或 Markdown；\n"
    "2) 长度 40–90 字，必须说清「围绕什么对象/系统」「要做什么/问了什么」「结论或产出是什么」；\n"
    "3) 若会话有明确结论（如参数取值、方案选择、生成物），必须带上；\n"
    "4) 不要复述原句、不要写「用户要求…AI 回答…」这类空话，直接概括内容本身。"
)

# 句边界：中英文句末 + 换行。用于降级路径「按句收敛」，避免半句话
_SENT_END = re.compile(r"[。！？!?；;\n]")


def _clean(text: str) -> str:
    """去 Markdown 装饰与多余空白，保留可读正文。"""
    s = str(text or "")
    s = re.sub(r"```.*?```", " ", s, flags=re.S)          # 代码块整体去掉（摘要不需要）
    s = re.sub(r"`([^`]*)`", r"\1", s)                     # 行内代码留内容
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)       # 链接/图片留文字
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)    # 标题符号
    s = re.sub(r"^\s*[-*+>]\s+", "", s, flags=re.M)        # 列表/引用符号
    s = re.sub(r"\*\*|__|\*|~~", "", s)                    # 强调符号
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _take_sentences(text: str, limit: int) -> str:
    """按句边界收敛到不超过 limit 字：整句优先，**不做硬切**。

    硬切（slice）正是用户抱怨的"像是截取了一段"的观感来源 —— 会在句子中间断开。
    这里改为：累积整句直到超限；若第一句就超限，才退回按标点/逗号收敛，最后才是硬切兜底。
    """
    s = _clean(text)
    if not s:
        return ""
    if len(s) <= limit:
        return s
    out, start = "", 0
    for m in _SENT_END.finditer(s):
        seg = s[start:m.end()].strip()
        if not seg:
            start = m.end()
            continue
        if len(out) + len(seg) > limit:
            break
        out += seg
        start = m.end()
    out = out.strip()
    if out:
        return out
    # 第一句就超限 → 退到逗号级收敛
    for m in re.finditer(r"[，,、]", s):
        if m.end() > limit:
            break
        out = s[:m.end()].strip()
    if out:
        return out
    return s[:limit].rstrip() + "…"


def _sources(msgs: list) -> tuple:
    """从消息列表取「首条用户请求」与「末条 AI 有内容回复」。"""
    users = [m for m in msgs if str(m.get("role") or "") == "user"]
    ais = [m for m in msgs
           if str(m.get("role") or "") == "assistant" and _clean(m.get("content"))]
    first_user = users[0] if users else None
    last_ai = ais[-1] if ais else None
    return first_user, last_ai


# 客套/元话语开场：降级路径取 AI 结论时要剥掉，否则摘要读起来仍像"截了一段"
# （实测：结论段开头是"直接回答您的问题：需要，但只需要一件事——"这类铺垫，
#  真正信息在后面。不剥掉等于换了种截取方式，用户的抱怨依旧成立。）
_CHATTY_PREFIX = re.compile(
    r"^(?:"
    r"直接回答(?:您|你)?的?问题[：:，,]?\s*"
    r"|(?:好|好的|明白|明白了|收到|了解)[，,。!！]?\s*"
    r"|(?:很|非常)高兴(?:为您|为你)?[^。！？\n]{0,20}[。！？]\s*"
    r"|首先[，,]?\s*|简单(?:来|地)?说[，,]?\s*|总结(?:一下)?[：:，,]?\s*"
    r"|以下是[^。\n]{0,20}[：:]\s*"
    r")+"
)
# 结论型标记：命中则从该处开始截取（"结论是/因此/所以"之后才是要点）。
# 踩坑（2026-09-28 实测）：首版写成 `结论(?:是|为)?`，可选后缀使裸「结论」也命中，
# 于是命中了正文里的**名词性「结论」**（"…的裁决意见…（t1 结论）检索到的互联数据为空"），
# 收敛结果成了 `结论：。` / `结论：）` —— 标记后紧跟的就是标点，说明根本没取到要点。
# 修法：① 「结论」必须带判断后缀（是/为）才算结论型标记；② 上界收紧到 5 字，避免跨句瞎配。
_CONCL_MARK = re.compile(
    r"(?:结论(?:是|为)|因此|所以|综上(?:所述)?|最终(?:结论)?)[：:，,]?\s*")


def _strip_chatty(text: str) -> str:
    """剥掉客套/元话语开场，保留实质内容。"""
    s = _clean(text)
    prev = None
    while s and s != prev:            # 反复剥，处理"好的，明白了，"这类叠加
        prev = s
        s = _CHATTY_PREFIX.sub("", s, count=1).strip()
    return s


def _condense_conclusion(text: str, limit: int) -> str:
    """把末条 AI 回复收敛成"结论"：优先取「结论是/因此/综上」之后的要点句。

    标记后必须确实是**要点正文**才采用 —— 若紧跟标点（`结论：。…`）说明命中的是
    名词性"结论"而非结论型标记，属于误判，此时保留全文按句收敛（宁可普通，不可荒唐）。
    """
    s = _strip_chatty(text)
    if not s:
        return ""
    m = _CONCL_MARK.search(s)
    if m:
        tail = s[m.end():].lstrip("：:，,、 \t")      # 标记自带冒号时可能残留
        if len(tail) >= 12 and not re.match(r"^[。！？；;）)】\]”’…]", tail):
            s = tail
    return _take_sentences(s, limit)


def _fallback_summary(msgs: list) -> str:
    """结构化概述（LLM 不可用时）：首条请求（主题）+ 末条结论，各自按句收敛。

    与旧实现的差别：
      ① 取**首条用户请求**而非最后一条 —— "这是关于什么的会话"由开头决定，
         旧实现取末条常常拿到收尾客套话（信息密度最低）；
      ② 结论段先**剥客套开场**、再优先取「结论是/因此/综上」之后的要点，最后才按句收敛 ——
         否则只是把"硬切前 140 字"换成了"硬切后 120 字"，用户的观感不变；
      ③ 两段按句边界收束，不出现半句话。
    """
    first_user, last_ai = _sources(msgs)
    topic = _take_sentences(first_user.get("content") if first_user else "", 110)
    concl = _condense_conclusion(last_ai.get("content") if last_ai else "", 110)
    if topic and concl and concl != topic:
        return f"{topic} ｜ 结论：{concl}"
    if topic:
        return topic
    if concl:
        return concl
    return ""


def _build_prompt(msgs: list) -> str:
    """把会话首尾压缩成一段供 LLM 概括的素材（带角色标注与截断上限）。"""
    head = msgs[:_HEAD_N]
    tail = msgs[-_TAIL_N:] if len(msgs) > _HEAD_N else []
    parts, total = [], 0

    def _add(tag, m):
        nonlocal total
        c = _clean(m.get("content"))
        if not c:
            return
        c = c[:_PER_MSG_CHARS]
        if total + len(c) > _MAX_TOTAL_CHARS:
            c = c[: max(_MAX_TOTAL_CHARS - total, 0)]
        if not c:
            return
        total += len(c)
        parts.append(f"[{tag}] {c}")

    for m in head:
        _add("用户" if m.get("role") == "user" else "AI", m)
    if tail and tail is not head:
        parts.append("……（中间过程略）……")
        for m in tail:
            _add("用户" if m.get("role") == "user" else "AI", m)
    return "\n".join(parts)


def summarize(conn, conv_id: int, msgs: list) -> dict:
    """生成/取用会话摘要。返回 {summary, source, msg_count}。

    source: "llm"（模型概括）| "structured"（结构化概述降级）| "empty"（无可用内容）
    任何异常都不外抛 —— 左侧列表 hover 不能因摘要失败而报错。
    """
    if not msgs:
        return {"summary": "", "source": "empty", "msg_count": 0}

    n = len(msgs)
    with _CACHE_LOCK:
        hit = _CACHE.get(conv_id)
        if hit and hit.get("msg_count") == n:
            return hit

    summary, source = "", "empty"
    try:
        # 主路径：LLM 概括
        from llm import llm_client
        from core import config as _cfg
        # force_mock（回归测试态）下 Mock 只会回显提示词 → 直接走结构化降级，避免把
        # 提示词当摘要展示（实测：Mock 的 content 就是入参回显）。
        if not _cfg.as_bool("llm", "force_mock", False):
            resp = llm_client.chat(
                [{"role": "system", "content": _SYS_PROMPT},
                 {"role": "user", "content": _build_prompt(msgs)}],
                temperature=0.2, max_tokens=300, _intent="conv_summary")
            meta = (resp or {}).get("_meta") or {}
            content = (((resp or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            text = _clean(content)
            if text and not meta.get("used_mock"):
                # 结果自带引号/前缀时剥掉；异常短（<8 字）视作无效
                text = text.strip('「」"\'“”‘’ ')
                if len(text) >= 8:
                    summary, source = text[:200], "llm"
    except Exception:
        # 静默兜底必须留痕（本仓纪律：不留痕会让人去追不存在的"偶发"）
        logger.warning("LLM 会话摘要失败，降级结构化概述：conv=%s\n%s", conv_id, traceback.format_exc())

    if not summary:
        summary = _fallback_summary(msgs)
        source = "structured" if summary else "empty"

    out = {"summary": summary, "source": source, "msg_count": n}
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:      # 简单淘汰：清掉最早插入的一批
            for k in list(_CACHE.keys())[: _CACHE_MAX // 4]:
                _CACHE.pop(k, None)
        _CACHE[conv_id] = out
    return out
