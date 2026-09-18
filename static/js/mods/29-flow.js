/* 流程编排：画布 / 运行 / 监控 / HIL
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 17706-18754  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function flowNodeMeta(type){ return FLOW_NODE_TYPES[type] || FLOW_NODE_TYPES.llm; }

function initFlowCanvas() {
  renderFlowPalette();
  loadFlowNodeOptions();  // 预加载字段候选源（LLM Provider/Agent/Skill/MCP）
  const wrap = document.getElementById('flow-canvas-wrap');
  if(!wrap) return;
  if(wrap._flowEventsBound) return;  // 防重复注册：loadFlows 每次刷新都会调用本函数，事件只绑一次（修复拖入出现两个）
  wrap._flowEventsBound = true;
  wrap.addEventListener('dragover', e=>e.preventDefault());
  wrap.addEventListener('drop', e=>{
    e.preventDefault();
    const t = e.dataTransfer.getData('text/plain');
    const r = wrap.getBoundingClientRect();
    const x = Math.max(0,e.clientX - r.left - 70), y = Math.max(0,e.clientY - r.top - 35);
    // Agent 节点：拖入预设 config.agent（palette 拖拽类型为 agent:<intent名>）
    if(t.startsWith('agent:')){
      flowAddNode('agent', x, y, {agent: t.slice(6)});
      return;
    }
    if(!FLOW_NODE_TYPES[t]) return;
    flowAddNode(t, x, y, {});
  });
}
// 建模主链路节点面板（2026-08-14 优化）：只展示已定义的 Agent，其他节点类型隐藏
function flowPaletteCard(type, label, sub){
  return `<div draggable="true" ondragstart="event.dataTransfer.setData('text/plain','${type}')"
      style="border:1px solid var(--line);border-radius:8px;padding:9px 10px;margin-bottom:8px;cursor:grab;background:#fff;font-size:12px;">
      <div style="font-weight:600;display:flex;align-items:center;gap:6px;"><span style="width:9px;height:9px;border-radius:3px;background:#7F77DD;display:inline-block;"></span>🤖 ${label}</div>
      <div style="font-size:10.5px;color:var(--mut);margin-top:3px;">${sub}</div>
    </div>`;
}
async function renderFlowPalette() {
  const el = document.getElementById('flow-palette');
  if(!el) return;
  el.innerHTML = '<div style="font-size:10.5px;color:var(--mut);">加载 Agent…</div>';
  try {
    const agents = _agentsCache && _agentsCache.length ? _agentsCache : await api('/api/studio/agents');
    if(!agents || !agents.length){
      el.innerHTML = '<div style="font-size:11px;color:var(--mut);">暂无可编排的 Agent，请先到「Agent 管理」定义。</div>';
      return;
    }
    el.innerHTML =
      `<div style="font-size:10.5px;color:var(--mut);margin-bottom:8px;font-weight:600;">🤖 Agent 节点（拖入画布）</div>` +
      agents.map(a=>flowPaletteCard('agent:'+a.name, a.display_name||a.name, a.name)).join('');
  } catch(e) {
    el.innerHTML = '<div style="font-size:11px;color:var(--mut);">Agent 列表加载失败</div>';
  }
}
function flowAddNode(type, x, y, preset) {
  const id = 'n' + (flowCanvas.nextId++);
  const meta = flowNodeMeta(type);
  flowCanvas.nodes.push({id, type, label: meta.label, config: preset||{}, x: Math.round(x), y: Math.round(y)});
  flowCanvas.sel = id;
  renderFlowCanvas(); flowRenderProps();
  document.getElementById('flow-canvas-hint').style.display = 'none';
}
function renderFlowCanvas() {
  const svg = document.getElementById('flow-canvas-svg');
  const box = document.getElementById('flow-canvas-nodes');
  if(!svg || !box) return;
  let s = '';
  flowCanvas.edges.forEach((e,i)=>{
    const a = flowCanvas.nodes.find(n=>n.id===e.source), b = flowCanvas.nodes.find(n=>n.id===e.target);
    if(!a||!b) return;
    const x1 = a.x+70, y1 = a.y+68, x2 = b.x+70, y2 = b.y;
    const mx = (x1+x2)/2;
    const selE = flowCanvas.selEdge===i;
    const condTxt = (e.when?` when=${e.when}`:'') + (e.condition?` if ${e.condition}`:'') + (e.loop?' 🔄':'');
    s += `<path d="M${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}" fill="none"
      stroke="${selE?'#E24B4A':(e.loop?'#BA7517':'#888780')}" stroke-width="${selE?2.4:1.4}" stroke-dasharray="${e.loop?'6,4':undefined}"
      style="pointer-events:stroke;cursor:pointer;"
      onclick="flowSelEdge(${i})" title="${e.source} → ${e.target}${condTxt}（点击编辑连线）"/>`;
    if(condTxt){
      s += `<text x="${mx}" y="${(y1+y2)/2-4}" font-size="9.5" fill="${selE?'#E24B4A':'#888780'}" text-anchor="middle"
        style="pointer-events:none;">${esc(condTxt.trim())}</text>`;
    }
  });
  if(flowCanvas.link) {
    const {x1,y1,x2,y2} = flowCanvas.link;
    const mx=(x1+x2)/2;
    s += `<path d="M${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}" fill="none" stroke="#378ADD" stroke-width="1.6" stroke-dasharray="5,3"/>`;
  }
  svg.innerHTML = s;
  box.innerHTML = flowCanvas.nodes.map(n=>{
    const meta = flowNodeMeta(n.type);
    const sel = flowCanvas.sel===n.id;
    const sub = n.type==='llm'?(n.config.prompt||''):(n.config.tool||n.config.agent||n.config.skill||n.config.endpoint||'');
    return `<div data-fn="${n.id}" onmousedown="flowNodeDown(event,'${n.id}')"
      style="position:absolute;left:${n.x}px;top:${n.y}px;width:140px;border:1.5px solid ${sel?'#378ADD':meta.color};border-radius:10px;background:#fff;cursor:move;font-size:11.5px;box-shadow:${sel?'0 0 0 2px var(--blue-l)':'0 1px 3px rgba(0,0,0,.08)'};">
      <div style="display:flex;align-items:center;gap:6px;padding:5px 8px;background:${meta.color};color:#fff;border-radius:8px 8px 0 0;font-weight:600;font-size:11px;">
        <span>${meta.icon}</span>${esc(n.label)}<span style="flex:1"></span>
        <span onclick="event.stopPropagation();flowDeleteNode('${n.id}')" style="cursor:pointer;opacity:.85;" title="删除节点">✕</span>
      </div>
      <div style="padding:5px 8px;font-size:10px;color:var(--mut);min-height:22px;max-height:34px;overflow:hidden;">${esc(String(sub||'未配置'))}</div>
      <div onmousedown="flowLinkDown(event,'${n.id}')" title="拖出连线到目标节点"
        style="position:absolute;left:50%;bottom:-7px;width:14px;height:14px;margin-left:-7px;border-radius:50%;background:${meta.color};border:2px solid #fff;cursor:crosshair;box-shadow:0 0 0 1px rgba(0,0,0,.2);"></div>
    </div>`;
  }).join('');
}
function flowNodeDown(ev, id) {
  ev.preventDefault(); ev.stopPropagation();
  flowCanvas.sel = id; flowCanvas.selEdge = null; renderFlowCanvas();
  const wrap = document.getElementById('flow-canvas-wrap');
  const n = flowCanvas.nodes.find(x=>x.id===id);
  if(!n) return;
  const r = wrap.getBoundingClientRect();
  const ox = ev.clientX - r.left - n.x, oy = ev.clientY - r.top - n.y;
  const sx = ev.clientX, sy = ev.clientY;
  let moved = false;
  function mv(e2){
    if(Math.abs(e2.clientX-sx)>3 || Math.abs(e2.clientY-sy)>3) moved = true;
    n.x = Math.max(0, Math.min(r.width-140, e2.clientX - r.left - ox));
    n.y = Math.max(0, Math.min(r.height-80, e2.clientY - r.top - oy));
    renderFlowCanvas();
  }
  function up(){
    document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up);
    if(!moved) flowRenderProps();  // 点击（非拖拽）→ 右侧弹窗配置属性
  }
  document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
}
function flowLinkDown(ev, srcId) {
  ev.preventDefault(); ev.stopPropagation();
  const wrap = document.getElementById('flow-canvas-wrap');
  const r = wrap.getBoundingClientRect();
  const src = flowCanvas.nodes.find(n=>n.id===srcId);
  if(!src) return;
  flowCanvas.link = {src: srcId, x1: src.x+70, y1: src.y+68, x2: src.x+70, y2: src.y+68};
  function mv(e2){
    flowCanvas.link.x2 = e2.clientX - r.left; flowCanvas.link.y2 = e2.clientY - r.top;
    renderFlowCanvas();
  }
  function up(e2){
    document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up);
    const tx = e2.clientX - r.left, ty = e2.clientY - r.top;
    const tgt = flowCanvas.nodes.find(n=>n.id!==srcId && tx>=n.x && tx<=n.x+140 && ty>=n.y && ty<=n.y+70);
    flowCanvas.link = null;
    if(tgt && !flowCanvas.edges.some(e=>e.source===srcId && e.target===tgt.id)){
      flowCanvas.edges.push({source:srcId, target:tgt.id});
      toast(`${srcId} → ${tgt.id} 已连接`);
    }
    renderFlowCanvas();
  }
  document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
}
function flowSelEdge(i){
  if(!flowCanvas.edges[i]) return;
  flowCanvas.selEdge = (flowCanvas.selEdge===i) ? null : i;
  if(flowCanvas.selEdge!==null) flowCanvas.sel = null;
  renderFlowCanvas(); flowRenderProps();
}
async function flowDeleteNode(id){
  if(!(await confirmDialog('删除该节点？关联连线一并删除'))) return;
  flowCanvas.nodes = flowCanvas.nodes.filter(n=>n.id!==id);
  flowCanvas.edges = flowCanvas.edges.filter(e=>e.source!==id && e.target!==id);
  if(flowCanvas.sel===id) flowCanvas.sel=null;
  renderFlowCanvas(); flowRenderProps();
}
// ── 节点字段候选源：从已定义数据（LLM Provider/Agent/Skill/MCP Server）加载，支持「选择 or 手写」──
let flowNodeOpts = null;   // {providers, models, agents, skills, mcp_servers, mcp_tools}
async function loadFlowNodeOptions(){
  if(flowNodeOpts) return;
  try{
    const [providers, agents, skills, mcp] = await Promise.all([
      api('/api/llm/providers'), api('/api/studio/agents'),
      api('/api/studio/skills'), api('/api/studio/mcp-servers')
    ]);
    const models = [...new Set((providers||[]).map(p=>p.model_name).filter(Boolean))];
    const mcpTools = [...new Set((mcp||[]).flatMap(s=>{ try{ return JSON.parse(s.tools||'[]'); }catch(e){ return []; } }))];
    flowNodeOpts = {
      providers:   (providers||[]).map(p=>({value:String(p.id), label:`${p.id} · ${p.name} (${p.model_name||'-'})`})),
      models:      models.map(m=>({value:m, label:m})),
      agents:      (agents||[]).map(a=>({value:a.name, label:`${a.name} · ${a.display_name||''}`})),
      skills:      (skills||[]).map(s=>({value:s.name, label:`${s.name} · ${(s.description||s.status||'')}`})),
      mcp_servers: (mcp||[]).map(s=>({value:s.endpoint, label:`${s.name} (${s.transport||'sse'})`})),
      mcp_tools:   mcpTools.map(t=>({value:t, label:t})),
    };
  }catch(e){ flowNodeOpts = {}; }
}
function flowDatalistHtml(source){
  if(!flowNodeOpts || !flowNodeOpts[source]) return '';
  const id = 'fl-' + source;
  if(document.getElementById(id)) return '';
  const d = document.createElement('datalist'); d.id = id;
  flowNodeOpts[source].forEach(o=>{ const op=document.createElement('option'); op.value=o.value; op.label=o.label||o.value; d.appendChild(op); });
  document.body.appendChild(d);
  return '';
}
function flowOptCustomToggle(k){
  const sel = document.getElementById('fp-' + k);
  const cus = document.getElementById('fp-' + k + '-custom');
  if(!sel || !cus) return;
  if(sel.value === '__custom'){ cus.style.display = 'block'; cus.focus(); }
  else cus.style.display = 'none';
}
function flowRenderProps(){
  const title = document.getElementById('panel-title');
  const body = document.getElementById('panel-body');
  if(!title || !body) return;
  // ── 边模式：编辑 when / condition / loop ──
  if(flowCanvas.selEdge!==null && flowCanvas.edges[flowCanvas.selEdge]){
    const e = flowCanvas.edges[flowCanvas.selEdge];
    const cond = e.condition||'';
    title.textContent = `连线 ${e.source} → ${e.target}`;
    body.innerHTML = `<div style="font-size:11px;color:var(--mut);margin-bottom:8px;">条件连线：只有条件满足时下游节点才会执行；<b>循环连线</b>（勾选）使 if 条件命中时回跳到上游节点重做（需配合 if 节点 max_iterations 防死循环）。</div>
      <div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">分支（when，if 节点出边用）</label>
        <select id="fp-when" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;">
          <option value="">无条件（始终激活）</option>
          <option value="true" ${e.when==='true'?'selected':''}>仅当上游 if=true</option>
          <option value="false" ${e.when==='false'?'selected':''}>仅当上游 if=false</option>
        </select></div>
      <div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">条件表达式（可选，如 {{n1.data.result}} contains 冲突）</label>
        <input id="fp-cond" value="${esc(cond)}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;font-family:Consolas,monospace;"></div>
      <div style="margin-bottom:8px;display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--mut);">
        <input type="checkbox" id="fp-loop" ${e.loop?'checked':''} style="accent-color:var(--blue-d);"> 🔄 循环连线（条件命中 → 回跳上游节点重做）
      </div>
      <div style="display:flex;gap:6px;margin-top:6px;">
        <button class="btn sm" onclick="flowApplyEdgeProps()">✔ 应用</button>
        <button class="btn sm red" onclick="flowDeleteEdge('${e.source}','${e.target}')">🗑 删除连线</button>
      </div>`;
    openPanel(title.textContent, body.innerHTML);
    return;
  }
  // ── 节点模式 ──
  const n = flowCanvas.nodes.find(x=>x.id===flowCanvas.sel);
  if(!n){ closePanel(); return; }
  const meta = flowNodeMeta(n.type);
  title.textContent = n.id + ' · ' + meta.label;
  body.innerHTML = `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">节点名称</label>
    <input id="fp-label" value="${esc(n.label)}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"></div>` +
    meta.fields.map(f=>{
      const k = f.k, v = (n.config[k]!==undefined&&n.config[k]!==null) ? (typeof n.config[k]==='object'?JSON.stringify(n.config[k]):String(n.config[k])) : '';
      if(f.type==='textarea') return `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">${f.label}</label><textarea id="fp-${k}" style="width:100%;min-height:64px;border:1px solid var(--line);border-radius:6px;padding:5px 7px;font-size:11.5px;font-family:Consolas,monospace;">${esc(v)}</textarea></div>`;
      if(f.type==='select') return `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">${f.label}</label><select id="fp-${k}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;"><option value="">— 选择 —</option>${f.options.map(o=>`<option ${String(o)===v?'selected':''}>${esc(o)}</option>`).join('')}</select></div>`;
      if(f.source){  // 从已定义数据选择 or 手写
        const opts = (flowNodeOpts && flowNodeOpts[f.source]) || [];
        if(f.multi){  // 多值逗号分隔 → datalist 候选提示 + 自由输入
          flowDatalistHtml(f.source);
          return `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">${f.label}<span style="color:var(--blue-d);">（可下拉选或手写，多值逗号分隔）</span></label><input id="fp-${k}" value="${esc(v)}" list="fl-${f.source}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"></div>`;
        }
        if(!opts.length) return `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">${f.label}</label><input id="fp-${k}" value="${esc(v)}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"></div>`;
        const inList = v && opts.some(o=>String(o.value)===String(v));
        return `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">${f.label}<span style="color:var(--blue-d);">（可选已定义 or 手写）</span></label>
          <select id="fp-${k}" onchange="flowOptCustomToggle('${k}')" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;">
            <option value="">— 默认/未设置 —</option>
            ${opts.map(o=>`<option value="${esc(o.value)}" ${String(o.value)===String(v)?'selected':''}>${esc(o.label)}</option>`).join('')}
            <option value="__custom" ${(v&&!inList)?'selected':''}>✏️ 自定义输入…</option>
          </select>
          <input id="fp-${k}-custom" value="${(v&&!inList)?esc(v):''}" placeholder="手写值" style="display:${(v&&!inList)?'block':'none'};width:100%;margin-top:4px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"></div>`;
      }
      return `<div style="margin-bottom:8px;"><label style="font-size:11px;color:var(--mut);">${f.label}</label><input id="fp-${k}" value="${esc(v)}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"></div>`;
    }).join('') + `<div style="display:flex;gap:6px;margin-top:6px;">
    <button class="btn sm" onclick="flowApplyProps()">✔ 应用</button>
    <button class="btn sm red" onclick="flowDeleteNode('${n.id}')">🗑 删除节点</button>
  </div>`;
  openPanel(title.textContent, body.innerHTML);
}
function flowApplyEdgeProps(){
  const e = flowCanvas.edges[flowCanvas.selEdge];
  if(!e) return;
  const w = document.getElementById('fp-when');
  const c = document.getElementById('fp-cond');
  const lp = document.getElementById('fp-loop');
  if(w) e.when = w.value || undefined;
  if(c) e.condition = c.value.trim() || undefined;
  if(lp) e.loop = lp.checked || undefined;
  renderFlowCanvas(); flowRenderProps();
  toast('连线条件已应用');
}
function flowApplyProps(){
  const n = flowCanvas.nodes.find(x=>x.id===flowCanvas.sel);
  if(!n) return;
  const lab = document.getElementById('fp-label'); if(lab) n.label = lab.value.trim() || flowNodeMeta(n.type).label;
  flowNodeMeta(n.type).fields.forEach(f=>{
    const el = document.getElementById('fp-'+f.k);
    if(!el) return;
    let v = el.value;
    if(f.source && !f.multi){  // 下拉+自定义双模式：__custom → 取手写输入框
      if(v === '__custom'){
        const cus = document.getElementById('fp-'+f.k+'-custom');
        v = cus ? cus.value : '';
      }
      if(f.k === 'provider_id' && v === '') v = '';
    }
    if(f.type==='json'){
      if(typeof v === 'string'){
        const s = v.trim();
        if(s === ''){ v = undefined; }
        else if(s[0]==='[' || s[0]==='{'){
          try{ v = JSON.parse(s); }catch(e){ /* 非 JSON（如 {{节点.字段}} 模板引用/逗号分隔）保留原字符串，由后端运行时解析 */ }
        }
        /* 其余（模板引用、逗号分隔字符串）直接透传 */
      }
    }
    n.config[f.k] = v;
  });
  renderFlowCanvas(); flowRenderProps();
  toast('节点配置已应用');
}
function flowDeleteEdge(src,tgt){
  flowCanvas.edges = flowCanvas.edges.filter(e=>!(e.source===src && e.target===tgt));
  flowCanvas.selEdge = null;
  renderFlowCanvas(); flowRenderProps();
}
// ── 画布 ↔ 定义（nodes/edges JSON）──
function flowCanvasToDef(){
  return {nodes: flowCanvas.nodes.map(({id,type,label,config})=>({id,type,label,config})),
          edges: flowCanvas.edges.map(e=>({source:e.source,target:e.target,when:e.when,condition:e.condition,loop:e.loop}))};
}
function flowDefToCanvas(def){
  flowCanvas.nodes = (def.nodes||[]).map((n,i)=>({...n, x:60+(i%3)*200, y:40+Math.floor(i/3)*140}));
  flowCanvas.edges = (def.edges||[]).map(e=>({...e}));
  flowCanvas.nextId = flowCanvas.nodes.length+1;
  flowCanvas.sel = null; flowCanvas.selEdge = null;
  renderFlowCanvas(); flowRenderProps();
  const h=document.getElementById('flow-canvas-hint'); if(h) h.style.display = flowCanvas.nodes.length?'none':'';
}
function flowToggleJson(){
  openFlowRunPanel('info');  // JSON 编辑区在「流程信息」Tab 内，切换时同步展开
  const ta = document.getElementById('flow-json');
  if(!ta) return;
  if(ta.style.display==='none'){
    ta.style.display='block';
    ta.value = JSON.stringify(flowCanvasToDef(), null, 2);
  } else {
    try{ flowDefToCanvas(JSON.parse(ta.value)); ta.style.display='none'; toast('已从 JSON 同步到画布'); }
    catch(e){ toast('JSON 格式错误：' + e.message); }
  }
}
async function flowCanvasClear(){
  if(!(await confirmDialog('清空画布？'))) return;
  flowCanvas = {nodes:[], edges:[], sel:null, nextId:1, link:null};
  renderFlowCanvas(); flowRenderProps();
  const h=document.getElementById('flow-canvas-hint'); if(h) h.style.display='';
}
// ── 流程列表 / 保存 / 运行 / 删除 ──
async function loadFlows() {
  initFlowCanvas();
  loadFlowRuns();
  const flows = await api('/api/studio/agent-flows');
  document.getElementById('flow-list').innerHTML = flows.map(f=>{
    let nCount = 0; try { nCount = JSON.parse(f.nodes||'[]').length; } catch(e){ nCount = 0; }
    return `
    <div onclick="flowSelect(${f.id})" data-flow="${f.id}" style="padding:9px 10px;margin-bottom:6px;border:1px solid var(--line);border-radius:7px;cursor:pointer;font-size:12px;${flowEditId===f.id?'background:var(--blue-l);border-color:var(--blue);':''}">
      <div style="font-weight:600;display:flex;align-items:center;gap:6px;">${esc(f.name)}<span class="st ${f.status==='published'?'ok':f.status==='archived'?'g':'w'}" style="font-size:9.5px;padding:0 5px;">${esc(f.status||'draft')}</span></div>
      <div style="font-size:10.5px;color:var(--mut);margin-top:2px;">${esc(f.description||'-')} ｜ ${nCount} 节点${f.version?` ｜ v${esc(f.version)}`:''}</div>
      <div style="margin-top:4px;display:flex;gap:6px;">
        <button class="btn sm" style="font-size:10.5px;padding:1px 7px;" onclick="event.stopPropagation();flowEditOf(${f.id})" title="流程设置">✏️</button>
        <button class="btn sm ghost" style="font-size:10.5px;padding:1px 7px;" onclick="event.stopPropagation();flowRunHistoryOf(${f.id})" title="运行历史">📈</button>
        <button class="btn sm red" style="font-size:10.5px;padding:1px 7px;" onclick="event.stopPropagation();flowDelete(${f.id})">🗑</button>
      </div>
    </div>`;}).join('') || '<div style="color:var(--mut);font-size:11.5px;padding:8px;">暂无流程，从右侧拖拽节点搭建，或「✨ AI 生成」</div>';
}
// 流程卡「✏️」：选中该流程并打开流程设置（原画布头部「⚙ 流程设置」入口移除，改由卡片承担）
async function flowEditOf(fid) {
  await flowSelect(fid);
  flowSettings();
}
// 流程卡「📈」：右侧弹窗「运行历史」Tab 展示该流程的历史列表（点击记录卡片 → viewFlowRun 详情）
function flowRunHistoryOf(fid) {
  openFlowRunPanel('hist');
  document.getElementById('flow-run-scope').value = 'current';
  loadFlowRuns(fid);
}
async function flowSelect(fid) {
  flowEditId = fid;
  const f = await api('/api/studio/agent-flows/' + fid);
  document.getElementById('flow-name').value = f.name || '';
  document.getElementById('flow-desc').value = f.description || '';
  flowVersion = f.version || 'v1';
  flowStatus = f.status || 'draft';
  const def = {nodes: JSON.parse(f.nodes||'[]'), edges: JSON.parse(f.edges||'[]')};
  flowDefToCanvas(def);
  document.getElementById('flow-result').innerHTML = '';
  hideFlowMonitorInline();  // 切换流程：收起监控内联区
  loadFlows();
}
function flowNew() {
  flowEditId = null;
  document.getElementById('flow-name').value = '';
  document.getElementById('flow-desc').value = '';
  flowVersion = 'v1'; flowStatus = 'draft';
  document.getElementById('flow-result').innerHTML = '';
  hideFlowMonitorInline();  // 新建流程：收起监控内联区
  flowCanvasClear();
}
// ── 运行结果容器：▶ 运行 / ⏩ 异步 / 历史详情 统一在右侧弹窗 flow-result 展示 ──
let _flowResultEl = null;   // 当前结果容器（默认 flow-result；运行/异步/历史详情均指向弹窗内结果区）
function curFlowResult(){
  return _flowResultEl || document.getElementById('flow-result');
}
// ── 运行监控内联区（📊 监控：内联展示；自动轮询实现"实时"）──
let _monitorTimer = null;   // 监控自动刷新定时器（30s）
function showFlowMonitorInline(){
  const el = document.getElementById('flow-monitor-inline');
  if(el) el.style.display = 'block';
  return el;
}
function hideFlowMonitorInline(){
  const el = document.getElementById('flow-monitor-inline');
  if(el) el.style.display = 'none';
  stopMonitorAutoRefresh();
}
function startMonitorAutoRefresh(){
  stopMonitorAutoRefresh();
  _monitorTimer = setInterval(()=>{ flowMonitor(); }, 30000);  // 30s 自动刷新（含 HIL 队列实时性）
}
function stopMonitorAutoRefresh(){
  if(_monitorTimer){ clearInterval(_monitorTimer); _monitorTimer = null; }
}
// ── 流程设置面板（名称/描述/版本/状态 关键信息）──
let flowVersion = 'v1', flowStatus = 'draft';
function flowSettings() {
  const title = document.getElementById('panel-title');
  if(title) title.textContent = '⚙ 流程设置';
  const body = document.getElementById('panel-body');
  body.innerHTML = `
    <div style="padding:14px;display:flex;flex-direction:column;gap:12px;font-size:12.5px;">
      <div style="font-size:12px;color:var(--mut);line-height:1.6;">配置流程的关键信息：<b>名称（保存必填）</b>、描述、版本与状态。修改后点「✔ 应用设置」写回画布。</div>
      <div class="form-row"><label>流程名称 <span style="color:var(--red);">*</span></label>
        <input id="fs-name" value="${esc(document.getElementById('flow-name').value)}" placeholder="如：需求冲突影响分析流程" style="border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12.5px;width:100%;"></div>
      <div class="form-row"><label>描述</label>
        <textarea id="fs-desc" placeholder="流程用途说明（可空）" style="border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12.5px;width:100%;min-height:56px;">${esc(document.getElementById('flow-desc').value)}</textarea></div>
      <div style="display:flex;gap:10px;">
        <div style="flex:1;"><label style="font-size:11px;color:var(--mut);">版本</label>
          <input id="fs-version" value="${esc(flowVersion||'v1')}" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;width:100%;"></div>
        <div style="flex:1;"><label style="font-size:11px;color:var(--mut);">状态</label>
          <select id="fs-status" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;width:100%;">
            <option value="draft" ${flowStatus==='draft'?'selected':''}>draft（草稿）</option>
            <option value="published" ${flowStatus==='published'?'selected':''}>published（已发布）</option>
            <option value="archived" ${flowStatus==='archived'?'selected':''}>archived（归档）</option>
          </select></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:4px;">
        <button class="btn" onclick="flowSettingsApply()">✔ 应用设置</button>
        <button class="btn ghost" onclick="closePanel()">取消</button>
      </div>
    </div>`;
  document.getElementById('panel-detail').classList.add('open');
  document.getElementById('overlay').classList.add('show');
  document.getElementById('fs-name').focus();
}
function flowSettingsApply() {
  document.getElementById('flow-name').value = document.getElementById('fs-name').value.trim();
  document.getElementById('flow-desc').value = document.getElementById('fs-desc').value.trim();
  flowVersion = document.getElementById('fs-version').value.trim() || 'v1';
  flowStatus = document.getElementById('fs-status').value;
  closePanel();
  toast('流程设置已应用（保存后生效）');
}
async function flowSave() {
  const ta = document.getElementById('flow-json');
  let def;
  if(ta && ta.style.display !== 'none'){ try{ def = JSON.parse(ta.value); }catch(e){ toast('流程 JSON 格式错误：' + e.message); return; } }
  else def = flowCanvasToDef();
  const name = document.getElementById('flow-name').value.trim();
  if(!name) {
    toast('请先在「⚙ 流程设置」中填写流程名称');
    flowSettings();
    return;
  }
  const desc = document.getElementById('flow-desc').value.trim();
  const nodes = def.nodes||[], edges = def.edges||[];
  const payload = {name, description:desc, nodes, edges, version:flowVersion||'v1', status:flowStatus||'draft'};
  if(flowEditId) payload.id = flowEditId;
  const r = await api('/api/studio/agent-flows', {method:'POST', body:JSON.stringify(payload)});
  if(r && r.error) { toast('保存失败：' + r.error); return; }
  if(r.id) flowEditId = r.id;
  toast(`流程「${name}」已保存（${nodes.length} 节点 / ${edges.length} 连线）`);
  loadFlows();
  // 滚动到列表顶部的新流程并高亮
  const first = document.querySelector('#flow-list [data-flow]');
  if(first) first.scrollIntoView({block:'nearest'});
}
async function flowRun() {
  // 运行当前流程：打开右侧弹窗仅展示本次运行结果（历史数据走流程卡「📈」单独入口）
  if(!flowEditId) { await flowSave(); }
  if(!flowEditId) return;
  const ta = document.getElementById('flow-json');
  let def;
  if(ta && ta.style.display !== 'none'){ try{ def = JSON.parse(ta.value); }catch(e){ toast('流程 JSON 格式错误：' + e.message); return; } }
  else def = flowCanvasToDef();
  openFlowRunPanel('hist', false);  // 不加载历史列表
  _flowResultEl = document.getElementById('flow-result');
  const el = _flowResultEl;
  el.innerHTML = '<span style="color:var(--blue-d);">▶ 正在运行当前流程…</span>';
  try {
    const r = await api(`/api/studio/agent-flows/${flowEditId}/run`, {method:'POST', body:JSON.stringify({payload:{}, definition:def})});
    if(r && r.error) { el.innerHTML = '<span style="color:var(--red);">运行失败：' + esc(r.error) + '</span>'; return; }
    renderFlowRunResult(el, r);
  } catch(e) { el.innerHTML = '<span style="color:var(--red);">运行失败：' + esc(e.message) + '</span>'; }
}
// ── M4：异步运行（run-async + SSE 进度订阅，长流程不阻塞）──
async function flowRunAsync() {
  // 提交当前流程异步运行：打开右侧弹窗实时展示本次进度与结果（历史数据走流程卡「📈」单独入口）
  if(!flowEditId) { await flowSave(); }
  if(!flowEditId) return;
  const ta = document.getElementById('flow-json');
  let def;
  if(ta && ta.style.display !== 'none'){ try{ def = JSON.parse(ta.value); }catch(e){ toast('流程 JSON 格式错误：' + e.message); return; } }
  else def = flowCanvasToDef();
  openFlowRunPanel('hist', false);  // 不加载历史列表
  _flowResultEl = document.getElementById('flow-result');
  const el = _flowResultEl;
  el.innerHTML = '<span style="color:var(--blue-d);">⏳ 已提交异步运行，等待进度…</span>';
  try {
    const r = await api(`/api/studio/agent-flows/${flowEditId}/run-async`, {method:'POST', body:JSON.stringify({payload:{}, definition:def})});
    if(r && r.error) { el.innerHTML = '<span style="color:var(--red);">提交失败：' + esc(r.error) + '</span>'; return; }
    const rid = r.run_id;
    el.innerHTML = `<span style="color:var(--blue-d);">⏳ 异步运行中（run #${rid}）…</span>`;
    const es = new EventSource('/api/studio/flow-runs/' + rid + '/events');
    es.addEventListener('step', ev => {
      try {
        const steps = JSON.parse(ev.data || '[]');
        const last = steps[steps.length-1];
        if(last) el.innerHTML = `<span style="color:var(--blue-d);">⏳ 运行中：${esc(last.node_id)} 已完成（${steps.length} 检查点）…</span> <button class="btn sm" style="margin-top:4px;" onclick="flowPauseRun(${rid})">⏸ 暂停</button>`;
      } catch(e){}
    });
    es.addEventListener('done', ev => {
      es.close();
      const d = JSON.parse(ev.data || '{}');
      loadFlowRunDetail(rid, el, d);
    });
    es.addEventListener('error', ev => {
      es.close();
      el.innerHTML = '<span style="color:var(--red);">进度订阅中断，运行结果可到流程卡「📈」查看</span>';
    });
  } catch(e) { el.innerHTML = '<span style="color:var(--red);">提交失败：' + esc(e.message) + '</span>'; }
}
// ── M4：异步运行结果加载（按 run_id 拉详情渲染）──
async function loadFlowRunDetail(rid, el, meta) {
  try {
    const r = await api('/api/studio/flow-runs/' + rid);
    if(r && r.id){ renderFlowRunResult(el, {status: r.status, order: r.order, results: Object.fromEntries((r.steps||[]).map(s=>[s.node_id, {status:s.status, type:s.node_type, node:s.node_label, content:s.content, data:s.data, latency_ms:s.latency_ms, provider:s.data&&s.data.provider}])), total_latency_ms: r.total_latency_ms, run_id: rid}); }
    else if(meta) el.innerHTML = `<span style="color:var(--grn);">运行完成：${esc(meta.status)}（${meta.total_latency_ms}ms）</span>`;
  } catch(e){ el.innerHTML = '<span style="color:var(--red);">加载运行详情失败：' + esc(e.message) + '</span>'; }
}
// ── P0：运行结果渲染（含检查点/循环统计 + 断点恢复入口）──
function renderFlowRunResult(el, r) {
  const badge = r.status==='completed' ? '<span style="color:var(--grn);font-weight:600;">✓ completed</span>'
    : (r.status==='paused' ? '<span style="color:var(--amb);font-weight:600;">⏸ paused（检查点保留，可恢复）</span>'
    : (r.status==='running' ? '<span style="color:var(--blue-d);font-weight:600;">⏳ running</span>'
    : '<span style="color:var(--amb);font-weight:600;">⚠ partial</span>'));
  let html = `<div style="margin-bottom:6px;">${badge} · 执行顺序 ${esc(r.order.join(' → '))} · 总耗时 ${r.total_latency_ms}ms` +
    (r.checkpoint_count ? ` · <span style="color:var(--mut);">${r.checkpoint_count} 检查点</span>` : '') +
    (r.loop_count ? ` · <span style="color:var(--amber,#854F0B);">🔄 循环 ${r.loop_count} 次</span>` : '') + `</div>`;
  if(r.run_id){
    const hasPlanner = (r.order||[]).some(nid => (r.results[nid]||{}).type === 'planner');
    const btns = [
      `<button class="btn sm ghost" onclick="flowTimeTravel(${r.run_id})">🕘 检查点回放</button>`,
      `<button class="btn sm ghost" onclick="flowViewBlackboard(${r.run_id})">🧮 共享黑板</button>`
    ];
    if(hasPlanner) btns.push(`<button class="btn sm ghost" onclick="flowViewPlannerTasks(${r.run_id})">🗂 任务队列</button>`);
    if(r.status==='running') btns.push(`<button class="btn sm" onclick="flowPauseRun(${r.run_id})">⏸ 暂停</button>`);
    else if(r.status!=='completed') btns.push(`<button class="btn sm" onclick="flowResumeRun(${r.run_id})">▶ 从断点恢复</button>`);
    html += `<div style="margin-bottom:8px;display:flex;gap:6px;flex-wrap:wrap;">${btns.join('')}</div>`;
  }
  (r.order||[]).forEach(nid=>{
    const o = r.results[nid];
    const st = o.status==='done' ? '<span style="color:var(--grn);">done</span>' : (o.status==='error' ? '<span style="color:var(--red);">error</span>' : `<span style="color:var(--amb);">${esc(o.status)}</span>`);
    const meta = o.provider ? ` · <span style="color:var(--mut);">${esc(o.provider)}/${esc(o.model)}${o.used_mock?'(mock)':''}</span>` : '';
    html += `<div style="border:1px solid var(--line);border-radius:7px;padding:7px 9px;margin-bottom:6px;background:#fff;">
      <div style="font-weight:600;">${esc(o.node||nid)} <span style="font-size:10.5px;color:var(--mut);">[${esc(o.type||'')}]</span> ${st} ${meta} · ${o.latency_ms||0}ms</div>
      <div style="font-size:11px;color:var(--mut);margin-top:3px;max-height:110px;overflow:auto;">${esc(String(o.content||o.error||''))}</div>
    </div>`;
  });
  if(r.errors && r.errors.length) html += `<div style="color:var(--red);font-size:11px;">错误：${esc(JSON.stringify(r.errors))}</div>`;
  el.innerHTML = html;
}
// ── P3：运行监控（内联展示，与运行结果区同款风格；30s 自动轮询实时刷新 + 手动刷新）──
async function flowMonitor() {
  const m = await api('/api/studio/monitor/runs?days=7');
  const el = showFlowMonitorInline();
  const rate = m.success_rate || 0;
  const rateColor = rate >= 90 ? 'var(--grn)' : (rate >= 60 ? 'var(--amb)' : 'var(--red)');
  const updated = new Date().toLocaleTimeString('zh-CN',{hour12:false});
  let html = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
      <b style="font-size:12.5px;color:var(--blue-d);">📊 运行监控</b>
      <span class="badge">近 7 天</span>
      <span style="color:var(--mut);font-size:10.5px;">更新于 ${updated} · 每 30s 自动刷新</span>
      <span style="flex:1"></span>
      <button class="btn sm ghost" onclick="flowMonitor()">🔄 刷新</button>
      <button class="btn sm ghost" onclick="hideFlowMonitorInline()">✕ 收起</button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px;">
      <div style="border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;"><div style="font-size:20px;font-weight:500;">${m.total_runs||0}</div><div style="font-size:10.5px;color:var(--mut);">近7天运行</div></div>
      <div style="border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;"><div style="font-size:20px;font-weight:500;color:${rateColor};">${rate}%</div><div style="font-size:10.5px;color:var(--mut);">成功率</div></div>
      <div style="border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;"><div style="font-size:20px;font-weight:500;">${m.avg_latency_ms||0}ms</div><div style="font-size:10.5px;color:var(--mut);">平均耗时</div></div>
      <div style="border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;"><div style="font-size:20px;font-weight:500;">${m.success_runs||0}</div><div style="font-size:10.5px;color:var(--mut);">成功次数</div></div>
    </div>
    <div style="font-size:12px;font-weight:500;margin:10px 0 6px;">按流程统计</div>` +
    ((m.by_flow||[]).length ? (m.by_flow||[]).map(f=>`<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line);font-size:11.5px;">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(f.name)}</span>
      <span class="st ${f.runs===f.ok?'ok':(f.ok?'w':'r')}" style="font-size:9.5px;">${f.ok}/${f.runs}</span>
      <span style="color:var(--mut);">${f.avg_ms}ms</span></div>`).join('') : '<div style="color:var(--mut);font-size:11px;">暂无运行记录</div>') +
    `<div style="font-size:12px;font-weight:500;margin:12px 0 6px;">节点类型耗时</div>` +
    `<div style="display:flex;flex-wrap:wrap;gap:6px;">` +
    (m.node_stats||[]).map(n=>`<span style="font-size:10.5px;background:var(--line);border-radius:8px;padding:2px 8px;">${esc(n.type)} ${n.count}次 · ${n.avg_ms}ms${n.errors?` <b style="color:var(--red);">${n.errors}错</b>`:''}</span>`).join('') + `</div>
    <div style="font-size:12px;font-weight:500;margin:12px 0 6px;">失败归因（按节点类型）</div>` +
    (Object.entries(m.failures_by_type||{}).length ? Object.entries(m.failures_by_type).map(([t,c])=>`<span style="font-size:10.5px;color:var(--red);background:var(--red-l,#FCEBEB);border-radius:8px;padding:2px 8px;margin-right:6px;">${esc(t)} × ${c}</span>`).join('') : '<span style="color:var(--mut);font-size:11px;">近7天无节点失败</span>') +
    `<div style="margin-top:16px;border-top:1px solid var(--line);padding-top:10px;" id="monitor-extra">
      <div style="font-size:12px;font-weight:500;margin-bottom:6px;">加载 LLM 用量 / HIL 队列…</div>
    </div>`;
  el.innerHTML = html;
  monitorExtra();  // M7/M5：异步加载 LLM 用量 + HIL 待确认（内联区内）
  startMonitorAutoRefresh();  // 30s 自动轮询（含 HIL 队列实时性）
}
// ── M7：LLM 用量与成本 + M5：HIL 确认队列（监控面板增强区）──
async function monitorExtra() {
  try {
    const [u, h] = await Promise.all([
      api('/api/studio/monitor/usage?days=7'),
      api('/api/studio/hil-confirmations?status=pending&limit=20')
    ]);
    const el = document.getElementById('monitor-extra');
    if(!el) return;
    const rateColor = (u.mock_rate||0) > 60 ? 'var(--amb)' : 'var(--grn)';
    const costDisp = (u.estimated_cost||0) >= 0.001 ? '$' + u.estimated_cost : '<$0.001';
    let html = `<div style="font-size:12px;font-weight:500;margin-bottom:6px;">LLM 用量与成本（近7天）</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px;">
        <div style="border:1px solid var(--line);border-radius:8px;padding:8px;text-align:center;"><div style="font-size:16px;font-weight:500;">${u.total_calls||0}</div><div style="font-size:10px;color:var(--mut);">调用次数</div></div>
        <div style="border:1px solid var(--line);border-radius:8px;padding:8px;text-align:center;"><div style="font-size:16px;font-weight:500;color:${rateColor};">${u.mock_rate||0}%</div><div style="font-size:10px;color:var(--mut);">Mock 降级率</div></div>
        <div style="border:1px solid var(--line);border-radius:8px;padding:8px;text-align:center;"><div style="font-size:16px;font-weight:500;">${u.total_tokens||0}</div><div style="font-size:10px;color:var(--mut);">总 Tokens</div></div>
        <div style="border:1px solid var(--line);border-radius:8px;padding:8px;text-align:center;"><div style="font-size:16px;font-weight:500;">${costDisp}</div><div style="font-size:10px;color:var(--mut);">估算成本</div></div>
      </div>` +
      ((u.by_provider||[]).length?`<div style="font-size:11px;margin-bottom:8px;">` + (u.by_provider||[]).map(p=>`<span style="display:inline-block;background:var(--line);border-radius:8px;padding:2px 8px;margin:0 4px 4px 0;font-size:10.5px;">${esc(p.name)} · ${p.calls}次 · ${p.tokens}tok${p.mock?` · <b style="color:var(--amb);">${p.mock}mock</b>`:''}</span>`).join('') + `</div>`:'') +
      `<div style="font-size:12px;font-weight:500;margin:12px 0 6px;">HIL 人工确认队列（待处理 ${(h||[]).length} 项）<span style="color:var(--mut);font-size:10.5px;font-weight:400;"> · 按编排 Run 分组，勾选 = 批准</span></div>` +
      hilQueueHtml(h||[]);
    el.innerHTML = html;
  } catch(e){ const el = document.getElementById('monitor-extra'); if(el) el.innerHTML = '<span style="color:var(--mut);font-size:11px;">用量/HIL 加载失败：' + esc(e.message) + '</span>'; }
}
// Task 14：HIL 确认队列按 run_id 分组渲染（组头「编排 Run #N」+ 每行 checkbox 勾选=批准）
function hilQueueHtml(items){
  if(!items.length) return '<span style="color:var(--mut);font-size:11px;">暂无待确认的写操作</span>';
  const groups = {};
  items.forEach(c=>{ const k = c.run_id||0; (groups[k] = groups[k]||[]).push(c); });
  let html = '';
  Object.keys(groups).sort((a,b)=>b-a).forEach(rid=>{
    const list = groups[rid];
    html += `<div style="border:1px solid var(--line);border-radius:8px;margin-bottom:8px;overflow:hidden;">
      <div style="background:var(--blue-l);padding:5px 9px;font-size:11.5px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        ${rid && rid!=='0' ? '编排 Run #' + rid : '未关联 Run（对话直发）'} <span class="tag" style="font-size:9.5px;">${list.length} 项</span>
        <span style="flex:1"></span>
        <button class="btn sm grn" style="font-size:10px;padding:0 8px;" onclick="hilApproveChecked('${rid}')">✅ 批准勾选项</button>
        <button class="btn sm ghost" style="font-size:10px;padding:0 8px;" onclick="hilRejectChecked('${rid}')">❌ 驳回其余</button>
      </div>
      <div style="background:#fff;">` +
      list.map(c=>`<label style="display:flex;align-items:flex-start;gap:8px;padding:6px 9px;border-top:1px solid var(--line);cursor:pointer;">
        <input type="checkbox" class="hil-check" data-rid="${rid}" data-cid="${c.id}" style="margin-top:3px;">
        <span style="flex:1;min-width:0;">
          <span style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;"><span style="font-weight:600;">#${c.id}</span>${c.task_key?`<span class="tag" style="font-size:9.5px;">↳ 子任务 ${esc(c.task_key)}</span>`:''}<span style="color:var(--amb);">${esc(c.action)}</span><span style="color:var(--mut);">${esc(c.agent_id||'-')}</span><span style="flex:1;color:var(--mut);font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.preview||'')}</span>
          <button class="btn sm" style="font-size:10px;padding:0 8px;" onclick="hilDecide(${c.id},true)">✅ 确认</button>
          <button class="btn sm ghost" style="font-size:10px;padding:0 8px;" onclick="hilDecide(${c.id},false)">❌ 拒绝</button></span>
          <div style="font-size:10.5px;color:var(--mut);margin-top:3px;">参数：<code style="word-break:break-all;">${esc(JSON.stringify(c.payload||{}))}</code></div>
        </span></label>`).join('') + `</div></div>`;
  });
  return html;
}
// Task 14：批准勾选项（逐个调 HILService.decide approve）
async function hilApproveChecked(rid){
  const ids = [...document.querySelectorAll('.hil-check')].filter(c=>c.dataset.rid===String(rid) && c.checked).map(c=>+c.dataset.cid);
  if(!ids.length){ toast('请先勾选要批准的事项'); return; }
  for(const cid of ids){
    try { await api('/api/studio/hil-confirmations/' + cid + '/decide', {method:'POST', body:JSON.stringify({approve:true, decided_by:'王工'})}); }
    catch(e){ toast('#' + cid + ' 批准失败：' + e.message); }
  }
  toast(`✅ 已批准 ${ids.length} 项（确认后由执行方按 payload 执行）`);
  monitorExtra();
}
// Task 14：驳回其余（未勾选项视为驳回）
async function hilRejectChecked(rid){
  const list = [...document.querySelectorAll('.hil-check')].filter(c=>c.dataset.rid===String(rid));
  const checked = new Set(list.filter(c=>c.checked).map(c=>+c.dataset.cid));
  const rest = list.map(c=>+c.dataset.cid).filter(id=>!checked.has(id));
  if(!rest.length){ toast('没有未勾选的事项可驳回'); return; }
  for(const cid of rest){
    try { await api('/api/studio/hil-confirmations/' + cid + '/decide', {method:'POST', body:JSON.stringify({approve:false, decided_by:'王工'})}); }
    catch(e){ toast('#' + cid + ' 驳回失败：' + e.message); }
  }
  toast(`❌ 已驳回 ${rest.length} 项`);
  monitorExtra();
}
// ── M5：HIL 确认/拒绝 ──
async function hilDecide(cid, approve) {
  try {
    const r = await api('/api/studio/hil-confirmations/' + cid + '/decide', {method:'POST', body:JSON.stringify({approve, decided_by:'王工'})});
    toast('确认单 #' + cid + ' 已' + (approve ? '确认' : '拒绝'));
    monitorExtra();
    if(approve) toast('提示：确认后请由执行方按 payload 重新执行该写操作');
  } catch(e){ toast('操作失败：' + e.message); }
}
// ── M3：Planner 任务队列视图 ──
async function flowViewPlannerTasks(rid) {
  try {
    const tasks = await api('/api/studio/planner/' + rid + '/tasks');
    const el = curFlowResult();
    const stZh = {planned:'计划中', ready:'就绪', running:'执行中', done:'✅完成', failed:'❌失败', blocked:'⚠️阻塞', canceled:'已取消'};
    let html = `<div style="margin-bottom:8px;"><b>🗂 Planner 任务队列</b>（run #${rid}）<span style="color:var(--mut);font-size:11px;"> · 依赖拓扑 + 结构化移交</span></div>`;
    if(!tasks || !tasks.length){ el.innerHTML = html + '<span style="color:var(--mut);">该运行没有任务队列记录</span>'; return; }
    html += tasks.map(t=>{
      const deps = (t.deps||[]).length ? '依赖: ' + t.deps.join(', ') : '无依赖';
      const meta = t.metadata && Object.keys(t.metadata).length ? `<div style="font-size:10.5px;color:var(--mut);">移交：${esc(JSON.stringify(t.metadata).slice(0,180))}</div>` : '';
      // P2：blocked/failed 人工处理（重试 / 人工确认完成）
      let ops = '';
      if(t.status==='blocked' || t.status==='failed'){
        ops = `<div style="margin-top:5px;display:flex;gap:5px;">
          <button class="btn sm ghost" onclick="plannerTaskOp(${t.id},'retry')">↻ 重试</button>
          ${t.status==='blocked'?`<button class="btn sm ghost" onclick="plannerTaskOp(${t.id},'resolve')">✓ 人工确认完成</button>`:''}
        </div>`;
      }
      return `<div style="border:1px solid var(--line);border-radius:7px;padding:7px 9px;margin-bottom:6px;background:#fff;">
        <div style="font-weight:600;font-size:12px;">${esc(t.task_key)} · ${esc(t.title)} <span style="font-size:10.5px;color:var(--mut);">→ ${esc(t.agent_id||'-')}</span>
          <span style="float:right;font-size:10.5px;">${stZh[t.status]||esc(t.status)}${t.latency_ms?` · ${t.latency_ms}ms`:''}</span></div>
        <div style="font-size:10.5px;color:var(--mut);margin-top:3px;">${deps} · 类型:${esc(t.task_type||'agent')}</div>
        ${t.error?`<div style="color:var(--red);font-size:11px;">错误：${esc(t.error)}</div>`:''}
        ${t.result?`<div style="font-size:11px;color:var(--mut);margin-top:3px;max-height:70px;overflow:auto;">${esc(t.result.slice(0,300))}</div>`:''}
        ${meta}
        ${ops}
      </div>`;
    }).join('');
    el.innerHTML = html;
  } catch(e){ toast('加载任务队列失败：' + e.message); }
}
// P2：Planner 任务人工处理（重试 / 确认完成）
async function plannerTaskOp(tid, op){
  try{
    await api(`/api/studio/planner/tasks/${tid}/${op}`, {method:'POST', body:'{}'});
    toast(op==='retry' ? '已重试（任务重新进入执行队列）' : '已人工确认完成');
    // 刷新当前队列（从当前展示的运行详情中提取 run 号）
    const ridEl = curFlowResult();
    const cur = (ridEl.innerHTML.match(/run #(\d+)/)||[])[1] || '';
    if(cur) flowViewPlannerTasks(parseInt(cur));
  }catch(e){ toast('操作失败：' + e.message); }
}
// ── P1：共享黑板查看（L1 工作记忆）──
async function flowViewBlackboard(rid) {
  const rows = await api('/api/studio/memory/blackboard?run_id=' + rid);
  const el = curFlowResult();
  if(!rows || !rows.length) { el.innerHTML = '<div style="color:var(--mut);">该运行无黑板内容（节点需配置 write_keys 写入）</div><div style="margin-top:8px;"><button class="btn sm ghost" onclick="viewFlowRun(' + rid + ')">← 返回</button></div>'; return; }
  let html = `<div style="margin-bottom:6px;">🧮 运行 #${rid} 共享黑板（L1 工作记忆，${rows.length} 项）：</div>`;
  rows.forEach(r=>{
    let v = r.value;
    const vs = (typeof v === 'object' ? JSON.stringify(v) : String(v));
    html += `<div style="border:1px solid var(--line);border-radius:7px;padding:7px 9px;margin-bottom:6px;background:#fff;font-size:11.5px;">
      <b>${esc(r.key)}</b> <span class="st w" style="font-size:9.5px;padding:0 5px;">${esc(r.mem_type||'artifacts')}</span> · ${(r.updated_at||'').slice(11,19)}
      <div style="color:var(--mut);margin-top:3px;max-height:120px;overflow:auto;">${esc(vs)}</div>
    </div>`;
  });
  html += `<div style="margin-top:8px;"><button class="btn sm ghost" onclick="viewFlowRun(${rid})">← 返回运行详情</button></div>`;
  el.innerHTML = html;
}
// ── P0：时间旅行（检查点回放）──
async function flowTimeTravel(rid) {
  const cps = await api('/api/studio/flow-runs/' + rid + '/checkpoints');
  if(!cps || !cps.length) { toast('该运行无检查点（请先运行流程）'); return; }
  const el = curFlowResult();
  el.innerHTML = `<div style="margin-bottom:6px;">🕘 运行 #${rid} 检查点（${cps.length} 个，点击查看该时刻全部节点状态）：</div>` +
    cps.map(c=>`<div style="border:1px solid var(--line);border-radius:7px;padding:6px 9px;margin-bottom:5px;cursor:pointer;background:#fff;font-size:11.5px;" onclick="flowViewCheckpoint(${rid},${c.seq})">
      <b>#${c.seq}</b> · 节点 ${esc(c.node_id||'-')} · ${(c.created_at||'').slice(11,19)}</div>`).join('') +
    `<div style="margin-top:8px;"><button class="btn sm ghost" onclick="viewFlowRun(${rid})">← 返回运行详情</button></div>`;
}
async function flowViewCheckpoint(rid, seq) {
  const r = await api(`/api/studio/flow-runs/${rid}/checkpoints/${seq}`);
  if(r && r.error) { toast('加载失败：' + r.error); return; }
  const el = curFlowResult();
  const results = r.results || {};
  let html = `<div style="margin-bottom:6px;">🕘 检查点 <b>#${r.seq}</b>（节点 <b>${esc(r.node_id||'-')}</b> 执行后）· 该时刻全部节点状态（时间旅行快照）：</div>`;
  Object.entries(results).forEach(([nid,o])=>{
    const st = o.status==='done' ? '<span style="color:var(--grn);">done</span>' : (o.status==='error' ? '<span style="color:var(--red);">error</span>' : `<span style="color:var(--amb);">${esc(o.status||'-')}</span>`);
    html += `<div style="border:1px solid var(--line);border-radius:7px;padding:6px 9px;margin-bottom:5px;background:#fff;font-size:11.5px;">
      <b>${esc(o.node||nid)}</b> <span style="color:var(--mut);">[${esc(o.type||'')}]</span> ${st} · <span style="color:var(--mut);">${esc(String(o.content||o.error||'').slice(0,120))}</span></div>`;
  });
  html += `<div style="margin-top:8px;"><button class="btn sm ghost" onclick="flowTimeTravel(${rid})">← 检查点列表</button></div>`;
  el.innerHTML = html;
}
// ── D6：主动暂停（执行器在下一节点检查点后静默停机，检查点保留）──
async function flowPauseRun(rid) {
  if(!(await confirmDialog(`确认暂停运行 #${rid}？已完成节点结果保留，可从断点恢复。`))) return;
  try {
    const r = await api(`/api/studio/flow-runs/${rid}/pause`, {method:'POST', body:'{}'});
    if(r && r.error) { toast('暂停失败：' + r.error); return; }
    toast('已请求暂停（执行器将在当前节点完成后停机）');
    setTimeout(()=>{ loadFlowRunDetail(rid, curFlowResult()); }, 800);  // 暂停后刷新当前弹窗内的运行详情（不加载历史列表）
  } catch(e) { toast('暂停失败：' + e.message); }
}
// ── P0：从断点恢复 ──
async function flowResumeRun(rid) {
  const el = curFlowResult();
  el.innerHTML = '<span style="color:var(--blue-d);">从断点恢复中…（已完成节点复用结果，只执行未完成部分）</span>';
  try {
    const r = await api(`/api/studio/flow-runs/${rid}/resume`, {method:'POST', body:JSON.stringify({payload:{}})});
    if(r && r.error) { el.innerHTML = '<span style="color:var(--red);">恢复失败：' + esc(r.error) + '</span>'; return; }
    renderFlowRunResult(el, r);
    loadFlowRuns();
  } catch(e) { el.innerHTML = '<span style="color:var(--red);">恢复失败：' + esc(e.message) + '</span>'; }
}
async function flowDelete(fid) {
  if(!(await confirmDialog(`确认删除流程 #${fid}？`))) return;
  const r = await api('/api/studio/agent-flows/' + fid, {method:'DELETE'});
  if(r && r.error) { toast('删除失败：' + r.error); return; }
  if(flowEditId === fid) flowEditId = null;
  toast('流程已删除'); loadFlows();
}
// ── AI 工坊 Copilot：自然语言生成流程（多轮对话式）──
let aiFlowDef = null;       // 最近一次 AI 生成/迭代的流程定义
let aiFlowSource = 'template';
let aiFlowConv = [];        // 多轮对话历史 [{role:'user'|'assistant', content}]（追加指令不覆盖，上下文连续）
function aiFlowDialog(refineMode) {
  if(!refineMode) { aiFlowConv = []; aiFlowDef = null; }   // 全新生成：清空对话
  const title = document.getElementById('panel-title');
  if(title) title.textContent = refineMode ? '✨ AI 迭代修改流程' : '✨ AI 生成流程';
  const body = document.getElementById('panel-body');
  const convHtml = aiFlowConv.map(m=>`
    <div style="margin-bottom:6px;${m.role==='user'?'text-align:right;':'text-align:left;'}">
      <div style="display:inline-block;max-width:85%;padding:6px 10px;border-radius:8px;font-size:11.5px;${m.role==='user'?'background:var(--blue-l);':'background:#fff;border:1px solid var(--line);'}">${esc(m.content)}</div>
    </div>`).join('');
  body.innerHTML = `
    <div style="padding:12px;display:flex;flex-direction:column;gap:10px;font-size:12.5px;height:calc(100vh - 60px);min-height:420px;">
      <div style="font-size:12px;color:var(--mut);line-height:1.6;">
        ${refineMode ? '基于当前流程继续<b>追加指令</b>（多轮对话，历史上下文保留，不会覆盖前序指令）：' : '用自然语言描述要编排的任务，AI 自动生成流程（节点/连线/条件分支），校验后导入画布。'}<br>
        <span style="color:var(--blue-d);">示例：从需求文本抽取条目，做冲突检测，再生成影响分析报告</span>
      </div>
      <div id="ai-conv" style="max-height:180px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;padding:8px;background:#fafaf7;">${convHtml || '<div style="color:var(--mut);font-size:11px;">尚未开始对话</div>'}</div>
      <textarea id="ai-prompt" style="width:100%;min-height:72px;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12.5px;" placeholder="${refineMode?'描述你要追加修改的内容（如：最后加一个 LLM 总结节点）…':'描述任务…'}"></textarea>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <label style="display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--mut);cursor:pointer;">
          <input type="checkbox" id="ai-thinking" style="accent-color:var(--blue-d);"> 🧠 深度思考（较慢，分析更深入）
        </label>
        <span style="flex:1"></span>
        <button class="btn" onclick="aiFlowGenerate(${refineMode?'true':'false'})">${refineMode?'追加并重新生成':'生成'}</button>
        <span id="ai-flow-source" style="font-size:11px;color:var(--mut);"></span>
      </div>
      <div id="ai-flow-result" style="font-size:11.5px;flex:1;overflow-y:auto;min-height:0;"></div>
    </div>`;
  document.getElementById('panel-detail').classList.add('open');
  document.getElementById('overlay').classList.add('show');   // 与 closePanel 一致（classList），避免 style 残留蒙层
  document.getElementById('ai-prompt').focus();
}
async function aiFlowGenerate(refineMode) {
  const prompt = document.getElementById('ai-prompt').value.trim();
  if(!prompt) { toast('请描述任务'); return; }
  const thinking = document.getElementById('ai-thinking') && document.getElementById('ai-thinking').checked;
  const el = document.getElementById('ai-flow-result');
  el.innerHTML = '<span style="color:var(--blue-d);">AI 分析中…（' + (thinking ? '🧠 深度思考模式，需要较长时间' : '检索范例 → 调用 LLM 生成 → 校验') + '）</span>';
  try {
    let r;
    if(refineMode) {
      const def = aiFlowDef || flowCanvasToDef();
      r = await api('/api/studio/ai/refine-flow', {method:'POST', body:JSON.stringify({prompt, definition:def, deep_thinking:thinking, conversation:aiFlowConv})});
    } else {
      r = await api('/api/studio/ai/generate-flow', {method:'POST', body:JSON.stringify({prompt, deep_thinking:thinking})});
    }
    if(r && r.error) { el.innerHTML = '<span style="color:var(--red);">生成失败：' + esc(r.error) + '</span>'; return; }
    aiFlowDef = {name:r.name, description:r.description, nodes:r.nodes||[], edges:r.edges||[]};
    aiFlowSource = r.source || 'template';
    // 追加对话历史（用户指令 + AI 回复摘要）——不覆盖前序内容
    aiFlowConv.push({role:'user', content:prompt});
    aiFlowConv.push({role:'assistant', content: `📋 ${r.name||'未命名'} · ${(r.nodes||[]).length} 节点 / ${(r.edges||[]).length} 连线${r.source==='llm'?' · 来源 LLM':''}`});
    const convEl = document.getElementById('ai-conv');
    if(convEl) convEl.innerHTML = aiFlowConv.map(m=>`
      <div style="margin-bottom:6px;${m.role==='user'?'text-align:right;':'text-align:left;'}">
        <div style="display:inline-block;max-width:85%;padding:6px 10px;border-radius:8px;font-size:11.5px;${m.role==='user'?'background:var(--blue-l);':'background:#fff;border:1px solid var(--line);'}">${esc(m.content)}</div>
      </div>`).join('');
    if(convEl) convEl.scrollTop = convEl.scrollHeight;
    const srcTag = document.getElementById('ai-flow-source');
    if(srcTag) srcTag.textContent = (r.source==='llm' ? '来源：LLM' : '来源：模板兜底') + (thinking ? ' · 🧠 深度思考' : '');
    const errs = (r.validations||[]).filter(v=>v.level==='error');
    const warns = (r.validations||[]).filter(v=>v.level==='warn');
    let html = `<div style="font-weight:600;font-size:13px;">📋 ${esc(r.name||'未命名流程')}</div>
      <div style="color:var(--mut);margin:4px 0 8px;">${esc(r.description||'')}</div>
      <div style="margin-bottom:6px;">
        <span class="st ${errs.length?'r':(warns.length?'w':'ok')}">${errs.length?'有 '+errs.length+' 处错误':(warns.length?'警告 '+warns.length:'校验通过')}</span>
        <span style="color:var(--mut);font-size:11px;">${(r.nodes||[]).length} 节点 / ${(r.edges||[]).length} 连线</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px;">` +
      (r.nodes||[]).map(n=>`<span style="font-size:10.5px;background:var(--line);border-radius:8px;padding:1px 7px;">${esc(n.label||n.id)}</span>`).join('') +
      `</div>`;
    if(r.reasoning) html += `<div style="border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin-bottom:8px;background:var(--amb-l);max-height:110px;overflow:auto;"><div style="font-size:10.5px;color:var(--amb);font-weight:600;">🧠 思考过程（摘要）</div><div style="font-size:11px;color:var(--mut);margin-top:3px;">${esc(r.reasoning)}</div></div>`;
    if(r.note) html += `<div style="border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin-bottom:8px;background:var(--amb-l);"><div style="font-size:11px;color:var(--amb);">💡 ${esc(r.note)}</div></div>`;
    if(errs.length || warns.length){
      html += `<div style="border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin-bottom:8px;max-height:120px;overflow:auto;">` +
        [...errs.map(v=>`<div style="color:var(--red);font-size:11px;">⚠ ${esc(v.message)}</div>`),
         ...warns.map(v=>`<div style="color:var(--amb);font-size:11px;">· ${esc(v.message)}</div>`)].join('') + `</div>`;
    }
    html += `<div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn" onclick="aiFlowImport()">⇥ 导入画布</button>
        <button class="btn ghost" onclick="aiFlowKeepTalking()">✏️ 继续追加指令</button>
        <button class="btn red" onclick="closePanel()">取消</button>
      </div>
      <div style="margin-top:8px;font-size:10.5px;color:var(--mut);">生成结果与手动创建完全兼容：导入后可用拖拽微调、保存、运行，走同一链路。「继续追加指令」保留历史上下文，可多轮完善。</div>`;
    el.innerHTML = html;
    document.getElementById('ai-prompt').value = '';
    document.getElementById('ai-prompt').focus();
  } catch(e) { el.innerHTML = '<span style="color:var(--red);">生成失败：' + esc(e.message) + '</span>'; }
}
function aiFlowKeepTalking() {
  // 切换到「迭代修改」模式（重渲染面板，保留对话历史与当前流程）：
  // 提交按钮变为「追加并重新生成」→ 走 refine-flow 携带 conversation 上下文，多轮对话不覆盖前序指令
  aiFlowDialog(true);
}
function aiFlowImport() {
  if(!aiFlowDef) return;
  document.getElementById('flow-name').value = aiFlowDef.name || '';
  document.getElementById('flow-desc').value = aiFlowDef.description || '';
  flowDefToCanvas(aiFlowDef);
  closePanel();
  toast('已导入画布，可拖拽微调后保存');
}

// ── 运行历史（轨迹落库）──
async function loadFlowRuns(forceFlowId) {
  // 运行/异步弹窗场景：跳过历史列表加载（历史数据走流程卡「📈」单独入口）
  if(_skipHistoryFill) return;
  // 运行历史维度：默认「全部流程」；切到「当前流程」按 flowEditId 过滤；forceFlowId 用于流程卡「📈 运行历史」入口（无需切换选中流程）
  const scope = (document.getElementById('flow-run-scope')||{}).value || '';
  const fid = forceFlowId || (scope === 'current' && flowEditId ? flowEditId : '');
  const q = fid ? `?flow_id=${fid}&limit=30` : '?limit=30';
  const runs = await api('/api/studio/flow-runs' + q);
  // 右侧弹窗内运行历史列表（点卡片 → viewFlowRun 展示详情）
  const el = document.getElementById('flow-runs');
  if(el) el.innerHTML = runs.map(r=>`
    <div onclick="viewFlowRun(${r.id})" style="padding:6px 8px;margin-bottom:5px;border:1px solid var(--line);border-radius:6px;cursor:pointer;font-size:11px;background:#fff;" title="点击查看运行详情">
      <div style="display:flex;align-items:center;gap:6px;">
        <span class="st ${r.status==='completed'?'ok':'w'}">${r.status}</span>
        <b style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(r.flow_name||('#run '+r.id))}</b>
        ${r.parent_run_id?'<span class="st" style="background:#eee;color:var(--mut);">子</span>':''}
        <span style="color:var(--mut);">${r.node_count} 节点</span>
      </div>
      <div style="font-size:10px;color:var(--mut);margin-top:2px;">${(r.created_at||'').slice(0,16)} · ${r.total_latency_ms}ms${r.error_count?' · <span style="color:var(--red);">'+r.error_count+' 错误</span>':''}</div>
    </div>`).join('') || '<div style="color:var(--mut);font-size:11px;padding:6px;">暂无运行记录，点「▶ 运行」后这里显示轨迹</div>';
}
async function viewFlowRun(rid) {
  openFlowRunPanel('hist');  // 运行历史选择 → 右侧弹窗「运行历史」Tab 展开详情
  _flowResultEl = document.getElementById('flow-result');  // 历史详情路径：结果容器指向弹窗
  const r = await api('/api/studio/flow-runs/' + rid);
  if(r && r.error) { toast('加载失败：' + r.error); return; }
  const el = _flowResultEl;
  const badge = r.status==='completed' ? '<span style="color:var(--grn);font-weight:600;">✓ completed</span>'
    : (r.status==='paused' ? '<span style="color:var(--amb);font-weight:600;">⏸ paused（检查点保留，可恢复）</span>'
    : (r.status==='running' ? '<span style="color:var(--blue-d);font-weight:600;">⏳ running</span>'
    : '<span style="color:var(--amb);font-weight:600;">⚠ partial</span>'));
  let html = `<div style="margin-bottom:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <button class="btn sm ghost" onclick="flowRunBackToList()">← 返回列表</button>
      <span style="font-size:11.5px;">📈 运行 #${r.id} · ${esc(r.flow_name)} · ${badge} · 总耗时 ${r.total_latency_ms}ms · ${(r.created_at||'').slice(0,16)}</span>
    </div>`;
  const btns = [
    `<button class="btn sm ghost" onclick="flowTimeTravel(${r.id})">🕘 检查点回放</button>`,
    `<button class="btn sm ghost" onclick="flowViewBlackboard(${r.id})">🧮 共享黑板</button>`,
    `<button class="btn sm ghost" onclick="openEventSubs(${r.id})">🔔 事件订阅</button>`
  ];
  if((r.steps||[]).some(s=>s.node_type==='planner')) btns.push(`<button class="btn sm ghost" onclick="flowViewPlannerTasks(${r.id})">🗂 任务队列</button>`);
  if(r.status==='running') btns.push(`<button class="btn sm" onclick="flowPauseRun(${r.id})">⏸ 暂停</button>`);
  else if(r.status!=='completed') btns.push(`<button class="btn sm" onclick="flowResumeRun(${r.id})">▶ 从断点恢复</button>`);
  html += `<div style="margin-bottom:8px;display:flex;gap:6px;flex-wrap:wrap;">${btns.join('')}</div>`;
  if(r.parent_run_id) html += `<div style="font-size:10.5px;color:var(--mut);margin-bottom:4px;">↑ 父运行 #${r.parent_run_id}</div>`;
  html += `<div id="flow-run-tree" style="margin-bottom:8px;"></div>`;
  (r.order||[]).forEach(nid=>{
    const s = (r.steps||[]).find(x=>x.node_id===nid) || {};
    const st = s.status==='done' ? '<span style="color:var(--grn);">done</span>' : (s.status==='error' ? '<span style="color:var(--red);">error</span>' : `<span style="color:var(--amb);">${esc(s.status||'-')}</span>`);
    const d = s.data||{};
    const meta = d.provider ? ` · <span style="color:var(--mut);">${esc(d.provider)}/${esc(d.model||'')}${d.used_mock?'(mock)':''}</span>` : '';
    html += `<div style="border:1px solid var(--line);border-radius:7px;padding:7px 9px;margin-bottom:6px;background:#fff;">
      <div style="font-weight:600;">${esc(s.node_label||nid)} <span style="font-size:10.5px;color:var(--mut);">[${esc(s.node_type||'')}]</span> ${st} ${meta} · ${s.latency_ms||0}ms</div>
      <div style="font-size:11px;color:var(--mut);margin-top:3px;max-height:110px;overflow:auto;">${esc(String(s.content||s.error||''))}</div>
    </div>`;
  });
  el.innerHTML = html;
  renderFlowRunTree(rid);
}
// ── D9：运行层级拓扑树（Supervisor 树：顶层 Manager → 中层 Supervisor → 底层 Worker）──
async function renderFlowRunTree(rid) {
  const el = document.getElementById('flow-run-tree');
  if(!el) return;
  const t = await api('/api/studio/flow-runs/' + rid + '/tree');
  if(!t || t.error || !(t.children||[]).length) { el.innerHTML = ''; return; }
  el.innerHTML = `<div style="font-size:10.5px;color:var(--mut);margin-bottom:4px;">🗂 层级拓扑（父委派子流程）</div>` + treeNodeHtml(t, 0);
}
function treeNodeHtml(n, depth) {
  const stColor = n.status==='completed' ? 'var(--grn)' : (n.status==='error' ? 'var(--red)' : (n.status==='running' ? 'var(--blue-d)' : 'var(--amb)'));
  const badge = n.status==='completed' ? 'ok' : (n.status==='error' ? 'red' : 'w');
  let h = `<div style="margin-left:${depth*18}px;margin-bottom:5px;">`;
  h += `<div style="border:1px solid var(--line);border-left:3px solid ${stColor};border-radius:6px;padding:5px 8px;background:#fff;display:flex;align-items:center;gap:6px;font-size:11px;flex-wrap:wrap;">`;
  h += `<span class="st ${badge}">${n.status}</span>`;
  h += `<b>#${n.id} ${esc(n.flow_name)}</b>`;
  h += (n.parent_run_id ? '<span class="st" style="background:#eee;color:var(--mut);">子</span>' : '<span class="st b">顶层</span>');
  h += `<span style="color:var(--mut);">${n.node_count} 节点</span>`;
  h += `<span style="color:var(--mut);">${n.total_latency_ms}ms</span>`;
  h += `<span style="color:var(--mut);">${(n.created_at||'').slice(0,16)}</span>`;
  h += `</div>`;
  (n.children||[]).forEach(c => h += treeNodeHtml(c, depth+1));
  return h + `</div>`;
}
// ── D11 事件回调订阅（A2A：节点完成/出错/运行完成 → Webhook 回调）──
let evSubRunId = 0;
async function openEventSubs(run_id) {
  evSubRunId = run_id || 0;
  showModal('evsub');
  document.getElementById('evsub-filter').innerHTML = evSubRunId
    ? `🎯 作用范围：运行 #${evSubRunId} 级（含全局订阅，任意运行触发）`
    : '🎯 作用范围：全局（任意运行触发）';
  await loadEventSubs();
}
async function loadEventSubs() {
  const q = evSubRunId ? `?run_id=${evSubRunId}` : '';
  const subs = await api('/api/studio/event-subscriptions' + q);
  document.getElementById('evsub-rows').innerHTML = subs.map(s=>`<tr>
    <td>${s.id}</td>
    <td><span class="st b" style="font-size:10.5px;">${s.event_type}</span></td>
    <td>${s.run_id ? `<span class="st">run#${s.run_id}</span>` : '<span class="st g">全局</span>'} <span style="font-size:10px;color:var(--mut);">${s.node_id||'全部节点'}</span></td>
    <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(s.webhook_url)}">${esc(s.webhook_url)}</td>
    <td>${s.secret ? '<span class="st ok">HMAC</span>' : '-'}</td>
    <td style="font-size:10.5px;color:var(--mut);">${s.last_event_at ? (s.last_status_code + ' · ' + s.last_event_at.slice(0,16)) : '-'}</td>
    <td><span class="st ${s.status==='active'?'ok':'w'}">${s.status}</span></td>
    <td style="white-space:nowrap;">
      <button class="btn sm ghost" onclick="toggleEventSub(${s.id})">${s.status==='active'?'暂停':'激活'}</button>
      <button class="btn sm red" onclick="delEventSub(${s.id})">删除</button>
    </td>
  </tr>`).join('') || '<tr><td colspan="8" style="text-align:center;color:var(--mut);font-size:11px;padding:12px;">暂无订阅——运行节点完成/出错、运行完成时将回调注册的 URL（标准化 A2A 事件）</td></tr>';
}
async function addEventSub() {
  const webhook_url = document.getElementById('ev-url').value.trim();
  if(!webhook_url) { toast('Webhook URL 必填'); return; }
  const body = {
    run_id: evSubRunId,
    event_type: document.getElementById('ev-type').value,
    node_id: document.getElementById('ev-node').value.trim(),
    webhook_url,
    secret: document.getElementById('ev-secret').value.trim(),
  };
  await api('/api/studio/event-subscriptions', {method:'POST', body:JSON.stringify(body)});
  toast('已注册事件订阅');
  document.getElementById('ev-url').value = '';
  document.getElementById('ev-secret').value = '';
  await loadEventSubs();
}
async function delEventSub(id) {
  if(!(await confirmDialog('删除该事件订阅？'))) return;
  await api('/api/studio/event-subscriptions/' + id, {method:'DELETE'});
  await loadEventSubs();
}
async function toggleEventSub(id) {
  await api('/api/studio/event-subscriptions/' + id + '/toggle', {method:'POST'});
  await loadEventSubs();
}
