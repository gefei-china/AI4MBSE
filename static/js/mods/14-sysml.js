// ── 工程维度入库（2026-09-09 新流程）：工程全部版本 → 三元组 → 个人分支图库 ──
// 旧单版本「入库」入口已移除：AI 建模代码不再直接入库，归档动作收敛为工程级一次性操作。
let _piPreview = null;   // 缓存预览结果供向导确认使用
async function projectIngestWizard(){
  if(!currentConvId){ toast('缺少会话上下文'); return; }
  toast('正在统计工程版本…');
  let r;
  try{
    r = await api('/api/knowledge/project-ingest/preview', {method:'POST', body:JSON.stringify({conversation_id:currentConvId})});
  }catch(e){ toast('预览失败：' + (e.message||'')); return; }
  if(!r || r.error){ toast((r&&r.error)||'预览失败'); return; }
  _piPreview = r;
  const s = r.stats || {};
  const stat = (k,v,c)=>`<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border:1px solid ${c}33;background:${c}0d;border-radius:14px;font-size:11px;color:${c};"><b style="font-size:13px;">${v}</b>${k}</span>`;
  const verRows = (r.versions||[]).map(v=>`<div style="display:flex;gap:8px;align-items:center;padding:3px 0;font-size:11px;border-bottom:1px dashed var(--line);">
      <b style="min-width:96px;">${esc(v.label||('v'+v.id))}</b>
      <span class="st ${v.status==='committed'?'b':(v.status==='current'?'w':'')}" style="padding:0 6px;font-size:9.5px;">${v.status==='committed'?'已入库':(v.status==='current'?'当前':'已替代')}</span>
      <span style="color:var(--mut);">${v.error?('<span style="color:var(--red);">✕ '+esc(v.error)+'</span>'):(`${v.entities} 实体 · ${v.relations} 关系 · ${v.attributes||0} 属性`)}</span>
    </div>`).join('');
  openPanel('📦 工程入库 — ' + (r.project_name||''),
    `<div style="font-size:12px;line-height:1.8;">
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px;">
        ${stat('个版本', s.versions||0, '#1d4ed8')}
        ${stat('去重实体', s.unique_entities||0, '#2f855a')}
        ${stat('关系', s.relations||0, '#7c3aed')}
        ${stat('属性', s.attributes||0, '#c98a2e')}
      </div>
      <div style="padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font-size:11.5px;color:var(--mut);line-height:1.9;">
        与个人分支图库已有实体重合：<b style="color:var(--ink);">${s.overlap_with_branch||0}</b> 个（入库时自动对齐合并） · 预计新增 <b style="color:var(--grn,#2f855a);">${s.estimated_new||0}</b> 个<br>
        目标分支：<b>personal</b>（个人分支图库） · 写入前自动过融合闸+质量闸 · 可回滚（版本快照）
      </div>
      <div style="margin:10px 0 4px;font-weight:600;font-size:11.5px;">版本清单（${(r.versions||[]).length}）</div>
      <div style="max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:6px 10px;">${verRows||'<span style="color:var(--mut);">无可入库版本</span>'}</div>
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn ghost" onclick="closePanel()">取消</button>
        <button class="btn primary" id="pi-commit-btn" onclick="projectIngestCommit()">📦 确认入库</button>
      </div>
      <div style="margin-top:6px;font-size:10.5px;color:var(--mut);">提交后按版本依次：候选化 → 融合闸 → 物化入图库 → 生成三元组 → 写批次记录。</div>
    </div>`);
}
async function projectIngestCommit(){
  const btn = document.getElementById('pi-commit-btn');
  if(btn){ btn.disabled = true; btn.textContent = '⏳ 入库中…'; }
  let r;
  try{
    r = await api('/api/knowledge/project-ingest/commit', {method:'POST', body:JSON.stringify({conversation_id:currentConvId})});
  }catch(e){ toast('入库失败：' + (e.message||'')); if(btn){ btn.disabled=false; btn.textContent='📦 确认入库'; } return; }
  if(!r || r.error){ toast((r&&r.error)||'入库失败'); if(btn){ btn.disabled=false; btn.textContent='📦 确认入库'; } return; }
  const s = r.stats || {};
  const stCls = r.status==='success'?'ok':(r.status==='partial'?'w':'r');
  const stTxt = r.status==='success'?'✅ 入库完成':(r.status==='partial'?'⚠️ 部分成功':'✕ 入库失败');
  const row = (k,v,c)=>`<div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:4px 0;"><span style="color:var(--mut);">${k}</span><b style="color:${c||'var(--ink)'};">${v}</b></div>`;
  openPanel('📦 工程入库回执 — ' + (r.project_name||''),
    `<div style="font-size:12px;line-height:1.8;">
      <div style="margin:8px 0;"><span class="st ${stCls}" style="font-size:12px;padding:3px 12px;">${stTxt}</span>
        <span style="font-size:10.5px;color:var(--mut);margin-left:8px;">批次 ${esc(r.batch_id)} · 分支 ${esc(r.target_branch)} · 耗时 ${(s.elapsed_ms/1000||0).toFixed(1)}s</span></div>
      <div style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">
        ${row('版本（成功/总数）', `${s.versions_total-(s.versions_failed||0)} / ${s.versions_total}`, s.versions_failed?'var(--amb)':'var(--grn,#2f855a)')}
        ${row('候选（实体+关系）', s.candidates||0)}
        ${row('生成三元组', '+' + (s.triples_staged||0), '#7c3aed')}
        ${row('拍板批准三元组', s.triples_approved||0, 'var(--grn,#2f855a)')}
        ${row('落图：实体 / 关系', `${s.entities_written||0} / ${s.relations_written||0}`, '#1d4ed8')}
        ${row('融合闸：自动合并 / 人工队列', `${s.auto_merged||0} / ${s.review_queue||0}`)}
        ${row('本体校验拒绝', s.rejected||0, (s.rejected?'var(--amb)':''))}
      </div>
      ${(r.errors&&r.errors.length)?`<div style="margin-top:8px;padding:8px 10px;border:1px solid #F3C1C1;background:#fff5f5;border-radius:8px;font-size:11px;color:#b91c1c;">${r.errors.map(e=>'· '+esc(e)).join('<br>')}</div>`:''}
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn" onclick="projectIngestLogsPanel()">📜 查看入库历史</button>
        <button class="btn primary" onclick="closePanel()">完成</button>
      </div>
    </div>`);
  if(_artConv) loadSysmlVersionsBar(_artConv);
}
async function projectIngestLogsPanel(projectId){
  if(!projectId && currentConvId){
    try{
      // 会话 → 工程反查（无工程则看全部）
      const conv = await api('/api/conversations/' + currentConvId);
      projectId = (conv && conv.project_id) || '';
    }catch(e){}
  }
  let r;
  try{
    r = await api('/api/knowledge/project-ingest/logs' + (projectId?('?project_id='+encodeURIComponent(projectId)):''));
  }catch(e){ toast('加载入库历史失败：' + (e.message||'')); return; }
  const logs = (r && r.logs) || [];
  const stMap = {success:['ok','✅ 成功'], partial:['w','⚠️ 部分'], failed:['r','✕ 失败'], running:['w','⏳ 进行中']};
  const rows = logs.map(l=>{
    const st = stMap[l.status] || ['','⏳'];
    const s = l.stats || {};
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:8px;background:var(--card);">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <b style="font-size:11.5px;">${esc(l.project_name||l.project_id)}</b>
        <span class="st ${st[0]}" style="padding:0 6px;font-size:9.5px;">${st[1]}</span>
        <span style="font-size:10px;color:var(--mut);">${esc((l.created_at||'').slice(0,16))} · ${esc(l.operator||'')} · ${esc(l.target_branch)}</span>
        <span style="margin-left:auto;font-size:10px;color:var(--mut);">${esc(l.batch_id)}</span>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px;font-size:10.5px;color:var(--mut);">
        <span>版本 <b style="color:var(--ink);">${(s.versions_total||0)-(s.versions_failed||0)}/${s.versions_total||0}</b></span>
        <span>三元组 <b style="color:#7c3aed;">+${s.triples_approved||s.triples_staged||0}</b></span>
        <span>落图实体 <b style="color:var(--grn,#2f855a);">${s.entities_written||0}</b></span>
        <span>落图关系 <b style="color:#1d4ed8;">${s.relations_written||0}</b></span>
        <span>人工队列 ${s.review_queue||0}</span>
        <span>耗时 ${((s.elapsed_ms||0)/1000).toFixed(1)}s</span>
      </div>
      ${l.error_msg?`<div style="margin-top:5px;font-size:10.5px;color:var(--red);">${esc(l.error_msg)}</div>`:''}
    </div>`;
  }).join('');
  openPanel('📜 工程入库历史' + (projectId?'':'（全部工程）'),
    `<div style="font-size:11.5px;color:var(--mut);margin-bottom:8px;">每次工程入库的批次记录与数据统计；入库内容可在「知识库 · 图谱」按 personal 分支查看，快照可回滚。</div>
     ${rows || '<div style="padding:24px;text-align:center;color:var(--mut);">暂无入库记录——在版本历史面板点击「📦 工程入库」发起第一次归档</div>'}`);
}
// 标记为采纳版本（同会话后标记覆盖）
async function sysmlAdoptVersion(vid){
  try{
    const r = await api('/api/sysml-versions/' + vid + '/adopt', {method:'POST', body:'{}'});
    if(r && r.error){ toast(r.error); return; }
    toast('已标记为采纳版本');
    if(_artConv) loadSysmlVersionsBar(_artConv);
  }catch(e){ toast('标记失败：' + (e.message||'')); }
}
// P0-6：查看指定版本的 SysML v2 代码（版本跟随代码文件——代码文本存 sysml_versions.code_text）
async function openSvmVersionCode(vid){
  try{
    const r = await api('/api/sysml-versions/' + vid);
    const code = (r && r.code_text) || '';
    openPreviewTab({id:null, kind:'code', kind_label:'代码',
      title: ((r && r.version_label) || ('v'+vid)) + ' · SysML v2 代码',
      preview_type:'code', preview_content: code || '(该版本未留存代码文本)', message_id:null, meta:{}});
  }catch(e){ toast('加载版本代码失败：' + (e.message||'')); }
}
// 版本历史渲染（SysML 文件预览底部）
async function loadSysmlVersionsBar(convId){
  const el = document.getElementById('preview-sysml-versions');
  if(!el || !convId){ if(el) el.innerHTML=''; return; }
  try{
    const r = await api('/api/sysml-versions?conversation_id=' + convId);
    const vs = r.versions || [];
    if(!vs.length){ el.innerHTML=''; return; }
    el.innerHTML = `<div style="margin-top:10px;padding-top:8px;border-top:1px dashed var(--line);text-align:left;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <span style="font-size:11px;color:var(--mut);">📜 SysML 代码版本历史（每次生成 = 一个代码文件版本，可查看各版本代码）</span>
        <span style="margin-left:auto;display:flex;gap:4px;flex:none;">
          <button class="btn sm ghost" style="font-size:10px;padding:0 8px;color:var(--blue-d);border-color:var(--blue-bd,#B5D4F4);" onclick="projectIngestWizard()" title="工程维度入库：本会话所属工程的全部版本 → 三元组 → 个人分支图库">📦 工程入库</button>
          <button class="btn sm ghost" style="font-size:10px;padding:0 8px;color:var(--blue-d);border-color:var(--blue-bd,#B5D4F4);" onclick="zhiyuanPullIngest()" title="从智源拉取当前工程建模数据，后端直接转三元组入个人图库（对话流回执）">⇩ 智源拉取</button>
          <button class="btn sm ghost" style="font-size:10px;padding:0 8px;" onclick="projectIngestLogsPanel()" title="历次工程入库批次与统计信息">📜 入库历史</button>
        </span>
      </div>
      <div style="display:flex;flex-direction:column;gap:5px;">` + vs.map(v=>{
      const es = v.element_summary || {};
      const st = v.adopted ? ['ok','✅ 采纳']
        : (v.status==='committed' ? ['b','📦 已入库'] : (v.status==='current' ? ['w','🕒 当前'] : ['','◻️ 已替代']));
      return `<div style="display:flex;align-items:center;gap:8px;font-size:11.5px;">
        <b>代码文件 ${esc(v.version_label)}</b>
        <span class="st ${st[0]}" style="padding:1px 6px;">${st[1]}</span>
        <span style="color:var(--mut);font-size:10.5px;">${es.views||0} 视图 · ${es.entities||0} 实体 / ${es.relations||0} 关系</span>
        <span style="margin-left:auto;display:flex;gap:4px;">
          <button class="btn sm ghost" style="font-size:10px;padding:0 8px;" onclick="openSvmVersionCode(${v.id})" title="查看该版本 SysML v2 代码">👁 代码</button>
          ${v.adopted?'':`<button class="btn sm ghost" style="font-size:10px;padding:0 8px;" onclick="sysmlAdoptVersion(${v.id})" title="标记为最终采纳版本">📌 采纳</button>`}
        </span>
      </div>`;
    }).join('') + `</div></div>`;
  }catch(e){}
}
// ── P0-3 会话产物卡片网格（规范06）：每产物一卡（图标+标题+类型+操作），≥3 横滑轨道 ──
function artGridHtml(m, cd){
  const items = [];
  // P0-5（2026-08-23）：查看入口收敛——代码/视图查看已由消息内「代码|视图」tab 承载，产物网格不再渲染重复的视图/代码卡
  if(cd && (cd.sections || cd.report || m.msg_type==='card_report')){
    items.push({icon:'📄', title:'分析报告', meta:'Markdown · 已归档', sub:'',
      ops:`<span class="pr-act" onclick="event.stopPropagation();artReportFromMsg(${m.id},'md')">⬇ md</span> <span class="pr-act" onclick="event.stopPropagation();artReportFromMsg(${m.id},'docx')">docx</span> <span class="pr-act" onclick="event.stopPropagation();artReportFromMsg(${m.id},'pdf')">pdf</span>`});
  }
  if(!items.length) return '';
  (window._artByMsg = window._artByMsg || {})[m.id] = {cd: cd, content: m.content||''};
  const many = items.length >= 3 ? ' many' : '';
  return `<div class="art-grid${many}">` + items.map(it=>`
    <div class="art-card" title="点击打开文件预览" onclick="artOpenFromMsg(${m.id})">
      <div class="ac-head"><span class="ac-ico">${it.icon}</span><span class="ac-title">${esc(it.title)}</span></div>
      <div class="ac-meta">${esc(it.meta)}${it.sub?` · ${esc(it.sub)}`:''}</div>
      <div class="ac-ops">${it.ops}</div>
    </div>`).join('') + `</div>`;
}
// P0-3 报告产物导出：从消息缓存恢复 _lastReport 再下载（多消息报告互不串扰）
function artReportFromMsg(mid, fmt){
  const a = (window._artByMsg||{})[mid];
  if(a && a.cd){
    const c = a.cd, r = c.report || {};
    window._lastReport = {title:r.title||'报告', sections:c.sections||(r.sections||[]), summary:r.summary||'', report_type:r.report_type||'analysis', meta:r.meta||{}};
  }
  downloadReport(fmt);
}
// 2026-09-17 移除「⚠ 需要确认 · SysML 产物入库」审批卡（用户确认移除）：
//   - 与架构已脱节：本文件顶部即注明「旧单版本入库入口已移除，AI 建模代码不再直接入库，归档收敛为工程级一次性操作」；
//   - 伪闸门：「批准」只写 localStorage 并提示去版本历史点「📦 工程入库」，无真实入库动作；「驳回」同样无副作用；
//   - 误导成本：消息流内常驻琥珀审批卡会让用户以为「不确认就无法入库」，实际入库路径与之无关。
//   一并移除：sysmlApprovalCardHtml / approvalDecision / _sysmlApGet / _sysmlApSet（唯一调用点在 05-markdown.js，已同步删除）
//   + index.html 中仅服务该卡的 .approval-card 样式。入库真实入口：图谱工作区 · 版本历史 →「📦 工程入库」。
// 消歧徽章（对齐 v2g matching_status / rel_matching_status 语义）
function _sysmlMatchBadge(c){
  if(c.status==='rejected') return `<span class="st r">🛑 本体校验不通过</span>`;
  if(c.entity_type==='关系候选'){
    return c.rel_matching_status==='dup_high'
      ? `<span class="st w">⚠ 与已有关系重复 → 建议跳过</span>` : `<span class="st ok">✅ 新建</span>`;
  }
  if(c.matching_status==='dup_high') return `<span class="st w">⚠ 与已有实体重复 → 建议对齐/跳过</span>`;
  if(c.matching_status==='dup_suspect') return `<span class="st w">🔀 疑似重复 → 建议人工确认</span>`;
  return `<span class="st ok">✅ 新建</span>`;
}
// 入库向导（右侧滑入抽屉 #import-drawer）：候选清单（勾选部分入库）+ 消歧徽章 + 来源视图 + 重复策略 → v2g/confirm
// P0-2：行渲染统一取 name ?? entity_name；P1-4：勾选部分（selected_ids）+ 顶部统计（类型分组/重复数）
// P2-9：实体按类型聚合分组；P1-5：重复策略影响预览；P1-7：来源视图徽章；P1-6：「放弃候选」
function openSysmlImportDialog(batchId, candidates, versionId, dup){
  const drawer = document.getElementById('import-drawer');
  const mask = document.getElementById('import-mask');
  const b = document.getElementById('import-drawer-body');
  const sub = document.getElementById('import-sub');
  const ents = (candidates||[]).filter(c=>(c.entity_type!=='关系候选' && c.entity_type!=='关系'));
  const rels = (candidates||[]).filter(c=>(c.entity_type==='关系候选' || c.entity_type==='关系'));
  const rejN = (candidates||[]).filter(c=>c.status==='rejected').length;
  const _nm = c => c.name ?? c.entity_name ?? '';
  const _view = c => (c.properties && c.properties.view) || '';
  const _batchDup = c => !!(c.properties && c.properties.batch_dup);
  // 关系候选行名：优先 rel_source/rel_type/rel_target 三元组拼接（首开路径 name='关系候选'，幂等重开才有 entity_name 拼接）
  const _relLabel = c => {
    if (c.rel_source && c.rel_type && c.rel_target)
      return `${c.rel_source} —${c.rel_type}→ ${c.rel_target}`;
    const nm = _nm(c);
    if (!nm || nm === '关系候选' || nm === '关系') return '';   // 无字段且无名称 → 空（展示端兜底为空行）
    const parts = nm.split('--');                                // entity_name 形如 "A --R-- B"
    if (parts.length >= 3) return `${parts[0].trim()} —${parts[1].trim()}→ ${parts.slice(2).join('--').trim()}`;
    return nm;
  };
  const _isRel = c => (c.entity_type === '关系候选' || c.entity_type === '关系');
  // P2-9：实体按类型分组（数量降序）
  const groups = {};
  ents.forEach(c => { const t = c.entity_type||'实体'; (groups[t] = groups[t]||[]).push(c); });
  const typeNames = Object.keys(groups).sort((a,b)=>groups[b].length-groups[a].length);
  const dupN = ents.filter(c=>(c.matching_status==='dup_high'||c.matching_status==='dup_suspect')||_batchDup(c)).length
             + rels.filter(c=>c.rel_matching_status==='dup_high').length;
  const rowHtml = c => `
    <div style="display:flex;align-items:center;gap:6px;padding:5px 6px;border-bottom:1px dashed var(--line);font-size:11.5px;">
      <input type="checkbox" class="si-cb" value="${c.cid}" checked style="width:auto;flex:none;" title="勾选该候选入库">
      <span style="width:14px;">${c.status==='rejected'?'🛑':'☑'}</span>
      <b style="max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(_isRel(c) ? _relLabel(c) : _nm(c))}">${esc(_isRel(c) ? _relLabel(c) : _nm(c))}</b>
      ${_view(c)?`<span class="tag" style="font-size:9.5px;background:#eef1f6;color:var(--mut);">${esc(_view(c))}</span>`:''}
      <span class="tag" style="font-size:10px;">${esc(_isRel(c) ? '关系' : (c.entity_type||'实体'))}</span>
      ${_batchDup(c)?`<span class="st w" title="与同批次候选近似重复">批内疑似</span>`:''}
      <span style="margin-left:auto;">${_sysmlMatchBadge(c)}</span>
    </div>`;
  const listHtml = typeNames.map(t => `
    <div style="background:#f7f9fc;padding:4px 6px;font-size:10.5px;color:var(--mut);border-bottom:1px solid var(--line);">${esc(t)}（${groups[t].length}）</div>
    ${groups[t].map(rowHtml).join('')}`).join('');
  b.innerHTML = `
    <div style="border:1px solid var(--line);border-radius:8px;font-size:11.5px;margin-bottom:10px;">
      ${listHtml}
      ${rels.length?`<div style="background:#f7f9fc;padding:4px 6px;font-size:10.5px;color:var(--mut);border-bottom:1px solid var(--line);">关系候选（${rels.length}，两端实体须已入库，确认时自动校验）</div>`:''}
      ${rels.map(rowHtml).join('')}
      ${(ents.length+rels.length)===0?'<div style="padding:12px;color:var(--mut);text-align:center;">无候选</div>':''}
    </div>
    <div style="display:flex;align-items:center;gap:8px;margin:8px 0;font-size:11px;color:var(--mut);flex-wrap:wrap;">
      <label style="display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="checkbox" id="si-all" checked style="width:auto;">全选</label>
      <span>重复候选处理：<select id="sysml-dup-action" style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;">
        <option value="create">强制新建（保留重复）</option>
        <option value="align">对齐复用（合并到已有元素）</option>
        <option value="skip">跳过（不入库）</option>
      </select></span>
      <span id="si-dup-hint" style="color:var(--mut);font-size:10.5px;"></span>
    </div>
    <div style="font-size:10.5px;color:var(--mut);">AI 建模生成的 V2 候选默认进入「待审核队列」，经三元组统一审核通过后写入模型库（一次入库 = 一次提交）；入库数据与「个人分支已有模型元素 + 同批候选」比对消歧后写入 personal 分支。</div>`;
  if(sub) sub.textContent = `${versionId?`版本 ${versionId} · `:''}来源：统一闸门暂存（未入库） · 共 ${ents.length} 实体 / ${rels.length} 关系${dupN?` · 重复 ${dupN}`:''}${rejN?` · ${rejN} 项校验不通过（自动拒绝）`:''}`;
  drawer.classList.add('show');
  mask.classList.add('show');
  const close = () => closeSysmlImportDrawer();
  const updateDupHint = () => {
    const act = document.getElementById('sysml-dup-action').value;
    const n = ents.filter(c=>(c.matching_status==='dup_high'||c.matching_status==='dup_suspect')||_batchDup(c)).length
             + rels.filter(c=>c.rel_matching_status==='dup_high').length;
    const el = document.getElementById('si-dup-hint');
    if(el) el.textContent = n ? `${act==='skip'?'跳过':act==='align'?'对齐复用':'强制新建'}将影响 ${n} 条重复候选` : '';
  };
  document.getElementById('si-all').onchange = e => {
    document.querySelectorAll('.si-cb').forEach(cb=>cb.checked=e.target.checked);
  };
  document.getElementById('sysml-dup-action').onchange = updateDupHint;
  document.getElementById('si-cancel').onclick = close;
  document.getElementById('si-discard').onclick = async () => {
    if(!versionId){ toast('缺少版本信息，无法放弃'); return; }
    const okDiscard = await confirmDialog('放弃候选？将删除已生成的待确认候选，可重新生成。');
    if(!okDiscard) return;
    try{
      const r = await api('/api/sysml-versions/' + versionId + '/import-candidates', {method:'DELETE'});
      if(r && r.error){ toast(r.error); return; }
      close();
      toast(`已放弃候选（删除 ${r.deleted||0} 条），可重新生成`);
      if(_artConv) loadSysmlVersionsBar(_artConv);
    }catch(e){ toast('放弃失败：' + (e.message||'')); }
  };
  document.getElementById('si-ok').onclick = async () => {
    const act = document.getElementById('sysml-dup-action').value;
    const ids = [...document.querySelectorAll('.si-cb:checked')].map(cb=>parseInt(cb.value,10));
    const okBtn = document.getElementById('si-ok'); okBtn.disabled = true; okBtn.textContent = '处理中…';
    try{
      const body = {batch_id: batchId, dup_action: act};
      // P1-4：部分勾选 → 只确认勾选候选（全选时省略 selected_ids 保持向后兼容）
      if(ids.length && ids.length < (candidates||[]).length) body.selected_ids = ids;
      const r = await api('/api/knowledge/v2g/confirm', {method:'POST', body:JSON.stringify(body)});
      if(r && r.error){ toast('入库失败：' + r.error); okBtn.disabled = false; okBtn.textContent = '✓ 确认入库'; return; }
      close();
      const _g = r && r.gate;
      let _gateMsg = '';
      if(_g && _g.enabled){
        _gateMsg = _g.status==='approved'
          ? '（已自动发布到权威分支）'
          : (_g.status==='pending' ? '（已提交待审：发布需审批合并请求）'
             : (_g.status==='waiting' ? '（已有待审批发布合并请求，请先处理）' : ''));
      }
      if(r && r.pending_review){
        // 优化一：V2 生成候选默认进入候选待审（未直接入库）
        toast(`🕒 V2 生成候选（${r.pending_review} 条）已进入待审核队列；请在数据整理/三元组统一审核中完成确认后再正式入库`);
        if(_artConv) loadSysmlVersionsBar(_artConv);
        return;
      }
      toast(`✅ 候选已确认（入库 ${r.confirmed||0} 条${(r.skipped&&r.skipped.length)?` / 跳过 ${r.skipped.length}`:''}），进入实体/关系审核队列正式审核${_gateMsg}`);
      // 反馈捕获：确认 = 采纳（有消息上下文时隐式记录）
      if(versionId){ try{
        const vr = await api('/api/sysml-versions/' + versionId);
        if(vr && vr.message_id) api(`/api/messages/${vr.message_id}/feedback`, {method:'POST', body:JSON.stringify({type:'approve'})}).catch(()=>{});
      }catch(e){} }
      if(_artConv) loadSysmlVersionsBar(_artConv);
    }catch(e){ toast('入库失败：' + (e.message||'')); okBtn.disabled = false; okBtn.textContent = '✓ 确认入库'; }
  };
  updateDupHint();
}
// 关闭入库向导右侧抽屉
function closeSysmlImportDrawer(){
  const d = document.getElementById('import-drawer');
  const m = document.getElementById('import-mask');
  if(d) d.classList.remove('show');
  if(m) m.classList.remove('show');
}

// ── 知识库 ──


// ══ 2026-09-11：智源拉取直通入库（AI 建模对话流回执，不进数据治理面板） ══
async function zhiyuanPullIngest(){
  if(!(await confirmDialog('从智源拉取当前工程建模数据？\n后端将直接：解析 → 候选化 → 融合闸 → 转三元组 → 入库个人分支图库。\n（不产生数据治理审核面板待办，回执在本对话流显示）'))) return;
  toast('⇩ 正在从智源拉取建模数据并直通入库…');
  let r;
  try{
    r = await api('/api/knowledge/sysml/pull-ingest', {method:'POST', body:JSON.stringify({})});
  }catch(e){ toast('拉取失败：'+(e.message||'')); return; }
  if(!r || r.error){ toast('拉取失败：'+((r&&r.error)||'')); _zhiyuanReceipt(r); return; }
  _zhiyuanReceipt(r);
  if(_artConv) loadSysmlVersionsBar(_artConv);
}
// 回执卡：插入当前对话流底部（AI 建模流程内可见）
function _zhiyuanReceipt(r){
  const area = document.getElementById('chat-area');
  const s = r.stats || {};
  const row = (k,v,c)=>'<div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">'+k+'</span><b style="color:'+(c||'var(--ink)')+';">'+v+'</b></div>';
  let body;
  if(r.error){
    body = '<div style="font-size:12px;color:#b91c1c;">✕ 拉取入库失败：'+esc(r.error)+'</div>';
  } else {
    body = '<div style="font-size:12px;line-height:1.8;">'
      + '<div style="margin-bottom:6px;"><span class="st ok" style="font-size:12px;padding:3px 12px;">✅ 智源拉取直通入库完成</span>'
      + '<span style="font-size:10.5px;color:var(--mut);margin-left:8px;">批次 '+esc(r.batch_id||'')+' · 分支 '+esc(r.target_branch||'personal')+' · 耗时 '+((s.elapsed_ms/1000)||0).toFixed(1)+'s</span></div>'
      + '<div style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">'
      + row('模型来源', esc(r.model_name||'智源'))
      + row('候选（实体+关系）', s.candidates||0)
      + row('生成三元组', '+'+(s.triples_staged||0), '#7c3aed')
      + row('自动批准并落图：实体 / 关系', (s.entities_written||0)+' / '+(s.relations_written||0), '#1d4ed8')
      + row('融合闸：自动合并 / 人工队列', (s.auto_merged||0)+' / '+(s.review_queue||0))
      + row('本体校验拒绝', s.rejected||0, s.rejected?'var(--amb)':'')
      + '</div>'
      + (r.text_preview?('<details style="margin-top:6px;"><summary style="cursor:pointer;font-size:10.5px;color:var(--mut);">智源回读文本预览（前 400 字符）</summary><pre style="margin:4px 0 0;padding:6px 8px;background:#0f172a;color:#e2e8f0;border-radius:5px;font-size:10px;overflow-x:auto;white-space:pre-wrap;">'+esc(r.text_preview)+'</pre></details>'):'')
      + '</div>';
  }
  if(!area){ toast(r.error? '拉取失败' : '✅ 智源拉取入库完成'); return; }
  const card = document.createElement('div');
  card.className = 'msg ai';
  card.innerHTML = '<div class="msg-inner"><div class="body" style="border-color:#C9B8F5;background:#FBF9FF;"><div style="font-size:11px;color:#7c3aed;font-weight:600;margin-bottom:6px;">⇩ 智源拉取直通入库</div>'+body+'</div></div>';
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;
}
