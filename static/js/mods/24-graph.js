/* 图谱：着/ 筛/ 骨架 / 布局算法
 * static/index.html 巨型 inline script 机械切分而来
 * 原行13272-13690  ·  全局作用域（module），内联 onclick 依赖全局函数
 */
/* ══ P0-1（2026-11-28）图谱样式单一真源 ══
   Cytoscape 的 style 数组不接受 var()，所以首次取色时把 CSS 变量实值读一次、
   缓存进 _gvTokens；主画布 SVG / SysML Cytoscape / 影响图 Cytoscape / minimap 全从这里取色。
   —— 换品牌色只改 tokens.css 一处；未覆盖类型仍走稳定色带 GRAPH_PALETTE。

   ⚠ 只读 --graph-*（图谱专用色板），**不得读 --color-***
   图谱画布是固定白纸面，不随 UI 主题明暗切换；--color-primary 在
   [data-theme="dark"] 里被覆盖成 #5b9ce6，读它会让白底画布出现色差。
   测试位：verify_graph_p0_render.py E 组（图谱色不随 data-theme 变化）。 */
let _gvTokens = null;
function graphTok(name){
  if(!_gvTokens){
    const pick = (v, fb) => {
      try{
        const got = getComputedStyle(document.documentElement).getPropertyValue(v).trim();
        return got || fb;
      }catch(e){ return fb; }
    };
    _gvTokens = {
      // 品牌 / 语义（tokens.css 的 --graph-* 专用色板；fallback 与 :root 同值）
      primary:   pick('--graph-primary',   '#185FA5'),
      primaryD:  pick('--graph-primary-d', '#0C447C'),
      primaryL:  pick('--graph-primary-l', '#E6F1FB'),
      green:     pick('--graph-green',     '#3B6D11'),
      greenL:    pick('--graph-green-l',   '#EAF3DE'),
      amber:     pick('--graph-amber',     '#BA7517'),
      amberL:    pick('--graph-amber-l',   '#FAEEDA'),
      red:       pick('--graph-red',       '#A32D2D'),
      redL:      pick('--graph-red-l',     '#FCEBEB'),
      ink:       pick('--graph-ink',       '#1a2332'),
      muted:     pick('--graph-muted',     '#5a6678'),
      line:      pick('--graph-line',      '#d5dce6'),
      // 图谱专用（tokens.css 无同名变量，集中在此；其它文件禁止再写死）
      canvas:    '#ffffff',
      grid:      '#ECEDE7',
      edge:      '#B4B2A9',
      edgeActive:'#D85A30',
      edgeInfer: '#E8912D',
      edgeLabel: '#5F5E5A',
      text:      '#444441',
      nodeSub:   '#888888',
      teal:      '#0F6E56',
      greenDeep: '#2E7D32',
      ground:    '#00796B',
      userSeg:   '#00838F',
      link:      '#7F77DD',
      rust:      '#993C1D',
      slate:     '#5E7A99',
      purple:    '#6B4FA1',
      purpleL:   '#F1E8F9',
      neutral:   '#5F5E5A',
      neutralL:  '#F2F4F8',
      selFill:   '#FAC775',
      nbrStroke: '#D85A30',
      nbrFill:   '#FFF1B8',
      skelFill:  '#FFF9EC',
      linkFill:  '#CFE8FF',
      miniNode:  '#9DB8D8',
      miniEdge:  '#C9CCC4',
      // 视图专用深浅（同一语义的多档色阶，集中在此避免各文件各写一份）
      reqFill:   '#FFF7E0',   // SysML 需求框底
      stateFill: '#FDF0F0',   // 状态/迁移底
      parentFill:'#F5F8FC',   // SysML 复合节点（父包）底
      nodeText:  '#1F2D3D',   // Cytoscape 节点文字
      edgeText:  '#555555',   // Cytoscape 边标签
      redDeep:   '#7A1F1F',   // 影响图变更源描边
      amberSoft: '#F8B24B',   // 影响图直接影响
      amberBd:   '#D98E10',
      slateLight:'#C7CFDA',   // 影响图间接影响
      slateMid:  '#8A94A2',
      traceFill: '#EDF1F6',   // 影响图追溯提示
      traceLine: '#9AA7B5',
      edgeImp:   '#B9C2CE',   // 影响图常规边
      amberEdge: '#F5A623',   // 影响图中风险边
      // 焦点压暗强度：三套画布**同一权重**（原 SysML 0.18 / 影响图 0.12 各写一份 ⇒ 语义漂移）
      dimOpacity: 0.18
    };
  }
  return name ? _gvTokens[name] : _gvTokens;
}
// 实体类型 → 语义色：全部由 token 派生（同一语义只有一处定义）
let _gvColorTable = null;
function graphColorTable(){
  if(_gvColorTable) return _gvColorTable;
  const k = graphTok();
  _gvColorTable = {
    '需求':k.primary,'利益相关方需求':k.primary,'系统需求':k.primary,'子系统需求':k.primary,'单元需求':k.primary,
    '部件':k.teal,'载荷':k.teal,'通信载荷':k.teal,'转发器':k.teal,'天线':k.teal,'相控阵天线':k.teal,'功率放大器':k.teal,'变频器':k.teal,'滤波器':k.teal,'TWTA':k.teal,
    '卫星系统':k.greenDeep,'卫星平台':k.greenDeep,'电源分系统':k.greenDeep,'姿轨控分系统':k.greenDeep,'测控分系统':k.greenDeep,'热控分系统':k.greenDeep,
    '地面段':k.ground,'信关站':k.ground,'测控站':k.ground,'地面站':k.ground,
    '用户段':k.userSeg,'用户终端':k.userSeg,
    '通信链路':k.link,'上行链路':k.link,'下行链路':k.link,'连接':k.link,'接口':k.link,
    '功能':k.amber,'用例':k.amber,'利益相关方':k.amber,
    '验证活动':k.rust,'约束':k.rust,
    '系统元素':k.slate
  };
  return _gvColorTable;
}
// 12 色稳定色带：未覆盖类型的确定性散列色（非品牌色，不随 token 变）
const GRAPH_PALETTE = ['#3F7CB8','#4E9A7A','#B0813C','#9C6BD6','#C36A9E','#5B8FB5',
'#8FA653','#C2764F','#6E6FC4','#4FA3A5','#A5679B','#7A8E6B'];
function graphHashColor(type){
  let h = 0;
  const s = String(type||'');
  for(let i=0;i<s.length;i++) h = (h*31 + s.charCodeAt(i)) >>> 0;
  return GRAPH_PALETTE[h % GRAPH_PALETTE.length];
}
function graphColor(type){
  const t = type || '';
  const T = graphColorTable();
  // 1) 精确匹配（本体类型名动态注册优先）
  if(T[t]) return T[t];
  // 2) 最长子串匹配（消除 includes 顺序歧义，系统需求/系统元素不再互相抢先）
  let best = null;
  for(const k in T){
    if(t.indexOf(k) >= 0 && (best===null || k.length > best.length)) best = k;
  }
  if(best) return T[best];
  // 3) 未知类型 → 稳定动态色（不再一律落灰）
  return graphHashColor(t);
}
/* ══ P0-2（2026-11-28）三套画布统一邻域高亮 ══
   同一语义：选中/聚焦节点 ⇒ 1-hop 闭邻域保持清晰、其余压暗、焦点节点加光晕；
   点空白 ⇒ 全部恢复。Cytoscape 用 .dimmed / .focus-hi（三处样式表各自声明，色值同源）；
   手写 SVG 主画布复用既有 gvNodeHover / gvNodeHoverEnd（hover 与选中同一套视觉）。
   target：Cytoscape 实例，或 'svg'（主知识图谱画布）；id 为 null/'' 表示取消聚焦。

   ⚠ 能力判定只用 **Cytoscape core 真实存在**的方法
   2026-11-28 真机实测缺陷：原判定写作 `typeof target.closedNeighborhood === 'function'`，
   但 closedNeighborhood 是 **collection/元素** 的方法，core 上没有 ⇒ isCy 恒 false ⇒
   三个 Cytoscape 画布的焦点高亮全部静默失效（真机 dimmedCount 恒为 0）。
   现在按 core 的组合特征判定（elements + nodes + getElementById 三者同时存在）。
   测试位：verify_graph_p0_render.py C 组（桩必须与真实 API 一致，且不得自带 closedNeighborhood）。 */
