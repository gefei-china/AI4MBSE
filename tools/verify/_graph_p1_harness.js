/* 图谱 P1 交互门禁的 Node 执行体（由 tools/verify/verify_graph_p1_interact.py 调用）
 *
 * 纪律：从 26-grapheditor.js **原文抽取函数体**后在 vm 里执行，不复刻逻辑。
 *      复刻只能测零件，测不出装配（P0 那次「桩自己带了不存在的方法」就是教训）。
 * 用法：node _graph_p1_harness.js <mods_dir>
 * 输出：最后一行 JSON {"checks":[{id,ok,detail}]}
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIR = process.argv[2] || path.resolve(__dirname, "../../static/js/mods");
const SRC24 = fs.readFileSync(path.join(DIR, "24-graph.js"), "utf8");
const SRC26 = fs.readFileSync(path.join(DIR, "26-grapheditor.js"), "utf8");
const checks = [];
/* 签名必须是 (id, name, ok, detail) —— 2026-11-28 踩过：原来只收 3 参（id, ok, detail），
   而全部调用都按 4 参写，于是 ok 收到的是标题字符串（恒 truthy）⇒ 17/17 全绿是假绿。
   判据函数自己也会骗自己，改动后务必先看一个必红用例是否真红。 */
const rec = (id, name, ok, detail) =>
  checks.push({ id, name, ok: !!ok, detail: detail === undefined ? "" : String(detail) });

/* 花括号配平取函数体（含函数头）。模板串里的 ${...} 也是配平的，注释里的括号可能干扰，
   但本文件抽取的函数体里没有裸括号注释。 */
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

// ── 通用执行上下文（DOM 全部桩化）──
function mkCtx(extra) {
  const calls = { toast: [], rendered: 0, removed: [] };
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    setTimeout: () => 1,
    clearTimeout() {},
    Math, JSON, Number, String, Boolean, Array, Object, Set, Map, isFinite, parseFloat, parseInt,
    document: {
      documentElement: {},
      getElementById: (id) => {
        if (id === "gv-svg") return { style: {}, classList: { toggle() {}, contains() { return false; } } };
        if (id === "gv-box") return null;          // gvBoxClear 找不到即无事
        return null;
      },
      querySelectorAll: () => [],
      createElementNS: () => ({
        setAttribute(k, v) { this["_" + k] = v; },
        getAttribute(k) { return this["_" + k]; },
        style: {},
      }),
      createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
      head: { appendChild() {} },
    },
    toast: (m) => calls.toast.push(String(m)),
    renderGraph: () => { calls.rendered++; },
    gvMultiSelDetail() {},
    showGraphDetail() {},
    graphState: { nodes: [], edges: [], view: {}, vp: { scale: 1, panX: 0, panY: 0 },
                  multiSel: new Set(), fold: null, path: null, box: null },
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.__calls = calls;
  Object.assign(ctx, extra || {});
  vm.createContext(ctx);
  return ctx;
}

