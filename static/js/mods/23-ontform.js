/* 本体表单：滑窗 / 属性绑定 / OWL 导入导出
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 12737-13271  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function renderOntEntProps(){
  const wrap = document.getElementById('ot-props-rows'); if(!wrap) return;
  const name = ((document.getElementById('ot-name')||{}).value || '').trim();
  const { own, inh, chain } = ontEntAttrSources(name);
  const t = ontTypeEditId ? (ontData.types||[]).find(x => String(x.id) === String(ontTypeEditId)) : null;
  const c = _asObj(t && t.constraints);
  const req = new Set(c.required||[]), uniq = new Set(c.unique||[]);
  const avMap = (!Array.isArray(c.allowed_values) && c.allowed_values) ? c.allowed_values : {};
  const legacy = t ? Object.keys(_asObj(t.properties)) : [];
  const badge = (txt,bg,fg)=>`<span style="flex:none;font-size:10px;padding:1px 6px;border-radius:8px;background:${bg};color:${fg};white-space:nowrap;">${txt}</span>`;
  const mkRow = (a, src)=>{
    const xsd = String(_asObj(a.constraints).xsd_type || 'string').replace(/^xsd:/,'');
    const al = avMap[a.name] || [];
    const inhFrom = src==='inherit' ? (ontEntParentChain(name).find(cn=>ontAttrDomains(a).includes(cn)) || '父类') : '';
    const body = src==='legacy'
      ? `<span style="flex:1;font-size:11px;color:var(--mut);">只读（历史自由行：定义未入「数据属性」，保存时原样保留）</span>`
      : `<span style="flex:1;"></span>
         <label style="display:inline-flex;align-items:center;gap:3px;font-size:11.5px;color:var(--mut);flex:none;cursor:pointer;"><input type="checkbox" class="er-req" data-name="${esc(a.name)}" ${req.has(a.name)?'checked':''}>必填</label>
         <label style="display:inline-flex;align-items:center;gap:3px;font-size:11.5px;color:var(--mut);flex:none;cursor:pointer;"><input type="checkbox" class="er-uniq" data-name="${esc(a.name)}" ${uniq.has(a.name)?'checked':''}>唯一</label>
         <input class="er-allowed" data-name="${esc(a.name)}" value="${esc(Array.isArray(al)?al.join(', '):'')}" placeholder="允许值，逗号分隔；留空=不约束" style="flex:2;min-width:110px;border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:11.5px;">`;
    return `<div class="ent-ref-row" style="display:flex;gap:6px;align-items:center;background:#fafafa;border:1px solid var(--line);border-radius:6px;padding:5px 8px;">
      <span style="width:100px;flex:none;font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(a.name)}">${esc(a.name)}</span>
      ${badge(esc(xsd),'#E6F1FB','#185FA5')}
      ${src==='own' ? badge(_entBindAdd.has(a.name)?'本类·待存':'本类','#E5F6E8','#1B7A2F')
                    : (src==='inherit' ? badge('继承 · '+esc(inhFrom),'#F2EDFB','#6B46B8') : badge('存量','#FDF3E3','#8A5A10'))}
      ${body}
      ${src==='own' ? `<button class="er-unbind" data-name="${esc(a.name)}" title="解绑：从该数据属性的 适用类型(domain) 移除本类" style="flex:none;background:transparent;border:none;color:var(--mut);cursor:pointer;font-size:13px;padding:0 2px;">✕</button>` : ''}
    </div>`;
  };
  const lines = [];
  own.forEach(a=>lines.push(mkRow(a,'own')));
  inh.forEach(a=>lines.push(mkRow(a,'inherit')));
  legacy.forEach(k=>lines.push(mkRow({name:k,constraints:{}},'legacy')));
  wrap.innerHTML = lines.length ? lines.join('')
    : '<div style="font-size:11.5px;color:var(--mut);padding:4px 2px;line-height:1.8;">暂无属性。行业标准（OWL/SHACL）：属性在「数据属性」全局定义、类只做引用——点 <b>⇄ 绑定已有</b> 挂接数据属性，或 <b>＋ 新建属性</b>（适用类型自动填本类）。</div>';
  wrap.querySelectorAll('.er-unbind').forEach(btn=>{
    btn.onclick = ()=>{
      const nm = btn.dataset.name;
      if(_entBindAdd.has(nm)) _entBindAdd.delete(nm);   // 会话内刚绑定的 → 直接撤销
      else _entBindDel.add(nm);                          // 已落库的 → 保存时解绑
      renderOntEntProps();
    };
  });
}
// ⇄ 绑定已有：候选=未挂到本类/父类链的数据属性（多选）
function ontEntBindExisting(){
  const nm = ((document.getElementById('ot-name')||{}).value || '').trim();
  if(!nm){ toast('请先填写类型名称，再绑定属性'); const i=document.getElementById('ot-name'); if(i) i.focus(); return; }
  const { own, inh } = ontEntAttrSources(nm);
  const have = new Set([...own, ...inh].map(a=>a.name));
  const cands = (ontData.types||[]).filter(t => t.type_kind==='attribute' && !have.has(t.name));
  if(!cands.length){ toast('没有可绑定的数据属性——请先切到「数据属性」维度新建属性类型'); return; }
  const lbx = document.getElementById('lbx'), body = document.getElementById('lbx-body');
  body.innerHTML = `<h3>⇄ 绑定已有数据属性</h3>
    <div style="font-size:12px;color:var(--mut);margin:4px 0 8px;line-height:1.6;">勾选要挂到「${esc(nm)}」的数据属性；保存类型时自动写入其 适用类型（rdfs:domain）。</div>
    <div style="display:flex;flex-direction:column;gap:4px;max-height:260px;overflow:auto;margin-bottom:4px;">
      ${cands.map(a=>`<label style="display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-size:12.5px;cursor:pointer;">
        <input type="checkbox" class="bd-chk" value="${esc(a.name)}">
        <b>${esc(a.name)}</b>
        <span style="color:var(--mut);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(String(_asObj(a.constraints).xsd_type||'string').replace(/^xsd:/,''))}${a.description?' · '+esc(String(a.description).slice(0,40)):''}</span>
      </label>`).join('')}
    </div>
    <div class="lbx-actions"><button class="btn ghost" id="bd-cancel">取消</button><button class="btn" id="bd-ok">确定</button></div>`;
  lbx.classList.add('show');
  document.getElementById('bd-cancel').onclick = ()=>lbx.classList.remove('show');
  document.getElementById('bd-ok').onclick = ()=>{
    body.querySelectorAll('.bd-chk').forEach(ch=>{
      if(ch.checked){ _entBindAdd.add(ch.value); _entBindDel.delete(ch.value); }
      else _entBindAdd.delete(ch.value);
    });
    lbx.classList.remove('show');
    renderOntEntProps();
  };
}
// ＋ 新建属性：切到属性类型表单，适用类型预填本类
async function ontEntCreateAttr(){
  const nm = ((document.getElementById('ot-name')||{}).value || '').trim();
  if(!nm){ toast('请先填写类型名称（属性的适用类型将默认填本类）'); const i=document.getElementById('ot-name'); if(i) i.focus(); return; }
  if(!(await confirmDialog('将切换到「新建属性类型」表单（适用类型预填「'+nm+'」）。\n当前类型表单未保存的内容将丢弃，是否继续？'))) return;
  const wasEdit = !!ontTypeEditId;
  ontTypeEditId = null; window._ontTypeEditId = null; window._ontTypeMode = 'add';
  _entBindAdd = new Set(); _entBindDel = new Set();
  document.getElementById('ot-kind').value = 'attribute';
  ontKindChanged();
  const ft = document.getElementById('ont-form-title'); if(ft) ft.textContent = ontSlideTitle('add','attribute');
  document.getElementById('ot-name').value = '';
  document.getElementById('ot-name-dup').textContent = '';
  document.getElementById('ot-iri').value = '';
  document.getElementById('ot-desc').value = '';
  document.getElementById('ot-dtype').value = 'string';
  document.getElementById('ot-attr-allowed').value = '';
  const el = document.getElementById('ot-attr-domain');
  if(el) Array.from(el.options).forEach(o=>{ o.selected = (o.value === nm); });
  const ni = document.getElementById('ot-name'); if(ni) ni.focus();
  toast('已切换「新建属性类型」，适用类型已预填「'+nm+'」' + (wasEdit ? '；该类型本身的修改请重新进入编辑' : ''));
}
// 保存类型成功后：把会话内的 绑定/解绑 落到数据属性（domain_classes），纯前端组合调用现有 PUT
async function _applyEntAttrBindings(entityName){
  if(!_entBindAdd.size && !_entBindDel.size) return;
  const putAttr = (a, ds)=>{
    const c = Object.assign({}, _asObj(a.constraints));
    if(ds.length) c.domain_classes = ds; else delete c.domain_classes;
    return api('/api/knowledge/ontology/types/'+a.id, {method:'PUT', body:JSON.stringify({
      name:a.name, type_kind:'attribute', properties:_asObj(a.properties), constraints:c,
      description:a.description||'', status:a.status||null,
      profile_source:a.profile_source||'', profile_ref:a.profile_ref||'',
      icon:a.icon||'', color:a.color||'#185FA5', parent_id:a.parent_id||null, iri:a.iri||''
    })});
  };
  const jobs = [];
  _entBindAdd.forEach(an=>{
    const a = (ontData.types||[]).find(x => x.name === an && x.type_kind === 'attribute'); if(!a) return;
    const ds = ontAttrDomains(a); if(!ds.includes(entityName)) ds.push(entityName);
    jobs.push(putAttr(a, ds));
  });
  _entBindDel.forEach(an=>{
    const a = (ontData.types||[]).find(x => x.name === an && x.type_kind === 'attribute'); if(!a) return;
    jobs.push(putAttr(a, ontAttrDomains(a).filter(d => d !== entityName)));
  });
  _entBindAdd = new Set(); _entBindDel = new Set();
  const rs = await Promise.all(jobs);
  const bad = rs.filter(r => r && r.error);
  if(bad.length) toast('⚠ 属性绑定更新失败 '+bad.length+' 项：'+bad[0].error);
}
function ontologyAddType() {
  ontTypeEditId = null;
  window._ontTypeMode = 'add';
  window._ontTypeEditId = null; window._ontTypeEditName = '';
  _entBindAdd = new Set(); _entBindDel = new Set();   // 方案A：会话内绑定态清零
  openOntSlide('add', 'entity');
  document.getElementById('ot-name').value = '';
  document.getElementById('ot-name-dup').textContent = '';
  document.getElementById('ot-iri').value = '';
  document.getElementById('ot-kind').value = 'entity';
  document.getElementById('ot-desc').value = '';
  // 2026-09-07：状态去空选项，新建默认 released
  document.getElementById('ot-status').value = 'released';
  document.getElementById('ot-unit').value = '';
  document.getElementById('ot-qkind').value = '';
  otUnitFormInit('', '');   // 2026-09-08 五轮优化：单位/量纲级联下拉重置
  document.getElementById('ot-dtype').value = 'string';
  document.getElementById('ot-attr-allowed').value = '';
  const domEl = document.getElementById('ot-attr-domain'); if(domEl) Array.from(domEl.options).forEach(o=>o.selected=false);
  document.getElementById('ot-abstract').checked = false;
  document.getElementById('ot-func').checked = false;
  document.querySelectorAll('.ot-char').forEach(c=>c.checked=false);
  document.getElementById('ot-profile-src').value = '';
  document.getElementById('ot-profile-ref').value = '';
  populateEntityTypeSelects();
  populateDisjointSelect(null);
  toggleOntKindFields('entity');   // 内部 renderOntEntProps() 渲染只读引用视图（新建=空态引导）
}
function ontologyEditType(id, name, kind, consStr) {
  ontTypeEditId = id;
  window._ontTypeMode = 'edit';
  window._ontTypeEditId = id; window._ontTypeEditName = name || '';
  _entBindAdd = new Set(); _entBindDel = new Set();   // 方案A：会话内绑定态清零
  openOntSlide('edit', kind || 'entity');
  kind = kind || 'entity';
  const cons = safeParse(consStr, {});
  const t = ontData.types.find(x=>x.id===id) || {};
  document.getElementById('ot-name').value = name || '';
  document.getElementById('ot-name-dup').textContent = '';
  document.getElementById('ot-iri').value = t.iri || '';
  document.getElementById('ot-kind').value = kind;
  // P1-10：描述合并为单字段（历史 constraints.desc 优先，保存后收敛到 description）
  document.getElementById('ot-desc').value = cons.desc || t.description || '';
  // 2026-09-07：状态去空选项，编辑回填 t.status（若无则默认 released）
  document.getElementById('ot-status').value = t.status || 'released';
  // P0-3 / P1-8：单位、数据类型、允许值与外部标准映射回填
  document.getElementById('ot-unit').value = cons.unit || '';
  document.getElementById('ot-qkind').value = cons.quantity_kind || '';
  document.getElementById('ot-dtype').value = (cons.xsd_type || '').replace(/^xsd:/, '') || 'string';
  document.getElementById('ot-attr-allowed').value = Array.isArray(cons.allowed_values) ? cons.allowed_values.join(', ') : '';
  const _aaEl = document.getElementById('ot-attr-allowed'); if(_aaEl) initEnumChips(_aaEl);   // 2026-09-13 枚举 chips
  // 2026-09-07：适用类型 domain_classes 回填（兼容旧单值字符串）
  const domArr = Array.isArray(cons.domain_classes) ? cons.domain_classes : (cons.domain_classes ? [cons.domain_classes] : []);
  const domElInit = document.getElementById('ot-attr-domain');
  if(domElInit) Array.from(domElInit.options).forEach(o => { o.selected = domArr.includes(o.value); });
  document.getElementById('ot-abstract').checked = !!cons.abstract;
  document.getElementById('ot-func').checked = cons.cardinality === '1';
  document.querySelectorAll('.ot-char').forEach(c=>{ c.checked = (cons.characteristics||[]).includes(c.value); });
  document.getElementById('ot-profile-src').value = t.profile_source || '';
  document.getElementById('ot-profile-ref').value = t.profile_ref || '';
  // 2026-09-07 方案A：#ot-props-rows 由 renderOntEntProps() 接管（实体=只读引用视图），
  // 不再用自由属性行预填；relation/attribute 下该 section 隐藏，无需填充。
  populateEntityTypeSelects();
  // 关系 domain/range 多选回填（数组=多域/多值域；兼容旧单值字符串）
  const av = cons.allowed_values || {};
  const dom = document.getElementById('ot-domain');
  const rng = document.getElementById('ot-range');
  const srcArr = Array.isArray(av.src) ? av.src : (av.src ? [av.src] : []);
  const tgtArr = Array.isArray(av.tgt) ? av.tgt : (av.tgt ? [av.tgt] : []);
  if(dom) Array.from(dom.options).forEach(o=>{ o.selected = srcArr.includes(o.value); });
  if(rng) Array.from(rng.options).forEach(o=>{ o.selected = tgtArr.includes(o.value); });
  // 关系端点基数（card_src/card_tgt）回填
  const _cs = cons.card_src || {}, _ct = cons.card_tgt || {};
  const _fillN = (id, val) => { const el = document.getElementById(id); if(el !== null) el.value = (val == null ? '' : String(val)); };
  _fillN('ot-card-src-min', _cs.min); _fillN('ot-card-src-max', _cs.max);
  _fillN('ot-card-tgt-min', _ct.min); _fillN('ot-card-tgt-max', _ct.max);
  // 实体：互斥多选回填
  populateDisjointSelect(name || '');
  const dj = document.getElementById('ot-disjoint');
  const djSet = new Set(cons.disjoint_with || []);
  if(dj) Array.from(dj.options).forEach(o=>{ o.selected = djSet.has(o.value); });
  toggleOntKindFields(kind);
  // 方案A（2026-09-07）：实体=只读引用视图；必填/唯一/白名单改由属性行内联回填，
  // 旧「约束规则」section 已隐藏，不再需要 ot-req/ot-uniq/ot-allowed-rows 回填。
  if(kind === 'entity'){ renderOntEntProps(); document.querySelectorAll('#ot-props-rows .er-allowed').forEach(el=>initEnumChips(el)); }   // 2026-09-13 枚举 chips
}
function toggleOntKindFields(kind) {
  // P1-10（2026-09-07）表单重构：按 kind 切分整 section——简化原逐 row 切换为 section 切换
  //   entity: 类型特征/属性/约束规则（section）；父类型
  //   relation: 关系特性（section）；父类型不适用
  //   attribute: 数据属性配置（section，含 适用类型/domain）；父属性
  const isRel = kind === 'relation', isEnt = kind === 'entity', isAttr = kind === 'attribute';
  const show = (id, on, disp) => { const el = document.getElementById(id); if(el) el.style.display = on ? (disp||'') : 'none'; };
  // 章节容器（按 kind 整体切显隐）
  show('ot-props-section', isEnt);
  show('ot-cons-section', false);   // 方案A：必填/唯一/白名单已内联到属性行，独立「约束规则」不再展示
  show('ot-relation-section', isRel);
  show('ot-attr-section', isAttr);
  // 类型特征章节内的"实体/属性"专属行
  show('ot-abstract-row', isEnt, 'block');
  show('ot-disjoint-row', isEnt, 'block');
  show('ot-parent-row', (isEnt || isAttr), 'block');
  const pl = document.getElementById('ot-parent-label');
  if(pl) pl.innerHTML = isAttr
    ? '父属性 <span class="info-tip" title="subPropertyOf（可选，子属性继承父属性的 xsd_type / 允许值 / domain）">ⓘ</span>'
    : '父类型 <span class="info-tip" title="subClassOf（可选，子类继承父类属性/必填/唯一/取值）">ⓘ</span>';
  if(isRel) populateEntityTypeSelects();
  if(isAttr) { populateEntityTypeSelects(); populateOntAttrDomainSelect(); }
  if(isEnt || isAttr) populateOntParentSelect(window._ontTypeEditId||null, window._ontTypeEditName||'', kind);
  if(isEnt) { renderOntEntProps(); populateDisjointSelect(); }
}
// 2026-09-07：数据属性「适用类型」多选候选 = 所有 entity 类型名（不排除自身——
// 自身是 attribute 不会出现在 entity 列表中，逻辑天然安全）
function populateOntAttrDomainSelect() {
  const el = document.getElementById('ot-attr-domain');
  if(!el) return;
  const editName = window._ontTypeEditName || '';
  const names = [...new Set((ontData.types||[])
    .filter(t => t.type_kind === 'entity' && t.name !== editName)
    .map(t => t.name))].sort();
  const prev = new Set(Array.from(el.selectedOptions).map(o => o.value));
  el.innerHTML = names.map(n => `<option value="${esc(n)}" ${prev.has(n) ? 'selected' : ''}>${esc(n)}</option>`).join('');
}
// P1-10：约束规则与属性行联动——必填/唯一多选候选、白名单按行生成
function refreshOntConsLinkage() {
  const keys = [];
  document.querySelectorAll('#ot-props-rows .prop-row').forEach(row=>{
    const k = row.querySelector('.pr-k'); if(k && k.value.trim()) keys.push(k.value.trim());
  });
  ['ot-req','ot-uniq'].forEach(id=>{
    const el = document.getElementById(id); if(!el) return;
    const sel = new Set(Array.from(el.selectedOptions).map(o=>o.value));
    el.innerHTML = keys.map(k=>`<option value="${esc(k)}" ${sel.has(k)?'selected':''}>${esc(k)}</option>`).join('');
  });
  const wrap = document.getElementById('ot-allowed-rows'); if(!wrap) return;
  const prev = {}; wrap.querySelectorAll('.ot-allowed-row').forEach(r=>{ prev[r.dataset.k] = r.querySelector('input').value; });
  wrap.innerHTML = keys.map(k=>`
    <div class="ot-allowed-row" data-k="${esc(k)}" style="display:flex;gap:6px;align-items:center;">
      <span style="width:90px;font-size:11.5px;color:var(--blue-d);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(k)}">${esc(k)}</span>
      <input value="${esc(prev[k]||'')}" placeholder="允许值，逗号分隔；留空=不约束" style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:11.5px;flex:1;">
    </div>`).join('');
}
// P1-10：互斥类型多选候选（排除自身）
function populateDisjointSelect(excludeName) {
  const el = document.getElementById('ot-disjoint'); if(!el) return;
  const names = [...new Set((ontData.types||[]).filter(t=>t.type_kind==='entity' && t.name!==excludeName).map(t=>t.name))];
  el.innerHTML = names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
}
// ── 2026-09-13 枚举 chips：把枚举输入增强为可增删的标签交互（回车/逗号添加，✕ 删除）──
function initEnumChips(input){
  if(!input || input.dataset.chipInit==='1') return; input.dataset.chipInit='1';
  // 建容器与内联输入，替换原文本框的可见性（原 input 保留为数据承载，兼容旧读取）
  const vals = (input.value||'').split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  input.dataset.chips = JSON.stringify(vals);
  const wrap = document.createElement('div');
  wrap.className = 'enum-chips';
  wrap.style.cssText='display:flex;flex-wrap:wrap;gap:4px;align-items:center;border:1px solid var(--line);border-radius:5px;padding:2px 4px;background:#fff;min-height:24px;box-sizing:border-box;';
  input.parentNode.insertBefore(wrap, input);
  input.style.display='none';
  const inp = document.createElement('input'); inp.type='text'; inp.placeholder='输入后回车/逗号添加';
  inp.style.cssText='border:none;outline:none;flex:1;min-width:70px;font-size:11.5px;background:transparent;box-sizing:border-box;';
  wrap.appendChild(inp);
  const render = () => {
    wrap.querySelectorAll('.e-chip').forEach(x=>x.remove());
    let arr=[]; try{ arr=JSON.parse(input.dataset.chips||'[]'); }catch(e){ arr=[]; }
    if(!Array.isArray(arr)) arr=[];
    arr.forEach(v=>{
      const c=document.createElement('span'); c.className='e-chip'; c.textContent=v; c.title=v;
      c.style.cssText='display:inline-flex;align-items:center;gap:2px;background:var(--blue-l);color:var(--blue-d);border-radius:999px;padding:1px 6px;font-size:11px;';
      const x=document.createElement('i'); x.textContent='✕'; x.style.cssText='cursor:pointer;font-style:normal;opacity:.75;padding-left:2px;';
      x.onclick=()=>{ input.dataset.chips = JSON.stringify(arr.filter(a=>a!==v)); render(); };
      c.appendChild(x); wrap.insertBefore(c, inp);
    });
  };
  const add = () => {
    const v = inp.value.trim(); if(!v) return;
    let arr=[]; try{ arr=JSON.parse(input.dataset.chips||'[]'); }catch(e){ arr=[]; }
    if(!arr.includes(v)) arr.push(v);
    input.dataset.chips = JSON.stringify(arr); inp.value=''; render();
  };
  inp.addEventListener('keydown', e=>{ if(e.key==='Enter'||e.key===','){ e.preventDefault(); add(); } });
  inp.addEventListener('blur', add);
  render();
}
function collectEnumChips(input){
  if(!input) return [];
  if(input.dataset.chips !== undefined && input.dataset.chips !== ''){
    try{ const a = JSON.parse(input.dataset.chips); return Array.isArray(a)?a.filter(Boolean):[]; }catch(e){}
  }
  // 未初始化 chips（如新建表单）回退读原始 value（逗号分隔）
  return (input.value||'').split(/[,，]/).map(s=>s.trim()).filter(Boolean);
}
function getSelectValues(id) {
  const el = document.getElementById(id);
  return el ? Array.from(el.selectedOptions).map(o=>o.value).filter(Boolean) : [];
}
function getSelectValue(id) {
  const sel = document.getElementById(id);
  return sel && sel.value ? sel.value : '';
}
// ── 滑窗「单位/量纲」级联下拉（与中栏 ontUnitEditor 共用 ONT_QK_UNITS 标准）──
function otUnitFormInit(unit, qkind){
  const kSel=document.getElementById('ot-qkind'); if(!kSel) return;
  kSel.innerHTML='<option value="">— 量纲类别（ISO 80000/SI）—</option>'
    +Object.keys(ONT_QK_UNITS).map(k=>`<option value="${k}">${k}（SI: ${ONT_QK_UNITS[k].si}）</option>`).join('')
    +'<option value="__custom">自定义…</option>';
  const kc=document.getElementById('ot-qkind-custom'), uc=document.getElementById('ot-unit-custom');
  kc.value=''; uc.value='';
  if(qkind){ if(ONT_QK_UNITS[qkind]) kSel.value=qkind; else { kSel.value='__custom'; kc.value=qkind; } }
  else kSel.value='';
  otUnitSyncQkind(unit||'');
}
// 量纲变更 → 单位下拉级联重填；标准量纲=级联备选（可自定义），自定义/未选=单位自由填写
function otUnitSyncQkind(preUnit){
  const kSel=document.getElementById('ot-qkind'); if(!kSel) return;
  const kc=document.getElementById('ot-qkind-custom'), uSel=document.getElementById('ot-unit'), uc=document.getElementById('ot-unit-custom');
  if(!uSel) return;
  kc.style.display = kSel.value==='__custom' ? '' : 'none';
  if(kSel.value && kSel.value!=='__custom'){
    const def=ONT_QK_UNITS[kSel.value];
    uSel.disabled=false;
    uSel.innerHTML='<option value="">— 选择单位 —</option>'
      +def.units.map(u=>`<option value="${u}">${u}${u===def.si?'（SI）':''}</option>`).join('')
      +'<option value="__custom">自定义…</option>';
    uc.value=(preUnit && !def.units.includes(preUnit)) ? preUnit : '';
    uc.style.display = uc.value ? '' : 'none';
    uSel.value = (preUnit && def.units.includes(preUnit)) ? preUnit : '';
    if(uc.value) uSel.value='__custom';
  } else {
    uSel.disabled=true; uSel.innerHTML='<option value="">— 自由填写单位 →</option>';
    uc.style.display='';
    uc.value=preUnit||'';
    uc.placeholder = kSel.value==='__custom' ? '自定义单位（量纲可含在左侧输入）' : '自定义单位，如 nT';
  }
  if(!uSel._hookCustom){
    uSel._hookCustom=true;
    uSel.addEventListener('change', ()=>{ uc.style.display = uSel.value==='__custom' ? '' : 'none'; if(uSel.value!=='__custom') uc.value=''; });
  }
}
async function saveOntType() {
  const name = document.getElementById('ot-name').value.trim();
  const oldT = ontTypeEditId ? (ontData.types||[]).find(x=>String(x.id)===String(ontTypeEditId)) : null;
  const oldName = oldT ? oldT.name : name;  // R3：改名联动基线
  if(!name) { toast('类型名称必填'); return; }
  const dup = (ontData.types||[]).find(t => t.name === name && t.id !== ontTypeEditId);
  if(dup) { toast('⚠ 已存在同名类型：' + name); return; }
  const kind = document.getElementById('ot-kind').value;
  // 方案A（2026-09-07）：实体类型的 properties 不再由自由属性行写入——属性=数据属性的全局定义（事实源），
  // 类视图只做引用。编辑时原样保留存量 properties（不丢历史数据），新建为空对象。
  const props = kind === 'entity'
    ? (ontTypeEditId ? _asObj(((ontData.types||[]).find(x=>String(x.id)===String(ontTypeEditId))||{}).properties) : {})
    : {};
  // 2026-09-08 P0 兜底（编辑整合方案）：以旧 constraints 为基底（kind 不变时），保留表单未覆盖的键
  //（equivalent_to / axioms 等中栏维护字段）——修复"滑窗保存即清空中栏等价类/公理"；kind 变更时全新构建防格式残留
  const sameKind = !!oldT && oldT.type_kind === kind;
  const constraints = sameKind ? (JSON.parse(JSON.stringify(_asObj(oldT.constraints)))) : {};
  if(kind === 'relation') {
    // P1-10：多域/多值域（列表=SHACL sh:or 并集语义，写路径后端已归一）
    // 2026-09-08：基底 clone 语义下空值必须显式删除（整单替换→"设置或删除"）
    const src = getSelectValues('ot-domain'), tgt = getSelectValues('ot-range');
    if(src.length || tgt.length) constraints.allowed_values = {src, tgt};
    else delete constraints.allowed_values;
    const chars = Array.from(document.querySelectorAll('.ot-char:checked')).map(c=>c.value);
    if(chars.length) constraints.characteristics = chars;
    else delete constraints.characteristics;
    if(document.getElementById('ot-func').checked) constraints.cardinality = '1';
    else delete constraints.cardinality;
    // 端点基数（源/目标 min/max，写入 SHACL 校验用的 card_src/card_tgt）
    const _intOrNull0 = id => { const el=document.getElementById(id); const v=el?el.value:''; if(v==='') return null; const n=Number(v); return Number.isFinite(n)?n:null; };
    const _cs2 = {min:_intOrNull0('ot-card-src-min'), max:_intOrNull0('ot-card-src-max')};
    const _ct2 = {min:_intOrNull0('ot-card-tgt-min'), max:_intOrNull0('ot-card-tgt-max')};
    if(_cs2.min!=null || _cs2.max!=null) constraints.card_src = _cs2; else delete constraints.card_src;
    if(_ct2.min!=null || _ct2.max!=null) constraints.card_tgt = _ct2; else delete constraints.card_tgt;
  } else if(kind === 'attribute') {
    // P1-10：数据属性——数据类型 / 允许值 plain list / 单位量纲 / 适用类型（OWL rdfs:domain）
    constraints.xsd_type = document.getElementById('ot-dtype').value;
    const _aaEl1 = document.getElementById('ot-attr-allowed'); const av = collectEnumChips(_aaEl1);
    if(av.length) constraints.allowed_values = av;
    else if(!Array.isArray(constraints.allowed_values)) delete constraints.allowed_values;  // 2026-09-08：仅覆盖数组型，防误删对象型（历史兼容）
    // 2026-09-07：适用类型多选 → constraints.domain_classes（OWL rdfs:domain，合取语义）
    const domClasses = getSelectValues('ot-attr-domain');
    if(domClasses.length) constraints.domain_classes = domClasses; else delete constraints.domain_classes;
    // 2026-09-08 五轮优化：单位/量纲读自级联下拉（含自定义输入回退）
    const _kSel=document.getElementById('ot-qkind'), _kc=document.getElementById('ot-qkind-custom');
    const _uSel=document.getElementById('ot-unit'), _uc=document.getElementById('ot-unit-custom');
    const qkind = _kSel.value==='__custom' ? _kc.value.trim() : _kSel.value;
    const unit = (_uSel.disabled || _uSel.value==='__custom') ? _uc.value.trim() : _uSel.value;
    if(unit) constraints.unit = unit; else delete constraints.unit;
    if(qkind) constraints.quantity_kind = qkind; else delete constraints.quantity_kind;
  } else {
    // 实体（方案A）：局部约束覆盖来自「属性（引用）」行内勾选；抽象/互斥不变
    const reqSet = new Set(), uniqSet = new Set(), avMap = {};
    document.querySelectorAll('#ot-props-rows .er-req:checked').forEach(c=>reqSet.add(c.dataset.name));
    document.querySelectorAll('#ot-props-rows .er-uniq:checked').forEach(c=>uniqSet.add(c.dataset.name));
    document.querySelectorAll('#ot-props-rows .er-allowed').forEach(inp=>{
      const v = collectEnumChips(inp);
      if(v.length) avMap[inp.dataset.name] = v;
    });
    if(reqSet.size) constraints.required = [...reqSet]; else delete constraints.required;
    if(uniqSet.size) constraints.unique = [...uniqSet]; else delete constraints.unique;
    if(Object.keys(avMap).length) constraints.allowed_values = avMap;
    else if(!Array.isArray(constraints.allowed_values)) delete constraints.allowed_values;  // 2026-09-08：仅覆盖对象型，防误删数组型
    if(document.getElementById('ot-abstract').checked) constraints.abstract = true; else delete constraints.abstract;
    const dj = getSelectValues('ot-disjoint');
    if(dj.length) constraints.disjoint_with = dj; else delete constraints.disjoint_with;
  }
  const body = {
    name, type_kind: kind,
    properties: props,
    constraints,
    // P1-10：描述合并为单字段（历史 constraints.desc 不再写入）
    description: document.getElementById('ot-desc').value.trim(),
    // P1-10：生命周期（空=编辑保持不变/新建默认 released）
    status: document.getElementById('ot-status').value || null,
    // P1-8：外部标准映射（profile_source/profile_ref 列激活）
    profile_source: document.getElementById('ot-profile-src').value.trim(),
    profile_ref: document.getElementById('ot-profile-ref').value.trim(),
    // Icon/颜色表单字段已移除：编辑保留存量原值，新建用默认
    icon: (ontTypeEditId ? ((ontData.types||[]).find(x=>String(x.id)===String(ontTypeEditId))||{}).icon : '') || '',
    // P1 颜色自动适配：编辑保留存量原值，新建按名称 hash 取调色板色
    color: (ontTypeEditId ? ((ontData.types||[]).find(x=>String(x.id)===String(ontTypeEditId))||{}).color : '') || ontAutoColor(name),
    // P0-1/P1-10：父类型（entity=subClassOf，attribute=subPropertyOf；关系不适用）
    parent_id: kind !== 'relation'
      ? ((ontData.types||[]).find(t=>t.name===getSelectValue('ot-parent') && t.type_kind===kind)?.id ?? null)
      : null,
    // P0-1：IRI（留空=后端按策略自动生成；改名联动由后端判断）
    iri: document.getElementById('ot-iri').value.trim(),
    // 2026-09-08 二轮审计：replaced_by/deprecated_note 表单无输入项，编辑路径须从旧值透传（否则 PUT 清空）
    replaced_by: oldT ? (oldT.replaced_by||'') : '',
    deprecated_note: oldT ? (oldT.deprecated_note||'') : '',
  };
  try {
    if(ontTypeEditId) {
      // 2026-09-14 保存前实例影响预览（只读）：徽章写入抽屉；红级（改名有实例/存量违例）二次确认
      const _ibox = document.getElementById('ot-impact-box');
      if(_ibox) _ibox.style.display = 'none';
      let pv = null;
      try { pv = await api('/api/knowledge/ontology/impact-preview', {method:'POST', body:JSON.stringify({tid: ontTypeEditId, ...body})}); }
      catch(e) { console.warn('[impact-preview]', e); }
      if(pv && !pv.error) {
        renderOntImpactPreview(pv);
        if(pv.severity === 'red' && !(await confirmOntImpact(pv))) return;
      }
      const r = await api(`/api/knowledge/ontology/types/${ontTypeEditId}`, {method:'PUT', body:JSON.stringify(body)});
      if(r.error) { toast('保存失败：'+r.error); return; }
      // 2026-09-07：先落 UI 再提示——避免提示环节异常导致面板不关/列表不刷新（表现为"保存无效"）
      closeOntSlide(); ontTypeEditId = null; window._ontTypeEditId = null;
      try{ await _applyEntAttrBindings(name); }catch(e){ console.error('[ent-bind]', e); }
      try{ loadOntology(); }catch(e){}
      // 2026-09-14：L1 内联迁移结果随保存提示（改名 → 实例/关系/属性键自动跟随）
      const _mig = [];
      if(r.migrated_instances) _mig.push(`实体×${r.migrated_instances}`);
      if(r.migrated_relations) _mig.push(`关系×${r.migrated_relations}`);
      if(r.migrated_prop_keys) _mig.push(`属性键×${r.migrated_prop_keys}`);
      toast('✅ 类型已更新' + (_mig.length ? `（实例已跟随迁移：${_mig.join(' · ')}）` : ''));
    // R3 词法层同步：类型改名 → 迁移指向旧名的词典词条（逻辑抽公共函数，与中栏名称行内编辑共用）
    if(oldT && oldName !== name){
      try{ await ontMaybeMigrateGlossary(oldName, name); }catch(e){}
    }
    } else {
      const r = await api('/api/knowledge/ontology/types', {method:'POST', body:JSON.stringify(body)});
      if(r.error) { toast('新增失败：'+r.error); return; }
      // 2026-09-07：同上，先落 UI 再提示
      closeOntSlide(); ontTypeEditId = null; window._ontTypeEditId = null;
      try{ await _applyEntAttrBindings(name); }catch(e){ console.error('[ent-bind]', e); }
      try{ loadOntology(); }catch(e){}
      toast('✅ 类型已新增');
    }
  } catch(e) {
    console.error('[saveOntType]', e);
    toast('保存失败：' + (e && e.message ? e.message : '未知错误'));
  }
}
// 2026-09-14 影响预览徽章（保存前写入抽屉 #ot-impact-box）：红=破坏性(改名有实例) / 黄=存量违例 / 绿=无影响
function renderOntImpactPreview(pv) {
  const box = document.getElementById('ot-impact-box');
  if(!box) return;
  const _c = {red:['var(--red)','var(--red-l)','🔴 破坏性'],yellow:['var(--amb)','var(--amb-l)','🟡 需校验'],green:['var(--grn)','var(--grn-l)','🟢 无影响']}[pv.severity]||['var(--mut)','transparent','−'];
  const _items = (pv.violations||[]).slice(0,8).map(v=>`<div style="font-size:11px;color:var(--mut);padding:1px 0;">· [${esc(v.kind)}] ${esc(v.detail||'')}</div>`).join('');
  const _more = (pv.violation_count||0) > 8 ? `<div style="font-size:11px;color:var(--mut);">…共 ${pv.violation_count} 条违例</div>` : '';
  const _rn = pv.rename ? `<div style="font-size:11.5px;">改名「${esc(pv.rename.from)}」→「${esc(pv.rename.to)}」` +
    (pv.auto_fix ? `：保存时<b>自动迁移</b> ${pv.rename.entities||pv.rename.relations||pv.rename.prop_keys||0} 条实例（同事务，可回滚）` : `涉及存量数据`) + `</div>` : '';
  box.innerHTML = `<div style="border:1px solid ${_c[0]};background:${_c[1]};border-radius:8px;padding:8px 10px;margin-top:8px;line-height:1.6;">
    <div style="font-size:12px;font-weight:500;">${_c[2]} · 实例影响预览</div>
    ${_rn}
    ${pv.violation_count ? `<div style="font-size:11.5px;">新约束下 <b>${pv.violation_count}</b> 条存量违例需人工裁决（保存后发布时生成迁移计划）</div>` : ''}
    ${_items}${_more}
    <div style="font-size:10.5px;color:var(--mut);margin-top:2px;">${esc(pv.summary||'')}</div>
  </div>`;
  box.style.display = '';
}
async function confirmOntImpact(pv) {
  const _mv = pv.rename ? (pv.rename.entities||pv.rename.relations||pv.rename.prop_keys||0) : 0;
  const _msg = pv.violation_count
    ? `本次修改将影响存量数据：改名迁移 ${_mv} 条，且新约束下存在 ${pv.violation_count} 条存量违例（发布时会生成迁移计划供人工裁决）。\n\n确认保存？`
    : `改名将自动迁移 ${_mv} 条实例（与保存同事务原子提交，改名后旧名不再存在）。\n\n确认保存？`;
  return await confirmDialog(_msg, {title:'⚠️ 本体变更影响确认'});
}
function safeParse(s, d) { try { return JSON.parse(s||'{}')||d; } catch(e) { return d; } }
// O-2：本体 ↔ OWL/Profile 导入导出（导入下拉 4 项 · 导出下拉 4 项）
async function doOntExport(fmt) {
  // 方案 A3：导出前一致性预检——高危问题明示（导出文件内也有行内 WARNING 注释）
  try{
    const v = await api('/api/knowledge/ontology/validate');
    if(v && !v.error && (v.high||0) > 0){
      const hi = (v.issues||[]).filter(x=>x.severity==='high').slice(0,2).map(x=>x.message).join('；');
      toast('⚠ 导出含 '+v.high+' 项高危数据问题（文件内有 WARNING 注释）：'+hi, 6000);
    }
  }catch(e){}
  let url, msg;
  if(fmt === 'ttl'){ url='/api/knowledge/ontology/export?fmt=ttl'; msg='OWL/Turtle 已导出（ontology.ttl）'; }
  else if(fmt === 'owl'){ url='/api/knowledge/ontology/export'; msg='OWL/RDF-XML 已导出（ontology.owl）'; }
  else { url='/api/knowledge/ontology/export'; msg='导出完成'; }
  window.open(url, '_blank');
  toast(msg);
}
// fmt: owl | ttl | profile1x | profile2x
async function ontologyImport(fmt) {
  fmt = fmt || 'owl';
  // SysML Profile：跳转本体蓝图模式（草案审核，不直接入库）
  if(fmt === 'profile1x' || fmt === 'profile2x'){
    openBlueprint();
    bpSetSource('profile');
    var sel = document.getElementById('bp-fmt');
    if(sel) sel.value = (fmt === 'profile2x' ? 'v2' : '1x');
    var isV2 = fmt === 'profile2x';
    var bpAccept = isV2 ? '.kerml,text/plain' : '.profile,.xmi,.uml,.xml';
    var bpFileName = isV2 ? 'sysml-v2.kerml' : 'sysml-v1.profile';
    window.__bpPendingProfileAccept = bpAccept;
    window.__bpPendingProfileFileName = bpFileName;
    setTimeout(function(){
      var lbl = document.getElementById('bp-input-label');
      if(lbl) lbl.textContent = (isV2 ? 'SysML 2.x KerML 文本（.kerml）' : 'SysML 1.x XMI / Profile 文本（.profile/.xmi/.uml）');
      var ta = document.getElementById('bp-text');
      if(ta) ta.placeholder = (isV2 ? '粘贴 SysML 2.x KerML 文本，或点击「📤 上传文件」选择 .kerml…' : '粘贴 SysML 1.x XMI 文本，或点击「📤 上传文件」选择 .profile/.xmi/.uml…');
      var fi = document.getElementById('bp-file');
      if(fi){ fi.accept = bpAccept; fi.title = (fi.title ? fi.title.split('；')[0]+'；当前模式：' : '默认：')+bpAccept.replace(/,/g,' / '); fi.setAttribute('data-default-name', bpFileName); }
    }, 60);
    toast('请在本体蓝图中粘贴/上传 SysML Profile → 提取草案 → 勾选后应用');
    return;
  }
  // 2026-09-02 优化：OWL 导入改为统一对话框（粘贴与上传并存，不再用「取消=粘贴」反直觉二选一）
  _oiFmt = fmt;
  showModal('owlImport');
  const isTtl = fmt === 'ttl';
  const title = document.getElementById('oi-title');
  if(title) title.textContent = isTtl ? '⬆ 导入 OWL · Turtle' : '⬆ 导入 OWL · RDF/XML';
  const tip = document.getElementById('oi-tip');
  if(tip) tip.textContent = isTtl
    ? '粘贴 OWL/Turtle（.ttl）内容（同名类型跳过幂等），或点击下方「📤 上传文件」载入 .ttl'
    : '粘贴 OWL/RDF-XML 内容（同名类型跳过幂等），或点击下方「📤 上传文件」载入 .owl/.rdf/.xml';
  const ta = document.getElementById('oi-text');
  if(ta) ta.placeholder = isTtl
    ? '@prefix : <http://example.org/ontology#> .\n:Foo a owl:Class ; rdfs:label "Foo" .'
    : '<rdf:RDF xmlns:owl="http://www.w3.org/2002/07/owl#" ...>';
  const fi = document.getElementById('oi-file');
  if(fi) fi.accept = isTtl ? '.ttl,text/turtle' : '.owl,.rdf,.xml,application/rdf+xml';
  const fn = document.getElementById('oi-file-name');
  if(fn) fn.textContent = '';
  const t2 = document.getElementById('oi-text');
  if(t2) t2.value = '';
}
let _oiFmt = 'owl';   // 当前导入格式（owl/ttl），供 oiDoImport 使用
// 上传文件 → 载入输入框（与粘贴共用同一条导入流程）
function oiFileChosen(input){
  const f = input.files && input.files[0];
  const nm = document.getElementById('oi-file-name');
  if(!f){ if(nm) nm.textContent=''; return; }
  if(f.size > 5*1024*1024){ toast('文件超过 5MB，请截取关键片段后粘贴'); input.value=''; if(nm) nm.textContent=''; return; }
  const rd = new FileReader();
  rd.onload = e => {
    const ta = document.getElementById('oi-text');
    if(ta) ta.value = String(e.target.result||'');
    if(nm) nm.textContent = `已载入 ${f.name}（${(f.size/1024).toFixed(1)} KB），点击「⬆ 开始导入」`;
    toast('已载入 ' + f.name);
  };
  rd.onerror = () => { toast('文件读取失败，请重试'); input.value=''; };
  rd.readAsText(f, 'utf-8');
}
async function oiDoImport(){
  const ta = document.getElementById('oi-text');
  const content = (ta && ta.value.trim()) || '';
  if(!content){ toast('请先粘贴内容或上传文件'); return; }
  try{
    const r = await api('/api/knowledge/ontology/import', {method:'POST', body:JSON.stringify({fmt: _oiFmt||'owl', content})});
    if(r.error){ toast('导入失败：'+r.error); return; }
    toast(`✅ 导入 ${r.imported} / 跳过 ${r.skipped}${(r.errors&&r.errors.length)?' · ⚠️ '+r.errors.length+' 提示':''}`);
    closeModal();
    loadOntology();
  }catch(e){ toast('导入失败：'+e.message); }
}
/* KB-P2: 力导向图可视化（零依赖 SVG 实现，拖拽/缩放/平移 + 审核工作流） */
let graphState = {nodes:[], edges:[], sel:null, svgW:880, svgH:460,
  vp:{scale:1, panX:0, panY:0},
  view:{status:'all', hideIsolated:false, types:[], layout:'force', scope:'all', docs:[]}, all:{nodes:[], edges:[]},   // 2026-09-12 默认力导向
  sk:{on:false, drill:null, focus:null}, _skTrunc:0};   // sk：骨架模式（L1 类型聚合 / drill=类型名 L2 实例束 / focus=实例id L3 邻域）
