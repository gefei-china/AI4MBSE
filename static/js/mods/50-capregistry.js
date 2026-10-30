/* 50-capregistry.js — 能力账本（2026-10-27，P0-1/P0-2/P0-3 的界面出口）
 *
 * 目的：把 `capability_registry` 统一注册处**从仓库搬到柜子里**。
 *   在此之前它 205 行、度量口径经对拍完全准确（graph_retrieve 442=442 …），
 *   但 `routers/` 引用 0 处 ⇒ 只有告警循环与周报在读，界面看不到。
 *
 * 本面板要回答三个**治理问题**（不是技术问题）：
 *   ① 这个能力到底有没有在用？（统一注册处的 call_count + 最近使用时间）
 *   ② 界面说它"未绑定"，是不是等于"没人用"？→ **不是**。所以给 2×2 矩阵：
 *        声明集（4 条入口）与调用集分开算，两者不一致处照实标注。
 *   ③ 出了问题能不能查清"哪一轮调了什么、花了多少"？→ 追溯填充率 + trace 下钻。
 *
 * 四条设计纪律：
 *   ① **判定由后端给**（`verdict` / `entry_labels`），前端不自己推 —— 两处各写一遍必然漂移。
 *   ② **必须有空态**：新库 / 未跑过真实链路时，页面要能说清"为什么空、怎么才有数据"，
 *      否则空白页会被读成"功能坏了"。
 *   ③ **颜色只表达判定，不表达好坏**：`runtime_only`（有调用无声明）是**需要公示的事实**，
 *      用琥珀色而不是红色 —— 它不一定是缺陷（运行时注入的 ask_user 就属于这一类）。
 *   ④ **不展示 arguments/result**：本面板是治理视图，不是内容审计；
 *      正文里有用户数据，端点也没下发（见 routers/capabilities.py 的权限说明）。
 */

var CAPR_STATE = { days: 30, kind: '', verdict: '', q: '', ledger: null, trace: null };

var CAPR_VERDICT = {
  in_use: { label: '在用', color: '#2e7d32', bg: '#e8f5e9' },
  declared_unused: { label: '已声明未用', color: '#8a6d3b', bg: '#fdf6e3' },
  runtime_only: { label: '无声明入口', color: '#b26a00', bg: '#fff4e5' },
  unused: { label: '未使用', color: '#6b7280', bg: '#f1f2f4' },
  // ★ 2026-10-29：插件（kind=plugin）里 prompt 类**没有用量事实源**。
  //   此前它们与"真没人用"同显「未使用」⇒ 用户会把还在用的 prompt 当垃圾清掉。
  //   现在单列一档，标签即结论：不知道就是不知道。
  unknown: { label: '无可判据', color: '#5b6b7c', bg: '#eef1f5' },
};

