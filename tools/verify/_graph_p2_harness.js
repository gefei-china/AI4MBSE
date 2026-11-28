/* 图谱 P2 门禁的 Node 执行体（由 tools/verify/verify_graph_p2_render.py 调用）
 *
 * 纪律：从 24-graph.js / 26-grapheditor.js **原文抽取函数**后在 vm 里执行，不复刻逻辑。
 *      P0 的教训：桩自己带了一个不存在的方法，判据再绿也测不出装配层缺陷 ⇒
 *      本文件的 DOM 桩只提供**代码真的用到**的成员，宁可让函数在桩上抛错，也不假装支持。
 * 用法：node _graph_p2_harness.js <mods_dir>
 * 输出：最后一行 JSON {"checks":[{id,name,ok,detail}]}
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIR = process.argv[2] || path.resolve(__dirname, "../../static/js/mods");
const SRC24 = fs.readFileSync(path.join(DIR, "24-graph.js"), "utf8");
const SRC26 = fs.readFileSync(path.join(DIR, "26-grapheditor.js"), "utf8");
const checks = [];
/* 签名 (id, name, ok, detail)：P1 曾因 rec 只收 3 参而把标题当 ok（恒真）⇒ 17/17 假绿。
   本文件所有调用一律 4 参，且 detail 里必须放真实数值。 */
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
function extractConst(src, name) {
  const head = "const " + name + " = ";
  const i = src.indexOf(head);
  if (i < 0) throw new Error("const not found: " + name);
  let j = i + head.length, depth = 0, k = j;
  for (; k < src.length; k++) {
    const c = src[k];
    if (c === "[" || c === "{") depth++;
    else if (c === "]" || c === "}") { depth--; if (depth === 0) { k++; break; } }
    if (c === ";" && depth === 0) { k++; break; }
  }
  return src.slice(i, k);
}

/* ── localStorage 桩（记录真实写入的键值，便于断言「按分支分开」） ── */
function mkLocalStorage() {
  const m = new Map();
  return {
    _m: m,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
    get size() { return m.size; },
  };
}

/* ── 通用上下文 ── */
function mkCtx(extra) {
  const calls = { toast: [], detail: [], rendered: 0, applied: 0, fitted: 0 };
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    Math, JSON, Number, String, Boolean, Array, Object, Set, Map, isFinite, parseFloat, parseInt, Date,
    localStorage: mkLocalStorage(),
    toast: (m) => calls.toast.push(String(m)),
    getCurrentBranch: () => ctx.__branch || "personal",
    __branch: "personal",
    graphState: {
      nodes: [], edges: [], all: { nodes: [], edges: [] },
      view: { hideIsolated: false, types: [], layout: "force", layoutOpts: null, colorBy: "type" },
      vp: { scale: 1, panX: 0, panY: 0 },
      sk: { on: false, drill: null, focus: null },
      fold: null, path: null, multiSel: new Set(), sel: null, focusNid: null,
      svgW: 880, svgH: 460,
    },
    renderGraph: () => { calls.rendered++; },
    graphApplyView: () => { calls.applied++; },
    graphLayout: () => { calls.layout = (calls.layout || 0) + 1; },
    gvApplyView: () => { calls.applied++; },
    gvFitView: () => { calls.fitted++; },
    gvEnsureMinimap: () => {},
    showGraphDetail: (id) => calls.detail.push(String(id)),
    document: { getElementById: () => null, querySelectorAll: () => [], createElementNS: () => ({}) },
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.__calls = calls;
  Object.assign(ctx, extra || {});
  vm.createContext(ctx);
  return ctx;
}

/* 把 24-graph.js 里 P2-1/P2-2 的常量与函数装进上下文 */
const FNS24 = [
  "gvAdjacency", "gvComponents", "gvDetectCommunities", "gvAssignCommunities",
  "graphCommunityStats", "gvCommunityColor",
  "gvViewStoreRead", "gvViewStoreWrite", "gvListSavedViews", "gvSnapshotView",
  "gvSaveCurrentView", "gvDeleteSavedView", "gvSanitizeSnapshot", "gvApplySnapshot",
];
const CONSTS24 = ["GV_COMMUNITY_PALETTE", "GV_VIEW_STORE_KEY", "GV_VIEW_LIMIT", "GV_VIEW_POS_CAP"];
function install24(ctx) {
  CONSTS24.forEach((n) => vm.runInContext(extractConst(SRC24, n), ctx, { filename: "c:" + n }));
  FNS24.forEach((n) => vm.runInContext(extractFn(SRC24, n), ctx, { filename: "f:" + n }));
}

