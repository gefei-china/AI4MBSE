"""verify_view_content — 视图**内容层**判据（checker.jar 管不到的部分）。

## 为什么需要它
`checker.jar` 校验 **SysML 语法与语义**（能不能编译、引用能不能解析），
**完全不检查「这个视图该有哪些要素」**。实测证据：2026-10-08 活动视图产出
`verdict=pass`（0 错）但只有 `action def` + `in/out item`，没有 part、没有分支。

⇒ **pass ≠ 合格**。本模块提供内容层断言，与语法层并列，两者都过才算通过。

## 判据设计原则
① 只断言**该视图公认的必备要素**，不臆造业务要求
   （如「异常分支」按米爸 2026-10-08 口径**已降为按需可选**，不作为判据）
② 每条判据必须能在产出文本上**确定性判定**（正则/关键字），不靠主观判断
③ 区分「硬性必备」与「按需可选」，后者只提示不判FAIL
"""
from __future__ import annotations

import re

# ══════════════════════════════════════════════════════════════════
# 各视图的内容层判据
#   must: 必备要素（缺 → FAIL）
#   warn: 按需/建议（缺 → WARN，不阻断）
#   forbidden: 不该出现的构造（出现 → FAIL）
# ══════════════════════════════════════════════════════════════════
VIEW_RULES = {
    "requirement": {
        "must": [
            (r"\brequirement\b", "有 requirement 定义"),
            (r"\bdoc\b|\btext\b", "需求有说明文本（doc/text）"),
            # ★ 2026-10-09 修正：原正则 `subject\s*=` **只认等号形态**，
            #   而 SysML v2 的真实产出是 **`subject 'X' : Type;`**（单引号 + 类型，无等号）。
            #   实测后果：N3 稳定性 3 轮里 requirement/structure/usecase/activity
            #   **11/24 次假失败**——代码里明明写着 `subject '热管理系统' : Part;`，
            #   判据却报「缺必备要素」。
            #   （两种形态都用 checker.jar 实测过：n_hard 均为 0，即都被接受；
            #     但带引号+类型才是真实产出形态。）
            (r"\b(subject\s*(=|['\"])|satisfy\b)",
             "需求有主体或满足关系（subject/satisfy）"),
        ],
        "warn": [
            (r"\bstakeholder\b", "建议声明 stakeholder（涉众）"),
            (r"\brefine|:>", "有细化关系（refine/:>）"),
        ],
        "forbidden": [
            # ★ 两种出现顺序都要判（2026-10-08 实测：只写"satisfy...subject"单向正则，
            #   subject 写在前面时**漏判**，而这正是真实产出的写法）
            # 同样放宽：`subject` 后接等号或引号都算出现
            (r"\bsatisfy\b[\s\S]{0,300}?\bsubject\s*(=|['\"])",
             "subject 与 satisfy 同时出现（语义冲突，须二选一）"),
            (r"\bsubject\s*(=|['\"])[\s\S]{0,300}?\bsatisfy\b",
             "subject 与 satisfy 同时出现（语义冲突，须二选一）"),
        ],
        "note": "subject 与 satisfy **二选一**（已含在 forbidden 里）",
    },
    "structure": {
        "must": [
            (r"\bpart\s+def\b", "有 part def（类型定义）"),
        ],
        "warn": [
            (r"\bpart\s+(?!def\b)\w+", "有 part usage（实例）"),
            (r"\bport\s+def\b", "有 port def（端口定义）"),
        ],
        "forbidden": [
            (r"connector\s+\w+\s+from", "connector...from...to 是 v1 旧记法，应用 connect"),
            (r"\bextend\b", "extend 是 v1 关键字（应用 include use case 或 :>）"),
        ],
        "note": "命名规范由 sysml_v2_lint 负责（def 大驼峰 / usage 小驼峰）",
    },
    "usecase": {
        "must": [
            (r"\buse\s+case\s+def\b", "有 use case def（用例定义）"),
        ],
        "warn": [
            (r"\bactor\b|\bstakeholder\b", "有涉众/参与者声明"),
            (r"\binclude\b", "有 include（复用用例）"),
        ],
        "forbidden": [
            (r"\bextend\s+\w+\s*:", "extend 是 v1 关键字，应用 include use case"),
        ],
        "note": "SysML v2 参与方须用 part def 表达（不用独立 actor 关键字）",
    },
    "activity": {
        "must": [
            (r"\baction\b", "有 action（动作）"),
        ],
        "warn": [
            #★ 口径修正（米爸 2026-10-08）：异常分支**按需可选**，不作为必备判据
            (r"\bif\b", "有判定分支（if/else）"),
            (r"\bexception|异常|超时|故障", "有异常/超时/故障路径（按业务需要，非必备）"),
        ],
        "forbidden": [
            # ⚠️ 2026-10-08 **移除** `first ... if/then` 这条 forbidden：
            #   原规则 `\bfirst\s+\w+\s+(if|then)` 是**误判**——SysML v2 的
            #   `succession s first A then B;`（顺序流）是**合法语法**，
            #   实测 activity 产出 5 处 succession 全被这条规则判 FAIL，
            #   而官方 checker.jar 对同一份代码判 `verdict=pass / n_hard=0`。
            #   ⇒ 校验器说是合法的，判据就不该判它违规。
            #   **教训：forbidden 规则必须先确认 SysML v2 真的禁用该构造**，
            #   否则内容层判据会盖过官方校验器、且方向相反。
            # 真正的 v1 残留是 `if/then/else` 这种**关键字式分支**，
            # 但它在 v2 里已不是关键字（写成 `if <条件> { } else { }`），
            # 交由 checker.jar 的词法/语法层判定，此处不再重复拦截。
            (r"\|\||&&", "逻辑运算符须用单字符 | 与&"),
        ],
        "note": "**异常分支按需可选**（米爸 2026-10-08 明确：不强制要求）",
    },
    "ibd": {
        "must": [
            (r"\bconnect\b", "有 connect（连接）"),
        ],
        "warn": [
            (r"\bport\b", "有端口定义或使用"),
        ],
        "forbidden": [
            (r"connector\s+\w+\s+from", "connector...from...to 是 v1 旧记法，应用 connect"),
        ],
        "note": "连接端点用点号访问（vehicle1.engine）",
    },
    "sequence": {
        "must": [
            (r"\bmessage\b|\bmsg\b", "有消息（message）"),
        ],
        "warn": [
            (r"\b(lifeline|生命线)\b", "有生命线（lifeline）"),
        ],
        "forbidden": [],
        "note": "⚠️ AST 元类 MessageUsage 未映射 ⇒ sysml_ast_extract 抽不出消息边；"
                "**抽不出≠模型里没有**，本判据只判文本里有没有消息关键字",
    },
    "state": {
        "must": [
            (r"\bstate\b", "有状态（state）"),
        ],
        "warn": [
            (r"\btransition\b|\btransition\s+\w", "有转换（transition）"),
            (r"\bguard\b|when\s", "转换有守护条件（guard/when）"),
        ],
        "forbidden": [],
        "note": "⚠️ TransitionUsage 未映射 ⇒ 抽不出转换边；转换须写触发事件与守护条件，"
                "**缺条件应标注，不要凭空补**",
    },
    "parameter": {
        "must": [
            (r"\bconstraint\b", "有约束（constraint）"),
        ],
        "warn": [
            (r"\battribute\b", "有属性定义"),
            (r"\bitem\s+def\b", "有 item def（数据类定义）"),
        ],
        "forbidden": [],
        "note": "⚠️ ParameterUsage 未映射 ⇒ 抽不出参数化边",
    },
}


