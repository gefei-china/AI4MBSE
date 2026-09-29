// 澄清卡 stepper 的**变异自证**（node 单进程版，与 shots_clarify.js 同构，避开 python 子进程被 SIGTERM）
//   基线 → M1「一次铺开」还原 → M2「输入框常显」还原，判据必须分别在 M1/M2 下转 FAIL。
//   变异只在**页面内**对已渲染的卡施加（源码零改动），故无需还原源码。
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir();
let seq = 0;
function ab(...args) {
  const o = path.join(TMP, `ab_${++seq}.out`), e = path.join(TMP, `ab_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args], { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { text: (stdout + stderr).trim(), status: r.status, err: r.error ? String(r.error) : "" };
}
function ev(src) {
  let v = ab("eval", src).text;
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  return v;
}

const BUILD = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const MUT = "MUTATE";
  const host = document.getElementById('chat-area');
  const old = document.getElementById('verify-clarify'); if (old) old.remove();
  const holder = document.createElement('div');
  holder.className = 'msg ai'; holder.id = 'verify-clarify';
  holder.innerHTML = '<div class="msg-inner"><div class="body"></div></div>';
  host.appendChild(holder);
  const QS = [
    {id:'q1', question:'BDD 希望以哪种形式交付？', options:['PlantUML','Mermaid','SysML v2 文本'], allow_custom:true},
    {id:'q2', question:'是否需要接口与定量参数？', options:['需要','不需要'], allow_custom:true}
  ];
  holder.querySelector('.body').innerHTML =
    '<div class="clarify-ask-card" id="clarify-ask">' + clarifyCardInnerHtml(QS, '') + '</div>';
  const card = holder.querySelector('.clarify-ask-card');
  if (typeof clarifyGo === 'function') clarifyGo(card, 0);
  await sleep(250);
  const steps = Array.from(card.querySelectorAll('.clarify-step'));
  if (MUT === 'M1_flatten') { steps.forEach(s => { s.style.display = ''; }); }
  if (MUT === 'M2_custom_always') { card.querySelectorAll('.cq-custom-box').forEach(b => { b.style.display = ''; }); }
  const vis = steps.filter(s => getComputedStyle(s).display !== 'none').length;
  const box = steps[0].querySelector('.cq-custom-box');
  const inp = steps[0].querySelector('.cq-custom');
  const A1 = (vis === 1);
  const A2 = getComputedStyle(box).display === 'none' && inp.offsetParent === null;
  let posted = false;
  const _api = window.api;
  window.api = async () => { posted = true; return {ok:true, resume_text:'x'}; };
  const _t = window.toast; window.__toasts = [];
  window.toast = (m) => { window.__toasts.push(String(m)); };
  if (typeof clarifyAnswerSend === 'function') await clarifyAnswerSend();
  await sleep(250);
  const A3 = (!posted) && (window.__toasts || []).length > 0;
  window.api = _api; window.toast = _t;
  holder.remove();
  return JSON.stringify({ mut: MUT, visSteps: vis, A1_once_at_a_time: A1, A2_custom_hidden: A2, A3_guard_blocks: A3 });
})()`;

const log = [];
ab("open", "http://127.0.0.1:8000");
ab("set", "viewport", "1600", "900");
ab("set", "media", "light");
ab("wait", "--load", "networkidle");
ab("wait", "2500");
ab("eval", "if(typeof go==='function') go('ai');");
ab("wait", "1200");

const cases = [["基线（无变异）", "NONE"], ["M1 还原「一次铺开」", "M1_flatten"], ["M2 还原「输入框常显」", "M2_custom_always"]];
const res = {};
for (const [label, m] of cases) {
  const v = ev(BUILD.split("MUTATE").join(m));
  res[m] = v;
  log.push({ label, mut: m, result: v });
}
log.push({ pageErrors: ab("errors").text });
ab("close");

const fails = [];
const base = res["NONE"] || {};
["A1_once_at_a_time", "A2_custom_hidden", "A3_guard_blocks"].forEach(k => {
  if (base[k] !== true) fails.push(`基线 ${k} 未通过（判据/夹具自身有问题）`);
});
if ((res["M1_flatten"] || {}).A1_once_at_a_time !== false) fails.push("M1（一次铺开）未被 A1 抓住 → 空转断言");
if ((res["M2_custom_always"] || {}).A2_custom_hidden !== false) fails.push("M2（输入框常显）未被 A2 抓住 → 空转断言");
log.push({ verdict: fails.length ? "FAIL" : "PASS", fails, note: "变异在页面内施加，源码零改动，无需还原" });
fs.writeFileSync(OUT + "_verify_clarify_mutate.json", JSON.stringify(log, null, 1), "utf8");
console.log("DONE");
