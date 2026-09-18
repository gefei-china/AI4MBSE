const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().includes('404')) errors.push('CONSOLE: ' + m.text()); });
  await page.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);
  await page.evaluate(() => { for (const t of document.querySelectorAll('.tab')) if (t.textContent.includes('知识库')) { t.click(); break; } });
  await page.waitForTimeout(400);
  await page.evaluate(() => { for (const s of document.querySelectorAll('[data-tabgrp="kb"]')) if (s.textContent.includes('本体管理')) { s.click(); break; } });
  await page.waitForTimeout(1200);

  // 看 attribute 类型编辑
  // 切到属性 tab
  await page.evaluate(() => setOntCategory('attribute'));
  await page.waitForTimeout(400);
  const attrs = await page.evaluate(() => {
    const items = document.querySelectorAll('#ont-types-list [data-ont-type]');
    return Array.from(items).map(it => it.dataset.ontType);
  });
  console.log('属性列表项:', JSON.stringify(attrs));

  // 点击 attribute 类型第一项（频段）的编辑按钮
  await page.evaluate(() => {
    const items = document.querySelectorAll('#ont-types-list [data-ont-type]');
    for (const it of items) {
      if (it.textContent.includes('频段')) {
        const btn = it.querySelector('button:nth-of-type(1)');
        if (btn) btn.click();
        break;
      }
    }
  });
  await page.waitForTimeout(600);
  const attr = await page.evaluate(() => ({
    title: document.getElementById('ont-slide-title').textContent,
    kind: document.getElementById('ot-kind').value,
    name: document.getElementById('ot-name').value,
    desc: document.getElementById('ot-desc').value,
    icon: document.getElementById('ot-icon').value,
    iconRowHidden: document.getElementById('ot-icon-row').style.display === 'none',
    propsHidden: document.getElementById('ot-props-section').style.display === 'none',
    consHidden: document.getElementById('ot-cons-section').style.display === 'none'
  }));
  console.log('编辑 attribute(频段):', JSON.stringify(attr, null, 2));

  // 截图本体管理页面
  await page.evaluate(() => closeOntSlide());
  await page.waitForTimeout(300);
  await page.screenshot({ path: 'C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/outputs/ont_manage.png' });

  // 打开 RDF tab 截图
  await page.evaluate(() => { for (const s of document.querySelectorAll('[data-tabgrp="ont"]')) if (s.textContent.includes('RDF')) { s.click(); break; } });
  await page.waitForTimeout(800);
  await page.screenshot({ path: 'C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/outputs/ont_rdf.png' });

  console.log('JS错误:', errors.length);
  errors.slice(0, 3).forEach(e => console.log(' ', e));
  await browser.close();
})();