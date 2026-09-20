// ── V3 产物下拉浮层：在预览面板头部下拉展开 ──
function toggleProdDropdown(){
  const panel = document.getElementById('art-list-panel');
  const trigger = document.getElementById('prod-trigger');
  if(!panel || !trigger) return;
  // 已钉住时直接显示，不再走浮层逻辑
  if(panel.classList.contains('pinned')) return;
  const isOpen = panel.classList.toggle('open');
  trigger.classList.toggle('active', isOpen);
  if(isOpen && currentConvId){
    loadArtifacts(currentConvId);
  }
}
// 点击外部关闭产物浮层（钉住态不受影响）
document.addEventListener('click', (e) => {
  const panel = document.getElementById('art-list-panel');
  const trigger = document.getElementById('prod-trigger');
  if(!panel || !panel.classList.contains('open')) return;
  if(!panel.contains(e.target) && !trigger.contains(e.target)){
    panel.classList.remove('open');
    trigger.classList.remove('active');
  }
});

// ── V3 图钉：钉住/取消钉住产物面板 ──
function togglePinProd(){
  const panel = document.getElementById('art-list-panel');
  const preview = document.getElementById('preview-panel');
  const pinBtn = panel ? panel.querySelector('.pin-btn') : null;
  if(!panel) return;
  const isPinned = panel.classList.toggle('pinned');
  if(isPinned){
    panel.classList.remove('open');
    if(preview) preview.classList.add('pinned');
    if(pinBtn){ pinBtn.classList.add('pinned'); pinBtn.title='取消钉住'; }
    if(currentConvId) loadArtifacts(currentConvId);
  } else {
    if(preview) preview.classList.remove('pinned');
    if(pinBtn){ pinBtn.classList.remove('pinned'); pinBtn.title='钉住/取消钉住'; }
  }
  localStorage.setItem('mbse_prod_pinned', isPinned ? '1' : '0');
  syncPreviewReopen();
}
// 页面加载时恢复折叠状态 + 预览面板默认收起
(function(){
  // 2026-09-04 v2：旧 conv-sidebar 已并入 gnav-task；旧 mbse_conv_collapsed 折叠态迁移到任务分组
  if(localStorage.getItem('mbse_task_collapsed') === null
     && localStorage.getItem('mbse_conv_collapsed') === '1'){
    localStorage.setItem('mbse_task_collapsed', '1');
  }
  const _tg = document.getElementById('gnav-task');
  if(_tg && localStorage.getItem('mbse_task_collapsed')==='1'){
    _tg.classList.add('collapsed');
  }
  // 预览面板默认不展示（每次进入页面均默认收起）：
  // 不再恢复 mbse_preview_width 到 inline style——inline 宽度优先级高于 collapsed 类，会导致默认展开且收不起来；
  // 保存的宽度只在用户展开面板时（togglePreview / renderPreviewPanel）再应用。
  const pp = document.getElementById('preview-panel');
  if(pp){pp.classList.add('collapsed');}
  const btn = document.getElementById('preview-toggle');
  if(btn){btn.classList.add('collapsed');}
  // 注意：不再恢复产物面板钉住态（mbse_prod_pinned）——钉住态会强制展开预览面板，违反"默认不展示"；
  // 钉住改为会话内临时态，刷新后回到默认收起。
  syncPreviewReopen();
})();

// 展开预览面板时恢复用户上次拖拽保存的宽度（默认收起时不应用，避免 inline 宽度覆盖 collapsed）
function previewApplySavedWidth(){
  const pp = document.getElementById('preview-panel');
  if(!pp || pp.classList.contains('collapsed')) return;
  const savedW = localStorage.getItem('mbse_preview_width');
  if(savedW){
    const w = parseInt(savedW,10);
    if(w >= 280 && w <= 800) pp.style.width = w + 'px';
  }
}
// 收起预览面板时清除 inline 宽度，确保 collapsed（width:0 !important）彻底生效
function previewClearWidth(){
  const pp = document.getElementById('preview-panel');
  if(pp) pp.style.width = '';
}
// 收起状态下的展开入口按钮：面板收起时显示，展开时隐藏
function syncPreviewReopen(){
  const pp = document.getElementById('preview-panel');
  const btn = document.getElementById('preview-reopen');
  if(!pp || !btn) return;
  btn.classList.toggle('show', pp.classList.contains('collapsed'));
}
// ── 切换预览面板展开/收起 ──
function togglePreview(){
  const pp = document.getElementById('preview-panel');
  if(!pp) return;
  const isCollapsed = pp.classList.toggle('collapsed');
  if(isCollapsed){
    // 收起时同步解除钉住态，避免 pinned 布局（600px 宽）覆盖 collapsed 导致面板收不起来
    pp.classList.remove('pinned');
    const panel = document.getElementById('art-list-panel');
    if(panel) panel.classList.remove('pinned');
    const pinBtn = panel ? panel.querySelector('.pin-btn') : null;
    if(pinBtn){ pinBtn.classList.remove('pinned'); pinBtn.title='钉住/取消钉住'; }
    localStorage.setItem('mbse_prod_pinned','0');
    previewClearWidth();
  } else {
    previewApplySavedWidth();
  }
  localStorage.setItem('mbse_preview_collapsed', isCollapsed ? '1' : '0');
  const btn = document.getElementById('preview-toggle');
  if(btn) btn.classList.toggle('collapsed', isCollapsed);
  syncPreviewReopen();
}

