/* 术语：概念 / 词典 / 映射 / 导入导出
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 8176-8700  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 * 2026-09-09 三轮反馈重构：左树右详情布局（树形=上位概念层级），去掉预览抽屉与
 * 平铺/层级树切换——点击树节点直接进统一编辑页（分区块+全宽字段行，对齐本体编辑页风格）；
 * AI 建议采纳复用同一编辑页（adopt 模式，AI 预填可改）。
 */
function _cptBadge(st){
  const s = _CPT_STATUS[st] || [st,'#718096','#EDF0F2'];
  return `<span style="font-size:10px;padding:1px 7px;border-radius:8px;background:${s[2]};color:${s[1]};font-weight:600;">${s[0]}</span>`;
}
const _CPT_DOT = {candidate:'#B7791F', approved:'#2F855A', deprecated:'#C53030', retired:'#718096'};

// ── 主加载：工具栏下方 = [批量条] + [左树 | 右详情] ──
async function conceptsLoad(){
  const el = document.getElementById('cpts-list');
  if(!el) return;
  try{
    const kw = encodeURIComponent((document.getElementById('cpts-kw')||{}).value||'');
    const st = (document.getElementById('cpts-status')||{}).value||'';
    const r = await api(`/api/glossary/concepts?keyword=${kw}${st?`&status=${st}`:''}`);
    const items = r.items||[];
    window._cptItems = items;
    // 缓存域清单/待审定计数/概念选项（批量模态与采纳向导下拉数据源），同步待审定按钮态
    window._cptDomains = r.domains || [];
    window._cptPending = r.pending || 0;
    conceptOptions('').then(opts => { window._cptOptions = opts; }).catch(()=>{});
    // 待审定入口已收敛到状态筛选：候选项动态带计数（⏳按钮 2026-09-09 七轮反馈移除）
    const optC = document.getElementById('opt-candidate');
    if(optC) optC.textContent = '候选' + (window._cptPending ? `（${window._cptPending}）` : '');
    // 首次：搭骨架（批量条 + 左树/右详情 split）；后续只就地更新（不重建，保护未保存表单）
    if(!document.getElementById('cpt-tree')){
      el.innerHTML = `<div id="cpt-batch-bar" style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:0 0 8px;padding:6px 10px;background:#f0f6ff;border:1px solid #B5D4F4;border-radius:8px;font-size:11.5px;display:none;">
        <span>已选 <b style="color:#1d4ed8;">0</b> 个概念</span>
        <span style="color:var(--line);">|</span>
        <label style="display:flex;align-items:center;gap:3px;cursor:pointer;"><input type="checkbox" onchange="cptSelAll(this.checked)" style="cursor:pointer;">全选</label>
        <span style="color:var(--line);">|</span>
        <button class="btn sm" style="padding:2px 10px;font-size:11px;" onclick="cptBatch('approve')" title="批量批准（缺定义自动跳过并提示）">✓ 批量批准</button>
        <button class="btn sm ghost" style="padding:2px 10px;font-size:11px;" onclick="cptBatchFlow()" title="批量状态流转（弃用需替代概念）">⤵ 批量流转</button>
        <button class="btn sm ghost" style="padding:2px 10px;font-size:11px;" onclick="cptBatchDomain()" title="批量修改域">🏷 批量改域</button>
        <button class="btn sm ghost" style="padding:2px 10px;font-size:11px;color:var(--mut);" onclick="cptBatchClear()">⏮ 清除选择</button>
        <span style="flex:1"></span>
        <span style="font-size:10.5px;color:var(--mut);">拦截项（缺定义/弃用无替代）自动跳过并列出原因</span>
      </div>
      <div style="display:flex;gap:10px;align-items:stretch;">
        <div id="cpt-tree" style="width:300px;flex:none;border:1px solid var(--line);border-radius:8px;background:#fff;max-height:calc(100vh - 250px);display:flex;flex-direction:column;">
          <div style="display:flex;align-items:center;gap:6px;padding:7px 10px;border-bottom:1px solid var(--line);flex:none;">
            <b style="font-size:12px;">概念层级</b><span style="flex:1"></span>
            <button class="btn sm" onclick="cptCreateMini()" title="新建概念：输入名称回车创建，左树与右侧同步出现">＋ 新增</button>
          </div>
          <div id="cpt-tree-body" style="padding:6px 4px;overflow:auto;flex:1;min-height:0;">${_cptTreeHtml(items)}</div>
        </div>
        <div id="cpt-detail" style="flex:1;min-width:0;overflow:auto;border:1px solid var(--line);border-radius:8px;background:#fff;padding:14px 18px;max-height:calc(100vh - 250px);"></div>
      </div>`;
    }
    document.getElementById('cpt-tree-body').innerHTML = _cptTreeHtml(items);
    const detail = document.getElementById('cpt-detail');
    const selAlive = window._cptSelNode && items.some(c=>c.concept_id===window._cptSelNode);
    if(selAlive){
      if(!window._cptFormDirty) cptSelect(window._cptSelNode, {keepScroll:true});
    } else if(window._cptMode==='create'){
      /* 新建/采纳表单进行中：不抢占右栏 */
    } else if(items.length){
      // ② 默认选中排序第一的数据（树渲染序 = 首个根节点，zh 排序）
      const children0 = _cptTreeData(items);
      const roots0 = children0['']||[];
      if(roots0.length){
        _cptBatchBarSync();
        cptSelect(roots0[0].concept_id);
        return;
      }
      detail.innerHTML = _cptDetailEmpty(false);
    } else {
      window._cptSelNode = null; window._cptFormDirty = false;
      detail.innerHTML = _cptDetailEmpty(true);
    }
    _cptBatchBarSync();
  }catch(e){ el.innerHTML = `<div class="fempty">加载失败：${esc(String(e))}</div>`; }
}
function _cptDetailEmpty(noData){
  return '<div style="height:320px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--mut);font-size:12px;gap:8px;">'
    + '<div style="font-size:30px;opacity:.4;">🗂</div>'
    + (noData ? '<div>暂无概念——点左树顶部「＋ 新增」建立第一个概念</div>'
              : '<div>左侧选择一个概念，此处直接进入编辑</div>'
               +'<div style="font-size:11px;">树按「上位概念」层级组织 · 节点圆点=状态 · 勾选可批量操作</div>')
    + '</div>';
}
// ── 左树：broader 层级（选中高亮 / 折叠 / 状态圆点 / 缺定义标记 / 批量勾选） ──
function _cptTreeData(items){
  const byId = {}; items.forEach(c=>{ byId[c.concept_id]=c; });
  const keep = new Set(items.map(c=>c.concept_id));
  items.forEach(c=>{ let b=c.broader; const seen=new Set();
    while(b && byId[b] && !seen.has(b)){ seen.add(b); keep.add(b); b=byId[b].broader; } });
  const children = {};
  items.forEach(c=>{ if(!keep.has(c.concept_id)) return;
    const p = (c.broader && byId[c.broader] && keep.has(c.broader)) ? c.broader : '';
    (children[p] = children[p]||[]).push(c); });
  Object.values(children).forEach(a=>a.sort((x,y)=>x.pref_label.localeCompare(y.pref_label,'zh')));
  return children;
}
function _cptTreeHtml(items){
  const children = _cptTreeData(items);
  const sel = window._cptSelNode||'';
  const collapsed = window._cptCollapsed || (window._cptCollapsed = new Set());
  const row = (c, depth)=>{
    const kids = children[c.concept_id]||[];
    const open = !collapsed.has(c.concept_id);
    const caret = kids.length
      ? `<span onclick="event.stopPropagation();cptTreeToggle('${c.concept_id}')" style="cursor:pointer;display:inline-block;width:13px;flex:none;color:var(--mut);font-size:9px;transform:rotate(${open?'0deg':'-90deg'});transition:transform .12s;">▶</span>`
      : '<span style="display:inline-block;width:13px;flex:none;"></span>';
    const on = sel===c.concept_id;
    return `<div class="cpt-tree-row" style="display:flex;align-items:center;gap:3px;padding:4px 6px 4px ${4+depth*16}px;border-radius:6px;cursor:pointer;${on?'background:var(--blue-l);':''}"
        onmouseenter="if('${on}'==='false') this.style.background='#F2F6FB';" onmouseleave="if('${on}'==='false') this.style.background='transparent';">
      <input type="checkbox" ${((window._cptSel||new Set()).has(c.concept_id))?'checked':''} onclick="event.stopPropagation();" onchange="cptSelToggle('${c.concept_id}',this.checked)" style="cursor:pointer;flex:none;" title="加入批量选择">
      ${caret}
      <span onclick="cptSelect('${c.concept_id}')" style="flex:1;min-width:0;display:flex;align-items:center;gap:5px;overflow:hidden;">
        <span style="width:7px;height:7px;border-radius:50%;background:${_CPT_DOT[c.concept_status]||'#999'};flex:none;" title="${c.concept_status}"></span>
        <b style="font-size:12px;${on?'color:var(--blue-d);':''}white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(c.pref_label)}</b>
        ${c.homonym?'<span title="同形异义：该词指向多个概念">🔴</span>':''}
        ${c.definition?'':'<span style="color:#C53030;font-size:9px;flex:none;font-weight:600;" title="缺定义（ISO 704 审批拦截）">缺定</span>'}
        ${kids.length?`<span style="color:var(--mut);font-size:9.5px;flex:none;">${kids.length}</span>`:''}
      </span>
      <span class="cpt-ops" style="display:inline-flex;gap:1px;flex:none;opacity:0;transition:opacity .12s;">
        <a title="新增直接下级（上位概念自动填本节点）" onclick="event.stopPropagation();cptCreateChild('${c.concept_id}')">＋</a>
        <a title="删除概念（有下级/被引用为替代时拦截）" onclick="event.stopPropagation();conceptDelete('${c.concept_id}')">🗑</a>
      </span>
    </div>` + (kids.length && open ? kids.map(k=>row(k,depth+1)).join('') : '');
  };
  const roots = children['']||[];
  return roots.length ? roots.map(c=>row(c,0)).join('')
    : '<div style="padding:16px 10px;color:var(--mut);font-size:11.5px;text-align:center;line-height:1.8;">当前筛选无匹配概念<div style="margin-top:6px;"><button class="btn sm ghost" onclick="cptFilterReset()">清除筛选</button></div></div>';
}
function cptTreeToggle(cid){
  const s = window._cptCollapsed || (window._cptCollapsed = new Set());
  s.has(cid) ? s.delete(cid) : s.add(cid);
  cptTreeRefresh();
}
function cptTreeRefresh(){
  const el = document.getElementById('cpt-tree-body');
  if(el) el.innerHTML = _cptTreeHtml(window._cptItems||[]);
}

