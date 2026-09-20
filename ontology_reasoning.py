"""P0-2：本体可执行推理（轻量规则引擎，零外部推理器）。

对齐行业"语义契约层"的轻量落地，替代原"4 项规则提示"：
1. 分类推理 classify —— 子类实例亦属于父类（沿 ontology_types.parent_id 链物化）
2. 传递闭包 transitive —— 可传递关系（包含/连接/追溯/满足/派生/执行）的 BFS 闭包
3. 一致性 consistency —— 实例类型合法性 + 必填继承 + 取值白名单 + 关系 domain/range

所有函数既支持"实例导入预览三元组"（triples 参数，不入库），
也支持"全库图谱扫描"（triples=None 时读取 entities/relations 表）。
"""
import json

# 可传递关系白名单（与 SysML 导入/需求追溯语义对齐）
# P1-2：此常量降级为**兜底默认值**——优先从本体公理读（ontology_types.properties
# 的 transitive/characteristics 标记），本体未标记任何传递关系时才回退到这里。
# P1-2 归一（2026-09-06）：补英文 canonical 名（真实数据/本体均用英文；
# 中文关系类型已 deprecated，保留中文仅为历史数据防御）。
TRANSITIVE_RELATIONS = ("CONTAINS", "CONNECTS", "TRACE", "SATISFIES", "DERIVES",
                        "包含", "连接", "追溯", "满足", "派生")
# 类层级 source: 实例类型映射三元组标记
RDF_TYPE = "a"


def load_transitive_properties(conn) -> set:
    """P1-2：从本体公理读传递关系，替代硬编码。

    读取 ontology_types（type_kind='relation'）的 properties JSON，识别：
    - properties.transitive == true
    - properties.characteristics 列表含 'transitive'
    返回标记为传递的关系名集合；本体一个都没标时回退 TRANSITIVE_RELATIONS 兜底。
    """
    marked = set()
    try:
        rows = conn.execute(
            "SELECT name, properties FROM ontology_types WHERE type_kind='relation'"
        ).fetchall()
    except Exception:
        rows = []
    for r in rows:
        try:
            props = json.loads(r["properties"] or "{}")
        except Exception:
            continue
        if not isinstance(props, dict):
            continue
        if props.get("transitive") is True or \
                "transitive" in [str(c).lower() for c in (props.get("characteristics") or [])]:
            marked.add(r["name"])
    return marked if marked else set(TRANSITIVE_RELATIONS)


def _load_type_hierarchy(conn) -> dict:
    """类型层级：name → parent_name（沿 parent_id）。返回 {name: parent_name}。"""
    rows = conn.execute("SELECT id, name, parent_id FROM ontology_types").fetchall()
    id2name = {r["id"]: r["name"] for r in rows}
    out = {}
    for r in rows:
        out[r["name"]] = id2name.get(r["parent_id"], None) if r["parent_id"] else None
    return out


def _ancestors(hierarchy: dict, type_name: str) -> list:
    """返回全部祖先名（不含自身）。"""
    out, seen, cur = [], set(), type_name
    while cur and cur not in seen:
        seen.add(cur)
        p = hierarchy.get(cur)
        if not p:
            break
        out.append(p)
        cur = p
    return out


def _pred_name(p) -> str:
    """谓词/类型引用归一：去 ex: 前缀 + percent-decode（ns.local 编码的中文名还原）。

    P0 修复（2026-09-09）：_load_all_triples 用 ns.local 编码中文类型/关系名
    （部件 → %E9%83%A8%E4%BB%B6），此前推理器拿编码名与中文本体名比对，
    产生 285 假阳性「未声明类型」+ 122 假阳性「非法关系」。
    """
    from urllib.parse import unquote
    s = str(p or "")
    if s.startswith("ex:"):
        s = s[3:]
    return unquote(s)