// ── V3 拖拽：调整 chat-area ↔ preview-panel 宽度（产物列表已移入为浮层）──
let _previewDrag = null;
function previewDragStart(e){
  if(e.button !== 0) return;
  e.preventDefault();
  const panel = document.getElementById('preview-panel');
  if(!panel) return;
  panel.style.transition = 'none';
  const divider = document.getElementById('preview-divider');
  if(divider) divider.classList.add('active');
  document.body.classList.add('preview-dragging');
  _previewDrag = { startX: e.clientX, startW: panel.getBoundingClientRect().width, panel };
  document.addEventListener('mousemove', previewDragMove);
  document.addEventListener('mouseup', previewDragEnd);
}
function previewDragMove(e){
  if(!_previewDrag) return;
  // 手柄向右拖 → preview-panel 变窄（手柄右移，聊天区变大）；向左拖 → 变宽
  const dx = e.clientX - _previewDrag.startX;
  let newW = _previewDrag.startW - dx;
  if(newW < 280) newW = 280;
  if(newW > 800) newW = 800;
  _previewDrag.panel.style.width = newW + 'px';
}
function previewDragEnd(){
  if(!_previewDrag) return;
  const panel = _previewDrag.panel;
  const divider = document.getElementById('preview-divider');
  if(divider) divider.classList.remove('active');
  document.body.classList.remove('preview-dragging');
  panel.style.transition = '';
  const w = parseInt(panel.style.width,10);
  if(w) localStorage.setItem('mbse_preview_width', w + 'px');
  document.removeEventListener('mousemove', previewDragMove);
  document.removeEventListener('mouseup', previewDragEnd);
  _previewDrag = null;
}

