/* 文档目录树（左栏「结构目录 / 智能视图」）+ 移动/批量移动（2026-09-21）
 *
 * 依据：docs/文档库基于文件的管理-评估与优化方案-20260921.md §4.3（合并版 UX）
 *   - 左栏 220px 可折叠，**分两组、视觉分离**：
 *       「结构目录」= 人工创建，文档可移入（本模块管理；改名 F2/双击、删除 Delete）；
 *       「智能视图」= 系统按状态聚合，**只读**（往里拖文件没有意义——那是视图不是目录）。
 *   - 中栏加「所属目录」列 + 面包屑；批量移动挂到既有 #doc-batch-bar（不另起一套多选）。
 *   - 复用清单（方案 §4.4，不重复造轮子）：
 *       renderPagerBar(15-kb.js) / openPanel(02-shell.js) / docStateBadge+docDerivedState(20-docs.js)
 *       / esc+api+toast(01-core.js) / 目录树视觉沿用本体树 .ont-tnd 的层级语言
 *   - 铁律：只用现有 CSS 变量（--line/--blue-l/--mut/--blue-d/--red/--grn），不引任何依赖。
 *
 * 两个关键实现决定（都是踩过坑后的选择）：
 *   1) **事件委托**，不给每个节点挂 inline onclick。目录名是用户输入，含单引号/双引号会让
 *      `onclick="fn('${name}')"` 直接语法崩（既有模块里 esc() 只转 &<> 不转引号）。
 *      委托到永不重建的 #doc-folders 容器 → 名字里有什么字符都安全，且键盘流集中一处。
 *   2) **目录筛选走服务端**（folder_id + folder_scope，含子目录展开在服务端做），
 *      **智能视图走客户端**（未归类/未入库/解析失败/未分类/回收站 是派生谓词，
 *      服务端没有"未分类"这类参数；且列表本身已是全量客户端分页，口径一致不会打架）。
 */

// ── 状态（与 20-docs.js 的 _doc* 同域，声明也在那里，避免 TDZ）──
// _docFolders   目录树缓存（含 doc_count / doc_count_all）
// _docFolderId  当前选中目录（'' = 全部；0 = 未归类；>0 = 具体目录）
// _docFolderScope  'self' | 'subtree'
// _docSmartView  当前智能视图 key（'' = 无），与 _docFolderId 互斥

const DOC_SMART_VIEWS = [
  { key:'uncategorized', icon:'📥', label:'未归类', test:d=>!(d.folder_id>0) },
  { key:'unstored',      icon:'⏳', label:'未入库', test:d=>(d.lifecycle_status||'')==='stored' },
  { key:'failed',        icon:'⚠',  label:'解析失败', test:d=>(d.parse_status||'')==='failed' },
  { key:'nocategory',    icon:'🏷', label:'未分类', test:d=>!(d.knowledge_category||'') },
  { key:'trash',         icon:'🗑', label:'回收站', test:d=>(d.lifecycle_status||'')==='deprecated' },
];

let _docFoldersBound = false;

// ── 加载与渲染 ──
async function loadDocFolders() {
  const box = document.getElementById('doc-folders');
  if(!box) return;
  try {
    const t = await api('/api/doc-folders');
    _docFolders = (t && t.folders) || [];
    _docUncategorized = (t && t.uncategorized) || 0;
    _docTotalAll = (t && t.total_docs) || 0;
    renderDocFolders();
  } catch(e) {
    box.innerHTML = `<div style="padding:8px;font-size:11px;color:var(--red);">目录加载失败：${esc(e.message)}</div>`;
  }
}

function docFolderName(fid) {
  if(!fid) return '';
  const f = (_docFolders||[]).find(x=>x.id===fid);
  return f ? f.name : '';
}

