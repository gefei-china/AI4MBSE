/* 工作台：市场 / 插件 / 技能 / MCP
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 16689-17705  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function loadStudioTab(id) {
  // 2026-09-16：能力中心接管四个类型页（市场 / 我安装的双区，读 /api/plugins）
  if(typeof capMountForTab === 'function' && capMountForTab(id)) return;
  if(id==='st-prompt') loadPrompts();
  if(id==='st-skill') loadSkillCombined();
    if(id==='st-mcp') switchToolTab(null, _toolTab);   // 工具管理：恢复上次子菜单（MCP/HTTP/内置）
  if(id==='st-agent') { loadAgents(); loadToolLogs(); }
  if(id==='st-market') { applyMarketPerm(); loadMarketManage(); }
  if(id==='st-flow') loadFlows();
  if(id==='st-model') loadLLMProviders();
}

// ── 设置（项目记忆 + 确定性工具钩子）──
function loadSettingsTab(id) {
  if(id==='st-projmem') { loadPMProjects(); loadProjectMemories(); }
  if(id==='st-hooks') loadToolHooks();
  if(id==='st-fextract') renderFileExtractSettings();
  if(id==='st-ctx') loadCtxConfig();
  // 2026-09-26：意图样本池（采样→标注→评测闭环），照 st-ctx 的懒加载约定
  if(id==='st-intent-samples') loadIntentSamples();
}

// ── 插件管理（Skill/MCP/工具 公共市场 + 我的插件 + 市场管理；安装=复制私有副本，版本固定）──
const MARKET_KIND_META = {skill:{label:'🧩 Skill', icon:'🧩'}, mcp:{label:'🔌 MCP', icon:'🔌'}, tool:{label:'🔧 工具', icon:'🔧'}};
let _pluginManagePerm = null;   // ai_studio:market_admin 是否有市场管理权限（null=未判定）
async function canManageMarket(){
  if(_pluginManagePerm !== null) return _pluginManagePerm;
  try{
    const me = await api('/api/users/me').catch(()=>null);
    if(!me || !me.permissions) { _pluginManagePerm = true; return true; }  // 匿名向后兼容（后端放行）
    const p = me.permissions||{};
    // 2026-09-17 P1：与后端 store.is_market_admin 同口径 ——
    // 市场治理（待审/下架/编辑/置顶/审核策略）需 ai_studio:market_admin；
    // 创作者（ai_studio:publish）不再自动享有市场管理入口。
    _pluginManagePerm = !!((p.admin&&p.admin.length) || (p.ai_studio||[]).includes('market_admin'));
  }catch(e){ _pluginManagePerm = true; }
  return _pluginManagePerm;
}
let _pluginPublishPerm = null;  // ai_studio:publish 是否可发布自建能力到市场（null=未判定）
async function canPublishToMarket(){
  if(_pluginPublishPerm !== null) return _pluginPublishPerm;
  try{
    const me = await api('/api/users/me').catch(()=>null);
    if(!me || !me.permissions) { _pluginPublishPerm = true; return true; }
    const p = me.permissions||{};
    _pluginPublishPerm = !!((p.admin&&p.admin.length) || (p.ai_studio||[]).includes('publish'));
  }catch(e){ _pluginPublishPerm = true; }
  return _pluginPublishPerm;
}
async function applyMarketPerm(){
  // 市场管理入口仅对有权限用户可见（无 ai_studio:publish 权限时隐藏侧边栏入口）
  const ok = await canManageMarket();
  const link = document.getElementById('nav-market');
  if(link) link.style.display = ok ? '' : 'none';
}
// 公共市场 → 按管理入口拆分：技能市场 / MCP 市场（工具已移出插件市场）
async function loadMarket(){
  await Promise.all([loadSkillMarket(), loadMCPMarket()]);
}
// 市场分类下拉（随入口种类变化，首次加载一次）
let _skCatsLoaded = false, _mcpCatsLoaded = false;
// 技能整合视图：Skills 列表（插件市场 / 自定义 / 内置 单一筛选；操作入口在右上角）

async function loadSkillDrafts(box, sum){
  const r = await api('/api/studio/skills?status=draft').catch(()=>[]);
  const items = r || [];
  if(sum) sum.textContent = `草稿箱共 ${items.length} 个（AI 自动沉淀/自改进草稿，审核后发布）`;
  if(!items.length){ box.innerHTML = '<div style="grid-column:1/-1;padding:36px;text-align:center;color:var(--mut);font-size:12px;">暂无草稿。任务产出满足「方法/流程/模板/最佳实践」信号时会自动沉淀为技能草稿</div>'; return; }
  box.innerHTML = items.map(s=>{
    const srcTag = s.source==='ai_deposit' ? '<span class="st amb">AI 沉淀</span>'
      : s.source==='ai_improve' ? '<span class="st amb">AI 改进</span>' : '<span class="st b">草稿</span>';
    const stTag = s.status==='draft_revision' ? '<span class="st amb">修订版</span>' : '';
    const preview = String(s.content||'').slice(0,160);
    return `<div class="panel"><div class="ph">${esc(s.name||'')} ${srcTag} ${stTag}</div>
    <div class="pb">
      <div style="font-size:11px;color:var(--mut);line-height:1.5;min-height:34px;max-height:60px;overflow:hidden;" title="${esc(s.description||'')}">${esc(preview)}</div>
      ${cardMeta({category:s.category, version:s.version, description:s.description})}
      <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;">
        <button class="btn sm" onclick="publishSkill(${s.id})">✓ 发布</button>
        <button class="btn sm ghost" onclick="openSkillForm(${s.id})">✏️ 查看</button>
        <button class="btn sm red" onclick="deleteSkill(${s.id})">🗑 删除</button>
      </div>
    </div></div>`;
  }).join('');
}

async function loadSkillCombined(){
  const box = document.getElementById('sk-cards');
  if(!box) return;
  const f = document.getElementById('sk-filter')?.value || '';
  const sum = document.getElementById('sk-market-summary');
  if(f==='draft'){
    return loadSkillDrafts(box, sum);
  }
  try{
    const [mkt, mine] = await Promise.all([
      api('/api/studio/market?kind=skill'),
      api('/api/studio/plugins/mine?kind=skill'),
    ]);
    const mktItems = mkt.items || [];   // 内置由市场侧渲染（平台内置；个人空间不提供内置插件）
    const mineItems = mine.items || [];
    let items = [];
    for(const it of mineItems){
      if(it.builtin) continue;                  // 个人空间不提供内置插件
      if(it.source_ref) items.push({...it, view:'market-installed'});
      else items.push({...it, view:'personal'});
    }
    for(const it of mktItems){
      if(it.installed) continue;  // 已添加副本已由私人侧渲染
      items.push(it.builtin ? {...it, view:'builtin'} : {...it, view:'market-add'});
    }
    if(f==='market') items = items.filter(x=>x.view==='market-add' || x.view==='market-installed');
    else if(f==='custom') items = items.filter(x=>x.view==='personal');
    else if(f==='builtin') items = items.filter(x=>x.view==='builtin');
    const mkN = items.filter(x=>x.view==='market-add' || x.view==='market-installed').length;
    const pN = items.filter(x=>x.view==='personal').length;
    const bN = items.filter(x=>x.view==='builtin').length;
    if(sum) sum.textContent = `共 ${items.length} 个 · 插件市场 ${mkN} · 自定义 ${pN} · 内置 ${bN}`;
    if(!items.length){ box.innerHTML = '<div style="grid-column:1/-1;padding:36px;text-align:center;color:var(--mut);font-size:12px;">暂无匹配的技能（调整筛选条件），或点击「＋ 添加技能」创建/上传</div>'; return; }
    box.innerHTML = items.map(it=>{
      if(it.view==='market-add'){
        return `<div class="panel"><div class="ph">${esc(it.name)} <span class="st b">Skill</span></div>
        <div class="pb">
          <div style="font-size:11.5px;color:var(--mut);min-height:34px;max-height:60px;overflow:hidden;" title="${dispDescr(it)}">${dispDescr(it)}</div>${cardMeta(it)}
          <div style="margin-top:8px;"><button class="btn sm" onclick="marketInstall('skill','${esc(String(it.name).replace(/'/g,"\\'"))}',this)">＋ 添加</button></div>
        </div></div>`;
      }
      if(it.view==='builtin'){
        return `<div class="panel"><div class="ph">${esc(it.name)} <span class="st b">平台内置</span></div>
        <div class="pb">
          <div style="font-size:11.5px;color:var(--mut);min-height:34px;max-height:60px;overflow:hidden;" title="${dispDescr(it)}">${dispDescr(it)}</div>${cardMeta(it)}
          <div style="margin-top:8px;"><span style="font-size:10.5px;color:var(--mut);">平台预置技能，直接可用</span></div>
        </div></div>`;
      }
      const tag = it.view==='market-installed' ? '<span class="st" style="background:#e8f5e9;color:#2e7d32;">来自市场</span>' : '<span class="st amb">自定义</span>';
      const st = it.enabled===0 ? '<span class="st r">停用</span>' : '<span class="st ok">启用</span>';
      // 2026-09-17 第二刀：分享/撤回统一走 /api/plugins/{plugin_id}/*（与能力中心同一套守卫与语义）。
      //   · 旧实现调 /api/studio/share/submit 且传 plugins 表数值主键，后端按 name/label 查找
      //     必 404 —— 按钮形式存在、点了必失败；
      //   · submitted 态此前只渲染标签、无撤回入口（P1-1 用户旅程级准死锁）；
      //   · 另修：技能卡此处原误写 sharePlugin('mcp',…)，现按条目自身 type 派生，不再硬编码。
      const _sk = esc(it.type || it.kind || '');
      let share = '';
      if(it.view==='personal' && it.plugin_id){
        if(it.share_status==='submitted')
          share = `<span class="st amb">申请上架中</span><button class="btn sm ghost" title="撤回后仅自己可见，能力照常可用" onclick="cancelSharePlugin('${_sk}','${esc(it.plugin_id)}')">↩ 撤回申请</button>`;
        else if(it.share_status==='rejected')
          share = `<span class="st r">已驳回</span><button class="btn sm ghost" onclick="sharePlugin('${_sk}','${esc(it.plugin_id)}')">↻ 重新申请</button>`;
        else if(it.scope !== 'public')
          share = `<button class="btn sm ghost" onclick="sharePlugin('${_sk}','${esc(it.plugin_id)}')">⬆ 分享到市场</button>`;
      }
      let ops = `<button class="btn sm ghost" onclick="editMCP(${it.id})">✏️</button>
        <button class="btn sm ghost" title="复制为副本" onclick="copySkill(${it.id})">📑 复制</button>
        <button class="btn sm ${it.enabled===0?'ok':'ghost amb'}" onclick="toggleMCPStatus(${it.id},${it.enabled===0?1:0})">${it.enabled===0?'启用':'停用'}</button>
        <button class="btn sm red" onclick="deleteMCP(${it.id})">删除</button>`;
      return `<div class="panel"><div class="ph"> ${esc(it.name)} ${tag} <span style="flex:1"></span>${st}</div>
      <div class="pb">
        <div style="font-size:11.5px;color:var(--mut);line-height:1.5;min-height:34px;" title="${dispDescr(it)}">${dispDescr(it)}</div>${cardMeta(it)}
        <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">${ops}${share}</div>
      </div></div>`;
    }).join('');
  }catch(e){ box.innerHTML = '<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--red);">加载失败：' + esc(e.message||e) + '</div>'; }
}
// 兼容旧调用点（loadMarket/loadMinePlugins/写操作刷新链）：已整合为单视图
async function loadSkillMarket(){ return loadSkillCombined(); }
async function loadMySkills(){ return loadSkillCombined(); }
// MCP 工具整合视图：自定义 + 插件市场 单入口（分类筛选；操作入口在右上角）
async function loadMCPCombined(){
  const box = document.getElementById('mcp-cards');
  if(!box) return;
  const f = document.getElementById('mcp-filter')?.value || '';
  const sum = document.getElementById('mcp-market-summary');
  try{
    const [mkt, mine] = await Promise.all([
      api('/api/studio/market?kind=mcp'),
      api('/api/studio/plugins/mine?kind=mcp'),
    ]);
    const mktItems = mkt.items || [];   // 内置由市场侧渲染（平台内置；个人空间不提供内置插件）
    const mineItems = mine.items || [];
    let items = [];
    for(const it of mineItems){
      if(it.builtin) continue;                  // 个人空间不提供内置插件
      if(it.source_ref) items.push({...it, view:'market-installed'});
      else items.push({...it, view:'personal'});
    }
    for(const it of mktItems){
      if(it.installed) continue;  // 已添加副本已由私人侧渲染
      items.push(it.builtin ? {...it, view:'builtin'} : {...it, view:'market-add'});
    }
    if(f==='custom') items = items.filter(x=>x.view==='personal');
    else if(f==='market') items = items.filter(x=>x.view==='market-add' || x.view==='market-installed');
    else if(f==='builtin') items = items.filter(x=>x.view==='builtin');
    const mkN = items.filter(x=>x.view==='market-add' || x.view==='market-installed' || x.view==='builtin').length;
    const pN = items.filter(x=>x.view==='personal').length;
    if(sum) sum.textContent = `共 ${items.length} 个 · 插件市场 ${mkN} · 自定义 ${pN}`;
    const mcpCnt = document.getElementById('tm-mcp-count');
    if(mcpCnt) mcpCnt.textContent = items.length;
    if(!items.length){ box.innerHTML = '<div style="grid-column:1/-1;padding:36px;text-align:center;color:var(--mut);font-size:12px;">暂无匹配的工具（调整筛选条件），或点击「＋ 创建工具」创建</div>'; return; }
    box.innerHTML = items.map(it=>{
      if(it.view==='market-add'){
        return `<div class="panel"><div class="ph"> ${esc(it.name)} <span class="st" style="background:#e8f5e9;color:#2e7d32;">来自市场</span></div>
        <div class="pb">
          <div style="font-size:11.5px;color:var(--mut);line-height:1.5;min-height:34px;" title="${dispDescr(it)}">${dispDescr(it)}</div>${cardMeta(it)}
          <div style="margin-top:8px;"><button class="btn sm" onclick="marketInstall('mcp','${esc(String(it.name).replace(/'/g,"\\'"))}',this)">＋ 添加</button></div>
        </div></div>`;
      }
      if(it.view==='builtin'){
        return `<div class="panel"><div class="ph">${esc(it.name)} <span class="st b">内置</span></div>
        <div class="pb">
          <div style="font-size:11.5px;color:var(--mut);line-height:1.5;min-height:34px;" title="${dispDescr(it)}">${dispDescr(it)}</div>${cardMeta(it)}
          <div style="margin-top:8px;"><span style="font-size:10.5px;color:var(--mut);">平台预置，直接可用</span></div>
        </div></div>`;
      }
      const tag = it.view==='market-installed' ? '<span class="st" style="background:#e8f5e9;color:#2e7d32;">来自市场</span>' : '<span class="st amb">自定义</span>';
      const st = it.enabled===0 ? '<span class="st r">停用</span>' : '<span class="st ok">启用</span>';
      // 2026-09-17 第二刀：分享/撤回统一走 /api/plugins/{plugin_id}/*（与能力中心同一套守卫与语义）。
      //   · 旧实现调 /api/studio/share/submit 且传 plugins 表数值主键，后端按 name/label 查找
      //     必 404 —— 按钮形式存在、点了必失败；
      //   · submitted 态此前只渲染标签、无撤回入口（P1-1 用户旅程级准死锁）；
      //   · 另修：技能卡此处原误写 sharePlugin('mcp',…)，现按条目自身 type 派生，不再硬编码。
      const _sk = esc(it.type || it.kind || '');
      let share = '';
      if(it.view==='personal' && it.plugin_id){
        if(it.share_status==='submitted')
          share = `<span class="st amb">申请上架中</span><button class="btn sm ghost" title="撤回后仅自己可见，能力照常可用" onclick="cancelSharePlugin('${_sk}','${esc(it.plugin_id)}')">↩ 撤回申请</button>`;
        else if(it.share_status==='rejected')
          share = `<span class="st r">已驳回</span><button class="btn sm ghost" onclick="sharePlugin('${_sk}','${esc(it.plugin_id)}')">↻ 重新申请</button>`;
        else if(it.scope !== 'public')
          share = `<button class="btn sm ghost" onclick="sharePlugin('${_sk}','${esc(it.plugin_id)}')">⬆ 分享到市场</button>`;
      }
      let ops = `<button class="btn sm ghost" onclick="editMCP(${it.id})">✏️</button>
        <button class="btn sm ghost" title="复制为副本" onclick="copyMCP(${it.id})">📑 复制</button>
        <button class="btn sm ${it.enabled===0?'ok':'ghost amb'}" onclick="toggleMCPStatus(${it.id},${it.enabled===0?1:0})">${it.enabled===0?'启用':'停用'}</button>
        <button class="btn sm red" onclick="deleteMCP(${it.id})">删除</button>`;
      return `<div class="panel"><div class="ph"> ${esc(it.name)} ${tag} <span style="flex:1"></span>${st}</div>
      <div class="pb">
        <div style="font-size:11.5px;color:var(--mut);line-height:1.5;min-height:34px;" title="${dispDescr(it)}">${dispDescr(it)}</div>${cardMeta(it)}
        <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">${ops}${share}</div>
      </div></div>`;
    }).join('');
  }catch(e){ box.innerHTML = '<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--red);">加载失败：' + esc(e.message||e) + '</div>'; }
}
// 兼容旧调用点（loadMarket/loadMinePlugins/写操作刷新链）：已整合为单视图
async function loadMCPMarket(){ return loadMCPCombined(); }
async function loadMyMCPs(){ return loadMCPCombined(); }
// ── 工具管理：MCP 工具 / HTTP 工具 / 内置工具 三个子菜单并排 ──
let _toolTab = 'tm-mcp';
function switchToolTab(el, id) {
  _toolTab = id;
  document.querySelectorAll('#st-mcp .subtab span[data-tab]').forEach(s=>s.classList.toggle('on', s.dataset.tab===id));
  ['tm-mcp','tm-http'].forEach(p=>{ const d = document.getElementById(p); if(d) d.classList.toggle('on', p===id); });
  if(id==='tm-mcp') loadMCPCombined();
  if(id==='tm-http') loadHttpTools();
}
// HTTP 工具：tools 表 source='http'（工具注册表）
async function loadHttpTools(){
  const box = document.getElementById('http-tool-cards');
  if(!box) return;
  const f = document.getElementById('http-filter')?.value || '';
  try{
    const all = await api('/api/studio/tools');
    // HTTP 集成工具按 kind='http' 识别（source 可能是 http/zhiyuan/外部系统名，2026-09-11）
    let items = all.filter(t=>(t.source||'')==='http' || ((t.source||'')==='builtin' || t.builtin===1) && t.config && (()=>{try{const c=typeof t.config==='string'?JSON.parse(t.config):t.config;return c.method;}catch(e){return null;}})());
    // 简化：HTTP tab 显示 kind='http' 的工具 + 内置工具中有 HTTP config 的
    items = all.filter(t=>{
      const isHttp = (t.source||'')==='http' || (t.kind||'')==='http';
      const isBuiltinHttp = ((t.source||'')==='builtin' || t.builtin===1) && (()=>{try{const c=typeof t.config==='string'?JSON.parse(t.config):t.config;return c&&c.method;}catch(e){return false;}})();
      return isHttp || isBuiltinHttp;
    });
    if(f==='custom') items = items.filter(t=>(t.source||'')==='http' || (t.kind||'')==='http');
    else if(f==='builtin') items = items.filter(t=>(t.source||'')==='builtin' || t.builtin===1);
    const cnt = document.getElementById('tm-http-count');
    if(cnt) cnt.textContent = items.length;
    renderToolSourceCards(box, 'http-tool-summary', items, 'http');
  }catch(e){ box.innerHTML = '<div style="grid-column:1/-1;padding:24px;text-align:center;color:var(--red);">加载失败：' + esc(e.message||e) + '</div>'; }
}

function renderToolSourceCards(box, sumId, items, kind){
  const sum = document.getElementById(sumId);
  if(sum) sum.textContent = `共 ${items.length} 个工具`;
  if(!items.length){
    box.innerHTML = '<div style="grid-column:1/-1;padding:36px;text-align:center;color:var(--mut);font-size:12px;">暂无匹配的工具（调整筛选条件），或点击「＋ 新增工具」创建</div>';
    return;
  }
  box.innerHTML = items.map(t=>toolSourceCard(t, kind)).join('');
}
async function copySkill(id){
  const r = await api(`/api/studio/skills/${id}/copy`,{method:'POST',body:'{}'}).catch(e=>({error:e.message}));
  if(r.error){ toast('复制失败：'+r.error); return; }
  toast('已复制：'+r.name); loadSkillCombined(); loadMarket();
}
async function copyMCP(id){
  const r = await api(`/api/studio/mcp-servers/${id}/copy`,{method:'POST',body:'{}'}).catch(e=>({error:e.message}));
  if(r.error){ toast('复制失败：'+r.error); return; }
  toast('已复制：'+r.name); loadMCPCombined(); loadMarket();
}
async function copyTool(id){
  const r = await api(`/api/studio/tools/${id}/copy`,{method:'POST',body:'{}'}).catch(e=>({error:e.message}));
  if(r.error){ toast('复制失败：'+r.error); return; }
  toast('已复制：'+r.name); loadHttpTools();
}
async function copyAgent(id){
  const r = await api(`/api/studio/agents/${id}/copy`,{method:'POST',body:'{}'}).catch(e=>({error:e.message}));
  if(r.error){ toast('复制失败：'+r.error); return; }
  toast('已复制：'+r.name); loadAgents();
}
function toolSourceCard(t, kind){
  const builtin = (t.source==='builtin' || t.builtin===1);
  const typeTag = builtin ? '<span class="st b">内置</span>' : '<span class="st amb">自定义</span>';
  const st = `<span class="st ${t.status==='active'?'ok':'r'}">${t.status==='active'?'启用':'停用'}</span>`;
  // 2026-09-17 第二刀：工具列表后端已补 plugin_id（见 routers/studio_parts/tools.py），
  // 分享直接走 /api/plugins/{plugin_id}/share|unshare —— 与技能/MCP 卡完全同一套守卫与语义。
  //   · 旧实现按 name 反查必 404（按钮在、点了报「未找到插件」）；
  //   · 未纳入 plugins 表的工具不渲染分享入口（无处可分享，如实呈现）。
  const _tpid = esc(t.plugin_id || '');
  let share = '';
  if(!builtin && t.view==='personal' && _tpid) {
    if(t.share_status==='submitted')
      share = `<span class="st amb">申请上架中</span><button class="btn sm ghost" title="撤回后仅自己可见，能力照常可用" onclick="cancelSharePlugin('tool','${_tpid}')">↩ 撤回申请</button>`;
    else if(t.share_status==='rejected')
      share = `<span class="st r">已驳回</span><button class="btn sm ghost" onclick="sharePlugin('tool','${_tpid}')">↻ 重新申请</button>`;
    else if(t.scope !== 'public')
      share = `<button class="btn sm ghost" onclick="sharePlugin('tool','${_tpid}')">⬆ 分享到市场</button>`;
  }
  // 另修（2026-09-17）：原字符串把「删除」与「复制」两个 button 拼串行 —— 复制按钮内嵌了
  // onclick="deleteTool(...)" 的片段，浏览器解析后「删除」按钮实际不可点。
  const ops = builtin
    ? `<button class="btn sm ghost" onclick="showToolForm(${t.id})">✏️</button>
       <button class="btn sm ${t.status==='active'?'ghost amb':'ok'}" onclick="toggleToolStatus(${t.id},${t.status==='active'?0:1})">${t.status==='active'?'停用':'启用'}</button>`
    : `<button class="btn sm ghost" onclick="showToolForm(${t.id})">✏️</button>
       <button class="btn sm ghost" title="复制为副本" onclick="copyTool(${t.id})">📑 复制</button>
       <button class="btn sm ${t.status==='active'?'ghost amb':'ok'}" onclick="toggleToolStatus(${t.id},${t.status==='active'?0:1})">${t.status==='active'?'停用':'启用'}</button>
       <button class="btn sm red" onclick="deleteTool(${t.id})">删除</button>`;
  return `<div class="panel">
    <div class="ph">${esc(t.name)} ${typeTag} <span style="flex:1"></span>${st}</div>
    <div class="pb">
      <div style="font-size:11.5px;color:var(--mut);line-height:1.5;min-height:34px;" title="${esc(t.description||'')}">${esc(t.description||'-')}</div>
      <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">${ops}${share}</div>
    </div></div>`;
}

// 个人插件申请上架（2026-09-17 第二刀：统一到 /api/plugins/{pid}/share）
//   · 语义 =「只改可见范围 scope（personal→pending_public）」，能力对作者照常可用；
//   · 旧实现调 /api/studio/share/submit 且传 plugins 表数值主键，后端按 name/label
//     查找必 404 —— 按钮在、点了必失败（P0-3 遗留链路的用户可见症状）。
async function sharePlugin(kind, pid){
  if(!pid){ toast('缺少插件标识，无法分享'); return; }
  if(!(await confirmDialog('确定将该插件分享到插件市场？\n提交后需管理员审核：通过则进入市场，驳回则返回个人空间。\n（审核期间该能力对你仍照常可用）'))) return;
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/share', {method:'POST', body:'{}'});
    if(r.error){ toast(r.error); return; }
    toast('已提交上架申请，等待管理员审核');
    refreshCapabilityViews(); loadMarket(); loadMarketManage();
  }catch(e){ toast('提交失败：' + (e.message||e)); }
}
// 撤回上架申请（作者侧，pending_public → personal）。后端 /unshare 按 scope 分派：
//   申请中 → cancel_share（作者可撤回）；已上架 → withdraw_share（仅市场管理员）
async function cancelSharePlugin(kind, pid){
  if(!pid){ toast('缺少插件标识，无法撤回'); return; }
  if(!(await confirmDialog('撤回上架申请？\n撤回后仅自己可见，该能力对你照常可用。'))) return;
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/unshare', {method:'POST', body:'{}'});
    if(r.error){ toast(r.error); return; }
    toast('已撤回上架申请（仅自己可见）');
    refreshCapabilityViews(); loadMarket(); loadMarketManage();
  }catch(e){ toast('撤回失败：' + (e.message||e)); }
}
// 管理员审核上架申请（2026-09-17 双轨合并：统一到 /api/plugins/{pid}/review）
//   approve → scope=public（上架）；reject → scope=personal（返回个人空间，可重新申请）
async function shareReview(kind, pid, action){
  const isApprove = action==='approve';
  const c = await promptDialog({
    title: isApprove ? '通过上架审核' : '驳回上架审核',
    message: isApprove ? '确认通过该上架申请？通过后该能力进入公共市场，全团队可见、可安装。' : '确认驳回该上架申请？驳回后返回个人空间，作者可修改后重新申请。',
    value: '',
    placeholder: isApprove ? '（可选）审核备注' : '请填写驳回原因（可选）',
    okText: isApprove ? '通过' : '驳回',
    multiline: true, rows: 2
  });
  if(c === null || c === undefined) return;
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/review',
      {method:'POST', body:JSON.stringify({action, comment: c})});
    if(r.error){ toast(r.error); return; }
    // 后端按对象分派并回传 review_kind（2026-09-17 第二刀：双轨合并后不再混装）：
    //   share   = 上架审核（scope 维度，通过即入市）
    //   publish = 历史遗留「发布需审核」（status 维度，通过**不上架**）
    // 此前统一提示「已上架市场」，对遗留条目是虚假承诺。
    if(r.review_kind === 'publish'){
      toast(isApprove ? '已通过（历史发布审核：仅发布可用，未上架市场）' : '已驳回');
    }else{
      toast(isApprove ? '已通过并上架市场' : '已驳回申请（返回个人空间）');
    }
    loadMarketManage(); loadMarket(); loadUnifiedPending();
  }catch(e){ toast('操作失败：' + (e.message||e)); }
}
// 审批记录（提交/通过/驳回留痕）
async function openShareLog(){
  try{
    const r = await api('/api/studio/share/log').catch(()=>({items:[]}));
    const actMap = {submit:['已提交分享','st amb'], approve:['已通过（入市）','st ok'], reject:['已驳回','st r']};
    const rows = (r.items||[]).map(l=>{
      const m = actMap[l.action] || [l.action, 'st'];
      return `<tr><td style="white-space:nowrap;">${esc(l.created_at||'')}</td><td>${l.kind==='skill'?'🧩 技能':'🔌 MCP'}</td><td><b>${esc(l.item_name)}</b></td><td><span class="${m[1]}">${m[0]}</span></td><td>${esc(l.operator||'')}</td><td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(l.comment||'')}">${esc(l.comment||'-')}</td></tr>`;
    }).join('') || '<tr><td colspan="6" style="text-align:center;color:var(--mut);">暂无审批记录</td></tr>';
    openPanel('📋 插件审批记录', `<table class="t"><tr><th>时间</th><th>类型</th><th>条目</th><th>动作</th><th>操作人</th><th>备注</th></tr>${rows}</table>`);
  }catch(e){ toast('加载审批记录失败：' + (e.message||e)); }
}
// 添加技能下拉（创建 / 上传两种方式）——fixed 定位防漂移
function toggleAddSkillMenu(ev){
  toggleDropMenu('add-skill-menu', document.getElementById('add-skill-btn'), ev);
}
function hideAddSkillMenu(){
  const m = document.getElementById('add-skill-menu');
  if(m) m.style.display = 'none';
}
document.addEventListener('click', function(e){
  const m = document.getElementById('add-skill-menu');
  if(!m || m.style.display==='none') return;
  const btn = document.getElementById('add-skill-btn');
  if((btn && btn.contains(e.target)) || m.contains(e.target)) return;
  m.style.display = 'none';
});
async function marketInstall(kind, name, btn){
  if(!(await confirmDialog(`确定添加「${name}」到您的个人空间？\n将复制一份独立副本（版本固定，可自由编辑/停用/删除，不受市场更新影响）。`))) return;
  if(btn){ btn.disabled = true; btn.textContent = '添加中…'; }
  try{
    const r = await api('/api/studio/market/install', {method:'POST', body:JSON.stringify({kind, name})});
    if(r.error){ toast('添加失败：' + r.error); if(btn){btn.disabled=false; btn.textContent='＋ 添加';} return; }
    toast(kind==='skill' ? '已添加到个人空间（来自技能市场）' : kind==='mcp' ? '已添加到个人空间（来自MCP市场）' : '已安装到「我的插件」');
    loadMarket(); loadMinePlugins(); loadMarketManage();
  }catch(e){ toast('添加失败：' + (e.message||e)); if(btn){btn.disabled=false; btn.textContent='＋ 添加';} }
}
// 种类切换时联动刷新分类下拉（分类随 kind 范围变化）；同类切换不重置用户选择
let _marketCatsLoadedKind = null;
// 插件市场（2026-09-17 定稿）：全量市场插件统一管理 —— 按类型纵向分区铺开，
// 支持 类型/状态/来源 筛选；disabled 条目可「恢复上架」；页尾联动上架审核队列 + 已驳回 + 审核策略
const _MANAGE_TYPE_ORDER = ['skill','mcp','tool','agent','prompt','bundle'];
async function loadMarketManage(){
  const box = document.getElementById('market-manage-sections');
  if(!box) return;
  // 2026-09-17：目录对所有人可见（浏览 + 安装）；管理动作（编辑/置顶/停用/删除/恢复/审核/策略）仅市场管理员渲染
  const can = await canManageMarket();
  const kind = document.getElementById('mm-kind')?.value || 'all';
  const ori = document.getElementById('mm-origin')?.value || '';
  const st = document.getElementById('mm-status')?.value || '';
  const q = document.getElementById('mm-search')?.value.trim() || '';
  const sum = document.getElementById('mm-summary');
  try{
    const r = await api('/api/studio/market?kind=' + encodeURIComponent(kind) + '&q=' + encodeURIComponent(q));
    // 2026-09-17：不再滤除 builtin —— 需求「展示所有的市场中的插件」；内置条目打标，管理动作受限（系统组成部分）
    let items = (r.items || []);
    if(ori) items = items.filter(x=>(x.origin||'')===ori);
    if(st) items = items.filter(x=>(x.status||'published')===st);
    const sc = document.getElementById('mm-sale-count');
    if(sc) sc.textContent = items.length;
    const builtinN = items.filter(x=>!!x.builtin).length;
    const adminN = items.filter(x=>(x.origin||'')!=='share' && !x.builtin).length;
    const shareN = items.filter(x=>(x.origin||'')==='share').length;
    const offN = items.filter(x=>(x.status||'published')==='disabled').length;
    if(sum) sum.textContent = `共 ${items.length} 条 · 平台内置 ${builtinN} · 管理员创建 ${adminN} · 个人分享 ${shareN} · 已停用 ${offN}`;
    if(!items.length){
      box.innerHTML = '<div style="padding:24px;text-align:center;color:var(--mut);font-size:12px;">暂无符合条件的市场插件（「＋ 新建插件」创建即上架；个人分享需通过审核后入市场）</div>';
    } else {
      // 按类型分组，纵向按区域铺开
      const groups = {};
      items.forEach(it=>{ const k = it.kind || it.type || 'other'; (groups[k] = groups[k] || []).push(it); });
      const ordered = _MANAGE_TYPE_ORDER.filter(k=>groups[k]).concat(Object.keys(groups).filter(k=>!_MANAGE_TYPE_ORDER.includes(k)));
      const _micon = {skill:'🧩', mcp:'🔌', tool:'🛠', agent:'🤖', prompt:'📝', bundle:'📦'};
      const _mlabel = {skill:'技能 Skill', mcp:'MCP', tool:'工具 Tool', agent:'Agent', prompt:'Prompt', bundle:'Bundle'};
      box.innerHTML = ordered.map(k=>{
        const list = groups[k];
        const cards = list.map(it=>{
          const isBuiltin = !!it.builtin;
          const oriBadge = isBuiltin
            ? '<span class="st b" title="平台预置能力：默认全员可用，可被个人停用，不可下架/删除">🏛 平台内置</span>'
            : (it.origin||'')==='share'
              ? '<span class="st" style="background:#e8f5e9;color:#2e7d32;">🟩 个人分享</span>'
              : '<span class="st b">🟦 管理员创建</span>';
          const off = (it.status||'published')==='disabled';
          const stBadge = off ? '<span class="st r">⛔ 已停用</span>' : '<span class="st ok">已发布</span>';
          // 内置能力：可 安装/启停/编辑/置顶/恢复上架；不提供 删除（系统组成部分，后端拒绝 soft_delete）
          // 2026-09-17：提供者（is_mine）在目录中视为已安装 —— 消费判定本就含自建已发布能力，无需重复安装
          const inst = !!it.installed || !!it.is_mine;
          // 动作区分两组（2026-09-17 入口归位）：消费动作（安装/已安装）与市场治理动作。
          //   治理动作只在市场页出现（本页即市场），前三个类型 tab 不再提供 —— 见 36-capability.js。
          const _nm = esc(String(it.name).replace(/'/g, "\\'"));
          const _pid = esc(it.plugin_id || '');
          const cons = off
            ? (can ? `<button class="btn sm ok" title="恢复为已发布（全局生效）：个人可重新安装" onclick="marketGlobalToggle('${_pid}', true)">▶ 恢复发布</button>` : '')
            : (inst
                ? `<span class="st ok" title="已在你的能力清单中，无需重复安装">✓ 已安装</span>`
                : ((it.kind==='skill'||it.kind==='mcp') && !isBuiltin
                    ? `<button class="btn sm" title="实例化到个人技能/MCP" onclick="marketInstall('${it.kind}','${_nm}',this)">＋ 添加</button>`
                    : `<button class="btn sm" title="安装到我的能力（个人启停只影响自己）" onclick="unifiedToggleInstall('${_pid}', true)">＋ 安装</button>`));
          const gov = can ? `${off ? '' : `<button class="btn sm ghost amb" title="全局停用：暂停新安装，已安装者同步失效（条目仍留在市场）" onclick="marketGlobalToggle('${_pid}', false)">⛔ 全局停用</button>`}
             <button class="btn sm ghost" title="编辑清单/载荷（与类型页新增/编辑一致）" onclick="marketEditUnified('${_pid}')">✏️</button>
             <button class="btn sm ghost" title="${it.pinned?'取消置顶':'置顶'}" onclick="marketPin('${it.kind}','${_nm}')">📌</button>
             ${isBuiltin ? '' : `<button class="btn sm ghost" title="下架：撤回上架回个人空间，他人不再可见/可装；作者仍可用" onclick="marketUnpublish('${_pid}','${_nm}')">↩ 下架</button>
             <button class="btn sm red" title="删除该能力本体（软删除，审计保留）" onclick="marketDeletePlugin('${_pid}','${_nm}')">🗑</button>`}` : '';
          const ops = cons + (gov
            ? `<span style="width:1px;height:16px;background:var(--line,#e3e3e3);display:inline-block;"></span>${gov}`
            : '');
          return `<div class="panel"><div class="ph">${_micon[k]||'🧩'} ${esc(it.name)} ${oriBadge} ${stBadge} <span style="flex:1"></span></div>
      <div class="pb">
        <div style="font-size:11.5px;color:var(--mut);min-height:32px;max-height:58px;overflow:hidden;" title="${esc(it.descr||'')}">${dispDescr(it)}</div>${cardMeta(it)}
        <div class="kv"><span>分类</span><b>${esc(it.category||_mlabel[k]||'—')}</b></div>
        <div class="kv"><span>版本</span><b>${esc(it.version||'—')}</b></div>
        <div class="kv"><span>安装</span><b>${it.install_count||0} 次</b></div>
        <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">${ops}</div>
      </div></div>`;
        }).join('');
        return `<div style="margin-bottom:14px;">
          <div style="font-size:12.5px;font-weight:600;margin-bottom:6px;">${_micon[k]||'🧩'} ${_mlabel[k]||k} <span class="tag">${list.length}</span></div>
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;">${cards}</div>
        </div>`;
      }).join('');
    }
  }catch(e){ box.innerHTML = '<div style="padding:16px;color:var(--red);">加载失败：' + esc(e.message||e) + '</div>'; }
  // 页尾管理区仅市场管理员可见：上架审核队列（/api/plugins/pending，含 scope=pending_public 新语义）+ 审核策略 + 已驳回
  const admz = document.getElementById('market-admin-zone');
  if(admz) admz.style.display = can ? '' : 'none';
  // 上架审核 tab 同样仅管理员可见（普通用户在插件清单 tab 浏览/安装即可）
  const rt = document.querySelector('#mm-sale .subtab span[data-mtab="review"]');
  if(rt) rt.style.display = can ? '' : 'none';
  if(can){
    loadUnifiedPending();
    loadUnifiedSettings();
    loadShareManage();  // 渲染已驳回表（share-pending-table 已并入 unified 队列，不再单独渲染）
  }
}
// 市场级启停（2026-09-17）：统一走 PUT /enabled（store.set_enabled，仅市场管理员）——
//   false = 全局停用（暂停新安装 + 已安装者运行时失效，scope 仍为 public）
//   true  = 恢复上架（回到已发布）
// 旧 marketUnpublish（下架回个人空间）从卡片入口移除；撤回上架走 withdraw_share 语义另案。
async function marketGlobalToggle(pid, enabled){
  if(!pid){ toast('该条目缺少 plugin_id，无法操作'); return; }
  if(!enabled && !(await confirmDialog('全局停用后：个人暂不可安装，已安装者同步失效。\n确定停用？'))) return;
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/enabled', {method:'PUT', body:JSON.stringify({enabled})});
    if(r.error) toast(r.error); else { toast(enabled ? '已恢复上架（全局发布）' : '已全局停用'); loadMarketManage(); }
  }catch(e){ toast('操作失败：' + (e.message||e)); }
}
// 市场卡片编辑（2026-09-17）：与前三个类型 tab 的新增/编辑一致 ——
// 有 legacy 映射（迁移能力）→ 打开原专用表单（editSkill/editAgent/showToolForm/editMCP/editPrompt）；
// 无映射（纯插件能力）→ 回退统一插件清单表单 openUnifiedPluginForm（预填 manifest/SKILL.md/server.json）
async function marketEditUnified(pid){
  if(!pid){ toast('该条目缺少 plugin_id，无法编辑'); return; }
  try{
    const d = await api('/api/plugins/' + encodeURIComponent(pid));
    if(d.error){ toast(d.error); return; }
    const rt = (d.manifest && d.manifest.runtime) || {};
    const route = { skills:'editSkill', agents:'editAgent', tools:'showToolForm',
                    mcp_servers:'editMCP', prompts:'editPrompt' }[rt.legacy_table];
    const lid = parseInt(rt.legacy_id, 10);
    if(route && typeof window[route] === 'function' && lid > 0){ window[route](lid); return; }
    openUnifiedPluginForm(pid);
  }catch(e){ toast('打开编辑失败：' + (e.message||e)); }
}
// 插件市场内部水平 tab：插件清单 / 上架审核（2026-09-17）
function switchMarketSubTab(el, tab){
  document.querySelectorAll('#mm-sale .subtab span[data-mtab]').forEach(function(s){ s.classList.toggle('on', s.dataset.mtab===tab); });
  const c = document.getElementById('mm-sale-catalog');
  const r = document.getElementById('mm-sale-review');
  if(c) c.classList.toggle('on', tab==='catalog');
  if(r) r.classList.toggle('on', tab==='review');
  if(tab==='review') loadUnifiedPending();
}
// 分享管理：个人分享存储与审核（待审核/已驳回，与审批记录整合；未通过审核不入市场清单）
async function loadShareManage(){
  const pBox = document.getElementById('share-pending-table');
  const rBox = document.getElementById('share-rejected-table');
  if(!pBox && !rBox) return;
  try{
    const r = await api('/api/studio/share/items?kind=all');
    const items = r.items || [];
    const pend = items.filter(x=>x.share_status==='submitted');
    const rej = items.filter(x=>x.share_status==='rejected');
    const pc = document.getElementById('share-pending-count');
    if(pc) pc.textContent = pend.length;
    const rc = document.getElementById('share-rejected-count');
    if(rc) rc.textContent = rej.length;
    const mc = document.getElementById('mm-share-count');
    if(mc) mc.textContent = pend.length + rej.length;
    if(pBox){
      pBox.innerHTML = pend.length
        ? `<table class="t"><tr><th>类型</th><th>名称</th><th>描述</th><th>版本</th><th>提交时间</th><th>操作</th></tr>` +
          pend.map(it=>`<tr>
            <td>${(MARKET_KIND_META[it.kind]||{}).label||it.kind}</td>
            <td><b>${esc(it.name)}</b></td>
            <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(it.descr||'')}">${dispDescr(it)}</td>
            <td>${esc(it.version||'-')}</td>
            <td>${esc((it.created_at||'').slice(0,19))}</td>
            <td style="white-space:nowrap;">
              <button class="btn sm ok" onclick="shareReview('${esc(it.kind)}','${esc(it.plugin_id||it.id)}','approve')">✅ 通过</button>
              <button class="btn sm red" onclick="shareReview('${esc(it.kind)}','${esc(it.plugin_id||it.id)}','reject')">❌ 驳回</button>
            </td>
          </tr>`).join('') + `</table>`
        : '<div style="padding:16px;color:var(--mut);font-size:12px;">暂无待审核的分享申请</div>';
    }
    if(rBox){
      rBox.innerHTML = rej.length
        ? `<table class="t"><tr><th>类型</th><th>名称</th><th>描述</th><th>版本</th><th>操作</th></tr>` +
          rej.map(it=>`<tr>
            <td>${(MARKET_KIND_META[it.kind]||{}).label||it.kind}</td>
            <td><b>${esc(it.name)}</b></td>
            <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(it.descr||'')}">${dispDescr(it)}</td>
            <td>${esc(it.version||'-')}</td>
            <td style="white-space:nowrap;">
              <button class="btn sm ghost" title="编辑" onclick="marketEditPrivateItem('${it.kind}',${it.id})">✏️</button>
              <button class="btn sm red" title="删除" onclick="deleteMarketItem('${it.kind}',${it.id},'${esc(String(it.name).replace(/'/g,"\\'"))}')">🗑</button>
            </td>
          </tr>`).join('') + `</table>`
        : '<div style="padding:16px;color:var(--mut);font-size:12px;">暂无驳回记录</div>';
    }
  }catch(e){
    if(pBox) pBox.innerHTML = '<div style="padding:12px;color:var(--red);">加载失败：' + esc(e.message||e) + '</div>';
  }
}
// 新建插件：创建后自动上架（管理员创建来源，origin=admin），立即出现在市场在售清单
let _mmCreate = null;   // 当前新建类型（skill|tool|mcp），保存成功后自动上架
function mmNewForm(kind){
  _mmCreate = kind;
  if(kind==='skill') openSkillForm();
  else if(kind==='mcp') showToolForm(null);
  else showToolForm();
  hideManageNewMenu();
}
async function mmAutoPublish(kind, name){
  try{
    const r = await api(`/api/studio/market/${kind}/${encodeURIComponent(name)}/publish`, {method:'POST', body:'{}'});
    if(r.error){ toast('自动上架失败：' + r.error); return; }
    // 2026-09-17 第二刀：该兼容入口 = 发布（自用）+ 申请上架两步，不再直改 status。
    //   返回 scope=pending_public 表示进入上架审核队列；免审直上（auto 策略 + 市场管理员）时已是 public。
    toast(r.scope === 'public' ? '已创建并上架市场（管理员免审）' : '已创建并提交上架申请（待审核）');
  }catch(e){ toast('自动上架失败：' + (e.message||e)); }
}
// 私人待发布条目 → 编辑（复用技能/MCP/工具表单）
function marketEditPrivateItem(kind, id){
  if(kind==='skill') editSkill(id);
  else if(kind==='mcp') editMCP(id);
  else showToolForm(id);
}
// 删除市场条目（公共/私人均按 id 删除；内置已被过滤）
async function deleteMarketItem(kind, id, name){
  if(!(await confirmDialog(`确认删除插件「${name}」？该操作不可恢复。`))) return;
  try{
    const url = kind==='skill' ? `/api/studio/skills/${id}` : kind==='mcp' ? `/api/studio/mcp-servers/${id}` : `/api/studio/tools/${id}`;
    const r = await api(url, {method:'DELETE'});
    if(r.error){ toast(r.error); return; }
    toast('已删除'); loadMarketManage(); loadMarket(); loadMinePlugins();
  }catch(e){ toast('删除失败：' + (e.message||e)); }
}
// 新建插件下拉（表单创建 / 上传技能包 / 工具 / MCP）——fixed 定位防漂移（flex-wrap/滚动/transform 容器下仍贴按钮展示）
function toggleManageNewMenu(ev){
  toggleDropMenu('mm-new-menu', document.getElementById('mm-new-btn'), ev);
}
function hideManageNewMenu(){
  const m = document.getElementById('mm-new-menu');
  if(m) m.style.display = 'none';
}
// 上传技能包（控制台新建）：解析确认入库后自动上架（管理员创建）
function mmUploadSkill(){
  _mmCreate = 'skill';
  document.getElementById('skill-upload-input').click();
  hideManageNewMenu();
}
// 2026-09-17 定稿：插件市场页无二级分类 tab（mm-sale 单页直出），switchManageTab 已废除
let manageTab = 'mm-sale';
// 通用下拉：fixed 定位到按钮正下方（宽度/视口边界自适应），彻底避免 absolute 漂移
function toggleDropMenu(id, btnEl, ev){
  if(ev) ev.stopPropagation();
  const m = document.getElementById(id);
  if(!m || !btnEl) return;
  if(m.style.display !== 'none'){ m.style.display = 'none'; return; }
  // fixed 定位前先清掉 CSS .ddm 的 right:0/top 偏移，避免与 left 冲突导致宽度拉伸、定位漂移
  m.style.position = 'fixed';
  m.style.left = 'auto';
  m.style.right = 'auto';
  m.style.top = '0px';
  m.style.display = '';
  const r = btnEl.getBoundingClientRect();
  const w = m.offsetWidth || 190;
  const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
  m.style.left = left + 'px';
  m.style.top = (r.bottom + 4) + 'px';
}
document.addEventListener('click', function(e){
  const m = document.getElementById('mm-new-menu');
  if(!m || m.style.display==='none') return;
  const btn = document.getElementById('mm-new-btn');
  if((btn && btn.contains(e.target)) || m.contains(e.target)) return;
  m.style.display = 'none';
});
async function marketPin(kind, name){
  try{
    const r = await api(`/api/studio/market/${kind}/${encodeURIComponent(name)}/pin`, {method:'POST', body:'{}'});
    if(r.error) toast(r.error); else { toast(r.pinned?'已置顶':'已取消置顶'); loadMarketManage(); loadMarket(); }
  }catch(e){ toast('操作失败：' + (e.message||e)); }
}
// 市场下架 = 撤回上架（scope 语义 public → personal），**不改 status**：
//   作者与已安装者照常可用，只是他人不再可见、不可安装。
// 2026-09-17 入口归位：本动作属市场治理，仅在「插件市场」页提供（前三个类型 tab 已移除）。
// 实现走统一的 /api/plugins/{pid}/unshare（后端按 scope 分派 withdraw_share），
// 不再依赖 legacy 的 kind+name 定位 —— agent/prompt 不在 _VALID_MARKET_KIND 内，
// 旧端点 /api/studio/market/{kind}/{name}/unpublish 对它们必然 400。
async function marketUnpublish(pid, name){
  if(!pid){ toast('该条目缺少 plugin_id，无法下架'); return; }
  if(!(await confirmDialog(`确定下架「${name}」？\n\n· 撤回上架：他人不再可见、不可安装\n· 作者与已安装者照常可用\n· 条目回到个人空间，之后可重新申请上架`))) return;
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/unshare', {method:'POST', body:'{}'});
    if(r.error){ toast('下架失败：' + r.error); return; }
    toast('已下架（撤回上架，回到个人空间）');
    loadMarketManage(); loadMarket(); loadMinePlugins();
  }catch(e){ toast('操作失败：' + (e.message||e)); }
}
// 市场删除（软删除，审计保留）—— 后端 soft_delete 守卫：
//   作者可删自己的条目；已上架（public）条目的删除需市场管理员；
//   内置能力（legacy 种子/平台内置）不可删除。
async function marketDeletePlugin(pid, name){
  if(!pid){ toast('该条目缺少 plugin_id，无法删除'); return; }
  if(!(await confirmDialog(`确认删除「${name}」？\n\n· 移除该能力本体（软删除，审计保留）\n· 已安装者的副本同时失效\n· 不可恢复`))) return;
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid), {method:'DELETE'});
    if(r.error){ toast('删除失败：' + r.error); return; }
    toast('已删除');
    loadMarketManage(); loadMarket(); loadMinePlugins();
  }catch(e){ toast('删除失败：' + (e.message||e)); }
}
// 我的插件统一列表（skill/mcp/tool 聚合；编辑/移除/发布）

// ── 统一插件体系（P0）──
let _unifiedEdit = null;   // 正在编辑的插件（null=新建）
// 2026-09-16：补齐 tool/agent/prompt —— 此前只有三类，迁移进来的 50 条数据会显示为未知类型
const UNIFIED_TYPE_META = {
  skill:{icon:'🧩',label:'Skill'}, mcp:{icon:'🔌',label:'MCP'},
  tool:{icon:'🔧',label:'Tool'}, agent:{icon:'🤖',label:'Agent'},
  prompt:{icon:'📝',label:'Prompt'}, bundle:{icon:'📦',label:'Bundle'},
};
const UNIFIED_STATUS_ST = {
  draft:'st', private:'st b', submitted:'st amb', rejected:'st red',
  published:'st ok', disabled:'st', enabled:'st ok', deprecated:'st', removed:'st',
};
// 2026-09-17 定稿：消费视图由 36-capability.js 的 cap-zone 承载（每个类型页内「个人可用」单区），
// mm-unified 消费页与 loadUnifiedPlugins/loadUnifiedMine/renderUnifiedMine/renderUnifiedInstalled 已删除。
// 统一刷新入口：刷新当前类型的能力区 + 插件市场管理页（若在台上）
function refreshCapabilityViews(){
  try{ if(typeof capLoad === 'function' && typeof _capCur !== 'undefined') capLoad(_capCur); }catch(e){}
  if(document.getElementById('market-manage-sections')) loadMarketManage();
}
async function loadUnifiedPending(){
  const r = await api('/api/plugins/pending').catch(()=>({items:[]}));
  renderUnifiedPending(r.items || []);
  const cnt = document.getElementById('unified-pending-count');
  if(cnt) cnt.textContent = (r.items||[]).length;
  return r;
}
async function loadUnifiedSettings(){
  const r = await api('/api/plugins/settings').catch(()=>({}));
  const sel = document.getElementById('unified-review-policy');
  if(sel && r.review_policy) sel.value = r.review_policy;
  return r;
}
function renderUnifiedPending(items){
  const sum = document.getElementById('unified-pending-summary');
  const box = document.getElementById('unified-pending-table');
  if(!box) return;
  if(sum) sum.textContent = `${items.length} 个待审`;
  if(!items.length){ box.innerHTML = '<div style="padding:14px;color:var(--mut);font-size:12px;">暂无待审核插件</div>'; return; }
  box.innerHTML = `<table class="t"><tr><th>名称</th><th>类型</th><th>版本</th><th>作者</th><th>描述</th><th>操作</th></tr>` +
    items.map(it=>{
      const m = UNIFIED_TYPE_META[it.type] || {icon:'🧩', label:it.type};
      return `<tr>
        <td><b>${m.icon} ${esc(it.manifest?.label?.name || it.name)}</b></td>
        <td>${m.label}</td>
        <td>${esc(it.current_version||'-')}</td>
        <td>${esc(it.author_name||'-')}</td>
        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(it.description||'')}">${esc(it.description||'-')}</td>
        <td style="white-space:nowrap;">
          <button class="btn sm" onclick="unifiedReview('${esc(it.plugin_id)}','approve')">✅ 通过</button>
          <button class="btn sm ghost amb" onclick="unifiedReview('${esc(it.plugin_id)}','reject')">⛔ 驳回</button>
          <button class="btn sm ghost" title="分配可见范围" onclick="unifiedGrant('${esc(it.plugin_id)}','${esc(it.name)}')">🎯 范围</button>
        </td>
      </tr>`;
    }).join('') + `</table>`;
}
// ── 统一插件：新建/编辑 ──
async function openUnifiedPluginForm(pid){
  _unifiedEdit = pid || null;
  // 注意：showModal 是惰性插入表单 DOM，必须先渲染再访问表单元素
  showModal('unifiedPlugin');
  const t = document.getElementById('up-form-title');
  if(t) t.textContent = _unifiedEdit ? '✏️ 编辑统一插件' : '🧩 新建统一插件';
  document.getElementById('up-manifest').value = '';
  document.getElementById('up-skill-md').value = '';
  document.getElementById('up-mcp-server').value = '';
  document.getElementById('up-skill-row').style.display = '';
  document.getElementById('up-mcp-row').style.display = 'none';
  // 2026-09-16：显式「分类」「Agent 角色」字段（此前只能手改 JSON）
  const _catReset = document.getElementById('up-category');
  if(_catReset) _catReset.value = '';
  const _roleRowReset = document.getElementById('up-agent-role-row');
  if(_roleRowReset) _roleRowReset.style.display = 'none';
  _capFillCategoryList();
  if(pid){
    try{
      const d = await api('/api/plugins/' + encodeURIComponent(pid));
      if(d.error){ toast(d.error); return; }
      document.getElementById('up-manifest').value = JSON.stringify(d.manifest||{}, null, 2);
      // 载荷（SKILL.md / server.json 随详情返回，缺失时为空）
      const md = d.skill_md || '';
      document.getElementById('up-skill-md').value = md;
      document.getElementById('up-skill-row').style.display = (d.type==='skill') ? '' : 'none';
      const sv = d.mcp_server ? JSON.stringify(d.mcp_server, null, 2) : '';
      document.getElementById('up-mcp-server').value = sv;
      document.getElementById('up-mcp-row').style.display = (d.type==='mcp') ? '' : 'none';
      _capToggleRows(d.type);
      _capFillValues(d.manifest || {});
    }catch(e){ toast('加载插件失败：' + (e.message||e)); return; }
  }
  // 类型切换联动载荷行
  const m = document.getElementById('up-manifest');
  if(m) m.oninput = ()=>{
    try{
      const j = JSON.parse(m.value);
      _capToggleRows(j.type);
    }catch(e){}
  };
}
/* 2026-09-16：按类型联动表单行显隐（载荷行 + Agent 角色行） */
function _capToggleRows(type){
  const skill = document.getElementById('up-skill-row');
  const mcp = document.getElementById('up-mcp-row');
  const role = document.getElementById('up-agent-role-row');
  if(skill) skill.style.display = (type==='skill') ? '' : 'none';
  if(mcp) mcp.style.display = (type==='mcp') ? '' : 'none';
  if(role) role.style.display = (type==='agent') ? '' : 'none';
}
/* 从 manifest 回填显式字段（分类 / Agent 角色） */
function _capFillValues(j){
  const catEl = document.getElementById('up-category');
  if(catEl) catEl.value = (j && j.label && j.label.category) || '';
  const roleEl = document.getElementById('up-agent-role');
  if(roleEl) roleEl.value = (j && j.agent_role) || 'sub';
}
/* 分类候选（datalist）：优先复用能力中心已加载数据，冷启动时拉一次全量。
 * 分类本身是数据驱动的 —— 这里只做"输入建议"，填新分类同样有效。 */
