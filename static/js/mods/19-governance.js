/* 治理：闸口 / 发布 / 去重合并
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 8701-8989  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function openGovPanel(){
  openPanel('🛡 知识治理健康度', '<div class="loading">指标计算中…</div>');
  const r = await api('/api/governance/metrics?run_shacl=true');
  if(r.error){ document.getElementById('panel-body').innerHTML = `<div class="fempty">加载失败：${esc(r.error)}</div>`; return; }
  const color = {ok:['#2F855A','#E8F5E9'], warn:['#B7791F','#FFF7E0'], alert:['#C53030','#FDE8E8']};
  // P0 反馈（2026-09-09）：每项指标补数据来源口径说明（悬停 ⓘ 查看），消除"一头雾水"
  const srcMap = {
    shacl_violations:   '来源：SHACL 门禁校验结果表（shacl_findings，近 7 天）。本体约束（必填/取值/domain-range）违规在入库闸门记录。',
    trace_coverage:     '来源：查询链路表（query_trace）。有 Trace 记录的查询 / 总查询数——低说明多数查询未走归一化管线。',
    orphan_rate:        '来源：relations 表。一端实体已删除/不存在的关系占比——高说明关系完整性有问题。',
    mapping_coverage:   '来源：词典概念（glossary_concepts）的本体映射字段（maps_to_class/prop/inst 非空占比）。低 = 概念层刚建立，属预期。',
    pending_candidates: '来源：图谱候选（v2g_candidates）中 status=pending 的数量——审核积压指示。',
    low_confidence:     '来源：三元组（triples）confidence<0.7 的占比。',
    deprecated_residue: '来源：已弃用概念（deprecated）的术语仍被图库实体/关系引用的数量——需要迁移到替代概念。',
    homonym_terms:      '来源：同一术语名指向多个概念（glossary_terms 自连接判定）——消歧待处理。',
    mirror_sync:        '来源：triples 中 graph_stored 标记与图库实体/关系的水位差——三元组审核通过后未落图的部分。',
  };
  const label = {ok:'正常', warn:'预警', alert:'告警'};
  const cards = r.items.map(i=>{
    const c = color[i.status]||color.warn;
    return `<div style="border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fff;">
      <div style="display:flex;align-items:center;gap:6px;">
        <b style="font-size:12px;">${esc(i.name)}</b>
        <span class="info-tip" title="${esc(srcMap[i.key] || '来源：治理指标实时计算（governance.compute_metrics）')}" style="cursor:help;color:var(--blue);font-size:10px;">ⓘ</span>
        <span style="flex:1"></span>
        <span style="font-size:10px;padding:1px 8px;border-radius:8px;background:${c[1]};color:${c[0]};font-weight:600;">${label[i.status]||i.status}</span>
      </div>
      <div style="margin-top:4px;font-size:20px;font-weight:700;color:var(--blue-d);">${i.value!==null&&i.value!==undefined?i.value:'—'}<span style="font-size:11px;color:var(--mut);font-weight:400;"> ${i.unit||''}</span></div>
      <div style="font-size:10.5px;color:var(--mut);">阈值: ${esc(i.threshold||'-')}${i.detail?` · ${esc(i.detail)}`:''}</div>
    </div>`;}).join('');
  const sm = r.summary||{};
  const gm = r.gate_mode||'warn';
  document.getElementById('panel-body').innerHTML = `
    <div style="font-size:12.5px;line-height:1.7;">
      <div style="margin-bottom:10px;padding:8px 12px;background:var(--blue-l);border-radius:8px;font-size:11.5px;color:var(--blue-d);line-height:1.8;">
        <b>本页是什么：</b>知识资产的健康度体检——9 项指标全部<b style="text-decoration:underline;">实时计算自系统数据库</b>（不是外部数据）：词典概念表、图库实体/关系表、三元组表、查询链路表、SHACL 校验结果表。
        每张卡悬停 ⓘ 可看该指标的具体数据来源与口径。阈值触发 预警/告警 状态；SHACL 门禁模式决定违规是仅记录还是阻断入库。
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
        <span class="tag">正常 ${sm.ok||0} · 预警 ${sm.warn||0} · 告警 ${sm.alert||0}</span>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:var(--mut);">SHACL 门禁</span>
        <select id="gov-gate-mode" onchange="govSetGateMode(this.value)" style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:12px;">
          ${['off','warn','enforce'].map(m=>`<option value="${m}" ${m===gm?'selected':''}>${m==='off'?'off 关闭':m==='warn'?'warn 记录':'enforce 阻断'}</option>`).join('')}
        </select>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">${cards}</div>
      <div style="margin-top:12px;font-size:11px;color:var(--mut);">生成时间 ${esc(r.generated_at||'')} · 追溯覆盖度/映射覆盖率低 = 概念层刚上线，属预期；孤立率高 = 检查导入的关系完整性。</div>
      <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn sm" onclick="govReleaseModal()">🏷 本体发布快照</button>
        <button class="btn sm ghost" onclick="govShaclCheck()">🔍 SHACL 校验</button>
        <button class="btn sm ghost" onclick="openGovPanel()">🔄 刷新</button>
      </div>
    </div>`;
}
async function govSetGateMode(mode){
  const r = await api('/api/governance/gate-mode', {method:'PUT', body:JSON.stringify({mode})});
  if(r.error){ toast('失败：'+r.error); return; }
  toast(`✅ 门禁模式 = ${mode}${mode==='enforce'?'（违规将阻断审批）':''}`);
}
// ── 2026-09-13 SHACL 全量数据校验分析界面（复用后端 /api/governance/shacl-check）──
async function govShaclCheck(){
  const panel = document.getElementById('panel-body'); if(!panel) return;
  const old = document.getElementById('gov-shacl-r'); if(old) old.remove();
  const wrap = document.createElement('div'); wrap.id='gov-shacl-r';
  wrap.style.cssText='margin-top:12px;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff;';
  panel.appendChild(wrap);
  wrap.innerHTML = '<div class="loading" style="padding:20px;text-align:center;">SHACL 全量校验中（含必填/取值/domain-range/基数）…</div>';
  const r = await api('/api/governance/shacl-check', {method:'POST', body:'{}'});   // 该端点为 POST
  if(r.error){ wrap.innerHTML = `<div style="padding:16px;color:#C53030;">校验失败：${esc(r.error)}</div>`; return; }
  const okC = r.conforms === true;
  const vs = r.violations || [];
  const dist = r.distribution || [];
  const dl = '/api/knowledge/ontology/shacl';
  wrap.innerHTML = `
    <div style="padding:10px 12px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--line);background:${okC?'#E8F5E9':'#FDE8E8'};">
      <b style="font-size:13px;color:${okC?'#2F855A':'#A31F1F'};">${okC?'✅ 数据符合本体约束':'⚠ 发现 '+esc(String(r.violation_n||vs.length))+' 条 SHACL 违规'}</b>
      <span style="flex:1"></span>
      <a class="btn sm ghost" href="${dl}" target="_blank" download="shacl-shapes.ttl">⤓ 导出 SHACL shapes (Turtle)</a>
      <button class="btn sm" onclick="govShaclCheck()">🔄 重新校验</button>
      <button class="btn sm ghost" onclick="govShaclClose()">✕</button>
    </div>
    <div style="padding:8px 12px;font-size:11px;color:var(--mut);border-bottom:1px solid var(--line);">
      门禁模式 <b>${esc(r.mode||'-')}</b>
      ${r.skipped ? (' · 校验已跳过（'+esc(r.reason||'')+'）') : (' · 检查 <b>'+(r.total_checked!=null?r.total_checked:0)+'</b> 三元组 · 违规 <b>'+(r.violation_n||0)+'</b>')}
    </div>
    ${dist.length?`
    <div style="padding:8px 12px;border-bottom:1px solid var(--line);">
      <b style="font-size:11px;">违规分布（Top）</b>
      <div style="margin-top:4px;">${dist.map(([k,n])=>`<div style="font-size:11px;color:#444;line-height:1.7;"><span style="color:var(--mut);">${esc(String(n))}×</span>　${esc(k)}</div>`).join('')}</div>
    </div>`:''}
    <div style="padding:8px 12px;max-height:300px;overflow:auto;">
      <b style="font-size:11px;">违规明细</b>
      ${vs.length ? vs.map(v=>`<div style="margin-top:6px;padding:6px 8px;background:#FFF9FB;border:1px solid #F3D0D0;border-radius:6px;font-size:11px;line-height:1.6;">
        <div><b style="color:#A31F1F;">${esc(v.subject||'-')}</b> <span style="color:var(--mut);">· ${esc(v.severity||'Violation')}</span></div>
        <div style="color:#5a3b3b;">${esc(v.message||'')}</div>
        <div style="color:var(--mut);font-size:10px;">path: ${esc(v.path||'-')}</div>
        ${govIsEntitySubject(v.subject) ? `<div style="margin-top:5px;text-align:right;"><button class="btn sm ghost" style="color:var(--blue-d);padding:1px 8px;" onclick="govOpenFix('${esc(v.subject)}')">📌 定位修复</button></div>` : ''}
      </div>`).join('')
      : (r.skipped ? '' : '<div style="padding:8px;color:var(--mut);">✅ 无违规</div>')}
    </div>`;
}
function govShaclClose(){ const el=document.getElementById('gov-shacl-r'); if(el) el.remove(); }

// ── 2026-09-13 SHACL 违规「定位修复」：从违规 subject IRI 反查实体 → 复用图谱实例编辑 Schema+保存 API ──
function govIsEntitySubject(sub){ return String(sub||'').indexOf('ent/') >= 0; }
function govEntityIdFromSubject(sub){
  const s = String(sub||''), i = s.indexOf('ent/');
  if(i < 0) return '';
  const raw = s.slice(i+4);
  try{ return decodeURIComponent(raw); }catch(e){ return raw; }
}
function govFixPropRow(p, val){
  const required = p.required ? '<span style="color:var(--amb);margin-left:2px;">*</span>' : '';
  const typeLabel = p.type && p.type!=='xsd:string' ? `<span title="本体类型 ${esc(p.type)}" style="font-size:10px;color:var(--blue-d);margin-left:2px;">${esc(p.type.replace('xsd:',''))}</span>` : '';
  const unit = p.unit ? `<span title="单位 ${esc(p.unit)}" style="font-size:10px;color:var(--amber-d,#8a5a00);margin-left:3px;">${esc(p.unit)}</span>` : '';
  let input;
  if((p.allowed_values||[]).length){
    input = `<select class="govfx-val" data-name="${esc(p.name)}" style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:11.5px;flex:1;background:#fff;">
      ${p.allowed_values.map(v=>`<option value="${esc(v)}" ${String(v)===String(val)?'selected':''}>${esc(v)}</option>`).join('')}
    </select>`;
  } else {
    const m={'xsd:int':'number','xsd:decimal':'number','xsd:float':'number','xsd:double':'number','xsd:dateTime':'datetime-local','xsd:date':'date','xsd:boolean':'checkbox','int':'number','decimal':'number','float':'number','double':'number','dateTime':'datetime-local','date':'date','boolean':'checkbox','bool':'checkbox'};
    const t = m[p.type]||'text';
    if(t==='checkbox'){
      input = `<input type="checkbox" class="govfx-val" data-name="${esc(p.name)}" ${String(val)==='true'||val===true?'checked':''} style="flex:1;">`;
    } else {
      const step = (p.type==='xsd:decimal'||p.type==='xsd:float'||p.type==='xsd:double') ? ' step="any"' : '';
      input = `<input class="govfx-val" data-name="${esc(p.name)}" type="${t}"${step} value="${esc(val==null?'':String(val))}" placeholder="${esc(p.description||'')}" style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:11.5px;flex:1;">`;
    }
  }
  const help = p.description ? `<span class="info-tip" title="${esc(p.description)}">ⓘ</span>` : '';
  return `<div style="display:flex;gap:6px;margin-bottom:5px;align-items:center;">
    <span style="width:84px;font-size:11.5px;color:var(--blue-d);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc(p.name)}">${esc(p.name)}${required}</span>
    ${input}${typeLabel}${unit}${help}
  </div>`;
}
async function govOpenFix(subject){
  const id = govEntityIdFromSubject(subject);
  if(!id){ toast('该违规非实体实例级（subject 无法解析），请到本体模型/数据整理页处理'); return; }
  const wrap = document.getElementById('gov-shacl-r'); if(!wrap) return;
  wrap.innerHTML = '<div class="loading" style="padding:20px;text-align:center;">加载实体与本体属性 Schema…</div>';
  try{
    const e = await api('/api/knowledge/entities/' + encodeURIComponent(id));
    if(e.error){ wrap.innerHTML = `<div style="padding:16px;color:#C53030;">实体不存在：${esc(String(e.error))}</div>`; return; }
    const type = e.entity_type || '';
    const schema = await api('/api/knowledge/graph/data-properties?type=' + encodeURIComponent(type));
    let cur = {}; try{ cur = JSON.parse(e.properties||'{}')||{}; }catch(x){}
    const rows = schema.length ? schema.map(p=>govFixPropRow(p, cur[p.name])).join('')
      : '<div style="color:var(--mut);font-size:11px;padding:6px 0;">该实体类型未定义本体数据属性，可直接保存或到图谱工作区操作。</div>';
    wrap.innerHTML = `
      <div style="padding:10px 12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--line);background:var(--blue-l);">
        <b style="font-size:13px;color:var(--blue-d);">📌 修复违规实体 · ${esc(e.name||id)}</b>
        <span style="flex:1"></span>
        <button class="btn sm ghost" onclick="govShaclCheck()">↩ 返回校验结果</button>
      </div>
      <div style="padding:10px 12px;font-size:11.5px;color:#444;line-height:1.8;">
        <div>🧬 类型 <span class="tag" style="color:var(--blue-d);">${esc(type||'(未标注)')}</span> · <span style="color:var(--mut);">id ${esc(id)}</span></div>
        <div style="margin-top:8px;"><b style="color:var(--blue-d);">🏷 本体受控属性（属性名与取值受本体约束）</b></div>
        <div style="margin-top:4px;">${rows}</div>
        <input type="hidden" id="gov-fix-id" value="${esc(id)}">
        <div style="margin-top:10px;text-align:right;"><button class="btn grn" onclick="govSaveFix()">💾 保存并重新校验</button></div>
      </div>`;
  }catch(err){ wrap.innerHTML = `<div style="padding:16px;color:#C53030;">修复面板加载失败：${esc(err.message||err)}</div>`; }
}
async function govSaveFix(){
  const idEl = document.getElementById('gov-fix-id'); if(!idEl) return;
  const id = idEl.value; if(!id){ toast('缺少实体 id'); return; }
  if(typeof branchWritable==='function' && !branchWritable()) return;
  const props = {};
  document.querySelectorAll('#gov-shacl-r .govfx-val').forEach(el=>{
    const k = el.dataset.name; if(!k) return;
    let v = el.type==='checkbox' ? (el.checked?'true':'false') : el.value;
    if(v !== '' && v != null) props[k] = v;
  });
  const e = await api('/api/knowledge/entities/' + encodeURIComponent(id));
  if(e.error){ toast('实例加载失败：'+(e.error||'')); return; }
  const br = (typeof getCurrentBranch==='function') ? getCurrentBranch() : 'dev';
  const r = await api('/api/knowledge/graph/nodes/' + encodeURIComponent(id), {method:'PUT', body:JSON.stringify({name:e.name||id, entity_type:e.entity_type||'', properties:props, branch:br})});
  if(r.error){ toast('保存失败：'+(typeof r.error==='string'?r.error:JSON.stringify(r.error))); return; }
  toast('✅ 实体已更新，重新校验中…');
  govShaclCheck();
}

function govReleaseModal(){
  const html = `<div style="font-size:12.5px;line-height:1.7;">
    <div style="background:#FFF7E0;border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:11.5px;">
      发布 = 冻结当前 OWL + SHACL 产物为不可变快照。存在 SHACL Violation 时会被拦截。
      major=语义变更（改定义/公理）· minor=新增 · patch=笔误。
    </div>
    <div><b>版本（SemVer）*</b></div>
    <input id="gov-rel-ver" placeholder="1.0.0" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin-top:4px;">
    <div style="margin-top:8px;"><b>发布说明</b></div>
    <textarea id="gov-rel-note" rows="2" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin-top:4px;"></textarea>
    <div style="margin-top:12px;text-align:right;"><button class="btn" onclick="govReleaseSubmit()">发布</button></div>
  </div>`;
  openPanel('本体发布快照', html);
}
async function govReleaseSubmit(){
  const body = { version:(document.getElementById('gov-rel-ver')||{}).value||'',
                 note:(document.getElementById('gov-rel-note')||{}).value||'' };
  const r = await api('/api/governance/ontology/release', {method:'POST', body:JSON.stringify(body)});
  if(r.error){ toast('失败：'+(typeof r.error==='string'?r.error:JSON.stringify(r.error))); return; }
  toast(`✅ 已发布 ${r.version}（OWL ${Math.round((r.owl_bytes||0)/1024)}KB · SHACL ${Math.round((r.shacl_bytes||0)/1024)}KB）`);
  closePanel();
}

let dupFilter = 'pending';
let dupChannel = '';   // S5：灰区来源筛选 ''|doc|sysml|cross（SysML 通道入库后自动生效）
let _dup = { page:1, size:15 };
async function loadDuplicates(status, channel) {
  dupFilter = status || dupFilter;
  if(channel !== undefined) dupChannel = channel;
  const el = document.getElementById('dup-panel');
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const [dups, merges] = await Promise.all([
      api(`/api/knowledge/entity-dups`),
      api('/api/knowledge/entity-merges?limit=500'),
    ]);
    // Q2：统一口径（entity-dups 待审全量 pending；entity-merges 返回 {items,total}）
    const mergeList = Array.isArray(merges) ? merges : (merges.items||[]);
    const autoAll = mergeList.filter(m=>m.operator==='auto'&&m.status==='merged');
    const autoM = autoAll.slice(0,10);
    const history = mergeList.slice(0,500);
    // S5：channel 解析与过滤（提升至 filterRow 之前供计数使用；channel 字段后端入库后自动生效）
    const chOf = d => { try{ const ev=JSON.parse(d.evidence||'{}'); const a=ev.a&&ev.a.channel, b=ev.b&&ev.b.channel;
      if(a&&b) return a!==b ? 'cross' : a; return b||a||'doc'; }catch(e){ return 'doc'; } };
    const pendingAll = (dups.duplicates||[]).filter(d=>d.status==='pending');
    const chCount = {doc:0, sysml:0, cross:0};
    pendingAll.forEach(d=>{ chCount[chOf(d)] = (chCount[chOf(d)]||0)+1; });
    const pending = pendingAll.filter(d=>!dupChannel || chOf(d)===dupChannel);
    // 过滤按钮组
    const filterRow = `<div style="display:flex;gap:6px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
      <button class="btn sm ${dupFilter==='pending'?'':'ghost'}" onclick="loadDuplicates('pending')">⏳ 待审（${pending.length}）</button>
      <button class="btn sm ${dupFilter==='auto'?'':'ghost'}" onclick="loadDuplicates('auto')">🤖 自动合并（${autoAll.length}）</button>
      <button class="btn sm ${dupFilter==='history'?'':'ghost'}" onclick="loadDuplicates('history')">📋 合并历史（${mergeList.length}）</button>
      <button class="btn sm" onclick="triggerDupDetect()">🔍 触发检测</button>
      <span style="flex:1"></span>
      <span style="font-size:11px;color:var(--mut);">${
        pendingAll.length ? (pending.length > 0 ? pending.length+' 候选待审' : '✅ 全部已处理') : '暂无数据'
      }</span>
    </div>`;
    if(dupFilter==='pending') {
      // S5：来源筛选行（channel 计数见上；SysML 通道入库后自动生效）
      const chRow = `<div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
        <span style="font-size:11.5px;color:var(--mut);">来源：</span>
        ${[['','全部'],['doc','📄 文档'],['sysml','⌨ SysML'],['cross','🔗 跨源']].map(([v,l])=>
          `<button class="fchip ${dupChannel===v?'on':''}" onclick="loadDuplicates('pending','${v}')" title="${v==='cross'?'两侧来源通道不同（如 文档⇄SysML）：跨源对齐卡片':v==='sysml'?'SysML 建模通道候选（锚点直通，正常不进灰区）':'文档抽取通道候选'}">${l} <b>${v===''?pendingAll.length:chCount[v]||0}</b></button>`).join('')}
      </div>`;
      if(!pending.length) { el.innerHTML = filterRow + chRow + '<div class="fempty">✅ <b style="color:var(--grn,#2f855a);font-weight:500;">灰区已清零</b>——身份判定完成，下一步处理冲突字段或直接物化。<br><button class="btn sm" onclick="fusNav(Array.from(document.querySelectorAll(\'.fus-nav-btn\')).find(b=>b.dataset.fpane===\'conflict\'),\'conflict\')">去冲突裁决 →</button> <button class="btn sm ghost" onclick="fusNav(Array.from(document.querySelectorAll(\'.fus-nav-btn\')).find(b=>b.dataset.fpane===\'confirm\'),\'confirm\')">去批次确认 →</button></div>'; return; }
      // 批量操作栏（行业：高相似度批量合并降低人工瓶颈；合并属破坏性操作需确认）
      const pPages = Math.max(1, Math.ceil(pending.length / _dup.size));
      if(_dup.page > pPages) _dup.page = pPages;
      const items = pending.slice((_dup.page-1)*_dup.size, _dup.page*_dup.size);
      const batchBar = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;font-size:11.5px;flex-wrap:wrap;">
        <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" id="dup-check-all" onchange="dupToggleAll(this)"> 全选</label>
        <button class="btn sm grn" onclick="dupBatchReview('confirm')">✅ 批量合并</button>
        <button class="btn sm red" onclick="dupBatchReview('reject')">🚫 批量驳回</button>
        <span style="color:var(--mut);">勾选后按「保留方优先」规则批量处理（属性融合 + 关系重指向，可审计撤销）</span>
      </div>`;
      el.innerHTML = filterRow + batchBar + items.map(d=>{
        const ev = JSON.parse(d.evidence||'{}');
        const keepN = ev.keep_name || d.keep_id.slice(0,10);
        const dupN = ev.dup_name || d.dup_id.slice(0,10);
        const score = parseFloat(d.score||0);
        const lvl = score>=0.9 ? ['high','var(--red)','高'] : score>=0.75 ? ['mid','var(--amb)','中'] : ['low','var(--grn)','低'];
        // S5：来源通道徽标（SysML 通道候选入库后自动显示；两侧通道不同 = 跨源对齐卡片）
        const _ch = chOf(d);
        // 工序② LLM 成对判定（消歧+对齐+冲突裁决+理由+置信度）——审核溯源
        const lv = ev.llm_verdict;
        let llmHtml = '';
        if(lv && lv.verdict){
          const vColor = lv.verdict==='merge' ? 'var(--grn)' : 'var(--blue-d)';
          const vLabel = lv.verdict==='merge' ? '🤖 建议合并' : '✅ 不同实体';
          const aligns = lv.alignment||{};
          const conflicts = (lv.conflict||[]).map(c=>`<span style="font-size:10.5px;color:var(--amb);">⚔️${esc(c.field||'')}→采纳${esc(c.take||'')}</span>`).join(' ') || '';
          llmHtml = `<div style="margin-top:4px;border:1px dashed ${vColor}66;border-radius:6px;padding:5px 8px;background:#fafaf7;font-size:11px;">
            <div><span class="tag" style="border-color:${vColor};color:${vColor};font-size:10px;">${vLabel}</span> <span style="color:var(--mut);font-size:10.5px;">置信度 ${(lv.confidence||0).toFixed(2)}</span> ${conflicts}</div>
            <div style="color:var(--mut);margin-top:2px;">消歧：${esc(lv.disambiguation||'')}</div>
            ${aligns.reason?`<div style="color:var(--mut);">对齐：${esc(aligns.reason)}</div>`:''}
            ${lv.degraded?`<div style="color:var(--red);font-size:10.5px;">⚠ LLM 不可用，已降级人工裁决</div>`:''}
          </div>`;
        } else if(lv && lv.degraded){
          llmHtml = `<div style="margin-top:4px;border:1px dashed var(--amb);border-radius:6px;padding:5px 8px;background:#fafaf7;font-size:10.5px;color:var(--red);">⚠ LLM 不可用，已降级：人工直接裁决（不影响流程）</div>`;
        }
        return `<div class="fcard ${_ch==='cross'?'cross':lvl[0]}" id="gc-${d.id}">
          <div style="display:flex;gap:8px;align-items:flex-start;">
          <input type="checkbox" class="dup-check" value="${d.id}" style="margin-top:3px;">
          <div style="flex:1;">
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:3px;">
              <span class="fpill ${_ch==='cross'?'amb':_ch==='sysml'?'':'gray'}">${_ch==='cross'?'🔗 跨源对齐':_ch==='sysml'?'⌨ SysML 通道':'📄 文档'}</span>
              <span class="fpill ${lvl[0]==='hi'?'amb':lvl[0]==='low'?'grn':'gray'}">相似度${lvl[2]} ${d.score}</span>
            </div>
            <div style="font-size:12.5px;"><b style="color:var(--grn,#2f855a);">${esc(keepN)}</b> <span style="color:var(--mut);font-size:11px;">(${esc(d.keep_id.slice(0,10))})</span> <span style="color:var(--mut);">⇄</span> <b style="color:var(--red);">${esc(dupN)}</b> <span style="color:var(--mut);font-size:11px;">(${esc(d.dup_id.slice(0,10))})</span></div>
            <div style="font-size:11px;color:var(--mut);margin-top:3px;">得分依据：向量 ${ev.vec_score||'-'} / 模糊 ${ev.fuzzy_score||'-'} / 方法 ${esc(d.method)}${ev.a&&(ev.a.source_doc||ev.b&&ev.b.source_doc)?` · 📄 来源：${esc((ev.a&&ev.a.source_doc)||'')} / ${esc((ev.b&&ev.b.source_doc)||'')}`:''}</div>
            ${llmHtml}
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end;">
            <button class="btn sm ghost" onclick="dupEvidence('${esc(d.keep_id)}','${esc(d.dup_id)}')" title="查看完整证据与来源上下文">📋 证据</button>
            ${lv && lv.verdict==='separate'
              ? `<button class="btn sm grn" onclick="dupReview(${d.id},'reject')" title="LLM 判定为不同实体：采纳即驳回合并候选">✅ 采纳(不同)</button>`
              : `<button class="btn sm grn" onclick="dupReview(${d.id},'confirm')" title="采纳 LLM/规则建议：合并候选（属性融合+关系重指向）">✅ 采纳</button>`}
            <button class="btn sm ghost" style="color:var(--red);" onclick="dupReview(${d.id},'reject')">🚫 驳回</button>
          </div>
          </div>
        </div>`;
      }).join('') + `<div style="display:flex;justify-content:flex-end;margin-top:6px;"><div id="dup-pager" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;"></div></div>`;
      renderPagerBar({
        el: document.getElementById('dup-pager'), total: pending.length, page: _dup.page, size: _dup.size,
        onPage: p => { _dup.page = p; loadDuplicates('pending'); },
        onSize: s => { _dup.size = s; _dup.page = 1; loadDuplicates('pending'); }
      });
    } else if(dupFilter==='auto') {
      if(!autoM.length) { el.innerHTML = filterRow + '<div style="color:var(--mut);font-size:12px;">暂无自动合并记录</div>'; return; }
      const hint = autoAll.length > autoM.length ? `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">共 ${autoAll.length} 条自动合并，显示最近 ${autoM.length} 条</div>` : '';
      el.innerHTML = filterRow + hint + autoM.map(m=>`
        <div style="border:1px solid var(--grn);border-radius:8px;padding:6px 10px;margin-bottom:4px;display:flex;gap:8px;align-items:center;font-size:12px;background:var(--green-l);">
          <span style="flex:1;"><b>${esc(m.keep_name||m.keep_id.slice(0,10))}</b> ← <b style="color:var(--red);">${esc(m.dup_name||m.dup_id.slice(0,10))}</b> · score ${m.score} · ${m.method} · <small style="color:var(--mut);">${(m.created_at||'').slice(0,16)}</small></span>
          <button class="btn sm red" onclick="rollbackMerge(${m.id})">↩ 撤销</button>
        </div>`).join('');
    } else {
      if(!history.length) { el.innerHTML = filterRow + '<div style="color:var(--mut);font-size:12px;">暂无合并记录</div>'; return; }
      const hint = merges.length > history.length ? `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">共 ${merges.length} 条合并记录，显示最近 ${history.length} 条</div>` : '';
      el.innerHTML = filterRow + hint + history.map(m=>`
        <div style="border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin-bottom:4px;font-size:11.5px;display:flex;gap:8px;align-items:center;">
          <span class="st ${m.status==='merged'?'ok':'w'}">${m.status==='merged'?'已合并':'已撤销'}</span>
          <span style="flex:1;"><b>${esc(m.keep_name||m.keep_id.slice(0,10))}</b> ← ${esc(m.dup_name||m.dup_id.slice(0,10))} · ${m.operator} · <small style="color:var(--mut);">${(m.created_at||'').slice(0,16)}</small></span>
          ${m.status==='merged'?`<button class="btn sm red" onclick="rollbackMerge(${m.id})">↩ 撤销</button>`:''}
        </div>`).join('');
    }
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
// 批量处理消歧候选（confirm=合并 / reject=驳回；逐条调用后端，任一失败不影响其余）
function dupToggleAll(cb) {
  document.querySelectorAll('.dup-check').forEach(x=>{ x.checked = cb.checked; });
}
async function dupBatchReview(action) {
  if(!branchWritable()) return;
  const ids = Array.from(document.querySelectorAll('.dup-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要处理的消歧候选'); return; }
  if(action==='confirm' && !(await confirmDialog(`批量合并 ${ids.length} 对候选实体？\n将按「保留方优先」规则融合属性、关系重指向、源实体软删除（可审计、可撤销）。`, {okText:'批量合并'}))) return;
  if(action==='reject' && !(await confirmDialog(`批量驳回 ${ids.length} 条消歧候选？`, {okText:'批量驳回'}))) return;
  let ok = 0, auto = 0, fail = 0; const fails = [];
  for(const cid of ids){
    try {
      const r = await api(`/api/knowledge/entity-dups/${cid}/review`, {method:'POST', body:JSON.stringify({action})});
      if(r && !r.error && r.auto_rejected) { auto++; continue; }
      if(r && !r.error) { ok++; continue; }
      fail++; fails.push((r&&r.error)||'未知错误');
    } catch(e) { fail++; fails.push(e.message||'网络错误'); }
  }
  let tip = ok>0 ? `✅ 批量${action==='confirm'?'合并':'驳回'} ${ok} 条` : `🚫 批量${action==='confirm'?'合并':'驳回'}未生效`;
  if(auto>0) tip += `；${auto} 条源实体缺失已自动清理`;
  if(fail>0) tip += `；${fail} 条失败${fails.length?`（${fails[0]}${fails.length>1?'等':''}）`:''}`;
  toast(tip);
  loadDuplicates(); loadReviewQueue(); loadKBStats();
}
async function triggerDupDetect() {
  if(!branchWritable()) return;
  toast('🔍 正在检测重复实体（blocking + 双判据评分）…');
  try {
    const r = await api('/api/knowledge/entity-dups/detect', {method:'POST'});
    toast(`🔍 检测完成：候选 ${r.candidates} / 自动合并 ${r.auto_merged}`);
    loadDuplicates();
    loadReviewQueue();
  } catch(e) { toast('检测失败：'+e.message); }
}
async function dupReview(cid, action) {
  if(!branchWritable()) return;
  const r = await api(`/api/knowledge/entity-dups/${cid}/review`, {method:'POST', body:JSON.stringify({action})});
  if(r.error) { toast('操作失败：'+r.error); return; }
  toast(action==='confirm'?'✅ 已合并':'🚫 已驳回');
  loadDuplicates();
  loadReviewQueue();
}
// 消歧证据对比：右侧面板并排展示保留/合并两实体属性 + 合并预览（对齐行业消歧决策视图）
async function dupEvidence(keepId, dupId) {
  try {
    const [k, d] = await Promise.all([
      api('/api/knowledge/entities/' + encodeURIComponent(keepId)),
      api('/api/knowledge/entities/' + encodeURIComponent(dupId)),
    ]);
    const parseP = e => { try { return JSON.parse(e.properties||'{}'); } catch(x) { return {}; } };
    const kp = parseP(k), dp = parseP(d);
    const allKeys = [...new Set([...Object.keys(kp), ...Object.keys(dp)])];
    const rows = allKeys.map(key=>`<tr><td>${esc(key)}</td><td>${esc(kp[key]!==undefined?String(kp[key]):'-')}</td><td>${esc(dp[key]!==undefined?String(dp[key]):'-')}</td></tr>`).join('');
    const relsOf = e => (e.relations||[]).map(r=>`${r.source_name||r.source_id} ${r.relation_type} ${r.target_name||r.target_id}`).join('；') || '无';
    // 合并预览：幸存值规则（保留方优先，缺失由合并方补齐；别名并入）
    const newKeys = Object.keys(dp).filter(kk=>kp[kk]===undefined);
    const mergePreview = `<div style="border:1px dashed var(--blue);border-radius:6px;padding:8px 10px;margin:8px 0;font-size:11px;line-height:1.7;">
      <b style="color:var(--blue-d);">🧩 合并预览（幸存值规则：保留方优先，缺失项由合并方补齐）</b>
      <div style="margin-top:4px;">合并后属性 <b>${allKeys.length}</b> 项 · 合并方补入 <b style="color:var(--grn);">${newKeys.length}</b> 项（${newKeys.map(x=>esc(x)).join('、')||'无'}）</div>
      <div>🔗 关系重指向：保留实体关系 ${(k.relations||[]).length} 条 + 合并实体关系 ${(d.relations||[]).length} 条</div>
      <div style="margin-top:4px;">确认合并后：属性融合、关系重指向到保留实体、源实体软删除（可审计、可撤销）。</div>
    </div>`;
    openPanel('🔀 消歧证据对比', `
      ${mergePreview}
      <table class="t">
        <tr><th style="width:90px;">属性</th><th style="color:var(--grn);">保留 ${esc(k.name)}</th><th style="color:var(--red);">合并 ${esc(d.name)}</th></tr>
        ${rows || '<tr><td colspan="3" style="color:var(--mut);">两实体无属性差异</td></tr>'}
      </table>
      <div style="margin-top:8px;font-size:11px;color:var(--mut);">
        <div>🧬 保留实体：${esc(k.entity_type)} · 来源 ${esc(k.source_doc||k.source_type||'-')} · 状态 ${esc(k.status)}</div>
        <div>🧬 合并实体：${esc(d.entity_type)} · 来源 ${esc(d.source_doc||d.source_type||'-')} · 状态 ${esc(d.status)}</div>
        <div style="margin-top:4px;">🔗 关系：保留 ${relsOf(k)}</div>
        <div>🔗 关系：合并 ${relsOf(d)}</div>
      </div>
      <div style="margin-top:10px;text-align:right;">
        <button class="btn sm grn" onclick="closePanel();mergeEntities('${esc(k.id)}','${esc(d.id)}','${esc(d.name)}')">✅ 确认合并</button>
      </div>`);
  } catch(e) { toast('证据加载失败：'+e.message); }
}
async function rollbackMerge(mid) {
  if(!(await confirmDialog('撤销合并将恢复被合并实体（软删除→恢复、关系回指）。确认？'))) return;
  const r = await api(`/api/knowledge/entity-merges/${mid}/rollback`, {method:'POST'});
  if(r.error) { toast('撤销失败：'+r.error); return; }
  toast('↩ 合并已撤销');
  loadDuplicates();
}
async function mergeEntities(keep, dup, dupName) {
  if(!(await confirmDialog(`确认将「${dupName}」合并到目标实体？\n属性融合、关系重指向、源实体软删除（可审计）。`))) return;
  const r = await api('/api/knowledge/entities/merge', {method:'POST', body:JSON.stringify({keep_id:keep, dup_id:dup})});
  if(r.error) { toast('合并失败：'+r.error); return; }
  toast(`✅ 已合并：属性融合 ${r.merged_props.length} 项`);
  loadDuplicates(); loadReviewQueue(); loadKBStats();
}

// ── KB-P0：文档管道管理（上传流程 + 进度 + 列表 + 追溯 + 元数据表单）──
let pendingDocFiles = [];   // S4：批量上传队列
let pendingDocFile = null;
