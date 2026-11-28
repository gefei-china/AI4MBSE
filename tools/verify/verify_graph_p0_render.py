# -*- coding: utf-8 -*-
"""门禁：图谱前端 P0 优化（样式单一真源 / 拖拽增量重绘 / 三画布统一焦点 / 统一提示）。

背景（2026-11-28，方案 docs/图谱前端样式与交互优化方案-20261127.md）：
  现状四短板：① 四处硬编码配色，SVG 与 Cytoscape 同类型色可漂移；
  ② 拖拽时每次 mousemove 整图 innerHTML 重建（大图掉帧）；③ SysML/影响图只有 :selected，
  没有主画布的 1-hop 邻域高亮；④ 三处提示各写一套。
  本门禁把四件事钉成判据，并做变异自证（证明断言有鉴别力，不是空转）。

判据分四组：
  A 单一真源（真读 CSS 变量 / 类型色由 token 派生 / 换品牌色跟着变）
  B 拖拽增量（真跑 gvPatchNode：只动被拖节点与相邻边；graphSvgMove 不得再调 renderGraph）
  C 统一焦点（真跑 applyGraphFocus：压暗非邻域、点亮焦点、点空白只清不加）
  D 统一提示（三 mode 齐备 / 明确「点空白取消」/ 不预告未落地能力）
  S 结构（跨文件不得各写一份色表/焦点实现；三处必须接线）
  M 变异自证（4 组，每组只允许目标判据翻红，不能全红）

用法：python tools/verify/verify_graph_p0_render.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODS = os.path.join(_ROOT, "static", "js", "mods")
_HARNESS = os.path.join(_ROOT, "tools", "verify", "_graph_p0_harness.js")

RESULTS = []
FILES = ("24-graph.js", "25-graphview.js", "26-grapheditor.js", "09-impact.js")

# 参与「单一真源」判定的语义色（大小写不敏感）
BRAND = [
    "185FA5", "0C447C", "E6F1FB", "3B6D11", "EAF3DE", "BA7517", "A32D2D", "6B4FA1",
    "F1E8F9", "FFF7E0", "FDF0F0", "F5F8FC", "F2F4F8", "5F5E5A", "B4B2A9", "D85A30",
    "E8912D", "ECEDE7", "FAC775", "FFF1B8", "FFF9EC", "CFE8FF", "9DB8D8", "C9CCC4",
    "F8B24B", "D98E10", "C7CFDA", "8A94A2", "EDF1F6", "9AA7B5", "B9C2CE", "F5A623",
    "7A1F1F",
]


def check(cid: str, name: str, ok: bool, detail: str = ""):
    RESULTS.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})


def _read(mods_dir: str, name: str) -> str:
    with open(os.path.join(mods_dir, name), encoding="utf-8") as f:
        return f.read()


def _body(src: str, header: str):
    """取函数体：从 `function NAME(` 起做花括号配平（模板串里的 ${...} 也是配平的）。"""
    i = src.find(header)
    if i < 0:
        return None
    j = src.find("{", i)
    depth = 0
    for k in range(j, len(src)):
        ch = src[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    return None


def _brand_hits(text: str):
    low = text.lower()
    return sorted({h for h in BRAND if ("#" + h.lower()) in low})


def _code_lines(text: str):
    """只保留非注释行（行首 // 或 /* 块注释），避免「注释里提到 renderGraph」被当成真调用。"""
    out, in_block = [], False
    for line in text.splitlines():
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def run_harness(mods_dir: str):
    """在 Node 里 exec 真函数（不复刻逻辑）；node 缺失 ⇒ 明确失败，不静默通过。"""
    try:
        p = subprocess.run(["node", _HARNESS, mods_dir], capture_output=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        check("N0_node", "Node 可用（判据需 exec 真函数，CI ubuntu runner 自带）", False, repr(e))
        return {}
    out = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    if not out:
        check("N1_harness", "Node 执行体返回结果", False, (p.stderr or b"").decode("utf-8", "replace")[-500:])
        return {}
    try:
        data = json.loads(out[-1])
    except Exception as e:
        check("N1_harness", "Node 执行体返回结果", False, "%r / %s" % (e, out[-1][:200]))
        return {}
    got = {}
    for c in data.get("checks", []):
        got[c["id"]] = (bool(c.get("ok")), c.get("detail", ""))
    return got


def evaluate(mods_dir: str) -> dict:
    """跑一次全量判据，返回 {id: (ok, detail)}，便于变异前后对比。"""
    global RESULTS
    RESULTS = []
    got = run_harness(mods_dir)
    for cid, (ok, detail) in got.items():
        check(cid, cid, ok, detail)

    src24, src25 = _read(mods_dir, "24-graph.js"), _read(mods_dir, "25-graphview.js")
    src26, src09 = _read(mods_dir, "26-grapheditor.js"), _read(mods_dir, "09-impact.js")

    # ── S1 主画布 renderGraph：色值必须走 token，不得再写死语义色 ──
    rg = _body(src25, "function renderGraph(") or ""
    check("S1_renderGraph_no_hardcode", "主画布 renderGraph 无写死语义色",
          bool(rg) and not _brand_hits(rg), "hits=%s" % (_brand_hits(rg) or "none"))

    # ── S2 拖拽路径：不得整图重建（只看非注释行，注释里解释「不再 renderGraph」不算） ──
    mv = _body(src26, "function graphSvgMove(") or ""
    mv_code = _code_lines(mv)
    check("S2_drag_no_full_rebuild", "拖拽 mousemove 不调 renderGraph（改增量 patch）",
          bool(mv) and "renderGraph(" not in mv_code and "gvPatchNodeSoon(" in mv_code,
          "renderGraph=%s patch=%s" % ("renderGraph(" in mv_code, "gvPatchNodeSoon(" in mv_code))

    # ── S3 影响图：样式表与图例都不得写死语义色 ──
    imp = (_body(src09, "function renderImpactGraph(") or "") + (_body(src09, "function impactLegendHtml(") or "")
    check("S3_impact_no_hardcode", "影响图渲染/图例无写死语义色",
          bool(imp) and not _brand_hits(imp), "hits=%s" % (_brand_hits(imp) or "none"))

    # ── S4 SysML Cytoscape 样式表：无写死语义色（data URI 里的 %23 不算） ──
    svm = _body(src25, "function svmRenderCytoscape(") or ""
    check("S4_sysml_no_hardcode", "SysML Cytoscape 样式无写死语义色",
          bool(svm) and not _brand_hits(svm), "hits=%s" % (_brand_hits(svm) or "none"))

    # ── S5 两处 Cytoscape 都声明了统一的焦点类 ──
    for cid, name, src in (("S5a", "SysML 画布声明 .dimmed/.focus-hi", src25),
                           ("S5b", "影响图画布声明 .dimmed/.focus-hi", src09)):
        check(cid, name, "'.dimmed'" in src and "'.focus-hi'" in src)
    # S5c：压暗强度必须同源。原 SysML 写 0.18、影响图写 0.12 —— 同一语义两个视觉权重。
    check("S5c", "两画布压暗强度取自同一 token",
          src25.count("'opacity':k.dimOpacity") == 1 and src09.count("'opacity':k.dimOpacity") == 1
          and not re.search(r"'\.dimmed',\s*style:\{'opacity':\s*[0-9]", src25 + src09))

    # ── S6 焦点必须走统一入口（不是各画布各写一套） ──
    check("S6a", "SysML tap 走 applyGraphFocus", "applyGraphFocus(cy" in svm or "applyGraphFocus(cy," in svm)
    check("S6b", "影响图 tap 走 applyGraphFocus", "applyGraphFocus(cy" in imp)
    check("S6c", "applyGraphFocus 只有一处实现",
          sum(s.count("function applyGraphFocus(") for s in (src24, src25, src26, src09)) == 1)
    check("S6d", "graphTok 只有一处实现",
          sum(s.count("function graphTok(") for s in (src24, src25, src26, src09)) == 1)
    # S6e：压暗类**只允许**由 applyGraphFocus 写入。
    # 2026-11-28 实测 09-impact.js 里 impactViewerEvents / impactFocusNode 各手写了一份
    # `addClass('dimmed')`，语义漂移点（其中一份漏了先清后加）。判据盯「写入点个数」，不盯调用次数。
    _dim_writers = {f: s.count("addClass('dimmed')") for f, s in
                    (("24-graph.js", src24), ("25-graphview.js", src25),
                     ("26-grapheditor.js", src26), ("09-impact.js", src09))}
    check("S6e", "压暗类只有 applyGraphFocus 一处写入",
          sum(_dim_writers.values()) == 1 and _dim_writers["24-graph.js"] == 1,
          str(_dim_writers))

    # ── S7 三处提示条接线 ──
    check("S7a", "主画布接入 gvHintBar('kg')", "gvHintBar('kg')" in src25)
    check("S7b", "SysML 视图接入 gvHintBar('sysml')", "gvHintBar('sysml')" in src25)
    check("S7c", "影响图接入 gvHintBar('impact')", "gvHintBar('impact')" in src09)

    return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}


