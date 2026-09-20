"""O-3：SysML V2 模型 → 知识图谱导入 + 双向一致性（客户 P13-8，P3 落地）。

对齐行业路径（preprints.org/202512.0439）：MBSE 模型是天然结构化数据，
直接解析文本符号/JSON → 图 schema（RFLP 四层），无需 LLM 抽取。
映射（方案 §7）：
- requirement def/usage → 需求节点（类型 FR/PR/RR/DR）
- part def/usage → 部件节点（载荷/转发器/天线…）
- attribute def / port def → 属性 / Port 节点
- satisfies → SATISFIES 边；part 嵌套 → CONTAINS；connector/flow → FLOWS_TO；action → PERFORMS

双向一致性：sysml_sync 表记录 sysml_ref ↔ node_id 映射，供增量同步。
"""
import json
import re
import uuid

from ontology_semantics import GraphStore


def _new_batch() -> str:
    return f"sysml-{uuid.uuid4().hex[:8]}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_v2(content: str) -> dict:
    """SysML v2 文本 → {nodes, edges}。**唯一实现 = OMG 官方解析器**（sysml_ast）。

    2026-09-20：原 `parse_text`（手写关键字扫描 + 括号配对，~420 行）已删除。
    它不是语法解析器，实测会**漏建「包级 part usage」节点**（conv375 的
    `part evThermalSystem : EVThermalManagementSystem{...}` 声明了却不在节点集），
    导致引用它的 126/220 条关系整条丢弃（含 14 条 satisfy + 18 条 connect）
    —— 这正是 BDD 投影 0 条边、追溯关系全 0、与「1 项悬空引用」的共同根因。
    见 `sysml_ast.py` 顶部说明与 `tools/sysml_ast/SysMLAstExport.java`。
    失败会抛 RuntimeError（**必须可见**，不再有会静默丢数据的兜底）。
    """
    from sysml_ast import parse_strict
    return parse_strict(content)

def parse_json(model: dict) -> dict:

    """解析模型 JSON（AI 建模 Agent 输出结构）→ {nodes, edges}。"""
    nodes, edges = [], []
    for n in model.get("nodes", []):
        nodes.append({
            "name": n.get("name", ""),
            "entity_type": n.get("entity_type", "部件"),
            "properties": n.get("properties", {}),
            "def_id": n.get("id", ""),
        })
    for e in model.get("edges", []):
        edges.append({
            "source_name": e.get("source", ""),
            "target_name": e.get("target", ""),
            "relation_type": e.get("relation_type", "包含"),
            "props": e.get("props", {}),
        })
    return {"nodes": nodes, "edges": edges}


def parse_xml(xml_text: str) -> dict:
    """解析 SysML XML → {nodes, edges}。

    支持两种标准表示（零依赖，xml.etree）：
    - SysML 2.x XML 表示（W3C/SysML v2 API）：partDefinition/partUsage、requirement*、
      action*、connectionUsage 等元素，属性 id/declaredName/type。
    - SysML 1.x XMI（UML 2 元模型）：uml:Class + uml:Association（memberEnd xmi:idref）。
    连接端点通过元素引用（#id / xmi:idref / connectorEnd 的 type）解析到节点 id → name。
    """
    import xml.etree.ElementTree as ET
    nodes, edges = [], []

    def _local(tag):
        # 去 namespace：{uri}Local / prefix:Local → Local
        s = tag.rsplit("}", 1)[-1]
        return s.rsplit(":", 1)[-1]

    def _attr(el, *keys):
        for k in keys:
            v = el.get(k)
            if v is not None:
                return v
        # namespace 属性兜底：xmi:id / xmi:type / xmi:idref → {uri}id 等
        for k, v in el.attrib.items():
            local = k.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
            for key in keys:
                if key.split(":")[-1] == local:
                    return v
        return None

    def _resolve_ref(val, id2name):
        """引用值（#id / xmi:idref / id 或 xmi:id）→ 名称。"""
        if not val:
            return ""
        v = str(val).lstrip("#")
        return id2name.get(v, v)

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": f"XML 解析失败: {e}"}

    # 1) 元素类型 → 实体类型映射（SysML 2 局部名）
    NODE_KIND = {
        "partDefinition": "部件", "partUsage": "部件", "elementDefinition": "部件",
        "requirementDefinition": "需求", "requirementUsage": "需求",
        "actionDefinition": "功能", "actionUsage": "功能",
        "attributeDefinition": "属性", "attributeUsage": "属性",
        "portDefinition": "部件", "portUsage": "部件",
        "interfaceDefinition": "部件", "interfaceUsage": "部件",
        "stateDefinition": "部件", "stateUsage": "部件",
        # XMI（UML 2 元模型）类型
        "Class": "部件", "Requirement": "需求", "Component": "部件", "Node": "部件",
        "DataType": "属性", "Actor": "部件", "UseCase": "功能",
    }
    REL_KIND = {
        "connectionDefinition": "连接", "connectionUsage": "连接",
        "flowDefinition": "连接", "flowUsage": "连接",
        "satisfactionDefinition": "满足", "satisfactionUsage": "满足",
        "requirementSatisfaction": "满足",
        "Association": "连接", "Connector": "连接",
    }
    id2name = {}
    raw_nodes = []  # (id, name, entity_type, properties, el)
    for el in root.iter():
        tag = _local(el.tag)
        xmitype = ""
        if tag in ("packagedElement", "ownedMember", "nestedElement", "member"):
            # XMI：真实类型在 xmi:type 属性（如 uml:Class）
            xmitype = _attr(el, "xmi:type", "{http://www.omg.org/XMI}type")
            if xmitype:
                tag = _local(xmitype)
        if tag in NODE_KIND:
            nid = _attr(el, "xmi:id", "id", "ID") or ""
            name = _attr(el, "declaredName", "name", "Name") or ""
            props = {"kind": xmitype or tag}
            # requirement 的正文（reqText）
            for child in el:
                if _local(child.tag) in ("reqText", "text"):
                    props["req_text"] = (_clean(child.text or "") or "").strip()
            raw_nodes.append((nid, name, NODE_KIND[tag], props, el))
            if nid:
                id2name[nid] = name
            if name:
                nodes.append({"name": name, "entity_type": NODE_KIND[tag],
                              "properties": props, "def_id": nid})
        elif tag in REL_KIND:
            nid = _attr(el, "xmi:id", "id", "ID") or ""
            name = _attr(el, "declaredName", "name", "Name") or ""
            raw_nodes.append((nid, name, REL_KIND[tag], {"kind": xmitype or tag}, el))
        elif tag == "generalization":
            # 子类：generalization 子元素引用
            child = _attr(el, "general", "generalization", "general")
            specific = _attr(el, "specific", "specialized")
            if child and specific:
                edges.append({"source_name": _resolve_ref(specific, id2name),
                              "target_name": _resolve_ref(child, id2name),
                              "relation_type": "子类", "props": {"sysml": "generalization"}})

    # 2) 连接端点解析（SysML 2）：connectionUsage 的 connectorEnd / 引用
    for nid, name, kind, props, el in raw_nodes:
        if kind not in ("连接", "满足"):
            continue
        # satisfactionUsage：<type href=需求/> <target href=部件/> → 部件 --满足→ 需求
        if kind == "满足":
            type_ref = target_ref = ""
            for ce in el:
                ct = _local(ce.tag)
                if ct == "type":
                    type_ref = _attr(ce, "href", "resource", "xmi:idref", "id", "ID") or ""
                elif ct == "target":
                    target_ref = _attr(ce, "href", "resource", "xmi:idref", "id", "ID") or ""
            src_name = _resolve_ref(target_ref, id2name)
            tgt_name = _resolve_ref(type_ref, id2name)
            if src_name and tgt_name:
                edges.append({"source_name": src_name, "target_name": tgt_name,
                              "relation_type": "满足", "props": {"sysml": kind}})
            continue
        ends = []
        # a) connectorEnd 子元素（SysML 2；memberEnd 属 XMI，交给第 3 段处理）
        for ce in el:
            if _local(ce.tag) in ("connectorEnd", "end"):
                ref = _attr(ce, "type", "referencingFeature", "featuringType", "xmi:type",
                            "xmi:idref", "href", "id", "ID")
                ends.append(ref or "")
        # b) 子元素 <type href="#id"/>
        if not ends or not any(ends):
            for ce in el:
                if _local(ce.tag) == "type":
                    ends.append(_attr(ce, "href", "xmi:idref", "resource", "") or "")
        src_ref = ends[0] if ends else ""
        tgt_ref = ends[1] if len(ends) > 1 else ""
        src_name = _resolve_ref(src_ref, id2name)
        tgt_name = _resolve_ref(tgt_ref, id2name)
        if src_name and tgt_name:
            edges.append({"source_name": src_name, "target_name": tgt_name,
                          "relation_type": "连接", "props": {"sysml": kind, "def_id": nid}})
        elif name and " to " in name:
            # 兜底：declaredName 形如 "A to B"
            a, _, b = name.partition(" to ")
            if a.strip() and b.strip():
                edges.append({"source_name": a.strip(), "target_name": b.strip(),
                              "relation_type": "连接", "props": {"sysml": kind}})

    # 3) XMI Association（memberEnd xmi:idref → Property 的 type 引用）
    if not edges:
        assoc_ends = []  # [(assoc, [end_ref...])]
        for el in root.iter():
            tag = _local(el.tag)
            if tag in ("packagedElement", "ownedMember", "nestedElement", "member"):
                xt = _local(_attr(el, "xmi:type", "{http://www.omg.org/XMI}type") or "")
                if xt:
                    tag = xt
            if tag in ("Association", "Connector"):
                ends = [c.get("{http://www.omg.org/XMI}idref") or c.get("xmi:idref")
                        for c in el if _local(c.tag) in ("memberEnd", "end")]
                assoc_ends.append((el, [e for e in ends if e]))
        # Property → type 引用映射（xmi:id → 类型名）
        prop_type = {}
        for el in root.iter():
            tag = _local(el.tag)
            if tag in ("packagedElement", "ownedMember", "nestedElement", "member"):
                xt = _local(_attr(el, "xmi:type", "{http://www.omg.org/XMI}type") or "")
                if xt:
                    tag = xt
            if tag == "Property":
                pid = _attr(el, "xmi:id", "id")
                tref = _attr(el, "type", "xmi:type", "xmi:idref") or ""
                prop_type[pid] = tref
        for el, ends in assoc_ends:
            if len(ends) >= 2:
                src = _resolve_ref(prop_type.get(ends[0], ""), id2name)
                tgt = _resolve_ref(prop_type.get(ends[1], ""), id2name)
                rel = _attr(el, "name", "declaredName") or "连接"
                if src and tgt:
                    edges.append({"source_name": src, "target_name": tgt,
                                  "relation_type": rel, "props": {"sysml": "XMI association"}})

    return {"nodes": nodes, "edges": edges}