/* ══════════ C 组：P2-1 社区聚类 ══════════ */
try {
  const ctx = mkCtx();
  install24(ctx);

  /* 两个 4 节点团 + 一条桥边：期望两侧各成一个主社区（桥的两端归属不同侧） */
  const nodes = [];
  const edges = [];
  [["a1", 0, 0], ["a2", 100, 0], ["a3", 0, 100], ["a4", 100, 100],
   ["b1", 600, 0], ["b2", 700, 0], ["b3", 600, 100], ["b4", 700, 100]].forEach(([id, x, y]) => {
    nodes.push({ id, name: id, entity_type: "T", x, y });
  });
  /* 每个 4 节点团**全连接**（6 条边）。原来写成 4-环（4 条边），
     而 4-环的模块度最优点本来就是两两分组 —— 那样测不出「团是否被正确分开」。 */
  [["a1","a2"],["a1","a3"],["a1","a4"],["a2","a3"],["a2","a4"],["a3","a4"],
   ["b1","b2"],["b1","b3"],["b1","b4"],["b2","b3"],["b2","b4"],["b3","b4"],
   ["a4","b1"]].forEach(([s, t], i) => edges.push({ id: "e" + i, source_id: s, target_id: t, relation_type: "R" }));

  const r1 = ctx.gvDetectCommunities(nodes, edges);
  const sideA = new Set(["a1", "a2", "a3", "a4"].map((id) => r1.of.get(id)));
  const sideB = new Set(["b1", "b2", "b3", "b4"].map((id) => r1.of.get(id)));
  rec("C1_modularity_two_clusters", "两个密集团被分成两个社区（跨团不混）",
    sideA.size === 1 && sideB.size === 1 && [...sideA][0] !== [...sideB][0],
    "A=" + [...sideA].join(",") + " B=" + [...sideB].join(",") + " 社区数=" + r1.list.length);

  /* 确定性：节点/边顺序反转后结果必须逐节点相同 */
  const rev = ctx.gvDetectCommunities(nodes.slice().reverse(), edges.slice().reverse());
  const same = nodes.every((n) => rev.of.get(n.id) === r1.of.get(n.id));
  rec("C2_deterministic_order", "同一份数据换输入顺序结果不变（确定性）", same,
    same ? "8/8 一致" : nodes.filter((n) => rev.of.get(n.id) !== r1.of.get(n.id)).map((n) => n.id).join(","));

  /* 孤立节点不分社区 */
  const withIso = nodes.concat([{ id: "iso1", name: "孤立", entity_type: "T", x: 999, y: 999 }]);
  const r2 = ctx.gvDetectCommunities(withIso, edges);
  rec("C3_isolate_null", "孤立节点 _community 为 null（不造噪声社区）",
    r2.of.get("iso1") === null && !r2.list.some((g) => g.members.includes("iso1")),
    String(r2.of.get("iso1")));

  /* 连通分量模式：三个不相连的组 → 三个社区 */
  const cn = [{ id: "x1" }, { id: "x2" }, { id: "y1" }, { id: "y2" }, { id: "z1" }, { id: "z2" }];
  const ce = [{ source_id: "x1", target_id: "x2" }, { source_id: "y1", target_id: "y2" }, { source_id: "z1", target_id: "z2" }];
  const rc = ctx.gvDetectCommunities(cn, ce, { method: "components" });
  rec("C4_components_method", "components 模式按连通分量分组（3 组）",
    rc.list.length === 3 && rc.method === "components", "组数=" + rc.list.length);

  /* 色带：12 色、去重、HSL 饱和度 ≤ 0.45（低饱和，不与类型色抢视觉） */
  /* ⚠ 顶层 const 不会成为 ctx 的属性（与 P1「顶层 let 不挂 window」同源坑）：
     ctx.GV_COMMUNITY_PALETTE 恒 undefined。必须用 vm 在上下文里求值。 */
  const pal = vm.runInContext("GV_COMMUNITY_PALETTE", ctx);
  const hslSat = (hex) => {
    const r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    if (mx === mn) return 0;
    return (mx - mn) / (1 - Math.abs(2 * l - 1));
  };
  const maxSat = Math.max(...pal.map(hslSat));
  rec("C5_palette_low_sat", "社区色带 12 色且低饱和（HSL S ≤ 0.45）",
    pal.length === 12 && new Set(pal).size === 12 && maxSat <= 0.45 + 1e-9,
    "n=" + pal.length + " uniq=" + new Set(pal).size + " maxSat=" + maxSat.toFixed(3));

  /* 颜色稳定：社区身份取组内字典序最小 id ⇒ 加成员不换色 */
  const r3 = ctx.gvDetectCommunities(nodes, edges);
  const r4 = ctx.gvDetectCommunities(nodes.concat([{ id: "a5", name: "a5", entity_type: "T" }]),
    edges.concat([{ id: "eX", source_id: "a1", target_id: "a5" }]));
  const anchorA = r3.list.find((g) => g.members.includes("a1"));
  const anchorA2 = r4.list.find((g) => g.members.includes("a1"));
  rec("C6_color_stable", "社区身份取组内最小 id（加成员不换色）",
    !!anchorA && !!anchorA2 && anchorA.label === anchorA2.label &&
    ctx.gvCommunityColor(anchorA.label) === ctx.gvCommunityColor(anchorA2.label),
    (anchorA && anchorA.label) + " / " + (anchorA2 && anchorA2.label));

  /* 写回：n._community 真的落到节点上 */
  const ns = nodes.map((n) => Object.assign({}, n));
  const r5 = ctx.gvAssignCommunities(ns, edges);
  rec("C7_assign_writes_nodes", "gvAssignCommunities 把结果写回 n._community",
    ns.every((n) => n._community === r5.of.get(String(n.id))),
    ns.filter((n) => n._community).length + "/" + ns.length + " 有社区");
} catch (e) {
  rec("C0_runtime", "C 组可执行", false, e.message);
}

