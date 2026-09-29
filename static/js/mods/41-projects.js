/* 左侧导航「项目」组 + 项目创建/编辑弹窗（2026-09-24 v2）
 *
 * 语义（用户拍板）：**项目 = 任务的容器**，每个项目声明「数据来源」——
 *   · 💻 本地：绑定本机工作空间文件夹（「选择文件夹…」经后端原生对话框写绝对路径）；
 *   · ☁ 远端：绑定 SSH 连接（显示名称 / 主机名 / SSH 端口（可选） / 无身份验证 | 身份文件 / 身份文件路径）。
 * v2 变更：原「建模工具对接」（智源 / MagicDraw 绑定卡片）**已从本弹窗移除**，由数据来源二选一取代。
 *   数据层未删列：projects.tool_binding 仍可读（14-sysml.js 的写回/拉取按绑定路由逻辑不动）。
 *
 * 项目行操作：＋ 新建任务 / ✏ 编辑名称 / 🗑 移除；行左侧 ▸ 展开该项目任务列表，
 *   任务条目操作与原任务列表**完全一致**（✏ 重命名 / 🗑 删除，直接复用 03-chat.js 的
 *   renameConv / deleteConv，条目 DOM 与 class 也复用 .task-it → 样式与交互同一套）。
 * 项目行主体仍保留原语义：点击 = 切换当前工程（宪法注入/图谱隔离随其切换）。
 *
 * 全局作用域（非 module），内联 onclick 依赖全局函数名。
 */

// ── 弹窗表单：创建 / 编辑共用一份 HTML（唯一来源，避免两处漂移）──
function zpProjectFormHTML(mode){
  const isEdit = mode === 'edit';
  return `<h3>${isEdit ? '🗂 编辑项目' : '🗂 创建项目'}</h3>
  <div class="form-section">
    <div class="form-section-head"><span class="fs-icon">📁</span><span class="fs-title">基础信息</span></div>
    <div class="form-row"><label>项目名称 <span class="req">*</span></label>
      <input id="cp-name" placeholder="如：星网宽带通信系统" style="padding-left:10px;background-image:none;"></div>
  </div>
  <div class="form-section">
    <div class="form-section-head"><span class="fs-icon">🗄</span><span class="fs-title">数据来源
      <span class="fs-hint">（本地 = 本机工作空间文件夹；远端 = SSH 连接）</span></span></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:2px 0 6px;">
      <div class="cp-src-card on" id="cp-src-local" onclick="zpSetSource('local')">
        <div class="cp-src-title">💻 本地</div>
        <small>绑定本机工作空间文件夹，模型数据在本系统内。</small>
      </div>
      <div class="cp-src-card" id="cp-src-remote" onclick="zpSetSource('remote')">
        <div class="cp-src-title">☁ 远端</div>
        <small>SSH 连接远端主机，可无身份验证或指定身份文件。</small>
      </div>
    </div>
    <div id="cp-local-box">
      <div class="form-row"><label>本地工作空间 <span class="req">*</span>
        <span class="info-tip" title="项目根目录：本机绝对路径，由「选择文件夹…」的原生对话框写入，也可手动填写">ⓘ</span></label>
        <div style="display:flex;gap:6px;">
          <input id="cp-workspace" placeholder="如：D:\\Work\\StarNet" style="flex:1;font-size:11.5px;">
          <button class="btn ghost sm" style="flex:none;white-space:nowrap;" onclick="pickProjectFolder()">选择文件夹…</button>
        </div>
      </div>
    </div>
    <div id="cp-remote-box" style="display:none;">
      <div class="ssh-field"><label class="ssh-lb">显示名称</label>
        <div class="ssh-in"><span class="ssh-ic">🌐</span>
          <input id="cp-rm-name" placeholder="如：建模工作站"></div></div>
      <div class="ssh-field"><label class="ssh-lb">主机名</label>
        <div class="ssh-in"><input id="cp-rm-host" placeholder="host.com 或 user@host.com"></div></div>
      <div class="ssh-field"><label class="ssh-lb">SSH 端口 <span class="ssh-opt">（可选）</span></label>
        <div class="ssh-in"><input id="cp-rm-port" placeholder="22"></div></div>
      <div class="ssh-seg">
        <div class="ssh-seg-it on" id="cp-auth-none" onclick="zpSetAuth('none')">无身份验证</div>
        <div class="ssh-seg-it" id="cp-auth-key" onclick="zpSetAuth('key')">身份文件</div>
      </div>
      <div class="ssh-field" id="cp-rm-key-row" style="display:none;"><label class="ssh-lb">身份文件路径</label>
        <div class="ssh-in"><input id="cp-rm-key" placeholder="如：C:\\Users\\me\\.ssh\\id_rsa"></div></div>
    </div>
  </div>
  <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button>
    <button class="btn" id="cp-go" onclick="submitProjectForm()" style="background:var(--color-primary);color:#fff;">${isEdit ? '保存' : '创建项目'}</button></div>`;
}
MODAL_FORMS.createProject = zpProjectFormHTML('create');
MODAL_FORMS.editProject = zpProjectFormHTML('edit');

