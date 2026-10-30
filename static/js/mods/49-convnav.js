/* ==========================================================================
 * 49-convnav.js —— 会话轮次目录（2026-10-22，米爸需求3b）
 *
 * 需求原话：「可以把会话记录按照对话轮次生成一个目录，以便快速定位；
 *          默认收起，hover 展示用户输入的清单」。
 *
 * 形态（与米爸给的参考图一致）：
 *   · 收起态 = 会话区右缘一列**短刻度**，每轮一个刻度；当前轮刻度更长更深；
 *   · hover  = 向左展开浮层，按轮次列出**用户每次输入**（右对齐、两行截断，同用户气泡的语感）；
 *   · 点击刻度或清单行 → 跳到该轮首个用户消息，并给它一个短暂高亮；
 *   · 随滚动实时更新"当前轮"。
 *
 * ⚠️ 两条设计约束（踩过坑，别改）：
 *   ① **右缘必须避开 #chat-area 的滚动条**：滚动条就在聊天列最右侧，
 *      刻度若压上去会抢走拖拽（米爸本轮第2条反馈正是"滚动条拖不动"）⇒ 用 right:18px 内缩。
 *   ② **默认收起**是米爸明确要求的形态，不是"没做完"；别改成常驻展开。
 * ========================================================================== */

