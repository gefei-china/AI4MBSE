/* 分支：分支管理 / 提交 / MR / 合并 / diff
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 15245-16688  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function loadBranches() {
  const branches = await api('/api/branches');
  _branches = branches;
  try{ window._branches = branches; }catch(e){}
  const cur = getCurrentBranch();
  initGlobalBranch();   // 同步顶部全局分支选择器（隐藏 select 状态载体）
  renderBrDropdown();   // GitHub 式分支下拉（kb-d 工作区头部）
  syncWsBranchBtn(cur); // 刷新头部按钮状态（含「⇵ 同步基线」仅个人分支显示）
  const el = document.getElementById('branch-list');
  if(!el) { renderBranchKpis(); return; }   // 2026-09-04 合并队列页已移除，分支列表容器不再存在
  if(!branches || !branches.length) {
    el.innerHTML = '<div style="padding:10px;color:var(--mut);font-size:12px;">暂无分支，点击「＋ 创建」新建个人分支</div>';
    renderBranchKpis();
    return;
  }
  // 按类型分组：主开发 / 发布 / 个人·本地
  const groups = [
    {label:'主开发', icon:'🟦', items:[]},
    {label:'发布分支', icon:'🔒', items:[]},
    {label:'个人 / 本地', icon:'👤', items:[]},
  ];
  branches.forEach(b=>{
    const g = b.branch_type==='dev' ? groups[0] : (b.branch_type==='release' ? groups[1] : groups[2]);
    g.items.push(b);
  });
  el.innerHTML = groups.filter(g=>g.items.length).map(g=>`
    <div style="font-size:11px;color:var(--mut);font-weight:600;margin:6px 2px 6px;letter-spacing:.5px;">${g.icon} ${g.label} <span class="tag">${g.items.length}</span></div>
    ${g.items.map(b=>branchCardHtml(b, cur)).join('')}
  `).join('');
  renderBranchKpis();
}
// ── 分支卡片：第一行 名称+徽章+状态、操作列纵向收敛；第二行 描述/基线/元素（受保护分支只读降级）──
function branchCardHtml(b, cur) {
  const isProtected = PROTECTED_BRANCHES.includes(b.name);
  const isCur = b.name===cur;
  // P1-2 ahead/behind（对标 GitHub 分支列表）：dev 相对 release、个人分支相对 parent_branch；后端未升级时字段缺失自动隐藏
  const abN = (b.ahead||0) + (b.behind||0);
  const abBadge = abN>0
    ? `<span style="font-size:10.5px;padding:1px 7px;border-radius:8px;background:var(--amb-l,#fdf3e3);color:#a16207;" title="相对基线分支：领先 ${b.ahead||0} 次提交 / 落后 ${b.behind||0} 次提交（含合并指针计算）">↑${b.ahead||0} ↓${b.behind||0}</span>` : '';
  const note = b.branch_type==='release'
    ? '<span style="font-size:10.5px;color:var(--red);background:var(--red-l);padding:1px 7px;border-radius:8px;">🔒 仅通过 dev 合并更新（只读）</span>'
    : (b.name==='dev' ? '<span style="font-size:10.5px;color:var(--blue-d);background:var(--blue-l);padding:1px 7px;border-radius:8px;">✅ 主开发分支（唯一）</span>' : '');
  const ops = [];
  ops.push(`<button class="btn sm ghost" style="font-size:10.5px;padding:1px 6px;" onclick="openBranchDiffDrawer('${esc(b.name)}')" title="对比该分支与基线差异">🔍</button>`);
  ops.push(`<button class="btn sm ghost" style="font-size:10.5px;padding:1px 6px;" onclick="openBranchCommits('${esc(b.name)}','${esc(b.branch_type)}')" title="查看该分支提交历史">📜</button>`);
  if(!isProtected) {
    ops.push(`<button class="btn sm ghost" style="font-size:10.5px;padding:1px 6px;" onclick="editBranch('${esc(b.name)}')" title="编辑">✏️</button>`);
    ops.push(`<button class="btn sm red" style="font-size:10.5px;padding:1px 6px;" onclick="deleteBranch('${esc(b.name)}')" title="删除">🗑</button>`);
  }
  if(!isCur) ops.push(`<button class="btn sm" style="font-size:10.5px;padding:1px 8px;" onclick="setCurrentBranch('${esc(b.name)}')" title="设为工作分支：数据看板/图谱/文档/实体/建模检索均按该分支加载">↗ 设为工作分支</button>`);
  return `
    <div style="border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin-bottom:10px;background:#fff;${isCur?'border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-l);':''}${b.status==='archived'?'opacity:.62;':''}">
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="display:flex;align-items:center;gap:6px;flex:1;min-width:0;flex-wrap:wrap;">
          <b style="font-size:13px;white-space:nowrap;">${esc(b.name)}</b>
          ${branchTypeBadge(b.branch_type)}
          ${abBadge}
          ${isCur?'<span style="font-size:10.5px;color:var(--blue-d);background:var(--blue-l);padding:1px 7px;border-radius:8px;">✓ 当前</span>':''}
          ${b.status==='archived'?'<span style="font-size:10.5px;color:var(--mut);">已归档</span>':''}
        </div>
        <div style="display:flex;gap:4px;flex:none;">${ops.join('')}</div>
      </div>
      <div style="font-size:11.5px;color:var(--mut);margin-top:6px;line-height:1.6;">
        ${esc(b.description||'无描述')} ${note?`<span style="margin-left:4px;">${note}</span>`:''}
        <div style="margin-top:2px;">基线来源：<b>${esc(b.parent_branch||'-')}</b> ｜ <span style="color:var(--grn);">${b.entity_count||0} 元素</span></div>
      </div>
    </div>`;
}
// ── KPI 概览：分支数 / 当前分支元素 / 待评审 MR / 未解决冲突（数据来自 _branches/_mrs，双列表加载完成后填充）──
function renderBranchKpis() {
  const el = document.getElementById('branch-kpis');
  if(!el || !_branches || !_mrs) return;
  const active = _branches.filter(b=>b.status!=='archived');
  const curB = active.find(b=>b.name===getCurrentBranch());
  // P1-1 状态机：待评审 = open（draft 草稿不计入评审 KPI）；旧数据已由后端迁移，缺失字段兜底
  const pendingN = _mrs.filter(m=>(m.status||'open')==='open').length;
  const conflictN = _mrs.reduce((s,m)=>s+(m.unresolved_conflicts||0),0);
  document.getElementById('bk-branches').textContent = _branches.length + (active.length<_branches.length?`（归档 ${_branches.length-active.length}）`:'');
  const curName = getCurrentBranch()||'dev';
  document.getElementById('bk-elements').textContent = (curB?(curB.entity_count||0):0) + ' 元素 · ' + curName;
  const pEl = document.getElementById('bk-pending');
  pEl.textContent = pendingN; pEl.style.color = pendingN>0?'var(--red)':'';
  const cEl = document.getElementById('bk-conflicts');
  cEl.textContent = conflictN; cEl.style.color = conflictN>0?'var(--red)':'';
}
// ── kb-d 工作区：图谱 / 推理 / 历史 / 合并请求 Tab（2026-09-09 v2 扩展，2026-09-12 去三元组）──
let wsCurTab = 'content';
function wsTab(t) {
  wsCurTab = t;
  ['content','history','mr','reasoning'].forEach(k=>{
    const el = document.getElementById('ws-'+k);
    if(el) el.style.display = (k===t) ? '' : 'none';
  });
  document.querySelectorAll('#ws-tabs .t').forEach(el=>{
    const on = el.dataset.t===t;
    el.classList.toggle('on', on);
    el.style.color = on ? 'var(--blue-d)' : 'var(--mut)';
    el.style.fontWeight = on ? '700' : '400';
    el.style.borderBottom = on ? '2px solid var(--blue)' : '2px solid transparent';
  });
  if(t==='history') loadWsHistory();
  if(t==='mr') loadMerges();
  // 2026-09-14 推理恢复为独立 Tab（置于历史左侧）：首次切入懒初始化推理面板
  if(t==='reasoning' && typeof gwtReasoningInit==='function' && typeof gwtR!=='undefined' && gwtR && !gwtR.inited) gwtReasoningInit();
}
// 历史 Tab：当前分支提交时间线（行内展开快照明细，复用 toggleCommitDetail / 回滚逻辑）
// 2026-09-12 完善：历史时间线按日期分组 + 相对时间 + 变更内容摘要（实体/关系/文档）
function wsHistDayLabel(ts){
  const d = ts ? new Date((ts||'').replace(' ','T')) : null;
  if(!d || isNaN(d)) return '';
  const now = new Date();
  const y = new Date(now); y.setDate(now.getDate()-1);
  const same = x => d.getFullYear()===x.getFullYear() && d.getMonth()===x.getMonth() && d.getDate()===x.getDate();
  if(same(now)) return '今天';
  if(same(y)) return '昨天';
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function wsHistRelTime(ts){
  const t = ts ? new Date((ts||'').replace(' ','T')) : null;
  if(!t || isNaN(t)) return ts||'';
  const sec = Math.floor((Date.now()-t.getTime())/1000);
  if(sec<60) return '刚刚';
  if(sec<3600) return Math.floor(sec/60)+' 分钟前';
  if(sec<86400) return Math.floor(sec/3600)+' 小时前';
  return (t.getMonth()+1)+'月'+t.getDate()+'日';
}
function wsHistChangeSummary(c){
  const ch = (c&&c.changes)||{};
  const ents = Array.isArray(ch.entities)?ch.entities.length:0;
  const rels = Array.isArray(ch.relations)?ch.relations.length:0;
  const docs = Array.isArray(ch.documents)?ch.documents.length:0;
  // 单对象快照（manual 打点）changes.entities 可能为空，则以 snapshot.id 计 1 实体
  const snap = c&&c.snapshot;
  const ent = ents || (snap && !Array.isArray(snap.entity_snapshots) && (snap.id||snap.name) ? 1 : 0);
  const parts=[];
  if(ent) parts.push('🗂 实体 <b>'+ent+'</b>');
  if(rels) parts.push('🔗 关系 <b>'+rels+'</b>');
  if(docs) parts.push('📄 文档 <b>'+docs+'</b>');
  return parts.join(' &nbsp; ') || '<span style="opacity:.55;">（无字段级快照）</span>';
}
function wsHistKindIcon(k){
  return k==='merge'?'🔀':k==='import'?'📥':k==='rollback'?'↩':k==='manual'?'✏️':'📝';
}
function wsAuditSection(audit){
  // 2026-09-14 维度补全：纳入 entity_*（KB API 直写路径）与 reasoning_*（推理并入图库）——
  // 此前只显示 ontology_/graph_/relation_，"新增节点/关系/属性变更/推理并入"的其它入口变化不可见
  var A=(audit||[]).filter(function(x){ return /^(ontology|graph|relation|entity|reasoning)_/.test(x.event_type||''); });
  if(!A.length) return '';
  var groups=[];
  A.forEach(function(c){ var key=wsHistDayLabel(c.created_at)||'\u66f4\u65e9'; var last=groups[groups.length-1]; if(last&&last.key===key){last.items.push(c);} else {groups.push({key:key,items:[c]});} });
  var html='';
  groups.forEach(function(g){
    html+='<div style="display:flex;align-items:center;gap:8px;padding:5px 14px;background:#fafaf7;border-top:1px solid var(--line);"><span style="font-size:11.5px;font-weight:600;color:var(--blue-d);">'+g.key+'</span><span style="font-size:10.5px;color:var(--mut);">'+g.items.length+' \u6761</span><span style="flex:1;border-bottom:1px dashed var(--line);margin-left:4px;"></span></div>';
    g.items.forEach(function(c){
      var ki=wsAuditKindInfo(c.event_type);
      var kObj=wsAuditKindFromDetail(c.detail, ki.cls);
      var kc = kObj==='\u5b9e\u4f53' ? '#185F5A' : (kObj==='\u5173\u7cfb' ? '#7F77DD' : '#BA7517');
      var act=wsAuditActionOf(c.event_type);
      var pd=wsAuditParseDiff(c.detail);
      var diffRows=(pd.items||[]).map(wsAuditDiffRow).join('');
      html+='<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 14px;border-top:1px dashed var(--line);background:#fff;">'
        +'<span style="font-size:15px;flex:none;line-height:1.4;">'+ki.ic+'</span>'
        +'<div style="flex:1;min-width:0;">'
          +'<div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;"><b style="font-size:12.5px;">'+esc(c.user_name||'-')+'</b><span style="font-size:11px;color:var(--mut);">'+wsAuditLabel(c.event_type)+'</span><span class="st" style="font-size:10px;color:'+kc+';border-color:'+kc+';">'+esc(kObj)+'</span><span class="st" style="font-size:10px;color:'+act.c+';border-color:'+act.c+';">'+esc(act.t)+'</span></div>'
          +(pd.base?('<div style="margin-top:2px;font-size:11px;color:var(--mut);">'+esc(pd.base)+'</div>'):'')
          +(diffRows?('<div style="margin-top:4px;background:#f6f7f9;border:1px solid var(--line);border-radius:6px;padding:5px 8px;">'+diffRows+'</div>'):'')
        +'</div>'
        +'<span style="flex:none;font-size:10.5px;color:var(--mut);white-space:nowrap;" title="'+esc((c.created_at||'').slice(0,19))+'">'+wsHistRelTime(c.created_at)+'</span>'
      +'</div>';
    });
  });
  return '<div style="border-bottom:1px solid var(--line);background:#F4F7FB;">'
    +'<div style="display:flex;align-items:center;gap:8px;padding:9px 14px;font-size:12px;color:var(--blue-d);font-weight:600;"><span>\ud83d\udd55 \u64cd\u4f5c\u5ba1\u8ba1</span><span style="flex:1;"></span><span style="font-size:10.5px;color:var(--mut);font-weight:400;">\u6700\u8fd1 '+A.length+' \u6761</span></div>'
    +html+'</div>';
}
function wsAuditDimName(d){
  var x=d||'';
  if(x.indexOf('属性.')===0) return '属性 \u00b7 '+x.slice(3);
  if(x.indexOf('properties.')===0) return '属性 \u00b7 '+x.slice(11);
  if(x.indexOf('constraints.')===0) return '约束 \u00b7 '+x.slice(12);
  var m={'name':'\u540d\u79f0','type_kind':'\u7c7b\u578b','description':'\u63cf\u8ff0','icon':'\u56fe\u6807','color':'\u989c\u8272','status':'\u72b6\u6001','iri':'IRI','\u540d\u79f0':'\u540d\u79f0','\u7c7b\u578b':'\u7c7b\u578b'};
  return m[x]||x;
}
function wsAuditParseDiff(detail){
  var base='', items=[];
  if(!detail) return {base:'',items:[]};
  var parts=String(detail).split('\uff1b'), rests=[];
  for(var i=0;i<parts.length;i++){ if(parts[i].indexOf('\u2192')>=0) rests.push(parts[i]); else base=(base?base+'\uff1b':'')+parts[i]; }
  items=rests.map(function(p){
    var ci=p.indexOf(':'); if(ci<0) return null;
    var dim=p.slice(0,ci), arr=p.slice(ci+1).split('\u2192');
    var from=(arr.length&&arr[0]!=='undefined')?arr[0]:'';
    var to=(arr.length>1&&arr[1]!=='undefined')?arr[1]:'';
    var kind=(to==='\u2205'||to==='')?2:((from==='\u2205'||from==='')?1:0);
    return {dim:wsAuditDimName(dim), from:from, to:to, kind:kind};
  }).filter(function(x){return x;});
  return {base:base, items:items};
}
function wsAuditActionOf(et){
  if(/(add|create)$/.test(et)) return {t:'新增', c:'var(--grn)'};
  if(/delete/.test(et)) return {t:'删除', c:'var(--red)'};
  if(/reject$/.test(et)) return {t:'驳回', c:'var(--red)'};
  if(/(approve|merge)$/.test(et)) return {t:/direct_merge$/.test(et)?'并入':'合并', c:'#7F77DD'};
  if(/review$/.test(et)) return {t:'审核', c:'#BA7517'};
  return {t:'修改', c:'#B7791F'};
}
function wsAuditDiffRow(it){
  var act = it.kind===1?'\uff08\u65b0\u589e\uff09':(it.kind===2?'\uff08\u5220\u9664\uff09':'');
  var fromTxt = (it.kind===1)?'\u2205':it.from;
  var toTxt = (it.kind===2)?'\u2205':it.to;
  var fromCss = (it.kind===2)?'color:var(--red);text-decoration:line-through;':'color:var(--mut);text-decoration:line-through;';
  var toCss = (it.kind===1)?'color:var(--grn);font-weight:600;':(it.kind===2)?'color:var(--red);font-weight:600;':'color:var(--blue-d);font-weight:600;';
  return '<div style="display:flex;align-items:center;gap:8px;font-size:11px;padding:2px 0;line-height:1.6;">'
    +'<span style="width:104px;flex:none;text-align:right;padding-right:8px;color:var(--blue-d);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="'+esc(it.dim)+'">'+esc(it.dim)+'</span>'
    +'<span style="flex:none;min-width:30px;'+fromCss+'">'+esc(fromTxt)+'</span>'
    +'<span style="flex:none;color:var(--mut);">\u2192</span>'
    +'<span style="flex:none;'+toCss+'">'+esc(toTxt)+(act?'<span style="font-size:10px;color:'+(it.kind===1?'var(--grn)':'var(--red)')+';margin-left:4px;">'+act+'</span>':'')+'</span>'
    +'</div>';
}

function wsAuditKindInfo(et){
  if(et.indexOf('ontology_')===0) return {ic:'🧬', cls:'本体'};
  if(et.indexOf('graph_node_')===0 || et.indexOf('entity_')===0) return {ic:'🗂', cls:'实体'};
  if(et.indexOf('graph_edge_')===0 || et.indexOf('relation_')===0) return {ic:'🔗', cls:'关系'};
  if(et.indexOf('reasoning_')===0) return {ic:'🧠', cls:'推理'};
  return {ic:'🗖', cls:'操作'};
}
function wsAuditKindFromDetail(detail, cls){
  if(cls==='本体'){
    var m=/((\w+))/.exec(detail||'');
    var k=m&&m[1]||'';
    if(k==='entity') return '类';
    if(k==='attribute') return '数据属性';
    if(k==='relation') return '对象属性';
    return '本体类型';
  }
  return cls;
}
function wsAuditLabel(et){
  var m={'ontology_add':'新增类型','ontology_update':'更新类型','ontology_delete':'删除类型','ontology_delete_force':'强制删除类型','ontology_meta':'本体元信息','graph_node_add':'新增实体','graph_node_update':'更新实体','graph_node_delete':'删除实体','graph_edge_add':'新增关系','relation_review':'审核关系','relation_merge':'合并关系','entity_create':'创建实体','entity_update':'更新实体','entity_review':'审核实体','entity_category':'实体分类','commit_revert':'回滚提交','reasoning_run':'执行推理','reasoning_direct_merge':'推理并入','reasoning_cohort_approve':'推理并入（批次）','reasoning_cohort_item_approve':'推理并入（单条）','reasoning_cohort_reject':'推理驳回（批次）','reasoning_cohort_item_reject':'推理驳回（单条）'};
  return m[et]||et;
}

async function loadWsHistory() {
  const el = document.getElementById('ws-history');
  if(!el) return;
  const name = getCurrentBranch();
  const b = (_branches||[]).find(x=>x.name===name);
  const branchType = b ? b.branch_type : '';
  _bcCtx = {name, branchType, canMerge:false};
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const [hist, perms, auditLogs] = await Promise.all([
      api(`/api/branches/${encodeURIComponent(name)}/history`),
      (async()=>{ if(!_bcPerms) _bcPerms = (((await api('/api/users/me'))||{}).permissions)||{}; return _bcPerms; })(),
      api('/api/audit?limit=250&branch=' + encodeURIComponent(name)).then(x=>Array.isArray(x)?x:((x&&x.logs)||[])).catch(()=>[]),
    ]);
    _bcCtx.canMerge = ((_bcPerms['branch_release']||[]).includes('review_merge'));
    if(!hist || !hist.length) {
      el.innerHTML = '<div style="padding:16px;color:var(--mut);font-size:12px;">该分支暂无提交记录</div>';
      return;
    }
    // 按日期分组（今天/昨天/具体日期），组内 id DESC 已由后端保证
    const groups = [];
    hist.forEach(c=>{
      const key = wsHistDayLabel(c.created_at) || '更早';
      const last = groups[groups.length-1];
      if(last && last.key===key) last.items.push(c); else groups.push({key, items:[c]});
    });
    const rows = groups.map(g=>`
      <div style="padding:7px 14px 5px;background:#fafaf7;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px;">
        <b style="font-size:11.5px;color:var(--blue-d);">${esc(g.key)}</b>
        <span style="color:var(--mut);font-size:10.5px;">${g.items.length} 条提交</span>
        <span style="flex:1;border-bottom:1px dashed var(--line);"></span>
        <span style="color:var(--mut);font-size:10px;">${esc((g.items[0]&&g.items[0].created_at||'').slice(0,10))}</span>
      </div>`+
      g.items.map(c=>`
      <div style="border-bottom:1px solid var(--line);padding:9px 14px;background:#fff;">
        <div style="cursor:pointer;display:flex;gap:10px;align-items:flex-start;" onclick="toggleCommitDetail(${c.id}, this)">
          <span title="${esc(commitKindBadge(c.kind).replace(/<[^>]*>/g,''))}" style="font-size:15px;flex:none;line-height:1.3;">${wsHistKindIcon(c.kind)}</span>
          <div style="flex:1;min-width:0;">
            <div style="display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;">
              <span style="font-size:11px;">${commitKindBadge(c.kind)}</span>
              <b style="font-size:12.5px;">${esc(c.message||'')}</b>
            </div>
            <div style="color:var(--mut);font-size:11px;margin-top:4px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <span>${wsHistChangeSummary(c)}</span>
              <span style="opacity:.8;" title="${esc((c.created_at||'').slice(0,19))}">🕐 <b>${wsHistRelTime(c.created_at)}</b></span>
              <span title="提交人 / 提交 ID #${c.id}">👤 ${esc(c.created_by||'-')} <code style="font-size:9.5px;color:var(--mut);">#${c.id}</code></span>
            </div>
          </div>
          <span style="color:var(--mut);font-size:14px;flex:none;align-self:center;">▾</span>
        </div>
        <div id="cd-${c.id}" style="display:none;margin-top:6px;border-top:1px dashed var(--line);padding-top:6px;font-size:12px;"></div>
      </div>`).join('')
    ).join('');
    el.innerHTML = `<div style="font-size:11.5px;color:var(--mut);padding:8px 14px;border-bottom:1px solid var(--line);background:#fafaf7;display:flex;align-items:center;gap:8px;">
      📜 <b style="color:var(--blue-d);">${esc(name)}</b> 提交历史 · 共 ${hist.length} 条${branchType==='release'?' · 🔒 发布分支（提交可回滚）':''}
      <span style="flex:1;"></span>
      <span style="opacity:.8;">按日期分组 · 点击行展开变更清单</span></div>${wsAuditSection(auditLogs)}${rows}`;
  } catch(e) {
    el.innerHTML = `<div style="color:var(--red);padding:12px;font-size:12px;">加载失败：${esc(e.message)}</div>`;
  }
}
// GitHub 式分支下拉：搜索过滤 / 类型分组徽章 / 行内快捷操作（对比/历史）/ 底部新建分支
function toggleBrDD(ev) {
  if(ev) ev.stopPropagation();
  const dd = document.getElementById('ws-brDD');
  if(!dd) return;
  if(dd.style.display==='block') { dd.style.display='none'; return; }
  renderBrDropdown();       // 先用缓存立即渲染
  dd.style.display = 'block';
  loadBranches();           // 后台刷新分支列表（完成后 renderBrDropdown 自动重渲）
}
function hideBrDD() { const dd=document.getElementById('ws-brDD'); if(dd) dd.style.display='none'; }
function pickWsBranch(name) { hideBrDD(); onGlobalBranchChange(name); }
function wsBrApplyFilter() {
  const q = (document.getElementById('ws-br-filter')||{}).value || '';
  const kq = q.trim().toLowerCase();
  document.querySelectorAll('#ws-brList .ws-br-item').forEach(el=>{
    el.style.display = !kq || (el.dataset.nm||'').toLowerCase().includes(kq) ? '' : 'none';
  });
}
function renderBrDropdown() {
  const cur = getCurrentBranch()||'dev';
  const btn = document.getElementById('ws-branch-cur');
  if(btn) btn.textContent = cur;
  const list = document.getElementById('ws-brList');
  if(!list) return;
  const alive = (_branches||[]).filter(b=>b.status!=='archived');
  if(!alive.length) { list.innerHTML = '<div style="padding:10px;color:var(--mut);font-size:12px;">暂无分支</div>'; return; }
  const groups = [
    {label:'👤 个人 / 本地', items: alive.filter(b=>b.branch_type!=='dev' && b.branch_type!=='release')},
    {label:'🟦 开发', items: alive.filter(b=>b.branch_type==='dev')},
    {label:'🔒 发布', items: alive.filter(b=>b.branch_type==='release')},
  ];
  list.innerHTML = groups.filter(g=>g.items.length).map(g=>
    `<div style="font-size:10.5px;color:var(--mut);font-weight:600;padding:5px 12px 2px;letter-spacing:.5px;">${g.label} <span class="tag" style="font-size:9.5px;">${g.items.length}</span></div>` +
    g.items.map(b=>{
      const isCur = b.name===cur;
      const rel = b.branch_type==='release' || isReleaseBranch(b.name);
      const ec = b.entity_count||0;
      // 2026-09-12：内置分支标「🔒 系统内置」；自定义分支数字>0 时按钮/数字说明删除需先清空
      const isBuiltIn = typeof PROTECTED_BRANCHES!=='undefined' && PROTECTED_BRANCHES.includes(b.name);
      let badge = rel ? '<span class="st" style="font-size:9.5px;">🔒 只读</span>'
        : (b.parent_branch?`<span style="font-size:9.5px;color:var(--mut);">← 自 ${esc(b.parent_branch)}</span>`:'');
      if(isBuiltIn) badge += ' <span class="st" style="font-size:9.5px;">🔒 系统内置</span>';
      const delBtn = isBuiltIn ? ''
        : `<button class="btn sm red" style="font-size:10px;padding:0 5px;" onclick="event.stopPropagation();deleteBranch('${esc(b.name)}')" title="删除自定义分支（当前含 ${ec} 实体，需先清空或合并才能删）">🗑</button>`;
      const quick = `<span style="display:inline-flex;gap:2px;">
        <button class="btn sm ghost" style="font-size:10px;padding:0 5px;" onclick="event.stopPropagation();openBranchDiffDrawer('${esc(b.name)}')" title="对比该分支与基线差异">🔍</button>
        <button class="btn sm ghost" style="font-size:10px;padding:0 5px;" onclick="event.stopPropagation();openBranchCommits('${esc(b.name)}','${esc(b.branch_type)}')" title="查看该分支提交历史">📜</button>
        ${delBtn}
      </span>`;
      return `<div class="ws-br-item" data-nm="${esc(b.name)}" style="display:flex;align-items:center;gap:8px;padding:5px 12px;cursor:pointer;font-size:12px;" onclick="pickWsBranch('${esc(b.name)}')" title="${isBuiltIn?'系统内置分支，不可删除':'切换到该分支'};实体数=${ec}（未废弃实体计数）">
        <span style="width:12px;color:var(--blue);flex:none;">${isCur?'✓':''}</span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(b.name)} ${badge}</span>
        <span style="font-size:9.5px;color:${ec>0?'var(--amb,#a16207)':'var(--grn)'};flex:none;" title="${isBuiltIn?'系统内置不可删':'实体数（未废弃实体计数）'};删除需实体数为 0">${ec}</span>
        ${quick}
      </div>`;
    }).join('')
  ).join('');
}
// 点击空白处关闭分支下拉（一次性绑定）
if(!window._wsBrDDBound) {
  window._wsBrDDBound = true;
  document.addEventListener('click', e=>{ if(!e.target.closest('#ws-branch-ctl')) hideBrDD(); });
}
// ── 分支提交历史（时间线：kind 徽章 + message + 变更数 + 时间/操作人；点击展开变更清单；release 分支提交可回滚）──
let _bcCtx = {name:'', branchType:'', canMerge:false};   // 当前打开的提交时间线上下文（供详情回滚判断）
let _bcPerms = null;                                     // /api/users/me permissions 缓存
async function openBranchCommits(name, branchType) {
  _bcCtx = {name, branchType:branchType||'', canMerge:false};
  openPanel(`📜 提交历史：${name}`, '<div class="loading">加载中…</div>');
  try {
    const [hist, perms, auditLogs] = await Promise.all([
      api(`/api/branches/${encodeURIComponent(name)}/history`),
      (async ()=>{
        if(!_bcPerms) _bcPerms = (((await api('/api/users/me'))||{}).permissions)||{};
        return _bcPerms;
      })(),
    ]);
    _bcCtx.canMerge = ((_bcPerms['branch_release']||[]).includes('review_merge'));
    if(!hist || !hist.length) {
      openPanel(`📜 提交历史：${name}`, '<div style="color:var(--mut);font-size:12px;padding:10px;">该分支暂无提交记录</div>');
      return;
    }
    const rows = hist.map(c=>`
      <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;background:#fff;">
        <div style="cursor:pointer;" onclick="toggleCommitDetail(${c.id}, this)">
          ${commitKindBadge(c.kind)} <b>${esc(c.message||'')}</b>
          <span style="color:var(--mut);font-size:11px;">变更 ${c.changes_count||0} 项</span>
          <div style="color:var(--mut);font-size:11px;margin-top:2px;">${esc((c.created_at||'').slice(0,16))} · ${esc(c.created_by||'-')}</div>
        </div>
        <div id="cd-${c.id}" style="display:none;margin-top:6px;border-top:1px dashed var(--line);padding-top:6px;font-size:12px;"></div>
      </div>`).join('');
    const note = _bcCtx.branchType==='release'
      ? '🔒 发布分支（提交可回滚）'
      : `分支类型：${esc(branchType||'-')}`;
    openPanel(`📜 提交历史：${name}`,
      `<div style="font-size:11.5px;color:var(--mut);margin-bottom:6px;">${note} · 共 ${hist.length} 条提交</div>${rows}`);
  } catch(e) {
    openPanel(`📜 提交历史：${name}`, `<div style="color:var(--red);padding:10px;font-size:12px;">加载失败：${esc(e.message)}</div>`);
  }
}
async function toggleCommitDetail(cid, headEl) {
  const body = document.getElementById('cd-'+cid);
  if(!body) return;
  if(body.style.display !== 'none') { body.style.display = 'none'; return; }
  body.style.display = 'block';
  if(body.dataset.loaded) return;
  body.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const c = await api(`/api/knowledge/commits/${cid}`);
    if(c && c.error) { body.innerHTML = `<span style="color:var(--red);">加载失败：${esc(c.error)}</span>`; return; }
    const list = (o)=> Object.values(o||{}).map(v=>esc(String(v))).join('、');
    // GitHub changed files 式：数组快照明细（merge/import）→ 分组清单带字段级变化
    const snapItems = (c.entity_snapshots||[]).filter(x=>x && x.id);
    let html = '';
    if(snapItems.length) {
      const g = {added:[], modified:[], removed:[], unchanged:[]};
      snapItems.forEach(x=>{ (g[x.action]||g.unchanged).push(x); });
      const relType = {added:['🟢 新增','var(--grn)'], modified:['🟠 修改','var(--amb)'], removed:['🔴 移除','var(--red)'], unchanged:['○ 本提交写入','var(--blue-d)']};
      const stColor = s => s==='reviewed' ? 'var(--grn)' : s==='deprecated' ? 'var(--red)' : s==='raw_chunk' ? 'var(--mut)' : 'var(--amb)';
      html += '<div style="color:var(--blue-d);font-weight:600;">📄 变更清单</div>';
      ['added','modified','removed','unchanged'].forEach(k=>{
        if(!g[k].length) return;
        const [lab,col] = relType[k];
        html += `<div style="margin-top:6px;"><b style="color:${col};font-size:11.5px;">${lab}（${g[k].length}）</b></div>` +
          g[k].map(x=>`<div style="padding:2px 0 2px 12px;border-bottom:1px dashed var(--line);">
            <b>${esc(x.name||x.id)}</b> <span style="color:var(--mut);font-size:11px;">${esc(x.entity_type||'')}</span>
            ${x.status?`<span class="st" style="font-size:10px;color:${stColor(x.status)};">${esc(x.status)}</span>`:''}
            ${(x.changes||[]).map(ch=>`<br>&nbsp;&nbsp;<code style="font-size:10.5px;">${esc(ch.field)}</code> <span style="color:var(--mut);">${esc(String(ch.base_value))}</span> → <b>${esc(String(ch.head_value))}</b>`).join('')}
          </div>`).join('');
      });
    } else {
      const ens = list(c.entity_names), rels = list(c.relation_names), docs = list(c.document_names);
      html = '<div style="color:var(--blue-d);font-weight:600;">📄 变更清单</div>';
      if(ens) html += `<div style="color:var(--mut);margin-top:3px;">实体：${ens}</div>`;
      if(rels) html += `<div style="color:var(--mut);margin-top:3px;">关系：${rels}</div>`;
      if(docs) html += `<div style="color:var(--mut);margin-top:3px;">文档：${docs}</div>`;
      if(!ens && !rels && !docs) html += `<div style="color:var(--mut);margin-top:3px;">（无对象级变更）</div>`;
    }
    // 回滚：仅 release 分支提交 + branch_release:review_merge 权限
    if(_bcCtx && _bcCtx.branchType==='release' && _bcCtx.canMerge) {
      html += `<div style="margin-top:8px;"><button class="btn sm red" onclick="revertCommitFlow(${cid})">↩ 回滚</button></div>`;
    }
    body.innerHTML = html;
    body.dataset.loaded = '1';
  } catch(e) {
    body.innerHTML = `<span style="color:var(--red);">加载失败：${esc(e.message)}</span>`;
  }
}
async function revertCommitFlow(cid) {
  // ① 预览：取变更清单（不执行）
  const p = await api(`/api/knowledge/commits/${cid}/revert`, {method:'POST', body:JSON.stringify({preview:true})});
  if(p && p.error) { toast('回滚预览失败：' + p.error); return; }
  const pv = (p && p.preview) || {};
  const ents = pv.entities || [], rels = pv.relations || [];
  const cnt = (arr, act) => arr.filter(x=>x.action===act).length;
  if(!ents.length && !rels.length) { toast('该提交无可回滚的变更（可能已回滚）'); return; }
  const lines = [
    `将恢复 ${cnt(ents,'restore')} 实体 / 软删 ${cnt(ents,'soft_delete')} 实体 / 恢复 ${cnt(rels,'restore')} 关系 / 软删 ${cnt(rels,'soft_delete')} 关系`,
  ];
  if(ents.length) lines.push('实体：' + ents.map(x=>`${x.name}(${x.action==='restore'?'恢复':'软删'})`).join('、'));
  if(rels.length) lines.push('关系：' + rels.map(x=>`${x.source_id}→${x.target_id}(${x.action==='restore'?'恢复':'软删'})`).join('、'));
  lines.push('回滚仅作用于该提交所在发布分支，执行后生成 rollback 提交，不可撤销。');
  if(!(await confirmDialog(lines.join('\n'), {title:'↩ 回滚预览', okText:'确认回滚'}))) return;
  // ② 执行
  const r = await api(`/api/knowledge/commits/${cid}/revert`, {method:'POST', body:JSON.stringify({preview:false})});
  if(r && r.error) { toast('回滚失败：' + r.error); return; }
  toast(`回滚成功：还原 ${(r.reverted||{}).entities||0} 实体 / ${(r.reverted||{}).relations||0} 关系`);
  // ③ 刷新时间线（沿用当前上下文）
  if(_bcCtx && _bcCtx.name) openBranchCommits(_bcCtx.name, _bcCtx.branchType);
  loadGraph();
}
// ── 分支对比（diff 大抽屉：差异统计卡片 + 类型/实体类型/关键词筛选，只读无副作用）──
let bdData = null;                                  // 最近一次 diff 结果（供筛选）
const bdFilter = {cat:'all', type:'', kw:''};       // 筛选状态：cat=all|added|modified|removed|rel
let bdMode = 'full';                                // P1-2 diff 模式：full 双点式 | merge-base 三点式（只看上次合并后的增量）

function openBranchDiffDrawer(headName) {
  document.getElementById('diff-mask').classList.add('show');
  document.getElementById('diff-drawer').classList.add('show');
  bdData = null;
  bdFilter.cat='all'; bdFilter.type=''; bdFilter.kw='';
  bdMode = 'full';
  if(typeof bdTab === 'function') bdTab('list');   // 重新打开回到清单视图
  initBranchDiff().then(()=>{
    const h = document.getElementById('bd-head');
    if(headName && h && [...h.options].some(o=>o.value===headName)) h.value = headName;
    loadBranchDiff();
  });
}
function closeBranchDiffDrawer() {
  document.getElementById('diff-mask').classList.remove('show');
  document.getElementById('diff-drawer').classList.remove('show');
}
async function initBranchDiff() {
  const branches = await api('/api/branches');
  const active = branches.filter(b=>b.status!=='archived');
  const opts = active.map(b=>`<option value="${esc(b.name)}">${esc(b.name)}（${b.branch_type}）</option>`).join('');
  const baseEl = document.getElementById('bd-base'), headEl = document.getElementById('bd-head');
  if(!baseEl || !headEl) return;
  baseEl.innerHTML = opts; headEl.innerHTML = opts;
  // 默认：base=最新 release 分支（发布基线），head=当前工作分支
  const rels = active.filter(b=>b.branch_type==='release');
  if(rels.length) baseEl.value = rels[rels.length-1].name;
  const cur = getCurrentBranch();
  headEl.value = active.some(b=>b.name===cur) ? cur : (active[0] ? active[0].name : '');
}
async function loadBranchDiff() {
  const sumEl = document.getElementById('bd-summary');
  const fltEl = document.getElementById('bd-filter');
  const bodyEl = document.getElementById('bd-body');
  if(!sumEl || !bodyEl) return;
  const base = document.getElementById('bd-base').value;
  const head = document.getElementById('bd-head').value;
  if(!base || !head) { sumEl.innerHTML = ''; fltEl.innerHTML=''; bodyEl.innerHTML = '<div style="padding:10px;color:var(--mut);">请选择两个分支</div>'; return; }
  if(base === head) { sumEl.innerHTML = '<b style="color:var(--red);">基线分支与对比分支不能相同</b>'; fltEl.innerHTML=''; bodyEl.innerHTML = ''; return; }
  bodyEl.innerHTML = '<div class="loading">对比中…</div>';
  const r = await api(`/api/branches/diff?base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}&mode=${encodeURIComponent(bdMode)}`);
  if(r && r.error) { sumEl.innerHTML = `<b style="color:var(--red);">${esc(r.error)}</b>`; fltEl.innerHTML=''; bodyEl.innerHTML = ''; return; }
  bdData = r;
  renderBranchDiff();
  if(typeof _bdTabCur !== 'undefined' && _bdTabCur==='graph') renderDiffOverlay('bd', bdData, []);   // 图谱 Tab 已打开时重渲染
}
// P1-2：diff 模式切换（full 双点式全量对比 | merge-base 三点式只看上次合并后源分支增量）
function setBdMode(k) { bdMode = k; loadBranchDiff(); }
function renderBranchDiff() {
  const r = bdData; if(!r) return;
  const s = r.summary||{};
  const cards = [
    {n:s.ent_added||0,    l:'🟢 新增实体', c:'var(--grn)'},
    {n:s.ent_modified||0, l:'🟠 修改实体', c:'var(--amb)'},
    {n:s.ent_removed||0,  l:'🔴 移除实体', c:'var(--red)'},
    {n:s.rel_added||0,    l:'🔗 新增关系', c:'var(--blue-d)'},
    {n:s.rel_modified||0, l:'✎ 修改关系', c:'var(--amb)'},
    {n:s.rel_removed||0,  l:'🔗 移除关系', c:'var(--blue-d)'},
  ];
  const modeChip = (k,l,tip)=> `<span class="st ${bdMode===k?'on':''}" style="cursor:pointer;font-size:10.5px;${bdMode===k?'background:var(--blue-l);color:var(--blue-d);font-weight:600;':''}" title="${tip}" onclick="setBdMode('${k}')">${l}</span>`;
  document.getElementById('bd-summary').innerHTML = `
    <div style="font-size:12px;color:var(--mut);margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <span>基线 <b>${esc(r.base)}</b> → 对比 <b>${esc(r.head)}</b></span>
      <span style="flex:1"></span>
      <span style="font-size:10.5px;">对比模式：</span>
      ${modeChip('full','全量（双点式）','对比两分支当前全量差异（git diff branch1..branch2）')}
      ${modeChip('merge-base','增量（三点式）','只看对比分支自上次合并基线以来的增量变更（git diff branch1...branch2），后端未升级时自动回退全量')}
    </div>
    <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px;">${cards.map(c=>`
      <div class="bd-stat-card"><div class="n" style="color:${c.c};">${c.n}</div><div class="l">${c.l}</div></div>`).join('')}
    </div>`;
  renderBdFilter();
  renderBdList();
}
function _bdCounts() {
  const r = bdData, E=r?r.entities:{}, R=r?r.relations:{};
  return {added:(E.added||[]).length, modified:(E.modified||[]).length, removed:(E.removed||[]).length,
          rel:(R.added||[]).length+(R.removed||[]).length+(R.modified||[]).length};
}
function renderBdFilter() {
  const c = _bdCounts();
  const cats = [
    {k:'all', l:`全部 ${c.added+c.modified+c.removed+c.rel}`},
    {k:'added', l:`🟢 新增 ${c.added}`},
    {k:'modified', l:`🟠 修改 ${c.modified}`},
    {k:'removed', l:`🔴 移除 ${c.removed}`},
    {k:'rel', l:`🔗 关系 ${c.rel}`},
  ];
  // 实体类型汇总（新增/修改/移除）
  const r = bdData; const types={};
  ((r?r.entities:{}).added||[]).concat((r?r.entities:{}).modified||[], (r?r.entities:{}).removed||[])
    .forEach(e=>{ if(e.entity_type) types[e.entity_type]=1; });
  const typeOpts = '<option value="">全部实体类型</option>' + Object.keys(types).sort()
    .map(t=>`<option value="${esc(t)}" ${bdFilter.type===t?'selected':''}>${esc(t)}</option>`).join('');
  document.getElementById('bd-filter').innerHTML = `
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:10px 0;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#fafaf7;">
      ${cats.map(x=>`<span class="st ${bdFilter.cat===x.k?'on':''}" style="cursor:pointer;${bdFilter.cat===x.k?'background:var(--blue-l);color:var(--blue-d);':''}" onclick="setBdCat('${x.k}')">${x.l}</span>`).join('')}
      <span style="flex:1"></span>
      <select id="bd-type-filter" onchange="setBdType(this.value)" style="border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:12px;">${typeOpts}</select>
      <input id="bd-kw" placeholder="搜索名称/ID/字段/关系…" value="${esc(bdFilter.kw)}" style="border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:12px;width:170px;" onkeydown="if(event.key==='Enter'){bdFilter.kw=this.value.trim();renderBdList();}">
    </div>`;
}
function setBdCat(k){ bdFilter.cat=k; renderBdFilter(); renderBdList(); }
function setBdType(v){ bdFilter.type=v; renderBdList(); }
// 风险分级：冲突(100) > 置废弃(50) > 字段变更数（清单排序用，风险高的置顶）
function _riskScore(e) {
  let s = 0;
  const chs = e.changes||[];
  if(chs.some(c=>c.field==='status' && String(c.head_value).includes('deprecated'))) s += 50;
  s += Math.min(chs.length, 10);
  return s;
}
function _riskTag(e, conflictIds) {
  if(conflictIds && conflictIds.includes(e.id)) return '<span class="st r">⚠ 冲突</span>';
  const chs = e.changes||[];
  if(chs.some(c=>c.field==='status' && String(c.head_value).includes('deprecated'))) return '<span class="st r">⬇ 置废弃</span>';
  return '';
}
const _dogLocateBtn = (pane, id)=>`<span style="cursor:pointer;float:right;margin-left:8px;" title="在叠加图谱中定位" onclick="dogLocate('${pane}','${encodeURIComponent(id)}')">📍</span>`;
function _bdMatchKw(e, kws) {  if(!kws.length) return true;
  const p = e.source_doc||''+' ';  // provenance 可搜索：来源文档/来源类型/创建人
  const hay = (e.name||'')+' '+(e.id||'')+' '+(e.entity_type||'')+' '+(e.relation_type||'')+' '+
    (e.source_id||'')+' '+(e.target_id||'')+' '+p+' '+(e.source_type||'')+' '+(e.created_by||'')+' '+
    (e.changes||[]).map(ch=>ch.field+' '+String(ch.base_value)+' '+String(ch.head_value)).join(' ');
  return kws.every(k=>hay.toLowerCase().includes(k));
}
// provenance 徽章行：来源文档/来源类型/创建人/置信度（diff 载荷可追溯）
function _bdProvBadge(e) {
  const bits = [];
  if(e.source_doc) bits.push(`📄 ${esc(e.source_doc)}`);
  if(e.source_type) bits.push(`🏷 ${esc(e.source_type)}`);
  if(e.created_by) bits.push(`👤 ${esc(e.created_by)}`);
  if(e.confidence !== undefined && e.confidence !== null && e.confidence !== '') bits.push(`📈 置信度 ${esc(String(e.confidence))}`);
  return bits.length ? `<br>&nbsp;&nbsp;<span style="font-size:11px;color:var(--mut);">${bits.join(' · ')}</span>` : '';
}
function _bdTypeOk(e) {
  return !bdFilter.type || (e.entity_type||'')===bdFilter.type;
}
function renderBdList() {
  const r = bdData;
  const bodyEl = document.getElementById('bd-body');
  if(!r) { bodyEl.innerHTML=''; return; }
  const E = r.entities||{}, R = r.relations||{};
  const kws = bdFilter.kw.toLowerCase().split(/\s+/).filter(Boolean);
  let html = '';
  const showCat = (k)=> bdFilter.cat==='all' || bdFilter.cat===k;
  if(showCat('added')) {
    const items=(E.added||[]).filter(e=>_bdTypeOk(e)&&_bdMatchKw(e,kws));
    if(items.length) html += `<div style="margin-top:6px;"><b style="color:var(--grn);">🟢 新增实体（${items.length}）</b></div>` +
      items.map(e=>`<div style="padding:4px 12px;border-bottom:1px dashed var(--line);">＋ <b>${esc(e.name)}</b> <code>${esc(e.id)}</code> <span style="color:var(--mut);">(${esc(e.entity_type)||'-'})</span>${_bdProvBadge(e)}${_dogLocateBtn('bd', e.id)}</div>`).join('');
  }
  if(showCat('modified')) {
    const items=(E.modified||[]).filter(e=>_bdTypeOk(e)&&_bdMatchKw(e,kws)).sort((a,b)=>_riskScore(b)-_riskScore(a));
    if(items.length) html += `<div style="margin-top:6px;"><b style="color:var(--amb);">🟠 修改实体（${items.length}）</b><span style="font-size:11px;color:var(--mut);margin-left:6px;">风险高的在前</span></div>` +
      items.map(e=>`<div style="padding:4px 12px;border-bottom:1px dashed var(--line);">✎ <b>${esc(e.name)}</b> <code>${esc(e.id)}</code> <span style="color:var(--mut);">(${esc(e.entity_type)||'-'})</span> ${_riskTag(e, [])}${_bdProvBadge(e)}${_dogLocateBtn('bd', e.id)}
        ${(e.changes||[]).map(ch=>`<br>&nbsp;&nbsp;<code>${esc(ch.field)}</code>: <span style="color:var(--mut);">${esc(String(ch.base_value))}</span> → <b>${esc(String(ch.head_value))}</b>`).join('')}</div>`).join('');
  }
  if(showCat('removed')) {
    const items=(E.removed||[]).filter(e=>_bdTypeOk(e)&&_bdMatchKw(e,kws));
    if(items.length) html += `<div style="margin-top:6px;"><b style="color:var(--red);">🔴 移除实体（${items.length}）</b></div>` +
      items.map(e=>`<div style="padding:4px 12px;border-bottom:1px dashed var(--line);">－ <b>${esc(e.name)}</b> <code>${esc(e.id)}</code> <span style="color:var(--mut);">(${esc(e.entity_type)||'-'})</span>${_bdProvBadge(e)}${_dogLocateBtn('bd', e.id)}</div>`).join('');
  }
  if(showCat('rel')) {
    const added=(R.added||[]).filter(x=>_bdMatchKw(x,kws));
    if(added.length) html += `<div style="margin-top:6px;"><b style="color:var(--blue-d);">🔗 新增关系（${added.length}）</b></div>` +
      added.map(x=>`<div style="padding:4px 12px;border-bottom:1px dashed var(--line);">＋ <code>${esc(x.source_id)}</code> -${esc(x.relation_type)}→ <code>${esc(x.target_id)}</code></div>`).join('');
    const removed=(R.removed||[]).filter(x=>_bdMatchKw(x,kws));
    if(removed.length) html += `<div style="margin-top:6px;"><b style="color:var(--blue-d);">🔗 移除关系（${removed.length}）</b></div>` +
      removed.map(x=>`<div style="padding:4px 12px;border-bottom:1px dashed var(--line);">－ <code>${esc(x.source_id)}</code> -${esc(x.relation_type)}→ <code>${esc(x.target_id)}</code></div>`).join('');
    const relmod=(R.modified||[]).filter(x=>_bdMatchKw(x,kws)).sort((a,b)=>(b.changes||[]).length-(a.changes||[]).length);
    if(relmod.length) html += `<div style="margin-top:6px;"><b style="color:var(--amb);">✎ 修改关系（${relmod.length}）</b></div>` +
      relmod.map(x=>`<div style="padding:4px 12px;border-bottom:1px dashed var(--line);">✎ <code>${esc(x.source_id)}</code> -${esc(x.relation_type)}→ <code>${esc(x.target_id)}</code>
        ${(x.changes||[]).map(ch=>`<br>&nbsp;&nbsp;<code>${esc(ch.field)}</code>: <span style="color:var(--mut);">${esc(String(ch.base_value))}</span> → <b>${esc(String(ch.head_value))}</b>`).join('')}</div>`).join('');
  }
  const total = _bdCounts();
  const hasAny = total.added+total.modified+total.removed+total.rel > 0;
  bodyEl.innerHTML = html || `<div style="padding:16px;color:var(--mut);text-align:center;">${hasAny ? '无匹配筛选条件的差异项' : '两个分支无差异'}</div>`;
}
// ── 合并请求（页面列表）：状态筛选 chips + 中文状态徽章 + 时间列 + 操作按状态收敛（待评审置顶）
// 注：命名加 Branch 前缀，避免与「合并对比分析」抽屉的 renderMergeFilter/renderMergeList（md* 系列）同名冲突
async function loadMerges() {
  // 2026-09-14 分支视角：只显示与当前分支相关（作为源或目标）的合并请求
  const br = (typeof getCurrentBranch==='function') ? getCurrentBranch() : '';
  _mrs = await api('/api/branches/merge-requests' + (br ? ('?branch=' + encodeURIComponent(br)) : ''));
  renderBranchMergeList();
}
function renderBranchMergeList() {
  const el = document.getElementById('merge-list');
  if(!_mrs || !_mrs.length) {
    el.innerHTML = '<div style="padding:10px;color:var(--mut);font-size:12px;">当前分支暂无合并请求</div>';
    renderBranchKpis();
    return;
  }
  el.innerHTML = renderBranchMergeFilter() + renderBranchMergeTable();
  renderBranchKpis();
}
function setMergeFilter(f) { mergeFilter = f; renderBranchMergeList(); }
// P1-1 状态机（对标 GitHub PR）：draft → open → merged / closed（closed 可 reopen）
// 排序：待评审(open)置顶 → 草稿 → 已合并 → 已关闭；旧状态值（pending/approved/rejected）已由后端迁移，缺省兜底 open
function _mergeStatusOrd(s){ return ({open:0, draft:1, merged:2, closed:3})[s||'open'] ?? 4; }
// 中文徽章映射（merged 用紫色，对标 GitHub PR merged 徽章）
function _mrStatusInfo(s){ return ({draft:['📝 草稿','w'], open:['🕐 待评审','b'], merged:['🟪 已合并','ok'], closed:['❌ 已关闭','r']})[s||'open'] || [s||'-','g']; }
function renderBranchMergeFilter() {
  const counts = {
    all:_mrs.length,
    open:_mrs.filter(m=>(m.status||'open')==='open').length,
    draft:_mrs.filter(m=>m.status==='draft').length,
    merged:_mrs.filter(m=>m.status==='merged').length,
    closed:_mrs.filter(m=>m.status==='closed').length,
  };
  const chips = [
    {k:'all', l:`全部 ${counts.all}`},
    {k:'open', l:`🕐 待评审 ${counts.open}`},
    {k:'draft', l:`📝 草稿 ${counts.draft}`},
    {k:'merged', l:`🟪 已合并 ${counts.merged}`},
    {k:'closed', l:`❌ 已关闭 ${counts.closed}`},
  ];
  return `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid var(--line);background:#fafaf7;">
    ${chips.map(x=>`<span class="st ${mergeFilter===x.k?'on':''}" style="cursor:pointer;padding:2px 10px;border-radius:10px;${mergeFilter===x.k?'background:var(--blue-l);color:var(--blue-d);font-weight:600;':''}" onclick="setMergeFilter('${x.k}')">${x.l}</span>`).join('')}
    <span style="flex:1"></span>
    <span style="font-size:11px;color:var(--mut);">待评审需冲突解决 + 人工评审后方可发布</span>
  </div>`;
}
function renderBranchMergeTable() {
  const rows = _mrs.filter(m=>mergeFilter==='all'||m.status===mergeFilter)
    .slice().sort((a,b)=> (_mergeStatusOrd(a.status)-_mergeStatusOrd(b.status)) || String(b.created_at||'').localeCompare(String(a.created_at||'')));
  return `<table class="t">
    <tr><th>源 → 目标</th><th>状态</th><th>冲突</th><th>创建时间</th><th>合并结果</th><th>操作</th></tr>` +
    rows.map(m=>{
      let detail = '';
      try { const d = JSON.parse(m.merge_detail||'{}');
        const verb = m.target_branch==='release' ? '复制' : '迁入';   // 发布=复制快照；个人合并回 dev=迁入
        if(d.moved||d.updated||d.doc_snapshot) detail = `${verb} ${d.moved||0} / 更新 ${d.updated||0}${d.doc_snapshot?' / 文档快照 '+d.doc_snapshot:''}`;
      } catch(e){}
      let conflicts = [];
      try { conflicts = JSON.parse(m.conflicts||'[]'); } catch(e){}
      const unresolved = m.unresolved_conflicts || 0;
      const conflictBadge = conflicts.length
        ? `<span class="${unresolved>0?'st r':'st ok'}" style="cursor:pointer;" title="点击打开合并对比分析" onclick="openMergeDiffDrawer(${m.id})">${conflicts.length-unresolved}/${conflicts.length} 已解决</span>`
        : '<span class="st ok">无</span>';
      const st = _mrStatusInfo(m.status);
      const isMerged = m.status==='merged';
      const isOpen = (m.status||'open')==='open';
      const isDraft = m.status==='draft';
      const isClosed = m.status==='closed';
      const approveDisabled = isOpen && unresolved>0 ? 'disabled title="还有冲突未解决"' : '';
      const approveNote = isOpen && unresolved>0 ? `<div style="font-size:10.5px;color:var(--red);margin-top:3px;">⚠ ${unresolved} 处冲突未解决，先解决再通过</div>` : '';
      // P1-1 驳回意见（closed 时展示，只读追溯）
      const noteHtml = (m.status==='closed' && m.review_note)
        ? `<div style="font-size:10.5px;color:var(--mut);margin-top:3px;max-width:180px;" title="${esc(m.review_note)}">💬 ${esc(String(m.review_note).slice(0,26))}${String(m.review_note).length>26?'…':''}</div>` : '';
      // 草稿标记（draft 且有标题时展示标题）
      const titleHtml = m.title ? `<div style="font-size:10.5px;color:var(--mut);margin-top:2px;">${esc(m.title)}</div>` : '';
      return `<tr>
      <td><b>${m.source_branch}</b> → ${m.target_branch}${titleHtml}</td>
      <td><span class="st ${st[1]}">${st[0]}</span>${noteHtml}</td>
      <td>${conflictBadge}${approveNote}</td>
      <td style="font-size:11px;color:var(--mut);white-space:nowrap;">${esc((m.created_at||'').slice(0,16))}${m.created_by?`<br>${esc(m.created_by)}`:''}</td>
      <td style="font-size:11px;color:var(--mut);">${detail||'-'}</td>
      <td style="white-space:nowrap;">
      <button class="btn sm ghost" onclick="openMergeDiffDrawer(${m.id})">🔍 对比分析</button>
      <button class="btn sm ghost" onclick="toggleMrComments(${m.id}, this)" title="评审意见时间线">💬</button>
      ${!isMerged?`<button class="btn sm ghost" onclick="previewMerge(${m.id})" title="合并结果预览（不落库）">🔮</button>`:''}
      ${isOpen?`<button class="btn sm grn" ${approveDisabled} onclick="resolveMerge(${m.id},'approve')">通过</button> <button class="btn sm red" onclick="resolveMerge(${m.id},'reject')">驳回</button>`:''}
      ${isDraft?`<button class="btn sm" onclick="openMrForReview(${m.id})" title="草稿转正式评审（draft → open）">📨 转评审</button> <button class="btn sm red" onclick="resolveMerge(${m.id},'reject')">关闭</button>`:''}
      ${isClosed?`<button class="btn sm" onclick="reopenMr(${m.id})" title="重新打开（closed → open）">↩ 重新打开</button>`:''}
      ${isMerged && m.target_branch==='release'?` <button class="btn sm red" onclick="rollbackMergeRequest(${m.id})" title="release 还原到该合并前快照">↩ 回滚</button>`:''}
      ${!isMerged?` <button class="btn sm ghost" onclick="deleteMergeRequest(${m.id})">🗑</button>`:''}</td>
    </tr>`;
    }).join('') + '</table>';
}
function renderConflictItem(mrId, c) {
  // P0-2：每条冲突含 conflict_type（property 属性冲突 | delete_modify 删除vs修改冲突），缺省兜底 property（后端未升级时降级兼容）
  const ctype = c.conflict_type || 'property';
  const typeBadge = ctype==='delete_modify'
    ? '<span class="st r" style="font-size:9.5px;" title="删除 vs 修改冲突：一方已软删该实体，另一方仍有修改">🗑 删除/修改</span>'
    : '<span class="st w" style="font-size:9.5px;">属性</span>';
  if(ctype === 'delete_modify') {
    // 删除/修改冲突：二选一（keep_delete 目标分支一并下线该实体 | keep_modify 目标分支保留该实体）
    const radioName = `rc-${mrId}-${c.entity_id}-del`.replace(/[^\w-]/g,'_');
    const checked = (v)=> (c.pick===v ? 'checked' : '');
    const srcDesc = c.source_value==='deprecated' ? '源分支已删除该实体' : '目标分支已删除该实体';
    const otherDesc = c.source_value==='deprecated' ? '目标分支仍有修改' : '源分支仍有修改';
    return `<div style="border:1px solid var(--red-l);border-radius:8px;padding:8px 10px;margin:5px 0;background:#fff;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
      <b>${esc(c.entity_name||c.entity_id)}</b> ${typeBadge}
      <span style="flex:1"></span>
      ${c.resolved?`<span class="st ok">✓ 已解决${c.pick==='keep_delete'?'（保留删除）':'（保留修改）'}</span>`:'<span class="st r">⚠ 未解决</span>'}
    </div>
    <div style="font-size:11.5px;color:var(--mut);margin-bottom:6px;">⚠ ${esc(srcDesc)}，${esc(otherDesc)} —— 需明确取舍后才能通过合并</div>
    <div style="display:flex;align-items:center;gap:12px;font-size:12px;flex-wrap:wrap;">
      <label style="display:flex;gap:4px;align-items:center;"><input type="radio" name="${radioName}" value="keep_delete" ${checked('keep_delete')}> 保留删除（目标分支一并下线）</label>
      <label style="display:flex;gap:4px;align-items:center;"><input type="radio" name="${radioName}" value="keep_modify" ${checked('keep_modify')}> 保留修改（目标分支保留该实体）</label>
      <button class="btn sm" onclick="resolveConflict(${mrId}, '${esc(c.entity_id)}', '${esc(c.field)}', '${radioName}')">💾 保存</button>
    </div>
  </div>`;
  }
  // 属性冲突：源/目标值对比 + 三选一解决（source|target|manual）
  const radioName = `rc-${mrId}-${c.entity_id}-${c.field}`.replace(/[^\w-]/g,'_');
  const manualHidden = c.pick==='manual' ? '' : 'style="display:none;"';
  const pickMap = {source:0, target:1, manual:2};
  const checked = (i)=> (pickMap[c.pick]===i ? 'checked' : '');
  return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:5px 0;background:#fff;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
      <b>${esc(c.entity_name||c.entity_id)}</b> 的字段 <code>${esc(c.field)}</code> ${typeBadge}
      <span style="flex:1"></span>
      ${c.resolved?`<span class="st ok">✓ 已解决${c.pick==='manual'?'（手动:'+esc(String(c.value||''))+'）':c.pick==='source'?'（以源为准）':'（以目标为准）'}</span>`:'<span class="st r">⚠ 未解决</span>'}
    </div>
    <div style="font-size:11.5px;color:var(--mut);margin-bottom:6px;">
      源分支 <code>${esc(String(c.source_value))}</code> &nbsp;≠&nbsp; 目标分支 <code>${esc(String(c.target_value))}</code>
    </div>
    <div style="display:flex;align-items:center;gap:12px;font-size:12px;flex-wrap:wrap;">
      <label style="display:flex;gap:4px;align-items:center;"><input type="radio" name="${radioName}" value="source" ${checked(0)}> 以源分支为准</label>
      <label style="display:flex;gap:4px;align-items:center;"><input type="radio" name="${radioName}" value="target" ${checked(1)}> 以目标分支为准</label>
      <label style="display:flex;gap:4px;align-items:center;"><input type="radio" name="${radioName}" value="manual" ${checked(2)} onchange="manualVisible('${radioName}')"> 自定义</label>
      <input id="${radioName}-manual" placeholder="手动值" value="${esc(c.pick==='manual'?String(c.value||''):'')}" ${manualHidden} style="width:150px;border:1px solid var(--line);border-radius:5px;padding:3px 7px;font-size:11.5px;">
      <button class="btn sm" onclick="resolveConflict(${mrId}, '${esc(c.entity_id)}', '${esc(c.field)}', '${radioName}')">💾 保存</button>
    </div>
  </div>`;
}
function manualVisible(radioName) {
  const m = document.getElementById(radioName + '-manual');
  if(m) m.style.display = 'block';
}
async function resolveConflict(mrId, entityId, field, radioName) {
  const pick = document.querySelector(`input[name="${radioName}"]:checked`);
  if(!pick) { toast('请先选择解决方式'); return; }
  const value = pick.value==='manual' ? (document.getElementById(radioName+'-manual').value || '') : '';
  const r = await api(`/api/branches/merge-requests/${mrId}/resolve-conflict`,
    {method:'POST', body:JSON.stringify({entity_id:entityId, field, pick:pick.value, value})});
  if(r && r.error) { toast('保存失败：' + r.error); return; }
  toast('冲突已解决 ✓');
  if(mrgMrId === mrId) loadMergeConflicts();   // 对比分析抽屉内保存：同步刷新冲突状态
  loadMerges();
}
// ── MR 评审意见时间线（行内展开，对标 GitHub PR conversation）──
function _mrActionIcon(a) {
  return ({approve:'✅ 通过', reject:'❌ 驳回', comment:'💬 评论', rollback:'↩ 回滚',
           reopen:'↪ 重新打开', open:'📨 转评审'})[a] || '💬 ' + (a||'comment');
}
async function toggleMrComments(mrId, btn) {
  const row = btn.closest('tr');
  // 已展开则收起（展开行带 data-mr-id 标记，紧跟在该行之后）
  const next = row.nextElementSibling;
  if(next && next.dataset && next.dataset.mrExpand === String(mrId)) { next.remove(); return; }
  const comments = await api(`/api/branches/merge-requests/${mrId}/comments`);
  if(comments && comments.error) { toast('加载失败：' + comments.error); return; }
  const items = Array.isArray(comments) ? comments : [];
  const tl = items.length ? items.map(c=>{
    const ic = _mrActionIcon(c.action);
    const color = c.action==='approve' ? 'var(--grn)' : c.action==='reject' ? 'var(--red)' :
                  c.action==='rollback' ? 'var(--red)' : 'var(--mut)';
    return `<div style="display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px dashed var(--line);">
      <span style="font-size:11px;font-weight:600;color:${color};white-space:nowrap;">${ic}</span>
      <div style="flex:1;font-size:12px;">${esc(c.comment||'')}
        <div style="font-size:10.5px;color:var(--mut);margin-top:2px;">${esc(c.author||'-')} · ${esc(String(c.created_at||'').slice(0,16))}</div>
      </div></div>`;
  }).join('') : '<div style="padding:8px 0;font-size:12px;color:var(--mut);">暂无评审记录</div>';
  const exp = document.createElement('tr');
  exp.dataset.mrExpand = String(mrId);
  exp.innerHTML = `<td colspan="6" style="background:#fafaf7;padding:10px 14px;border-bottom:1px solid var(--line);">
    <div style="font-size:11.5px;font-weight:600;margin-bottom:4px;">💬 评审时间线（${items.length}）</div>
    ${tl}
    <div style="display:flex;gap:6px;margin-top:8px;">
      <input id="mr-cmt-${mrId}" placeholder="追加评审意见…" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"
        onkeydown="if(event.key==='Enter'){submitMrComment(${mrId});}">
      <button class="btn sm" onclick="submitMrComment(${mrId})">发送</button>
    </div></td>`;
  row.after(exp);
}
async function submitMrComment(mrId) {
  const input = document.getElementById('mr-cmt-' + mrId);
  const text = (input && input.value || '').trim();
  if(!text) { toast('请先填写意见'); return; }
  const r = await api(`/api/branches/merge-requests/${mrId}/comments`,
    {method:'POST', body:JSON.stringify({comment:text})});
  if(r && r.error) { toast('提交失败：' + r.error); return; }
  toast('意见已记录 ✓');
  // 重展开刷新时间线
  const exp = document.querySelector(`tr[data-mr-expand="${mrId}"]`);
  if(exp) { const btn = exp.previousElementSibling && exp.previousElementSibling.querySelector('button[onclick*="toggleMrComments"]'); if(btn) { exp.remove(); toggleMrComments(mrId, btn); } }
}
// ── 合并结果预览（dry-run，不落库）：基于当前分支差异展示合并将发生的变更 ──
async function previewMerge(mrId) {
  const r = await api(`/api/branches/merge-requests/${mrId}/preview-merge`);
  if(r && r.error) { toast('预览失败：' + r.error); return; }
  const s = r.summary || {};
  const row = (l, n, unit, color) => `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed var(--line);font-size:12.5px;">
    <span style="color:var(--mut);">${l}</span><b style="color:${color};">${n} ${unit}</b></div>`;
  const lbx = document.getElementById('lbx');
  document.getElementById('lbx-body').innerHTML = `
    <h3>🔮 合并结果预览（不落库）</h3>
    <div style="font-size:12.5px;color:var(--mut);margin:4px 0 8px;">
      <b>${esc(r.source)}</b> → ${esc(r.target)}（${esc(r.verb)}语义）· 基于当前分支差异实时计算
    </div>
    ${row('🟢 新增实体', s.entities_add, '条', 'var(--grn)')}
    ${row('🟠 更新实体', s.entities_update, '条', 'var(--amb)')}
    ${row('🔴 移除实体', s.entities_remove, '条', 'var(--red)')}
    ${row('🔗 新增关系', s.relations_add, '条', 'var(--grn)')}
    ${row('🔗 更新关系', s.relations_update, '条', 'var(--amb)')}
    ${row('🔗 移除关系', s.relations_remove, '条', 'var(--red)')}
    ${row('⚠ 冲突（未解决）', r.conflicts_unresolved, '处', r.conflicts_unresolved>0?'var(--red)':'var(--grn)')}
    ${r.conflicts_unresolved>0
      ? '<div style="font-size:11.5px;color:var(--red);margin-top:8px;">⚠ 存在未解决冲突，通过前须先逐条解决</div>'
      : '<div style="font-size:11.5px;color:var(--grn);margin-top:8px;">✓ 无未解决冲突，可发起通过</div>'}
    <div class="lbx-actions"><button class="btn ghost" onclick="document.getElementById('lbx').classList.remove('show')">关闭</button></div>`;
  lbx.classList.add('show');
}
async function deleteMergeRequest(id) {  if(!(await confirmDialog(`确认删除合并请求 #${id}？`))) return;
  const r = await api(`/api/branches/merge-requests/${id}`, {method:'DELETE'});
  if(r && r.error) { toast('删除失败：' + r.error); return; }
  toast('合并请求已删除'); loadMerges();
}
async function resolveMerge(id, action) {
  // P1-1 驳回必填意见（≥5 字，追溯用）；后端同样校验，前端先拦一遍。返回 true=操作成功
  let reviewNote = '';
  if(action === 'reject') {
    const note = await promptDialog({title:'驳回合并请求', message:'请填写驳回意见（≥5 字，将记入评审记录用于追溯）：', placeholder:'如：需求覆盖不全，冲突解决后重新提交', okText:'确认驳回', multiline:true, rows:3});
    if(note === null) return false;   // 用户取消
    reviewNote = String(note).trim();
    if(reviewNote.length < 5) { toast('驳回意见至少 5 个字'); return false; }
  }
  const r = await api(`/api/branches/merge-requests/${id}/resolve`, {method:'POST', body:JSON.stringify({action, review_note:reviewNote})});
  if(r && r.error) {
    // P0-1 冲突重算：冲突清单已变化（409 conflict_changed）→ 弹引导对话框列出新增/消失冲突
    if(r.code === 'conflict_changed') {
      const added = r.added || [], removed = r.removed || [];
      const lines = ['⚠ 冲突清单已变化（目标分支在评审期间被更新），需重新确认后再通过：'];
      if(added.length) lines.push('', `新增 ${added.length} 处冲突：`, ...added.slice(0,5).map(c=>`· ${c.entity_name||c.entity_id} 的 ${c.field}${c.conflict_type==='delete_modify'?'（删除/修改）':''}`));
      if(removed.length) lines.push('', `已消解 ${removed.length} 处冲突（原解决决策作废）`);
      await confirmDialog(lines.join('\n'), {title:'⚠ 冲突清单已更新', okText:'我知道了，去处理'});
      loadMerges();
      openMergeDiffDrawer(id);   // 直接打开对比分析抽屉处理新冲突
      return false;
    }
    toast('操作失败：' + r.error); return false;
  }
  toast(`合并请求已${action==='approve'?'通过并合并':'关闭'}`);
  loadMerges();
  loadBranches();  // 合并后目标分支元素数变化
  loadGraph();     // 当前分支图谱刷新
  return true;
}
// P1-1：草稿转正式评审（draft → open）
async function openMrForReview(id) {
  const r = await api(`/api/branches/merge-requests/${id}/open`, {method:'POST'});
  if(r && r.error) { toast('操作失败：' + r.error); return; }
  toast('已转正式评审，进入待评审队列'); loadMerges();
}
// P1-1：重新打开已关闭的合并请求（closed → open）
async function reopenMr(id) {
  const r = await api(`/api/branches/merge-requests/${id}/reopen`, {method:'POST'});
  if(r && r.error) { toast('操作失败：' + r.error); return; }
  toast('合并请求已重新打开'); loadMerges();
}
// FR-KG-16：回滚已合并的发布合并请求（release 还原到合并前快照）
async function rollbackMergeRequest(id) {
  if(!(await confirmDialog(`确认回滚合并请求 #${id}？\nrelease 分支将还原到该合并前的快照（实体/关系/文档/分块），此操作不可撤销。`))) return;
  const r = await api(`/api/branches/merge-requests/${id}/rollback`, {method:'POST'});
  if(r && r.error) { toast('回滚失败：' + r.error); return; }
  toast(`回滚成功：还原 ${r.restored_entities||0} 实体 / ${r.restored_relations||0} 关系`);
  loadMerges();
  loadBranches();  // release 元素数变化
  loadGraph();     // 当前分支图谱刷新
}
// ── 合并对比分析抽屉（差异分析 + 冲突检测结果 + 解决操作，git 风格合并主流程）──
let mrgMrId = null;                              // 当前分析的合并请求 id
let mdData = null;                               // diff 结果（供筛选）
const mdFilter = {cat:'all', type:'', kw:''};    // 差异筛选状态：cat=all|added|modified|removed|rel
let mrgUnresolved = 0;                           // 未解决冲突数
let _mrgConflictIds = [];                        // 冲突实体 id 集合（供左清单着色）
let mrgConflicts = [];                           // 当前 MR 冲突明细（供右栏字段级解决 UI）

// ════ 图谱工作区右上角 MR 全局入口数据源（2026-09-10 米爸裁剪后）════
// 原分支上下文条（🌿 分支 / ahead-behind / 最近提交 / 实体关系计数）已整条删除：
//   分支由 ws-branch-ctl 选择器承载；MR 收敛至 ph 行右上角 #ws-mr-entry（进行中数量 + 发起 MR）
// 函数名与调用点保持不变（24-graph.js loadGraph 链路零改动），职责变为刷新 MR 徽标
async function loadBranchCtxBar() {
  const wrap = document.getElementById('ws-mr-entry');
  if(!wrap) return;
  try{
    const mrs = await api('/api/branches/merge-requests').catch(()=>null);
    const open = (Array.isArray(mrs)?mrs:[]).filter(m=>['draft','open'].includes(m.status||'open')).length;
    const badge = document.getElementById('ws-mr-badge');
    if(badge){ badge.textContent = open>0 ? ('进行中 '+open) : ''; badge.style.display = open>0 ? 'inline-block' : 'none'; }
  }catch(e){ /* MR 徽标非关键路径：失败静默，入口按钮仍可用 */ }
}
// 一键发起 MR：从图谱工作区当前分支发起（预填源/目标，对标 GitHub「Compare & pull request」）
async function openMrPrefill() {
  const cur = getCurrentBranch();
  const b = (Array.isArray(_branches)?_branches:[]).find(x=>x.name===cur) || {};
  const tgt = cur==='dev' ? 'release' : (b.parent_branch || 'dev');
  showModal('merge');
  const t = document.getElementById('f-tgt'), s = document.getElementById('f-src');
  for(let i=0;i<20;i++) { await new Promise(r=>setTimeout(r,100)); if(s && s.options.length>1) break; }
  if(t && [...t.options].some(o=>o.value===tgt)) { t.value = tgt; if(t.onchange) t.onchange(); }
  if(s && [...s.options].some(o=>o.value===cur)) { s.value = cur; if(s.onchange) s.onchange(); }
}

