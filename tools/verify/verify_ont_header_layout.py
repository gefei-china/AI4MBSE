# -*- coding: utf-8 -*-
"""门禁：顶部 Tab 行 / 工具行「不得变形折行」（CSS 结构层，CI 可跑）。

背景（2026-11-28 米爸反馈）："本体模型页面顶部的操作入口变形了，都换行了"。

真机实测根因（不是"窗口太窄"）：
  `.subtab{display:flex}` 既没 `flex-wrap` 也没 `white-space`，子项默认 `flex-shrink:1`。
  本体顶栏有 6 Tab + 9 按钮 + 3 个占位 + 版本徽标，自然宽 **1510px**，容器仅 **1356px**
  ⇒ 每一项被压窄到**小于自身文字宽度** ⇒ 文字在每一项内部折行：
    · Tab 高 41→64，按钮高 29→47
    · 出现 **8 个不同 top 值**（115~162）⇒ 竖向错位，这就是"变形"
  另一处诱因：行内 `<span style="flex:1">` 占位项在 flex-wrap 容器里先吃掉本行剩余空间
  （实测被拉到 225px），把版本徽标挤到第二行 ⇒ 行高 93（两行）、Tab 下划线脱离行底边框。

修法三条（缺一不可）：
  ① `.subtab` 加 `flex-wrap:wrap` —— 放不下时**整行换行**，而不是把文字压进每一项内部；
  ② 子项 `white-space:nowrap` + `flex:0 0 auto` —— 任何一项都不被压窄、不折行；
  ③ 本体工具行：隐藏 `flex:1` 占位项，改由第一个下拉 `margin-left:auto` 推到右侧，
     使参与换行计算的只剩真实控件 ⇒ 单行装得下；同时逐项收紧留白（不隐藏任何入口）。

本门禁检查的是**结构不变量**（CI 无浏览器也能跑）；真实像素测量在
verify_ont_runtime_browser.js 的 B8/B9（tier=local）。

用法：python tools/verify/verify_ont_header_layout.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CSS = os.path.join(_ROOT, "static", "css", "app.css")
_HTML = os.path.join(_ROOT, "static", "index.html")

RESULTS = []
FILES = ("static/css/app.css",)


def check(cid, name, ok, detail=""):
    RESULTS.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})


def _read(p):
    """读文件必须保留原始行尾（newline=""）：默认读取会把 CRLF 规范化成 LF，
    变异自证的「还原」再写回就会把整个文件的换行符改掉（曾把 24-graph.js 从 CRLF 变成 LF）。"""
    if not os.path.exists(p):
        return ""
    with io.open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _rule(src, selector):
    """取某条 CSS 规则的声明体（首个匹配的 `selector{...}`）。"""
    i = src.find(selector + "{")
    if i < 0:
        return ""
    j = src.find("}", i)
    return src[i + len(selector) + 1: j] if j > 0 else ""


def evaluate():
    global RESULTS
    RESULTS = []
    css = _read(_CSS)
    html = _read(_HTML)

    subtab = _rule(css, ".subtab")
    child = _rule(css, ".subtab > *")

    check("H1_subtab_wraps", ".subtab 允许整行换行（flex-wrap:wrap）",
          "flex-wrap:wrap" in subtab.replace(" ", ""), subtab[:120])

    check("H2_child_no_shrink", "子项不被压缩（flex:0 0 auto），否则会被压窄到文字折行",
          "flex:0 0 auto" in child.replace("flex: 0 0 auto", "flex:0 0 auto"),
          child[:120])

    check("H3_child_nowrap", "子项文字不折行（white-space:nowrap）",
          "white-space:nowrap" in child.replace(" ", ""), child[:120])

    # 三个不变量必须在 .subtab 本体或其子项规则里同时成立（缺一即回到变形）
    combo = ("flex-wrap" in subtab) and ("nowrap" in child) and ("0 0 auto" in child)
    check("H4_all_three_together", "三件套同时成立（缺一即复现折行变形）", combo,
          f"wrap={'flex-wrap' in subtab} nowrap={'nowrap' in child} noShrink={'0 0 auto' in child}")

    # 本体工具行：占位项必须被隐藏（否则把徽标挤到第二行）
    check("H5_ont_spacer_hidden", "本体顶栏隐藏 flex:1 占位项（它是两行的直接诱因）",
          re.search(r'#ont-subtab-row\s*>\s*span\[style\*="flex:1"\]\s*\{[^}]*display:none', css) is not None,
          "rule=" + str(re.search(r'#ont-subtab-row\s*>\s*span\[style\*="flex:1"\]\s*\{[^}]*\}', css).group(0)[:80]
                        if re.search(r'#ont-subtab-row\s*>\s*span\[style\*="flex:1"\]\s*\{[^}]*\}', css) else "missing"))

    check("H6_ont_right_push", "改用首个下拉 margin-left:auto 把操作组推到右侧",
          re.search(r'#ont-subtab-row\s*>\s*\.dd:first-of-type\s*\{[^}]*margin-left:auto', css) is not None,
          "命中" if re.search(r'#ont-subtab-row\s*>\s*\.dd:first-of-type', css) else "missing")

    check("H7_ont_keeps_tabs_underline", "仍保留 align-items:flex-end（Tab 选中下划线要贴住行底边框）",
          "#ont-subtab-row{align-items:flex-end;}" in css.replace(" ", ""),
          "flex-end 规则存在")

    # 入口不得被"藏起来"充数：CQ / 版本历史 / SWRL / 运行期语义 仍须在 HTML 里
    need = ["ontCqEntry20261015", "ontRuntimeBtn20261124"]
    missing = [x for x in need if x not in html]
    check("H8_entries_not_hidden", "没有靠隐藏入口来消掉折行（关键入口仍在 HTML 中）",
          not missing, ("缺:" + ",".join(missing)) if missing else "两个关键入口均在")

    # 不得用 overflow-x 滚动条掩盖（隐藏右侧入口同样是"看不见"）
    check("H9_no_hscroll_mask", "未用水平滚动条掩盖（那会藏住右侧入口）",
          "overflow-x:auto" not in subtab.replace(" ", "") and "overflow-x:scroll" not in subtab.replace(" ", ""),
          subtab[:100])

    return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}


MUTATIONS = [
    ("M1_去掉flex-wrap", "static/css/app.css",
     ".subtab{display:flex;flex-wrap:wrap;gap:4px;",
     ".subtab{display:flex;gap:4px;",
     "H1_subtab_wraps"),
    ("M2_子项重新可收缩", "static/css/app.css",
     ".subtab > *{flex:0 0 auto;white-space:nowrap;}",
     ".subtab > *{white-space:normal;}",
     "H2_child_no_shrink"),
    ("M3_占位项不再隐藏", "static/css/app.css",
     '#ont-subtab-row > span[style*="flex:1"]{display:none;}',
     '#ont-subtab-row > span[style*="flex:1"]{display:inline;}',
     "H5_ont_spacer_hidden"),
]


def mutation_selftest(base_results):
    """逐条改坏 CSS → 目标判据必须翻红 → 还原。"""
    mut = []

    def mrec(cid, name, ok, detail=""):
        mut.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})

    orig = _read(_CSS)
    try:
        for title, rel, old, new, target in MUTATIONS:
            p = os.path.join(_ROOT, rel)
            if orig.count(old) != 1:
                mrec("M_anchor:" + target, "变异锚点唯一（%s）" % title, False, "count=%d" % orig.count(old))
                continue
            mutated = orig.replace(old, new, 1)
            if mutated == orig:
                mrec("M_changed:" + target, "变异体真的变了（%s）" % title, False)
                continue
            with io.open(p, "w", encoding="utf-8", newline="") as f:
                f.write(mutated)
            try:
                got = evaluate()
                flipped = got.get(target, (True, ""))[0] is False
                green = [k for k, (ok, _) in got.items() if ok]
                mrec("M:" + target, "变异后目标判据翻红且未全红（%s）" % title,
                     flipped and len(green) >= 3,
                     "flipped=%s 仍绿=%d" % (flipped, len(green)))
            finally:
                with io.open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(orig)
        mrec("M_base", "基线全量判据绿（变异对照的基准）",
             all(r["ok"] for r in base_results),
             "红项=%d" % sum(1 for r in base_results if not r["ok"]))
    finally:
        with io.open(_CSS, "w", encoding="utf-8", newline="") as f:
            f.write(orig)
        RESULTS = list(base_results)
    return mut


def main():
    global RESULTS
    evaluate()
    base = list(RESULTS)
    if any(not r["ok"] for r in base):
        for r in base:
            if not r["ok"]:
                print("FAIL [%s] %s :: %s" % (r["id"], r["name"], r["detail"][:200]))
        print("\n合计 %d 项，绿 %d，红 %d（基线未绿，跳过变异自证）"
              % (len(base), sum(1 for r in base if r["ok"]), sum(1 for r in base if not r["ok"])))
        return 1
    RESULTS = base + mutation_selftest(base)
    seen, dup = set(), []
    for r in RESULTS:
        if r["id"] in seen:
            dup.append(r["id"])
        seen.add(r["id"])
    for r in RESULTS:
        if not r["ok"]:
            print("FAIL [%s] %s :: %s" % (r["id"], r["name"], r["detail"][:200]))
    red = [r for r in RESULTS if not r["ok"]]
    print("")
    print("合计 %d 项，绿 %d，红 %d%s" % (len(RESULTS), len(RESULTS) - len(red), len(red),
                                    ("（重复 id：%s）" % dup) if dup else ""))
    return 1 if (red or dup) else 0


if __name__ == "__main__":
    sys.exit(main())
