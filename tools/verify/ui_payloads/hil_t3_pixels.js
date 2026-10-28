/* HIL 确认卡验证 —— t3：**像素/布局级断言**（DOM 断言全绿 ≠ 用户看得见）
 *
 * ## 为什么还要这一层
 * 技能「元素在 DOM 里、class 也对 ≠ 用户看得见」那条：位置型/尺寸型 bug 会让
 * DOM 断言 100% 绿而用户啥也看不到。
 * ⇒ 必须量 `getBoundingClientRect()`，并**同时量参照物**（summary vs details 的相对位置）。
 *
 * ## 本组断言
 * ① 每张卡的 rect 都在视口内且有高度
 * ② 卡片之间**不重叠**（overlap 检测）
 * ③ 审批按钮**真的可见**（rect 高 > 0）—— 卡片能看不能点是致命缺陷
 * ④ "查看原始参数"折叠行可见，且参数区**默认收起**（details.open === false）
 * ⑤ 文字未被横向裁剪（scrollWidth <= clientWidth + 1）
 * ⑥ 卡片宽度与容器宽度关系合理（不是被压成一条缝）
 */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  await sleep(200);
  const out = {};
  const rect = el => { const r = el.getBoundingClientRect();
    return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left),
             right: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) }; };

  const cards = Array.from(document.querySelectorAll('[data-pid^="hil_"]'));
  out.cardCount = cards.length;
  out.innerH = window.innerHeight;
  out.innerW = window.innerWidth;

  out.cards = cards.map(el => {
    const r = rect(el);
    const cid = String(el.getAttribute('data-pid') || '').replace('hil_', '');
    const okBtn = document.getElementById('hil-ok-' + cid);
    const noBtn = document.getElementById('hil-no-' + cid);
    const det = el.querySelector('details');
    const sum = det ? det.querySelector('summary') : null;
    const body = el.querySelector('.proc-body');
    return {
      cid,
      rect: r,
      inViewport: r.h > 0 && r.bottom > 0 && r.top < window.innerHeight,
      hasHeight: r.h > 20,
      okBtnVisible: okBtn ? (okBtn.getBoundingClientRect().height > 0) : false,
      noBtnVisible: noBtn ? (noBtn.getBoundingClientRect().height > 0) : false,
      detailsVisible: det ? (det.getBoundingClientRect().height > 0) : false,
      argsCollapsed: det ? (det.open === false) : false,
      // 参照物相对位置：summary 必须在 details 顶部下方（否则是错位）
      sumAboveArgs: (sum && det) ? (sum.getBoundingClientRect().top <= det.getBoundingClientRect().top + 2) : false,
      hClipped: body ? (body.scrollWidth > body.clientWidth + 1) : false,
      vClipped: body ? (body.scrollHeight > body.clientHeight + 2 && body.clientHeight > 0) : false,
      bodyClientH: body ? Math.round(body.clientHeight) : -1
    };
  });

  // ② 两两不重叠（只比纵向区间，同列布局下足够）
  let overlap = 0;
  for (let i = 0; i < cards.length; i++) {
    for (let j = i + 1; j < cards.length; j++) {
      const a = rect(cards[i]), b = rect(cards[j]);
      if (a.top < b.bottom - 2 && b.top < a.bottom - 2) overlap++;
    }
  }
  out.pairwiseOverlap = overlap;

  // ⑥ 卡片宽度 vs 容器宽度（防止被压成缝）
  const box = document.getElementById('proc-box');
  const boxW = box ? Math.round(box.getBoundingClientRect().width) : -1;
  out.boxW = boxW;
  out.cardWidthSane = cards.length ? cards.every(el => {
    const w = el.getBoundingClientRect().width;
    return boxW <= 0 ? w > 100 : (w >= boxW * 0.5);
  }) : false;

  // 汇总布尔（便于一眼判定）
  out.all = {
    everyCardVisible: cards.length > 0 && out.cards.every(c => c.inViewport && c.hasHeight),
    everyCardHasButtons: out.cards.every(c => c.okBtnVisible && c.noBtnVisible),
    everyArgsCollapsed: out.cards.every(c => c.argsCollapsed),
    noOverlap: overlap === 0,
    noHorizontalClip: out.cards.every(c => !c.hClipped)
  };
  return JSON.stringify(out);
})()
