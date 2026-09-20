/* 覆盖性分析页（SRS-GN-CO，2026-09-20）
 * 数据源：/api/coverage/{tool}（只读确定性工具层 coverage_tools.exec_tool，与对话 Agent 同源）
 * 五个视图：覆盖矩阵 / 追溯链 / 场景覆盖 / 缺项汇总 / 建模批次
 * 全局作用域（非 module），内联 onclick 依赖全局函数名
 */
const COV_TOOLS = ['coverage_matrix', 'trace_chain_check', 'scene_coverage', 'gap_summary', 'modeling_coverage'];
const COV_TABS = { coverage_matrix: '覆盖矩阵', trace_chain_check: '追溯链', scene_coverage: '场景覆盖', gap_summary: '缺项汇总', modeling_coverage: '建模批次' };
const covState = { project: '', branch: '', tool: 'coverage_matrix' };

function covEsc(s) { return esc(String(s == null ? '' : s)); }

async function initCoverage() {
  // 工程下拉：全部工程 + 预选默认工程；分支下拉：空=默认分支（由工具层 _resolve_scope 决定）
  try {
    const [projs, def] = await Promise.all([api('/api/projects'), api('/api/projects/default')]);
    const sel = document.getElementById('cov-project');
    sel.innerHTML = '<option value="">（当前默认工程）</option>' +
      (projs || []).map(p => `<option value="${covEsc(p.id)}">${covEsc(p.name || p.id)}</option>`).join('');
    if (def && def.id) sel.value = def.id;
  } catch (e) { /* 下拉保持默认项 */ }
  covState.project = document.getElementById('cov-project').value;
  covState.branch = document.getElementById('cov-branch').value;
  renderCovTabs();
  runCoverage();
}

function renderCovTabs() {
  document.getElementById('cov-tabs').innerHTML = COV_TOOLS.map(t =>
    `<button class="btn sm ${t === covState.tool ? '' : 'ghost'}" onclick="covSwitchTool('${t}')">${COV_TABS[t]}</button>`).join('');
}

function covSwitchTool(t) { covState.tool = t; renderCovTabs(); runCoverage(); }

async function runCoverage() {
  const body = document.getElementById('cov-body');
  body.innerHTML = '<div class="loading">分析中…</div>';
  const qs = new URLSearchParams();
  if (covState.project) qs.set('project_id', covState.project);
  if (covState.branch) qs.set('branch', covState.branch);
  try {
    const r = await api(`/api/coverage/${covState.tool}?` + qs.toString());
    if (!r.ok) {
      body.innerHTML = `<div style="border:1px dashed var(--line);border-radius:10px;padding:24px;text-align:center;color:var(--mut);font-size:13px;">
        ⚠️ ${covEsc(r.result)}<br><span style="font-size:11px;">（覆盖性分析拒绝全库混算：请先在报告页/界面设置默认工程，或在上方选择工程）</span></div>`;
      return;
    }
    const o = typeof r.result === 'string' ? JSON.parse(r.result) : r.result;
    const scopeHtml = covScopeBar(o.scope || o);
    body.innerHTML = scopeHtml + (COV_RENDER[covState.tool] || (() => ''))(o);
  } catch (e) {
    body.innerHTML = `<div class="loading">❌ 加载失败：${covEsc(e.message)}</div>`;
  }
}

function covScopeBar(sc) {
  const bits = [];
  if (sc.project_id) bits.push(`工程 <b>${covEsc(sc.project_id)}</b>`);
  if (sc.branch || sc.baseline_branch) bits.push(`分支 <b>${covEsc(sc.branch || sc.baseline_branch)}</b>`);
  if (sc.batch_id) bits.push(`批次 <b>${covEsc(sc.batch_id)}</b>`);
  if (sc.rule_version) bits.push(`规则 <b>${covEsc(sc.rule_version)}</b>`);
  if (sc.entity_count != null) bits.push(`实体 ${sc.entity_count}`);
  if (sc.trace_relation_count != null) bits.push(`追溯关系 ${sc.trace_relation_count}`);
  return `<div style="font-size:11px;color:var(--mut);margin-bottom:10px;display:flex;gap:14px;flex-wrap:wrap;">${bits.map(b => `<span>${b}</span>`).join('')}</div>`;
}

