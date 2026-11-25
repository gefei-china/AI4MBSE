"""P1：SysML 2.x Profile（KerML metadata）⇄ 本体 转换核心函数。

对齐 OMG SysML v2（formal-25-09-03）metadata 机制（取代 1.x Profile）：

    package Name {
      metadata def 类型名 [ : 父类型 ] {
        attribute 属性 : 类型 ;      // 数据类型属性
        feature 关系 : 目标类型 ;     // 对象引用（指向本包 metadata def）→ 关系
      }
    }

职责边界（详见 docs/sysml-profile-本体转换详细设计.md §4）：
- parse_profile_v2：KerML 文本 → ProfileModel（中间结构，不落库）
- to_profile_v2：本体（ontology_types）→ KerML 文本（供 SysML v2 工具加载）

解析/生成约定：
- 对象引用判定：属性/特征类型指向本包已定义的 metadata def → relation_ends；否则 attributes
- KerML 无内建 required 关键字：以 `// required: a, b` 注释承载（往返保留）
- 枚举取值：以 `// allowed 属性: v1, v2` 注释承载
- 未知名类型（既非基础类型也非本包 def）→ 保留原名入 attributes.type，dataType 兜底 string
"""
import json
import re

# KerML 标识符（SysML v2 支持 Unicode 标识符，含中文）
_IDENT = r"[A-Za-z0-9_\u4e00-\u9fa5]+"
_IDENT_TYPE = r"[A-Za-z0-9_\u4e00-\u9fa5.]+"

# 内部属性类型 → KerML 基础类型
KERML_TYPE = {
    "string": "String", "text": "String",
    "decimal": "ScalarValue",
    "bool": "Boolean", "boolean": "Boolean",
    "date": "Instant",
}

# KerML 基础类型 → 内部属性类型
KERML_TO_INTERNAL = {
    "String": "string",
    "ScalarValue": "decimal", "Real": "decimal", "Number": "decimal", "Integer": "decimal",
    "Boolean": "bool",
    "Instant": "date", "Date": "date",
}


def _ident(s) -> str:
    """名称 → 合法 KerML 标识符（保留中英文/数字/下划线，其余转 _）。"""
    return re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5]", "_", str(s))


def _is_data_type(t: str) -> bool:
    """是否为 KerML 基础数据类型（非对象引用）。"""
    return t in KERML_TO_INTERNAL


# ═══════════ 1. 解析：KerML 文本 → ProfileModel ═══════════

