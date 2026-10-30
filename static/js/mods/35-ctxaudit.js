/* 35-ctxaudit.js — 上下文构成观测面板（2026-10-27）
 *
 * 目的：把 `context_audit` 的分块归因**从库里搬到界面上**。
 * 在此之前，归因数据虽然每轮都在落库，但只有命令行报表能看（tools/report_context_audit.py），
 * 业务方看不见 ⇒ 观测能力只建了一半，"开关该开还是关"仍然靠猜。
 *
 * 数据来源（全部只读，不写库）：
 *   GET /api/diagnostics/context-audit/overview?days=N   —— 一次拿全（本模块只用这一个）
 *   GET /api/diagnostics/context-audit/blocks            —— 块级分布（overview 里已含）
 *   GET /api/diagnostics/context-audit/switches          —— 优化项实际运行情况
 *   GET /api/diagnostics/context-audit/daily             —— 按日趋势
 *   GET /api/diagnostics/context-audit/conversations     —— 最贵会话
 *
 * 三条设计纪律：
 *   ① **快照型指标取均值、不求和**。后端 `switches()` 已按 `kind` 分好并下发 `value`，
 *      前端**不自己判断** —— 判断逻辑只存一份，避免前后端各写一遍必然漂移。
 *      （背景：mcp_selected=8 是"每轮 8 个"，累加 4 轮成 32 会被读成"暴露了 32 个工具"。）
 *   ② **必须有空态**。生产库 context_audit 在新代码跑过之前是 0 行，
 *      空白页会让人分不清"没数据"和"功能坏了" ⇒ 空态给出可操作的原因清单。
 *   ③ **静态/动态分色**。`skill_prompt` 是第一块"每轮变"的，它后面的全部不命中
 *      prompt cache ⇒ 用颜色让"断点在哪"一眼可见，比一张饼图有用。
 */

var _CA_DAYS = 7;
var _CA_BUSY = false;

/* METRIC_LABELS 的兜底（后端已下发 label，这里只防旧数据缺字段） */
var _CA_NOUN = {
  mcp_selected: 'MCP 选中', mcp_injected: 'MCP 注入', mcp_total: 'MCP 总数',
  tool_payload_total: '工具载荷', retrieval_effective: '检索有效条数',
};

function caNum(n) {
  if (n == null || n === '') return '—';
  const v = Number(n);
  if (!isFinite(v)) return '—';
  if (Math.abs(v) >= 100) return Math.round(v).toLocaleString();
  if (Math.abs(v) >= 10) return (Math.round(v * 10) / 10).toString();
  return (Math.round(v * 100) / 100).toString();
}

function caKpi(label, value, sub, color) {
  return `<div style="background:var(--bg2,#f7f9fc);border-radius:8px;padding:12px;min-width:0;">
    <div style="font-size:11.5px;color:var(--mut);margin-bottom:6px;">${esc(label)}</div>
    <div style="font-size:22px;font-weight:600;line-height:1.15;${color ? 'color:' + color + ';' : ''}
                overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(value)}</div>
    <div style="font-size:10.5px;color:var(--mut);margin-top:4px;">${esc(sub || '')}</div>
  </div>`;
}