/* ══════════ V 组：P2-2 视角保存 ══════════ */
try {
  const ctx = mkCtx();
  install24(ctx);
  const gs = ctx.graphState;
  gs.nodes = [{ id: "n1", name: "甲", entity_type: "需求", x: 10, y: 20 },
              { id: "n2", name: "乙", entity_type: "部件", x: 110, y: 220 },
              { id: "n3", name: "丙", entity_type: "功能", x: 310, y: 120 }];
  gs.all.nodes = gs.nodes.slice();
  gs.edges = [{ id: "e1", source_id: "n1", target_id: "n2" }];
  gs.view = { hideIsolated: true, types: ["需求"], layout: "layered", layoutOpts: { sep: 120, rep: 30000, iters: 300 }, colorBy: "community" };
  gs.vp = { scale: 1.5, panX: -40, panY: 25 };
  gs.sk = { on: true, drill: "需求", focus: "n1" };

  const snap = ctx.gvSnapshotView("我的视角");
  rec("V1_snapshot_fields", "快照含 view/vp/sk/pos 四类字段",
    snap.schema === 1 && snap.name === "我的视角" && snap.view.colorBy === "community" &&
    snap.vp.scale === 1.5 && snap.sk.focus === "n1" && Object.keys(snap.pos).length === 3,
    "schema=" + snap.schema + " pos=" + Object.keys(snap.pos).length + " vp=" + snap.vp.scale);

  const saved = ctx.gvSaveCurrentView("我的视角");
  const listed = ctx.gvListSavedViews("personal");
  rec("V2_save_list_roundtrip", "保存后能在同分支列出（名称/缩放一致）",
    !!saved && listed.length === 1 && listed[0].name === "我的视角" && listed[0].vp.scale === 1.5,
    "listed=" + listed.length + " name=" + (listed[0] && listed[0].name));

  /* 分支隔离：dev 分支看不到 personal 的视角 */
  const devList = ctx.gvListSavedViews("dev");
  const store = vm.runInContext("gvViewStoreRead()", ctx);
  rec("V3_per_branch", "视角按分支独立（dev 分支读不到 personal 的）",
    devList.length === 0 && !!store.personal && !store.dev,
    "dev=" + devList.length + " keys=" + Object.keys(store).join(","));

  /* 恢复到「已变化的数据集」：悬空类型/骨架焦点被丢弃，缩放被夹紧 */
  const stale = JSON.parse(JSON.stringify(snap));
  stale.view.types = ["需求", "已删除的类型"];
  stale.sk.focus = "gone-id";
  stale.sk.drill = "已删除的类型";
  stale.vp.scale = 99;
  stale.pos.missingNode = [1, 2];
  const san = ctx.gvSanitizeSnapshot(stale, gs.nodes, gs.all.nodes);
  rec("V4_sanitize_dangling", "清洗悬空类型/骨架焦点 + 夹紧缩放",
    san.ok && san.dropped.types === 1 && san.dropped.focus === 1 && san.dropped.drill === 1 &&
    san.snap.sk.focus === null && san.snap.view.types.length === 1 && san.snap.vp.scale === 3,
    "dropped=" + JSON.stringify(san.dropped) + " scale=" + san.snap.vp.scale);

  /* 跨分支快照必须拒绝 */
  const other = JSON.parse(JSON.stringify(snap));
  other.branch = "dev";
  const rej = ctx.gvSanitizeSnapshot(other, gs.nodes, gs.all.nodes);
  rec("V5_branch_mismatch_refused", "跨分支快照被拒绝（不静默套用）",
    rej.ok === false && /分支/.test(rej.reason), String(rej.reason));

  /* 精确恢复：坐标齐全 ⇒ 逐节点坐标与 vp 完全复原 */
  gs.vp = { scale: 1, panX: 0, panY: 0 };
  gs.nodes[0].x = 999;
  const ap = ctx.gvApplySnapshot(snap);
  rec("V6_apply_exact", "坐标齐全时精确复原（坐标 + 缩放平移）",
    ap.ok && ap.exact === true && gs.nodes[0].x === 10 && gs.vp.scale === 1.5 && gs.vp.panX === -40,
    "exact=" + ap.exact + " x=" + gs.nodes[0].x + " scale=" + gs.vp.scale);

  /* 非精确：快照缺坐标 ⇒ 重排 + 自适应，且如实报告 */
  const noPos = JSON.parse(JSON.stringify(snap));
  noPos.pos = {};
  const ap2 = ctx.gvApplySnapshot(noPos);
  rec("V7_apply_fallback", "坐标缺失时重排并如实标记（exact=false）",
    ap2.ok && ap2.exact === false && gs.vp.scale === 1,
    "exact=" + ap2.exact + " notes=" + (ap2.notes || []).join("|"));

  /* 上限：同名覆盖、超额截断 */
  const c2 = mkCtx(); install24(c2);
  c2.graphState.nodes = [{ id: "z", name: "z", entity_type: "T", x: 0, y: 0 }];
  c2.graphState.all.nodes = c2.graphState.nodes.slice();
  for (let i = 0; i < 15; i++) c2.gvSaveCurrentView("v" + i);
  const n15 = c2.gvListSavedViews("personal").length;
  c2.gvSaveCurrentView("v14");   // 同名覆盖，不应新增
  const n16 = c2.gvListSavedViews("personal").length;
  rec("V8_limit_and_replace", "上限截断且同名覆盖不重复（≤12）",
    n15 === 12 && n16 === 12, "after15=" + n15 + " afterReplace=" + n16);

  /* 删除 */
  ctx.gvDeleteSavedView("我的视角");
  rec("V9_delete", "删除后列表为空", ctx.gvListSavedViews("personal").length === 0,
    String(ctx.gvListSavedViews("personal").length));
} catch (e) {
  rec("V0_runtime", "V 组可执行", false, e.message);
}

