/* 文档：上传 / 抽取 / 元数据 / 数据源
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 8990-9638  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function uploadDocWithMeta(input) {
  const files = Array.from(input.files || []);
  if(!files.length) return;
  pendingDocFiles = files;
  pendingDocFile = files[0];
  document.getElementById('doc-upload-wrap').style.display = 'block';
  document.getElementById('dfc-name').textContent = files.length > 1 ? `已选 ${files.length} 个文件` : files[0].name;
  const f0 = files[0];
  const ext = f0.name.split('.').pop() || '-';
  document.getElementById('dfc-meta').textContent = files.length > 1
    ? `${files.length} 个文件 · 共 ${(files.reduce((a,f)=>a+f.size,0)/1024).toFixed(1)} KB · 批量上传（逐个解析）`
    : `${ext.toUpperCase()} · ${(f0.size/1024).toFixed(1)} KB · ${(f0.size/1024/1024).toFixed(2)} MB`;
  document.getElementById('doc-file-card').style.display = 'block';
  // 批量列表
  const bl = document.getElementById('doc-batch-list');
  if(files.length > 1) {
    bl.style.display = 'block';
    bl.innerHTML = files.map(f=>`<div style="font-size:11px;padding:2px 4px;">📄 ${esc(f.name)} <small style="color:var(--mut);">${(f.size/1024).toFixed(1)}KB</small></div>`).join('');
  } else { bl.style.display = 'none'; bl.innerHTML = ''; }
  // 默认预填：标题=文件名（去扩展名）、作者=王工
  if(!document.getElementById('dm-title').value) {
    document.getElementById('dm-title').value = f0.name.replace(/\.[^.]+$/, '');
  }
  if(!document.getElementById('dm-author').value) {
    document.getElementById('dm-author').value = '王工';
  }
}
function clearDocFile() {
  pendingDocFiles = [];
  pendingDocFile = null;
  document.getElementById('doc-upload-input').value = '';
  document.getElementById('doc-upload-wrap').style.display = 'none';
  document.getElementById('doc-file-card').style.display = 'none';
  document.getElementById('doc-upload-progress').style.display = 'none';
  document.getElementById('doc-extract-result').style.display = 'none';
  document.getElementById('doc-batch-list').style.display = 'none';
  document.getElementById('doc-batch-list').innerHTML = '';
}
function setUploadStage(idx, state, statusText) {
  for(let i=1;i<=5;i++) {
    const el = document.getElementById('up-stage'+i);
    if(!el) continue;
    el.className = 'st ' + (i < idx ? 'ok' : (i === idx ? (state==='run'?'w':(state==='done'?'ok':'r')) : 'w'));
    if(i < idx) el.textContent = el.textContent.replace(/^[^ ]+ /, '✓ ');
  }
  if(statusText) document.getElementById('up-status').textContent = statusText;
}
// 实体类型着色：参考通用产品「按类型颜色编码」的展示惯例（不同实体类型以颜色区分）
const TYPE_COLORS = {
  '载荷':'#2f6fed','天线':'#0e9f6e','需求':'#c27c1b','部件':'#8a5cf6','系统':'#0891b2',
  '性能':'#c2410c','接口':'#be185d','约束':'#b91c1c','状态':'#4f46e5','数据':'#0d9488'
};
function typeColor(t){ return TYPE_COLORS[t] || graphHashColor(t); }
// 抽取结果内嵌渲染（上传后直接可见，S2：常驻展示 + 去审核跳转）
// 展示优化：分类统计徽章 + 按实体类型分组着色 + 关系三元组（主体→关系→客体）
function renderExtractResult(docRef, vr, total, docId) {
  const el = document.getElementById('doc-extract-result');
  if(!el) return;
  el.style.display = 'block';
  el.scrollIntoView({behavior:'smooth', block:'nearest'});
  // 开关关闭：明确提示未开启（区别于「抽取完成 0 条」）
  if(vr && vr.enabled === false) {
    el.innerHTML = `<div style="font-size:12px;"><b>⚙ 文档库实体抽取未开启</b>
      <div style="font-size:11px;color:var(--mut);margin-top:3px;">上传文档仅完成 解析→分块→向量化，不自动抽取实体/关系候选（系统默认）。</div></div>`;
    return;
  }
  if(!total) {
    el.innerHTML = `<div style="font-size:12px;"><b>🔍 抽取完成：0 条候选</b>
      <div style="font-size:11px;color:var(--mut);margin-top:3px;">文档内容未匹配到本体类型词（载荷/天线/需求/部件…）。可在「数据看板」检索后手动抽取，或先在本体模型添加实体类型。
      <button class="btn sm ghost" style="margin-left:6px;" onclick="goReviewTab()">去治理中心</button></div></div>`;
    window._lastV2GBatch = vr.batch_id;
    return;
  }
  const cands = (vr.candidates||[]).slice(0,30);
  const rels = cands.filter(c=>c.entity_type==='关系候选' && (c.rel_type||c.rel_source||c.rel_target));
  const ents = cands.filter(c=>c.entity_type!=='关系候选');
  const nNode = (vr.node_count!=null && vr.node_count>=0) ? vr.node_count : ents.length;
  const nEdge = (vr.edge_count!=null && vr.edge_count>=0) ? vr.edge_count : rels.length;
  // 实体按类型分组（同名去重）
  const groups = {};
  ents.forEach(c=>{ const t = c.entity_type||'未分类'; (groups[t]=groups[t]||[]).push(c.name); });
  const groupHtml = Object.entries(groups).map(([t,names])=>{
    const col = typeColor(t);
    const uniq = [...new Set(names)];
    return `<div style="margin:3px 0;display:flex;align-items:flex-start;gap:4px;flex-wrap:wrap;">
      <span class="tag" style="background:${col}1a;color:${col};border:1px solid ${col}55;font-weight:600;flex:none;margin:2px 0;">${esc(t)} × ${uniq.length}</span>
      <span style="flex:1;min-width:0;">${uniq.map(n=>`<span class="tag" style="margin:2px 3px;color:var(--ink);">${esc(n)}</span>`).join('')}</span>
    </div>`;
  }).join('');
  // 关系候选以三元组「主体 →(关系) 客体」展示
  const relHtml = rels.length ? `<div style="font-size:11px;color:var(--mut);margin:8px 0 2px;">🔗 关系候选</div>
    ${rels.map(r=>`<div style="font-size:12px;display:flex;gap:6px;align-items:center;margin:3px 0;flex-wrap:wrap;">
      <b style="color:var(--ink);">${esc(r.rel_source||'-')}</b>
      <span class="st b">${esc(r.rel_type||'关联')}</span>
      <b style="color:var(--ink);">${esc(r.rel_target||'-')}</b>
    </div>`).join('')}` : '';
  const rejectInfo = (vr.rejected||[]).length
    ? `<span style="color:var(--red);">校验拒绝 ${vr.rejected.length} 条：${esc((vr.rejected||[]).slice(0,3).map(r=>r.errors||'').join('；'))}</span>`
    : '全部通过本体预校验';
  el.innerHTML = `<div style="font-size:12.5px;">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
      <b>🔍 抽取完成</b>
      <span class="st ok">实体 ${nNode}</span>
      <span class="st b">关系 ${nEdge}</span>
      <span class="st w">候选合计 ${total}</span>
      <span style="flex:1"></span>
      <button class="btn sm" onclick="goReviewTab()">✅ 去治理中心确认</button>
    </div>
    ${groupHtml}
    ${relHtml}
    <div style="font-size:11px;color:var(--mut);margin-top:8px;">${rejectInfo} · <span style="opacity:.8">确认后进入实体/关系审核队列正式审核</span></div>
  </div>`;
  // 保存本次批次号，标注审核页可定位
  window._lastV2GBatch = vr.batch_id;
}
// 跳转到标注审核子页（二级页已提升至主导航）并加载待审候选
function goReviewTab() {
  go('kb','kb-b');
  // IA 收敛：候选批次已并入数据整理左栏第 0 站，直达 batch 队列
  setTimeout(()=>{
    fusNav(document.querySelector('.fus-nav-btn[data-fpane="batch"]'), 'batch');
  }, 600);
}
// P2 文档→抽取闭环：从文档列表直达治理中心并自动定位该文档的抽取批次
function gotoV2GBatch(batchId) {
  window._v2gLocateBatch = batchId || null;
  goReviewTab();
}
async function doUploadDoc() {
  // 文档全局化：**不**受分支只读门禁约束——documents 是全局资产（服务端 /api/documents/upload
  // 硬编码 branch='global'，只校验 DOC_WRITE_PERMS，无任何分支校验）。
  // 旧实现在此处调用 branchWritable() 会在 release 分支上拦下上传并提示"切到 dev 分支编辑"，
  // 属分支隔离模型的残留（分支只读针对实体/关系，与文档无关）。
  const files = pendingDocFiles.length ? pendingDocFiles : (pendingDocFile ? [pendingDocFile] : []);
  if(!files.length) { toast('请先选择文件'); return; }
  const prog = document.getElementById('doc-upload-progress');
  const _ues = document.getElementById('up-extract-stage'); if(_ues) _ues.style.display = _fileExtractEnabled ? '' : 'none';
  const bar = document.getElementById('up-bar');
  prog.style.display = 'block';
  document.getElementById('doc-file-card').style.display = 'none';
  const author = document.getElementById('dm-author').value.trim() || '王工';
  const version = document.getElementById('dm-version').value.trim() || 'v1.0';
  const tags = document.getElementById('dm-tags').value.trim();
  const totalN = files.length;
  let okCount = 0, failCount = 0;
  for(let fi=0; fi<totalN; fi++) {
    const f = files[fi];
    const label = totalN > 1 ? `[${fi+1}/${totalN}] ${f.name}` : f.name;
    setUploadStage(1, 'run', `(${label}) 解析文档内容…`); bar.style.width = '10%';
    const fd = new FormData();
    fd.append('file', f);
    // 标题：单文件用表单值；批量则每个文件用文件名（避免全队列同标题）
    const title = totalN > 1 ? '' : document.getElementById('dm-title').value.trim();
    fd.append('title', title);
    fd.append('author', author);
    fd.append('version', version);
    fd.append('tags', tags);
    // 文档全局化：上传不再携带分支（后端统一写 global，不随分支变化）
    // 落点（2026-09-21 目录树）：上传落到左栏当前选中的**结构目录**；
    // 正停在回收站/全部文档/未归类时不指定目录（= 未归类），语义与用户看到的一致。
    const _landing = (!_docSmartView && _docFolderId !== '' && Number(_docFolderId) > 0) ? Number(_docFolderId) : 0;
    fd.append('folder_id', String(_landing));
    try {
      const r = await fetch('/api/documents/upload', {method:'POST', body:fd}).then(x=>x.json());
      if(r.error) {
        setUploadStage(1, 'fail', `(${label}) 失败：${r.error||''}`);
        bar.style.width = '100%'; bar.style.background = 'var(--red)';
        toast(`❌ ${f.name} 解析失败：${r.error||''}`);
        failCount++;
        continue;
      }
      setUploadStage(2, 'done', `(${label}) 分块完成`); bar.style.width = '35%';
      setUploadStage(3, 'done', `(${label}) 向量化（${r.embed_version||'-'}）`); bar.style.width = '55%';
      setUploadStage(4, 'done', `(${label}) ✅ 入库完成`); bar.style.width = '75%';
      okCount++;
      // 自动抽取（阶段 5）：后端管道已内嵌抽取，直接用返回的 auto_extract（避免重复调用）
      try {
        const ae = r.auto_extract || {};
        if(!_fileExtractEnabled) {
          // 抽取开关关闭：阶段5隐藏，不展示任何抽取信息
        } else if(ae.enabled) {
          const totalC = (ae.node_count||0) + (ae.edge_count||0);
          setUploadStage(5, 'done', `(${label}) ✅ 抽取 ${ae.extracted||0} 条候选`); bar.style.width = '100%';
          // 最后一个文件的抽取结果内嵌展示（批量时汇总）
          if(fi === totalN-1) renderExtractResult(r.doc_id, ae, totalC, r.doc_id);
        } else if(fi === totalN-1) {
          renderExtractResult(r.doc_id, {enabled:false}, 0, r.doc_id);
        }
      } catch(e) {
        setUploadStage(5, 'fail', `(${label}) 抽取结果展示失败：${e.message}`);
      }
    } catch(e) {
      setUploadStage(1, 'fail', `(${label}) 网络错误：${e.message}`);
      bar.style.width = '100%'; bar.style.background = 'var(--red)';
      toast(`❌ ${f.name} 上传失败：${e.message}`);
      failCount++;
    }
  }
  pendingDocFiles = [];
  pendingDocFile = null;
  document.getElementById('doc-upload-input').value = '';
  document.getElementById('doc-batch-list').innerHTML = '';
  if(totalN > 1) {
    toast(`📦 批量上传完成：成功 ${okCount} / 失败 ${failCount}`);
  }   else if(failCount === 0) {
    const r = null; // 单文件成功 toast 已在上传中提示
  }
  if(okCount) _docTreeStale = true;   // 新增文档 → 目录计数与总数要重拉（见 _docTreeStale 说明）
  loadDocs();
  v2gReviewLoad();
  setTimeout(()=>{ prog.style.display = 'none'; bar.style.width = '0%'; bar.style.background = 'var(--blue)'; }, 1500);
}
// 资料库上传/解析**能力白名单**（2026-09-28 新增，与后端 knowledge_pipeline/extract.py 逐条对齐）。
// ⚠️ 唯一权威在后端 `extract_text_ex()` 的分支表；本常量是它的**前端镜像**，改后端必须同步改这里。
// 用途：格式筛选下拉的**常驻全集** —— 让用户在看不到任何 docx 时也能选「DOCX」并得到空列表，
// 而不是以为"系统不支持 docx"（旧实现只在遇到过的格式里累积，未出现过的格式永远不出现）。
const DOC_FORMAT_CAPABILITY = [
  { ext:'txt',  label:'TXT',  group:'文本',  note:'纯文本' },
  { ext:'md',   label:'MD',   group:'文本',  note:'Markdown' },
  { ext:'log',  label:'LOG',  group:'文本',  note:'日志' },
  { ext:'csv',  label:'CSV',  group:'表格',  note:'逗号分隔' },
  { ext:'xlsx', label:'XLSX', group:'表格',  note:'Excel 新格式' },
  { ext:'xls',  label:'XLS',  group:'表格',  note:'Excel 旧格式' },
  { ext:'docx', label:'DOCX', group:'文档',  note:'Word 新格式' },
  { ext:'doc',  label:'DOC',  group:'文档',  note:'Word 旧格式（解析成功率低）' },
  { ext:'pdf',  label:'PDF',  group:'文档',  note:'PDF（需 pdfplumber）' },
  { ext:'pptx', label:'PPTX', group:'演示',  note:'PowerPoint 新格式' },
  { ext:'ppt',  label:'PPT',  group:'演示',  note:'PowerPoint 旧格式' },
  { ext:'json', label:'JSON', group:'数据',  note:'JSON' },
  { ext:'xml',  label:'XML',  group:'数据',  note:'XML' },
  { ext:'yaml', label:'YAML', group:'数据',  note:'YAML' },
  { ext:'yml',  label:'YML',  group:'数据',  note:'YAML' },
  { ext:'png',  label:'PNG',  group:'图片',  note:'需 OCR' },
  { ext:'jpg',  label:'JPG',  group:'图片',  note:'需 OCR' },
  { ext:'jpeg', label:'JPEG', group:'图片',  note:'需 OCR' },
  { ext:'gif',  label:'GIF',  group:'图片',  note:'需 OCR' },
  { ext:'bmp',  label:'BMP',  group:'图片',  note:'需 OCR' },
  { ext:'webp', label:'WEBP', group:'图片',  note:'需 OCR' },
  { ext:'tiff', label:'TIFF', group:'图片',  note:'需 OCR' },
  { ext:'tif',  label:'TIF',  group:'图片',  note:'需 OCR' },
];

// 用最近一次文档结果增量填充「上传人 / 格式」筛选下拉选项（保留当前选中值）。
// 2026-09-28 修正两个缺陷：
//   ① 旧实现只做 union 从不做差集 —— 一旦某种格式被筛选排除过，它会**永久留在下拉里**
//      （实测：筛过 png 后把 png 文档删掉，下拉仍有 PNG 选项，选它得到空列表）。
//      现在格式选项 = 能力全集 ∪ 实际出现过的格式（差集不再必要，全集本就常驻）。
//   ② 上传人仍按"实际出现过的值"累积（上传人无法枚举，只能从数据里发现），
//      但**改为按当前全量文档重算而非 union** —— 否则某人文档全删光后仍留在下拉里。
function fillDocFilterOptions(docs) {
  const upSel = document.getElementById('doc-uploader-filter');
  const fmSel = document.getElementById('doc-format-filter');
  if(!upSel || !fmSel) return;
  // 上传人：按本次结果重算（不 union，避免幽灵选项）
  const ups = new Set();
  docs.forEach(d=>{ if(d.uploaded_by) ups.add(d.uploaded_by); });
  // 格式：能力全集 + 数据里实际出现过的（含历史遗留的非常规扩展名，不丢）
  const seen = new Set();
  docs.forEach(d=>{ if(d.file_type) seen.add(String(d.file_type).toLowerCase().replace(/^\./,'')); });
  const capExts = new Set(DOC_FORMAT_CAPABILITY.map(c=>c.ext));
  const extra = [...seen].filter(e=>e && !capExts.has(e)).sort();
  const upCur = upSel.value, fmCur = fmSel.value;
  // 保持既有 option 中已选但本次未出现的上传人（否则筛选态会被静默重置）
  if(upCur) ups.add(upCur);
  upSel.innerHTML = '<option value="">全部上传人</option>'
    + [...ups].sort().map(u=>`<option value="${esc(u)}">${esc(u)}</option>`).join('');
  upSel.value = upCur;
  // 格式下拉按「分组」组织，共 23 种内置能力 + 数据中发现的额外扩展名
  const groups = [];
  DOC_FORMAT_CAPABILITY.forEach(c=>{
    let g = groups.find(x=>x.name===c.group);
    if(!g) { g = { name:c.group, items:[] }; groups.push(g); }
    g.items.push(c);
  });
  let fmHtml = '<option value="">全部格式</option>';
  groups.forEach(g=>{
    // ⚠️ 分组名字段是 g.name（构建时 {name:c.group, items:[]}）—— 写成 g.group 会得到
    //    esc(undefined)='' → label 静默变空串（实测踩过：下拉分组标题全部消失但不报错）。
    fmHtml += `<optgroup label="${esc(g.name)}">`
      + g.items.map(c=>`<option value="${esc(c.ext)}" title="${esc(c.note)}">${esc(c.label)}</option>`).join('')
      + '</optgroup>';
  });
  if(extra.length) {
    fmHtml += '<optgroup label="其他（数据中出现）">'
      + extra.map(e=>`<option value="${esc(e)}">${esc(e.toUpperCase())}</option>`).join('')
      + '</optgroup>';
  }
  fmSel.innerHTML = fmHtml;
  // ⚠️ 恢复选中值：若当前选中值已不在新选项里（例如选了个已被清空的非常规格式），
  //    value 赋值会静默失败并回落成第一个 option（=""）→ 表现为"筛选自己跳回全部"。
  //    这里显式检测并提示，让状态变化可见。
  fmSel.value = fmCur;
  if(fmCur && fmSel.value !== fmCur) {
    fmSel.value = '';
    if(typeof toast === 'function') toast('所选格式已不在可选范围内，已重置为「全部格式」');
  }
}
// ── 资料库·实体与关系抽取设置（设置页「📄 文件抽取」Tab；开关默认关 + 候选来源默认 sysml）──
let _fileExtractEnabled = false;    // 上传自动抽取开关（settings.file_auto_extract_enabled）
let _entityCandidateSource = 'sysml'; // 实体候选来源（settings.entity_candidate_source）
async function loadFileExtractSettings() {
  try {
    const s = await api('/api/settings');
    _fileExtractEnabled = (s && (s['file_auto_extract_enabled']||'0')) === '1';
    _entityCandidateSource = (s && s['entity_candidate_source']) || 'sysml';
  } catch(e) { /* 静默：保持默认（关闭 + sysml） */ }
}
// ── 资料库列表 hover 摘要卡片（2026-09-08）──
const _dhcCache = Object.create(null);
let _dhcTimer = null, _dhcId = 0;
// 文件名未含格式后缀时补显（如 .PDF）——列表格式列已移除，格式并入文件名（2026-09-10 米爸）
function docExtSuffix(d){
  if(!d || !d.file_type) return '';
  const ext = String(d.file_type).toLowerCase().replace(/^\./,'');
  if(!ext) return '';
  const fn = String(d.filename||'').toLowerCase();
  if(fn.endsWith('.' + ext)) return '';
  return ` <small style="color:var(--mut);font-weight:600;">.${esc(ext.toUpperCase())}</small>`;
}
// hover 卡片精简版（2026-09-10 米爸）：只展示 名称 + 摘要，其余信息列表已有
function _dhcRender(d, fallback){
  const el = document.getElementById('doc-hover-card');
  if(!el || !d) return;
  const statBadge = d.parse_status==='completed'
    ? '<span class="st ok">已完成</span>'
    : d.parse_status==='failed'
    ? '<span class="st r">失败</span>'
    : '<span class="st w">解析中</span>';
  const sum = (d.summary && d.summary.trim())
    ? `<div class="dhc-sum">${esc(d.summary)}</div>`
    : `<div class="dhc-sum empty">（暂无可摘要正文，文档可能为空或尚未分块完成）</div>`;
  el.innerHTML = `
    <div class="dhc-title">${esc(d.filename)} ${docExtSuffix(d)} ${statBadge}</div>
    ${sum}`;
}
function docHoverEnter(evt, id, fallback){
  if(_dhcTimer) clearTimeout(_dhcTimer);
  _dhcId = id;
  const local = (window._docs||[]).find(x=>x.id===id) || fallback || {};
  const cache = _dhcCache[id];
  _dhcRender(Object.keys(cache||{}).length ? Object.assign({}, local, cache) : local);
  const el = document.getElementById('doc-hover-card');
  el.style.display = 'block';
  docHoverMove(evt);
  if(cache){ return; }
  // 异步拉取摘要（避免快速划过触发请求）
  _dhcTimer = setTimeout(async ()=>{
    if(_dhcId !== id) return;
    try{
      const d = await api('/api/documents/'+id+'/preview');
      if(_dhcId !== id) return;
      _dhcCache[id] = d;
      _dhcRender(Object.assign({}, local, d));
    }catch(e){ /* 静默：保持列表项骨架 */ }
  }, 220);
}
function docHoverMove(evt){
  const el = document.getElementById('doc-hover-card');
  if(!el || el.style.display==='none') return;
  const W = el.offsetWidth || 340, H = el.offsetHeight || 200;
  const pad = 14;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if(x + W > window.innerWidth - 8) x = evt.clientX - W - pad;
  if(y + H > window.innerHeight - 8) y = evt.clientY - H - pad;
  if(x < 8) x = 8; if(y < 8) y = 8;
  el.style.left = x + 'px';
  el.style.top = y + 'px';
}
function docHoverLeave(){
  if(_dhcTimer){ clearTimeout(_dhcTimer); _dhcTimer = null; }
  _dhcId = 0;
  const el = document.getElementById('doc-hover-card');
  if(el) el.style.display = 'none';
}
// 渲染设置页 Tab：拉取当前值填充表单（默认不开启抽取）
async function renderFileExtractSettings() {
  await loadFileExtractSettings();
  const en = document.getElementById('fe-enabled'); if(en) en.value = _fileExtractEnabled ? '1' : '0';
  const src = document.getElementById('fe-source'); if(src) src.value = _entityCandidateSource;
}
async function saveFileExtractSettings() {
  const en = document.getElementById('fe-enabled')?.value || '0';
  const src = document.getElementById('fe-source')?.value || 'sysml';
  try {
    await api('/api/settings/file_auto_extract_enabled', {method:'PUT', body:JSON.stringify({value: en})});
    await api('/api/settings/entity_candidate_source', {method:'PUT', body:JSON.stringify({value: src})});
    _fileExtractEnabled = en === '1';
    _entityCandidateSource = src;
    toast(`✅ 已保存：自动抽取${_fileExtractEnabled?'开启':'关闭'} · 候选来源=${src==='sysml'?'SysML建模数据':'LLM自由抽取'}`);
    loadDocs(); v2gReviewLoad();
  } catch(e) { toast('保存失败：' + e.message); }
}
// ── P0：文档生命周期状态徽章（FR-KG-8/ArcR-5）──
const LIFECYCLE_BADGES = {
  uploaded:    {cls:'lc-info',    icon:'⏳', label:'上传中'},
  processing:  {cls:'lc-info',    icon:'⏳', label:'解析中'},
  stored:      {cls:'lc-success', icon:'✅', label:'待入库'},
  committed:   {cls:'lc-primary', icon:'🎯', label:'正式入库'},
  deprecated:  {cls:'lc-muted',   icon:'🚫', label:'已废弃'},
  archived:    {cls:'lc-warning', icon:'📦', label:'已归档'},
};
function lifecycleBadge(status) {
  const b = LIFECYCLE_BADGES[status] || LIFECYCLE_BADGES.uploaded;
  return `<span class="lc-badge ${b.cls}" title="文档生命周期：${b.label}（${status}）">${b.icon} ${b.label}</span>`;
}

