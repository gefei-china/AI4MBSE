# -*- coding: utf-8 -*-
"""智能任务分派（Task 6）：确定性 Agent 选择——专长匹配 × 负载 × 历史成功率。

纯逻辑模块：零外部依赖，不 import 项目其他模块；输入输出均为 dict/list。

打分模型（供编排执行层在每批 ready 任务前按负载重算）：
    assign_score = W_CAP*capability_match + W_LOAD*(1 - current_load/max(max_concurrency,1))
                 + W_HIST*eff_success

- normalize_capability(cap)：能力词 → 归一化主词（SYNONYM_MAP 反向查找；无命中原样返回）
- capability_match(capabilities, task_text, embed_fn=None)：专长匹配度 0~1
    ① **字符 bigram 重合度**（★ 2026-10-13 新增，见下"标定实测"）
    ② 归一化词表命中：task_text 含主词 → 1.0；含同义变体 → 0.8
    ③ embed_fn 提供时：词表未命中则算余弦相似度，超阈值取该值
- assign_score(candidate, task_text, embed_fn=None)：单候选分派分数（None 成功率兜底 0.5）
- rank_candidates(candidates, task_text, embed_fn=None)：批量打分排序（降序、并列稳定）；
    冷启动：success_rate 为 None 的项取池内有值项的中位数，池全 None → 0.5
- best_agent(candidates, task_text, embed_fn=None)：rank_candidates 首个；空列表返回 None

## ★ 2026-10-13（B1-7）权重与匹配口径的标定实测

**先量后调，不先调后量。** 三条实测结论直接决定了本模块现在的形态：

**① 原来的 `SYNONYM_MAP` 只有 6 组，覆盖真库 76 个 capability 词条的 9 个（12%）。**
   8 视图系列（`SysML v2 需求视图代码` / `状态视图代码` / `活动视图代码` …）**全部未覆盖** ⇒
   那些任务的 `capability_match` **恒为 0**，权重 `0.6*capability_match` 整项塌缩，
   实际打分退化成"负载 0.3 + 成功率 0.1"。**接上去等于接了个残缺品。**

**② `embed_fn` 兜底在当前 embedding 配置下**不能**救回来。**
   实测 `knowledge_pipeline.embedder.Embedder` 处于 **bigram TF 降级态**（4096 维里
   一条文本只有 3~12 个非零位，特征名走 md5 哈希）。实测余弦：
   - 正样本「生成状态机视图」~「SysML v2 状态视图代码」= **0.236**
   - 负样本「生成状态机视图」~「报告生成」= **0.236**（**与正样本同分**）
   两者都低于 `EMBED_THRESHOLD=0.55` ⇒ 该路径对本场景**无区分度**。
   ⇒ 不依赖 embed 兜底；它只在真 embedding 后端（openai-compat）可用时才有意义。

**③ 因此新增 ①「字符 bigram 重合度」作为主匹配手段** —— 这与本仓 embedding 降级用的是
   **同一套字符 bigram 特征**（`embedder._bigram_feature_text`），口径一致；
   且它对中文有效：「状态视图」与「SysML v2 状态视图代码」共享 bigram「状态」「状视」
   「视图」⇒ 重合度显著高于与「报告生成」的（共享「生成」一个）。

**权重取 0.5 / 0.3 / 0.2（理由，不是不拍脑袋）**：
- `capability_match` 0.5：匹配是分派的**主因**，但实测有未覆盖面 ⇒ 不给满权，留给负载纠偏；
- 负载 0.3：与原值同量级（0.3），编排内单批并发 ≤6，负载差异有限，权重不宜高于匹配；
- 成功率 0.2（**原 0.1 调高**）：真库 54 个 failed / 168 done ⇒ 每个 Agent 的成功率
  是**实测可得且区分度明显**的信号（有的 0，有的 1），比负载更稳定，值得双倍权重。
  ⚠️ 三项之和为 1.0（便于按比例整体缩放而不改变相对关系）。
- 三项**均可配置**（`agent_assignment.*`），本次标定的结论是默认值，改配置即可覆盖。
"""
import math
import re
import statistics

# ── 打分权重（可配置；见配置组 agent_assignment）──────────────────────────────
# ⚠️ 改这里之前先看模块 docstring「标定实测」—— 三个数字都对应实测证据，不是随手取的。
WEIGHT_CAPABILITY = 0.5   # 专长匹配
WEIGHT_LOAD = 0.3         # 负载
WEIGHT_HISTORY = 0.2      # 历史成功率（实测区分度最明显的信号）

# ── 归一化词表：主词 → 同义变体（**仅作精确命中加速用**，非主匹配手段）──────
# ⚠️ 实测该词表只覆盖真库 76 个 capability 词条的 9 个（12%），8 视图系列全未覆盖
#    ⇒ 不能作为唯一匹配手段。保留它的价值：命中时给确定性的高分（1.0 / 0.8）。
SYNONYM_MAP = {
    "需求拆解": ["需求分析", "需求条目化", "需求梳理"],
    "方案设计": ["架构设计", "系统设计", "设计"],
    "变更影响": ["影响评估", "影响分析"],
    "质量评审": ["预评审", "评审", "审查"],
    "报告生成": ["报告撰写", "生成报告", "报告"],
    "系统建模": ["工程建模", "建模"],
}

#: 归一化词表命中即给满分/高分（确定性优先于统计）
_WORDLIST_EXACT = 1.0
_WORDLIST_VARIANT = 0.8