// ── 表单基件（对齐本体编辑页风格：分区块标题 + label 左/字段右全宽行 + 行尾提示） ──
function _fsec(title){
  return `<div style="display:flex;align-items:center;gap:8px;margin:16px 0 10px;">
    <span style="width:3px;height:14px;background:var(--blue);border-radius:2px;flex:none;"></span>
    <b style="font-size:12.5px;">${title}</b><span style="flex:1;height:1px;background:var(--line);"></span></div>`;
}
function _frow(label, fieldHtml, hint){
  return `<div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:8px;">
    <label style="width:104px;flex:none;text-align:right;color:var(--mut);font-size:12px;padding-top:6px;">${label}${hint?`<span class="info-tip" title="${escA(hint)}">ⓘ</span>`:''}</label>
    <div style="flex:1;min-width:0;">${fieldHtml}</div>
  </div>`;
}
const _FINP = 'width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:12.5px;font-family:inherit;box-sizing:border-box;';
// 必填标识（红星）与校验失败高亮（红框+聚焦+滚动到视野，输入即清除）
const _req = () => ' <span style="color:#C53030;font-weight:700;" title="必填">*</span>';
// 条件必填标识（琥珀星：仅在特定操作/状态下必填，hover 看条件）
const _reqC = (t) => ' <span style="color:#B7791F;font-weight:700;" title="' + t + '">*</span>';
function _hlRequired(id){
  const el = document.getElementById(id);
  if(!el || ((el.value||'').trim())) return false;
  el.style.borderColor = '#C53030';
  el.style.boxShadow = '0 0 0 3px rgba(197,48,48,.13)';
  try{ el.scrollIntoView({block:'center', behavior:'smooth'}); }catch(e){}
  el.focus();
  el.addEventListener('input', ()=>{ el.style.borderColor=''; el.style.boxShadow=''; }, {once:true});
  return true;
}
function _cptBindDirty(host){
  window._cptFormDirty = false;
  const mark = ()=>{ window._cptFormDirty = true; };
  host.addEventListener('input', mark, {once:false, capture:true});
  host.addEventListener('change', mark, {once:false, capture:true});
}
function _cptFormVals(){
  const v = id => ((document.getElementById(id)||{}).value||'').trim();
  const b = Object.assign({
    pref_label: v('cf-label'),
    definition: (document.getElementById('cf-def')||{}).value||'',
    concept_status: v('cf-status')||'candidate',
    replaced_by: v('cf-rep'),
    english: v('cf-en'),
    intent: v('cf-intent'),
    boost: parseFloat(v('cf-boost')) || 1.0,
    context: v('cf-context'),
    source: v('cf-dsrc'),
    abbr: v('cf-abbr'),
  }, _collectMap('cf'), _collectRel('cf'));
  // 注/域已从编辑页移除：字段缺席时不发送，后端按「缺省不动」保留原值
  if(document.getElementById('cf-domain')) b.domain = v('cf-domain')||'unknown';
  if(document.getElementById('cf-note')) b.note = (document.getElementById('cf-note')||{}).value||'';
  return b;
}