/* ══════════ X 组：P2-3 导出当前视野 ══════════ */
try {
  /* 最小 DOM 桩：只实现 gvExportBBox / gvExportSvgSource 真正用到的成员。
     cloneNode 复制 attrs；insertBefore 按 ref 位置插入；serializeToString 输出可断言字符串。 */
  function mkEl(tag) {
    const attrs = {};
    const kids = [];
    const el = {
      tagName: tag, _attrs: attrs, _kids: kids,
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return attrs[k]; },
      removeAttribute(k) { delete attrs[k]; },
      insertBefore(ch, ref) {
        const i = ref ? kids.indexOf(ref) : -1;
        if (i < 0) kids.push(ch); else kids.splice(i, 0, ch);
        return ch;
      },
      get firstChild() { return kids[0] || null; },
      querySelectorAll(sel) {
        if (sel === "[tabindex]") return [{ removeAttribute(k) { attrs.__removedTab = k; } }];
        return [];
      },
      cloneNode() { const c = mkEl(tag); Object.keys(attrs).forEach((k) => (c._attrs[k] = attrs[k])); return c; },
      classList: { remove() {}, add() {}, contains() { return false; }, toggle() {} },
      style: {}, clientWidth: 900, clientHeight: 420,
    };
    return el;
  }
  const svgEl = mkEl("svg");
  svgEl.setAttribute("id", "gv-svg");
  const innerEl = mkEl("g");
  let bbox = { x: 10, y: 20, width: 300, height: 200 };
  innerEl.getBBox = () => bbox;

  const ctx = mkCtx({
    graphTok: (n) => {
      const T = { canvas: "#ffffff", primary: "#185FA5", primaryD: "#0C447C", primaryL: "#E6F1FB" };
      return n ? (T[n] || "#000000") : T;   // 无参返回整表（导出代码就是这么用的）
    },
    graphState: { vp: { scale: 2, panX: 30, panY: -10 }, svgW: 880, svgH: 460, nodes: [], edges: [] },
    document: {
      getElementById: (id) => (id === "gv-svg" ? svgEl : id === "gv-inner" ? innerEl : null),
      createElementNS: (ns, tag) => mkEl(tag),
      createElement: () => mkEl("div"),
      querySelectorAll: () => [],
      body: { appendChild() {}, removeChild() {} },
    },
    XMLSerializer: function () { this.serializeToString = (n) => "<svg " + Object.keys(n._attrs).map((k) => k + '="' + n._attrs[k] + '"').join(" ") + ">"; },
    Blob: function () {}, URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    Image: function () { this.onload = null; this.src = ""; },
    setTimeout: () => 1,
  });
  vm.runInContext(extractFn(SRC26, "gvExportBBox"), ctx, { filename: "f:gvExportBBox" });
  vm.runInContext(extractFn(SRC26, "gvExportSvgSource"), ctx, { filename: "f:gvExportSvgSource" });
  vm.runInContext(extractFn(SRC26, "gvExportPngScale"), ctx, { filename: "f:gvExportPngScale" });

  /* bbox：getBBox 是「#gv-inner 局部坐标」，必须乘 scale 加 pan 才是视口坐标 */
  const bb = ctx.gvExportBBox();
  const expX = Math.round(10 * 2 + 30 - 18), expW = Math.ceil(300 * 2 + 36);
  rec("X1_bbox_viewport", "导出包围盒换算到视口坐标（×scale + pan + padding）",
    bb.x === expX && bb.width === expW && bb.height === Math.ceil(200 * 2 + 36),
    "x=" + bb.x + "(exp " + expX + ") w=" + bb.width + " h=" + bb.height);

  /* PNG 倍率上限 */
  const s1 = ctx.gvExportPngScale(400, 300), s2 = ctx.gvExportPngScale(5000, 3000);
  rec("X2_png_scale_cap", "PNG 倍率：小图 2x，超大图自动降到 ≤1（单边/面积双上限）",
    s1 === 2 && s2 <= 1 && s2 > 0, "small=" + s1 + " huge=" + s2);

  /* SVG 序列化：xmlns / viewBox / 背景 rect / 字体栈 / 去 tabindex / 内联类样式 */
  const got = ctx.gvExportSvgSource();
  const a = got && got.clone ? null : null;
  rec("X3_svg_source", "导出 SVG 补全 xmlns/尺寸/viewBox/字体栈",
    !!got && /xmlns="http:\/\/www\.w3\.org\/2000\/svg"/.test(got.src)
      && /viewBox="[-\d.]+ [-\d.]+ \d+ \d+"/.test(got.src)
      && /font-family:'Microsoft YaHei'/.test(got.src)
      && got.w === bb.width && got.h === bb.height,
    got ? "src=" + got.src.slice(0, 90) : "null");
} catch (e) {
  rec("X0_runtime", "X 组可执行", false, e.message);
}

