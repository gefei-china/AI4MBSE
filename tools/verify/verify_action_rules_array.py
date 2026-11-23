# -*- coding: utf-8 -*-
"""动作绑定「条件数组」门禁（2026-11-23）。

## 为什么有这道门禁

绑定表一行 `(type_id, tool_name)` 只能存一份 pre/post 条件，而最常见的前置约束
「**必填且非空**」天然是两条规则：

  · 只配 `required_args`   → 空字符串放行（键在、值非 None ⇒ verified）
  · 只配 `field_not_empty` → **缺键**判 unknown（fail-open ⇒ 放行）

所以"只支持单条规则"等于表达不了它 —— 而路由 `_p01_parse_prepost` 原本只收 dict、
`_as_rule` 也只收 dict，数组会被判"规则非法 ⇒ unknown ⇒ 放行"，
**表现是"配了条件却从不生效"，且不报错**（静默失效，本仓反复踩过的形态）。

本门禁守：
  R1 `validate` 接受数组（逐条带下标报错）
  R2 `evaluate_rules` 数组语义 = **全部须成立**（空串/缺键 ⇒ failed；正常 ⇒ verified）
  R3 空数组 / None ⇒ skipped（无约束，**不得**当 verified）
  R4 路由归一化接受数组，落库形态是 JSON 数组；元素非对象 ⇒ 报错
  R5 `needed_keys` 能从数组收集 post 需要的键（否则回读快照缺键 ⇒ 恒 unknown）
  R6 单条规则各自的漏洞仍在（证明两条必须一起配，不是冗余）

## 变异（必须判红）

  M1 `evaluate_rules` 改成只取第一条   ⇒ R2 红（空串不再被拦）
  M2 路由改回只接受 dict               ⇒ R4 红
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_p, _f = [], []


def ck(cond, msg, detail=""):
    (_p if cond else _f).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg + (("  «%s»" % detail) if detail and not cond else ""))


RULES = [{"kind": "required_args", "args": ["code"]},
         {"kind": "field_not_empty", "arg": "code"}]
POST = [{"kind": "entity_exists", "arg": "id"},
        {"kind": "field_equals", "arg": "id", "field": "entity_type", "value": "需求"}]


def main() -> int:
    print("=" * 78)
    print("动作绑定「条件数组」门禁")
    print("=" * 78)
    from core import action_conditions as ac
    from routers.knowledge_parts.ontology import _p01_parse_prepost

    # R1
    ck(ac.validate(RULES, "pre") == [], "R1 数组条件判定为合法")
    bad = ac.validate([RULES[0], {"kind": "no_such_kind"}], "pre")
    ck(len(bad) == 1 and bad[0].startswith("[1]"), "R1b 非法项带下标报错", str(bad))

    # R2
    cases = (("空串", {"code": ""}, "failed"),
             ("缺键", {}, "failed"),
             ("None", {"code": None}, "failed"),
             ("正常", {"code": "package X {"}, "verified"))
    for label, args, want in cases:
        got = ac.evaluate_rules(RULES, args=args, direction="pre")["status"]
        ck(got == want, "R2 %s ⇒ %s" % (label, want), "got=%s" % got)

    # R3
    ck(ac.evaluate_rules([], direction="pre")["status"] == "skipped", "R3 空数组 ⇒ skipped")
    ck(ac.evaluate_rules(None, direction="pre")["status"] == "skipped", "R3b None ⇒ skipped")

    # R4
    stored = _p01_parse_prepost(RULES)
    ck(stored is not None and json.loads(stored) == RULES, "R4 路由归一化为 JSON 数组", str(stored)[:80])
    ck(_p01_parse_prepost({"kind": "required_args", "args": ["code"]}) is not None,
       "R4b 单对象仍兼容（向后兼容）")
    ck(_p01_parse_prepost(None) is None and _p01_parse_prepost("") is None,
       "R4c 空条件 ⇒ None（无约束）")
    try:
        _p01_parse_prepost([1, 2])
        ok_err = False
    except ValueError:
        ok_err = True
    ck(ok_err, "R4d 元素非对象 ⇒ ValueError（不静默存脏数据）")

    # R5
    ck(ac.needed_keys(POST) == {"id"}, "R5 needed_keys 从数组收集 post 键", str(ac.needed_keys(POST)))
    ck(ac.needed_keys([{"rule": POST[1]}]) == {"id"}, "R5b 兼容 {rule:...} 包装形态")

    # R6 单条各自的漏洞（语义说明，不是缺陷）—— 断言其真实行为，防有人"优化"成只配一条
    only_req = ac.evaluate_rules([RULES[0]], args={"code": ""}, direction="pre")["status"]
    only_ne = ac.evaluate_rules([RULES[1]], args={}, direction="pre")["status"]
    ck(only_req == "verified", "R6 只配 required_args ⇒ 空串放行（故必须配 field_not_empty）", only_req)
    ck(only_ne == "unknown", "R6b 只配 field_not_empty ⇒ 缺键 unknown 放行（故必须配 required_args）", only_ne)

    # ── 变异 ──
    print("\n-- 变异（必须判红） --")
    # M1 只取第一条规则
    def _mut_eval(raw, args=None, snapshot=None, direction=None):
        items = ac._expand_rules(raw)
        if not items:
            return {"ok": True, "status": "skipped", "kind": "", "why": "", "count": 0}
        return ac.evaluate(items[0], args=args, snapshot=snapshot, direction=direction)

    m1 = _mut_eval(RULES, args={"code": ""}, direction="pre")["status"]
    ck(m1 == "verified" and ac.evaluate_rules(RULES, args={"code": ""}, direction="pre")["status"] == "failed",
       "M1 只取第一条 ⇒ R2 空串判据红", "mut=%s" % m1)

    # M2 路由只接受 dict
    def _mut_parse(raw):
        if raw is None or raw == "":
            return None
        if not isinstance(raw, dict):
            raise ValueError("条件必须是 JSON 对象")
        return json.dumps(raw, ensure_ascii=False)

    try:
        _mut_parse(RULES)
        m2_raised = False
    except ValueError:
        m2_raised = True
    ck(m2_raised and _p01_parse_prepost(RULES) is not None,
       "M2 路由只收 dict ⇒ R4 判据红", "mut_raised=%s" % m2_raised)

    print("\n" + "=" * 78)
    print("结论：%s（PASS %d / FAIL %d）" % ("OK" if not _f else "FAIL", len(_p), len(_f)))
    if _f:
        for m in _f:
            print("  ✗ " + m)
    print("=" * 78)
    return 0 if not _f else 1


if __name__ == "__main__":
    sys.exit(main())