// ── 统一编辑页（原 conceptDetail 预览 + conceptEditModal 编辑 合并；点击树节点直达） ──
async function cptSelect(cid, opt){
  opt = opt||{};
  const tok = (window._cptSelToken = (window._cptSelToken||0) + 1);   // 并发选择：旧渲染作废
  window._cptSelNode = cid;
  window._cptAdopt = null;
  window._cptMode = null;
  cptTreeRefresh();
  const host = document.getElementById('cpt-detail');
  if(!host) return;
  const sc = opt.keepScroll ? host.scrollTop : 0;
  host.innerHTML = '<div class="loading">加载中…</div>';
  const r = await api(`/api/glossary/concepts/${encodeURIComponent(cid)}`);
  if(tok !== window._cptSelToken) return;
  if(r.error){ host.innerHTML = `<div class="fempty">失败：${esc(r.error)}</div>`; return; }
  const c = r.concept, terms = r.terms||[], impact = r.impact||[];
  const cands = await conceptOptions(cid);
  window._cptEditOrigin = {concept_status: c.concept_status};
  const abbrs = terms.filter(t=>t.term_kind==='abbr').map(t=>t.term);
  const next = {candidate:['approved','批准'], approved:['deprecated','弃用'], deprecated:['retired','退役']}[c.concept_status];
  const termRows = terms.map(t=>`<tr style="border-top:1px solid var(--line);">
      <td style="padding:5px 8px;font-weight:${t.term_kind==='preferred'?'600':'400'};">${esc(t.term)}</td>
      <td style="padding:5px 8px;color:var(--mut);">${t.lang==='en'?'EN':'ZH'}</td>
      <td style="padding:5px 8px;">${_TERM_KIND_CN[t.term_kind]||t.term_kind}</td>
      <td style="padding:5px 8px;color:var(--mut);font-size:11px;">${esc(t.source||'')}</td>
      <td style="padding:5px 8px;"><a style="color:#C53030;cursor:pointer;font-size:11px;" onclick="termRemove(${t.id},'${esc(t.term).replace(/'/g,'')}', '${escA(cid)}')">删</a></td>
    </tr>`).join('');
  const mapTxt = c.maps_to_class?'类':c.maps_to_prop?'属性':c.maps_to_inst?'关系':'';
  const head = `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;">
      <b style="font-size:15px;">${esc(c.pref_label)}</b>${_cptBadge(c.concept_status)}
      <span style="color:var(--mut);font-size:11px;">ID <code>${esc(c.concept_id)}</code></span>
      <span style="color:var(--mut);font-size:11px;">v${c.version||1}</span>
      ${c.homonym?`<span style="color:#C53030;font-size:11px;">🔴 同形异义 ×${r.homonym} <a style="cursor:pointer;text-decoration:underline;" onclick="conceptMergeModal('${escA(cid)}')">合并…</a></span>`:''}
      <span style="flex:1"></span>
      <button class="btn sm ghost" onclick="cptSelect('${escA(cid)}')" title="放弃未保存修改，重新加载">↺ 重置</button>
      ${next?`<button class="btn sm" onclick="conceptFlow('${escA(cid)}','${next[0]}')" title="ISO 704 状态流转（弃用需指定替代概念）">${next[1]}</button>`:''}
      <button class="btn sm" onclick="cptSave('${escA(cid)}')" style="min-width:74px;">💾 保存</button>
    </div>`;
  const tabbar = `<div style="display:flex;gap:2px;border-bottom:1px solid var(--line);margin:2px 0 4px;">
      <button id="cpt-dt-info" class="cpt-dtab on" onclick="cptDetailTab('info')">详情</button>
      <button id="cpt-dt-hist" class="cpt-dtab" onclick="cptDetailTab('hist')">变更历史</button>
      <span style="flex:1"></span></div>`;
  const html = head + tabbar + '<div id="cpt-tab-info">'
    + _fsec('基本信息')
    + _frow('规范词'+_req(), `<input id="cf-label" value="${escA(c.pref_label)}" style="${_FINP}">`, '首选术语；改名不动 ID，旧词自动降级为同义词')
    + _frow('英文对照', `<input id="cf-en" value="${escA(c.english||'')}" placeholder="Transponder" style="${_FINP}">`, '跨语言召回（ISO 704 建议）')
    + _frow('缩写', _abbrBoxHtml(), '输入后回车新增 · 点击 × 删除')
    + _fsec('定义与上下文')
    + _frow('定义'+_reqC('提交审批（approved）时必填——ISO 704'), `<textarea id="cf-def" rows="3" placeholder="描述该概念是什么、与相邻概念的区别" style="${_FINP}resize:vertical;">${esc(c.definition||'')}</textarea>`, 'ISO 704：定义为空不能提交审批')
    + _frow('语境 / 示例', `<input id="cf-context" value="${escA(c.context||'')}" placeholder="使用语境或示例句" style="${_FINP}">`, 'SKOS skos:example')
    + _frow('定义来源', `<input id="cf-dsrc" value="${escA(c.source||'')}" placeholder="如 GB/T XXXX / 手册 3.2 节" style="${_FINP}">`, 'ISO 704 要求定义可溯源')
    + _fsec(`术语与同义词（${terms.length}）`)
    + `<div style="display:flex;gap:6px;align-items:flex-start;margin-bottom:6px;">
        <textarea id="new-term-input" rows="1" placeholder="批量录入：分号 / 换行分隔，如 功放; 放大器; PA" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;resize:vertical;"></textarea>
        <select id="new-term-kind" style="border:1px solid var(--line);border-radius:6px;padding:5px;font-size:12px;">
          <option value="synonym">同义</option><option value="abbr">缩写</option><option value="alias">别名</option><option value="hidden">禁用</option>
        </select>
        <select id="new-term-lang" style="border:1px solid var(--line);border-radius:6px;padding:5px;font-size:12px;">
          <option value="">语(自动)</option><option value="zh">中文</option><option value="en">EN</option>
        </select>
        <button class="btn sm" onclick="termBatchAdd('${escA(cid)}')">＋ 批量加</button>
      </div>
      <table class="tbl" style="width:100%;font-size:12px;">
        <thead><tr style="color:var(--mut);text-align:left;"><th style="padding:5px 8px;">术语</th><th style="padding:5px 8px;">语</th><th style="padding:5px 8px;">类型</th><th style="padding:5px 8px;">来源</th><th></th></tr></thead>
        <tbody>${termRows||'<tr><td colspan="5" style="padding:8px;color:var(--mut);">暂无术语</td></tr>'}</tbody>
      </table>`
    + _fsec('关联信息')
    + _frow('上位概念', `<select id="cf-broader" style="${_FINP}"><option value="">— 无（顶级概念）—</option>
        ${cands.map(o=>`<option value="${escA(o.id)}"${c.broader===o.id?' selected':''}>${esc(o.label)}</option>`).join('')}</select>`, 'broader 构成层级（左树结构）')
    + _frow('相关概念', `<select id="cf-related" multiple size="4" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;">
        ${cands.map(o=>`<option value="${escA(o.id)}"${(c.related||'').split(',').filter(Boolean).includes(o.id)?' selected':''}>${esc(o.label)}</option>`).join('')}</select>
        <small style="color:var(--mut);font-size:11px;">Ctrl/Shift 多选，或点开面板搜索</small>`, 'related 表达横向关联')
    + ((c.narrower||[]).length?_frow('下位概念（自动派生）', `<div>${c.narrower.map(n=>`<span class="tag" style="cursor:pointer;" onclick="cptSelect('${escA(n)}')">${esc(n.replace(/^C-/,'')||n)}</span>`).join(' ')}</div>`):'')
    + _frow('关系图', '<div id="cpt-minigraph" style="font-size:11px;color:var(--mut);">📊 生成中…</div>')
    + _fsec('本体映射')
    + _frow('映射对象', _mapRow('cf', {cls:c.maps_to_class, prop:c.maps_to_prop, inst:c.maps_to_inst}), '挂接后术语命中可直连图谱查询')
    + _fsec('治理信息')
    + _frow('状态', `<select id="cf-status" onchange="cptStatusChanged()" style="${_FINP}">
        ${['candidate','approved','deprecated','retired'].map(s=>`<option value="${s}"${c.concept_status===s?' selected':''}>${({candidate:'候选',approved:'已批准',deprecated:'已弃用',retired:'退役'})[s]}</option>`).join('')}
      </select>`, 'ISO 704 生命周期')
    + _frow('替代概念<span id="cf-rep-req"></span>', `<input id="cf-rep" value="${escA(c.replaced_by||'')}" placeholder="状态=已弃用时必填" style="${_FINP}">`)
    + _frow('意图路由', `<input id="cf-intent" value="${escA(c.intent||'')}" placeholder="如 knowledge_qa" style="${_FINP}">`, '命中该概念术语时路由到对应意图')
    + _frow('检索加权', `<input id="cf-boost" type="number" step="0.1" min="1" value="${c.boost||1.0}" style="${_FINP}">`)
    + (impact.length?`<div style="margin:8px 0 8px 102px;padding:8px 10px;background:#FDE8E8;border-radius:8px;font-size:12px;">
        <b style="color:#C53030;">弃用影响：${impact.length} 处仍在使用该概念的术语</b>
        <div style="margin-top:4px;">${impact.slice(0,10).map(x=>`<span class="tag">${x.kind==='entity'?'实体':'关系'}: ${esc(x.name)}</span>`).join(' ')}${impact.length>10?'…':''}</div></div>`:'')
    + `</div>
      <div id="cpt-tab-hist" style="display:none;">
        <div id="cpt-changelog"><div style="color:var(--mut);font-size:11px;margin-top:6px;">加载中…</div></div>
      </div>
      <div style="margin-top:10px;padding:8px 10px;background:#F8FAFC;border:1px solid var(--line);border-radius:8px;font-size:10.5px;color:var(--mut);line-height:1.7;">
        提示：修改后点右上角「💾 保存」；未保存切换节点将丢弃修改。</div>`;
  host.innerHTML = `${_mapDatalist(ontTypeCandidates())}${html}`;
  setTimeout(()=>enhanceMultiSelect(document.getElementById('cf-related')),0);
  window._cptAbbr = abbrs.slice();
  _abbrRender();
  cptStatusChanged();
  _cptBindDirty(host);
  host.scrollTop = sc;
  // 异步：关系 mini 图 + 变更历史
  api('/api/glossary/concepts?limit=1000').then(all=>{
    if(tok !== window._cptSelToken) return;
    const el2 = document.getElementById('cpt-minigraph'); if(!el2) return;
    const items = all.items||[];
    const labels = {}; const childrenByBroader = {};
    items.forEach(x=>{ labels[x.concept_id] = x.pref_label;
      if(x.broader){ (childrenByBroader[x.broader] = childrenByBroader[x.broader]||[]).push({id:x.concept_id, label:x.pref_label}); } });
    el2.innerHTML = _cptMiniGraph(cid, c, {labels, childrenByBroader});
  }).catch(()=>{});
  api('/api/glossary/changelog?concept_id=' + encodeURIComponent(cid)).then(cl=>{
    if(tok !== window._cptSelToken) return;
    const el = document.getElementById('cpt-changelog'); if(!el) return;
    const its = (cl.items||[]).slice(0, 15).map(x=>{
      const actCls = {flow:'b', add:'ok', merge:'w', update:'', terms:''}[x.action] || '';
      const actLbl = {flow:'流转', add:'新建', merge:'合并', update:'编辑', terms:'术语'}[x.action] || x.action;
      return `<div style="padding:4px 0;border-bottom:1px dashed var(--line);font-size:11px;line-height:1.6;">
        <span class="st ${actCls}" style="padding:0 6px;font-size:9.5px;">${actLbl}</span>
        ${esc(x.detail||'')}${x.reason?`<span style="color:var(--mut);"> · 理由: ${esc(x.reason)}</span>`:''}
        <span style="color:var(--mut);font-size:10px;margin-left:4px;">${esc((x.created_at||'').slice(0,16).replace('T',' '))}${x.operator?' · '+esc(x.operator):''}</span>
      </div>`;}).join('');
    el.innerHTML = '<div style="font-size:11.5px;font-weight:600;color:var(--blue-d);">📜 变更历史'+((cl.items||[]).length?'（'+cl.items.length+'）':'')+'</div>'
      + (its || '<div style="color:var(--mut);font-size:11px;margin-top:4px;">暂无变更记录</div>');
  }).catch(()=>{});
}
// 兼容别名：既有调用点（mini 图链/术语增删/合并流）不动
function conceptDetail(cid){ return cptSelect(cid); }
function conceptEditModal(cid){ return cptSelect(cid); }

async function cptSave(cid){
  const body = _cptFormVals();
  if(_hlRequired('cf-label')){ toast('规范词为必填项'); return; }
  if(!body.pref_label){ toast('规范词必填'); return; }
  if((window._cptEditOrigin||{}).concept_status==='approved' && (window._cptEditOrigin||{}).concept_status===body.concept_status){
    cptPromptModal('变更理由', '变更理由 *', null, v=>{
      body.change_reason = v || '维护性修改';
      _cptSaveDo(cid, body);
    }, '已批准概念的修改将进入变更历史（审计留痕）', '如：修正英文对照，来源 GB/T 说明书');
    return;
  }
  _cptSaveDo(cid, body);
}
async function _cptSaveDo(cid, body){
  const r = await api(`/api/glossary/concepts/${encodeURIComponent(cid)}`, {method:'PUT', body:JSON.stringify(body)});
  if(r.error){ toast('失败：'+r.error); return; }
  const abbr = (body.abbr||'').split(/[;；]/).map(s=>s.trim()).filter(Boolean);
  if(abbr.length){
    await api(`/api/glossary/concepts/${encodeURIComponent(cid)}/terms/batch`,
      {method:'POST', body:JSON.stringify({terms:abbr.join(';'), term_kind:'abbr', lang:'en'})});
  }
  window._cptFormDirty = false;
  conceptOptionsInvalidate();
  toast('✅ 已保存');
  conceptsLoad();          // 树徽标/计数刷新（会重新拉取编辑表单）
}