def parse_profile_v2(text: str) -> dict:
    """解析 SysML 2.x KerML 文本 → ProfileModel（format="v2"）。

    支持：package 块、metadata def（含 specialization `: Parent`）、
    attribute/feature 声明、`// required:` 与 `// allowed 属性:` 注释、import。
    属性类型指向本包已定义 def → relation_ends；否则 attributes。
    """
    pm = {"name": "", "version": "1.0", "format": "v2",
          "base_metaclass_map": {}, "enumerations": {}, "stereotypes": []}
    text = text or ""

    # 1) package 名（可选）
    m = re.search(rf"\bpackage\s+({_IDENT})\s*\{{", text)
    if m:
        pm["name"] = m.group(1)

    # 2) 第一遍：metadata def 定位（括号配对取体）
    defs = []
    for m in re.finditer(rf"\bmetadata\s+def\s+({_IDENT})(?:\s*:\s*({_IDENT}))?\s*\{{", text):
        name, parent = m.group(1), m.group(2)
        depth, j = 1, m.end() - 1  # 从 '{' 开始配对
        while j < len(text) - 1 and depth:
            j += 1
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
        if depth != 0:
            continue
        defs.append({"name": name, "parent": parent,
                     "body": text[m.end():j], "comment": _block_comment(text[m.start():m.end()])})

    # 3) 第二遍：属性/关系判定（类型是否为本包 def）
    def_names = {d["name"] for d in defs}
    for d in defs:
        st = {"name": d["name"], "parent": d["parent"], "kind": "entity",
              "attributes": [], "relation_ends": [], "required": [],
              "constraints_raw": ""}
        raw_lines = []
        for line in d["body"].splitlines():
            line = line.strip()
            if not line:
                continue
            # 提取行尾注释（`// ...`，支持行首纯注释与声明行尾注释）
            comment = ""
            ci = line.find("//")
            if ci >= 0:
                comment = line[ci + 2:].strip()
                line = line[:ci].strip()
            req_matched = al_matched = False
            if comment:
                # required / allowed 注释（KerML 无内建关键字，注释承载）
                req = re.search(r"required\s*:\s*(.+)", comment)
                if req:
                    req_matched = True
                    st["required"] += [x.strip() for x in req.group(1).split(",") if x.strip()]
                al = re.search(r"allowed\s+(\S+)\s*:\s*(.+)", comment)
                if al:
                    al_matched = True
                    key, vals = al.group(1), [x.strip() for x in al.group(2).split(",") if x.strip()]
                    pm["enumerations"].setdefault(key, vals)
                # 纯注释行（无声明）：仅非 required/allowed 的原文保留
                if not line and not req_matched and not al_matched:
                    raw_lines.append(comment)
            if not line:
                continue
            # attribute / feature 声明（带名）
            am = re.match(rf"(attribute|feature)\s+({_IDENT})\s*:\s*({_IDENT_TYPE})\s*;?", line)
            if am:
                kind, aname, atype = am.group(1), am.group(2), am.group(3)
                if atype in def_names:
                    # 对象引用 → 关系端（src=本类型，tgt=引用类型）
                    st["relation_ends"].append({"src": d["name"], "tgt": atype, "name": aname})
                else:
                    st["attributes"].append({
                        "name": aname, "type": atype,
                        "dataType": KERML_TO_INTERNAL.get(atype, "string"),
                        "min": 1 if kind == "feature" else None, "max": None,
                        "enum": pm["enumerations"].get(aname),
                    })
                continue
            # 无名声明（关系引用常用形式，doc §6.2）：attribute : Type; / feature : Type;
            am2 = re.match(rf"(attribute|feature)\s*:\s*({_IDENT_TYPE})\s*;?", line)
            if am2 and am2.group(2) in def_names:
                # 无名引用指向本包 def → 关系端（name 兜底取目标类型名）
                st["relation_ends"].append({"src": d["name"], "tgt": am2.group(2),
                                            "name": am2.group(2)})
                continue
            raw_lines.append(line)
        st["constraints_raw"] = " / ".join(raw_lines[:5])
        pm["stereotypes"].append(st)
    return pm


def _block_comment(prefix: str) -> str:
    """提取 def 声明行内尾随注释（`metadata def X { // note`）。"""
    i = prefix.find("//")
    return prefix[i + 2:].strip() if i >= 0 else ""


# ═══════════ 2. 生成：本体 → KerML 文本 ═══════════