// ── 会话产物分栏（V3：由 art-list-panel + preview-panel 替代原 art-side）──
let _artConv = null, _artKind = '', _artActive = null;
let _artFiles = [];  // 2026-09-01：会话导入文件（消息附件合并，分组展示于文件清单）
// 2026-09-18（S5 复检新增）：_artList 此前全仓从未声明，仅在 loadArtifacts() 的 try 内裸赋值；
// 一旦产物接口失败（被 catch 静默）或尚未加载，openPreviewTab→renderArtV3List 读取未声明的隐式
// 全局即抛 ReferenceError（09-impact.js 早已用 (_artList||[]) 绕过该坑）。此处补声明，降级为空列表。
let _artList = [];
let _previewTabs = [];        // 预览区已打开的多文件 tab（顶部切换，可关闭）
let _previewActiveKey = null; // 当前激活 tab key
let _convTabs = {};           // 会话预览记忆：convId → {tabs, activeKey}，切回会话时恢复
const _ART_ICONS = {report:'📑', code:'💻', sysml:'🕸', document:'📄', image:'🖼', other:'📎'};
const _ART_KINDS = [['report','报告'],['code','代码'],['sysml','SysML'],['document','文档'],['image','图片']];
// （2026-09-18 S5 收正）原「分界线拖拽调整产物分栏宽度」的 artApplyWidth / artDragMove /
// artDragEnd 及模块级 _artDrag 已随 V3 重构失效：其目标 #art-side / #art-divider 在 DOM 中
// 已不存在（仅 app.css 保留 `#art-side{display:none}` 兼容规则），拖拽能力现由
// previewDragStart / previewDragMove / previewDragEnd 绑定 #preview-divider 承担 —— 属被
// 活实现严格取代，故移除；此处不再保留同名变量以免误导。
function resetArtPreview(){
  _artActive = null;
  _previewTabs = [];
  _previewActiveKey = null;
  const bar = document.getElementById('preview-tabs');
  if(bar){ bar.innerHTML=''; bar.style.display='none'; }
  const pv = document.getElementById('art-preview');
  if(pv) pv.innerHTML = '<div style="color:var(--mut);font-size:11px;">点击上方产物查看预览</div>';
  const titleEl = document.getElementById('preview-title');
  if(titleEl) titleEl.textContent = '点击文件查看';
  const bodyEl = document.getElementById('preview-content');
  if(bodyEl) bodyEl.innerHTML = '<div style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--mut);font-size:12px;text-align:center;">点击「🗂 文件」下拉列表<br>在此处预览内容</div>';
}
async function loadArtifacts(convId){
  if(!convId) return;
  _artConv = convId;
  // V3: 不再强制展开 art-side，产物列表独立
  try{
    const [items, stats, msgRes] = await Promise.all([
      api('/api/artifacts?conversation_id='+convId+(_artKind?'&kind='+encodeURIComponent(_artKind):'')),
      api('/api/artifacts/stats?conversation_id='+convId),
      api(`/api/conversations/${convId}/messages?limit=50`),
    ]);
    _artList = items || [];
    _artFiles = collectAttachFiles(msgRes && msgRes.messages);
    renderArtV3List(stats || {total:0});
  }catch(e){ /* 静默：文件分栏不阻断会话 */ }
}
// 2026-09-01：会话消息附件 → 文件清单项（url 去重；AI 建模导入的文件也进文件清单）
function collectAttachFiles(msgs){
  const seen = new Set(), out = [];
  (msgs||[]).forEach(m=>{
    let atts = [];
    try{ atts = typeof m.attachments==='string' ? JSON.parse(m.attachments||'[]') : (m.attachments||[]); }catch(e){ atts=[]; }
    (atts||[]).forEach(a=>{
      if(!a || !a.url || seen.has(a.url)) return;
      seen.add(a.url);
      out.push({id:'attach:'+a.url, kind:'file', kind_label:'文件', title:a.filename||'附件',
                url:a.url, is_image:!!a.is_image, size:a.size||0, source:'import', message_id:m.id});
    });
  });
  return out;
}
// 2026-09-01：文件项预览（图片直显 / 文本 fetch 前 40KB）
async function artFilePreview(f){
  if(!f) return;
  if(f.is_image){
    openPreviewTab({id:null, kind:'image', kind_label:'文件', title:f.title,
      file_url:f.url, preview_content:f.url, content:'', meta:{}, message_id:f.message_id});
    return;
  }
  try{
    const txt = await fetch(f.url).then(r=>r.text());
    const slice = txt.slice(0, 40000);
    openPreviewTab({id:null, kind:'code', kind_label:'文件', title:f.title,
      preview_type:'text', preview_content:slice, content:slice, meta:{},
      message_id:f.message_id, file_url:f.url});
    if(txt.length > 40000) toast('文件较大，预览前 40KB（完整内容可下载）');
  }catch(e){ toast('文件预览失败：'+(e.message||e)); }
}
// 2026-09-01：文件项加入会话（注入引用 chip，复用 chatRefs 通道）
async function artFileAddToChat(f){
  if(!f) return;
  try{
    let text = '';
    if(!f.is_image){
      try{ text = await fetch(f.url).then(r=>r.text()); }catch(e){ text=''; }
    }
    if(!String(text).trim()) text = '[附件] ' + (f.title||'');
    injectChatRef(String(text).slice(0, 4000), '文件 · '+(f.title||''));
    toast('📎 已加入会话（'+(f.title||'')+'），输入问题后发送');
  }catch(e){ toast('加入会话失败：'+(e.message||e)); }
}
// 2026-09-01：文件项右键菜单（加入会话 / 打开 / 另存为）
function artFileContextMenu(ev, idx){
  ev.preventDefault(); ev.stopPropagation();
  const f = (_artFiles||[])[idx];
  if(!f) return;
  const m = document.createElement('div');
  m.id = 'art-file-ctx-menu';
  m.style.cssText = 'position:fixed;z-index:6000;min-width:150px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.14);padding:4px;font-size:12px;';
  const item = (label, fn) => '<div class="rm-item" style="padding:6px 10px;border-radius:6px;" onclick="'+fn+'">'+label+'</div>';
  m.innerHTML = item('📎 加入会话', `artFileAddToChatByIdx(${idx})`)
    + item('📖 打开', `artFilePreviewByIdx(${idx})`)
    + item('💾 另存为', `artFileSaveAsByIdx(${idx})`);
  document.body.appendChild(m);
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(ev.clientX, window.innerWidth - w - 8) + 'px';
  m.style.top = Math.min(ev.clientY, window.innerHeight - h - 8) + 'px';
}
function artFilePreviewByIdx(i){ const f=(_artFiles||[])[i]; if(f) artFilePreview(f); }
function artFileAddToChatByIdx(i){ const f=(_artFiles||[])[i]; if(f) artFileAddToChat(f); }
function artFileSaveAsByIdx(i){
  const f=(_artFiles||[])[i]; if(!f) return;
  const a=document.createElement('a'); a.href=f.url; a.download=f.title||'file';
  document.body.appendChild(a); a.click(); a.remove();
  toast('💾 开始下载：'+(f.title||''));
}
document.addEventListener('click', ()=>{ const m=document.getElementById('art-file-ctx-menu'); if(m) m.remove(); });
// ── V3 文件列表：分组（AI 生成产物 / 导入文件）+ 上下列表 ──
function renderArtV3List(stats){
  const el = document.getElementById('art-list-v3');
  if(!el) return;
  const arts = [..._artList].sort((a,b)=> new Date(b.created_at||0) - new Date(a.created_at||0));
  const files = _artFiles || [];
  if(!arts.length && !files.length){
    el.innerHTML = `<div style="color:var(--mut);font-size:11px;padding:12px 4px;text-align:center;">暂无文件</div>`;
    return;
  }
  const item = (a, icon, fmt) => `<div class="art-list-item ${_artActive===a.id?'active':''}" onclick="${a.onclick||('artPreviewV3('+a.id+')')}" oncontextmenu="${a.onctx||'artContextMenu(event,'+a.id+')'}" title="${esc(a.title)} (${fmt}) · 右键：加入会话/打开/另存为">
    <span class="art-icon">${icon}</span><span class="art-name">${esc(a.title)}</span><span class="art-format">${esc(fmt)}</span></div>`;
  const group = (title, list) => list.length ?
    `<div style="font-size:10.5px;color:var(--mut);padding:8px 4px 4px;letter-spacing:.5px;border-top:${_artList.length?'1px dashed var(--line)':'none'};margin-top:${_artList.length?'4px':'0'};">${title}（${list.length}）</div>` +
    list.map(x=>item(x, _ART_ICONS[x.kind]||'📎', x.kind_label||x.kind||'FILE')).join('') : '';
  el.innerHTML = group('AI 生成产物', arts) + group('导入文件', files.map(f=>Object.assign(f, {
    onclick:'artFilePreviewByIdx('+(_artFiles.indexOf(f))+')',
    onctx:'artFileContextMenu(event,'+(_artFiles.indexOf(f))+')'
  })));
  // 更新头部计数
  const headEl = document.querySelector('#art-list-panel .side-head-text');
  if(headEl) headEl.textContent = `🗂 文件 (${arts.length + files.length})`;
}
// [S5 2026-09-18 保留] 可达性分析判定不可达（无任何引用）；但「按 kind 过滤产物列表」这一能力
// 在 V3 面板里没有对应实现（_artKind 现在只被 loadArtifacts 读、无处可设）—— 属「无活后继」，
// 按 S5 判定标准不删，留待产品决策（要保留能力就补 V3 的筛选入口，不要就走移除流程）。
function artSetKind(kind){
  _artKind = kind;
  resetArtPreview();
  if(_artConv) loadArtifacts(_artConv);
}
// ── V3 文件预览：写入右侧 #preview-panel，自动展开预览面板 ──
async function artPreviewV3(id){
  try{
    const a = await api('/api/artifacts/'+id);
    _artActive = id;
    renderArtV3List(); // 更新列表高亮
    renderPreviewPanel(a); // 写入预览面板 + 自动展开
    // 选中后自动关闭产物浮层
    const panel = document.getElementById('art-list-panel');
    const trigger = document.getElementById('prod-trigger');
    if(panel && panel.classList.contains('open')){
      panel.classList.remove('open');
      if(trigger) trigger.classList.remove('active');
    }
  }
  catch(e){ toast('文件加载失败'); }
}
// 兼容旧调用：打开（或激活）文件预览 tab
function renderPreviewPanel(a){ openPreviewTab(a); }
// 会话内报告预览：报告卡片「📄 完整报告」统一在右侧预览区以 tab 打开（替代原右侧弹窗 openPanel）
function openPreviewReport(title, html){
  openPreviewTab({id:null, kind:'report', kind_label:'报告', title: title || '报告',
    preview_type:'html', preview_content: html || '', content:'', message_id:null, meta:{}});
}
// 打开一个文件预览 tab（已打开则激活并刷新内容），支持顶部多 tab 切换
function openPreviewTab(a){
  const pp = document.getElementById('preview-panel');
  // 自动展开预览面板
  if(pp && pp.classList.contains('collapsed')){
    pp.classList.remove('collapsed');
    localStorage.setItem('mbse_preview_collapsed','0');
    const btn = document.getElementById('preview-toggle');
    if(btn) btn.classList.remove('collapsed');
  }
  previewApplySavedWidth();
  syncPreviewReopen();
  const key = previewTabKey(a);
  let tb = _previewTabs.find(t=>t.key===key);
  if(!tb){
    tb = {key, id: a.id||null, title: a.title||'文件预览', kind: a.kind||'other', kind_label: a.kind_label||a.kind||'',
          preview_type: a.preview_type||'', preview_content: a.preview_content||'', content: a.content||'',
          meta: a.meta||{}, message_id: a.message_id||null, file_url: a.file_url||''};
    _previewTabs.push(tb);
  } else {
    // 同 key 再次打开：刷新内容与标题
    tb.title = a.title||tb.title; tb.kind_label = a.kind_label||a.kind||tb.kind_label;
    tb.preview_content = a.preview_content||tb.preview_content; tb.content = a.content||tb.content;
    tb.meta = a.meta||tb.meta; tb.file_url = a.file_url||tb.file_url;
  }
  _previewActiveKey = key;
  if(a.id) _artActive = a.id;
  renderArtV3List();
  renderPreviewTabs();
  renderPreviewBody(tb);
}
function previewTabKey(a){
  if(a && a.id) return 'art:' + a.id;
  // 无产物 id（消息级/报告/视图直开）：标题参与 key，同名内容复用同一 tab、不同内容可多开对比
  const slug = (a && a.title) ? String(a.title).replace(/\s+/g, '').slice(0, 24) : '';
  return 'msg:' + ((a && a.message_id) || 'x') + ':' + ((a && a.kind) || 'other') + (slug ? ':' + slug : '');
}
// 渲染预览顶部 tab 栏（无 tab 时隐藏）
function renderPreviewTabs(){
  const bar = document.getElementById('preview-tabs');
  if(!bar) return;
  if(!_previewTabs.length){ bar.innerHTML=''; bar.style.display='none'; return; }
  bar.style.display='flex';
  bar.innerHTML = _previewTabs.map(t=>{
    const icon = _ART_ICONS[t.kind]||'📎';
    const active = t.key===_previewActiveKey ? ' active':'';
    return `<div class="preview-tab${active}" data-key="${esc(t.key)}" onclick="activatePreviewTab(this.dataset.key)" title="${esc(t.title)}">
      <span class="pt-ico">${icon}</span><span class="pt-name">${esc(t.title)}</span>
      <button class="pt-close" onclick="event.stopPropagation();closePreviewTab(this.closest('.preview-tab').dataset.key)" title="关闭">×</button>
    </div>`;
  }).join('');
}
// 切换激活 tab
function activatePreviewTab(key){
  const tb = _previewTabs.find(t=>t.key===key);
  if(!tb) return;
  _previewActiveKey = key;
  _artActive = tb.id||null;
  renderArtV3List();
  renderPreviewTabs();
  renderPreviewBody(tb);
}
// 关闭 tab（关闭当前激活时自动切换到相邻 tab；全部关闭显示默认提示）
function closePreviewTab(key){
  const idx = _previewTabs.findIndex(t=>t.key===key);
  if(idx===-1) return;
  _previewTabs.splice(idx,1);
  if(_previewActiveKey===key){
    if(_previewTabs.length){
      const next = _previewTabs[Math.min(idx, _previewTabs.length-1)];
      _previewActiveKey = next.key;
      _artActive = next.id||null;
      renderArtV3List();
      renderPreviewBody(next);
    } else {
      _previewActiveKey = null;
      _artActive = null;
      renderArtV3List();
      renderPreviewBody(null);
    }
  }
  renderPreviewTabs();
}
// 渲染当前激活 tab 的预览内容（null → 默认提示）
function renderPreviewBody(a){
  window._pvCurrent = a || null;   // Cursor 式引用：当前产物对象（浮动工具条按需取用）
  const titleEl = document.getElementById('preview-title');
  const bodyEl = document.getElementById('preview-content');
  if(!a){
    if(titleEl) titleEl.textContent = '点击文件查看';
    if(bodyEl) bodyEl.innerHTML = '<div style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--mut);font-size:12px;text-align:center;">点击「🗂 文件」下拉列表<br>在此处预览内容</div>';
    return;
  }
  if(titleEl) titleEl.textContent = (a.title||'文件预览') + (' · ' + (a.kind_label||a.kind||''));
  if(!bodyEl) return;
  // 根据类型渲染不同预览
  const kind = a.kind || 'other';
  if(kind === 'sysml'){
    bodyEl.innerHTML = `<div class="preview-sysml"><div style="text-align:center;width:100%;">
      <div style="font-size:13px;font-weight:600;color:var(--blue-d);margin-bottom:2px;">${esc(a.title||'SysML 视图')}</div>
      <div style="font-size:11px;color:var(--mut);margin-bottom:8px;">SysML v2 视图预览 · 可缩放拖拽 · 点节点查看属性</div>
      <div id="preview-sysml-canvas" style="width:100%;min-height:320px;overflow:hidden;text-align:left;"></div>
      <div id="preview-sysml-legend" style="margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--mut);"></div>
      <div style="margin-top:8px;display:flex;gap:6px;justify-content:center;"><button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="previewDownloadProxy()">⬇ 下载</button></div>
      <div id="preview-sysml-versions"></div>
    </div></div>`;
    if(a.meta && a.meta.sysml_views){
      const lg = document.getElementById('preview-sysml-legend');
      if(lg) lg.innerHTML = svmLegendHtml();
      setTimeout(()=>renderSysMLInPreview(a.meta.sysml_views), 50);
    }
    // 版本历史 + 入库/采纳（来源会话）
    const svConv = _artConv || currentConvId || 0;
    setTimeout(()=>loadSysmlVersionsBar(svConv), 80);
  } else if(kind === 'code'){
    const code = a.preview_content || a.content || '';
    // P0 性能修复：超大代码文件截断展示（全量 esc+innerHTML 曾致点击预览即卡死）；完整内容走下载
    const CODE_PREVIEW_MAX = 120000;
    let codeHtml, truncNote = '';
    if(code.length > CODE_PREVIEW_MAX){
      codeHtml = esc(code.slice(0, CODE_PREVIEW_MAX));
      truncNote = `<div style="margin:6px 0;font-size:11px;color:var(--mut);text-align:center;">⚠ 文件较大（${(code.length/1024).toFixed(0)} KB），已截断预览前 ${(CODE_PREVIEW_MAX/1024).toFixed(0)} KB · 完整内容请下载</div>`;
    } else {
      codeHtml = esc(code);
    }
    bodyEl.innerHTML = `<div class="preview-code" style="max-width:100%;overflow:auto;">${codeHtml}</div>
      ${truncNote}
      <div style="margin-top:8px;display:flex;gap:6px;"><button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="previewDownloadProxy()">⬇ 下载</button></div>
      <div id="preview-sysml-versions" style="text-align:left;"></div>`;
    // P0-6：产物入口版本跟随代码文件——代码文件预览也挂版本链（含采纳/入库入口）
    const svConvC = _artConv || currentConvId || 0;
    setTimeout(()=>loadSysmlVersionsBar(svConvC), 80);
  } else if(kind === 'image'){
    const url = a.file_url || a.preview_content || '';
    bodyEl.innerHTML = url ? `<img class="preview-image" style="max-width:100%;height:auto;" src="${esc(url)}" alt="${esc(a.title)}"><div style="margin-top:8px;"><button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="previewDownloadProxy()">⬇ 下载</button></div>` : '<div style="color:var(--mut);padding:20px;text-align:center;">无图片数据</div>';
  } else if(kind === 'report'){
    // 报告：结构化分节（sections）优先 → 元信息条 + 编号分节卡片 + 导出按钮；否则按 markdown 渲染
    // 2026-09-16：移除 meta.graph 的 Cytoscape「影响拓扑图谱」画布——与第4章 Mermaid 视图数据同源、
    // 展示重复（用户确认）；报告内图谱统一由 Mermaid 视图承载（渲染/切换/复制），外部工具可直接渲染
    const content = a.preview_content || a.content || '';
    const secs = (a.meta && a.meta.sections) || [];
    if(secs.length){
      const fid = a.id;
      bodyEl.innerHTML = reportSectionsHtml(secs, a.meta.meta) + `
        <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
          <button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="reportTabExport('md')">⬇ Markdown</button>
          <button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="reportTabExport('docx')">⬇ Word</button>
          <button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="reportTabExport('pdf')">⬇ PDF</button>
          ${fid?`<button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="artDownload(${fid})">📦 下载产物</button>`:''}
        </div>`;
    } else {
      bodyEl.innerHTML = (content ? `<div style="font-size:12.5px;line-height:1.7;max-width:100%;overflow-wrap:break-word;">${a.preview_type === 'html' ? content : renderMarkdown(content)}</div>` : '<span style="color:var(--mut);">暂无预览内容</span>');
    }
  } else {
    const content = a.preview_content || a.content || '';
    bodyEl.innerHTML = `<div style="font-size:12px;line-height:1.65;max-width:100%;overflow-wrap:break-word;">${content ? renderMarkdown(content) : '<span style="color:var(--mut);">暂无预览内容</span>'}</div>`;
  }
}
// 视图预览渲染：按可见性懒挂载 Cytoscape（IntersectionObserver，root=预览滚动容器），
// 滚出即销毁实例 → 同一时刻仅 1~2 个图引擎，杜绝「打开预览一次性挂载 N 个」卡顿与内存累积。
let _pvRenderQueue = [];
let _pvRenderTimer = null;
let _pvObs = null;                       // 观察器
const _pvCyFor = new Map();              // canvas -> cytoscape 实例
function viewsRenderQueuePush(fn){       // 串行化：同一时刻至多挂载 1 个
  _pvRenderQueue.push(fn);
  if(_pvRenderTimer) return;
  _pvRenderTimer = setTimeout(()=>{
    _pvRenderTimer = null;
    const fns = _pvRenderQueue.splice(0);
    let i = 0;
    (function step(){
      if(i >= fns.length) return;
      try{ fns[i](); }catch(e){}
      i++;
      requestAnimationFrame(step);
    })();
  }, 24);
}
function _pvMountView(canvas, vm){
  if(!canvas.isConnected || !canvas.offsetParent) return;
  if(_pvCyFor.has(canvas)) return;       // 已挂载
  // v6.4 P0：整体 idle 化（svmRenderCytoscape 内 init 212ms + fit 50ms 拆两帧；用户感觉画布立刻可见、节点渐进出现）
  window.__cyIdleMount(canvas, ()=>{
    try{
      const cy = svmRenderCytoscape(canvas, vm);
      if(cy){
        _pvCyFor.set(canvas, cy);
        window.__cyIdleRun(()=>{ try{ cy.fit(undefined, 24); }catch(e){} });
        return cy;
      }
    }catch(e){}
    return null;
  }, null);
}
function _pvDestroyView(canvas){
  const cy = _pvCyFor.get(canvas);
  if(!cy) return;
  try{ cy.destroy(); }catch(e){}
  _pvCyFor.delete(canvas);
  if(_cyInstances && _cyInstances[canvas.id]) delete _cyInstances[canvas.id];
  if(window['cy_' + canvas.id]) delete window['cy_' + canvas.id];
}
function _pvEnsureObserver(){
  if(_pvObs) return;
  const rootEl = document.getElementById('preview-content') || null;  // 观察作用域=预览滚动区
  _pvObs = new IntersectionObserver((entries)=>{
    entries.forEach(en=>{
      const cv = en.target;
      // 节点被移出 DOM（切 tab / 重渲染 / 关闭预览）→ 销毁实例防泄漏
      if(!cv.isConnected){ _pvDestroyView(cv); return; }
      // 首次进入视口才挂载；离开视口不销毁——避免「展开面板过渡期间反复挂-销」持续刷主线程导致卡顿
      if(en.isIntersecting && !_pvCyFor.has(cv)){
        const vm = cv.__pvvm;
        if(vm) viewsRenderQueuePush(()=>_pvMountView(cv, vm));
      }
    });
  }, { root: rootEl, threshold: 0 });
}
function _pvClear(){
  if(_pvCyFor.size){ _pvCyFor.forEach((cy)=>{ try{ cy.destroy(); }catch(e){} }); _pvCyFor.clear(); }
  if(_pvObs){ try{ _pvObs.disconnect(); }catch(e){} _pvObs = null; }
}
function renderSysMLInPreview(sysmlData){
  const container = document.getElementById('preview-sysml-canvas');
  if(!container || !sysmlData || !sysmlData.views) return;
  _pvClear();                              // 重渲染前销毁旧实例 + 旧观察
  _pvRenderQueue.length = 0;
  container.innerHTML = '';
  const views = Object.values(sysmlData.views || {});
  if(!views.length){ container.innerHTML = '<div style="padding:20px;color:var(--mut);font-size:12px;text-align:center;">无视图数据</div>'; return; }
  const _ts = Date.now();
  _pvEnsureObserver();
  views.forEach((vm, idx)=>{
    if(!vm) return;
    const vname = (vm.view && vm.view.name) || ('视图 ' + (idx + 1));
    const box = document.createElement('div');
    box.style.cssText = 'margin-bottom:12px;';
    const canvasId = 'svm-pv-' + idx + '-' + _ts;
    box.innerHTML = `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
      <b style="color:var(--blue-d);font-size:12px;">${esc(vname)}</b>
      <span style="font-size:10.5px;color:var(--mut);">v1=${esc((vm.view && vm.view.v1) || '')} · ${(vm.nodes||[]).length} 节点 / ${(vm.edges||[]).length} 边</span></div>
      <div class="svm-canvas" id="${canvasId}" style="min-height:320px;max-height:560px;overflow:auto;"></div>`;
    container.appendChild(box);
    const canvas = box.querySelector('.svm-canvas');
    if((vm.nodes || []).length){
      canvas.__pvvm = vm;                  // 供观察器进入时挂载
      _pvObs.observe(canvas);
    } else {
      canvas.innerHTML = '<div style="padding:40px;color:var(--mut);font-size:12px;text-align:center;">该视图无可用模型元素<br><span style="font-size:11px;">' + esc((vm.warnings||[])[0]||'') + '</span></div>';
    }
  });
}
// 从消息 card_data 直接渲染预览（历史消息/流式消息，无需后端产物 id；V3 写入右侧 #preview-panel 并自动展开）
function artOpenFromMsg(msgId){
  const entry = (window._artByMsg||{})[msgId];
  if(!entry){ renderPreviewPanel({title:'该消息无产物数据', kind:'other', preview_content:''}); return; }
  const cd = entry.cd || {};
  const codeBlk = _firstCodeBlock(entry.content||'');
  const hasSysml = !!(cd.sysml_views && cd.sysml_views.views);
  const isReport = !!(cd.sections || cd.report);
  const kind = hasSysml ? 'sysml' : (isReport ? 'report' : 'code');
  const previewContent = hasSysml ? '' : (isReport ? (entry.content||'') : (codeBlk||entry.content||''));
  renderPreviewPanel({id:null, kind, kind_label:{sysml:'SysML',report:'报告',code:'代码'}[kind]||kind,
                    title: hasSysml?'SysML 视图预览':(isReport?'AI 生成报告':'代码块预览'),
                    preview_type: hasSysml?'sysml':(isReport?'markdown':'code'),
                    preview_content: previewContent, message_id: msgId,
                    meta: {sysml_views: cd.sysml_views, sections: cd.sections||cd.report, report_type: cd.report_type}});
  if(currentConvId) loadArtifacts(currentConvId);
}
function _firstCodeBlock(content){
  const m = (content||'').match(/```[\w+-]*\n([\s\S]*?)```/);
  return m ? m[1].trim() : '';
}
// 报告文档元信息条（文档编号/版本/日期/密级/编制）
function reportMetaHtml(meta){
  if(!meta || typeof meta !== 'object') return '';
  const items = [['文档编号','doc_no'],['版本','version'],['日期','date'],['密级','classification'],['编制','author']];
  const shown = items.filter(([k,key])=>meta[key]).map(([k,key])=>`<span class="rm-item"><b>${k}</b>${esc(meta[key])}</span>`).join('');
  return shown ? `<div class="report-meta">${shown}</div>` : '';
}
// 报告 sections 渲染（预览面板内：元信息条 + 编号分节卡片，正文按 markdown 渲染；支持 graph 字段呈现影响拓扑图谱）
function reportSectionsHtml(sections, meta){
  if(!Array.isArray(sections) || !sections.length) return '<div style="color:var(--mut);font-size:11px;">报告内容为空</div>';
  const metaHtml = reportMetaHtml(meta);
  const body = sections.map((s,i)=>{
    const txt = (s.body||'').trim();
    const hasGraph = !!(s.graph && (s.graph.impact_nodes||[]).length);
    const graphHtml = hasGraph
      ? `<div class="report-graph-wrap">${impactLegendHtml()}<div id="report-graph-${i}" style="width:100%;height:380px;border:1px solid var(--line);border-radius:8px;background:#fafbfc;margin-top:6px;"></div></div>`
      : '';
    return `<div class="report-sec">
      <div class="report-sec-h"><span class="report-sec-n">${String(i+1).padStart(2,'0')}</span><span class="report-sec-t">${esc(s.heading||'')}</span></div>
      ${txt?`<div class="report-sec-b">${renderMarkdown(txt)}</div>`:''}
      ${graphHtml}
      ${s.table?`<div class="report-sec-b" style="margin-top:6px;">${renderTable(mdTableToRows(s.table))}</div>`:''}
    </div>`;
  }).join('');
  // 图谱延迟初始化（容器挂载后渲染）
  if(body.includes('report-graph-')){
    setTimeout(()=>{
      sections.forEach((s,i)=>{
        if(s.graph && (s.graph.impact_nodes||[]).length) renderImpactGraph('report-graph-'+i, s.graph);
      });
    }, 150);
  }
  return metaHtml + '<div>' + body + '</div>';
}
// markdown 表格文本 → [[cell,...],...]（保留表头/分隔行/数据行，供 renderTable 消费）
function mdTableToRows(table){
  return String(table||'').split('\n')
    .filter(l=>/^\s*\|/.test(l))
    .map(l=>l.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim()));
}
function artDownload(id){
  window.open('/api/artifacts/'+id+'/download', '_blank');
  toast('产物下载已开始');
}
// [S5 2026-09-18 保留] 可达性分析判定不可达；「按产物 id 二次导出 md/docx/pdf」的能力在 V3 预览中
// 已无入口（13-reports.js 的 downloadReport 按钮挂在报告视图、走的是 window._lastReport，非本路径），
// 属「无活后继」，不删，留待产品决策。
// 报告产物二次导出（md/docx/pdf）：落盘临时文件后下载
async function artDownloadFmt(id, fmt){
  try{
    const a = await api('/api/artifacts/'+id);
    const rep = {title:a.title, sections:(a.meta&&a.meta.sections)||[], summary:(a.meta&&a.meta.summary)||'', report_type:(a.meta&&a.meta.report_type)||'analysis', meta:(a.meta&&a.meta.meta)||{}};
    window._lastReport = rep;
    downloadReport(fmt);
  }catch(e){ toast('导出失败'); }
}

