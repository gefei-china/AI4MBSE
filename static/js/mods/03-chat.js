/* 会话：会话列表 / 欢迎页 / 任务分组
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 1040-1467  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function goToConversation(id){
  go('ai');
  setTimeout(()=>{ try{ selectConv(id); }catch(e){} }, 350);
}
// P2 角色化布局：按权限域判定角色侧重 → 推荐入口（Perspective）
function getRoleProfile(me){
  const perms = (me && me.permissions) || {};
  const admin = perms['admin']||[];
  const kbOnto = perms['kb_ontology']||[];
  const design = (perms['ai_chat']||[]).concat(perms['ai_studio']||[]).concat(perms['branch_dev']||[]);
  // 强特征判定：admin 管理操作 > 知识治理写操作（本体编辑/评审合并）> 设计操作
  const isAdmin = admin.includes('user_manage')||admin.includes('ops_manage')||admin.includes('audit_view');
  const isKnowledge = kbOnto.includes('edit')||kbOnto.includes('profile_io')||
    (perms['branch_release']||[]).includes('review_merge');
  const isDesigner = design.length>0;
  let key = 'general';
  if(isAdmin) key = 'admin';
  else if(isKnowledge) key = 'knowledge';
  else if(isDesigner) key = 'designer';
  const profiles = {
    designer:  {label:'设计师',   entries:[['💬 新建建模对话','ai'],['🔀 变更影响分析','ai'],['📄 报告','reports'],['🔀 合并请求','branch']]},
    // 2026-09-17 D3：与主导航口径同步——「资料库 / 图谱工作区」合并为「🕸 知识中心」（页内双 Tab 切换）；
    // 「AI 设计工坊」统一称「能力中心」；2026-09-11 数据整理入口已隐藏（深链 go('kb','kb-b') 仍可用）
    knowledge: {label:'知识工程师', entries:[['🕸 知识中心','kb','kb-a'],['🧬 本体模型','kb','kb-c'],['💬 AI 建模','ai']]},
    // 2026-10-23：审批管理入口下线（纯前端原型、假审批流水，后端另立需求重做）
    admin:     {label:'系统管理员', entries:[['👥 用户与权限','users'],['🛡 审计日志','audit'],['📈 运行监控','ops']]},
    general:   {label:'通用',     entries:[['💬 新建建模对话','ai'],['🕸 知识中心','kb','kb-a'],['🏭 能力中心','studio'],['📄 报告','reports']]},
  };
  return profiles[key] || profiles.general;
}
// P2 最近访问：渲染最近访问过的页面标签（localStorage mbse_recent_pages）
function renderRecentPages(){
  const el = document.getElementById('recent-pages-home');
  if(!el) return;
  let rp = [];
  try{ rp = JSON.parse(localStorage.getItem('mbse_recent_pages')||'[]'); }catch(e){}
  if(!rp.length){ el.innerHTML = '<span style="font-size:11px;color:var(--mut);">🕘 最近访问：暂无 — 访问过的页面会出现在这里</span>'; return; }
  // 2026-09-17 R7 兼容：TITLES 已移除 branch/modelcfg 死条目，但旧 localStorage 里可能仍存有这两个值，
  // 保留一个只读的旧值兜底映射，避免「最近访问」出现裸英文标签
  const _legacy = {branch:'知识中心', modelcfg:'模型配置'};
  el.innerHTML = '<span style="color:var(--mut);font-size:11px;">🕘 最近访问：</span>' +
    rp.map(p=>`<span class="tag" style="cursor:pointer;font-size:11px;" onclick="go('${p}')">${esc(TITLES[p]||_legacy[p]||p)}</span>`).join(' ');
}

// ── AI 建模 ──
async function loadConversations() {
  // 2026-09-06 P0-3：拉取前给任务列表插 skeleton 占位，0 网络等待的空白感
  const _list = _getConvListEl();
  if(_list && !_list.children.length){
    _list.innerHTML = Array.from({length:5}).map(()=>`
      <div class="task-it skel" style="padding:7px 10px;">
        <div class="sk-line" style="width:80%;"></div>
        <div class="sk-line sm" style="width:50%;margin-top:6px;opacity:.6;"></div>
      </div>`).join('');
  }
  const convs = await api('/api/conversations');
  const _isArr = Array.isArray(convs);
  // 2026-09-24 左侧项目管理：**项目下的任务只在项目分组内展示**，本组降级为「未分组任务」兜底。
  // 归属判定用后端给的 c.grouped（= 已归属某个仍存在的项目），前端不拿 project_id 非空自判。
  const all = _isArr ? convs : [];
  // 2026-10-16 P0：缓存最近一次全量列表，供切换会话时局部刷新高亮（避免每次切换都重拉全量）
  window._convListCache = all;
  const ungrouped = all.filter(c=>!c.grouped);
  // 2026-09-28 多工程 P0-2：记录当前任务的工程归属（顶栏据此给出「未关联工程 · 归入」收敛入口）。
  const _cc = currentConvId ? all.find(c=>c.id===currentConvId) : null;
  window._curConvProjectId = _cc ? String(_cc.project_id || '') : undefined;
  if(typeof renderProjectTag === 'function') renderProjectTag();
  // 2026-09-04 v4：AI 页且无会话 → 中栏展示欢迎屏（非草稿态）
  const onAI = document.getElementById('pg-ai') && document.getElementById('pg-ai').classList.contains('on');
  if(onAI && !currentConvId && !_isDraft) renderChatWelcome();
  // 2026-10-16 P1/P2：搜索 / 置顶优先 / 按时间分组 / 行画像 / 分页，统一由 _renderConvListView 渲染
  _renderConvListView();
}

// ── 2026-10-16 P1/P2 会话列表增强：搜索 / 置顶优先 / 按时间分组 / 行画像 / 分页 ──
// 数据全部来自 window._convListCache（切换会话不再重拉，见 _refreshConvSelection）；
// 本段只做「过滤(标题/意图即时 + 内容后端合并) → 排序(置顶优先) → 时间分组 → 分页渲染」。
const _CONV_PAGE = 50;
let _convView = { search: '', rendered: 0, contentMatches: null };
let _convContentTimer = null;

function _getConvListEl(){
  return document.getElementById('task-conv-list') || document.getElementById('conv-list');
}
function _nowISO(){
  const d = new Date(), p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function _convTimeBucket(iso){
  if(!iso) return 'earlier';
  // ★ 2026-10-28：改用 parseServerTime（统一按 UTC 解析服务端时间戳）。
  //   原 `new Date(String(iso).replace(' ','T'))` 会把 UTC 当本地 ⇒ 分桶/时间早 8 小时。
  const d = parseServerTime(iso);
  if(!d) return 'earlier';
  const now = new Date();
  const t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const y0 = new Date(t0); y0.setDate(y0.getDate()-1);
  const w0 = new Date(t0); w0.setDate(w0.getDate()-7);
  if(d >= t0) return 'today';
  if(d >= y0) return 'yest';
  if(d >= w0) return 'week';
  return 'earlier';
}
// 从缓存过滤（未分组）+ 搜索（标题/意图即时，内容后端合并）+ 置顶优先排序
function _convFiltered(){
  let list = (window._convListCache || []).filter(c => !c.grouped);
  const q = (_convView.search || '').toLowerCase().trim();
  if(q){
    list = list.filter(c =>
      (c.title||'').toLowerCase().includes(q) || (c.intent||'').toLowerCase().includes(q));
    const cm = _convView.contentMatches;
    if(cm && cm.length){
      const have = new Set(list.map(c => c.id));
      for(const c of cm){ if(!c.grouped && !have.has(c.id)){ list.push(c); have.add(c.id); } }
    }
  }
  list.sort((a,b) => {
    const pa = a.pinned_at ? 1 : 0, pb = b.pinned_at ? 1 : 0;
    if(pa !== pb) return pb - pa;
    return (b.updated_at||'').localeCompare(a.updated_at||'');
  });
  return list;
}
function _convItemHTML(c){
  const pinned = !!c.pinned_at;
  // 2026-10-16：竖向铅笔 SVG 替代 ✏ emoji（emoji 在 Windows 渲染为横放笔形，用户要求竖向）；
  // 移除标题下方 ti-meta 行（intent/条数/时间）——信息仍保留在 data-* 属性，hover 提示可见。
  const PEN_V = '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true"><path fill="currentColor" d="M10 2.5h4v3h-4zM10 7h4v9.5l-2 4.5-2-4.5z"/></svg>';
  return `
    <div class="task-it ${c.id===currentConvId?'on':''}" data-id="${c.id}" data-intent="${escA(c.intent||'')}" data-updated="${escA(c.updated_at||'')}" data-msg="${c.msg_count||0}"
      onclick="selectConv(${c.id})" onmouseenter="showTaskTip(this)" onmouseleave="hideTaskTip()">
      <div class="ti-row">
        <span class="ti-tt" title="${esc(c.title)}">${esc(c.title)}</span>
        <span class="ti-ops" onclick="event.stopPropagation()">
          <button class="ti-pin ${pinned?'on':''}" title="${pinned?'取消置顶':'置顶'}" onclick="togglePinConv(${c.id})">${pinned?'📌':'📍'}</button>
          <button title="重命名" onclick="renameConv(${c.id})">${PEN_V}</button>
          <button title="删除" onclick="deleteConv(${c.id})">🗑</button>
        </span>
      </div>
    </div>`;
}
function _convGroupBlock(label, items){
  return `<div class="conv-grp-h">${label}<span class="conv-grp-n">${items.length}</span></div>`
       + items.map(_convItemHTML).join('');
}
function _convGroupRender(items){
  const pinned = items.filter(c => c.pinned_at);
  const rest = items.filter(c => !c.pinned_at);
  const b = {today:[],yest:[],week:[],earlier:[]};
  for(const c of rest) b[_convTimeBucket(c.updated_at)].push(c);
  let h = '';
  if(pinned.length)   h += _convGroupBlock('📌 置顶', pinned);
  if(b.today.length) h += _convGroupBlock('今天', b.today);
  if(b.yest.length)  h += _convGroupBlock('昨天', b.yest);
  if(b.week.length)  h += _convGroupBlock('本周', b.week);
  if(b.earlier.length) h += _convGroupBlock('更早', b.earlier);
  return h;
}
function _renderConvListView(){
  const list = _getConvListEl();
  if(!list) return;
  const items = _convFiltered();
  const cnt = document.getElementById('task-count');
  if(cnt){ cnt.textContent = String(items.length); cnt.classList.toggle('zero', items.length === 0); }
  if(!items.length){
    const searching = !!_convView.search;
    const inProjects = (window._convListCache||[]).some(c => c.grouped);
    list.innerHTML = searching
      ? `<div class="task-empty"><div class="te-title">未找到匹配的会话</div><div class="te-sub">换个关键词，或检查是否含错别字</div></div>`
      : `<div class="task-empty">
           <div class="te-ic"><svg width="34" height="34" aria-hidden="true" style="color:var(--color-primary);"><use href="#ic-empty-inbox"/></svg></div>
           <div class="te-title">${inProjects ? '暂无未分组任务' : '暂无任务'}</div>
           <div class="te-sub">${inProjects ? `${inProjects} 个任务已归入上方「项目」分组<br>展开项目即可查看` : '点击「＋ 新建任务」<br>或下方快捷入口'}</div>
           <button class="te-go" onclick="go('ai');setTimeout(newTask,200);"><svg width="12" height="12" aria-hidden="true" style="color:currentColor;margin-right:4px;"><use href="#ic-plus"/></svg>新建任务</button>
         </div>`;
    return;
  }
  const shown = items.slice(0, _convView.rendered + _CONV_PAGE);
  let html = _convGroupRender(shown);
  if(shown.length < items.length){
    html += `<button class="conv-more" onclick="_convLoadMore()">加载更多（还剩 ${items.length - shown.length} 条）</button>`;
  }
  list.innerHTML = html;
}
function _convLoadMore(){ _convView.rendered += _CONV_PAGE; _renderConvListView(); }
function _onConvSearch(val){
  _convView.search = (val || '').trim();
  _convView.rendered = 0;
  _renderConvListView();
  clearTimeout(_convContentTimer);
  if(_convView.search){
    const q = _convView.search;
    _convContentTimer = setTimeout(async () => {
      try{
        const res = await api('/api/conversations/search?q=' + encodeURIComponent(q));
        _convView.contentMatches = Array.isArray(res) ? res : [];
      }catch(e){ _convView.contentMatches = null; }
      if(_convView.search === q) _renderConvListView();   // 仅当搜索词未变才刷新，避免旧请求覆盖新输入
    }, 350);
  } else {
    _convView.contentMatches = null;
  }
}
async function togglePinConv(id){
  const c = (window._convListCache || []).find(x => x.id === id);
  const willPin = !(c && c.pinned_at);
  try{
    await api(`/api/conversations/${id}`, {method:'PATCH', body: JSON.stringify({pinned: willPin})});
    if(c) c.pinned_at = willPin ? _nowISO() : null;   // 乐观更新缓存，免二次全量拉取
    _renderConvListView();
  }catch(e){ toast('置顶操作失败：' + (e.message || '')); }
}
// ── 2026-10-16 P0 切换慢修复：切换会话时的「选中态」局部刷新 ──
// 替代 selectConv 内原先的 loadConversations()+renderGnavProjects() 全量重拉。
// 那两个调用各自拉一次全量 /api/conversations（当前 325 行、每行带关联 COUNT 子查询）并重建整棵侧栏 DOM，
// 单次切换 = 2 次全量查询 + 2 次整列重建；而切换并不改变列表内容，纯属浪费。
// 本函数只做高亮态局部更新：切换 .on 类 + 刷新当前会话工程归属，零网络、零整列重建。
function _refreshConvSelection(id){
  document.querySelectorAll('.task-it[data-id]').forEach(el=>{
    el.classList.toggle('on', +el.dataset.id === id);
  });
  // 顶栏「归入项目」收敛入口依赖 _curConvProjectId：从最近一次列表缓存取，不重新拉服务端
  const cache = (window._convListCache || []);
  const c = cache.find(x=>x.id === id);
  window._curConvProjectId = c ? String(c.project_id || '') : undefined;
  if(typeof renderProjectTag === 'function') renderProjectTag();
}
// ── 2026-09-04 v3：会话条目 hover 信息卡（名称/类型/更新时间/内容摘要）──
// 2026-09-28：两处修正（用户反馈）
//   ① **摘要改为服务端真正概括**：此前是「取最后 1 条消息 → 去符号 → slice(0,140)」的原文截断，
//      观感就是"截了一段"。现调 `/api/conversations/{id}/summary`，失败才退回本地兜底概述。
//   ② **两个入口字段对齐**：项目分组下的 .task-it 此前缺 data-intent/data-updated/data-msg
//      （41-projects.js 已补），但即使补了，只要**将来有新入口**再漏一次就又会不一致 ——
//      故这里改为「dataset 缺失时用接口数据补齐」，从机制上消除"两种入口显示不一致"。
let _taskTipTimer = null;
let _taskTipId = 0;
// 摘要来源徽标：让"这是模型概括还是结构化概述"可辨（便于甄别降级，不再是无从判断的黑盒）
const TT_SRC = {llm: '模型概括', structured: '概述（未接模型）', empty: ''};
async function showTaskTip(el){
  const tip = document.getElementById('task-tip');
  if(!tip) return;
  const id = +el.dataset.id;
  _taskTipId = id;
  const title = (el.querySelector('.ti-tt') ? el.querySelector('.ti-tt').textContent : '').trim() || '未命名';
  const intent = el.dataset.intent || '';
  const updated = el.dataset.updated || '';
  const msg = el.dataset.msg || '0';
  clearTimeout(_taskTipTimer);
  tip.innerHTML = `
    <div class="tt-title"><span class="tt-dot"></span><span>${esc(title)}</span></div>
    <div class="tt-row tt-row-meta"><span class="tt-kind">🗂 本地任务</span><span class="tt-tag">${esc(intent || '建模会话')}</span><span class="tt-cnt">💬 ${esc(String(msg))} 条</span></div>
    <div class="tt-row tt-row-time"${updated ? '' : ' style="display:none;"'}>🕒 更新于 <span style="font-variant-numeric:tabular-nums;">${updated ? esc(fmtLocalTime(updated)) : ''}</span></div>
    <div class="tt-sum" id="tt-sum">正在生成摘要…</div>`;
  const r = el.getBoundingClientRect();
  tip.style.left = (r.right + 10) + 'px';
  tip.style.top = (r.top - 6) + 'px';
  tip.style.display = 'block';
  const sumEl0 = document.getElementById('tt-sum');
  try{
    const r2 = await api(`/api/conversations/${id}/summary`);
    if(_taskTipId !== id) return;   // 已切换到其它会话，忽略过期结果
    const txt = String((r2 && r2.summary) || '').trim();
    const src = String((r2 && r2.source) || '');
    if(sumEl0){
      sumEl0.innerHTML = txt
        ? esc(txt) + (TT_SRC[src] ? `<span class="tt-src">${esc(TT_SRC[src])}</span>` : '')
        : '（暂无消息内容）';
    }
    // dataset 缺失（新入口漏挂属性）→ 用接口值回填，保证与未分组列表一致
    if(!updated && r2 && r2.updated_at){
      const rowT = tip.querySelector('.tt-row-time');
      if(rowT){ rowT.style.display = ''; rowT.innerHTML = `🕒 更新于 <span style="font-variant-numeric:tabular-nums;">${esc(fmtLocalTime(r2.updated_at))}</span>`; }
    }
    if((!msg || msg === '0') && r2 && typeof r2.msg_count === 'number'){
      const cntEl = tip.querySelector('.tt-cnt');
      if(cntEl) cntEl.textContent = `💬 ${r2.msg_count} 条`;
    }
  }catch(e){
    if(_taskTipId !== id) return;
    // 摘要接口不可用（旧后端/网络）→ 本地兜底：取最后一条 AI+用户消息合成概述（仍不做硬截断）
    try{
      const mr = await api(`/api/conversations/${id}/messages?limit=40`);
      if(_taskTipId !== id) return;
      const msgs = Array.isArray(mr) ? mr : (mr && Array.isArray(mr.messages) ? mr.messages : []);
      const clean = s => String(s||'').replace(/```[\s\S]*?```/g,' ').replace(/[#*`>\[\](){}\r\n]/g,' ').replace(/\s+/g,' ').trim();
      const fu = msgs.find(m=>m.role==='user');
      const la = [...msgs].reverse().find(m=>m.role==='assistant' && clean(m.content));
      const t1 = clean(fu && fu.content); const t2 = clean(la && la.content);
      const txt = t1 ? (t2 && t2 !== t1 ? (t1.slice(0,110) + ' ｜ 结论：' + t2.slice(0,110)) : t1.slice(0,160)) : t2.slice(0,160);
      if(sumEl0) sumEl0.textContent = txt || '（摘要加载失败）';
    }catch(e2){
      if(sumEl0) sumEl0.textContent = '（摘要加载失败）';
    }
  }
}
function hideTaskTip(){
  clearTimeout(_taskTipTimer);
  const tip = document.getElementById('task-tip');
  if(tip) tip.style.display = 'none';
}
// ── 2026-09-04 v4：会话初始欢迎屏（WorkBuddy 风格）──
// 主智能选项（场景/主 Agent persona，第一个默认选中）；点击 pill 把起始提示填入输入框
// 2026-09-04 v5：去掉「快速模块」，改为「标题 + 主智能 pills + 画布中部大输入卡片」布局。
// 输入区（#chat-input-dock，含 mention/引用/技能/模型等全部控件）初始态整体迁移到画布中部居中展示，
// 发送或选中已有会话后归位到底部（保持既有功能零改动）。
const CW_SCENARIOS = [
  {ic:'🎯', label:'需求工程', prompt:'请以系统工程师视角，帮我梳理本系统的需求，并把需求条目化/结构化。我先把需求描述发给你：'},
  {ic:'🧩', label:'功能设计', prompt:'请基于已定义的需求，设计系统的功能分解与功能流（FFBD），并输出活动图（Activity）。'},
  {ic:'🕸', label:'架构设计', prompt:'请从逻辑与物理两层设计系统架构，给出部件、接口与端口（IBD），并输出架构视图。'},
  {ic:'🧪', label:'验证分析', prompt:'请针对当前设计生成验证用例，并分析需求→功能→组件→验证的追溯闭环。'},
  {ic:'🔗', label:'追溯分析', prompt:'请生成当前模型的五元素追溯链：需求→功能→组件→验证，找出缺失或悬空的环节。'},
];
let _cwSel = 0;
let _welcomeMode = false;   // true = 输入区居中的欢迎屏态（dock 在 #chat-area 内）
function renderChatWelcome(opts){
  opts = opts || {};
  const area = document.getElementById('chat-area');
  if(!area) return;
  // 2026-09-04 v6 幂等保护：欢迎屏已渲染且输入区已在槽位内时不再重建，
  // 否则并发调用（go('ai') + 独立 loadConversations）会用 area.innerHTML 清空内含的 dock，导致输入卡片丢失。
  if(_welcomeMode && area.querySelector('.chat-welcome') && document.getElementById('chat-input-dock')
     && document.getElementById('chat-input-dock').parentNode.id === 'cw-dock-slot'){
    return;
  }
  const draftBadge = opts.draft ? '<span class="cw-draft">✎ 草稿模式 · 发送后才会出现在左侧「任务」列表</span>' : '';
  area.innerHTML = `
    <div class="chat-welcome">
      <div class="cw-hero">
        <div class="cw-logo">⚡</div>
        <h2>AI4MBSE，我帮你</h2>
        ${draftBadge}
      </div>
      <div class="cw-sec" style="max-width:760px;">
        <div class="cw-pills" id="cw-scen">${CW_SCENARIOS.map((s,i)=>`
          <div class="cw-pill ${i===_cwSel?'on':''}" onclick="cwPickScenario(${i})"><span class="cp-ic">${s.ic}</span>${s.label}</div>`).join('')}
        </div>
      </div>
      <div class="cw-dock-slot" id="cw-dock-slot"></div>
    </div>`;
  // 把底部真实输入区整体迁入画布中部居中展示（保持 id 唯一、功能不变）
  mountWelcomeDock();
  const _at = document.getElementById('chat-area');
  if(_at) _at.scrollTop = 0;
}
// 输入区迁入画布中部（欢迎屏态）：dock 从底部位移到 #cw-dock-slot
function mountWelcomeDock(){
  const dock = document.getElementById('chat-input-dock');
  const slot = document.getElementById('cw-dock-slot');
  if(!dock || !slot) return;
  slot.appendChild(dock);            // appendChild 自动从原位置摘除（节点唯一）
  dock.classList.add('in-hero');
  _welcomeMode = true;
}
// 输入区归位到底部（聊天态）：dock 从 #cw-dock-slot 移回 #chat-col 末尾，并清掉槽位
function unmountWelcomeDock(){
  const dock = document.getElementById('chat-input-dock');
  const home = document.getElementById('chat-col');
  const slot = document.getElementById('cw-dock-slot');
  if(!dock) return;
  dock.classList.remove('in-hero');
  if(home) home.appendChild(dock);   // 归位到底部（末尾）
  if(slot) slot.remove();
  _welcomeMode = false;
}
function cwPickScenario(i){
  _cwSel = i;
  document.querySelectorAll('#cw-scen .cw-pill').forEach((p,idx)=>p.classList.toggle('on', idx===i));
  const input = document.getElementById('chat-input');
  if(input){ input.value = CW_SCENARIOS[i].prompt; input.focus(); try{ handleChatInput && handleChatInput(); }catch(e){} }
}

async function createConversation() {
  const title = await promptDialog({title:'新建对话', message:'请输入对话标题：', value:'新建建模对话'});
  if(!title) return;
  const r = await api('/api/conversations', {method:'POST', body:JSON.stringify({title})});
  currentConvId = r.id;
  loadConversations();
  toast('对话已创建');
}
async function newTask(projectId, projectName) {
  // 2026-09-04 v3：新建任务 = 进入「草稿态」，不立即创建会话。
  // 首次通过 sendChat 提交正文时才 POST 创建（避免左侧列表出现空会话）；
  // 输入内容暂存到 mbse_draft_input，下次新建任务时恢复到输入框。
  // 2026-09-24：可选 projectId —— 左侧「项目 → ＋ 新建任务」发起时归属该项目，
  //   暂存到 window._pendingProjectId，由 12-chatsend.js 创建会话时随 body 提交。
  // 2026-09-29 口径（用户拍板，推翻 09-28 P0-2 的"归属本页当前工程"）：
  //   **非项目入口**（顶部导航 ＋ 新建任务 / KBar / 空态按钮）的新建任务 = 通用会话，
  //   不归属任何工程（project_id 空串，后端不落项目）；只有从项目下发起（带 projectId
  //   实参，如 41-projects.js 的「项目 → ＋ 新建任务」）才归属该工程。
  window._pendingProjectId = projectId ? String(projectId) : '';
  currentConvId = null;
  _isDraft = true;
  // 2026-09-04 v4：中栏展示 WorkBuddy 风格初始欢迎屏（主智能选项 + 快速模块）
  renderChatWelcome({draft:true});
  const input = document.getElementById('chat-input');
  if(input){
    let draft = '';
    try{ draft = localStorage.getItem('mbse_draft_input') || ''; }catch(e){}
    input.value = draft;
    input.placeholder = '输入建模需求或问题（＋ 号可引用附件 / 知识库 / 工程文件 · 行首 / 选择技能）';
    setTimeout(()=>{ try{ input.focus(); }catch(e){} }, 60);
  }
  const status = document.getElementById('chat-status');
  if(status) status.textContent = window._pendingProjectId
    ? `草稿模式：发送后创建任务并归入项目「${projectName || window._pendingProjectId}」`
    : '草稿模式：输入内容并发送后创建会话（不归属任何工程，之后可在顶栏「归入」挂到项目）';
  toggleTaskGroup(false);   // 展开分组，便于看到即将创建的任务
  go('ai');
  toast(window._pendingProjectId
    ? `新建任务：将归入项目「${projectName || window._pendingProjectId}」，发送后入列表`
    : '新建任务：草稿模式，发送后入列表（未关联工程）');
}
async function renameConv(id) {
  const convs = await api('/api/conversations');
  const c = convs.find(x=>x.id===id);
  if(!c) return;
  const title = await promptDialog({title:'重命名对话', message:'新标题：', value:c.title});
  if(!title || title===c.title) return;
  await api(`/api/conversations/${id}`, {method:'PATCH', body:JSON.stringify({title})});
  toast('对话已重命名');
  loadConversations();
}
async function deleteConv(id) {
  if(!(await confirmDialog('确认删除该会话？其下所有消息与反馈将一并删除，不可恢复。'))) return;
  await api(`/api/conversations/${id}`, {method:'DELETE'});
  if(currentConvId === id) {
    currentConvId = null;
    document.getElementById('chat-area').innerHTML = '<div class="loading">选择或新建对话开始建模…</div>';
    // 2026-09-29：「未选择」与「已加载」同性质（零信息量常驻），不再写入状态栏
  }
  toast('对话已删除');
  loadConversations();
}

// ── 消息渲染性能（P0 修复）──
// 长会话（实测 1500+ 条）曾一次性 innerHTML 全量渲染 → 数万 DOM 节点同步插入，
// 主线程阻塞数秒（鼠标无法移动）。现改为：后端分页拉取 + rAF 分片渲染。
const MSG_PAGE_SIZE = 80;             // 每批渲染条数（首屏与"加载更早"共用）
const MSG_RENDER_CHUNK = 20;          // 单帧分片插入条数（避免长任务）
let _convMsgTotal = 0;                // 当前会话消息总数（判断是否还有更早消息）
let _oldestMsgId = null;              // 当前已渲染最早消息 id（向上翻页游标）
let _renderToken = 0;                 // 渲染代际令牌：切会话/重渲染时作废旧分片任务
// ── 2026-09-29 「贴底才跟随」滚动策略（AI 输出期间允许用户上滑查看历史）──
// 需求：AI 输出过程中用户要能滑动页面看上面的内容，此前每帧/每 token 无条件
//   `scrollTop = scrollHeight` 把视口强行拽回底部，拖动滚动条无效。
// 语义（与主流 IM/LLM 客户端一致）：
//   · 视口在底部附近（≤ STICK_EPS px）→ 视为"跟读中"，新内容到达时继续钉底；
//   · 用户主动上滑（滚轮/触摸/拖动滚动条）→ 立即脱离跟读，此后不再自动滚动；
//   · 用户回到底部附近 → 自动恢复跟读（无需额外操作）。
// 实现要点：只读几何量判断，不改写用户滚动位置；用 rAF 合批，避免每 token 强制重排。
const _STICK_EPS = 48;                 // 距底 ≤48px 视为"在底部"
let _stickFollow = true;               // 当前是否处于跟读态（切会话时复位）
let _stickRaf = 0;                     // rAF 句柄（合批用）
// UX规范§8.4（2026-10-02）：脱离跟读期间新内容到达 → 未读增量计数 + 「回到最新」浮钮。
// 内容每到达一批 +1（封顶 999）；回到底部 / 点击浮钮 / 发送消息即清零。
let _stickUnread = 0;

// 浮钮：挂在 #chat-col（聊天列）右下、输入区上方。只增不重建，按 unread 显隐。
function _stickBtnShow(){
  const col = document.getElementById('chat-col');
  if(!col) return;
  let btn = document.getElementById('jump-latest-btn');
  if(!btn){
    if(col.style.position !== 'relative') col.style.position = 'relative';
    btn = document.createElement('button');
    btn.id = 'jump-latest-btn';
    btn.type = 'button';
    btn.style.cssText = 'display:none;position:absolute;right:20px;bottom:160px;z-index:30;'
      + 'border:1px solid var(--line);background:#fff;color:var(--pri,#3478f6);border-radius:16px;'
      + 'padding:4px 14px;font-size:12px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.10);';
    btn.onclick = function(){ _stickUnread = 0; scrollChatToBottom(); };
    col.appendChild(btn);
  }
  if(_stickUnread > 0){
    btn.textContent = '⬇ 回到最新' + (_stickUnread > 1 ? ('（+' + _stickUnread + '）') : '');
    btn.style.display = 'block';
  } else {
    btn.style.display = 'none';
  }
}

function _chatAreaEl(){
  return document.getElementById('chat-area');
}
// ★ 2026-10-22（米爸反馈「打开会话/输出完成后总定位在最上面，要拖很久」）——
//   **锚定式贴底**：以「最后一条 .msg 的真实底边」为基准，而不是 `scrollHeight`。
//   根因（真实浏览器实测，勿凭印象改）：
//     `.msg` 带 `content-visibility:auto; contain-intrinsic-size:auto 132px`（P0 性能优化，
//     防长会话上万 DOM 节点同步布局）。副作用是**屏外消息只按 132px 估算**，于是
//     `scrollHeight` 严重失真 —— 实测同一会话：估算 3680px / 真实 28170px（**差 6.6 倍**）。
//     ⇒ `scrollTop = scrollHeight` 这种「钉底」钉到的是**估算出来的假底部**：
//       实测钉完之后最后一条消息仍在视口下方 3190px（视口仅 659px）= 用户完全看不见最新内容，
//       体感就是「停在了最上面 / 中间，得手动拖很久」。
//     ⇒ 锚定法实测 1 轮迭代即收敛（最后一条消息底边精确贴齐视口底）。
//   ⚠️ 保留「先粗跳到 scrollHeight」这一步：它会把底部内容**逼进视口**触发真实布局，
//      再由锚定做微校正；纯锚定在极端情况下（最后一条尚未实现、rect 高 0）会无从下手。
function _latestChatMsg(area){
  try{
    const msgs = area && area.querySelectorAll ? area.querySelectorAll('.msg') : null;
    return (msgs && msgs.length) ? msgs[msgs.length - 1] : null;
  }catch(e){ return null; }
}
// 返回 true=本次做了校正（还需再迭代一轮）；false=已贴底或无法锚定
function _anchorChatToLatest(area){
  const last = _latestChatMsg(area);
  if(!last || !last.getBoundingClientRect || !area.getBoundingClientRect) return false;
  const r = last.getBoundingClientRect();
  if(!(r.height > 0)) return false;              // 未实现（屏外被 content-visibility 跳过）→ 交给粗跳
  const box = area.getBoundingClientRect();
  const d = r.bottom - box.bottom;               // >0 底边在视口下方（看不见最新）／<0 已过底
  if(Math.abs(d) <= 0.5) return false;
  area.scrollTop += d;
  return true;
}
function _isNearBottom(area){
  if(!area) return true;
  if((area.scrollHeight - area.scrollTop - area.clientHeight) <= _STICK_EPS) return true;
  // scrollHeight 是估算值时的补判据：最后一条消息本身已在视口底附近 ⇒ 视为在底部。
  // （否则内容每长高一帧，距底就变大一次 ⇒ 被 scroll 监听误判成"用户上滑"⇒ 中途脱离跟读。）
  try{
    const last = _latestChatMsg(area);
    if(last && last.getBoundingClientRect && area.getBoundingClientRect){
      const r = last.getBoundingClientRect();
      if(r.height > 0){
        const box = area.getBoundingClientRect();
        return (r.bottom - box.bottom) >= -_STICK_EPS;
      }
    }
  }catch(e){}
  return false;
}
// 用户交互 → 脱离跟读。一次绑定，常驻监听（只读判断，成本可忽略）。
function _bindStickDetach(){
  const area = _chatAreaEl();
  if(!area || area.dataset.stickBind) return;
  area.dataset.stickBind = '1';
  const detach = ()=>{ _stickFollow = _isNearBottom(area); };
  area.addEventListener('wheel', detach, {passive:true});
  area.addEventListener('touchmove', detach, {passive:true});
  // 拖动滚动条 / 键盘翻页 / 程序性滚动后同步跟读态；回到底部 → 未读清零并隐藏浮钮
  area.addEventListener('scroll', ()=>{
    _stickFollow = _isNearBottom(area);
    if(_stickFollow){ _stickUnread = 0; _stickBtnShow(); }
  }, {passive:true});
}
// 请求一次"若在跟读态则钉底"（合批到下一帧，最多一次）
function stickBottom(force){
  const area = _chatAreaEl();
  if(!area) return;
  if(force) _stickFollow = true;
  if(!_stickFollow){
    // UX规范§8.4（2026-10-02）：脱离跟读期间新内容到达 → 未读增量 + 浮钮；绝不打扰用户当前视口。
    _stickUnread = Math.min(_stickUnread + 1, 999);
    _stickBtnShow();
    return;
  }
  if(_stickRaf) return;
  _stickRaf = requestAnimationFrame(()=>{
    _stickRaf = 0;
    const a = _chatAreaEl();
    if(!a || !_stickFollow) return;
    a.scrollTop = a.scrollHeight;
    _anchorChatToLatest(a);      // 2026-10-22：估算高度失真 ⇒ 粗跳之后再按真实元素校正一次
  });
}
// 强制滚动（用户显式动作：发送消息、切换会话、点"回到最新"）——绕过跟读态判断
function scrollChatToBottom(){
  const area = _chatAreaEl();
  if(!area) return;
  uxTrack('scroll_force', {reason: 'user'});   // §11 埋点：用户显式回底（守护指标白名单 reason）
  _stickFollow = true;
  _stickUnread = 0;      // UX规范§8.4：显式回底 = 已读到最新，清零并隐藏浮钮
  _stickBtnShow();
  area.scrollTop = area.scrollHeight;
  _anchorChatToLatest(area);
}
// 2026-09-16：打开会话后持续钉底——懒加载内容（mermaid 库首载/图片/缩略图挂载）会在
// 数秒内持续长高，固定时间补偿追不上。策略：150ms 步进钉底至超时；用户滚轮/触摸上滑立即让位。
// 2026-10-22：每步在粗跳之后补一次锚定校正（同 stickBottom 的理由：估算高度失真）。
function pinChatBottom(area, ms){
  let userInterrupted = false;
  const stop = ()=>{ userInterrupted = true; };
  area.addEventListener('wheel', stop, {once:true, passive:true});
  area.addEventListener('touchmove', stop, {once:true, passive:true});
  const start = Date.now();
  (function tick(){
    if(userInterrupted || Date.now() - start > (ms||8000)) return;
    area.scrollTop = area.scrollHeight;
    _anchorChatToLatest(area);
    setTimeout(tick, 150);
  })();
}
// ★ 2026-10-17（米爸反馈1）：同轮澄清卡合并预处理 —— msg_type='clarify' 的
// AI 消息**不再独立成气泡**，作为「确认环节」挂到其后第一条 assistant 消息的
// _clarify_prev 上（renderMessage 据此在流程时间线里渲染确认块）。
// 仅合并「后面还有 assistant」的卡；末尾挂起态（用户尚未作答）保留独立渲染，
// 作答入口由 setPendingClarify 挂在输入框上方。
// ⚠️ 对 DB 层未单轮化的旧数据（[U1][C1][U2][A2]）同样生效：U2 照常渲染，
//   C1 仍合并进 A2 ⇒ 刷新后从 4 气泡收敛为 3 气泡（AI 输出只剩 1 个）。
function _mergeClarifyTurns(msgs){
  if(!Array.isArray(msgs)) return msgs;
  const out = [];
  let pending = null;
  for(const m of msgs){
    if(m && m.role==='assistant' && m.msg_type==='clarify'){ pending = m; continue; }
    if(pending && m && m.role==='assistant'){ m._clarify_prev = pending; pending = null; }
    out.push(m);
  }
  if(pending) out.push(pending);   // 末尾挂起态：保留独立渲染（等用户作答）
  return out;
}
function renderMessagesChunked(area, msgs, done){
  const token = ++_renderToken;
  msgs = _mergeClarifyTurns(msgs);
  let i = 0;
  (function step(){
    if(token !== _renderToken) return;          // 已被新一轮渲染取代：丢弃
    const frag = document.createDocumentFragment();
    const end = Math.min(i + MSG_RENDER_CHUNK, msgs.length);
    for(; i < end; i++){
      const holder = document.createElement('div');
      holder.innerHTML = renderMessage(msgs[i]);
      while(holder.firstChild) frag.appendChild(holder.firstChild);
    }
    area.appendChild(frag);
    if(i < msgs.length) requestAnimationFrame(step);
    else if(done) done();
  })();
}
// 加载更早消息（向上翻页）：prepend 到消息区，保持视口停留位置不跳动
async function loadEarlierMessages(){
  if(!_oldestMsgId) return;
  const area = document.getElementById('chat-area');
  const btn = document.getElementById('load-earlier-btn');
  if(btn) btn.textContent = '加载中…';
  try{
    const r = await api(`/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}&before_id=${_oldestMsgId}`);
    const msgs = (r && r.messages) || [];
    if(msgs.length){
      _oldestMsgId = msgs[0].id;
      const prevHeight = area.scrollHeight;
      const _merged = _mergeClarifyTurns(msgs);   // ★ 2026-10-17：同轮澄清卡合并为确认环节
      const frag = document.createDocumentFragment();
      _merged.forEach(m=>{
        const holder = document.createElement('div');
        holder.innerHTML = renderMessage(m);
        while(holder.firstChild) frag.appendChild(holder.firstChild);
      });
      area.insertBefore(frag, area.firstChild);
      area.scrollTop += area.scrollHeight - prevHeight;   // 视口锚定：读完仍在原位置
    }
    const btn2 = document.getElementById('load-earlier-btn');
    if(btn2){
      if(!msgs.length || _oldestMsgId <= 1 || area.querySelectorAll('.msg').length >= _convMsgTotal){
        btn2.remove();                                   // 已到顶
      } else {
        btn2.textContent = `⬆ 加载更早消息（剩余 ${Math.max(0, _convMsgTotal - area.querySelectorAll('.msg').length)} 条）`;
      }
    }
  }catch(e){
    const btn3 = document.getElementById('load-earlier-btn');
    if(btn3) btn3.textContent = '⬆ 加载更早消息（重试）';
  }
}
// ── 2026-09-29 活跃流保护 ──────────────────────────────────────────
// #stream-ai 是流式现场唯一载体：handleSSE 按 id 取元素追加 token/done 渲染。
// 任何重渲染（切页回点/侧栏切换）清掉它 = 输出中断 + 现场丢失。三层防护：
//   ① 同会话重选且流式活跃 → 不重渲染（库中 assistant 未落库，重渲染只会更少）
//   ② 切到其他会话 → 元素寄存到隐藏容器（仍在 DOM，getElementById 命中，token 继续追加）
//   ③ 切回流式会话 → 寄存元素挂回消息区尾部（在分片渲染 done 回调里做，防乱序）
function _streamPark(){
  let p = document.getElementById('stream-parking');
  if(!p){
    p = document.createElement('div');
    p.id = 'stream-parking';
    p.style.display = 'none';
    document.body.appendChild(p);
  }
  return p;
}
function _liveStreamBox(){ return document.getElementById('stream-ai'); }
// 把寄存的流式现场挂回当前会话消息区（仅当它属于该会话）；挂在渲染完成后调用
function _reattachLiveStream(area, convId){
  const box = _liveStreamBox();
  if(!box || String(box.dataset.convId||'') !== String(convId)) return false;
  if(box.parentElement && box.parentElement.id === 'stream-parking'){
    area.appendChild(box);
    // 2026-09-29：切回流式会话属用户显式跳转 → 强制钉底看现场，并复位贴底跟读态
    scrollChatToBottom();
    _bindStickDetach();   // 重新绑定（消息区可能已被重渲染，监听器随旧节点丢失）
  }
  return true;
}
async function selectConv(id) {
  // 2026-09-17：切换会话前先退出「归一确认全屏」——全屏宿主挂在 <body>，会话重渲染后原锚点失效，
  // 不退出会留下一个悬空的覆盖层（07-norm.js 的 _nrToggleFullscreen 内部有锚点失效兜底，这里再显式收口一次）
  try{ if(window._nrState && window._nrState.fullscreen && typeof _nrToggleFullscreen === 'function') _nrToggleFullscreen(false); }catch(e){}
  // R3：全局侧栏——非 AI 页点击会话先切到 AI 建模页再加载（判激活态而非元素存在）
  if(!document.getElementById('pg-ai') || !document.getElementById('pg-ai').classList.contains('on')){
    go('ai');
    setTimeout(()=>{ if(document.getElementById('pg-ai').classList.contains('on')) selectConv(id); }, 400);
    return;
  }
  // 2026-09-29 活跃流保护①：流式进行中重新选中同一会话（典型：切页后切回、点侧栏任务项）
  // → 现场本来就在消息区，直接返回。此时 assistant 消息尚未落库，重渲染只会把现场换成一屏旧历史。
  if(_streaming && id === currentConvId && _liveStreamBox()){
    loadConversations();                                  // 侧栏高亮/摘要仍要刷新
    if(typeof renderGnavProjects === 'function') renderGnavProjects();
    return;
  }
  // 活跃流保护②：流式进行中切到其他会话 → 把流式现场寄存到隐藏容器（仍在 DOM，
  // handleSSE 的 getElementById 照常命中，token/done 继续处理，输出不丢）。
  // 注意在 currentConvId 被改写之前判断归属。
  const _liveBox = _liveStreamBox();
  if(_streaming && _liveBox){
    _streamPark().innerHTML = '';      // 清掉上次 done 在寄存区留下的渲染残渣（真数据在库里）
    _streamPark().appendChild(_liveBox);
  }
  // 切换前：记忆当前会话已打开的预览 tab（切回时恢复）
  if(currentConvId){
    _convTabs[currentConvId] = { tabs: _previewTabs.slice(), activeKey: _previewActiveKey };
  }
  currentConvId = id;
  _isDraft = false;   // 2026-09-04 v3：选中真实会话，退出草稿态
  _renderToken++;                       // 作废进行中的分片渲染
  svmVmStorePrune();                    // 清理旧会话缩略图视图数据缓存
  // 2026-09-04 v5：选中会话进入聊天态 → 先把居中的输入区归位到底部，避免被下方 area.innerHTML 覆盖丢失
  if(_welcomeMode) unmountWelcomeDock();
  // 2026-10-16 P0 切换慢修复：原先此处调 loadConversations()+renderGnavProjects()，
  // 二者各拉一次全量 /api/conversations 并重建整棵侧栏 DOM ⇒ 单次切换 = 2 次全量查询 + 2 次整列重建。
  // 切换不改变列表内容，改为只做「高亮态」局部更新（零网络、零整列重建）。
  _refreshConvSelection(id);
  const r = await api(`/api/conversations/${id}/messages?limit=${MSG_PAGE_SIZE}`);
  // 2026-10-16 R1：会话级主 Agent 回填 —— 用服务端库值覆盖 localStorage，
  //   保证"这个会话一直由同一个主 Agent 执行"在刷新/切换会话后也成立（不漂移）。
  //   空串=本会话从未绑定，交给下方下拉三级优先逻辑（此时会落到 localStorage/排序第一）。
  if(r && typeof r.main_agent === 'string' && r.main_agent){
    window._convMainAgent = r.main_agent;
  }
  // 2026-09-25：打开会话先恢复「待澄清」状态（在渲染消息之前）——历史澄清卡据此决定
  // 是可作答的卡还是只读摘要，输入区上方的提示条也据此显示（刷新后作答路径）。
  if(typeof setPendingClarify === 'function') setPendingClarify(r && r.pending_clarify);
  const msgs = (r && r.messages) || r || [];
  _convMsgTotal = (r && r.total) || msgs.length;
  _oldestMsgId = msgs.length ? msgs[0].id : null;
  const area = document.getElementById('chat-area');
  if(!msgs.length) {
    area.innerHTML = '<div style="text-align:center;color:var(--mut);padding:40px;">开始输入以启动 AI 建模…</div>';
    // 活跃流保护③兜底：流式现场属于本会话却拿到空消息（后端入口落库失效时）→ 现场挂回，别显示空态
    _reattachLiveStream(area, id);
  }
  else {
    area.innerHTML = msgs.length < _convMsgTotal
      ? `<div id="load-earlier-btn" class="load-earlier" onclick="loadEarlierMessages()">⬆ 加载更早消息（剩余 ${_convMsgTotal - msgs.length} 条）</div>`
      : '';
    // 分片渲染：首屏不再一次性插入全部消息（长会话曾致主线程阻塞数秒）
    // 2026-09-16：固定时间补偿追不上懒加载长高（mermaid 首载/图片/缩略图），改为持续钉底 8s，
    // 用户滚轮/触摸上滑立即让位（renderMessagesChunked 上方 pinChatBottom 定义）
    // 2026-09-29：done 回调里先挂回流式现场再钉底 —— 分片是 rAF 异步追加，
    // 若在调用点直接挂回，后续分片会排到现场之后造成消息乱序。
    // 同时复位「贴底跟读」态（新会话/重开会话从底部开始跟随）并绑定脱离监听。
    renderMessagesChunked(area, msgs, ()=>{
      _stickFollow = true;
      _stickUnread = 0; _stickBtnShow();   // UX规范§8.4：切会话 = 未读清零、隐藏浮钮
      _bindStickDetach();
      _reattachLiveStream(area, id);
      pinChatBottom(area, 8000);
    });
  }
  // 2026-09-29（用户反馈）：状态栏不再写「已加载」——零信息量却让蓝色状态条常驻不消失
  // （initChatStatusBar 有文本即显示）。状态栏只承载实质状态（意图调度/失败/停止等）。
  // const _st2 = document.getElementById('chat-conv-status');
  // if(_st2) _st2.textContent = '已加载';
  resetArtPreview();
  // P0-2 断点续跑（2026-10-03）：会话加载完成后再查可恢复批次（不阻塞首屏消息渲染）。
  // 失败静默 —— 检查点是旁路能力，任何异常都不应影响会话正常打开。
  if(typeof checkResumableRuns === 'function') checkResumableRuns(id);
  // 恢复该会话的预览状态：对应会话有已打开预览 → 展开渲染；无 → 预览区默认收起
  const saved = _convTabs[id];
  const pp = document.getElementById('preview-panel');
  if(saved && saved.tabs && saved.tabs.length){
    _previewTabs = saved.tabs.slice();
    _previewActiveKey = saved.activeKey || _previewTabs[0].key;
    const tb = _previewTabs.find(t=>t.key===_previewActiveKey) || _previewTabs[0];
    _artActive = tb.id || null;
    renderPreviewTabs();
    renderPreviewBody(tb);
    if(pp && pp.classList.contains('collapsed')) togglePreview();   // 展开预览面板
  } else {
    if(pp && !pp.classList.contains('collapsed')) togglePreview();  // 无预览内容 → 默认收起
  }
  loadArtifacts(id);   // 会话产物分栏：按当前会话加载
}

// ── V3 可折叠四栏：会话列表收起展开（图标由 CSS transform 控制方向）──
function convToggle(){
  // 2026-09-04 v2：旧 conv-sidebar 已并入 gnav-task；保留 convToggle 作废兼容（外部代码历史调用）
  toggleTaskGroup();
}
// ── 2026-09-04 v2：任务分组（gnav 内）展开/收起；状态按 mbse_task_collapsed 持久化 ──
function toggleTaskGroup(forceCollapsed){
  const el = document.getElementById('gnav-task');
  if(!el) return;
  const wasCollapsed = el.classList.contains('collapsed');
  const next = (typeof forceCollapsed === 'boolean') ? forceCollapsed : !wasCollapsed;
  el.classList.toggle('collapsed', next);
  localStorage.setItem('mbse_task_collapsed', next ? '1' : '0');
}
// ── 2026-09-04 v3：「更多」hover 右侧滑出卡片（审计日志 / 运行监控 / 报告 / AI 建模） ──
let _flyTimer = null;
function openMoreFlyout(){
  const link = document.getElementById('more-link');
  const fo = document.getElementById('more-flyout');
  if(!link || !fo) return;
  clearTimeout(_flyTimer);
  const r = link.getBoundingClientRect();
  fo.style.left = (r.right + 8) + 'px';
  fo.style.top = (r.top - 6) + 'px';
  fo.classList.add('open');
}
function hideMoreFlyout(){
  const fo = document.getElementById('more-flyout');
  if(fo){ clearTimeout(_flyTimer); fo.classList.remove('open'); }
}
function closeMoreFlyout(){
  const fo = document.getElementById('more-flyout');
  if(fo){
    clearTimeout(_flyTimer);
    _flyTimer = setTimeout(()=>{ fo.classList.remove('open'); }, 120);
  }
}
// ESC 关闭"更多"卡片 + 账号弹窗
document.addEventListener('keydown', (e) => {
  if(e.key === 'Escape'){
    hideMoreFlyout();
    closeUserMenu();
  }
});
document.addEventListener('click', (e) => {
  const fo = document.getElementById('more-flyout');
  const link = document.getElementById('more-link');
  if(fo && fo.classList.contains('open') && !fo.contains(e.target) && !(link && link.contains(e.target))){
    hideMoreFlyout();
  }
});

// 兼容旧调用（产物列表已改为浮层，不再有列折叠）

/* ══════════════════════════════════════════════════════════════════════════
   P0-2 断点续跑（2026-10-03）：会话打开时若存在"中断的编排批次"，顶部给一条
   「继续」提示条；点继续 → 走 resume 端点（SSE 事件复用既有 handleSSE）。

   为什么要做入口而不是"重发一遍原话"：重发会重跑意图识别 + planner LLM，
   得到的任务拆分与旧批次不一致 → 已完成子任务的结果无法复用（等于从头再来，
   还要再付一遍 LLM 费用）。resume 只补跑未完成部分。

   注意：本文件是 03（早于 11-pipeline.js 加载），handleSSE 在首次用户交互时
   早已可用；仍保留 typeof 防御，避免脚本加载顺序变化导致整段崩。
   ══════════════════════════════════════════════════════════════════════════ */
