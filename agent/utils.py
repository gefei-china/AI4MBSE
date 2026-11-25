"""AgentPipeline 纯辅助函数（P1-5 保守拆分，2026-08-31）：无类依赖/无状态的顶层工具函数。

从 agent/pipeline.py 提取（AgentPipeline 类保持原位），pipeline.py import 后 re-export，
外部既有引用（from agent.pipeline import _archive_artifacts 等）保持兼容。
"""
import json
import re

from core.artifact_titles import (FALLBACK_DOCUMENT_TITLE,
                                     FALLBACK_REPORT_TITLE,
                                     derive_title_from_content)


# ── 问答可解释性：检索分块（chunk_hits）→ 前端 [n] 引用链接数据 ──
def _citations_payload(hits) -> list:
    """把检索到的 chunk_hits 裁剪为前端引用数据（[n] 引用链接 → 来源段落/模型元素跳转）。

    与 _build_context 中【来源n】编号保持 1 基对齐：前端 [n] 映射 cites[n-1]。

    ★ B5-10（2026-10-14 米爸报「引用内容区域总是显示 8 条」）：
      原实现是**写死的 `[:8]`**，与"实际注入了几条"完全脱钩 ⇒
      ① 界面恒显 8 条，看着像"检索到 8 条"，实际是截断常量；
      ② 更要命的是**编号体系错位** —— `_build_context` 只注入
         `rag.inject_k`（默认 3）条【来源n】到 prompt，
         于是正文最多标到 `[3]`，而界面给了 8 个可点条目
         ⇒ **后 5 条的编号在 prompt 里根本不存在，正文永远不可能引用到它们**；
      ③ 实测 4 条真实消息：citations 全是 8，正文 [n] 标注**全是 0**
         —— 用户看到的是一排点不动的编号。

      修法：**与注入同源** —— 取同一个 `rag.inject_k` 作为上限，
      这样"界面有多少条 ⇔ prompt 里有多少个【来源n】"逐一对齐，
      多出来的命中只作为后续轮次的候选池，不再冒充"已引用来源"。
      ⚠️ 读配置失败时回落到 8（原值），保证异常态不把引用区清空。
    """
    try:
        from core import config as _cfg
        _k = int(_cfg.get("rag", "inject_k", 3)) or 3
    except Exception:
        _k = 8
    # ★ 2026-11-01：与 prompt 注入**同源**——都走 `select_for_injection`。
    #   修的是既有编号错位（实测位次 3/4/5 全错）：
    #     prompt 注入的是 `select_diverse` 之后的结果（受 per_doc_max 配额、
    #     近邻合并影响），而这里一直用**原始** `chunk_hits[:k]` ⇒ 同一编号
    #     指向两个不同来源 ⇒ 模型写 `[3]` 指的是 A 文档，用户点第 3 条看到的
    #     是 B 文档（引用指向错误证据，比"少引几条"严重）。
    #   抽成共用函数而不是两边各抄一份：抄两份早晚会因单边改参数再次错位
    #   （B5-10「四处各写一份数字」同族）。
    items = (hits or [])[:_k]
    try:
        from agent.retrieval_diversity import select_for_injection
        _sel, _ = select_for_injection(hits)
        items = _sel[:_k]
    except Exception:
        pass    # 选择器异常 ⇒ 退回原始截断（宁可不合并，也不能让引用区清空）
    out = []
    for h in items:
        out.append({
            "document_id": h.get("document_id"),
            "source_doc": h.get("source_doc", ""),
            "chunk_index": h.get("chunk_index"),
            "section": h.get("section", ""),
            "score": round(float(h.get("score") or 0), 3),
            "confidence_level": h.get("confidence_level", ""),
            "content": (h.get("content") or "")[:800],
            # ★ 2026-11-15：补 `anchor` —— 否则**两条路径条目不同构**：
            #   登记簿路径有 anchor，本回退路径没有 ⇒ 前端读 `x.anchor` 跳转时
            #   走回退就跳不动（真机历史消息 6597/6585/6583 的 anchor 全是 None）。
            #   锚点算法仍然只有 `source_anchor` 一份（不另抄）。
            "anchor": source_anchor(h),
        })
    return out


#: 条款/章节号的**形状**（只管形状，不含任何领域词/文档名）：
#: 形如 `5.2.7` / `4.1.22` / `9.5.9`（点分数字，允许后面跟标题文字）。
#: 可用配置 `context_audit.citation_clause_regex` 覆盖（不同标准编号习惯不同）。
_CLAUSE_SHAPE = re.compile(r"^\s*(\d+(?:\.\d+){1,4})(?![\d.])")


def _clause_re():
    try:
        from core import config as _cfg
        pat = str(_cfg.get("context_audit", "citation_clause_regex", "") or "").strip()
        if pat:
            return re.compile(pat)
    except Exception:
        pass
    return _CLAUSE_SHAPE


def source_anchor(c: dict) -> str:
    """给一条命中片段一个**真实**的引用锚点（【来源n】标签与编号对照表共用）。

    ★ 2026-11-14（米爸报「引用的片段 输出正文没有展示引用标识」）真根因：
      原实现 `_sec = c["section_range"] or c["chunk_index"]` 后拼成 `§{_sec}`。
      但 `section_range` **只在近邻合并时才有值，且是 chunk 区间**（见
      `agent/retrieval_diversity.py:135`），其余一律回落成 `§406` ——
      **把 chunk 序号伪装成条款号**。实测 5 条 citations 的 `section_range` 全为 None。
      于是 prompt 里给模型的是「来源3 = §627」，而它真正想引的是正文里的 §5.2.5：
      两套编号之间**没有任何映射**，模型无法低成本对应 ⇒ 干脆只写条款号、不写 [n]
      （实测 12 条最近回答里有 9 条正文 `[n]` 数为 0，而 `§x.y` 出现 41~63 次）。

    现在的优先级（**只说真话，不冒充**）：
      ① 片段自带 `section` 且形如条款号 ⇒ `§5.2.7`（可带标题，便于模型认出来）
      ② 有 `section_range`（近邻合并区间）⇒ `片段#12-14`
      ③ 都没有 ⇒ `片段#406`（明确写"片段"，不写"§"）

    ★ 2026-11-15：从 `agent/pipeline_parts/context.py` **上移到本模块** ——
      引用登记簿（`_register_citations`）也要用它算锚点，而登记簿在本模块；
      留在 context.py 会变成"工具路径拿不到锚点 ⇒ 两处锚点不一致"。
    """
    sec = str(c.get("section") or "").strip()
    if sec:
        m = _clause_re().match(sec)
        if m:
            head = m.group(1)
            tail = sec[m.end():].strip(" -–—:：·|")
            return "§%s%s" % (head, (" " + _short(tail, 24)) if tail else "")
    rng = str(c.get("section_range") or "").strip()
    if rng:
        return "片段#%s" % rng
    idx = c.get("chunk_index", 0)
    return "片段#%s" % idx


