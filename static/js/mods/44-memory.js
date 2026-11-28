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
  if (val('mem-f-scope')) qs.set('scope_type', val('mem-f-scope'));
  if (val('mem-f-verify')) qs.set('verify_state', val('mem-f-verify'));
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
  const VERIFY_LABEL = { verified: '已证实', needs_review: '待人工', unverifiable: '无法验证', contradicted: '已证伪' };
  fill('mem-f-verify', s.by_verify_state, '校验：全部', (k, n) => `${VERIFY_LABEL[k] || k}（${n}）`);
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

const MEM_VERIFY_LABEL = {
  verified: '已证实', needs_review: '待人工', unverifiable: '无法验证', contradicted: '已证伪',
};
const MEM_SCOPE_LABEL = { project: '工程', user: '用户', '': '全局' };

function memRenderTable(items) {
  const table = document.getElementById('mem-table');
  if (!table) return;
  if (!items || !items.length) {
    table.innerHTML = '<div style="text-align:center;color:var(--mut);padding:20px;">'
      + '暂无记忆。AI 在对话中沉淀的偏好/事实/经验会自动出现在这里。</div>';
    memUpdateSelected();
    return;
  }
  const rows = items.map(m => {
    const tm = MEM_TIER_META[m.tier] || { label: m.tier || '—', cls: 'g', hint: '' };
    const tl = MEM_TYPE_LABEL[m.mem_type] || m.mem_type || '—';
    const content = String(m.content || '');
    const short = content.length > 100 ? content.slice(0, 100) + '…' : content;
    const scope = MEM_SCOPE_LABEL[m.scope_type || ''] || (m.scope_type || '全局');
    const verify = MEM_VERIFY_LABEL[m.verify_state || 'unverifiable'] || (m.verify_state || '无法验证');
    const ops = `<button class="btn sm ghost" onclick="memDetail(${m.id})" title="查看全部字段">🔍 详情</button>`
      + (m.forgotten
        ? `<button class="btn sm ghost" onclick="memRestore(${m.id})" title="恢复到检索范围">♻️ 恢复</button>
           <button class="btn sm red" onclick="memDelete(${m.id})" title="真删，不可恢复">🗑 删除</button>`
        : `<button class="btn sm ghost" onclick="memForget(${m.id})" title="软删：检索不再注入，可恢复">📦 归档</button>
           <button class="btn sm red" onclick="memDelete(${m.id})" title="真删，不可恢复">🗑 删除</button>`);
    return `<tr>
      <td><input type="checkbox" class="mem-pick" value="${m.id}" onchange="memUpdateSelected()"></td>
      <td>#${m.id}</td>
      <td style="font-size:12.5px;">${esc(m.agent_id || '-')}</td>
      <td style="font-size:12.5px;">${esc(tl)}</td>
      <td><span class="st ${tm.cls}" title="${escA(tm.hint)}">${esc(tm.label)}</span></td>
      <td style="font-size:12px;">${esc(scope)}<br><span style="color:var(--mut);">${esc(String(m.scope_id || '').slice(0, 12))}</span></td>
      <td style="font-size:12px;">${esc(verify)}</td>
      <td style="max-width:360px;font-size:12.5px;" title="${escA(content)}">${esc(short)}</td>
      <td style="font-size:12.5px;color:var(--mut);text-align:right;">${m.access_count || 0}</td>
      <td style="font-size:12.5px;color:var(--mut);white-space:nowrap;">${esc(m.created_at || '-')}</td>
      <td style="white-space:nowrap;">${ops}</td>
    </tr>`;
  }).join('');
  table.innerHTML = '<table class="t"><thead><tr>'
    + '<th><input type="checkbox" id="mem-select-all" onchange="memToggleAll(this.checked)" title="全选本页"></th>'
    + '<th>ID</th><th>Agent</th><th>类型</th><th>层级</th><th>作用域</th><th>校验</th><th>内容</th>'
    + '<th style="text-align:right;">访问</th><th>创建时间</th><th>操作</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
  memUpdateSelected();
}

