# -*- coding: utf-8 -*-
"""`sysml_v2_autofix` —— SysML v2 **规则级**确定性修复。

⚠️ 与 `sysml_v2_validate` 的关系（两者约束不同，别混）
------------------------------------------------------
`sysml_check_tools` 的 ① 号约束是「**只判错，不修复**」，理由是：
  「自动改写、尤其"改到校验通过为止"会**掩盖真实建模缺陷**（把类型族错误改成能编译的形状，
    模型语义就丢了）」。
**本模块不违反该约束**，因为它只做**语义等价**的语法改写：

  | 类别 | 行为 |
  |---|---|
  | 纯语法等价（`connector...from...to` → `connect A to B`） | ✅ 自动改写 |
  | 关键字等价（`refines` → `:>`、`traces` → `dependency from..to`） | ✅ 自动改写 |
  | 可见性前缀补全（`import X` → `private import X`） | ✅ 自动改写 |
  | 涉及建模意图的（类型族、`satisfy` 引用谁、`subject` 与 `satisfy` 二选一、`first...if`缺条件） | ❌ **绝不改写**，只报告 |

**绝不把错误"修成能编译的形状"** —— 那正是 ① 号约束要防的事。
本模块只消除「写法与v2 语法不符」这一类**纯表面错误**。

自校验机制（不能只信「替换成功」）
------------------------------------
每条规则改写后，**必须重跑 checker.jar 确认没有引入新错**：
  · `n_hard` 未增加 → 改写可接受
  · `n_hard` 增加 → **回滚该条改写**，标为「本规则在当前上下文有害」
⇒ 这防的是「把错误修成另一类错误」（实测踩过：缩进沿用导致 `evaluate;` + `if` 粘连成 `evaluateif`）。

工具契约
--------
`side_effect=write`（改写并返回新代码，但**不落盘**）、`risk_level=low`。
输出面向 LLM：逐条列出改了什么、哪些没改及原因，并给出修复后应再调validate 的提示。
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

MAX_FIXES_DEFAULT = 20

def _fix_import_visibility(code: str):
    """R01：给 import 补可见性前缀。

    为什么不用正则（实测踩坑记录）
    ------------------------------
    要判断"是否已有 private/public 前缀"，用 `(?<!...)\\s+(?!private|public)` 这类
    否定前瞻组合**必然出错**：前瞻会被 `\\s*` 消耗而失效，
    导致 `private import X::*;` 被再插一次前缀（实测产出 `private private import`）。
    试过「可选前缀作优先分支」，同样失败（回溯把前缀当成了包名）。

    ⇒ 结论：**能用 `in`/切片判断清楚的逻辑，不要塞进正则。**
    这里按分号切分逐语句处理，逻辑直白可验。

    ⚠️ 第二版踩坑：用 `rfind("import")` 找关键字会命中**前缀里的 import**
    （`private import` 中的 import），导致判断失效 ⇒必须用 `find`（首个）或不查找、
       直接用「语句去掉缩进后是否以 private/public 开头」来判断。
    """
    out, n = [], 0
    # 按分号切分但保留分号（末尾无分号的片段也会保留原文）
    parts = code.split(";")
    for k, raw in enumerate(parts):
        tail = ";" if k < len(parts) - 1 else ""
        if "import" not in raw:
            out.append(raw + tail)
            continue
        # 逐行找 import 语句（一个片段可能含多行）
        lines = raw.split("\n")
        changed = False
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s.startswith("import"):
                continue
            #已有可见性前缀？
            if s.startswith("private ") or s.startswith("public ") \
                    or s.startswith("private\t") or s.startswith("public\t"):
                continue
            rest = s[len("import"):]
            if "::*" not in rest:
                continue                      # 不是 import ...::* 形态
            lines[i] = ln.replace("import", "private import", 1)
            changed = True
            n += 1
        out.append("\n".join(lines) + tail)
    return "".join(out), n


# ── 规则表 ────────────────────────────────────────────────────────
# (规则ID, 说明, 正则, 替换, 是否需要上下文保护)
# ⚠️ 替换文本**必须自带换行与缩进** —— 沿用原缩进会与上一行粘连（实测踩过）。
RULES = [
    # ⚠️ R01（import 可见性前缀）**不在此表**：它需要判断"是否已有前缀"，
    #    用正则必然出错（见 _fix_import_visibility 的踩坑记录）故走独立函数处理。

    ("R02_connector_to_connect", "connector...from...to → connect A to B",
     re.compile(r"^(\s*)connector\s+\w+\s+from\s+(.+?)\s+to\s+(.+?)\s*;\s*$", re.M),
     r"\1connect \2 to \3;",
     "v2 无 connector 记法，connect 是等价写法"),

    ("R03_satisfaction_prefix", "satisfaction satisfy R by V → satisfy requirement r : R by V",
     re.compile(r"^(\s*)satisfaction\s+satisfy\s+(\w+)\s+by\s+(.+?)\s*;\s*$", re.M),
     # ⚠️ 命名必须是大驼峰（requirement usage 命名规范）：
     # 早期版本生成 r_sat_ReqXxx 蛇形名，被 sysml_v2_lint 判为违规（实测 9 处）。
     # ⇒ 规则文本本身就要合规，否则 autofix 会持续生产不合规产物。
     r"\1satisfy requirement Sat\2 by \3;",
     "`satisfaction` 是中间表示节点名，误入文本"),

    # R04：refines → :>。**三种形态都要覆盖**（实测踩过：漏了 `def` 形态，
    #      而真实样例写的正是 `requirement def B refines A;` ⇒ 规则完全不匹配）。
    #      ① `requirement def B refines A;`  ② `requirement B refines A;`  ③ 带体 `... {`
    #      统一用 `(?:def\s+)?` 可选匹配，一棵树覆盖前两种；带体单独一条。
    ("R04_refines_to_specializes", "requirement [def] r refines R → r :> R（无体）",
     re.compile(r"^(\s*)requirement\s+(?:def\s+)?(\w+)\s+refines\s+(\w+)\s*;", re.M),
     r"\1requirement def \2 :> \3;",
     "v2 用 :> 表 specializes/refines 关系"),

    ("R04b_refines_to_specializes_body", "requirement [def] r refines R → r :> R（带体）",
     re.compile(r"^(\s*)requirement\s+(?:def\s+)?(\w+)\s+refines\s+(\w+)\s*(\{)", re.M),
     r"\1requirement def \2 :> \3 \4",
     "v2 用 :> 表 specializes/refines 关系"),

    ("R05_traces_to_dependency", "requirement [def] A traces B → dependency from A to B",
     re.compile(r"^(\s*)requirement\s+(?:def\s+)?(\w+)\s+traces\s+(\w+)\s*;", re.M),
     r"\1dependency from \2 to \3;",
     "v2 无 traces 关键字，用 dependency 表达追溯"),

    ("R06_extend_to_include", "extend X : Y → include use case X : Y",
     re.compile(r"^(\s*)extend\s+(\w+)\s*:\s*(\w+)\s*;", re.M),
     r"\1include use case \2 : \3;",
     "v2 无 extend 关键字；用例复用写 include"),
]

# ── 明确不改写的模式：只报告（涉及建模意图）────────────────────
UNFIXABLE = [
    ("U01_subject_and_satisfy", "subject 与 satisfy 同时写",
     re.compile(r"subject\s*=",re.I),
     "二者对同一需求只能二选一，同时写会报 Cannot override a binding feature value。"
     "**改哪个、还是改需求结构，是建模决策**，不由本工具决定。"),
    ("U02_satisfy_def", "satisfy 疑似引用 definition 而非 usage",
     re.compile(r"^\s*sat Satisfy\s+\w+\s+by\s", re.M),
     "satisfy 必须引用 usage。补usage 声明会改变模型结构（新增元素），"
     "属建模决策 —— 本工具只报告。"),
    ("U03_first_no_condition", "first...else 但缺 if 条件",
     re.compile(r"^\s*first\s+\w+\s+else\s+\w+\s*;", re.M),
     "原文未给判定条件。补条件等于**新增建模语义**，本工具不猜测。"),
    ("U04_type_family", "类型族可能不匹配（part/port/item 混用）",
     re.compile(r"\b(item|port|part)\s+\w+\s*:\s*\w+\s*;", re.I),
     "类型族错误若「改形状」就会丢掉建模语义，正是 ① 号约束要防的。"),
]


def _as_int(v, d, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return d


def apply_rules(code: str, rule_ids=None, max_fixes: int = MAX_FIXES_DEFAULT):
    """应用规则；每条改写后**重跑校验**确认未引入新错，有害的回滚。

    回滚判据（两条都要满足才算"可接受"）
    --------------------------------------
    ① `n_hard` 未增加  —— 不能把语法错改成另一类语法错
    ② `n_error` 未增加 —— 也不能把"可修的错"改成"同样多但不同"的错

    ⚠️ 实测踩过：只查 ① 时出现过「改写确实生效但产物仍不合法」的情况
       （`ReqA ;` —— 分号前多一个空格，checker 不认，n_hard 与 n_error 都不变，
       工具会"成功"返回一个没修好的结果）。加 ② 也不能完全覆盖，
       故另加**修复有效性检查**：见下面 `effective` 标记。

    返回 (fixed_code, applied, rolled_back, baseline, after)
    """
    import sysml_v2_check as svc

    base = svc.check_code(code)
    base_hard = base.get("n_hard", 0) or 0
    base_err = base.get("n_error", 0) or 0
    cur = code
    applied, rolled = [], []

    rules = [r for r in RULES if (not rule_ids or r[0] in rule_ids)]
    prev_hard, prev_err = base_hard, base_err     # 本条规则**改写前**的数值

    # ── R01 走独立函数（不能用正则判断"是否已有前缀"）────────────
    if not rule_ids or "R01_import_visibility" in rule_ids:
        new, n = _fix_import_visibility(code)
        if n:
            chk = svc.check_code(new)
            nh, ne = chk.get("n_hard", 0) or 0, chk.get("n_error", 0) or 0
            if nh > base_hard or ne > base_err:
                rolled.append({
                    "rule_id": "R01_import_visibility",
                    "desc": "import 缺可见性前缀", "hits": n,
                    "reason": f"改写后错误上升（硬错 {base_hard}→{nh} 总错 {base_err}→{ne}），已回滚",
                })
            else:
                cur = new
                prev_hard, prev_err = nh, ne
                applied.append({
                    "rule_id": "R01_import_visibility",
                    "desc": "import 缺可见性前缀", "hits": n,
                    "after_n_hard": nh, "after_n_error": ne,
                    "effective": (ne < base_err) or (nh < base_hard),
                    "why_safe": "成员可见性前缀必填；补 private 不改变引用语义",
                })
                base_hard, base_err = nh, ne

    for rid, desc, pat, rep, why in rules:
        if len(applied) >= max_fixes:
            break
        new, n = pat.subn(rep, cur)
        if not n:
            continue
        chk = svc.check_code(new)
        new_hard = chk.get("n_hard", 0) or 0
        new_err = chk.get("n_error", 0) or 0
        # ① 硬错不得上升
        if new_hard > prev_hard:
            rolled.append({
                "rule_id": rid, "desc": desc, "hits": n,
                "reason": f"改写后硬错数上升（{prev_hard} → {new_hard}），"
                          f"本规则在当前上下文有害，已回滚",
            })
            continue
        # ② 总错不得上升
        if new_err > prev_err:
            rolled.append({
                "rule_id": rid, "desc": desc, "hits": n,
                "reason": f"改写后总错数上升（{prev_err} → {new_err}），已回滚",
            })
            continue
        cur = new
        applied.append({
            "rule_id": rid, "desc": desc, "hits": n,
            "after_n_hard": new_hard, "after_n_error": new_err,
            # ③ 有效性：**与本条规则改写前**比较（不是与最初 baseline 比——
            #    实测踩过：误用全局 baseline 导致「3→0 修好了」被判成无效）
            "effective": (new_err < prev_err) or (new_hard < prev_hard),
            "why_safe": why,
        })
        prev_hard, prev_err = new_hard, new_err
    return cur, applied, rolled, base, svc.check_code(cur)


def _render(code, applied, rolled, base, after, unfixable):
    L = ["【SysML v2 规则级自动修复】只做语义等价的语法改写，不改建模语义。"]
    L.append("")
    L.append(f"修复前：verdict={base.get('verdict')} "
             f"硬错={base.get('n_hard')} 总错={base.get('n_error')}")
    if applied:
        L.append("")
        L.append(f"已应用 {len(applied)} 类规则：")
        for a in applied:
            flag = "" if a.get("effective", True) else "  ⚠️ **本条实际未减少错误，请人工复核**"
            L.append(f"  · {a['rule_id']} {a['desc']} ×{a['hits']}"
                     f"（修后 硬错={a['after_n_hard']} 总错={a['after_n_error']}）{flag}")
    else:
        L.append("")
        L.append("未应用任何自动修复（无可匹配的确定性语法错误）。")
    if rolled:
        L.append("")
        L.append("已回滚（改写反而引入新错，规则在当前上下文有害）：")
        for r in rolled:
            L.append(f"  ✗ {r['rule_id']} {r['desc']} ×{r['hits']} —— {r['reason']}")
    if unfixable:
        L.append("")
        L.append("以下问题**需建模判断，本工具不自动改写**（改动会改变模型语义）：")
        for u in unfixable:
            L.append(f"  ⚠ {u[0]} {u[1]}")
            L.append(f"     {u[3]}")
    L.append("")
    if after.get("verdict") == "pass":
        L.append("修复后校验：**pass**。仍建议调用 sysml_v2_validate 复核确认。")
    else:
        L.append(f"修复后校验：verdict={after.get('verdict')} "
                 f"硬错={after.get('n_hard')} 总错={after.get('n_error')} —— 仍有需处理项。")
        L.append("请调用 sysml_v2_validate 获取按位置聚合的诊断后继续修复。")
    L.append("")
    L.append("⚠️ 本工具**不改写文件**；请自行把 fixed_code 写回你的产物。")
    L.append("⚠️ 修完必须**再次调用 sysml_v2_validate**，不要在未复核的情况下声称已修复。")
    return "\n".join(L)


def _autofix(args):
    try:
        import sysml_v2_check as svc
    except Exception as exc:                                        # noqa: BLE001
        return {"ok": False,
                "result": f"模块不可用：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表代码合法 —— 请如实说明『未能自动修复』。"}
    code = args.get("code")
    if not code or not str(code).strip():
        return {"ok": False,
                "result": "参数 code 为空：请把要修复的 SysML v2 源码整段作为 code 传入。"}

    max_fixes = _as_int(args.get("max_fixes"), MAX_FIXES_DEFAULT, 1, 100)
    rule_ids = args.get("rules")
    if isinstance(rule_ids, str):
        rule_ids = [x.strip() for x in rule_ids.split(",") if x.strip()]

    fixed, applied, rolled, base, after = apply_rules(
        str(code), rule_ids, max_fixes)

    unfixable = []
    for uid, desc, pat, why in UNFIXABLE:
        if pat.search(fixed):
            unfixable.append((uid, desc, pat, why))

    out = {
        "ok": True,
        "result": _render(str(code), applied, rolled, base, after, unfixable),
        "fixed_code": fixed,
        "changed": fixed != str(code),
        "n_applied": len(applied),
        "n_rolled_back": len(rolled),
        "applied_rules": [a["rule_id"] for a in applied],
        "applied_detail": applied,
        "ineffective_rules": [a["rule_id"] for a in applied if not a.get("effective", True)],
        "rolled_back_rules": [r["rule_id"] for r in rolled],
        "unfixable": [{"id": u[0], "desc": u[1], "reason": u[3]} for u in unfixable],
        "before": {"verdict": base.get("verdict"),
                   "n_hard": base.get("n_hard"),
                   "n_error": base.get("n_error")},
        "after": {"verdict": after.get("verdict"),
                  "n_hard": after.get("n_hard"),
                  "n_error": after.get("n_error")},
    }
    return out


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    """工具执行入口（`tools.py` 按 `sysml_v2_` 前缀路由）。"""
    args = arguments or {}
    if name == "sysml_v2_autofix":
        return _autofix(args)
    if name == "sysml_v2_validate":                # 复用既有实现，避免两处维护
        import sysml_check_tools as ct
        return ct.exec_tool(name, args)
    return {"ok": False, "result": f"未知 SysML 工具: {name}"}


if __name__ == "__main__":
    import sys
    import json
    demo = """package P {
  import ScalarValues::*;
  part def Vehicle;
  part def Engine;
  part vehicle1 { part engine; }
  connector c1 from vehicle1::engine to vehicle1;
}"""
    r = _autofix({"code": demo})
    print(r["result"])
    print("\n--- fixed_code ---")
    print(r["fixed_code"])