def _short(s: str, cap: int) -> str:
    """按**词边界**截断（不要在词中间切，否则 `ISO 29148-2011 Require` 这种半截词
    反而让模型认不出是哪份文档 —— 对照表存在的意义就是让它一眼对上号）。

    ★ 2026-11-15：从 `agent/pipeline_parts/context.py` **上移**到本模块。
      上移原因：引用登记簿（在本模块）算锚点时也要截断尾巴，
      两处各写一份截断函数必然漂移（本次就发现两份的边界条件不一样）。
      **以原 context.py 那份为准**（`sp >= cap*0.55` 才按词切 + 去尾部标点）。
    """
    s = re.sub(r"\s+", " ", str(s or "").strip())
    if len(s) <= cap:
        return s
    cut = s[:cap]
    sp = cut.rfind(" ")
    if sp >= cap * 0.55:          # 太靠前就不按词切了，否则只剩一两个词
        cut = cut[:sp]
    return cut.rstrip(" -–—,，。:：|") + "…"


# ══════════════════════════════════════════════════════════════════════════
# 引用登记簿（2026-11-15）——【来源n】与前端 citations 的**唯一真源**
# ══════════════════════════════════════════════════════════════════════════# ★ 为什么要它（真机 conv 999999033 / msg 6603 实证）：
#   Skill 的硬门槛要求"必须先调用 graph_retrieve 检索条款原文" ⇒ **主路径就是工具检索**。
#   但：
#     · 提示词注入的【来源n】来自**初始检索**（`retrieval.chunk_hits`）；
#     · 工具检索的命中**只留在工具局部变量里**，从不回写 retrieval；
#     · 而 `citations`（前端可点条目）也只从**初始检索**取。
#   ⇒ 走工具路径时 citations 必为空；而工具输出文本自带**另一套**【来源 1】~【来源 4】，
#     模型照着那套写 [1]~[5] ⇒ **正文里的编号全部越界、点不动**
#     （真机：citations=0，5 个 [n] 全部越界；模型还照着工具里的编号写了 §4.1.6/§4.2.2）。
#   ⇒ 必须有一本**全局登记簿**：初始检索与工具检索登记到同一处，
#     编号全局唯一且单调递增，两边都用它 ⇒ 模型写 [n] 必然能点。
#
# 登记簿条目结构（纯 dict，JSON 可序列化，便于落库/调试）：
#   {"n": 编号(1基), "chunk": 原始 hit, "doc": 文档名, "anchor": 锚点}


def _citation_key(h: dict) -> str:
    """命中片段的**去重键**（同一片段在初始检索与工具检索里可能各出现一次）。"""
    did = str(h.get("document_id") or "").strip()
    if not did:
        did = str(h.get("source_doc") or "").strip()
    return "%s|%s" % (did, str(h.get("chunk_index") or ""))


def _register_citations(registry: list, hits, *, limit: int = 0,
                        max_total: int = 0) -> list:
    """把命中片段登记进登记簿，返回**逐条对应 hits** 的条目列表。

    :param registry: 登记簿（list，就地追加）—— 必须在**一次用户请求内**保持同一个对象。
    :param hits: 命中片段列表。
    :param limit: 本次最多登记几条（0 = 不限）。
    :param max_total: 登记簿总条数上限（0 = 不限）。达上限后新片段**不分配编号**
        （但**仍然返回**，只是 ``numbered=False``）—— 调用方照样把内容给模型看，
        只是不给它可引用编号。宁可少一个可点条目，也不制造"正文写了 [n] 但点不动"的死编号。
    :returns: 与 ``hits`` **等长**的列表，每项：
        ``{"n": 编号或 None, "numbered": bool, "chunk": hit, "doc": 文档名, "anchor": 锚点}``

    ★ 已登记过的片段**复用原编号**（不占新号）——
      否则同一份文档被初始检索和工具检索各命中一次时会占两个号，
      编号空间被无谓撑大，且"同一个 [n] 指向两条内容"会让模型对不上。
    ★ 返回等长是关键：调用方要**逐条**把内容展示给模型（有的带编号、有的不带）。
    """
    out = []
    if registry is None:
        return out
    _seen = {}
    for _e in registry:
        try:
            _seen[_e.get("key")] = _e
        except Exception:
            continue
    for h in (hits or []):
        if not isinstance(h, dict):
            continue
        if limit and len(out) >= limit:
            break
        _k = _citation_key(h)
        if _k in _seen:
            # ★ 2026-11-15：返回**登记簿里的同一个对象**（不是副本）——
            #   原实现 `dict(_seen[_k])` 返回副本 ⇒ 调用方写的 `_e["anchor"]=...`
            #   写回不到登记簿 ⇒ `_citations_payload_from_registry` 读出来的 anchor
            #   **永远为空**（真机 msg 6612 实测：8 条 citations 的 anchor 全是 ""）。
            #   教训：返回副本 ⇒ 调用方的写回全部丢失（"验证对象也会错"同族）。
            _e = _seen[_k]
            _e["numbered"] = True
            out.append(_e)
            continue
        if max_total and len(registry) >= max_total:
            out.append({"n": None, "numbered": False, "key": _k, "chunk": h,
                        "doc": h.get("source_doc", ""),
                        # 锚点照算：未分配编号的补充材料也该能跳转到位置，
                        #   只是不能被正文引用而已。
                        "anchor": source_anchor(h)})
            continue      # 达上限：不分配编号
        _n = len(registry) + 1
        _entry = {"n": _n, "numbered": True, "key": _k, "chunk": h,
                  "doc": h.get("source_doc", ""),
                  # ★ 锚点在**登记时**就算好（单一真源）：
                  #   以前靠调用方回写 `_e["anchor"]`，返回副本 ⇒ 写回丢失 ⇒ 全空。
                  #   现在登记簿自带锚点，调用方不再各算一份。
                  "anchor": source_anchor(h)}
        registry.append(_entry)
        _seen[_k] = _entry
        out.append(_entry)
    return out


def _citations_payload_from_registry(registry: list) -> list:
    """从登记簿生成前端引用数据（与【来源n】1 基对齐）。

    ⚠️ 与 `_citations_payload` 的差别：后者按 `rag.inject_k` 截断**初始检索**的
    chunk_hits；本函数返回登记簿的**全部**已分配编号条目（初始检索 + 工具检索）。
    登记簿为空 ⇒ 返回 []（调用方回退旧逻辑）。
    """
    out = []
    for e in (registry or []):
        h = e.get("chunk") or {}
        if not isinstance(h, dict):
            continue
        out.append({
            "document_id": h.get("document_id"),
            "source_doc": h.get("source_doc", ""),
            "chunk_index": h.get("chunk_index"),
            "section": h.get("section", ""),
            "score": round(float(h.get("score") or 0), 3),
            "confidence_level": h.get("confidence_level", ""),
            "content": (h.get("content") or "")[:800],
            "anchor": e.get("anchor") or "",
        })
    return out


def _conversation_project_id(conn, conversation_id) -> str:
    """会话归属项目（无则空串）。

    2026-09-20：报告归档此前**硬编码** `'project-satnet-broadband'`（域固化残留；该列 DDL
    默认值也是它），使报告的项目归属与会话实际归属无关。改取会话自身归属 ——
    会话的项目由「用户配置的默认项目」决定（见 repositories.project_repo.resolve_project_id）。

    2026-09-28 P1-1：实现**委托**给 `repositories.project_repo.conversation_project_id`
    （唯一实现，取数失败会打 warning + traceback —— 本文件的旧拷贝是静默 `return ""`，
    "取不到"与"本就没有归属"症状相同，排查成本极高）。保留本函数只为不动既有调用点。
    """
    from repositories.project_repo import conversation_project_id
    return conversation_project_id(conn, conversation_id)


