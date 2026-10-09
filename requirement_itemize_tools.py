# -*- coding: utf-8 -*-
"""`requirement_itemize` —— 需求条目化 + 需求视图骨架生成。

为什么一个工具做两件事（这是刻意的设计）
--------------------------------------
N1（需求结构化）的产物是条目数据 `items[]`，但**条目本身不是模型**——
要进 SysML 必须变成 `requirement def` + 关系（refine/derive/satisfy）。
本工具一次产出两份，让 N1 的输出**直接可被 N3 消费**，中间不需要再转一次：

  ① `items`      —— 条目数据（供追溯矩阵、覆盖性分析、报告）
  ② `req_view_code` —— **需求视图骨架代码**（`package` + stakeholder + requirement + 关系）

职责边界（重要，别越界）
------------------------
* 本工具**只生成需求视图**，不生成结构/行为/视图 —— 那是 N3 其他 `view_type` 的活。
* `satisfy`（需求→设计元素）**不在这里生成**：此时还没有 design 元素，
  强行生成会引用不存在的元素（实测报`Must be a valid feature`）。
  satisfy 分配由 **N2 骨架生成后 / N5 追溯核验时**补，本工具只留 `satisfy_target` 字段供后续引用。

    ┌─ 需求条目化 ──► items[]（数据）
    │                req_view_code（需求视图：stakeholder/requirement/refine/derive）
    │                          │
    └──────────────────────────┴──► N3 视图展开（structure/usecase/activity…）
                                        │
                                        ▼N2/N5 补 satisfy 分配（因为需要 design 元素）
"""
import logging
import re
import uuid

logger = logging.getLogger(__name__)

CATEGORIES = ["functional", "performance", "interface", "constraint"]
CAT_CODE = {"functional": "F", "performance": "P", "interface": "I", "constraint": "C"}

# 模糊词（实测来自 requirement_quality.FUZZY_WORDS 的同款判据）
FUZZY = ("良好", "优秀", "快速", "高效", "友好", "灵活", "尽量", "尽可能", "适当",
         "合理", "显著", "大幅", "一定程度", "若干", "某些")
# 量纲词（出现数值+单位 ⇒ 性能类）
# ⚠️ **只收录含中文/明确多字符的单位**。实测踩过：收录单字母 `C`/`s`/`A`/`V`/`W` 后，
#   「通过 CAN 总线通信」会因含 C、A 而被判成 performance ⇒ 接口需求全被误分类。
#   单位要么用中文（分钟/秒/℃），要么用带边界的正则（km/h、percent）。
PERF_HINT = ("℃", "分钟", "秒", "毫秒", "百分比", "年", "个月", "天", "小时",
             "km/h", "km", "kg", "mAh", "Wh", "rpm", "N·m", "kW", "MW")
# 数值 + 单位的严格正则（ASCII 单位须带边界，避免命中普通英文单词）
_PERF_RE = re.compile(
    r"\d+\s*(℃|度|分钟|min|秒|s\b|km/h|km|kg|mAh|Wh|rpm|N·m|kW|MW|W\b|V\b|A\b|%)",
    re.I)
# 接口类线索
IFACE_HINT = ("接口", "通信", "协议", "信号", "总线", "CAN", "LIN", "UART",
              "SPI", "I2C", "OBD", "UDS", "API", "信号量", "针脚")

_SENT_SPLIT = re.compile(r"(?<=[。；！？\n])|(?<=[.;])\s+")


def _as_text(v):
    return str(v or "").strip()


def _looks_perf(s):
    return any(u in s for u in PERF_HINT) or bool(_PERF_RE.search(s))


def _looks_iface(s):
    return any(k in s for k in IFACE_HINT)


def _classify(s):
    """确定性分类：接口线索 > 性能线索 > 约束线索 > 默认功能。"""
    if _looks_iface(s):
        return "interface"
    if _looks_perf(s):
        return "performance"
    if re.search(r"(不得|禁止|必须|应|shall|must|shall not)", s):
        return "constraint"
    return "functional"


def _verifiability(s):
    """返回 (是否可验证, 原因)。不可验证的条目必须进 gaps，不许混进 items。

    ⚠️ 实测踩过：可验证动词表最初只含「能够/可以/应能/支持/实现/提供/完成」，
    结果「系统应通过 CAN 总线与整车控制器通信」被判**不可验证**扔进 gaps
    —— 但接口需求完全可验证（可通过总线测试验收）。
    ⇒ 动词表必须覆盖**接口/安全/约束类**的常见动作词。
    """
    reasons = []
    if any(f in s for f in FUZZY):
        reasons.append("含模糊词（" + "、".join(
            f for f in FUZZY if f in s) + "）")
    if re.search(r"(良好|优秀|快速|高效|灵活|方便)", s) and not _looks_perf(s):
        reasons.append("形容词无量化判据")
    # 可验证动作词：功能 + 接口 + 安全 + 约束四类
    if not re.search(r"[0-9０-９]", s) and not re.search(
            r"(能够|可以|应能|支持|实现|提供|完成|满足|符合|"
            r"通信|交互|传输|发送|接收|连接|集成|预留|"
            r"检测|监测|报警|告警|预警|保护|联锁|追溯|"
            r"不得|禁止|必须|应当)", s):
        reasons.append("无量化指标且无可验证动词")
    return (len(reasons) == 0, "；".join(reasons))


