/**
 * 事件面板 **真实浏览器** 验收（2026-11-01）。
 *
 * ── 为什么必须有这一层（不能只靠 mock）
 * 项目铁律 1：前端模块在全局作用域、`onclick` 直调函数 —— **改完必须浏览器验证**；
 * 只读源码或只跑 mock 会漏掉"函数没被调用 / 加载顺序不对 / DOM 挂错位置"这类问题。
 *
 * 本脚本用 **Chrome DevTools Protocol** 真开一个浏览器：
 *   ① 打开 index.html（本工程**无 hash 路由**，`#ops` 不会导航 —— 这本身是踩过的坑）；
 *   ② 在页面里执行 `go('ops')` 真正切到运行监控页；
 *   ③ 等事件面板渲染完；
 *   ④ 断言 `#ev-panel` 里出现了四区块与真实数据。
 *
 * 前置：后端已在 --url 所指地址运行（默认 http://127.0.0.1:8123）。
 * 跑法：node tools/verify/verify_events_panel_browser.js [--url http://127.0.0.1:8123]
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const args = process.argv.slice(2);
const urlArg = (() => {
  const i = args.indexOf('--url');
  return i >= 0 ? args[i + 1] : 'http://127.0.0.1:8123';
})();

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

function findChrome() {
  for (const p of CHROME_CANDIDATES) if (fs.existsSync(p)) return p;
  return null;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function cdp(ws, method, params, id) {
  return new Promise((resolve, reject) => {
    const _id = id;
    const onMsg = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.id === _id) { ws.removeEventListener('message', onMsg); resolve(m); }
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id: _id, method, params: params || {} }));
    setTimeout(() => { ws.removeEventListener('message', onMsg); reject(new Error('cdp timeout: ' + method)); }, 30000);
  });
}

(async () => {
  const chrome = findChrome();
  if (!chrome) { console.log('⚠️ 未找到 Chrome/Edge，跳过真实浏览器验证'); process.exit(0); }

  // 前置：后端可达？
  try {
    const r = await fetch(urlArg + '/api/events/stats');
    if (!r.ok) throw new Error('stats ' + r.status);
  } catch (e) {
    console.log('⚠️ 后端不可达（' + urlArg + '），跳过真实浏览器验证：' + e.message);
    process.exit(0);
  }

  const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_evbr_'));
  const port = 9333;
  const proc = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    '--remote-debugging-port=' + port,
    '--user-data-dir=' + udd,
    '--window-size=1680,1400',
    'about:blank',
  ], { stdio: 'ignore' });

  let ws = null;
  try {
    // 等调试端口起来
    let list = null;
    for (let i = 0; i < 40; i++) {
      await sleep(300);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/list`);
        list = await r.json();
        if (list && list.length) break;
      } catch (_) { /* 继续等 */ }
    }
    if (!list || !list.length) throw new Error('调试端口未就绪');
    const target = list.find(t => t.type === 'page') || list[0];

    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res);
      ws.addEventListener('error', rej);
      setTimeout(() => rej(new Error('ws open timeout')), 10000);
    });

    await cdp(ws, 'Page.enable', {}, 1);
    await cdp(ws, 'Runtime.enable', {}, 2);
    await cdp(ws, 'Page.navigate', { url: urlArg + '/static/index.html' }, 3);
    await sleep(4000);   // 等首屏脚本就绪

    // 关键：本工程无 hash 路由 ⇒ 必须显式 go('ops')
    const nav = await cdp(ws, 'Runtime.evaluate', {
      expression: "typeof go === 'function' ? (go('ops'), 'navigated') : 'go_missing'",
      returnByValue: true,
    }, 4);
    const navVal = nav.result && nav.result.result && nav.result.result.value;
    check('B1', "页面能执行 go('ops') 切到运行监控页", navVal === 'navigated', String(navVal));

    // 等事件面板异步渲染（它内部 5 路并行 fetch）
    await sleep(4000);

    const dump = await cdp(ws, 'Runtime.evaluate', {
      expression: `(() => {
        const el = document.getElementById('ev-panel');
        if(!el) return JSON.stringify({missing:true});
        return JSON.stringify({
          len: el.innerHTML.length,
          text: el.innerText.slice(0, 1200),
          loading: el.innerHTML.indexOf('加载事件面板') >= 0,
          tables: el.querySelectorAll('table').length,
          rows: el.querySelectorAll('tbody tr').length,
          buttons: el.querySelectorAll('button').length
        });
      })()`,
      returnByValue: true,
    }, 5);
    const d = JSON.parse((dump.result && dump.result.result && dump.result.result.value) || '{}');

    check('B2', '事件面板已渲染（不再是"加载事件面板…"）',
      !d.missing && !d.loading && d.len > 500, 'len=' + d.len + ' loading=' + d.loading);
    check('B3', '四区块文案齐全',
      typeof d.text === 'string' && d.text.includes('待审工单') && d.text.includes('事件台账')
      && d.text.includes('已执行') && d.text.includes('来源与开关'), '');
    check('B4', '四个表格都渲染出来了',
      d.tables >= 4, 'tables=' + d.tables);
    check('B5', '有数据行（后端已有事件/工单时）',
      d.rows >= 3, 'rows=' + d.rows);
    check('B6', '有可操作按钮（接力 / 放开执行 / 刷新）',
      d.buttons >= 3, 'buttons=' + d.buttons);

    console.log('\n  ── 面板真实渲染文本（前 500 字）──');
    console.log('  ' + String(d.text || '').replace(/\n/g, ' | ').slice(0, 500));

  } catch (e) {
    console.log('  [FAIL] 浏览器验证异常: ' + e.message);
    fail++;
  } finally {
    try { proc.kill(); } catch (_) {}
    try { ws && ws.close(); } catch (_) {}
    try { fs.rmSync(udd, { recursive: true, force: true }); } catch (_) {}
  }

  console.log('\n' + '='.repeat(60));
  console.log(`真实浏览器验证: ${pass}/${pass + fail} PASS`);
  console.log('='.repeat(60));
  if (fail) { console.log('\n❌ 有失败项'); process.exit(1); }
  console.log('\n✅ 事件面板在真实浏览器中渲染正常');
})();
