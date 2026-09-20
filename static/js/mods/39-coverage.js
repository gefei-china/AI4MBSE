/* 覆盖性分析呈现组件库（SRS-GN-CO，2026-09-21 重构）
 *
 * 定位（用户拍板方案 3）：不设独立导航页——分析能力全在 AI 建模环节（Agent+Skill+工具），
 * 本模块只负责「呈现」：① 对话流工具卡片内嵌富组件（06-cards.js 调 covTryCard）
 *                      ② AI 建模页右侧预览面板大视图（covOpenInPreview）
 * 数字与对话 Agent 同源：同一 coverage_tools.exec_tool（REST /api/coverage/{tool} 保留供导出）。
 *
 * 样式对标行业案例：
 *   - 覆盖矩阵：IBM DOORS / Polarion 追溯矩阵——关系类型着色（SATISFIES 绿 / ALLOCATED_TO 蓝
 *     / VERIFIED_BY 紫 / DERIVES 黄）、未覆盖红叉、覆盖率语义色（≥80 绿 ≥50 黄 <50 红）
 *   - 追溯链：Cameo Systems Modeler 链式呈现——需求→架构→验证三段节点，关系类型标注在边上
 *   - 缺项清单：Polarion/JIRA 风险分级——P1/P2/P3 徽章 + 左边框色 + 分类统计条，不得自动隐藏
 *   - 建模批次：CI/CD dashboard——基线→合并双条形 + Δ 徽章（降红升绿）
 * 全局作用域（非 module），内联 onclick 依赖全局函数名
 */
const COV_TOOLS = ['coverage_matrix', 'trace_chain_check', 'scene_coverage', 'gap_summary', 'modeling_coverage'];
const COV_TABS = { coverage_matrix: '覆盖矩阵', trace_chain_check: '追溯链', scene_coverage: '场景覆盖', gap_summary: '缺项汇总', modeling_coverage: '建模批次' };
/* 追溯关系 → 行业惯例着色（DOORS/Polarion 语义） */
const COV_REL_COLOR = { SATISFIES: '#18a058', ALLOCATED_TO: '#2f7fd1', VERIFIED_BY: '#7a5af5', DERIVES: '#d88c00' };
window._covLast = window._covLast || {};   // uid → 解析后的工具结果（预览大图入口用）
let _covUid = 0;

function covEsc(s) { return esc(String(s == null ? '' : s)); }

function covScopeBar(sc) {
  const bits = [];
  if (sc.project_id) bits.push(`工程 <b>${covEsc(sc.project_id)}</b>`);
  if (sc.branch || sc.baseline_branch) bits.push(`分支 <b>${covEsc(sc.branch || sc.baseline_branch)}</b>`);
  if (sc.batch_id) bits.push(`批次 <b>${covEsc(sc.batch_id)}</b>`);
  if (sc.rule_version) bits.push(`规则 <b>${covEsc(sc.rule_version)}</b>`);
  if (sc.entity_count != null) bits.push(`实体 ${sc.entity_count}`);
  if (sc.trace_relation_count != null) bits.push(`追溯关系 ${sc.trace_relation_count}`);
  return `<span style="display:inline-flex;gap:12px;flex-wrap:wrap;font-weight:400;">${bits.map(b => `<span>${b}</span>`).join('')}</span>`;
}

function covChip(text, color) {
  const c = color || 'var(--line)';
  return `<span style="display:inline-block;border:1px solid ${c};border-radius:20px;padding:1px 8px;font-size:11px;margin:1px 2px;">${covEsc(text)}</span>`;
}

function covRelChip(rel, name) {
  const c = COV_REL_COLOR[rel] || 'var(--line)';
  return `<span style="display:inline-block;border:1px solid ${c};color:${c};border-radius:6px;padding:1px 7px;font-size:11px;margin:1px 2px;" title="追溯关系：${covEsc(rel)}">${covEsc(name)}</span>`;
}

function covRate(pct) {
  if (pct == null) return '<b style="color:var(--mut)">—</b>';
  const v = Math.round(pct * 1000) / 10;
  const color = v >= 80 ? '#18a058' : (v >= 50 ? '#d88c00' : '#d03050');
  return `<b style="color:${color};font-size:20px;">${v}%</b>`;
}

