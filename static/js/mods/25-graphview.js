async function fillSvmBranches() {
  const sel = document.getElementById('svm-branch');
  const cur = getCurrentBranch();
  sel.innerHTML = `<option value="">已发布(release)分支（推荐）</option>`;
  try {
    const bs = await api('/api/branches');
    (bs||[]).forEach(b=>{
      if(b.status!=='active') return;
      // 默认推荐 release；仅当当前工作分支本身是 release 时才选中当前分支
      const selected = isReleaseBranch(b.name) && b.name===cur ? 'selected' : '';
      sel.insertAdjacentHTML('beforeend', `<option value="${esc(b.name)}" ${selected}>${esc(b.name)}</option>`);
    });
  } catch(e) { /* 分支加载失败不阻断 */ }
}
// P0-2 视图类型下拉填充（修复孤儿链路：index.html 弹窗存在但 svm-type 无人填充，
// 投影永远落在"未知视图"。fillSvmBranches 同理此前无调用点——统一由 openSysmlViewStudio 驱动）
async function fillSvmTypes() {
  const sel = document.getElementById('svm-type');
  if(!sel || sel.options.length) return;   // 幂等：已填充不重复
  try {
    const r = await api('/api/sysml/views/types');
    (r.types||[]).forEach(t=>{
      sel.insertAdjacentHTML('beforeend', `<option value="${esc(t.id)}">${esc(t.name)}（${esc(t.id)}）</option>`);
    });
  } catch(e) { /* 类型清单失败不阻断：loadSysmlView 会显示投影错误 */ }
}
// 打开「SysML 视图预览」弹窗的唯一入口（补齐初始化：填下拉 → 开面板 → 首次投影）
async function openSysmlViewStudio() {
  await Promise.all([fillSvmTypes(), fillSvmBranches()]);
  document.getElementById('svm-mask')?.classList.add('show');
  document.getElementById('svm-panel')?.classList.add('show');
  await loadSysmlView();
}
async function loadSysmlView() {
  const meta = document.getElementById('svm-meta');
  const selType = document.getElementById('svm-type');
  const type = selType.value || (selType.options[0] && selType.options[0].value) || 'BDD';  // 兜底：下拉未选中/未填充时取首项
  const branch = document.getElementById('svm-branch').value;
  meta.textContent = '投影中…';
  try {
    const r = await api('/api/sysml/views?type='+encodeURIComponent(type)+'&branch='+encodeURIComponent(branch||''));
    if(!r.ok){ meta.textContent = '失败：'+(r.warnings||[]).join('；'); return; }
    renderSysmlView(r);
    meta.innerHTML = `<b>${esc(r.view.name)}</b> · v1=${esc(r.view.v1)} · v2=${esc(r.view.v2)} · 节点 ${r.nodes.length} / 边 ${r.edges.length}`;
  } catch(e) { meta.textContent = '失败：'+e.message; }
}
function closeSysmlViewPreview() {
  document.getElementById('svm-mask').classList.remove('show');
  document.getElementById('svm-panel').classList.remove('show');
}
// 视图元素 kind → 节点渲染参数（P0-1：色值全部取自 graphTok()，见 24-graph.js）
const SVM_NODE_STYLE = (function(){
  const k = graphTok();
  return {
    block:    {shape:'rect',  fill:k.greenL,   stroke:k.green,   w:120, h:44},
    part:     {shape:'rect',  fill:k.primaryL, stroke:k.primary, w:120, h:44},
    requirement:{shape:'req', fill:k.reqFill,  stroke:k.amber,   w:140, h:52},
    actor:    {shape:'actor', fill:k.neutral,  stroke:k.neutral, w:34, h:56},
    usecase:  {shape:'ellipse',fill:k.purpleL, stroke:k.purple,  w:120, h:46},
    action:   {shape:'round', fill:k.purpleL,  stroke:k.purple,  w:130, h:46},
    state:    {shape:'round', fill:k.stateFill,stroke:k.red,     w:130, h:46},
    constraint:{shape:'para', fill:k.neutralL, stroke:k.neutral, w:150, h:48},
    package:  {shape:'pkg',  fill:k.primaryL,  stroke:k.primary, w:150, h:52},
    port:     {shape:'rect',  fill:k.canvas,   stroke:k.primary, w:70, h:28},
  };
})();
// 视图边 kind → 线型（OMG SysML 规范：composition 源端实心菱形+实线无箭头；generalization 空心三角）
const SVM_EDGE_STYLE = (function(){
  const k = graphTok();
  return {
    composition:{stroke:k.green,   dash:'',    marker:'',            diamond:true},
    dependency: {stroke:k.neutral, dash:'6 3', marker:'svm-arrow',   diamond:false},
    satisfy:    {stroke:k.primary, dash:'6 4', marker:'svm-arrow-blu', diamond:false},
    derive:     {stroke:k.amber,   dash:'6 3', marker:'svm-arrow-amb', diamond:false},
    conflict:   {stroke:k.red,     dash:'4 3', marker:'svm-arrow-red', diamond:false},
    include:    {stroke:k.purple,  dash:'6 3', marker:'svm-arrow-pur', diamond:false},
    extend:     {stroke:k.purple,  dash:'6 3', marker:'svm-arrow-pur', diamond:false},
    allocate:   {stroke:k.primary, dash:'8 4', marker:'svm-arrow-blu', diamond:false},
    flow:       {stroke:k.primary, dash:'',    marker:'svm-arrow-blu', diamond:false},
    connect:    {stroke:k.primary, dash:'',    marker:'',             diamond:false},
    message:    {stroke:k.primary, dash:'',    marker:'svm-arrow-blu', diamond:false},
    transition: {stroke:k.red,     dash:'',    marker:'svm-arrow-red', diamond:false},
    bind:       {stroke:k.neutral, dash:'2 2', marker:'svm-arrow',     diamond:false},
  };
})();
// OMG 规范边标签（双尖括号）——无标准标签的关系回退显示原类型
const SVM_EDGE_LABEL = {
  composition: '', dependency: '', satisfy: '«satisfy»', derive: '«deriveReqt»',
  conflict: '«conflict»', include: '«include»', extend: '«extend»',
  allocate: '«allocate»', flow: '', connect: '', message: '', transition: '', bind: '',
  verify: '«verify»', alias: '«alias»', succession: '«succession»',
};
// 简单分层布局：按入度分层（拓扑层）+ 同层横向排列
function svmLayout(nodes, edges) {
  const n = nodes.length; if(!n) return {};
  const idx = {}; nodes.forEach((nd,i)=>idx[nd.id]=i);
  const outDeg = new Array(n).fill(0);
  const inDeg = new Array(n).fill(0);
  edges.forEach(e=>{
    if(idx[e.source]===undefined||idx[e.target]===undefined) return;
    outDeg[idx[e.source]]++; inDeg[idx[e.target]]++;
  });
  // BFS 分层：从入度为 0 的根开始；环兜底
  const layer = new Array(n).fill(0);
  const q = nodes.map((nd,i)=>i).filter(i=>inDeg[i]===0);
  if(!q.length) q.push(0);
  const seen = new Array(n).fill(false);
  while(q.length){
    const i = q.shift();
    if(seen[i]) continue;
    seen[i] = true;
    const srcId = nodes[i].id;
    nodes.forEach((nd,j)=>{
      if(j===i) return;
      if(edges.some(e=>e.source===srcId && e.target===nd.id)) layer[j] = Math.max(layer[j], layer[i]+1);
    });
    nodes.forEach((nd,j)=>{
      if(!seen[j] && edges.some(e=>e.source===srcId && e.target===nd.id)) q.push(j);
    });
  }
  // 同层排序：按名称
  const byLayer = {};
  nodes.forEach((nd,i)=>{ (byLayer[layer[i]] = byLayer[layer[i]]||[]).push(i); });
  Object.keys(byLayer).forEach(k=>byLayer[k].sort((a,b)=>nodes[a].name.localeCompare(nodes[b].name,'zh')));
  const pos = {};
  const STYLE = SVM_NODE_STYLE;
  const cellW = 180, cellH = 110, padX = 30, padY = 30;
  Object.keys(byLayer).forEach((L,i)=>{
    const items = byLayer[L];
    items.forEach((ndi,j)=>{
      const st = STYLE[nodes[ndi].kind] || STYLE.block;
      const w = (st.w||120)+ (nodes[ndi].name.length>8?(nodes[ndi].name.length-8)*7:0);
      pos[nodes[ndi].id] = {x: padX + j*cellW + w/2, y: padY + i*cellH, w, h: st.h||44, layer:i};
    });
  });
  let maxW = 100; let maxH = 100;
  nodes.forEach(nd=>{ const p=pos[nd.id]; if(p){ maxW=Math.max(maxW,p.x+p.w/2+padX); maxH=Math.max(maxH,p.y+p.h+padY); } });
  return {pos, W:maxW, H:maxH};
}
function svmNodeSvg(nd, p, selected) {
  const st = SVM_NODE_STYLE[nd.kind] || SVM_NODE_STYLE.block;
  const x = p.x - p.w/2, y = p.y - p.h/2, w = p.w, h = p.h;
  const name = esc(nd.name);
  const sub = esc(nd.type||'');
  const sel = selected?'stroke-width:3;':'';
  let shape = '';
  if(st.shape==='ellipse'){
    shape = `<ellipse cx="${p.x}" cy="${p.y}" rx="${w/2}" ry="${h/2}" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.6" style="${sel}"/>`;
  } else if(st.shape==='actor'){
    // 火柴人参与者
    const cy = p.y + 4;
    shape = `<circle cx="${p.x}" cy="${cy-18}" r="6" fill="none" stroke="${st.stroke}" stroke-width="1.6"/>
      <line x1="${p.x}" y1="${cy-12}" x2="${p.x}" y2="${cy+6}" stroke="${st.stroke}" stroke-width="1.6"/>
      <line x1="${p.x-10}" y1="${cy}" x2="${p.x+10}" y2="${cy}" stroke="${st.stroke}" stroke-width="1.6"/>
      <line x1="${p.x}" y1="${cy+6}" x2="${p.x-8}" y2="${cy+18}" stroke="${st.stroke}" stroke-width="1.6"/>
      <line x1="${p.x}" y1="${cy+6}" x2="${p.x+8}" y2="${cy+18}" stroke="${st.stroke}" stroke-width="1.6"/>`;
    return `<g transform="translate(0,0)">${shape}<text x="${p.x}" y="${p.y+h-2}" font-size="10.5" fill="#444" text-anchor="middle">${name}</text></g>`;
  } else if(st.shape==='req'){
    // 需求框（拐角标签）
    shape = `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.6" style="${sel}"/>
      <polyline points="${x},${y} ${x+24},${y} ${x+24},${y+14} ${x},${y+14}" fill="#fff" stroke="${st.stroke}" stroke-width="1.2"/>`;
  } else if(st.shape==='round'){
    shape = `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="14" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.6" style="${sel}"/>`;
  } else if(st.shape==='para'){
    shape = `<polygon points="${x+14},${y} ${x+w},${y} ${x+w-14},${y+h} ${x},${y+h}" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.6" style="${sel}"/>`;
  } else if(st.shape==='pkg'){
    shape = `<rect x="${x}" y="${y+12}" width="${w}" height="${h-12}" rx="3" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.6" style="${sel}"/>
      <path d="M${x},${y+12} l0,-10 l22,0 l8,10 z" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.4"/>`;
  } else {
    shape = `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="${st.fill}" stroke="${st.stroke}" stroke-width="1.6" style="${sel}"/>`;
  }
  const labelY = sub ? p.y - 3 : p.y + 4;
  const subY = sub ? p.y + 12 : null;
  return `<g>${shape}
    <text x="${p.x}" y="${labelY}" font-size="11" font-weight="600" fill="#333" text-anchor="middle">${name.length>16?name.slice(0,16)+'…':name}</text>
    ${sub?`<text x="${p.x}" y="${subY}" font-size="8.5" fill="#888" text-anchor="middle">«${sub}»</text>`:''}
  </g>`;
}
function renderSysmlView(vm) {
  const canvas = document.getElementById('svm-canvas');
  const warn = document.getElementById('svm-warn');
  warn.style.display = (vm.warnings&&vm.warnings.length)?'block':'none';
  warn.innerHTML = '⚠️ ' + (vm.warnings||[]).map(esc).join('<br>⚠️ ');
  // 图例（P0-1：色值取自 graphTok，与画布/影响图同源；P0-4：前置统一交互提示条）
  document.getElementById('svm-legend').innerHTML = `
    ${gvHintBar('sysml')}
    <span class="lg"><span class="lsw lbl fill"></span>块/部件</span>
    <span class="lg"><span class="lsw lbl req"></span>需求</span>
    <span class="lg"><span class="lsw lbl act"></span>用例/动作</span>
    <span class="lg"><span class="lsw lbl st"></span>状态</span>
    <span class="lg"><span class="lsw" style="border-color:${graphTok('green')};"></span>组合 composition</span>
    <span class="lg"><span class="lsw" style="border-color:${graphTok('primary')};"></span>满足/连接 satisfy</span>
    <span class="lg"><span class="lsw dash" style="border-color:${graphTok('amber')};"></span>派生/依赖 derive</span>
    <span class="lg"><span class="lsw dash" style="border-color:${graphTok('red')};"></span>冲突/迁移</span>`;
  canvas.innerHTML = buildSvmSvg(vm);
  // P0-视图升级：右侧面板优先用 Cytoscape 渲染（旧 SVG 作为回退）
  if(window.cytoscape){
    canvas.innerHTML = '';
    svmRenderInViewer(vm, 'svm-canvas');
  }
  // P0-2 布局质量：指标计算 + 校验结果 + 存档（异步，不阻断渲染）
  if((vm.nodes||[]).length) svmArchiveLayoutCheck(vm);
}
// 生成单个视图的 SVG 字符串（会话内预览卡片与右侧面板共用；空视图返回提示占位）
function buildSvmSvg(vm) {
  const nodes = vm.nodes||[], edges = vm.edges||[];
  if(!nodes.length){
    return `<div style="padding:40px;color:var(--mut);font-size:14.5px;text-align:center;">该视图无可用模型元素<br><span style="font-size:12.5px;">请先在 SysML 代码中补充 ${esc(vm.view.type)} 视图所需元素（${esc((vm.warnings||[])[0]||'')}）</span></div>`;
  }
  const {pos, W, H} = svmLayout(nodes, edges);
  const markers = `
    <defs>
      <marker id="svm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${graphTok('neutral')}"/></marker>
      <marker id="svm-arrow-grn" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${graphTok('green')}"/></marker>
      <marker id="svm-arrow-blu" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${graphTok('primary')}"/></marker>
      <marker id="svm-arrow-amb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${graphTok('amber')}"/></marker>
      <marker id="svm-arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${graphTok('red')}"/></marker>
      <marker id="svm-arrow-pur" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${graphTok('purple')}"/></marker>
    </defs>`;
  // 边（先画线）
  let es = '';
  edges.forEach(e=>{
    const sp = pos[e.source], tp = pos[e.target];
    if(!sp||!tp) return;
    const st = SVM_EDGE_STYLE[e.kind] || SVM_EDGE_STYLE.dependency;
    const mx=(sp.x+tp.x)/2, my=(sp.y+tp.y)/2;
    const arrow = st.marker ? ` marker-end="url(#${st.marker})"` : '';
    es += `<line x1="${sp.x}" y1="${sp.y}" x2="${tp.x}" y2="${tp.y}" stroke="${st.stroke}" stroke-width="1.4" ${st.dash?`stroke-dasharray="${st.dash}"`:''}${arrow}/>`;
    if(st.diamond){
      // OMG 组合：源端（整体端）实心菱形，实线无箭头
      const dx=tp.x-sp.x, dy=tp.y-sp.y, len=Math.max(1,Math.hypot(dx,dy));
      const ux=dx/len, uy=dy/len, s=7, rx=sp.x+ux*(s+2), ry=sp.y+uy*(s+2);
      es += `<polygon points="${rx},${ry} ${rx-uy*s},${ry+ux*s} ${rx-ux*s},${ry-uy*s} ${rx+uy*s},${ry-ux*s}" fill="${st.stroke}" stroke="#fff" stroke-width="1"/>`;
    }
    const label = SVM_EDGE_LABEL[e.kind] || e.label || '';
    if(label){
      es += `<text x="${mx}" y="${my-5}" font-size="9" fill="#666" text-anchor="middle">${esc(label)}</text>`;
    }
  });
  let ns = nodes.map(nd=>svmNodeSvg(nd, pos[nd.id])).join('');
  const title = esc(vm.view.name);
  return `<svg viewBox="0 0 ${W+20} ${H+36}" xmlns="http://www.w3.org/2000/svg" style="min-width:700px;min-height:460px;">
    <text x="${W/2+10}" y="16" font-size="12" font-weight="700" fill="${graphTok('primaryD')}" text-anchor="middle">${title}（${vm.view.v1}）</text>
    ${markers}${es}${ns}
  </svg>`;
}