function renderDocFolders() {
  const box = document.getElementById('doc-folders');
  if(!box) return;
  docBindFolderEvents();

  // 按 parent 分组（保持服务端 parent_id,sort,name 的稳定序）
  const byParent = {};
  (_docFolders||[]).forEach(f=>{ (byParent[f.parent_id] = byParent[f.parent_id] || []).push(f); });

  const nodeHtml = (f, depth) => {
    const sel = !_docSmartView && _docFolderId !== '' && Number(_docFolderId) === f.id;
    const dot = f.doc_count_all > f.doc_count
      ? `<span style="color:var(--mut);font-size:9.5px;" title="含子目录共 ${f.doc_count_all}">/${f.doc_count_all}</span>` : '';
    return `<div class="ont-tnd${sel?' on':''}" data-fid="${f.id}" data-act="select" tabindex="-1"
        style="padding-left:${depth*14+8}px" title="${esc(f.path)}&#10;单击选中 · 双击/F2 重命名 · Delete 删除">
      <span style="color:var(--mut);flex:none;">${depth?'⤷':'▸'}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(f.name)}</span>
      <span style="color:var(--mut);font-size:10px;" title="本目录 ${f.doc_count} 份">${f.doc_count}${dot}</span>
      <span class="ont-tadd" data-act="add" data-fid="${f.id}" title="在此目录下新建子目录">＋</span>
      <span class="ont-del" data-act="del" data-fid="${f.id}" title="删除目录（其中的文档回到「未归类」，不会删文档）">✕</span>
    </div>` + (function ch(){
      // 子节点：物化路径已保序，按 parent 递归即可（深度 ≤ 8，无递归风险）
      return (byParent[f.id]||[]).map(c=>nodeHtml(c, depth+1)).join('');
    })();
  };

  const structRows = (byParent[0]||[]).map(f=>nodeHtml(f, 0)).join('');
  const allSel = _docFolderId === '' && !_docSmartView;

  const smartRows = DOC_SMART_VIEWS.map(v=>{
    const n = (_docs||[]).filter(v.test).length;
    const sel = _docSmartView === v.key;
    return `<div class="ont-tnd${sel?' on':''}" data-smart="${v.key}" data-act="smart" tabindex="-1"
        style="padding-left:8px" title="系统聚合视图（只读，不可拖入）">
      <span style="color:var(--mut);flex:none;">${v.icon}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${v.label}</span>
      <span style="color:var(--mut);font-size:10px;">${n}</span>
    </div>`;
  }).join('');

  box.innerHTML = `
    <div style="display:flex;align-items:center;padding:4px 8px;font-size:10.5px;color:var(--mut);text-transform:none;" title="人工创建的结构目录（可把文档移入）">
      <span style="flex:1;font-weight:600;">结构目录</span>
      <span data-act="add" data-fid="0" style="cursor:pointer;font-size:12px;padding:0 3px;" title="在根下新建目录">＋</span>
    </div>
    <div class="ont-tnd${allSel?' on':''}" data-fid="" data-act="select" tabindex="-1" style="padding-left:8px" title="显示全部文档（不按目录筛选）">
      <span style="color:var(--mut);flex:none;">📚</span>
      <span style="flex:1;">全部文档</span><span style="color:var(--mut);font-size:10px;">${_docTotalAll||(_docs||[]).length}</span>
    </div>
    ${structRows || '<div style="padding:4px 8px 6px 22px;font-size:10.5px;color:var(--mut);">（暂无目录，点右上 ＋ 新建）</div>'}
    <div style="display:flex;align-items:center;padding:8px 8px 4px;margin-top:4px;border-top:1px solid var(--line);font-size:10.5px;color:var(--mut);" title="系统按状态聚合，只读；不能把文件拖进来">
      <span style="flex:1;font-weight:600;">智能视图</span>
    </div>
    ${smartRows}`;
}