# ── 会话产物归档（AI 生成内容 → artifacts 表 + 报告自动写 reports 表）──
_FENCE_OPEN = re.compile(r"^\s*```([\w+-]+)\s*$")   # 带语言标签的开围栏
_FENCE_BARE = re.compile(r"^\s*```\s*$")            # 裸 ```（闭合/无标签开块）
_FENCE_ANY = re.compile(r"^\s*```")                 # 任意围栏形态（含带尾巴的闭合）


def _extract_code_blocks(content: str, limit: int = 5) -> list:
    r"""从 markdown 文本提取 ``` 围栏代码块，返回 [{lang, code}]（最多 limit 条）。

    ★ 2026-11-21（真库 conv 999999977 msg 7205 实证）：原实现用非贪婪正则
      ```([\w+-]*) + (.*?) + ``` 顺序配对 —— 模型写了**未闭合的 ```json**
      后又开 ```sysml 时，```sysml 被当成 json 的**闭合围栏**消费掉
      ⇒ 后面**整段真正的 SysML 代码**被错配成 text/错位 lang
      ⇒ 被 `lang ∈ (sysml,kerml,…)` 过滤丢弃 ⇒ 版本/产物全部丢失
      （该轮 3 段代码 5 个 package，归档 0 条）。
      修法：行级状态机 —— **带语言标签的 ```lang 一律视为"开新块"**：
      模型写 ```sysml 的意图只可能是开代码块，绝不可能是闭合上一个块；
      若当前块未闭合，先在那里截断闭合它。裸 ``` 才是闭合。
      附带抢救：**末尾未闭合的块也收**（模型漏写最后一个闭合围栏时，
      代码本身是有效产出，不能因为少一行 ``` 就整段丢掉）。
    """
    blocks = []
    cur_lang, cur_lines = None, []

    def _flush(cur_lang, cur_lines):
        code = "\n".join(cur_lines).strip()
        if code and len(code) >= 20:  # 过滤过短片段（与旧判据一致）
            blocks.append({"lang": cur_lang, "code": code})

    for raw in (content or "").splitlines():
        if cur_lang is None:
            m = _FENCE_OPEN.match(raw)
            if m:
                cur_lang, cur_lines = m.group(1), []
            elif _FENCE_BARE.match(raw):
                cur_lang, cur_lines = "text", []   # 裸开块（旧正则 lang=''→text 的兼容）
            continue
        # ── 块内 ──
        m = _FENCE_OPEN.match(raw)
        if m:
            # 带标签围栏 = 开新块 ⇒ 先截断闭合当前块（漏闭合抢救）
            _flush(cur_lang, cur_lines)
            cur_lang, cur_lines = m.group(1), []
            if len(blocks) >= limit:
                return blocks[:limit]
            continue
        if _FENCE_ANY.match(raw):
            _flush(cur_lang, cur_lines)            # 裸/带尾巴 ``` = 闭合
            cur_lang, cur_lines = None, []
            if len(blocks) >= limit:
                break
            continue
        cur_lines.append(raw)
    if cur_lang is not None:                        # 末尾未闭合块：同样抢救
        _flush(cur_lang, cur_lines)
    return blocks[:limit]


def _archive_impact_analysis(conn, conversation_id: int, message_id: int,
                             card_data, user_input: str, created_by: str = "未登录") -> None:
    """CIA（FR-CIA-3）：变更影响分析结果快照落库（含失败引导记录，可追溯）。

    在 execute / execute_stream 两条落库链路统一调用；失败静默（不影响会话主流程）。
    """
    try:
        from repositories.impact_repo import ImpactRepo
        _cd = json.loads(card_data) if isinstance(card_data, str) else (card_data or {})
        if _cd.get("intent") == "impact" or _cd.get("ok") is not None or _cd.get("impact_nodes") is not None:
            ImpactRepo(conn).create_analysis(
                title=(_cd.get("change_source") or {}).get("name", str(user_input)[:60]),
                change_source=(_cd.get("change_source") or {}).get("name", ""),
                params=_cd.get("params", {}),
                result=_cd,
                status="ok" if _cd.get("ok") is not False else "error",
                error_code=_cd.get("code", ""),
                conversation_id=conversation_id, message_id=message_id,
                created_by=created_by)
    except Exception:
        pass


