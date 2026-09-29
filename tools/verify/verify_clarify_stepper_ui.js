// 2026-09-28：AI 任务优化两项改动的真实浏览器验证
//   C-1 澄清卡 stepper（一次一题 / 其他才展开输入框 / 末步补充说明多行）
//   C-2 会话内容居中收窄 + 打开预览时向左推动
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir(), PAY = ROOT + "/tmp/payloads/";
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `ab_${++seq}.out`), e = path.join(TMP, `ab_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = (r) => (r.stdout + r.stderr).trim();

function run(name, subs) {
  let src = fs.readFileSync(PAY + name, "utf8");
  (subs || []).forEach(([k, v]) => { src = src.split(k).join(v); });
  const r = ab("eval", src);
  let v = show(r);
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  return v;
}

const log = [];
const T = (label, v) => log.push({ label, result: v });
const shot = (n) => { ab("screenshot", OUT + n + ".png"); log.push({ shot: n + ".png", ok: fs.existsSync(OUT + n + ".png") }); };

const qs = fs.readFileSync(PAY + "_qs337.json", "utf8").trim();

ab("open", "http://127.0.0.1:8000");
ab("set", "viewport", "1600", "900");
ab("set", "media", "light");
ab("wait", "--load", "networkidle");
ab("wait", "2500");

// ── C-1：澄清卡 stepper（历史还原真实路径，题目取自真库 会话337/消息2790）──
T("T1 澄清卡 stepper", run("tc1_clarify_stepper.js", [["NEWQS", qs]]));
shot("verify_cl_01_stepper");

// 出图：手动再展开「其他」输入框 + 切到补充说明步，各留一张
ab("eval", `(async()=>{const s=document.querySelector('.clarify-ask-card.hist');
 if(!s) return 'no'; const st=s.querySelectorAll('.clarify-step');
 const c=st[0].querySelector('input[value="__custom__"]'); if(c) c.click();
 const i=st[0].querySelector('.cq-custom'); if(i) i.value='PlantUML，另附端口与值属性';
 return 'ok';})()`);
ab("wait", "400");
shot("verify_cl_02_custom_input");

ab("eval", `(async()=>{const s=document.querySelector('.clarify-ask-card.hist');
 if(!s) return 'no'; const n=s.querySelector('.cq-next');
 for(let k=0;k<3;k++){ if(n) n.click(); await new Promise(r=>setTimeout(r,180)); }
 const t=s.querySelector('.cq-note'); if(t) t.value='补充：按 EV 乘用车热管理口径建模';
 return 'ok';})()`);
ab("wait", "400");
shot("verify_cl_03_note_step");

// ── C-2：居中收窄 + 预览展开向左推动 ──
T("T2 布局 居中收窄/向左推动", run("tc2_layout_push.js"));
shot("verify_cl_04_narrow_centered");

ab("eval", `(async()=>{const p=document.getElementById('preview-panel');
 if(p && p.classList.contains('collapsed')) togglePreview(); return 'ok';})()`);
ab("wait", "900");
shot("verify_cl_05_preview_open_pushed_left");

log.push({ pageErrors: show(ab("errors")), consoleTail: show(ab("console")).slice(-1000) });
ab("close");
fs.writeFileSync(OUT + "_verify_clarify_layout.json", JSON.stringify(log, null, 1), "utf8");
console.log("DONE");