// ── 统一状态推导（2026-09-10 米爸：解析×生命周期合一，列与筛选项完全一致）──
// 优先级：解析失败 > 已废弃 > 已归档 > 向量就绪 > 处理中(上传/解析) > 正式入库
const DOC_STATES = {
  committed:  {cls:'lc-primary', icon:'🎯', label:'正式入库'},
  stored:     {cls:'lc-success', icon:'✅', label:'待入库'},
  processing: {cls:'lc-info',    icon:'⏳', label:'处理中'},
  failed:     {cls:'lc-danger',  icon:'⚠️', label:'解析失败'},
  deprecated: {cls:'lc-muted',   icon:'🚫', label:'已废弃'},
  archived:   {cls:'lc-warning', icon:'📦', label:'已归档'},
};
function docDerivedState(d){
  const lc = d.lifecycle_status || 'uploaded';
  if(d.parse_status === 'failed') return 'failed';
  if(lc === 'deprecated') return 'deprecated';
  if(lc === 'archived') return 'archived';
  if(lc === 'stored') return 'stored';
  if(lc === 'uploaded' || lc === 'processing' || d.parse_status === 'parsing') return 'processing';
  return 'committed';
}
function docStateBadge(d){
  const b = DOC_STATES[docDerivedState(d)] || DOC_STATES.committed;
  return `<span class="lc-badge ${b.cls}" title="状态：${b.label}（解析 ${d.parse_status||'-'} / 生命周期 ${d.lifecycle_status||'uploaded'}）">${b.icon} ${b.label}</span>`;
}

