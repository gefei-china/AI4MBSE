# -*- coding: utf-8 -*-
"""P0-1（2026-11-22）对象类型 ↔ Action 绑定的 **pre/post 条件规则表**。

## 为什么单独成文件
方案 §1.4 规定绑定行可带 `pre_condition` / `post_condition`（JSON），
判定必须是**纯函数 + 显式规则表**，不做运行时 AI 推断（本仓纪律：规则判定不得
靠模型临时判断，与单位换算/本体一致性同口径）。

- 与 `core/ontology_rules.py` 的分工：后者是**本体一致性体检**的 issue 目录
  （code→标签/严重度），本文件是**工具调用前后条件**的判定规则表，两者语义不同，
  但共用 `RULE_VERSION` 口径（绑定的 `rule_version` 字段回显本文件版本）。
- 与 `core/graph_write_gate.py` 的分工：后者在**写入发生时**回读核验并返回四态
  （verified/failed/unknown/skipped）；本文件只回答「这条 JSON 规则**是否合法**
  以及**给定参数/行快照时真假」**，是纯函数，不碰 DB、不碰网络。执行侧若要
  用 post 条件，消费本模块的 `evaluate()` 即可，判定口径单一真源。

## 设计约束
1. **标准库依赖**：零循环导入，可被后端/门禁/夹具共同消费（沿用 branch_rules 范式）。
2. **非法 kind fail-safe**：未登记 kind 不猜、不静默当通过 —— `validate()` 直接报
   `unknown_kind`，`evaluate()` 返回 `{"ok": False, "status": "unknown", ...}`。
3. **读不到参数 ≠ 条件不满足**：参数缺失/类型不对归 `unknown`（"无法证实"），
   不是 `failed`（"证实为假"）——与 P0-3 四态同型，避免把"观测不到"误判成"失败"。
4. **no-op 条件**：空串/None/`{}` 视为 `skipped`（无前置/后置约束），**不得**当 verified。
"""
from __future__ import annotations

import json
from typing import Any

#: 规则版本口径：与 core.ontology_rules.RULE_VERSION 同源（绑定行回显）。
#: 单独暴露常量而非 import，避免两模块版本漂移时静默不一致；门禁断言两处相等。
RULE_VERSION = "2026-11-22.1"

# ── 显式规则表 ──────────────────────────────────────────────────────
# kind → (required_keys, direction, 说明)
#   direction: pre / post / both
#   required_keys: 规则对象必须出现的键（缺失即非法，validate 报错）
RULES = {
    "required_args": {
        "direction": "both", "required": ("args",),
        "desc": "调用参数含全部指定键",
    },
    "entity_type_in": {
        "direction": "pre", "required": ("arg", "types"),
        "desc": "参数 arg 指向的实体类型属于 types",
    },
    "field_not_empty": {
        "direction": "pre", "required": ("arg",),
        "desc": "参数 arg 非空（非 None/空串/空容器）",
    },
    "entity_exists": {
        "direction": "post", "required": ("arg",),
        "desc": "回读确认 arg 指向的实体存在",
    },
    "field_equals": {
        "direction": "post", "required": ("arg", "field", "value"),
        "desc": "回读确认 arg 实体的 field 等于 value",
    },
    "relation_exists": {
        "direction": "post", "required": ("source", "target", "type"),
        "desc": "回读确认 source→target 的 type 关系存在",
    },
}

VALID_KINDS = tuple(RULES)
VALID_STATUS = ("verified", "failed", "unknown", "skipped")


def _as_rule(raw: Any) -> tuple[dict | None, str]:
    """把绑定行的 JSON 文本/对象归一为 dict；返回 (rule, error)。"""
    if raw is None or raw == "":
        return None, ""
    if isinstance(raw, dict):
        return raw, ""
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except Exception as ex:
            return None, "JSON 解析失败: %s" % str(ex)[:80]
        if isinstance(v, dict):
            return v, ""
        return None, "规则顶层必须是 JSON 对象，实际 %s" % type(v).__name__
    return None, "规则类型非法: %s" % type(raw).__name__


def validate(raw: Any, direction: str | None = None) -> list[str]:
    """校验规则对象合法性，返回问题清单（空 = 合法）。

    :param direction: 'pre' / 'post' 时额外校验方向匹配（如把 entity_exists 写进
      pre_condition 即报 direction_mismatch）。None 时只校验结构。
    """
    rule, err = _as_rule(raw)
    if err:
        return [err]
    if rule is None:
        return []  # 空条件合法（= 无约束）
    kind = str(rule.get("kind") or "")
    if not kind:
        return ["缺 kind"]
    spec = RULES.get(kind)
    if spec is None:
        return ["unknown_kind: %s（未登记，拒绝执行）" % kind]
    if direction and direction in ("pre", "post") and spec["direction"] not in (direction, "both"):
        return ["direction_mismatch: %s 只能用于 %s，实际用在 %s"
                % (kind, spec["direction"], direction)]
    bad = []
    for k in spec["required"]:
        if k not in rule:
            bad.append("缺字段 %s" % k)
    # args 必须是非空字符串列表
    if kind == "required_args":
        v = rule.get("args")
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            bad.append("args 必须是非空字符串数组")
    # entity_type_in.types 必须是非空字符串列表
    if kind == "entity_type_in":
        v = rule.get("types")
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            bad.append("types 必须是非空字符串数组")
    return bad