// ── 事件委托（唯一入口，名字含引号也不会崩）──
function docBindFolderEvents() {
  if(_docFoldersBound) return;
  const box = document.getElementById('doc-folders');
  if(!box) return;
  _docFoldersBound = true;
  box.addEventListener('click', ev=>{
    const t = ev.target.closest('[data-act]');
    if(!t || !box.contains(t)) return;
    const act = t.getAttribute('data-act');
    ev.stopPropagation();
    if(act === 'add')   { docFolderCreate(t.getAttribute('data-fid') || 0); return; }
    if(act === 'del')   { docFolderDelete(Number(t.getAttribute('data-fid'))); return; }
    if(act === 'smart') { docSelectSmartView(t.getAttribute('data-smart')); return; }
    if(act === 'select'){
      const raw = t.getAttribute('data-fid');
      docSelectFolder(raw === '' || raw === null ? '' : Number(raw));
      box.focus();
      return;
    }
  });
  box.addEventListener('dblclick', ev=>{
    const t = ev.target.closest('[data-act="select"]');
    if(!t) return;
    const raw = t.getAttribute('data-fid');
    if(raw === '' || raw === null) return;   // 「全部文档」不可改名
    docFolderRename(Number(raw));
  });
  // 键盘流（方案 §4.3）：Enter 选中 · F2 改名 · Delete 删除 · Esc 清除筛选
  box.addEventListener('keydown', ev=>{
    const t = ev.target.closest('[data-act]');
    if(ev.key === 'Escape'){ docSelectFolder(''); docSelectSmartView(''); return; }
    if(!t) return;
    if(ev.key === 'Enter'){ ev.preventDefault(); t.click(); return; }
    const raw = t.getAttribute('data-fid');
    if(raw === '' || raw === null) return;
    if(ev.key === 'F2'){ ev.preventDefault(); docFolderRename(Number(raw)); return; }
    if(ev.key === 'Delete'){ ev.preventDefault(); docFolderDelete(Number(raw)); }
  });
}

// ── 选择态 ──
function docSelectFolder(fid) {
  _docFolderId = (fid === '' || fid === null || fid === undefined) ? '' : Number(fid);
  _docSmartView = '';                    // 与智能视图互斥（两种筛选口径不叠加，避免"筛完什么都没有"）
  _docSelected.clear();
  loadDocs();                            // loadDocs 内部会刷新目录树与面包屑
}

function docSelectSmartView(key) {
  _docSmartView = (_docSmartView === key) ? '' : (key || '');
  _docFolderId = '';
  _docSelected.clear();
  loadDocs();
}

function docSetFolderScope(scope) {
  _docFolderScope = (scope === 'subtree') ? 'subtree' : 'self';
  loadDocs();
}

function docToggleLeft() {
  const left = document.getElementById('doc-left');
  const bt = document.getElementById('doc-left-collapse');
  if(!left) return;
  const collapsed = left.getAttribute('data-collapsed') === '1';
  const next = collapsed ? '0' : '1';
  left.setAttribute('data-collapsed', next);
  // ⚠️ 关键坑：min/max 必须跟着 width 一起改。只写 width=40px 而 minWidth 仍是 220px 时，
  // min-width 会把宽度**夹回** 220px —— DOM 属性与按钮文本都对了，用户却看不到任何变化
  // （实测：折叠后 getBoundingClientRect().width 仍是 220，肉眼完全没反应）。
  if(next === '1'){
    left.style.width = '40px';
    left.style.minWidth = '40px';
    left.style.maxWidth = '40px';
    left.style.overflow = 'hidden';   // 折叠后内容会溢出，必须裁掉
  }else{
    left.style.width = '220px';
    left.style.minWidth = '40px';
    left.style.maxWidth = '220px';
    left.style.overflow = '';
  }
  // 折叠后隐藏标题/新建按钮/目录树，避免 40px 宽里挤出一堆溢出文字
  left.querySelectorAll('[data-page-node-id="docleft-title"],[data-page-node-id="docleft-new"],#doc-folders')
      .forEach(el => { el.style.display = (next === '1') ? 'none' : ''; });
  if(bt) bt.textContent = collapsed ? '«' : '»';
}

