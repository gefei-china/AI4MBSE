/* 图谱 P0 渲染门禁的 Node 执行体（由 tools/verify/verify_graph_p0_render.py 调用）
 *
 * 纪律：判据**直接 exec 24-graph.js / 25-graphview.js 的真函数**，不复刻逻辑
 *      （复刻只能测零件，测不出装配）。
 * 用法：node graph_p0_harness.js <mods_dir>
 * 输出：最后一行 JSON {"checks":[{id,ok,detail}]}
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIR = process.argv[2] || path.resolve(__dirname, "../../static/js/mods");
const checks = [];
const rec = (id, ok, detail) => checks.push({ id, ok: !!ok, detail: detail === undefined ? "" : String(detail) });

function loadCtx(cssVars) {
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    setTimeout: () => 1,
    clearTimeout() {},
    getComputedStyle: () => ({ getPropertyValue: (n) => (cssVars || {})[n] || "" }),
    document: {
      documentElement: {},
      addEventListener() {},
      getElementById: () => null,
      querySelectorAll: () => [],
      createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {} } }),
      head: { appendChild() {} },
    },
    localStorage: { getItem: () => null, setItem() {} },
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  return ctx;
}

const SRC24 = fs.readFileSync(path.join(DIR, "24-graph.js"), "utf8");
// 26-grapheditor.js 只做静态取证（能力是否真实现），不在本上下文 exec（它依赖 DOM/async）
const SRC26 = fs.readFileSync(path.join(DIR, "26-grapheditor.js"), "utf8");
const SRC25 = fs.readFileSync(path.join(DIR, "25-graphview.js"), "utf8");

// ── A. P0-1 样式单一真源：真读 CSS 变量，且类型色由 token 派生 ──
try {
  const c1 = loadCtx({});                                  // CSS 变量取不到 ⇒ 走 fallback
  vm.runInContext(SRC24, c1, { filename: "24-graph.js" });
  rec("A1_fallback_eq_tokens_css", c1.graphTok("primary") === "#185FA5", c1.graphTok("primary"));

  const c2 = loadCtx({ "--graph-primary": "#ABCDEF", "--graph-amber": "#00FF00" });
  vm.runInContext(SRC24, c2, { filename: "24-graph.js" });
  rec("A2_reads_css_var", c2.graphTok("primary") === "#ABCDEF" && c2.graphTok("amber") === "#00FF00",
    c2.graphTok("primary") + "/" + c2.graphTok("amber"));

  rec("A3_type_color_from_token",
    c1.graphColor("系统需求") === c1.graphTok("primary") && c1.graphColor("部件") === c1.graphTok("teal"),
    c1.graphColor("系统需求") + "/" + c1.graphColor("部件"));

  // 未知类型仍走稳定色带（不落灰、不随 token 变）
  rec("A4_unknown_type_stable_palette",
    /^#[0-9A-F]{6}$/i.test(c1.graphColor("从未见过的类型XYZ")) &&
    c1.graphColor("从未见过的类型XYZ") === c1.graphColor("从未见过的类型XYZ"),
    c1.graphColor("从未见过的类型XYZ"));

  // 换品牌色 ⇒ 类型色跟着变（证明不是把 hex 抄了一份）
  rec("A5_brand_change_follows", c2.graphColor("系统需求") === "#ABCDEF", c2.graphColor("系统需求"));
} catch (e) {
  rec("A0_load_24", false, e.message);
}

// ── E. P0-1b 图谱色**不得**随 UI 主题漂移（真机实测缺陷的回归判据）──
// 图谱画布固定白纸面：暗色主题把 --color-primary 改成 #5b9ce6，
// 若 graphTok 读 --color-*，白底画布就会画出浅蓝（实测 tok #185FA5 vs 实际 #5b9ce6）。
try {
  const DARK = {          // 模拟 [data-theme="dark"] 覆盖后的整套变量
    "--color-primary": "#5b9ce6", "--color-primary-d": "#aac8f0", "--color-primary-l": "#1a3257",
    "--color-green": "#7cc564", "--color-amber": "#f0b75a", "--color-red": "#e57575",
    "--color-ink": "#e5e9f0", "--color-muted": "#8a96ad", "--color-line": "#2a3a55",
    "--graph-primary": "#185FA5", "--graph-primary-d": "#0C447C", "--graph-primary-l": "#E6F1FB",
    "--graph-green": "#3B6D11", "--graph-amber": "#BA7517", "--graph-red": "#A32D2D",
    "--graph-ink": "#1a2332", "--graph-muted": "#5a6678", "--graph-line": "#d5dce6",
  };
  const cd = loadCtx(DARK);
  vm.runInContext(SRC24, cd, { filename: "24-graph.js" });
  // 显式映射：token 键 → 暗色主题里被改写的变量名（这两列必须一一对上才测得出漂移）
  const PAIRS = [
    ["primary", "--color-primary"], ["primaryD", "--color-primary-d"], ["primaryL", "--color-primary-l"],
    ["green", "--color-green"], ["amber", "--color-amber"], ["red", "--color-red"],
    ["ink", "--color-ink"], ["muted", "--color-muted"], ["line", "--color-line"],
  ];
  const drift = PAIRS
    .filter(([k, cv]) => String(cd.graphTok(k)).toLowerCase() === String(DARK[cv] || "").toLowerCase())
    .map(([k]) => k);
  rec("E1_graph_color_ignores_theme", drift.length === 0, drift.join(",") || "none_drifted");
  rec("E1b_graph_primary_light", cd.graphTok("primary") === "#185FA5", cd.graphTok("primary"));

  // 只有 --color-* 被改、--graph-* 全缺 ⇒ 仍必须是浅色常量（不能退回主题色）
  const cOnly = loadCtx({ "--color-primary": "#5b9ce6", "--color-amber": "#f0b75a" });
  vm.runInContext(SRC24, cOnly, { filename: "24-graph.js" });
  rec("E2_no_graph_var_still_light",
    cOnly.graphTok("primary") === "#185FA5" && cOnly.graphTok("amber") === "#BA7517",
    cOnly.graphTok("primary") + "/" + cOnly.graphTok("amber"));
} catch (e) {
  rec("E0_load_24_theme", false, e.message);
}

// ── B/C/D：需要 24 + 25 同上下文 ──
let ctx = null;
try {
  ctx = loadCtx({});
  vm.runInContext(SRC24, ctx, { filename: "24-graph.js" });
  vm.runInContext(SRC25, ctx, { filename: "25-graphview.js" });
  vm.runInContext(
    "var graphState = {nodes:[], edges:[], sel:null, svgW:880, svgH:460," +
    " vp:{scale:1, panX:0, panY:0}, view:{layout:'layered', types:[], hideIsolated:false}," +
    " all:{nodes:[], edges:[]}, sk:{on:false, drill:null, focus:null}, total:0};",
    ctx, { filename: "state.js" });
} catch (e) {
  rec("B0_load_24_25", false, e.message);
}

// ── B. P0-3 拖拽增量重绘：只动被拖节点与其相邻边 ──
if (ctx) {
  try {
    const writes = {};
    const nodeEl = {
      getAttribute: (k) => (k === "data-nid" ? "n1" : null),
      setAttribute: (k, v) => { writes["node:" + k] = v; },
    };
    const mk = (tag) => ({ setAttribute: (k, v) => { writes[tag + ":" + k] = v; } });
    const e1Path = mk("path_e1"), e1Lbl = mk("label_e1"), e2Path = mk("path_e2");
    const svg = { querySelectorAll: () => [nodeEl] };
    ctx.document.getElementById = (id) => {
      if (id === "gv-svg") return svg;
      if (id === "gv-edge-e1") return e1Path;
      if (id === "gv-elabel-e1") return e1Lbl;
      if (id === "gv-edge-e2") return e2Path;
      return null;
    };
    vm.runInContext(
      "graphState.nodes=[{id:'n1',x:10,y:10},{id:'n2',x:100,y:10},{id:'n3',x:10,y:100}];" +
      "graphState.edges=[{id:'e1',source_id:'n1',target_id:'n2'},{id:'e2',source_id:'n2',target_id:'n3'}];" +
      "graphState._edgeGeom={'e1':{a:'n1',b:'n2',off:0.22},'e2':{a:'n2',b:'n3',off:0.22}};" +
      "graphState.nodes[0].x=200; graphState.nodes[0].y=200; gvPatchNode('n1');",
      ctx, { filename: "patch.js" });

    rec("B1_node_transform_patched", /translate\(200,\s*200\)/.test(writes["node:transform"] || ""),
      writes["node:transform"]);
    rec("B2_adjacent_edge_repainted", /^M 200 200/.test(String(writes["path_e1:d"] || "")),
      writes["path_e1:d"]);
    rec("B3_edge_label_moved", writes["label_e1:x"] !== undefined && writes["label_e1:y"] !== undefined,
      writes["label_e1:x"] + "," + writes["label_e1:y"]);
    rec("B4_nonadjacent_edge_untouched", Object.keys(writes).every((k) => !k.startsWith("path_e2")),
      Object.keys(writes).join("|"));
  } catch (e) {
    rec("B5_patch_runtime", false, e.message);
  }

  // ── C. P0-2 统一邻域高亮 ──
  // ⚠ 桩必须与真实 Cytoscape API 一致：closedNeighborhood 只存在于 **collection/元素**，
  //    core 上**没有**。桩若自带该方法，就会把「误用 core 方法判型」这类缺陷测成绿（2026-11-28 实测）。
  try {
    const calls = [];
    const nb = { __nb: 1 };
    const others = { addClass: (c) => calls.push("others+" + c) };
    // 元素 collection（真实 API：有 closedNeighborhood / addClass / length）
    const el = { length: 1, addClass: (c) => calls.push("el+" + c), closedNeighborhood: () => nb };
    // core 桩（真实 API：elements/nodes/edges/getElementById —— 故意不给 closedNeighborhood）
    const cyStub = {
      nodes: () => [],
      edges: () => [],
      elements: () => ({
        removeClass: (c) => calls.push("rm:" + c),
        not: (x) => { calls.push("not:" + (x === nb ? "nb" : "other")); return others; },
      }),
      getElementById: (i) => (i === "n1" ? el : { length: 0 }),
    };
    ctx.__cy = cyStub;
    rec("C0_stub_matches_real_api",
      typeof cyStub.closedNeighborhood !== "function" && typeof cyStub.elements === "function",
      "core.closedNeighborhood=" + typeof cyStub.closedNeighborhood);

    vm.runInContext("applyGraphFocus(__cy, 'n1');", ctx, { filename: "focus1.js" });
    const focusText = calls.join(" ; ");
    rec("C1_focus_dims_non_neighbors",
      calls.includes("not:nb") && calls.includes("others+dimmed") && calls.includes("el+focus-hi"),
      focusText);
    rec("C2_focus_clears_first", calls.indexOf("rm:dimmed focus-hi") === 0, focusText);

    calls.length = 0;
    vm.runInContext("applyGraphFocus(__cy, null);", ctx, { filename: "focus2.js" });
    rec("C3_empty_click_clears_only",
      calls.includes("rm:dimmed focus-hi") && !calls.some((c) => c.startsWith("others+") || c.startsWith("el+")),
      calls.join(" ; "));

    // C5：非 Cytoscape 目标不得误入 Cytoscape 分支（防「一律当 cy」的反向退化）
    calls.length = 0;
    vm.runInContext("applyGraphFocus('svg', 'n1');", ctx, { filename: "focus3.js" });
    rec("C5_non_cy_target_not_treated_as_cy", !calls.some((c) => c.startsWith("rm:") || c.startsWith("others+")),
      calls.join(" ; ") || "no_cy_calls");
  } catch (e) {
    rec("C4_focus_runtime", false, e.message);
  }

  // ── D. P0-4 统一交互提示 ──
  try {
    const kg = ctx.gvHintBar("kg"), svm = ctx.gvHintBar("sysml"), imp = ctx.gvHintBar("impact");
    rec("D1_hint_three_modes",
      kg.includes('data-mode="kg"') && svm.includes('data-mode="sysml"') && imp.includes('data-mode="impact"'),
      [kg.length, svm.length, imp.length].join("/"));
    rec("D2_hint_tells_blank_click_cancels",
      kg.includes("点空白取消") && svm.includes("点空白取消") && imp.includes("点空白取消"));
    /* 不得预告未落地能力 —— 判据形式：**提示里出现某个能力词，就必须有对应实现**。
       原判据是"禁止出现框选/双击"（把 P1 未做的事实写死），P1 落地后会自己变假红；
       现在改成「出现即要求实现存在」，判据随能力落地自动保持有效。 */
    const CAP_EVIDENCE = [
      ["框选", /function gvBoxCommit\(/],
      ["双击", /function graphNodeDblClick\(/],
      ["隐藏关系标签", /function gvSyncLabelDensity\(/],
      ["路径", /function gvPathBFS\(/],
    ];
    const unimplemented = CAP_EVIDENCE
      .filter(([word, re]) => [kg, svm, imp].some((h) => h.includes(word)) && !re.test(SRC26))
      .map(([word]) => word);
    rec("D3_hint_no_unimplemented_features", unimplemented.length === 0,
      unimplemented.join(",") || "all_hinted_caps_implemented");
    rec("D4_hint_fallback", ctx.gvHintBar("unknown").includes('data-mode="kg"'));
  } catch (e) {
    rec("D5_hint_runtime", false, e.message);
  }
}

process.stdout.write(JSON.stringify({ checks: checks }) + "\n");
