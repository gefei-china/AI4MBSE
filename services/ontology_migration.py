# -*- coding: utf-8 -*-
"""本体变更 → 实例影响分析与自动迁移服务（2026-09-14）。

配套设计文档：docs/本体变更实例影响分析与自动迁移方案.md

三级能力：
- impact_preview：编辑保存前的影响预览（只读）——改名影响面 + 新约束下的实例违例清单。
- build_plan / save_plan：发布挂钩（version/publish）或手动补迁时，从 ontology_change_logs
  的 before/after 留痕生成迁移计划（ops 落 ontology_instance_migrations 表）。
- dry_run / apply：计划预演（计数+抽样，不写业务表）→ 人工确认 → 单事务执行回写。

分级原则（与设计文档一致）：
- L1 内联（改名类，无歧义、幂等 SQL）：在 routers/.../ontology.py::ontology_update_type
  保存事务内直接执行，不经本服务。
- L2 计划（删除/约束收紧类，有歧义或需人工裁决）：本服务负责计划、预演与执行；
  flag_violations / flag_dangling 类 op 只产出清单，不自动改数据。

实例对本体的引用是按名软引用（entities.entity_type / relations.relation_type /
properties 键名 ≈ attribute 名），所有 op 的 SQL/扫描均设计为可重入（幂等）。
"""
import json
from datetime import datetime

# 数据属性 xsd 类型 → Python 校验/转换（轻量实现，避免与 SHACL 校验器耦合）
_XSD_NUM = {"int", "integer", "decimal", "float", "double"}


def _loads(s, d=None):
    try:
        return json.loads(s) if isinstance(s, str) else (s if s is not None else d)
    except Exception:
        return d if d is not None else {}


def _dumps(o):
    return json.dumps(o, ensure_ascii=False, default=str)


