/* 向量转图谱：候选审核 / 批量
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 7712-8175  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function v2gReviewLoad() {
  const el = document.getElementById('v2g-review-panel');
  if(!el) return;
  try {
    const [cands, batches] = await Promise.all([
      api('/api/knowledge/v2g/candidates?limit=3000'),
      api('/api/knowledge/v2g/batches'),
    ]);
    _v2g.cands = cands || [];
    _v2g.batches = batches || [];
    // 全局定位：文档管道 / 手动抽取设置的待定位批次（P2 文档→抽取闭环）
    if(window._v2gLocateBatch){
      const lb = window._v2gLocateBatch;
      if(_v2g.batches.some(b=>b.batch_id===lb)) _v2g.batch = lb;
      window._v2gLocateBatch = null;
    }
    // 已有实体名称集合（关系候选两端节点未入库提示用）
    try { const ents = await api('/api/knowledge/entities'); _v2g.entityNames = new Set((ents||[]).map(e=>e.name)); } catch(e) {}
    // LLM 语义护栏：候选载入后先做类型白名单校验（校正/拦截），再渲染
    await v2gRunGuardrails();
    v2gRenderBatchPanel();
    v2gRenderFilters();
    v2gRenderTable();
  } catch(e) { el.innerHTML = `<div style="padding:10px;color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
// ── LLM 语义护栏（Semantic Guardrails）：把 LLM 抽取候选的实体/关系类型约束到本体白名单 ──
// 白名单来源：/api/knowledge/ontology（ontology_types 表，type_kind=entity/relation/attribute）。
// 缓存于 _v2g.ontWL，避免每次加载重复拉取。实体候选查 entity 类型集合，关系候选查 relation 类型集合。
// 策略：①精确命中→放行；②归一/包含/最长公共子串近似→标记「已校正」，给出就近本体类型建议；
//       ③无近似→标记「已拦截」，提示改用本体已有类型或驳回，杜绝以未知类型静默入图污染。
async function v2gLoadOntWhiteList() {
  if(_v2g.ontWL) return _v2g.ontWL;
  try {
    const list = await api('/api/knowledge/ontology');
    const wl = { entity:[], relation:[], attribute:[], entitySet:new Set(), relationSet:new Set() };
    (Array.isArray(list)?list:[]).forEach(t=>{
      if(!t || !t.name) return;
      const k = t.type_kind==='relation' ? 'relation' : (t.type_kind==='attribute' ? 'attribute' : 'entity');
      wl[k].push(t.name);
      if(k==='entity') wl.entitySet.add(t.name);
      else if(k==='relation') wl.relationSet.add(t.name);
    });
    _v2g.ontWL = wl;
    return wl;
  } catch(e) { _v2g.ontWL = null; return null; }
}
function _grNorm(s){ return String(s||'').trim().toLowerCase().replace(/[\s()（）·\-—_《》<>「」【】,，]/g,''); }
function _grLcs(a,b){
  const dp=Array(a.length+1).fill(0).map(()=>Array(b.length+1).fill(0));
  let best=0;
  for(let i=1;i<=a.length;i++)for(let j=1;j<=b.length;j++){
    if(a[i-1]===b[j-1]){ dp[i][j]=dp[i-1][j-1]+1; if(dp[i][j]>best)best=dp[i][j]; }
  }
  return best;
}
// 单候选类型校验：返回 {status:'ok'|'corrected'|'blocked', from, to, reason}
function v2gGuardType(wl, typeStr, isRel){
  const t=(typeStr||'').trim();
  if(!t) return {status:'ok', from:t, to:t, reason:''};
  const set=isRel?wl.relationSet:wl.entitySet;
  const kinds=isRel?wl.relation:wl.entity;
  if(set.has(t)) return {status:'ok', from:t, to:t, reason:''};
  const nt=_grNorm(t);
  if(!kinds.length) return {status:'blocked', from:t, to:t, reason:'本体尚无可用类型'};
  for(const k of kinds) if(_grNorm(k)===nt) return {status:'corrected', from:t, to:k, reason:'归一校正：'+k};
  let best=null, bestScore=0;
  for(const k of kinds){
    const nk=_grNorm(k); if(!nk) continue;
    if(nk.indexOf(nt)>=0 || nt.indexOf(nk)>=0){ const s=Math.min(nt.length,nk.length); if(s>bestScore){bestScore=s;best=k;} continue; }
    const sc=_grLcs(nt,nk)/Math.min(nt.length,nk.length);
    if(sc>bestScore && sc>=0.6){ bestScore=sc; best=k; }
  }
  if(best) return {status:'corrected', from:t, to:best, reason:'近似映射：'+best};
  return {status:'blocked', from:t, to:t, reason:'本体无此类型且无近似'};
}
// 对候选池整体跑护栏，写回 _v2g.gr（enabled + 汇总计数 + Map<id,{...}>）
async function v2gRunGuardrails(){
  _v2g.gr={enabled:false, corrected:0, blocked:0, ok:0, items:new Map()};
  const wl=await v2gLoadOntWhiteList();
  if(!wl) return;
  _v2g.gr.enabled=true;
  (_v2g.cands||[]).forEach(c=>{
    const isRel=c.entity_type==='关系候选';
    const raw=isRel?c.rel_type:(c.entity_type||'');
    const r=v2gGuardType(wl, raw, isRel);
    if(r.status==='corrected') _v2g.gr.corrected++;
    else if(r.status==='blocked') _v2g.gr.blocked++;
    else _v2g.gr.ok++;
    _v2g.gr.items.set(c.id, r);
  });
}
// 行内护栏提示标签（展示于类型列附近；title 给出校正映射/拦截原因）
function v2gGuardTag(id){
  const gr=_v2g.gr;
  if(!gr || !gr.enabled) return '';
  const it=gr.items.get(id);
  if(!it || it.status==='ok') return '';
  if(it.status==='corrected')
    return `<span class="tag" style="border-color:var(--blue);color:var(--blue-d);font-size:10px;margin-left:4px;" title="LLM 候选类型「${esc(it.from)}」不在本体，已就近校正为「${esc(it.to)}」。确认前请复核或编辑修正">🛡 已校正</span>`;
  return `<span class="tag" style="border-color:var(--red);color:var(--red);font-size:10px;margin-left:4px;" title="LLM 候选类型「${esc(it.from)}」不在本体且无近似映射，已拦截。请改用本体已有类型或驳回，避免污染图谱">🛡 已拦截</span>`;
}
// 护栏汇总横幅（对齐既有候选汇总样式）
function v2gGuardBanner(){
  const gr=_v2g.gr;
  if(!gr || !gr.enabled) return '<div style="padding:6px 10px;font-size:11px;color:var(--mut);border-bottom:1px solid var(--line);">🛡 语义护栏未启用（本体类型列表不可用）</div>';
  if(gr.corrected+gr.blocked===0) return '<div style="padding:6px 10px;font-size:11px;color:var(--grn);border-bottom:1px solid var(--line);">🛡 语义护栏已启用：候选类型均在本体白名单内，未发现越界类型</div>';
  return `<div style="padding:6px 10px;font-size:11px;color:var(--mut);border-bottom:1px solid var(--line);">🛡 语义护栏：<span style="color:var(--blue-d);">已校正 ${gr.corrected}</span> · <span style="color:var(--red);">已拦截 ${gr.blocked}</span>（越界类型已就近校正或标记拦截，不下发入图）</div>`;
}
// 批次聚合计数（治理中心顶部摘要）
function v2gBatchCounts() {
  const b = _v2g.batches || [];
  return {
    total: b.length,
    pending: b.reduce((s,x)=>s+x.pending_n,0),
    confirmed: b.reduce((s,x)=>s+x.confirmed_n,0),
    rejected: b.reduce((s,x)=>s+x.rejected_n,0),
  };
}
// 上传批次面板（P2：任务式批次卡 + 状态过滤 + 搜索 + 时间范围 + 清理，对齐行业批次任务管理）
function v2gRenderBatchPanel() {
  const bs = (_v2g.batches || []).filter(b=>{
    if(_v2g.batchStatus!=='all'){
      const n = _v2g.batchStatus==='pending' ? b.pending_n : _v2g.batchStatus==='confirmed' ? b.confirmed_n : b.rejected_n;
      if(!n) return false;
    }
    if(_v2g.batchTime){
      const days = parseInt(_v2g.batchTime);
      const cutoff = Date.now() - days*86400*1000;
      const t = new Date((b.last_at||'').replace(' ','T'));
      if(isNaN(t.getTime()) || t.getTime() < cutoff) return false;
    }
    if(_v2g.batchKw){
      const kw = _v2g.batchKw.toLowerCase();
      const doc = (b.source_docs||[]).join(' ').toLowerCase();
      if(!(doc.includes(kw) || b.batch_id.toLowerCase().includes(kw))) return false;
    }
    return true;
  });
  const counts = v2gBatchCounts();
  const bc = document.getElementById('v2g-batch-count');
  if(bc) bc.textContent = `共 ${counts.total} 批 · 待审 ${counts.pending} · 已确认 ${counts.confirmed} · 已驳回 ${counts.rejected}`;
  const statusEl = document.getElementById('v2g-batch-status');
  if(statusEl){
    const statuses = [['all','全部'],['pending','待审'],['confirmed','已确认'],['rejected','已驳回']];
    statusEl.innerHTML = statuses.map(([k,l])=>`<span class="st ${_v2g.batchStatus===k?'b':'g'}" style="cursor:pointer;font-size:10.5px;${_v2g.batchStatus===k?'':'opacity:.7;'}" onclick="v2gSetBatchStatus('${k}')">${l}</span>`).join('');
  }
  const el = document.getElementById('v2g-batches');
  if(!el) return;
  // 方案A：批次卡片默认折叠（区域收敛为一行工具栏），可展开/收起
  el.style.display = _v2g.batchesExpanded ? 'flex' : 'none';
  const tg = document.getElementById('v2g-batch-toggle');
  if(tg) tg.innerHTML = _v2g.batchesExpanded ? '▾ 收起批次' : `▸ 展开批次${counts.pending>0?`（待审 ${counts.pending}）`:''}`;
  // 高级搜索折叠时，按钮带批次待办提示（批次区在高级搜索内）
  const advTg = document.getElementById('v2g-adv-toggle');
  if(advTg && !_v2g.advExpanded) advTg.innerHTML = `▸ 高级搜索${counts.pending>0?`（待审 ${counts.pending}）`:''}`;
  const card = (bid, inner) => `<div onclick="v2gSetBatch('${bid}')" style="flex:none;min-width:205px;max-width:240px;border:1px solid ${_v2g.batch===bid?'var(--blue)':'var(--line)'};background:${_v2g.batch===bid?'var(--blue-l)':'#fff'};border-radius:8px;padding:7px 10px;cursor:pointer;" title="点击按此批次过滤候选">${inner}</div>`;
  const cards = bs.map(b=>{
    const canClear = b.pending_n===0;
    return card(b.batch_id, `
    <div style="display:flex;gap:6px;align-items:center;">
      <span style="font-size:14px;">📄</span>
      <div style="flex:1;min-width:0;">
        <div style="font-size:11.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc((b.source_docs||[]).join('、'))}">${esc((b.source_docs&&b.source_docs[0])||'手动抽取')}</div>
        <div style="font-size:10px;color:var(--mut);">${esc(b.batch_id.slice(-6))} · ${esc((b.last_at||'').slice(5,16).replace('T',' '))}</div>
      </div>
      ${v2gReflowTag(b)}
      ${b.pending_n>0?`<span class="st w" style="font-size:10px;">待${b.pending_n}</span>`:''}
      ${canClear?`<span class="st r" style="font-size:10px;cursor:pointer;" title="清理该批次候选记录（不影响已入库实体）" onclick="event.stopPropagation();v2gClearBatch('${b.batch_id}')">🗑 清理</span>`:''}
    </div>
    <div style="font-size:10.5px;color:var(--mut);margin-top:4px;">实体${b.node_n}/关系${b.rel_n} · <span style="color:var(--grn);">✓${b.confirmed_n}</span> <span style="color:var(--red);">✗${b.rejected_n}</span></div>`);
  });
  el.innerHTML = bs.length ? cards.join('') : '<span style="font-size:11px;color:var(--mut);padding:4px 2px;">暂无匹配批次</span>';
}
function v2gSetBatchStatus(s) { _v2g.batchStatus = s; v2gRenderBatchPanel(); }
function v2gSetBatchTime() { _v2g.batchTime = document.getElementById('v2g-batch-time').value; v2gRenderBatchPanel(); }
function v2gBatchSearch() { _v2g.batchKw = (document.getElementById('v2g-batch-search').value||'').trim(); v2gRenderBatchPanel(); }
// 方案A：展开/收起批次卡片区（默认折叠，只留工具栏一行）
function v2gToggleBatches() { _v2g.batchesExpanded = !_v2g.batchesExpanded; v2gRenderBatchPanel(); }
// 高级搜索（批次筛选 + 类型筛选）折叠切换
function v2gToggleAdv() {
  _v2g.advExpanded = !_v2g.advExpanded;
  const adv = document.getElementById('v2g-adv');
  const tg = document.getElementById('v2g-adv-toggle');
  if(adv) adv.style.display = _v2g.advExpanded ? 'block' : 'none';
  if(tg) tg.innerHTML = _v2g.advExpanded ? '▾ 收起高级' : '▸ 高级搜索';
}
// FR-KG-12 补 G13：回流批次徽章——batch_id 以 reflow- 开头，或批次内 source_doc 为
// conv-/impact-/review- 前缀（对话/影响分析/模型评审自动回流）→ 显示「🔄 回流」+ 来源标签
function v2gReflowTag(b) {
  const srcs = (b.source_docs||[]).filter(Boolean).map(String);
  const isReflow = String(b.batch_id||'').startsWith('reflow-') || srcs.some(d=>/^(conv|impact|review)-/.test(d));
  if(!isReflow) return '';
  const labelMap = [['conv-','对话'],['impact-','影响分析'],['review-','模型评审']];
  const labels = labelMap.filter(([pre])=>srcs.some(d=>d.startsWith(pre))).map(x=>x[1]);
  const srcHtml = labels.length ? `<span style="font-size:9.5px;color:var(--mut);margin-left:2px;">${labels.join('/')}</span>` : '';
  return `<span class="st w" style="font-size:10px;cursor:default;white-space:nowrap;" title="AI 建模/对话自动回流的知识候选">🔄 回流</span>${srcHtml}`;
}
// P3 批次清理：仅无待审批次的清理（删除候选记录，已入库实体/溯源不受影响，审计留痕）
async function v2gClearBatch(bid) {
  if(!(await confirmDialog(`清理批次 ${bid.slice(0,12)}… 的全部候选记录？\n（不影响已入库实体与 chunk 溯源，删除后不可恢复，操作留痕）`, {title:'清理批次', okText:'清理'}))) return;
  const r = await api(`/api/knowledge/v2g/batches/${encodeURIComponent(bid)}/clear`, {method:'POST'});
  if(r.error) { toast('清理失败：'+r.error); return; }
  toast(`🗑 已清理批次，删除 ${r.deleted} 条候选`);
  if(_v2g.batch===bid) _v2g.batch='';
  v2gReviewLoad();
}
function v2gSetBatch(b) { _v2g.batch = b; _v2g.page = 1; v2gRenderBatchPanel(); v2gRenderTable(); }
// 筛选器渲染（P2：类型多选 chips 带计数；来源维度由批次筛选覆盖，已移除来源下拉）
function v2gRenderFilters() {
  const typeCounts = {};
  _v2g.cands.forEach(c=>{ const t=c.entity_type||'未分类'; typeCounts[t]=(typeCounts[t]||0)+1; });
  const el = document.getElementById('v2g-type-chips');
  if(el){
    const entries = Object.entries(typeCounts).sort((a,b)=>b[1]-a[1]);
    el.innerHTML = '<span style="font-size:11px;color:var(--mut);flex:none;align-self:center;">类型：</span>' +
      entries.map(([t,n])=>{
        const on = _v2g.types.includes(t);
        return `<span class="tag" style="cursor:pointer;font-size:11px;${on?'background:var(--blue-l);color:var(--blue-d);border-color:var(--blue);':''}" onclick="v2gToggleType('${esc(t)}')">${esc(t)} <small style="opacity:.7;">${n}</small></span>`;
      }).join('') || '<span style="font-size:11px;color:var(--mut);">无类型</span>';
  }
}
function v2gToggleType(t) {
  const i = _v2g.types.indexOf(t);
  if(i>=0) _v2g.types.splice(i,1); else _v2g.types.push(t);
  _v2g.page = 1;
  v2gRenderFilters();
  v2gRenderTable();
}
function v2gApplyFilter() {
  _v2g.kw = (document.getElementById('v2g-search').value||'').trim();
  _v2g.status = document.getElementById('v2g-status').value;
  _v2g.matching = document.getElementById('v2g-matching').value;
  _v2g.confMin = parseFloat(document.getElementById('v2g-conf-min').value) || 0;
  _v2g.relOnly = document.getElementById('v2g-rel-only').checked;
  _v2g.rejOnly = document.getElementById('v2g-rej-only').checked;
  const grpEl = document.getElementById('v2g-group');
  if(grpEl) _v2g.group = grpEl.value;
  _v2g.page = 1;
  v2gRenderTable();
}
function v2gFiltered() {
  return _v2g.cands.filter(c=>{
    if(_v2g.batch && c.batch_id!==_v2g.batch) return false;
    // P0-1：实体/关系分组视图（关系候选 = entity_type 为「关系候选」）
    if(_v2g.group==='entity' && c.entity_type==='关系候选') return false;
    if(_v2g.group==='relation' && c.entity_type!=='关系候选') return false;
    // 「只看被拒」时忽略状态下拉（状态下拉默认「待审」，会拦截被拒候选）
    if(!_v2g.rejOnly && _v2g.status!=='' && c.status!==_v2g.status) return false;
    if(_v2g.relOnly && c.entity_type!=='关系候选') return false;
    if(_v2g.rejOnly && c.status!=='rejected') return false;
    if(_v2g.types.length && !_v2g.types.includes(c.entity_type)) return false;
    if(_v2g.matching && (c.matching_status||'none')!==_v2g.matching) return false;
    if(_v2g.confMin>0 && parseFloat(c.confidence||0) < _v2g.confMin) return false;
    if(_v2g.kw){ const kw=_v2g.kw.toLowerCase(); const n=(c.entity_name||'').toLowerCase(); const s=(c.rel_source||'').toLowerCase(); const t=(c.rel_target||'').toLowerCase(); if(!(n.includes(kw)||s.includes(kw)||t.includes(kw))) return false; }
    return true;
  });
}
// 关系候选展示：源 —[关系]→ 目标；两端节点未入库时黄色警示（P1 关系候选引导）
function v2gRelDisp(c) {
  const parts = (c.entity_name||'').split('--');
  const s = c.rel_source || parts[0] || '';
  const r = c.rel_type || parts[1] || '';
  const t = c.rel_target || parts.slice(2).join('--') || '';
  const sIn = s && _v2g.entityNames.has(s);
  const tIn = t && _v2g.entityNames.has(t);
  const warn = (s && !sIn) || (t && !tIn)
    ? `<span class="tag" style="border-color:var(--amb);color:var(--amb);font-size:10px;margin-left:4px;" title="先确认两端实体节点入库，关系建边才可用">⚠ 两端节点未入库</span>` : '';
  return `<span>${esc(s)} <span style="color:var(--amb);font-weight:600;">—[${esc(r)}]→</span> ${esc(t)}</span>${warn}`;
}
// 消歧匹配状态（P1：抽取时与已有图谱比对打标；P0-A 关系候选同样打标）
function v2gMatchingTag(c) {
  if(c.entity_type==='关系候选'){
    const rms = c.rel_matching_status||'none';
    if(rms==='none') return '<span style="font-size:11px;color:var(--grn);">无重复</span>';
    return `<span class="tag" style="border-color:var(--red);color:var(--red);font-size:10px;" title="与已有关系 #${esc(c.rel_match_rel_id||'')} 同三元组，建议合并或跳过">⚠ 关系重复</span>`;
  }
  const ms = c.matching_status||'none';
  if(ms==='none') return '<span style="font-size:11px;color:var(--grn);">无重复</span>';
  if(ms==='dup_suspect') return `<span class="tag" style="border-color:var(--amb);color:var(--amb);font-size:10px;" title="与 ${esc(c.match_entity_id||'')} 疑似重复，请确认是否合并">⚠ 疑似重复</span>`;
  return `<span class="tag" style="border-color:var(--red);color:var(--red);font-size:10px;" title="与 ${esc(c.match_entity_id||'')} 高度重复，建议驳回或合并">⚠ 高度重复</span>`;
}
function v2gStatusTag(c) {
  const m = {pending:['w','待审'], confirmed:['ok','已确认'], rejected:['r','已驳回']};
  const t = m[c.status]||['w',c.status];
  const reason = (c.status==='rejected' && c.reject_reason)
    ? `<div style="font-size:10px;color:var(--mut);" title="驳回原因：${esc(c.reject_reason)}">${esc(c.reject_reason.slice(0,20))}</div>` : '';
  return `<span class="st ${t[0]}">${t[1]}</span>${reason}`;
}
function v2gRenderTable() {
  const el = document.getElementById('v2g-review-panel');
  if(!el) return;
  const all = v2gFiltered();
  const total = all.length;
  const size = _v2g.size;
  const pages = Math.max(1, Math.ceil(total/size));
  if(_v2g.page > pages) _v2g.page = pages;
  const rows = all.slice((_v2g.page-1)*size, _v2g.page*size);
  const cnt = {pending:0, confirmed:0, rejected:0};
  all.forEach(c=>{ if(cnt[c.status]!=null) cnt[c.status]++; });
  const sum = document.getElementById('v2g-summary');
  if(sum) sum.innerHTML = `筛选后 <b>${total}</b> 条 · <span style="color:var(--amb);">待审 ${cnt.pending}</span> / <span style="color:var(--grn);">已确认 ${cnt.confirmed}</span> / <span style="color:var(--red);">已驳回 ${cnt.rejected}</span>`;
  if(!all.length) {
    el.innerHTML = '<div style="padding:12px;color:var(--mut);font-size:12px;">暂无匹配候选（上传文档自动抽取，或「数据看板→向量图谱转化」手动抽取）</div>';
    return;
  }
  const batchBar = `<div style="padding:6px 8px;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line);font-size:11.5px;">
      <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" id="v2g-check-all" onchange="v2gToggleAll(this)"> 全选</label>
      <button class="btn sm" onclick="v2gReviewConfirm()">✅ 确认（选中）</button>
      <button class="btn sm ghost" onclick="v2gReviewReject()">🚫 批量驳回（选中）</button>
      <span style="flex:1"></span>
    </div>`;
  const body = rows.map(c=>{
    const isRel = c.entity_type==='关系候选';
    const disp = isRel ? v2gRelDisp(c) : `<b>${esc(c.entity_name)}</b>`;
    const typeTag = (isRel
      ? '<span class="tag" style="border-color:var(--amb);color:var(--amb);">关系（建边）</span>'
      : `<span class="tag">${esc(c.entity_type)}</span>`) + v2gGuardTag(c.id);
    const conf = (c.confidence!=null && c.confidence!=='') ? parseFloat(c.confidence||0).toFixed(2) : '-';
    const confTag = `<span style="font-size:11px;${parseFloat(conf)>=0.85?'color:var(--grn);':parseFloat(conf)>=0.7?'color:var(--blue-d);':'color:var(--amb);'}">${conf}</span>`;
    const src = c.source_doc
      ? `<span style="cursor:pointer;color:var(--blue-d);font-size:11px;" onclick="v2gViewSource(${c.id})" title="查看来源片段">📄 ${esc((c.source_doc||'').slice(0,14))}</span>`
      : '<span style="font-size:11px;color:var(--mut);">手动</span>';
    const errHint = c.errors ? `<div style="font-size:10.5px;color:var(--red);">${esc(c.errors)}</div>` : '';
    // P0-2：候选行内联证据（命中原文片段 + 来源类别），点击可打开完整来源面板
    const mention = c.chunk_content
      ? `<div onclick="event.stopPropagation();v2gViewSource(${c.id})" title="点击查看完整来源片段" style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10.5px;color:var(--mut);background:#f8fafc;border:1px solid var(--line);border-radius:5px;padding:2px 6px;margin-top:3px;cursor:pointer;">${esc(c.chunk_content)}</div>`
      : '';
    const srcType = /^(SYSM|ai_)/i.test(String(c.batch_id||'')) ? 'AI建模' : (/(reflow|conv|impact|review)-/i.test(String(c.batch_id||'')) ? '智能回流' : '抽取');
    const ops = c.status==='pending'
      ? `<button class="btn sm grn" onclick="v2gConfirmOne(${c.id})">✅ 确认</button>
         <button class="btn sm ghost" onclick="v2gViewSource(${c.id})">📄 来源</button>
         <button class="btn sm ghost" onclick="v2gEditCandidate(${c.id})">✏️ 编辑</button>
         <button class="btn sm red" onclick="v2gRejectOne(${c.id})">🚫 驳回</button>`
      : `<button class="btn sm ghost" onclick="v2gViewSource(${c.id})">📄 来源</button>`;
    return `<tr>
      <td>${c.status==='pending'?`<input type="checkbox" class="v2g-review-check" value="${c.id}">`:'-'}</td>
      <td style="font-size:12.5px;">${disp}${mention}${errHint}</td>
      <td>${typeTag}</td>
      <td>${confTag}</td>
      <td>${v2gMatchingTag(c)}</td>
      <td>${src}${srcType?`<span class="tag" style="font-size:9.5px;margin-left:4px;opacity:.85;">${srcType}</span>`:''}</td>
      <td>${v2gStatusTag(c)}</td>
      <td style="white-space:nowrap;">${ops}</td>
    </tr>`;
  }).join('');
  const foot = `<div style="padding:8px 10px;display:flex;gap:8px;align-items:center;font-size:11px;color:var(--mut);flex-wrap:wrap;">
      <span>✅ 确认后生成待审三元组（与已有实体重复时先选处理方式：对齐合并/强制新建/跳过）；经「三元组审核」通过并 commit 落图后写入图谱实体，分支发布见「发布」站。</span>
      <span style="flex:1"></span>
      <div id="v2g-pager" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;"></div>
    </div>`;
  el.innerHTML = batchBar + v2gGuardBanner() + '<table class="t"><tr><th>选</th><th>候选</th><th>类型</th><th>置信度</th><th>消歧</th><th>来源</th><th>状态</th><th>操作</th></tr>' + body + '</table>' + foot;
  renderPagerBar({
    el: document.getElementById('v2g-pager'), total, page: _v2g.page, size,
    onPage: p => { _v2g.page = p; v2gRenderTable(); },
    onSize: s => { _v2g.size = s; _v2g.page = 1; v2gRenderTable(); }
  });
}
// S4：批量操作——全选 / 批量驳回（带原因留痕）
function v2gToggleAll(cb) {
  document.querySelectorAll('.v2g-review-check').forEach(x=>{ x.checked = cb.checked; });
  if(cb.checked) toast(`已全选 ${document.querySelectorAll('.v2g-review-check:checked').length} 条`);
}
async function v2gReviewReject() {
  const ids = Array.from(document.querySelectorAll('.v2g-review-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要驳回的候选'); return; }
  const reason = await promptDialog({title:'批量驳回', message:`驳回 ${ids.length} 条候选，请填写原因（留痕便于追溯）：`, value:'', placeholder:'必须填写，如：重复 / 信息不完整 / 超出本体范围…', multiline:true, okText:'驳回'});
  if(reason===null) return;
  if(!String(reason).trim()){ toast('驳回必须填写原因'); return; }
  const r = await api('/api/knowledge/v2g/reject', {method:'POST', body:JSON.stringify({candidate_ids:ids, reason})});
  toast(`🚫 已驳回 ${r.rejected||0} 条`);
  v2gReviewLoad();
}
// P0-C/P0-A：消歧确认弹窗——重复候选清单（实体/关系）+ 处理动作选择（轻量居中卡片 #lbx）
// dups: [{name, match_id, level, match_kind}]；match_kind='relation' 表示与已有关系重复
// 返回 Promise<'skip'|'align'|'create'|null（取消）>
// 2026-09-22：消歧弹窗等处 id→名称 映射（一次拉取缓存；用户视角只见名称不见编码）
window._v2gEntNames = null;
async function v2gEntNameMap(){
  if(window._v2gEntNames) return window._v2gEntNames;
  try{
    const arr = await api('/api/knowledge/entities?limit=3000').catch(()=>[]) || [];
    const m = {}; (Array.isArray(arr)?arr:[]).forEach(e=>{ if(e && e.id) m[e.id] = e.name || e.id; });
    window._v2gEntNames = m; return m;
  }catch(e){ return {}; }
}
function v2gDupDialog(dups) {
  return (async () => {
  const _names = await v2gEntNameMap();
  const lbx = document.getElementById('lbx');
  const b = document.getElementById('lbx-body');
  const rows = (dups||[]).map((d,i)=>{
    const isRel = d.match_kind==='relation';
    const lv = d.level==='dup_high' ? '高度重复' : (isRel ? '关系重复' : '疑似重复');
    const lc = d.level==='dup_high' ? 'var(--red);border-color:var(--red);' : 'var(--amb);border-color:var(--amb);';
    const targetTxt = isRel
      ? `已有关系 <span style="color:var(--blue-d);">#${esc(d.match_id)}</span>`
      : `已有实体 <span style="color:var(--blue-d);">${esc(_names[d.match_id] || d.match_id)}</span>`;
    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;background:#fff;">
      <span style="font-size:12px;color:var(--mut);">${i+1}.</span>
      <b style="font-size:12.5px;flex:1;">${esc(d.name)}</b>
      <span class="tag" style="font-size:10px;color:${lc}">${lv}</span>
      <span style="font-size:11px;color:var(--mut);">${targetTxt}</span>
    </div>`;
  }).join('');
  b.innerHTML = `<h3>⚠ 候选与已有实体/关系重复（${(dups||[]).length}）</h3>
    <div style="font-size:12px;color:var(--mut);margin:4px 0 8px;">以下候选与已有知识重复，请选择处理方式后再确认：</div>
    <div style="max-height:170px;overflow:auto;">${rows}</div>
    <div style="margin:10px 0 4px;font-size:12px;font-weight:600;">处理方式</div>
    <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;cursor:pointer;"><input type="radio" name="dup-action" value="align" checked><div><div style="font-size:12.5px;">🔗 对齐合并到已有实体/关系</div><div style="font-size:11px;color:var(--mut);">不新建节点/边，文档溯源指向已存在对象（推荐）</div></div></label>
    <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;cursor:pointer;"><input type="radio" name="dup-action" value="create"><div><div style="font-size:12.5px;">➕ 强制新建独立实体/关系</div><div style="font-size:11px;color:var(--mut);">可能造成重复，后续需消歧治理</div></div></label>
    <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;cursor:pointer;"><input type="radio" name="dup-action" value="skip"><div><div style="font-size:12.5px;">⏭ 跳过该候选</div><div style="font-size:11px;color:var(--mut);">不进入管线，标记驳回（消歧跳过）留痕</div></div></label>
    <div class="lbx-actions"><button class="btn ghost" id="vd-cancel">取消</button><button class="btn" id="vd-ok">确定</button></div>`;
  lbx.classList.add('show');
  return new Promise(resolve => {
    const done = v => { lbx.classList.remove('show'); document.removeEventListener('keydown', _kd); resolve(v); };
    const pick = () => { const el = document.querySelector('input[name="dup-action"]:checked'); return el ? el.value : 'align'; };
    document.getElementById('vd-ok').onclick = () => done(pick());
    document.getElementById('vd-cancel').onclick = () => done(null);
    const _kd = e => {
      if(e.key === 'Enter') done(pick());
      else if(e.key === 'Escape') done(null);
    };
    document.addEventListener('keydown', _kd);
    setTimeout(()=>{ const ok = document.getElementById('vd-ok'); if(ok) ok.focus(); }, 30);
    lbx.onclick = e => { if(e.target===lbx) done(null); };
    const cb = document.querySelector('#lbx .lbx-close');
    if(cb) cb.onclick = () => done(null);
  });
})();
}
async function v2gReviewConfirm() {
  if(!branchWritable()) return;
  const ids = Array.from(document.querySelectorAll('.v2g-review-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请先勾选要确认的候选'); return; }
  // P0-C/P0-A：筛选与已有实体/关系重复的候选 → 需先选处理动作（消歧前移）
  const dups = _v2g.cands.filter(c=>ids.includes(c.id) && (
    (c.entity_type!=='关系候选' && (c.matching_status||'none')!=='none' && c.match_entity_id) ||
    (c.entity_type==='关系候选' && (c.rel_matching_status||'none')!=='none' && c.rel_match_rel_id)
  )).map(c=> c.entity_type==='关系候选'
    ? {name:`${c.rel_source||''} —[${c.rel_type||''}]→ ${c.rel_target||''}`, match_id:c.rel_match_rel_id, match_kind:'relation'}
    : {name:c.entity_name, match_id:c.match_entity_id, level:c.matching_status||'dup_suspect', match_kind:'entity'});
  let dupAction = 'create';
  if(dups.length){
    const a = await v2gDupDialog(dups);
    if(a===null) return; // 取消
    dupAction = a;
  } else if(!(await confirmDialog(`确认这 ${ids.length} 条候选？（生成待审三元组 —— 经「三元组审核」通过并发布后写入图谱实体）`))) return;
  // S7修复：直接按勾选的候选 id 确认（支持跨批次），不再依赖 batch_id 整批
  const r = await api('/api/knowledge/v2g/confirm', {method:'POST', body:JSON.stringify({selected_ids:ids, dup_action:dupAction})});
  const rej = r.rejected || []; const al = r.aligned || []; const sk = r.skipped || [];
  const parts = [`✅ 已确认 ${r.confirmed} 条（生成待审三元组）`];
  if(al.length) parts.push(`🔗 对齐 ${al.length} 条`);
  if(sk.length) parts.push(`⏭ 跳过 ${sk.length} 条`);
  if(rej.length) parts.push(`❌ 拒绝 ${rej.length} 条`);
  toast(parts.join('；') + (rej.length ? `（${rej.slice(0,2).map(x=>x.errors||'').join('；')}）` : ''));
  v2gReviewLoad();
  loadReviewQueue();
  loadRelReview();
  loadDuplicates();
}
// 单条操作（P0/P0-A：单条确认 / 单条驳回带原因）
async function v2gConfirmOne(id) {
  if(!branchWritable()) return;
  const c = _v2g.cands.find(x=>x.id===id);
  let dupAction = 'create';
  const isRelDup = c && c.entity_type==='关系候选' && (c.rel_matching_status||'none')!=='none' && c.rel_match_rel_id;
  const isEntDup = c && c.entity_type!=='关系候选' && (c.matching_status||'none')!=='none' && c.match_entity_id;
  if(isEntDup || isRelDup){
    const d = isRelDup
      ? [{name:`${c.rel_source||''} —[${c.rel_type||''}]→ ${c.rel_target||''}`, match_id:c.rel_match_rel_id, match_kind:'relation'}]
      : [{name:c.entity_name, match_id:c.match_entity_id, level:c.matching_status||'dup_suspect', match_kind:'entity'}];
    const a = await v2gDupDialog(d);
    if(a===null) return;
    dupAction = a;
  } else if(!(await confirmDialog('确认该候选？（生成待审三元组 —— 经「三元组审核」通过并发布后写入图谱实体）'))) return;
  const r = await api('/api/knowledge/v2g/confirm', {method:'POST', body:JSON.stringify({selected_ids:[id], dup_action:dupAction})});
  const al = r.aligned || []; const sk = r.skipped || [];
  let msg;
  if(al.length) msg = al[0].entity_id
    ? `🔗 已对齐合并到已有实体 ${al[0].entity_id}`
    : `🔗 已对齐复用已有关系 #${al[0].relation_id}`;
  else if(sk.length) msg = '⏭ 已跳过该候选（消歧跳过）';
  else msg = r.rejected && r.rejected.length ? `❌ 确认拒绝：${(r.rejected[0].errors||'').slice(0,80)}` : '✅ 已确认（生成待审三元组，经「三元组审核」发布后入图）';
  toast(msg);
  v2gReviewLoad();
  loadReviewQueue();
  loadRelReview();
}
async function v2gRejectOne(id) {
  if(!branchWritable()) return;
  const c = _v2g.cands.find(x=>x.id===id);
  const name = c ? (c.entity_type==='关系候选' ? `「${c.rel_source||''} —[${c.rel_type||''}]→ ${c.rel_target||''}」` : `「${c.entity_name}」`) : '';
  const reason = await promptDialog({title:'驳回候选', message:`驳回 ${name}，请填写原因（留痕便于追溯）：`, value:'', placeholder:'必须填写，如：重复 / 信息不完整 / 超出本体范围…', multiline:true, okText:'驳回'});
  if(reason===null) return;
  if(!String(reason).trim()){ toast('驳回必须填写原因'); return; }
  const r = await api('/api/knowledge/v2g/reject', {method:'POST', body:JSON.stringify({candidate_ids:[id], reason})});
  toast(`🚫 已驳回 1 条`);
  v2gReviewLoad();
}
// 来源溯源：右侧详情面板（chunk 原文 + 属性 + 已链接实体）
async function v2gViewSource(id) {
  let c = _v2g.cands.find(x=>x.id===id);
  if(!c) { toast('候选不存在'); return; }
  const isRel = c.entity_type==='关系候选';
  let linkedHtml = '';
  let chunkHtml = '';
  if(c.chunk_id) {
    try {
      const linked = await api(`/api/knowledge/chunks/${c.chunk_id}/linked`);
      if(linked && linked.length) linkedHtml = '<div style="font-size:11px;color:var(--grn);margin:6px 0;">🔗 已确认关联实体：' + linked.map(e=>`<span class="tag" style="font-size:10px;">${esc(e.name)}</span>`).join(' ') + '</div>';
    } catch(e) {}
    chunkHtml = c.chunk_content
      ? `<div style="margin-top:8px;"><b style="font-size:11.5px;color:var(--blue-d);">📄 来源片段（chunk #${c.chunk_id}）</b><div style="font-size:12px;line-height:1.7;background:#f8fafc;border:1px solid var(--line);border-radius:6px;padding:8px;margin-top:4px;max-height:220px;overflow:auto;white-space:pre-wrap;">${esc(c.chunk_content)}</div></div>`
      : '<div style="font-size:11px;color:var(--mut);margin-top:6px;">（该候选未绑定 chunk 原文）</div>';
  }
  const title = document.getElementById('panel-title');
  if(title) title.textContent = isRel ? '🔗 关系候选来源' : '📄 候选来源';
  document.getElementById('panel-body').innerHTML =
    `<div style="font-size:12.5px;">${isRel
      ? `<b>${esc(c.rel_source||'')}</b> <span class="st b">${esc(c.rel_type||'')}</span> <b>${esc(c.rel_target||'')}</b>`
      : `<b>${esc(c.entity_name)}</b> <span class="tag">${esc(c.entity_type)}</span>`}</div>
     <div style="font-size:11px;color:var(--mut);margin-top:5px;">批次：${esc(c.batch_id||'')} · 来源文档：${esc(c.source_doc||'手动抽取')} · 创建：${esc(c.created_at||'')}</div>
     <div style="font-size:11px;color:var(--mut);margin:4px 0;">置信度：${(parseFloat(c.confidence||0)).toFixed(2)} · 消歧：${v2gMatchingTag(c)} · 状态：${c.status}${c.reject_reason?' · 驳回原因：'+esc(c.reject_reason):''}</div>
     <div style="font-size:11px;color:var(--mut);margin:4px 0;">属性：${esc(c.properties||'{}')}</div>
     ${linkedHtml}
     ${chunkHtml}`;
  document.getElementById('panel-detail').classList.add('open');
  document.getElementById('overlay').style.display = 'block';
}
// 编辑候选：右侧详情面板表单（改名称/类型/属性 → 重新校验 + 消歧打标，仍为 pending）
async function v2gEditCandidate(id) {
  const c = _v2g.cands.find(x=>x.id===id);
  if(!c) { toast('候选不存在'); return; }
  const title = document.getElementById('panel-title');
  if(title) title.textContent = '✏️ 编辑候选';
  document.getElementById('panel-body').innerHTML = `
    <div class="form-row"><label>候选名称</label><input id="v2g-e-name" value="${esc(c.entity_name)}"></div>
    <div class="form-row"><label>实体类型（本体）</label><input id="v2g-e-type" value="${esc(c.entity_type)}"></div>
    <div class="form-row"><label>属性 JSON</label><textarea id="v2g-e-props" style="min-height:90px;font-family:monospace;font-size:11.5px;">${esc(c.properties||'{}')}</textarea></div>
    <div style="font-size:11px;color:var(--mut);margin-top:4px;">保存后重新本体校验与消歧打标，仍为待审状态。</div>
    <div style="margin-top:10px;text-align:right;"><button class="btn grn" onclick="v2gSaveCandidate(${c.id})">💾 保存</button>
    <button class="btn ghost" onclick="closePanel()">取消</button></div>`;
  document.getElementById('panel-detail').classList.add('open');
  document.getElementById('overlay').style.display = 'block';
}
async function v2gSaveCandidate(id) {
  const name = document.getElementById('v2g-e-name').value.trim();
  const etype = document.getElementById('v2g-e-type').value.trim();
  const props = document.getElementById('v2g-e-props').value.trim();
  if(!name) { toast('名称不能为空'); return; }
  const r = await api(`/api/knowledge/v2g/candidates/${id}`, {method:'PUT', body:JSON.stringify({name, entity_type:etype, properties:props||'{}'})});
  if(r.error) { toast('保存失败：'+r.error); return; }
  closePanel();
  toast(r.errors && r.errors.length ? `⚠ 已保存，但存在校验问题：${r.errors.join('；')}` : '✅ 候选已更新');
  v2gReviewLoad();
}
// ── E-1~E-7：实体消歧三区（待审/自动合并/历史 + 证据 + 撤销 + 批量操作）──

// ══ P0-3/P0-4 概念层（2026-09-06）：概念-术语双栏 + 状态流转 + 弃用影响 ══
// 概念 = ISO 704 知识单元（语言无关）；术语 = 指称（语言相关）。
// 关键规则：IRI/concept_id 只增不删；改规范词=术语层换首选词；审批需定义；弃用需替代概念。
const _CPT_STATUS = {candidate:['候选','#B7791F','#FFF7E0'], approved:['已批准','#2F855A','#E8F5E9'], deprecated:['已弃用','#C53030','#FDE8E8'], retired:['退役','#718096','#EDF0F2']};