def _resolve_entity_type(conn, entity_type: str, name: str) -> str:
    """类型兜底：SysML 解析的类型不在本体时，用名称子串匹配本体实体类型，
    仍无匹配则用 '部件'（MBSE 通用，种子已含）。"""
    from ontology_semantics import OntologyValidator
    known = OntologyValidator(conn).entity_types()
    if entity_type in known:
        return entity_type
    for k in known:
        if k and k in name:
            return k
    if "部件" in known:
        return "部件"
    return entity_type


def sysml_to_candidates(conn, content, version_id: int = 0, model_name: str = "AI 建模",
                        source: str = "text", batch_id: str = "") -> dict:
    """AI 建模 SysML → v2g_candidates 候选（统一 v2g 候选治理入口，**不入库**）。

    解析 SysML（文本/JSON/XML 自动探测）→ 实体/关系候选写入 v2g_candidates
    （batch 前缀 SYSM-，source_doc 携带版本号，sysml_version_id 关联版本）
    + 本体预校验（validate_node，不合法提前标 rejected）
    + 消歧打标（matching_status / rel_matching_status，复用 v2g 判定）。

    返回 {batch_id, candidates, node_count, edge_count, rejected}
    """
    from ontology_semantics import OntologyValidator
    from vector2graph import _disambiguate_many, _disambiguate_rel, _normalize_mentions
    batch = batch_id or f"SYSM-{uuid.uuid4().hex[:8]}"
    # 1) 解析（文本/JSON/XML 自动探测，复用既有解析器）
    if source == "json" and isinstance(content, dict):
        if "views" in content:
            # sysml_views 格式 {views:{v:{nodes,edges}}} → 合并为标准 {nodes,edges}
            # （AI 建模消息归档的版本快照即此格式）
            # P0-1：跨视图按 name 去重——同一元素在多个视图（REQ/TRACE/BDD…）重复出现只保留一个候选；
            # P1-7：记录来源视图（properties.view，弹窗展示来源徽章，不入图库属性）
            vnodes, vedges = [], []
            id2name = {}
            seen_names = set()
            for vname, vd in (content.get("views") or {}).items():
                for n in (vd.get("nodes") or []):
                    nm = n.get("name") or n.get("id") or ""
                    if not nm:
                        continue
                    if n.get("id"):
                        id2name[str(n.get("id"))] = nm
                    if nm in seen_names:
                        continue
                    seen_names.add(nm)
                    props = dict(n.get("properties") or {})
                    if "view" not in props:
                        props["view"] = str(vname)
                    vnodes.append({
                        "name": nm,
                        "entity_type": n.get("entity_type") or n.get("type") or "部件",
                        "properties": props,
                    })
                for e in (vd.get("edges") or []):
                    s = e.get("source_name") or e.get("source") or ""
                    t = e.get("target_name") or e.get("target") or ""
                    # 引用 id → 解析为节点名（关系入库按名称定位两端实体）
                    s = id2name.get(str(s), s)
                    t = id2name.get(str(t), t)
                    eprops = dict(e.get("props") or {})
                    if "view" not in eprops:
                        eprops["view"] = str(vname)
                    vedges.append({
                        "source_name": s,
                        "target_name": t,
                        "relation_type": e.get("relation_type") or e.get("type") or "",
                        "props": eprops,
                    })
            # P2-8：容器/分组节点（需求清单/分组容器，SysML 视图分组语义）不入候选，
            # 其关联边一并剔除（减少噪音，避免「需求清单—包含→R-xxx」批量污染）
            _container_suffix = ("清单", "分组", "组", "Group", "group")
            _container_names = {n["name"] for n in vnodes
                                if n["name"].endswith(_container_suffix)}
            if _container_names:
                vnodes = [n for n in vnodes if n["name"] not in _container_names]
                vedges = [e for e in vedges
                          if e["source_name"] not in _container_names
                          and e["target_name"] not in _container_names]
            # 边去重：跨视图同三元组（s, rtype, t）只保留一个
            _seen_edges, _dedup_edges = set(), []
            for e in vedges:
                _key = (e["source_name"], e["relation_type"], e["target_name"])
                if _key in _seen_edges:
                    continue
                _seen_edges.add(_key)
                _dedup_edges.append(e)
            vedges = _dedup_edges
            parsed = {"nodes": vnodes, "edges": vedges}
        else:
            parsed = parse_json(content)
    elif isinstance(content, str):
        stripped = content.lstrip()
        if stripped.startswith("<"):
            parsed = parse_xml(content)
            if parsed.get("error"):
                parsed = _parse_v2(content)
        else:
            parsed = _parse_v2(content)
    else:
        return {"batch_id": batch, "candidates": [], "node_count": 0, "edge_count": 0,
                "rejected": [], "error": "无法解析输入"}
    if parsed.get("error"):
        return {"batch_id": batch, "candidates": [], "node_count": 0, "edge_count": 0,
                "rejected": [], "error": parsed["error"]}
    # P1-2 归一（2026-09-06）：关系名 canonical 化（core.relmap 单点事实来源）。
    # 中文关系名（包含/满足/连接/派生/追溯/子类）→ 英文标准名，与本体现行约束同源；
    # 废弃名（属于/执行/…）canonical() 返回 None → 边剔除并计入 rejected。
    # 位置在 parsed 三分支汇聚之后、候选落库之前；归一后再去重一次——
    # 视图路径的 dedup 在归一前，"包含"/"CONTAINS" 同边会双双存活，此处合并。
    from core.relmap import canonical as _rel_canon
    _norm_edges, _norm_reject = [], []
    for e in parsed.get("edges", []):
        orig = e.get("relation_type") or e.get("type") or ""
        _c = _rel_canon(orig)
        if _c is None:
            _norm_reject.append({
                "name": f"{e.get('source_name', '')}--{orig}-->{e.get('target_name', '')}",
                "reason": f"关系类型已废弃且无 canonical 映射: {orig}"})
            continue
        if _c != orig:
            props = e.get("props") if isinstance(e.get("props"), dict) else {}
            props["rel_normalized_from"] = orig
            e = dict(e)
            e["relation_type"] = _c
            e["props"] = props
        _norm_edges.append(e)
    _seen2, _dedup2 = set(), []
    for e in _norm_edges:
        _k = (e.get("source_name"), e.get("relation_type"), e.get("target_name"))
        if _k in _seen2:
            continue
        _seen2.add(_k)
        _dedup2.append(e)
    parsed["edges"] = _dedup2
    if _norm_reject:
        parsed.setdefault("rejected", []).extend(_norm_reject)
    ov = OntologyValidator(conn)
    known_types = set(ov.entity_types())
    src_label = f"AI建模·{model_name or '会话'}" + (f"·v{version_id}" if version_id else "")
    candidates, rejected = [], []
    name2type = {}
    # P0-2 表面归一：对全部节点/关系端点名批量归一（供消歧/确认定位，不改原名）
    _all_names = [n.get("name") for n in parsed.get("nodes", []) if n.get("name")] \
        + [e.get("source_name", "") for e in parsed.get("edges", [])] \
        + [e.get("target_name", "") for e in parsed.get("edges", [])]
    # P1 类型限定归一（SysML 通道接线）：节点类型经 _resolve_entity_type 归到本体类后传入，
    # glossary 绑定 entity_type 的词条仅对匹配类型折叠
    _sysml_type_map = {}
    for n in parsed.get("nodes", []):
        if n.get("name"):
            try:
                _sysml_type_map[n["name"]] = _resolve_entity_type(
                    conn, n.get("entity_type", "部件"), n.get("name", ""))
            except Exception:
                _sysml_type_map[n["name"]] = n.get("entity_type", "")
    norms = _normalize_mentions(conn, _all_names, types=_sysml_type_map)
    # 2) 实体候选（本体类型对齐 + 预校验 + 消歧）
    # P1-2 批量消歧：所有实体名一次建索引，循环内零全表扫
    disamb = _disambiguate_many(conn, [
        str(n.get("name", "")).strip() for n in parsed.get("nodes", []) if n.get("name")]) \
        if parsed.get("nodes") else {}
    # P2-10 批内消歧：跨视图去重后仍可能近似重复（载荷 vs 载荷分系统），
    # 批内包含关系（≥2 字）打标 properties.batch_dup，弹窗提示「批内疑似重复」
    _batch_names = [str(n.get("name", "")).strip()
                    for n in parsed.get("nodes", []) if n.get("name")]
    for n in parsed.get("nodes", []):
        if not n.get("name"):
            continue
        etype = _resolve_entity_type(conn, n.get("entity_type", "部件"), n.get("name", ""))
        name2type[n["name"]] = etype
        props = n.get("properties") or {}
        if not isinstance(props, dict):
            props = {"raw": str(props)[:500]}
        else:
            props = dict(props)
        errs = ov.validate_node(etype, props)
        status = "pending"
        if errs:
            status = "rejected"
            rejected.append({"name": n.get("name"), "errors": errs})
        m_status, m_eid = disamb.get(str(n["name"]).strip(), ("none", "")) \
            if status == "pending" else ("none", "")
        if status == "pending" and m_status == "none":
            nm = str(n["name"]).strip()
            for other in _batch_names:
                if other == nm or len(other) < 2 or len(nm) < 2:
                    continue
                if other in nm or nm in other:
                    m_status = "dup_suspect"
                    props["batch_dup"] = True
                    props["batch_dup_of"] = other
                    break
        conf = 0.9 if etype in known_types else 0.6
        cur = conn.execute(
            "INSERT INTO v2g_candidates (batch_id, source_doc, entity_name, entity_type, properties, "
            "status, errors, confidence, matching_status, match_entity_id, sysml_version_id, normalized_name, source_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, src_label, str(n["name"])[:80], etype, json.dumps(props, ensure_ascii=False),
             status, "; ".join(errs), conf, m_status, m_eid, version_id,
             norms.get(n["name"], n["name"]), "ai_model"))
        cid = cur.lastrowid
        candidates.append({"cid": cid, "name": n["name"], "entity_type": etype, "status": status,
                           "confidence": conf, "matching_status": m_status, "match_entity_id": m_eid,
                           "normalized_name": norms.get(n["name"], n["name"]),
                           "properties": props})
    # 3) 关系候选（三元组消歧打标；两端实体须已入库才能匹配）
    for e in parsed.get("edges", []):
        s_name = e.get("source_name") or ""
        t_name = e.get("target_name") or ""
        r_type = e.get("relation_type") or ""
        if not (s_name and t_name and r_type):
            continue
        # satisfy 方向归一化（项目约定：满足者(系统元素) → 需求；本体约束 src=部件族/tgt=需求）。
        # SysML "satisfy 需求 by 满足者" 解析方向为 需求→满足者，入库前统一反转。
        if r_type in ("满足", "satisfy", "satisfies", "SATISFIES"):
            s_t, t_t = name2type.get(s_name), name2type.get(t_name)
            if s_t == "需求" and t_t != "需求":
                s_name, t_name = t_name, s_name
        e_props = e.get("props") or {}
        if not isinstance(e_props, dict):
            e_props = {}
        rm_status, rm_rel_id = _disambiguate_rel(conn, s_name, t_name, r_type)
        e_conf = 0.85 if r_type else 0.6
        cur = conn.execute(
            "INSERT INTO v2g_candidates (batch_id, source_doc, entity_name, entity_type, properties, "
            "rel_type, rel_source, rel_target, status, confidence, rel_matching_status, rel_match_rel_id, sysml_version_id, source_type) "
            "VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?, ?, ?, 'ai_model')",
            (batch, src_label, f"{s_name} --{r_type}-- {t_name}"[:80], "关系候选",
             json.dumps(e_props, ensure_ascii=False), r_type, s_name, t_name,
             e_conf, rm_status, rm_rel_id, version_id))
        candidates.append({"cid": cur.lastrowid, "name": "关系候选", "entity_type": "关系候选",
                           "status": "pending", "confidence": e_conf, "matching_status": "none",
                           "match_entity_id": "", "rel_matching_status": rm_status,
                           "rel_match_rel_id": rm_rel_id, "rel_type": r_type,
                           "rel_source": s_name, "rel_target": t_name})
    conn.commit()
    # P1-2 归一剔除项汇入 rejected（废弃关系名的边不落候选，原因可追溯）
    rejected = rejected + [
        {"name": r["name"], "errors": [r["reason"]]} for r in parsed.get("rejected", [])
    ]
    return {"batch_id": batch, "candidates": candidates, "rejected": rejected,
            "node_count": len([c for c in candidates if c["entity_type"] != "关系候选"]),
            "edge_count": len([c for c in candidates if c["entity_type"] == "关系候选"])}


