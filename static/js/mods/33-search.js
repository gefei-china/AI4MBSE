/* 全局搜索
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 21334-21407  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function openGlobalSearch(){
  __gkOpen = true;
  let box = document.getElementById('gk-search');
  if(!box){
    box = document.createElement('div');
    box.id = 'gk-search';
    box.style.cssText = 'position:fixed;inset:0;background:rgba(15,32,52,.35);z-index:2000;display:flex;justify-content:center;align-items:flex-start;padding-top:12vh;';
    box.innerHTML = `<div style="width:560px;max-width:92vw;background:#fff;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.25);overflow:hidden;">
      <div style="padding:12px 14px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;">
        <span style="color:var(--blue-d);font-size:14px;">⌕</span>
        <input id="gk-input" placeholder="搜索会话 / 本体类型 / 实体（回车跳转首项，Esc 关闭）" style="flex:1;border:none;outline:none;font-size:13px;">
        <span class="tag">Ctrl+K</span>
      </div>
      <div id="gk-results" style="max-height:50vh;overflow-y:auto;padding:6px;"></div>
    </div>`;
    document.body.appendChild(box);
    box.addEventListener('click', e=>{ if(e.target===box) closeGlobalSearch(); });
    document.getElementById('gk-input').addEventListener('input', ()=>{ clearTimeout(__gkTimer); __gkTimer = setTimeout(gkSearch, 250); });
    document.getElementById('gk-input').addEventListener('keydown', e=>{ if(e.key==='Enter') gkJumpFirst(); });
  }
  box.style.display = 'flex';
  const inp = document.getElementById('gk-input');
  inp.value = ''; inp.focus();
  document.getElementById('gk-results').innerHTML = '<div style="color:var(--mut);font-size:12px;padding:14px;text-align:center;">输入关键词搜索…</div>';
}
function closeGlobalSearch(){ __gkOpen = false; const b = document.getElementById('gk-search'); if(b) b.style.display = 'none'; }
async function gkSearch(){
  const q = document.getElementById('gk-input').value.trim();
  const res = document.getElementById('gk-results');
  if(!q){ res.innerHTML = '<div style="color:var(--mut);font-size:12px;padding:14px;text-align:center;">输入关键词搜索…</div>'; return; }
  res.innerHTML = '<div style="color:var(--mut);font-size:12px;padding:14px;text-align:center;">搜索中…</div>';
  __gkData = [];
  try{
    const [convs, ont, ents] = await Promise.all([
      api('/api/conversations').catch(()=>[]),
      api('/api/knowledge/ontology').catch(()=>[]),
      api('/api/knowledge/entities?search=' + encodeURIComponent(q)).catch(()=>[])
    ]);
    const ql = q.toLowerCase();
    const c = (convs||[]).filter(x=>((x.title||'')+' '+(x.intent||'')).toLowerCase().includes(ql)).slice(0,5)
      .map(x=>({kind:'conv', id:x.id, name:x.title, sub:'会话 · '+(x.msg_count||0)+' 条消息'}));
    const t = (ont||[]).filter(x=>((x.name||'')).toLowerCase().includes(ql)).slice(0,5)
      .map(x=>({kind:'type', id:x.id, name:x.name, sub:'本体类型 · '+(x.type_kind==='relation'?'关系':x.type_kind==='attribute'?'属性':'类')}));
    const e = (ents||[]).filter(x=>((x.name||'')).toLowerCase().includes(ql)).slice(0,5)
      .map(x=>({kind:'ent', id:x.id, name:x.name, sub:'实例 · '+x.entity_type+'（'+(x.branch||'')+'）'}));
    __gkData = [...c, ...t, ...e];
    if(!__gkData.length){ res.innerHTML = '<div style="color:var(--mut);font-size:12px;padding:14px;text-align:center;">无结果</div>'; return; }
    res.innerHTML = __gkData.map((d,i)=>`<div class="gk-item" data-i="${i}" style="padding:8px 14px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;border-bottom:1px dashed var(--line);" onclick="gkJump(${i})">
      <b style="font-size:12.5px;">${d.kind==='conv'?'💬':d.kind==='type'?'🧬':'🕸'} ${esc(d.name)}</b>
      <small style="color:var(--mut);font-size:11px;flex:none;">${esc(d.sub)}</small></div>`).join('');
  }catch(e){ res.innerHTML = '<div style="color:var(--red);font-size:12px;padding:14px;">搜索失败：'+esc(e.message)+'</div>'; }
}
function gkJump(i){
  const d = __gkData[i]; if(!d) return;
  closeGlobalSearch();
  if(d.kind==='conv'){ go('ai'); setTimeout(()=>selectConv(d.id), 350); }
  else if(d.kind==='type'){ go('kb','kb-c'); setTimeout(()=>{ if(window.selectOntType) selectOntType(d.name); }, 500); }
  else { go('kb','kb-d'); setTimeout(()=>{ if(window.viewEntity) viewEntity(d.id); }, 350); }
}
function gkJumpFirst(){ if(__gkData.length) gkJump(0); }

// ── 初始化 ──
initNav();   // 2026-09-04 v3：折叠态恢复 + data-name 初始化 + hover tooltip / 更多卡片（此前漏调用导致折叠无名称提示）
// 2026-09-04 v6：默认进入「AI 建模」页（HTML 上 pg-ai 已带 on），不调用 loadHome 以免拉无用的 dashboard。
// go('ai') 内部 loadPage('ai') 已加载会话列表并渲染欢迎屏（无会话/未选会话时），此处不再重复调用 loadConversations。
go('ai');
// 会话列表描述性注释（旧逻辑已由上方全局加载取代）
loadCurrentUser();
loadCurrentProject();
initGlobalBranch();
applyMarketPerm();  // 市场管理侧边栏入口按 ai_studio:publish 权限显隐
// 说明：旧 art-side 的"恢复收起状态与分栏宽度"加载逻辑已随 V3 重构移除（#art-side 元素已不存在），
// 预览面板默认收起状态由上方 init IIFE 统一控制，不再恢复任何历史展开/宽度状态。
