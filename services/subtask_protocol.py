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

# ── 结论分档（★ 2026-10-14 批次三 #16）──────────────────────────────────────
# **为什么在 `basis` 之外还要 claims[]**：两者回答的不是同一个问题。
#   · `evidence[].basis` 是**给证据分档** —— "你引用的这条依据有多硬"；
#   · `claims[].kind`   是**给结论分档** —— "你这句话本身能不能直接采信"。
# 缺 `claims` 时，读者只能从 evidence 间接猜结论的可信度，而**最危险的那类结论
# 往往不带 evidence**（模型自顾自下了个结论，谁都没引）—— 那正是 `asserted` 兜底覆盖、
# 但读者在汇总正文里**看不见**的一类。
#
# 三档的判据是「要复核它，需要做什么」：
#   · `fact`      = 结论本身可被第三方独立复核（引用了具体 id/路径/校验器输出）；
#   · `inference` = 由若干 fact 推出，**复核它要先把那些 fact 复核一遍**（链式）；
#   · `to_verify` = **明确需要验证但尚未验证** —— 模型自己知道它没底。
#
# ⚠️ `to_verify` 是本项的**核心新增档**，它与 `basis=asserted` 是**不同语义**：
#   · `asserted` = 「模型没给引用」—— 可能是它偷懒，也可能它私下查过只是没写；
#   · `to_verify` = 「模型明说这条没验证」—— 是**主动的诚实声明**。
# 此前二者混在一档，导致「诚实声明自己没底」与「懒得给引用」不可区分，
# 评审/汇总无法优先处理前者。⚠️ `to_verify` **不是**"更假的断言"，
# 它的价值在于**把不确定性显式化**，因此下游必须**呈现**它而不是隐藏它。
#
# **能力边界（同 basis）**：kind 是**模型自报**，服务端只做枚举校验与截断。
#   我们无从判断一条 `fact` 是不是真的查过 ⇒ **不自动采信、不自动过滤**，
#   只在汇总与评审中**分档呈现**，让人能一眼看出"哪些结论要自己去核"。
CLAIM_KINDS = ("fact", "inference", "to_verify")
# 缺 `kind` 时的兜底：`to_verify`（**比 `basis` 的 `asserted` 更保守**）。
# 为什么更保守：一条没标 kind 的结论，读者无法判断它是否需要复核 ——
#   此时最安全的假设是"它需要复核"，而不是"它是事实"。
DEFAULT_CLAIM_KIND = "to_verify"

