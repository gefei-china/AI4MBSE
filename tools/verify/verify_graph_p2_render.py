# -*- coding: utf-8 -*-
"""门禁：图谱前端 P2（社区聚类着色 / 视角保存 / 导出当前视野 / 键盘可达性）。

背景（2026-11-28，方案 docs/图谱前端样式与交互优化方案-20261127.md §P2）：
  P2 是"中长期"四条。本门禁把它们钉成判据，并对每条做变异自证
  （证明断言有鉴别力，不是空转）。

判据分组：
  C 社区聚类（真跑 gvDetectCommunities：两团分离 / 输入顺序无关 / 孤立不分社区 /
              连通分量模式 / 色带低饱和 / 加成员不换色 / 写回 n._community）
  V 视角保存（真跑 gvSnapshotView/gvSanitizeSnapshot/gvApplySnapshot：
              字段齐备 / 存取往返 / 按分支隔离 / 悬空项清洗 + 缩放夹紧 /
              跨分支拒绝 / 精确复原 / 坐标缺失退化为重排 / 上限与同名覆盖 / 删除）
  X 导出当前视野（真跑 gvExportBBox/gvExportSvgSource/gvExportPngScale：
              包围盒换算到视口坐标 / PNG 倍率双上限 / SVG 补 xmlns+viewBox+字体栈 /
              背景 rect / 内联类样式 / 剥 tabindex）
  K 键盘可达性（真跑 gvNeighborInDirection/gvNodeKey/gvRestoreFocus：
              四向选邻且优先正对 / 无邻返回 null / Enter 选中并阻止默认 /
              方向键移焦点且阻止滚动 / Esc 清除 / 重建后焦点复位）
  S 结构与单一实现（社区配色一处 / 与类型色互斥 / 图例随维度切换 / 视角只落本机 /
              快照带 schema+分支 / 清洗夹紧 / 导出区分全量与视野 / PNG 倍率走函数 /
              ARIA 属性 / 重建后恢复焦点 / 色带不散落）
  M 变异自证（5 组，每组只允许目标判据翻红，且不得全红）

用法：python tools/verify/verify_graph_p2_render.py
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
_HARNESS = os.path.join(_ROOT, "tools", "verify", "_graph_p2_harness.js")

RESULTS = []
FILES = ("23-ontform.js", "24-graph.js", "25-graphview.js", "26-grapheditor.js")

# Node 执行体必须产出的判据全集。缺任何一条都算红 ——
# 「执行范围缩小」是门禁最常见的假绿来源（少跑几条，总数下降但全绿）。
EXPECTED_HARNESS = {
    "C1_modularity_two_clusters", "C2_deterministic_order", "C3_isolate_null",
    "C4_components_method", "C5_palette_low_sat", "C6_color_stable", "C7_assign_writes_nodes",
    "V1_snapshot_fields", "V2_save_list_roundtrip", "V3_per_branch", "V4_sanitize_dangling",
    "V5_branch_mismatch_refused", "V6_apply_exact", "V7_apply_fallback",
    "V8_limit_and_replace", "V9_delete",
    "X1_bbox_viewport", "X2_png_scale_cap", "X3_svg_source", "X4_export_background_rect",
    "X5_export_inline_style", "X6_export_strips_tabindex",
    "K1_right_prefers_aligned", "K2_up_down_left", "K3_no_neighbor_null",
    "K4_enter_activates", "K5_arrow_moves_focus", "K6_escape_clears", "K7_restore_focus",
}


def check(cid: str, name: str, ok: bool, detail: str = ""):
    RESULTS.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})


def _read(mods_dir: str, name: str) -> str:
    p = os.path.join(mods_dir, name)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


def _body(src: str, header: str) -> str:
    """取函数体：从函数头起做花括号配平。模板串里的 ${...} 也是配平的。"""
    i = src.find(header)
    if i < 0:
        return ""
    j = src.find("{", i)
    depth, k = 0, j
    while k < len(src):
        c = src[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    return src[i:]


def run_harness(mods_dir: str):
    """执行 Node 执行体，返回 {id: (ok, detail)}。"""
    try:
        p = subprocess.run(["node", _HARNESS, mods_dir],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:                       # noqa: BLE001
        return {"H0_spawn": (False, "node 调用失败: %s" % e)}
    lines = (p.stdout or "").strip().splitlines()
    if not lines:
        return {"H0_output": (False, "无输出; stderr=%s" % (p.stderr or "")[:200])}
    try:
        data = json.loads(lines[-1])
    except Exception:                            # noqa: BLE001
        return {"H0_parse": (False, "最后一行不是 JSON: %s" % lines[-1][:200])}
    return {c["id"]: (bool(c["ok"]), c.get("detail", "")) for c in data.get("checks", [])}


def evaluate(mods_dir: str):
    global RESULTS
    RESULTS = []
    src23 = _read(mods_dir, "23-ontform.js")
    src24 = _read(mods_dir, "24-graph.js")
    src25 = _read(mods_dir, "25-graphview.js")
    src26 = _read(mods_dir, "26-grapheditor.js")
    allsrc = (src23, src24, src25, src26)

    snap_body = _body(src24, "function gvSnapshotView(")
    san_body = _body(src24, "function gvSanitizeSnapshot(")
    apply_body = _body(src24, "function gvApplySnapshot(")
    save_body = _body(src24, "function gvSaveCurrentView(")
    list_body = _body(src24, "function gvListSavedViews(")
    store_body = _body(src24, "function gvViewStoreWrite(")
    detect_body = _body(src24, "function gvDetectCommunities(")
    svgx_body = _body(src26, "function gvExportSvgSource(")
    pngscale_body = _body(src26, "function gvExportPngScale(")
    export_body = _body(src26, "function gvExportView(")
    key_body = _body(src26, "function gvNodeKey(")
    dir_body = _body(src26, "function gvNeighborInDirection(")

    # ── C/V/X/K：真函数执行 ──
    harness = run_harness(mods_dir)
    missing = sorted(EXPECTED_HARNESS - set(harness))
    check("H1_harness_complete", "Node 执行体产出全部预期判据（不允许静默少跑）",
          not missing,
          ("缺:" + ",".join(missing)) if missing else "%d 条齐备" % len(EXPECTED_HARNESS))
    for cid, (ok, detail) in harness.items():
        check(cid, "（Node 实测）%s" % cid, ok, detail)
    r0 = sorted([k for k in harness if re.match(r"^[CVXK]0_", k)])
    if r0:
        check("H2_harness_no_runtime_error", "Node 执行体无运行期异常",
              all(harness[k][0] for k in r0), ",".join(r0))

    # ── S 组：结构与单一实现 ──
    check("S1_comm_color_one_impl", "社区配色只有一处实现",
          sum(s.count("function gvCommunityColor(") for s in allsrc) == 1,
          str(sum(s.count("function gvCommunityColor(") for s in allsrc)))

    check("S2_color_mutually_exclusive", "社区色与类型色互斥（社区模式取社区色，孤立退回类型色）",
          "colorByCommunity && n._community" in src25 and "gvCommunityColor(n._community)" in src25)

    check("S3_legend_switch", "图例随着色维度切换（社区模式给规模，类型模式才可点切类型）",
          "graphCommunityStats()" in src25 and "graphSetColorBy(false)" in src25
          and "graphSetColorBy(true)" in src25)

    check("S4_default_type_coloring", "默认仍是类型色（community 只能由用户显式开启）",
          "colorBy" not in src23 and "colorBy === 'community'" in src25)

    check("S5_views_local_only", "视角只落本机（不写后端、不跨用户）",
          "localStorage.setItem" in store_body and "api(" not in save_body and "fetch(" not in save_body,
          "store/localStorage=%s save/api=%s" % ("localStorage.setItem" in store_body, "api(" in save_body))

    check("S6_snapshot_branch_schema", "快照带 schema 版本与分支名（版本化 + 分支绑定）",
          "schema: 1" in snap_body and "branch: getCurrentBranch()" in snap_body)

    check("S7_sanitize_clamps", "恢复前清洗悬空项并夹紧缩放（0.3–3）",
          "clamp(svp.scale, 0.3, 3, 1)" in san_body and "dropped.focus" in san_body
          and "dropped.types" in san_body)

    check("S8_export_view_items", "导出下拉区分「全量数据」与「当前视野」并提供 SVG/PNG",
          "gvExportView('svg')" in src25 and "gvExportView('png')" in src25
          and "全量数据" in src25 and "当前视野" in src25)

    check("S9_export_svg_complete", "导出 SVG 补 xmlns / 背景 rect / 字体栈 / viewBox",
          "setAttribute('xmlns'" in svgx_body and "createElementNS(NS, 'rect')" in svgx_body
          and "font-family" in svgx_body and "viewBox" in svgx_body)

    check("S10_png_scale_used", "PNG 光栅化倍率走上限函数（不硬编码 2 倍）",
          "gvExportPngScale(" in export_body and "ctx.fillStyle = graphTok('canvas')" in export_body
          and "const scale = 2" not in export_body)

    check("S11_a11y_attrs", "节点 <g> 带 tabindex/role/aria-label 与 keydown 接线",
          'tabindex="0" role="button" aria-label=' in src25 and "onkeydown=\"gvNodeKey(event" in src25)

    check("S12_focus_restore", "renderGraph 重建后恢复焦点（innerHTML 会丢焦点）",
          src25.count("gvRestoreFocus(document.getElementById('gv-svg'))") == 1,
          str(src25.count("gvRestoreFocus(document.getElementById('gv-svg'))")))

    check("S13_keyboard_no_scroll", "方向键阻止默认（不许变成页面滚动）",
          "ev.preventDefault()" in key_body and "// 方向键不得滚动页面" in key_body)

    check("S14_palette_not_scattered", "社区色带只在 24-graph.js 定义一次（其它文件不写死）",
          all(src24.count(h) == 1 for h in re.findall(r"'(#[0-9A-Fa-f]{6})'", _body(src24, "const GV_COMMUNITY_PALETTE = [")))
          and not any(re.search(r"#[0-9A-Fa-f]{6}", src25) and h in src25
                      for h in re.findall(r"'(#[0-9A-Fa-f]{6})'", _body(src24, "const GV_COMMUNITY_PALETTE = ["))),
          "palette 命中 24:%d" % len(re.findall(r"'(#[0-9A-Fa-f]{6})'", _body(src24, "const GV_COMMUNITY_PALETTE = ["))))

    check("S15_pngscale_one_impl", "PNG 倍率函数只有一处实现",
          sum(s.count("function gvExportPngScale(") for s in allsrc) == 1)

    check("S16_view_menu_refresh", "保存/删除后重渲染视角下拉（不只改数据）",
          "gvRefreshViewMenu()" in _body(src25, "function gvSaveViewFromInput(")
          and "gvRefreshViewMenu()" in _body(src25, "function gvRemoveSavedView("))

    check("S17_modularity_documented", "算法与方案原文的偏离有据可查（LPA 会塌成单社区）",
          "LPA 会把**整张连通图塌成 1 个社区**" in src24 or "塌成 1 个社区" in src24)

    return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}


MUTATIONS = [
    ("M1_孤立节点也分社区", "24-graph.js",
     "  list.forEach(id=>{ if(wdegOf(id) === 0) label.set(id, null); });",
     "  list.forEach(id=>{ if(wdegOf(id) === 0) label.set(id, 'c:' + id); });",
     "C3_isolate_null"),
    ("M2_视角不分分支", "24-graph.js",
     "  const br = branch || getCurrentBranch();",
     "  const br = 'personal';",
     "V3_per_branch"),
    ("M3_不清洗悬空骨架焦点", "24-graph.js",
     "  if(focus && !idsNow.has(focus)){ dropped.focus = 1; focus = null; }",
     "  if(false){ dropped.focus = 1; focus = null; }",
     "V4_sanitize_dangling"),
    ("M4_导出缺xmlns", "26-grapheditor.js",
     "  clone.setAttribute('xmlns', NS);",
     "  void 0;",
     "X3_svg_source"),
    ("M5_方向键忽略横向偏移", "26-grapheditor.js",
     "    const score = along - across * 2;",
     "    const score = along;",
     "K1_right_prefers_aligned"),
]


def mutation_selftest(base_results):
    """逐条改坏源码 → 断言目标判据翻红且未全红 → 还原。返回变异组检查项。"""
    mut_checks = []

    def mcheck(cid, name, ok, detail=""):
        mut_checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})

    tmp = tempfile.mkdtemp(prefix="gv_p2_mut_")
    try:
        for f in FILES:
            src_p = os.path.join(_MODS, f)
            if os.path.exists(src_p):
                shutil.copy2(src_p, os.path.join(tmp, f))
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
                green = [cid for cid, (ok, _) in got.items() if ok]
                mcheck("M:" + target, "变异后目标判据翻红且未全红（%s）" % title,
                       flipped and len(green) >= 3,
                       "flipped=%s 仍绿=%d 目标=%s" % (flipped, len(green),
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
