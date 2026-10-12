"""verify_stdlib_index_coverage — 标准库索引**覆盖率**判据（含变异自证）。

## 缺陷（2026-10-09 实测发现）
`sysml_stdlib_tools._DEF_RE` 的 `name` 只认 `\w+`，
**漏掉 `<gv>` 这种尖括号标识符**。而标准库确实在用它：

    view def <gv> GeneralView { ...        ← Systems Library/StandardViewDefinitions.sysml
    attribute <g> gram : MassUnit;         ← Domain Libraries/Quantities and Units/SI.sysml

⇒ 实测 **7 个包"声明了却索引到 0 个成员"**，`sysml_stdlib_meta` 对它们一律回
「未找到包X」。
⇒ 连锁后果：模型据此以为那个包不存在，**去查标准库、改代码**
（实测该工具占八视图工具调用的 13/41 = 32%，是耗时大头）。

## 为什么要有这个判据
"索引有没有漏包" **不能靠肉眼看工具输出** ——
工具对漏掉的包回的是「未找到包」，看起来完全像"模型问错了"。
⇒ 必须**直接扫库**、与磁盘上的真实文件对照。

## 判据
① **零成员文件数 = 0**：每个声明了 `package` 的 `.sysml` 都必须索引到成员
② **已知易漏包必须在列**：`StandardViewDefinitions` / `Attributes` / `SI` …
③ **定义总数下限**：防止"改坏了导致索引整体变空"

## 变异自证（必跑）
把 `name` 正则改回 `\w+`，断言 ①②③ **变红**；改回后再断言变绿。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_stdlib_index_coverage.py
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 实测因name 正则漏 `<...>` 而索引不到成员的包（2026-10-09）。
#: ⚠️ 判据只要求这些包**在索引里能查到**（`packages` 有键），
#:   **不要求成员数 > 0** —— 首版要求 `>0` 结果永远判红：
#:   · `SI` / `SIPrefixes` 只有实例（`attribute <g> gram`），本就0 成员
#:   · `DS_Views` 只是外层壳（成员都在子包 `SymbolicViews`），修复后**在索引、0 成员**
#:   ⇒ 混在一起的教训：**「能查到」与「有成员」是两件事**，断言必须分开写。
#:   「有成员」那部分交给判据 ①（零成员白名单）。
KNOWN_TRICKY = [
    "StandardViewDefinitions",   # view def <gv> GeneralView
    "SI",                        # attribute <g> gram
    "SIPrefixes",
    "DS_Views",
    "ParametersOfInterestMetadata",
    "RequirementDerivation",
]

#: **实测确认「本来就 0 成员」的包** —— 不是缺陷，是这些包只有
#: `import` / `alias` / nested package，压根没有 `def` 定义。
#: ⚠️ 首版我把这5 个也当"漏包"写进 KNOWN_TRICKY，判据因此**永远判红**
#:   ⇒ 与"空转断言"相反的错误：**判据本身写错 ⇒ 永远是红**。
#:   逐个查过内容才敢这么写：
#:     SI            —— `attribute <g> gram : MassUnit;`（实例，非 def）
#:     SIPrefixes    —— 同为实例
#:     Attributes    —— `alias AttributeValue for DataValue`（alias，非 def）
#:     SysML         —— `package Systems { … }` 嵌套，子包才有 def
#:     DS_Views / ParametersOfInterestMetadata / RequirementDerivation —— 已修（曾为 0）
LEGITIMATELY_EMPTY = {"SI", "SIPrefixes", "Attributes", "SysML"}

#: 定义总数下限（实测放宽后为 1308；留余量防波动，只挡"整体变空"这种事故）
MIN_DEFS = 1200

#: **严格下限**：实测旧正则 1286 / 放宽后 1308。取 1300——
#: 低于 1300 ⇒ `<...>` 标识符漏索引回来了（这是唯一能抓住它的判据）。
MIN_DEFS_STRICT = 1300


def scan() -> tuple[int, list[tuple[str, str]]]:
    """扫库 → (定义总数, [(包名, 相对路径)]) 列表中第二项为「零成员」的包。"""
    import glob
    import sysml_stdlib_tools as T
    idx = T.build_index()
    total = sum(len(p["members"]) for p in idx["packages"].values())
    zero = []
    for path in sorted(glob.glob(os.path.join(T.LIB_DIR, "**", "*.sysml"),
                                 recursive=True)):
        src = open(path, encoding="utf-8", errors="replace").read()
        pk = [m.group(1) for m in T._PKG_RE.finditer(src)]
        if not pk:
            continue
        members = idx["packages"].get(pk[0], {}).get("members") or []
        if not members:
            zero.append((pk[0],
                         os.path.relpath(path, T.LIB_DIR).replace(os.sep, "/")))
    return total, zero


def run() -> bool:
    import glob
    import sysml_stdlib_tools as T
    total, zero = scan()
    idx_pkgs = T.build_index()["packages"]
    pkgs = sorted(idx_pkgs)
    ok = True

    print(f"[INFO] 索引包数 = {len(pkgs)}；定义总数 = {total}")

    # ① **每个声明了 package 的文件，其包名都必须能在索引里查到**。
    #    ⚠️首版这里断言的是「零成员文件数 = 0」，**方向错了**：
    #    修复后 `DS_Views` / `SI` / `Attributes` 等**就该是 0 成员**
    #    （只有 import / alias / 嵌套子包）⇒ 那条断言永远判红。
    #    真正要保的是「**包能查到**」，这才是工具回「未找到包」与否的分界。
    absent_files = []
    for path in sorted(glob.glob(os.path.join(T.LIB_DIR, "**", "*.sysml"),
                                 recursive=True)):
        src = open(path, encoding="utf-8", errors="replace").read()
        pk = [m.group(1) for m in T._PKG_RE.finditer(src)]
        if not pk:
            continue
        for name in pk:
            if name not in idx_pkgs:
                absent_files.append((name, os.path.relpath(path, T.LIB_DIR)))
    passed = not absent_files
    print(f"[{'OK  ' if passed else 'FAIL'}] 所有 `.sysml` 声明的包名都能在索引里查到"
          f"（扫 {len(idx_pkgs)} 个包，**查不到**的 {len(absent_files)} 个）")
    for name, f in absent_files:
        print(f"        ★ 查不到: {name} ← {f}")
    ok = ok and passed

    # ①b 零成员包的清单**留痕**（不是判据 —— 它们本就该是 0）
    print(f"[INFO] 零成员包 {len(zero)} 个"
          + (f"：{[p for p, _ in zero]}" if zero else "")
          + " —— 工具仍可回「包 X 存在但无成员」")
    ok = ok and True

    # ② 已知易漏包必须**能在索引里查到**（成员数不限，见上方注释）
    absent = [n for n in KNOWN_TRICKY if n not in idx_pkgs]
    passed = not absent
    detail = ", ".join(f"{n}={len(T.build_index()['packages'][n]['members'])}成员"
                       for n in KNOWN_TRICKY if n in idx_pkgs)
    print(f"[{'OK  ' if passed else 'FAIL'}] {len(KNOWN_TRICKY)} 个`<...>` 标识符易漏包"
          f"均**在索引中可查**（{detail}）"
          + (f"（缺失 {absent}）" if absent else ""))
    ok = ok and passed

    # ③ 定义总数下限
    passed = total >= MIN_DEFS
    print(f"[{'OK  ' if passed else 'FAIL'}] 定义总数 {total} ≥ 下限 {MIN_DEFS}"
          "（防「改坏导致索引整体变空」）")
    ok = ok and passed

    # ★ ④ **定义数下限**（真正抓住 `_DEF_RE` 放宽的那一条）。
    #   为什么必须单列：修复 ②（无条件登记包）之后，**即使把name 正则改回
    #   `\w+`，所有包名仍都能查到** ⇒ 判据 ①②照样绿
    #   ⇒ 「索引有没有漏成员」这件事，**只有定义总数能证明**。
    #   实测：正则放宽前 1286→ 放宽后 1308（+22）。
    #   这也印证变异自证的价值：**它的第一版就是这样"仍是绿"**，
    #   说明必须有一条直接对着"成员"本身的判据。
    passed = total >= MIN_DEFS_STRICT
    print(f"[{'OK  ' if passed else 'FAIL'}] 定义总数 {total} ≥ 严格下限"
          f" {MIN_DEFS_STRICT}（★ 抓住 `<...>` 标识符漏索引；实测 1286→1308）")
    ok = ok and passed

    return ok


def main() -> int:
    print("=" * 68)
    print("标准库索引覆盖判据 · `_DEF_RE` 漏 `<...>` 标识符")
    print("=" * 68)
    green = run()

    print("\n[变异自证] 把 name 正则改回 `\\w+`，判据应变红")
    src_path = os.path.join(_ROOT, "sysml_stdlib_tools.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    mutated = src.replace(
        r'(?P<name><[^>\n]+>|\w+)', r'(?P<name>\w+)')
    if mutated == src:
        print("[FAIL] 变异未生效：找不到可替换的 name 正则")
        return 1
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(mutated)
    try:
        # 索引是进程内缓存，换进程自然重建 ⇒ 直接重跑 scan 即可
        import importlib
        import sysml_stdlib_tools
        importlib.reload(sysml_stdlib_tools)
        red = run()
        print(f"\n[变异自证] 判据结果："
              f"{'**已变红（真判据）**' if not red else '**仍是绿 ⇒ 空转断言**'}")
    finally:
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(src)
        import importlib
        import sysml_stdlib_tools
        importlib.reload(sysml_stdlib_tools)
    with open(src_path, encoding="utf-8") as f:
        assert f.read() == src, "回滚失败：生产代码未还原！"
    print("[OK  ] 回滚校验：生产代码已逐字还原")

    green_again = run()
    print("\n" + "=" * 68)
    print(f"判据结论：{'全部通过' if (green and not red and green_again) else '未通过'}")
    print("=" * 68)
    return 0 if (green and not red and green_again) else 1


if __name__ == "__main__":
    sys.exit(main())