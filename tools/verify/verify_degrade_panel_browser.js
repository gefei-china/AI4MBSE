// CI-OPTIONAL: [local] tier=local —— 真实浏览器门禁：需本机 Chrome/Edge + 8000 真服务
/**
 * P0「降级在执行过程中可见」**真实浏览器**验收（2026-11-25）。
 *
 * 验两件事（分别对应用户要求的"前端执行过程中要体现出来"与"全局可查"）：
 *   A. 执行过程卡片：`procAddDegrade()` 真的能把一条降级渲染成时间线卡片 ——
 *      critical 红色不收起、degraded 橙色带 ×N、cosmetic **不建卡**只计数
 *      （否则纯观测失败会刷屏，与"分级"的初衷相反）；
 *      收口条要出现「N 处结果不可信 / N 处降级」的汇总。
 *   B. 运行监控看板：`loadDegradePanel()` 把 /api/health/degrade 渲染成三档计数 + 排行。
 *
 * 为什么必须用真实浏览器：这两个函数全靠 DOM 操作（procUpsert → procRender → innerHTML），
 * 静态检查只能证明"函数存在"，证明不了"用户看得见"。本仓已归档多次"改了但页面没变"。
 *
 * 跑法：node tools/verify/verify_degrade_panel_browser.js [--url http://127.0.0.1:8000]
 * 复用 verify_ont_runtime_browser.js 的 CDP 范式（spawn + stdio ignore，
 * 绕开 agent-browser 守护进程管 stdio 导致 spawnSync 卡死的坑）。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const args = process.argv.slice(2);
const i = args.indexOf('--url');
const urlArg = i >= 0 ? args[i + 1] : 'http://127.0.0.1:8000';
const CHROME_CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];
let pass = 0, fail = 0;
function check(id, name, cond, detail) {
  if (cond) { pass++; console.log(`  [PASS] ${id} ${name}${detail ? '  — ' + detail : ''}`); }
  else { fail++; console.log(`  [FAIL] ${id} ${name}${detail ? '  — ' + detail : ''}`); }
}
function findChrome() { for (const p of CHROME_CANDIDATES) if (fs.existsSync(p)) return p; return null; }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function cdp(ws, method, params, id) {
  return new Promise((resolve, reject) => {
    const onMsg = ev => {
      let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.id === id) { ws.removeEventListener('message', onMsg); resolve(m); }
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
    setTimeout(() => { ws.removeEventListener('message', onMsg); reject(new Error('cdp timeout ' + method)); }, 30000);
  });
}
async function evalJs(ws, expr, id, awaitPromise) {
  const r = await cdp(ws, 'Runtime.evaluate',
    { expression: expr, returnByValue: true, awaitPromise: !!awaitPromise }, id);
  return r && r.result && r.result.result ? r.result.result.value : undefined;
}

(async () => {
  const chrome = findChrome();
  if (!chrome) { console.log('⚠️ 未找到 Chrome/Edge，跳过'); process.exit(0); }
  try {
    const r = await fetch(urlArg + '/api/health/degrade?limit=5');
    if (!r.ok) throw new Error('health/degrade ' + r.status);
  } catch (e) { console.log('⚠️ 后端不可达，跳过：' + e.message); process.exit(0); }

  const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_dgbr_'));
  const port = 9381;
  const proc = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    '--remote-debugging-port=' + port, '--user-data-dir=' + udd,
    '--window-size=1680,1200', 'about:blank',
  ], { stdio: 'ignore' });

  let ws = null;
  try {
    let list = null;
    for (let k = 0; k < 40; k++) {
      await sleep(300);
      try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); list = await r.json(); if (list && list.length) break; } catch (_) {}
    }
    if (!list || !list.length) throw new Error('调试端口未就绪');
    const target = list.find(t => t.type === 'page') || list[0];
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); setTimeout(() => rej(new Error('ws open timeout')), 10000); });

    await cdp(ws, 'Page.enable', {}, 1);
    await cdp(ws, 'Runtime.enable', {}, 2);
    await cdp(ws, 'Page.navigate', { url: urlArg + '/static/index.html' }, 3);
    await sleep(4500);

    // ── A. 执行过程卡片 ────────────────────────────────────────────────
    const boot = await evalJs(ws, `JSON.stringify({
      add: typeof procAddDegrade==='function',
      panel: typeof loadDegradePanel==='function',
      proc: typeof procRender==='function'})`, 4);
    const b = JSON.parse(boot || '{}');
    check('A1', 'procAddDegrade / loadDegradePanel 已加载', b.add && b.panel && b.proc, boot);

    // 建一个 proc 容器（真实页面里由 pumpStream 创建；此处模拟执行中状态）
    // ★ 注入到**主内容区顶部**并滚到可见 —— 只加到 body 末尾的话截图落在页面外，
    //   出图会看不出任何东西（"断言过了但图上看不见"最容易骗过自己）。
    await evalJs(ws, `(()=>{try{resetPipeline();}catch(e){}
      if(!document.getElementById('proc-box')){
        const d=document.createElement('div'); d.id='proc-box';
        d.style.cssText='width:900px;margin:16px auto;position:relative;z-index:9999;';
        const host=document.querySelector('.page.active')||document.querySelector('#app')||document.body;
        host.insertBefore(d, host.firstChild);
      }
      return 'ok';})()`, 5);

    await evalJs(ws, `(()=>{
      procAddDegrade({where:'graph_db.get_writer.all_down', level:'critical', count:1,
        impact:'图库两个后端均不可用，已落内存 Mock：本轮图检索与图写入结果无效',
        error:'RuntimeError: RocksDB lock failed'});
      procAddDegrade({where:'rag.knowledge_categories', level:'degraded', count:3,
        impact:'知识类别读取失败：设计方法知识/设计资产未能区分',
        error:'OperationalError: no such table'});
      procAddDegrade({where:'metrics_core.metric_trend', level:'cosmetic', count:5});
      return 'ok';})()`, 6);
    await sleep(600);

    const card = await evalJs(ws, `JSON.stringify((()=>{
      const cards=[...document.querySelectorAll('.proc-block.degrade')];
      const crit=document.querySelector('.proc-block.degrade.deg-critical');
      const sum=document.querySelector('.proc-summary');
      const soft=cards.find(c=>!c.classList.contains('deg-critical'));
      return {
        n:cards.length,
        hasCrit: !!crit,
        critCollapsed: crit ? crit.classList.contains('collapsed') : null,
        // 底色用的是 linear-gradient ⇒ backgroundColor 恒 transparent，必须看 backgroundImage
        critBg: crit ? (getComputedStyle(crit).backgroundImage + '|' + getComputedStyle(crit).borderTopColor) : '',
        softCount: soft ? (soft.innerText.indexOf('×3')>=0) : false,
        sumTxt: sum ? sum.innerText.replace(/\\s+/g,' ') : '',
        cosmeticCards: cards.filter(c=>c.dataset.pid && c.dataset.pid.indexOf('cosmetic')>=0).length
      };})())`, 7);
    const c = JSON.parse(card || '{}');
    check('A2', 'critical + degraded 各建一张卡，cosmetic 不建卡（共 2 张）',
      c.n === 2 && c.cosmeticCards === 0, `n=${c.n} cosmeticCards=${c.cosmeticCards}`);
    check('A3', 'critical 卡带 deg-critical 类且默认展开（不许自动收起）',
      c.hasCrit === true && c.critCollapsed === false, `hasCrit=${c.hasCrit} collapsed=${c.critCollapsed}`);
    check('A4', 'critical 卡底色/边框是红色系（真的走到了 CSS，不是只有类名）',
      /255, 246, 245|243, 167, 156|rgb\(24[0-9], 1[0-9][0-9], 1[0-9][0-9]\)/.test(c.critBg || '')
      && (c.critBg || '').indexOf('255, 0, 0') < 0,
      c.critBg);
    check('A5', 'degraded 卡显示合并次数 ×3', c.softCount === true, c.sumTxt.slice(0, 120));
    check('A6', '收口条汇总出「1 处结果不可信」与「1 处降级」',
      /1 处结果不可信/.test(c.sumTxt || '') && /1 处降级/.test(c.sumTxt || ''),
      (c.sumTxt || '').slice(0, 160));

    await evalJs(ws, `(()=>{const b=document.getElementById('proc-box');
      if(b&&b.scrollIntoView){try{b.scrollIntoView({block:'start'});}catch(e){}}
      return 'ok';})()`, 8);
    const shot1 = await cdp(ws, 'Page.captureScreenshot', { format: 'png' }, 81);
    const p1 = path.join(process.cwd(), 'screenshots', 'degrade_cards_20261125.png');
    try {
      fs.mkdirSync(path.dirname(p1), { recursive: true });
      fs.writeFileSync(p1, Buffer.from(shot1.result.data, 'base64'));
      console.log('    截图：' + p1);
    } catch (_) {}

    // ── B. 运行监控看板 ────────────────────────────────────────────────
    // 必须先切到 ops 页：dg-panel 在 pg-ops 里，首页快照看不出面板
    await evalJs(ws, `(()=>{try{if(typeof go==='function') go('ops');}catch(e){} return 'ok';})()`, 90);
    await sleep(800);
    const pan = await evalJs(ws, `(async()=>{try{await loadDegradePanel();}catch(e){return 'err:'+e.message}
      const el=document.getElementById('dg-panel');
      if(!el) return 'no-panel';
      const t=el.innerText.replace(/\\s+/g,' ');
      return JSON.stringify({hasTitle:t.indexOf('静默兜底台账')>=0,
        hasLevels:t.indexOf('结果不可信')>=0 && t.indexOf('功能降级')>=0 && t.indexOf('轻微')>=0,
        unavailable:t.indexOf('台账不可用')>=0, text:t.slice(0,300)});})()`, 9, true);
    let pb = {};
    try { pb = JSON.parse(pan || '{}'); } catch (_) { pb = { raw: pan }; }
    check('B1', '监控页存在 dg-panel 容器', pan !== 'no-panel', String(pan).slice(0, 80));
    check('B2', '面板渲染出标题与三档级别', pb.hasTitle === true && pb.hasLevels === true,
      (pb.text || '').slice(0, 160));
    check('B3', '接口不可用时不显示成"0 条降级"（必须显式说不可用）',
      pb.unavailable === false || pb.unavailable === undefined,
      `unavailable=${pb.unavailable}`);

    // 截图前：移除刚才注入的 proc-box（它浮在页面顶部会遮住看板），
    // 并把 dg-panel 滚到可见处 —— 图要能当证据，不能"断言过了但图上看不见"。
    await evalJs(ws, `(()=>{const b=document.getElementById('proc-box'); if(b) b.remove();
      const el=document.getElementById('dg-panel');
      if(el&&el.scrollIntoView){try{el.scrollIntoView({block:'start'});}catch(e){}}
      return 'ok';})()`, 11);
    await sleep(400);
    const shot2 = await cdp(ws, 'Page.captureScreenshot', { format: 'png' }, 10);
    const p2 = path.join(process.cwd(), 'screenshots', 'degrade_panel_20261125.png');
    try { fs.writeFileSync(p2, Buffer.from(shot2.result.data, 'base64')); console.log('    截图：' + p2); } catch (_) {}
  } catch (e) {
    check('BX', '浏览器验证执行', false, e.message);
  } finally {
    if (ws) { try { ws.close(); } catch (_) {} }
    try { proc.kill('SIGTERM'); } catch (_) {}
    console.log(`\n结果：${pass} pass / ${fail} fail`);
    process.exit(fail ? 1 : 0);
  }
})();
