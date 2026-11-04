/* 编排卡「计划校验 + 重规划」渲染门禁（2026-11-04）
 *
 * 为什么要有这条：这两块此前**只进 SSE 事件与日志**，刷新页面就没了，
 * 事后无法回答"这轮计划合不合规""为什么少了几个任务"。补了 card_data 字段之后，
 * 必须证明前端**真的会渲染**、且**空态不渲染空壳**（否则又是一个"数据写了没人看"）。
 *
 * 口径：Node 离线渲染（不启浏览器）。被验的是 `renderOrchCard` 的真实模板字符串；
 * `esc`/`subtaskCardHtml` 用桩，`document` 只用于 onclick 里的 getElementById。
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
let bad = 0;
const check = (label, ok, detail) => {
  console.log((ok ? "  ✓ " : "  ✗ ") + label + (ok ? "" : "   " + (detail === undefined ? "" : detail)));
  if (!ok) bad++;
};

// ── 1. 数据源：流式卡片必须真的带上这两段 ──────────────────────────────
const streamSrc = fs.readFileSync(path.join(ROOT, "agent/pipeline_parts/stream.py"), "utf8");
check("1.1 流式卡片写入 plan_guard", streamSrc.includes('"plan_guard": _plan_guard_info'));
check("1.2 流式卡片写入 replan", streamSrc.includes('"replan": _replan_events'));
check("1.3 早停决策进 _replan_events", streamSrc.includes('"action": "early_stop"')
      && streamSrc.includes("_replan_events.append"));
check("1.4 非流式编排卡片同样带这两段",
      (() => {
        const orch = fs.readFileSync(path.join(ROOT, "agent/pipeline_parts/orchestration.py"), "utf8");
        return orch.includes('"plan_guard"') && orch.includes('"replan"');
      })());
// 既有缺陷回归：`_gate_gaps` 只在"有成功子任务"的分支里赋值，零交付时会 UnboundLocalError，
// 整轮以 error 收尾（实测会话 10028）。必须有兜底。
check("1.5 零交付时的 `_gate_gaps` 兜底存在（防 UnboundLocalError 整轮报错）",
      streamSrc.includes("except NameError:") && streamSrc.includes("_gate_gaps = []"));
// 初始化必须**早于**赋值：曾把 `_plan_guard_info = None` 放在建计划之后，
// 结果把校验结果覆盖成 None（会话 10029 实测：日志无违规、卡片字段却为 null）。
check("1.6 `_plan_guard_info` 的初始化在赋值之前（不得覆盖）",
      streamSrc.indexOf("_plan_guard_info = None") > 0
      && streamSrc.indexOf("_plan_guard_info = None") < streamSrc.indexOf("_plan_guard_info = _pg"));

// ── 2. 渲染：注入样例，断言关键文案 ────────────────────────────────────
const src = fs.readFileSync(path.join(ROOT, "static/js/mods/06-cards.js"), "utf8");
const stubs = {
  esc: s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  subtaskCardHtml: p => `<div class="st-row">${p.key || ""}</div>`,
  document: { getElementById: () => null },
  window: {},
};
const api = new Function("esc", "subtaskCardHtml", "document", "window", "console",
  src + "\nreturn {renderOrchCard};")(stubs.esc, stubs.subtaskCardHtml, stubs.document, stubs.window, console);

const card = {
  plan: [{ key: "t1", status: "done" }, { key: "t2", status: "done" }, { key: "t3", status: "cancelled" }],
  orchestrated: true, orchestrated_status: "full",
  plan_guard: {
    violations: [
      { code: "stage.required_missing", severity: "warn",
        message: "计划缺少必需阶段：model_release", fix: "补派该阶段" },
      { code: "stage.order_reversed", severity: "error",
        message: "阶段逆序：view_expansion 早于 architecture_skeleton", fix: "调整 deps" },
    ],
  },
  replan: [
    { action: "none", reason: "巡检未超阈（M1 0%/M2 0/M4 n/a）⇒ 计划仍健康" },
    { action: "early_stop", reason: "required 产出已全部达成（t1,t2）⇒ 提前收敛", skipped: ["t3"] },
  ],
};
const html = api.renderOrchCard(card);

check("2.1 chip 同时报两段数量", html.includes("计划校验 2 项（1 项严重）")
      && html.includes("执行中调整 1 次"), "");
check("2.2 严重项用 ✗ 且透出 message", html.includes("✗") && html.includes("阶段逆序"));
check("2.3 告警项用 ! 且透出 message", html.includes("!") && html.includes("计划缺少必需阶段"));
check("2.4 修复建议（fix）可见", html.includes("补派该阶段"));
check("2.5 早停如实列出被放弃的任务", html.includes("提前收敛") && html.includes("放弃 t3"));
check("2.6 action=none 也留痕（为什么不动同样可见）", html.includes("计划仍健康"));
check("2.7 明细默认收起（不霸屏）", html.includes("display:none"));

// 空态抑制：没有这两段 ⇒ 不得出现 🧭 空壳
const html2 = api.renderOrchCard({ plan: [{ key: "t1", status: "done" }], orchestrated: true });
check("2.8 无校验/无重规划 ⇒ 不渲染该行", !html2.includes("🧭"));

// 只有 none（计划健康）时：不应报"执行中调整 N 次"（避免把"没动"说成"动过"）
const html3 = api.renderOrchCard({ plan: [{ key: "t1", status: "done" }], orchestrated: true,
  replan: [{ action: "none", reason: "计划仍健康" }] });
check("2.9 只有 none ⇒ 不宣称「执行中调整」", !html3.includes("执行中调整") && html3.includes("🧭"));

console.log();
console.log(bad === 0 ? "ALL PASS（9 项）" : `FAIL = ${bad}`);
process.exit(bad === 0 ? 0 : 1);
