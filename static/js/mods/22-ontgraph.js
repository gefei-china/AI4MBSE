/* 本体图谱交互：拖拽 / 连线 / 布局 / 聚焦
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 11982-12736  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
let _ogViewTok = 0, _ogCur = {px: graphView.panX, py: graphView.panY, s: graphView.scale};
function _ogApplyView(){
  _ogCur = {px: graphView.panX, py: graphView.panY, s: graphView.scale};
  document.querySelectorAll('#og-inner').forEach(g=>{
    g.setAttribute('transform', `translate(${_ogCur.px} ${_ogCur.py}) scale(${_ogCur.s})`);
  });
  const z = document.getElementById('gv-zoom-pct');
  if(z) z.textContent = Math.round(_ogCur.s * 100) + '%';
}
// 2026-09-13 主图谱优化同步：缩放/复位走平滑过渡动画，滚轮/拖拽走即时跟手
function applyGraphTransform(immediate) {
  ogEnsureStyle();
  if(immediate){ _ogViewTok++; _ogApplyView(); return; }
  const tok = ++_ogViewTok;
  const from = {px:_ogCur.px, py:_ogCur.py, s:_ogCur.s};
  const to = {px: graphView.panX, py: graphView.panY, s: graphView.scale};
  const dur = 220, t0 = performance.now();
  const step = (now) => {
    if(tok !== _ogViewTok) return;
    let t = (now - t0) / dur; if(t > 1) t = 1;
    const e = 1 - Math.pow(1 - t, 3);
    graphView.panX = from.px + (to.px - from.px) * e;
    graphView.panY = from.py + (to.py - from.py) * e;
    graphView.scale = from.s + (to.s - from.s) * e;
    _ogApplyView();
    if(t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function graphZoomReset() { graphView.scale = 1; graphView.panX = 0; graphView.panY = 0; applyGraphTransform(); }
function graphZoomIn() { graphView.scale = Math.min(3, graphView.scale * 1.2); applyGraphTransform(); }
function graphZoomOut() { graphView.scale = Math.max(0.3, graphView.scale / 1.2); applyGraphTransform(); }
// ── 本体图谱展示/动效同步：hover 邻域高亮 + tooltip + 边方向流动样式 ──
let _ogStyle = null;
function ogEnsureStyle(){
  if(_ogStyle) return;
  _ogStyle = document.createElement('style');
  _ogStyle.textContent =
    '@keyframes ogDashFlow{to{stroke-dashoffset:-28}}' +
    '.og-flow{stroke-dasharray:8 8;stroke-width:2.3;animation:ogDashFlow .5s linear infinite;}' +
    '.og-node{transition:opacity .12s ease;}' +
    '.og-hov-dim{opacity:.25 !important;}' +
    '.og-hov-ring>circle:first-of-type{stroke-width:3.2;stroke:#185FA5;filter:drop-shadow(0 0 4px rgba(24,95,165,.75));}' +
    '#og-svg{cursor:url(\"data:image/svg+xml;utf8,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2224%22 height=%2224%22 viewBox=%220 0 24 24%22%3E%3Cpath d=%22M12 1v6M12 17v6M1 12h6M17 12h6%22 stroke=%22%231F2D3D%22 stroke-width=%222%22/%3E%3Ccircle cx=%2212%22 cy=%2212%22 r=%222.2%22 fill=%22%23185FA5%22/%3E%3C/svg%3E\") 12 12, crosshair;}';
  document.head.appendChild(_ogStyle);
}
function ogNodeHover(name){
  ogEnsureStyle();
  const svg = document.getElementById('og-svg');
  if(!svg) return;
  let g = null; svg.querySelectorAll('.og-node').forEach(x=>{ if(x.getAttribute('data-name')===name) g = x; });
  const nb = new Set([name]);
  (ogRels||[]).forEach(r=>{ if(r.src===name) nb.add(r.tgt); if(r.tgt===name) nb.add(r.src); });
  svg.querySelectorAll('.og-node').forEach(el=>{
    const dn = el.getAttribute('data-name');
    if(nb.has(dn)){ if(dn===name) el.classList.add('og-hov-ring'); } else { el.classList.add('og-hov-dim'); }
  });
  const t = (ontData.types||[]).find(x=>x.name===name); if(!t) return;
  const body = document.getElementById('ont-graph-body'); if(!body) return;
  let tip = document.getElementById('og-node-tip');
  if(!tip){ tip = document.createElement('div'); tip.id='og-node-tip';
    tip.style.cssText='position:absolute;z-index:9;background:rgba(31,45,61,.95);color:#fff;border-radius:6px;padding:6px 9px;font-size:11px;line-height:1.55;pointer-events:none;max-width:200px;box-shadow:0 2px 8px rgba(0,0,0,.22);';
    body.appendChild(tip);
  }
  const bind = (ontData.binding||[]).find(b=>b.entity_type===name);
  tip.innerHTML = `<div style="font-weight:700;color:#FFE9A8;">${esc(name)}</div><div style="opacity:.85;">类型：${esc(t.type_kind||'entity')}${t.icon?('　'+esc(t.icon)):''}</div><div style="opacity:.7;">实例：${bind?esc(bind.total||0):0}</div>`;
  if(g){ const rr = g.getBoundingClientRect(), br = body.getBoundingClientRect();
    const cw = tip.offsetWidth || 160, ch = tip.offsetHeight || 60;
    let left = (rr.left - br.left) + rr.width + 14;   // 默认放节点右上方，保持间距
    let top  = (rr.top - br.top) - 8;
    if(left + cw > br.width - 8) left = (rr.left - br.left) - cw - 12;      // 右侧越界放左侧
    if(top < 6) top = (rr.top - br.top) + rr.height + 10;                    // 顶部越界放节点下方
    tip.style.left = Math.max(6, left) + 'px'; tip.style.top = Math.max(6, top) + 'px'; tip.style.display='block'; }
}
function ogNodeHoverEnd(){
  document.querySelectorAll('#og-svg .og-hov-dim,#og-svg .og-hov-ring').forEach(x=>x.classList.remove('og-hov-dim','og-hov-ring'));
  const t = document.getElementById('og-node-tip'); if(t) t.style.display='none';
}
// ── 本体类型图：节点拖动（仅布局调整，缓存 _x/_y）──
let ogDrag = null; // {name, offX, offY}
// 屏幕坐标 → 画布用户坐标（含 pan/scale/viewBox 变换）
function ogToUser(body, clientX, clientY) {
  const svg = body.querySelector('#og-svg');
  if(!svg) return null;
  const ctm = svg.getScreenCTM();
  if(!ctm) return null;
  return new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
}
// ── FR-KG-9 补 G12：拖线新建关系类型（Ctrl+节点拖到另一节点 → 命名 → 写 domain/range）──
let ogLink = null; // {src, x0, y0, line}
function ogLinkStart(ev, name) {
  const body = document.getElementById('ont-graph-body');
  const p = ogToUser(body, ev.clientX, ev.clientY);
  const inner = body.querySelector('#og-inner');
  if(!p || !inner) return;
  // 移除旧临时线（容错），新建跟随鼠标的虚线（pointer-events:none 避免遮挡节点命中）
  const old = body.querySelector('#og-tmp-link');
  if(old) old.remove();
  const NS = 'http://www.w3.org/2000/svg';
  const line = document.createElementNS(NS, 'line');
  line.id = 'og-tmp-link';
  line.setAttribute('x1', p.x); line.setAttribute('y1', p.y);
  line.setAttribute('x2', p.x); line.setAttribute('y2', p.y);
  line.setAttribute('stroke', '#D85A30'); line.setAttribute('stroke-width', '1.6');
  line.setAttribute('stroke-dasharray', '5 4'); line.setAttribute('pointer-events', 'none');
  inner.appendChild(line);
  ogLink = {src: name, x0: p.x, y0: p.y, line};
  // 释放点可能落在画布容器外：挂 document 级 mouseup 保证线被清理
  const onUp = e => {
    document.removeEventListener('mouseup', onUp);
    ogLinkEnd(e);
  };
  document.addEventListener('mouseup', onUp);
}
function ogLinkMove(body, ev) {
  if(!ogLink) return;
  const p = ogToUser(body, ev.clientX, ev.clientY);
  if(!p) return;
  ogLink.line.setAttribute('x2', p.x);
  ogLink.line.setAttribute('y2', p.y);
}
function ogLinkEnd(ev) {
  if(!ogLink) return;
  const {src, x0, y0, line} = ogLink;
  ogLink = null;
  if(line && line.parentNode) line.remove();
  const body = document.getElementById('ont-graph-body');
  // 未实际拖动的 Ctrl+点击 → 静默取消（不打扰节点点选）
  const p = ogToUser(body, ev.clientX, ev.clientY);
  if(p && Math.hypot(p.x - x0, p.y - y0) < 6) return;
  // 释放点落在另一节点上 → 命名新建关系类型
  const el = document.elementFromPoint(ev.clientX, ev.clientY);
  const node = el && el.closest ? el.closest('.og-node') : null;
  if(!node) return;
  const dst = node.getAttribute('data-name');
  if(!dst || dst === src) return; // 落回自身节点 → 取消
  ontCanvasAddRelation(src, dst);
}
// 双击画布空白新建实体类型（Protégé 对齐体验；命中节点/边/组标签时忽略）
function ontCanvasAddEntityByDblClick() {
  promptDialog({
    title: '🧬 新建实体类型',
    message: '在画布双击空白处新建实体类型节点：\n输入类型名称（如：终端 / 放大器 / 用户段）。',
    placeholder: '输入实体类型名称',
    okText: '创建',
  }).then(name => {
    const nm = (name || '').trim();
    if(!nm) return;
    const body = {name: nm, type_kind: 'entity', properties: {}, constraints: {}, description: '', icon: '', color: '#185FA5', parent_id: null};
    api('/api/knowledge/ontology/types', {method:'POST', body: JSON.stringify(body)}).then(r=>{
      // 成功 → 选中新类型并刷新图谱；失败（同名 400 / 无权限 403）→ 提示
      if(r && r.ok) {
        // P0-1 相似名消歧提示（不阻断）：新建成功后在已有类型中查相似名，提醒人工确认
        try {
          const sim = (ontData.types||[]).filter(t=>{
            if(!t.name || t.name===nm) return false;
            const a=nm.toLowerCase(), b=t.name.toLowerCase();
            if(a.length<2 || b.length<2) return false;
            return b.includes(a) || a.includes(b);
          }).slice(0,3).map(t=>t.name);
          if(sim.length) toast('✅ 已新建：' + nm + '（⚠ 与现有类型相似：' + sim.join(' / ') + '，如为重复请合并）');
          else toast('✅ 已新建实体类型：' + nm);
        } catch(e){ toast('✅ 已新建实体类型：' + nm); }
        ontSelected = {kind:'type', id: r.id, name: nm};
        loadOntology();
      } else {
        toast('❌ 新建实体类型失败：' + (r.error || r.detail || '未知错误'));
      }
    });
  });
}
// 拖线新建关系类型：命名后 POST（constraints.allowed_values.src/tgt = domain/range）
function ontCanvasAddRelation(srcName, tgtName) {
  promptDialog({
    title: '🔗 新建关系类型',
    message: `从「${srcName}」连接到「${tgtName}」：\n输入关系类型名称，将自动写入 domain=${srcName}、range=${tgtName}。`,
    placeholder: '如：包含 / 关联 / 组成',
    okText: '创建',
  }).then(name => {
    const nm = (name || '').trim();
    if(!nm) return;
    const body = {name: nm, type_kind: 'relation', properties: {}, constraints: {allowed_values: {src: srcName, tgt: tgtName}}, description: '', icon: '🔗', color: ontAutoColor(nm), parent_id: null};
    api('/api/knowledge/ontology/types', {method:'POST', body: JSON.stringify(body)}).then(r=>{
      if(r && r.ok) {
        toast('✅ 已新建关系类型：' + nm);
        ontSelected = {kind:'type', id: r.id, name: nm};
        loadOntology();
      } else {
        toast('❌ 新建关系类型失败：' + (r.error || r.detail || '未知错误'));
      }
    });
  });
}
function ogNodeDown(ev, name) {
  ev.stopPropagation();
  if(ev.button !== 0) return;
  // Ctrl+拖线：从该节点拖出临时线到另一节点 → 新建关系类型（不动节点布局）
  if(ev.ctrlKey) { ogLinkStart(ev, name); return; }
  const body = document.getElementById('ont-graph-body');
  const g = body.querySelector(`g[data-name="${name}"]`);
  if(!g) return;
  const p = ogToUser(body, ev.clientX, ev.clientY);
  if(!p) return;
  const t = ontData.types.find(x=>x.name===name);
  const cx = (t && t._x != null) ? t._x : 0;
  const cy = (t && t._y != null) ? t._y : 0;
  ogDrag = {name, offX: p.x - cx, offY: p.y - cy};
}
function ogNodeDragEnd() { ogDrag = null; }
// 图谱交互：缩放（wheel）+ 平移（空白 pan）+ 本体类型节点拖动 + 双击空白新建类型 + Ctrl 拖线建关系
function oiBindInteractions(body) {
  if(body._oiBound) return;
  body._oiBound = true;
  body.addEventListener('wheel', ev=>{
    if(ev.target.closest('.og-node')) return;
    ev.preventDefault();
    const r = body.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const v = graphView;
    const factor = ev.deltaY < 0 ? 1.15 : 1/1.15;
    const newScale = Math.max(0.3, Math.min(3, v.scale * factor));
    const wx = (mx - v.panX) / v.scale;
    const wy = (my - v.panY) / v.scale;
    v.scale = newScale;
    v.panX = mx - wx * newScale;
    v.panY = my - wy * newScale;
    applyGraphTransform(true);
  }, {passive:false});
  body.addEventListener('mousedown', ev=>{
    if(ev.target.closest('.og-node')) return;
    oiPanOn = true;
    oiPanStartX = ev.clientX; oiPanStartY = ev.clientY;
    oiPanOrigX = graphView.panX; oiPanOrigY = graphView.panY;
    body.style.cursor = 'grabbing';
    ev.preventDefault();
  });
  body.addEventListener('mousemove', ev=>{
    if(ogLink) { ogLinkMove(body, ev); return; }
    if(ogDrag) { ogNodeDragMove(body, ev); return; }
    if(oiPanOn) {
      graphView.panX = oiPanOrigX + (ev.clientX - oiPanStartX);
      graphView.panY = oiPanOrigY + (ev.clientY - oiPanStartY);
      applyGraphTransform(true);
    }
  });
  body.addEventListener('mouseup', ev=>{
    if(ogLink) ogLinkEnd(ev);
    if(ogDrag) ogNodeDragEnd();
    if(oiPanOn) { oiPanOn = false; body.style.cursor = 'grab'; }
  });
  // 双击空白新建实体类型（命中节点/边/组标签/关系标签时忽略）
  body.addEventListener('dblclick', ev=>{
    const el = ev.target;
    if(el && el.closest) {
      if(el.closest('.og-node') || el.closest('.og-edge') || el.closest('.og-group-tag')) return;
      const id = el.getAttribute && el.getAttribute('id') || '';
      if(id.indexOf('og-lbl-') === 0) return;
    }
    ontCanvasAddEntityByDblClick();
  });
}
// ── 本体类型图渲染：曲线边 + 关联高亮 + 节点拖动联动 ──
let ogRels = []; // 渲染的边缓存 {rel, src, tgt, color, idx}
// 计算二次贝塞尔曲线边（端点缩进 + 控制点按关系名错开弯曲）
function ogEdgePath(a, b, rel) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const dist = Math.sqrt(dx*dx + dy*dy) || 1;
  const off = 22;
  const x1 = a.x + dx/dist*off, y1 = a.y + dy/dist*off;
  const x2 = b.x - dx/dist*off, y2 = b.y - dy/dist*off;
  let bend = 26;
  if(rel) {
    let h = 0; for(let i=0;i<rel.length;i++) h = (h*31 + rel.charCodeAt(i)) % 997;
    bend = 16 + (h % 3) * 20;
  }
  const nx = -dy/dist, ny = dx/dist;
  const cx = (x1+x2)/2 + nx*bend, cy = (y1+y2)/2 + ny*bend;
  // 曲线 t=0.5 点（标签位置）
  const lx = 0.25*x1 + 0.5*cx + 0.25*x2;
  const ly = 0.25*y1 + 0.5*cy + 0.25*y2;
  return {d:`M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`, lx, ly};
}
// ── 本体图谱布局（复用图谱页优化后的算法：分层/力导向/环形/分组 + 动态画布，坐标写回 _x/_y 供拖动联动）──
let ontLayout = 'force';   // 2026-09-13 默认力导向
function ontSetLayout(l){ ontLayout = l; ontRelayout(); }
// 分组布局（WebVOVL 模块化）：按 subClassOf 顶层组划分，组中心环形分布、组内成员环绕——语义模块一目了然
// 组标签：组名显示在成员环上方，明确每个节点的语义归属（避免"哪个类属于哪个模块"的视觉歧义）
let __ontGroups = [];  // [{name, cx, cy, r, n}]
function ontGroupedPos(nodes, ents, W, H){
  const name2node = {}; nodes.forEach(n=>name2node[n.name]=n);
  const entNames = new Set(ents.map(t=>t.name));
  const rootOf = (t, depth)=>{
    if(depth>10 || !t.parent_id) return t.name;
    const p = ontData.types.find(x=>x.id===t.parent_id);
    if(!p || !entNames.has(p.name)) return t.name;
    return rootOf(p, depth+1);
  };
  const groups = {};
  ents.forEach(t=>{ const r = rootOf(t,0); (groups[r]=groups[r]||[]).push(t.name); });
  const gnames = Object.keys(groups);
  const cx = W/2, cy = H/2;
  const R = Math.min(W,H)/2 - Math.max(160, gnames.length*34);
  __ontGroups = [];
  gnames.forEach((g,i)=>{
    const gc = gnames.length>1
      ? {x: cx + R*Math.cos(2*Math.PI*i/gnames.length), y: cy + R*Math.sin(2*Math.PI*i/gnames.length)}
      : {x: cx, y: cy};
    const members = groups[g];
    const n = members.length;
    const r2 = n<=8 ? 64 : Math.max(90, Math.ceil(n/4)*60);  // 组内半径随成员数扩展
    members.forEach((m,j)=>{
      const a = 2*Math.PI*j/Math.max(n,1) - Math.PI/2;
      name2node[m].x = Math.round(gc.x + r2*Math.cos(a));
      name2node[m].y = Math.round(gc.y + r2*Math.sin(a));
    });
    __ontGroups.push({name: g, cx: gc.x, cy: gc.y, r: r2, n});
  });
}
function ontRelayout(){
  const body = document.getElementById('ont-graph-body');
  if(!body) return;
  const ents = (ontData.types||[]).filter(t=>t.type_kind==='entity');
  if(!ents.length) return;
  const W = Math.max(body.clientWidth||700, 760), H = Math.max(body.clientHeight||500, 520);
  const vw = Math.max(W, Math.ceil(ents.length/4)*180), vh = Math.max(H, Math.ceil(ents.length/4)*120);
  const nodes = ents.map(t=>({id:`OT-${t.name}`, name:t.name, x:0, y:0}));
  const edges = ontRelationRows().filter(r=>r.src!=='?'&&r.tgt!=='?')
    .map(r=>({source_id:`OT-${r.src}`, target_id:`OT-${r.tgt}`}));
  if(ontLayout==='ring'){
    const cx=W/2, cy=H/2, R=Math.min(W,H)/2-70;
    nodes.forEach((n,i)=>{ n.x=cx+R*Math.cos(2*Math.PI*i/nodes.length); n.y=cy+R*Math.sin(2*Math.PI*i/nodes.length); });
    graphCollide(nodes, 50);
  } else if(ontLayout==='force'){
    graphForcePos(nodes, edges, W, H);
  } else if(ontLayout==='group'){
    ontGroupedPos(nodes, ents, W, H);
    graphCollide(nodes, 50);
  } else {
    graphLayeredPos(nodes, edges, W, H);
  }
  ents.forEach((t,i)=>{ t._x = Math.round(nodes[i].x); t._y = Math.round(nodes[i].y); });
  // 初始视图适配：缩放至全图可见（对齐图谱数据页默认看全貌，zoom 交互仍可用）
  graphView.scale = 1; graphView.panX = 0; graphView.panY = 0;   // 2026-09-13 viewBox 已收缩到节点包围盒，默认铺满可视区
  renderOntGraph();
}
// ── 2026-09-02 邻域聚焦：选中实体 → 图谱只显示"匹配数据"（该实体+直接关联实体+关联边），其他隐藏 ──
// 匹配逻辑与原有"选中高亮关联"（relNodeNames）完全一致（用户确认保持现状），
// 仅把"非关联淡化"改为"非匹配隐藏"。
// 2026-09-09 层级维度（对齐 Protégé OntoGraf）：直接父类/直接子类纳入可见范围（不占关联配额），
// subtree=直接子类集合、ancestors=父类链——树标签与图谱「↓子类/↑父类」角标由此驱动。
function ontFocusSet(name){
  const related = new Set(), relNames = new Set(), cnt = {};
  ontRelationRows().forEach(e=>{
    if(e.src==='?' || e.tgt==='?') return;
    if(e.src===name && e.tgt!==name){ related.add(e.tgt); relNames.add(e.rel); cnt[e.tgt]=(cnt[e.tgt]||0)+1; }
    if(e.tgt===name && e.src!==name){ related.add(e.src); relNames.add(e.rel); cnt[e.src]=(cnt[e.src]||0)+1; }
  });
  // 层级：直接父类（Protégé 口径：仅一级，不含祖父以上）+ 直接子类
  const parents = [], children = new Set();
  const byId = {};
  (ontData.types||[]).forEach(t=>{ if(t.type_kind==='entity') byId[t.id]=t; });
  const self=(ontData.types||[]).find(t=>t.type_kind==='entity' && t.name===name);
  if(self && self.parent_id){
    const p = byId[self.parent_id];
    if(p) parents.push(p.name);
  }
  (ontData.types||[]).forEach(t=>{
    if(t.type_kind!=='entity' || !t.parent_id) return;
    const p = byId[t.parent_id];
    if(p && p.name===name) children.add(t.name);
  });
  // 匹配强度降序 + 数量控制（ontFocusLimit：8/16/0=全部），总数记录
  const relatedSorted = [...related].sort((a,b)=>((cnt[b]||0)-(cnt[a]||0)) || a.localeCompare(b,'zh'));
  const relatedAll = relatedSorted.length;
  const lim = ontFocusLimit || relatedAll;
  const relatedShown = new Set(relatedSorted.slice(0, lim));
  const visible = new Set([name, ...relatedShown, ...children, ...parents]);
  return {subtree:children, ancestors:parents, parents:new Set(parents), children,
          assoc:relatedShown, assocAll:relatedAll,
          assocRelNames:relNames, visible};
}
function ontFocusTo(name){  // 点击外部引用锚点 / 直接聚焦某类型
  const t = (ontData.types||[]).find(x=>x.name===name);
  if(!t) return;
  ontFocus = name;
  ontSelected = {kind:'type', id:t.id, name};
  renderOntTree(); renderOntGraph(); renderOntSideDetail(); ontFocusBreadcrumb();
}
function ontFocusExit(){
  ontFocus = null;
  renderOntGraph(); renderOntTree(); ontFocusBreadcrumb();
}
function ontFocusBreadcrumb(){  // 工具栏聚焦状态 + 面包屑（祖先路径）+ 子树/关联计数
  const bar = document.getElementById('ont-focus-bar');
  if(!bar) return;
  if(ontPaneMode==='graf' && ontFocus && ontView==='classes'){   // 三轮调整：聚焦条仅类维度有意义（props/dprops 走 propFset 筛选）
    const fset = ontFocusSet(ontFocus);
    const bc = document.getElementById('ont-focus-bc');
    if(bc){
      bc.innerHTML = `📌 <b>${esc(ontFocus)}</b>`
        + `<span style="margin-left:6px;opacity:.8;">关联 ${fset.assocAll||0} 个${(fset.assocAll||0)>fset.assoc.size?`（显示 ${fset.assoc.size}）`:''}${ontFocusRelMode==='direct'?' · 仅直接关系':''} · 含父类/子类层级</span>`;
      bc.title = `树驱动图谱：显示选中实体 + 属性关联实体（数量可切）+ 直接父类/子类（Protégé OntoGraf 层级范式）；关系范围可在右侧切换`;
    }
    const ab = document.getElementById('ont-focus-assoc');
    if(ab) ab.classList.toggle('on', ontFocusAssoc);
    const lim = document.getElementById('ont-focus-limit');
    if(lim) lim.value = String(ontFocusLimit||0);
    const rm = document.getElementById('ont-focus-rel');
    if(rm) rm.value = ontFocusRelMode||'all';
    bar.style.display = 'inline-flex';
  } else {
    bar.style.display = 'none';
  }
}
function renderOntGraph() {
  ogEnsureStyle();   // 2026-09-13 注入本体图优化样式（hover 邻域/方向流动/drop-shadow）
  const body = document.getElementById('ont-graph-body');
  if(!body) return;
  // 2026-09-02 P0-4：中栏图谱模式焦点维度跟随顶部实体维度 Tab（props→关系边显著/节点淡化；dprops→节点旁显示属性）
  const dimProps = ontPaneMode==='graf' && ontView==='props';
  const dimDprops = ontPaneMode==='graf' && ontView==='dprops';
  const W = body.clientWidth||700, H = body.clientHeight||500;
  // 世界布局空间：按实体类型数动态扩展（对齐图谱数据页：节点多画布大 → 层间距充足不重叠，SVG meet 自动缩放显示）
  const entsAll = (ontData.types||[]).filter(t=>t.type_kind==='entity');
  // 2026-09-02 三轮调整：对象属性/数据属性维度按选中节点筛选（非全量）——
  // props=选中关系的域∪值域节点，仅画该关系的边；dprops=拥有该属性的实体类型，保留其间关系边
  let propFset = null;
  if(ontPaneMode==='graf' && (ontView==='props' || ontView==='dprops')){
    const _sn = ontSelected && ontSelected.kind==='type' ? ontSelected.name : null;
    const _st = _sn ? (ontData.types||[]).find(x=>x.name===_sn) : null;
    if(_st && ontView==='props' && _st.type_kind==='relation'){
      const rows = ontRelationRows().filter(e=>e.rel===_sn && e.src!=='?' && e.tgt!=='?');
      const vis = new Set(); rows.forEach(e=>{ vis.add(e.src); vis.add(e.tgt); });
      if(vis.size) propFset = {visible:vis, relRows:rows};
    } else if(_st && ontView==='dprops' && _st.type_kind==='attribute'){
      const vis = new Set();
      entsAll.forEach(tt=>{
        let pk=[]; try{ pk=Object.keys(tt.properties||{}); }catch(e){ pk=[]; }
        if(pk.includes(_sn)) vis.add(tt.name);
      });
      if(vis.size) propFset = {visible:vis, relRows:null};
    }
  }
  // 2026-09-02 聚焦下钻：只渲染可见集（子树+祖先链+关联类），节点坐标保留 _x/_y 不位移（props/dprops 筛选优先）
  const fset = (ontPaneMode==='graf' && ontFocus && !propFset) ? ontFocusSet(ontFocus) : null;
  const entsList = propFset ? entsAll.filter(t=>propFset.visible.has(t.name))
    : (fset ? entsAll.filter(t=>fset.visible.has(t.name)) : entsAll);
  const vw = Math.max(W, Math.ceil(entsAll.length/4)*180);
  const vh = Math.max(H, Math.ceil(entsAll.length/4)*120);
  // 节点（坐标写回 _x/_y 缓存，供拖动联动与边跟随）
  const ents = entsList.map((t,i)=>{
    const x = (t._x != null) ? t._x : 90 + (i%5)*160;
    const y = (t._y != null) ? t._y : 70 + Math.floor(i/5)*120;
    t._x = x; t._y = y;
    return {id:`OT-${t.name}`, name:t.name, kind:'entity', x, y,
      icon:t.icon||'🛰', color:ontColorOf(t)};
  });
  // 三轮调整：筛选视图（props/dprops）节点少且缓存坐标常挤成一线 → 环形重排（仅本次渲染，不写回 _x/_y 缓存）
  if(propFset && ents.length){
    const cx2=vw/2, cy2=vh/2;
    const R2=Math.max(110, Math.min(vw,vh)/2 - Math.max(90, ents.length*8));
    ents.forEach((n,i)=>{
      const a2=2*Math.PI*i/ents.length - Math.PI/2;
      n.x=cx2+R2*Math.cos(a2); n.y=cy2+R2*Math.sin(a2);
    });
  }
  // 2026-09-07 聚焦视图环形重排：选中类居中、关联类环绕——沿用全量布局缓存坐标会挤成一线（演示观感差）；
  // 同 propFset 只改本次渲染坐标，不写回 _x/_y 缓存（退出聚焦恢复原布局）
  else if(fset && ents.length){
    const cx2=vw/2, cy2=vh/2;
    const R2=Math.max(150, Math.min(vw,vh)/2 - Math.max(110, ents.length*12));
    const selN = ents.find(n=>n.name===ontFocus);
    const others = ents.filter(n=>n.name!==ontFocus);
    if(selN){ selN.x=cx2; selN.y=cy2; }
    others.forEach((n,i)=>{
      const a2=2*Math.PI*i/others.length - Math.PI/2;
      n.x=cx2+R2*Math.cos(a2); n.y=cy2+R2*Math.sin(a2);
    });
  }
  // 边（关系类型约束 → 展开为每条独立关系，与左栏关系行同源同序；聚焦时仅两端可见才画，
  //  ontFocusRelMode=direct 时仅保留选中节点直接参与的边）
  const relsAll = ontRelationRows();
  ogRels = (propFset
      ? (propFset.relRows ? propFset.relRows : relsAll.filter(e=>propFset.visible.has(e.src)&&propFset.visible.has(e.tgt)))
      : (fset ? relsAll.filter(e=>fset.visible.has(e.src)&&fset.visible.has(e.tgt)
          && (ontFocusRelMode!=='direct' || e.src===ontFocus || e.tgt===ontFocus)) : relsAll))
    .map((r, idx)=>({...r, idx}));
  // 选中高亮集：entity 选中→关联节点+关联边；relation 选中→该关系类型所有边；edge 选中→单条边
  const selName = ontSelected && ontSelected.kind==='type' ? ontSelected.name : null;
  const selType = selName ? ontData.types.find(t=>t.name===selName) : null;
  const selKind = selType ? selType.type_kind : null;
  const selEdge = ontSelected && ontSelected.kind==='edge' ? ontSelected.idx : null;
  const relNodeNames = new Set();
  const hlEdgeKeys = new Set();
  if(selEdge != null) {
    const e = ogRels[selEdge];
    if(e) { hlEdgeKeys.add(e.idx); relNodeNames.add(e.src); relNodeNames.add(e.tgt); }
  } else if(selKind === 'entity') {
    ogRels.forEach(e=>{
      if(e.src===selName || e.tgt===selName) {
        hlEdgeKeys.add(e.idx);
        relNodeNames.add(e.src===selName ? e.tgt : e.src);
      }
    });
  } else if(selKind === 'relation') {
    ogRels.forEach(e=>{
      if(e.rel===selName) {
        hlEdgeKeys.add(e.idx);
        relNodeNames.add(e.src); relNodeNames.add(e.tgt);
      }
    });
  }
  const v = graphView;
  // 三轮调整：筛选视图（props/dprops）收缩 viewBox 到可见节点包围盒（ogToUser 基于 getScreenCTM，拖拽/缩放不受影响）
  // 2026-09-07 聚焦视图（fset）同样收缩：环形布局只在局部区域，不收缩则整图缩得只剩一小簇
  let vbX=0, vbY=0, vbW=vw, vbH=vh;
  if(ents.length){
    const xs=ents.map(n=>n.x), ys=ents.map(n=>n.y);
    const pad=120;
    vbX=Math.max(0, Math.min(...xs)-pad); vbY=Math.max(0, Math.min(...ys)-pad);
    vbW=Math.max(W, Math.max(...xs)+pad-vbX); vbH=Math.max(H, Math.max(...ys)+pad-vbY);
  }
  const searchActive = !!(ontSearchHits && ontSearchHits.size>0);
  // 2026-09-07 筛选/聚焦视图：viewBox 已收缩到局部邻域，内层 g 若沿用 ontRelayout 的全量 fit 缩放会二次缩小
  //（表现为星形簇只有指甲盖大）→ 模式/焦点变更时重置为 scale 1、pan 0；同模式内保留用户 wheel/拖拽
  const ogFitKeyNow = propFset ? 'props' : (fset ? ('focus:'+(ontFocus||'')+':'+ontFocusRelMode) : '');
  if((propFset||fset) && ogFitKey!==ogFitKeyNow){ graphView.scale=1; graphView.panX=0; graphView.panY=0; }
  else if(!propFset && !fset && ogFitKey!==''){
    // 从筛选/聚焦退回全量：viewBox 回到全图，需重算 fit 缩放（同 ontRelayout 公式），否则停在 scale 1 只见局部
    graphView.scale = 1; graphView.panX = 0; graphView.panY = 0;   // 2026-09-13 viewBox 已收缩
  }
  ogFitKey = ogFitKeyNow;
  const hasSel = selEdge != null || !!selName;
  let s = `<svg id="og-svg" width="100%" height="100%" viewBox="${vbX} ${vbY} ${vbW} ${vbH}" preserveAspectRatio="xMidYMid meet" style="background:#fff;">
    <defs>
      <marker id="og-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="#B4B2A9"/></marker>
      <marker id="og-arrow-hl" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#D85A30"/></marker>
    </defs>
    <g id="og-inner" transform="translate(${v.panX} ${v.panY}) scale(${v.scale})">`;
  // 分组布局：绘制组标签（组名在成员环上方，明确语义模块归属）
  if(ontLayout==='group'){
    __ontGroups.forEach(g=>{
      const gx = g.cx, gy = g.cy - g.r - 16;
      s += `<g class="og-group-tag" transform="translate(${gx},${gy})">
        <rect x="-${Math.max(42, g.name.length*7+14)}" y="-12" width="${Math.max(84, g.name.length*14+28)}" height="24" rx="12" fill="#185FA5" opacity="0.85"/>
        <text y="4.5" font-size="11" fill="#fff" font-weight="600" text-anchor="middle">${esc(g.name)}</text>
      </g>`;
    });
  }
  ogRels.forEach(e=>{
    const a = ents.find(n=>n.name===e.src), b = ents.find(n=>n.name===e.tgt);
    if(!a||!b) return;
    const p = ogEdgePath(a, b, e.rel);
    const hlBase = hlEdgeKeys.has(e.idx);
    const hl = searchActive ? (ontSearchHits.has(e.src)&&ontSearchHits.has(e.tgt)) : hlBase;
    const eo0 = searchActive ? (hl?1:0.07) : (hlBase?1:(hasSel&&!propFset?0.1:1));   // 筛选视图：边均为相关数据，不淡化
    const eo = dimProps ? Math.max(eo0, 0.8) : eo0;   // 对象属性维度：关系边整体稍显著（不强制全高亮），选中才橙色
    const hlV = hl;   // 仅选中/搜索命中才高亮（2026-08-31：避免全边高亮刺眼）
    // 2026-09-02 邻域聚焦：可见边均为匹配边（选中↔关联 / 关联之间），正常实线显示
    let dashA = '', eoF = eo;
    if(fset){
      eoF = Math.max(eo, hlBase ? 1 : 0.85);   // 匹配边稍显著（选中仍橙色高亮）
    }
    // 边可点击：点哪条高亮哪条（selectOntEdge 单边选中）
    s += `<path id="og-edge-${e.idx}" class="og-edge${hlV?' og-flow':''}" data-rel="${esc(e.rel)}" data-src="${esc(e.src)}" data-tgt="${esc(e.tgt)}" d="${p.d}" fill="none" stroke="${hlV?'#D85A30':e.color}" stroke-width="${hlV?2.4:1.2}" opacity="${eoF}"${dashA} style="cursor:pointer;" onclick="selectOntEdge(${e.idx})" marker-end="url(#${hlV?'og-arrow-hl':'og-arrow'})"/>`;
    s += `<text id="og-lbl-${e.idx}" x="${p.lx}" y="${p.ly-7}" font-size="${dimProps?11:10}" fill="${hlV?'#D85A30':'#5F5E5A'}" font-weight="${hlV?'700':'400'}" text-anchor="middle" opacity="${eoF}" style="cursor:pointer;" onclick="selectOntEdge(${e.idx})">${esc(e.rel)}</text>`;
  });
  ents.forEach(n=>{
    const isSel = selName===n.name;
    const isRel = relNodeNames.has(n.name);
    const inSearch = searchActive && ontSearchHits.has(n.name);
    // 2026-09-02 邻域聚焦：选中节点橙色高亮，直接关联实体正常显示（轻角标区分）；非匹配节点已过滤隐藏
    let fill = propFset ? (isSel ? '#FAC775' : '#E6F1FB') : (isSel ? '#FAC775' : (isRel ? '#FFE9B8' : (inSearch ? '#FAE3C9' : '#E6F1FB')));
    let sw = (isSel||inSearch) ? 3 : (isRel ? 2.5 : 1.5);
    let nstrk = inSearch ? '#D85A30' : n.color;
    let no0 = searchActive ? (inSearch?1:0.14) : (propFset ? 1 : ((hasSel && !isSel && !isRel) ? 0.18 : 1));   // 筛选视图内节点均为相关，不淡化
    // r/instN 提前声明（if(fset) 内 tagTxt 用到 r，TDZ 防错）
    const bindMapG = {}; (ontData.binding||[]).forEach(b=>bindMapG[b.entity_type]=b);
    const instN = (bindMapG[n.name]||{}).total || 0;
    const r = 13 + Math.min(12, Math.sqrt(instN)*1.3);  // 2026-09-13 节点加大；VOWL 语义：节点半径随实例数增长
    let dashN = '', tagTxt = '';
    if(fset){
      if(isSel){ fill = '#FAC775'; sw = 3; no0 = 1; }
      else { fill = '#E6F1FB'; sw = 1.5; no0 = 1; }   // 匹配实体（直接关联）正常显示
      if(fset.parents && fset.parents.has(n.name)) tagTxt = `<text y="${(r+31).toFixed(1)}" font-size="8" fill="#8A6D3B" text-anchor="middle">↑父类</text>`;
      else if(fset.children && fset.children.has(n.name)) tagTxt = `<text y="${(r+31).toFixed(1)}" font-size="8" fill="#2F6F4F" text-anchor="middle">↓子类</text>`;
      else if(fset.assoc.has(n.name)) tagTxt = `<text y="${(r+31).toFixed(1)}" font-size="8" fill="#A08B3C" text-anchor="middle">关联</text>`;
    }
    const no = (dimProps && !propFset) ? Math.min(no0, 0.4) : no0;   // 对象属性维度：节点淡化突出关系边（筛选视图内不淡化）
    // 数据属性维度：节点下方列出该类型属性名（筛选视图内选中属性橙色置前）
    let attrTxt = '';
    if(dimDprops){
      const _tt = ontData.types.find(x=>x.name===n.name);
      let _pk = [];
      try{ _pk = Object.keys((typeof (_tt&&_tt.properties)==='string'?JSON.parse((_tt&&_tt.properties)||'{}'):(_tt&&_tt.properties))||{}); }catch(e){}
      if(_pk.length){
        if(propFset && _pk.includes(selName)){
          const rest=_pk.filter(k=>k!==selName);
          attrTxt = `<text y="${(r+33).toFixed(1)}" font-size="8.5" text-anchor="middle"><tspan fill="#D85A30" font-weight="700">${esc(selName)}</tspan>${rest.length?`<tspan fill="#185FA5"> · ${esc(rest.slice(0,3).join(' · '))}${rest.length>3?' …':''}</tspan>`:''}</text>`;
        } else {
          attrTxt = `<text y="${(r+33).toFixed(1)}" font-size="8.5" fill="#185FA5" text-anchor="middle">${esc(_pk.slice(0,4).join(' · '))}${_pk.length>4?' …':''}</text>`;
        }
      }
    }
    s += `<g class="og-node" data-name="${esc(n.name)}" transform="translate(${n.x},${n.y})" style="cursor:grab;opacity:${no};" onmousedown="ogNodeDown(event,'${esc(n.name)}')" onclick="selectOntType('${esc(n.name)}',true)" ondblclick="ontGotoEdit('${esc(n.name)}')" onmouseenter="ogNodeHover('${esc(n.name)}')" onmouseleave="ogNodeHoverEnd()">
      <circle r="${r.toFixed(1)}" fill="${fill}" stroke="${nstrk}" stroke-width="${sw}"${dashN} style="filter:drop-shadow(0 1px 2px rgba(0,0,0,.16));"/>
      <text y="3.5" font-size="11" text-anchor="middle">${esc(n.icon)}</text>
      <text y="${(r+11).toFixed(1)}" font-size="9.5" fill="#444441" text-anchor="middle" font-weight="500">${esc(n.name.length>7?n.name.slice(0,7)+'…':n.name)}</text>
      ${instN?`<text y="${(r+21).toFixed(1)}" font-size="8.5" fill="#888780" text-anchor="middle">${instN} 实例</text>`:''}
      ${attrTxt}${tagTxt}
    </g>`;
    // 2026-09-02 邻域聚焦：可见节点（选中+关联）指向外部不可见实体的边 → 「↗ 外部类型」端口锚点，点击跳转聚焦
    if(fset){
      const extSet = new Set();
      relsAll.forEach(e=>{
        if(e.src==='?' || e.tgt==='?') return;
        if(e.src===n.name && !fset.visible.has(e.tgt)) extSet.add(e.tgt);
        if(e.tgt===n.name && !fset.visible.has(e.src)) extSet.add(e.src);
      });
      [...extSet].slice(0, 3).forEach((ext, ei)=>{
        s += `<g class="og-ext" transform="translate(${n.x},${n.y + r + 34 + ei*16})" onclick="ontFocusTo('${esc(ext)}')" style="cursor:pointer;opacity:.8;">
          <rect x="-34" y="-9" width="68" height="18" rx="9" fill="#fff" stroke="#B4B2A9" stroke-dasharray="3 2"/>
          <text y="4" font-size="8" fill="#888780" text-anchor="middle">↗ ${esc(ext.length>7?ext.slice(0,7)+'…':ext)}</text></g>`;
      });
    }
  });
  s += '</g></svg>';
  body.innerHTML = s;
  oiBindInteractions(body);
  ontFocusBreadcrumb();   // 2026-09-02 聚焦工具栏状态同步
}
// 节点拖动：更新节点 transform + 缓存坐标 + 联动刷新关联曲线边
function ogNodeDragMove(body, ev) {
  if(!ogDrag) return;
  const p = ogToUser(body, ev.clientX, ev.clientY);
  if(!p) return;
  const nx = p.x - ogDrag.offX;
  const ny = p.y - ogDrag.offY;
  const g = body.querySelector(`g[data-name="${ogDrag.name}"]`);
  if(!g) return;
  g.setAttribute('transform', `translate(${nx},${ny})`);
  const t = ontData.types.find(x=>x.name===ogDrag.name);
  if(t) { t._x = nx; t._y = ny; }
  // 联动：刷新与该节点相连的曲线边
  ogRels.forEach(e=>{
    if(e.src!==ogDrag.name && e.tgt!==ogDrag.name) return;
    const sa = ontData.types.find(x=>x.name===e.src);
    const sb = ontData.types.find(x=>x.name===e.tgt);
    if(!sa||!sb||sa._x==null||sb._x==null) return;
    // 注意：ogEdgePath 读取 x/y，类型对象存 _x/_y
    const p = ogEdgePath({x:sa._x, y:sa._y}, {x:sb._x, y:sb._y}, e.rel);
    const pe = body.querySelector(`#og-edge-${e.idx}`);
    const pl = body.querySelector(`#og-lbl-${e.idx}`);
    if(pe) pe.setAttribute('d', p.d);
    if(pl) { pl.setAttribute('x', p.lx); pl.setAttribute('y', p.ly-7); }
  });
}
// ── S3/S9/E：本体管理 Modal 表单化 + 关系下拉 + 基数 + 重复校验 + 属性动态行（Icon/Color 选择已移除）──
let ontTypeEditId = null;

function populateEntityTypeSelects() {
  // 仅取 type_kind==='entity' 的本体类型（不含实例），Set 去重防重复数据；多选=多域/多值域
  const names = [...new Set((ontData.types||[]).filter(t=>t.type_kind==='entity').map(t=>t.name))];
  const opts = names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  const dom = document.getElementById('ot-domain');
  const rng = document.getElementById('ot-range');
  if(dom) dom.innerHTML = opts;
  if(rng) rng.innerHTML = opts;
}
// P0-1：父类型下拉——entity=subClassOf、attribute=subPropertyOf（P1-10 扩展 kind 感知），排除自身防环
function populateOntParentSelect(editId, editName, kind) {
  kind = kind || 'entity';
  const el = document.getElementById('ot-parent');
  if(!el) return;
  const names = [...new Set((ontData.types||[])
    .filter(t=>t.type_kind===kind && t.name!==editName)
    .map(t=>t.name))];
  el.innerHTML = '<option value="">— 无父类型（顶层）—</option>'
    + names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if(editId) {
    const t = (ontData.types||[]).find(x=>x.id===editId);
    if(t && t.parent_id) {
      const p = (ontData.types||[]).find(x=>x.id===t.parent_id);
      if(p) { Array.from(el.options).forEach(o=>{ if(o.value===p.name) o.selected=true; }); }
    }
  }
  otParentHint();
}
function otParentHint() {
  const hint = document.getElementById('ot-parent-hint');
  if(!hint) return;
  const el = document.getElementById('ot-parent');
  const p = el && el.value;
  if(!p) { hint.textContent = ''; return; }
  const pt = (ontData.types||[]).find(t=>t.name===p);
  if(!pt) { hint.textContent = ''; return; }
  let n = 0; try { n = Object.keys(JSON.parse(pt.properties||'{}')||{}).length; } catch(e){}
  const req = (pt.constraints && (pt.constraints.required||[]).length) || 0;
  hint.textContent = `将继承「${p}」的属性(${n}个)与必填约束(${req}项)；类型上仍可补充/覆盖。`;
}
// 属性动态行（attr 实体/属性 / rel 关系）
let _propRowId = 0;
function addPropRow(target, kv) {
  // target: 'attr' 或 'rel'
  const wrapId = target === 'rel' ? 'ot-rel-props-rows' : 'ot-props-rows';
  const wrap = document.getElementById(wrapId);
  if(!wrap) return;
  _propRowId++;
  const id = _propRowId;
  const k = kv?.k || '';
  const v = kv?.v || '';
  const typ = kv?.type || 'string';
  const req = kv?.required || false;
  const div = document.createElement('div');
  div.className = 'prop-row';
  div.dataset.rowId = id;
  div.dataset.target = target;
  div.style.cssText = 'display:flex;gap:4px;align-items:center;background:#fafafa;border:1px solid var(--line);border-radius:6px;padding:4px 6px;';
  div.innerHTML = `
    <span style="color:var(--mut);font-size:12px;cursor:grab;">⠿</span>
    <input class="pr-k" value="${esc(k)}" placeholder="属性名" style="border:none;background:transparent;width:90px;font-size:12px;font-weight:600;">
    <input class="pr-v" value="${esc(typeof v==='object'?JSON.stringify(v):v)}" placeholder="说明/示例" style="border:none;background:transparent;flex:1;font-size:12px;">
    <select class="pr-t" style="border:none;background:transparent;font-size:11px;color:var(--mut);">
      <option value="string" ${typ==='string'||typ==='xsd:string'?'selected':''}>string</option>
      <option value="int" ${typ==='int'||typ==='xsd:int'?'selected':''}>int</option>
      <option value="decimal" ${typ==='decimal'||typ==='xsd:decimal'?'selected':''}>decimal</option>
      <option value="boolean" ${typ==='boolean'||typ==='bool'||typ==='xsd:boolean'?'selected':''}>boolean</option>
      <option value="date" ${typ==='date'||typ==='xsd:date'?'selected':''}>date</option>
      <option value="dateTime" ${typ==='dateTime'||typ==='xsd:dateTime'?'selected':''}>dateTime</option>
      <option value="enum" ${typ==='enum'?'selected':''}>enum</option>
      <option value="text" ${typ==='text'?'selected':''}>text</option>
    </select>
    <button class="pr-req" title="必填" style="background:${req?'var(--blue)':'transparent'};color:${req?'#fff':'var(--mut)'};border:none;border-radius:4px;padding:1px 5px;font-size:11px;cursor:pointer;">✏️</button>
    <button class="pr-del" style="background:transparent;border:none;color:var(--mut);cursor:pointer;font-size:14px;" onclick="delPropRow(this)">🗑</button>`;
  // 必填按钮切换
  div.querySelector('.pr-req').onclick = function() {
    const cur = this.dataset.on === '1';
    const next = !cur;
    this.dataset.on = next ? '1' : '0';
    this.style.background = next ? 'var(--blue)' : 'transparent';
    this.style.color = next ? '#fff' : 'var(--mut)';
  };
  div.querySelector('.pr-req').dataset.on = req ? '1' : '0';
  const kInput = div.querySelector('.pr-k');
  if(kInput) kInput.addEventListener('input', ()=>{ if(typeof refreshOntConsLinkage === 'function') refreshOntConsLinkage(); });  // P1-10：属性名变更联动
  wrap.appendChild(div);
  if(target === 'attr' && typeof refreshOntConsLinkage === 'function') refreshOntConsLinkage();
}
function delPropRow(btn) {
  btn.closest('.prop-row').remove();
  if(typeof refreshOntConsLinkage === 'function') refreshOntConsLinkage();  // P1-10：联动刷新
}
function otCheckDup() {
  const el = document.getElementById('ot-name-dup');
  if(!el) return;
  const name = document.getElementById('ot-name').value.trim();
  if(!name) { el.textContent = ''; return; }
  const dup = (ontData.types||[]).find(t => t.name === name && t.id !== ontTypeEditId);
  if(dup) el.textContent = '⚠ 已存在同名类型';
  else el.textContent = '';
}
// ══ 2026-09-07 方案A：实体类型「属性」= 只读引用视图（Protégé / OWL·SHACL 对齐）══
// 行业标准事实源：数据属性（Data Property）在「数据属性」维度全局定义，
// 经 rdfs:domain（constraints.domain_classes）绑定到类；类视图为派生引用，不复制定义。
// 三类来源：own=本类绑定 / inherit=父类链继承 / legacy=历史自由属性行（只读展示，保存时原样保留）。
// 行内仅做「局部约束覆盖」（必填/唯一/白名单 → 写入本类 constraints），与 OWL 导出 sh:minCount 等对应。
function _asObj(x){ return (x && typeof x === 'object') ? x : safeParse(x, {}); }
let _entBindAdd = new Set();   // 本次会话新绑定（保存类型时写入 attribute.domain_classes）
let _entBindDel = new Set();   // 本次会话待解绑（保存类型时从 attribute.domain_classes 移除）
function ontEntParentChain(name){
  const chain = []; const seen = new Set(); let cur = name;
  while(cur && !seen.has(cur)){
    seen.add(cur);
    const t = (ontData.types||[]).find(x => x.name === cur && x.type_kind === 'entity');
    if(!t || !t.parent_id) break;
    const p = (ontData.types||[]).find(x => x.id === t.parent_id);
    if(!p) break;
    chain.push(p.name); cur = p.name;
  }
  return chain;
}
function ontAttrDomains(a){
  const c = _asObj(a && a.constraints);
  return Array.isArray(c.domain_classes) ? c.domain_classes : (c.domain_classes ? [c.domain_classes] : []);
}
function ontEntAttrSources(name){
  const chain = name ? ontEntParentChain(name) : [];
  const own = [], inh = [];
  (ontData.types||[]).filter(t => t.type_kind === 'attribute').forEach(a=>{
    let ds = ontAttrDomains(a);
    if(_entBindDel.has(a.name)) ds = ds.filter(d => d !== name);           // 会话内待解绑 → 从视图移除
    if(_entBindAdd.has(a.name) && !ds.includes(name)) ds = ds.concat(name); // 会话内新绑定 → 立即入视图
    if(ds.includes(name)) own.push(a);
    else if(chain.some(cn => ds.includes(cn))) inh.push(a);
  });
  return { own, inh, chain };
}