let _zpSource = 'local';      // 弹窗当前数据来源：local | remote
let _zpAuth = 'none';         // 远端认证方式：none=无身份验证 | key=身份文件
let _zpEditingId = '';        // 非空 = 编辑模式（指向 projects.id）
let _zpProjects = [];         // 项目清单缓存 [{...}]（编辑弹窗回填 / 行渲染共用）
let _zpOpen = new Set();      // 已展开的项目 id 集合（仅本次会话内记忆）
let _zpAutoExpanded = false;  // 首屏是否已自动展开「当前工程」——只自动一次，之后完全听用户的

// ── 页面级「当前工程」（多工程 P0-2，2026-09-28）──
// 此前「当前工程」= 后端 settings.default_project_id（**全局单行**）：两个标签页同时开着时，
// 后切换工程的一方会把它覆盖，先开的那个标签页再新建任务就**静默错归属**到别人的工程下。
// 现改为**页面级状态**：本页一旦确定当前工程，其它标签页的切换不再影响本页。
// `undefined` = 本页尚未初始化（此时才用平台默认值初始化一次）；`''` = 本页明确不关联工程。
function curProjectId(){ return window._curProjectId || ''; }
function setCurProjectId(pid){ window._curProjectId = pid || ''; }

// ── 从建模工具打开：URL 参数 → 反查本地工程（多工程 P0-1）──
// 建模工具（智源 / MagicDraw）打开 AI 平台时带 ?tool=zhiyuan&ref=<工程标识>（或 ?project_id=xxx）。
// 此前前端**完全不读** location.search，工具侧上下文接不住，只能打开首页后人工记得切工程。
async function applyProjectFromUrl(){
  let q = {};
  try{ q = Object.fromEntries(new URLSearchParams(location.search||'')); }catch(e){ q = {}; }
  const tool = (q.tool||q.tool_name||'').trim();
  const ref  = (q.ref||q.tool_ref||q.vc||'').trim();
  const pid  = (q.project_id||q.pid||'').trim();
  if(!tool && !ref && !pid) return null;
  try{
    const qs = new URLSearchParams();
    if(pid) qs.set('project_id', pid);
    if(tool) qs.set('tool', tool);
    if(ref) qs.set('ref', ref);
    const r = await api('/api/projects/resolve?' + qs.toString());
    if(r && r.matched && r.project_id){
      setCurProjectId(r.project_id);
      toast('已定位工程：' + ((r.project && r.project.name) || r.project_id), 4000);
      return r;
    }
    // 未命中：不猜、不默认落到某个工程（「未匹配到就空着」政策），只告知用户去绑定。
    const why = (r && r.reason === 'ref_mismatch')
      ? '该工具下已绑定其它工程，未找到本工程对应的本地项目'
      : '该工具侧工程尚未绑定本地项目';
    toast('未能定位工程（' + why + '）：请在左侧「项目」中绑定后再试', 5000);
    return r;
  }catch(e){ return null; }
}

