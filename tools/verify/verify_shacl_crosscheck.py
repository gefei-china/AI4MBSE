# -*- coding: utf-8 -*-
"""门禁：SHACL（第三方 pySHACL）与自研校验器**逐维度交叉一致**（2026-11-15）。

## 为什么需要它（本仓最看重的那条判据）

本工程所有实例校验都靠**自研**实现：
  · `OntologyValidator.validate_node`（必填/唯一/白名单/类型格式/抽象）
  · `core/typevocab.check_value`（格式正则）
  · `shacl_export`（把约束翻译成 SHACL 形状，但形状**从没被真正的 SHACL 引擎跑过**）

⇒ 一个问题：**如果自研实现有 bug，没有任何人能发现**。
自研的 SHACL 导出再"看起来对"，也只是"我们认为它按 SHACL 语义写"。

本门禁接 **pySHACL 0.40.1**（W3C SHACL 的参考实现之一）做独立校验，
与自研实现**逐维度比对结论** —— 两套独立实现一致，才说明"这条约束真的生效了"。

## 为什么用**夹具**而不是直接跑真库

实测真库的校验面几乎为空：
  · 28 个实体的 `properties` **全为空**（无任何属性值）
  · 实体侧 `required / unique / allowed_values / disjoint_with` **全为 0 条**
  ⇒ 真库上 SHACL 只会有 `sh:datatype` 一条约束，且没有值可校验
  ⇒ 直接跑真库只会得到"两边都 0 违规"，**空过**（本仓已记过："空过的判据
     比没有判据更危险"）。

所以：真库那条只做**连通性**（形状能导出、能被 pySHACL 加载），
**语义一致性全部用合成的坏/好样本**验（4 个维度各一条，判据明确）。

## 四个维度（每个都要求"两边都检出且指向同一属性"）

| # | 维度 | 坏样本 | 自研产出 | pySHACL 产出 |
|---|---|---|---|---|
| 1 | 数据类型 | `P_INT='abc'` | `不符合类型 xsd:int` | `sh:datatype` 违规 |
| 2 | 必填 | 不给 `P_INT` | `缺少必填属性` | `sh:minCount` 违规 |
| 3 | 枚举白名单 | `P_ENUM='C'` | `不在允许值` | `sh:in` 违规 |
| 4 | 唯一性 | 同值已存在 | `重复` | `sh:maxCount 1` 违规 |

## 运行

    python tools/verify/verify_shacl_crosscheck.py

rc=0 全绿 / 1 有红项 / 2 SKIP（无 pyshacl）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PASS = 0
_FAIL = 0
_FAILED = []


def check(name, ok, detail=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [PASS] %s" % name)
    else:
        _FAIL += 1
        _FAILED.append("%s :: %s" % (name, detail))
        print("  [FAIL] %s :: %s" % (name, detail))


# ══════════════════════════════════════════════════════════════════════════
# 合成本体：四个维度各配一条约束
# ══════════════════════════════════════════════════════════════════════════
def _fixture():
    """返回 (conn, rows)：内存库 + 合成类型行。

    ⚠️ 每行必须带 `tlo_anchor`（`_ontology_check` 的列探测需要），
    以及 `id/name/type_kind/parent_id/properties/constraints`（校验器所需）。
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, name TEXT, entity_type TEXT,
            properties TEXT DEFAULT '{}', status TEXT DEFAULT 'reviewed',
            branch TEXT DEFAULT 'dev', project_id TEXT DEFAULT 'p1');
        -- ⚠️ 必须建 ontology_types：`OntologyValidator.validate_node` 会查它做
        --   「属性声明类型」查找（做格式校验）。夹具缺表 ⇒ 当场 no such table。
        CREATE TABLE ontology_types (
            id INTEGER PRIMARY KEY, name TEXT, type_kind TEXT, parent_id INTEGER,
            properties TEXT DEFAULT '{}', constraints TEXT DEFAULT '{}',
            description TEXT DEFAULT '', tlo_anchor TEXT DEFAULT '');
    """)
    rows = [
        {"id": 1, "name": "E1", "type_kind": "entity", "parent_id": None,
         "properties": "{}", "constraints": json.dumps({
             "required": ["P_INT"],
             "unique": ["P_STR"],
             "allowed_values": {"P_ENUM": ["A", "B"]},
         }, ensure_ascii=False), "tlo_anchor": "Object"},
        {"id": 2, "name": "P_INT", "type_kind": "attribute", "parent_id": None,
         "properties": "{}", "constraints": json.dumps({
             "xsd_type": "xsd:int", "domain_classes": ["E1"]}, ensure_ascii=False),
         "tlo_anchor": ""},
        {"id": 3, "name": "P_STR", "type_kind": "attribute", "parent_id": None,
         "properties": "{}", "constraints": json.dumps({
             "xsd_type": "xsd:string", "domain_classes": ["E1"]}, ensure_ascii=False),
         "tlo_anchor": ""},
        {"id": 4, "name": "P_ENUM", "type_kind": "attribute", "parent_id": None,
         "properties": "{}", "constraints": json.dumps({
             "xsd_type": "xsd:string", "domain_classes": ["E1"]}, ensure_ascii=False),
         "tlo_anchor": ""},
    ]
    for t in rows:
        c.execute(
            "INSERT INTO ontology_types "
            "(id,name,type_kind,parent_id,properties,constraints,tlo_anchor) "
            "VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["name"], t["type_kind"], t.get("parent_id"),
             t.get("properties", "{}"), t.get("constraints", "{}"),
             t.get("tlo_anchor", "")))
    c.commit()
    return c, rows


def _data_graph(instances, type_map=None):
    """把 [{id, type, props}] 构造成 rdflib Graph。

    ⚠️ **必须按属性声明的 xsd 类型构造 Literal**（type_map: 属性名 → xsd 完整 IRI）。
    第一版统一用 `rdflib.Literal(str(v))`（无 datatype ⇒ 等价 xsd:string），
    于是任何 `sh:datatype xsd:int` 都对"好样本"判违规 —— 那是**门禁脚本自己的 bug**，
    会把它误读成"两套实现不一致"（本轮实测踩到，报 `Value Node: Literal("5")`
    `Message: Value is not Literal with datatype xsd:int`）。

    ⚠️ URI 必须与 `shacl_export` 里 `_cls()` / `_prop()` **逐字节一致**，
    否则 SHACL 的 targetClass 与 sh:path 都匹配不上 ⇒ 校验"通过"但其实没查到东西
    （这正是本仓反复踩的"看起来验了、其实空过"）。故统一走 core.ns 的构造函数。
    """
    import rdflib
    from core import ns as _ns

    g = rdflib.Graph()
    RDF = rdflib.RDF
    for inst in instances:
        subj = rdflib.URIRef(_ns.ent_uri(inst.get("name", ""), inst["id"]))
        g.add((subj, RDF.type, rdflib.URIRef(_ns.class_uri(inst["type"]))))
        for k, v in (inst.get("props") or {}).items():
            pred = rdflib.URIRef(_ns.NS_PRED_PROP + _ns.local(_ns.loc_key(k)))
            _dt = (type_map or {}).get(k)
            if _dt:
                g.add((subj, pred, rdflib.Literal(str(v), datatype=rdflib.URIRef(_dt))))
            else:
                g.add((subj, pred, rdflib.Literal(str(v))))
    return g


def _pshacl_paths(results_graph, ns_map):
    """从 pySHACL 的结果图里抽出违规的 `sh:path`（局部名），用于与自研报错对齐。"""
    import rdflib
    SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    out = set()
    for res in results_graph.subjects(rdflib.RDF.type, SH.ValidationResult):
        for p in results_graph.objects(res, SH.resultPath):
            s = str(p)
            # 取路径 IRI 的最后一段并做 percent-decode（属性名多为中文）
            tail = s.rstrip("/").rsplit("/", 1)[-1]
            try:
                from urllib.parse import unquote
                tail = unquote(tail)
            except Exception:
                pass
            out.add(tail)
    return out


def main():
    try:
        import pyshacl
        import rdflib
    except ImportError as e:
        print("SKIP: 缺 pySHACL 或 rdflib（%s）" % e)
        return 2

    from ontology_semantics import OntologyValidator
    from core import ns as _ns

    print("=" * 78)
    print("门禁：SHACL（pySHACL %s）与自研校验器逐维度交叉一致" % pyshacl.__version__)
    print("=" * 78)

    conn, rows = _fixture()
    try:
        # 属性名 → xsd 完整 IRI（供 _data_graph 构造带类型的 Literal）
        from core import typevocab as _tv
        _xsd_ns = "http://www.w3.org/2001/XMLSchema#"
        _TYPE_MAP = {}
        for _r in rows:
            if _r["type_kind"] != "attribute":
                continue
            _c = json.loads(_r["constraints"] or "{}")
            _x = _tv.xsd_of(_c.get("xsd_type"))
            # ⚠️ xsd:string **不加显式 datatype**：RDF 1.1 规定 plain literal 等价于
            #   `"x"^^xsd:string`，但 pySHACL 的 `sh:in` 按**严格 RDF 项相等**比较
            #   ⇒ 若这里写成 `Literal('A', datatype=xsd:string)`，就与形状里
            #   `sh:in ("A" "B")` 的 plain literal 不相等，好样本会被误判违规
            #   （本轮实测踩到：paths=['P_ENUM']）。用 plain literal 与真实数据也更贴近
            #   —— 从 JSON 取来的字符串本就没有显式 xsd:string 标注。
            if _x and _x != "xsd:string":
                _TYPE_MAP[_r["name"]] = _xsd_ns + _x.split(":")[-1]

        v = OntologyValidator(conn, rows=rows)
        shapes = v.shacl_export()
        check("A1 合成本体能导出 SHACL 形状（非空）", bool(shapes and len(shapes) > 200),
              "len=%d" % len(shapes or ""))

        # 形状本身必须能被 RDF 解析器读入（否则 pySHACL 报的是语法错，不是语义）
        sg = rdflib.Graph()
        try:
            sg.parse(data=shapes, format="turtle")
            check("A2 SHACL 形状是合法 Turtle（pySHACL 能加载）", len(sg) > 0, "三元组=%d" % len(sg))
        except Exception as e:
            check("A2 SHACL 形状是合法 Turtle（pySHACL 能加载）", False,
                  "%s: %s" % (type(e).__name__, str(e)[:200]))
            return _summary()

        # 形状里四个维度是否都真的生成了（这是"翻译正确"的静态判据）
        sh_txt = shapes
        check("A3 形状含 sh:datatype（数据类型维度已翻译）", "sh:datatype" in sh_txt, "")
        check("A4 形状含 sh:minCount（必填维度已翻译）", "sh:minCount" in sh_txt,
              "真库 requried 全空时不会生成，但夹具里必须生成")
        check("A5 形状含 sh:in（枚举维度已翻译）", "sh:in" in sh_txt, "")
        check("A6 形状含 sh:maxCount（唯一/基数维度已翻译）", "sh:maxCount" in sh_txt, "")

        def run_both(inst, ename="E1"):
            """返回 (自研错误列表, pySHACL 违规路径集合, pySHACL conforms)。"""
            errs = v.validate_node(ename, inst.get("props") or {})
            dg = _data_graph([inst], _TYPE_MAP)
            conforms, rgraph, _txt = pyshacl.validate(
                dg, shacl_graph=sg, inference="none", abort_on_first=False)
            return errs, _pshacl_paths(rgraph, _ns), conforms

        # ── 维度 1：数据类型 ──
        e, paths, conf = run_both({"id": "i1", "name": "n1", "type": "E1",
                                   "props": {"P_INT": "abc", "P_STR": "s1", "P_ENUM": "A"}})
        d1_self = any("P_INT" in x for x in e)
        d1_shacl = "P_INT" in paths
        check("1 数据类型：自研报「不符合类型」（P_INT='abc'）", d1_self, "%s" % e)
        check("1 数据类型：pySHACL 报 sh:datatype 违规且指向 P_INT", d1_shacl, "paths=%s" % sorted(paths))
        check("1 数据类型：两套实现结论一致（都检出同一条）", d1_self and d1_shacl,
              "self=%s shacl=%s" % (d1_self, d1_shacl))

        # ── 维度 2：必填 ──
        e2, paths2, conf2 = run_both({"id": "i2", "name": "n2", "type": "E1",
                                      "props": {"P_STR": "s2", "P_ENUM": "A"}})
        d2_self = any("必填" in x and "P_INT" in x for x in e2)
        d2_shacl = "P_INT" in paths2
        check("2 必填：自研报「缺少必填属性」（不给 P_INT）", d2_self, "%s" % e2)
        check("2 必填：pySHACL 报 sh:minCount 违规且指向 P_INT", d2_shacl, "paths=%s" % sorted(paths2))
        check("2 必填：两套实现结论一致", d2_self and d2_shacl,
              "self=%s shacl=%s" % (d2_self, d2_shacl))

        # ── 维度 3：枚举白名单 ──
        e3, paths3, conf3 = run_both({"id": "i3", "name": "n3", "type": "E1",
                                      "props": {"P_INT": "5", "P_STR": "s3", "P_ENUM": "C"}})
        d3_self = any("P_ENUM" in x for x in e3)
        d3_shacl = "P_ENUM" in paths3
        check("3 枚举：自研报白名单违规（P_ENUM='C' 不在 [A,B]）", d3_self, "%s" % e3)
        check("3 枚举：pySHACL 报 sh:in 违规且指向 P_ENUM", d3_shacl, "paths=%s" % sorted(paths3))
        check("3 枚举：两套实现结论一致", d3_self and d3_shacl,
              "self=%s shacl=%s" % (d3_self, d3_shacl))

        # ── 维度 4：唯一性 ──（自研查库，故先插一条同值实例）
        conn.execute("INSERT INTO entities (id,name,entity_type,properties,status) "
                     "VALUES ('e_dup','dup','E1',?,'reviewed')",
                     (json.dumps({"P_INT": "7", "P_STR": "DUPVAL"}, ensure_ascii=False),))
        conn.commit()
        e4, paths4, conf4 = run_both({"id": "i4", "name": "n4", "type": "E1",
                                      "props": {"P_INT": "7", "P_STR": "DUPVAL", "P_ENUM": "A"}})
        d4_self = any("P_STR" in x for x in e4)
        d4_shacl = "P_STR" in paths4
        check("4a 唯一：自研报重复（P_STR 与存量实例同值）—— 全局唯一它能查", d4_self, "%s" % e4)
        # ⚠️ **这里刻意不断言"两套一致"**，因为 SHACL 语言表达不了"全局唯一"：
        #   `sh:maxCount 1` 的语义是「**单个实例内**该属性最多 1 个值」，
        #   而自研的 unique 是「**全库**该属性值不重复」（查 entities 表）。
        #   要表达全局唯一必须上 `sh:sparql`（SHACL-SPARQL 扩展）。
        #   ⇒ 如实断言"pySHACL **不报**"，证明这个能力差异真实存在且被看见；
        #     而不是硬把两边凑成一致（那会让人误以为 SHACL 能替代自研唯一性检查）。
        check("4b 唯一：pySHACL **不**报（= SHACL 语言无「全局唯一」构件，属已知语义降级）",
              not d4_shacl, "paths=%s —— 若报了就说明我上面的判断过时，需重新界定能力边界"
              % sorted(paths4))
        # 但降级必须**可见**：形状里 unique 应确实翻译成 maxCount，而不是被悄悄丢掉。
        check("4c 唯一：降级被显式记录（形状里 unique → sh:maxCount，不是静默丢弃）",
              "sh:maxCount" in shapes and "P_STR" in shapes,
              "若形状里连 maxCount 都没有，说明 unique 被静默丢了")

        # ── 维度 5（反向）：好样本两边都**不报** ──────────────────────────
        #   没有这一条，"两边一致"可能只是"两边都把什么都报成违规"。
        e5, paths5, conf5 = run_both({"id": "i5", "name": "n5", "type": "E1",
                                      "props": {"P_INT": "5", "P_STR": "OKVAL", "P_ENUM": "A"}})
        check("5 好样本：自研零错误", not e5, "%s" % e5)
        check("5 好样本：pySHACL conforms 且无违规路径",
              conf5 is True and not paths5, "conforms=%s paths=%s" % (conf5, sorted(paths5)))

        # ── 6 变异自证：把 SHACL 的 datatype 断掉 ⇒ 维度 1 的 pySHACL 侧必须翻红 ──
        print("\n--- 变异自证 ---")
        import re as _re
        # 变异的对象是**形状文本**（不改源码）：把 P_INT 所在的 sh:datatype 全删，
        # 再看 pySHACL 是否还报 —— 若不报了，说明维度 1 的断言确实依赖 datatype。
        mut = _re.sub(r"\s*;\s*sh:datatype\s+xsd:int", "", shapes)
        if mut == shapes:
            check("6 变异锚点存在（形状里能定位到 P_INT 的 sh:datatype）", False, "未匹配到")
        else:
            mg = rdflib.Graph()
            mg.parse(data=mut, format="turtle")
            dg1 = _data_graph([{"id": "i1", "name": "n1", "type": "E1",
                                "props": {"P_INT": "abc", "P_STR": "s1", "P_ENUM": "A"}}])
            _c, mres, _t = pyshacl.validate(dg1, shacl_graph=mg, inference="none",
                                            abort_on_first=False)
            mpaths = _pshacl_paths(mres, _ns)
            check("6 变异自证：删掉 sh:datatype xsd:int ⇒ pySHACL 不再报 P_INT（证明断言有鉴别力）",
                  "P_INT" not in mpaths, "变异后 paths=%s" % sorted(mpaths))

        # ── 7 真库连通性（不做语义断言，只证明生产口径的形状能被 pySHACL 吃下）──
        print("\n--- 真库连通性 ---")
        db = os.path.join(_ROOT, "mbse.db")
        if os.path.exists(db):
            con2 = sqlite3.connect(db, timeout=30)
            con2.row_factory = sqlite3.Row
            try:
                v2 = OntologyValidator(con2)   # 消费口径（快照）
                prod = v2.shacl_export()
                pg = rdflib.Graph()
                pg.parse(data=prod, format="turtle")
                check("7 真库导出的 SHACL 形状能被 pySHACL 加载（生产口径合法）",
                      len(pg) > 100, "三元组=%d" % len(pg))
                # 真库实例（有属性值的极少）跑一遍，只要不崩、且不产生"未知谓词"类错误
                rows_inst = con2.execute(
                    "SELECT id,name,entity_type,properties FROM entities "
                    "WHERE status!='deprecated' LIMIT 200").fetchall()
                insts = []
                for r in rows_inst:
                    try:
                        pr = json.loads(r["properties"] or "{}")
                    except Exception:
                        pr = {}
                    insts.append({"id": r["id"], "name": r["name"] or "",
                                  "type": r["entity_type"] or "", "props": pr or {}})
                dg2 = _data_graph([i for i in insts if i["type"]])
                conf_p, rg_p, _ = pyshacl.validate(dg2, shacl_graph=pg,
                                                   inference="none", abort_on_first=False)
                paths_p = _pshacl_paths(rg_p, _ns)
                print("    真库实例 %d 个，conforms=%s，违规路径 %d 类：%s"
                      % (len(insts), conf_p, len(paths_p), sorted(paths_p)[:6]))
                check("7 真库实例校验未崩溃（pySHACL 端到端可跑）", True, "")
            finally:
                con2.close()
        else:
            check("7 真库不存在（SKIP 连通性）", True, "")
    finally:
        conn.close()

    return _summary()


def _summary():
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d" % (_PASS, _FAIL))
    if _FAILED:
        print("红项：")
        for x in _FAILED:
            print("  -", x)
    print("=" * 78)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
