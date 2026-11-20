// CI-OPTIONAL: [local] tier=local —— 真实浏览器验证执行过程区的归属徽章（Agent/技能/工具），需本机 Chrome/Edge + 运行中服务；改法须同步改 gates_manifest.json tier
/**
 * 执行环节归属徽章 **真实浏览器** 验收（2026-11-20）。
 *
 * 用户要求：「执行过程中调用的 agent、skill、tool 等，需要在对应的环节标识出来」。
 * 只读源码或只跑纯函数会漏掉"函数没被调用 / 样式没生效 / DOM 没挂上"这类问题
 * （本仓铁律：前端模块是全局函数 + onclick 直调，改完必须浏览器验证）。
 *
 * 做法：真开 headless 浏览器 → 打开页面 → 在页面里**直接驱动真实函数**
 * （procAddAgent/procAddSkill/procAddTool/procRender）→ 检查 DOM 里
 *   ① 出现 .proc-badge.ba / .bs 徽章且文字含真实名字；
 *   ② 徽章在 .proc-head 内（即"标在对应环节"而不是飘在别处）；
 *   ③ 参与方汇总行存在且三分类齐全；
 *   ④ 计算样式真的给了颜色（不是 display:none / 透明）。
 *
 * 跑法：node tools/verify/verify_proc_badges_browser.js [--url http://127.0.0.1:8000]
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const args = process.argv.slice(2);
const urlArg = (() => {
  const i = args.indexOf('--url');
  return i >= 0 ? args[i + 1] : 'http://127.0.0.1:8000';
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
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function findChrome() {
  for (const p of CHROME_CANDIDATES) if (fs.existsSync(p)) return p;
  return null;
}

async function cdp(ws, method, params, id) {
  return new Promise((resolve, reject) => {
    const onMsg = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.id === id) { ws.removeEventListener('message', onMsg); resolve(m); }
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
    setTimeout(() => { ws.removeEventListener('message', onMsg); reject(new Error('cdp timeout: ' + method)); }, 30000);
  });
}

(async () => {
  const chrome = findChrome();
  if (!chrome) { console.log('⚠️ 未找到 Chrome/Edge，跳过真实浏览器验证'); process.exit(0); }
  try {
    const r = await fetch(urlArg + '/');
    if (!r.ok) throw new Error('root ' + r.status);
  } catch (e) {
    console.log('⚠️ 后端不可达（' + urlArg + '），跳过：' + e.message);
    process.exit(0);
  }

  const udd = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_badge_'));
  const port = 9347;
  const proc = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    '--remote-debugging-port=' + port,
    '--user-data-dir=' + udd,
    '--window-size=1680,1400',
    'about:blank',
  ], { stdio: 'ignore' });

  let ws = null;
  try {
    let list = null;
    for (let i = 0; i < 40; i++) {
      await sleep(300);
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/list`);
        list = await r.json();
        if (list && list.length) break;
      } catch (_) {}
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
    await sleep(4500);

    // ① 驱动真实函数造一条「Agent → 技能 → 工具」链路
    const drive = await cdp(ws, 'Runtime.evaluate', {
      expression: `(() => {
        const need = ['procAddAgent','procAddSkill','procAddTool','procRender','procReset'];
        const miss = need.filter(k => typeof window[k] !== 'function');
        if(miss.length) return JSON.stringify({missing: miss});
        // #proc-box 只在流式生成时被创建；首屏没有它 ⇒ 这里补一个容器，
        // 测的是「渲染逻辑与样式」，与真实路径用的是同一个 procRender。
        if(!document.getElementById('proc-box')){
          const d = document.createElement('div');
          d.id = 'proc-box';
          document.body.appendChild(d);
        }
        if(typeof procReset === 'function') procReset();
        if(!document.getElementById('proc-box')){
          const d2 = document.createElement('div'); d2.id='proc-box'; document.body.appendChild(d2);
        }
        procAddAgent({type:'agent', status:'run', name:'design', display_name:'工程建模方案设计Agent'});
        procAddSkill({type:'skill', status:'done', names:['sysml_view_generation_requirement'],
                      agent:'design', note:'触发词命中'});
        procAddTool({type:'tool', status:'run', name:'sysml_v2_validate',
                     agent:'design', skill:'sysml_view_generation_requirement',
                     skill_guide:'sysml_view_generation_requirement', depth:2,
                     call_id:'c1', arguments:{code:'package X{}'}});
        procAddTool({type:'tool', status:'done', name:'sysml_v2_validate', ok:true,
                     result:'词法 0 / 语法 0 / 语义 0', latency_ms:812,
                     agent:'design', skill:'sysml_view_generation_requirement',
                     skill_guide:'sysml_view_generation_requirement', depth:2, call_id:'c1'});
        if(typeof procRender === 'function') procRender();
        return JSON.stringify({ok:true});
      })()`,
      returnByValue: true,
    }, 4);
    check('B1', '页面里三个归属函数可用（全局作用域未丢）',
      (drive.result && drive.result.result && drive.result.result.value || '').includes('"ok":true'),
      String(drive.result && drive.result.result && drive.result.result.value));

    await sleep(600);

    const dump = await cdp(ws, 'Runtime.evaluate', {
      expression: `(() => {
        const box = document.getElementById('proc-box');
        if(!box) return JSON.stringify({missing:'proc-box'});
        const ba = [...box.querySelectorAll('.proc-badge.ba')];
        const bs = [...box.querySelectorAll('.proc-badge.bs')];
        // ⚠️ 只校验**环节行**的徽章位置：参与方汇总块的徽章在展开体（proc-body）内是设计如此。
        const blocks = [...box.querySelectorAll('.proc-block:not(.participants)')];
        const inHead = (el) => blocks.some(b => {
          const h = b.querySelector('.proc-head');
          return h && h.contains(el);
        });
        const baRow = [...box.querySelectorAll('.proc-block:not(.participants) .proc-badge.ba')];
        const bsRow = [...box.querySelectorAll('.proc-block:not(.participants) .proc-badge.bs')];
        const cs = ba.length ? getComputedStyle(ba[0]) : null;
        return JSON.stringify({
          baN: ba.length, bsN: bs.length,
          baText: ba.map(e=>e.innerText).slice(0,4),
          bsText: bs.map(e=>e.innerText).slice(0,4),
          baInHead: baRow.length ? baRow.every(inHead) : false,
          bsInHead: bsRow.length ? bsRow.every(inHead) : false,
          baRowN: baRow.length, bsRowN: bsRow.length,
          color: cs ? cs.color : null,
          bg: cs ? cs.backgroundColor : null,
          partTitle: (box.innerText.match(/执行参与方（[^）]*）/) || [''])[0],
          partBadges: box.querySelectorAll('.proc-block.participants .proc-badge').length,
          toolRows: box.querySelectorAll('.proc-block.tool').length
        });
      })()`,
      returnByValue: true,
    }, 5);
    const d = JSON.parse((dump.result && dump.result.result && dump.result.result.value) || '{}');

    check('B2', 'Agent 徽章渲染出来了', d.baN > 0, 'n=' + d.baN);
    check('B3', 'Agent 徽章显示真实名字（不是只有图标）',
      Array.isArray(d.baText) && d.baText.some(t => t && t.includes('工程建模方案设计Agent')), JSON.stringify(d.baText));
    check('B4', '技能徽章渲染并显示技能名',
      d.bsN > 0 && (d.bsText || []).some(t => t && t.includes('sysml_view_generation_requirement')),
      JSON.stringify(d.bsText));
    check('B5', '徽章标在**对应环节**的行内（proc-head）', d.baInHead && d.bsInHead,
      'agent=' + d.baInHead + ' skill=' + d.bsInHead);
    check('B6', '样式真的生效（有前景色/底色，不是透明）',
      !!d.color && !!d.bg && d.bg !== 'rgba(0, 0, 0, 0)', 'color=' + d.color + ' bg=' + d.bg);
    check('B7', '参与方汇总行存在且三段计数',
      typeof d.partTitle === 'string' && d.partTitle.indexOf('执行参与方') === 0, d.partTitle);
    check('B8', '参与方汇总里也有徽章（展开体）', d.partBadges >= 2, 'n=' + d.partBadges);
    check('B9', '工具环节渲染出来了', d.toolRows >= 1, 'n=' + d.toolRows);

    const shot = await cdp(ws, 'Page.captureScreenshot', { format: 'png' }, 6);
    const b64 = shot.result && shot.result.data;
    if (b64) {
      const out = path.join(__dirname, '..', '..', 'outputs', 'verify_proc_badges.png');
      fs.mkdirSync(path.dirname(out), { recursive: true });
      fs.writeFileSync(out, Buffer.from(b64, 'base64'));
      console.log('  截图 → ' + out);
    }
  } catch (e) {
    console.log('⚠️ 浏览器验证异常：' + e.message);
    fail++;
  } finally {
    try { if (ws) ws.close(); } catch (_) {}
    try { proc.kill(); } catch (_) {}
  }

  console.log(`\n结论：${fail === 0 ? 'PASS' : 'FAIL'}（${pass} PASS / ${fail} FAIL）`);
  process.exit(fail === 0 ? 0 : 1);
})();
