# -*- coding: utf-8 -*-
"""离线 Dreaming（2026-11-11，对标 Anthropic《The components of agent memory》）。

## 定位：这是**贯穿四层的维护引擎**，不是第五层存储
Claude 那套的关键不是"四层金字塔"，而是 `dreaming` 拿 L1 原始流水去校验、
重写、编译 L2 笔记，并把索引推上/推下 L4。本模块补的正是我们唯一的结构性缺口：
`core/evolution_tick.py` 默认 observe 只到 HIL 门口，`MemoryService.consolidate`
只在记忆库内部两两软删，**全仓原本没有「回读流水 → 重写上层笔记」的链路**。

## 硬纪律（与本项目既有纪律一致，别改）
1. `dream()` **只读**：不写任何表、不调 LLM、不发起网络请求。全部判据是确定性函数，
   可重放、可变异自证；"审判模型"的职能由显式规则 + 流水证据承担。
2. 输出**独立候选包**（对齐 Managed Agents dreams：input store is never changed）。
   输入 store 的 sha256 写进包里，门禁会核对前后一致。
3. 落库必须过 `apply_candidates(hil_approved=True)`；未批准直接拒绝。
   绝不自动转正（沿用 `core/online_eval.py` 的 B1 纪律）。
4. apply **只改状态、不改 content**：沿用 `superseded_by` 影子列哲学——
   判断会错，判错要能看见、能回退。写"纠错"不等于"删掉旧结论"。
5. 不碰词表 / 配置 / 门禁：那是生产语义，不是记忆。

## 四类动作（与原文一一对应）
| 动作 | 原文 | 本模块实现 |
|---|---|---|
| correct mistakes | `correct mistakes` | 拿流水证据校验 claim，产出 verified / contradicted 状态候选 |
| create notes | `create notes` | 跨 ≥3 个会话重复出现的问法 → 候选规则 |
| build representations | `build new memory representations` | `build_wiki()` 按 topic 生成带双向链接 + evidence_link 的页面 |
| shape injected context | `shape what's preemptively loaded` | `build_index()` 产出 ≤200 行目录，promote/demote 候选 |
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from memory_service import MemoryService

SCHEMA = 1
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024
MIN_SESSIONS_FOR_CREATE = 3
NEGATION_MARKERS = (
    "不是", "不在", "没有", "并未", "并非", "已删", "失败", "错误",
    "不可用", "禁止", "不再", "不正确", "有误", "失效", "不存在", "已废弃",
)
VALID_STATES = ("verified", "contradicted", "unverifiable", "needs_review")
#: 召回侧会排除的状态（只有强信号才进得来；见 `_verify_claim` 的分级理由）
EXCLUDED_FROM_RECALL = ("contradicted",)


def _sha256(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _keywords(text: str) -> set:
    """中文 bigram + ASCII 词，用于证据重合判断（不依赖分词库，离线可用）。"""
    s = text or ""
    out = set(re.findall(r"[A-Za-z0-9_]{2,}", s.lower()))
    for seg in re.findall(r"[\u4e00-\u9fff]+", s):
        # 2-gram 用于重合度判断；3-gram 让 FTS5(trigram) 的 MATCH 通道真被使用
        # （trigram 最短 3 字符，只有 2-gram 会全部落到 LIKE 兜底，索引形同虚设）。
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
        for i in range(len(seg) - 2):
            out.add(seg[i:i + 3])
        if len(seg) >= 2:
            out.add(seg)
    return out


def _input_snapshot(conn) -> tuple:
    """输入 store 的确定性快照 —— 门禁据此证明 dream 没有原地改记忆。"""
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT id, content, verify_state, source_credibility FROM agent_memory "
            "ORDER BY id")]
    except Exception:
        # 老库缺列：快照退化为最小三列，不静默抛
        return [tuple(r) for r in conn.execute(
            "SELECT id, content FROM agent_memory ORDER BY id")]


def _evidence_for(conn, claim: str, limit: int = 5) -> list:
    """用流水 FTS 找证据。短词/无 FTS 时由 search_transcripts 内部走 LIKE 兜底。

    ## 两条实战修正（2026-11-11 真机验证抓到的）
    ① **按 kind 轮转取样**：原实现"先到先得"，`query_trace` 里高频词（如"转发器"）
       一次占满配额 ⇒ `messages` 里那条**明确否定**的流水连候选都进不来，
       于是该被证伪的笔记被标成 verified。现在每类流水（messages / tool_logs /
       query_trace）各留位置，任何一类都不能把别的类挤掉。
    ② **选取按首次出现位置**：按"长度降序"会挑出笔记后半段的无意义 3-gram
       （"代表月/即代表"），证据里一个都没有 ⇒ 取证恒为空。
    """
    from transcript_fts import search_transcripts
    low = (claim or "").lower()
    cand = [k for k in _keywords(claim) if 2 <= len(k) <= 8]
    ordered, seen_pos = [], set()
    for k in sorted(cand, key=lambda k: (low.find(k), -len(k))):
        p = low.find(k)
        if p < 0 or p in seen_pos:
            continue
        seen_pos.add(p)
        ordered.append(k)
    kws = ordered[:5]

    per_kind: dict = {}
    for kw in kws:
        for _kind in ("messages", "tool_logs", "query_trace", ""):
            # 前三轮各锁一类（保证每类都有位置），最后一轮不带 kind 捞其余类型。
            hits = search_transcripts(conn, kw, limit=limit, kind=_kind)
            for hit in hits:
                kind = hit.get("kind") or "?"
                bucket = per_kind.setdefault(kind, [])
                key = (kind, hit.get("ref_id"))
                if any((b.get("kind"), b.get("ref_id")) == key for b in bucket):
                    continue
                if len(bucket) >= limit:
                    continue
                bucket.append({"kind": kind, "ref_id": hit.get("ref_id"),
                               "text": hit.get("snippet") or "", "keyword": kw})
    # 轮转摊平：messages → tool_logs → query_trace → … 交替取，避免某一类垄断
    out, idx = [], 0
    while any(len(v) > idx for v in per_kind.values()):
        for kind in ("messages", "tool_logs", "query_trace"):
            bucket = per_kind.get(kind) or []
            if len(bucket) > idx:
                out.append(bucket[idx])
        for kind, bucket in per_kind.items():
            if kind not in ("messages", "tool_logs", "query_trace") and len(bucket) > idx:
                out.append(bucket[idx])
        idx += 1
    return out[:limit * 3]


def _numbers(text: str) -> list:
    """抽取数值（含小数与常见单位前缀），用于判"数值冲突"。"""
    return re.findall(r"\d+(?:\.\d+)?", text or "")


def _numbers_conflict(claim: str, text: str) -> tuple:
    """断言里的数值是否被**紧邻否定**。返回 (是否强信号, 依据)。

    ## 只保留一种形态（形态 (b)「数值无交集」已作废）
    `上限为 30dBW` vs `上限**不是** 30dBW` —— 同一个数被否词紧贴否定，
    这是"断言被改写"的确定性证据。

    ⚠️ 曾实现过的形态 (b)「claim 与证据的数值集合无交集」**必须废弃**：
    真库实测它判出 8 条 contradicted 全是假阳性 ——
    `工程007` 的 007 vs 流水里的 2、`SysML v2` 的 2 vs `0/01/02`。
    长文本里数字天然互不相同，"数值集合无交集"在任何两条长文本间都成立。
    ⇒ 强信号只留"同一数值被否"，其余一律 needs_review。
    """
    a = set(_numbers(claim))
    if not a:
        return False, ""
    neg_pat = "(%s)" % "|".join(re.escape(x) for x in NEGATION_MARKERS)
    for m in re.finditer(neg_pat + r"\s*[^。；;\n]{0,4}?(\d+(?:\.\d+)?)", text or ""):
        if m.group(2) in a:
            return True, "同一数值 %s 被紧邻否定「%s」" % (m.group(2), m.group(1))
    return False, ""


def _negation_near(claim: str, text: str, window: int = 20,
                   min_anchor: int = 6) -> list:
    """否定词是否**紧邻 claim 的同一断言**——而不是长文本里随便出现一次。

    ## 为什么必须有这一层（2026-11-11 真机数据打出来的）
    只判"证据里有无否定词"时，真库 80 条笔记被判出 **55 条 contradicted**。
    抽查全是假阳性：一条 MBSE 长笔记因为证据里出现「不是」被判伪（overlap=30）、
    一条 vc 格式笔记因为工具日志里出现「不存在」被判伪。
    ⇒ 否定词出现 ≠ 否定这条断言。判据要问的是「它否定的**是不是同一件事**」。

    ## 两条互补判据（任一成立即算邻近，确定性、无新依赖）
    **(a) 否定词后面紧跟 claim 的某个关键词**（窗口 14 字内）
        这是主判据，因为它直接对应语义："否定词否定的是 claim 提到的东西"。
        实测形态：`宝可梦中心不**在东侧**`（"东侧"是 claim 的 2-gram）、
        `上限不**是30dBW**`（数值）。
    **(b) 与 claim 有一段 ≥6 字的最长公共片段，且否定词落在其 ±20 字内**
        补充判据，覆盖"否定词在断言**前面**"的语序
        （如 `并非如此：转发器EIRP上限为30dBW`）。

    ⚠️ 只用 (b) 会漏真阳性：`宝可梦中心在山洞东侧` vs `宝可梦中心不在东侧`
       的最长公共片段只有「宝可梦中心」5 字，达不到 6 字门槛 ⇒ 判不出否定。
       这正是 min_anchor 从 4 抬到 6 之后实测暴露的回归。
    """
    import difflib
    a, b = (claim or "").strip(), (text or "").strip()
    if not a or not b:
        return []
    kws = sorted((k for k in _keywords(a) if len(k) >= 2),
                 key=len, reverse=True)
    hits: list = []
    for neg in NEGATION_MARKERS:
        start = b.find(neg)
        while start >= 0:
            tail = b[start + len(neg): start + len(neg) + 14]
            if any(k in tail for k in kws):
                hits.append(neg)
                break
            start = b.find(neg, start + 1)
        if hits and hits[-1] == neg:
            continue
    if hits:
        return sorted(set(hits))
    # (b) 语序补充：公共片段 + 邻近窗口
    try:
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        blocks = [m for m in sm.get_matching_blocks() if m.size >= min_anchor]
    except Exception:
        return []
    for m in blocks:
        lo = max(0, m.b - window)
        hi = min(len(b), m.b + m.size + window)
        piece = b[lo:hi]
        for neg in NEGATION_MARKERS:
            if neg in piece:
                hits.append(neg)
    return sorted(set(hits))


def _verify_claim(claim: str, evidence: list) -> dict:
    """确定性事实校验。**否定优先于重合度**（两遍判定）。

    ## 为什么必须两遍（2026-11-11 真机验证抓到的实质缺陷）
    原实现只取"重合度最高的那一条证据"再判有无否定。实测反例：
      探针笔记「转发器EIRP上限为30dBW，位于东侧机柜」
      流水里有一条明确否定「上限**不是**30dBW→位于西侧，已确认错误」，
      但同时存在一条历史 query「转发器EIRP上限…」（query_trace#680，无否定），
      它的字面重合恰好比否定行**多** ⇒ 判成 `verified`（该被证伪的笔记被标成已确认）。
    ⇒ 单条取样让"否定证据"被"高重合但不含否定的证据"盖掉，方向恰好是危险的：
      错笔记被升级为 verified，然后进入 core 常驻注入。

    两遍判定：
      ① 扫**全部**证据，只要有一条含否定标记且关键词重合 ≥2 ⇒ 立即 contradicted；
      ② 没有任何否定证据，才按最高重合判 verified（≥3）/ unverifiable。
    宁可漏判 verified（保守），不可漏判 contradicted（危险）。
    """
    kw = _keywords(claim)
    scored = []
    for ev in evidence:
        txt = ev.get("text") or ""
        overlap = len(kw & _keywords(txt))
        if overlap >= 2:
            scored.append((overlap, ev, _negation_near(claim, txt)))
    if not scored:
        return {"state": "unverifiable", "evidence_ref": "", "overlap": 0,
                "reason": "流水中没有足以校验这条笔记的证据"}
    # ① 否定优先：在全部"够格"的证据里找任何一条**紧邻同一断言**的否定
    negs = [s for s in scored if s[2]]
    if negs:
        overlap, best, marks = max(negs, key=lambda x: x[0])
        ref = "%s#%s" % (best.get("kind"), best.get("ref_id"))
        txt = best.get("text") or ""
        # ★ 分级（2026-11-11 真库精度实测后连续收窄两次的经验）：
        #   规则法读不懂语义，真库抽查里 "否定的是别的事" 占多数
        #   （证据写 `[错误] REQ-XX` 否定的是另一条需求；证据里"没有"出现在别的小句）。
        #   而 contradicted 在召回侧会被**排除**，误判 = 丢真记忆，代价不可逆。
        #   ⇒ **只有数值冲突**（claim 与证据都含数值且无交集 = 断言被改写）才算强信号；
        #     关键词重合数**不能**当强信号：长文本天然重合几十项（实测 overlap=46
        #     仅因证据里出现一个"没有"）。
        #   其余带邻近否定的一律 needs_review：不排除、不注入理由，只排队等人工。
        if _numbers_conflict(claim, txt)[0]:
            _strong, _why = _numbers_conflict(claim, txt)
            return {"state": "contradicted", "evidence_ref": ref, "overlap": overlap,
                    "reason": "断言邻近处含否定 %s；%s" % ("/".join(marks[:2]), _why)}
        return {"state": "needs_review", "evidence_ref": ref, "overlap": overlap,
                "reason": "断言邻近处含否定 %s，但数值未变 ⇒ 交人工判定"
                          % "/".join(marks[:2])}
    # ② 无邻近否定，才谈"已确认"
    overlap, best, _ = max(scored, key=lambda x: x[0])
    ref = "%s#%s" % (best.get("kind"), best.get("ref_id"))
    if overlap >= 3:
        return {"state": "verified", "evidence_ref": ref, "overlap": overlap,
                "reason": "全部证据中未发现邻近否定，最高关键词重合 %d 项" % overlap}
    return {"state": "unverifiable", "evidence_ref": ref, "overlap": overlap,
            "reason": "重合仅 %d 项且无邻近否定，证据不足以确认" % overlap}


def _create_candidates(conn, limit: int = 3) -> list:
    """跨会话重复出现的问法 → 候选规则（create notes）。"""
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(query_raw),''),query) AS q, "
            "COUNT(DISTINCT DATE(created_at)) AS days, COUNT(*) AS n "
            "FROM query_trace WHERE COALESCE(query,'')<>'' "
            "GROUP BY q HAVING days>=? ORDER BY days DESC, n DESC LIMIT ?",
            (MIN_SESSIONS_FOR_CREATE, limit))]
    except Exception:
        return []
    return [{"action": "create", "claim": r["q"][:120], "sessions": r["days"],
             "occurrences": r["n"],
             "reason": "跨 %d 个会话重复出现，值得沉淀为候选规则" % r["days"],
             "confidence": min(0.9, 0.4 + 0.1 * r["days"])} for r in rows]


def _merge_candidates(conn, limit: int = 5) -> list:
    """同 topic 且 content 前缀相同的笔记 → 合并候选（不落库，只提议）。"""
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, mem_topic, SUBSTR(content,1,30) AS head, COUNT(*) OVER "
            "(PARTITION BY COALESCE(mem_topic,''), SUBSTR(content,1,30)) AS n "
            "FROM agent_memory WHERE COALESCE(forgotten,0)=0")]
    except Exception:
        return []
    groups = {}
    for r in rows:
        if r["n"] > 1:
            groups.setdefault((r["mem_topic"] or "", r["head"]), []).append(r["id"])
    out = []
    for (topic, head), ids in sorted(groups.items(), key=lambda x: -len(x[1]))[:limit]:
        out.append({"action": "merge", "memory_ids": sorted(ids),
                    "keep_id": max(ids), "topic": topic, "claim": head,
                    "reason": "同主题同前缀 %d 条，建议保留最新并给旧条打影子标记" % len(ids),
                    "confidence": 0.6})
    return out


def _tier_candidates(conn, limit: int = 6) -> list:
    """按访问频次/新鲜度给上下推建议（shape injected context）。"""
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, tier, access_count, last_accessed_at, mem_topic "
            "FROM agent_memory WHERE COALESCE(forgotten,0)=0 AND tier IS NOT NULL")]
    except Exception:
        return []
    out = []
    for r in rows:
        ac = int(r.get("access_count") or 0)
        last = str(r.get("last_accessed_at") or "")
        if r["tier"] == "recall" and ac >= 5 and last:
            out.append({"action": "promote", "memory_id": r["id"],
                        "to_tier": "core", "reason": "访问 %d 次且近期命中" % ac,
                        "confidence": 0.7})
        elif r["tier"] == "core" and not last:
            out.append({"action": "demote", "memory_id": r["id"],
                        "to_tier": "recall", "reason": "core 但无最近访问记录",
                        "confidence": 0.5})
        if len(out) >= limit:
            break
    return out


def build_wiki(notes: list, candidates: list) -> dict:
    """P1-2：把笔记按 topic 编译成带双向链接的 Markdown 页面网络。

    纪律（沿用 `knowledge_reflow.py`）：**不复制知识库实体**，只组织记忆本身；
    每页附 `evidence_link` 回指流水。二次编译会补齐死链（对齐原文第二遍 Dreaming）。
    """
    pages, by_topic = {}, {}
    for n in notes[:500]:
        topic = (n.get("mem_topic") or "未分类").strip() or "未分类"
        by_topic.setdefault(topic, []).append(n)
    # evidence_link 只挂**被证伪**的条目：那才是需要回溯流水的少数关键行。
    # 给每条都挂会把短笔记集合撑成 Borges 那张"和帝国一样大的地图"。
    ev_by_id = {c.get("memory_id"): c.get("evidence_ref")
                for c in candidates
                if c.get("action") == "correct" and c.get("state") == "contradicted"}
    for topic, items in sorted(by_topic.items()):
        # ★ 页是**导航索引**，不是正文副本。原文反例：wiki 长到 1.77× 语料，
        #   "地图画得和帝国一样大"等于什么都没抽象。故每条只留 40 字摘要 +
        #   行内 evidence，正文一律由 memory_fetch(id) 按需深读。
        lines = ["# %s" % topic]
        for it in items[:20]:
            content = (it.get("content") or "").replace("\n", " ")[:24]
            state = it.get("verify_state") or "unverifiable"
            ev = ev_by_id.get(it.get("id")) or (it.get("evidence_ref") or "")
            lines.append("- %s|%s|%s%s" % (
                it.get("id"), state[0], content, (" ←[[%s]]" % ev) if ev else ""))
        # 链接是导航价值所在，但每页都写会把短笔记集合撑成
        # "地图和帝国一样大"（原文 1.77× 反例）⇒ 只给前 3 个主页面挂。
        links = [t for t in by_topic if t != topic]
        if links and len(pages) < 3:
            lines.append("→ " + " ".join("[[%s]]" % t for t in links[:2]))
        pages[topic] = "\n".join(lines)
    # 死链自愈：所有 [[X]] 引用必须能解析到已有页或 evidence 引用
    known = set(pages)
    dangling = []
    for topic, body in pages.items():
        for ref in re.findall(r"\[\[([^\]]+)\]\]", body):
            if ref in known or "#" in ref:
                continue
            dangling.append((topic, ref))
    src_chars = sum(len(n.get("content") or "") for n in notes[:500])
    return {"pages": pages, "dangling_links": dangling,
            "source_chars": src_chars,
            "wiki_chars": sum(len(v) for v in pages.values()),
            # 每页固定 32 字符的标题/链接预算。短笔记集合的页头占比天然偏高，
            # 用"绝对值 ≤ 源笔记"会把正常索引误判成膨胀；但预算是**常数**，
            # 仍抓得住原文那个 1.77× / 88KB 差额的 Borges 反例。
            "budget_chars": src_chars + 32 * len(pages)}


def build_index(conn, max_lines: int = MAX_INDEX_LINES,
                max_bytes: int = MAX_INDEX_BYTES) -> dict:
    """P1-1：Claude Code 式「顶层目录、底层仓库」——只放索引，不放正文。

    硬上限 200 行 / 25 KB（原文：更长的指令文件会降低指令遵循度）。
    每行形如 `- [topic] #id (state, cred)`，可用 `memory_fetch(id)` 按需深读。
    """
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, mem_topic, mem_type, verify_state, source_credibility, access_count "
            "FROM agent_memory WHERE COALESCE(forgotten,0)=0 "
            "ORDER BY COALESCE(access_count,0) DESC, id DESC")]
    except Exception:
        rows = []
    lines = ["# 记忆索引（目录；正文用 memory_fetch(id) 按需读取）", ""]
    for r in rows:
        if len(lines) >= max_lines:
            break
        topic = (r.get("mem_topic") or r.get("mem_type") or "未分类")
        state = r.get("verify_state") or "unverifiable"
        cred = r.get("source_credibility")
        line = "- [%s] #%s (%s, cred=%s)" % (
            str(topic).replace("\n", " "), r["id"], state,
            "—" if cred is None else cred)
        lines.append(line)
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > max_bytes:
        text = text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    return {"text": text, "lines": len(text.splitlines()),
           "bytes": len(text.encode("utf-8")), "entries": min(len(rows), max_lines - 2),
           "truncated": len(rows) > max_lines - 2}


def memory_fetch(conn, memory_id: int) -> dict:
    """按需深读单条记忆（索引 → 仓库的取回端）。"""
    try:
        r = conn.execute("SELECT * FROM agent_memory WHERE id=?", (int(memory_id),)).fetchone()
        return dict(r) if r else {}
    except Exception:
        return {}


def wiki_dir() -> str:
    """wiki 落盘目录（默认 `data/memory_wiki/`，随仓库外的运行数据走）。"""
    import os
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "memory_wiki")
    os.makedirs(base, exist_ok=True)
    return base


def _safe_name(topic: str) -> str:
    import re as _re
    s = _re.sub(r'[\\/:*?"<>|\s]+', "_", (topic or "未分类")).strip("_")
    return (s or "未分类")[:60]


def persist_wiki(wiki: dict, *, keep: int = 200) -> dict:
    """把编译出的页面写到磁盘（P1-2 的可查看端）。

    ## 为什么必须落盘
    `build_wiki()` 只在内存里生成 dict —— 页面网络再漂亮，没有落点就等于没产出
    （本仓既有教训：`v_memory_notes` 建了视图但零消费方）。落盘后可直接用编辑器/
    浏览器查看，也便于人工核对"这条结论到底来自哪条笔记"。

    ## 纪律
    · 只写 `data/memory_wiki/`，不碰仓库源码树；
    · 每页带 `<!-- dream:fingerprint -->` 头，便于判断是不是本次编译产物；
    · `keep` 控制单次最多写多少页，防一次写爆磁盘；
    · 写失败不抛（返回 errors 计数），Dreaming 主流程不该被 IO 拖死。
    """
    import os
    out = {"dir": "", "written": 0, "errors": 0, "files": []}
    try:
        base = wiki_dir()
        out["dir"] = base
        pages = (wiki or {}).get("pages") or {}
        for topic in sorted(pages)[:keep]:
            path = os.path.join(base, _safe_name(topic) + ".md")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(pages[topic])
                out["written"] += 1
                out["files"].append(os.path.basename(path))
            except Exception:
                out["errors"] += 1
    except Exception:
        out["errors"] += 1
    return out


def load_wiki_page(topic: str) -> str:
    """从磁盘读回某一页（前端/编辑器查看用）。"""
    import os
    path = os.path.join(wiki_dir(), _safe_name(topic) + ".md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def metrics(package: dict) -> dict:
    """P2：固定四项指标，避免"感觉变好"式汇报（对齐 sandbox_runs 双臂 A/B）。"""
    cand = package.get("candidates") or []
    return {
        "memory_hit_rate": None,          # 由 A/B 实测填充，dream 阶段不臆造
        "fact_conflict_rate": round(
            sum(1 for c in cand if c.get("state") == "contradicted") / max(1, len(cand)), 4),
        "tokens_per_question": None,      # 同上
        "injected_chars": len((package.get("index") or {}).get("text") or ""),
        "candidate_count": len(cand),
    }


def compare_packages(before: dict, after: dict) -> dict:
    """P2：双臂 A/B 的确定性 delta。

    只对比**已实测**的数值；`memory_hit_rate` / `tokens_per_question` 由外部
    sandbox_runs 实测填入，未填时 delta 保持 None —— 不用 0 冒充"没变化"。
    """
    a, b = metrics(before), metrics(after)
    keys = ("memory_hit_rate", "fact_conflict_rate",
            "tokens_per_question", "injected_chars", "candidate_count")
    out = {}
    for k in keys:
        x, y = a.get(k), b.get(k)
        out[k] = (round(y - x, 4)
                  if isinstance(x, (int, float)) and isinstance(y, (int, float))
                  else None)
    out["direction"] = (
        "improved" if (out["fact_conflict_rate"] or 0) < 0 and
        (out["injected_chars"] or 0) <= 0 else
        "regressed" if (out["fact_conflict_rate"] or 0) > 0 or
        (out["injected_chars"] or 0) > 0 else "flat")
    return out


def dream(conn, *, agent_id: str = "", sessions: int = 10,
          instructions: str = "", note_limit: int = 80) -> dict:
    """只读对账 → 独立候选包。**不写任何表、不调 LLM。**

    :param sessions: 语义上的"回看多少会话"，用于指纹与报告；实际证据检索
                     由 transcript_fts 全库覆盖（不按会话切窗，避免漏证据）。
    """
    snapshot = _input_snapshot(conn)
    input_sha = _sha256(snapshot)
    try:
        notes = [dict(r) for r in conn.execute(
            "SELECT id, content, mem_topic, mem_type, verify_state, evidence_ref, "
            "source_credibility, tier, access_count, last_accessed_at "
            "FROM agent_memory WHERE COALESCE(forgotten,0)=0 ORDER BY id LIMIT ?",
            (note_limit,))]
    except Exception:
        notes = [dict(r) for r in conn.execute(
            "SELECT id, content, mem_topic, mem_type FROM agent_memory "
            "WHERE COALESCE(forgotten,0)=0 ORDER BY id LIMIT ?", (note_limit,))]
        for n in notes:
            n.setdefault("verify_state", "unverifiable")

    candidates = []
    for n in notes:
        ev = _evidence_for(conn, n.get("content") or "")
        v = _verify_claim(n.get("content") or "", ev)
        if v["state"] == "unverifiable":
            continue
        candidates.append({"action": "correct", "memory_id": n["id"],
                           "claim": (n.get("content") or "")[:120],
                           "state": v["state"], "evidence_ref": v["evidence_ref"],
                           "reason": v["reason"], "overlap": v["overlap"],
                           "confidence": min(0.9, 0.3 + 0.1 * v["overlap"])})
    candidates += _create_candidates(conn)
    candidates += _merge_candidates(conn)
    candidates += _tier_candidates(conn)

    wiki = build_wiki(notes, candidates)
    index = build_index(conn)
    fprint = _sha256({"schema": SCHEMA, "input_sha": input_sha,
                      "sessions": sessions, "instructions": instructions,
                      "note_ids": [n["id"] for n in notes]})
    pkg = {"schema": SCHEMA, "fingerprint": fprint, "input_sha256": input_sha,
           "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "stats": {"notes_scanned": len(notes),
                     "correct": sum(1 for c in candidates if c["action"] == "correct"),
                     "contradicted": sum(1 for c in candidates if c.get("state") == "contradicted"),
                     "create": sum(1 for c in candidates if c["action"] == "create"),
                     "merge": sum(1 for c in candidates if c["action"] == "merge"),
                     "tier": sum(1 for c in candidates if c["action"] in ("promote", "demote"))},
           "candidates": candidates, "wiki": wiki, "index": index,
           "metrics": {}, "read_only": True}
    pkg["metrics"] = metrics(pkg)
    return pkg


def apply_candidates(conn, package: dict, *, hil_approved: bool = False,
                     approved_by: str = "") -> dict:
    """HIL 门后才落库；且**只改状态，不改 content**（影子语义，可回退）。

    没批准就一个字节都不写。这条是 Anthropic「错误笔记被提升到 injected context
    会污染每个会话」的对应防线，也是本仓 B1 纪律的延续。
    """
    out = {"ok": False, "applied": 0, "reason": "", "fingerprint": ""}
    if not package or not isinstance(package, dict):
        out["reason"] = "EMPTY_PACKAGE"
        return out
    out["fingerprint"] = package.get("fingerprint", "")
    if not hil_approved:
        out["reason"] = "HIL_REQUIRED"
        return out
    if _sha256(_input_snapshot(conn)) != package.get("input_sha256"):
        out["reason"] = "INPUT_STORE_CHANGED"
        return out
    for c in package.get("candidates") or []:
        if c.get("action") != "correct" or c.get("state") not in VALID_STATES:
            continue
        if MemoryService.mark_verify_state(
                conn, c.get("memory_id"), c["state"],
                evidence_ref=c.get("evidence_ref") or "",
                note="dream:%s %s" % (package.get("fingerprint", "")[:12],
                                      (c.get("reason") or "")[:80])):
            out["applied"] += 1
    out["ok"] = True
    out["reason"] = "APPLIED_STATES_ONLY"
    return out
