# -*- coding: utf-8 -*-
"""`sysml_v2_autofix` 门禁 —— 双判据。

① **规则覆盖判据**：每条规则必须能匹配它声称能处理的写法。
   实测踩过：R04/R05 漏了 `requirement def X ...`（带 `def`）形态，
   而**真实样例用的正是带def 的写法** ⇒ 规则完全不匹配、autofix 静默无效。
   这种 bug 不会报错、不会异常，只会让工具"看起来在跑但什么也没修"。
   ⇒ 必须有一份「规则 × 探测写法」的矩阵判据。

② **修复有效性判据**：对每个「应被修好」的样例，
   autofix 后 `n_hard`/`n_error` 必须真的下降。
   若不降 ⇒ 规则匹配了但没修好（如产出 `ReqA ;` 分号前多空格）。

③ **无副作用判据**：不涉及建模语义的错误才允许自动改写；
   涉及建模意图的必须只报告不改写（`UNFIXABLE` 覆盖）。

用法
----
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_sysml_autofix.py
退出码 0 = 可作为 CI 门禁。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── 判据①：规则 × 探测写法矩阵（每条规则至少命中 1 行）──────────
# 取自**真实样例与官方错误对照表**的写法，不是凭空编的
COVERAGE_PROBES = [
    ("R01", "  import ScalarValues::*;"),
    ("R01", "  import (ScalarValues::*);"),
    ("R02", "  connector c1 from a.p to b.q;"),
    ("R03", "    satisfaction satisfy ReqA by SubsysB;"),
    ("R04", "  requirement def B refines A;"),
    ("R04", "  requirement B refines A;"),
    ("R04b", "  requirement def B refines A {"),
    ("R05", "  requirement def A traces B;"),
    ("R05", "  requirement A traces B;"),
    ("R06", "  extend Mode : EnergyMode;"),
]

# ── 判据②：应被修好的样例（错误数必须下降）──────────────────────
SHOULD_FIX = [
    ("import 无可见性前缀", """package P {
  import ScalarValues::*;
  part def Vehicle;
}"""),
    ("connector 旧记法", """package P {
  part def V;
  part v1 { part p; }
  connector c1 from v1::p to v1;
}"""),
    ("satisfaction 中间表示残留", """package P {
  part def V;
  part v1;
  requirement def ReqA { doc /* r */ }
  satisfaction satisfy ReqA by v1;
}"""),
    ("requirement def 带 refines", """package P {
  part def V;
  requirement def A { doc /* a */ }
  requirement def B refines A;
}"""),
    ("requirement def 带 traces", """package P {
  part def V;
  part v1;
  requirement def A { doc /* a */ }
  requirement def A traces v1;
}"""),
]

# ── 判据③：不应被自动改写的（涉及建模语义）────────────────────
MUST_NOT_FIX = [
    ("subject 与 satisfy 并存", """package P {
  part def V;
  part v1;
  requirement def ReqA { subject = v1; doc /* r */ }
  satisfy requirement r1 : ReqA by v1;
}"""),
]


def main() -> int:
    import sysml_autofix_tools as af
    from sysml_v2_check import check_code

    print("=" * 74)
    print("sysml_v2_autofix 门禁（规则覆盖 + 修复有效性 + 无副作用）")
    print("=" * 74)
    ok = True

    # ① 规则覆盖
    print("\n── ① 规则覆盖矩阵（每条规则须命中其探测写法）──")
    for rid, probe in COVERAGE_PROBES:
        if rid == "R01":
            # R01 走独立函数（不能用正则判断前缀）
            new, n = af._fix_import_visibility(probe)
            hit = n > 0
            # 反向：已有前缀的绝不能被改
            nofalse = af._fix_import_visibility("  private import X::*;")[1] == 0
            ok_now = hit and nofalse
            if not ok_now:
                ok = False
            print(f"  [{'OK  ' if ok_now else 'FAIL'}] R01   ← {probe.strip()[:40]}"
                  f" 命中={hit} 已有前缀不误改={nofalse}")
            if not nofalse:
                print("         ⇒ 已有 private/public 前缀被重复加前缀（危险）")
            continue
        hit = [r[0] for r in af.RULES if r[0].startswith(rid) and r[2].search(probe)]
        if hit:
            print(f"  [OK  ] {rid:5} ← {probe.strip()[:44]}")
        else:
            ok = False
            print(f"  [FAIL] {rid:5} 未匹配 {probe.strip()[:44]}")
            print(f"         ⇒ 该规则对这种写法**完全无效**（静默失败）")

    # ② 修复有效性
    print("\n── ② 修复有效性（错误数必须真下降）──")
    for name, code in SHOULD_FIX:
        before = check_code(code)
        r = af._autofix({"code": code})
        a_b, a_a = r["before"], r["after"]
        down = (a_a["n_error"] < a_b["n_error"]) or (a_a["n_hard"] < a_b["n_hard"])
        mark = "OK  " if down else "FAIL"
        if not down:
            ok = False
        print(f"  [{mark}] {name:26} 错误 {a_b['n_error']}→{a_a['n_error']} "
              f"硬错 {a_b['n_hard']}→{a_a['n_hard']}  规则={r['applied_rules']}")
        if not down:
            print(f"         ⇒ 规则未生效或产出仍不合法（changed={r['changed']}）")
        if r["ineffective_rules"]:
            ok = False
            print(f"         ⇒ 无效改写: {r['ineffective_rules']}")

    # ③ 无副作用
    print("\n── ③ 无副作用（建模语义类问题只报告不改写）──")
    for name, code in MUST_NOT_FIX:
        before = check_code(code)
        r = af._autofix({"code": code})
        unchanged = (r["fixed_code"] == code)
        mark = "OK  " if unchanged else "FAIL"
        if not unchanged:
            ok = False
        print(f"  [{mark}] {name:26} 产物是否未变={unchanged}  "
              f"报告的 unfixable={[u['id'] for u in r['unfixable']]}")
        if not unchanged:
            print("         ⇒ 工具改写了涉及建模语义的内容 —— 违反 ① 号约束")

    # ④ 已修好的样例不应被二次改坏（幂等/无过度修正）
    print("\n── ④ 过度修正检查：合法代码不该被改写 ──")
    legal = """package P {
  private import ScalarValues::*;
  part def Vehicle;
  part v1 { part p; }
  connect v1.p to v1;
}"""
    r = af._autofix({"code": legal})
    nochange = (r["fixed_code"] == legal)
    mark = "OK  " if nochange else "FAIL"
    if not nochange:
        ok = False
    print(f"  [{mark}] 合法代码未被改写={nochange}（改写则说明规则过宽）")
    if not nochange:
        print("  改写结果:", [l for l in r["fixed_code"].split("\n") if "connect" in l])

    print("\n" + "=" * 74)
    print("✅ 全部门禁通过 ⇒ 可作为 CI 门禁" if ok else "❌ 有 FAIL 项")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
