# -*- coding: utf-8 -*-
"""需求分析与冲突检测：配置化链路的**通用胶水**（不含任何业务规则）。

## 为什么有这个文件
米爸约束：需求规则与标准必须是知识资产（知识库文档 + Skill 正文），不写死代码。
但平台的通用能力之间有 5 处「接口缝隙」需要接通，故集中在此：

  ① scope_for_tools()        —— 把 Agent 的 kb_scope 透传给工具路径的检索
  ② items_to_claims()        —— 需求条目 → subtask_protocol.claims[]（纯数据映射）
  ③ assemble_result()        —— 结构化结果 → quality_report / card_data 通用字段
  ④ load_profile()           —— 标准档案配置 → kb_scope / 类别子集
  ⑤ must_retrieve()          —— 硬门槛判定：声明了必须检索却没检索到就必须报 insufficient

⚠️ 本文件**不定义任何规则、阈值、词表或冲突类型**：它们全部来自
`data/requirement_standards/rule_catalog.json` 与 Skill 正文。
改规则 = 改配置/Skill；改本文件 = 改通用管道，不应发生。

所有函数都是纯函数（除 load_profile 读配置表），可单测、可复现。
"""
from __future__ import annotations

import json
import re
from typing import Any

PROFILE_KEY = "requirement_standard_profile"
CATALOG_REL = "data/requirement_standards/rule_catalog.json"

#: 需要走「配置化需求校验」的意图（来自标准档案 profile，不在此硬编码类别）
CHECK_INTENTS = frozenset({
    "requirement_rule_check",
    "requirement_completeness_check",
    "requirement_conflict_check",
})


# ── ④ 标准档案 ────────────────────────────────────────────────────────────
def load_profile(conn) -> dict:
    """从 generate_rules 读取**当前生效**的标准档案；读不到返回空 dict（fail-open）。

    返回 {"profile_id", "active_profile", "available_profiles",
          "docs", "skills", "agents", "catalog_path", "standard_ids", ...}

    ── 多档案（2026-11-14，米爸要求「拆多套」）──
    数据结构（同一行 `requirement_standard_profile`，向后兼容旧单档案）：

        {"active_profile": "iso29148_incose_v1",
         "profiles": {
             "iso29148_incose_v1": {"standard_ids": [...], "docs": [...], "skills": [...]},
             "iso29148_only_v1":   {...},
             "incose_only_v1":     {...}}}

    · 有 `profiles` ⇒ 取 `active_profile` 指向那套，与顶层公共字段合并（档案可只覆盖差异项）；
    · 无 `profiles` ⇒ 顶层即单档案（旧数据原样可用，不改它们就永远不用迁移）；
    · `active_profile` 指向不存在的 id ⇒ **回退第一套**并打 `_active_fallback=True`
      （宁可退到默认档案继续工作，也不要因为一个手滑的字符串让整条链路 fail-closed）；
    · 回显 `available_profiles` 供前端/CLI 列出可选项。

    ⚠️ 这里只做"读 + 选"，不写库；切换用 `switch_profile()`（唯一写入点）。
    """
    try:
        row = conn.execute(
            "SELECT rule_value FROM generate_rules WHERE rule_key=?", (PROFILE_KEY,)
        ).fetchone()
        if not row:
            return {}
        val = row[0]
        d = json.loads(val) if isinstance(val, str) else dict(val)
        if not isinstance(d, dict):
            return {}
    except Exception:
        return {}

    profs = d.get("profiles")
    if not isinstance(profs, dict) or not profs:
        return d                     # 旧单档案结构：原样返回

    base = {k: v for k, v in d.items() if k not in ("profiles", "active_profile")}
    avail = sorted(profs)
    act = str(d.get("active_profile") or "")
    fallback = False
    if act not in profs:
        act = avail[0]
        fallback = True
    sel = dict(base)
    _one = profs.get(act) or {}
    if isinstance(_one, dict):
        sel.update(_one)
    sel["active_profile"] = act
    sel["available_profiles"] = avail
    sel["profile_id"] = act          # 与 active 同源，避免两处口径漂移
    if fallback:
        sel["_active_fallback"] = True
    return sel


