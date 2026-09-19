/* ══════════════════════════════════════════════════════════════════════════
   统一源文件预览（文档库 / AI 建模附件 / 产物共用）
   2026-09-18 需求：无论文档库上传，还是 AI 建模环节上传附件，都要能预览源文件。

   为什么单独抽一层：预览有 4 条真实来源，此前各写各的，且会话附件点击是「下载」而非预览：
     ① 文档库文档 —— 原件在 data/uploads/，不在 /static 挂载点下，无静态 URL
        → 必须走 /api/documents/{id}/raw
     ② 会话附件已入库（有 doc_id）→ 同上
     ③ 会话附件图片 / 入库失败的附件（原件在 static/uploads/{uuid}{ext}）→ 直接用该 url
     ④ 文档原件副本缺失 → 降级 /source 文本视图（由分块拼接），不报死

   渲染策略（零新依赖，遵守 AGENTS.md 铁律 4：不引 pdf.js / mammoth）：
     inline 类（PDF / 图片 / 音视频）→ <iframe>/<img>/<video> 交给浏览器原生渲染
     text 类（txt/md/csv/json…）    → 默认文本视图，可切原件视图
     office 类（docx/xls/pptx）     → 浏览器无法内嵌 → 文本视图 + 明确提示 + 下载

   注意：/source 的文本抽取对 PDF 依赖可选依赖 pdfplumber（未安装时返回空串）、
        对图片只返回占位提示串，故它只能作降级通道，不能作唯一预览通道。
   ══════════════════════════════════════════════════════════════════════════ */

const PV_INLINE_EXTS = ['.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg',
                        '.mp4', '.webm', '.mp3', '.wav', '.ogg'];
const PV_IMAGE_EXTS  = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'];
const PV_VIDEO_EXTS  = ['.mp4', '.webm', '.ogg'];
const PV_AUDIO_EXTS  = ['.mp3', '.wav', '.ogg'];
const PV_TEXT_EXTS   = ['.txt', '.md', '.markdown', '.csv', '.log', '.json', '.xml',
                        '.yaml', '.yml', '.sysml', '.kerml'];

let _pvState = null;   // { docId, filename, url, meta, text, textLoaded, kind, tab, loadErr }

function pvExt(name){
  const m = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
  return m ? '.' + m[1] : '';
}
/* 后端 preview.kind 优先（它知道原件副本是否在盘）；无 doc_id 时按扩展名推断 */
function pvKindOf(ext, meta){
  if(meta && meta.kind) return meta.kind;
  if(PV_INLINE_EXTS.indexOf(ext) >= 0) return 'inline';
  if(PV_TEXT_EXTS.indexOf(ext) >= 0) return 'text';
  return ext ? 'office' : 'unknown';
}
/* 仅接受站内相对路径，杜绝 javascript:/data: 注入 iframe/img */
function pvSafeUrl(u){
  const s = String(u || '');
  return /^\/[^/]/.test(s) ? s : '';
}
function pvRawUrl(st){
  if(!st) return '';
  if(st.docId) return '/api/documents/' + st.docId + '/raw';
  return pvSafeUrl(st.url);
}
function pvDownloadUrl(st){
  if(!st) return '';
  if(st.docId) return '/api/documents/' + st.docId + '/raw?download=1';
  return pvSafeUrl(st.url);
}
function pvKindLabel(st){
  if(!st) return '-';
  const ext = pvExt(st.filename) || (st.meta && st.meta.ext) || '';
  if(st.docId && st.meta && st.meta.has_file === false) return ext + ' · 原件副本缺失，已降级为文本视图';
  if(st.kind === 'inline') return ext + ' · 浏览器原生渲染原件';
  if(st.kind === 'text')   return ext + ' · 文本可直读';
  if(st.kind === 'office') return ext + ' · 二进制格式，浏览器无法内嵌，降级文本视图';
  return ext || '-';
}
function pvNotice(html, tone){
  const border = tone === 'warn' ? 'var(--red,#c53030)' : 'var(--line,#e5e8ee)';
  return '<div style="border:1px dashed ' + border + ';border-radius:8px;padding:12px 14px;'
       + 'font-size:12.5px;line-height:1.8;color:var(--txt,#222);margin-bottom:8px;">' + html + '</div>';
}

/* ── 入口：统一打开源文件预览 ───────────────────────────────────────────
   o = { doc_id?, filename?, url?, tab? }                                    */