function covChip(text, color) {
  const c = color || 'var(--line)';
  return `<span style="display:inline-block;border:1px solid ${c};border-radius:20px;padding:1px 8px;font-size:11px;margin:1px 2px;">${covEsc(text)}</span>`;
}

function covRate(pct) {
  if (pct == null) return '<b style="color:var(--mut)">—</b>';
  const v = Math.round(pct * 1000) / 10;
  const color = v >= 80 ? '#18a058' : (v >= 50 ? '#d88c00' : '#d03050');
  return `<b style="color:${color};font-size:20px;">${v}%</b>`;
}

/* ── 视图 1：覆盖矩阵 ── */
function covRenderMatrix(o) {
  const s = o.summary || {};
  const rows = (o.matrix || []).map(m => {
    const covered = (m.covered_by || []).length > 0;
    const chips = (m.covered_by || []).map(cb =>
      covChip(cb.arch_name, cb.rel === 'DERIVES' ? '#d88c00' : '')).join('') || '<span style="color:#d03050;font-size:11px;">未覆盖</span>';
    return `<tr>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:12px;"><b>${covEsc(m.req.name)}</b><br><span style="color:var(--mut);font-size:10px;">${covEsc(m.req.type)}</span></td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);">${chips}</td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);text-align:center;">${covered ? '<span style="color:#18a058;">✓</span>' : '<span style="color:#d03050;">✗</span>'}</td>
    </tr>`;
  }).join('');
  const unc = (o.uncovered_requirements || []).map(u => covChip(u.name, '#d03050')).join('') || '无';
  const baseless = (o.baseless_arch_elements || []).map(u => covChip(u.name + '（' + u.entity_type + '）', '#d88c00')).join('') || '无';
  const abnormal = (o.abnormal_relations || []).map(u => covChip(u.kind + ': ' + u.detail, '#d03050')).join('') || '无';
  const dist = (o.arch_type_distribution || []).map(d => covChip(d.entity_type + ' ×' + d.count)).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;">
    <div class="panel" style="padding:10px;text-align:center;">需求总数<div>${s.requirement_total ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">已覆盖<div>${s.covered_requirements ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">覆盖率<div>${covRate(s.coverage_rate)}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">派生覆盖<div>${s.derive_only_requirements ?? 0}</div></div>
  </div>
  <div class="panel"><div class="ph">需求 × 架构 覆盖矩阵 <span style="flex:1"></span><span style="font-size:10px;color:var(--mut);">${dist}</span></div>
  <div class="pb" style="padding:0;max-height:420px;overflow:auto;"><table style="width:100%;border-collapse:collapse;">
    <thead><tr style="background:rgba(127,163,204,.08);"><th style="padding:6px 8px;text-align:left;font-size:11px;">需求</th><th style="padding:6px 8px;text-align:left;font-size:11px;">覆盖关系（按架构元素）</th><th style="padding:6px 8px;font-size:11px;">状态</th></tr></thead>
    <tbody>${rows}</tbody></table></div></div>
  <div class="panel" style="margin-top:10px;"><div class="ph">三类异常</div><div class="pb" style="font-size:12px;line-height:2;">
    <div>🔴 未覆盖需求：${unc}</div><div>🟡 无需求依据的架构元素：${baseless}</div><div>🔴 异常追溯关系：${abnormal}</div>
  </div></div>`;
}

/* ── 视图 2：追溯链 ── */
function covRenderChains(o) {
  const s = o.summary || {};
  const chains = (o.chains || []).map(c => {
    const arch = (c.sample_targets || []).find(t => t.via_rel === 'SATISFIES' || t.via_rel === 'ALLOCATED_TO');
    const verify = (c.sample_targets || []).filter(t => t.via_rel === 'VERIFIED_BY');
    const okArch = (c.reachable_arch_count || 0) > 0;
    const node = (txt, bad) => `<span style="border:1px solid ${bad ? '#d03050' : 'var(--line)'};${bad ? 'color:#d03050;' : ''}border-radius:8px;padding:3px 10px;font-size:11px;background:rgba(127,163,204,.06);">${txt}</span>`;
    const arrow = `<span style="color:var(--mut);font-size:11px;">→</span>`;
    const chain = [
      node('📋 ' + covEsc(c.req.name), false), arrow,
      node(okArch ? '🏗 ' + covEsc(arch ? arch.name : `架构 ×${c.reachable_arch_count}`) : '🏗 断链（无架构）', !okArch), arrow,
      node(c.reached_verification ? ('🧪 ' + covEsc(verify.length ? verify[0].name : '已验证')) : '🧪 未到验证', !c.reached_verification),
    ].join(' ');
    return `<div style="border-bottom:1px solid var(--line);padding:8px 4px;">
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">${chain}
        <span style="margin-left:auto;font-size:10px;color:var(--mut);">深度 ${c.max_hops}</span></div>
      <div style="margin-top:4px;font-size:10px;color:var(--mut);">架构可达 ${c.reachable_arch_count} 个 ｜ 验证用例 ${verify.map(v => covEsc(v.name)).join('、') || '—'}</div>
    </div>`;
  }).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px;">
    <div class="panel" style="padding:10px;text-align:center;">需求总数<div>${s.requirement_total ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">架构可达<div>${s.with_arch_link ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">验证率<div>${covRate(s.verification_rate)}</div></div>
  </div>
  <div class="panel"><div class="ph">端到端追溯链（需求 → 架构 → 验证）</div><div class="pb" style="padding:0;max-height:460px;overflow:auto;">${chains}</div></div>`;
}

/* ── 视图 3：场景覆盖 ── */
function covRenderScenes(o) {
  const s = o.summary || {};
  const cards = (o.scenes || []).map(sc => {
    const linked = sc.linked_total || 0;
    const acts = (sc.activities_states || []).slice(0, 6).map(a => covChip(a.name + '（' + a.rel + '）')).join('');
    return `<div class="panel" style="padding:10px;margin-bottom:8px;">
      <div style="display:flex;gap:8px;align-items:center;"><b style="font-size:13px;">🎬 ${covEsc(sc.scene.name)}</b>
        <span style="font-size:10px;color:var(--mut);">${covEsc(sc.scene.type)}</span>
        <span style="margin-left:auto;font-size:11px;color:${linked > 0 ? '#18a058' : '#d03050'};">${linked > 0 ? '关联 ' + linked + ' 项' : '孤立场景'}</span></div>
      <div style="margin-top:6px;font-size:11px;line-height:1.9;">${acts || '<span style="color:#d03050;">无活动/状态关联</span>'}</div>
    </div>`;
  }).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px;">
    <div class="panel" style="padding:10px;text-align:center;">场景总数<div>${s.scene_total ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">有活动链<div>${s.with_activity_chain ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">孤立场景<div style="color:${(s.orphan_scenes || []).length ? '#d03050' : '#18a058'};">${(s.orphan_scenes || []).length}</div></div>
  </div>
  <div class="panel"><div class="ph">场景链路（活动 / 状态 / 参与对象）</div><div class="pb" style="max-height:460px;overflow:auto;">${cards}</div></div>`;
}

/* ── 视图 4：缺项汇总 ── */
function covRenderGaps(o) {
  const s = o.summary || {};
  const riskMeta = { high: ['高风险', '#d03050'], medium: ['中风险', '#d88c00'], low: ['低风险', 'var(--mut)'] };
  const byRisk = { high: [], medium: [], low: [] };
  (o.gaps || []).forEach(g => { (byRisk[g.risk] = byRisk[g.risk] || []).push(g); });
  const sections = Object.entries(riskMeta).map(([rk, [label, color]]) => {
    const items = (byRisk[rk] || []).map(g => `
      <div style="border:1px solid var(--line);border-left:3px solid ${color};border-radius:8px;padding:6px 10px;margin-bottom:6px;font-size:12px;">
        <b>${covEsc(g.name)}</b> <span style="color:var(--mut);font-size:10px;">${covEsc(g.category)}</span>
        <div style="color:var(--mut);font-size:11px;">${covEsc(g.detail)} ｜ ${covEsc(g.locate || '')}</div>
      </div>`).join('');
    return `<div style="margin-bottom:12px;"><div style="font-size:12px;margin-bottom:6px;"><b style="color:${color};">${label}</b>（${(byRisk[rk] || []).length}）</div>${items || '<span style="color:var(--mut);font-size:11px;">无</span>'}</div>`;
  }).join('');
  const cats = Object.entries(s.by_category || {}).map(([k, v]) => covChip(k + ' ×' + v)).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px;">
    <div class="panel" style="padding:10px;text-align:center;">缺项总数<div style="color:${s.gap_total ? '#d03050' : '#18a058'};font-size:20px;"><b>${s.gap_total ?? '—'}</b></div></div>
    <div class="panel" style="padding:10px;text-align:center;">覆盖率<div>${covRate(s.coverage_rate)}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">验证率<div>${covRate(s.verification_rate)}</div></div>
  </div>
  <div style="font-size:11px;color:var(--mut);margin-bottom:10px;">${cats}</div>
  <div class="panel"><div class="ph">缺项清单（按风险分级 · 不得自动隐藏）</div><div class="pb" style="max-height:440px;overflow:auto;">${sections}</div></div>`;
}

/* ── 视图 5：建模批次 ── */
function covRenderModeling(o) {
  const b = o.batch || {}, c = o.coverage || {};
  const bar = (label, rate) => `<div style="display:flex;align-items:center;gap:10px;margin:8px 0;">
    <span style="width:90px;font-size:12px;color:var(--mut);">${label}</span>
    <div style="flex:1;height:14px;background:rgba(127,163,204,.12);border-radius:7px;overflow:hidden;">
      <div style="width:${Math.round((rate || 0) * 100)}%;height:100%;background:${(rate || 0) >= 0.5 ? '#18a058' : '#d88c00'};"></div></div>
    <span style="width:64px;font-size:12px;">${rate == null ? '—' : Math.round(rate * 1000) / 10 + '%'}</span></div>`;
  const gaps = (o.modeling_gaps || []).map(g => `<div style="border:1px solid #d88c00;border-radius:8px;padding:6px 10px;margin-bottom:6px;font-size:12px;background:rgba(216,140,0,.06);">⚠️ ${covEsc(g)}</div>`).join('') || '无';
  const types = (b.entity_type_distribution || []).map(d => covChip(d.entity_type + ' ×' + d.count)).join('');
  const match = Object.entries(b.matching_distribution || {}).map(([k, v]) => covChip(k + ' ×' + v)).join('');
  return `
  <div class="panel" style="margin-bottom:10px;"><div class="ph">覆盖率对比（基线 → 并入本批次）</div><div class="pb">
    ${bar('基线', (c.baseline || {}).coverage_rate)}
    ${bar('合并后', (c.merged || {}).coverage_rate)}
    <div style="font-size:11px;color:var(--mut);">Δ ${(c.coverage_rate_delta ?? 0) >= 0 ? '+' : ''}${Math.round((c.coverage_rate_delta || 0) * 1000) / 10}% ｜
      需求 ${((c.baseline || {}).requirement_total ?? '—')} → ${((c.merged || {}).requirement_total ?? '—')} ｜
      追溯关系 ${((c.merged || {}).trace_relation_total ?? '—')}（不变 = 本批次无追溯关系候选）</div>
  </div></div>
  <div class="panel" style="margin-bottom:10px;"><div class="ph">批次构成</div><div class="pb" style="font-size:12px;line-height:2;">
    候选总数 <b>${b.candidate_total ?? 0}</b>（实体 ${b.entity_candidates ?? 0} ｜ 关系 ${b.relation_candidates ?? 0} ｜ 关系形态误入实体池 ${b.relation_like_entity_candidates ?? 0}）<br>
    类型分布 ${types}<br>重复匹配 ${match}（并入基线 ${b.dup_merged_into_baseline ?? 0}）
  </div></div>
  <div class="panel"><div class="ph">建模缺口（下一步该补什么）</div><div class="pb">${gaps}</div></div>`;
}

const COV_RENDER = {
  coverage_matrix: covRenderMatrix,
  trace_chain_check: covRenderChains,
  scene_coverage: covRenderScenes,
  gap_summary: covRenderGaps,
  modeling_coverage: covRenderModeling,
};