function applyGraphFocus(target, id){
  if(!target) return;
  // Cytoscape core 的真实特征：elements()/nodes()/getElementById() 都是 core 方法
  const isCy = typeof target === 'object'
    && typeof target.elements === 'function'
    && typeof target.nodes === 'function'
    && typeof target.getElementById === 'function';
  if(isCy){
    try{
      target.elements().removeClass('dimmed focus-hi');
      if(id == null || id === '') return;
      const el = target.getElementById(String(id));
      if(!el || !el.length) return;
      target.elements().not(el.closedNeighborhood()).addClass('dimmed');
      el.addClass('focus-hi');
    }catch(e){}
    return;
  }
  try{
    if(id == null || id === ''){ if(typeof gvNodeHoverEnd === 'function') gvNodeHoverEnd(); return; }
    if(typeof gvNodeHover === 'function') gvNodeHover(id);
  }catch(e){}
}
/* ══ P0-4（2026-11-28）三画布统一交互提示 ══
   文案只描述**已实现**能力（P1 的框选/双击展开未落地，不得预告）。
   mode：'kg' 主知识图谱 | 'sysml' SysML 视图 | 'impact' 影响拓扑图 */
function gvHintBar(mode){
  const T = {
    kg:     '滚轮缩放 · 空白拖动平移 · 节点拖拽调整布局 · 点节点高亮上下游（点空白取消）',
    sysml:  '滚轮缩放 · 空白拖动平移 · 节点可拖动 · 点节点高亮 1-hop 邻域（点空白取消）',
    impact: '滚轮缩放 · 画布拖拽平移 · 节点可拖动 · 点节点高亮上下游（点空白取消）'
  };
  const m = T[mode] ? mode : 'kg';   // 未知 mode 归一为 kg（data-mode 恒可枚举，便于样式/门禁）
  return `<span class="gv-hint" data-mode="${m}" style="color:${graphTok('muted')};font-size:12px;line-height:1.6;">💡 ${T[m]}</span>`;
}
// ── 图谱视图筛选（对齐行业图数据平台：状孤立/类型过滤 + 布局算法 + 类型图例）──
function graphDegreeMap(nodes, edges){
  const deg = {};
  nodes.forEach(n=>deg[n.id]=0);
  edges.forEach(e=>{ if(deg[e.source_id]!==undefined) deg[e.source_id]++; if(deg[e.target_id]!==undefined) deg[e.target_id]++; });
  return deg;
}
function graphApplyView(){
  const v = graphState.view;
  let nodes = graphState.all.nodes.slice();
  let edges = graphState.all.edges.slice();
  const linked = {};
  edges.forEach(e=>{ linked[e.source_id]=1; linked[e.target_id]=1; });
  if(v.status && v.status!=='all') nodes = nodes.filter(n=>n.status===v.status);
  if(v.hideIsolated) nodes = nodes.filter(n=>linked[n.id]);
  if(v.docs && v.docs.length) {
    const ds = new Set(v.docs);
    nodes = nodes.filter(n=>ds.has(n.source_doc||''));
  }
  if(v.types && v.types.length) {
    const ts = new Set(v.types);
    nodes = nodes.filter(n=>ts.has(n.entity_type));
  }
  const ids = new Set(nodes.map(n=>n.id));
  edges = edges.filter(e=>ids.has(e.source_id)&&ids.has(e.target_id));
  // 边去重（同一 (目标,类型) 多分多版本行只留一条，避免重复连线
  const seen = {};
  edges = edges.filter(e=>{
    const k = e.source_id+'|'+e.target_id+'|'+e.relation_type;
    if(seen[k]) return false; seen[k]=1; return true;
  });
  graphState.nodes = nodes; graphState.edges = edges;
  graphSkeletonApply();   // 骨架模式开启时，把过滤结果替换L1 类型聚合 / L2 实例
}
// ── 骨架模式（万级图谱三级下钻）：L1 类型聚合点击类型节点下钻 L2 实例点击实例下钻 L3 邻域 ──
function graphSkeletonApply(){
  const sk = graphState.sk;
  if(!sk.on) return;
  const base = graphState.nodes, baseEdges = graphState.edges;
  if(!sk.drill){
    // L1 类型骨架：节实体类型（含实例计数），类型间聚合关系（关系类型×次数 聚合
    const tmap = {}; base.forEach(n=>tmap[n.id]=n.entity_type);
    const cnt = {}; base.forEach(n=>{ cnt[n.entity_type]=(cnt[n.entity_type]||0)+1; });
    const agg = {};
    baseEdges.forEach(e=>{
      const st=tmap[e.source_id], tt=tmap[e.target_id];
      if(!st||!tt) return;
      const k = st+'|'+tt+'|'+e.relation_type;
      agg[k]=(agg[k]||0)+1;
    });
    let ei = 0;
    graphState.nodes = Object.keys(cnt).map(t=>({id:'sk:t:'+t, name:t, entity_type:t, x:0, y:0, _skel:1, _count:cnt[t]}));
    graphState.edges = Object.entries(agg).map(([k,n])=>{
      const [st,tt,rel] = k.split('|');
      return {id:'sk:e:'+(ei++), source_id:'sk:t:'+st, target_id:'sk:t:'+tt, relation_type: rel+' ×'+n};
    });
    graphState._skTrunc = 0;
  } else if(!sk.focus || !base.some(n=>n.id===sk.focus)){
    // L2 实例束：下钻类型的实例节点（>100 截断保护 实例间关系；focus 失效时自动回L2
    const all = base.filter(n=>n.entity_type===sk.drill);
    const inst = all.slice(0,100);
    const ids = new Set(inst.map(n=>n.id));
    graphState.nodes = inst;
    graphState.edges = baseEdges.filter(e=>ids.has(e.source_id)&&ids.has(e.target_id));
    graphState._skTrunc = Math.max(0, all.length - inst.length);
  } else {
    // L3 邻域：焦点实+ 1-hop 邻居（从分支全量数据取，跨类型、不受视图过滤限制），边=邻域内部关系
    const srcN = graphState.all.nodes, srcE = graphState.all.edges;
    const focus = srcN.find(n=>n.id===sk.focus) || base.find(n=>n.id===sk.focus);
    if(!focus){ sk.focus = null; return; }
    const nbrs = [];
    const nbIds = new Set([focus.id]);
    const seenE = {};
    srcE.forEach(e=>{
      let other = null;
      if(e.source_id===focus.id) other = e.target_id;
      else if(e.target_id===focus.id) other = e.source_id;
      if(!other) return;
      const k = e.source_id+'|'+e.target_id+'|'+e.relation_type;
      if(seenE[k]) return; seenE[k]=1;
      if(!nbIds.has(other)) nbrs.push(other);
    });
    // 限制邻居数（0）防止超大节点画布爆
    const limited = nbrs.slice(0,60);
    graphState._skTrunc = Math.max(0, nbrs.length - limited.length);
    limited.forEach(id=>nbIds.add(id));
    const idset = nbIds;
    graphState.nodes = srcN.filter(n=>idset.has(n.id)).map(n=>({...n}));
    graphState.edges = srcE.filter(e=>idset.has(e.source_id)&&idset.has(e.target_id));
  }
}
function gvToggleSkeleton(){
  const sk = graphState.sk;
  sk.on = !sk.on; sk.drill = null; sk.focus = null;
  graphState.sel = null;
  applyGraphViewAndRender(); gvFitView();
}
function gvSkBack(){
  const sk = graphState.sk;
  if(sk.focus) sk.focus = null;          // L3 L2
  else if(sk.drill) sk.drill = null;     // L2 L1
  graphState.sel = null;
  applyGraphViewAndRender(); gvFitView();
}
function gvSkHome(){
  graphState.sk.drill = null; graphState.sk.focus = null;
  graphState.sel = null;
  applyGraphViewAndRender(); gvFitView();
}
function gvSkeletonBanner(){
  const sk = graphState.sk;
  if(!sk.on) return '';
  const crumb = sk.focus
    ? `<b>🦴 L3 邻域</b>：<b>${esc((graphState.nodes.find(n=>n.id===sk.focus)||{}).name||sk.focus)}</b> + ${graphState.nodes.length-1} 个邻居${graphState._skTrunc>0?`（已截断 ${graphState._skTrunc}）`:''}`
    : sk.drill
      ? `<b>🦴 L2 实例</b>：<b>${esc(sk.drill)}</b> ${graphState.nodes.length} 个实例${graphState._skTrunc>0?`，已截断 ${graphState._skTrunc}`:''}）点击实例下钻邻域`
      : `<b>🦴 L1 类型骨架</b> ${graphState.nodes.length} 个类型 · 点击类型节点下钻实例`;
  const crumbs = [];
  if(sk.drill) crumbs.push(`<span style="cursor:pointer;text-decoration:underline;" onclick="gvSkHome()">类型骨架</span>`);
  if(sk.focus) crumbs.push(`<span style="cursor:pointer;text-decoration:underline;" onclick="gvSkBack()">${esc(sk.drill)}</span>`);
  return `<div style="padding:5px 12px;font-size:12.5px;color:var(--blue-d);background:var(--blue-l,#E6F1FB);border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center;">
    ${crumbs.length?`<span style="color:var(--mut);">${crumbs.join(' / ')} /</span>`:''}${crumb}
    ${sk.drill?`<button class="btn sm ghost" style="padding:1px 8px;font-size:12.5px;" onclick="gvSkBack()">返回${sk.focus ? '实例' : '类型骨架'}</button>`:''}
    <span style="flex:1"></span>
    <span style="color:var(--mut);">点击 🦴 骨架 按钮退出</span>
  </div>`;
}
function graphTypeStats(){
  const m = {};
  graphState.nodes.forEach(n=>{ m[n.entity_type]=(m[n.entity_type]||0)+1; });
  return Object.entries(m).sort((a,b)=>b[1]-a[1]);
}
function graphToggleType(t){
  const v = graphState.view, i = v.types.indexOf(t);
  if(i>=0) v.types.splice(i,1); else v.types.push(t);
  applyGraphViewAndRender();
}
function _branchKind(b){ if(!b) return 'personal'; if(b==='release'||b.startsWith('release/')) return 'release'; if(b==='dev'||b.startsWith('dev/')) return 'dev'; return 'personal'; }
function branchPill(b){
  const k=_branchKind(b);
  if(k==='release') return '<span class="st ok" title="release 权威基线（已发布）</span>';
  if(k==='dev') return '<span class="st a" title="dev 分支（待发布 release）</span>';
  return '<span class="st w" title="个人分支草稿（审核落图后未发布）">🕒 草稿</span>';
}
// 2026-09-10 状态机收口：graphSetStatus 已删——画布固reviewed（入图即确认），状态不可切
function graphToggleIsolated(cb){ graphState.view.hideIsolated=cb.checked; graphApplyView(); graphLayout(); renderGraph(); }
function graphSetLayout(l){ graphState.view.layout=l; graphLayout(); renderGraph(); gvEnsureMinimap(graphState.nodes, graphState.edges); }
function applyGraphViewAndRender(){ graphApplyView(); graphLayout(); renderGraph(); }

