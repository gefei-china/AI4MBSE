"""O-2：本体 ↔ OWL 导入导出（客户 P13-5/10，对齐 Neo4j n10s 映射路径）。

映射（与行业标准一致）：
- entity 类型  → owl:Class
- relation 类型 → owl:ObjectProperty（rdfs:domain=源实体类型，rdfs:range=目标实体类型）
- attribute 类型 → owl:DatatypeProperty
- required/unique 约束 → rdfs:comment 保留（OWL 无直接等价，注释承载）

零依赖：仅用 xml.etree.ElementTree 生成/解析 RDF/XML。
"""
import json
import re
import xml.etree.ElementTree as ET

from core import typevocab


# ── 自定义公理编译（2026-09-07 Q1 做实）：此前 axioms 仅序列化为注释（# axiom: ... /
#    XML Comment），Protégé/推理器零语义。现识别内部限制语法编译为真 OWL 公理：
#    `属性 some "值"` → owl:someValuesFrom；`属性 exactly 1 "值"` → owl:cardinality 1。
#    未识别语法的表达式保留原文注释（不丢弃、不猜测）。
_AXIOM_RE = re.compile(r'^\s*:?\s*([^\s"].*?)\s+(some|exactly)\s+(?:1\s+)?["\u201c](.+?)["\u201d]\s*$')


def _compile_axiom(expr):
    """解析公理表达式 → (属性名, 'some'|'exactly', 字面值)；未识别返回 None。"""
    m = _AXIOM_RE.match(expr or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2), m.group(3)

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XSD = "http://www.w3.org/2001/XMLSchema#"
# P3-①（2026-09-11）W3C Time Ontology 命名空间
TIME = "http://www.w3.org/2006/time#"
XSD_DATETIME = XSD + "dateTime"

# R2a（2026-08-30）：关系推理性质（constraints.characteristics）→ OWL 属性类型
OWL_CHAR_TYPE = {
    "transitive": "owl:TransitiveProperty",
    "symmetric": "owl:SymmetricProperty",
    "asymmetric": "owl:AsymmetricProperty",
    "reflexive": "owl:ReflexiveProperty",
    "functional": "owl:FunctionalProperty",
    "inverse_functional": "owl:InverseFunctionalProperty",
}
NS = {"rdf": RDF, "rdfs": RDFS, "owl": OWL, "xsd": XSD, "time": TIME}
NSMAP = {
    "xmlns:rdf": RDF,
    "xmlns:rdfs": RDFS,
    "xmlns:owl": OWL,
    "xmlns:xsd": XSD,
    "xmlns:time": TIME,
}
# P0-1：与图库共享同一命名空间规则（core.ns 为单一事实来源）。
# 此前本体用 http://.../ontology#、图库用 urn:mbse:*，OWL 公理对图库数据完全无效。
from core import ns as _ns  # noqa: E402

BASE = _ns.NS_ONTOLOGY


def _q(tag):
    """构造带命名空间的 Element 标签。"""
    return f"{{{NS[tag]}}}{tag}"


def _local(uri):
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _slug(s):
    """类/属性 IRI 的局部名（P0-1：与 graph_db.class_uri 同源，规则见 core.ns）。

    历史实现为「非法字符 → _」并保留中文；现统一为 percent-encoding，
    保证本体导出的 owl:Class IRI 与图库 rdf:type 指向的类 IRI 逐字节一致。
    """
    return _ns.local(_ns.loc_key(s))


def _build_name2iri(rows, base=BASE):
    """P0-1：类型名 → 实体 IRI 映射（优先 iri 列，回退 base+slug(name)）。rows 为类型行列表。"""
    name2iri = {}
    for r in rows:
        try:
            v = r["iri"] if "iri" in r.keys() else ""
        except Exception:
            v = ""
        name2iri[r["name"]] = v or (base + _slug(r["name"]))
    return name2iri


def _register_ns():
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    ET.register_namespace("", BASE)


def _export_warnings(conn) -> list:
    """方案 A3（2026-08-29）：导出前完整性扫描——悬空 parent / 对象属性 dom-range 指向不存在类型。

    走查 P0-2：悬空引用此前在 TTL/OWL 导出时静默丢失（subClassOf 行无声消失）。
    现导出函数会收集警告并：① 行内输出 WARNING 注释 ② 汇总进 X-Ont-Warnings 响应头。
    """
    warns = []
    rows = conn.execute("SELECT id, name, type_kind, parent_id, constraints FROM ontology_types").fetchall()
    by_id = {str(r["id"]): r for r in rows}
    ent_names = {r["name"] for r in rows if r["type_kind"] == "entity"}
    for r in rows:
        if r["parent_id"] and str(r["parent_id"]) not in by_id:
            warns.append(f"悬空父类：{r['name']} 的 parent_id={r['parent_id']} 不存在（subClassOf 将不导出）")
    for r in rows:
        if r["type_kind"] != "relation":
            continue
        try:
            c = __import__("json").loads(r["constraints"] or "{}")
        except Exception:
            c = {}
        bad = sorted({c.get(k) for k in ("domain", "range")
                      if c.get(k) and c.get(k) not in ent_names})
        if bad:
            warns.append(f"对象属性 {r['name']} 的定义域/值域指向不存在的实体类型: {'、'.join(bad)}")
    return warns