// ── Q5：预览区产物下载（代码/文档/图片/SysML，样式与报告下载按钮一致）──
function previewDownloadProxy(){
  const tb = _previewTabs.find(t=>t.key===_previewActiveKey);
  if(!tb){ toast('无预览数据'); return; }
  if(tb.id){ artDownload(tb.id); return; }          // 有产物 id → 走后端落盘下载
  try{
    if(tb.kind==='image'){                          // 消息级图片 → 前端拉取下载
      const url = tb.file_url||tb.preview_content||'';
      if(!url){ toast('无图片可下载'); return; }
      fetch(url).then(r=>r.blob()).then(b=>{
        const ext = (url.split('?')[0].split('.').pop()||'png');
        _savePreviewBlob(b, (tb.title||'image')+'.'+ext);
      }).catch(()=>toast('图片下载失败'));
      return;
    }
    const text = tb.preview_content||tb.content||'';  // 代码/文档文本
    if(!text){ toast('无内容可下载'); return; }
    const ext = (tb.kind==='sysml')?'sysml':'md';
    _savePreviewBlob(new Blob([text],{type:'text/plain;charset=utf-8'}), (tb.title||'产物')+'.'+ext);
    toast('下载已开始');
  }catch(e){ toast('下载失败'); }
}
function _savePreviewBlob(blob, fname){
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = fname;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 1000);
}
// [S5 2026-09-18 保留] 可达性分析判定不可达；V3 预览体只提供「⬇ 下载」（previewDownloadProxy），
// 无「复制文本」入口，本能力属「无活后继」，不删，留待产品决策。
async function copyArtText(id){
  try{
    const a = await api('/api/artifacts/'+id);
    await navigator.clipboard.writeText(a.preview_content||a.title||'');
    toast('已复制到剪贴板');
  }catch(e){ toast('复制失败'); }
}
// [S5 2026-09-18 保留] 可达性分析判定不可达，且**无任何后继实现**（V3 预览未提供「溯源跳转」，
// 基线提交起即无调用点）—— 属「断链/未接线」而非「被取代」，按 S5 判定标准（无可达后继 ⇒ 不删）
// 予以保留，列为待接线缺陷。
// 定位消息流中的源消息
function scrollToMessage(msgId){
  const el = document.getElementById('msg-'+msgId) || document.querySelector(`.msg[data-mid="${msgId}"]`);
  if(!el){ toast('源消息不存在'); return; }
  el.scrollIntoView({behavior:'smooth', block:'center'});
  el.style.outline = '2px solid var(--blue)';
  setTimeout(()=>{ el.style.outline = ''; }, 2000);
}
// ── 轻量 Markdown 渲染（离线自实现）：标题/粗斜体/行内代码/代码块/表格/列表/引用/链接 + 数字表格→SVG 柱状图 ──
