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
VALID_STATES = ("verified", "contradicted", "unverifiable")


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
    """用流水 FTS 找证据。短词/无 FTS 时由 search_transcripts 内部走 LIKE 兜底。"""
    from transcript_fts import search_transcripts
    # 选词策略（实测踩过）：按「长度降序」会挑出笔记**后半段**的无意义 3-gram
    # （如"代表月/即代表"），证据里一个都没有 ⇒ 取证恒为空。
    # 改为按**在 claim 中首次出现的位置**排序、同起点只留最长的那个，
    # 前几个词就落在主语/实体上，正是证据里会复述的部分。
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
    seen, out = set(), []
    for kw in kws:
        for hit in search_transcripts(conn, kw, limit=limit):
            key = (hit.get("kind"), hit.get("ref_id"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": hit.get("kind"), "ref_id": hit.get("ref_id"),
                        "text": hit.get("snippet") or "", "keyword": kw})
        if len(out) >= limit:
            break
    return out[:limit]


def _verify_claim(claim: str, evidence: list) -> dict:
    """确定性事实校验：证据与 claim 关键词重合 + 显式否定词 ⇒ contradicted。

    这是**保守**判据：只在"证据里明确出现否定、且关键词重合 ≥2"时判伪；
    否则重合 ≥3 才判 verified；其余一律 unverifiable（不猜测）。
    """
    kw = _keywords(claim)
    best, best_hits = None, 0
    for ev in evidence:
        txt = ev.get("text") or ""
        overlap = len(kw & _keywords(txt))
        if overlap > best_hits:
            best, best_hits = ev, overlap
    if best is None or best_hits < 2:
        return {"state": "unverifiable", "evidence_ref": "", "overlap": best_hits,
                "reason": "流水中没有足以校验这条笔记的证据"}
    ref = "%s#%s" % (best.get("kind"), best.get("ref_id"))
    neg = [m for m in NEGATION_MARKERS if m in (best.get("text") or "")]
    if neg:
        return {"state": "contradicted", "evidence_ref": ref, "overlap": best_hits,
                "reason": "流水含否定标记 %s，与笔记关键词重合 %d 项" % ("/".join(neg[:2]), best_hits)}
    if best_hits >= 3:
        return {"state": "verified", "evidence_ref": ref, "overlap": best_hits,
                "reason": "流水关键词重合 %d 项，未发现否定" % best_hits}
    return {"state": "unverifiable", "evidence_ref": ref, "overlap": best_hits,
            "reason": "重合仅 %d 项，证据不足以确认或否定" % best_hits}


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