// ══════════ P 组：路径 BFS（真跑 gvPathBFS） ══════════
try {
  const ctx = mkCtx();
  vm.runInContext(extractFn(SRC26, "gvPathBFS"), ctx, { filename: "gvPathBFS.js" });
  /* BFS 调用一律带执行超时：变异体（比如去掉 visited 判断）遇上自环会死循环，
     裸调会把整条门禁挂死成「环境问题」。vm 的 timeout 能中断同步死循环，
     让「变异打坏了」表现为一条可读的失败，而不是进程被杀。 */
  const BFS = (from, to) => {
    ctx.__a = from; ctx.__b = to;
    return vm.runInContext("gvPathBFS(__a, __b)", ctx, { timeout: 3000, filename: "bfs_call.js" });
  };
  const setGraph = (nodes, edges) => {
    vm.runInContext("graphState.nodes = __n; graphState.edges = __e;", Object.assign(ctx, { __n: nodes, __e: edges }),
      { filename: "setGraph.js" });
  };

  // 链 A→B→C→D，另有 A→C 直达（用于验证"最少跳数"而不是"最先遇到"）
  setGraph(
    [{ id: "A" }, { id: "B" }, { id: "C" }, { id: "D" }],
    [
      { id: "e1", source_id: "A", target_id: "B", relation_type: "r1" },
      { id: "e2", source_id: "B", target_id: "C", relation_type: "r2" },
      { id: "e3", source_id: "C", target_id: "D", relation_type: "r3" },
      { id: "e4", source_id: "A", target_id: "C", relation_type: "r0" },
    ]
  );
  const r1 = BFS("A", "B");
  rec("P1_1hop", "相邻两点 = 1 跳", r1 && r1.hops === 1, r1 && r1.hops);
  const r2 = BFS("A", "D");
  rec("P2_shortest", "A→D 取最短（2 跳 A-C-D，而非 3 跳 A-B-C-D）",
    r2 && r2.hops === 2 && r2.nodeIds.has("C"), r2 && (r2.hops + ":" + [...r2.nodeIds].join("/")));
  rec("P3_steps_len", "步数 = 跳数，且 step 首尾闭环",
    r2 && r2.steps.length === 2 && r2.steps[0].from === "A" && r2.steps[r2.steps.length - 1].to === "D",
    r2 && JSON.stringify(r2.steps.map((s) => s.from + ">" + s.to)));
  const r4 = BFS("D", "A");
  rec("P4_undirected", "无向：逆着关系方向也能找到同一条链",
    r4 && r4.hops === 2, r4 && r4.hops);

  // 不可达
  setGraph([{ id: "A" }, { id: "Z" }], [{ id: "e1", source_id: "A", target_id: "B", relation_type: "r1" }]);
  const r5 = BFS("A", "Z");
  rec("P5_unreachable_null", "无路径返回 null（不是空集合假装成功）", r5 === null, String(r5));

  // 同层字典序：A 同时连到 B（关系 zeta）与 C（关系 alpha），两者都能到 D
  //   两条等长路径 A-zeta->B-alpha->D / A-alpha->C-zeta->D，字典序应先走 alpha（C）
  setGraph(
    [{ id: "A" }, { id: "B" }, { id: "C" }, { id: "D" }],
    [
      { id: "e1", source_id: "A", target_id: "B", relation_type: "zeta" },
      { id: "e2", source_id: "A", target_id: "C", relation_type: "alpha" },
      { id: "e3", source_id: "B", target_id: "D", relation_type: "alpha" },
      { id: "e4", source_id: "C", target_id: "D", relation_type: "zeta" },
    ]
  );
  const r6a = BFS("A", "D");
  const r6b = BFS("A", "D");
  rec("P6_deterministic", "同一数据两次结果完全一致（可复现口径）",
    r6a && r6b && r6a.steps.map((s) => s.eid).join(",") === r6b.steps.map((s) => s.eid).join(","),
    r6a && r6a.steps.map((s) => s.eid).join(","));
  rec("P7_lexico_order", "同层按关系类型字典序（alpha 先于 zeta）",
    r6a && r6a.steps[0].rel === "alpha", r6a && r6a.steps[0].rel);

  // 自环/多重边不得死循环
  setGraph([{ id: "A" }, { id: "B" }], [
    { id: "e1", source_id: "A", target_id: "A", relation_type: "self" },
    { id: "e2", source_id: "A", target_id: "B", relation_type: "r" },
    { id: "e3", source_id: "A", target_id: "B", relation_type: "r" },
  ]);
  const r8 = BFS("A", "B");
  rec("P8_selfloop_multiedge", "自环与多重边不影响结果（=1 跳）", r8 && r8.hops === 1, r8 && r8.hops);
} catch (e) {
  rec("P0_runtime", "P 组可执行（含 3s 防死循环超时）", false, e.message);
}

