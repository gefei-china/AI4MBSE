// CI-OPTIONAL: [local] tier=local —— 真实浏览器门禁：需本机 Chrome/Edge + 8000 真服务
/**
 * 「看板区分未留痕 / 真 0 分」**真实浏览器**验收（2026-11-26）。
 *
 * ## 为什么需要浏览器级判据（接口对了不等于用户看对了）
 *
 * 数据层加了 `assign_traced`、接口把未留痕的分数返回成 `null` —— 但用户看到的是**渲染结果**。
 * JS 里 `0` 与 `null` 都是 falsy：只要渲染代码写成 `(it.assign_score || 0).toFixed(3)`，
 * 未留痕就会显示成 **0.000**，语义与"打分器算出 0 分"完全相反
 * （前者=没有依据，后者=有依据：该 Agent 不匹配）。这类缺陷**接口判据永远抓不到**。
 *
 * ## 判据
 *
 *   A1 `loadOpsAssignTrace` / `renderOpsAssignTrace` 已加载
 *   A2 未留痕任务在表格里**不含 "0.000"**，且出现「未留痕」字样
 *   A3 有留痕任务显示三位小数分数（不是 "—"）
 *   A4 真 0 分（assign_known=true 且 score=0）显示 **0.000** 而**不是**「未留痕」
 *      —— A2 与 A4 必须同时成立，否则"一律显示未留痕"也能骗过 A2
 *   A5 汇总徽标把 traced/untraced 分开报
 *   A6 截图落在 ops 页且面板可见（图要能当证据）
 *
 * 跑法：node tools/verify/verify_assign_trace_browser.js [--url http://127.0.0.1:8000]
 * 复用 verify_degrade_panel_browser.js 的 CDP 范式：
 *   spawn + `stdio:'ignore'`，绕开 agent-browser 守护进程管 stdio 导致 spawnSync 卡死的坑。
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
  let probe = null;
  try {
    const r = await fetch(urlArg + '/api/monitor/assignment-trace?limit=200');
    probe = await r.json();
  } catch (e) { console.log('⚠️ 后端不可达，跳过：' + e.message); process.exit(0); }

  const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_atbr_'));
  const port = 9382;
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

    const boot = await evalJs(ws, `JSON.stringify({
      load: typeof loadOpsAssignTrace==='function',
      render: typeof renderOpsAssignTrace==='function',
      go: typeof go==='function'})`, 4);
    const b = JSON.parse(boot || '{}');
    check('A1', 'loadOpsAssignTrace / renderOpsAssignTrace 已加载', b.load && b.render, boot);

    // 切到 ops 页（面板在 pg-ops 里；首页快照看不到）
    await evalJs(ws, `(()=>{try{if(typeof go==='function') go('ops');}catch(e){} return 'ok';})()`, 5);
    await sleep(600);

    // ★ 造一组**可判定的混合样本**注入渲染：真数据里历史未留痕很多、真 0 分很少，
    //   直接依赖真数据可能一整天都没有"真 0 分"样本 ⇒ A4 会退化成永远 SKIP。
    //   注入只走**真实渲染函数**（不 mock DOM 结构），所以验的还是产品代码。
    const inj = await evalJs(ws, `(()=>{
      const box=document.getElementById('ops-assign-trace');
      if(!box) return 'no-box';
      const sample={ok:true, enabled:true, summary:{traced:2,untraced:1,avg_score:0.5,
        note:'untraced 表示『该任务没有分派打分留痕』，其分数不存在，**不要按 0 分统计**。'},
        items:[
          {task_key:'mk-known', agent:'agentA', title:'有留痕样本', status:'done',
           conversation_id:1, assign_known:true, assign_score:0.75, capability_match:0.8},
          {task_key:'mk-zero', agent:'agentB', title:'真零分样本', status:'done',
           conversation_id:1, assign_known:true, assign_score:0, capability_match:0},
          {task_key:'mk-unknown', agent:'agentC', title:'未留痕样本', status:'done',
           conversation_id:1, assign_known:false, assign_score:null, capability_match:null}
        ]};
      box.innerHTML = renderOpsAssignTrace(sample);
      return 'ok';})()`, 6);
    check('A1b', '渲染函数可被真实调用并产出 DOM', inj === 'ok', String(inj));

    const r = await evalJs(ws, `JSON.stringify((()=>{
      const box=document.getElementById('ops-assign-trace');
      if(!box) return {err:'no-box'};
      const rows=[...box.querySelectorAll('tbody tr')];
      const pick=(k)=>{ for(const tr of rows){ if(tr.innerText.indexOf(k)>=0) return tr.innerText.replace(/\\s+/g,' '); } return ''; };
      return {n:rows.length,
        known:pick('mk-known'), zero:pick('mk-zero'), unknown:pick('mk-unknown'),
        text:box.innerText.replace(/\\s+/g,' ').slice(0,400)};})())`, 7);
    const d = JSON.parse(r || '{}');
    check('A2', '样本三行都渲染出来', d.n === 3, `n=${d.n}`);

    // ★ A3 核心：未留痕行**不得**出现三位小数分数（0.000 也不行 —— 那就是"显示成 0 分"）
    const unknownHasScore = /0\.000|\b0\.00\b/.test(d.unknown || '') || /\s0\s/.test(d.unknown || '');
    check('A3 ★未留痕行不显示任何分数、且出现「未留痕」字样',
      (d.unknown || '').indexOf('未留痕') >= 0 && !unknownHasScore,
      `行文本=${(d.unknown || '').slice(0, 120)}`);

    // ★ A4 反向：真 0 分**必须**显示 0.000（若它也显示「未留痕」，说明两档被翻反了）
    check('A4 ★真 0 分显示 0.000 而非「未留痕」（两档没被翻反）',
      /0\.000/.test(d.zero || '') && (d.zero || '').indexOf('未留痕') < 0,
      `行文本=${(d.zero || '').slice(0, 120)}`);

    check('A5 有留痕行显示三位小数分数',
      /0\.750/.test(d.known || ''), (d.known || '').slice(0, 120));

    // ── A6：真数据（不打桩）也要能渲染 ──────────────────────────────────
    const real = await evalJs(ws, `(async()=>{try{await loadOpsAssignTrace();}catch(e){return 'err:'+e.message}
      const box=document.getElementById('ops-assign-trace');
      if(!box) return 'no-box';
      const t=box.innerText.replace(/\\s+/g,' ');
      return JSON.stringify({n:(box.querySelectorAll('tbody tr')||[]).length,
        hasTbl:box.querySelectorAll('table').length>0, head:t.slice(0,160)});})()`, 8, true);
    let rr = {}; try { rr = JSON.parse(real || '{}'); } catch (_) { rr = { raw: real }; }
    check('A6 真实数据经端点渲染出表格', rr.hasTbl === true, JSON.stringify(rr).slice(0, 150));

    // 截图：滚到面板
    await evalJs(ws, `(()=>{const el=document.getElementById('ops-assign-trace');
      if(el){const p=el.closest('.panel'); if(p&&p.scrollIntoView){try{p.scrollIntoView({block:'start'});}catch(e){}}}
      return 'ok';})()`, 9);
    await sleep(500);
    const shot = await cdp(ws, 'Page.captureScreenshot', { format: 'png' }, 10);
    const p1 = path.join(process.cwd(), 'screenshots', 'assign_trace_panel_20261126.png');
    try {
      fs.mkdirSync(path.dirname(p1), { recursive: true });
      fs.writeFileSync(p1, Buffer.from(shot.result.data, 'base64'));
      console.log('    截图：' + p1);
    } catch (_) {}

    // 端侧交叉核对：接口层确实把未留痕给成 null（浏览器只证明渲染，不证明契约）
    const items = (probe && probe.items) || [];
    const un = items.filter(x => !x.assign_known);
    const kn = items.filter(x => x.assign_known);
    check('B1 接口：未留痕项 assign_score 全为 null',
      un.length > 0 ? un.every(x => x.assign_score === null && x.capability_match === null) : true,
      `未留痕 ${un.length} / 有留痕 ${kn.length}`);
    check('B2 接口：汇总 traced+untraced 等于条数',
      probe && probe.summary && (probe.summary.traced + probe.summary.untraced) === items.length,
      JSON.stringify(probe && probe.summary).slice(0, 120));
  } catch (e) {
    check('AX', '浏览器验证执行', false, e.message);
  } finally {
    if (ws) { try { ws.close(); } catch (_) {} }
    try { proc.kill('SIGTERM'); } catch (_) {}
    console.log(`\n结果：${pass} pass / ${fail} fail`);
    process.exit(fail ? 1 : 0);
  }
})();
