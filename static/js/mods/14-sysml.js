// ── 2026-09-24 方案A（用户拍板）：「📦 工程入库」向导已移除 ──
// 理由：工程入库是"片段直入图库"的捷径（sysml_versions 视图 JSON 不经过建模工具直接融合落 personal 分支），
// 与设计主链「归一确认 → 写回智源 → 智源拉取 → 个人分支」不一致，且造成双数据源（图库混入未经智源的数据）。
// 数据链路统一后，AI 建模数据进图库的唯一路径 = 写回智源（push-zhiyuan）→ 智源拉取（zhiyuan_pull_ingest）。
// projectIngestWizard / projectIngestCommit / _piPreview 已随入口一并删除；
// 后端 /api/knowledge/project-ingest/* 端点暂保留供历史批次治理（回滚/审计），前端无入口。
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
  const srcMap = {
    zhiyuan_pull: ['⇩ 智源拉取', '#7c3aed'],
    project_ingest: ['📦 工程入库（旧）', 'var(--mut)']
  };
  const rows = logs.map(l=>{
    const st = stMap[l.status] || ['','⏳'];
    const s = l.stats || {};
    const src = srcMap[l.source || 'project_ingest'] || ['📥 入库', 'var(--mut)'];
    const isZpull = (l.source === 'zhiyuan_pull');
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:8px;background:var(--card);">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <span class="st" style="padding:0 6px;font-size:9.5px;color:${src[1]};border-color:${src[1]}44;background:${src[1]}11;">${src[0]}</span>
        <b style="font-size:11.5px;">${esc(l.project_name||l.project_id||'—')}</b>
        <span class="st ${st[0]}" style="padding:0 6px;font-size:9.5px;">${st[1]}</span>
        <span style="font-size:10px;color:var(--mut);">${esc((l.created_at||'').slice(0,16))} · ${esc(l.operator||'')} · ${esc(l.target_branch)}</span>
        <span style="margin-left:auto;font-size:10px;color:var(--mut);">${esc(l.batch_id)}</span>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px;font-size:10.5px;color:var(--mut);">
        ${isZpull ? `<span>候选 <b style="color:var(--ink);">${s.candidates||0}</b></span>`
                  : `<span>版本 <b style="color:var(--ink);">${(s.versions_total||0)-(s.versions_failed||0)}/${s.versions_total||0}</b></span>`}
        <span>三元组 <b style="color:#7c3aed;">+${s.triples_approved||s.triples_staged||0}</b></span>
        <span>落图实体 <b style="color:var(--grn,#2f855a);">${s.entities_written||0}</b></span>
        <span>落图关系 <b style="color:#1d4ed8;">${s.relations_written||0}</b></span>
        <span>人工队列 ${s.review_queue||0}</span>
        <span>耗时 ${((s.elapsed_ms||0)/1000).toFixed(1)}s</span>
      </div>
      ${l.error_msg?`<div style="margin-top:5px;font-size:10.5px;color:var(--red);">${esc(l.error_msg)}</div>`:''}
    </div>`;
  }).join('');
  openPanel('📜 入库台账' + (projectId?'':'（全部工程）'),
    `<div style="font-size:11.5px;color:var(--mut);margin-bottom:8px;">本工程历次入库批次的持久记录（对话流回执易逝，以台账为准）。入库内容可在「知识库 · 图谱」按 personal 分支查看。工程入库直入捷径已下线（2026-09-24 方案A：数据链路统一走「写回智源 → 智源拉取」），其历史批次标记为「旧」保留可查。</div>
     ${rows || '<div style="padding:24px;text-align:center;color:var(--mut);">暂无入库记录——点击「⇩ 智源拉取」发起第一次入库</div>'}`);
}
// 2026-09-24 方案A（用户拍板）：AI 建模数据链路统一「归一确认 → 写回智源 → 智源拉取 → 个人分支」——
//   「📦 工程入库」直入捷径入口移除（见文件头注释）；新增「🔗 写回智源」（对当前版本，带状态标识与防重复）；
//   保留 ⇩ 智源拉取（工程维度 vc 路由 + 绑定）/ 📜 入库台账（智源拉取批次落 project_ingest_logs）。
// 写回状态：zhiyuan_imported_id 非空 = 已写回 → 按钮置灰「✅ 已写回智源」（暂定规则：不可重复写回）；
//   归一修改会产生新版本（新 id），新版本自然恢复可写。
async function loadSysmlIngestBar(convId){
  const el = document.getElementById('preview-sysml-versions');
  if(!el || !convId){ if(el) el.innerHTML=''; return; }
  let pushBtn = '';
  try{
    const r = await api('/api/sysml-versions?conversation_id=' + convId);
    const vs = (r && r.versions) || [];
    // 写回对象 = 当前版本（current 且留有源码）；已替代/空码版本不提供写回
    const cur = vs.find(v=>v.status==='current' && (v.code_len||0) > 0) || null;
    if(cur){
      const pushed = String(cur.zhiyuan_imported_id || '').trim();
      const lbl = String(cur.version_label || '').replace(/['"\\]/g, '');
      pushBtn = pushed
        ? `<button class="btn sm ghost" disabled style="font-size:10px;padding:0 8px;color:var(--mut);cursor:not-allowed;" title="当前版本已写回智源（${esc(pushed)}）。暂定规则：已写回的版本不可重复写回；归一修改会产生新版本，新版本可再次写回。">✅ 已写回智源</button>`
        : `<button class="btn sm ghost" style="font-size:10px;padding:0 8px;color:var(--blue-d);border-color:var(--blue-bd,#B5D4F4);" onclick="sysmlPushModal(${cur.id},'${lbl}')" title="把当前版本 SysML v2 源码写入智源建模软件：先语法检测（只读），通过后覆盖导入；成功后「智源拉取」即可拉到本版本">🔗 写回智源</button>`;
    }
  }catch(e){ pushBtn = ''; }
  el.innerHTML = `<div style="margin-top:10px;padding-top:8px;border-top:1px dashed var(--line);text-align:left;display:flex;gap:4px;justify-content:flex-end;">
      ${pushBtn}
      <button class="btn sm ghost" style="font-size:10px;padding:0 8px;color:var(--blue-d);border-color:var(--blue-bd,#B5D4F4);" onclick="zhiyuanPullIngest()" title="从智源拉取当前工程建模数据，后端直接转三元组入个人图库（对话流回执）">⇩ 智源拉取</button>
      <button class="btn sm ghost" style="font-size:10px;padding:0 8px;" onclick="projectIngestLogsPanel()" title="本工程历次入库批次台账（智源拉取 / 旧工程入库）">📜 入库台账</button>
  </div>`;
}
// ── 写回智源（modal 确认 → push-zhiyuan；异常分类提示）──
// 复用 07-norm 归一页写回的确认范式（先检测后写入，二次点击才执行）；此处为入口条常驻入口。
function sysmlPushModal(vid, label){
  const ov = document.createElement('div');
  ov.id = 'svm-push-modal';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = `<div style="width:520px;background:#fff;border-radius:10px;box-shadow:0 10px 34px rgba(20,35,60,.2);overflow:hidden;font-size:12.5px;">
      <div style="padding:12px 16px;border-bottom:1px solid var(--line);font-weight:600;">🔗 写回智源建模软件（${esc(label||('v'+vid))}）</div>
      <div style="padding:14px 16px;line-height:1.95;">
        <div>版本控制串 vc（留空则用系统默认配置）：</div>
        <input id="svm-push-vc" style="width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:5px;font-size:12px;" placeholder="projectId,branchId">
        <div style="margin-top:6px;color:var(--mut);font-size:11px;">流程：语法检测（只读）→ 通过后覆盖导入智源。检测不通过 / 智源不可达均不会写入；写回成功后该版本标记「已写回」，不可重复写回。</div>
        <div id="svm-push-msg" style="margin-top:8px;"></div>
      </div>
      <div style="padding:10px 16px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn ghost" onclick="document.getElementById('svm-push-modal').remove()">取消</button>
        <button class="btn primary" id="svm-push-btn" onclick="sysmlPushGo(${vid})">确认写入</button>
      </div></div>`;
  document.body.appendChild(ov);
}
async function sysmlPushGo(vid){
  const vc = (document.getElementById('svm-push-vc')||{}).value || '';
  const box = document.getElementById('svm-push-msg');
  const btn = document.getElementById('svm-push-btn');
  if(btn){ btn.disabled = true; btn.textContent = '⏳ 检测并写入中…'; }
  if(box) box.innerHTML = `<span style="color:var(--mut);">⏳ 正在语法检测并写入智源…</span>`;
  let r;
  try{
    r = await api('/api/sysml-versions/' + vid + '/push-zhiyuan', {method:'POST', body: JSON.stringify({vc: vc})});
  }catch(e){
    if(box) box.innerHTML = `<div style="padding:8px 10px;border-radius:6px;border:1px solid #F3C1C1;background:#fff5f5;color:#b91c1c;">✕ 请求失败：${esc(e.message||String(e))}</div>`;
    if(btn){ btn.disabled=false; btn.textContent='确认写入'; }
    return;
  }
  if(btn){ btn.disabled=false; btn.textContent='确认写入'; }
  if(!box) return;
  const errHtml = (msg, hint)=>`<div style="padding:8px 10px;border-radius:6px;border:1px solid #F3C1C1;background:#fff5f5;color:#b91c1c;">✕ ${esc(msg||'写入失败')}${hint?`<div style="margin-top:4px;color:#6b7280;">${hint}</div>`:''}</div>`;
  if(r && r.ok){
    box.innerHTML = `<div style="padding:8px 10px;border-radius:6px;border:1px solid #C0DD97;background:#f7fbee;color:#2f855a;">✅ 已写入智源建模软件（${esc(r.vc||'')}）</div>`;
    toast('✅ 已写回智源');
    setTimeout(()=>{ const m=document.getElementById('svm-push-modal'); if(m) m.remove(); }, 1200);
    if(_artConv) loadSysmlIngestBar(_artConv);   // 刷新入口条 → 「✅ 已写回智源」置灰
    return;
  }
  const code = (r && r.code) || '';
  if(code==='ALREADY_PUSHED'){
    box.innerHTML = errHtml(r && r.error, '该版本此前已写回过；归一修改产生新版本后，可对新版本写回。');
  }else if(code==='CHECK_FAILED'){
    const det = String((r && r.detail) || '').slice(0, 160);
    box.innerHTML = errHtml('语法检测未通过，未执行写入', det ? esc(det) : '请修正代码后重试');
  }else if(code==='CALL_FAILED' || code==='CONFIG_MISSING' || code==='VC_REQUIRED'){
    box.innerHTML = errHtml(r && r.error, '智源服务不可达或配置缺失——请确认智源服务状态与连接配置后再试');
  }else{
    box.innerHTML = errHtml((r && (r.error || r.detail)) || '写入失败');
  }
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
      if(_artConv) loadSysmlIngestBar(_artConv);
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
        if(_artConv) loadSysmlIngestBar(_artConv);
        return;
      }
      toast(`✅ 候选已确认（入库 ${r.confirmed||0} 条${(r.skipped&&r.skipped.length)?` / 跳过 ${r.skipped.length}`:''}），进入实体/关系审核队列正式审核${_gateMsg}`);
      // 反馈捕获：确认 = 采纳（有消息上下文时隐式记录）
      if(versionId){ try{
        const vr = await api('/api/sysml-versions/' + versionId);
        if(vr && vr.message_id) api(`/api/messages/${vr.message_id}/feedback`, {method:'POST', body:JSON.stringify({type:'approve'})}).catch(()=>{});
      }catch(e){} }
      if(_artConv) loadSysmlIngestBar(_artConv);
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
// 2026-09-24（方案A收尾）：拉取升级为工程维度路由——弹窗确认工程与 vc 绑定，
// 不再默认弹 confirm 硬拉：vc 显式输入一次即绑定本工程（projects.tool_binding），下次免输。
// 2026-09-24 建模工具适配层：绑定泛化 {tool,ref,name}（zhiyuan/magicdraw）——
// 非智源工具 → 显示"通道预留"面板（连接器接入前不可拉取，绑定已登记）。
function _zpParseBinding(proj){
  let b = null;
  try{ b = proj && proj.tool_binding ? JSON.parse(proj.tool_binding) : null; }catch(e){ b = null; }
  if(b && b.tool) return b;
  if(proj && (proj.zhiyuan_vc||'').trim()) return {tool:'zhiyuan', ref:proj.zhiyuan_vc, name:proj.zhiyuan_project_name||''};
  return null;
}
const ZP_PULL_TOOL_LABEL = {zhiyuan:'智源', magicdraw:'MagicDraw'};   // ⚠ 不得与 41-projects.js 的全局同名（经典 script 全局词法作用域共享，重名 = 后加载的整个文件失效）
async function zhiyuanPullIngest(){
  if(!currentConvId){ toast('缺少会话上下文'); return; }
  // 会话 → 工程反查（拉取是工程维度动作）
  let conv = null, proj = null;
  try{ conv = await api('/api/conversations/' + currentConvId); }catch(e){}
  const pid = (conv && conv.project_id) || '';
  if(pid){
    try{ proj = await api('/api/projects/' + encodeURIComponent(pid)); }catch(e){}
  }
  const b = _zpParseBinding(proj);
  const projName = (proj && proj.name) || '';
  // 非智源工具 → 通道预留面板（连接器未接入，明确告知而非误导性报错）
  if(b && b.tool && b.tool !== 'zhiyuan'){
    const tl = ZP_PULL_TOOL_LABEL[b.tool] || b.tool;
    openPanel('⇩ 模型拉取 — ' + (projName || '（会话未关联工程）'),
      `<div style="font-size:12px;line-height:1.9;">
        <div style="padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font-size:11.5px;color:var(--mut);">
          目标工程：<b style="color:var(--ink);">${esc(projName||'-')}</b> · 绑定建模工具：<b style="color:var(--ink);">${tl}</b>${b.name?`（${esc(b.name)}）`:''}
        </div>
        <div style="margin-top:10px;padding:10px 12px;border:1px solid #F3D9A4;background:#fffbf0;border-radius:8px;font-size:11.5px;color:#8a5a10;line-height:1.8;">
          ℹ️ <b>${tl}</b> 的拉取连接器尚未接入——绑定已登记（ref=${esc(b.ref)}），无需重新绑定。<br>
          MagicDraw 通道按 08-31 设计分三层：<b>文件导入</b>（.kerml/.profile，MVP）→ <b>SysML v2 API</b>（ISO 标准）→ <b>Cameo OpenAPI 插件</b>（深度集成）。<br>
          任一通道接入后，本工程的写回 / 拉取将自动按绑定路由。
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
          <button class="btn ghost" onclick="closePanel()">知道了</button>
        </div>
      </div>`);
    return;
  }
  const boundVc = (b && b.tool === 'zhiyuan' && b.ref) || '';
  openPanel('⇩ 智源拉取 — ' + (projName || '（会话未关联工程）'),
    `<div style="font-size:12px;line-height:1.8;">
      <div style="padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font-size:11.5px;color:var(--mut);">
        后端将把该工程在<b style="color:var(--ink);">智源</b>中的建模数据：解析 → 候选化 → 融合闸 → 转三元组 → 入库<b style="color:var(--ink);">个人分支图库</b>。<br>
        （不产生数据治理审核面板待办，回执在本对话流显示，批次记录进「入库台账」）
      </div>
      <div style="margin:10px 0 4px;">目标工程</div>
      <div style="padding:6px 10px;border:1px solid var(--line);border-radius:8px;font-size:12px;">
        ${projName ? esc(projName) : '<span style="color:var(--red);">✕ 当前会话未关联工程——请先在工程页归属会话</span>'}
      </div>
      <div style="margin:10px 0 4px;">智源 vc（branchId）</div>
      <input id="zp-vc-input" type="text" style="width:100%;box-sizing:border-box;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);font-size:12px;" placeholder="${boundVc ? '' : '未绑定——填写该工程在智源的 vc，填一次即长期绑定本工程'}"
             value="${esc(boundVc)}" ${boundVc ? '' : ''}>
      <div style="margin-top:4px;font-size:10.5px;color:var(--mut);">
        ${boundVc ? '✅ 本工程已绑定 vc，可直接拉取；修改输入框可改绑' : '⚠ 本工程尚未绑定智源 vc，不绑定无法确定拉取目标（防止拉错工程数据）'}
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn ghost" onclick="closePanel()">取消</button>
        <button class="btn primary" id="zp-go-btn" onclick="zhiyuanPullGo(${JSON.stringify(pid).replace(/"/g,'&quot;')})">⇩ 开始拉取</button>
      </div>
    </div>`);
}
async function zhiyuanPullGo(projectId){
  const vcEl = document.getElementById('zp-vc-input');
  const vc = (vcEl && vcEl.value || '').trim();
  if(!projectId){ toast('会话未关联工程，无法拉取'); return; }
  const btn = document.getElementById('zp-go-btn');
  if(btn){ btn.disabled = true; btn.textContent = '⏳ 拉取中…'; }
  closePanel();
  toast('⇩ 正在从智源拉取建模数据并直通入库…');
  let r;
  try{
    r = await api('/api/knowledge/sysml/pull-ingest', {method:'POST', body:JSON.stringify({conversation_id: currentConvId, vc: vc})});
  }catch(e){ toast('拉取失败：'+(e.message||'')); return; }
  if(!r || r.error){ toast('拉取失败：'+((r&&r.error)||'')); _zhiyuanReceipt(r); return; }
  _zhiyuanReceipt(r);
  if(_artConv) loadSysmlIngestBar(_artConv);
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
    const vcSrc = r.vc_source || '';
    const vcNote = vcSrc==='project' ? '（工程绑定 vc）'
      : vcSrc==='explicit' ? '（本次指定并已绑定工程）'
      : vcSrc==='default' ? '<span style="color:var(--amb,#b45309);">（⚠ 默认 vc，非工程绑定——建议在拉取弹窗绑定本工程 vc）</span>' : '';
    body = '<div style="font-size:12px;line-height:1.8;">'
      + '<div style="margin-bottom:6px;"><span class="st ok" style="font-size:12px;padding:3px 12px;">✅ 智源拉取直通入库完成</span>'
      + '<span style="font-size:10.5px;color:var(--mut);margin-left:8px;">批次 '+esc(r.batch_id||'')+' · 工程 '+esc(r.project_name||'默认工程')+' · vc '+esc(r.vc||'')+' '+vcNote+' · 分支 '+esc(r.target_branch||'personal')+' · 耗时 '+((s.elapsed_ms/1000)||0).toFixed(1)+'s</span></div>'
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
