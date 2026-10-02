// ── 设置·AI 记忆（st-mem tab）──
// 2026-10-02：跨会话长期记忆（agent_memory）的**可见 + 可删**入口。
// 数据源：GET /api/memory/list（列表 + stats）、DELETE /api/memory/{id}（真删）、
//         POST .../{id}/forget|restore、POST /api/memory/purge-forgotten、GET /api/memory/export。
//
// 为什么必须有这个页面（对标标杆）：
//   ChatGPT（设置→个性化→记忆）、Claude（展示可编辑的记忆摘要 + 删除对话即删派生记忆）、
//   Gemini 都让用户**看得见、删得掉** AI 记住了什么。本工程此前只有写入没有落点 ——
//   上百条 preference/fact 进了库，用户在界面上看不到，也无法删除。
//
// 两条删除语义**刻意分开**（口径同后端 MemoryAdminRepo，别在前端把它抹平）：
//   · 🗑 删除   = 真删（DELETE），不可恢复 —— 用户行使「被遗忘权」。
//   · 📦 归档   = 软删（forgotten=1），检索跳过、可恢复 —— 与遗忘引擎同语义。
// ⚠️ 记忆目前按 agent 归属、**无用户维度**（全局可见）。多用户部署前必须补 user 列。

const MEM_TIER_META = {
  core: { label: '常驻', cls: 'ok', hint: '偏好类：每轮注入，不按相关性过滤' },
  recall: { label: '按需召回', cls: 'g', hint: '与当前问题相关时才注入' },
  archival: { label: '已归档', cls: 'w', hint: '软删：检索跳过，可恢复' },
};
const MEM_TYPE_LABEL = {
  preference: '偏好', fact: '事实', experience: '经验', rule: '规则',
  decision: '决策', summary: '摘要', context: '上下文',
};

let _memQTimer = null;      // 关键词输入防抖（服务端筛选）
let _memCache = [];         // 当前列表（用于导出/操作回显）

// ── 加载与渲染 ─────────────────────────────────────────────────────────────
async function loadMemories() {
  const table = document.getElementById('mem-table');
  if (!table) return;
  const val = id => { const el = document.getElementById(id); return el ? el.value : ''; };
  const qs = new URLSearchParams();
  if (val('mem-f-agent')) qs.set('agent_id', val('mem-f-agent'));
  if (val('mem-f-tier')) qs.set('tier', val('mem-f-tier'));
  if (val('mem-f-type')) qs.set('mem_type', val('mem-f-type'));
  const q = String(val('mem-f-q') || '').trim();
  if (q) qs.set('q', q);
  const inc = document.getElementById('mem-f-forgotten');
  if (inc && inc.checked) qs.set('include_forgotten', '1');
  qs.set('limit', '500');
  try {
    const d = await api('/api/memory/list?' + qs.toString());
    if (d && d.error) { toast.error('加载失败：' + d.error); return; }
    _memCache = (d && d.items) || [];
    memFillFilters(d && d.stats);
    memRenderStats(d && d.stats);
    memRenderTable(_memCache);
  } catch (e) {
    table.innerHTML = '<div class="loading">加载失败：' + esc(e.message || e) + '</div>';
    toast.error('加载失败：' + (e.message || e));
  }
}

/** 筛选下拉的选项来自 stats 分布（agent/类型）。只在签名变化时重建，保留用户当前选中值。 */
function memFillFilters(stats) {
  const s = stats || {};
  const fill = (id, map, allLabel, labeler) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const keys = Object.keys(map || {});
    const sig = keys.join(',');
    if (sel.dataset.sig === sig) return;
    const cur = sel.value;
    sel.innerHTML = `<option value="">${allLabel}</option>`
      + keys.map(k => `<option value="${escA(k)}">${esc(labeler(k, map[k]))}</option>`).join('');
    sel.dataset.sig = sig;
    if (cur) sel.value = cur;
  };
  fill('mem-f-agent', s.by_agent, 'Agent：全部', (k, n) => `${k}（${n}）`);
  fill('mem-f-type', s.by_mem_type, '类型：全部', (k, n) => `${MEM_TYPE_LABEL[k] || k}（${n}）`);
}

/** 顶部统计：存活/归档/被访问过 + 层级分布。 */
function memRenderStats(stats) {
  const el = document.getElementById('mem-summary');
  if (!el) return;
  const s = stats || {};
  const bt = s.by_tier || {};
  const parts = [`存活 ${s.alive || 0} 条`];
  Object.keys(MEM_TIER_META).forEach(k => {
    if (bt[k]) parts.push(`${MEM_TIER_META[k].label} ${bt[k]}`);
  });
  if (s.forgotten) parts.push(`已归档 ${s.forgotten}`);
  if (s.accessed) parts.push(`被访问过 ${s.accessed}`);
  el.innerHTML = parts.map(p => esc(p)).join(' · ');
}