def _parent_name(conn, r, rows=None) -> str:
    """P0-1：取父类型名（parent_id → name），无父返回空串。

    2026-09-02 快照消费：rows 传入已发布快照行时从快照解析（避免查活表）。
    """
    if not r["parent_id"]:
        return ""
    if rows is not None:
        for x in rows:
            if x["id"] == r["parent_id"]:
                return x["name"]
        return ""
    row = conn.execute("SELECT name FROM ontology_types WHERE id=?", (r["parent_id"],)).fetchone()
    return row["name"] if row else ""


def to_owl(conn, ontology_name: str = "XingWangDomain", rows=None):
    """ontology_types → OWL/RDF-XML 字符串。方案 A3：返回 (xml, warnings)。

    2026-09-02 快照消费：rows 传入已发布快照行时导出该版本（None=当前表）。
    """
    _register_ns()
    _warns = _export_warnings(conn)
    if rows is None:
        rows = conn.execute("SELECT * FROM ontology_types").fetchall()
    root = ET.Element(f"{{{RDF}}}RDF")
    ont = ET.SubElement(root, f"{{{OWL}}}Ontology")
    ET.SubElement(ont, f"{{{OWL}}}imports", **{f"{{{RDF}}}resource": f"{BASE}{ontology_name}"})

    # P0-1：实体 IRI 主轴——主体/引用 URI 均优先用 iri 列，回退 name 派生
    name2iri = _build_name2iri(rows, base=BASE)
    def _uri(name):
        return name2iri.get(name) or (BASE + _slug(name))

    for r in rows:
        name = r["name"]
        kind = r["type_kind"]
        try:
            cons = __import__("json").loads(r["constraints"] or "{}")
        except Exception:
            cons = {}
        # P0-1：实体 URI 用 IRI 列
        uri = _uri(name)
        if kind == "entity":
            cls = ET.SubElement(root, f"{{{OWL}}}Class", **{f"{{{RDF}}}about": uri})
            ET.SubElement(cls, f"{{{RDFS}}}label").text = str(name)
            # P0-1：子类（subClassOf 层级）；方案 A3：悬空时行内 XML 注释（不再静默丢失）
            pname = _parent_name(conn, r, rows)
            if pname:
                ET.SubElement(cls, f"{{{RDFS}}}subClassOf",
                              **{f"{{{RDF}}}resource": _uri(pname)})
            elif r["parent_id"]:
                cls.append(ET.Comment(f" WARNING: dangling parent (id={r['parent_id']}) — subClassOf omitted "))
            # 方案 C（2026-08-29）：公理双写——owl:Restriction（required=minCardinality 1 / unique=cardinality 1）
            for k in cons.get("required", []):
                restr = ET.SubElement(cls, f"{{{RDFS}}}subClassOf")
                bnode = ET.SubElement(restr, f"{{{OWL}}}Restriction")
                ET.SubElement(bnode, f"{{{OWL}}}onProperty",
                              **{f"{{{RDF}}}resource": _uri(str(k))})
                ET.SubElement(bnode, f"{{{OWL}}}minQualifiedCardinality").text = "1"
            for k in cons.get("unique", []):
                restr = ET.SubElement(cls, f"{{{RDFS}}}subClassOf")
                bnode = ET.SubElement(restr, f"{{{OWL}}}Restriction")
                ET.SubElement(bnode, f"{{{OWL}}}onProperty",
                              **{f"{{{RDF}}}resource": _uri(str(k))})
                ET.SubElement(bnode, f"{{{OWL}}}cardinality").text = "1"
            # B/C（2026-08-30）：等价类 / 互斥 / 自定义公理
            for x in cons.get("equivalent_to", []):
                cls.append(ET.Comment(f" equivalentTo: {x} "))
            for x in cons.get("disjoint_with", []):
                ET.SubElement(cls, f"{{{OWL}}}disjointWith",
                              **{f"{{{RDF}}}resource": _uri(str(x))})
            # Q1 做实（2026-09-07）：可编译模式 → 真 owl:Restriction 公理；未识别 → 注释保留原文
            for x in cons.get("axioms", []):
                _ax = _compile_axiom(x)
                if _ax:
                    _prop, _mode, _lit = _ax
                    restr = ET.SubElement(cls, f"{{{RDFS}}}subClassOf")
                    bnode = ET.SubElement(restr, f"{{{OWL}}}Restriction")
                    ET.SubElement(bnode, f"{{{OWL}}}onProperty",
                                  **{f"{{{RDF}}}resource": _uri(_prop)})
                    if _mode == "some":
                        ET.SubElement(bnode, f"{{{OWL}}}someValuesFrom").text = _lit
                    else:
                        ET.SubElement(bnode, f"{{{OWL}}}cardinality").text = "1"
                else:
                    cls.append(ET.Comment(f" axiom(未编译，保留原文): {x} "))
        elif kind == "relation":
            prop = ET.SubElement(root, f"{{{OWL}}}ObjectProperty", **{f"{{{RDF}}}about": uri})
            ET.SubElement(prop, f"{{{RDFS}}}label").text = str(name)
            allowed = cons.get("allowed_values") or {}
            srcs = allowed.get("src", [])
            tgts = allowed.get("tgt", [])
            if isinstance(srcs, str):
                srcs = [srcs]
            if isinstance(tgts, str):
                tgts = [tgts]
            for stype in srcs:
                ET.SubElement(prop, f"{{{RDFS}}}domain",
                              **{f"{{{RDF}}}resource": _uri(str(stype))})
            for ttype in tgts:
                ET.SubElement(prop, f"{{{RDFS}}}range",
                              **{f"{{{RDF}}}resource": _uri(str(ttype))})
            if cons.get("cardinality") == "1" or "functional" in (cons.get("characteristics") or []):
                ET.SubElement(prop, f"{{{RDF}}}type",
                              **{f"{{{RDF}}}resource": OWL + "FunctionalProperty"})
            for _ch in cons.get("characteristics", []):
                _owl_uri = OWL_CHAR_TYPE.get(_ch)
                if _owl_uri and _ch != "functional":
                    ET.SubElement(prop, f"{{{RDF}}}type",
                                  **{f"{{{RDF}}}resource": _owl_uri.replace("owl:", OWL)})
            for x in cons.get("equivalent_to", []):
                prop.append(ET.Comment(f" equivalentTo: {x} "))
            for x in cons.get("disjoint_with", []):
                ET.SubElement(prop, f"{{{OWL}}}disjointWith",
                              **{f"{{{RDF}}}resource": _uri(str(x))})
        elif kind == "attribute":
            prop = ET.SubElement(root, f"{{{OWL}}}DatatypeProperty", **{f"{{{RDF}}}about": uri})
            ET.SubElement(prop, f"{{{RDFS}}}label").text = str(name)
            # P0-1：属性类型也支持层级（父属性）
            pname = _parent_name(conn, r, rows)
            if pname:
                ET.SubElement(prop, f"{{{RDFS}}}subPropertyOf",
                              **{f"{{{RDF}}}resource": _uri(pname)})
            # B/C（2026-08-30）：Domain 绑定 / XSD Range / Functional
            for c in cons.get("domain_classes", []):
                ET.SubElement(prop, f"{{{RDFS}}}domain",
                              **{f"{{{RDF}}}resource": _uri(str(c))})
            # P1-8：xsd 类型走 typevocab 词表（兼容 xsd:*/int/decimal 等历史写法；
            # 声明位置兼容 constraints.xsd_type 与 properties.type 两种历史写法）
            _props_obj = r["properties"] if "properties" in r.keys() else {}
            try:
                _props_obj = json.loads(_props_obj or "{}") if isinstance(_props_obj, str) else (_props_obj or {})
            except Exception:
                _props_obj = {}
            declared_t = cons.get("xsd_type") or (_props_obj.get("type") if isinstance(_props_obj, dict) else None)
            xsd_t = typevocab.xsd_of(declared_t)
            if xsd_t:
                ET.SubElement(prop, f"{{{RDFS}}}range",
                              **{f"{{{RDF}}}resource": xsd_t})
            if cons.get("cardinality") == "1" or "functional" in (cons.get("characteristics") or []):
                ET.SubElement(prop, f"{{{RDF}}}type",
                              **{f"{{{RDF}}}resource": OWL + "FunctionalProperty"})
            for _ch in cons.get("characteristics", []):
                _owl_uri = OWL_CHAR_TYPE.get(_ch)
                if _owl_uri and _ch != "functional":
                    ET.SubElement(prop, f"{{{RDF}}}type",
                                  **{f"{{{RDF}}}resource": _owl_uri.replace("owl:", OWL)})
        # 约束注释 + P0（2026-09-08）注释导出补全：description → rdfs:comment 首段（此前右栏注释不进导出）；
        # profile_source/ref → rdfs:seeAlso；deprecated → owl:deprecated true + replaced_by 注记（OWL 标准弃用语义）
        tgt = prop if kind != "entity" else cls
        note = []
        for k in cons.get("required", []):
            note.append(f"required:{k}")
        for k in cons.get("unique", []):
            note.append(f"unique:{k}")
        _desc = (r["description"] if "description" in r.keys() else "") or ""
        if _desc:
            note.insert(0, _desc)
        _rb = (r["replaced_by"] if "replaced_by" in r.keys() else "") or ""
        if _rb:
            note.append(f"replaced_by:{_rb}")
        if note:
            ET.SubElement(tgt, f"{{{RDFS}}}comment").text = "; ".join(note)
        _psrc = (r["profile_source"] if "profile_source" in r.keys() else "") or ""
        _pref = (r["profile_ref"] if "profile_ref" in r.keys() else "") or ""
        if _psrc or _pref:
            ET.SubElement(tgt, f"{{{RDFS}}}seeAlso").text = _psrc + (" :: " if _psrc and _pref else "") + _pref
        _st = (r["status"] if "status" in r.keys() else "") or ""
        if _st == "deprecated":
            ET.SubElement(tgt, f"{{{OWL}}}deprecated").text = "true"
    # P3-①（2026-09-11）W3C Time Ontology 引用：本体层声明 time 命名空间使用，
    # 实体级 time:Interval 关系由 services/sparql_sync.py 的 build_entities_ttl() 输出。
    ET.SubElement(ont, f"{{{RDFS}}}comment").text = (
        "P3-① 引用 W3C Time Ontology (http://www.w3.org/2006/time#)；"
        "实体时态属性 validFrom/validTo 映射为 time:hasTime/time:Interval。"
    )
    return ET.tostring(root, encoding="unicode"), _warns