function memSelectedIds() {
  return [...document.querySelectorAll('.mem-pick:checked')].map(x => Number(x.value)).filter(Boolean);
}
function memToggleAll(on) {
  document.querySelectorAll('.mem-pick').forEach(x => { x.checked = !!on; });
  memUpdateSelected();
}
function memUpdateSelected() {
  const el = document.getElementById('mem-selected');
  if (el) el.textContent = '已选 ' + memSelectedIds().length + ' 条';
  const all = document.getElementById('mem-select-all');
  if (all) {
    const boxes = [...document.querySelectorAll('.mem-pick')];
    all.checked = boxes.length > 0 && boxes.every(x => x.checked);
  }
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

// ── 详情 / 批量编辑（2026-11-28）─────────────────────────────────────────
async function memDetail(id) {
  try {
    const m = await api(`/api/memory/${id}`);
    if (!m || m.error) { toast.error('读取详情失败：' + (m && m.error || '未知错误')); return; }
    const kv = (k, v) => `<div class="kv"><span>${esc(k)}</span><b style="font-weight:500;word-break:break-word;">${esc(v == null || v === '' ? '—' : String(v))}</b></div>`;
    const emb = m.embedding ? `已向量化（维度 ${Array.isArray(m.embedding) ? m.embedding.length : '—'}，版本 ${esc(m.embed_version || '—')}）` : '无向量（等待重建）';
    const html = '<div style="font-size:13px;line-height:1.9;">'
      + kv('ID', m.id) + kv('Agent', m.agent_id) + kv('类型', MEM_TYPE_LABEL[m.mem_type] || m.mem_type)
      + kv('层级', (MEM_TIER_META[m.tier] || {}).label || m.tier) + kv('作用域', (MEM_SCOPE_LABEL[m.scope_type || ''] || m.scope_type || '全局') + ' / ' + (m.scope_id || '—'))
      + kv('主题', m.mem_topic) + kv('校验状态', MEM_VERIFY_LABEL[m.verify_state] || m.verify_state)
      + kv('证据引用', m.evidence_ref) + kv('证据说明', m.evidence_note)
      + kv('来源', m.source) + kv('来源可信度', m.source_credibility)
      + kv('相关性', m.relevance) + kv('激活度', m.activation) + kv('访问次数', m.access_count)
      + kv('最后访问', m.last_accessed_at) + kv('有效期', (m.valid_from || '—') + ' → ' + (m.valid_to || '—'))
      + kv('被取代于', m.superseded_by) + kv('归档时间', m.archived_at) + kv('软删', m.forgotten ? '是' : '否')
      + kv('向量', emb) + kv('创建时间', m.created_at)
      + '<div style="margin-top:10px;"><div style="color:var(--mut);margin-bottom:4px;">正文</div>'
      + `<div style="white-space:pre-wrap;background:var(--bg,#f7f9fc);border:1px solid var(--line);border-radius:6px;padding:10px;max-height:320px;overflow:auto;">${esc(m.content || '')}</div></div>`
      + '<div style="margin-top:12px;"><button class="btn sm" onclick="memBatchEdit([' + m.id + '])">✏️ 编辑此条</button></div>'
      + '</div>';
    openPanel('🔍 记忆详情 #' + m.id, html);
  } catch (e) { toast.error('读取详情失败：' + (e.message || e)); }
}

let _memBatchIds = [];
let _memOriginal = {};   // 单条编辑时的当前值，用于只提交真正改动的字段
function _memField(label, id, value, type) {
  const t = type || 'text';
  return `<label style="display:block;margin:5px 0;font-size:12.5px;">${label}<br>`
    + `<input id="${id}" type="${t}" value="${escA(value == null ? '' : value)}" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px 7px;font-size:12.5px;"></label>`;
}
async function memBatchEdit(ids) {
  const picked = ids || memSelectedIds();
  if (!picked.length) { toast.error('请先勾选记忆'); return; }
  _memBatchIds = picked;
  // ★ 2026-11-28：编辑页必须展示对应数据。单条先拉详情并预填；
  // 多条批量时留空并显示提示（不同记忆的字段通常不一致，预填任一条都会误导）。
  let cur = null;
  if (picked.length === 1) {
    try { cur = await api(`/api/memory/${picked[0]}`); } catch (e) { cur = null; }
    if (cur && cur.error) cur = null;
  }
  _memOriginal = cur || {};
  const v = k => (cur && cur[k] != null ? String(cur[k]) : '');
  const sel = (id, pairs, curVal) => {
    let opts = '<option value="">（不改）</option>';
    const all = pairs.slice();
    if (curVal && !all.some(p => p[0] === curVal)) all.push([curVal, curVal]);
    opts += all.map(([val, label]) => '<option value="' + escA(val) + '"'
      + (curVal && val === curVal ? ' selected' : '') + '>' + esc(label) + '</option>').join('');
    return '<label style="font-size:12.5px;">' + id.replace('mem-be-', '') + '<br><select id="' + id
      + '" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px;">' + opts + '</select></label>';
  };
  const typePairs = Object.keys(MEM_TYPE_LABEL).map(k => [k, MEM_TYPE_LABEL[k]]);
  const tierPairs = [['core', '常驻'], ['recall', '按需召回'], ['archival', '已归档']];
  const scopePairs = [['project', '工程'], ['user', '用户'], ['__global__', '全局（无作用域）']];
  const verifyPairs = Object.keys(MEM_VERIFY_LABEL).map(k => [k, MEM_VERIFY_LABEL[k]]);
  const fgPairs = [['0', '否（恢复检索）'], ['1', '是（归档）']];
  const curScope = cur ? (v('scope_type') || '__global__') : '';
  const hint = picked.length === 1 && cur
    ? '已载入当前值；修改需要改的字段，未改的保持原样。'
    : '批量编辑 ' + picked.length + ' 条：字段当前值可能不同，留空表示不修改该字段。';
  const html = '<div style="font-size:12.5px;color:var(--mut);margin-bottom:8px;">' + esc(hint)
    + '（编号 ' + picked.slice(0, 12).join(', ') + (picked.length > 12 ? '…' : '') + '）</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;">'
    + '<label style="font-size:12.5px;">类型<br><select id="mem-be-type" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px;">'
    + '<option value="">（不改）</option>' + typePairs.map(([val, label]) => '<option value="' + escA(val) + '"'
      + (v('mem_type') === val ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select></label>'
    + '<label style="font-size:12.5px;">层级<br><select id="mem-be-tier" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px;">'
    + '<option value="">（不改）</option>' + tierPairs.map(([val, label]) => '<option value="' + escA(val) + '"'
      + (v('tier') === val ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select></label>'
    + _memField('Agent', 'mem-be-agent', v('agent_id'))
    + _memField('主题', 'mem-be-topic', v('mem_topic'))
    + '<label style="font-size:12.5px;">作用域<br><select id="mem-be-scope" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px;">'
    + '<option value="">（不改）</option>' + scopePairs.map(([val, label]) => '<option value="' + escA(val) + '"'
      + (curScope === val ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select></label>'
    + _memField('作用域 ID', 'mem-be-scopeid', v('scope_id'))
    + '<label style="font-size:12.5px;">校验状态<br><select id="mem-be-verify" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px;">'
    + '<option value="">（不改）</option>' + verifyPairs.map(([val, label]) => '<option value="' + escA(val) + '"'
      + (v('verify_state') === val ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select></label>'
    + '<label style="font-size:12.5px;">软删<br><select id="mem-be-forgotten" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:4px;">'
    + '<option value="">（不改）</option>' + fgPairs.map(([val, label]) => '<option value="' + escA(val) + '"'
      + (cur && String(cur.forgotten) === val ? ' selected' : '') + '>' + esc(label) + '</option>').join('') + '</select></label>'
    + _memField('相关性', 'mem-be-relevance', v('relevance'), 'number')
    + _memField('激活度', 'mem-be-activation', v('activation'), 'number')
    + '</div>'
    + '<label style="display:block;margin-top:8px;font-size:12.5px;">正文（修改后旧向量会失效并重建）<br><textarea id="mem-be-content" rows="5" style="width:100%;border:1px solid var(--line);border-radius:5px;padding:6px;font-size:12.5px;">' + esc(v('content')) + '</textarea></label>'
    + '<div style="margin-top:12px;"><button class="btn" onclick="memBatchEditSave()">保存编辑</button></div>';
  openPanel((picked.length === 1 ? '✏️ 编辑记忆 #' + picked[0] : '✏️ 批量编辑记忆'), html);
}

async function memBatchEditSave() {
  const v = id => { const el = document.getElementById(id); return el ? String(el.value || '').trim() : ''; };
  const updates = {};
  // 单条编辑：表单预填了当前值，只提交与当前值不同的字段（避免无改动也清空向量）；
  // 多条编辑：留空=不改，填了才提交。
  const put = (k, val) => {
    if (!_memBatchIds || _memBatchIds.length !== 1) { if (val !== '') updates[k] = val; return; }
    const old = _memOriginal[k] == null ? '' : String(_memOriginal[k]);
    if (val !== old) updates[k] = val;
  };
  put('mem_type', v('mem-be-type')); put('tier', v('mem-be-tier'));
  put('agent_id', v('mem-be-agent')); put('mem_topic', v('mem-be-topic'));
  const sc = v('mem-be-scope'); put('scope_type', sc === '__global__' ? '' : sc);
  put('scope_id', v('mem-be-scopeid')); put('verify_state', v('mem-be-verify'));
  put('forgotten', v('mem-be-forgotten')); put('relevance', v('mem-be-relevance'));
  put('activation', v('mem-be-activation')); put('content', v('mem-be-content'));
  if (!Object.keys(updates).length) { toast.error('没有修改任何字段'); return; }
  try {
    const r = await api('/api/memory/batch', {
      method: 'PUT', body: JSON.stringify({ ids: _memBatchIds, updates }),
    });
    if (!r || r.error) { toast.error('批量编辑失败：' + (r && r.error || '未知错误')); return; }
    toast(`已编辑 ${r.updated || 0} 条` + (r.skipped && r.skipped.length ? `，跳过 ${r.skipped.length} 条（无权或不存在）` : ''));
    _memBatchIds = [];
    try { closePanel(); } catch (e) {}
    loadMemories();
  } catch (e) { toast.error('批量编辑失败：' + (e.message || e)); }
}

async function memBatchState(forgotten, label) {
  const ids = memSelectedIds();
  if (!ids.length) { toast.error('请先勾选记忆'); return; }
  try {
    const r = await api('/api/memory/batch', {
      method: 'PUT', body: JSON.stringify({ ids, updates: { forgotten } }),
    });
    if (!r || r.error) { toast.error(label + '失败：' + (r && r.error || '未知错误')); return; }
    toast(`${label} ${r.updated || 0} 条`);
    loadMemories();
  } catch (e) { toast.error(label + '失败：' + (e.message || e)); }
}
function memBatchForget() { return memBatchState(1, '批量归档'); }
function memBatchRestore() { return memBatchState(0, '批量恢复'); }

async function memBatchDelete() {
  const ids = memSelectedIds();
  if (!ids.length) { toast.error('请先勾选记忆'); return; }
  if (!(await confirmDialog(`确认永久删除选中的 ${ids.length} 条记忆？\n\n编号：${ids.slice(0, 20).join(', ')}${ids.length > 20 ? '…' : ''}\n\n删除后不可恢复。`, { title: '批量永久删除（不可恢复）', okText: '永久删除' }))) return;
  let ok = 0, fail = 0;
  for (const id of ids) {
    try {
      const r = await api(`/api/memory/${id}`, { method: 'DELETE' });
      if (r && r.error) fail++; else ok++;
    } catch (e) { fail++; }
  }
  toast(`已删除 ${ok} 条${fail ? '，失败 ' + fail + ' 条' : ''}`);
  loadMemories();
}

// ── 离线对账（Dreaming，2026-11-12）─────────────────────────────────────
// 端点：/api/memory/dream/{preview,apply,persist,ab-report}
// 纪律（与后端一致，别在前端抹平）：
//   · preview **只读**；只有显式勾选 + 点「应用所选」才写状态；
//   · apply 只改 verify_state / evidence_ref，**不改正文**；
//   · needs_review 是"规则法判不准、交人工"的中间态 —— 只展示，
//     不提供批量批准，也不把它混进 contradicted 的可勾选项；
//   · fingerprint + note_limit 必须原样回传（后端按同一窗口重算指纹）。
let _memDreamPkg = null;

async function memDreamPreview() {
  const box = document.getElementById('mem-dream-cands');
  const sum = document.getElementById('mem-dream-summary');
  if (!box) return;
  box.innerHTML = '<div class="loading">正在对账（只读）…</div>';
  try {
    const d = await api('/api/memory/dream/preview?note_limit=300');
    if (d && d.error) { toast.error('预览失败：' + d.error); return; }
    _memDreamPkg = d;
    const s = d.stats || {};
    const strong = (d.candidates || []).filter(c => c.state === 'contradicted');
    const review = (d.candidates || []).filter(c => c.state === 'needs_review');
    const ok = (d.candidates || []).filter(c => c.state === 'verified');
    sum.innerHTML = `扫描 <b>${s.notes_scanned || 0}</b> 条记忆 ｜ `
      + `<span style="color:var(--bad,#c55)">疑似证伪 ${strong.length}</span> ｜ `
      + `流水支持 ${ok.length} ｜ <b>需人工 ${review.length}</b> ｜ `
      + `重复合并 ${s.merge || 0} ｜ 层级建议 ${s.tier || 0}<br>`
      + `<span style="font-size:11.5px;">需人工项只列出标题，不提供批量批准；`
      + `「疑似证伪」需逐条看证据引用后再勾选。</span>`;
    document.getElementById('mem-dream-fp').textContent =
      '指纹 ' + String(d.fingerprint || '').slice(0, 12) + ' · 窗口 ' + d.note_limit;
    let html = '';
    if (strong.length === 0) {
      html += '<div style="font-size:12px;color:var(--mut);padding:6px 0;">'
        + '本次没有强信号（同一数值被流水紧邻否定）的候选。'
        + '这是正常的：判据刻意保守，避免误排除真实记忆。</div>';
    } else {
      html += strong.map(c => `<label style="display:block;padding:6px 4px;border-bottom:1px solid var(--line);cursor:pointer;font-size:12px;">
        <input type="checkbox" class="mem-dream-pick" value="${c.memory_id}">
        <b>#${c.memory_id}</b> ${esc(c.claim || '')}<br>
        <span style="color:var(--mut);">证据 ${esc(c.evidence_ref || '—')} ｜ ${esc(c.reason || '')}</span>
      </label>`).join('');
    }
    if (review.length) {
      html += `<details style="margin-top:8px;font-size:11.5px;color:var(--mut);">
        <summary>需人工判定 ${review.length} 条（展开查看，不提供勾选）</summary>`
        + review.slice(0, 30).map(c => `<div style="padding:3px 0;">#${c.memory_id} ${esc((c.claim || '').slice(0, 52))} — ${esc((c.reason || '').slice(0, 60))}</div>`).join('')
        + '</details>';
    }
    box.innerHTML = html;
    const btn = document.getElementById('mem-dream-apply-btn');
    if (btn) btn.disabled = strong.length === 0;
    toast('预览完成（未修改任何记忆）');
  } catch (e) { box.innerHTML = ''; toast.error('预览失败：' + (e.message || e)); }
}

async function memDreamApply() {
  if (!_memDreamPkg) { toast.error('请先生成预览'); return; }
  const ids = [...document.querySelectorAll('.mem-dream-pick:checked')]
    .map(x => Number(x.value)).filter(Boolean);
  if (!ids.length) { toast.error('请至少勾选一条候选'); return; }
  if (!(await confirmDialog(
    `将对 ${ids.length} 条记忆写入校验状态（编号 ${ids.join(', ')}）。\n\n`
    + '只改 verify_state / 证据引用，不删除也不改写正文；被证伪项将不再进入召回。',
    { title: '应用离线对账结果', okText: '应用所选' }))) return;
  try {
    const d = await api('/api/memory/dream/apply', {
      method: 'POST',
      body: JSON.stringify({ fingerprint: _memDreamPkg.fingerprint,
                             note_limit: _memDreamPkg.note_limit,
                             memory_ids: ids, approved_by: '设置页人工审批' }),
    });
    if (d && d.error) { toast.error('应用失败：' + d.error + (d.hint ? '（' + d.hint + '）' : '')); return; }
    toast(`已应用 ${d.applied || 0} 条（仅状态，正文未改）`);
    _memDreamPkg = null;
    document.getElementById('mem-dream-apply-btn').disabled = true;
    memDreamPreview();
    loadMemories();
  } catch (e) { toast.error('应用失败：' + (e.message || e)); }
}

async function memDreamPersist() {
  try {
    const d = await api('/api/memory/dream/persist', { method: 'POST' });
    if (d && d.error) { toast.error('落盘失败：' + d.error); return; }
    toast(`wiki 已落盘 ${d.written || 0} 页（data/memory_wiki/）`);
  } catch (e) { toast.error('落盘失败：' + (e.message || e)); }
}

async function memDreamAB() {
  try {
    const d = await api('/api/memory/dream/ab-report');
    if (!d.measured) { toast('尚未实测：请先运行 tools/eval/eval_memory_dream_ab.py'); return; }
    const a = d.arm_a_baseline || {}, b = d.arm_b_shadow_hypothetical || {};
    const box = document.getElementById('mem-dream-summary');
    box.innerHTML = `<b>双臂影子评测</b>（${esc(d.measured_at || '')}）<br>`
      + `A 现状：命中率 ${a.memory_hit_rate} / 每问题注入 ${a.tokens_per_question} 字符<br>`
      + `B 影子（假设全部批准）：命中率 ${b.memory_hit_rate} / ${b.tokens_per_question} 字符<br>`
      + `差值 ${JSON.stringify(d.delta)} ｜ 影子状态 ${d.shadow_states} 条`
      + `（证伪 ${d.shadow_contradicted} / 需人工 ${d.shadow_needs_review}）<br>`
      + `真库零写入：${d.db_untouched === true ? '✅ 已核验（状态指纹前后一致）' : '⚠️ 未核验/异常，请勿采用本组数字'}<br>`
      + `<span style="font-size:11.5px;">${esc(a.unit_note || '')} ｜ B 臂只在数据库副本里发生，是假设值，不是已发生的事实。</span>`;
  } catch (e) { toast.error('读取效果报告失败：' + (e.message || e)); }
}
