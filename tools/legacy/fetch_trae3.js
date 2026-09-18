/* 进入 iframe 抓取 artifact 内容 */
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const page = await browser.newPage({ viewport: { width: 1500, height: 3000 } });
  await page.goto('https://share.traecontent.cn/artifact/AR8MBY_GQ2VFW2', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(()=>{});
  await page.waitForTimeout(5000);
  const frame = page.frames().find(f => f.url().includes('__virtual_fs__')) || page.frames()[1];
  if (!frame) { console.log('NO FRAME'); process.exit(2); }
  console.log('FRAME URL:', frame.url().slice(0, 150));
  // 等 iframe 内渲染
  let len = 0;
  for (let i = 0; i < 8; i++) {
    await page.waitForTimeout(4000);
    len = await frame.evaluate(() => (document.body.innerText || '').length).catch(() => 0);
    console.log(`轮询${i+1}: iframe 文本长度 =`, len);
    if (len > 3000) break;
  }
  const text = await frame.evaluate(() => document.body.innerText || '');
  console.log('=== iframe 全文 (' + text.length + ' 字) ===');
  console.log(text.slice(0, 40000));
  await frame.screenshot({ path: 'outputs/trae_artifact_content.png', fullPage: true }).catch(()=>{});
  // 保存全文
  const fs = require('fs');
  fs.writeFileSync('outputs/trae_artifact_text.txt', text, 'utf-8');
  await browser.close().catch(()=>{});
  process.exit(0);
})().catch(e => { console.error('ERR:', e.message); process.exit(2); });
