# -*- coding: utf-8 -*-
"""SysML v1/v2 视图投影层（View Generator）。

从图谱（entities/relations）按视图类型投影为「视图模型 ViewModel」，
前端据此渲染 SVG 预览。视图类型覆盖 OMG SysML v1 九图 + v2 视图族，
并与知识库「运行视角-视图规范元素要求」文档的 11 个视图建立映射。

投影原则：
- 消费侧只读：默认只消费已发布(release)分支；显式传 branch 时按其过滤。
- 视图 = 节点筛选 + 边筛选 + 最小输入集校验（缺失元素 → warnings，不阻断）。
- ViewModel 结构统一：{view, nodes, edges, warnings}，前端按 kind 渲染。
"""
from __future__ import annotations

import json

from database import get_db


# ──────────────────────────────────────────────
# 视图类型注册表（v1 图 + v2 视图族 + 知识库视图映射）
# ──────────────────────────────────────────────
VIEW_TYPES = {
    # id: {name, v1(OMG SysML v1 图), v2(SysML v2 视图族), kind, kb(知识库11视图), desc}
    "BDD": {
        "name": "块定义图",
        "v1": "Block Definition Diagram",
        "v2": "Structural View / Definition View",
        "kind": "结构",
        "kb": "场景组成结构视图",
        "desc": "展示系统层级结构与部件间组合/引用关系（part def/part）。",
    },
    "IBD": {
        "name": "内部块图",
        "v1": "Internal Block Diagram",
        "v2": "Internal Connection View",
        "kind": "结构",
        "kb": "系统运行交互视图",
        "desc": "展示块内部部件与端口连接关系（part/port/connect）。",
    },
    "REQ": {
        "name": "需求图",
        "v1": "Requirement Diagram",
        "v2": "Requirement View",
        "kind": "需求",
        "kb": "利益相关方需求视图",
        "desc": "展示需求条目及其派生/满足/冲突关系（requirement/derive/satisfy）。",
    },
    "UC": {
        "name": "用例图",
        "v1": "Use Case Diagram",
        "v2": "Case View",
        "kind": "行为",
        "kb": "用户用例视图",
        "desc": "展示参与者与系统用例的交互边界（actor/use case）。",
    },
    "ACT": {
        "name": "活动图",
        "v1": "Activity Diagram",
        "v2": "Behavioral View (Activity)",
        "kind": "行为",
        "kb": "运行场景视图",
        "desc": "展示动作流与并发/分支控制（action/then/decide）。",
    },
    "SEQ": {
        "name": "顺序图",
        "v1": "Sequence Diagram",
        "v2": "Behavioral View (Sequence)",
        "kind": "行为",
        "kb": "运行资源交互接口及信息视图",
        "desc": "展示参与者/部件间的消息时序（lifeline/message）。",
    },
    "STM": {
        "name": "状态机图",
        "v1": "State Machine Diagram",
        "v2": "Behavioral View (State)",
        "kind": "行为",
        "kb": "运行场景视图",
        "desc": "展示状态及状态迁移（state/transition）。",
    },
    "PAR": {
        "name": "参数图",
        "v1": "Parametric Diagram",
        "v2": "Constraint View",
        "kind": "结构",
        "kb": "定义运行分析用例",
        "desc": "展示约束表达式与参数绑定关系（constraint/bind）。",
    },
    "PKG": {
        "name": "包图",
        "v1": "Package Diagram",
        "v2": "Package / Containment View",
        "kind": "结构",
        "kb": "视图组织（包划分）",
        "desc": "展示模型包的嵌套组织（package/containment）。",
    },
    "TRACE": {
        "name": "追溯视图",
        "v1": "Traceability Matrix / Allocation",
        "v2": "Traceability View",
        "kind": "需求",
        "kb": "追溯视图",
        "desc": "展示需求→设计→实现的跨层追溯（satisfy/derive/allocate）。",
    },
}

# v1 九图顺序（OMG SysML 1.x 标准图类型）
V1_DIAGRAMS = ["BDD", "IBD", "REQ", "UC", "ACT", "SEQ", "STM", "PAR", "PKG"]