async function _capFillCategoryList(){
  const dl = document.getElementById('up-category-list');
  if(!dl) return;
  let cats = [];
  try{
    Object.keys(_capSt).forEach(function(p){
      ['market','mine'].forEach(function(k){
        (_capSt[p][k]||[]).forEach(function(x){
          if(x.category && cats.indexOf(x.category) < 0) cats.push(x.category);
        });
      });
    });
    if(!cats.length){
      const r = await api('/api/plugins');
      (r.items||[]).forEach(function(x){
        if(x.category && cats.indexOf(x.category) < 0) cats.push(x.category);
      });
    }
  }catch(e){}
  dl.innerHTML = cats.map(function(c){
    return '<option value="' + String(c).replace(/"/g,'') + '"></option>';
  }).join('');
}
async function saveUnifiedPlugin(){
  let manifest;
  try{ manifest = JSON.parse(document.getElementById('up-manifest').value); }
  catch(e){ toast('manifest JSON 解析失败'); return; }
  // 2026-09-16：把表单显式字段合并进 manifest
  const _catEl = document.getElementById('up-category');
  const _cat = _catEl ? String(_catEl.value || '').trim() : '';
  if(_cat){
    manifest.label = manifest.label || {};
    manifest.label.category = _cat;
  }
  const _roleEl = document.getElementById('up-agent-role');
  if(_roleEl && manifest.type === 'agent' && _roleEl.value){
    manifest.agent_role = _roleEl.value;
  }
  const body = { manifest };
  const md = document.getElementById('up-skill-md').value.trim();
  const sv = document.getElementById('up-mcp-server').value.trim();
  if(md) body.skill_md = md;
  if(sv){ try{ body.mcp_server = JSON.parse(sv); }catch(e){ toast('server.json JSON 解析失败'); return; } }
  try{
    const r = _unifiedEdit
      ? await api('/api/plugins/' + encodeURIComponent(_unifiedEdit), {method:'PUT', body:JSON.stringify(body)})
      : await api('/api/plugins', {method:'POST', body:JSON.stringify(body)});
    if(r.error){ toast('保存失败：' + r.error); return; }
    toast(_unifiedEdit ? '已更新' : '已创建（草稿）');
    closeModal(); refreshCapabilityViews();
  }catch(e){ toast('保存失败：' + (e.message||e)); }
}
async function unifiedReview(pid, action){
  const comment = action==='reject' ? await promptDialog({title:'驳回原因', placeholder:'说明驳回原因（写入审核记录）', okText:'确认驳回'}) : '';
  if(action==='reject' && comment===null) return;
  try{ const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/review', {method:'POST', body:JSON.stringify({action, comment: comment||''})});
    if(r.error) toast(r.error); else { toast(action==='approve' ? '已通过并上架' : '已驳回'); refreshCapabilityViews(); } }
  catch(e){ toast('操作失败：' + (e.message||e)); }
}
async function unifiedGrant(pid, name){
  const v = await promptDialog({title:'🎯 分配可见范围（' + name + '）', message:'target_type: all（全员）/ team（部门，填部门名）/ role（角色，填角色名）\n示例：{"target_type":"role","target_id":"系统工程师"}', placeholder:'{"target_type":"all","target_id":"","permission":"use"}', okText:'分配', multiline:true, rows:4});
  if(v===null || !v.trim()) return;
  let body; try{ body = JSON.parse(v); }catch(e){ toast('JSON 格式错误'); return; }
  try{ const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/grant', {method:'PUT', body:JSON.stringify(body)});
    if(r.error) toast(r.error); else { toast('已分配范围'); refreshCapabilityViews(); } }
  catch(e){ toast('操作失败：' + (e.message||e)); }
}
async function unifiedToggleInstall(pid, install){
  try{ const r = await api('/api/plugins/' + encodeURIComponent(pid) + (install?'/install':'/uninstall'), {method:'POST', body:'{}'});
    if(r.error) toast(r.error); else { toast(install?'已安装':'已卸载'); refreshCapabilityViews(); } }
  catch(e){ toast('操作失败：' + (e.message||e)); }
}
async function saveUnifiedPolicy(policy){
  try{ const r = await api('/api/plugins/settings', {method:'PUT', body:JSON.stringify({review_policy: policy})});
    if(r.error){ toast(r.error); const sel = document.getElementById('unified-review-policy'); if(sel) sel.value = (sel.value==='forced'?'auto':'forced'); return; }
    toast(policy==='forced' ? '已切换：共享需强制审核' : '已切换：共享免审直发'); }
  catch(e){ toast('设置失败：' + (e.message||e)); }
}
// ── 统一插件：沙箱执行 / 调用日志 ──
async function unifiedRun(pid){
  const params = await promptDialog({title:'▶️ 沙箱执行测试', message:'tool（skill 指令名，可空）+ params（JSON）\n示例：{"tool":"","params":{"text":"hello"}}', placeholder:'{"tool":"", "params":{}}', okText:'执行', multiline:true, rows:3});
  if(params===null || !params.trim()) return;
  let body; try{ body = JSON.parse(params); }catch(e){ toast('JSON 格式错误'); return; }
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/run', {method:'POST', body:JSON.stringify(body)});
    if(r.error){ toast(r.error); return; }
    const res = r.result || {};
    let out = res.output || res.stdout || res.result || '';
    if(typeof out === 'object') out = JSON.stringify(out, null, 2);
    await promptDialog({title:'▶️ 执行结果（' + (r.latency_ms||'') + 'ms · ' + (res.status||res.ok||'') + '）', value: String(out||'（空输出）'), okText:'关闭', multiline:true, rows:12});
  }catch(e){ toast('执行失败：' + (e.message||e)); }
}
async function unifiedCalls(pid){
  try{
    const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/calls');
    const items = r.items || [];
    if(!items.length){ toast('暂无调用日志'); return; }
    const txt = items.slice(0, 30).map(c=>`[${c.created_at||''}] ${c.tool_name||''} · ${c.status||''} · ${c.latency_ms||0}ms\n  params: ${c.params_snapshot||''}`).join('\n\n');
    await promptDialog({title:'📈 调用日志（最近 ' + Math.min(items.length,30) + ' 条）', value: txt, okText:'关闭', multiline:true, rows:14});
  }catch(e){ toast('加载失败：' + (e.message||e)); }
}
// 我的插件 → 按管理入口拆分：来自个人 / 来自市场副本（工具已移出插件市场）
async function loadMinePlugins(){
  await Promise.all([loadMySkills(), loadMyMCPs()]);
}
// 私人空间条目 → 发布到公共市场（需 ai_studio:publish 创作者权限；上架后由市场管理员治理）
async function marketPublishFrom(kind, name){
  if(!(await canPublishToMarket())){ toast('无发布权限（需要 ai_studio:publish 角色权限）'); return; }
  if(!(await confirmDialog(`确定发布「${name}」到公共市场？\n发布后该条目进入市场供其他用户安装；维护请到「插件管理 → 市场管理」。`))) return;
  try{
    const r = await api(`/api/studio/market/${kind}/${encodeURIComponent(name)}/publish`, {method:'POST', body:'{}'});
    if(r.error) toast(r.error); else { toast('已发布到公共市场'); loadMinePlugins(); loadMarket(); loadMarketManage(); }
  }catch(e){ toast('发布失败：' + (e.message||e)); }
}

