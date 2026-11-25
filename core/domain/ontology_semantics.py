"""KB-P4：本体语义层——本体驱动图谱实例化（校验 + GraphStore 抽象 + Agent 语义注入）。

设计（对齐米爸要求：本体用于 AI Agent 语义层，图谱构建按本体的类型/关系/属性/约束规则实例化）：
1. OntologyValidator：节点/边实例化前按本体校验（类型合法、必填、唯一、取值白名单、关系源/目标约束）
2. GraphStore：图数据库抽象层（SQLite 基线；Neo4j 可按同一接口适配，GRAPH_ENGINE 切换）
3. schema_text()：本体 Schema 文本，注入 Agent system_prompt 约束 LLM 抽取与生成
"""
import json
import re
import uuid

from core import ns as _ns
from core import typevocab

# P0-1：默认命名空间统一取自 core.ns（与图库 core.ns.NS_ONTOLOGY 同源）
DEFAULT_NAMESPACE = _ns.NS_ONTOLOGY


def active_rows(conn):
    """**消费口径单一真源**：返回消费侧应使用的本体类型行（active 版本的最新快照）。

    2026-09-23 口径统一（决策 D2 = 消费读最新快照）。改造之前的状态是"口径分裂"：
      · `_active_ont_rows`（routers/knowledge_parts/shared.py）已按"已发布快照"读取，
        `/schema`、`/graph`、`/shacl`、`/export` 也走它；
      · 但 `OntologyValidator(conn)` **默认读编辑态** `ontology_types` → AI 建模语义注入
        (`agent/pipeline_parts/prompt.py`)、抽取侧 (`vector2graph`)、写校验 (`GraphStore`)
        吃的是**编辑态**；更糟的是 `_ontology_check`（一致性体检）也读编辑态
        → 「体检的问题集」未必等于「AI 建模真正吃到的那份 schema」。实测 active 版本
        v25.0.0 的 `snapshot_count=0`（快照从未写入）使回退路径**长期生效**，掩盖了分裂。
    现收敛到本函数一处：`OntologyValidator` 默认（rows=None）与 `shared._active_ont_rows` 都走它。

    回退策略（显式，避免"消费空数据"）：
      · 无 active 版本 / active 版本无快照（快照治理上线前的存量 released）→ 回退编辑态；
      · `ontology_versions` 表不存在（夹具库 / 极早期库）→ 回退编辑态。
    """
    try:
        v = conn.execute(
            "SELECT id FROM ontology_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        if v:
            # ⚠️ 列清单按**实际表结构探测**，不硬枚举：
            #   2026-11-15 加 tlo_anchor 时，硬枚举让手写 DDL 夹具崩掉
            #   （`no such column: tlo_anchor`）。探测写法下：
            #   · 真库有该列 → 正常返回；
            #   · 夹具/老库没有 → 少该键，下游用 `"tlo_anchor" in r.keys()` 守卫，
            #     并把"该维度未检查"登记进 degraded（不静默当成"全部合规"）。
            _scols = [x[1] for x in conn.execute(
                "PRAGMA table_info(ontology_version_snapshots)")]
            _sel = ("type_id AS id, name, type_kind, parent_id, properties, constraints, "
                    "description, icon, color, iri")
            if "tlo_anchor" in _scols:
                _sel += ", tlo_anchor"
            rows = conn.execute(
                "SELECT " + _sel + " FROM ontology_version_snapshots WHERE version_id=?",
                (v["id"],)).fetchall()
            if rows:                      # active 版本有快照 → 消费快照
                return rows
    except Exception:                     # 表不存在等 → 回退（下游不得因体裁表缺失而空转）
        pass
    return conn.execute("SELECT * FROM ontology_types").fetchall()


def slugify(name: str) -> str:
    """实体名 → IRI 局部名（P0-1：与 graph_db.class_uri 同源，规则见 core.ns）。

    历史实现为「非法字符 → _」并保留中文，导致本体导出的类 IRI 与图库 rdf:type
    指向的类 IRI 对不上；现统一 percent-encoding，两边逐字节一致。
    """
    return _ns.local(_ns.loc_key(name))


def get_ontology_meta(conn) -> dict:
    """读取本体元信息（命名空间 / IRI 策略 / 默认前缀）。无行回退默认。"""
    try:
        row = conn.execute("SELECT * FROM ontology_meta WHERE id=1").fetchone()
    except Exception:
        return {"namespace": DEFAULT_NAMESPACE, "iri_strategy": "hash-name", "default_prefix": ""}
    if not row:
        return {"namespace": DEFAULT_NAMESPACE, "iri_strategy": "hash-name", "default_prefix": ""}
    return dict(row)


def make_iri(conn, name: str, strategy: str = None, taken: set = None) -> str:
    """生成实体 IRI：namespace + 尾段。

    - hash-name（默认）：namespace + 局部名（percent-encoding，规则见 core.ns）
    - uuid：namespace + uuid4 hex（绝对唯一，不可读）

    P0-2：hash-name 不再用「冲突追加 _2/_3」——那样 IRI 依赖插入顺序、不幂等。
    局部名由名称确定性派生，重名应被上游拒绝（本体类型名本身应唯一），
    而不是静默生成 `X_2` 造成两个 IRI 指向同一概念。
    taken 仍保留参数兼容：仅用于检测已存在冲突并给出提示性后缀。
    """
    meta = get_ontology_meta(conn)
    ns = meta.get("namespace") or DEFAULT_NAMESPACE
    strategy = strategy or meta.get("iri_strategy") or "hash-name"
    if strategy == "uuid":
        return f"{ns}{uuid.uuid4().hex}"
    return _ns.class_iri(name, base=ns)


class OntologyValidator:
    """本体语义层校验器：ontology_types 表（type_kind=entity|relation|attribute）+ constraints JSON 驱动。

    2026-09-02 快照消费：rows 传入已发布快照行时以该版本为准。
    2026-09-23 口径统一（决策 D2 = 消费读最新快照）：**rows=None（默认）不再读编辑态**，
    改走 `active_rows(conn)`（active 版本的最新快照；无快照/无表回退编辑态）——
    使语义注入、抽取、写校验、导出、体检吃的是**同一份** schema（此前注入/写校验读编辑态、
    导出读快照，属"口径分裂"）。要显式读编辑态，请显式传 rows=编辑态行。
    """

    def __init__(self, conn, rows=None):
        self.conn = conn
        self._types = None
        self._pre_rows = rows

    def _load_types(self) -> dict:
        if self._types is None:
            self._types = {}
            if self._pre_rows is not None:
                rows = self._pre_rows
            else:
                rows = active_rows(self.conn)
            for r in rows:
                d = dict(r)
                # ★ 2026-11-15：**已解析的结构体不能再 json.loads**。
                #   DB 取出的行是 JSON 字符串，但调用方注入的内存行（`rows=` 参数、
                #   门禁夹具、影响面模拟）常常已经是 dict。此前一律 json.loads ⇒
                #   `TypeError` 被下面的 except 吞掉 ⇒ `constraints` 静默变成 `{}`
                #   ⇒ **所有约束凭空消失**，校验"全绿"（本笔实测踩到：影响面恒为 0，
                #   把 required 硬挂到有 2 个缺属性实例的根类上竟也报 0 违规）。
                #   这是"兜底把错误变成不可见"的典型：宁可让类型显式分流，不要靠 except。
                for _k in ("constraints", "properties"):
                    _v = d.get(_k)
                    if isinstance(_v, (dict, list)):
                        continue
                    try:
                        d[_k] = json.loads(_v or "{}")
                    except Exception:
                        d[_k] = {}
                self._types[d["name"]] = d
        return self._types

    def entity_types(self) -> list:
        return [t["name"] for t in self._load_types().values() if t["type_kind"] == "entity"]

    def relation_types(self) -> list:
        return [t["name"] for t in self._load_types().values() if t["type_kind"] == "relation"]

    # ── P0-1：类层级（subClassOf）──
    def parents_of(self, type_name: str) -> list:
        """返回直接父类型名列表（当前实现单父，列表便于扩展）。"""
        t = self._load_types().get(type_name)
        if not t or not t.get("parent_id"):
            return []
        pt = self._load_types().get(self._name_by_id(t["parent_id"]))
        return [pt["name"]] if pt else []

    def _name_by_id(self, tid) -> str:
        for t in self._load_types().values():
            if t["id"] == tid:
                return t["name"]
        return ""

    def ancestors(self, type_name: str) -> list:
        """沿 parent_id 链返回祖先类型名列表（不含自身，父→祖顺序）。"""
        out, seen, cur = [], set(), type_name
        while cur and cur not in seen:
            seen.add(cur)
            ps = self.parents_of(cur)
            if not ps:
                break
            p = ps[0]
            out.append(p)
            cur = p
        return out

    def classify(self, entity_type: str) -> list:
        """分类推理：返回该类型及全部祖先类型（含自身）——'子类实例亦属于父类'。"""
        return [entity_type] + self.ancestors(entity_type)

    def _merged_constraints(self, type_name: str) -> dict:
        """P0-1：合并自身 + 全部祖先的约束（子类继承父类必填/唯一/取值白名单）。"""
        merged = {"required": [], "unique": [], "allowed_values": {},
                 "disjoint_with": [], "characteristics": [], "cardinality": ""}
        for name in self.classify(type_name):
            t = self._load_types().get(name)
            if not t:
                continue
            cons = t.get("constraints") or {}
            for k in cons.get("required", []):
                if k not in merged["required"]:
                    merged["required"].append(k)
            for k in cons.get("unique", []):
                if k not in merged["unique"]:
                    merged["unique"].append(k)
            for k, v in (cons.get("allowed_values") or {}).items():
                merged["allowed_values"][k] = v
            # R4：互斥/推理性质/基数透传（供 SHACL sh:not / sh:maxCount / 领域公理消费）
            for _d in cons.get("disjoint_with", []):
                if _d not in merged["disjoint_with"]:
                    merged["disjoint_with"].append(_d)
            for _c in cons.get("characteristics", []):
                if _c not in merged["characteristics"]:
                    merged["characteristics"].append(_c)
            if cons.get("cardinality") and not merged["cardinality"]:
                merged["cardinality"] = cons["cardinality"]
        return merged

    def merged_properties(self, type_name: str) -> dict:
        """P0-1：合并自身 + 祖先的属性（子类继承父类属性定义）。"""
        out = {}
        for name in self.classify(type_name):
            t = self._load_types().get(name)
            if not t:
                continue
            for k, v in (t.get("properties") or {}).items():
                out.setdefault(k, v)
        return out

    def bound_attributes(self, entity_type: str) -> dict:
        """实体类型可用的数据属性 —— **单一真源**（schema_text 与 shacl_export 共用）。

        ## 为什么必须合并两侧（2026-11-15 实测）

        本工程的属性是「全局数据属性 + 绑定到实体」的 OWL/Protégé 模型：
          · 属性侧 `constraints.domain_classes` = 该属性适用于哪些实体类型；
          · 实体侧 `properties` = 历史/局部内联定义。
        真库实测：**28 个实体的 properties 全为空**，绑定 100% 在属性侧。

        而此前的 `schema_text()` 与 `shacl_export()` **都只读实体侧**
        ⇒ 后果一（更严重）：注入 Agent system_prompt 的本体摘要里
          **28 个实体全部显示"属性:无"、11 个属性名一个都不出现**
          ⇒ AI 建模时根本不知道这些实体能填什么属性，只能自由发挥或干脆不填；
        ⇒ 后果二：SHACL 里 `sh:datatype` 全库出现 0 次（xsd 强校验是死代码）。

        ## 合并口径（与 required/unique/白名单的局部覆盖同构）

          · 并集：两侧同名属性只算一次；
          · **类型/单位/量纲**取属性侧（它是全局定义）；
          · **required / unique / allowed_values** 取实体侧局部覆盖（本类的额外约束）。
          · 属性侧的 `domain_classes` 命中**自身或任一祖先**即可（子类继承父类的适用范围）。

        :returns: ``{属性名: {"type": "xsd:int"|"", "required": bool, "unique": bool,
                              "allowed_values": list, "unit": str,
                              "quantity_kind": str, "declared_by": "entity"|"attribute"}}``
        """
        atk = self._load_types()
        cons = self._merged_constraints(entity_type)   # 含祖先的 required/unique/白名单
        cls_chain = set(self.classify(entity_type))    # 自身 + 祖先

        out = {}

        # ① 实体侧内联定义（历史通路，保留；实测多为空）
        for k, v in self.merged_properties(entity_type).items():
            ptype = v.get("type") if isinstance(v, dict) else None
            out[k] = {"type": typevocab.xsd_of(ptype) if ptype else "",
                      "declared_by": "entity"}

        # ② 属性侧绑定（真库唯一的实际通路）
        for nm, t in atk.items():
            if t["type_kind"] != "attribute":
                continue
            ac = t.get("constraints") or {}
            dcs = ac.get("domain_classes")
            if dcs is None:
                dcs = ac.get("domain")
            if isinstance(dcs, str):
                dcs = [dcs]
            if not any(str(x).strip() in cls_chain for x in (dcs or []) if str(x).strip()):
                continue
            slot = out.setdefault(nm, {})
            # 属性侧是全局定义 ⇒ 类型/单位优先
            if ac.get("xsd_type"):
                slot["type"] = typevocab.xsd_of(ac.get("xsd_type"))
            if ac.get("unit"):
                slot["unit"] = str(ac.get("unit"))
            if ac.get("quantity_kind"):
                slot["quantity_kind"] = str(ac.get("quantity_kind"))
            av = ac.get("allowed_values")
            if isinstance(av, list) and av:
                slot["allowed_values"] = list(av)
            slot["declared_by"] = slot.get("declared_by") or "attribute"
            slot.setdefault("type", "")

        # ③ 实体侧局部约束覆盖（本类对某属性的额外要求）
        for k, slot in out.items():
            if k in (cons.get("required") or []):
                slot["required"] = True
            if k in (cons.get("unique") or []):
                slot["unique"] = True
            av_local = (cons.get("allowed_values") or {}).get(k)
            if isinstance(av_local, list) and av_local:
                slot["allowed_values"] = list(av_local)   # 局部覆盖全局
        return out

    def validate_node(self, entity_type: str, props: dict, branch: str = "",
                      exclude_id: str = "") -> list:
        """校验节点实例化：类型合法（含子类）+ 必填/唯一/取值白名单（含继承）。返回错误列表（空=通过）。

        :param branch: **唯一性的判定范围**（2026-11-15 新增，见下）。
        :param exclude_id: **唯一性比对时排除自身**（2026-11-15 新增，编辑路径必须传）。

        ## 为什么唯一性必须带分支（真缺陷，非洁癖）

        本工程 `entities` 的主键是复合键 `(id, branch)`：同一个逻辑实例在
        dev / release / personal 三个分支**各存一份**（实测 24 个需求 × 3 分支，
        同分支内 req_id 24/24 不重复，跨分支合计仍是 24 个唯一值）。

        而原实现的唯一性查询是**全库比对**（`WHERE entity_type=? AND properties LIKE ?`，
        无 branch 过滤）⇒ 每一条正常的跨分支副本都被判成"违反唯一约束"
        ⇒ **任何 unique 约束在本工程都必然误报**，这解释了为什么真库
        `constraints.unique` 全域为空（0/28）—— 不是业务没填，是填了就报错。

        ## 两种口径的适用面（显式声明，避免"同一语义两套写法"）

        · **传 branch（写入路径）**：编辑/新建已落分支的实例 —— 分支内判重，正确语义。
          `routers/knowledge_parts/graph.py`（PUT 保存）、`GraphStore.create_node`。
        · **不传 branch（导入/候选路径）**：数据尚未落分支（`v2g_candidates` 是待审候选、
          sysml 导入在分配分支之前）—— 只能按全局判重，且**更严**：可防"同一需求
          被重复导入到另一个分支"。调用方不传即为该语义，不是"忘了传"。
        """
        types = self._load_types()
        errs = []
        if entity_type not in types:
            known = self.entity_types()
            if known:
                errs.append(f"实体类型 '{entity_type}' 不在本体中（可用: {', '.join(known[:8])}）")
            else:
                errs.append("本体未配置实体类型")
            return errs
        cons = self._merged_constraints(entity_type)  # P0-1：含父类继承
        # P2（2026-09-07）：抽象类型不可直接实例化（含祖先链，constraints.abstract）
        for _anc in self.classify(entity_type):
            _at = self._load_types().get(_anc)
            if _at and (_at.get("constraints") or {}).get("abstract"):
                errs.append(f"抽象类型 '{_anc}' 不可直接实例化（请使用其子类型）")
                break
        for k in cons.get("required", []):
            if not props.get(k):
                errs.append(f"[{entity_type}] 缺少必填属性: {k}")
        for k in cons.get("unique", []):
            v = props.get(k)
            if v is not None:
                # 判定范围：传 branch ⇒ 分支内判重；不传 ⇒ 全局判重（见 docstring）
                _sql = ("SELECT id FROM entities WHERE entity_type=? AND properties LIKE ?"
                        " AND status!='deprecated'")
                _args = [entity_type, f'%"{k}": "{v}"%']
                if branch:
                    _sql += " AND branch=?"
                    _args.append(branch)
                # ★ 必须排除自身：编辑已有实例时（PUT 保存）它自己就在库里，
                #   不排除 ⇒ 每次保存都判"违反唯一约束" ⇒ 一旦启用 unique，
                #   该类型的实例就**再也改不动**（与"编辑入口被 400 堵死"同型）。
                if exclude_id:
                    _sql += " AND id != ?"
                    _args.append(exclude_id)
                dup = self.conn.execute(_sql + " LIMIT 1", tuple(_args)).fetchone()
                if dup:
                    _scope = f"（分支 {branch} 内）" if branch else "（全库）"
                    errs.append(f"[{entity_type}] 属性 {k}={v} 违反唯一约束{_scope}")
        for k, allowed in (cons.get("allowed_values") or {}).items():
            v = props.get(k)
            if v is not None and v not in allowed:
                errs.append(f"[{entity_type}] 属性 {k}={v} 不在允许值内 {allowed}")
        # P1-9（2026-09-07）：属性类型级取值白名单——独立数据属性的
        # constraints.allowed_values 为 plain list，domain 命中当前类型链才校验
        # （与实体级 map 形状区分；前端 data-properties 已按 list 渲染下拉）。
        _chain = set(self.classify(entity_type))
        for _r in self.conn.execute(
                "SELECT name, constraints FROM ontology_types WHERE type_kind='attribute'").fetchall():
            try:
                _ac = json.loads(_r["constraints"] or "{}")
            except Exception:
                _ac = {}
            _av = _ac.get("allowed_values")
            if not isinstance(_av, list) or not _av:
                continue
            _dom = _ac.get("domain_classes") or []
            if _dom and not (set(_dom) & _chain):
                continue
            _v = props.get(_r["name"])
            if _v is not None and _v != "" and _v not in _av:
                errs.append(f"[{entity_type}] 属性 {_r['name']}={_v} 不在允许值内 {_av}")
        errs.extend(self._xsd_type_errors(entity_type, props))
        return errs

    # P1-6（2026-09-07）：受控属性 xsd 数据类型校验——值存在时按本体 dataProperty
    # 声明的 type 检查格式（防 API 直写绕过前端受控 input；扩展属性键不在本体定义
    # 范围内则不校验，保留自由扩展能力）。
    # P1-8（2026-09-07）：xsd pattern 与别名映射统一移至 core/typevocab.py（词表单一
    # 事实来源），此处不再维护本地副本。

    def _xsd_type_errors(self, entity_type: str, props: dict) -> list:
        """P1-6（2026-09-07）初版；P1-8（2026-09-07）词表统一改造：
        声明类型兼容两种历史写法（properties.type 与 constraints.xsd_type，见
        docs/ontology-schema-gap-assessment.md §0.1），统一走 core.typevocab
        （别名→规范名→xsd pattern）。扩展属性键不在本体定义范围则不校验。"""
        errs = []
        chain = set(self.classify(entity_type))
        rows = self.conn.execute(
            "SELECT name, properties, constraints FROM ontology_types WHERE type_kind='attribute'"
        ).fetchall()
        for r in rows:
            try:
                p = json.loads(r["properties"] or "{}")
            except Exception:
                p = {}
            try:
                cons = json.loads(r["constraints"] or "{}")
            except Exception:
                cons = {}
            if not isinstance(p, dict):
                continue
            # 两种历史写法：内联 properties.type / 独立属性 constraints.xsd_type
            declared = p.get("type") if isinstance(p.get("type"), str) else cons.get("xsd_type")
            dom = cons.get("domain_classes") or []
            if dom and not (set(dom) & chain):
                continue  # 该属性不约束此实体类型
            v = props.get(r["name"])
            if v is None or v == "":
                continue
            msg = typevocab.check_value(declared, v)
            if msg:
                errs.append(f"[{entity_type}] 属性 {r['name']}: {msg}")
            # 2026-11-24 A5：min/max/pattern（与 migration 侧同口径）
            lo, hi, pat = cons.get('min'), cons.get('max'), cons.get('pattern')
            if lo is not None or hi is not None:
                try:
                    num = float(v)
                    if (lo is not None and num < float(lo)) or (hi is not None and num > float(hi)):
                        errs.append(f"[{entity_type}] 属性 {r['name']}: 取值「{v}」超出范围 [{lo}, {hi}]")
                except (TypeError, ValueError):
                    pass
            if pat:
                try:
                    import re as _re
                    if not _re.search(str(pat), str(v)):
                        errs.append(f"[{entity_type}] 属性 {r['name']}: 取值「{v}」不匹配模式 {pat}")
                except Exception:
                    pass
        return errs

    def _type_matches(self, actual: str, allowed: list) -> bool:
        """P0-1：类型匹配——actual 等于 allowed 中某类型，或是其子类（子类实例可用于父类型关系）。"""
        if actual in allowed:
            return True
        return any(a in self.ancestors(actual) for a in allowed)

    def validate_edge(self, src_type: str, rel_type: str, tgt_type: str) -> list:
        """校验边实例化：关系类型合法（含来源/目标类型约束，子类可匹配父类约束）。"""
        types = self._load_types()
        errs = []
        if rel_type not in types or types[rel_type]["type_kind"] != "relation":
            known = self.relation_types()
            if known:
                errs.append(f"关系类型 '{rel_type}' 不在本体中（可用: {', '.join(known[:10])}）")
            else:
                errs.append("本体未配置关系类型")
            return errs
        cons = types[rel_type]["constraints"]
        allowed = cons.get("allowed_values") or {}
        allowed_src = allowed.get("src") or []
        allowed_tgt = allowed.get("tgt") or []
        if isinstance(allowed_src, str): allowed_src = [allowed_src]
        if isinstance(allowed_tgt, str): allowed_tgt = [allowed_tgt]
        if allowed_src and not self._type_matches(src_type, allowed_src):
            errs.append(f"关系 {rel_type} 不允许来源类型 '{src_type}'（允许: {allowed_src} 或其子类）")
        if allowed_tgt and not self._type_matches(tgt_type, allowed_tgt):
            errs.append(f"关系 {rel_type} 不允许目标类型 '{tgt_type}'（允许: {allowed_tgt} 或其子类）")
        return errs

    def schema_text(self) -> str:
        """本体 Schema 文本（注入 Agent system_prompt，约束 LLM 按本体抽取与生成）。"""
        types = self._load_types()
        if not types:
            return ""
        # 2026-09-14 修：type_kind 'entity'+'class' 双口径（本体模型建的类型是 class，
        # 旧逻辑只认 entity → class 类型在 SHACL 形状/LLM Schema 注入中双双静默失效）
        ents = [t for t in types.values() if t["type_kind"] in ("entity", "class")]
        rels = [t for t in types.values() if t["type_kind"] == "relation"]
        lines = ["【领域本体约束】知识图谱实例化必须遵循以下类型与规则："]
        if ents:
            def _attrs_of(name):
                """属性摘要：`名:类型[单位]`，让 LLM 知道该填什么格式。

                ★ 2026-11-15 修（真机实测的重大缺陷）：这里此前用
                  `self.merged_properties()`（只读**实体侧** properties），而真库
                  28 个实体的 properties 全为空 ⇒ 注入 system_prompt 的摘要里
                  **28 个实体全部是"属性:无"、11 个属性名一个都不出现**
                  ⇒ AI 建模时不知道这些实体能填什么属性，只能自由发挥。
                  改走 `bound_attributes()`（实体侧 ∪ 属性侧绑定，单一真源）。
                """
                ba = self.bound_attributes(name)
                if not ba:
                    return "无"
                parts = []
                for k, m in ba.items():
                    _t = str(m.get("type") or "").replace("xsd:", "")
                    _u = m.get("unit") or ""
                    s1 = k + (":" + _t if _t else "") + ("[" + _u + "]" if _u else "")
                    if m.get("required"):
                        s1 += "*"      # * = 必填
                    parts.append(s1)
                return ",".join(parts)

            lines.append("实体类型: " + ", ".join(
                f"{t['name']}"
                f"{'(' + '继承:' + '/'.join(self.ancestors(t['name'])) + ')' if self.parents_of(t['name']) else ''}"
                f"(属性:{_attrs_of(t['name'])})"
                for t in ents))
            lines.append("（属性格式：名:数据类型[单位]，带 * 者为必填）")
        if rels:
            lines.append("关系类型: " + ", ".join(t["name"] for t in rels))
        return "\n".join(lines)

    def shacl_export(self) -> str:
        """P0-3：本体约束 → W3C SHACL 形状（Turtle 序列化，可复制/导出/离线校验）。

        对齐行业"SHACL 入库前自动校验"标准（PySHACL/Jena 兼容）：
        - entity 类型 → sh:NodeShape（sh:targetClass；含子类继承的属性与约束）
        - 属性 → sh:property [sh:path; sh:datatype | sh:in(枚举); sh:minCount(必填)]
        - 出边关系 → sh:property [sh:path rel-IRI; sh:class 类IRI]

        P1-1 修复：IRI 全部走 core.ns（类=ontology#enc、属性=prop/enc、关系=rel/enc），
        与图库数据（ABox）逐字节同源——否则 sh:class / sh:path 约束对数据永远落空。
        """
        from core import ns as _ns

        types = self._load_types()
        if not types:
            return ""
        lines = []
        lines.append("@prefix sh: <http://www.w3.org/ns/shacl#> .")
        lines.append("@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .")
        lines.append("@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .")
        # 局部名含 percent-encoding， Turtle 前缀名的 PN_LOCAL 兼容性不稳，统一用 <IRI> 全形
        lines.append(f"@prefix ex: <{_ns.NS_ONTOLOGY}> .")
        lines.append("")

        def _cls(n) -> str:
            return f"<{_ns.class_uri(n)}>"

        def _prop(k) -> str:
            return f"<{_ns.NS_PRED_PROP + _ns.local(_ns.loc_key(k))}>"

        def _rel(p) -> str:
            return f"<{_ns.NS_PRED_REL + _ns.local(_ns.loc_key(p))}>"

        # P1-8（2026-09-07）：数据类型词表统一走 core.typevocab（此前本地映射缺
        # int/boolean 且写法 bool 与其它端不一致，四套词表互不兼容）
        XSD = {}
        # 2026-09-14 修：type_kind 'entity'+'class' 双口径（本体模型建的类型是 class，
        # 旧逻辑只认 entity → class 类型在 SHACL 形状/LLM Schema 注入中双双静默失效）
        ents = [t for t in types.values() if t["type_kind"] in ("entity", "class")]
        rels = [t for t in types.values() if t["type_kind"] == "relation"]

        # ── P0-1（2026-11-15）：属性绑定**双轨**归一 ─────────────────────────
        # 本工程的属性是「全局数据属性 + 绑定到实体」的 OWL/Protégé 模型：
        #   · 属性类型行的 `constraints.domain_classes` = 该属性适⽤于哪些实体类型；
        #   · 实体行的 `properties` = （历史/局部）内联属性定义。
        # 实测（2026-11-15 真库）：**28 个实体的 properties 全部为空**，
        # 绑定关系 100% 存在于 attribute.domain_classes 一侧。
        # 而 shacl_export 此前只读 `merged_properties()`（→ 实体 properties），
        # ⇒ **结构性地看不到任何属性** ⇒ 全库 `sh:datatype` 出现 0 次
        #   ⇒ 实例编辑器的 xsd 强校验、枚举白名单在真实数据上全是死代码。
        # 修法：两侧**并集**消费（不删任何一侧，避免"修一个新问题、断一条旧通路"），
        #   并以「属性侧的声明优先」——它是全局定义，实体侧只是历史局部覆盖。
        for t in ents:
            name = t["name"]
            cons = self._merged_constraints(name)         # P0-1：含父类继承
            # ★ 2026-11-15：属性来源收敛到 `bound_attributes()` **单一真源**
            #   （entity 侧 properties ∪ attribute 侧 domain_classes）。
            #   此前这里有两份循环（一份读实体侧、一份读属性侧），
            #   而 `schema_text()` 又只读实体侧 ⇒ 同一语义两处各写一份，必然漂移
            #   （实测：SHACL 有 56 条 datatype，而注入提示词的本体摘要里却是
            #    28 个"属性:无"、11 个属性名一个不出现）。
            props = []
            for k, meta in self.bound_attributes(name).items():
                block = f"[ sh:path {_prop(k)}"
                # ⚠️ 未声明类型就不发 sh:datatype：`typevocab.xsd_of(None)` 会宽松
                #   兜底成 xsd:string，而 SHACL 里它是**强断言** ⇒ 把"没声明"伪装成
                #   "声明为字符串"，数值属性会被误判违规。
                _xsd_dt = meta.get("type") or ""
                if _xsd_dt:
                    block += f" ; sh:datatype {_xsd_dt}"
                _av = meta.get("allowed_values")
                if isinstance(_av, list) and _av:
                    block += " ; sh:in (" + " ".join(f'"{a}"' for a in _av) + ")"
                if meta.get("required"):
                    block += " ; sh:minCount 1"
                if meta.get("unique"):
                    block += " ; sh:maxCount 1"   # sh:unique 非标准词，唯一性归 maxCount 语义
                # R4：函数型特性（cardinality=1 或 characteristics 含 functional）→ 基数上限
                if cons.get("cardinality") == "1" or "functional" in (cons.get("characteristics") or []):
                    block += " ; sh:maxCount 1"
                # 单位/量纲：SHACL 无标准谓词承载，用 sh:description 作**注解**留痕
                # （不发明自定义谓词去骗校验器；需要真校验单位时得上 SHACL-SPARQL）
                _u = str(meta.get("unit") or "").strip()
                _q = str(meta.get("quantity_kind") or "").strip()
                if _u or _q:
                    _note = "量纲单位：" + (_u or "（无量纲）")
                    if _q:
                        _note += f"；量纲类别：{_q}"
                    block += f' ; sh:description "{_note}"'
                props.append(block + " ]")
            # 出边关系（本类型作为 domain 的关系 → sh:class 约束 range）
            # P1-2 修复（2026-09-06）：同一 path 的多个目标类必须合并为一个 sh:or
            # （并集语义：值属于任一 tgt 类即可）。此前按 tgt 逐类拆成多个
            # sh:property 块，SHACL 多块同 path = 交集语义（值须同时是所有
            # tgt 类的实例）→ 全量校验 1007 条假违规（载荷 Shape 的 CONTAINS
            # 重复 19 块，几乎无数据能同时是 19 个类的实例）。
            rel_tgts: dict = {}   # 关系名 → 目标类名集合
            # P1 精确多重性（2026-09-10）：关系类型 constraints.card_src/card_tgt = {min,max}
            # → 源端类形状 sh:minCount/sh:maxCount（出边）；目标端类经 sh:inversePath（入边）
            rel_cards: dict = {}
            rel_in_cards: dict = {}
            for r in rels:
                av = r.get("constraints", {}).get("allowed_values", {})
                srcs = av.get("src") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                tgts = av.get("tgt") or []
                if isinstance(tgts, str):
                    tgts = [tgts]
                _rc = r.get("constraints", {}) or {}
                if name in srcs:
                    rel_tgts.setdefault(r["name"], set()).update(tgts)
                    _cs = _rc.get("card_src") or {}
                    if isinstance(_cs, dict) and (_cs.get("min") is not None or _cs.get("max") is not None):
                        rel_cards[r["name"]] = _cs
                if name in tgts:
                    _ct = _rc.get("card_tgt") or {}
                    if isinstance(_ct, dict) and (_ct.get("min") is not None or _ct.get("max") is not None):
                        rel_in_cards[r["name"]] = _ct

            def _count_part(card: dict) -> str:
                part = ""
                try:
                    _mn, _mx = card.get("min"), card.get("max")
                    if _mn is not None and int(_mn) > 0:
                        part += f" ; sh:minCount {int(_mn)}"
                    if _mx is not None:
                        part += f" ; sh:maxCount {int(_mx)}"
                except Exception:
                    pass
                return part

            for rname, tgts in sorted(rel_tgts.items()):
                _body = f"sh:path {_rel(rname)}"
                if len(tgts) == 1:
                    _body += f" ; sh:class {_cls(next(iter(tgts)))}"
                elif tgts:
                    # sh:or 内多个 sh:class → 并集语义（pySHACL/Jena 兼容写法）
                    alt = " ".join(f"[ sh:class {_cls(t)} ]" for t in sorted(tgts))
                    _body += f" ; sh:or ( {alt} )"
                _body += _count_part(rel_cards.get(rname) or {})
                props.append("[ " + _body + " ]")
            # 入边基数：目标类形状上的 incoming 边计数（SHACL inversePath，pySHACL 支持）
            for rname, _card in sorted(rel_in_cards.items()):
                props.append("[ sh:path [ sh:inversePath " + _rel(rname) + " ]" + _count_part(_card) + " ]")
            # 谓词统一收集后拼接，避免 targetClass/disjoint/property 之间的分隔符遗漏
            #（P1-1 修复：原实现在"无 disjoint_with 且有 property"时缺 ';'，产物非法 Turtle）
            preds = []
            # R4：互斥类 → sh:not 约束（实例不能同时是两类）
            for _d in (cons.get("disjoint_with") or []):
                preds.append(f"sh:not [ sh:class {_cls(_d)} ]")
            if props:
                preds.extend(f"sh:property {p}" for p in props)
            _chars = cons.get("characteristics") or []
            if _chars:
                # 领域公理标记：OWL 特性（owl:*Property）表达，推理引擎消费，SHACL 注释仅作提示
                preds.append(f'## characteristics: {", ".join(_chars)} → 由 owl:*Property 表达，推理引擎消费')
            shape_iri = f"<{_ns.NS_ONTOLOGY}{_ns.local(_ns.loc_key(name))}Shape>"
            if preds:
                lines.append(f"{shape_iri} a sh:NodeShape ;")
                lines.append(f"  sh:targetClass {_cls(name)} ;")
                lines.append("  " + " ;\n  ".join(preds) + " .")
            else:
                lines.append(f"{shape_iri} a sh:NodeShape ;")
                lines.append(f"  sh:targetClass {_cls(name)} .")
            lines.append("")
        return "\n".join(lines)

    def to_graph(self) -> dict:
        """本体图谱视图（类型层：EntityType 节点 + RelationType 约束边），供前端图谱展示。"""
        types = self._load_types()
        nodes = []
        id_by_name = {t["name"]: t["id"] for t in types.values()}
        for t in types.values():
            if t["type_kind"] != "attribute":
                nodes.append({
                    "id": f"OT-{t['name']}",
                    "name": t["name"],
                    "entity_type": "本体类型" if t["type_kind"] == "entity" else "本体关系",
                    "kind": t["type_kind"],
                    "properties": t.get("properties", {}),
                    "constraints": t.get("constraints", {}),
                    "icon": t.get("icon", ""),
                    "color": t.get("color", "#185FA5"),
                    # P0-1：父类型（subClassOf 层级），供前端树形/图谱展示
                    "parent_id": t.get("parent_id"),
                    "parent_name": next((n for n, i in id_by_name.items() if i == t.get("parent_id")), None),
                })
        edges = []
        for t in types.values():
            if t["type_kind"] != "relation":
                continue
            allowed = t.get("constraints", {}).get("allowed_values", {})
            srcs = allowed.get("src") or []
            tgts = allowed.get("tgt") or []
            if isinstance(srcs, str): srcs = [srcs]
            if isinstance(tgts, str): tgts = [tgts]
            for s in srcs:
                for g in tgts:
                    if f"OT-{s}" in {n['id'] for n in nodes} and f"OT-{g}" in {n['id'] for n in nodes}:
                        edges.append({
                            "source_id": f"OT-{s}",
                            "target_id": f"OT-{g}",
                            "relation_type": t["name"],
                            "kind": "ontology_constraint",
                        })
        # P0-1：层级边（子类 → 父类，kind=subclass 供前端虚线/特殊样式）
        for n in nodes:
            if n.get("parent_name"):
                edges.append({
                    "source_id": n["id"],
                    "target_id": f"OT-{n['parent_name']}",
                    "relation_type": "subClassOf",
                    "kind": "subclass",
                })
        return {"nodes": nodes, "edges": edges, "is_ontology": True}


class GraphStore:
    """KB-P4：图数据库抽象层（GraphStore 接口）。

    SQLite 实现为基线（零依赖）；Neo4j 适配按同一接口实现后通过 GRAPH_ENGINE=neo4j 切换。
    """

    def __init__(self, conn):
        self.conn = conn
        self.validator = OntologyValidator(conn)

    def create_node(self, node_id, name, entity_type, props, branch="dev", x=0, y=0,
                    knowledge_category="", source_doc="", source_type="manual", created_by="未登录"):
        """创建节点（先过本体校验）。返回 (ok, errors|node_id)。

        版本化语义：同 id 允许在**不同分支**分别创建版本行（支撑 dev fork），
        仅拦截「同 id 同分支」重复创建。
        """
        # 2026-11-15：传 branch ⇒ 唯一性按分支判定（同一实例在三分支各存一份）
        errs = self.validator.validate_node(entity_type, props, branch)
        if errs:
            return False, errs
        from repositories.knowledge_repo import KnowledgeRepo
        repo = KnowledgeRepo(self.conn)
        if repo.get_entity(node_id, branch):
            return False, [f"节点已存在: {node_id}（分支 {branch}）"]
        # 2026-09-10 状态机收口：图库内手动创建 = 工程师显式确认行为，直接 reviewed（创建即确认，不再绕审核队列）
        repo.create_entity(node_id, name, entity_type, json.dumps(props, ensure_ascii=False), branch,
                           knowledge_category=knowledge_category, source_doc=source_doc,
                           source_type=source_type, created_by=created_by, status="reviewed")
        self.conn.execute("UPDATE entities SET graph_x=?, graph_y=? WHERE id=? AND branch=?",
                          (x, y, node_id, branch))
        return True, node_id

    def create_edge(self, source_id, target_id, rel_type, props, branch="dev",
                    source_doc="", created_by=""):
        """创建边（先过本体校验：源/目标类型 + 关系类型）。返回 (ok, errors|edge_id)。
        引用完整性：源/目标实体必须与本关系同分支存在——跨分支回退查询会导致
        关系行建在本分支而实体在其它分支（图谱按分支过滤后永不渲染的死行），
        曾产生 33 条脏数据（2026-09-04 清理），此处严禁回退。"""
        from repositories.knowledge_repo import KnowledgeRepo
        repo = KnowledgeRepo(self.conn)
        src = repo.get_entity(source_id, branch)
        tgt = repo.get_entity(target_id, branch)
        errs = []
        if not src:
            errs.append(f"源节点在分支 {branch} 不存在: {source_id}"
                        + ("（存在于其它分支，请先迁移/合并实体）" if repo.get_entity(source_id) else ""))
        if not tgt:
            errs.append(f"目标节点在分支 {branch} 不存在: {target_id}"
                        + ("（存在于其它分支，请先迁移/合并实体）" if repo.get_entity(target_id) else ""))
        if errs:
            return False, errs
        errs = self.validator.validate_edge(src["entity_type"], rel_type, tgt["entity_type"])
        if errs:
            return False, errs
        eid = KnowledgeRepo(self.conn).create_relation(
            source_id, target_id, rel_type, json.dumps(props, ensure_ascii=False), branch,
            source_doc=source_doc, created_by=created_by)
        return True, eid

    def bulk_create(self, nodes, edges, branch="dev"):
        """批量实例化（SysML 导入 / LLM 抽取入图谱前调用）。返回 {created, rejected}。"""
        created, rejected = [], []
        for n in nodes:
            ok, res = self.create_node(
                n.get("id", ""), n.get("name", ""),
                n.get("entity_type", ""), n.get("properties", {}), branch,
                n.get("x", 0), n.get("y", 0),
                source_doc=n.get("source_doc", ""),
                source_type=n.get("source_type", "manual"),
                created_by=n.get("created_by", "未登录"))
            if ok:
                created.append(res)
            else:
                rejected.append({"id": n.get("id", ""), "name": n.get("name", ""), "errors": res})
        for e in edges:
            ok, res = self.create_edge(
                e.get("source_id", ""), e.get("target_id", ""),
                e.get("relation_type", ""), e.get("props", {}), branch,
                source_doc=e.get("source_doc", ""),
                created_by=e.get("created_by", ""))
            if not ok:
                rejected.append({
                    "edge": f"{e.get('source_id')}--{e.get('relation_type')}--{e.get('target_id')}",
                    "errors": res})
        return {"created": created, "rejected": rejected,
                "created_count": len(created), "rejected_count": len(rejected)}


# ───────────────────────────────────────────────────────────────
#  P0-③ 本体边界说明（AI 必读，避免超纲回答）
#  依据：ontology 报告（文章三）§3.5：本体的 3 大局限
#  - 常识边界：本体只能定义明确边界的概念
#  - 完全未知：本体覆盖外的新实体无法推理
#  - 自我意识：本体定义"是什么"，不产生"为什么"
# ───────────────────────────────────────────────────────────────
def boundary_text() -> str:
    """本体边界说明：注入 AI system_prompt，约束 LLM 不超纲回答。

    静态函数——与 schema_text() 不同，不需要 DB 连接，可在 prompt 装配阶段
    直接调用。Agent 端通过 _build_boundary_hint() 取用。
    """
    return (
        "# 本体边界说明（AI 必须遵守）\n\n"
        "## 本体给不了的 3 件事\n"
        "1. **常识边界**：本体只能定义明确边界的概念\n"
        "   - 模糊/连续/语境相关的问题（如「差不多/有点烫/看起来像」）请明确说明不确定性\n"
        "   - 不要给伪精确数字（如「半径大约 3.5 米」——应说「3.5 米左右，可能有 0.x 米偏差」）\n"
        "2. **完全未知**：本体未定义的新实体（如「外星物质」）\n"
        "   - 遇到本体覆盖外的问题，必须明确告知「超纲」，不要编造\n"
        "   - 建议业务方补充建模或查询 Harness\n"
        "3. **自我意识**：本体定义了「Agent 是什么」，但不会让 Agent 产生「我为什么要做」的体验\n"
        "   - 目标/动机/价值判断不在本体范围，转交 Harness 处理\n\n"
        "## 行动约束（AI 必读）\n"
        "- 推理/查询：必须基于本体存在的类、属性、实例\n"
        "- 模糊问题：明确给出「不确定」或「超出本体边界」，不要强行给具体值\n"
        "- 超出范围：不要编造，应建议「超纲」或「需补充建模」\n"
        "- 推断时：说「基于本体的 X 类，可以推断为 Y」而非「X 就是 Y」\n"
        "- 矛盾发现：本体冲突应报告给本体工程师，不要默默忽略\n\n"
        "## 失败模式（应转交 Harness）\n"
        "- 「我理解这个任务但不知道为什么要做」 → 转 Harness 解释目标\n"
        "- 「工具调用失败/超时」 → 转 Harness 重试/降级\n"
        "- 「权限拒绝」 → 转 Harness 申请权限\n"
        "- 「上下文过长」 → 转 Harness 压缩/摘要"
    )


def decision_text() -> str:
    """决策清单文本（AI 必读）：来自 ontology_decisions 表。

    默认空——决策清单由 P1 决策一等化方案补充；本接口预留以便 prompt 装配。
    """
    return ""


# P0-④ 时态管理（标准版）：W3C Time Ontology 适配层
# 依据：ontology 报告（文章三）§2.4 L4 持久世界模型
# 实现：SQL 双时态列 + is_current 标记 + as_of 查询辅助
def temporal_query_filter(as_of=None, range_start=None, range_end=None):
    """生成时态 WHERE 子句与参数。

    Args:
        as_of: 单时点查询（'2026-01-01' 或 '2026-01-01T12:00:00'）
        range_start / range_end: 时段查询（任意时段与查询时段相交）

    Returns:
        (where_clause, params) — 拼到现有 SQL 的 WHERE 后面。
        旧调用不传时 → 返回 ("", [])，与现状一致。
    """
    clauses, params = [], []
    if as_of:
        clauses.append("valid_from <= ?")
        params.append(as_of)
        clauses.append("(valid_to IS NULL OR valid_to > ?)")
        params.append(as_of)
    elif range_start or range_end:
        if range_end:
            clauses.append("valid_from < ?")
            params.append(range_end)
        if range_start:
            clauses.append("(valid_to IS NULL OR valid_to > ?)")
            params.append(range_start)
    return (" AND ".join(clauses), params) if clauses else ("", [])
