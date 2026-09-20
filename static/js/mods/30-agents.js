/* Agent：提示词 / 技能 / MCP / 工具 / LLM
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 18755-20184  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
async function loadPrompts() {
  const items = await api('/api/studio/prompts');
  document.getElementById('prompt-table').innerHTML = `<table class="t">
    <tr><th>名称</th><th>场景</th><th>变量</th><th>版本</th><th>状态</th><th>操作</th></tr>` +
    items.map(p=>`<tr>
      <td><b>${p.name}</b></td><td>${p.scenario}</td><td>${JSON.parse(p.variables||'[]').length}</td>
      <td>${p.version}</td><td><span class="st ${p.status==='published'?'ok':'w'}">${p.status}</span></td>
      <td><button class="btn sm ghost" onclick="editPrompt(${p.id})">编辑</button>
      ${p.status!=='published'?`<button class="btn sm" onclick="publishPrompt(${p.id})">发布</button>`:''}
      <button class="btn sm red" onclick="deletePrompt(${p.id})">删除</button></td>
    </tr>`).join('') + '</table>';
}
async function savePrompt() {
  const name = document.getElementById('f-name').value;
  const scenario = document.getElementById('f-scenario').value;
  const content = document.getElementById('f-content').value;
  if(!name||!content) { toast('名称和内容必填'); return; }
  await api('/api/studio/prompts', {method:'POST', body:JSON.stringify({name, scenario, content, variables:['ontology_profile','linked_data_scope']})});
  closeModal(); toast('提示词已保存'); loadPrompts();
}
async function editPrompt(id) {
  const items = await api('/api/studio/prompts');
  const p = items.find(x=>x.id===id);
  if(!p) return;
  showModal('prompt');
  document.getElementById('f-name').value = p.name;
  document.getElementById('f-scenario').value = p.scenario;
  document.getElementById('f-content').value = p.content;
  // Override save to update
  window._editPromptId = id;
}
async function publishPrompt(id) {
  await api(`/api/studio/prompts/${id}/publish`, {method:'POST'});
  toast('已发布'); loadPrompts();
}
async function deletePrompt(id) {
  if(!(await confirmDialog('确认删除？'))) return;
  await api(`/api/studio/prompts/${id}`, {method:'DELETE'});
  toast('已删除'); loadPrompts();
}
async function loadSkills() {
  const items = await api('/api/studio/skills');
  _skillsCache = items || _skillsCache;  // V3：同步 /技能弹窗缓存
  // 状态筛选：按启用状态过滤（全部/启用/停用）
  const f = document.getElementById('skill-status-filter');
  const fv = f ? f.value : '';
  // 类型筛选：按内置标识过滤（全部/内置/自定义）
  const ft = document.getElementById('skill-type-filter');
  const ftv = ft ? ft.value : '';
  const filtered = items.filter(s => {
    if(fv==='enabled' && s.enabled===0) return false;
    if(fv==='disabled' && s.enabled!==0) return false;
    const builtin = s.builtin===1;
    if(ftv==='builtin' && !builtin) return false;
    if(ftv==='custom' && builtin) return false;
    return true;
  });
  // 卡片化：与 MCP / Agent 卡片风格一致（移除发布状态/发布按钮/触发词/依赖/白名单/分类展示）
  const box = document.getElementById('skill-table');
  if(!box) return;  // 旧 Skill 模板库已迁移到「技能」主菜单入口
  if(!filtered.length){
    box.innerHTML = `<div style="padding:36px;text-align:center;color:var(--mut);font-size:12px;">${items.length?'无匹配的 Skill（调整筛选条件）':'暂无 Skill'}</div>`;
    return;
  }
  box.innerHTML = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;">` +
    filtered.map(s=>`<div class="panel"><div class="ph">${esc(s.name)} ${s.builtin?'<span class="st b">内置</span>':''} <span class="st ${s.enabled===0?'r':'ok'}">${s.enabled===0?'停用':'启用'}</span></div>
    <div class="pb">
      <div style="font-size:11px;color:var(--mut);margin-bottom:6px;"><span class="st b">${esc(s.skill_type)}</span> v${esc(s.version||'1.0')}</div>
      <div style="font-size:12px;color:var(--mut);min-height:32px;">${esc(s.description||'-')}</div>

      <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;">
        <button class="btn sm ghost" onclick="editSkill(${s.id})">✏️</button>
        <button class="btn sm ${s.enabled===0?'ok':'ghost amb'}" onclick="toggleSkillStatus(${s.id},${s.enabled===0?1:0})">${s.enabled===0?'启用':'停用'}</button>
        ${s.builtin?'<span style="font-size:10.5px;color:var(--mut);align-self:center;">内置不可删</span>':(s.scope==='public'?'<span class="st" style="background:#e8f5e9;color:#2e7d32;align-self:center;">市场在售</span>':`<button class="btn sm ghost" onclick="marketPublishFrom('skill','${esc(s.name.replace(/'/g,"\\'"))}')">⬆ 发布</button><button class="btn sm red" onclick="deleteSkill(${s.id})">删除</button>`)}
      </div>
    </div></div>`).join('') + '</div>';
}
async function toggleSkillStatus(id, enabled) {
  await api(`/api/studio/skills/${id}/${enabled?'enable':'disable'}`, {method:'POST'});
  toast(enabled ? '已启用' : '已停用');
  loadSkills();
  loadMinePlugins();
  loadMarket();
  loadMarketManage();
}
let skillEditId = null;
let skillPrevEnabled = 1;
function editSkill(id) {
  skillEditId = id;
  showModal('skill');
  // 回填
  api('/api/studio/skills').then(items=>{
    const s = items.find(x=>x.id===id);
    if(!s) return;
    document.getElementById('f-name').value = s.name;
    document.getElementById('f-desc').value = s.description||'';
    document.getElementById('f-content').value = s.content||'';
    skillPrevEnabled = s.enabled===0 ? 0 : 1;
    const en = document.getElementById('f-enabled');
    if(en) en.checked = skillPrevEnabled === 1;
  });
}
function openSkillForm() {
  skillEditId = null;
  skillPrevEnabled = 1;
  showModal('skill');
  const en = document.getElementById('f-enabled');
  if(en) en.checked = true;
}
async function saveSkill() {
  const name = document.getElementById('f-name').value.trim();
  if(!name) { toast('名称必填'); return; }
  const body = {
    name,
    description: document.getElementById('f-desc').value,
    content: document.getElementById('f-content').value,
  };
  const enEl = document.getElementById('f-enabled');
  const wantEnabled = !enEl || enEl.checked ? 1 : 0;
  let sid = null;
  if(skillEditId) { await api('/api/studio/skills/'+skillEditId, {method:'PUT', body:JSON.stringify(body)}); sid = skillEditId; toast('已更新'); }
  else { const r = await api('/api/studio/skills', {method:'POST', body:JSON.stringify(body)}); sid = r && r.id; toast('已创建'); if(_mmCreate){ _mmCreate = null; await mmAutoPublish('skill', name); } }
  // 同步启用状态（后端 create/update 不接收 enabled，走独立 enable/disable 接口）
  if(sid && wantEnabled !== skillPrevEnabled) {
    await api('/api/studio/skills/'+sid+'/'+(wantEnabled?'enable':'disable'), {method:'POST'});
  }
  closeModal(); skillEditId = null; skillPrevEnabled = 1; loadSkills(); loadMinePlugins(); loadMarket(); loadMarketManage();
}
async function deleteSkill(id) {
  if(!(await confirmDialog('确认删除该 Skill？'))) return;
  await api(`/api/studio/skills/${id}`, {method:'DELETE'});
  toast('已删除'); loadSkills(); loadMinePlugins(); loadMarket(); loadMarketManage();
}
// 2026-09-17：草稿箱「✓ 发布」——此前调用的是从未定义的 publishSkill()，按钮点了报错。
//   走 /api/studio/skills/{sid}/publish（含依赖校验：依赖技能须存在、已发布且版本达标）。
async function publishSkill(id) {
  if(!(await confirmDialog('确认发布该草稿为可用技能？\n\n发布后 AI 即可消费该技能；依赖未满足时会被拒绝并给出原因。'))) return;
  try{
    const r = await api(`/api/studio/skills/${id}/publish`, {method:'POST', body:'{}'});
    if(r.error){ toast('发布失败：' + r.error); return; }
    toast('已发布');
    loadSkillCombined();
  }catch(e){ toast('发布失败：' + (e.message||e)); }
}
async function uploadSkill(input) {
  const file = input.files[0];
  if(!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/studio/skills/upload', {method:'POST', body:fd}).then(x=>x.json());
  input.value = '';
  if(r.error) { toast('解析失败：'+r.error); return; }
  // 预览确认后入库
  const ok = await confirmDialog(`技能包解析成功：\n名称：${r.name}\n描述：${r.description}\n触发词：${(r.triggers||[]).join(', ')||'-'}\n文件数：${r.file_count||0}\n\n确认入库？`, {title:'技能包解析成功', okText:'确认入库'});
  if(!ok) { _mmCreate = null; return; }
  const body = {
    name: r.name, description: r.description,
    triggers: r.triggers||[], category: r.category||'',
    content: r.content||'', skill_type: 'package',
    frontmatter: JSON.stringify(r),
  };
  await api('/api/studio/skills', {method:'POST', body:JSON.stringify(body)});
  if(_mmCreate){ _mmCreate = null; await mmAutoPublish('skill', body.name); }
  toast('技能包已入库'); loadSkills(); loadMinePlugins(); loadMarket(); loadMarketManage();
}
// 2026-08-11 优化：st-mcp 页内 Tab 切换（MCP 服务器 / 工具注册表），记忆上次停留页
let stmTab = 'stm-mcp';
function applyMCPTab() {
  document.querySelectorAll('#st-mcp .subtab span').forEach(s=>s.classList.toggle('on', s.dataset.tab===stmTab));
  const mcp = document.getElementById('stm-mcp-legacy'), tools = document.getElementById('stm-tools');
  if(mcp) mcp.classList.toggle('on', stmTab==='stm-mcp');
  if(tools) tools.classList.toggle('on', stmTab==='stm-tools');
}
async function loadMCPServers() {
  const all = await api('/api/studio/mcp-servers');
  const tabCount = document.getElementById('mcp-tab-count');
  if(tabCount) tabCount.textContent = all.length;
  // 状态筛选（启用/停用）+ 类型筛选（内置/自定义）
  const fs = document.getElementById('mcp-status-filter');
  const fsv = fs ? fs.value : '';
  const ft = document.getElementById('mcp-type-filter');
  const ftv = ft ? ft.value : '';
  const items = all.filter(s => {
    if(fsv==='enabled' && s.enabled===0) return false;
    if(fsv==='disabled' && s.enabled!==0) return false;
    const builtin = s.builtin===1;
    if(ftv==='builtin' && !builtin) return false;
    if(ftv==='custom' && builtin) return false;
    return true;
  });
  const sum = document.getElementById('mcp-summary');
  if(sum) sum.textContent = `共 ${items.length} 个服务器 · ${items.filter(s=>s.status==='online').length} 在线 · ${items.filter(s=>s.enabled!==0).length} 启用`;
  const box = document.getElementById('mcp-cards');
  if(!box) return;  // 旧 MCP 服务器管理已迁移到「MCP」主菜单入口
  if(!items.length){
    box.innerHTML = `<div style="grid-column:1/-1;padding:36px;text-align:center;color:var(--mut);font-size:12px;">${all.length?'无匹配的 MCP 服务器（调整筛选条件）':'暂无 MCP 服务器，点击「＋ 添加 MCP 服务器」创建'}</div>`;
    return;
  }
  box.innerHTML = items.map(s=>{
    const resN = (()=>{ try{return JSON.parse(s.resources||'[]').length;}catch(e){return 0;} })();
    const prmN = (()=>{ try{return JSON.parse(s.prompts||'[]').length;}catch(e){return 0;} })();
    const toolN = (()=>{ try{return JSON.parse(s.tools||'[]').length;}catch(e){return 0;} })();
    const proto = s.protocol_version?`<span class="st b">${s.protocol_version}</span>`:'';
    const err = s.last_error?`<div style="font-size:11px;color:var(--red);margin-top:4px;" title="${esc(s.last_error)}">⚠ ${esc(s.last_error).slice(0,80)}</div>`:'';
    const statusBadge = s.status==='online' ? '<span class="st ok">online</span>' : '';
    return `<div class="panel"><div class="ph">${s.name} ${s.builtin?'<span class="st b">内置</span>':''} ${statusBadge} <span class="st ${s.enabled===0?'r':'ok'}">${s.enabled===0?'停用':'启用'}</span> ${proto}</div>
    <div class="pb"><div class="kv"><span>端点</span><b>${s.endpoint}</b></div>
    <div class="kv"><span>传输</span><b>${s.transport||'sse'}</b></div>
    <div class="kv"><span>清单</span><b>${toolN} 工具 / ${resN} 资源 / ${prmN} 提示词</b></div>
    <div class="kv"><span>延迟</span><b>${s.latency_ms}ms</b> <span style="font-size:11px;color:var(--mut);">巡检 ${(s.last_check||'').slice(5,19)||'-'}</span></div>
    ${err}
    <div style="margin-top:6px;display:flex;gap:6px;">
      <button class="btn sm ghost" onclick="testMCP(${s.id})">🔌 测试</button>
      <button class="btn sm ghost" onclick="editMCP(${s.id})">✏️</button>
      <button class="btn sm ${s.enabled===0?'ok':'ghost amb'}" onclick="toggleMCPStatus(${s.id},${s.enabled===0?1:0})">${s.enabled===0?'启用':'停用'}</button>
      ${s.builtin?'<span style="font-size:10.5px;color:var(--mut);align-self:center;">内置不可删</span>':(s.scope==='public'?'<span class="st" style="background:#e8f5e9;color:#2e7d32;align-self:center;">市场在售</span>':`<button class="btn sm ghost" onclick="marketPublishFrom('mcp','${esc(s.name.replace(/'/g,"\\'"))}')">⬆ 发布</button><button class="btn sm red" onclick="deleteMCP(${s.id})">删除</button>`)}
    </div></div></div>`;
  }).join('');
}
async function toggleMCPStatus(id, enabled) {
  await api(`/api/studio/mcp-servers/${id}/${enabled?'enable':'disable'}`, {method:'POST'});
  toast(enabled ? '已启用' : '已停用');
  loadMCPServers();
  loadMinePlugins();
  loadMarket();
  loadMarketManage();
}
async function readMCPResource(sid, uri) {
  if(!uri) return;
  try {
    const r = await api(`/api/studio/mcp-servers/${sid}/read-resource?uri=${encodeURIComponent(uri)}`);
    const ok = r.ok!==false && !r.is_error;
    toast(ok ? '资源读取成功' : '资源读取失败：'+(r.result||'').slice(0,80));
    if(ok) { const body = document.getElementById('mcp-detail-body'); if(body) body.innerHTML += `<div class="panel" style="margin-top:8px;"><div class="ph">资源内容</div><pre style="white-space:pre-wrap;font-size:11px;max-height:220px;overflow:auto;margin:0;">${esc(r.result)}</pre></div>`; }
  } catch(e) { toast('❌ 读取失败：'+e.message); }
}
let mcpEditId = null;
function editMCP(id) {
  toolEditId = id;
  showModal('tool');
  document.getElementById('tool-modal-title').textContent = '编辑工具';
  api('/api/studio/mcp-servers').then(items=>{
    const s = items.find(x=>x.id===id);
    if(!s) return;
    document.getElementById('f-tool-type').value = 'mcp';
    toggleToolType();
    document.getElementById('f-tool-name').value = s.name;
    document.getElementById('f-tool-name').disabled = true;
    document.getElementById('f-tool-desc').value = '';
    document.getElementById('f-tool-transport').value = s.transport||'streamable_http';
    document.getElementById('f-tool-url-mcp').value = s.endpoint||'';
    addToolHeader();
  });
}
async function testMCP(id) {
  toast('正在测试连通…');
  try {
    const r = await api(`/api/studio/mcp-servers/${id}/test`, {method:'POST'});
    if(r.ok!==false) toast(`✅ 连通成功：${r.tools.length} 工具 / ${r.latency_ms}ms${r.protocol?' / 协议 '+r.protocol:''}`);
    else toast(`❌ 失败：${r.error||r.note||''}`);
  } catch(e) { toast('❌ 测试失败：'+e.message); }
  loadMCPServers();
}
async function deleteMCP(id) {
  if(!(await confirmDialog('确认删除？'))) return;
  await api(`/api/studio/mcp-servers/${id}`, {method:'DELETE'});
  toast('已删除'); loadMCPServers(); loadMinePlugins(); loadMarket(); loadMarketManage();
}
// ── P0 平台化：Agent 管理 ──
let agentEditId = null;
let agentRunId = null;
// 优化1：工具调用日志（可观测）——独立 Tab + 筛选（Agent/类别/工具类型/成败）
let agentTab = 'sta-agents';
function switchAgentTab(el, id) {
  agentTab = id;
  document.querySelectorAll('#st-agent .subtab span').forEach(s=>s.classList.toggle('on', s.dataset.tab===id));
  const agents = document.getElementById('sta-agents'), logs = document.getElementById('sta-logs');
  if(agents) agents.classList.toggle('on', id==='sta-agents');
  if(logs) logs.classList.toggle('on', id==='sta-logs');
  if(id==='sta-logs') { loadLogFilterAgents(); loadToolLogs(); }
}
async function loadLogFilterAgents() {
  const sel = document.getElementById('log-filter-agent');
  if(!sel || sel.options.length > 1) return;
  try {
    const items = await api('/api/studio/agents');
    sel.innerHTML = '<option value="">全部 Agent</option>' +
      items.map(a=>`<option value="${esc(a.display_name||a.name)}">${esc(a.display_name||a.name)}</option>`).join('');
  } catch(e) {}
}
let _logs = { page:1, size:15 };
function logApplyFilter() { _logs.page = 1; loadToolLogs(); }
async function loadToolLogs() {
  const el = document.getElementById('tool-logs');
  const g = id => document.getElementById(id)?.value || '';
  const kind = g('log-filter-kind'), type = g('log-filter-type'), ok = g('log-filter-ok');
  const agent = encodeURIComponent(g('log-filter-agent'));
  const params = new URLSearchParams({page:_logs.page, limit:_logs.size});
  if(agent) params.set('agent', agent);
  if(kind) params.set('call_kind', kind);
  if(type) params.set('tool_type', type);
  if(ok !== '') params.set('ok', ok);
  try {
    const r = await api('/api/studio/tool-logs?' + params.toString());
    const logs = Array.isArray(r) ? r : ((r||{}).items||[]);
    const total = Array.isArray(r) ? r.length : ((r||{}).total||0);
    const cnt = document.getElementById('log-tab-count');
    if(cnt) cnt.textContent = total;
    const sum = document.getElementById('log-summary');
    if(sum) sum.textContent = `共 ${total} 条`;
    const pagerEl = document.getElementById('tool-logs-pager');
    if(!logs.length) {
      el.innerHTML = '<div style="padding:12px;color:var(--mut);font-size:12px;">暂无调用日志（Agent 执行工具 / 触发技能时自动记录）</div>';
      if(pagerEl) pagerEl.innerHTML = '';
      return;
    }
    el.innerHTML = `<table class="t">
      <tr><th>时间</th><th>Agent</th><th>名称</th><th>类别</th><th>类型</th><th>结果摘要</th><th>耗时</th><th>状态</th></tr>` +
      logs.map(l=>`<tr>
        <td style="font-size:11px;">${(l.created_at||'').slice(5,19)}</td>
        <td style="font-size:12px;">${esc(l.agent_name||'-')}</td>
        <td><b>${esc(l.tool_name)}</b></td>
        <td><span class="st ${l.call_kind==='skill'?'w':'b'}">${l.call_kind||'tool'}</span></td>
        <td><span class="tag">${l.tool_type||'-'}</span></td>
        <td style="font-size:11px;color:var(--mut);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(l.result||'')}">${esc(l.result||'').slice(0,60)}</td>
        <td>${l.latency_ms}ms</td>
        <td><span class="st ${l.ok?'ok':'r'}">${l.ok?'成功':'失败'}</span></td>
      </tr>`).join('') + '</table>';
    if(pagerEl) renderPagerBar({
      el: pagerEl, total, page: _logs.page, size: _logs.size,
      onPage: p => { _logs.page = p; loadToolLogs(); },
      onSize: s => { _logs.size = s; _logs.page = 1; loadToolLogs(); }
    });
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${esc(e.message)}</div>`; }
}
async function toggleAgentStatus(id, cur) {
  const act = cur==='active' ? 'disable' : 'enable';
  await api(`/api/studio/agents/${id}/${act}`, {method:'POST'});
  toast(act==='enable' ? '已启用' : '已停用');
  loadAgents();
}

// ── 项目级持久记忆（Project Constitution）：规范/基线/决策/经验管理（st-projmem）──
let _pmEdit = {mode:null, id:null, projectId:''};
let _pmCache = [];
async function loadPMProjects(){
  const sel = document.getElementById('pm-project');
  if(!sel) return;
  try{
    const projects = await api('/api/projects');
    const def = await api('/api/projects/default');
    const defId = def ? def.id : (projects[0] ? projects[0].id : '');
    sel.innerHTML = (projects||[]).map(p=>`<option value="${esc(p.id)}">${esc(p.name)}${p.code?`（${esc(p.code)}）`:''}</option>`).join('');
    if(_pmEdit.projectId && [...sel.options].some(o=>o.value===_pmEdit.projectId)) sel.value = _pmEdit.projectId;
    else if(defId) sel.value = defId;
  }catch(e){ /* 静默 */ }
}
async function loadProjectMemories(){
  const el = document.getElementById('pm-cards');
  if(!el) return;
  let pid = document.getElementById('pm-project')?.value || '';
  if(!pid){ await loadPMProjects(); pid = document.getElementById('pm-project')?.value || ''; }  // 项目下拉未就绪先加载
  if(!pid){ el.innerHTML = '<div class="loading">请先选择项目</div>'; return; }
  const cat = document.getElementById('pm-category')?.value || '';
  try{
    const list = await api(`/api/projects/${encodeURIComponent(pid)}/memories?all=1${cat?`&category=${encodeURIComponent(cat)}`:''}`);
    _pmCache = list || [];
    const sum = document.getElementById('pm-summary');
    if(sum) sum.textContent = `共 ${_pmCache.length} 条 · ${_pmCache.filter(m=>m.enabled).length} 启用`;
    if(!_pmCache.length){ el.innerHTML = '<div class="loading">暂无记忆条目，点击「＋ 新增记忆」添加项目规范/基线/决策/经验</div>'; return; }
    const catMeta = {规范:'#E6F1FB', 基线:'#FFF3E0', 决策:'#EDE7F6', 经验:'#EAF3DE'};
    el.innerHTML = _pmCache.map(m=>`
      <div class="panel">
        <div class="ph"><span class="st" style="background:${catMeta[m.category]||'#eef1f6'};color:var(--blue-d);">${esc(m.category)}</span> ${esc(m.title)} ${m.enabled?'':'<span class="st r">停用</span>'}
          <span style="flex:1"></span>
          <button class="btn sm ghost" onclick="toggleProjectMemory(${m.id},${m.enabled?0:1})" title="启用/停用">${m.enabled?'停用':'启用'}</button>
        </div>
        <div class="pb" style="font-size:12px;color:var(--ink);white-space:pre-wrap;word-break:break-all;max-height:160px;overflow-y:auto;">${esc(m.content||'-')}</div>
        <div style="display:flex;gap:6px;padding:8px 12px;border-top:1px solid var(--line);">
          <button class="btn sm ghost" onclick="editProjectMemory(${m.id})">✏️ 编辑</button>
          <button class="btn sm red" onclick="deleteProjectMemory(${m.id})">🗑 删除</button>
        </div>
      </div>`).join('');
  }catch(e){ el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${esc(e.message)}</div>`; }
}
function openProjectMemoryForm(){
  _pmEdit = {mode:'create', id:null, projectId: document.getElementById('pm-project')?.value || ''};
  showModal('projmem');
  document.getElementById('pm-form-title').textContent = '新增项目记忆';
  document.getElementById('f-pm-category').value = '规范';
  document.getElementById('f-pm-title').value = '';
  document.getElementById('f-pm-content').value = '';
  document.getElementById('f-pm-enabled').value = '1';
}
function editProjectMemory(id){
  const m = (_pmCache||[]).find(x=>x.id===id);
  if(!m) return;
  _pmEdit = {mode:'edit', id, projectId: document.getElementById('pm-project')?.value || ''};
  showModal('projmem');
  document.getElementById('pm-form-title').textContent = '编辑项目记忆';
  document.getElementById('f-pm-category').value = m.category || '规范';
  document.getElementById('f-pm-title').value = m.title || '';
  document.getElementById('f-pm-content').value = m.content || '';
  document.getElementById('f-pm-enabled').value = m.enabled ? '1' : '0';
}
async function saveProjectMemory(){
  const title = document.getElementById('f-pm-title').value.trim();
  if(!title){ toast('标题必填'); return; }
  const body = {
    category: document.getElementById('f-pm-category').value,
    title,
    content: document.getElementById('f-pm-content').value,
    enabled: document.getElementById('f-pm-enabled').value === '1' ? 1 : 0,
  };
  const pid = _pmEdit.projectId;
  try{
    if(_pmEdit.mode === 'edit'){
      await api(`/api/projects/${encodeURIComponent(pid)}/memories/${_pmEdit.id}`, {method:'PUT', body:JSON.stringify(body)});
      toast('已更新');
    } else {
      await api(`/api/projects/${encodeURIComponent(pid)}/memories`, {method:'POST', body:JSON.stringify(body)});
      toast('已新增');
    }
    closeModal();
    loadProjectMemories();
  }catch(e){ toast('保存失败：' + e.message); }
}
async function toggleProjectMemory(id, enabled){
  await api(`/api/projects/${encodeURIComponent(_pmEdit.projectId || document.getElementById('pm-project')?.value || '')}/memories/${id}`, {method:'PUT', body:JSON.stringify({enabled})});
  toast(enabled ? '已启用' : '已停用');
  loadProjectMemories();
}
async function deleteProjectMemory(id){
  if(!(await confirmDialog('确认删除该记忆条目？删除后 AI 会话将不再注入。'))) return;
  await api(`/api/projects/${encodeURIComponent(_pmEdit.projectId || document.getElementById('pm-project')?.value || '')}/memories/${id}`, {method:'DELETE'});
  toast('已删除');
  loadProjectMemories();
}

// ── 确定性工具钩子（对齐 Claude Code PreToolUse）：工具调用前强制校验（st-hooks）──
let _hookEdit = {mode:null, id:null};
let _hookCache = [];
async function loadToolHooks(){
  const el = document.getElementById('hook-table');
  if(!el) return;
  try{
    const list = await api('/api/studio/tool-hooks');
    _hookCache = list || [];
    const sum = document.getElementById('hook-summary');
    if(sum) sum.textContent = `共 ${_hookCache.length} 条 · ${_hookCache.filter(h=>h.enabled).length} 启用`;
    const actMeta = {block:['⛔','r'], require_confirm:['🙋','a'], warn:['⚠️','w']};
    el.innerHTML = `<table class="t"><thead><tr><th>ID</th><th>名称</th><th>匹配工具</th><th>动作</th><th>参数条件</th><th>提示文案</th><th>状态</th><th>操作</th></tr></thead><tbody>` +
      (_hookCache.length ? _hookCache.map(h=>{
        const am = actMeta[h.action] || actMeta.warn;
        return `<tr>
          <td>#${h.id}</td>
          <td><b>${esc(h.name)}</b><br><span style="font-size:11px;color:var(--mut);">${esc(h.description||'')}</span></td>
          <td><code style="font-size:11px;">${esc(h.tool_pattern)}</code></td>
          <td><span class="st ${am[1]}">${am[0]} ${esc(h.action)}</span></td>
          <td style="font-size:11px;color:var(--mut);">${esc(h.condition_args||'-')}</td>
          <td style="font-size:11px;color:var(--mut);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(h.message||'')}">${esc(h.message||'-')}</td>
          <td><span class="st ${h.enabled?'ok':'r'}">${h.enabled?'启用':'停用'}</span></td>
          <td style="white-space:nowrap;">
            <button class="btn sm ghost" onclick="toggleToolHook(${h.id},${h.enabled?0:1})">${h.enabled?'停用':'启用'}</button>
            <button class="btn sm ghost" onclick="editToolHook(${h.id})">✏️</button>
            <button class="btn sm red" onclick="deleteToolHook(${h.id})">🗑</button>
          </td></tr>`;}).join('')
        : '<tr><td colspan="8" style="text-align:center;color:var(--mut);padding:20px;">暂无工具钩子，点击「＋ 新增钩子」配置工具调用前的强制校验</td></tr>') +
      `</tbody></table>`;
  }catch(e){ el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${esc(e.message)}</div>`; }
}
function openToolHookForm(){
  _hookEdit = {mode:'create', id:null};
  showModal('hook');
  document.getElementById('hook-form-title').textContent = '新增工具钩子';
  document.getElementById('f-hook-name').value = '';
  document.getElementById('f-hook-pattern').value = '';
  document.getElementById('f-hook-action').value = 'block';
  document.getElementById('f-hook-condition').value = '';
  document.getElementById('f-hook-message').value = '';
  document.getElementById('f-hook-desc').value = '';
  document.getElementById('f-hook-enabled').value = '1';
}
function editToolHook(id){
  const h = (_hookCache||[]).find(x=>x.id===id);
  if(!h) return;
  _hookEdit = {mode:'edit', id};
  showModal('hook');
  document.getElementById('hook-form-title').textContent = '编辑工具钩子';
  document.getElementById('f-hook-name').value = h.name || '';
  document.getElementById('f-hook-pattern').value = h.tool_pattern || '';
  document.getElementById('f-hook-action').value = h.action || 'block';
  document.getElementById('f-hook-condition').value = h.condition_args || '';
  document.getElementById('f-hook-message').value = h.message || '';
  document.getElementById('f-hook-desc').value = h.description || '';
  document.getElementById('f-hook-enabled').value = h.enabled ? '1' : '0';
}
async function saveToolHook(){
  const name = document.getElementById('f-hook-name').value.trim();
  const pattern = document.getElementById('f-hook-pattern').value.trim();
  if(!name || !pattern){ toast('名称与匹配工具必填'); return; }
  const condition = document.getElementById('f-hook-condition').value.trim();
  if(condition){
    try{ JSON.parse(condition); }catch(e){ toast('参数条件必须是合法 JSON（如 {"key":"path","contains":".sysml"}）'); return; }
  }
  const body = {
    name, tool_pattern: pattern,
    action: document.getElementById('f-hook-action').value,
    condition_args: condition,
    message: document.getElementById('f-hook-message').value,
    description: document.getElementById('f-hook-desc').value,
    enabled: document.getElementById('f-hook-enabled').value === '1' ? 1 : 0,
  };
  try{
    if(_hookEdit.mode === 'edit'){
      await api(`/api/studio/tool-hooks/${_hookEdit.id}`, {method:'PUT', body:JSON.stringify(body)});
      toast('已更新');
    } else {
      await api('/api/studio/tool-hooks', {method:'POST', body:JSON.stringify(body)});
      toast('已新增');
    }
    closeModal();
    loadToolHooks();
  }catch(e){ toast('保存失败：' + e.message); }
}
async function toggleToolHook(id, enabled){
  await api(`/api/studio/tool-hooks/${id}/${enabled?'enable':'disable'}`, {method:'POST'});
  toast(enabled ? '已启用' : '已停用');
  loadToolHooks();
}
async function deleteToolHook(id){
  if(!(await confirmDialog('确认删除该工具钩子？删除后工具调用将不再受该校验约束。'))) return;
  await api(`/api/studio/tool-hooks/${id}`, {method:'DELETE'});
  toast('已删除');
  loadToolHooks();
}