def _archive_sysml_version(conn, conversation_id: int, message_id: int,
                           sysml_views: dict, created_by: str = "未登录",
                           code_text: str = "") -> int | None:
    """AI 建模 SysML 版本建档：每次 AI 生成/修订 SysML → sysml_versions 新版本（v0.n 递增）。

    幂等：同一 (会话, 消息) 已建档则不重复（同一消息的 views 不变）。
    修订链：旧 current/draft → superseded，新版本 current。
    返回新版本 id；失败返回 None（不阻断归档主流程）。
    """
    try:
        sysml_views = sysml_views or {}
        # ★ 2026-11-09（会话 990541）：原守卫要求必须有结构化视图，导致「直接吐代码」的
        #   交付轮不入版本链 —— 实测真正 pass 的那轮没建档，库里的 current 反而是坏的。
        #   现放宽为：有视图 **或** 有 sysml 代码文本，都建档（调用点已按 lang 收紧了代码来源）。
        _has_views = bool(sysml_views.get("views") or {})
        if not _has_views and not (code_text or "").strip():
            return None
        # 幂等：同消息已建档跳过
        dup = conn.execute(
            "SELECT COUNT(*) FROM sysml_versions WHERE conversation_id=? AND message_id=?",
            (conversation_id, message_id)).fetchone()[0]
        if dup:
            return None
        # 会话版本计数 + 上一版本（修订链）
        #
        # ★★ 2026-11-13（真机 conv 990727 实证）：**版本链必须按「产物主体」分组**。
        #   原实现按**会话**计数：`WHERE conversation_id=?` ⇒ label = v0.{会话版本数+1}。
        #   后果（实测该会话）：
        #     v0.1 = `package '串联混动汽车运行需求'`（需求视图）
        #     v0.2 = `package '串联混动汽车用例'`（用例视图）— parent_id 竟指向 v0.1
        #   ⇒ 两个完全不同的产物被串成一条修订链，用户在"演进路线"里看到
        #     「用例视图是需求视图的 v0.2」，版本号与内容完全对不上。
        #   修法：subject = `derive_subject(code_text)`（package 名 → 主定义名 → 实例名），
        #     同 subject 才 +1 与串联 parent；不同 subject 各自从 v0.1 起。
        #   subject 为空（无代码、只有结构化视图）时退回按会话计数，
        #   保持旧行为（这类轮次本来只有一张视图，串链风险低）。
        subject = ""
        try:
            if (code_text or "").strip():
                from core.artifact_naming import derive_subject as _dsub
                subject = str(_dsub(code_text, "") or "")[:80]
        except Exception:
            subject = ""
        if subject:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(id) AS last_id FROM sysml_versions "
                "WHERE conversation_id=? AND subject=?",
                (conversation_id, subject)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(id) AS last_id FROM sysml_versions WHERE conversation_id=?",
                (conversation_id,)).fetchone()
        n = row["n"] if row else 0
        last_id = row["last_id"] if row else None
        label = f"v0.{n + 1}"
        # 元素摘要（结构化视图 nodes/edges 汇总）
        #
        # ★ 2026-11-09 修正（会话 990541 实测）：原实现直接 append，**不去重**。
        #   同一个元素会出现在多个视图（BDD/IBD/活动图各画一次），于是 entities 记的是
        #   「跨视图出现次数」而不是「元素个数」。实测该会话 v0.3 报 entities=12，
        #   实际唯一名只有 4 个（CommunicationSatellite / Transponder / satellite / transponder），
        #   重复率 67%。这个虚高数字会直接注入下一轮上下文，等于给模型一个错的事实。
        #   去重保序：既修正计数，也保住"先出现的先列"这个可读性。
        def _uniq_keep(seq):
            _seen, _out = set(), []
            for _x in seq:
                # 过滤不可解码的乱码节点（U+FFFD）：实测 conv 9586/9779/9629 的 nodes 里
                # 有整片 '��������' —— 那是历史 GBK/UTF-8 误解码留下的。放进契约与告警里
                # 只会制造噪音（用户看到一串方块，完全无法判断该补什么）。
                if not _x or "\ufffd" in _x:
                    continue
                if _x not in _seen:
                    _seen.add(_x)
                    _out.append(_x)
            return _out

        nodes, edges = [], []
        for vname, vdata in (sysml_views.get("views") or {}).items():
            for nd in (vdata.get("nodes") or []):
                nodes.append(nd.get("name") or nd.get("id") or "")
            for ed in (vdata.get("edges") or []):
                edges.append(f"{ed.get('source') or ''}--{ed.get('type') or ''}--{ed.get('target') or ''}")
        nodes = _uniq_keep(nodes)
        edges = _uniq_keep(edges)
        summary = {
            "entities": len(nodes), "relations": len(edges),
            "nodes": nodes[:200], "edges": edges[:200], "intent": sysml_views.get("intent", ""),
            "views": len((sysml_views.get("views") or {}))}
        # P3（集成指南 §2.2 接入点③）：把「生成后本地校验（checker.jar）」摘要随版本链留痕。
        # 摘要由 cards.py::_check_generated_sysml 产出、挂在 views 的兄弟键 `check`
        # （与既有 quality_check 同级）。价值：`/api/sysml-versions/{id}` 本来就会
        # json.loads(element_summary) 返回 → **前端零改动**即可显示「这个版本当时合不合法」。
        # 字段：rc/verdict(pass|report|block|unavailable)/blocked/n_error/n_syntax/n_semantic/
        #       n_lexical/n_hard/n_warn/scope/top(前 5 条诊断)/at。
        #       判据只认**硬错** = 词法 + 语法（n_hard>0 → blocked）；语义错只报告不阻断
        #       （2026-09-19 三路化：词法/语法/语义分列，词法与语法同门槛）。
        _chk = sysml_views.get("check")
        if isinstance(_chk, dict) and _chk:
            summary["check"] = _chk
        # 相对上版差异 + 累积契约
        #
        # ★ 2026-11-09 修正（会话 990541 / 全库实测）：原实现**只算 added_nodes**，
        #   没有任何 removed 维度。后果是「修订时把上一版的东西丢了」这件事在数据层
        #   **结构上不可见** —— 全库 131 个版本里含 removed* 字段的只有 1 个（缺 99.2%），
        #   而按 element_summary 逐对复算：37 个可比对父子版本中有 32 对（86.5%）发生元素丢失，
        #   最大一对从 67 个元素掉到 33 个（conv 514 v0.4→v0.5）。
        #   只记"新增"的 diff 永远报喜不报忧，这是"前面犯的错后面还在犯"的数据层根因。
        #
        # ★ 为什么还要 contract（累积契约）：逐版比对会漏掉**累积蚕食** ——
        #   v1 {A,B} → v2 丢 A（removed=[A]）→ v3 只改别处（removed=[]），
        #   到 v3 时「当前 vs 上一版」完全正常，但 A 已经消失两轮了。
        #   contract = 本版本链**历史上出现过的元素并集**，只有对契约比才能算"累计还差几个"。
        #   只记录、不阻断（shadow 语义）：先观察真实噪声，再决定是否升级为拦截闸。
        _old = {}
        if last_id:
            try:
                _old = json.loads(conn.execute(
                    "SELECT element_summary FROM sysml_versions WHERE id=?", (last_id,)).fetchone()["element_summary"] or "{}")
            except Exception:
                _old = {}
            if not isinstance(_old, dict):
                _old = {}

        diff = {"added_nodes": nodes[:200], "removed_nodes": [],
                "added_edges": [], "removed_edges": [], "kept_nodes": len(nodes)}
        if _old:
            # 旧数据（2026-11-09 之前）的 nodes 未去重，这里统一按集合比，避免重复项被误判为新增
            _old_nodes_raw = [x for x in (_old.get("nodes") or []) if x]
            _old_nodes_order = _uniq_keep(_old_nodes_raw)
            _old_nodes = set(_old_nodes_raw)
            _cur_nodes = set(nodes)
            diff["added_nodes"] = [x for x in nodes if x not in _old_nodes][:200]
            # removed 保持上一版的出现顺序，便于模型对回它自己写过的东西
            diff["removed_nodes"] = [x for x in _old_nodes_order if x not in _cur_nodes][:200]
            diff["kept_nodes"] = len(_cur_nodes & _old_nodes)
            _old_edges_raw = [x for x in (_old.get("edges") or []) if x]
            _old_edges = set(_old_edges_raw)
            _cur_edges = set(edges)
            diff["added_edges"] = [x for x in edges if x not in _old_edges][:200]
            diff["removed_edges"] = [x for x in _uniq_keep(_old_edges_raw) if x not in _cur_edges][:200]

        # 累积契约：**滚动窗口**内出现过的元素并集。
        # 为什么不用「全历史并集」：实测 conv 514 有 9 个版本、跨越多天多个主题，
        # 全历史并集 223 个元素、末版只剩 50 个 ⇒ 报"缺 173 个"，其中绝大多数是
        # **改名/重构**留下的旧名，不是真的丢。契约窗口限定为最近 N 版，
        # 关注"近期写过、现在没有"（这才是米爸说的"前面有的后面丢了"）。
        _CONTRACT_WINDOW = 5
        _recent = []
        try:
            _rows = conn.execute(
                "SELECT element_summary FROM sysml_versions WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?", (conversation_id, _CONTRACT_WINDOW)).fetchall()
            for _r in _rows:
                _raw = _r[0] if not hasattr(_r, "keys") else _r["element_summary"]
                try:
                    _e = json.loads(_raw or "{}")
                except Exception:
                    _e = {}
                if isinstance(_e, dict):
                    _recent.extend([x for x in (_e.get("nodes") or []) if x])
        except Exception:
            _recent = [x for x in (_old.get("nodes") or []) if x]
        _contract = _uniq_keep(_recent + nodes)
        _cur_set = set(nodes)
        _lost = [x for x in _contract if x not in _cur_set]
        summary["contract"] = _contract[:200]
        summary["coverage"] = {
            "contract_size": len(_contract),
            "covered": len(_cur_set & set(_contract)),
            "lost_n": len(_lost),
            "lost": _lost[:200],
            "window": _CONTRACT_WINDOW,
        }
        # 旧版本状态收敛：draft/current → superseded
        #   ★ 2026-11-13：**只在同一 subject 内收敛**。原实现是「整个会话最后一条
        #   current/draft」无差别 supersede ⇒ 一旦按 subject 分组计数，
        #   生成"用例视图 v0.1"会把并存的"需求视图 v0.1"也标成 superseded
        #   ⇒ 两个独立产物互相把对方作废（版本状态失真，切版本时找不到 current）。
        if subject:
            cur = conn.execute(
                "SELECT id, status FROM sysml_versions "
                "WHERE conversation_id=? AND subject=? AND status IN ('draft','current') "
                "ORDER BY id DESC LIMIT 1", (conversation_id, subject)).fetchone()
        else:
            cur = conn.execute(
                "SELECT id, status FROM sysml_versions WHERE conversation_id=? AND status IN ('draft','current') "
                "ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
        if cur:
            conn.execute("UPDATE sysml_versions SET status='superseded' WHERE id=?", (cur["id"],))
        # P1-1（2026-09-28）：project_id **写入时定格**会话所属工程（空=无工程会话，合法）。
        # 此后会话改归属，历史版本仍留在它产生时所属的工程下（写回智源的目标工程不漂移）。
        cur2 = conn.execute(
            "INSERT INTO sysml_versions (artifact_id, conversation_id, message_id, project_id, version_label, content, diff, element_summary, parent_id, status, created_by, code_text, subject) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (0, conversation_id, message_id,
             _conversation_project_id(conn, conversation_id), label,
             json.dumps(sysml_views, ensure_ascii=False), json.dumps(diff, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), last_id, "current", created_by,
             code_text or "", subject))
        # 不在此 commit：由调用方（db_conn 上下文）统一提交，保证与消息落库同事务
        return cur2.lastrowid
    except Exception:
        # 不留痕的话，"版本没建出来"与"会话本就无版本"症状完全相同，排查成本极高
        import logging, traceback
        logging.getLogger(__name__).warning(
            "[save_sysml_version] 版本落库失败（conv=%s msg=%s）：\n%s",
            conversation_id, message_id, traceback.format_exc())
        return None