# ── 长度/数量上限 ──
MAX_SUMMARY = 500
MAX_TITLE = 60
MAX_DESC = 200
MAX_ADVICE = 200
MAX_REF = 100
MAX_ARTIFACTS = 20
MAX_CLAIMS = 30

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
        # ★ 2026-10-14 批次三 #16：claims 为**可选**（刻意不列 required，
        #   否则旧格式全判 ok=False ⇒ 存量全红）。
        "claims": {"type": "array", "items": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "maxLength": MAX_SUMMARY,
                         "description": "结论一句话（供汇总/评审消费）"},
                "kind": {"type": "string", "enum": list(CLAIM_KINDS),
                         "description": "fact=可独立复核 / inference=由 fact 推出（链式复核）"
                                        " / to_verify=明确待验证（模型自报的诚实声明）。"
                                        "★ 模型自报，服务端只校验枚举，不验证真实性；"
                                        "缺省按 to_verify（最保守）处理"},
                "subject": {"type": "string", "maxLength": MAX_TITLE,
                            "description": "★ 冲突检测锚点：这条结论讲的是哪个对象"
                                           "（实体名/指标名）。同 subject 不同 text ⇒ 冲突候选"},
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
    "claims": [],
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

    # claims（★ 2026-10-14 批次三 #16；**可选字段**，缺省空数组）
    # ⚠️ 与 artifacts/risks 不同：claims 非数组**不判 ok=False** ——
    #   它是新增的**可选**维度，旧数据全都没有它；若因此把存量判成非法，
    #   就等于"加一个字段废掉一批已归档结果"（与 basis 当初刻意不进 required 同理）。
    cls_ = raw.get("claims", [])
    if cls_ is None:
        cls_ = []
    if not isinstance(cls_, list):
        warnings.append("claims 非数组，归一为空列表（不判非法：可选字段）")
        cls_ = []
    if len(cls_) > MAX_CLAIMS:
        truncated = True
        warnings.append("claims 条数 %d 超过上限 %d，已截断" % (len(cls_), MAX_CLAIMS))
    out["claims"] = []
    for i, c in enumerate(cls_[:MAX_CLAIMS]):
        if not isinstance(c, dict):
            warnings.append("claims[%d] 非对象，丢弃" % i)
            continue
        _txt = c.get("text")
        if _txt is None or not str(_txt).strip():
            # text 是 claims 的**唯一必填**：没有内容的条目等于没写
            warnings.append("claims[%d] 缺 text，丢弃" % i)
            continue
        if len(str(_txt)) > MAX_SUMMARY:
            truncated = True
        _k = c.get("kind")
        if _k is not None and str(_k) not in CLAIM_KINDS:
            warnings.append("claims[%d].kind=%r 不在枚举内，按 %s 处理"
                            % (i, _k, DEFAULT_CLAIM_KIND))
            _k = None
        _sub = c.get("subject")
        if _sub is not None and len(str(_sub)) > MAX_TITLE:
            truncated = True
        out["claims"].append({
            "text": _cut(_txt, MAX_SUMMARY),
            # 兜底方向：缺 kind ⇒ to_verify（**比 basis 的 asserted 更保守**）。
            # 理由：没标 kind 的结论，读者无从判断要不要复核 ⇒ 假设"要"才安全。
            "kind": str(_k) if _k is not None else DEFAULT_CLAIM_KIND,
            # subject 是**冲突检测的锚点**（批次三 #15 消费），缺省空串而非丢弃整条 ——
            #   丢掉会连带丢掉那条有价值的结论本身。
            "subject": _cut(_sub, MAX_TITLE),
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
        "claims": [],      # ★ 2026-10-14 #16：纯文本无结构化结论 ⇒ 空数组（不是伪造）
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


# ══════════════════════════════════════════════════════════════════════════
# 批次三 #15：多 Agent 结论冲突检测（**确定性，不调 LLM**）
# ══════════════════════════════════════════════════════════════════════════
#
# ## 要解决的真缺口（2026-10-14 取证）
#   `workflows/refine.py::RefineGate` 存在且被 `stream.py:1135` 调用，但评审输入
#   只有**拼接后的** `orch_content` —— 它看到的是"综合报告"，不是"各子 Agent 各自的结论"。
#   ⇒ 两个子 Agent 对同一事实给了相反说法时，**综合报告会把它们揉成一句看似自洽的话**，
#     评审（无论多准）都发现不了，因为它拿不到"这是 t2 说的、那是 t3 说的"这个归属。
#
# ## 为什么不用 LLM 判定冲突（刻意的取舍）
#   ① 成本：每次编排多一次 LLM 调用，而这是**每轮必跑**的路径；
#   ② 可靠性：LLM 判"矛盾"本身不可靠，且**不可复现**（温度非零）；
#   ③ 可解释性：报"冲突"必须能指出**哪两条、差在哪**，
#     否则用户只能收到一句"可能存在矛盾"，无法自行裁决。
#   ⇒ 只做**可判定的确定性档**：同一 subject（对象/指标）上出现**互斥的取值**。
#     判不出的（措辞不同但同义、隐含矛盾）**明确不报**，宁可漏也不错报。
#
# ## 判据（全部是可复核的字面比较，不含语义推断）
#   C1 **同 subject 不同结论**：两条 claim 的 subject 归一后相同，
#      但抽取出的**取值不同**（数值型按归一化后的数字串比；枚举型按归一化后的词比）。
#   C2 **数值互斥**：同一 subject 上出现不同数值 ⇒ 冲突（附容差，见 `value_equal`）。
#   C3 **枚举互斥**：同一 subject 上出现不同枚举值，且**不在同一互斥族内**（如 否/是）。
#   C4 **反向断言**：同一 subject 上出现「支持/不支持」「是/否」「有/无」这类
#      **极性词**相反 ⇒ 冲突（即使措辞完全不同）。
#
# ## 能力边界（重要，别把它当语义冲突检测用）
#   · 只看 **claims[].subject**，没写 subject 的 claim **一律不参与**
#     （无锚点就无从判断"是不是在讲同一件事"）；
#   · 只报**取值互斥**，不报"程度不同""描述详略不同""一个更具体一个更概括"；
#   · ⚠️ 归一化是**字面**的（去空白/标点/大小写/单位后缀），
#     同义词（"3 个"/"三个"）**认不出** ⇒ 会漏。宁可漏。
#   ⇒ 它的定位是"**兜住最刺眼的那一类矛盾**"，不是"发现所有矛盾"。

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 常见动词/状态前缀（用于剥出中心名词）。**刻意保守**：只收几乎无歧义的。
_VERBS = ("对外提供", "提供了", "提供", "支持", "包含", "具有", "包含有", "带有",
          "已经", "已", "可以", "能够", "能", "可", "会")
# 极性词：互为反义。**只收语义高度确定的**，不收"高/低"这类可同时成立的。
_POLARITY = {
    "支持": 1, "不支持": 0, "反对": 0,
    "是": 1, "否": 0, "有": 1, "无": 0,
    "true": 1, "false": 0,
    "yes": 1, "no": 0,
    "启用": 1, "禁用": 0, "开启": 1, "关闭": 0,
    "存在": 1, "不存在": 0,
    "可以": 1, "不可以": 0, "能": 1, "不能": 0,
    "允许": 1, "禁止": 0,
}


def _norm_subject(s):
    """subject 归一：去空白与常见标点，转小写。**字面归一，不做同义扩展**。"""
    t = str(s or "").strip().lower()
    t = re.sub(r"[\s　]+", "", t)
    return t.strip("：:，,。.、;；'\"“”‘’()（）[]【】")


def _norm_text(s):
    """text 归一：去空白/中文标点/句末标点，转小写。用于取值与极性比较。"""
    t = str(s or "").strip().lower()
    t = re.sub(r"[\s　]+", "", t)
    t = re.sub(r"[，,。.、;；:：!！?？'\"“”‘’()（）\[\]【】]", "", t)
    return t.strip()


def _extract_value(text):
    """从结论文本里抽**可比对的取值**：优先数值，否则归一后的整串。

    返回 (kind, value)：kind ∈ {"num","enum"}；抽不出返回 ("", "")。

    ⚠️ 数值**必须带单位后缀展开**（"128k" ⇒ 128000、"3万" ⇒ 30000）——
      否则 "128k" 与 "128000" 会被判成两个取值 ⇒ **误报冲突**。
      ⚠️ 这里只取**第一处**数字，且必须**从数字本身匹配**：
      原实现写 `float(m.group(0).rstrip())` 而 `group(0)` 含前面的文字
      （"上下文窗口128k"）⇒ ValueError 被吞 ⇒ 整条退回枚举路径 ⇒ 单位归一形同虚设。
      （这处是门禁 `verify_claims_and_conflicts` C2 当场抓出来的。）
    """
    t = _norm_text(text)
    if not t:
        return "", ""
    _m = _NUM_RE.search(t)
    if _m:
        try:
            v = float(_m.group(0))
            # 单位后缀紧跟数字之后（如 "128k" / "3万"）
            _suf_m = re.match(r"\s*([km万千百])", t[_m.end():])
            suf = _suf_m.group(1) if _suf_m else ""
            mult = {"k": 1e3, "m": 1e6, "万": 1e4, "千": 1e3, "百": 1e2}.get(suf, 1)
            v2 = v * mult
            # 能表示为整数就取整，避免 "3" 与 "3.0" 判成不同
            return "num", (str(int(round(v2))) if abs(v2 - round(v2)) < 1e-9 else str(v2))
        except Exception:
            pass
    return "enum", t


def _polarity(text):
    """极性判定：文本里出现极性词则返回 1/0，无则 None。

    ⚠️ **必须按词长降序匹配、且长词命中后不重复计入短词** ——
      否则「不支持」里同时命中「不支持」(0) 与「支持」(1) ⇒ found={0,1} ⇒ 返回 None，
      极性冲突**永远检不出来**。
      （这处是门禁 `verify_claims_and_conflicts` C3 当场抓出来的。）
    ⚠️ 多个**互不嵌套**的极性词同时出现（如"不是没有"）⇒ 返回 None（**判不出就不报**）。
    """
    t = _norm_text(text)
    found = set()
    consumed = []          # 已命中的区间，用于排除嵌套短词
    for w in sorted(_POLARITY, key=len, reverse=True):
        start = 0
        while True:
            i = t.find(w, start)
            if i < 0:
                break
            end = i + len(w)
            # 已被更长的词覆盖 ⇒ 跳过（嵌套）
            if not any(a <= i and end <= b for a, b in consumed):
                found.add(_POLARITY[w])
                consumed.append((i, end))
            start = end
    if len(found) == 1:
        return next(iter(found))
    return None


def _char_overlap(a, b):
    """两个核心词的**字符 Jaccard 重叠度**（0~1），用于判"一详一略"。

    例："接口"(2 字) 与 "外部接口"(4 字) ⇒ 交 2 / 并 4 = **0.5**；
       "批量导入" 与 "导入接口" ⇒ 交 1（导入）/ 并 6 ≈ 0.17。
    ⚠️ 阈值由**门禁实测标定**（`verify_claims_and_conflicts` C6）：
       定 0.75 时 "提供接口"/"提供外部接口" 能豁免（0.5 < 0.75 ⇒ **不能**豁免）——
       ⇒ 实测发现 0.5 就该豁免，故阈值取 **0.5**，并由 C6/C8 两条判据双向锁住：
       高重叠 ⇒ 不报；低重叠 ⇒ 必须报。⚠️ 调这个数会同时改这两条判据的期望。
    """
    sa, sb = set(str(a or "")), set(str(b or ""))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _tail_core(t):
    """取结论文本的**中心名词**（末段名词性成分），用于判"一详一略"。

    ⚠️ 这里**刻意做得很浅**：只剥离常见动词/形容前缀（提供/支持/包含/具有/已/可/能…），
      取剩下的核心部分。真实构词千变万化（"提供接口" vs "提供外部接口" vs "对外接口"），
      任何启发式都认不全。
    ⇒ **认不出时返回空串**，调用方据此**保持报冲突**（宁可误报也不漏报矛盾）。
      第一版想返回整串，导致 `cores` 恒有值 ⇒ 子串豁免永远走真实分支 ⇒ 等于没做。
    """
    s = str(t or "")
    if not s:
        return ""
    for _ in range(3):                      # 最多剥 3 层前缀
        _hit = None
        for p in _VERBS:
            if s.startswith(p) and len(s) > len(p):
                _hit = p
                break
        if _hit is None:
            break
        s = s[len(_hit):]
    return s if 0 < len(s) <= 12 else ""


def detect_conflicts(summaries, max_conflicts=10):
    """在一组子任务摘要里检出**结论互斥**（批次三 #15）。

    参数 summaries：一组 `validate()` 归一后的协议结构（或含 `claims` 的 dict）。
        每个元素可带 `task_key`，用于把冲突归因到具体子 Agent。
    返回：[{"subject", "kind", "claims": [{task_key, text, kind, value}], "reason"}]

    ⚠️ 返回**空列表**有三种截然不同的原因，调用方必须能区分（否则又是一轮假绿）：
      ① 真的没有互斥（正常）；
      ② 根本没抽到可比对的 claim（**协议没落地**）—— 看 `anchored` 字段；
      ③ 抽到了 claim 但都没写 `subject`（**锚点缺失**）—— 看 `anchored` 字段。
    ⇒ 返回值里带 `stats`（本函数把它放进每个条目会冗余，故另设 `conflict_stats`），
      调用方应同时读 `conflict_stats(summaries)` 判断"是真没冲突还是没数据可判"。
    """
    buckets = {}          # norm_subject -> [claim 记录]
    for s in summaries or []:
        if not isinstance(s, dict):
            continue
        tk = str(s.get("task_key") or "-")
        for c in (s.get("claims") or []):
            if not isinstance(c, dict):
                continue
            txt = str(c.get("text") or "").strip()
            if not txt:
                continue
            sub = _norm_subject(c.get("subject"))
            if not sub:
                continue          # ★ 无锚点不参与：无从判断是否在讲同一件事
            buckets.setdefault(sub, []).append({
                "task_key": tk,
                "text": txt[:120],
                "kind": str(c.get("kind") or DEFAULT_CLAIM_KIND),
                "_val": _extract_value(txt),
                "_pol": _polarity(txt),
                "_nt": _norm_text(txt),
            })

    conflicts = []
    for sub, items in buckets.items():
        if len(items) < 2:
            continue
        # ── C4 极性互斥优先（最刺眼的一类，且不看取值是否真的不同）──
        pols = {}
        for it in items:
            if it["_pol"] is not None:
                pols.setdefault(it["_pol"], []).append(it)
        reported = set()
        if len(pols) >= 2:
            sides = sorted(pols)
            picked = []
            for v in sides:
                picked.extend(pols[v][:2])
            conflicts.append({
                "subject": sub,
                "kind": "polarity",
                "claims": [{k: it[k] for k in ("task_key", "text", "kind")} for it in picked],
                "reason": "同一对象出现相反断言（极性词相反：%s）"
                          % " vs ".join("肯定" if v else "否定" for v in sides),
            })
            for it in picked:
                reported.add(id(it))
            if len(conflicts) >= max_conflicts:
                break
        # ── C1/C2/C3 取值互斥 ──
        groups = {}
        for it in items:
            if id(it) in reported:
                continue
            k, v = it["_val"]
            if not v:
                continue
            groups.setdefault((k, v), []).append(it)
        if len(groups) >= 2:
            vals = sorted(groups)
            picked = []
            for g in vals:
                picked.extend(groups[g][:1])
            # 枚举型要再排一次"其实说的是同一件事"的情形：
            #   "3 个接口" vs "3个接口" 已由归一处理；
            #   此处排除**中心词相同**的（"提供接口" vs "提供外部接口" 属一详一略，不是矛盾）。
            # ⚠️ **不能用整串子串判断**（门禁当场证伪）："提供接口" 并非 "提供外部接口"
            #   的子串（中间隔了"外部"）⇒ 子串法对最典型的"加限定词"情形**完全失效**。
            #   改为比**尾部中心词**（最后一个名词性片段）：一致 ⇒ 判为同一件事，不报。
            # ⚠️ 取比较对象必须按**该分组自己的 value** 找条目（分组键是 `(kind, value)`）——
            #   取 `groups[g][0]` 即"组内第一条"，同组多条时拿到的可能不是这个取值的文本。
            if vals[0][0] == "enum":
                texts = []
                for g in vals:
                    _hit = next((it["_nt"] for it in groups[g] if it["_val"][1] == g[1]),
                                groups[g][0]["_nt"])
                    texts.append(_hit)
                cores = [_tail_core(x) for x in texts]
                # ★ 判"一详一略"用**核心词的字符重叠度**，不是子串：
                #   "接口" vs "外部接口" 子串成立，但"批量导入" vs "导入接口" 就不是。
                #   门禁实测证伪过子串法（对最典型的"加限定词"完全失效）。
                #   阈值 0.75：核心词高度重合才豁免，差异大的一律报冲突。
                # ⚠️ 认不出核心词（空串）⇒ **保持报冲突**（宁可误报不漏报矛盾）。
                _pairs = [(a, b) for i, a in enumerate(cores) for j, b in enumerate(cores)
                          if i != j]
                if _pairs and all(a and b and _char_overlap(a, b) >= 0.5
                                  for a, b in _pairs):
                    continue
            conflicts.append({
                "subject": sub,
                "kind": "value",
                "claims": [{k: it[k] for k in ("task_key", "text", "kind")} for it in picked],
                "reason": "同一对象给出不同取值：%s"
                          % " vs ".join("%s(%s)" % (g[1], groups[g][0]["task_key"]) for g in vals),
            })
            if len(conflicts) >= max_conflicts:
                break
    return conflicts


def conflict_stats(summaries):
    """冲突检测的**可证伪口径**：到底有多少 claim 参与了判定。

    ⚠️ 这个函数存在的唯一理由：`detect_conflicts` 返回 `[]` 时，
    调用方**分不清**"没冲突"和"压根没数据可判"。
    ⇒ 任何以"未检出冲突"为依据的判定，都必须同时读这里的 `anchored`，
      且 `anchored == 0` 时**不得声称"已检查无冲突"**（那是本仓反复栽过的假绿）。
    """
    total = anchored = 0
    subjects = set()
    for s in summaries or []:
        if not isinstance(s, dict):
            continue
        for c in (s.get("claims") or []):
            if not isinstance(c, dict):
                continue
            if not str(c.get("text") or "").strip():
                continue
            total += 1
            sub = _norm_subject(c.get("subject"))
            if sub:
                anchored += 1
                subjects.add(sub)
    # 可判冲突的最小前提：同一 subject 上至少有 2 条 claim
    comparable = sum(1 for s in summaries or [] if isinstance(s, dict)
                     and len([c for c in (s.get("claims") or [])
                              if isinstance(c, dict) and _norm_subject(c.get("subject"))]) >= 1)
    return {"claims_total": total, "claims_anchored": anchored,
            "subjects": len(subjects), "subtasks_with_claims": comparable,
            # ★ 一句话口径，供日志/事件直接引用
            "verdict": ("no-data" if anchored == 0 else
                        "insufficient" if comparable < 2 else "checkable")}


def format_conflicts(conflicts, limit=5):
    """把冲突渲染成给评审/用户看的一行行文本（**刻意不隐藏任何一条**）。"""
    out = []
    for c in (conflicts or [])[:limit]:
        sides = "；".join("[%s] %s" % (x.get("task_key"), x.get("text")) for x in (c.get("claims") or []))
        out.append("⚠️ %s（%s）：%s" % (c.get("subject"), c.get("reason"), sides))
    return "\n".join(out)