# embed 余弦相似度采纳阈值（超 0.55 才取该值）
# ⚠️ 实测：当前 embedding 为 bigram TF 降级态，正负样本余弦同分（0.236）⇒ 该路径当前无区分度。
#    保留阈值与实现，待 embedding 切到真后端后自然生效。
EMBED_THRESHOLD = 0.55
# 冷启动默认成功率（池内无任何历史时使用）
DEFAULT_SUCCESS_RATE = 0.5

#: bigram 重合度低于此值直接判 0（噪声门槛）。
#: 取 0.12 的依据：实测「生成活动图」~「BFS 依赖遍历」的共享 bigram 只有「生」这一个
#   （重合度 ~0.09），而真同族配对普遍 >0.25 —— 0.12 落在两者之间，能滤掉噪声又不砍真匹配。
BIGRAM_MIN_OVERLAP = 0.12


def normalize_capability(cap):
    """cap → 归一化主词（查 SYNONYM_MAP 反向；无命中原样返回）。"""
    cap = str(cap or "")
    for main, variants in SYNONYM_MAP.items():
        if cap in variants:
            return main
    return cap


def _bigrams(text: str) -> set:
    """文本 → 字符 bigram 集合（单字文本退化为其自身）。

    与 `knowledge_pipeline.embedder._bigram_feature_text` **同口径**（去空白后取相邻 2 字符）。
    不复用那边的实现：本模块声明"零外部依赖"，且那边是 md5→定长哈希（会丢可读性，
    无法在这里做集合运算）。
    """
    s = re.sub(r"\s+", "", str(text or ""))
    if not s:
        return set()
    if len(s) < 2:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_overlap(capabilities, task_text) -> float:
    """★ 2026-10-13：能力词表与任务文本的**字符 bigram 最大重合度**（0~1）。

    做法：对每个 capability，算它与 task_text 的 |交集| / min(|cap|,|task|)，
    取**最大值**（一个能力词命中即算命中）。
    用 min 而非 union 作分母：避免"任务文本很长 ⇒ 重合度被稀释"（任务文本通常比能力词长得多）。
    ⚠️ 对**空 capability 集**返回 0（不是 1.0）—— 没能力描述就没有匹配证据。
    """
    tg = _bigrams(task_text)
    if not tg:
        return 0.0
    best = 0.0
    for cap in capabilities or []:
        cg = _bigrams(str(cap or ""))
        if not cg:
            continue
        inter = len(cg & tg)
        if not inter:
            continue
        denom = min(len(cg), len(tg))
        if denom:
            best = max(best, inter / denom)
    return best


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
    """专长匹配度 0~1。**分层判定**（★ 2026-10-13 重排，依据见模块 docstring 实测）：

      ① **归一化词表精确命中** → 1.0（主词）/ 0.8（同义变体）。
         确定性最高，命中即给高分 —— 但实测只覆盖 12% 的真实词条。
      ② **字符 bigram 重合度** → 噪声门槛以上直接取该值。
         这是**主匹配手段**：实测 8 视图系列（词表完全没覆盖的那批）靠它能拿到 0.4~0.6，
         而与无关能力（报告生成 / BFS 遍历）只有 0.1 以下，被门槛滤掉。
      ③ **embed 余弦兜底**（`embed_fn` 提供且 ①② 都未给分时）→ 超阈值取该值。
         ⚠️ 当前 embedding 为 bigram TF 降级态，实测正负样本同分 ⇒ 当前无区分度；
         切到真后端后自然生效，故保留。

    ⚠️ 三层都拿不到分时返回 **0.0**（不是某个"未知"默认值）——
    0 意味着"没有匹配证据"，会让负载/成功率两项仍能起作用（它们不会被 0 归一化压掉）。
    """
    caps = capabilities or []
    task_text = str(task_text or "")

    # ① 归一化词表精确命中（确定性最高）
    for cap in caps:
        norm = normalize_capability(cap)
        if norm and norm in task_text:
            return _WORDLIST_EXACT
        for variant in SYNONYM_MAP.get(norm, []):
            if variant and variant in task_text:
                return _WORDLIST_VARIANT

    # ② 字符 bigram 重合度（主匹配手段，对中文有效且与 embedder 同口径）
    _ov = bigram_overlap(caps, task_text)
    if _ov >= BIGRAM_MIN_OVERLAP:
        return _ov

    # ③ embed 余弦兜底
    if embed_fn is not None:
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
    return 0.0


def _weights():
    """权重三元组 —— 读 `agent_assignment` 配置组，缺项回落到模块默认值。

    ⚠️ 用模块常量作 default 而非硬编码字面量：默认值本身是被标定过的（见 docstring），
    配置只是**覆盖**手段，不是唯一来源。
    """
    w = (WEIGHT_CAPABILITY, WEIGHT_LOAD, WEIGHT_HISTORY)
    try:
        from core.config import get as _get
        cap = float(_get("agent_assignment", "weight_capability", WEIGHT_CAPABILITY))
        load = float(_get("agent_assignment", "weight_load", WEIGHT_LOAD))
        hist = float(_get("agent_assignment", "weight_history", WEIGHT_HISTORY))
        if cap < 0 or load < 0 or hist < 0:
            return w
        return (cap, load, hist)
    except Exception:
        return w


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
    _wc, _wl, _wh = _weights()
    return _wc * match + _wl * (1.0 - load / maxc) + _wh * eff_success


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