def extract_sysml_doc_from_result(tool_name: str, result_text: str) -> dict | None:
    """从工具结果里识别「完整 SysML/KerML 文档」（纯函数，2026-10-17 米爸反馈4）。

    背景（会话 9446 实测）：智源导出→读取链路取回的 24688 字节全文在**工具结果**
    里（JSON 信封 fileName+content），AI 正文只有分析描述——
    `_archive_artifacts` 的正文代码块判据够不着 ⇒ 产物库空；
    AI 想落盘又无写入通道（技能未命中 file_write 不注入）⇒ 自述「没有写入能力」。
    ⇒ 在**工具结果返回的瞬间**把交付物摘出来，persist 时登记进产物库。

    识别形态（两种都认）：
    A. JSON 信封：有 ``fileName``（.sysml/.kerml 结尾）+ ``content``（≥500 字符），
       且 ``eof``/``completeFile`` 标记完整（分片读取的中间片段不归档，防半个文件）；
    B. 裸文本：直接以 ``package`` 开头的 SysML 源码 ≥500 字符。

    :returns: ``{"title": 文件名或通用标题, "text": 全文}``，不命中返 None。
    """
    t = (result_text or "").strip()
    if len(t) < 500:
        return None
    # 形态 A：JSON 信封（智源 export READ 动作的实际形态）
    if t.startswith("{"):
        try:
            d = json.loads(t)
        except Exception:
            d = None
        if isinstance(d, dict):
            fn = str(d.get("fileName") or "").strip()
            body = d.get("content")
            # ★ 2026-10-27（会话 9629 实测）：**分页读取的尾段不是全文**。
            #   该轮 export_sysml_v2 按 offset 0/32767/65534 三段读（maxBytes=32768），
            #   只有第三段带 eof=true，但它的 content 只是 65534 之后的**尾部 15KB**。
            #   旧判据只看 `eof is True` ⇒ 会把半截尾巴当全文归档（比不归档更坏：
            #   产物库里出现一个"看起来完整"的残缺文件）。
            #   ⇒ 带非零 offset 的段一律交给 accumulate_paged_doc 累积，单段不单独归档。
            _off = d.get("offset")
            try:
                _off_n = int(_off) if _off is not None else 0
            except Exception:
                _off_n = 0
            _paged = (_off_n > 0) or ("nextOffset" in d)
            if isinstance(body, str) and len(body) >= 500 and not _paged:
                _complete = (d.get("eof") is True) or (d.get("completeFile") is True) \
                    or ("eof" not in d and "completeFile" not in d)
                _is_sysml = fn.lower().endswith((".sysml", ".kerml")) or \
                    body.lstrip().lower().startswith("package")
                if _complete and _is_sysml:
                    return {"title": fn or "SysML 源码（工具取回）", "text": body}
    # 形态 B：裸文本 SysML 源码
    if t.lower().startswith("package"):
        return {"title": "SysML 源码（工具取回）", "text": t}
    return None


def accumulate_paged_doc(pages: dict, tool_name: str, result_text: str) -> dict | None:
    """**分页读取**的通用累积器：按字节偏移把多个分片拼成全文，只在「齐了」时返回。

    为什么需要（会话 9629 实测，产物库为空的真因之二）：
      智源 `export_sysml_v2` 的 READ 是**分页协议**——一次只回 maxBytes 字节，
      带 ``offset`` / ``bytesRead`` / ``nextOffset`` / ``eof``。80KB 的导出要读 3 段。
      旧链路对每段独立判断「是否完整」⇒ 尾段(eof=true)被当成整份文件，
      而前面两段被丢弃 ⇒ 要么归档半截、要么（配合响应截断）干脆一段都不命中。

    判据（**协议通用，不针对任何具体平台/工具**）：
      · 信封可解析，且有 ``content``；
      · 分片按 ``offset`` 落位，用 ``bytesRead``（缺省则按 UTF-8 字节数）串成**无缝链**；
      · 链首必须从 0 开始，末段 ``eof``/``completeFile`` 为真，且**每一段都在**；
      ⇒ 齐全才返回 ``{"title": 文件名, "text": 拼接全文}``，否则返回 None（半截不入库）。

    :param pages: 调用方持有的累积状态 dict（每请求生命周期内复用；键=文件名/taskId）；
                  本函数**就地**写入，便于跨轮跨工具累积。
    """
    t = (result_text or "").strip()
    if not t.startswith("{"):
        return None
    try:
        d = json.loads(t)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    body = d.get("content")
    if not isinstance(body, str) or not body:
        return None
    fn = str(d.get("fileName") or "").strip()
    key = fn or str(d.get("taskId") or "") or str(tool_name or "")
    try:
        off = int(d.get("offset")) if d.get("offset") is not None else 0
    except Exception:
        off = 0
    try:
        nbytes = int(d.get("bytesRead")) if d.get("bytesRead") is not None \
            else len(body.encode("utf-8"))
    except Exception:
        nbytes = len(body.encode("utf-8"))
    eof = (d.get("eof") is True) or (d.get("completeFile") is True) \
        or ("eof" not in d and "completeFile" not in d and "nextOffset" not in d)
    st = pages.setdefault(key, {"segs": {}, "title": fn, "tool": str(tool_name or "")})
    st["segs"][off] = {"text": body, "nbytes": nbytes}
    if not eof:
        return None      # 还没读到末段 ⇒ 不归档（防半截）
    # 串链：从 0 起，每段的 [offset, offset+bytesRead) 必须与下一段首尾相接
    pos = 0
    parts = []
    for o in sorted(st["segs"]):
        seg = st["segs"][o]
        if o != pos:
            return None  # 有缺口（中间段缺失/被截断）⇒ 不归档
        parts.append(seg["text"])
        pos = o + seg["nbytes"]
    text = "".join(parts)
    if len(text) < 500:
        return None
    _is_sysml = key.lower().endswith((".sysml", ".kerml")) \
        or text.lstrip().lower().startswith("package")
    if not _is_sysml:
        return None
    return {"title": st.get("title") or "SysML 源码（分页取回）", "text": text}


