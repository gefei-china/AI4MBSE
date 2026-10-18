# CI-OPTIONAL: A 需 checker.jar 三件套（java/jar/library 随包运行时、不入库）——2026-10-18 实测：开发机三件套齐备 rc=0（56s），但 `real_checker_checks()` 在 `C.available()` 为假时返回 FAIL 而非 SKIP ⇒ CI 的 ubuntu runner 上接进来就是常红；开发机手动跑
"""verify_checker_three_paths — 判据：**三路计数（词法/语法/语义）归类正确**。

## 为什么要有这道门禁（脆弱点实测）
`sysml_v2_check.classify()` 把 checker 的诊断分成三路：

| 路 | 判据 | 是否硬错 |
|---|---|---|
| `lexical` | 匹配 `no viable alternative at character` / `token recognition error` | ✅ |
| `syntax` | 匹配 `no viable alternative` / `mismatched input` / `extraneous input` / `missing` | ✅ |
| `semantic` | 其余 | ❌（不触发 `block`） |

⇒ **它靠匹配 Xtext 的英文错误文案**。Xtext 版本一改文案格式，
   归类就会错 ⇒ `n_syntax` 数错 ⇒ **门禁 `block`/`report` 误判**
⇒ 而 `block` 直接决定模型要不要继续修，**误判代价很大**。

实测（2026-10-10）：现有 4 个门禁里**没有一道真覆盖 classify**
（只 import 提及）⇒ 这条路径**裸奔**。

## 判据
① 符号表匹配的是 **Xtext 真实文案**（用真实 checker 产出，不用构造文案）
② **8 段真实代码**归类符合预期（含 autofix 的 3 条旧记法规则）
③ 归类顺序**词法优先**（`lexical` 与 `syntax` 文案有重叠时必须归 lexical）
④ `n_hard = n_lexical + n_syntax` 这条不变式成立
⑤ 变异自证：破坏符号表 ⇒ 判据变红

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_checker_three_paths.py
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: (用例名, 代码, 是否期望硬错)
#: ⚠️ 期望值按「Xtext 是否认为写法合法」定，**不是**按我们的偏好 ——
#: 这正是要守住的判据：checker 说合法就是合法，我们不能自己加严。
CASES = [
    ("正确代码",      "package P { part def A; }",                          False),
    ("合法 import",   "package P { private import X::*; part def Y; }",     False),
    ("中文标识符",    "package P { part def 零件; }",                       True),
    ("if 混进模型",   "package P { part def A { if x == 1 { action 'a'; } } }", True),
    ("refines 旧记法", "package P { requirement def r refines R; }",        True),
    ("traces 旧记法",  "package P { requirement A traces B; }",             True),
    ("extend 旧记法",  "package P { extend X : Y; }",                      True),
    ("缺右花括号",     "package P { part def A;",                          True),
]


def real_checker_checks() -> list[tuple[str, bool]]:
    """★ 必须用**真实 checker** 的产出，不能用我构造的文案 ——
    构造文案只能证明"正则对"，证明不了"与 Xtext 实际输出一致"。"""
    import sysml_v2_check as C
    out = []
    if not C.available():
        return [("checker 三件套齐备", False)]
    out.append(("checker 三件套齐备（java/jar/library）", True))

    for name, code, want_hard in CASES:
        r = C.check_code(code)
        got = (r.get("n_hard") or 0) > 0
        out.append((f"{name:14} n_hard={r.get('n_hard')} "
                    f"lex={r.get('n_lexical')} syn={r.get('n_syntax')} "
                    f"sem={r.get('n_semantic')} ⇒硬错={got}（期望 {want_hard}）",
                    got == want_hard))
    return out


def invariant_checks() -> list[tuple[str, bool]]:
    import sysml_v2_check as C
    out = []
    # ④ 不变式：n_hard == n_lexical + n_syntax
    ok_inv = True
    detail = []
    for name, code, _ in CASES:
        r = C.check_code(code)
        lhs = r.get("n_hard")
        rhs = (r.get("n_lexical") or 0) + (r.get("n_syntax") or 0)
        detail.append(f"{name}:{lhs}=={rhs}")
        if lhs != rhs:
            ok_inv = False
    out.append((f"不变式 n_hard = n_lexical + n_syntax（{'; '.join(detail[:4])}…）", ok_inv))

    # ③ 顺序敏感：词法优先（文案同时含两种特征时必须归 lexical）
    both = "no viable alternative at character 'x'"
    got = C.classify(both)
    out.append((f"词法优先（文案含 lexical+syntax 特征 ⇒归 lexical，实际 {got}）",
                got == "lexical"))

    # 未知文案应落 semantic（保守：不误判成硬错）
    unknown = "some brand new xtext wording nobody has seen"
    got_u = C.classify(unknown)
    out.append((f"未知文案落 semantic（实际 {got_u}）—— 不会误判成硬错",
                got_u == "semantic"))

    # 符号表非空（防止被清空导致全部落 semantic ⇒ 门禁失效）
    lex, syn = C._signs()
    out.append(("符号表非空（lex/syn 均有 pattern）",
                bool(getattr(lex, "pattern", "")) and bool(getattr(syn, "pattern", ""))))
    return out


def main() -> int:
    print("=" * 78)
    print("判据 · checker 三路计数归类（脆弱点：靠 Xtext 错误文案匹配）")
    print("=" * 78)
    ok = True
    for title, checks in [("① 真实 checker 产出回归", real_checker_checks()),
                          ("② 归类不变式与边界", invariant_checks())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 78)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 78)
    if ok:
        print("\n[变异自证] 破坏词法符号表（清空），判据应变红")
        src_path = os.path.join(_ROOT, "sysml_v2_check.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        # ⚠️ 定义形态是 `_LEX_DEFAULT = r"..."`（**无括号**）⇒ 首版正则
        #   `_LEX_DEFAULT\s*=\s*"(.*?)(")` 匹配不到 ⇒ 变异没做就报"锚点未找到"。
        #   ⇒ 改为按**行**定位（r 前缀用两段可选匹配）。
        m = re.search(r'^(_LEX_DEFAULT\s*=\s*r?")(.*?)(["\']\s*$)',
                      src, re.M)
        assert m, "未找到 _LEX_DEFAULT 定义（形态可能又变了，请人工检查）"
        mutated = src[:m.start(2)] + "" + src[m.end(2):]
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            import importlib

            import sysml_v2_check as C
            importlib.reload(C)
            red = not all(p for _, p in invariant_checks())
            print(f"[变异自证] {'已变红（真判据）' if red else '**仍是绿 ⇒ 空转断言**'}")
            ok = ok and red
        finally:
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(src)
            import importlib

            import sysml_v2_check as C
            importlib.reload(C)
        with open(src_path, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
        print("=" * 78)
        print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())