/* 本体工作台：树 / 详情 / 变更日志 / 实例
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 9639-11981  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function ontGraphFullscreen(){
  const g = document.getElementById('ont-graph');
  if(!g) return;
  const on = g.classList.toggle('ont-fullscreen');
  const btn = document.getElementById('ont-fs-btn');
  if(btn) btn.textContent = on ? '⛶ 退出全屏' : '⛶ 全屏';
  document.body.style.overflow = on ? 'hidden' : '';
  setTimeout(()=>{ try{ graphZoomReset(); }catch(e){} }, 120);
}
document.addEventListener('keydown', e=>{
  if(e.key==='Escape' && document.getElementById('ont-graph')?.classList.contains('ont-fullscreen')) ontGraphFullscreen();
});
let ontView = 'classes';   // 实体维度（2026-09-02 P0-2 顶部 Tab）：classes | props | dprops
let ontPaneMode = 'edit';  // 中栏视图模式（2026-09-02 P0-4）：edit（字段表单）| graf（1-hop 邻域子图）
let ontMegaTab = 'model';  // 一级 tab：model（本体模型）/ terms（术语词典）
// 顶部实体维度 Tab：决定左栏树内容 + 中栏投影对象；不改变中栏视图模式（ontPaneMode 保持）
function switchOntTab(el, tab) {
  ontView = tab;
  ontLeaveGlobal();   // 五轮调整：切顶部维度 Tab 退出全局内联视图（变更历史/OWL），恢复左栏与工具条
  el.parentNode.querySelectorAll('[data-tabgrp=ont]').forEach(x=>x.classList.remove('on'));
  el.classList.add('on');
  const _tp = document.getElementById('ont-pane-terms');
  if(_tp) _tp.style.display = 'none';
  if(ontMegaTab!=='model') return;
  const _oe = document.getElementById('ont-pane-edit');
  if(_oe) _oe.style.display = 'flex';
  // 左栏维度标题随 Tab 变化（P0-3：左栏唯一选择源）
  const _dim = document.getElementById('ont-left-dim');
  if(_dim) _dim.textContent = tab==='props' ? '对象属性（域 → 值域）' : (tab==='dprops' ? '数据属性（XSD 类型）' : (tab==='individuals' ? '实体类型（选择查看个体）' : '类层次（subClassOf）'));
  if(!(ontData && ontData.types && ontData.types.length)){ loadOntology(); return; }
  // 焦点永不清空：当前选中不属于新维度 → 自动选中该维度第一项；属于 → 保持
  const kindByView = {classes:'entity', props:'relation', dprops:'attribute', individuals:'entity'};
  const cur = ontCurType();
  if(!cur || cur.type_kind!==kindByView[tab]){
    const first=(ontData.types||[]).find(x=>x.type_kind===kindByView[tab]);
    if(first) ontSelected={kind:'type', id:first.id, name:first.name};
  }
  renderOntTree();
  renderOntDesc();
  if(ontPaneMode==='graf'){ renderOntGraph(); renderOntSideDetail(); }
  else if(ontPaneMode==='owl'){ renderOntSideDetail(); renderOntNodeOwl(); }  // 四轮调整：owl 态随维度切换刷新节点序列化
  else if(ontPaneMode==='cl'){ renderOntSide(); renderOntNodeChangeLog(); }  // 五轮调整：cl 态随维度切换刷新节点变更历史
  else { renderOntSide(); }
  ontFocusBreadcrumb();
}
// 中栏视图切换（P0-4 + 五轮调整）：edit=字段表单 / graf=以选中节点为焦点的子图 / owl=当前节点 OWL 序列化 / cl=当前节点变更历史；左栏树与右栏职责不变
function switchOntPane(mode){
  ontLeaveGlobal();   // 五轮调整：从全局视图切 toggle → 退出全局视图，恢复左栏与工具条
  ontPaneMode = mode;
  ['edit','graf','owl','cl'].forEach(x=>{
    const b=document.getElementById('ont-pane-btn-'+x);
    if(b){ const on=mode===x; b.style.background=on?'var(--blue-l)':'#fff'; b.style.color=on?'var(--blue-d)':'var(--mut)'; b.style.fontWeight=on?'600':'400'; }
  });
  renderOntTree();   // 三轮调整：树节点点击处理器随模式刷新（图谱态 keepView=true 联动），避免 stale onclick 重置视图
  if(!(ontData && ontData.types && ontData.types.length)){ loadOntology(); return; }
  if(mode==='graf'){
    // 图谱模式：以左栏当前选中为焦点（契约：中栏=左栏选中节点的投影）
    const t = ontCurType();
    if(t && t.type_kind==='entity') ontFocus = t.name;
    ontSyncViewPanes();
    renderOntSideDetail();   // 三轮调整：编辑→图谱切换时右栏 body 重渲染（详情+变更历史），修复停留编辑态内容的缺口
    const gf=document.getElementById('ont-graph-footer');
    if(gf) gf.innerHTML = ontGraphLegend();
    try{ ontRelayout(); }catch(e){}
  } else if(mode==='owl'){
    // 四轮调整：节点级 OWL 序列化（RDF/XML · Turtle · SHACL，可复制/下载）
    const t = ontCurType();
    owlNodeId = t ? t.id : null;
    ontSyncViewPanes();
    renderOntSideDetail();
    renderOntNodeOwl();
  } else if(mode==='cl'){
    // 五轮调整：节点级变更历史（OWL 右侧第四段，当前所选对象）
    ontSyncViewPanes();
    renderOntSide();
    renderOntNodeChangeLog();
  } else {
    ontSyncViewPanes();
    renderOntDesc(); renderOntSide();
  }
  ontFocusBreadcrumb();
}
// 节点级 OWL 视图渲染（toggle 第三态）：针对当前所选节点
function renderOntNodeOwl(){
  const box=document.getElementById('ont-nodeowl');
  if(!box) return;
  const t=ontCurType();
  if(!t){ box.innerHTML='<div style="color:var(--mut);font-size:12px;padding:20px;">从左侧选择一个类型后查看其 OWL 序列化</div>'; return; }
  const kindName=t.type_kind==='relation'?'对象属性':(t.type_kind==='attribute'?'数据属性':'类');
  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap;">
      <b style="font-size:12.5px;color:var(--blue-d);">🧬 OWL</b>
      <span class="tag">${esc(t.icon||'🧬')} ${esc(t.name)} · ${kindName}</span>
      ${ontOwlChipsHtml('node')}
      <span style="flex:1"></span>
      <button class="btn sm" onclick="ontOwlCopy('node')">📋 复制</button>
      <button class="btn sm ghost" onclick="ontOwlDownload('node')">⬇ 下载</button>
    </div>
    <div style="flex:none;font-size:11px;color:var(--mut);background:var(--blue-l);border-bottom:1px solid var(--line);padding:4px 14px;">
      当前节点及其直接语境（父类链 / 域值域 / 关联关系）的序列化片段；全量本体见顶栏「🧬 OWL」。
    </div>
    <pre id="ont-owl-pre-node" style="flex:1;min-height:0;margin:0;background:#1e1e1e;color:#d4d4d4;padding:14px;font-size:11.5px;line-height:1.55;overflow:auto;white-space:pre-wrap;word-break:break-all;"></pre>`;
  ontOwlRefresh('node');
}
// 旧 kb2 子页 tab 切换（抽取批次已并入数据整理）：保留兼容，v2g 分支改道 batch 队列
function switchKB2Tab(el, pane) {
  if(el && el.parentNode) el.parentNode.querySelectorAll('[data-tabgrp=kb2]').forEach(x=>x.classList.remove('on'));
  if(el) el.classList.add('on');
  const v2gPane = document.getElementById('kb2-pane-v2g');
  if(v2gPane) v2gPane.style.display = pane==='v2g'?'block':'none';
  const fusPane = document.getElementById('kb2-pane-fusion');
  if(fusPane) fusPane.style.display = pane==='fusion'?'block':'none';
  if(pane==='v2g') fusNav(document.querySelector('.fus-nav-btn[data-fpane="batch"]'), 'batch');
  if(pane==='fusion') loadFusion();
}
// FR-KG-1 补 G15 抽取质量评估已移除（2026-08-17 标注审核优化）
// ⚔️ 冲突消解（P0-1：融合第四步——检测矛盾事实 → 加权评分 → 人工裁决留痕）
async function loadConflicts(){
  const box = document.getElementById('conflict-panel');
  if(!box) return;
  try{
    const list = await api('/api/knowledge/conflicts?status=pending&limit=100');
    const arr = list||[];
    if(!arr.length){ box.innerHTML = '<div class="fempty">✅ <b style="color:var(--grn,#2f855a);font-weight:500;">属性矛盾已清零</b>——字段裁决完成。后续：待落图三元组在「三元组审核」通过后落图；分支发布走「发布」站。<br><button class="btn sm" onclick="fusNav(Array.from(document.querySelectorAll(\'.fus-nav-btn\')).find(b=>b.dataset.fpane===\'store\'),\'store\')">去三元组审核 →</button> <button class="btn sm" onclick="fusNav(Array.from(document.querySelectorAll(\'.fus-nav-btn\')).find(b=>b.dataset.fpane===\'pub\'),\'pub\')">去发布 →</button> <button class="btn sm ghost" onclick="conflictDetect()">🔍 再检测一轮</button></div>'; return; }
    box.innerHTML = arr.map(x=>{
      const ev = x.evidence||{};
      const aSrc = (ev.a&&(ev.a.source_doc||ev.a.source_type))||'';
      const bSrc = (ev.b&&(ev.b.source_doc||ev.b.source_type))||'';
      return `<div class="fcard">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
          <span class="fpill gray">#${x.id}</span>
          <span class="fpill ${x.kind==='relation_attr'?'amb':''}" style="${x.kind==='relation_attr'?'':'background:var(--blue-l);color:var(--blue-d);'}">${x.kind==='entity_attr'?'🧬 实体属性冲突':'🔗 关系属性冲突'}</span>
          <b style="font-size:12.5px;">${esc(x.attr_key)}</b>
          ${x.triple?`<span style="color:var(--mut);font-size:11px;">三元组：${esc(x.triple)}</span>`:''}
          <span style="flex:1"></span>
          <span style="color:var(--mut);font-size:11px;">${esc(x.created_at||'')}</span>
        </div>
        <div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;">
          <div style="flex:1;min-width:220px;border:1px solid var(--line);border-radius:8px;padding:7px 10px;background:#fafbfd;">
            <div style="font-size:11px;color:var(--mut);">A 侧：${esc(x.name_a)}${aSrc?` <span class="fpill gray">${esc(aSrc)}</span>`:''} <span class="fpill gray">加权 ${(x.score_a||0).toFixed(3)}</span></div>
            <div style="font-size:13px;margin-top:2px;word-break:break-all;">${esc(x.value_a||'')}</div>
          </div>
          <div style="flex:1;min-width:220px;border:1px solid var(--line);border-radius:8px;padding:7px 10px;background:#fafbfd;">
            <div style="font-size:11px;color:var(--mut);">B 侧：${esc(x.name_b)}${bSrc?` <span class="fpill gray">${esc(bSrc)}</span>`:''} <span class="fpill gray">加权 ${(x.score_b||0).toFixed(3)}</span></div>
            <div style="font-size:13px;margin-top:2px;word-break:break-all;">${esc(x.value_b||'')}</div>
          </div>
        </div>
        <div style="display:flex;gap:6px;margin-top:8px;align-items:center;">
          <span style="font-size:11px;color:var(--mut);">裁决（采纳侧值统一两侧 + 审计留痕）：</span>
          <button class="btn sm" onclick="conflictAdjudicate(${x.id},'left')">采纳 A 侧</button>
          <button class="btn sm ghost" onclick="conflictAdjudicate(${x.id},'right')">采纳 B 侧</button>
          <button class="btn sm ghost" onclick="conflictAdjudicate(${x.id},'ignore')">忽略</button>
        </div>
        <details style="margin-top:8px;border-top:1px dashed var(--line);padding-top:6px;">
          <summary style="cursor:pointer;font-size:11.5px;color:var(--blue-d);">📄 查看证据与上下文（人工确认的依据）</summary>
          <div style="padding:6px 2px 2px;font-size:11.5px;">
            <div style="margin-bottom:6px;"><b style="color:var(--blue-d);">得分构成</b>　A 侧加权 ${(x.score_a||0).toFixed(3)} · B 侧加权 ${(x.score_b||0).toFixed(3)} · 检测方式：${esc(x.method||'同归一名称/同三元组值冲突扫描')}</div>
            <div style="margin-bottom:6px;"><b style="color:var(--blue-d);">属性语义提示</b>　编号/标识类属性冲突=身份矛盾，须人工确认权威值；量测类属性可多值保留并标注来源与时间。</div>
            <div style="margin-bottom:4px;"><b style="color:var(--blue-d);">来源定位</b></div>
            <div style="color:var(--mut);line-height:1.7;">A 侧：${esc(aSrc||'来源未记录')}${ev.a&&ev.a.value_time?` · 值时间：${esc(ev.a.value_time)}`:''}<br>B 侧：${esc(bSrc||'来源未记录')}${ev.b&&ev.b.value_time?` · 值时间：${esc(ev.b.value_time)}`:''}</div>
          </div>
        </details>
      </div>`;
    }).join('');
  }catch(e){ box.innerHTML = '<div style="padding:12px;color:var(--red);font-size:12px;">冲突列表加载失败</div>'; }
}
async function conflictDetect(){
  if(!branchWritable()) return;
  try{
    const r = await api('/api/knowledge/conflicts/detect', {method:'POST'});
    toast(`冲突检测完成：新增 ${r.detected} 条` + (r.skipped?`（跳过重复 ${r.skipped}）`:''));
    loadConflicts();
  }catch(e){ toast('冲突检测失败：'+(e.message||e)); }
}
async function conflictAdjudicate(id, decision){
  if(!branchWritable()) return;
  const label = decision==='left'?'采纳 A 侧':decision==='right'?'采纳 B 侧':'忽略';
  if(!(await confirmDialog(`确认「${label}」？采纳侧值将统一到两侧实体/关系属性，并记录裁决审计留痕。`, {title:'属性融合'}))) return;
  try{
    const r = await api(`/api/knowledge/conflicts/${id}/adjudicate`, {method:'POST', body: JSON.stringify({decision})});
    if(r.error){ toast(r.error); return; }
    toast(r.action==='ignored' ? '已忽略该冲突（两侧保留）' : `裁决完成：规范值「${r.canonical}」已统一两侧`);
    loadConflicts();
  }catch(e){ toast('裁决失败：'+(e.message||e)); }
}
// 📝 本体蓝图（P2-1 统一 Schema 冷启动：业务文本 / SysML Profile → 草案 → 人工确认应用）
let _bpDrafts = [];
let _bpSource = 'text';
function bpSetSource(mode){
  _bpSource = mode;
  document.getElementById('bp-src-text').classList.toggle('on', mode==='text');
  document.getElementById('bp-src-profile').classList.toggle('on', mode==='profile');
  const txt = document.getElementById('bp-src-text'), pf = document.getElementById('bp-src-profile');
  if(txt) txt.style.cssText = mode==='text' ? 'cursor:pointer;background:var(--blue-l);color:var(--blue-d);padding:2px 10px;border-radius:10px;' : 'cursor:pointer;padding:2px 10px;border-radius:10px;';
  if(pf) pf.style.cssText = mode==='profile' ? 'cursor:pointer;background:var(--blue-l);color:var(--blue-d);padding:2px 10px;border-radius:10px;' : 'cursor:pointer;padding:2px 10px;border-radius:10px;';
  const fmtRow = document.getElementById('bp-profile-fmt');
  if(fmtRow) fmtRow.style.display = mode==='profile' ? 'block' : 'none';
  const lab = document.getElementById('bp-input-label');
  const ta = document.getElementById('bp-text');
  const fi = document.getElementById('bp-file');
  if(fi) fi.accept = mode==='profile' ? '.profile,.xmi,.uml,.kerml,.xml' : '.txt,.md,.csv,.json,.xml,.yaml,.yml,.log,.sql,.ddl';
  if(mode==='profile'){
    if(lab) lab.textContent = 'SysML Profile 内容（XMI XML / KerML 文本）';
    if(ta) ta.placeholder = '粘贴 SysML Profile 内容：\n1.x → <uml:Profile ...><packagedElement xmi:type="uml:Stereotype" name="部件"/>…\n2.x → package X { metadata def 部件 { attribute 名称 : String; } }';
  } else {
    if(lab) lab.textContent = '业务文档 / 模型描述文本';
    if(ta) ta.placeholder = '粘贴业务文档、DDL 或模型描述文本，LLM 提炼实体/关系/属性草案（Mock 模式按「X 是 Y 的子类」「定义 X 类型」规则提取）…';
  }
}
function openBlueprint(){
  _bpDrafts = [];
  showModal('blueprint');
  bpSetSource('text');
  const fi = document.getElementById('bp-file');
  if(fi) fi.value = '';
  const fn = document.getElementById('bp-file-name');
  if(fn) fn.textContent = '';
  const d = document.getElementById('bp-drafts');
  if(d) d.innerHTML = '<div style="padding:10px;color:var(--mut);font-size:12px;">粘贴或上传文件后「✨ 提取草案」，在此勾选确认应用</div>';
}
// 上传文件：读取文本内容载入输入框（与粘贴共用同一条提取流程）
function bpFileChosen(input){
  const f = input.files && input.files[0];
  const nm = document.getElementById('bp-file-name');
  if(!f){ if(nm) nm.textContent=''; return; }
  if(f.size > 5*1024*1024){ toast('文件超过 5MB，请截取关键片段后粘贴'); input.value=''; if(nm) nm.textContent=''; return; }
  const rd = new FileReader();
  rd.onload = e => {
    const ta = document.getElementById('bp-text');
    if(ta) ta.value = String(e.target.result||'');
    if(nm) nm.textContent = `已载入 ${f.name}（${(f.size/1024).toFixed(1)} KB），点击「✨ 提取草案」继续`;
    toast('已载入 ' + f.name);
  };
  rd.onerror = () => { toast('文件读取失败，请重试'); input.value=''; };
  rd.readAsText(f, 'utf-8');
}
async function blueprintExtract(){
  const ta = document.getElementById('bp-text');
  const text = (ta && ta.value.trim()) || '';
  if(!text){ toast(_bpSource==='profile' ? '请先粘贴或上传 SysML Profile 内容' : '请先粘贴或上传业务文档/模型描述文本'); return; }
  const body = _bpSource==='profile'
    ? {source_type:'profile', content:text, format:document.getElementById('bp-fmt')?.value || '1x'}
    : {source_type:'text', text};
  try{
    const r = await api('/api/knowledge/ontology/draft', {method:'POST', body: JSON.stringify(body)});
    if(r.error){ toast(r.error); return; }
    _bpDrafts = r.drafts || [];
    toast(`已提取 ${_bpDrafts.length} 条草案` + (_bpDrafts.length?'':(_bpSource==='profile'?'（Profile 解析未命中，检查格式）':'（规则未命中，配置 LLM Key 后自动智能提炼）')));
    blueprintRenderDrafts();
  }catch(e){ toast('提取失败：'+(e.message||e)); }
}
function blueprintRenderDrafts(){
  const box = document.getElementById('bp-drafts');
  if(!box) return;
  if(!_bpDrafts.length){ box.innerHTML = '<div style="padding:10px;color:var(--mut);font-size:12px;">未提取到草案——' + (_bpSource==='profile' ? 'Profile 内容需为合法 XMI/KerML（或类型与现有同名被去重）。' : '文本需含「X 是 Y 的子类」「定义 X 类型」等描述（或配置 LLM Key 获取智能提炼）。') + '</div>'; return; }
  box.innerHTML = `<div style="display:flex;gap:6px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--line);font-size:11px;color:var(--mut);background:#fafaf7;"><input type="checkbox" id="bp-all" onchange="bpToggleAll(this)"> 全选${_bpSource==='profile'?' · SysML Profile 结构化解析（1.x/2.x）':''}</div>` +
    _bpDrafts.map(d=>`<div style="display:flex;gap:6px;align-items:flex-start;padding:6px 10px;border-bottom:1px solid var(--line);font-size:12px;">
      <input type="checkbox" class="bp-chk" value="${d.id}" style="margin-top:2px;">
      <div style="flex:1;">
        <div><b>${esc(d.name)}</b> <span class="tag">${d.type_kind==='entity'?'🧬 实体':d.type_kind==='relation'?'🔗 关系':'📋 属性'}</span>
          ${d.parent_name?`<span class="tag" style="background:#ecfdf5;color:#059669;">父：${esc(d.parent_name)}</span>`:''}
          ${d.relation_src?`<span class="tag" style="background:#f3eeff;color:#7c3aed;">${esc(d.relation_src)} → ${esc(d.relation_tgt)}</span>`:''}
          ${d.profile_source?`<span class="tag" style="background:#fff7ed;color:#f59e0b;">Profile：${esc(d.profile_source)}</span>`:''}
        </div>
        ${d.evidence?`<div style="font-size:11px;color:var(--mut);margin-top:2px;">${_bpSource==='profile'?'提示':'证据'}：${esc(d.evidence)}</div>`:''}
      </div>
    </div>`).join('');
}
function bpToggleAll(el){ document.querySelectorAll('.bp-chk').forEach(x=>x.checked = el.checked); }
async function blueprintApply(){
  const ids = [...document.querySelectorAll('.bp-chk:checked')].map(x=>Number(x.value));
  if(!ids.length){ toast('请先勾选要应用的草案'); return; }
  if(!(await confirmDialog(`确认应用 ${ids.length} 条本体草案？将写入本体类型（重名/父类型不存在的会被拦截）。`, {title:'应用本体草案'}))) return;
  try{
    const r = await api('/api/knowledge/ontology/drafts/apply', {method:'POST', body: JSON.stringify({ids})});
    if(r.error){ toast(r.error); return; }
    const okN = (r.applied||[]).length, errs = r.errors||[];
    toast(`已应用 ${okN} 条` + (errs.length?`，拦截 ${errs.length} 条`:''));
    if(errs.length) toast('拦截：' + errs.map(e=>`${e.name||''}(${e.error})`).join('；'));
    const appliedIds = new Set((r.applied||[]).map(a=>a.id));
    _bpDrafts = _bpDrafts.filter(d=>!appliedIds.has(d.id));
    blueprintRenderDrafts();
    try{ loadOntology(); }catch(e2){}
  }catch(e){ toast('应用失败：'+(e.message||e)); }
}
// P1-E：知识治理全链路进度条（数据整理收敛——4 个 tab 强化为同一条流水线的环节，待办直达）
// 环节：抽取预览 → 融合工作台（两段式）→ 实体待审 → 关系待审 → 已发布；待办>0 黄色高亮，点击直达
async function loadKBFlowBar() {
  const steps = document.getElementById('kb-flow-steps');
  if(!steps) return;
  let v2g=0, fusion=0, store=0, pub=0, trips={};
  try {
    const [batches, fus, _trips, mrs] = await Promise.all([
      api('/api/knowledge/v2g/batches').catch(()=>[]),
      api('/api/knowledge/fusion/status').catch(()=>({})),
      api('/api/knowledge/triples/stats').catch(()=>({})),
      api('/api/branches/merge-requests').catch(()=>[]).then(x=>Array.isArray(x)?x:[]),
    ]);
    trips = _trips || {};   // 2026-09-22 修复：trips 原为 try 块内 const 解构，块外 segTip 引用即 ReferenceError（切回本页报「trips is not defined」）
    v2g = (batches||[]).reduce((s,b)=>s+(b.pending_n||0),0);
    fusion = (fus.pending_pairs||0) + (fus.conflict_n||0);
    store = trips.to_store||0;
    // P0-3 发布显式化：待发布 = 待评审（open）的合并请求数（AI 批次 personal→dev 门禁 / dev→release 发布）
    pub = Array.isArray(mrs) ? mrs.filter(m=>(m.status||'open')==='open').length : 0;
  } catch(e) {}
  // 2026-09-22 五站化：水位条段名与五站导航对齐（store=三元组审核的待落图积压，pub=发布站待审 MR）
  const segs = [
    ['v2g','① 抽取审核', v2g],
    ['fusion','② 消歧与融合', fusion],
    ['store','🧾 待落图', store],
    ['pub','📦 发布待办', pub],
  ];
  // 管线水位计：四段 = 流水线四个缓冲区，数字为积压量；归一在①→②间静默完成，发布是收口动作
  const segTip = {
    v2g: '① 抽取审核：文档/SysML 解析出的知识候选，等待人工验收。词典归一（命名归一）在 ①→② 之间静默完成',
    fusion: '② 消歧与融合待办：需要人工判定的重复对与属性矛盾 —— 重复消歧 / 属性融合两站即其内部工位',
    store: store>0 ? `🧾 待落图：三元组审核已通过、等待 commit 落图（已落图 ${trips.stored||0} 条 / 待落图 ${store} 条）` : `🧾 待落图：当前无积压（已落图 ${trips.stored||0} 条）`,
    pub: '📦 发布待办：待评审的合并请求数（personal→dev→release）——合并评审通过即进入 RAG 消费视野'
  };
  steps.innerHTML = segs.map(([pane,label,n],i)=>{
    const hot = n>0;
    return (i?`<span style="color:var(--mut);font-size:10px;">→</span>`:'') +
      `<span class="st ${hot?'w':'g'} b" style="cursor:pointer;font-size:11px;" onclick="kbFlowGo('${pane}')" title="${segTip[pane]||'点击直达该环节'}">${label} <b>${n}</b></span>`;
  }).join('');
  // 2026-09-22 五站化：导航角标同步（store=待落图积压，pub=待审 MR 数）
  const _tS = document.getElementById('fus-n-store'); if(_tS) _tS.textContent = store||0;
  const _tP = document.getElementById('fus-n-pub'); if(_tP) _tP.textContent = pub||0;
}
async function kbFlowGo(pane) {
  // 发布显式化（P0-3）：有待审批合并请求时，先展示发布待办面板，可直达版本管理审批合并
  if(pane==='pub'){
    let mrs=[]; try{ mrs = (await api('/api/branches/merge-requests').catch(()=>[]))||[]; }catch(e){}
    mrs = Array.isArray(mrs)?mrs:[];
    const pend = mrs.filter(m=>(m.status||'open')==='open');
    if(pend.length){
      const rows = pend.map(m=>`<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;background:#fff;">
        <b style="font-size:12px;">#${m.id}</b> <span class="st b">${esc(m.source_branch)} → ${esc(m.target_branch)}</span>
        <span style="font-size:11px;color:var(--mut);flex:1;">${esc((m.created_at||'').slice(0,16).replace('T',' '))}</span>
        <span class="st ${(m.unresolved_conflicts||0)>0?'r':'a'}">${(m.unresolved_conflicts||0)>0?`冲突 ${m.unresolved_conflicts}`:'就绪'}</span></div>`).join('');
      openPanel('📦 发布待办（待审批合并请求）', `<div style="font-size:12px;color:var(--mut);margin:2px 0 10px;">以下合并请求尚未审批。personal→dev 为工作分支合并评审，dev→release 为正式发布门禁（发布后知识进入 AI 建模 / 问答消费视野；RAG 只消费 release）。</div><div style="max-height:260px;overflow:auto;">${rows}</div><div style="margin-top:12px;text-align:right;"><button class="btn ghost" onclick="closePanel()">关闭</button><button class="btn" onclick="closePanel();go('branch')">去合并请求审批 →</button></div>`);
      return;
    }
    toast('无待处理合并请求，发布基线完整');
    go('branch'); return;
  }
  if(pane==='store'){
    // 2026-09-22 断链修复：先审后落。pending 三元组存在 → 展示审核面板（通过后自动落图）；
    // 无 pending 才直接 tripleCommit（兼容历史 approved 未落图数据）。此前此点击会跳过审核直接落图，
    // 而审核面板容器已移除 → 文档抽取链的 pending 三元组永远无法批准。
    let _stats = {};
    try{ _stats = await api('/api/knowledge/triples/stats').catch(()=>({})) || {}; }catch(e){}
    if((_stats.pending||0) > 0){
      const el = document.getElementById('triple-review-pane');
      if(el) el.scrollIntoView({behavior:'smooth', block:'center'});
      loadTripleReviewPane();
      toast(`有 ${_stats.pending} 条待审三元组 —— 请在「三元组审核」面板中通过后自动落图`);
      return;
    }
    tripleCommit(); return;
  }
  if(pane==='v2g'){
    // IA 收敛：抽取候选直达 → 数据整理左栏第 0 站（候选批次队列）
    go('kb','kb-b');
    setTimeout(()=>{ fusNav(document.querySelector('.fus-nav-btn[data-fpane="batch"]'), 'batch'); }, 400);
    return;
  }
  const el = Array.from(document.querySelectorAll('[data-tabgrp="kb2"]'))
    .find(t=>(t.getAttribute('onclick')||'').includes(`'${pane}'`));
  if(el) switchKB2Tab(el, pane);
}
// 融合工作台：两段式校验（工序① 规则预处理免费 + 工序② LLM 成对判定）+ 状态卡
let _fusionBusy = false;
// ── P0 方案 v2 / S5：融合工作台双栏布局 —— 右栏队列切换 + 批次确认 ──
function fusNav(btn, pane){
  document.querySelectorAll('.fus-nav-btn').forEach(x=>{x.style.borderColor='';x.classList.remove('on');});
  if(btn){ btn.style.borderColor='var(--blue)'; btn.classList.add('on'); }
  // 2026-09-17 R5：原「_kbBEntry 归属 + nav-glossary/nav-workbench 高亮」已成死代码（两入口先后隐藏），整体移除；
  // 知识中心整合后 kb-b/kb-d 的高亮由 02-shell.js 的 go()/tab() 统一负责。
  // 词典=主数据独立页：隐藏治理队列与全链路条（流水线装饰不属于词典页）
  const isGl = (pane==='terms');
  const _navPanel = document.getElementById('fus-nav');
  if(_navPanel) _navPanel.style.display = isGl ? 'none' : '';
  const _fb = document.getElementById('kb-flow-bar');
  if(_fb) _fb.style.display = 'none';   // 2026-09-22 简化：水位条与五站导航重复，隐藏（角标同步逻辑保留）
  ['batch','gray','conflict','store','confirm','terms','pub'].forEach(p=>{
    const el = document.getElementById('fus-pane-'+p);
    if(el) el.style.display = p===pane ? '' : 'none';
  });
  if(pane==='batch') v2gReviewLoad();
  if(pane==='gray') loadDuplicates();
  if(pane==='conflict') loadConflicts();
  if(pane==='confirm') renderBatchConfirm();
  if(pane==='terms') conceptsLoad();
  // 2026-09-22 五站化：store/pub 为真实动作站 —— 三元组审核（独立 fus-pane-store，随导航切换）与发布（图库水位 + MR 评审）
  if(pane==='store') loadTripleReviewPane();
  if(pane==='pub') renderPubPane();
}
// 2026-09-22 五站化 P0：发布站面板 —— 图库水位 + 待审批合并请求（personal→dev→release）
async function renderPubPane(){
  const el = document.getElementById('fus-pub-body');
  if(!el) return;
  let trips = {}, mrs = [];
  try{ trips = await api('/api/knowledge/triples/stats').catch(()=>({})) || {}; }catch(e){}
  try{ mrs = await api('/api/branches/merge-requests').catch(()=>[]) || []; }catch(e){}
  const pend = Array.isArray(mrs) ? mrs.filter(m=>(m.status||'open')==='open') : [];
  const mrRows = pend.length ? pend.map(m=>`<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;">
      <b style="font-size:12px;">#${m.id}</b> <span class="st b">${esc(m.source_branch)} → ${esc(m.target_branch)}</span>
      <span style="font-size:11px;color:var(--mut);flex:1;">${esc((m.created_at||'').slice(0,16).replace('T',' '))}</span>
      <span class="st ${(m.unresolved_conflicts||0)>0?'r':'a'}">${(m.unresolved_conflicts||0)>0?`冲突 ${m.unresolved_conflicts}`:'就绪'}</span></div>`).join('')
    : '<div style="padding:8px 0;color:var(--mut);font-size:12px;">✅ 无待审批合并请求 —— 发布基线完整（历史发布在「图谱工作区 → 合并请求」可查）</div>';
  el.innerHTML = `
    <div style="display:flex;gap:18px;flex-wrap:wrap;padding:10px 12px;border-bottom:1px dashed var(--line);font-size:12px;">
      <span>图库水位：已落图 <b>${trips.stored||0}</b> 条 / 待落图 <b style="color:${(trips.to_store||0)>0?'var(--amb,#c77700)':'inherit'}">${trips.to_store||0}</b> 条</span>
      <span style="color:var(--mut);">落图由「三元组审核」commit 驱动；分支发布由合并请求评审驱动</span>
    </div>
    <div style="padding:10px 12px;">
      <div style="font-size:12.5px;font-weight:500;margin-bottom:6px;">待审批合并请求（${pend.length}）</div>
      ${mrRows}
      <div style="margin-top:10px;text-align:right;"><button class="btn" onclick="go('branch')">去合并请求审批 →</button></div>
    </div>`;
}
  // loadDictQueue / fusGotoTerms 已随 D2 新词建议队列收敛移除（2026-08-27）：功能上移至术语词典页「⏳ 待采纳建议」折叠区（前端过滤 suggested_by==='llm'）
async function renderBatchConfirm(){
  const sel = document.getElementById('fus-confirm-batch');
  const bodyEl = document.getElementById('fus-confirm-body');
  if(!sel || !bodyEl) return;
  let batches = [];
  try{ batches = await api('/api/knowledge/v2g/batches'); }catch(e){ bodyEl.innerHTML='<div style="color:var(--red);font-size:12px;">批次加载失败</div>'; return; }
  batches = (batches || []).filter(b=>(b.pending_n||0) > 0);
  if(sel.options.length === 0){
    sel.innerHTML = '<option value="">— 选择待确认批次 —</option>' +
      batches.map(b=>`<option value="${esc(b.batch_id)}">${esc(String(b.batch_id).slice(0,12))}…（待审 ${b.pending_n}${b.source_docs&&b.source_docs[0]?' · 来源 '+esc(String(b.source_docs[0]).slice(0,16)):''}）</option>`).join('');
  }
  const bid = sel.value;
  if(!bid){ bodyEl.innerHTML = '<div style="font-size:12px;color:var(--mut);">选择批次后查看分档统计，按消歧策略重新确认（生成待审三元组）。</div>'; return; }
  const cands = await api(`/api/knowledge/v2g/candidates?limit=3000`);
  const items = (cands || []).filter(c=>c.batch_id===bid && c.status==='pending' && c.entity_type!=='关系候选');
  const rels  = (cands || []).filter(c=>c.batch_id===bid && c.status==='pending' && c.entity_type==='关系候选');
  const auto  = items.filter(c=>c.matching_status==='none').length;
  const high  = items.filter(c=>c.matching_status==='dup_high').length;
  const gray  = items.filter(c=>c.matching_status==='dup_suspect').length;
  const cell = (k, v, color)=>`<div style="flex:1;min-width:90px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;text-align:center;">
    <div style="font-size:20px;font-weight:600;color:${color};">${v}</div>
    <div style="font-size:10.5px;color:var(--mut);margin-top:2px;">${k}</div></div>`;
  const canConfirm = high+gray === 0 && items.length > 0;
  bodyEl.innerHTML = `<div style="display:flex;gap:8px;flex-wrap:wrap;">
      ${cell('实体候选', items.length, 'var(--blue-d)')}
      ${cell('无匹配·新建', auto, 'var(--grn)')}
      ${cell('高重复 dup_high', high, high?'var(--red)':'var(--mut)')}
      ${cell('疑似重复', gray, gray?'var(--amb)':'var(--mut)')}
      ${cell('关系候选', rels.length, 'var(--blue-d)')}
    </div>
    <div style="font-size:11px;color:var(--mut);margin-top:8px;">确认语义：<b>align</b>=挂靠合并（别名并入+属性补齐+来源挂接，不新增重复实体）；create=强制新建。确认后生成待审三元组，经「三元组审核」通过并 commit 落图，全程 lineage 可撤销。</div>
    <div style="margin-top:8px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
      ${canConfirm
        ? `<button class="btn sm grn" onclick="fusConfirmBatch('${esc(bid)}','align')">✅ 一键重新确认（${items.length} 条·默认对齐）</button>`
        : `<span class="tag a">存在 ${high+gray} 条重复候选未消歧——请先处理「重复消歧」队列再重新确认</span>`}
      ${high+gray > 0 ? `<button class="btn sm ghost" onclick="fusNav(Array.from(document.querySelectorAll('.fus-nav-btn')).find(b=>b.dataset.fpane==='gray'),'gray')">去处理 →</button>` : ''}
      <button class="btn sm ghost" onclick="loadCommitHistory()">📜 查看 lineage / 撤销</button>
    </div>`;
}
async function fusConfirmBatch(batchId, action){
  const r = await api('/api/knowledge/v2g/confirm', {method:'POST', body:JSON.stringify({batch_id:batchId, selected_ids:[], dup_action:action})});
  if(r.error){ toast('确认失败：'+r.error); return; }
  if(r.pending_review){ toast(`⚠ ${r.pending_review} 条 V2 候选已进入待审闸门（须先提交审核）`); }
    else toast(`✅ 已确认 ${r.confirmed||0}（生成待审三元组）/ 对齐 ${(r.aligned||[]).length} / 跳过 ${(r.skipped||[]).length}`);
    document.getElementById('fus-confirm-batch').innerHTML = '';
    loadFusion(); renderBatchConfirm();
  }
async function loadCommitHistory(){
  let r = []; try{ r = await api('/api/knowledge/commits?size=20') || []; }catch(e){ r = []; }
  const list = (r && r.items) ? r.items : (Array.isArray(r)?r:[]);
  if(!list.length){
    openPanel('📜 入图 lineage', '<div style="color:var(--mut);font-size:12px;padding:10px;">暂无 lineage 记录（尚无确认 / 合并操作）。</div><div style="margin-top:10px;text-align:right;"><button class="btn ghost" onclick="closePanel()">关闭</button></div>');
    return;
  }
  const rows = list.map(c=>`<div style="display:flex;gap:8px;align-items:center;padding:6px 8px;border:1px solid var(--line);border-radius:6px;margin-bottom:6px;">
    <b style="font-size:12px;">#${c.id}</b> <span class="tag">${esc(c.kind||'')}</span>
    <span style="font-size:11px;color:var(--mut);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc((c.message||'').slice(0,60))}</span>
    <button class="btn sm ghost" onclick="revertCommitFlow(${c.id})">↩ 撤销</button></div>`).join('');
  openPanel('📜 入图 lineage（最近确认/合并记录）', `<div style="max-height:300px;overflow:auto;">${rows}</div><div style="margin-top:10px;text-align:right;"><button class="btn ghost" onclick="closePanel()">关闭</button></div>`);
}
async function loadFusion() {
  const statusEl = document.getElementById('fusion-status');
  if(statusEl) statusEl.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const [st] = await Promise.all([
      api('/api/knowledge/fusion/status').catch(()=>({})),
      loadDuplicates(),
      loadConflicts(),
    ]);
    const tabBadge = document.getElementById('fusion-count-tab');
    const todo = (st.pending_pairs||0) + (st.conflict_n||0);
    if(tabBadge) tabBadge.textContent = todo ? `${todo} 条` : '0';
    // P0 方案 v2 / S5：左栏导航角标 + 最近待确认批次行
    const nGray = document.getElementById('fus-n-gray');
    if(nGray) nGray.textContent = st.pending_pairs||0;
    const nConflict = document.getElementById('fus-n-conflict');
    if(nConflict) nConflict.textContent = st.conflict_n||0;
    try{
      const bs = await api('/api/knowledge/v2g/batches');
      const pend = (bs||[]).filter(b=>(b.pending_n||0)>0);
      const batchTotal = pend.reduce((s,b)=>s+(b.pending_n||0),0);
      const nBatch = document.getElementById('fus-n-batch');
      if(nBatch) nBatch.textContent = batchTotal;
      const lineEl = document.getElementById('fus-batch-line');
      if(lineEl) lineEl.innerHTML = pend.length
        ? `<b>${pend.length}</b> 个批次待确认 · 最近 <b style="color:var(--blue-d);">${esc(String(pend[0].batch_id))}</b>（待审 ${pend[0].pending_n}）`
        : '✅ 无待确认批次';
    }catch(e){}
    // 初始默认显示第 0 站（若无任何面板可见）
    const _fp = ['batch','terms','gray','conflict','confirm'];
    const _anyVisible = _fp.some(p=>{const el=document.getElementById('fus-pane-'+p);return el&&el.style.display!=='none';});
    if(!_anyVisible){ const tp = window._fusTargetPane || 'batch'; fusNav(document.querySelector('.fus-nav-btn[data-fpane="'+tp+'"]') || document.querySelector('.fus-nav-btn[data-fpane="batch"]'), tp); return; }
    // 统计升为页面级维度（用户反馈：操作区优先，统计是整页信息）——渲染到顶部全链路条，左栏不再显示
    const pageStats = document.getElementById('fusion-page-stats');
    if(pageStats){
      pageStats.innerHTML = `
        <span>图谱实体 <b>${st.entity_n||0}</b></span>
        <span>自动对齐 <b style="color:var(--grn,#2f855a);">${st.auto_merged||0}</b></span>
        <span>待消歧 <b style="color:${(st.pending_pairs||0)>0?'var(--amb,#c77700)':'inherit'};">${st.pending_pairs||0}</b></span>
        <span>冲突 <b style="color:${(st.conflict_n||0)>0?'var(--red)':'inherit'};">${st.conflict_n||0}</b></span>
        <span>LLM已判 <b>${st.llm_judged||0}</b></span>`;
    }
  } catch(e) {
    if(statusEl) statusEl.innerHTML = `<div style="padding:10px;color:var(--red);font-size:12px;">融合状态加载失败：${e.message||e}</div>`;
  }
}
let rdfFmt = 'owl';       // 全局 OWL 视图当前格式（owl | ttl | shacl）
let owlNodeFmt = 'owl';   // 节点级 OWL 视图当前格式
let owlNodeId = null;     // 节点级 OWL 当前类型 id
let ontGlobalView = null; // 中栏全局内联视图：null | 'changelog' | 'owl'（2026-09-02 四轮调整）
// ── OWL/RDF 序列化（2026-09-02 四轮调整：中栏内联展示，右侧滑窗已移除）──
function ontOwlUrl(fmt, typeId){
  if(fmt==='ttl') return '/api/knowledge/ontology/export?fmt=ttl' + (typeId?('&type_id='+typeId):'');
  if(fmt==='shacl') return '/api/knowledge/ontology/shacl' + (typeId?('?type_id='+typeId):'');
  return '/api/knowledge/ontology/export' + (typeId?('?type_id='+typeId):'');
}
async function ontOwlFetch(preId, fmt, typeId){
  const pre=document.getElementById(preId);
  if(!pre) return;
  pre.textContent = '加载中…';
  try{ pre.textContent = await fetch(ontOwlUrl(fmt, typeId)).then(x=>x.text()); }
  catch(e){ pre.textContent = '加载失败：'+(e.message||e); }
}
function ontOwlChipsHtml(scope){
  const cur = scope==='global' ? rdfFmt : owlNodeFmt;
  const chip = (f,label)=>`<span class="st" id="ont-owl-fmt-${scope}-${f}" onclick="ontOwlSetFmt('${scope}','${f}')" style="cursor:pointer;padding:2px 10px;border-radius:10px;${cur===f?'background:var(--blue-l);color:var(--blue-d);font-weight:600;':''}">${label}</span>`;
  return chip('owl','RDF/XML（OWL）')+chip('ttl','Turtle')+chip('shacl','SHACL');
}
function ontOwlSetFmt(scope, fmt){
  if(scope==='global') rdfFmt = fmt; else owlNodeFmt = fmt;
  ontOwlRefresh(scope);
}
function ontOwlRefresh(scope){
  const fmt = scope==='global' ? rdfFmt : owlNodeFmt;
  ['owl','ttl','shacl'].forEach(f=>{
    const el=document.getElementById(`ont-owl-fmt-${scope}-${f}`);
    if(el){ const on=f===fmt; el.style.background=on?'var(--blue-l)':''; el.style.color=on?'var(--blue-d)':''; el.style.fontWeight=on?'600':'400'; }
  });
  const typeId = scope==='global' ? null : owlNodeId;
  ontOwlFetch(scope==='global' ? 'ont-owl-pre-global' : 'ont-owl-pre-node', fmt, typeId);
}
function ontOwlCopy(scope){
  const pre=document.getElementById(scope==='global'?'ont-owl-pre-global':'ont-owl-pre-node');
  if(!pre) return;
  navigator.clipboard.writeText(pre.textContent).then(()=>toast('已复制到剪贴板'), ()=>toast('复制失败'));
}
function ontOwlDownload(scope){
  const fmt = scope==='global' ? rdfFmt : owlNodeFmt;
  const typeId = scope==='global' ? null : owlNodeId;
  window.open(ontOwlUrl(fmt, typeId), '_blank');
  toast(fmt==='ttl'?'Turtle 已导出':(fmt==='shacl'?'SHACL 已导出':'OWL/RDF-XML 已导出'));
}
// 中栏全局内联视图：进入（隐藏 编辑/图谱/节点OWL/节点变更历史 四态，清空 toggle 高亮）
// 五轮调整：全局视图不基于节点 → 隐藏左栏（#ont-left）与中栏工具条（#ont-center-bar，含编辑/图谱/OWL toggle）
function ontEnterGlobal(view){
  ontGlobalView = view;
  const g=document.getElementById('ont-globalview');
  if(g){ g.style.display='flex'; g.innerHTML=''; }
  ['ont-main','ont-graph','ont-nodeowl','ont-nodecl'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.style.display='none';
  });
  const lf=document.getElementById('ont-left');
  if(lf) lf.style.display='none';
  const bar=document.getElementById('ont-center-bar');
  if(bar) bar.style.display='none';
  ['edit','graf','owl','cl'].forEach(x=>{
    const b=document.getElementById('ont-pane-btn-'+x);
    if(b){ b.style.background='#fff'; b.style.color='var(--mut)'; b.style.fontWeight='400'; }
  });
}
// 退出全局视图：恢复左栏与工具条（display 置空回落到内联样式属性），toggle 高亮按当前模式复位
function ontRestoreWorkspace(){
  const lf=document.getElementById('ont-left');
  if(lf) lf.style.display='';
  const bar=document.getElementById('ont-center-bar');
  if(bar) bar.style.display='';
  ['edit','graf','owl','cl'].forEach(x=>{
    const b=document.getElementById('ont-pane-btn-'+x);
    if(b){ const on=x===ontPaneMode; b.style.background=on?'var(--blue-l)':'#fff'; b.style.color=on?'var(--blue-d)':'var(--mut)'; b.style.fontWeight=on?'600':'400'; }
  });
  // 五轮调整：顶栏高亮同步回当前维度 Tab（OWL/变更历史为全局态，节点视图回落到维度 Tab）
  document.querySelectorAll('#ont-subtab-row [data-tabgrp=ont]').forEach(x=>x.classList.remove('on'));
  const _tt=document.querySelector('#ont-subtab-row [data-tabgrp="ont"][onclick*="\''+ontView+'\'"]');
  if(_tt) _tt.classList.add('on');
}
function ontLeaveGlobal(){
  if(!ontGlobalView) return;
  ontGlobalView = null;
  const g=document.getElementById('ont-globalview');
  if(g){ g.style.display='none'; g.innerHTML=''; }
  ontRestoreWorkspace();
}
// 六轮调整：「✕ 返回」按钮及 ontExitGlobal 已移除——退出全局视图统一走顶栏维度 Tab（switchOntTab 内 ontLeaveGlobal）
// 顶栏全局入口（🧬 OWL / 📜 变更历史）：选择组件与类/属性 Tab 一致（data-tabgrp=ont，高亮互斥）
function ontTopGlobalTab(el, view){
  el.parentNode.querySelectorAll('[data-tabgrp=ont]').forEach(x=>x.classList.remove('on'));
  el.classList.add('on');
  if(view==='owl') openOntOwl(); else openOntChangeLog();
}
// 🧬 OWL 全局视图（顶栏入口）：全量本体序列化，内联展示
function openOntOwl(){
  ontEnterGlobal('owl');
  const gv=document.getElementById('ont-globalview');
  if(!gv) return;
  gv.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap;">
      <b style="font-size:12.5px;color:var(--blue-d);">🧬 OWL / RDF 序列化</b>
      <span class="tag">全局本体 · 全量 Schema</span>
      ${ontOwlChipsHtml('global')}
      <span style="flex:1"></span>
      <button class="btn sm" onclick="ontOwlCopy('global')">📋 复制</button>
      <button class="btn sm ghost" onclick="ontOwlDownload('global')">⬇ 下载</button>
    </div>
    <div style="flex:none;font-size:11px;color:var(--mut);background:var(--blue-l);border-bottom:1px solid var(--line);padding:4px 14px;">
      <b style="color:var(--blue-d);">RDF 与 OWL</b>：RDF 是 W3C 底层三元组数据模型，OWL 是构建其上的本体语言；RDF/XML 与 Turtle 是同一套 OWL 语义的两种等价序列化，SHACL 将约束表达为可机器校验的数据形状。
    </div>
    <pre id="ont-owl-pre-global" style="flex:1;min-height:0;margin:0;background:#1e1e1e;color:#d4d4d4;padding:14px;font-size:11.5px;line-height:1.55;overflow:auto;white-space:pre-wrap;word-break:break-all;"></pre>`;
  ontOwlRefresh('global');
}
async function loadOntology() {
  const [types, binding, ver] = await Promise.all([
    api('/api/knowledge/ontology'),
    api('/api/knowledge/ontology/binding').catch(()=>({bindings:[]})),
    api('/api/knowledge/ontology/version').catch(()=>null),
  ]);
  // 版本徽章（2026-09-08 版本语义收敛）：显示消费基线（active released）SemVer；编辑态只留变更记录不升版本号，
  // 有未发布变更时追加橙点提示（悬停见说明），发布后消失
  const vbadge = document.getElementById('ont-version-badge');
  if(vbadge && ver && ver.current){
    const lbl = ver.current.version_label || 'v1.0.0';
    vbadge.innerHTML = '🧬 ' + esc(lbl) + (ver.dirty ? ' <span style="color:var(--amb,#b7791f);" title="有未发布变更">•未发布</span>' : '');
    vbadge.title = '本体版本（消费基线 SemVer）。编辑只留变更记录、版本号仅在发布时升级' + (ver.dirty ? `；当前有 ${ver.pending} 条未发布变更，到「版本历史 → 发布当前数据」生成新版本` : '');
  }
  // 解析 JSON 字符串字段（后端返回的 constraints/properties 可能是 JSON 字符串）
  types.forEach(t=>{
    if(typeof t.constraints === 'string') { try{ t.constraints = JSON.parse(t.constraints||'{}'); }catch(e){ t.constraints = {}; } }
    if(typeof t.properties === 'string') { try{ t.properties = JSON.parse(t.properties||'{}'); }catch(e){ t.properties = {}; } }
  });
  ontData.types = types;
  ontData.binding = binding.bindings||[];
  renderOntTree();
  ontRelayout();  // 图形模式画布：首次加载按默认分层布局排布（复用图谱页优化算法），内部会 renderOntGraph
  oiBindInteractions(document.getElementById('ont-graph-body'));
  // 保持当前选中（新增/编辑后仍聚焦该类型或单条关系）；无选中则默认第一个实体类型
  // 2026-09-02 P0-4：重载数据不退出中栏当前视图（keepView=ontPaneMode!=='edit'，四轮调整含 owl 态）
  const _gv = ontPaneMode!=='edit';
  if(ontSelected && ontSelected.kind==='type' && types.find(t=>t.name===ontSelected.name)) {
    selectOntType(ontSelected.name, _gv);
  } else {
    const first = types.find(t=>t.type_kind===_kindOfView());
    if(first) selectOntType(first.name, _gv);
  }
}
// P0-2：当前实体维度对应的 type_kind
function _kindOfView(){ return ontView==='props' ? 'relation' : (ontView==='dprops' ? 'attribute' : 'entity'); }// ⚙ 本体设置（P2-6 Project Settings）：命名空间 / IRI 生成策略 / 默认前缀 → PUT /ontology/meta
async function openOntSettings(){
  openPanel('⚙ 本体设置', '<div class="loading">加载中…</div>');
  try{
    const m = await api('/api/knowledge/ontology/meta');
    const row = (label, tip, ctrl) => `<div style="margin-bottom:14px;"><div style="font-size:12px;font-weight:600;color:var(--blue-d);margin-bottom:5px;">${label} <span style="font-weight:400;color:var(--mut);font-size:11px;">— ${tip}</span></div>${ctrl}</div>`;
    openPanel('⚙ 本体设置（Project Settings）', `
      <div style="max-width:520px;">
        ${row('命名空间 Namespace IRI','新建实体 IRI 的前缀',
          `<input id="ont-set-ns" value="${esc(m.namespace||'')}" placeholder="http://www.example.org/ontology#" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;font-family:ui-monospace,Consolas,monospace;">`)}
        ${row('IRI 生成策略','hash-name=命名空间+实体名（推荐，可读）；uuid=随机 UUID；user-supplied=新建时人工填写',
          `<select id="ont-set-strategy" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;">
            <option value="hash-name"${m.iri_strategy==='hash-name'?' selected':''}>hash-name — 命名空间 + 实体名（可读，Protégé 默认）</option>
            <option value="uuid"${m.iri_strategy==='uuid'?' selected':''}>uuid — 随机 UUID（绝对唯一）</option>
            <option value="user-supplied"${m.iri_strategy==='user-supplied'?' selected':''}>user-supplied — 新建时人工填写</option>
          </select>`)}
        ${row('默认前缀 prefix','RDF/XML / Turtle 导出时的 @prefix 名（如 mbse）',
          `<input id="ont-set-prefix" value="${esc(m.default_prefix||'')}" placeholder="留空则与命名空间保持一致" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;">`)}
        <div style="font-size:11px;color:var(--mut);line-height:1.7;background:var(--blue-l);border:1px solid var(--line);border-radius:6px;padding:8px 10px;">
          策略仅影响<b>后续新建</b>实体的 IRI；已有实体 IRI 不变。实体改名时，自动生成的 IRI 跟随新名，人工改过的 IRI 保留。
        </div>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
          <button class="btn sm ghost" onclick="closePanel()">取消</button>
          <button class="btn sm" onclick="saveOntSettings()">💾 保存</button>
        </div>
      </div>`);
  }catch(e){ openPanel('⚙ 本体设置', `<div style="color:var(--red);font-size:12px;padding:10px;">加载失败：${esc(e.message||e)}</div>`); }
}
async function saveOntSettings(){
  const body = {
    namespace: (document.getElementById('ont-set-ns')||{}).value||'',
    iri_strategy: (document.getElementById('ont-set-strategy')||{}).value||'hash-name',
    default_prefix: (document.getElementById('ont-set-prefix')||{}).value||'',
  };
  try{
    const r = await api('/api/knowledge/ontology/meta', {method:'PUT', body:JSON.stringify(body)});
    if(r && r.error){ toast('保存失败：'+r.error); return; }
    toast('✅ 本体设置已保存（影响后续新建实体 IRI）');
    closePanel();
  }catch(e){ toast('保存失败：'+(e.message||e)); }
}
// 🕘 本体版本历史（右侧弹窗）：版本链 SemVer + 发布稳定基线
let _ontVerPerm = false;   // 当前用户是否有 kb_ontology:edit（决定发布按钮显隐）
async function openOntVersionHistory(){
  openPanel('🧬 本体版本历史', '<div class="loading">加载中…</div>');
  try{
    const [v, me, logs] = await Promise.all([
      api('/api/knowledge/ontology/version'),
      api('/api/users/me').catch(()=>({permissions:{}})),
      api('/api/knowledge/ontology/changelog').catch(()=>[]),
    ]);
    _ontVerPerm = ((me.permissions||{})['kb_ontology']||[]).includes('edit');
    const rows = v.history || [];
    const cur = v.current || {};
    const _typeLbl = {add:'新增', update:'更新', delete:'删除', apply:'蓝图应用', import:'OWL 导入'};
    const _typeCls = {add:'ok', update:'b', delete:'r', apply:'g', import:'w'};
    const _stBadge = r => r.status==='released'
      ? '<span class="st ok" title="已发布（稳定基线）">✅ 已发布</span>'
      : '<span class="st w" title="草稿（未发布）">🟡 草稿</span>';
    // 2026-09-02 P1：兼容性徽章（消费策略：兼容自动跟随 / 破坏性需迁移）+ diff 摘要徽章
    const _compatBadge = r => {
      if(r.status!=='released') return '';
      return r.compatible
        ? '<span class="st g" title="与上一已发布版本兼容（以新增为主），消费侧自动跟随">✓ 兼容·自动跟随</span>'
        : '<span class="st r" title="破坏性变更（删除/改名/域值域变更），消费侧需确认迁移">⚠ 破坏性·需迁移</span>';
    };
    const _diffBadge = r => {
      const d = r.diff;
      if(!d) return '';
      const p = [];
      if(d.added && d.added.length) p.push(`<span class="st ok" title="新增：${esc(d.added.slice(0,8).join('、'))}${d.added.length>8?' …':''}">+${d.added.length} 新增</span>`);
      if(d.removed && d.removed.length) p.push(`<span class="st r" title="删除：${esc(d.removed.slice(0,8).join('、'))}${d.removed.length>8?' …':''}">−${d.removed.length} 删除</span>`);
      if(d.dom_changed) p.push(`<span class="st b">${d.dom_changed} 域值域变更</span>`);
      return p.join(' ');
    };
    // 2026-09-02 发布语义对齐：版本历史 = 已发布版本（发布产物）+ 草稿变更记录（自动日志，折叠）
    const releasedRows = rows.filter(r=>r.status==='released');
    const draftRows = rows.filter(r=>r.status!=='released');
    const releasedList = releasedRows.map(r=>`
      <div style="border:1px solid ${r.active?'var(--grn,#2f855a)':'var(--line)'};border-radius:8px;padding:8px 10px;margin:6px 0;background:${r.active?'#f0faf4':'#fff'};">
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <b style="color:var(--blue-d);min-width:64px;">${esc(r.version_label)}</b>
          <span class="st ok" title="已发布（稳定基线）">✅ 已发布</span>
          ${_compatBadge(r)}
          ${r.active?'<span class="tag" style="border:1px solid var(--grn,#2f855a);color:var(--grn,#2f855a);" title="当前消费基线：AI 语义注入/图谱/导出以该版本为准">📌 消费基线（启用中）</span>':''}
          <span style="flex:1;"></span>
          <button class="btn sm ghost" style="padding:1px 8px;font-size:11px;" onclick="viewOntVersionSnapshot(${r.id})" title="查看该版本快照的完整本体数据（只读，不可变）">📋 数据</button>
          ${!r.active && _ontVerPerm
            ? `<button class="btn sm" style="padding:1px 8px;font-size:11px;" onclick="activateOntVersion(${r.id})" title="启用该版本为消费基线（快照不删，版本不可变）">▶ 启用</button>`
            : ''}
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:3px;">
          <div style="color:var(--mut);font-size:11px;flex:1;min-width:200px;">${esc(r.summary||'')}</div>
          ${_diffBadge(r)}
        </div>
        <div style="color:var(--mut);font-size:11px;margin-top:1px;">🚀 ${esc((r.released_at||r.created_at||'').slice(0,16))} · ${esc(r.released_by||r.operator||'')}${r.snapshot_count?` · 快照 ${r.snapshot_count} 类型`:''}</div>
      </div>`).join('');
    // 2026-09-08 版本语义收敛：编辑态不再产生草稿版本行，「编辑留痕」区改读 ontology_change_logs
    //（比旧 draft 行更细：每条含 before/after 全行 diff）；存量草稿行（旧版本语义产物）保留折叠展示
    const _logAct = {add:['新增','ok'], update:['更新','b'], delete:['删除','r']};
    const logRows = (Array.isArray(logs)?logs:[]).slice(0,30);
    const logsHtml = logRows.length
      ? `<details style="margin-top:12px;border-top:1px dashed var(--line);padding-top:8px;" open>
          <summary style="cursor:pointer;font-size:11.5px;color:var(--mut);outline:none;user-select:none;">📝 编辑留痕（未发布 · 每次编辑自动记录，发布时汇总升版本 · 最近 ${logRows.length} 条）<span style="float:right;font-size:10px;">点击展开/收起</span></summary>
          <div style="margin-top:6px;max-height:240px;overflow:auto;">
            ${logRows.map(l=>{
              const a=l.after||{}, b=l.before||{};
              const nm=a.name||b.name||('#'+(l.type_id??''));
              const[_la,_lc]=_logAct[l.action]||[l.action,'g'];
              return `<div style="display:flex;gap:6px;align-items:center;padding:3px 2px;font-size:11px;color:var(--mut);">
              <span class="st ${_lc}" style="font-size:9.5px;padding:0 5px;">${_la}</span>
              <b style="color:var(--blue-d);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px;" title="${esc(nm)}">${esc(nm)}</b>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(ontChangeSummary({action:l.action,before:b,after:a}))}">${esc(ontChangeSummary({action:l.action,before:b,after:a}))}</span>
              <span style="color:#aaa;flex:none;">${esc((l.created_at||'').slice(0,16))} · ${esc(l.operator||'')}</span>
            </div>`;}).join('')}
          </div>
        </details>`
      : '';
    const draftsHtml = draftRows.length
      ? `<details style="margin-top:8px;border-top:1px dashed var(--line);padding-top:8px;">
          <summary style="cursor:pointer;font-size:11.5px;color:var(--mut);outline:none;user-select:none;">🗂 存量草稿版本行（旧版自动日志 · ${draftRows.length} 条，新版本语义已不再产生）<span style="float:right;font-size:10px;">点击展开/收起</span></summary>
          <div style="margin-top:6px;max-height:200px;overflow:auto;">
            ${draftRows.map(r=>`<div style="display:flex;gap:6px;align-items:center;padding:3px 2px;font-size:11px;color:var(--mut);">
              <span style="min-width:52px;color:#888;">${esc(r.version_label)}</span>
              <span class="st ${_typeCls[r.change_type]||'g'}" style="font-size:9.5px;padding:0 5px;">${_typeLbl[r.change_type]||esc(r.change_type)}</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(r.summary||'')}">${esc(r.summary||'')}</span>
              <span style="color:#aaa;flex:none;">${esc((r.created_at||'').slice(0,16))} · ${esc(r.operator||'')}</span>
            </div>`).join('')}
          </div>
        </details>`
      : '';
    const _activeRel = releasedRows.find(r=>r.active) || releasedRows[0] || {};
    openPanel('🧬 本体版本历史',
      `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;">
        <b style="color:var(--blue-d);font-size:13px;">${releasedRows.length ? '已发布 '+esc(_activeRel.version_label||'') : '尚未发布'}</b>
        ${v.dirty?`<span class="st w" title="最近 ${v.pending} 条编辑留痕晚于当前基线发布时间——编辑只记录不升版本，点「发布当前数据」生成新版本">🟡 ${v.pending} 条未发布变更</span>`:''}
        ${_ontVerPerm
          ? `<button class="btn sm" style="padding:3px 12px;font-size:12px;" onclick="publishOntology()" title="将当前编辑的本体数据发布为新版本（按变更 diff 自动定 SemVer：破坏性=major/新增=minor，生成不可变快照并设为消费基线）">🚀 发布当前数据</button>
             <button class="btn sm ghost" style="padding:3px 10px;font-size:12px;" onclick="openOntMigrations()" title="本体变更→实例迁移计划列表（发布时自动生成，支持预演与确认执行；也可手动扫描历史悬空实例）">🧩 实例迁移</button>`
          : ''}
        <span style="flex:1;"></span>
        <span style="font-size:10.5px;color:var(--mut);">📌 消费基线 = AI 语义注入 / 图谱 / 导出使用的版本（可随时启用切换）</span>
      </div>
      <div style="font-size:11px;color:var(--mut);line-height:1.6;margin-bottom:6px;">版本规则（2026-09-08 收敛）：编辑只留「编辑留痕」，<b>版本号仅在发布时升级</b>——按与上一发布版的 diff 自动定 SemVer：<b>major</b>=破坏性（删除/改名/改域值域）→ 启用需迁移确认 · <b>minor</b>=新增类型/属性（自动跟随）。</div>
      ${releasedList || '<div style="padding:12px;color:var(--mut);font-size:12px;text-align:center;">暂无已发布版本——编辑本体后点击上方「🚀 发布当前数据」生成第一个版本</div>'}
      ${logsHtml}
      ${draftsHtml}`);
  }catch(e){ openPanel('🧬 本体版本历史', `<div style="color:var(--red);padding:10px;font-size:12px;">加载失败：${esc(e.message)}</div>`); }
}
// 2026-09-02 发布语义对齐：主动发布当前编辑数据为新版本
async function publishOntology(){
  // 发布说明输入（可选；取消=不发布）。填写的说明展示在版本历史并用于变更汇总，留空按 diff 自动生成摘要
  const note = await promptDialog({
    title: '🚀 发布当前数据',
    message: '填写本次发布说明（推荐）——展示在版本历史并用于变更汇总；留空则自动按「新增/删除/域值域变更」生成摘要。\n\n发布后：① 生成不可变快照（版本库新增一条，可随时「▶ 启用」切换）② 兼容性判定（删除/改名/改域值域 = 破坏性）。当前数据与已发布版本无差异将被拒绝。',
    multiline: true, rows: 4,
    placeholder: '例如：新增「XX」类型支撑 YY 场景；调整「ZZ」关系定义域…'
  });
  if(note === null) return;   // 取消发布
  try{
    const r = await api('/api/knowledge/ontology/version/publish', {method:'POST', body:JSON.stringify({summary: String(note||'').trim()})});
    if(r.error){ toast('发布失败：'+r.error); return; }
    const v = r.version || {};
    const d = r.diff||{};
    const parts = [];
    if(d.added && d.added.length) parts.push(`+${d.added.length} 新增`);
    if(d.removed && d.removed.length) parts.push(`−${d.removed.length} 删除`);
    if(d.dom_changed) parts.push(`${d.dom_changed} 域值域变更`);
    toast(`✅ 已发布 ${v.version_label}${parts.length?'（'+parts.join(' · ')+'）':''}${r.snapshot&&!r.snapshot.compatible?' ⚠️ 破坏性需迁移确认':''}`);
    loadOntology();
    openOntVersionHistory();
    // 2026-09-14 发布挂钩：本批变更生成了实例迁移计划 → 直接弹出计划面板（dry-run → 确认执行）
    if(r.migration_plan_id && r.migration_plan && r.migration_plan.ops > 0){
      openOntMigrationPlan(r.migration_plan_id);
    }
  }catch(e){ toast('发布失败：'+e.message); }
}
// ── 2026-09-14 本体变更 → 实例迁移计划（L2 链路：列表 / 详情 / dry-run / apply）──
const _migOpLbl = {migrate_instances:'按名迁移', deprecate_instances:'实例弃用', drop_prop_key:'清理死键',
  convert_prop_values:'值类型转换', flag_violations:'违例清单', flag_dangling:'悬空扫描'};
async function openOntMigrations(){
  openPanel('🧩 实例迁移计划', '<div class="loading">加载中…</div>');
  try{
    const r = await api('/api/knowledge/ontology/migrations');
    if(r.error){ openPanel('🧩 实例迁移计划', `<div style="color:var(--red);padding:10px;font-size:12px;">${esc(r.error)}</div>`); return; }
    if(!(r.plans||[]).length){
      openPanel('🧩 实例迁移计划', `<div style="padding:12px;color:var(--mut);font-size:12px;text-align:center;">暂无迁移计划——发布本体版本（含删除/约束收紧）时自动生成；也可 <button class="btn sm" onclick="ontMigBuild()">手动扫描补建</button></div>`);
      return;
    }
    const _html = r.plans.map(p=>{
      const ops = p.ops.map(o=>{
        const st = {pending:['w','待处理'],dry_run:['b','已预演'],applied:['g','已执行'],failed:['r','失败'],dismissed:['','已忽略']}[o.status]||['',''];
        return `<div style="display:flex;gap:6px;align-items:baseline;padding:4px 2px;border-bottom:1px dashed var(--line);font-size:11.5px;flex-wrap:wrap;">
          <span class="st ${st[0]}" style="font-size:9.5px;padding:0 5px;">${st[1]}</span>
          <b style="min-width:70px;">${_migOpLbl[o.op_type]||esc(o.op_type)}</b>
          <span style="color:var(--blue-d);">${esc(o.target)}</span>
          <span style="color:var(--mut);">影响 ${o.affected} 条</span>
          <span style="flex:1;min-width:120px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;" title="${esc(o.note||'')}">${esc(o.note||esc((o.payload||{}).reason||''))}</span>
        </div>`;
      }).join('');
      const act = p.pending
        ? `<button class="btn sm" onclick="ontMigDryRun(${p.plan_id})">🔍 预演</button>
           <button class="btn sm" onclick="ontMigApply(${p.plan_id})">▶ 确认执行（${p.pending} op / 影响 ${p.affected_total} 条）</button>`
        : `<span style="font-size:11px;color:var(--mut);">✅ 全部已执行</span>`;
      return `<div style="border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap;">
          <b style="font-size:12.5px;">计划 #${p.plan_id}</b>
          ${p.version_id?`<span style="font-size:10.5px;color:var(--mut);">关联版本 #${p.version_id}</span>`:'<span style="font-size:10.5px;color:var(--mut);">手动补建</span>'}
          <span style="font-size:10.5px;color:var(--mut);">${esc((p.created_at||'').slice(0,16))} · ${esc(p.created_by||'')}</span>
          <span style="flex:1;"></span>${act}
        </div>${ops}</div>`;
    }).join('');
    openPanel('🧩 实例迁移计划', `<div style="font-size:11px;color:var(--mut);margin-bottom:8px;">执行语义：迁移/弃用/清键/转换为幂等 SQL（单事务，失败整体回滚）；违例清单只存档不自动改数据——改哪条、删还是放宽约束是工程判断，请在图谱中人工裁决。</div>${_html}`);
  }catch(e){ openPanel('🧩 实例迁移计划', `<div style="color:var(--red);padding:10px;font-size:12px;">加载失败：${esc(e.message)}</div>`); }
}
async function openOntMigrationPlan(planId){
  await openOntMigrations();
}
async function ontMigDryRun(planId){
  try{
    const r = await api(`/api/knowledge/ontology/migrations/${planId}/dry-run`, {method:'POST'});
    if(r.error){ toast('预演失败：'+r.error); return; }
    toast(`🔍 预演完成：${r.ops} op / 影响 ${r.affected_total} 条（未写任何数据）`);
    openOntMigrations();
  }catch(e){ toast('预演失败：'+e.message); }
}
async function ontMigApply(planId){
  if(!(await confirmDialog('确认执行该迁移计划？将在单事务内回写实例数据（幂等 SQL，失败整体回滚）；违例清单类 op 仅存档不改数据。', {title:'执行实例迁移'}))) return;
  try{
    const r = await api(`/api/knowledge/ontology/migrations/${planId}/apply`, {method:'POST'});
    if(r.error){ toast('执行失败：'+r.error); return; }
    toast(`✅ 迁移完成：${(r.applied||[]).length} op 已执行`);
    openOntMigrations();
  }catch(e){ toast('执行失败：'+e.message); }
}
async function ontMigBuild(){
  try{
    const r = await api('/api/knowledge/ontology/migrations/build', {method:'POST'});
    if(r.error){ toast('生成失败：'+r.error); return; }
    if(!r.plan_id){ toast('✓ 无待迁移实例影响（无未处理留痕、无悬空实例）'); return; }
    toast(`已生成迁移计划 #${r.plan_id}（${r.ops} op / 影响 ${r.affected} 条）`);
    openOntMigrations();
  }catch(e){ toast('生成失败：'+e.message); }
}
// 2026-09-02 P1：回滚（切换消费基线到指定已发布版本，快照不删）
async function activateOntVersion(vid){
  try{
    if(!(await confirmDialog('将消费基线切换到该版本？（回滚 = active 指针切换，快照不删除；AI 语义注入/图谱/导出立即以该版本为准）', {title:'切换消费基线'}))) return;
    const r = await api('/api/knowledge/ontology/version/activate', {method:'POST', body:JSON.stringify({version_id: vid})});
    if(r.error){ toast('切换失败：'+r.error); return; }
    toast(r.already_active ? '该版本已是消费基线' : `✅ 消费基线已切换到 ${r.version.version_label}`);
    openOntVersionHistory();
  }catch(e){ toast('切换失败：'+e.message); }
}
// 2026-09-02 P1：查看指定版本的完整快照数据（只读面板：实体继承树 / 关系域值域 / 属性）
async function viewOntVersionSnapshot(vid){
  openPanel('📋 版本数据', '<div class="loading">加载中…</div>');
  try{
    const r = await api('/api/knowledge/ontology/version/'+vid+'/snapshot');
    if(r.error){ openPanel('📋 版本数据', `<div style="color:var(--red);padding:10px;font-size:12px;">${esc(r.error)}</div>`); return; }
    if(r.empty){  // 快照治理上线前的存量发布：无快照
      openPanel('📋 版本数据 · '+r.version.version_label,
        `<div style="font-size:11.5px;color:var(--mut);line-height:1.8;padding:12px 4px;">${esc(r.note||'该版本无快照数据')}<br><br>👉 到「本体模型」页修改后<b>重新发布</b>当前版本，即可生成含快照的新版本查看数据。</div>`);
      return;
    }
    const types = r.types || [];
    const byKind = {entity:[], relation:[], attribute:[]};
    types.forEach(t=>{ if(byKind[t.type_kind]) byKind[t.type_kind].push(t); });
    // 实体继承树
    const byId = {}; types.forEach(t=>byId[t.id]=t);
    const children = {};
    types.forEach(t=>{ if(t.parent_id && byId[t.parent_id]) (children[t.parent_id]=children[t.parent_id]||[]).push(t); });
    const roots = types.filter(t=>t.type_kind==='entity' && !(t.parent_id && byId[t.parent_id]));
    const walk = (t, d) => `<div style="padding:2px 0 2px ${d*14}px;font-size:12px;${d?'color:var(--mut);':''}" title="${esc(t.description||'')}">${d?'⤷ ':''}${esc(t.icon||'🛰')} ${esc(t.name)}</div>` + (children[t.id]||[]).map(c=>walk(c,d+1)).join('');
    const entsHtml = roots.map(t=>walk(t,0)).join('') || '<div style="color:var(--mut);font-size:12px;">无</div>';
    // 关系：域 → 值域
    const relsHtml = byKind.relation.map(t=>{
      let av = {};
      try{ av = ((typeof t.constraints==='string'?JSON.parse(t.constraints||'{}'):t.constraints)||{}).allowed_values||{}; }catch(e){}
      const src = Array.isArray(av.src)?av.src.join('/'):(av.src||'—');
      const tgt = Array.isArray(av.tgt)?av.tgt.join('/'):(av.tgt||'—');
      return `<div style="padding:2px 0;font-size:12px;"><span style="color:var(--mut);">◇</span> <b>${esc(t.name)}</b> <span style="color:var(--mut);font-size:11px;">${esc(String(src))} → ${esc(String(tgt))}</span></div>`;
    }).join('') || '<div style="color:var(--mut);font-size:12px;">无</div>';
    // 属性
    const attrsHtml = byKind.attribute.map(t=>{
      let xsd = '';
      try{ xsd = ((typeof t.constraints==='string'?JSON.parse(t.constraints||'{}'):t.constraints)||{}).xsd_type||''; }catch(e){}
      return `<div style="padding:2px 0;font-size:12px;"><span style="color:var(--mut);">◆</span> ${esc(t.name)}${xsd?` <span style="color:var(--mut);font-size:11px;">${esc(xsd)}</span>`:''}</div>`;
    }).join('') || '<div style="color:var(--mut);font-size:12px;">无</div>';
    openPanel('📋 版本数据 · '+r.version.version_label,
      `<div style="font-size:11.5px;color:var(--mut);margin-bottom:6px;line-height:1.6;">📌 <b>${esc(r.version.version_label)}</b> · 已发布快照（不可变）· 实体 ${r.counts.entity} / 关系 ${r.counts.relation} / 属性 ${r.counts.attribute}${r.version.active?' · <b style="color:var(--grn,#2f855a);">当前消费基线</b>':''}</div>
      <div style="font-size:12px;font-weight:600;color:var(--blue-d);margin:8px 0 4px;">🧬 实体类型（继承层级）</div>
      <div style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;max-height:220px;overflow:auto;">${entsHtml}</div>
      <div style="font-size:12px;font-weight:600;color:var(--blue-d);margin:10px 0 4px;">🔗 关系类型（域 → 值域）</div>
      <div style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;max-height:200px;overflow:auto;">${relsHtml}</div>
      <div style="font-size:12px;font-weight:600;color:var(--blue-d);margin:10px 0 4px;">📋 属性类型</div>
      <div style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;max-height:160px;overflow:auto;">${attrsHtml}</div>`);
  }catch(e){ openPanel('📋 版本数据', `<div style="color:var(--red);padding:10px;font-size:12px;">加载失败：${esc(e.message)}</div>`); }
}
const TYPE_ICONS = {entity:'🛰', relation:'🔗', attribute:'🏷'};
let ontSearchHits = new Set();  // 搜索命中（P2-3 图谱联动高亮）
function ontApplyFilter(){
  ontSyncGraphHl();
  renderOntTree();
  renderOntGraph();
}
function ontSyncGraphHl(){
  ontSearchHits = new Set();
  const q=((document.getElementById('ont-filter')||{}).value||'').trim();
  if(!q) return;
  (ontData.types||[]).forEach(t=>{
    if(t.type_kind!=='entity') return;
    let p=''; try{ p=Object.keys(JSON.parse(t.properties||'{}')||{}).join(' '); }catch(e){}
    if(((t.name||'')+' '+(t.description||'')+' '+p.toLowerCase()).includes(q.toLowerCase())) ontSearchHits.add(t.name);
  });
}
let __ontHoverBound=false;
function ontToggleLeft(){
  const L=document.getElementById('ont-left'), hdr=document.getElementById('ont-left-hdr'),
        lst=document.getElementById('ont-types-list'), bt=document.getElementById('ont-left-collapse'),
        bb=document.getElementById('ont-batch-bar'), gs=document.getElementById('ont-graf-side');
  const collapsed = (L.style.width||'')==='52px';
  // 2026-08-31：展开时图例区仅在中栏图谱模式显示——切回编辑模式后展开左栏不应冒出图例
  if(collapsed){ L.style.width='236px'; if(hdr)hdr.style.display=''; if(lst)lst.style.display=''; if(gs)gs.style.display = (typeof ontPaneMode!=='undefined' && ontPaneMode==='graf') ? '' : 'none'; bt.innerHTML='«'; }
  else { L.style.width='52px'; if(hdr)hdr.style.display='none'; if(lst)lst.style.display='none'; if(gs)gs.style.display='none'; if(bb)bb.style.display='none'; bt.innerHTML='»'; }
}
function ontRelationRows() {
  const rows = [];
  (ontData.types||[]).filter(t=>t.type_kind==='relation').forEach(t=>{
    const av = (t.constraints && t.constraints.allowed_values) || {};
    const srcList = Array.isArray(av.src) ? av.src : (av.src ? [av.src] : []);
    const tgtList = Array.isArray(av.tgt) ? av.tgt : (av.tgt ? [av.tgt] : []);
    const srcs = srcList.length ? srcList : ['?'];
    const tgts = tgtList.length ? tgtList : ['?'];
    srcs.forEach(s=>{ tgts.forEach(g=>{
      rows.push({rel:t.name, src:s, tgt:g, color:ontColorOf(t), typeId:t.id});
    });});
  });
  return rows;
}
function ontTypeTree(kind){
  const list = (ontData.types||[]).filter(t=>t.type_kind===kind);
  if(!list.length) return [];
  const byName = {}; list.forEach(t=>byName[t.name]=t);
  const children = {};
  list.forEach(t=>{
    if(t.parent_id){
      const p = ontData.types.find(x=>x.id===t.parent_id);
      if(p && byName[p.name]) (children[p.name]=children[p.name]||[]).push(t.name);
    }
  });
  const roots = list.filter(t=>{
    if(!t.parent_id) return true;
    const p = ontData.types.find(x=>x.id===t.parent_id);
    return !(p && byName[p.name]);
  }).map(t=>t.name).sort((a,b)=>a.localeCompare(b,'zh'));
  Object.keys(children).forEach(k=>children[k].sort((a,b)=>a.localeCompare(b,'zh')));
  const out = [];
  const walk = (name, depth)=>{
    out.push({t: byName[name], depth});
    (children[name]||[]).forEach(c=>walk(c, depth+1));
  };
  roots.forEach(r=>walk(r,0));
  return out;
}
function selectOntType(name, keepView) {
  const t = (ontData.types||[]).find(x=>x.name===name);
  if(!t) return;
  // 2026-09-02 P0-2/P0-4 重构：
  // keepView=true（图谱/联动语境）→ 维持当前实体维度，仅更新选中（中栏投影跟随左栏选中）
  // keepView=false（编辑定位语境）→ 切换实体维度到该类型所属 Tab（顶部 Tab 高亮同步），中栏模式保持不变
  // 2026-09-02 三轮调整：切换左栏选中不重置中栏模式——编辑/图谱保持当前状态，
  // 中栏基于新选中重投影（编辑=字段表单 / 图谱=该节点邻域）。显式切回编辑仅由 ontGotoEdit 触发
  if(!keepView){
    // P1-5：individuals Tab 下选择实体类型 → 保持个体维度（中栏投影=该类型个体列表）
    const want = t.type_kind==='relation' ? 'props' : (t.type_kind==='attribute' ? 'dprops' : (ontView==='individuals' ? 'individuals' : 'classes'));
    if(ontView!==want){
      ontView = want;
      document.querySelectorAll('#ont-subtab-row [data-tabgrp=ont]').forEach(x=>x.classList.remove('on'));
      const _tab = document.querySelector('#ont-subtab-row [data-tabgrp="ont"][onclick*="\''+want+'\'"]');
      if(_tab) _tab.classList.add('on');
      const _dim = document.getElementById('ont-left-dim');
      if(_dim) _dim.textContent = want==='props' ? '对象属性（域 → 值域）' : (want==='dprops' ? '数据属性（XSD 类型）' : (want==='individuals' ? '实体类型（选择查看个体）' : '类层次（subClassOf）'));
    }
    ontSyncViewPanes();
  }
  ontSelected = {kind:'type', id:t.id, name};
  // 五轮调整：左栏选中变化 → 退出全局内联视图（变更历史/OWL），恢复左栏与工具条
  ontLeaveGlobal();
  // 2026-09-02 聚焦下钻：图谱模式选中实体即聚焦其 1-hop 邻域（不依赖 keepView，三轮调整）
  if(ontPaneMode==='graf' && t.type_kind==='entity') ontFocus = name;
  renderOntTree();
  renderOntGraph();
  if(ontPaneMode==='graf'){
    renderOntSideDetail();
  } else if(ontPaneMode==='owl'){
    // 四轮调整：owl 态下切换选中 → 重渲染该节点的 OWL 视图
    owlNodeId = t.id;
    renderOntSideDetail();
    renderOntNodeOwl();
  } else if(ontPaneMode==='cl'){
    // 五轮调整：cl 态下切换选中 → 重渲染该节点的变更历史
    renderOntSide();
    renderOntNodeChangeLog();
  } else {
    renderOntDesc();
    renderOntSide();
  }
  ontFocusBreadcrumb();
}
// 图谱中点选边 → 图谱内查看该关系类型详情（2026-08-31：不退出图谱视图）
function selectOntEdge(idx) {
  const e = ogRels && ogRels[idx];
  if(!e) return;
  selectOntType(e.rel, true);
}
async function deleteOntType(id) {
  if(!(await confirmDialog('确认删除该本体类型？相关图谱实例将不再受此类型约束。'))) return;
  api(`/api/knowledge/ontology/types/${id}`, {method:'DELETE'}).then(r=>{
    if(r && r.error) { toast('删除失败：' + r.error); return; }
    toast(r && r.affected_instances != null ? `✅ 已删除（影响 ${r.affected_instances} 个实例）` : '已删除');
    loadOntology();
  });
}
// ── Protégé 式 Description 面板（严格字段：Equivalent To / SubClass Of / Disjoint With / Domain / Range / Characteristics / Inverse Of）──
const ontJs = v => String(v==null?'':v).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;');  // 2026-09-07 补 &quot;：值含双引号时 onclick 属性被截断（如公理 some "8m"）

function ontSyncViewPanes(){
  // 2026-09-02 P0-4 + 五轮调整：中栏视图模式唯一同步点——edit=Description 字段表单 / graf=1-hop 邻域子图 / owl=节点级 OWL 序列化 / cl=节点级变更历史
  // 契约：仅切换中栏容器显隐，左栏树（唯一选择源）与右栏职责不随动
  const graf = ontPaneMode==='graf';
  const owl = ontPaneMode==='owl';
  const cl = ontPaneMode==='cl';
  const grafSide=document.getElementById('ont-graf-side');
  const main=document.getElementById('ont-main');
  const side=document.getElementById('ont-side');
  const graph=document.getElementById('ont-graph');
  const nowl=document.getElementById('ont-nodeowl');
  const ncl=document.getElementById('ont-nodecl');
  if(grafSide) grafSide.style.display = graf?'block':'none';
  if(main) main.style.display = (graf||owl||cl)?'none':'flex';
  // 三栏工作台：图谱模式下右栏保持显示（节点详情面板）
  if(side) side.style.display = '';
  if(graph) graph.style.display = graf?'flex':'none';
  if(nowl) nowl.style.display = owl?'flex':'none';
  if(ncl) ncl.style.display = cl?'flex':'none';
  const st = document.getElementById('ont-side-title');
  if(st) st.textContent = graf ? '📋 详情' : '注释 · 引用';
  const gf = document.getElementById('ont-graph-footer');
  if(gf) gf.style.display = graf?'flex':'none';
}
let ontGrafDim = 'classes'; // 兼容保留：图谱焦点维度已与顶部实体维度 Tab 合一（= ontView）
let ontFocus = null;        // 2026-09-02 邻域聚焦：聚焦实体类型名（null=全量）；仅中栏图谱模式生效
let ontFocusLimit = 8;      // 2026-09-02 优化：匹配节点显示数量（8/16/0=全部）
let ontFocusRelMode = 'all'; // 2026-09-02 优化：关系范围 all=全部匹配边 / direct=仅选中节点直接边
// 图谱右栏详情（三栏工作台：左导航 | 画布 | 详情）
function renderOntSideDetail(){
  const box=document.getElementById('ont-side-body');
  if(!box) return;
  const st=document.getElementById('ont-side-title');
  if(st) st.textContent='📋 详情';
  const t=ontCurType();
  if(!t){ box.innerHTML='<div style="color:var(--mut);font-size:11.5px;line-height:1.9;padding-top:8px;">点击画布中的圆点（实体类型）或箭头线（关系）<br>此处显示完整详情</div>'; return; }
  const cons=t.constraints||{};
  const av=cons.allowed_values||{};
  const parent=t.parent_id?(ontData.types||[]).find(x=>x.id===t.parent_id):null;
  const kindName=t.type_kind==='relation'?'对象属性':(t.type_kind==='attribute'?'数据属性':'实体类型');
  let propsN=0; try{ propsN=Object.keys((typeof t.properties==='string'?JSON.parse(t.properties||'{}'):t.properties)||{}).length; }catch(e){}
  const row=(k,v)=>`<div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:4px 0;font-size:11.5px;"><span style="color:var(--mut);">${k}</span><span style="font-weight:500;text-align:right;">${v}</span></div>`;
  let h=`<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;"><span style="font-size:20px;">${esc(t.icon||'🧬')}</span><b style="font-size:14px;">${esc(t.name)}</b><span class="tag">${kindName}</span></div>`;
  if(t.type_kind==='relation'){
    h+=row('域（FROM）',esc(av.src||'—'))+row('范围（TO）',esc(av.tgt||'—'))+row('基数', (cons.card_src||cons.card_tgt) ? ('源 '+(cons.card_src?(cons.card_src.min??0)+'..'+(cons.card_src.max??'∞'):'—')+' / 目标 '+(cons.card_tgt?(cons.card_tgt.min??0)+'..'+(cons.card_tgt.max??'∞'):'—')) : esc(cons.cardinality||'—'))+row('特征',esc((cons.characteristics||[]).join(' / ')||'—'));
  } else {
    const bindN=((ontData.binding||[]).find(b=>b.entity_type===t.name)||{}).total||0;
    h+=row('子类',parent?esc(parent.name):'owl:Thing')+row('实例数',bindN)+row('必填 / 唯一',(cons.required||[]).length+' / '+(cons.unique||[]).length)+row('属性表',propsN+' 项');
  }
  if(t.description) h+=`<div style="font-size:11px;color:var(--mut);margin-top:8px;line-height:1.7;">${esc(t.description)}</div>`;
  h+=`<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;">
    <button class="btn sm" onclick="ontGotoEdit('${ontJs(t.name)}')" title="进入本体模型维护编辑该类型">✏️ 编辑</button>
    <button class="btn sm ghost" style="color:var(--red);" onclick="deleteOntType(${t.id})">🗑 删除</button>
    <span style="flex:1"></span>
    <button class="btn sm ghost" onclick="ontGotoEdit('${ontJs(t.name)}')" title="切换到编辑模式查看完整定义（中栏字段表单）">📝 编辑模式 →</button>
  </div>`;
  box.innerHTML=h;
  // 2026-09-02 三轮调整：图谱态右栏追加当前对象变更历史（对应 WebProtégé 详情面板 Changes 区）
  const cl=document.createElement('div');
  cl.id='ont-side-cl';
  cl.style.cssText='margin-top:12px;border-top:1px dashed var(--line);padding-top:8px;';
  cl.innerHTML='<div style="font-size:11.5px;font-weight:600;color:var(--blue-d);margin-bottom:4px;">📜 变更历史</div><div style="color:var(--mut);font-size:11px;">加载中…</div>';
  box.appendChild(cl);
  api('/api/knowledge/ontology/changelog?type_id='+t.id).then(logs=>{
    const el=document.getElementById('ont-side-cl'); if(!el) return;
    const arr=Array.isArray(logs)?logs:[];
    const items=arr.slice(0,10).map(l=>{
      const [label,color]=ONT_ACT_BADGE[l.action]||[l.action||'变更','var(--mut)'];
      return `<div style="padding:4px 0;border-bottom:1px dashed var(--line);font-size:11px;line-height:1.6;">
        <span style="padding:0 6px;border-radius:8px;font-size:10px;font-weight:600;background:${color};color:#fff;">${label}</span>
        ${esc(ontChangeSummary(l))}
        <span style="color:var(--mut);font-size:10px;margin-left:4px;">${esc((l.operator||''))}${(l.operator||'')?' · ':''}${esc((l.created_at||'').slice(0,16).replace('T',' '))}</span>
      </div>`;
    }).join('');
    el.innerHTML='<div style="font-size:11.5px;font-weight:600;color:var(--blue-d);margin-bottom:4px;">📜 变更历史'+(arr.length?'（'+arr.length+'）':'')+'</div>'
      +(items||'<div style="color:var(--mut);font-size:11px;">暂无变更记录</div>');
  }).catch(()=>{ const el=document.getElementById('ont-side-cl'); if(el) el.innerHTML='<div style="font-size:11.5px;font-weight:600;color:var(--blue-d);margin-bottom:4px;">📜 变更历史</div><div style="color:var(--mut);font-size:11px;">加载失败</div>'; });
}
// 图谱详情 → 维护编辑：切到本体模型维护（对应视图）并选中该类型
function ontGotoEdit(name){
  ontPaneMode = 'edit';   // 三轮调整：显式入口（图谱侧「编辑」按钮/双击节点）才切回编辑模式
  ontSyncViewPanes();
  selectOntType(name);
  toast('已切换到本体模型维护：'+name);
}
// 2026-09-02 P0-2 收窄：ontSetView 仅负责实体维度切换（classes|props|dprops）；
// 中栏「编辑⇄图谱」由 switchOntPane 负责。保留函数作为兼容入口（renderOntSideDetail 等调用）。
function ontSetView(v){
  ontView = v;
  ontPaneMode = 'edit';
  const _dim = document.getElementById('ont-left-dim');
  if(_dim) _dim.textContent = v==='props' ? '对象属性（域 → 值域）' : (v==='dprops' ? '数据属性（XSD 类型）' : '类层次（subClassOf）');
  ontSyncViewPanes();
  const kindByView = {classes:'entity', props:'relation', dprops:'attribute'};
  const want = kindByView[v];
  const cur = ontCurType();
  if(want && (!cur || cur.type_kind!==want)){
    const first=(ontData.types||[]).find(x=>x.type_kind===want);
    if(first) ontSelected={kind:'type', id:first.id, name:first.name};
  }
  renderOntTree();
  renderOntGraph();
  renderOntDesc();
  renderOntSide();
  ontFocusBreadcrumb();
}
// 图例（中栏图谱模式画布底部）
function ontGraphLegend(){
  return `<span style="font-weight:600;color:var(--ink,#223);">图例</span>
<span style="display:inline-flex;align-items:center;gap:5px;"><span style="width:12px;height:12px;border-radius:50%;background:#E6F1FB;border:2px solid #185FA5;flex:none;"></span>圆点 = 实体类型（圆内图标 · 描边色为类型区分色）</span>
<span style="display:inline-flex;align-items:center;gap:5px;"><span style="width:12px;height:12px;border-radius:50%;background:#E6F1FB;border:2px solid #185FA5;position:relative;flex:none;"><span style="position:absolute;right:-2px;bottom:-2px;width:6px;height:6px;border-radius:50%;background:#185FA5;"></span></span>圆越大 = 实例越多（旁标「N 实例」）</span>
<span style="display:inline-flex;align-items:center;gap:5px;"><svg width="20" height="8" style="flex:none;"><line x1="1" y1="4" x2="15" y2="4" stroke="#B4B2A9" stroke-width="1.4"/><polygon points="19,4 15,2 15,6" fill="#B4B2A9"/></svg>箭头线 = 类型间的关系（线旁有名称）</span>
<span style="display:inline-flex;align-items:center;gap:5px;"><span style="width:12px;height:12px;border-radius:50%;background:#FAC775;border:2px solid #D85A30;flex:none;"></span>橙色 = 当前选中</span>`;
}
function ontAddByView(){
  if(ontView==='individuals'){ ontIndividualAdd(); return; }   // P1-5：个体 Tab 下「＋ 新建」= 新建个体
  // 2026-09-08 编辑整合：默认走「两字段轻创建」迷你框（创建即选中，中栏补全细节）；?ont-slide=1 降级回滑窗表单
  const kind = ontView==='props' ? 'relation' : (ontView==='dprops' ? 'attribute' : 'entity');
  if(ontSlideEnabled()){
    ontologyAddType();
    document.getElementById('ot-kind').value=kind;
    toggleOntKindFields(kind);
    return;
  }
  ontQuickCreate(kind);
}
// ── 2026-09-08 编辑整合：滑窗降级开关（?ont-slide=1 回退旧表单流程，一个版本周期后删除）──
const ONT_SLIDE_FALLBACK = /[?&]ont-slide=1/.test(location.search);
function ontSlideEnabled(){ return ONT_SLIDE_FALLBACK; }
// ── 2026-09-08 二轮（P1）颜色自动适配：新建全默认 #185FA5 → 图谱节点同色难区分 ──
// 规则：①新建时按名称 hash 写入调色板色（落库）②渲染兜底 ontColorOf——存量默认色/缺失色按名称 hash 上调色板，
// 用户显式设置过自定义色则尊重。同色冲突概率：12 色调色板 + hash 离散，同屏 67 类下同色约 5 类但分布散
const ONT_PALETTE = ['#185FA5','#1B7A2F','#6B46B8','#BA7517','#C0392B','#0E7C86','#3F5E9E','#8A5A10','#2C7A4B','#7B3FA0','#B0563C','#0F6A5F'];
function ontAutoColor(name){
  let h=0; const s=String(name||'');
  for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))%9973;
  return ONT_PALETTE[h%ONT_PALETTE.length];
}
function ontColorOf(t){
  const c=t&&t.color;
  return (c && c!=='#185FA5') ? c : ontAutoColor(t&&t.name);
}
// ── 两字段迷你创建框（对标 WebProtégé：新增=创建+选中，编辑收敛中栏）──
// 2026-09-08 二轮简化：种类由左侧当前 Tab 定位（类层次→Class / 对象属性→ObjectProperty / 数据属性→DataProperty），
// 不再放种类下拉；支持 parentName（树节点 ＋ 新增子级）——创建后自动选中并高亮父子关系
const ONT_QC_KINDS = [['entity','实体类型（Class）'],['relation','关系类型（Object Property）'],['attribute','属性类型（Data Property）']];
function ontQuickCreate(prefKind, parentName){
  const host=document.getElementById('ont-left'); if(!host) return;
  const old=document.getElementById('ont-qc-pop'); if(old){ old.remove(); return; }   // 再点＝关闭
  const anchor=document.getElementById('ont-add-btn');
  const pop=document.createElement('div'); pop.id='ont-qc-pop';
  pop.style.cssText='position:absolute;z-index:70;width:232px;background:#fff;border:1px solid var(--blue);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.14);padding:10px;';
  pop.style.left='8px';
  pop.style.top=(anchor?anchor.getBoundingClientRect().bottom-host.getBoundingClientRect().top+4:40)+'px';
  const kindLbl=(ONT_QC_KINDS.find(k=>k[0]===prefKind)||['','类型'])[1];
  pop.innerHTML=`<div style="font-size:11.5px;font-weight:600;color:var(--blue-d);margin-bottom:6px;">🧬 新建${parentName?'子级':''}：${kindLbl} <span style="font-weight:400;color:var(--mut);">（创建后在中栏补全）</span></div>
    ${parentName?`<div style="font-size:11.5px;color:var(--mut);margin-bottom:5px;">父级：${esc(parentName)}（subClassOf 自动挂接）</div>`:''}
    <input id="qc-name" placeholder="名称，如：${prefKind==='relation'?'执行':(prefKind==='attribute'?'毁伤半径':'巡飞弹')}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;outline:none;">
    <div style="display:flex;gap:6px;margin-top:8px;justify-content:flex-end;">
      <button class="btn sm ghost" id="qc-cancel" style="padding:2px 10px;">取消</button>
      <button class="btn sm" id="qc-ok" style="padding:2px 10px;">创建</button></div>`;
  host.appendChild(pop);
  const close=()=>pop.remove();
  const nameEl=pop.querySelector('#qc-name');
  pop.querySelector('#qc-cancel').onclick=close;
  pop.querySelector('#qc-ok').onclick=()=>_ontQuickCreateSave(nameEl.value, prefKind, close, parentName);
  nameEl.onkeydown=e=>{ if(e.key==='Enter')_ontQuickCreateSave(nameEl.value, prefKind, close, parentName); if(e.key==='Escape')close(); };
  pop.onkeydown=e=>{ if(e.key==='Escape')close(); };
  setTimeout(()=>nameEl.focus(), 30);
}
async function _ontQuickCreateSave(rawName, kind, close, parentName){
  const nm=(rawName||'').trim();
  if(!nm){ toast('类型名称必填'); return; }
  if((ontData.types||[]).some(t=>t.name===nm)){ toast('⚠ 已存在同名类型：'+nm); return; }
  // 父级解析（树 ＋ 新增子级）：仅实体层级支持子级挂接；父级 must 同 kind 且存在
  let parentId=null;
  if(parentName){
    const p=(ontData.types||[]).find(t=>t.name===parentName && t.type_kind===kind);
    if(!p){ toast('父级类型不存在：'+parentName); return; }
    parentId=p.id;
  }
  const body={name:nm, type_kind:kind, properties:{}, constraints:{}, description:'', icon:'', color:ontAutoColor(nm), parent_id:parentId, status:'released'};
  const r=await api('/api/knowledge/ontology/types', {method:'POST', body:JSON.stringify(body)});
  if(r && r.error){ toast('创建失败：'+r.error); return; }
  if(close) close();
  // 相似名消歧提示（不阻断）
  try{
    const sim=(ontData.types||[]).filter(t=>{ if(!t.name||t.name===nm) return false; const a=nm.toLowerCase(), b=t.name.toLowerCase(); return (a.length>1&&b.length>1)&&(b.includes(a)||a.includes(b)); }).slice(0,3).map(t=>t.name);
    toast('✅ 已新建：'+nm+(sim.length?'（⚠ 与现有类型相似：'+sim.join(' / ')+'，如为重复请合并）':''));
  }catch(e){ toast('✅ 已新建：'+nm); }
  // 统一选择源：切换维度 → 选中 → 编辑态 → 焦点落中栏名称行内编辑
  ontView = kind==='relation' ? 'props' : (kind==='attribute' ? 'dprops' : 'classes');
  const _dim=document.getElementById('ont-left-dim');
  if(_dim) _dim.textContent = ontView==='props' ? '对象属性（域 → 值域）' : (ontView==='dprops' ? '数据属性（XSD 类型）' : '类层次（subClassOf）');
  ontPaneMode='edit'; ontSyncViewPanes();
  ontSelected={kind:'type', id:r.id, name:nm};
  await loadOntology();
  renderOntTree();
  const inp=document.getElementById('ont-name-inp'); if(inp){ inp.focus(); inp.select(); }
}
// ── 中栏行内编辑（2026-09-08 编辑整合）：名称 / 生命周期 / 抽象 / 属性引用即时写回 ──
// 名称行内编辑（Protégé 式：点击名称即改；改名联动词典迁移）
function ontInlineRename(el){
  const t=ontCurType(); if(!t || el.dataset.editing) return;
  el.dataset.editing='1';
  const oldName=t.name;
  const inp=document.createElement('input');
  inp.id='ont-name-inp';
  inp.value=oldName;
  inp.style.cssText='font-size:17px;font-weight:600;border:1px solid var(--blue);border-radius:6px;padding:1px 8px;outline:none;width:220px;font-family:inherit;';
  el.replaceWith(inp); inp.focus(); inp.select();
  let fired=false;
  const done=async(save)=>{
    if(fired) return; fired=true;
    const nv=(inp.value||'').trim();
    if(!save || !nv || nv===oldName){ renderOntDesc(); return; }
    if((ontData.types||[]).some(x=>x.name===nv && x.id!==t.id)){ toast('⚠ 已存在同名类型：'+nv); renderOntDesc(); return; }
    const cons=JSON.parse(JSON.stringify(t.constraints||{}));
    const ok=await ontPutType(t, cons, undefined, {name:nv});
    if(ok) await ontMaybeMigrateGlossary(oldName, nv); else renderOntDesc();
  };
  inp.onkeydown=e=>{ if(e.key==='Enter')done(true); if(e.key==='Escape')done(false); };
  inp.onblur=()=>done(true);
}
// R3 词法层同步共用：类型改名 → 迁移指向旧名的词典词条（人工确认）
async function ontMaybeMigrateGlossary(oldName, newName){
  if(!oldName || oldName===newName) return;
  try{
    const c = await api('/api/knowledge/glossary/by-canonical?term='+encodeURIComponent(oldName));
    const n = (c && c.count) || 0;
    if(n>0){
      const goMig = await confirmDialog('类型已改名「'+oldName+'」→「'+newName+'」：有 '+n+' 条词典词条指向旧名。是否同步迁移（词法层同步规则）？');
      if(goMig){
        const m = await api('/api/knowledge/glossary/migrate', {method:'POST', body:JSON.stringify({from_term:oldName, to_term:newName})});
        toast(m && m.migrated!=null ? '✅ 已迁移 '+m.migrated+' 条词典词条' : '迁移完成');
      }
    }
  }catch(e){}
}
// 生命周期 badge：点击循环 draft→review→released→deprecated（P1-8 白名单，后端校验兜底）
const ONT_STATUS_LABEL = {draft:'draft·草稿', review:'review·评审中', released:'released·已发布', deprecated:'deprecated·已弃用'};
const ONT_STATUS_COLOR = {draft:'#8A5A10', review:'#6B46B8', released:'#1B7A2F', deprecated:'#c0392b'};
function ontStatusBadge(t){
  const s=t.status||'released';
  return `<span class="ont-chip" onclick="ontCycleStatus()" title="生命周期（点击切换 draft→review→released→deprecated）" style="cursor:pointer;border-color:${ONT_STATUS_COLOR[s]};">
    <span style="width:7px;height:7px;border-radius:50%;background:${ONT_STATUS_COLOR[s]};flex:none;"></span>${ONT_STATUS_LABEL[s]}</span>`;
}
async function ontCycleStatus(){
  const t=ontCurType(); if(!t) return;
  const seq=['draft','review','released','deprecated'];
  const cur=seq.indexOf(t.status||'released');
  const next=seq[(cur+1)%seq.length];
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  const ok=await ontPutType(t, cons, undefined, {status:next});
  if(ok) toast('生命周期：'+ONT_STATUS_LABEL[next]);
}
// 抽象类型 chip（仅实体：abstract=true 不可直接创建实例）
async function ontToggleAbstract(){
  const t=ontCurType(); if(!t || t.type_kind!=='entity') return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  if(cons.abstract) delete cons.abstract; else cons.abstract=true;
  const ok=await ontPutType(t, cons);
  if(ok && cons.abstract) toast('已设为抽象类型（不可直接创建实例）');
}
// 中栏属性引用（2026-09-08：滑窗 ot-props-rows 能力平移，行内即时写回）
function ontPropsRefHtml(t){
  _entBindAdd=new Set(); _entBindDel=new Set();   // 即时写回模式：清会话态，保证来源计算纯净
  const { own, inh } = ontEntAttrSources(t.name);
  const c=typeof t.constraints==='string'?safeParse(t.constraints,{}):(t.constraints||{});
  const req=new Set(c.required||[]), uniq=new Set(c.unique||[]);
  const avMap=(!Array.isArray(c.allowed_values)&&c.allowed_values)?c.allowed_values:{};
  const badge=(txt,bg,fg)=>`<span style="flex:none;font-size:10px;padding:1px 6px;border-radius:8px;background:${bg};color:${fg};white-space:nowrap;">${txt}</span>`;
  const mkRow=(a,src)=>{
    const xsd=String(_asObj(a.constraints).xsd_type||'string').replace(/^xsd:/,'');
    const al=avMap[a.name]||[];
    const inhFrom=src==='inherit'?(ontEntParentChain(t.name).find(cn=>ontAttrDomains(a).includes(cn))||'父类'):'';
    return `<div class="ent-ref-row" style="display:flex;gap:6px;align-items:center;background:#fafafa;border:1px solid var(--line);border-radius:6px;padding:4px 8px;">
      <span style="width:96px;flex:none;font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a.name)}">${esc(a.name)}</span>
      ${badge(esc(xsd),'#E6F1FB','#185FA5')}
      ${src==='own'?badge('本类','#E5F6E8','#1B7A2F'):badge('继承 · '+esc(inhFrom),'#F2EDFB','#6B46B8')}
      <span style="flex:1;"></span>
      <label style="display:inline-flex;align-items:center;gap:3px;font-size:11.5px;color:var(--mut);flex:none;cursor:pointer;" title="创建实例时必填（缺则校验报错）"><input type="checkbox" ${req.has(a.name)?'checked':''} onchange="ontEntToggleRestr('required','${ontJs(a.name)}',this.checked)">必填</label>
      <label style="display:inline-flex;align-items:center;gap:3px;font-size:11.5px;color:var(--mut);flex:none;cursor:pointer;" title="值在所有实例中唯一（防重）"><input type="checkbox" ${uniq.has(a.name)?'checked':''} onchange="ontEntToggleRestr('unique','${ontJs(a.name)}',this.checked)">唯一</label>
      <input title="允许值，逗号分隔；留空=不约束（失焦/回车写回）" value="${esc(Array.isArray(al)?al.join(', '):'')}" placeholder="允许值…" style="flex:2;min-width:100px;border:1px solid var(--line);border-radius:5px;padding:2px 6px;font-size:11.5px;" onchange="ontEntSetAllowed('${ontJs(a.name)}',this.value)">
      ${src==='own'?`<span title="解绑：从该数据属性的 适用类型(domain) 移除本类" style="flex:none;color:var(--mut);cursor:pointer;font-size:13px;padding:0 2px;" onclick="ontEntUnbindAttr('${ontJs(a.name)}')">✕</span>`:''}
    </div>`;
  };
  const rows=[...own.map(a=>mkRow(a,'own')), ...inh.map(a=>mkRow(a,'inherit'))];
  return rows.length ? rows.join('')
    : '<div style="font-size:11.5px;color:var(--mut);line-height:1.8;">暂无属性引用。属性在「数据属性」全局定义（OWL/SHACL 惯例），类只做绑定——点 <b>⇄ 绑定已有</b> 选择数据属性（新建属性请到左侧「数据属性」维度）。</div>';
}
async function ontEntToggleRestr(kind, attrName, on){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  const set=new Set(cons[kind]||[]);
  if(on) set.add(attrName); else set.delete(attrName);
  if(set.size) cons[kind]=[...set]; else delete cons[kind];
  await ontPutType(t, cons);
}
async function ontEntSetAllowed(attrName, raw){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  const vals=(raw||'').split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  let av=cons.allowed_values;
  if(!av || Array.isArray(av) || typeof av!=='object') av={};   // 对象型专用（entity 局部覆盖）；数组型/缺失均重建
  if(vals.length) av[attrName]=vals; else delete av[attrName];
  if(Object.keys(av).length) cons.allowed_values=av; else delete cons.allowed_values;
  await ontPutType(t, cons);
}
async function ontEntUnbindAttr(attrName){
  const t=ontCurType(); if(!t) return;
  const a=(ontData.types||[]).find(x=>x.name===attrName && x.type_kind==='attribute'); if(!a) return;
  const ds=ontAttrDomains(a).filter(d=>d!==t.name);
  const c=Object.assign({}, _asObj(a.constraints));
  if(ds.length) c.domain_classes=ds; else delete c.domain_classes;
  const r=await api('/api/knowledge/ontology/types/'+a.id, {method:'PUT', body:JSON.stringify({
    name:a.name, type_kind:'attribute', properties:_asObj(a.properties), constraints:c,
    description:a.description||'', status:null, profile_source:a.profile_source||'', profile_ref:a.profile_ref||'',
    icon:a.icon||'', color:a.color||'#185FA5', parent_id:a.parent_id||null, iri:a.iri||''})});
  if(r && r.error){ toast('解绑失败：'+r.error); return; }
  toast('✅ 已解绑「'+attrName+'」'); loadOntology();
}
// ⇄ 绑定已有（中栏版，即时写回）：勾选确定 → 逐个 PUT 数据属性 domain_classes
function ontBindAttrModal(){
  const t=ontCurType(); if(!t || t.type_kind!=='entity') return;
  const { own, inh }=ontEntAttrSources(t.name);
  const have=new Set([...own, ...inh].map(a=>a.name));
  const cands=(ontData.types||[]).filter(x=>x.type_kind==='attribute' && !have.has(x.name));
  if(!cands.length){ toast('没有可绑定的数据属性——请先在「数据属性」维度新建属性类型'); return; }
  const lbx=document.getElementById('lbx'), body=document.getElementById('lbx-body');
  body.innerHTML=`<h3>⇄ 绑定已有数据属性</h3>
    <div style="font-size:12px;color:var(--mut);margin:4px 0 8px;line-height:1.6;">勾选要挂到「${esc(t.name)}」的数据属性，确定后<b>立即写入</b>其 适用类型（rdfs:domain）。</div>
    <div style="display:flex;flex-direction:column;gap:4px;max-height:260px;overflow:auto;margin-bottom:4px;">
      ${cands.map(a=>`<label style="display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-size:12.5px;cursor:pointer;">
        <input type="checkbox" class="bd-chk" value="${esc(a.name)}">
        <b>${esc(a.name)}</b>
        <span style="color:var(--mut);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(String(_asObj(a.constraints).xsd_type||'string').replace(/^xsd:/,''))}${a.description?' · '+esc(String(a.description).slice(0,40)):''}</span>
      </label>`).join('')}
    </div>
    <div class="lbx-actions"><button class="btn ghost" id="bd-cancel">取消</button><button class="btn" id="bd-ok">确定</button></div>`;
  lbx.classList.add('show');
  document.getElementById('bd-cancel').onclick=()=>lbx.classList.remove('show');
  document.getElementById('bd-ok').onclick=async()=>{
    const picked=[...body.querySelectorAll('.bd-chk:checked')].map(c=>c.value);
    lbx.classList.remove('show');
    if(!picked.length) return;
    const jobs=picked.map(an=>{
      const a=(ontData.types||[]).find(x=>x.name===an && x.type_kind==='attribute'); if(!a) return null;
      const ds=ontAttrDomains(a); if(!ds.includes(t.name)) ds.push(t.name);
      const c=Object.assign({}, _asObj(a.constraints)); c.domain_classes=ds;
      return api('/api/knowledge/ontology/types/'+a.id, {method:'PUT', body:JSON.stringify({
        name:a.name, type_kind:'attribute', properties:_asObj(a.properties), constraints:c,
        description:a.description||'', status:null, profile_source:a.profile_source||'', profile_ref:a.profile_ref||'',
        icon:a.icon||'', color:a.color||'#185FA5', parent_id:a.parent_id||null, iri:a.iri||''})});
    }).filter(Boolean);
    const rs=await Promise.all(jobs);
    const bad=rs.filter(r=>r && r.error);
    if(bad.length) toast('⚠ 绑定更新失败 '+bad.length+' 项：'+bad[0].error);
    else toast('✅ 已绑定 '+picked.length+' 个数据属性');
    loadOntology();
  };
}
// 2026-09-09：类编辑页「＋ 新建属性」入口移除（属性新建收敛到「数据属性」维度，
// 类只做绑定——OWL domain 惯例）；ontQuickAddAttr 无调用方，已删除。
// 2026-09-08 五轮优化：单位/量纲标准化（ISO 80000 / SI 国际单位制）
// 量纲类别 → SI 一贯单位 + 常用倍数单位（级联备选）；增益/比率为无量纲导出量。中栏内联编辑器与右侧滑窗共用此表。
const ONT_QK_UNITS = {
  '时间':   {'si':'s',  'units':['ms','s','min','h','d']},
  '长度':   {'si':'m',  'units':['mm','cm','m','km']},
  '质量':   {'si':'kg', 'units':['mg','g','kg','t']},
  '温度':   {'si':'K',  'units':['K','°C']},
  '频率':   {'si':'Hz', 'units':['Hz','kHz','MHz','GHz']},
  '功率':   {'si':'W',  'units':['mW','W','kW','MW','GW']},
  '电压':   {'si':'V',  'units':['mV','V','kV']},
  '电流':   {'si':'A',  'units':['mA','A','kA']},
  '能量':   {'si':'J',  'units':['mJ','J','kJ','MJ','kWh']},
  '压力':   {'si':'Pa', 'units':['hPa','kPa','MPa','bar']},
  '速度':   {'si':'m/s','units':['m/s','km/h','kn']},
  '角度':   {'si':'rad','units':['rad','°','mil']},
  '数据量': {'si':'B',  'units':['bit','B','KB','MB','GB','TB']},
  '增益':   {'si':'dB', 'units':['dB','dBi','dBW','dBm']},
  '比率':   {'si':'—（无量纲）','units':['%','‰','ppm']},
};
// 中栏内联级联编辑器：先选量纲类别 → 单位级联下拉（SI 一贯单位标（SI），可自定义）；自定义量纲/未选时单位自由填写
function ontUnitEditor(anchor){
  const t0=ontCurType(); if(!t0) return;
  const cons0=JSON.parse(JSON.stringify(t0.constraints||{}));
  const wrap=document.createElement('span');
  wrap.style.cssText='display:inline-flex;gap:4px;align-items:center;vertical-align:middle;flex-wrap:wrap;';
  const kOpts='<option value="">— 量纲 —</option>'+Object.keys(ONT_QK_UNITS).map(k=>`<option value="${k}">${k}（SI: ${ONT_QK_UNITS[k].si}）</option>`).join('')+'<option value="__custom">自定义…</option>';
  wrap.innerHTML='<select class="ue-k" style="font-size:11.5px;padding:2px 4px;border:1px solid var(--line);border-radius:6px;outline:none;">'+kOpts+'</select>'
    +'<input class="ue-kc" placeholder="自定义量纲" style="display:none;font-size:11.5px;padding:2px 6px;border:1px solid var(--line);border-radius:10px;width:70px;outline:none;">'
    +'<select class="ue-u" style="font-size:11.5px;padding:2px 4px;border:1px solid var(--line);border-radius:6px;outline:none;" disabled><option>—</option></select>'
    +'<input class="ue-uc" placeholder="自定义单位" style="display:none;font-size:11.5px;padding:2px 6px;border:1px solid var(--line);border-radius:10px;width:70px;outline:none;">'
    +'<button class="btn sm" style="padding:1px 8px;font-size:11px;">确定</button><span class="ont-x" style="cursor:pointer;" title="取消">×</span>';
  anchor.replaceWith(wrap);
  const kSel=wrap.querySelector('.ue-k'), kc=wrap.querySelector('.ue-kc'), uSel=wrap.querySelector('.ue-u'), uc=wrap.querySelector('.ue-uc');
  const sync=()=>{
    kc.style.display = kSel.value==='__custom' ? '' : 'none';
    if(kSel.value && kSel.value!=='__custom'){
      const def=ONT_QK_UNITS[kSel.value];
      uSel.disabled=false;
      uSel.innerHTML=def.units.map(u=>`<option value="${u}">${u}${u===def.si?'（SI）':''}</option>`).join('')+'<option value="__custom">自定义…</option>';
      uc.style.display='none'; uc.value='';
    } else {
      uSel.disabled=true; uSel.innerHTML='<option>—</option>';
      uc.style.display='';
    }
  };
  uSel.onchange=()=>{ if(uSel.disabled) return; uc.style.display = uSel.value==='__custom' ? '' : 'none'; if(uSel.value!=='__custom') uc.value=''; };
  kSel.onchange=sync;
  sync();
  if(cons0.quantity_kind){ if(ONT_QK_UNITS[cons0.quantity_kind]) kSel.value=cons0.quantity_kind; else { kSel.value='__custom'; kc.value=cons0.quantity_kind; } sync(); }
  if(cons0.unit){
    if(!uSel.disabled){
      if([...uSel.options].some(o=>o.value===cons0.unit)) uSel.value=cons0.unit;
      else { uSel.value='__custom'; uc.value=cons0.unit; uc.style.display=''; }
    } else uc.value=cons0.unit;
  }
  let fired=false;
  const done=async(save)=>{
    if(fired) return; fired=true;
    if(save){
      const t=ontCurType(); if(!t) return;
      const cons=JSON.parse(JSON.stringify(t.constraints||{}));
      const k=kSel.value;
      if(!k) delete cons.quantity_kind;
      else if(k==='__custom'){ if(kc.value.trim()) cons.quantity_kind=kc.value.trim(); else delete cons.quantity_kind; }
      else cons.quantity_kind=k;
      const uv=uc.value.trim();
      if(uSel.disabled || uSel.value==='__custom'){ if(uv) cons.unit=uv; else delete cons.unit; }
      else if(uSel.value) cons.unit=uSel.value;
      else delete cons.unit;
      const ok=await ontPutType(t, cons);
      if(ok) toast('✅ 单位/量纲已更新'+(cons.quantity_kind?`（${cons.quantity_kind}${cons.unit?' · '+cons.unit:''}）`:''));
      return;
    }
    const add=document.createElement('span'); add.className='ont-add';
    add.textContent='＋ 设置 / 修改'; add.title='选择单位与量纲（ISO 80000/SI 标准级联下拉）';
    add.onclick=function(){ ontUnitEditor(add); };
    if(document.body.contains(wrap)) wrap.replaceWith(add);
  };
  wrap.querySelector('button').onclick=()=>done(true);
  wrap.querySelector('.ont-x').onclick=()=>done(false);
  kSel.focus();
}
// 中栏字段级删除（单位/量纲 chip 的 ×）
async function ontDelField(field){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  delete cons[field];
  await ontPutType(t, cons);
}
// 左栏树（2026-09-02 P0-3 唯一选择源）：内容随顶部实体维度 Tab（ontView）切换
// 中栏图谱模式下点击节点保持联动聚焦（keepView=true），不跳编辑
function renderOntTree(){
  const box=document.getElementById('ont-types-list');
  if(!box) return;
  const isGraf = ontPaneMode==='graf';
  const view = ontView;
  const q=((document.getElementById('ont-filter')||{}).value||'').trim().toLowerCase();
  const clk = name => isGraf ? `selectOntType('${ontJs(name)}', true)` : `selectOntType('${ontJs(name)}')`;
  if(view==='dprops'){
    const attrs=(ontData.types||[]).filter(t=>t.type_kind==='attribute' && (!q || (t.name||'').toLowerCase().includes(q)));
    box.innerHTML = attrs.length ? attrs.map(a=>{
      const sel=ontSelected && ontSelected.kind==='type' && ontSelected.name===a.name;
      const xsd=(a.constraints&&a.constraints.xsd_type)||'';
      return `<div class="ont-tnd${sel?' on':''}" onclick="${clk(a.name)}" title="${esc(a.description||'')}"><span style="color:var(--mut);">◆</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(a.name)}</span><span style="color:var(--mut);font-size:10px;">${esc(xsd)}</span><span class="ont-del" title="删除该数据属性类型" onclick="event.stopPropagation();deleteOntType(${a.id})">✕</span></div>`;
    }).join('') : '<div style="color:var(--mut);font-size:11.5px;padding:6px;">暂无数据属性类型</div>';
    return;
  }
  if(view==='props'){
    const rels=(ontData.types||[]).filter(t=>t.type_kind==='relation' && (!q || (t.name||'').toLowerCase().includes(q)));
    box.innerHTML = rels.length ? rels.map(r=>{
      const av=(r.constraints&&r.constraints.allowed_values)||{};
      const g=Array.isArray(av.tgt)?av.tgt.join('/'):(av.tgt||'—');
      const sel=ontSelected && ontSelected.kind==='type' && ontSelected.name===r.name;
      return `<div class="ont-tnd${sel?' on':''}" onclick="${clk(r.name)}" title="${esc(r.name)}"><span style="color:var(--mut);">◇</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(r.name)}</span><span class="ont-del" title="删除该对象属性类型" onclick="event.stopPropagation();deleteOntType(${r.id})">✕</span></div>`;
    }).join('') : '<div style="color:var(--mut);font-size:11.5px;padding:6px;">暂无关系类型</div>';
    return;
  }
  const items=ontTypeTree('entity').filter(({t})=>!q || (t.name||'').toLowerCase().includes(q));
  // 2026-09-02 聚焦下钻：树联动——非可见节点灰显（子树/祖先/关联正常），保持层级可读
  const fsetT = (ontFocus && view==='classes') ? ontFocusSet(ontFocus) : null;
  box.innerHTML = items.length ? items.map(({t,depth})=>{
    const sel=(ontSelected && ontSelected.kind==='type' && ontSelected.name===t.name) || (fsetT && ontFocus && t.name===ontFocus);
    const dim = fsetT && !fsetT.visible.has(t.name);
    let tag = '';
    if(fsetT && fsetT.visible.has(t.name) && t.name!==ontFocus){
      if(fsetT.subtree.has(t.name)) tag = ' <span style="color:#2F6F4F;font-size:9px;">↓子类</span>';
      else if(fsetT.ancestors.includes(t.name)) tag = ' <span style="color:var(--mut);font-size:9px;">↑祖先</span>';
      else tag = ' <span style="color:#A08B3C;font-size:9px;">·关联</span>';
    }
    return `<div class="ont-tnd${sel?' on':''}${dim?' ont-dimmed':''}" style="padding-left:${depth*16+8}px" onclick="${clk(t.name)}" title="${esc(t.description||'')}${dim?'（非聚焦节点，点击可切换聚焦）':''}"><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${depth?'<span style="color:var(--mut);">⤷ </span>':''}${esc(t.name)}${tag}</span><span class="ont-tadd" title="新增子类（挂到「${esc(t.name)}」下）" onclick="event.stopPropagation();ontQuickCreate('entity','${ontJs(t.name)}')">＋</span><span class="ont-del" title="删除该类（子类挂靠父类型，实例不再受约束）" onclick="event.stopPropagation();deleteOntType(${t.id})">✕</span></div>`;
  }).join('') : '<div style="color:var(--mut);font-size:11.5px;padding:6px;">暂无类</div>';
}
// 写回：PUT 类型（constraints/parent 可变，其余保留原值）
// 2026-09-08 扩展：①补 iri/profile_source/profile_ref（原缺失会被后端默认值 "" 清空）②status 恒传 null（=不变更）
// ③extra：字段级覆盖入口（name/description/status/profile_* —— 中栏名称行内编辑、生命周期 badge、右栏 Annotations 即时写回共用）
// ④补 replaced_by/deprecated_note 透传（后端 PUT 整体替换列，缺失即清空——2026-09-08 二轮审计发现）
// P1 精确多重性：保存关系类型基数（constraints.card_src/card_tgt）
async function ontSaveCard(){
  const t=ontCurType(); if(!t || t.type_kind!=='relation') return;
  const g=id=>{ const v=(document.getElementById(id)||{}).value; return (v===''||v===null||v===undefined)?null:parseInt(v,10); };
  const cs={min:g('ont-cs-min'), max:g('ont-cs-max')};
  const ct={min:g('ont-ct-min'), max:g('ont-ct-max')};
  if(cs.min!==null && cs.max!==null && cs.min>cs.max){ toast('源端 min 不能大于 max'); return; }
  if(ct.min!==null && ct.max!==null && ct.min>ct.max){ toast('目标端 min 不能大于 max'); return; }
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  if(cs.min===null && cs.max===null) delete cons.card_src; else cons.card_src=cs;
  if(ct.min===null && ct.max===null) delete cons.card_tgt; else cons.card_tgt=ct;
  const ok=await ontPutType(t, cons);
  if(ok) toast('✅ 基数已保存——SHACL minCount/maxCount 形状与「基数校验」即时生效');
}
async function ontPutType(t, constraints, parentId, extra){
  let props = t.properties||{};
  if(typeof props==='string'){ try{ props=JSON.parse(props||'{}'); }catch(e){ props={}; } }
  const body={
    name:(extra&&extra.name!==undefined)?extra.name:t.name, type_kind:t.type_kind,
    properties:props,
    constraints:constraints||t.constraints||{},
    description:(extra&&extra.description!==undefined)?extra.description:(t.description||''),
    icon:t.icon||'', color:t.color||'#185FA5',
    parent_id:(parentId!==undefined)?parentId:(t.parent_id||null),
    iri:t.iri||'', status:null,
    profile_source:t.profile_source||'', profile_ref:t.profile_ref||'',
    replaced_by:t.replaced_by||'', deprecated_note:t.deprecated_note||'',
  };
  if(extra) for(const k of ['status','profile_source','profile_ref']){ if(extra[k]!==undefined) body[k]=extra[k]; }
  try{
    const r=await api('/api/knowledge/ontology/types/'+t.id, {method:'PUT', body:JSON.stringify(body)});
    if(r && r.error){ toast('保存失败：'+r.error); return false; }
    toast('✅ 已更新');
    loadOntology();
    return true;
  }catch(e){ toast('保存失败：'+(e.message||e)); return false; }
}
function ontCurType(){
  return ontSelected ? (ontData.types||[]).find(x=>x.id===ontSelected.id || x.name===ontSelected.name) : null;
}
// chip 删除类操作（写回 API）
async function ontDelParent(){
  const t=ontCurType(); if(!t) return;
  await ontPutType(t, t.constraints, null);
}
async function ontDelRestr(kind, key){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  cons[kind]=(cons[kind]||[]).filter(k=>k!==key);
  await ontPutType(t, cons);
}
async function ontDelDomRange(side, key){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  const av=cons.allowed_values||{};
  const arr=Array.isArray(av[side])?av[side]:(av[side]?[av[side]]:[]);
  const rest=arr.filter(x=>x!==key);
  if(rest.length) av[side]=rest; else delete av[side];
  cons.allowed_values=av;
  await ontPutType(t, cons);
}
// R2a：特性多选切换（transitive/symmetric/asymmetric/reflexive/functional/inverse_functional）
// 兼容旧数据：cardinality='1' 读时视为 functional（写时统一走 characteristics）
async function ontToggleChar(key){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  const arr=Array.isArray(cons.characteristics)?cons.characteristics:[];
  if(arr.includes(key)) cons.characteristics=arr.filter(x=>x!==key);
  else cons.characteristics=[...arr,key];
  const ok=await ontPutType(t, cons);
  if(ok) toast(ONT_CHAR_DEFS.some(d=>d[0]===key && cons.characteristics.includes(key))
    ? '✅ 已启用「'+ONT_CHAR_DEFS.find(d=>d[0]===key)[1]+'」公理' : '已取消「'+key+'」公理');
}
// 2026-09-08 五轮优化：特征定义表（key/中文/OWL 公理/语义解释/示例）—— chip tooltip 直接可读
const ONT_CHAR_DEFS=[
  ['transitive','传递','owl:TransitiveProperty','若 A→B 且 B→C，则 A→C 自动成立（推理器派生）','例：「包含」——部件包含组件、组件包含零件 ⇒ 部件包含零件'],
  ['symmetric','对称','owl:SymmetricProperty','若 A→B 成立，则 B→A 自动成立','例：「相邻」——A 相邻 B ⇒ B 相邻 A'],
  ['asymmetric','反对称','owl:AsymmetricProperty','若 A→B 成立，则 B→A 必不成立（与「对称」互斥）','例：「上级」——A 是 B 的上级 ⇒ B 不可能是 A 的上级'],
  ['reflexive','自反','owl:ReflexiveProperty','每个实例与自身都成立该关系','例：「等同」——任何元素等同它自己'],
  ['functional','函数型','owl:FunctionalProperty','每个源实例最多指向一个目标（i→≤1 个 j），写入超量时报错','例：「所属系统」——一个部件只属于一个系统'],
  ['inverse_functional','反函数型','owl:InverseFunctionalProperty','每个目标实例最多被一个源指向（j→≤1 个 i 指向它）','例：「唯一编码指向」——一个序列号只对应一个设备']];
// R2a：特性 chips 渲染（6 枚举 + 兼容 cardinality=1 → functional）
// 2026-09-08 五轮优化：开关语义可见化——实心 chip=已启用（点击取消），＋=未启用（点击启用）；tooltip 含中文语义+示例
function ontCharChips(cons, mode){
  // 六轮调整：数据属性仅函数型（传递/对称等为对象属性专属，WebProtégé 对 DatatypeProperty 只提供 Functional）
  const pool = mode==='attr' ? ONT_CHAR_DEFS.filter(d=>d[0]==='functional') : ONT_CHAR_DEFS;
  const arr=Array.isArray(cons.characteristics)?cons.characteristics:[];
  const isFunc=arr.includes('functional') || cons.cardinality==='1';
  return pool.map(([k,label,owl,sem,ex])=>{
    const tip=`【${owl}】${sem}\n${ex}\n（点击切换 启用/取消）`;
    const on = k==='functional' ? isFunc : arr.includes(k);
    return on ? ontChipHtml(label, owl, `ontToggleChar('${k}')`)
              : `<span class="ont-add" onclick="ontToggleChar('${k}')" title="${tip}">＋ ${label}</span>`;
  }).join(' ');
}
// 特征节帮助图标（对象属性/数据属性共用）：说明点击切换交互
function ontCharHelp(isAttr){
  return `<span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="${isAttr
    ?'数据属性（owl:DatatypeProperty）在 OWL 中合法特征只有「函数型 Functional」：每个实例该属性最多取一个值。实心 chip=已启用（点击取消），＋=未启用（点击启用）。'
    :'OWL 公理开关：点击「＋ 名称」启用，点击实心 chip 取消。启用后写入本体并参与推理/校验。各项语义将鼠标悬停在对应项上查看。'}">ⓘ</span>`;
}
// ＋ 内联添加：parent / restr / dom / rng（回车写回，Esc 取消）
function ontInlineAdd(kind, anchorEl){
  const wrap=document.createElement('span');
  wrap.style.cssText='display:inline-flex;gap:4px;align-items:center;vertical-align:middle;';
  const entOpts=(ontData.types||[]).filter(x=>x.type_kind==='entity')
    .map(x=>`<option value="${esc(x.name)}">${esc(x.name)}</option>`).join('');
  const _dlId='ont-dl-'+kind+'-'+Date.now();
  const _dlHtml='<datalist id="'+_dlId+'">'+entOpts+'</datalist>';
  if(kind==='parent'){
    // 2026-09-07 组件优化：原生 select → 可搜索 input+datalist（WebProtégé 式输入即过滤）
    wrap.innerHTML='<input list="'+_dlId+'" placeholder="输入/选择父类…" class="ont-inline-inp" title="支持输入过滤">'+_dlHtml;
  } else if(kind==='dom' || kind==='rng'){
    wrap.innerHTML='<input list="'+_dlId+'" placeholder="输入/选择实体类型…" class="ont-inline-inp" title="支持输入过滤">'+_dlHtml;
  } else {
    wrap.innerHTML='<select style="font-size:11px;padding:2px 3px;border:1px solid var(--line);border-radius:6px;"><option value="req">some（必填）</option><option value="uni">exactly 1（唯一）</option></select>'
      +'<input placeholder="属性名" style="font-size:11.5px;padding:2px 6px;border:1px solid var(--line);border-radius:10px;width:86px;outline:none">';
  }
  anchorEl.replaceWith(wrap);
  let fired=false;
  const done=async()=>{
    if(fired) return; fired=true;
    const t=ontCurType();
    let ok=true;
    const _rd=()=> (wrap.querySelector('input')||{}).value!=null ? wrap.querySelector('input').value.trim() : '';
    if(kind==='parent'){
      const v=_rd();
      if(v && t){
        const p=(ontData.types||[]).find(x=>x.type_kind==='entity'&&x.name===v);
        if(!p){ toast('未找到类型「'+v+'」'); ok=false; }
        else ok=await ontPutType(t, t.constraints, p.id);
      }
    } else if(kind==='dom'||kind==='rng'){
      const v=_rd();
      if(v && t){
        const p=(ontData.types||[]).find(x=>x.type_kind==='entity'&&x.name===v);
        if(!p){ toast('未找到类型「'+v+'」'); ok=false; }
        else{
          const cons=JSON.parse(JSON.stringify(t.constraints||{})); const av=cons.allowed_values||{};
          const arr=Array.isArray(av[kind])?av[kind]:(av[kind]?[av[kind]]:[]);
          if(!arr.includes(v)) arr.push(v);
          av[kind]=arr; cons.allowed_values=av; ok=await ontPutType(t, cons);
        }
      }
    } else {
      const mode=wrap.querySelector('select').value, v=wrap.querySelector('input').value.trim();
      if(v && t){ const cons=JSON.parse(JSON.stringify(t.constraints||{}));
        const key=mode==='req'?'required':'unique';
        cons[key]=cons[key]||[]; if(!cons[key].includes(v)) cons[key].push(v);
        ok=await ontPutType(t, cons); }
    }
    if(ok) return; // 成功时 loadOntology 已重渲整个面板
    // 失败：还原 ＋ 按钮
    const add=document.createElement('span'); add.className='ont-add';
    add.textContent = kind==='restr' ? '＋ 限制' : '＋';
    add.title='添加'; add.onclick=function(){ ontInlineAdd(kind, add); };
    if(document.body.contains(wrap)) wrap.replaceWith(add);
  };
  const inp=wrap.querySelector('input');
  const sel=wrap.querySelector('select');
  if(inp){
    inp.onkeydown=e=>{ if(e.key==='Enter')done(); if(e.key==='Escape'){ inp.value=''; done(); } };
    inp.onblur=()=>setTimeout(done,150);
    if(sel) sel.onchange=()=>inp.focus();
    inp.focus();
  } else if(sel){
    sel.onchange=done; sel.onblur=()=>setTimeout(done,200);
    sel.focus();
  }
}
function ontChipHtml(label, k, delJs){
  return `<span class="ont-chip${label.indexOf(':')===0?' ont-mono':''}"><b>${label}</b>${k?`<span class="k">${k}</span>`:''}${delJs?`<span class="ont-x" onclick="${delJs}" title="移除（写回 API）">×</span>`:''}</span>`;
}
// ── P1-5 个体 Tab（Individuals）：中栏投影 = 左栏选中类型的实例列表 ──
let ontIndCache = null;    // 个体数据缓存 {ts, entities}
async function ontIndividualsData(){
  if(ontIndCache && Date.now()-ontIndCache.ts < 30000) return ontIndCache.entities;
  const g = await api('/api/knowledge/graph?branch=dev&status=all');
  const ents = (g.entities||[]);
  ontIndCache = {ts:Date.now(), entities:ents};
  return ents;
}
async function renderOntIndividuals(box){
  const t=ontCurType();
  if(!t || t.type_kind!=='entity'){
    box.innerHTML='<div style="color:var(--mut);font-size:12.5px;padding:20px 0;">从左侧选择一个实体类型，此处显示该类型的个体（实例）列表。</div>';
    return;
  }
  box.innerHTML = '<div class="loading">加载个体数据…</div>';
  let ents=[];
  try{ ents = await ontIndividualsData(); }catch(e){
    box.innerHTML = `<div style="color:var(--red);font-size:12px;padding:12px 0;">个体数据加载失败：${esc(e.message||e)}</div>`;
    return;
  }
  const mine = ents.filter(e=>e.entity_type===t.name);
  const stCls = s => s==='reviewed' ? '<span class="st ok" title="已评审">✅ 已评审</span>' : (s==='candidate' ? '<span class="st w" title="候选/待评审">🟡 候选</span>' : `<span class="st w">${esc(s||'—')}</span>`);
  const rows = mine.map(e=>{
    let propsN=0; try{ propsN=Object.keys((typeof e.properties==='string'?JSON.parse(e.properties||'{}'):e.properties)||{}).length; }catch(ex){}
    return `<tr style="border-bottom:1px dashed var(--line);">
      <td style="padding:6px 8px;font-weight:500;">${esc(e.icon||'🔹')} ${esc(e.name)}</td>
      <td style="padding:6px 8px;">${stCls(e.status)}</td>
      <td style="padding:6px 8px;color:var(--mut);font-size:11px;">${esc(e.source_type||'—')}</td>
      <td style="padding:6px 8px;color:var(--mut);">${propsN||'—'}</td>
      <td style="padding:6px 8px;color:var(--mut);font-size:11px;">${esc((e.created_at||'').slice(0,10)||'—')}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
      <span style="font-size:15px;font-weight:600;color:var(--blue-d);">${esc(t.icon||'🛰')} ${esc(t.name)} 的个体</span>
      <span class="tag">${mine.length} 个实例</span>
      <span style="flex:1"></span>
      <button class="btn sm" onclick="ontIndividualAdd()">＋ 新建个体</button>
      <button class="btn sm ghost" onclick="ontIndCache=null;renderOntDesc();">↻ 刷新</button>
      <button class="btn sm ghost" onclick="try{go('kb','kb-d')}catch(e){}" title="跳转图谱工作区页查看关系全景">↗ 图谱工作区</button>
    </div>
    ${mine.length ? `<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid var(--line);border-radius:8px;">
      <thead><tr style="background:#fafaf7;border-bottom:1px solid var(--line);color:var(--mut);font-size:11px;">
        <th style="text-align:left;padding:6px 8px;">名称</th><th style="text-align:left;padding:6px 8px;">状态</th>
        <th style="text-align:left;padding:6px 8px;">来源</th><th style="text-align:left;padding:6px 8px;">属性</th>
        <th style="text-align:left;padding:6px 8px;">创建日期</th>
      </tr></thead><tbody>${rows}</tbody></table>`
    : `<div style="color:var(--mut);font-size:12px;padding:16px;border:1px dashed var(--line);border-radius:8px;line-height:1.8;">
        该类型暂无个体（实例）。<br>可「＋ 新建个体」手工创建，或在 <b>资料库</b> 抽取 / <b>图谱工作区</b> 页维护后回到此处查看。
      </div>`}`;
}
// ＋ 新建个体：POST /api/knowledge/graph/nodes（entity_type=左栏选中类型，status=candidate 进入评审闸门）
async function ontIndividualAdd(){
  const t=ontCurType();
  if(!t || t.type_kind!=='entity'){ toast('请先在左侧选择一个实体类型'); return; }
  const name = await promptDialog({title:'新建个体', message:'在「'+t.name+'」下新建个体：', placeholder:'个体名称（如：Ku 转发器 #1）'});
  if(!name || !name.trim()) return;
  try{
    const r = await api('/api/knowledge/graph/nodes', {method:'POST', body:JSON.stringify({
      name:name.trim(), entity_type:t.name, branch:'dev',
    })});
    if(r && r.error){ toast('创建失败：'+r.error); return; }
    toast('✅ 个体已创建（候选态，评审通过后落图）');
    ontIndCache = null;
    renderOntDesc();
  }catch(e){ toast('创建失败：'+(e.message||e)); }
}
// Description 主面板（严格 Protégé 字段分组）
function renderOntDesc(){
  const box=document.getElementById('ont-desc');
  // 四轮调整：仅编辑态渲染 Description（graf=图谱 / owl=节点OWL 由各自容器接管）
  if(!box || ontPaneMode!=='edit') return;
  // P1-5 个体 Tab：中栏投影 = 选中类型的实例列表（个体=类型的实例投影）
  if(ontView==='individuals'){ renderOntIndividuals(box); return; }
  const t=ontCurType();
  if(!t){ box.innerHTML='<div style="color:var(--mut);font-size:12.5px;padding:20px 0;">从左侧选择一个 Class 或 Object Property，此处显示其 Description（Protégé 字段）。</div>'; return; }
  const cons=t.constraints||{};
  const isRel=t.type_kind==='relation';
  const av=cons.allowed_values||{};
  const domArr=Array.isArray(av.src)?av.src:(av.src?[av.src]:[]);
  const rngArr=Array.isArray(av.tgt)?av.tgt:(av.tgt?[av.tgt]:[]);
  const parent=t.parent_id ? (ontData.types||[]).find(x=>x.id===t.parent_id) : null;
  let propsN=0; try{ propsN=Object.keys((typeof t.properties==='string'?JSON.parse(t.properties||'{}'):t.properties)||{}).length; }catch(e){}
  const restr=[].concat(
    (cons.required||[]).map(k=>[':hasAttr some "'+k+'"','必填','required',k]),
    (cons.unique||[]).map(k=>[':hasAttr exactly 1 "'+k+'"','唯一','unique',k]));
  if(t.type_kind==='attribute'){ box.innerHTML=renderOntDescAttr(t,cons); return; }
  // 2026-09-08 编辑整合：头部=名称行内编辑 + 类型 badge + 生命周期 badge；「✏️ 编辑」仅滑窗降级模式渲染
  let h=`<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;">
    <span style="font-size:19px;font-weight:600;cursor:text;border-bottom:1px dashed transparent;" id="ont-name-wrap" title="点击修改名称" onclick="ontInlineRename(this)">${esc(t.name)}</span>
    <span class="ont-mono" style="font-size:11.5px;color:var(--mut);">${isRel?'owl:ObjectProperty':'owl:Class'}</span>
    ${ontStatusBadge(t)}
    <span style="flex:1"></span><span id="ont-flash" style="display:none;font-size:11.5px;color:var(--grn);">已写回</span>
    ${ontSlideEnabled()?`<button class="btn sm ghost" onclick="ontEditTypeById(${t.id})" title="${isRel?'约束复杂编辑（右侧滑窗）':'属性表/约束复杂编辑（右侧滑窗）'}">${isRel?'✏️ 编辑':'✏️ 属性表'}</button>`:''}</div>`;
  // 四轮调整：中栏右上角「🗑 删除」已移除——删除统一走左栏树节点悬停 ✕ / 图谱右栏详情
  h+=`<div style="display:flex;align-items:center;gap:6px;margin-top:14px;font-size:11.5px;color:var(--mut);">
    <span style="background:var(--blue-l);border:1px solid var(--line);padding:1px 6px;border-radius:4px;font-size:10.5px;font-weight:600;color:var(--blue-d);">IRI</span>
    <span class="ont-mono" style="color:var(--blue-d);cursor:pointer;word-break:break-all;" onclick="copyIri(${t.id})" title="点击复制 IRI">${esc(t.iri||'—')}</span>
    <button class="btn sm ghost" style="padding:0 6px;font-size:11px;" onclick="editEntityIri(${t.id})" title="修改 IRI">🔗</button>
  </div>`;
  if(isRel){
    h+=`</div><div class="ont-fcard"><div class="ont-sec">等价类 <span style="letter-spacing:0;">Equivalent To · Manchester 表达式</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="owl:equivalentClass：与本属性等价的表达式集合（当前版本随本体注释导出）">ⓘ</span></div><div>${(cons.equivalent_to||[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('equivalent_to','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('equivalent_to',this)" title="输入属性表达式">＋ 表达式</span></div>`;
        h+=`</div><div class="ont-fcard"><div class="ont-sec">定义域 <span style="letter-spacing:0;">Domain</span></div><div>${domArr.map(x=>ontChipHtml(':'+x,'',`ontDelDomRange('src','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAdd('dom',this)" title="添加定义域">＋</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">值域 <span style="letter-spacing:0;">Range</span></div><div>${rngArr.map(x=>ontChipHtml(':'+x,'',`ontDelDomRange('tgt','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAdd('rng',this)" title="添加值域">＋</span></div>`;
    // P1 精确多重性（2026-09-10）：min/max → SHACL minCount/maxCount + 推理「基数校验」
    const _csrc=(cons.card_src||{}), _ctgt=(cons.card_tgt||{});
    const _cInp='border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;width:100%;box-sizing:border-box;';
    h+=`</div><div class="ont-fcard"><div class="ont-sec">基数 <span style="letter-spacing:0;">Cardinality · 精确多重性</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="源端=该关系的每个源实例发出的边数；目标端=每个目标实例接收的边数（SHACL inversePath）。min/max 留空=不限。保存后自动生成 sh:minCount/sh:maxCount 形状，推理面板「基数校验」即时消费">ⓘ</span></div>
      <div style="display:grid;grid-template-columns:auto 1fr 1fr;gap:4px 8px;align-items:center;font-size:11.5px;max-width:340px;">
        <span style="color:var(--mut);">源端（发出）</span>
        <input id="ont-cs-min" type="number" min="0" step="1" value="${_csrc.min ?? ''}" placeholder="min（空=0）" style="${_cInp}">
        <input id="ont-cs-max" type="number" min="0" step="1" value="${_csrc.max ?? ''}" placeholder="max（空=∞）" style="${_cInp}">
        <span style="color:var(--mut);">目标端（接收）</span>
        <input id="ont-ct-min" type="number" min="0" step="1" value="${_ctgt.min ?? ''}" placeholder="min（空=0）" style="${_cInp}">
        <input id="ont-ct-max" type="number" min="0" step="1" value="${_ctgt.max ?? ''}" placeholder="max（空=∞）" style="${_cInp}">
      </div>
      <div style="margin-top:4px;"><span class="ont-add" onclick="ontSaveCard()" title="写入 constraints.card_src/card_tgt，SHACL 形状与推理基数校验即时消费">💾 保存基数</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">特征 <span style="letter-spacing:0;">Characteristics · 推理性质</span>${ontCharHelp(false)}</div><div style="display:flex;flex-wrap:wrap;gap:4px;">${ontCharChips(cons)}</div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">互斥 <span style="letter-spacing:0;">Disjoint With</span></div><div>${(cons.disjoint_with||[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('disjoint_with','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('disjoint_with',this)" title="选择互斥的关系">＋</span></div>`;
      } else {
    h+=`</div><div class="ont-fcard"><div class="ont-sec">等价类 <span style="letter-spacing:0;">Equivalent To · Manchester 表达式</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="owl:equivalentClass：与本类等价的类表达式集合（当前版本随本体注释导出，推理语义在路线图）">ⓘ</span></div><div>${(cons.equivalent_to||[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('equivalent_to','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('equivalent_to',this)" title="输入 OWL 类表达式">＋ 表达式</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">子类于 <span style="letter-spacing:0;">Subclass Of</span></div>`;
    h+=`<div>${parent?ontChipHtml(esc(parent.name),'',`ontDelParent()`):ontChipHtml('owl:Thing','')}<span class="ont-add" onclick="ontInlineAdd('parent',this)" title="添加 / 更换父类">＋</span></div>`;
    // 2026-09-08 五轮优化：抽象类型独立成节（类型自身性质，与继承/限制无关）；限制独立节标题（some/exactly 1 属匿名类表达式，Protégé 惯例挂 Subclass Of 但需视觉分组）
    h+=`</div><div class="ont-fcard"><div class="ont-sec">抽象类型 <span style="letter-spacing:0;">Abstract</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="勾选后该类型不可直接创建实例，仅作分类节点（抽象父类）。点击下方 chip 切换">ⓘ</span></div>
      <div><span class="ont-chip" onclick="ontToggleAbstract()" title="点击切换：●=抽象（不可直接创建实例） / ○=具体（可创建实例）" style="cursor:pointer;${cons.abstract?'border-color:var(--blue);color:var(--blue-d);background:var(--blue-l);':''}">${cons.abstract?'●':'○'} 抽象类型</span>
      <span style="font-size:11px;color:var(--mut);margin-left:6px;">${cons.abstract?'不可直接创建实例，仅作分类节点':'可创建实例（点击 chip 切换为抽象）'}</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">限制 <span style="letter-spacing:0;">Restrictions · 匿名类表达式</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="owl:Restriction：对「本类实例必须如何使用属性」的约束。some=必填（缺则校验报错）/ exactly 1=唯一（防重）。导出时编译为真公理，推理器可消费">ⓘ</span></div>
      <div>${restr.length?restr.map(([expr,k,kind,key])=>ontChipHtml(expr,k,`ontDelRestr('${kind}','${ontJs(key)}')`)).join(' '):'<span style="font-size:11.5px;color:var(--mut);">未设置（也可在「属性引用」行内按属性行勾选必填/唯一）</span>'}<span class="ont-add" onclick="ontInlineAdd('restr',this)" title="添加限制表达式（some=必填 / exactly 1=唯一）">＋ 限制</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">互斥 <span style="letter-spacing:0;">Disjoint With</span></div><div>${(cons.disjoint_with||[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('disjoint_with','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('disjoint_with',this)" title="选择互斥的类">＋</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">一般类公理 <span style="letter-spacing:0;">General Axioms</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="OWL 一般类公理（GCI）。支持语法：属性 some「值」/ 属性 exactly 1「值」—— 导出时编译为 owl:Restriction 真公理（Protégé/推理器可消费）；未识别语法则以注释保留原文">ⓘ</span></div><div>${(cons.axioms||[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('axioms','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('axioms',this)" title="输入公理表达式，如：毁伤半径 some 「8m」">＋ 公理</span></div>`;
    // 2026-09-08 编辑整合：属性引用=滑窗 ot-props-rows 能力平移（必填/唯一/白名单行内即时写回 + 绑定/解绑即时生效）
  h+=`</div><div class="ont-fcard"><div class="ont-sec">属性引用 <span style="letter-spacing:0;">Attributes · 数据属性绑定与局部约束</span><span style="flex:1"></span>
    <button class="btn sm ghost ont-act-btn" style="padding:1px 8px;font-size:11px;" onclick="ontBindAttrModal()" title="把已有的数据属性绑定到本类（立即写入其 适用类型/domain）">⇄ 绑定已有</button></div>
    <div id="ont-props-ref">${ontPropsRefHtml(t)}</div>`;
  }
  h+='</div>';   // 2026-09-11 分组卡片：闭合最后一个 .ont-fcard（首个卡片前的多余 </div> 会被解析器忽略）
  box.innerHTML=h;
}
// P0-1：IRI 复制 / 修改（WebProtege 对齐：每个实体有 IRI，编辑区顶部展示）
function copyIri(id){
  const t=(ontData.types||[]).find(x=>x.id===id);
  if(!t){ toast('未找到类型'); return; }
  if(!t.iri){ toast('该类型暂无 IRI'); return; }
  navigator.clipboard.writeText(t.iri).then(()=>toast('✅ IRI 已复制'), ()=>toast('复制失败'));
}
async function editEntityIri(id){
  const t=(ontData.types||[]).find(x=>x.id===id);
  if(!t) return;
  const cur=t.iri||'';
  promptDialog({title:'修改 IRI', message:'为「'+t.name+'」设置 IRI（留空=系统按命名空间策略自动生成；改名时自动生成的 IRI 会跟随）：', value:cur, placeholder:'http://www.xingwang.mbse/ontology#...'}).then(async v=>{
    const iri=(v||'').trim();
    if(iri===cur) return;
    const props = typeof t.properties==='string' ? safeParse(t.properties,{}) : (t.properties||{});
    const cons = typeof t.constraints==='string' ? safeParse(t.constraints,{}) : (t.constraints||{});
    const body = { name:t.name, type_kind:t.type_kind, properties:props, constraints:cons,
                   description:t.description||'', icon:t.icon||'', color:t.color||'#185FA5',
                   parent_id: t.parent_id ?? null, iri:iri };
    const r=await api('/api/knowledge/ontology/types/'+id, {method:'PUT', body:JSON.stringify(body)});
    if(r && r.error){ toast('保存失败：'+r.error); return; }
    toast('✅ IRI 已更新');
    loadOntology();
  });
}
// 右栏：Annotations / Instances / Usage（六轮调整：变更历史入口已移除，节点级走中栏 toggle 第四段）
function renderOntSide(){
  const box=document.getElementById('ont-side-body');
  // 四轮调整：仅编辑态渲染注释·引用（graf/owl 态右栏为详情面板）
  if(!box || ontPaneMode!=='edit') return;
  const t=ontCurType();
  if(!t){ box.innerHTML='<div style="color:var(--mut);font-size:11.5px;">未选择实体</div>'; return; }
  const isRel=t.type_kind==='relation';
  const b=(ontData.binding||[]).find(x=>x.entity_type===t.name)||{};
  let relAsSrc=[],relAsTgt=[];
  (ontData.types||[]).filter(x=>x.type_kind==='relation').forEach(r=>{
    const av=(r.constraints&&r.constraints.allowed_values)||{};
    if((Array.isArray(av.src)?av.src:(av.src?[av.src]:[])).includes(t.name)) relAsSrc.push(r.name);
    if((Array.isArray(av.tgt)?av.tgt:(av.tgt?[av.tgt]:[])).includes(t.name)) relAsTgt.push(r.name);
  });
  let h='';
  if(!isRel){
    h+=`<div style="font-size:11px;color:var(--mut);letter-spacing:.4px;">实例 <span style="letter-spacing:0;">Instances</span></div>
      <div style="margin-top:6px;"><span class="ont-chip"><b>${b.total||0} 个</b></span>
      <button class="btn sm ghost" style="margin-left:6px;padding:1px 8px;font-size:11px;" onclick="try{go('kb','kb-d')}catch(e){}" title="按当前工作分支加载实例子集（本体为全局 Schema，不随分支隔离）">在图谱工作区查看（${esc(getCurrentBranch())}）→</button></div>
      <div style="font-size:10.5px;color:var(--mut);margin-top:4px;">已评 ${b.reviewed||0} · 待审 ${b.candidate||0} · 全分支合计</div>`;
  }
  h+=`<div style="font-size:11px;color:var(--mut);letter-spacing:.4px;margin-top:14px;">注释 <span style="letter-spacing:0;">Annotations</span></div>
    <div style="font-size:12px;line-height:1.9;margin-top:6px;"><span class="ont-mono" style="color:var(--mut);font-size:11px;">rdfs:label</span><br>${esc(t.name)}</div>
    <div style="margin-top:4px;"><span class="ont-mono" style="color:var(--mut);font-size:11px;">rdfs:comment</span>
    <textarea id="ont-desc-edit" title="类型描述（失焦自动保存）" placeholder="类型用途/定义说明…" style="width:100%;min-height:64px;margin-top:3px;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;font-family:inherit;outline:none;resize:vertical;background:#fff;">${esc(t.description||'')}</textarea></div>
    <div style="margin-top:8px;font-size:11px;color:var(--mut);">外部映射 <span style="letter-spacing:0;">Profile</span></div>
    <input id="ont-prof-src" title="来源 Profile，如 SysML V2（失焦自动保存）" placeholder="来源，如 SysML V2" value="${esc(t.profile_source||'')}" style="width:100%;margin-top:3px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:11.5px;outline:none;background:#fff;">
    <input id="ont-prof-ref" title="引用元素，如 Part::PartDefinition / ISO 15288 条款（失焦自动保存）" placeholder="引用，如 Part::PartDefinition" value="${esc(t.profile_ref||'')}" style="width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:11.5px;outline:none;background:#fff;">`;
  if(!isRel && (relAsSrc.length||relAsTgt.length)){
    h+=`<div style="font-size:11px;color:var(--mut);letter-spacing:.4px;margin-top:14px;">引用 <span style="letter-spacing:0;">Usage · 关系引用</span></div>
      <div style="font-size:12px;line-height:1.9;margin-top:6px;">作为定义域：${relAsSrc.map(r=>`<span class="ont-chip"><b>${esc(r)}</b></span>`).join(' ')||'—'}<br>作为值域：${relAsTgt.map(r=>`<span class="ont-chip"><b>${esc(r)}</b></span>`).join(' ')||'—'}</div>`;
  }
  const isAttrT = t.type_kind==='attribute';
  if(!isAttrT){
    h+=`<div style="font-size:11px;color:var(--mut);letter-spacing:.4px;margin-top:14px;">别名 <span style="letter-spacing:0;">Glossary · 词法层</span></div>`;
    h+=`<div id="ont-alias-box" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px;"><span style="font-size:11px;color:var(--mut);">加载中…</span></div>`;
    h+=`<button class="btn sm ghost ont-act-btn" style="margin-top:6px;width:100%;font-size:11px;" onclick="ontAliasAdd()" title="添加别名后，抽取归一自动映射到该类型">＋ 添加别名</button>`;
  }
  // 六轮调整：右栏「📜 变更历史」入口移除——节点级变更历史统一走中栏 toggle 第四段，全局走顶栏
  box.innerHTML=h;
  if(!isAttrT) loadOntAliases(t);
  // 2026-09-08 编辑整合：注释/外部映射 失焦即存（本体即改即生效，替代滑窗整单填报）
  const descEl=document.getElementById('ont-desc-edit');
  if(descEl){
    descEl.onblur=async()=>{
      const nv=descEl.value.trim();
      if(nv===(t.description||'')) return;
      const ok=await ontPutType(t, t.constraints, undefined, {description:nv});
      if(!ok) descEl.value=t.description||'';
    };
  }
  const profSrc=document.getElementById('ont-prof-src'), profRef=document.getElementById('ont-prof-ref');
  const profSave=async()=>{
    if(!profSrc || !profRef) return;
    const src=profSrc.value.trim(), ref=profRef.value.trim();
    if(src===(t.profile_source||'') && ref===(t.profile_ref||'')) return;
    const ok=await ontPutType(t, t.constraints, undefined, {profile_source:src, profile_ref:ref});
    if(!ok){ profSrc.value=t.profile_source||''; profRef.value=t.profile_ref||''; }
  };
  if(profSrc) profSrc.onblur=profSave;
  if(profRef) profRef.onblur=profSave;
}
// R4：图谱工作区 → 本体类型 反向跳转（等待 kb-c 本体数据加载完成后选中）
function ontGotoType(name){
  if(!name){ toast('该实体未标注类型'); return; }
  go('kb','kb-c');
  const trySel = ()=>{
    if(ontData.types && ontData.types.length){ selectOntType(name); }
    else setTimeout(trySel, 300);
  };
  setTimeout(trySel, 500);
}
// R1：反查指向当前类型的词典词条（provenance.entity_type 锚定）
async function loadOntAliases(t){
  const box=document.getElementById('ont-alias-box'); if(!box) return;
  box.dataset.t=t.name;
  try{
    const r=await api('/api/knowledge/glossary/by-type?type='+encodeURIComponent(t.name));
    if(box.dataset.t!==t.name) return;
    const items=r.items||[];
    box.innerHTML = items.length
      ? items.map(a=>`<span class="ont-chip" title="${esc(a.kind)} · ${esc(a.user_term)} → ${esc(a.canonical_term)}"><b>${esc(a.user_term)}</b><span class="k">${esc(a.kind)}</span><span class="ont-x" onclick="ontAliasDel(${a.id},'${ontJs(a.user_term)}')" title="删除词条">×</span></span>`).join(' ')
      : '<span style="font-size:11px;color:var(--amb);">⚠ 零别名——抽取归一盲区，建议添加常用称呼</span>';
  }catch(e){ box.innerHTML='<span style="font-size:11px;color:var(--mut);">别名加载失败</span>'; }
}
async function ontAliasAdd(){
  const t=ontCurType(); if(!t) return;
  promptDialog({title:'添加别名', message:'为「'+t.name+'」添加词典别名（口语/缩写/错拼均可，抽取归一时自动映射到该类型）：', placeholder:'如：pay-load / 星上载荷'}).then(async v=>{
    const alias=(v||'').trim(); if(!alias) return;
    const kind = t.type_kind==='relation' ? 'predicate' : 'entity';
    const r=await api('/api/knowledge/glossary', {method:'POST', body:JSON.stringify({user_term:alias, canonical_term:t.name, kind:kind, entity_type:t.name})});
    if(r && r.error){ toast('添加失败：'+r.error); return; }
    if(r && r.anchored===false){ toast('⚠ 游离词条：'+(r.hint||''), 5000); }
    else toast('✅ 别名已入词典并锚定到类型');
    loadOntAliases(t);
  });
}
async function ontAliasDel(gid, term){
  if(!(await confirmDialog('删除词典词条「'+term+'」？'))) return;
  const r=await api('/api/knowledge/glossary/'+gid, {method:'DELETE'});
  if(r && r.error){ toast('删除失败：'+r.error); return; }
  toast('已删除'); const t=ontCurType(); if(t) loadOntAliases(t);
}
// ── B/C：通用列表 chip 操作（equivalent_to / disjoint_with / axioms / domain_classes）+ Data Properties ──
const XSD_TYPES = ['xsd:string','xsd:integer','xsd:decimal','xsd:double','xsd:boolean','xsd:date','xsd:dateTime','xsd:anyURI'];
const chr10 = String.fromCharCode(10);
async function ontDelListItem(key, val){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  cons[key]=(cons[key]||[]).filter(x=>x!==val);
  if(!cons[key].length) delete cons[key];
  await ontPutType(t, cons);
}
function ontInlineAddList(key, anchorEl){
  const isSelect = (key==='disjoint_with' || key==='domain_classes');
  const wrap=document.createElement('span');
  wrap.style.cssText='display:inline-flex;gap:4px;align-items:center;vertical-align:middle;';
  if(isSelect){
    // 2026-09-07 组件优化：可搜索 input+datalist
    const dlId='ont-dl-'+key+'-'+Date.now();
    const entOpts=(ontData.types||[]).filter(x=>x.type_kind==='entity')
      .map(x=>`<option value="${esc(x.name)}">`).join('');
    wrap.innerHTML='<input list="'+dlId+'" placeholder="输入/选择类…" class="ont-inline-inp" title="支持输入过滤"><datalist id="'+dlId+'">'+entOpts+'</datalist>';
  } else {
    const ph = key==='axioms' ? '公理表达式，如：毁伤半径 some "8m"' : '类表达式，如： :hasAttr exactly 1 "编号"';
    wrap.innerHTML='<input placeholder="'+ph+'" style="font-size:11.5px;padding:2px 8px;border:1px solid var(--line);border-radius:10px;width:190px;outline:none" class="ont-mono">';
  }
  anchorEl.replaceWith(wrap);
  let fired=false;
  const done=async()=>{
    if(fired) return; fired=true;
    const t=ontCurType();
    let ok=true;
    const v = wrap.querySelector('input').value.trim();
    if(v && t){
      if(isSelect && !(ontData.types||[]).some(x=>x.type_kind==='entity'&&x.name===v)){
        toast('未找到类型「'+v+'」'); ok=false;
      } else {
        const cons=JSON.parse(JSON.stringify(t.constraints||{}));
        cons[key]=cons[key]||[]; if(!cons[key].includes(v)) cons[key].push(v);
        ok=await ontPutType(t, cons);
      }
    }
    if(ok) return;
    const add=document.createElement('span'); add.className='ont-add';
    add.textContent = isSelect ? '＋' : (key==='axioms' ? '＋ 公理' : '＋ 表达式');
    add.title='添加'; add.onclick=function(){ ontInlineAddList(key, add); };
    if(document.body.contains(wrap)) wrap.replaceWith(add);
  };
  const el=wrap.querySelector('input,select');
  if(el.tagName==='SELECT'){
    el.onchange=done; el.onblur=()=>setTimeout(done,200);
  } else {
    el.onkeydown=e=>{ if(e.key==='Enter')done(); if(e.key==='Escape'){ el.value=''; done(); } };
    el.onblur=()=>setTimeout(done,200);
  }
  el.focus();
}
function ontInlineAddXsd(anchorEl){
  const wrap=document.createElement('span');
  wrap.style.cssText='display:inline-flex;gap:4px;align-items:center;vertical-align:middle;';
  wrap.innerHTML='<select style="font-size:11.5px;padding:2px 4px;border:1px solid var(--line);border-radius:6px;"><option value="">— 选择 XSD 类型 —</option>'+XSD_TYPES.map(x=>`<option value="${x}">${x}</option>`).join('')+'</select>';
  anchorEl.replaceWith(wrap);
  let fired=false;
  const sel=wrap.querySelector('select');
  const done=async()=>{
    if(fired) return; fired=true;
    const t=ontCurType();
    const v=sel.value;
    let ok=true;
    if(v && t){ const cons=JSON.parse(JSON.stringify(t.constraints||{})); cons.xsd_type=v; ok=await ontPutType(t, cons); }
    if(!ok && document.body.contains(wrap)){
      const add=document.createElement('span'); add.className='ont-add'; add.textContent='＋ XSD'; add.title='设置 XSD 类型';
      add.onclick=function(){ ontInlineAddXsd(add); }; wrap.replaceWith(add);
    }
  };
  sel.onchange=done; sel.onblur=()=>setTimeout(done,200);
  sel.focus();
}
async function ontDelListItemXsd(){
  const t=ontCurType(); if(!t) return;
  const cons=JSON.parse(JSON.stringify(t.constraints||{}));
  delete cons.xsd_type;
  await ontPutType(t, cons);
}
// Data Properties 的 Description（Domain 绑定 + XSD Range）
function renderOntDescAttr(t, cons){
  const domCls=cons.domain_classes||[];
  const xsd=cons.xsd_type||'';
  // 2026-09-08 编辑整合：头部=名称行内 + 生命周期 badge；「✏️ 编辑」仅滑窗降级模式渲染
  let h=`<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;">
    <span style="font-size:19px;font-weight:600;cursor:text;" id="ont-name-wrap" title="点击修改名称" onclick="ontInlineRename(this)">${esc(t.name)}</span>
    <span class="ont-mono" style="font-size:11.5px;color:var(--mut);">owl:DatatypeProperty</span>
    ${ontStatusBadge(t)}
    <span style="flex:1"></span><span id="ont-flash" style="display:none;font-size:11.5px;color:var(--grn);">已写回</span>
    ${ontSlideEnabled()?`<button class="btn sm ghost" onclick="ontEditTypeById(${t.id})" title="说明等复杂编辑（右侧滑窗）">✏️ 编辑</button>`:''}</div>`;
  // 六轮调整：🗑 删除按钮移除——与类/对象属性统一，删除走左栏树节点悬停 ✕
  h+=`<div style="display:flex;align-items:center;gap:6px;margin-top:14px;font-size:11.5px;color:var(--mut);">
    <span style="background:var(--blue-l);border:1px solid var(--line);padding:1px 6px;border-radius:4px;font-size:10.5px;font-weight:600;color:var(--blue-d);">IRI</span>
    <span class="ont-mono" style="color:var(--blue-d);cursor:pointer;word-break:break-all;" onclick="copyIri(${t.id})" title="点击复制 IRI">${esc(t.iri||'—')}</span>
    <button class="btn sm ghost" style="padding:0 6px;font-size:11px;" onclick="editEntityIri(${t.id})" title="修改 IRI">🔗</button>
  </div>`;
  // 六轮调整：段落顺序与对象属性统一为 WebProtégé 属性描述序：Equivalent To → SubProperty Of → Domain → Range → Characteristics → Disjoint With
  h+=`</div><div class="ont-fcard"><div class="ont-sec">等价类 <span style="letter-spacing:0;">Equivalent To · 预留</span></div><div><span style="font-size:11.5px;color:var(--mut);">—</span></div>`;
    h+=`</div><div class="ont-fcard"><div class="ont-sec">定义域 <span style="letter-spacing:0;">Domain · 可绑定类</span></div><div>${domCls.map(x=>ontChipHtml(esc(x),'',`ontDelListItem('domain_classes','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('domain_classes',this)" title="绑定可使用该属性的类">＋</span></div>`;
  h+=`</div><div class="ont-fcard"><div class="ont-sec">值域 <span style="letter-spacing:0;">Range · XSD 类型</span></div><div>${xsd?ontChipHtml(xsd,'',`ontDelListItemXsd()`):`<span style="font-size:11.5px;color:var(--mut);">未设置</span>`} <span class="ont-add" onclick="ontInlineAddXsd(this)" title="设置 XSD 数据类型">＋ XSD</span></div>`;
  // 2026-09-08 编辑整合：补齐 允许值 / 单位量纲（原仅滑窗可编辑）
  h+=`</div><div class="ont-fcard"><div class="ont-sec">允许值 <span style="letter-spacing:0;">Allowed Values · 白名单（留空=不约束）</span></div><div>${(Array.isArray(cons.allowed_values)?cons.allowed_values:[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('allowed_values','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('allowed_values',this)" title="添加允许值（实例取值将在此白名单内校验）">＋</span></div>`;
  h+=`</div><div class="ont-fcard"><div class="ont-sec">单位 · 量纲 <span style="letter-spacing:0;">Unit · Quantity Kind（ISO 80000/SI）</span><span class="help-ico" style="cursor:help;font-size:10px;color:var(--blue);" title="设置顺序：先选量纲类别（如 频率），再从级联下拉选单位（SI 一贯单位+常用倍数，如 Hz/kHz/MHz/GHz）；均可「自定义…」自由输入。点「＋ 设置 / 修改」打开编辑器">ⓘ</span></div><div>
    ${cons.unit?ontChipHtml('单位: '+esc(cons.unit),'',`ontDelField('unit')`):''}
    ${cons.quantity_kind?ontChipHtml('量纲: '+esc(cons.quantity_kind),'',`ontDelField('quantity_kind')`):''}
    ${(!cons.unit&&!cons.quantity_kind)?'<span style="font-size:11.5px;color:var(--mut);">未设置</span>':''}
    <span class="ont-add" onclick="ontUnitEditor(this)" title="选择单位与量纲：先选量纲类别，单位随级联下拉（ISO 80000/SI 标准，可自定义）">＋ 设置 / 修改</span></div>`;
  h+=`</div><div class="ont-fcard"><div class="ont-sec">特征 <span style="letter-spacing:0;">Characteristics · 推理性质（数据属性仅函数型）</span>${ontCharHelp(true)}</div><div style="display:flex;flex-wrap:wrap;gap:4px;">${ontCharChips(cons,'attr')}</div>`;
  h+=`</div><div class="ont-fcard"><div class="ont-sec">互斥 <span style="letter-spacing:0;">Disjoint With</span></div><div>${(cons.disjoint_with||[]).map(x=>ontChipHtml(esc(x),'',`ontDelListItem('disjoint_with','${ontJs(x)}')`)).join(' ')}<span class="ont-add" onclick="ontInlineAddList('disjoint_with',this)" title="选择互斥的数据属性">＋</span></div>`;
  h+='</div>';   // 2026-09-11 分组卡片：闭合最后一个 .ont-fcard
  return h;
}
// 底部 RDF 折叠条（当前实体 Turtle 片段）
// 底部 RDF 预览条相关函数（ontRdfBarToggle/ontTurtleSnippet/renderRdfBar）已移除（2026-09-07）：与中栏「OWL」视图重复
// FR-KG-4 补 G7：本体类型变更历史（详情面板 📜 变更历史 折叠区，首次点击拉取并展示）
const ONT_ACT_BADGE = {add:['新增','var(--grn)'], update:['修改','var(--blue)'], delete:['删除','var(--red)']};
function ontChangeSummary(log) {
  // 变更摘要：add/delete 直接读快照行名；update 对比 before/after 关键字段
  const b = log.before || {}, a = log.after || {};
  if(log.action === 'add') return `新增类型「${a.name||''}」（${a.type_kind||''}）`;
  if(log.action === 'delete') return `删除类型「${b.name||''}」（${b.type_kind||''}）`;
  const lines = [];
  if((b.name||'') !== (a.name||'')) lines.push(`名称：${b.name||'—'} → ${a.name||'—'}`);
  if(String(b.parent_id||'') !== String(a.parent_id||'')) lines.push('父类型变更');
  if((b.description||'') !== (a.description||'')) lines.push('描述变更');
  if((b.properties||'') !== (a.properties||'')) lines.push('属性变更');
  if((b.constraints||'') !== (a.constraints||'')) lines.push('约束变更');
  if((b.icon||'') !== (a.icon||'')) lines.push('图标变更');
  if((b.color||'') !== (a.color||'')) lines.push('颜色变更');
  return lines.length ? lines.join('；') : '字段更新';
}
const ONT_ATTR_ESC = s => esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
function ontUsageChips(list){
  if(!list || !list.length) return '<span style="color:var(--mut);">无</span>';
  return list.map(r=>'<span class="tag ont-usage-jump" style="margin:1px 2px;cursor:pointer;" data-nm="'+ONT_ATTR_ESC(r.name)+'" onclick="selectOntType(this.dataset.nm)">'+esc(r.name)+'</span>').join('');
}
function ontUsageHtml(u){
  return [
    '<div class="kv"><span>子类型（继承本类型）</span><b>'+ontUsageChips(u.as_parent)+'</b></div>',
    '<div class="kv"><span>作为来源的关系</span><b>'+ontUsageChips(u.as_source)+'</b></div>',
    '<div class="kv"><span>作为目标的关系</span><b>'+ontUsageChips(u.as_target)+'</b></div>',
    '<div class="kv"><span>实例化数量</span><b style="color:'+((u.instances||0)?'var(--grn)':'var(--mut)')+'">'+(u.instances||0)+'</b> <small style="color:var(--mut);">（全分支 · 非废弃）</small></div>'
  ].join('');
}
// 📜 全局变更历史（2026-09-02 二轮调整：顶部 Tab 行入口，替代隐藏的个体 Tab；聚合全部类型的 changelog 倒序）
// 📜 变更历史（2026-09-02 四轮调整：WebProtégé Changes 式中栏内联展示，右侧滑窗不再使用）
function ontClKindLabel(k){ return k==='relation'?'object property':(k==='attribute'?'data property':'class'); }
function ontClTname(id, fb){
  const t=(ontData.types||[]).find(x=>x.id===id);
  return t?t.name:(fb||('类型#'+id));
}
function ontClTimeago(s){
  // created_at 形如 "YYYY-MM-DD HH:MM:SS"（UTC）→ 相对时间；解析失败回退原串
  try{
    const ms = Date.parse(s.replace(' ','T')+'Z');
    if(isNaN(ms)) return esc(s||'');
    const diff = Math.max(0, Date.now()-ms);
    const m = Math.floor(diff/60000);
    if(m < 1) return 'just now';
    if(m < 60) return m + ' minute' + (m>1?'s':'') + ' ago';
    const h = Math.floor(m/60);
    if(h < 24) return h + ' hour' + (h>1?'s':'') + ' ago';
    const d = Math.floor(h/24);
    return d + ' day' + (d>1?'s':'') + ' ago';
  }catch(e){ return esc(s||''); }
}
function ontClRow(kind, text){
  const plus = kind==='+';
  return `<div style="display:flex;align-items:baseline;gap:7px;padding:4px 10px;font-size:11.5px;font-family:ui-monospace,Consolas,monospace;${plus?'':'background:#fdf3f3;'}">
    <span style="flex:none;font-weight:700;color:${plus?'#2f855a':'var(--red)'};">${plus?'＋':'−'}</span>
    <span style="min-width:0;word-break:break-all;">${text}</span></div>`;
}
function ontClRows(l){
  const b=l.before||{}, a=l.after||{};
  const nm = a.name||b.name||'';
  const rows=[];
  if(l.action==='add'){
    rows.push(ontClRow('+', esc(ontClKindLabel(a.type_kind))+' <b>'+esc(nm)+'</b>'));
    if(a.parent_id) rows.push(ontClRow('+', '<b>'+esc(nm)+'</b> subClassOf '+esc(ontClTname(a.parent_id))));
  } else if(l.action==='delete'){
    rows.push(ontClRow('-', esc(ontClKindLabel(b.type_kind))+' <b>'+esc(b.name||'')+'</b>'));
  } else {
    if((b.name||'')!==(a.name||'')){
      if(b.name) rows.push(ontClRow('-', 'rdfs:label "'+esc(b.name)+'"'));
      if(a.name) rows.push(ontClRow('+', 'rdfs:label "'+esc(a.name)+'"'));
    }
    if(String(b.parent_id||'')!==String(a.parent_id||'')){
      if(b.parent_id) rows.push(ontClRow('-', '<b>'+esc(nm)+'</b> subClassOf '+esc(ontClTname(b.parent_id))));
      if(a.parent_id) rows.push(ontClRow('+', '<b>'+esc(nm)+'</b> subClassOf '+esc(ontClTname(a.parent_id))));
    }
    if((b.description||'')!==(a.description||'')){
      const cut = s => (s||'').length>40 ? (s||'').slice(0,40)+'…' : (s||'');
      if(b.description) rows.push(ontClRow('-', 'rdfs:comment "'+esc(cut(b.description))+'"'));
      if(a.description) rows.push(ontClRow('+', 'rdfs:comment "'+esc(cut(a.description))+'"'));
    }
    const cnt = v => { try{ return Object.keys((typeof v==='string'?JSON.parse(v||'{}'):v)||{}).length; }catch(e){ return 0; } };
    if((b.properties||'')!==(a.properties||'')){
      rows.push(ontClRow('-', '属性表（'+cnt(b.properties)+' 项）'));
      rows.push(ontClRow('+', '属性表（'+cnt(a.properties)+' 项）'));
    }
    if((b.constraints||'')!==(a.constraints||'')){
      rows.push(ontClRow('-', '约束定义（旧）'));
      rows.push(ontClRow('+', '约束定义（新）'));
    }
    if(!rows.length) rows.push(ontClRow('+', esc(ontChangeSummary(l))));
  }
  return rows.join('');
}
// 变更历史分组渲染（五轮调整抽出共用）：WebProtégé 式——同操作人+同分钟+同类型+同动作 合并为一组（一次提交）
function ontClGroupsHtml(list){
  if(!Array.isArray(list) || !list.length) return '<div style="color:var(--mut);font-size:12px;padding:12px 0;">暂无变更记录（新增/修改/删除本体类型后会自动记录）</div>';
  const groups=[];
  list.forEach(l=>{
    const key = (l.operator||'')+'|'+String(l.created_at||'').slice(0,16)+'|'+l.type_id+'|'+l.action;
    const last = groups[groups.length-1];
    if(last && last.key===key) last.items.push(l);
    else groups.push({key, items:[l]});
  });
  return groups.map(g=>{
    const f=g.items[0];
    const b=f.before||{}, a=f.after||{};
    const nm = a.name||b.name||ontClTname(f.type_id);
    let title;
    if(f.action==='add') title = 'Created '+ontClKindLabel(a.type_kind)+' <b>'+esc(nm)+'</b>';
    else if(f.action==='delete') title = 'Deleted '+ontClKindLabel(b.type_kind)+' <b>'+esc(b.name||'')+'</b>';
    else title = 'Edited <b>'+esc(nm)+'</b>';
    const av = (f.operator||'?').trim().charAt(0).toUpperCase()||'?';
    return `<div style="margin-bottom:16px;">
      <div style="font-size:12.5px;color:var(--tx,#333);">${title}</div>
      <div style="display:flex;align-items:center;gap:7px;margin:5px 0 6px;">
        <span style="flex:none;width:22px;height:22px;border-radius:50%;background:var(--blue);color:#fff;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;">${esc(av)}</span>
        <span style="font-size:11px;color:var(--mut);">${esc(f.operator||'—')} authored ${g.items.length} change${g.items.length>1?'s':''} · ${ontClTimeago(f.created_at)}</span>
      </div>
      <div style="border:1px solid var(--line);border-radius:6px;overflow:hidden;background:#fff;">${g.items.map(ontClRows).join('')}</div>
    </div>`;
  }).join('');
}
// 节点级变更历史视图（toggle 第四段「OWL 右侧」，五轮调整）：基于左栏当前所选对象
async function renderOntNodeChangeLog(){
  const box=document.getElementById('ont-nodecl');
  if(!box) return;
  const t=ontCurType();
  if(!t){ box.innerHTML='<div style="color:var(--mut);font-size:12px;padding:20px;">从左侧选择一个类型后查看其变更历史</div>'; return; }
  const kindName=t.type_kind==='relation'?'对象属性':(t.type_kind==='attribute'?'数据属性':'类');
  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap;">
      <b style="font-size:12.5px;color:var(--blue-d);">📜 变更历史</b>
      <span class="tag">${esc(t.icon||'🧬')} ${esc(t.name)} · ${kindName}</span>
      <span style="flex:1"></span>
      <button class="btn sm ghost" onclick="renderOntNodeChangeLog()" title="重新加载">🔄 刷新</button>
    </div>
    <div style="flex:none;font-size:11px;color:var(--mut);background:var(--blue-l);border-bottom:1px solid var(--line);padding:4px 14px;">
      当前所选对象（${esc(t.name)}）的类型级变更记录；全局变更历史见顶栏「📜 变更历史」。
    </div>
    <div style="flex:1;min-height:0;overflow-y:auto;padding:14px 18px;"><div style="color:var(--mut);font-size:12px;">加载中…</div></div>`;
  const listBox = box.lastElementChild;
  try{
    const list = await api(`/api/knowledge/ontology/changelog?type_id=${t.id}`);
    listBox.innerHTML = Array.isArray(list) ? ontClGroupsHtml(list)
      : '<div style="color:var(--red);font-size:12px;">加载失败：'+esc((list&&list.error)||'未知错误')+'</div>';
  }catch(e){
    listBox.innerHTML = '<div style="color:var(--red);font-size:12px;">加载失败：'+esc(e.message||e)+'</div>';
  }
}
async function openOntChangeLog(){
  // 四轮调整：进入中栏全局内联视图（WebProtégé Changes 式分组列表）
  ontEnterGlobal('changelog');
  const gv=document.getElementById('ont-globalview');
  if(!gv) return;
  gv.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap;">
      <b style="font-size:12.5px;color:var(--blue-d);">📜 变更历史</b>
      <span class="tag">全局 · 本体类型级变更记录</span>
      <span style="flex:1"></span>
      <button class="btn sm ghost" onclick="openOntChangeLog()" title="重新加载">🔄 刷新</button>
    </div>
    <div style="flex:1;min-height:0;overflow-y:auto;padding:14px 18px;"><div style="color:var(--mut);font-size:12px;">加载中…</div></div>`;
  const listBox = gv.lastElementChild;
  try{
    const list = await api('/api/knowledge/ontology/changelog');
    if(!Array.isArray(list)){
      listBox.innerHTML = '<div style="color:var(--red);font-size:12px;">加载失败：'+esc((list&&list.error)||'未知错误')+'</div>';
      return;
    }
    listBox.innerHTML = ontClGroupsHtml(list);   // 空列表由 ontClGroupsHtml 内置空态
  }catch(e){
    listBox.innerHTML = '<div style="color:var(--red);font-size:12px;">加载失败：'+esc(e.message||e)+'</div>';
  }
}
// 六轮调整：toggleOntChangeLog（右栏折叠区）已移除——入口按钮不复存在，节点级变更历史统一走中栏 toggle 第四段
// 左栏/详情按钮：按 id 查全局数据，避免 onclick 字符串转义问题
function ontEditTypeById(id) {
  const t = ontData.types.find(x=>x.id===id);
  if(!t) return;
  ontologyEditType(t.id, t.name, t.type_kind, JSON.stringify(t.constraints||{}));
}
// ── 图谱缩放/平移（本体类型约束图谱）──
let oiPanOn = false, oiPanStartX = 0, oiPanStartY = 0, oiPanOrigX = 0, oiPanOrigY = 0;
// 全局图谱视图（共享 zoom/pan）
const graphView = {scale: 1, panX: 0, panY: 0};
let ogFitKey = '';   // 2026-09-07 筛选/聚焦视图切换检测：变更时重置 graphView（内层 g 变换），避免全量 fit 缩放叠加
