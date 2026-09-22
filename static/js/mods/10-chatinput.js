/* 聊天输入：附件 / 范围选择 / 知识库选择
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 4535-5202  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function toggleAttachMenu(btn){
  const menu = document.getElementById('attach-menu');
  if(!menu) return;
  if(menu.style.display === 'block'){ closeAttachMenu(); return; }
  // 先显示以获取尺寸，再定位到按钮右上方（空间不足时落到按钮下方）
  menu.style.display = 'block';
  closeAttachProject();
  const r = btn.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = r.left, top = r.top - mh - 8;
  if(left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
  if(top < 8) top = r.bottom + 8;
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  // 点击菜单外关闭（一次性监听，避免堆积）
  setTimeout(()=>{
    document.addEventListener('click', closeAttachMenu, {once:true});
  }, 0);
}
function closeAttachMenu(){
  const menu = document.getElementById('attach-menu');
  if(menu) menu.style.display = 'none';
  closeAttachProject();
  closeAttachEngineering();
}
// 二级面板：当前项目文件清单
function toggleAttachProject(ev){
  ev.stopPropagation();
  const menu = document.getElementById('attach-menu');
  const pop = document.getElementById('attach-project-pop');
  if(!menu || !pop) return;
  if(pop.style.display === 'block'){ closeAttachProject(); return; }
  closeAttachEngineering();
  pop.style.display = 'block';
  positionSubPop(menu, pop);
  loadAttachProjectFiles();
  setTimeout(()=>{
    document.addEventListener('click', closeAttachAll, {once:true});
  }, 0);
}
function closeAttachProject(){
  const pop = document.getElementById('attach-project-pop');
  if(pop) pop.style.display = 'none';
}
function closeAttachAll(){ closeAttachMenu(); }
// 二级面板：当前工程文件（MBSE 工程树：类型 → 实例，点击实例注入引用）
function toggleAttachEngineering(ev){
  ev.stopPropagation();
  const menu = document.getElementById('attach-menu');
  const pop = document.getElementById('attach-eng-pop');
  if(!menu || !pop) return;
  if(pop.style.display === 'block'){ closeAttachEngineering(); return; }
  closeAttachProject();
  pop.style.display = 'block';
  positionSubPop(menu, pop);
  loadAttachEngTree();
  setTimeout(()=>{
    document.addEventListener('click', closeAttachAll, {once:true});
  }, 0);
}
function closeAttachEngineering(){
  const pop = document.getElementById('attach-eng-pop');
  if(pop) pop.style.display = 'none';
}
// 二级面板统一定位：一级菜单右侧弹出，放不下翻左侧，垂直不超视口
function positionSubPop(menu, pop){
  const mr = menu.getBoundingClientRect();
  const pw = pop.offsetWidth;
  let left = mr.right + 6;
  if(left + pw > window.innerWidth - 8) left = mr.left - pw - 6;
  if(left < 8) left = Math.max(8, (window.innerWidth - pw) / 2);
  let top = mr.top;
  if(top + pop.offsetHeight > window.innerHeight - 8) top = window.innerHeight - pop.offsetHeight - 8;
  pop.style.left = left + 'px';
  pop.style.top = Math.max(8, top) + 'px';
}
// 点击「上传附件」：直接触发系统文件选择框（不限类型）
function pickLocalFile(){
  document.getElementById('file-input').click();
  closeAttachMenu();
}
let _attachProjectDocs = [];   // 2026-09-04 缓存当前会话文件清单，供本地搜索过滤
// 「当前会话的文件」：从当前会话历史消息 attachments 提取（去重），与 @ 会话文件同源
async function loadAttachProjectFiles(){
  const listEl = document.getElementById('attach-project-list');
  const emptyEl = document.getElementById('attach-project-empty');
  const searchEl = document.getElementById('attach-project-search');
  if(!listEl || !emptyEl) return;
  listEl.innerHTML = '<div style="color:var(--mut);font-size:12px;padding:8px;">加载中…</div>';
  emptyEl.style.display = 'none';
  if(searchEl) searchEl.value = '';
  _attachProjectDocs = [];
  if(typeof currentConvId !== 'undefined' && currentConvId){
    try{
      const mr = await api(`/api/conversations/${currentConvId}/messages?limit=50`);
      const seen = {};
      ((mr && mr.messages) || []).forEach(mm => {
        let atts = [];
        try{ atts = mm.attachments ? (typeof mm.attachments==='string' ? JSON.parse(mm.attachments) : mm.attachments) : []; }catch(e){ atts = []; }
        (atts || []).forEach(a => {
          const key = a.filename || a.url || '';
          if(!key || seen[key]) return;
          seen[key] = 1;
          _attachProjectDocs.push({id:a.doc_id||'', filename:key, url:a.url||'', size:a.size||0, is_image:!!a.is_image, file_type:key.split('.').pop()||'文件'});
        });
      });
    }catch(e){}
  }
  renderAttachProjectList();
}
function renderAttachProjectList(){
  const listEl = document.getElementById('attach-project-list');
  const emptyEl = document.getElementById('attach-project-empty');
  const emptyTxt = document.getElementById('attach-project-empty-txt');
  if(!listEl || !emptyEl) return;
  const kw = (document.getElementById('attach-project-search')?.value || '').trim().toLowerCase();
  const docs = _attachProjectDocs.filter(d=>{
    if(!kw) return true;
    return String(d.filename||'').toLowerCase().includes(kw);
  });
  if(!_attachProjectDocs.length){
    listEl.innerHTML = '';
    if(emptyTxt) emptyTxt.textContent = '当前会话还没有文件';
    emptyEl.style.display = 'flex';
    return;
  }
  if(!docs.length){
    listEl.innerHTML = '';
    if(emptyTxt) emptyTxt.textContent = '没有匹配「'+kw+'」的文件';
    emptyEl.style.display = 'flex';
    return;
  }
  emptyEl.style.display = 'none';
  listEl.innerHTML = docs.map((d,i)=>{
    const icon = d.is_image ? '🖼' : fileIcon(d.filename || '');
    return `<div onclick="attachConvFile(${i})" style="display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;cursor:pointer;transition:background .12s;">
      <span style="font-size:18px;">${icon}</span>
      <div style="flex:1;min-width:0;">
        <div style="font-size:12.5px;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(d.filename || '未命名')}</div>
        <div style="font-size:10.5px;color:var(--mut);">${esc(d.file_type||'文件')} · 会话附件${d.doc_id?' · 已入库':''}</div>
      </div>
      <span style="font-size:12px;color:var(--blue-d);flex-shrink:0;">添加</span>
    </div>`;
  }).join('');
}
function attachConvFile(i){
  const d = _attachProjectDocs[i];
  if(!d) return;
  if(pendingAttachments.some(a=>a.filename===d.filename)){ toast('该文件已在附件栏'); closeAttachMenu(); return; }
  pendingAttachments.push({
    url: d.url || (d.doc_id ? '/api/documents/'+d.doc_id+'/download' : ''),
    filename: d.filename,
    size: d.size || 0,
    is_image: !!d.is_image,
    doc_id: d.doc_id || undefined,
    source: 'conversation'
  });
  renderAttachBar();
  closeAttachMenu();
  toast('已添加会话文件：'+d.filename);
}
// ── 「当前工程文件」：MBSE 工程树（类型 → 实例），点击实例注入 Cursor 式引用 ──
let _attachEngNodes = [];      // 缓存工程实体
let _attachEngCollapsed = new Set();
async function loadAttachEngTree(){
  const treeEl = document.getElementById('attach-eng-tree');
  const emptyEl = document.getElementById('attach-eng-empty');
  const searchEl = document.getElementById('attach-eng-search');
  if(!treeEl || !emptyEl) return;
  treeEl.innerHTML = '<div style="color:var(--mut);font-size:12px;padding:8px;">加载中…</div>';
  emptyEl.style.display = 'none';
  if(searchEl) searchEl.value = '';
  try{
    const br = (typeof getCurrentBranch==='function') ? getCurrentBranch() : '';
    const g = await api('/api/knowledge/graph?branch=' + encodeURIComponent(br||'') + '&status=all');
    _attachEngNodes = (g && g.entities) || [];
  }catch(e){ _attachEngNodes = []; }
  renderAttachEngTree();
}
function renderAttachEngTree(){
  const treeEl = document.getElementById('attach-eng-tree');
  const emptyEl = document.getElementById('attach-eng-empty');
  const emptyTxt = document.getElementById('attach-eng-empty-txt');
  if(!treeEl || !emptyEl) return;
  const kw = (document.getElementById('attach-eng-search')?.value || '').trim().toLowerCase();
  const nodes = _attachEngNodes.filter(n=>{
    if(!kw) return true;
    return String(n.name||'').toLowerCase().includes(kw) || String(n.entity_type||'').toLowerCase().includes(kw);
  });
  if(!_attachEngNodes.length){
    treeEl.innerHTML = '';
    if(emptyTxt) emptyTxt.textContent = '当前工程暂无实体';
    emptyEl.style.display = 'flex';
    return;
  }
  if(!nodes.length){
    treeEl.innerHTML = '';
    if(emptyTxt) emptyTxt.textContent = '没有匹配「'+kw+'」的实体';
    emptyEl.style.display = 'flex';
    return;
  }
  emptyEl.style.display = 'none';
  // 按类型分组（类型 → 实例），支持点击类型名折叠
  const byType = {};
  nodes.forEach(n=>{ const t=n.entity_type||'未分类'; (byType[t]=byType[t]||[]).push(n); });
  treeEl.innerHTML = Object.keys(byType).sort().map(t=>{
    const collapsed = _attachEngCollapsed.has(t) && !kw;
    const rows = byType[t].map(n=>`
      <div onclick="attachEngEntity('${esc(n.id)}','${esc(String(n.name||'').replace(/"/g,'&quot;'))}','${esc(n.entity_type||'')}')" style="display:flex;align-items:center;gap:8px;padding:6px 10px 6px 26px;border-radius:7px;cursor:pointer;" onmouseover="this.style.background='var(--bg-soft,#f5f5f2)'" onmouseout="this.style.background=''">
        <span style="width:7px;height:7px;border-radius:50%;background:${graphColor(t)};display:inline-block;flex:none;"></span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:var(--txt);">${esc(n.name||n.id)}</span>
        <span style="font-size:10.5px;color:var(--blue-d);flex-shrink:0;">添加</span>
      </div>`).join('');
    return `<div style="margin-bottom:2px;">
      <div onclick="_attachEngCollapsed.has('${esc(t)}')?_attachEngCollapsed.delete('${esc(t)}'):_attachEngCollapsed.add('${esc(t)}');renderAttachEngTree()" style="display:flex;align-items:center;gap:8px;padding:6px 10px;cursor:pointer;position:sticky;top:0;background:var(--panel,#fff);">
        <span style="color:var(--mut);font-size:9px;width:10px;flex:none;">${collapsed?'▸':'▾'}</span>
        <span style="width:9px;height:9px;border-radius:50%;background:${graphColor(t)};display:inline-block;flex:none;"></span>
        <span style="flex:1;font-size:12px;font-weight:600;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(t)}</span>
        <span class="tag" style="font-size:10px;">${byType[t].length}</span>
      </div>
      ${collapsed?'':rows}
    </div>`;
  }).join('');
}
async function attachEngEntity(id, name, type){
  // 把工程实体摘要注入 Cursor 式引用（chatRefs），随消息发送给 AI
  try{
    const r = await api('/api/knowledge/entities/'+encodeURIComponent(id));
    const n = (r && (r.entity || r)) || {};
    let props = {};
    try{ props = typeof n.properties==='string' ? JSON.parse(n.properties||'{}') : (n.properties||{}); }catch(e){}
    const lines = [
      'MBSE 工程实体引用',
      '- 名称：'+(n.name||name||id),
      '- 类型：'+(n.entity_type||type||'-'),
      '- 状态：'+(n.status||'-'),
      Object.keys(props).length ? '- 属性：'+JSON.stringify(props) : ''
    ].filter(Boolean);
    chatRefs.push({label:'🧬 '+(n.name||name||id), text:lines.join('\n')});
    renderRefBar();
    closeAttachMenu();
    toast('已引用工程实体：'+(n.name||name||id));
  }catch(e){
    // 详情拉取失败 → 退化为纯文本标记插入输入框
    const ta = document.getElementById('chat-input');
    if(ta){ const tag='【MBSE实体：'+(type?type+'/':'')+name+'】'; ta.value += tag; ta.focus(); }
    closeAttachMenu();
    toast('实体详情读取失败，已在输入框插入标记');
  }
}
function fileIcon(name){
  const ext = (name.split('.').pop() || '').toLowerCase();
  if(['png','jpg','jpeg','gif','webp','bmp','svg'].includes(ext)) return '🖼';
  if(['pdf'].includes(ext)) return '📕';
  if(['doc','docx'].includes(ext)) return '📘';
  if(['xls','xlsx','csv'].includes(ext)) return '📊';
  if(['ppt','pptx'].includes(ext)) return '📽';
  if(['txt','md','json','xml','yaml','yml','py','js','ts','html','css'].includes(ext)) return '📝';
  if(['zip','rar','7z','tar','gz'].includes(ext)) return '📦';
  return '📄';
}

let pendingAttachments = [];  // V2.3 富输入：待发送附件 [{url, filename, size, is_image}]
// 2026-08-28 补齐附件上传链路（函数历史丢失）：上传 → /api/upload（文档类自动入资料库，
// ingest_upload_document 共用入口）→ attach-bar chips（文件名+入库标记+可移除）→ 随消息发送
// ── 2026-09-04 模型能力判断：上传后按当前对话模型是否支持该格式给出提示反馈 ──
const IMG_EXTS = ['png','jpg','jpeg','gif','webp','bmp','svg'];
const ARCHIVE_EXTS = ['zip','rar','7z','tar','gz'];
const PARSE_DOC_EXTS = ['txt','md','csv','json','xml','yaml','yml','log','docx','doc','pdf','xlsx','xls','pptx','ppt'];
function currentChatProvider(){
  const id = Number(document.getElementById('llm-providers')?.value) || 0;
  if(id) return _chatProvidersCache.find(p=>p.id===id) || null;
  // 未显式选择 → 会话实际用的就是「全局默认对话模型」（后端 llm._load_provider_cfg 的选取规则：
  // model_type='chat' AND is_default=1）。原先直接 return null，会让下面 modelSupportsVision 的
  // `if(!p) return true` 静默放行 → **提示说的和实际会不会发图对不上**。回落以对齐二者。
  return _chatProvidersCache.find(p=>p.model_type==='chat' && p.is_default===1)
      || _chatProvidersCache.find(p=>p.model_type==='chat') || null;
}
// ⚠️ 2026-09-21 修：API 返回的 tags 是**数组**（如 ['chinese','fast']），原实现对数组做 JSON.parse ——
//    JSON.parse(String(['vision'])) === JSON.parse('vision') → 抛 SyntaxError → 恒返回 []。
//    结果是下面 modelSupportsVision 的 tags 分支**从未生效**，一直静默只靠模型名启发式。
function providerProviderTags(p){
  try{
    let t = (p && p.tags);
    if(typeof t === 'string') t = JSON.parse(t || '[]');   // 兼容字符串形态
    if(!Array.isArray(t)) t = t ? [t] : [];
    return t;
  }catch(e){ return []; }
}
function modelSupportsVision(p){
  if(!p) return true;  // 无 provider 配置（Mock 兜底）：无法判定，不拦截
  const tags = providerProviderTags(p);
  // 字符集与后端 llm/__init__.py 的 VISION_TAG_RE 同套（用户标 'vision' 或 '多模态' 两侧都认）
  if(tags.some(t=>/vision|image|multimodal|多模态|图片/i.test(String(t)))) return true;
  // 模型名启发式：常见多模态 chat 模型（仅用于「善意提示」，私有部署命名可能漏判，故以 tags 为准）
  return /gpt-4o|gpt-4\.1|gpt-5|o[34]|vision|qwen[22?.]*vl|glm-4v|glm-5v|claude|gemini|doubao.*vision|grok-[24]|kimi/i.test(String(p.model_name||''));
}
function attachFormatFeedback(f, r){
  const p = currentChatProvider();
  const mName = p ? (p.name+' · '+(p.model_name||'')) : '默认模型';
  const ext = (String(f.name).split('.').pop()||'').toLowerCase();
  if(IMG_EXTS.includes(ext)){
    if(!modelSupportsVision(p)){
      toast('⚠ 当前模型「'+mName+'」未标记支持图片理解，图片可能无法被直接识别'+(r&&r.doc_id?'（已按文档解析入库）':''), 5000);
    }
  } else if(ARCHIVE_EXTS.includes(ext)){
    if(!(r&&r.doc_id)) toast('⚠ 压缩包无法解析入库，将作为内联附件随消息发送，模型可能无法读取其内容', 5000);
  } else if(PARSE_DOC_EXTS.includes(ext)){
    if(!(r&&r.doc_id)) toast('⚠ 该文档未成功解析入库，将作为内联附件发送', 4000);
  } else {
    if(!(r&&r.doc_id)) toast('ℹ 「.'+ext+'」格式不在常规解析列表，将作为内联附件发送', 4000);
  }
}
async function uploadFiles(input, kind){
  const files = Array.from(input.files || []);
  input.value = '';
  if(!files.length) return;
  for(const f of files){
    const fd = new FormData();
    fd.append('file', f);
    try{
      const r = await fetch('/api/upload', {method:'POST', body: fd}).then(x=>x.json());
      if(r.error){ toast('上传失败：'+r.error); continue; }
      pendingAttachments.push({url:r.url, filename:r.filename, size:r.size, is_image:r.is_image, doc_id:r.doc_id, parse_status:r.parse_status});
      renderAttachBar();
      if(r.doc_id) toast('📎 '+r.filename+' 已入文档库（解析 '+(r.chunk_count||0)+' 块）');
      else toast('📎 '+r.filename+' 已就绪（会话内联附件）');
      attachFormatFeedback(f, r);   // 2026-09-04：按当前模型能力对格式给出提示
    }catch(e){ toast('上传失败：'+(e.message||e)); }
  }
}
function renderAttachBar(){
  const bar = document.getElementById('attach-bar');
  if(!bar) return;
  if(!pendingAttachments.length){ bar.style.display='none'; bar.innerHTML=''; return; }
  bar.style.display='flex';
  bar.innerHTML = pendingAttachments.map((a,i)=>`
    <span class="attach-chip"${pvDataAttrs({doc_id:a.doc_id, filename:a.filename||'', url:a.url||''})} onclick="pvOpenFromEl(this)" title="点击预览源文件：${esc(a.filename||'')}" style="cursor:pointer;">${a.is_image?`<img src="${esc(a.url)}" alt="">`:'📄'}<span class="an" title="${esc(a.filename)}">${esc(a.filename)}</span>${a.doc_id?'<span style="font-size:10px;color:var(--grn,#2f855a);flex:none;">已入库</span>':''}<span class="rm" onclick="event.stopPropagation();removeAttach(${i})" title="移除附件">✕</span></span>`).join('');
}
function removeAttach(i){ pendingAttachments.splice(i,1); renderAttachBar(); }
let activeScopes = [];                 // 当前选择的多个建模范围 [{id,name,mode,doc_names}]
let _scopeDraft = null;
let _scopeDocChunks = [];
let _scopeHits = [];                 // 文本圈定：待确认的命中片段 [{doc,section,content,on}]
function renderScopeChip(){
  const bar = document.getElementById('scope-chip-bar');
  if(!bar) return;
  if(!activeScopes.length){ bar.style.display='none'; bar.innerHTML=''; return; }
  bar.style.display='flex';
  bar.innerHTML = activeScopes.map((s,i)=>`<span class="attach-chip"><span class="an">🎯 范围：${esc(s.name)}${s.mode!=='doc'?'（片段）':''}</span><span class="rm" onclick="scopeUnselect(${i})" title="取消该范围">✕</span></span>`).join('');
}
function scopeUnselect(i){ activeScopes.splice(i,1); renderScopeChip(); }
function scopeIsSel(id){ return activeScopes.some(s=>s.id===id); }
function scopeTab(pane){
  document.querySelectorAll('.scope-tab-btn').forEach(b=>{
    const on = b.dataset.pane===pane;
    b.classList.toggle('on', on);
    b.style.background = on ? 'var(--blue-l)' : '';
    b.style.color = on ? 'var(--blue-d)' : '';
  });
  document.querySelectorAll('.scope-tab-pane').forEach(pp=>{ pp.style.display = (pp.id==='scope-pane-'+pane) ? 'block' : 'none'; });
}
async function scopeLoadSaved(){
  const el = document.getElementById('scope-pane-saved'); if(!el) return;
  try{
    const scopes = (await api('/api/scopes')) || [];
    let h;
    if(!scopes.length){ h = '<div style="color:var(--mut);font-size:12px;padding:10px 2px;">暂无已保存范围。切到「文本圈定」或「文档切片圈定」新建。</div>'; }
    else{
      h = '<div style="color:var(--mut);font-size:11px;margin-bottom:6px;">全局范围（所有用户可见）· 勾选多个可叠加，点击「应用」即选中</div>';
      scopes.forEach(s=>{
        const sel = scopeIsSel(s.id);
        const meta = `<div style="font-size:10.5px;color:var(--mut);">创建 ${esc(s.created_by||'-')} · ${esc((s.created_at||'').slice(0,16))} · ${(s.doc_names||[]).length} 篇</div>`;
        h += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:6px 10px;">
<label style="display:flex;align-items:center;gap:6px;"><input type="checkbox" class="scope-sel-cb" value="${s.id}" ${sel?'checked':''} onchange="scopeSavedCount()"></label>
<div style="flex:1;min-width:0;"><b style="font-size:12.5px;">${esc(s.name)}</b><span class="st ok" style="margin-left:6px;">${esc(s.mode)}</span>${meta}</div>
<button class="btn sm ghost" onclick="scopeDelete(${s.id})">删除</button></div>`;
      });
      h += `<div style="margin-top:8px;display:flex;gap:8px;align-items:center;justify-content:flex-end;">
<button class="btn ghost" onclick="scopeSavedClearAll()">清空已选</button>
<button class="btn" onclick="scopeApplySaved()">应用已选（<span id="scope-saved-n">0</span>）</button></div>`;
    }
    el.innerHTML = h;
    scopeSavedCount();
  }catch(e){ el.innerHTML = '<div style="color:var(--red);font-size:12px;">加载失败：'+esc(e.message)+'</div>'; }
}
function scopeSavedCount(){
  const n = document.querySelectorAll('#scope-pane-saved .scope-sel-cb:checked').length;
  const el = document.getElementById('scope-saved-n'); if(el) el.textContent = n;
}
function scopeSavedClearAll(){ document.querySelectorAll('#scope-pane-saved .scope-sel-cb').forEach(c=>c.checked=false); scopeSavedCount(); }
async function scopeApplySaved(){
  const ids = [...document.querySelectorAll('#scope-pane-saved .scope-sel-cb:checked')].map(c=>+c.value);
  const scopes = (await api('/api/scopes')) || [];
  const picked = (scopes||[]).filter(s=> ids.includes(s.id)).map(s=>({id:s.id, name:s.name, mode:s.mode, doc_names:s.doc_names||[]}));
  if(!picked.length){ toast('请至少勾选一个范围'); return; }
  activeScopes = picked;
  renderScopeChip(); closeModal(); toast('已应用 '+picked.length+' 个范围');
}
function scopeBuildTextPane(){
  const el = document.getElementById('scope-pane-text'); if(!el) return;
  el.innerHTML = `<div style="margin-bottom:6px;"><b>范围名称</b></div>
<input id="scope-text-name" placeholder="范围名称（默认取输入开头）" style="border:1px solid var(--line);border-radius:6px;padding:5px 10px;font-size:13px;width:100%;box-sizing:border-box;margin-bottom:8px;">
<textarea id="scope-text" style="width:100%;min-height:64px;box-sizing:border-box;border:1px solid var(--line);border-radius:6px;padding:6px;font-size:12px;" placeholder="引用文档的描述，例：《宽带载荷需求》第2节性能指标"></textarea>
<div id="scope-parse-res" style="margin-top:8px;min-height:22px;"></div>`;
  const ta = document.getElementById('scope-text'); if(ta) ta.oninput = ()=>scopeParseText();
}
async function scopeParseText(){
  const res = document.getElementById('scope-parse-res'); if(!res) return;
  const text = (document.getElementById('scope-text')||{}).value||'';
  if(!text.trim()){ res.innerHTML=''; return; }
  try{
    const draft = await api('/api/scopes/from-text',{method:'POST',body:JSON.stringify({text})});
    _scopeDraft = draft;
    let hits = [];
    let pv = '<span class="tag">'+esc(draft.reason||'')+'</span>';
    if(draft.doc_names && draft.doc_names.length){
      try{
        const pp = await api('/api/scopes/preview',{method:'POST',body:JSON.stringify({scope:{mode:draft.mode||'doc',doc_names:draft.doc_names}})});
        const sm = (pp&&pp.samples)||[];
        hits = sm.map(x=>({doc:x.doc, section:x.section, content:x.content, on:true}));
        pv += '<div style="margin-top:6px;max-height:150px;overflow-y:auto;border:1px dashed var(--line);border-radius:6px;padding:6px;"><div style="font-size:11px;color:var(--mut);margin-bottom:4px;">命中片段（勾选要纳入范围的，默认全选）</div>';
        sm.forEach((x,i)=>{
          pv += `<label style="display:flex;gap:6px;align-items:flex-start;font-size:11px;padding:3px 4px;cursor:pointer;">
<input type="checkbox" class="scope-pick-cb" data-i="${i}" checked>
<span style="color:var(--mut);flex:1;word-break:break-all;"><b>📄 ${esc(x.doc||'')}</b>${x.section?' <span class="tag">'+esc(x.section)+'</span>':''}<br>${esc((x.content||'').slice(0,90))}</span></label>`;
        });
        pv += '</div><div style="margin-top:4px;display:flex;gap:6px;"><button class="btn sm ghost" onclick="scopePickAll(true)">全选</button><button class="btn sm ghost" onclick="scopePickAll(false)">全不选</button></div>';
      }catch(_e){ pv += '<div style="color:var(--mut);font-size:11px;margin-top:4px;">命中片段预览不可用</div>'; }
    } else {
      pv += '<div style="color:var(--mut);font-size:11px;margin-top:4px;">未命中具体文档，请补充文档名</div>';
    }
    _scopeHits = hits;
    pv += '<div style="margin-top:8px;display:flex;gap:8px;align-items:center;"><button class="btn" onclick="scopeSaveThis()">保存并使用</button><span style="color:var(--mut);font-size:11px;">仅保存勾选的片段</span></div>';
    res.innerHTML = pv;
  }catch(e){ res.innerHTML = '<span style="color:var(--red)">'+esc(e.message)+'</span>'; }
}
function scopePickAll(on){ document.querySelectorAll('.scope-pick-cb').forEach(cb=> cb.checked = on); }
async function scopeSaveThis(){
  const text = (document.getElementById('scope-text')||{}).value||'';
  let chosen;
  const hasPick = document.querySelectorAll('.scope-pick-cb').length > 0;
  if(hasPick){
    chosen = [...document.querySelectorAll('.scope-pick-cb:checked')].map(cb=> (_scopeHits||[])[+cb.dataset.i]).filter(Boolean);
    if(!chosen.length){ toast('未勾选任何命中片段'); return; }
  } else {
    chosen = (_scopeHits || []).filter(Boolean);
  }
  const docset = [...new Set(chosen.map(x=>x.doc).filter(Boolean))];
  if(!docset.length){ toast('未命中文档'); return; }
  const fragment_text = chosen.map(x=>x.content||'').filter(Boolean).join('\n\n');
  const mode = fragment_text ? 'fragment_group' : 'doc';
  const name = ((document.getElementById('scope-text-name')||{}).value||'').trim() || (text.trim().match(/^\S+/)||[''])[0].slice(0,16) || '文本范围';
  const saved = await api('/api/scopes',{method:'POST',body:JSON.stringify({name, mode, doc_names: docset, fragment_text})});
  activeScopes = [{id:saved.id, name:saved.name, mode, doc_names: docset}];
  renderScopeChip(); closeModal(); toast('已保存并使用：'+saved.name);
}


async function scopeUse(id){
  const rows = (await api('/api/scopes')) || [];
  const s = rows.find(x=>x.id===id);
  if(!s){ toast('范围不存在'); return; }
  const idx = activeScopes.findIndex(a=>a.id===id);
  if(idx>=0){ activeScopes.splice(idx,1); }
  else { activeScopes.push({id:s.id,name:s.name,mode:s.mode,doc_names:s.doc_names||[]}); }
  renderScopeChip();
  if(activeScopes.length){ toast('已选范围：'+activeScopes.map(a=>a.name).join('、')); }
}
async function scopeDelete(id){
  if(!confirm('删除该建模范围？')) return;
  await api('/api/scopes/'+id,{method:'DELETE'});
  activeScopes = activeScopes.filter(a=>a.id!==id);
  renderScopeChip();
  scopeLoadSaved();
}
function scopeChunkCount(){
  const n = document.querySelectorAll('.scope-chunk-cb:checked').length;
  const el = document.getElementById('scope-chunk-sel-n'); if(el) el.textContent = '已选 '+n+' 切片';
}
function scopeSelectAll(on){ document.querySelectorAll('.scope-chunk-cb').forEach(cb=> cb.checked = on); scopeChunkCount(); }
function scopeSelectSection(btn, on){
  const sec = btn.closest('.scope-sec'); if(!sec) return;
  sec.querySelectorAll('.scope-chunk-cb').forEach(cb=> cb.checked = on);
  scopeChunkCount();
}
async function scopeBuildChunkPane(){
  const el = document.getElementById('scope-pane-chunk'); if(!el) return;
  let _opts = '<option value="">选择文档…</option>';
  try{ const _dd = (await api('/api/documents')) || []; _scopeDocChunks = []; _opts += _dd.map(d=>`<option value="${d.id}::${esc(d.filename)}">${esc(d.filename)}</option>`).join(''); }catch(_e){}
  el.innerHTML = `<div style="margin-bottom:6px;"><b>范围名称</b></div>
<input id="scope-chunk-name" placeholder="范围名称（默认=文档名）" style="border:1px solid var(--line);border-radius:6px;padding:5px 10px;font-size:13px;width:100%;box-sizing:border-box;margin-bottom:8px;">
<div style="margin-bottom:6px;"><b>文档切片</b><span style="color:var(--mut);font-size:11px;"> 选文档→加载分片→勾选切片（可「全选全部」）</span></div>
<select id="scope-doc-select" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;max-width:220px;">${_opts}</select> <button class="btn sm" onclick="scopeLoadDoc()">加载切片</button>
<div id="scope-chunk-tree" style="margin-top:6px;max-height:220px;overflow-y:auto;border:1px solid var(--line);border-radius:6px;padding:6px;"><div style="color:var(--mut);font-size:12px;padding:6px;">选择文档并「加载切片」后，此处按章节显示切片供勾选。</div></div>`;
}
async function scopeLoadDoc(){
  const sel = document.getElementById('scope-doc-select');
  const v = (sel && sel.value || '').split('::');
  const did = v[0], fn = v[1] || '';
  if(!did){ toast('请先选择文档'); return; }
  const tree = document.getElementById('scope-chunk-tree');
  if(!tree) return;
  tree.innerHTML = '<div class="loading">加载切片…</div>';
  try{
    const chunks = (await api('/api/scopes/docs/'+did+'/chunks')) || [];
    chunks.forEach(c=> c.document_id = c.document_id || did);
    _scopeDocChunks = chunks;
    if(!chunks.length){ tree.innerHTML = '<div style="color:var(--mut);font-size:12px;">该文档暂无分片（未完成解析/分块）</div>'; return; }
    const groups = {};
    chunks.forEach(c=>{ const k=c.section||'（无章节）'; (groups[k]=groups[k]||[]).push(c); });
    let g = '<div style="margin-bottom:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;"><b>'+esc(fn)+'</b> <button class="btn sm" onclick="scopeSelectAll(true)">全选全部</button><button class="btn sm ghost" onclick="scopeSelectAll(false)">清空</button> <span id="scope-chunk-sel-n" style="color:var(--mut);font-size:11px;">已选 0 切片</span></div>';
    Object.entries(groups).forEach(([sec,arr])=>{
      g += '<div class="scope-sec" style="margin-bottom:4px;">';
      g += `<div style="margin:6px 0 2px;display:flex;align-items:center;gap:6px;"><b style="font-size:11.5px;">${esc(sec)}</b><button class="btn sm ghost" onclick="scopeSelectSection(this,true)">本节约</button><button class="btn sm ghost" onclick="scopeSelectSection(this,false)">本节清空</button></div>`;
      arr.forEach(c=>{
        const cc = c.content || '';
        g += `<label style="display:flex;gap:6px;align-items:flex-start;font-size:11px;padding:2px 4px;cursor:pointer;">
<input type="checkbox" class="scope-chunk-cb" data-idx="${c.chunk_index}" onchange="scopeChunkCount()">
<span style="color:var(--mut);flex:1;word-break:break-all;">${esc(cc.slice(0,60))}${cc.length>60?'…':''}</span>
<span class="tag">§${c.chunk_index}</span></label>`;
      });
      g += '</div>';

    });
    g += `<div style="margin-top:8px;display:flex;gap:8px;align-items:center;"><button class="btn" onclick="scopeSaveChunks()">保存为范围并选中</button><span style="color:var(--mut);font-size:11px;">保存后加入所选范围</span></div>`;
    tree.innerHTML = g;
  }catch(e){ tree.innerHTML = '<div style="color:var(--red);font-size:12px;">加载失败：'+esc(e.message)+'</div>'; }
}
async function scopeSaveChunks(){
  const fn = ((document.getElementById('scope-doc-select')||{}).value||'').split('::')[1] || '';
  const cbs = [...document.querySelectorAll('.scope-chunk-cb:checked')];
  if(!cbs.length){ toast('请至少勾选一个切片'); return; }
  const idxs = new Set(cbs.map(cb=>+cb.dataset.idx));
  const chunk_ids = _scopeDocChunks.filter(c=> idxs.has(c.chunk_index))
    .map(c=>({document_id:c.document_id, chunk_index:c.chunk_index, section:c.section, content:c.content}));
  const fragment_text = chunk_ids.map(c=>c.content||'').join('\n\n');
  const name = ((document.getElementById('scope-chunk-name')||{}).value||'').trim() || fn;
  const saved = await api('/api/scopes',{method:'POST',body:JSON.stringify({name, mode:'chunk', doc_names:[fn], fragment_text, chunk_ids})});
  activeScopes.push({id:saved.id, name:saved.name, mode:'chunk', doc_names:[fn]});
  renderScopeChip(); closeModal(); toast('已保存并加入：'+saved.name);
}


function updateKBPickCount() {
  const n = document.querySelectorAll('.kb-pick-cb:checked').length;
  const btn = document.getElementById('kb-pick-confirm');
  if(btn) btn.textContent = n ? `插入所选（${n}）` : '插入所选';
}
function pickKBTags() {
  const picked = [...document.querySelectorAll('.kb-pick-cb:checked')].map(c=>c.value);
  const inp = document.getElementById('chat-input');
  if(!inp) return;
  if(picked.length) {
    inp.value = (inp.value ? inp.value + ' ' : '') + picked.map(n=>'#' + n + ' ').join('');
    toast(`已引用 ${picked.length} 个知识库文件`);
  }
  inp.focus();
  closeModal();
}
function closeToolPop() {
  const pop = document.getElementById('tool-pop');
  const btn = document.getElementById('tool-btn');
  if(pop) pop.classList.remove('show');
  if(btn) btn.classList.remove('on');
}
document.addEventListener('click', e => {
  const pop = document.getElementById('tool-pop');
  if(!pop || !pop.classList.contains('show')) return;
  if(pop.contains(e.target) || e.target.id==='tool-btn') return;
  closeToolPop();
});

// V2.3 语音输入：Web Speech API（中文）,识别结果填入输入框
let voiceRec = null;
function toggleVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById('tool-btn');  // V2.3.1: 录音中态绑定到 ➕ 主按钮
  if(!SR) { toast('当前浏览器不支持语音输入（建议 Chrome/Edge）'); return; }
  if(!voiceRec) {
    voiceRec = new SR();
    voiceRec.lang = 'zh-CN';
    voiceRec.interimResults = false;
    voiceRec.continuous = false;
    voiceRec.onresult = e => {
      const txt = e.results[0][0].transcript;
      const inp = document.getElementById('chat-input');
      inp.value = (inp.value ? inp.value + ' ' : '') + txt;
      toast('语音已识别');
    };
    voiceRec.onerror = e => { toast(`语音识别失败：${e.error}`); btn && btn.classList.remove('on'); };
    voiceRec.onend = () => btn && btn.classList.remove('on');
  }
  if(btn && btn.classList.contains('on')) { try{voiceRec.stop();}catch(e){} btn.classList.remove('on'); }
  else { try { voiceRec.start(); if(btn) btn.classList.add('on'); } catch(e) { toast('启动语音识别失败：'+e.message); } }
}

// V2.4 执行过程全部内联会话流（proc-box），右侧执行面板已移除；此处仅重置内联执行过程