async function checkResumableRuns(convId){
  try{
    const area = document.getElementById('chat-area');
    if(!area || !convId) return;
    const old = document.getElementById('resumable-bar');
    if(old) old.remove();
    const r = await api(`/api/orchestration/resumable?conversation_id=${convId}&limit=3`);
    const items = (r && r.items) || [];
    if(!items.length) return;
    const it = items[0];
    // C-1：两种恢复模式对用户是**两件不同的事**，文案必须区分 ——
    //   summary_only=true  → 子任务已全部完成，只需重做汇总（不重跑任务、不重复计费）
    //   否则                → 还有子任务没做完，继续跑未完成部分
    const onlySummary = !!it.summary_only;
    const txt = onlySummary
      ? `上次编排的子任务已全部完成，但最终汇总未生成（批次 #${it.run_id}）`
      : `上次编排未完成（批次 #${it.run_id}，${it.pending} 个子任务待执行，` +
        `${Math.round((it.age_s||0)/60)} 分钟前中断）`;
    const bar = document.createElement('div');
    bar.id = 'resumable-bar';
    bar.className = 'resumable-bar' + (onlySummary ? ' rb-summary' : '');
    bar.innerHTML =
      `<span class="rb-dot"></span>` +
      `<span class="rb-txt">${txt}</span>` +
      `<button class="rb-btn" id="rb-go">${onlySummary ? '重新生成汇总' : '继续编排'}</button>` +
      `<button class="rb-btn rb-ghost" id="rb-no">忽略</button>`;
    area.insertBefore(bar, area.firstChild);
    document.getElementById('rb-no').onclick = ()=> bar.remove();
    document.getElementById('rb-go').onclick = ()=> startResumeRun(it.run_id, bar);
  }catch(e){ /* 检查点查询失败静默：不应影响会话正常加载 */ }
}

