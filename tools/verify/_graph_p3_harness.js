/* 图谱 P3 门禁的 Node 执行体（由 tools/verify/verify_graph_p3_focus.py 调用）
 *
 * 主题（2026-11-28 米爸反馈）：节点太小 / 选中应更大 / 选中后自动移到视图中间。
 * 纪律：从 24-graph.js / 25-graphview.js / 26-grapheditor.js **原文抽取函数**后在 vm 执行。
 * 用法：node _graph_p3_harness.js <mods_dir>
 * 输出：最后一行 JSON {"checks":[{id,name,ok,detail}]}
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIR = process.argv[2] || path.resolve(__dirname, "../../static/js/mods");
const SRC24 = fs.readFileSync(path.join(DIR, "24-graph.js"), "utf8");
const SRC25 = fs.readFileSync(path.join(DIR, "25-graphview.js"), "utf8");
const SRC26 = fs.readFileSync(path.join(DIR, "26-grapheditor.js"), "utf8");
const checks = [];
/* 签名 (id, name, ok, detail)。P1 曾因 rec 只收 3 参把标题当 ok（恒真）⇒ 假绿。 */
const rec = (id, name, ok, detail) =>
  checks.push({ id, name, ok: !!ok, detail: detail === undefined ? "" : String(detail) });

function extractFn(src, name) {
  const header = "function " + name + "(";
  const i = src.indexOf(header);
  if (i < 0) throw new Error("fn not found: " + name);
  let j = src.indexOf("{", i), depth = 0, k = j;
  for (; k < src.length; k++) {
    const c = src[k];
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) { k++; break; } }
  }
  return src.slice(i, k);
}

/* ── 最小 DOM 桩：只实现被测函数真正用到的成员 ── */
function mkCtx(extra) {
  const calls = { applied: 0, rendered: 0, saved: [], toast: [] };
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    Math, JSON, Number, String, Boolean, Array, Object, Set, Map, isFinite, parseFloat, parseInt,
    // 令牌表：与 24-graph.js 中的 fallback 同值，便于断言「实现不写死数字」
    __tok: {
      nodeBaseR: 17, nodeDegStep: 2.0, nodeDegCap: 16,
      skelBaseR: 26, skelCountStep: 1.0, skelCountCap: 16,
      typeTextMinR: 21, nodeSelScale: 1.32, nodeFocusScale: 0.9, nodeCollideMinD: 56,
    },
    graphState: {
      nodes: [], edges: [], view: {}, vp: { scale: 1, panX: 0, panY: 0 },
      sel: null, drag: null, pan: null, box: null, focusNid: null,
      multiSel: new Set(), fold: null, path: null, svgW: 880, svgH: 460,
    },
    gvApplyView: () => { calls.applied++; },
    renderGraph: () => { calls.rendered++; },
    toast: (m) => calls.toast.push(String(m)),
    showGraphDetail: () => {},
    saveNodePos: (n) => calls.saved.push(String(n && n.id)),
    isReleaseBranch: () => false,
    getCurrentBranch: () => "personal",
    gvBoxCommit: () => {},
    gvPatchNodeSoon: () => {},
    gvRevealNode: (id) => { calls.revealed = String(id); },
    document: {
      getElementById: () => null,
      querySelectorAll: () => [],
      createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} } }),
    },
  };
  ctx.graphTok = (n) => (n ? ctx.__tok[n] : ctx.__tok);
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.__calls = calls;
  Object.assign(ctx, extra || {});
  vm.createContext(ctx);
  return ctx;
}