// ── 把会话归入项目（收敛入口）：无工程会话 → 归属项目 ──
async function assignConvToProject(convId, pid){
  try{
    const r = await api(`/api/conversations/${convId}/project`,
                        {method:'POST', body: JSON.stringify({project_id: pid||''})});
    if(r && r.error){ toast('归入失败：' + r.error); return false; }
    toast(pid ? '已归入当前工程' : '已解除工程归属');
    if(typeof loadConversations === 'function') loadConversations();
    if(typeof renderGnavProjects === 'function') renderGnavProjects();
    if(typeof loadCurrentProject === 'function') loadCurrentProject();
    return true;
  }catch(e){ toast('归入失败：' + (e.message||'')); return false; }
}

// ── 数据来源解析：projects 行 → {source, workspace, remote:{...}}（老库无列值按本地兜底）──
function zpParseSource(p){
  let r = null;
  try{ r = p && p.remote ? JSON.parse(p.remote) : null; }catch(e){ r = null; }
  r = r || {};
  const source = ((p && p.source) || '').trim().toLowerCase() || 'local';
  return {
    source: source === 'remote' ? 'remote' : 'local',
    workspace: ((p && p.workspace) || '').trim(),
    remote: {
      display_name: r.display_name || '',
      host: r.host || '',
      port: r.port == null ? '' : String(r.port),
      auth_mode: r.auth_mode === 'key' ? 'key' : 'none',
      identity_file: r.identity_file || '',
    },
  };
}

// ── 左侧导航项目组渲染 ──
async function renderGnavProjects(){
  const el = document.getElementById('gnav-project-list');
  if(!el) return;
  try{
    const [projs, def, convs] = await Promise.all([
      api('/api/projects'), api('/api/projects/default'), api('/api/conversations')]);
    const list = Array.isArray(projs) ? projs : [];
    const convList = Array.isArray(convs) ? convs : [];
    _zpProjects = list;
    // 本页首次确定「当前工程」：以平台默认值为初值（此后只由本页切换/URL 反查改变，
    // 其它标签页改平台默认值不再影响本页 —— 多工程并发不串归属的关键）。
    if(window._curProjectId === undefined) setCurProjectId((def && def.id) || '');
    const defId = curProjectId();
    // 首次渲染自动展开一个项目：项目下的任务**只在项目分组内展示**（下方「任务」列表
    // 只留未分组），若默认全收起，用户会以为任务不见了。优先展开「当前工程」，未设置则展开首个
    // （纯视图态，不改任何数据）。只自动一次，之后完全听用户的收起/展开。
    if(!_zpAutoExpanded){
      _zpAutoExpanded = true;
      const auto = defId || (list[0] && list[0].id) || '';
      if(auto) _zpOpen.add(String(auto));
    }
    const cnt = document.getElementById('proj-count');
    if(cnt){ cnt.textContent = String(list.length); cnt.classList.toggle('zero', list.length === 0); }
    el.innerHTML = list.length
      ? list.map(p=>_zpProjectRow(p, defId, convList)).join('')
      : '<div class="gp-empty">暂无项目 — 点右上 ＋ 创建</div>';
  }catch(e){
    el.innerHTML = '<div class="gp-empty">项目加载失败</div>';
  }
}

