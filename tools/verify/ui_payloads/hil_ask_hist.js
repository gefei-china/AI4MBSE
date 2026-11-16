/* HIL 三载体门禁 · 历史还原路径断言（配合 tools/verify/verify_hil_ask_card.js）
 *
 * ⚠️ 判别必须靠**按钮 id 前缀**，不能用 `.hil-card[data-cid]`：
 *    三处载体共用同一 class 与 data-cid，用 class 判断「消息流卡存在」会**恒真**
 *    （dock 卡也命中）⇒ 变异掉渲染分支后断言仍绿 = 空转断言。第一版就栽在这。
 *    前缀：hilmsg-（消息流）／hildock-（输入框 dock）／hil-（时间线）
 */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  if (typeof selectConv === 'function') selectConv(990727);
  await sleep(9000);   // 等历史消息 + loadPendingConfirms 完成

  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && r.height > 0;
  };
  const rect = el => {
    const r = el && el.getBoundingClientRect();
    return r ? { top: Math.round(r.top), h: Math.round(r.height) } : null;
  };

  const msgOk = document.getElementById('hilmsg-ok-101');
  const dockOk = document.getElementById('hildock-ok-101');
  const msgCard = msgOk ? msgOk.closest('.hil-card') : null;
  const dockCard = dockOk ? dockOk.closest('.hil-card') : null;
  const pend = (window._pendingAsk && window._pendingAsk.confirm) || [];

  return JSON.stringify({
    msgCardExists: !!msgCard,
    msgCardVisible: vis(msgOk),
    msgCardInViewport: (() => { if (!msgOk) return false; const r = msgOk.getBoundingClientRect();
                                return r.bottom > 0 && r.top < window.innerHeight; })(),
    msgCardNotInDock: !!(msgCard && !msgCard.closest('#chat-input-dock')),
    msgCardInMsgBlock: !!(msgCard && msgCard.closest('.msg')),
    msgCardTextHasApprove: msgCard ? msgCard.textContent.indexOf('\u6279\u51c6\u5e76\u6267\u884c') >= 0 : false,
    msgCardTextHasTool: msgCard ? msgCard.textContent.indexOf('save_artifact') >= 0 : false,
    msgCardRect: rect(msgCard),
    dockCardExists: !!dockCard,
    dockCardVisible: vis(dockOk),
    dockCardTextHasApprove: dockCard ? dockCard.textContent.indexOf('\u6279\u51c6\u5e76\u6267\u884c') >= 0 : false,
    // 历史行 preview 形如「执行「xxx.sysml」」；无 preview 时才走 payload.title 兜底「写入产物：…」
    dockCardTextHasPreview: dockCard
      ? (dockCard.textContent.indexOf('\u6267\u884c\u300c') >= 0
         || dockCard.textContent.indexOf('\u5199\u5165\u4ea7\u7269') >= 0)
      : false,
    dockCardInsideConfirmDock: !!(dockOk && dockOk.closest('#confirm-ask')),
    dockRect: rect(dockCard),
    pendingCount: pend.length,
    askLayerExists: typeof handleAskEvent === 'function',
    dockLoadExists: typeof loadPendingConfirms === 'function',
    allHilCards: document.querySelectorAll('.hil-card').length,
    innerH: window.innerHeight, innerW: window.innerWidth
  });
})()