/* ══════════ N 组：gvRevealNode（居中 + 缩放下限） ══════════ */
try {
  const ctx = mkCtx();
  vm.runInContext(extractFn(SRC25, "gvRevealNode"), ctx, { filename: "f:gvRevealNode" });
  const gs = ctx.graphState;
  const svg = { clientWidth: 848, clientHeight: 619 };
  ctx.document.getElementById = (id) => (id === "gv-svg" ? svg : null);
  gs.nodes = [{ id: "n1", x: 300, y: 800 }, { id: "n2", x: 100, y: 100 }];

  // ① 低缩放 ⇒ 拉回下限 nodeFocusScale，且 pan 落到「节点居中」
  gs.vp = { scale: 0.55, panX: 0, panY: 0 };
  const ok1 = ctx.gvRevealNode("n1");
  const s1 = gs.vp.scale;
  /* 预期值不能取整：实现是直接赋值 619/2 - s*y = -410.5，
     而 Math.round(-410.5) 在 JS 里是 -410（向 +∞ 取整）⇒ 断言会误判。 */
  const expPanX = 848 / 2 - s1 * 300;
  const expPanY = 619 / 2 - s1 * 800;
  rec("N1_low_scale_raised_and_centered", "低缩放时拉到下限并居中（pan 按新 scale 算）",
    ok1 === true && Math.abs(s1 - 0.9) < 1e-9
      && Math.abs(gs.vp.panX - expPanX) < 0.01 && Math.abs(gs.vp.panY - expPanY) < 0.01,
    `scale=${s1} pan=(${gs.vp.panX},${gs.vp.panY}) exp=(${expPanX},${expPanY})`);
  rec("N1b_applied_once", "居中后触发一次视图应用（否则画布不会真的动）",
    ctx.__calls.applied === 1, String(ctx.__calls.applied));

  // ② 已放大 ⇒ 不得缩回（不抢用户的缩放决定）
  gs.vp = { scale: 1.8, panX: 0, panY: 0 };
  ctx.gvRevealNode("n2");
  rec("N2_never_zoom_out", "已放大时点节点不被缩回（只放大不缩小）",
    Math.abs(gs.vp.scale - 1.8) < 1e-9, String(gs.vp.scale));

  // ③ 节点不存在 / 无坐标 ⇒ 返回 false 且不动 vp
  gs.vp = { scale: 0.4, panX: 7, panY: 9 };
  const ok3 = ctx.gvRevealNode("ghost");
  rec("N3_unknown_noop", "节点不存在时不改 vp 且返回 false",
    ok3 === false && gs.vp.scale === 0.4 && gs.vp.panX === 7, String(ok3));
  gs.nodes.push({ id: "nz", x: null, y: null });
  const ok4 = ctx.gvRevealNode("nz");
  rec("N4_null_coord_noop", "节点无坐标时不动 vp（避免 NaN 传染到 transform）",
    ok4 === false && gs.vp.scale === 0.4, String(ok4));

  // ⑤ 居中目标必须用「节点世界坐标 × scale」，不能漏 scale
  gs.nodes = [{ id: "m1", x: 200, y: 100 }];
  gs.vp = { scale: 1.0, panX: 0, panY: 0 };
  ctx.gvRevealNode("m1");
  const panX1 = gs.vp.panX;
  gs.vp = { scale: 2.0, panX: 0, panY: 0 };
  ctx.gvRevealNode("m1");
  rec("N5_scale_in_pan", "不同缩放下 pan 随 scale 变化（漏乘 scale 会得到同一个 pan）",
    Math.abs((panX1) - (848 / 2 - 1 * 200)) < 0.01
      && Math.abs(gs.vp.panX - (848 / 2 - 2 * 200)) < 0.01,
    `pan@1x=${panX1} pan@2x=${gs.vp.panX}`);
} catch (e) {
  rec("N0_runtime", "N 组可执行", false, e.message);
}