def import_sysml(conn, source: str, content: str | dict, imported_by: str = "系统工程师",
                 model_name: str = "") -> dict:
    """O-3：SysML 导入主入口——解析 → GraphStore 实例化（本体校验）→ 批次/映射落库。

    返回 {batch_id, status, imported, rejected, entity_count, relation_count}
    """
    batch = _new_batch()
    # 1) 解析
    if source == "json" and isinstance(content, dict):
        parsed = parse_json(content)
    elif source == "xml" and isinstance(content, str):
        parsed = parse_xml(content)
        if parsed.get("error"):
            return {"batch_id": batch, "status": "failed", "imported": 0, "rejected": [],
                    "entity_count": 0, "relation_count": 0, "detail": parsed["error"]}
    elif source in ("text", "upload") and isinstance(content, str):
        # upload 时自动探测：XML/XMI 走 XML 解析
        if content.lstrip().startswith("<"):
            parsed = parse_xml(content)
            if parsed.get("error"):
                parsed = _parse_v2(content)
        else:
            parsed = _parse_v2(content)
    else:
        return {"batch_id": batch, "status": "failed", "imported": 0, "rejected": [],
                "entity_count": 0, "relation_count": 0, "detail": "无法解析输入"}
    nodes = parsed["nodes"]
    edges = parsed["edges"]
    # P1-2 归一（2026-09-06）：落库路径同样 canonical 化——必须在下方预校验之前，
    # 否则废弃中文类型的旧约束（如"满足"）仍参与 domain/range 判定。
    from core.relmap import canonical as _rel_canon
    _norm_rejected = []
    _edges2 = []
    for e in edges:
        orig = e.get("relation_type") or "包含"
        _c = _rel_canon(orig)
        if _c is None:
            _norm_rejected.append({"edge": f"{e.get('source_name')}--{orig}-->{e.get('target_name')}",
                                   "errors": [f"关系类型已废弃且无 canonical 映射: {orig}"]})
            continue
        if _c != orig:
            props = dict(e.get("props") or {})
            props["rel_normalized_from"] = orig
            e = dict(e)
            e["relation_type"] = _c
            e["props"] = props
        _edges2.append(e)
    edges = _edges2

    # 2) 本体类型映射：类型解析兜底（子串匹配/通用部件）
    gs = GraphStore(conn)
    name_to_id = {}
    created_nodes, rejected = [], []
    rejected.extend(_norm_rejected)   # P1-2 归一剔除项并入（废弃关系名的边）
    for n in nodes:
        if not n.get("name"):
            continue
        nid = f"S-{uuid.uuid4().hex[:8]}"
        etype = _resolve_entity_type(conn, n.get("entity_type", "部件"), n.get("name", ""))
        ok, res = gs.create_node(nid, n["name"], etype,
                                 n.get("properties", {}), "dev")
        if ok:
            name_to_id[n["name"]] = nid
            created_nodes.append(nid)
            # 溯源标记
            conn.execute("UPDATE entities SET graph_source='sysml_import', sysml_import_id=? WHERE id=?",
                         (batch, nid))
            conn.execute("INSERT INTO sysml_sync (batch_id, sysml_ref, node_id) VALUES (?,?,?)",
                         (batch, n.get("def_id") or n["name"], nid))
        else:
            rejected.append({"name": n["name"], "errors": res})
    edge_ok = 0
    for e in edges:
        src = name_to_id.get(e.get("source_name", ""))
        tgt = name_to_id.get(e.get("target_name", ""))
        if not src or not tgt:
            # 目标可能是已存在实体（需求）
            if not tgt:
                ex = conn.execute("SELECT id FROM entities WHERE name=? AND status!='deprecated' LIMIT 1",
                                  (e.get("target_name", ""),)).fetchone()
                tgt = ex["id"] if ex else None
            if not src:
                ex = conn.execute("SELECT id FROM entities WHERE name=? AND status!='deprecated' LIMIT 1",
                                  (e.get("source_name", ""),)).fetchone()
                src = ex["id"] if ex else None
        if src and tgt:
            ok, res = gs.create_edge(src, tgt, e.get("relation_type", "包含"), e.get("props", {}), "dev")
            if ok:
                edge_ok += 1
            else:
                rejected.append({"edge": f"{e.get('source_name')}--{e.get('relation_type')}--{e.get('target_name')}",
                                 "errors": res})

    status = "done" if not rejected else "partial"
    conn.execute(
        "INSERT INTO sysml_imports (batch_id, model_name, source, entity_count, relation_count, status, detail, imported_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (batch, model_name or "未命名模型", source, len(created_nodes), edge_ok, status,
         f"拒绝 {len(rejected)} 项", imported_by))
    conn.commit()
    return {"batch_id": batch, "status": status, "imported": len(created_nodes),
            "rejected": rejected, "entity_count": len(created_nodes),
            "relation_count": edge_ok}


