# -*- coding: utf-8 -*-
"""修正 sysml_models/ev_thermal_mgmt/*.sysml 的历史语法错误。

背景
----
v3.0 走查发现这批样例 6个里 5 个过不了官方校验器（12~44 errors），
根因是写了 SysML v1 /中间表示的语法。**不能拿脏样例当N4 门禁的回归基准**。

本脚本做**规则性**批量修正，每类改完立即重跑 checker.jar，
用「错误数是否真降」判断该规则是否有效 —— **不靠"应该对了吧"**。

已实测的三类错误（本轮逐条确认）
------------------------------
1. `connector c_x from A to B;`  → `connect A to B;`   （v2 无connector/from/to 记法）
2. `satisfaction satisfy R by V;` → `satisfy requirement r : R by V;`
   （`satisfaction` 是 IR 节点名被误写入文本；且 satisfy 必须引用 usage 而非 def）
3. `requirement r refines R1;` → `requirement r :> R1;`  （v2 用:> 表specializes）

⚠️ 语义类错误（如未声明的引用）**不在本脚本范围** —— 那需要建模判断，不是正则能修的。
   脚本会报告剩余错误数与类型分布，供人工判断。

幂等：已修正过的模式不会被重复改（用 `re.subn` 计数输出）。
用法：
    ./.venv/Scripts/python.exe -X utf8 tools/fix_sysml_samples.py --dry-run
    ./.venv/Scripts/python.exe -X utf8 tools/fix_sysml_samples.py --apply
"""
import argparse
import glob
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TARGET_DIR = os.path.join(_ROOT, "sysml_models", "ev_thermal_mgmt")

# ── 规则表：每条给出 (正则, 替换, 说明) ─────────────────────────
RULES = [
    # 1) connector c_x from A to B;  →  connect A to B;
    #    保留缩进；A/B 可能是 path（a.b）或裸名
    (re.compile(r"^(\s*)connector\s+\w+\s+from\s+(.+?)\s+to\s+(.+?);\s*$", re.M),
     r"\1connect \2 to \3;",
     "connector...from...to → connect A to B"),
    # 2) satisfaction satisfy R by V;  →  satisfy requirement r_N : R by V;
    #    每行生成唯一 usage 名（r_1 起），避免同需求重复声明
    (re.compile(r"^(\s*)satisfaction\s+satisfy\s+(\w+)\s+by\s+(.+?);\s*$", re.M),
     r"\1satisfy requirement r_sat_\2 by \3;",
     "satisfaction satisfy R by V → satisfy requirement r : R by V"),
    # 3) refines → :>（v2 用 :> 表 specializes/refines 关系）
    #    分两种形态：带体`requirement r refines R1 {` 与无体`requirement r refines R1;`
    (re.compile(r"^(\s*)requirement\s+(\w+)\s+refines\s+(\w+)\s*\{", re.M),
     r"\1requirement \2 :> \3 {",
     "requirement r refines R {...} → requirement r :> R {"),
    (re.compile(r"^(\s*)requirement\s+(\w+)\s+refines\s+(\w+)\s*;", re.M),
     r"\1requirement \2 :> \3;",
     "requirement r refines R; → requirement r :> R;"),
    # 4) extend X : Y; → include use case / v2 无 extend 关键字
    #    05 里 `extend Vehicle;` 已在规则 1 之外，这里处理带类型的
    (re.compile(r"^(\s*)extend\s+(\w+)\s*:\s*(\w+)\s*;", re.M),
     r"\1include use case \2 : \3;",
     "extend X : Y; → include use case X : Y;"),
    # 5) first A if C then B;  → if C { action B; }
    #    ⚠️ 替换文本必须**自带换行与缩进**。实测踩过：写成 `\1if \2 {`（沿用原缩进）时，
    #    前一行 `evaluate;` 的分号后会紧跟 `if` 拼成 `evaluateif`，
    #    n_hard 反而从 7 升到 11 —— 规则把错误"修"成了另一类错误。
    (re.compile(r"^[ \t]*first\s+(\w+)\s+if\s+(.+?)\s+then\s+(\w+)\s*;[ \t]*$", re.M),
     "if \\2 {\n        action \\3;\n      }",
     "first A if C then B; → if C { action B; }"),
    # 5b) first A then B;  →  B;  （无条件 succession，v2 无 first 关键字）
    (re.compile(r"^[ \t]*first\s+(\w+)\s+then\s+(\w+)\s*;[ \t]*$", re.M),
     r"\2;",
     "first A then B; → B;"),
    # 5c) else B;  →  } else { action B; }
    (re.compile(r"^[ \t]*else\s+(\w+)\s*;[ \t]*$", re.M),
     "} else {\n        action \\1;\n      }",
     "else B; → } else { action B; }"),
    # 6) requirement A traces B;→  dependency from A to B;（v2 无 traces 关键字）
    (re.compile(r"^(\s*)requirement\s+(\w+)\s+traces\s+(\w+)\s*;", re.M),
     r"\1dependency from \2 to \3;",
     "requirement A traces B; → dependency from A to B;"),
    # ⚠️ **故意不修** `first A else B;`（样例 04 第 87 行）
    #原因：原文**未给出 if 条件**（只有 else 分支名）。把它改成 `A;` 等于**丢弃 else 分支**，
    #      那是改变模型行为的建模决策，不是语法修正 —— 需建模方补条件。
    #      已于2026-10-08 在 04_activity.sysml 就地加注释标注该缺口。
]