/* X4/X5 单独一组：抓 clone 上的背景 rect / 内联样式 / tabindex 清理。
   上面 X3 只断言「最终字符串」，抓不到「像素会透明」这类问题（背景是 rect 不是 style）。 */
try {
  const made = { rect: [], style: [], removedTab: 0 };
  function mkEl2(tag) {
    const attrs = {}, kids = [];
    const el = {
      tagName: tag, _attrs: attrs, _kids: kids,
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return attrs[k]; },
      removeAttribute(k) { delete attrs[k]; },
      insertBefore(ch, ref) { const i = ref ? kids.indexOf(ref) : -1; if (i < 0) kids.push(ch); else kids.splice(i, 0, ch); return ch; },
      get firstChild() { return kids[0] || null; },
      querySelectorAll(sel) {
        if (sel === "[tabindex]") return [{ removeAttribute() { made.removedTab++; } }];
        return [];
      },
      cloneNode() { const c = mkEl2(tag); Object.keys(attrs).forEach((k) => (c._attrs[k] = attrs[k])); return c; },
      classList: { remove() {}, add() {}, contains() { return false; }, toggle() {} },
      style: {}, clientWidth: 900, clientHeight: 420,
    };
    return el;
  }
  const svgEl = mkEl2("svg");
  const innerEl = mkEl2("g");
  innerEl.getBBox = () => ({ x: 0, y: 0, width: 100, height: 80 });
  const ctx = mkCtx({
    graphTok: (n) => {
      const T = { canvas: "#ffffff", primary: "#185FA5", primaryD: "#0C447C", primaryL: "#E6F1FB" };
      return n ? (T[n] || "#000000") : T;   // 无参返回整表（导出代码就是这么用的）
    },
    graphState: { vp: { scale: 1, panX: 0, panY: 0 }, svgW: 880, svgH: 460, nodes: [], edges: [] },
    document: {
      getElementById: (id) => (id === "gv-svg" ? svgEl : id === "gv-inner" ? innerEl : null),
      createElementNS: (ns, tag) => {
        const e = mkEl2(tag);
        if (tag === "rect") made.rect.push(e);
        if (tag === "style") made.style.push(e);
        return e;
      },
      createElement: () => mkEl2("div"),
      querySelectorAll: () => [],
      body: { appendChild() {}, removeChild() {} },
    },
    XMLSerializer: function () { this.serializeToString = () => "<svg/>"; },
    setTimeout: () => 1,
  });
  vm.runInContext(extractFn(SRC26, "gvExportBBox"), ctx, { filename: "f:gvExportBBox" });
  vm.runInContext(extractFn(SRC26, "gvExportSvgSource"), ctx, { filename: "f:gvExportSvgSource" });
  const got = ctx.gvExportSvgSource();
  const bg = made.rect[0];
  const st = made.style[0];
  rec("X4_export_background_rect", "导出补画布底色 rect（CSS background 不会被序列化带走）",
    !!bg && bg._attrs.fill === "#ffffff" && +bg._attrs.width > 0 && +bg._attrs.height > 0,
    bg ? ("fill=" + bg._attrs.fill + " w=" + bg._attrs.width) : "no rect");
  rec("X5_export_inline_style", "导出内联类样式（标签密度/多选/路径，且色值来自 token）",
    !!st && /gv-far/.test(st.textContent || "") && /gv-path-node/.test(st.textContent || "")
      && /#E6F1FB/i.test(st.textContent || "") && /undefined/i.test(st.textContent || "") === false,
    st ? String(st.textContent).slice(0, 90) : "no style");
  rec("X6_export_strips_tabindex", "导出剥掉 tabindex（导出图不带键盘焦点）",
    made.removedTab >= 1 && !!got, "removed=" + made.removedTab);
} catch (e) {
  rec("X7_runtime", "X4-X6 可执行", false, e.message);
}