def list_imports(conn, limit: int = 20) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM sysml_imports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get_import(conn, batch_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sysml_imports WHERE batch_id=?", (batch_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["entities"] = [dict(r) for r in conn.execute(
        "SELECT id, name, entity_type, status, created_at FROM entities WHERE sysml_import_id=?",
        (batch_id,)).fetchall()]
    d["sync_map"] = [dict(r) for r in conn.execute(
        "SELECT sysml_ref, node_id, updated_at FROM sysml_sync WHERE batch_id=?", (batch_id,)).fetchall()]
    return d

# ─────────────────────────────────────────────────────────────────────────
# 归一校验报告（只读 · 2026-09-08 P0-1）
#
# 与 sysml_to_candidates 的差异：
#   1) **零副作用**：不写 v2g_candidates、不创建 batch、不 commit
#   2) 入参是 sysml_versions.content（{views:{name:{view,nodes,edges,warnings}}}），
#      不是 sysml_to_candidates 的 {views:{name:{nodes,edges}}}；做 schema 适配
#   3) 返回结构化报告给前端用于「per-row 人工确认」UI，不下发候选库
#
# 报告结构：
#   {
#     batch_id, source_version_id, source_message_id,
#     code_text,  # V2 源码（从 sysml_versions.code_text 或 messages.content 抽取）
#     summary: {total, aligned, merged, suspect, unmatched, rejected, edges_total, edges_dangling},
#     rows: [
#       {idx, kind:'entity'|'relation', view, name, normalized_name, entity_type,
#        matching_status, matched_entity_id, matched_entity_name, confidence, errors,
#        batch_dup_of, decision:'pending'} # decision 前端 in-memory 决策
#     ],
#     consistency: {batch_dups:[[a,b],...], dangling:[[s,t],...], type_conflicts:[...]}
#   }
#
# 匹配状态说明（与 vector2graph 消歧输出对齐）：
#   matched_high    — 高置信命中（≥0.85）已对齐规范名
#   matched_low     — 低置信命中（0.5-0.85）需人工复核
#   dup_high        — 与已存在实体高度同形，建议合并
#   dup_suspect     — 批内含关系疑似重复（载荷 vs 载荷分系统）
#   none            — 词典未命中，UI 标红
#   rejected        — 本体预校验失败（type 不合法 / 缺必填属性）
# ─────────────────────────────────────────────────────────────────────────
# ────────────────────── 归一报告行增强：本体类型 / 证据 / V2 定位 ──────────────────────
# 2026-09-08 人在回路改造：确认列表需要回答三个问题 ——
#   ① 这条实例对应本体模型里的哪个类/属性/关系？（ontology_type）
#   ② 系统凭什么给出这个建议？（evidence）
#   ③ 它在本次生成的 V2 代码里长什么样？（v2_ref，点击查看）
def _load_ontology_index(conn) -> dict:
    """载入 ontology_types（类/属性/关系）索引：
    {by_name:{name:(id,kind)}, by_kind:{kind:[(name,id)]}, props:{name:properties dict}}。
    props 供属性行的数据类型一致性比对（本体属性类型在 properties 声明 datatype）。"""
    idx = {"by_name": {}, "by_kind": {}, "props": {}}
    try:
        for r in conn.execute(
                "SELECT id, name, type_kind, properties FROM ontology_types "
                "WHERE COALESCE(status,'active')!='deprecated'"):
            nm = str(r["name"] or "").strip()
            if not nm:
                continue
            tk = str(r["type_kind"] or "entity").strip()
            idx["by_name"].setdefault(nm, (r["id"], tk))
            idx["by_kind"].setdefault(tk, []).append((nm, r["id"]))
            try:
                _p = json.loads(r["properties"] or "{}")
                if isinstance(_p, dict) and _p:
                    idx["props"][nm] = _p
            except Exception:
                pass
    except Exception:
        pass
    return idx