// P1-P3-helper-done
// ── P1/P2/P3 图谱页交互辅助（导出下拉／筛选抽屉／浮动状态条／大图上限条）──
// 2026-09-12：截断信息已并入 gv-stat-chip（唯一统计入口），横幅不再单独输出，消除重复统计
function gvLimitBanner(){ return ''; }
function gvUpdateStatusPill(){
  // 2026-09-12 统计唯一入口 = 工具栏 gv-stat-chip（原底部 gvLimitBanner 冗余节点计数已并入此 chip）
  const chip = document.getElementById('gv-stat-chip');
  if(!chip) return;
  const shown = graphState.nodes ? graphState.nodes.length : 0;
  const total = graphState.total || shown;
  const infN = (graphState._infEdges||[]).length;
  const truncated = total > shown && shown > 0;
  const nodeTxt = truncated ? '<b>'+shown+'</b> / '+total : '<b>'+shown+'</b>';
  chip.innerHTML = nodeTxt + ' 节点 · <b>'+(graphState.edges?graphState.edges.length:0)+'</b> 条关系'
    + (infN ? ' · <span style="color:#8A4B00;" title="橙色虚线边（⇢ 后缀）为推理引擎叠加的推断关系，未入库；点击边可查看推导路径">🧠 推断 <b>'+infN+'</b>（<span style="border-bottom:2px dashed #E8912D;">橙虚线</span>）</span>' : '');
  chip.title = truncated ? ('当前节点 ' + shown + ' / 后端匹配 ' + total + '（截断，切换状态或筛选可查看更多）') : (shown + ' 节点 · ' + (graphState.edges?graphState.edges.length:0) + ' 条关系' + (infN ? ' · 含 ' + infN + ' 条叠加推断边（橙色虚线，点击可看推导路径）' : ''));
  chip.style.display = 'inline-block';
}
function gvRenderFilterBadge(){
  const n = document.getElementById('gv-filter-n');
  if(!n) return;
  const v = graphState.view||{};
  // 2026-09-10 米爸裁剪：状态筛选退役，徽标仅统隐藏孤立节点"一
  let c = 0;
  if(v.hideIsolated) c++;
  n.textContent = c ? '('+c+')' : '';
}
function gvToggleMenu(id, ev){
  if(ev){ ev.preventDefault(); ev.stopPropagation(); }
  const panel = document.getElementById(id);
  if(!panel) return;
  const open = panel.style.display !== 'none';
  document.querySelectorAll('.gv-dd-panel').forEach(x=>x.style.display='none');
  panel.style.display = open ? 'none' : 'block';
}
function gvCloseMenu(id){ const el=document.getElementById(id); if(el) el.style.display='none'; }
function gvClearFilters(){
  // 2026-09-10 状态机收口：画布固reviewed（status 不再参与），清除只重置类孤立过滤
  const v = graphState.view;
  v.hideIsolated = false; v.types = [];
  loadGraph();
  gvRenderFilterBadge();
}
function toggleMenu(id, ev){
  if(ev){ ev.preventDefault(); ev.stopPropagation(); }
  const panel = document.getElementById(id);
  if(!panel) return;
  const open = panel.style.display !== 'none';
  document.querySelectorAll('.panel-dd, .gv-dd-panel').forEach(x=>x.style.display='none');
  panel.style.display = open ? 'none' : 'block';
}
document.addEventListener('click', function(e){
  if(!e.target.closest('.gv-dd') && !e.target.closest('.dd')){
    document.querySelectorAll('.panel-dd, .gv-dd-panel').forEach(x=>x.style.display='none');
  } else if(!e.target.closest('.gv-dd')){
    document.querySelectorAll('.gv-dd-panel').forEach(x=>x.style.display='none');
  } else if(!e.target.closest('.dd')){
    document.querySelectorAll('.panel-dd').forEach(x=>x.style.display='none');
  }
});