// ═══════════════ P0-视图渲染升级：Cytoscape.js 专业图引擎（替代自研 BFS，行业标配）═══════════════
// ViewModel {nodes, edges} → Cytoscape elements；力导向(cose)/层级(dagre)自动布局 + 缩放/拖拽/框选
let _cyInstances = {};  // key=视图类型+时间戳 → cytoscape 实例（防重复 init 泄漏）

// 视图类型 → 布局策略：BDD/PKG/REQ/TRACE 用 dagre 层级（composition/satisfy 边驱动节点层级排序）
function svmCytLayoutFor(viewType) {
  const hier = ['BDD','PKG','REQ','TRACE'];          // dagre 层级
  const grid = ['UC','SEQ','ACT'];                   // 网格
  const circ = ['STM'];                              // 环形
  if(hier.includes(viewType)) return 'dagre';
  if(grid.includes(viewType)) return 'grid';
  if(circ.includes(viewType)) return 'circle';
  return 'cose';                                     // IBD/PAR：拓扑均衡
}
// ViewModel → Cytoscape elements（node kind → 样式类；edge kind → 线型类）
// 布局：优先用《视图规范》布局引擎计算位置（确定性规则布局器，对齐行业标准）
function svmToElements(vm) {
  const nodes = (vm.nodes||[]).map(nd=>{
    const a = nd.attrs || {};
    // 节点标签：第一行名称（粗体），后续行类型/值/端口（说明属性）
    const extras = [];
    if (nd.type && nd.type !== nd.kind && nd.type !== nd.name) extras.push(nd.type);
    const val = a.value ? String(a.value).slice(0, 18) : '';
    if (val) extras.push(val);
    if (a.ports && a.ports.length) extras.push('端口:' + a.ports.slice(0, 3).join(','));
    const label = nd.name + (extras.length ? '\n' + extras.join(' / ') : '');
    // 2026-11-20 方案2：尺寸由布局引擎统一算（与 _elkNodeSize 共用 svmNodeSizeFor），
    // 布局与渲染同一口径，消除"以为放得下却重叠"；长名小幅加宽不截断。
    const sz = (typeof svmNodeSizeFor === 'function')
      ? svmNodeSizeFor(nd.kind||'block', nd.name||'') : {w:150,h:60};
    return {
      data: {id: String(nd.id), label, name: nd.name||'', kind: nd.kind, type: nd.type||'',
             attrs: a, isPort: nd.kind === 'port', svmW: sz.w, svmH: sz.h},
      classes: 'n-'+ (nd.kind||'block')
    };
  });
  const edges = (vm.edges||[]).map((e,i)=>{
    return {
      data: {id: 'e'+i, source: String(e.source), target: String(e.target), kind: e.kind||'dependency', label: SVM_EDGE_LABEL[e.kind] || e.label || ''},
      classes: 'e-'+ (e.kind||'dependency')
    };
  });
  // 《视图规范》布局引擎：8 种视图各用专用确定性布局器
  const vt = (vm.view && vm.view.type) || '';
  const viewNodes = (vm.nodes||[]).map(nd => ({id: nd.id, name: nd.name||'', kind: nd.kind||'block', type: nd.type||'', attrs: nd.attrs||{}}));
  const viewEdges = (vm.edges||[]).map(e => ({source: e.source, target: e.target, kind: e.kind||'dependency'}));
  const posMap = computeViewLayout(vt, viewNodes, viewEdges);
  if (posMap && Object.keys(posMap).length) {
    nodes.forEach(n => {
      const p = posMap[String(n.data.id)];
      if (p) n.position = p;
    });
  }
  // P0-2 布局质量存档：坐标留给指标计算/回传（svmArchiveLayoutCheck 用）
  _lastViewLayout = {vm, vt, posMap: posMap||{}};
  return {nodes, edges};
}
// ═══════════════ P0-2 视图布局质量指标与存档（SRS-GN-MG-BJYH 可验收闭环）═══════════════
// 最近一次 svmToElements 的布局坐标（key=vm 引用，防视图切换后误用旧坐标）
let _lastViewLayout = {vm: null, vt: '', posMap: {}};
// 线段相交（叉积方向法）；共享端点的两条边不算交叉
function svmSegCross(a1, a2, b1, b2) {
  const d1 = (a2.x-a1.x)*(b1.y-a1.y) - (a2.y-a1.y)*(b1.x-a1.x);
  const d2 = (a2.x-a1.x)*(b2.y-a1.y) - (a2.y-a1.y)*(b2.x-a1.x);
  const d3 = (b2.x-b1.x)*(a1.y-b1.y) - (b2.y-b1.y)*(a1.x-b1.x);
  const d4 = (b2.x-b1.x)*(a2.y-b1.y) - (b2.y-b1.y)*(a2.x-b1.x);
  return (d1*d2 < 0) && (d3*d4 < 0);
}
// 布局质量指标：交叉数 crossings / 总边长 edge_length_sum / 归一化评分 score（0~1，无交叉=1）
function svmLayoutMetrics(pos, edges) {
  const pts = edges.map(e => {
    const s = pos[String(e.source)], t = pos[String(e.target)];
    return (s && t) ? {s, t, src: String(e.source), tgt: String(e.target)} : null;
  }).filter(Boolean);
  let crossings = 0, lenSum = 0;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i];
    lenSum += Math.hypot(p.t.x-p.s.x, p.t.y-p.s.y);
    for (let j = i+1; j < pts.length; j++) {
      const q = pts[j];
      if (p.src===q.src || p.src===q.tgt || p.tgt===q.src || p.tgt===q.tgt) continue;
      if (svmSegCross(p.s, p.t, q.s, q.t)) crossings++;
    }
  }
  const score = pts.length ? Math.max(0, Math.round((1 - crossings/pts.length) * 1000) / 1000) : 1;
  return {crossings, edge_length_sum: Math.round(lenSum), score};
}
// 渲染后回传：POST /api/sysml/views/check（后端校验 + 布局指标落 view_layout_checks 表）
async function svmArchiveLayoutCheck(vm) {
  try {
    const branch = (document.getElementById('svm-branch')||{}).value || '';
    const pos = (_lastViewLayout.vm === vm && Object.keys(_lastViewLayout.posMap).length)
      ? _lastViewLayout.posMap
      : svmLayout(vm.nodes||[], vm.edges||[]).pos || {};   // SVG 回退路径坐标
    const metrics = svmLayoutMetrics(pos, vm.edges||[]);
    const r = await api('/api/sysml/views/check', {method:'POST', body: JSON.stringify({
      type: (vm.view&&vm.view.type)||'', branch,
      layout_metrics: metrics,
    })});
    if (r && r.ok) {
      const meta = document.getElementById('svm-meta');
      if (meta) {
        // 重复回传时只保留最新一条布局质量标记（初排 → ELK 精排覆盖）
        meta.innerHTML = meta.innerHTML.replace(/\s*·\s*布局质量[\s\S]*$/, '');
        const cov = r.check && r.check.coverage_pass;
        meta.insertAdjacentHTML('beforeend',
          ` · 布局质量 <b>score ${metrics.score}</b>（交叉 ${metrics.crossings}）`
          + (cov ? ' ✓覆盖完整' : ' ⚠输入集缺失'));
      }
    }
  } catch(e) { /* 指标存档失败不阻断渲染（后端留痕 layout_save_error） */ }
}
// P0-2 v2：ELK 行业标准布局内核异步精排（仅图结构类视图 BDD/PKG/TRACE）
// 流程：同步引擎初排先上屏（保底）→ ELK 交叉最小化精排覆盖坐标 → 更新指标并重新回传落库
async function svmElkRelayout(cy, vm) {
  const vmRef = vm;
  if (cy) cy.__elkDone = false;   // 2026-11-20 方案3：精排完成信号，导出前可等待
  try {
    const vt = (vm.view && vm.view.type) || '';
    // ★ 2026-11-09：把 parent（所属包/组合）一起传给布局内核。
    //   此前只传 id/name/kind/type/attrs —— 布局器**看不到层级**，于是把包节点
    //   当普通元素平铺，包里包外形全丢；回写坐标时又把包节点位置一起改掉，
    //   与 Cytoscape 复合节点「由子节点包围盒决定父位置」直接打架 ⇒ 框错位/重叠。
    const viewNodes = (vm.nodes||[]).map(nd => ({
      id: nd.id, name: nd.name||'', kind: nd.kind||'block', type: nd.type||'',
      attrs: nd.attrs||{}, parent: nd.parent || '', isParent: !!nd.isParent }));
    const viewEdges = (vm.edges||[]).map(e => ({source: e.source, target: e.target, kind: e.kind||'dependency'}));
    const pos = await computeViewLayoutAsync(vt, viewNodes, viewEdges);
    // 不适用类型（pos=null）也算"已完成"——没有待处理的精排，导出可立即进行
    if (!pos || _lastViewLayout.vm !== vmRef || !cy || cy.destroyed()) {
      if (cy && !cy.destroyed()) cy.__elkDone = true;
      return;  // 视图已切换或无精排则放弃
    }
    let _skipped = 0, _placed = 0;
    cy.nodes().forEach(n => {
      // 复合节点（包）的位置由 Cytoscape 依据子节点包围盒自动确定；
      // 手动写它的 position 会让框与子节点错开（这是"布局散乱"最直接的来源）。
      if (n.isParent()) { _skipped++; return; }
      const p = pos[n.id()];
      if (p) { n.position(p); n.unlock(); _placed++; }
    });
    _lastViewLayout.placed = _placed;
    _lastViewLayout.skippedParents = _skipped;
    // ELK 精排后图幅可能超出可视区（初排 fit 坐标已失效），重新适配
    try { cy.resize(); cy.fit(undefined, 40); } catch(e) {}
    _lastViewLayout.posMap = pos;
    if (cy) cy.__elkDone = true;   // 精排落位完成
    svmArchiveLayoutCheck(vmRef);  // 用精排后坐标重新回传（meta 只保留最新一条）
  } catch(e) {
    if (cy && !cy.destroyed()) cy.__elkDone = true;  // 失败也算完成，别让导出卡住
    console.warn('[ELK-relayout]', e.message);
  }
}
// 容器内渲染 Cytoscape 视图（返回实例；容器需已挂载）
function svmRenderCytoscape(container, vm) {
  if(!window.cytoscape || !container) return null;
  const key = container.id || 'cy-'+Date.now();
  // 清理旧实例（同容器重渲染时销毁，防内存泄漏）
  if(_cyInstances[key]){ try{ _cyInstances[key].destroy(); }catch(e){} delete _cyInstances[key]; }
  const {nodes, edges} = svmToElements(vm);
  if(!nodes.length) return null;
  const layoutName = svmCytLayoutFor(vm.view && vm.view.type);
  const isSeq = layoutName === 'grid';
  // P0 性能修复：渲染热路径不打日志（多图挂载时 console 输出本身即显著开销）
  // dagre roots：找无 parent 的最顶层节点（system/根包），让 dagre 树状自顶向下排
  const hierRoots = (vm.nodes||[]).filter(n => !n.parent).map(n => n.id);
  const k = graphTok();   // P0-1：样式单一真源（见 24-graph.js），本文件的样式表不再写死色值
  const cy = cytoscape({
    container,
    elements: {nodes, edges},
    style: [
      {selector:'node', style:{
        'content':'data(label)',
        'background-color':k.greenL,'border-color':k.green,'border-width':1.5,
        'color':k.nodeText,'font-size':12,'text-valign':'center','text-halign':'center',
        'text-wrap':'wrap','text-max-width':function(el){
          // 长名自适应：宽度跟随节点实际宽度（留 12px 内边距），不再固定 130 截断
          return Math.max(90, (el.data('svmW')||150) - 12);
        },
        // canvas 文字必须显式给 CJK 字体栈：缺省字体在部分环境缺中文字形会渲染成 tofu（菱形问号）
        'font-family':'"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif',
        'width':'data(svmW)','height':'data(svmH)','shape':'round-rectangle','text-margin-y':-2
      }},
      // 复合节点（父节点：包住子节点，SysML BDD/PKG 标准层级视觉）
      {selector:'node:parent', style:{
        'content':'data(label)',
        'background-color':k.parentFill,'background-opacity':0.4,
        'border-color':k.primary,'border-width':1.2,'border-style':'solid',
        'padding':'14px','shape':'round-rectangle',
        'text-valign':'top','text-halign':'center','font-size':10.5,'color':k.primary,
        'font-weight':'700','text-margin-y':-6
      }},
      {selector:'node[?isPort]', style:{'background-color':k.canvas,'border-color':k.primary,'shape':'round-rectangle','font-size':9,'content':'data(label)'}},
      // 按 kind 配色（尺寸统一走基础规则的 data(svmW/svmH)，此处只管配色/形状）
      {selector:'.n-requirement', style:{'background-color':k.reqFill,'border-color':k.amber,'shape':'round-rectangle'}},
      {selector:'.n-part', style:{'background-color':k.primaryL,'border-color':k.primary,'shape':'round-rectangle'}},
      {selector:'.n-block', style:{'background-color':k.greenL,'border-color':k.green,'shape':'round-rectangle'}},
      {selector:'.n-actor', style:{
        'background-color':'transparent','border-width':0,
        'background-image':'url("data:image/svg+xml;utf8,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 48 64%27%3E%3Ccircle cx=%2724%27 cy=%2711%27 r=%277%27 fill=%27%235F5E5A%27/%3E%3Cline x1=%2724%27 y1=%2718%27 x2=%2724%27 y2=%2744%27 stroke=%27%235F5E5A%27 stroke-width=%273%27/%3E%3Cline x1=%2724%27 y1=%2726%27 x2=%279%27 y2=%2738%27 stroke=%27%235F5E5A%27 stroke-width=%273%27/%3E%3Cline x1=%2724%27 y1=%2726%27 x2=%2739%27 y2=%2738%27 stroke=%27%235F5E5A%27 stroke-width=%273%27/%3E%3Cline x1=%2724%27 y1=%2744%27 x2=%2711%27 y2=%2761%27 stroke=%27%235F5E5A%27 stroke-width=%273%27/%3E%3Cline x1=%2724%27 y1=%2744%27 x2=%2737%27 y2=%2761%27 stroke=%27%235F5E5A%27 stroke-width=%273%27/%3E%3C/svg%3E")',
        'background-fit':'contain','background-repeat':'no-repeat',
        'shape':'rectangle',
        'color':k.neutral,'text-valign':'bottom','text-halign':'center','font-size':10,'text-margin-y':2,'text-wrap':'wrap','text-max-width':90
      }},
      {selector:'.n-usecase', style:{'background-color':k.purpleL,'border-color':k.purple,'shape':'ellipse'}},
      {selector:'.n-action', style:{'background-color':k.purpleL,'border-color':k.purple,'shape':'round-rectangle'}},
      {selector:'.n-state', style:{'background-color':k.stateFill,'border-color':k.red,'shape':'round-rectangle'}},
      {selector:'.n-constraint', style:{'background-color':k.neutralL,'border-color':k.neutral,'shape':'round-rectangle'}},
      {selector:'.n-package', style:{'background-color':k.primaryL,'border-color':k.primary,'shape':'round-rectangle'}},
      // 边：默认灰虚线
      {selector:'edge', style:{
        'curve-style':'bezier','width':1.4,'line-color':k.neutral,'line-style':'dashed',
        'target-arrow-shape':'triangle','target-arrow-color':k.neutral,
        'content':function(el){return el.data('label')||'';},'font-size':9,'text-rotation':'autorotate',
        'text-background-color':k.canvas,'text-background-opacity':0.7,'color':k.edgeText
      }},
      // 按 kind 线型（对齐 SVM_EDGE_STYLE）
      {selector:'.e-composition', style:{'line-color':k.green,'line-style':'solid','target-arrow-shape':'triangle','target-arrow-color':k.green}},
      {selector:'.e-satisfy', style:{'line-color':k.primary,'line-style':'dashed','target-arrow-shape':'triangle','target-arrow-color':k.primary}},
      {selector:'.e-connect', style:{'line-color':k.primary,'line-style':'solid','target-arrow-shape':'none'}},
      {selector:'.e-flow', style:{'line-color':k.primary,'line-style':'solid','target-arrow-shape':'triangle','target-arrow-color':k.primary}},
      {selector:'.e-derive', style:{'line-color':k.amber,'line-style':'dashed','target-arrow-shape':'triangle','target-arrow-color':k.amber}},
      {selector:'.e-conflict', style:{'line-color':k.red,'line-style':'dashed','target-arrow-shape':'triangle','target-arrow-color':k.red}},
      {selector:'.e-include', style:{'line-color':k.purple,'line-style':'dashed','target-arrow-shape':'triangle','target-arrow-color':k.purple}},
      {selector:'.e-extend', style:{'line-color':k.purple,'line-style':'dashed','target-arrow-shape':'triangle','target-arrow-color':k.purple}},
      {selector:'.e-allocate', style:{'line-color':k.primary,'line-style':'dashed','target-arrow-shape':'triangle','target-arrow-color':k.primary}},
      {selector:'.e-transition', style:{'line-color':k.red,'line-style':'solid','target-arrow-shape':'triangle','target-arrow-color':k.red}},
      {selector:'.e-bind', style:{'line-color':k.neutral,'line-style':'dotted','target-arrow-shape':'triangle','target-arrow-color':k.neutral}},
      {selector:'.e-assoc', style:{'line-color':k.neutral,'line-style':'solid','target-arrow-shape':'none','width':1.6}},
      // 选中高亮 + 1-hop 焦点（P0-2：与主画布/影响图同一套「邻域亮、其余暗」语义）+ 标签溢出保护
      {selector:':selected', style:{'border-width':3,'border-color':k.primary,'opacity':1}},
      {selector:'.dimmed', style:{'opacity':k.dimOpacity}},
      {selector:'.focus-hi', style:{'border-width':3.5,'border-color':k.primary,
        'overlay-color':k.primary,'overlay-opacity':0.14,'overlay-padding':6}},
    ],
    layout: {name:'preset', animate:false},  // 《视图规范》布局引擎已在 svmToElements 预计算位置，preset 保留
    wheelSensitivity: 0.2,
    minZoom: 0.2, maxZoom: 2.5,
  });
  // BDD/PKG：用 dagre 层级布局（composition 边驱动节点层级排序）
  // 交互：节点点击 → 1-hop 邻域高亮（P0-2）+ 属性浮层；点空白取消高亮
  cy.on('tap', 'node', evt=>{
    const d = evt.target.data();
    applyGraphFocus(cy, evt.target.id());
    const attrs = d.attrs || {};
    const lines = Object.keys(attrs).filter(kk=>attrs[kk]).map(kk=>`<b>${esc(kk)}</b>: ${esc(String(attrs[kk]))}`).join('<br>');
    const html = `<b>${esc(d.label)}</b> <span style="color:var(--mut);font-size:12px;">[${esc(d.kind||'block')}${d.type?' · '+esc(d.type):''}]</span><br>${lines||'<span style="color:var(--mut);">无附加属性</span>'}`;
    toastHtml(html);
  });
  cy.on('tap', 'edge', evt=>{
    const d = evt.target.data();
    toastHtml(`<b>${esc(d.kind||'relation')}</b>${d.label?'：'+esc(d.label):''}`);
  });
  cy.on('tap', evt=>{ if(evt.target === cy) applyGraphFocus(cy, null); });   // 点画布空白 ⇒ 统一取消高亮
  _cyInstances[key] = cy;
  if (container.id) window['cy_' + container.id] = cy;  // 调试/自动化引用
  svmElkRelayout(cy, vm);  // ELK 精排（异步、不阻断；不适用类型内部直接返回）
  return cy;
}
// 视图预览弹窗内渲染（替换原 buildSvmSvg 调用点）
function svmRenderInViewer(vm, containerId) {
  const el = document.getElementById(containerId);
  if(!el) return;
  el.innerHTML = '';  // 清空旧 SVG/容器
  // v6.4 P0：整体 idle 化（svmRenderCytoscape init 212ms + resize 50ms 拆两帧；用户感觉画布立刻可见）
  window.__cyIdleMount(el, ()=>{
    const cy = svmRenderCytoscape(el, vm);
    if(cy){
      // svmRenderCytoscape 内部已 idle fit；这里再 resize（弹窗 mount 时容器 flex 高度可能未分配）
      window.__cyIdleRun(()=>{ try{ cy.resize(); }catch(e){} });
      return cy;
    }
    if(!cy && (vm.nodes||[]).length){
      el.innerHTML = `<div style="padding:40px;color:var(--mut);font-size:14.5px;text-align:center;">该视图无可用模型元素<br><span style="font-size:12.5px;">${esc((vm.warnings||[])[0]||'')}</span></div>`;
    }
    return cy;
  }, null);
}