/* 块级分布：横向条形，按 token 降序。静态=灰、动态=橙（橙色即"每轮变"） */
function caBlocksHtml(b) {
  const rows = (b.rows || []);
  if (!rows.length) return '';
  const max = Math.max.apply(null, rows.map(r => +r.tokens || 0)) || 1;
  const body = rows.map(r => {
    const tok = +r.tokens || 0;
    const w = Math.max(Math.min(tok / max, 1), 0.02) * 100;
    const dyn = r.layer === 'dynamic';
    const col = dyn ? 'var(--amb,#EF9F27)' : 'var(--line-strong,#B4B2A9)';
    return `<div style="display:grid;grid-template-columns:104px 1fr 62px 46px;gap:8px;
                align-items:center;font-size:11.5px;margin-bottom:7px;">
      <div style="color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
           title="${escA(r.label || r.block)}">${esc(r.block)}</div>
      <div style="height:9px;border-radius:4px;background:${col};width:${w}%;min-width:3px;"></div>
      <div style="text-align:right;font-weight:600;">${caNum(tok)}</div>
      <div style="text-align:right;color:var(--mut);font-size:10.5px;">${caNum(r.token_share)}%</div>
    </div>`;
  }).join('');
  const L = b.layers || {};
  const st = +L.static || 0, dy = +L.dynamic || 0, tt = st + dy || 1;
  // ⚠️ 装配次数必须从 `b.assemblies` 取 —— 不能用调用方的局部变量（作用域错误会让整块渲染中断）。
  //   `b.total_hits` 是块行数之和，不是装配次数。
  const asm = +b.assemblies || 0;
  return `<div class="panel" style="margin-bottom:12px;">
    <div class="ph">📊 块级 token 分布
      <span class="badge">按占比降序</span>
      <span style="flex:1"></span>
      <span style="font-size:10.5px;font-weight:400;color:var(--mut);">
        灰=静态可缓存 · 橙=动态每轮变</span>
    </div>
    <div class="pb">
      ${body}
      <div style="margin-top:10px;padding-top:9px;border-top:1px solid var(--line-soft,#e9edf3);
                  display:flex;gap:18px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);">
        <span>静态合计 <b style="color:var(--txt);">${caNum(st)}</b>
          <span style="font-size:10.5px;">(${(st / tt * 100).toFixed(1)}%)</span></span>
        <span>动态合计 <b style="color:var(--amb,#BA7517);">${caNum(dy)}</b>
          <span style="font-size:10.5px;">(${(dy / tt * 100).toFixed(1)}%)</span></span>
        <span>合计 <b style="color:var(--txt);">${caNum(tt)}</b> tok</span>
        <span style="flex:1"></span>
        <span>${b.total_hits || 0} 块行 / ${asm || '—'} 次装配 / ${b.window_days} 天</span>
      </div>
    </div>
  </div>`;
}

/* 优化项实际运行情况：计数型 / 快照型分两段（语义不同，混在一起必然误读） */
function caSwitchChip(m) {
  const v = +m.value || 0;
  if (m.kind === 'count') {
    return v > 0
      ? '<span style="font-size:10.5px;padding:1px 6px;border-radius:4px;background:var(--grn-l);color:var(--grn-d);">生效</span>'
      : '<span style="font-size:10.5px;padding:1px 6px;border-radius:4px;background:var(--bg2,#f1efE8);color:var(--mut);">未触发</span>';
  }
  return '<span style="font-size:10.5px;padding:1px 6px;border-radius:4px;background:var(--amb-l);color:var(--amb);">每轮均值</span>';
}

function caSwitchesHtml(s) {
  const ms = (s.metrics || []);
  if (!ms.length) return '';
  const cnt = ms.filter(m => m.kind === 'count');
  const gau = ms.filter(m => m.kind !== 'count');
  const head = '<div style="display:grid;grid-template-columns:1.5fr 84px 96px 60px;gap:8px;' +
    'padding:5px 0;color:var(--mut);border-bottom:1px solid var(--line-soft,#e9edf3);font-size:11px;">' +
    '<span>指标</span><span>状态</span><span style="text-align:right;">数值</span>' +
    '<span style="text-align:right;">样本</span></div>';
  const row = m => {
    const u = m.unit || '';
    return `<div style="display:grid;grid-template-columns:1.5fr 84px 96px 60px;gap:8px;
                padding:6px 0;border-bottom:1px solid var(--line-soft,#e9edf3);align-items:center;">
      <span title="${escA(m.metric)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
        ${esc(m.label || m.metric)}</span>
      <span>${caSwitchChip(m)}</span>
      <span style="text-align:right;font-weight:600;">${caNum(m.value)}${esc(u)}</span>
      <span style="text-align:right;color:var(--mut);font-size:10.5px;">${m.samples || 0}</span>
    </div>`;
  };
  let html = `<div class="panel" style="margin-bottom:12px;">
    <div class="ph">🔧 优化项实际运行情况
      <span style="font-size:10.5px;font-weight:400;color:var(--mut);">
        回答「改了但生效了吗」· 读运行时指标，不是读配置</span>
    </div>
    <div class="pb" style="font-size:11.5px;">`;
  if (cnt.length) {
    html += `<div style="font-size:10.5px;color:var(--mut);margin-bottom:4px;">
      计数型：值为窗口内<b>发生次数</b>（累加有意义）</div>` + head + cnt.map(row).join('');
  }
  if (gau.length) {
    html += `<div style="font-size:10.5px;color:var(--mut);margin:12px 0 4px;">
      快照型：值为<b>单轮</b>用量，显示<b>均值</b>（累加会翻倍失真，故不展示总和）</div>` +
      head + gau.map(row).join('');
  }
  html += `<div style="margin-top:9px;font-size:10.5px;color:var(--mut);line-height:1.6;">
      读法：计数型 0 = 该能力本窗口<b>没被触发</b>（配置未开 / 前置不满足如 MCP offline / 场景没走到）；
      快照型的均值才是「每轮典型用量」，跟配置阈值比才有意义。</div>
    </div></div>`;
  return html;
}