/* ══════════ M 组：graphSvgUp 的「单击 vs 拖拽」分流 ══════════ */
try {
  const ctx = mkCtx();
  vm.runInContext(extractFn(SRC26, "graphSvgUp"), ctx, { filename: "f:graphSvgUp" });
  const gs = ctx.graphState;

  // ① 单击（位移 0）：不写库 + 触发居中
  gs.nodes = [{ id: "a", x: 100, y: 100 }];
  gs.drag = { id: "a", ox: 0, oy: 0, sx0: 100, sy0: 100 };
  ctx.__calls.saved.length = 0;
  ctx.__calls.revealed = null;
  ctx.graphSvgUp({});
  rec("U1_click_centers", "单击（位移 0）触发居中", ctx.__calls.revealed === "a", String(ctx.__calls.revealed));
  rec("U2_click_no_write", "单击不写坐标（原来每次单击都发一次 PUT）",
    ctx.__calls.saved.length === 0, ctx.__calls.saved.join(","));
  rec("U3_click_rerenders", "单击后仍统一刷新一次（选中态/光晕）",
    ctx.__calls.rendered === 1 && gs.drag === null, `rendered=${ctx.__calls.rendered} drag=${gs.drag}`);

  // ② 真拖拽（位移 60）：写库 + **不**居中
  gs.nodes = [{ id: "b", x: 160, y: 100 }];
  gs.drag = { id: "b", ox: 0, oy: 0, sx0: 100, sy0: 100 };
  ctx.__calls.saved.length = 0;
  ctx.__calls.revealed = null;
  ctx.__calls.rendered = 0;
  ctx.graphSvgUp({});
  rec("U4_drag_saves", "真拖拽写坐标", ctx.__calls.saved.join(",") === "b", ctx.__calls.saved.join(","));
  rec("U5_drag_no_center", "真拖拽**不**居中（用户刚摆好的位置不该被抢走）",
    ctx.__calls.revealed === null, String(ctx.__calls.revealed));

  // ③ 阈值：位移 1.5px 视为单击（手抖不应算拖拽）
  gs.nodes = [{ id: "c", x: 101.5, y: 100 }];
  gs.drag = { id: "c", ox: 0, oy: 0, sx0: 100, sy0: 100 };
  ctx.__calls.saved.length = 0;
  ctx.__calls.revealed = null;
  ctx.graphSvgUp({});
  rec("U6_jitter_is_click", "位移 ≤2px 视为单击（手抖不算拖拽）",
    ctx.__calls.revealed === "c" && ctx.__calls.saved.length === 0,
    `revealed=${ctx.__calls.revealed} saved=${ctx.__calls.saved.length}`);

  // ④ 位移 3px 视为拖拽
  gs.nodes = [{ id: "d", x: 103.5, y: 100 }];
  gs.drag = { id: "d", ox: 0, oy: 0, sx0: 100, sy0: 100 };
  ctx.__calls.saved.length = 0;
  ctx.__calls.revealed = null;
  ctx.graphSvgUp({});
  rec("U7_small_drag_is_drag", "位移 >2px 视为拖拽（写库、不居中）",
    ctx.__calls.saved.join(",") === "d" && ctx.__calls.revealed === null,
    `saved=${ctx.__calls.saved.join(",")} revealed=${ctx.__calls.revealed}`);

  // ⑤ 框选提交优先：box 存在时直接 return，不进入拖拽分支
  const ctx2 = mkCtx({ gvBoxCommit: () => { ctx2.__calls.boxCommitted = true; } });
  vm.runInContext(extractFn(SRC26, "graphSvgUp"), ctx2, { filename: "f:graphSvgUp2" });
  ctx2.graphState.box = { any: 1 };
  ctx2.graphState.drag = { id: "e", ox: 0, oy: 0, sx0: 0, sy0: 0 };
  ctx2.graphSvgUp({});
  rec("U8_box_short_circuits", "框选提交优先（不误入拖拽/居中）",
    ctx2.__calls.boxCommitted === true && ctx2.__calls.revealed === undefined,
    `box=${ctx2.__calls.boxCommitted} revealed=${ctx2.__calls.revealed}`);
} catch (e) {
  rec("U0_runtime", "U 组可执行", false, e.message);
}

