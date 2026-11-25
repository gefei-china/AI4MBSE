"""P0 需求质量分析（对齐 visuresolutions AI-MBSE 的需求质量分析能力）。

功能：
1. 规则通道（确定性，零依赖）：扫描需求文本 → 模糊词/缺验收标准/不可验证项检测
2. LLM 通道（可选增强）：深入分析一致性/完整性，输出建议
3. 输出结构化问题清单：{problem, severity, evidence, suggestion}

设计：
- 规则通道保证 Mock/离线可回归（MBSE_LLM_FORCE_MOCK 下仍可用）
- LLM 通道在真实 LLM 可用时增强（补充语义级问题），失败静默降级
- 复用现有 report/卡片渲染（前端可展示）
"""
import re

# 模糊词表：不可验证、不可测量的主观形容词/副词（对齐 visuresolutions：快速/可靠/高效/友好）
FUZZY_WORDS = [
    "快速", "可靠", "高效", "友好", "良好", "稳定", "充分", "适当", "灵活",
    "安全", "便捷", "易用", "最佳", "最优", "正常", "合理", "及时", "准确",
    "强大", "完善", "显著", "有效", "优化", "舒适", "美观", "简单",
    "fast", "reliable", "efficient", "friendly", "robust", "stable",
]

# 验收标准缺失特征：需求句若含动作/性能词但无量化单位 → 提示缺验收
MEASURE_PATTERN = re.compile(
    r"(\d+(\.\d+)?\s*(ms|s|秒|分钟|min|h|小时|Gbps|Mbps|kbps|bps|GB|MB|KB|TB|%|℃|°C|度|米|m|kg|W|kW|V|A|次|个|路|通道))",
    re.IGNORECASE,
)

# 需求句终止特征（一句话算一条需求）
SENT_SPLIT = re.compile(r"(?<=[。；;！？\n])")


def analyze_requirements(text: str) -> dict:
    """规则通道：对需求文本做质量分析，返回结构化问题清单。

    返回 {issues: [{problem, severity, evidence, suggestion}], stats: {total, fuzzy, no_measure}}
    severity: high|medium|low
    """
    if not text or not text.strip():
        return {"issues": [], "stats": {"total": 0, "fuzzy": 0, "no_measure": 0}}

    sentences = [s.strip() for s in SENT_SPLIT.split(text) if s and len(s.strip()) >= 4]
    issues = []
    fuzzy_count = 0
    no_measure_count = 0

    for i, sent in enumerate(sentences[:50]):
        # 1) 模糊词检测
        hit_words = [w for w in FUZZY_WORDS if w in sent]
        if hit_words:
            fuzzy_count += 1
            issues.append({
                "problem": "存在不可验证的模糊词",
                "severity": "high" if len(hit_words) >= 2 else "medium",
                "evidence": f"第 {i + 1} 条：{sent[:80]}",
                "suggestion": f"将「{'、'.join(hit_words[:4])}」替换为可测量的指标，"
                              f"如具体数值、容差范围或验收判据（对齐 INCOSE 需求质量准则）",
            })
        # 2) 缺验收标准/量化：含动作词但无任何量化单位
        has_verb = any(v in sent for v in ("应", "须", "需要", "必须", "确保", "支持", "实现", "提供", "达到"))
        if has_verb and not MEASURE_PATTERN.search(sent):
            no_measure_count += 1
            issues.append({
                "problem": "缺少可验证的量化验收标准",
                "severity": "medium",
                "evidence": f"第 {i + 1} 条：{sent[:80]}",
                "suggestion": "补充量化指标与验收判据，如「响应时间 ≤ 500ms」「可用率 ≥ 99.9%」"
                              "（使需求可测试、可验证）",
            })

    # 去重（同句可能同时命中两类，保留最高 severity 一条即可，但统计分开）
    seen = set()
    dedup = []
    for it in issues:
        k = it["evidence"][:40]
        if k not in seen:
            seen.add(k)
            dedup.append(it)
    issues = dedup

    return {
        "issues": issues[:30],
        "stats": {"total": len(sentences), "fuzzy": fuzzy_count, "no_measure": no_measure_count},
    }


def analyze_with_llm(text: str, rule_result: dict = None) -> dict:
    """LLM 通道（可选增强）：深入分析——需求一致性/完整性/歧义。失败静默降级为规则结果。"""
    try:
        from llm import llm_client
        rule = rule_result or analyze_requirements(text)
        resp = llm_client.chat([
            {"role": "system", "content": (
                "你是需求工程专家（对齐 INCOSE 需求质量准则）。分析用户提供的需求文本，输出结构化 JSON："
                '{"issues":[{"problem":"问题描述","severity":"high|medium|low",'
                '"evidence":"原文引用","suggestion":"可执行修改建议"}],"summary":"总体质量评价(30字内)"}。'
                "重点检查：模糊不可验证、自相矛盾、需求粒度不均、缺少验收判据、可追溯性缺失。"
                "只输出 JSON 对象，不要其他文字。")},
            {"role": "user", "content": str(text)[:3000]}],
            _intent="requirement_quality")
        content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        from agent import AgentPipeline
        parsed = AgentPipeline._parse_json_block(content)
        if isinstance(parsed, dict) and parsed.get("issues"):
            # 合并规则 + LLM 问题（LLM 结果优先去重，按 severity 排序）
            merged = list(rule.get("issues", [])) + list(parsed.get("issues", []))
            seen = set()
            out = []
            for it in merged:
                k = (it.get("problem", "") + it.get("evidence", ""))[:50]
                if k not in seen:
                    seen.add(k)
                    out.append(it)
            sev_rank = {"high": 0, "medium": 1, "low": 2}
            out.sort(key=lambda x: sev_rank.get(x.get("severity", "low"), 3))
            return {
                "issues": out[:30],
                "stats": rule.get("stats", {}),
                "summary": parsed.get("summary", ""),
                "llm_enhanced": True,
            }
        return rule
    except Exception:
        return rule_result or analyze_requirements(text)


def build_quality_report(text: str, use_llm: bool = True) -> dict:
    """统一入口：规则 + LLM 双通道，返回前端可渲染的报告结构。"""
    rule = analyze_requirements(text)
    result = analyze_with_llm(text, rule) if use_llm else rule
    stats = result.get("stats", {})
    result["score"] = max(0, 100 - len(result.get("issues", [])) * 12 - stats.get("fuzzy", 0) * 5)
    return result