function _zpProjectRow(p, defId, convs){
  const pid = String(p.id || '');
  const src = zpParseSource(p);
  const open = _zpOpen.has(pid);
  const tasks = convs.filter(c=>String(c.project_id || '') === pid);
  const srcText = src.source === 'remote'
    ? ('☁ 远端 · ' + (src.remote.display_name || src.remote.host || '未命名连接')
       + (src.remote.host ? `（${src.remote.host}${src.remote.port ? ':' + src.remote.port : ''}）` : '')
       + (src.remote.auth_mode === 'key' ? ' · 身份文件' : ' · 无身份验证'))
    : ('📁 本地 · ' + (src.workspace || '未指定工作空间'));
  // 数据来源不再单独占一行展示（2026-09-25 用户反馈：项目名下的「📁 本地 · 路径」副行去掉），
  // 只保留在项目行的 hover 提示里（title）+ 编辑弹窗里可查可改，信息不丢。
  const rows = tasks.map(c=>`
      <div class="task-it ${c.id === currentConvId ? 'on' : ''}" data-id="${c.id}"
        data-intent="${escA(c.intent||'')}" data-updated="${escA(c.updated_at||'')}" data-msg="${c.msg_count||0}"
        onclick="selectConv(${c.id})" onmouseenter="showTaskTip(this)" onmouseleave="hideTaskTip()">
        <div class="ti-row">
          <span class="ti-tt" title="${esc(c.title)}">${esc(c.title)}</span>
          <span class="ti-ops" onclick="event.stopPropagation()">
            <button title="重命名" onclick="prjRenameTask(${c.id})">✏</button>
            <button title="删除" onclick="prjDeleteTask(${c.id})">🗑</button>
          </span>
        </div>
      </div>`).join('');
  return `
  <div class="gp-blk ${pid === defId ? 'cur' : ''}">
    <div class="gp-it" onclick="switchProject('${escA(pid)}','${escA(p.name || '')}')"
      title="切换当前工程：${escA(p.name || pid)}\n${escA(srcText)}">
      <span class="gp-arr" onclick="event.stopPropagation();prjToggle('${escA(pid)}')"
        title="${open ? '收起任务列表' : '展开任务列表'}">${open ? '▾' : '▸'}</span>
      <span class="gp-name">${esc(p.name || pid)}</span>
      <span class="gp-cnt">${tasks.length}</span>
      <span class="gp-ops" onclick="event.stopPropagation()">
        <button title="在此项目新建任务" onclick="prjNewTask('${escA(pid)}')">＋</button>
        <button title="编辑名称 / 数据来源" onclick="openEditProjectModal('${escA(pid)}')">✏</button>
        <button title="移除项目" onclick="prjDeleteProject('${escA(pid)}')">🗑</button>
      </span>
    </div>
    ${open ? `<div class="gnav-task-body gp-tasks">${rows || '<div class="gp-empty2">该项目暂无任务 — 点行内 ＋ 新建</div>'}</div>` : ''}
  </div>`;
}

function prjToggle(pid){
  const k = String(pid);
  if(_zpOpen.has(k)) _zpOpen.delete(k); else _zpOpen.add(k);
  renderGnavProjects();
}
function toggleProjGroup(){
  document.getElementById('gnav-project')?.classList.toggle('collapsed');
}

// ── 切换当前工程（页面级 + 平台默认；宪法注入/图谱隔离随其切换）──
// 2026-09-28 多工程 P0-2：**先落页面级**（本页新建任务的归属依据，不受其它标签页影响），
// 再写平台默认（供新打开的标签页初始化用）。后端已不再拿它做会话归属的隐式回落，
// 故即使被其它标签页覆盖，本页已建的会话也不会串。
async function switchProject(pid, name){
  try{
    await api('/api/projects/default', {method:'POST', body: JSON.stringify({project_id: pid})});
    setCurProjectId(pid);
    toast('当前工程已切换：' + (name || pid));
    renderGnavProjects();
    if(typeof loadCurrentProject === 'function') loadCurrentProject();
  }catch(e){ toast('切换失败：' + (e.message||'')); }
}

