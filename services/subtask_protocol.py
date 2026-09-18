# -*- coding: utf-8 -*-
"""主-子 Agent 间结构化摘要协议（Task 2）：纯函数、零外部依赖。

背景：编排路径（agent/pipeline.py::_stream_orchestrated_flow）执行子任务时，
子 Agent 结果以纯文本透传（历史产物 card_data.exec.subtasks 即为此格式）。
本模块定义主-子 Agent 间的结构化摘要协议，供后续子任务执行/汇总消费
（Task 9/10 使用），本次仅建模块 + 自测。

- validate(raw)：校验并归一化结构化摘要（超长截断、非法结构归一，不抛异常）
- normalize_text(raw_text)：旧文本格式兼容解析 → 协议结构
- summarize_status(results)：一组子任务结果 status 三态聚合

纯函数模块：不 import DB / 网络 / LLM，可被 pipeline 与验证脚本复用。
"""

SCHEMA_VERSION = 1

# ── 协议合法取值（枚举）──
STATUS_VALUES = ("full", "partial", "failed")          # full=全部交付物就绪 / partial=部分缺失 / failed=无可交付物
KIND_VALUES = ("sysml", "requirement", "report", "doc")
RISK_LEVELS = ("high", "mid", "low")
EVIDENCE_SOURCES = ("graph", "vector", "attachment")

# ── 长度/数量上限 ──
MAX_SUMMARY = 500
MAX_TITLE = 60
MAX_DESC = 200
MAX_ADVICE = 200
MAX_REF = 100
MAX_ARTIFACTS = 20

DEFAULT_CONFIDENCE = 0.5
DEFAULT_TASK_KEY = "t"   # 与 pipeline 编排的 tkey 默认值保持一致

# ── 摘要 JSON Schema（文档化常量，供校验函数与后续消费方引用）──
SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["task_key", "status", "summary"],
    "properties": {
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION,
                           "description": "协议版本（v1）"},
        "task_key": {"type": "string", "maxLength": MAX_TITLE,
                     "description": "子任务标识，如 t2"},
        "status": {"type": "string", "enum": list(STATUS_VALUES),
                   "description": "full=全部交付物就绪 / partial=部分缺失(缺失在 missing[] 标注) / failed=无可交付物"},
        "summary": {"type": "string", "maxLength": MAX_SUMMARY,
                    "description": "一句话成果摘要"},
        "artifacts": {"type": "array", "maxItems": MAX_ARTIFACTS, "items": {
            "type": "object",
            "required": ["kind", "title", "ref"],
            "properties": {
                "kind": {"type": "string", "enum": list(KIND_VALUES)},
                "title": {"type": "string", "maxLength": MAX_TITLE},
                "ref": {"type": "string", "maxLength": MAX_REF,
                        "description": "subtask://{run_id}/{task_key}/a{idx}"},
            }}},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "required": ["source", "ref"],
            "properties": {
                "source": {"type": "string", "enum": list(EVIDENCE_SOURCES)},
                "ref": {"type": "string", "maxLength": MAX_REF},
            }}},
        "risks": {"type": "array", "items": {
            "type": "object",
            "required": ["level", "title", "desc", "advice"],
            "properties": {
                "level": {"type": "string", "enum": list(RISK_LEVELS)},
                "title": {"type": "string", "maxLength": MAX_TITLE},
                "desc": {"type": "string", "maxLength": MAX_DESC},
                "advice": {"type": "string", "maxLength": MAX_ADVICE},
            }}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "置信度 0~1，缺省 0.5"},
        "meta": {"type": "object",
                 "description": "执行元信息：latency_ms / model / degraded / token_count"},
    },
}

DEFAULT_SUMMARY = {
    "schema_version": SCHEMA_VERSION,
    "task_key": DEFAULT_TASK_KEY,
    "status": "partial",
    "summary": "",
    "artifacts": [],
    "evidence": [],
    "risks": [],
    "confidence": DEFAULT_CONFIDENCE,
    "meta": {},
}


def _cut(value, limit):
    """任意值 → 字符串并截断到 limit 字符（None → 空串）。"""
    if value is None:
        return ""
    s = str(value)
    return s if len(s) <= limit else s[:limit]


