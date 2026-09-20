/* 管理：用户 / 角色 / 权限 / 审计 / 运维
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 20185-20909  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function setCurrentUser(id){
  localStorage.setItem('mbse_user_id', String(id));
  closePanel();
  loadCurrentUser();
  toast('已切换账号');
}
function logoutUser(){
  localStorage.removeItem('mbse_user_id');
  closeUserMenuOnce();
  showLoggedOutUI();
  toast('已退出登录，点击右上角头像重新登录');
  switchAccount();
}
function loadURTab(id) {
  if(id==='ur-a') loadUsers();
  if(id==='ur-b') loadRoles();
  if(id==='ur-c') loadPermPanel();
  if(id==='ur-d') loadDepartments();
}
// ── 全局状态：用户/角色缓存 + 分页 + 弹窗编辑模式 ──
let UR_USERS=[], UR_ROLES=[], UR_PERM_DOMAINS=[], UR_DEPTS=[];
const UR_PAGE={users:1, roles:1, depts:1};
let UR_SIZE={users:15, roles:15, depts:15};
let _urEdit={mode:null,id:null};   // mode: 'user'|'role'；id=null 表示新增
let _permDirty=false;

function renderPager(kind,total,pages){
  const el=document.getElementById(kind+'-pager');
  if(!el) return;
  const refresh=()=>{ if(kind==='users') renderUsers(); else if(kind==='roles') renderRoles(); else renderDepartments(); };
  renderPagerBar({
    el, total, page:UR_PAGE[kind], size:UR_SIZE[kind],
    onPage:p=>{ UR_PAGE[kind]=p; refresh(); },
    onSize:s=>{ UR_SIZE[kind]=s; UR_PAGE[kind]=1; refresh(); }
  });
}

async function loadUsers() {
  try{ UR_USERS = await api('/api/users'); renderUsers(); }
  catch(e){ toast('加载用户失败'); }
}
function renderUsers() {
  const kw=(document.getElementById('user-search')?.value||'').trim().toLowerCase();
  const rfilter=document.getElementById('user-role-filter')?.value||'';
  let list=UR_USERS;
  if(kw) list=list.filter(u=>(u.display_name||'').toLowerCase().includes(kw)||(u.username||'').toLowerCase().includes(kw)||(u.department||'').toLowerCase().includes(kw));
  if(rfilter==='with') list=list.filter(u=>(u.role_name||'').trim());
  if(rfilter==='none') list=list.filter(u=>!(u.role_name||'').trim());
  const total=list.length, pages=Math.max(1,Math.ceil(total/UR_SIZE.users));
  if(UR_PAGE.users>pages) UR_PAGE.users=pages;
  const items=list.slice((UR_PAGE.users-1)*UR_SIZE.users, UR_PAGE.users*UR_SIZE.users);
  const cur=currentUserId();
  document.getElementById('user-table').innerHTML=`<table class="t">
    <tr><th>用户</th><th>部门</th><th>角色</th><th>工作空间</th><th>来源</th><th>状态</th><th>操作</th></tr>`+
    (items.length?items.map(u=>`<tr>
      <td><b>${esc(u.display_name)}</b><br><small style="color:var(--mut);">@${esc(u.username)}</small></td>
      <td>${esc(u.department||'-')}</td>
      <td><span class="st b">${esc(u.role_name||'-')}（${esc(u.role_type||'-')}）</span></td>
      <td>${esc(u.workspace||'-')}</td>
      <td>${esc(u.source||'-')}</td>
      <td><span class="st ${u.status==='active'?'ok':'g'}">${u.status==='active'?'启用':'停用'}</span></td>
      <td style="white-space:nowrap;">
        <button class="btn sm ghost" onclick="editUser(${u.id})">编辑</button>
        <button class="btn sm ghost" onclick="toggleUserStatus(${u.id})">${u.status==='active'?'停用':'启用'}</button>
        ${u.id===cur?'<span class="st g">当前</span>':`<button class="btn sm red" onclick="deleteUser(${u.id})">删除</button>`}
      </td>
    </tr>`).join(''):'<tr><td colspan="7" style="text-align:center;color:var(--mut);padding:16px;">无匹配用户</td></tr>')+
  '</table>';
  renderPager('users',total,pages);
}
async function loadRoleOptions() {
  if(!UR_ROLES.length) { try{ UR_ROLES = await api('/api/roles'); }catch(e){ return; } }
  const sel=document.getElementById('f-role');
  if(sel) sel.innerHTML='<option value="">未分配</option>'+UR_ROLES.map(r=>`<option value="${r.id}">${r.name}</option>`).join('');
}
function editUser(id){
  const u=UR_USERS.find(x=>x.id===id); if(!u) return;
  showModal('user');                       // 规则经验：先渲染表单再填充
  document.getElementById('user-form-title').textContent='编辑用户';
  const un=document.getElementById('f-username');
  un.value=u.username; un.disabled=true;
  document.getElementById('f-display').value=u.display_name||'';
  document.getElementById('f-ws').value=u.workspace||'';
  document.getElementById('f-status').value=u.status||'active';
  _urEdit={mode:'user',id:u.id};
  loadDepartmentOptions(u.department||'');   // 部门下拉：受控词表
  loadRoleOptions().then(()=>{ document.getElementById('f-role').value=(u.role_id??''); });
}
async function saveUser() {
  const username=(document.getElementById('f-username').value||'').trim();
  const display_name=(document.getElementById('f-display').value||'').trim();
  const department=(document.getElementById('f-dept').value||'').trim();
  const role_id=parseInt(document.getElementById('f-role').value)||null;
  const workspace=(document.getElementById('f-ws').value||'').trim();
  const status=document.getElementById('f-status').value||'active';
  if(!username||!display_name){ toast('用户名和显示名必填'); return; }
  const payload={username,display_name,department,role_id,workspace,status};
  try{
    let r;
    if(_urEdit.mode==='user'&&_urEdit.id){
      r=await api('/api/users/'+_urEdit.id,{method:'PUT',body:JSON.stringify(payload)});
      if(r.error){ toast(r.error); return; }
      toast('用户已更新');
    }else{
      r=await api('/api/users',{method:'POST',body:JSON.stringify(payload)});
      if(r.error){ toast(r.error); return; }
      toast('用户已创建');
    }
    closeModal(); _urEdit={mode:null,id:null};
    await loadUsers();
  }catch(e){ toast('保存失败'); }
}
async function toggleUserStatus(id){
  const u=UR_USERS.find(x=>x.id===id); if(!u) return;
  const next=u.status==='active'?'disabled':'active';
  const r=await api(`/api/users/${id}/status`,{method:'PATCH',body:JSON.stringify({status:next})});
  if(r.error){ toast(r.error); return; }
  toast(next==='disabled'?'已停用':'已启用'); await loadUsers();
}
async function deleteUser(id){
  const u=UR_USERS.find(x=>x.id===id);
  if(!(await confirmDialog(`确认删除用户「${u?u.display_name:''}」？\n删除后不可恢复。`))) return;
  const r=await api('/api/users/'+id,{method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('用户已删除'); await loadUsers();
}
// 清理无角色用户（2026-09-01）：批量删除未分配角色用户；跳过当前用户与内置种子账号
async function cleanNoRoleUsers(){
  const cur=currentUserId();
  const protect=new Set(['admin','wang','li','chen']);   // 内置种子账号保护（避免误删系统账号）
  const noRole=UR_USERS.filter(u=>!(u.role_name||'').trim());
  const targets=noRole.filter(u=>u.id!==cur && !protect.has(u.username));
  if(!targets.length){ toast('没有可清理的无角色用户'); return; }
  const names=targets.slice(0,8).map(u=>u.display_name||u.username).join('、')+(targets.length>8?' 等':'');
  if(!(await confirmDialog(`将删除 ${targets.length} 个未分配角色的用户（已跳过当前用户与内置账号）：\n${names}\n\n删除后不可恢复，确定继续？`))) return;
  let ok=0, fail=0;
  for(const u of targets){
    const r=await api('/api/users/'+u.id,{method:'DELETE'});
    if(r.error){ fail++; } else { ok++; }
  }
  toast(`清理完成：删除 ${ok} 个${fail?'，失败 '+fail+' 个（请逐个处理）':''}`);
  await loadUsers();
}

// ── 部门设置（受控词表：用户表单部门下拉 + 部门管理）──
async function loadDepartments() {
  try{ UR_DEPTS = await api('/api/departments'); renderDepartments(); }
  catch(e){ toast('加载部门失败'); }
}
function renderDepartments() {
  const total=UR_DEPTS.length, pages=Math.max(1,Math.ceil(total/UR_SIZE.depts));
  if(UR_PAGE.depts>pages) UR_PAGE.depts=pages;
  const items=UR_DEPTS.slice((UR_PAGE.depts-1)*UR_SIZE.depts, UR_PAGE.depts*UR_SIZE.depts);
  document.getElementById('dept-table').innerHTML=`<table class="t">
    <tr><th>部门名称</th><th>描述</th><th>排序</th><th>使用人数</th><th>状态</th><th>操作</th></tr>`+
    (items.length?items.map(d=>`<tr>
      <td><b>${esc(d.name)}</b></td>
      <td><small style="color:var(--mut);">${esc(d.description||'-')}</small></td>
      <td>${d.sort_order||0}</td>
      <td><span class="st ${d.user_count>0?'ok':'g'}">${d.user_count}</span></td>
      <td><span class="st ${d.status==='active'?'ok':'g'}">${d.status==='active'?'启用':'停用'}</span></td>
      <td style="white-space:nowrap;">
        <button class="btn sm ghost" onclick="editDepartment(${d.id})">编辑</button>
        <button class="btn sm ghost" onclick="toggleDeptStatus(${d.id})">${d.status==='active'?'停用':'启用'}</button>
        <button class="btn sm red" onclick="deleteDepartment(${d.id})">删除</button>
      </td>
    </tr>`).join(''):'<tr><td colspan="6" style="text-align:center;color:var(--mut);padding:16px;">暂无部门，点击右上角「＋ 新增部门」创建</td></tr>')+
  '</table>';
  renderPager('depts',total,pages);
}
async function loadDepartmentOptions(selected) {
  // 用户表单「部门」下拉：受控词表（含当前用户归属但不在表中的历史值兜底）
  let list;
  try{ list = await api('/api/departments'); UR_DEPTS = list; }
  catch(e){ list = UR_DEPTS; }
  const sel=document.getElementById('f-dept');
  if(!sel) return;
  let html='<option value="">未分配</option>';
  (list||[]).filter(d=>d.status==='active').forEach(d=>{
    html+=`<option value="${esc(d.name)}">${esc(d.name)}</option>`;
  });
  if(selected && !(list||[]).some(d=>d.name===selected)) html+=`<option value="${esc(selected)}">${esc(selected)}（历史）</option>`;
  sel.innerHTML=html;
  sel.value=selected||'';
}
function editDepartment(id){
  const d=UR_DEPTS.find(x=>x.id===id); if(!d) return;
  showModal('department');
  document.getElementById('dept-form-title').textContent='编辑部门';
  document.getElementById('f-dname').value=d.name||'';
  document.getElementById('f-ddesc').value=d.description||'';
  document.getElementById('f-dorder').value=d.sort_order||0;
  document.getElementById('f-dstatus').value=d.status||'active';
  _urEdit={mode:'department',id:d.id};
}
async function saveDepartment() {
  const name=(document.getElementById('f-dname').value||'').trim();
  const description=(document.getElementById('f-ddesc').value||'').trim();
  const sort_order=parseInt(document.getElementById('f-dorder').value)||0;
  const status=document.getElementById('f-dstatus').value||'active';
  if(!name){ toast('部门名称必填'); return; }
  try{
    let r;
    if(_urEdit.mode==='department'&&_urEdit.id){
      r=await api('/api/departments/'+_urEdit.id,{method:'PUT',body:JSON.stringify({name,description,sort_order,status})});
      if(r.error){ toast(r.error); return; }
      toast('部门已更新');
    }else{
      r=await api('/api/departments',{method:'POST',body:JSON.stringify({name,description,sort_order,status})});
      if(r.error){ toast(r.error); return; }
      toast('部门已创建');
    }
    closeModal(); _urEdit={mode:null,id:null};
    await loadDepartments();
    await loadUsers();   // 部门改名同步后刷新用户列表
  }catch(e){ toast('保存失败'); }
}
async function toggleDeptStatus(id){
  const d=UR_DEPTS.find(x=>x.id===id); if(!d) return;
  const next=d.status==='active'?'disabled':'active';
  const r=await api('/api/departments/'+id,{method:'PUT',body:JSON.stringify({name:d.name,description:d.description||'',sort_order:d.sort_order||0,status:next})});
  if(r.error){ toast(r.error); return; }
  toast(next==='disabled'?'已停用':'已启用'); await loadDepartments();
}
async function deleteDepartment(id){
  const d=UR_DEPTS.find(x=>x.id===id);
  if(!(await confirmDialog(`确认删除部门「${d?d.name:''}」？\n部门下有用户时不可删除。`))) return;
  const r=await api('/api/departments/'+id,{method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('部门已删除'); await loadDepartments();
}

async function loadRoles() {
  try{ UR_ROLES = await api('/api/roles'); renderRoles(); }
  catch(e){ toast('加载角色失败'); }
}
function renderRoles() {
  const kw=(document.getElementById('role-search')?.value||'').trim().toLowerCase();
  let list=UR_ROLES;
  if(kw) list=list.filter(r=>(r.name||'').toLowerCase().includes(kw)||(r.description||'').toLowerCase().includes(kw));
  const total=list.length, pages=Math.max(1,Math.ceil(total/UR_SIZE.roles));
  if(UR_PAGE.roles>pages) UR_PAGE.roles=pages;
  const items=list.slice((UR_PAGE.roles-1)*UR_SIZE.roles, UR_PAGE.roles*UR_SIZE.roles);
  document.getElementById('role-table').innerHTML=`<table class="t">
    <tr><th>角色名</th><th>类型</th><th>人数</th><th>操作</th></tr>`+
    (items.length?items.map(r=>`<tr>
      <td><b>${esc(r.name)}</b>${r.description?'<br><small style="color:var(--mut);">'+esc(r.description)+'</small>':''}</td>
      <td><span class="st b">${r.type==='preset'?'预置':'自定义'}</span></td>
      <td>${r.user_count}</td>
      <td style="white-space:nowrap;">
        <button class="btn sm ghost" onclick="editRole(${r.id})">编辑</button>
        <button class="btn sm ghost" onclick="copyRole(${r.id})">复制</button>
        <button class="btn sm ghost" onclick="editPerms(${r.id})">权限</button>
        ${r.type!=='preset'?`<button class="btn sm red" onclick="deleteRole(${r.id})">删除</button>`:'<span class="st g">预置</span>'}
      </td>
    </tr>`).join(''):'<tr><td colspan="4" style="text-align:center;color:var(--mut);padding:16px;">无匹配角色</td></tr>')+
  '</table>';
  renderPager('roles',total,pages);
}
function editRole(id){
  const role=UR_ROLES.find(r=>r.id===id); if(!role) return;
  if(role.type==='preset'){ toast('预置角色不可修改，请先复制为新角色'); return; }
  showModal('role');
  document.getElementById('role-form-title').textContent='编辑角色';
  document.getElementById('f-name').value=role.name;
  document.getElementById('f-desc').value=role.description||'';
  _urEdit={mode:'role',id:role.id};
}
function copyRole(id){
  const role=UR_ROLES.find(r=>r.id===id); if(!role) return;
  showModal('role');
  document.getElementById('role-form-title').textContent='复制角色（另存为新角色）';
  document.getElementById('f-name').value=role.name+' 副本';
  document.getElementById('f-desc').value=role.description||'';
  window._copyPerms=JSON.parse(role.permissions||'{}');   // 继承源角色权限
  _urEdit={mode:'role',id:null};
}
async function saveRole() {
  const name=(document.getElementById('f-name').value||'').trim();
  const description=(document.getElementById('f-desc').value||'').trim();
  if(!name){ toast('角色名必填'); return; }
  const perms=window._copyPerms||{};
  window._copyPerms=null;
  try{
    let r;
    if(_urEdit.mode==='role'&&_urEdit.id){
      r=await api('/api/roles/'+_urEdit.id,{method:'PUT',body:JSON.stringify({name,description,permissions:perms})});
      if(r.error){ toast(r.error); return; }
      toast('角色已更新');
    }else{
      r=await api('/api/roles',{method:'POST',body:JSON.stringify({name,description,permissions:perms})});
      if(r.error){ toast(r.error); return; }
      toast('角色已创建');
    }
    closeModal(); _urEdit={mode:null,id:null};
    await loadRoles();
  }catch(e){ toast('保存失败'); }
}
async function deleteRole(id) {
  const role=UR_ROLES.find(r=>r.id===id);
  if(!(await confirmDialog(`确认删除角色「${role?role.name:''}」？\n删除后不可恢复。`))) return;
  const r=await api('/api/roles/'+id,{method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('已删除'); await loadRoles();
}
async function editPerms(id) {
  // 角色列表「权限」→ 直接进入权限配置 Tab 并选中该角色（可编辑矩阵；预置角色只读有提示）
  const tabEl=[...document.querySelectorAll('[data-tabgrp="ur"]')].find(t=>(t.getAttribute('onclick')||'').includes("'ur-c'"));
  if(tabEl) tab(tabEl,'ur','ur-c');          // 切换 tab（loadURTab 会触发 loadPermPanel 异步填充下拉）
  const sel=document.getElementById('perm-role');
  if(!sel) return;
  // 首次进入时下拉 options 尚未填充（loadPermPanel 异步），轮询等待就绪后再选中目标角色
  const t0=Date.now();
  while(!sel.options.length && Date.now()-t0<3000) await new Promise(r=>setTimeout(r,50));
  if(UR_ROLES.some(r=>r.id===id)){ sel.value=id; }
  renderPermMatrix();
}

// ── 权限配置矩阵（FR-UI-7 可配置落地）──
async function loadPermPanel(){
  try{
    const [roles, dom] = await Promise.all([api('/api/roles'), api('/api/permissions/domains')]);
    UR_ROLES=roles; UR_PERM_DOMAINS=(dom&&dom.domains)||[];
    fillPermRoleSelect();
  }catch(e){ toast('加载权限配置失败'); }
}
function fillPermRoleSelect(){
  const sel=document.getElementById('perm-role');
  if(!sel) return;
  const cur=sel.value?parseInt(sel.value):null;
  sel.innerHTML=UR_ROLES.map(r=>`<option value="${r.id}">${esc(r.name)}${r.type==='preset'?'（预置）':''}</option>`).join('');
  if(cur&&UR_ROLES.some(r=>r.id===cur)) sel.value=cur;
  renderPermMatrix();
}
function currentPermRole(){
  const sel=document.getElementById('perm-role');
  if(!sel||!sel.value) return null;
  return UR_ROLES.find(r=>r.id===parseInt(sel.value))||null;
}
function renderPermMatrix(){
  const el=document.getElementById('perm-matrix');
  const role=currentPermRole();
  if(!role){ el.innerHTML='<div class="note">暂无角色，请先到「角色管理」创建角色</div>'; return; }
  const readonly=role.type==='preset';
  const perms=JSON.parse(role.permissions||'{}');
  // 面板头徽章同步（角色状态一目了然，替代冗余顶部行）
  const badge=document.getElementById('perm-role-badge');
  if(badge) badge.innerHTML=`<span class="st ${readonly?'b':'ok'}">${readonly?'🔒 预置 · 只读（复制为新角色后可编辑）':'✓ 自定义 · 可编辑'}</span>`;
  let html=readonly?'<div class="note" style="margin-bottom:8px;">预置角色只读：请先在角色列表「复制」为新角色后再编辑权限。</div>':'';
  if(!UR_PERM_DOMAINS.length){ el.innerHTML=html+'<div class="loading">权限定义加载中…</div>'; return; }
  html+=`<table class="t"><tr><th>功能域</th><th>操作权限</th></tr>`;
  for(const dom of UR_PERM_DOMAINS){
    const checked=perms[dom.key]||[];
    html+=`<tr><td style="white-space:nowrap;"><b>${esc(dom.label)}</b><br><small style="color:var(--mut);">${esc(dom.key)}</small></td>
      <td>${dom.ops.map(o=>`<label style="display:inline-flex;align-items:center;gap:4px;margin:2px 12px 2px 0;font-size:12px;${readonly?'color:var(--mut);':''}"><input type="checkbox" data-dom="${esc(dom.key)}" data-op="${esc(o.key)}" ${checked.includes(o.key)?'checked':''} ${readonly?'disabled':''} onchange="onPermChange()"> ${esc(o.label)}</label>`).join('')}</td></tr>`;
  }
  html+='</table>';
  el.innerHTML=html;
  _permDirty=false;
}
function onPermChange(){ _permDirty=true; }
function resetPermMatrix(){ renderPermMatrix(); }   // 从已保存 role.permissions 重新渲染 = 还原
// ── 权限域管理（2026-09-01 权限域表化：openPermDomainMgr 面板，内置种子域受保护）──
async function openPermDomainMgr(){
  openPanel('⚙️ 权限域管理', '<div id="perm-dom-mgr" style="font-size:12px;"><div class="loading">加载中…</div></div>');
  await permDomMgrLoad();
}
async function permDomMgrLoad(){
  const d = await api('/api/permissions/domains').catch(()=>null);
  const doms = (d&&d.domains)||[];
  UR_PERM_DOMAINS = doms;
  const el = document.getElementById('perm-dom-mgr');
  if(!el) return;
  let h = '<div class="note" style="margin-bottom:8px;">🛡 内置种子域/操作项受保护不可删除；自定义域可增删（删除前校验角色引用）。变更后权限矩阵自动生效。</div>';
  h += '<div style="margin-bottom:10px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;background:var(--bg2, #fafafa);padding:8px;border-radius:8px;">';
  h += '<b style="color:var(--blue-d);">＋ 新增域</b>';
  h += '<input id="pdm-key" placeholder="域键 kb_publish" style="width:130px;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;">';
  h += '<input id="pdm-label" placeholder="显示名" style="width:100px;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;">';
  h += '<button class="btn sm" onclick="permDomAdd()">创建</button></div>';
  h += '<div style="max-height:46vh;overflow:auto;">';
  h += doms.map(d=>{
    const ops = (d.ops||[]).map(o=>
      `<span style="display:inline-flex;align-items:center;gap:3px;background:var(--bg2,#f5f5f5);border:1px solid var(--line);border-radius:12px;padding:1px 8px;margin:2px;font-size:11px;">
         ${esc(o.label)}<small style="color:var(--mut);">(${esc(o.key)})</small>
         ${o.builtin?'':`<a style="color:var(--red);cursor:pointer;" title="删除操作项" onclick="permOpDel('${esc(d.key)}','${esc(o.key)}')">✕</a>`}
       </span>`).join('');
    const addOp = `<span style="display:inline-flex;gap:3px;margin:2px;"><input id="pop-${esc(d.key)}" placeholder="操作key" style="width:86px;border:1px solid var(--line);border-radius:6px;padding:2px 4px;font-size:11px;"><input id="popl-${esc(d.key)}" placeholder="显示名" style="width:64px;border:1px solid var(--line);border-radius:6px;padding:2px 4px;font-size:11px;"><button class="btn sm ghost" style="padding:1px 8px;" onclick="permOpAdd('${esc(d.key)}')">＋</button></span>`;
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px;margin-bottom:8px;">
      <div style="display:flex;align-items:center;gap:6px;">
        <b>${esc(d.label)}</b><small style="color:var(--mut);">${esc(d.key)}</small>
        ${d.builtin?'<span class="st b">🛡 内置</span>':`<button class="btn sm red" onclick="permDomDel('${esc(d.key)}')">🗑 删域</button>`}
      </div>
      <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:2px;align-items:center;">${ops}${addOp}</div>
    </div>`;
  }).join('');
  h += '</div>';
  el.innerHTML = h;
}
async function permDomAdd(){
  const key = document.getElementById('pdm-key').value.trim();
  const label = document.getElementById('pdm-label').value.trim();
  if(!key || !label){ toast('请填写域键与显示名'); return; }
  const r = await api('/api/permissions/domains', {method:'POST', body:JSON.stringify({key, label, ops:[]})});
  if(r.error){ toast(r.error); return; }
  toast('权限域已创建');
  await permDomMgrLoad(); renderPermMatrix();
}
async function permDomDel(key){
  if(!confirm(`确认删除权限域「${key}」？相关操作项将一并删除`)) return;
  const r = await api('/api/permissions/domains/'+key, {method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('权限域已删除');
  await permDomMgrLoad(); renderPermMatrix();
}
async function permOpAdd(dkey){
  const k = document.getElementById('pop-'+dkey).value.trim();
  const l = document.getElementById('popl-'+dkey).value.trim();
  if(!k || !l){ toast('请填写操作键与显示名'); return; }
  const r = await api(`/api/permissions/domains/${dkey}/ops`, {method:'POST', body:JSON.stringify({key:k, label:l})});
  if(r.error){ toast(r.error); return; }
  toast('操作项已添加');
  await permDomMgrLoad(); renderPermMatrix();
}
async function permOpDel(dkey, opkey){
  if(!confirm(`确认删除操作项「${opkey}」？`)) return;
  const r = await api(`/api/permissions/domains/${dkey}/ops/${opkey}`, {method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('操作项已删除');
  await permDomMgrLoad(); renderPermMatrix();
}
async function savePerms(){
  const role=currentPermRole();
  if(!role){ toast('请先选择角色'); return; }
  if(role.type==='preset'){ toast('预置角色只读，请先复制为新角色'); return; }
  if(!_permDirty){ toast('权限未变更'); return; }
  const perms={};
  document.querySelectorAll('#perm-matrix input[data-dom]').forEach(cb=>{
    if(cb.checked){ const d=cb.getAttribute('data-dom'); (perms[d]=perms[d]||[]).push(cb.getAttribute('data-op')); }
  });
  const r=await api('/api/roles/'+role.id,{method:'PUT',body:JSON.stringify({name:role.name,description:role.description||'',permissions:perms})});
  if(r.error){ toast(r.error); return; }
  role.permissions=JSON.stringify(perms);
  _permDirty=false;
  toast('权限已保存');
  renderPermMatrix();
}

// ── 审计 ──
async function loadAudit() {
  const search = document.getElementById('audit-search').value;
  const d = await api(`/api/audit?limit=200${search?'&search='+encodeURIComponent(search):''}`);
  document.getElementById('audit-stats').innerHTML = `
    <div class="kpi"><div class="n">${d.stats.today_llm}</div><div class="l">今日 LLM 交互</div></div>
    <div class="kpi"><div class="n">${d.stats.today_upload}</div><div class="l">今日上传</div></div>
    <div class="kpi"><div class="n" style="color:var(--amb);">${d.stats.blocked}</div><div class="l">越权拦截</div></div>`;
  document.getElementById('audit-table').innerHTML = `<table class="t">
    <tr><th>时间</th><th>用户</th><th>事件</th><th>详情</th><th>结果</th></tr>` +
    d.logs.map(l=>`<tr>
      <td>${(l.created_at||'').slice(0,19)}</td><td>${l.user_name}</td>
      <td><span class="st b">${l.event_type}</span></td><td>${l.detail}</td>
      <td><span class="st ${l.result==='success'?'ok':l.result==='blocked'?'r':'w'}">${l.result}</span></td>
    </tr>`).join('') + '</table>';
}

// ── 统一监控平台（D12） ──
async function loadOps() {
  const [d, m] = await Promise.all([api('/api/monitor/dashboard?days=7'), api('/api/ops/metrics')]);
  const ok = d.runs && d.llm && d.trend;
  if(!ok){ document.getElementById('ops-kpis').innerHTML = '<div class="mut">监控接口不可用</div>'; return; }
  // KPI 四卡：真实聚合（成功率/平均耗时/LLM Mock率/开放告警）
  document.getElementById('ops-kpis').innerHTML = `
    <div class="kpi"><div class="n" style="color:${d.runs.total?((d.runs.success_rate<100)?'var(--amb)':'var(--grn)'):'var(--mut)'};">${d.runs.success_rate}<span style="font-size:13px;">%</span></div><div class="l">运行成功率 <span class="st ${d.runs.total?((d.runs.failed>0)?'w':'ok'):'g'}">${d.runs.total} 次 / ${d.runs.failed} 失败</span></div></div>
    <div class="kpi"><div class="n">${d.runs.avg_latency_ms}<span style="font-size:13px;">ms</span></div><div class="l">平均耗时 <span class="st b">近 7 天</span></div></div>
    <div class="kpi"><div class="n" style="color:${d.llm.calls?((d.llm.mock_rate>50)?'var(--amb)':'var(--grn)'):'var(--mut)'};">${d.llm.mock_rate}<span style="font-size:13px;">%</span></div><div class="l">LLM Mock 率 <span class="st g">${d.llm.calls} 次调用 / ${d.llm.total_tokens} Tokens</span></div></div>
    <div class="kpi" style="cursor:pointer;" onclick="loadOpsAlerts()"><div class="n" style="color:${d.open_alerts>0?'var(--red)':'var(--grn)'};">${d.open_alerts}</div><div class="l">开放告警 <span class="st ${d.open_alerts>0?'r':'ok'}">${d.subs_active} 订阅活跃</span></div></div>`;
  document.getElementById('ops-alert-badge').textContent = `${d.open_alerts} 开放`;
  // 趋势 SVG（柱=运行数，折线=平均耗时）
  document.getElementById('ops-trend').innerHTML = renderOpsTrend(d.trend);
  // 节点耗时分布
  document.getElementById('ops-node-stats').innerHTML = renderOpsNodeStats(d.node_stats);
  // 最近告警
  renderOpsAlerts(d.recent_alerts);
  // 系统信息（进程 PID / 运行时长 / DB 大小）挂到趋势面板右上角
  const sys = d.system || {};
  document.getElementById('ops-trend').insertAdjacentHTML('beforeend',
    `<div class="mut" style="font-size:11px;margin-top:6px;">PID ${sys.pid} · 运行 ${fmtUptime(sys.uptime_s)} · DB ${sys.db_size_kb} KB</div>`);
  // 错误码 + 备份容灾（沿用 7.1/7.2/7.3 运维信息）
  document.getElementById('ops-errors').innerHTML = `<table class="t">
    <tr><th>错误码</th><th>含义</th><th>解决方案</th></tr>` +
    m.error_codes.map(e=>`<tr><td>${e.code}</td><td>${e.desc}</td><td>${e.solution}</td></tr>`).join('') + '</table>';
  document.getElementById('ops-backup').innerHTML = `
    <div class="kv"><span>备份策略</span><b>${m.backup.strategy}</b></div>
    <div class="kv"><span>最近备份</span><b>${m.backup.last_backup}</b></div>
    <div class="kv"><span>下次维护</span><b>${m.backup.next_maintenance}</b></div>
    <div class="kv"><span>实体总数</span><b>${m.total_entities}</b></div>
    <div class="kv"><span>对话总数</span><b>${m.total_conversations}</b></div>
    <div class="kv"><span>消息总数</span><b>${m.total_messages}</b></div>
    <div class="kv"><span>审计日志</span><b>${m.total_audit_logs}</b></div>`;
}
function fmtUptime(sec){
  sec = Number(sec)||0;
  const h = Math.floor(sec/3600), mi = Math.floor(sec%3600/60);
  return h?`${h}h ${mi}m`:`${Math.floor(sec/60)}m`;
}
function renderOpsTrend(trend){
  if(!trend || !trend.length) return '<div class="mut">暂无趋势数据</div>';
  const W=470,H=192,padL=38,padR=58,padT=16,padB=24;
  const n=trend.length, iw=W-padL-padR, ih=H-padT-padB;
  const maxR=Math.max(...trend.map(t=>t.runs),1);
  const maxM=Math.max(...trend.map(t=>t.avg_ms),1);
  const X=i=>padL+(n===1?0:i*(iw/(n-1)));
  const YR=v=>padT+ih-(v/maxR)*ih;
  const YM=v=>padT+ih-(v/maxM)*ih;
  let bars='', line='', dots='', msTxt='', labels='';
  trend.forEach((t,i)=>{
    const bx=X(i), bw=Math.max(3,iw/n*0.5);
    const bh=Math.max(2,padT+ih-YR(t.runs));
    bars += `<rect x="${(bx-bw/2).toFixed(1)}" y="${YR(t.runs).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="#c9baff"></rect>`;
    bars += `<text x="${bx.toFixed(1)}" y="${(YR(t.runs)-3).toFixed(1)}" font-size="8.5" fill="#7c5cff" text-anchor="middle">${t.runs}</text>`;
    line += `${i?',':''}${bx.toFixed(1)},${YM(t.avg_ms).toFixed(1)}`;
    dots += `<circle cx="${bx.toFixed(1)}" cy="${YM(t.avg_ms).toFixed(1)}" r="2.5" fill="#ff7a45"></circle>`;
    msTxt += `<text x="${bx.toFixed(1)}" y="${(YM(t.avg_ms)-5).toFixed(1)}" font-size="8" fill="#ff7a45" text-anchor="middle">${t.avg_ms}ms</text>`;
    labels += `<text x="${bx.toFixed(1)}" y="${H-padB+12}" font-size="8.5" fill="#888" text-anchor="middle">${escMd(t.date.slice(5))}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;background:#fff;border:1px solid var(--line);border-radius:8px;display:block;">
    <text x="${padL}" y="12" font-size="10" fill="#888">近 7 天：运行数（柱） + 平均耗时 ms（折线）</text>
    <line x1="${padL}" y1="${H-padB}" x2="${W-padR}" y2="${H-padB}" stroke="#eee"></line>
    <text x="${W-padR+6}" y="30" font-size="8.5" fill="#ff7a45">avg_ms</text>
    <text x="${W-padR+6}" y="42" font-size="8.5" fill="#7c5cff">runs</text>
    ${bars}
    <polyline points="${line}" fill="none" stroke="#ff7a45" stroke-width="1.6"></polyline>${dots}${msTxt}${labels}
  </svg>`;
}
function renderOpsNodeStats(stats){
  if(!stats || !stats.length) return '<div class="mut" style="padding:10px;font-size:12px;">暂无节点执行数据</div>';
  const maxMs = Math.max(...stats.map(s=>s.avg_ms),1);
  return stats.map(s=>`
    <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);">
      <span class="badge" style="width:64px;text-align:center;">${escMd(s.type)}</span>
      <div style="flex:1;background:#f0f2f7;border-radius:4px;height:10px;overflow:hidden;">
        <div style="height:100%;width:${Math.min(100,(s.avg_ms/maxMs)*100).toFixed(1)}%;background:${s.errors>0?'var(--red)':'#7c5cff'};border-radius:4px;"></div>
      </div>
      <span style="width:70px;text-align:right;font-size:11px;color:#555;">${s.avg_ms}ms</span>
      <span class="st ${s.errors>0?'r':'ok'}">×${s.count}${s.errors?` ⚠${s.errors}`:''}</span>
    </div>`).join('');
}
function levelBadge(l){
  return l==='critical'?'<span class="st r">critical</span>':l==='warning'?'<span class="st w">warning</span>':'<span class="st g">info</span>';
}
function renderOpsAlerts(alerts){
  const box = document.getElementById('ops-alerts');
  if(!alerts || !alerts.length){ box.innerHTML = '<div class="mut" style="padding:10px;font-size:12px;">暂无告警事件</div>'; return; }
  box.innerHTML = `<table class="t"><tr><th>时间</th><th>规则</th><th>条件</th><th>触发值</th><th>等级</th><th>状态</th><th></th></tr>` +
    alerts.map(a=>`<tr>
      <td style="font-size:11px;">${escMd((a.created_at||'').slice(5,16))}</td>
      <td><b>${escMd(a.rule_name)}</b><div style="font-size:10px;color:var(--mut);">run#${a.run_id} ${escMd(a.flow_name||'')}</div></td>
      <td>${escMd(a.metric)} ${escMd(a.operator)} ${a.threshold}</td>
      <td style="color:${a.actual>=a.threshold?'var(--red)':'var(--amb)'};font-weight:600;">${a.actual}</td>
      <td>${levelBadge(a.level)}</td>
      <td>${a.status==='open'?'<span class="st w">open</span>':'<span class="st ok">acked</span>'}</td>
      <td>${a.status==='open'?`<button class="btn sm ghost" onclick="ackAlert(${a.id})">✅ 确认</button>`:''}</td>
    </tr>`).join('') + '</table>';
}
async function loadOpsAlerts(){
  const d = await api('/api/monitor/alerts');
  document.getElementById('ops-alert-badge').textContent = `${d.total_open} 开放`;
  renderOpsAlerts(d.alerts);
}
async function ackAlert(id){
  await api(`/api/monitor/alerts/${id}/ack`, {method:'POST'});
  toast('告警已确认');
  loadOpsAlerts();
}
async function loadOpsLogs(){
  const p = new URLSearchParams();
  const rid = document.getElementById('ops-log-runid').value.trim();
  const nt = document.getElementById('ops-log-type').value;
  const st = document.getElementById('ops-log-status').value;
  const q = document.getElementById('ops-log-q').value.trim();
  if(rid) p.set('run_id', rid);
  if(nt) p.set('node_type', nt);
  if(st) p.set('status', st);
  if(q) p.set('q', q);
  const d = await api('/api/monitor/logs?'+p.toString());
  const box = document.getElementById('ops-log-list');
  if(!d.logs || !d.logs.length){ box.innerHTML = '<div class="mut" style="padding:10px;font-size:12px;">无匹配日志</div>'; return; }
  box.innerHTML = `<table class="t"><tr><th>Run</th><th>节点</th><th>类型</th><th>状态</th><th>耗时</th><th>内容</th></tr>` +
    d.logs.map(l=>`<tr>
      <td style="font-size:11px;">#${l.run_id}</td>
      <td><b>${escMd(l.node_label||l.node_id)}</b></td>
      <td>${escMd(l.node_type)}</td>
      <td>${l.status==='done'?'<span class="st ok">done</span>':l.status==='error'?'<span class="st r">error</span>':'<span class="st g">skipped</span>'}</td>
      <td>${l.latency_ms||0}ms</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;">${escMd(String(l.content||'').slice(0,70))}</td>
    </tr>`).join('') + '</table>';
}
// ── 告警规则管理（弹窗） ──
async function openAlertRules(){
  const rules = await api('/api/monitor/alert-rules');
  const b = document.getElementById('modal-body');
  b.innerHTML = `
    <h3 style="margin:0 0 2px;">告警规则管理</h3>
    <div class="mut" style="font-size:12px;margin-bottom:10px;">运行结束后自动评估：success_rate / avg_latency / error_count / mock_rate，命中写入告警中心并可推送 A2A webhook</div>
    <div style="max-height:290px;overflow:auto;margin-bottom:12px;">
      <table class="t" id="ar-table"><tr><th>规则</th><th>条件</th><th>等级</th><th>通知</th><th>状态</th><th>最近触发</th><th></th></tr>
      ${rules.length?rules.map(r=>`<tr>
        <td><b>${escMd(r.name)}</b></td>
        <td>${escMd(r.metric)} ${escMd(r.operator)} ${r.threshold}</td>
        <td>${levelBadge(r.level)}</td>
        <td style="font-size:11px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escMd(r.notify_url||'—')}</td>
        <td>${r.status==='active'?'<span class="st ok">active</span>':'<span class="st w">paused</span>'}</td>
        <td style="font-size:11px;">${escMd((r.last_fired_at||'—').slice(0,16))}</td>
        <td style="white-space:nowrap;">
          <button class="btn sm ghost" onclick="toggleAlertRule(${r.id})" title="${r.status==='active'?'暂停':'激活'}">${r.status==='active'?'⏸':'▶'}</button>
          <button class="btn sm ghost" onclick="delAlertRule(${r.id})" title="删除">🗑</button>
        </td></tr>`).join(''):'<tr><td colspan="7" class="mut">暂无规则，请在下方新增</td></tr>'}
      </table>
    </div>
    <div style="border-top:1px solid var(--line);padding-top:10px;">
      <div class="ph" style="padding:0 0 8px;">➕ 新增规则</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:8px;">
        <input id="ar-name" placeholder="规则名" value="成功率过低">
        <select id="ar-metric">
          <option value="success_rate">success_rate 成功率%</option>
          <option value="avg_latency">avg_latency 平均耗时ms</option>
          <option value="error_count">error_count 错误数</option>
          <option value="mock_rate">mock_rate Mock占比%</option>
        </select>
        <div style="display:flex;gap:6px;"><select id="ar-op" style="width:64px;"><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option></select><input id="ar-th" type="number" value="100" style="flex:1;"></div>
        <select id="ar-level"><option value="warning">warning</option><option value="critical">critical</option><option value="info">info</option></select>
        <input id="ar-url" placeholder="通知 webhook URL（可空）">
        <input id="ar-secret" placeholder="HMAC 签名密钥（可空）">
      </div>
      <button class="btn" onclick="saveAlertRule()">保存规则</button>
    </div>`;
  document.getElementById('modal').classList.add('show');
}
async function saveAlertRule(){
  const name = document.getElementById('ar-name').value.trim();
  const metric = document.getElementById('ar-metric').value;
  const op = document.getElementById('ar-op').value;
  const th = parseFloat(document.getElementById('ar-th').value) || 0;
  const level = document.getElementById('ar-level').value;
  const url = document.getElementById('ar-url').value.trim();
  const secret = document.getElementById('ar-secret').value.trim();
  if(!name){ toast('规则名必填'); return; }
  const r = await api('/api/monitor/alert-rules', {method:'POST', body:JSON.stringify({
    name, metric, operator:op, threshold:th, level, notify_url:url, secret, status:'active'})});
  if(r.error){ toast(r.error); return; }
  toast('规则已保存');
  openAlertRules();
  loadOps();
}
async function toggleAlertRule(id){
  await api(`/api/monitor/alert-rules/${id}/toggle`, {method:'POST'});
  openAlertRules();
  loadOps();
}
async function delAlertRule(id){
  if(!(await confirmDialog('确认删除该告警规则？'))) return;
  await api(`/api/monitor/alert-rules/${id}`, {method:'DELETE'});
  toast('规则已删除');
  openAlertRules();
  loadOps();
}

// ── 保存函数 ──
async function saveBranch() {
  const name = document.getElementById('f-name').value.trim();
  // 分支模型：个人分支从 dev/release 拉基线可合并回 dev；本地分支（local）离线/实验不可合并
  const type = document.getElementById('f-type').value;
  const parentEl = document.getElementById('f-parent');
  const parent = type==='local' ? '' : parentEl.value;
  const desc = document.getElementById('f-desc').value.trim();
  const editName = document.getElementById('f-edit-branch').value;
  if(!name) { toast('分支名必填'); return; }
  if(type==='personal' && !PROTECTED_BRANCHES.includes(parent)) { toast('个人分支基线来源只能是 dev 或 release'); return; }
  const payload = {name, branch_type:type, parent_branch:parent, description:desc};
  let r;
  if(editName) {
    const sr = document.getElementById('f-status');
    if(sr) payload.status = sr.value;
    r = await api('/api/branches/' + encodeURIComponent(editName), {method:'PUT', body:JSON.stringify(payload)});
  } else {
    r = await api('/api/branches', {method:'POST', body:JSON.stringify(payload)});
  }
  if(r && r.error) { toast('保存失败：' + r.error); return; }
  closeModal();
  toast(editName ? `分支已更新为 ${name}` : `分支 ${name} 已创建`);
  document.getElementById('f-edit-branch').value = '';
  // 若改名的分支是当前工作分支 → 同步本地记录
  if(editName && editName !== name && getCurrentBranch() === editName) {
    localStorage.setItem('mbse_branch', name);
  }
  loadBranches(); loadMerges(); loadGraph();
}
async function saveMerge() {
  const src = document.getElementById('f-src').value;
  const tgt = document.getElementById('f-tgt').value;
  if(!src||!tgt) { toast('请选择源分支与目标分支'); return; }
  if(src===tgt) { toast('源分支与目标分支不能相同'); return; }
  // 发布版本号：仅目标=release 时透传（后端自动生成 v{n} 兜底），合并到 dev 忽略
  const rvEl = document.getElementById('f-release-version');
  const rv = rvEl ? rvEl.value.trim() : '';
  // P1-1：标题 + 草稿复选（后端未升级时多余字段被忽略，向前兼容）
  const titleEl = document.getElementById('f-mr-title');
  const draftEl = document.getElementById('f-mr-draft');
  const r = await api('/api/branches/merge-requests', {method:'POST', body:JSON.stringify({
    source_branch:src, target_branch:tgt, release_version: tgt==='release' ? rv : '',
    title: titleEl ? titleEl.value.trim() : '', draft: draftEl ? draftEl.checked : false
  })});
  if(r && r.error) { toast('创建失败：' + r.error); return; }
  closeModal();
  toast(draftEl && draftEl.checked ? '草稿已创建，可在列表中「转评审」进入正式评审' : `合并请求已创建，检测到 ${r.conflicts} 处冲突`);
  loadMerges();
}

// ════════════ 审批管理（前端原型：mock 数据存 localStorage，结构对齐未来后端接口）════════════
const AP_KEY='mbse_approval_data_v1';
// 原型演示用「角色 → 当前用户」映射（真实系统按用户-角色关系匹配）
const AP_ROLE_USERS={'部门负责人':'王工','平台管理员':'赵管','技术负责人':'王工','知识工程师':'李工','系统管理员':'赵管'};
let AP=null;              // {types:[], tpls:[], runs:[], seq:{}}
let _apTplDraft=null;     // 模板编辑草稿
let _apTypeEdit=null;     // 类型编辑 {id}