function caprEsc(v) {
  if (typeof esc === 'function') return esc(v == null ? '' : String(v));
  return String(v == null ? '' : v).replace(/[&<>"']/g, s =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[s]));
}

function caprNum(n) {
  if (n == null || n === '') return '—';
  const v = Number(n);
  if (!isFinite(v)) return '—';
  return Math.abs(v) >= 1000 ? v.toLocaleString() : String(v);
}

function caprKpi(label, value, sub, tone) {
  const c = tone || '#1f2937';
  return `<div style="background:var(--bg2,#f7f9fc);border-radius:8px;padding:12px;min-width:0;">
    <div style="font-size:11.5px;color:var(--mut);margin-bottom:6px;">${caprEsc(label)}</div>
    <div style="font-size:22px;font-weight:600;line-height:1.15;color:${c};
                overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${caprEsc(value)}</div>
    <div style="font-size:10.5px;color:var(--mut);margin-top:4px;">${caprEsc(sub || '')}</div>
  </div>`;
}

function caprChip(text, tone) {
  const t = tone || { color: '#374151', bg: '#eef1f5' };
  return `<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-size:10.5px;
    line-height:1.6;color:${t.color};background:${t.bg};margin:1px 3px 1px 0;white-space:nowrap;"
    >${caprEsc(text)}</span>`;
}

/* 2×2 矩阵：声明集 × 调用集 —— 本面板的核心，也是"让视图说实话"的全部内容 */
function caprMatrixHtml(m) {
  m = m || {};
  const cell = (title, n, tone, desc) => `
    <div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0;">
      <div style="font-size:11px;color:var(--mut);">${caprEsc(title)}</div>
      <div style="font-size:19px;font-weight:600;color:${tone};margin:2px 0 3px;">${caprNum(n)}</div>
      <div style="font-size:10.5px;color:var(--mut);line-height:1.5;">${caprEsc(desc)}</div>
    </div>`;
  const _unk = Number(m.unknown || 0);
  return `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;">
    ${cell('有声明入口 · 有调用', m.in_use, '#2e7d32', '正常在用')}
    ${cell('有声明入口 · 无调用', m.declared_unused, '#8a6d3b', '冗余绑定，可清理')}
    ${cell('无声明入口 · 有调用', m.runtime_only, '#b26a00', '运行时旁路，需公示')}
    ${cell('无声明入口 · 无调用', m.unused, '#6b7280', _unk
        ? ('未使用；另有 ' + _unk + ' 项无可判据')
        : '未使用')}
  </div>`;
}

function caprRowHtml(r) {
  const v = CAPR_VERDICT[r.verdict] || CAPR_VERDICT.unused;
  const entries = (r.entry_labels || []);
  const entHtml = entries.length
    ? entries.map(x => caprChip(x, { color: '#1c4fc4', bg: '#eef4ff' })).join('')
    : caprChip('无声明入口', { color: '#b26a00', bg: '#fff4e5' });
  const bound = r.bound_count > 0
    ? `<span title="${caprEsc((r.bound_agents || []).join(' / '))}">${r.bound_count} 个</span>`
    : '<span style="color:var(--mut);">0</span>';
  const fail = r.fail_count > 0
    ? `<span style="color:#c62828;">${caprNum(r.fail_count)}</span>`
    : '<span style="color:var(--mut);">—</span>';
  return `<tr>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);">
      <div style="font-weight:500;">${caprEsc(r.display_name || r.capability_id)}</div>
      <div style="font-size:10.5px;color:var(--mut);font-family:ui-monospace,Consolas,monospace;">
        ${caprEsc(r.capability_id)}</div>
    </td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);font-size:11.5px;color:var(--mut);">
      ${caprEsc(r.kind)}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);">${entHtml}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;font-size:11.5px;">${bound}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;font-weight:500;">
      ${caprNum(r.call_count)}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;font-size:11.5px;">${fail}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px;color:var(--mut);">
      ${caprEsc((r.last_used_at || '').slice(0, 16) || '—')}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);">${caprChip(v.label, v)}</td>
  </tr>`;
}

function caprOrphanHtml(list) {
  if (!list || !list.length) {
    return `<div style="font-size:11.5px;color:var(--mut);padding:8px 0;">
      窗口内没有"被调用但不在注册表"的名字。</div>`;
  }
  const rows = list.slice(0, 12).map(x => `<tr>
    <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-family:ui-monospace,Consolas,monospace;
        font-size:11.5px;">${caprEsc(x.tool_name)}</td>
    <td style="padding:5px 8px;border-bottom:1px solid var(--line);">${
      (x.entry_labels || []).map(t => caprChip(t, t === '无声明入口（运行时旁路）'
        ? { color: '#b26a00', bg: '#fff4e5' } : { color: '#1c4fc4', bg: '#eef4ff' })).join('')}</td>
    <td style="padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;">${caprNum(x.call_count)}</td>
    <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:11px;color:var(--mut);">
      ${caprEsc((x.last_used_at || '').slice(0, 16) || '—')}</td>
  </tr>`).join('');
  return `<table style="width:100%;border-collapse:collapse;font-size:12px;">
    <thead><tr style="color:var(--mut);font-size:11px;text-align:left;">
      <th style="padding:4px 8px;">名字</th><th style="padding:4px 8px;">入口</th>
      <th style="padding:4px 8px;text-align:right;">调用</th><th style="padding:4px 8px;">最近</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function caprTraceHtml(t) {
  if (!t) return '';
  const tone = r => (r == null ? '#6b7280' : (r >= 80 ? '#2e7d32' : (r >= 30 ? '#b26a00' : '#c62828')));
  const cols = (t.fill || []).map(x => `
    <div style="min-width:0;">
      <div style="font-size:11px;color:var(--mut);">${caprEsc(x.column)}</div>
      <div style="font-size:15px;font-weight:600;color:${tone(x.rate)};">
        ${x.rate == null ? '—' : (x.rate + '%')}</div>
      <div style="font-size:10px;color:var(--mut);">${caprNum(x.filled)}/${caprNum(x.total)}</div>
    </div>`).join('');
  const joinOk = (t.joinable_traces || 0) > 0;
  return `<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:8px;">${cols}</div>
    <div style="font-size:11.5px;color:var(--mut);line-height:1.6;">
      近 ${caprEsc(t.window_days)} 天：工具调用侧 trace <b>${caprNum(t.tool_traces)}</b> 个 ·
      LLM 使用量侧 trace <b>${caprNum(t.llm_traces)}</b> 个 ·
      两侧都能找到的 <b style="color:${joinOk ? '#2e7d32' : '#b26a00'};">${caprNum(t.joinable_traces)}</b> 个
      ${joinOk ? '（可以回答"这一轮调了哪些能力、花了多少"）'
               : '（还没有可 JOIN 的轮次 —— 跑一轮真实对话后回来看）'}
      <br>${caprEsc(t.note || '')}
    </div>`;
}

function caprEmptyHtml() {
  return `<div style="padding:18px;background:var(--bg2,#f7f9fc);border-radius:8px;
      font-size:12px;color:var(--mut);line-height:1.7;">
    当前筛选下没有能力行。常见原因（按可能性排序）：<br>
    ① 统一注册处尚未建立 —— 服务启动时会 reconcile 一次，重启后应 ≥200 行；<br>
    ② 筛选条件太窄（试试点「全部」）；<br>
    ③ 统计窗口内没有真实调用 —— 能力存在但 call_count=0 属正常，切到「未使用」查看。
  </div>`;
}

async function loadCapRegistry() {
  const body = document.getElementById('capr-body');
  if (!body) return;
  body.innerHTML = '<div class="loading">加载能力账本…</div>';
  const st = CAPR_STATE;
  const qs = new URLSearchParams({ days: String(st.days) });
  if (st.kind) qs.set('kind', st.kind);
  if (st.verdict) qs.set('verdict', st.verdict);
  try {
    const [led, tr] = await Promise.all([
      fetch('/api/capabilities/ledger?' + qs.toString()).then(r => r.json()),
      fetch('/api/capabilities/trace?days=' + st.days).then(r => r.json()),
    ]);
    st.ledger = led; st.trace = tr;
    const rows = (led.rows || []).filter(r => {
      if (!st.q) return true;
      const q = st.q.toLowerCase();
      return (r.capability_id || '').toLowerCase().includes(q)
        || (r.display_name || '').toLowerCase().includes(q);
    });
    const m = led.matrix || {};
    body.innerHTML = `
      ${caprMatrixHtml(m)}
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;">
        <span style="font-size:11.5px;color:var(--mut);">
          窗口 ${caprEsc(led.window_days)} 天 · 注册 ${caprNum((led.rows || []).length)} 行 ·
          入口标注读后端 <code>entry_labels</code>（声明集共 4 条：Agent 绑定 / 全局技能池 /
          内置声明 / 技能白名单 + 无条件注入）
        </span>
        <span style="flex:1"></span>
        <button class="btn sm" id="capr-rediscover-btn" onclick="caprRediscoverMcp()"
          title="重新探测全部在线 MCP 服务器并回填工具元数据（验证 zhiyuan-dev-mcp 等是否可用）"
          style="border:1px solid var(--line);">🔄 重新发现 MCP 工具</button>
        <input id="capr-q" placeholder="搜能力 id / 名称" value="${caprEsc(st.q)}"
               oninput="caprSetQ(this.value)"
               style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:180px;">
      </div>
      <div id="capr-rediscover" style="margin-bottom:8px;"></div>
      <div style="max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead><tr style="position:sticky;top:0;background:var(--bg2,#f7f9fc);color:var(--mut);
              font-size:11px;text-align:left;z-index:1;">
            <th style="padding:6px 8px;">能力</th><th style="padding:6px 8px;">类型</th>
            <th style="padding:6px 8px;">声明入口</th>
            <th style="padding:6px 8px;text-align:right;">绑定 Agent</th>
            <th style="padding:6px 8px;text-align:right;">调用</th>
            <th style="padding:6px 8px;text-align:right;">失败</th>
            <th style="padding:6px 8px;">最近使用</th><th style="padding:6px 8px;">判定</th>
          </tr></thead>
          <tbody id="capr-ledger-tbody">${rows.length ? rows.map(caprRowHtml).join('') : ''}</tbody>
        </table>
      </div>
      <!-- 空态常驻 DOM 用 display 控制：caprSetQ 只重画 tbody，需能切换它 -->
      <div id="capr-empty" style="display:${rows.length ? 'none' : ''};">${rows.length ? '' : caprEmptyHtml()}</div>

      <div style="margin-top:16px;display:flex;gap:8px;align-items:center;">
        <b style="font-size:12.5px;">未进注册表但被调用的名字</b>
        <span style="font-size:11px;color:var(--mut);">
          （运行时注入的 ask_user 等 / MCP 工具 / 已删能力 —— <b>不是</b>可忽略的噪声，
          它们是"注册表不是消费唯一来源"的证据）</span>
      </div>
      <div style="margin-top:6px;">${caprOrphanHtml(led.orphan_calls)}</div>

      <div style="margin-top:18px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <b style="font-size:12.5px;">任务级追溯（P0-3）</b>
        <span style="font-size:11px;color:var(--mut);">
          trace_id 与 LLM 使用量同源 ⇒ 可回答"这一轮调了哪些能力、花了多少钱"</span>
        <span style="flex:1"></span>
        <input id="capr-trace" placeholder="粘贴或输入 trace_id 下钻"
               style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:220px;">
        <button class="btn sm" onclick="caprTraceDetail()">下钻</button>
      </div>
      <div style="margin-top:8px;">${caprTraceHtml(tr)}</div>
      <div id="capr-trace-detail"></div>

      <!-- 原始注册表视图：接 capabilities/registry（2026-10-29）。
           与账本报表的分工：账本报表是"加语义判定后的视图"，只回 top500 行；
           本端点=原始投影流水，支持 kind/status 搜索与分页。
           此前它后端一直存在、前端引用 0 处（孤儿端点）⇒ 想看第 501 行、
           或按名字定位一个具体能力，界面上做不到。这里把它接上。
           ⚠️ 本段是 JS 模板字符串内部的 HTML 注释 —— **不得出现反引号**：
              一旦出现会提前终止模板字符串，后面的裸词变成标识符，
              node --check 仍通过（语法凑巧成立），只有运行时才 ReferenceError。
              实测踩过：注释里给端点名加了反引号 ⇒ 页面报
              "加载失败：xxx is not defined"。 -->
      <details id="capr-raw" style="margin-top:18px;">
        <summary style="cursor:pointer;font-size:12.5px;font-weight:500;padding:4px 0;">
          原始注册表（capability_registry 投影行 · 支持搜索 / 类型 / 状态 / 分页）
          <span id="capr-raw-meta" style="font-weight:400;font-size:11px;color:var(--mut);"></span>
        </summary>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0;">
          <input id="capr-raw-q" placeholder="搜 capability_id / 名称"
                 style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:200px;">
          <select id="capr-raw-kind" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;">
            <option value="">全部类型</option>
            <option value="tool">工具 tool</option>
            <option value="skill">技能 skill</option>
            <option value="mcp_server">MCP 服务</option>
            <option value="mcp_tool">MCP 工具</option>
            <option value="plugin">插件 plugin</option>
            <option value="prompt">提示词 prompt</option>
          </select>
          <select id="capr-raw-status" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;">
            <option value="">全部状态</option>
            <option value="active">active</option>
            <option value="disabled">disabled</option>
          </select>
          <button class="btn sm" onclick="caprRawLoad(0)">查询</button>
        </div>
        <div id="capr-raw-body"></div>
      </details>

      <div style="margin-top:10px;font-size:11px;color:var(--mut);line-height:1.6;">
        ${caprEsc((led.hint || '') + '　' + (tr && tr.note ? '' : ''))}
      </div>`;
  } catch (e) {
    body.innerHTML = `<div style="color:#c62828;font-size:12px;">加载失败：${caprEsc(e && e.message)}</div>`;
  }
}

function caprSetQ(v) {
  CAPR_STATE.q = v || '';
  // ★ 2026-10-29 修：本函数注释原本就写着"只重画表格，不整页重载（输入过程中
  //   重建整页会让输入框失焦）"，但实现直接调了 `loadCapRegistry()` ——
  //   那会重建整个 `#capr-body`，把**输入框本体**替换掉 ⇒ 每按一个键就失焦一次，
  //   并且并发两次 fetch（先返回的那次后渲染，可能覆盖新结果）。
  //   改为：只重画 tbody + 切换空态。**不再发请求**（过滤在本地做）。
  const tb = document.getElementById('capr-ledger-tbody');
  const led = CAPR_STATE.ledger;
  if (!tb || !led) return;
  const q = (CAPR_STATE.q || '').toLowerCase();
  const rows = (led.rows || []).filter(r => !q
    || (r.capability_id || '').toLowerCase().includes(q)
    || (r.display_name || '').toLowerCase().includes(q));
  tb.innerHTML = rows.map(caprRowHtml).join('');
  const empt = document.getElementById('capr-empty');
  if (empt) {
    empt.style.display = rows.length ? 'none' : '';
    empt.innerHTML = rows.length ? '' : caprEmptyHtml();
  }
}

/* ── 原始注册表视图（/api/capabilities/registry）────────────────────────
   与 /ledger 的分工（后端注释原文）：本端点=「账本的原始流水」，
   /ledger=「加上语义判定后的报表」。前者支持 kind/status 搜索与分页，
   后者只回 top500 判定行。此前本页只用了后者 ⇒ 想看第 501 行、
   或按名字定位一个具体能力，界面上做不到（端点成孤儿）。 */
async function caprRawLoad(offset) {
  const body = document.getElementById('capr-raw-body');
  if (!body) return;
  const q = (document.getElementById('capr-raw-q') || {}).value || '';
  const kind = (document.getElementById('capr-raw-kind') || {}).value || '';
  const status = (document.getElementById('capr-raw-status') || {}).value || '';
  const limit = 50;
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset || 0) });
  if (q) qs.set('q', q);
  if (kind) qs.set('kind', kind);
  if (status) qs.set('status', status);
  body.innerHTML = '<div class="loading" style="font-size:11.5px;">加载原始注册表…</div>';
  try {
    const r = await fetch('/api/capabilities/registry?' + qs.toString());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const rows = d.rows || [];
    const total = d.total || 0;
    const off = d.offset || 0;
    const meta = document.getElementById('capr-raw-meta');
    if (meta) {
      const bk = d.by_kind || {};
      meta.textContent = ` · 共 ${total} 行` +
        (Object.keys(bk).length ? `（${Object.keys(bk).map(k => k + ' ' + bk[k]).join(' / ')}）` : '');
    }
    const trs = rows.map(x => `<tr>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:11.5px;">
        <div>${caprEsc(x.display_name || x.capability_id)}</div>
        <div style="font-size:10px;color:var(--mut);font-family:ui-monospace,Consolas,monospace;">${caprEsc(x.capability_id)}</div>
      </td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:11px;color:var(--mut);">${caprEsc(x.kind)}</td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:11px;">${caprEsc(x.status || '')}</td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;font-size:11.5px;">${caprNum(x.call_count)}</td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;font-size:11.5px;">
        ${x.fail_count > 0 ? '<span style="color:#c62828;">' + caprNum(x.fail_count) + '</span>' : '<span style="color:var(--mut);">—</span>'}</td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:10.5px;color:var(--mut);">${caprEsc((x.last_used_at || '').slice(0, 16) || '—')}</td>
    </tr>`).join('');
    const pageBtn = (label, target, disabled) =>
      `<button class="btn sm" ${disabled ? 'disabled style="opacity:.45;cursor:default;"' : ''}
        onclick="caprRawLoad(${target})">${label}</button>`;
    body.innerHTML = `
      <div style="max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
        <table style="width:100%;border-collapse:collapse;">
          <thead><tr style="position:sticky;top:0;background:var(--bg2,#f7f9fc);color:var(--mut);font-size:11px;text-align:left;">
            <th style="padding:6px 8px;">能力</th><th style="padding:6px 8px;">类型</th>
            <th style="padding:6px 8px;">状态</th>
            <th style="padding:6px 8px;text-align:right;">调用</th>
            <th style="padding:6px 8px;text-align:right;">失败</th>
            <th style="padding:6px 8px;">最近使用</th>
          </tr></thead>
          <tbody>${trs || '<tr><td colspan="6" style="padding:14px;color:var(--mut);font-size:11.5px;">无匹配行</td></tr>'}</tbody>
        </table>
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px;font-size:11px;color:var(--mut);">
        ${pageBtn('‹ 上一页', Math.max(0, off - limit), off <= 0)}
        <span>${off + 1}–${Math.min(off + rows.length, total)} / ${total}</span>
        ${pageBtn('下一页 ›', off + limit, off + limit >= total)}
      </div>`;
  } catch (e) {
    body.innerHTML = `<div style="color:#c62828;font-size:11.5px;">原始注册表加载失败：${caprEsc(e && e.message)}</div>`;
  }
}

