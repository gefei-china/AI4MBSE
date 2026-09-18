/* 审批：类型 / 模板 / 待办 / 记录
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 20910-21333  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function apMe(){ const n=document.getElementById('ufoot-name'); return (n&&n.textContent)||'王工'; }
function apNow(){ return new Date().toISOString().slice(0,16).replace('T',' '); }

function apEnsure(){
  if(AP) return AP;
  try{ const raw=localStorage.getItem(AP_KEY); if(raw){ AP=JSON.parse(raw); if(AP&&AP.types) return AP; } }catch(e){}
  AP={
    seq:{type:6,tpl:5,run:6},
    types:[
      {id:1,name:'发布审批',icon:'📦',scenario:'模型 / 报告 / 技能包等资产正式发布前的审核把关',desc:'预置通用类型',builtin:true,enabled:true},
      {id:2,name:'变更审批',icon:'🔄',scenario:'工程变更请求（ECR/ECO）及模型变更影响评估后的变更确认',desc:'预置通用类型',builtin:true,enabled:true},
      {id:3,name:'资产入库审批',icon:'🏦',scenario:'实体 / 文档 / 知识资产新增入库前的合规确认',desc:'预置通用类型',builtin:true,enabled:true},
      {id:4,name:'分支合并审批',icon:'🌿',scenario:'分支合并请求：合并前冲突审查与合并后基线确认',desc:'预置通用类型',builtin:true,enabled:true},
      {id:5,name:'自定义审批',icon:'⚙️',scenario:'其他未覆盖的通用业务场景，管理员可按需扩展',desc:'预置通用类型',builtin:true,enabled:true}
    ],
    tpls:[
      {id:1,name:'发布审批·两级',type_id:1,desc:'发布类标准流程：部门负责人 → 平台管理员终审',builtin:true,enabled:true,
        nodes:[
          {name:'部门负责人审批',kind:'approve',approver_mode:'role',approvers:['部门负责人'],pass:'all'},
          {name:'平台管理员终审',kind:'approve',approver_mode:'specify',approvers:['赵管'],pass:'all'}]},
      {id:2,name:'变更审批·单级',type_id:2,desc:'变更类标准流程：技术负责人单级审批',builtin:true,enabled:true,
        nodes:[{name:'技术负责人审批',kind:'approve',approver_mode:'role',approvers:['技术负责人'],pass:'all'}]},
      {id:3,name:'资产入库审批·复核',type_id:3,desc:'入库类标准流程：知识工程师复核 → 平台管理员终审',builtin:true,enabled:true,
        nodes:[
          {name:'知识工程师复核',kind:'approve',approver_mode:'role',approvers:['知识工程师'],pass:'all'},
          {name:'平台管理员终审',kind:'approve',approver_mode:'specify',approvers:['赵管'],pass:'all'}]},
      {id:4,name:'分支合并审批·自动路由',type_id:4,desc:'合并类标准流程：部门负责人审批 → 抄送申请人',builtin:true,enabled:true,
        nodes:[
          {name:'部门负责人审批',kind:'approve',approver_mode:'role',approvers:['部门负责人'],pass:'all'},
          {name:'抄送申请人',kind:'cc',approvers:['发起人'],pass:'all'}]}
    ],
    runs:[
      {id:1,title:'「宽带载荷方案」模型正式发布',type_id:1,tpl_id:1,applicant:'王工',time:'2026-08-11 09:20',status:'running',node_idx:0,content:'模型已通过内部评审，申请正式发布至 release 基线。',
        hist:[{node:'发起申请',by:'王工',act:'提交',time:'2026-08-11 09:20',comment:''}]},
      {id:2,title:'分支 dev/bw-antenna → main 合并请求',type_id:4,tpl_id:4,applicant:'李工',time:'2026-08-11 10:05',status:'running',node_idx:0,content:'天线载荷分支已开发完成，申请合并至主分支。',
        hist:[{node:'发起申请',by:'李工',act:'提交',time:'2026-08-11 10:05',comment:''}]},
      {id:3,title:'新增实体「宽带载荷」入库',type_id:3,tpl_id:3,applicant:'王工',time:'2026-08-10 16:40',status:'approved',node_idx:2,content:'抽取候选确认后申请入库。',
        hist:[{node:'发起申请',by:'王工',act:'提交',time:'2026-08-10 16:40',comment:''},{node:'知识工程师复核',by:'李工',act:'通过',time:'2026-08-10 17:02',comment:'材料完整'},{node:'平台管理员终审',by:'赵管',act:'通过',time:'2026-08-10 17:15',comment:'同意入库'}]},
      {id:4,title:'报告「行业调研」技能发布',type_id:1,tpl_id:1,applicant:'赵管',time:'2026-08-10 14:12',status:'rejected',node_idx:0,content:'技能包开发完成，申请发布。',
        hist:[{node:'发起申请',by:'赵管',act:'提交',time:'2026-08-10 14:12',comment:''},{node:'部门负责人审批',by:'王工',act:'驳回',time:'2026-08-10 14:40',comment:'文档缺失'}]},
      {id:5,title:'变更请求 ECR-026：热控指标调整',type_id:2,tpl_id:2,applicant:'李工',time:'2026-08-11 11:30',status:'approved',node_idx:1,content:'热控指标由 50℃ 调整为 55℃。',
        hist:[{node:'发起申请',by:'李工',act:'提交',time:'2026-08-11 11:30',comment:''},{node:'技术负责人审批',by:'王工',act:'通过',time:'2026-08-11 11:48',comment:'影响可控'}]}
    ]
  };
  apSave();
  return AP;
}
function apSave(){ try{ localStorage.setItem(AP_KEY, JSON.stringify(AP)); }catch(e){} }

function loadApprovalTab(id){
  apEnsure();
  if(id==='ap-type') renderApprovalTypes();
  else if(id==='ap-tpl') renderApprovalTpls();
  else if(id==='ap-new') renderApprovalNewForm();
  else if(id==='ap-todo') renderApprovalTodo();
  else if(id==='ap-rec') renderApprovalRecs();
}

// ── 工具函数 ──
function apTypeOf(id){ return AP.types.find(x=>x.id===id); }
function apTplOf(id){ return AP.tpls.find(x=>x.id===id); }
function apTypeName(id){ const t=apTypeOf(id); return t?t.name:'-'; }
function apTypeIcon(id){ const t=apTypeOf(id); return t?t.icon:'📄'; }
function apNodeLabel(n){ return n.kind==='approve'?'✅ 审批':n.kind==='cc'?'📨 抄送':'🚩 起始'; }
function apNodeApproverText(n){
  if(!n) return '';
  if(n.kind==='cc') return '抄送：'+((n.approvers||[]).join('、')||'相关人');
  if(n.approver_mode==='role') return '角色：'+(n.approvers||[]).join('、');
  if(n.approver_mode==='starter') return '发起人自选';
  return '人员：'+((n.approvers||[]).join('、')||'待指定');
}
function apNodeInvolves(n, run){
  if(!n) return false;
  const me=apMe();
  if(n.approver_mode==='specify') return (n.approvers||[]).includes(me);
  if(n.approver_mode==='role') return (n.approvers||[]).some(r=>AP_ROLE_USERS[r]===me);
  if(n.approver_mode==='starter') return run.applicant===me;
  return false;
}
function apStatusBadge(s){
  if(s==='running') return '<span class="st w">审批中</span>';
  if(s==='approved') return '<span class="st ok">已通过</span>';
  if(s==='rejected') return '<span class="st r">已驳回</span>';
  return '<span class="st g">已撤销</span>';
}
function apCurNode(run){ const t=apTplOf(run.tpl_id); return t?t.nodes[run.node_idx]:null; }

// ── Tab1 审批类型 ──
function renderApprovalTypes(){
  const kw=(document.getElementById('ap-type-search')?.value||'').trim().toLowerCase();
  let list=AP.types;
  if(kw) list=list.filter(t=>(t.name||'').toLowerCase().includes(kw)||(t.scenario||'').toLowerCase().includes(kw));
  const tb=document.getElementById('ap-type-table');
  tb.innerHTML=`<table class="t"><tr><th>类型</th><th>适用场景（用于哪些场景）</th><th>说明</th><th>状态</th><th>来源</th><th>操作</th></tr>`+
    (list.length?list.map(t=>`<tr>
      <td><b>${t.icon||'📄'} ${esc(t.name)}</b></td>
      <td style="max-width:380px;">${esc(t.scenario||'-')}</td>
      <td>${esc(t.desc||'-')}</td>
      <td>${t.enabled?'<span class="st ok">启用</span>':'<span class="st g">停用</span>'}</td>
      <td>${t.builtin?'<span class="badge">预置</span>':'<span class="badge b">自定义</span>'}</td>
      <td style="white-space:nowrap;">
        <button class="btn sm ghost" onclick="editApprovalType(${t.id})">编辑</button>
        <button class="btn sm ghost" onclick="toggleApprovalType(${t.id})">${t.enabled?'停用':'启用'}</button>
        ${t.builtin?'':`<button class="btn sm red" onclick="delApprovalType(${t.id})">删除</button>`}
      </td>
    </tr>`).join(''):'<tr><td colspan="6" style="text-align:center;color:var(--mut);padding:16px;">无匹配审批类型</td></tr>')+`</table>`;
}
function editApprovalType(id){
  const t=id?apTypeOf(id):null;
  _apTypeEdit=id||null;
  showModal('approval_type');
  document.getElementById('apt-form-title').textContent = t?'✏️ 编辑审批类型':'＋ 新增审批类型';
  document.getElementById('apt-id').value = t?t.id:'';
  document.getElementById('apt-name').value = t?t.name:'';
  document.getElementById('apt-icon').value = t?t.icon||'':'';
  document.getElementById('apt-scenario').value = t?t.scenario:'';
  document.getElementById('apt-desc').value = t?t.desc||'':'';
  document.getElementById('apt-enabled').value = t?(t.enabled?'1':'0'):'1';
}
function saveApprovalType(){
  const name=(document.getElementById('apt-name').value||'').trim();
  const scenario=(document.getElementById('apt-scenario').value||'').trim();
  if(!name){ toast('类型名称必填'); return; }
  if(!scenario){ toast('请填写适用场景（用于哪些场景）'); return; }
  const id=document.getElementById('apt-id').value;
  const icon=(document.getElementById('apt-icon').value||'').trim();
  const desc=(document.getElementById('apt-desc').value||'').trim();
  const enabled=document.getElementById('apt-enabled').value==='1';
  if(id){ const t=apTypeOf(parseInt(id)); if(t){ Object.assign(t,{name,icon,scenario,desc,enabled}); toast('审批类型已更新'); } }
  else{ AP.types.push({id:AP.seq.type++,name,icon,scenario,desc,builtin:false,enabled}); toast('审批类型已创建'); }
  apSave(); closeModal(); renderApprovalTypes();
}
function toggleApprovalType(id){ const t=apTypeOf(id); if(!t) return; t.enabled=!t.enabled; apSave(); toast(t.enabled?'已启用':'已停用'); renderApprovalTypes(); }
async function delApprovalType(id){
  const t=apTypeOf(id); if(!t) return;
  if(AP.tpls.some(tp=>tp.type_id===id)){ toast('该类型下存在流程模板，无法删除（可停用）'); return; }
  if(!(await confirmDialog(`确认删除审批类型「${t.name}」？`))) return;
  AP.types=AP.types.filter(x=>x.id!==id); apSave(); toast('已删除'); renderApprovalTypes();
}

// ── Tab2 流程模板 ──
function renderApprovalTpls(){
  const tb=document.getElementById('ap-tpl-table');
  tb.innerHTML=`<table class="t"><tr><th>模板</th><th>审批类型</th><th>流程节点</th><th>状态</th><th>来源</th><th>操作</th></tr>`+
    (AP.tpls.length?AP.tpls.map(tp=>`<tr>
      <td><b>${esc(tp.name)}</b><br><small style="color:var(--mut);">${esc(tp.desc||'')}</small></td>
      <td>${apTypeIcon(tp.type_id)} ${esc(apTypeName(tp.type_id))}</td>
      <td><div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">${tp.nodes.map((n,i)=>`<span class="st b" title="${esc(apNodeApproverText(n))}">${i+1}. ${apNodeLabel(n)}</span>`).join('<span style="color:var(--mut);font-size:10px;">→</span>')}</div></td>
      <td>${tp.enabled?'<span class="st ok">启用</span>':'<span class="st g">停用</span>'}</td>
      <td>${tp.builtin?'<span class="badge">预置</span>':'<span class="badge b">自定义</span>'}</td>
      <td style="white-space:nowrap;">
        <button class="btn sm ghost" onclick="editApprovalTpl(${tp.id})">编辑</button>
        <button class="btn sm ghost" onclick="toggleApprovalTpl(${tp.id})">${tp.enabled?'停用':'启用'}</button>
        ${tp.builtin?'':`<button class="btn sm red" onclick="delApprovalTpl(${tp.id})">删除</button>`}
      </td>
    </tr>`).join(''):'<tr><td colspan="6" style="text-align:center;color:var(--mut);padding:16px;">暂无流程模板，点击右上角「＋ 新建模板」</td></tr>')+`</table>`;
}
function editApprovalTpl(id){
  const t=id?apTplOf(id):null;
  const firstType=AP.types.find(x=>x.enabled);
  _apTplDraft=t?{id:t.id,name:t.name,type_id:t.type_id,desc:t.desc||'',enabled:t.enabled,nodes:(t.nodes||[]).map(n=>({...n,approvers:(n.approvers||[]).join(', ')}))}:{id:null,name:'',type_id:firstType?firstType.id:1,desc:'',enabled:true,nodes:[]};
  showModal('approval_tpl');
  document.getElementById('aptpl-form-title').textContent = t?'✏️ 编辑流程模板':'＋ 新建流程模板';
  document.getElementById('aptpl-id').value = t?t.id:'';
  document.getElementById('aptpl-name').value = _apTplDraft.name;
  document.getElementById('aptpl-desc').value = _apTplDraft.desc;
  document.getElementById('aptpl-enabled').value = _apTplDraft.enabled?'1':'0';
  const sel=document.getElementById('aptpl-type');
  sel.innerHTML=AP.types.map(x=>`<option value="${x.id}">${x.icon||'📄'} ${esc(x.name)}</option>`).join('');
  sel.value=String(_apTplDraft.type_id);
  apTplRenderNodes();
}
function apTplNodeRow(n,i){
  const cfgVisible = n.kind!=='start';
  return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:#fbfcfe;">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:${cfgVisible?'6px':'0'};">
      <span class="st b" style="flex:none;">${i+1}</span>
      <input id="apn-${i}-name" value="${esc(n.name)}" placeholder="节点名称" style="flex:1;min-width:88px;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
      <select id="apn-${i}-kind" onchange="apTplNodeKind(${i})" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
        <option value="start" ${n.kind==='start'?'selected':''}>🚩 起始</option>
        <option value="approve" ${n.kind==='approve'?'selected':''}>✅ 审批</option>
        <option value="cc" ${n.kind==='cc'?'selected':''}>📨 抄送</option>
      </select>
      <button class="btn sm ghost" title="上移" onclick="apTplMoveNode(${i},-1)" ${i===0?'disabled':''}>↑</button>
      <button class="btn sm ghost" title="下移" onclick="apTplMoveNode(${i},1)" ${i===_apTplDraft.nodes.length-1?'disabled':''}>↓</button>
      <button class="btn sm red" title="删除节点" onclick="apTplDelNode(${i})">🗑</button>
    </div>
    <div id="apn-${i}-cfg" style="display:${cfgVisible?'flex':'none'};gap:6px;align-items:center;flex-wrap:wrap;">
      <select id="apn-${i}-mode" onchange="apTplNodeMode(${i})" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
        <option value="specify" ${n.approver_mode==='specify'?'selected':''}>指定人员</option>
        <option value="role" ${n.approver_mode==='role'?'selected':''}>指定角色</option>
        <option value="starter" ${n.approver_mode==='starter'?'selected':''}>发起人自选</option>
      </select>
      <input id="apn-${i}-appr" value="${esc(n.approvers||'')}" placeholder="${n.approver_mode==='role'?'角色名，如：部门负责人':'人员姓名，逗号分隔'}" style="flex:1;min-width:100px;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
      <select id="apn-${i}-pass" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;display:${n.kind==='approve'?'':'none'};" title="通过规则">
        <option value="all" ${n.pass==='all'?'selected':''}>全部通过</option>
        <option value="any" ${n.pass==='any'?'selected':''}>任一通过</option>
      </select>
    </div>
  </div>`;
}
function apTplRenderNodes(){
  const box=document.getElementById('aptpl-nodes');
  if(!box) return;
  box.innerHTML=_apTplDraft.nodes.map(apTplNodeRow).join('')||'<div style="color:var(--mut);font-size:12px;background:var(--blue-l);border:1px dashed var(--blue-bd);border-radius:8px;padding:10px 12px;">暂无节点，点击「＋ 添加节点」开始编排流程</div>';
}
function apTplAddNode(){ _apTplDraft.nodes.push({name:'',kind:'approve',approver_mode:'specify',approvers:'',pass:'all'}); apTplRenderNodes(); }
function apTplDelNode(i){ _apTplDraft.nodes.splice(i,1); apTplRenderNodes(); }
function apTplMoveNode(i,d){ const arr=_apTplDraft.nodes; const j=i+d; if(j<0||j>=arr.length) return; const tmp=arr[i]; arr[i]=arr[j]; arr[j]=tmp; apTplRenderNodes(); }
function apTplNodeKind(i){
  const cfg=document.getElementById('apn-'+i+'-cfg');
  const k=document.getElementById('apn-'+i+'-kind').value;
  if(cfg) cfg.style.display=(k==='start')?'none':'flex';
  const p=document.getElementById('apn-'+i+'-pass');
  if(p) p.style.display=(k==='approve')?'':'none';
}
function apTplNodeMode(i){
  const m=document.getElementById('apn-'+i+'-mode').value;
  const a=document.getElementById('apn-'+i+'-appr');
  if(a) a.placeholder=m==='role'?'角色名，如：部门负责人':'人员姓名，逗号分隔';
}
function saveApprovalTpl(){
  const name=(document.getElementById('aptpl-name').value||'').trim();
  if(!name){ toast('模板名称必填'); return; }
  if(!_apTplDraft.nodes.length){ toast('请至少添加一个节点'); return; }
  const type_id=parseInt(document.getElementById('aptpl-type').value)||1;
  const nodes=_apTplDraft.nodes.map((n,i)=>{
    const nameEl=document.getElementById('apn-'+i+'-name');
    const kindEl=document.getElementById('apn-'+i+'-kind');
    const node={name:(nameEl?nameEl.value:'').trim()||'节点'+(i+1),kind:kindEl?kindEl.value:'approve'};
    if(node.kind!=='start'){
      const modeEl=document.getElementById('apn-'+i+'-mode');
      const apprEl=document.getElementById('apn-'+i+'-appr');
      const passEl=document.getElementById('apn-'+i+'-pass');
      node.approver_mode=modeEl?modeEl.value:'specify';
      node.approvers=(apprEl?apprEl.value:'').split(',').map(s=>s.trim()).filter(Boolean);
      if(node.kind==='approve') node.pass=passEl?passEl.value:'all';
    }
    return node;
  });
  if(!nodes.some(n=>n.kind==='approve')){ toast('流程中至少需要一个审批节点'); return; }
  const id=document.getElementById('aptpl-id').value;
  const desc=(document.getElementById('aptpl-desc').value||'').trim();
  const enabled=document.getElementById('aptpl-enabled').value==='1';
  if(id){ const t=apTplOf(parseInt(id)); if(t){ Object.assign(t,{name,type_id,desc,enabled,nodes}); toast('流程模板已更新'); } }
  else{ AP.tpls.push({id:AP.seq.tpl++,name,type_id,desc,builtin:false,enabled,nodes}); toast('流程模板已创建'); }
  apSave(); closeModal(); renderApprovalTpls();
}
function toggleApprovalTpl(id){ const t=apTplOf(id); if(!t) return; t.enabled=!t.enabled; apSave(); toast(t.enabled?'已启用':'已停用'); renderApprovalTpls(); }
async function delApprovalTpl(id){
  const t=apTplOf(id); if(!t) return;
  if(AP.runs.some(r=>r.tpl_id===id)){ toast('该模板已有审批单，无法删除（可停用）'); return; }
  if(!(await confirmDialog(`确认删除流程模板「${t.name}」？`))) return;
  AP.tpls=AP.tpls.filter(x=>x.id!==id); apSave(); toast('已删除'); renderApprovalTpls();
}

// ── Tab3 发起审批 ──
function renderApprovalNewForm(){
  const el=document.getElementById('ap-new-form');
  const enabledTypes=AP.types.filter(t=>t.enabled);
  if(!enabledTypes.length){ el.innerHTML='<div class="note">当前没有启用的审批类型，请先到「📋 审批类型」启用或新增。</div>'; return; }
  el.innerHTML=`
    <div style="max-width:580px;">
      <div class="form-row"><label>审批类型 <span style="color:var(--mut);font-size:11px;">（用于哪些场景）</span></label>
        <select id="apn-type" onchange="apNewTypeChanged()">${enabledTypes.map(t=>`<option value="${t.id}">${t.icon||'📄'} ${esc(t.name)}（${esc(t.scenario)}）</option>`).join('')}</select></div>
      <div class="form-row"><label>流程模板</label><select id="apn-tpl" onchange="apNewPreview()"></select></div>
      <div class="form-row"><label>审批标题</label><input id="apn-title" placeholder="如：「XX」模型正式发布"></div>
      <div class="form-row"><label>审批内容</label><textarea id="apn-content" placeholder="补充说明 / 变更原因 / 附件说明…"></textarea></div>
      <div id="apn-preview" style="font-size:11.5px;color:var(--mut);background:var(--blue-l);border-radius:6px;padding:8px 10px;margin-bottom:10px;"></div>
      <div class="form-actions" style="justify-content:flex-start;"><button class="btn" onclick="submitApproval()">🚀 提交审批</button><button class="btn ghost" onclick="renderApprovalNewForm()">清空</button></div>
    </div>`;
  apNewTypeChanged();
}
function apNewTypeChanged(){
  const tid=parseInt(document.getElementById('apn-type').value);
  const tpls=AP.tpls.filter(t=>t.type_id===tid&&t.enabled);
  const sel=document.getElementById('apn-tpl');
  sel.innerHTML=tpls.length?tpls.map(t=>`<option value="${t.id}">${esc(t.name)}（${t.nodes.length} 节点）</option>`).join(''):'<option value="">（该类型下暂无启用模板）</option>';
  apNewPreview();
}
function apNewPreview(){
  const pv=document.getElementById('apn-preview'); if(!pv) return;
  const tid=parseInt(document.getElementById('apn-type')?.value||0);
  const t=apTplOf(parseInt(document.getElementById('apn-tpl')?.value||0));
  const type=apTypeOf(tid);
  let chain='';
  if(t){ chain=t.nodes.map(n=>{ const who=n.kind==='cc'?'抄送'+((n.approvers||[]).join('、')||'相关人'):'审批：'+apNodeApproverText(n); return `<span class="st b" style="margin:0 2px;">${esc(n.name)}</span><small style="color:var(--mut);">${esc(who)}</small>`; }).join('<span style="color:var(--mut);margin:0 4px;">→</span>'); }
  pv.innerHTML=`<b>流程预览</b>：${type?esc(type.name):'-'}${t?' · '+esc(t.name):''}　发起人：<b>${esc(apMe())}</b><br>${chain||'请选择流程模板'}`;
}
function submitApproval(){
  const title=(document.getElementById('apn-title').value||'').trim();
  if(!title){ toast('请填写审批标题'); return; }
  const t=apTplOf(parseInt(document.getElementById('apn-tpl').value||0));
  if(!t){ toast('请选择流程模板'); return; }
  const content=(document.getElementById('apn-content').value||'').trim();
  const me=apMe();
  AP.runs.push({id:AP.seq.run++,title,type_id:t.type_id,tpl_id:t.id,applicant:me,time:apNow(),status:'running',node_idx:0,content,hist:[{node:'发起申请',by:me,act:'提交',time:apNow(),comment:content}]});
  apSave();
  toast('审批已提交');
  const todoTab=[...document.querySelectorAll('[data-tabgrp="ap"]')].find(s=>(s.getAttribute('onclick')||'').includes('ap-todo'));
  if(todoTab) tab(todoTab,'ap','ap-todo');
}

// ── Tab4 我的待办 ──
function renderApprovalTodo(){
  const me=apMe();
  const list=AP.runs.filter(r=>r.status==='running'&&apNodeInvolves(apCurNode(r),r));
  const badge=document.getElementById('ap-todo-badge'); if(badge) badge.textContent=list.length;
  const tb=document.getElementById('ap-todo-table');
  tb.innerHTML=`<table class="t"><tr><th>审批标题</th><th>类型</th><th>当前节点</th><th>发起人</th><th>发起时间</th><th>操作</th></tr>`+
    (list.length?list.map(r=>{ const n=apCurNode(r); return `<tr>
      <td><b>${esc(r.title)}</b><br><small style="color:var(--mut);">${esc((r.content||'').slice(0,60))}</small></td>
      <td>${apTypeIcon(r.type_id)} ${esc(apTypeName(r.type_id))}</td>
      <td><span class="st b">${esc(n?n.name:'-')}</span><br><small style="color:var(--mut);">${esc(apNodeApproverText(n||{}))}</small></td>
      <td>${esc(r.applicant)}</td>
      <td>${esc(r.time)}</td>
      <td style="white-space:nowrap;">
        <button class="btn sm" onclick="handleApproval(${r.id},'approve')">✓ 通过</button>
        <button class="btn sm red" onclick="handleApproval(${r.id},'reject')">✗ 驳回</button>
        <button class="btn sm ghost" onclick="viewApproval(${r.id})">详情</button>
      </td>
    </tr>`;}).join(''):'<tr><td colspan="6" style="text-align:center;color:var(--mut);padding:16px;">暂无待办审批</td></tr>')+`</table>`;
}
async function handleApproval(id, action){
  const r=AP.runs.find(x=>x.id===id); if(!r) return;
  const t=apTplOf(r.tpl_id); const n=t?t.nodes[r.node_idx]:null;
  const me=apMe();
  if(action==='approve'){
    const comment=await promptDialog({title:'审批通过',message:`通过「${r.title}」的节点「${n?n.name:''}」？`,value:'',placeholder:'审批意见（可空）',okText:'确认通过'});
    if(comment===null) return;
    r.hist.push({node:n?n.name:'',by:me,act:'通过',time:apNow(),comment});
    let idx=r.node_idx+1;
    while(t&&idx<t.nodes.length&&t.nodes[idx].kind!=='approve'){
      r.hist.push({node:t.nodes[idx].name,by:'系统',act:'抄送',time:apNow(),comment:''});
      idx++;
    }
    if(!t||idx>=t.nodes.length){ r.status='approved'; r.node_idx=t?t.nodes.length:0; toast('审批已完成（全部通过）'); }
    else{ r.node_idx=idx; toast('已通过，流转至下一节点'); }
  }else{
    const comment=await promptDialog({title:'审批驳回',message:`驳回「${r.title}」？`,value:'',placeholder:'驳回原因（建议填写）',okText:'确认驳回'});
    if(comment===null) return;
    r.hist.push({node:n?n.name:'',by:me,act:'驳回',time:apNow(),comment});
    r.status='rejected'; toast('已驳回');
  }
  apSave(); renderApprovalTodo(); renderApprovalRecs();
}

// ── Tab5 审批记录 ──
function renderApprovalRecs(){
  const st=document.getElementById('ap-rec-status')?.value||'';
  let list=[...AP.runs].filter(r=>!st||r.status===st).sort((a,b)=>b.id-a.id);
  const tb=document.getElementById('ap-rec-table');
  tb.innerHTML=`<table class="t"><tr><th>审批标题</th><th>类型</th><th>发起人</th><th>当前节点</th><th>时间</th><th>状态</th><th>操作</th></tr>`+
    (list.length?list.map(r=>{ const n=apCurNode(r); return `<tr>
      <td><b>${esc(r.title)}</b></td>
      <td>${apTypeIcon(r.type_id)} ${esc(apTypeName(r.type_id))}</td>
      <td>${esc(r.applicant)}</td>
      <td>${r.status==='running'&&n?esc(n.name):'—'}</td>
      <td>${esc(r.time)}</td>
      <td>${apStatusBadge(r.status)}</td>
      <td style="white-space:nowrap;">
        <button class="btn sm ghost" onclick="viewApproval(${r.id})">详情</button>
        ${r.status==='running'&&r.applicant===apMe()?`<button class="btn sm red" onclick="withdrawApproval(${r.id})">撤销</button>`:''}
      </td>
    </tr>`;}).join(''):'<tr><td colspan="7" style="text-align:center;color:var(--mut);padding:16px;">暂无审批记录</td></tr>')+`</table>`;
}
async function withdrawApproval(id){
  const r=AP.runs.find(x=>x.id===id); if(!r) return;
  if(!(await confirmDialog(`确认撤销审批「${r.title}」？`))) return;
  r.status='withdrawn';
  r.hist.push({node:apCurNode(r)?apCurNode(r).name:'',by:apMe(),act:'撤销',time:apNow(),comment:''});
  apSave(); toast('已撤销'); renderApprovalRecs();
}

// ── 审批详情（右侧滑出面板）──
function viewApproval(id){
  const r=AP.runs.find(x=>x.id===id); if(!r) return;
  const t=apTplOf(r.tpl_id); const type=apTypeOf(r.type_id);
  let nodeHtml='';
  if(t){
    nodeHtml=`<div style="font-size:12px;font-weight:600;color:var(--blue-d);margin:10px 0 4px;">🛤 流程节点（${t.nodes.length}）</div><div style="display:flex;flex-direction:column;gap:6px;">`+
      t.nodes.map((n,i)=>{
        let st={cls:'g',txt:'待审批'};
        if(r.status==='withdrawn'){ st={cls:'g',txt:'已撤销'}; }
        else if(i<r.node_idx){ st=n.kind==='cc'?{cls:'g',txt:'已抄送'}:{cls:'ok',txt:'已通过'}; }
        else if(i===r.node_idx){
          if(r.status==='rejected') st={cls:'r',txt:'已驳回'};
          else if(r.status==='running') st={cls:'w',txt:'进行中'};
          else st=n.kind==='cc'?{cls:'g',txt:'已抄送'}:{cls:'ok',txt:'已通过'};
        }
        const cfg=apNodeApproverText(n);
        return `<div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:#fbfcfe;display:flex;align-items:center;gap:8px;">
          <span class="st ${st.cls}" style="flex:none;">${st.txt}</span>
          <b style="flex:1;font-size:12.5px;">${i+1}. ${esc(n.name)}</b>
          <small style="color:var(--mut);">${esc(cfg)}</small>
        </div>`;
      }).join('')+`</div>`;
  }
  const histHtml=r.hist.map(h=>`<div style="padding:6px 0;border-bottom:1px dashed #e9edf3;display:flex;gap:8px;align-items:baseline;">
    <span class="st ${h.act==='驳回'?'r':h.act==='通过'?'ok':h.act==='抄送'?'g':'b'}" style="flex:none;">${h.act}</span>
    <span style="flex:1;font-size:12px;">${esc(h.node||'发起申请')} · <b>${esc(h.by)}</b>${h.comment?'：<span style="color:var(--mut);">'+esc(h.comment)+'</span>':''}</span>
    <small style="color:var(--mut);">${esc(h.time)}</small>
  </div>`).join('')||'<div class="mut" style="padding:6px 0;">暂无记录</div>';
  openPanel('✅ 审批详情', `
    <div style="font-size:12.5px;">
      <div style="font-size:14px;font-weight:600;color:var(--blue-d);">${esc(r.title)}</div>
      <div style="margin:6px 0 0;color:var(--mut);">${type?type.icon+' '+esc(type.name):''}${t?' · '+esc(t.name):''}　状态：${apStatusBadge(r.status)}</div>
      <div style="margin:4px 0 0;color:var(--mut);">发起人：<b>${esc(r.applicant)}</b>　时间：${esc(r.time)}</div>
      ${r.content?`<div style="margin:8px 0 0;background:var(--blue-l);border-radius:6px;padding:8px 10px;color:var(--blue-d);">${esc(r.content)}</div>`:''}
      ${nodeHtml}
      <div style="font-size:12px;font-weight:600;color:var(--blue-d);margin:12px 0 4px;">🕘 流转记录（${r.hist.length}）</div>
      ${histHtml}
    </div>`);
}

// ── UI 清单②：Ctrl+K 全局搜索（Codex 理念：会话/本体类型/实体统一搜索）──
let __gkOpen = false, __gkTimer = null, __gkData = [];
document.addEventListener('keydown', e => {
  if((e.ctrlKey||e.metaKey) && (e.key||'').toLowerCase()==='k'){
    e.preventDefault();
    __gkOpen ? closeGlobalSearch() : openGlobalSearch();
  }
  if(e.key==='Escape' && __gkOpen) closeGlobalSearch();
});