// ── 面包屑 ──
function renderDocCrumb() {
  const el = document.getElementById('doc-crumb');
  if(!el) return;
  const parts = [];
  if(_docSmartView) {
    const v = DOC_SMART_VIEWS.find(x=>x.key===_docSmartView);
    if(v) parts.push(`<span style="color:var(--blue-d);font-weight:600;">${v.icon} ${v.label}</span>
      <span style="color:var(--mut);" title="系统聚合视图：只读，不按目录筛选">（智能视图，可与上方筛选叠加）</span>`);
  } else if(_docFolderId !== '') {
    // 面包屑由 path 反推（'/规范/热管理/' → [规范, 热管理]），不额外发请求
    const f = (_docFolders||[]).find(x=>x.id===Number(_docFolderId));
    const seg = f ? (f.path||'').split('/').filter(Boolean) : [];
    parts.push(`<span style="cursor:pointer;color:var(--blue-d);" onclick="docSelectFolder('')" title="返回全部文档">全部文档</span>`);
    seg.forEach((name,i)=>{
      parts.push('<span style="color:var(--mut);">/</span>');
      const isLast = i === seg.length-1;
      parts.push(isLast
        ? `<b>${esc(name)}</b>`
        : `<span style="color:var(--blue-d);">${esc(name)}</span>`);
    });
    parts.push(`<label style="margin-left:10px;font-size:11px;color:var(--mut);cursor:pointer;" title="勾选后，子目录里的文档也一并显示">
        <input type="checkbox" ${_docFolderScope==='subtree'?'checked':''} onchange="docSetFolderScope(this.checked?'subtree':'self')"> 含子目录</label>`);
  } else {
    return;   // 无筛选 → 不占位
  }
  el.style.display = 'flex';
  el.innerHTML = parts.join('');
}

// ── 目录写操作 ──
async function docFolderCreate(parentId) {
  const parentName = parentId ? (docFolderName(Number(parentId)) || `#${parentId}`) : '根目录';
  const name = prompt(`在「${parentName}」下新建目录：\n（同级下不能重名）`, '');
  if(name === null) return;
  if(!name.trim()) { toast('目录名不能为空'); return; }
  try {
    const r = await api('/api/doc-folders', {
      method:'POST', body: JSON.stringify({ name: name.trim(), parent_id: Number(parentId) || 0 })
    });
    if(!r.ok) { toast('新建失败：' + (r.error || '未知原因')); return; }
    toast(`📁 已创建「${name.trim()}」`);
    _docTreeStale = true;
    loadDocs();     // 内部会重载目录树
  } catch(e) { toast('新建失败：' + e.message); }
}

async function docFolderRename(fid) {
  const old = docFolderName(fid);
  if(!old) return;
  const name = prompt('重命名目录：', old);
  if(name === null || name.trim() === old) return;
  if(!name.trim()) { toast('目录名不能为空'); return; }
  try {
    const r = await api(`/api/doc-folders/${fid}`, { method:'PUT', body: JSON.stringify({ name: name.trim() }) });
    if(!r.ok) { toast('重命名失败：' + (r.error || '未知原因')); return; }
    toast(`📁 已重命名为「${name.trim()}」`);
    _docTreeStale = true;
    loadDocs();
  } catch(e) { toast('重命名失败：' + e.message); }
}