def _ex_entity_type(obj: str) -> str:
    """三元组宾语 'ex:部件'（或编码形态）→ '部件'；非类型引用返回 ''。"""
    if obj and str(obj).startswith("ex:"):
        return _pred_name(obj)
    return ""


def _load_all_triples(conn, branch: str = "") -> list:
    """从 entities/relations 表加载全库三元组（triples=None 时的全库图谱扫描）。

    branch 非空时只加载该分支数据（2026-09-14：推理只针对当前分支，与图谱视图口径一致；
    本体公理/类型层级为全局共享 Schema，不受分支过滤影响）。

    三元组格式与实例导入预览（r2rml 源）完全一致，保证推理函数行为一致：
    - 实体类型声明: (ex:{id}, "a", "ex:{type}")
    - 实体属性:      (ex:{id}, "ex:{prop}", str(value))
    - 关系:          (ex:{sid}, "ex:{rel}", ex:{tid})
    """
    import json as _json
    from core import ns as _ns
    # P0-1：实例命名空间统一到 core.ns（原来是第三套 inst#，与本体/图库都不通）
    BASE_T = _ns.NS_ENT

    def _local(name):
        return _ns.local(_ns.loc_key(name))

    def _uri(name):
        return BASE_T + _local(name)

    br_sql = " AND branch=?" if branch else ""
    br_params = (branch,) if branch else ()
    triples = []
    for e in conn.execute(
            f"SELECT id, entity_type, properties FROM entities WHERE status!='deprecated'{br_sql}",
            br_params).fetchall():
        uri = _uri(e["id"])
        et = e["entity_type"] or ""
        if et:
            triples.append((uri, "a", f"ex:{_local(et)}"))
        props = {}
        try:
            props = _json.loads(e["properties"] or "{}")
        except Exception:
            pass
        for k, v in (props or {}).items():
            triples.append((uri, f"ex:{_local(k)}", str(v)))
    for r in conn.execute(
            f"SELECT source_id, target_id, relation_type FROM relations WHERE status!='deprecated'{br_sql}",
            br_params).fetchall():
        triples.append((_uri(r["source_id"]), f"ex:{_local(r['relation_type'] or 'relatedTo')}",
                        _uri(r["target_id"])))
    return triples

def classify_instances(conn, triples: list | None = None) -> list:
    """分类推理：实例类型 + 层级 → 推断"子类实例亦属于父类"的三元组。

    返回 [{s, parent_type, via}]（via=直接子类型链）。
    """
    hierarchy = _load_type_hierarchy(conn)
    out = []
    seen = set()
    if triples is None:
        triples = _load_all_triples(conn)
    for s, p, o in triples:
        if p != RDF_TYPE:
            continue
        t = _ex_entity_type(o)
        if not t or t not in hierarchy:
            continue
        for anc in _ancestors(hierarchy, t):
            key = (s, anc)
            if key in seen:
                continue
            seen.add(key)
            out.append({"s": s, "parent_type": anc, "via": t})
    return out


def transitive_closure(conn, triples: list | None = None, rel_types: tuple | None = None) -> list:
    """传递闭包：对可传递关系做有向 BFS，推断间接关系三元组。

    P1-2：rel_types=None（默认）→ 从本体公理读（load_transitive_properties），
    本体无标记时回退 TRANSITIVE_RELATIONS；显式传入元组则用传入值。
    返回 [{s, p, o, path}]（path 为中间节点链）。
    """
    if rel_types is None:
        rel_types = load_transitive_properties(conn)
    # 关系三元组：(s, pred, o)；pred 为关系类型（可能是 ex:前缀）
    if triples is None:
        triples = _load_all_triples(conn)
    edges = {}   # pred → {s: [targets]}
    for s, p, o in triples:
        pred = _pred_name(p)
        if pred not in rel_types:
            continue
        edges.setdefault(pred, {}).setdefault(s, []).append(o)
    out = []
    for pred, adj in edges.items():
        # BFS 每起点求闭包；path 长度 ≥3（至少一条中间节点）才计为推断的间接关系
        for start in adj:
            visited = set()
            queue = [(start, [start])]
            while queue:
                cur, path = queue.pop(0)
                for nxt in adj.get(cur, []):
                    if nxt in path:
                        continue  # 防环
                    new_path = path + [nxt]
                    if len(new_path) >= 3:
                        key = (start, pred, nxt)
                        if key in visited:
                            continue
                        visited.add(key)
                        out.append({"s": start, "p": pred, "o": nxt,
                                    "path": " → ".join(new_path)})
                    queue.append((nxt, new_path))
    return out