// ════ 差异叠加图谱（OntoDiffGraph 式：变更着色 + 1-hop 灰显上下文 + 清单/图谱双向联动）════
const _dogState = {};   // pane -> {loading, rendered, byId, kindOf, headNodes, baseNodes, pendingFocus}
const _DOG_COLORS = {
  conflict: {fill:'#EEEDFE', stroke:'#534AB7', text:'#3C3489', ico:'⚠ '},
  removed:  {fill:'#FCEBEB', stroke:'#A32D2D', text:'#791F1F', ico:'－ ', dash:' stroke-dasharray="5 3"'},
  added:    {fill:'#EAF3DE', stroke:'#3B6D11', text:'#27500A', ico:'＋ '},
  modified: {fill:'#FAEEDA', stroke:'#BA7517', text:'#633806', ico:'✎ '},
  context:  {fill:'#F1EFE8', stroke:'#B4B2A9', text:'#888780', ico:''},
};
function _dogKind(id, added, modified, removed, conflict) {
  if(conflict.has(id)) return 'conflict';
  if(removed.has(id)) return 'removed';
  if(added.has(id)) return 'added';
  if(modified.has(id)) return 'modified';
  return 'context';
}
function _dogProps(p) {
  try { const o = (typeof p==='string') ? JSON.parse(p||'{}') : (p||{}); return (o && typeof o==='object') ? o : {}; }
  catch(e) { return {}; }
}
async function renderDiffOverlay(pane, diffData, conflictIds) {
  const svgEl = document.getElementById(pane+'-graph-svg');
  const legEl = document.getElementById(pane+'-graph-legend');
  const detEl = document.getElementById(pane+'-graph-detail');
  if(!svgEl) return;
  if(!diffData) { svgEl.innerHTML = '<div style="padding:24px;color:var(--mut);text-align:center;">无差异数据</div>'; return; }
  if(_dogState[pane] && _dogState[pane].loading) return;
  _dogState[pane] = {loading:true, rendered:false, pendingFocus:(_dogState[pane]||{}).pendingFocus};
  detEl.innerHTML = '';
  const base = diffData.base, head = diffData.head;
  svgEl.innerHTML = '<div style="padding:24px;color:var(--mut);text-align:center;">构建差异叠加图谱中…（拉取两分支图谱）</div>';
  // 两分支全量图谱（status=all；后端 LIMIT 200 兜底），做 id→实体 映射
  const [hg, bg] = await Promise.all([
    api(`/api/knowledge/graph?branch=${encodeURIComponent(head)}&status=all`).catch(()=>null),
    api(`/api/knowledge/graph?branch=${encodeURIComponent(base)}&status=all`).catch(()=>null),
  ]);
  const headNodes = {}; (hg&&hg.entities||[]).forEach(e=>{ headNodes[e.id]=e; });
  const baseNodes = {}; (bg&&bg.entities||[]).forEach(e=>{ baseNodes[e.id]=e; });
  const added = new Set((diffData.entities.added||[]).map(e=>e.id));
  const modified = new Set((diffData.entities.modified||[]).map(e=>e.id));
  const removed = new Set((diffData.entities.removed||[]).map(e=>e.id));
  const conflict = new Set(conflictIds||[]);
  const changed = new Set([...added, ...modified, ...removed]);
  if(!changed.size) {
    svgEl.innerHTML = '<div style="padding:24px;color:var(--grn);text-align:center;">🎉 两个分支图谱无差异</div>';
    legEl.innerHTML = ''; _dogState[pane] = {loading:false, rendered:true}; return;
  }
  // 关系：以 head 为主 + base 补充（移除的关系/孤立移除节点也要可见）；1-hop 邻居灰显
  const headRels = (hg&&hg.relations||[]), baseRels = (bg&&bg.relations||[]);
  const relAddedSet = new Set((diffData.relations.added||[]).map(r=>`${r.source_id}|${r.target_id}|${r.relation_type}`));
  const relRemovedSet = new Set((diffData.relations.removed||[]).map(r=>`${r.source_id}|${r.target_id}|${r.relation_type}`));
  const relModifiedSet = new Set((diffData.relations.modified||[]).map(r=>`${r.source_id}|${r.target_id}|${r.relation_type}`));
  const seenRel = new Set(), allRels = [];
  const pushRel = (r, srcHead)=>{ const k = `${r.source_id}|${r.target_id}|${r.relation_type}`;
    if(seenRel.has(k)) return; seenRel.add(k); allRels.push({...r, _side: srcHead?'head':'base'}); };
  headRels.forEach(r=>pushRel(r,true)); baseRels.forEach(r=>pushRel(r,false));
  // 上下文节点：与变更节点直接相连、自身未变更
  const context = new Set();
  allRels.forEach(r=>{
    if(changed.has(r.source_id) && !changed.has(r.target_id)) context.add(r.target_id);
    if(changed.has(r.target_id) && !changed.has(r.source_id)) context.add(r.source_id);
  });
  let ctxCap = 60, ctxCut = false;
  if(context.size > ctxCap) { ctxCut = true; const keep = [...context].slice(0, ctxCap); context.clear(); keep.forEach(id=>context.add(id)); }
  const renderIds = new Set([...changed, ...context]);
  const nodes = [...renderIds].map(id=>{
    const e = headNodes[id] || baseNodes[id] || {id, name:id};
    return {id, name:e.name||id, kind:_dogKind(id, added, modified, removed, conflict)};
  });
  const edges = allRels
    .filter(r=>renderIds.has(r.source_id) && renderIds.has(r.target_id))
    .map(r=>({...r, kind: relAddedSet.has(`${r.source_id}|${r.target_id}|${r.relation_type}`) ? 'added'
      : relRemovedSet.has(`${r.source_id}|${r.target_id}|${r.relation_type}`) ? 'removed'
      : relModifiedSet.has(`${r.source_id}|${r.target_id}|${r.relation_type}`) ? 'modified' : 'context'}));
  // dagre 确定性布局（LR）；无 dagre 时降级网格
  let pos = {}, W = 640, H = 80, usedG = null;
  if(window.dagre && nodes.length) {
    const g = new dagre.graphlib.Graph({multigraph:true});
    g.setGraph({rankdir:'LR', nodesep:16, ranksep:52, marginx:14, marginy:14});
    g.setDefaultNodeLabel(()=>({})); g.setDefaultEdgeLabel(()=>({}));
    nodes.forEach(n=>{ n.w = Math.max(104, Math.min(250, n.name.length*7.4+42)); n.h = 30; g.setNode(n.id, {width:n.w, height:n.h}); });
    edges.forEach((e,i)=>g.setEdge(e.source_id, e.target_id, {}, 'e'+i));
    try { dagre.layout(g);
      nodes.forEach(n=>{ const gn = g.node(n.id); pos[n.id] = {x:gn.x, y:gn.y, w:n.w, h:n.h}; });
      const gi = g.graph(); W = gi.width||640; H = gi.height||80; usedG = g;
    } catch(err) { pos = null; }
  } else pos = null;
  if(!pos) { // 网格降级
    const cols = Math.ceil(Math.sqrt(nodes.length));
    nodes.forEach((n,i)=>{ pos[n.id] = {x:(i%cols)*150+80, y:Math.floor(i/cols)*54+30, w:130, h:30}; });
    W = cols*150+60; H = Math.ceil(nodes.length/cols)*54+40;
  }
  const enc = s=>encodeURIComponent(String(s));
  const edgePath = pts => { if(!pts || !pts.length) return '';
    let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
    for(let i=1;i<pts.length;i++) d += ` L ${pts[i].x.toFixed(1)} ${pts[i].y.toFixed(1)}`;
    return d; };
  const eColors = {added:['#3B6D11',''], removed:['#A32D2D',' stroke-dasharray="5 3"'], modified:['#BA7517',''], context:['#C9C7BD','']};
  const edgeSvgStr = edges.map(e=>{
    let pts = null;
    if(usedG) { try { const ge = usedG.edge(e.source_id, e.target_id); pts = ge && ge.points; } catch(x) { pts = null; } }
    if(!pts) { const a=pos[e.source_id], b=pos[e.target_id]; if(!a||!b) return ''; pts=[{x:a.x+a.w/2,y:a.y},{x:b.x-b.w/2,y:b.y}]; }
    const [col, dash] = eColors[e.kind]||eColors.context;
    return `<path d="${edgePath(pts)}" fill="none" stroke="${col}" stroke-width="${e.kind==='context'?1:1.6}"${dash} marker-end="url(#dog-arrow)" opacity="${e.kind==='context'?0.55:0.95}"/>`;
  }).join('');
  const nodeSvg = nodes.map(n=>{
    const p = pos[n.id], c = _DOG_COLORS[n.kind];
    const label = n.name.length>18 ? n.name.slice(0,17)+'…' : n.name;
    return `<g data-dog-id="${enc(n.id)}" style="cursor:pointer;" onclick="dogShowDetail('${pane}','${enc(n.id)}')">
      <rect x="${(p.x-p.w/2).toFixed(1)}" y="${(p.y-15).toFixed(1)}" width="${p.w}" height="30" rx="7" fill="${c.fill}" stroke="${c.stroke}" stroke-width="1"${c.dash||''}/>
      <text x="${p.x.toFixed(1)}" y="${p.y.toFixed(1)}" text-anchor="middle" dominant-baseline="central" font-size="12" fill="${c.text}" font-family="system-ui,sans-serif">${c.ico}${esc(label)}</text>
    </g>`;
  }).join('');
  svgEl.innerHTML = `<svg width="${Math.max(W,200)}" height="${Math.max(H,80)}" viewBox="0 0 ${Math.max(W,200)} ${Math.max(H,80)}" xmlns="http://www.w3.org/2000/svg" role="img">
    <defs><marker id="dog-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="#888780" stroke-width="1.5" stroke-linecap="round"/></marker></defs>
    ${edgeSvgStr}${nodeSvg}</svg>`;
  legEl.innerHTML = `<b style="font-size:12px;">基线 ${esc(base)} → ${esc(head)}</b>
    <span style="color:#3B6D11;">＋ 新增 ${added.size}</span><span style="color:#BA7517;">✎ 修改 ${modified.size}${relModifiedSet.size?`（含边 ${relModifiedSet.size}）`:''}</span>
    <span style="color:#A32D2D;">－ 移除 ${removed.size}</span>${conflict.size?`<span style="color:#534AB7;">⚠ 冲突 ${conflict.size}</span>`:''}
    <span style="color:#888780;">灰=1-hop 上下文${ctxCut?`（超出上限仅显示 ${ctxCap} 个）`:''}</span>`;
  // 保存状态供详情/联动
  const byId = {}; nodes.forEach(n=>byId[n.id]=n);
  _dogState[pane] = {loading:false, rendered:true, byId, kindOf:id=>byId[id]?byId[id].kind:null,
    headNodes, baseNodes, added, modified, removed, conflict, allRels,
    modEntries:(diffData.entities.modified||[]), pendingFocus:_dogState[pane].pendingFocus};
  if(_dogState[pane].pendingFocus) { const f = _dogState[pane].pendingFocus; _dogState[pane].pendingFocus = null; setTimeout(()=>dogFlash(pane, f), 80); }
}
function dogShowDetail(pane, encId) {
  const st = _dogState[pane]; const detEl = document.getElementById(pane+'-graph-detail');
  if(!st || !st.rendered || !detEl) return;
  const id = decodeURIComponent(encId);
  const kind = st.kindOf(id); if(!kind) return;
  const h = st.headNodes[id] || {}, b = st.baseNodes[id] || {};
  const kindLabel = {conflict:'⚠ 冲突实体', removed:'－ 移除实体（基线存在，head 不存在）', added:'＋ 新增实体（head 新出现）', modified:'✎ 修改实体', context:'上下文节点（未变更）'}[kind] || kind;
  const propRows = p => { const o = _dogProps(p); const ks = Object.keys(o);
    return ks.length ? ks.map(k=>`<tr><td style="padding:2px 8px;color:var(--mut);white-space:nowrap;"><code>${esc(k)}</code></td><td style="padding:2px 8px;">${esc(String(o[k]).slice(0,80))}</td></tr>`).join('')
      : `<tr><td colspan="2" style="padding:2px 8px;color:var(--mut);">（无属性）</td></tr>`; };
  let body = '';
  if(kind==='modified' || kind==='conflict' || kind==='added') body = propRows(h.properties);
  else body = propRows(b.properties);
  const rels = st.allRels.filter(r=>r.source_id===id || r.target_id===id).slice(0,8);
  const relHtml = rels.length ? `<div style="margin-top:6px;font-size:11.5px;color:var(--mut);">关联关系（前 ${rels.length}）：${rels.map(r=>`<code>${esc(r.relation_type)}</code>`).join('、')}</div>` : '';
  detEl.innerHTML = `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--blue-l);">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <b>${esc(h.name||b.name||id)}</b><code>${esc(id)}</code>
      <span class="st" style="background:${_DOG_COLORS[kind].fill};color:${_DOG_COLORS[kind].text};">${kindLabel}</span>
      ${h.status?`<span class="st">head: ${esc(h.status)}</span>`:''}${b.status?`<span class="st">base: ${esc(b.status)}</span>`:''}
      <span style="flex:1"></span><button class="btn sm ghost" onclick="document.getElementById('${pane}-graph-detail').innerHTML=''">✕</button>
    </div>
    ${kind==='modified'||kind==='conflict' ? `<div style="margin-top:8px;font-size:12px;"><b>属性变更（base → head）</b>
      <table style="width:100%;border-collapse:collapse;margin-top:4px;font-size:12px;">
        ${(() => { const modEnt = (st.modEntries||[]).find(x=>x.id===id); const chs = modEnt ? modEnt.changes||[] : [];
          return chs.length ? chs.map(ch=>`<tr><td style="padding:3px 8px;white-space:nowrap;"><code>${esc(ch.field)}</code></td>
          <td style="padding:3px 8px;color:var(--mut);">${esc(String(ch.base_value).slice(0,60))}</td>
          <td style="padding:3px 8px;">→</td><td style="padding:3px 8px;"><b>${esc(String(ch.head_value).slice(0,60))}</b></td></tr>`).join('')
          : '<tr><td colspan="4" style="padding:3px 8px;color:var(--mut);">无字段级变更记录</td></tr>'; })()}
      </table></div>` : `<div style="margin-top:8px;font-size:12px;"><b>${kind==='added'?'head 属性':'base 属性'}</b>
      <table style="width:100%;border-collapse:collapse;margin-top:4px;font-size:12px;">${body}</table></div>`}
    ${relHtml}</div>`;
}
function dogFlash(pane, encId) {
  const el = document.querySelector(`#${pane}-graph-svg [data-dog-id="${encId}"]`);
  if(!el) { toast('该实体不在图谱视图范围（可能不在 1-hop 上下文内）'); return; }
  el.scrollIntoView({behavior:'smooth', block:'center', inline:'center'});
  const rect = el.querySelector('rect');
  if(rect) {
    if(!rect.getAttribute('data-orig-stroke')) {
      rect.setAttribute('data-orig-stroke', rect.getAttribute('stroke')||'');
      rect.setAttribute('data-orig-width', rect.getAttribute('stroke-width')||'1');
    }
    rect.setAttribute('stroke', '#E24B4A'); rect.setAttribute('stroke-width', '2.5');
    setTimeout(()=>{ rect.setAttribute('stroke', rect.getAttribute('data-orig-stroke'));
      rect.setAttribute('stroke-width', rect.getAttribute('data-orig-width')); }, 1800);
  }
}
function dogLocate(pane, encId) {   // encId 已 encodeURIComponent（列表 📍 / 图谱节点一致）
  if(pane==='bd') bdTab('graph'); else { toast('图谱定位仅适用于分支对比视图'); return; }
  const st = _dogState[pane];
  if(st && st.rendered) dogFlash(pane, encId);
  else { if(!st) _dogState[pane] = {}; _dogState[pane].pendingFocus = encId; toast('叠加图谱渲染中，完成后自动定位…'); }
}
let _bdTabCur = 'list';
function bdTab(t) {
  _bdTabCur = t;
  const on = 'background:var(--blue-l);color:var(--blue-d);font-weight:600;';
  const off = '';
  document.getElementById('bd-tab-list').style.cssText = `cursor:pointer;font-size:12px;padding:6px 14px;border-radius:6px 6px 0 0;${t==='list'?on:''}`;
  document.getElementById('bd-tab-graph').style.cssText = `cursor:pointer;font-size:12px;padding:6px 14px;border-radius:6px 6px 0 0;${t==='graph'?on:''}`;
  ['bd-summary','bd-filter','bd-body'].forEach(id=>{ const e=document.getElementById(id); if(e) e.style.display = t==='list' ? '' : 'none'; });
  const gp = document.getElementById('bd-graph-pane'); if(gp) gp.style.display = t==='graph' ? '' : 'none';
  if(t==='graph' && bdData) renderDiffOverlay('bd', bdData, []);
  else if(t==='graph' && !bdData) { const s=document.getElementById('bd-graph-svg'); if(s) s.innerHTML='<div style="padding:24px;color:var(--mut);text-align:center;">先在上方选择分支并点击「对比」</div>'; }
}
function openMergeDiffDrawer(mrId) {
  mrgMrId = mrId;
  mdData = null; mdFilter.cat='all'; mdFilter.type=''; mdFilter.kw='';
  mrgUnresolved = 0; mrgConflicts = [];
  document.getElementById('mrg-mask').classList.add('show');
  document.getElementById('mrg-drawer').classList.add('show');
  const _mprog = document.getElementById('mrg-progress'); if(_mprog) _mprog.innerHTML='';
  const _mfield = document.getElementById('mrg-field');
  if(_mfield) _mfield.innerHTML='<div style="padding:18px;color:var(--mut);text-align:center;">加载中…</div>';
  loadMergeDetail();
}
function closeMergeDiffDrawer() {
  document.getElementById('mrg-mask').classList.remove('show');
  document.getElementById('mrg-drawer').classList.remove('show');
}
async function loadMergeDetail() {
  if(!mrgMrId) return;
  const titleEl = document.getElementById('mrg-title');
  const noteEl = document.getElementById('mrg-action-note');
  const approveBtn = document.getElementById('mrg-approve');
  const rejectBtn = document.getElementById('mrg-reject');
  titleEl.innerHTML = '<span style="color:var(--mut);">加载中…</span>';
  const mrs = await api('/api/branches/merge-requests');
  const m = (mrs||[]).find(x=>x.id===mrgMrId);
  if(!m) { titleEl.innerHTML = '合并请求不存在或已被删除'; return; }
  const st = _mrStatusInfo(m.status);
  titleEl.innerHTML = `<b>${esc(m.source_branch)}</b> → <b>${esc(m.target_branch)}</b> · <span class="st ${st[1]}">${st[0]}</span> · ${esc(m.created_by||'-')}`
    + (m.title ? ` · <span style="font-weight:400;">${esc(m.title)}</span>` : '');
  // P1-1 审批信息行：评审人 / 驳回意见 / 冲突清单重算时间（P0-1 mergeability 追溯）
  const auditLine = [
    m.reviewed_by ? `评审人：${esc(m.reviewed_by)}` : '',
    (m.status==='closed' && m.review_note) ? `驳回意见：${esc(m.review_note)}` : '',
    m.conflict_updated_at ? `冲突重算：${esc(String(m.conflict_updated_at).slice(0,16))}` : '',
  ].filter(Boolean).join(' ｜ ');
  const isOpen = (m.status||'open')==='open';
  if(!isOpen) {
    approveBtn.style.display = 'none'; rejectBtn.style.display = 'none';
    const notes = {
      draft:  '📝 草稿状态（暂不进入评审队列），可在列表中「转评审」',
      merged: '🟪 该合并请求已通过并合并（历史记录，只读）',
      closed: '❌ 该合并请求已关闭（历史记录，可在列表中重新打开）',
    };
    noteEl.innerHTML = (notes[m.status] || '该合并请求已处理（历史记录，只读）') + (auditLine?`<div style="font-size:11px;color:var(--mut);margin-top:4px;">${auditLine}</div>`:'');
  } else {
    approveBtn.style.display = ''; rejectBtn.style.display = '';
    if(auditLine) noteEl.innerHTML = `<div style="font-size:11px;color:var(--mut);">${auditLine}</div>`;
  }
  // FR-KG-16：已合并且目标为 release 的合并请求可回滚
  const rbBtn = document.getElementById('mrg-rollback');
  if(rbBtn) rbBtn.style.display = (m.status==='merged' && m.target_branch==='release') ? '' : 'none';
  await loadMergeDiff();
  await loadMergeConflicts();
}
async function loadMergeDiff() {
  const bodyEl = document.getElementById('mrg-body');
  const mrs = await api('/api/branches/merge-requests');
  const m = (mrs||[]).find(x=>x.id===mrgMrId);
  if(!m) return;
  bodyEl.innerHTML = '<div style="padding:16px;color:var(--mut);text-align:center;">对比差异中…</div>';
  // 以目标分支为基线对比源分支（合并将把源分支变更带入目标）
  const r = await api(`/api/branches/diff?base=${encodeURIComponent(m.target_branch)}&head=${encodeURIComponent(m.source_branch)}`);
  if(r && r.error) { bodyEl.innerHTML = `<b style="color:var(--red);">${esc(r.error)}</b>`; return; }
  mdData = r;
  renderMergeDiff();
}
function renderMergeDiff() {
  const r = mdData; if(!r) return;
  const bodyEl = document.getElementById('mrg-body');
  const E = r.entities||{};
  const added = (E.added||[]).slice();
  const modified = (E.modified||[]).slice().sort((a,b)=>_riskScore(b)-_riskScore(a));
  const removed = (E.removed||[]).slice();
  const total = added.length + modified.length + removed.length;
  if(!total) { bodyEl.innerHTML = '<div style="padding:16px;color:var(--mut);text-align:center;">两个分支无差异</div>'; return; }
  const row = (e, label) => {
    const conf = _mrgConflictIds.includes(e.id);
    const color = conf ? 'var(--red)' : (label==='add' ? 'var(--blue-d)' : (label==='mod' ? 'var(--amb)' : 'var(--red)'));
    const ico = conf ? '⚠' : (label==='add' ? '＋' : (label==='mod' ? '✎' : '－'));
    const badge = conf ? '<span class="st r">⚠ 冲突</span>'
      : (label==='add') ? '<span class="st ok">新增</span>'
      : (label==='mod') ? (_riskTag(e, _mrgConflictIds) || '<span class="st w">修改</span>')
      : '<span class="st r">移除</span>';
    return `<div onclick="mrgFocusEntity('${esc(e.id)}')" style="cursor:pointer;padding:8px 12px;border-bottom:1px dashed var(--line);border-left:3px solid ${color};${conf?'background:#fdf3f3;':''}" title="点击查看字段级详情">
      <div style="display:flex;align-items:center;gap:6px;"><b style="color:${color};">${ico}</b> <b>${esc(e.name)}</b></div>
      <div style="font-size:10.5px;color:var(--mut);margin-top:2px;"><code>${esc(e.id)}</code> ${badge} <span style="color:var(--mut);">${esc(e.entity_type||'-')}</span></div>
    </div>`;
  };
  let html = `<div style="padding:8px 12px;border-bottom:1px solid var(--line);font-size:11.5px;font-weight:600;color:var(--mut);">差异清单 <span style="font-weight:400;">(${total})</span></div>`;
  if(modified.length) html += modified.map(e=>row(e,'mod')).join('');
  if(added.length)    html += added.map(e=>row(e,'add')).join('');
  if(removed.length)  html += removed.map(e=>row(e,'rem')).join('');
  bodyEl.innerHTML = html;
}
function mrgFocusEntity(id) {
  const field = document.getElementById('mrg-field');
  if(!field) return;
  const E = (mdData && mdData.entities) || {};
  const inList = (arr)=> (arr||[]).find(e=>e.id===id);
  const ent = inList(E.added) || inList(E.modified) || inList(E.removed);
  const myConflicts = mrgConflicts.filter(c=>c.entity_id===id);
  if(!ent && !myConflicts.length) { field.innerHTML = '<div style="padding:18px;color:var(--mut);text-align:center;">未找到该实体的字段级详情</div>'; return; }
  let html = '';
  if(ent) {
    const kind = inList(E.added) ? 'add' : (inList(E.modified) ? 'mod' : 'rem');
    const kindLabel = kind==='add' ? '🟢 新增' : (kind==='mod' ? '🟠 修改' : '🔴 移除');
    const color = kind==='add' ? 'var(--blue-d)' : (kind==='mod' ? 'var(--amb)' : 'var(--red)');
    const changes = ent.changes||[];
    html += `<div style="border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:10px;">
      <div style="font-size:13px;font-weight:600;">${kindLabel} <b style="color:${color};">${esc(ent.name)}</b> <span class="st w" style="font-size:9.5px;">${esc(ent.entity_type||'-')}</span></div>
      <div style="font-size:11px;color:var(--mut);margin-top:2px;"><code>${esc(ent.id)}</code>${_riskTag(ent,_mrgConflictIds)?' '+_riskTag(ent,_mrgConflictIds):''}</div>
      ${changes.length ? '' : '<div style="font-size:11.5px;color:var(--mut);margin-top:6px;">（此次变更无字段级变化，请参考左侧清单）</div>'}
    </div>`;
    html += changes.map(ch=>`
      <div style="padding:8px 0;border-bottom:1px dashed var(--line);">
        <div style="font-size:12px;font-weight:600;">字段 <code>${esc(ch.field)}</code></div>
        <div style="display:flex;gap:14px;font-size:11.5px;margin-top:4px;flex-wrap:wrap;">
          <span><span class="st w">目标</span> <code>${esc(String(ch.base_value))}</code></span>
          <span><span class="st ok">源</span> <code>${esc(String(ch.head_value))}</code></span>
        </div>
      </div>`).join('');
  } else {
    html += `<div style="border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:10px;font-size:13px;font-weight:600;">${esc(myConflicts[0].entity_name||id)}</div>`;
  }
  if(myConflicts.length) {
    html += `<div style="font-size:12px;font-weight:600;color:var(--red);margin-bottom:2px;">⚠ 冲突解决（${myConflicts.length} 处）</div>`;
    html += myConflicts.map(c=>renderConflictItem(mrgMrId, c)).join('');
  } else if(ent) {
    html += '<div style="font-size:11.5px;margin-top:6px;"><span class="st ok">✓ 该实体无属性冲突</span></div>';
  }
  field.innerHTML = html;
}
async function loadMergeConflicts() {
  const field = document.getElementById('mrg-field');
  if(!field) return;
  field.innerHTML = '<div style="padding:16px;color:var(--mut);text-align:center;">加载冲突检测结果…</div>';
  const r = await api(`/api/branches/merge-requests/${mrgMrId}/conflicts`);
  if(r && r.error) { field.innerHTML = `<b style="color:var(--red);">${esc(r.error)}</b>`; return; }
  mrgUnresolved = r.unresolved||0;
  const conflicts = r.conflicts||[];
  mrgConflicts = conflicts;
  _mrgConflictIds = [...new Set(conflicts.map(c=>c.entity_id).filter(Boolean))];   // 供左清单冲突着色
  const approveBtn = document.getElementById('mrg-approve');
  const note = document.getElementById('mrg-action-note');
  const prog = document.getElementById('mrg-progress');
  if(prog) {
    if(conflicts.length) {
      const total = conflicts.length, resolved = total - mrgUnresolved;
      const pct = total ? Math.round(resolved/total*100) : 0;
      prog.innerHTML = `<div style="display:flex;align-items:center;gap:10px;font-size:12px;">
        <span style="white-space:nowrap;">冲突解决 <b>${resolved}/${total}</b></span>
        <div style="flex:1;height:8px;border-radius:5px;background:var(--line);overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:${mrgUnresolved>0?'var(--amb)':'var(--grn)'};transition:width .3s;"></div>
        </div>
        <span style="white-space:nowrap;color:${mrgUnresolved>0?'var(--red)':'var(--grn)'};font-size:11px;">${mrgUnresolved>0?('⚠ '+mrgUnresolved+' 处未解决'):'✅ 已全部解决'}</span>
      </div>`;
    } else {
      prog.innerHTML = '<span style="color:var(--grn);font-size:12px;">🎉 无属性冲突，可直接通过合并</span>';
    }
  }
  if(mrgUnresolved>0) {
    approveBtn.disabled = true; approveBtn.title = '还有 ' + mrgUnresolved + ' 处冲突未解决';
    if(approveBtn.style.display !== 'none') note.innerHTML = `⚠ ${mrgUnresolved} 处冲突未解决，先解决再通过`;
  } else {
    approveBtn.disabled = false; approveBtn.title = '';
    if(approveBtn.style.display !== 'none') note.innerHTML = conflicts.length ? '✅ 冲突已全部解决，可执行合并' : '🎉 无属性冲突，可直接通过合并';
  }
  // 右侧 #mrg-field：默认聚焦第一个冲突实体；无冲突则提示
  if(conflicts.length) {
    const firstId = conflicts[0].entity_id;
    if(firstId) mrgFocusEntity(firstId);
    else field.innerHTML = conflicts.map(c=>renderConflictItem(mrgMrId, c)).join('');
    renderMergeDiff();   // 刷新左清单冲突着色
  } else {
    field.innerHTML = '<div style="padding:24px;text-align:center;color:var(--grn);font-size:13px;">🎉 两个分支无属性冲突，可直接通过合并</div>';
  }
}
async function resolveMergeFromDrawer(action) {
  if(!mrgMrId) return;
  // 与列表页 resolveMerge 共用逻辑（驳回意见弹窗 / conflict_changed 引导）；
  // 仅成功时关抽屉——引导对话框场景保持抽屉打开，便于直接处理新冲突
  const ok = await resolveMerge(mrgMrId, action);
  if(ok) closeMergeDiffDrawer();
}
// ── 分支表单（创建/编辑双模式）──
async function initBranchForm() {
  const editName = document.getElementById('f-edit-branch').value;
  const parentEl = document.getElementById('f-parent');
  // 基线来源固定为 dev | release（个人分支仅从这两个分支拉取基线）
  parentEl.innerHTML = '<option value="dev">dev（主开发分支）</option><option value="release">release（发布分支）</option>';
  const typeEl = document.getElementById('f-type');
  const nameEl = document.getElementById('f-name');
  const parentRow = document.getElementById('f-parent-row');
  // 本地分支（离线/实验）不 fork 基线：隐藏基线来源行
  const toggleParent = (t) => { if(parentRow) parentRow.style.display = (t==='local') ? 'none' : ''; };
  typeEl.onchange = ()=> toggleParent(typeEl.value);
  // 编辑模式回填
  if(editName) {
    document.getElementById('branch-form-title').textContent = '✏️ 编辑分支：' + editName;
    const b = (await api('/api/branches')).find(x=>x.name===editName);
    if(b) {
      nameEl.value = b.name;
      typeEl.value = b.branch_type || 'personal';
      typeEl.disabled = true;   // 分支类型创建后不可修改（后端 update 亦忽略）
      toggleParent(typeEl.value);
      // 父分支若非预置基线（历史数据），动态补选项
      if(![...parentEl.options].some(o=>o.value===b.parent_branch) && b.parent_branch) {
        const o = document.createElement('option'); o.value = b.parent_branch; o.textContent = b.parent_branch + '（历史基线）';
        parentEl.appendChild(o);
      }
      parentEl.value = b.parent_branch || 'dev';
      document.getElementById('f-desc').value = b.description || '';
      const sr = document.getElementById('f-status-row'); sr.style.display = 'block';
      document.getElementById('f-status').value = b.status || 'active';
    }
  } else {
    const sr = document.getElementById('f-status-row'); if(sr) sr.style.display = 'none';
    typeEl.disabled = false;
    nameEl.value = '';
    typeEl.value = 'personal';
    toggleParent('personal');
    parentEl.value = 'dev';
    document.getElementById('f-desc').value = '';
  }
}
function editBranch(name) {
  showModal('branch');          // 先渲染表单（会重置 hidden 值）
  document.getElementById('f-edit-branch').value = name;  // 再标记编辑目标
  initBranchForm();             // 手动触发编辑模式回填
}
async function deleteBranch(name) {
  if(PROTECTED_BRANCHES.includes(name)) { toast(name + ' 是受保护分支，不可删除'); return; }
  if(!(await confirmDialog(`确认删除分支「${name}」？\n（含其历史合并请求；有实体/关系/子分支时将阻止）`))) return;
  const r = await api('/api/branches/' + encodeURIComponent(name), {method:'DELETE'});
  if(r && r.error) { toast('删除失败：' + r.error); return; }
  toast('分支已删除');
  if(getCurrentBranch() === name) localStorage.removeItem('mbse_branch');
  loadBranches(); loadMerges(); loadGraph();
}
async function initMergeForm() {
  const branches = await api('/api/branches');
  const active = branches.filter(b=>b.status!=='archived');
  // 合并流向（git 风格）：目标只能是 dev 或 release；源排除 release；目标=release 时源仅 dev
  const renderMergeSrc = ()=>{
    const tgt = document.getElementById('f-tgt').value;
    const srcEl = document.getElementById('f-src');
    // 本地分支（离线/实验）不可作为合并源
    let list = active.filter(b=>b.name!==tgt && b.name!=='release' && b.branch_type!=='local');
    if(tgt === 'release') list = list.filter(b=>b.name==='dev');   // 发布：仅 dev → release
    const prev = srcEl.value;
    srcEl.innerHTML = '<option value="">— 选择源分支 —</option>' + list.map(b=>`<option value="${esc(b.name)}">${esc(b.name)}（${b.branch_type}·${b.entity_count||0}元素）</option>`).join('');
    if(list.some(b=>b.name===prev)) srcEl.value = prev;
  };
  // 目标分支：仅 dev 与 release（2026-09-12 从 PROTECTED_BRANCHES 解耦——保护名单含 personal，但 personal 不可作合并目标）
  document.getElementById('f-tgt').innerHTML = '<option value="">— 选择目标分支 —</option>' +
    active.filter(b=>MERGE_TARGET_BRANCHES.includes(b.name)).map(b=>`<option value="${esc(b.name)}">${esc(b.name)}（${b.branch_type==='release'?'🔒 发布':'开发'}）</option>`).join('');
  const srcEl = document.getElementById('f-src'), tgtEl = document.getElementById('f-tgt');
  tgtEl.onchange = ()=>{ renderMergeSrc(); mergePreview(); updateRvHint(); };
  srcEl.onchange = mergePreview;
  renderMergeSrc();
  updateRvHint();
}
// 发布版本号提示：仅目标=release 时提示留空自动生成（合并到 dev 忽略该字段）
function updateRvHint() {
  const el = document.getElementById('f-rv-hint');
  if(!el) return;
  const tgt = document.getElementById('f-tgt');
  el.textContent = tgt && tgt.value==='release' ? '（发布到 release，留空自动生成 v1/v2…）' : '（仅目标=release 时生效，合并到 dev 将忽略）';
}
async function mergePreview() {
  const el = document.getElementById('merge-preview');
  const src = document.getElementById('f-src').value;
  const tgt = document.getElementById('f-tgt').value;
  if(!src || !tgt) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  if(src === tgt) { el.innerHTML = '<b style="color:var(--red);">源/目标不能相同</b>'; return; }
  el.innerHTML = '计算合并差异中…';
  try {
    // 只读 diff（不落库）：以 tgt 为基线对比 src 的新增/修改/移除
    const r = await api(`/api/branches/diff?base=${encodeURIComponent(tgt)}&head=${encodeURIComponent(src)}`);
    if(r && r.error) { el.innerHTML = `<b style="color:var(--red);">${esc(r.error)}</b>`; return; }
    const s = r.summary||{};
    el.innerHTML = `源分支 <b>${esc(src)}</b> → 目标 <b>${esc(tgt)}</b>：
      🟢 新增 <b style="color:var(--grn);">${s.ent_added||0}</b> ·
      🟠 修改 <b style="color:var(--amb);">${s.ent_modified||0}</b> ·
      🔴 移除 <b style="color:var(--red);">${s.ent_removed||0}</b> ·
      🔗 关系 <b>+${s.rel_added||0}/~${s.rel_modified||0}/-${s.rel_removed||0}</b><br>
      <span style="font-size:11px;color:var(--mut);">（发起合并时后端将检测同实体属性冲突，冲突需逐字段解决后才能通过）</span>`;
  } catch(e) { el.innerHTML = '预览失败：' + esc(e.message); }
}

// ── AI 设计工坊 ──