def to_turtle(conn, ontology_name: str = "XingWangDomain", rows=None):
    """ontology_types → W3C Turtle 序列化（RDF 三元组的可读文本语法）。

    OWL 是构建在 RDF 之上的本体语言；Turtle 与 RDF/XML 是 RDF 的两种
    等价序列化语法（W3C 标准），表达同一套 OWL 语义。
    方案 A3：返回 (text, warnings)——悬空引用行内 WARNING 注释 + 汇总警告列表。
    2026-09-02 快照消费：rows 传入已发布快照行时导出该版本（None=当前表）。
    """
    import json as _json
    _warns = _export_warnings(conn)
    if rows is None:
        rows = conn.execute("SELECT * FROM ontology_types").fetchall()
    L = []
    L.append("@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .")
    L.append("@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .")
    L.append("@prefix owl: <http://www.w3.org/2002/07/owl#> .")
    L.append("@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .")
    # P0-1：namespace 动态读 ontology_meta，IRI 主轴（CURIE 尾段用真实 iri，跨 namespace 回退 <完整iri>）
    try:
        from ontology_semantics import get_ontology_meta
        _ns = get_ontology_meta(conn).get("namespace") or BASE
    except Exception:
        _ns = BASE
    L.append(f"@prefix : <{_ns}> .")
    L.append("")
    L.append(f":{ontology_name} a owl:Ontology .")
    L.append("")

    _name2iri = _build_name2iri(rows, base=_ns)
    def _uri(name):
        iri = _name2iri.get(name) or (_ns + _slug(name))
        if iri.startswith(_ns):
            return ":" + iri[len(_ns):]
        return f"<{iri}>"

    for r in rows:
        name = r["name"]
        kind = r["type_kind"]
        try:
            cons = _json.loads(r["constraints"] or "{}")
        except Exception:
            cons = {}
        u = _uri(name)
        if kind == "entity":
            L.append(f"{u} a owl:Class ;")
            L.append(f'    rdfs:label "{name}"')
            # P0-1：子类（subClassOf 层级）；方案 A3：悬空时行内 WARNING（不再静默丢失）
            pname = _parent_name(conn, r, rows)
            if pname:
                L[-1] += " ;"
                L.append(f"    rdfs:subClassOf {_uri(pname)} .")
            else:
                L[-1] += " ."
                if r["parent_id"]:
                    L.append(f"# WARNING: dangling parent (id={r['parent_id']}) for {u} — subClassOf 省略")
            # Q1 做实（2026-09-07）：可编译模式 → rdfs:subClassOf Restriction 真公理；未识别 → 注释
            for x in cons.get("axioms", []):
                _ax = _compile_axiom(x)
                if _ax:
                    _prop, _mode, _lit = _ax
                    _restr = (f"[ rdf:type owl:Restriction ; owl:onProperty {_uri(_prop)} ; "
                              + (f'owl:someValuesFrom "{_lit}" ]' if _mode == "some"
                                 else "owl:cardinality 1 ]"))
                    L.append(f"{u} rdfs:subClassOf {_restr} .")
                else:
                    L.append(f"# axiom(未编译，保留原文): {x}")
        elif kind == "relation":
            L.append(f"{u} a owl:ObjectProperty ;")
            L.append(f'    rdfs:label "{name}"')
            allowed = cons.get("allowed_values") or {}
            srcs = allowed.get("src", [])
            tgts = allowed.get("tgt", [])
            if isinstance(srcs, str):
                srcs = [srcs]
            if isinstance(tgts, str):
                tgts = [tgts]
            parts = [f"    rdfs:domain {_uri(stype)}" for stype in srcs]
            parts += [f"    rdfs:range {_uri(ttype)}" for ttype in tgts]
            if parts:
                # label 行以分号续接，domain/range 间分号分隔，最后句点结尾（W3C Turtle 语法）
                L[-1] += " ;"
                L.append(" ;".join(parts) + " .")
            else:
                L[-1] += " ."
            if cons.get("cardinality") == "1" or "functional" in (cons.get("characteristics") or []):
                L.append(f"{u} rdf:type owl:FunctionalProperty .")
            for _ch in cons.get("characteristics", []):
                if _ch in OWL_CHAR_TYPE and _ch != "functional":
                    L.append(f"{u} rdf:type {OWL_CHAR_TYPE[_ch]} .")
            for x in cons.get("disjoint_with", []):
                L.append(f"{u} owl:disjointWith {_uri(x)} .")
            for x in cons.get("equivalent_to", []):
                L.append(f"# equivalentTo: {x}")
        elif kind == "attribute":
            L.append(f"{u} a owl:DatatypeProperty ;")
            L.append(f'    rdfs:label "{name}"')
            # P1-2 修复（2026-09-06）：subPropertyOf 与 domain/range 必须合并到
            # 同一条语句后再统一收尾。原先两个独立 if 各自续接，第二个块在语句
            # 已以 " ." 收尾后再加 " ;" → 产出 'rdfs:label "发射功率" . ;'
            # （非法 Turtle，rdflib BadSyntax；"父属性+domain"并存时同样触发）
            pname = _parent_name(conn, r, rows)
            cont = []
            if pname:
                cont.append(f"    rdfs:subPropertyOf {_uri(pname)}")
            # B/C（2026-08-30）：Domain 绑定 / XSD Range
            cont += [f"    rdfs:domain {_uri(c)}" for c in cons.get("domain_classes", [])]
            _props_obj2 = r["properties"] if "properties" in r.keys() else {}
            try:
                _props_obj2 = json.loads(_props_obj2 or "{}") if isinstance(_props_obj2, str) else (_props_obj2 or {})
            except Exception:
                _props_obj2 = {}
            _xsd2 = typevocab.xsd_of(cons.get("xsd_type") or (_props_obj2.get("type") if isinstance(_props_obj2, dict) else None))
            if _xsd2:
                cont.append(f"    rdfs:range {_xsd2}")
            if cont:
                L[-1] += " ;"
                L.append(" ;".join(cont) + " .")
            else:
                L[-1] += " ."
            if cons.get("cardinality") == "1" or "functional" in (cons.get("characteristics") or []):
                L.append(f"{u} rdf:type owl:FunctionalProperty .")
            for _ch in cons.get("characteristics", []):
                if _ch in OWL_CHAR_TYPE and _ch != "functional":
                    L.append(f"{u} rdf:type {OWL_CHAR_TYPE[_ch]} .")
        # P0（2026-09-08）注释导出补全：description → rdfs:comment 首段（此前右栏注释不进导出）；
        # profile_source/ref → rdfs:seeAlso；deprecated → owl:deprecated true + replaced_by 注记（OWL 标准弃用语义）
        note = []
        for k in cons.get("required", []):
            note.append(f"required:{k}")
        for k in cons.get("unique", []):
            note.append(f"unique:{k}")
        _desc = (r["description"] if "description" in r.keys() else "") or ""
        if _desc:
            note.insert(0, _desc)
        _rb = (r["replaced_by"] if "replaced_by" in r.keys() else "") or ""
        if _rb:
            note.append(f"replaced_by:{_rb}")
        if note:
            _lit = "; ".join(note).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            L.append(f'{u} rdfs:comment "{_lit}" .')
        _psrc = (r["profile_source"] if "profile_source" in r.keys() else "") or ""
        _pref = (r["profile_ref"] if "profile_ref" in r.keys() else "") or ""
        if _psrc or _pref:
            _lit2 = (_psrc + (" :: " if _psrc and _pref else "") + _pref)
            _lit2 = _lit2.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            L.append(f'{u} rdfs:seeAlso "{_lit2}" .')
        _st = (r["status"] if "status" in r.keys() else "") or ""
        if _st == "deprecated":
            L.append(f"{u} owl:deprecated true .")
        # 方案 C（2026-08-29）：公理双写——owl:Restriction 正式公理（required=minCardinality 1 / unique=cardinality 1），
        # onProperty 指向同名词（attribute 类型或数据属性 URI）；comment 保留兼容旧消费者。
        if kind == "entity":
            for k in cons.get("required", []):
                prop_uri = _uri(k)
                L.append(f"{u} rdfs:subClassOf [ a owl:Restriction ;")
                L.append(f"    owl:onProperty {prop_uri} ;")
                L.append("    owl:minQualifiedCardinality 1 ] .")
            for k in cons.get("unique", []):
                prop_uri = _uri(k)
                L.append(f"{u} rdfs:subClassOf [ a owl:Restriction ;")
                L.append(f"    owl:onProperty {prop_uri} ;")
                L.append("    owl:cardinality 1 ] .")
            # B/C（2026-08-30）：等价类 / 互斥 / 自定义公理
            for x in cons.get("equivalent_to", []):
                # 2026-09-08 修复：Manchester 表达式不能直接当 Turtle 三元组宾语（此前只要存在 equivalent_to 整个文件 BadSyntax）→ 注释保留原文（与 to_owl 一致）
                L.append(f"# equivalentTo: {x}")
            for x in cons.get("disjoint_with", []):
                L.append(f"{u} owl:disjointWith {_uri(x)} .")
            for x in cons.get("axioms", []):
                L.append(f"# axiom: {x}")
        L.append("")
    return chr(10).join(L), _warns