function caprDays(el) {
  CAPR_STATE.days = parseInt(el.value || '30', 10) || 30;
  loadCapRegistry();
}

function caprFilter(el, key) {
  CAPR_STATE[key] = el.value || '';
  loadCapRegistry();
}

async function caprRediscoverMcp() {
  const box = document.getElementById('capr-rediscover');
  const btn = document.getElementById('capr-rediscover-btn');
  if (!box) return;
  if (btn) { btn.disabled = true; btn.textContent = '🔄 探测中…'; }
  box.innerHTML = '<div class="loading">正在重新探测全部在线 MCP 服务器并回填工具元数据…</div>';
  try {
    const r = await fetch('/api/studio/mcp-servers/check-all', {method:'POST'}).then(x => x.json());
    const details = (r.details || []).map(d => {
      const ok = d.status === 'online';
      const tools = (d.tool_meta_tools != null ? d.tool_meta_tools : '?');
      const back = d.tool_meta_backfilled ? ' · 已回填' : '';
      const err = d.backfill_error ? ' · ⚠️' + caprEsc(d.backfill_error) : '';
      return `<div style="font-size:11.5px;padding:2px 0;">
        ${caprChip(d.name, ok ? {color:'#2e7d32',bg:'#e8f5e9'} : {color:'#c62828',bg:'#fdecea'})}
        ${ok ? '在线' : '离线'} · 工具 ${caprNum(tools)}${back}${err}</div>`;
    }).join('');
    const online = r.online != null ? r.online : '?';
    const checked = r.checked != null ? r.checked : '?';
    box.innerHTML = `<div style="padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg2,#f7f9fc);">
      <div style="font-size:12px;margin-bottom:6px;"><b>重新发现完成</b>：${caprNum(online)}/${caprNum(checked)} 在线
        ${r.backfilled ? ' · 本轮回填 ' + caprNum(r.backfilled) + ' 台' : ''}</div>
      ${details || '<div style="font-size:11.5px;color:var(--mut);">无 MCP 服务器</div>'}
      <div style="font-size:11px;color:var(--mut);margin-top:6px;">下方账本已刷新；mcp_tool 投影与 registry 状态同步更新。</div>
    </div>`;
    loadCapRegistry();
  } catch (e) {
    box.innerHTML = `<div style="color:#c62828;font-size:12px;">重新发现失败：${caprEsc(e && e.message)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 重新发现 MCP 工具'; }
  }
}

async function caprTraceDetail() {
  const inp = document.getElementById('capr-trace');
  const box = document.getElementById('capr-trace-detail');
  if (!inp || !box) return;
  const tid = (inp.value || '').trim();
  if (tid.length < 4) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="loading">查询中…</div>';
  try {
    const d = await fetch('/api/capabilities/trace/detail?trace_id=' +
      encodeURIComponent(tid)).then(r => r.json());
    const calls = d.tool_calls || [];
    const llm = d.llm || {};
    box.innerHTML = `
      <div style="margin-top:10px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;">
        <div style="font-size:12px;margin-bottom:6px;">
          工具调用 <b>${caprNum(d.tool_call_count)}</b> 次 ·
          LLM 调用 <b>${caprNum(llm.calls)}</b> 次 ·
          prompt <b>${caprNum(llm.prompt_tokens)}</b> tok
          （其中缓存命中 <b>${caprNum(llm.cache_hit_tokens)}</b>）·
          成本 <b>¥${(Number(llm.cost) || 0).toFixed(4)}</b>
        </div>
        ${calls.length ? `<table style="width:100%;border-collapse:collapse;font-size:11.5px;">
          <thead><tr style="color:var(--mut);font-size:11px;text-align:left;">
            <th style="padding:4px 8px;">工具</th><th style="padding:4px 8px;">能力 ID</th>
            <th style="padding:4px 8px;">结果</th><th style="padding:4px 8px;text-align:right;">耗时</th>
            <th style="padding:4px 8px;">时间</th></tr></thead>
          <tbody>${calls.map(c => `<tr>
            <td style="padding:4px 8px;border-bottom:1px solid var(--line);">${caprEsc(c.tool_name)}</td>
            <td style="padding:4px 8px;border-bottom:1px solid var(--line);font-size:10.5px;
                color:var(--mut);font-family:ui-monospace,Consolas,monospace;">${caprEsc(c.capability_id || '—')}</td>
            <td style="padding:4px 8px;border-bottom:1px solid var(--line);">
              ${c.ok ? caprChip('成功', { color: '#2e7d32', bg: '#e8f5e9' })
                     : caprChip(c.failure_kind || '失败', { color: '#c62828', bg: '#fdecea' })}</td>
            <td style="padding:4px 8px;border-bottom:1px solid var(--line);text-align:right;">${caprNum(c.latency_ms)} ms</td>
            <td style="padding:4px 8px;border-bottom:1px solid var(--line);font-size:10.5px;color:var(--mut);">
              ${caprEsc((c.created_at || '').slice(0, 19))}</td>
          </tr>`).join('')}</tbody></table>`
        : '<div style="font-size:11.5px;color:var(--mut);">该 trace 没有工具调用（纯 LLM 轮次）。</div>'}
      </div>`;
  } catch (e) {
    box.innerHTML = `<div style="color:#c62828;font-size:12px;">查询失败：${caprEsc(e && e.message)}</div>`;
  }
}
