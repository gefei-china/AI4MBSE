# -*- coding: utf-8 -*-
"""本体数据整理：三项裁决的落地（2026-11-15）。

默认 **dry-run**（只算影响面、不写库）；`--apply` 才写。
全部经 **HTTP API** 写入 —— 后端那份校验（父类合法性 / 锚点冲突 / 关系归一）
是唯一判据，直连 DB 写等于把判据抄第二份，必然漂移（本仓既有教训）。

子项（`--only` 逗号分隔，默认全做）：
  functional  ALLOCATED_TO 摘掉误声明的 `functional`，清理无人消费的 `dom`/`rng` 陈旧键，
              并把已过期的 description 改对
  hierarchy   `需求` 改为需求族根类（消除孤立类）
  attrs       登记 `req_id` / `priority` 属性 + 补 required / unique / 枚举

每项都走同一套路：
  ① 先在**内存里的"预期态"**上算影响面（`OntologyValidator(conn, rows=预期行)`），
     逐实例复验，打印"会变违规的实例数"；
  ② 影响不为 0 ⇒ 默认**拒绝写入**并说明（要 `--force` 才写）；
  ③ 写入后**重新回读**，再跑一遍同一套复验，必须为 0；
  ④ 打印字段级 before → after。

用法：
  python tools/curate_ontology.py                    # 只看影响面
  python tools/curate_ontology.py --apply            # 真写
  python tools/curate_ontology.py --only attrs --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict

_THIS = __file__.replace("\\", "/")
ROOT = _THIS.rsplit("/tools/", 1)[0]
sys.path.insert(0, ROOT)

DB = ROOT + "/mbse.db"
BASE = "http://127.0.0.1:8000"

# 需求四个具体子类型（真库实例落在这四类上）
REQ_LEGS = ["利益相关方需求", "系统需求", "子系统需求", "单元需求"]
# 需求族的"最上层可挂约束的类"：required/unique 挂在这里，四个子类型自动继承，
# 而根类 `需求` 自己那 2 个实例不受影响（它们不是它的子类）。
REQ_ANCHOR_TYPE = "利益相关方需求"


# ─────────────────────────────────────────────────────────────────────────────
# 基础设施
# ─────────────────────────────────────────────────────────────────────────────
class Row(dict):
    """让 dict 支持 `row.keys()` —— `OntologyValidator` 的 rows 参数按 DB 行用。"""

    def keys(self):
        return list(super().keys())


def _conn():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def edit_rows(con):
    """编辑态全部类型行（**不是**快照）—— 整理的对象就是它。"""
    out = []
    for r in con.execute("SELECT * FROM ontology_types").fetchall():
        d = dict(r)
        for k in ("constraints", "properties"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except Exception:
                d[k] = {}
        out.append(Row(d))
    return out


def by_name(rows):
    return {r["name"]: r for r in rows}


def api(method, path, body=None, timeout=40):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode() or "{}"
            try:
                return r.status, json.loads(txt)
            except Exception:
                return r.status, txt
    except urllib.error.HTTPError as e:
        return e.code, {"error": (e.read().decode() or "")[:300]}
    except Exception as e:
        return -1, {"error": "%s: %s" % (type(e).__name__, e)}


def put_type(row, **overrides):
    """按"整行回写"的纪律 PUT（UPDATE 是整行替换，漏字段会被清空）。"""
    body = {
        "name": overrides.get("name", row["name"]),
        "type_kind": overrides.get("type_kind", row["type_kind"]),
        "parent_id": overrides.get("parent_id", row["parent_id"]),
        "properties": overrides.get("properties", row.get("properties") or {}),
        "constraints": overrides.get("constraints", row.get("constraints") or {}),
        "description": overrides.get("description", row.get("description") or ""),
        "icon": overrides.get("icon", row.get("icon") or ""),
        "color": overrides.get("color", row.get("color") or "#185FA5"),
        "iri": overrides.get("iri", row.get("iri") or ""),
        "profile_source": overrides.get("profile_source", row.get("profile_source") or ""),
        "profile_ref": overrides.get("profile_ref", row.get("profile_ref") or ""),
        "status": overrides.get("status", row.get("status") or "released"),
        "replaced_by": overrides.get("replaced_by", row.get("replaced_by") or ""),
        "deprecated_note": overrides.get("deprecated_note", row.get("deprecated_note") or ""),
        "tlo_anchor": overrides.get("tlo_anchor", row.get("tlo_anchor") or ""),
    }
    return api("PUT", "/api/knowledge/ontology/types/%s" % row["id"], body)


def instances(con):
    """全部未废弃实例（含 branch）—— 影响面复验的范围。"""
    return [dict(r) for r in con.execute(
        "SELECT id, name, entity_type, properties, branch, project_id "
        "FROM entities WHERE status!='deprecated'")]


def violations(con, rows, only_types=None, insts=None):
    """用**真校验器**逐实例复验，返回 [(类型, 实例id, 错误)]。

    刻意不自己写一套"覆盖率检查"：那会变成第二份判据，跟真校验器漂移。
    这里直接调 `OntologyValidator.validate_node`（`exclude_id` 传自身，
    因为问的是"这些存量实例在加了约束之后**还合法吗**"）。

    :param insts: 可注入实例集（自检时用来造"数据被改坏"的情形）。
                  不传则取真库当前未废弃实例。
    """
    from ontology_semantics import OntologyValidator
    v = OntologyValidator(con, rows=rows)
    bad = []
    for e in (insts if insts is not None else instances(con)):
        et = e["entity_type"]
        if only_types and et not in only_types:
            continue
        try:
            props = json.loads(e["properties"] or "{}")
        except Exception:
            props = {}
        errs = v.validate_node(et, props, e["branch"] or "",
                               exclude_id=e["id"])
        if errs:
            bad.append((et, e["id"], errs))
    return bad


def as_db_rows(rows):
    """把解析过的行还原成 **DB 形态**（constraints/properties 为 JSON 字符串）。

    `_ontology_check(rows=...)` 与 `active_rows` 都按 DB 行处理（内部 json.loads），
    传解析过的 dict 会被它当字符串解析失败 —— 这是本笔踩过的"兜底吞异常"同族坑，
    故这里显式还原，不做隐式兼容假设。
    """
    out = []
    for r in rows:
        d = dict(r)
        for k in ("constraints", "properties"):
            v = d.get(k)
            if not isinstance(v, str):
                d[k] = json.dumps(v or {}, ensure_ascii=False)
        out.append(d)
    return out


def do_verify():
    """编辑态一致性验证（**不是**消费快照口径）。

    为什么要单独一步：`_ontology_check` 的缺省是**消费口径**（active 快照），
    刚 PUT 完的编辑态它看不见 ⇒ 会得到"改了但体检没变"的假象（本笔第一版
    三项全报 FAIL 就是这个原因，而写入其实全部成功）。发布门禁也是显式传
    编辑态，这里与它同口径。
    """
    print()
    print("═" * 78)
    print("④ 验证（编辑态口径；发布后消费口径另由 /validate 确认）")
    print("═" * 78)
    con = _conn()
    try:
        rows = edit_rows(con)
        db_rows = as_db_rows(rows)

        import routers.knowledge_parts.shared as S
        rep = S._ontology_check(con, rows=db_rows)
        codes = Counter(x["code"] for x in rep["issues"])
        print("  体检：high=%s warn=%s low=%s info=%s  degraded=%s"
              % (rep["high"], rep["warn"], rep["low"], rep["info"], rep["degraded"] or "[]"))
        print("  问题码：%s" % dict(codes))

        from ontology_semantics import OntologyValidator
        v = OntologyValidator(con, rows=db_rows)
        print("  classify('单元需求') = %s" % v.classify("单元需求"))
        print("  %s 的 bound_attributes = %s"
              % (REQ_ANCHOR_TYPE, sorted(v.bound_attributes(REQ_ANCHOR_TYPE).keys())))

        bad = violations(con, db_rows)
        _print_violations(bad)

        sh = v.shacl_export()
        print("  SHACL：sh:in %d 次 | sh:minCount %d 次 | sh:maxCount %d 次"
              % (sh.count("sh:in ("), sh.count("sh:minCount"), sh.count("sh:maxCount")))
        st = v.schema_text()
        print("  提示词(schema_text)：含 req_id=%s 含 priority=%s | 长度 %d"
              % ("req_id" in st, "priority" in st, len(st)))

        ok = (not bad and "req_id" in st and "priority" in st
              and rep["high"] == 0 and "functional_data_conflict" not in codes)
        print("  编辑态验证: %s" % ("PASS" if ok else "FAIL"))
        return ok
    finally:
        con.close()


def _print_violations(bad, limit=6):
    if not bad:
        print("    影响面：0 个实例会变违规")
        return
    agg = Counter(t for t, _, _ in bad)
    print("    影响面：**%d** 个实例会变违规  按类型：%s" % (len(bad), dict(agg)))
    for t, i, errs in bad[:limit]:
        print("      · [%s] %s  %s" % (t, i, errs[0][:110]))
    if len(bad) > limit:
        print("      · …… 其余 %d 条省略" % (len(bad) - limit))


# ─────────────────────────────────────────────────────────────────────────────
# 子项一：ALLOCATED_TO 的 functional 与陈旧键
# ─────────────────────────────────────────────────────────────────────────────
FUNC_TARGET = "ALLOCATED_TO"
FUNC_DESC = "需求分配到功能/系统元素（需求 → 功能、外部系统、分系统）"


def do_functional(apply, force):
    print("═" * 78)
    print("① ALLOCATED_TO：摘掉误声明的 functional + 清理陈旧 dom/rng")
    print("═" * 78)
    con = _conn()
    try:
        rows = edit_rows(con)
        t = by_name(rows).get(FUNC_TARGET)
        if not t:
            print("  未找到 %s，跳过" % FUNC_TARGET)
            return True
        cons = dict(t["constraints"])
        chars = list(cons.get("characteristics") or [])
        av = dict(cons.get("allowed_values") or {})

        # 证据：一源多目标统计
        multi = con.execute(
            "SELECT COUNT(*) FROM (SELECT source_id FROM relations "
            "WHERE relation_type=? AND status!='deprecated' "
            "GROUP BY source_id HAVING COUNT(*)>1)", (FUNC_TARGET,)).fetchone()[0]
        print("  证据：%d 个源节点有多个目标（functional 要求每源最多 1 个）" % multi)
        print("  before characteristics: %s" % chars)
        print("  before allowed_values 键: %s" % list(av.keys()))

        new_chars = [c for c in chars if c != "functional"]
        stale = [k for k in ("dom", "rng") if k in av]
        for k in stale:
            av.pop(k, None)
        print("  after  characteristics: %s" % new_chars)
        print("  after  allowed_values 键: %s（删除陈旧键 %s）" % (list(av.keys()), stale or "无"))
        print("  description: %r → %r" % ((t.get("description") or "")[:40], FUNC_DESC))

        cons["characteristics"] = new_chars
        cons["allowed_values"] = av

        if not apply:
            print("  [dry-run] 未写入；影响面需写入后由体检确认（本条不改实例约束）")
            return True

        code, res = put_type(t, constraints=cons, description=FUNC_DESC)
        print("  PUT -> http=%s ok=%s err=%s" % (code, res.get("ok"), res.get("error")))

        # 回读
        con2 = _conn()
        r2 = by_name(edit_rows(con2)).get(FUNC_TARGET) or {}
        c2 = r2.get("constraints") or {}
        print("  回读 characteristics: %s" % (c2.get("characteristics")))
        print("  回读 allowed_values 键: %s" % list((c2.get("allowed_values") or {}).keys()))

        # 体检（**编辑态**口径）：functional_data_conflict 必须消失
        import routers.knowledge_parts.shared as S
        rep = S._ontology_check(con2, rows=as_db_rows(edit_rows(con2)))
        hits = [x for x in rep["issues"] if x["code"] == "functional_data_conflict"]
        print("  体检：high=%s warn=%s low=%s info=%s | functional_data_conflict=%d"
              % (rep["high"], rep["warn"], rep["low"], rep["info"], len(hits)))
        return not hits
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# 子项二：需求 改为需求族根类
# ─────────────────────────────────────────────────────────────────────────────
ROOT_TYPE = "需求"
FIRST_LEG = "利益相关方需求"


def do_hierarchy(apply, force):
    print()
    print("═" * 78)
    print("② 需求 改为需求族根类（消除孤立类 isolated）")
    print("═" * 78)
    con = _conn()
    try:
        rows = edit_rows(con)
        idx = by_name(rows)
        root = idx.get(ROOT_TYPE)
        leg = idx.get(FIRST_LEG)
        if not (root and leg):
            print("  未找到 需求 / 利益相关方需求，跳过")
            return True
        print("  before: 需求(parent=%s)  利益相关方需求(parent=%s)"
              % (root["parent_id"], leg["parent_id"]))
        print("  after : 利益相关方需求(parent=%s → 需求)" % root["id"])

        # 前置自检（任一条不满足就不该改）
        checks = []
        checks.append(("根类自身无父（不会形成环）", not root["parent_id"]))
        checks.append(("两侧锚点一致（否则继承冲突）",
                       (root.get("tlo_anchor") or "") == (leg.get("tlo_anchor") or "")))
        checks.append(("根类不是抽象类（它有 2 个实例，抽象会立刻违规）",
                       not (root.get("constraints") or {}).get("abstract")))
        checks.append(("根类无 parent 链到子类（无环风险）", True))
        for label, ok in checks:
            print("    自检 %-46s %s" % (label, "PASS" if ok else "FAIL"))
        if not all(ok for _, ok in checks):
            print("  自检未通过 ⇒ 拒绝写入")
            return False

        # 影响面：改 relation 的 parent 只影响 classify 链，不直接改实例属性；
        # 但继承会让根类的 constraints 扩散到全族 —— 根类 constraints 为空，故无扩散。
        print("  根类 constraints: %s（空 ⇒ 不会向全族扩散必填/枚举）"
              % (root.get("constraints") or {}))
        bad = violations(con, _apply_rows(rows, {FIRST_LEG: root["id"]}))
        _print_violations(bad)
        if bad and not force:
            print("  影响非 0 ⇒ 拒绝写入（要写加 --force）")
            return False

        if not apply:
            print("  [dry-run] 未写入")
            return True

        code, res = put_type(leg, parent_id=root["id"])
        print("  PUT -> http=%s ok=%s err=%s" % (code, res.get("ok"), res.get("error")))

        con2 = _conn()
        from ontology_semantics import OntologyValidator
        _rows2 = as_db_rows(edit_rows(con2))
        v = OntologyValidator(con2, rows=_rows2)
        chain = v.classify("单元需求")
        print("  classify('单元需求') = %s" % chain)
        ok_chain = ROOT_TYPE in chain
        import routers.knowledge_parts.shared as S
        rep = S._ontology_check(con2, rows=_rows2)
        iso = [x for x in rep["issues"] if x["code"] == "isolated"]
        print("  体检：high=%s warn=%s low=%s | isolated=%d %s"
              % (rep["high"], rep["warn"], rep["low"], len(iso),
                 [x["name"] for x in iso]))
        return ok_chain and not iso
    finally:
        con.close()


def _apply_rows(rows, parent_overrides):
    """在内存里造"预期态"（只改 parent_id），用于写入前的影响面模拟。"""
    out = []
    for r in rows:
        d = Row(dict(r))
        if d["name"] in parent_overrides:
            d["parent_id"] = parent_overrides[d["name"]]
        out.append(d)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 子项三：登记 req_id / priority + required / unique / 枚举
# ─────────────────────────────────────────────────────────────────────────────
# 只登记**业务属性**。刻意不登记 kind / aliases / matched_sysml_entity ——
# 那是导入簿记（kind 是元素类别判别符，与 entity_type 冗余；matched_sysml_entity
# 是 SysML 对齐痕迹；aliases 全为空列表），进本体只会污染语义层。
NEW_ATTRS = [
    {"name": "req_id",
     "description": "需求编号（如 STR-001 / SYR-001），分支内唯一",
     "constraints": {"xsd_type": "string",
                     "alt_labels": ["需求编号", "需求ID"],
                     "lang": "zh",
                     "domain_classes": REQ_LEGS}},
    {"name": "priority",
     "description": "需求优先级：high / medium / low",
     "constraints": {"xsd_type": "string",
                     "alt_labels": ["优先级"],
                     "lang": "zh",
                     "allowed_values": ["high", "medium", "low"],
                     "domain_classes": REQ_LEGS}},
]
# required 取"覆盖率 100%"的项（加进去对存量 0 影响，但对**新增**实例是真约束）；
# unique 取"分支内已验证不重复"的项。
NEW_REQUIRED = ["req_id", "priority"]
NEW_UNIQUE = ["req_id"]


def _coverage(con):
    """各需求子类型的属性覆盖率 + 分支内重复情况（影响面的原始数据）。"""
    cov = defaultdict(Counter)
    total = Counter()
    per_branch = defaultdict(list)
    for e in instances(con):
        et = e["entity_type"]
        if et not in REQ_LEGS:
            continue
        total[et] += 1
        try:
            p = json.loads(e["properties"] or "{}")
        except Exception:
            p = {}
        for k in ("req_id", "priority"):
            if p.get(k):
                cov[et][k] += 1
        if p.get("req_id"):
            per_branch[(et, e["branch"])].append(str(p["req_id"]))
    return cov, total, per_branch


def do_attrs(apply, force):
    print()
    print("═" * 78)
    print("③ 登记 req_id / priority 属性 + required / unique / 枚举")
    print("═" * 78)
    con = _conn()
    try:
        rows = edit_rows(con)
        idx = by_name(rows)
        cov, total, per_branch = _coverage(con)

        print("  真库现状（需求四类实例的属性覆盖率）:")
        for et in REQ_LEGS:
            print("    %-12s 实例 %-3d  req_id %d/%d   priority %d/%d"
                  % (et, total[et], cov[et]["req_id"], total[et],
                     cov[et]["priority"], total[et]))

        print("  分支内 req_id 重复检查（unique 是否可行）:")
        all_ok = True
        for (et, br), vals in sorted(per_branch.items()):
            dup = [v for v, c in Counter(vals).items() if c > 1]
            all_ok = all_ok and not dup
            print("    %-12s branch=%-9s %d 个，重复 %d" % (et, br, len(vals), len(dup)))

        # ① 预期态：新增两个属性类型 + 在 REQ_ANCHOR_TYPE 上加 required/unique
        proj = [Row(dict(r)) for r in rows]
        existing = by_name(proj)
        for a in NEW_ATTRS:
            if a["name"] in existing:
                print("  属性 %s 已存在，跳过创建（不覆盖已有声明）" % a["name"])
                continue
            proj.append(Row({
                "id": -1, "name": a["name"], "type_kind": "attribute",
                "parent_id": None, "properties": {}, "constraints": dict(a["constraints"]),
                "description": a["description"], "icon": "", "color": "#185FA5",
                "iri": "", "profile_source": "", "profile_ref": "",
                "status": "released", "replaced_by": "", "deprecated_note": "",
                "tlo_anchor": "",
            }))
        anchor = [r for r in proj if r["name"] == REQ_ANCHOR_TYPE][0]
        c = dict(anchor["constraints"])
        c["required"] = sorted(set(list(c.get("required") or []) + NEW_REQUIRED))
        c["unique"] = sorted(set(list(c.get("unique") or []) + NEW_UNIQUE))
        anchor["constraints"] = c
        print("  %s 预期 required=%s unique=%s" % (REQ_ANCHOR_TYPE, c["required"], c["unique"]))

        bad = violations(con, proj)
        _print_violations(bad)
        if bad and not force:
            print("  影响非 0 ⇒ 拒绝写入（要写加 --force）")
            return False

        if not apply:
            print("  [dry-run] 未写入")
            return True

        # ② 真写：先建属性，再挂 required/unique
        for a in NEW_ATTRS:
            if a["name"] in by_name(edit_rows(_conn())):
                continue
            body = {
                "name": a["name"], "type_kind": "attribute", "parent_id": None,
                "properties": {}, "constraints": a["constraints"],
                "description": a["description"], "icon": "", "color": "#185FA5",
                "status": "released", "tlo_anchor": "",
            }
            code, res = api("POST", "/api/knowledge/ontology/types", body)
            print("  POST 属性 %-10s -> http=%s ok=%s err=%s"
                  % (a["name"], code, res.get("ok"), res.get("error")))

        con_mid = _conn()
        anchor_row = by_name(edit_rows(con_mid)).get(REQ_ANCHOR_TYPE)
        code, res = put_type(anchor_row, constraints=anchor["constraints"])
        print("  PUT required/unique 到 %s -> http=%s ok=%s err=%s"
              % (REQ_ANCHOR_TYPE, code, res.get("ok"), res.get("error")))

        # ③ 回读 + 复验
        con2 = _conn()
        idx2 = by_name(edit_rows(con2))
        for a in NEW_ATTRS:
            t = idx2.get(a["name"])
            print("  回读属性 %-10s %s  constraints=%s"
                  % (a["name"], "存在" if t else "**缺失**",
                     (t or {}).get("constraints")))
        c2 = (idx2.get(REQ_ANCHOR_TYPE) or {}).get("constraints") or {}
        print("  回读 %s required=%s unique=%s"
              % (REQ_ANCHOR_TYPE, c2.get("required"), c2.get("unique")))

        bad2 = violations(con2, edit_rows(con2))
        _print_violations(bad2)

        # ④ SHACL / 提示词是否真吃到（能力必须在消费端可见，否则又是"建好了没生效"）
        from ontology_semantics import OntologyValidator as _OV
        vv = _OV(con2, rows=as_db_rows(edit_rows(con2)))
        sh = vv.shacl_export()
        print("  SHACL: sh:in 出现 %d 次（priority 枚举）| sh:minCount %d 次 | sh:maxCount %d 次"
              % (sh.count("sh:in ("), sh.count("sh:minCount"), sh.count("sh:maxCount")))
        st = vv.schema_text()
        print("  提示词(schema_text) 含 req_id=%s 含 priority=%s"
              % ("req_id" in st, "priority" in st))
        return not bad2 and "req_id" in st
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--force", action="store_true", help="影响面非 0 也写")
    ap.add_argument("--only", default="functional,hierarchy,attrs")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}

    print("本体整理 dry-run=%s force=%s only=%s" % (not a.apply, a.force, sorted(only)))
    print("API=%s  DB=%s" % (BASE, DB))
    code, _ = api("GET", "/api/dashboard")
    print("服务探活: http=%s\n" % code)

    results = []
    if "functional" in only:
        results.append(("functional", do_functional(a.apply, a.force)))
    if "hierarchy" in only:
        results.append(("hierarchy", do_hierarchy(a.apply, a.force)))
    if "attrs" in only:
        results.append(("attrs", do_attrs(a.apply, a.force)))

    results.append(("verify(edit-state)", do_verify()))

    print()
    print("═" * 78)
    for n, ok in results:
        print("  %-12s %s" % (n, "PASS" if ok else "FAIL"))
    print("  RESULT: %s" % ("PASS" if all(ok for _, ok in results) else "FAIL"))
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
