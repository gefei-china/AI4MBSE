/**
 * O83（2026-10-15）：本体顶栏按钮对齐 —— 真实渲染几何验证（playwright headless）。
 *
 * 背景：#ont-subtab-row 混排 Tab span / 直挂按钮 / .dd 包裹按钮，flex 默认 stretch
 * 使直挂按钮拉满行高、.dd 内按钮保持原高 ⇒ 导入/导出与一致性检查等视觉错位。
 * 修复：app.css #ont-subtab-row{align-items:flex-end}。
 *
 * 方法：夹具 = index.html 原样抠出的顶栏行 + 真实 app.css（file:// 加载，无手写样式）。
 * 断言（夹具完整性守卫见 G 组）：
 *   A1 导入/导出/一致性检查/CQ/SWRL/⚙ 六按钮底边 y 相等（±1px）
 *   A2 六按钮高度相等（±1px）
 *   A3 Tab span「类」底边贴行底（下划线位置不因修复而变）
 *   M1 变异自证：把新规则禁用后 A1 必须失败（证明断言真的在测这条规则）
 */
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const file = 'file:///' + path.resolve(__dirname, '_tmp_ontbar_harness.html').replace(/\\/g, '/');
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(file);

  const sel = {
    imp:  '#ont-subtab-row .dd:nth-of-type(1)>button',
    exp:  '#ont-subtab-row .dd:nth-of-type(2)>button',
    chk:  'button[onclick="oeConsistency()"]',
    cq:   'button[onclick="openOntCQs()"]',
    swrl: 'button[onclick="openOntSWRL()"]',
    gear: 'button[onclick="openOntSettings()"]',
    tab:  '#ont-subtab-row span[data-tabgrp="ont"].on',
    row:  '#ont-subtab-row',
  };
  for (const [k, s] of Object.entries(sel)) {
    if (!(await page.$(s))) { console.log(`FAIL G-夹具缺元素 ${k} (${s})`); process.exit(1); }
  }
  const geo = async s => page.$eval(s, el => { const r = el.getBoundingClientRect();
    return { top: +r.top.toFixed(1), bottom: +r.bottom.toFixed(1), h: +r.height.toFixed(1) }; });

  const measure = async () => {
    const o = {};
    for (const [k, s] of Object.entries(sel)) o[k] = await geo(s);
    return o;
  };

  const diff = (vals, key) => Math.max(...vals) - Math.min(...vals);

  // A 组：修复后
  const g = await measure();
  const btns = ['imp', 'exp', 'chk', 'cq', 'swrl', 'gear'];
  const bottoms = btns.map(k => g[k].bottom), heights = btns.map(k => g[k].h);
  const dBot = diff(bottoms), dH = diff(heights);
  const rowBottom = g.row.bottom, tabBottom = g.tab.bottom;
  let ok = true;
  const A1 = dBot <= 1.0, A2 = dH <= 1.0;
  const A3 = Math.abs(tabBottom - rowBottom) <= 1.0;
  console.log(`A1 六按钮底边差 = ${dBot.toFixed(1)}px  ${A1 ? 'PASS' : 'FAIL'}`);
  console.log(`A2 六按钮高度差 = ${dH.toFixed(1)}px  ${A2 ? 'PASS' : 'FAIL'}`);
  console.log(`A3 Tab 下划线仍贴行底 (${tabBottom} vs ${rowBottom})  ${A3 ? 'PASS' : 'FAIL'}`);
  ok = A1 && A2 && A3;

  // M 组：变异自证 —— 禁用新规则必须复现错位
  await page.addStyleTag({ content: '#ont-subtab-row{align-items:stretch !important;}' });
  const g2 = await measure();
  const dBot2 = diff(btns.map(k => g2[k].bottom));
  const M1 = dBot2 > 1.0;
  console.log(`M1 变异（还原 stretch）底边差 = ${dBot2.toFixed(1)}px（应 >1）  ${M1 ? 'PASS' : 'FAIL'}`);
  ok = ok && M1;

  await browser.close();
  console.log(ok ? 'ALL PASS' : 'FAILED');
  process.exit(ok ? 0 : 1);
})();