function memRenderTable(items) {
  const table = document.getElementById('mem-table');
  if (!table) return;
  if (!items || !items.length) {
    table.innerHTML = '<div style="text-align:center;color:var(--mut);padding:20px;">'
      + '暂无记忆。AI 在对话中沉淀的偏好/事实/经验会自动出现在这里。</div>';
    return;
  }
  const rows = items.map(m => {
    const tm = MEM_TIER_META[m.tier] || { label: m.tier || '—', cls: 'g', hint: '' };
    const tl = MEM_TYPE_LABEL[m.mem_type] || m.mem_type || '—';
    const content = String(m.content || '');
    const short = content.length > 120 ? content.slice(0, 120) + '…' : content;
    const ops = m.forgotten
      ? `<button class="btn sm ghost" onclick="memRestore(${m.id})" title="恢复到检索范围">♻️ 恢复</button>
         <button class="btn sm red" onclick="memDelete(${m.id})" title="真删，不可恢复">🗑 删除</button>`
      : `<button class="btn sm ghost" onclick="memForget(${m.id})" title="软删：检索不再注入，可恢复">📦 归档</button>
         <button class="btn sm red" onclick="memDelete(${m.id})" title="真删，不可恢复">🗑 删除</button>`;
    return `<tr>
      <td>#${m.id}</td>
      <td style="font-size:11.5px;">${esc(m.agent_id || '-')}</td>
      <td style="font-size:11.5px;">${esc(tl)}</td>
      <td><span class="st ${tm.cls}" title="${escA(tm.hint)}">${esc(tm.label)}</span></td>
      <td style="max-width:420px;font-size:11.5px;" title="${escA(content)}">${esc(short)}</td>
      <td style="font-size:11px;color:var(--mut);text-align:right;">${m.access_count || 0}</td>
      <td style="font-size:11px;color:var(--mut);white-space:nowrap;">${esc(m.created_at || '-')}</td>
      <td style="white-space:nowrap;">${ops}</td>
    </tr>`;
  }).join('');
  table.innerHTML = '<table class="t"><thead><tr>'
    + '<th>ID</th><th>Agent</th><th>类型</th><th>层级</th><th>内容</th>'
    + '<th style="text-align:right;">访问</th><th>创建时间</th><th>操作</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

/** 关键词输入防抖（300ms），避免每敲一键都打接口。 */
function memFilterQ() {
  clearTimeout(_memQTimer);
  _memQTimer = setTimeout(loadMemories, 300);
}

// ── 操作 ───────────────────────────────────────────────────────────────────
async function memDelete(id) {
  const m = (_memCache || []).find(x => x.id === id);
  const preview = m ? String(m.content || '').slice(0, 60) : '';
  if (!(await confirmDialog(
    `确认永久删除这条记忆？\n\n${preview}${preview.length >= 60 ? '…' : ''}\n\n`
    + '删除后不可恢复，AI 将不再记得该内容。若只是想让它不再注入，请改用「归档」。',
    { title: '永久删除记忆（不可恢复）', okText: '永久删除' }))) return;
  try {
    const r = await api(`/api/memory/${id}`, { method: 'DELETE' });
    if (r && r.error) { toast.error('删除失败：' + r.error); return; }
    toast('已删除');
    loadMemories();
  } catch (e) { toast.error('删除失败：' + (e.message || e)); }
}

async function memForget(id) {
  try {
    const r = await api(`/api/memory/${id}/forget`, { method: 'POST' });
    if (r && r.error) { toast.error('归档失败：' + r.error); return; }
    toast('已归档（检索不再注入，可恢复）');
    loadMemories();
  } catch (e) { toast.error('归档失败：' + (e.message || e)); }
}

async function memRestore(id) {
  try {
    const r = await api(`/api/memory/${id}/restore`, { method: 'POST' });
    if (r && r.error) { toast.error('恢复失败：' + r.error); return; }
    toast('已恢复');
    loadMemories();
  } catch (e) { toast.error('恢复失败：' + (e.message || e)); }
}

async function memExport() {
  try {
    const d = await api('/api/memory/export');
    if (d && d.error) { toast.error('导出失败：' + d.error); return; }
    const blob = new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `ai-memory-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`已导出 ${d.count || 0} 条记忆`);
  } catch (e) { toast.error('导出失败：' + (e.message || e)); }
}