def to_profile_v2(conn, profile_name: str = "XingWangProfile") -> str:
    """本体（ontology_types）→ SysML 2.x KerML 文本。

    映射（与 parse_profile_v2 互逆，支持往返）：
    - entity 类型 → metadata def（parent_id → `: Parent`）
    - properties → attribute（类型经 KERML_TYPE 映射；枚举/必填 → 注释）
    - relation 类型 → 挂到 domain 类型的 feature（type=range），域空则生成独立注释提示
    """
    rows = conn.execute("SELECT * FROM ontology_types").fetchall()
    id2name = {r["id"]: r["name"] for r in rows}
    ents, rels = [], []
    for r in rows:
        d = dict(r)
        try:
            d["properties"] = json.loads(d.get("properties") or "{}")
        except Exception:
            d["properties"] = {}
        try:
            d["constraints"] = json.loads(d.get("constraints") or "{}")
        except Exception:
            d["constraints"] = {}
        if d["type_kind"] == "entity":
            ents.append(d)
        elif d["type_kind"] == "relation":
            rels.append(d)

    L = [f"package {_ident(profile_name)} {{"]
    if not ents:
        L.append("  // 本体为空，无 metadata def")
        L.append("}")
        return "\n".join(L)

    for e in ents:
        parent = id2name.get(e.get("parent_id")) if e.get("parent_id") else None
        head = f"  metadata def {_ident(e['name'])}" + (f" : {_ident(parent)}" if parent else "") + " {"
        L.append(head)
        props, cons = e["properties"], e["constraints"]
        # 属性
        for k, v in props.items():
            t = v.get("type") if isinstance(v, dict) else "string"
            kt = KERML_TYPE.get(t, "String")
            note = []
            if k in cons.get("required", []):
                note.append("required: " + k)
            av = (cons.get("allowed_values") or {}).get(k)
            if isinstance(av, list) and av:
                note.append("allowed " + k + ": " + ", ".join(str(x) for x in av))
            comment = ("  // " + " | ".join(note)) if note else ""
            L.append(f"    attribute {_ident(k)} : {kt};{comment}")
        # 出边关系（domain 含本类型）→ feature
        rel_feats = []
        for r in rels:
            av = r["constraints"].get("allowed_values") or {}
            srcs = av.get("src") or []
            if isinstance(srcs, str):
                srcs = [srcs]
            tgts = av.get("tgt") or []
            if isinstance(tgts, str):
                tgts = [tgts]
            if e["name"] not in srcs:
                continue
            for g in tgts:
                rel_feats.append((r["name"], g))
        for fname, gtgt in rel_feats:
            L.append(f"    feature {_ident(fname)} : {_ident(gtgt)};  // 关系")
        L.append("  }")
    # 关系未挂出的提示（无 domain 的关系）
    orphans = [r["name"] for r in rels
               if not ((r["constraints"].get("allowed_values") or {}).get("src"))]
    if orphans:
        L.append("  // 提示：以下关系未定义 domain，未导出为 feature: " + "、".join(orphans))
    L.append("}")
    return "\n".join(L)


# ═══════════ 2.5 图谱实例 → SysML 2.x KerML 模型文本（FR-KG-7 补 G11）═══════════