def from_owl(xml_text: str, conn) -> dict:
    """OWL/RDF-XML → ontology_types 入库（幂等：同名类型跳过）。

    返回 {imported, skipped, errors}
    """
    imported, skipped, errors = 0, 0, []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        return {"imported": 0, "skipped": 0, "errors": [f"XML 解析失败: {e}"]}

    # 先收集实体类型（domain/range 可能引用）
    classes = []
    object_props = []
    data_props = []
    subclass_links = []   # P0-1：(child_uri, parent_uri) 类层级
    subprop_links = []    # P0-1：(child_uri, parent_uri) 属性层级
    for elem in root.iter():
        tag = elem.tag
        if tag == f"{{{OWL}}}Class":
            uri = elem.get(f"{{{RDF}}}about") or elem.get(f"{{{RDF}}}ID")
            label = elem.findtext(f"{{{RDFS}}}label") or (_local(uri) if uri else "")
            comment = elem.findtext(f"{{{RDFS}}}comment") or ""
            classes.append({"name": label or "未命名", "uri": uri, "comment": comment})
            # P0-1：subClassOf 层级
            for sc in elem.findall(f"{{{RDFS}}}subClassOf"):
                p = sc.get(f"{{{RDF}}}resource", "")
                if uri and p:
                    subclass_links.append((uri, p))
        elif tag == f"{{{OWL}}}ObjectProperty":
            uri = elem.get(f"{{{RDF}}}about") or elem.get(f"{{{RDF}}}ID")
            label = elem.findtext(f"{{{RDFS}}}label") or (_local(uri) if uri else "")
            domains = [d.get(f"{{{RDF}}}resource", "") for d in elem.findall(f"{{{RDFS}}}domain")]
            ranges = [d.get(f"{{{RDF}}}resource", "") for d in elem.findall(f"{{{RDFS}}}range")]
            comment = elem.findtext(f"{{{RDFS}}}comment") or ""
            object_props.append({"name": label or "未命名", "domains": domains,
                                 "ranges": ranges, "comment": comment})
        elif tag == f"{{{OWL}}}DatatypeProperty":
            uri = elem.get(f"{{{RDF}}}about") or elem.get(f"{{{RDF}}}ID")
            label = elem.findtext(f"{{{RDFS}}}label") or (_local(uri) if uri else "")
            comment = elem.findtext(f"{{{RDFS}}}comment") or ""
            data_props.append({"name": label or "未命名", "comment": comment})
            for sp in elem.findall(f"{{{RDFS}}}subPropertyOf"):
                p = sp.get(f"{{{RDF}}}resource", "")
                if uri and p:
                    subprop_links.append((uri, p))

    # 类名集合（解析 domain/range 引用）
    class_names = {c["name"] for c in classes}

    # P0-1：URI → 名称映射（label 优先，兼容 Protégé 导出的无 label 文档）
    uri2name = {}
    for c in classes:
        uri2name[c["uri"]] = c["name"]

    def parse_comment(comment: str) -> dict:
        cons = {}
        for part in re.split(r"[;,]", comment or ""):
            part = part.strip()
            if part.startswith("required:"):
                cons.setdefault("required", []).append(part.split(":", 1)[1].strip())
            elif part.startswith("unique:"):
                cons.setdefault("unique", []).append(part.split(":", 1)[1].strip())
        return cons

    # 入库（幂等：同名跳过）
    for c in classes:
        dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (c["name"],)).fetchone()
        if dup:
            skipped += 1
            continue
        cons = parse_comment(c.get("comment", ""))
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, properties, constraints, description) VALUES (?,?,?,?,?)",
            (c["name"], "entity", "{}", __import__("json").dumps(cons, ensure_ascii=False),
             f"imported from OWL: {c.get('uri','')}"))
        imported += 1
    for p in object_props:
        dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (p["name"],)).fetchone()
        if dup:
            skipped += 1
            continue
        allowed = {}
        srcs = [_local(d) for d in p["domains"] if d and _local(d) in class_names]
        tgts = [_local(d) for d in p["ranges"] if d and _local(d) in class_names]
        if srcs:
            allowed["src"] = srcs
        if tgts:
            allowed["tgt"] = tgts
        cons = parse_comment(p["comment"])
        if allowed:
            cons["allowed_values"] = allowed
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, properties, constraints, description) VALUES (?,?,?,?,?)",
            (p["name"], "relation", "{}", __import__("json").dumps(cons, ensure_ascii=False),
             f"imported from OWL: ObjectProperty"))
        imported += 1
    for p in data_props:
        dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (p["name"],)).fetchone()
        if dup:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, properties, constraints, description) VALUES (?,?,?,?,?)",
            (p["name"], "attribute", "{}", __import__("json").dumps(parse_comment(p["comment"]), ensure_ascii=False),
             f"imported from OWL: DatatypeProperty"))
        imported += 1

    # P0-1：重建层级（subClassOf → parent_id；subPropertyOf → parent_id），幂等：仅补关系不覆盖已存在父
    name2id = {}
    for r in conn.execute("SELECT id, name FROM ontology_types").fetchall():
        name2id[r["name"]] = r["id"]
    for child_uri, parent_uri in subclass_links + subprop_links:
        cname = uri2name.get(child_uri) or _local(child_uri)
        pname = uri2name.get(parent_uri) or _local(parent_uri)
        if not cname or not pname:
            continue
        cid, pid = name2id.get(cname), name2id.get(pname)
        if not cid or not pid or cid == pid:
            continue
        cur = conn.execute("SELECT parent_id FROM ontology_types WHERE id=?", (cid,)).fetchone()
        if cur and cur["parent_id"]:
            continue  # 已有父，跳过（幂等）
        conn.execute("UPDATE ontology_types SET parent_id=? WHERE id=?", (pid, cid))
    conn.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── Turtle 导入：零依赖最小化解析器（覆盖 to_turtle 输出的反向 + Protégé 默认导出子集） ──
