/* 图谱工作区 v2（2026-09-09 P0）：搜索补全 / 三元组 Tab / 推理 Tab / 推断叠加 / 跨 Tab 联动
 * 全局作用域（非 module），内联 onclick 依赖全局函数名。
 * 后端：routers/graph_workspace.py（entity-search / triples/browse / reasoning/run / materialize / inferred）
 */

// ═══════════════ A. 顶部全局搜索（2026-09-10 三模式：实体 / NL / SPARQL）═══════════════
let _gwtSearchTimer = null, _gwtSearchItems = [];
const GWT_SEARCH_PH = {
  entity: '🔍 搜索实体（名称 / ID / 类型）',
  nl:     '🗣 自然语言检索，如“载荷相关的需求”，回车执行',
  sparql: 'SPARQL：SELECT ?s ?p ?o WHERE { … } LIMIT 10',
};
function gwtSearchMode(){
  const sel = document.getElementById('gv-search-mode');
  return (sel && sel.value) || 'entity';
}
function gwtSearchModeChange(){
  const inp = document.getElementById('gv-search'); if(!inp) return;
  inp.placeholder = GWT_SEARCH_PH[gwtSearchMode()] || GWT_SEARCH_PH.entity;
  gwtSearchClose();
  if(gwtSearchMode()==='sparql'){ inp.value=''; }
}
function gwtSearchInput(){
  const inp = document.getElementById('gv-search'); if(!inp) return;
  graphState._searchQ = inp.value;
  clearTimeout(_gwtSearchTimer);
  const q = inp.value.trim();
  if(!q){ gwtSearchClose(); return; }
  const mode = gwtSearchMode();
  if(mode==='sparql') return;                        // SPARQL：回车执行，不做逐字查询
  _gwtSearchTimer = setTimeout(()=>{ mode==='nl' ? gwtNlSearch(q) : gwtDoSearch(q); }, 300);
}
function gwtSearchClose(){ const dd=document.getElementById('gv-search-dd'); if(dd) dd.style.display='none'; }
async function gwtDoSearch(q){
  const dd = document.getElementById('gv-search-dd'); if(!dd) return;
  try{
    const r = await api('/api/knowledge/graph/entity-search?q='+encodeURIComponent(q)
      +'&branch='+encodeURIComponent(getCurrentBranch())+'&limit=12');
    _gwtSearchItems = r.items||[];
    if(!_gwtSearchItems.length){
      dd.innerHTML='<div style="padding:8px 10px;font-size:11px;color:var(--mut);">无匹配实体</div>';
      dd.style.display='block'; return;
    }
    dd.innerHTML = _gwtSearchItems.map((it,i)=>`<div class="gv-dd-item" style="padding:6px 10px;font-size:11.5px;cursor:pointer;display:flex;gap:6px;align-items:center;" onclick="gwtSearchPick(${i})">
      <span style="width:8px;height:8px;border-radius:50%;background:${graphColor(it.entity_type)};flex:none;border:1px solid rgba(0,0,0,.1);"></span>
      <b>${esc(it.name)}</b><span style="color:var(--mut);font-size:10px;">${esc(it.entity_type||'')} · ${esc(it.branch||'')}</span></div>`).join('');
    dd.style.display='block';
  }catch(e){ gwtSearchClose(); }
}
// NL 模式：优先后端 NL→SPARQL 图问答（需 graph_db.enabled）；未启用/失败/0 行时
// 自动降级为当前分支实体关键词匹配（图库依赖同步链，实体库实时——保证一定搜得到）
async function gwtNlSearch(q){
  const dd = document.getElementById('gv-search-dd'); if(!dd) return;
  dd.innerHTML = '<div style="padding:8px 10px;font-size:11px;color:var(--mut);">检索中…</div>';
  dd.style.display = 'block';
  try{
    const r = await api('/api/graph-db/nlquery', {method:'POST', body:JSON.stringify({query:q, limit:10})});
    if((r.count||0) > 0){ gwtRenderRows(dd, r, 'NL→SPARQL 图问答'); return; }
    // 0 行：图库未命中（可能未同步）→ 自动降级当前分支实体搜索
  }catch(e){ /* graph_db 未启用或翻译失败 → 降级 */ }
  try{
    const r = await api('/api/knowledge/graph/entity-search?q='+encodeURIComponent(q)
      +'&branch='+encodeURIComponent(getCurrentBranch())+'&limit=12');
    _gwtSearchItems = r.items||[];
    const head = '<div style="padding:5px 10px;font-size:10px;color:var(--mut);border-bottom:1px solid var(--line);">当前分支「'+esc(getCurrentBranch())+'」实体匹配</div>';
    dd.innerHTML = head + (!_gwtSearchItems.length
      ? '<div style="padding:8px 10px;font-size:11px;color:var(--mut);">当前分支无匹配实体</div>'
      : _gwtSearchItems.map((it,i)=>`<div class="gv-dd-item" style="padding:6px 10px;font-size:11.5px;cursor:pointer;display:flex;gap:6px;align-items:center;" onclick="gwtSearchPick(${i})">
        <span style="width:8px;height:8px;border-radius:50%;background:${graphColor(it.entity_type)};flex:none;border:1px solid rgba(0,0,0,.1);"></span>
        <b>${esc(it.name)}</b><span style="color:var(--mut);font-size:10px;">${esc(it.entity_type||'')}</span></div>`).join(''));
    dd.style.display = 'block';
  }catch(e){
    dd.innerHTML = '<div style="padding:8px 10px;font-size:11px;color:var(--mut);">检索失败：'+esc(e.message||e)+'</div>';
  }
}
// SPARQL 模式：回车执行只读 SELECT，结果表内联展示 + 跳转完整编辑器入口
async function gwtSparqlQuick(){
  const inp = document.getElementById('gv-search'); if(!inp) return;
  const q = inp.value.trim(); if(!q) return;
  const dd = document.getElementById('gv-search-dd'); if(!dd) return;
  dd.innerHTML = '<div style="padding:8px 10px;font-size:11px;color:var(--mut);">SPARQL 查询中…</div>';
  dd.style.display='block';
  try{
    const r = await api('/api/graph-db/query', {method:'POST', body:JSON.stringify({query:q, limit:10})});
    gwtRenderRows(dd, r, 'SPARQL 只读查询');
  }catch(e){
    dd.innerHTML = '<div style="padding:8px 10px;font-size:11px;color:#A32D2D;">❌ '+esc(e.message||e)
      +'<div style="margin-top:4px;"><span style="color:var(--blue);cursor:pointer;text-decoration:underline;" onclick="gwtSearchClose();window.open(\'/static/sparql_playground.html\',\'_blank\')">→ 打开 SPARQL Playground</span></div></div>';
  }
}
// 行集渲染（nlquery / query 共用）：自适应列名表格
function gwtRenderRows(dd, r, label){
  const rows = (r && (r.rows||r.results||r.data)) || [];
  const cols = rows.length ? Object.keys(rows[0]) : [];
  dd.innerHTML = `<div style="padding:5px 10px;font-size:10px;color:var(--mut);border-bottom:1px solid var(--line);">${esc(label||'查询')} · ${rows.length} 行${(r&&r.count&&r.count>rows.length)?'（截断）':''}</div>`
    + (!rows.length ? '<div style="padding:8px 10px;font-size:11px;color:var(--mut);">无结果</div>'
    : `<div style="overflow-x:auto;"><table style="border-collapse:collapse;font-size:10.5px;min-width:260px;">
        <tr>${cols.map(c=>`<th style="text-align:left;padding:3px 8px;background:#fafaf7;border-bottom:1px solid var(--line);color:var(--blue-d);white-space:nowrap;">${esc(c)}</th>`).join('')}</tr>
        ${rows.slice(0,10).map(row=>`<tr>${cols.map(c=>`<td style="padding:3px 8px;border-bottom:1px solid #f0efe9;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(String(row[c]??''))}">${esc(String(row[c]??''))}</td>`).join('')}</tr>`).join('')}
      </table></div>`);
}
function gwtSearchKey(ev){
  if(ev.key==='Enter'){
    ev.preventDefault();
    const mode = gwtSearchMode();
    const inp = ev.target;
    if(mode==='sparql'){ gwtSparqlQuick(); return; }
    if(mode==='nl'){ const q=(inp.value||'').trim(); if(q) gwtNlSearch(q); return; }
    if(_gwtSearchItems.length) gwtSearchPick(0);
  }
  if(ev.key==='Escape') gwtSearchClose();
}
// 画布工具栏「🔍 搜索」入口 / 全局「/」快捷键 → 聚焦顶部搜索框（2026-09-10 二次修正）
function gwtFocusSearch(){
  if(typeof wsCurTab!=='undefined' && wsCurTab!=='content') wsTab('content');
  const inp = document.getElementById('gv-search');
  if(!inp){ if(typeof toast==='function') toast('搜索框未就绪，请刷新页面'); return; }
  try{ inp.scrollIntoView({block:'nearest'}); }catch(e){}
  inp.focus(); try{ inp.select(); }catch(e){}
  inp.style.boxShadow = '0 0 0 3px rgba(56,120,220,.20)';
  setTimeout(()=>{ inp.style.boxShadow = ''; }, 1000);
}
if(!window._gwtSearchHotkeyBound){
  window._gwtSearchHotkeyBound = true;
  document.addEventListener('keydown', function(ev){
    if(ev.key!=='/' || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const t = ev.target;
    if(t && (t.tagName==='INPUT' || t.tagName==='TEXTAREA' || t.isContentEditable)) return;
    if(document.getElementById('pg-kb') && !document.getElementById('pg-kb').classList.contains('on')) return;
    ev.preventDefault();
    gwtFocusSearch();
  });
}
async function gwtSearchPick(i){
  const it = _gwtSearchItems[i]; gwtSearchClose();
  if(!it) return;
  if(typeof wsCurTab!=='undefined' && wsCurTab!=='content') wsTab('content');   // 搜索框常驻顶部：定位前先切回图谱 Tab
  if(!(graphState.all.nodes||[]).find(n=>n.id===it.id)){
    graphState.view.status='all';               // 被状态筛选隐藏 → 放宽后重载
    await loadGraph();
  }
  if(!(graphState.all.nodes||[]).find(n=>n.id===it.id)){ toast('该实体不在当前分支图谱中（可能在其他分支）'); return; }
  if(!graphState.nodes.find(n=>n.id===it.id)){  // 被类型筛选隐藏 → 清类型筛选
    graphState.view.types=[]; applyGraphViewAndRender();
  }
  gvTreeSelEntity(it.id);
}

const gwtR = {kinds:{classify:true, transitive:true, symmetric:false, reflexive:false, consistency:true},
  last:null, filterKind:'all', focusQ:'', mat:null, showMat:false, inited:false,
  page:1, pageSize:50, ignored:new Set(), queued:new Set(), sel:new Set()};   // 分页 + 行级取舍 + 批量勾选（2026-09-14）
const GWT_KIND_LABEL = {classify:'分类', transitive:'传递', symmetric:'对称', reflexive:'自反', consistency:'一致性'};
const GWT_KIND_COLOR = {classify:'#6B4FA1', transitive:'#185FA5', symmetric:'#0F6E56', reflexive:'#BA7517'};
function gwtReasoningInit(){
  const el = document.getElementById('ws-reasoning'); if(!el) return;
  el.innerHTML = `
  <div style="display:flex;flex-direction:column;height:100%;">
    <div style="display:flex;gap:10px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);background:#fafaf7;flex-wrap:wrap;">
      <b style="color:var(--blue-d);font-size:12px;">🧠 本体推理</b>
      ${Object.keys(gwtR.kinds).map(k=>`<label style="display:inline-flex;align-items:center;gap:4px;font-size:11.5px;cursor:pointer;" title="${{classify:'子类实例亦属于父类（沿类型层级）',transitive:'包含/连接/追溯等可传递关系的 BFS 闭包',symmetric:'公理标记对称的关系反向推断',reflexive:'公理标记自反的关系自环补齐',consistency:'类型合法性 + 必填继承 + 关系 domain/range'}[k]}">
        <input type="checkbox" ${gwtR.kinds[k]?'checked':''} onchange="gwtR.kinds.${k}=this.checked"> ${GWT_KIND_LABEL[k]}
      </label>`).join('')}
      <button class="btn sm" onclick="gwtReasoningRun()">▶ 执行推理</button>
      <span style="border-left:1px solid var(--line);height:18px;"></span>
      <button class="btn sm ghost" onclick="gwtReasoningOverlay()" title="把推断关系以橙色虚线叠加到图谱画布" id="gwt-overlay-btn" disabled>🕸 叠加到图谱</button>
      <button class="btn sm ghost" onclick="gwtReasoningMaterialize()" title="数据流：推断三元组写入暂存（status=inferred）并生成审核批次，审核确认后才真正并入图库（实线）" id="gwt-mat-btn" disabled>📤 提交审核</button>
      <button class="btn sm ghost" id="gwt-queue-btn" onclick="gwtOpenQueue()" title="数据流：推理→提交审核→✅确认（幂等并入图库，实线显示）/🚫驳回（不落图）。支持批次级与单条级并入/忽略；批次条目可逐条取舍">🧭 审核队列</button>
      <span style="flex:1"></span>
      <span id="gwt-r-stats" style="font-size:10.5px;color:var(--mut);"></span>
    </div>
    <div id="gwt-r-body" style="flex:1;overflow:auto;padding:10px 14px;">
      <div style="color:var(--mut);font-size:12px;padding:24px;text-align:center;">
        勾选推理项 → 点击「▶ 执行推理」对当前全库图谱执行扫描<br>
        <span style="font-size:10.5px;">推断结果不落库，可「叠加到图谱」可视化或「物化入库」进入三元组原子表</span></div>
    </div>
  </div>`;
  gwtR.inited = true;
  if(gwtR.last) gwtReasoningRenderResults();
}
async function gwtReasoningRun(){
  const body = document.getElementById('gwt-r-body'); if(!body) return;
  const kinds = Object.keys(gwtR.kinds).filter(k=>gwtR.kinds[k]);
  if(!kinds.length){ toast('至少勾选一项推理'); return; }
  body.innerHTML = '<div class="loading">推理引擎扫描全库图谱中…</div>';
  try{
    const r = await api('/api/knowledge/reasoning/run', {method:'POST', body:JSON.stringify({kinds, branch:(typeof getCurrentBranch==='function'?getCurrentBranch():'')})});
    gwtR.last = r;
    gwtR.directMerge = !!r.direct_merge;   // 简化模式：行级并入直接入图（跳过审核队列）
    gwtR.page = 1;                    // 新一轮推理回到第 1 页
    gwtR.ignored = new Set();         // 行级取舍状态随新一轮重置
    gwtR.queued = new Set();
    gwtR.sel = new Set();             // 批量勾选状态随新一轮重置
    // 门禁按钮随开关显隐：直接并入模式隐藏「提交审核」「审核队列」
    ['gwt-mat-btn','gwt-queue-btn'].forEach(id=>{
      const b = document.getElementById(id);
      if(b) b.style.display = gwtR.directMerge ? 'none' : '';
    });
    document.getElementById('gwt-overlay-btn').disabled = !(r.inferred||[]).some(t=>t.kind!=='classify');
    document.getElementById('gwt-mat-btn').disabled = !(r.inferred||[]).length;
    gwtReasoningRenderResults();
  }catch(e){ body.innerHTML = '<div style="color:#A32D2D;font-size:12px;padding:16px;">推理失败：'+esc(e.message||e)+'</div>'; }
}
function gwtEntName(idOrUri){
  const id = gwtUri2id(idOrUri) || idOrUri;
  const n = (graphState.all.nodes||[]).find(x=>x.id===id);
  return {id, name: n ? n.name : id};
}
// 推理结果人读化（2026-09-14）：优先用后端权威 id→名称映射（gwtR.last.names），
// 覆盖图谱视图未加载到的实体；ex: 类型引用剥壳；fallback 短 id（不再露出长 URI）。
function gwtName(x){
  const s = String(x||'').trim();
  if(!s) return '';
  const names = (gwtR.last&&gwtR.last.names)||{};
  let id = s.startsWith('ex:') ? s.slice(3) : (gwtUri2id(s) || s);
  if(names[s]) return names[s];
  if(names[id]) return names[id];
  const n = (graphState.all.nodes||[]).find(v=>v.id===id);
  return n ? n.name : id;
}
function gwtPrettyPred(p){
  p = String(p||'').trim();
  if(p==='a' || p==='type') return 'type（类型声明）';
  return p.replace(/^ex:/,'');
}
// 传递路径解码：'URI → URI → URI' → '泵A → 管路B → 冷板C'
function gwtDecodePath(p){
  const s = String(p||'').trim();
  if(!s || s==='—') return '—';
  return s.split('→').map(x=>gwtName(x)).join(' → ');
}
// 一致性检查问题项 → 跳图谱定位该实体（2026-09-15 强化：不再依赖 gvTreeSelEntity 的静默早退，
// 层层保障直到节点处于选中态）：①当前视图直选 ②放宽状态筛选重载 ③清类型筛选重渲染 ④跨分支提示
// 选中四件套 = 画布节点高亮(sel) + 视图居中 + 右栏详情 + 左栏树行高亮滚动
async function gwtLocateEntity(uri){
  const id = gwtUri2id(uri) || String(uri||'').trim();
  if(!id){ toast('无法解析该问题项的实体标识'); return; }
  wsTab('content');
  const inAll = () => (graphState.all.nodes||[]).find(n=>n.id===id);
  const inShow = () => graphState.nodes.find(n=>n.id===id);
  // ① 数据保障：不在全量集 → 放宽状态筛选 + 清类型筛选后重载
  if(!inAll()){
    try{
      graphState.view.status = 'all';
      graphState.view.types = [];
      await loadGraph();
    }catch(e){ /* 重载失败继续走兜底 */ }
  }
  // ② 跨分支兜底：查后端给出"实体在哪个分支"的准确提示
  if(!inAll()){
    try{
      const r = await api('/api/knowledge/graph/entity-search?q=' + encodeURIComponent(id));
      const hit = (r.items||[]).find(x=>x.id===id);
      if(hit){
        toast(`实体「${hit.name||id}」在分支「${hit.branch||'?'}」上，当前分支「${getCurrentBranch()}」不可见——左上角切换到该分支即可定位`, 4000);
      }else{
        toast('图库中未找到该实体（可能已被删除或下线）');
      }
    }catch(e){ toast('该实体不在当前图谱视图中，请检查分支与筛选条件'); }
    return;
  }
  // ③ 显示集保障：被状态/类型筛选隐藏 → 放宽状态 + 清类型筛选后重渲染
  if(!inShow()){
    graphState.view.status = 'all';
    graphState.view.types = [];
    applyGraphViewAndRender();
  }
  if(!inShow()){ toast('实体已找到，但仍被视图筛选隐藏（请检查 ⚙ 筛选面板）'); return; }
  // ④ 选中四件套（gvTreeSelEntity 同款，但去掉静默早退）
  graphState.sel = {type:'node', id};
  renderGraph();
  gvCenterOnNode(id);
  gvTreeLocate(id);
  showGraphDetail(id);
  const _n = inShow();
  toast('🎯 已定位并选中：' + (_n ? (_n.name||id) : id));
}
// 检查卡片说明与处理指引（name 对齐 ontology_reasoning.consistency_check / run 端点）
const GWT_CHECK_GUIDE = {
  '分类推理':   {what:'沿本体「子类→父类」层级，把子类的实例也推断为父类的实例'},
  '传递推理':   {what:'对标记了「传递」公理的关系（如 包含/连接/追溯）做闭包，推断出间接关系（A→B、B→C ⇒ A→C）'},
  '对称推理':   {what:'对标记了「对称」公理的关系自动推断反向边（A 关联 B ⇒ B 关联 A）'},
  '自反推理':   {what:'对标记了「自反」公理的关系补齐自环（A 关联 A）'},
  '一致性检查':        {what:'校验每个实例的类型是否已在本体中声明', fix:'① 在「本体模型」中声明该类型；② 或修正实体的类型为本体已有类型'},
  '基数验证（必填继承）': {what:'本体标记为「必填」的属性，实例必须赋值（必填定义沿父类继承）', fix:'在实体详情中编辑属性，补齐缺失的必填值'},
  '关系类型校验':      {what:'关系的两端类型必须符合本体定义的 domain/range（谁能有这条关系、连向谁）', fix:'① 调整关系两端的实体类型；② 或修正本体中该关系的 domain/range 定义'},
};
function gwtUri2id(u){
  u = String(u||'');
  const NS = 'http://www.xingwang.mbse/ent/';
  if(u.startsWith(NS)){ const f=u.slice(NS.length); try{return decodeURIComponent(f);}catch(e){return f;} }
  if(u.startsWith('urn:mbse:ent:')){ const r=u.slice(13); return r.includes(':')?r.slice(r.lastIndexOf(':')+1):r; }
  return '';
}
function gwtReasoningRenderResults(){
  const body = document.getElementById('gwt-r-body'); if(!body) return;
  const r = gwtR.last; if(!r) return;
  const checks = r.checks||[], inferred = r.inferred||[];
  const stats = r.stats||{};
  document.getElementById('gwt-r-stats').textContent =
    Object.entries(stats).filter(([k,v])=>v).map(([k,v])=>`${GWT_KIND_LABEL[k]||k}:${v}`).join(' · ')
    + (inferred.length?` · 推断合计 ${inferred.length}`:'');
  const kinds = [...new Set(inferred.map(t=>t.kind))];
  const filtered = inferred.filter(t=>(gwtR.filterKind==='all'||t.kind===gwtR.filterKind)
    && !gwtR.ignored.has(inferred.indexOf(t))
    && (!gwtR.focusQ || JSON.stringify(t).toLowerCase().includes(gwtR.focusQ.toLowerCase())
        || gwtEntName(t.s).name.includes(gwtR.focusQ) || gwtEntName(t.o).name.includes(gwtR.focusQ)));
  // 分页（2026-09-14）：页码越界自动收敛；筛选/新推理时重置为第 1 页
  const totalPages = Math.max(1, Math.ceil(filtered.length / gwtR.pageSize));
  gwtR.page = Math.min(Math.max(1, gwtR.page||1), totalPages);
  const pgStart = (gwtR.page - 1) * gwtR.pageSize;
  const pageRows = filtered.slice(pgStart, pgStart + gwtR.pageSize);
  // 旧后端自检（2026-09-14）：响应缺 names 字段 / 告警卡缺结构化 errors → 后端代码未重启，明说而不是让用户猜
  const oldBackend = !gwtR.last || !('names' in gwtR.last)
    || checks.some(c=>!c.pass && !('errors' in c));
  body.innerHTML = `
  <div style="display:flex;flex-direction:column;gap:10px;">
    ${oldBackend?`<div style="border:1px solid #E5B9B9;background:#FDF3F3;border-radius:8px;padding:8px 12px;font-size:12px;color:#A32D2D;line-height:1.6;">
      ⚠️ 检测到旧版后端响应（缺名称映射/结构化错误项）：请<b>重启后端服务</b>并<b>强刷页面（Ctrl+F5）</b>后重新执行推理，才有中文名解码、逐条定位与处理指引。</div>`:''}
    <div style="font-size:10.5px;color:var(--mut);">📖 列说明：<b>推理</b>=推断种类（分类/传递/对称/自反）· <b>主语/谓词/宾语</b>=推断出的三元组（<b>type</b>=类型声明，不生成连线）· <b>推导依据</b>=传递关系的中间路径链。每行可单条「✅ 并入 / ✕ 忽略」，或整批「📤 提交审核」。扫描范围：<b>当前分支</b>${typeof getCurrentBranch==='function'?`「${esc(getCurrentBranch())}」`:''}。</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px;">
      ${checks.map(c=>{
        const guide = GWT_CHECK_GUIDE[c.name]||{};
        const errs = c.errors||[];
        const many = errs.length > 5;   // 2026-09-14：明细多时折叠+滚动区，卡片不再被撑爆
        const errId = 'gwt-errs-' + c.name;
        return `<div style="border:1px solid ${c.pass?'#B7D6A8':'#E5B9B9'};background:${c.pass?'#F4FAEF':'#FDF3F3'};border-radius:8px;padding:8px 10px;">
        <div style="font-weight:700;font-size:12px;color:${c.pass?'#3B6D11':'#A32D2D'};" title="${esc(guide.what||'')}">${c.pass?'✅':'⚠️'} ${esc(c.name)}${!c.pass&&errs.length?` <span style="font-weight:400;font-size:10.5px;color:#A32D2D;">（${errs.length} 条问题）</span>`:''}</div>
        <div style="font-size:10.5px;color:#667;margin-top:3px;line-height:1.5;">${esc(guide.what||'')}</div>
        <div style="font-size:11px;color:#555;margin-top:4px;line-height:1.6;">${esc(c.detail||'')}</div>
        ${!c.pass && errs.length?`
          <div id="${esc(errId)}" style="margin-top:6px;border-top:1px dashed #E5B9B9;padding-top:5px;${many?'max-height:130px;overflow-y:auto;':''}">
            ${errs.map(e=>`<div style="font-size:10.5px;color:#8A3B3B;line-height:1.7;display:flex;gap:6px;align-items:baseline;padding:1px 0;">
              <span style="flex:1;">• ${esc(e.text||'')}</span>
              <a style="color:var(--blue);cursor:pointer;font-size:10px;white-space:nowrap;" onclick="gwtLocateEntity('${esc(e.ent||'')}')" title="跳到图谱定位该实体">🕸 定位</a>
            </div>`).join('')}
            ${errs.length>=50?'<div style="font-size:10px;color:var(--mut);">（仅列出前 50 条）</div>':''}
          </div>
          ${many?`<a style="display:inline-block;margin-top:3px;color:var(--blue);cursor:pointer;font-size:10px;" onclick="(function(b,a){var open=b.style.maxHeight==='none';b.style.maxHeight=open?'130px':'none';a.textContent=open?('▼ 展开全部 '+${errs.length}+' 条'):'▲ 收起';})(document.getElementById('${esc(errId)}'),this)">▼ 展开全部 ${errs.length} 条</a>`:''}
        `:''}
        ${!c.pass && guide.fix?`<div style="font-size:10.5px;color:#7A5A10;background:#FBF3DC;border-radius:5px;padding:4px 6px;margin-top:6px;line-height:1.5;">🛠 怎么处理：${esc(guide.fix)}</div>`:''}
      </div>`;}).join('')}
    </div>
    ${inferred.length?`
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <b style="font-size:12px;color:var(--blue-d);">推断三元组（${filtered.length}/${inferred.length}）</b>
      <select onchange="gwtR.filterKind=this.value;gwtR.page=1;gwtReasoningRenderResults()" style="border:1px solid var(--line);border-radius:6px;padding:2px 6px;font-size:11px;">
        <option value="all" ${gwtR.filterKind==='all'?'selected':''}>全部类型</option>
        ${kinds.map(k=>`<option value="${k}" ${gwtR.filterKind===k?'selected':''}>${GWT_KIND_LABEL[k]||k}</option>`).join('')}
      </select>
      <input placeholder="过滤（实体名/路径）…" value="${esc(gwtR.focusQ)}" onkeydown="if(event.key==='Enter'){gwtR.focusQ=this.value;gwtR.page=1;gwtReasoningRenderResults();}" style="border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:11px;width:160px;">
      <span style="flex:1;"></span>
      <span style="display:inline-flex;gap:6px;align-items:center;font-size:11px;color:var(--mut);">已选 <b id="gwt-sel-n" style="color:var(--blue-d);">${gwtR.sel.size}</b> 条</span>
      <button class="btn sm grn" style="font-size:11px;padding:2px 10px;" ${gwtR.sel.size?'':'disabled'} onclick="gwtBatchMerge()" title="把勾选的推断批量${gwtR.directMerge?'直接幂等并入图库':'提交审核（追加到当前待审批次）'}">✅ 批量并入</button>
      <button class="btn sm ghost" style="font-size:11px;padding:2px 10px;" ${gwtR.sel.size?'':'disabled'} onclick="gwtBatchIgnore()" title="本次结果中批量忽略勾选条目">✕ 批量忽略</button>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:11.5px;">
      <thead><tr style="background:var(--blue-l);color:var(--blue-d);text-align:left;">
        <th style="padding:5px 4px;border-bottom:1px solid var(--line);width:26px;"><input type="checkbox" title="全选/取消本页" onchange="gwtSelAllPage(this.checked)"></th>
        <th style="padding:5px 8px;border-bottom:1px solid var(--line);">推理</th>
        <th style="padding:5px 8px;border-bottom:1px solid var(--line);">主语</th>
        <th style="padding:5px 8px;border-bottom:1px solid var(--line);">谓词</th>
        <th style="padding:5px 8px;border-bottom:1px solid var(--line);">宾语</th>
        <th style="padding:5px 8px;border-bottom:1px solid var(--line);">推导依据</th>
        <th style="padding:5px 8px;border-bottom:1px solid var(--line);">操作</th>
      </tr></thead>
      <tbody>${pageRows.map(t=>{
        const idx = inferred.indexOf(t);
        const isCls = t.kind==='classify';
        const sName = gwtName(t.s), oName = isCls ? gwtName(t.o) : gwtName(t.o);
        const viaTxt = isCls ? '类型层级继承（子类实例 ⇒ 父类型）' : gwtDecodePath(t.path||t.via);
        const opCell = gwtR.queued.has(idx)
          ? '<span style="color:var(--grn);font-size:10.5px;white-space:nowrap;">✓ 已并入图库</span>'
          : `<button class="btn sm grn" style="font-size:10.5px;padding:1px 7px;" onclick="gwtRowSubmit(${idx})" title="${gwtR.directMerge?'直接幂等并入图库（跳过审核）':'单条提交审核：写入暂存并追加到当前待审批次，确认后入图'}">✅ 并入</button>
             <button class="btn sm ghost" style="font-size:10.5px;padding:1px 7px;" onclick="gwtRowIgnore(${idx})" title="本次结果中忽略该条">✕ 忽略</button>`;
        const chk = gwtR.queued.has(idx)
          ? '<span style="color:var(--mut);font-size:10px;" title="已并入">✓</span>'
          : `<input type="checkbox" ${gwtR.sel.has(idx)?'checked':''} onchange="gwtRowSel(${idx}, this.checked)">`;
        return `<tr style="border-bottom:1px solid #f0efe9;">
        <td style="padding:4px 4px;text-align:center;">${chk}</td>
        <td style="padding:4px 8px;"><span style="color:${GWT_KIND_COLOR[t.kind]||'#888'};font-weight:600;">${GWT_KIND_LABEL[t.kind]||t.kind}</span></td>
        <td style="padding:4px 8px;font-weight:600;" title="${esc(t.s)}">${esc(sName)}</td>
        <td style="padding:4px 8px;color:var(--blue-d);">${esc(gwtPrettyPred(t.p))}</td>
        <td style="padding:4px 8px;" title="${esc(t.o)}">${esc(oName)}</td>
        <td style="padding:4px 8px;color:var(--mut);font-size:10px;word-break:break-all;" title="${esc(t.path||t.via||'')}">${esc(viaTxt)}</td>
        <td style="padding:4px 8px;white-space:nowrap;">${opCell}</td>
      </tr>`;}).join('')}</tbody></table>
    <div style="display:flex;gap:10px;align-items:center;font-size:11px;color:var(--mut);flex-wrap:wrap;">
      <span>共 ${filtered.length} 条 · 第 ${gwtR.page}/${totalPages} 页（每页 ${gwtR.pageSize} 条）</span>
      <button class="btn sm ghost" style="font-size:11px;padding:1px 8px;" ${gwtR.page<=1?'disabled':''} onclick="gwtR.page--;gwtReasoningRenderResults()">← 上一页</button>
      <button class="btn sm ghost" style="font-size:11px;padding:1px 8px;" ${gwtR.page>=totalPages?'disabled':''} onclick="gwtR.page++;gwtReasoningRenderResults()">下一页 →</button>
      <span>每页</span>
      <select onchange="gwtR.pageSize=+this.value||50;gwtR.page=1;gwtReasoningRenderResults()" style="border:1px solid var(--line);border-radius:6px;padding:1px 4px;font-size:11px;">
        ${[50,100,200].map(n=>`<option value="${n}" ${gwtR.pageSize===n?'selected':''}>${n}</option>`).join('')}
      </select>
      <span>条</span>
    </div>`
    :'<div style="color:var(--mut);font-size:12px;padding:10px;">本次推理无新增推断（图谱可能已完备，或本体未标记相关公理）</div>'}
  </div>`;
}
// 行级单条操作（2026-09-14）：
// direct_merge=true（默认）→ ✅ 并入 = 直接幂等写入图库（跳过审核队列，审计留痕）
// direct_merge=false → ✅ 并入 = 提交审核（追加到当前待审批次，队列确认后入图）
// ✕ 忽略 = 本次结果中隐藏
async function gwtRowSubmit(idx){
  const t = ((gwtR.last||{}).inferred||[])[idx]; if(!t) return;
  const dm = gwtR.directMerge;
  const url = dm ? '/api/knowledge/reasoning/direct-merge' : '/api/knowledge/reasoning/materialize';
  const body = dm
    ? {inferred:[t], branch:(typeof getCurrentBranch==='function'?getCurrentBranch():'')}
    : {inferred:[t], append_pending:true};
  try{
    const res = await api(url, {method:'POST', body:JSON.stringify(body)});
    if(res.error){ toast((dm?'并入失败：':'提交失败：')+(res.error||'')); return; }
    gwtR.queued.add(idx);
    if(dm){
      const n = ((res.entities||[]).length + (res.relations||[]).length) || res.written || 0;
      toast(`✅ 已直接并入图库（${res.skipped?'重复跳过 '+res.skipped+'，':' '}图谱刷新后实线显示）`);
    }else{
      toast(`📤 已提交审核（批次 #${res.cohort_id}${res.skipped?'，重复跳过 '+res.skipped:''}）；到「🧭 审核队列」确认后入图`);
    }
  }catch(e){ toast('操作失败：'+(e.message||e)); return; }
  gwtReasoningRenderResults();
}
function gwtRowIgnore(idx){
  gwtR.ignored.add(idx);
  gwtReasoningRenderResults();
}
// ── 批量操作（2026-09-14）：勾选 → 批量并入 / 批量忽略 ──
function gwtRowSel(idx, on){
  if(on) gwtR.sel.add(idx); else gwtR.sel.delete(idx);
  const n = document.getElementById('gwt-sel-n');
  if(n) n.textContent = gwtR.sel.size;
  const bar = document.getElementById('gwt-batchbar');
  if(bar){
    bar.querySelectorAll('button').forEach(b=>{ if(!b.id.startsWith('gwt-selall')) b.disabled = !gwtR.sel.size; });
  }
}
function gwtSelAllPage(on){
  // 对当前页可见行全选/全不选（已并入的行除外）
  const r = gwtR.last; if(!r) return;
  const inferred = r.inferred||[];
  document.querySelectorAll('#gwt-r-body tbody input[type="checkbox"]').forEach(cb=>{
    const m = /gwtRowSel\((\d+)/.exec(cb.getAttribute('onchange')||'');
    if(!m) return;
    const idx = +m[1];
    if(on && !gwtR.queued.has(idx)) gwtR.sel.add(idx);
    if(!on) gwtR.sel.delete(idx);
    cb.checked = on && !gwtR.queued.has(idx);
  });
  const n = document.getElementById('gwt-sel-n');
  if(n) n.textContent = gwtR.sel.size;
}
async function gwtBatchMerge(){
  const inferred = ((gwtR.last||{}).inferred)||[];
  const idxs = [...gwtR.sel].filter(i=>!gwtR.queued.has(i));
  const triples = idxs.map(i=>inferred[i]).filter(Boolean);
  if(!triples.length){ toast('请先勾选要并入的条目'); return; }
  const dm = gwtR.directMerge;
  const url = dm ? '/api/knowledge/reasoning/direct-merge' : '/api/knowledge/reasoning/materialize';
  const body = dm
    ? {inferred:triples, branch:(typeof getCurrentBranch==='function'?getCurrentBranch():'')}
    : {inferred:triples, append_pending:true};
  try{
    const res = await api(url, {method:'POST', body:JSON.stringify(body)});
    if(res.error){ toast((dm?'批量并入失败：':'批量提交失败：')+(res.error||'')); return; }
    idxs.forEach(i=>gwtR.queued.add(i));
    gwtR.sel.clear();
    if(dm){
      const n = ((res.entities||[]).length + (res.relations||[]).length) || res.written || 0;
      toast(`✅ 批量并入完成：${triples.length} 条（写入 ${n}${res.skipped?'，重复跳过 '+res.skipped:''}）；图谱刷新后实线显示`);
    }else{
      toast(`📤 批量提交审核：${triples.length} 条（批次 #${res.cohort_id}${res.skipped?'，重复跳过 '+res.skipped:''}）；到「🧭 审核队列」确认后入图`);
    }
  }catch(e){ toast('批量操作失败：'+(e.message||e)); return; }
  gwtReasoningRenderResults();
}
function gwtBatchIgnore(){
  if(!gwtR.sel.size){ toast('请先勾选要忽略的条目'); return; }
  gwtR.sel.forEach(i=>gwtR.ignored.add(i));
  const n = gwtR.sel.size;
  gwtR.sel.clear();
  toast(`✕ 已忽略 ${n} 条（仅本次结果中隐藏，重新推理可恢复）`);
  gwtReasoningRenderResults();
}
function gwtReasoningOverlay(){
  const r = gwtR.last; if(!r) return;
  const edges = gwtBuildInfEdges(r.inferred||[]);
  if(!edges.length){ toast('没有可叠加的推断关系（分类推断为类型声明，不生成边）'); return; }
  graphState._infEdges = edges;
  wsTab('content');
  applyGraphViewAndRender(); gvFitView();
  toast(`⇢ 已叠加 ${edges.length} 条推断边（橙色虚线）；清除：推理 Tab → ✕ 清除叠加`);
  gvStatusUpdate();
}
function gwtBuildInfEdges(inferred){
  const out = [];
  let i = 0;
  inferred.forEach(t=>{
    if(t.kind==='classify') return;                 // 类型声明不画边
    const sid = gwtUri2id(t.s), tid = gwtUri2id(t.o);
    if(!sid||!tid) return;
    if(sid===tid && t.kind!=='reflexive') return;   // 自环仅自反推理保留
    out.push({id:'inf:'+(i++), source_id:sid, target_id:tid, relation_type:t.p,
      _inferred:1, _kind:GWT_KIND_LABEL[t.kind]||t.kind, _via:t.path||t.via||'',
      _sName:gwtEntName(t.s).name, _tName:gwtEntName(t.o).name});
  });
  return out;
}
function gwtClearInferred(){
  graphState._infEdges = [];
  applyGraphViewAndRender();
  toast('已清除推断叠加');
  gvStatusUpdate();
}
async function gwtReasoningMaterialize(){
  const r = gwtR.last; if(!r||!(r.inferred||[]).length) return;
  // 审核门禁：先弹影响预览
  const relN = r.inferred.filter(t=>t.kind!=='classify').length;
  const entN = r.inferred.length - relN;
  const ok = await confirmDialog(
    `本次推断影响预览：\n${r.inferred.length} 条推断三元组\n预计 ${entN} 条实体类型声明 · ${relN} 条关系边\n\n确认后写入 triples 原子表（status=inferred）并进入「推理审核队列」，经确认后幂等并入图库。`,
    {okText:'进入审核队列', title:'物化·审核门禁确认'});
  if(!ok) return;
  try{
    const res = await api('/api/knowledge/reasoning/materialize', {method:'POST',
      body:JSON.stringify({inferred:r.inferred})});
    if(res.error){ toast('物化失败：'+(res.error||'')); return; }
    toast(`💾 已进入审核队列：新写入 ${res.written} 条，跳过重复 ${res.skipped} 条`);
    gwtOpenQueue();
  }catch(e){ toast('物化失败：'+(e.message||e)); }
}

// ═══════════════ 推理审核队列（物化→审核门禁→并入图库）═══════════════
const GWT_COHORT_ST = {pending:['待审核','var(--blue)'], approved:['已确认并入','var(--grn)'], rejected:['已驳回','#A32D2D']};
async function gwtOpenQueue(){
  const body = document.getElementById('gwt-r-body'); if(!body) return;
  body.innerHTML = '<div class="loading">加载推理审核队列…</div>';
  await gwtRenderQueue();
}
async function gwtRenderQueue(){
  const body = document.getElementById('gwt-r-body'); if(!body) return;
  let rows = [];
  try{
    const r = await api('/api/knowledge/reasoning/cohorts');
    rows = r.rows||[];
  }catch(e){
    body.innerHTML = '<div style="color:#A32D2D;font-size:12px;padding:16px;">加载审核队列失败：'+esc(e.message||e)+'</div>';
    return;
  }
  body.innerHTML = `
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;flex-wrap:wrap;">
    <b style="font-size:12px;color:var(--blue-d);">🧭 推理审核队列（${rows.length}）</b>
    <button class="btn sm ghost" onclick="gwtRenderQueue()" title="刷新队列">🔄 刷新</button>
    <button class="btn sm ghost" onclick="gwtReasoningRenderResults()">⬅ 返回推理结果</button>
  </div>
  <div style="font-size:10.5px;color:var(--mut);margin-bottom:8px;">数据流：推理结果 →「📤 提交审核」写入暂存并生成批次 → 在此逐条取舍（✅ 并入 / ✕ 忽略）或整批 <b style="color:var(--grn);">✅ 确认</b>（幂等并入图库，实线显示）/<b style="color:#A32D2D;">🚫 驳回</b>（不落图）。点批次行「📋 条目」展开单条操作。</div>
  ${!rows.length
    ?`<div style="color:var(--mut);font-size:12px;padding:20px;text-align:center;">暂无待审核批次<br><span style="font-size:10.5px;">推理完成后点「📤 提交审核」即进入本审核队列</span></div>`
    :`<table style="width:100%;border-collapse:collapse;font-size:11.5px;">
    <thead><tr style="background:var(--blue-l);color:var(--blue-d);text-align:left;">
      <th style="padding:5px 8px;border-bottom:1px solid var(--line);">批次</th>
      <th style="padding:5px 8px;border-bottom:1px solid var(--line);">状态</th>
      <th style="padding:5px 8px;border-bottom:1px solid var(--line);">并入实体/关系</th>
      <th style="padding:5px 8px;border-bottom:1px solid var(--line);">创建人/时间</th>
      <th style="padding:5px 8px;border-bottom:1px solid var(--line);">操作</th>
    </tr></thead><tbody>
    ${rows.map(c=>{
      const [slabel, scolor] = GWT_COHORT_ST[c.status]||['未知','#888'];
      return `<tr style="border-bottom:1px solid #f0efe9;">
      <td style="padding:4px 8px;">#${c.id}<div style="color:var(--mut);font-size:10px;">${c.item_count||0} 条推断</div></td>
      <td style="padding:4px 8px;"><span style="color:${scolor};font-weight:600;">${slabel}</span>${c.note?`<div style="color:var(--mut);font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(c.note)}">${esc(c.note)}</div>`:''}</td>
      <td style="padding:4px 8px;">${c.status==='approved'?`<b style="color:var(--grn);">${c.entity_count||0} 实体 · ${c.relation_count||0} 关系</b>`:'<span style="color:var(--mut);">—</span>'}</td>
      <td style="padding:4px 8px;color:var(--mut);font-size:10.5px;">${esc(c.created_by||'')}<br>${esc((c.created_at||'').slice(5,16))}</td>
      <td style="padding:4px 8px;white-space:nowrap;">${gwtQueueActions(c)}</td>
      </tr>`;
    }).join('')}
    </tbody></table>`}
  <div id="gwt-q-items"></div>
  `;
}
// 批次条目展开（行级审核）：✅ 并入 / ✕ 忽略，逐条取舍不必整批操作
let _gwtQItemsFor = 0;
async function gwtQueueItems(cid){
  const box = document.getElementById('gwt-q-items'); if(!box) return;
  if(_gwtQItemsFor === cid){ _gwtQItemsFor = 0; box.innerHTML = ''; return; }   // 再点收起
  box.innerHTML = '<div class="loading">加载批次条目…</div>';
  let d;
  try{ d = await api('/api/knowledge/reasoning/cohorts/' + cid); }
  catch(e){ box.innerHTML = '<div style="color:#A32D2D;font-size:12px;padding:12px;">加载失败：'+esc(e.message||e)+'</div>'; return; }
  if(d.error){ box.innerHTML = '<div style="color:#A32D2D;font-size:12px;padding:12px;">'+esc(d.error)+'</div>'; return; }
  _gwtQItemsFor = cid;
  const items = d.items || [];
  const stMap = {pending:['待定','var(--mut)'], approved:['✅ 已并入','var(--grn)'], rejected:['✕ 已忽略','#A32D2D']};
  box.innerHTML = `
  <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:8px;background:#fcfcf9;">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap;">
      <b style="font-size:11.5px;color:var(--blue-d);">📋 批次 #${cid} 条目（${items.length}）</b>
      <span style="font-size:10.5px;color:var(--mut);">✅ 并入 = 幂等写入图库（实线）；✕ 忽略 = 该条不再落图</span>
    </div>
    ${!items.length ? '<div style="color:var(--mut);font-size:11.5px;padding:8px;">该批次无条目</div>' : `
    <table style="width:100%;border-collapse:collapse;font-size:11.5px;">
      <thead><tr style="background:var(--blue-l);color:var(--blue-d);text-align:left;">
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">推理</th>
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">主语</th>
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">谓词</th>
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">宾语</th>
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">依据</th>
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">状态</th>
        <th style="padding:4px 8px;border-bottom:1px solid var(--line);">操作</th>
      </tr></thead>
      <tbody>${items.map(it=>{
        const st = it.status||'pending';
        const [sl, sc] = stMap[st]||['待定','var(--mut)'];
        return `<tr style="border-bottom:1px solid #f0efe9;${st==='rejected'?'opacity:.55;':''}">
        <td style="padding:3px 8px;"><span style="color:${GWT_KIND_COLOR[it.kind]||'#888'};font-weight:600;">${GWT_KIND_LABEL[it.kind]||esc(it.kind||'-')}</span></td>
        <td style="padding:3px 8px;font-weight:600;" title="${esc(it.s)}">${esc(it.s_name||'')}</td>
        <td style="padding:3px 8px;color:var(--blue-d);">${esc(it.p==='a'||it.p==='type'?'type':esc(it.p))}</td>
        <td style="padding:3px 8px;" title="${esc(it.o)}">${esc(it.o_name||'')}</td>
        <td style="padding:3px 8px;color:var(--mut);font-size:10px;word-break:break-all;" title="${esc(it.via||'')}">${esc((it.via||'—').slice(0,60))}</td>
        <td style="padding:3px 8px;color:${sc};font-weight:600;white-space:nowrap;">${sl}</td>
        <td style="padding:3px 8px;white-space:nowrap;">${st==='pending'
          ? `<button class="btn sm grn" style="font-size:10.5px;padding:1px 7px;" onclick="gwtItemApprove(${cid},${it.id})" title="单条幂等并入图库">✅ 并入</button>
             <button class="btn sm ghost" style="font-size:10.5px;padding:1px 7px;" onclick="gwtItemReject(${cid},${it.id})" title="忽略该条，不再落图">✕ 忽略</button>`
          : '<span style="color:var(--mut);font-size:10.5px;">—</span>'}</td>
      </tr>`;}).join('')}</tbody>
    </table>`}
  </div>`;
}
async function gwtItemApprove(cid, iid){
  try{
    const res = await api(`/api/knowledge/reasoning/cohorts/${cid}/items/${iid}/approve`, {method:'POST', body:'{}'});
    if(res.error){ toast('并入失败：'+res.error); return; }
    const n = (res.entities||[]).length + (res.relations||[]).length;
    toast(res.idempotent ? '该条目已并入过' : `✅ 已并入图库：新增 ${n} 项（实线显示）`);
  }catch(e){ toast('并入失败：'+(e.message||e)); return; }
  gwtQueueItems(cid);
}
async function gwtItemReject(cid, iid){
  try{
    const res = await api(`/api/knowledge/reasoning/cohorts/${cid}/items/${iid}/reject`, {method:'POST', body:'{}'});
    if(res.error){ toast('忽略失败：'+res.error); return; }
    toast('✕ 已忽略该条，不再落图');
  }catch(e){ toast('忽略失败：'+(e.message||e)); return; }
  gwtQueueItems(cid);
}
function gwtQueueActions(c){
  const itemsBtn = `<button class="btn sm ghost" style="font-size:10.5px;padding:1px 7px;" onclick="gwtQueueItems(${c.id})" title="展开批次条目，逐条 ✅ 并入 / ✕ 忽略">📋 条目</button> `;
  if(c.status==='pending'){
    return `${itemsBtn}<button class="btn sm grn" onclick="gwtQueueApprove(${c.id})" title="确认并入图库（整批）">✅ 确认</button>
            <button class="btn sm red" onclick="gwtQueueReject(${c.id})" title="驳回，需填原因（整批不落图）">🚫 驳回</button>`;
  }
  if(c.status==='approved'){
    return `${itemsBtn}<a style="color:var(--blue);cursor:pointer;font-size:11px;" onclick="gwtQueueView(${c.id})">📋 本次并入 ${c.entity_count||0} 实体 · ${c.relation_count||0} 关系</a>
            <button class="btn sm ghost" onclick="gwtOverlayCohort(${c.id})" title="把该批确认并入的边叠加高亮到图谱">🕸 叠加</button>`;
  }
  return `${itemsBtn}<button class="btn sm ghost" onclick="gwtQueueView(${c.id})" title="查看该批次详情">👁 查看</button>`;
}
async function gwtQueueApprove(id){
  const c = (await api('/api/knowledge/reasoning/cohorts')).rows||[];
  const it = c.find(x=>x.id===id);
  const ok = await confirmDialog(
    `确认把推断批次 #${id} 并入图库？\n该批 ${it?it.item_count||0:''} 条推断将幂等落为正式实体/关系（当前分支：${esc(getCurrentBranch())}）。`, 
    {okText:'确认并入', title:'审核确认'});
  if(!ok) return;
  try{
    const res = await api(`/api/knowledge/reasoning/cohorts/${id}/approve`, {method:'POST',
      body:JSON.stringify({branch:getCurrentBranch()})});
    if(res.error){ toast('并入失败：'+(res.error||'')); return; }
    toast(`✅ 已确认并入：新增 ${res.entity_count||0} 实体 · ${res.relation_count||0} 关系`);
    await loadGraph();                 // 刷新图谱并入的新边（实线）
    await gwtRenderQueue();
    gwtQueueView(id);                  // 展示本次并入清单
  }catch(e){ toast('并入失败：'+(e.message||e)); }
}
async function gwtQueueReject(id){
  const note = await promptDialog({title:'驳回原因', multiline:true,
    message:'请输入驳回原因（不少于 2 字），被拒项将不再落图。',
    placeholder:'如：该推理结论与现有设计约束冲突', okText:'确认驳回'});
  if(note==null) return;
  if(String(note||'').trim().length < 2){ toast('驳回原因至少 2 个字'); return; }
  try{
    const res = await api(`/api/knowledge/reasoning/cohorts/${id}/reject`, {method:'POST',
      body:JSON.stringify({note: String(note).trim()})});
    if(res.error){ toast('驳回失败：'+(res.error||'')); return; }
    toast('🚫 已驳回该批次，推断不再落图');
    await gwtRenderQueue();
  }catch(e){ toast('驳回失败：'+(e.message||e)); }
}
async function gwtQueueView(id){
  let d;
  try{ d = await api(`/api/knowledge/reasoning/cohorts/${id}`); }
  catch(e){ toast('加载失败：'+(e.message||e)); return; }
  if(!d.ok){ toast((d.error||'加载失败')); return; }
  const ents = d.entities||[], rels = d.relations||[];
  const body = document.getElementById('gwt-r-body'); if(!body) return;
  body.innerHTML = `
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
    <b style="font-size:12px;color:var(--blue-d);">📋 本次并入 #${id} · ${ents.length} 实体 · ${rels.length} 关系</b>
    <button class="btn sm ghost" onclick="gwtOverlayCohort(${id})" title="把该批确认并入的边叠加高亮到图谱">🕸 叠加到图谱</button>
    <button class="btn sm ghost" onclick="gwtRenderQueue()">⬅ 返回审核队列</button>
  </div>
  ${ents.length?`<div style="font-weight:700;font-size:12px;color:var(--blue-d);margin:6px 0 4px;">新增实体</div>
   <table style="width:100%;border-collapse:collapse;font-size:11.5px;">
    <thead><tr style="background:var(--blue-l);color:var(--blue-d);text-align:left;"><th style="padding:5px 8px;">名称</th><th style="padding:5px 8px;">类型</th><th style="padding:5px 8px;">ID</th></tr></thead>
    <tbody>${ents.map(e=>`<tr style="border-bottom:1px solid #f0efe9;"><td style="padding:4px 8px;font-weight:600;">${esc(e.name)}</td><td style="padding:4px 8px;"><span style="color:${graphColor(e.entity_type)};">●</span> ${esc(e.entity_type)}</td><td style="padding:4px 8px;color:var(--mut);font-size:10.5px;" title="${esc(e.id)}">${esc(e.id)}</td></tr>`).join('')}</tbody>
   </table>`:''}
  ${rels.length?`<div style="font-weight:700;font-size:12px;color:var(--blue-d);margin:10px 0 4px;">新增关系</div>
   <table style="width:100%;border-collapse:collapse;font-size:11.5px;">
    <thead><tr style="background:var(--blue-l);color:var(--blue-d);text-align:left;"><th style="padding:5px 8px;">源</th><th style="padding:5px 8px;">谓词</th><th style="padding:5px 8px;">目标</th></tr></thead>
    <tbody>${rels.map(r=>`<tr style="border-bottom:1px solid #f0efe9;"><td style="padding:4px 8px;font-weight:600;">${esc(r.source||r.source_name||'')}</td><td style="padding:4px 8px;color:var(--blue-d);">${esc(r.predicate||r.name||'')}</td><td style="padding:4px 8px;">${esc(r.target||r.target_name||'')}</td></tr>`).join('')}</tbody>
   </table>`:''}
  ${!ents.length && !rels.length?'<div style="color:var(--mut);font-size:12px;padding:16px;">该批次无新增实体/关系（可能在其它分支已存在，或均为重叠项）</div>':''}
  `;
}
// 可选：把「本次并入」的边叠加高亮到图谱（复用既有橙色虚线叠加机制）
async function gwtOverlayCohort(id){
  let d;
  try{ d = await api(`/api/knowledge/reasoning/cohorts/${id}`); }catch(e){ toast('加载失败：'+(e.message||e)); return; }
  const rels = d.relations||[];
  const edges = [];
  const name2id = {};
  (graphState.all.nodes||[]).forEach(n=>{ name2id[n.name]=n.id; });
  rels.forEach((r, i)=>{
    const sName = r.source || r.source_name || '';
    const tName = r.target || r.target_name || '';
    const sid = r.source_id || name2id[sName];
    const tid = r.target_id || name2id[tName];
    if(!sid || !tid) return;
    edges.push({id:'cohort:'+i, source_id:sid, target_id:tid,
      relation_type:(r.predicate||r.name||''), _inferred:1, _kind:'本批并入',
      _via:'', _sName:sName, _tName:tName, _cohort:1});
  });
  if(!edges.length){ toast('该批次没有可叠加的关系边'); return; }
  graphState._infEdges = edges;
  wsTab('content');
  applyGraphViewAndRender(); gvFitView();
  toast(`⇢ 已叠加本批并入 ${edges.length} 条关系边（橙色虚线高亮）`);
  gvStatusUpdate();
}
// 2026-09-14：「📦 待审推断」独立视图已并入「🧭 审核队列」的批次条目展开（gwtQueueItems），
// 原初始 gwtReasoningLoadMat / gwtReasoningOverlayMat（triples 暂存池视角）随入口一并移除。

// ═══════════════ D. 跨 Tab 联动 ═══════════════
window.gwtReasoningPanelShown = false;
function gwtToggleReasoning(forceShow){
  const el = document.getElementById('ws-reasoning'); if(!el) return;
  const show = (typeof forceShow==='boolean') ? forceShow : !window.gwtReasoningPanelShown;
  // 推理面板与工作区 Tab 统一联动（2026-09-12 入口收口）：打开时收起图谱/三元组等内容 Tab，收起时回到图谱
  if(typeof wsTab==='function' && typeof wsCurTab!=='undefined'){
    wsTab(show ? 'reasoning' : 'content');
  } else {
    el.style.display = show ? 'block' : 'none';
  }
  if(show && typeof gwtReasoningInit==='function' && gwtR && !gwtR.inited) gwtReasoningInit();
  window.gwtReasoningPanelShown = show;
}
function gwtGotoReasoning(name){
  gwtToggleReasoning(true);
  gwtR.focusQ = name||'';
  gwtReasoningInit();
  gwtReasoningRun();
}

// ═══════════════ E. 既有函数扩展（包装，零侵入原实现）═══════════════
// E-1：graphApplyView 尾部追加推断边叠加（仅非骨架模式；与原始边按 S|T|P 去重）
if(typeof graphApplyView==='function'){
  const _origGAV = graphApplyView;
  graphApplyView = function(){
    _origGAV();
    const inf = graphState._infEdges||[];
    if(inf.length && !graphState.sk.on){
      const ids = new Set(graphState.nodes.map(n=>n.id));
      const exist = new Set(graphState.edges.map(e=>e.source_id+'|'+e.target_id+'|'+e.relation_type));
      graphState.edges = graphState.edges.concat(inf.filter(e=>
        ids.has(e.source_id)&&ids.has(e.target_id)&&!exist.has(e.source_id+'|'+e.target_id+'|'+e.relation_type)));
    }
  };
}
// E-2：实例详情 sticky 头部注入「🧠 推理」跨 Tab 联动按钮
if(typeof showGraphDetail==='function'){
  const _origSGD = showGraphDetail;
  showGraphDetail = function(id){
    _origSGD(id);
    const body = document.getElementById('gv-detail-body');
    const sticky = body && body.querySelector('.ip-sticky');
    if(sticky && !sticky.querySelector('.gv-xlinks')){
      const n = (graphState.all.nodes||[]).find(x=>x.id===id);
      const nm = n ? n.name : '';
      sticky.insertAdjacentHTML('beforeend', `<div class="gv-xlinks" style="display:flex;gap:6px;margin-top:6px;">
        <button class="btn sm ghost" style="font-size:11px;padding:2px 8px;" onclick="gwtGotoReasoning('${esc(nm)}')" title="跳推理面板执行推理，过滤该实体相关推断">🧠 推理</button>
        ${(graphState._infEdges||[]).length?`<button class="btn sm ghost" style="font-size:11px;padding:2px 8px;color:#E8912D;" onclick="gwtClearInferred()" title="清除图谱上的推断边叠加">✕ 清除叠加</button>`:''}
      </div>`);
    }
  };
}

// ═══════════════ F. 统计收敛（2026-09-10 米爸裁剪后）═══════════════
// 原顶部推理开关（gv-infer-switch）与画布底部状态栏（gv-statusbar）均已删除：
//   - 推理叠加唯一入口 = 「推理」Tab 内「叠加到图谱 / 清除叠加」按钮
//   - 统计唯一入口 = 画布左上 gv-stat-chip（节点 · 边 · 推断），由 gvUpdateStatusPill 为主更新
// gvStatusUpdate 保留为空安全同步点（多处调用），职责收敛为刷新左上统计徽标
function gvStatusUpdate(){
  if(typeof gvUpdateStatusPill==='function') gvUpdateStatusPill();
}