def to_model_v2(conn, branch: str = "release", model_name: str = "GraphInstanceModel") -> str:
    """已入库图谱实例（entities/relations）→ SysML 2.x KerML 模型文本。

    实例层映射（对齐 to_profile_v2 注释风格，与 sysml_importer 构成双向闭环）：
    - entity_type → `part def <TypeName>;`（每个类型一个定义）
    - 实体实例 → `part <id> : <TypeName> { attribute <key> : <ScalarType>; ... }`
      （properties JSON 每键一个 attribute；number → Real，其余 → String；空值省略为注释）
    - 关系 → `connector <rel_type> from <src_id> to <tgt_id>;`
    - 标识符一律经 _ident 合法化；分支语义对齐 graph_entities
      （branch IN (指定分支, 'release')，同 id 当前分支版本优先）

    健壮性：任意脏数据不抛异常（JSON 解析兜底空 dict）；分支无实例时返回含注释的空包。
    """
    import datetime

    rows = conn.execute(
        "SELECT id, name, entity_type, status, branch, properties FROM entities "
        "WHERE status NOT IN ('deprecated','raw_chunk') AND branch IN (?, 'release')",
        (branch,)).fetchall()
    # 版本化去重：同 id 优先指定分支版本（对齐 graph_entities 展示语义）
    seen = {}
    for r in rows:
        d = dict(r)
        if d["id"] not in seen or d["branch"] == branch:
            seen[d["id"]] = d
    ents = list(seen.values())

    rel_rows = conn.execute(
        "SELECT source_id, target_id, relation_type, branch FROM relations "
        "WHERE status != 'deprecated' AND branch IN (?, 'release')",
        (branch,)).fetchall()
    rels, seen_rel = [], {}
    for r in rel_rows:
        d = dict(r)
        key = (d["source_id"], d["target_id"], d["relation_type"])
        if key not in seen_rel or d["branch"] == branch:
            seen_rel[key] = d
    # 仅导出两端实体均在本分支（含 release 兜底）且非废弃的关系
    ent_ids = {e["id"] for e in ents}
    rels = [d for d in seen_rel.values()
            if d["source_id"] in ent_ids and d["target_id"] in ent_ids]

    pkg = _ident(model_name)
    L = [
        "// ════════════════════════════════════════════════════════════════",
        "// SysML v2 (KerML) 模型导出 —— 图谱实例反向导出（FR-KG-7 补 G11）",
        f"// 来源分支: {branch}    生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"// 实体 {len(ents)} 个 · 关系 {len(rels)} 条",
        "// 由 mbse_system 按本体约束自动生成（part def / part usage / connector）",
        "// ════════════════════════════════════════════════════════════════",
        f"package {pkg} {{",
    ]
    if not ents:
        L.append("  // 分支无可用图谱实例（实体为空），无内容可导出")
        L.append("}")
        return "\n".join(L)

    # 1) 类型定义：每个 entity_type 一个 part def
    L.append("  // ── 类型定义（按本体 entity_type）──")
    for t in sorted({e["entity_type"] for e in ents}):
        L.append(f"  part def {_ident(t)};")

    # 2) 实例 usage：part <id> : <TypeName> { attribute ... }
    L.append("")
    L.append("  // ── 实例（part usage）──")
    for e in ents:
        try:
            props = json.loads(e.get("properties") or "{}") or {}
        except Exception:
            props = {}  # 脏 JSON 兜底
        L.append(f"  part {_ident(e['id'])} : {_ident(e['entity_type'])} {{")
        if not props:
            L.append("    // 无属性")
        for k, v in props.items():
            if v is None or v == "":
                L.append(f"    // attribute {_ident(k)} : String;  // 空值省略")
                continue
            st = "Real" if isinstance(v, (int, float)) and not isinstance(v, bool) else "String"
            L.append(f"    attribute {_ident(k)} : {st};")
        L.append("  }")

    # 3) 关系 connector
    L.append("")
    if rels:
        L.append("  // ── 关系（connector）──")
        for d in rels:
            L.append(f"  connector {_ident(d['relation_type'])} "
                     f"from {_ident(d['source_id'])} to {_ident(d['target_id'])};")
    else:
        L.append("  // 无可用关系")
    L.append("}")
    return "\n".join(L)


# ═══════════ 3. SysML 1.x Profile（UML XMI）解析 ═══════════