def validate(raw):
    """校验并归一化结构化摘要（不抛异常，永远返回结果 dict）。

    返回 {"ok", "sanitized", "reason", "truncated", "warnings"}：
    - ok: 结构是否合法（关键字段齐全 + status 合法 + artifacts 数组）
    - sanitized: 尽力归一化后的协议结构；raw 非 dict 时为 None
    - reason: ok=False 时的失败原因
    - truncated: 是否有字段被截断
    - warnings: 非致命提示（枚举越界、可选字段类型错误等）

    归一化规则：
    - schema_version 缺省/非 1 → 归一为 1（v1 兼容）
    - task_key / status / summary 缺关键字段 → ok=False 并尽力补默认值
    - status 非法值 → ok=False 并归一为 partial（保守：交付情况不明）
    - artifacts 非数组 → ok=False 并归一为 []；条目限 20 条
    - 超长字段截断：summary≤500、title≤60、desc≤200、advice≤200、evidence.ref≤100
    - confidence 越界 clamp 到 [0,1]，缺省 0.5；meta 非 dict 归一为 {}
    - status=partial 且带 missing[] 时保留（标注缺失交付物，限 20 条）
    """
    warnings = []
    truncated = False
    if not isinstance(raw, dict):
        return {"ok": False, "sanitized": None,
                "reason": "raw 不是 dict 对象，无法归一化",
                "truncated": False, "warnings": ["输入不是 dict 对象"]}

    out = {}
    ok = True
    reason = None

    # schema_version：v1 兼容（缺省/非 1 → 归一为 1）
    sv = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(sv, int) or sv != SCHEMA_VERSION:
        warnings.append(f"schema_version={sv!r} 不受支持，归一为 {SCHEMA_VERSION}")
        sv = SCHEMA_VERSION
    out["schema_version"] = sv

    # task_key（关键字段）
    tk = raw.get("task_key")
    if tk is None or str(tk).strip() == "":
        if reason is None:
            ok, reason = False, "缺关键字段 task_key"
        warnings.append("task_key 缺失，占位为 't'")
        tk = DEFAULT_TASK_KEY
    out["task_key"] = _cut(tk, MAX_TITLE)
    if len(str(tk)) > MAX_TITLE:
        truncated = True

    # status（关键字段）
    st = raw.get("status")
    if st is None or str(st).strip() == "":
        if reason is None:
            ok, reason = False, "缺关键字段 status"
        warnings.append("status 缺失，保守归一为 'partial'")
        st = "partial"
    elif str(st) not in STATUS_VALUES:
        if reason is None:
            ok, reason = False, f"未知 status 值 {st!r}"
        warnings.append(f"status={st!r} 非法，归一为 'partial'")
        st = "partial"
    out["status"] = str(st)

    # summary（关键字段）
    sm = raw.get("summary")
    if sm is None or str(sm).strip() == "":
        if reason is None:
            ok, reason = False, "缺关键字段 summary"
        sm = ""
    out["summary"] = _cut(sm, MAX_SUMMARY)
    if len(str(sm)) > MAX_SUMMARY:
        truncated = True

    # artifacts（数组，限 20 条）
    arts = raw.get("artifacts", [])
    if arts is None or not isinstance(arts, list):
        if reason is None:
            ok, reason = False, "artifacts 必须是数组"
        warnings.append("artifacts 非数组，归一为空列表")
        arts = []
    if len(arts) > MAX_ARTIFACTS:
        truncated = True
    out["artifacts"] = []
    for i, a in enumerate(arts[:MAX_ARTIFACTS]):
        if not isinstance(a, dict):
            warnings.append(f"artifacts[{i}] 非对象，丢弃")
            continue
        kind = a.get("kind")
        if kind is not None and str(kind) not in KIND_VALUES:
            warnings.append(f"artifacts[{i}].kind={kind!r} 不在枚举内")
        ref = a.get("ref")
        if len(str(a.get("title", ""))) > MAX_TITLE or len(str(ref or "")) > MAX_REF:
            truncated = True
        out["artifacts"].append({
            "kind": str(kind) if kind is not None else "doc",
            "title": _cut(a.get("title"), MAX_TITLE),
            "ref": str(ref) if ref is not None else f"a{i}",
        })

    # evidence（数组，ref≤100）
    evs = raw.get("evidence", [])
    if evs is None or not isinstance(evs, list):
        warnings.append("evidence 非数组，归一为空列表")
        evs = []
    out["evidence"] = []
    for i, e in enumerate(evs):
        if not isinstance(e, dict):
            warnings.append(f"evidence[{i}] 非对象，丢弃")
            continue
        src = e.get("source")
        if src is not None and str(src) not in EVIDENCE_SOURCES:
            warnings.append(f"evidence[{i}].source={src!r} 不在枚举内")
        if len(str(e.get("ref", ""))) > MAX_REF:
            truncated = True
        out["evidence"].append({
            "source": str(src) if src is not None else "graph",
            "ref": _cut(e.get("ref"), MAX_REF),
        })

    # risks（数组，level/title/desc/advice 截断）
    rks = raw.get("risks", [])
    if rks is None or not isinstance(rks, list):
        warnings.append("risks 非数组，归一为空列表")
        rks = []
    out["risks"] = []
    for i, r in enumerate(rks):
        if not isinstance(r, dict):
            warnings.append(f"risks[{i}] 非对象，丢弃")
            continue
        lv = r.get("level")
        if lv is not None and str(lv) not in RISK_LEVELS:
            warnings.append(f"risks[{i}].level={lv!r} 不在枚举内")
        if any(len(str(r.get(k, ""))) > lim for k, lim in
               (("title", MAX_TITLE), ("desc", MAX_DESC), ("advice", MAX_ADVICE))):
            truncated = True
        out["risks"].append({
            "level": str(lv) if lv is not None else "mid",
            "title": _cut(r.get("title"), MAX_TITLE),
            "desc": _cut(r.get("desc"), MAX_DESC),
            "advice": _cut(r.get("advice"), MAX_ADVICE),
        })

    # confidence：数值，clamp [0,1]，缺省 0.5
    conf = raw.get("confidence", DEFAULT_CONFIDENCE)
    if conf is None or isinstance(conf, bool) or not isinstance(conf, (int, float)):
        warnings.append(f"confidence={conf!r} 非数值，归一为 {DEFAULT_CONFIDENCE}")
        conf = DEFAULT_CONFIDENCE
    else:
        conf = float(conf)
        if conf < 0.0 or conf > 1.0:
            warnings.append(f"confidence={conf!r} 越界，clamp 到 [0,1]")
            conf = min(max(conf, 0.0), 1.0)
    out["confidence"] = conf

    # meta：必须 dict
    meta = raw.get("meta")
    if meta is None:
        meta = {}
    elif not isinstance(meta, dict):
        warnings.append("meta 非 dict，归一为空对象")
        meta = {}
    out["meta"] = dict(meta)

    # partial 状态下缺失项标注（可选，兼容 status 语义描述）
    missing = raw.get("missing")
    if out["status"] == "partial" and isinstance(missing, list) and missing:
        out["missing"] = [_cut(m, MAX_TITLE) for m in missing[:MAX_ARTIFACTS]]

    return {"ok": ok, "sanitized": out, "reason": reason,
            "truncated": truncated, "warnings": warnings}