def _camel(name):
    """把中文/任意文本转成合法的 ASCII 标识符片段。"""
    parts = re.findall(r"[A-Za-z0-9]+", name or "")
    if parts:
        return "".join(p[:1].upper() + p[1:] for p in parts) or "Req"
    return "Req"


def itemize(text, domain="", source_ref="", project="EVTM"):
    """核心：自然语言需求 → (items, gaps, req_view_code, stats)"""
    raw = _as_text(text)
    items, gaps = [], []
    seen_text = {}
    used_names = set()          # ⚠️ 已用标识符集合 —— 防止中文名转ASCII 后撞名

    for seg in _SENT_SPLIT.split(raw):
        s = _as_text(seg)
        if len(s) < 4:                      # 太短，多半是标题或噪声
            continue
        ok, why = _verifiability(s)
        if not ok:
            gaps.append({"text": s, "reason": why, "need_clarification": True})
            continue
        if s in seen_text:                  # 完全重复
            gaps.append({"text": s, "reason": "与第 %d 条重复" % seen_text[s],
                         "need_clarification": False})
            continue
        cat = _classify(s)
        rid = "REQ-%s%03d" % (CAT_CODE[cat], len(items) + 1)
        # ⚠️ 标识符**必须唯一**。实测踩过：中文需求名转ASCII 后常只剩数字
        #   （「电池温差不超过 5℃」→ `Req5`、「热泵 COP…2.0」→ `Req2`）⇒ 多条同名 requirement。
        #   规则：`Req` + 语义片段；片段为空或已占用时，用 `reqId` 数字后缀兜底。
        base = _camel(s)[:48]
        name = ("Req" + base) if base and ("Req" + base) not in used_names else None
        if name is None:
            seq = len(items) + 1
            name = "Req%s_%02d" % (base if base else "Item", seq)
            while name in used_names:                # 极端情况：仍撞名则加后缀
                name += "x"
        used_names.add(name)
        seen_text[s] = len(items) + 1
        items.append({
            "id": rid,
            "text": s,
            "category": cat,
            "subject": "",                 # 由 N3 补（需要知道哪个 part 是主体）
            "source_ref": source_ref,
            "ambiguous": bool(re.search(r"(等|若干|某些|适当|尽量)", s)),
            "sysml_name": name,             # 需求视图里的 requirement 名（保证唯一）
        })

    code = _build_view(items, project)
    stats = {"sentences": len(items) + len(gaps), "items": len(items),
             "gaps": len(gaps),
             "by_category": {c: sum(1 for i in items if i["category"] == c)
                             for c in CATEGORIES}}
    return items, gaps, code, stats


def _esc(text):
    """doc /* */ 里不能出现 */ ，转义。"""
    return _as_text(text).replace("*/", "*/")


def _build_view(items, project):
    """生成需求视图骨架：package + stakeholder + requirement + refine 关系。

    ⚠️ **刻意不生成 satisfy** —— 此时无design 元素，生成必然报
       `Must be a valid feature`（实测）。satisfy 由 N2/N5 补。
    """
    L = []
    L.append("package %s_Requirements {" % project)
    L.append("  //需求视图骨架（由 requirement_itemize 生成）")
    L.append("  // 约定：satisfy 分配待design 元素确定后由 N2/N5 补充")
    L.append("  private import ScalarValues::*;")
    L.append("")
    # stakeholder（按条目 subject 聚合；空则给默认占位）
    subs = sorted({i["subject"] for i in items if i.get("subject")})
    if subs:
        L.append("  // ---------- 需求主体 ----------")
        for s in subs:
            L.append("  part def %s;" % _camel(s))
        L.append("")
    L.append("  // ---------- 需求条目 ----------")
    for i in items:
        nm = i["sysml_name"]
        L.append("  requirement %s {" % nm)
        L.append("    doc /* %s */" % _esc(i["text"]))
        L.append('    attribute reqId = "%s";' % i["id"])
        L.append('    attribute category = "%s";' % i["category"])
        L.append("  }")
    # 派生/细化关系：**只在有明确依据时生成**
    # ⚠️ 实测踩过：早期版本按「同 category 就细化」⇒ 6 条无 subject 的需求全挂到第1 条上，
    #   等于**凭空断言了 5 条不存在的派生关系**。
    #   正确口径：细化必须有依据 —— ① 同 subject（同主体下的性能指标细化于主需求）
    #                ② 文本显式含「细分/细化/其中/之一」等关系词。
    #   否则**不生成关系**，由 N3/N5 依建模判断补。
    REL_HINT = ("细分", "细化", "其中", "之一", "进一步", "子要求", "包括", "分为")
    by = {}
    for i in items:
        sub = i.get("subject") or ""
        if sub:                                # 有主体才可能构成细化
            by.setdefault(sub, []).append(i)
    rel = []
    for sub, lst in by.items():
        if len(lst) < 2:
            continue
        base = lst[0]
        for other in lst[1:]:
            if any(h in other["text"] for h in REL_HINT):
                rel.append((other, base))
    if rel:
        L.append("")
        L.append("  // ---------- 细化关系（:> = refines，有明确依据才生成）----------")
        for o, b in rel:
            L.append("  requirement %s :> %s;" % (o["sysml_name"], b["sysml_name"]))
    L.append("}")
    L.append("  // 注：细化关系仅在「同主体 + 关系词」时生成；"
             "无依据的关系请由 N3/N5 依建模判断补。")
    return "\n".join(L)


