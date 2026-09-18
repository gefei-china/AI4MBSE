/* ==========================================================================
 * perf.js — 性能 baseline 采集器（v6.2）
 * 位置：在 index.html 末尾引用，DOM 之后
 * 作用：
 *   1. 抓 first-paint / first-contentful-paint / dom-content-loaded / load
 *   2. 给核心路由切换（go('ai') / go('kb') / ...）打点 route-change-start/end
 *   3. 大表渲染时长（技能/本体/插件列表 cell 化测时）
 *   4. 数据上报到 console + localStorage['mbse_perf']，便于 Playwright 抓取
 * ========================================================================== */
(function () {
  'use strict';

  const store = {
    navigation: {},     // 各阶段首屏：fp / fcp / domContentLoaded / load / fullyLoaded
    routeChanges: [],   // { from, to, ms, ts } 切换耗时
    tableRenders: [],   // { table, rows, ms, ts } 大表渲染
    longTasks: [],      // { duration, startTime } 超过 50ms 的长任务
    startedAt: Date.now(),
  };

  // ── A. 第一指标：Navigation Timing ──
  function captureNavigation() {
    const nt = performance.getEntriesByType('navigation')[0];
    if (!nt) return;
    store.navigation = {
      dns_ms: Math.round(nt.domainLookupEnd - nt.domainLookupStart),
      connect_ms: Math.round(nt.connectEnd - nt.connectStart),
      ttfb_ms: Math.round(nt.responseStart - nt.requestStart),
      html_ms: Math.round(nt.responseEnd - nt.responseStart),
      domContentLoaded_ms: Math.round(nt.domContentLoadedEventEnd - nt.startTime),
      load_ms: Math.round(nt.loadEventEnd - nt.startTime),
      domInteractive_ms: Math.round(nt.domInteractive - nt.startTime),
      transferSize: nt.transferSize || 0,
    };
    // 尽量补上 paint 时点
    const paint = performance.getEntriesByType('paint');
    paint.forEach(p => { store.navigation[p.name + '_ms'] = Math.round(p.startTime); });
  }

  // ── B. Long Tasks（>50ms 任务，应 < 1 个/页） ──
  try {
    const lt = new PerformanceObserver(list => {
      list.getEntries().forEach(en => {
        store.longTasks.push({ duration: Math.round(en.duration), startTime: Math.round(en.startTime) });
      });
    });
    lt.observe({ entryTypes: ['longtask'] });
  } catch (_) { /* 旧浏览器无 longtask */ }

  // ── C. 路由切换打点（劫持全局 go()） ──
  // 注意：必须在 index.html 已定义 go() 之后注入
  window.__perf = {
    markRouteStart(name) {
      window.__perf._routeStart = performance.now();
      window.__perf._routeName = name || '?';
    },
    markRouteEnd(name) {
      const t = performance.now();
      if (window.__perf._routeStart) {
        const ms = Math.round(t - window.__perf._routeStart);
        store.routeChanges.push({
          from: '(prev)', to: name || window.__perf._routeName || '?',
          ms, ts: new Date().toISOString().slice(11, 19),
        });
        window.__perf._routeStart = null;
      }
    },
    markTableRender(table, rows) {
      store.tableRenders.push({
        table, rows,
        ms: Math.round(performance.now() - (window.__perf._tableStart || performance.now())),
        ts: new Date().toISOString().slice(11, 19),
      });
    },
    report() {
      const r = {
        ...store,
        avg: {
          routeChange_ms: store.routeChanges.length
            ? Math.round(store.routeChanges.reduce((a, b) => a + b.ms, 0) / store.routeChanges.length)
            : 0,
          tableRender_ms: store.tableRenders.length
            ? Math.round(store.tableRenders.reduce((a, b) => a + b.ms, 0) / store.tableRenders.length)
            : 0,
          longTaskCount: store.longTasks.length,
          longestTask_ms: store.longTasks.reduce((a, b) => Math.max(a, b.duration), 0),
        },
      };
      try { localStorage.setItem('mbse_perf', JSON.stringify(r)); } catch (_) {}
      console.log('[perf]', JSON.stringify(r, null, 2));
      return r;
    },
    store,
  };

  // ── D. 自动启动上报（页面 load 后 5 秒打一次 baseline） ──
  function bootReport() {
    captureNavigation();
    setTimeout(() => {
      window.__perf.report();
    }, 5000);
  }
  if (document.readyState === 'complete') bootReport();
  else window.addEventListener('load', bootReport);

  // ── E. 主动调用：mbse_perf() ──
  window.mbse_perf = () => window.__perf.report();

  // ── F. cytoscape init 调度工具（v6.3 P0）──
  // 用 requestIdleCallback 把 layout/fit 等重活推迟到空闲时段，避免同帧 longtask 累计。
  // 用法：window.__cyIdleRun(()=>{ cy.layout({...}).run(); cy.fit(...) }, 500)
  // 实现：setTimeout(0) 让出当前栈 → rAF 让出当前帧 → rIC 等 idle（生产环境更精细）
  window.__cyIdleRun = function (fn, timeout) {
    return setTimeout(()=>{
      const _afterRAF = ()=>{
        if (window.requestIdleCallback) {
          window.requestIdleCallback(fn, {timeout: timeout || 500});
        } else {
          fn();
        }
      };
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(_afterRAF);
      } else {
        _afterRAF();
      }
    }, 0);
  };

  // ── G. cytoscape 挂载工具（v6.4 P0）──
  // 1500 节点 init 平均 212ms——把整个 cytoscape() 创建推迟到下一帧 + idle 时段执行。
  // 用户感觉"画布立刻可见、节点渐进出现"。
  // 用法：window.__cyIdleMount(loadingEl, mountFn, doneFn)
  //   loadingEl: 显示 spinner 的容器（DOM 节点）；mount 完成后自动移除 .cy-loading 类
  //   mountFn:   () => cyInstance（创建 cytoscape）
  //   doneFn:    (cy) => void（cy 拿到后注册事件/idle fit 等）
  //
  // 实现：setTimeout(0) 让出当前同步栈 → rAF 让出当前帧 → rIC 等浏览器空闲
  // 注：Playwright headless 下 rAF/rIC 可能同步触发，但 setTimeout(0) 保证异步——可测可证
  window.__cyIdleMount = function (loadingEl, mountFn, doneFn, timeout) {
    if (loadingEl) loadingEl.classList.add('cy-loading');
    const _run = () => {
      let cy = null;
      try { cy = mountFn(); } catch (e) { /* mount 失败也不影响后续 */ }
      if (loadingEl) loadingEl.classList.remove('cy-loading');
      if (doneFn) try { doneFn(cy); } catch (e) { /* doneFn 内部容错 */ }
    };
    // 三级调度：setTimeout(0) 强制让出当前栈 → rAF 让出当前帧 → rIC 等 idle（生产环境更精细）
    return setTimeout(()=>{
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(()=>{
          if (window.requestIdleCallback) {
            window.requestIdleCallback(_run, {timeout: timeout || 800});
          } else {
            _run();
          }
        });
      } else {
        if (window.requestIdleCallback) {
          window.requestIdleCallback(_run, {timeout: timeout || 800});
        } else {
          _run();
        }
      }
    }, 0);
  };
})();
