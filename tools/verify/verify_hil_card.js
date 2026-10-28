/* HIL 确认卡真实浏览器验证（按 mbse-ui-browser-verify 技能模板）
 *
 * 关键纪律：
 *   1. spawnSync 必须用 fd 接管 stdio（坑 4：守护进程持有管道 ⇒ 每次等到 timeout）
 *   2. 一个进程内串行 open → eval → screenshot → close（open 与 eval 分开调会页面空白）
 *   3. 结论写 UTF-8 文件（坑 5：shell 回传会按 GBK 解码中文）
 */
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");

const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/";
const PAY = ROOT + "/tools/verify/ui_payloads/";   // 2026-10-28 沉淀：从 tmp/ 移入 tools/verify（可复跑）
const TMP = os.tmpdir();
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `ab_hil_${++seq}.out`), e = path.join(TMP, `ab_hil_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = (r) => (r.stdout + r.stderr).trim();

function run(name) {
  const r = ab("eval", fs.readFileSync(PAY + name, "utf8"));
  let v = show(r);
  for (let i = 0; i < 4 && typeof v === "string"; i++) {
    try { v = JSON.parse(v); } catch (_) { break; }
  }
  return v;
}

const log = [];
const T = (label, v) => log.push({ label, result: v });
const shot = (n) => {
  ab("screenshot", OUT + n + ".png");
  const p = OUT + n + ".png";
  log.push({ shot: n + ".png", ok: fs.existsSync(p), bytes: fs.existsSync(p) ? fs.statSync(p).size : 0 });
};

const t0 = Date.now();
ab("open", "http://127.0.0.1:8000");
ab("set", "viewport", "1600", "900");
ab("set", "media", "light");
ab("wait", "--load", "networkidle");
ab("wait", "3000");

// ① 夹具
T("t0_fixture", run("hil_t0_fixture.js"));
shot("hil_00_fixture");

// ② 渲染细节
T("t1_render", run("hil_t1_render.js"));
shot("hil_01_cards");

// ③ 接线（真实 SSE 文本）
T("t2_wire", run("hil_t2_wire.js"));
shot("hil_02_wired");

// ④ 像素/布局（DOM 全绿 ≠ 用户看得见）
T("t3_pixels", run("hil_t3_pixels.js"));
shot("hil_03_pixels");

log.push({ pageErrors: show(ab("errors")).slice(-2000), consoleTail: show(ab("console")).slice(-1500) });
ab("close");

log.push({ elapsedMs: Date.now() - t0 });
fs.writeFileSync(OUT + "_verify_hil.json", JSON.stringify(log, null, 1), "utf8");
process.stdout.write("DONE\n");