# 视图 kind → 前端布局提示
KIND_LABEL = {"结构": "结构", "行为": "行为", "需求": "需求"}

# 实体类型 → 视图元素 kind 映射（投影语义层）
ENTITY_KIND_MAP = {
    # SysML v1/v2 元素kind：entity_type 命中 → 元素kind
    "需求": "requirement",
    "部件": "part",
    "载荷": "block",       # 领域具体类型按 block 渲染
    "转发器": "block",
    "TWTA": "block",
    "天线": "block",
    "系统": "block",
    "actor": "actor",
    "参与者": "actor",
    "use case": "usecase",
    "用例": "usecase",
    "action": "action",
    "动作": "action",
    "功能": "action",
    "state": "state",
    "状态": "state",
    "constraint": "constraint",
    "约束": "constraint",
    "parameter": "parameter",
    "参数": "parameter",
    "package": "package",
    "包": "package",
    "端口": "port",
    "项": "item",
    "interface": "interface",
    "接口": "interface",
    "occurrence": "occurrence",
    "发生": "occurrence",
    "event": "event",
    "事件": "event",
    "data": "data",
    "数据": "data",
    "analysis": "analysis",
    "分析": "analysis",
    "calculation": "calculation",
    "计算": "calculation",
    "view": "view",
    "视图": "view",
    "viewpoint": "viewpoint",
    "视角": "viewpoint",
    "metadata": "metadata",
    "元数据": "metadata",
    "属性": "attribute",
    "flow": "flow",
    "流": "flow",
    "lifeline": "lifeline",
}

# 关系类型 → 边 kind 映射
REL_KIND_MAP = {
    "CONTAINS": "composition",      # 组合/包含
    "包含": "composition",
    "SATISFIES": "satisfy",          # 满足
    "满足": "satisfy",
    "CONFLICTS": "conflict",         # 冲突
    "冲突": "conflict",
    "derive": "derive",              # 推导
    "DERIVE": "derive",
    "派生": "derive",
    "include": "include",
    "INCLUDE": "include",
    "extend": "extend",
    "EXTEND": "extend",
    "allocate": "allocate",
    "ALLOCATE": "allocate",
    "分配": "allocate",
    "connect": "connect",
    "CONNECT": "connect",
    "连接": "connect",
    "transition": "transition",
    "TRANSITION": "transition",
    "迁移": "transition",
    "message": "message",
    "MESSAGE": "message",
    "消息": "message",
    "flow": "flow",
    "FLOW": "flow",
    "流向": "flow",
    "顺序": "flow",
    "验证": "verify",
    "verify": "verify",
    "VERIFY": "verify",
    "参与": "assoc",
    "执行": "assoc",
    "别名": "alias",
    "alias": "alias",
    "导入": "dependency",
    "import": "dependency",
    "绑定": "bind",
    "binding": "bind",
    "接续": "succession",
    "succession": "succession",
    "dependency": "dependency",
    "DEPENDENCY": "dependency",
    "DEPENDS_ON": "dependency",
    "依赖": "dependency",
}

# 各视图需要的元素 kind（最小输入集校验）
VIEW_REQUIRED_KINDS = {
    "BDD": ["part", "block"],
    "IBD": ["part", "block", "port"],
    "REQ": ["requirement"],
    "UC": ["actor", "usecase"],
    "ACT": ["action"],
    "SEQ": ["part", "actor", "block"],
    "STM": ["state"],
    "PAR": ["constraint"],
    "PKG": ["package", "block", "part"],
    "TRACE": ["requirement"],
}


def _release_branches(conn) -> list:
    """已发布(release)分支列表：branches 表 branch_type='release'，无则回退字面 'release'。"""
    try:
        rows = conn.execute(
            "SELECT name FROM branches WHERE branch_type='release' AND status='active'"
        ).fetchall()
        names = [r["name"] for r in rows]
        if names:
            return names
    except Exception:
        pass
    return ["release"]


