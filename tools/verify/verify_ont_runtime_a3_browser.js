// CI-OPTIONAL: [local] tier=local —— 真实浏览器门禁：需本机 Chrome/Edge + 8000 真服务/真库
/**
 * A3 四动作真实浏览器验收（2026-11-24）：
 *   a 对象 ID 查询、b CQ logs 字段名修复（真库 965 条不再显示 0）、
 *   c 术语批量映射候选面板、d 推理裁决计数。
 * 跑法：node tools/verify/verify_ont_runtime_a3_browser.js [--url http://127.0.0.1:8000]
 */
const { spawn } = require('child_process');
const fs = require('fs'); const os = require('os'); const path = require('path');
const args = process.argv.slice(2); const ix = args.indexOf('--url');
const urlArg = ix >= 0 ? args[ix + 1] : 'http://127.0.0.1:8000';
const CHROME = ['C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'];
let pass = 0, fail = 0;
function check(id, name, cond, detail) {
  if (cond) { pass++; console.log(`  [PASS] ${id} ${name}${detail ? '  — ' + detail : ''}`); }
  else { fail++; console.log(`  [FAIL] ${id} ${name}${detail ? '  — ' + detail : ''}`); }
}
function findChrome() { for (const p of CHROME) if (fs.existsSync(p)) return p; return null; }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function cdp(ws, method, params, id) {
  return new Promise((resolve, reject) => {
    const fn = ev => { let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.id === id) { ws.removeEventListener('message', fn); resolve(m); } };
    ws.addEventListener('message', fn);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
    setTimeout(() => { ws.removeEventListener('message', fn); reject(new Error('timeout ' + method)); }, 30000);
  });
}
(async () => {
  const chrome = findChrome();
  if (!chrome) { console.log('⚠️ 未找到浏览器，跳过'); process.exit(0); }
  try { const r = await fetch(urlArg + '/api/dashboard'); if (!r.ok) throw new Error('dashboard ' + r.status); }
  catch (e) { console.log('⚠️ 后端不可达，跳过：' + e.message); process.exit(0); }
  const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_a3br_')); const port = 9388;
  const proc = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    '--remote-debugging-port=' + port, '--user-data-dir=' + udd, '--window-size=1680,1400', 'about:blank'], { stdio: 'ignore' });
  let ws = null;
  try {
    let list = null;
    for (let k = 0; k < 40; k++) { await sleep(300);
      try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); list = await r.json(); if (list && list.length) break; } catch (_) {} }
    if (!list || !list.length) throw new Error('调试端口未就绪');
    ws = new WebSocket((list.find(t => t.type === 'page') || list[0]).webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); setTimeout(() => rej(new Error('ws timeout')), 10000); });
    await cdp(ws, 'Page.enable', {}, 1); await cdp(ws, 'Runtime.enable', {}, 2);
    await cdp(ws, 'Page.navigate', { url: urlArg + '/static/index.html' }, 3);
    await sleep(4500);
    await cdp(ws, 'Runtime.evaluate', { expression: "go('kb','kb-c');openOntRuntime()", returnByValue: true }, 4);
    await sleep(6000);

    const dump = await cdp(ws, 'Runtime.evaluate', {
      expression: `(()=>{const el=document.getElementById('ont-slide-body'); if(!el) return '{}';
        const t=el.innerText; return JSON.stringify({
          len:t.length,
          cqCount:(t.match(/能力问题（最近 (\\d+) 次执行）/)||[])[1],
          hasQuery:!!document.getElementById('ont-runtime-q'),
          hasMap:!!document.getElementById('ont-map-panel'),
          mapText:(document.getElementById('ont-map-panel')||{}).innerText||'',
          hasPending:t.indexOf('待审批次')>=0,
          text:t.slice(0,700)});})()`, returnByValue: true }, 5);
    const d = JSON.parse(dump.result.result.value);
    check('A', '看板已渲染', d.len > 100, `len=${d.len}`);
    check('B', 'CQ logs 字段名修复（真库 965 条）',
      Number(d.cqCount) >= 100, `显示最近 ${d.cqCount} 次（真库 965 条）`);
    check('C', 'A3-a 对象查询输入存在', d.hasQuery, String(d.hasQuery));
    check('D', 'A3-c 术语映射面板存在且有候选', d.hasMap && d.mapText.indexOf('候选') >= 0,
      d.mapText.slice(0, 100).replace(/\n/g, ' | '));
    check('E', 'A3-d 推理裁决计数存在', d.hasPending, String(d.hasPending));

    // 实点对象查询：填入真库实体名，断言出现"本体上下文"式结果
    await cdp(ws, 'Runtime.evaluate', {
      expression: `(()=>{const i=document.getElementById('ont-runtime-q'); if(!i) return 'no-input';
        i.value='宽带载荷'; ontQueryContext(); return 'clicked';})()`, returnByValue: true }, 6);
    await sleep(2500);
    const qr = await cdp(ws, 'Runtime.evaluate', {
      expression: `(()=>{const o=document.getElementById('ont-runtime-result'); return o?o.innerText:'';})()`, returnByValue: true }, 7);
    const qtext = qr.result.result.value || '';
    check('F', 'A3-a 对象查询真机返回关系', qtext.indexOf('关系') >= 0 && qtext.indexOf('宽带载荷') >= 0,
      qtext.slice(0, 160).replace(/\n/g, ' | '));
  } catch (e) { check('X', 'A3 浏览器验证执行', false, e.message); }
  finally { if (ws) { try { ws.close(); } catch (_) {} }
    try { proc.kill('SIGTERM'); } catch (_) {}
    console.log(`\n结果：${pass} pass / ${fail} fail`); process.exit(fail ? 1 : 0); }
})();