def switch_profile(conn, profile_id: str) -> dict:
    """切换生效的标准档案：**只改 `active_profile` 一个字**，其余原样保留。

    返回 {"ok", "active_profile", "available_profiles", "docs", "standard_ids"}
    失败（档案不存在/结构不对）⇒ ok=False 且**不写库**（绝不落半截状态）。
    """
    row = conn.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                       (PROFILE_KEY,)).fetchone()
    if not row:
        return {"ok": False, "error": "缺少 %s 配置" % PROFILE_KEY}
    try:
        d = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception as e:
        return {"ok": False, "error": "档案 JSON 非法：%r" % (e,)}
    profs = d.get("profiles") or {}
    if not isinstance(profs, dict) or not profs:
        return {"ok": False, "error": "当前是单档案结构，无 profiles 可切换",
                "available_profiles": []}
    pid = str(profile_id or "").strip()
    if pid not in profs:
        return {"ok": False, "error": "档案不存在：%s" % pid,
                "available_profiles": sorted(profs)}
    d["active_profile"] = pid
    conn.execute("UPDATE generate_rules SET rule_value=? WHERE rule_key=?",
                 (json.dumps(d, ensure_ascii=False), PROFILE_KEY))
    conn.commit()
    sel = load_profile(conn)
    return {"ok": True, "active_profile": pid,
            "available_profiles": sorted(profs),
            "docs": sel.get("docs") or [],
            "standard_ids": sel.get("standard_ids") or []}


def profile_scope(profile: dict) -> dict:
    """标准档案 → 检索 scope（只取 docs，避免把无关字段塞进 kb_scope）。"""
    docs = [str(x) for x in (profile.get("docs") or []) if str(x).strip()]
    return {"docs": docs} if docs else {}


def merge_scope(effective_kb_scope: dict | None, profile: dict) -> dict:
    """④ 把标准档案的文档白名单合并进本轮 kb_scope（去重保序）。

    显式建模范围（用户选文档）优先：已存在的 docs 保留在前，档案 docs 追加。
    """
    out = dict(effective_kb_scope or {})
    p_docs = [str(x) for x in (profile.get("docs") or []) if str(x).strip()]
    if not p_docs:
        return out
    cur = [str(x) for x in (out.get("docs") or [])]
    merged = cur + [d for d in p_docs if d not in cur]
    out["docs"] = merged
    return out


def is_check_intent(intent: str) -> bool:
    return str(intent or "") in CHECK_INTENTS


def configured_categories(conn, key: str) -> list[str]:
    """读类别子集配置（如 requirement_rule_categories）。"""
    try:
        row = conn.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?", (key,)).fetchone()
        if not row:
            return []
        v = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


# ── ① 工具路径 scope 透传 ──────────────────────────────────────────────────
def scope_for_tools(pipe) -> dict | None:
    """① 取本轮 effective_kb_scope，供 graph_retrieve 等工具调用传给 rag.retrieve。

    只从 pipe 上已算好的字段读，不重复解析（避免两处取值链漂移）。
    """
    for attr in ("_effective_kb_scope", "_tool_kb_scope"):
        v = getattr(pipe, attr, None)
        if isinstance(v, dict) and v:
            return v
    return None


# ── ② 需求条目 → claims[] ─────────────────────────────────────────────────
def items_to_claims(items: list[dict], task_key: str = "req") -> list[dict]:
    """② 把条目化需求映射成 subtask_protocol 的 claims[]（**纯数据映射**）。

    subject 必须是「对象/指标名」且 ≤12 字符才参与冲突检测（detect_conflicts 的锚点要求）；
    取不到就留空 ⇒ 该条不参与判定，由 conflict_stats.anchored 如实反映，绝不伪造锚点。
    """
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or it.get("content") or "").strip()
        if not text:
            continue
        subj = str(it.get("subject") or it.get("sysml_name") or "").strip()
        claim = {
            "text": text[:500],
            "kind": "to_verify" if it.get("ambiguous") else "fact",
            "subject": subj[:12] if subj else "",
            "task_key": str(it.get("req_id") or task_key),
            "req_id": str(it.get("req_id") or ""),
        }
        out.append(claim)
    return out


def claims_to_summaries(items: list[dict]) -> list[dict]:
    """包装成 detect_conflicts 可吃的 summary 结构。"""
    return [{"task_key": "req", "status": "full", "summary": "",
             "claims": items_to_claims(items)}]


def run_deterministic_conflicts(items: list[dict]) -> dict:
    """调现成的确定性检测 + 可证伪口径；**不实现任何新判据**。"""
    from services import subtask_protocol as sp
    sums = claims_to_summaries(items)
    return {
        "conflicts": sp.detect_conflicts(sums),
        "stats": sp.conflict_stats(sums),
    }