/* 单点重跑入口：预填失败/部分完成的第一个 task_key，可改。 */
function orchRerunPrompt(runId){
  const def = (window._orchRerunDefault || '');
  const inp = prompt('要重跑的子任务 task_key（多个用逗号分隔；其下游会联动重跑，其余复用）：', def);
  if(inp === null) return;
  const keys = inp.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
  if(!keys.length){ toast('未输入 task_key'); return; }
  orchRerunAndResume(runId, keys);
}

/* ★ 2026-10-20（Task #43 观测性 trace）：拉取 run 的 span 树，在按钮所在步骤体内联渲染。
   数据来自 GET /api/orchestration/runs/{run_id}/trace（归属门与 rerun 同档）。
   再点一次按钮 = 收起（toggle）；老批次无 span 时后端给 note，明说"上线前的批次"，
   不渲染一个空面板让人以为"轨迹为空 = 没执行"。 */
async function orchTracePanel(runId, btn){
  const host = btn && btn.closest('.proc-body');
  if(!host) return;
  const old = host.querySelector('.orch-trace');
  if(old){ old.remove(); return; }
  const box = document.createElement('div');
  box.className = 'orch-trace';
  box.style.cssText = 'margin-top:8px;border:1px solid var(--line,#e5e8ee);border-radius:8px;padding:8px 10px;font-size:11px;background:var(--bg,#fff);';
  box.innerHTML = '<div style="color:var(--mut);">⏳ 正在加载执行轨迹…</div>';
  host.appendChild(box);
  try{
    const r = await fetch(`/api/orchestration/runs/${runId}/trace`, {headers: streamHeaders()});
    const j = await r.json().catch(()=>({}));
    if(!r.ok){
      box.innerHTML = '<div style="color:var(--red,#d93636);">加载失败：' + esc((j && j.error) || ('HTTP ' + r.status)) + '</div>';
      return;
    }
    box.innerHTML = orchTraceHtml(j);
  }catch(e){
    box.innerHTML = '<div style="color:var(--red,#d93636);">加载失败：' + esc((e && e.message) || e) + '</div>';
  }
}

