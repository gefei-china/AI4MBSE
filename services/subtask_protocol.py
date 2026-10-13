# -*- coding: utf-8 -*-
"""主-子 Agent 间结构化摘要协议（Task 2）：纯函数、零外部依赖。

背景：编排路径（agent/pipeline.py::_stream_orchestrated_flow）执行子任务时，
子 Agent 结果以纯文本透传（历史产物 card_data.exec.subtasks 即为此格式）。
本模块定义主-子 Agent 间的结构化摘要协议，供后续子任务执行/汇总消费
（Task 9/10 使用），本次仅建模块 + 自测。

- validate(raw)：校验并归一化结构化摘要（超长截断、非法结构归一，不抛异常）
- normalize_text(raw_text)：旧文本格式兼容解析 → 协议结构
- ui_summary(raw_text)：把子 Agent 原始输出压成**给用户看的一句话**（剔除内部交接语）
- summarize_status(results)：一组子任务结果 status 三态聚合

纯函数模块：不 import DB / 网络 / LLM（仅标准库 json+re），可被 pipeline 与验证脚本复用。
"""
import json
import re

SCHEMA_VERSION = 1

# ── 协议合法取值（枚举）──
STATUS_VALUES = ("full", "partial", "failed")          # full=全部交付物就绪 / partial=部分缺失 / failed=无可交付物
KIND_VALUES = ("sysml", "requirement", "report", "doc")
RISK_LEVELS = ("high", "mid", "low")
EVIDENCE_SOURCES = ("graph", "vector", "attachment")