// ══════════ Q 组：框选命中（真跑 gvWorldToScreen + gvBoxCommit） ══════════
try {
  const ctx = mkCtx();
  vm.runInContext(
    [extractFn(SRC26, "gvWorldToScreen"), extractFn(SRC26, "gvBoxClear"), extractFn(SRC26, "gvBoxCommit")].join("\n"),
    ctx, { filename: "box.js" }
  );
  vm.runInContext(
    "graphState.nodes=[{id:'n1',x:50,y:50},{id:'n2',x:300,y:300},{id:'n3',x:120,y:80}];" +
    "graphState.edges=[];" +
    "graphState.vp={scale:2,panX:100,panY:50};" +   // 世界(50,50)→视口(200,150)；(300,300)→(700,650)
    "graphState.box={x0:0,y0:0,x1:400,y1:400};" +
    "graphState.multiSel=new Set();graphState.fold=null;",
    ctx, { filename: "seed.js" }
  );
  ctx.gvBoxCommit();
  const got = [...ctx.graphState.multiSel].sort().join(",");
  // n1→(200,150) 在框内；n3→(340,210) 在框内；n2→(700,650) 在框外
  rec("Q1_hit_uses_viewport", "命中测试用视口坐标（世界坐标会误判 n2）",
    got === "n1,n3", got);
  rec("Q2_box_cleared", "提交后 box 状态清空（不残留框）", ctx.graphState.box === null, String(ctx.graphState.box));
  rec("Q3_render_after_commit", "提交后触发一次重绘", ctx.__calls.rendered === 1, ctx.__calls.rendered);

  // 误触：框太小 → 空集，不误选
  vm.runInContext(
    "graphState.multiSel=new Set();graphState.box={x0:10,y0:10,x1:12,y1:12};",
    ctx, { filename: "seed2.js" }
  );
  ctx.gvBoxCommit();
  rec("Q4_tiny_box_no_select", "过小的框（<4px）视为误触，不选中任何节点",
    ctx.graphState.multiSel.size === 0, ctx.graphState.multiSel.size);

  // 折叠域内的节点不可选
  vm.runInContext(
    "graphState.multiSel=new Set();graphState.fold={nid:'n1',hidden:new Set(['n3']),keep:1};" +
    "graphState.box={x0:0,y0:0,x1:400,y1:400};",
    ctx, { filename: "seed3.js" }
  );
  ctx.gvBoxCommit();
  rec("Q5_hidden_not_selectable", "折叠域内节点不参与框选",
    !ctx.graphState.multiSel.has("n3") && ctx.graphState.multiSel.has("n1"),
    [...ctx.graphState.multiSel].join(","));
} catch (e) {
  rec("Q0_runtime", "Q 组可执行", false, e.message);
}

// ══════════ R 组：布局参数（真跑 gvLayoutWith，桩 cytoscape 捕获实参） ══════════
try {
  let captured = null;
  const ctx = mkCtx({
    window: null,
    cytoscape: (opts) => ({
      layout(o) { captured = o; return { run() {} }; },
      nodes() { return { forEach() {} }; },
      destroy() {},
    }),
  });
  ctx.window = ctx;
  ctx.cytoscape = ctx.cytoscape;
  vm.runInContext(extractFn(SRC24, "gvLayoutWith"), ctx, { filename: "gvLayoutWith.js" });
  const nodes = [{ id: "a" }, { id: "b" }];
  const edges = [{ source_id: "a", target_id: "b" }];

  ctx.gvLayoutWith("cose", nodes, edges, { sep: 9999, rep: 999999, iters: 99999 });
  const hi = captured || {};
  rec("R1_clamp_upper", "超上限被夹紧：斥力 60000 / 迭代 800 / 间距 200",
    hi.nodeRepulsion && hi.nodeRepulsion() === 60000 && hi.numIter === 800 && hi.idealEdgeLength === 210,
    [hi.nodeRepulsion && hi.nodeRepulsion(), hi.numIter, hi.idealEdgeLength].join("/"));

  captured = null;
  ctx.gvLayoutWith("cose", nodes, edges, { sep: 1, rep: 1, iters: 1 });
  const lo = captured || {};
  rec("R2_clamp_lower", "超下限被夹紧：斥力 10000 / 迭代 100 / 间距 40",
    lo.nodeRepulsion && lo.nodeRepulsion() === 10000 && lo.numIter === 100 && lo.idealEdgeLength === 50,
    [lo.nodeRepulsion && lo.nodeRepulsion(), lo.numIter, lo.idealEdgeLength].join("/"));

  captured = null;
  ctx.gvLayoutWith("dagre", nodes, edges, { sep: 120 });
  const dg = captured || {};
  rec("R3_dagre_uses_opts", "dagre 也吃参数（nodeSep=120 / rankSep=140）",
    dg.nodeSep === 120 && dg.rankSep === 140, dg.nodeSep + "/" + dg.rankSep);

  captured = null;
  vm.runInContext("graphState.view.layoutOpts={sep:150,rep:30000,iters:250};", ctx, { filename: "opts.js" });
  ctx.gvLayoutWith("cose", nodes, edges);   // 不传 opts ⇒ 读 graphState.view.layoutOpts
  const fromState = captured || {};
  rec("R4_reads_state_opts", "未传 opts 时读 graphState.view.layoutOpts",
    fromState.nodeRepulsion && fromState.nodeRepulsion() === 30000 && fromState.numIter === 250,
    [fromState.nodeRepulsion && fromState.nodeRepulsion(), fromState.numIter].join("/"));
} catch (e) {
  rec("R0_runtime", "R 组可执行", false, e.message);
}

process.stdout.write(JSON.stringify({ checks: checks }) + "\n");