def _match_ontology_type(idx: dict, tk: str, inst: str, matched_name: str = "",
                         norm_name: str = "", entity_type: str = "") -> tuple:
    """实例名 → 本体类型（类/属性/关系名）。返回 (name, id, kind, how)。

    how: graph_entity 命中图库实体 / normalized 词典归一后精确 / exact 原名精确
         contains 模糊包含 / fallback 未命中（回退 entity_type 原始类型）。
    """
    by_name = idx.get("by_name") or {}
    for c, how in ((matched_name, "graph_entity"), (norm_name, "normalized"), (inst, "exact")):
        c = str(c or "").strip()
        if c and c in by_name:
            oid, okind = by_name[c]
            return c, oid, okind, how
    pool = (idx.get("by_kind") or {}).get(tk) or []
    inst_l = str(inst or "").strip()
    best = None
    for nm, oid in pool:
        if len(nm) < 2 or not inst_l:
            continue
        if nm in inst_l or inst_l in nm:
            if best is None or len(nm) > best[0]:
                best = (len(nm), nm, oid)
    if best:
        return best[1], best[2], tk, "contains"
    _fb = {"entity": str(entity_type or "").strip() or "实体（未匹配本体）",
           "attribute": "属性（未匹配本体）",
           "relation": "关系（未匹配本体）"}.get(tk, str(entity_type or "未分类").strip())
    return _fb, "", tk, "fallback"


_VDT_ALIASES = {"real": "real", "double": "real", "float": "real",
                "integer": "integer", "int": "integer",
                "string": "string", "text": "string",
                "boolean": "boolean", "bool": "boolean"}


def _vdt_eq(a: str, b: str) -> bool:
    """数据类型等价：大小写不敏感 + 常见别名归一（real/double/float 一族等）。"""
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return _VDT_ALIASES.get(a, a) == _VDT_ALIASES.get(b, b)


def _build_evidence(r: dict, ohow: str, onto_name: str) -> dict:
    """构造「为什么给这个建议」的证据与上下文（供人工判断可不可信）。"""
    src, rule = [], ""
    # 通用结构字段（stereotype/ports/value 等）不是业务术语，报「图库已有实体」属误导，跳过
    if not r.get("generic_key"):
        if r.get("matched_entity_name"):
            src.append(f"图库已有实体「{r['matched_entity_name']}」"
                       f"(#{r.get('matched_entity_id') or '-'})")
            rule = rule or "entity_match"
        elif r.get("matched_entity_id"):
            src.append(f"图库已有实体 #{r.get('matched_entity_id')}")
            rule = rule or "entity_match"
    if r.get("normalized_name") and r["normalized_name"] != r.get("name"):
        src.append(f"词典归一：「{r.get('name')}」→「{r['normalized_name']}」")
        rule = rule or "glossary"
    if r.get("batch_dup_of"):
        src.append(f"批内疑似重复：与「{r['batch_dup_of']}」互为子串")
        rule = rule or "batch_dup"
    if r.get("_cross_view_dup"):
        src.append("跨视图重复出现：" + "+".join(r.get("_views") or []))
        rule = rule or "cross_view"
    if r.get("generic_key"):
        src.append("通用结构字段（非业务术语，不参与归一）")
        rule = rule or "generic"
    for e in (r.get("errors") or [])[:2]:
        src.append("校验：" + str(e))
        rule = rule or "validation"
    if ohow in ("exact", "normalized"):
        src.append(f"本体类型精确匹配：{onto_name}")
        rule = rule or "ontology_exact"
    elif ohow == "contains":
        src.append(f"本体类型模糊匹配：{onto_name}")
        rule = rule or "ontology_fuzzy"
    elif ohow == "graph_entity":
        src.append(f"本体类型（由图库实体映射）：{onto_name}")
    if not src:
        src.append("本体 / 图库 / 词典均无命中，建议新建")
        rule = "new"
    ctx = []
    if r.get("view"):
        ctx.append("视图 " + str(r["view"]))
    if r.get("parent"):
        ctx.append("所属 " + str(r["parent"]))
    if r.get("entity_type"):
        ctx.append("类型 " + str(r["entity_type"]))
    if r.get("props_keys"):
        ctx.append("属性 " + ", ".join(str(x) for x in (r["props_keys"] or [])[:3]))
    return {"rule": rule or (r.get("matching_status") or "none"),
            "sources": src[:5], "context": " · ".join(ctx)}


def _locate_in_v2(name, lines: list, lower: list) -> dict:
    """在本次生成的 V2 源码里定位该实例所在行，返回 {found,line,snippet,hit_line}。"""
    if not name or not lines:
        return {"found": False, "line": 0, "line_end": 0, "snippet": "", "hit_line": 0}
    q = str(name).strip().lower()
    if not q:
        return {"found": False, "line": 0, "line_end": 0, "snippet": "", "hit_line": 0}
    for i, ln in enumerate(lower):
        if q in ln:
            st, en = max(0, i - 1), min(len(lines), i + 2)
            snip = "\n".join(lines[st:en])
            if len(snip) > 400:
                snip = snip[:400] + "…"
            return {"found": True, "line": i + 1, "line_end": i + 1,
                    "snippet": snip, "hit_line": i - st + 1}
    return {"found": False, "line": 0, "line_end": 0, "snippet": "", "hit_line": 0}