// ── 新建概念（同一编辑页布局，右详情区呈现；adopt 模式=AI 建议预填） ──
function cptCreateRender(pref, adopt){
  pref = pref||{};
  window._cptAdopt = adopt||null;
  window._cptSelNode = null;
  window._cptMode = 'create';
  cptTreeRefresh();
  const host = document.getElementById('cpt-detail');
  if(!host) return;
  Promise.all([ensureOntTypes(), conceptOptions(null)]).then(([,cands])=>{
    const sug = (adopt&&adopt.sug)||{};
    const banner = adopt
      ? `<div style="margin-bottom:10px;padding:8px 12px;background:#F5F3FF;border:1px solid #DDD6FE;border-radius:8px;font-size:11.5px;color:#7c3aed;line-height:1.7;">
          <b>💡 AI 建议采纳</b>${sug.definition?' · AI 已预填下列字段（可修改）':''}
          ${sug.is_new_concept===false?`<div style="margin-top:3px;">🔀 AI 判定这可能不是新概念${sug.synonym_of?`（应归属 ${esc(sug.synonym_of)}）`:''}——建议关闭后用「合并到已有概念」。</div>`:''}
        </div>`
      : '';
    const html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
        <b style="font-size:15px;">新建概念</b><span style="color:var(--mut);font-size:11px;">创建后为候选（candidate），走「⏳ 待审定」审批</span>
        <span style="flex:1"></span>
        <button class="btn ghost" onclick="cptCreateCancel()">取消</button>
        <button class="btn" onclick="cptCreateSubmit()" style="min-width:88px;">✓ 创建</button>
      </div>` + banner
      + _fsec('基本信息')
      + _frow('规范词'+_req(), `<input id="cf-label" value="${escA(pref.pref_label||'')}" placeholder="概念的首选术语" style="${_FINP}">`, '发现词与规范词不同会自动登记为同义词')
      + _frow('英文对照', `<input id="cf-en" value="${escA(pref.english||'')}" placeholder="Transponder" style="${_FINP}">`)
      + _frow('缩写', _abbrBoxHtml(), '输入后回车新增 · 点击 × 删除')
      + _fsec('定义与上下文')
      + _frow('定义'+_reqC('提交审批（approved）时必填——ISO 704'), `<textarea id="cf-def" rows="3" placeholder="描述该概念是什么、与相邻概念的区别" style="${_FINP}resize:vertical;">${esc(pref.definition||'')}</textarea>`, 'ISO 704：定义为空不能提交审批')
      + _frow('语境 / 示例', `<input id="cf-context" value="${escA(pref.context||'')}" style="${_FINP}">`)
      + _frow('定义来源', `<input id="cf-dsrc" value="${escA(pref.source||'')}" placeholder="如 GB/T XXXX" style="${_FINP}">`)
      + _frow('注', `<textarea id="cf-note" rows="2" style="${_FINP}resize:vertical;">${esc(pref.note||'')}</textarea>`)
      + _frow('域', `<input id="cf-domain" value="${escA(pref.domain||'')}" placeholder="如：载荷" style="${_FINP}">`)
      + _frow('状态', `<select id="cf-status" onchange="cptStatusChanged()" style="${_FINP}">
          <option value="candidate">候选</option><option value="approved">已批准（需定义非空）</option></select>`)
      + _fsec('关联信息')
      + _frow('上位概念', `<select id="cf-broader" style="${_FINP}"><option value="">— 无（顶级概念）—</option>
          ${cands.map(o=>`<option value="${escA(o.id)}"${(pref.broader||adopt&&adopt.sug&&adopt.sug.broader)===o.id?' selected':''}>${esc(o.label)}</option>`).join('')}</select>`)
      + _frow('相关概念', `<select id="cf-related" multiple size="3" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;">
          ${cands.map(o=>`<option value="${escA(o.id)}">${esc(o.label)}</option>`).join('')}</select>`)
      + _fsec('本体映射')
      + _frow('映射对象', _mapRow('cf', {}), '可留空稍后补');
    host.innerHTML = `${_mapDatalist(ontTypeCandidates())}${html}`;
    setTimeout(()=>enhanceMultiSelect(document.getElementById('cf-related')),0);
    window._cptAbbr = (pref.abbr||[]).slice();
    _abbrRender();
    cptStatusChanged();
    _cptBindDirty(host);
    host.scrollTop = 0;
  });
}
// ⑤ mini 卡片新建（对标本体：输名称回车 → 同步出现在左树与右侧编辑区）
function cptCreateMini(pref){
  pref = pref||{};
  window._cptAdopt = null; window._cptMode = 'create'; window._cptSelNode = null; window._cptFormDirty = false;
  window._cptMiniBroader = pref.broader||'';
  cptTreeRefresh();
  const host = document.getElementById('cpt-detail');
  if(!host) return;
  const bLabel = pref.broader ? (((window._cptItems||[]).find(x=>x.concept_id===pref.broader)||{}).pref_label || pref.broader) : '';
  host.innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
      <b style="font-size:14.5px;">新建概念</b>
      <span style="color:var(--mut);font-size:11px;">输入名称回车创建，随后在右侧完善字段</span></div>
    <div style="border:1px solid var(--line);border-radius:10px;padding:20px 22px;background:#F8FAFD;max-width:440px;">
      <div style="font-size:12px;color:var(--mut);margin-bottom:6px;">规范词 <span style="color:#C53030;font-weight:700;">*</span></div>
      <input id="cpt-mini-name" placeholder="如：行波管放大器" style="width:100%;border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:13px;font-family:inherit;box-sizing:border-box;"
        onkeydown="if(event.key==='Enter'){event.preventDefault();cptMiniSubmit();}">
      <div style="margin-top:10px;font-size:10.5px;color:var(--mut);line-height:1.8;">
        ${bLabel?`上位概念将默认填入「<b>${esc(bLabel)}</b>」。<br>`:''}创建后为候选（candidate），进「⏳ 待审定」审批；定义补全后方可批准（ISO 704）。</div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
        <button class="btn ghost" onclick="cptCreateCancel()">取消</button>
        <button class="btn" onclick="cptMiniSubmit()">创建</button>
      </div>
    </div>`;
  setTimeout(()=>{ const el=document.getElementById('cpt-mini-name'); if(el) el.focus(); },60);
}
async function cptMiniSubmit(){
  if(_hlRequired('cpt-mini-name')){ toast('概念名称为必填项'); return; }
  const name = ((document.getElementById('cpt-mini-name')||{}).value||'').trim();
  const body = {pref_label: name, concept_status: 'candidate', definition: '', domain: 'unknown'};
  if(window._cptMiniBroader) body.broader = window._cptMiniBroader;
  const r = await api('/api/glossary/concepts', {method:'POST', body:JSON.stringify(body)});
  if(r.error){ toast('创建失败：'+r.error); return; }
  window._cptMode = null; window._cptMiniBroader = '';
  conceptOptionsInvalidate();
  toast('✅ 已创建「'+name+'」（candidate）——请补全定义后提交审批');
  await conceptsLoad();
  cptSelect(r.concept_id);
}
function cptCreateCancel(){
  window._cptAdopt = null; window._cptFormDirty = false; window._cptMode = null;
  if(window._cptSelNode) cptSelect(window._cptSelNode);
  else document.getElementById('cpt-detail').innerHTML = _cptDetailEmpty(!(window._cptItems||[]).length);
}
async function cptCreateSubmit(){
  const b = _cptFormVals();
  if(_hlRequired('cf-label')){ toast('规范词为必填项'); return; }
  if(b.concept_status==='approved' && _hlRequired('cf-def')){
    toast('批准需定义非空（ISO 704）——请填写定义或改选候选');
    return;
  }
  if(window._cptAdopt){
    // AI 建议采纳：走 adopt 通道（创建概念 + 同义词登记 + 发现池状态流转）
    const did = window._cptAdopt.did;
    const r = await api(`/api/glossary/discoveries/${did}/adopt`, {method:'POST',
      body:JSON.stringify({pref_label:b.pref_label, definition:b.definition, english:b.english,
                           domain:b.domain, broader:b.broader})});
    if(!r || r.error){ toast('采纳失败：'+((r&&r.error)||'')); return; }
    if(b.abbr){
      await api(`/api/glossary/concepts/${encodeURIComponent(r.concept_id)}/terms/batch`,
        {method:'POST', body:JSON.stringify({terms:b.abbr, term_kind:'abbr', lang:'en'})}).catch(()=>{});
    }
    window._cptAdopt = null; window._cptFormDirty = false; window._cptMode = null;
    conceptOptionsInvalidate();
    toast('✅ 已创建概念 '+r.pref_label+'（candidate）——可在待审定看板继续处理');
    await conceptsLoad();
    cptSelect(r.concept_id);
    return;
  }
  const r = await api('/api/glossary/concepts', {method:'POST', body:JSON.stringify(
    Object.assign({}, b, {abbr:undefined}))});
  if(r.error){ toast('失败：'+r.error); return; }
  if(b.abbr){
    await api(`/api/glossary/concepts/${encodeURIComponent(r.concept_id)}/terms/batch`,
      {method:'POST', body:JSON.stringify({terms:b.abbr, term_kind:'abbr', lang:'en'})});
  }
  window._cptFormDirty = false; window._cptMode = null;
  conceptOptionsInvalidate();
  toast('✅ 概念已创建');
  await conceptsLoad();
  cptSelect(r.concept_id);
}

// 2026-09-07 分区常量
const _TERM_KIND_CN = {preferred:'首选',synonym:'同义',abbr:'缩写',alias:'别名',hidden:'禁用'};
async function conceptFlow(cid, next){
  if(next==='deprecated'){
    conceptOptions(cid).then(opts=>{
      cptPromptModal('弃用概念', '替代概念 *', opts, v=>{
        _conceptFlowDo(cid, 'deprecated', v);
      }, '弃用后由替代概念承接召回（replaced_by）；旧词保留可检索', '选择承接召回落词的概念');
    });
    return;
  }
  if(next==='approved' && _hlRequired('cf-def')){
    toast('批准拦截：定义为必填项（ISO 704）——请填写后保存再批准');
    return;
  }
  _conceptFlowDo(cid, next);
}
async function _conceptFlowDo(cid, next, replaced_by){
  const body = {concept_status: next};
  if(replaced_by) body.replaced_by = replaced_by;
  const r = await api(`/api/glossary/concepts/${encodeURIComponent(cid)}`, {method:'PUT', body:JSON.stringify(body)});
  if(r.error){ toast('失败：'+r.error); return; }
  toast('✅ 状态已流转');
  window._cptFormDirty = false;
  conceptsLoad();
}
// R3：批量加术语（分号/换行分隔，可选语种与类型）
async function termBatchAdd(cid){
  const raw = ((document.getElementById('new-term-input')||{}).value||'').trim();
  if(!raw){ toast('请输入术语（多个用分号或换行分隔）'); return; }
  const kind = (document.getElementById('new-term-kind')||{}).value||'synonym';
  const lang = (document.getElementById('new-term-lang')||{}).value||'';
  const r = await api(`/api/glossary/concepts/${encodeURIComponent(cid)}/terms/batch`,
    {method:'POST', body:JSON.stringify({terms:raw, term_kind:kind, lang})});
  if(r.error){ toast('失败：'+r.error); return; }
  let msg = `✅ 新增 ${r.added} 条术语${r.skipped?`，${r.skipped} 条已存在`:''}`;
  if((r.dups||[]).length) msg += ` · ⚠ ${r.dups.length} 条与其他概念冲突`;
  toast(msg);
  cptSelect(cid, {keepScroll:true}); conceptsLoad();
}
// R3：同形异义合并——源概念术语并入目标，源转 deprecated 并指向目标
async function conceptMergeModal(cid){
  const r = await api('/api/glossary/concepts?limit=500');
  const cands = (r.items||[]).filter(c=>c.concept_id!==cid);
  if(!cands.length){ toast('没有其他可合并的概念'); return; }
  const lbx = document.getElementById('lbx'), body = document.getElementById('lbx-body');
  if(!lbx || !body){ toast('弹窗容器缺失'); return; }
  body.innerHTML = `<h3>⇄ 合并同形异义概念</h3>
    <div style="font-size:12px;color:var(--mut);margin:4px 0 8px;line-height:1.6;">
      源概念 <b>${esc(cid)}</b> 的术语将并入目标概念，源转为「已弃用」并指向目标（旧词保留可召回）。</div>
    <div style="margin-bottom:8px;">
      <div style="font-size:12px;margin-bottom:4px;">目标概念（可搜索）</div>
      <input id="mg-kw" oninput="conceptMergeFilter()" placeholder="🔍 搜索概念名/ID" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:12px;box-sizing:border-box;">
      <select id="mg-target" size="8" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;margin-top:4px;">
        ${cands.map(c=>`<option value="${escA(c.concept_id)}">${esc(c.pref_label)}　${esc(c.concept_id)}</option>`).join('')}
      </select>
    </div>
    <div class="lbx-actions"><button class="btn ghost" id="mg-cancel">取消</button><button class="btn" id="mg-ok">确认合并</button></div>`;
  lbx.classList.add('show');
  window._mgAll = cands.slice();
  document.getElementById('mg-cancel').onclick = ()=>lbx.classList.remove('show');
  document.getElementById('mg-ok').onclick = async ()=>{
    const tgt = (document.getElementById('mg-target')||{}).value;
    if(!tgt){ toast('请选择目标概念'); return; }
    if(!(await confirmDialog(`确认把「${cid}」合并入「${tgt}」？源概念将置为已弃用。`))) return;
    const rr = await api('/api/glossary/concepts/merge', {method:'POST',
      body:JSON.stringify({source_cid:cid, target_cid:tgt})});
    if(rr.error){ toast('失败：'+rr.error); return; }
    lbx.classList.remove('show');
    toast(`✅ 已合并：迁移 ${rr.moved} 条术语`);
    if(window._cptSelNode===cid){ window._cptSelNode = tgt; }
    window._cptFormDirty = false;
    conceptsLoad();
  };
}
function conceptMergeFilter(){
  const kw = (((document.getElementById('mg-kw')||{}).value)||'').trim().toLowerCase();
  const sel = document.getElementById('mg-target');
  if(!sel) return;
  sel.innerHTML = (window._mgAll||[]).filter(c=>!kw || c.pref_label.toLowerCase().includes(kw)
      || c.concept_id.toLowerCase().includes(kw))
    .map(c=>`<option value="${escA(c.concept_id)}">${esc(c.pref_label)}　${esc(c.concept_id)}</option>`).join('');
}
async function termRemove(tid, term, cid){
  if(!(await confirmDialog(`删除术语「${term}」？`))) return;
  const r = await api(`/api/glossary/terms/${tid}`, {method:'DELETE'});
  if(r.error){ toast('失败：'+r.error); return; }
  toast('已删除'); cptSelect(cid, {keepScroll:true}); conceptsLoad();
}
// 本体类型候选（供映射下拉 datalist 搜索）：{entity:[], attribute:[], relation:[]}
function ontTypeCandidates(){
  const t = (ontData && ontData.types) || [];
  return {
    entity: [...new Set(t.filter(x=>x.type_kind==='entity').map(x=>x.name))].sort(),
    attribute: [...new Set(t.filter(x=>x.type_kind==='attribute').map(x=>x.name))].sort(),
    relation: [...new Set(t.filter(x=>x.type_kind==='relation').map(x=>x.name))].sort(),
  };
}
function _mapDatalist(cands){
  return ['entity','attribute','relation'].map(k=>
    `<datalist id="dl-${k}">${cands[k].map(n=>`<option value="${escA(n)}">`).join('')}</datalist>`).join('');
}
function _mapRow(prefix, cur){
  const kind = cur.cls ? 'entity' : cur.prop ? 'attribute' : cur.inst ? 'relation' : '';
  const val = cur.cls || cur.prop || cur.inst || '';
  return `<div style="display:flex;gap:8px;margin-top:4px;">
    <select id="${prefix}-mapkind" onchange="ontMapKindChanged('${prefix}')" style="width:96px;flex:none;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;">
      <option value="">不映射</option>
      <option value="entity"${kind==='entity'?' selected':''}>类</option>
      <option value="attribute"${kind==='attribute'?' selected':''}>属性</option>
      <option value="relation"${kind==='relation'?' selected':''}>关系</option>
    </select>
    <input id="${prefix}-mapval" list="dl-entity" value="${escA(val)}" placeholder="选择或输入本体类型名（可搜索）" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:12px;">
  </div>`;
}
function ontMapKindChanged(prefix){
  const k = (document.getElementById(prefix+'-mapkind')||{}).value || 'entity';
  const el = document.getElementById(prefix+'-mapval');
  if(el) el.setAttribute('list', 'dl-'+k);
}
function _collectMap(prefix){
  const k = (document.getElementById(prefix+'-mapkind')||{}).value || '';
  const v = ((document.getElementById(prefix+'-mapval')||{}).value||'').trim();
  return {maps_to_class: k==='entity'?v:'', maps_to_prop: k==='attribute'?v:'', maps_to_inst: k==='relation'?v:''};
}
async function ensureOntTypes(){
  if(ontData && (ontData.types||[]).length) return;
  try{ await loadOntology(); }catch(e){}
}
// ── ISO 25964 / SKOS 概念关系（上位 broader / 相关 related）──
let _cptOptionCache = null;
async function conceptOptions(excludeCid){
  if(!_cptOptionCache){
    try{
      const r = await api('/api/glossary/concepts?limit=1000');
      _cptOptionCache = (r.items||[]).map(c=>({id:c.concept_id, label:c.pref_label}));
    }catch(e){ _cptOptionCache = []; }
  }
  return _cptOptionCache.filter(c=>c.id !== excludeCid);
}
function conceptOptionsInvalidate(){ _cptOptionCache = null; }
function _collectRel(prefix){
  return {
    broader: ((document.getElementById(prefix+'-broader')||{}).value||'').trim(),
    related: Array.from(((document.getElementById(prefix+'-related')||{}).selectedOptions)||[])
              .map(o=>o.value).filter(Boolean).join(','),
  };
}

// ══ R2 导入导出（2026-09-07）：CSV/Excel 导入 + dry-run 预检 + CSV/SKOS 导出 ══
function glossaryExportCsv(){ location.href = '/api/glossary/export.csv'; }
function glossaryExportSkos(){ location.href = '/api/glossary/export.skos'; }
function glossaryTemplate(){ location.href = '/api/glossary/template.csv'; }
// 工具栏下拉菜单（导出收口，2026-09-07）：toggle + 点外部关闭
function gddToggle(id, ev){
  if(ev) ev.stopPropagation();
  const el = document.getElementById(id);
  if(!el) return;
  const was = el.classList.contains('open');
  document.querySelectorAll('.gdd.open').forEach(x=>x.classList.remove('open'));
  if(!was) el.classList.add('open');
}
function gddRun(fn){
  document.querySelectorAll('.gdd.open').forEach(x=>x.classList.remove('open'));
  try{ fn(); }catch(e){ toast('操作失败：'+e.message); }
}
document.addEventListener('click', ()=>document.querySelectorAll('.gdd.open').forEach(x=>x.classList.remove('open')));
let _glImpFile = null;
function glossaryImportModal(){
  const lbx = document.getElementById('lbx'), body = document.getElementById('lbx-body');
  if(!lbx || !body){ toast('弹窗容器缺失'); return; }
  body.innerHTML = `<h3>⇧ 导入术语</h3>
    <div style="font-size:12px;color:var(--mut);margin:4px 0 10px;line-height:1.7;">
      支持 CSV / XLSX，表头需含「名称」列。先<b>预检</b>（不落库）确认问题无误后再导入。
      <a style="color:var(--blue-d);cursor:pointer;" onclick="glossaryTemplate()">下载模板</a></div>
    <input type="file" id="gi-file" accept=".csv,.txt,.xlsx,.xlsm" style="font-size:12px;margin-bottom:8px;">
    <div id="gi-report" style="max-height:300px;overflow:auto;font-size:12px;"></div>
    <div class="lbx-actions">
      <button class="btn ghost" id="gi-close">关闭</button>
      <button class="btn ghost" id="gi-check">预检 dry-run</button>
      <button class="btn" id="gi-do" disabled>确认导入</button>
    </div>`;
  lbx.classList.add('show');
  document.getElementById('gi-close').onclick = ()=>lbx.classList.remove('show');
  document.getElementById('gi-file').onchange = e=>{
    _glImpFile = e.target.files[0] || null;
    const rep = document.getElementById('gi-report');
    rep.innerHTML = _glImpFile ? `<div style="color:var(--mut);">已选择：${esc(_glImpFile.name)}（点「预检」开始校验）</div>` : '';
    document.getElementById('gi-do').disabled = true;
  };
  document.getElementById('gi-check').onclick = ()=>glossaryImportRun('dry-run');
  document.getElementById('gi-do').onclick = ()=>glossaryImportRun('commit');
}
async function glossaryImportRun(mode){
  if(!_glImpFile){ toast('请先选择文件'); return; }
  const rep = document.getElementById('gi-report');
  rep.innerHTML = '<div class="loading">处理中…</div>';
  const fd = new FormData();
  fd.append('file', _glImpFile); fd.append('mode', mode);
  try{
    const res = await fetch('/api/glossary/import', {method:'POST', body:fd});
    const r = await res.json();
    if(r.error){ rep.innerHTML = `<div style="color:#C53030;">${esc(r.error)}</div>`; return; }
    const errs = (r.issues||[]).filter(i=>i.level==='error');
    const warns = (r.issues||[]).filter(i=>i.level!=='error');
    if(mode==='commit'){
      rep.innerHTML = `<div style="color:#2F855A;font-weight:600;">✅ 导入完成</div>
        <div style="margin-top:4px;">新建 ${r.stats.created} · 更新 ${r.stats.updated} · 术语 ${r.stats.terms} · 跳过 ${r.stats.skipped}</div>`;
      document.getElementById('gi-do').disabled = true;
      conceptsLoad();
      return;
    }
    rep.innerHTML = `<div style="margin-bottom:6px;">共 ${r.total} 行，
      新建 ${(r.preview||[]).filter(p=>p.action==='新建').length} ·
      更新 ${(r.preview||[]).filter(p=>p.action==='更新').length}</div>
      ${errs.length?`<div style="color:#C53030;">错误 ${errs.length} 条：${errs.slice(0,8).map(i=>`<div>· 第${i.row}行 ${esc(i.msg)}</div>`).join('')}</div>`:''}
      ${warns.length?`<div style="color:#B7791F;margin-top:4px;">提醒 ${warns.length} 条：${warns.slice(0,8).map(i=>`<div>· 第${i.row}行 ${esc(i.msg)}</div>`).join('')}</div>`:''}
      ${!errs.length&&!warns.length?'<div style="color:#2F855A;">✅ 无问题，可导入</div>':''}`;
    document.getElementById('gi-do').disabled = errs.length > 0;
  }catch(e){ rep.innerHTML = `<div style="color:#C53030;">失败：${esc(String(e))}</div>`; }
}

// ══ 知识治理看板（P1-7，右侧滑动面板）══

// ══ P0 治理闭环：树节点勾选批量（PoolParty bulk ops 对标） ══
function cptSelToggle(cid, on){
  window._cptSel = window._cptSel || new Set();
  if(on) window._cptSel.add(cid); else window._cptSel.delete(cid);
  _cptBatchBarSync();   // 只同步工具条，不重建树（避免勾选时滚动位置丢失）
}
function _cptBatchBarSync(){
  const bar = document.getElementById('cpt-batch-bar');
  const n = (window._cptSel||new Set()).size;
  if(!bar) return;
  bar.style.display = n ? 'flex' : 'none';
  const cnt = bar.querySelector('b');
  if(cnt) cnt.textContent = n;
}
function cptSelAll(on){
  window._cptSel = window._cptSel || new Set();
  document.querySelectorAll('#cpt-tree input[type=checkbox]').forEach(cb=>{
    const m = (cb.getAttribute('onchange')||'').match(/'([^']+)'/);
    if(!m) return;
    if(on) window._cptSel.add(m[1]); else window._cptSel.delete(m[1]);
  });
  cptTreeRefresh(); _cptBatchBarSync();
}
function cptBatchClear(){
  window._cptSel = new Set();
  document.querySelectorAll('#cpt-tree input[type=checkbox]').forEach(cb=>{ cb.checked = false; });
  _cptBatchBarSync();
}
function cptFilterReset(){
  const st = document.getElementById('cpts-status'); if(st) st.value = '';
  const kw = document.getElementById('cpts-kw'); if(kw) kw.value = '';
  conceptsLoad();
}
async function cptBatch(action, extra){
  const ids = [...(window._cptSel||new Set())];
  if(!ids.length){ toast('请先勾选概念'); return; }
  const body = Object.assign({ids, action}, extra||{});
  const r = await api('/api/glossary/concepts/batch', {method:'POST', body:JSON.stringify(body)});
  if(!r || r.error){ toast('失败：'+((r&&r.error)||'')); return; }
  const skip = (r.skipped||[]);
  if(skip.length){
    toast(`✅ 成功 ${r.done} 项；跳过 ${skip.length} 项（${skip.slice(0,3).map(x=>x.cid.replace('C-','')+': '+x.error).join('；')}${skip.length>3?'…':''}）`, 6000);
  } else {
    toast(`✅ 批量操作完成（${r.done} 项）`);
  }
  cptBatchClear();
  conceptsLoad();
}
function cptBatchFlow(){
  const ids = [...(window._cptSel||new Set())];
  if(!ids.length){ toast('请先勾选概念'); return; }
  openPanel(`⤵ 批量状态流转（${ids.length} 个概念）`, `
    <div style="font-size:12.5px;line-height:1.8;">
      <div style="margin-bottom:10px;color:var(--mut);">治理规则与单条一致：批准需定义非空（ISO 704）；弃用需替代概念——不满足的自动跳过并列出原因。</div>
      <div style="margin-bottom:10px;"><b>目标状态</b><br>
        <select id="pb-status" style="width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
          <option value="approved">已批准（approved）</option>
          <option value="deprecated">已弃用（deprecated）</option>
          <option value="retired">退役（retired）</option>
        </select></div>
      <div id="pb-rep-row" style="margin-bottom:10px;display:none;"><b>替代概念 <span style="color:var(--red);">*</span></b><br>
        <select id="pb-replaced" style="width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
          ${(window._cptOptions||[]).map(o=>`<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('')}
        </select>
        <div style="font-size:10.5px;color:var(--mut);margin-top:3px;">弃用概念将由替代概念承接召回（replaced_by）</div></div>
      <div style="margin-bottom:10px;"><b>变更理由</b><br>
        <input id="pb-reason" placeholder="记录本次批量流转理由（进入变更历史）" style="width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;font-family:inherit;"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn ghost" onclick="closePanel()">取消</button>
        <button class="btn primary" onclick="cptBatchFlowSubmit()">确认流转</button>
      </div>
    </div>`);
  const st = document.getElementById('pb-status');
  if(st) st.onchange = () => { const row=document.getElementById('pb-rep-row'); if(row) row.style.display = st.value==='deprecated' ? '' : 'none'; };
}
function cptBatchFlowSubmit(){
  const tgt = (document.getElementById('pb-status')||{}).value||'';
  const extra = {target_status: tgt, reason: (document.getElementById('pb-reason')||{}).value||''};
  if(tgt==='deprecated'){
    const rep = (document.getElementById('pb-replaced')||{}).value||'';
    if(!rep){ toast('弃用必须选择替代概念'); return; }
    extra.replaced_by = rep;
  }
  closePanel();
  cptBatch('flow', extra);
}
function cptBatchDomain(){
  const ids = [...(window._cptSel||new Set())];
  if(!ids.length){ toast('请先勾选概念'); return; }
  const doms = window._cptDomains||[];
  openPanel(`🏷 批量修改域（${ids.length} 个概念）`, `
    <div style="font-size:12.5px;line-height:1.8;">
      <div style="margin-bottom:10px;color:var(--mut);">域（subject field）用于检索过滤与知识分域；下拉选项来自现有概念的域去重。</div>
      <div style="margin-bottom:10px;"><b>目标域</b><br>
        <select id="pb-domain" style="width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
          ${doms.map(d=>`<option value="${esc(d)}">${esc(d)}</option>`).join('')}
          <option value="__new">＋ 新建域…</option>
        </select></div>
      <div id="pb-dom-new" style="margin-bottom:10px;display:none;"><b>新域名称</b><br>
        <input id="pb-domain-new" placeholder="输入新域名称" style="width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;font-family:inherit;"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn ghost" onclick="closePanel()">取消</button>
        <button class="btn primary" onclick="cptBatchDomainSubmit()">确认修改</button>
      </div>
    </div>`);
  const sel = document.getElementById('pb-domain');
  if(sel) sel.onchange = () => { const row=document.getElementById('pb-dom-new'); if(row) row.style.display = sel.value==='__new' ? '' : 'none'; };
}
function cptBatchDomainSubmit(){
  let dom = (document.getElementById('pb-domain')||{}).value||'';
  if(dom==='__new') dom = ((document.getElementById('pb-domain-new')||{}).value||'').trim();
  if(!dom){ toast('域名称必填'); return; }
  closePanel();
  cptBatch('domain', {domain: dom});
}

// 概念关系 mini 图：中心概念 + 上位/下位/相关（SVG 星形，对标 Visual Mapper 1-hop）
function _cptMiniGraph(cid, c, ctx){
  const nodes = [{id:cid, label:c.pref_label, kind:'self'}];
  const push = (id, label, kind) => { if(id && label && !nodes.some(n=>n.id===id)) nodes.push({id, label, kind}); };
  if(c.broader) push(c.broader, (ctx && ctx.labels && ctx.labels[c.broader]) || c.broader_label || c.broader, 'broader');
  String(c.related||'').split(',').filter(Boolean).forEach(rid=>{
    push(rid, (ctx && ctx.labels && ctx.labels[rid]) || rid, 'related');
  });
  const chList = (ctx && ctx.childrenByBroader) ? (ctx.childrenByBroader[cid]||[]) : [];
  chList.forEach(x=>push(x.id, x.label, 'narrower'));
  if(nodes.length < 2) return '<div style="color:var(--mut);font-size:11px;padding:4px 0;">暂无关系可视化数据（补上位/相关概念并保存后自动生成）</div>';
  const W = 480, H = 210, cx = W/2, cy = H/2;
  const others = nodes.filter(n=>n.id!==cid);
  const R = 78;
  const pos = others.map((n,i)=>{
    const a = -Math.PI/2 + (2*Math.PI*i)/others.length;
    return {n, x: cx + R*Math.cos(a), y: cy + R*Math.sin(a)*0.82};
  });
  const kindColor = {broader:'#8A6D3B', narrower:'#2F6F4F', related:'#7c3aed'};
  const lines = pos.map(p=>{
    const col = kindColor[p.n.kind] || '#999';
    const dash = p.n.kind==='related' ? ' stroke-dasharray="4 3"' : '';
    const lbl = p.n.kind==='broader' ? '上位' : (p.n.kind==='narrower' ? '下位' : '相关');
    return '<line x1="'+cx+'" y1="'+cy+'" x2="'+p.x.toFixed(1)+'" y2="'+p.y.toFixed(1)+'" stroke="'+col+'" stroke-width="1.4"'+dash+'/>'
      + '<text x="'+((cx+p.x)/2).toFixed(1)+'" y="'+((cy+p.y)/2-3).toFixed(1)+'" font-size="8.5" fill="'+col+'" text-anchor="middle">'+lbl+'</text>';
  }).join('');
  const dots = pos.map(p=>{
    const col = kindColor[p.n.kind] || '#999';
    return '<circle cx="'+p.x.toFixed(1)+'" cy="'+p.y.toFixed(1)+'" r="6" fill="#fff" stroke="'+col+'" stroke-width="1.6"/>'
      + '<text x="'+p.x.toFixed(1)+'" y="'+(p.y+18).toFixed(1)+'" font-size="9.5" text-anchor="middle">'+esc(p.n.label.slice(0,10))+'</text>';
  }).join('');
  return '<svg width="100%" viewBox="0 0 '+W+' '+H+'" style="background:#fbfcfe;border:1px solid var(--line);border-radius:8px;max-width:520px;">'
    + lines
    + '<circle cx="'+cx+'" cy="'+cy+'" r="13" fill="#E6F1FB" stroke="#1d4ed8" stroke-width="2"/>'
    + '<text x="'+cx+'" y="'+(cy+34).toFixed(1)+'" font-size="11" font-weight="600" fill="#0C447C" text-anchor="middle">'+esc(c.pref_label.slice(0,12))+'</text>'
    + dots + '</svg>';
}

// ══ P2-10：AI 建议流·发现池（对标 PoolParty Taxonomy Advisor：自动收集/人工触发 AI/审批入库） ══
let _discCache = [];
async function discoveryPanel(){
  openPanel('💡 AI 建议 · 发现池', '<div class="loading">加载发现池…</div>');
  const r = await api('/api/glossary/discoveries?status=all');
  if(r.error){ document.getElementById('panel-body').innerHTML = '<div class="fempty">失败：'+esc(r.error)+'</div>'; return; }
  _discCache = r.items||[];
  if(!window._cptDiscTab) window._cptDiscTab = 'discovered';
  _discRender();
}
function discTab(t){ window._cptDiscTab = t; _discRender(); }
function _discRender(){
  const groups = {discovered:[], adopted:[], dismissed:[]};
  _discCache.forEach(x=>(groups[x.status]||groups.discovered).push(x));
  const tab = window._cptDiscTab||'discovered';
  const stMap = {discovered:['w','🆕 待处理'], adopted:['ok','✅ 已采纳'], dismissed:['','⛔ 已忽略']};
  const card = x=>{
    const st = stMap[x.status]||['','?'];
    const sug = x.suggestion||{};
    const sugTxt = sug.pref_label
      ? '<div style="margin-top:5px;padding:6px 9px;background:#EAF3DE;border-radius:6px;font-size:11px;line-height:1.7;">'
        + '<b style="color:#2F6F4F;">AI 建议：</b>'+esc(sug.pref_label||'')+(sug.english?(' · '+esc(sug.english)):'')+(sug.domain?(' · 域:'+esc(sug.domain)):'')
        + (sug.definition?('<div style="color:var(--mut);margin-top:2px;">'+esc(sug.definition)+'</div>'):'')
        + (sug.is_new_concept===false?('<div style="color:#7c3aed;margin-top:2px;">🔀 AI 判定：非新概念'+(sug.synonym_of?('（应归属 '+esc(sug.synonym_of)+'）'):'')+'</div>'):'')
        + '</div>' : '';
    const ops = x.status==='discovered'
      ? '<button class="btn sm" style="padding:2px 10px;font-size:11px;" onclick="discoverySuggest('+x.id+')">🤖 AI 预填</button>'
        + '<button class="btn sm ghost" style="padding:2px 10px;font-size:11px;" onclick="discoveryAdopt('+x.id+')" title="进入统一建词页（AI 字段已预填，可修改）">＋ 采纳</button>'
        + '<button class="btn sm ghost" style="padding:2px 10px;font-size:11px;color:var(--mut);" onclick="discoveryDismiss('+x.id+')">⛔ 忽略</button>'
      : '';
    return '<div style="border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:8px;background:#fff;">'
      + '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">'
      + '<b style="font-size:12.5px;">'+esc(x.term)+'</b>'
      + '<span class="st '+st[0]+'" style="padding:0 6px;font-size:9.5px;">'+st[1]+'</span>'
      + '<span style="font-size:10.5px;color:#B7791F;" title="归一中未命中的出现次数">频次 ×'+(x.freq||1)+'</span>'
      + '<span style="font-size:10px;color:var(--mut);">'+esc((x.created_at||'').slice(0,10))+'</span>'
      + '<span style="flex:1"></span>'+ops+'</div>'
      + (x.context?('<div style="margin-top:3px;font-size:10.5px;color:var(--mut);">上下文：'+esc(x.context.slice(0,80))+'</div>'):'')
      + sugTxt + '</div>';
  };
  const tbtn = (k, label, n)=>'<button class="cpt-dtab'+(tab===k?' on':'')+'" onclick="discTab(\''+k+'\')">'+label+'（'+n+'）</button>';
  document.getElementById('panel-body').innerHTML =
    '<div style="font-size:11.5px;color:var(--mut);line-height:1.8;margin-bottom:10px;padding:8px 12px;background:var(--blue-l);border-radius:8px;">'
    + '<b>词库的"发现漏斗"：</b>AI 建模/归一校验中<b>未命中词库</b>的实体名自动收集到这里（不进词库）。值得沉淀的 → 「AI 预填」→「采纳」（统一建词页，AI 字段可改）；一次性实例名 → 「忽略」。</div>'
    + '<div style="display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:10px;">'
    + tbtn('discovered','🆕 待处理',groups.discovered.length)
    + tbtn('adopted','✅ 已采纳',groups.adopted.length)
    + tbtn('dismissed','⛔ 已忽略',groups.dismissed.length)
    + '<span style="flex:1"></span></div>'
    + (groups[tab].length ? groups[tab].map(card).join('') : '<div style="color:var(--mut);font-size:11.5px;padding:14px 0;text-align:center;">该状态下暂无记录</div>');
}
async function discoverySuggest(did){
  toast('🤖 AI 分析中…（生成规范词/定义/英文/上位词/消歧预判）');
  const r = await api('/api/glossary/discoveries/'+did+'/suggest', {method:'POST'});
  if(!r || r.error){ toast('AI 预填失败：'+((r&&r.error)||'')); return; }
  toast('✅ AI 建议已生成（卡片内可查看）'); discoveryPanel();
}
async function discoveryDismiss(did){
  cptPromptModal('⛔ 忽略发现', '忽略原因（可选）', null, v=>{
    _discoveryDismissDo(did, v);
  }, '一次性实例名 / 测试词 / 错拼等——忽略后不再提示', '如：一次性实例名', true);
}
async function _discoveryDismissDo(did, reason){
  const r = await api('/api/glossary/discoveries/'+did+'/dismiss', {method:'POST', body:JSON.stringify({reason: reason||''})});
  if(!r || r.error){ toast('失败：'+((r&&r.error)||'')); return; }
  discoveryPanel();
}
// 采纳：关闭发现池面板 → 右详情区打开统一建词页（AI 预填可改），与列表新建同一布局
async function discoveryAdopt(did){
  closePanel();
  const r = await api('/api/glossary/discoveries?status=all');
  const hit = ((r&&r.items)||[]).find(x=>x.id===did);
  const sug = (hit&&hit.suggestion)||{};
  cptCreateRender(
    {pref_label:sug.pref_label||hit.term, definition:sug.definition||'', english:sug.english||'',
     domain:sug.domain||'', abbr:sug.abbr||[]},
    {did, sug});
}

// ② 详情页 tab：详情 / 变更历史（切换仅显隐，数据各自异步加载）
function cptDetailTab(t){
  ['info','hist'].forEach(k=>{
    const b=document.getElementById('cpt-dt-'+k);
    if(b) b.classList.toggle('on', k===t);
    const c=document.getElementById('cpt-tab-'+k);
    if(c) c.style.display = k===t ? '' : 'none';
  });
}

// ══ 五轮反馈（2026-09-09）：树节点增删 + 系统弹框替代浏览器 prompt ══
// ① 新增直接下级：上位概念默认填入所选节点
function cptCreateChild(cid){
  const c = (window._cptItems||[]).find(x=>x.concept_id===cid);
  cptCreateMini({broader: cid, domain: (c&&c.domain)||''});
}
// ① 删除概念（治理拦截在服务端：有下级 / 被引用为替代概念）
async function conceptDelete(cid){
  const c = (window._cptItems||[]).find(x=>x.concept_id===cid);
  if(!(await confirmDialog('确认删除概念「'+((c&&c.pref_label)||cid)+'」？其术语与变更历史将一并删除（不可恢复）。'))) return;
  const r = await api('/api/glossary/concepts/'+encodeURIComponent(cid), {method:'DELETE'});
  if(r.error){ toast('删除失败：'+r.error); return; }
  toast('✅ 已删除');
  if(window._cptSelNode===cid){ window._cptSelNode=null; window._cptFormDirty=false; }
  conceptOptionsInvalidate();
  conceptsLoad();
}
// ④ 通用输入弹框（替代浏览器 prompt）：opts=数组时渲染下拉，否则文本输入；allowEmpty 允许空提交
function cptPromptModal(title, label, opts, cb, hint, placeholder, allowEmpty){
  const F = 'width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:12.5px;font-family:inherit;box-sizing:border-box;';
  const field = Array.isArray(opts)
    ? '<select id="cpt-pm-in" style="'+F+'"><option value="">— 请选择 —</option>'
      + opts.map(o=>'<option value="'+esc(o.id)+'">'+esc(o.label)+'</option>').join('')+'</select>'
    : '<input id="cpt-pm-in" placeholder="'+escA(placeholder||'')+'" style="'+F+'">';
  openPanel(title, '<div style="font-size:12.5px;line-height:1.8;">'
    + '<div style="margin-bottom:4px;"><b>'+label+'</b></div>'
    + (hint?'<div style="font-size:11px;color:var(--mut);margin-bottom:8px;line-height:1.7;">'+esc(hint)+'</div>':'')
    + field
    + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">'
    + '<button class="btn ghost" onclick="closePanel()">取消</button>'
    + '<button class="btn" onclick="cptPromptSubmit()">确认</button></div></div>');
  window._cptPmCb = cb; window._cptPmEmpty = !!allowEmpty;
  setTimeout(()=>{ const el=document.getElementById('cpt-pm-in');
    if(el){ el.focus(); el.onkeydown=e=>{ if(e.key==='Enter' && el.tagName==='INPUT') cptPromptSubmit(); }; } },60);
}
function cptPromptSubmit(){
  const v = ((document.getElementById('cpt-pm-in')||{}).value||'').trim();
  if(!v && !window._cptPmEmpty){ toast('必填项不能为空'); return; }
  closePanel();
  const cb = window._cptPmCb; window._cptPmCb=null; window._cptPmEmpty=false;
  if(cb) cb(v);
}

// ══ 六轮反馈（2026-09-09）：缩写 chips（回车新增/×删除） ══
function _abbrBoxHtml(){
  window._cptAbbr = window._cptAbbr||[];
  return '<div id="cf-abbr-box" style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;border:1px solid var(--line);border-radius:6px;padding:4px 8px;min-height:34px;background:#fff;">'
    + '<input type="hidden" id="cf-abbr">'
    + '<input id="cf-abbr-in" placeholder="输入缩写后回车新增" style="border:0;outline:none;flex:1;min-width:120px;font-size:12.5px;font-family:inherit;background:transparent;" onkeydown="cptAbbrKey(event)">'
    + '</div>';
}
function _abbrRender(){
  const box = document.getElementById('cf-abbr-box');
  if(!box) return;
  box.querySelectorAll('.cpt-abbr-tag').forEach(t=>t.remove());
  const inEl = document.getElementById('cf-abbr-in');
  (window._cptAbbr||[]).forEach((a,i)=>{
    const s = document.createElement('span');
    s.className = 'cpt-abbr-tag';
    s.style.cssText = 'display:inline-flex;align-items:center;gap:4px;background:var(--blue-l);color:var(--blue-d);border-radius:10px;padding:2px 8px;font-size:11.5px;font-weight:600;';
    s.innerHTML = esc(a) + ' <a style="cursor:pointer;color:var(--mut);font-size:12px;" title="删除" onclick="cptAbbrRemove('+i+')">×</a>';
    box.insertBefore(s, inEl);
  });
  const hid = document.getElementById('cf-abbr');
  if(hid) hid.value = (window._cptAbbr||[]).join(';');
}
function cptAbbrKey(e){
  if(e.key==='Enter' || e.key===',' || e.key==='；'){ e.preventDefault();
    const v = e.target.value.trim(); if(!v) return;
    window._cptAbbr = window._cptAbbr||[];
    if(window._cptAbbr.includes(v)){ e.target.value=''; return; }
    window._cptAbbr.push(v); e.target.value='';
    _abbrRender(); window._cptFormDirty = true;
  }
}
function cptAbbrRemove(i){
  (window._cptAbbr||[]).splice(i,1);
  _abbrRender(); window._cptFormDirty = true;
}

// 必填标识随状态联动：弃用 → 替代概念转必填（红星）
function cptStatusChanged(){
  const s = document.getElementById('cf-status');
  const m = document.getElementById('cf-rep-req');
  if(m) m.innerHTML = (s && s.value==='deprecated') ? _reqC('状态为已弃用时必填——由它承接召回落词') : '';
}
