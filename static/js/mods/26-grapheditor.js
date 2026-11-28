/* 图谱编辑：节点 / 边 / 视图 / 导入面板
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 14364-15244  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function renderEntityTree(){
  const box = document.getElementById('gv-ent-tree');
  if(!box) return;
  await gvEnsureTypes();
  const nodes = graphState.all.nodes||[];
  const q = '';   // 2026-09-10 米爸裁剪：左栏过滤框已删（右上全局搜索覆盖），过滤逻辑保留结构、q 恒空
  const hitOf = list => list;
  const byType = {};
  nodes.forEach(n=>{ const t=n.entity_type||'未分类'; (byType[t]=byType[t]||[]).push(n); });
  const items = (typeof ontTypeTree==='function')
    ? ontTypeTree('entity')
    : (ontData.types||[]).filter(t=>t.type_kind==='entity').map(t=>({t,depth:0}));
  const seen = new Set();
  let html = '';
  items.forEach(({t,depth})=>{
    seen.add(t.name);
    const hit = hitOf(byType[t.name]||[]);
    const agg = [...gvTypeFamily(t.name)].reduce((s,fm)=>s+(hitOf(byType[fm]||[])).length,0);  // 继承归组（含子类型）——仅用于父类型显示/聚焦判定
    if(agg===0) return;                            // 零实例类型（含子树无实例）不出现在树中
    if(q && !hit.length && agg===0) return;
    const collapsed = gvTreeSt.collapsed.has(t.name) && !q;
    const fam = [...gvTypeFamily(t.name)];
    const vts = graphState.view.types || [];
    const focused = graphState.sk.on ? graphState.sk.drill===t.name
      : (vts.length>0 && fam.every(x=>vts.includes(x)) && vts.every(x=>fam.includes(x)));
    html += `<div class="ont-tnd" style="padding-left:8px;${focused?'background:var(--blue-l,#E6F1FB);font-weight:600;':''}" onclick="gvTreeTypeFocus('${esc(t.name)}')" title="类型 ${esc(t.name)}：点击行在图上查看该类型（含子类型）${focused?'（当前已聚焦，再点恢复全图）':''}；点击 ▸/▾ 展开或折叠实体列表">
      <span style="cursor:pointer;color:var(--mut);font-size:11.5px;width:12px;display:inline-block;flex:none;text-align:center;border-radius:3px;" onclick="event.stopPropagation();gvTreeToggle('${esc(t.name)}')" title="展开/折叠「${esc(t.name)}」下的实体列表">${collapsed?'▸':'▾'}</span>
      <span style="width:9px;height:9px;border-radius:50%;background:${graphColor(t.name)};display:inline-block;flex:none;"></span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(t.name)}</span>
      <span class="tag" style="font-size:12px;" title="直接实例 ${hit.length}${agg>hit.length?'（含子类型共 '+agg+'）':''}">${hit.length}</span></div>`;
    if(collapsed) return;
    html += gvTreeRowsFor(hit, depth);
  });
  // 未挂到本体类型的分组（entity_type 无类型定义 / 树外孤儿）兜底展示
  Object.keys(byType).filter(t=>!seen.has(t)).forEach(t=>{
    const hit = hitOf(byType[t]);
    if(!hit.length) return;                         // 零实例分组不展示
    const collapsed = gvTreeSt.collapsed.has(t) && !q;
    const vts = graphState.view.types || [];
    const focused = graphState.sk.on ? graphState.sk.drill===t
      : (vts.length>0 && vts.length===1 && vts[0]===t);
    html += `<div class="ont-tnd" style="padding-left:8px;${focused?'background:var(--blue-l,#E6F1FB);font-weight:600;':''}" onclick="gvTreeTypeFocus('${esc(t)}')" title="分组 ${esc(t)}：点击行在图上查看该类型${focused?'（当前已聚焦，再点恢复全图）':''}；点击 ▸/▾ 展开或折叠">
      <span style="cursor:pointer;color:var(--mut);font-size:11.5px;width:12px;display:inline-block;flex:none;text-align:center;border-radius:3px;" onclick="event.stopPropagation();gvTreeToggle('${esc(t)}')" title="展开/折叠「${esc(t)}」下的实体列表">${collapsed?'▸':'▾'}</span>
      <span style="width:9px;height:9px;border-radius:50%;background:${graphColor(t)};display:inline-block;flex:none;"></span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(t)}</span>
      <span class="tag" style="font-size:12px;" title="实例数">${hit.length}</span></div>`;
    if(collapsed) return;
    html += gvTreeRowsFor(hit, 0);
  });
  box.innerHTML = html || '<div style="color:var(--mut);font-size:12.5px;padding:6px;">当前分支暂无实体</div>';
  const cnt = document.getElementById('gv-tree-count');
  if(cnt) cnt.textContent = nodes.length ? nodes.length+' 个' : '';
}
// 组合父子树（2026-09-12）：同一类型分组内按 COMPOSED_OF/CONTAINS 边递归展开子实例
const gvTreeParentRels = ['COMPOSED_OF','CONTAINS'];
function gvTreeRowsFor(instances, depthBase){
  if(!instances || !instances.length) return '';
  const edges = graphState.all.edges || [];
  if(!edges.length) return instances.map(n=>gvTreeRow(n, depthBase)).join('');
  const byId = {}; instances.forEach(n=>byId[n.id]=n);
  const kids = {};
  edges.forEach(e=>{
    if(e.source_id===e.target_id) return;
    if(gvTreeParentRels.includes(e.relation_type) && byId[e.source_id] && byId[e.target_id]){
      (kids[e.source_id]=kids[e.source_id]||[]).push(e.target_id);
    }
  });
  const isRoot = n => !edges.some(e=>e.target_id===n.id && gvTreeParentRels.includes(e.relation_type) && byId[e.source_id]);
  const out = [];
  const emit = (n, d)=>{
    out.push(gvTreeRow(n, d));
    (kids[n.id]||[]).forEach(cid=>{ const c=byId[cid]; if(c) emit(c, d+1); });
  };
  instances.filter(isRoot).forEach(n=>emit(n, depthBase));
  return out.join('');
}
function gvTreeToggle(name){
  gvTreeSt.collapsed.has(name) ? gvTreeSt.collapsed.delete(name) : gvTreeSt.collapsed.add(name);
  renderEntityTree();
}
// 一级类型行 → 图上联动（R1 2026-09-10）：整行点击；骨架模式 = 等同点击画布 L1 类型节点下钻 L2 实例束；
// 普通模式 = 单选替换式过滤聚焦该类型族（点 B 从"仅A"切到"仅B"；点当前聚焦行恢复全图）。
// 反馈：toast + 树行高亮。▸/▾ 箭头另绑定 gvTreeToggle 负责折叠树。
function gvTreeTypeFocus(name){
  const sk = graphState.sk;
  const v = graphState.view;
  const vts = v.types = v.types || [];
  if(sk.on){
    sk.drill = name; sk.focus = null;
    graphState.sel = null;
    applyGraphViewAndRender(); gvFitView();
    renderEntityTree();
    toast('🦴 已下钻「'+esc(name)+'」：L2 实例束（'+graphState.nodes.length+' 实例）· 点击实例下钻 L3 邻域');
    return;
  }
  const fam = [...gvTypeFamily(name)];
  const wasOn = vts.length>0 && fam.every(x=>vts.includes(x)) && vts.every(x=>fam.includes(x));
  vts.length = 0;
  if(!wasOn) vts.push(...fam);
  graphState.sel = null;
  applyGraphViewAndRender(); gvFitView();
  renderEntityTree();
  if(wasOn) toast('已取消「'+esc(name)+'」过滤，恢复全图');
  else toast('已聚焦「'+esc(name)+'」'+(fam.length>1?'（含 '+(fam.length-1)+' 个子类型）':'')+'：仅显示该类型；再点该行恢复全图');
}
// 2026-09-10 米爸裁剪：左栏过滤框已删，gvTreeApplyFilter 一并移除（gvTreeSt.filter 恒空，renderEntityTree 过滤逻辑已退化为直通）
function gvTreeSelEntity(id){
  const n = (graphState.all.nodes||[]).find(x=>x.id===id);
  if(!n) return;
  if(!graphState.nodes.find(x=>x.id===id)){ toast('该实体被当前状态筛选隐藏，请在 ⚙ 筛选 中调整状态'); return; }
  graphState.sel = {type:'node', id};
  renderGraph(); gvCenterOnNode(id); showGraphDetail(id);
}
function gvTreeLocate(id){
  // 图 → 树反向定位：清旧高亮 → 高亮该实体行并滚动到可见；树中不存在（被过滤/折叠）时重渲后定位
  document.querySelectorAll('#gv-ent-tree .ont-tnd.on').forEach(el=>el.classList.remove('on'));
  let el = document.getElementById('gvtn-'+id);
  if(!el){
    // 展开其所属类型再定位
    const n = (graphState.all.nodes||[]).find(x=>x.id===id);
    if(n){ gvTreeSt.collapsed.delete(n.entity_type||'未分类'); }
    renderEntityTree();
    el = document.getElementById('gvtn-'+id);
  }
  if(!el) return;
  el.classList.add('on'); el.scrollIntoView({block:'nearest'});
}
function gvToggleLeft(){
  const L=document.getElementById('gv-left'), hdr=document.getElementById('gv-left-hdr'),
        lst=document.getElementById('gv-ent-tree'), bt=document.getElementById('gv-left-collapse');
  if(!L) return;
  const collapsed = (L.style.width||'')==='52px';
  if(collapsed){ L.style.width='236px'; if(hdr)hdr.style.display=''; if(lst)lst.style.display=''; bt.innerHTML='«'; }
  else { L.style.width='52px'; if(hdr)hdr.style.display='none'; if(lst)lst.style.display='none'; bt.innerHTML='»'; }
}
// 变更历史（右栏内联折叠）：/api/knowledge/entities/{id} 已附带 commit_history
const _gvHistCache = {};
async function gvToggleHistory(id){
  const box = document.getElementById('gv-hist'); if(!box) return;
  // 2026-09-04 优化：默认首次进入即展开（用 box.dataset.open 跟踪状态）
  if(box.dataset.open==='1' && box.dataset.loaded==='1'){ box.dataset.open='0'; box.dataset.loaded='0'; box.innerHTML='<span style="color:var(--mut);">点击展开查看该实体的提交记录</span>'; return; }
  box.dataset.open='1'; box.dataset.loaded='1'; box.innerHTML='<span style="color:var(--mut);">加载中…</span>';
  try{
    let rows = _gvHistCache[id];
    if(!rows){ const r = await api('/api/knowledge/entities/'+encodeURIComponent(id)); rows = (r&&r.commit_history)||[]; _gvHistCache[id]=rows; }
    box.innerHTML = rows.length ? rows.map(c=>`<div style="margin-bottom:5px;line-height:1.6;">
      <span class="tag" style="font-size:12px;">#${c.id||'-'} ${esc(c.kind||'')}</span> ${esc(c.message||'')}
      <div style="color:var(--mut);font-size:12px;">${esc((c.created_at||'').slice(0,16)||'')} · ${esc(c.created_by||'-')}</div></div>`).join('')
      : '<span style="color:var(--mut);">暂无提交记录</span>';
  }catch(e){ box.innerHTML='<span style="color:var(--mut);">加载失败：'+esc(e.message||e)+'</span>'; }
}
// ── 详情面板：节点（属性编辑 + 元数据/审核/合并/追溯） / 边（修正 + 元数据/审核/追溯）──
function gvStatusTxt(st){ return ({reviewed:'✅ 已评审', candidate:'⏳ 候选', raw_chunk:'🧩 原始块', deprecated:'🚫 已驳回'})[st] || st || '-'; }
function gvSetDetailPanel(on){
  const p = document.getElementById('gv-detail-panel');
  if(p) p.style.display = on ? 'flex' : 'none';
}
function gvCloseDetail(){
  graphState.sel = null; graphState.drag = null; graphState.pan = null;
  renderGraph();
  const body = document.getElementById('gv-detail-body');   // 右栏常驻：清除选中回到空态提示（不隐藏面板）
  if(body) body.innerHTML = '<div style="color:var(--mut);font-size:12.5px;padding:6px;">点左栏实体或图中节点查看详情</div>';
  renderEntityTree();   // 清除左树高亮
}

// ── P0：节点/边数据来源一键追溯（FR-KG-11）──
async function gvShowProvenance(entityId) {
  try {
    const data = await api(`/api/entities/${entityId}/provenance`);
    if(data.error) { toast('追溯加载失败：'+data.error); return; }
    renderProvenancePanel(data, 'entity');
  } catch(e) { toast('追溯加载失败：'+e.message); }
}

function renderProvenancePanel(data, kind) {
  const body = document.getElementById('gv-detail-body');
  if(!body) return;
  const ent = data.entity || data.relation || {};
  const entName = ent.name || ent.relation_type || ent.id || '追溯';
  const sd = data.source_document || null;
  const chunks = data.source_chunks || [];
  const versions = data.version_chain || [];
  const audit = data.audit_trail || [];
  const siblings = data.siblings || [];
  const dep = data.deprecated_trace;
  const sourceEnt = data.source_entity, targetEnt = data.target_entity;

  // 节点信息行
  const metaRows = `
    <div class="kv"><span>ID</span><b>${esc(ent.id||'-')}</b></div>
    <div class="kv"><span>类型</span><b>${esc(ent.entity_type||ent.relation_type||'-')}</b></div>
    <div class="kv"><span>创建人</span><b>${esc(ent.created_by||'-')}</b></div>
    <div class="kv"><span>创建时间</span><b>${esc((ent.created_at||'').slice(0,16))}</b></div>
    <div class="kv"><span>审核人</span><b>${esc(ent.reviewed_by||'-')}</b></div>
    <div class="kv"><span>审核时间</span><b>${esc((ent.reviewed_at||'').slice(0,16))}</b></div>
    ${ent.confidence!=null?`<div class="kv"><span>置信度</span><b>${Math.round(ent.confidence*100)}%</b></div>`:''}
    ${ent.source_type?`<div class="kv"><span>来源类型</span><b>${esc(ent.source_type)}</b></div>`:''}
    ${ent.status?`<div class="kv"><span>状态</span><b><span class="st ${ent.status==='deprecated'?'r':ent.status==='reviewed'?'ok':'w'}">${esc(ent.status)}</span></b></div>`:''}`;

  // 来源文档
  const docHtml = sd
    ? `<div class="kv"><span>文件名</span><b>${esc(sd.filename)}</b></div>
       <div class="kv"><span>生命周期</span><b><span class="lc-badge ${({uploaded:'lc-info',processing:'lc-info',stored:'lc-success',committed:'lc-primary',deprecated:'lc-muted',archived:'lc-warning'}[sd.lifecycle_status]||'lc-info')}">${esc(sd.lifecycle_status||'-')}</span></b></div>
       <div class="kv"><span>标题 / 作者</span><b>${esc(sd.title||'-')} / ${esc(sd.author||'-')}</b></div>
       <div class="kv"><span>上传人 / 时间</span><b>${esc(sd.uploaded_by||'-')} / ${esc((sd.created_at||'').slice(0,16))}</b></div>
       ${sd.deprecated_at?`<div class="kv"><span>废弃时间</span><b>${esc((sd.deprecated_at||'').slice(0,16))}</b></div>`:''}`
    : '<div style="color:var(--mut);font-size:12.5px;">无关联文档</div>';

  // 命中切片
  const chunkHtml = chunks.length
    ? chunks.map(c => `<div style="border:1px solid var(--line);border-radius:5px;padding:6px 8px;margin-bottom:5px;font-size:12.5px;">
        <div style="color:var(--mut);font-size:12px;">chunk#${c.chunk_index||'-'} ${c.section?'· '+esc(c.section):''}</div>
        <div style="margin-top:3px;line-height:1.6;">${esc((c.content||'').slice(0,160))}${(c.content||'').length>160?'…':''}</div>
      </div>`).join('')
    : '<div style="color:var(--mut);font-size:12.5px;">未命中具体切片（可能为手工建模或来源未挂接）</div>';

  // 版本链路
  const versionHtml = versions.length
    ? versions.map(v => `<div style="font-size:12.5px;padding:3px 0;">
        <b>v${esc(v.sysml_version||v.version||'-')}</b>
        <span style="color:var(--mut);">· ${esc(v.created_by||'-')} · ${esc((v.created_at||'').slice(0,16))}</span>
        ${v.message?`<div style="font-size:12px;color:var(--mut);margin-top:2px;">${esc(v.message)}</div>`:''}
      </div>`).join('')
    : '<div style="color:var(--mut);font-size:12.5px;">无 SysML 版本链路</div>';

  // 审计轨迹
  const auditHtml = audit.length
    ? audit.slice(0,15).map(a => `<div style="font-size:12.5px;padding:3px 0;border-bottom:1px dashed var(--line);">
        <span class="st ${a.kind==='edit'?'b':'w'}">${esc(a.kind)}</span>
        <b>${esc(a.action||'-')}</b>
        <span style="color:var(--mut);">by ${esc(a.operator||'-')} · ${esc((a.created_at||'').slice(0,16))}</span>
      </div>`).join('')
    : '<div style="color:var(--mut);font-size:12.5px;">暂无审计记录</div>';

  // 同源节点（仅节点）
  const siblingHtml = siblings.length
    ? siblings.map(s => `<span class="tag" style="cursor:pointer;margin:2px;" onclick="showGraphDetail('${esc(s.id)}')" title="${esc(s.id)} · ${esc(s.created_by||'')}">${esc(s.name)} <small style="color:var(--mut);">(${esc(s.entity_type||'-')})</small></span>`).join('')
    : '<div style="color:var(--mut);font-size:12.5px;">同源文档无其他节点</div>';

  // 边两端节点引用
  const refHtml = (kind === 'relation' && (sourceEnt || targetEnt)) ? `
    <div class="kv"><span>起点</span><b>${esc(sourceEnt?.name||'-')} <small style="color:var(--mut);">(${esc(sourceEnt?.entity_type||'-')})</small></b></div>
    <div class="kv"><span>终点</span><b>${esc(targetEnt?.name||'-')} <small style="color:var(--mut);">(${esc(targetEnt?.entity_type||'-')})</small></b></div>` : '';

  // 废弃轨迹
  const depHtml = dep
    ? `<div style="background:var(--color-warning-bg,#fff3e6);border:1px solid var(--color-warning,#c98a2e);border-radius:5px;padding:8px 10px;font-size:12.5px;">
        <b style="color:var(--color-warning,#c98a2e);">🚫 节点已废弃</b>
        <div style="margin-top:4px;color:var(--mut);">操作人：${esc(dep.deprecated_by||'-')}</div>
        <div style="color:var(--mut);">时间：${esc((dep.deprecated_at||'').slice(0,16))}</div>
        ${dep.note?`<div style="margin-top:3px;color:var(--mut);">${esc(dep.note)}</div>`:''}
      </div>`
    : '<div style="color:var(--mut);font-size:12.5px;">当前未废弃</div>';

  // Tabs + 折叠区（默认展开节点/文档/切片，折叠其他）
  body.innerHTML = `
    <div class="ip-sticky" style="position:sticky;top:0;z-index:5;background:#fafaf7;border-bottom:1px solid var(--line);padding:8px 10px;">
      <div style="display:flex;align-items:center;gap:6px;">
        <span style="font-size:21px;">🔍</span>
        <div style="flex:1;min-width:0;">
          <div style="font-size:15.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">数据来源：${esc(entName)}</div>
          <div style="font-size:12.5px;color:var(--mut);margin-top:1px;">${esc(kind==='relation'?'关系':'节点')} · ${esc(ent.id||entName)}</div>
        </div>
        <button class="btn sm ghost" style="padding:1px 7px;font-size:12.5px;" onclick="showGraphDetail('${esc(ent.id)}')" title="返回节点详情">↩️</button>
      </div>
    </div>

    <div class="ip-section" data-sec="meta" style="border-bottom:1px solid var(--line);">
      <div class="ip-h" onclick="toggleIpSection(this,'meta')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>📌 ${esc(kind==='relation'?'关系':'节点')}信息</span>
        <span class="ip-arr">▼</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;font-size:12.5px;color:#444;line-height:1.9;">
        ${metaRows}${refHtml}
      </div>
    </div>

    <div class="ip-section" data-sec="doc" style="border-bottom:1px solid var(--line);">
      <div class="ip-h" onclick="toggleIpSection(this,'doc')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>📄 来源文档</span>
        <span class="ip-arr">▼</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;font-size:12.5px;color:#444;line-height:1.9;">${docHtml}</div>
    </div>

    <div class="ip-section" data-sec="chunk" style="border-bottom:1px solid var(--line);">
      <div class="ip-h" onclick="toggleIpSection(this,'chunk')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>🔖 命中切片（${chunks.length}）</span>
        <span class="ip-arr">▼</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;">${chunkHtml}</div>
    </div>

    <div class="ip-section" data-sec="version" style="border-bottom:1px solid var(--line);">
      <div class="ip-h" onclick="toggleIpSection(this,'version')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>🧬 版本链路（${versions.length}）</span>
        <span class="ip-arr">▶</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;display:none;">${versionHtml}</div>
    </div>

    <div class="ip-section" data-sec="audit" style="border-bottom:1px solid var(--line);">
      <div class="ip-h" onclick="toggleIpSection(this,'audit')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>🛡 审计轨迹（${audit.length}）</span>
        <span class="ip-arr">▶</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;display:none;">${auditHtml}</div>
    </div>

    ${kind==='entity'?`
    <div class="ip-section" data-sec="siblings" style="border-bottom:1px solid var(--line);">
      <div class="ip-h" onclick="toggleIpSection(this,'siblings')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>🔗 同源节点（${siblings.length}）</span>
        <span class="ip-arr">▶</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;display:none;">${siblingHtml}</div>
    </div>`:''}

    <div class="ip-section" data-sec="deprecated">
      <div class="ip-h" onclick="toggleIpSection(this,'deprecated')" style="padding:7px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;display:flex;justify-content:space-between;align-items:center;">
        <span>🚫 废弃轨迹</span>
        <span class="ip-arr">▶</span>
      </div>
      <div class="ip-c" style="padding:6px 10px;display:none;">${depHtml}</div>
    </div>`;
}
function showGraphDetail(id) {
  const body = document.getElementById('gv-detail-body');
  if(!body) return;
  const n = graphState.nodes.find(x=>x.id===id);
  if(!n) { body.innerHTML='<div style="color:var(--mut);font-size:12.5px;padding:6px;">点左栏实体或图中节点查看详情</div>'; return; }
  gvSetDetailPanel(true);
  let props = {};
  try{ props = JSON.parse(n.properties||'{}')||{}; }catch(e){}
  const rels = graphState.edges.filter(e=>e.source_id===id||e.target_id===id);
  const ro = isReleaseBranch(getCurrentBranch());   // release 只读：隐藏编辑/合并
  const docAttr = String(n.source_doc||'').replace(/"/g,'&quot;');
  // 2026-09-04 优化：右栏重构为 sticky 头部 + 四段折叠（元数据 / 属性 / 关系 / 变更历史）
  body.innerHTML = `
    <!-- sticky 头部：节点身份常驻可视 -->
    <div class="ip-sticky" style="position:sticky;top:0;z-index:5;background:#fafaf7;border-bottom:1px solid var(--line);padding:8px 10px;">
      <div style="display:flex;align-items:center;gap:6px;">
        <span style="font-size:23px;">🛰</span>
        <div style="flex:1;min-width:0;">
          <div style="font-size:16.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(n.name)}</div>
          <!-- 本体类型标签（2026-09-10 米爸）：名称下方标签，点击跳本体模型查看该类型定义；类型名直接取实体数据，保证一致 -->
          <!-- 2026-09-10 状态机收口：画布实体全为 reviewed（入图即确认），状态徽章退化为常量 → 移除 -->
          <div style="font-size:12.5px;color:var(--mut);margin-top:1px;">
            ${n.entity_type?`<span class="tag" style="cursor:pointer;background:var(--blue-l);color:var(--blue-d);font-weight:600;" onclick="ontGotoType('${ontJs(n.entity_type)}')" title="本体类型：点击跳转本体模型，查看「${esc(n.entity_type)}」的定义、属性与公理">🧬 ${esc(n.entity_type)}</span>`:'<span class="tag" title="未标注本体类型">未标注类型</span>'}
            · <span class="tag" title="${esc(n.id)}">${esc(n.id).slice(0,14)}${n.id.length>14?'…':''}</span>
          </div>
        </div>
        <button class="btn sm ghost" style="padding:1px 7px;font-size:12.5px;" onclick="gvShowProvenance('${esc(n.id)}')" title="查看数据来源（FR-KG-11 一键追溯）">🔍</button>
        <button class="btn sm ghost" style="padding:1px 7px;font-size:12.5px;" onclick="showGraphDetail('${esc(n.id)}')" title="刷新本节点详情">🔄</button>
      </div>
    </div>

    <div class="ip-section" data-sec="meta">
      <div class="ip-h" onclick="toggleIpSection(this,'meta')" style="padding:8px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;user-select:none;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);">
        <span>📌 元数据与追溯</span>
        <span class="ip-arr" style="transition:transform .15s;font-size:12px;color:var(--mut);">▼</span>
      </div>
      <div class="ip-c" style="padding:8px 10px;font-size:12.5px;color:#444;line-height:1.9;">
        <div>流转阶段 <span style="float:right;">${branchPill(n.branch||getCurrentBranch())}</span></div>
        <div>所在分支 <span style="float:right;color:var(--mut);">${esc(n.branch||getCurrentBranch())}</span></div>
        <div>跨分支状态 <span style="float:right;" id="ent-brn-status">检测中…</span></div>
        <div>创建人 / 时间 <span style="float:right;color:var(--mut);">${esc(n.created_by||'-')} · ${esc((n.created_at||'').slice(0,16)||'-')}</span></div>
        <div>审核人 / 时间 <span style="float:right;color:var(--mut);">${esc(n.reviewed_by||'-')} · ${esc((n.reviewed_at||'').slice(0,16)||'-')}</span></div>
        <div>来源文档 <span style="float:right;color:var(--mut);">${n.source_doc?`<a style="color:var(--blue-d);cursor:pointer;text-decoration:underline;" data-doc="${docAttr}" onclick="gvTraceSource(this.getAttribute('data-doc'))">📄 ${esc(n.source_doc)}</a>`:'-'}</span></div>
        <div>来源类型 <span style="float:right;color:var(--mut);">${esc(n.source_type||'-')}</span></div>
        <div>置信度 <span style="float:right;color:var(--mut);">${n.confidence!=null?Math.round(n.confidence*100)+'%':'-'}</span></div>
        <div style="clear:both;"></div>
        <div>图谱来源 <span style="float:right;color:var(--mut);">${esc(n.graph_source||'manual')}</span></div>
        ${ro?'':`<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">
          ${n.source_doc?`<button class="btn sm ghost" data-doc="${docAttr}" onclick="gvTraceSource(this.getAttribute('data-doc'))">🔍 一键追溯来源</button>`:''}
          <button class="btn sm ghost" onclick="gvMergeNode('${esc(n.id)}')">🔄 合并到…</button>
          <!-- 2026-09-10 状态机收口：candidate 不再进入画布（在数据整理·审核队列确认后入图），画布内审核按钮随之移除；本体类型入口收敛为名称下方标签 -->
        </div>`}
      </div>
    </div>

    <div class="ip-section" data-sec="props">
      <div class="ip-h" onclick="toggleIpSection(this,'props')" style="padding:8px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;user-select:none;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);">
        <span>🏷 属性${ro?'（release 只读）':''}<span style="font-weight:400;color:var(--mut);font-size:12px;margin-left:4px;">按本体约束</span></span>
        <span class="ip-arr" style="transition:transform .15s;font-size:12px;color:var(--mut);">▼</span>
      </div>
      <div class="ip-c" style="padding:8px 10px;font-size:12.5px;color:#444;">
        <div id="inst-ont-props"><div style="color:var(--mut);font-size:12.5px;">正在加载本体数据属性…</div></div>
        <!-- 2026-09-10 米爸裁剪：自定义扩展属性编辑区删除——属性名/值全部受本体 dataProperty 约束
             （名 = 本体定义下拉/固定行，值 = 按 xsd 类型渲染 number/date/checkbox/枚举下拉），不允许自由键值 -->
        ${ro?'':`<div style="margin-top:8px;">
          <button class="btn grn" onclick="saveGvInstProps('${esc(n.id)}')">💾 保存属性</button>
          <span style="font-size:12px;color:var(--mut);margin-left:6px;">属性名与取值范围由本体定义约束</span>
        </div>`}
      </div>
    </div>

    <div class="ip-section" data-sec="rels">
      <div class="ip-h" onclick="toggleIpSection(this,'rels')" style="padding:8px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;user-select:none;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);">
        <span>🔗 关联关系 <span style="background:var(--blue-l);color:var(--blue-d);padding:0 6px;border-radius:8px;font-size:12px;margin-left:4px;">${rels.length}</span></span>
        <span class="ip-arr" style="transition:transform .15s;font-size:12px;color:var(--mut);">▼</span>
      </div>
      <div class="ip-c" style="padding:8px 10px;font-size:12.5px;${rels.length?'':'color:var(--mut);'}">
        ${rels.length?rels.map(r=>{
          const sName = graphState.nodes.find(x=>x.id===r.source_id)?.name || r.source_id;
          const tName = graphState.nodes.find(x=>x.id===r.target_id)?.name || r.target_id;
          const isOut = r.source_id===id;
          return `<div style="margin-bottom:4px;line-height:1.6;">
            <span class="tag" style="font-size:12px;">${esc(r.relation_type)}</span>
            ${isOut
              ? `<span style="color:var(--mut);">${esc(sName)} →</span> <a style="color:var(--blue-d);cursor:pointer;text-decoration:underline;" onclick="showGraphDetail('${esc(r.target_id)}')">${esc(tName)}</a>`
              : `<a style="color:var(--blue-d);cursor:pointer;text-decoration:underline;" onclick="showGraphDetail('${esc(r.source_id)}')">${esc(sName)}</a> <span style="color:var(--mut);">→ ${esc(tName)}</span>`}
          </div>`;
        }).join(''):'<div style="padding:8px 0;color:var(--mut);">该实体暂无关联关系</div>'}
      </div>
    </div>

    <div class="ip-section" data-sec="hist">
      <div class="ip-h" onclick="toggleIpSection(this,'hist')" style="padding:8px 10px;font-size:13.5px;font-weight:600;color:var(--blue-d);cursor:pointer;user-select:none;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);">
        <span>🕘 变更历史</span>
        <span class="ip-arr" style="transition:transform .15s;font-size:12px;color:var(--mut);">▼</span>
      </div>
      <div class="ip-c" style="padding:8px 10px;font-size:12.5px;">
        <div id="gv-hist" data-open="0" data-loaded="0" style="color:var(--mut);">点击展开查看该实体的提交记录</div>
      </div>
    </div>
  `;
  loadEntityBranchStatus(n.id);
  gvTreeLocate(n.id);   // 双向联动：图点节点 → 左树滚动定位高亮
  // 2026-09-04 优化：① 按本体 dataProperty 渲染受控属性 ② 变更历史默认首次选中即展开
  gvRenderOntProps(n.id, n.entity_type, props);
  const _hid = String(n.id);
  setTimeout(()=>{ try{ gvToggleHistory(_hid); }catch(e){} }, 0);
}
// 折叠段切换（2026-09-04 优化：右栏重构新增）
function toggleIpSection(h, key){
  const sec = h.closest('.ip-section');
  if(!sec) return;
  const c = sec.querySelector('.ip-c');
  const arr = sec.querySelector('.ip-arr');
  const collapsed = c.style.display === 'none';
  if(collapsed){ c.style.display = ''; arr.style.transform = ''; sec.dataset.collapsed = '0'; }
  else { c.style.display = 'none'; arr.style.transform = 'rotate(-90deg)'; sec.dataset.collapsed = '1'; }
}
// 按本体 dataProperty 渲染属性编辑区（2026-09-04 新增）
async function gvRenderOntProps(id, type, currentProps){
  const wrap = document.getElementById('inst-ont-props');
  if(!wrap) return;
  wrap.innerHTML = '<div style="color:var(--mut);font-size:12.5px;">加载本体数据属性…</div>';
  try{
    const schema = await api('/api/knowledge/graph/data-properties?type='+encodeURIComponent(type||''));
    window._ontPropKeys = new Set(schema.map(p=>p.name));
    if(!schema.length){
      wrap.innerHTML = `<div style="color:var(--mut);font-size:12.5px;">本体未为「<b>${esc(type||'(未指定类型)')}</b>」定义数据属性。属性编辑仅允许本体受控属性——如需扩展，请到本体模型为该类型定义 dataProperty。</div>`;
      return;
    }
    const ro = isReleaseBranch(getCurrentBranch());
    wrap.innerHTML = schema.map(p=>gvRenderOntPropRow(p, currentProps[p.name], ro)).join('');
  }catch(e){
    wrap.innerHTML = '<div style="color:var(--mut);font-size:12.5px;">加载失败：'+esc(e.message||e)+'</div>';
  }
}
function gvRenderOntPropRow(p, val, ro){
  const required = p.required ? '<span style="color:var(--amb);margin-left:2px;">*</span>' : '';
  const choices = p.allowed_values && p.allowed_values.length;
  const typeLabel = p.type && p.type!=='xsd:string' ? `<span title="本体类型 ${esc(p.type)}" style="font-size:12px;color:var(--blue-d);margin-left:2px;">${esc(p.type.replace('xsd:',''))}</span>` : '';
  // 2026-09-10 修复：unitSuffix 此前定义在 else 块作用域内、return 模板在外层引用 → ReferenceError
  // （潜伏 bug：domain 全空时走不到渲染分支，绑定后首次渲染即爆）
  const unitSuffix = p.unit ? `<span title="单位 ${esc(p.unit)}" style="font-size:12px;color:var(--amber-d,#8a5a00);margin-left:3px;white-space:nowrap;">${esc(p.unit)}</span>` : '';
  let input;
  if(choices){
    input = `<select class="ip-ont-val" data-name="${esc(p.name)}" ${ro?'disabled':''} style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:12.5px;flex:1;">
      ${(p.allowed_values||[]).map(v=>`<option value="${esc(v)}" ${String(v)===String(val)?'selected':''}>${esc(v)}</option>`).join('')}
    </select>`;
  } else {
    const htmlType = ({'xsd:int':'number','xsd:decimal':'number','xsd:float':'number','xsd:double':'number','xsd:dateTime':'datetime-local','xsd:date':'date','xsd:boolean':'checkbox',
      'int':'number','decimal':'number','float':'number','double':'number','dateTime':'datetime-local','date':'date','boolean':'checkbox','bool':'checkbox'})[p.type] || 'text';
    const step = (p.type==='xsd:decimal'||p.type==='xsd:float'||p.type==='xsd:double') ? ' step="any"' : '';
    const isBool = p.type==='xsd:boolean';
    if(isBool){
      input = `<input class="ip-ont-val" data-name="${esc(p.name)}" type="checkbox" ${String(val)==='true'||val===true?'checked':''} ${ro?'disabled':''}>`;
    } else {
      input = `<input class="ip-ont-val" data-name="${esc(p.name)}" type="${htmlType}"${step}
        value="${esc(val==null?'':String(val))}" placeholder="${esc(p.description||'')}" ${ro?'disabled':''}
        style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:12.5px;flex:1;">`;
    }
  }
  const helpMark = p.description ? `<span class="info-tip" title="${esc(p.description)}">ⓘ</span>` : '';
  const missingMark = p.required && (val==null || val==='') ? `<span title="必填项待补" style="color:var(--amb);font-size:12px;margin-left:2px;">⚠</span>` : '';
  return `<div class="ip-ont-row" data-ont-name="${esc(p.name)}" style="display:flex;gap:6px;margin-bottom:5px;align-items:center;">
    <span style="width:84px;font-size:12.5px;color:var(--blue-d);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(p.name)}">${esc(p.name)}${required}</span>
    ${input}
    ${typeLabel}${unitSuffix}${helpMark}${missingMark}
  </div>`;
}
// 收集受控属性（2026-09-10 裁剪：自定义扩展属性已移除，仅收集本体 dataProperty 渲染行，PUT 时合并提交）
function gvCollectProps(){
  const props = {};
  document.querySelectorAll('.ip-ont-val').forEach(el=>{
    const name = el.dataset.name;
    if(!name) return;
    let v;
    if(el.type==='checkbox') v = el.checked ? 'true' : 'false';
    else v = el.value;
    if(v !== '' && v != null) props[name] = v;
  });
  return props;
}
async function loadEntityBranchStatus(id){
  const el=document.getElementById('ent-brn-status'); if(!el) return;
  try{
    const r = await api('/api/knowledge/entities/'+encodeURIComponent(id)+'/branch-status');
    const b = (r&&r.branches)||{};
    const rel=b['release'], dev=b['dev'], per=b['personal'];
    const released = !!(rel && rel.status==='reviewed');
    const draftChanged = !!(per && per.status && per.status!=='reviewed' && per.status!=='deprecated');
    let lbl;
    if(released && (b['personal']||b['dev'])) lbl='<span class="st a" title="release 已发布，但 personal/dev 有未发布改动">✅ 已发布·有改动</span>';
    else if(released) lbl='<span class="st ok">✅ 已发布</span>';
    else if(per && per.status==='candidate') lbl='<span class="st w">🕒 仅草稿（未发布）</span>';
    else if(dev) lbl='<span class="st a">📦 dev 待发布</span>';
    else lbl='<span style="color:var(--mut);">-</span>';
    const diff = (typeof openBranchDiffDrawer==='function')
      ? ' <button class="btn sm ghost" style="float:right;font-size:12px;padding:0 8px;" onclick="openBranchDiffDrawer()" title="打开分支对比查看两分支差异">🔍 查看差异</button>'
      : '';
    el.innerHTML = lbl + diff;
  }catch(e){ el.innerHTML='<span style="color:var(--mut);">-</span>'; }
}
function showGraphEdgeDetail(id) {
  const body = document.getElementById('gv-detail-body');
  if(!body) return;
  const e = graphState.edges.find(x=>String(x.id)===String(id));
  if(!e) { body.innerHTML='<div style="color:var(--mut);font-size:12.5px;padding:6px;">点左栏实体或图中节点/关系查看详情</div>'; return; }
  gvSetDetailPanel(true);
  const sN = graphState.nodes.find(n=>n.id===e.source_id);
  const tN = graphState.nodes.find(n=>n.id===e.target_id);
  const docAttr = String(e.source_doc||'').replace(/"/g,'&quot;');
  const ro = isReleaseBranch(getCurrentBranch());   // release 只读：隐藏修正关系
  body.innerHTML = `
    <div style="border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:8px;">
      <div style="font-size:16.5px;font-weight:700;">🔗 关系详情 <span class="tag">#${e.id}</span></div>
      <div style="font-size:13.5px;margin-top:5px;">${esc(sN? sN.name : e.source_id)} <span class="tag" style="font-size:12px;color:var(--blue-d);">${esc(e.relation_type)}</span> ${esc(tN? tN.name : e.target_id)}</div>
      <div style="font-size:12.5px;color:var(--mut);margin-top:2px;">${esc(e.source_id)} → ${esc(e.target_id)}</div>
    </div>
    ${ro?'':`<div style="font-size:13.5px;font-weight:600;color:var(--blue-d);">✏️ 修正关系（保存后重新进入审核）</div>
    <div style="display:flex;gap:6px;margin-top:6px;">
      <input id="gv-edge-type" value="${esc(e.relation_type||'')}" placeholder="关系类型" style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:12.5px;flex:1;">
      <button class="btn grn" onclick="gvSaveEdge(${e.id})">💾 保存</button>
    </div>`}
    <div style="margin-top:10px;border-top:1px dashed var(--line);padding-top:8px;">
      <div style="font-size:13.5px;font-weight:600;color:var(--blue-d);">📌 元数据与追溯</div>
      <div style="margin-top:4px;font-size:12.5px;color:#444;line-height:1.9;">
        <div>创建时间 <span style="float:right;color:var(--mut);">${esc((e.created_at||'').slice(0,16)||'-')}（${esc(e.created_by||'-')}）</span></div>
        <div>审核人 / 时间 <span style="float:right;color:var(--mut);">${esc(e.reviewed_by||'-')} · ${esc((e.reviewed_at||'').slice(0,16)||'-')}</span></div>
        <div>来源文档 <span style="float:right;color:var(--mut);">${e.source_doc?`<a style="color:var(--blue-d);cursor:pointer;text-decoration:underline;" data-doc="${docAttr}" onclick="gvTraceSource(this.getAttribute('data-doc'))">📄 ${esc(e.source_doc)}</a>`:'-'}</span></div>
        <div>置信度 <span style="float:right;color:var(--mut);">${e.confidence!=null?Math.round(e.confidence*100)+'%':'-'}</span></div>
      </div>
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">
        ${e.source_doc?`<button class="btn sm ghost" data-doc="${docAttr}" onclick="gvTraceSource(this.getAttribute('data-doc'))">🔍 一键追溯来源</button>`:''}
        <!-- 2026-09-10 状态机收口：候选关系审核在数据整理·审核队列完成，画布内审核按钮移除 -->
      </div>
    </div>`;
}
// ── 实例属性保存（图谱右侧详情面板）──
// 2026-09-10 裁剪：addInstPropRow/delInstPropRow/_legacyCollectInstProps 已删——属性全部受本体约束，不再有自由键值编辑
async function saveGvInstProps(id) {
  if(!branchWritable()) return;
  const props = gvCollectProps();
  const n = graphState.nodes.find(x=>x.id===id) || {};
  try {
    const r = await api(`/api/knowledge/graph/nodes/${encodeURIComponent(id)}`, {method:'PUT', body:JSON.stringify({
      name: n.name, entity_type: n.entity_type, properties: props, x: n.x||0, y: n.y||0, branch: getCurrentBranch()})});
    if(r.error) { toast('保存失败：'+r.error); return; }
    toast('✅ 实例属性已保存');
    n.properties = JSON.stringify(props);
    showGraphDetail(id);
  } catch(e) { toast('保存失败：'+e.message); }
}
// ── 画布视口：缩放/平移（对齐本体图谱交互：滚轮缩放 · 空白拖动平移）──
function gvToUser(ev) {
  const svg = document.getElementById('gv-svg');
  if(!svg) return null;
  const ctm = svg.getScreenCTM();
  if(!ctm) return null;
  return new DOMPoint(ev.clientX, ev.clientY).matrixTransform(ctm.inverse());
}
/* P1-1 前置（2026-11-28）：屏幕坐标 → **世界坐标**。
   gvToUser 只反变换到 SVG 视口（getScreenCTM 不含 #gv-inner 的 transform），
   而 pan/scale 施加在 #gv-inner 上 ⇒ 缩放后拿视口坐标直接减 n.x/n.y 会少除一次 scale。
   真机取证（scale=2 时拖拽）：鼠标右移屏幕 100px，节点世界坐标也变 100（应为 50），
   屏幕位移 -1128px，节点直接飞出视口。
   拖拽定位与框选命中测试都必须走这里。 */