/* 按日趋势：每天一根柱（高度按当日 token 归一），下方标静态占比 */
function caDailyHtml(d) {
  const rows = (d.rows || []);
  if (!rows.length) return '';
  const max = Math.max.apply(null, rows.map(r => +r.tokens || 0)) || 1;
  const bars = rows.slice(-21).map(r => {
    const h = Math.max((+r.tokens || 0) / max * 100, 2);
    const ratio = +r.static_ratio || 0;
    const col = ratio >= 70 ? 'var(--blu,#185FA5)' : (ratio >= 40 ? 'var(--amb,#EF9F27)' : 'var(--red,#A32D2D)');
    return `<div title="${escA(r.date)}：${caNum(r.tokens)} tok · 静态 ${ratio}% · ${r.traces} 轮"
      style="flex:1;min-width:6px;display:flex;flex-direction:column;justify-content:flex-end;height:54px;">
      <div style="height:${h}%;background:${col};border-radius:3px 3px 0 0;"></div></div>`;
  }).join('');
  return `<div class="panel" style="margin-bottom:12px;">
    <div class="ph">📈 按日趋势
      <span style="font-size:10.5px;font-weight:400;color:var(--mut);">
        柱高=当日 system 块总 token · 颜色=静态占比（蓝≥70% 优 / 橙 40~70% / 红&lt;40%）</span>
    </div>
    <div class="pb">
      <div style="display:flex;gap:3px;align-items:flex-end;">${bars}</div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--mut);margin-top:5px;">
        <span>${esc(rows[0].date)}</span><span>${esc(rows[rows.length - 1].date)}</span>
      </div>
    </div>
  </div>`;
}

function caTopConvHtml(list) {
  if (!list || !list.length) return '';
  const rows = list.map(c => `<div style="display:grid;grid-template-columns:1fr 70px 80px;gap:8px;
      padding:5px 0;border-bottom:1px solid var(--line-soft,#e9edf3);font-size:11.5px;">
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
        title="${escA(c.title || c.conversation_id)}">${esc(c.title || ('会话 ' + c.conversation_id))}</span>
      <span style="text-align:right;color:var(--mut);">${c.traces || 0} 轮</span>
      <span style="text-align:right;font-weight:600;">${caNum(c.tokens)}</span>
    </div>`).join('');
  return `<div class="panel">
    <div class="ph">💰 最贵会话 TOP ${list.length}
      <span style="font-size:10.5px;font-weight:400;color:var(--mut);">按 system 块 token 合计</span>
    </div>
    <div class="pb">
      <div style="display:grid;grid-template-columns:1fr 70px 80px;gap:8px;padding:4px 0;
                  color:var(--mut);font-size:10.5px;border-bottom:1px solid var(--line-soft,#e9edf3);">
        <span>会话</span><span style="text-align:right;">轮次</span>
        <span style="text-align:right;">token</span></div>
      ${rows}
    </div>
  </div>`;
}

/* 空态：必须能区分「没数据」与「功能坏了」，否则第一次打开就是一片空白 */
function caEmptyHtml(ov) {
  const hint = ov && ov.hint ? ov.hint : '';
  return `<div class="panel">
    <div class="ph">📭 暂无归因数据</div>
    <div class="pb" style="font-size:12px;line-height:1.8;">
      <div style="color:var(--mut);">最近 <b>${_CA_DAYS}</b> 天没有真实的上下文装配落库。</div>
      <div style="margin-top:10px;padding:9px 11px;border-radius:6px;
                  background:var(--amb-l);color:var(--amb);font-size:11.5px;">
        这不是错误，通常是以下三种情况之一：
      </div>
      <ol style="margin:8px 0 0 18px;padding:0;color:var(--txt);font-size:11.5px;line-height:1.9;">
        <li><b>部署后还没有新对话</b> —— 归因在每次真实请求装配时写入，发一条对话后点「刷新」即可。</li>
        <li><b>归因开关被关</b> —— 检查 <code>context_audit.enabled</code>（默认开）。</li>
        <li><b>表未迁移</b> —— 建表在 <code>init_db()</code>（仅应用启动时执行），
            未迁移会得到「表不存在」而不是空数据。</li>
      </ol>
      ${hint ? `<div style="margin-top:10px;font-size:10.5px;color:var(--mut);">诊断提示：${esc(hint)}</div>` : ''}
    </div>
  </div>`;
}

