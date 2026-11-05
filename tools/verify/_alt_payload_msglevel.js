/* 多方案对比卡 · 浏览器断言 payload（配合 tools/verify/verify_alternatives_browser.js）
 *
 * 判据要点（对应「元素在 DOM 里」≠「用户看得见」）：
 *   · 对比表在**消息级**（最近的 .rc 祖先存在，且**不在** .hist-proc 执行过程区内）
 *   · **用户看得见**：rect 有尺寸 + 在视口内（scrollIntoView 后再量一次）
 *   · 内容完整：维度行 / 方案列 / 原始量可见 / 综合得分行 / 推荐徽章
 *
 * ⚠️ 会话 id **不写死**：先向接口要「最近一次含 alternatives_score 调用的会话」，
 *    取不到就回退 ⇒ 门禁可重复运行，不依赖某次测试的残留会话。
 */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = {};

  // ── 1) 找一个含 alternatives_score 调用的会话 ──
  let convId = null;
  try {
    const cs = await (await fetch('/api/conversations?limit=50', {cache:'no-store'})).json();
    const list = (cs && (cs.conversations || cs.items || cs)) || [];
    for (const c of list) {
      const cid = c.id || c.conversation_id;
      if (!cid) continue;
      try {
        const msgs = await (await fetch('/api/conversations/' + cid + '/messages', {cache:'no-store'})).json();
        const arr = (msgs && (msgs.messages || msgs.items || msgs)) || [];
        const hit = arr.some(m => {
          try { return JSON.stringify(m.card_data || {}).indexOf('alternatives_score') >= 0; }
          catch (_) { return false; }
        });
        if (hit) { convId = cid; break; }
      } catch (_) {}
    }
  } catch (_) {}
  out.convId = convId;

  if (convId && typeof selectConv === 'function') {
    selectConv(convId);
    await sleep(8000);
  }
  out.msgCount = document.querySelectorAll('.msg').length;

  // ── 2) 消息级对比卡 ──
  const tables = Array.from(document.querySelectorAll('table.alt-table'));
  out.tableCount = tables.length;
  const msgLevel = tables.filter(t => !!t.closest('.rc') && !t.closest('.hist-proc'));
  out.msgLevelTables = msgLevel.length;
  out.insideProc = tables.length - msgLevel.length;

  if (msgLevel.length) {
    const t = msgLevel[msgLevel.length - 1];
    const cs = getComputedStyle(t);
    const r = t.getBoundingClientRect();
    out.hasTable = true;
    out.dimRows = t.querySelectorAll('tr th.alt-dim').length;
    out.colNames = Array.from(t.querySelectorAll('.alt-colname')).map(e => e.textContent.trim());
    out.hasTotalRow = t.innerText.indexOf('综合得分') >= 0;
    out.hasRecBadge = t.parentElement.innerText.indexOf('推荐') >= 0;
    out.display = cs.display;
    out.rect = { top: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width) };
    out.innerH = window.innerHeight;
    out.hasSize = r.height > 0 && r.width > 0;
    out.inViewport = r.bottom > 0 && r.top < window.innerHeight;
    out.barCount = t.querySelectorAll('.alt-bar').length;
    out.naCount = t.querySelectorAll('.alt-na').length;
    t.scrollIntoView({block:'center'});
    await sleep(600);
    const r2 = t.getBoundingClientRect();
    out.afterScroll = { top: Math.round(r2.top), h: Math.round(r2.height) };
    out.visibleAfterScroll = r2.height > 0 && r2.top < window.innerHeight && r2.bottom > 0;
  } else {
    out.hasTable = false;
  }
  return JSON.stringify(out);
})()
