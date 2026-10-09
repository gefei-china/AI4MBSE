# -*- coding: utf-8 -*-
"""SysML v2 基准集门禁 —— 双判据，防「门禁形同虚设」。

判据（两类缺一不可）
------------------
① **正例必须全过**（PASS_CASES → verdict=pass）
   作用：证明校验器与门禁链路**能放行**。这类红 ⇒ 校验器/门禁坏了（假阴性）。
② **反例必须全被拦**（FAIL_CASES → verdict=block）
   作用：证明门禁**能拦住**。这类绿 ⇒ 门禁形同虚设，**比红更危险**。

⚠️ 只做①不做②，得到的结论是「门禁能过」—— 但这无法区分
   「门禁正确」与「门禁什么都没检」。必须双向都验。

用法
----
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_sysml_baseline.py
退出码 0 = 基准集与校验器均正常，可作为 CI 门禁。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sysml_baseline_cases import PASS_CASES, FAIL_CASES   # noqa: E402


def run():
    from sysml_v2_check import check_code
    rows = []
    for k, c in PASS_CASES.items():
        r = check_code(c["code"])
        rows.append(("PASS", k, c, r))
    for k, c in FAIL_CASES.items():
        r = check_code(c["code"])
        rows.append(("FAIL", k, c, r))
    return rows


def main() -> int:
    print("=" * 74)
    print("SysML v2 基准集门禁（正例须全过 + 反例须全拦）")
    print("=" * 74)
    rows = run()

    print("\n── ① 正例（PASS_CASES）：必须 verdict=pass ──")
    ok = True
    for kind, k, c, r in rows:
        if kind != "PASS":
            continue
        v = r.get("verdict")
        good = (v == "pass")
        if not good:
            ok = False
        print(f"  [{'OK  ' if good else 'FAIL'}] {k:24} verdict={v:8} "
              f"n_hard={r.get('n_hard')} n_sem={r.get('n_semantic')}")
        if not good:
            print(f"        {c['desc']}")
            for e in (r.get("errors") or [])[:3]:
                print(f"        L{e.get('line')} [{e.get('source')}] {str(e.get('msg'))[:60]}")

    print("\n── ② 反例（FAIL_CASES）：必须被判为非法 ──")
    for kind, k, c, r in rows:
        if kind != "FAIL":
            continue
        v = r.get("verdict")
        # 每条反例自带 expect_verdict：语义错按现行判据是 report（不阻断），
        # 词法/语法错才是 block。**用统一的 block 判据会把正确的语义错用例判成"门禁失效"。**
        want = c.get("expect_verdict", "block")
        good = (v == want)
        if not good:
            ok = False
        n_err = len(r.get("errors") or [])
        print(f"  [{'OK  ' if good else 'FAIL'}] {k:26} verdict={v:8}(期望{want:8}) "
              f"n_hard={r.get('n_hard')} 报错数={n_err}")
        if n_err == 0:
            print("        ⚠️ 该反例竟无任何报错 —— 用例可能写错了（没真正触发错误）")
        if not good:
            print(f"        {c['desc']}")
            for e in (r.get("errors") or [])[:3]:
                print(f"        L{e.get('line')} [{e.get('source')}] {str(e.get('msg'))[:60]}")

    n_pass = sum(1 for r in rows if r[0] == "PASS")
    n_fail = sum(1 for r in rows if r[0] == "FAIL")
    print("\n" + "=" * 74)
    print(f"  基准集规模：正例 {n_pass} 条 / 反例 {n_fail} 条")
    if not ok:
        print("❌ 有用例不满足预期 ⇒ 基准集或校验器有问题，不可作为门禁基准")
    else:
        print("✅ 双向判据均通过")
        print("   ⇒ 校验器既能放行合法代码，也能拦住已知非法代码")
        print("   ⇒ 可作为 N4 校验门禁与 CI 的回归基准")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
