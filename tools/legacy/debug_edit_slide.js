/* 诊断：编辑本体类型弹窗是否正常 */
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

  // 点第一个实体类型的编辑按钮
  const before = await page.evaluate(() => ({
    slideOpen: document.getElementById('ont-slide').classList.contains('open'),
    fields: {
      name: document.getElementById('ot-name') && document.getElementById('ot-name').value,
      kind: document.getElementById('ot-kind') && document.getElementById('ot-kind').value,
      desc: document.getElementById('ot-desc') && document.getElementById('ot-desc').value,
      icon: document.getElementById('ot-icon') && document.getElementById('ot-icon').value,
      color: document.getElementById('ot-color') && document.getElementById('ot-color').value,
    }
  }));
  console.log('编辑前:', JSON.stringify(before));

  await page.evaluate(() => {
    // 找到部件的编辑按钮
    const items = document.querySelectorAll('#ont-types-list [data-ont-type]');
    for (const it of items) {
      if (it.textContent.includes('部件')) {
        const btn = it.querySelector('button:nth-of-type(1)'); // ✏️ 编辑按钮（第一个）
        if (btn) btn.click();
        break;
      }
    }
  });
  await page.waitForTimeout(600);

  const afterEdit = await page.evaluate(() => {
    const open = document.getElementById('ont-slide').classList.contains('open');
    const title = document.getElementById('ont-slide-title') && document.getElementById('ont-slide-title').textContent;
    const formTitle = document.getElementById('ont-form-title') && document.getElementById('ont-form-title').textContent;
    const inputs = {
      name: document.getElementById('ot-name') && document.getElementById('ot-name').value,
      kind: document.getElementById('ot-kind') && document.getElementById('ot-kind').value,
      desc: document.getElementById('ot-desc') && document.getElementById('ot-desc').value,
      descLong: document.getElementById('ot-desc-long') && document.getElementById('ot-desc-long').value,
      icon: document.getElementById('ot-icon') && document.getElementById('ot-icon').value,
      color: document.getElementById('ot-color') && document.getElementById('ot-color').value,
      iconGridExists: !!document.getElementById('ot-icon-grid'),
      colorRowExists: !!document.getElementById('ot-color-row'),
      propsRowsExists: !!document.getElementById('ot-props-rows'),
    };
    const iconGridKids = document.getElementById('ot-icon-grid') && document.getElementById('ot-icon-grid').children.length;
    const colorRowKids = document.getElementById('ot-color-row') && document.getElementById('ot-color-row').children.length;
    const propsRowsKids = document.getElementById('ot-props-rows') && document.getElementById('ot-props-rows').children.length;
    return { open, title, formTitle, inputs, iconGridKids, colorRowKids, propsRowsKids };
  });
  console.log('编辑实体后:', JSON.stringify(afterEdit, null, 2));

  // 关闭后再编辑一个关系类型
  await page.evaluate(() => closeOntSlide());
  await page.waitForTimeout(300);
  await page.evaluate(() => {
    const items = document.querySelectorAll('#ont-rels-list [data-ont-type]');
    for (const it of items) {
      if (it.textContent.includes('包含')) {
        const btn = it.querySelector('button:nth-of-type(1)');
        if (btn) btn.click();
        break;
      }
    }
  });
  await page.waitForTimeout(600);

  const afterRel = await page.evaluate(() => {
    const open = document.getElementById('ont-slide').classList.contains('open');
    const title = document.getElementById('ont-slide-title') && document.getElementById('ont-slide-title').textContent;
    const inputs = {
      name: document.getElementById('ot-name') && document.getElementById('ot-name').value,
      kind: document.getElementById('ot-kind') && document.getElementById('ot-kind').value,
      domain: document.getElementById('ot-domain') && document.getElementById('ot-domain').value,
      range: document.getElementById('ot-range') && document.getElementById('ot-range').value,
      cardinality: document.getElementById('ot-cardinality') && document.getElementById('ot-cardinality').value,
      iconRowHidden: document.getElementById('ot-icon-row') && document.getElementById('ot-icon-row').style.display === 'none',
      domRowVisible: document.getElementById('ot-domain-row') && document.getElementById('ot-domain-row').style.display === 'flex',
    };
    return { open, title, inputs };
  });
  console.log('编辑关系后:', JSON.stringify(afterRel, null, 2));

  console.log('JS错误数:', errors.length);
  errors.slice(0, 5).forEach(e => console.log('  ' + e));
  await browser.close();
})();