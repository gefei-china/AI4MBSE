/* 评测管理页（eval management）：三段信息架构 —— 现在什么状态 / 历史怎么变 / 什么在拦着我
 *
 * 2026-10-22 新增。落点依据：评估发现"检索质量的度量比决策质量更完备，
 * 却完全没被使用"，以及"评测跑得怎么样只能问开发看 JSON 文件"——后者等于
 * 没有可见性。本页是那个可见性的落点。
 *
 * ★为什么放在「更多」浮层而不是知识中心页内：
 *   评测的对象不是"知识"，而是"这套系统现在答得好不好"——它横跨检索/决策/
 *   记忆三类能力，也横跨开发（门禁）与产品（趋势）两个视角。按工程既有的
 *   「管理类入口统一收口」原则（用户与权限 2026-09-14、审批管理 2026-09-17
 *   均按此原则补进「更多」），与「自进化周报」完全同构。
 *
 * ★三条硬纪律贯穿全页：
 *   1. **只渲染不计算** —— 阈值/判定全在后端（core/config.py 的 eval_gates
 *      是唯一真源），前端不重写一遍阈值口径。
 *   2. **缺失即 null 不填 0** —— 记忆评测没有 doc_recall_at_5 这个概念，
 *      渲染成 0 会被读成"召回 0 分"。后端已按 null 返回，这里如实显示「—」。
 *   3. **不能点的事不渲染成按钮** —— 本页全部只读，没有处理端点的东西
 *      一律渲染成说明文字，不给点了没反应的按钮。
 */
