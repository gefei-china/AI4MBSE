/* 知识库：统计 / 三元组 / 覆盖度 / 图库
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 6726-7309  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function loadKBStats() {
  const s = await api('/api/knowledge/stats?branch=' + encodeURIComponent(getCurrentBranch()));
  document.getElementById('kb-stats').innerHTML = `
    <div class="kpi"><div class="n">${s.reviewed}</div><div class="l">已评审元素 <span class="st ok">reviewed</span></div></div>
    <div class="kpi"><div class="n" style="color:var(--amb);">${s.candidate}</div><div class="l">候选元素 <span class="st w">candidate</span></div></div>
    <div class="kpi"><div class="n">${s.total_relations}</div><div class="l">关系总数</div></div>
    <div class="kpi"><div class="n" style="color:var(--mut);">${s.deprecated}</div><div class="l">已废弃 <span class="st g">deprecated</span></div></div>`;
  loadEngineStats();
  loadGraphDbStats();  // P2：图数据库看板（ArcR-5 查询镜像）
  loadLifecycle();
  loadKBCatOptions();
  loadKBOverview();   // FR-KG-8/11 补 G5：知识库总览一次聚合
  loadCoverage();     // FR-KG-13 补 G6：知识完整度评估
}
/* P0-4: 生命周期分布（发布环节）+ 来源分布（纯 CSS 条形，无图表库） */
async function loadLifecycle() {
  const el = document.getElementById('kb-lifecycle');
  try {
    const r = await api('/api/knowledge/lifecycle');
    const lc = r.lifecycle||{};
    const total = r.total || 1;
    const order = [['raw_chunk','原始切片','#9CA3AF'],['candidate','候选','#F59E0B'],['reviewed','已评审(未发布)','#60A5FA'],
                   ['published','已发布(权威基线)','#10B981'],['deprecated','已废弃','#9CA3AF']];
    const bar = order.map(([k,label,color])=>{
      const n = lc[k]||0;
      const pct = Math.round(n*100/total);
      return `<div style="flex:1;min-width:70px;text-align:center;">
        <div style="height:10px;border-radius:5px;background:${color};opacity:${n?1:0.25};"></div>
        <div style="font-size:11px;font-weight:600;margin-top:4px;">${n}</div>
        <div style="font-size:10px;color:var(--mut);">${label}</div>
        <div style="font-size:10px;color:var(--mut);">${pct}%</div></div>`;
    }).join('');
    const src = (r.by_source||[]).slice(0,6).map(s=>
      `<span class="tag">${esc(s.src)} ${s.n}</span>`).join(' ');
    const cat = (r.by_category||[]).slice(0,8).map(c=>
      `<span class="tag" title="知识类别">${esc(c.cat)} ${c.n}</span>`).join(' ');
    el.innerHTML = `<div style="display:flex;gap:8px;align-items:flex-end;">${bar}</div>
      <div style="margin-top:8px;font-size:11px;color:var(--mut);">按来源：${src||'无'}${cat?`　按类别：${cat}`:''}　发布记录 ${r.published_records||0} 条</div>`;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
/* 优化三：三元组统一审核（S-P-O）+ 本体就绪提示 */
async function loadOntologyReadiness(){
  const el = document.getElementById('onto-ready'); if(!el) return;
  try{
    const r = await api('/api/knowledge/ontology/check');
    el.innerHTML = r && r.ready
      ? `<span style="font-size:11px;color:var(--grn);">✅ 本体已就绪（${r.entity_types.length} 实体类型 / ${r.relation_types.length} 关系类型），AI 建模/抽取受 Schema 约束</span>`
      : `<span style="font-size:11px;color:var(--amb);">⚠️ 本体未完整定义，请先进入「本体模型」完成建模前置（实体 / 关系类型至少各 1）</span>`;
    try{
      const ac = await api('/api/knowledge/ontology/alias-coverage');
      if(ac && ac.zero_count>0){
        el.innerHTML += `<span style="font-size:11px;color:var(--amb);cursor:pointer;margin-left:10px;" onclick="ontAliasCoverage()" title="零别名类型=抽取归一盲区">⚠ 零别名类 ${ac.zero_count}/${ac.total}（点击查看）</span>`;
      } else if(ac && ac.total){
        el.innerHTML += `<span style="font-size:11px;color:var(--grn);margin-left:10px;" title="词典（词法层）覆盖检查">别名覆盖 ${ac.covered}/${ac.total}</span>`;
      }
    }catch(e){}
  }catch(e){ el.innerHTML = '<span style="font-size:11px;color:var(--mut);">本体就绪检测失败</span>'; }
}
// R3：零别名类清单面板
async function ontAliasCoverage(){
  try{
    const ac = await api('/api/knowledge/ontology/alias-coverage');
    const list = (ac.zero_alias||[]).map(x=>`<div style="display:flex;gap:6px;align-items:center;padding:4px 0;border-bottom:1px dashed var(--line);font-size:12px;"><span class="st ${x.kind==='relation'?'b':'w'}">${x.kind==='relation'?'关系':'类'}</span><b>${esc(x.name)}</b></div>`).join('');
    openPanel('⚠ 零别名类型（抽取归一盲区 · '+ac.zero_count+'/'+ac.total+'）',
      '<div style="font-size:11.5px;color:var(--mut);margin-bottom:8px;line-height:1.6;">以下本体类型在词典（词法层）中没有任何别名——抽取遇到这些概念的口语/缩写/错拼时将无法归一。可进入各类型的 Description 面板「＋ 添加别名」补齐。</div>' + (list || '<div style="color:var(--grn);font-size:12px;">✅ 全覆盖</div>'));
  }catch(e){ toast('覆盖度加载失败：'+e.message); }
}
/* ⑤ 三元组统一审核（S-P-O）：待审队列表 + 通过/驳回/全部通过（真实调用后端） */
async function loadTripleReviewPane(){
  const el = document.getElementById('triple-review-pane'); if(!el) return;
  el.innerHTML = '<div class="loading">加载三元组待审队列…</div>';
  try{
    const r = await api('/api/knowledge/triples/review-queue?status=pending&limit=200');
    const items = r.items || []; const st = r.stats || {};
    window._tripleReviewIds = items.map(t=>t.triple_id);
    window._tripleReviewList = items.map(t=>({triple_id:t.triple_id, subject_name:t.subject_name||'', object_type:t.object_type||''}));
    const badge = document.getElementById('triple-count-tab');
    if(badge) badge.textContent = items.length;
    if(!items.length){
      el.innerHTML = `<div style="padding:14px;color:var(--mut);font-size:12px;">暂无待审三元组。三元组统计：已通过 ${st.approved||0} / 待审 ${st.pending||0} / 驳回 ${st.rejected||0}。<br>AI 建模入库候选确认后会自动产生侯审三元组，以(S-P-O)原子单元在此统一审核。</div>`;
      return;
    }
    const dupBadge = t => {
      const di = t.dup_info || {kind:'none'};
      if(di.kind==='triple_repeat') return `<span class="st w" title="${esc(di.dup_with||'')}">🔁 已存在</span>`;
      if(di.kind==='suspect') return `<span class="st w" title="${esc(di.dup_with||'')}">🔀 与图谱「${esc(di.dup_with||'?')}」近似</span>`;
      return `<span class="st ok" title="图库无同指实体/三元组">✅ 新建</span>`;
    };
    const row = t => `<div style="display:flex;align-items:center;gap:6px;padding:6px 8px;border-bottom:1px dashed var(--line);font-size:12px;">
      <span class="tag">${esc(t.object_type==='entity'?'🔗 关系':'🏷 属性')}</span>
      <b style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(t.label)}">${esc(t.subject_name)}</b>
      <span style="color:var(--blue);white-space:nowrap;">${esc(t.predicate)}</span>
      <span style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(t.object_value||t.object_id||'')}</span>
      ${dupBadge(t)}
      <span style="margin-left:auto;display:flex;align-items:center;gap:4px;">
        <span style="font-size:10px;color:var(--mut);">置信 ${t.confidence||0}</span>
        <button class="btn sm grn" style="font-size:10px;padding:1px 8px;" onclick="tripleAct('${t.triple_id}','approved')">通过</button>
        <button class="btn sm ghost" style="font-size:10px;padding:1px 8px;background:rgba(229,62,62,.1);color:var(--red);" onclick="tripleAct('${t.triple_id}','rejected')">驳回</button>
      </span></div>`;
    el.innerHTML = `<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);background:#f7f9fc;font-size:12px;">
      <b>共 ${items.length} 条待审</b>
      <span style="font-size:11px;color:var(--mut);">立即全部通过（慢速核对或批量放行）</span>
      <span style="flex:1"></span>
      <button class="btn sm grn" onclick="tripleApproveAll()">✅ 全部通过</button></div>` +
      items.map(row).join('');
  }catch(e){ el.innerHTML = `<div style="color:var(--red);font-size:12px;">三元组队列加载失败：${e.message}</div>`; }
}
window.tripleAct = async function(triple_id, decision){
  const el = document.getElementById('triple-review-pane'); if(!el) return;
  try{
    const r = await api('/api/knowledge/triples/' + encodeURIComponent(triple_id) + '/review',
      {method:'POST', body:JSON.stringify({decision:decision, note: decision==='approved'?'SDK:三元组统一审核通过':'SDK:三元组统一审核驳回'})});
    if(r && r.error){ toast('操作失败：'+r.error); return; }
    toast(decision==='approved'?'已通过该三元组':'已驳回该三元组');
    if(decision==='approved'){ tripleCommit(); } else { loadTripleReviewPane(); }
  }catch(e){ toast('操作失败：'+(e.message||'')); }
};
function tripleGotoGraph(){
  const subj = (window._tripleReviewList||[]).map(t=>t.subject_name).filter(Boolean);
  try{ if(typeof go==='function') go('kb','kb-d'); }catch(e){}
  if(window.graphState){ graphState.view = graphState.view||{}; graphState.view.status = 'active'; }
  window._tripleGraphSubjects = subj;
  setTimeout(()=>{ try{ if(typeof loadGraph==='function') loadGraph(); }catch(e){} 
    if(subj.length){ try{ if(typeof toast==='function') toast('👁 图谱已按「草稿+已发布」展示；本次三元组主体 '+subj.length+' 个'); }catch(e){} } }, 600);
}
async function tripleCommit(){ 
  const r = await api('/api/knowledge/triples/commit', {method:'POST', body:'{}'});
  toast(`✅ 三元组已落图：实体 ${(r&&r.entities)||0} / 关系 ${(r&&r.relations)||0}`);
  loadTripleReviewPane();
}
async function tripleApproveAll(){
  const ids = window._tripleReviewIds || [];
  if(!ids.length){ toast('无待审三元组'); return; }
  const r = await api('/api/knowledge/triples/batch-review',
    {method:'POST', body:JSON.stringify({triple_ids:ids, decision:'approved', note:'SDK:三元组批量全部通过'})});
  toast(`已通过 ${(r&&r.reviewed)||0} 条三元组，自动落图`);
  tripleCommit();
}
/* FR-KG-8/11 补 G5：知识库总览一次聚合（KPI 卡 + 待评审队列 + 来源分布 + 图谱缩略）
   与「🔄 数据生命周期」面板共存：生命周期数据由 overview.lifecycle 带回但前端不重复渲染（沿用 kb-lifecycle 面板） */
async function loadKBOverview() {
  const el = document.getElementById('kb-overview');
  try {
    const r = await api('/api/knowledge/overview');
    const k = r.kpi||{};
    const kpis = [
      ['🧬 实体', k.entities||0, ''],
      ['🔗 关系', k.relations||0, ''],
      ['📄 文档', k.documents||0, ''],
      ['🕘 待评审', k.pending_review||0, 'color:var(--amb);']
    ].map(([l,n,style])=>`<div class="asset"><div class="n" style="${style}">${n}</div><div class="l">${l}</div></div>`).join('');
    const q = r.review_queue||{};
    const queueRow = (title, total, items, itemFn) => `
      <div style="flex:1;min-width:210px;">
        <div style="font-size:11px;color:var(--mut);margin-bottom:4px;">${title} <span class="st w">${total} 条</span></div>
        ${items.length ? items.map(itemFn).join('') : '<div style="font-size:11px;color:var(--mut);padding:5px 0;">暂无待评审</div>'}
      </div>`;
    const v2gItem = c=>`<div class="todo-item" onclick="go('kb','kb-b')" title="点击前往数据整理处理"><b>${esc(c.name)}</b><span class="tag">${esc(c.entity_type||'候选')}</span></div>`;
    const entItem = c=>`<div class="todo-item" onclick="go('kb','kb-b')" title="点击前往数据整理处理"><b>${esc(c.name)}</b><span class="tag">${esc(c.entity_type)}</span></div>`;
    const relItem = c=>`<div class="todo-item" onclick="go('kb','kb-b')" title="点击前往数据整理处理"><b>${esc(c.source_name)} → ${esc(c.relation_type)} → ${esc(c.target_name)}</b><span class="tag">关系</span></div>`;
    // 来源分布（横向条，纯 CSS）
    const src = r.source_dist||[];
    const maxN = src.reduce((m,s)=>Math.max(m,s.n||0), 1);
    const srcHtml = src.length ? `<div style="margin-top:12px;font-size:11px;color:var(--mut);">来源分布：</div><div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:8px 16px;">` +
      src.map(s=>`<div style="font-size:11px;flex:1;min-width:130px;"><div style="display:flex;justify-content:space-between;"><span>${esc(s.source_type)}</span><b>${s.n}</b></div><div style="height:6px;border-radius:3px;background:var(--line);margin-top:2px;"><div style="height:6px;border-radius:3px;background:var(--blue);width:${Math.round((s.n||0)*100/maxN)}%;"></div></div></div>`).join('') + '</div>' : '';
    // 图谱缩略（静态 top-N 计数列表，不做力导向重排）
    const gt = r.graph_thumb||{};
    const thumb = (gt.entities||[]).slice(0,14).map(e=>`<span class="tag" title="${esc(e.entity_type)}">${esc(e.name)}</span>`).join(' ');
    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">${kpis}</div>
      <div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;">${queueRow('🕘 v2g 候选', q.v2g?.total||0, q.v2g?.items||[], v2gItem)}${queueRow('🧬 实体候选', q.entities?.total||0, q.entities?.items||[], entItem)}${queueRow('🔗 关系候选', q.relations?.total||0, q.relations?.items||[], relItem)}</div>
      ${srcHtml}
      <div style="margin-top:12px;font-size:11px;color:var(--mut);">图谱缩略（已评审实体 <b>${(gt.entities||[]).length}</b> / 关系 <b>${gt.relations||0}</b>）：</div>
      <div style="margin-top:4px;line-height:1.9;">${thumb||'<span style="color:var(--mut);font-size:11px;">暂无已评审实体</span>'}</div>`;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">总览加载失败：${e.message}</div>`; }
}
/* FR-KG-13 补 G6：知识完整度（类型覆盖率 + chunk 链接率 + fallback 建议抽取主题 TOP10） */
async function loadCoverage() {
  const el = document.getElementById('kb-coverage');
  try {
    const r = await api('/api/knowledge/coverage');
    const tc = r.type_coverage||{};
    const cl = r.chunk_linked||{};
    const rate = Math.min(100, Math.round((tc.coverage_rate||0)*100));
    const covBar = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
      <div style="flex:1;height:10px;border-radius:5px;background:var(--line);"><div style="height:10px;border-radius:5px;background:var(--grn);width:${rate}%;"></div></div>
      <b style="font-size:16px;color:var(--blue-d);">${rate}%</b></div>
      <div style="font-size:11px;color:var(--mut);">本体实体类型实例覆盖率：${tc.covered_types||0}/${tc.total_types||0} 有实例</div>`;
    const unlink = Math.round((cl.unlinked_ratio||0)*100);
    const chunkHtml = `<div style="font-size:11px;color:var(--mut);margin-top:6px;">文档 chunk 链接率：${cl.linked_chunks||0}/${cl.total_chunks||0} 已链接实体，<span style="color:${unlink>50?'var(--amb)':'var(--mut)'};">未链接 ${unlink}%</span></div>`;
    const empties = tc.empty_types||[];
    const emptyHtml = `<div style="margin-top:10px;font-size:11px;color:var(--mut);">空类型（无已评审实例，建议优先抽取）：</div>
      <div style="margin-top:4px;line-height:1.9;">${empties.length ? empties.map(t=>`<span class="tag" style="border-color:var(--amb);color:var(--amb);" title="无已评审实例">${esc(t.name)}</span>`).join(' ') : '<span style="font-size:11px;color:var(--grn);">✅ 全部类型均有已评审实例</span>'}</div>`;
    const topics = (r.fallback_topics||[]).slice(0,10);
    const topicHtml = `<div style="margin-top:10px;font-size:11px;color:var(--mut);">建议抽取主题（检索 fallback 高频词，点击直达 v2g 手动抽取）：</div>
      <div style="margin-top:4px;line-height:1.9;">${topics.length ? topics.map(t=>`<span class="tag" style="border-color:var(--blue);color:var(--blue-d);cursor:pointer;" title="点击用该主题触发抽取" onclick="coverageExtractTopic('${esc(t.word).replace(/'/g,"\\'")}')">${esc(t.word)} ${t.count}</span>`).join(' ') : '<span style="font-size:11px;color:var(--mut);">暂无 fallback 检索记录</span>'}</div>`;
    el.innerHTML = covBar + chunkHtml + emptyHtml + topicHtml;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">完整度加载失败：${e.message}</div>`; }
}
/* 点击完整度主题词 → 填入检索框并触发 v2g 手动抽取（最简路径，等价 impToV2G 抽取流程） */
function coverageExtractTopic(word) {
  const q = document.getElementById('kb-chunk-q');
  if(q) q.value = word;
  v2gExtract();
}
/* P0-3: 知识类别下拉选项（设计方法知识/设计资产） */
let KB_CATS = [];
async function loadKBCatOptions() {
  try {
    const cats = await api('/api/knowledge/categories');
    KB_CATS = Array.isArray(cats)?cats:[];
    const sel = document.getElementById('kb-cat-filter');
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部类别</option>' +
      KB_CATS.map(c=>`<option value="${esc(c.name)}" data-g="${c.group_name}">${c.name}</option>`).join('');
    sel.value = cur;
  } catch(e) {}
}
function kbCatLabel(name) {
  const c = KB_CATS.find(x=>x.name===name);
  return c ? `<span class="tag" style="border-color:${c.group_name==='设计方法知识'?'var(--blue)':'var(--green)'};">${c.group_name==='设计方法知识'?'📐':'🧩'}${esc(name)}</span>` : (name?`<span class="tag">${esc(name)}</span>`:'<span style="color:var(--mut);">未分类</span>');
}
/* P0-3: 实体打知识类别标签（居中卡片输入，空=清除） */
async function kbSetEntityCategory(id, current) {
  if(!branchWritable()) return;
  const names = KB_CATS.map(c=>`${c.name}（${c.group_name}）`).join('\n');
  const v = await promptDialog({title:'设置知识类别', message:`输入类别名（设计方法知识/设计资产子类）：\n可选：${names}\n留空=清除分类`, value:current||'', okText:'保存'});
  if(v===null) return;
  const r = await api(`/api/knowledge/entities/${id}/category`, {method:'PUT', body:JSON.stringify({category:v.trim()})});
  if(r && r.error) { toast('设置失败：' + r.error); return; }
  toast(`✅ 已设置类别：${v.trim()||'未分类'}`);
  loadKBEntities(); loadLifecycle(); loadGraph && loadGraph();
}
/* P0-3: 文档打知识类别标签（居中卡片输入，空=清除） */
async function kbSetDocCategory(id, current) {
  if(!branchWritable()) return;
  const names = KB_CATS.map(c=>`${c.name}（${c.group_name}）`).join('\n');
  const v = await promptDialog({title:'设置文档知识类别', message:`输入类别名（设计方法知识/设计资产子类）：\n可选：${names}\n留空=清除分类`, value:current||'', okText:'保存'});
  if(v===null) return;
  const r = await api(`/api/documents/${id}/category`, {method:'PUT', body:JSON.stringify({category:v.trim()})});
  if(r && r.error) { toast('设置失败：' + r.error); return; }
  toast(`✅ 文档已设置类别：${v.trim()||'未分类'}`);
  loadDocs(); loadLifecycle();
}
/* P1: 双引擎消费统计（图/向量/混合路由占比） */
async function loadEngineStats() {
  const el = document.getElementById('engine-stats');
  try {
    const s = await api('/api/knowledge/engine-stats');
    const routes = s.routes||{};
    const bars = ['graph','mixed','vector'].filter(k=>routes[k]).map(k=>{
      const r = routes[k];
      const nm = {graph:'纯图检索',mixed:'图+向量混合',vector:'纯向量检索'}[k]||k;
      return `<div style="flex:1;min-width:130px;border:1px solid var(--line);border-radius:8px;padding:10px;background:#fff;">
        <div style="font-size:11px;color:var(--mut);">${nm} <b style="float:right;">${r.pct}%</b></div>
        <div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${r.count}<small style="font-size:11px;color:var(--mut);"> 次</small></div>
        <div style="font-size:11px;color:var(--mut);">平均置信 ${r.avg_confidence||0} · ${r.avg_latency_ms||0}ms</div>
      </div>`;
    }).join('');
    const recent = (s.recent||[]).slice(0,4).map(x=>`<span class="tag">${x.route}「${x.query}」</span>`).join(' ');
    el.innerHTML = `
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <div style="flex:1;min-width:130px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
          <div style="font-size:11px;color:var(--blue-d);">累计查询</div>
          <div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${s.total_queries}<small style="font-size:11px;"> 次</small></div>
          <div style="font-size:11px;color:var(--blue-d);">每次检索自动落库统计</div>
        </div>
        ${bars}
      </div>
      ${recent?`<div style="margin-top:8px;font-size:11px;color:var(--mut);">最近：${recent}</div>`:''}`;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
/* 2026-09-01 P2：图数据库看板（ArcR-5 查询镜像：stats / SPARQL 只读查询 / 同步入图 / N-Quads 导出） */
async function loadGraphDbStats(){
  const el = document.getElementById('graphdb-stats');
  try {
    const s = await api('/api/graph-db/status');
    if(!s.ok || !s.enabled){
      el.innerHTML = `<div style="color:var(--mut);font-size:12px;">图数据库未启用——配置 graph_db.enabled=true 后重启生效（后端：pyoxigraph 嵌入式默认 / fuseki / neo4j）</div>`;
      return;
    }
    const st = s.stats||{};
    const graphs = (st.graphs||[]).map(g=>`<span class="tag">${esc(String(g).replace(/^(urn:mbse:graph:|http:\/\/www\.xingwang\.mbse\/graph:)/,'').replace(/%2F/gi,'/'))}</span>`).join(' ');
    el.innerHTML = `<div style="display:flex;gap:10px;flex-wrap:wrap;">
      <div style="flex:1;min-width:120px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
        <div style="font-size:11px;color:var(--blue-d);">后端</div><div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${esc(st.backend||'-')}</div></div>
      <div style="flex:1;min-width:120px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
        <div style="font-size:11px;color:var(--blue-d);">三元组</div><div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${st.triple_count||0}</div></div>
      <div style="flex:2;min-width:200px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
        <div style="font-size:11px;color:var(--blue-d);">命名图（分支）</div><div style="margin:6px 0 0;">${graphs||'<span style="color:var(--mut)">空</span>'}</div></div>
    </div>`;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
async function runGraphDbQuery(){
  const q = (document.getElementById('graphdb-q')||{}).value;
  if(!q || !q.trim()) return;
  const out = document.getElementById('graphdb-result');
  out.innerHTML = '<span style="color:var(--mut)">执行中…</span>';
  const r = await api('/api/graph-db/query', {method:'POST', body:JSON.stringify({query:q.trim(), limit:20})});
  if(r.error || r.ok===false){ out.innerHTML = `<span style="color:var(--red)">${esc(r.error||'查询失败')}</span>`; return; }
  if(!r.rows || !r.rows.length){ out.innerHTML = '<span style="color:var(--mut)">图谱无命中——可调整查询、切换分支命名图，或先执行「同步入图」</span>'; return; }
  const keys = Object.keys(r.rows[0]);
  const rowsHtml = '<table class="t" style="width:100%;font-size:12px;"><tr>' +
    keys.map(k=>`<th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">${esc(k)}</th>`).join('') + '</tr>' +
    r.rows.map(row=>'<tr>'+keys.map(k=>`<td style="padding:5px 8px;border-bottom:1px solid var(--line);word-break:break-all;">${esc(String(row[k]??''))}</td>`).join('')+'</tr>').join('') + '</table>';
  out.innerHTML = `<div style="color:var(--mut);margin-bottom:6px;">命中 ${r.count} 行（显示前 ${r.rows.length}）：${r.note||''}</div>` + rowsHtml;
}
async function syncGraphDb(){
  const r = await api('/api/graph-db/sync', {method:'POST'});
  if(r.error || r.ok===false) toast('同步失败：'+(r.error||''));
  else toast(`✅ 已同步 ${r.synced||0} 条（剩余待入图 ${r.pending_left||0}）`);
  loadGraphDbStats();
}
async function exportGraphDb(){
  const r = await api('/api/graph-db/export');
  if(r.error || r.ok===false) toast('导出失败：'+(r.error||''));
  else toast(`✅ 已导出 ${r.quads||0} 条四元组 → ${r.path||''}`);
}
async function searchChunks() {
  const q = document.getElementById('kb-chunk-q').value.trim();
  if(!q) { toast('请输入查询'); return; }
  const mode = document.getElementById('kb-chunk-mode').value;
  const el = document.getElementById('chunk-hits');
  el.innerHTML = '<div class="loading">检索中…</div>';
  try {
    const r = await api('/api/knowledge/chunks/search', {method:'POST', body:JSON.stringify({query:q, hybrid: mode==='hybrid'})});
    const hits = r.hits||[];
    if(!hits.length) { el.innerHTML = '<div style="color:var(--mut);font-size:12px;">无命中分块（请先上传并解析文档）</div>'; return; }
    el.innerHTML = hits.map(h=>`
      <div style="border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px;background:#fff;">
        <div style="font-size:11px;color:var(--mut);margin-bottom:4px;">
          <span class="st b">命中 ${h.score}</span>
          ${h.confidence_level?`<span class="st ${h.confidence_level==='高'?'ok':h.confidence_level==='中'?'a':'w'}">置信 ${h.confidence_level}</span>`:''}
          ${h.vec_score!==undefined?`<span class="tag">vec ${h.vec_score}</span><span class="tag">bm25 ${h.bm25_score}</span>`:''}
          <span class="tag">${esc(h.source_doc)}</span>
          ${h.section?`<span class="tag">${esc(h.section)}</span>`:''}
          <span class="tag">chunk#${h.chunk_index}</span>
          <span class="st g">${h.embed_version}</span>
        </div>
        ${h.recall_reason?`<div style="font-size:11px;color:var(--blue-d);margin-bottom:4px;">🔗 ${esc(h.recall_reason)}</div>`:''}
        <div style="font-size:12.5px;line-height:1.6;">${esc(h.content)}</div>
      </div>`).join('') + (r.mode==='hybrid'?`<div style="font-size:11px;color:var(--mut);">混合检索：BM25 ${r.bm25_count} / 向量 ${r.vec_count}</div>`:'');
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">检索失败：${e.message}</div>`; }
}
// ── O-1：向量→图谱半自动转化（抽取候选 → 勾选确认入库 → chunk 溯源）──
let v2gBatchId = null;
async function v2gExtract() {
  const q = document.getElementById('kb-chunk-q').value.trim();
  if(!q) { toast('请先输入查询'); return; }
  const el = document.getElementById('v2g-panel');
  el.innerHTML = '<div class="loading">抽取候选（LLM 受本体 schema 约束）…</div>';
  try {
    const r = await api('/api/knowledge/v2g/extract', {method:'POST', body:JSON.stringify({query:q, top_k:5})});
    v2gBatchId = r.batch_id;
    toast(`抽取完成：候选 ${r.node_count+r.edge_count}（拒绝 ${r.rejected.length}）`);
    loadV2GCandidates();
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">抽取失败：${e.message}</div>`; }
}
// B2：候选来源徽章（source_type：ai_model=AI建模 / doc_extract=文档抽取 / 空=旧数据）
function v2gSrcBadge(c){
  const st = c.source_type || '';
  if(st === 'ai_model') return '<span class="tag" style="background:#eaf2ff;color:#185FA5;" title="AI 建模/SysML 通道（统一闸门暂存）">🤖 AI建模</span>';
  if(st === 'doc_extract') return '<span class="tag" style="background:#f0f7f0;color:#2f855a;" title="文档抽取通道">📄 文档抽取</span>';
  return '<span style="font-size:11px;color:var(--mut);">' + esc(c.source_doc||'-') + '</span>';
}
// B2：候选面板批量驳回（勾选 .v2g-check）
async function v2gCandReject() {
  const ids = Array.from(document.querySelectorAll('.v2g-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要驳回的候选'); return; }
  const reason = await promptDialog({title:'批量驳回候选', message:`驳回 ${ids.length} 条候选，请填写原因：`, value:'', placeholder:'如：重复 / 信息不完整 / 超出本体范围…', multiline:true, okText:'驳回'});
  if(reason===null) return;
  if(!String(reason).trim()){ toast('驳回必须填写原因'); return; }
  const r = await api('/api/knowledge/v2g/reject', {method:'POST', body:JSON.stringify({candidate_ids:ids, reason})});
  toast(`🚫 已驳回 ${r.rejected||0} 条`);
  loadV2GCandidates();
}
async function loadV2GCandidates() {
  const el = document.getElementById('v2g-panel');
  try {
    // B2：批次筛选下拉（SYSM- 前缀=AI建模批次）
    let batchOpts = '';
    try {
      const batches = await api('/api/knowledge/v2g/batches');
      if(Array.isArray(batches) && batches.length){
        batchOpts = `<select id="v2g-batch-filter" style="border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:11.5px;" onchange="v2gBatchId=this.value||null;loadV2GCandidates();">
          <option value="">全部批次</option>` + batches.map(b=>`<option value="${esc(b.batch_id||'')}" ${(v2gBatchId&&v2gBatchId===b.batch_id)?'selected':''}>${esc(b.batch_id||'')}${String(b.batch_id||'').startsWith('SYSM-')?' ·AI建模':''}${(b.total!=null)?`（${b.total}）`:''}</option>`).join('') + `</select>`;
      }
    } catch(e) {}
    const cands = await api('/api/knowledge/v2g/candidates?limit=200' + (v2gBatchId?`&batch_id=${v2gBatchId}`:''));
    if(!cands.length) { el.innerHTML = (batchOpts?`<div style="margin-bottom:8px;display:flex;align-items:center;gap:8px;">批次：${batchOpts}<span style="font-size:11px;color:var(--mut);">（v2g_candidates.source_type 已启用来源区分）</span></div>`:'') + '<div style="color:var(--mut);font-size:12px;">暂无候选（先抽取，或刷新查看最新批次）</div>'; return; }
    const pendN = cands.filter(c=>c.status==='pending').length;
    el.innerHTML = (batchOpts?`<div style="margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">批次：${batchOpts}<span style="font-size:11px;color:var(--mut);">共 ${cands.length} 条 · 待处理 ${pendN}</span></div>`:'') +
      `<table class="t"><tr><th>选</th><th>候选</th><th>类型</th><th>来源</th><th>批次</th><th>状态</th><th>校验错误</th></tr>` +
      cands.map(c=>`<tr>
        <td>${c.status==='pending'?`<input type="checkbox" class="v2g-check" value="${c.id}">`:'-'}</td>
        <td><b>${esc(c.entity_name)}</b></td>
        <td><span class="tag">${esc(c.entity_type)}</span></td>
        <td style="font-size:11px;">${v2gSrcBadge(c)}</td>
        <td style="font-size:10.5px;color:var(--mut);">${esc(c.batch_id||'-')}</td>
        <td><span class="st ${c.status==='confirmed'?'ok':c.status==='rejected'?'r':'w'}">${c.status}</span></td>
        <td style="font-size:11px;color:var(--red);">${esc(c.errors||'')}</td>
      </tr>`).join('') + '</table>' +
      (pendN?`<div style="padding:8px;display:flex;gap:8px;align-items:center;"><button class="btn sm" onclick="v2gCandReject()" style="color:var(--red);">🚫 批量驳回勾选</button><span style="font-size:11px;color:var(--mut);">勾选待处理候选后驳回（留痕原因）</span></div>`:'') +
      (v2gBatchId ? `<div style="padding:8px;text-align:right;"><button class="btn sm" onclick="gotoV2GBatch('${v2gBatchId}')">🗂 去抽取治理中心批量确认 →</button></div>` : '');
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
async function v2gConfirm() {
  if(!branchWritable()) return;
  if(!v2gBatchId) { toast('请先抽取候选'); return; }
  const ids = Array.from(document.querySelectorAll('.v2g-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要入库的候选'); return; }
  const r = await api('/api/knowledge/v2g/confirm', {method:'POST', body:JSON.stringify({batch_id:v2gBatchId, selected_ids:ids})});
  toast(`✅ 入库 ${r.confirmed} 条 / 拒绝 ${r.rejected.length} 条（chunk 溯源已链接）`);
  loadV2GCandidates(); loadGraph();
}
let _kbEnt = { page:1, size:15 };
async function loadKBEntities() {
  const status = document.getElementById('kb-status-filter').value;
  const search = document.getElementById('kb-search').value;
  const cat = document.getElementById('kb-cat-filter').value;
  let url = '/api/knowledge/entities?';
  url += 'branch=' + encodeURIComponent(getCurrentBranch()) + '&';   // KB分支隔离：实体浏览按当前工作分支
  if(status) url += `status=${status}&`;
  if(search) url += `search=${encodeURIComponent(search)}&`;
  let entities = await api(url);
  // P0-3: 知识类别前端二次过滤（后端接口无类别参数）
  if(cat) entities = entities.filter(e=>(e.knowledge_category||'')===cat);
  const total = entities.length;
  const pages = Math.max(1, Math.ceil(total/_kbEnt.size));
  if(_kbEnt.page > pages) _kbEnt.page = pages;
  const items = entities.slice((_kbEnt.page-1)*_kbEnt.size, _kbEnt.page*_kbEnt.size);
  const tbl = document.getElementById('kb-entity-table');
  tbl.innerHTML = `<tr><th>ID</th><th>名称</th><th>类型</th><th>知识类别</th><th>状态</th><th>分支</th><th>来源</th><th>操作</th></tr>` +
    (items.length ? items.map(e=>`<tr>
      <td>${e.id}</td><td><b>${e.name}</b></td><td>${e.entity_type}</td>
      <td>${kbCatLabel(e.knowledge_category)}</td>
      <td><span class="st ${e.status==='reviewed'?'ok':e.status==='candidate'?'w':'g'}">${e.status}</span></td>
      <td>${e.branch}</td><td>${e.source_type||'-'}</td>
      <td><button class="btn sm ghost" onclick="viewEntity('${e.id}')">查看</button>
          <button class="btn sm ghost" onclick="kbSetEntityCategory('${e.id}', '${esc(e.knowledge_category||'')}')" title="知识类别">🏷</button></td>
    </tr>`).join('') : '<tr><td colspan="8" style="text-align:center;color:var(--mut);padding:16px;">无匹配实体</td></tr>');
  renderPagerBar({
    el: document.getElementById('kb-ent-pager'), total, page: _kbEnt.page, size: _kbEnt.size,
    onPage: p => { _kbEnt.page = p; loadKBEntities(); },
    onSize: s => { _kbEnt.size = s; _kbEnt.page = 1; loadKBEntities(); }
  });
}
// 提交 kind 元数据（修改历史/分支时间线共用）：徽章中文标签 + st 颜色类
const COMMIT_KIND_META = {
  import:  {label:'导入',     cls:'b'},
  review:  {label:'审核',     cls:'ok'},
  merge:   {label:'合并',     cls:'g'},
  manual:  {label:'手动编辑', cls:'w'},
  rollback:{label:'回滚',     cls:'r'},
};
function commitKindBadge(kind) {
  const m = COMMIT_KIND_META[kind] || {};
  return `<span class="st ${m.cls||'g'}">${m.label||esc(kind||'')}</span>`;
}
async function viewEntity(id) {
  const e = await api(`/api/knowledge/entities/${id}`);
  const props = e.properties ? JSON.parse(e.properties) : {};
  // 一键追溯（创建/审核人/时间/来源文档元数据）
  const trace = `
    <h4 style="margin:12px 0 6px;font-size:13px;">📌 一键追溯</h4>
    <table class="t"><tr><th>元数据</th><th>值</th></tr>
      <tr><td>创建人</td><td>${esc(e.created_by||'-')}</td></tr>
      <tr><td>创建时间</td><td>${esc(e.created_at||'-')}</td></tr>
      <tr><td>审核人</td><td>${esc(e.reviewed_by||'-')}</td></tr>
      <tr><td>审核时间</td><td>${esc(e.reviewed_at||'-')}</td></tr>
      <tr><td>来源文档</td><td>${esc(e.source_doc||'-')}</td></tr>
      <tr><td>来源类型</td><td>${esc(e.source_type||'-')}</td></tr>
      <tr><td>图谱来源</td><td>${esc(e.graph_source||'manual')}</td></tr>
    </table>`;
  // 修改历史（分支版本管理 FR-KG-11：含该实体的提交时间线，id DESC）
  const khRows = (e.commit_history||[]).map(c=>{
    const f = c.fields||{};
    const fsum = (f.name||f.entity_type||f.status)
      ? ` <span style="color:var(--mut);font-size:11px;">（${esc(f.name||'')} · ${esc(f.entity_type||'')} · ${esc(f.status||'')}）</span>` : '';
    return `<div style="padding:5px 0;border-bottom:1px dashed var(--line);font-size:12px;">
      ${commitKindBadge(c.kind)} ${esc(c.message||'')}${fsum}
      <div style="color:var(--mut);font-size:11px;">${esc((c.created_at||'').slice(0,16))} · ${esc(c.created_by||'-')}</div>
    </div>`;
  }).join('');
  const hist = `<h4 style="margin:12px 0 6px;font-size:13px;">📜 修改历史</h4>` + (khRows
    ? `<div style="border:1px solid var(--line);border-radius:8px;padding:4px 10px;">${khRows}</div>`
    : '<div style="color:var(--mut);font-size:12px;">暂无修改记录（新实体一般只有基线）</div>');
  let html = `<h4 style="color:var(--blue-d);margin-bottom:10px;">${e.name} (${e.id})</h4>
    <div class="kv"><span>类型</span><b>${e.entity_type}</b></div>
    <div class="kv"><span>状态</span><b><span class="st ${e.status==='reviewed'?'ok':'w'}">${e.status}</span></b></div>
    <div class="kv"><span>分支</span><b>${e.branch}</b></div>
    <div class="kv"><span>知识类别</span><b>${kbCatLabel(e.knowledge_category)}
      <button class="btn sm ghost" style="margin-left:6px;" onclick="kbSetEntityCategory('${e.id}', '${esc(e.knowledge_category||'')}')">🏷 打标</button></b></div>
    <div class="kv"><span>发布时间</span><b>${e.published_at?`<span class="st ok">已发布 ${e.published_at}</span>`:'<span style="color:var(--mut);">未发布（未进入 release 基线）</span>'}</b></div>
    <div class="kv"><span>来源</span><b>${e.source_doc||e.source_type||'-'}</b></div>
    <h4 style="margin:12px 0 6px;">属性</h4>
    <table class="t"><tr><th>键</th><th>值</th></tr>${Object.entries(props).map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table>${trace}${hist}`;
  if(e.relations && e.relations.length) {
    html += `<h4 style="margin:12px 0 6px;">关系</h4><table class="t"><tr><th>源</th><th>关系</th><th>目标</th></tr>${e.relations.map(r=>`<tr><td>${r.source_name||r.source_id}</td><td>${r.relation_type}</td><td>${r.target_name||r.target_id}</td></tr>`).join('')}</table>`;
  }
  openPanel(`实体详情 · ${e.name}`, html);
}
function loadKBTab(id) {
  // 2026-09-18：先把 kb-c 的语境落定（术语词典是**显式子态**，由入口置 _kbCtxTerms=true 触发），
  // 必须在下方 chip 高亮之前完成 —— 否则高亮读到的是上一次导航残留的 _kbCtx。
  if(id === 'kb-c'){
    window._kbCtx = (window._kbCtxTerms === true) ? 'terms' : 'model';
    window._kbCtxTerms = false;   // 一次性消费，随即复位（深链/侧栏/角色快捷默认落「本体模型」）
  }
  // 2026-09-17 知识中心整合：资料库(kb-e)/图谱工作区(kb-d) 顶层双 Tab 切换器——
  // 显隐 + 高亮同步。chip 点击走 go('kb', tabId) 完整路由，此处只做状态回写。
  // 2026-09-18 知识中心收敛：知识域顶层 Tab 为 5 个
  // （知识浏览 kb-a / 资料库 kb-e / 图谱工作区 kb-d / 本体模型 kb-c / 术语词典 kb-c+terms）；
  // 术语词典是 kb-c 的显式子态，按 _kbCtx 决定高亮哪一个 chip。
  const hubTabs = document.getElementById('kbhub-tabs');
  if(hubTabs){
    // 2026-09-18：知识浏览(kb-a) 迁入后，知识域共 5 个顶层 Tab；
    // kb-a 也从"隐藏入口"变为正式 Tab（kb-b 数据整理按用户要求暂不动，仍不显示 Tab 栏）
    const isHub = (id==='kb-a'||id==='kb-d'||id==='kb-e'||id==='kb-c');
    hubTabs.style.display = isHub ? 'flex' : 'none';
    const _isTerms = (window._kbCtx === 'terms');
    const tA = document.getElementById('kbhub-tab-a');
    const tE = document.getElementById('kbhub-tab-e'), tD = document.getElementById('kbhub-tab-d');
    const tC = document.getElementById('kbhub-tab-c'), tT = document.getElementById('kbhub-tab-t');
    if(tA) tA.classList.toggle('on', id==='kb-a');
    if(tE) tE.classList.toggle('on', id==='kb-e');
    if(tD) tD.classList.toggle('on', id==='kb-d');
    if(tC) tC.classList.toggle('on', id==='kb-c' && !_isTerms);
    if(tT) tT.classList.toggle('on', id==='kb-c' && _isTerms);
  }
  // 知识库顶部模块标题行：已全部停用（2026-09-18）
  // - 该行只剩一个模块标题（分支切换早已下沉到图谱数据行），与上方 Tab 栏信息重复；
  // - 原仅知识浏览(kb-a)保留，但 kb-a 现已成为知识中心第一个 Tab → 一并隐藏，避免标题与 Tab 双份。
  const bar = document.getElementById('kb-toolbar');
  if(bar) bar.style.display = 'none';
  const mt = document.getElementById('kb-module-title');
  if(mt) mt.textContent = KB_TAB_TITLES[id] || '知识库';
  if(id==='kb-a') { loadKBStats(); loadKBEntities(); }
  if(id==='kb-e') loadDocs();
  if(id==='kb-b') { loadKBFlowBar(); v2gReviewLoad(); loadFusion(); }  // loadReviewQueue 已随标注审核面板收敛移除（容器不存在，调用即抛空引用）
  if(id==='kb-c') {
    // 2026-09-02 P0-2/P0-4：顶部 Tab=实体维度（类/对象属性/数据属性），图谱降为中栏视图；进入默认「类 · 编辑」
    // 2026-09-18：语境（terms/model）已在 loadKBTab 开头落定并消费掉入口标记，此处只读取结果。
    const _termsEntry = (window._kbCtx === 'terms');
    ontPaneMode = 'edit';
    const _row = document.getElementById('ont-subtab-row'); if(_row) _row.style.display = '';
    const _ontDef = document.querySelector('#ont-subtab-row [data-tabgrp="ont"][onclick*="\'classes\'"]');
    if(_ontDef) switchOntTab(_ontDef, 'classes');
    loadOntology(); loadOntologyReadiness();
    // 2026-09-17 R6：术语词典在同一次 loadKBTab 内同步切到术语视图，替代 02-shell.js 原先的
    // setTimeout(showOntTerms,500) 硬等待 —— 消除「500ms 内改点其它入口被强制拉回术语视图 + 面包屑被覆盖」的竞态。
    if(_termsEntry && typeof showOntTerms === 'function') showOntTerms();
  }
  if(id==='kb-d') loadGraph();
}
// ══ 统一分页组件（全局列表页共用）：右下角对齐，内容 = 总数 · 当前页/总页数 · 页码切换 · 每页条数下拉(15/20/50/100) ══
function renderPagerBar(o) {
  const el = o.el;
  if (!el) return;
  if (!o.total) { el.innerHTML = ''; return; }
  const size = o.size || 15;
  const pages = Math.max(1, Math.ceil(o.total / size));
  const page = Math.max(1, Math.min(o.page || 1, pages));
  const sizes = o.sizes || [15, 20, 50, 100];
  const pgBtn = (label, p, disabled) =>
    `<button class="btn sm ghost" style="padding:2px 8px;font-size:11px;" ${disabled?'disabled':''} data-pg-page="${p}" title="第 ${p} 页">${label}</button>`;
  // 页码窗口：首尾 + 当前页±2，间隔用省略号
  const numSet = new Set([1, pages]);
  for (let p = page - 2; p <= page + 2; p++) if (p >= 1 && p <= pages) numSet.add(p);
  const nums = [...numSet].sort((a,b)=>a-b).map((p,i,arr)=>
    (i>0 && p-arr[i-1]>1 ? '<span style="color:var(--mut);padding:0 1px;">…</span>' : '') +
    `<button class="btn sm ghost" style="padding:2px 7px;font-size:11px;${p===page?'background:var(--blue);color:#fff;border-color:var(--blue);':''}" data-pg-page="${p}">${p}</button>`
  ).join('');
  const sel = `<select class="pg-size" title="每页条数" style="border:1px solid var(--line);border-radius:6px;padding:2px 6px;font-size:11.5px;color:var(--mut);">` +
    sizes.map(s=>`<option value="${s}" ${s===size?'selected':''}>${s}</option>`).join('') + '</select>';
  el.innerHTML =
    `<span style="color:var(--mut);">共 <b style="color:var(--blue-d);">${o.total}</b> 条</span>` +
    `<span style="color:var(--mut);">第 <b style="color:var(--blue-d);">${page}</b>/<b>${pages}</b> 页</span>` +
    `<span style="flex:1"></span>` +
    pgBtn('«', 1, page<=1) + pgBtn('‹', page-1, page<=1) + nums + pgBtn('›', page+1, page>=pages) + pgBtn('»', pages, page>=pages) +
    `<span style="color:var(--mut);">每页 ${sel} 条</span>`;
  // 页码点击委派（data-pg-page 属性寻址）
  el.onclick = ev => {
    const t = ev.target;
    if (t && t.dataset && t.dataset.pgPage !== undefined && o.onPage) o.onPage(parseInt(t.dataset.pgPage, 10));
  };
  const selEl = el.querySelector('.pg-size');
  if (selEl) selEl.onchange = () => { if (o.onSize) o.onSize(parseInt(selEl.value, 10)); };
}
// ── 实体审核队列（标准审核：candidate → reviewed / deprecated；对齐行业质检工作台：待办优先 + 分区视图 + 分页）──
let _rev = { list:[], reviewed:[], deprecated:[], kw:'', type:'', confMin:0, seg:'candidate', page:1, size:15 };