function setCurrentBranch(name) {
  localStorage.setItem('mbse_branch', name);
  closePanel();
  toast(isReleaseBranch(name) ? `📖 已切换到 ${name}（已发布·只读查看，写操作将被拦截）` : `已切换到工作分支 ${name}`);
  // 全局分支选择器 + KB 页当前分支标签同步
  syncGlobalBranch();
  const kbTag = document.getElementById('kb-branch-tag');
  if(kbTag) kbTag.textContent = name;
  // 按新分支重载：图谱 / 资料库 / 实体浏览 / 统计 / 版本管理
  loadGraph();
  loadBranches();
  loadMerges();
  loadDocs();
  loadKBEntities();
  loadKBStats();
  // 标注审核（kb-b）队列按新分支刷新：抽取候选 / 实体关系审核 / 融合工作台（元素常驻 DOM，仅知识库页激活时刷新，避免跨页多余请求）
  const kbPage = document.getElementById('pg-kb');
  if(kbPage && kbPage.classList.contains('on')) {
    v2gReviewLoad(); loadReviewQueue(); loadRelReview(); loadFusion();
  }
  // 当前工作区 Tab 按新分支重载（历史/MR/推理懒加载，切分支需补刷）
  if(typeof wsCurTab!=='undefined'){
    if(wsCurTab==='content' && typeof loadGraph==='function') loadGraph();
    else if(wsCurTab==='history' && typeof loadWsHistory==='function') loadWsHistory();
    else if(wsCurTab==='mr' && typeof loadMerges==='function') loadMerges();
    else if(wsCurTab==='reasoning' && typeof gwtReasoningInit==='function') gwtReasoningInit();
  }
}
// ── 全局分支选择器（顶部导航栏右上角）：列出全部分支，release（已发布）标 🔒 只读查看；写操作由 branchWritable 守卫 ──
async function initGlobalBranch() {
  const sel = document.getElementById('global-branch');
  if(!sel) return;
  try {
    const branches = await api('/api/branches');
    RELEASE_BRANCH_NAMES = (branches||[]).filter(b=>b.branch_type==='release').map(b=>b.name);
    const alive = (branches||[]).filter(b=>b.status!=='archived');
    // 仅当当前分支不存在（被删除/归档）时回退 dev；release 允许显式选择做只读查看，不强制弹回
    const cur = getCurrentBranch();
    if(cur!=='dev' && !alive.some(b=>b.name===cur)) {
      localStorage.setItem('mbse_branch', 'dev');
      toast('当前分支不可用，工作分支已回退 dev');
    }
    const cur2 = getCurrentBranch();
    const list = alive.length ? alive : [{name:'dev', branch_type:'dev'}];
    sel.innerHTML = list.map(b=>{
      const rel = b.branch_type==='release' || isReleaseBranch(b.name);
      const label = rel ? `${b.name}（🔒 已发布·只读查看）`
        : `${b.name}（${b.branch_type==='dev'?'开发':b.branch_type==='personal'?'个人':'本地'}）`;
      return `<option value="${esc(b.name)}">${esc(label)}</option>`;
    }).join('');
    if([...sel.options].some(o=>o.value===cur2)) sel.value = cur2;
    const kbTag = document.getElementById('kb-branch-tag');
    if(kbTag) kbTag.textContent = cur2;
    syncWsBranchBtn(cur2);
  } catch(e) {}
}
function onGlobalBranchChange(name) {
  if(!name || name===getCurrentBranch()) return;
  setCurrentBranch(name);
  // 图谱页：分支由顶部 global-branch 唯一入口驱动（筛选区 gv-branch 已移除），切换后重载图谱
  syncGlobalBranch();
  var kb_d = document.getElementById('kb-d');
  if(kb_d && kb_d.classList.contains('on')) loadGraph();
}
function syncGlobalBranch() {
  const sel = document.getElementById('global-branch');
  if(sel && [...sel.options].some(o=>o.value===getCurrentBranch())) sel.value = getCurrentBranch();
  syncWsBranchBtn(getCurrentBranch());
}
// 2026-09-12 修复：分支入口按钮文本始终跟随当前分支
//（此前仅 renderBrDropdown 在点开下拉时更新，初始加载显示写死的 dev 与真实分支错位）
function syncWsBranchBtn(name) {
  const cur = name || getCurrentBranch();
  const btn = document.getElementById('ws-branch-cur');
  if(btn) btn.textContent = cur;
  // ⇵ 同步基线按钮（2026-09-14 迁移到分支切换右侧）：仅个人分支显示——
  // 从基线同步（git merge 语义）只对 personal 有意义，dev/release 隐藏
  const sb = document.getElementById('hdr-sync-baseline');
  if(sb){
    const info = (Array.isArray(window._branches)?window._branches:[]).find(x=>x.name===cur);
    sb.style.display = (info && info.branch_type==='personal') ? '' : 'none';
  }
}
// ── 2026-09-12 展示优化：边贝塞尔曲线（二次贝塞尔+法线曲率偏移，支持多重边避让）与节点 hover 提示 ──
function gvEdgeCtrl(x1,y1,x2,y2,off){
  const mx=(x1+x2)/2, my=(y1+y2)/2, dx=x2-x1, dy=y2-y1, L=Math.hypot(dx,dy)||1;
  const nx=-dy/L, ny=dx/L;                    // 线段单位法线
  const cu = L * 0.22 * off;                  // 曲率随边长与偏移系数
  return {x: mx + nx*cu, y: my + ny*cu};
}
function gvEdgePath(x1,y1,x2,y2,off){
  const c = gvEdgeCtrl(x1,y1,x2,y2,off);
  return `M ${x1} ${y1} Q ${c.x} ${c.y} ${x2} ${y2}`;
}
function gvEdgeMid(x1,y1,x2,y2,off){
  const c = gvEdgeCtrl(x1,y1,x2,y2,off); return {x:c.x, y:c.y};
}
// ── 2026-09-12 颜色/动效增强：类型色转 rgba 淡底 + hover 邻域高亮/目标光晕 ──
function hexToRgba(hex, alpha){
  const m = /^#?([a-f\d]{3}|[a-f\d]{6})$/i.exec(hex || '');
  if(!m) return graphTok('primaryL');
  let h = m[1]; if(h.length === 3) h = h.split('').map(x=>x+x).join('');
  const n = parseInt(h, 16), r = (n>>16)&255, g = (n>>8)&255, b = n&255;
  return `rgba(${r},${g},${b},${alpha})`;
}
function gvTypeTint(color){ return hexToRgba(color, 0.16); }
let _gvTipEl = null;
let _gvHoverStyle = null;
function gvEnsureHoverStyle(){
  if(_gvHoverStyle) return;
  const k = graphTok();   // P0-1：hover/选中光晕与画布同源
  _gvHoverStyle = document.createElement('style');
  _gvHoverStyle.textContent =
    '.gv-node{transition:opacity .12s ease;}' +
    '.gv-hov-dim{opacity:.25 !important;}' +
    '.gv-hov-ring > circle:first-of-type{stroke-width:3.4;stroke:' + k.primary +
      ';filter:drop-shadow(0 0 4px ' + hexToRgba(k.primary, .75) + ');}' +
    '.gv-hov-ring > circle:nth-of-type(2){opacity:.45;}' +
    '.gv-sel-node > circle:first-of-type{filter:drop-shadow(0 0 8px rgba(250,199,117,.95)) drop-shadow(0 1px 2px rgba(0,0,0,.18));}' +
    // 选中节点关系方向流动（蚂蚁线沿 source→target 流动；入边反向流动以示关系方向）
    '@keyframes gvDashFlow{to{stroke-dashoffset:-28}}' +
    '.gv-flow-out,.gv-flow-in{stroke-dasharray:8 8;stroke-width:2.6;}' +
    '.gv-flow-out{animation:gvDashFlow .5s linear infinite;}' +
    '.gv-flow-in{animation:gvDashFlow .5s linear infinite reverse;}' +
    '#gv-svg{cursor:url(\"data:image/svg+xml;utf8,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2224%22 height=%2224%22 viewBox=%220 0 24 24%22%3E%3Cpath d=%22M12 1v6M12 17v6M1 12h6M17 12h6%22 stroke=%22%231F2D3D%22 stroke-width=%222%22/%3E%3Ccircle cx=%2212%22 cy=%2212%22 r=%222.2%22 fill=%22%23185FA5%22/%3E%3C/svg%3E\") 12 12, crosshair;}';
  document.head.appendChild(_gvHoverStyle);
}
function gvClearHover(){
  document.querySelectorAll('#gv-svg .gv-hov-dim,#gv-svg .gv-hov-ring')
    .forEach(x=>x.classList.remove('gv-hov-dim','gv-hov-ring'));
}
function gvClearAllFlow(){
  document.querySelectorAll('#gv-svg .gv-flow-out').forEach(p=>p.classList.remove('gv-flow-out'));
}
// 为某节点(选中或 hover)的相邻边按真实方向(success->success)加流动；先清后加
function gvApplyFlowFor(nid){
  const svg = document.getElementById('gv-svg'); if(!svg) return;
  gvClearAllFlow();
  const sid = String(nid);
  svg.querySelectorAll('[id^="gv-edge-"]').forEach(p=>{
    const e = (graphState.edges||[]).find(x=>String(x.id)===p.id.slice('gv-edge-'.length));
    if(e && (String(e.source_id)===sid || String(e.target_id)===sid)) p.classList.add('gv-flow-out');
  });
}
function gvNodeHover(id){
  gvEnsureHoverStyle(); gvClearHover();
  const svg = document.getElementById('gv-svg');
  const nid = String(id);
  // 邻域高亮：目标+其 1-hop 邻居保持清晰，其余淡出；目标加光晕
  if(svg){
    const nb = new Set([nid]);
    (graphState.edges || []).forEach(e=>{
      if(String(e.source_id) === nid) nb.add(String(e.target_id));
      if(String(e.target_id) === nid) nb.add(String(e.source_id));
    });
    svg.querySelectorAll('.gv-node').forEach(g=>{
      const dn = g.getAttribute('data-nid');
      if(nb.has(dn)){ if(dn === nid) g.classList.add('gv-hov-ring'); }
      else { g.classList.add('gv-hov-dim'); }
    });
    gvApplyFlowFor(id);        // hover 也按真实方向触发相邻边流动
  }
  const n = graphState.nodes.find(x=>x.id===id); if(!n) return;
  const el = document.getElementById('gv-graph-area'); if(!el) return;
  // FIX 2026-09-13: renderGraph 用 innerHTML 清空画布容器时会移除 tooltip 元素，但 _gvTipEl 仍非空，
  // 导致 hover 时不再重建、卡片写到已脱离文档的元素上而看不到——用 isConnected 判定失效即重建
  if(!_gvTipEl || !_gvTipEl.isConnected){
    _gvTipEl = document.createElement('div'); _gvTipEl.id='gv-node-tip';
    _gvTipEl.style.cssText='position:absolute;z-index:9;background:rgba(31,45,61,.95);color:#fff;border-radius:6px;padding:6px 9px;font-size:12.5px;line-height:1.55;pointer-events:none;max-width:220px;box-shadow:0 2px 8px rgba(0,0,0,.22);white-space:nowrap;';
    el.appendChild(_gvTipEl);
  }
  const s = graphState.vp.scale||1, px = graphState.vp.panX||0, py = graphState.vp.panY||0;
  const name = esc(n.name), type = esc(n.entity_type||'');
  const deg = (graphDegreeMap(graphState.nodes, graphState.edges)[id]) || 0;
  _gvTipEl.innerHTML = `<div style="font-weight:700;color:#FFE9A8;max-width:200px;overflow:hidden;text-overflow:ellipsis;">${name}</div><div style="opacity:.85;">类型：${type || '—'}${n._skel ? ('　×'+esc(n._count||1)) : ''}</div><div style="opacity:.7;">关系 ${deg} 条</div>`;
  const sx = n.x*s + px, sy = n.y*s + py;
  _gvTipEl.style.left = (sx + 12) + 'px';
  _gvTipEl.style.top = Math.max(4, sy - _gvTipEl.offsetHeight - 10) + 'px';
  _gvTipEl.style.display = 'block';
}
function gvNodeHoverEnd(){
  gvClearHover(); gvClearAllFlow();
  if(graphState.sel && graphState.sel.type==='node') gvApplyFlowFor(graphState.sel.id);   // 保留已选中节点的方向流动
  if(_gvTipEl){ _gvTipEl.style.display='none'; }
}
/* ═══ 2026-09-16 变更影响分析联动：图谱工作区高亮影响子图 ═══
   由影响分析卡「🕸 在图谱工作区打开」触发（window._gvPendingImpact）：
   非影响节点压淡（复用 .gv-hov-dim）+ 变更源光晕（.gv-hov-ring）+ 影响边蚂蚁线流动（.gv-flow-out）
   —— 零新渲染代码，全部复用 KG 既有样式与交互；重新 loadGraph 即恢复全图。 */