/* ══════════ K 组：P2-4 键盘可达性 ══════════ */
try {
  const ctx = mkCtx();
  const SRC = SRC26;
  vm.runInContext(extractConst(SRC, "GV_DIRS"), ctx, { filename: "c:GV_DIRS" });
  ["gvNeighborInDirection", "gvRestoreFocus", "gvNodeKey", "gvNodeActivate"].forEach((n) =>
    vm.runInContext(extractFn(SRC, n), ctx, { filename: "f:" + n }));

  /* 中心节点 c；右 (100,0)、右偏下 (140,40)、上 (0,-90)、左下 (-80,60) */
  const nodes = [
    { id: "c", x: 0, y: 0 },
    { id: "r", x: 100, y: 0 },
    { id: "rd", x: 140, y: 40 },
    { id: "u", x: 0, y: -90 },
    { id: "dl", x: -80, y: 60 },
  ];
  const edges = [
    { id: "e1", source_id: "c", target_id: "r" },
    { id: "e2", source_id: "c", target_id: "rd" },
    { id: "e3", source_id: "u", target_id: "c" },
    { id: "e4", source_id: "c", target_id: "dl" },
  ];
  rec("K1_right_prefers_aligned", "右方向优先选正对着的邻居（不是更远的斜向邻居）",
    ctx.gvNeighborInDirection(nodes, edges, "c", "ArrowRight") === "r",
    String(ctx.gvNeighborInDirection(nodes, edges, "c", "ArrowRight")));
  rec("K2_up_down_left", "上/下/左三向各自命中",
    ctx.gvNeighborInDirection(nodes, edges, "c", "ArrowUp") === "u" &&
    ctx.gvNeighborInDirection(nodes, edges, "c", "ArrowLeft") === "dl" &&
    ctx.gvNeighborInDirection(nodes, edges, "c", "ArrowDown") === "dl",
    ["ArrowUp", "ArrowLeft", "ArrowDown"].map((d) => ctx.gvNeighborInDirection(nodes, edges, "c", d)).join("/"));
  rec("K3_no_neighbor_null", "该方向无邻居返回 null（不是乱跳）",
    ctx.gvNeighborInDirection(nodes, edges, "r", "ArrowRight") === null,
    String(ctx.gvNeighborInDirection(nodes, edges, "r", "ArrowRight")));

  /* Enter：选中 + 打开详情 + 阻止默认（防止触发表单提交） */
  ctx.graphState.nodes = nodes;
  ctx.graphState.edges = edges;
  let prevented = 0, stopped = 0;
  const ev = { key: "Enter", preventDefault: () => prevented++, stopPropagation: () => stopped++ };
  ctx.gvNodeKey(ev, "c");
  rec("K4_enter_activates", "Enter 选中节点并打开详情（且 preventDefault）",
    ctx.graphState.sel && ctx.graphState.sel.id === "c" && ctx.__calls.detail.includes("c") && prevented === 1 && stopped === 1,
    "sel=" + JSON.stringify(ctx.graphState.sel) + " detail=" + ctx.__calls.detail.join(",") + " prevented=" + prevented);

  /* 方向键：focusNid 移到邻居 + 阻止默认（不滚页面）+ 触发一次重建 */
  const before = ctx.__calls.rendered;
  const ev2 = { key: "ArrowRight", preventDefault: () => prevented++, stopPropagation: () => stopped++ };
  ctx.gvNodeKey(ev2, "c");
  rec("K5_arrow_moves_focus", "方向键把焦点移到邻居并重建（并 preventDefault 防滚动）",
    ctx.graphState.focusNid === "r" && ctx.__calls.rendered === before + 1 && prevented === 2,
    "focusNid=" + ctx.graphState.focusNid + " rendered+" + (ctx.__calls.rendered - before));

  /* Esc：清选中与焦点 */
  ctx.gvNodeKey({ key: "Escape", preventDefault() {}, stopPropagation() {} }, "r");
  rec("K6_escape_clears", "Esc 清选中与 focusNid",
    ctx.graphState.sel === null && ctx.graphState.focusNid === null,
    "sel=" + ctx.graphState.sel + " focusNid=" + ctx.graphState.focusNid);

  /* 焦点复位：innerHTML 重建后按 focusNid 找回同一个 <g> */
  const focused = [];
  const fakeSvg = {
    querySelectorAll: () => [
      { getAttribute: () => "other", focus: () => focused.push("other") },
      { getAttribute: () => "n9", focus: () => focused.push("n9") },
    ],
  };
  ctx.graphState.focusNid = "n9";
  const okFocus = ctx.gvRestoreFocus(fakeSvg);
  ctx.graphState.focusNid = "ghost";
  const okGhost = ctx.gvRestoreFocus(fakeSvg);
  rec("K7_restore_focus", "重建后按 focusNid 复位焦点；节点不存在时返回 false",
    okFocus === true && focused.join(",") === "n9" && okGhost === false,
    "focused=" + focused.join(",") + " ghost=" + okGhost);
} catch (e) {
  rec("K0_runtime", "K 组可执行", false, e.message);
}

process.stdout.write(JSON.stringify({ checks: checks }) + "\n");