async function openFilePreview(o){
  o = o || {};
  const st = {
    docId: o.doc_id ? Number(o.doc_id) : 0,
    filename: o.filename || '未命名文件',
    url: o.url || '',
    meta: null, text: '', textLoaded: false, kind: '', tab: o.tab || '', loadErr: '',
  };
  _pvState = st;

  if(st.docId){
    openPanel('📖 ' + st.filename, pvSkeleton('正在读取源文件…'));
    try{
      const r = await api('/api/documents/' + st.docId + '/source');
      if(r && r.error){ st.loadErr = r.error; }
      else if(r){
        st.text = String(r.content || '');
        st.textLoaded = true;
        st.meta = r.preview || null;
        if(r.filename) st.filename = r.filename;
      }
    }catch(e){ st.loadErr = (e && e.message) || String(e); }
  }

  st.kind = pvKindOf(pvExt(st.filename), st.meta);
  // 默认视图：原件可内嵌就原件，否则文本视图。
  // st.loadErr 非空（文档不存在 / /source 读取失败）时必须落文本视图 ——
  // 否则 iframe 会指向 404 的 /raw，面板一片空白且不告知原因。
  const hasRaw = !st.loadErr && !!pvRawUrl(st) && st.kind !== 'office'
    && !(st.meta && st.meta.has_file === false);
  if(!st.tab) st.tab = hasRaw ? 'raw' : 'text';
  renderPvPanel();
  return st;
}

function pvSkeleton(msg){
  return '<div style="padding:20px 4px;color:var(--mut,#888);font-size:12.5px;">'
       + esc(msg) + '</div>';
}

/* ── 面板渲染 ─────────────────────────────────────────────────────────── */
function pvTabsHtml(st){
  const raw = pvRawUrl(st);
  const mk = (tab, label, on) =>
    '<button class="btn sm' + (on ? '' : ' ghost') + '" onclick="pvSwitch(\'' + tab + '\')">'
    + label + '</button>';
  const rawDisabled = !raw
    ? ' title="无原件地址（该附件未落盘原件）"'
    : ((st.meta && st.meta.has_file === false) ? ' title="原件副本缺失，将自动降级文本视图"' : '');
  return '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:10px 0 8px;">'
    + '<span' + rawDisabled + '>' + mk('raw', '🖼 原件视图', st.tab === 'raw') + '</span>'
    + mk('text', '📝 文本视图', st.tab === 'text')
    + '<span style="flex:1;"></span>'
    + '<button class="btn sm ghost" onclick="pvDownload()" title="另存为原件（原件缺失时后端自动导出抽取文本）">⬇ 下载</button>'
    + '<button class="btn sm ghost" onclick="pvOpenTab()" title="在新标签页打开原件">↗ 新窗口</button>'
    + '</div>';
}

