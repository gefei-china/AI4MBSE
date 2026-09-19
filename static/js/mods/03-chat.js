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
    admin:     {label:'系统管理员', entries:[['👥 用户与权限','users'],['🛡 审计日志','audit'],['📈 运行监控','ops'],['✅ 审批管理','approval']]},
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
  const _list = document.getElementById('task-conv-list') || document.getElementById('conv-list');
  if(_list && !_list.children.length){
    _list.innerHTML = Array.from({length:5}).map(()=>`
      <div class="task-it skel" style="padding:7px 10px;">
        <div class="sk-line" style="width:80%;"></div>
        <div class="sk-line sm" style="width:50%;margin-top:6px;opacity:.6;"></div>
      </div>`).join('');
  }
  const convs = await api('/api/conversations');
  const _isArr = Array.isArray(convs);
  // 2026-09-04 v2：会话列表主渲染目标 = gnav 内的 #task-conv-list；旧 .conv 容器若被其它 JS 引用，仍兼容渲染
  const list = _list || document.getElementById('task-conv-list') || document.getElementById('conv-list');
  const cnt = document.getElementById('task-count');
  if(cnt) {
    const n = _isArr ? convs.length : 0;
    cnt.textContent = String(n);
    cnt.classList.toggle('zero', n === 0);
  }
  if(!list) return;
  if(!_isArr || !convs.length) {
    // 2026-09-06 P0-4：任务列表空态引导 —— 点新建任务开始（而不是冷冰冰一行字）
    // 2026-09-06 P1-4：用 SVG sprite 的 empty-inbox 图标 + emoji 回退
    list.innerHTML = `
      <div class="task-empty">
        <div class="te-ic">
          <svg width="34" height="34" aria-hidden="true" style="color:var(--color-primary);"><use href="#ic-empty-inbox"/></svg>
        </div>
        <div class="te-title">暂无任务</div>
        <div class="te-sub">点击「＋ 新建任务」<br>或下方快捷入口</div>
        <button class="te-go" onclick="go('ai');setTimeout(newTask,200);"><svg width="12" height="12" aria-hidden="true" style="color:currentColor;margin-right:4px;"><use href="#ic-plus"/></svg>新建任务</button>
      </div>`;
    // 2026-09-04 v4：AI 页且无会话 → 中栏展示 WorkBuddy 风格欢迎初始化屏（非草稿态）
    const onAI = document.getElementById('pg-ai') && document.getElementById('pg-ai').classList.contains('on');
    if(onAI && !currentConvId && !_isDraft) renderChatWelcome();
    return;
  }
  list.innerHTML = convs.map(c=>`
    <div class="task-it ${c.id===currentConvId?'on':''}" data-id="${c.id}" data-intent="${escA(c.intent||'')}" data-updated="${escA(c.updated_at||'')}" data-msg="${c.msg_count||0}"
      onclick="selectConv(${c.id})" onmouseenter="showTaskTip(this)" onmouseleave="hideTaskTip()">
      <div class="ti-row">
        <span class="ti-tt" title="${esc(c.title)}">${esc(c.title)}</span>
        <span class="ti-ops" onclick="event.stopPropagation()">
          <button title="重命名" onclick="renameConv(${c.id})">✏</button>
          <button title="删除" onclick="deleteConv(${c.id})">🗑</button>
        </span>
      </div>
    </div>`).join('');
  // 2026-09-04 v6：初始进入 AI 建模页一律展示「画布中部欢迎屏」（会话框居中），不自动选中会话。
  // 用户从左侧任务列表手动点击某条会话时，才通过 selectConv 进入聊天态。
  // 注：v4 曾要求"默认选中首条会话"，后 v5 调整为"初始中栏=欢迎屏布局"，故以 v6 为准（仅当用户明确点选才进会话）。
  const onAI = document.getElementById('pg-ai') && document.getElementById('pg-ai').classList.contains('on');
  if(onAI && !currentConvId && !_isDraft){
    renderChatWelcome();
  }
}
// ── 2026-09-04 v3：会话条目 hover 信息卡（名称/类型/更新时间/内容摘要）──
let _taskTipTimer = null;
let _taskTipId = 0;
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
    <div class="tt-row">🗂 本地任务<span class="tt-tag">${esc(intent || '建模会话')}</span></div>
    ${updated ? `<div class="tt-row">🕒 更新于 <span style="font-variant-numeric:tabular-nums;">${esc(fmtLocalTime(updated))}</span></div>` : ''}
    <div class="tt-row">💬 ${msg} 条消息</div>
    <div class="tt-sum" id="tt-sum">正在加载摘要…</div>`;
  const r = el.getBoundingClientRect();
  tip.style.left = (r.right + 10) + 'px';
  tip.style.top = (r.top - 6) + 'px';
  tip.style.display = 'block';
  try{
    const mr = await api(`/api/conversations/${id}/messages?limit=1`);
    if(_taskTipId !== id) return;   // 已切换到其它会话，忽略过期结果
    const msgs = Array.isArray(mr) ? mr : (mr && Array.isArray(mr.messages) ? mr.messages : []);
    const last = msgs[msgs.length-1];
    const sumEl = document.getElementById('tt-sum');
    if(sumEl){
      const txt = last ? String(last.content||last.text||'').replace(/[#*`>\[\](){}\r\n]/g,' ').trim() : '';
      sumEl.textContent = txt ? (txt.length>140 ? txt.slice(0,140)+'…' : txt) : '（暂无消息内容）';
    }
  }catch(e){
    if(_taskTipId !== id) return;
    const sumEl = document.getElementById('tt-sum');
    if(sumEl) sumEl.textContent = '（摘要加载失败）';
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
async function newTask() {
  // 2026-09-04 v3：新建任务 = 进入「草稿态」，不立即创建会话。
  // 首次通过 sendChat 提交正文时才 POST 创建（避免左侧列表出现空会话）；
  // 输入内容暂存到 mbse_draft_input，下次新建任务时恢复到输入框。
  currentConvId = null;
  _isDraft = true;
  // 2026-09-04 v4：中栏展示 WorkBuddy 风格初始欢迎屏（主智能选项 + 快速模块）
  renderChatWelcome({draft:true});
  const input = document.getElementById('chat-input');
  if(input){
    let draft = '';
    try{ draft = localStorage.getItem('mbse_draft_input') || ''; }catch(e){}
    input.value = draft;
    input.placeholder = '输入建模需求或问题（@ 引用会话文件 · # 引用知识库文件 · 行首 / 选择技能）';
    setTimeout(()=>{ try{ input.focus(); }catch(e){} }, 60);
  }
  const status = document.getElementById('chat-status');
  if(status) status.textContent = '草稿模式：输入内容并发送后创建会话';
  toggleTaskGroup(false);   // 展开分组，便于看到即将创建的任务
  go('ai');
  toast('新建任务：草稿模式，发送后入列表');
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
    const _st = document.getElementById('chat-conv-status');
    if(_st) _st.textContent = '未选择';
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
// 分片渲染：把消息数组按 chunk 逐帧插入（rAF），期间不阻塞交互
// 2026-09-16：打开会话后持续钉底——懒加载内容（mermaid 库首载/图片/缩略图挂载）会在
// 数秒内持续长高，固定时间补偿追不上。策略：150ms 步进钉底至超时；用户滚轮/触摸上滑立即让位。
function pinChatBottom(area, ms){
  let userInterrupted = false;
  const stop = ()=>{ userInterrupted = true; };
  area.addEventListener('wheel', stop, {once:true, passive:true});
  area.addEventListener('touchmove', stop, {once:true, passive:true});
  const start = Date.now();
  (function tick(){
    if(userInterrupted || Date.now() - start > (ms||8000)) return;
    area.scrollTop = area.scrollHeight;
    setTimeout(tick, 150);
  })();
}
function renderMessagesChunked(area, msgs, done){
  const token = ++_renderToken;
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
      const frag = document.createDocumentFragment();
      msgs.forEach(m=>{
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
  loadConversations();
  const r = await api(`/api/conversations/${id}/messages?limit=${MSG_PAGE_SIZE}`);
  const msgs = (r && r.messages) || r || [];
  _convMsgTotal = (r && r.total) || msgs.length;
  _oldestMsgId = msgs.length ? msgs[0].id : null;
  const area = document.getElementById('chat-area');
  if(!msgs.length) { area.innerHTML = '<div style="text-align:center;color:var(--mut);padding:40px;">开始输入以启动 AI 建模…</div>'; }
  else {
    area.innerHTML = msgs.length < _convMsgTotal
      ? `<div id="load-earlier-btn" class="load-earlier" onclick="loadEarlierMessages()">⬆ 加载更早消息（剩余 ${_convMsgTotal - msgs.length} 条）</div>`
      : '';
    // 分片渲染：首屏不再一次性插入全部消息（长会话曾致主线程阻塞数秒）
    // 2026-09-16：固定时间补偿追不上懒加载长高（mermaid 首载/图片/缩略图），改为持续钉底 8s，
    // 用户滚轮/触摸上滑立即让位（renderMessagesChunked 上方 pinChatBottom 定义）
    renderMessagesChunked(area, msgs, ()=>{ pinChatBottom(area, 8000); });
  }
  const _st2 = document.getElementById('chat-conv-status');
  if(_st2) _st2.textContent = '已加载';
  resetArtPreview();
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
