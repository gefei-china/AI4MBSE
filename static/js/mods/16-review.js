/* 审核：实体审核 / 关系审核 / 重复簇
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 7310-7711  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function loadReviewQueue() {
  const br = 'personal';   // 标注审核：数据来自上游候选，审核后进入个人图数据库（仅与个人分支关联）
  const [cands, reviewed, deprecated, dups] = await Promise.all([
    api('/api/knowledge/entities?status=candidate&branch=' + br),
    api('/api/knowledge/entities?status=reviewed&branch=' + br),
    api('/api/knowledge/entities?status=deprecated&branch=' + br).catch(()=>[]),
    api('/api/knowledge/entity-dups').catch(()=>({duplicates:[]})),
  ]);
  _rev.list = cands || [];
  _rev.reviewed = reviewed || [];
  _rev.deprecated = deprecated || [];
  // 合并候选子分组：涉及 pending 消歧候选的实体 id 集合（keep/dup 双端）
  _rev.mergeIds = new Set();
  ((dups||{}).duplicates||[]).filter(d=>d.status==='pending').forEach(d=>{
    _rev.mergeIds.add(d.keep_id); _rev.mergeIds.add(d.dup_id);
  });
  _rev.page = 1;
  const _rc = document.getElementById('review-count');
  if(_rc) _rc.textContent = `待审核 ${_rev.list.length}`;
  const tabBadge = document.getElementById('review-count-tab');
  if(tabBadge) tabBadge.textContent = `${_rev.list.length} 条`;
  const el = document.getElementById('review-queue');
  if(!el) return;   // 标注审核面板已移除：容器不存在则跳过（避免切分支时空 innerHTML 报错）
  const totalDone = _rev.list.length + _rev.reviewed.length;
  const passRate = totalDone > 0 ? Math.round(_rev.list.length/totalDone*100) : 0;
  const types = [...new Set(_rev.list.map(e=>e.entity_type).filter(Boolean))];
  // 状态分区（行业质检工作台：默认聚焦待办，已通过/已驳回按需切换查看全生命周期）
  // 合并建议子分组：候选实体中涉及 pending 消歧候选的对（进入审核队列的合并候选）
  const mergeN = _rev.list.filter(e=>_rev.mergeIds.has(e.id)).length;
  const segs = [['candidate','⏳ 待审'],['merge','🔀 合并建议'],['reviewed','✅ 已通过'],['deprecated','🚫 已驳回']];
  const segBar = `<div style="padding:8px 10px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <span style="font-size:11px;color:var(--mut);">待审率 <b>${passRate}%</b></span>
      ${segs.map(([k,l])=>`<span class="st ${_rev.seg===k?'b':'g'}" style="cursor:pointer;font-size:11px;${_rev.seg===k?'':'opacity:.7;'}" onclick="revSetSeg('${k}')">${l} <b>${k==='candidate'?_rev.list.length:k==='merge'?mergeN:k==='reviewed'?_rev.reviewed.length:_rev.deprecated.length}</b></span>`).join('')}
      <span style="flex:1"></span>
      <input id="rev-search" placeholder="🔍 搜索名称…" oninput="revApplyFilter()" style="border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:12px;width:130px;">
      <select id="rev-type" onchange="revApplyFilter()" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><option value="">全部类型</option>${types.map(t=>`<option>${esc(t)}</option>`).join('')}</select>
      <label style="font-size:12px;color:var(--mut);display:flex;align-items:center;gap:3px;">置信度 ≥ <input id="rev-conf" type="number" min="0" max="1" step="0.05" onchange="revApplyFilter()" style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:12px;width:50px;"></label>
      <button class="btn sm ghost" onclick="loadReviewQueue()">刷新</button>
    </div>`;
  const rows = revFiltered();
  const pages = Math.max(1, Math.ceil(rows.length / _rev.size));
  if(_rev.page > pages) _rev.page = pages;
  const items = rows.slice((_rev.page-1)*_rev.size, _rev.page*_rev.size);
  const isActive = _rev.seg==='candidate' || _rev.seg==='merge';
  const batchBar = isActive
    ? `<div style="padding:6px 8px;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line);font-size:11.5px;">
      <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" id="rev-check-all" onchange="revToggleAll(this)"> 全选</label>
      <button class="btn sm grn" onclick="batchReview('confirm')">✅ 批量通过</button>
      <button class="btn sm red" onclick="batchReview('reject')">🚫 批量驳回</button>
      <span style="flex:1"></span><span style="color:var(--mut);">筛选后 ${rows.length} 条</span>
    </div>` : '';
  const emptyMsg = `<div style="padding:12px;color:var(--mut);font-size:12px;">${
    _rev.seg==='candidate' ? '暂无待审实体（抽取候选确认入库后进入本队列，通过后状态为 reviewed）'
    : _rev.seg==='merge' ? '暂无合并建议（融合工作台生成候选对后自动进入本子分组）'
    : _rev.seg==='reviewed' ? '暂无已通过实体' : '暂无已驳回实体'}</div>`;
  if(!rows.length) { el.innerHTML = segBar + batchBar + emptyMsg; return; }
  const head = isActive
    ? '<tr><th>选</th><th>ID</th><th>名称</th><th>类型</th><th>来源</th><th>置信度</th><th>建议</th><th>操作</th></tr>'
    : '<tr><th>ID</th><th>名称</th><th>类型</th><th>来源</th><th>置信度</th><th>审核时间</th><th>操作</th></tr>';
  const body = items.map(e=>{
    const confCell = `<span style="font-size:11px;color:${(e.confidence||0)>=0.8?'var(--grn)':(e.confidence||0)>=0.6?'var(--blue-d)':'var(--amb)'};">${(e.confidence||0).toFixed(2)}</span>`;
    if(isActive){
      // P0-B：高置信建议（≥0.85 可直接通过）+ 重复簇提示（同名同类型，>1 即重复）
      const suggest = [];
      if((e.confidence||0)>=0.85) suggest.push('<span class="tag" style="border-color:var(--grn);color:var(--grn);font-size:10px;">高置信建议通过</span>');
      if((e.dup_count||0)>1) suggest.push(`<span class="tag" style="border-color:var(--amb);color:var(--amb);font-size:10px;cursor:pointer;" title="同名同类型实体共 ${e.dup_count} 条，建议合并或仅保留一条" onclick="showDupCluster('entity','${e.id}')">⚠ 同簇 ${e.dup_count} 条</span>`);
      if(_rev.mergeIds.has(e.id)) suggest.push('<span class="tag" style="border-color:var(--blue);color:var(--blue-d);font-size:10px;" title="融合工作台已生成合并候选">🔀 合并候选</span>');
      const suggestHtml = suggest.join(' ') || '-';
      const mergeBtn = _rev.mergeIds.has(e.id)
        ? `<button class="btn sm ghost" onclick="gotoFusionDup()">🔀 去融合</button>` : '';
      return `<tr>
      <td><input type="checkbox" class="rev-check" value="${e.id}"></td>
      <td>${e.id}</td><td><b>${e.name}</b></td><td>${e.entity_type}</td><td>${e.source_doc||e.source_type||'-'}</td>
      <td>${confCell}</td><td>${suggestHtml}</td>
      <td style="white-space:nowrap;"><button class="btn sm grn" onclick="approveEntity('${e.id}')">✅ 通过</button>
      <button class="btn sm red" onclick="rejectEntity('${e.id}')">🚫 驳回</button>
      ${mergeBtn}
      <button class="btn sm ghost" onclick="viewEntity('${e.id}')">📋 详情</button></td>
    </tr>`;
    }
    return `<tr>
      <td>${e.id}</td><td><b>${e.name}</b></td><td>${e.entity_type}</td><td>${e.source_doc||e.source_type||'-'}</td>
      <td>${confCell}</td><td style="font-size:11px;color:var(--mut);white-space:nowrap;">${esc((e.reviewed_at||e.created_at||'').slice(0,16))}</td>
      <td><button class="btn sm ghost" onclick="viewEntity('${e.id}')">📋 详情</button></td>
    </tr>`;
  }).join('');
  const foot = `<div style="padding:8px 10px;display:flex;gap:8px;align-items:center;font-size:11px;color:var(--mut);flex-wrap:wrap;">
      <span>${isActive ? '✅ 通过→reviewed 进入知识库正式实体；🚫 驳回→已驳回区（留痕可查）。' : '只读历史视图，可点详情查看实体快照。'}</span>
      <span style="flex:1"></span>
      <div id="rev-pager" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;"></div>
    </div>`;
  if(el) el.innerHTML = segBar + batchBar + `<table class="t">${head}${body}</table>` + foot;
  renderPagerBar({
    el: document.getElementById('rev-pager'), total: rows.length, page: _rev.page, size: _rev.size,
    onPage: p => { _rev.page = p; loadReviewQueue(); },
    onSize: s => { _rev.size = s; _rev.page = 1; loadReviewQueue(); }
  });
}
function revSetSeg(seg) {
  _rev.seg = seg; _rev.page = 1;
  loadReviewQueue();
}
function revApplyFilter() {
  _rev.kw = (document.getElementById('rev-search').value||'').trim();
  _rev.type = document.getElementById('rev-type').value;
  _rev.confMin = parseFloat(document.getElementById('rev-conf').value) || 0;
  _rev.page = 1;
  loadReviewQueue();
}
function revSegList() {
  if(_rev.seg==='merge') return _rev.list.filter(e=>_rev.mergeIds.has(e.id));
  return _rev.seg==='candidate' ? _rev.list : _rev.seg==='reviewed' ? _rev.reviewed : _rev.deprecated;
}
function gotoFusionDup() {
  // 从实体审核队列「🔀 去融合」跳到融合工作台灰区裁决区（P0 v2 双栏：右栏定位到 gray 队列）
  const el = Array.from(document.querySelectorAll('[data-tabgrp="kb2"]'))
    .find(t=>(t.getAttribute('onclick')||'').includes(`'fusion'`));
  if(el) switchKB2Tab(el, 'fusion');
  setTimeout(()=>{ fusNav(document.querySelector('.fus-nav-btn[data-fpane="terms"]'), 'terms'); }, 300);
}
function revFiltered() {
  return revSegList().filter(e=>{
    if(_rev.kw && !(e.name||'').toLowerCase().includes(_rev.kw.toLowerCase())) return false;
    if(_rev.type && e.entity_type!==_rev.type) return false;
    if(_rev.confMin>0 && (e.confidence||0) < _rev.confMin) return false;
    return true;
  });
}
// 单条审核（修复：此前按钮引用未定义的 approveEntity/rejectEntity，点击报错）
async function approveEntity(id) {
  if(!branchWritable()) return;
  if(!(await confirmDialog('确认通过该实体审核？（状态置为 reviewed，进入知识库正式实体）'))) return;
  const r = await api(`/api/knowledge/entities/${id}/review`, {method:'POST', body:JSON.stringify({action:'confirm'})});
  if(r.error) { toast('操作失败：'+r.error); return; }
  toast(`✅ 已通过 ${id}`);
  loadReviewQueue(); loadDuplicates(); loadKBStats();
}
async function rejectEntity(id) {
  if(!branchWritable()) return;
  if(!(await confirmDialog('确认驳回该实体？（状态置为 deprecated，进入已驳回区留痕）'))) return;
  const r = await api(`/api/knowledge/entities/${id}/review`, {method:'POST', body:JSON.stringify({action:'reject'})});
  if(r.error) { toast('操作失败：'+r.error); return; }
  toast(`🚫 已驳回 ${id}`);
  loadReviewQueue(); loadDuplicates(); loadKBStats();
}
// S4：批量审核（通过/驳回）
function revToggleAll(cb) {
  document.querySelectorAll('.rev-check').forEach(x=>{ x.checked = cb.checked; });
}
async function batchReview(action) {
  if(!branchWritable()) return;
  const ids = Array.from(document.querySelectorAll('.rev-check:checked')).map(x=>x.value);
  if(!ids.length) { toast('请勾选要审核的实体'); return; }
  if(action==='reject' && !(await confirmDialog(`批量驳回 ${ids.length} 条候选实体？`))) return;
  const r = await api('/api/knowledge/entities/batch-review', {method:'POST', body:JSON.stringify({entity_ids:ids, action})});
  toast(`${action==='confirm'?'✅':'🚫'} 批量${action==='confirm'?'通过':'驳回'} ${r.reviewed||0} 条`);
  loadReviewQueue();
  loadDuplicates();
}
// ── P0-A：关系审核队列（关系候选确认入库后 status=candidate，在此正式审核；与实体同级治理）──
let _rel = { candidate:[], reviewed:[], deprecated:[], seg:'candidate', kw:'', page:1, size:15 };
async function loadRelReview() {
  const el = document.getElementById('rel-review-queue');
  if(!el) return;
  try {
    const st = document.getElementById('rel-status') ? document.getElementById('rel-status').value : 'candidate';
    _rel.seg = st || 'candidate';
    const br = 'personal';   // 标注审核：关系审核队列仅与个人分支关联（审核后进入个人图数据库）
    const [cands, reviewed, deprecated, conflicts] = await Promise.all([
      api('/api/knowledge/graph/edges/review?status=candidate&limit=200&branch=' + br),
      api('/api/knowledge/graph/edges/review?status=reviewed&limit=200&branch=' + br),
      api('/api/knowledge/graph/edges/review?status=deprecated&limit=200&branch=' + br).catch(()=>({items:[]})),
      api('/api/knowledge/conflicts?status=pending&limit=100').catch(()=>[]),
    ]);
    _rel.candidate = (cands||{}).items||[];
    _rel.reviewed = (reviewed||{}).items||[];
    _rel.deprecated = (deprecated||{}).items||[];
    // 冲突待审项：同三元组矛盾关系值（内嵌提示 → 前往融合工作台裁决）
    const relConflicts = (conflicts||[]).filter(x=>x.kind==='relation_attr');
    const relChip = document.getElementById('rel-conflict-chip');
    if(relChip) relChip.style.display = relConflicts.length ? '' : 'none';
    if(relChip && relConflicts.length) relChip.textContent = `⚔️ 关系冲突待审 ${relConflicts.length}`;
    _rel.page = 1;
    const tabBadge = document.getElementById('rel-count-tab');
    if(tabBadge) tabBadge.textContent = `${_rel.candidate.length} 条`;
    const segs = [['candidate','⏳ 待审'],['reviewed','✅ 已通过'],['deprecated','🚫 已驳回']];
    const segBar = `<div style="padding:8px 10px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      ${segs.map(([k,l])=>`<span class="st ${_rel.seg===k?'b':'g'}" style="cursor:pointer;font-size:11px;${_rel.seg===k?'':'opacity:.7;'}" onclick="relSetSeg('${k}')">${l} <b>${k==='candidate'?_rel.candidate.length:k==='reviewed'?_rel.reviewed.length:_rel.deprecated.length}</b></span>`).join('')}
      <span style="flex:1"></span>
      <input id="rel-search" placeholder="🔍 搜索关系类型/端点…" oninput="relApplyFilter()" style="border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:12px;width:150px;">
      <button class="btn sm ghost" onclick="loadRelReview()">刷新</button>
    </div>`;
    const rows = relFiltered();
    const pages = Math.max(1, Math.ceil(rows.length / _rel.size));
    if(_rel.page > pages) _rel.page = pages;
    const items = rows.slice((_rel.page-1)*_rel.size, _rel.page*_rel.size);
    const batchBar = (_rel.seg==='candidate')
      ? `<div style="padding:6px 8px;display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line);font-size:11.5px;">
        <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" id="rel-check-all" onchange="relToggleAll(this)"> 全选</label>
        <button class="btn sm grn" onclick="batchRelReview('confirm')">✅ 批量通过</button>
        <button class="btn sm red" onclick="batchRelReview('reject')">🚫 批量驳回</button>
        <span style="flex:1"></span><span style="color:var(--mut);">筛选后 ${rows.length} 条</span>
      </div>` : '';
    if(!rows.length) {
      el.innerHTML = segBar + batchBar +
        `<div style="padding:12px;color:var(--mut);font-size:12px;">${
          _rel.seg==='candidate' ? '暂无待审关系（关系候选确认入库后进入本队列，通过后状态为 reviewed）'
          : _rel.seg==='reviewed' ? '暂无已通过关系' : '暂无已驳回关系'}</div>`;
      return;
    }
    const head = _rel.seg==='candidate'
      ? '<tr><th>选</th><th>源 —[关系]→ 目标</th><th>端点状态</th><th>来源</th><th>置信度</th><th>操作</th></tr>'
      : '<tr><th>源 —[关系]→ 目标</th><th>端点状态</th><th>来源</th><th>置信度</th><th>操作</th></tr>';
    const body = items.map(r=>{
      const triple = `<b>${esc(r.source_name||r.source_id)}</b> <span class="st b" style="font-size:10px;">${esc(r.relation_type)}</span> → <b>${esc(r.target_name||r.target_id)}</b>`;
      const srcSt = r.src_status==='reviewed'?'<span class="st ok" style="font-size:10px;">源✓</span>'
        : r.src_status==='deprecated'?'<span class="st r" style="font-size:10px;">源✗</span>'
        : '<span class="st w" style="font-size:10px;">源待审</span>';
      const tgtSt = r.tgt_status==='reviewed'?'<span class="st ok" style="font-size:10px;">目标✓</span>'
        : r.tgt_status==='deprecated'?'<span class="st r" style="font-size:10px;">目标✗</span>'
        : '<span class="st w" style="font-size:10px;">目标待审</span>';
      const confCell = `<span style="font-size:11px;color:${(r.confidence||0)>=0.8?'var(--grn)':'var(--amb)'};">${(r.confidence||0).toFixed(2)}</span>`;
      if(_rel.seg==='candidate'){
        // P0-B：重复簇提示（同三元组 >1 即重复）+ P0-C：一键合并
        const dupTag = (r.dup_count||0)>1
          ? `<span class="tag" style="border-color:var(--amb);color:var(--amb);font-size:10px;cursor:pointer;" title="同三元组关系共 ${r.dup_count} 条，建议合并或仅保留一条" onclick="showDupCluster('relation','${r.id}')">⚠ 同簇 ${r.dup_count} 条</span>` : '';
        const mergeBtn = (r.dup_count||0)>1
          ? `<button class="btn sm ghost" onclick="relMergeEdge(${r.id})">🔀 合并</button>` : '';
        return `<tr>
        <td><input type="checkbox" class="rel-check" value="${r.id}"></td>
        <td>${triple}${dupTag}</td><td>${srcSt} ${tgtSt}</td><td style="font-size:11px;color:var(--mut);">${esc(r.source_doc||'-')}</td>
        <td>${confCell}</td>
        <td style="white-space:nowrap;"><button class="btn sm grn" onclick="relReviewOne(${r.id},'confirm')">✅ 通过</button>
        <button class="btn sm red" onclick="relReviewOne(${r.id},'reject')">🚫 驳回</button>
        ${mergeBtn}
        <button class="btn sm ghost" onclick="relFixEdge(${r.id},'${esc(r.relation_type)}')">✏️ 修正</button>
        <button class="btn sm ghost" onclick="relEdgeDetail(${r.id})">📋 详情</button></td></tr>`;
      }
      return `<tr><td>${triple}</td><td>${srcSt} ${tgtSt}</td><td style="font-size:11px;color:var(--mut);">${esc(r.source_doc||'-')}</td>
        <td>${confCell}</td>
        <td><button class="btn sm ghost" onclick="relEdgeDetail(${r.id})">📋 详情</button></td></tr>`;
    }).join('');
    const foot = `<div style="padding:8px 10px;display:flex;gap:8px;align-items:center;font-size:11px;color:var(--mut);flex-wrap:wrap;">
      <span>✅ 通过→reviewed 进入图谱/发布；🚫 驳回→已驳回区（留痕可查）。两端实体须已入库（源/目标待审时边仍可审，发布时会被门禁拦截）。</span>
      <span style="flex:1"></span>
      <div id="rel-pager" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;"></div>
    </div>`;
    el.innerHTML = segBar + batchBar + `<table class="t">${head}${body}</table>` + foot;
    renderPagerBar({
      el: document.getElementById('rel-pager'), total: rows.length, page: _rel.page, size: _rel.size,
      onPage: p => { _rel.page = p; loadRelReview(); },
      onSize: s => { _rel.size = s; _rel.page = 1; loadRelReview(); }
    });
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
function relSetSeg(seg) {
  _rel.seg = seg; _rel.page = 1;
  // 同步 rel-status 下拉（两套状态控件联动，避免 loadRelReview 读取 select 覆盖 seg）
  const sel = document.getElementById('rel-status');
  if(sel) sel.value = seg;
  loadRelReview();
}
function relApplyFilter() {
  _rel.kw = (document.getElementById('rel-search').value||'').trim();
  _rel.page = 1;
  loadRelReview();
}
function relSegList() {
  return _rel.seg==='candidate' ? _rel.candidate : _rel.seg==='reviewed' ? _rel.reviewed : _rel.deprecated;
}
function relFiltered() {
  return relSegList().filter(r=>{
    if(!_rel.kw) return true;
    const k = _rel.kw.toLowerCase();
    const hay = `${r.relation_type||''} ${r.source_name||''} ${r.target_name||''} ${r.source_id||''} ${r.target_id||''}`.toLowerCase();
    return hay.includes(k);
  });
}
function relToggleAll(cb) { document.querySelectorAll('.rel-check').forEach(x=>{ x.checked = cb.checked; }); }
async function relReviewOne(eid, action) {
  if(!branchWritable()) return;
  const lbl = action==='confirm' ? '通过该关系审核？（状态置为 reviewed，进入图谱/发布）' : '驳回该关系？（状态置为 deprecated，进入已驳回区留痕）';
  if(!(await confirmDialog(lbl))) return;
  const r = await api(`/api/knowledge/graph/edges/${eid}/review`, {method:'POST', body:JSON.stringify({action})});
  if(r.error) { toast('操作失败：'+r.error); return; }
  toast(`${action==='confirm'?'✅':'🚫'} 关系 #${eid} 已${action==='confirm'?'通过':'驳回'}`);
  loadRelReview(); loadGraph && loadGraph();
}
async function batchRelReview(action) {
  if(!branchWritable()) return;
  const ids = Array.from(document.querySelectorAll('.rel-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要审核的关系'); return; }
  if(action==='reject' && !(await confirmDialog(`批量驳回 ${ids.length} 条关系？`))) return;
  let n = 0;
  for(const eid of ids){
    const r = await api(`/api/knowledge/graph/edges/${eid}/review`, {method:'POST', body:JSON.stringify({action})});
    if(!r.error) n++;
  }
  toast(`${action==='confirm'?'✅':'🚫'} 批量${action==='confirm'?'通过':'驳回'} ${n} 条`);
  loadRelReview(); loadGraph && loadGraph();
}
async function relFixEdge(eid, current) {
  if(!branchWritable()) return;
  const v = await promptDialog({title:'修正关系类型', message:'输入新的关系类型（修改后置回待审重新进入审核流）：', value:current||'', okText:'保存'});
  if(v===null || !v.trim()) return;
  const r = await api(`/api/knowledge/graph/edges/${eid}`, {method:'PUT', body:JSON.stringify({relation_type:v.trim()})});
  if(r.error) { toast('修正失败：'+r.error); return; }
  toast(`✅ 已修正关系 #${eid} → ${v.trim()}（重新进入审核流）`);
  loadRelReview(); loadGraph && loadGraph();
}
async function relEdgeDetail(eid) {
  const list = _rel.candidate.concat(_rel.reviewed).concat(_rel.deprecated);
  const r = list.find(x=>x.id===eid) || (await api('/api/knowledge/graph/edges/'+eid).catch(()=>null));
  if(!r) { toast('关系不存在'); return; }
  let props = {};
  try { props = JSON.parse(r.properties||'{}'); } catch(e){}
  const srcSt = {reviewed:'<span class="st ok">已入库✓</span>', candidate:'<span class="st w">待审</span>', deprecated:'<span class="st r">已废弃</span>'}[r.src_status]||'-';
  const tgtSt = {reviewed:'<span class="st ok">已入库✓</span>', candidate:'<span class="st w">待审</span>', deprecated:'<span class="st r">已废弃</span>'}[r.tgt_status]||'-';
  const html = `<h4 style="color:var(--blue-d);margin-bottom:10px;">关系 #${eid} · ${esc(r.relation_type)}</h4>
    <div class="kv"><span>源</span><b>${esc(r.source_name||r.source_id)} ${srcSt}</b></div>
    <div class="kv"><span>关系</span><b>${esc(r.relation_type)}</b></div>
    <div class="kv"><span>目标</span><b>${esc(r.target_name||r.target_id)} ${tgtSt}</b></div>
    <div class="kv"><span>状态</span><b><span class="st ${r.status==='reviewed'?'ok':r.status==='deprecated'?'r':'w'}">${r.status}</span></b></div>
    <div class="kv"><span>置信度</span><b>${(r.confidence||0).toFixed(2)}</b></div>
    <div class="kv"><span>来源文档</span><b>${esc(r.source_doc||'-')}</b></div>
    <div class="kv"><span>创建时间</span><b>${esc((r.created_at||'').slice(0,16))}</b></div>
    <h4 style="margin:12px 0 6px;">属性</h4>
    <table class="t"><tr><th>键</th><th>值</th></tr>${Object.entries(props).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join('')||'<tr><td colspan="2">无</td></tr>'}</table>`;
  openPanel(`关系详情 · ${esc(r.relation_type)}`, html);
}
// ── P0-B：重复簇详情（实体：同名同类型；关系：同三元组），keep 建议高亮 ──
async function showDupCluster(kind, id) {
  const list = kind==='entity' ? revSegList() : relSegList();
  const cur = list.find(x=>String(x.id)===String(id));
  if(!cur || !(cur.dup_ids||[]).length){ toast('无同簇成员'); return; }
  const keepId = String(cur.dup_keep_id || cur.id);
  const rows = [];
  for(const mid of cur.dup_ids){
    let label = mid, status = '', isKeep = String(mid)===keepId;
    try{
      if(kind==='entity'){
        const e = await api('/api/knowledge/entities/'+mid);
        if(e && !e.error){ label = e.name; status = e.status; }
      } else {
        const pool = _rel.candidate.concat(_rel.reviewed).concat(_rel.deprecated);
        const r = pool.find(x=>String(x.id)===String(mid));
        if(r) label = `${r.source_name||r.source_id} —[${r.relation_type}]→ ${r.target_name||r.target_id}`;
        status = r ? (r.status||'') : '';
      }
    }catch(err){}
    rows.push({id:mid, label, status, isKeep});
  }
  const lbx = document.getElementById('lbx');
  const b = document.getElementById('lbx-body');
  const body = rows.map(r=>`<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid ${r.isKeep?'var(--grn)':'var(--line)'};border-radius:6px;margin-bottom:6px;background:#fff;">
      <b style="font-size:12.5px;flex:1;">${esc(r.label)}</b>
      ${r.isKeep?'<span class="tag" style="font-size:10px;border-color:var(--grn);color:var(--grn);">keep 建议</span>':''}
      <span style="font-size:11px;color:var(--mut);">${esc(r.id)} · ${esc(r.status||'')}</span>
    </div>`).join('');
  b.innerHTML = `<h3>${kind==='entity'?'🔀 实体重复簇（同名同类型）':'🔀 关系重复簇（同三元组）'}（${rows.length} 条）</h3>
    <div style="font-size:12px;color:var(--mut);margin:4px 0 8px;">keep 建议 = 已入库(reviewed)优先；可在对应审核队列对该项执行「🔀 合并」。</div>
    <div style="max-height:240px;overflow:auto;">${body}</div>
    <div class="lbx-actions"><button class="btn" onclick="document.getElementById('lbx').classList.remove('show')">关闭</button></div>`;
  lbx.classList.add('show');
}
// ── P0-C：关系重复簇一键合并（dup → keep；keep 建议 reviewed 优先）──
async function relMergeEdge(id) {
  if(!branchWritable()) return;
  const list = relSegList();
  const r = list.find(x=>String(x.id)===String(id));
  if(!r){ toast('关系不存在'); return; }
  const keepId = r.dup_keep_id || r.id;
  const toMerge = (r.dup_ids||[]).filter(x=>String(x)!==String(keepId));
  if(!toMerge.length){ toast('无可合并的重复关系'); return; }
  if(!(await confirmDialog(`将同簇 ${toMerge.length} 条重复关系合并到边 #${keepId}？\n属性/来源文档并入保留边，重复边废弃留痕`))) return;
  let n = 0, errs = [];
  for(const did of toMerge){
    const res = await api('/api/knowledge/graph/edges/merge', {method:'POST', body:JSON.stringify({keep_id:keepId, dup_id:did})});
    if(res.error){ errs.push(did+':'+res.error); } else n++;
  }
  toast(`✅ 合并 ${n} 条重复关系` + (errs.length?`（失败 ${errs.length}：${errs[0]}）`:''));
  loadRelReview(); if(window.loadGraph) loadGraph();
}
// ── 抽取治理中心（P0/P1：批次卡 + 检索筛选 + 分页 + 来源溯源 + 单条编辑/驳回 + 消歧警示 + 关系引导 + 质量反馈）──
let _v2g = { cands:[], batches:[], batch:'', kw:'', types:[], doc:'', status:'pending', matching:'', confMin:0, relOnly:false, rejOnly:false, group:'entity', page:1, size:15, entityNames:new Set(), batchStatus:'pending', batchKw:'', batchTime:'', batchesExpanded:false, advExpanded:false };