def parse_profile_1x(xml_text: str) -> dict:
    """解析 SysML 1.x Profile（XMI 2.x）→ ProfileModel（format="1x"）。

    支持（对齐 Eclipse UML2 / Cameo 导出结构）：
    - uml:Profile 根 + uml:Stereotype（ownedAttribute / generalization / ownedRule）
    - PrimitiveType / Enumeration（ownedLiteral）→ 属性类型与取值
    - 对象引用判定：ownedAttribute.type 指向本 Profile 内 Stereotype → relation_ends
    - 多重性 lower="1" → required
    - uml:Extension 的 ownedEnd name="base_<元类>" → base_metaclass_map（UML 标准扩展端命名）
    解析失败返回 {"error": ...}（与 ProfileModel 同构，error 字段供上层提示）。
    """
    import xml.etree.ElementTree as ET

    pm = {"name": "", "version": "1.0", "format": "1x",
          "base_metaclass_map": {}, "enumerations": {}, "stereotypes": []}
    try:
        root = ET.fromstring(xml_text or "")
    except Exception as e:
        pm["error"] = f"XML 解析失败: {e}"
        return pm

    def _local(tag):
        s = tag.rsplit("}", 1)[-1]
        return s.rsplit(":", 1)[-1]

    def _attr(el, *keys):
        for k in keys:
            v = el.get(k)
            if v is not None:
                return v
        for k, v in el.attrib.items():
            local = k.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
            for key in keys:
                if key.split(":")[-1] == local:
                    return v
        return None

    # 1) 全量索引：xmi:id → {name, tag, el}（PrimitiveType/Enumeration/Stereotype/Extension 等）
    id_info = {}
    for el in root.iter():
        nid = _attr(el, "xmi:id", "id", "ID") or ""
        if not nid:
            continue
        tag = _local(el.tag)
        # packagedElement/ownedMember 的真实类型在 xmi:type 属性（如 uml:Stereotype）
        if tag in ("packagedElement", "ownedMember", "member", "nestedElement"):
            xmitype = _attr(el, "xmi:type", f"{{{XMI_NS}}}type") or ""
            if xmitype:
                tag = _local(xmitype)
        id_info[nid] = {"name": _attr(el, "declaredName", "name", "Name") or "",
                        "tag": tag, "el": el}
    pm["name"] = _attr(root, "name", "Name") or ""

    # 2) 枚举字面量
    for nid, info in id_info.items():
        if info["tag"] != "Enumeration":
            continue
        literals = [_attr(le, "name", "Name") for le in info["el"]
                    if _local(le.tag) in ("ownedLiteral", "literal") and _attr(le, "name", "Name")]
        pm["enumerations"][info["name"]] = [l for l in literals if l]

    # 3) Stereotype
    for nid, info in id_info.items():
        if info["tag"] != "Stereotype":
            continue
        el = info["el"]
        st = {"name": info["name"], "parent": None, "kind": "entity",
              "attributes": [], "relation_ends": [], "required": [],
              "constraints_raw": ""}
        for child in el:
            ctag = _local(child.tag)
            if ctag == "ownedAttribute":
                aname = _attr(child, "name", "Name") or ""
                tref = _attr(child, "type") or ""
                lower = _attr(child, "lower") or ""
                tinfo = id_info.get(tref)
                ttag, tname = (tinfo["tag"], tinfo["name"]) if tinfo else ("", "")
                if ttag == "Stereotype" and tname:
                    # 对象引用 → 关系端
                    st["relation_ends"].append({"src": info["name"], "tgt": tname, "name": aname})
                else:
                    # 数据类型 / 枚举属性
                    internal = "string"
                    if ttag == "Enumeration":
                        internal = "enum"
                    elif tname in KERML_TO_INTERNAL:
                        internal = KERML_TO_INTERNAL[tname]
                    elif tname == "String":
                        internal = "string"
                    st["attributes"].append({
                        "name": aname, "type": tname or "",
                        "dataType": internal,
                        "min": 1 if lower == "1" else None, "max": None,
                        "enum": (pm["enumerations"].get(tname) if ttag == "Enumeration"
                                 else pm["enumerations"].get(aname)),
                    })
                    if lower == "1":
                        st["required"].append(aname)
            elif ctag == "generalization":
                gref = _attr(child, "general", "generalization", "general") or ""
                st["parent"] = id_info.get(gref, {}).get("name") if gref else None
            elif ctag == "ownedRule":
                st["constraints_raw"] = (st["constraints_raw"] + " " + (child.text or "")).strip()
        pm["stereotypes"].append(st)

    # 4) Extension → base_metaclass_map（ownedEnd name="base_<元类>"，UML 标准命名）
    for nid, info in id_info.items():
        if info["tag"] != "Extension":
            continue
        for child in info["el"]:
            if _local(child.tag) not in ("ownedEnd", "end"):
                continue
            cname = _attr(child, "name", "Name") or ""
            tref = _attr(child, "type") or ""
            if cname.startswith("base_"):
                base = cname[len("base_"):]
                stname = id_info.get(tref, {}).get("name") if tref else ""
                if stname:
                    pm["base_metaclass_map"][stname] = base
    return pm


# ═══════════ 4. SysML 1.x Profile（XMI）生成 ═══════════

