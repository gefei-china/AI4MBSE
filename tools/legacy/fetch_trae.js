/* 用 Playwright 渲染 Trae artifact 分享页并提取内容 */
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 2400 } });
  const reqs = [];
  page.on('response', r => {
    const u = r.url();
    if (u.includes('api') || u.includes('artifact') || u.includes('share')) {
      if (!u.includes('static/') && !u.includes('sw.js')) reqs.push(r.status() + ' ' + u.slice(0, 160));
    }
  });
  try {
    await page.goto('https://share.traecontent.cn/artifact/AR8MBY_GQ2VFW2', { waitUntil: 'networkidle', timeout: 60000 });
  } catch(e) { console.log('goto warn:', e.message.slice(0, 100)); }
  await page.waitForTimeout(6000);
  // 提取正文
  const text = await page.evaluate(() => {
    const body = document.body.innerText || '';
    return body.slice(0, 20000);
  });
  console.log('=== 页面文本 (前20000字) ===');
  console.log(text);
  console.log('=== 关键请求 ===');
  reqs.forEach(r => console.log(r));
  await page.screenshot({ path: 'outputs/trae_artifact.png', fullPage: true }).catch(()=>{});
  await browser.close().catch(()=>{});
  process.exit(0);
})().catch(e => { console.error('ERR:', e.message); process.exit(2); });