def _project_id_of(conn) -> str:
    """取当前项目 id：**用户配置的默认项目**，未配置返回空串。

    2026-09-20：原实现是「`SELECT id FROM projects LIMIT 1`（**任意取一条**），失败则回退
    硬编码 `'project-satnet-broadband'`」—— 两者都与「当前项目」无关，且会让视图生成指向一个
    已归档项目。改为与写入链同源（repositories.project_repo.resolve_project_id）。
    """
    from repositories.project_repo import resolve_project_id
    return resolve_project_id(conn)


def _load_graph(conn, branch: str | None, project_id: str):
    """读取当前分支（或 release）下的实体与关系。"""
    if branch:
        branches = [branch]
    else:
        branches = _release_branches(conn) or ["dev"]
    ph = ",".join("?" * len(branches))

    entities = []
    seen_ids = set()
    rows = conn.execute(
        f"SELECT id,name,entity_type,properties,branch FROM entities "
        f"WHERE status != 'deprecated' AND branch IN ({ph}) ORDER BY entity_type, name",
        branches,
    ).fetchall()
    for r in rows:
        eid = r["id"]
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        entities.append({
            "id": eid,
            "name": r["name"],
            "entity_type": r["entity_type"],
            "properties": _parse_json(r["properties"] or "{}"),
            "branch": r["branch"],
        })

    rels = []
    seen_rels = set()
    rrows = conn.execute(
        f"SELECT id,source_id,target_id,relation_type,properties,branch FROM relations "
        f"WHERE status != 'deprecated' AND branch IN ({ph})",
        branches,
    ).fetchall()
    for r in rrows:
        key = (r["source_id"], r["target_id"], r["relation_type"])
        if key in seen_rels:
            continue
        seen_rels.add(key)
        rels.append({
            "id": r["id"],
            "source": r["source_id"],
            "target": r["target_id"],
            "type": r["relation_type"],
            "properties": _parse_json(r["properties"] or "{}"),
            "branch": r["branch"],
        })
    return entities, rels


def _parse_json(s):
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def _elem_kind(entity: dict) -> str:
    # 优先 properties.kind（解析器精确标记 lifeline/use_case/state 等）；无则按 entity_type 映射
    props = entity.get("properties") or {}
    pk = props.get("kind") or props.get("entity_type") or ""
    _prop_map = {"lifeline": "lifeline", "use_case": "usecase", "actor": "actor",
                 "state": "state", "constraint": "constraint", "package": "package",
                 "action": "action", "port": "port", "part": "part",
                 "requirement": "requirement", "block": "block", "attribute": "attribute",
                 "parameter": "parameter", "item": "item", "interface": "interface",
                 "occurrence": "occurrence", "event": "event", "data": "data",
                 "analysis": "analysis", "calculation": "calculation", "view": "view",
                 "viewpoint": "viewpoint", "metadata": "metadata", "flow": "flow"}
    if pk in _prop_map:
        return _prop_map[pk]
    et = (entity.get("entity_type") or "").strip()
    # 精确映射 → 类型名包含映射 → 默认 block
    if et in ENTITY_KIND_MAP:
        return ENTITY_KIND_MAP[et]
    for key, kind in ENTITY_KIND_MAP.items():
        if key and key in et:
            return kind
    return "block"


def _edge_kind(rtype: str) -> str:
    return REL_KIND_MAP.get(rtype, "dependency")


def _view_node(entity: dict) -> dict:
    props = entity.get("properties", {})
    return {
        "id": entity["id"],
        "name": entity["name"],
        "kind": _elem_kind(entity),
        "type": entity.get("entity_type", ""),
        "attrs": {
            "stereotype": props.get("stereotype", ""),
            "ports": props.get("ports", []),
            "value": props.get("value", ""),
            # usage 类型引用（IBD 端口继承用）：part cabin : Cabin → ref_type=Cabin
            "ref_type": props.get("part_type") or props.get("port_type") or "",
        },
    }


