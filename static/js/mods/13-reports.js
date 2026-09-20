/* 报告：列表 / 查看 / 导出
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 6091-6433  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function loadCurrentProject(){
  const tag = document.getElementById('ai-project-tag');
  if(!tag) return;
  try{
    const p = await api('/api/projects/default');
    if(p && p.name){
      tag.classList.remove('nomatch');
      tag.innerHTML = `<span class="dot"></span> 当前工程：${esc(p.name)}${p.code?`（${esc(p.code)}）`:''}`;
    } else {
      tag.classList.add('nomatch');
      tag.innerHTML = `<span class="dot"></span> 暂未匹配工程`;
    }
  }catch(e){
    tag.classList.add('nomatch');
    tag.innerHTML = `<span class="dot"></span> 暂未匹配工程`;
  }
}
async function loadQuickBar(){
  try{
    const [agents, skills, teams] = await Promise.all([
      api('/api/studio/agents'), api('/api/studio/skills'), api('/api/studio/agent-teams')
    ]);
    _agentsCache = agents || _agentsCache;  // V3：供 @智能体弹窗/chip 复用
    _skillsCache = skills || _skillsCache;  // V3：供 /技能弹窗/chip 复用
    _teamsCache = teams || _teamsCache;     // 团队模式：主 Agent 团队（工作流下拉）数据源
    const aSel = document.getElementById('quick-agent');
    if(aSel && agents && agents.length){
      aSel.innerHTML = '<option value="">自动匹配</option>' + agents.map(a=>`<option value="${esc(a.name)}">${esc(a.display_name||a.name)}</option>`).join('');
      // V2.7 默认选中：上次选择的智能体（localStorage 记录），无记录/已失效 → 排序第一
      let last = '';
      try{ last = localStorage.getItem('mbse_last_agent') || ''; }catch(e){}
      const names = agents.map(a=>a.name);
      aSel.value = (last && names.includes(last)) ? last : names[0];
      // 记录上次选择（change 防重复绑定；@提及等程序赋值点需手动 dispatchEvent('change')）
      if(!aSel.dataset.lastBind){
        aSel.dataset.lastBind = '1';
        aSel.addEventListener('change', ()=>{
          try{ localStorage.setItem('mbse_last_agent', aSel.value || ''); }catch(e){}
          if(typeof renderChips === 'function') renderChips();
        });
      }
      if(typeof renderChips === 'function') renderChips();
    }
    const sSel = document.getElementById('quick-skill');
    if(sSel && skills && skills.length){
      sSel.innerHTML = '<option value="">自动匹配</option>' + skills.map(s=>`<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('');
    }
    const tSel = document.getElementById('quick-team');
    if(tSel){
      if(teams && teams.length){
        // 团队按主 Agent 划分：每个主 Agent 即一个智能体团队（工作流）
        tSel.innerHTML = '<option value="">自动（不指定团队）</option>' + teams.map(t=>`<option value="${esc(t.name)}">👑 ${esc(t.display_name||t.name)}${t.team_count?`（${t.team_count}人）`:''}</option>`).join('');
        tSel.title = '选择智能体团队（工作流）：主 Agent 负责意图识别/任务拆分/计划制定/任务分派/内容整合输出';
      } else {
        tSel.innerHTML = '<option value="">无智能体团队</option>';
        tSel.title = '暂无主 Agent 团队，请到「Agent 管理」创建主 Agent 并配置成员';
      }
    }
  }catch(e){ /* 候选加载失败静默：快捷条仍可用（可手输） */ }
}
let _chatProvidersLoaded = false;
let _chatProvidersCache = [];   // 2026-09-04 缓存模型列表，供附件上传时判断当前模型能力（vision 等）
async function loadChatProviders(){
  try{
    const providers = await api('/api/llm/providers');
    _chatProvidersCache = providers || [];
    const sel = document.getElementById('llm-providers');
    const chatOnes = (providers||[]).filter(p=>p.model_type==='chat' && p.status!=='disabled');  // 对话模型：向量模型不进选择器
    if(sel){
      if(chatOnes.length){
        const def = chatOnes.find(p=>p.is_default) || chatOnes[0];
        sel.innerHTML = '<option value="">默认模型</option>' + chatOnes.map(p=>`<option value="${p.id}" ${p.id===def.id?'selected':''}>${esc((p.name||'')+' · '+(p.model_name||'-'))}</option>`).join('');
      } else {
        sel.innerHTML = '<option value="">未配置模型</option>';
      }
      _chatProvidersLoaded = true;
    }
  }catch(e){ /* 加载失败静默：保留「默认模型」选项 */ }
}
// ── 报告下载（通用报告服务 → 多格式导出 md/docx/pdf）──
// 从当前激活预览 tab 导出报告（分节卡片视图上的导出按钮）
function reportTabExport(fmt){
  const tb = _previewTabs.find(t=>t.key===_previewActiveKey);
  if(!tb || !tb.meta || !tb.meta.sections){ toast('暂无报告可导出'); return; }
  window._lastReport = {title:tb.title||'报告', sections:tb.meta.sections, summary:tb.meta.summary||'', report_type:tb.meta.report_type||'analysis', meta:tb.meta.meta||{}};
  downloadReport(fmt);
}
async function downloadReport(fmt){
  const rep = window._lastReport;
  if(!rep){ toast('暂无可下载的报告'); return; }
  fmt = fmt || 'md';
  if(fmt === 'md'){
    let md = `# ${rep.title || '报告'}\n\n`;
    const meta = rep.meta || {};
    if(meta.doc_no) md += `| 文档编号 | 版本 | 日期 | 密级 | 编制 |\n| --- | --- | --- | --- | --- |\n| ${meta.doc_no||'-'} | ${meta.version||'-'} | ${meta.date||'-'} | ${meta.classification||'-'} | ${meta.author||'-'} |\n\n`;
    (rep.sections || []).forEach(s=>{
      md += `## ${s.heading}\n\n${s.body || ''}\n\n`;
      if(s.table) md += s.table + '\n\n';
    });
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (rep.title || 'report').replace(/[\\/:*?"<>|]/g, '_') + '.md';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 300);
    toast('报告已下载（Markdown）');
    return;
  }
  // docx / pdf：调后端报告导出服务生成二进制文件流
  try{
    const resp = await fetch('/api/report/export', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title: rep.title||'报告', sections: rep.sections||[], summary: rep.summary||'', report_type: rep.report_type||'', meta: rep.meta||{}, fmt})
    });
    if(!resp.ok){ const err = await resp.json().catch(()=>({})); toast('导出失败：' + (err.detail || resp.status)); return; }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (rep.title || 'report').replace(/[\\/:*?"<>|]/g, '_') + '.' + fmt;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 300);
    toast('报告已下载（' + fmt.toUpperCase() + '）');
  }catch(e){ toast('导出失败：' + (e.message||e)); }
}
// ── 报告（AI 建模报告统一归档 / 检索 / 导出）──
let _rpt = { page:1, size:15 };
async function loadReports() {
  const type = document.getElementById('rpt-type')?.value || '';
  const kw = (document.getElementById('rpt-search')?.value || '').trim();
  try{
    const rows = await api(`/api/reports?report_type=${encodeURIComponent(type)}&keyword=${encodeURIComponent(kw)}`);
    const el = document.getElementById('reports-table');
    const cnt = document.getElementById('rpt-count');
    if(cnt) cnt.textContent = rows.length ? `共 ${rows.length} 份` : '';
    const total = rows.length;
    const pages = Math.max(1, Math.ceil(total/_rpt.size));
    if(_rpt.page > pages) _rpt.page = pages;
    const items = rows.slice((_rpt.page-1)*_rpt.size, _rpt.page*_rpt.size);
    const typeCls = {impact:'b', review:'w', analysis:'ok', other:'g'};
    el.innerHTML = `<table class="t">
      <tr><th>标题</th><th>类型</th><th>来源</th><th>生成人</th><th>生成时间</th><th>状态</th><th>操作</th></tr>` +
      (items.length ? items.map(r=>`<tr>
        <td><b style="cursor:pointer;color:var(--blue-d);" onclick="viewReport(${r.id})">${esc(r.title)}</b></td>
        <td><span class="st ${typeCls[r.report_type]||'g'}">${esc(r.report_type_label||r.report_type)}</span></td>
        <td style="font-size:11px;color:var(--mut);">${esc(r.source||'-')}</td>
        <td style="font-size:11px;">${esc(r.created_by||'-')}</td>
        <td style="font-size:11px;color:var(--mut);">${esc((r.created_at||'').slice(0,16).replace('T',' '))}</td>
        <td><span class="st ${r.status==='final'?'ok':'g'}">${r.status==='final'?'已定稿':'草稿'}</span></td>
        <td style="white-space:nowrap;">
          <button class="btn sm ghost" onclick="viewReport(${r.id})">查看</button>
          <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'md')">⬇MD</button>
          <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'docx')">⬇Word</button>
          <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'pdf')">⬇PDF</button>
          ${r.status==='draft'?`<button class="btn sm ghost" onclick="finalizeReport(${r.id},'${esc(r.title).replace(/'/g,"\\'")}')">定稿</button>`:''}
          <button class="btn sm red" onclick="deleteStoredReport(${r.id})">删除</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="7" style="text-align:center;color:var(--mut);padding:18px;">暂无报告 — 在 AI 建模对话中生成「变更影响分析 / 预评审 / 模型分析」报告后将自动归档至此</td></tr>') +
    '</table>';
    renderPagerBar({
      el: document.getElementById('reports-pager'), total, page: _rpt.page, size: _rpt.size,
      onPage: p => { _rpt.page = p; loadReports(); },
      onSize: s => { _rpt.size = s; _rpt.page = 1; loadReports(); }
    });
  }catch(e){ toast('加载报告失败'); }
}
async function viewReport(id){
  try{
    const r = await api('/api/reports/' + id);
    if(r.error || r.detail){ toast(r.error || r.detail || '加载失败'); return; }
    const meta = {doc_no: r.doc_no||'', version:r.version||'', date:(r.created_at||'').slice(0,10), classification:'', author:r.created_by||''};
    let html = `<div class="note" style="margin-bottom:12px;">类型：${esc(r.report_type_label||r.report_type)} ｜ 来源：${esc(r.source||'-')} ｜ 生成人：${esc(r.created_by||'-')} ｜ ${esc((r.created_at||'').slice(0,16).replace('T',' '))} ｜ 状态：${r.status==='final'?'已定稿':'草稿'}</div>`;
    if(r.summary) html += `<div class="report-sec" style="margin-bottom:12px;"><div class="report-sec-h"><span class="report-sec-n">📌</span><span class="report-sec-t">摘要</span></div><div class="report-sec-b">${esc(r.summary)}</div></div>`;
    html += reportSectionsHtml(r.sections||[], meta);
    html += `<div style="display:flex;gap:6px;margin-top:14px;flex-wrap:wrap;">
      <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'md')">⬇ Markdown</button>
      <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'docx')">⬇ Word</button>
      <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'pdf')">⬇ PDF</button>
      ${r.status==='draft'?`<button class="btn sm" onclick="finalizeReport(${r.id},'${esc(r.title).replace(/'/g,"\\'")}')">✅ 定稿</button>`:''}
    </div>`;
    openPanel('📄 ' + (r.title||'报告详情'), html);
  }catch(e){ toast('加载报告详情失败'); }
}
async function exportStoredReport(id, fmt){
  try{
    const resp = await fetch(`/api/reports/${id}/export?fmt=${fmt}`, {method:'POST'});
    if(!resp.ok){ const err = await resp.json().catch(()=>({})); toast('导出失败：' + (err.detail || resp.status)); return; }
    const blob = await resp.blob();
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    const fname = m ? decodeURIComponent(m[1]) : ('report.' + fmt);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 300);
    toast('报告已导出（' + fmt.toUpperCase() + '）');
  }catch(e){ toast('导出失败：' + (e.message||e)); }
}
async function finalizeReport(id, title){
  if(!(await confirmDialog(`确认将报告「${title}」定稿？\n定稿后不可再编辑。`, {title:'报告定稿', okText:'定稿'}))) return;
  const r = await api('/api/reports/' + id);
  if(r.error || r.detail){ toast(r.error || r.detail || '加载失败'); return; }
  const res = await api('/api/reports/' + id, {method:'PUT', body:JSON.stringify({title:r.title, report_type:r.report_type, summary:r.summary||'', sections:r.sections||[], status:'final'})});
  if(res.error){ toast(res.error); return; }
  toast('已定稿'); await loadReports();
}
async function deleteStoredReport(id){
  if(!(await confirmDialog('确认删除该报告？\n删除后不可恢复。', {title:'删除报告', okText:'删除'}))) return;
  const r = await api('/api/reports/' + id, {method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('报告已删除'); await loadReports();
}
function finishStream(r, aiBox) {
  // P1-1 流式结束：恢复发送按钮
  showStopBtn(false); _streaming = false; _stoppedManually = false;
  // 内容级澄清收尾：done 用 renderMessage 重建会清掉交互卡 → 重建后重新插入澄清卡
  if(r && r.msg_type === 'clarify'){
    const _pb0 = document.getElementById('proc-box');
    if(_pb0) _pb0.querySelectorAll('.proc-block').forEach(b=>b.classList.add('collapsed'));
    const procHtml = _pb0?.innerHTML || '';
    const aiMsg = {role:'assistant', content:r.content||'', msg_type:'text',
                   card_data:'{}', id:r.message_id, process_html: procHtml};
    aiBox.outerHTML = renderMessage(aiMsg);
    const newEl = r.message_id ? document.getElementById('msg-' + r.message_id) : null;
    const nb = newEl ? newEl.querySelector('.body') : null;
    if(nb) renderClarifyAsk({questions: r.questions||[]}, nb);
    const _a2 = document.getElementById('chat-area'); if(_a2) _a2.scrollTop = _a2.scrollHeight;
    return;
  }
  // 流式结束：替换为完整 renderMessage（含执行过程块 + 卡片 + HIL 按钮 + 来源信息）
  const cardTypeMap = {impact:'card_impact', review:'card_review', requirement_analysis:'card_candidates'};
  // 流式结束：全部步骤已完成 → 统一收口（V2.6：默认收起时间线，只留「✓ 已完成」汇总条，可展开按层级回看）
  const _pb = document.getElementById('proc-box');
  if(_pb) _pb.querySelectorAll('.proc-block').forEach(b=>b.classList.add('collapsed'));
  const procHtml = _pb?.innerHTML || '';  // V2.4 保留会话内执行过程（完整时间线，历史展开时还原）
  // V2.6：标记完成 → 收口条切「✓ 已完成 · N 环节 · 耗时 · token」
  let _sumTxt = '';
  try{
    _procS.done = true; _procS.endTime = Date.now();
    _procS.summaryCollapsed = true;
    const _llm = r.llm || {};
    const _tk = _llm.tokens || {};
    const _used = (r.card && r.card.run_budget && r.card.run_budget.used_tokens) || 0;
    let _tokTxt = '';
    if(_tk.prompt || _tk.completion) _tokTxt = ` · ⚡ 入 ${_tk.prompt||0} / 出 ${_tk.completion||0} tok`;
    else if(_used) _tokTxt = ` · ⚡ ≈${_used} tok`;
    const _failN = _procS.timeline.filter(x=>x.status==='failed').length;
    _sumTxt = `✓ 已完成 · ${_procS.timeline.length} 环节 · 耗时 ${_procElapsedText()}${_tokTxt}${_failN?` · ${_failN} 失败`:''}`;
    stopElapsedTimer();
  }catch(e){}
  // 历史消息执行过程：收口条 + 默认收起的时间线容器（点击展开按层级回看）
  const histProcHtml = procHtml
    ? `<div class="hist-proc"><div class="proc-summary collapsed" onclick="histProcToggle(this)">${_sumTxt||'✓ 已完成'}<span class="chev">▾</span></div><div class="proc-timeline">${procHtml}</div></div>`
    : '';
  const aiMsg = {role:'assistant', content:r.content, msg_type:cardTypeMap[r.intent]||'text', card_data:JSON.stringify(r.card||r.retrieval||{}), id:r.message_id, process_html: histProcHtml};
  aiBox.outerHTML = renderMessage(aiMsg);
  // 闭环：附件解析状态 + 匹配工作流卡片（插入 AI 消息后）
  let extra = '';
  if(r.attachments_info && r.attachments_info.parsed > 0){
    let atag = `📎 已解析 ${r.attachments_info.parsed} 份上传资料并注入分析`;
    if(r.retrieval && r.retrieval.attachment_used) atag += `，本次消费 ${r.attachments_info.consumed||0} 条资料片段`;
    atag += (r.attachments_info.skipped||[]).length ? `（${r.attachments_info.skipped.length} 份无法解析）` : '';
    extra += `<div style="margin:2px 0 6px 44px;font-size:11px;color:var(--grn);">${atag}</div>`;
  }
  if(r.report && r.report.sections){
    window._lastReport = r.report;
    // 报告已由后端在落库时自动归档到报告（artifacts + reports 表），前端仅提供导出入口
    extra += `<div style="margin:2px 0 6px 44px;display:flex;gap:6px;flex-wrap:wrap;">
      <button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="downloadReport('md')">⬇ Markdown</button>
      <button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="downloadReport('docx')">⬇ Word</button>
      <button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="downloadReport('pdf')">⬇ PDF</button>
      <span class="st ok" style="align-self:center;" title="AI 建模产出报告已自动归档，可在「报告」统一检索/导出">📄 已归档</span>
    </div>`;
  }
  // P0 需求质量分析：V2.6 视觉降级——默认折叠为一行次要摘要（不再喧宾夺主），点击展开明细
  if(r.quality_report && r.quality_report.issues && r.quality_report.issues.length){
    const q = r.quality_report;
    const stats = q.stats||{};
    const sevColor = {high:'var(--red)', medium:'var(--amber)', low:'var(--grn)'};
    const qHtml = q.issues.map(it=>`<div style="padding:4px 0;border-bottom:1px solid var(--line);">
      <div style="font-size:11.5px;"><span style="color:${sevColor[it.severity]||'var(--mut)'};font-size:10px;margin-right:4px;">[${(it.severity||'').toUpperCase()}]</span>${esc(it.problem)}</div>
      <div style="color:var(--mut);font-size:10.5px;margin-top:2px;">📌 ${esc(it.evidence||'')}</div>
      <div style="color:var(--blue-d);font-size:10.5px;margin-top:2px;">💡 ${esc(it.suggestion||'')}</div></div>`).join('');
    const _qsum = `📋 需求质量参考：${q.issues.length} 条建议 · 评分 ${q.score||0}/100（需求 ${stats.total||0} 条 · 模糊 ${stats.fuzzy||0} · 缺量化 ${stats.no_measure||0}）`;
    extra += `<div style="margin:2px 0 6px 44px;">
      <div class="qr-summary" onclick="qrToggle(this)" style="display:flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:#fafbfc;font-size:11px;color:var(--mut);cursor:pointer;user-select:none;">
        <span class="chev" style="font-size:9px;transition:transform .2s;">▸</span>${_qsum}<span style="font-size:10px;color:var(--blue-d);text-decoration:underline;">明细</span></div>
      <div class="qr-detail" style="display:none;border:1px solid var(--line);border-radius:6px;padding:6px 10px;background:#fafbfc;font-size:11px;margin-top:4px;max-height:260px;overflow:auto;">${qHtml}</div>
    </div>`;
  }
  if(extra) document.getElementById('chat-area').insertAdjacentHTML('beforeend', extra);
  // 本次会话信息
  document.getElementById('chat-intent').textContent = r.intent;
  document.getElementById('chat-agent').textContent = r.agent||'-';
  document.getElementById('chat-hil').textContent = r.hil_level||'-';
  const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = `${r.agent||''} 已响应 ｜ 意图：${r.intent} ｜ HIL ${r.hil_level||'-'} ｜ 来源：${r.retrieval?.source||'-'}（图谱${r.retrieval?.graph_count||0}/向量${r.retrieval?.vector_count||0}）`;
  if(r.retrieval) {
    const kbHtml = (r.kb_tags||[]).length ? `<div class="src"><span class="dot b"></span>知识库引用：${r.kb_tags.map(t=>'#'+t).join(' ')}</div>` : '';
    document.getElementById('chat-sources').innerHTML = `
      ${kbHtml}
      <div class="src"><span class="dot g"></span>图谱检索：${r.retrieval.graph_count} 条</div>
      <div class="src"><span class="dot v"></span>向量补充：${r.retrieval.vector_count} 条</div>
      <div style="font-size:11px;color:var(--mut);">置信度：${(r.retrieval.confidence||0).toFixed(2)}</div>`;
  }
  // 会话产物分栏增量刷新（后端已归档本消息产物）
  if(currentConvId) loadArtifacts(currentConvId);
  const area = document.getElementById('chat-area');
  area.scrollTop = area.scrollHeight;
}
// V2.6 需求质量参考：折叠明细切换
function qrToggle(sumEl){
  const wrap = sumEl.closest('div[style*="margin:2px 0 6px 44px"]') || sumEl.parentElement;
  const det = wrap ? wrap.querySelector('.qr-detail') : null;
  if(!det) return;
  const open = det.style.display !== 'none';
  det.style.display = open ? 'none' : 'block';
  const ch = sumEl.querySelector('.chev');
  if(ch) ch.style.transform = open ? '' : 'rotate(90deg)';
}

// ── 问答可解释性：答案内 [n] 引用链接 → 来源详情面板（文档段落 + 关联模型元素）──
// 依赖 renderMessage 缓存到 window._msgCites[msgId] 的 citations（后端 chunk_hits 透传）
async function openCitation(sup){
  const msg = sup.closest('.msg');
  const mid = msg ? (msg.dataset.mid || '') : '';
  const i = parseInt(sup.dataset.i || '1', 10) - 1;
  const cites = (window._msgCites && mid && window._msgCites[mid]) || [];
  const c = cites[i];
  if(!c){ toast('引用来源不可用'); return; }
  const num = i + 1;
  const confCls = c.confidence_level==='高' ? 'ok' : (c.confidence_level==='中' ? 'a' : 'w');
  let html = `
    <div style="font-size:11px;color:var(--mut);margin-bottom:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
      <span class="st b">引用 ${num}</span>
      <span class="tag">${esc(c.source_doc||'-')}</span>
      ${c.section?`<span class="tag">${esc(c.section)}</span>`:''}
      <span class="tag">chunk#${c.chunk_index!==undefined?c.chunk_index:'-'}</span>
      ${c.confidence_level?`<span class="st ${confCls}">置信 ${esc(c.confidence_level)}</span>`:''}
      ${c.score!==undefined?`<span class="tag">score ${c.score}</span>`:''}
    </div>
    <h4 style="margin:8px 0 6px;font-size:13px;">📄 命中段落</h4>
    <div style="border:1px solid var(--line);border-radius:8px;padding:10px;background:#fffdf5;font-size:12.5px;line-height:1.7;max-height:200px;overflow:auto;white-space:pre-wrap;">${esc(c.content||'')}</div>`;
  openPanel(`引用来源 [${num}] · ${c.source_doc||'未知文档'}`, html + '<div class="loading">正在加载来源详情…</div>');
  // 拉取文档详情：定位命中段落上下文 + 关联模型元素（chunk↔实体溯源）
  if(c.document_id){
    try{
      const d = await api('/api/documents/' + c.document_id);
      let extra = '';
      const chunks = (d.chunks||[]).filter(x=>x.chunk_index!==undefined);
      const idx = chunks.findIndex(x=>Number(x.chunk_index)===Number(c.chunk_index));
      if(chunks.length){
        const from = Math.max(0, idx-1), to = Math.min(chunks.length, idx+2);
        const ctx = chunks.slice(from, to).map(x=>{
          const isCur = Number(x.chunk_index)===Number(c.chunk_index);
          return `<div style="margin-top:6px;padding:6px 9px;border-radius:6px;${isCur?'border:1px solid var(--blue-d);background:var(--blue-l);':'border:1px solid var(--line);background:#fafafa;'}" ${isCur?'id="cite-chunk"':''}>
            <div style="font-size:10.5px;color:var(--mut);margin-bottom:3px;">§${x.chunk_index}${isCur?' · 命中引用':' · 上下文'}</div>
            <div style="font-size:12px;line-height:1.65;color:${isCur?'var(--txt)':'var(--mut)'};">${esc((x.content||'').slice(0,260))}${(x.content||'').length>260?'…':''}</div>
          </div>`;
        }).join('');
        extra += `<h4 style="margin:14px 0 6px;font-size:13px;">📍 文档原文定位（§${c.chunk_index}）</h4><div style="max-height:300px;overflow:auto;">${ctx}</div>`;
      }
      // 模型元素：chunk 溯源链接的实体（viewEntity 展示图谱详情）
      const ents = d.linked_entities||[];
      if(ents.length){
        extra += `<h4 style="margin:14px 0 6px;font-size:13px;">🧬 关联模型元素（${ents.length}）</h4>
          <div style="display:flex;flex-direction:column;gap:4px;">` + ents.slice(0,20).map(e=>
          `<div style="display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;">
            <span class="st ${e.status==='reviewed'?'ok':e.status==='candidate'?'w':'g'}">${esc(e.status||'')}</span>
            <b>${esc(e.name)}</b><span class="tag">${esc(e.entity_type||'')}</span>
            <span style="margin-left:auto;"><button class="btn sm ghost" style="font-size:10.5px;padding:0 8px;" onclick="viewEntity('${e.id}')">查看</button></span>
          </div>`).join('') + '</div>';
      }
      const body = document.getElementById('panel-body');
      if(body) body.innerHTML = html + extra;
      const cur = document.getElementById('cite-chunk');
      if(cur) cur.scrollIntoView({block:'center'});
    }catch(e){
      const body = document.getElementById('panel-body');
      if(body) body.innerHTML = html + `<div style="color:var(--mut);font-size:11px;margin-top:10px;">来源详情加载失败：${esc(e.message||'')}</div>`;
    }
  }
}

// ── V3.0 AI 建模 SysML 版本链 / 入库（统一 v2g 候选治理入口）──