# ── ⑤ 硬门槛（**无开关**，2026-11-14 米爸拍板 D9-A 删除）─────────────────
# 设计取舍（为什么删而不是留着）：
#   ① 原来那个布尔（`requirement_must_retrieve`）名义上管"是否必须检索"，实际同时管着
#      「要不要检索」「要不要产出结构化结果」「没检索到怎么判」——一个开关三种语义；
#   ② 它的原始用途（让"没配标准档案的普通问答"不受影响）**已由 `bool(profile)` 覆盖**，
#      开关纯属第二层冗余；
#   ③ 真机实测它还根本读不到（`must_retrieve(profile)` 调用未传 conn ⇒ 死配置）；
#      同族的 `requirement_check_enabled` 更是全仓零消费点。
#   ⇒ 按"少一个配置项就少一个死配置"的原则直接删除：**有档案 + 需求校验意图 ⇒ 恒硬门槛**。
#     想放宽判据的正确做法是改档案/类别清单（配置），而不是加一个能让整条链静默关闭的开关。
def retrieval_verdict(retrieval: dict | None) -> str:
    """判定检索是否达标：**无命中即 insufficient**（不许写成已检查）。

    证据来源（`agent/rag.py::retrieve()` 的真实返回键，均已核实）：
      · `chunk_hits`  分块级命中预览（rag.py:815，**存在**）
      · `vector_docs` 向量结果（rag.py:814）
      · `entities`    图谱实体（rag.py:812）
      · `vector_count` / `graph_count` 恒有的计数
    多键取"任一有据"而不是只认一个：
      · 单认 `chunk_hits` 时，若上层只透出 `vector_docs`（如工具路径裁剪）会误判未检索；
      · 单认 `vector_docs` 时，低置信过滤会把它清空而 `chunk_hits` 仍保留预览 ⇒ 误判；
      ⇒ 三者 + 计数兜底并集判定，"某一路没透出"不等于"没检索"。

    ⚠️ 记一次教训（2026-11-14）：我一度断言"rag 从不返回 chunk_hits、只是内部局部
      变量"，据此把判据改成只读 `vector_docs` —— **那个断言是错的**（当时只看到
      rag.py 第 814 行就下了结论，实际第 815 行就是 `"chunk_hits": chunk_hits`）。
      错话差点被写死进代码与门禁里，故此处如实记录：**结论要看到定义处再下**。
    """
    if not isinstance(retrieval, dict):
        return "insufficient"
    chunks = retrieval.get("chunk_hits") or retrieval.get("vector_docs") or []
    ents = retrieval.get("entities") or []
    if not isinstance(chunks, list):
        chunks = []
    if not isinstance(ents, list):
        ents = []
    if chunks or ents:
        return "checked"
    # 计数兜底：即使上层把明细裁掉，只要计数>0 也算有据（不因"看不见明细"就判失败）
    try:
        if int(retrieval.get("vector_count") or 0) > 0 or int(retrieval.get("graph_count") or 0) > 0:
            return "checked"
    except Exception:
        pass
    return "insufficient"


# ── ③ 结构化结果组装 ──────────────────────────────────────────────────────
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}", re.M)


def _decode_all_objects(text: str) -> list[dict]:
    """从一个字符串里**依次**解出所有顶层 JSON 对象（含数组里的对象）。

    ⚠️ 2026-11-14 真机：模型输出出现**两段 JSON 直接拼接**
      `{"conflicts":[…]}{"items":[…],"verdict":"checked"}`
      ⇒ `json.loads` 报 `Extra data: line 3 column 1`，
        而原来的兜底正则 `\\{[\\s\\S]*\\}` 是**贪婪**的（从第一个 `{` 吞到最后一个 `}`）
        ⇒ 拼起来仍不是合法 JSON ⇒ 判"解析失败" ⇒ 结构化结果整个丢掉
        （真机表现为 card_data 里 `requirement_check_result` 为空、verdict 被降成 insufficient）。
      这不是模型"没给结果"，是**解析器容错不够**。用 raw_decode 逐段解码并合并，
      比"要么全对、要么全丢"更符合真实输出。

    只做解码+合并，不做任何推断：解不出对象就返回空列表（由调用方判失败，不伪造）。
    """
    out: list[dict] = []
    dec = json.JSONDecoder()
    i, n = 0, len(text or "")
    while i < n:
        while i < n and text[i] in " \t\r\n,;":
            i += 1
        if i >= n:
            break
        if text[i] != "{":
            nxt = text.find("{", i)
            if nxt < 0:
                break
            i = nxt
        try:
            obj, end = dec.raw_decode(text, i)
        except Exception:
            nxt = text.find("{", i + 1)
            if nxt < 0:
                break
            i = nxt
            continue
        if isinstance(obj, dict):
            out.append(obj)
        elif isinstance(obj, list):
            out.extend([x for x in obj if isinstance(x, dict)])
        i = end if end > i else i + 1
    return out


