/* 全路径验证 v2：eval 只做短动作，等待循环放 node 侧（避免单次 eval 超时） */
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir();
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `ab_${++seq}.out`), e = path.join(TMP, `ab_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const t0 = Date.now();
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 60000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  fs.unlinkSync(o); fs.unlinkSync(e);
  return { out: (stdout + stderr).trim(), ms: Date.now() - t0 };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const JS = (code) => {
  let v = ab("eval", code).out;
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  return v;
};

(async () => {
  const log = [];
  ab("open", "http://127.0.0.1:8000");
  ab("set", "viewport", "1600", "900");
  ab("wait", "--load", "networkidle");
  ab("wait", "2500");

  // 步骤1：填入并发送（短 eval）
  const sent = JS(`(async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let input = null;
    for (let i = 0; i < 10 && !input; i++) { input = document.getElementById('chat-input'); if (!input) await sleep(1000); }
    if (!input) return JSON.stringify({ error: 'no chat-input', bodyLen: document.body.textContent.trim().length });
    input.value = '\u5206\u6790\u4e00\u4e0b\u5f53\u524d\u6a21\u578b\u7684\u8986\u76d6\u6027\uff0c\u7ed9\u51fa\u8986\u76d6\u7387\u4e0e\u4e3b\u8981\u7f3a\u9879';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(300);
    if (typeof sendChat !== 'function') return JSON.stringify({ error: 'no sendChat' });
    sendChat();
    return JSON.stringify({ sent: true });
  })()`);
  log.push({ sent });

  // 步骤2：node 侧轮询（每次短 eval 读最后一条消息）
  let text = "", stable = 0, waited = 0;
  const readJs = `(() => {
    const msgs = document.querySelectorAll('.msg');
    return JSON.stringify({ n: msgs.length, t: msgs.length ? msgs[msgs.length - 1].textContent.slice(-3000) : '' });
  })()`;
  while (waited < 170000) {
    await sleep(6000); waited += 6000;
    const r = JS(readJs);
    const t = (r && r.t) || "";
    if (t.length > text.length + 5) { text = t; stable = 0; } else if (t) { stable += 1; }
    if ((t.indexOf('\u8986\u76d6\u7387') >= 0 || t.indexOf('\u7f3a\u9879') >= 0) && stable >= 3) break;
  }
  log.push({ waitedSec: waited / 1000, replyLen: text.length,
    hasCoverageKw: text.indexOf('\u8986\u76d6\u7387') >= 0,
    hasGapKw: text.indexOf('\u672a\u8986\u76d6') >= 0 || text.indexOf('\u7f3a\u9879') >= 0,
    hasSeventyTwo: text.indexOf('72.7') >= 0,
    hasRealPercent: /\d{1,3}(\.\d+)?%/.test(text),
    replyTail: text.slice(-800) });

  ab("screenshot", OUT + "verify_coverage_e2e.png");
  log.push({ shot: fs.existsSync(OUT + "verify_coverage_e2e.png") });
  log.push({ pageErrors: ab("errors").out.slice(-400) });
  ab("close");
  fs.writeFileSync(OUT + "_verify_coverage_e2e.json", JSON.stringify(log, null, 1), "utf8");
  console.log("DONE");
})();