def normalize_text(raw_text):
    """旧文本格式兼容解析：历史编排产物中纯文本子任务结果 → 协议结构。

    历史产物（card_data.exec.subtasks）里子 Agent 直接输出纯文本（pipeline
    中 sub_res / done_items[].result）。解析规则：视为 status=full（有交付
    内容）、summary=文本前 MAX_SUMMARY 字符、artifacts/evidence/risks 为空、
    confidence=0.5、meta 为空。非 str 输入（None 等）归为一空文本摘要。
    """
    if raw_text is None:
        text = ""
    elif isinstance(raw_text, str):
        text = raw_text
    else:
        text = str(raw_text)
    return {
        "schema_version": SCHEMA_VERSION,
        "task_key": DEFAULT_TASK_KEY,
        "status": "full",
        "summary": text[:MAX_SUMMARY],
        "artifacts": [],
        "evidence": [],
        "risks": [],
        "confidence": DEFAULT_CONFIDENCE,
        "meta": {},
    }


def summarize_status(results):
    """聚合一组子任务结果的 status 三态。

    规则（按序判定）：全 full → full；有 partial → partial；
    有 failed 且无任何成功(full/partial) → failed；其余混合 → partial。
    忽略非 dict 项与非法 status 值；空列表视为 partial（无证据表明全部就绪）。
    """
    statuses = []
    for r in results or []:
        if isinstance(r, dict) and r.get("status") in STATUS_VALUES:
            statuses.append(r["status"])
    if not statuses:
        return "partial"
    if all(s == "full" for s in statuses):
        return "full"
    if "partial" in statuses:
        return "partial"
    if "failed" in statuses and not any(s in ("full", "partial") for s in statuses):
        return "failed"
    return "partial"
