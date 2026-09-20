# -*- coding: utf-8 -*-
"""智能任务分派（Task 6）：确定性 Agent 选择——专长匹配 × 负载 × 历史成功率。

纯逻辑模块：零外部依赖，不 import 项目其他模块；输入输出均为 dict/list。

打分模型（供编排执行层在每批 ready 任务前按负载重算）：
    assign_score = 0.6*capability_match + 0.3*(1 - current_load/max(max_concurrency,1)) + 0.1*eff_success

- normalize_capability(cap)：能力词 → 归一化主词（SYNONYM_MAP 反向查找；无命中原样返回）
- capability_match(capabilities, task_text, embed_fn=None)：专长匹配度 0~1
    ① 归一化词表命中：task_text 含任一归一化主词 → 1.0；含同义变体 → 0.8
    ② embed_fn 提供时：词表未命中则对 capabilities 与 task_text 算余弦相似度，
       超阈值 EMBED_THRESHOLD(0.55) 取该值（embed_fn(text)->list[float]，向量不等长返回 0）
    ③ 否则 0
- assign_score(candidate, task_text, embed_fn=None)：单候选分派分数（None 成功率兜底 0.5）
- rank_candidates(candidates, task_text, embed_fn=None)：批量打分排序（降序、并列稳定）；
    冷启动：success_rate 为 None 的项取池内有值项的中位数，池全 None → 0.5
- best_agent(candidates, task_text, embed_fn=None)：rank_candidates 首个；空列表返回 None
"""
import math
import statistics

# ── 归一化词表：主词 → 同义变体（覆盖需求/设计/影响/评审/报告/建模 6 组）──
SYNONYM_MAP = {
    "需求拆解": ["需求分析", "需求条目化", "需求梳理"],
    "方案设计": ["架构设计", "系统设计", "设计"],
    "变更影响": ["影响评估", "影响分析"],
    "质量评审": ["预评审", "评审", "审查"],
    "报告生成": ["报告撰写", "生成报告", "报告"],
    "系统建模": ["工程建模", "建模"],
}

# embed 余弦相似度采纳阈值（超 0.55 才取该值）
EMBED_THRESHOLD = 0.55
# 冷启动默认成功率（池内无任何历史时使用）
DEFAULT_SUCCESS_RATE = 0.5


def normalize_capability(cap):
    """cap → 归一化主词（查 SYNONYM_MAP 反向；无命中原样返回）。"""
    cap = str(cap or "")
    for main, variants in SYNONYM_MAP.items():
        if cap in variants:
            return main
    return cap


def _cosine(a, b):
    """简单余弦相似度；向量缺失/不等长/零模 → 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def capability_match(capabilities, task_text, embed_fn=None):
    """专长匹配度 0~1（①②③ 顺序，见模块 docstring）。"""
    caps = capabilities or []
    task_text = str(task_text or "")
    best = 0.0
    for cap in caps:
        norm = normalize_capability(cap)
        if norm and norm in task_text:              # ① 主词命中 → 1.0
            best = max(best, 1.0)
            continue
        for variant in SYNONYM_MAP.get(norm, []):   # ① 同义变体命中 → 0.8
            if variant and variant in task_text:
                best = max(best, 0.8)
                break
    if best > 0:
        return best
    if embed_fn is not None:                        # ② embed 余弦兜底
        try:
            tv = embed_fn(task_text)
        except Exception:
            tv = None
        if tv:
            best_emb = 0.0
            for cap in caps:
                try:
                    cv = embed_fn(str(cap))
                except Exception:
                    cv = None
                if cv:
                    best_emb = max(best_emb, _cosine(tv, cv))
            if best_emb > EMBED_THRESHOLD:
                return best_emb
    return 0.0                                      # ③ 否则 0


def _eff_success(success_rate, fallback):
    """成功率取值：数值/数字字符串直接取；None/非法 → fallback。"""
    if success_rate is None:
        return fallback
    try:
        return float(success_rate)
    except (TypeError, ValueError):
        return fallback


def _score(candidate, task_text, embed_fn, success_fallback, match=None):
    """候选分派分数实现（assign_score / rank_candidates 共用）。"""
    if match is None:
        match = capability_match(candidate.get("capabilities") or [], task_text, embed_fn)
    try:
        load = float(candidate.get("current_load") or 0)
    except (TypeError, ValueError):
        load = 0.0
    try:
        maxc = float(candidate.get("max_concurrency"))
    except (TypeError, ValueError):
        maxc = 0.0
    if maxc <= 0:
        maxc = 1.0
    eff_success = _eff_success(candidate.get("success_rate"), success_fallback)
    return 0.6 * match + 0.3 * (1.0 - load / maxc) + 0.1 * eff_success


def assign_score(candidate, task_text, embed_fn=None):
    """候选分派分数（None 成功率以 DEFAULT_SUCCESS_RATE=0.5 兜底）。

    批量场景请用 rank_candidates：其 None 项会按池内有值项的中位数填充。
    """
    return _score(candidate, task_text, embed_fn, DEFAULT_SUCCESS_RATE)


def _pool_median(candidates):
    """池内有值 success_rate 的中位数；全 None/非法 → DEFAULT_SUCCESS_RATE。"""
    vals = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        sr = c.get("success_rate")
        if sr is None:
            continue
        try:
            vals.append(float(sr))
        except (TypeError, ValueError):
            continue
    return statistics.median(vals) if vals else DEFAULT_SUCCESS_RATE


def rank_candidates(candidates, task_text, embed_fn=None):
    """批量打分并按 assign_score 降序排序（并列稳定：同分按原列表顺序）。

    冷启动：success_rate 为 None 的项取池内有值项的中位数；池全 None → 0.5，
    缺历史的候选不因缺数据被排除。
    返回 [{**candidate, "assign_score": s, "capability_match": m}]。
    """
    cands = list(candidates) if candidates else []
    median = _pool_median(cands)
    scored = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        m = capability_match(c.get("capabilities") or [], task_text, embed_fn)
        s = _score(c, task_text, embed_fn, median, match=m)
        scored.append({**c, "assign_score": s, "capability_match": m})
    scored.sort(key=lambda x: x["assign_score"], reverse=True)
    return scored


def best_agent(candidates, task_text, embed_fn=None):
    """rank_candidates 首个（最高分候选）；空列表/None 返回 None。"""
    ranked = rank_candidates(candidates, task_text, embed_fn)
    return ranked[0] if ranked else None