function pvBodyHtml(st){
  const raw = pvRawUrl(st);
  const ext = pvExt(st.filename) || (st.meta && st.meta.ext) || '';

  if(st.tab === 'raw'){
    if(st.loadErr){
      return pvNotice('⚠️ <b>无法预览原件</b><br>' + esc(st.loadErr)
        + '<br>该文档可能已被删除或不在当前资料库中。', 'warn');
    }
    if(!raw){
      return pvNotice('⚠️ <b>没有可用的原件地址</b><br>该附件未落盘原件副本，无法预览原件。已提供文本视图。', 'warn');
    }
    if(st.meta && st.meta.has_file === false){
      return pvNotice('⚠️ <b>原件副本缺失</b><br>该文档入库时未保留原件（或副本已被清理），'
        + '已降级为文本视图（由分块拼接）。', 'warn');
    }
    if(st.kind === 'office'){
      return pvNotice('ℹ️ <b>' + esc(ext) + ' 为 Office 二进制格式</b><br>'
        + '浏览器无法内嵌渲染该格式（本项目约定不引入外部 CDN / 构建链 / 新依赖，故不引 mammoth / SheetJS）。'
        + '<br>请使用下方 <b>文本视图</b>（抽取段落与表格），或点「下载」用本地 Office 打开。')
        + '<div style="margin-top:6px;"><button class="btn sm" onclick="pvSwitch(\'text\')">切到文本视图</button></div>';
    }
    if(PV_IMAGE_EXTS.indexOf(ext) >= 0){
      return '<div style="display:flex;align-items:center;justify-content:center;background:var(--blue-l,#f4f8ff);'
        + 'border-radius:8px;padding:10px;min-height:120px;">'
        + '<img src="' + escA(raw) + '" alt="' + escA(st.filename) + '" '
        + 'style="max-width:100%;max-height:min(68vh,660px);border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,.12);"></div>';
    }
    if(PV_VIDEO_EXTS.indexOf(ext) >= 0){
      return '<video src="' + escA(raw) + '" controls style="width:100%;max-height:min(68vh,660px);'
        + 'background:#000;border-radius:8px;"></video>';
    }
    if(PV_AUDIO_EXTS.indexOf(ext) >= 0){
      return '<audio src="' + escA(raw) + '" controls style="width:100%;"></audio>';
    }
    // PDF 与文本类：交给浏览器原生渲染（PDF 内嵌查看器支持翻页/缩放/搜索）
    const frag = (ext === '.pdf') ? '#zoom=page-width' : '';
    return '<iframe src="' + escA(raw + frag) + '" title="' + escA(st.filename) + '" '
      + 'style="width:100%;height:min(70vh,680px);border:1px solid var(--line,#e5e8ee);'
      + 'border-radius:8px;background:#fff;"></iframe>';
  }

  // ── 文本视图 ──
  if(!st.textLoaded){
    if(st.loadErr) return pvNotice('⚠️ 读取失败：' + esc(st.loadErr), 'warn');
    return pvNotice('ℹ️ 该附件没有对应的入库文档，无法提供文本视图。<br>请使用「原件视图」查看。');
  }
  if(!st.text){
    // 按格式指出真实原因：哪些格式的抽取依赖可选依赖（未装则必然为空），
    // 哪些格式是真无文本层（扫描件 / 纯图 PDF / 图片）—— 避免排障时被误导。
    const _dep = {
      '.pdf': 'pdfplumber', '.ppt': 'python-pptx', '.pptx': 'python-pptx',
      '.doc': 'python-docx', '.docx': 'python-docx',
      '.xls': 'openpyxl', '.xlsx': 'openpyxl',
    }[ext];
    const why = _dep
      ? ('该格式的文本抽取依赖<b>可选依赖 ' + _dep + '</b>，本机未安装，故抽取为空。')
      : '原件中未抽取到文本层（可能是扫描件 / 纯图 PDF / 图片）。';
    return pvNotice('ℹ️ <b>文本视图为空</b><br>' + why
      + '<br>请使用「原件视图」查看原始版式与图表。')
      + (pvRawUrl(st)
          ? '<div style="margin-top:6px;"><button class="btn sm" onclick="pvSwitch(\'raw\')">切到原件视图</button></div>'
          : '');
  }
  return '<div style="font-size:12px;color:var(--mut,#888);margin-bottom:6px;">'
    + '抽取全文 ' + st.text.length.toLocaleString() + ' 字符'
    + (st.meta && st.meta.has_file === false ? '（由分块拼接）' : '')
    + '</div>'
    + '<div style="background:var(--blue-l,#f4f8ff);border-radius:8px;padding:12px;font-size:12.5px;'
    + 'white-space:pre-wrap;word-break:break-word;line-height:1.75;max-height:min(70vh,680px);overflow:auto;">'
    + esc(st.text) + '</div>';
}

function renderPvPanel(){
  const st = _pvState; if(!st) return;
  const body = document.getElementById('panel-body');
  if(!body) return;
  const head = '<div class="kv"><span>文件</span><b>' + esc(st.filename) + '</b></div>'
    + '<div class="kv"><span>预览方式</span><b>' + esc(pvKindLabel(st)) + '</b></div>';
  document.getElementById('panel-title').textContent = '📖 源文件预览：' + st.filename;
  body.innerHTML = head + pvTabsHtml(st) + pvBodyHtml(st);
}

function pvSwitch(tab){
  if(!_pvState) return;
  _pvState.tab = tab;
  renderPvPanel();
}
function pvDownload(){
  const st = _pvState; if(!st) return;
  const u = pvDownloadUrl(st);
  if(!u){ toast('无可下载的原件'); return; }
  const a = document.createElement('a');
  a.href = u;
  a.download = st.filename || '';
  document.body.appendChild(a); a.click(); a.remove();
  toast('开始下载：' + st.filename + '（原件副本缺失时后端自动导出抽取文本）');
}
function pvOpenTab(){
  const st = _pvState; if(!st) return;
  const u = pvRawUrl(st);
  if(!u){ toast('无原件地址'); return; }
  window.open(u, '_blank');
}

/* ── 委托入口：由 data-pv-* 属性触发（避免把参数拼进 onclick 引号里）── */
function pvOpenFromEl(el){
  if(!el || !el.dataset) return;
  openFilePreview({
    doc_id: el.dataset.pvDoc ? Number(el.dataset.pvDoc) : 0,
    filename: el.dataset.pvName || '',
    url: el.dataset.pvUrl || '',
  });
}
/* 供渲染侧复用的 data-* 片段生成器 */
function pvDataAttrs(o){
  o = o || {};
  let s = ' data-pv-name="' + escA(o.filename || '') + '"';
  if(o.doc_id) s += ' data-pv-doc="' + escA(o.doc_id) + '"';
  if(o.url)    s += ' data-pv-url="' + escA(o.url) + '"';
  return s;
}