async function loadAgents() {
  const all = await api('/api/studio/agents');
  _agentsCache = all || _agentsCache;  // V3：同步 @智能体弹窗缓存
  const cnt = document.getElementById('agent-tab-count');
  if(cnt) cnt.textContent = all.length;
  // 整体筛选：角色（主/子）+ 类型（内置/自定义）+ 状态（启用=active）
  const fsv = document.getElementById('agent-status-filter')?.value || '';
  const ftv = document.getElementById('agent-type-filter')?.value || '';
  const frv = document.getElementById('agent-role-filter')?.value || '';
  const items = all.filter(a => {
    if(fsv==='enabled' && a.status!=='active') return false;
    if(fsv==='disabled' && a.status==='active') return false;
    const builtin = a.builtin===1;
    if(ftv==='builtin' && !builtin) return false;
    if(ftv==='custom' && builtin) return false;
    if(frv==='main' && a.agent_role!=='main') return false;
    if(frv==='sub' && a.agent_role==='main') return false;
    return true;
  });
  const mains = items.filter(a=>a.agent_role==='main');
  const subs = items.filter(a=>a.agent_role!=='main');
  const sum = document.getElementById('agent-summary');
  if(sum) sum.textContent = `共 ${items.length} 个 · ${mains.length} 主 / ${subs.length} 子 · ${items.filter(a=>a.status==='active').length} 启用`;
  const mcnt = document.getElementById('agent-main-count');
  if(mcnt) mcnt.textContent = mains.length;
  const scnt = document.getElementById('agent-sub-count');
  if(scnt) scnt.textContent = subs.length;
  const mbox = document.getElementById('agent-main-cards');
  const sbox = document.getElementById('agent-sub-cards');
  if(mbox) mbox.innerHTML = mains.length ? mains.map(agentCard).join('') : '<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--mut);font-size:12px;">暂无主 Agent — 点击「＋ 新建 Agent」创建（角色选「主 Agent」）</div>';
  if(sbox) sbox.innerHTML = subs.length ? subs.map(agentCard).join('') : '<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--mut);font-size:12px;">暂无子 Agent — 点击「＋ 新建 Agent」创建（角色选「子 Agent」）</div>';
}
// 主/子分区共用的 Agent 卡片（无「试运行」入口）
function agentCard(a){
  return `<div class="panel"><div class="ph">${a.icon||'🤖'} ${a.display_name||a.name} ${a.agent_role==='main'?'<span class="st" style="background:#fff3e0;color:#e65100;">👑 主</span>':'<span class="st b">🧩 子</span>'} ${a.builtin?'<span class="st b">内置</span>':''} <span class="st ${a.status==='active'?'ok':'r'}">${a.status}</span></div>
    <div class="pb">
      <div style="font-size:11px;color:var(--mut);margin-bottom:6px;"><span class="st b">${a.name}</span> ${a.provider_name?`<span class="st b">⚙ ${a.provider_name}</span>`:''}</div>
      <div style="font-size:12px;color:var(--mut);min-height:32px;">${a.description||'-'}</div>
      <div class="kv"><span>绑定工具</span><b>${a.tool_count||0} 个</b></div>
      ${a.agent_role==='main'?`<div class="kv"><span>团队规模</span><b>${a.team_count||0} 人</b></div>`:''}
      <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;">
        <button class="btn sm ghost" onclick="editAgent(${a.id})">✏️</button>
        <button class="btn sm ghost" title="复制为副本" onclick="copyAgent(${a.id})">📑 复制</button>
        <button class="btn sm ${a.status==='active'?'ghost amb':'ok'}" onclick="toggleAgentStatus(${a.id},'${a.status}')">${a.status==='active'?'停用':'启用'}</button>
        ${a.builtin?'<span style="font-size:10.5px;color:var(--mut);align-self:center;">内置不可删</span>':`<button class="btn sm red" onclick="deleteAgent(${a.id})">删除</button>`}
      </div>
    </div></div>`;
}
async function loadProviderOptions(selectedId) {
  try {
    const ps = await api('/api/llm/providers');
    const sel = document.getElementById('f-provider');
    if(!sel) return;
    sel.innerHTML = '<option value="">（全局默认）</option>' +
      ps.map(p=>`<option value="${p.id}" ${p.id===selectedId?'selected':''}>${p.name} · ${p.model_name}</option>`).join('');
  } catch(e) {}
}
function editAgent(id) {
  agentEditId = id;
  showModal('agent');
  loadProviderOptions();
  api('/api/studio/agents').then(items=>{
    const a = items.find(x=>x.id===id);
    if(!a) return;
    document.getElementById('f-name').value = a.name;
    document.getElementById('f-disp').value = a.display_name||'';
    document.getElementById('f-desc').value = a.description||'';
    document.getElementById('f-provider').value = a.model_provider_id || '';
    document.getElementById('f-sp').value = a.system_prompt||'';
    // 主/子 Agent 角色回填 + 团队成员（主 Agent 才加载）
    document.getElementById('f-role').value = a.agent_role==='main' ? 'main' : 'sub';
    onAgentRoleChange((a.team_members||[]).map(m=>m.id));
    // 能力绑定回填（skill / mcp / tool，逐个添加列表）
    loadAgentBindOptions((a.tools||[]).map(t=>`${t.tool_type}:${t.tool_name}`));
  });
}
// ── 主/子 Agent 团队：角色切换 → 团队成员候选加载（子 Agent 无成员区；下拉选择 + 逐个添加）──
let _agentTeamSet = new Set();
async function onAgentRoleChange(selectedMemberIds){
  const role = document.getElementById('f-role').value;
  const row = document.getElementById('f-team-row');
  const sel = document.getElementById('f-team');
  if(role !== 'main'){ if(row) row.style.display = 'none'; _agentTeamSet.clear(); renderTeamChips(); return; }
  if(!row || !sel) return;
  row.style.display = '';
  sel.innerHTML = '<option value="">加载候选…</option>';
  _agentTeamSet = new Set(selectedMemberIds||[]);
  renderTeamChips();
  try{
    const exclude = agentEditId || 0;
    const cands = await api('/api/studio/agents/sub-candidates?exclude=' + exclude);
    if(!(cands||[]).length){ sel.innerHTML = '<option value="">暂无可用子 Agent（请先创建子 Agent 角色）</option>'; return; }
    sel.innerHTML = '<option value="">选择子 Agent…</option>' + cands.map(c=>`<option value="${c.id}">${c.icon||'🤖'} ${esc(c.display_name||c.name)}（${esc(c.name)}）${(c.capabilities||[]).length?` · ${esc(c.capabilities.slice(0,3).join('/'))}`:''}</option>`).join('');
  }catch(e){ sel.innerHTML = '<option value="">加载失败</option>'; }
}
function renderTeamChips(){
  const box = document.getElementById('f-team-chips'); if(!box) return;
  box.innerHTML = [..._agentTeamSet].map(id=>{
    const opt = [...document.querySelectorAll('#f-team option')].find(o=>parseInt(o.value,10)===id);
    const label = opt ? opt.textContent.trim() : ('成员#'+id);
    return `<span class="hil l0" style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;">${esc(label)}<b style="cursor:pointer;color:var(--red);" title="移除" onclick="bindTeamRemove(${id})">×</b></span>`;
  }).join('') || '<span style="font-size:11px;color:var(--mut);">尚未添加成员</span>';
}
function bindTeamAdd(){
  const sel = document.getElementById('f-team');
  const v = sel ? parseInt(sel.value,10) : NaN;
  if(!v){ toast('请先选择要添加的子 Agent'); return; }
  _agentTeamSet.add(v);
  if(sel) sel.value = '';
  renderTeamChips();
}
function bindTeamRemove(id){
  _agentTeamSet.delete(id);
  renderTeamChips();
}
function collectAgentTeam(){
  const role = document.getElementById('f-role').value;
  if(role !== 'main') return [];
  return [..._agentTeamSet];
}
async function syncAgentTeam(aid){
  const role = document.getElementById('f-role').value;
  if(role !== 'main') return;   // 子 Agent 不设置团队
  try{
    const r = await api(`/api/studio/agents/${aid}/team`, {method:'PUT', body:JSON.stringify({sub_agent_ids: collectAgentTeam()})});
    if(r && r.error) toast('⚠ 团队同步失败：' + r.error);
  }catch(e){ if(e && e.message) toast('⚠ 团队同步失败：' + e.message); }
}
// ── Agent 能力绑定（skill / MCP / tool / plugin：下拉选择 + 逐个添加成标签，保存时同步）──
let _agentBindSets = {skill:new Set(), mcp:new Set(), tool:new Set(), plugin:new Set()};
const _BIND_EL = {skill:'skills', mcp:'mcps', tool:'tools', plugin:'plugins'};   // kind → 元素 id 复数后缀（f-bind-skills/mcps/tools/plugins）
async function loadAgentBindOptions(selected){
  const selectedSet = new Set(selected||[]);
  _agentBindSets = {skill:new Set(), mcp:new Set(), tool:new Set(), plugin:new Set()};
  [...selectedSet].forEach(k=>{
    const [t, n] = k.split(/:(.*)/s);
    if(n && t in _agentBindSets) _agentBindSets[t].add(n);
  });
  try {
    const [skills, mcps, tools, plugins] = await Promise.all([
      api('/api/studio/skills'), api('/api/studio/mcp-servers'), api('/api/studio/agent-tools'),
      api('/api/plugins'),
    ]);
    const fill = (elId, opts) => {
      const el = document.getElementById(elId);
      if(!el) return;
      el.innerHTML = (opts||[]).length
        ? '<option value="">选择…</option>' + opts.map(o=>`<option value="${esc(o.value)}">${esc(o.value)}${o.note?`（${esc(o.note)}）`:''}</option>`).join('')
        : '<option value="">暂无可用</option>';
    };
    fill('f-bind-skills', (skills||[]).filter(s=>s.enabled!==0).map(s=>({value:s.name})));
    fill('f-bind-mcps', (mcps||[]).filter(m=>m.status==='online' && m.enabled!==0).map(m=>({value:m.name})));
    fill('f-bind-tools', (tools||[]).filter(t=>t.source==='builtin').map(t=>({value:t.name})));
    fill('f-bind-plugins', (plugins&&plugins.items||[]).filter(p=>p.scope==='public'&&p.status==='published').map(p=>({value:p.plugin_id, note:p.name})));
    renderBindChips('skill'); renderBindChips('mcp'); renderBindChips('tool'); renderBindChips('plugin');
  } catch(e) {
    ['f-bind-skills','f-bind-mcps','f-bind-tools','f-bind-plugins'].forEach(id=>{ const el=document.getElementById(id); if(el) el.innerHTML='<option value="">加载失败</option>'; });
  }
}
function renderBindChips(kind){
  const box = document.getElementById('f-bind-' + _BIND_EL[kind] + '-chips'); if(!box) return;
  box.innerHTML = [..._agentBindSets[kind]].map(n=>`<span class="hil l0" style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;">${esc(n)}<b style="cursor:pointer;color:var(--red);" title="移除" onclick="bindRemove('${kind}','${esc(n.replace(/'/g,"\\'"))}')">×</b></span>`).join('')
    || '<span style="font-size:11px;color:var(--mut);">尚未绑定</span>';
}
function bindAdd(kind){
  const el = document.getElementById('f-bind-' + _BIND_EL[kind]);
  const v = el ? el.value : '';
  if(!v){ toast('请先选择要添加的' + ({skill:'技能', mcp:'MCP', tool:'工具', plugin:'插件'}[kind])); return; }
  _agentBindSets[kind].add(v);
  if(el) el.value = '';
  renderBindChips(kind);
}
function bindRemove(kind, name){
  _agentBindSets[kind].delete(name);
  renderBindChips(kind);
}
function collectAgentBindings(){
  return {
    skills: [..._agentBindSets.skill],
    mcps: [..._agentBindSets.mcp],
    tools: [..._agentBindSets.tool],
    plugins: [..._agentBindSets.plugin],
  };
}
async function syncAgentBindings(aid){
  try {
    const a = await api(`/api/studio/agents/${aid}`);
    const old = (a.tools||[]).map(t=>`${t.tool_type}:${t.tool_name}`);
    const oldSet = new Set(old);
    const b = collectAgentBindings();
    const want = [...b.skills.map(n=>'skill:'+n), ...b.mcps.map(n=>'mcp:'+n), ...b.tools.map(n=>'tool:'+n), ...b.plugins.map(n=>'plugin:'+n)];
    const wantSet = new Set(want);
    // 新增：不在旧绑定中 → POST
    for(const key of want) if(!oldSet.has(key)) {
      const [t, n] = key.split(/:(.*)/s);
      await api(`/api/studio/agents/${aid}/tools`, {method:'POST', body:JSON.stringify({tool_type:t, tool_name:n})});
    }
    // 移除：旧绑定中但不在新选择 → DELETE（需先查 id）
    if([...oldSet].some(k=>!wantSet.has(k))) {
      const stale = (a.tools||[]).filter(t=>!wantSet.has(`${t.tool_type}:${t.tool_name}`));
      for(const t of stale) await api(`/api/studio/agents/${aid}/tools/${t.id}`, {method:'DELETE'});
    }
  } catch(e) { /* 绑定同步失败不阻断主保存（toast 提示） */ if(e && e.message) toast('⚠ 能力绑定同步失败：'+e.message); }
}
function openAgentForm(){
  showModal('agent');
  document.getElementById('f-name').value = '';
  document.getElementById('f-role').value = 'sub';      // 新建默认子 Agent
  const row = document.getElementById('f-team-row');
  if(row) row.style.display = 'none';
  _agentTeamSet.clear(); renderTeamChips();
  loadProviderOptions();
  loadAgentBindOptions([]);
}
async function saveAgent() {
  const disp = document.getElementById('f-disp').value.trim();
  if(!disp) { toast('名称必填'); return; }
  const name = document.getElementById('f-name').value.trim() || disp;   // 新建时 name=展示名（路由标识）
  const provVal = document.getElementById('f-provider').value;
  const role = document.getElementById('f-role').value === 'main' ? 'main' : 'sub';
  const body = {
    name,
    display_name: disp,
    description: document.getElementById('f-desc').value,
    model_provider_id: provVal ? parseInt(provVal) : null,
    intent_keywords: [],
    system_prompt: document.getElementById('f-sp').value,
    kb_required: false,
    kb_scope: {},
    agent_role: role,
  };
  let aid = null;
  if(agentEditId) { await api(`/api/studio/agents/${agentEditId}`, {method:'PUT', body:JSON.stringify(body)}); aid = agentEditId; toast('已更新'); }
  else { const r = await api('/api/studio/agents', {method:'POST', body:JSON.stringify(body)}); aid = r.id; toast('已创建'); }
  await syncAgentBindings(aid);
  await syncAgentTeam(aid);   // 主 Agent 保存团队成员（子 Agent 跳过）
  closeModal(); agentEditId = null; loadAgents();
}
async function deleteAgent(id) {
  if(!(await confirmDialog('确认删除该 Agent？其工具绑定将级联清理。'))) return;
  await api(`/api/studio/agents/${id}`, {method:'DELETE'});
  toast('已删除'); loadAgents();
}
// ── Task 14：P2 意图路由规则配置（/api/studio/intent-rules CRUD）──
async function loadIntentRules() {
  const el = document.getElementById('intent-rules-table');
  const cnt = document.getElementById('rule-tab-count');
  const sum = document.getElementById('rule-summary');
  if(!el) return;
  try {
    const items = await api('/api/studio/intent-rules');
    if(cnt) cnt.textContent = items.length;
    if(sum) sum.textContent = `共 ${items.length} 条 · ${items.filter(r=>r.enabled).length} 启用`;
    if(!items.length){ el.innerHTML = '<div style="padding:12px;color:var(--mut);font-size:12px;">暂无意图规则，点击「＋ 新增规则」创建（trigger 匹配用户输入 → 按 weight 路由到 intent）</div>'; return; }
    el.innerHTML = `<table class="t"><tr><th>ID</th><th>触发词 trigger</th><th>定向意图 intent</th><th>权重</th><th>状态</th><th>操作</th></tr>` +
      items.map(r=>`<tr>
        <td>#${r.id}</td>
        <td><b>${esc(r.trigger)}</b></td>
        <td><span class="st b">${esc(r.intent)}</span></td>
        <td>${r.weight}</td>
        <td><span class="st ${r.enabled?'ok':''}" style="${r.enabled?'':'color:var(--mut);'}">${r.enabled?'启用':'停用'}</span></td>
        <td style="white-space:nowrap;">
          <button class="btn sm ghost" onclick="toggleIntentRule(${r.id},${r.enabled?1:0})">${r.enabled?'⏸ 停用':'▶ 启用'}</button>
          <button class="btn sm ghost" onclick="editIntentRule(${r.id})">✏️</button>
          <button class="btn sm red" onclick="deleteIntentRule(${r.id})">删除</button>
        </td></tr>`).join('') + '</table>';
  } catch(e){ el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${esc(e.message)}</div>`; }
}
async function editIntentRule(id) {
  let cur = null;
  if(id !== null && id !== undefined){
    try { cur = (await api('/api/studio/intent-rules')).find(r=>r.id===id) || null; } catch(e){}
    if(!cur){ toast('规则不存在或已删除'); loadIntentRules(); return; }
  }
  const r = await promptDialog({
    title: id ? '编辑意图规则 #' + id : '新增意图规则',
    message: '每行一个值：第 1 行 trigger（匹配用户输入）、第 2 行 intent（定向意图）、第 3 行 weight（正数权重，可空默认 1）。\n示例：\n功耗校核\nimpact\n1.2',
    value: cur ? `${cur.trigger}\n${cur.intent}\n${cur.weight}` : '',
    placeholder: 'trigger\nintent\nweight', multiline: true, rows: 4, okText: '保存'
  });
  if(r === null || r === undefined) return;
  const lines = String(r).split('\n');
  const trig = (lines[0]||'').trim(), inte = (lines[1]||'').trim(), w = lines[2] ? lines[2].trim() : '';
  if(!trig || !inte){ toast('trigger 与 intent 均必填'); return; }
  const weight = w ? (parseFloat(w) || 1) : 1;
  try {
    const body = {trigger:trig, intent:inte, weight};
    if(id){ await api('/api/studio/intent-rules/' + id, {method:'PUT', body:JSON.stringify(body)}); toast('已更新'); }
    else { await api('/api/studio/intent-rules', {method:'POST', body:JSON.stringify(body)}); toast('已创建'); }
    loadIntentRules();
  } catch(e){ toast('保存失败：' + (e.message||'')); }
}
async function toggleIntentRule(id, curEnabled) {
  try {
    await api('/api/studio/intent-rules/' + id, {method:'PUT', body:JSON.stringify({enabled: curEnabled ? 0 : 1})});
    toast(curEnabled ? '已停用' : '已启用'); loadIntentRules();
  } catch(e){ toast('操作失败：' + (e.message||'')); }
}
async function deleteIntentRule(id) {
  let trigger = '#' + id;
  try { const list = await api('/api/studio/intent-rules'); const hit = list.find(r=>r.id===id); if(hit) trigger = hit.trigger; } catch(e){}
  if(!(await confirmDialog(`确认删除意图规则「${trigger}」？删除后不再参与意图路由。`))) return;
  try { await api('/api/studio/intent-rules/' + id, {method:'DELETE'}); toast('已删除'); loadIntentRules(); }
  catch(e){ toast('删除失败：' + (e.message||'')); }
}
function toggleToolType() {
  const t = document.getElementById('f-tool-type').value;
  const mcpF = document.getElementById('tool-mcp-fields');
  const httpF = document.getElementById('tool-http-fields');
  const hint = document.getElementById('f-tool-name-hint');
  if (t === 'mcp') { mcpF.style.display=''; httpF.style.display='none'; hint.title='将作为运行时工具标识名'; }
  else { mcpF.style.display='none'; httpF.style.display=''; hint.title='将作为运行时工具名，如 get_weather'; }
}
function addToolHeader() {
  const list = document.getElementById('tool-headers-list');
  const row = document.createElement('div');
  row.className = 'form-row'; row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = '<input class="tool-hdr-key" placeholder="Key" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><input class="tool-hdr-val" placeholder="Value" style="flex:1.5;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><button class="btn sm ghost" onclick="this.parentElement.remove()" style="padding:2px 8px;font-size:11px;">✕</button>';
  list.appendChild(row);
}

function toggleToolType() {
  const t = document.getElementById('f-tool-type').value;
  const mcpF = document.getElementById('tool-mcp-fields');
  const httpF = document.getElementById('tool-http-fields');
  const hint = document.getElementById('f-tool-name-hint');
  if (t === 'mcp') { mcpF.style.display=''; httpF.style.display='none'; hint.title='将作为运行时工具标识名'; }
  else { mcpF.style.display='none'; httpF.style.display=''; hint.title='将作为运行时工具名，如 get_weather'; }
}
function addToolHeader() {
  const list = document.getElementById('tool-headers-list');
  const row = document.createElement('div');
  row.className = 'form-row'; row.style.cssText = 'display:flex;gap:6px;align-items:center;';
  row.innerHTML = '<input class="tool-hdr-key" placeholder="Key" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><input class="tool-hdr-val" placeholder="Value" style="flex:1.5;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><button class="btn sm ghost" onclick="this.parentElement.remove()" style="padding:2px 8px;font-size:11px;">✕</button>';
  list.appendChild(row);
}

let toolEditId = null;
function showToolForm(id) {
  toolEditId = id || null;
  showModal('tool');
  const t = (id && _toolsCache) ? _toolsCache.find(x=>x.id===id) : null;
  document.getElementById('tool-modal-title').textContent = t ? `编辑工具 #${id}` : '新增工具';
  // 判断类型：MCP 工具 source='mcp' 或有 mcp_server_id，HTTP 工具 source='http' 或 config 有 method/url
  const isMcp = t && (t.source === 'mcp' || t.mcp_server_id);
  const cfg = (t && t.config) ? (typeof t.config==='string' ? (()=>{try{return JSON.parse(t.config);}catch(e){return{};}})() : t.config) : {};
  document.getElementById('f-tool-type').value = isMcp ? 'mcp' : 'http';
  toggleToolType();
  document.getElementById('f-tool-name').value = t ? t.name : '';
  document.getElementById('f-tool-name').disabled = !!t;
  document.getElementById('f-tool-desc').value = t ? (t.description||'') : '';
  // MCP 字段回填
  if (isMcp && t) {
    document.getElementById('f-tool-transport').value = t.transport || 'streamable_http';
    document.getElementById('f-tool-url-mcp').value = t.endpoint || '';
  }
  // HTTP 字段回填
  document.getElementById('f-tool-method').value = cfg.method || 'GET';
  document.getElementById('f-tool-url-http').value = cfg.url || '';
  document.getElementById('f-tool-schema').value = t ? JSON.stringify(t.input_schema||{}, null, 1) : '';
  document.getElementById('f-tool-param-in').value = cfg.param_in ? JSON.stringify(cfg.param_in) : '';
  document.getElementById('f-tool-timeout').value = cfg.timeout || 15;
  // Headers 回填
  const hdrList = document.getElementById('tool-headers-list');
  hdrList.innerHTML = '';
  const hdrs = cfg.headers || {};
  Object.entries(hdrs).forEach(([k,v]) => {
    const row = document.createElement('div');
    row.className = 'form-row'; row.style.cssText = 'display:flex;gap:6px;align-items:center;';
    row.innerHTML = '<input class="tool-hdr-key" placeholder="Key" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;" value="'+k+'"><input class="tool-hdr-val" placeholder="Value" style="flex:1.5;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;" value="'+v+'"><button class="btn sm ghost" onclick="this.parentElement.remove()" style="padding:2px 8px;font-size:11px;">✕</button>';
    hdrList.appendChild(row);
  });
  if (!Object.keys(hdrs).length) addToolHeader();
  // 容错策略回填
  const rp = (t && t.retry_policy) ? (typeof t.retry_policy==='string' ? (()=>{try{return JSON.parse(t.retry_policy);}catch(e){return{};}})() : t.retry_policy) : {};
  document.getElementById('f-tool-retries').value = rp.max_retries||0;
  document.getElementById('f-tool-backoff').value = rp.backoff_base_ms||500;
  document.getElementById('f-tool-backoff-mult').value = rp.backoff_multiplier||2;
  const _fbEl = document.getElementById('f-tool-fallback'); if(_fbEl) _fbEl.value = t ? (t.fallback_to||'') : '';
}
async function saveTool() {
  const type = document.getElementById('f-tool-type').value;
  const name = document.getElementById('f-tool-name').value.trim();
  if(!name) { toast('名称必填'); return; }
  const desc = document.getElementById('f-tool-desc').value;
  // 收集 Headers
  const hdrKeys = document.querySelectorAll('#tool-headers-list .tool-hdr-key');
  const hdrVals = document.querySelectorAll('#tool-headers-list .tool-hdr-val');
  const headers = {};
  hdrKeys.forEach((el,i) => { const k = el.value.trim(); if(k) headers[k] = hdrVals[i].value; });
  if (type === 'mcp') {
    // MCP Server → 走 mcp-servers API
    const endpoint = document.getElementById('f-tool-url-mcp').value.trim();
    if(!endpoint) { toast('URL 必填'); return; }
    const body = {name, endpoint, transport: document.getElementById('f-tool-transport').value,
                  command: '', tools: [], args: [], env: {}};
    if(toolEditId) { await api(`/api/studio/mcp-servers/${toolEditId}`, {method:'PUT', body:JSON.stringify(body)}); toast('已更新'); }
    else { await api('/api/studio/mcp-servers', {method:'POST', body:JSON.stringify(body)}); toast('已添加'); }
    closeModal(); toolEditId = null; loadMCPServers(); loadMinePlugins(); loadMarket(); loadMarketManage();
  } else {
    // HTTP 接口 → 走 tools API
    const url = document.getElementById('f-tool-url-http').value.trim();
    if(!url) { toast('URL 必填'); return; }
    let schema = {};
    const raw = document.getElementById('f-tool-schema').value.trim();
    if(raw) { try { schema = JSON.parse(raw); } catch(e) { toast('input_schema 不是合法 JSON'); return; } }
    let paramIn = {};
    const piRaw = document.getElementById('f-tool-param-in').value.trim();
    if(piRaw) { try { paramIn = JSON.parse(piRaw); } catch(e) { toast('param_in 不是合法 JSON'); return; } }
    const timeout = Math.max(1, parseInt(document.getElementById('f-tool-timeout').value)||15);
    const retry_policy = {
      max_retries: Math.max(0, parseInt(document.getElementById('f-tool-retries').value)||0),
      backoff_base_ms: Math.max(100, parseInt(document.getElementById('f-tool-backoff').value)||500),
      backoff_multiplier: Math.max(1, parseInt(document.getElementById('f-tool-backoff-mult').value)||2),
    };
    const body = {
      name, description: desc, source: 'http',
      input_schema: schema,
      method: document.getElementById('f-tool-method').value,
      url, param_in: paramIn, timeout, headers,
      retry_policy,
      fallback_to: (document.getElementById('f-tool-fallback')||{value:''}).value.trim(),
    };
    if(toolEditId) { await api(`/api/studio/tools/${toolEditId}`, {method:'PUT', body:JSON.stringify(body)}); toast('已更新'); }
    else { await api('/api/studio/tools', {method:'POST', body:JSON.stringify(body)}); toast('已注册'); if(_mmCreate){ _mmCreate = null; await mmAutoPublish('tool', name); } }
    closeModal(); toolEditId = null; loadTools(); loadMinePlugins(); loadMarketManage();
  }
}
async function toggleToolStatus(id, enabled) {
  const t = _toolsCache.find(x=>x.id===id);
  if(enabled) {
    await api(`/api/studio/tools/${id}/enable`, {method:'POST'});
    toast(`已启用「${t?t.name:''}」`);
  } else {
    const r = await api(`/api/studio/tools/${id}/disable`, {method:'POST'});
    toast(`已停用「${t?t.name:''}」${r.note||''}`);
  }
  loadTools();
  loadMinePlugins();
}
async function deleteTool(id) {
  const t = _toolsCache.find(x=>x.id===id);
  if(!(await confirmDialog(`确认删除工具「${t?t.name:''}」？此操作不可恢复。`))) return;
  await api(`/api/studio/tools/${id}`, {method:'DELETE'});
  toast('已删除'); loadTools(); loadMinePlugins(); loadMarketManage();
}
let toolNeverFilter = false;
async function loadTools() {
  const items = await api('/api/studio/tools');
  _toolsCache = items;
  if(!document.getElementById('tool-cards')) return;  // 旧工具注册表已随「工具移出插件市场」隐藏
  renderToolCards();
}
// 2026-08-11 优化：工具注册表统一为卡片形式（与 MCP 服务器卡片一致，Dify/Coze 式）
function renderToolCards() {
  // 组合筛选：仅看未使用 + 状态（启用/停用）+ 类型（内置/自定义，source='builtin' 即内置）
  const fs = document.getElementById('tool-status-filter');
  const fsv = fs ? fs.value : '';
  const ft = document.getElementById('tool-type-filter');
  const ftv = ft ? ft.value : '';
  const items = (_toolsCache||[]).filter(t => {
    if(toolNeverFilter && !t.never_used) return false;
    if(fsv==='enabled' && t.status!=='active') return false;
    if(fsv==='disabled' && t.status==='active') return false;
    const builtin = t.source==='builtin' || t.builtin===1;
    if(ftv==='builtin' && !builtin) return false;
    if(ftv==='custom' && builtin) return false;
    return true;
  });
  const seZh = {read:'read 只读', write:'write 写入', destructive:'destructive 高风险'};
  const rkZh = {low:'低', medium:'中', high:'高'};
  const neverCount = (_toolsCache||[]).filter(t=>t.never_used).length;
  const tabCount = document.getElementById('tool-tab-count');
  if(tabCount) tabCount.textContent = (_toolsCache||[]).length;
  const sum = document.getElementById('tool-summary');
  if(sum) sum.innerHTML = `${(_toolsCache||[]).length} 个工具 · <span style="color:${neverCount?'var(--red)':'var(--mut)'};">从未使用 ${neverCount} 个</span>`;
  const btn = document.getElementById('tool-never-btn');
  if(btn) btn.style.background = toolNeverFilter ? 'var(--blue-l)' : '';
  const box = document.getElementById('tool-cards');
  if(!box) return;  // 旧工具注册表已随「工具移出插件市场」隐藏
  if(!items.length){
    const filteredEmpty = toolNeverFilter || fsv || ftv;
    box.innerHTML = `<div style="grid-column:1/-1;padding:36px;text-align:center;color:var(--mut);font-size:12px;">${toolNeverFilter?'🎉 未使用工具已清零':(filteredEmpty?'无匹配的工具（调整筛选条件）':'暂无工具，点击右上角「＋ 新增工具」注册')}</div>`;
    return;
  }
  box.innerHTML = items.map(t=>{
    const side = `<span class="st ${t.side_effect==='destructive'?'r':t.side_effect==='write'?'w':'b'}">${seZh[t.side_effect]||t.side_effect||'read'}</span>`;
    const risk = `<span class="st ${t.risk_level==='high'?'r':t.risk_level==='medium'?'w':'b'}">${rkZh[t.risk_level]||t.risk_level||'-'}</span>`;
    const st = `<span class="st ${t.status==='active'?'ok':'w'}">${t.status==='active'?'启用':'停用'}</span>${t.health&&t.health!=='ok'?` <span class="st r">${t.health}</span>`:''}`;
    const usage = t.never_used
      ? `<span class="st r" title="从未被任何会话调用，候选清理">未使用</span>`
      : `<span style="font-size:11px;color:var(--mut);">${t.usage_count} 次${t.last_used_at?` · 最近 ${(t.last_used_at||'').slice(5,16)}`:''}</span>`;
    return `<div class="panel">
      <div class="ph">${esc(t.name)} <span class="st b">${esc(t.source)}</span><span style="flex:1"></span>${st}</div>
      <div class="pb">
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">${side}${risk}${t.version?`<span class="st b">${esc(t.version)}</span>`:''}${t.owner?`<span class="tag">👤 ${esc(t.owner)}</span>`:''}</div>
        <div style="font-size:11.5px;color:var(--mut);line-height:1.5;min-height:34px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;" title="${esc(t.description||'')}">${esc(t.description||'-')}</div>
        <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;gap:6px;">
          <span style="font-size:11.5px;color:var(--mut);">📈 ${usage}</span>
          <span style="display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn sm ghost" onclick="showToolForm(${t.id})">✏️</button>
            <button class="btn sm ${t.status==='active'?'ghost amb':'ok'}" onclick="toggleToolStatus(${t.id},${t.status==='active'?0:1})">${t.status==='active'?'停用':'启用'}</button>
            ${(t.source==='builtin'||t.builtin===1)?'<span style="font-size:10.5px;color:var(--mut);">内置</span>':(t.scope==='public'?'<span class="st" style="background:#e8f5e9;color:#2e7d32;">市场在售</span>':`<button class="btn sm ghost" onclick="marketPublishFrom('tool','${esc(t.name.replace(/'/g,"\\'"))}')">⬆ 发布</button><button class="btn sm red" onclick="deleteTool(${t.id})">删除</button>`)}
          </span>
        </div>
      </div>
    </div>`;
  }).join('');
}
let llmTypeFilter = 'all';
function setLLMType(t, btn) {
  llmTypeFilter = t;
  document.querySelectorAll('#st-model [id^="llm-type-"]').forEach(b=>b.style.background='');
  if(btn) btn.style.background = 'var(--blue-l)';
  loadLLMProviders();
}
async function loadLLMProviders() {
  const url = llmTypeFilter==='all' ? '/api/llm/providers' : `/api/llm/providers?model_type=${llmTypeFilter}`;
  const items = await api(url);
  document.getElementById('llm-table').innerHTML = `<table class="t">
    <tr><th>名称</th><th>类型</th><th>模型</th><th>设为默认</th><th>状态</th><th>操作</th></tr>` +
    items.map(p=>{
      const disabled = p.status==='disabled';
      return `<tr>
      <td><b>${esc(p.name)}</b></td>
      <td>${p.model_type==='embedding'?'<span class="st b">向量</span>':'<span class="st g">对话</span>'}</td>
      <td style="font-size:11px;">${esc(p.model_name)}</td>
      <td><label class="sw" title="设为该类型（对话/向量）的默认模型"><input type="checkbox" ${p.is_default===1?'checked':''} onchange="toggleLLMDefault(${p.id},this.checked)"><span class="sl"></span></label></td>
      <td>${disabled?'<span class="st g">停用</span>':'<span class="st ok">启用</span>'}</td>
      <td style="white-space:nowrap;"><button class="btn sm ghost" onclick="editLLM(${p.id})">编辑</button>
      <button class="btn sm ghost" onclick="testLLM(${p.id})">测试</button>
      ${disabled
        ? '<button class="btn sm grn" onclick="toggleLLMStatus('+p.id+',\'active\')">启用</button>'
        : '<button class="btn sm ghost" onclick="toggleLLMStatus('+p.id+',\'disabled\')">停用</button>'}
      <button class="btn sm red" onclick="deleteLLM(${p.id})">删除</button></td>
    </tr>`;}).join('') + '</table>';
}
function onLLMTypeChange(){
  const v = document.getElementById('f-model-type')?.value;
  const h = document.getElementById('f-embed-hint');
  if(h) h.style.display = (v==='embedding') ? 'block' : 'none';
}
function addLLM() {
  showModal('llm');
  document.getElementById('f-edit-llm').value = '';
  document.getElementById('llm-modal-title').innerText = '添加模型';
  ['f-name','f-url','f-key','f-model','f-tags'].forEach(i=>{const el=document.getElementById(i); if(el) el.value='';});
  document.getElementById('f-model-type').value = 'chat';
  const dcb = document.getElementById('f-is-default');
  if(dcb) dcb.checked = false;
  document.getElementById('f-ctx').value = 8192;
  document.getElementById('f-ptype').value = 'openai';
  document.getElementById('f-maxt').value = 8192;
  document.getElementById('f-temp').value = 0.3;
  document.getElementById('f-priority').value = 0;
  document.getElementById('f-budget').value = 0;
  document.getElementById('f-top-p').value = '';
  document.getElementById('f-top-k').value = '';
  const tr = document.querySelector('input[name="f-thinking"][value="模型默认"]'); if(tr) tr.checked = true;
  const k = document.getElementById('f-key'); if(k) k.disabled = false;
  const kh = document.getElementById('f-key-hint'); if(kh) kh.style.display = 'none';
  onLLMTypeChange();
}
async function editLLM(id) {
  const items = await api('/api/llm/providers');
  const p = items.find(x=>x.id===id);
  if(!p) return;
  showModal('llm');
  document.getElementById('f-edit-llm').value = id;
  document.getElementById('llm-modal-title').innerText = '编辑模型';
  document.getElementById('f-name').value = p.name;
  document.getElementById('f-url').value = p.base_url;
  document.getElementById('f-model').value = p.model_name;
  document.getElementById('f-model-type').value = p.model_type || 'chat';
  const dcb = document.getElementById('f-is-default');
  if(dcb) dcb.checked = (p.is_default === 1);
  document.getElementById('f-ctx').value = p.context_window || 8192;
  document.getElementById('f-ptype').value = p.provider_type || 'openai';
  document.getElementById('f-maxt').value = p.max_tokens || 8192;
  document.getElementById('f-temp').value = p.temperature ?? 0.3;
  document.getElementById('f-tags').value = (p.tags||[]).join(', ');
  document.getElementById('f-priority').value = p.priority || 0;
  document.getElementById('f-budget').value = p.budget_tokens || 0;
  // 高级设置回填（上下文输入/输出、温度、TopP、TopK、思考模式）
  const mp = p.model_params || {};
  document.getElementById('f-top-p').value = (mp.top_p ?? '');
  document.getElementById('f-top-k').value = (mp.top_k ?? '');
  const tr = document.querySelector('input[name="f-thinking"][value="' + (mp.thinking_mode||'模型默认') + '"]');
  if(tr) tr.checked = true;
  // 密钥：已配置 → 默认展示点点且不可编辑修改（保存时也不回传）；未配置 → 可填写
  const k = document.getElementById('f-key');
  const kh = document.getElementById('f-key-hint');
  if(p.api_key_masked){ k.value = '••••••••'; k.disabled = true; if(kh) kh.style.display='inline'; }
  else { k.value = ''; k.disabled = false; if(kh) kh.style.display='none'; }
  onLLMTypeChange();
}
async function saveLLM() {
  const id = document.getElementById('f-edit-llm').value;
  const name = document.getElementById('f-name').value;
  const base_url = document.getElementById('f-url').value;
  const keyInput = document.getElementById('f-key');
  const api_key = keyInput.disabled ? '' : keyInput.value;
  const model_name = document.getElementById('f-model').value;
  const model_type = document.getElementById('f-model-type')?.value || 'chat';
  const is_default = document.getElementById('f-is-default')?.checked ? 1 : 0;
  const context_window = parseInt(document.getElementById('f-ctx').value)||8192;
  const maxtRaw = parseInt(document.getElementById('f-maxt').value);
  const tempRaw = parseFloat(document.getElementById('f-temp').value);
  // 温度 0 是合法值（贪心解码），不能用 || 兜底；仅当留空/NaN/越界时回落默认
  const temperature = (Number.isFinite(tempRaw) && tempRaw >= 0 && tempRaw <= 2) ? tempRaw : 0.3;
  const max_tokens = (Number.isFinite(maxtRaw) && maxtRaw > 0) ? maxtRaw : Math.min(context_window, 8192);
  if(max_tokens > context_window) { toast('上下文输出不能超过输入窗口'); return; }
  // 高级设置：TopP（0~1）、TopK（整数 1~100）、思考模式（模型默认/开启/关闭）
  const topPRaw = parseFloat(document.getElementById('f-top-p').value);
  const topP = (Number.isFinite(topPRaw) && topPRaw >= 0 && topPRaw <= 1) ? topPRaw : null;
  const topKRaw = document.getElementById('f-top-k').value.trim();
  let topK = null;
  if(topKRaw !== ''){
    const tk = parseInt(topKRaw);
    if(!Number.isInteger(tk) || tk < 1 || tk > 100){ toast('TopK 需为 1~100 的整数'); return; }
    topK = tk;
  }
  const thinking_mode = document.querySelector('input[name="f-thinking"]:checked')?.value || '模型默认';
  const body = {
    name, base_url, api_key, model_name, model_type, context_window, is_default,
    provider_type: document.getElementById('f-ptype').value || 'openai',
    max_tokens, temperature,
    tags: document.getElementById('f-tags').value.split(',').map(s=>s.trim()).filter(Boolean),
    priority: parseInt(document.getElementById('f-priority').value)||0,
    budget_tokens: parseInt(document.getElementById('f-budget').value)||0,
  };
  if(!name||!base_url||!model_name) { toast('必填'); return; }
  let savedId = id;
  if(id) {
    await api(`/api/llm/providers/${id}`, {method:'PUT', body:JSON.stringify(body)});
  } else {
    const r = await api('/api/llm/providers', {method:'POST', body:JSON.stringify(body)});
    savedId = r.id || '';
  }
  // 高级参数（top_p/top_k/thinking_mode）落库 model_params
  if(savedId) await api(`/api/llm/providers/${savedId}/params`, {method:'PATCH', body:JSON.stringify({model_params:{top_p:topP, top_k:topK, thinking_mode}})});
  closeModal(); toast(id ? '已保存' : '已添加'); loadLLMProviders(); loadHome();
}
async function testLLM(id) {
  const r = await api(`/api/llm/providers/${id}/test`, {method:'POST'});
  toast(r.ok ? '连接成功' : `连接失败：${r.error||'未知错误'}`);
}
// 编辑表单内「测试」：用表单当前配置（名称/URL/Key/模型名）实时连通性测试，未保存也可测；
// 编辑模式下表单 Key 为点点占位（已配置不可改）→ 回退用已保存的 provider 配置测试
async function testLLMFromForm() {
  const id = document.getElementById('f-edit-llm').value;
  const name = document.getElementById('f-name').value.trim();
  const base_url = document.getElementById('f-url').value.trim();
  const keyInput = document.getElementById('f-key');
  const api_key = keyInput.disabled ? '' : keyInput.value.trim();
  const model_name = document.getElementById('f-model').value.trim();
  if(!name || !base_url || !model_name) { toast('请先填写必填项：名称 / Base URL / 模型名'); return; }
  toast('正在测试连通…');
  try {
    let r;
    if(id && !api_key) {
      // 编辑模式：Key 未改动（留空/点点占位）→ 用已保存配置测试
      r = await api(`/api/llm/providers/${id}/test`, {method:'POST'});
    } else {
      r = await api('/api/llm/providers/test', {method:'POST', body:JSON.stringify({name, base_url, api_key, model_name, model_type: document.getElementById('f-model-type')?.value||'chat'})});
    }
    toast(r.ok ? `✅ 连接成功${r.latency_ms?`（${r.latency_ms}ms）`:''}${r.model?`：${r.model}`:''}` : `❌ 连接失败：${r.error||'未知错误'}`);
  } catch(e) { toast('❌ 测试失败：'+e.message); }
}
async function deleteLLM(id) {
  if(!(await confirmDialog(`确认删除该模型？\n删除后不可恢复。`))) return;
  await api(`/api/llm/providers/${id}`, {method:'DELETE'});
  toast('已删除'); loadLLMProviders(); loadHome();
}
// 设为默认开关：开 → PUT is_default=1（后端按 model_type 隔离清旧默认）；关 → PUT is_default=0
async function toggleLLMDefault(id, on) {
  const items = await api('/api/llm/providers');
  const p = items.find(x=>x.id===id);
  if(!p) return;
  await api(`/api/llm/providers/${id}`, {method:'PUT', body:JSON.stringify({
    name:p.name, base_url:p.base_url, api_key:'', model_name:p.model_name,
    model_type:p.model_type||'chat', context_window:p.context_window||8192,
    max_tokens:p.max_tokens||8192, temperature:p.temperature??0.3, is_default: on?1:0,
    tags:p.tags||[], priority:p.priority||0, budget_tokens:p.budget_tokens||0,
    provider_type:p.provider_type||'openai',
  })});
  toast(on ? `已将「${p.name}」设为${p.model_type==='embedding'?'向量':'对话'}默认模型` : `已取消「${p.name}」的默认标记`);
  loadLLMProviders(); loadHome();
}
// 停用/启用：PATCH status（active|disabled）；停用后不参与调用与智能路由
async function toggleLLMStatus(id, status) {
  const items = await api('/api/llm/providers');
  const p = items.find(x=>x.id===id);
  if(!p) return;
  await api(`/api/llm/providers/${id}/status`, {method:'PATCH', body:JSON.stringify({status})});
  toast(status==='active' ? `已启用「${p.name}」` : `已停用「${p.name}」（不再参与调用/路由）`);
  loadLLMProviders(); loadHome();
}
async function loadRules() {
  const items = await api('/api/studio/rules');
  document.getElementById('rules-table').innerHTML = `<table class="t">
    <tr><th>规则键</th><th>当前值</th><th>说明</th><th>操作</th></tr>` +
    items.map(r=>`<tr>
      <td>${r.rule_key}</td><td><b>${r.rule_value}</b></td><td>${r.description}</td>
      <td><button class="btn sm ghost" onclick="editRule(${r.id},'${r.rule_key}','${r.rule_value}')">编辑</button></td>
    </tr>`).join('') + '</table>';
}
async function editRule(id, key, value) {
  const newVal = await promptDialog({title:'编辑规则', message:`编辑规则 ${key}：`, value});
  if(newVal===null) return;
  await api(`/api/studio/rules/${id}`, {method:'PUT', body:JSON.stringify({value:newVal})});
  toast('规则已更新'); loadRules();
}

// ── 用户与权限 ──
// 右上角账号：当前登录用户（localStorage 模拟登录态）/ 切换账号 / 退出登录
function currentUserId(){ return parseInt(localStorage.getItem('mbse_user_id')||'1',10); }
async function loadCurrentUser(){
  try{
    const users = await api('/api/users');
    const uid = currentUserId();
    const u = users.find(x=>x.id===uid) || users[0] || null;
    if(u){ localStorage.setItem('mbse_user_id', String(u.id)); setCurrentUserUI(u); }
    else showLoggedOutUI();
  }catch(e){ showLoggedOutUI(); }
}
function setCurrentUserUI(u){
  const av = document.getElementById('user-avatar');
  if(av) av.textContent = (u.display_name||'?').slice(0,1);
  const nm = document.getElementById('user-name');
  if(nm) nm.textContent = u.display_name || '未登录';
  const head = document.getElementById('user-menu-head');
  if(head) head.innerHTML = `<b>${esc(u.display_name)}</b><br><small>${esc(u.role_name||'未分配角色')}${u.department?' · '+esc(u.department):''}</small>`;
  const ua = document.getElementById('ufoot-avatar');
  if(ua) ua.textContent = (u.display_name||'?').slice(0,1);
  const un = document.getElementById('ufoot-name');
  if(un) un.textContent = u.display_name || '';
  const ur = document.getElementById('ufoot-role');
  if(ur) ur.textContent = u.role_name || '未分配角色';
  const ud = document.getElementById('ufoot-dept');
  if(ud) ud.textContent = u.department || '';
}
function showLoggedOutUI(){
  const av = document.getElementById('user-avatar');
  if(av) av.textContent = '登';
  const nm = document.getElementById('user-name');
  if(nm) nm.textContent = '未登录';
  const head = document.getElementById('user-menu-head');
  if(head) head.innerHTML = '<b>未登录</b><br><small>点击下方选择账号登录</small>';
  const ua = document.getElementById('ufoot-avatar');
  if(ua) ua.textContent = '登';
  const un = document.getElementById('ufoot-name');
  if(un) un.textContent = '未登录';
  const ur = document.getElementById('ufoot-role');
  if(ur) ur.textContent = '-';
  const ud = document.getElementById('ufoot-dept');
  if(ud) ud.textContent = '-';
}
function toggleUserMenu(ev){
  // 2026-09-04 v3：账号弹窗 fixed 定位，按 user-chip 位置计算，避免被 gnav overflow 裁剪左边框
  if(ev) ev.stopPropagation();
  const m = document.getElementById('user-menu');
  const chip = document.getElementById('user-chip');
  if(!m || !chip) return;
  const willOpen = !m.classList.contains('open');
  closeUserMenu();
  if(willOpen){
    const r = chip.getBoundingClientRect();
    m.style.left = 'auto'; m.style.right = 'auto'; m.style.bottom = 'auto'; m.style.top = 'auto';
    m.classList.add('open');
    m.style.left = Math.max(6, r.left) + 'px';
    const cr = chip.getBoundingClientRect();   // 重新取值（菜单打开不影响 chip）
    m.style.top = (cr.top - m.offsetHeight - 8) + 'px';
    setTimeout(()=>document.addEventListener('click', closeUserMenuOnce, {once:true}), 0);
  }
}
function closeUserMenu(){
  const m=document.getElementById('user-menu'); if(m) m.classList.remove('open');
}
function closeUserMenuOnce(){ closeUserMenu(); }
async function switchAccount(){
  closeUserMenuOnce();
  try{
    const users = await api('/api/users');
    const cur = currentUserId();
    const options = users.map(u=>`<div onclick="setCurrentUser(${u.id})" style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--line);font-size:12.5px;${u.id===cur?'background:var(--blue-l);font-weight:600;':''}display:flex;align-items:center;gap:8px;">
      <span class="avatar" style="width:24px;height:24px;font-size:10px;flex:none;">${esc((u.display_name||'?').slice(0,1))}</span>
      <div style="min-width:0;"><b>${esc(u.display_name)}</b> <small style="color:var(--mut);">${esc(u.username)}</small><br>
      <small style="color:var(--mut);">${esc(u.role_name||'-')}${u.department?' · '+esc(u.department):''}</small></div>
      ${u.id===cur?'<b style="margin-left:auto;color:var(--blue-d);">✓ 当前</b>':''}
    </div>`).join('') || '<div style="padding:10px;color:var(--mut);font-size:12px;">暂无用户</div>';
    const title = document.getElementById('panel-title');
    if(title) title.textContent = '👤 选择账号';
    document.getElementById('panel-body').innerHTML = options;
    document.getElementById('panel-detail').classList.add('open');
    document.getElementById('overlay').style.display = 'block';
  }catch(e){ toast('获取用户列表失败'); }
}