/* ══════════ R 组：半径公式（选中放大 / 骨架 / 类型文字门槛） ══════════ */
try {
  const ctx = mkCtx();
  /* exec **真函数** gvNodeRadius（不是复刻公式）。
     第一版是按「同一表达式」在测试里重写了一遍 —— 变异「去掉选中放大」后判据仍绿，
     因为改的是源码、测试里那份没变。这是 P0「桩照抄错误假设」的同族错误。 */
  /* gvNodeRadius 在 24-graph.js（2026-11-28 从 25 下移）：
     布局层 graphCollide / graphSettleOverlaps 也要按半径算碰撞距离，必须共用同一实现。 */
  vm.runInContext(extractFn(SRC24, "gvNodeRadius"), ctx, { filename: "f:gvNodeRadius" });
  const expr = (n, deg, sel) => ({ r: ctx.gvNodeRadius(n, deg, sel) });
  // rBase 单独取：把 sel 传 false 再算一次，即为未放大半径
  const rBaseOf = (n, deg) => ctx.gvNodeRadius(n, deg, false);

  const t = (n) => ctx.__tok[n];
  const r0 = { rBase: rBaseOf({ id: "a" }, {}), r: ctx.gvNodeRadius({ id: "a" }, {}, false) };
  const r0sel = expr({ id: "a" }, {}, true);
  rec("R1_base_radius", "无关系节点半径 = nodeBaseR",
    r0.rBase === t("nodeBaseR"), `${r0.rBase} vs ${t("nodeBaseR")}`);
  rec("R2_sel_enlarged", "选中半径 = round(base × nodeSelScale)",
    r0sel.r === Math.round(t("nodeBaseR") * t("nodeSelScale")),
    `${r0sel.r} vs ${Math.round(t("nodeBaseR") * t("nodeSelScale"))}`);

  const rdeg = { rBase: rBaseOf({ id: "b" }, { b: 3 }) };
  rec("R3_degree_growth", "半径随度数增长（3 条关系 = base + 3×step）",
    rdeg.rBase === t("nodeBaseR") + 3 * t("nodeDegStep"), String(rdeg.rBase));
  const rcap = { rBase: rBaseOf({ id: "c" }, { c: 99 }) };
  rec("R4_degree_capped", "度数加成有上限（不会无限变大）",
    rcap.rBase === t("nodeBaseR") + t("nodeDegCap"), String(rcap.rBase));

  const rskel = { rBase: rBaseOf({ id: "d", _skel: 1, _count: 5 }, {}) };
  rec("R5_skel_radius", "骨架聚合节点用 skelBaseR + 计数加成",
    rskel.rBase === t("skelBaseR") + 5 * t("skelCountStep"), String(rskel.rBase));

  // 选中的放大必须「真的变大」而不是变小
  rec("R6_sel_scale_gt_1", "nodeSelScale > 1（选中必须变大）", t("nodeSelScale") > 1, String(t("nodeSelScale")));
  rec("R7_formula_single_impl", "半径公式只有一处实现（在 24-graph.js），25 渲染处调它、不另写副本",
    SRC24.indexOf("function gvNodeRadius(") >= 0
      && SRC25.indexOf("function gvNodeRadius(") < 0
      && /const r = gvNodeRadius\(n, deg, sel\);/.test(SRC25)
      && !/13 \+ Math\.min/.test(SRC25),
    `in24=${SRC24.indexOf("function gvNodeRadius(") >= 0} dup25=${SRC25.indexOf("function gvNodeRadius(") >= 0} call=${/const r = gvNodeRadius\(/.test(SRC25)}`);
} catch (e) {
  rec("R0_runtime", "R 组可执行", false, e.message);
}

