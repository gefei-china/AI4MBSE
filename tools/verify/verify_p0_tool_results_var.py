"""verify_p0_tool_results_var — P0 缺陷修复的**可复现判据 + 变异自证**。

## 缺陷
`agent/pipeline_parts/execute.py` 里三个「跳过执行」分支 append 到
**`_tool_results`（有下划线）**，而本函数声明的是 `tool_results`（无下划线）：

    :415  tool_results = []          ← 声明的是这个
    :428  _tool_results.append(...)  ← 往这个写⇒ NameError
    :438  _tool_results.append(...)
    :450  _tool_results.append(...)

触发后果：整轮崩在收尾前，**零产出 + 零落库**，前端只表现为「没反应」。

## 为什么判据要用 AST 而不是正则
正则匹配 `_tool_results` 会同时命中 `tool_results`（子串），
必须用 AST 区分「读了但从未赋值」与「正常赋值」。
**正则在这里必然误判** —— 2026-10-09 实测踩过。

## 变异自证（必跑）
只做「修后变绿」这种断言是**空转断言**：一个永远返回 True 的检查也能让测试通过。
⇒ `main()` 末尾会把三个变量名**故意改回去**，确认判据**能变红**，
   改回原样后再确认变绿，两次都过才算真判据。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p0_tool_results_var.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
TARGET = os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py")

#: 必须被单独赋值（声明）的名字
CANON = "tool_results"
#: 曾经被误用的名字
BUGGY = "_tool_results"


def find_unassigned_used(src: str, func_name: str = "execute") -> set:
    """返回「函数内被读取、但从未被赋值」的 tool_results 类变量名。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            assigned, used = set(), set()
            for x in ast.walk(node):
                if isinstance(x, ast.Name):
                    if isinstance(x.ctx, ast.Store):
                        assigned.add(x.id)
                    else:
                        used.add(x.id)
            return {
                u for u in used
                if "tool_result" in u
                and u not in assigned
                and u != CANON and BUGGY not in u
            }
    raise AssertionError(f"未找到函数 {func_name}")


def check_buggy_append_count(src: str) -> int:
    """统计 ``_tool_results.append`` 的出现次数（缺陷点计数，期望 0）。"""
    n = 0
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Attribute)
                and node.attr == "append"
                and isinstance(node.value, ast.Name)
                and node.value.id == BUGGY):
            n += 1
    return n


def run() -> bool:
    with open(TARGET, encoding="utf-8") as f:
        src = f.read()

    checks: list[tuple[str, bool]] = []

    # 1. 无「读但未赋值」的 tool_results 类变量
    bad = find_unassigned_used(src)
    checks.append(("execute() 内无『读了但未赋值』的 tool_results 变量", not bad))

    # 2. 缺陷点计数归零
    n = check_buggy_append_count(src)
    checks.append((f"`_tool_results.append` 出现次数为 0（实测 {n}）", n == 0))

    # 3. 正确变量仍在使用（防止"修成全删"这种假绿）
    canonical_used = any(
        isinstance(x, ast.Attribute) and x.attr == "append"
        and isinstance(x.value, ast.Name) and x.value.id == CANON
        for x in ast.walk(ast.parse(src))
    )
    checks.append((f"`{CANON}.append` 仍在使用（防止过度删除导致假绿）", canonical_used))

    # 4. 语法可编译
    try:
        compile(src, TARGET, "exec")
        checks.append(("文件语法可编译", True))
    except SyntaxError as exc:
        checks.append((f"文件语法可编译（SyntaxError: {exc}）", False))

    ok = True
    for name, passed in checks:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 64)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 64)
    return ok


def main() -> int:
    print("=" * 64)
    print("P0 缺陷判据 · `_tool_results` 未定义（execute.py）")
    print("=" * 64)
    green = run()

    # ── 变异自证：故意改坏，判据必须变红 ────────────────────────
    print("\n[变异自证] 把三处 append 故意改回 `_tool_results`，判据应变红")
    with open(TARGET, encoding="utf-8") as f:
        src = f.read()
    mutated = src.replace(f"{CANON}.append({{", f"{BUGGY}.append({{")
    if mutated == src:
        print("[FAIL] 变异未生效：源码里找不到可替换的 `tool_results.append({`")
        return 1
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(mutated)
    try:
        red = run()
        print(f"\n[变异自证] 判据结果：{'**已变红（真判据）**' if not red else '**仍是绿 ⇒ 空转断言**'}")
    finally:
        # 回滚必须放在 finally：崩了也要还原生产代码
        with open(TARGET, "w", encoding="utf-8") as f:
            f.write(src)
    # 独立复核：生产态必须与原样逐字一致
    with open(TARGET, encoding="utf-8") as f:
        restored = f.read()
    assert restored == src, "回滚失败：生产代码未还原！"
    print("[OK  ] 回滚校验：生产代码已逐字还原")

    green_again = run()
    return 0 if (green and not red and green_again) else 1


if __name__ == "__main__":
    sys.exit(main())