// ── 项目行操作：新建任务 / 编辑 / 移除 ──
function prjNewTask(pid){
  const p = _zpProjects.find(x=>String(x.id) === String(pid));
  // 展开该项目：任务只在项目分组内展示，不展开的话用户看不到它落到哪
  _zpOpen.add(String(pid));
  // 2026-09-28 多工程 P0-2：在某项目下点 ＋ 即**进入该项目上下文**（页面级当前工程），
  // 否则会出现「任务建在项目 A、顶栏与后续新建却仍指向项目 B」的分裂状态。
  setCurProjectId(pid);
  renderGnavProjects();
  if(typeof loadCurrentProject === 'function') loadCurrentProject();
  newTask(pid, (p && p.name) || pid);
}
async function prjDeleteProject(pid){
  const p = _zpProjects.find(x=>String(x.id) === String(pid));
  const name = (p && p.name) || pid;
  let n = 0;
  try{ const r = await api(`/api/projects/${encodeURIComponent(pid)}/task-count`); n = (r && r.tasks) || 0; }catch(e){}
  // 说明：本仓库无 markdown 解析器，对话框按原文渲染，故此处不用 ** 强调（否则显示成星号）
  const msg = `确认移除项目「${name}」？\n\n`
    + (n > 0 ? `该项目下 ${n} 个任务不会删除，会解绑到左侧「任务」列表继续保留。`
             : '该项目下暂无任务。');
  if(!(await confirmDialog(msg, {title:'移除项目', okText:'移除'}))) return;
  let r = null;
  try{ r = await api(`/api/projects/${encodeURIComponent(pid)}`, {method:'DELETE'}); }
  catch(e){ r = {error: (e.message||'')}; }
  if(!r || r.error){ toast('移除失败：' + ((r && r.error) || '')); return; }
  _zpOpen.delete(String(pid));
  toast(`已移除项目「${name}」` + (r.detached_tasks ? `（解绑任务 ${r.detached_tasks} 个）` : '')
        + (r.cleared_default ? '｜该项目原为当前工程，请在上方另行指定' : ''), 4000);
  renderGnavProjects();
  loadConversations();   // 解绑的任务要立刻出现在「任务」组
  if(r.cleared_default && typeof loadCurrentProject === 'function') loadCurrentProject();
}
// 项目内任务操作：与原任务列表一致（复用 03-chat.js 实现，仅追加项目组刷新）
async function prjRenameTask(id){ await renameConv(id); renderGnavProjects(); }
async function prjDeleteTask(id){ await deleteConv(id); renderGnavProjects(); }

// ── 弹窗：创建 / 编辑 ──
function openCreateProjectModal(){
  _zpEditingId = '';
  _zpSource = 'local'; _zpAuth = 'none';
  showModal('createProject');
  zpSetSource('local'); zpSetAuth('none');
  const nameEl = document.getElementById('cp-name');
  if(nameEl) setTimeout(()=>nameEl.focus(), 60);
}
function openEditProjectModal(pid){
  const p = _zpProjects.find(x=>String(x.id) === String(pid));
  if(!p){ toast('项目未加载，请刷新后重试'); return; }
  _zpEditingId = String(pid);
  showModal('editProject');
  const src = zpParseSource(p);
  const nameEl = document.getElementById('cp-name');
  if(nameEl) nameEl.value = p.name || '';
  const wsEl = document.getElementById('cp-workspace');
  if(wsEl) wsEl.value = src.workspace || '';
  const set = (id, v)=>{ const el = document.getElementById(id); if(el) el.value = v; };
  set('cp-rm-name', src.remote.display_name);
  set('cp-rm-host', src.remote.host);
  set('cp-rm-port', src.remote.port);
  set('cp-rm-key', src.remote.identity_file);
  zpSetSource(src.source);
  zpSetAuth(src.remote.auth_mode);
}
function zpSetSource(src){
  _zpSource = src === 'remote' ? 'remote' : 'local';
  document.getElementById('cp-src-local')?.classList.toggle('on', _zpSource === 'local');
  document.getElementById('cp-src-remote')?.classList.toggle('on', _zpSource === 'remote');
  const lbox = document.getElementById('cp-local-box');
  const rbox = document.getElementById('cp-remote-box');
  if(lbox) lbox.style.display = _zpSource === 'local' ? '' : 'none';
  if(rbox) rbox.style.display = _zpSource === 'remote' ? '' : 'none';
}
function zpSetAuth(mode){
  _zpAuth = mode === 'key' ? 'key' : 'none';
  document.getElementById('cp-auth-none')?.classList.toggle('on', _zpAuth === 'none');
  document.getElementById('cp-auth-key')?.classList.toggle('on', _zpAuth === 'key');
  const row = document.getElementById('cp-rm-key-row');
  if(row) row.style.display = _zpAuth === 'key' ? '' : 'none';
}
async function pickProjectFolder(){
  const wsEl = document.getElementById('cp-workspace');
  const initial = (wsEl && wsEl.value || '').trim();
  let r = null;
  try{ r = await api('/api/projects/pick-folder', {method:'POST', body: JSON.stringify({initial})}); }
  catch(e){ r = {ok:false, error:(e.message||'')}; }
  if(r && r.ok && r.path){
    if(wsEl) wsEl.value = r.path;
    toast('已选择工作空间：' + r.path);
    return;
  }
  if(r && r.cancelled) return;   // 用户取消，静默
  toast('文件夹选择不可用：' + ((r && r.error) || '') + '（可手动填写路径）', 4000);
}