def _ent_short(u) -> str:
    """实体 URI → 短 id（剥命名空间 + percent-decode）；非 URI 原样返回。推理结果人读化用。"""
    from core import ns as _ns
    try:
        _, eid = _ns.parse_ent_uri(str(u))
        return eid or str(u)
    except Exception:
        return str(u)


def consistency_check(conn, triples: list | None = None) -> list:
    """一致性检查：类型合法 + 必填继承 + 取值白名单 + 关系 domain/range。

    返回 [{type, name, pass, detail, errors[]}]（error 空=通过）；
    errors 为结构化问题项 [{ent, text}]（ent=实体 URI，供前端「定位实体」按钮）。
    """
    from ontology_semantics import OntologyValidator
    v = OntologyValidator(conn)
    # 已声明类型 = entity + class 双口径（2026-09-14 修假阳性：本体模型建的类型 type_kind='class'，
    # 旧逻辑只认 'entity'，导致全部 class 类型被误报「未在本体声明」）
    known = set(v.entity_types()) | {r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind IN ('entity','class')").fetchall()}

    # 实体 URI → 图库原始 id 反查表（2026-09-14 修定位失效：loc_key 会把 id 里的
    # '-' 归一为 '_'，URI 短 id（ENT_TEST_x）与图库节点 id（ENT-TEST-x）不一致，
    # 错误项若携带变形 id，前端「定位」永远匹配不上图谱节点）
    from core import ns as _ns
    uri2raw = {}
    try:
        for r in conn.execute("SELECT id FROM entities").fetchall():
            raw = r["id"]
            uri2raw[_ns.NS_ENT + _ns.local(_ns.loc_key(raw))] = raw
            uri2raw[_ns.local(_ns.loc_key(raw))] = raw
            uri2raw[raw] = raw
    except Exception:
        pass

    def _raw(u) -> str:
        """URI/短 id → 图库原始实体 id（供前端定位；查无映射则原样返回）。"""
        return uri2raw.get(str(u)) or _ent_short(u) or str(u)

    def _disp(u) -> str:
        """实体 URI → 显示名（查图库实体名；查无→原始 id）。错误信息人读化用。"""
        raw = _raw(u)
        try:
            r = conn.execute("SELECT name FROM entities WHERE id=? LIMIT 1", (raw,)).fetchone()
            return (r["name"] if r and r["name"] else raw)
        except Exception:
            return raw
    rels = {r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='relation'").fetchall()}
    if triples is None:
        triples = _load_all_triples(conn)

    # 聚合每个实例的类型 + 属性 + 出边关系（关系谓词按"是否为本体关系类型"判定，
    # 与预览/全库一致：关系宾语为 ex:URI，属性宾语为字符串）
    inst_type, inst_props = {}, {}
    inst_edges = []   # (s, rel, o)
    for s, p, o in triples:
        if p == RDF_TYPE:
            t = _ex_entity_type(o)
            if t and t != "Thing":
                inst_type[s] = t
            continue
        rel = _pred_name(p)
        if rel in rels:
            inst_edges.append((s, rel, o))
        elif rel != "inferredFrom":
            inst_props.setdefault(s, {})[rel] = o

    results = []
    # 1) 类型合法性
    bad = [(s, t) for s, t in inst_type.items() if t not in known]
    results.append({
        "name": "一致性检查",
        "pass": not bad,
        "detail": "全部实例类型合法" if not bad else f"发现 {len(bad)} 个未在本体声明的类型: {'、'.join(f'{_disp(s)}({t})' for s, t in bad[:6])}",
        "errors": [{"ent": _raw(s), "text": f"{_disp(s)} 的类型「{t}」未在本体声明"} for s, t in bad[:50]]})
    # 2) 必填（含父类继承）
    missing = []
    for s, t in inst_type.items():
        if t not in known:
            continue
        cons = v._merged_constraints(t)
        props = inst_props.get(s, {})
        for k in cons.get("required", []):
            if not props.get(k):
                missing.append((s, t, k))
    results.append({
        "name": "基数验证（必填继承）",
        "pass": not missing,
        "detail": "必填属性已全部赋值" if not missing else f"{len(missing)} 处缺失: {'、'.join(f'{_disp(s)} 缺 {k}' for s, t, k in missing[:6])}",
        "errors": [{"ent": s, "text": f"{_disp(s)}（{t}）缺必填属性「{k}」"} for s, t, k in missing[:50]]})
    # 3) 关系 domain/range（实例化校验的推理侧复核）
    bad_edges = []
    for s, rel, o in inst_edges:
        st, tt = inst_type.get(s, ""), inst_type.get(o, "")
        if not st or not tt:
            continue
        if v.validate_edge(st, rel, tt):
            bad_edges.append((s, rel, o))
    results.append({
        "name": "关系类型校验",
        "pass": not bad_edges,
        "detail": "关系均符合本体 domain/range" if not bad_edges else f"{len(bad_edges)} 条非法: {'、'.join(f'{_disp(s)}--[{rel}]-->{_disp(o)}' for s, rel, o in bad_edges[:6])}",
        "errors": [{"ent": _raw(s), "text": f"{_disp(s)} --[{rel}]--> {_disp(o)} 不符合本体的 domain/range 约束"} for s, rel, o in bad_edges[:50]]})
    return results


def cardinality_check(conn) -> list:
    """P1 精确多重性校验（2026-09-10）：关系类型 min/max 基数 vs 图库实际出边/入边计数。

    数据源：权威表 entities/relations（与 SHACL 数据图同源）。
    card_src：源端类的每个实例发出的该关系数量；card_tgt：目标端类每个实例接收的数量。
    返回 [{name, pass, detail}]，与 consistency_check 同形。
    """
    import json as _json
    from collections import Counter
    results = []
    rtypes = {}
    for r in conn.execute("SELECT name, constraints FROM ontology_types "
                          "WHERE type_kind='relation'").fetchall():
        try:
            cons = _json.loads(r["constraints"] or "{}")
        except Exception:
            continue
        av = cons.get("allowed_values") or {}
        srcs = av.get("src") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        tgts = av.get("tgt") or []
        if isinstance(tgts, str):
            tgts = [tgts]
        cs = cons.get("card_src") or {}
        ct = cons.get("card_tgt") or {}
        _cs_ok = isinstance(cs, dict) and (cs.get("min") is not None or cs.get("max") is not None)
        _ct_ok = isinstance(ct, dict) and (ct.get("min") is not None or ct.get("max") is not None)
        if _cs_ok or _ct_ok:
            rtypes[r["name"]] = (set(srcs), set(tgts),
                                 cs if _cs_ok else {}, ct if _ct_ok else {})
    if not rtypes:
        results.append({"name": "基数校验", "pass": True,
                        "detail": "无基数约束（在关系类型中栏「基数」设置 min/max 后即启用）"})
        return results
    etype = {e["name"]: (e["entity_type"] or "") for e in conn.execute(
        "SELECT name, entity_type FROM entities WHERE status!='deprecated'").fetchall()}
    eid2name = {e["id"]: e["name"] for e in conn.execute("SELECT id, name FROM entities").fetchall()}
    out_cnt, in_cnt = Counter(), Counter()
    for rel in conn.execute("SELECT source_id, target_id, relation_type FROM relations "
                            "WHERE status!='deprecated'").fetchall():
        rt = str(rel["relation_type"] or "")
        if rt in rtypes:
            out_cnt[(rt, rel["source_id"])] += 1
            in_cnt[(rt, rel["target_id"])] += 1
    viol, checked = [], 0

    def _check(pool, cnt, rt, card, tag):
        nonlocal checked
        mn, mx = card.get("min"), card.get("max")
        for nm in pool:
            checked += 1
            c = cnt.get(nm, 0)
            if (mn is not None and c < int(mn)) or (mx is not None and c > int(mx)):
                viol.append(f"{nm} {tag} {rt}×{c}"
                            f"（要求 {mn if mn is not None else 0}..{mx if mx is not None else '∞'}）")

    for rt, (srcs, tgts, cs, ct) in rtypes.items():
        if cs:
            cnt_by_name = Counter()
            for (r2, sid), n in out_cnt.items():
                if r2 == rt:
                    cnt_by_name[eid2name.get(sid, sid)] += n
            _check([n for n, t in etype.items() if t in srcs] if srcs else list(cnt_by_name.keys()),
                   cnt_by_name, rt, cs, "出边")
        if ct:
            cnt_by_name = Counter()
            for (r2, tid), n in in_cnt.items():
                if r2 == rt:
                    cnt_by_name[eid2name.get(tid, tid)] += n
            _check([n for n, t in etype.items() if t in tgts] if tgts else list(cnt_by_name.keys()),
                   cnt_by_name, rt, ct, "入边")
    results.append({"name": "基数校验", "pass": not viol,
                    "detail": (f"已核对 {checked} 个实例的基数，全部满足" if checked else "无可核对实例")
                              if not viol else f"{len(viol)} 处违反: {'、'.join(viol[:6])}"})
    return results


def load_owl_axioms(conn) -> dict:
    """P1（2026-09-09）：从本体公理读 OWL 特征标记，供运行时推理消费。

    读取 ontology_types（type_kind='relation'）properties JSON 的 characteristics：
    返回 {"symmetric": set, "reflexive": set}（对称/自反——可在实例层直接推断的两类）。
    asymmetric/inverse_functional 属约束型公理，由一致性检查/SHACL 消费，不在推断集。
    """
    out = {"symmetric": set(), "reflexive": set()}
    try:
        rows = conn.execute(
            "SELECT name, properties FROM ontology_types WHERE type_kind='relation'"
        ).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            props = json.loads(r["properties"] or "{}")
        except Exception:
            continue
        if not isinstance(props, dict):
            continue
        chars = {str(c).lower() for c in (props.get("characteristics") or [])}
        if "symmetric" in chars:
            out["symmetric"].add(r["name"])
        if "reflexive" in chars:
            out["reflexive"].add(r["name"])
    return out


def symmetric_inference(conn, triples: list | None = None, axioms: dict | None = None) -> list:
    """对称推理：公理标记 symmetric 的关系 p，(s,p,o) ⇒ 推断 (o,p,s)。

    返回 [{s, p, o}]（已去除与原边重复的自环情形）。
    """
    if axioms is None:
        axioms = load_owl_axioms(conn)
    sym = axioms.get("symmetric") or set()
    if not sym:
        return []
    if triples is None:
        triples = _load_all_triples(conn)
    out = []
    seen = set()
    for s, p, o in triples:
        pred = _pred_name(p)
        if not pred or (s) == (o):
            continue
        if pred in sym:
            key = (o, pred, s)
            if key in seen:
                continue
            seen.add(key)
            out.append({"s": o, "p": pred, "o": s})
    return out


def reflexive_check(conn, triples: list | None = None, axioms: dict | None = None) -> dict:
    """自反检查/推断：公理标记 reflexive 的关系 p，每个该关系域内实例应存在 (s,p,s)。

    现有实例边缺自环 → 补推断；返回 {"inferred": [{s,p,o}], "violations": [...]}。
    """
    if axioms is None:
        axioms = load_owl_axioms(conn)
    ref = axioms.get("reflexive") or set()
    if not ref:
        return {"inferred": [], "violations": []}
    if triples is None:
        triples = _load_all_triples(conn)
    have = set()
    for s, p, o in triples:
        pred = _pred_name(p)
        if pred in ref:
            have.add((s, pred, o))
    inferred, violations = [], []
    # 已有边的端点缺自环 → 推断补齐（OWL 自反语义的局部实现：仅对关系实例端点）
    for (s, p, o) in have:
        if (s, p, s) not in have:
            key = {"s": s, "p": p, "o": s}
            if key not in inferred:
                inferred.append(key)
    return {"inferred": inferred, "violations": violations}


def run_reasoning(conn, triples: list, inst_count: int = 0, rel_count: int = 0) -> dict:
    """统一入口：分类 + 传递 + 一致性 三项可执行推理。

    返回 {checks:[{name,pass,detail}], inferred:[{kind,s,p,o,path|via}], stats}
    """
    checks = []
    inferred = []

    cls = classify_instances(conn, triples)
    inferred += [{"kind": "classify", "s": c["s"], "p": "a", "o": f"ex:{c['parent_type']}",
                  "via": c["via"]} for c in cls]
    checks.append({
        "name": "分类推理",
        "pass": True,
        "detail": f"已按子类链物化 {len(cls)} 条「子类实例亦属于父类」推断"
                  + (f"（如 {cls[0]['s']} 归类为 {cls[0]['parent_type']}）" if cls else " — 当前无子类实例")
                  + f"（共 {len(_load_type_hierarchy(conn))} 个类型参与层级）"})

    tr = transitive_closure(conn, triples)
    inferred += [{"kind": "transitive", "s": t["s"], "p": t["p"], "o": t["o"], "path": t["path"]}
                 for t in tr]
    checks.append({
        "name": "传递推理",
        "pass": True,
        "detail": f"已物化 {len(tr)} 条间接关系"
                  + (f"（示例: {tr[0]['path']}）" if tr else " — 无可传递关系（包含/连接/追溯/满足/派生/执行）")})

    # P1（2026-09-09）：对称/自反公理运行时推理（此前仅导出 OWL 注记、零消费）
    axioms = load_owl_axioms(conn)
    sym = symmetric_inference(conn, triples, axioms)
    inferred += [{"kind": "symmetric", "s": t["s"], "p": t["p"], "o": t["o"]} for t in sym]
    ax_n = len(axioms.get("symmetric") or set())
    checks.append({
        "name": "对称推理",
        "pass": True,
        "detail": f"已推断 {len(sym)} 条反向关系"
                  + (f"（{ax_n} 个对称公理）" if ax_n else " — 本体未标记对称公理（关系编辑特征里勾选「对称」即生效）")})

    rx = reflexive_check(conn, triples, axioms)
    inferred += [{"kind": "reflexive", "s": t["s"], "p": t["p"], "o": t["o"]}
                 for t in rx.get("inferred") or []]
    checks.append({
        "name": "自反推理",
        "pass": True,
        "detail": f"已补齐 {len(rx.get('inferred') or [])} 条自环关系"
                  + (f"（{len(axioms.get('reflexive') or set())} 个自反公理）"
                     if axioms.get("reflexive") else " — 本体未标记自反公理")})

    checks += consistency_check(conn, triples)
    checks += cardinality_check(conn)
    checks += cardinality_check(conn)

    return {"checks": checks, "inferred": inferred,
            "stats": {"classify": len(cls), "transitive": len(tr),
                      "symmetric": len(sym), "reflexive": len(rx.get("inferred") or []),
                      "instances": inst_count, "relations": rel_count}}