def generate_view(view_type: str, branch: str | None = None, project_id: str | None = None) -> dict:
    """按视图类型投影图谱为 ViewModel。

    :param view_type: VIEW_TYPES 的 id（如 BDD/REQ/UC...）
    :param branch: 指定分支；None → 默认消费 release 已发布分支
    :return: {view, nodes, edges, warnings}
    """
    vt = VIEW_TYPES.get(view_type or "")
    if not vt:
        return {
            "view": {"type": view_type or "", "name": "未知视图", "v1": "", "v2": "", "kind": "", "kb": "", "desc": ""},
            "nodes": [], "edges": [], "warnings": [f"不支持的视图类型：{view_type}，可选：{', '.join(VIEW_TYPES)}"],
        }

    conn = get_db()
    if not project_id:
        project_id = _project_id_of(conn)
    entities, rels = _load_graph(conn, branch, project_id)

    nodes, edges, warnings = _project(view_type, entities, rels, conn)
    return _finalize(view_type, nodes, edges, warnings)


def generate_views_from_sysml(code_text: str, view_types: list | None = None, intent: str | None = None) -> dict:
    """AI 建模联动：将 LLM 生成的 SysML v2 代码解析为内存图谱并投影各视图 ViewModel。

    不落库、不依赖已有图谱——直接预览「本次生成代码」对应的视图。
    内部复用 sysml_importer.parse_text（文本符号 → nodes/edges）与 _project（视图投影）。

    :param code_text: SysML v2 代码文本（可含多个代码块，自动拼接）
    :param view_types: 视图类型子集；None → 按意图映射或全部 10 种
    :param intent: 意图识别结果；命中 INTENT_VIEWS 时只投影该意图匹配的视图类型
    :return: {"parsed": {"nodes": n, "edges": m}, "views": {type: ViewModel, ...}, "intent": intent}
    """
    try:
        from sysml_importer import parse_text
        parsed = parse_text(code_text)
    except Exception:
        parsed = {"nodes": [], "edges": []}
    nodes_in = parsed.get("nodes") or []
    edges_in = parsed.get("edges") or []

    entities = []
    for i, n in enumerate(nodes_in):
        eid = str(n.get("def_id") or n.get("name") or f"n{i}")
        entities.append({
            "id": eid,
            "name": str(n.get("name") or eid),
            "entity_type": str(n.get("entity_type") or "部件"),
            "properties": n.get("properties") or {},
        })
    name2id = {e["name"]: e["id"] for e in entities}
    rels = []
    for i, r in enumerate(edges_in):
        src, tgt = name2id.get(str(r.get("source_name") or "")), name2id.get(str(r.get("target_name") or ""))
        if not src or not tgt:
            continue
        rels.append({
            "id": f"r{i}",
            "source": src,
            "target": tgt,
            "type": str(r.get("relation_type") or "依赖"),
            "properties": r.get("props") or {},
        })

    # 视图类型选择：显式指定 > 意图映射 > 全量 10 种
    types = view_types
    if not types:
        types = INTENT_VIEWS.get(intent) if intent else None
    if not types:
        types = list(VIEW_TYPES)

    views = {}
    for vt in types:
        if vt not in VIEW_TYPES:
            continue
        nodes, edges, warnings = _project(vt, entities, rels, None)
        views[vt] = _finalize(vt, nodes, edges, warnings)
    return {"parsed": {"nodes": len(entities), "edges": len(rels)}, "views": views, "intent": intent}


# 意图识别 → 视图类型映射（AI 建模按意图只输出匹配视图，避免全量 10 图噪音）
INTENT_VIEWS = {
    "requirement_analysis": ["REQ", "TRACE"],            # 需求分析：需求视图 + 追溯视图
    "design": ["BDD", "IBD", "PKG", "PAR"],              # 方案设计：结构族（块定义/内部块/包/参数）
    "impact": ["TRACE", "REQ"],                          # 变更影响：追溯链路 + 需求
    "review": ["REQ", "BDD", "UC"],                      # 预评审：需求/结构/用例规范性校验
    "report_generation": ["BDD", "REQ", "UC", "ACT", "TRACE"],  # 报告：核心结构/需求/行为/追溯
    "knowledge_qa": [],                                  # 知识问答：不生成视图
    "chat": [],                                          # 通用问答：不生成视图
}


