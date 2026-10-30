/* HIL 确认卡验证 —— t0：造夹具容器（模仿真实 aiBox 结构，保证 CSS 命中） */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 清掉上一次的残留（同一浏览器进程内可能重跑）
  document.querySelectorAll('[data-hil-fixture]').forEach(e => e.remove());

  const wrap = document.createElement('div');
  wrap.className = 'msg ai';
  wrap.setAttribute('data-hil-fixture', '1');
  // ★ 必须带 id="stream-ai"：handleSSE 开头 `document.getElementById('stream-ai')`
  //   + `if(!aiBox) return;` —— 夹具漏了它，整个 handleSSE 直接早退（本文件已踩过一次）
  wrap.id = 'stream-ai';
  wrap.innerHTML = '<span class="who">AI</span>'
    + '<div class="msg-inner">'
    + '  <div class="proc" id="proc-box"></div>'
    + '  <div class="body streaming"></div>'
    + '</div>';
  const host = document.getElementById('chat-col') || document.body;
  host.appendChild(wrap);
  await sleep(300);

  const box = document.getElementById('proc-box');
  const r = box ? box.getBoundingClientRect() : null;
  const cs = box ? getComputedStyle(box) : null;
  return JSON.stringify({
    hostId: host.id || host.tagName,
    boxExists: !!box,
    boxDisplay: cs ? cs.display : '',
    boxH: r ? Math.round(r.height) : -1,
    boxTop: r ? Math.round(r.top) : -1,
    innerH: window.innerHeight,
    procSReachable: (typeof _procS !== 'undefined'),
    procSOnWindow: (typeof window._procS !== 'undefined'),
    fnReachable: (typeof procAddHilConfirm === 'function'),
    fnOnWindow: (typeof window.procAddHilConfirm === 'function'),
    handleSSEonWindow: (typeof window.handleSSE === 'function')
  });
})()
