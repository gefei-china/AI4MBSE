"""SWRL 确定性物化器（Python/SQL 评估）：保障「规则可执行」可落地可验证。

背景：owlready2 捆绑的 Pellet 2.3.1 在 Python 3.14 + owlready2 环境下，推理后的
推断公理无法回灌（DL 分类、SWRL 头均不 materialize 回实体）。这是环境级限制，
非引擎代码 bug。为让"SWRL 规则 ≥3 条可注册可执行"的验收指标真正达成且可用
Playwright/SQL 验证，提供本确定性物化器作为可靠执行通道。

支持子集：
  body = Atom, Atom, ...   （AND）
  head = Atom
Atom：
  - X(?var)                    var 的 entity_type == X
  - objectProp(?a, ?b)         存在 a --objectProp--> b
  - dataProp(?a, ?val)         数据属性取值/约束
  - swrlb:op(?x, C) / swrlb:op(?x, ?y)   比较内置 >= <= > < = !=
head：
  - dataProp(?var, literal)    推断数据属性断言
  - objectProp(?a, ?b)         推断对象属性断言
"""
from __future__ import annotations

import json
import re
import sqlite3

_ATOM_RE = re.compile(
    r'^\s*(swrlb:)?\s*([A-Za-z_][\w:<>/.-]*)\s*\('
    r'(.*?)\s*\)\s*$',
    re.DOTALL,
)


def split_atoms(text: str) -> list:
    parts = []
    depth = 0
    cur = ""
    for ch in (text or ""):
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return [p for p in parts if p]


_OPS = {
    "greaterthanorequal", "greaterthan", "lessthanorequal", "lessthan",
    "equal", "notequal", ">", "<", ">=", "<=", "=", "!=",
}


def parse_atom(atom: str):
    m = _ATOM_RE.match(atom)
    if not m:
        return None
    swrlb, pred, raw_args = m.group(1), m.group(2), m.group(3)
    args = [a.strip() for a in raw_args.split(",") if a.strip()]
    pred_low = pred.split("#")[-1].lower()
    is_builtin = bool(swrlb) or (pred_low in _OPS)
    return {"builtin": is_builtin, "pred": pred, "args": args}


def is_var(s: str) -> bool:
    return bool(s) and s.startswith("?")


def strip_literal(s: str) -> str:
    s = s.strip()
    if "^^" in s:
        s = s.split("^^", 1)[0]
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s


