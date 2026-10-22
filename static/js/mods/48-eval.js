/* 评测管理页（eval management）：三段信息架构 —— 现在什么状态 / 历史怎么变 / 什么在拦着我
 *
 * 2026-10-22 新增。落点依据：评估发现"检索质量的度量比决策质量更完备，
 * 却完全没被使用"，以及"评测跑得怎么样只能问开发看 JSON 文件"——后者等于
 * 没有可见性。本页是那个可见性的落点。
 *
 * ★2026-10-22 晚：下钻能力（drill-down）。行业标杆（LangSmith/Langfuse/
 *   Braintrust）的评测页都是"总览 → 数据集 → 单条证据"的可下钻结构：
 *   纯展示只能回答"好不好"，回答不了"哪条坏了、为什么"——而后者才是
 *   本工程评测的既定用途（使用指南 §4.2 快速定位）。数据一直都在
 *   （detail JSON 里有 wrong 案例/混淆矩阵/负对照/judge 明细），缺的
 *   只是通路：指标卡与历史行可点击 → GET /api/eval/report?id= → 右侧
 *   滑出面板按类型渲染证据段。聚合层保持只读（不在这页发起动作），
 *   证据层全下钻。
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
  const _KIND_ORDER = ['rag_retrieval', 'memory_quality', 'intent_routing', 'trace_consistency', 'doc_extract', 'generation_quality', ''];

  function _kindLabel(k) {
    const m = { rag_retrieval: 'RAG 检索评测', memory_quality: '记忆质量评测',
                intent_routing: '意图路由评测', trace_consistency: 'Trace 一致性评测',
                doc_extract: '文档抽取评测',
                generation_quality: '生成质量评测',
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
      return `<div class="asset" style="text-align:left;cursor:pointer;" title="点击查看该评测的证据明细（错例/混淆矩阵/负对照）" onclick="openEvalReport(${it.id})">
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
      // ★2026-10-22 晚：每个取值可点 → 打开那一次评测的证据明细
      //   （"历史怎么变"必须能下钻到"那一次到底发生了什么"，否则趋势线
      //   只是一串数字）；右端附内联 SVG 迷你趋势线（零外部依赖）。
      const rows = cols.map(c => {
        const vals = seq.map(p => p[c.key]);
        const hasData = vals.some(v => v !== null && v !== undefined);
        const line = vals.map((v, i) => {
          if (v === null || v === undefined) {
            return '<span style="color:var(--mut);">—</span>';
          }
          const pt = seq[i] || {};
          const tip = pt.at ? ('查看 ' + String(pt.at) + ' 那次评测的证据') : '查看证据';
          return `<span style="cursor:pointer;text-decoration:underline dotted;"
            onclick="openEvalReport(${pt.id})" title="${esc(tip)}">${
              Number(v).toFixed(3).replace(/0+$/, '').replace(/\.$/, '')}</span>`;
        }).join(' <span style="color:var(--mut);">→</span> ');
        const tpl = seq.map(p => p.trustworthy
          ? '<span class="st ok" style="font-size:9px;">可</span>'
          : '<span class="st r" style="font-size:9px;">疑</span>').join(' ');
        return `<tr>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;color:var(--mut);">${esc(c.label)}</td>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);font-size:11.5px;">${hasData ? line : '<span style="color:var(--mut);">该类型无此指标</span>'}</td>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:10px;">${tpl}</td>
          <td style="padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;">${_sparkline(vals)}</td>
        </tr>`;
      }).join('');

      return `<div style="margin-bottom:14px;">
        <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px;">
          ${_kindBadge(k)} <span style="font-size:10.5px;">共 ${seq.length} 次</span>
          <span style="font-size:10.5px;color:var(--mut);">（旧 → 新，取值可点下钻）</span></div>
        <table class="t" style="width:100%;font-size:11.5px;">
          <tr><th style="padding:4px 8px;">指标</th><th style="padding:4px 8px;">取值序列</th>
            <th style="padding:4px 8px;">可信度</th><th style="padding:4px 8px;">趋势</th></tr>
          ${rows || '<tr><td colspan="4" style="color:var(--mut);font-size:11px;">该类型无可展示指标</td></tr>'}
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

  /* ══ 下钻：证据面板（2026-10-22 晚新增）══════════════════════════
   *
   * 行业标杆（LangSmith/Langfuse/Braintrust）的评测页都能从"总览"
   * 点进"是哪几条案例"。本页的证据一直存在 detail JSON 里（wrong
   * 案例、混淆矩阵、负对照、judge 明细、取材记账），此前只缺通路。
   *
   * 三条纪律：
   *   1. **只渲染不计算** —— 状态/阈值全部来自后端（eval_gates 唯一
   *      真源），前端不重写判定口径。
   *   2. **缺失即「—」** —— detail 里没有的字段渲染成「—」，不渲染
   *      undefined/[object Object]（门禁 C4 专门守这条）。
   *   3. **聚合层只读、证据层下钻** —— 不在本页发起任何"改参数/重跑"
   *      动作：动作属于产品页，评测页是观察台。
   * ================================================================= */
  function _sparkline(vals) {
    const nums = (vals || []).filter(v => typeof v === 'number' && isFinite(v));
    if (nums.length < 2) {
      return '<span style="color:var(--mut);font-size:10px;">—</span>';
    }
    const W = 110, H = 16, P = 2;
    const min = Math.min.apply(null, nums);
    const max = Math.max.apply(null, nums);
    const span = (max - min) || 1;
    const segs = [];
    let cur = [];
    (vals || []).forEach((v, i) => {
      if (typeof v === 'number' && isFinite(v)) {
        const x = P + (i * (W - 2 * P)) / Math.max(1, (vals || []).length - 1);
        const y = H - P - ((v - min) / span) * (H - 2 * P);
        cur.push(x.toFixed(1) + ',' + y.toFixed(1));
      } else if (cur.length) {
        segs.push(cur.join(' '));
        cur = [];
      }
    });
    if (cur.length) segs.push(cur.join(' '));
    const paths = segs.map(s =>
      '<polyline points="' + s + '" fill="none" stroke="var(--color-text)" stroke-width="1.2"/>'
    ).join('');
    return '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '">' + paths + '</svg>';
  }

  function _dval(v) {
    if (v === null || v === undefined || v === '') {
      return '<span style="color:var(--mut);">—</span>';
    }
    if (typeof v === 'number') return '<b>' + v.toFixed(3).replace(/0+$/, '').replace(/\.$/, '') + '</b>';
    return esc(String(v));
  }

  function _dtable(head, rows) {
    if (!rows || !rows.length) {
      return '<div style="color:var(--mut);font-size:11.5px;">无数据</div>';
    }
    const th = head.map(h => '<th style="padding:4px 8px;white-space:nowrap;">' + esc(h) + '</th>').join('');
    const tr = rows.map(r => '<tr>' + r.map(c =>
      '<td style="padding:4px 8px;border-bottom:1px solid var(--line);font-size:11.5px;vertical-align:top;">' + c + '</td>'
    ).join('') + '</tr>').join('');
    return '<table class="t" style="width:100%;"><tr>' + th + '</tr>' + tr + '</table>';
  }

  function _dsection(title, body, note) {
    return '<div class="panel" style="margin-bottom:12px;">'
      + '<div class="ph">' + esc(title)
      + (note ? ' <span class="tag">' + esc(note) + '</span>' : '') + '</div>'
      + '<div class="pb">' + body + '</div></div>';
  }

  function _dtrace(d) {
    const p = d.population || {}, pos = d.positive || {}, neg = d.negative || {};
    const ctl = d.negative_control || {}, wr = d.weak_regression || {};
    const pop = [
      ['trace_total', p.trace_total], ['strong', p.strong],
      ['positive', p.positive], ['negative(纠错/池有效性)',
        (p.negative_routing_error || 0) + ' / ' + (p.negative_pool_invalid || 0)],
      ['weak 抽样', p.weak_sampled], ['gold 失效', p.gold_stale],
      ['路由异常', p.route_errors], ['LLM 口径', d.llm_fallback],
      ['语义后端', d.semantic_backend],
    ];
    let h = _dsection('取材记账（排除了什么、为什么）',
      _dtable(['项', '值'], pop.map(r => [esc(r[0]), _dval(r[1])])), 'visible');
    h += _dsection('裁决一致率',
      _dtable(['项', '值'], [
        ['一致率 agreement', _dval(pos.agreement)],
        ['macro-F1', _dval(pos.macro_f1)],
        ['样本数 n', _dval(pos.n)],
        ['负对照（gold 洗牌）', _dval(ctl.recall)],
        ['差距', _dval(ctl.gap)],
        ['可判别', ctl.discriminates === true ? '是' : (ctl.discriminates === false ? '否' : '—')],
        ['gold 意图类别数', _dval(ctl.distinct_gold)],
      ]) + (ctl.note ? '<div style="font-size:11px;color:var(--amb);margin-top:6px;">' + esc(ctl.note) + '</div>' : ''));
    const pc = pos.per_class || [];
    if (pc.length) {
      h += _dsection('逐意图表现（precision / recall / F1）',
        _dtable(['意图', 'P', 'R', 'F1', 'n'], pc.map(x => [
          esc(x.intent), _dval(x.precision), _dval(x.recall), _dval(x.f1), _dval(x.n)
        ])));
    }
    const wrong = pos.wrong || [];
    h += _dsection('不一致样本（当前路由 ≠ 人工裁决）',
      _dtable(['问句', '人工裁决', '当前路由', '层', '置信'], wrong.slice(0, 20).map(w => [
        esc(String(w.query || '').slice(0, 60)),
        esc(w.gold || '—'), esc(w.pred || '—'),
        esc(w.route || '—'), _dval(w.conf)
      ])), wrong.length + ' 条');
    const rep = neg.repeats || [];
    h += _dsection('仍犯旧错（人工纠正过的路由又错了）',
      _dtable(['问句', '仍判成', '层'], rep.slice(0, 20).map(r => [
        esc(String(r.query || '').slice(0, 60)), esc(r.gold || '—'), esc(r.route || '—')
      ])), (neg.n || 0) + ' 条负样本');
    const wd = d.weak || {};
    const dist = wd.intent_dist || {};
    const distRows = Object.keys(dist).map(k => [esc(k), _dval(dist[k])]);
    h += _dsection('weak 回归集路由分布（只观测，不判定）',
      _dtable(['意图', '条数'], distRows)
      + (wr && wr.n_compared ? '<div style="font-size:11px;color:var(--mut);margin-top:6px;">'
        + '跑批间稳定率 ' + _dval(wr.stability) + '（交集 ' + _dval(wr.n_compared) + ' 条）'
        + (wr.note ? '　' + esc(wr.note) : '') + '</div>' : ''));
    return h;
  }

  function _dgen(d) {
    const s = d.summary || {}, p = d.population || {};
    const pop = Object.keys(p || {}).map(k => [esc(k), _dval(p[k])]);
    let h = _dsection('取材记账（排除了什么、为什么）',
      _dtable(['项', '值'], pop), 'visible');
    h += _dsection('确定性指标',
      _dtable(['指标', '值'], [
        ['引用编号有效率', _dval(s.citation_valid_rate)],
        ['引用使用率', _dval(s.citation_usage_rate)],
        ['无据声称密度', _dval(s.no_citation_factual_rate)],
        ['实体接地率', _dval(s.grounded_entity_rate)],
        ['样本数 n', _dval(s.n_answers)],
        ['负对照（池化）', _dval((s.control || {}).pooled_grounded_rate)],
        ['负对照可判别', (s.control || {}).discriminates ? '是' : '否'],
      ]));
    const ug = d.ungrounded_examples || [];
    h += _dsection('未接地实体样例（确定性信号，供人工复核）',
      _dtable(['消息', '实体', '类型'], ug.slice(0, 15).map(u => [
        esc(String(u.message_id)), esc((u.entity || '')), esc(u.kind || '')
      ])), ug.length + ' 条');
    const j = d.llm_judge || {};
    if (j.enabled) {
      const c = j.control || {};
      h += _dsection('LLM 忠实度评判（语义层）',
        _dtable(['项', '值'], [
          ['忠实率 faithful_rate', _dval(j.faithful_rate)],
          ['faithful / unfaithful / 判不了', _dval(j.n_faithful) + ' / ' + _dval(j.n_unfaithful) + ' / ' + _dval(j.n_failed)],
          ['评判成功条数', _dval(j.n_judged)],
          ['judge 自证', c.ok === true ? '通过' : (c.ran ? '失败：' + (c.note || '') : '未跑：' + (c.note || ''))],
          ['模型', _dval(j.model)],
        ]));
      const ue = j.unfaithful_examples || [];
      h += _dsection('无据声称明细（judge 判定，需人工复核）',
        _dtable(['消息', '无据声称', '理由'], ue.slice(0, 15).map(e => [
          esc(String(e.id)),
          esc(((e.unsupported || [])[0] || '').slice(0, 80)),
          esc(String(e.reason || '').slice(0, 80))
        ])), ue.length + ' 条');
    }
    return h;
  }

  function _drag(d) {
    const s = d.summary || {};
    const doc = s.doc || {}, graph = s.graph || {}, neg = s.neg || {};
    const ctl = s.doc_control || {};
    return _dsection('检索三域指标',
      _dtable(['域', '指标', '值'], [
        ['doc', 'n', _dval(doc.n)], ['doc', 'recall@5', _dval(doc['recall@5'])],
        ['doc', 'mrr@10', _dval(doc['mrr@10'])], ['doc', 'ndcg@10', _dval(doc['ndcg@10'])],
        ['doc', 'pool_recall@20', _dval(doc['pool_recall@20'])],
        ['doc', '重排丢对率', _dval(doc.rerank_drop_rate)],
        ['graph', 'n', _dval(graph.n)], ['graph', 'route_acc', _dval(graph.route_acc)],
        ['graph', 'entity_recall', _dval(graph.entity_recall)],
        ['neg', 'n', _dval(neg.n)], ['neg', 'route_ok', _dval(neg.route_ok)],
        ['neg', 'empty_hit_rate', _dval(neg.empty_hit_rate)],
        ['负对照', 'recall@5', _dval(ctl['recall@5'])],
        ['负对照', 'mrr@10', _dval(ctl['mrr@10'])],
      ]));
  }

  function _dgeneric(d) {
    const keys = Object.keys(d || {}).filter(k => k !== 'per_answer' && k !== 'decisions');
    const rows = keys.map(k => {
      const v = d[k];
      if (v && typeof v === 'object') {
        const n = Array.isArray(v) ? v.length : Object.keys(v).length;
        return [esc(k), '<span style="color:var(--mut);">对象（' + n + ' 项，见类型专用面板或原始 JSON）</span>'];
      }
      return [esc(k), _dval(v)];
    });
    return _dsection('报告字段（原始明细）', _dtable(['字段', '值'], rows));
  }

  function _renderReportDetail(r) {
    const d = r.detail || {};
    const head = '<h3 style="margin:0 0 2px;">' + esc(r.kind_label || r.eval_kind || '评测报告') + '</h3>'
      + '<div class="mut" style="font-size:11.5px;margin-bottom:12px;">'
      + esc(r.doc_name || '') + '　·　' + esc(String(r.created_at || ''))
      + '　·　报告 id=' + esc(String(r.id || '')) + '</div>';
    let body;
    if (r.eval_kind === 'trace_consistency') body = _dtrace(d);
    else if (r.eval_kind === 'generation_quality') body = _dgen(d);
    else if (r.eval_kind === 'rag_retrieval') body = _drag(d);
    else body = _dgeneric(d);
    return head + body;
  }

  async function openEvalReport(id) {
    const m = document.getElementById('modal');
    const b = document.getElementById('modal-body');
    if (!m || !b) return;
    b.innerHTML = '<div class="loading">加载评测证据明细…</div>';
    m.classList.add('show');
    m.onclick = e => { if (e.target === m) closeModal(); };
    let r;
    try {
      r = await api('/api/eval/report?id=' + encodeURIComponent(id));
    } catch (e) {
      b.innerHTML = '<div class="panel"><div class="pb"><div style="color:var(--red);font-size:12px;">'
        + esc('加载失败：' + (e && e.message || e)) + '</div></div></div>';
      return;
    }
    if (!r || r.ok === false) {
      b.innerHTML = '<div class="panel"><div class="pb"><div style="color:var(--red);font-size:12px;">'
        + esc((r && r.error) || '报告不可用') + '</div></div></div>';
      return;
    }
    b.innerHTML = _renderReportDetail(r);
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
  window.openEvalReport = openEvalReport;
})();
