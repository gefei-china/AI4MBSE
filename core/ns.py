# -*- coding: utf-8 -*-
"""统一命名空间与 IRI 规则（P0-1 / P0-2 的单一事实来源）。

背景：此前工程存在三套互不相通的命名空间——
  · ontology_owl.py      : http://www.xingwang.mbse/ontology#（TBox）
  · graph_db.py          : urn:mbse:*（ABox，且类型谓词自造 urn:mbse:type）
  · ontology_reasoning.py: http://www.xingwang.mbse/inst#（实例，又一套）
后果：OWL 公理（domain/range/特性公理）对图库数据完全无效，推理机什么都推不出来。

本模块把 IRI 规则收敛到一处，graph_db / ontology_owl / ontology_reasoning 都必须引用它，
不再各自定义。局部名统一 percent-encoding（纯 ASCII），原因：
  · pyoxigraph / Jena 对含非 ASCII 的 IRIREF 态度不一致（见 graph_db 注释「中文 IRI 不能
    写在 <...> 中」），编码后可用普通 SPARQL 文本查询；
  · 保证「同一输入 → 同一 IRI」的幂等性。

局部名规则可升级（如改为 slug）：只要本模块一处修改，全工程同步生效。
"""
from urllib.parse import quote, unquote

# ── 命名空间 ──────────────────────────────────────────────────
BASE = "http://www.xingwang.mbse/"
NS_ONTOLOGY = BASE + "ontology#"   # 本体类 / 属性（TBox）
NS_ENT = BASE + "ent/"             # 实体实例（ABox）
NS_PRED_REL = BASE + "rel/"        # 对象属性（关系）
NS_PRED_PROP = BASE + "prop/"      # 数据属性
NS_META = BASE + "meta/"           # 元数据（溯源）
NS_GRAPH = BASE + "graph/"         # 命名图（按 branch）

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
RDF_TYPE = RDF_NS + "type"
RDFS_LABEL = RDFS_NS + "label"

# legacy（旧 urn: 前缀）：仅用于解析存量数据，不再写入
LEGACY_NS_ENT = "urn:mbse:ent:"
LEGACY_NS_PRED_TYPE = "urn:mbse:type"
LEGACY_NS_PRED_REL = "urn:mbse:rel:"
LEGACY_NS_PRED_PROP = "urn:mbse:prop:"
LEGACY_NS_META = "urn:mbse:meta:"
LEGACY_NS_GRAPH = "urn:mbse:graph:"

_UNRESERVED = "-._~"   # RFC 3986 非保留字符：编码时保持可读


def loc_key(name) -> str:
    """名称 → 稳定局部标识（非法字符归一为 `_`，保留中英文与数字）。"""
    import re
    return re.sub(r"[^A-Za-z0-9_一-龥]", "_", str(name or "").strip())


def local(s: str) -> str:
    """局部名 → IRI 安全片段（percent-encoding，纯 ASCII）。"""
    return quote(str(s or "").strip(), safe=_UNRESERVED)


def unlocal(s: str) -> str:
    """local() 的逆运算。"""
    try:
        return unquote(str(s or ""))
    except Exception:
        return str(s or "")


# ── IRI 构造 ──────────────────────────────────────────────────
def ent_uri(name, eid="") -> str:
    """实体 IRI：**只依赖稳定 id**（P0-2）。

    - 有 eid → `{NS_ENT}{enc(eid)}`：改名/重名都不变（推荐）
    - 无 eid → `{NS_ENT}{enc(loc_key(name))}`：兜底，名称变化会导致 IRI 变化
    """
    if eid:
        return NS_ENT + local(eid)
    return NS_ENT + local(loc_key(name))


def class_iri(name, base: str = NS_ONTOLOGY) -> str:
    """本体类 IRI（TBox）。base 允许按项目/领域包覆盖（如领域包自带命名空间）。"""
    return (base or NS_ONTOLOGY) + local(loc_key(name))


def class_uri(name) -> str:
    """本体类 IRI（默认命名空间）。与 ontology_owl 导出的 owl:Class 必须同源。"""
    return class_iri(name)


def pred_uri(predicate: str, object_type: str = "literal") -> str:
    """谓词 IRI：rdf:type / rdfs:label / rel:{p}（对象属性）/ prop:{p}（数据属性）。"""
    p = str(predicate or "").strip()
    if p in ("a", "type", "rdf:type", RDF_TYPE):
        return RDF_TYPE
    if p in ("label", "rdfs:label", RDFS_LABEL, "名称"):
        return RDFS_LABEL
    if object_type == "entity":
        return NS_PRED_REL + local(p)
    return NS_PRED_PROP + local(p)


def graph_uri(branch: str) -> str:
    return NS_GRAPH + local(str(branch or "dev").strip() or "dev")


# ── IRI 解析（兼容 legacy 前缀）────────────────────────────────
def parse_ent_uri(uri: str) -> tuple:
    """实体 IRI → (name, id)。新版 IRI 不含名称，name 为空（需从 rdfs:label 取）。"""
    u = str(uri or "")
    if u.startswith(NS_ENT):
        return "", unlocal(u[len(NS_ENT):])
    if u.startswith(LEGACY_NS_ENT):
        rest = u[len(LEGACY_NS_ENT):]
        if ":" in rest:
            name, eid = rest.rsplit(":", 1)
            return name, eid
        return rest, ""
    return "", ""


def rel_name(pred_iri: str) -> str:
    """谓词 IRI → 关系/属性名。"""
    p = str(pred_iri or "")
    for ns in (NS_PRED_REL, LEGACY_NS_PRED_REL):
        if p.startswith(ns):
            return unlocal(p[len(ns):])
    for ns in (NS_PRED_PROP, LEGACY_NS_PRED_PROP):
        if p.startswith(ns):
            return unlocal(p[len(ns):])
    if p in (RDF_TYPE, LEGACY_NS_PRED_TYPE):
        return "type"
    if p == RDFS_LABEL:
        return "label"
    return p


def graph_name(graph_iri: str) -> str:
    """命名图 IRI → 分支名。"""
    g = str(graph_iri or "")
    for ns in (NS_GRAPH, LEGACY_NS_GRAPH):
        if g.startswith(ns):
            return unlocal(g[len(ns):])
    return g