def _norm_list(v):
    """约束里的列表字段归一（str/单元素/None → 去空字符串列表）。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() and v != "?" else []
    return [str(x).strip() for x in v if str(x).strip() and str(x) != "?"]


def _xsd_check(v, xsd) -> bool:
    """实例属性值是否满足 xsd 类型（宽松：空值不校验）。"""
    if v is None or v == "":
        return True
    try:
        if xsd in ("int", "integer"):
            int(str(v))
            return True
        if xsd in ("decimal", "float", "double"):
            float(str(v))
            return True
        if xsd == "boolean":
            return str(v).strip().lower() in ("true", "false", "1", "0") or isinstance(v, bool)
        if xsd == "date":
            datetime.strptime(str(v)[:10], "%Y-%m-%d")
            return True
        if xsd == "dateTime":
            s = str(v)[:19].replace("T", " ")
            datetime.strptime(s[:10] if len(s) <= 10 else s, "%Y-%m-%d %H:%M:%S" if len(s) > 10 else "%Y-%m-%d")
            return True
    except Exception:
        return False
    return True  # string/text/enum 未声明类型或未覆盖的格式放行（enum 单独走白名单校验）


def _xsd_convert(v, to_type):
    """属性值类型转换（op=convert_prop_values 用）。成功返回新值，失败返回 None。"""
    try:
        if to_type in ("int", "integer"):
            f = float(str(v))
            return int(f) if f == int(f) else None
        if to_type in ("decimal", "float", "double"):
            return float(str(v))
        if to_type == "boolean":
            s = str(v).strip().lower()
            if s in ("true", "1"):
                return True
            if s in ("false", "0"):
                return False
            return None
        if to_type in ("string", "text"):
            return str(v)
        if to_type == "enum":
            return str(v)
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 实例回溯校验器（impact-preview 与 build_plan 共用）
# ─────────────────────────────────────────────────────────────────────────────

def validate_relation_instances(conn, rel_name: str, cons: dict, limit_per_kind: int = 50) -> list:
    """校验某关系类型名下的存量关系是否满足新约束（dom/range/端点基数）。

    违例条目：{kind, relation_id, source_id, source_type, target_id, target_type, detail}
    """
    av = cons.get("allowed_values") if isinstance(cons.get("allowed_values"), dict) else {}
    srcs, tgts = _norm_list(av.get("src")), _norm_list(av.get("tgt"))
    card_src = cons.get("card_src") if isinstance(cons.get("card_src"), dict) else None
    card_tgt = cons.get("card_tgt") if isinstance(cons.get("card_tgt"), dict) else None
    if not srcs and not tgts and not card_src and not card_tgt:
        return []
    ent_type = {r["id"]: r["entity_type"] for r in conn.execute(
        "SELECT id, entity_type FROM entities WHERE status!='deprecated'").fetchall()}
    ent_name = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM entities").fetchall()}
    rows = conn.execute(
        "SELECT id, source_id, target_id FROM relations WHERE relation_type=? AND status!='deprecated'",
        (rel_name,)).fetchall()
    out = []
    src_cnt, tgt_cnt = {}, {}
    for r in rows:
        st = ent_type.get(r["source_id"], "")
        tt = ent_type.get(r["target_id"], "")
        if srcs and st and st not in srcs:
            out.append({"kind": "domain", "relation_id": r["id"],
                        "source_id": r["source_id"], "source_type": st,
                        "target_id": r["target_id"], "target_type": tt,
                        "detail": f"源端类型「{st}」不在定义域 {srcs} 内"})
        if tgts and tt and tt not in tgts:
            out.append({"kind": "range", "relation_id": r["id"],
                        "source_id": r["source_id"], "source_type": st,
                        "target_id": r["target_id"], "target_type": tt,
                        "detail": f"目标端类型「{tt}」不在值域 {tgts} 内"})
        src_cnt[r["source_id"]] = src_cnt.get(r["source_id"], 0) + 1
        tgt_cnt[r["target_id"]] = tgt_cnt.get(r["target_id"], 0) + 1
    if card_src:
        mx = card_src.get("max")
        if mx not in (None, "", 0):
            for sid, n in src_cnt.items():
                if n > int(mx):
                    out.append({"kind": "card_src", "relation_id": 0, "source_id": sid,
                                "source_type": ent_type.get(sid, ""), "target_id": "", "target_type": "",
                                "detail": f"实例「{ent_name.get(sid, sid)}」源端出边 {n} 条超上限 {mx}"})
    if card_tgt:
        mx = card_tgt.get("max")
        if mx not in (None, "", 0):
            for tid_, n in tgt_cnt.items():
                if n > int(mx):
                    out.append({"kind": "card_tgt", "relation_id": 0, "source_id": "",
                                "source_type": "", "target_id": tid_, "target_type": ent_type.get(tid_, ""),
                                "detail": f"实例「{ent_name.get(tid_, tid_)}」目标端入边 {n} 条超上限 {mx}"})
    return out[:limit_per_kind] if limit_per_kind else out


def validate_attribute_instances(conn, attr_name: str, props: dict, cons: dict,
                                 limit_per_kind: int = 50) -> list:
    """校验存量实例 properties 中该属性键的值是否满足新约束（xsd/白名单）。

    仅检查 domain_classes 命中的实体（未绑定域 = 对所有类型生效）。
    违例条目：{kind, entity_id, entity_name, entity_type, value, detail}
    """
    xsd = cons.get("xsd_type") or (props or {}).get("type") or ""
    allowed = _norm_list(cons.get("allowed_values")) if isinstance(cons.get("allowed_values"), list) else []
    dom_classes = _norm_list(cons.get("domain_classes"))
    if not xsd and not allowed:
        return []
    fil, params = "", []
    if dom_classes:
        fil = " AND entity_type IN (%s)" % ",".join("?" * len(dom_classes))
        params = dom_classes
    rows = conn.execute(
        "SELECT id, name, entity_type, properties FROM entities "
        "WHERE status!='deprecated'" + fil, params).fetchall()
    out = []
    for r in rows:
        props_obj = _loads(r["properties"], {})
        if attr_name not in props_obj:
            continue
        v = props_obj[attr_name]
        if allowed and str(v) not in allowed:
            out.append({"kind": "allowed", "entity_id": r["id"], "entity_name": r["name"],
                        "entity_type": r["entity_type"], "value": v,
                        "detail": f"取值「{v}」不在白名单 {allowed} 内"})
            continue
        if xsd and not _xsd_check(v, xsd):
            out.append({"kind": "xsd", "entity_id": r["id"], "entity_name": r["name"],
                        "entity_type": r["entity_type"], "value": v,
                        "detail": f"取值「{v}」不满足类型 {xsd}"})
    return out[:limit_per_kind] if limit_per_kind else out


def scan_dangling_instances(conn, limit: int = 200) -> list:
    """悬空实例扫描：entity_type 不在本体实体类型名集合内（历史改名/删除遗留）。"""
    type_names = {r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
    rows = conn.execute(
        "SELECT id, name, entity_type, branch, COUNT(*) OVER (PARTITION BY entity_type) AS cnt "
        "FROM entities WHERE status!='deprecated'").fetchall()
    out, seen = [], set()
    for r in rows:
        if r["entity_type"] in type_names or r["entity_type"] in seen:
            continue
        seen.add(r["entity_type"])
        out.append({"entity_type": r["entity_type"], "count": r["cnt"],
                    "sample": [{"id": r["id"], "name": r["name"], "branch": r["branch"]}]})
        if len(out) >= limit:
            break
    return out


def _usage_for_name(conn, name: str, tid=None) -> dict:
    """类型反向引用聚合（与 GET /types/{tid}/usage 同口径，供预览复用）。"""
    children = [{"id": r["id"], "name": r["name"], "type_kind": r["type_kind"]}
                for r in conn.execute(
                    "SELECT id, name, type_kind FROM ontology_types WHERE parent_id=?", (tid or -1,)).fetchall()]
    as_source, as_target = [], []
    for r in conn.execute(
            "SELECT id, name, constraints FROM ontology_types WHERE type_kind='relation'").fetchall():
        cons = _loads(r["constraints"], {})
        av = cons.get("allowed_values") or {}
        if name in _norm_list(av.get("src")):
            as_source.append({"id": r["id"], "name": r["name"]})
        if name in _norm_list(av.get("tgt")):
            as_target.append({"id": r["id"], "name": r["name"]})
    instances = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
        (name,)).fetchone()[0]
    return {"as_parent": children, "as_source": as_source, "as_target": as_target,
            "instances": instances}


# ─────────────────────────────────────────────────────────────────────────────
# 影响预览（编辑保存前，只读）
# ─────────────────────────────────────────────────────────────────────────────

def impact_preview(conn, body: dict) -> dict:
    """预览一次本体类型编辑对实例数据的影响。body = 编辑后的完整类型字段 + 可选 tid。

    返回 {severity, auto_fix, rename, usage, violations, violation_count, summary}
    severity: red=破坏性（改名有实例 / 删除场景）；yellow=新约束下存在违例；green=无影响。
    auto_fix: red 是否可自动修复（L1 内联迁移覆盖 → true，保存即自动迁移）。
    """
    tid = body.get("tid")
    name = str(body.get("name") or "").strip()
    kind = body.get("type_kind") or "entity"
    cons = body.get("constraints") if isinstance(body.get("constraints"), dict) else {}
    if not tid:
        return {"severity": "green", "auto_fix": True, "rename": None,
                "usage": None, "violations": [], "violation_count": 0,
                "summary": "新增类型：无存量实例影响（实例按需挂接新类型）"}
    before = conn.execute("SELECT * FROM ontology_types WHERE id=?", (tid,)).fetchone()
    if not before:
        return {"severity": "green", "auto_fix": True, "rename": None,
                "usage": None, "violations": [], "violation_count": 0,
                "summary": "类型不存在（可能已被删除）"}
    old_name = before["name"]
    usage = _usage_for_name(conn, old_name, tid)
    rename = None
    violations = []
    auto_fix = True
    severity = "green"
    if old_name != name and name:
        if kind == "entity":
            n = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
                (old_name,)).fetchone()[0]
            rename = {"from": old_name, "to": name, "entities": n, "relations": 0}
            if n > 0:
                severity = "red"
        elif kind == "relation":
            n = conn.execute(
                "SELECT COUNT(*) FROM relations WHERE relation_type=?", (old_name,)).fetchone()[0]
            rename = {"from": old_name, "to": name, "entities": 0, "relations": n}
            if n > 0:
                severity = "red"
        else:  # attribute：实例 properties 键跟随改名（L1 内联 JSON 键迁移）
            pat = "%" + old_name.replace("%", r"\%").replace("_", r"\_") + "%"
            n = sum(1 for r in conn.execute(
                "SELECT id, properties FROM entities WHERE properties LIKE ? ESCAPE '\\' AND status!='deprecated'",
                (pat,)).fetchall()
                if old_name in (_loads(r["properties"], {})))
            rename = {"from": old_name, "to": name, "entities": n, "relations": 0, "prop_keys": n}
            if n > 0:
                severity = "red"
    # 新约束下的实例违例（无论是否改名都算——约束收紧影响存量）
    if kind == "relation":
        violations = validate_relation_instances(conn, old_name, cons)
    elif kind == "attribute":
        violations = validate_attribute_instances(
            conn, old_name, _loads(before["properties"], {}) if hasattr(before, "keys") else {}, cons)
    if violations:
        severity = "yellow" if severity != "red" else "red"
        auto_fix = False
    # 摘要
    parts = []
    if rename:
        moved = rename.get("entities") or rename.get("relations") or rename.get("prop_keys") or 0
        parts.append(f"改名「{rename['from']}」→「{rename['to']}」将自动迁移 {moved} 条实例"
                     if moved else f"改名「{rename['from']}」→「{rename['to']}」（无实例引用）")
    if usage.get("instances"):
        parts.append(f"现有实例 {usage['instances']} 条")
    if violations:
        parts.append(f"新约束下 {len(violations)} 条存量违例需人工裁决")
    if not parts:
        parts.append("无实例影响")
    return {"severity": severity, "auto_fix": auto_fix, "rename": rename, "usage": usage,
            "violations": violations, "violation_count": len(violations),
            "summary": "；".join(parts)}


# ─────────────────────────────────────────────────────────────────────────────
# 迁移计划（发布挂钩 / 手动补迁）
# ─────────────────────────────────────────────────────────────────────────────

def diff_type_change(before: dict, after: dict) -> list:
    """单条变更留痕的字段级 diff → 标准化事件（rename 由 L1 内联处理，此处不再产出）。"""
    events = []
    if not before or not after:
        return events
    kind = after.get("type_kind") or before.get("type_kind") or ""
    ob = _loads(before.get("constraints"), {}) if isinstance(before.get("constraints"), (str, dict)) else {}
    na = _loads(after.get("constraints"), {}) if isinstance(after.get("constraints"), (str, dict)) else {}

    def _av(c):
        av = c.get("allowed_values")
        return av if isinstance(av, dict) else {}

    if kind == "relation":
        ob_av, na_av = _av(ob), _av(na)
        removed_src = sorted(set(_norm_list(ob_av.get("src"))) - set(_norm_list(na_av.get("src"))))
        removed_tgt = sorted(set(_norm_list(ob_av.get("tgt"))) - set(_norm_list(na_av.get("tgt"))))
        if removed_src or removed_tgt:
            events.append({"kind": "tighten_domrange", "removed_src": removed_src,
                           "removed_tgt": removed_tgt})
        for side, key in (("src", "card_src"), ("tgt", "card_tgt")):
            oc, nc = ob.get(key), na.get(key)
            oc = oc if isinstance(oc, dict) else {}
            nc = nc if isinstance(nc, dict) else {}
            om = oc.get("max")
            nm = nc.get("max")
            tightened = (om in (None, "", 0) and nm not in (None, "", 0)) or \
                        (om not in (None, "", 0) and nm not in (None, "", 0) and int(nm) < int(om))
            if tightened:
                events.append({"kind": "tighten_card", "side": side, "card_key": key,
                               "old_max": om, "new_max": nm})
    elif kind == "attribute":
        ox = ob.get("xsd_type") or ""
        nx = na.get("xsd_type") or ""
        if nx and ox and nx != ox:
            events.append({"kind": "convert_prop_values", "from_type": ox, "to_type": nx})
        oa = _norm_list(ob.get("allowed_values")) if isinstance(ob.get("allowed_values"), list) else []
        na_ = _norm_list(na.get("allowed_values")) if isinstance(na.get("allowed_values"), list) else []
        removed_av = sorted(set(oa) - set(na_))
        if removed_av:
            events.append({"kind": "tighten_allowed", "removed": removed_av})
    return events


def build_plan(conn, target_prefix: str = "") -> dict:
    """从上一发布之后的 ontology_change_logs 留痕 + 全量悬空扫描生成迁移计划。

    target_prefix：可选目标过滤（如 verify 脚本用唯一前缀隔离测试数据；
    手动补迁也可用前限定类型名族）。空 = 不过滤（全量）。

    返回 {plan_hint, ops: [{op_type, target, payload, affected, change_log_id}]}
    ops 为空 → 本次发布无实例迁移需求。
    """
    rel = conn.execute(
        "SELECT released_at FROM ontology_versions WHERE status='released' AND released_at!='' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    since = rel["released_at"] if rel else ""
    if since:
        logs = conn.execute(
            "SELECT * FROM ontology_change_logs WHERE created_at > ? ORDER BY id", (since,)).fetchall()
    else:
        logs = conn.execute("SELECT * FROM ontology_change_logs ORDER BY id").fetchall()
    ops = []
    for lg in logs:
        before = _loads(lg["before"], {})
        after = _loads(lg["after"], {})
        action = lg["action"]
        if action == "delete":
            name = before.get("name") or ""
            tkind = before.get("type_kind") or ""
            if not name:
                continue
            if tkind == "entity":
                n = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
                    (name,)).fetchone()[0]
                if n:
                    ops.append({"op_type": "deprecate_instances", "target": name,
                                "payload": {"type_name": name, "reason": f"类型「{name}」已删除"},
                                "affected": n, "change_log_id": lg["id"]})
            elif tkind == "relation":
                n = conn.execute(
                    "SELECT COUNT(*) FROM relations WHERE relation_type=? AND status!='deprecated'",
                    (name,)).fetchone()[0]
                if n:
                    ops.append({"op_type": "deprecate_instances", "target": name,
                                "payload": {"type_name": name, "type_kind": "relation",
                                            "reason": f"关系类型「{name}」已删除"},
                                "affected": n, "change_log_id": lg["id"]})
            elif tkind == "attribute":
                pat = "%" + name.replace("%", r"\%").replace("_", r"\_") + "%"
                n = sum(1 for r in conn.execute(
                    "SELECT id, properties FROM entities WHERE properties LIKE ? ESCAPE '\\' AND status!='deprecated'",
                    (pat,)).fetchall()
                    if name in (_loads(r["properties"], {})))
                if n:
                    ops.append({"op_type": "drop_prop_key", "target": name,
                                "payload": {"prop_key": name,
                                            "reason": f"属性类型「{name}」已删除，清理实例死键"},
                                "affected": n, "change_log_id": lg["id"]})
        elif action == "update" and after:
            name = after.get("name") or ""
            kind = after.get("type_kind") or ""
            cons = _loads(after.get("constraints"), {}) if isinstance(after.get("constraints"), (str, dict)) else {}
            for ev in diff_type_change(before, after):
                if kind == "relation" and ev["kind"] in ("tighten_domrange", "tighten_card"):
                    viol = validate_relation_instances(conn, name, cons)
                    ops.append({"op_type": "flag_violations", "target": name,
                                "payload": {"type_kind": "relation", "event": ev,
                                            "reason": f"关系「{name}」约束收紧，存量违例需人工裁决"},
                                "affected": len(viol), "change_log_id": lg["id"],
                                "violations": viol[:50]})
                elif kind == "attribute" and ev["kind"] == "convert_prop_values":
                    conv = _count_convertible(conn, name, cons, ev)
                    ops.append({"op_type": "convert_prop_values", "target": name,
                                "payload": {"prop_key": name, "from_type": ev["from_type"],
                                            "to_type": ev["to_type"],
                                            "reason": f"属性「{name}」类型 {ev['from_type']}→{ev['to_type']}"},
                                "affected": conv["convertible"], "change_log_id": lg["id"],
                                "convert_failed": conv["failed"], "violations": conv["violations"][:50]})
                elif kind == "attribute" and ev["kind"] == "tighten_allowed":
                    viol = validate_attribute_instances(conn, name, {}, cons)
                    ops.append({"op_type": "flag_violations", "target": name,
                                "payload": {"type_kind": "attribute", "event": ev,
                                            "reason": f"属性「{name}」白名单收紧，存量违例需人工裁决"},
                                "affected": len(viol), "change_log_id": lg["id"],
                                "violations": viol[:50]})
    # 悬空扫描（历史遗留：改名前数据 / 未迁移删除）——与留痕无关，全量查
    for d in scan_dangling_instances(conn):
        if target_prefix and not str(d["entity_type"]).startswith(target_prefix):
            continue
        ops.append({"op_type": "flag_dangling", "target": d["entity_type"],
                    "payload": {"type_name": d["entity_type"], "count": d["count"],
                                "sample": d["sample"][:3],
                                "reason": f"实例类型「{d['entity_type']}」已不存在于本体（悬空 {d['count']} 条）"},
                    "affected": d["count"], "change_log_id": 0})
    if target_prefix:
        ops = [o for o in ops if str(o.get("target") or "").startswith(target_prefix)]
    return {"ops": ops, "since": since,
            "affected_total": sum(o["affected"] for o in ops)}


def _count_convertible(conn, attr_name: str, cons: dict, event: dict) -> dict:
    """统计属性值可自动转换/转换失败的条数（convert_prop_values 预估）。"""
    to_type = event.get("to_type") or ""
    dom_classes = _norm_list(cons.get("domain_classes"))
    fil, params = "", []
    if dom_classes:
        fil = " AND entity_type IN (%s)" % ",".join("?" * len(dom_classes))
        params = dom_classes
    ok = fail = 0
    violations = []
    for r in conn.execute(
            "SELECT id, name, entity_type, properties FROM entities "
            "WHERE status!='deprecated'" + fil, params).fetchall():
        po = _loads(r["properties"], {})
        if attr_name not in po:
            continue
        nv = _xsd_convert(po[attr_name], to_type)
        if nv is None:
            fail += 1
            violations.append({"kind": "convert", "entity_id": r["id"], "entity_name": r["name"],
                               "entity_type": r["entity_type"], "value": po[attr_name],
                               "detail": f"取值「{po[attr_name]}」无法自动转换为 {to_type}"})
        else:
            ok += 1
    return {"convertible": ok, "failed": fail, "violations": violations}


def save_plan(conn, plan: dict, version_id: int = 0, user=None) -> int:
    """计划落库。幂等守卫（跨计划去重，防重复补建生成重复 op）：
    - 动作类 op：同 (change_log_id, op_type, target) 已存在未关闭记录 → 跳过
    - 清单类 op（flag_*）：同 (op_type, target) 已存在未关闭记录 → 跳过
      （清单 op 不修数据，重复扫描必然重发现；未关闭= pending/dry_run/applied 均算，
        dismissed 允许重建——用户忽略后数据没变，重建是合理诉求）
    返回 plan_id。"""
    plan_id = conn.execute(
        "SELECT COALESCE(MAX(plan_id),0)+1 FROM ontology_instance_migrations").fetchone()[0]
    actor = (user or {}).get("display_name") if isinstance(user, dict) else ""
    saved = 0
    for op in plan.get("ops", []):
        clid = op.get("change_log_id") or 0
        if op["op_type"].startswith("flag_"):
            dup = conn.execute(
                "SELECT id FROM ontology_instance_migrations "
                "WHERE op_type=? AND target=? AND status!='dismissed' LIMIT 1",
                (op["op_type"], op.get("target") or "")).fetchone()
        else:
            dup = conn.execute(
                "SELECT id FROM ontology_instance_migrations "
                "WHERE change_log_id=? AND op_type=? AND target=? AND status!='dismissed' LIMIT 1",
                (clid, op["op_type"], op.get("target") or "")).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO ontology_instance_migrations "
            "(plan_id, change_log_id, version_id, op_type, target, payload, affected, sample, status, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (plan_id, clid, version_id or 0, op["op_type"],
             op.get("target") or "", _dumps(op.get("payload") or {}),
             op.get("affected") or 0,
             _dumps((op.get("violations") or op.get("sample") or [])[:10]),
             "pending", actor or ""))
        saved += 1
    return plan_id if saved else 0


def _op_estimate(conn, op: dict) -> tuple:
    """dry-run：重算单 op 预计行数 + 抽样 ≤10。返回 (affected, sample)。"""
    p = op.get("payload") or {}
    t = op.get("op_type")
    if t == "flag_violations":
        return op.get("affected") or 0, (op.get("violations") or [])[:10]
    if t == "convert_prop_values":
        cons = {}
        row = conn.execute("SELECT constraints FROM ontology_types WHERE name=?", (op["target"],)).fetchone()
        if row:
            cons = _loads(row["constraints"], {})
        conv = _count_convertible(conn, op["target"], cons,
                                  {"from_type": p.get("from_type"), "to_type": p.get("to_type")})
        return conv["convertible"], conv["violations"][:10]
    if t == "flag_dangling":
        return p.get("count") or 0, (p.get("sample") or [])[:10]
    if t == "migrate_instances":
        if p.get("type_kind") == "relation":
            n = conn.execute("SELECT COUNT(*) FROM relations WHERE relation_type=?",
                             (p.get("from"),)).fetchone()[0]
        else:
            n = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
                (p.get("from"),)).fetchone()[0]
        sample = [{"id": r["id"], "name": r["name"], "branch": r["branch"]} for r in conn.execute(
            "SELECT id, name, branch FROM entities WHERE entity_type=? AND status!='deprecated' LIMIT 10",
            (p.get("from"),)).fetchall()] if p.get("type_kind") != "relation" else []
        return n, sample
    # deprecate_instances / drop_prop_key
    if p.get("type_kind") == "relation":
        n = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE relation_type=? AND status!='deprecated'",
            (p.get("type_name"),)).fetchone()[0]
        return n, []
    if t == "drop_prop_key":
        key = p.get("prop_key") or ""
        pat = "%" + key.replace("%", r"\%").replace("_", r"\_") + "%"
        n = 0
        sample = []
        for r in conn.execute(
                "SELECT id, name, branch, properties FROM entities WHERE properties LIKE ? ESCAPE '\\' AND status!='deprecated'",
                (pat,)).fetchall():
            if key in (_loads(r["properties"], {})):
                n += 1
                if len(sample) < 10:
                    sample.append({"id": r["id"], "name": r["name"], "branch": r["branch"]})
        return n, sample
    n = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type=? AND status!='deprecated'",
        (p.get("type_name"),)).fetchone()[0]
    sample = [{"id": r["id"], "name": r["name"], "branch": r["branch"]} for r in conn.execute(
        "SELECT id, name, branch FROM entities WHERE entity_type=? AND status!='deprecated' LIMIT 10",
        (p.get("type_name"),)).fetchall()]
    return n, sample


def dry_run(conn, plan_id: int) -> dict:
    """计划预演：逐 op 重算预计行数 + 抽样，写回 affected/sample，status=dry_run。不写业务表。"""
    rows = conn.execute(
        "SELECT * FROM ontology_instance_migrations WHERE plan_id=? AND status IN ('pending','dry_run')",
        (plan_id,)).fetchall()
    if not rows:
        return {"ok": False, "error": f"计划 #{plan_id} 无待处理 op（可能已执行）"}
    total = 0
    for r in rows:
        op = {"op_type": r["op_type"], "target": r["target"], "payload": _loads(r["payload"], {}),
              "affected": r["affected"], "violations": _loads(r["sample"], [])}
        affected, sample = _op_estimate(conn, op)
        conn.execute(
            "UPDATE ontology_instance_migrations SET affected=?, sample=?, status='dry_run' WHERE id=?",
            (affected, _dumps(sample), r["id"]))
        total += affected
    conn.commit()
    return {"ok": True, "plan_id": plan_id, "ops": len(rows), "affected_total": total}


def apply(conn, plan_id: int, user=None) -> dict:
    """执行迁移计划：单事务逐 op 回写；flag_* 类只标记清单不改数据；失败整体回滚。"""
    rows = conn.execute(
        "SELECT * FROM ontology_instance_migrations WHERE plan_id=? AND status IN ('pending','dry_run') "
        "ORDER BY id", (plan_id,)).fetchall()
    if not rows:
        return {"ok": False, "error": f"计划 #{plan_id} 无待执行 op"}
    actor = (user or {}).get("display_name") if isinstance(user, dict) else ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    done, notes = [], []
    try:
        for r in rows:
            p = _loads(r["payload"], {})
            t, note = r["op_type"], ""
            if t == "migrate_instances":
                if p.get("type_kind") == "relation":
                    cur = conn.execute("UPDATE relations SET relation_type=? WHERE relation_type=?",
                                       (p.get("to"), p.get("from")))
                else:
                    cur = conn.execute(
                        "UPDATE entities SET entity_type=? WHERE entity_type=? AND status!='deprecated'",
                        (p.get("to"), p.get("from")))
                note = f"已替换 {cur.rowcount or 0} 条"
            elif t == "deprecate_instances":
                if p.get("type_kind") == "relation":
                    cur = conn.execute(
                        "UPDATE relations SET status='deprecated' WHERE relation_type=? AND status!='deprecated'",
                        (p.get("type_name"),))
                else:
                    cur = conn.execute(
                        "UPDATE entities SET status='deprecated' WHERE entity_type=? AND status!='deprecated'",
                        (p.get("type_name"),))
                note = f"已弃用 {cur.rowcount or 0} 条"
            elif t == "drop_prop_key":
                key = p.get("prop_key") or ""
                n = 0
                pat = "%" + key.replace("%", r"\%").replace("_", r"\_") + "%"
                for er in conn.execute(
                        "SELECT id, properties FROM entities WHERE properties LIKE ? ESCAPE '\\' AND status!='deprecated'",
                        (pat,)).fetchall():
                    po = _loads(er["properties"], {})
                    if key not in po:
                        continue
                    po.pop(key, None)
                    conn.execute("UPDATE entities SET properties=? WHERE id=? AND branch=?",
                                 (_dumps(po), er["id"], er["branch"]))
                    n += 1
                note = f"已清理 {n} 条死键"
            elif t == "convert_prop_values":
                to_type = p.get("to_type") or ""
                n = fail = 0
                dom_classes = _norm_list(p.get("domain_classes"))
                fil, params = "", []
                if dom_classes:
                    fil = " AND entity_type IN (%s)" % ",".join("?" * len(dom_classes))
                    params = dom_classes
                for er in conn.execute(
                        "SELECT id, branch, properties FROM entities WHERE status!='deprecated'" + fil,
                        params).fetchall():
                    po = _loads(er["properties"], {})
                    if r["target"] not in po:
                        continue
                    nv = _xsd_convert(po[r["target"]], to_type)
                    if nv is None:
                        fail += 1
                        continue
                    po[r["target"]] = nv
                    conn.execute("UPDATE entities SET properties=? WHERE id=? AND branch=?",
                                 (_dumps(po), er["id"], er["branch"]))
                    n += 1
                note = f"已转换 {n} 条" + (f"，{fail} 条无法自动转换需人工处理" if fail else "")
            elif t in ("flag_violations", "flag_dangling"):
                note = f"违例清单 {r['affected']} 条已存档（人工裁决，不自动改数据）"
            else:
                note = "未知 op 类型，跳过"
            conn.execute(
                "UPDATE ontology_instance_migrations SET status='applied', applied_at=?, applied_by=?, note=? WHERE id=?",
                (now, actor or "", note, r["id"]))
            done.append({"op": t, "target": r["target"], "note": note})
            notes.append(f"{t}:{r['target']} {note}")
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.execute(
            "UPDATE ontology_instance_migrations SET status='failed', note=? WHERE plan_id=? AND status IN ('pending','dry_run')",
            (f"执行失败已回滚: {e}", plan_id))
        conn.commit()
        return {"ok": False, "error": f"迁移执行失败已整体回滚: {e}"}
    return {"ok": True, "plan_id": plan_id, "applied": done}


def list_plans(conn, limit: int = 20) -> dict:
    """迁移计划列表（按 plan 倒序，含各 op 状态）。"""
    ids = [r["plan_id"] for r in conn.execute(
        f"SELECT DISTINCT plan_id FROM ontology_instance_migrations ORDER BY plan_id DESC LIMIT {int(limit)}").fetchall()]
    plans = []
    for pid in ids:
        rows = conn.execute(
            "SELECT * FROM ontology_instance_migrations WHERE plan_id=? ORDER BY id", (pid,)).fetchall()
        ops = []
        for r in rows:
            ops.append({"id": r["id"], "op_type": r["op_type"], "target": r["target"],
                        "payload": _loads(r["payload"], {}), "affected": r["affected"],
                        "status": r["status"], "note": r["note"],
                        "sample": _loads(r["sample"], [])[:10],
                        "version_id": r["version_id"], "change_log_id": r["change_log_id"],
                        "applied_at": r["applied_at"], "applied_by": r["applied_by"]})
        plans.append({"plan_id": pid, "version_id": rows[0]["version_id"] if rows else 0,
                      "created_at": rows[0]["created_at"] if rows else "",
                      "created_by": rows[0]["created_by"] if rows else "",
                      "pending": sum(1 for o in ops if o["status"] in ("pending", "dry_run")),
                      "affected_total": sum(o["affected"] for o in ops if o["status"] in ("pending", "dry_run")),
                      "ops": ops})
    pending = sum(1 for p in plans for o in p["ops"] if o["status"] in ("pending", "dry_run"))
    return {"plans": plans, "pending_plans": sum(1 for p in plans if p["pending"] > 0),
            "pending_ops": pending}
