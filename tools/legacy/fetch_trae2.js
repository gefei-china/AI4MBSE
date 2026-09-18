/* 完整渲染 Trae artifact 并提取全部文本 */
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const page = await browser.newPage({ viewport: { width: 1500, height: 3000 } });
  const fsReqs = [];
  page.on('response', r => {
    const u = r.url();
    if (u.includes('__virtual_fs__') || u.includes('charts')) fsReqs.push(u.slice(0, 180));
  });
  await page.goto('https://share.traecontent.cn/artifact/AR8MBY_GQ2VFW2', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(()=>{});
  // 等渲染
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(5000);
    const t = await page.evaluate(() => (document.body.innerText || '').length);
    console.log(`第${i+1}次轮询: body 文本长度 =`, t);
    if (t > 1000) break;
  }
  const text = await page.evaluate(() => document.body.innerText || '');
  const iframes = await page.evaluate(() => Array.from(document.querySelectorAll('iframe')).map(f => f.src));
  console.log('iframes:', iframes);
  console.log('=== 全文 (' + text.length + ' 字) ===');
  console.log(text.slice(0, 30000));
  console.log('=== 虚拟文件请求 ===');
  fsReqs.forEach(r => console.log(r));
  await page.screenshot({ path: 'outputs/trae_artifact_full.png', fullPage: true }).catch(()=>{});
  await browser.close().catch(()=>{});
  process.exit(0);
})().catch(e => { console.error('ERR:', e.message); process.exit(2); });