def _turtle_unquote(s: str) -> str:
    """Turtle 字符串字面量解转义（支持 \"、\\n、\\t、\\uXXXX）。"""
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == 'n':
                out.append('\\n'); i += 2; continue
            if nxt == 't':
                out.append('\\t'); i += 2; continue
            if nxt == 'r':
                out.append('\\r'); i += 2; continue
            if nxt == '"':
                out.append('"'); i += 2; continue
            if nxt == "'":
                out.append("'"); i += 2; continue
            if nxt == '\\':
                out.append('\\'); i += 2; continue
            if nxt == 'u' and i + 5 < len(s):
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6; continue
                except Exception:
                    pass
            out.append(nxt); i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


def _turtle_expand_curie(tok: str, prefixes: dict) -> str:
    """CURIE/前缀缩写 → 完整 URI；纯 URI（<...>）直接取内层；裸 IRI（含 :// 或 #）原样。"""
    tok = (tok or "").strip()
    if not tok:
        return ""
    if tok.startswith("<") and tok.endswith(">"):
        return tok[1:-1]
    if "://" in tok or tok.startswith("_:"):
        return tok
    if ":" in tok:
        pref, rest = tok.split(":", 1)
        if pref in prefixes:
            return prefixes[pref] + rest
        if "" in prefixes:
            return prefixes[""] + tok
    return tok


