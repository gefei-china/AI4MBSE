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
    if (!d) return '<span style="color:var(--mut);font-size:12.5px;">持平</span>';
    // 涨不一定是坏（待人工确认多了说明有活动在发生），所以不按正负表红绿，
    // 只用中性箭头 + 数字，含义由指标名决定。
    const arrow = d > 0 ? '▲' : '▼';
    const col = d > 0 ? 'var(--amb)' : 'var(--grn)';
    return `<span style="color:${col};font-size:12.5px;margin-left:6px;">${arrow} ${d > 0 ? '+' : ''}${d}</span>`;
  }

  function _metricRow(m) {
    if (m.state !== 'ok') {
      // 本周无：显式写清为什么，而不是渲染一个 0
      return `<div style="display:flex;align-items:baseline;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);">
        <span style="font-size:13.5px;color:var(--mut);flex:1;">${esc(m.name)}</span>
        <span class="badge">本周无</span>
        <span style="font-size:12.5px;color:var(--mut);">${esc(m.why || '数据不足')}</span>
      </div>`;
    }
    return `<div style="display:flex;align-items:baseline;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);">
      <span style="font-size:13.5px;flex:1;">${esc(m.name)}</span>
      <b style="font-size:14.5px;">${m.current}</b>
      ${m.previous !== null && m.previous !== undefined
        ? `<span style="font-size:12.5px;color:var(--mut);">上周 ${m.previous}</span>` : ''}
      ${_deltaHtml(m)}
    </div>`;
  }

  function _actionItem(it) {
    /* ★模板必须逐标签配平：head 做成「自闭合」的小函数——曾经让 head 的
       <div> 悬空、由调用处补 </div>，12 个条目各漏一个，浏览器解析时把
       后续的体检/可信度 panel 全部嵌进最后一个条目 div 内部（面板查询
       panelCount=1 才暴露）。innerHTML 不报错、console 干净，只有
       「数一数渲染出几个 panel」和静态配平哨兵能抓到它。
       自闭合还有个好处：模板文本上 open/close 严格相等，门禁 E8 的
       静态配平断言才成立（共享半个模板会让文本计数天然失配）。 */
    const head = (extra) => `<div style="display:flex;align-items:center;gap:8px;">
      <span class="badge">${esc(it.source_label)}</span>
      <span style="font-size:14.5px;font-weight:500;flex:1;">${esc(it.title)}</span>${extra || ''}</div>`;
    // ★真闭环：引擎给了 action 才渲染按钮
    if (it.action) {
      const alts = (it.action.alt || []).map(a =>
        `<button class="btn sm" data-ev-act="${esc(a.payload.decision)}" data-ev-path="${esc(it.action.path)}">${esc(a.label)}</button>`).join('');
      return `<div style="padding:8px 0;border-bottom:1px solid var(--line);">
        ${head('')}
        <div style="display:flex;align-items:center;gap:8px;">
          <button class="btn sm pri" data-ev-act="${esc(it.action.payload.decision)}" data-ev-path="${esc(it.action.path)}">${esc(it.action.label)}</button>
          ${alts}
        </div>
        ${it.detail ? `<div style="font-size:12.5px;color:var(--mut);padding:0 0 8px;">${esc(it.detail)}</div>` : ''}
      </div>`;
    }
    // ★不能点：渲染成说明条而不是死按钮
    // ★ 2026-11-06 P0：改成可展开——把「为什么卡住 / 下一步是什么」说清楚，
    //   而不是让「只能看」三个字成为终点。仍**不新增越权动作按钮**。
    const why = it.not_actionable_reason || '暂无处理入口';
    return `<div style="padding:8px 0;border-bottom:1px solid var(--line);">
      ${head('<span class="badge">只能看</span><button class="btn sm ghost" data-ev-toggle style="margin-left:6px;"><span class="chev">▸</span> 为什么</button>')}
      <div class="ev-fold-body" style="display:none;font-size:12.5px;color:var(--mut);padding:4px 0 6px;">
        ${it.detail ? esc(it.detail) + ' ' : ''}（${esc(why)}）<br>
        <b>下一步</b>：本条没有可一键处理的端点；需由对应模块补齐处理入口，或由管理员在「运行监控」人工处置。
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
        <span style="font-size:13.5px;color:var(--mut);margin-left:8px;">
          窗口 ${rep.window_days} 天 · 生成于 ${esc(rep.generated_at)}
        </span>`;
    }

    let h = '';

    /* ── 置顶块：本周因你而改变（2026-11-06 P0）──
         用户最关心「我的反馈后来怎样了」，故置于首屏最上方。
         实测放在三段之后时 panelTop=1728 > 视口 900 ⇒ 首屏完全不可见。
            把「我的反馈后来怎样了」如实说出来。空数据必须显示卡点，不得显示「一切正常」。 */
    const ft = rep.feedback_trace || null;
    if (ft) {
      const vCls = ft.verdict === 'needs_attention' ? 'a' : (ft.verdict === 'ok' ? 'g' : '');
      h += `<div class="panel" style="margin-bottom:14px;"><div class="ph">本周因你而改变
        <span class="badge ${vCls}">${esc(ft.verdict || '')}</span>
        <span style="flex:1"></span>
        <span style="font-weight:400;font-size:12.5px;color:var(--mut);">收到 ${ft.received||0} 条 · 已进下游 ${ft.consumed||0} 条 · 卡住 ${ft.stuck||0} 条</span>
      </div><div class="pb">
        <div style="font-size:13.5px;margin-bottom:8px;">${esc(ft.reason || '')}</div>`;
      if ((ft.items || []).length) {
        h += '<table class="t"><tr><th>时间</th><th>编号</th><th>信号</th><th>状态</th><th>当前关联</th></tr>'
          + ft.items.map(x => `<tr>
              <td style="font-size:12.5px;white-space:nowrap;">${esc(String(x.created_at||'').slice(5,16))}</td>
              <td>feedback#${esc(x.id)} <span style="color:var(--mut);font-size:12px;">消息 #${esc(x.message_id)}</span></td>
              <td>${esc(x.quality || '—')}</td>
              <td><span class="st ${x.state==='verified'?'ok':'w'}">${esc(x.state||'new')}</span></td>
              <td style="font-size:12.5px;color:var(--mut);">${esc(x.link_kind ? x.link_kind + ' #' + x.link_id : '—')}</td>
            </tr>`).join('') + '</table>';
      } else {
        h += '<div style="font-size:13px;color:var(--mut);">窗口内没有可展示的反馈明细。</div>';
      }
      const tk = rep.tick || {};
      const tkTxt = tk.running
        ? `自动触发器运行中 · ${esc(tk.mode || '')} 模式 · 每 ${tk.interval_s||0}s 扫描 · 已跑 ${tk.ticks||0} 轮`
        : `自动触发器未运行（${esc(tk.reason || (tk.enabled ? '线程未启动' : '已关闭'))}）`;
      h += `<div class="note" style="margin-top:10px;">${tkTxt}<br>
        每条反馈的完整处理时间线见「⋯ 更多 → 我的反馈」。
        ${esc(ft.disclaimer || '')}</div></div></div>`;
    }


    /* ── 第 1 块：要你决策的（页面主体，最先看到） ── */
    h += `<div class="panel" style="margin-bottom:14px;">
      <div class="ph">要你决策的
        <span class="badge ${a.actionable_count ? 'a' : ''}">${a.count || 0} 项</span>
        ${a.actionable_count
          ? `<span class="badge g">${a.actionable_count} 项可一键处理</span>`
          : `<span class="badge">暂无一键处理入口</span>`}
        <span style="flex:1"></span>
        <span style="font-weight:400;font-size:12.5px;color:var(--mut);">${esc(a.reason || '')}</span>
      </div>
      <div class="pb" id="evr-actions">`;
    if ((a.items || []).length) {
      h += a.items.map(_actionItem).join('');
    } else {
      h += '<div style="font-size:13.5px;color:var(--mut);padding:6px 0;">本周没有需要你决策的事项。</div>';
    }
    if ((a.collect_failures || []).length) {
      h += '<div class="note" style="color:var(--amb);margin-top:10px;">部分来源采集失败（不影响其余条目）：'
        + a.collect_failures.map(f => esc(f.label) + ' — ' + esc(f.error)).join('；') + '</div>';
    }
    h += '</div></div>';

    /* ── 第 2 块：本周体检（环比，不是孤立快照） ── */
    h += '<div class="panel" style="margin-bottom:14px;"><div class="ph">本周体检'
      + '<span style="font-weight:400;font-size:12.5px;color:var(--mut);">按窗口对比上周</span></div><div class="pb">';
    (d.sections || []).forEach(sec => {
      h += `<div style="margin-bottom:12px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
          ${_badge(sec.verdict)}
          <b style="font-size:14.5px;">${esc(sec.title)}</b>
          <span style="font-size:12.5px;color:var(--mut);flex:1;">${esc(sec.subtitle || '')}</span>
          <span style="font-size:12.5px;color:var(--mut);">${sec.known}/${sec.total} 项有数据</span>
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
      <span style="font-weight:400;font-size:12.5px;color:var(--mut);">${esc(c.reason || '')}</span>
    </div><div class="pb">`;
    if ((c.unknown_metrics || []).length) {
      h += '<div style="font-size:13.5px;color:var(--mut);margin-bottom:6px;">以下指标本周无数据，'
        + '<b>不能读作 0，也不能读作「正常」</b>：</div>';
      h += c.unknown_metrics.map(u =>
        `<div style="font-size:12.5px;color:var(--mut);padding:2px 0;">· ${esc(u.where)} —— ${esc(u.why || '')}</div>`).join('');
    }
    (c.notes || []).forEach(n => {
      h += `<div class="note" style="margin-top:8px;">${esc(n)}</div>`;
    });
    h += `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line);
      font-size:12.5px;color:var(--mut);">${esc(c.disclaimer || '')}</div></div></div>`;

    box.innerHTML = h;
    _bindActions();
  }

  function _bindActions() {
    const box = document.getElementById('evr-actions');
    if (!box) return;
    // ★ 2026-11-06 P0：「只能看」可展开（照抄 05-markdown.js 的 codeFoldToggle 模式）
    box.querySelectorAll('[data-ev-toggle]').forEach(btn => {
      btn.onclick = () => {
        const row = btn.closest('div[style*="border-bottom"]');
        const body = row && row.querySelector('.ev-fold-body');
        if (!body) return;
        const open = body.style.display === 'none';
        body.style.display = open ? 'block' : 'none';
        const chev = btn.querySelector('.chev');
        if (chev) chev.textContent = open ? '▾' : '▸';
      };
    });
    box.querySelectorAll('button[data-ev-path]').forEach(btn => {
      btn.onclick = async () => {
        const path = btn.dataset.evPath, decision = btn.dataset.evAct;
        const old = btn.textContent;
        btn.disabled = true; btn.textContent = '处理中…';
        try {
          // ★ 2026-11-06：旧写法传裸对象，fetch 会发 [object Object]，后端收不到 decision
          const r = await api(path, { method: 'POST', body: JSON.stringify({ decision }) });
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

  // ★ 2026-11-06 P1：周报页轻量自动刷新（30s，只在页面可见时），
  //   让「系统变了」能自己冒出来，不必重进页面。模板同 48-eval.js 的作业卡轮询。
  setInterval(function(){
    const pg = document.getElementById('pg-evolution');
    if(!pg || !pg.classList.contains('on')) return;
    loadEvolutionReport();
  }, 30000);

  window.loadEvolutionReport = loadEvolutionReport;
})();