def _merge_objects(objs: list[dict]) -> dict | None:
    """合并多段 JSON：列表键**拼接**，标量键**先到先得**（不覆盖已确认的信息）。"""
    if not objs:
        return None
    if len(objs) == 1:
        return objs[0]
    merged: dict[str, Any] = {}
    for o in objs:
        for k, v in o.items():
            if k not in merged:
                merged[k] = v
            elif isinstance(merged[k], list) and isinstance(v, list):
                merged[k] = merged[k] + v
            elif isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = {**v, **merged[k]}
            # 其余冲突：保留先出现的（模型后面那段常是补充说明，不覆盖正式结论）
    return merged


def parse_result_json(text: str) -> dict | None:
    """从 LLM 正文里抽出结构化 JSON 对象（ tolerant，失败返回 None）。

    容错顺序：整体 → ```json 围栏内 → 贪婪块 → **逐段解码并合并**。
    """
    if not text:
        return None
    s = str(text)
    try:
        d = json.loads(s)
        if isinstance(d, dict):
            return d
        if isinstance(d, list) and d and all(isinstance(x, dict) for x in d):
            return _merge_objects(d)
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    for cand in ([fenced.group(1)] if fenced else []) + [s]:
        m = _JSON_BLOCK.search(cand)
        if m:
            try:
                d = json.loads(m.group(0))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        d = _merge_objects(_decode_all_objects(cand))
        if d:
            return d
    return None


_RULE_ID_RE = re.compile(r"^\s*([A-Z]{2}-\d{2})")


def _rule_id_of(c: dict) -> str:
    """从一条结论里取规则编号 —— 兼容 `rule_id` / `rule` / `category`。

    ⚠️ 2026-11-14 真机：`checks` 的 items 在 output_schema 里**没有声明字段**
      （`{"type":"array"}`），模型于是把编号写进 `category`（形如
      "RC-01 编写模板与句式规范"）⇒ 只读 `rule_id` 会得到空值 ⇒ 前端显示 `[-] 需复核`
      （用户看不出是哪条规则在报错）。
      两件事一起修：本函数容忍三种字段名并从"编号+名称"里取出编号前缀；
      output_schema 同时补声明 items 字段（配置侧），让模型有确定的写法。
    """
    for k in ("rule_id", "rule", "category", "id", "type"):
        v = str(c.get(k) or "").strip()
        if not v:
            continue
        m = _RULE_ID_RE.match(v)
        if m:
            return m.group(1)
        if k in ("rule_id", "rule", "id"):
            return v[:24]
    return ""


def to_quality_issues(result: dict) -> list[dict]:
    """把校验/冲突结论转成 quality_report.issues[]（前端 13-reports.js 现成契约）。"""
    issues: list[dict] = []
    for c in (result.get("checks") or []):
        if not isinstance(c, dict):
            continue
        if str(c.get("verdict") or "") == "pass":
            continue
        _rid = _rule_id_of(c)
        issues.append({
            "problem": "[%s] %s" % (_rid or "-", c.get("suggestion")
                                  or c.get("category") or "需复核"),
            "severity": str(c.get("severity") or "low").lower(),
            "evidence": "%s ｜ %s ｜ 依据 %s" % (
                c.get("req_id") or "-", str(c.get("evidence") or "")[:120],
                "; ".join(c.get("clause_ref") or []) if isinstance(c.get("clause_ref"), list)
                else str(c.get("clause_ref") or "-")),
            "suggestion": str(c.get("suggestion") or "按条款修订")[:200],
        })
    for c in (result.get("completeness") or []):
        if not isinstance(c, dict) or c.get("covered"):
            continue
        issues.append({
            "problem": "[完整性] %s 未覆盖" % (c.get("category") or "-"),
            "severity": "medium",
            "evidence": "；".join(str(x) for x in (c.get("missing") or [])[:5]) or "见原文",
            "suggestion": "补齐该类别缺失项后再复核",
        })
    for c in (result.get("conflicts") or []):
        if not isinstance(c, dict):
            continue
        issues.append({
            "problem": "[冲突] %s %s" % (c.get("type") or "-", c.get("description") or ""),
            "severity": str(c.get("severity") or "medium").lower(),
            "evidence": "%s ｜ %s" % (", ".join(c.get("req_ids") or []),
                                    str(c.get("evidence") or "")[:120]),
            "suggestion": str(c.get("suggestion") or "人工裁决")[:200],
        })
    return issues[:30]