(function () {
  var MIN_TURNS = 2;        // 少于 2 轮不显示（单轮会话用不上目录，徒增干扰）
  var NAV_ID = 'conv-nav';
  var _turns = [];          // [{el, mid, text, idx}]
  var _active = -1;
  var _inited = false;
  var _obs = null;
  var _refreshTimer = 0;
  var _activeRaf = 0;

  function area() { return document.getElementById('chat-area'); }

  /* 采集轮次：每个**用户消息**（.msg.u）算一轮。
     用 :scope > 直接子元素 —— 消息卡片内部也可能有 .msg.u 之类的类名，
     不限定作用域会把卡片里的东西也算成一轮（本工程已有同类误命中的先例）。 */
  function collectTurns() {
    var a = area();
    if (!a || !a.querySelectorAll) return [];
    var list;
    try {
      list = a.querySelectorAll(':scope > .msg.u');
    } catch (e) {
      list = a.querySelectorAll('.msg.u');   // 老浏览器兜底
    }
    var out = [];
    Array.prototype.forEach.call(list, function (el, i) {
      var body = el.querySelector('.body');
      var t = (body ? body.textContent : el.textContent) || '';
      t = t.replace(/\s+/g, ' ').trim();
      out.push({
        el: el,
        mid: el.getAttribute('data-mid') || el.id || '',
        text: t,
        idx: i + 1
      });
    });
    return out;
  }

  function esc1(s) { return (typeof esc === 'function') ? esc(s) : String(s); }

  function render() {
    var a = area();
    var nav = document.getElementById(NAV_ID);
    if (!a || !nav) return;

    _turns = collectTurns();
    // 欢迎屏（新建任务初始页）没有轮次概念 → 隐藏
    var isWelcome = !!a.querySelector('.chat-welcome');
    var show = !isWelcome && _turns.length >= MIN_TURNS;
    nav.style.display = show ? 'flex' : 'none';
    if (!show) { _active = -1; return; }

    var ticks = document.getElementById('cn-ticks');
    var panel = document.getElementById('cn-panel');

    ticks.innerHTML = _turns.map(function (t, i) {
      var tip = t.text.slice(0, 60) || '（空消息）';
      return '<button type="button" class="cn-tick" data-ti="' + i + '" title="第 ' + t.idx + ' 轮：' + esc1(tip) + '"></button>';
    }).join('');

    panel.innerHTML = '<div class="cn-head">会话轮次（' + _turns.length + '）</div>'
      + _turns.map(function (t, i) {
          var txt = t.text.slice(0, 90) || '（空消息）';
          return '<div class="cn-row" data-ti="' + i + '">'
            + '<span class="cn-idx">' + t.idx + '</span>'
            + '<span class="cn-txt">' + esc1(txt) + '</span>'
            + '<i class="cn-mark"></i></div>';
        }).join('');

    _active = -1;
    syncActive();
  }

  /* 当前轮 = 视口顶部那一条（其 el.top 已越过探针线、且是最后一个越过的）。
     与主流对话客户端的大纲一致；没有"中段处于两轮之间"的含糊状态。 */
  function computeActive() {
    var a = area();
    if (!a || !_turns.length) return -1;
    var ar = a.getBoundingClientRect();
    var probe = ar.top + Math.min(64, Math.max(24, a.clientHeight * 0.18));
    var idx = 0;
    for (var i = 0; i < _turns.length; i++) {
      var r = _turns[i].el.getBoundingClientRect();
      if (r.height > 0 && r.top <= probe) idx = i;
    }
    return idx;
  }

  function syncActive() {
    var idx = computeActive();
    if (idx === _active) return;
    _active = idx;
    var nav = document.getElementById(NAV_ID);
    if (!nav) return;
    var tk = nav.querySelectorAll('#cn-ticks .cn-tick');
    Array.prototype.forEach.call(tk, function (el) {
      el.classList.toggle('on', Number(el.dataset.ti) === idx);
    });
    var rows = nav.querySelectorAll('#cn-panel .cn-row');
    Array.prototype.forEach.call(rows, function (el) {
      el.classList.toggle('on', Number(el.dataset.ti) === idx);
    });
    // 浮层展开时，把当前行滚进可视区（轮次很多时否则找不到"我在哪"）
    var cur = nav.querySelector('#cn-panel .cn-row.on');
    if (cur && nav.matches && nav.matches(':hover')) {
      try { cur.scrollIntoView({ block: 'nearest' }); } catch (e) {}
    }
  }

  function scheduleActive() {
    if (_activeRaf) return;
    _activeRaf = requestAnimationFrame(function () {
      _activeRaf = 0;
      syncActive();
    });
  }

  /* 跳到第 i 轮。
     ⚠️ 2026-10-22 实测（别简化成一次 rect 校正）：
       `.msg` 带 content-visibility:auto ⇒ **远离视口的目标元素 rect 被归零**
       （实测 height=0、top=-2878 是失真值，offsetTop 也只有 56）。
       单次 `scrollTop += (rect.top - box.top)` 因此必然算错（实测偏了 6226px）。
       可靠做法 = ① 先用原生 `scrollIntoView` 让浏览器为它**强制布局**并粗定位
       （实测落点 top=0 精确命中）；② 再按 rect 迭代微校正补上边距（实测 3 轮收敛）。
       两者都保留：只做 ① 会贴死视口顶边、只做 ② 在元素未实现时无从下手。 */
  function jumpTo(i) {
    var t = _turns[i];
    if (!t) return;
    var a = area();
    if (!a) return;
    var el = t.el;

    try { el.scrollIntoView({ block: 'start' }); } catch (e) {
      a.scrollTop = a.scrollHeight;      // 极端兜底
    }

    var rounds = 0;
    (function fix() {
      var r = el.getBoundingClientRect();
      var box = a.getBoundingClientRect();
      if (!(r.height > 0)) {             // 仍未实现 → 再等一帧让它布局
        if (rounds++ < 4) { setTimeout(fix, 120); }
        return;
      }
      var d = (r.top - box.top) - 44;    // 顶部留 44px 呼吸，不贴死边
      if (Math.abs(d) > 6 && rounds < 6) {
        a.scrollTop += d;
        rounds++;
        setTimeout(fix, 110);
        return;
      }
      // 落点提示：短暂高亮目标消息（纯视觉，自动消失）
      try {
        el.classList.remove('cn-flash');
        void el.offsetWidth;             // 强制重排，保证动画可重复触发
        el.classList.add('cn-flash');
        setTimeout(function () { el.classList.remove('cn-flash'); }, 1400);
      } catch (e) {}
      scheduleActive();
    })();
  }

  function scheduleRefresh() {
    if (_refreshTimer) return;
    _refreshTimer = setTimeout(function () {
      _refreshTimer = 0;
      render();
    }, 180);
  }

  function init() {
    if (_inited) return;
    var col = document.getElementById('chat-col');
    var a = area();
    if (!col || !a) return;          // AI 页还没渲染，等下次
    _inited = true;

    if (getComputedStyle(col).position === 'static') col.style.position = 'relative';

    var nav = document.createElement('div');
    nav.id = NAV_ID;
    nav.style.display = 'none';
    nav.innerHTML = '<div class="cn-ticks" id="cn-ticks"></div>'
                  + '<div class="cn-panel" id="cn-panel"></div>';
    col.appendChild(nav);

    // 事件委托：刻度与清单行都带 data-ti
    nav.addEventListener('click', function (e) {
      var el = e.target && e.target.closest ? e.target.closest('[data-ti]') : null;
      if (!el) return;
      e.preventDefault();
      jumpTo(Number(el.dataset.ti));
    });

    a.addEventListener('scroll', scheduleActive, { passive: true });

    // 消息增删（分片渲染 / 发送新消息 / 加载更早 / 清空）都会走到这里
    if (typeof MutationObserver === 'function') {
      _obs = new MutationObserver(function () { scheduleRefresh(); });
      _obs.observe(a, { childList: true });
    }
    render();
  }

  // defer 脚本执行时 DOM 已解析；但 AI 页可能是懒渲染的，故兜底重试一次
  function bootstrap() {
    init();
    if (!_inited) setTimeout(init, 800);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }

  // 供外部（切换会话后）显式刷新
  window.convNavRefresh = function () { try { render(); } catch (e) {} };
})();