// 布局算法：分层（拓扑层级，适合需求金字塔/系统分解/ 力导向（F-R 简碰撞避免/ 环形（按类型分组
function graphCollide(nodes, minD){
  // 碰撞避免：重叠节点沿连线推开（布局后消除节点重叠）
  for(let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++){
    const dx=nodes[i].x-nodes[j].x, dy=nodes[i].y-nodes[j].y;
    const d=Math.sqrt(dx*dx+dy*dy)||0.1;
    if(d<minD){
      const push=(minD-d)/2;
      nodes[i].x+=dx/d*push; nodes[i].y+=dy/d*push;
      nodes[j].x-=dx/d*push; nodes[j].y-=dy/d*push;
    }
  }
}
// 拓扑分层坐标：层自上而下（入0 的根在顶层，沿边逐层下推，环兜底），同层节点横向居中排列—
// 需求金字塔（利益相关方→系统→子系统→单元）与系统分解（卫星→平台/载荷→分系统/设备）的天然结构
function graphLayeredPos(nodes, edges, W, H){
  const inDeg = {}; nodes.forEach(n=>inDeg[n.id]=0);
  edges.forEach(e=>{ if(inDeg[e.target_id]!==undefined) inDeg[e.target_id]++; });
  const layer = {}; const assigned = {};
  let frontier = nodes.filter(n=>!inDeg[n.id]).map(n=>n.id);
  if(!frontier.length) frontier = [nodes[0].id];
  let L = 0;
  const adj = {};
  edges.forEach(e=>{ (adj[e.source_id]=adj[e.source_id]||[]).push(e.target_id); });
  while(frontier.length){
    const next = [];
    frontier.forEach(id=>{
      if(assigned[id]) return; assigned[id]=1; layer[id]=L;
      (adj[id]||[]).forEach(t=>{ if(!assigned[t]) next.push(t); });
    });
    const rest = nodes.filter(n=>!assigned[n.id]);
    if(!next.length && rest.length) next.push(rest[0].id);
    if(!next.length) break;
    frontier = next; L++;
  }
  const byLayer = {};
  nodes.forEach(n=>{ (byLayer[layer[n.id]||0]=byLayer[layer[n.id]||0]||[]).push(n); });
  Object.keys(byLayer).forEach(k=>byLayer[k].sort((a,b)=>a.name.localeCompare(b.name,'zh')));
  const layers = Object.keys(byLayer).map(Number).sort((a,b)=>a-b);
  const maxCol = Math.max(...layers.map(lk=>byLayer[lk].length), 1);
  // 铺满画布：层层间间距按画布尺寸均分（无紧上限，消节点聚中心、四周留；仅保最小间距防极端
  const colW = Math.max(80, W/maxCol);
  const rowH = Math.max(90, H/Math.max(layers.length,1));
  const oy = (H - rowH*layers.length)/2;
  layers.forEach((lk,ri)=>{
    const items = byLayer[lk];
    const ox = (W - colW*items.length)/2;                            // 每层水平居中
    items.forEach((n,ci)=>{ n.x = ox + colW*ci + colW/2; n.y = oy + rowH*ri + rowH/2; });
  });
}
// ── 2026-09-12 展示优化：布局算法切到成熟库（dagre 分层 / cose 力导向），手写算法仅作回退 ──
// 布局结果归一化到画布世界坐标（等比缩放不拉伸，保留现有 pad 语义）
function gvNormalize(nodes, W, H){
  if(!nodes.length) return;
  const xs=nodes.map(n=>n.x), ys=nodes.map(n=>n.y);
  const minX=Math.min(...xs), maxX=Math.max(...xs), minY=Math.min(...ys), maxY=Math.max(...ys);
  const sw=maxX-minX||1, sh=maxY-minY||1, pad=50;
  const sc=Math.min((W-2*pad)/sw,(H-2*pad)/sh);
  nodes.forEach(n=>{ n.x=pad+(n.x-minX)*sc+(W-(maxX-minX)*sc-2*pad)/2; n.y=pad+(n.y-minY)*sc+(H-(maxY-minY)*sc-2*pad)/2; });
}
// 离屏 Cytoscape 布局：headless 实例跑成熟布局（dagre 分层 / cose 力导向）→ 坐标回填 SVG 节点。
// headless + animate:false 在 layout().run() 内同步完成，保持调用方同步路径（graphSetLayout 等）不破；
// 任一环节失败则回退手写算法，不影响上游。输出仍是 n.x/n.y，下游 renderGraph/骨架/连线零改动。
function gvLayoutWith(algo, nodes, edges){
  if(!window.cytoscape || !nodes.length) return false;
  const elNodes = nodes.map(n=>({data:{id:String(n.id)}}));
  const elEdges = edges.map((e,i)=>({data:{id:'gve'+i, source:String(e.source_id), target:String(e.target_id)}}));
  let cy = null;
  try{
    cy = cytoscape({headless:true, style:[{selector:'node', style:{'width':70,'height':56}}],
      elements:{nodes:elNodes, edges:elEdges}});
    const opt = (algo==='dagre')
      ? {name:'dagre', animate:false, nodeSep:80, rankSep:100, rankDir:'TB', edgeSep:60, margin:40}
      : {name:'cose', animate:false, nodeRepulsion:()=>26000, idealEdgeLength:90, gravity:0.85,
         numIter:500, initialTemp:200, coolingFactor:0.9, randomize:true, padding:40};
    cy.layout(opt).run();
    const pmap = {};
    cy.nodes().forEach(n=>{ pmap[n.id()] = {x:n.position('x'), y:n.position('y')}; });
    cy.destroy(); cy = null;
    let ok = false;
    nodes.forEach(n=>{ const p=pmap[String(n.id)]; if(p){ n.x=p.x; n.y=p.y; ok=true; } });
    return ok;
  }catch(e){
    console.warn('[gvLayoutWith] 布局失败回退手写：', algo, e);
    if(cy){ try{ cy.destroy(); }catch(_){} }
    return false;
  }
}
function graphLayout(){
  const nodes = graphState.nodes, edges = graphState.edges;
  if(!nodes.length) return;
  // 画布高度按节点数自适应（每 4 节点80px，层间距充足不重叠）
  graphState.svgH = Math.max(460, Math.ceil(nodes.length/4)*80);
  const W = graphState.svgW, H = graphState.svgH;
  const cx = W/2, cy = H/2;
  const order = (graphState.view.layout==='ring')
    ? nodes.slice().sort((a,b)=>graphColor(a.entity_type).localeCompare(graphColor(b.entity_type)))
    : nodes.slice();
  if(graphState.view.layout==='ring'){
    const R = Math.min(W,H)/2 - 70;
    order.forEach((n,i)=>{ n.x = cx + R*Math.cos(2*Math.PI*i/order.length); n.y = cy + R*Math.sin(2*Math.PI*i/order.length); });
    graphCollide(nodes, 44);
    return;
  }
  if(graphState.view.layout==='layered'){
    // 成熟分层布局（dagre 离屏）→ 归一；失败回退手写 graphLayeredPos
    if(!gvLayoutWith('dagre', nodes, edges)) graphLayeredPos(nodes, edges, W, H);
    gvNormalize(nodes, W, H);
    return;
  }
  // 成熟力导向（cose 离屏）→ 碰撞清除 + 边界夹紧；失败回退手写 graphForcePos
  if(!gvLayoutWith('cose', nodes, edges)){ graphForcePos(nodes, edges, W, H); }
  else { gvNormalize(nodes, W, H); }
  for(let r=0; r<6; r++){
    graphCollide(nodes, 46);
    nodes.forEach(n=>{ n.x=Math.max(30,Math.min(W-30,n.x)); n.y=Math.max(30,Math.min(H-30,n.y)); });
  }
}
// 公共力导向布局：分层坐标初始化 斥力+引力迭代（引力按节点对去重，高密度约束图不会重复拉近）→ 画布归一
function graphForcePos(nodes, edges, W, H){
  if(!nodes.length) return;
  graphLayeredPos(nodes, edges, W, H);
  const idx = {}; nodes.forEach((n,i)=>idx[n.id]=i);
  // 节点对去重：同一对节点多条边（不同关系类型）只算一条引力，避免高密度约束图把节点重复拉
  const pairSet = new Set(), pairs = [];
  edges.forEach(e=>{
    const key = e.source_id < e.target_id ? e.source_id+'|'+e.target_id : e.target_id+'|'+e.source_id;
    if(!pairSet.has(key)){ pairSet.add(key); pairs.push([e.source_id, e.target_id]); }
  });
  const k = Math.sqrt(W*H/Math.max(nodes.length,1));
  for(let it=0; it<60; it++){
    const cool = 1 - it/70;
    for(let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++){
      let dx = nodes[i].x-nodes[j].x, dy = nodes[i].y-nodes[j].y;
      const d = Math.sqrt(dx*dx+dy*dy)||1;
      const f = k*k/d * 0.09 * cool;
      nodes[i].x += dx/d*f; nodes[i].y += dy/d*f;
      nodes[j].x -= dx/d*f; nodes[j].y -= dy/d*f;
    }
    pairs.forEach(p=>{
      const a=idx[p[0]], b=idx[p[1]];
      if(a===undefined||b===undefined) return;
      let dx = nodes[b].x-nodes[a].x, dy = nodes[b].y-nodes[a].y;
      const d = Math.sqrt(dx*dx+dy*dy)||1;
      const f = d*d/k * 0.032 * cool;
      nodes[a].x += dx/d*f; nodes[a].y += dy/d*f;
      nodes[b].x -= dx/d*f; nodes[b].y -= dy/d*f;
    });
    if(it%6===5) graphCollide(nodes, 44*(1-cool*0.4));
  }
  // 画布归一
  const xs = nodes.map(n=>n.x), ys = nodes.map(n=>n.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const sw = maxX-minX||1, sh = maxY-minY||1, pad = 50;
  const scale = Math.min((W-2*pad)/sw, (H-2*pad)/sh);
  nodes.forEach(n=>{ n.x = pad + (n.x-minX)*scale + (W-(maxX-minX)*scale-2*pad)/2; n.y = pad + (n.y-minY)*scale + (H-(maxY-minY)*scale-2*pad)/2; });
}
// ── 2026-09-12 从基线同步：把 dev/release 的新增实体/关系拉到当前 personal 分支（git merge：新增采纳、冲突保留本地）──
async function gvSyncFromBaseline(){
  const cur = getCurrentBranch();
  if(isReleaseBranch(cur)){ toast('🔒 发布分支只读，不可同步'); return; }
  const b = (Array.isArray(window._branches)?window._branches:[]).find(x=>x.name===cur);
  const defaultSrc = (b && b.parent_branch) ? (b.parent_branch === cur ? 'dev' : b.parent_branch) : 'dev';
  if(cur === 'dev'){ toast('dev 已是主开发基线，无需从其它分支同步'); return; }
  if(cur === defaultSrc){ /* personal 不应等于 dev；跳过 */ }
  try{
    if(typeof confirm==='function' && !confirm(`从基线分支「${defaultSrc}」同步到当前分支「${cur}」？\n将自动采纳基线新增的实体与关系，已存在但内容不同的将保留本分支（不覆盖本地修改）。`)) return;
  }catch(e){}
  try{
    const r = await api(`/api/branches/${encodeURIComponent(cur)}/sync`, {method:'POST', body: JSON.stringify({source: defaultSrc, adopt_modified: []})});
    if(r && r.ok){
      const conf=(r.entities_conflicts||[]).length;
      const removed=(r.entities_removed_in_source||[]).length;
      toast(`✅ 已从「${r.synced_from}」同步：新增实体 ${r.entities_added}、关系 ${r.relations_added}${r.entities_adopted?`、覆盖 ${r.entities_adopted}`:''}${conf?`、保留冲突 ${conf} 条（未覆盖本分支）`:''}${removed?`、基线已移除 ${removed} 条未删`:''}`);
      loadGraph();
    } else {
      toast('❌ 同步失败：'+(r && r.error || '未知错误'));
    }
  }catch(e){ toast('❌ 同步失败：'+(e.message||String(e))); }
}
async function loadBranchMaturity(branch){
  const el = document.getElementById('br-maturity-pill'); if(!el) return;
  try{
    const b = branch || getCurrentBranch();
    const [c, r] = await Promise.all([
      api('/api/knowledge/graph?branch=' + encodeURIComponent(b) + '&status=candidate').catch(()=>({total_entities:0})),
      api('/api/knowledge/graph?branch=' + encodeURIComponent(b) + '&status=reviewed').catch(()=>({total_entities:0})),
    ]);
    const cand = c.total_entities||0, rev = r.total_entities||0;
    el.title = '当前分支实体成熟度（候选待审 / 已入图确认后）；候选在「数据整理·审核队列」确认后入图';
    el.innerHTML = `<span title="候选态：抽取确认后待审核，审核通过即入图">候<b style="color:var(--amb);">${cand}</b></span>
      <span style="margin:0 7px;color:var(--line);">|</span>
      <span title="已入图：审核通过或图库内手动创建（创建即确认即入图）">已入<b style="color:var(--grn);">${rev}</b></span>`;
  }catch(e){ el.innerHTML = '成熟度统计加载失败'; }
}
async function loadGraph() {
  // 按所选分支（图谱页分支选择器优先，缺省回退工作分支）加载图谱（后端 LIMIT 200 兜底大图谱）
  const br = getCurrentBranch();   // 2026-09-11 单源收口：分支唯一来源 = localStorage.mbse_branch（gv-branch 已下线，graphState.branch 字段同步删除）
  // 2026-09-10 状态机收口：图谱 = 确认后的数据。画布固定只拉 reviewed（入图即已审核）
  // candidate 在数据整理·审核队列确认后入图，raw_chunk 是抽取管道中间态——两者均不出现在画布
  const g = await api('/api/knowledge/graph?branch=' + encodeURIComponent(br) + '&status=reviewed');
  graphState.total = (g && g.total_entities) != null ? g.total_entities : 0;
  graphState.all.nodes = (g.entities||[]).map((n,i)=>({...n, x:n.graph_x||0, y:n.graph_y||0}));
  graphState.all.edges = (g.relations||[]).map(r=>({...r, id:r.id}));
  graphApplyView(); graphLayout(); renderGraph(); gvFitView(); gvEnsureMinimap(graphState.nodes, graphState.edges);   // 限高画布 + 缩略图总览同步
  gvLoadViews();  // 子图下拉（模板来源文档/已存子图）随图谱加载刷新
  loadBranchMaturity(br);  // P1-②：按分支展示成熟度统计联动
  loadBranchCtxBar();      // GitHub 式分支上下文条：ahead/behind + 最近提交 + 进行 MR + 发起 MR
  renderEntityTree();      // 三栏化：左栏实体树随图谱数据刷新（类型继承树 + 实例挂载）
}
// ── 工作分支切换（闭环：分支选择 建模/导入/文档/实体/图谱/检索均基于当前分支）──
let RELEASE_BRANCH_NAMES = ['release'];   // 已发布（release）分支名缓存：写守卫 + 顶部选择器过滤用
function getCurrentBranch() {
  const saved = localStorage.getItem('mbse_branch');
  return saved || 'personal';   // 默认工作分支=个人分支（不存在时 initGlobalBranch 回退 dev）
}
function isReleaseBranch(name) {
  return RELEASE_BRANCH_NAMES.includes(name) || name === 'release' || (name||'').startsWith('release/');
}
// ── SysML v1/v2 视图预览（视图投SVG 渲染）──
let SVM_TYPES = [];