def _all_entities(conn):
    rows = conn.execute(
        "SELECT id, name, entity_type, properties FROM entities "
        "WHERE is_current=1 AND status<>'deprecated'"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {"id": r[0], "name": r[1], "entity_type": r[2], "properties": r[3]}
        try:
            d["_props"] = json.loads(d.get("properties") or "{}")
        except Exception:
            d["_props"] = {}
        out.append(d)
    return out


def _relations(conn, pred):
    rows = conn.execute(
        "SELECT source_id, target_id FROM relations WHERE relation_type=? AND status<>'deprecated'",
        (pred,),
    ).fetchall()
    return [{"source_id": r[0], "target_id": r[1]} for r in rows]


def _entity_by_id(conn, eid, entities):
    for e in entities:
        if e["id"] == eid:
            return e
    return {"id": eid, "entity_type": "", "_props": {}}


def _pred_kind(conn, pred):
    row = conn.execute(
        "SELECT type_kind FROM ontology_types WHERE name=?", (pred,)
    ).fetchone()
    if row is None:
        return "attribute"
    return row[0] if not hasattr(row, "keys") else row["type_kind"]


def _resolve_value(conn, arg, b, entities, literal=False):
    if is_var(arg):
        v = b.get(arg)
        if isinstance(v, dict):
            return v.get("id")
        return v
    if literal:
        return strip_literal(arg)
    return arg


def _op_map(pred):
    p = pred.split("#")[-1].lower()

    def num(x, y):
        try:
            float(x)
            float(y)
            return True
        except Exception:
            return False

    m = {
        "greaterthanorequal": lambda x, y: (float(x) >= float(y)) if num(x, y) else str(x) >= str(y),
        "greaterthan": lambda x, y: (float(x) > float(y)) if num(x, y) else str(x) > str(y),
        "lessthanorequal": lambda x, y: (float(x) <= float(y)) if num(x, y) else str(x) <= str(y),
        "lessthan": lambda x, y: (float(x) < float(y)) if num(x, y) else str(x) < str(y),
        "equal": lambda x, y: str(x) == str(y),
        "notequal": lambda x, y: str(x) != str(y),
        ">": lambda x, y: float(x) > float(y),
        "<": lambda x, y: float(x) < float(y),
        ">=": lambda x, y: float(x) >= float(y),
        "<=": lambda x, y: float(x) <= float(y),
        "=": lambda x, y: str(x) == str(y),
        "!=": lambda x, y: str(x) != str(y),
    }
    return m.get(p)


def _apply_atom(conn, a, bindings):
    result = []
    entities = _all_entities(conn)
    if a.get("builtin"):
        op = _op_map(a["pred"])
        args = a["args"]
        if not op or len(args) < 2:
            return []
        for b in bindings:
            x = _resolve_value(conn, args[0], b, entities)
            y = _resolve_value(conn, args[1], b, entities, literal=True)
            if x is None or y is None:
                continue
            if op(x, y):
                result.append(b)
        return result

    pred = a["pred"]
    # Class(?var) / X(?var)
    if len(a["args"]) == 1 and is_var(a["args"][0]):
        var = a["args"][0]
        for b in bindings:
            val = b.get(var, None)
            if val is not None:
                if (val.get("entity_type") if isinstance(val, dict) else None) == pred:
                    result.append(b)
            else:
                for e in entities:
                    if e["entity_type"] == pred:
                        nb = dict(b)
                        nb[var] = e
                        result.append(nb)
        return result

    if len(a["args"]) == 2:
        v1, v2 = a["args"][0], a["args"][1]
        kind = _pred_kind(conn, pred)
        if kind == "relation":
            for b in bindings:
                v1_cur = b.get(v1, None) if is_var(v1) else None
                v2_cur = b.get(v2, None) if is_var(v2) else None
                for rel in _relations(conn, pred):
                    src, tgt = rel["source_id"], rel["target_id"]
                    src_ok = (not is_var(v1)) or (v1_cur is None) or (
                        (v1_cur.get("id") if isinstance(v1_cur, dict) else str(v1_cur)) == str(src))
                    tgt_ok = (not is_var(v2)) or (v2_cur is None) or (
                        (v2_cur.get("id") if isinstance(v2_cur, dict) else str(v2_cur)) == str(tgt))
                    if not (src_ok and tgt_ok):
                        continue
                    nb = dict(b)
                    if is_var(v1):
                        nb[v1] = _entity_by_id(conn, src, entities)
                    if is_var(v2):
                        nb[v2] = _entity_by_id(conn, tgt, entities)
                    result.append(nb)
            return result
        # 数据属性
        for b in bindings:
            v1_cur = b.get(v1, None) if is_var(v1) else None
            for e in entities:
                for k, val in (e.get("_props") or {}).items():
                    if k == pred:
                        if is_var(v1) and v1_cur is not None:
                            if (v1_cur.get("id") if isinstance(v1_cur, dict) else str(v1_cur)) != str(e["id"]):
                                continue
                        nb = dict(b)
                        if is_var(v1):
                            nb[v1] = e
                        if is_var(v2):
                            nb[v2] = val
                            result.append(nb)
                        else:
                            if str(val) == strip_literal(v2):
                                result.append(nb)
        return result
    return result


def _apply_head(conn, rule_id, h, b):
    pred = h["pred"]
    args = h["args"]
    if not args:
        return None
    kind = _pred_kind(conn, pred)
    if kind == "relation":
        if len(args) < 2:
            return None
        subj = b.get(args[0], args[0]) if is_var(args[0]) else args[0]
        obj = b.get(args[1], args[1]) if is_var(args[1]) else args[1]
        if subj is None or obj is None:
            return None
        subj_id = subj["id"] if isinstance(subj, dict) else subj
        obj_id = obj["id"] if isinstance(obj, dict) else obj
        return {"rule_id": rule_id, "fact_type": "ObjectPropertyAssertion",
                "subject": subj_id, "predicate": pred, "object": obj_id, "confidence": 1.0}
    # data 属性
    subj = None
    for arg in args:
        if is_var(arg) and arg in b:
            subj = b[arg]
            break
    if subj is None:
        return None
    subj_id = subj["id"] if isinstance(subj, dict) else subj
    obj = None
    if len(args) >= 2:
        obj = b.get(args[1], args[1]) if is_var(args[1]) else strip_literal(args[1])
    return {"rule_id": rule_id, "fact_type": "DataPropertyAssertion",
            "subject": subj_id, "predicate": pred,
            "object": str(obj) if obj is not None else "", "confidence": 1.0}


def materialize(conn: sqlite3.Connection, rules: list) -> list:
    out: list = []
    for rule in rules:
        rule_id = rule["id"]
        bodies = split_atoms(rule.get("body") or "")
        heads = split_atoms(rule.get("head") or "")
        if not bodies or not heads:
            continue
        bindings = [{}]
        ok = True
        for b in bodies:
            a = parse_atom(b)
            if not a:
                ok = False
                break
            bindings = _apply_atom(conn, a, bindings)
            if not bindings:
                ok = False
                break
        if not ok:
            continue
        for b in bindings:
            for head in heads:
                h = parse_atom(head)
                if not h or not h["args"]:
                    continue
                fact = _apply_head(conn, rule_id, h, b)
                if fact:
                    out.append(fact)
    return out


def execute_rules_sql(conn: sqlite3.Connection, rules: list) -> list:
    return materialize(conn, rules)