// ── 资料库列表：分页展示（统一分页组件，筛选/刷新回到第一页）──
let _docs = [], _docPage = 1, _docSize = 15, _docExtMap = {};
let _docStateFilter = '';      // P0：统一状态过滤（解析×生命周期合一，2026-09-10）
let _docSelected = new Set();  // P0：批量废弃多选
// 2026-09-21 目录树（「基于文件的管理」；状态声明放在本文件 = 与 _doc* 同域，避免 40-docfolders.js
// 未加载时被 loadDocs() 引用造成 TDZ 报错）：
//   _docFolders       目录树缓存（含 doc_count/doc_count_all）
//   _docFolderId      选中目录（'' = 全部；0 = 未归类；>0 = 具体目录）；服务端筛选
//   _docFolderScope   'self'（仅当前目录）| 'subtree'（含子目录）
//   _docSmartView     智能视图 key（'' = 无）；客户端派生筛选，与 _docFolderId 互斥
//   _docTreeStale     目录树是否需要重新拉取 —— 只在「文档归属目录 / 目录本身」变化时置真。
//                     纯勾选/取消勾选不置真（否则每点一次复选框就多发一次目录请求）。
let _docFolders = [], _docUncategorized = 0, _docTotalAll = 0;
let _docFolderId = '', _docFolderScope = 'subtree', _docSmartView = '', _docTreeStale = true;
// 空态「清除全部筛选条件」：把工具栏所有筛选控件 + 目录/视图选中态一次归零。
// 存在的理由：空态往往由 5~6 个条件叠加造成，让用户逐个找哪个开着手工清是低效且易漏的。
function clearAllDocFilters() {
  ['doc-search','doc-date-from','doc-date-to'].forEach(id=>{
    const el = document.getElementById(id); if(el) el.value = '';
  });
  ['doc-uploader-filter','doc-format-filter','doc-origin-filter','doc-status-filter'].forEach(id=>{
    const el = document.getElementById(id); if(el) el.value = '';
  });
  _docStateFilter = '';
  _docSelected.clear();
  _docFolderId = '';
  _docSmartView = '';
  _docFolderScope = 'subtree';
  loadDocs();
}
async function loadDocs() {
  await loadFileExtractSettings();   // 渲染前同步开关状态（补抽入口/抽取列据此显隐）
  const _feT = document.getElementById('doc-flow-extract'); if(_feT) _feT.style.display = _fileExtractEnabled ? '' : 'none';
  const el = document.getElementById('doc-list');
  const q = (document.getElementById('doc-search')?.value || '').trim();
  const ub = (document.getElementById('doc-uploader-filter')?.value || '');
  const ft = (document.getElementById('doc-format-filter')?.value || '');
  const og = (document.getElementById('doc-origin-filter')?.value || '');
  const df = (document.getElementById('doc-date-from')?.value || '');
  const dt = (document.getElementById('doc-date-to')?.value || '');
  try {
    let url = '/api/documents';
    const params = [];
    // 文档全局化：资料库不随分支变化（后端忽略 branch 参数，这里不再传）
    if(q) params.push('search=' + encodeURIComponent(q));
    if(ub) params.push('uploaded_by=' + encodeURIComponent(ub));
    if(ft) params.push('file_type=' + encodeURIComponent(ft));
    if(og) params.push('origin=' + encodeURIComponent(og));
    if(df) params.push('date_from=' + df);
    if(dt) params.push('date_to=' + dt);
    // 统一「状态」下拉（解析×生命周期合一）：committed/stored/processing/failed/deprecated/archived
    if(_docStateFilter) {
      params.push('state=' + encodeURIComponent(_docStateFilter));
    }
    // 2026-09-21 目录维筛选（服务端做「含子目录」展开；智能视图不走这里，见下方客户端过滤）
    if(!_docSmartView && _docFolderId !== '') {
      params.push('folder_id=' + encodeURIComponent(_docFolderId));
      if(_docFolderScope === 'subtree') params.push('folder_scope=subtree');
    }
    if(params.length) url += '?' + params.join('&');
    let docs = await api(url);
    // 智能视图：客户端派生谓词（服务端没有"未分类"这类参数；且列表本就是全量客户端分页，
    // 口径一致不会出现"左侧计数 3、列表 0 条"这种自相矛盾）
    if(_docSmartView) {
      const v = (typeof DOC_SMART_VIEWS !== 'undefined') ? DOC_SMART_VIEWS.find(x=>x.key===_docSmartView) : null;
      if(v) docs = (docs||[]).filter(v.test);
    }
    _docs = docs;
    _docPage = 1;
    fillDocFilterOptions(docs);
    document.getElementById('doc-count').textContent = docs.length ? `共 ${docs.length} 个` : '';
    const _bb = document.getElementById('doc-batch-bar');
    if(_bb) _bb.style.display = _docSelected.size > 0 ? 'flex' : 'none';
    const _bc = document.getElementById('doc-batch-count');
    if(_bc) _bc.textContent = `已选 ${_docSelected.size} 个`;
    // P2 文档→抽取闭环：按文档名聚合 v2g 批次抽取状态（待审/已确认/已驳回 + 可点击直达治理中心）
    let docExtMap = {};
    try {
      const bs = await api('/api/knowledge/v2g/batches');
      (bs||[]).forEach(b=>{
        const doc = (b.source_docs&&b.source_docs[0]) || '';
        if(!doc) return;
        const e = docExtMap[doc] = docExtMap[doc] || {pending:0, confirmed:0, rejected:0, batch:''};
        e.pending += b.pending_n; e.confirmed += b.confirmed_n; e.rejected += b.rejected_n;
        if(b.pending_n > 0 && !e.batch) e.batch = b.batch_id;
        else if(!e.batch) e.batch = b.batch_id;
      });
    } catch(e) {}
    _docExtMap = docExtMap;
    // 目录树：仅当「文档归属 / 目录本身」变化过才重新拉取（_docTreeStale），
    // 否则只重渲染 —— 智能视图的计数取自刚更新的 _docs，天然最新，零额外请求。
    if(typeof loadDocFolders === 'function') {
      if(_docTreeStale) { _docTreeStale = false; await loadDocFolders(); }
      else renderDocFolders();
    }
    if(typeof renderDocCrumb === 'function') renderDocCrumb();
    renderDocs();
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${esc(e.message)}</div>`; }
}
// ── 资料库列表「更多」下拉：切换显隐，按按钮右下角定位（fixed 避免被滚动容器裁剪）──
// 注：原「🔌 数据源」入口已随 R1=B 数据集成移除（2026-09-01）一并删除，openDataSources 为死代码清理
function renderDocs() {
  const el = document.getElementById('doc-list');
  const pagerEl = document.getElementById('doc-pager');
  const docs = _docs, docExtMap = _docExtMap;
  const total = docs.length;
  const pages = Math.max(1, Math.ceil(total / _docSize));
  if(_docPage > pages) _docPage = pages;
  if(!total) {
    // 空态要区分「真的没有」与「筛没了」——后者必须给出一步清除筛选的出口，
    // 否则用户只看到「暂无匹配文档」，会以为是数据丢了（本仓历史上踩过同类困惑）。
    // 2026-09-28 补强：把**生效中的筛选条件**逐条列出。此前只说"当前视图下没有文档"，
    // 而实际最常见的空态是「目录 + 格式 + 状态」叠加过窄 —— 不点名条件用户只能盲目试。
    const _scoped = _docSmartView || _docFolderId !== '';
    const _conds = [];
    if(_docSmartView === 'trash') _conds.push('视图：🗑 回收站');
    else if(Number(_docFolderId) === 0) _conds.push('视图：📥 未归类');
    else if(_docFolderId !== '') _conds.push('目录：' + (docFolderName(_docFolderId) || _docFolderId)
      + (_docFolderScope === 'subtree' ? '（含子目录）' : '（仅本目录）'));
    const _q = (document.getElementById('doc-search')?.value || '').trim();
    if(_q) _conds.push('搜索：' + _q);
    const _fmt = (document.getElementById('doc-format-filter')?.value || '');
    if(_fmt) _conds.push('格式：' + _fmt.toUpperCase());
    const _ub = (document.getElementById('doc-uploader-filter')?.value || '');
    if(_ub) _conds.push('上传人：' + _ub);
    if(_docStateFilter) _conds.push('状态：' + _docStateFilter);
    const _og = (document.getElementById('doc-origin-filter')?.value || '');
    if(_og) _conds.push('来源：' + _og);
    const _df = (document.getElementById('doc-date-from')?.value || '');
    const _dt = (document.getElementById('doc-date-to')?.value || '');
    if(_df || _dt) _conds.push('时间：' + (_df || '…') + ' ~ ' + (_dt || '…'));
    const _btnAll = '<button class="btn sm ghost" style="margin-top:8px;" onclick="clearAllDocFilters()">清除全部筛选条件</button>';
    el.innerHTML = `<div style="padding:14px;color:var(--mut);font-size:12px;">
        ${_conds.length
          ? '<b style="color:var(--ink);">当前条件下没有文档</b><br>生效条件：' + _conds.map(esc).join(' · ') + '<br>' + _btnAll
          : (_scoped
              ? '当前视图下没有文档。<br>' + _btnAll
              : '暂无匹配文档，可点击右上角「📤 上传文件」上传（自动解析分块向量化）')}</div>`;
    if(pagerEl) pagerEl.innerHTML = '';
    return;
  }
  const items = docs.slice((_docPage-1)*_docSize, _docPage*_docSize);
  el.innerHTML = `<div style="overflow-x:auto;"><table class="t">
    <tr><th><input type="checkbox" ${_docSelected.size > 0 && _docSelected.size === items.length ? 'checked' : ''} onchange="toggleSelectAll(this, ${JSON.stringify(items.map(i=>i.id))})" title="全选当前页"></th><th>文件</th><th>上传人</th><th>上传时间</th><th>知识类别</th><th>所属目录</th><th>版本</th><th>块数</th>${_fileExtractEnabled?'<th>抽取</th>':''}<th>状态</th><th>操作</th></tr>` +
    items.map(d=>{
      const ex = docExtMap[d.filename];
      // 抽取列：展示既有候选统计（真实数据）；无候选时按开关状态提示「未抽取/—」
      let extCell;
      if(ex && (ex.pending+ex.confirmed+ex.rejected) > 0) {
        extCell = `<span style="font-size:11px;">
            <span class="st w" style="cursor:pointer;font-size:10px;" onclick="gotoV2GBatch('${esc(ex.batch)}')" title="点击直达抽取治理中心（按批次过滤）">待审 ${ex.pending}</span>
            <span style="color:var(--grn);font-size:10px;">✓${ex.confirmed}</span>
            <span style="color:var(--red);font-size:10px;">✗${ex.rejected}</span></span>`;
      } else {
        let _pd = {}; try { _pd = JSON.parse(d.pipeline_detail||'{}')||{}; } catch(e) {}
        extCell = _pd.extraction === 'disabled'
          ? '<span style="font-size:11px;color:var(--mut);" title="资料库实体抽取开关未开启">未抽取</span>'
          : '<span style="font-size:11px;color:var(--mut);">-</span>';
      }
      const lc = d.lifecycle_status || 'uploaded';
      const isDep = lc === 'deprecated';
      const isArc = lc === 'archived';
      return `<tr draggable="true" data-drag-doc="${d.id}" oncontextmenu="docContextMenu(event,${d.id})" style="cursor:context-menu;${isDep?'opacity:.55;':''}${isArc?'background:var(--color-bg-muted,#fafbfc);':''}" title="右键：加入会话 / 下载 / 追溯 / 元数据；拖到左栏目录可直接归类">
      <td><input type="checkbox" ${_docSelected.has(d.id)?'checked':''} onchange="toggleDocSelect(${d.id}, this.checked)" title="选中以批量操作"></td>
      <td><div class="dhc-trigger" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" onmouseenter="docHoverEnter(event,${d.id})" onmousemove="docHoverMove(event)" onmouseleave="docHoverLeave()" title="${esc(d.title && d.title !== d.filename ? d.title + '\n' : '')}${esc(d.filename)}"><b>${esc(d.filename)}</b>${d.origin==='ai_generated'?'<span title="AI 建模产物收编（AI 建模检索默认不消费，可在建模范围中显式开启）" style="cursor:help;">🤖</span>':''}${docExtSuffix(d)}<small style="color:var(--mut);margin-left:5px;">${((d.file_size||0)/1024).toFixed(1)}KB</small></div></td>
      <td style="font-size:12px;">${esc(d.uploaded_by||'-')}</td>
      <td style="font-size:11px;color:var(--mut);white-space:nowrap;">${esc((d.created_at||'').slice(0,16))}</td>
      <td style="cursor:pointer;" onclick="kbSetDocCategory(${d.id}, '${esc(d.knowledge_category||'')}')" title="点击设置知识类别（设计方法知识/设计资产）">${kbCatLabel(d.knowledge_category)}</td>

      <td style="font-size:11.5px;">${(()=>{
        // 所属目录（2026-09-21）：未归类显示为可点的兜底入口——「不整理也能被发现」是既有承诺
        const fid = d.folder_id || 0;
        return fid > 0
          ? `<span style="cursor:pointer;color:var(--blue-d);" onclick="docSelectFolder(${fid})" title="点击查看该目录（右键可用「移动到…」改变归属）">📁 ${esc(docFolderName(fid) || ('#'+fid))}</span>`
          : `<span style="color:var(--mut);cursor:pointer;" onclick="docSelectFolder(0)" title="点击查看全部未归类文档">📥 未归类</span>`;
      })()}</td>
      <td>${d.version||'-'}</td>
      <td><b>${d.chunk_count||0}</b></td>

      ${_fileExtractEnabled?'<td>'+extCell+'</td>':''}
      <td style="vertical-align:top;">
        <div class="lc-cell">${docStateBadge(d)}
        ${d.parse_status==='failed'
          // 明细收成一行：失败阶段 + 原因；全文进 title，悬停可见（用 escA，esc 不转引号会截断属性）
          ? (() => {
              const _why = `失败于${failStage(d.pipeline_detail)}：${d.error_msg||'处理失败'}`;
              return `<div class="lc-note err" title="${escA(_why)}">✗ ${esc(_why)}</div>`;
            })()
          : (docDerivedState(d)!=='committed' ? '' : (() => {
              const _st = stageText(d.pipeline_detail);
              return _st ? `<div class="lc-note mut" title="${escA(_st)}">${esc(_st)}</div>` : '';
            })())}</div>
      </td>
      <td style="white-space:nowrap;">${lc === 'stored' ? `<button class="btn sm" onclick="commitDoc(${d.id})" title="人工确认：内容已进入图库，正式入库">入库</button> ` : ''}<button class="btn sm ghost" onclick="viewDocSource(${d.id})">预览</button>
      <button class="btn sm ghost" onclick="viewDocTrace(${d.id})">追溯</button>
      <button class="btn sm ghost" onclick="editDocMeta(${d.id})">元数据</button>
      <span class="rm-wrap">
        <button class="btn sm ghost" title="更多操作" onclick="event.stopPropagation();toggleRowMenu(this)">⋮</button>
        <div class="row-menu">
          <div class="rm-item" onclick="closeRowMenus();docAddToChat(${d.id})">加入会话</div>
          <div class="rm-item" onclick="closeRowMenus();downloadDoc(${d.id})">下载</div>
          <div class="rm-item" onclick="closeRowMenus();docMovePrompt(${d.id})" title="移动到某个结构目录（不改文件名、不重算向量）">移动到…</div>
          <div style="border-top:1px solid var(--line);margin:2px 0;"></div>
          ${lc === 'stored' ? `<div class="rm-item" onclick="closeRowMenus();commitDoc(${d.id})" style="color:var(--blue-d);font-weight:600;" title="人工确认：内容已进入图库，正式入库">正式入库</div>` : ''}
          ${d.parse_status==='completed' && _fileExtractEnabled?`<div class="rm-item" onclick="closeRowMenus();extractDoc(${d.id})">抽取</div>`:''}
          ${d.parse_status==='failed'?`<div class="rm-item" onclick="closeRowMenus();retryDoc(${d.id})">重新解析</div>`:`<div class="rm-item" onclick="closeRowMenus();reindexDoc(${d.id})">重索引</div>`}
          <div style="border-top:1px solid var(--line);margin:2px 0;"></div>
          ${isDep
            ? `<div class="rm-item" onclick="closeRowMenus();restoreDoc(${d.id})">撤销废弃</div>`
            : (lc !== 'archived' ? `<div class="rm-item" onclick="closeRowMenus();deprecateDoc(${d.id})" style="color:var(--red);">废弃</div>` : '')}
          ${lc !== 'archived' && !isDep
            ? `<div class="rm-item" onclick="closeRowMenus();archiveDoc(${d.id})">归档</div>` : ''}
          <div class="rm-item red" onclick="closeRowMenus();deleteDoc(${d.id})">删除</div>
        </div>
      </span></td>
    </tr>`;
    }).join('') + '</table></div>';
  if(pagerEl) renderPagerBar({
    el: pagerEl, total, page: _docPage, size: _docSize,
    onPage: p => { _docPage = p; renderDocs(); },
    onSize: s => { _docSize = s; _docPage = 1; renderDocs(); }
  });
}

// ── P0：文档生命周期操作（前端按钮 → 调用 R1 后端 API）──
// 2026-09-10 米爸澄清：入库（committed）= 内容已进图库，需人工确认切换（解析完成只到「待入库」）
async function commitDoc(id) {
  if(!(await confirmDialog('确认将该文档正式入库？（表示内容已进入图库，可被建模/检索正式引用）'))) return;
  try {
    const r = await api(`/api/documents/${id}/commit`, {method:'POST', body:JSON.stringify({})});
    if(r.error) { toast('入库失败：'+r.error); return; }
    toast('🎯 已正式入库');
    _docSelected.delete(id);
    loadDocs();
  } catch(e) { toast('入库失败：'+e.message); }
}

async function batchCommitSelected() {
  const ids = [..._docSelected];
  if(!ids.length) { toast('请先勾选文档'); return; }
  if(!(await confirmDialog(`批量正式入库 ${ids.length} 个文档？（仅对待入库文档生效）`))) return;
  try {
    const r = await api('/api/documents/batch-commit', {method:'POST', body:JSON.stringify({ids})});
    if(r.error) { toast('批量入库失败：'+r.error); return; }
    toast(`🎯 入库 ${r.ok||0} 个${r.skipped?` · 跳过 ${r.skipped}`:''}${r.failed&&r.failed.length?` · 失败 ${r.failed.length}`:''}`);
    _docSelected.clear();
    loadDocs();
  } catch(e) { toast('批量入库失败：'+e.message); }
}

async function deprecateDoc(id) {
  const reason = prompt('请输入废弃理由（必填，审计要求）：', '资料版本过时');
  if(reason === null) return;
  if(!reason.trim()) { toast('❌ 废弃理由不能为空'); return; }
  try {
    const r = await api(`/api/documents/${id}/deprecate`, {method:'POST', body:JSON.stringify({reason})});
    if(r.error) { toast('废弃失败：'+r.error); return; }
    toast('🚫 已废弃');
    _docSelected.delete(id);
    loadDocs();
  } catch(e) { toast('废弃失败：'+e.message); }
}

async function restoreDoc(id) {
  if(!(await confirmDialog('撤销该文档的废弃标记？（24h 后需管理员强制）'))) return;
  try {
    const r = await api(`/api/documents/${id}/restore`, {method:'POST', body:JSON.stringify({force:true})});
    if(r.error) { toast('恢复失败：'+r.error); return; }
    toast('↩️ 已恢复');
    _docSelected.delete(id);
    loadDocs();
  } catch(e) { toast('恢复失败：'+e.message); }
}

async function archiveDoc(id) {
  if(!(await confirmDialog('归档该文档？归档后进入只读分区（紧急恢复用）。'))) return;
  try {
    const r = await api(`/api/documents/${id}/archive`, {method:'POST', body:JSON.stringify({reason:'归档'})});
    if(r.error) { toast('归档失败：'+r.error); return; }
    toast('📦 已归档');
    _docSelected.delete(id);
    loadDocs();
  } catch(e) { toast('归档失败：'+e.message); }
}

function toggleDocSelect(id, checked) {
  if(checked) _docSelected.add(id); else _docSelected.delete(id);
  loadDocs();  // 刷新批量条显隐
}

function toggleSelectAll(cb, ids) {
  if(cb.checked) ids.forEach(id => _docSelected.add(id));
  else ids.forEach(id => _docSelected.delete(id));
  loadDocs();
}

function clearDocSelection() {
  _docSelected.clear();
  loadDocs();
}

async function batchDeprecateSelected() {
  if(_docSelected.size === 0) { toast('请先选中要废弃的文档'); return; }
  const reason = prompt(`确认批量废弃 ${_docSelected.size} 个文档？\n请输入废弃理由：`, '批量废弃（资料过时）');
  if(reason === null || !reason.trim()) return;
  try {
    const r = await api('/api/documents/batch-deprecate', {
      method:'POST', body:JSON.stringify({ids: [..._docSelected], reason})
    });
    const ok = r.ok || 0, skipped = r.skipped || 0, failed = (r.failed||[]).length;
    toast(`🚫 批量废弃完成：成功 ${ok} · 跳过 ${skipped} · 失败 ${failed}`);
    _docSelected.clear();
    loadDocs();
  } catch(e) { toast('批量废弃失败：'+e.message); }
}

function setDocStateFilter(v) {
  _docStateFilter = v || '';
  loadDocs();
}
function toggleRowMenu(btn) {
  const menu = btn.closest('.rm-wrap').querySelector('.row-menu');
  closeRowMenus(menu);
  if(menu.style.display === 'block') { menu.style.display = 'none'; return; }
  menu.style.display = 'block'; // 先显示再测量宽度（display:none 时 offsetWidth 为 0）
  const r = btn.getBoundingClientRect();
  menu.style.left = Math.max(8, r.right - menu.offsetWidth) + 'px';
  menu.style.top = (r.bottom + 4) + 'px';
}
function closeRowMenus(except) {
  document.querySelectorAll('.row-menu').forEach(m => { if(m !== except) m.style.display = 'none'; });
}
document.addEventListener('click', e => { if(!e.target.closest('.rm-wrap')) closeRowMenus(); });
document.addEventListener('scroll', () => closeRowMenus(), true);
// 手动补抽：对已有文档触发候选抽取（上传失败/未触发时用）
async function extractDoc(id) {
  try {
    const r = await api(`/api/documents/${id}/extract`, {method:'POST'});
    if(r.error) { toast('抽取失败：'+r.error); return; }
    const totalC = (r.node_count||0) + (r.edge_count||0);
    toast(`✅ 已抽取 ${r.candidates||0} 条候选（${totalC} 实体/关系）`);
    const vr = await api('/api/knowledge/v2g/candidates?batch_id=' + encodeURIComponent(r.batch_id)).catch(()=>null);
    const cands = (vr||[]).filter(c=>c.batch_id===r.batch_id);
    window._lastV2GBatch = r.batch_id;
    renderExtractResult(id, {
      batch_id: r.batch_id,
      node_count: r.node_count, edge_count: r.edge_count,
      candidates: (cands||[]).slice(0,12).map(c=>({name:c.entity_name, entity_type:c.entity_type})),
      rejected: []
    }, totalC, id);
    loadDocs();
  } catch(e) { toast('抽取失败：'+e.message); }
}
async function reindexDoc(id) {
  if(!(await confirmDialog('重新向量化该文档全部块？（Embedding 模型升级后使用）'))) return;
  try {
    const r = await api(`/api/documents/${id}/reindex`, {method:'POST'});
    if(r.error) { toast('失败：'+r.error); return; }
    toast(`✅ 重索引完成：${r.chunk_count} 块 / ${r.embed_version}`);
    loadDocs();
  } catch(e) { toast('失败：'+e.message); }
}
// ── 状态语义：管道阶段明细（解析/切片/向量化/入库/抽取候选）──
const STAGE_NAMES = {parse:'解析', chunk:'切片', embed:'向量化', insert:'入库', extraction:'抽取'};
function stageText(detail) {
  if(!detail) return '';
  let d = {};
  try { d = JSON.parse(detail||'{}')||{}; } catch(e) {}
  const parts = [];
  const _kkeys = _fileExtractEnabled ? ['parse','chunk','embed','insert','extraction'] : ['parse','chunk','embed','insert'];
  for(const k of _kkeys) {
    const v = d[k];
    if(v==='done') parts.push(`${STAGE_NAMES[k]}✓`);
    else if(v==='failed') parts.push(`${STAGE_NAMES[k]}✗`);
    else if(v==='disabled') parts.push(`${STAGE_NAMES[k]}—`);  // 文件抽取开关关闭：跳过
    else parts.push(`${STAGE_NAMES[k]}…`);
  }
  return parts.join(' ');
}
function failStage(detail) {
  let d = {};
  try { d = JSON.parse(detail||'{}')||{}; } catch(e) {}
  for(const k of ['parse','chunk','embed','insert']) {
    if(d[k]==='failed') return STAGE_NAMES[k]||k;
  }
  return '未知阶段';
}
function stageTable(detail) {
  let d = {};
  try { d = JSON.parse(detail||'{}')||{}; } catch(e) {}
  const rows = [];
  for(const k of ['parse','chunk','embed','insert']) {
    const v = d[k];
    const icon = v==='done' ? '<span class="st ok">✓ 完成</span>' : (v==='failed' ? '<span class="st r">✗ 失败</span>' : '<span class="st w">…</span>');
    rows.push(`<tr><td>${STAGE_NAMES[k]||k}</td><td>${icon}</td></tr>`);
  }
  return `<table class="t" style="margin-top:4px;"><tr><th>管道阶段</th><th>状态</th></tr>${rows.join('')}</table>`;
}
async function retryDoc(id) {
  if(!(await confirmDialog('使用已保存的源文件副本重新执行解析→切片→向量化→入库？'))) return;
  toast('重试中…');
  try {
    const r = await api(`/api/documents/${id}/retry`, {method:'POST'});
    if(r.error) { toast('❌ 重试失败：'+r.error); loadDocs(); return; }
    toast(`✅ 重试成功：${r.chunk_count} 块 / ${r.embed_version}`);
    loadDocs();
  } catch(e) { toast('重试失败：'+e.message); loadDocs(); }
}
/* 2026-09-18 预览统一：改由 38-filepreview.js 的 openFilePreview 统一实现。
   原实现只渲染 /source 的「文本抽取」结果，对两类真实文件失效 ——
   PDF（文本抽取依赖可选依赖 pdfplumber，本机未安装 → 返回空串）、
   图片（只返回"需 OCR"占位提示串）。现为原件直出（/api/documents/{id}/raw）
   + 文本视图双通道，原件缺失时自动降级文本视图。
   保留本函数名，兼容既有调用点（列表「预览」按钮、追溯面板「查看完整文档」）。 */
async function viewDocSource(id) { return openFilePreview({ doc_id: id }); }
async function viewDocTrace(id) {
  const d = await api(`/api/documents/${id}`);
  const chunks = (d.chunks||[]).map(c=>`
    <div style="border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:6px;font-size:12px;">
      <small style="color:var(--mut);">chunk#${c.chunk_index} ${c.section?'· '+esc(c.section):''} · ${c.embed_version}</small>
      <div style="margin-top:3px;">${esc(c.content)}</div>
    </div>`).join('') || '<div style="color:var(--mut);font-size:12px;">无分块</div>';
  const ents = (d.linked_entities||[]).map(e=>`<span class="tag" onclick="viewEntity('${e.id}')" style="cursor:pointer;">${esc(e.name)}(${e.entity_type})</span>`).join(' ') || '-';
  openPanel(`📚 文档追溯：${d.filename}`, `
    <div class="kv"><span>标题</span><b>${esc(d.title||'-')}</b></div>
    <div class="kv"><span>作者 / 版本</span><b>${esc(d.author||'-')} / ${d.version}</b></div>
    <div class="kv"><span>上传人 / 时间</span><b>${esc(d.uploaded_by||'-')} / ${esc((d.created_at||'').slice(0,16))}</b></div>
    <div class="kv"><span>状态</span><b><span class="st ${d.parse_status==='completed'?'ok':d.parse_status==='failed'?'r':'w'}">${d.parse_status==='completed'?'已完成':d.parse_status==='failed'?'失败':'解析中'}</span></b></div>
    ${d.parse_status==='failed'&&d.error_msg?`<div style="font-size:11px;color:var(--red);margin-top:4px;">✗ 失败原因：${esc(d.error_msg)}</div>`:''}
    <div style="margin-top:8px;border-top:1px dashed var(--line);padding-top:8px;font-size:12px;color:var(--mut);">管道阶段明细：</div>
    ${stageTable(d.pipeline_detail)}
    <div class="kv"><span>块数 / 向量</span><b>${d.chunk_count||0} / ${d.embed_version}</b></div>
    <div style="margin-top:8px;"><button class="btn sm ghost" onclick="viewDocSource(${id})">📖 查看完整文档</button></div>
    <div style="margin-top:10px;border-top:1px dashed var(--line);padding-top:10px;font-size:12px;color:var(--mut);">关联实体（图谱追溯）：</div>
    <div style="margin-top:6px;">${ents}</div>
    <div style="margin-top:12px;border-top:1px dashed var(--line);padding-top:10px;font-size:12px;color:var(--mut);">分块列表（${(d.chunks||[]).length}）：</div>
    <div style="margin-top:6px;">${chunks}</div>`);
}
// ── 元数据编辑：正式表单（上传人/时间只读，对齐主流工具）──
let docMetaId = null;
function editDocMeta(id) {
  docMetaId = id;
  // 先打开表单（DOM 存在），再异步填充数据
  showModal('docmeta');
  api(`/api/documents/${id}`).then(d=>{
    const tags = (JSON.parse(d.tags||'[]')||[]).join(', ');
    document.getElementById('dm2-title').value = d.title || d.filename || '';
    document.getElementById('dm2-author').value = d.author || '';
    document.getElementById('dm2-version').value = d.version || 'v1.0';
    document.getElementById('dm2-tags').value = tags;
    document.getElementById('dm2-desc').value = (d.extra ? JSON.parse(d.extra||'{}').desc : '') || '';
    document.getElementById('dm2-uploader').textContent = `${d.uploaded_by||'-'} · ${(d.created_at||'').slice(0,16)}`;
    document.getElementById('dm2-file').textContent = d.filename || '-';
  });
}
function saveDocMeta() {
  if(!docMetaId) return;
  const extra = {desc: document.getElementById('dm2-desc').value.trim()};
  api(`/api/documents/${docMetaId}/meta`, {method:'PUT', body:JSON.stringify({
    title: document.getElementById('dm2-title').value.trim(),
    author: document.getElementById('dm2-author').value.trim(),
    version: document.getElementById('dm2-version').value.trim() || 'v1.0',
    tags: document.getElementById('dm2-tags').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),
    extra,
  })}).then(()=>{
    toast('✅ 元数据已更新');
    closeModal(); docMetaId = null; loadDocs();
  });
}
async function deleteDoc(id) {
  if(!(await confirmDialog('确认删除该文档？分块将级联清理。\n（原件副本也会一并清理——2026-09-21 起删除路径不再留孤儿副本）'))) return;
  await api(`/api/documents/${id}`, {method:'DELETE'});
  _docTreeStale = true;   // 总数与目录计数都变了
  toast('已删除'); loadDocs();
}
// 选中的本体类型（驱动中/右栏联动）
let ontSelected = null;
let ontData = {types:[], binding:[]};
// 本体 Graph 全屏查看：fixed 覆盖 + 画布重排（交互保留）

/* ═════════ 外部数据源（P0-4 2026-09-20）：注册 db/api/file 三类源 → 测试 → 预览 → 抽取 ═════════ */
const DS_CONFIG_TEMPLATES = {
  db: '{\n  "connection": "sqlite:///D:/path/企业数据库.db",\n  "table": "parts",\n  "limit": 200\n}',
  api: '{\n  "url": "https://host/api/items",\n  "token_env": "MY_API_TOKEN",\n  "data_key": "items",\n  "limit": 200\n}',
  file: '{\n  "path": "data/uploads/企业文档.md"\n}'
};
const DS_TYPE_LABEL = { db: '数据库', api: '接口', file: '文件' };

function dsConfigHint() {
  const t = document.getElementById('ds-type').value;
  document.getElementById('ds-config').value = DS_CONFIG_TEMPLATES[t] || '{}';
  document.getElementById('ds-config-hint').textContent = t === 'db'
    ? 'connection 当前支持 sqlite:/// 绝对路径（强制只读）；table 或 sql 二选一。pg/mysql 驱动属 P1 批次。'
    : t === 'api' ? 'url 需 http(s)://；token_env 填环境变量名（Bearer 方式注入）；data_key 为响应中数组字段名（可省略）。'
    : 'path 为服务器本地文件绝对路径，整篇作为文档入库。';
}

/* 外部数据源右侧滑窗（P0-4 2026-09-20 迁移）：原内嵌折叠面板改为 openPanel 滑窗承载，
   减少对文档列表页的 DOM 侵入（列表页只留入口按钮）；全部函数 id 不变，CRUD/预览逻辑零改动 */
function openDsDrawer() {
  const html = `
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
    <span style="font-size:11px;color:var(--mut);">抽取产物：记录型文档进向量管线 + 候选实体/关系进未评审区（需人工确认后正式入图）</span>
    <span style="flex:1"></span>
    <button class="btn sm ghost" onclick="dsToggleForm()" id="ds-form-toggle">➕ 注册数据源</button>
    <button class="btn sm ghost" onclick="loadDataSources()">刷新</button>
  </div>
  <div id="ds-form" style="display:none;border:1px solid var(--blue);border-radius:8px;padding:10px;margin-bottom:10px;background:var(--blue-l);">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <input id="ds-name" placeholder="数据源名称" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:160px;">
      <select id="ds-type" onchange="dsConfigHint()" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;">
        <option value="db">数据库（db）</option>
        <option value="api">接口（api）</option>
        <option value="file">文件（file）</option>
      </select>
      <span style="flex:1"></span>
      <button class="btn sm" onclick="dsCreate()">注册</button>
    </div>
    <textarea id="ds-config" placeholder='连接配置 JSON（凭据只填环境变量名，不明文落库）' style="width:100%;margin-top:8px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;font-family:monospace;min-height:64px;"></textarea>
    <div id="ds-config-hint" style="font-size:11px;color:var(--mut);margin-top:4px;"></div>
    <div style="margin-top:8px;border-top:1px dashed var(--line);padding-top:8px;">
      <div style="font-size:11px;color:var(--mut);margin-bottom:4px;">字段映射（预留口子：后端当前仅存储不消费；对接企业库持续同步时启用 —— primary_key 供增量 upsert，field_map 声明 源列→本体类型.属性）：</div>
      <textarea id="ds-mapping" placeholder='{"primary_key": "part_no", "field_map": {"名称": "实体名", "材料": "Part.material"}}' style="width:100%;border:1px dashed var(--line);border-radius:6px;padding:6px 8px;font-size:12px;font-family:monospace;min-height:48px;"></textarea>
    </div>
  </div>
  <div id="ds-list" style="display:flex;flex-direction:column;gap:6px;"><span style="font-size:12px;color:var(--mut);">加载中…</span></div>
  <div id="ds-preview" style="display:none;margin-top:10px;border:1px dashed var(--line);border-radius:8px;padding:8px;max-height:260px;overflow:auto;"></div>`;
  openPanel('🔌 外部数据源', html);
  loadDataSources();
  dsToggleForm();   // 滑窗打开即展开注册表单（滑窗内空间纵向充裕，少一次点击）
}

function dsToggleForm() {
  const f = document.getElementById('ds-form');
  if (!f) return;   // 滑窗未打开时静默
  const show = f.style.display === 'none';
  f.style.display = show ? '' : 'none';
  document.getElementById('ds-form-toggle').textContent = show ? '✖ 收起表单' : '➕ 注册数据源';
  if (show && !document.getElementById('ds-config').value) dsConfigHint();
}

async function loadDataSources() {
  const el = document.getElementById('ds-list');
  try {
    const r = await api('/api/knowledge/data-sources');
    const items = r.items || [];
    if (!items.length) { el.innerHTML = '<span style="font-size:12px;color:var(--mut);">暂无数据源，点「注册数据源」接入数据库 / 接口 / 文件。</span>'; return; }
    el.innerHTML = items.map(d => {
      const status = esc(d.last_status || '未测试');
      let cfg = d.config || {};
      if (typeof cfg === 'string') { try { cfg = JSON.parse(cfg); } catch (e) { cfg = {}; } }
      const hasMap = cfg.mapping && typeof cfg.mapping === 'object';
      return `
      <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;display:flex;gap:8px;align-items:center;background:#fff;flex-wrap:wrap;">
        <span class="tag">${DS_TYPE_LABEL[d.type] || d.type}</span>
        <b style="font-size:12px;">${esc(d.name)}</b>
        ${hasMap ? '<span class="tag" title="已配置字段映射（预留口子，暂不消费）">映射</span>' : ''}
        <span style="font-size:11px;color:${d.enabled ? 'var(--ok,green)' : 'var(--mut)'};">${d.enabled ? '● 已启用' : '○ 已停用'}</span>
        <span style="font-size:11px;color:var(--mut);flex:1;min-width:160px;" title="${status}">${status}</span>
        <button class="btn sm ghost" onclick="dsAction('test',${d.id})">测试</button>
        <button class="btn sm ghost" onclick="dsAction('preview',${d.id})">预览</button>
        <button class="btn sm" onclick="dsAction('ingest',${d.id})" ${d.enabled ? '' : 'disabled'}>抽取入库</button>
        <button class="btn sm red" onclick="dsDelete(${d.id},'${esc(d.name)}')">删除</button>
      </div>`;
    }).join('');
  } catch (e) { el.innerHTML = `<span style="font-size:12px;color:var(--red,red);">加载失败：${esc(e.message)}</span>`; }
}

async function dsCreate() {
  const name = document.getElementById('ds-name').value.trim();
  const type = document.getElementById('ds-type').value;
  let config = {};
  try { config = JSON.parse(document.getElementById('ds-config').value || '{}'); }
  catch (e) { toast('❌ 配置不是合法 JSON：' + e.message); return; }
  // 字段映射（预留口子）：非空则校验 JSON 后并入 config.mapping（后端暂只存储不消费）
  const mapRaw = ((document.getElementById('ds-mapping') || {}).value || '').trim();
  if (mapRaw) {
    try { config.mapping = JSON.parse(mapRaw); }
    catch (e) { toast('❌ 字段映射不是合法 JSON：' + e.message); return; }
  }
  try {
    await api('/api/knowledge/data-sources', { method: 'POST', body: JSON.stringify({ name, type, config, enabled: 1 }) });
    toast('✅ 数据源已注册：' + name);
    document.getElementById('ds-name').value = '';
    loadDataSources();
  } catch (e) { toast('❌ 注册失败：' + e.message); }
}

async function dsAction(action, id) {
  try {
    const r = await api(`/api/knowledge/data-sources/${id}/${action}`, { method: 'POST' });
    if (action === 'test') {
      toast((r.ok ? '✅ 连通成功：' : '❌ 连通失败：') + r.message);
      loadDataSources();
    } else if (action === 'preview') {
      if (!r.ok) { toast('❌ 预览失败：' + (r.error || '未知错误')); return; }
      renderDsPreview(r);
    } else if (action === 'ingest') {
      toast(`✅ 抽取完成：文档 #${r.doc_id}（${r.chunk_count} 块）→ 候选 ${r.node_count + r.edge_count} 条（batch ${r.batch_id}），请到图谱工作区·审核确认入图`);
      loadDataSources();
      if (typeof loadDocs === 'function') loadDocs();
    }
  } catch (e) { toast(`❌ ${action} 失败：${e.message}`); }
}

