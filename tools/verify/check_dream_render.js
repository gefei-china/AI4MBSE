/* eslint-disable */
// 离线渲染留证（2026-11-12）：Chrome 本机 os error 14001 起不来时的降级通道。
// 纪律（同 mbse-ui-browser-verify 技能）：
//   1. 44-memory.js **原样内联**，只 stub 外部依赖（api / esc / toast / confirmDialog / DOM）；
//   2. 数据取**运行中服务的真实接口**返回，不手写夹具；
//   3. 渲染入口 memDreamPreview() / memDreamAB() 真跑一遍；
//   4. 夹具完整性守卫：接口缺关键字段直接 exit 1。
// 局限：证明布局与数据绑定正确，**证明不了真实浏览器里的层叠/可见性/交互**。
//       本次无真实截图，交付时必须如实说明。
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const BASE = "http://127.0.0.1:8000";
const OUT = path.join(ROOT, "outputs", "_verify_dream_render.json");

const nodes = {};
function mkNode(id) {
  return {
    id, innerHTML: "", textContent: "", value: "", disabled: false,
    style: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    getAttribute() { return null; }, setAttribute() {},
    appendChild() {}, remove() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
  };
}
["mem-dream-cands", "mem-dream-summary", "mem-dream-fp",
 "mem-dream-apply-btn", "mem-table"].forEach(id => { nodes[id] = mkNode(id); });

let previewData = null, abData = null;
const calls = [];

const sandbox = {
  console, setTimeout, clearTimeout, URLSearchParams, JSON, Math, Date,
  document: {
    getElementById: id => nodes[id] || null,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: () => mkNode("tmp"),
    body: mkNode("body"),
  },
  window: {}, localStorage: { getItem: () => null, setItem() {} },
  Blob: function () {}, URL: { createObjectURL: () => "", revokeObjectURL() {} },
  // 外部依赖：真实接口数据（按 URL 精确分流，不放行未知路径）
  api: async (p) => {
    calls.push(p);
    if (/^\/api\/memory\/dream\/preview(\?|$)/.test(p)) return previewData;
    if (/^\/api\/memory\/dream\/ab-report(\?|$)/.test(p)) return abData;
    throw new Error("夹具未覆盖的路径：" + p);
  },
  esc: s => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
  toast: () => {}, toastErr: () => {},
  confirmDialog: async () => true,
  loadMemories: () => {}, memFilterQ: () => {},
  // api() 对象风格在本模块只用 toast.error，这里补一层
};
sandbox.toast.error = () => {};
sandbox.window = sandbox;

async function main() {
  const log = { mode: "offline-dom-render", real_screenshot: false };
  const rp = await fetch(BASE + "/api/memory/dream/preview?note_limit=300");
  previewData = await rp.json();
  const ra = await fetch(BASE + "/api/memory/dream/ab-report");
  abData = await ra.json();

  // 夹具完整性守卫：渲染代码要读的字段，缺一个就 exit 1（别让缺字段伪装成渲染缺陷）
  const needP = ["fingerprint", "note_limit", "stats", "candidates"];
  const needA = ["measured", "measured_at", "arm_a_baseline",
                 "arm_b_shadow_hypothetical", "delta", "shadow_states"];
  const missP = needP.filter(k => !(k in previewData));
  const missA = needA.filter(k => !(k in abData));
  log.fixture = { previewKeys: Object.keys(previewData).slice(0, 12),
                  abMeasured: abData.measured, missingPreview: missP, missingAB: missA };
  if (missP.length || missA.length || !abData.measured) {
    log.error = "夹具不完整，拒绝继续";
    fs.writeFileSync(OUT, JSON.stringify(log, null, 1), "utf8");
    return 1;
  }

  const src = fs.readFileSync(path.join(ROOT, "static/js/mods/44-memory.js"), "utf8");
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "44-memory.js" });

  // 渲染入口真跑
  await sandbox.memDreamPreview();
  const sumP = nodes["mem-dream-summary"].innerHTML;
  const cands = nodes["mem-dream-cands"].innerHTML;
  const fp = nodes["mem-dream-fp"].textContent;
  log.preview = {
    summaryHasScan: sumP.indexOf("扫描") >= 0,
    summaryHasReview: sumP.indexOf("需人工") >= 0,
    summaryHasStatsNum: /\d+/.test(sumP),
    fingerprintShown: fp.indexOf("指纹") >= 0 && fp.length > 12,
    candsFilled: cands.trim().length > 0,
    noUndefinedNaN: !/undefined|NaN/.test(sumP + cands),
    applyBtnDisabled: nodes["mem-dream-apply-btn"].disabled,
    candsHead: cands.slice(0, 100),
  };

  await sandbox.memDreamAB();
  const sumA = nodes["mem-dream-summary"].innerHTML;
  log.ab = {
    hasTitle: sumA.indexOf("双臂影子评测") >= 0,
    hasNumeric: /\d+\.\d+/.test(sumA),
    hasShadowNote: sumA.indexOf("影子") >= 0,
    hasUnitNote: sumA.indexOf("字符") >= 0,
    noUndefinedNaN: !/undefined|NaN/.test(sumA),
    tail: sumA.slice(0, 180),
  };
  log.apiCalls = calls;

  const oks = [
    log.preview.summaryHasScan, log.preview.summaryHasReview,
    log.preview.summaryHasStatsNum, log.preview.fingerprintShown,
    log.preview.candsFilled, log.preview.noUndefinedNaN,
    log.ab.hasTitle, log.ab.hasNumeric, log.ab.hasShadowNote,
    log.ab.hasUnitNote, log.ab.noUndefinedNaN,
  ];
  log.passed = oks.filter(Boolean).length;
  log.total = oks.length;
  log.allGreen = log.passed === log.total;
  log.limitation = "离线 DOM 渲染：证明布局与数据绑定正确；"
    + "未验证真实浏览器层叠/可见性/交互，本次无真实截图"
    + "（Chrome 启动 os error 14001，环境级问题，按技能纪律不重试）。";
  fs.writeFileSync(OUT, JSON.stringify(log, null, 1), "utf8");
  return log.allGreen ? 0 : 1;
}

main().then(rc => process.exit(rc)).catch(e => {
  fs.writeFileSync(OUT, JSON.stringify({ error: String(e && e.stack || e) }, null, 1), "utf8");
  process.exit(1);
});