// ── 流程编排（可视化拖拽画布 + DAG 真实执行，参考 n8n/Dify 标准交互）──
let flowEditId = null;

// 节点类型定义（托盘 + 属性面板字段）
const FLOW_NODE_TYPES = {
  llm:   {label:'LLM 节点',    icon:'🧠', color:'#378ADD', fields:[
    {k:'prompt',        label:'Prompt（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'system_prompt', label:'System Prompt（可空）', type:'textarea'},
    {k:'model',         label:'模型名（可空，默认 Provider 模型）', type:'text', source:'models'},
    {k:'provider_id',   label:'Provider（可空，默认全局）', type:'text', source:'providers'},
    {k:'write_keys',    label:'写入黑板 write_keys（逗号分隔，如 requirements, summary）', type:'text'},
    {k:'subscribe',     label:'订阅黑板 subscribe（逗号分隔，如 requirements）', type:'text'},
  ]},
  tool:  {label:'工具节点',    icon:'🔧', color:'#639922', fields:[
    {k:'tool',      label:'工具', type:'select', options:['graph_retrieve','conflict_check','impact_analyze','validate','entity_create','file_list','file_read','file_write','file_append','file_mkdir','file_delete','report_export']},
    {k:'arguments', label:'参数 JSON（值可 {{节点id.字段}} 引用）', type:'json'},
    {k:'branch',    label:'知识分支 branch（可空，默认 dev/main）', type:'text'},
    {k:'hil_level', label:'HIL 分级（L2 时写操作需人工确认）', type:'select', options:['L0','L2']},
  ]},
  agent: {label:'Agent 节点',  icon:'🤖', color:'#7F77DD', fields:[
    {k:'agent', label:'子 Agent（intent 名）', type:'text', source:'agents'},
    {k:'query', label:'Query（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'provider_id', label:'Provider（可空，默认 Agent 自身配置）', type:'text', source:'providers'},
    {k:'memorize', label:'执行后沉淀长期记忆', type:'select', options:['true','false']},
    {k:'write_keys', label:'写入黑板 write_keys（逗号分隔）', type:'text'},
    {k:'subscribe', label:'订阅黑板 subscribe（逗号分隔）', type:'text'},
  ]},
  skill: {label:'Skill 节点',  icon:'🧩', color:'#BA7517', fields:[
    {k:'skill', label:'Skill 名称（published）', type:'text', source:'skills'},
  ]},
  orchestrator: {label:'Manager 节点', icon:'⚖', color:'#534AB7', fields:[
    {k:'workers', label:'Worker Agents（逗号分隔 intent 名）', type:'text', source:'agents', multi:true},
    {k:'task', label:'团队任务（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'strategy', label:'协作策略（contract_net 招标属高级模式，MBSE 主链路推荐 sequential/parallel）', type:'select', options:['sequential','parallel','contract_net']},
  ]},
  react: {label:'ReAct 节点', icon:'🧬', color:'#0E8F6E', fields:[
    {k:'goal', label:'目标（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'max_steps', label:'最大循环步数（默认 5）', type:'text'},
    {k:'tools', label:'可用工具（逗号分隔，默认全部）', type:'text'},
    {k:'provider_id', label:'Provider（可空，默认全局）', type:'text', source:'providers'},
    {k:'write_keys', label:'写入黑板 write_keys（逗号分隔）', type:'text'},
  ]},
  planner: {label:'Planner 节点', icon:'🗂', color:'#B2347E', fields:[
    {k:'goal', label:'团队目标（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'agents', label:'可用 Agent 池（逗号分隔，空则计划器自主分配）', type:'text', source:'agents', multi:true},
    {k:'max_tasks', label:'计划任务上限（默认 6）', type:'text'},
    {k:'parallel', label:'就绪任务并行执行', type:'select', options:['false','true']},
    {k:'provider_id', label:'Provider（可空，默认全局）', type:'text', source:'providers'},
    {k:'write_keys', label:'写入黑板 write_keys（逗号分隔）', type:'text'},
  ]},
  reflection: {label:'反思节点', icon:'🔍', color:'#D4537E', fields:[
    {k:'target', label:'被评审节点 id（如 n1）', type:'text'},
    {k:'criteria', label:'评估标准（如：输出是否完整合理）', type:'textarea'},
  ]},
  mcp:   {label:'MCP 节点',    icon:'🔌', color:'#D4537E', fields:[
    {k:'endpoint', label:'MCP 端点 URL', type:'text', source:'mcp_servers'},
    {k:'tool',     label:'工具名', type:'text', source:'mcp_tools'},
    {k:'arguments',label:'参数 JSON', type:'json'},
    {k:'transport',label:'传输方式', type:'select', options:['sse','streamable_http','http']},
  ]},
  if:    {label:'条件节点',    icon:'🔀', color:'#E24B4A', fields:[
    {k:'expression', label:'条件表达式（如 {{n1.data.result}} contains 冲突 / {{n2.data.value}} == true）', type:'text'},
    {k:'max_iterations', label:'循环上限 max_iterations（配合 🔄 循环连线，防死循环）', type:'text'},
  ]},
  code:  {label:'代码节点',    icon:'👨‍💻', color:'#0E8F6E', fields:[
    {k:'language', label:'语言（默认 python；js 需服务端已装 Node）', type:'select', options:['python','js']},
    {k:'code',     label:'代码（_in 读输入，_out 写输出，JSON 序列化进 data.outputs；受限沙箱：仅纯计算内建，禁止文件/网络/反射）', type:'textarea'},
    {k:'inputs',   label:'输入 inputs JSON（值可 {{节点id.字段}} / {{blackboard.键}} 引用）', type:'json'},
    {k:'timeout',  label:'超时（秒，默认 10，防死循环）', type:'text'},
  ]},
  http:  {label:'HTTP 节点',   icon:'🌐', color:'#378ADD', fields:[
    {k:'method',   label:'方法', type:'select', options:['GET','POST','PUT','PATCH','DELETE']},
    {k:'url',      label:'URL（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'text'},
    {k:'headers',  label:'请求头 JSON', type:'json'},
    {k:'body',     label:'请求体 JSON', type:'json'},
    {k:'query',    label:'查询参数 JSON', type:'json'},
    {k:'timeout',  label:'超时（秒，默认 15）', type:'text'},
  ]},
  iteration: {label:'迭代节点', icon:'🔁', color:'#639922', fields:[
    {k:'items',    label:'迭代项（JSON 数组 或 {{节点.字段}} 引用 或 逗号分隔字符串）', type:'json'},
    {k:'subflow',  label:'子流程节点 id（逗号分隔，每轮按序执行，可引用 {{blackboard.__item}}）', type:'text'},
  ]},
  knowledge: {label:'知识节点', icon:'📚', color:'#BA7517', fields:[
    {k:'query',    label:'检索 Query（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'top_k',    label:'返回条数 top_k（默认 5）', type:'text'},
    {k:'branch',   label:'知识分支 branch（默认 dev）', type:'text'},
  ]},
  pubsub: {label:'消息节点', icon:'📡', color:'#0E8F6E', fields:[
    {k:'op',       label:'操作', type:'select', options:['publish','subscribe']},
    {k:'topic',    label:'主题 topic（发布/订阅同一主题互通；支持 {{节点id.字段}} / {{blackboard.键}}）', type:'text'},
    {k:'payload',  label:'发布内容 payload（订阅方认领后注入；文本或 JSON，支持 {{节点id.字段}} 引用）', type:'textarea'},
    {k:'write_key',label:'订阅写入黑板键 write_key（默认取 topic；下游 {{blackboard.键}} / llm subscribe 可用）', type:'text'},
  ]},
  debate: {label:'协商节点', icon:'⚖️', color:'#7B5CBF', fields:[
    {k:'mode',     label:'协商模式', type:'select', options:['vote','price']},
    {k:'proposal', label:'议题 proposal（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'textarea'},
    {k:'sources',  label:'观点来源 sources（节点 id 逗号分隔；留空取所有已执行节点）', type:'text'},
    {k:'options',  label:'投票选项 options（逗号分隔；内容子串命中计票，未命中记弃权）', type:'text'},
    {k:'threshold',label:'共识阈值 threshold（默认 0.6；最高得票比例达到即达成共识）', type:'text'},
  ]},
  webhook: {label:'Webhook 节点', icon:'🔔', color:'#D4537E', fields:[
    {k:'url',      label:'回调 URL（支持 {{节点id.字段}} / {{blackboard.键}}）', type:'text'},
    {k:'method',   label:'方法', type:'select', options:['POST','PUT','GET']},
    {k:'payload',  label:'发送内容 payload（文本或 JSON；自动包装为 A2A message 发送）', type:'textarea'},
    {k:'secret',   label:'签名密钥 secret（HMAC-SHA256，放 X-A2A-Signature 头；空=不签名）', type:'text'},
    {k:'headers',  label:'附加请求头 JSON', type:'json'},
    {k:'timeout',  label:'超时（秒，默认 10）', type:'text'},
  ]},
};

let flowCanvas = {nodes:[], edges:[], sel:null, selEdge:null, nextId:1, link:null};
