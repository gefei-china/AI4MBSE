# -*- coding: utf-8 -*-
"""core/ontology_tlo.py —— 上层本体（TLO）锚点词表：**BFO 轻量子集**。

## 为什么需要它（2026-11-15，决策 D1=A）

本工程此前的本体**没有上层锚点**：52 个领域类型直接平铺，靠 `parent_id` 互相挂。
后果不是"不好看"，而是**语义不可判**：

  · `部件` 是「在时间中保持同一性的对象」，`功能` 是「在时间中展开的过程」——
    两者范畴不同，但平铺后它们只是两个同级的类，`disjointWith` 只能靠人工逐个声明；
  · 每次新增场景（如"验证活动"）都可能与旧声明冲突，改一处要重新审一遍全表
    ⇒ 这就是「本体越用越乱」的机制。上层本体提供**稳定的范畴锚点**，
    让"对象 vs 过程 vs 质量"这类互斥由范畴本身保证，而不是逐对声明。

## 为什么是「轻量子集」而不是完整 BFO

完整 BFO（ISO/IEC 21838-2）有 30+ 类。本工程当前 28 个实体类型，
把它们强行摊到 30 个 BFO 类上是**过度建模**（多数类永远空着，反而增加维护面）。
故只取 **6 个判别性最强**的类 + 2 个中间类，足以覆盖 MBSE 的全部常见范畴：

| BFO 类 | 范畴 | MBSE 典型对应 |
|---|---|---|
| Entity | 最普遍 | （根） |
| Continuant | 在时间中保持同一性 | — |
| Occurrent | 在时间中展开 | — |
| Object | 独立的对象 | 部件、分系统、星座、设备 |
| Process | 过程/活动 | 功能、验证活动、测试 |
| Quality | 可测量的属性 | 属性类型（带宽、功率…） |
| InformationContentEntity | 信息内容实体 | **需求、规范、验证用例** |
| Role | 角色（可选的实现依赖） | （预留，当前不强制） |

> ⚠️ **命名空间诚实声明**：下表 IRI 采用 OBO Foundry 通行的 BFO 2.0 / ISO 21838-2 标识符。
> 用于**内部锚点**（本工程只存 local name）时是安全的；若要对外互操作（导入 Protégé +
> 与外部 BFO 本体对齐），**必须**先对照 OBO 官方发布件核对 IRI —— 本模块把它集中成
> 一处常量就是为了让核对/修正只需改一个地方。

## 存储口径（为什么不是 DB 里的一批行）

TLO 类**不写进 `ontology_types`**。理由：那张表是"领域类型"的集合，用户的实例创建、
关系定义域候选、图谱节点都读它 —— 混入 BFO 的 8 个类会污染候选列表，且这些类
永远不会有实例（它们是范畴，不是领域概念）。

所以：`ontology_types.tlo_anchor` 存 **local name**（如 `Object` / `Process`），
本模块是唯一词表。**有效锚点 = 自身锚点，否则沿 parent 链继承**（见 `effective_anchor`）。
"""
from __future__ import annotations

__all__ = [
    "TLO_CLASSES", "TLO_LOCALS", "TLO_ROOT", "BFO_NS",
    "is_tlo", "tlo_meta", "tlo_iri", "parent_of", "ancestors_of",
    "effective_anchor", "anchor_chain", "missing_anchor",
    "suggest_anchor", "SUGGEST_RULES", "options_for_ui",
]

#: OBO 的 BFO IRI 前缀（BFO 2.0 / ISO 21838-2 通行写法）
BFO_NS = "http://purl.obolibrary.org/obo/"