def collect_tool_sysml_doc(pages: dict, tool_name: str, result_text: str) -> dict | None:

    """工具结果 → 完整 SysML 文档的唯一入口（单次全文 + 分页累积两条路）。

    调用方（stream / execute 同构）只调本函数，不再各自拼判据——避免两条路径判据漂移。
    """
    doc = extract_sysml_doc_from_result(tool_name, result_text)
    if doc:
        return doc
    try:
        return accumulate_paged_doc(pages, tool_name, result_text)
    except Exception:
        return None


#: 工具调用标记泄漏的识别（实测 conv 9629 msg 4892：模型把 tool call 写成了正文，
#: 形如 `<｜｜DSML｜｜ invoke name="export_sysml_v2">`，全角 `｜`(U+FF5C) 与半角 `|` 都会出现）。
#: 后果有两层：① 用户直接看到一串机器标记；② 那些调用**根本没执行**，
#: 模型却按"已发出"继续叙事（随后自述"工具额度已用尽"）。
_LEAK_MARK = re.compile(r"(?:DSML|dsml|antml|ANTML)")
#: 标记外层的定界符（全角/半角竖线，允许 1~2 个）
_LEAK_SEP = "｜|"
#: 清理后可能残留的碎片行（整行只有 `xxx>` 这种残骸）
_LEAK_RESIDUE = re.compile(r"(?m)^\s*(?:invoke|parameter|calls|function_calls|"
                           r"function|parameters|function_results)\s*>?\s*$")


def strip_leaked_tool_markup(text: str) -> tuple:
    """清掉正文里泄漏的工具调用标记，返回 ``(clean_text, removed_count)``。

    为什么用"扫描"而不是一条正则：泄漏标记的闭合形态不稳定（`</｜｜DSML｜｜ invoke>`
    / `<｜｜DSML｜｜ parameter ...>` / 只留 `<... calls>`），单条正则非贪婪会把
    开标签和闭标签拆开、留下 `parameter>` 这类碎片。这里按"标记出现处向左扩到最近的
    `<`、向右扩到最近的 `>`"整段切除，循环到没有标记为止，再清残骸行。

    只做**可见性**处理；**不**把泄漏内容还原成真实工具调用——参数未经协议校验，
    贸然执行可能带错 taskId 重复提交，比不执行更危险。
    """
    s = text or ""
    if not s:
        return s, 0
    marks = list(_LEAK_MARK.finditer(s))
    if not marks:
        return s, 0
    first = marks[0]
    # 从"第一个标记所属的 `<`"起**整段截断**：标记之后全是参数值等机器残骸
    # （实测残留 `STATUS` / `2114956963811217410` / `1` / `100` 这类裸值），
    # 逐段删会留下这类碎片，比原样更难看。正文 prose 在标记之前，截断即干净。
    cut = s.rfind("<", max(0, first.start() - 64), first.start() + 1)
    if cut < 0:
        cut = first.start()
    return s[:cut].rstrip(), len(marks)


def _conversation_goal_text(conn, conversation_id) -> str:
    """取"生成时的诉求与目标"：优先会话标题（本就是诉求的精炼），退回首条用户消息。

    不调 LLM：命名一旦依赖模型输出，同一段代码在不同时刻可能得到不同名字，
    与"多版本保持一致"直接冲突。
    """
    try:
        row = conn.execute("SELECT title FROM conversations WHERE id=?",
                           (conversation_id,)).fetchone()
        if row:
            t = (row[0] if not hasattr(row, "keys") else row["title"]) or ""
            if str(t).strip():
                return str(t).strip()
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND role='user' "
            "ORDER BY id LIMIT 1", (conversation_id,)).fetchone()
        if row:
            c = (row[0] if not hasattr(row, "keys") else row["content"]) or ""
            return str(c).strip().split("\n")[0][:80]
    except Exception:
        pass
    return ""


def _resolve_code_artifact_title(conn, conversation_id, code_text: str,
                                 version_label: str = "", lang: str = "sysml") -> tuple:
    """产物标题 + 文件名（**会话内锁定**，多版本只变版本后缀）。

    为什么锁定（米爸 2026-11-09「多版本要保持一致性」）：
      若每版都按当版代码重新取名，重构一次 package 名就会让列表从
      「LinkBudget」跳成「PowerBudget」，用户在演进链里反而找不到同一个模型。
      故：**首版定名，后续复用**，锁定值写 `artifacts.meta["name_base"]`。

    返回 (title, filename, meta_extra)。
    """
    from core.artifact_naming import compose_title, derive_subject, filename_for

    goal = _conversation_goal_text(conn, conversation_id)
    # ★★ 2026-11-13（真机 conv 990727 实证）：锁定必须**按产物主体匹配**，
    #   不能"取会话第一条 code 产物"就复用。
    #   原实现 `ORDER BY id LIMIT 1` 取最早那条的 name_base ⇒ 后续**任何**新产物
    #   都继承第一个产物的名字：实测该会话的用例视图（`package '串联混动汽车用例'`）
    #   被命名为「串联混动汽车运行需求 · 需求视图 … · v0.2」，用户根本认不出这是用例视图。
    #   修法：先算出本轮的 subject，**只在 subject 相同的历史产物里**找锁定名；
    #   找不到 ⇒ 这是新产物，用自己的 subject 重新定名（各自独立成链）。
    _subject_now = derive_subject(code_text, goal)
    locked_base, locked_subject = "", ""
    try:
        for (m,) in conn.execute(
                "SELECT meta FROM artifacts WHERE conversation_id=? AND kind='code' "
                "ORDER BY id LIMIT 200", (conversation_id,)):
            try:
                j = json.loads(m or "{}")
            except Exception:
                j = {}
            if not (isinstance(j, dict) and j.get("name_base")):
                continue
            # 同主体才复用（大小写/空白差异不算不同产物）
            if str(j.get("name_key") or "").strip() == str(_subject_now or "").strip():
                locked_base = str(j.get("name_base") or "")
                locked_subject = str(j.get("name_key") or "")
                break
    except Exception:
        pass

    subject = locked_subject or _subject_now
    base = locked_base or compose_title(subject, goal, "")
    title = f"{base} · {version_label}" if version_label else base
    return title, filename_for(subject, version_label, lang), {
        "name_key": subject, "name_base": base}


