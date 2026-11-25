"""纯判据单一真源：evaluate_answers（P1 收敛，2026-11-25）。

从 `tools/eval/eval_generation_quality.py` 下沉：原调用方 `core/` 反向依赖上层（tools / services）属方向错误。
本模块零业务依赖；原路径保留 re-export，旧 import 拿到同一对象。
"""

import re

_ENT_ID = re.compile(r"\b[A-Z]{1,5}-\d+[A-Za-z]?\b")
_ENT_NUM = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:dBi|Gbps|Mbps|GHz|MHz|kHz|万路|万用户|ms|°C|%|dB)\b")
_CITE_MARK = re.compile(r"\[(\d+)\]")
def _entities(text):
    """抽取可核查实体（ID + 带单位数值）。宁缺毋滥：假阳性会淹没真信号。"""
    out = set()
    for rx in (_ENT_ID, _ENT_NUM):
        out |= set(m.group(0).strip() for m in rx.finditer(text or ""))
    return out
def _citation_markers(body):
    """正文里的 [n] 引用标记（数字列表）。"""
    return [int(m.group(1)) for m in _CITE_MARK.finditer(body or "")]
def _citation_context(citations):
    """引用块正文并集（接地质检的"证据海"）。"""
    return " ".join((c.get("content") or "") for c in citations or [])