#: 轻量子集定义。parent 为 None 表示本子集的根。
#: `mbse_hint` 用于前端提示与 `suggest_anchor` 的**可读依据**（不是自动赋值）。
TLO_CLASSES = (
    {"local": "Entity", "label": "实体", "iri": BFO_NS + "BFO_0000001", "parent": None,
     "note": "最普遍范畴（根）。领域类型最终都应能归结到它的某个子范畴。"},
    {"local": "Continuant", "label": "持续体", "iri": BFO_NS + "BFO_0000002", "parent": "Entity",
     "note": "在时间中保持同一性的事物（对象、质量都属此类）。"},
    {"local": "Occurrent", "label": "发生体", "iri": BFO_NS + "BFO_0000003", "parent": "Entity",
     "note": "在时间中展开的事物（过程、事件）。"},
    {"local": "Object", "label": "对象", "iri": BFO_NS + "BFO_0000030", "parent": "Continuant",
     "mbse_hint": "部件、分系统、设备、星座、地面段、终端等「有边界、能独立存在」的东西"},
    {"local": "Quality", "label": "质量", "iri": BFO_NS + "BFO_0000019", "parent": "Continuant",
     "mbse_hint": "可测量、依附于载体的特征（带宽、功率、寿命）——对应「属性类型」而非实体"},
    {"local": "Process", "label": "过程", "iri": BFO_NS + "BFO_0000015", "parent": "Occurrent",
     "mbse_hint": "功能、验证活动、测试、分析等「在时间里发生的事」"},
    {"local": "InformationContentEntity", "label": "信息内容实体",
     "iri": BFO_NS + "IAO_0000033", "parent": "Continuant",
     "mbse_hint": "需求、规范、验证用例、模型等「关于某事物的信息载体」（BFO 里属依赖连续体）"},
    {"local": "Role", "label": "角色", "iri": BFO_NS + "BFO_0000023", "parent": "Continuant",
     "mbse_hint": "预留：某对象在特定情境下扮演的外部角色（当前工程未强制使用）"},
)

#: local name 集合（校验/归一用）
TLO_LOCALS = frozenset(c["local"] for c in TLO_CLASSES)

#: 本子集的根
TLO_ROOT = "Entity"

#: local → 定义
_BY_LOCAL = {c["local"]: c for c in TLO_CLASSES}

#: 大小写不敏感别名（容忍前端/导入时的写法差异：object / bfo:Object / BFO_0000030）
_ALIASES = {}
for _c in TLO_CLASSES:
    _ALIASES[_c["local"]] = _c["local"]
    _ALIASES[_c["local"].lower()] = _c["local"]
    _ALIASES[_c["label"]] = _c["local"]
    _ALIASES[_c["iri"]] = _c["local"]
    _ALIASES[_c["iri"].rsplit("/", 1)[-1]] = _c["local"]      # BFO_0000030
    _ALIASES["bfo:" + _c["local"]] = _c["local"]              # bfo:Object
del _c


