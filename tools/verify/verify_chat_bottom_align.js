// 2026-09-28 (B-0) 会话流下方（状态栏/输入区）与会话消息列宽度对齐 —— 真实浏览器验证
// 用法：node tools/verify/verify_chat_bottom_align.js
//
// 【为什么这样断言】
// 诉求：「会话流输出下方的内容宽度不合适…需要与会话流输出区域相同的宽度」
// 一稿曾按"#chat-area 左右各 24px padding"推出 968px（920+48）——**错的**：
//   .msg 边框盒=920、left=410；968px 的状态栏/输入区 left=393 → 两者左右各差 17~24px，根本没对齐。
// 正确参照物是 `#chat-area` 内被 `max-width:920px` 收窄的 **.msg**，不是通栏的 #chat-area。
// 且 #chat-area 是 overflow-y:auto，滚动条只吃右侧 → 消息列居中心线与下方元素差 ~6.5px（实测 7px）。
//
// 故判据 = **内容盒**（用户实际看到的文本/输入框边界）三者零偏差：
//   status/dock 的 content 宽/左右边界 == 最宽 .msg 的边框盒边界。
//   边框盒比 .msg 宽 78px（内衬 39×2）是**预期**的，不作断言对象。
// 三个状态都要成立：宽态 / 打开预览的窄态 / 恢复态。
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");

const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir(), PAY = ROOT + "/tmp/payloads/";
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `ba_${++seq}.out`), e = path.join(TMP, `ba_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = r => (r.stdout + r.stderr).trim();

let ok = 0, fail = 0; const fails = [];
function chk(tag, cond, detail = "") {
  if (cond) { ok++; console.log("  PASS  " + tag); }
  else { fail++; fails.push(tag); console.log("  FAIL  " + tag + (detail ? "   " + detail : "")); }
}

console.log("=".repeat(78));
console.log("B-0 会话流下方元素 ↔ 消息列 宽度对齐（内容盒零偏差；三态）");
console.log("=".repeat(78));

ab("open", "http://127.0.0.1:8000");
ab("set", "viewport", "1600", "900");
ab("set", "media", "light");
ab("wait", "--load", "networkidle");
ab("wait", "2500");

let data = null;
{
  const r = ab("eval", fs.readFileSync(PAY + "hb5_content_align.js", "utf8"));
  let v = show(r);
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  data = v;
}
if (!data || !data.step) { console.log("!! eval 未返回 JSON：", JSON.stringify(data).slice(0, 300)); process.exit(1); }

const states = Object.fromEntries(data.step);
if (data.err && data.err.length) console.log("err:", data.err);

for (const name of ["wide", "narrow", "restored"]) {
  const s = states[name];
  console.log("\n── %s ──", name);
  if (!s || !s.track) { chk(name + " 有测量值", false, "缺失"); continue; }
  console.log("  track w=%s l=%s r=%s | status w=%s l=%s r=%s | dock w=%s l=%s r=%s",
    s.track.w, s.track.l, s.track.r, s.status.w, s.status.l, s.status.r, s.dock.w, s.dock.l, s.dock.r);
  chk(name + " 状态栏内容宽 == 消息列宽", s.dW[0] === 0, "Δw=" + s.dW[0]);
  chk(name + " 输入区内容宽 == 消息列宽", s.dW[1] === 0, "Δw=" + s.dW[1]);
  chk(name + " 状态栏左边界对齐", s.dL[0] === 0, "Δl=" + s.dL[0]);
  chk(name + " 输入区左边界对齐", s.dL[1] === 0, "Δl=" + s.dL[1]);
  chk(name + " 状态栏右边界对齐", s.dR[0] === 0, "Δr=" + s.dR[0]);
  chk(name + " 输入区右边界对齐", s.dR[1] === 0, "Δr=" + s.dR[1]);
}

// 尺寸合理性：宽态应为 920（设计轨道），窄态应小于 920（预览挤压）
console.log("\n── 行为合理性 ──");
chk("宽态轨道为设计的 920px", states.wide && states.wide.track.w === 920, states.wide ? String(states.wide.track.w) : "");
chk("窄态轨道被预览挤压（<920）", states.narrow && states.narrow.track.w < 920, states.narrow ? String(states.narrow.track.w) : "");
chk("窄态内容整体左移（向左推动）", states.narrow && states.wide && states.narrow.track.l < states.wide.track.l,
  states.narrow && states.wide ? `${states.wide.track.l} -> ${states.narrow.track.l}` : "");
chk("恢复态回到 920px", states.restored && states.restored.track.w === 920, states.restored ? String(states.restored.track.w) : "");

const s1 = OUT + "verify_chat_bottom_align.png";
ab("screenshot", s1);
chk("截图已生成", fs.existsSync(s1), s1);

console.log("\n== 汇总: %d PASS / %d FAIL ==", ok, fail);
if (fails.length) console.log("失败项:", fails);
process.exit(fail ? 1 : 0);