UML_NS = "http://www.eclipse.org/uml2/5.0.0/UML"
XMI_NS = "http://www.omg.org/XMI"
# 内部类型 → UML PrimitiveType 名（SysML 1.x 建模工具可识别）
UML_TYPE = {"string": "String", "text": "String", "decimal": "Real",
            "bool": "Boolean", "date": "Date"}


def _read_base_map(conn) -> dict:
    """读最近一次 Profile 导入的 base_metaclass_map（表不存在/为空 → {}）。"""
    try:
        row = conn.execute(
            "SELECT base_metaclass_map FROM ontology_profile_meta ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return {}


def to_profile_1x(conn, profile_name: str = "XingWangProfile") -> str:
    """本体 → SysML 1.x Profile（XMI 2.x .profile 文件）。

    映射（与 parse_profile_1x 互逆）：
    - entity → uml:Stereotype（parent_id → uml:Generalization）
    - properties → ownedAttribute（type 引用 PrimitiveType；枚举 → uml:Enumeration + ownedLiteral）
    - required → lower="1"；allowed_values 枚举 → 注释保留
    - relation → 挂到 domain 类型的引用属性（type=range stereotype）
    - base_metaclass_map → uml:Extension（ownedEnd name="base_<元类>"）；缺省 Block
    """
    import xml.etree.ElementTree as ET

    rows = conn.execute("SELECT * FROM ontology_types").fetchall()
    ents, rels = [], []
    for r in rows:
        d = dict(r)
        try:
            d["properties"] = json.loads(d.get("properties") or "{}")
        except Exception:
            d["properties"] = {}
        try:
            d["constraints"] = json.loads(d.get("constraints") or "{}")
        except Exception:
            d["constraints"] = {}
        if d["type_kind"] == "entity":
            ents.append(d)
        elif d["type_kind"] == "relation":
            rels.append(d)

    ET.register_namespace("uml", UML_NS)
    ET.register_namespace("xmi", XMI_NS)

    def q(tag):
        return f"{{{UML_NS}}}{tag}"

    root = ET.Element(q("Profile"),
                      {f"{{{XMI_NS}}}version": "2.1", "name": _ident(profile_name)})

    def pe(*, etype, eid=None, name=None, **attrs):
        el = ET.SubElement(root, q("packagedElement"), {f"{{{XMI_NS}}}type": etype})
        if eid:
            el.set(f"{{{XMI_NS}}}id", eid)
        if name:
            el.set("name", name)
        for k, v in attrs.items():
            el.set(k, str(v))
        return el

    # 1) 类型预扫：PrimitiveType 需求 + 枚举属性
    needed_prims = set()
    enums = {}   # 枚举名 → 字面量列表
    for e in ents:
        for k, v in e["properties"].items():
            t = v.get("type") if isinstance(v, dict) else "string"
            if t == "enum":
                av = (e["constraints"].get("allowed_values") or {}).get(k)
                if isinstance(av, list) and av:
                    enums.setdefault(k, [str(x) for x in av])
            else:
                needed_prims.add(UML_TYPE.get(t, "String"))

    # 2) PrimitiveType 定义
    for pname in sorted(needed_prims):
        pe(etype="uml:PrimitiveType", eid=f"_T_{pname}", name=pname)
    # 3) Enumeration 定义
    for ename, literals in enums.items():
        ee = pe(etype="uml:Enumeration", eid=f"_E_{_ident(ename)}", name=ename)
        for i, lit in enumerate(literals):
            ET.SubElement(ee, q("ownedLiteral"),
                          {f"{{{XMI_NS}}}id": f"_EL_{_ident(ename)}_{i}", "name": lit})

    # 4) Stereotype（entity）+ 关系引用属性
    id_by_name = {e["name"]: f"_S_{_ident(e['name'])}" for e in ents}
    for e in ents:
        se = pe(etype="uml:Stereotype", eid=id_by_name[e["name"]], name=e["name"])
        cons = e["constraints"]
        # 属性
        for k, v in e["properties"].items():
            t = v.get("type") if isinstance(v, dict) else "string"
            if t == "enum" and k in enums:
                tref = f"_E_{_ident(k)}"
                dtype = "enum"
            else:
                tref = f"_T_{UML_TYPE.get(t, 'String')}"
                dtype = "string"
            attrs = {"type": tref}
            if k in cons.get("required", []):
                attrs["lower"] = "1"
            aa = ET.SubElement(se, q("ownedAttribute"),
                               {f"{{{XMI_NS}}}id": f"_A_{_ident(e['name'])}_{_ident(k)}",
                                "name": k, **attrs})
            if dtype == "enum":
                av = (cons.get("allowed_values") or {}).get(k)
                if isinstance(av, list) and av:
                    ET.SubElement(aa, q("ownedComment"),
                                  {f"{{{XMI_NS}}}id": f"_AC_{_ident(e['name'])}_{_ident(k)}"}).text = \
                        "allowed: " + ", ".join(str(x) for x in av)
        # 出边关系（domain 含本类型）→ 引用型 ownedAttribute
        for r in rels:
            av = r["constraints"].get("allowed_values") or {}
            srcs = av.get("src") or []
            if isinstance(srcs, str):
                srcs = [srcs]
            tgts = av.get("tgt") or []
            if isinstance(tgts, str):
                tgts = [tgts]
            if e["name"] not in srcs:
                continue
            for g in tgts:
                if g not in id_by_name:
                    continue
                ET.SubElement(se, q("ownedAttribute"),
                              {f"{{{XMI_NS}}}id": f"_A_{_ident(e['name'])}_{_ident(r['name'])}",
                               "name": _ident(r["name"]), "type": id_by_name[g]})
        # 层级
        if e.get("parent_id"):
            pname = next((x["name"] for x in ents if x["id"] == e["parent_id"]), None)
            if pname and pname in id_by_name:
                ET.SubElement(se, q("generalization"),
                              {f"{{{XMI_NS}}}id": f"_G_{_ident(e['name'])}",
                               "general": id_by_name[pname]})

    # 5) Extension（base_metaclass_map 反查；缺省 Block）
    base_map = _read_base_map(conn)
    for e in ents:
        base = base_map.get(e["name"], "Block")
        ex = pe(etype="uml:Extension", eid=f"_EXT_{_ident(e['name'])}",
                name=f"Ext_{_ident(e['name'])}")
        ET.SubElement(ex, q("ownedEnd"),
                      {f"{{{XMI_NS}}}id": f"_EO_{_ident(e['name'])}",
                       "name": f"base_{base}", "type": id_by_name[e["name"]], "aggregation": "none"})
        ET.SubElement(ex, q("ownedEnd"),
                      {f"{{{XMI_NS}}}id": f"_EC_{_ident(e['name'])}",
                       "name": f"extension_{base}", "aggregation": "none"})
        ET.SubElement(ex, q("memberEnd"),
                      {f"{{{XMI_NS}}}idref": f"_EO_{_ident(e['name'])}"})
        ET.SubElement(ex, q("memberEnd"),
                      {f"{{{XMI_NS}}}idref": f"_EC_{_ident(e['name'])}"})

    # XML 序列化（UTF-8，xml 声明）
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


# ═══════════ 5. 转换入库：ProfileModel → 本体候选 / 落库 ═══════════

def _attrs_of(st: dict) -> dict:
    """ProfileModel 的 stereotype/def → 本体 properties 与 constraints（含枚举/必填）。"""
    props, cons = {}, {"required": list(st.get("required") or [])}
    for a in st.get("attributes") or []:
        t = a.get("dataType") or "string"
        if a.get("enum"):
            t = "enum"
            cons.setdefault("allowed_values", {})[a["name"]] = a["enum"]
        props[a["name"]] = {"note": a.get("type") or "", "type": t}
    return props, cons


def _is_relation_def(st: dict) -> bool:
    """纯关系 def：无数据属性且含对象引用特征（`metadata def 关系 { feature : 目标 }`）。"""
    return bool(st.get("relation_ends")) and not (st.get("attributes") or [])


def profile_to_candidates(conn, pm: dict) -> dict:
    """ProfileModel → 本体候选（含冲突标记），供导入预览确认（对齐 v2g 治理范式）。

    返回 {candidates:[{name,type_kind,parent,properties,constraints,conflicts[]}],
          conflicts:[全局冲突], stats:{entity,relation,conflict}}
    """
    existing = {r["name"] for r in conn.execute("SELECT name FROM ontology_types").fetchall()}
    candidates, conflicts = [], []

    # 1) entity 候选（Stereotype / metadata def）；纯关系 def 跳过（走 relation 候选）
    for st in pm.get("stereotypes", []):
        if _is_relation_def(st):
            continue
        props, cons = _attrs_of(st)
        cand = {"name": st["name"], "type_kind": "entity",
                "parent": st.get("parent"), "properties": props, "constraints": cons,
                "conflicts": []}
        if st["name"] in existing:
            cand["conflicts"].append(f"与现有类型同名: {st['name']}（导入策略：跳过或覆盖）")
        if st.get("parent") and st["parent"] not in existing:
            cand["conflicts"].append(f"父类型 '{st['parent']}' 不在库中，层级将跳过")
        candidates.append(cand)
        conflicts += cand["conflicts"]

    # 2) relation 候选（类型内 feature + 纯关系 def，按 (名, 端对) 去重）
    seen = set()
    for st in pm.get("stereotypes", []):
        if _is_relation_def(st):
            # 纯关系 def：range 取其 feature 引用类型；domain 可选（specialization 作为提示）
            tgts = sorted({r.get("tgt") for r in st.get("relation_ends") or [] if r.get("tgt")})
            cand = {"name": st["name"], "type_kind": "relation", "parent": None,
                    "properties": {},
                    "constraints": {"allowed_values": {"tgt": tgts}},
                    "conflicts": [f"与现有关系类型同名: {st['name']}"] if st["name"] in existing else []}
            if st.get("parent"):
                cand["constraints"]["allowed_values"]["src"] = [st["parent"]]
            else:
                cand["conflicts"].append(
                    f"关系 '{st['name']}' 未声明 domain（仅 range: {'/'.join(tgts) or '未定'}），导入后可编辑")
            key = (st["name"], tuple(sorted([st.get("parent") or "", ] + tgts)))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(cand)
            conflicts += cand["conflicts"]
            continue
        for r in st.get("relation_ends") or []:
            key = (r.get("name"), tuple(sorted([r.get("src"), r.get("tgt")])))
            if key in seen:
                continue
            seen.add(key)
            cname = r.get("name") or "相关"
            cand = {"name": cname, "type_kind": "relation", "parent": None,
                    "properties": {},
                    "constraints": {"allowed_values": {"src": [r.get("src")], "tgt": [r.get("tgt")]}},
                    "conflicts": [f"与现有关系类型同名: {cname}"] if cname in existing else []}
            candidates.append(cand)
            conflicts += cand["conflicts"]

    return {"candidates": candidates, "conflicts": conflicts,
            "stats": {"entity": sum(1 for c in candidates if c["type_kind"] == "entity"),
                      "relation": sum(1 for c in candidates if c["type_kind"] == "relation"),
                      "conflict": len(conflicts)}}


def parse_profile(content: str, fmt: str) -> dict:
    """按格式分发解析（1x | v2）。未知格式/解析失败 → 携带 error 的 ProfileModel。"""
    fmt = (fmt or "1x").lower()
    if fmt == "1x":
        return parse_profile_1x(content)
    if fmt == "v2":
        return parse_profile_v2(content)
    return {"name": "", "version": "1.0", "format": fmt,
            "base_metaclass_map": {}, "enumerations": {}, "stereotypes": [],
            "error": f"不支持的 Profile 格式: {fmt}（支持 1x | v2）"}