def normalize(name):
    """任意写法 → local name；未识别返回空串（**不**做猜测兜底）。

    ⚠️ 刻意不兜底成 'Entity'：把写错的锚点静默归一到根，会掩盖录入错误，
    让「所有类型都锚住了」的假象成立（本仓已有"静默兜底=把错误变成不可见"的前车）。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    return _ALIASES.get(s, _ALIASES.get(s.lower(), ""))


def is_tlo(name) -> bool:
    """是否为合法的 TLO 锚点名（任意可识别写法）。"""
    return bool(normalize(name))


def tlo_meta(name):
    """锚点名 → 完整定义（归一后）；未识别返回 None。"""
    n = normalize(name)
    return dict(_BY_LOCAL[n]) if n else None


def tlo_iri(name) -> str:
    """锚点名 → BFO IRI；未识别返回 ''（不编造 IRI）。"""
    n = normalize(name)
    return _BY_LOCAL[n]["iri"] if n else ""


def parent_of(name):
    """锚点名的父锚点名（根返回 None）。"""
    n = normalize(name)
    return _BY_LOCAL[n]["parent"] if n else None


def ancestors_of(name) -> list:
    """从自身到根的锚点链（不含自身则为父链）。未识别返回 []。"""
    out, cur, guard = [], normalize(name), 0
    while cur and guard < 20:
        out.append(cur)
        cur = _BY_LOCAL.get(cur, {}).get("parent")
        guard += 1
    return out


def options_for_ui() -> list:
    """前端下拉选项（含 label / note / 父级，便于分组缩进渲染）。"""
    return [{"local": c["local"], "label": c["label"], "iri": c["iri"],
             "parent": c["parent"], "note": c.get("note", ""),
             "mbse_hint": c.get("mbse_hint", "")} for c in TLO_CLASSES]


# ══════════════════════════════════════════════════════════════════════════
# 有效锚点：自身 → 沿 parent 链继承
# ══════════════════════════════════════════════════════════════════════════
def effective_anchor(row, by_id=None, max_hop: int = 30):
    """求某类型的**有效** TLO 锚点（自身优先，否则沿 parent_id 链上溯继承）。

    :param row: 一行（需支持 row["tlo_anchor"] / row["parent_id"] / row["id"]，
                或 dict）
    :param by_id: {str(id): row} 字典，供上溯；None 时只查自身。
    :returns: (anchor_local, source) —— source ∈ {"self", "inherited", ""}；
              无锚点时 anchor_local 为 ''。
    """
    def _g(r, k):
        try:
            v = r[k]
        except (KeyError, IndexError, TypeError):
            v = None
        return v

    own = normalize(_g(row, "tlo_anchor"))
    if own:
        return own, "self"
    if not by_id:
        return "", ""
    seen, cur, hop = set(), _g(row, "parent_id"), 0
    while cur and hop < max_hop:
        key = str(cur)
        if key in seen:                 # 防御循环继承（体检已有 cycle 规则，这里不重复报）
            break
        seen.add(key)
        p = by_id.get(key)
        if p is None:
            break
        a = normalize(_g(p, "tlo_anchor"))
        if a:
            return a, "inherited"
        cur = _g(p, "parent_id")
        hop += 1
    return "", ""


def anchor_chain(row, by_id=None) -> list:
    """从自身到根的局部锚点链（用于前端展示「继承自 X」）。"""
    out = []
    own = normalize(row["tlo_anchor"] if "tlo_anchor" in row.keys() else None) \
        if hasattr(row, "keys") else normalize((row or {}).get("tlo_anchor"))
    if own:
        out.append(own)
    if not by_id:
        return out
    seen, cur, hop = set(), (row["parent_id"] if hasattr(row, "keys") else (row or {}).get("parent_id")), 0
    while cur and hop < 30:
        key = str(cur)
        if key in seen:
            break
        seen.add(key)
        p = by_id.get(key)
        if p is None:
            break
        a = normalize(p["tlo_anchor"] if hasattr(p, "keys") else p.get("tlo_anchor"))
        if a:
            out.append(a)
            break                      # 继承只取最近的一个祖先锚点
        cur = p["parent_id"] if hasattr(p, "keys") else p.get("parent_id")
        hop += 1
    return out


def missing_anchor(row, by_id=None) -> bool:
    """该类型是否「无有效 TLO 锚点」（体检规则 missing_tlo_anchor 的判据）。"""
    return not effective_anchor(row, by_id)[0]


# ══════════════════════════════════════════════════════════════════════════
# 建议（**只是建议**，绝不自动写入）
# ══════════════════════════════════════════════════════════════════════════
#: 名称关键词 → 建议锚点。刻意保持**短小且可解释**：命中就给建议 + 命中词，
#: 未命中就不建议（宁可让人自己选，也不瞎猜）。
#:
#: 为什么用规则而不是 LLM：锚点是**结构判定**，一旦错了会污染整条继承链
#: （下游 `disjointWith`、SHACL 形状全部跟着错）。规则可审计、可复现；
#: LLM 建议只能作为"另一个候选"由人确认 —— 本工程已走 `ontology_drafts` 确认流。
SUGGEST_RULES = (
    # 过程类：功能、验证活动、测试、分析
    ("Process", ("功能", "活动", "过程", "测试", "验证用例", "验证活动", "分析",
                 "function", "process", "activity", "test", "verification")),
    # 信息内容类：需求、规范、用例、模型、文档
    ("InformationContentEntity", ("需求", "规范", "用例", "指标", "文档", "模型",
                                  "requirement", "spec", "use case", "model", "document")),
    # 质量类：属性类型（可测量）
    ("Quality", ("属性", "参数", "指标值", "property", "attribute", "quality")),
    # 对象类：部件、分系统、设备、系统、站、段、终端、载荷
    ("Object", ("部件", "分系统", "子系统", "系统", "设备", "装置", "站", "段", "终端",
                "载荷", "星座", "卫星", "平台", "产品", "component", "subsystem",
                "system", "device", "equipment", "terminal", "payload", "platform")),
)


def suggest_anchor(name, kind="entity", description=""):
    """按名称/种类给出**建议**锚点。

    :returns: dict(anchor=local 或 '', 命中词=str, 依据=str, 需确认=True)
              未命中返回 anchor=''（不硬猜）。
    """
    nm = str(name or "")
    desc = str(description or "")
    if kind == "attribute":
        # 属性类型天然是 Quality（可测量特征）—— 这是范畴层面的必然，不是猜测
        return {"anchor": "Quality", "hit": "", "basis": "type_kind=attribute ⇒ 可测量特征",
                "need_confirm": True}
    hit = ""
    for anchor, words in SUGGEST_RULES:
        for w in words:
            if w.lower() in nm.lower() or (desc and w.lower() in desc.lower()):
                hit = w
                return {"anchor": anchor, "hit": hit,
                        "basis": f"名称/描述命中「{hit}」", "need_confirm": True}
    return {"anchor": "", "hit": "", "basis": "未命中任何规则，请人工选择", "need_confirm": True}