def evaluate_answers(answers):
    """answers: [{id, body, citations:[{content,...}]}] → 指标 dict。

    citations 为空列表 = 无引用答案（进 no_citation_factual 分母）。
    返回四项指标 + 计数 + 池化负对照。任何分母为 0 的指标返 None
    （不是 0——"没测到"与"测到 0 分"必须可区分，否则看板会把前者渲染成
    后者，那是假绿）。
    """
    n_ans = len(answers)
    with_cit = [a for a in answers if a.get("citations")]
    without_cit = [a for a in answers if not a.get("citations")]

    # ── citation_valid_rate：正文 [n] 必须全部落在引用范围内 ──
    total_marks = valid_marks = 0
    answers_using = 0
    cits_without_content = 0     # schema 探针：见下
    for a in with_cit:
        marks = _citation_markers(a.get("body"))
        n_cit = len(a.get("citations") or [])
        if marks:
            answers_using += 1
            total_marks += len(marks)
            valid_marks += sum(1 for n in marks if 1 <= n <= n_cit)
        # schema 探针：引用块没有 content 正文 ⇒ 接地质检对这套引用**静默
        # 失效**（实体海是空的， grounded 率会假性冲到 1.0 —— 典型的
        # "看起来全绿"路径）。计数进报告，门禁要求为 0。
        cits_without_content += sum(
            1 for c in (a.get("citations") or []) if not (c.get("content") or ""))
    citation_valid_rate = (valid_marks / total_marks) if total_marks else None
    citation_usage_rate = (answers_using / len(with_cit)) if with_cit else None

    # ── citation_contiguity_rate：编号是否连续（1..K 无断号）─────────────
    #
    # ★ 2026-11-16（真实 bug 暴露的指标盲区）：`citation_valid_rate` 只判
    #   `1 <= n <= N`（在范围内），因此 **断号完全看不见**。实测 msg 6674：
    #   body 用 [1][2][3][5]、citations 5 条 ⇒ 全部在范围内 ⇒ valid_rate=1.0
    #   全绿，而用户看到的是「显示 5 条、页面只有 1,2,3,5」——正是这条盲区
    #   让它上线以来一直"没问题"。
    #
    #   语义：正文实际使用的编号去重后是否恰好构成 1..K（K=去重后条数）。
    #   断号意味着两件事，都值得看趋势：
    #     · 读者视角：编号不连续，找不到被跳过的那条（本次修的显示问题根因）；
    #     · 检索视角：被跳过的编号 = **检索到但没用上**的片段（精度信号）。
    #   ⚠️ 刻意**不设门禁阈值**（与 grounded_entity_rate 同一处置）：模型选择性
    #      引用是正常行为，断号率不是"缺陷率"。设阈值只会要么恒绿（假绿）
    #      要么天天红（狼来了）—— 先算出来、进 eval_metrics 看趋势，
    #      等分布稳定后再决定是否升级为门禁。
    #   ⚠️ K=1 时恒真（单条引用不可能断号）⇒ 该样本对指标无信息量，但计入
    #      分母不影响正确性；真正的信息量来自 K>=2 的样本（见 fixture F1c）。
    contig_n = contig_ok = 0
    for a in with_cit:
        marks = _citation_markers(a.get("body"))
        if not marks:
            continue
        used = sorted(set(marks))
        contig_n += 1
        if used == list(range(1, len(used) + 1)):
            contig_ok += 1
    citation_contiguity_rate = (contig_ok / contig_n) if contig_n else None

    # ── grounded_entity_rate：答案实体在"自己的"引用并集里的比例 ──
    # ── no_citation_factual_rate：无引用答案中含可核查实体的比例 ──
    total_ent = grounded_ent = 0
    per_answer = []
    for a in with_cit:
        ctx = _citation_context(a.get("citations"))
        ents = _entities(a.get("body"))
        g = sum(1 for e in ents if e in ctx)
        total_ent += len(ents)
        grounded_ent += g
        if ents or _citation_markers(a.get("body")):
            _marks = _citation_markers(a.get("body"))
            _used = sorted(set(_marks))
            per_answer.append({
                "id": a.get("id"), "kind": "with_citations",
                "n_entities": len(ents), "n_grounded": g,
                "ungrounded": sorted(e for e in ents if e not in ctx),
                "n_markers": len(_marks),
                # ★ 2026-11-16：断号可直接从这两列算出来（used ⊂ 1..retrieved）
                "n_used": len(_used), "n_retrieved": len(a.get("citations") or []),
                "numbers_used": _used,
                "skipped_numbers": [i for i in range(1, len(_used) + 1) if i not in _used],
            })
    grounded_entity_rate = (grounded_ent / total_ent) if total_ent else None

    factual_nocit = 0
    for a in without_cit:
        ents = _entities(a.get("body"))
        if ents:
            factual_nocit += 1
            per_answer.append({
                "id": a.get("id"), "kind": "no_citations",
                "n_entities": len(ents), "n_grounded": 0,
                "ungrounded": sorted(ents), "n_markers": 0,
            })
    no_citation_factual_rate = (
        factual_nocit / len(without_cit)) if without_cit else None

    # ── 池化负对照：每条答案对"其它全部答案"的引用并集算接地率 ──
    # 判别力检验：真实接地率必须**显著高于**池化接地率，否则该指标在这批
    # 样本上没在测"答案↔自己证据"的对齐关系（可能是实体太通用，或证据
    # 主语根本不在 citations 里）。拉不开 ⇒ 门禁判 SKIP 而非通过。
    # ⚠ 池子必须**排除本条自己的引用** —— 第一版把全部答案（含自己）的
    #   引用并集当池子，自己的证据当然在自己的池子里 ⇒ 对照恒等于真值、
    #   discriminates 恒 False。是黄金样本 F4F5 抓出来的（合成数据上
    #   真值 1.0 / 含己池 1.0 / 排己池 0.0，一眼可见）。
    pooled_total = pooled_grounded = 0
    for a in with_cit:
        own_ids = {id(c) for c in (a.get("citations") or [])}
        others_ctx = _citation_context(
            [c for b in with_cit if b is not a
             for c in (b.get("citations") or []) if id(c) not in own_ids])
        ents = _entities(a.get("body"))
        pooled_total += len(ents)
        pooled_grounded += sum(1 for e in ents if e in others_ctx)
    control_rate = (pooled_grounded / pooled_total) if pooled_total else None
    true_rate = grounded_entity_rate
    discriminates = (
        true_rate is not None and control_rate is not None
        and true_rate >= control_rate + 0.15)

    return {
        "n_answers": n_ans,
        "n_with_citations": len(with_cit),
        "n_without_citations": len(without_cit),
        "citation_valid_rate": citation_valid_rate,
        "citation_usage_rate": citation_usage_rate,
        "citation_contiguity_rate": citation_contiguity_rate,
        "citation_contiguity_n": contig_n,
        "grounded_entity_rate": grounded_entity_rate,
        "no_citation_factual_rate": no_citation_factual_rate,
        "total_markers": total_marks,
        "valid_markers": valid_marks,
        "answers_using_citations": answers_using,
        "citations_without_content": cits_without_content,
        "n_entities": total_ent,
        "n_grounded_entities": grounded_ent,
        "control": {"pooled_grounded_rate": control_rate,
                    "discriminates": discriminates},
        "per_answer": per_answer,
    }