# ── 事实/推断分档（★ 2026-10-13，文章 §08）─────────────────────────────────
# **加一维"这句话的根据是什么"**，与 `source`（依据来自哪个检索通道）正交：
#   · source 答"从哪儿找到的"（图谱/向量库/附件）；
#   · basis  答"这是查到的还是想出来的"—— 后者才是**读者能不能直接信**的依据。
#
# 取值（三档，刻意不用"真/假"二元 —— 模型自报的"真"不可信，见下方"能力边界"）：
#   · `verified`：有可复核的引用（图谱实体/需求条目 id/校验器输出/附件路径），
#                 读者可自己回查；
#   · `derived` ：由已验证事实**算出来/推出来**的（可复核，但需跟着依据一起看）；
#   · `asserted`：模型**直接断言**、无可复核引用（可能是对的，也可能是在编）。
#     ⚠️ 不叫 `inferred`：那会暗示"这是推理结论因而可靠"，
#     实际上"无引用的断言"恰恰是**最不可靠**的一档，命名不能误导读者。
#
# **能力边界（重要，别把它当校验器用）**：
# 本字段是**模型自报**，服务端**只做枚举校验与截断，不做真实性验证** ——
# 我们没有任何手段判断一条 `verified` 是不是真的查过图谱。
#   ⇒ 因此 `basis` 的正确用法是**呈现**（"以下 N 条为无引用断言"），
#     **不是**自动采信/自动过滤（否则等于用一个未经验证的标签去决定内容去留）。
#   ⇒ 兜底：缺 `basis` 时按 `asserted` 处理（最保守的一档）——
#     旧数据（无该字段）与"模型忘了写"都落到这里，不会被高估。
BASIS_VALUES = ("verified", "derived", "asserted")
DEFAULT_BASIS = "asserted"     # 缺字段时的保守兜底

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
                # ★ 2026-10-13 §08：basis 为**可选**（刻意不列进 required）——
                #   列进去会让所有旧格式（无该字段）判 ok=False ⇒ 存量全红。
                "basis": {"type": "string", "enum": list(BASIS_VALUES),
                          "description": "依据强度：verified=有可复核引用 / derived=由已验证事实推出"
                                         " / asserted=无引用的直接断言（最不可靠）。"
                                         "★ 模型自报，服务端只校验枚举，不验证真实性"},
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
        # ★ 2026-10-13 §08：basis 归一化（**可选字段，缺则按 asserted 兜底**）。
        # 兜底方向是"往更不可靠的一档靠"：宁可把没标的一律当未验证，
        # 也不让"漏写 basis"被当成 verified —— 那等于用一个能高估自己的标签去决定内容可信度。
        _basis = e.get("basis")
        if _basis is not None and str(_basis) not in BASIS_VALUES:
            warnings.append(f"evidence[{i}].basis={_basis!r} 不在枚举内，按 {DEFAULT_BASIS} 处理")
            _basis = DEFAULT_BASIS
        out["evidence"].append({
            "source": str(src) if src is not None else "graph",
            "ref": _cut(e.get("ref"), MAX_REF),
            "basis": str(_basis) if _basis is not None else DEFAULT_BASIS,
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


def extract_protocol_json(text):
    """★ 2026-10-13（批次二 P2-2）：从子任务输出里**抽出尾部的协议 JSON**。

    为什么需要（实测踩到的一个真缺口）：子任务 prompt 要求的是
    「**正文在前**，另起一段以单个 JSON 对象结尾」（不能只吐 JSON —— 那会毁掉
    面向用户的可读交付物）。而调用方此前的解析只试 `text.strip().startswith("{")`，
    对"正文 + JSON"这种形态**恒为 False** ⇒ 永远走 `normalize_text`
    ⇒ 三数组永远为空 ⇒ **注入契约了却仍拿不到结构化字段**。
    ⇒ 光注入契约不够，**解析端必须同步支持尾部 JSON**。

    识别口径（从后往前找，优先取最后一个平衡的 JSON 对象）：
      ① 整段就是 JSON → 直接用；
      ② 否则找**最后一个** `}`，向前做括号配平，得到候选片段后 `json.loads`；
      ③ 解析失败 → 返回 None（调用方回落 `normalize_text`，行为与改动前一致）。

    ⚠️ 只取**一个** JSON 对象（不合并多个）—— 多 JSON 场景本协议未定义，
    宁可漏也不猜（错的字段比空字段更坏）。
    ⚠️ 配平时**跳过字符串字面量内的括号**，否则正文中出现 `"}"` 就会算错配平。
    """
    s = str(text or "")
    if not s.strip():
        return None
    # ① 整段就是 JSON
    t = s.strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    # ② 从最后一个 } 向前配平
    end = t.rfind("}")
    if end < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(end, -1, -1):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                frag = t[i:end + 1]
                try:
                    obj = json.loads(frag)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def normalize_text(raw_text, task_status=None):
    """旧文本格式兼容解析：历史编排产物中纯文本子任务结果 → 协议结构。

    历史产物（card_data.exec.subtasks）里子 Agent 直接输出纯文本（pipeline
    中 sub_res / done_items[].result）。**有产出**时视为 status=full，
    summary=文本前 MAX_SUMMARY 字符、artifacts/evidence/risks 为空、
    confidence=0.5、meta 为空。非 str 输入（None 等）归为一空文本摘要。

    ★ 2026-10-13（对标 B1-2）：原实现**无条件**写 `status="full"`，被实测证伪为假绿源头 ——
      · `normalize_text` 是 `stream.py:450` 在 **LLM 未按协议输出 JSON** 时的唯一产出路径
        （实测 `agent_tasks.summary_json` **180/180 行 status 全为 `full`**，
         `artifacts/evidence/risks` 值非空率 **0%**、`confidence` 恒 0.5）；
      · 与此同时 `agent_tasks` 里有 **54 行 `failed`** —— 这些失败在**交付协议层完全不可见**；
      · `stream.py:805` 的三态聚合第 1 段因此**永久返回 full**，
        只剩 `:846` 的「只降不升」硬修正在兜底 ⇒ **"编排成功"在数据上不可证伪**。

      **修法只用一条可证伪的判据：文本是否为空**（不给启发式、不猜关键词）。
      依据是生产库实测（2026-10-12 只读，见下）：
        · `status='failed'` 的 54 行，`result` **100% 为空串**
          （error 聚类：46 条「编排中断…孤儿运行」+ 7 条 `timeout` + 1 条 `timeout（240s 无产出）`）；
        · `status='done'` 的 168 行，`result` **100% 非空**（最短 24 字符，p10=936，中位数 2562）。
      ⇒ 空/空白文本判 `failed`、有文本判 `full`，**在真库上零错分**。

      ⚠️ **刻意不做关键词判定**（曾评估并否决）：真库里 `done` 的 168 条中
        「无法」出现 **93** 次、「失败」16 次、「未能」17 次 —— 这些是**子 Agent 在正文里
        诚实描述上游缺口**（如"上游 t1 未产出需求条目，故本次无可校验对象"），
        属**有价值交付**，不是失败。用关键词判 failed 会把它们全部误杀。
      这也是本仓老教训「**「某条链路有噪声」≠「该修」——先证明噪声参与决策**」的同族应用。

    参数 task_status：调用方已知的执行状态（`agent_tasks.status`）。
      传入 `failed`/`blocked` 时**优先采用**，避免"执行已知失败但正文非空"被误判 full。
      None 表示不采信（保持纯文本判据）。
    """
    if raw_text is None:
        text = ""
    elif isinstance(raw_text, str):
        text = raw_text
    else:
        text = str(raw_text)
    # ── 状态判定（2026-10-13 新增；此前恒为 "full"）──
    # 优先级：调用方给的执行状态 > 空文本判据。
    # ⚠️ 只认 failed/blocked 两个"已确定未交付"的值；planned/ready/running 不采纳
    #    （那是瞬态，传进来说明调用方用错了字段，静默降级到文本判据更安全）。
    _ts = str(task_status or "").strip().lower()
    if _ts in ("failed", "blocked"):
        _status = "failed"
    elif not text.strip():
        _status = "failed"
    else:
        _status = "full"
    return {
        "schema_version": SCHEMA_VERSION,
        "task_key": DEFAULT_TASK_KEY,
        "status": _status,
        "summary": text[:MAX_SUMMARY],
        "artifacts": [],
        "evidence": [],
        "risks": [],
        "confidence": DEFAULT_CONFIDENCE,
        "meta": {"normalized_from_text": True,
                 "empty_text": not bool(text.strip())},
    }


# ── 面向用户的交付摘要（2026-09-26）─────────────────────────────────────────
#  背景（用户报障）：每张子任务卡下面那行摘要，显示的是"一、对上游意图识别结果的承接"
#  "本任务承接 t2 的执行计划，职责是…"这类**子 Agent 之间的内部交接语**，还被前端硬截 60 字
#  断在词中间（""职责是**产"）——用户问"这段是必须的吗、价值是什么"。
#  结论：**"有一行交付摘要"这件事要保留**（编排模式下过程可审计：每步交出了什么），
#  但要把它从"内部协议原文"改造成"面向用户的一句话"。
#  实现约束（重要）：本函数只用于**展示**，`normalize_text` 产出的 `summary` 原样保留 ——
#  后者会作为"上游快照"喂给下游子任务（Task 9/10 链路），清洗它会破坏子 Agent 的承接能力。
_UI_NOISE = (
    "承接", "上游", "下游", "前序", "本任务", "本子任务", "子任务", "职责是", "交接",
    "交付边界", "执行计划", "依赖 t", "任务标题", "作为", "综上所述",
)
# 标题行（`#`/`##`）是**小标题**不是交付内容 → 整行跳过；列表/编号标记里往往就是内容 → 只去标记保留文字
_UI_HEADING = re.compile(r"^\s*#{1,6}\s")
_UI_BULLET = re.compile(r"^\s*(?:[-*•>]\s+|[一二三四五六七八九十]+[、.．)）]\s*|\d+[、.．)）]\s*|"
                        r"（[一二三四五六七八九十\d]+）\s*)")


def ui_summary(raw_text, limit=60):
    """把子 Agent 的原始输出，压成**给用户看的一句话交付摘要**（找不到就返回空串）。

    规则（按序）：
      1) 去行内 markdown（`**粗体**`、反引号）；
      2) 逐行处理：**`#` 标题行整行跳过**（那是小标题，不是内容）；列表/编号标记只去标记、保留文字；
         含"承接/上游/职责是/执行计划"等**内部协作词**的行跳过；太短的行跳过；
      3) 取剩下的第一行，按**第一句**收口（。！？；），超长再按标点收口并补 `…`；
      4) 全被剔掉 → 返回 ""（**宁可没有这行**，也不把内部黑话端给用户）。
    为什么按句切：原实现是 `slice(0,60)`，必然断在词中间（实测"职责是**产"即被切断处）。
    """
    text = "" if raw_text is None else str(raw_text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)        # 粗体
    text = re.sub(r"`([^`]*)`", r"\1", text)            # 行内代码
    lines = []
    for ln in text.split("\n"):
        if not ln.strip() or _UI_HEADING.match(ln):     # ⚠️ 标题必须在去标记**之前**判掉：
            continue                                    #    否则 `## 交付说明` 会被当成正文返回（实测踩过）
        s = _UI_BULLET.sub("", ln).strip()
        if len(s) < 4:
            continue
        if any(w in s for w in _UI_NOISE):
            continue
        lines.append(s)
    if not lines:
        return ""
    para = lines[0]
    m = re.search(r"[。！？；!?;]", para)
    if m:                       # 先按**第一句**收口（摘要本质是"一句话"）
        para = para[:m.end()]
    if len(para) > limit:       # 超长再按最近的标点收口，避免断在词中间
        cut = max(para.rfind(c, 0, limit) for c in "，,、；;。！？!? ")
        para = (para[:cut].rstrip("，,、；; ") + "…") if cut > limit * 0.5 else (para[:limit] + "…")
    return para.strip()


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


def apply_quality_gate(agg_status, reflection):
    """把反思闭环（reflection）的质量信号并入编排汇总三态（full/partial/failed）。

    为什么需要（2026-09-19 取证）：`reflection.passed/score` 原先只写进 `card_data.reflection`，
    **不参与** `orchestrated_status` 聚合。后果是「失败被记录成成功」：子任务 t1/t3 的输出里
    明明写着「未产出交付物、请先确认缺口」，协议却因 LLM 正常返回文本而判 `full`；质量评审
    已给出 `passed=false / score=62`，卡片仍显示「正常完成」—— 用户完全无法观测失败。

    口径：**只降不升**（full → partial；已是 partial/failed 保持不变）。
    复用既有三态与前端徽章渲染（`06-cards.js` 已按 full/partial/failed 分支渲染），
    因此并入聚合即可让失败可见，**无需改前端**。

    返回 `(新状态, 缺口说明列表)`；reflection 缺失/通过时原样返回。
    """
    gaps = []
    if not isinstance(reflection, dict) or reflection.get("passed") is not False:
        return agg_status, gaps
    _iss = [str(i)[:60] for i in (reflection.get("issues") or [])[:2]]
    gaps.append(f"质量评审未通过（{reflection.get('score')}/100）"
                + ("：" + "；".join(_iss) if _iss else ""))
    return ("partial" if agg_status == "full" else agg_status), gaps
