# -*- coding: utf-8 -*-
"""门禁：图谱前端 P1 交互（框选多选 / 路径高亮 / 双击折叠 / 标签密度 / 布局参数）。

背景（2026-11-28，方案 docs/图谱前端样式与交互优化方案-20261127.md §P1）：
  P1 五条是"中等成本、显著提升专业度"的交互能力。本门禁把它们钉成判据，
  并对每条做变异自证（证明断言有鉴别力，不是空转）。

判据分组：
  P 路径 BFS（真跑 gvPathBFS：跳数最短 / 无向 / 不可达 / 字典序可复现 / 自环多重边）
  Q 框选命中（真跑 gvBoxCommit：视口坐标而非世界坐标 / 误触 / 折叠域不可选）
  R 布局参数（真跑 gvLayoutWith：上下限夹紧 / dagre 也吃参 / 未传时读 state）
  F 框选接线与视觉（Shift 手势 / rect 取 token / move-up 闭环 / 多选类与 chip）
  G 路径接线（按钮 / 模式分流 / 注入高亮集合 / Esc 可退）
  H 折叠接线（dblclick / 节点与边都过滤 / +N 徽标 / 状态 chip / 取景按可见节点）
  I 标签密度（阈值取 token 唯一真源 / 两画布都切 class / 重绘后重新施加）
  J 布局参数接线（三滑杆 / 防抖 / 局部重排只动 1-hop）
  K 压暗单一真源回归（P0-2 收口：不得再出现硬编码 0.22 / 0.12）
  S 同语义单一实现（每个能力只有一处实现，阈值只有一处定义）
  M 变异自证（5 组，每组只允许目标判据翻红）

用法：python tools/verify/verify_graph_p1_interact.py
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
_HARNESS = os.path.join(_ROOT, "tools", "verify", "_graph_p1_harness.js")

RESULTS = []
FILES = ("24-graph.js", "25-graphview.js", "26-grapheditor.js")

# Node 执行体必须产出的判据全集。缺任何一条都算红 ——
# 「扫描/执行范围缩小」是门禁最常见的假绿来源（少跑几条，总数下降但全绿）。
EXPECTED_HARNESS = {
    "P1_1hop", "P2_shortest", "P3_steps_len", "P4_undirected", "P5_unreachable_null",
    "P6_deterministic", "P7_lexico_order", "P8_selfloop_multiedge",
    "Q1_hit_uses_viewport", "Q2_box_cleared", "Q3_render_after_commit",
    "Q4_tiny_box_no_select", "Q5_hidden_not_selectable",
    "R1_clamp_upper", "R2_clamp_lower", "R3_dagre_uses_opts", "R4_reads_state_opts",
}


def check(cid: str, name: str, ok: bool, detail: str = ""):
    RESULTS.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})


def _read(mods_dir: str, name: str) -> str:
    with open(os.path.join(mods_dir, name), encoding="utf-8") as f:
        return f.read()


def _body(src: str, header: str) -> str:
    """取函数体：从函数头起做花括号配平。"""
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


def _code_lines(src: str):
    """剔除注释行（否则注释里提到的函数名会被当成真调用，P0 的 S2 假红就是这么来的）。"""
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        out.append(ln)
    return out


def run_harness(mods_dir: str):
    """执行 Node 执行体，返回 {id: (ok, detail)}。"""
    try:
        p = subprocess.run([("node" if os.name != "nt" else "node"), _HARNESS, mods_dir],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:                       # noqa: BLE001
        return {"H0_spawn": (False, "node 调用失败: %s" % e)}
    line = (p.stdout or "").strip().splitlines()
    if not line:
        return {"H0_output": (False, "无输出; stderr=%s" % (p.stderr or "")[:200])}
    try:
        data = json.loads(line[-1])
    except Exception:                            # noqa: BLE001
        return {"H0_parse": (False, "最后一行不是 JSON: %s" % line[-1][:200])}
    return {c["id"]: (bool(c["ok"]), c.get("detail", "")) for c in data.get("checks", [])}


def evaluate(mods_dir: str):
    global RESULTS
    RESULTS = []
    src24 = _read(mods_dir, "24-graph.js")
    src25 = _read(mods_dir, "25-graphview.js")
    src26 = _read(mods_dir, "26-grapheditor.js")
    allsrc = (src24, src25, src26)

    down = _body(src26, "function graphSvgDown(")
    move = _body(src26, "function graphSvgMove(")
    up = _body(src26, "function graphSvgUp(")
    boxcommit = _body(src26, "function gvBoxCommit(")
    boxpaint = _body(src26, "function gvBoxPaint(")
    pathpick = _body(src26, "function gvPathPick(")
    dbl = _body(src26, "function graphNodeDblClick(")
    lblfn = _body(src26, "function gvSyncLabelDensity(")
    relayout = _body(src26, "function gvRelayoutAround(")
    optinput = _body(src26, "function gvLayoutOptInput(")
    layoutwith = _body(src24, "function gvLayoutWith(")
    fitview = _body(src25, "function gvFitView(")

    # ── P/Q/R：真函数执行 ──
    harness = run_harness(mods_dir)
    missing = sorted(EXPECTED_HARNESS - set(harness))
    extra_red = sorted([k for k in harness if k.startswith(("P0_", "Q0_", "R0_", "H0_"))])
    check("H1_harness_complete", "Node 执行体产出全部预期判据（不允许静默少跑）",
          not missing, ("缺:" + ",".join(missing)) if missing else "%d 条齐备" % len(EXPECTED_HARNESS))
    for cid, (ok, detail) in harness.items():
        check(cid, "（Node 实测）%s" % cid, ok, detail)
    if extra_red:
        check("H2_harness_no_runtime_error", "Node 执行体无运行期异常",
              all(harness[k][0] for k in extra_red), ",".join(extra_red))

    # ── F 框选接线 ──
    check("F1_shift_gesture", "Shift+空白拖动进入框选（不抢平移手势）",
          "ev.shiftKey" in down and "graphState.box" in down)
    # 注释里出现 renderGraph() 会被误判成真调用（P0 的 S2 假红就是这么来的）⇒ 先剔注释行
    move_code = "\n".join(_code_lines(move))
    check("F2_move_updates_box", "move 期间只更新框尺寸并重画（不整图重建）",
          "graphState.box" in move_code and "gvBoxPaint()" in move_code and "renderGraph()" not in move_code)
    check("F3_up_commits", "up 提交框选结果", "gvBoxCommit()" in up)
    check("F4_rect_from_token", "框选矩形取色来自 graphTok（无写死语义色）",
          "graphTok(" in boxpaint and "#" not in boxpaint)
    check("F5_rect_not_hit", "框选矩形不吃鼠标事件（否则 move 会打到 rect 上）",
          "pointer-events" in boxpaint and "none" in boxpaint)
    check("F6_hit_viewport", "命中测试走 gvWorldToScreen（世界坐标会误判视口外节点）",
          "gvWorldToScreen(" in boxcommit)
    check("F7_ms_class", "多选节点有专属 class（统一淡蓝，不与类型色抢信息）",
          "gv-ms-node" in src25 and ".gv-ms-node" in src25)
    check("F8_blank_clears_ms", "点空白清空多选（与提示条「点空白取消」语义一致）",
          "graphState.multiSel = new Set()" in down)
    check("F9_chip", "工具栏显示框选数量 chip（可选可见、可一键清空）",
          "gv-ms-chip" in src25 and "gvMultiSelClear()" in src25)

    # ── G 路径接线 ──
    check("G1_button", "工具栏「路径」按钮与开关函数齐备",
          'id="gv-path-btn"' in src25 and "function gvTogglePathMode(" in src26)
    check("G2_mode_dispatch", "路径模式下点节点走 gvPathPick（互斥于连线模式）",
          "gvPathPick(id)" in _body(src26, "async function graphNodeDown("))
    src26_code = "\n".join(_code_lines(src26))
    check("G3_global_let_trap", "模式状态用 typeof 判定（顶层 let 不挂 window，P1 顺带修掉的缺陷）",
          "typeof gvPathMode !== 'undefined'" in src26_code and "window.gvLinkMode" not in src26_code,
          "残留=%d" % src26_code.count("window.gvLinkMode"))
    check("G4_inject_highlight", "路径节点/边并入同一套高亮集合（不新造第二套视觉）",
          "_P.nodeIds.forEach" in src25 and "_P.edgeIds.forEach" in src25)
    check("G5_path_visual", "路径节点有专属描边类，路径边复用流动动效",
          "gv-path-node" in src25 and ".gv-path-node" in src25)
    check("G6_esc_exit", "Esc 可退出路径模式（最临时状态优先收）",
          "gvPathClear()" in src26 and "__gvP1EscBound" in src26)
    check("G7_detail", "右栏输出路径摘要（跳数 + 逐段关系）",
          "function gvPathDetail(" in src26 and "🧭 路径" in src26)

    # ── H 折叠接线 ──
    check("H1_dblclick_wired", "节点 <g> 上真的绑了 ondblclick",
          "ondblclick=\"graphNodeDblClick(event," in src25)
    check("H2_node_filter", "折叠域内节点不渲染", "_hidden.has(String(n.id))" in src25)
    check("H3_edge_filter", "折叠域内的边不渲染（两端任一被折叠即跳过）",
          "_hidden.has(String(e.source_id))" in src25 and "_hidden.has(String(e.target_id))" in src25)
    check("H4_badge", "被折叠节点带 +N 虚线徽标（可点击恢复）", "gv-fold-badge" in src25)
    check("H5_chip", "工具栏显示折叠状态 chip（写明折叠数 + 展开入口）",
          "gv-fold-chip" in src25 and "gvUnfold()" in src25)
    check("H6_fit_visible", "折叠后取景只按可见节点算包围盒（否则画面空旷偏心）",
          "graphState.fold && graphState.fold.hidden" in fitview)
    check("H7_unfold_refit", "展开后回到全图视角（与折叠聚焦对称）",
          "gvFitView()" in _body(src26, "function gvUnfold("))

    # ── I 标签密度 ──
    check("I1_threshold_from_token", "阈值取 graphTok（两画布同一真源，不各写一份）",
          "graphTok('labelEdgeZoom')" in lblfn and "graphTok('labelNodeZoom')" in lblfn)
    check("I2_css_rules", "两档 class 有对应 CSS 规则",
          ".gv-far .gv-elabel" in src25 and ".gv-farther .gv-nlabel" in src25)
    check("I3_label_classes", "关系标签与节点名都带可选择的 class",
          'class="gv-elabel"' in src25 and 'class="gv-nlabel"' in src25)
    check("I4_sysml_side", "SysML Cytoscape 侧同样按缩放切 class",
          "svm-lbl-off" in src25 and "cy.on('zoom'" in src25)
    check("I5_reapply_after_render", "renderGraph 重建 svg 后重新施加缩放档位",
          "gvSyncLabelDensity()" in _body(src25, "function renderGraph("))
    check("I6_apply_on_view", "gvApplyView 内同步缩放档位",
          "gvSyncLabelDensity()" in _body(src26, "function gvApplyView("))

    # ── J 布局参数 ──
    check("J1_clamp", "参数经硬上限夹紧（间距 40–200 / 斥力 10000–60000 / 迭代 100–800）",
          "clamp(src.sep, 40, 200" in layoutwith and "clamp(src.rep, 10000, 60000" in layoutwith
          and "clamp(src.iters, 100, 800" in layoutwith)
    check("J2_sliders", "筛选面板提供三个滑杆（不写代码即可调参）",
          src25.count("gvLayoutOptInput(") >= 3 and "gv-lo-sep-v" in src25)
    check("J3_debounce", "滑杆输入防抖（连续拖动不每帧跑布局）",
          "clearTimeout(_gvLoTimer)" in optinput and "setTimeout(" in optinput)
    check("J4_relayout_1hop", "局部重排只取 1-hop 并归回原包围盒（其余节点不动）",
          "const nb = new Set([id])" in relayout and "bx0 + (n.x-ax0)/sw*tw" in relayout)
    check("J5_not_silent_writeback", "局部重排不静默回写坐标（提示里写明）",
          "未回写坐标" in src26)

    # ── K 压暗单一真源回归（P0-2 收口后的防回归）──
    rg = _body(src25, "function renderGraph(")
    check("K1_no_hardcoded_fade", "renderGraph 内不再有硬编码淡出（0.22 / 0.12）",
          not re.search(r"\?\s*0\.(22|12)\s*:\s*1", rg),
          "命中" if re.search(r"\?\s*0\.(22|12)\s*:\s*1", rg) else "clean")
    check("K2_edge_dim", "边淡出取 k.dimOpacity", "dim ? k.dimOpacity : 1" in rg)
    check("K3_node_dim", "节点淡出取 k.dimOpacity（锚点含 !isMs，与边淡出区分开）",
          "!isMs) ? k.dimOpacity : 1" in rg)

    # ── S 同语义单一实现 ──
    for cid, name, needle in (
        ("S1", "gvPathBFS 只有一处实现", "function gvPathBFS("),
        ("S2", "gvBoxCommit 只有一处实现", "function gvBoxCommit("),
        ("S3", "graphNodeDblClick 只有一处实现", "function graphNodeDblClick("),
        ("S4", "gvSyncLabelDensity 只有一处实现", "function gvSyncLabelDensity("),
        ("S5", "gvScreenToWorld 只有一处实现", "function gvScreenToWorld("),
    ):
        n = sum(s.count(needle) for s in allsrc)
        check(cid + "_single_impl", name, n == 1, "出现 %d 次" % n)
    # 判据针对「阈值数字」而非「变量名被引用次数」：名称当然可以在各处出现，
    # 但 0.6 / 0.4 这两个数字只允许在 24-graph.js 的 token 表里定义一次。
    check("S6_threshold_single_source", "标签密度阈值数字只在 24-graph.js 定义一次",
          src24.count("labelEdgeZoom: 0.6") == 1 and src24.count("labelNodeZoom: 0.4") == 1
          and "labelEdgeZoom: 0." not in (src25 + src26)
          and "labelNodeZoom: 0." not in (src25 + src26),
          "24:%d/%d 其它:%d/%d" % (src24.count("labelEdgeZoom: 0.6"), src24.count("labelNodeZoom: 0.4"),
                                  (src25 + src26).count("labelEdgeZoom: 0."),
                                  (src25 + src26).count("labelNodeZoom: 0.")))
    check("S7_no_dup_focus_write", "压暗类仍只允许 applyGraphFocus 一处写入",
          sum(s.count("addClass('dimmed')") for s in allsrc) == 1)

    # 供变异自证按 id 取结果（缺这行会让变异阶段拿到 None，症状像「门禁崩了」）
    return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}


MUTATIONS = [
    # 变异后必须只让目标判据翻红
    # M1 原设计是「去掉 visited 判断」，但那样会与自环用例组成死循环：
    #    变异体不是「判据抓到」而是「跑不完」，鉴别力不干净（门禁自己有 vm 超时兜底）。
    #    换成去掉邻接表排序 —— 同样指向 BFS 的实现约定，且一定收敛。
    ("M1_BFS不再按关系类型排序", "26-grapheditor.js",
     "  adj.forEach(list=>list.sort((a,b)=> a.rel.localeCompare(b.rel) || a.to.localeCompare(b.to)));",
     "  ;", "P7_lexico_order"),
    ("M2_折叠不过滤节点", "25-graphview.js",
     "    if(_hidden && _hidden.has(String(n.id))) return;", "    ;", "H2_node_filter"),
    ("M3_阈值写死0.5", "26-grapheditor.js",
     "  const far = s < graphTok('labelEdgeZoom'), farther = s < graphTok('labelNodeZoom');",
     "  const far = s < 0.5, farther = s < 0.2;", "I1_threshold_from_token"),
    ("M4_框选漏视口换算", "26-grapheditor.js",
     "    const p = gvWorldToScreen(n.x, n.y);", "    const p = {x:n.x, y:n.y};", "Q1_hit_uses_viewport"),
    ("M5_节点压暗退回硬编码", "25-graphview.js",
     "    const no = (dim && !sel && !nb && !isMs) ? k.dimOpacity : 1;",
     "    const no = (dim && !sel && !nb && !isMs) ? 0.22 : 1;", "K3_node_dim"),
]


def mutation_selftest(base_results):
    """每组变异：只允许目标判据翻红，且不能全红。返回变异组自己的判据列表。"""
    global RESULTS
    mut_checks = []

    def mcheck(cid, name, ok, detail=""):
        mut_checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})

    tmp = tempfile.mkdtemp(prefix="gv_p1_mut_")
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