# ── 变异自证 ────────────────────────────────────────────────────
MUTATIONS = [
    ("M1_色彩不再来自 token", "24-graph.js",
     "if(T[t]) return T[t];", "if(T[t]) return '#000000';", "A3_type_color_from_token"),
    ("M2_拖拽退回整图重建", "26-grapheditor.js",
     "gvPatchNodeSoon(graphState.drag.id);", "renderGraph();", "S2_drag_no_full_rebuild"),
    ("M3_焦点不再压暗非邻域", "24-graph.js",
     "target.elements().not(el.closedNeighborhood()).addClass('dimmed');",
     "target.elements().not(el.closedNeighborhood());", "C1_focus_dims_non_neighbors"),
    ("M4_影响图丢掉 dimmed 样式", "09-impact.js",
     "{selector:'.dimmed', style:{'opacity':k.dimOpacity}},",
     "{selector:'.dimmed-x', style:{'opacity':k.dimOpacity}},", "S5b"),
    # M5：把「Cytoscape 能力判定」退回误用 core 上不存在的 closedNeighborhood
    #     —— 2026-11-28 真机实测缺陷的原样复现（三画布焦点高亮全部静默失效）
    ("M5_Cytoscape判型误用core方法", "24-graph.js",
     "    && typeof target.getElementById === 'function';",
     "    && typeof target.closedNeighborhood === 'function';",
     "C2_focus_clears_first"),
]