def _finalize(view_type: str, nodes: list, edges: list, warnings: list) -> dict:
    """统一收尾：过滤无效边 → 最小输入集校验 → 组装 ViewModel（DB 投影与代码投影共用）。"""
    vt = VIEW_TYPES.get(view_type)
    # 边有效性兜底：两端节点必须都在本视图节点集内
    nids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in nids and e["target"] in nids]

    # 最小输入集校验
    required = VIEW_REQUIRED_KINDS.get(view_type, [])
    have = {n["kind"] for n in nodes}
    missing = [k for k in required if k not in have]
    if missing:
        warnings.append("视图最小输入集缺失：" + "、".join(missing) + "（请先补充对应模型元素）")

    return {
        "view": {
            "type": view_type,
            "name": vt["name"],
            "v1": vt["v1"],
            "v2": vt["v2"],
            "kind": vt["kind"],
            "kb": vt["kb"],
            "desc": vt["desc"],
        },
        "nodes": nodes,
        "edges": edges,
        "warnings": warnings,
    }


def _project(view_type: str, entities: list, rels: list, conn) -> tuple:
    """各视图类型投影：返回 (nodes, edges, warnings)。"""
    warnings = []
    emap = {e["id"]: e for e in entities}
    # 关系仅保留两端都在实体集内的
    valid_rels = [r for r in rels if r["source"] in emap and r["target"] in emap]

    if view_type == "BDD":
        # 结构层级：部件/块/参与者 + 组合/依赖边（端口是部件特征，不独立显示）
        sel = [e for e in entities if _elem_kind(e) in ("block", "part", "actor")]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("composition", "dependency"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "IBD":
        # 内部连接：仅部件/块节点；端口不独立显示——端口是部件边界特征（规范 7.12），
        # 由渲染层按节点 attrs.ports 在部件边界画端口方块，connect 边 props.ports 锚定
        sel = [e for e in entities if _elem_kind(e) in ("block", "part")]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("connect", "composition"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "REQ":
        # 需求视图：requirement 节点 + satisfy 边连接的被满足元素（引用节点，标准需求图语义）
        sel = [e for e in entities if _elem_kind(e) == "requirement"]
        sel_ids = {e["id"] for e in sel}
        edges = []
        ref_ids = set()
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("satisfy", "derive", "conflict", "dependency", "verify"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
                # satisfy/conflict 边连接的被满足/冲突对象（非 requirement）→ 引用节点
                if k in ("satisfy", "conflict"):
                    for nid in (r["source"], r["target"]):
                        if nid not in sel_ids and nid in emap:
                            ref_ids.add(nid)
        if ref_ids:
            sel += [emap[nid] for nid in ref_ids]
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "UC":
        sel = [e for e in entities if _elem_kind(e) in ("actor", "usecase")]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("include", "extend", "dependency", "satisfy", "assoc"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        # 兜底关联（规范 7.22：actor 是用例的外部参与者）：
        # 无参与边的 use case → 连所有 actor；无参与边的 actor → 连所有 use case
        actor_ids = [e for e in sel if _elem_kind(e) == "actor"]
        uc_ids = [e for e in sel if _elem_kind(e) == "usecase"]
        if actor_ids and uc_ids:
            linked = {e["source"] for e in edges} | {e["target"] for e in edges}
            for u in uc_ids:
                if u["id"] in linked:
                    continue
                for a in actor_ids:
                    edges.append({"source": a["id"], "target": u["id"],
                                  "kind": "assoc", "type": "参与", "label": ""})
            for a in actor_ids:
                if a["id"] in linked:
                    continue
                for u in uc_ids:
                    edges.append({"source": a["id"], "target": u["id"],
                                  "kind": "assoc", "type": "参与", "label": ""})
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "ACT":
        sel = [e for e in entities if _elem_kind(e) == "action"]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("flow", "dependency"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "SEQ":
        # 顺序图：生命线顶部横排，消息自上而下按时间序（标准时序图布局）
        # 消息按出现顺序编号 seq；生命线按消息首次出现顺序排列
        # 生命线优先取 lifeline 节点；无 lifeline 时回退消息两端实体 ∪ 部件/参与者
        lifelines_only = [e for e in entities if _elem_kind(e) == "lifeline"]
        if lifelines_only:
            sel = lifelines_only
        else:
            sel = [e for e in entities if _elem_kind(e) in ("part", "actor", "block", "port")]
        edges = []
        msg_rels_all = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("message", "flow"):
                msg_rels_all.append((r, k))
        # 标准时序图只消费 message 边；无 message 时 flow 兜底（避免活动/状态边混入）
        msg_rels = [x for x in msg_rels_all if x[1] == "message"] or msg_rels_all
        msg_rels.sort(key=lambda x: (x[1] != "message",))  # message 优先，flow 次之（保序）
        for i, (r, k) in enumerate(msg_rels, 1):
            e = _edge(emap[r["source"]], emap[r["target"]], k, r)
            e["seq"] = i  # 时序编号（1 起），前端按此垂直排列
            e["label"] = ((r.get("properties") or r.get("props") or {}).get("name")
                          or (r.get("type") or k))
            edges.append(e)
        # 生命线：消息两端实体 ∪ 显式 lifeline（实体多重身份如 actor/part 也纳入，按消息出现顺序排前）
        msg_side = set()
        for r, _k in msg_rels:
            msg_side.add(r["source"])
            msg_side.add(r["target"])
        if msg_side or any(_elem_kind(e) == "lifeline" for e in entities):
            sel = [e for e in entities
                   if e["id"] in msg_side or _elem_kind(e) == "lifeline"]
        else:
            sel = [e for e in entities if _elem_kind(e) in ("part", "actor", "block", "port")]
        # 生命线排序：出现在消息边的实体按首次出现顺序排前，其余按名称
        linked_order = {}
        for r, _k in msg_rels:
            for nid in (r["source"], r["target"]):
                if nid not in linked_order:
                    linked_order[nid] = len(linked_order)
        sel.sort(key=lambda e: (e["id"] not in linked_order, linked_order.get(e["id"], 1 << 30), e["name"]))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "STM":
        sel = [e for e in entities if _elem_kind(e) == "state"]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("transition", "dependency"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "PAR":
        sel = [e for e in entities if _elem_kind(e) in ("constraint", "parameter")]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("bind", "dependency", "flow"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "PKG":
        # 包图：包 + 包内元素（composition 边表达包含）
        sel = [e for e in entities if _elem_kind(e) in ("package", "block", "part", "requirement")]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("composition", "dependency"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return [_view_node(e) for e in sel], edges, warnings

    if view_type == "TRACE":
        # 追溯：需求→设计→实现 的 satisfy/derive/allocate 全链
        sel = [_view_node(e) for e in entities]
        edges = []
        for r in valid_rels:
            k = _edge_kind(r["type"])
            if k in ("satisfy", "derive", "allocate", "dependency", "composition", "conflict"):
                edges.append(_edge(emap[r["source"]], emap[r["target"]], k, r))
        return sel, edges, warnings

    # 兜底：全量
    return [_view_node(e) for e in entities], [_edge(emap[r["source"]], emap[r["target"]], _edge_kind(r["type"]), r) for r in valid_rels], warnings


def _edge(src: dict, tgt: dict, kind: str, rel: dict) -> dict:
    return {
        "source": src["id"],
        "target": tgt["id"],
        "kind": kind,
        "type": rel.get("type", ""),
        "label": rel.get("type", ""),
    }


def list_view_types() -> list:
    """视图类型清单（供前端下拉）。"""
    out = []
    for vid, v in VIEW_TYPES.items():
        out.append({
            "id": vid,
            "name": v["name"],
            "v1": v["v1"],
            "v2": v["v2"],
            "kind": v["kind"],
            "kb": v["kb"],
            "desc": v["desc"],
            "v1_standard": vid in V1_DIAGRAMS,
        })
    return out