(function () {
  const _KIND_ORDER = ['rag_retrieval', 'memory_quality', 'intent_routing', 'doc_extract', ''];

  function _kindLabel(k) {
    const m = { rag_retrieval: 'RAG 检索评测', memory_quality: '记忆质量评测',
                intent_routing: '意图路由评测', doc_extract: '文档抽取评测',
                '': '历史记录（未标注类型）' };
    return m[k] || (k || '未标注类型');
  }

  function _kindBadge(k) {
    const cls = k === 'rag_retrieval' ? 'ok' : (k === '' ? '' : 'a');
    return `<span class="badge ${cls}">${esc(_kindLabel(k))}</span>`;
  }

  /* 指标值渲染：null/undefined 一律显示「—」并标注"不适用"，
     绝不显示 0 —— 0 与"这个类型没这个指标"是两件事。

     ★三色判定（好/中/差）的阈值**不在前端写死** —— 它由后端按
       core/config.py 的 eval_gates 逐指标给出（status: ok/warn/alert），
       前端只渲染。这条纪律来自 15-kb.js 文件头的明文约定：
       「前端只渲染不计算，阈值三色 status 由后端判定」。
       本页第一版曾在前端写 0.15/0.85/0.30/0.70 四个魔数，
       门禁 A8 判红后移除 —— 前端写阈值 = 改阈值要改两个地方 = 迟早漂移。 */
  function _metricVal(v, status, direction) {
    if (v === null || v === undefined) {
      return '<span style="color:var(--mut);">—</span>'
        + '<span style="font-size:10px;color:var(--mut);margin-left:4px;">不适用</span>';
    }
    const num = Number(v);
    if (isNaN(num)) return `<span style="color:var(--mut);">${esc(String(v))}</span>`;
    const cls = status === 'ok' ? 'ok' : (status === 'warn' ? 'w' : (status === 'alert' ? 'r' : 'g'));
    const col = status === 'ok' ? 'var(--grn)'
      : (status === 'warn' ? 'var(--amb)' : (status === 'alert' ? 'var(--red)' : 'var(--color-text)'));
    const tip = direction ? ` title="${esc(direction)}"` : '';
    return `<b style="color:${col};${tip}">${num.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')}</b>`;
  }

  /* 可信度徽章：这是本页最该被看见的东西。
     trustworthy = 有负对照 且 无语料漂移。缺任一 ⇒ 这个分数不能当依据。 */
  function _trustBadge(item) {
    if (item.trustworthy) {
      return '<span class="st ok" title="有负对照且无语料漂移 ⇒ 该分数可作依据">可信</span>';
    }
    const why = [];
    if (!item.negative_control) why.push('无负对照（无法排除假绿）');
    if (item.fingerprint_drift && item.fingerprint_drift.length) why.push('语料已漂移');
    return `<span class="st r" title="${esc(why.join('；') || '未知原因')}">不可信</span>`;
  }

  /* ══ 段 ①：现在什么状态 ══ */
  function _renderLatest(d) {
    const latest = d.latest || {};
    const cov = d.coverage || {};
    const kinds = Object.keys(latest).sort((a, b) => {
      const ia = _KIND_ORDER.indexOf(a), ib = _KIND_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });

    if (!kinds.length) {
      return '<div class="panel"><div class="ph">各能力最新分</div>'
        + '<div class="pb" style="color:var(--mut);font-size:12px;">'
        + '还没有任何评测记录。生成方式：<code>tools/eval/build_evalset.py</code> → '
        + '<code>tools/eval/run_eval.py --control</code> → <code>tools/eval/save_report.py</code>'
        + '</div></div>';
    }

    const cards = kinds.map(k => {
      const it = latest[k] || {};
      const cols = it.metric_columns || [];
      const st = it.metric_status || {};
      const metrics = cols.length ? cols.map(c =>
        `<div style="display:flex;align-items:baseline;gap:6px;padding:3px 0;">
          <span style="font-size:11.5px;color:var(--mut);flex:1;">${esc(c.label)}</span>
          ${_metricVal(it[c.key], st[c.key], c.direction)}</div>`).join('')
        : '<div style="font-size:11.5px;color:var(--mut);">该类型无可展示指标</div>';
      const drift = (it.fingerprint_drift && it.fingerprint_drift.length)
        ? `<div style="font-size:10.5px;color:var(--red);margin-top:4px;">⚠ 语料漂移：${esc(it.fingerprint_drift[0])}</div>`
        : '';
      return `<div class="asset" style="text-align:left;">
        <div class="l" style="display:flex;justify-content:space-between;gap:6px;align-items:center;">
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(it.kind_label || _kindLabel(k))}</span>
          ${_trustBadge(it)}</div>
        <div class="l" style="font-size:10.5px;color:var(--mut);">${esc(it.created_at || '')}</div>
        <div style="margin-top:6px;">${metrics}</div>
        ${drift}
        <div class="l" style="font-size:10.5px;color:var(--mut);margin-top:4px;">${esc(it.doc_name || '')}</div>
      </div>`;
    }).join('');

    // 能力覆盖：缺口要显式说出来
    const missing = (cov.missing || []).map(k => esc(_kindLabel(k))).join('、');
    const genWarn = cov.generation_quality === false
      ? `<div style="font-size:11.5px;color:var(--amb);margin:0 0 10px;line-height:1.7;">
           ⚠ <b>当前没有生成质量评测</b>（答案忠实度 / 答得对不对）——检索与决策分数高
           <b>不代表答案对</b>。这是本体系已知最大的能力空白。</div>`
      : '';
    const missLine = missing
      ? `<div style="font-size:11px;color:var(--mut);margin:0 0 10px;">尚无记录的评测类型：${missing}</div>`
      : '';

    return `<div class="panel" style="margin-bottom:14px;">
      <div class="ph">① 现在什么状态 <span class="tag">各能力最新分</span><span style="flex:1"></span>
        <span style="font-size:10.5px;color:var(--mut);">阈值口径见 core/config.py eval_gates</span></div>
      <div class="pb">
        ${genWarn}${missLine}
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;">${cards}</div>
      </div>
    </div>`;
  }

  /* ══ 段 ②：历史怎么变 ══ */
  function _renderTrends(d) {
    const trends = d.trends || {};
    const kinds = Object.keys(trends).sort((a, b) => {
      const ia = _KIND_ORDER.indexOf(a), ib = _KIND_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    if (!kinds.length) {
      return '<div class="panel"><div class="ph">② 历史怎么变</div>'
        + '<div class="pb" style="color:var(--mut);font-size:12px;">暂无历史序列。</div></div>';
    }
    const colMeta = d.metric_columns || {};

    const blocks = kinds.map(k => {
      const seq = trends[k] || [];
      const cols = colMeta[k] || [];
      if (!seq.length) {
        return `<div style="margin-bottom:12px;">
          <div style="font-size:11.5px;color:var(--mut);margin-bottom:4px;">${_kindBadge(k)}</div>
          <div style="font-size:11.5px;color:var(--mut);">无数据点</div></div>`;
      }
      // 逐指标一行：最近 N 次的取值序列（null 断点，不补 0）
      const rows = cols.map(c => {
        const vals = seq.map(p => p[c.key]);
        const hasData = vals.some(v => v !== null && v !== undefined);
        const line = vals.map(v => (v === null || v === undefined)
          ? '<span style="color:var(--mut);">—</span>'
          : `<span style="color:var(--color-text);">${Number(v).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')}</span>`)
          .join(' <span style="color:var(--mut);">→</span> ');
        const tpl = seq.map(p => p.trustworthy
          ? '<span class="st ok" style="font-size:9px;">可</span>'
          : '<span class="st r" style="font-size:9px;">疑</span>').join(' ');
        return `<tr>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;color:var(--mut);">${esc(c.label)}</td>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);font-size:11.5px;">${hasData ? line : '<span style="color:var(--mut);">该类型无此指标</span>'}</td>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:10px;">${tpl}</td>
        </tr>`;
      }).join('');

      return `<div style="margin-bottom:14px;">
        <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px;">
          ${_kindBadge(k)} <span style="font-size:10.5px;">共 ${seq.length} 次</span>
          <span style="font-size:10.5px;color:var(--mut);">（旧 → 新）</span></div>
        <table class="t" style="width:100%;font-size:11.5px;">
          <tr><th style="padding:4px 8px;">指标</th><th style="padding:4px 8px;">取值序列</th>
            <th style="padding:4px 8px;">可信度</th></tr>
          ${rows || '<tr><td colspan="3" style="color:var(--mut);font-size:11px;">该类型无可展示指标</td></tr>'}
        </table>
      </div>`;
    }).join('');

    return `<div class="panel" style="margin-bottom:14px;">
      <div class="ph">② 历史怎么变 <span class="tag">按类型分组</span></div>
      <div class="pb">${blocks}</div>
    </div>`;
  }

  /* ══ 段 ③：什么在拦着我 ══ */
  function _renderGates(g) {
    if (!g) {
      return '<div class="panel"><div class="ph">③ 什么在拦着我</div>'
        + '<div class="pb" style="color:var(--mut);font-size:12px;">门禁状态不可用</div></div>';
    }
    const c = g.counts || {};
    const rows = (g.gates || []).filter(x => x.status !== 'wired');
    const wiredList = (g.gates || []).filter(x => x.status === 'wired');

    // 未接线门禁是**真问题**（元门禁会判红），要显眼
    const unwired = (g.gates || []).filter(x => x.status === 'unwired');
    const head = unwired.length
      ? `<div style="font-size:11.5px;color:var(--red);margin:0 0 10px;">
           ⚠ <b>${unwired.length} 个门禁未接线</b> —— 写了但没进 CI，等于没写。
           门禁元门禁（verify_gate_wiring）会因此判红。</div>`
      : `<div style="font-size:11.5px;color:var(--grn);margin:0 0 10px;">
           ✅ 全部门禁均已接线或带理由豁免</div>`;

    const exemptRows = rows.map(x => `<tr>
      <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;">
        ${esc(x.name)}</td>
      <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;">
        <span class="st ${x.status === 'unwired' ? 'r' : 'w'}">${x.status === 'unwired' ? '未接线' : '已豁免'}</span></td>
      <td style="padding:4px 8px;border-bottom:1px solid var(--line);color:var(--mut);font-size:11px;">
        ${esc(x.exemption_reason || '（无理由 —— 元门禁会判红）')}</td>
    </tr>`).join('');

    const detail = rows.length
      ? `<table class="t" style="width:100%;font-size:11.5px;">
          <tr><th style="padding:4px 8px;">门禁</th><th style="padding:4px 8px;">状态</th>
            <th style="padding:4px 8px;">豁免理由 / 缺口</th></tr>
          ${exemptRows}</table>`
      : '<div style="font-size:11.5px;color:var(--mut);">无豁免、无未接线门禁。</div>';

    return `<div class="panel" style="margin-bottom:14px;">
      <div class="ph">③ 什么在拦着我 <span class="tag">门禁接线状态</span><span style="flex:1"></span>
        <span style="font-size:10.5px;color:var(--mut);">共 ${c.total || 0} 个 · 已接线 ${c.wired || 0} · 豁免 ${c.exempted || 0}</span></div>
      <div class="pb">
        ${head}
        ${detail}
        <div style="font-size:10.5px;color:var(--mut);margin-top:10px;">
          已接线的 ${wiredList.length} 个门禁不在此列出 —— 它们正在拦住回归。
          门禁清单与豁免理由的权威来源是 <code>tools/verify/verify_gate_wiring.py</code>。</div>
      </div>
    </div>`;
  }

  /* ══ 装配 ══ */
  function _mount(html) {
    const box = document.getElementById('evm-body');
    if (!box) return;
    box.innerHTML = html;
  }

  async function loadEvalManagement() {
    const box = document.getElementById('evm-body');
    if (!box) return;
    if (!box.innerHTML || box.innerHTML.indexOf('加载评测数据') < 0) {
      // 只在"还没开始加载"时显示加载态；重载时保留旧内容，避免闪白。
      // ⚠️ 不用 box.dataset 记录加载状态 —— Node vm 的 DOM stub 没有 dataset
      //   （实测渲染冒烟报 "Cannot read properties of undefined (reading 'loaded')"）。
      //   用 innerHTML 判断既真实又不依赖 stub 的额外能力。
      if (!box.innerHTML) box.innerHTML = '<div class="loading">加载评测数据…</div>';
    }
    try {
      const [ov, hist] = await Promise.all([
        api('/api/eval/overview'),
        api('/api/eval/history?limit=20')
      ]);
      if (ov && ov.ok === false) {
        _mount('<div class="panel"><div class="ph">评测管理</div><div class="pb">'
          + '<div style="color:var(--red);font-size:12px;">评测数据不可用：'
          + esc(ov.error || '未知原因') + '</div>'
          + '<div style="color:var(--mut);font-size:11.5px;margin-top:6px;">'
          + '库若未迁移 eval_kind 列，请先跑一次 init_db 完成迁移。</div></div></div>');
        return;
      }
      if (hist && hist.ok === false) {
        _mount('<div class="panel"><div class="ph">评测管理</div><div class="pb">'
          + '<div style="color:var(--red);font-size:12px;">历史数据不可用：'
          + esc(hist.error || '未知原因') + '</div></div></div>');
        return;
      }
      const g = ov && ov.gates ? ov.gates : null;
      _mount(
        _renderLatest(ov || {}) +
        _renderTrends(hist || {}) +
        _renderGates(g) +
        '<div style="font-size:10.5px;color:var(--mut);padding:2px 2px 12px;line-height:1.7;">'
        + '本页全部只读。指标阈值唯一真源：<code>core/config.py → eval_gates</code>；'
        + '详细用法见 <code>docs/评测工具使用指南-20261022.md</code>。</div>'
      );
    } catch (e) {
      _mount('<div class="panel"><div class="ph">评测管理</div><div class="pb">'
        + '<div style="color:var(--red);font-size:12px;">加载失败：'
        + esc(e && e.message || e) + '</div></div></div>');
    }
  }

  window.loadEvalManagement = loadEvalManagement;
})();