/* ── 组件 1：覆盖矩阵（DOORS/Polarion 追溯矩阵风格） ── */
function covRenderMatrix(o) {
  const s = o.summary || {};
  const rows = (o.matrix || []).map(m => {
    const covered = (m.covered_by || []).length > 0;
    const chips = (m.covered_by || []).map(cb => covRelChip(cb.rel, cb.arch_name)).join('')
      || '<span style="color:#d03050;font-size:11px;">✗ 未覆盖</span>';
    return `<tr>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);font-size:12px;"><b>${covEsc(m.req.name)}</b><br><span style="color:var(--mut);font-size:10px;">${covEsc(m.req.type)}</span></td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);">${chips}</td>
      <td style="padding:5px 8px;border-bottom:1px solid var(--line);text-align:center;">${covered ? '<span style="color:#18a058;">✓</span>' : '<span style="color:#d03050;">✗</span>'}</td>
    </tr>`;
  }).join('');
  const legend = Object.entries(COV_REL_COLOR).map(([k, c]) =>
    `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${c};margin:0 3px 0 8px;"></span><span style="font-size:10px;">${k}</span>`).join('');
  const unc = (o.uncovered_requirements || []).map(u => covChip(u.name, '#d03050')).join('') || '无';
  const baseless = (o.baseless_arch_elements || []).map(u => covChip(u.name + '（' + u.entity_type + '）', '#d88c00')).join('') || '无';
  const abnormal = (o.abnormal_relations || []).map(u => covChip(u.kind + ': ' + u.detail, '#d03050')).join('') || '无';
  const dist = (o.arch_type_distribution || []).map(d => covChip(d.entity_type + ' ×' + d.count)).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;">
    <div class="panel" style="padding:10px;text-align:center;">需求总数<div>${s.requirement_total ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">已覆盖<div>${s.covered_requirements ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">覆盖率<div>${covRate(s.coverage_rate)}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">派生覆盖<div>${s.derive_only_requirements ?? 0}</div></div>
  </div>
  <div class="panel"><div class="ph">需求 × 架构 覆盖矩阵 <span style="flex:1"></span>${dist}</div>
  <div class="pb" style="padding:0;max-height:380px;overflow:auto;"><table style="width:100%;border-collapse:collapse;">
    <thead><tr style="background:rgba(127,163,204,.08);"><th style="padding:6px 8px;text-align:left;font-size:11px;">需求</th><th style="padding:6px 8px;text-align:left;font-size:11px;">覆盖关系（按架构元素）</th><th style="padding:6px 8px;font-size:11px;">状态</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
  <div style="padding:5px 10px;border-top:1px solid var(--line);">图例：${legend}</div></div>
  <div class="panel" style="margin-top:10px;"><div class="ph">三类异常</div><div class="pb" style="font-size:12px;line-height:2;">
    <div>🔴 未覆盖需求：${unc}</div><div>🟡 无需求依据的架构元素：${baseless}</div><div>🔴 异常追溯关系：${abnormal}</div>
  </div></div>`;
}

/* ── 组件 2：追溯链（Cameo 链式呈现风格） ── */
function covRenderChains(o) {
  const s = o.summary || {};
  const chains = (o.chains || []).map(c => {
    const arch = (c.sample_targets || []).find(t => t.via_rel === 'SATISFIES' || t.via_rel === 'ALLOCATED_TO');
    const verify = (c.sample_targets || []).filter(t => t.via_rel === 'VERIFIED_BY');
    const okArch = (c.reachable_arch_count || 0) > 0;
    const node = (txt, bad) => `<span style="border:1px solid ${bad ? '#d03050' : 'var(--line)'};${bad ? 'color:#d03050;border-style:dashed;' : ''}border-radius:8px;padding:3px 10px;font-size:11px;background:rgba(127,163,204,.06);">${txt}</span>`;
    const edge = (rel, bad) => `<span style="display:inline-flex;flex-direction:column;align-items:center;line-height:1;"><span style="font-size:9px;color:${bad ? '#d03050' : (COV_REL_COLOR[rel] || 'var(--mut)')};">${covEsc(rel || (bad ? '断链' : '—'))}</span><span style="color:${bad ? '#d03050' : 'var(--mut)'};font-size:11px;">→</span></span>`;
    const chain = [
      node('📋 ' + covEsc(c.req.name), false),
      edge(arch ? arch.via_rel : '', !okArch),
      node(okArch ? '🏗 ' + covEsc(arch ? arch.name : `架构 ×${c.reachable_arch_count}`) : '🏗 断链（无架构）', !okArch),
      edge(verify.length ? 'VERIFIED_BY' : '', !c.reached_verification),
      node(c.reached_verification ? ('🧪 ' + covEsc(verify.length ? verify[0].name : '已验证')) : '🧪 未到验证', !c.reached_verification),
    ].join(' ');
    return `<div style="border-bottom:1px solid var(--line);padding:8px 4px;">
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">${chain}
        <span style="margin-left:auto;font-size:10px;color:var(--mut);">深度 ${c.max_hops}</span></div>
      <div style="margin-top:4px;font-size:10px;color:var(--mut);">架构可达 ${c.reachable_arch_count} 个 ｜ 验证用例 ${verify.map(v => covEsc(v.name)).join('、') || '—'}</div>
    </div>`;
  }).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;">
    <div class="panel" style="padding:10px;text-align:center;">需求总数<div>${s.requirement_total ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">架构可达<div>${s.with_arch_link ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">验证率<div>${covRate(s.verification_rate)}</div></div>
  </div>
  <div class="panel"><div class="ph">端到端追溯链（需求 → 架构 → 验证）</div><div class="pb" style="padding:0;max-height:420px;overflow:auto;">${chains}</div></div>`;
}

/* ── 组件 3：场景覆盖 ── */
function covRenderScenes(o) {
  const s = o.summary || {};
  const cards = (o.scenes || []).map(sc => {
    const linked = sc.linked_total || 0;
    const acts = (sc.activities_states || []).slice(0, 6).map(a => covChip(a.name + '（' + a.rel + '）')).join('');
    return `<div class="panel" style="padding:10px;margin-bottom:8px;">
      <div style="display:flex;gap:8px;align-items:center;"><b style="font-size:13px;">🎬 ${covEsc(sc.scene.name)}</b>
        <span style="font-size:10px;color:var(--mut);">${covEsc(sc.scene.type)}</span>
        <span style="margin-left:auto;font-size:11px;color:${linked > 0 ? '#18a058' : '#d03050'};">${linked > 0 ? '关联 ' + linked + ' 项' : '⚠️ 孤立场景'}</span></div>
      <div style="margin-top:6px;font-size:11px;line-height:1.9;">${acts || '<span style="color:#d03050;">无活动/状态关联</span>'}</div>
    </div>`;
  }).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;">
    <div class="panel" style="padding:10px;text-align:center;">场景总数<div>${s.scene_total ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">有活动链<div>${s.with_activity_chain ?? '—'}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">孤立场景<div style="color:${(s.orphan_scenes || []).length ? '#d03050' : '#18a058'};">${(s.orphan_scenes || []).length}</div></div>
  </div>
  <div class="panel"><div class="ph">场景链路（活动 / 状态 / 参与对象）</div><div class="pb" style="max-height:420px;overflow:auto;">${cards}</div></div>`;
}

/* ── 组件 4：缺项汇总（Polarion/JIRA 风险分级风格） ── */
function covRenderGaps(o) {
  const s = o.summary || {};
  const riskMeta = { high: ['P1 高风险', '#d03050'], medium: ['P2 中风险', '#d88c00'], low: ['P3 低风险', 'var(--mut)'] };
  const byRisk = { high: [], medium: [], low: [] };
  (o.gaps || []).forEach(g => { (byRisk[g.risk] = byRisk[g.risk] || []).push(g); });
  const total = s.gap_total || (o.gaps || []).length || 1;
  const sections = Object.entries(riskMeta).map(([rk, [label, color]]) => {
    const items = (byRisk[rk] || []).map(g => `
      <div style="border:1px solid var(--line);border-left:3px solid ${color};border-radius:8px;padding:6px 10px;margin-bottom:6px;font-size:12px;">
        <b>${covEsc(g.name)}</b> <span style="color:var(--mut);font-size:10px;">${covEsc(g.category)}</span>
        <div style="color:var(--mut);font-size:11px;">${covEsc(g.detail)} ｜ ${covEsc(g.locate || '')}</div>
      </div>`).join('');
    const pct = Math.round(((byRisk[rk] || []).length / total) * 100);
    return `<div style="margin-bottom:12px;"><div style="font-size:12px;margin-bottom:6px;display:flex;align-items:center;gap:8px;">
      <span class="tool-status ${rk === 'high' ? 'err' : (rk === 'medium' ? 'w' : '')}" style="color:${color};border:1px solid ${color};border-radius:10px;padding:0 8px;font-size:10px;">${label}</span>
      <b>${(byRisk[rk] || []).length}</b><span style="flex:1;height:4px;background:rgba(127,163,204,.12);border-radius:2px;overflow:hidden;"><span style="display:block;width:${pct}%;height:100%;background:${color};"></span></span><span style="font-size:10px;color:var(--mut);">${pct}%</span></div>${items || '<span style="color:var(--mut);font-size:11px;">无</span>'}</div>`;
  }).join('');
  const cats = Object.entries(s.by_category || {}).map(([k, v]) => covChip(k + ' ×' + v)).join('');
  return `
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;">
    <div class="panel" style="padding:10px;text-align:center;">缺项总数<div style="color:${s.gap_total ? '#d03050' : '#18a058'};font-size:20px;"><b>${s.gap_total ?? '—'}</b></div></div>
    <div class="panel" style="padding:10px;text-align:center;">覆盖率<div>${covRate(s.coverage_rate)}</div></div>
    <div class="panel" style="padding:10px;text-align:center;">验证率<div>${covRate(s.verification_rate)}</div></div>
  </div>
  <div style="font-size:11px;color:var(--mut);margin-bottom:10px;">${cats}</div>
  <div class="panel"><div class="ph">缺项清单（按风险分级 · 不得自动隐藏）</div><div class="pb" style="max-height:420px;overflow:auto;">${sections}</div></div>`;
}

/* ── 组件 5：建模批次（CI/CD dashboard 风格） ── */
function covRenderModeling(o) {
  const b = o.batch || {}, c = o.coverage || {};
  const delta = c.coverage_rate_delta || 0;
  const bar = (label, rate) => `<div style="display:flex;align-items:center;gap:10px;margin:8px 0;">
    <span style="width:90px;font-size:12px;color:var(--mut);">${label}</span>
    <div style="flex:1;height:14px;background:rgba(127,163,204,.12);border-radius:7px;overflow:hidden;">
      <div style="width:${Math.round((rate || 0) * 100)}%;height:100%;background:${(rate || 0) >= 0.5 ? '#18a058' : '#d88c00'};"></div></div>
    <span style="width:64px;font-size:12px;">${rate == null ? '—' : Math.round(rate * 1000) / 10 + '%'}</span></div>`;
  const dBadge = `<span style="border:1px solid ${delta >= 0 ? '#18a058' : '#d03050'};color:${delta >= 0 ? '#18a058' : '#d03050'};border-radius:10px;padding:0 8px;font-size:11px;">Δ ${delta >= 0 ? '+' : ''}${Math.round(delta * 1000) / 10}%</span>`;
  const gaps = (o.modeling_gaps || []).map(g => `<div style="border:1px solid #d88c00;border-radius:8px;padding:6px 10px;margin-bottom:6px;font-size:12px;background:rgba(216,140,0,.06);">⚠️ ${covEsc(g)}</div>`).join('') || '无';
  const types = (b.entity_type_distribution || []).map(d => covChip(d.entity_type + ' ×' + d.count)).join('');
  const match = Object.entries(b.matching_distribution || {}).map(([k, v]) => covChip(k + ' ×' + v)).join('');
  return `
  <div class="panel" style="margin-bottom:10px;"><div class="ph">覆盖率对比（基线 → 并入本批次） ${dBadge}</div><div class="pb">
    ${bar('基线', (c.baseline || {}).coverage_rate)}
    ${bar('合并后', (c.merged || {}).coverage_rate)}
    <div style="font-size:11px;color:var(--mut);">需求 ${((c.baseline || {}).requirement_total ?? '—')} → ${((c.merged || {}).requirement_total ?? '—')} ｜
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

/* ══ 对话流 / 预览面板统一入口 ══ */

/* 卡片外壳：头（标题+scope+预览入口）+ 渲染体。wide=预览面板大图（隐藏预览入口、放开高度） */
function covCardHtml(name, o, wide) {
  const fn = COV_RENDER[name];
  if (!fn || !o || typeof o !== 'object' || !o.scope) return null;
  const uid = 'c' + (++_covUid);
  window._covLast[uid] = { name, o };
  const openBtn = wide ? '' :
    `<span style="cursor:pointer;border:1px solid var(--blue);color:var(--blue);border-radius:10px;padding:0 9px;font-size:10.5px;white-space:nowrap;" onclick="covOpenInPreview('${uid}')" title="在右侧预览面板查看大图">🔎 预览大图</span>`;
  return `<div class="panel" style="margin:6px 0;">
    <div class="ph"><span>📊 ${covEsc(COV_TABS[name] || name)}</span>
      <span style="flex:1"></span>${covScopeBar(o.scope || {})}<span style="flex:1"></span>${openBtn}</div>
    <div class="pb">${fn(o)}</div>
  </div>`;
}

/* 对话流工具结果 → 富组件。
 * 三分支：① JSON 完整 → 同步渲染；② JSON 被后端 _TOOL_RESULT_CAP(6000) 截断（parse 失败）→
 * 占位卡 + covHydrateAll 从 /api/coverage/{tool}（同一工具层、无 CAP）异步水合；
 * ③ 非覆盖工具 → null（调用方回退 JSON 文本展示）。
 * args：工具调用参数（project_id/branch 透传给端点，保证与 Agent 调用口径一致） */
function covTryCard(name, resultStr, args) {
  if (COV_TOOLS.indexOf(name) < 0) return null;
  if (typeof resultStr === 'string' && resultStr) {
    try {
      const o = JSON.parse(resultStr);
      return covCardHtml(name, o, false);
    } catch (e) { /* 落入占位水合 */ }
  }
  let attrs = '';
  try {
    if (args && typeof args === 'object' && Object.keys(args).length) {
      attrs = ' data-args="' + escA(JSON.stringify(args)) + '"';
    }
  } catch (e) { /* 无参 */ }
  // MutationObserver 会触发水合；setTimeout 兜底消息渲染完成前的时序
  setTimeout(covHydrateAll, 120);
  return `<div class="cov-pending panel" data-cov-tool="${covEsc(name)}"${attrs} style="margin:6px 0;padding:14px;border:1px dashed var(--line);border-radius:10px;color:var(--mut);font-size:12px;">📊 ${covEsc(COV_TABS[name] || name)}：结构化视图加载中…</div>`;
}

/* 扫描全部未水合占位卡（含历史消息里的），从 REST 端点拉全量结果渲染 */
async function covHydrateAll() {
  const pend = document.querySelectorAll('.cov-pending:not([data-done])');
  if (!pend.length) return;
  for (const el of pend) {
    el.setAttribute('data-done', '1');
    const tool = el.getAttribute('data-cov-tool');
    if (COV_TOOLS.indexOf(tool) < 0) { el.remove(); continue; }
    let qs = '';
    try {
      const args = JSON.parse(el.getAttribute('data-args') || '{}');
      const sp = new URLSearchParams();
      if (args.project_id) sp.set('project_id', args.project_id);
      if (args.branch) sp.set('branch', args.branch);
      qs = sp.toString();
    } catch (e) { /* 无参走默认口径 */ }
    try {
      const r = await api('/api/coverage/' + tool + (qs ? '?' + qs : ''));
      if (!r || r.ok !== true) {
        el.innerHTML = '⚠️ ' + covEsc((r && r.result) || '分析失败');
        el.style.borderStyle = 'solid';
        continue;
      }
      const o = typeof r.result === 'string' ? JSON.parse(r.result) : r.result;
      const html = covCardHtml(tool, o, false);
      if (html) { el.outerHTML = html; } else { el.remove(); }
    } catch (e) {
      el.innerHTML = '⚠️ 结构化视图加载失败：' + covEsc(e.message);
      el.style.borderStyle = 'solid';
    }
  }
}

/* 消息 DOM 变化时自动水合（历史消息加载 / 流式渲染均覆盖）；有 pending 才动作 */
let _covHydrateTimer = null;
new MutationObserver(() => {
  if (!document.querySelector('.cov-pending:not([data-done])')) return;
  if (_covHydrateTimer) return;
  _covHydrateTimer = setTimeout(() => { _covHydrateTimer = null; covHydrateAll(); }, 200);
}).observe(document.body, { childList: true, subtree: true });

/* 预览面板大图：写入 AI 建模页右侧 #preview-content 并展开面板 */
function covOpenInPreview(uid) {
  const rec = (window._covLast || {})[uid];
  const pp = document.getElementById('preview-panel');
  const body = document.getElementById('preview-content');
  const title = document.getElementById('preview-title');
  if (!pp || !body || !rec || !rec.o) { toast('预览面板不可用'); return; }
  const { name, o } = rec;
  window._pvCurrent = { title: '覆盖性分析 · ' + (COV_TABS[name] || name) };
  if (title) title.textContent = '📊 覆盖性分析 · ' + (COV_TABS[name] || name);
  body.innerHTML = covCardHtml(name, o, true) || '<div style="color:var(--mut);font-size:12px;">无法渲染该结果</div>';
  pp.classList.remove('collapsed');
  body.scrollTop = 0;
}
