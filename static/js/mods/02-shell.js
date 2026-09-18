/* 外壳：滑窗 / 面板 / 导航 / 路由 / 页面加载
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 572-1039  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function ontSlideTitle(mode, kind) {
  const k = kind === 'relation' ? '关系类型' : (kind === 'attribute' ? '属性类型' : '实体类型');
  const act = (mode === 'edit') ? '编辑' : '新增';
  const icon = kind === 'relation' ? '🔗' : (kind === 'attribute' ? '🏷' : '🧬');
  return `${icon} ${act}${k}`;
}
// ── 可搜索下拉多选组件（msel，2026-09-07）──
// 增强原生 select[multiple]：隐藏原 select，渲染 触发器(chips)+搜索面板；选中状态仍写回
// select.options[].selected，所有既有读写逻辑（getSelectValues/selectedOptions）零改动。
// populate 函数重建 options 后由 MutationObserver 自动重绘，无需改调用方。
function enhanceMultiSelect(sel){
  if(!sel) return;
  if(sel._ms){ sel._ms.sync(); return; }
  const wrap = document.createElement('div');
  wrap.className = 'msel';
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.style.display = 'none';
  const trigger = document.createElement('div'); trigger.className = 'msel-trigger';
  const panel = document.createElement('div'); panel.className = 'msel-panel';
  panel.innerHTML = '<input class="msel-search" placeholder="🔍 搜索…"><div class="msel-list"></div>';
  wrap.appendChild(trigger); wrap.appendChild(panel);
  const search = panel.querySelector('.msel-search');
  const list = panel.querySelector('.msel-list');
  const opts = ()=>Array.from(sel.options);
  function sync(){
    const all = opts();
    const chosen = all.filter(o=>o.selected);
    trigger.innerHTML = !all.length
      ? '<span class="msel-ph">暂无候选</span>'
      : (chosen.length ? chosen.map(o=>`<span class="msel-chip" title="${escA(o.value)}"><span>${esc(o.value)}</span><b data-v="${escA(o.value)}">✕</b></span>`).join('')
                       : '<span class="msel-ph">点击选择（支持搜索）</span>');
    const q = (search.value||'').trim().toLowerCase();
    const vis = all.filter(o=>!q || o.value.toLowerCase().includes(q));
    list.innerHTML = vis.length
      ? vis.map(o=>`<div class="msel-opt ${o.selected?'on':''}" data-v="${escA(o.value)}"><span class="box">✓</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escA(o.value)}">${esc(o.value)}</span></div>`).join('')
      : '<div class="msel-empty">无匹配项</div>';
  }
  function open(){ closeAllMsel(wrap); wrap.classList.add('open'); search.value=''; sync(); search.focus(); }
  function close(){ wrap.classList.remove('open'); }
  trigger.addEventListener('click', e=>{
    const x = e.target.closest('.msel-chip b');
    if(x){ const o = opts().find(v=>v.value===x.dataset.v); if(o){ o.selected=false; sync(); } e.stopPropagation(); return; }
    wrap.classList.contains('open') ? close() : open();
    e.stopPropagation();
  });
  panel.addEventListener('click', e=>{
    const opt = e.target.closest('.msel-opt');
    if(opt){ const o = opts().find(v=>v.value===opt.dataset.v); if(o){ o.selected=!o.selected; sync(); } e.stopPropagation(); return; }
    e.stopPropagation();
  });
  search.addEventListener('input', sync);
  search.addEventListener('click', e=>e.stopPropagation());
  sel._ms = {sync, close};
  // childList：populate 重建 options；attributes[selected]：编辑回填直接设 o.selected（反映为 content attribute）
  new MutationObserver(()=>sync()).observe(sel, {childList:true, subtree:true, attributes:true, attributeFilter:['selected']});
  sync();
}
function closeAllMsel(except){
  document.querySelectorAll('.msel.open').forEach(m=>{ if(m !== except) m.classList.remove('open'); });
}
document.addEventListener('click', ()=>closeAllMsel());
// 本体表单内全部 multiple select 一键增强（原类型/目标类型/互斥/适用类型/必填/唯一）
function initMselAll(){
  ['ot-domain','ot-range','ot-disjoint','ot-attr-domain','ot-req','ot-uniq']
    .forEach(id=>enhanceMultiSelect(document.getElementById(id)));
}
function openOntSlide(mode, kind) {
  const panel = document.getElementById('ont-slide');
  const body = document.getElementById('ont-slide-body');
  body.innerHTML = MODAL_FORMS.onttype;
  // 2026-09-13：编辑区域上下滚动（根治：sp-body 为 Flex 子项需 min-height:0 才在内部滚动，见 app.css）
  body.style.overflowY = 'auto';
  initMselAll();   // 表单 HTML 重建后增强多选组件
  document.getElementById('ont-slide-title').textContent = ontSlideTitle(mode, kind || 'entity');
  const ft = document.getElementById('ont-form-title');
  if(ft) ft.textContent = ontSlideTitle(mode, kind || 'entity');
  panel.classList.add('open');
  document.getElementById('ont-overlay').style.display = 'block';
}
function closeOntSlide() {
  document.getElementById('ont-slide').classList.remove('open');
  document.getElementById('ont-overlay').style.display = 'none';
  ontTypeEditId = null;
}
function ontKindChanged() {
  const kind = document.getElementById('ot-kind').value;
  toggleOntKindFields(kind);
  const ft = document.getElementById('ont-form-title');
  if(ft) ft.textContent = ontSlideTitle(window._ontTypeMode, kind);
}
// ── 右侧弹窗统一宽度（2026-09-07）：所有滑窗/mbox 共用 CSS 变量 --sp-w，左缘把手可拖拽 ──
const _SP_KEY = 'mbse_sp_width', _SP_MIN = 420, _SP_DEFAULT = 560, _SP_MAX = 720;  // 2026-09-09 反馈：默认收窄至 560，拖拽上限 720（避免信息型面板过宽）
(function(){
  try{
    const w = parseInt(localStorage.getItem(_SP_KEY), 10);
    if(w && w >= _SP_MIN && w <= _SP_MAX) document.documentElement.style.setProperty('--sp-w', w + 'px');
  }catch(e){}
})();
let _spResize = null;
function spResizeStart(e){
  e.preventDefault(); e.stopPropagation();
  const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sp-w'), 10) || _SP_DEFAULT;
  _spResize = {x: e.clientX, w: cur};
  document.body.classList.add('sp-resizing');
  document.addEventListener('mousemove', spResizeMove);
  document.addEventListener('mouseup', spResizeEnd);
}
function spResizeMove(e){
  if(!_spResize) return;
  const max = Math.min(_SP_MAX, Math.max(_SP_MIN, window.innerWidth - 120));
  const w = Math.min(max, Math.max(_SP_MIN, _spResize.w - (e.clientX - _spResize.x)));  // 向左拖 = 变宽
  document.documentElement.style.setProperty('--sp-w', w + 'px');
}
function spResizeEnd(){
  _spResize = null;
  document.body.classList.remove('sp-resizing');
  document.removeEventListener('mousemove', spResizeMove);
  document.removeEventListener('mouseup', spResizeEnd);
  try{ localStorage.setItem(_SP_KEY, String(parseInt(
        getComputedStyle(document.documentElement).getPropertyValue('--sp-w'), 10))); }catch(e){}
}
function spResizeReset(e){
  e.stopPropagation();
  document.documentElement.style.setProperty('--sp-w', _SP_DEFAULT + 'px');
  try{ localStorage.setItem(_SP_KEY, String(_SP_DEFAULT)); }catch(e){}
  toast(`↔ 面板宽度已复位（${_SP_DEFAULT}px）`);
}

function openPanel(title, html) {
  document.getElementById('panel-title').textContent = title;
  document.getElementById('panel-body').innerHTML = html;
  document.getElementById('overlay').classList.add('show');
  document.getElementById('panel-detail').classList.add('open');
}
function closePanel() {
  document.getElementById('overlay').classList.remove('show');
  document.getElementById('overlay').style.display = '';   // 防御：清理 inline 残留（曾有 style 打开导致蒙层卡死）
  document.getElementById('panel-detail').classList.remove('open');
}

// ── 流程信息与运行结果 右侧弹窗（📋 流程信息 / 📈 运行历史 双 Tab，记忆上次 Tab，✕ / 遮罩可主动收起）──
let _flowRunTab = 'hist';   // 记忆上次 Tab（info | hist）
let _skipHistoryFill = false;   // 运行/异步弹窗场景：跳过历史列表加载（历史走流程卡「📈」入口）
function flowRunTab(tab, loadList = true){
  _flowRunTab = tab;
  const info = document.getElementById('flow-run-info');
  const hist = document.getElementById('flow-run-hist');
  const bi = document.getElementById('flow-tab-info');
  const bh = document.getElementById('flow-tab-hist');
  const ti = document.getElementById('flow-run-title');
  if(info) info.style.display = tab==='info' ? 'block' : 'none';
  if(hist) hist.style.display = tab==='hist' ? 'flex' : 'none';
  if(bi) bi.className = tab==='info' ? 'btn sm' : 'btn sm ghost';
  if(bh) bh.className = tab==='hist' ? 'btn sm' : 'btn sm ghost';
  if(ti) ti.textContent = tab==='info' ? '📋 流程信息' : '📈 运行历史';
  if(tab==='hist'){
    const fr = document.getElementById('flow-runs');
    if(loadList){ _skipHistoryFill = false; loadFlowRuns(); }   // 正常入口（流程卡 📈 / 历史详情）加载历史列表
    else { _skipHistoryFill = true; if(fr) fr.innerHTML = '<div style="color:var(--mut);font-size:11px;padding:6px;">运行历史请通过流程卡「📈」查看，本次弹窗仅展示当前运行结果</div>'; }
  }
}
function openFlowRunPanel(tab, loadList) {
  document.getElementById('flow-run-overlay').classList.add('show');
  document.getElementById('flow-run-panel').classList.add('open');
  flowRunTab(tab || _flowRunTab, loadList);
}
function closeFlowRunPanel() {
  document.getElementById('flow-run-overlay').classList.remove('show');
  document.getElementById('flow-run-panel').classList.remove('open');
}
// 历史详情 → 返回列表（清空详情 + 刷新列表）
function flowRunBackToList(){
  const el = document.getElementById('flow-result');
  if(el) el.innerHTML = '';
  loadFlowRuns();
}

// ── 导航 ──
// 2026-09-17 D1：kb 的一级名统一为「知识中心」（与主导航项 nav-kbhub 对齐；原先写作「模型本体」，
// 与导航标签口径不一致，会让「最近访问」标签显示成 模型本体）。
// 2026-09-17 R7：移除死条目 branch('合并队列' 页已删，go('branch') 重定向到 kb-d) 与 modelcfg('模型配置' 已并入 studio/st-model)。
const TITLES = {home:'总览',ai:'AI 建模',kb:'知识中心',reports:'报告',studio:'能力中心',settings:'设置',users:'用户与权限管理',audit:'审计日志',ops:'运行监控中心',approval:'审批管理'};
// 二级页提升一级导航：子 Tab 入口的页面标题映射
const KB_TAB_TITLES = {'kb-a':'知识浏览与搜索','kb-e':'资料库','kb-b':'数据整理','kb-c':'本体模型','kb-d':'图谱工作区',};
// 2026-09-17 R7：补 'st-prompt'（提示词模板，真实 subpage，`09-impact.js:1134` / `36-capability.js:72` 会 go 过来），
// 缺它会让面包屑退化成「能力中心」、且 loadPage 走不到子 Tab 直达分支
const ST_TAB_TITLES = {'st-agent':'Agent','st-skill':'技能','st-mcp':'工具与 MCP','st-market':'插件市场','st-model':'模型配置','st-prompt':'提示词模板'};
const STG_TAB_TITLES = {'st-projmem':'项目记忆','st-hooks':'工具钩子','st-fextract':'文件抽取'};
// 2026-09-17 D2：面包屑取名唯一入口。知识中心域（kb-d/kb-e）只显示一级名「知识中心」，
// 不再下钻到 资料库 / 图谱工作区 —— 明确「主导航=域，面包屑=域」的口径（其余页面仍取子 Tab 名）。
function pageCrumb(p, tabId){
  if(p === 'kb'){
    if(tabId === 'kb-d' || tabId === 'kb-e') return TITLES.kb;
    return (tabId && KB_TAB_TITLES[tabId]) || KB_TAB_TITLES['kb-e'] || TITLES.kb;
  }
  if(tabId){
    if(p === 'studio' && ST_TAB_TITLES[tabId]) return ST_TAB_TITLES[tabId];
    if(p === 'settings' && STG_TAB_TITLES[tabId]) return STG_TAB_TITLES[tabId];
  }
  return TITLES[p] || '';
}
// 2026-09-17 P2-4：导航高亮兜底 —— 当目标子页没有一一对应的导航项时，点亮其所属「域」的导航项，避免导航失焦。
//   kb-a / kb-b（入口已隐藏，仅剩深链）→ 知识中心
//   能力中心的非 Agent 子 Tab（技能/工具与MCP/插件市场/模型配置/提示词模板）→ 能力中心
function navFallbackOn(p){
  const el = (p === 'kb') ? document.getElementById('nav-kbhub')
           : (p === 'studio') ? document.querySelector('#mainnav a[data-page="studio"]')
           : null;
  if(el) el.classList.add('on');
}
function toggleNav() {
  // 2026-09-04 v4：☰ 折叠条作用于「会话列表区」（gnav-task 任务分组），不再折叠上方菜单区
  toggleTaskGroup();
}
function initNav() {
  // 2026-09-04 v4：☰ 现专用于「会话列表区」，整条导航不再由按键折叠 —— 清除旧版整条折叠键，避免启动时导航卡在收起态
  try{ localStorage.removeItem('mbse_nav_collapsed'); }catch(e){}
  // 2026-09-04 v3：折叠态 hover 提示——用 fixed #gnav-tip，避免被 gnav/nav overflow 裁剪
  document.querySelectorAll('#mainnav a').forEach(a=>{
    const ic = a.querySelector('.ic');
    const cc = a.querySelector('.more-caret');
    const lbl = a.querySelector('.lbl');
    let txt = lbl ? lbl.textContent.trim() : (a.textContent || '').trim();
    if(ic) txt = txt.replace(ic.textContent || '', '');
    if(cc) txt = txt.replace(cc.textContent || '', '');
    txt = txt.trim();
    if(txt) a.setAttribute('data-name', txt);
    if(a.id === 'more-link'){
      a.addEventListener('mouseenter', openMoreFlyout);
      a.addEventListener('mouseleave', closeMoreFlyout);
    } else {
      a.addEventListener('mouseenter', ()=>showGnavTip(a));
      a.addEventListener('mouseleave', hideGnavTip);
    }
  });
  // "更多"卡片自身 hover 不立即收起
  const fo = document.getElementById('more-flyout');
  if(fo){
    fo.addEventListener('mouseenter', ()=>{ clearTimeout(_flyTimer); });
    fo.addEventListener('mouseleave', closeMoreFlyout);
  }
}
let _gnavTipTimer = null;
function showGnavTip(a){
  const gnav = document.querySelector('.gnav');
  if(!gnav || !gnav.classList.contains('collapsed')) return;
  const tip = document.getElementById('gnav-tip');
  if(!tip) return;
  const name = a.dataset.name || (a.querySelector('.lbl') ? a.querySelector('.lbl').textContent.trim() : '');
  if(!name) return;
  clearTimeout(_gnavTipTimer);
  tip.textContent = name;
  const r = a.getBoundingClientRect();
  tip.style.left = (r.right + 10) + 'px';
  tip.style.top = (r.top + r.height / 2) + 'px';
  tip.style.transform = 'translateY(-50%)';
  tip.style.display = 'block';
}
function hideGnavTip(){
  clearTimeout(_gnavTipTimer);
  const tip = document.getElementById('gnav-tip');
  if(tip) tip.style.display = 'none';
}
function go(p, tabId) {
  // v6.2 性能 baseline：路由切换打点（开始时间 + 路由名）
  try { window.__perf && window.__perf.markRouteStart(p + (tabId?('/' + tabId):'')); } catch(_){}
  // 2026-09-04 合并队列页已取消（GitHub 式整合）：go('branch') 统一重定向到 图谱工作区页(kb-d) 的「合并请求」Tab
  if(p==='branch'){
    p = 'kb'; tabId = 'kb-d';
    setTimeout(()=>{ const d=document.getElementById('kb-d'); if(d && d.classList.contains('on')) wsTab('mr'); }, 250);
  }
  // 2026-09-17 R7：模型配置路由收口到 go() 入口（原在 loadPage 内二次跳转，会先把 'modelcfg' 写进「最近访问」）
  if(p==='modelcfg'){ p = 'studio'; tabId = 'st-model'; }
  // 未指定子 Tab 时进入默认 Tab（文档管道 / 提示词模板；知识浏览入口已隐藏，不再作为默认页）
  if(p==='kb' && !tabId) tabId = 'kb-e';
  // 2026-09-16：能力中心默认落在 Agent（用户反馈：此前默认技能，与「Agent 优先」的心智不符）
  if(p==='studio' && !tabId) tabId = 'st-agent';
  // P2 最近访问：记录非工作台页面（去重、限 5，供工作台「最近访问」展示）
  if(p && p!=='home'){
    let rp = [];
    try{ rp = JSON.parse(localStorage.getItem('mbse_recent_pages')||'[]'); }catch(e){}
    rp = [p, ...rp.filter(x=>x!==p)].slice(0,5);
    localStorage.setItem('mbse_recent_pages', JSON.stringify(rp));
  }
  let _navHit = false;
  document.querySelectorAll('#mainnav a').forEach(a=>{
    let on = a.dataset.page===p && (!a.dataset.tab || a.dataset.tab===tabId);
    // 2026-09-17 知识中心整合：kb-d/kb-e 两 Tab 同指一个导航项（nav-kbhub）
    if(p==='kb' && a.id==='nav-kbhub') on = (tabId==='kb-d'||tabId==='kb-e');
    // 2026-08-31 本体模型 | 术语词典 双导航项同指 kb-c：按语境（_kbCtx）高亮，避免同时点亮
    if(on && a.dataset.tab==='kb-c'){
      on = (a.id==='nav-glossary2') ? (window._kbCtx==='terms') : (window._kbCtx!=='terms');
    }
    if(on) _navHit = true;
    a.classList.toggle('on', on);
  });
  // 2026-09-17 P2-4 高亮兜底：无对应导航项的子页点亮其所属域（kb-a/kb-b → 知识中心）
  if(!_navHit) navFallbackOn(p);
  document.querySelectorAll('.page').forEach(d=>d.classList.toggle('on',d.id==='pg-'+p));
  // 2026-09-16：离开 AI 建模页 → 清除左侧会话列表选中态；回到 AI 页 → 恢复当前会话高亮
  // （会话仍保持打开状态——currentConvId 不动，仅视觉选中随页面走）
  const _tcl = document.getElementById('task-conv-list') || document.getElementById('conv-list');
  if(_tcl){
    _tcl.querySelectorAll('.task-it.on').forEach(it=>{
      it.classList.toggle('on', p==='ai' && String(currentConvId||'')===String(it.dataset.id||''));
    });
  }
  // 2026-09-07 kb-c 视口适配兜底：老浏览器不支持 CSS :has() 时由 .ont-fill 驱动同一 flex 布局（见 kb.css）
  const _pgkb=document.getElementById('pg-kb');
  if(_pgkb) _pgkb.classList.toggle('ont-fill', p==='kb' && tabId==='kb-c');
  // 2026-09-04 v2：会话列表常驻 gnav-task（不随页面切换显隐），AI 建模页自动展开以便用户继续会话
  const _tg = document.getElementById('gnav-task');
  if(_tg && p === 'ai' && !currentConvId && _tg.classList.contains('collapsed')){
    // 仅当首次进入 AI 建模页且尚未选中会话时，才把任务分组展开 —— 让用户能直接看到候选
    toggleTaskGroup(false);
  }
  // 兼容旧 conv-sidebar 引用：CSS 已隐藏，DOM 不动，避免其它 JS 报空
  const _cs = document.getElementById('conv-sidebar');
  if(_cs) _cs.classList.toggle('show', false);
  // 知识库模块工具行（模块名 + 右上角分支切换）：仅知识库页面显示；模块名跟随当前子页
  const _kbBar = document.getElementById('kb-toolbar');
  if(_kbBar) _kbBar.style.display = (p==='kb') ? 'flex' : 'none';
  if(p==='kb'){
    const _mt = document.getElementById('kb-module-title');
    if(_mt) _mt.textContent = (tabId && KB_TAB_TITLES[tabId]) || KB_TAB_TITLES['kb-e'] || '知识库';
  }
  // 顶栏面包屑：root 产品名固定 AI4MBSE，cur=当前页面/Tab 名（避免与页内 H3 重复呈现）
  // 2026-09-17 D2：统一走 pageCrumb()，知识中心域（kb-d/kb-e）只显示一级名「知识中心」
  document.getElementById('br-cur').textContent = pageCrumb(p, tabId);
  const _pagesEl = document.getElementById('pages');
  if(_pagesEl) _pagesEl.scrollTop = 0;
  loadPage(p, tabId);
  // v6.2 性能 baseline：路由切换打点结束（microtask 内捕获，避免 missing paint frame）
  setTimeout(()=>{ try{ window.__perf && window.__perf.markRouteEnd(p + (tabId?('/' + tabId):'')); }catch(_){} }, 0);
}
document.querySelectorAll('#mainnav a').forEach(a=>a.addEventListener('click',()=>{
  // 2026-09-04 「新建任务」等动作型导航项无 data-page，不参与页面路由
  if(!a.dataset.page) return;
  // 2026-08-31 记录 kb-c 页语境（本体模型 | 术语词典），供 go() 高亮区分双导航项
  if(a.dataset.page==='kb'){
    if(a.id==='nav-glossary2') window._kbCtx='terms';
    else if(a.dataset.tab==='kb-c') window._kbCtx='model';
  }
  go(a.dataset.page, a.dataset.tab);
}));
  // 2026-09-17 R5：原「数据整理双入口（nav-glossary/nav-workbench）+ _kbBEntry 归属」逻辑整体移除——
  // 两个导航入口已于 2026-08-31 / 2026-09-11 先后隐藏，_kbBEntry 无生产者（21-ontology.js 的写入也已同步清理），
  // 保留只会误导后续维护者以为双入口仍在。数据整理页（kb-b）深链由 go('kb','kb-b') 直接可达。
  // 2026-09-17 R6：术语词典不再用 setTimeout(showOntTerms,500) 硬等待（500ms 内切走会被强制改回 terms 视图、覆盖面包屑）；
  // 改由 loadKBTab('kb-c') 读取 window._kbCtx 后同步分流（见 15-kb.js）。
  // 术语词典内容面板（kb-c 内）：隐藏呈现形式 tab 行与 edit 面板，仅显示术语词典（由 loadKBTab('kb-c') 在 _kbCtx==='terms' 时调用）
  function showOntTerms(){
    window._kbCtx = 'terms';
    document.getElementById('br-cur').textContent = '术语词典';
    const row=document.getElementById('ont-subtab-row'); if(row) row.style.display='none';
    const ed=document.getElementById('ont-pane-edit'); if(ed) ed.style.display='none';
    const tp=document.getElementById('ont-pane-terms'); if(tp) tp.style.display='block';
    conceptsLoad();
  }
function tab(el,grp,id) {
  document.querySelectorAll('[data-tabgrp="'+grp+'"]').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');
  document.querySelectorAll('[data-tabpanel="'+grp+'"]').forEach(d=>d.classList.toggle('on',d.id===id));
  if(grp==='kb') loadKBTab(id);
  if(grp==='st') loadStudioTab(id);
  if(grp==='stg') loadSettingsTab(id);
  if(grp==='ur') loadURTab(id);
  if(grp==='ap') loadApprovalTab(id);
  // 二级页提升一级导航：页面内 Tab 切换联动主导航高亮 + 顶栏面包屑 cur
  if(grp==='kb'||grp==='st'||grp==='stg'){
    const pg = grp==='kb' ? 'kb' : (grp==='st' ? 'studio' : 'settings');
    const cur = document.querySelector('.page.on');
    if(cur && cur.id==='pg-'+pg){
      // 2026-09-17 D2：统一走 pageCrumb()（知识中心域只显示一级名）
      document.getElementById('br-cur').textContent = pageCrumb(pg, id);
    }
    let _navHit = false;
    document.querySelectorAll('#mainnav a').forEach(a=>{
      let on = !!(a.dataset.page===pg && (!a.dataset.tab || a.dataset.tab===id));
      // 2026-09-17 知识中心整合：kb-d/kb-e 两 Tab 同指一个导航项（nav-kbhub）
      if(pg==='kb' && a.id==='nav-kbhub') on = (id==='kb-d'||id==='kb-e');
      if(on) _navHit = true;
      a.classList.toggle('on', on);
    });
    // 2026-09-17 P2-4 高亮兜底：能力中心非 Agent 子 Tab / kb-a·kb-b 深链 → 点亮所属域导航项
    if(!_navHit) navFallbackOn(pg);
  }
}

// ── 页面加载 ──
function loadPage(p, tabId) {
  // 二级页提升一级导航：子 Tab 入口直达（subtab 已移除，直接切换 subpage）
  if(tabId){
    if(p==='kb' && KB_TAB_TITLES[tabId]){
      document.querySelectorAll('[data-tabpanel="kb"]').forEach(d=>d.classList.toggle('on', d.id===tabId));
      loadKBTab(tabId);
      return;
    }
    if(p==='studio' && ST_TAB_TITLES[tabId]){
      document.querySelectorAll('[data-tabpanel="st"]').forEach(d=>d.classList.toggle('on', d.id===tabId));
      // 2026-09-08：外部直达子 Tab（如旧 modelcfg 路由重定向）时同步页内 tab 条高亮
      document.querySelectorAll('[data-tabgrp=stlib]').forEach(x=>x.classList.toggle('on', x.dataset.pane===tabId));
      loadStudioTab(tabId);
      return;
    }
    if(p==='settings' && STG_TAB_TITLES[tabId]){
      document.querySelectorAll('[data-tabpanel="stg"]').forEach(d=>d.classList.toggle('on', d.id===tabId));
      loadSettingsTab(tabId);
      return;
    }
  }
  if(p==='home') loadHome();
  if(p==='ai') { loadConversations(); loadCurrentProject(); loadQuickBar(); loadChatProviders(); }
  if(p==='kb') { loadKBStats(); loadKBEntities(); }
  // 2026-09-17 R7：移除 p==='branch' 分支（go() 已把 branch 重定向到 kb/kb-d，此分支不可达）
  if(p==='reports') loadReports();
  if(p==='studio') loadStudioTab('st-prompt');
  // 2026-09-17 R7：p==='modelcfg' 重定向已上移到 go() 入口（避免 'modelcfg' 被写进「最近访问」）
  if(p==='settings') loadSettingsTab('st-projmem');
  if(p==='users') loadURTab('ur-a');
  if(p==='audit') loadAudit();
  if(p==='ops') loadOps();
  if(p==='approval') loadApprovalTab('ap-type');
}

// ── 工作台 ──
async function loadHome() {
  const d = await api('/api/dashboard');
  const kpis = document.querySelectorAll('#home-kpis .kpi .n');
  kpis[0].textContent = d.active_conversations;
  kpis[1].textContent = d.conflicts;
  // 2026-09-04 v2：消息通知 chip 已从 UI 移除；如页面里仍存在 todo-count（如旧缓存页），则安全更新；否则静默忽略
  const tc = document.getElementById('todo-count');
  if(tc) tc.textContent = d.conflicts;
  // ✅ 我的待办：任务闭环入口（可点击直达处理页）
  const td = d.todo||{};
  const todoEl = document.getElementById('home-todo');
  if(todoEl) todoEl.innerHTML = `
    <div class="todo-item" title="点击进入「合并请求」评审合并" onclick="go('branch')"><span>🔀 合并请求待评审</span><b class="st ${td.pending_merge_review>0?'w':''}">${td.pending_merge_review}</b></div>
    <div class="todo-item" title="点击进入「合并请求」处理冲突" onclick="go('branch')"><span>⚠️ 冲突告警</span><b class="st ${td.conflicts>0?'r':''}">${td.conflicts}</b></div>
    <div class="todo-item" title="点击进入「数据整理」" onclick="go('kb','kb-b')"><span>🧬 知识评审待办</span><b class="st ${td.knowledge_review>0?'w':''}">${td.knowledge_review}</b></div>`;
  // 💬 最近对话：一键进入
  const convs = d.recent_conversations||[];
  const convEl = document.getElementById('recent-convs-home');
  if(convEl) convEl.innerHTML = convs.length
    ? convs.map(c=>`<div style="display:flex;align-items:center;gap:6px;padding:5px 4px;border-bottom:1px dashed var(--line);cursor:pointer;border-radius:6px;" onclick="goToConversation(${c.id})" title="进入对话：${esc(c.title)}">
        <span class="st ${c.status==='active'?'ok':'g'}">${c.status==='active'?'进行中':'已归档'}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px;">${esc(c.title)}</span>
        <small style="color:var(--mut);font-size:10.5px;flex:none;">${esc((c.updated_at||'').slice(5,16).replace('T',' '))}</small>
      </div>`).join('')
    : '<div style="font-size:11.5px;color:var(--mut);padding:4px 0;">暂无对话 — 点击「新建建模对话」开始</div>';
  // 📦 资产概览：模型资产一屏掌握
  const as = d.assets||{};
  const assetEl = document.getElementById('assets-home');
  if(assetEl) assetEl.innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
    <div class="asset"><div class="n">${as.entities||0}</div><div class="l">🧬 实体</div></div>
    <div class="asset"><div class="n">${as.relations||0}</div><div class="l">🔗 关系</div></div>
    <div class="asset"><div class="n">${as.documents||0}</div><div class="l">📁 文档</div></div>
    <div class="asset"><div class="n">${as.models||0}</div><div class="l">🧠 模型</div></div>
  </div>`;
  // 角色化问候 + 快捷入口（角色预置推荐：按权限域判定角色，动态渲染推荐入口）
  let me = null;
  try{ me = await api('/api/users/me'); }catch(e){}
  if(me && me.display_name){
    const g = document.getElementById('home-greeting');
    const roleTxt = (me.role_name||'未分配角色') + (me.role_type==='preset'?'（预置）':'');
    const deptTxt = me.department ? (' · ' + me.department) : '';
    if(g) g.innerHTML = `你好，<b>${esc(me.display_name)}</b>（${esc(roleTxt)}${esc(deptTxt)}）｜ 专属工作空间，数据/模型/配置隔离（FR-UR-3）`;
    const prof = getRoleProfile(me);
    const reEl = document.getElementById('role-entries-home');
    if(reEl) reEl.innerHTML = `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">当前视角：<b>${esc(prof.label)}</b> — 按角色权限推荐入口</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">` +
      prof.entries.map(e=>`<button class="btn ghost" onclick="go('${e[1]}'${e[2]?`,'${e[2]}'`:''})">${e[0]}</button>`).join('') + '</div>';
  } else {
    const reEl = document.getElementById('role-entries-home');
    if(reEl) reEl.innerHTML = '<div style="font-size:11px;color:var(--mut);">未登录 — 切换账号后按角色展示专属入口</div>';
  }
  // 🕘 最近操作轨迹（audit_logs 当前用户）
  const acts = d.recent_activities||[];
  const actEl = document.getElementById('recent-acts-home');
  if(actEl) actEl.innerHTML = acts.length
    ? acts.map(a=>`<span class="tag" title="${esc(a.detail||'')}" style="font-size:11px;">${esc(a.event_type||'')}：${esc((a.detail||'').slice(0,24))} <small style="opacity:.7;">${esc((a.created_at||'').slice(5,16).replace('T',' '))}</small></span>`).join('')
    : '<span style="font-size:11px;color:var(--mut);">暂无操作记录 — 完成建模/审核等操作后会显示在这里</span>';
  renderRecentPages();
  // 近期报告：报告前 5 条（工作台资产聚合入口）
  try{
    const reports = await api('/api/reports?keyword=');
    const el = document.getElementById('recent-reports-home');
    const typeCls = {impact:'b', review:'w', analysis:'ok', other:'g'};
    if(!reports || !reports.length){
      el.innerHTML = '<div style="font-size:11.5px;color:var(--mut);padding:4px 0;">暂无报告 — AI 建模产出报告后自动归档</div>';
    }else{
      el.innerHTML = reports.slice(0,5).map(r=>`<div style="display:flex;align-items:center;gap:6px;padding:3px 0;border-bottom:1px dashed var(--line);">
        <span class="st ${typeCls[r.report_type]||'g'}" style="flex:none;">${esc(r.report_type_label||r.report_type)}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px;cursor:pointer;color:var(--blue-d);" onclick="viewReport(${r.id})" title="${esc(r.title)}">${esc(r.title)}</span>
        <span style="font-size:10.5px;color:var(--mut);flex:none;">${esc((r.created_at||'').slice(5,16).replace('T',' '))}</span>
      </div>`).join('') +
      (reports.length>5?`<div style="font-size:11px;color:var(--mut);padding-top:4px;text-align:center;">… 共 ${reports.length} 份，<a style="cursor:pointer;color:var(--blue-d);" onclick="go('reports')">查看全部 →</a></div>`:'');
    }
  }catch(e){ const el = document.getElementById('recent-reports-home'); if(el) el.innerHTML = '<div style="font-size:11.5px;color:var(--mut);">加载失败</div>'; }
}
// 工作台最近对话：进入 AI 建模并选中该对话