/* span 树渲染：根列表递归展开；每行 = 状态点 + 类型 + 名称 + 耗时 + 关键 meta。
   meta 只挑成本/排障相关的键（tokens/attempts/retry/score/views/source/tasks/error），
   不整块 dump JSON —— 面板是给人一眼看懂的，不是给排障链接逐字读的。 */
function orchTraceHtml(j){
  const st = j.stats || {};
  if(!(j.spans || []).length){
    return '<div style="color:var(--mut);">' + esc(st.note || '该批次暂无轨迹数据') + '</div>';
  }
  // P1 replanning（2026-10-21）：replan = 重规划决策记录节点（含 action=none 的
  // "为什么不重画"；level/action/reason 在行内 meta 区展示，见下方 row 的通用渲染）
  const kindMeta = {run:'🕸 整轮', plan:'🧭 规划', task:'🤖 子任务', summary:'📝 汇总', reflection:'🔍 评审', views:'🧩 视图', replan:'🔁 重规划'};
  const head = '<div style="display:flex;gap:10px;flex-wrap:wrap;color:var(--mut);margin-bottom:6px;">'
    + '<span>⏱ 合计 <b style="color:var(--ink,#223);">' + ((st.total_ms || 0)/1000).toFixed(1) + 's</b></span>'
    + '<span>🌳 ' + (j.span_count || 0) + ' 个节点</span>'
    + Object.entries(st.by_kind || {}).map(([k, v]) =>
        '<span title="' + esc(k) + ' 累计耗时">' + esc(kindMeta[k] || k) + ' ' + (v.ms/1000).toFixed(1) + 's'
        + (v.tokens ? ' · ' + Number(v.tokens).toLocaleString() + ' tok' : '') + '</span>').join('')
    + '</div>';
  const row = (s, depth) => {
    const m = s.meta || {};
    const ok = s.status === 'ok';
    const dot = ok ? '<span style="color:var(--grn,#3b6d11);">●</span>' : '<span style="color:var(--red,#d93636);">●</span>';
    const lat = s.latency_ms ? ' · ' + (s.latency_ms/1000).toFixed(1) + 's' : '';
    const bits = [];
    if(m.tokens) bits.push(Number(m.tokens).toLocaleString() + ' tok');
    if(m.attempts && m.attempts > 1) bits.push(m.attempts + ' 次尝试');
    if(m.retry_count) bits.push('重试 ' + m.retry_count);
    if(m.score !== undefined && m.score !== null) bits.push('评分 ' + m.score);
    if(m.views) bits.push(m.views + ' 个视图');
    if(m.source) bits.push('来源:' + m.source);
    if(m.tasks !== undefined) bits.push(m.tasks + ' 个任务');
    // P1 replanning：重规划决策行 —— level/action/reason（action=none 也展示：
    // "为什么不重画"与"为什么重画"同等重要；reason 截断展示，全文在 title）
    if(m.level) bits.push('<b>' + esc(m.level) + '/' + esc(m.action || 'none') + '</b>');
    if(m.reason) bits.push('<span title="' + esc(m.reason) + '">' + esc(String(m.reason).slice(0, 50)) + (String(m.reason).length > 50 ? '…' : '') + '</span>');
    if(m.error) bits.push('<span style="color:var(--red,#d93636);">' + esc(String(m.error).slice(0, 80)) + '</span>');
    return '<div style="display:flex;gap:6px;align-items:baseline;padding:1px 0;padding-left:' + (depth*14) + 'px;flex-wrap:wrap;">'
      + dot
      + '<span style="color:var(--mut);min-width:56px;">' + esc(kindMeta[s.kind] || s.kind || '?') + '</span>'
      + '<b>' + esc(s.name || '') + '</b>' + lat
      + (bits.length ? '<span style="color:var(--mut);"> · ' + bits.join(' · ') + '</span>' : '')
      + '</div>'
      + (s.children || []).map(c => row(c, depth + 1)).join('');
  };
  return head + (j.spans || []).map(s => row(s, 0)).join('');
}