function gvScreenToWorld(ev) {
  const p = gvToUser(ev);
  if(!p) return null;
  const vp = graphState.vp, s = vp.scale || 1;
  return { x: (p.x - (vp.panX || 0)) / s, y: (p.y - (vp.panY || 0)) / s };
}
/* 世界坐标 → SVG 视口坐标（框选命中测试的反向换算） */
function gvWorldToScreen(x, y) {
  const vp = graphState.vp, s = vp.scale || 1;
  return { x: x * s + (vp.panX || 0), y: y * s + (vp.panY || 0) };
}
// ── 2026-09-12 展示优化：gvApplyView 支持【即时】与【平滑过渡】双模式 ──
// 缩放按钮 / 树定位 gvCenterOnNode / 首次 gvFitView 走 220ms easeOut 过渡；画布平移(graphSvgMove)与滚轮缩放(graphSvgWheel)
// 走即时，避免节点/视口延迟跟随。gvMiniSync 让缩略图视口框随 vp 同步。
let _gvViewTok = 0, _gvCur = {px:0, py:0, s:1};
function gvApplyView(immediate) {
  const apply = () => {
    _gvCur = {px: graphState.vp.panX, py: graphState.vp.panY, s: graphState.vp.scale};
    const g = document.getElementById('gv-inner');
    if(g) g.setAttribute('transform', `translate(${_gvCur.px} ${_gvCur.py}) scale(${_gvCur.s})`);
    const z = document.getElementById('gv-zoom-pct2');
    if(z) z.textContent = Math.round(_gvCur.s * 100) + '%';
    gvSyncLabelDensity();   // P1-5：缩放级别 → 标签密度
    gvMiniSync();
  };
  if(immediate){ _gvViewTok++; apply(); return; }
  const tok = ++_gvViewTok;
  const from = {px:_gvCur.px, py:_gvCur.py, s:_gvCur.s};
  const to = {px: graphState.vp.panX, py: graphState.vp.panY, s: graphState.vp.scale};
  const dur = 220, t0 = performance.now();
  const step = (now) => {
    if(tok !== _gvViewTok) return;
    let t = (now - t0) / dur; if(t > 1) t = 1;
    const e = 1 - Math.pow(1 - t, 3);                       // easeOutCubic
    graphState.vp.panX = from.px + (to.px - from.px) * e;
    graphState.vp.panY = from.py + (to.py - from.py) * e;
    graphState.vp.scale = from.s + (to.s - from.s) * e;
    apply();
    if(t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function gvZoomReset() { graphState.vp.scale=1; graphState.vp.panX=0; graphState.vp.panY=0; gvApplyView(); }
/* P1-5 标签密度：阈值取自 graphTok（与 SysML Cytoscape 同一真源，不各写一份）。
   renderGraph 会重建 #gv-svg（innerHTML），所以 class 不能只在 gvApplyView 里加，
   渲染结束也要重新施加一次。 */
function gvSyncLabelDensity() {
  const svg = document.getElementById('gv-svg');
  if(!svg) return null;
  const s = graphState.vp.scale || 1;
  const far = s < graphTok('labelEdgeZoom'), farther = s < graphTok('labelNodeZoom');
  svg.classList.toggle('gv-far', far);
  svg.classList.toggle('gv-farther', farther);
  return { scale: s, edgeHidden: far, nodeHidden: farther };
}
function gvZoomIn() { graphState.vp.scale = Math.min(3, graphState.vp.scale * 1.2); gvApplyView(); }
function gvZoomOut() { graphState.vp.scale = Math.max(0.3, graphState.vp.scale / 1.2); gvApplyView(); }
// ── 缩略图（minimap）：只读总览 + 视口框随主视图 vp 移动；异常自动隐藏回退（不动主 SVG 与状态机）──
let _gvMini = null, _gvMiniEl = null, _gvMiniBox = null;
function gvMiniSync(){
  if(!_gvMini || !_gvMiniEl) return;
  try{
    const s=graphState.vp.scale||1, pw=graphState.vp.panX||0, ph=graphState.vp.panY||0;
    const vw=_gvMiniEl.clientWidth||190, vh=_gvMiniEl.clientHeight||130;
    const bb=_gvMini.extent();
    const svg=document.getElementById('gv-svg'); const svw=(svg&&svg.clientWidth)||800, svh=(svg&&svg.clientHeight)||400;
    const wx0=(-pw)/s, wy0=(-ph)/s, ww=svw/s, wh=svh/s;
    const dX=(bb.x2-bb.x1)||1, dY=(bb.y2-bb.y1)||1;
    const l=((wx0-bb.x1)/dX)*vw, t=((wy0-bb.y1)/dY)*vh, w=(ww/dX)*vw, h=(wh/dY)*vh;
    if(!_gvMiniBox){ _gvMiniBox=document.createElement('div'); _gvMiniBox.id='gv-minimap-box';
      const _k=graphTok();   // P0-1：视口框与主画布主色同源
      _gvMiniBox.style.cssText='position:absolute;border:1.5px solid '+_k.primary+';border-radius:2px;background:'+hexToRgba(_k.primary,.12)+';box-sizing:border-box;pointer-events:none;';
      _gvMiniEl.appendChild(_gvMiniBox); }
    _gvMiniBox.style.left=Math.max(0,l)+'px'; _gvMiniBox.style.top=Math.max(0,t)+'px';
    _gvMiniBox.style.width=Math.min(vw,Math.max(6,w))+'px'; _gvMiniBox.style.height=Math.min(vh,Math.max(6,h))+'px';
  }catch(e){ console.error('[gvMiniSync]', e); }
}
function gvEnsureMinimap(nodes, edges){
  try{
    const host=document.getElementById('gv-graph-area'); if(!host) return;
    if(!_gvMiniEl){
      _gvMiniEl=document.createElement('div'); _gvMiniEl.id='gv-minimap';
      _gvMiniEl.title='图谱总览（只读）';
      _gvMiniEl.style.cssText='position:absolute;right:14px;bottom:64px;z-index:6;width:190px;height:130px;background:rgba(255,255,255,.96);border:1px solid var(--line,#ddd);border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);overflow:hidden;';
      host.appendChild(_gvMiniEl);
      // 点击跳转：minimap 像素 -> 世界坐标 -> 主图居中定位（保持缩放，走 gvApplyView 平滑过渡）
      if(!_gvMiniEl._mmBound){
        _gvMiniEl._mmBound = 1;
        _gvMiniEl.style.cursor = 'pointer';
        _gvMiniEl.addEventListener('click', (ev) => {
          try{
            const r = _gvMiniEl.getBoundingClientRect();
            const vw = r.width || 190, vh = r.height || 130;
            const px = ev.clientX - r.left, py = ev.clientY - r.top;
            const bb = _gvMini.extent();
            const dX = (bb.x2 - bb.x1) || 1, dY = (bb.y2 - bb.y1) || 1;
            const wx = bb.x1 + (px / vw) * dX;
            const wy = bb.y1 + (py / vh) * dY;
            const svg = document.getElementById('gv-svg');
            const svw = (svg && svg.clientWidth) || 800, svh = (svg && svg.clientHeight) || 400;
            const s = graphState.vp.scale || 1;
            graphState.vp.panX = svw / 2 - s * wx;
            graphState.vp.panY = svh / 2 - s * wy;
            gvApplyView();
          }catch(e){ console.error('[gv-minimap-click]', e); }
        });
      }
    } else if(_gvMiniEl.parentNode !== host){
      host.appendChild(_gvMiniEl);   // renderGraph 清空容器后重新挂回
    }
    if(!_gvMini && window.cytoscape){
      const k = graphTok();   // P0-1：minimap 与主画布同源取色
      _gvMini = cytoscape({container:_gvMiniEl, style:[
        {selector:'node', style:{'width':8,'height':8,'background-color':k.miniNode,'border-width':0,'overlay-opacity':0}},
        {selector:'edge', style:{'width':1,'line-color':k.miniEdge,'curve-style':'bezier','target-arrow-shape':'triangle','target-arrow-color':k.miniEdge,'target-arrow-marker-size':4,'overlay-opacity':0}}
      ], layout:{name:'preset', animate:false}, minZoom:0.05, maxZoom:4});
    }
    if(_gvMini){
      const elN=(nodes||[]).map(n=>({data:{id:String(n.id)}, position:{x:n.x||0, y:n.y||0}}));
      const elE=(edges||[]).map((e,i)=>({data:{id:'gvme'+i, source:String(e.source_id), target:String(e.target_id)}}));
      // cytoscape 无 elements({nodes,edges}) setter；先清空再 add，避免 minmap 空白
      _gvMini.elements().remove();
      _gvMini.add({nodes: elN, edges: elE});
      try{ _gvMini.fit(null, 10); }catch(_){}
    }
    gvMiniSync();
  }catch(e){ console.error('[gvEnsureMinimap]', e); const el=document.getElementById('gv-minimap'); if(el) el.style.display='none'; }
}
function graphSvgWheel(ev) {
  ev.preventDefault();
  const svg = document.getElementById('gv-svg');
  if(!svg) return;
  const r = svg.getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const vp = graphState.vp;
  const factor = ev.deltaY < 0 ? 1.15 : 1/1.15;
  const ns = Math.max(0.3, Math.min(3, vp.scale * factor));
  const wx = (mx - vp.panX) / vp.scale, wy = (my - vp.panY) / vp.scale;
  vp.scale = ns;
  vp.panX = mx - wx * ns; vp.panY = my - wy * ns;
  gvApplyView(true);   // 2026-09-12 滚轮缩放走即时（跟手）
}
// ── 交互：节点拖拽（世界坐标） / 空白平移 / 边选中 ──
async function graphNodeDown(ev, id) {
  ev.stopPropagation();
  // 图上直接连关系（2026-09-12 连线模式）：开启后 mousedown 转为连线的源/目标选择
  // 2026-11-28 修：原写 window.gvLinkMode，而 gvLinkMode 是顶层 let（挂在全局词法环境、不挂 window）
  //   ⇒ 判定恒 false ⇒ 连线模式点节点走的是普通选中，模式开关形同虚设。
  if(typeof gvLinkMode !== 'undefined' && gvLinkMode){
    if(isReleaseBranch(getCurrentBranch())){ toast('🔒 发布分支只读，不可建立关系'); return; }
    gvLinkPick(id);
    return;
  }
  // P1-4 路径模式：点起点 → 点终点（与连线模式互斥）
  if(typeof gvPathMode !== 'undefined' && gvPathMode){
    gvPathPick(id);
    return;
  }
  // 骨架模式分流：L1 类型节点 → 下钻 L2 实例束（不进右栏详情、不拖拽）
  if(id.startsWith('sk:t:')) {
    if(graphState.sk.drill !== id.slice(5)) {
      graphState.sk.drill = id.slice(5);
      graphState.sk.focus = null;
      graphState.sel = null;
      applyGraphViewAndRender(); gvFitView();
    }
    return;
  }
  // 骨架 L2 实例节点 → 下钻 L3 邻域（同时打开右栏详情）
  if(graphState.sk.on && graphState.sk.drill && !graphState.sk.focus) {
    graphState.sk.focus = id;
    graphState.sel = {type:'node', id};
    applyGraphViewAndRender(); gvFitView();
    showGraphDetail(id);
    return;
  }
  const n = graphState.nodes.find(x=>x.id===id);
  if(!n) return;
  graphState.sel = {type:'node', id};
  // release 只读：可点击查看详情，但不启用拖拽（禁改坐标）
  const ro = isReleaseBranch(getCurrentBranch()) || !!n._skel;
  if(!ro) {
    const p = gvScreenToWorld(ev);   // P1-1 前置：世界坐标（含 vp 反算）
    // 记下按下时的世界坐标：mouseup 用它区分「单击」与「拖拽」（决定是否居中/写库）
    graphState.drag = p ? {id, ox: p.x - n.x, oy: p.y - n.y, sx0: n.x, sy0: n.y}
                        : {id, ox:0, oy:0, sx0: n.x, sy0: n.y};
  }
  graphState.multiSel = new Set();   // P1-1：单选与多选互斥，点节点即退出框选态
  renderGraph();
  showGraphDetail(id);
}
// ── 图上直接连关系（连线模式，2026-09-12）：点源 → 点目标 → 选关系类型 → 建边 ──
let gvLinkMode = false, gvLinkSrc = null;
function gvToggleLinkMode(){
  gvLinkMode = !gvLinkMode; gvLinkSrc = null;
  const btn = document.getElementById('gv-link-btn');
  if(btn){ btn.style.background = gvLinkMode ? 'var(--blue-l)' : ''; btn.style.color = gvLinkMode ? 'var(--blue)' : ''; btn.style.fontWeight = gvLinkMode ? '600' : ''; }
  const cancel = document.getElementById('gv-link-cancel');
  if(cancel) cancel.style.display = gvLinkMode ? '' : 'none';
  const tip = document.getElementById('gv-link-tip');
  if(tip) tip.style.display = gvLinkMode ? '' : 'none';
  const svg = document.getElementById('gv-svg');
  if(svg) svg.style.cursor = gvLinkMode ? 'crosshair' : '';   // 2026-09-13 退出连线回到十字定位光标
  renderGraph();
  if(!gvLinkMode){ graphState.sel = null; if(typeof showGraphDetail==='function') showGraphDetail(null); }
}
function gvLinkPick(id){
  if(String(id).startsWith('sk:')){ toast('骨架聚合节点不支持直接连线，请下钻到实例'); return; }
  if(!gvLinkSrc){ gvLinkSrc = id; renderGraph(); return; }
  if(gvLinkSrc === id){ gvLinkSrc = null; renderGraph(); return; }
  gvLinkChoose(gvLinkSrc, id);
}
function gvLinkChoose(srcId, tgtId){
  const nodes = graphState.all.nodes || [];
  const src = nodes.find(x=>x.id===srcId), tgt = nodes.find(x=>x.id===tgtId);
  const cand = (typeof gvcRelList==='function' && src)
    ? gvcRelList(src.entity_type).filter(r=>(r.targets||[]).some(t=>t.id===tgtId))
    : [];
  const body = document.getElementById('gv-detail-body');
  if(!body) return;
  if(!src || !tgt){ body.innerHTML = '<div style="color:var(--mut);font-size:12.5px;">节点不存在，请刷新</div>'; return; }
  if(!cand.length){
    // 2026-09-12：无候选时给出可建方向指引，避免“连线没用”的误判
    const avail = (typeof gvcRelList==='function' && src) ? (gvcRelList(src.entity_type)||[]) : [];
    const dirHtml = avail.length
      ? `${avail.map(r=>{
          const tg=((r.targets||[]).filter(x=>x.id!==srcId).slice(0,3)).map(x=>esc(x.name+'(%s)'.replace('%s',x.entity_type||''))).join('、');
          return `<div style="padding:2px 0;">🔗 <b>${esc(r.name)}</b> → 可连目标：${tg||'（暂无可用目标）'}</div>`;
        }).join('')}`
      : '<div style="color:var(--mut);">该类型下无可建立的关系类型。</div>';
    body.innerHTML = `<div style="font-size:13.5px;line-height:1.7;">
      <div style="margin-bottom:6px;"><b>${esc(src.name)}</b>(<span style="color:var(--mut);">${esc(src.entity_type)}</span>) — 关系？ → <b>${esc(tgt.name)}</b>(<span style="color:var(--mut);">${esc(tgt.entity_type)}</span>) </div>
      <div style="color:var(--amb,#a16207);font-size:12.5px;border-left:3px solid var(--amber);padding-left:8px;margin:6px 0;">
        这两个实例之间<b>没有可建立的关系类型</b>（坐标系未包含「${esc(src.entity_type)} → ${esc(tgt.entity_type)}」）。
      </div>
      <div style="color:var(--mut);font-size:12.5px;">「${esc(src.entity_type)}」当前可建立的关系：</div>
      ${dirHtml}
      <div style="color:var(--mut);font-size:12.5px;margin-top:6px;">提示：连关系需选择<b>有可用关系类型</b>的源/目标组合；也可到「本体模型」为关系类型放开 src/tgt 后重试。</div>
    </div>`;
    return;
  }
  body.innerHTML = `<div style="font-size:13.5px;line-height:1.7;">
    <div style="margin-bottom:8px;font-weight:600;color:var(--blue-d);">选择「${esc(src.name)}」→「${esc(tgt.name)}」的关系类型</div>
    <div style="display:flex;flex-direction:column;gap:6px;">` +
    cand.map(r=>`<button class="btn sm" style="text-align:left;justify-content:flex-start;" onclick="gvLinkCreate('${esc(srcId)}','${esc(tgtId)}','${esc(r.name)}')">${esc(r.name)}</button>`).join('') +
    `</div></div>`;
}
async function gvLinkCreate(srcId, tgtId, rel){
  const branch = getCurrentBranch();
  try{
    const r = await api('/api/knowledge/graph/edges', {method:'POST', body:JSON.stringify({source_id:srcId, target_id:tgtId, relation_type:rel, props:{}, branch})});
    if(!r || r.error){ toast('❌ 建立关系失败：'+(r && r.error || '未知错误')); return; }
    toast('✅ 已建立关系 '+rel);
    renderGraph();
    if(typeof showGraphDetail==='function') showGraphDetail(srcId);
  }catch(e){ toast('❌ 建立关系失败：'+(e.message || String(e))); }
}
function gvLinkCancel(){ gvToggleLinkMode(); }
// 连线模式的 Esc 退出已并入下方 __gvP1EscBound 统一监听：
// 旧实现写 `window.gvLinkMode`，而 gvLinkMode 是顶层 let（不挂 window）⇒ 判定恒 false，Esc 从来没生效过。
function graphSvgDown(ev) {
  if(ev.target.id==='gv-svg') {
    graphState.sel = null; graphState.drag = null;
    /* P1-1 框选：Shift + 空白拖动 = 矩形多选。
       用修饰键强制区分两种手势（行业惯例 Reagraph / Gephi），不平移就不抢同一手势。 */
    if(ev.shiftKey){
      graphState.pan = null;
      const p = gvToUser(ev);
      graphState.box = p ? {x0:p.x, y0:p.y, x1:p.x, y1:p.y} : null;
      const svg0 = document.getElementById('gv-svg');
      if(svg0) svg0.style.cursor = 'crosshair';
      renderGraph();      // 先清掉旧选中视觉
      gvBoxPaint();       // rect 必须在 renderGraph 之后插入（innerHTML 重建会清空画布）
      showGraphDetail(null);
      return;
    }
    graphState.pan = {sx: ev.clientX, sy: ev.clientY, px: graphState.vp.panX, py: graphState.vp.panY};
    graphState.multiSel = new Set();   // 点空白 = 取消一切临时选择（与提示条「点空白取消」语义一致）
    const svg = document.getElementById('gv-svg');
    if(svg) svg.style.cursor = 'grabbing';
    renderGraph();
    showGraphDetail(null);
  }
}
function graphSvgMove(ev) {
  if(graphState.box) {
    const p = gvToUser(ev);
    if(p){ graphState.box.x1 = p.x; graphState.box.y1 = p.y; gvBoxPaint(); }
    return;
  }
  if(graphState.drag) {
    const n = graphState.nodes.find(x=>x.id===graphState.drag.id);
    if(n) {
      const p = gvScreenToWorld(ev);   // P1-1 前置：必须反算 vp，否则缩放后位移被放大 scale 倍
      if(p) { n.x = p.x - graphState.drag.ox; n.y = p.y - graphState.drag.oy; }
    }
    // P0-3：拖拽期间只做增量 patch（节点 transform + 相邻边路径），rAF 合帧；
    //       不再 renderGraph() 全量重建 innerHTML —— 大图掉帧的根因就在这里。
    gvPatchNodeSoon(graphState.drag.id);
  } else if(graphState.pan) {
    graphState.vp.panX = graphState.pan.px + (ev.clientX - graphState.pan.sx);
    graphState.vp.panY = graphState.pan.py + (ev.clientY - graphState.pan.sy);
    gvApplyView(true);   // 2026-09-12 画布平移走即时（跟手）
  }
}
async function graphSvgUp(ev) {
  if(graphState.box) { gvBoxCommit(); return; }
  if(graphState.drag) {
    /* 区分「单击」与「拖拽」（2026-11-28）：
       · 位移 < 2px 视为单击 —— 不写坐标（原来每次单击都发一次 PUT，纯属无用写库），
         并把该节点居中 + 拉到可读缩放（米爸反馈：选中后要看得清、还得自己找位置）。
       · 真拖拽 —— 保存坐标，且**不居中**（用户刚摆好的位置不该被抢走）。
       居中放在 up 而不是 down：down 时 drag 的 ox/oy 已按旧 vp 算好，此刻改 vp 会让
       紧随其后的 mousemove 按新 vp 反算 ⇒ 节点跳飞。 */
    const d = graphState.drag;
    const n = graphState.nodes.find(x=>x.id===d.id);
    const moved = !!n && (Math.abs(n.x - d.sx0) > 2 || Math.abs(n.y - d.sy0) > 2);
    if(n && moved) saveNodePos(n);
    graphState.drag = null;
    renderGraph();   // P0-3：落位后统一刷新一次（选中态/光晕/边层级），拖拽期间零重建
    if(n && !moved) gvRevealNode(d.id);
  }
  if(graphState.pan) {
    graphState.pan = null;
    const svg = document.getElementById('gv-svg');
    if(svg) svg.style.cursor = '';   // 2026-09-13 恢复十字定位光标
  }
}
/* ══════════ P1（2026-11-28）主画布交互三件：框选多选 / 路径高亮 / 双击折叠 ══════════
   三者共用同一套「高亮集合 + 其余淡出」机制（neighbor / actEdgeIds / dim，见 renderGraph），
   不新造第二套视觉语言；状态各自独立，互斥关系在入口处显式处理。 */

// ── P1-1 框选：矩形 rect 画在 SVG 视口层（不是 #gv-inner），坐标与命中测试统一用视口坐标 ──
function gvBoxPaint(){
  const svg = document.getElementById('gv-svg'), b = graphState.box;
  if(!svg || !b) return;
  let r = document.getElementById('gv-box');
  if(!r){
    const k = graphTok();
    r = document.createElementNS('http://www.w3.org/2000/svg','rect');
    r.id = 'gv-box';
    r.setAttribute('fill', hexToRgba(k.primary, .10));
    r.setAttribute('stroke', k.primary);
    r.setAttribute('stroke-width', '1.2');
    r.setAttribute('stroke-dasharray', '4 3');
    r.setAttribute('pointer-events', 'none');
    svg.appendChild(r);
  }
  const x = Math.min(b.x0, b.x1), y = Math.min(b.y0, b.y1);
  r.setAttribute('x', x); r.setAttribute('y', y);
  r.setAttribute('width', Math.abs(b.x1 - b.x0));
  r.setAttribute('height', Math.abs(b.y1 - b.y0));
}
function gvBoxClear(){
  const r = document.getElementById('gv-box');
  if(r && r.parentNode) r.parentNode.removeChild(r);
}
function gvBoxCommit(){
  const b = graphState.box;
  graphState.box = null; gvBoxClear();
  const svg = document.getElementById('gv-svg');
  if(svg) svg.style.cursor = '';
  if(!b){ renderGraph(); return; }
  const x0 = Math.min(b.x0,b.x1), x1 = Math.max(b.x0,b.x1);
  const y0 = Math.min(b.y0,b.y1), y1 = Math.max(b.y0,b.y1);
  // 误触（Shift+单击）不给多选，按清空处理
  if((x1-x0) < 4 && (y1-y0) < 4){ graphState.multiSel = new Set(); renderGraph(); return; }
  const hidden = (graphState.fold && graphState.fold.hidden) || null;
  const hit = new Set();
  (graphState.nodes||[]).forEach(n=>{
    if(hidden && hidden.has(String(n.id))) return;             // 折叠域内的节点不可选
    if(n.x==null || n.y==null) return;
    const p = gvWorldToScreen(n.x, n.y);
    if(p.x>=x0 && p.x<=x1 && p.y>=y0 && p.y<=y1) hit.add(String(n.id));
  });
  graphState.multiSel = hit;
  renderGraph();
  gvMultiSelDetail();
  toast(hit.size ? ('已框选 ' + hit.size + ' 个节点') : '框选范围内没有节点');
}
function gvMultiSelClear(){
  graphState.multiSel = new Set();
  renderGraph();
  showGraphDetail(null);
}
function gvMultiSelDetail(){
  const body = document.getElementById('gv-detail-body');
  const ids = [...(graphState.multiSel||new Set())];
  if(!body || !ids.length) return;
  const byId = new Map((graphState.nodes||[]).map(n=>[String(n.id), n]));
  const rows = ids.map(id=>byId.get(id)).filter(Boolean).slice(0,50).map(n=>
    `<div style="display:flex;gap:6px;align-items:center;padding:2px 0;">
       <span style="width:8px;height:8px;border-radius:50%;background:${graphColor(n.entity_type)};flex:none;"></span>
       <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(n.name)}</span>
       <span style="color:var(--mut);font-size:11.5px;">${esc(n.entity_type)}</span></div>`).join('');
  body.innerHTML = `<div style="font-size:13px;">
    <div style="font-weight:700;color:var(--blue-d);margin-bottom:6px;">框选 ${ids.length} 个节点</div>
    <div style="max-height:340px;overflow:auto;">${rows}</div>
    ${ids.length>50?'<div style="color:var(--mut);font-size:12px;margin-top:6px;">仅列出前 50 个</div>':''}
    <div style="color:var(--mut);font-size:12px;margin-top:8px;line-height:1.6;">
      提示：Shift+拖动可重新框选，点 ✕ 或点任意节点退出多选。<br>
      当前仅支持「框选 + 批量查看」；批量改类型 / 批量连关系属后续阶段，未在本版提供。
    </div></div>`;
}

// ── P1-4 路径高亮：无向 BFS 最短路（最少跳数；同层按关系类型字典序，口径可复现） ──
let gvPathMode = false, gvPathSrc = null;
function gvTogglePathMode(){
  if(!gvPathMode && typeof gvLinkMode !== 'undefined' && gvLinkMode) gvToggleLinkMode();   // 与连线模式互斥
  gvPathMode = !gvPathMode; gvPathSrc = null;
  const btn = document.getElementById('gv-path-btn');
  if(btn){
    btn.style.background = gvPathMode ? graphTok('primaryL') : '';
    btn.style.color = gvPathMode ? graphTok('primaryD') : '';
    btn.style.fontWeight = gvPathMode ? '600' : '';
  }
  const tip = document.getElementById('gv-path-tip');
  if(tip) tip.style.display = gvPathMode ? '' : 'none';
  const svg = document.getElementById('gv-svg');
  if(svg) svg.style.cursor = gvPathMode ? 'crosshair' : '';
  if(!gvPathMode){ graphState.path = null; graphState.sel = null; }
  renderGraph();
}
function gvPathClear(){
  gvPathMode = false; gvPathSrc = null; graphState.path = null;
  const btn = document.getElementById('gv-path-btn');
  if(btn){ btn.style.background = ''; btn.style.color = ''; btn.style.fontWeight = ''; }
  const tip = document.getElementById('gv-path-tip'); if(tip) tip.style.display = 'none';
  const svg = document.getElementById('gv-svg'); if(svg) svg.style.cursor = '';
  renderGraph();
}
function gvPathPick(id){
  id = String(id);
  if(id.startsWith('sk:')){ toast('骨架聚合节点不支持路径查找，请下钻到实例'); return; }
  if(!gvPathSrc){
    gvPathSrc = id; graphState.sel = {type:'node', id}; renderGraph();
    toast('已设起点，请点终点（Esc 退出路径模式）');
    return;
  }
  if(gvPathSrc === id){ gvPathSrc = null; graphState.path = null; renderGraph(); return; }
  const r = gvPathBFS(gvPathSrc, id);
  gvPathSrc = null;
  if(!r){
    graphState.path = null; graphState.sel = null; renderGraph();
    toast('⚠ 这两点之间在当前画布内没有可达路径');
    return;
  }
  graphState.path = r;
  graphState.sel = null;
  renderGraph();
  gvPathDetail(r);
}
function gvPathBFS(from, to){
  from = String(from); to = String(to);
  const adj = new Map();
  const push = (a, b, eid, rel) => {
    if(!adj.has(a)) adj.set(a, []);
    adj.get(a).push({to:b, eid:eid, rel:rel});
  };
  (graphState.edges||[]).forEach(e=>{
    const s = String(e.source_id), t = String(e.target_id), eid = String(e.id), rel = String(e.relation_type||'');
    push(s, t, eid, rel); push(t, s, eid, rel);     // 无向：追踪链常常要逆着关系方向找
  });
  // 同层按关系类型字典序，再按目标 id —— 保证同一份数据每次得到的路径完全一致
  adj.forEach(list=>list.sort((a,b)=> a.rel.localeCompare(b.rel) || a.to.localeCompare(b.to)));
  const prev = new Map([[from, null]]);
  const q = [from];
  while(q.length){
    const cur = q.shift();
    if(cur === to) break;
    for(const st of (adj.get(cur)||[])){
      if(prev.has(st.to)) continue;
      prev.set(st.to, {from:cur, eid:st.eid, rel:st.rel});
      q.push(st.to);
    }
  }
  if(!prev.has(to)) return null;
  const nodeIds = new Set([to]), edgeIds = new Set(), steps = [];
  let cur = to;
  while(cur !== from){
    const p = prev.get(cur);
    if(!p) break;
    edgeIds.add(p.eid); nodeIds.add(p.from);
    steps.unshift({from:p.from, to:cur, rel:p.rel, eid:p.eid});
    cur = p.from;
  }
  return {from:from, to:to, nodeIds:nodeIds, edgeIds:edgeIds, steps:steps, hops:steps.length};
}
function gvPathDetail(r){
  const body = document.getElementById('gv-detail-body');
  if(!body) return;
  const byId = new Map((graphState.nodes||[]).map(n=>[String(n.id), n]));
  const nm = id => ((byId.get(String(id))||{}).name) || String(id);
  const chain = r.steps.map(st=>
    `<div style="display:flex;gap:6px;align-items:baseline;padding:2px 0;">
       <span style="color:var(--mut);font-size:11.5px;min-width:14px;">${''}</span>
       <span style="flex:1;min-width:0;"><b>${esc(nm(st.from))}</b>
         <span style="color:${graphTok('primary')};font-size:12px;">—${esc(st.rel)}→</span>
         <b>${esc(nm(st.to))}</b></span></div>`).join('');
  body.innerHTML = `<div style="font-size:13px;">
    <div style="font-weight:700;color:var(--blue-d);margin-bottom:4px;">🧭 路径 · ${r.hops} 跳</div>
    <div style="color:var(--mut);font-size:12px;margin-bottom:8px;">${esc(nm(r.from))} → ${esc(nm(r.to))}（BFS 最少跳数；同层按关系类型字典序）</div>
    <div style="max-height:340px;overflow:auto;">${chain}</div>
    <div style="color:var(--mut);font-size:12px;margin-top:8px;">路径上的节点与关系已高亮，其余淡出；再点「🧭 路径」或按 Esc 退出。</div></div>`;
}

// ── P1-2 双击节点：只看它的 1-hop（其余折叠），再次双击或点 +N 徽标恢复 ──
function gvUnfold(){
  if(!graphState.fold) return;
  graphState.fold = null;
  renderGraph();
  gvFitView();   // 展开后回到全图视角（与折叠时的聚焦视图对称）
}
function graphNodeDblClick(ev, id){
  if(ev){ ev.stopPropagation(); if(ev.preventDefault) ev.preventDefault(); }
  if(typeof gvLinkMode !== 'undefined' && gvLinkMode) return;   // 连线/路径模式下双击不抢语义
  if(typeof gvPathMode !== 'undefined' && gvPathMode) return;
  if(graphState.sk && graphState.sk.on) return;                  // 骨架模式有自己的下钻
  id = String(id);
  const cur = graphState.fold;
  if(cur && String(cur.nid) === id){ gvUnfold(); toast('已展开全部节点'); return; }
  const nb = new Set([id]);
  (graphState.edges||[]).forEach(e=>{
    if(String(e.source_id)===id) nb.add(String(e.target_id));
    if(String(e.target_id)===id) nb.add(String(e.source_id));
  });
  const hidden = new Set();
  (graphState.nodes||[]).forEach(n=>{ if(!nb.has(String(n.id))) hidden.add(String(n.id)); });
  if(!hidden.size){ toast('该节点已连到全部可见节点，无需折叠'); return; }
  graphState.fold = {nid:id, hidden:hidden, keep:nb.size};
  graphState.sel = {type:'node', id:id};
  graphState.multiSel = new Set();
  graphState.path = null;
  renderGraph();
  gvFitView();   // 折叠后把视图收到剩下的这一片，否则画面空旷、焦点飘在角落
  showGraphDetail(id);
  toast('已折叠 ' + hidden.size + ' 个非直接关联节点（双击该节点或点 +N 徽标恢复）');
}
// ── P1-3 布局参数（滑杆，防抖 220ms 后重排）与局部重排（只动选中节点的 1-hop） ──
let _gvLoTimer = 0;
function gvLayoutOptInput(key, val){
  const o = graphState.view.layoutOpts = graphState.view.layoutOpts || {sep:80, rep:26000, iters:500};
  o[key] = Number(val);
  const lbl = document.getElementById('gv-lo-' + key + '-v');
  if(lbl) lbl.textContent = String(val);
  if(_gvLoTimer) clearTimeout(_gvLoTimer);
  _gvLoTimer = setTimeout(()=>{ _gvLoTimer = 0; graphLayout(); renderGraph(); }, 220);
}
function gvRelayoutAround(){
  const sel = graphState.sel;
  if(!sel || sel.type !== 'node'){ toast('请先点选一个节点，再局部重排它的 1-hop'); return; }
  const id = String(sel.id);
  const nb = new Set([id]);
  (graphState.edges||[]).forEach(e=>{
    if(String(e.source_id)===id) nb.add(String(e.target_id));
    if(String(e.target_id)===id) nb.add(String(e.source_id));
  });
  const sub = (graphState.nodes||[]).filter(n=>nb.has(String(n.id)) && n.x!=null && n.y!=null);
  if(sub.length < 3){ toast('该节点邻域只有 ' + sub.length + ' 个节点，局部重排意义不大'); return; }
  const subEdges = (graphState.edges||[]).filter(e=>nb.has(String(e.source_id)) && nb.has(String(e.target_id)));
  // 原包围盒：重排后整体归一回原位，避免这一片被搬到画布别处、跟周围节点脱节
  const bx0 = Math.min(...sub.map(n=>n.x)), bx1 = Math.max(...sub.map(n=>n.x));
  const by0 = Math.min(...sub.map(n=>n.y)), by1 = Math.max(...sub.map(n=>n.y));
  if(!gvLayoutWith('cose', sub, subEdges, {sep:70, rep:22000, iters:300})){
    toast('局部重排失败：布局引擎不可用（已回退，坐标未改动）');
    return;
  }
  const ax0 = Math.min(...sub.map(n=>n.x)), ax1 = Math.max(...sub.map(n=>n.x));
  const ay0 = Math.min(...sub.map(n=>n.y)), ay1 = Math.max(...sub.map(n=>n.y));
  const sw = (ax1-ax0) || 1, sh = (ay1-ay0) || 1;
  const tw = Math.max(bx1-bx0, 60), th = Math.max(by1-by0, 60);
  sub.forEach(n=>{ n.x = bx0 + (n.x-ax0)/sw*tw; n.y = by0 + (n.y-ay0)/sh*th; });
  renderGraph();
  gvEnsureMinimap(graphState.nodes, graphState.edges);
  // 局部重排是「整理视图」动作，不回写 graph_x/graph_y（回写会发 N 个请求，且与拖拽落位的语义不同）；
  // 需要固化时逐个拖动即可（拖拽会 saveNodePos）。
  toast('已局部重排 ' + sub.length + ' 个节点（其余节点坐标不变；未回写坐标）');
}

// Esc 统一退出：路径 → 连线 → 多选 → 折叠（从最"临时"的状态开始收）
if(!window.__gvP1EscBound){
  window.__gvP1EscBound = 1;
  document.addEventListener('keydown', function(e){
    if(e.key !== 'Escape') return;
    if(typeof gvPathMode !== 'undefined' && gvPathMode){ gvPathClear(); return; }
    if(typeof gvLinkMode !== 'undefined' && gvLinkMode){ gvLinkCancel(); return; }
    if((graphState.multiSel||new Set()).size){ gvMultiSelClear(); return; }
    if(graphState.fold){ gvUnfold(); return; }
  });
}

/* ══ P2-4（2026-11-28）键盘可达性 ══
   目标：不用鼠标也能读图 —— Tab 进画布、方向键在 1-hop 邻居间移动、Enter 看详情、Esc 取消。
   两条硬约束：
   ① renderGraph 用 innerHTML 重建整图 ⇒ **DOM 焦点必丢**。焦点不靠浏览器记忆，
      而是记在 graphState.focusNid，重建后由 gvRestoreFocus 复位 ——
      否则按一次方向键焦点就掉到 body，键盘导航无法连续。
   ② 方向键绝不能变成页面滚动：命中方向键一律 preventDefault。
   方向选邻：轴向投影 >1（确实在该方向上前进）且横偏越小越优先（score = along - across*2）；
   平票取 id 字典序小者 —— 与社区/路径同一确定性约定，同一张图同一按键必到同一节点。
   测试位：verify_graph_p2_render.py K 组。 */
const GV_DIRS = {ArrowRight:[1,0], ArrowLeft:[-1,0], ArrowUp:[0,-1], ArrowDown:[0,1]};
function gvNeighborInDirection(nodes, edges, fromId, dir){
  const d = GV_DIRS[dir];
  if(!d) return null;
  const from = (nodes||[]).find(n=> n && String(n.id) === String(fromId));
  if(!from || from.x == null || from.y == null) return null;
  const ids = new Set();
  (edges||[]).forEach(e=>{
    if(String(e.source_id) === String(fromId)) ids.add(String(e.target_id));
    if(String(e.target_id) === String(fromId)) ids.add(String(e.source_id));
  });
  const ax = d[0], ay = d[1];
  let best = null, bestScore = -Infinity;
  (nodes||[]).forEach(n=>{
    if(!n || !ids.has(String(n.id)) || n.x == null || n.y == null) return;
    const dx = n.x - from.x, dy = n.y - from.y;
    const along = dx * ax + dy * ay;              // 轴向投影：<=1 视为「不在那个方向」
    if(along <= 1) return;
    const across = Math.abs(dx * ay - dy * ax);   // 横向偏移
    const score = along - across * 2;             // 越正对越优先
    if(score > bestScore || (score === bestScore && String(n.id) < String(best))){ bestScore = score; best = String(n.id); }
  });
  return best;
}
function gvNodeFocus(id){ graphState.focusNid = String(id); }
function gvNodeBlur(){ /* 仅失去 DOM 焦点：保留 focusNid，供 renderGraph 重建后复位 */ }
function gvRestoreFocus(svg){
  const nid = graphState.focusNid;
  if(!svg || nid == null) return false;
  const list = svg.querySelectorAll ? svg.querySelectorAll('.gv-node') : [];
  for(let i=0;i<list.length;i++){
    const g = list[i];
    if(String(g.getAttribute('data-nid')) !== String(nid)) continue;
    try{ if(g.focus) g.focus({preventScroll:true}); }catch(e){ try{ g.focus(); }catch(_){} }
    return true;
  }
  return false;
}
function gvNodeActivate(id){
  const nid = String(id);
  graphState.sel = {type:'node', id:nid};
  graphState.focusNid = nid;
  graphState.path = null;
  renderGraph();
  try{ showGraphDetail(nid); }catch(e){}
}
function gvNodeKey(ev, id){
  if(!ev) return;
  const key = ev.key;
  if(key === 'Enter' || key === ' ' || key === 'Spacebar'){
    if(ev.preventDefault) ev.preventDefault();
    if(ev.stopPropagation) ev.stopPropagation();
    gvNodeActivate(id);
    return;
  }
  if(key === 'Escape'){
    if(ev.preventDefault) ev.preventDefault();
    graphState.sel = null; graphState.focusNid = null;
    renderGraph();
    toast('已取消选中（Esc）');
    return;
  }
  if(GV_DIRS[key]){
    if(ev.preventDefault) ev.preventDefault();   // 方向键不得滚动页面
    if(ev.stopPropagation) ev.stopPropagation();
    const to = gvNeighborInDirection(graphState.nodes, graphState.edges, id, key);
    if(to){ graphState.focusNid = to; renderGraph(); }
    else toast('该方向没有相邻节点');
  }
}
function graphEdgeClick(id) {
  graphState.sel = {type:'edge', id:String(id)};
  graphState.drag = null; graphState.pan = null;
  renderGraph();
  const sid = String(id);
  if(sid.startsWith('sk:')) return;                        // 骨架聚合边只高亮（无实体详情）
  if(sid.startsWith('inf:')) {                            // 推断边：详情面板显示推理来源
    const e = (graphState.edges||[]).find(x=>String(x.id)===sid);
    const body = document.getElementById('gv-detail-body');
    if(body && e) body.innerHTML = `<div style="padding:4px;">
      <div style="font-weight:700;color:#E8912D;font-size:13.5px;margin-bottom:6px;">⇢ 推断关系（${esc(e._kind||'reasoning')}）</div>
      <div style="font-size:12.5px;line-height:1.8;">
        <b>${esc(e._sName||e.source_id)}</b> —<b>${esc(e.relation_type)}</b>→ <b>${esc(e._tName||e.target_id)}</b></div>
      ${e._via?`<div style="font-size:12px;color:var(--mut);margin-top:6px;">推导路径：${esc(e._via)}</div>`:''}
      <div style="font-size:12px;color:var(--mut);margin-top:8px;">该边由推理引擎生成，未入库前不参与审核链；点击「推理」Tab 可物化入库。</div></div>`;
    return;
  }
  showGraphEdgeDetail(sid);
}
async function saveNodePos(n) {
  let np = {};
  try { np = JSON.parse(n.properties||'{}'); } catch(e) {}
  try {
    await api(`/api/knowledge/graph/nodes/${encodeURIComponent(n.id)}`, {method:'PUT', body:JSON.stringify({
      name:n.name, entity_type:n.entity_type, properties:np, x:Math.round(n.x), y:Math.round(n.y), branch: getCurrentBranch()})});
  } catch(e) {}
}
// 2026-09-10 米爸裁剪：graphDeleteNode 已删——实例不允许直接删除（画布右键入口同步移除）；下线走审核驳回（deprecated）流程
/* ══ P2-3（2026-11-28）导出当前视野 SVG / PNG ══
   与既有「全量数据导出」（JSON / GraphML / SysML）互补：那两个导出的是**分支数据**，
   与画布当前所见无关；这两个导出的是**画面上这一张**（含筛选/折叠/高亮/缩放）。
   坑位（都在真机上踩过或已知会踩）：
   ① 页面里的 <svg> 是用 innerHTML 造的，**没有 xmlns** —— 序列化成独立文件后浏览器不认，直接打不开；
   ② 画布白底来自 CSS background，**序列化不带走** ⇒ 必须补一块 <rect> 底，否则导出透明底；
   ③ 选中光晕/多选/路径/标签密度都写在外部样式表的 class 里，**序列化不带走** ⇒ 需内联必要规则；
   ④ hover 压暗（.gv-hov-dim）是鼠标位置的一次性产物，导出前必须剥掉，否则导出的图会「有块地方莫名变淡」；
   ⑤ 中文字体：SVG 文本靠系统字体渲染，显式写字体栈（雅黑/苹方）避免栅格化时退化成衬线；
   ⑥ PNG 用 canvas 光栅化，单边 >4096 或总面积 >2400 万像素时多数浏览器会返回空白 ⇒ 先降采样。
   测试位：verify_graph_p2_render.py X 组（含变异：删掉 xmlns 必须翻红）。 */
function gvExportBBox(){
  const svg = document.getElementById('gv-svg');
  const inner = document.getElementById('gv-inner');
  if(!svg) return null;
  const vp = graphState.vp || {scale:1, panX:0, panY:0};
  const s = +vp.scale || 1, px = +vp.panX || 0, py = +vp.panY || 0;
  let bb = null;
  try{
    if(inner && typeof inner.getBBox === 'function'){
      const b = inner.getBBox();
      // getBBox 返回的是 #gv-inner **自身局部坐标**（不含它自己的 transform），
      // 而 viewBox 要用视口坐标 ⇒ 必须再乘 scale、加 pan 换算一次。
      if(b && b.width > 0 && b.height > 0) bb = {x: b.x * s + px, y: b.y * s + py, width: b.width * s, height: b.height * s};
    }
  }catch(e){}
  if(!bb){   // 退化：getBBox 不可用（无布局/空图）时按当前视口裁
    const vw = svg.clientWidth || graphState.svgW || 880, vh = svg.clientHeight || graphState.svgH || 460;
    bb = {x: 0, y: 0, width: vw, height: vh};
  }
  const pad = 18;
  return {x: Math.round(bb.x - pad), y: Math.round(bb.y - pad),
          width: Math.max(40, Math.ceil(bb.width + pad * 2)), height: Math.max(40, Math.ceil(bb.height + pad * 2))};
}
function gvExportSvgSource(){
  const svg = document.getElementById('gv-svg');
  const bb = gvExportBBox();
  if(!svg || !bb) return null;
  const k = graphTok();
  const NS = 'http://www.w3.org/2000/svg';
  const clone = svg.cloneNode(true);
  // ① hover 态是一次性产物，剥掉（保留选中/多选/路径/折叠等有意的态）
  clone.querySelectorAll('.gv-hov-dim, .gv-hov-ring').forEach(el=>{
    el.classList.remove('gv-hov-dim'); el.classList.remove('gv-hov-ring');
  });
  // ② 根元素补全：xmlns / 尺寸 / viewBox / 字体栈
  clone.setAttribute('xmlns', NS);
  clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
  clone.setAttribute('width', String(bb.width));
  clone.setAttribute('height', String(bb.height));
  clone.setAttribute('viewBox', bb.x + ' ' + bb.y + ' ' + bb.width + ' ' + bb.height);
  clone.setAttribute('style', 'background:' + k.canvas + ";font-family:'Microsoft YaHei','PingFang SC','Segoe UI',sans-serif;");
  // ③ 画布底色（CSS 背景不会被序列化带走）
  const bg = document.createElementNS(NS, 'rect');
  bg.setAttribute('x', String(bb.x)); bg.setAttribute('y', String(bb.y));
  bg.setAttribute('width', String(bb.width)); bg.setAttribute('height', String(bb.height));
  bg.setAttribute('fill', k.canvas);
  clone.insertBefore(bg, clone.firstChild);
  // ④ 类样式内联（外部样式表不进序列化结果）
  const st = document.createElementNS(NS, 'style');
  st.textContent = '.gv-flow-out,.gv-flow-in{stroke-dasharray:8 8;stroke-width:2.6}'
    + '#gv-svg.gv-far .gv-elabel{opacity:0}#gv-svg.gv-farther .gv-nlabel{opacity:0}'
    + '.gv-ms-node>circle:first-of-type{fill:' + k.primaryL + ';stroke:' + k.primary + ';stroke-width:3.2}'
    + '.gv-path-node>circle:first-of-type{stroke:' + k.primaryD + ';stroke-width:3.4}'
    + '.gv-sel-node>circle:first-of-type{stroke-width:3}';
  clone.insertBefore(st, bg.nextSibling);
  // ⑤ 导出图不该带焦点/内联交互
  clone.querySelectorAll('[tabindex]').forEach(el=> el.removeAttribute('tabindex'));
  const src = new XMLSerializer().serializeToString(clone);
  return {src, w: bb.width, h: bb.height, box: bb};
}
/* PNG 光栅化倍率：默认 2 倍；单边 4096 / 总面积 2400 万像素是多数浏览器的安全上限，
   超出会得到空白图（不是画得慢，是直接没有内容），必须提前降采样。 */
function gvExportPngScale(w, h, max){
  const cap = max || 4096, areaCap = 24e6;
  let s = Math.min(2, cap / Math.max(1, w), cap / Math.max(1, h), Math.sqrt(areaCap / Math.max(1, w * h)));
  return Math.max(0.25, Math.round(s * 100) / 100);
}
function gvDownloadBlob(blob, name){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(()=>{ try{ URL.revokeObjectURL(a.href); }catch(e){} }, 1500);
}
function gvExportView(fmt){
  const got = gvExportSvgSource();
  if(!got){ toast('当前没有可导出的画布'); return; }
  const br = String(getCurrentBranch() || 'graph').replace(/[^\w.-]+/g, '_');
  const d = new Date(), p2 = (n)=> String(n).padStart(2, '0');
  const stamp = d.getFullYear() + p2(d.getMonth()+1) + p2(d.getDate()) + '-' + p2(d.getHours()) + p2(d.getMinutes());
  if(fmt === 'svg'){
    gvDownloadBlob(new Blob([got.src], {type:'image/svg+xml;charset=utf-8'}), 'graph-view-' + br + '-' + stamp + '.svg');
    toast('✒ 已导出当前视野 SVG（' + got.w + '×' + got.h + '）');
    return;
  }
  const scale = gvExportPngScale(got.w, got.h);
  const tw = Math.max(1, Math.round(got.w * scale)), th = Math.max(1, Math.round(got.h * scale));
  toast('🖼 正在生成 PNG（' + tw + '×' + th + '）…');
  const img = new Image();
  img.onload = ()=>{
    try{
      const cv = document.createElement('canvas');
      cv.width = tw; cv.height = th;
      const ctx = cv.getContext('2d');
      ctx.fillStyle = graphTok('canvas'); ctx.fillRect(0, 0, tw, th);
      ctx.drawImage(img, 0, 0, tw, th);
      cv.toBlob((b)=>{
        if(!b){ toast('PNG 生成失败：画布过大或内存不足'); return; }
        gvDownloadBlob(b, 'graph-view-' + br + '-' + stamp + '.png');
        toast('🖼 已导出当前视野 PNG（' + tw + '×' + th + '）');
      }, 'image/png');
    }catch(e){ toast('PNG 生成失败：' + (e.message || e)); }
  };
  img.onerror = ()=> toast('PNG 生成失败：SVG 光栅化失败');
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(got.src);
}
function graphExport(fmt) {
  // 2026-09-10 修复：补 branch 参数（原实现后端默认 dev，personal 分支下会导出错分支数据）
  const br = getCurrentBranch();
  window.open('/api/knowledge/graph/export?fmt=' + (fmt||'json') + '&branch=' + encodeURIComponent(br), '_blank');
}
// ── 图谱 → SysML V2 (KerML) 反向导出（FR-KG-7 补 G11）：弹窗预览 + 复制/下载 ──
async function exportSysmlModel() {
  const br = getCurrentBranch();
  const lbx = document.getElementById('lbx');
  const b = document.getElementById('lbx-body');
  b.innerHTML = `<h3>📤 导出 SysML V2 模型 <span style="font-size:12.5px;color:var(--mut);font-weight:normal;">分支 ${esc(br)}</span></h3><div class="loading">生成中…</div>`;
  lbx.classList.add('show');
  lbx.onclick = e => { if(e.target===lbx) lbx.classList.remove('show'); };
  const cb = document.querySelector('#lbx .lbx-close');
  if(cb) cb.onclick = () => lbx.classList.remove('show');
  const fail = (msg) => { b.innerHTML = `<h3>导出失败</h3><div style="color:var(--red);font-size:13.5px;">${esc(msg)}</div><div class="lbx-actions"><button class="btn ghost" onclick="document.getElementById('lbx').classList.remove('show')">关闭</button></div>`; };
  let ker;
  try {
    const r = await fetch(API + '/api/knowledge/sysml/export?branch=' + encodeURIComponent(br));
    if(!r.ok){ fail('HTTP ' + r.status); return; }
    ker = await r.text();
  } catch(e) { fail(e.message); return; }
  b.innerHTML = `<h3>📤 导出 SysML V2 模型 <span style="font-size:12.5px;color:var(--mut);font-weight:normal;">分支 ${esc(br)} · ${ker.split('\n').length} 行</span></h3>
    <pre id="se-pre" style="background:#1e1e1e;color:#d4d4d4;padding:10px;border-radius:6px;font-size:12.5px;line-height:1.5;max-height:52vh;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:8px 0;">${esc(ker)}</pre>
    <div class="lbx-actions"><button class="btn ghost" id="se-copy">📋 复制</button><button class="btn" id="se-download">⬇ 下载</button><button class="btn ghost" onclick="document.getElementById('lbx').classList.remove('show')">关闭</button></div>`;
  document.getElementById('se-copy').onclick = () => {
    const done = () => toast('✅ 已复制到剪贴板');
    if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(ker).then(done).catch(()=>{});
    } else {
      const ta = document.createElement('textarea');
      ta.value = ker; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch(e) { toast('复制失败，请手动选择'); }
      document.body.removeChild(ta);
    }
  };
  document.getElementById('se-download').onclick = () => {
    const blob = new Blob([ker], {type:'text/plain;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'graph-export.sysml.ker';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
    toast('⬇ 已下载 graph-export.sysml.ker');
  };
}
// ── 一键追溯（2026-09-10 状态机收口：候选审核收敛至数据整理·审核队列，画布内 gvReviewNode/gvReviewEdge 已删）──
async function gvSaveEdge(id) {
  if(!branchWritable()) return;
  const rt = document.getElementById('gv-edge-type').value.trim();
  if(!rt) { toast('关系类型不能为空'); return; }
  const r = await api(`/api/knowledge/graph/edges/${id}`, {method:'PUT', body:JSON.stringify({relation_type: rt, branch: getCurrentBranch()})});
  if(r && r.error) { toast('保存失败：' + r.error); return; }
  toast('✅ 关系已修正');
  loadGraph();
}
// 合并节点（2026-09-10 米爸重做）：目标改为「同类型实体」搜索下拉选择，不再手输 ID
// 合并逻辑：源节点 A → 目标 B：A 的关联关系全部转移至 B，A 删除（后端 /entities/{id}/merge）；仅同本体类型可合并（类型不同则属性/关系语义不一致）
async function gvMergeNode(id) {
  if(!branchWritable()) return;
  const all = graphState.all.nodes||[];
  const src = all.find(x=>x.id===id);
  if(!src){ toast('源节点不在当前数据中'); return; }
  const typ = src.entity_type||'';
  if(!typ){ toast('该实体未标注本体类型，无法确定可合并范围（仅同类型可合并）'); return; }
  const candidates = all.filter(x=>x.id!==id && (x.entity_type||'')===typ)
    .sort((a,b)=>String(a.name).localeCompare(String(b.name),'zh'));
  if(!candidates.length){ toast(`没有同类型「${typ}」的其他实体可合并`); return; }
  const lbx = document.getElementById('lbx'), b = document.getElementById('lbx-body');
  if(!lbx || !b) return;
  b.innerHTML = `<h3>🔄 合并节点到…</h3>
    <div style="font-size:13.5px;color:var(--mut);line-height:1.7;margin:4px 0 10px;">
      将「<b>${esc(src.name)}</b>」合并到同类型目标实体：<b>关联关系全部转移至目标，本节点被删除</b>（不可逆，建议先确认目标数据完整）。<br>
      可选范围 = 本体类型「<b style="color:var(--blue-d);">${esc(typ)}</b>」下的 ${candidates.length} 个实体：
    </div>
    <input id="gv-merge-q" placeholder="🔍 搜索目标实体（名称/ID）…" oninput="gvMergeRenderList('${esc(id)}')" autocomplete="off"
      style="width:100%;border:1px solid var(--blue);border-radius:6px;padding:6px 10px;font-size:13.5px;outline:none;margin-bottom:8px;">
    <div id="gv-merge-list" style="max-height:300px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;"></div>
    <div class="lbx-actions" style="margin-top:10px;text-align:right;">
      <button class="btn ghost" onclick="document.getElementById('lbx').classList.remove('show')">取消</button>
    </div>`;
  gvMergeRenderList(id);
  lbx.classList.add('show');
  lbx.onclick = e => { if(e.target===lbx) lbx.classList.remove('show'); };
  setTimeout(()=>{ const q=document.getElementById('gv-merge-q'); if(q) q.focus(); }, 50);
}
function gvMergeRenderList(srcId){
  const src = (graphState.all.nodes||[]).find(x=>x.id===srcId); if(!src) return;
  const qEl = document.getElementById('gv-merge-q');
  const q = (qEl?qEl.value:'').trim().toLowerCase();
  const box = document.getElementById('gv-merge-list'); if(!box) return;
  const list = (graphState.all.nodes||[]).filter(x=>x.id!==srcId && (x.entity_type||'')===(src.entity_type||'')
    && (!q || String(x.name||'').toLowerCase().includes(q) || String(x.id||'').toLowerCase().includes(q)));
  box.innerHTML = list.length ? list.slice(0,50).map(x=>`
    <div style="display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:pointer;border-bottom:1px solid #f0efe9;" onmouseover="this.style.background='var(--blue-l,#E6F1FB)'" onmouseout="this.style.background=''" onclick="gvMergeConfirm('${esc(srcId)}','${esc(x.id)}')">
      <span style="width:8px;height:8px;border-radius:50%;background:${graphColor(x.entity_type)};flex:none;"></span>
      <b style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(x.name)}</b>
      <span class="st ${x.status==='reviewed'?'ok':'w'}" style="flex:none;">${esc(gvStatusTxt(x.status))}</span>
      <span style="color:var(--mut);font-size:12px;flex:none;" title="${esc(x.id)}">${esc(String(x.id).slice(0,10))}…</span>
    </div>`).join('')
    : '<div style="padding:10px;color:var(--mut);font-size:12.5px;">无匹配的同类型实体</div>';
  if(list.length>50){
    box.insertAdjacentHTML('beforeend', `<div style="padding:6px 10px;color:var(--mut);font-size:12px;">共 ${list.length} 条，仅显示前 50 条，请继续输入过滤</div>`);
  }
}
async function gvMergeConfirm(srcId, tgtId){
  const src = (graphState.all.nodes||[]).find(x=>x.id===srcId);
  const tgt = (graphState.all.nodes||[]).find(x=>x.id===tgtId);
  if(!src || !tgt) return;
  if(!(await confirmDialog(`确认将「${src.name}」合并到「${tgt.name}」？\n关联关系将全部转移至目标，本节点被删除（不可逆）。`))) return;
  const r = await api(`/api/knowledge/entities/${encodeURIComponent(srcId)}/merge`, {method:'POST', body:JSON.stringify({target_id: tgtId, branch: getCurrentBranch()})});
  if(r && r.error) { toast('合并失败：' + r.error); return; }
  document.getElementById('lbx').classList.remove('show');
  toast('✅ 合并完成：关联关系已转移至「'+esc(tgt.name)+'」');
  loadGraph();
}
async function gvTraceSource(doc) {
  if(!doc) { toast('无来源文档'); return; }
  try {
    const r = await api('/api/documents?search=' + encodeURIComponent(doc));
    const list = Array.isArray(r) ? r : (r.items || r.list || []);
    if(!list || !list.length) { toast('未找到来源文档：' + doc); return; }
    const d = list.find(x=>x.filename===doc) || list[0];
    await viewDocTrace(d.id);
  } catch(e) { toast('追溯失败：' + e.message); }
}
// ── 命名子图（KB-D）：全图 / 模块子图 / 来源文档子图 / 已存自定义子图 ──
// 模块/来源文档分组与计数由后端 /api/knowledge/graph/views/modules 计算（单一事实源，单元测试保障计数准确）
let gvViewDefs = {};  // key → {label, types, docs, status, hideIsolated, layout, custom, id}
function gvAttr(s){ return String(s==null?'':s).replace(/"/g,'&quot;'); }
function gvRenderViewSelect(){
  const sel = document.getElementById('gv-view');
  if(!sel) return;
  const modOpts = [], docOpts = [], savedOpts = [];
  Object.keys(gvViewDefs).forEach(k=>{
    if(k==='all') return;
    const d = gvViewDefs[k];
    if(k.startsWith('module:')) modOpts.push(`<option value="${gvAttr(k)}">${esc(d.label.slice(2))}（${d.count}）</option>`);
    else if(k.startsWith('doc:')) docOpts.push(`<option value="${gvAttr(k)}">${esc(d.label.slice(2))}（${d.count}）</option>`);
    else if(k.startsWith('view:')) savedOpts.push(`<option value="${gvAttr(k)}">${esc(d.label.slice(2))}</option>`);
  });
  // 2026-09-04 优化：移除"🌐 全图"冗余入口（全图 = 无任何类型/来源过滤，是事实驱动的默认值，不需要单独按钮）
  sel.innerHTML =
    (modOpts.length?`<optgroup label="📦 模块子图">${modOpts.join('')}</optgroup>`:'') +
    (docOpts.length?`<optgroup label="📄 来源文档">${docOpts.join('')}</optgroup>`:'') +
    (savedOpts.length?`<optgroup label="⭐ 已存子图">${savedOpts.join('')}</optgroup>`:'');
  // 默认选中：模块子图中的第一项（如有），否则来源文档子图第一项，否则已存子图第一项，都没有则留空（= 恢复全图视图）
  const firstKey = (modOpts.length?'module:':'') + (docOpts.length?'doc:':'') + (savedOpts.length?'view:':'');
  const cur = graphState.view.scope;
  if(cur && gvViewDefs[cur]) sel.value = cur;
  else if(modOpts.length) sel.value = Object.keys(gvViewDefs).find(k=>k.startsWith('module:'));
  else if(docOpts.length) sel.value = Object.keys(gvViewDefs).find(k=>k.startsWith('doc:'));
  else if(savedOpts.length) sel.value = Object.keys(gvViewDefs).find(k=>k.startsWith('view:'));
  // 同步「🗑 删除」按钮的显隐
  const btn = document.getElementById('gv-view-del');
  if(btn) btn.style.display = (sel.value && sel.value.startsWith('view:')) ? '' : 'none';
}
async function gvLoadViews(){
  // 2026-09-10 米爸裁剪：命名子图下拉已从工具栏移除，DOM 不存在时不再拉取子图数据（省 2 个请求）
  if(!document.getElementById('gv-view')) return;
  try {
    const [mod, saved] = await Promise.all([
      api('/api/knowledge/graph/views/modules?branch=' + encodeURIComponent(getCurrentBranch()) + '&status=' + encodeURIComponent(graphState.view.status||'all')).catch(()=>null),
      api('/api/knowledge/graph/views?branch=' + encodeURIComponent(getCurrentBranch())).catch(()=>[]),
    ]);
    gvViewDefs = {'all': {label:'🌐 全图', types:[], docs:[]}};
    // 📦 模块子图（后端按本体层级计算，计数与当前状态视图一致）
    ((mod && mod.modules)||[]).forEach(g=>{
      const key = 'module:'+g.name;
      gvViewDefs[key] = {label:'📦 '+g.name, types: g.types||[], docs: [], count: g.count||0};
    });
    // 📄 来源文档子图
    ((mod && mod.docs)||[]).forEach(d=>{
      const key = 'doc:'+d.name;
      gvViewDefs[key] = {label:'📄 '+d.name, docs: [d.name==='未标注来源'?'':d.name], types: [], count: d.count||0};
    });
    // ⭐ 已存自定义子图
    (saved||[]).forEach(v=>{
      const cfg = v.config||{};
      const key = 'view:'+v.id;
      gvViewDefs[key] = {label:'⭐ '+v.name, types: cfg.entity_types||[], docs: cfg.docs||[],
        status: cfg.status, hideIsolated: cfg.hideIsolated, layout: cfg.layout, custom: true, id: v.id, count: ''};
    });
    gvRenderViewSelect();
  } catch(e) { /* 子图加载失败不阻断图谱 */ }
}
function gvSetView(key){
  const cfg = gvViewDefs[key];
  if(!cfg){ toast('子图不存在'); return; }
  const v = graphState.view;
  const prevStatus = v.status;
  v.scope = key;
  v.types = cfg.types||[];
  v.docs = cfg.docs||[];
  if(cfg.status) v.status = cfg.status;
  if(cfg.hideIsolated!==undefined) v.hideIsolated = cfg.hideIsolated;
  if(cfg.layout) v.layout = cfg.layout;
  const sel = document.getElementById('gv-view');
  if(sel) sel.value = key;
  if(cfg.status && cfg.status!==prevStatus){ loadGraph(); }
  else { graphApplyView(); graphLayout(); renderGraph(); gvRenderViewSelect(); }
  toast(cfg.label ? `子图：${cfg.label}` : '子图已切换');
}
async function gvDelView(){
  const key = graphState.view.scope||'all';
  if(!key.startsWith('view:')){ toast('仅可删除自定义子图（模块/文档子图为自动生成）'); return; }
  const vid = key.split(':')[1];
  if(!(await confirmDialog('删除该命名子图？'))) return;
  const r = await api(`/api/knowledge/graph/views/${vid}`, {method:'DELETE'});
  if(r && r.error){ toast('删除失败：'+r.error); return; }
  graphState.view.scope = 'all';
  toast('✅ 子图已删除');
  gvLoadViews(); gvSetView('all');
}
// ── O-3：SysML V2 导入图谱 ──
// ── 实例数据导入：四方式 + 预览 + 4 项推理验证 ──
let importMode = 'r2rml';
let importTurtle = '';
function setImportMode(m) {
  importMode = m;
  ['r2rml','csv','manual','nlp'].forEach(x=>{
    const el = document.getElementById('imp-mode-' + x);
    const on = x === m;
    el.className = 'st' + (on ? ' on' : '');
    el.style.background = on ? 'var(--blue-l)' : '';
    el.style.color = on ? 'var(--blue-d)' : '';
    const body = document.getElementById('imp-mode-' + x + '-body');
    if(body) body.style.display = on ? 'block' : 'none';
  });
  document.getElementById('imp-preview-area').style.display = 'none';
}
function renderReasoning(items) {
  return items.map(it=>{
    const icon = it.pass ? '✅' : '❌';
    const color = it.pass ? 'var(--grn)' : 'var(--red)';
    return `<div style="margin-bottom:4px;"><span style="color:${color};font-weight:600;">${icon} ${esc(it.name)}</span> <span style="color:var(--mut);font-size:12.5px;">— ${esc(it.detail || '')}</span></div>`;
  }).join('');
}
// P0-2：推理物化明细（分类/传递推断的三元组，可折叠）
function renderInferred(items) {
  if(!items || !items.length) return '';
  return `<div style="margin-top:6px;border-top:1px dashed var(--line);padding-top:6px;">
    <div style="font-size:12.5px;color:var(--blue-d);font-weight:600;cursor:pointer;" onclick="this.parentElement.querySelector('.inf-body').style.display = this.parentElement.querySelector('.inf-body').style.display==='none'?'block':'none';">🔎 推理物化明细（${items.length}） <span style="color:var(--mut);font-weight:400;">点击展开/收起</span></div>
    <div class="inf-body" style="display:none;margin-top:4px;">
      ${items.slice(0, 20).map(i=>{
        if(i.kind === 'classify')
          return `<div style="font-size:12.5px;color:var(--mut);margin-top:2px;"><span class="tag" style="font-size:11.5px;border-color:var(--blue);color:var(--blue-d);">分类</span> ${esc(i.s)} <b>a</b> ${esc(i.o)} <small style="color:var(--mut);">（via ${esc(i.via)}）</small></div>`;
        return `<div style="font-size:12.5px;color:var(--mut);margin-top:2px;"><span class="tag" style="font-size:11.5px;border-color:var(--amb);color:var(--amb);">传递</span> ${esc(i.path)}</div>`;
      }).join('')}
      ${items.length > 20 ? `<div style="font-size:12px;color:var(--mut);margin-top:2px;">… 其余 ${items.length-20} 条略</div>` : ''}
    </div>
  </div>`;
}
// P2 入口统一：NLP 抽取的粘贴文本 → 存入抽取治理中心（v2g 批次），批量确认后再入库
async function impToV2G() {
  const q = document.getElementById('imp-nlp-text')?.value?.trim();
  if(!q) { toast('请先在「NLP 抽取」输入框粘贴文档文本'); return; }
  const r = await api('/api/knowledge/v2g/extract', {method:'POST', body:JSON.stringify({query:q, top_k:5})});
  toast(`✅ 已存入治理中心：候选 ${(r.node_count||0)+(r.edge_count||0)} 条（批次 ${String(r.batch_id||'').slice(0,12)}…）`);
  gotoV2GBatch(r.batch_id);
}
function profileExport(fmt) {
  window.open('/api/knowledge/profile/export?fmt=' + fmt, '_blank');
  toast(fmt==='1x' ? 'SysML 1.x Profile 已导出（.profile）' : 'SysML 2.x Profile 已导出（.kerml）');
}
// ── 版本管理 ──
// 分支模型（git 风格）：release 只读预置（仅 dev 合并更新）/ dev 唯一主开发 / personal 为个人分支（从 dev|release 拉取基线，合并回 dev）
// 2026-09-12：系统内置分支 = dev/release/personal 三种；其余自定义分支允许删除
const PROTECTED_BRANCHES = ['dev', 'release', 'personal'];
const MERGE_TARGET_BRANCHES = ['dev', 'release'];   // 合并目标仅主开发/发布，与【保护名单】解耦（personal 内置但不可作合并目标）
const BRANCH_TYPE_LABEL = {release:'🔒 发布', dev:'开发', personal:'个人', local:'本地'};
function branchTypeBadge(t) { return `<span class="st ${t==='release'?'ok':t==='dev'?'b':'g'}">${BRANCH_TYPE_LABEL[t]||t}</span>`; }
// release 只读守卫：写操作（改节点/连边/上传/实体）前调用
function branchWritable() {
  if(isReleaseBranch(getCurrentBranch())){ toast('🔒 已发布(release)分支只读，请切换到 dev 分支编辑'); return false; }
  return true;
}
// ── 版本管理数据缓存与筛选状态（loadBranches / loadMerges 共用，供 KPI 概览与合并请求筛选）──
let _branches = null, _mrs = null, mergeFilter = 'all';