/* ══════════ C 组：graphCollide（完全重合散开 + 半径感知宽度） ══════════ */
try {
  const ctx = mkCtx();
  // C5 要用真实半径函数算「两个大节点需要多宽」，必须一并注入（否则测的是猜的数）
  vm.runInContext(extractFn(SRC24, "gvNodeRadius"), ctx, { filename: "f:gvNodeRadius" });
  vm.runInContext(extractFn(SRC24, "graphCollide"), ctx, { filename: "f:graphCollide" });

  // ① 两节点完全重合 ⇒ 必须被推开（原实现 dx/d = 0/0.1 = 0 ⇒ 推不动）
  const two = [{ id: "p", x: 500, y: 300 }, { id: "q", x: 500, y: 300 }];
  ctx.graphCollide(two, 56);
  const d1 = Math.hypot(two[0].x - two[1].x, two[0].y - two[1].y);
  rec("C1_coincident_pushed", "完全重合的两个节点被推开（原实现推不动）",
    d1 > 1, `dist=${d1.toFixed(2)}`);

  // ② 确定性：同一输入两次运行结果一致（不能用 Math.random）
  const a2 = [{ id: "p", x: 500, y: 300 }, { id: "q", x: 500, y: 300 }];
  ctx.graphCollide(a2, 56);
  rec("C2_deterministic", "重合散开方向确定（同一输入同一结果，不用随机数）",
    Math.abs(a2[0].x - two[0].x) < 1e-9 && Math.abs(a2[0].y - two[0].y) < 1e-9,
    `run1=(${two[0].x.toFixed(2)},${two[0].y.toFixed(2)}) run2=(${a2[0].x.toFixed(2)},${a2[0].y.toFixed(2)})`);

  // ③ 一次调用即达 minD（若只推到一半，就要靠外层多轮，单轮判据会失效）
  rec("C3_reaches_minD", "单次 collide 后中心距 ≥ minD",
    d1 >= 56 - 1e-6, `dist=${d1.toFixed(2)} minD=56`);

  // ④ 正常情况不误动：距离已足够大时坐标不变
  const far = [{ id: "m", x: 0, y: 0 }, { id: "n", x: 500, y: 500 }];
  const bx = [far[0].x, far[1].x];
  ctx.graphCollide(far, 56);
  rec("C4_no_false_move", "距离足够远时坐标不被改动",
    far[0].x === bx[0] && far[1].x === bx[1], `${far[0].x}/${far[1].x}`);

  /* ⑤ 半径感知（2026-11-28 的真缺陷）：两个大节点需要 r_i+r_j+gap，
     固定 minD=56 时 33+33=66 > 56 ⇒ 数学上永远消不掉。
     这里用两个「半径 33」的节点，中心距 60（>56 但 <66）：修好后必须被推开。 */
  const big = [{ id: "b1", x: 0, y: 0 }, { id: "b2", x: 60, y: 0 }];
  // 给 b1/b2 都设成最大度数（deg=99 → 半径被 cap 到 33）
  const bigDeg = { b1: 99, b2: 99 };
  const radiusOf = (n) => ctx.gvNodeRadius(n, bigDeg, false);
  const moved = ctx.graphCollide(big, { minD: 56, gap: 6, radiusOf });
  const dBig = Math.abs(big[0].x - big[1].x);
  rec("C5_radius_aware", "碰撞按「半径和+间隙」判定（半径 33 的两个节点需 72，不是 56）",
    moved === 1 && dBig >= 33 + 33 + 6 - 1e-6,
    `moved=${moved} dist=${dBig.toFixed(2)} 期望≥72`);

  /* ⑥ 返回被移动对数：外层靠它判断收敛，返回 undefined 会让循环提前退出 */
  const mv = ctx.graphCollide([{ id: "x", x: 0, y: 0 }, { id: "y", x: 10, y: 0 }], { minD: 56 });
  rec("C6_returns_moved_count", "collide 返回被移动的对数（收敛判断依据）",
    typeof mv === "number" && mv === 1, `${typeof mv}:${mv}`);

  /* ⑦ 无重叠时必须返回 0（否则外层会误判未收敛，白跑 40 轮） */
  const mv0 = ctx.graphCollide([{ id: "x", x: 0, y: 0 }, { id: "y", x: 500, y: 0 }], { minD: 56 });
  rec("C7_zero_when_converged", "无重叠时返回 0（收敛信号）", mv0 === 0, String(mv0));
} catch (e) {
  rec("C0_runtime", "C 组可执行", false, e.message);
}