function renderDsPreview(r) {
  const el = document.getElementById('ds-preview');
  el.style.display = '';
  if (r.kind === 'text') {
    el.innerHTML = `<b style="font-size:12px;">文件内容头部预览</b><pre style="font-size:11px;white-space:pre-wrap;margin:6px 0 0;">${esc(r.sample)}</pre>`;
    return;
  }
  const cols = r.columns || [];
  const head = `<tr>${cols.map(c => `<th style="text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);font-size:11px;">${esc(c)}</th>`).join('')}</tr>`;
  const rows = (r.rows || []).map(row => `<tr>${cols.map(c => `<td style="padding:3px 8px;border-bottom:1px dashed var(--line);font-size:11px;">${esc(String(row[c] ?? ''))}</td>`).join('')}</tr>`).join('');
  el.innerHTML = `<b style="font-size:12px;">记录预览（${r.count} 条）</b>
    <div style="font-size:11px;color:var(--mut);margin:4px 0;">${esc(r.note || '')} —— 确认无误后点「抽取入库」。</div>
    <table style="border-collapse:collapse;width:100%;">${head}${rows}</table>`;
}

async function dsDelete(id, name) {
  if (!confirm(`删除数据源「${name}」？已入库的文档与候选不受影响。`)) return;
  try {
    await api(`/api/knowledge/data-sources/${id}`, { method: 'DELETE' });
    toast('🗑 已删除：' + name);
    loadDataSources();
  } catch (e) { toast('❌ 删除失败：' + e.message); }
}
