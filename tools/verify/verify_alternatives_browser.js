/* 多方案对比卡 · 真实浏览器验证（FR-MG-6 平台化）—— 常驻门禁
 *
 * 判据：真实打开会话 → 断言对比表在 DOM 里、**在消息级**（不在默认收起的执行过程区）、
 *        且**用户看得见**（rect 有尺寸 + 在视口内）。
 *
 * 用法（bash；node 不吃 -X utf8）：
 *   export PATH="/c/Users/gefei/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"
 *   cd "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system"
 *   node tools/verify/verify_alternatives_browser.js
 *
 * 前置：服务在跑（http://127.0.0.1:8000），且会话里有一次 alternatives_score 调用。
 * ⚠️ 改过 static/ 下的 JS **不需要重启**（no-cache 直读）；改过 .py **必须重启**。
 * ⚠️ 坑 4：必须用文件描述符接管 stdout/stderr（agent-browser 守护进程持有管道 ⇒ 永远等不到 EOF）。
 * ⚠️ 坑 5：结论写 UTF-8 文件再用 Read 工具读（PowerShell/控制台按 GBK 解码会乱码）。
 */
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir();
let seq = 0;

/* ⚠️ 坑 4：必须用文件描述符接管 stdout/stderr —— agent-browser 守护进程会继承
   管道并一直持有，spawnSync 永远等不到管道 EOF（每次都空等到 timeout）。 */
function ab(...args) {
  const o = path.join(TMP, `ab_${++seq}.out`), e = path.join(TMP, `ab_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  fs.unlinkSync(o); fs.unlinkSync(e);
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = (r) => (r.stdout + r.stderr).trim();

const PAY = ROOT + "/tools/verify/_alt_payload_msglevel.js";
function runPayload() {
  const r = ab("eval", fs.readFileSync(PAY, "utf8"));
  let v = show(r);
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  return v;
}

const log = [];
const shot = (n) => {
  ab("screenshot", OUT + n + ".png");
  log.push({ shot: n + ".png", ok: fs.existsSync(OUT + n + ".png") });
};

ab("open", "http://127.0.0.1:8000");
ab("set", "viewport", "1600", "900");
ab("set", "media", "light");
ab("wait", "--load", "networkidle");
ab("wait", "2500");

const r = runPayload();
log.push({ label: "对比表断言", result: r });
shot("verify_alt_table");

log.push({ pageErrors: show(ab("errors")).slice(-800),
           consoleTail: show(ab("console")).slice(-1000) });
ab("close");
fs.writeFileSync(OUT + "_verify_alt.json", JSON.stringify(log, null, 1), "utf8");

// 断言结果直接落 stdout（ASCII，PowerShell 不会乱码）；中文细节读 JSON 文件
const ok = r && r.msgLevelTables >= 1 && r.hasSize === true &&
           r.inViewport === true && r.dimRows === 5 && r.hasRecBadge === true;
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