/* ══════════ W 组：graphSettleOverlaps 收敛到「任意两节点不重叠」 ══════════ */
try {
  const ctx = mkCtx();
  ["gvNodeRadius", "graphDegreeMap", "graphCollide", "graphSettleOverlaps"].forEach((n) =>
    vm.runInContext(extractFn(SRC24, n), ctx, { filename: "f:" + n }));

  const mkGraph = (N, seed) => {
    const nodes = [], edges = [];
    for (let i = 0; i < N; i++) nodes.push({ id: "n" + i, entity_type: "T" });
    let ei = 0;
    for (let i = 1; i < N; i++) edges.push({ id: "e" + (ei++), source_id: "n" + Math.floor(i / 3), target_id: "n" + i });
    for (let i = 0; i < 30; i++) {
      const a = (i * 17 + seed) % N, b = (i * 29 + 7) % N;
      if (a !== b) edges.push({ id: "e" + (ei++), source_id: "n" + a, target_id: "n" + b });
    }
    return { nodes, edges };
  };
  const measure = (nodes, edges) => {
    const deg = ctx.graphDegreeMap(nodes, edges);
    const r = (n) => ctx.gvNodeRadius(n, deg, false);
    let ov = 0, minGap = Infinity;
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
      const g = d - (r(nodes[i]) + r(nodes[j]));
      if (g < 0) ov++;
      minGap = Math.min(minGap, g);
    }
    return { ov, minGap: minGap === Infinity ? null : +minGap.toFixed(2) };
  };

  // ① 20 节点完全重合（最恶劣，190 对重叠）⇒ 必须归零
  const g1 = mkGraph(20, 3);
  g1.nodes.forEach((n) => { n.x = 400; n.y = 300; });
  const b1 = measure(g1.nodes, g1.edges);
  const s1 = ctx.graphSettleOverlaps(g1.nodes, g1.edges, 880, 560);
  const a1 = measure(g1.nodes, g1.edges);
  rec("W1_all_coincident_settled", "20 节点完全重合 → 收敛到 0 对重叠",
    b1.ov === 190 && a1.ov === 0 && s1.overlaps === 0,
    `before=${b1.ov} after=${a1.ov} minGap=${a1.minGap} expanded=${s1.expanded}`);

  // ② 40 节点近重合（780 对）⇒ 归零，且间隙非负
  const g2 = mkGraph(40, 5);
  g2.nodes.forEach((n, i) => { n.x = 400 + (i % 3); n.y = 300 + (i % 2); });
  const b2 = measure(g2.nodes, g2.edges);
  const s2 = ctx.graphSettleOverlaps(g2.nodes, g2.edges, 880, 560);
  const a2m = measure(g2.nodes, g2.edges);
  rec("W2_near_coincident_settled", "40 节点近重合 → 0 对重叠且最小间隙 ≥0",
    b2.ov > 500 && a2m.ov === 0 && a2m.minGap >= -1e-6,
    `before=${b2.ov} after=${a2m.ov} minGap=${a2m.minGap}`);

  // ③ 100 节点超密（画布塞不下）⇒ 仍要归零，并如实报告 expanded
  const g3 = mkGraph(100, 9);
  g3.nodes.forEach((n, i) => { n.x = (i % 5) * 40; n.y = Math.floor(i / 5) * 30; });
  const s3 = ctx.graphSettleOverlaps(g3.nodes, g3.edges, 880, 560);
  const a3 = measure(g3.nodes, g3.edges);
  rec("W3_dense_settled", "100 节点超密 → 0 对重叠（画布不够则扩大，不硬夹）",
    s3.overlaps === 0 && a3.ov === 0 && s3.expanded >= 1,
    `after=${a3.ov} expanded=${s3.expanded} H=${s3.H}`);

  // ④ 已无重叠时不得乱动（幂等）：坐标应保持不变
  const g4 = mkGraph(12, 1);
  g4.nodes.forEach((n, i) => { n.x = 100 + i * 90; n.y = 200 + (i % 2) * 120; });
  ctx.graphSettleOverlaps(g4.nodes, g4.edges, 880, 560);
  const posA = g4.nodes.map((n) => [Math.round(n.x * 100), Math.round(n.y * 100)]);
  ctx.graphSettleOverlaps(g4.nodes, g4.edges, 880, 560);
  const posB = g4.nodes.map((n) => [Math.round(n.x * 100), Math.round(n.y * 100)]);
  rec("W4_idempotent", "已无重叠时重复调用不再移动（幂等，避免每次刷新都漂移）",
    JSON.stringify(posA) === JSON.stringify(posB), posA.length + " 节点");

  // ⑤ 单节点/空图不崩
  const s5 = ctx.graphSettleOverlaps([{ id: "only", x: 0, y: 0 }], [], 880, 560);
  const s6 = ctx.graphSettleOverlaps([], [], 880, 560);
  rec("W5_degenerate_ok", "单节点与空图不崩且返回 overlaps=0",
    s5.overlaps === 0 && s6.overlaps === 0, JSON.stringify([s5.overlaps, s6.overlaps]));
} catch (e) {
  rec("W0_runtime", "W 组可执行", false, e.message);
}