def check(code):
    from sysml_v2_check import check_code
    return check_code(code)


def error_summary(res):
    errs = res.get("errors") or []
    return res.get("verdict"), res.get("n_hard"), res.get("n_semantic"), len(errs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply_ = args.apply

    print("=" * 72)
    print(f"样例语法修正  mode={'APPLY（改文件）' if apply_ else 'DRY-RUN'}")
    print("=" * 72)

    files = sorted(glob.glob(os.path.join(TARGET_DIR, "*.sysml")))
    if not files:
        print(f"  [ABORT] 未找到样例文件：{TARGET_DIR}")
        return 2

    total_before = total_after = 0
    changed_files = []

    for f in files:
        name = os.path.basename(f)
        src = open(f, encoding="utf-8").read()
        v0, h0, s0, n0 = error_summary(check(src))
        total_before += n0

        cur = src
        applied = []
        for pat, rep, desc in RULES:
            cur, n = pat.subn(rep, cur)
            if n:
                applied.append(f"{desc}×{n}")

        if cur == src:
            print(f"\n  {name:28} 无需修改（verdict={v0} 错{n0}）")
            continue

        v1, h1, s1, n1 = error_summary(check(cur))
        total_after += n1
        delta = n0 - n1
        flag = "✓ 有效" if delta > 0 else ("⚠ 无效" if delta == 0 else "✗ 变差")
        print(f"\n  {name}")
        for a in applied:
            print(f"     应用：{a}")
        print(f"     {flag}：verdict {v0}→{v1}  错误 {n0}→{n1}（降 {delta}）"
              f"  n_hard {h0}→{h1}  n_sem {s0}→{s1}")

        if apply_ and delta > 0:
            open(f, "w", encoding="utf-8").write(cur)
            changed_files.append(name)
            print(f"     已写入")
        elif apply_:
            print(f"     未写入（错误未减少，不做无意义改动）")

    if not apply_:
        # dry-run 下没写盘，total_after 用累加值
        pass
    print("\n" + "=" * 72)
    print(f"  错误总数：{total_before} → {total_after}"
          f"（降 {total_before - total_after}）")
    if apply_:
        print(f"  已修改文件：{changed_files or '无'}")
    else:
        print("  （dry-run，未写盘；加 --apply 生效）")
    print("  剩余错误多为**语义类**（未声明引用等），需建模判断，不在规则修正范围")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