def check_view(view_type: str, code: str) -> dict:
    """对一个视图产出做内容层检查。返回 {level, must_hit, must_miss, warn_miss, forbidden_hit}"""
    rules = VIEW_RULES.get(view_type)
    if not rules:
        return {"level": "SKIP", "reason": f"未定义判据：{view_type}"}
    text = code or ""

    must_hit, must_miss = [], []
    for pat, why in rules["must"]:
        if re.search(pat, text):
            must_hit.append(why)
        else:
            must_miss.append(why)

    warn_miss = [why for pat, why in rules["warn"] if not re.search(pat, text)]

    forbidden_hit = []
    for pat, why in rules["forbidden"]:
        m = re.search(pat, text)
        if m:
            forbidden_hit.append(f"{why}（命中：{m.group(0)[:40]}）")

    if forbidden_hit or must_miss:
        level = "FAIL"
    elif warn_miss:
        level = "WARN"
    else:
        level = "PASS"
    return {"level": level, "view": view_type,
            "must_hit": must_hit, "must_miss": must_miss,
            "warn_miss": warn_miss, "forbidden_hit": forbidden_hit,
            "note": rules["note"]}


def render(result: dict) -> str:
    L = [f"【内容层判据】视图 = {result.get('view')}｜结论 = {result['level']}"]
    if result["level"] == "SKIP":
        return "\n".join(L + [f"  原因：{result['reason']}"])
    if result["must_hit"]:
        L.append("  必备要素已具备：")
        for x in result["must_hit"]:
            L.append(f"    ✓ {x}")
    if result["must_miss"]:
        L.append("  ✗ 必备要素缺失：")
        for x in result["must_miss"]:
            L.append(f"      · {x}")
    if result["forbidden_hit"]:
        L.append("  ✗ 出现禁用构造：")
        for x in result["forbidden_hit"]:
            L.append(f"      · {x}")
    if result["warn_miss"]:
        L.append(f"  ○ 按需项未出现（不阻断）：{'；'.join(result['warn_miss'])}")
    if result.get("note"):
        L.append(f"  ℹ {result['note']}")
    return "\n".join(L)