def _render(items, gaps, code, stats, domain):
    L = ["【需求条目化】确定性规则通道（无 LLM 参与）", ""]
    L.append(f"切分 {stats['sentences']} 句 → 条目 {stats['items']} 条 / "
             f"待澄清 {stats['gaps']} 条")
    dist = "、".join(f"{c}:{n}" for c, n in stats["by_category"].items() if n)
    L.append(f"分类分布：{dist or '（无）'}")
    L.append("")
    if items:
        L.append("条目清单：")
        for i in items:
            L.append(f"  {i['id']:12} [{i['category']:11}] {i['text'][:52]}")
    if gaps:
        L.append("")
        L.append("⚠ 待澄清（**不可直接建模**，需补量化判据）：")
        for g in gaps[:10]:
            L.append(f"  · {g['text'][:46]}")
            L.append(f"    原因：{g['reason']}")
        if len(gaps) > 10:
            L.append(f"  …… 另有 {len(gaps) - 10} 条")
    L.append("")
    L.append("需求视图骨架（已生成，可直接作为 N3 view_type=requirement 的基础）：")
    L.append("```sysml")
    L.append(code)
    L.append("```")
    L.append("")
    L.append("说明：")
    L.append("  · satisfy（需求→设计元素）**刻意未生成** —— 此时无 design 元素，"
             "生成会报 Must be a valid feature；由 N2/N5 补。")
    L.append("  · 需求项目的 subject（主体）需与架构对照，本工具留空交 N3 判定。")
    if domain:
        L.append(f"  · 领域：{domain}")
    return "\n".join(L)


def _exec(args):
    text = args.get("text") or args.get("raw_text")
    if not _as_text(text):
        return {"ok": False,
                "result": "参数 text 为空：请把待条目化的需求原文整段传入。"}
    domain = _as_text(args.get("domain"))
    source_ref = _as_text(args.get("source_ref"))
    project = _as_text(args.get("project")) or "EVTM"
    project = re.sub(r"[^A-Za-z0-9_]", "", project) or "EVTM"
    try:
        items, gaps, code, stats = itemize(str(text), domain, source_ref, project)
    except Exception as exc:                                       # noqa: BLE001
        return {"ok": False,
                "result": f"需求条目化失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表原文有问题 —— 请如实说明『未能条目化』。"}

    if not items and gaps:
        return {"ok": True,
                "result": ("【需求条目化】未能提取任何可验证条目，"
                           f"{len(gaps)} 句全部待澄清：\n"
                           + "\n".join(f"  · {g['text'][:46]} —— {g['reason']}"
                                       for g in gaps[:10])
                           + "\n\n⚠️ 不要跳过澄清直接建模 —— 不可验证的需求会产出无法验收的模型。"),
                "items": [], "gaps": gaps, "req_view_code": None,
                "stats": stats}

    return {
        "ok": True,
        "result": _render(items, gaps, code, stats, domain),
        "items": items,
        "gaps": gaps,
        "req_view_code": code,
        "stats": stats,
        "itemize_ratio": round(len(items) / max(1, stats["sentences"]), 3),
    }


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if name == "requirement_itemize":
        return _exec(args)
    return {"ok": False, "result": f"未知需求工具: {name}"}


if __name__ == "__main__":
    demo = ("EV 热管理系统需求：系统应支持 30 分钟低温启动，-20℃ 环境下 30 分钟内完成充电与放电准备；"
            "电池工作温度在 15~45℃ 之间；电池温差不超过 5℃；"
            "热管理总能耗不超过整车可用能量的 15%；"
            "热失控预警应至少提前 5 分钟触发保护动作并自动执行；"
            "系统应具有良好的用户体验；热泵 COP 在 -10℃ 时不低于 2.0；"
            "系统应通过 CAN 总线与整车控制器通信。")
    r = _exec({"text": demo, "domain": "thermal_mgmt", "project": "EVTM"})
    print(r["result"])
    print("\n--- 结构化字段 ---")
    for k in ("ok", "itemize_ratio"):
        print(f"  {k} = {r.get(k)}")
    print(f"  items = {len(r.get('items', []))} 条")
    print(f"  gaps  = {len(r.get('gaps', []))} 条")
    print(f"  stats = {r.get('stats')}")