def is_valid(raw: Any, direction: str | None = None) -> bool:
    """validate() 的布尔快捷（空条件视为合法）。"""
    return not validate(raw, direction)


def evaluate(raw: Any, args: dict | None = None, snapshot: dict | None = None,
             direction: str | None = None) -> dict:
    """纯函数判定一条规则。

    :param args:     本次工具调用参数（pre 条件用）
    :param snapshot: 工具执行后回读的对象快照（post 条件用），由调用方提供。
                     本函数**不查库**，读不到就传 None ⇒ 返回 unknown 而非 failed。
    :return: {"ok": bool, "status": verified/failed/unknown/skipped, "kind": str, "why": str}

    语义分级（与 P0-3 四态一致）：
      · skipped   — 无约束（空条件）
      · verified  — 证实为真
      · failed    — 证实为假（规则明确不满足）
      · unknown   — 无法证实（参数缺失/快照缺失/非法规则）；**不得当 verified**
    """
    rule, err = _as_rule(raw)
    if err:
        return {"ok": False, "status": "unknown", "kind": "", "why": err}
    if rule is None:
        return {"ok": True, "status": "skipped", "kind": "", "why": "无约束条件"}
    problems = validate(rule, direction)
    if problems:
        return {"ok": False, "status": "unknown",
                "kind": str(rule.get("kind") or ""), "why": "; ".join(problems)}

    kind = rule["kind"]
    args = args or {}
    snapshot = snapshot or {}

    def _verified(why=""):
        return {"ok": True, "status": "verified", "kind": kind, "why": why}

    def _failed(why):
        return {"ok": False, "status": "failed", "kind": kind, "why": why}

    def _unknown(why):
        return {"ok": False, "status": "unknown", "kind": kind, "why": why}

    if kind == "required_args":
        missing = [k for k in rule["args"] if k not in args or args[k] is None]
        return _failed("缺参数: %s" % ",".join(missing)) if missing else _verified()

    if kind == "field_not_empty":
        k = rule["arg"]
        if k not in args:
            return _unknown("参数 %s 未提供，无法判定非空" % k)
        v = args[k]
        empty = v is None or (isinstance(v, (str, list, dict, tuple)) and len(v) == 0)
        return _failed("参数 %s 为空" % k) if empty else _verified()

    if kind == "entity_type_in":
        k = rule["arg"]
        if k not in args:
            return _unknown("参数 %s 未提供，无法判定类型" % k)
        # 参数本体可以是 {'entity_type': ...} 或从 snapshot 取；两种口径都支持，
        # 但**只在有值时判**，读不到就 unknown（不猜）。
        et = snapshot.get("entity_type") or (args[k].get("entity_type")
                                             if isinstance(args[k], dict) else None)
        if not et:
            return _unknown("实体类型不可得，无法判定是否属于 %s" % rule["types"])
        return _verified() if et in rule["types"] else _failed(
            "实体类型 %s 不在允许集 %s" % (et, rule["types"]))

    if kind == "entity_exists":
        k = rule["arg"]
        if k not in snapshot:
            return _unknown("无 %s 的回读快照，无法证实存在" % k)
        return _verified() if snapshot[k] else _failed("回读确认 %s 不存在" % k)

    if kind == "field_equals":
        k, f, want = rule["arg"], rule["field"], rule["value"]
        row = snapshot.get(k)
        if not isinstance(row, dict) or f not in row:
            return _unknown("回读快照缺 %s.%s，无法判定" % (k, f))
        got = row[f]
        return _verified() if got == want else _failed(
            "%s.%s=%r，期望 %r" % (k, f, got, want))

    if kind == "relation_exists":
        s, t, rt = rule["source"], rule["target"], rule["type"]
        rows = snapshot.get("relations")
        if rows is None:
            return _unknown("无关系回读快照，无法证实 %s→%s:%s" % (s, t, rt))
        hit = any((r.get("source") == s and r.get("target") == t and r.get("type") == rt)
                  for r in rows if isinstance(r, dict))
        return _verified() if hit else _failed("回读未找到 %s→%s:%s" % (s, t, rt))

    return _unknown("未登记的判定分支: %s" % kind)


def rule_catalog() -> list:
    """规则目录（供前端/文档/门禁列举；单一真源，避免前端再写一份）。"""
    return [{"kind": k, "direction": v["direction"],
             "required": list(v["required"]), "desc": v["desc"]}
            for k, v in RULES.items()]