def _turtle_tokenize(seg: str) -> list:
    """Turtle 语句片段分词（空格/Tab/逗号分隔，尊重字符串与 <URI>）。"""
    s = (seg or "").replace(",", " ")
    toks = []
    cur = []
    i = 0
    in_str, quote = False, None
    in_angle = False
    while i < len(s):
        ch = s[i]
        if in_str:
            cur.append(ch)
            if ch == '\\' and i + 1 < len(s):
                cur.append(s[i + 1]); i += 2; continue
            if ch == quote:
                in_str = False; quote = None
            i += 1; continue
        if in_angle:
            cur.append(ch)
            if ch == ">":
                in_angle = False
            i += 1; continue
        if ch in ('"', "'"):
            in_str = True; quote = ch
            cur.append(ch); i += 1; continue
        if ch == "<":
            in_angle = True
            cur.append(ch); i += 1; continue
        if ch.isspace() or ch in ("\\t", "\\r", "\\n"):
            if cur:
                toks.append("".join(cur)); cur = []
            i += 1; continue
        cur.append(ch); i += 1
    if cur:
        toks.append("".join(cur))
    return toks


def from_turtle(text: str, conn) -> dict:
    """W3C Turtle (.ttl) → ontology_types 入库（幂等：同名类型跳过）。

    覆盖与 to_turtle 输出对称 + Protégé 默认导出子集：@prefix、owl:Class /
    ObjectProperty / DatatypeProperty、rdfs:label、rdfs:subClassOf /
    subPropertyOf、rdfs:domain / range、rdfs:comment（约束承载）。
    """
    imported, skipped, errors = 0, 0, []
    if not text:
        return {"imported": 0, "skipped": 0, "errors": ["内容为空"]}

    prefixes = {}
    classes, object_props, data_props = [], [], []
    subclass_links, subprop_links = [], []

    clean_lines = []
    _cmt_re = re.compile(r"(^|\s)#.*$")
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = _cmt_re.sub("", line).rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("@prefix "):
            try:
                # 先剥空白，再去掉结尾句点（rstrip 按字符集剥会误吃 >）
                tail = stripped[len("@prefix "):].strip()
                if tail.endswith("."):
                    tail = tail[:-1].strip()
                sp = tail.index(":")
                pref = tail[:sp].strip()
                uri_part = tail[sp + 1:].strip()
                if uri_part.startswith("<") and uri_part.endswith(">"):
                    uri_part = uri_part[1:-1]
                prefixes[pref] = uri_part
            except Exception as e:
                errors.append(f"@prefix 解析失败: {e}")
            continue
        clean_lines.append(line)

    # 按 "." 语句分割（尊重字符串）
    blob = "\n".join(clean_lines)
    statements = []
    cur, in_str, quote, i = [], False, None, 0
    while i < len(blob):
        ch = blob[i]
        if in_str:
            cur.append(ch)
            if ch == '\\' and i + 1 < len(blob):
                cur.append(blob[i + 1]); i += 2; continue
            if ch == quote:
                in_str = False; quote = None
            i += 1; continue
        if ch in ('"', "'"):
            in_str = True; quote = ch
            cur.append(ch); i += 1; continue
        if ch == ".":
            s = "".join(cur).strip()
            if s:
                statements.append(s)
            cur = []; i += 1; continue
        cur.append(ch); i += 1
    tail = "".join(cur).strip()
    if tail:
        statements.append(tail)

    def _subject_uri(subj: str) -> str:
        return _turtle_expand_curie(subj, prefixes)

    def _predicate(pred: str) -> str:
        return _turtle_expand_curie(pred, prefixes)

    def _object_value(obj: str):
        o = obj.strip()
        if not o:
            return "literal", ""
        if o.startswith("<") or o.startswith("_:") or "://" in o or (":" in o and not (o.startswith('"') or o.startswith("'"))):
            return "uri", _turtle_expand_curie(o, prefixes)
        if (o.startswith('"') and '"' in o[1:]) or (o.startswith("'") and "'" in o[1:]):
            q = o[0]
            end = o.index(q, 1)
            return "literal", _turtle_unquote(o[:end + 1])
        return "literal", o

    for stmt in statements:
        parts = [p.strip() for p in stmt.split(";") if p.strip()]
        subj = None
        items = []
        for idx, seg in enumerate(parts):
            tokens = _turtle_tokenize(seg)
            if len(tokens) < 2:
                continue
            if idx == 0:
                subj = tokens[0]
                pairs = tokens[1:]
            else:
                pairs = tokens
            if len(pairs) < 2:
                continue
            for k in range(0, len(pairs) - 1, 2):
                p = pairs[k]
                o = pairs[k + 1]
                items.append((p, o))
        if not subj or not items:
            continue
        s_uri = _subject_uri(subj)
        s_label = _local(s_uri)
        s_comment = ""
        s_type = None
        domains, ranges = [], []
        for pred_tok, obj_tok in items:
            p_uri = _predicate(pred_tok)
            okind, oval = _object_value(obj_tok)
            if p_uri == RDF + "type" or pred_tok == "a":
                if okind == "uri":
                    if oval == OWL + "Class":
                        s_type = "class"
                    elif oval == OWL + "ObjectProperty":
                        s_type = "obj"
                    elif oval == OWL + "DatatypeProperty":
                        s_type = "data"
            elif p_uri == RDFS + "label":
                if okind == "literal" and oval:
                    s_label = oval
            elif p_uri == RDFS + "comment":
                if okind == "literal":
                    s_comment = oval
            elif p_uri == RDFS + "subClassOf":
                if okind == "uri" and s_uri:
                    subclass_links.append((s_uri, oval))
            elif p_uri == RDFS + "subPropertyOf":
                if okind == "uri" and s_uri:
                    subprop_links.append((s_uri, oval))
            elif p_uri == RDFS + "domain":
                if okind == "uri":
                    domains.append(oval)
            elif p_uri == RDFS + "range":
                if okind == "uri":
                    ranges.append(oval)
        if not s_type or not s_label:
            continue
        rec = {"name": s_label, "uri": s_uri, "comment": s_comment,
               "domains": domains, "ranges": ranges}
        if s_type == "class":
            classes.append(rec)
        elif s_type == "obj":
            object_props.append(rec)
        elif s_type == "data":
            data_props.append(rec)

    class_names = {c["name"] for c in classes}
    uri2name = {}
    for c in classes:
        uri2name[c["uri"]] = c["name"]

    def parse_comment(comment: str) -> dict:
        cons = {}
        for part in re.split(r"[;,]", comment or ""):
            part = part.strip()
            if part.startswith("required:"):
                cons.setdefault("required", []).append(part.split(":", 1)[1].strip())
            elif part.startswith("unique:"):
                cons.setdefault("unique", []).append(part.split(":", 1)[1].strip())
        return cons

    for c in classes:
        dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (c["name"],)).fetchone()
        if dup:
            skipped += 1; continue
        cons = parse_comment(c.get("comment", ""))
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, properties, constraints, description) VALUES (?,?,?,?,?)",
            (c["name"], "entity", "{}", __import__("json").dumps(cons, ensure_ascii=False),
             f"imported from Turtle: {c.get('uri','')}"))
        imported += 1
    for p in object_props:
        dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (p["name"],)).fetchone()
        if dup:
            skipped += 1; continue
        allowed = {}
        srcs = [_local(d) for d in p.get("domains", []) if d and _local(d) in class_names]
        tgts = [_local(d) for d in p.get("ranges", []) if d and _local(d) in class_names]
        if srcs: allowed["src"] = srcs
        if tgts: allowed["tgt"] = tgts
        cons = parse_comment(p.get("comment", ""))
        if allowed: cons["allowed_values"] = allowed
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, properties, constraints, description) VALUES (?,?,?,?,?)",
            (p["name"], "relation", "{}", __import__("json").dumps(cons, ensure_ascii=False),
             "imported from Turtle: ObjectProperty"))
        imported += 1
    for p in data_props:
        dup = conn.execute("SELECT id FROM ontology_types WHERE name=?", (p["name"],)).fetchone()
        if dup:
            skipped += 1; continue
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, properties, constraints, description) VALUES (?,?,?,?,?)",
            (p["name"], "attribute", "{}",
             __import__("json").dumps(parse_comment(p.get("comment", "")), ensure_ascii=False),
             "imported from Turtle: DatatypeProperty"))
        imported += 1

    name2id = {}
    for r in conn.execute("SELECT id, name FROM ontology_types").fetchall():
        name2id[r["name"]] = r["id"]
    for child_uri, parent_uri in subclass_links + subprop_links:
        cname = uri2name.get(child_uri) or _local(child_uri)
        pname = uri2name.get(parent_uri) or _local(parent_uri)
        if not cname or not pname:
            continue
        cid, pid = name2id.get(cname), name2id.get(pname)
        if not cid or not pid or cid == pid:
            continue
        cur = conn.execute("SELECT parent_id FROM ontology_types WHERE id=?", (cid,)).fetchone()
        if cur and cur["parent_id"]:
            continue
        conn.execute("UPDATE ontology_types SET parent_id=? WHERE id=?", (pid, cid))
    conn.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}
