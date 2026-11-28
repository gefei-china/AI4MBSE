# -*- coding: utf-8 -*-
"""门禁：图谱节点尺寸与聚焦（节点加大 / 选中放大 / 选中自动居中）。

背景（2026-11-28 米爸反馈）："图谱中的节点有点小，选择节点后，被选择节点更大些，
且自动移到视图中间，以便查看。"

这条反馈背后有三个层次，门禁逐层钉住（否则改一处会被另一处抵消）：
  ① 半径：13–26 → 17–33（且全部走 graphTok，不写死数字）
  ② 选中放大：nodeSelScale = 1.32（选中要真的变大，不只是描边变粗）
  ③ 选中居中：居中 + **缩放下限** —— 只居中不提缩放没用：整图 fit 后 scale 常只有
     0.5~0.6，居中后屏幕直径仍不足 25px，等于没解决"看不清"
  ④ 根因：svgH 原公式"每 4 节点一排"与画布宽度无关 ⇒ 61 节点算出 1280 高、
     fit 只能压到 0.6。改为按密度摊派后 scale≈1.05

判据分组：
  N 聚焦（真跑 gvRevealNode：低缩放拉到下限并居中 / 已放大不缩回 / 未知节点不动 /
          无坐标不动 / pan 随 scale 变化）
  U 单击与拖拽分流（真跑 graphSvgUp：单击居中且不写库 / 拖拽写库且不居中 /
          2px 抖动算单击 / >2px 算拖拽 / 框选提交优先）
  R 半径公式（base / 度数加成 / 加成上限 / 骨架 / 选中放大 / 系数 >1 / 公式在源码里）
  C 碰撞（真跑 graphCollide：完全重合被推开 / 确定性 / 单次达 minD / 远处不误动）
  S 结构与单一实现（尺寸集中 token / 渲染处不写死半径 / 居中只在 up 不在 down /
          画布高度按密度且用宽度 / 碰撞 token 化 / 类型文字门槛 token 化）
  M 变异自证（5 组，每组只允许目标判据翻红，不得全红）

用法：python tools/verify/verify_graph_p3_focus.py
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
_HARNESS = os.path.join(_ROOT, "tools", "verify", "_graph_p3_harness.js")

RESULTS = []
FILES = ("24-graph.js", "25-graphview.js", "26-grapheditor.js")

EXPECTED_HARNESS = {
    "N1_low_scale_raised_and_centered", "N1b_applied_once", "N2_never_zoom_out",
    "N3_unknown_noop", "N4_null_coord_noop", "N5_scale_in_pan",
    "U1_click_centers", "U2_click_no_write", "U3_click_rerenders",
    "U4_drag_saves", "U5_drag_no_center", "U6_jitter_is_click",
    "U7_small_drag_is_drag", "U8_box_short_circuits",
    "R1_base_radius", "R2_sel_enlarged", "R3_degree_growth", "R4_degree_capped",
    "R5_skel_radius", "R6_sel_scale_gt_1", "R7_formula_single_impl",
    "C1_coincident_pushed", "C2_deterministic", "C3_reaches_minD", "C4_no_false_move",
    "C5_radius_aware", "C6_returns_moved_count", "C7_zero_when_converged",
    "W1_all_coincident_settled", "W2_near_coincident_settled", "W3_dense_settled",
    "W4_idempotent", "W5_degenerate_ok",
    "L1_ring_settles", "L2_layered_settles", "L3_force_settles", "L4_no_per_node_clamp",
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


def _code_only(src: str) -> str:
    """剔注释行：注释里提到函数名会被当成真调用（P0 的 S2 假红就是这么来的）。"""
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        out.append(ln)
    return "\n".join(out)


def run_harness(mods_dir: str):
    try:
        p = subprocess.run(["node", _HARNESS, mods_dir], capture_output=True, text=True, timeout=180)
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
    src24 = _read(mods_dir, "24-graph.js")
    src25 = _read(mods_dir, "25-graphview.js")
    src26 = _read(mods_dir, "26-grapheditor.js")
    allsrc = (src24, src25, src26)

    up_body = _body(src26, "function graphSvgUp(")
    down_body = _body(src26, "function graphNodeDown(")
    reveal_body = _body(src25, "function gvRevealNode(")
    layout_body = _body(src24, "function graphLayout(")
    collide_body = _body(src24, "function graphCollide(")

    # ── N/U/R/C：真函数执行 ──
    harness = run_harness(mods_dir)
    missing = sorted(EXPECTED_HARNESS - set(harness))
    check("H1_harness_complete", "Node 执行体产出全部预期判据（不允许静默少跑）",
          not missing,
          ("缺:" + ",".join(missing)) if missing else "%d 条齐备" % len(EXPECTED_HARNESS))
    for cid, (ok, detail) in harness.items():
        check(cid, "（Node 实测）%s" % cid, ok, detail)
    r0 = sorted([k for k in harness if re.match(r"^[NURC]0_", k)])
    if r0:
        check("H2_harness_no_runtime_error", "Node 执行体无运行期异常",
              all(harness[k][0] for k in r0), ",".join(r0))

    # ── S 组：结构与单一实现 ──
    tok_keys = ("nodeBaseR", "nodeDegStep", "nodeDegCap", "skelBaseR", "skelCountStep",
                "skelCountCap", "typeTextMinR", "nodeSelScale", "nodeFocusScale",
                "nodeCollideMinD")
    absent = [k for k in tok_keys if (k + ":") not in src24]
    check("S1_size_tokens_declared", "尺寸/聚焦参数全部在 24-graph.js 的 token 表里声明",
          not absent, ("缺:" + ",".join(absent)) if absent else "%d 项齐备" % len(tok_keys))

    # 渲染处不得再出现写死的旧半径公式（13 + ... / 20 + ...）
    stale = [pat for pat in (r"13 \+ Math\.min", r"20 \+ Math\.min", r"n\._skel \? 20")
             if re.search(pat, _code_only(src25))]
    check("S2_no_hardcoded_radius", "25-graphview.js 不再有写死的半径公式（改 token 后才换得动）",
          not stale, ",".join(stale) or "none")

    # gvNodeRadius 已下移到 24-graph.js（布局层也要按半径算碰撞距离）
    radius_body = _body(src24, "function gvNodeRadius(")
    check("S3_sel_scales_radius", "选中放大作用在半径上（不是只加粗描边），且渲染处调用该函数",
          "graphTok('nodeSelScale')" in radius_body
          and re.search(r"sel \? Math\.round\(rBase \* graphTok\('nodeSelScale'\)\)", radius_body) is not None
          and "const r = gvNodeRadius(n, deg, sel);" in _code_only(src25),
          "radius=%s call=%s" % ("graphTok('nodeSelScale')" in radius_body,
                                 "gvNodeRadius(n, deg, sel)" in _code_only(src25)))

    # 居中必须在 up，不能出现在 down（down 里居中会把紧随的拖拽坐标算错 → 节点跳飞）
    check("S4_center_in_up_not_down", "居中调用在 mouseup，不在 mousedown，也不在 mousemove",
          "gvRevealNode(" in _code_only(up_body)
          and "gvRevealNode(" not in _code_only(down_body)
          and "gvRevealNode(" not in _code_only(_body(src26, "function graphSvgMove(")),
          "up=%s down=%s" % ("gvRevealNode(" in _code_only(up_body),
                             "gvRevealNode(" in _code_only(down_body)))

    check("S5_click_vs_drag", "up 里用「按下坐标快照」区分单击与拖拽",
          "sx0" in _code_only(down_body) and "sx0" in _code_only(up_body), "sx0 双向命中")

    check("S6_reveal_has_scale_floor", "gvRevealNode 同时做缩放拉到下限与平移居中",
          "nodeFocusScale" in reveal_body and "vw/2-s*n.x" in reveal_body.replace(" ", ""),
          "floor=%s pan=%s" % ("nodeFocusScale" in reveal_body, "vw/2-s*n.x" in reveal_body.replace(" ", "")))

    check("S7_reveal_never_zoom_out", "gvRevealNode 只放大不缩小（Math.max(当前, 下限)）",
          re.search(r"Math\.max\(s0,\s*graphTok\('nodeFocusScale'\)\)", reveal_body) is not None,
          "max 命中")

    # 画布高度：必须用画布宽度参与计算（原公式与宽度无关 ⇒ 高得离谱 ⇒ 缩放被压小）
    check("S8_canvas_h_uses_width", "画布高度按密度并用画布宽度摊派（原公式与宽度无关）",
          "graphState.svgW" in layout_body and re.search(r"nodes\.length\s*\*\s*_cell", layout_body) is not None,
          "svgW=%s cell=%s" % ("graphState.svgW" in layout_body, "_cell" in layout_body))

    check("S9_collide_from_token", "碰撞最小距不再写死 44/46（节点变大后必须跟着上调）",
          bool(re.search(r"graphCollide\(nodes,\s*graphTok\('nodeCollideMinD'\)", _code_only(src24)))
          and not re.search(r"graphCollide\(nodes,\s*4[46]\)", _code_only(src24)),
          "token=%s 旧值残留=%s" % (
              bool(re.search(r"graphCollide\(nodes,\s*graphTok\('nodeCollideMinD'\)", _code_only(src24))),
              bool(re.search(r"graphCollide\(nodes,\s*4[46]\)", _code_only(src24)))))

    check("S10_collide_coincident_fix", "碰撞对完全重合给了确定方向（否则永远推不开）",
          "1e-6" in collide_body and "Math.cos" in collide_body and "Math.random" not in collide_body,
          "1e-6=%s cos=%s random=%s" % ("1e-6" in collide_body, "Math.cos" in collide_body,
                                        "Math.random" in collide_body))

    check("S11_type_text_token", "圆内类型文字门槛也走 token（不写死 16）",
          "graphTok('typeTextMinR')" in _code_only(src25), "命中")

    check("S12_reveal_single_impl", "gvRevealNode 只有一处实现",
          sum(s.count("function gvRevealNode(") for s in allsrc) == 1)

    check("S14_radius_single_impl", "半径公式只有一处实现（在 24，布局层与渲染层共用）",
          sum(x.count("function gvNodeRadius(") for x in allsrc) == 1
          and "function gvNodeRadius(" in src24 and "function gvNodeRadius(" not in src25
          and "const r = gvNodeRadius(n, deg, sel);" in _code_only(src25),
          "impl=%d in24=%s dup25=%s" % (sum(x.count("function gvNodeRadius(") for x in allsrc),
                                        "function gvNodeRadius(" in src24,
                                        "function gvNodeRadius(" in src25))

    # ── 2026-11-28 彻底解决遗留重叠：结构与单一实现 ──
    settle_body = _body(src24, "function graphSettleOverlaps(")
    collide_body2 = _body(src24, "function graphCollide(")
    check("S15_settle_single_impl", "收敛式消除只有一处实现（graphSettleOverlaps）",
          sum(x.count("function graphSettleOverlaps(") for x in allsrc) == 1,
          str(sum(x.count("function graphSettleOverlaps(") for x in allsrc)))

    check("S16_collide_radius_aware", "碰撞判定按「半径和 + 间隙」，不是固定 minD",
          "radiusOf(nodes[i]) + radiusOf(nodes[j]) + gap" in collide_body2,
          "radius-aware=%s" % ("radiusOf(nodes[i]) + radiusOf(nodes[j]) + gap" in collide_body2))

    check("S17_settle_translate_not_clamp", "边界用整体平移（不改变间距），不用逐点夹紧",
          "nodes.forEach(n=>{ n.x += dx; n.y += dy; })" in settle_body
          and "Math.max(pad, Math.min(W-pad" not in settle_body
          and "Math.min(Wc" not in settle_body,
          "translate=%s perNodeClamp=%s" % ("n.x += dx" in settle_body,
                                            "Math.min(W-pad" in settle_body))

    check("S18_settle_iterates_to_converge", "迭代到 collide 返回 0 才停（不靠固定轮数）",
          "graphCollide(nodes, opt) === 0" in settle_body, "converge-check=%s" % ("=== 0" in settle_body))

    check("S19_settle_expands_canvas", "画布塞不下时扩大画布，而不是把节点硬夹回去",
          "expanded++" in settle_body and "expanded" in settle_body, "expanded-tracked=%s" % ("expanded++" in settle_body))

    check("S20_layout_reports_overlaps", "布局把终态重叠数写进 state（排障与门禁可读）",
          "graphState._layoutOverlaps" in _code_only(src24), "命中")

    check("S13_old_center_kept", "既有 gvCenterOnNode 未被替换（树定位等调用方行为不变）",
          "function gvCenterOnNode(" in src25 and "function gvRevealNode(" in src25,
          "both present")

    return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}


MUTATIONS = [
    ("M1_居中挪到mousedown", "26-grapheditor.js",
     "  graphState.multiSel = new Set();   // P1-1：单选与多选互斥，点节点即退出框选态",
     "  graphState.multiSel = new Set(); gvRevealNode(id);",
     "S4_center_in_up_not_down"),
    ("M2_选中不再放大", "24-graph.js",
     "return sel ? Math.round(rBase * graphTok('nodeSelScale')) : rBase;",
     "return rBase;",
     "R2_sel_enlarged"),
    ("M3_不区分单击与拖拽", "26-grapheditor.js",
     "    const moved = !!n && (Math.abs(n.x - d.sx0) > 2 || Math.abs(n.y - d.sy0) > 2);",
     "    const moved = false;",
     "U4_drag_saves"),
    ("M4_重合不再给方向", "24-graph.js",
     "        ux = Math.cos(a); uy = Math.sin(a);",
     "        ux = 0; uy = 0;",
     "C1_coincident_pushed"),
    ("M5_居中不提缩放", "25-graphview.js",
     "  const s = Math.max(s0, graphTok('nodeFocusScale'));",
     "  const s = s0;",
     "N1_low_scale_raised_and_centered"),
    # ── 2026-11-28 彻底解决遗留重叠：这三条锁住新能力 ──
    ("M6_碰撞退回固定minD", "24-graph.js",
     "    const need = radiusOf ? Math.max(minD, radiusOf(nodes[i]) + radiusOf(nodes[j]) + gap) : minD;",
     "    const need = minD;",
     "C5_radius_aware"),
    ("M7_分层分支不消除重叠", "24-graph.js",
     "    if(!gvLayoutWith('dagre', nodes, edges)) graphLayeredPos(nodes, edges, W, H);\n    gvNormalize(nodes, W, H);",
     "    if(!gvLayoutWith('dagre', nodes, edges)) graphLayeredPos(nodes, edges, W, H);\n    gvNormalize(nodes, W, H); return;",
     "L2_layered_settles"),
    ("M8_收敛改成单轮", "24-graph.js",
     "      if(graphCollide(nodes, opt) === 0) break;   // 无对可推 ⇒ 收敛",
     "      graphCollide(nodes, opt); break;",
     "W2_near_coincident_settled"),
]


def mutation_selftest(base_results):
    mut_checks = []

    def mcheck(cid, name, ok, detail=""):
        mut_checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})

    tmp = tempfile.mkdtemp(prefix="gv_p3_mut_")
    try:
        for f in FILES:
            p = os.path.join(_MODS, f)
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(tmp, f))
        for title, fname, old, new, target in MUTATIONS:
            src = _read(_MODS, fname)
            if src.count(old) != 1:
                mcheck("M_anchor:" + target, "变异锚点唯一（%s）" % title, False,
                       "count=%d" % src.count(old))
                continue
            mutated = src.replace(old, new, 1)
            if mutated == src:   # 变异没打中 ⇒ 后面「翻红」的结论无效
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
        RESULTS = list(base_results)
    return mut_checks


def main() -> int:
    global RESULTS
    evaluate(_MODS)
    base_results = list(RESULTS)
    if any(not r["ok"] for r in base_results):
        # 基线就红时不做变异（否则「变异后更红」没有对照意义）
        for r in base_results:
            if not r["ok"]:          # 必须过滤：不过滤会把全绿项也打成 FAIL，误导出「12 个红」的假象
                print("FAIL [%s] %s :: %s" % (r["id"], r["name"], r["detail"][:220]))
        print("\n合计 %d 项，绿 %d，红 %d（基线未绿，跳过变异自证）"
              % (len(base_results), sum(1 for r in base_results if r["ok"]),
                 sum(1 for r in base_results if not r["ok"])))
        return 1
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