def _selftest():
    print("=" * 72)
    print("视图内容层判据 自测")
    print("=" * 72)
    cases = [
        ("structure", "package P { part def Vehicle; part v1 { port def p; } }", "PASS"),
        ("structure", "package P { part Vehicle; }", "FAIL"),          # 缺 part def
        ("structure", "package P { part def V; connector c from a to b; }", "FAIL"),  # 禁用构造
        ("activity", "package P { part def V; part v { action a; } }", "WARN"),  # 无if 分支→按需项缺
        ("activity", "package P { part def V; part v { action a; first x if c then y; } }", "WARN"),
        # ★ 2026-10-08 修正：`succession X first A then B` 是 **SysML v2 合法语法**
        #   （官方 checker.jar 对含此句的 activity 产出判 pass/0 硬错），
        #   原 forbidden 规则把它误判 FAIL ⇒ 现改为只判按需项缺失（WARN）。
        ("activity",
         "package P { part def V; part v { action a; "
         "succession s first a then b; } }",
         "WARN"),
        # subject 与 satisfy **并存**（两种顺序都要判 FAIL）
        #★ 反例教训：只写 `subject = v;`（无 satisfy）是**合法**的（二选一），
        #    第一次自测误把它当违规用例⇒ 期望值写错，掩盖了真实判据的正则问题。
        ("requirement",
         "package P { part def V; part v; requirement def R { doc /* r */ subject = v; "
         "satisfy requirement r1 : R by v; } }",
         "FAIL"),   # subject 在前 + satisfy
        ("requirement",
         "package P { part def V; part v; requirement def R { doc /* r */ subject = v; } }",
         "WARN"),   # subject 单独（无 satisfy）合法，只因缺按需项而 WARN
        ("requirement",
         "package P { part def V; part v; requirement def R { doc /* r */ subject = v; "
         "satisfy requirement r1 : R by v; } }", "FAIL"),           # subject+satisfy 并存
        ("ibd", "package P { part def A; part a; connect a to a; }", "WARN"),  # 缺 port
        ("state", "package P { part def V; part v { state s; } }", "WARN"),  # 缺转换（按需）
        ("sequence", "package P { part def V; part v { message m; } }", "WARN"),  # 缺 lifeline
        ("parameter", "package P { part def V; attribute def a; constraint c; }", "WARN"),  # 缺 item def
        ("usecase", "package P { use case def U { actor a; } }", "WARN"),  # 缺 include
        # subject+satisfy 并存的**另一种顺序**（satisfy 在前）
        ("requirement",
         "package P { part def V; part v; requirement def R { doc /* r */ "
         "satisfy requirement r1 : R by v; subject = v; } }",
         "FAIL"),
    ]
    ok = 0
    for vt, code, want in cases:
        r = check_view(vt, code)
        good = r["level"] == want
        ok += good
        print(f"  [{'OK  ' if good else 'FAIL'}] {vt:12} 期望 {want:5} 实际 {r['level']:5}"
              + ("" if good else f"  ← must_miss={r['must_miss'][:1]} forbid={r['forbidden_hit'][:1]}"))
    print()
    print("  ── 关键：活动图不含异常分支应 PASS（口径：按需可选）──")
    no_exc = "package P { part def V; part v { action a; } }"
    r_ne = check_view("activity", no_exc)
    print(f"     无异常分支 → {r_ne['level']}（期望 PASS/WARN，不判FAIL）")
    assert r_ne["level"] in ("PASS", "WARN"), "活动图不该因缺异常分支判 FAIL"
    ok += 1
    print()
    print("=" * 72)
    print(f"  自测通过 {ok}/{len(cases) + 1}")
    return 0 if ok == len(cases) + 1 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())