// ── 提交：创建 → POST /api/projects；编辑 → PUT /api/projects/{id} ──
async function submitProjectForm(){
  const isEdit = !!_zpEditingId;
  const name = ((document.getElementById('cp-name') || {}).value || '').trim();
  if(!name){ toast('请填写项目名称'); return; }
  const body = {name: name};
  if(_zpSource === 'local'){
    const ws = ((document.getElementById('cp-workspace') || {}).value || '').trim();
    if(!ws){ toast('请选择本地工作空间（本地文件夹）'); return; }
    body.source = 'local'; body.workspace = ws;
  }else{
    const host = ((document.getElementById('cp-rm-host') || {}).value || '').trim();
    if(!host){ toast('请填写主机名（host.com 或 user@host.com）'); return; }
    const port = ((document.getElementById('cp-rm-port') || {}).value || '').trim();
    if(port && !/^\d+$/.test(port)){ toast('SSH 端口必须是数字（或不填）'); return; }
    const ident = ((document.getElementById('cp-rm-key') || {}).value || '').trim();
    if(_zpAuth === 'key' && !ident){ toast('认证方式为「身份文件」时必须填写身份文件路径'); return; }
    body.source = 'remote';
    body.remote_display_name = ((document.getElementById('cp-rm-name') || {}).value || '').trim();
    body.remote_host = host;
    body.remote_port = port;
    body.remote_auth_mode = _zpAuth;
    body.remote_identity_file = ident;
  }
  const btn = document.getElementById('cp-go');
  const label = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = '⏳ 提交中…'; }
  let r = null;
  try{
    r = isEdit
      ? await api(`/api/projects/${encodeURIComponent(_zpEditingId)}`, {method:'PUT', body: JSON.stringify(body)})
      : await api('/api/projects', {method:'POST', body: JSON.stringify(body)});
  }catch(e){ r = {error: (e.message||'')}; }
  if(btn){ btn.disabled = false; btn.textContent = label; }
  if(!r || r.error){ toast((isEdit ? '保存失败：' : '创建失败：') + ((r && r.error) || '')); return; }
  if(!isEdit){
    try{ await api('/api/projects/default', {method:'POST', body: JSON.stringify({project_id: r.id})}); }
    catch(e){ /* 设默认失败不阻断：项目已创建 */ }
  }
  closeModal();
  const srcDesc = body.source === 'remote' ? '远端 SSH' : '本地';
  toast(isEdit ? `✅ 项目「${name}」已更新（${srcDesc}）`
               : `✅ 项目「${name}」已创建（${srcDesc}），已设为当前工程`);
  _zpEditingId = '';
  renderGnavProjects();
  if(typeof loadCurrentProject === 'function') loadCurrentProject();
}

// 初始化：导航项目组随首屏渲染
// 2026-09-28 多工程 P0-1：若 URL 带工具侧上下文（从建模工具打开），**先反查定位工程**
// 再渲染项目组 —— 否则首屏高亮的还是平台默认工程，工具带进来的上下文被丢掉。
async function initGnavProjects(){
  await applyProjectFromUrl();
  renderGnavProjects();
  if(typeof loadCurrentProject === 'function') loadCurrentProject();
}
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initGnavProjects);
else initGnavProjects();