def review_report_signal(card_data) -> dict | None:
    """编排 reflection 的评分/建议是否能沉淀为 report 产物（纯函数，零 IO）。

    ★ 2026-11-18（G08 根因闭合）：编排收口的 card_data.reflection 携带
      score / advice / issues / criteria / conflicts，是真实的评审交付物；
      但 _archive_artifacts 的 is_report 判据只看 intent / sections / report
      ⇒ 评审结果只落在 messages.card_data，从未成为 artifacts(kind=report)。
      实测 live G08 回放 conv 999999203：t2「打分评估+优化建议」done、
      reflection.score=90，却 report 产物 0 ⇒ artifact_kind_present(report) 不可达。

    返回 dict（应归档）或 None（不归档）。三个条件缺一不可：
      ① 本轮是编排（orchestrated 或 orchestrated_status 非空）——单 Agent 直行不误产；
      ② reflection 是 dict 且 enabled 不为 False；
      ③ score 非空（含 0）或 advice 非空——只认实质评审结果，不认空壳。
    """
    cd = card_data if isinstance(card_data, dict) else {}
    if not (cd.get("orchestrated") or cd.get("orchestrated_status")):
        return None
    refl = cd.get("reflection")
    if not isinstance(refl, dict) or refl.get("enabled") is False:
        return None
    score = refl.get("score")
    advice = str(refl.get("advice") or "").strip()
    if score in (None, "") and not advice:
        return None
    return {
        "score": score,
        "advice": advice[:2000],
        "rounds": refl.get("rounds"),
        "passed": refl.get("passed"),
        "issues": list(refl.get("issues") or [])[:20],
        "criteria": str(refl.get("criteria") or "")[:2000],
    }