def mutation_selftest(base_results):
    """每组变异：只允许目标判据翻红，且不能全红。返回变异组自己的判据列表。"""
    global RESULTS
    mut_checks = []

    def mcheck(cid, name, ok, detail=""):
        mut_checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})

    tmp = tempfile.mkdtemp(prefix="gv_p0_mut_")
    try:
        for f in FILES:
            shutil.copy2(os.path.join(_MODS, f), os.path.join(tmp, f))
        for title, fname, old, new, target in MUTATIONS:
            src = _read(_MODS, fname)
            if src.count(old) != 1:
                mcheck("M_anchor:" + target, "变异锚点唯一（%s）" % title, False,
                       "count=%d" % src.count(old))
                continue
            mutated = src.replace(old, new, 1)
            if mutated == src:   # 变异没打中 ⇒ 后续「翻红」结论无效
                mcheck("M_changed:" + target, "变异体真的变了（%s）" % title, False)
                continue
            with open(os.path.join(tmp, fname), "w", encoding="utf-8", newline="") as fp:
                fp.write(mutated)
            try:
                got = evaluate(tmp)
                flipped = got.get(target, (True, ""))[0] is False
                others = [cid for cid, (ok, _) in got.items() if ok]
                mcheck("M:" + target, "变异后目标判据翻红且未全红（%s）" % title,
                       flipped and len(others) >= 3,
                       "flipped=%s 仍绿=%d 目标=%s" % (flipped, len(others),
                                                     (got.get(target) or ("?", ""))[1]))
            finally:
                shutil.copy2(os.path.join(_MODS, fname), os.path.join(tmp, fname))
        mcheck("M_base", "基线全量判据绿（变异对照的基准）",
               all(r["ok"] for r in base_results),
               "红项=%d" % sum(1 for r in base_results if not r["ok"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        RESULTS = list(base_results)   # 恢复基线结果，避免变异运行污染汇总
    return mut_checks


def main() -> int:
    global RESULTS
    evaluate(_MODS)
    base_results = list(RESULTS)
    RESULTS = base_results + mutation_selftest(base_results)

    seen, dup = set(), []
    for r in RESULTS:
        if r["id"] in seen:
            dup.append(r["id"])
        seen.add(r["id"])

    for r in RESULTS:
        if not r["ok"]:
            print("FAIL [%s] %s :: %s" % (r["id"], r["name"], r["detail"][:220]))
    red = [r for r in RESULTS if not r["ok"]]
    print("")
    print("合计 %d 项，绿 %d，红 %d%s" % (len(RESULTS), len(RESULTS) - len(red), len(red),
                                    ("（重复 id：%s）" % dup) if dup else ""))
    return 1 if (red or dup) else 0


if __name__ == "__main__":
    sys.exit(main())
