/* 自进化周报页（evolution-report）：三段信息架构——要你决策的 / 本周体检 / 数据可信度
 *
 * 2026-10-20 产品化。原型的四段（省/抓/守/欠）是系统模块视角，用户看完仍
 * 要问「哪条要我点」「比上周好还是坏」「这数字是真的吗」。本页按这三个问题
 * 重排，域的划分退到体检分区内部。
 *
 * ★一条硬纪律贯穿全页：**不能点的事不渲染成按钮**。引擎对没有处理端点的
 * 条目给 action:null + not_actionable_reason，本页把它渲染成灰色说明条，
 * 而不是给一个点了没反应的按钮。
 */
(function () {
  const _V_LABEL = { clean: '正常', needs_attention: '待处理', insufficient_data: '本周无数据' };
  const _V_CLS = { clean: 'g', needs_attention: 'a', insufficient_data: '' };

  function _badge(v) {
    const k = v || 'insufficient_data';
    const cls = _V_CLS[k] || '';
    return `<span class="badge ${cls}">${_V_LABEL[k] || k}</span>`;
  }

  /* 环比箭头：只有 state==='ok' 且 delta 非空才画。
     delta 为 null（无对照）一律走 unknown 分支，绝不画成 0。 */
  function _deltaHtml(m) {
    if (m.state !== 'ok' || m.delta === null || m.delta === undefined) return '';
    const d = Number(m.delta);
    if (!d) return '<span style="color:var(--mut);font-size:11px;">持平</span>';
    // 涨不一定是坏（待人工确认多了说明有活动在发生），所以不按正负表红绿，
    // 只用中性箭头 + 数字，含义由指标名决定。
    const arrow = d > 0 ? '▲' : '▼';
    const col = d > 0 ? 'var(--amb)' : 'var(--grn)';
    return `<span style="color:${col};font-size:11px;margin-left:6px;">${arrow} ${d > 0 ? '+' : ''}${d}</span>`;
  }

  function _metricRow(m) {
    if (m.state !== 'ok') {
      // 本周无：显式写清为什么，而不是渲染一个 0
      return `<div style="display:flex;align-items:baseline;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);">
        <span style="font-size:12.5px;color:var(--mut);flex:1;">${esc(m.name)}</span>
        <span class="badge">本周无</span>
        <span style="font-size:11px;color:var(--mut);">${esc(m.why || '数据不足')}</span>
      </div>`;
    }
    return `<div style="display:flex;align-items:baseline;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);">
      <span style="font-size:12.5px;flex:1;">${esc(m.name)}</span>
      <b style="font-size:13px;">${m.current}</b>
      ${m.previous !== null && m.previous !== undefined
        ? `<span style="font-size:11px;color:var(--mut);">上周 ${m.previous}</span>` : ''}
      ${_deltaHtml(m)}
    </div>`;
  }

  function _actionItem(it) {
    const head = `<div style="display:flex;align-items:center;gap:8px;">
      <span class="badge">${esc(it.source_label)}</span>
      <span style="font-size:13px;font-weight:500;flex:1;">${esc(it.title)}</span>`;
    // ★真闭环：引擎给了 action 才渲染按钮
    if (it.action) {
      const alts = (it.action.alt || []).map(a =>
        `<button class="btn sm" data-ev-act="${esc(a.payload.decision)}" data-ev-path="${esc(it.action.path)}">${esc(a.label)}</button>`).join('');
      return `<div style="padding:8px 0;border-bottom:1px solid var(--line);">
        ${head}
        <button class="btn sm pri" data-ev-act="${esc(it.action.payload.decision)}" data-ev-path="${esc(it.action.path)}">${esc(it.action.label)}</button>
        ${alts}
      </div>
      ${it.detail ? `<div style="font-size:11.5px;color:var(--mut);padding:0 0 8px;">${esc(it.detail)}</div>` : ''}`;
    }
    // ★不能点：渲染成说明条而不是死按钮
    const why = it.not_actionable_reason || '暂无处理入口';
    return `<div style="padding:8px 0;border-bottom:1px solid var(--line);">
      ${head}
      <span class="badge">只能看</span>
      <div style="font-size:11.5px;color:var(--mut);margin-top:3px;">
        ${it.detail ? esc(it.detail) + ' ' : ''}（${esc(why)}）
      </div>
    </div>`;
  }

  async function loadEvolutionReport() {
    const box = document.getElementById('evr-body');
    if (!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    let rep;
    try {
      rep = await api('/api/evolution-report');
    } catch (e) {
      box.innerHTML = `<div class="note" style="color:var(--red);">周报加载失败：${esc(String(e && e.message || e))}</div>`;
      return;
    }
    // enabled=false 是配置关闭，不是失败——静默收起，不显示红色错误条
    if (rep && rep.enabled === false) {
      box.innerHTML = `<div class="note">${esc(rep.reason || '自进化周报已关闭')}</div>`;
      return;
    }
    const a = rep.actions || {}, d = rep.digest || {}, c = rep.confidence || {};
    const head = document.getElementById('evr-head');
    if (head) {
      head.innerHTML = `${_badge(rep.overall)}
        <span style="font-size:12px;color:var(--mut);margin-left:8px;">
          窗口 ${rep.window_days} 天 · 生成于 ${esc(rep.generated_at)}
        </span>`;
    }

    let h = '';

    /* ── 第 1 块：要你决策的（页面主体，最先看到） ── */
    h += `<div class="panel" style="margin-bottom:14px;">
      <div class="ph">要你决策的
        <span class="badge ${a.actionable_count ? 'a' : ''}">${a.count || 0} 项</span>
        ${a.actionable_count
          ? `<span class="badge g">${a.actionable_count} 项可一键处理</span>`
          : `<span class="badge">暂无一键处理入口</span>`}
        <span style="flex:1"></span>
        <span style="font-weight:400;font-size:11.5px;color:var(--mut);">${esc(a.reason || '')}</span>
      </div>
      <div class="pb" id="evr-actions">`;
    if ((a.items || []).length) {
      h += a.items.map(_actionItem).join('');
    } else {
      h += '<div style="font-size:12.5px;color:var(--mut);padding:6px 0;">本周没有需要你决策的事项。</div>';
    }
    if ((a.collect_failures || []).length) {
      h += '<div class="note" style="color:var(--amb);margin-top:10px;">部分来源采集失败（不影响其余条目）：'
        + a.collect_failures.map(f => esc(f.label) + ' — ' + esc(f.error)).join('；') + '</div>';
    }
    h += '</div></div>';

    /* ── 第 2 块：本周体检（环比，不是孤立快照） ── */
    h += '<div class="panel" style="margin-bottom:14px;"><div class="ph">本周体检'
      + '<span style="font-weight:400;font-size:11.5px;color:var(--mut);">按窗口对比上周</span></div><div class="pb">';
    (d.sections || []).forEach(sec => {
      h += `<div style="margin-bottom:12px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
          ${_badge(sec.verdict)}
          <b style="font-size:13px;">${esc(sec.title)}</b>
          <span style="font-size:11.5px;color:var(--mut);flex:1;">${esc(sec.subtitle || '')}</span>
          <span style="font-size:11px;color:var(--mut);">${sec.known}/${sec.total} 项有数据</span>
        </div>`;
      (sec.metrics || []).forEach(m => { h += _metricRow(m); });
      h += '</div>';
    });
    if ((d.collect_failures || []).length) {
      h += '<div class="note" style="color:var(--amb);">分区采集失败：'
        + d.collect_failures.map(f => esc(f.title) + ' — ' + esc(f.error)).join('；') + '</div>';
    }
    h += '</div></div>';

    /* ── 第 3 块：数据可信度（这一段的存在本身就是可信度声明） ── */
    h += `<div class="panel"><div class="ph">数据可信度
      ${_badge(c.verdict)}<span style="flex:1"></span>
      <span style="font-weight:400;font-size:11.5px;color:var(--mut);">${esc(c.reason || '')}</span>
    </div><div class="pb">`;
    if ((c.unknown_metrics || []).length) {
      h += '<div style="font-size:12px;color:var(--mut);margin-bottom:6px;">以下指标本周无数据，'
        + '<b>不能读作 0，也不能读作「正常」</b>：</div>';
      h += c.unknown_metrics.map(u =>
        `<div style="font-size:11.5px;color:var(--mut);padding:2px 0;">· ${esc(u.where)} —— ${esc(u.why || '')}</div>`).join('');
    }
    (c.notes || []).forEach(n => {
      h += `<div class="note" style="margin-top:8px;">${esc(n)}</div>`;
    });
    h += `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line);
      font-size:11px;color:var(--mut);">${esc(c.disclaimer || '')}</div></div></div>`;

    box.innerHTML = h;
    _bindActions();
  }

  function _bindActions() {
    const box = document.getElementById('evr-actions');
    if (!box) return;
    box.querySelectorAll('button[data-ev-path]').forEach(btn => {
      btn.onclick = async () => {
        const path = btn.dataset.evPath, decision = btn.dataset.evAct;
        const old = btn.textContent;
        btn.disabled = true; btn.textContent = '处理中…';
        try {
          const r = await api(path, { method: 'POST', body: { decision } });
          if (r && r.ok === false) throw new Error(r.error || '操作失败');
          toast('已记录裁决，刷新查看最新状态');
          loadEvolutionReport();
        } catch (e) {
          toast('裁决失败：' + (e && e.message || e), true);
          btn.disabled = false; btn.textContent = old;
        }
      };
    });
  }

  window.loadEvolutionReport = loadEvolutionReport;
})();