/* ★ 2026-10-20（单点重跑）：rerun 清指定任务（+传递下游）的状态 → 自动走既有 resume。
   runId 来自编排卡 card_data.run_id；taskKeys 为要重跑的 task_key 数组。 */
async function orchRerunAndResume(runId, taskKeys){
  try{
    const resp = await fetch(`/api/orchestration/runs/${runId}/rerun`, {
      method:'POST', headers: streamHeaders(),
      body: JSON.stringify({task_keys: taskKeys, include_downstream: true})
    });
    const j = await resp.json().catch(()=>({}));
    if(!resp.ok || j.ok === false){
      toast((j && j.error) || ('重跑预备失败（HTTP ' + resp.status + '）'), 'error');
      return false;
    }
    const keys = (j.rerun_keys || []).join('、');
    toast(`已标记重跑：${keys}${(j.downstream_added||[]).length ? '（含下游联动）' : ''}，开始执行…`);
    await startResumeRun(runId, null);
    return true;
  }catch(e){
    toast('重跑时出错：' + (e && e.message || e), 'error');
    return false;
  }
}

async function startResumeRun(runId, bar, includeFailed){
  if(bar){ bar.querySelector('.rb-btn').disabled = true; bar.querySelector('.rb-txt').textContent = '正在恢复编排…'; }
  try{
    const resp = await fetch(`/api/orchestration/runs/${runId}/resume` +
      (includeFailed ? '?include_failed=true' : ''), {
      method:'POST',
      headers: streamHeaders()
    });
    if(!resp.ok){
      // ★ B5-5：失败**不许静默移除提示条**。
      //改前是 `toast(msg) + bar.remove()` ⇒ 提示条一消失，"可继续"的假象也没了，
      //   用户看到的就是「点了一下什么都没发生」（米爸 2026-10-14 报的正是这个）。
      //   现在：把原因**留在横幅上**（不靠一闪而过的 toast），并按错误类型给出下一步动作。
      let msg = `恢复失败（HTTP ${resp.status}）`;
      let reason = '';
      try{
        const j = await resp.json();
        if(j && j.error) msg = j.error;
        if(j && j.detail) msg = j.detail;
        if(j && j.reason) reason = j.reason;
      }catch(e){}
      if(typeof toast === 'function') toast(msg, 'error');
      if(bar){
        bar.classList.add('rb-error');
        const t = bar.querySelector('.rb-txt');
        if(t) t.textContent = msg;
        // `nothing_to_do` 是**可自愈**的一类：批次里剩下的任务已被孤儿回收标成 failed，
        // 而 resume 默认 include_failed=false 会把它们一律跳过 ⇒ 必须让用户能显式重跑。
        if(reason === 'nothing_to_do' || resp.status === 409){
          const again = document.createElement('button');
          again.className = 'rb-btn';
          again.textContent = '连失败任务一起重跑';
          again.onclick = ()=> startResumeRun(runId, bar, true);
          bar.insertBefore(again, bar.querySelector('.rb-ghost'));
        }
        const btn = bar.querySelector('.rb-btn');
        if(btn) btn.disabled = false;
      }
      return;
    }
    if(bar) bar.remove();
    const reader = resp.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buf += dec.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) >= 0){
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if(frame.trim() && !frame.startsWith(':')){
          if(typeof handleSSE === 'function') handleSSE(frame);
        }
      }
    }
  }catch(e){
    if(typeof toast === 'function') toast('恢复编排时出错：' + (e && e.message || e), 'error');
    if(bar) bar.remove();
  }
}