def _archive_artifacts(conn, conversation_id: int, message_id: int,
                       card_data, content: str, intent: str, created_by: str = "未登录",
                       tool_docs=None) -> None:
    """把 AI 生成内容登记为会话产物（幂等：同 (会话,消息,类型,标题) 不重复）。

    - card_data.sysml_views → kind=sysml（预览走 Cytoscape 投影）
    - 报告意图（report_generation / card 含 sections）→ kind=report，并同时写 reports 表（报告中心自动归档）
    - 正文含 SysML/KerML 代码块 → kind=code（每条代码块登记一项）
    - tool_docs → 工具结果取回的完整 SysML 文档（米爸反馈4：导出文件不在正文里）
    仅 AI 生成内容；用户上传附件不进产物表（前端由消息附件合成文件清单项）。
    """
    try:
        from repositories.artifact_repo import ArtifactRepo
        from repositories.report_repo import ReportRepo
        repo = ArtifactRepo(conn)
        cd = card_data
        if isinstance(cd, str):
            try:
                cd = json.loads(cd) or {}
            except Exception:
                cd = {}
        cd = cd if isinstance(cd, dict) else {}
        content = content or ""

        # 1) SysML 视图产物 + 版本建档（sysml_versions 版本链）
        _vid = None  # P0-7：本消息 SysML 版本 id（代码文件产物关联版本）
        _code = ""   # P0-7：本消息提取的 SysML 代码文本
        sv = cd.get("sysml_views") or {}
        # ★ 2026-11-09（会话 990541）：版本建档原先被 `if sv.get("views")` 挡住 ——
        #   只要本轮没有结构化视图（模型直接吐代码，或视图解析失败），**就不建版本**。
        #   实测该会话 02:36 那轮真正通过校验（verdict=pass）的发文没有落库，
        #   于是库里 current 仍停在 02:33 那个 17 条语法错的版本上 —— **库里的"当前版本"
        #   与实际交付给用户的代码不一致**，后续任何"取上一版做基线"都会取到错的那份。
        #   修法：有结构化视图 **或** 有 sysml/kerml 标记的代码块，都算一次交付，都建档。
        #   收紧点：只认 lang ∈ {sysml,kerml,sysmlv2,kerml2} 的代码块，
        #   避免把 python/js 之类普通代码回答也写进 SysML 版本链（那会污染版本链）。
        _blocks = _extract_code_blocks(content)
        _code = "\n\n".join(b["code"] for b in _blocks
                            if b.get("lang", "").lower() in ("sysml", "kerml", "sysmlv2", "kerml2"))
        if not _code and _blocks and sv.get("views"):
            # 旧行为兜底：有结构化视图但代码块没打语言标记时取第一块
            _code = _blocks[0]["code"]
        if sv.get("views") or _code:
            for vname, vdata in (sv.get("views") or {}).items():
                title = f"SysML 视图：{vdata.get('view', {}).get('name', vname)}"
                if not repo.exists(conversation_id, message_id, "sysml", title):
                    repo.create_artifact(
                        conversation_id, message_id, "sysml", title, "sysml", "",
                        {"sysml_views": {"views": {vname: vdata}}}, source="conversation",
                        created_by=created_by)
            # AI 建模 SysML 版本建档（幂等；失败不阻断归档）
            # _blocks/_code 已在条件判定处统一算好（见上），此处不再重复提取
            _vid = _archive_sysml_version(conn, conversation_id, message_id, sv, created_by, _code)
            if _vid:
                for ar in conn.execute(
                        "SELECT id, meta FROM artifacts WHERE conversation_id=? AND message_id=? AND kind='sysml'",
                        (conversation_id, message_id)).fetchall():
                    m = {}
                    try:
                        m = json.loads(ar["meta"] or "{}")
                    except Exception:
                        m = {}
                    m["version"] = f"v{_vid}"
                    conn.execute("UPDATE artifacts SET meta=? WHERE id=?",
                                 (json.dumps(m, ensure_ascii=False), ar["id"]))

        # 2) 报告产物 + 报告中心自动归档
        is_report = intent == "report_generation" or bool(cd.get("sections")) or bool(cd.get("report"))
        # ★ 2026-11-18（G08 根因闭合）：编排评审的评分/建议也是交付物 ——
        #   原判据只看 intent/sections/report，漏掉了 reflection 里的实质评审结果，
        #   于是「打分评估+优化建议」跑完却零 report 产物。补一条受配置闸控制的入口；
        #   关掉闸 = 逐字节回到改动前（评审结果仍只留 messages.card_data）。
        _review = None
        if not is_report:
            try:
                from core import config as _cfg_rev
                if bool(_cfg_rev.get("orchestration", "archive_review_report", True)):
                    _review = review_report_signal(cd)
            except Exception:
                _review = None
            if _review:
                is_report = True
        if is_report:
            try:
                from services.report_generator import report_generator as _rg
                rtype = cd.get("report_type") or _rg.detect_report_type(str(content)[:80])
                # P1-9（2026-10-01）：写作侧不给 title 时**从内容取名**，而不是落固定兜底名。
                # 兜底名的根因不是"忘了写"，而是报告路径的 cd 里本来就没有 title 字段
                # （实测真库 report 占位名 3/6，另 3 条是模型结构化输出给了标题）。
                _t = str(cd.get("report_title") or cd.get("title") or "").strip()
                title = (_t or derive_title_from_content(content) or FALLBACK_REPORT_TITLE)[:120]
                report_meta = cd.get("meta") or _rg.build_meta(title, rtype)
                if _review:
                    # 把评分/建议随产物一并沉淀，避免只留标题不见结论
                    report_meta = dict(report_meta) if isinstance(report_meta, dict) else {}
                    report_meta["review"] = _review
                structured = cd.get("sections") or _rg.structure("", content, report_type=rtype)["sections"]
                summary = str(cd.get("summary") or structured[-1].get("body", "") if structured else "")[:500]
                if not repo.exists(conversation_id, message_id, "report", title):
                    _am = {"report_type": rtype, "sections": structured,
                           "summary": summary, "meta": report_meta}
                    if _review:
                        # ★ 顶层也放一份：让 `meta.review.score` 可被直接查询
                        #   （嵌套在 meta.meta.review 里对下游太深，易被漏读）。
                        _am["review"] = _review
                    repo.create_artifact(
                        conversation_id, message_id, "report", title, "markdown", content,
                        _am, source="conversation", created_by=created_by)
                    # 报告中心自动归档（幂等：同会话同标题不重复写 reports）
                    dup = conn.execute(
                        "SELECT COUNT(*) FROM reports WHERE conversation_id=? AND title=?",
                        (conversation_id, title)).fetchone()[0]
                    if not dup:
                        ReportRepo(conn).create_report(
                            title, rtype, summary, structured, "conversation",
                            conversation_id, "",
                            _conversation_project_id(conn, conversation_id), "draft", created_by)
            except Exception:
                pass

        # 3) 代码产物：SysML 版本 → 每个版本一个「代码文件 vN（SysML）」产物（产物列表并列展示演进路线）
        if _vid:
            _vlabel = f"v{_vid}"
            try:
                _vr = conn.execute("SELECT version_label FROM sysml_versions WHERE id=?", (_vid,)).fetchone()
                if _vr and _vr["version_label"]:
                    _vlabel = _vr["version_label"]
            except Exception:
                pass
            # ★ 2026-11-09（米爸）：原来固定叫「代码文件 vN（SysML）」——
            #   用户原话"都不知道具体这个代码是什么"。改为按 package/主定义名 + 会话诉求取名，
            #   且**会话内锁定**（后续版本只变版本后缀），见 _resolve_code_artifact_title。
            _vt, _vf, _vm = _resolve_code_artifact_title(
                conn, conversation_id, _code, _vlabel, "sysml")
            if _code and not repo.exists(conversation_id, message_id, "code", _vt):
                repo.create_artifact(
                    conversation_id, message_id, "code", _vt, "code", _code,
                    {"lang": "sysml", "version_id": _vid, "version_label": _vlabel, **_vm},
                    filename=_vf,
                    source="conversation", created_by=created_by)
        else:
            # 幂等修复（2026-09-01）：重复归档时 sysml 版本幂等返回 None → _vid 分支漂移。
            # 同消息已有任一 code 产物（如首次走 if _vid 分支的「代码文件」）则跳过代码块登记。
            # ★ 2026-10-17（米爸反馈2）：代码块产物**只收 SysML/KerML 族**——
            #   答复里顺手写的 Python/JS/SQL 片段是「正常对话输出」不是交付物，
            #   自动登记会把产物库变成聊天记录（与 document 误创建同根）。
            has_code = conn.execute(
                "SELECT 1 FROM artifacts WHERE conversation_id=? AND message_id=? AND kind='code' LIMIT 1",
                (conversation_id, message_id)).fetchone()
            if not has_code:
                for i, blk in enumerate(
                        [b for b in _extract_code_blocks(content)
                         if str(b.get("lang", "")).lower() in ("sysml", "kerml", "sysmlv2", "kerml2")], 1):
                    # 2026-11-09：不再用「代码块 N（lang）」这个零信息量名字。
                    # 同一消息里多个代码块主体名相撞时，才补序号区分（否则幂等判定会把第二块当重复）。
                    _bt, _bf, _bm = _resolve_code_artifact_title(
                        conn, conversation_id, blk["code"], "", blk["lang"])
                    title = _bt
                    if repo.exists(conversation_id, message_id, "code", title):
                        title = f"{_bt}（{i}）"
                    if not repo.exists(conversation_id, message_id, "code", title):
                        repo.create_artifact(
                            conversation_id, message_id, "code", title, "code", blk["code"],
                            {"lang": blk["lang"], **_bm},
                            filename=_bf,
                            source="conversation", created_by=created_by)

        # ★ 2026-10-17（米爸反馈2）：**删除「长 markdown → document 产物」自动登记**。
        #   原第 4 节：正文 ≥200 字且非 sysml/报告 ⇒ 自动建一份 document 产物——
        #   这正是「每次 AI 对话输出都自动创建一份产物文档」的根因（实测真库
        #   document 产物 23/23 全是此类误创建）。米爸口径：产物库只收**交付物**
        #   （SysML 代码文件 / 报告 / 导入附件），AI 的正常回答就是回答，不是产物。
        #   注：附件本就进产物库——前端 04-artifacts.js collectAttachFiles 从会话
        #   消息 attachments 合成「文件」清单项（url 去重），无需后端重复登记。

        # 5) ★ 2026-10-17（米爸反馈4）：工具结果取回的完整 SysML 文档 → code 产物。
        #    实测（会话 9446）：智源导出 24688 字节全文在工具结果的 JSON 信封里、
        #    AI 正文只有分析描述，正文代码块判据够不着 ⇒ 产物库空。
        #    拦截在工具结果返回的瞬间（stream/execute 两处同构），persist 时登记。
        for _i, _doc in enumerate(tool_docs or [], 1):
            _dt = str((_doc or {}).get("text") or "")
            if not _dt:
                continue
            if _dt[:400] in (content or ""):
                continue    # 正文已带同文（代码块路径已覆盖），避免双登记
            # 2026-11-09：工具侧若没给 title，不再落「SysML 源码（工具取回 #1）」这个占位名，
            # 改为按代码主体取名（与正文代码块同一套规则，保证同一模型在不同来源下同名）。
            _t5 = str((_doc or {}).get("title") or "").strip()
            _f5, _m5 = "", {}
            if not _t5:
                _t5, _f5, _m5 = _resolve_code_artifact_title(
                    conn, conversation_id, _dt, "", "sysml")
            if not repo.exists(conversation_id, message_id, "code", _t5):
                repo.create_artifact(
                    conversation_id, message_id, "code", _t5, "code", _dt,
                    {"lang": "sysml", "source": "tool_result", **_m5},
                    filename=_f5,
                    source="conversation", created_by=created_by)
    except Exception:
        # 归档失败不阻断主流程（对齐既有降级原则）
        pass