function gvImpactFocus(){
  const p = window._gvPendingImpact; if(!p || !(p.ids||[]).length) return;
  gvEnsureHoverStyle();
  const svg = document.getElementById('gv-svg'); if(!svg) return;
  const keep = new Set(p.ids.map(String));
  (p.edges||[]).forEach(([a,b])=>{ keep.add(String(a)); keep.add(String(b)); });
  svg.querySelectorAll('.gv-node').forEach(g=>{
    const dn = g.getAttribute('data-nid');
    if(dn && !keep.has(String(dn))) g.classList.add('gv-hov-dim');
  });
  // 影响子图内的边 → 方向流动（edge id 前缀 gv-edge-，回查 graphState.edges 取真实 source/target）
  const esets = new Set((p.edges||[]).map(([a,b])=>a+'→'+b));
  svg.querySelectorAll('[id^="gv-edge-"]').forEach(elm=>{
    const e = (graphState.edges||[]).find(x=>String(x.id)===elm.id.slice('gv-edge-'.length));
    if(e && esets.has(String(e.source_id)+'→'+String(e.target_id))) elm.classList.add('gv-flow-out');
  });
  if(p.src){
    const g = [...svg.querySelectorAll('.gv-node')].find(x=>String(x.getAttribute('data-nid'))===p.src);
    if(g) g.classList.add('gv-hov-ring');
    try{ gvCenterOnNode(p.src); }catch(e){}
  }
  toast(`🕸 已高亮影响子图（${p.ids.length} 节点）· 变更源：${p.name||'—'}；重新加载图谱即恢复`);
}
function renderGraph() {
  const el = document.getElementById('gv-graph-area');
  if(!el) return;
  gvEnsureHoverStyle();   // 注入边方向流动等 CSS（幂等）
  const W = graphState.svgW, H = graphState.svgH;
  const k = graphTok();   // P0-1：本函数所有色值取自单一真源
  // UI 清单③：空态引导（当前分支无图数据 → 下一步动作）
  if(!graphState.all.nodes.length){
    el.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--mut);font-size:13.5px;gap:8px;text-align:center;padding:30px;">
      <div style="font-size:16.5px;">🕸 当前分支「${esc(getCurrentBranch())}」暂无图谱数据</div>
      <div style="font-size:12.5px;">下一步：① 数据整理·抽取候选 → ② 候选确认入库 → ③ 刷新本视图</div>
      <div style="display:flex;gap:8px;margin-top:4px;">
        <button class="btn sm" onclick="go('kb','kb-b')">→ 去数据整理</button>
        <button class="btn sm ghost" onclick="loadGraph()">🔄 刷新</button>
      </div></div>`;
    if(typeof gvStatusUpdate==='function') gvStatusUpdate();
    return;
  }
  const v = graphState.view;
  // 类型图例（当前视图）—— 2026-09-04 优化：每项可点击切换类型可见性 + 不可见类型透明度 0.4
  const legend = graphTypeStats().slice(0,9).map(([t,c])=>{
    const v = graphState.view;
    const on = !v.types.length || v.types.includes(t);
    return `<span style="display:inline-flex;align-items:center;gap:3px;cursor:pointer;opacity:${on?1:.4};user-select:none;padding:1px 3px;border-radius:4px;" onclick="graphToggleType('${esc(t)}')" title="点击切换「${esc(t)}」类型的可见性">
      <span style="width:9px;height:9px;border-radius:50%;background:${graphColor(t)};display:inline-block;border:1px solid rgba(0,0,0,.1);"></span>${esc(t)} ${c}
    </span>`;
  }).join('');
  // 2026-09-10 米爸裁剪：命名子图组件（gv-view/💾/删除）与「🔍 搜索」按钮移除——子图是低频功能且与筛选语义重叠，
  // 搜索以顶部搜索框唯一入口（+ / 快捷键），避免双入口冗余
  let s = `<div style="padding:6px 10px;font-size:12.5px;color:var(--mut);border-bottom:1px solid var(--line);background:#fafaf7;">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <!-- 2026-09-10 二次修正：筛选+统计从画布内 absolute 浮层移回工具栏行（普通流）——
           浮层 top 固定值会先后与工具栏、骨架/限高提示横幅（gvSkeletonBanner/gvLimitBanner）重叠，普通流根治 -->
      <span class="gv-dd" style="position:relative;display:inline-flex;gap:8px;align-items:center;">
        <button class="btn sm ghost" onclick="gvToggleMenu('gv-filter-dd',event)">⚙ 筛选<span id="gv-filter-n" style="color:var(--blue-d);margin-left:2px;"></span> ▾</button>
        <span id="gv-stat-chip" title="节点（视图/后端匹配总数）· 关系数" style="background:#fff;border:1px solid var(--line);border-radius:14px;padding:2px 10px;font-size:12px;color:var(--mut);"></span>
        <div id="gv-filter-dd" class="gv-dd-panel" style="display:none;position:absolute;top:100%;left:0;z-index:30;min-width:240px;background:#fff;border:1px solid var(--blue);border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);padding:10px;">
          <div style="font-weight:700;font-size:13.5px;color:var(--blue-d);margin-bottom:6px;">筛选 · 仅作用于画布</div>
          <div style="font-size:12.5px;margin-bottom:4px;color:var(--mut);">说明：左栏实体树不受此筛选影响，便于跨视图查找</div>
          <label style="display:flex;align-items:center;gap:5px;cursor:pointer;font-size:12.5px;margin-bottom:10px;"><input type="checkbox" ${v.hideIsolated?'checked':''} onchange="graphToggleIsolated(this)" style="vertical-align:middle;"> 隐藏孤立节点</label>
          <div style="display:flex;gap:6px;">
            <button class="btn sm" style="flex:1;" onclick="gvClearFilters()">↺ 清除筛选</button>
            <button class="btn sm ghost" onclick="gvCloseMenu('gv-filter-dd')">完成</button>
          </div>
        </div>
      </span>
      <span style="border-left:1px solid var(--line);height:18px;"></span>
      <span style="display:inline-flex;gap:6px;align-items:center;">
        <select onchange="graphSetLayout(this.value)" title="布局算法" style="border:1px solid var(--line);border-radius:6px;padding:2px 6px;font-size:12.5px;">
          <option value="layered" ${v.layout==='layered'?'selected':''}>📐 分层（层级）</option>
          <option value="force" ${v.layout==='force'?'selected':''}>🔀 力导向</option>
          <option value="ring" ${v.layout==='ring'?'selected':''}>⭕ 环形</option>
        </select>
        <button class="btn sm ghost" ${graphState.sk.on?'style="background:var(--blue-l);color:var(--blue-d);font-weight:600;"':''} onclick="gvToggleSkeleton()" title="骨架模式（万级图谱三级下钻）：L1 类型聚合 → 点击类型下钻 L2 实例束 → 点击实例下钻 L3 邻域">🦴 骨架</button>
        <button class="btn sm ghost" id="gv-link-btn" onclick="gvToggleLinkMode()" title="连线模式：点源节点 → 点目标节点 → 选择关系类型建立边">🔗 连关系</button>
        <button class="btn sm ghost" id="gv-link-cancel" onclick="gvLinkCancel()" style="display:none;color:${graphTok('red')};" title="退出连线模式">✕ 取消</button>
      </span>
      <!-- 2026-09-10：搜索按钮已删（顶部搜索框唯一入口，快捷键 /） -->
      <span class="gv-dd" style="position:relative;">
        <button class="btn sm ghost" onclick="gvToggleMenu('gv-export-dd',event)">导出 ▾</button>
        <div id="gv-export-dd" class="gv-dd-panel" style="display:none;position:absolute;top:100%;left:0;z-index:30;min-width:250px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);padding:6px;">
          <div style="padding:4px 8px;font-size:12px;color:var(--mut);border-bottom:1px solid var(--line);margin-bottom:4px;">范围：当前分支<b>全量</b>实体与关系（含未在画布显示的节点），非当前筛选视图</div>
          <div class="gv-dd-item" onclick="graphExport('json')" title="当前分支全量实体+关系（JSON，程序互操作 / 备份）">📦 图谱数据 (JSON)</div>
          <div class="gv-dd-item" onclick="graphExport('graphml')" title="GraphML——Gephi / yEd / Cytoscape 等图分析工具通用 interchange 格式">🔀 GraphML（Gephi/yEd）</div>
          <div class="gv-dd-item" onclick="exportSysmlModel()" title="按本体约束把当前分支图谱导出为 SysML V2 (KerML) 模型文本">📄 SysML V2 模型 (KerML)</div>
        </div>
      </span>
      <span style="flex:1;"></span>
      <!-- ⇵ 同步基线 2026-09-14 迁移至顶部分支切换入口右侧（index.html #hdr-sync-baseline，仅个人分支显示） -->
      <span id="gv-link-tip" style="display:none;color:var(--blue-d);font-weight:600;font-size:12px;">🔗 连线模式：点源节点 → 点目标节点 → 右栏选关系类型（Esc 退出）</span>
      ${gvHintBar('kg')}
    </div>
    <!-- 2026-09-04 优化：筛选 + 缩放 + 类型图例 全部迁移至画布内 absolute 浮层，避免误关联左栏实体树 -->
  </div>
  ${gvSkeletonBanner()}${gvLimitBanner()}
  <!-- 2026-09-10：筛选+统计已移回工具栏行内（普通流），画布内不再有左上浮层——骨架/限高提示横幅不再被遮挡 -->
  <!-- 画布浮层：右下 · 缩放控件（绝对定位，便于聚焦画布视图） -->
  <div id="gv-zoom-fab" style="position:absolute;right:14px;bottom:14px;z-index:6;display:inline-flex;gap:3px;align-items:center;background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:8px;padding:2px 4px;box-shadow:0 1px 4px rgba(0,0,0,.08);">
    <button class="btn sm ghost" style="padding:1px 6px;font-size:12.5px;" onclick="gvZoomOut()" title="缩小（Ctrl+滚轮向下）">−</button>
    <span id="gv-zoom-pct2" style="font-size:12.5px;color:var(--mut);min-width:38px;text-align:center;cursor:pointer;" title="点击重置视图" onclick="gvZoomReset()">100%</span>
    <button class="btn sm ghost" style="padding:1px 6px;font-size:12.5px;" onclick="gvZoomIn()" title="放大（Ctrl+滚轮向上）">＋</button>
    <button class="btn sm ghost" style="padding:1px 6px;font-size:12.5px;" onclick="gvZoomReset()" title="重置视图（⌂）">⌂</button>
  </div>
  <!-- 画布浮层：左下 · 类型图例（绝对定位；点击图例项切换类型可见性） -->
  <div id="gv-legend-fab" style="position:absolute;left:14px;bottom:14px;z-index:6;background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:8px;padding:5px 8px;font-size:12.5px;color:var(--mut);box-shadow:0 1px 4px rgba(0,0,0,.08);max-width:420px;max-height:160px;overflow-y:auto;">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">${legend||'<span style="color:var(--mut);">暂无类型</span>'}</div>
  </div>
  <svg id="gv-svg" width="${W}" height="${H}" style="background:${k.canvas};display:block;width:100%;height:calc(100% - 36px);touch-action:none;"
    onmousedown="graphSvgDown(event)" onmousemove="graphSvgMove(event)" onmouseup="graphSvgUp(event)" onwheel="graphSvgWheel(event)">
  <rect x="-200" y="-200" width="${W+400}" height="${H+400}" fill="url(#gv-grid)" pointer-events="none"/>
  <g id="gv-inner" transform="translate(${graphState.vp.panX} ${graphState.vp.panY}) scale(${graphState.vp.scale})">`;
  // ── 选中元素邻接高亮：点节点 → 相连边+连接节点高亮；点边 → 两端节点高亮；其余淡出 ──
  const actNode = (graphState.sel && graphState.sel.type==='node') ? graphState.sel.id : null;
  const actEdge = (graphState.sel && graphState.sel.type==='edge') ? String(graphState.sel.id) : null;
  const neighbor = new Set(), actEdgeIds = new Set();
  if(actNode){
    graphState.edges.forEach(e=>{
      if(e.source_id===actNode){ neighbor.add(e.target_id); actEdgeIds.add(String(e.id)); }
      if(e.target_id===actNode){ neighbor.add(e.source_id); actEdgeIds.add(String(e.id)); }
    });
  } else if(actEdge){
    const e = graphState.edges.find(x=>String(x.id)===actEdge);
    if(e){ neighbor.add(e.source_id); neighbor.add(e.target_id); actEdgeIds.add(actEdge); }
  }
  const dim = !!(actNode || actEdge);  // 存在选中元素时其余元素淡出
  // edges —— 2026-09-12 展示优化：贝塞尔曲线边（gvEdgePath）＋ 同对多重边曲率偏移防重叠 ＋ 边标签取曲线中垂线
  // P0-3（2026-11-28）：把每条边几何（两端 + 曲率偏移）记入 graphState._edgeGeom，
  //   拖拽时只按此表重算**相邻边**的 d 与标签坐标，不再整图 innerHTML 重建（见 gvPatchNode）。
  const _eb={}, _ed={}, _geom = graphState._edgeGeom = {};
  graphState.edges.forEach(e=>{ const kk=[e.source_id,e.target_id].sort().join('|'); _eb[kk]=(_eb[kk]||0)+1; });
  graphState.edges.forEach(e=>{
    const sN = graphState.nodes.find(n=>n.id===e.source_id);
    const tN = graphState.nodes.find(n=>n.id===e.target_id);
    if(!sN||!tN) return;
    const x1=sN.x, y1=sN.y, x2=tN.x, y2=tN.y;
    const kk=[e.source_id,e.target_id].sort().join('|');
    const total=_eb[kk]||1, idx=(_ed[kk]=(_ed[kk]||0))+1;
    const off = total<=1 ? 0.22 : ((idx - (total+1)/2) * 0.5);
    const d = gvEdgePath(x1,y1,x2,y2,off);
    const md = gvEdgeMid(x1,y1,x2,y2,off);
    const isAct = actNode ? actEdgeIds.has(String(e.id)) : (actEdge ? String(e.id)===actEdge : false);
    _geom[String(e.id)] = {a:String(e.source_id), b:String(e.target_id), off:off};
    // 选中关联边方向动效：出边(success→target)正向流动、入边(target→选中)反向流动
    // 方向必须准确：流动恒沿边的真实方向 source->target（正向），不随选中端点改变
    const flowCls = isAct ? 'gv-flow-out' : '';
    const eo = isAct ? 1 : (dim ? 0.12 : 1);
    const inf = !!e._inferred;   // 推断边（推理叠加）：橙色虚线 + 橙色箭头，与原始边明确区分
    const eStroke = isAct ? k.edgeActive : (inf ? k.edgeInfer : k.edge);
    const eDash = inf ? ' stroke-dasharray="6 4"' : '';
    const eMarker = inf ? 'url(#gv-arrow-inf)' : 'url(#gv-arrow)';
    s += `<path id="gv-edge-${e.id}" d="${d}" fill="none" stroke="${eStroke}" stroke-width="${isAct?2.2:1.3}"${eDash} opacity="${eo}" marker-end="${eMarker}"${flowCls ? ' class="'+flowCls+'"' : ''} style="cursor:pointer;filter:drop-shadow(0 .5px 1px rgba(0,0,0,.08));" onclick="event.stopPropagation();graphEdgeClick('${e.id}')"/>`;
    s += `<text id="gv-elabel-${e.id}" x="${md.x}" y="${md.y+3}" font-size="10" fill="${inf?k.edgeInfer:k.edgeLabel}" text-anchor="middle" opacity="${eo}" pointer-events="none">${e.relation_type}${inf?' ⇢':''}</text>`;
  });
  // nodes（节点半径按关系度：连通度越大越显著；选中/邻居高亮）—— 2026-09-12 展示优化：径向高光＋阴影光晕＋类型标签移入圆内＋hover 提示
  const deg = graphDegreeMap(graphState.nodes, graphState.edges);
  const ro = isReleaseBranch(getCurrentBranch());   // release 只读：禁删除/拖拽（不影响展示）
  graphState.nodes.forEach(n=>{
    const c = graphColor(n.entity_type);
    const sel = graphState.sel&&graphState.sel.type==='node'&&graphState.sel.id===n.id;
    const nb = dim && neighbor.has(n.id);
    const linkHi = (typeof gvLinkMode!=='undefined') && gvLinkMode && gvLinkSrc===n.id;   // 连线模式源点高亮
    const tint = gvTypeTint(c);
    const fill = n._skel ? k.skelFill : (linkHi ? k.linkFill : (sel ? k.selFill : (nb ? k.nbrFill : tint)));
    const sw = linkHi ? 4 : (sel ? 3 : (nb ? 2.5 : 1.5));
    const no = (dim && !sel && !nb) ? 0.22 : 1;
    const r = n._skel ? 20 + Math.min((n._count||1)*0.9, 13) : 13 + Math.min((deg[n.id]||0)*1.7, 13);   // 2026-09-13 节点加大更醒目
    const hiR = Math.max(5, r*0.42);
    s += `<g class="gv-node${sel?' gv-sel-node':''}" data-nid="${esc(n.id)}" transform="translate(${n.x},${n.y})" style="cursor:pointer;opacity:${no};"
      onmousedown="event.stopPropagation();graphNodeDown(event,'${esc(n.id)}')"
      onmouseenter="gvNodeHover('${esc(n.id)}')" onmouseleave="gvNodeHoverEnd()">
      <circle r="${r}" fill="${fill}" stroke="${nb?k.nbrStroke:c}" stroke-width="${sw}" ${n._skel?'stroke-dasharray="4 2"':''} style="filter:drop-shadow(0 1px 2px rgba(0,0,0,.16));"/>
      ${n._skel
        ? `<text y="4" font-size="10.5" fill="${c}" text-anchor="middle" font-weight="700">×${n._count}</text>`
        : `<circle r="${hiR}" fill="${k.canvas}" opacity="0.36" pointer-events="none"/>
           ${r >= 16 ? `<text y="${Math.min(2, r*0.3)}" font-size="9.5" fill="${k.canvas}" text-anchor="middle" font-weight="700" pointer-events="none">${esc(n.entity_type)}</text>` : ''}
           <text y="${r+14}" font-size="11.5" fill="${k.text}" text-anchor="middle" font-weight="600" pointer-events="none">${esc(n.name.length>10?n.name.slice(0,10)+'…':n.name)}</text>`}
    </g>`;
  });
  s += `<defs><marker id="gv-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="${k.edge}"/></marker>
  <marker id="gv-arrow-inf" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="${k.edgeInfer}"/></marker>
  <pattern id="gv-grid" width="26" height="26" patternUnits="userSpaceOnUse"><path d="M 26 0 L 0 0 0 26" fill="none" stroke="${k.grid}" stroke-width="1"/></pattern></defs>`;
  s += '</g></svg>';
    el.innerHTML = s;
    const _mmEl = document.getElementById('gv-minimap'); if(_mmEl) el.appendChild(_mmEl);   // 2026-09-12 minimap 挂回（renderGraph 内 innerHTML 会清空画布容器）
  gvUpdateStatusPill(); gvRenderFilterBadge();
  if(typeof gvStatusUpdate==='function') gvStatusUpdate();
}
/* ══ P0-3（2026-11-28）拖拽增量重绘 ══
   原实现每次 mousemove 都调 renderGraph()（el.innerHTML 全量重建整图，大图掉帧）。
   现在拖拽期间只改被拖节点的 <g transform> 与其相邻边的 d / 标签坐标（rAF 合帧），
   落位后由 mouseup 统一 renderGraph() 一次。依赖 renderGraph 写入的 graphState._edgeGeom。 */
let _gvPatchRaf = 0, _gvPatchNid = null;
function gvPatchNodeSoon(nid){
  _gvPatchNid = nid;
  if(_gvPatchRaf) return;
  _gvPatchRaf = requestAnimationFrame(()=>{ _gvPatchRaf = 0; gvPatchNode(_gvPatchNid); });
}
function gvPatchNode(nid){
  const svg = document.getElementById('gv-svg');
  if(!svg || nid == null) return;
  const nodes = graphState.nodes || [];
  const byId = new Map(nodes.map(x=>[String(x.id), x]));
  const n = byId.get(String(nid));
  if(!n) return;
  // 1) 节点本体：只改 transform（不重建 <g>，事件绑定与过渡动画都保住）
  const gs = svg.querySelectorAll('.gv-node');
  for(let i=0;i<gs.length;i++){
    if(gs[i].getAttribute('data-nid') === String(nid)){
      gs[i].setAttribute('transform', `translate(${n.x},${n.y})`);
      break;
    }
  }
  // 2) 相邻边：按缓存几何重算贝塞尔路径与标签位置（非相邻边一字不改）
  const geom = graphState._edgeGeom || {};
  for(const eid in geom){
    const gm = geom[eid];
    if(gm.a !== String(nid) && gm.b !== String(nid)) continue;
    const sN = byId.get(gm.a), tN = byId.get(gm.b);
    if(!sN || !tN) continue;
    const path = document.getElementById('gv-edge-' + eid);
    if(path) path.setAttribute('d', gvEdgePath(sN.x, sN.y, tN.x, tN.y, gm.off));
    const lb = document.getElementById('gv-elabel-' + eid);
    if(lb){
      const md = gvEdgeMid(sN.x, sN.y, tN.x, tN.y, gm.off);
      lb.setAttribute('x', md.x); lb.setAttribute('y', md.y + 3);
    }
  }
}
// ── 左栏实体树（三栏化 2026-09-03）：类型继承树（ontology parent_id）+ 实例挂载 + 折叠/过滤 + 双向联动 ──
// 树数据 = 本体类型树（复用 ontTypeTree，与 kb-c 同构）+ graphState.all.nodes 按 entity_type 挂载；
// 折叠状态存类型名集合（默认全展开，点 ▸ 折叠——懒展开语义：折叠层不渲染子节点）
const gvTreeSt = { collapsed:new Set(), filter:'' };
async function gvEnsureTypes(){
  // 直接拉本体类型（不调 loadOntology，避免触发 kb-c 渲染/选中副作用）；成功后回填 ontData.types 供 ontTypeTree 复用
  if(ontData.types && ontData.types.length) return ontData.types;
  try{
    const r = await api('/api/knowledge/ontology');
    const types = (r && r.types) || r || [];
    types.forEach(t=>{
      if(typeof t.constraints==='string'){ try{ t.constraints=JSON.parse(t.constraints||'{}'); }catch(e){ t.constraints={}; } }
      if(typeof t.properties==='string'){ try{ t.properties=JSON.parse(t.properties||'{}'); }catch(e){ t.properties={}; } }
    });
    ontData.types = types;
  }catch(e){ ontData.types = []; }
  return ontData.types;
}
function gvTreeRow(n, depth){
  const sel = graphState.sel && graphState.sel.type==='node' && graphState.sel.id===n.id;
  const deg = (graphState.all.edges||[]).filter(e=>e.source_id===n.id||e.target_id===n.id).length;
  // 2026-09-10 状态机收口：画布实体全为 reviewed（入图即确认），状态色点失去信息量 → 移除；
  // 孤立实体（无关系）显式显示 0 + 整行淡化，可辨识、可点击
  const iso = !deg;
  const rio = `<span class="gv-rio">
    <span class="gv-act" onclick="event.stopPropagation();gvEditInstance('${esc(n.id)}')" title="编辑实例">✎</span>
    <span class="gv-act gv-del" onclick="event.stopPropagation();gvDelInstance('${esc(n.id)}')" title="删除实例">🗑</span>
  </span>`;
  return `<div id="gvtn-${esc(n.id)}" class="ont-tnd${sel?' on':''}" style="padding-left:${depth*16+28}px;${iso?'opacity:.62;':''}" onclick="gvTreeSelEntity('${esc(n.id)}')" title="${esc(n.name)} · 关系 ${deg}">
    <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(n.name)}</span>
    <span style="color:var(--mut);font-size:11.5px;">${iso?'0':deg}</span>
    ${rio}
  </div>`;
}
// 类型继承族：类型 + 全部后代类型（parent_id 递归闭包）——父类型的计数/筛选包含子类型实例（Protégé 继承归组语义）
function gvTypeFamily(name){
  const fam = new Set([name]);
  let grew = true;
  while(grew){
    grew = false;
    (ontData.types||[]).forEach(t=>{
      if(!t.parent_id || fam.has(t.name)) return;
      const p = ontData.types.find(x=>x.id===t.parent_id);
      if(p && fam.has(p.name)){ fam.add(t.name); grew = true; }
    });
  }
  return fam;
}
// 树选中 → 图视图平移把该节点带到画布中央（配合原生 1-hop 聚焦）
function gvCenterOnNode(id){
  const n = graphState.nodes.find(x=>x.id===id);
  if(!n || n.x==null) return;
  const svg = document.getElementById('gv-svg');
  if(!svg) return;
  const vw = svg.clientWidth||800, vh = svg.clientHeight||400;
  const s = graphState.vp.scale || 1;
  graphState.vp.panX = vw/2 - s*n.x;
  graphState.vp.panY = vh/2 - s*n.y;
  gvApplyView();
}
// 首次加载自适应缩放：限高后画布世界坐标可能超出可视区，按节点包围盒 fit（不改变布局）
function gvFitView(){
  const ns = graphState.nodes; if(!ns.length) return;
  const svg = document.getElementById('gv-svg'); if(!svg) return;
  let minX=1e9,minY=1e9,maxX=-1e9,maxY=-1e9;
  ns.forEach(n=>{ if(n.x==null) return; minX=Math.min(minX,n.x-70); maxX=Math.max(maxX,n.x+70); minY=Math.min(minY,n.y-45); maxY=Math.max(maxY,n.y+45); });
  if(minX>maxX) return;
  const vw = svg.clientWidth||800, vh = svg.clientHeight||400;
  const sc = Math.max(0.3, Math.min(1.2, Math.min(vw/(maxX-minX), vh/(maxY-minY))));
  graphState.vp = { scale: sc, panX: (vw - sc*(minX+maxX))/2, panY: (vh - sc*(minY+maxY))/2 };
  gvApplyView();
}


//═══════════ 图库实例 CRUD（树顶新增 + 节点 hover 编辑/删除；复用后端 /api/knowledge/graph/nodes 本体校验）═══════════
// 图库实例 CRUD 状态（2026-09-12 Schema 受控表单）
let gvcPropDefs = [];
let gvcParentId = '';
const _gvRioStyle = '__GVRiOSTYLE__';
if(!document.getElementById(_gvRioStyle)){
  const s = document.createElement('style'); s.id=_gvRioStyle;
  s.textContent = '.ont-tnd .gv-rio{display:none;flex:none;margin-left:auto;align-items:center;gap:2px}'+'.ont-tnd:hover .gv-rio{display:inline-flex}'+'.gv-rio .gv-act{display:inline-flex;width:17px;height:17px;align-items:center;justify-content:center;border-radius:4px;font-size:12.5px;line-height:1;color:var(--mut);cursor:pointer}'+'.gv-rio .gv-act:hover{background:var(--blue-l);color:var(--blue)}'+'.gv-rio .gv-del:hover{background:var(--red);color:#fff}';
  document.head.appendChild(s);
}
function _gvErr(msg){
  const e = document.getElementById('gvc-err');
  if(e){ e.style.display=''; e.textContent = String(msg||''); }
}
async function gvInstanceForm(id, preselectParentId){
  const branch = getCurrentBranch();
  if(isReleaseBranch(branch)){ toast('🔒 发布分支只读，不可新增/编辑实例'); return; }
  if(!((ontData||{}).types||[]).length){ try{ await gvEnsureTypes(); }catch(e){} }
  const nodes = graphState.all.nodes || [];
  const n = id ? nodes.find(x=>x.id===id) : null;
  if(id && !n){ toast('实例不存在，请刷新'); return; }
  const isEdit = !!id;
  const allTypes = (ontData.types||[]).filter(t=>t.type_kind==='entity' && !((t.constraints||{}).abstract));
  const initialType = isEdit ? n.entity_type : (allTypes.length ? allTypes[0].name : '');
  // 类型选项：新增=全部实体；编辑=安全迁移目标（祖先 ∪ 同父类型族）
  const typeOpts = isEdit ? gvMigratableTypes(n.entity_type) : allTypes.map(t=>t.name);
  const typeHtml = typeOpts.map(t=>`<option value="${esc(t)}" ${(isEdit&&t===n.entity_type)||(!isEdit&&t===initialType)?'selected':''}>${esc(t)}</option>`).join('');
  // 父实例（新建）：默认当前选中节点；可选组合/包含挂载为子级
  gvcParentId = (!isEdit && preselectParentId) ? preselectParentId
              : (!isEdit && graphState.sel && graphState.sel.type==='node') ? graphState.sel.id : '';
  const parenOpts = nodes.map(x=>`<option value="${esc(x.id)}" ${x.id===gvcParentId?'selected':''}>${esc(x.name)}（${esc(x.entity_type||'')}）</option>`).join('');
  // 对象属性（关系）区：按初始类型列出可用的关系类型 + 可选目标实例
  const relHtml = gvcRelHtml(gvcRelList(initialType), id ? n.id : '');
  openPanel(isEdit?('✏️ 编辑实例：'+esc(n.name)):'➕ 新增实例', `
    <div style="display:flex;flex-direction:column;gap:10px;min-width:380px;max-width:430px;box-sizing:border-box;">
      <div><label style="font-size:12.5px;color:var(--mut);">实例名称</label>
        <input id="gvc-name" value="${esc(id&&n?n.name:'')}" placeholder="必填" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 9px;font-size:14.5px;box-sizing:border-box;"></div>
      <div><label style="font-size:12.5px;color:var(--mut);">本体类型${isEdit?'（改类仅限安全迁移）':''}</label>
        <select id="gvc-type" onchange="gvcOnTypeChange()" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:14.5px;box-sizing:border-box;">${typeHtml||'<option value="">未定义</option>'}</select></div>
      ${(!isEdit && nodes.length)?`<div><label style="font-size:12.5px;color:var(--mut);">父实例（组合/包含挂载，可选）</label>
        <select id="gvc-parent" onchange="gvcParentId=this.value" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:13.5px;box-sizing:border-box;"><option value="">— 不挂父（作为根实例）—</option>${parenOpts}</select></div>`:''}
      <div><label style="font-size:12.5px;color:var(--mut);">数据属性（按本体 Schema 受控填写）</label>
        <div id="gvc-props" style="display:flex;flex-direction:column;gap:8px;border:1px solid var(--line);border-radius:6px;padding:8px 10px;background:#fcfcfb;max-height:240px;overflow:auto;">加载属性…</div></div>
      ${relHtml}
      <div id="gvc-err" style="color:#A32D2D;font-size:13.5px;display:none;background:#fff5f5;border:1px solid #F3C1C1;border-radius:6px;padding:6px 9px;white-space:pre-wrap;"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn sm ghost" onclick="closePanel()">取消</button>
        <button class="btn sm" onclick="gvcSubmit('${id||''}')">保存</button>
      </div>
    </div>`);
  const curType = (document.getElementById('gvc-type')||{}).value || '';
  gvcRenderProps(curType, n ? (n.properties||{}) : {}, isEdit);
}
async function gvcSubmit(id){
  const name = (document.getElementById('gvc-name')||{}).value || '';
  const etype = (document.getElementById('gvc-type')||{}).value || '';
  if(!name.trim()){ _gvErr('❌ 实例名称必填'); return; }
  if(!etype){ _gvErr('❌ 请选择本体类型'); return; }
  const props = gvcCollectProps();
  if(!gvcCheckRequired(props)) return;
  const branch = getCurrentBranch();
  const body = {name, entity_type:etype, properties:props, branch, x:0, y:0};
  try{
    let nid = id;
    if(id){
      const r = await api('/api/knowledge/graph/nodes/'+encodeURIComponent(id), {method:'PUT', body:JSON.stringify(body)});
      if(r && r.error){ _gvErr('❌ '+(r.error||'')); toast('未通过本体约束'); return; }
    } else {
      const r = await api('/api/knowledge/graph/nodes', {method:'POST', body:JSON.stringify(body)});
      if(!r || r.error){ _gvErr('❌ '+(r && r.error || '创建失败')); return; }
      nid = r.id;
    }
    // 汇总要建立的对象属性（关系）边：组合父 + 表单勾选的对象关系
    const edges = [];
    if(!id && gvcParentId && gvcParentId !== nid) edges.push({source_id:gvcParentId, target_id:nid, kind:'parent'});
    gvcCollectRels().forEach(x=>{ if(x.target_id && x.target_id!==nid) edges.push({source_id:nid, target_id:x.target_id, kind:x.rel}); });
    let okCnt = 0; const fails = [];
    for(const e of edges){
      const rels = e.kind==='parent' ? ['CONTAINS','COMPOSED_OF'] : [e.kind];
      let made = false, msg = '';
      for(const rt of rels){
        try{
          const er = await api('/api/knowledge/graph/edges', {method:'POST', body:JSON.stringify({source_id:e.source_id, target_id:e.target_id, relation_type:rt, props:{}, branch})});
          if(!er || !er.error){ made = true; break; }
          msg = er.error;
        }catch(err){ msg = err.message || String(err); }
      }
      if(made) okCnt++; else fails.push((e.kind==='parent'?'挂父':e.kind)+'：'+msg);
    }
    const verb = id ? '已保存' : '已新增';
    if(fails.length) toast(`✅ 实例${verb}（建立了 ${okCnt} 条关系）\n⚠ 部分关系失败：`+fails.slice(0,3).join('；'));
    else toast(`✅ 实例${verb}`+(okCnt?`，建立了 ${okCnt} 条关系`:''));
    closePanel(); loadGraph(); renderEntityTree();
  }catch(e){ _gvErr('❌ '+(e.message||String(e))); }
}
// 某类型的祖先链（含自身）——用于对象属性 src 匹配（validate_edge 同语义）
function gvAncestors(type){
  const types=ontData.types||[]; const byId={}; types.forEach(t=>byId[t.id]=t);
  const byName={}; types.forEach(t=>byName[t.name]=t);
  const pname=t=>(t&&t.parent_id!=null&&byId[t.parent_id])?byId[t.parent_id].name:null;
  const anc=new Set(); const seen=new Set(); let cur=byName[type];
  while(cur && !seen.has(cur.name)){ seen.add(cur.name); anc.add(cur.name); cur=pname(cur)?byName[pname(cur)]:null; }
  return anc;
}
// 该实例类型可建立的对象属性（关系）：关系类型 src 匹配（当前类型或其祖先），目标实例按 tgt 类型族过滤
function gvcRelList(type){
  const rels=(ontData.types||[]).filter(t=>t.type_kind==='relation' && (t.status||'active')!=='deprecated');
  const nodes=graphState.all.nodes||[];
  const anc=gvAncestors(type);
  const out=[];
  rels.forEach(r=>{
    try{
      const av=(r.constraints||{}).allowed_values||{};
      let src=av.src||[], tgt=av.tgt||[];
      if(typeof src==='string') src=[src]; if(typeof tgt==='string') tgt=[tgt];
      if(src.length && !src.some(s=>anc.has(s))) return;             // 当前类型(或祖先)不在允许来源 → 不可建
      const targets = tgt.length
        ? nodes.filter(n=>tgt.some(t=>gvTypeFamily(t).has(n.entity_type)))
        : nodes.slice();
      if(targets.length) out.push({name:r.name, targets});
    }catch(e){}
  });
  return out;
}
// 渲染对象属性（关系）区；selfId 用于剔除将连成环/自环的自身
function gvcRelHtml(rels, selfId){
  if(!rels.length) return '';
  const rows = rels.map(r=>{
    const opts = r.targets.filter(x=>x.id!==selfId).map(x=>`<option value="${esc(x.id)}">${esc(x.name)}（${esc(x.entity_type||'')}）</option>`).join('');
    return `<div style="display:flex;gap:6px;align-items:center;"><span style="flex:0 0 112px;font-size:12.5px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="关系类型 ${esc(r.name)}">${esc(r.name)}</span>
      <select data-rel="${esc(r.name)}" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 6px;font-size:12.5px;box-sizing:border-box;"><option value="">— 建立（选目标实例）—</option>${opts}</select></div>`;
  }).join('');
  return `<details style="margin-top:2px;">
    <summary style="cursor:pointer;font-size:12.5px;color:var(--blue-d);user-select:none;">🔗 对象属性（关系）— 可选 ${rels.length} 个关系类型</summary>
    <div style="display:flex;flex-direction:column;gap:6px;border:1px solid var(--line);border-radius:6px;padding:8px;margin-top:6px;background:#fcfcfb;">${rows}</div>
  </details>`;
}
// 收集表单勾选的对象关系
function gvcCollectRels(){
  const out=[];
  (document.querySelectorAll ? document.querySelectorAll('[data-rel]') : []).forEach(s=>{
    const tid=s.value; if(!tid) return;
    const rel=s.getAttribute && s.getAttribute('data-rel');
    if(rel) out.push({rel, target_id:tid});
  });
  return out;
}

