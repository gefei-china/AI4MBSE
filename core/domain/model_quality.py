"""P1b-1 模型质量三件套：对生成的 SysML v2 模型做自动校验（对齐 Visual Paradigm「活模型」）。

三件套：
1. 约束校验：基础语法/元素完整性（复用 ontology_semantics 本体校验 + 轻量正则）
2. 追溯完整性：$verify/$trace/$refine 关系检查（需求是否被验证/追溯/精化）
3. 一致性检查：重定义/端口/部件引用目标存在性

输出：结构化校验报告 {checks: [{name, pass, detail}], score, issues}
设计：纯规则实现（零外部依赖，Mock 可回归）；LLM 增强留扩展位。
"""
import re


def check_model_quality(sysml_code: str) -> dict:
    """对 SysML v2 文本模型做三件套校验。返回 {checks, score, issues, has_model}。

    checks: [{name, pass, detail}]（三件套逐项）
    score: 0-100 质量分
    issues: 具体问题清单 [{severity, message}]
    """
    if not sysml_code or not sysml_code.strip():
        return {"checks": [], "score": 0, "issues": [{"severity": "high", "message": "无模型代码可校验"}],
                "has_model": False}

    issues = []
    checks = []

    # ── ① 约束校验：基础语法 + 元素完整性 ──
    syntax_issues = []
    # 包定义闭合检查（花括号配对粗检）
    for m in re.finditer(r"package\s+([A-Za-z0-9_'\-]+)\s*\{", sysml_code):
        _open = sysml_code.count("{", m.start(), m.end())
        _seg = sysml_code[m.end():m.end() + 2000]
        _close = _seg.count("}") if _seg else 0
        if _close == 0 and _seg:
            syntax_issues.append(f"包 '{m.group(1)}' 可能缺少闭合括号（片段未在 2000 字符内闭合）")
    # requirement 元素应有 id 属性（可追溯性基础）；排除 'def'/'usage' 关键字
    reqs = re.findall(r"requirement\s+((?!def\b|usage\b)[A-Za-z0-9_'\-]+)", sysml_code)
    idless = []
    for r in reqs:
        # 形式1：def 块内 id 赋值：requirement def X { ... id = '...' ... }
        m_def = re.search(rf"requirement\s+def\s+{re.escape(r)}\s*\{{", sysml_code)
        if m_def:
            _seg = sysml_code[m_def.end():m_def.end() + 800]
            _close = _seg.find("}")
            if _close != -1 and re.search(r"\bid\b\s*=", _seg[:_close]):
                continue
            # 块未闭合（跨长文）→ 取片段判断
            if _close == -1 and re.search(r"\bid\b\s*=", _seg):
                continue
        # 形式2：usage 带属性 [id='...'] 或引用 .id =
        if re.search(rf"requirement\s+{re.escape(r)}\s*\[[^\]]*id", sysml_code):
            continue
        if re.search(rf"{re.escape(r)}\s*\.\s*id\s*=", sysml_code):
            continue
        idless.append(r)
    if idless:
        syntax_issues.append(f"{len(idless)} 个 requirement 缺少显式 id（可追溯性基础）：{', '.join(idless[:5])}")
    # part def / attribute 基础存在性
    has_part = bool(re.search(r"part\s+def\s+", sysml_code))
    has_req = bool(reqs)
    if not has_part and not has_req:
        syntax_issues.append("模型既无 part def 也无 requirement 元素，内容可能为空")
    if syntax_issues:
        issues += [{"severity": "high" if "闭合" in s or "为空" in s else "medium", "message": s}
                   for s in syntax_issues]
    checks.append({
        "name": "约束校验（语法/元素完整性）",
        "pass": not syntax_issues,
        "detail": f"发现问题 {len(syntax_issues)} 项" if syntax_issues else "语法完整，元素定义齐全",
    })

    # ── ② 追溯完整性：$verify/$trace/$refine ──
    trace_issues = []
    # SysML v2 追溯关系两种形式：$verify t1（无括号）或 $verify(test1, req)
    n_verify = len(re.findall(r"\$verify\s*\(?\s*[A-Za-z0-9_'\"]", sysml_code))
    n_trace = len(re.findall(r"\$trace\s*\(?\s*[A-Za-z0-9_'\"]", sysml_code))
    n_refine = len(re.findall(r"\$refine\s*\(?\s*[A-Za-z0-9_'\"]", sysml_code))
    if reqs and n_verify == 0:
        trace_issues.append(f"有 {len(reqs)} 个需求但无 $verify 验证关系（需求应链接到验证用例）")
    if reqs and n_trace == 0 and n_refine == 0:
        trace_issues.append("需求无 $trace/$refine 追溯或精化关系（建议建立 需求→子需求/用例 追溯）")
    if trace_issues:
        issues += [{"severity": "medium", "message": s} for s in trace_issues]
    checks.append({
        "name": "追溯完整性（$verify/$trace/$refine）",
        "pass": not trace_issues,
        "detail": f"verify×{n_verify} / trace×{n_trace} / refine×{n_refine}"
                  + (f"，缺 {len(trace_issues)} 项" if trace_issues else "，追溯链完整"),
    })

    # ── ③ 一致性检查：引用目标存在性 ──
    cons_issues = []
    # 收集所有已定义元素（part def / requirement / attribute / port）
    defined = set()
    defined |= set(re.findall(r"(?:part|attribute|port)\s+def\s+([A-Za-z0-9_'\-]+)", sysml_code))
    defined |= set(reqs)
    # 检查类型引用（: TypeName 形式）是否指向已定义或系统库类型（忽略单字母/常见内建）
    type_refs = re.findall(r":\s*([A-Za-z_][A-Za-z0-9_']{2,})", sysml_code)
    builtin = {"String", "Integer", "Real", "Boolean", "Complex", "MassValue", "LengthValue",
               "TemperatureValue", "FrequencyValue", "PowerValue"}
    dangling = [t for t in type_refs if t not in defined and t not in builtin]
    if dangling:
        # 去重 + 限 5 个
        seen = []
        for d in dangling:
            if d not in seen:
                seen.append(d)
        if len(seen) > 5:
            cons_issues.append(f"{len(seen)} 个类型引用未在本模型定义且非内建类型：{', '.join(seen[:5])}…")
        else:
            cons_issues.append(f"类型引用未定义：{', '.join(seen[:5])}")
    if cons_issues:
        issues += [{"severity": "medium", "message": s} for s in cons_issues]
    checks.append({
        "name": "一致性检查（引用目标存在性）",
        "pass": not cons_issues,
        "detail": f"发现 {len(cons_issues)} 项悬空引用" if cons_issues else f"全部 {len(type_refs)} 个类型引用均有定义",
    })

    # 综合评分：100 - 每 high 30 分 - 每 medium 15 分（下限 0）
    score = max(0, 100 - sum(30 if it["severity"] == "high" else 15 for it in issues))
    return {"checks": checks, "score": score, "issues": issues[:20], "has_model": True,
            "stats": {"requirements": len(reqs), "verify": n_verify, "trace": n_trace, "refine": n_refine}}


def attach_quality_to_views(view_model: dict, sysml_code: str) -> dict:
    """把质量校验结果附加到视图投影结果（前端一并渲染）。"""
    if not isinstance(view_model, dict):
        return view_model
    quality = check_model_quality(sysml_code)
    view_model["quality_check"] = quality
    return view_model
