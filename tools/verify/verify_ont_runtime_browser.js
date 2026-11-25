// CI-OPTIONAL: [local] tier=local —— 真实浏览器门禁：需本机 Chrome/Edge + 8000 真服务/真库，CI 无此组合
/**
 * 第一期「看得见」运行期语义看板 **真实浏览器** 验收（2026-11-24）。
 * 复用 verify_events_panel_browser.js 的 CDP 范式（spawnSync 被守护进程管 stdio 的坑已绕开：用 spawn + stdio ignore）。
 * 跑法：node tools/verify/verify_ont_runtime_browser.js [--url http://127.0.0.1:8000]
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

(async () => {
  const chrome = findChrome();
  if (!chrome) { console.log('⚠️ 未找到 Chrome/Edge，跳过'); process.exit(0); }
  try {
    const r = await fetch(urlArg + '/api/dashboard');
    if (!r.ok) throw new Error('dashboard ' + r.status);
  } catch (e) { console.log('⚠️ 后端不可达，跳过：' + e.message); process.exit(0); }

  const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_ontbr_'));
  const port = 9377;
  const proc = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    '--remote-debugging-port=' + port, '--user-data-dir=' + udd,
    '--window-size=1680,1400', 'about:blank',
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

    const boot = await cdp(ws, 'Runtime.evaluate', {
      expression: `JSON.stringify({go:typeof go==='function', open:typeof openOntRuntime==='function', btn:!!document.querySelector('[data-page-node-id="ontRuntimeBtn20261124"]')})`,
      returnByValue: true }, 4);
    const b = JSON.parse(boot.result.result.value);
    check('B1', '新按钮与新函数已加载', b.go && b.open && b.btn, JSON.stringify(b));

    const nav = await cdp(ws, 'Runtime.evaluate', {
      expression: `(()=>{try{go('kb','kb-c');return 'ok'}catch(e){return 'err:'+e.message}})()`, returnByValue: true }, 5);
    check('B2', "能切到本体模型页 go('kb','kb-c')", nav.result.result.value === 'ok', nav.result.result.value);

    const open = await cdp(ws, 'Runtime.evaluate', {
      expression: `(()=>{try{openOntRuntime();return 'ok'}catch(e){return 'err:'+e.message}})()`, returnByValue: true }, 6);
    check('B3', 'openOntRuntime() 可执行', open.result.result.value === 'ok', open.result.result.value);

    await sleep(5000);  // 等三路 fetch 完成

    const dump = await cdp(ws, 'Runtime.evaluate', {
      expression: `(()=>{const el=document.getElementById('ont-slide-body'); if(!el) return JSON.stringify({missing:true});
        const t=el.innerText; return JSON.stringify({
          len:t.length, loading:t.indexOf('正在加载')>=0,
          hasScanned:t.indexOf('实际扫描数据')>=0,
          has462:/实际扫描数据\\s*462/.test(t),
          hasCQ:t.indexOf('能力问题')>=0,
          hasSkip:t.indexOf('降级不是通过')>=0,
          text:t.slice(0,500)});})()`, returnByValue: true }, 7);
    const d = JSON.parse(dump.result.result.value);
    check('B4', '滑窗已渲染（非加载态）', !d.missing && !d.loading && d.len > 50, `len=${d.len}`);
    check('B5', '显示"实际扫描数据"指标', d.hasScanned, `has=${d.hasScanned}`);
    check('B6', '真机扫描数 462 已呈现', d.has462, d.text.slice(0, 160).replace(/\n/g, ' | '));
    check('B7', '显示 CQ 与 skip 纪律说明', d.hasCQ && d.hasSkip);

    await cdp(ws, 'Runtime.evaluate', { expression: `closeOntSlide && closeOntSlide()`, returnByValue: true }, 8);
  } catch (e) {
    check('BX', '浏览器验证执行', false, e.message);
  } finally {
    if (ws) { try { ws.close(); } catch (_) {} }
    try { proc.kill('SIGTERM'); } catch (_) {}
    console.log(`\n结果：${pass} pass / ${fail} fail`);
    process.exit(fail ? 1 : 0);
  }
})();