function gvMigratableTypes(name){
  const types = (ontData.types||[]);
  if(!name || !types.length) return [];
  const byId={}, byName={}; types.forEach(t=>{ byId[t.id]=t; byName[t.name]=t; });
  const pname = t => (t && t.parent_id!=null && byId[t.parent_id]) ? byId[t.parent_id].name : null;
  const anc=[]; const seen=new Set(); let cur=byName[name];
  while(cur && !seen.has(cur.name)){ seen.add(cur.name); anc.push(cur.name); cur = pname(cur) ? byName[pname(cur)] : null; }
  const children={}; types.forEach(t=>{ const p=pname(t); if(p) (children[p]=children[p]||[]).push(t.name); });
  const pool=[...anc];                       // 祖先（含自身）
  const walk=r=>{ (children[r]||[]).forEach(c=>{ pool.push(c); walk(c); }); };
  walk(pname(byName[name]) || name);         // 直接父的整棵子树（同父族）；根类型=自身族
  return [...new Set(pool)];
}
// 单个属性的受控控件（XSD 类型 + 取值白名单 → 控件）
function gvcField(prop, value){
  const req = !!prop.required, id = 'gvc-f-'+prop.name;
  const av = (prop.allowed_values||[]).filter(x=>x!=null && x!=='');
  const t = String(prop.type||'').toLowerCase();
  let ctl;
  if(av.length){
    ctl = `<select id="${id}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:13.5px;box-sizing:border-box;"><option value="">—</option>`+
      av.map(v=>`<option value="${esc(String(v))}" ${String(v)===String(value)?'selected':''}>${esc(String(v))}</option>`).join('')+`</select>`;
  } else if(t.includes('bool')){
    ctl = `<select id="${id}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:13.5px;box-sizing:border-box;"><option value="">—</option><option value="true" ${String(value)==='true'||value===true?'selected':''}>是</option><option value="false" ${String(value)==='false'||value===false?'selected':''}>否</option></select>`;
  } else if(/int|long|decimal|double|float|numeric/.test(t)){
    ctl = `<input id="${id}" type="number" step="any" value="${esc(value==null?'':value)}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:14.5px;box-sizing:border-box;">`;
  } else if(t.includes('date')){
    ctl = `<input id="${id}" type="date" value="${esc(value==null?'':String(value).slice(0,10))}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:14.5px;box-sizing:border-box;">`;
  } else {
    ctl = `<input id="${id}" type="text" value="${esc(value==null?'':value)}" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:14.5px;box-sizing:border-box;">`;
  }
  return `<div style="display:flex;flex-direction:column;gap:3px;"><label style="font-size:12.5px;color:var(--mut);display:flex;align-items:center;gap:4px;">${esc(prop.name)}${req?'<span style="color:#A32D2D;">*</span>':''}${prop.unit?`<span class="tag" style="font-size:11.5px;">${esc(prop.unit)}</span>`:''}${prop.description?`<span style="font-size:11.5px;color:var(--line);cursor:help;" title="${esc(prop.description)}">ⓘ</span>`:''}</label>${ctl}</div>`;
}
async function gvcOnTypeChange(){
  const el = document.getElementById('gvc-type'); if(!el) return;
  gvcRenderProps(el.value, {}, false);
}
// 收集受控字段 → properties（按 XSD 类型做值转换）
function gvcCollectProps(){
  const props = {};
  gvcPropDefs.forEach(p=>{
    const el = document.getElementById('gvc-f-'+p.name); if(!el) return;
    const raw = el.value;
    if(raw==null || raw==='') return;
    const t = String(p.type||'').toLowerCase();
    if(t.includes('bool')) props[p.name] = (raw==='true');
    else if(/int|long|decimal|double|float|numeric/.test(t)) props[p.name] = Number(raw);
    else props[p.name] = raw;
  });
  const ex = document.getElementById('gvc-extra');
  if(ex){ const txt = ex.value.trim(); if(txt){ try{ Object.assign(props, JSON.parse(txt)); }catch(e){} } }
  return props;
}
function gvcCheckRequired(props){
  const miss = gvcPropDefs.filter(p=>p.required && (props[p.name]===null || props[p.name]===undefined || props[p.name]===''));
  if(miss.length){ _gvErr('❌ 以下必填属性未填写：'+miss.map(p=>p.name).join('、')); return false; }
  return true;
}
function gvAddInstance(preselectParentId){ gvInstanceForm(null, preselectParentId); }
function gvEditInstance(id){ gvInstanceForm(id); }

async function gvDelInstance(id){
  const branch = getCurrentBranch();
  if(isReleaseBranch(branch)){ toast('🔒 发布分支只读，不可删除实例'); return; }
  if(!(await confirmDialog('确认删除该实例？将一并删除其关联边（仅当前分支，其余分支保留）。'))) return;
  try{
    const r = await api('/api/knowledge/graph/nodes/'+encodeURIComponent(id)+'?branch='+encodeURIComponent(branch), {method:'DELETE'});
    if(r && r.error){ toast('删除失败：'+r.error); return; }
    toast('🗑 实例已删除'); loadGraph(); renderEntityTree();
  }catch(e){ toast('删除失败：'+(e.message||String(e))); }
}