async function loadCtxAudit() {
  const box = document.getElementById('ctx-audit-body');
  if (!box) return;
  if (_CA_BUSY) return;
  _CA_BUSY = true;
  const sel = document.getElementById('ctx-audit-days');
  if (sel) _CA_DAYS = +(sel.value || 7);
  box.innerHTML = '<div class="loading">加载归因数据…</div>';
  let ov;
  try {
    ov = await api('/api/diagnostics/context-audit/overview?days=' + _CA_DAYS);
  } catch (e) {
    box.innerHTML = `<div class="panel"><div class="ph">⚠️ 加载失败</div>
      <div class="pb" style="font-size:12px;color:var(--mut);">
        ${esc(e && e.message || e)}<br><br>
        常见原因：后端未启动、或该接口需要更高权限。</div></div>`;
    _CA_BUSY = false;
    return;
  }
  _CA_BUSY = false;

  // 后端就绪自检失败（表不存在）⇒ 直接给出可操作提示，不渲染半套内容
  const blk = ov.blocks || {};
  if (blk.ready === false) {
    box.innerHTML = `<div class="panel"><div class="ph">⚠️ 归因表不可用</div>
      <div class="pb" style="font-size:12px;line-height:1.8;">
        <div style="color:var(--mut);">${esc(blk.hint || '')}</div>
        <div style="margin-top:8px;font-size:11.5px;">
          建表在 <code>init_db()</code>，只在应用启动时执行。启动一次服务即可自动补建。</div>
      </div></div>`;
    return;
  }
  if (!ov.has_data) {
    box.innerHTML = caEmptyHtml(ov);
    return;
  }

  const st = +(blk.layers && blk.layers.static) || 0;
  const dy = +(blk.layers && blk.layers.dynamic) || 0;
  const tt = st + dy || 1;
  /* ⚠️ 必须用 `assemblies`（真实装配次数）当分母，不能用 `total_hits`。
     `total_hits` 是**块行数之和**（每轮每块记一行），实测 106 行只对应 8 轮
     ⇒ 拿它当分母会把「每轮 6,559 tok」算成 495 tok，首屏 KPI 直接失真 13 倍。 */
  const hits = +blk.assemblies || 0;
  const avgSys = hits ? Math.round((+(blk.total_tokens) || 0) / hits) : 0;

  /* 缓存断点 = prompt **拼接顺序**上第一块"每轮变"的块，由后端按
     PROMPT_DYNAMIC_KEYS 顺序算出（`cache_boundary`）。
     ⚠️ 不能用 `blk.rows` 里的第一个 dynamic —— rows 是按 token 降序排的，
        那只是"最大的动态块"，不是顺序最靠前的（实测两者不同：断点是 model_code_req，
        而最大动态块可能是别的）。 */
  const firstDyn = blk.cache_boundary
    ? { block: blk.cache_boundary, tokens: blk.cache_boundary_tokens } : null;

  const kpis = `<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
      gap:12px;margin-bottom:12px;">
    ${caKpi('system 均值', caNum(avgSys), 'tok / 轮 · ' + caNum(blk.total_tokens) + ' 合计')}
    ${caKpi('静态可缓存', (st / tt * 100).toFixed(1) + '%', caNum(st) + ' tok', 'var(--blu,#185FA5)')}
    ${caKpi('缓存断点', firstDyn ? firstDyn.block : '—',
      firstDyn ? '首个易变块 · ' + caNum(firstDyn.tokens) + ' tok' : '全部静态', 'var(--amb,#BA7517)')}
    ${caKpi('采样轮次', caNum(hits), _CA_DAYS + ' 天 · ' + (ov.days_with_data || 0) + ' 天有数据')}
  </div>`;

  box.innerHTML = kpis
    + caBlocksHtml(blk)
    + caSwitchesHtml(ov.switches || {})
    + caDailyHtml(ov.daily || {})
    + caTopConvHtml(ov.top_conversations || []);
}

function ctxAuditDays(el) {
  _CA_DAYS = +(el.value || 7);
  loadCtxAudit();
}