def build_normalize_report(conn, content, code_text: str = "",
                           version_id: int = 0, message_id: int = 0,
                           source: str = "json") -> dict:
    """归一校验报告（只读）。复用 sysml_to_candidates 的归一/消歧/校验逻辑，但不入库。

    入参 content 兼容两种形态：
      A) sysml_versions.content 直出：{views:{name:{view,nodes,edges,warnings}}}
      B) sysml_to_candidates 标准入参：{views:{name:{nodes,edges}}}

    节点字段兼容：name / entity_type / kind / type / properties / props
    边字段兼容：source_name / source / target_name / target / relation_type / type / props
    """
    import json as _json
    report_batch = f"NRPT-{uuid.uuid4().hex[:8]}"
    if not isinstance(content, dict) or "views" not in content:
        return {"batch_id": report_batch, "source_version_id": version_id,
                "source_message_id": message_id, "code_text": code_text,
                "summary": {"total": 0}, "rows": [], "consistency": {
                    "batch_dups": [], "dangling": [], "type_conflicts": []},
                "error": "内容不含 views 结构"}

    # 1) 适配 nodes/edges 字段名 → sysml_to_candidates 内部约定的字段
    norm_views = {}
    # 跨视图聚合索引（2026-09-08 修正）：
    #   实测 AI 生成的 V2 视图数据里，同一批元素常在多个视图各出现一次
    #   （例：BDD 100 节点 ∩ IBD 100 节点 = 100 完全重叠），同名且同 id。
    #   这不是 definition vs usage —— 实测 kind 字段全为 'part'，数据里
    #   根本没有 def/usage 语义标记。因此这里做「跨视图去重」：
    #   同名节点合并为一行，记录出现过的视图列表，避免报告虚高与重复入库。
    cross_view_index = {}   # name -> {views: [...], ids: [...], kinds: [...], n: 节点数}
    for vname, vd in (content.get("views") or {}).items():
        if not isinstance(vd, dict):
            continue
        nodes, edges = [], []
        for n in (vd.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            nm = n.get("name") or n.get("id") or ""
            if not nm:
                continue
            et = (n.get("entity_type") or n.get("type")
                  or n.get("kind") or "部件")
            props = n.get("properties") if isinstance(n.get("properties"), dict) else {}
            if n.get("attrs") and isinstance(n["attrs"], dict):
                props = {**n["attrs"], **props}
            nodes.append({"name": str(nm)[:80], "entity_type": str(et)[:40],
                          "properties": props})
            # 跨视图登记
            _rec = cross_view_index.setdefault(
                str(nm)[:80], {"views": [], "ids": [], "kinds": []})
            if vname not in _rec["views"]:
                _rec["views"].append(vname)
            _rid = str(n.get("id") or "").strip()
            if _rid and _rid not in _rec["ids"]:
                _rec["ids"].append(_rid)
            _rk = str(n.get("kind") or "").strip()
            if _rk and _rk not in _rec["kinds"]:
                _rec["kinds"].append(_rk)
        for e in (vd.get("edges") or []):
            if not isinstance(e, dict):
                continue
            s = e.get("source_name") or e.get("source") or ""
            t = e.get("target_name") or e.get("target") or ""
            rt = e.get("relation_type") or e.get("type") or "包含"
            ep = e.get("props") if isinstance(e.get("props"), dict) else {}
            if e.get("attrs") and isinstance(e["attrs"], dict):
                ep = {**e["attrs"], **ep}
            edges.append({"source_name": str(s)[:80], "target_name": str(t)[:80],
                          "relation_type": str(rt)[:40], "props": ep})
        norm_views[vname] = {"nodes": nodes, "edges": edges}

    try:
        from ontology_semantics import OntologyValidator
        from vector2graph import (_disambiguate_many, _disambiguate_rel,
                                  _normalize_mentions)
        from core.relmap import canonical as _rel_canon
        ov = OntologyValidator(conn)
        known_types = set(ov.entity_types())
        rows = []
        consistency = {"batch_dups": [], "dangling": [], "type_conflicts": []}
        all_node_names = set()
        for vname, vd in norm_views.items():
            for n in vd["nodes"]:
                all_node_names.add(n["name"])
        # 关系 canonical 化 + 边去重
        for vname, vd in norm_views.items():
            _norm_edges, _norm_reject = [], []
            for e in vd["edges"]:
                orig = e["relation_type"]
                _c = _rel_canon(orig)
                if _c is None:
                    _norm_reject.append({"name": f"{e['source_name']}--{orig}-->{e['target_name']}",
                                         "view": vname, "reason": f"关系类型已废弃: {orig}"})
                    continue
                if _c != orig:
                    e = dict(e); e["relation_type"] = _c
                    e["props"] = {**e.get("props", {}), "rel_normalized_from": orig}
                _norm_edges.append(e)
            _seen, _dedup = set(), []
            for e in _norm_edges:
                _k = (e["source_name"], e["relation_type"], e["target_name"])
                if _k in _seen:
                    continue
                _seen.add(_k); _dedup.append(e)
            norm_views[vname]["edges"] = _dedup
            for nr in _norm_reject:
                rows.append({
                    "idx": -1, "kind": "relation", "view": vname,
                    "name": nr["name"], "normalized_name": nr["name"],
                    "entity_type": "关系候选",
                    "matching_status": "rejected",
                    "matched_entity_id": "", "matched_entity_name": "",
                    "confidence": 0.0,
                    "errors": [nr["reason"]],
                    "batch_dup_of": "", "decision": "pending"})

        _all_names = []
        for vname, vd in norm_views.items():
            for n in vd["nodes"]:
                _all_names.append(n["name"])
            for e in vd["edges"]:
                _all_names.append(e["source_name"]); _all_names.append(e["target_name"])
        _type_map = {}
        for vname, vd in norm_views.items():
            for n in vd["nodes"]:
                try:
                    _type_map[n["name"]] = _resolve_entity_type(conn, n["entity_type"], n["name"])
                except Exception:
                    _type_map[n["name"]] = n["entity_type"]
        norms = _normalize_mentions(conn, _all_names, types=_type_map)
        disamb = _disambiguate_many(conn, list(all_node_names))

        # ── 跨视图去重（2026-09-08 修正）：同名节点只生成一行 ──
        # 实测：BDD/IBD 两视图节点 100% 重叠，此前按视图各生成一行导致
        #   ①报告行数虚高 ②用户看到同元素两次、误判为 def vs usage ③确认后重复入库
        # 合并后：view 字段为视图列表（"BDD+IBD"），cross_view_dup=True
        import re as _re2
        dedup_nodes, _seen_nm = [], set()
        for vname, vd in norm_views.items():
            for n in vd["nodes"]:
                if n["name"] in _seen_nm:
                    continue
                _seen_nm.add(n["name"])
                _cv = cross_view_index.get(n["name"]) or {}
                _views = _cv.get("views") or [vname]
                _kinds = _cv.get("kinds") or []
                # def/usage 判定：只认节点 kind 字段里的**显式 def 标记**（如 'part def'）。
                # 注意：kind='part' 在 SysML V2 里既可能是 part def 也可能是 part usage，
                #       单看 'part' 无法判定 —— 实测数据正是全为 'part'，故一律 unknown，
                #       不做任何推断（宁缺勿错，避免把同一元素误标成型/实例关系）。
                if any(_re2.search(r"\bdef\b|definition", k, _re2.I) for k in _kinds):
                    _kd = "definition"
                else:
                    _kd = "unknown"
                n = dict(n)
                n["_views"] = _views
                n["_cross_view_dup"] = len(_views) > 1
                n["_kind_detail"] = _kd
                dedup_nodes.append(n)

        idx = 0
        for n in dedup_nodes:
            vname = "+".join(n["_views"])
            if True:
                etype = _type_map.get(n["name"], n["entity_type"])
                props = dict(n.get("properties") or {})
                try:
                    errs = ov.validate_node(etype, props) or []
                except Exception:
                    errs = []
                if errs:
                    rows.append({
                        "idx": idx, "kind": "entity", "view": vname,
                        "name": n["name"],
                        "normalized_name": norms.get(n["name"], n["name"]),
                        "entity_type": etype,
                        "matching_status": "rejected",
                        "matched_entity_id": "", "matched_entity_name": "",
                        "confidence": 0.6,
                        "errors": list(errs),
                        "props_keys": list((props or {}).keys())[:4],
                        "_kind_detail": n["_kind_detail"],
                        "_views": n["_views"], "_cross_view_dup": n["_cross_view_dup"],
                        "batch_dup_of": "", "decision": "pending"})
                    idx += 1
                    continue
                m_status, m_eid = disamb.get(n["name"], ("none", ""))
                batch_dup_of = ""
                if m_status == "none":
                    nm = n["name"]
                    for other in all_node_names:
                        if other == nm or len(other) < 2 or len(nm) < 2:
                            continue
                        if other in nm or nm in other:
                            m_status = "dup_suspect"
                            batch_dup_of = other
                            consistency["batch_dups"].append([nm, other])
                            break
                matched_name = ""
                if m_eid:
                    r = conn.execute("SELECT name FROM entities WHERE id=?",
                                     (m_eid,)).fetchone()
                    if r:
                        matched_name = r["name"]
                conf = 0.9 if etype in known_types else 0.6
                if m_status in ("matched_high", "dup_high"):
                    conf = 0.92
                elif m_status in ("matched_low",):
                    conf = 0.7
                elif m_status == "dup_suspect":
                    conf = 0.55
                # 用户问题 #3：属性 keys；#5：跨视图去重 + kind 驱动的 def/usage
                _props_keys = list((props or {}).keys())[:4]
                rows.append({
                    "idx": idx, "kind": "entity", "view": vname,
                    "name": n["name"],
                    "normalized_name": norms.get(n["name"], n["name"]),
                    "entity_type": etype,
                    "matching_status": m_status,
                    "matched_entity_id": m_eid, "matched_entity_name": matched_name,
                    "confidence": conf, "errors": [],
                    "props_keys": _props_keys,
                    "_kind_detail": n["_kind_detail"],
                    "_views": n["_views"],
                    "_cross_view_dup": n["_cross_view_dup"],
                    "batch_dup_of": batch_dup_of, "decision": "pending"})
                idx += 1

        for vname, vd in norm_views.items():
            for e in vd["edges"]:
                s, t, rt = e["source_name"], e["target_name"], e["relation_type"]
                rm_status, rm_rid = _disambiguate_rel(conn, s, t, rt)
                dangling = (s not in all_node_names) or (t not in all_node_names)
                if dangling:
                    consistency["dangling"].append([s, t, rt])
                rel_matched = "none"
                _rel_disp = ""
                if rm_rid:
                    # 目标端展示（人在回路）：带出已有关系的端点，供用户判断合并去向
                    try:
                        _rr = conn.execute(
                            "SELECT re.name AS s, rt.name AS t, rel.relation_type AS rt "
                            "FROM relations rel "
                            "LEFT JOIN entities re ON re.id = rel.source_id "
                            "LEFT JOIN entities rt ON rt.id = rel.target_id "
                            "WHERE rel.id=?", (rm_rid,)).fetchone()
                    except Exception:
                        _rr = None
                    if _rr:
                        rel_matched = "matched_high"
                        _rel_disp = f"{_rr['s'] or '?'} --{_rr['rt']}--> {_rr['t'] or '?'}"
                rows.append({
                    "idx": idx, "kind": "relation", "view": vname,
                    "name": f"{s} --{rt}--> {t}",
                    "normalized_name": f"{s} --{rt}--> {t}",
                    "entity_type": "关系候选",
                    "matching_status": "rejected" if dangling else rel_matched,
                    "matched_entity_id": rm_rid, "matched_entity_name": _rel_disp,
                    "confidence": 0.85 if rt and not dangling else 0.6,
                    "errors": ["悬空引用（端点不在本批节点中）"] if dangling else [],
                    "props_keys": list((e.get("props") or {}).keys())[:4],
                    "_kind_detail": "usage",  # 关系行在 def/usage 计数中归 usage
                    "batch_dup_of": "", "decision": "pending"})
                idx += 1

        # ── 属性行（2026-09-08：属性参与归一，支撑前端「属性」tab）──
        # 每个实体的属性 key 生成一行 kind='attr'，用词典（GlossaryMatcher）判定命中：
        #   命中 → matched_high（建议采用规范名）；未命中 → none（建议新建词条）
        # 行 idx 追加在实体/关系之后，保持既有 idx 稳定（前端 decisions 以 idx 为键）。
        _ATTR_ROW_LIMIT = 200   # 报告体积保护：属性行上限，超出则截断并记 attr_truncated
        _attr_rows = 0
        # 通用结构键白名单： stereotype/ports/value 等是视图渲染字段，不是业务术语，
        # 不参与归一、不进词库 —— 否则「未命中 200 项」全是噪音，淹没真正需要拍板的属性。
        _GENERIC_KEYS = {
            "id", "name", "label", "type", "kind", "value", "unit", "desc", "description",
            "note", "notes", "doc", "comment", "status", "state", "version", "tags", "tag",
            "stereotype", "ports", "port", "props", "attrs", "x", "y", "width", "height",
            "color", "shape", "source", "target", "parent", "children", "visible", "owner",
            "created", "updated", "namespace", "package", "alias", "multiplicity", "default",
            "min", "max", "index", "order", "weight", "score", "count", "总数", "名称", "类型",
            "描述", "备注", "单位", "状态", "数量", "上限", "下限", "默认值", "编号",
        }
        try:
            from glossary import GlossaryMatcher
            _gm = GlossaryMatcher(conn)
            _acache, _aseen = {}, set()
            for n in dedup_nodes:
                if _attr_rows >= _ATTR_ROW_LIMIT:
                    break
                props = n.get("properties") or {}
                for pk in list(props.keys())[:8]:
                    if _attr_rows >= _ATTR_ROW_LIMIT:
                        break
                    _k = (n["name"], pk)
                    if _k in _aseen:
                        continue
                    _aseen.add(_k)
                    # 属性值（仅用于人工确认展示，不参与改名/入库）
                    try:
                        _pv = props.get(pk)
                        _av = "" if _pv is None else str(_pv)
                        if len(_av) > 48:
                            _av = _av[:48] + "…"
                    except Exception:
                        _av = ""
                    if pk in _acache:
                        hit, canon, generic = _acache[pk]
                    else:
                        generic = str(pk).strip().lower() in _GENERIC_KEYS
                        if generic:
                            hit, canon = True, pk      # 通用结构键：视为已对齐，不改不入库
                        else:
                            try:
                                _r = _gm.resolve(pk)
                                _hits = _r.get("hits") or []
                                hit = bool(_hits)
                                canon = (_hits[0].get("canonical_term")
                                         or _r.get("normalized") or pk) if hit else pk
                            except Exception:
                                hit, canon = False, pk
                        _acache[pk] = (hit, canon, generic)
                    rows.append({
                        "idx": idx, "kind": "attr",
                        "view": "+".join(n.get("_views") or []),
                        "name": pk, "normalized_name": canon,
                        "entity_type": f"属性·{n['name']}",
                        "matching_status": "matched_high" if hit else "none",
                        "matched_entity_id": "",
                        "matched_entity_name": canon if hit else "",
                        "confidence": 0.8 if hit else 0.5,
                        "errors": [] if hit else ["词典未命中（可新建词条或忽略）"],
                        "generic_key": generic,
                        "props_keys": [pk], "parent": n["name"],
                        "_kind_detail": "attr", "_views": n.get("_views") or [],
                        "_cross_view_dup": False,
                        "batch_dup_of": "", "decision": "pending"})
                    idx += 1
                    _attr_rows += 1
        except Exception:
            pass

        # ── 行字段增强（人在回路确认）：本体类型 / 证据 / V2 代码定位 ──
        _onto = _load_ontology_index(conn)
        _code_lines = (code_text or "").splitlines()
        _code_lower = [str(x).lower() for x in _code_lines]
        # 实体名 → 本体类型（属性行显示「本体名称-属性名称」用）。
        # 属性行按构造排在实体行之后，故在增强循环内增量填充即可。
        _entity_onto = {}
        # 属性行数据类型提取（V2 代码声明：attribute X : Type;）—— 名称+数据类型双维比对
        _attr_type_cache = {}

        def _attr_decl_type(aname: str) -> str:
            if aname in _attr_type_cache:
                return _attr_type_cache[aname]
            t = ""
            try:
                m = re.search(r"attribute\s+(?:def\s+)?" + re.escape(str(aname))
                              + r"\s*:\s*([\w][\w.]*)", (code_text or ""))
                t = (m.group(1) if m else "").strip()
            except Exception:
                t = ""
            _attr_type_cache[aname] = t
            return t

        def _onto_datatype(onto_name: str) -> str:
            """本体属性类型声明中的数据类型（properties.data_type/datatype/value_type）。"""
            p = (_onto.get("props") or {}).get(str(onto_name or "").strip()) or {}
            for k in ("data_type", "datatype", "value_type", "数据类型"):
                v = p.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, dict) and str(v.get("type") or "").strip():
                    return str(v["type"]).strip()
            return ""

        for r in rows:
            _inst = r.get("name") or ""
            r["instance_name"] = _inst
            # 实例名称按类型结构化呈现（人在回路确认 · 用户要求）：
            #   实体 → 实例名称；属性 → 实体名 + 属性名 + 属性值；关系 → 源端 + 类型 + 目标端
            _kind = r.get("kind") or "entity"
            if _kind == "attr":
                r["attr_entity"] = r.get("attr_entity") or r.get("parent") or ""
                r["attr_name"] = r.get("attr_name") or _inst
                r["attr_value"] = r.get("attr_value") or ""
                r["instance_display"] = (
                    f"{r['attr_entity']} · {r['attr_name']}"
                    + (f" = {r['attr_value']}" if r["attr_value"] else ""))
            elif _kind == "relation":
                _mm = re.match(r"^(.*?)\s+--(.*?)-->\s*(.*)$", str(_inst))
                r["rel_source"] = (_mm.group(1).strip() if _mm else str(_inst))
                r["rel_type"] = (_mm.group(2).strip() if _mm else "")
                r["rel_target"] = (_mm.group(3).strip() if _mm else "")
                r["instance_display"] = _inst
            else:
                r["instance_display"] = _inst
            _tk = {"entity": "entity", "attr": "attribute",
                   "relation": "relation"}.get(_kind, "entity")
            _on, _oid, _okind, _ohow = _match_ontology_type(
                _onto, _tk, _inst, r.get("matched_entity_name") or "",
                r.get("normalized_name") or "", r.get("entity_type") or "")
            r["ontology_type"] = _on
            r["ontology_type_id"] = _oid
            r["ontology_type_kind"] = _okind
            r["ontology_matched"] = _ohow != "fallback"
            r["ontology_match_by"] = _ohow
            if _kind == "entity" and r.get("name"):
                _entity_onto[r["name"]] = _on
            # 属性行：显示「本体名称-属性名称」（本体名称 = 所属实体的本体类型）
            if _kind == "attr":
                _owner_onto = (_entity_onto.get(r.get("attr_entity") or r.get("parent"))
                               or "").strip()
                r["attr_type"] = _attr_decl_type(r.get("attr_name") or _inst)
                if _owner_onto:
                    r["attr_onto_display"] = f"{_owner_onto}-{r.get('attr_name') or _inst}"
                else:
                    r["attr_onto_display"] = ""
                # 名称命中后做数据类型一致性比对（本体/词典声明了数据类型且 V2 有声明时）
                _v2dt = r.get("attr_type") or ""
                _onto_dt = _onto_datatype(_on if r["ontology_matched"] else "")
                r["attr_type_check"] = ""
                if _v2dt and _onto_dt:
                    if _vdt_eq(_v2dt, _onto_dt):
                        r["attr_type_check"] = "match"
                        r["confidence"] = min(0.99, round(float(r.get("confidence") or 0.8) + 0.05, 2))
                    else:
                        r["attr_type_check"] = "mismatch"
                        r["confidence"] = max(0.4, round(float(r.get("confidence") or 0.8) - 0.2, 2))
                        if r.get("matching_status") == "matched_high":
                            r["matching_status"] = "matched_low"
                            r["errors"] = list(r.get("errors") or []) + [
                                f"数据类型不一致：V2 声明 {_v2dt} vs 本体 {_onto_dt}"]
            r["evidence"] = _build_evidence(r, _ohow, _on)
            if _kind == "attr" and (r.get("attr_type_check") or r.get("attr_type")):
                _v2dt = r.get("attr_type") or ""
                _onto_dt = _onto_datatype(_on if r["ontology_matched"] else "")
                ev = r["evidence"]
                if r["attr_type_check"] == "match":
                    ev["sources"] = list(ev.get("sources") or []) + [f"数据类型一致：{_v2dt}"]
                elif r["attr_type_check"] == "mismatch":
                    ev["sources"] = list(ev.get("sources") or []) + [
                        f"数据类型不一致：V2 声明 {_v2dt} ≠ 本体声明 {_onto_dt}（建议人工核对）"]
                    ev["rule"] = ev.get("rule") or "attr_type_mismatch"
                elif _v2dt:
                    ev["sources"] = list(ev.get("sources") or []) + [
                        f"V2 声明数据类型 {_v2dt}（比对目标未声明数据类型，仅按名称比对）"]
            _key = _inst
            if r.get("kind") == "relation":
                _key = str(_inst).split(" --")[0].strip() or _inst
            r["v2_ref"] = _locate_in_v2(_key, _code_lines, _code_lower)

        # 用户问题 #3：属性参与度统计；#5：跨视图去重 + def/usage 计数
        s = {"total": len(rows), "entities": 0, "entities_with_props": 0,
             "relations": 0, "relations_with_props": 0,
             "definitions": 0, "usages": 0,
             "attrs": 0, "attrs_matched": 0,
             "attr_truncated": _attr_rows >= _ATTR_ROW_LIMIT,
             "cross_view_dups": sum(1 for r in rows if r.get("_cross_view_dup")),
             "raw_node_count": len(_seen_nm),  # 去重后实际元素数
             "aligned": 0, "merged": 0, "suspect": 0, "unmatched": 0, "rejected": 0,
             "props_total": 0, "props_failed": 0,
             "edges_dangling": len(consistency["dangling"])}
        for r in rows:
            if r["kind"] == "attr":
                s["attrs"] += 1
                if r["matching_status"] == "matched_high":
                    s["attrs_matched"] += 1
                if r["matching_status"] == "matched_high":
                    s["aligned"] += 1
                elif r["matching_status"] == "none":
                    s["unmatched"] += 1
                continue
            if r["kind"] == "entity":
                s["entities"] += 1
                if r.get("props_keys"):
                    s["entities_with_props"] += 1
                    s["props_total"] += len(r["props_keys"])
                if r.get("_kind_detail") == "definition":
                    s["definitions"] += 1
                elif r.get("_kind_detail") == "usage":
                    s["usages"] += 1
            else:
                s["relations"] += 1
                if r.get("props_keys"):
                    s["relations_with_props"] += 1
                    s["props_total"] += len(r["props_keys"])
            ms = r["matching_status"]
            if ms == "matched_high":
                s["aligned"] += 1
            elif ms in ("dup_high", "matched_low"):
                s["merged"] += 1
            elif ms == "dup_suspect":
                s["suspect"] += 1
            elif ms == "none":
                s["unmatched"] += 1
            elif ms == "rejected":
                s["rejected"] += 1
            if r.get("matching_status") == "rejected" and any(
                "必填" in (e or "") or "缺少" in (e or "") or "invalid" in (e or "").lower()
                for e in (r.get("errors") or [])
            ):
                s["props_failed"] += 1
        return {"batch_id": report_batch, "source_version_id": version_id,
                "source_message_id": message_id, "code_text": code_text,
                "summary": s, "rows": rows, "consistency": consistency}
    except Exception as e:
        return {"batch_id": report_batch, "source_version_id": version_id,
                "source_message_id": message_id, "code_text": code_text,
                "summary": {"total": 0}, "rows": [], "consistency": {
                    "batch_dups": [], "dangling": [], "type_conflicts": []},
                "error": f"归一报告生成失败: {e}"}