/* ══════════ L 组：graphLayout 三分支都必须消除重叠 ══════════ */
try {
  const body = extractFn(SRC24, "graphLayout");
  /* 判据必须基于**去注释**的代码：本分支的注释里就写着「原实现这里直接 return」，
     拿含注释的切片去 indexOf("return") 会命中注释（实测 settle@332 / return@205 ⇒ 假红）。
     这是 P0「S2 假红」的同一形态，教训已有：判据扫代码，不扫注释。 */
  const stripComments = (s) => s.split(/\r?\n/)
    .filter((ln) => {
      const t = ln.trim();
      return !(t.startsWith("//") || t.startsWith("/*") || t.startsWith("*"));
    }).join("\n");
  const code = stripComments(body);
  // 三个分支各自都要调用 settle（dagre 分支原本直接 return，完全没做重叠消除）
  const ringPart = code.slice(code.indexOf("layout==='ring'"), code.indexOf("layout==='layered'"));
  const layPart = code.slice(code.indexOf("layout==='layered'"), code.indexOf("// 成熟力导向") < 0
    ? code.indexOf("gvLayoutWith('cose'") : code.indexOf("// 成熟力导向"));
  const forcePart = code.slice(code.indexOf("// 成熟力导向") >= 0
    ? code.indexOf("// 成熟力导向") : code.indexOf("gvLayoutWith('cose'"));
  rec("L1_ring_settles", "环形分支调用 graphSettleOverlaps",
    ringPart.indexOf("graphSettleOverlaps(") >= 0);
  /* 判据必须要求「settle 出现在分支内任何 return 之前」。
     只断言「调用了 settle」的话，在 gvNormalize 后插一个 return 仍能通过 ⇒ 判据没有鉴别力
     （变异 M7 第一版就是这么漏掉的：flipped=False 却仍报绿）。 */
  rec("L2_layered_settles", "分层分支在 return 之前调用 graphSettleOverlaps（原实现直接 return，完全不消除）",
    layPart.indexOf("graphSettleOverlaps(") >= 0
      && (layPart.indexOf("return") < 0 || layPart.indexOf("graphSettleOverlaps(") < layPart.indexOf("return")),
    `settle@${layPart.indexOf("graphSettleOverlaps(")} return@${layPart.indexOf("return")}`);
  rec("L3_force_settles", "力导向分支调用 graphSettleOverlaps",
    forcePart.indexOf("graphSettleOverlaps(") >= 0);
  // 边界不得再用逐点夹紧（那会把刚推开的节点压回去，与碰撞消除振荡）
  rec("L4_no_per_node_clamp", "布局里不再逐点夹紧（原 n.x=Math.max(30,Math.min(W-30,...)) 会撤销推开）",
    code.indexOf("Math.max(30,Math.min(W-30") < 0 && code.indexOf("Math.max(30, Math.min(W-30") < 0,
    code.indexOf("Math.max(30,Math.min(W-30") >= 0 ? "仍有逐点夹紧" : "none");
} catch (e) {
  rec("L0_runtime", "L 组可执行", false, e.message);
}

process.stdout.write(JSON.stringify({ checks: checks }) + "\n");