async function docFolderDelete(fid) {
  const name = docFolderName(fid);
  if(!name) return;
  // 先取真实数字做确认文案（子目录数/文档数），别让用户凭感觉点
  const f = (_docFolders||[]).find(x=>x.id===fid) || {};
  // 后代数：物化路径前缀法（与后端 _descendant_ids 同口径），不递归、不发请求
  const _base = f.path || '';
  const sub = _base
    ? (_docFolders||[]).filter(x=>x.id!==fid && String(x.path||'').startsWith(_base)).length
    : 0;
  const ok = await confirmDialog(
    `删除目录「${name}」？\n\n· 其下 ${f.doc_count||0} 份文档（含子目录共 ${f.doc_count_all||0} 份）将回到「未归类」\n· **不会删除任何文档**\n· 子目录也会一并删除${sub?`（${sub} 个）`:''}`);
  if(!ok) return;
  try {
    const r = await api(`/api/doc-folders/${fid}`, { method:'DELETE' });
    if(!r.ok) { toast('删除失败：' + (r.error || '未知原因')); return; }
    toast(`🗑 已删除「${r.deleted}」· ${r.moved_to_uncategorized||0} 份文档回到未归类`);
    if(Number(_docFolderId) === fid) _docFolderId = '';
    _docTreeStale = true;
    loadDocs();
  } catch(e) { toast('删除失败：' + e.message); }
}

// ── 移动 ──
function docMoveTargetsHtml() {
  const rows = [`<option value="0">📥 未归类（不放入任何目录）</option>`];
  const walk = (pid, depth) => {
    (_docFolders||[]).filter(f=>f.parent_id===pid).forEach(f=>{
      rows.push(`<option value="${f.id}">${'　'.repeat(depth)}${esc(f.name)}</option>`);
      walk(f.id, depth+1);
    });
  };
  walk(0, 0);
  return rows.join('');
}

async function docMovePrompt(docId) {
  const html = `<div style="font-size:12.5px;">
    <div style="margin-bottom:6px;color:var(--mut);">把文档移动到：</div>
    <select id="doc-move-sel" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
      ${docMoveTargetsHtml()}
    </select>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button class="btn sm ghost" onclick="closePanel()">取消</button>
      <button class="btn sm" onclick="docMoveConfirm(${docId})">移动</button>
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--mut);">提示：移动只改归属目录，不重算向量、不改文件名、不动磁盘副本。</div>
  </div>`;
  openPanel('📁 移动文档到目录', html);
}

async function docMoveConfirm(docId) {
  const sel = document.getElementById('doc-move-sel');
  const fid = sel ? Number(sel.value) : 0;
  try {
    const r = await api(`/api/documents/${docId}/move`, { method:'POST', body: JSON.stringify({ folder_id: fid }) });
    if(!r.ok) { toast('移动失败：' + (r.error || '未知原因')); return; }
    closePanel();
    toast(fid ? `📁 已移动到「${docFolderName(fid) || fid}」` : '📥 已移出到「未归类」');
    _docTreeStale = true;   // 目录计数变了
    loadDocs();
  } catch(e) { toast('移动失败：' + e.message); }
}

async function batchMoveSelected() {
  const ids = [..._docSelected];
  if(!ids.length) { toast('请先勾选文档'); return; }
  const html = `<div style="font-size:12.5px;">
    <div style="margin-bottom:6px;color:var(--mut);">把选中的 <b>${ids.length}</b> 份文档移动到：</div>
    <select id="doc-move-sel" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
      ${docMoveTargetsHtml()}
    </select>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button class="btn sm ghost" onclick="closePanel()">取消</button>
      <button class="btn sm" onclick="batchMoveConfirm()">移动 ${ids.length} 份</button>
    </div>
  </div>`;
  openPanel('📁 批量移动到目录', html);
}

async function batchMoveConfirm() {
  const ids = [..._docSelected];
  if(!ids.length) { toast('请先勾选文档'); return; }
  const sel = document.getElementById('doc-move-sel');
  const fid = sel ? Number(sel.value) : 0;
  try {
    const r = await api('/api/documents/batch', {
      method:'POST', body: JSON.stringify({ action:'move', ids, folder_id: fid })
    });
    const ok = r.ok || 0, failed = (r.failed || []).length;
    toast(`📁 移动完成：成功 ${ok}${failed?` · 失败 ${failed}`:''}`);
    closePanel();
    _docSelected.clear();
    _docTreeStale = true;   // 目录计数变了
    loadDocs();
  } catch(e) { toast('批量移动失败：' + e.message); }
}
