/* HIL 三载体门禁 · **实时路径**断言（SSE `hil_confirm` → handleAskEvent）
 *
 * 为什么不用真实写操作触发：真实写要求智源 MCP 平台可写（当前返回 PERMISSION_DENIED），
 * 而本门禁要验的是**前端消费链路**，与写入是否成功无关 ⇒ 合成一个 hil_confirm 事件
 * 直接喂统一入口，断言三处载体与登记/清理，全程不产生任何 DB 写入。
 *
 * 清理：只摘掉这张合成卡；**不调用 decide**（DB 里没有 9999，调了会 404）。
 */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const FAKE = 9999;
  if (typeof selectConv === 'function') selectConv(990727);
  await sleep(6000);

  if (typeof handleAskEvent !== 'function') return JSON.stringify({ error: 'handleAskEvent 未定义' });
  handleAskEvent('confirm', {
    confirmation_id: FAKE, tool: 'save_artifact', tool_type: 'mcp',
    level: 'confirm', match_key: 'write', kind: '',
    preview: '\u6267\u884c\u300c\u5b9e\u65f6\u8def\u5f84\u6d4b\u8bd5.sysml\u300d',
    arguments: { title: '\u5b9e\u65f6\u8def\u5f84\u6d4b\u8bd5.sysml' },
    batch_count: 2, batch_merged: true, timeout_at: '2026-11-13 13:00'
  });
  await sleep(1200);

  // 三处载体前缀各不相同，查错前缀会得到假阴性
  const msgOk = document.getElementById('hilmsg-ok-' + FAKE);
  const dockOk = document.getElementById('hildock-ok-' + FAKE);
  const msgCard = msgOk ? msgOk.closest('.hil-card') : null;
  const dock = document.getElementById('confirm-ask');
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && r.height > 0;
  };

  const out = {
    liveMsgCard: !!msgCard,
    liveMsgBtnVisible: vis(msgOk),
    liveMsgInStreamNotDock: !!(msgCard && !msgCard.closest('#chat-input-dock')),
    liveMsgIsOwnMessageBlock: !!(msgCard && msgCard.closest('.msg')),
    liveDockCard: !!dockOk,
    liveDockBtnVisible: vis(dockOk),
    liveDockInsideConfirmDock: !!(dockOk && dockOk.closest('#confirm-ask')),
    livePendingRegistered: ((window._pendingAsk && window._pendingAsk.confirm) || [])
      .some(x => parseInt(x.id, 10) === FAKE),
    dockIsClarifyStyle: !!(dock && dock.classList.contains('clarify-ask-card') && dock.classList.contains('dock')),
    batchTextShown: !!(msgCard && msgCard.textContent.indexOf('\u672c\u6279\u5171 2 \u9879') >= 0),
    previewShown: !!(msgCard && msgCard.textContent.indexOf('\u5b9e\u65f6\u8def\u5f84\u6d4b\u8bd5') >= 0)
  };

  try {
    document.querySelectorAll('.hil-card[data-cid="' + FAKE + '"]').forEach(el => {
      const m = el.closest('.msg'); (m || el).remove();
    });
    const tlEl = document.querySelector('[data-pid="hil_' + FAKE + '"]');
    if (tlEl && tlEl.parentNode) tlEl.parentNode.removeChild(tlEl);
    window._pendingAsk.confirm = (window._pendingAsk.confirm || [])
      .filter(x => parseInt(x.id, 10) !== FAKE);
    if (typeof updateConfirmHint === 'function') updateConfirmHint();
  } catch (e) { out.cleanupError = String(e && e.message || e).slice(0, 160); }

  out.cleanupPendingLeft = ((window._pendingAsk && window._pendingAsk.confirm) || [])
    .some(x => parseInt(x.id, 10) === FAKE);
  out.cleanupDomLeft = document.querySelectorAll('.hil-card[data-cid="' + FAKE + '"]').length;
  return JSON.stringify(out);
})()
