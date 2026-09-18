// 验证：设置页「📄 文件抽取」Tab 迁移（默认不开启抽取）
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle', timeout: 60000 });

  // 1. 主导航进入「设置」
  await page.click('#mainnav a[data-page="settings"]');
  await page.waitForTimeout(600);

  // 2. 点击「📄 文件抽取」Tab
  const tabSel = '#pg-settings .subtab span[onclick*="st-fextract"]';
  await page.waitForSelector(tabSel, { timeout: 8000 });
  await page.click(tabSel);
  await page.waitForTimeout(800);

  // 3. 校验表单渲染与默认值（默认不开启）
  const en = await page.inputValue('#fe-enabled').catch(() => null);
  const src = await page.inputValue('#fe-source').catch(() => null);
  const subpageOn = await page.$eval('#pg-settings .subpage.on', el => el.id).catch(() => null);
  const hasBadge = await page.evaluate(() => document.querySelector('#st-fextract .badge')?.textContent || '');
  console.log('激活 subpage:', subpageOn);
  console.log('fe-enabled 默认值:', en, en === '0' ? '✅ 默认关闭' : '❌ 应为 0');
  console.log('fe-source 默认值:', src, src === 'sysml' ? '✅ 默认 sysml' : '❌ 应为 sysml');
  console.log('badge:', hasBadge);

  await page.screenshot({ path: 'C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/outputs/fextract_settings_tab.png', fullPage: false });

  // 4. 回归：其他设置 tab 仍可切换
  await page.click('#pg-settings .subtab span[onclick*="st-projmem"]');
  await page.waitForTimeout(400);
  const pmOn = await page.$eval('#pg-settings .subpage.on', el => el.id).catch(() => null);
  console.log('切回项目记忆:', pmOn === 'st-projmem' ? '✅' : '❌ ' + pmOn);

  const jsErrors = errors.filter(e => !/favicon|net::ERR/.test(e));
  console.log('JS 错误:', jsErrors.length ? jsErrors : '无');
  await browser.close();
  process.exit(0);
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
