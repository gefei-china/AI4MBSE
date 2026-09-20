/* 全路径验证 v4：断言修正版（*= 包含匹配 + 直接中文字符 + 点第一个预览按钮） */
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir();
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `ab_${++seq}.out`), e = path.join(TMP, `ab_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 60000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  return { out: (fs.readFileSync(o, "utf8") + fs.readFileSync(e, "utf8")).trim() };
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
  ab("wait", "3000");

  // 会话状态：上一轮 v3 已发过覆盖性问题，历史消息应含富组件
  let st = JS(`(() => JSON.stringify({
    msgs: document.querySelectorAll('.msg').length,
    covBtns: document.querySelectorAll('[onclick*="covOpenInPreview"]').length,
    covV10: document.body.innerHTML.indexOf('cov-v1.0') >= 0
  }))()`);
  log.push({ initial: st });

  // 若当前无富组件（会话切换了），重新发一条
  if (!st || st.covBtns === 0) {
    const sent = JS(`(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      let input = null;
      for (let i = 0; i < 10 && !input; i++) { input = document.getElementById('chat-input'); if (!input) await sleep(1000); }
      if (!input) return JSON.stringify({ error: 'no chat-input' });
      input.value = '分析一下当前模型的覆盖性，给出覆盖率与主要缺项';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await sleep(300);
      sendChat();
      return JSON.stringify({ sent: true });
    })()`);
    log.push({ resent: sent });
    let waited = 0, stable = 0, lastLen = 0;
    while (waited < 170000) {
      await sleep(6000); waited += 6000;
      const r = JS(`(() => { const m = document.querySelectorAll('.msg'); return JSON.stringify({ len: m.length ? m[m.length-1].textContent.length : 0 }); })()`);
      const len = (r && r.len) || 0;
      if (len > lastLen + 5) { lastLen = len; stable = 0; } else if (len) { stable += 1; }
      const has = JS(`(() => JSON.stringify({ n: document.querySelectorAll('[onclick*="covOpenInPreview"]').length }))()`);
      if (has && has.n > 0 && stable >= 3) break;
    }
    st = JS(`(() => JSON.stringify({ covBtns: document.querySelectorAll('[onclick*="covOpenInPreview"]').length }))()`);
    log.push({ afterWait: st, waitedSec: waited / 1000 });
  }

  // 展开全部过程卡 → 截图对话流富组件
  JS(`(() => { document.querySelectorAll('.proc-block.collapsed .proc-head').forEach(h => h.click()); return '1'; })()`);
  await sleep(800);
  const chatState = JS(`(() => JSON.stringify({
    covBtns: document.querySelectorAll('[onclick*="covOpenInPreview"]').length,
    covV10: document.body.innerHTML.indexOf('cov-v1.0') >= 0,
    relLegend: document.body.innerHTML.indexOf('SATISFIES') >= 0,
    p1Badge: document.body.innerHTML.indexOf('P1') >= 0
  }))()`);
  log.push({ chatState });
  ab("screenshot", OUT + "verify_cov_card_chat.png");

  // 点第一个预览按钮 → 断言预览面板
  const prev = JS(`(() => {
    const btn = document.querySelector('[onclick*="covOpenInPreview"]');
    if (!btn) return JSON.stringify({ error: 'no preview btn' });
    btn.click();
    return JSON.stringify({ clicked: true });
  })()`);
  await sleep(1200);
  const pvState = JS(`(() => {
    const pp = document.getElementById('preview-panel');
    const c = document.getElementById('preview-content');
    const t = document.getElementById('preview-title');
    return JSON.stringify({
      panelOpen: !!pp && !pp.classList.contains('collapsed'),
      title: t ? t.textContent : '',
      hasCard: !!c && c.innerHTML.indexOf('cov-v1.0') >= 0,
      contentLen: c ? c.innerHTML.length : 0
    });
  })()`);
  log.push({ prev, pvState });
  ab("screenshot", OUT + "verify_cov_card_preview.png");
  log.push({ pageErrors: ab("errors").out.slice(-300) });
  ab("close");
  fs.writeFileSync(OUT + "_verify_cov_card.json", JSON.stringify(log, null, 1), "utf8");
  console.log("DONE");
})();