def assemble_result(llm_content: str, *, retrieval: dict | None = None,
                    deterministic: dict | None = None,
                    configured: list[str] | None = None) -> dict:
    """③ 组装最终结构化结果（供 quality_report / card_data 复用）。

    返回 {"ok", "result", "quality_report", "card_patch", "verdict", "reasons"}
    · 解析失败 ⇒ ok=False，绝不伪造结果；
    · 无检索命中 ⇒ verdict=insufficient（硬门槛**恒生效**，无开关，不许写成已检查）；
    · 类别未覆盖齐 ⇒ verdict=insufficient 并列出缺口；
    · 确定性层 anchored==0 ⇒ 不得写「无冲突」。
    """
    parsed = parse_result_json(llm_content)
    out: dict[str, Any] = {
        "ok": False, "result": parsed, "quality_report": None, "card_patch": {},
        "verdict": "insufficient", "reasons": [],
    }
    if parsed is None:
        out["reasons"].append("LLM 未输出可解析的结构化 JSON")
        return out

    verdict = retrieval_verdict(retrieval)
    reasons: list[str] = []
    if verdict == "insufficient":
        reasons.append("标准档案要求先检索条款，但本轮无检索命中（不得声称已按标准校验）")

    if configured:
        # ⚠️ 2026-11-14 真机修正：初版只认模型**自报**的 `covered_categories`，
        #   而该字段在 output_schema 里是可选 ⇒ 模型不报时 covered=[] ⇒ 14 类全判"未覆盖"
        #   ⇒ verdict 恒 insufficient。真机 P1 实测：模型逐条给出了 28 条 checks
        #   （RC-01…RC-09 都在），却因没写 covered_categories 被判"类别未覆盖齐"。
        #   ⇒ 覆盖口径改为**证据制**：`checks[].rule_id` 里真的出现过该类，才算检查过；
        #     模型自报仍并入（两者取并集），但**不能只靠自报**。
        covered = {str(x) for x in (parsed.get("covered_categories") or []) if str(x).strip()}
        for c in (parsed.get("checks") or []):
            if not isinstance(c, dict):
                continue
            rid = str(c.get("rule_id") or c.get("category") or "").strip()
            if rid:
                covered.add(rid)
        for c in (parsed.get("completeness") or []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("category") or c.get("rule_id") or "").strip()
            if cid:
                covered.add(cid)
        # 归一：模型常同时给 `rule_id`="RC-01" 与 `category`="RC-01 编写模板与句式规范"
        # ⇒ 同一类别被计两次（真机 covered=28 而实际只有 14 类）。统一取编号前缀去重。
        _norm: set[str] = set()
        for v in covered:
            m = _RULE_ID_RE.match(v)
            _norm.add(m.group(1) if m else v)
        covered = _norm
        missing = [c for c in configured if c not in covered]
        # 把**证据制**口径回显到结果里（前端/审计要能看出"哪些类别真有逐条结论"，
        # 而不是看到模型自报的那份）。自报原值另存，便于事后比对谁在虚报。
        parsed["covered_categories_declared"] = list(parsed.get("covered_categories") or [])
        parsed["covered_categories"] = sorted(covered)
        # 本轮**实际要求**的类别（来自档案配置）回显：换标准档案时这里必然变化，
        # 是"配置即行为"最省事的可观测锚点（不依赖模型自报）。
        parsed["configured_categories"] = [str(c) for c in configured]
        if missing:
            verdict = "insufficient"
            reasons.append("规则类别未覆盖齐：%s" % ", ".join(missing[:8]))

    det = dict(deterministic or {})
    # ⚠️ 2026-11-14 真机：`run_deterministic_conflicts`（确定性第一层）在**运行时零调用点**
    #   —— 全仓只有单测在调。⇒ 冲突检测实际只有模型语义一层，设计里的"双层"没落地；
    #   表现是 `deterministic_verdict` 恒 None、`conflict_stats` 恒空。
    #   修法：调用方没显式传时，若模型回传了 `items`/`requirements`（需求条目），
    #   就地跑一次确定性层 —— 仍然**复用现成** `subtask_protocol.detect_conflicts`，
    #   不新增任何比对算法。模型没回传条目 ⇒ 保持空（不伪造），由 anchored=0 判据兜底。
    if not det:
        _items = parsed.get("items") or parsed.get("requirements") or []
        if isinstance(_items, list) and _items:
            try:
                det = run_deterministic_conflicts(_items)
            except Exception:
                det = {}
    conflicts = [c for c in (parsed.get("conflicts") or []) if isinstance(c, dict)]
    if det:
        # 双层合并：确定性在前（可复算、可举证），语义在后；按 (类型, 涉及需求) 去重，
        # 避免同一冲突被两层各报一次。
        _seen = {(str(c.get("type") or ""), tuple(c.get("req_ids") or [])) for c in conflicts}
        for c in (det.get("conflicts") or []):
            if not isinstance(c, dict):
                continue
            _k = (str(c.get("type") or ""), tuple(c.get("req_ids") or []))
            if _k in _seen:
                continue
            _seen.add(_k)
            _c2 = dict(c)
            _c2.setdefault("layer", "deterministic")
            conflicts.insert(0, _c2)
        parsed["conflicts"] = conflicts
    if det:
        stats = det.get("stats") or {}
        # ⚠️ conflict_stats 的真字段名是 `claims_anchored`（不是 `anchored`）。
        #   实测返回 {claims_total, claims_anchored, subjects, subtasks_with_claims, verdict}。
        #   读错字段会恒得 0 ⇒ 把「有锚点」误判成「无可判定数据」，硬门槛形同虚设。
        anchored = int(stats.get("claims_anchored", stats.get("anchored", 0)) or 0)
        # 确定性层自身状态：0 锚点 = 它**没能力判定**（undetermined），
        # 不能因为语义层另外检出了冲突就把它标成 checked（那是把"没查"说成"查过"）。
        if anchored == 0:
            parsed["deterministic_verdict"] = "undetermined"
            if not conflicts:
                verdict = "insufficient"
                reasons.append("确定性层无可比对锚点（anchored=0），只能报「未判定」")
        else:
            parsed["deterministic_verdict"] = "checked"
        parsed["deterministic_conflicts"] = det.get("conflicts") or []
        parsed["conflict_stats"] = stats

    issues = to_quality_issues(parsed)
    q_report = None
    if issues:
        q_report = {"issues": issues,
                    "stats": {"total": len(issues), "fuzzy": 0, "no_measure": 0},
                    "score": max(0, 100 - len(issues) * 8)}
    # ⚠️ 2026-11-14 真机：模型常把**整段自然语言结论**写进 `verdict`
    #   （实测 "不通过：3 条需求中仅第 1 条…建议优先处理第 2 条重写…"）。
    #   若原样透出，前端无法据此配色/分支（字段语义应是机器可判的状态）。
    #   ⇒ `verdict` 固定为四个状态值之一，原始结论移到 `conclusion` 保留可读性。
    _model_verdict = str(parsed.get("verdict") or "").strip()
    parsed["conclusion"] = _model_verdict
    _vlow = _model_verdict.lower()
    if _vlow in ("pass", "通过", "ok", "合规"):
        _machine = "pass"
    elif _vlow in ("checked", "已检查", "已校验"):
        # 「检查过、但没有明确结论」≠「通过」—— 归 pass 是实质性过度声明
        _machine = "checked"
    elif _vlow in ("fail", "不通过", "未通过", "ng") or parsed.get("conflicts"):
        _machine = "fail"
    else:
        _fails = [c for c in (parsed.get("checks") or [])
                  if isinstance(c, dict) and str(c.get("verdict") or "") == "fail"]
        _vfs = [c for c in (parsed.get("checks") or [])
                if isinstance(c, dict) and str(c.get("verdict") or "") == "to_verify"]
        # 有 fail ⇒ fail；只有 to_verify / 无结论 ⇒ checked（"检查过，但结论待人工确认"）
        _machine = "fail" if _fails else ("checked" if _vfs or _model_verdict else "checked")
    parsed["verdict"] = "insufficient" if verdict == "insufficient" else _machine
    parsed["standard_check_reasons"] = reasons

    out.update({
        "ok": True,
        "result": parsed,
        "quality_report": q_report,
        "card_patch": {
            "requirement_check_result": parsed,
            "requirement_check_verdict": parsed["verdict"],
            "standard_check_reasons": reasons,
        },
        "verdict": parsed["verdict"],
        "reasons": reasons,
    })
    return out
