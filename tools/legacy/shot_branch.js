/* 分支管理优化：视觉截图验证 */
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  await page.goto('http://127.0.0.1:8000', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1200);
  await page.click('a[data-page="branch"]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: 'outputs/branch_manage.png' });

  // 打开创建分支表单截图
  await page.evaluate(() => { const b = Array.from(document.querySelectorAll('#pg-branch .ph button')).find(x => x.textContent.includes('创建')); if (b) b.click(); });
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'outputs/branch_create_form.png' });
  await page.evaluate(() => closeModal());

  // 打开合并请求表单截图
  await page.evaluate(() => { const b = Array.from(document.querySelectorAll('#pg-branch .ph button')).find(x => x.textContent.includes('新建合并请求')); if (b) b.click(); });
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'outputs/branch_merge_form.png' });

  await browser.close();
  console.log('screenshots done');
})();
