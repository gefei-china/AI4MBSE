/* 影响分析 / 沙箱仿真 / 一致性
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 3729-4534  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function panelCandidates(cd) {
  const cands = cd.candidates||[];
  const confs = cd.conflicts||[];
  return `<h3 style="color:var(--blue-d);margin-bottom:10px;">📋 需求分析结果</h3>
    <div class="note" style="margin-bottom:12px;">意图：需求分析（requirement_analysis）｜ 检索来源：${esc(cd.source)}（图谱 ${cd.graph_count||0} / 向量 ${cd.vector_count||0}）｜ 置信度 ${(cd.confidence||0).toFixed(2)}</div>
    <h4 style="margin:12px 0 6px;">候选需求条目（${cands.length}）<span class="badge">FR-HIL-1 逐条确认</span></h4>
    <table class="t"><tr><th>#</th><th>条目内容</th><th>来源</th><th>评分</th></tr>
    ${cands.map((c,i)=>`<tr><td>${i+1}</td><td>${esc(c.text)}</td><td>${esc(c.source)}</td><td>${c.score||0}</td></tr>`).join('')||'<tr><td colspan="4" style="color:var(--mut);">暂无候选条目</td></tr>'}</table>
    <h4 style="margin:14px 0 6px;">冲突检测（${confs.length}）<span class="badge">FR-MG-3</span></h4>
    ${confs.length?`<table class="t"><tr><th>新条目</th><th>既有元素</th></tr>${confs.map(c=>`<tr><td>${esc(c.new)}</td><td>${esc(c.existing)}</td></tr>`).join('')}</table>`:'<div style="color:var(--grn);">✓ 未检测到冲突</div>'}
    <p style="margin-top:14px;color:var(--mut);font-size:12px;">* 条目需人工逐条确认后并入模型（人在回路 BR-2 / FR-HIL-2）。</p>`;
}
/* ═══ CIA 影响拓扑图谱：Cytoscape 图谱效果（FR-CIA-2 可视化影响路径）═══
   节点：变更源=红菱形 / 直接影响=橙圆角矩 / 间接影响=灰椭圆；边框按影响级别加粗。
   边：按影响度着色（红>橙>灰），实线=直接、虚线=间接，标签=关系类型+影响度。
   布局：按深度分列（x=深度列、y=列内均分），图谱层级直观呈现影响深度。 */
function impactGraphElements(cd){
  const nodes = (cd.impact_nodes||[]).map(n=>{
    const cls = n.impact==='source' ? 'imp-src' : (n.impact==='direct' ? 'imp-direct' : 'imp-indirect');
    const lv = n.level==='high' ? 'lv-hi' : (n.level==='mid' ? 'lv-mi' : 'lv-lo');
    return {data:{id:String(n.id), name:n.name||n.id, type:n.type||'', depth:n.depth||0,
                  impact:n.impact||'', score:n.score||0, level:n.level||'low'}, classes: cls+' '+lv};
  });
  // 追溯性关联提示（2026-09-16 语义收紧）：仅经追溯/引用链可达——虚线灰蓝节点 + 点线边，不计入影响
  const thIds = new Set((cd.trace_hint_nodes||[]).map(n=>'th'+String(n.id)));
  (cd.trace_hint_nodes||[]).forEach(n=>{
    nodes.push({data:{id:'th'+String(n.id), name:(n.name||n.id)+'（追溯）', type:n.type||'', depth:n.depth!=null?n.depth:99,
                  impact:'trace_hint', score:0, level:'low'}, classes:'imp-trace'});
  });
  const edges = (cd.impact_edges||[]).map((e,i)=>{
    const lv = (e.score||0)>=0.7?'e-hi':(e.score||0)>=0.4?'e-mi':'e-lo';
    return {data:{id:'ie'+i, source:String(e.from), target:String(e.to), type:e.type||'', score:e.score||0,
                  label:(e.type||'')+((e.score||0)>0?' · '+e.score:''), impact:e.impact||''},
            classes:(e.impact==='direct'?'e-direct ':'e-indirect ')+lv};
  });
  (cd.trace_hint_edges||[]).forEach((e,i)=>{
    edges.push({data:{id:'the'+i, source: thIds.has('th'+String(e.from)) ? 'th'+String(e.from) : String(e.from),
                  target: thIds.has('th'+String(e.to)) ? 'th'+String(e.to) : String(e.to),
                  type:e.type||'', score:0, label:(e.type||'')+' · 追溯', impact:'trace'}, classes:'e-trace'});
  });
  return {nodes, edges};
}
function renderImpactGraph(gid, cd){
  const container = document.getElementById(gid);
  if(!container || !window.cytoscape || !(cd.impact_nodes||[]).length) return null;
  if(_cyInstances[gid]){ try{ _cyInstances[gid].destroy(); }catch(e){} delete _cyInstances[gid]; }
  // v6.4 P0：elements 构造 + cytoscape init + fit 整体推到 idle（1500 节点实测 init 212ms）
  // 用户感觉：画布立刻可见、节点渐进出现；fit 在第二次 idle 兜底
  window.__cyIdleMount(container, ()=>{
    const {nodes, edges} = impactGraphElements(cd);
    // 按深度分列定位（图谱层级 = 影响深度）；列内垂直均分避免重叠
    const byDepth = {};
    nodes.forEach(n=>{ (byDepth[n.data.depth]=byDepth[n.data.depth]||[]).push(n); });
    Object.keys(byDepth).sort((a,b)=>a-b).forEach(d=>{
      const col = byDepth[d];
      const gap = Math.min(88, 500/Math.max(col.length,1));
      col.forEach((n,i)=>{ n.position = {x: (+d)*230 + 60, y: 90 + (i - (col.length-1)/2)*gap}; });
    });
    const cy = cytoscape({
      container,
      elements:{nodes, edges},
      style:[
      {selector:'node', style:{'content':'data(name)','text-wrap':'wrap','text-max-width':110,
        'color':'#1F2D3D','font-size':11,'text-valign':'center','text-halign':'center',
        'width':110,'height':44,'border-width':2}},
      {selector:'.imp-src', style:{'background-color':'#A32D2D','border-color':'#7a1f1f',
        'shape':'diamond','width':92,'height':92,'color':'#fff','font-weight':700,'font-size':12}},
      {selector:'.imp-direct', style:{'background-color':'#F8B24B','border-color':'#D98E10','shape':'round-rectangle'}},
      {selector:'.imp-indirect', style:{'background-color':'#C7CFDA','border-color':'#8A94A2','shape':'ellipse','width':104,'height':40}},
      {selector:'.imp-trace', style:{'background-color':'#EDF1F6','border-color':'#9AA7B5','border-style':'dashed',
        'shape':'ellipse','width':96,'height':38,'color':'#5a6678','font-size':10}},
      {selector:'.lv-hi', style:{'border-color':'#A32D2D','border-width':3}},
      {selector:'.lv-mi', style:{'border-color':'#D98E10','border-width':2.5}},
      {selector:'node:selected', style:{'border-width':4,'border-color':'#185FA5'}},
      {selector:'edge', style:{'curve-style':'bezier','width':1.6,'line-color':'#B9C2CE',
        'target-arrow-shape':'triangle','target-arrow-color':'#B9C2CE',
        'content':'data(label)','font-size':9,'text-rotation':'autorotate',
        'text-background-color':'#fff','text-background-opacity':0.75,'color':'#555'}},
      {selector:'.e-direct', style:{'line-style':'solid'}},
      {selector:'.e-indirect', style:{'line-style':'dashed','width':1.3}},
      {selector:'.e-hi', style:{'line-color':'#A32D2D','target-arrow-color':'#A32D2D'}},
      {selector:'.e-mi', style:{'line-color':'#F5A623','target-arrow-color':'#F5A623'}},
      {selector:'.e-trace', style:{'line-style':'dotted','width':1.2,'line-color':'#9AA7B5',
        'target-arrow-color':'#9AA7B5','label':'data(label)','font-size':8}},
      {selector:'.dimmed', style:{'opacity':0.12}},
      {selector:'.focus-hi', style:{'border-width':4,'border-color':'#185FA5'}}
    ],
    layout:{name:'preset', animate:false},
    wheelSensitivity:0.25, minZoom:0.15, maxZoom:3,
  });
  _cyInstances[gid] = cy;
  // 2026-09-16 直接受影响边"蚂蚁线"流动（对齐图谱工作区 gvDashFlow 效果；Cytoscape line-dash-offset + rAF）
  try{
    const flow = cy.edges('.e-direct');
    if(flow.nonempty()){
      flow.style('line-dash-pattern', [7, 7]);
      let _off = 0, _alive = true;
      cy.on('destroy', ()=>{ _alive = false; });
      (function _flowAnim(){
        if(!_alive) return;
        _off = (_off - 1) % 28;
        try{ flow.style('line-dash-offset', _off); }catch(e){ _alive = false; }
        requestAnimationFrame(_flowAnim);
      })();
    }
  }catch(e){}
  cy.on('tap','node', evt=>{
    const d = evt.target.data();
    const imp = d.impact==='source'?'变更源':(d.impact==='direct'?'直接影响':(d.impact==='trace_hint'?'追溯性关联（仅提示，不计入影响）':'间接影响'));
    const lv = d.level==='high'?'高':(d.level==='mid'?'中':'低');
    toast(`[${imp}] ${d.name}（${d.type}）· 深度 ${d.depth} · ${lv}影响 ${d.score}`);
  });
  // v6.4：fit 在第二次 idle 兜底（cytoscape init 已在 idle 内完成；fit 再延一帧避免 layout 期间干扰）
  window.__cyIdleRun(()=>{ try{ cy.fit(undefined, 30); }catch(e){} });
  return cy;
  }, null);
}
function impactLegendHtml(){
  return `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;">
    <span class="rc-chip b">🗺 影响拓扑图谱</span>
    <span><i style="display:inline-block;width:10px;height:10px;background:#A32D2D;transform:rotate(45deg);margin-right:4px;"></i>变更源</span>
    <span><i style="display:inline-block;width:10px;height:10px;background:#F8B24B;border-radius:3px;margin-right:4px;"></i>直接影响</span>
    <span><i style="display:inline-block;width:10px;height:10px;background:#C7CFDA;border-radius:50%;margin-right:4px;"></i>间接影响</span>
    <span>— 实线=直接 / 虚线=间接，红=高影响 橙=中 灰=低</span>
    <span><i style="display:inline-block;width:10px;height:10px;background:#EDF1F6;border:1px dashed #9AA7B5;border-radius:50%;margin-right:4px;"></i>追溯提示（不计入影响）</span>
    <span style="color:var(--mut);font-size:11px;">可缩放 / 拖拽 / 点节点查看详情</span></div>`;
}
/* ═══ 2026-09-15 报告 2.0 P2：交互仪表板（三视图 tab + 清单联动高亮）═══ */
function impactDashTab(gid, key){
  ['graph','matrix','list'].forEach(k=>{
    const el = document.getElementById(gid+'-tab-'+k);
    if(el) el.style.display = (k===key)?'':'none';
  });
  const bar = document.getElementById(gid+'-tabbar');
  if(bar) Array.from(bar.children).forEach(b=>{
    const on = b.dataset.key===key;
    b.style.background = on?'#185FA5':'';
    b.style.color = on?'#fff':'';
    b.style.borderColor = on?'#185FA5':'var(--line)';
  });
  if(key==='graph'){ const cy=_cyInstances[gid]; if(cy){ try{ cy.resize(); cy.fit(undefined,30); }catch(e){} } }
}
/* 清单行 → 网络图定位：邻域高亮 + 其余压暗（Jama 式关联） */
function impactFocusNode(gid, nodeId, name){
  impactDashTab(gid, 'graph');
  const cy = _cyInstances[gid]; if(!cy) return;
  try{
    cy.elements().removeClass('dimmed focus-hi');
    const el = cy.getElementById(String(nodeId));
    if(el.length){
      const nb = el.closedNeighborhood();
      cy.elements().not(nb).addClass('dimmed');
      el.addClass('focus-hi');
      cy.animate({fit:{eles: nb, padding: 70}}, {duration: 280});
      toast(`🎯 已定位「${name||el.data('name')}」及其上下游链路（点画布空白处取消高亮）`);
    }
  }catch(e){}
}
/* 工作量估算表合计（人工输入的人日列求和） */
function impactEffortSum(gid){
  let sum = 0, filled = 0;
  ['model','requirement','interface','retest','doc','review','release','other'].forEach(k=>{
    const v = parseFloat((document.getElementById(gid+'-ef-'+k)||{}).value);
    if(!isNaN(v) && v>0){ sum += v; filled++; }
  });
  const out = document.getElementById(gid+'-ef-total');
  if(out) out.textContent = sum.toFixed(1) + ' 人日（' + filled + ' 项已估）';
}

/* ═══ 2026-09-16 联动图谱工作区：复用 KG 自研 SVG 全套效果（布局/小地图/骨架/边流动画/类型图例）═══
   不另造渲染器——影响子图作为高亮层叠加在知识图谱上（.dim 非影响节点 + 变更源 ring + 影响边蚂蚁线） */
/* ═══ 2026-09-16 会话内影响卡（设计定稿）：对话流只放摘要，图谱在完整报告里 ═══
   摘要 = 结论一句话 + 决策建议 + 关键数字 + 风险要点 + 动作按钮；
   影响拓扑图谱/三视图/路径明细/证据 → 「📄 完整报告」预览区（openImpactReport，图谱是报告的专门区域） */
function cardImpactSummary(cd){
  window._lastImpactCd = cd;   // 完整报告/模拟预演/图谱工作区/保存产物按钮的数据源
  const src = cd.change_source||{};
  const lv = cd.impact_levels||{};
  const ra = cd.risk_analysis||{};
  const chg = cd.change||{};
  const dec = cd.decisions||{};
  const affected = (cd.impact_nodes||[]).filter(n=>n.impact!=='source');
  const top = affected.slice().sort((a,b)=>-(a.score||0)+(b.score||0))[0];
  const retestN = (cd.retest_plan||{}).total||0;
  // 决策配色
  const decColor = dec.recommendation==='批准' ? 'var(--grn)' : (dec.recommendation==='提交 CCB 评审' ? 'var(--blue-d)' : 'var(--amb)');
  const decBg = dec.recommendation==='批准' ? 'var(--grn-l)' : (dec.recommendation==='提交 CCB 评审' ? 'var(--blue-l)' : 'var(--amb-l)');
  // 结论一句话
  const conclusion = `变更「${esc(src.name||'—')}」（${chg.type_label||'结构敏感性分析'}）共影响 <b>${affected.length}</b> 个元素` +
    `（直接 ${cd.direct_count||0} / 间接 ${cd.indirect_count||0}，高影响 ${lv.high||0}），覆盖率 ${ra.coverage!==undefined?ra.coverage+'%':'—'}` +
    `${retestN?`，需重测验证活动 <b>${retestN}</b> 项`:''}。` +
    (top?`组合风险最高：<b>${esc(top.name)}</b>（P=${top.score}）。`:'') +
    ((cd.trace_hint_nodes||[]).length?`另有 <b>${cd.trace_hint_nodes.length}</b> 个追溯性关联节点（仅提示，不计入影响）需人工确认。`:'');
  // 风险要点（Top 1）
  const risk = (ra.risks||[])[0];
  const riskHtml = risk ? `<div style="margin-top:6px;font-size:12px;"><b>${risk.level==='high'?'🔴':risk.level==='mid'?'🟠':'🔵'} ${esc(risk.title)}</b>` +
    `<div style="color:var(--mut);font-size:11.5px;margin-top:2px;">💡 ${esc(risk.advice||'')}</div></div>` : '';
  return `
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <div class="rc-chip b">🔀 变更影响分析 · 摘要</div>
      ${chg.type_label?`<span class="tag">${esc(chg.type_label)}</span>`:''}
      ${chg.desc?`<span style="font-size:11px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px;" title="${esc(chg.desc)}">变更：${esc(chg.desc)}</span>`:''}
    </div>
    <div style="margin:8px 0;font-size:12.5px;line-height:1.7;">📋 <b>结论：</b>${conclusion}</div>
    ${dec.recommendation?`<div style="border:1px solid ${decColor};background:${decBg};border-radius:8px;padding:7px 10px;margin-bottom:8px;">
      <b style="font-size:12.5px;">⚖ 决策建议：${esc(dec.recommendation)}</b>
      ${(dec.reasons||[]).slice(0,2).map(r=>`<div style="font-size:11.5px;margin-top:2px;">· ${esc(r)}</div>`).join('')}
      ${(dec.conditions||[]).slice(0,2).map(c=>`<div style="font-size:11.5px;color:var(--red);margin-top:2px;">▸ 条件：${esc(c)}</div>`).join('')}
    </div>`:''}
    <div class="rc-mini" style="margin-bottom:4px;">
      <span class="rc-chip r"><span class="n">${cd.direct_count||0}</span> 直接</span>
      <span class="rc-chip a"><span class="n">${cd.indirect_count||0}</span> 间接</span>
      <span class="rc-chip r"><span class="n">${lv.high||0}</span> 高影响</span>
      <span class="rc-chip b"><span class="n">${cd.path_count||'—'}</span> 传播路径</span>
      <span class="rc-chip g"><span class="n">${retestN}</span> 需重测</span>
    </div>
    ${riskHtml}
    <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
      <button class="btn sm" onclick="openImpactReport(window._lastImpactCd)">📄 查看完整报告（含影响图谱）</button>
      <button class="btn sm ghost" onclick="simStartFromImpact()">🛠 模拟预演</button>
    </div>
    <div style="margin-top:6px;font-size:11px;color:var(--mut);">完整报告（7 章，即会话产物）：变更概述 / 模型基线分析 / 变更影响分析（路径·维度·评级）/ 影响分析图谱 / 量化评估（统计·工作量·风险）/ 实施方案建议 / 结论。</div>`;
}

/* ═══ 2026-09-15 影响链路大屏可视化（FR-CIA-2 增强）：缩放 / 拖拽 / 自动布局 / 邻域高亮 ═══ */
let _viewerGid = null;
function openImpactGraphViewer(){
  const cd = window._lastImpactCd;
  if(!cd || !(cd.impact_nodes||[]).length){ toast('暂无影响分析数据，请先触发一次变更影响分析'); return; }
  _viewerGid = 'impact-viewer-' + Date.now();
  openPreviewTab({id:null, kind:'report', kind_label:'图谱', title:'🔍 影响链路可视化 · 大屏',
    preview_type:'html',
    preview_content: `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;">
        <span class="rc-chip b">🔍 影响链路 · 大屏视图</span>
        <button class="btn sm ghost" onclick="impactViewerZoom(1.25)">＋ 放大</button>
        <button class="btn sm ghost" onclick="impactViewerZoom(0.8)">－ 缩小</button>
        <button class="btn sm ghost" onclick="impactViewerFit()">⛶ 适配全图</button>
        <button class="btn sm ghost" onclick="impactViewerRelayout()">🔀 自动布局</button>
        <button class="btn sm ghost" onclick="impactViewerReset()">↺ 复位分层视图</button>
        <span style="flex:1;"></span>
        <span style="font-size:11px;color:var(--mut);">滚轮缩放 · 画布拖拽平移 · 节点可拖动 · 点节点高亮上下游（点空白取消）</span>
      </div>
      ${impactLegendHtml()}
      <div id="${_viewerGid}" style="width:100%;height:calc(100vh - 220px);min-height:520px;border:1px solid var(--line);border-radius:10px;background:#fafbfc;margin-top:6px;"></div>
      <div id="impact-viewer-info" style="margin-top:8px;font-size:12px;color:var(--mut);min-height:20px;">点击节点查看详情并高亮其上下游影响链路</div>`,
    content:'', message_id:null, meta:{}});
  setTimeout(()=>{ renderImpactGraph(_viewerGid, cd); impactViewerEvents(_viewerGid); }, 150);
}
function impactViewerZoom(f){
  const cy = _cyInstances[_viewerGid]; if(!cy) return;
  try{ cy.zoom({level: Math.min(3, Math.max(0.15, cy.zoom()*f)), renderedPosition:{x:cy.width()/2, y:cy.height()/2}}); }catch(e){}
}
function impactViewerFit(){
  const cy = _cyInstances[_viewerGid]; if(!cy) return;
  try{ cy.fit(undefined, 40); }catch(e){}
}
function impactViewerRelayout(){
  const cy = _cyInstances[_viewerGid]; if(!cy) return;
  try{ cy.layout({name:'breadthfirst', directed:true, spacingFactor:1.5, animate:true,
    animationDuration:400}).run(); setTimeout(()=>{ try{ cy.fit(undefined, 40); }catch(e){} }, 500); }catch(e){}
}
function impactViewerReset(){
  const cd = window._lastImpactCd;
  if(_viewerGid && cd){ renderImpactGraph(_viewerGid, cd); impactViewerEvents(_viewerGid); }
}
function impactViewerEvents(gid){
  const cy = _cyInstances[gid]; if(!cy) return;
  cy.on('tap','node', evt=>{
    const node = evt.target, d = node.data();
    node.closedNeighborhood().addClass('focus-hi');
    cy.elements().not(node.closedNeighborhood()).addClass('dimmed');
    const imp = d.impact==='source'?'变更源':(d.impact==='direct'?'直接影响':'间接影响');
    const lv = d.level==='high'?'高':(d.level==='mid'?'中':'低');
    const info = document.getElementById('impact-viewer-info');
    if(info) info.innerHTML = `<b>${esc(d.name)}</b>（${esc(d.type)}）· ${imp} · 深度 ${d.depth} · ${lv}影响 ${d.score} —— 已高亮其上下游影响链路（点击空白处取消）`;
  });
  cy.on('tap', evt=>{
    if(evt.target === cy){
      cy.elements().removeClass('dimmed focus-hi');
      const info = document.getElementById('impact-viewer-info');
      if(info) info.textContent = '点击节点查看详情并高亮其上下游影响链路';
    }
  });
}
/* ═══ 影响分析报告（7 章结构，2026-09-16 定稿）：对话流摘要卡的「📄 查看完整报告」= 产物清单中的同一份 ═══
   报告即会话产物（artifact，幂等创建一次）；图谱 = 报告第 4 章专门区域（平台内 Cytoscape 交互图 + 导出用 Mermaid 源码）。
   章节结构对齐用户模板：①变更概述 ②模型基线分析 ③变更影响分析 ④影响分析图谱 ⑤影响量化评估 ⑥实施方案建议 ⑦结论与建议 */

/* Mermaid 源码（graph TD，红=变更源/高、黄=中、绿=低，边标关系类型；供 VS Code/Typora/GitLab 渲染） */
function impactMermaid(cd){
  const nodes = (cd.impact_nodes||[]).slice(0,30);   // 可读性上限 30 节点
  const keep = new Set(nodes.map(n=>String(n.id)));
  const edges = (cd.impact_edges||[]).filter(e=>keep.has(String(e.from)) && keep.has(String(e.to))).slice(0,50);
  // 追溯性关联提示：点线连接（-.->），不计入影响
  const thNodes = (cd.trace_hint_nodes||[]).slice(0,10);
  const thSet = new Set(thNodes.map(n=>String(n.id)));
  const thEdges = (cd.trace_hint_edges||[]).filter(e=>keep.has(String(e.from)) || thSet.has(String(e.from))).slice(0,20);
  const sid = s => 'N' + String(s).replace(/[^\w]/g,'_');
  const txt = s => String(s||'').replace(/"/g,'').slice(0,24);
  const lines = ['graph TD'];
  nodes.forEach(n=>{
    const cls = n.impact==='source' ? 'src' : (n.level==='high'?'hi':(n.level==='mid'?'mi':'lo'));
    lines.push(`  ${sid(n.id)}["${txt(n.name)}<br/>${txt(n.type)}"]:::${cls}`);
  });
  edges.forEach(e=>lines.push(`  ${sid(e.from)} -->|${txt(e.type)||'rel'}| ${sid(e.to)}`));
  thNodes.forEach(n=>lines.push(`  ${sid('th'+n.id)}["${txt(n.name)}<br/>追溯提示"]:::tr`));
  thEdges.forEach(e=>{
    const s = thSet.has(String(e.from)) ? sid('th'+e.from) : sid(e.from);
    const t = thSet.has(String(e.to)) ? sid('th'+e.to) : sid(e.to);
    lines.push(`  ${s} -.->|${txt(e.type)||'追溯'}| ${t}`);
  });
  lines.push('  classDef src fill:#7A1F1F,color:#fff,stroke:#A32D2D,stroke-width:3px;');
  lines.push('  classDef hi fill:#E24B4A,color:#fff;');
  lines.push('  classDef mi fill:#F5A623,color:#fff;');
  lines.push('  classDef lo fill:#8FBC6E,color:#fff;');
  lines.push('  classDef tr fill:#EDF1F6,color:#555,stroke-dasharray:4 3;');
  if((cd.impact_nodes||[]).length > 30) lines.push(`  %% 注：节点超 30 个，仅展示组合风险 Top 30（传播链过长，建议分层展开分析）`);
  return lines.join('\n');
}

function impactSections(cd){
  const escMd = s=>String(s==null?'':s).replace(/\|/g,'/');
  const src = cd.change_source||{};
  const srcName = src.name||'变更对象';
  const affected = (cd.impact_nodes||[]).filter(n=>n.impact!=='source');
  const direct = affected.filter(n=>n.impact==='direct');
  const indirect = affected.filter(n=>n.impact==='indirect');
  const lv = cd.impact_levels||{};
  const ra = cd.risk_analysis||{};
  const chg = cd.change||{};
  const dec = cd.decisions||{};
  const rp = cd.retest_plan||{items:[]};
  const retestN = rp.total||0;
  const ep = cd.element_profile||{};
  const thN = cd.trace_hint_nodes||[];   // 追溯性关联提示（不计入影响统计，仅提示人工确认）
  const thE = cd.trace_hint_edges||[];
  const today = new Date().toISOString().slice(0,10);
  const dots = n => n==='high'?'🔴 高（必须修改）':(n==='mid'?'🟡 中（可能需要修改）':'🟢 低（仅需确认/验证）');
  const bl = cd.baseline||{};                       // S2/C3：基线元数据（分支/已发布版本/悬空实例扫描）
  const SUBMITTED_BY = cd.submitted_by||'会话发起人';
  // W2：SysML v2 英文术语映射（关系类型 → 标准术语）
  const SYSML = {'CONTAINS':'compose','组合':'compose','包含':'compose','满足':'satisfy','SATISFIES':'satisfy',
    'DEPENDS_ON':'dependency','依赖':'dependency','VERIFIED_BY':'verify','验证':'verify','FLOW_TO':'connect',
    '流':'connect','ALLOCATE':'allocate','分配':'allocate','DERIVE':'derive','派生':'derive','TRACE':'trace',
    '追溯':'trace','REFERENCES':'reference','引用':'reference','USES':'use','连接':'connect','冲突':'conflict'};
  const sysmlOf = t => SYSML[String(t||'').trim()] || String(t||'').toLowerCase() || '—';
  // C2：处置动作（按元素类型语义区分——"被影响"≠都"必须修改"）
  const isReq = t => /需求|requirement/i.test(t||''), isVer = t => /验证|试验|测试|V&V/i.test(t||'');
  const isParam = t => /约束|参数|constraint|parametric/i.test(t||'');
  const isStr = t => /系统|分系统|组件|设备|载荷|模块|接口|端口|part|interface/i.test(t||'');
  const actionFor = n => {
    if(isVer(n.type)) return '安排重测';
    if(isParam(n.type)) return n.level==='high'?'重解约束方程':'复核约束方程';
    if(isReq(n.type)) return n.level==='high'?'修改需求文本/指标':'复核指标口径';
    if(isStr(n.type)) return n.level==='high'?'修改设计/结构':'复核设计影响';
    return n.level==='high'?'必须修改':(n.level==='mid'?'评估是否修改':'确认/验证');
  };
  // 维度分类（SysML 语义分层：需求/结构/行为/参数；无法归类的入"其他/验证"）
  const DIMS = [
    ['需求维度', /需求|requirement/i], ['结构维度', /系统|分系统|组件|设备|载荷|模块|接口|端口|part|interface/i],
    ['行为维度', /功能|活动|动作|状态|activity|action|state/i], ['参数维度', /约束|参数|constraint|parametric/i],
    ['验证维度', /验证|试验|测试|V&V/i],
  ];
  const dimOf = t => (DIMS.find(([,re])=>re.test(t||''))||[null])[0];
  const byDim = {};
  affected.forEach(n=>{ const d = dimOf(n.type)||'其他'; (byDim[d]=byDim[d]||[]).push(n); });
  // 5.1 按类型统计（直接/间接/合计）
  const statMap = {};
  affected.forEach(n=>{
    const s = statMap[n.type] = statMap[n.type] || {d:0,i:0};
    if(n.impact==='direct') s.d++; else s.i++;
  });
  const statRows = Object.entries(statMap).sort((a,b)=>(b[1].d+b[1].i)-(a[1].d+a[1].i))
    .map(([t,s])=>`| ${escMd(t)} | ${s.d} | ${s.i} | ${s.d+s.i} |`).join('\n');
  const efRows = (cd.effort_estimate||[]).map(r=>`| ${r.label} | ${r.count||0} | ${r.effort||''} | ${r.note||''} |`).join('\n');
  const rtRows = (rp.items||[]).map(it=>`| ${escMd(it.name)} | ${escMd(it.type)} | ${escMd(it.reason)}${it.via?'（'+escMd(it.via)+'）':''} | ${it.score!=null?it.score:'-'} |`).join('\n');
  const evLines = (cd.evidence||[]).map(e=>`- **${escMd(e.element)}**（影响度 ${e.score!=null?e.score:''}）：` +
    (e.hits||[]).map(h=>`${h.source_doc}（${h.score}）`).join('、')).join('\n');
  const riskLines = (ra.risks||[]).map(r=>`> [!警告] **${r.title}**：${r.desc||''} → 缓解：${r.advice||''}`).join('\n\n');
  const maxDepth = ra.max_depth||cd.depth||1;
  const crossDom = Object.keys(cd.domain_stats||{}).length;
  const scopeJudge = (cd.indirect_count>0 && maxDepth>=2)
    ? `**跨层级传播**——间接影响 ${cd.indirect_count} 个、传播 ${maxDepth} 层、波及 ${crossDom} 个工程领域，需按第 3/6 章逐层处置`
    : '**局部修改**——未发现多层间接传播，按直接影响清单处置即可';
  const priority = (lv.high||0)>0 || (ra.coverage||0)>=60 ? '高' : (affected.length>0 ? '中' : '低');
  const lvlTxt = n => n==='high'?'🔴 高':(n==='mid'?'🟡 中':'🟢 低');
  const matrixRows = (cd.risk_matrix||[]).slice(0,15).map(m=>{
    const n = (cd.impact_nodes||[]).find(x=>String(x.id)===String(m.to))||{};
    return `| ${escMd(n.name||m.to)} | ${escMd(n.type||'')} | ${m.combined!=null?m.combined:'-'} | ${lvlTxt(n.level)||''} | ${actionFor(n)} | ${m.path_count||1} |`;
  }).join('\n');
  const directRows = direct.map(n=>`| ${escMd(n.name)} | ${escMd(n.type)} | ${n.score!=null?n.score:'-'} | ${lvlTxt(n.level)} |`).join('\n');
  const edgeRows = (cd.impact_edges||[]).filter(e=>e.impact==='direct').slice(0,15).map(e=>{
    const nm = id => { const n=(cd.impact_nodes||[]).find(x=>String(x.id)===String(id)); return escMd((n&&n.name)||id); };
    return `| ${nm(e.from)} | ${escMd(e.type)} | ${sysmlOf(e.type)} | ${nm(e.to)} |`;
  }).join('\n');
  // S1 路径合理性判读标注：存在关系 ≠ 影响必然传播（追溯链仅提示，需人工确认）
  const PLAUS_LBL = {reasonable:'✅ 合理传播', reasonable_partial:'⚠️ 含追溯段', trace:'ℹ️ 追溯性关联'};
  const pl = (cd.path_list||[]);
  const plCount = {reasonable:0, reasonable_partial:0, trace:0};
  pl.forEach(p=>{ if(plCount[p.plausibility]!==undefined) plCount[p.plausibility]++; });
  const pathLines = pl.slice(0,10).map((p,i)=>`${i+1}. [${PLAUS_LBL[p.plausibility]||'✅ 合理传播'}] ${p.node_names.map(escMd).join(' → ')}（P=${p.likelihood}，${p.node_ids.length-1} 跳）${p.plausibility==='trace'?'——需人工确认是否实质受影响':''}`).join('\n');
  const plSummary = `路径合理性构成：✅ 合理传播 ${plCount.reasonable||0} 条 ｜ ⚠️ 含追溯段 ${plCount.reasonable_partial||0} 条 ｜ ℹ️ 纯追溯 ${plCount.trace||0} 条（追溯性关联不构成必然影响）。`;
  const dimSec = Object.entries(byDim).map(([d,list])=>{
    const names = list.sort((a,b)=>-(a.score||0)+(b.score||0)).slice(0,8).map(n=>`${escMd(n.name)}（${lvlTxt(n.level)}）`).join('、');
    const more = list.length>8?` 等 ${list.length} 个`:'';
    return `- **${d}**：${names}${more}${d==='需求维度'?'——需复核满足/派生链是否受影响':''}${d==='结构维度'?'——需复核组合层次与接口分配':''}${d==='参数维度'?'——需复核约束方程是否仍成立':''}`;
  }).join('\n');
  const stepLines = [];
  stepLines.push('1. 修改变更源「'+escMd(srcName)+'」，同步更新元素属性/参数');
  direct.filter(n=>n.level==='high').slice(0,6).forEach(n=>stepLines.push(`2. 处置直接影响元素「${escMd(n.name)}」（${lvlTxt(n.level)}，P=${n.score}）`));
  if(rtRows) stepLines.push(`${stepLines.length+1}. 执行重测验证活动（见 5.2 清单：${(rp.items||[]).slice(0,3).map(it=>escMd(it.name)).join('、')}${(rp.items||[]).length>3?' 等':''}）`);
  stepLines.push(`${stepLines.length+1}. 同步需求/文档文本，按第 6.2 清单完成一致性验证`);
  stepLines.push(`${stepLines.length+1}. 提交评审（${dec.recommendation||'CCB'}）后发布`);
  const sections = [
    {heading:'第1章 变更概述', body:
      `**1.1 变更标识**\n\n| 项目 | 内容 |\n| --- | --- |\n| 变更编号 | CIA-${today.replace(/-/g,'')}-${(cd.impact_nodes||[]).length?srcName:'000'} |\n| 提出人 | ${escMd(SUBMITTED_BY)} |\n| 提出日期 | ${today} |\n| 变更类型 | ${escMd(chg.type_label||'属性/构成变更')} |\n| 建议优先级 | ${priority}（依据：高影响 ${lv.high||0} 项、覆盖率 ${ra.coverage!=null?ra.coverage+'%':'—'}） |\n| 分析基线 | ${escMd(bl.branch||'release')}${bl.version_label?' · 本体版本 '+escMd(bl.version_label):''}（快照 ${escMd(bl.checked_at||today)}） |\n\n` +
      `**1.2 变更内容**\n\n| 元素名称 | 元素类型 | 变更属性 | 当前值 | 目标值 | 所属包 |\n| --- | --- | --- | --- | --- | --- |\n| ${escMd(srcName)} | ${escMd(src.entity_type||'')} | ${escMd(chg.attribute||'待验证')} | ${escMd(chg.old_value||'待验证（需补充基线取值）')} | ${escMd(chg.new_value||'待验证（需明确变更幅度）')} | 待验证（模型未建包层级） |\n\n> 按"不臆造信息"约束：属性/当前值/目标值/所属包缺少模型数据时标待验证——补充方式：在变更描述中写明"把 X 的 Y 从 A 改为 B"。\n\n` +
      `**1.3 变更理由**\n\n${escMd(chg.reason||chg.desc)||'待补充（建议记录技术必要性与业务驱动因素）'}\n\n` +
      `**1.4 变更范围初步判断**\n\n${scopeJudge}。`},
    {heading:'第2章 模型基线分析', body:
      `**2.1 直接相关模型元素清单**（沿 release 基线依赖网络，单关系可达）\n\n| 元素 | 类型 | 组合风险 | 影响程度 |\n| --- | --- | --- | --- |\n${directRows||'无'}\n\n` +
      `**2.2 元素关系描述**（变更源的出边，SysML 语义）\n\n| 源元素 | 关系 | SysML 术语 | 目标元素 |\n| --- | --- | --- | --- |\n${edgeRows||'无'}\n\n` +
      `**2.3 基线一致性检查**\n\n分析基线：${escMd(bl.branch||'release')}${bl.version_label?'（本体版本 '+escMd(bl.version_label)+'）':''}。` +
      `一致性扫描（${escMd(bl.checked_at||today)}）：悬空实例 **${bl.dangling_instances!=null?bl.dangling_instances:'待验证'}** 个` +
      `${bl.dangling_instances===0?'（✓ 元素类型均在本体注册，无悬挂引用）':'——存在类型未注册的元素，建议先执行本体一致性检查与清理'}。`},
    {heading:'第3章 变更影响分析', body:
      `**3.1 影响传播路径分析**（Top ${Math.min(10,pl.length)}/${cd.path_count||0} 条，按概率降序）\n\n${plSummary}\n\n${pathLines||'无传播路径（该变更类型下传播通道权重为 0）'}\n\n` +
      `**3.2 按维度分类的影响分析**\n\n${dimSec||'- 未识别受影响元素'}\n${!/行为维度/.test(dimSec)?'- **行为维度**：模型中无行为元素（Activity/Action/State），行为影响未覆盖——建议补充行为建模后重新分析':''}\n${!/参数维度/.test(dimSec)?'- **参数维度**：模型中无约束/参数元素，参数影响未覆盖':''}\n` +
      `**3.3 影响程度评级**（🔴 高=必须修改否则模型不一致；🟡 中=可能需要修改，取决于实现方式；🟢 低=仅需确认或验证）\n\n| 元素 | 类型 | 组合风险 | 影响程度 | 处置动作 | 传播路径数 |\n| --- | --- | --- | --- | --- | --- |\n${matrixRows||'无'}\n\n> P=组合风险（0~1，CPM 合并：1-∏(1-单路径概率)）；阈值 ≥0.7 高 / ≥0.4 中 / <0.4 低。处置动作按元素类型语义区分——需求/验证类"受影响"不等于"必须修改文本"。\n\n` +
      `**3.4 间接影响与涟漪效应**\n\n- 间接影响 ${cd.indirect_count||0} 个元素，最深 ${maxDepth} 层（传播深度控制在 5 层内${maxDepth>=5?'，已达上限，建议分层展开分析':''}）\n- 跨领域波及 ${crossDom} 个工程领域：${Object.entries(cd.domain_stats||{}).map(([d,o])=>`${escMd(d)}(${o.count})`).join('、')}\n- 变更放大器（Multiplier，沿出边传播强）：${Object.values(ep).filter(p=>p.profile==='multiplier').slice(0,5).map(p=>escMd(p.name)).join('、')||'无'}\n- **追溯性关联提示**：${thN.length?`${thN.length} 个节点仅经追溯/引用类关系可达（${thN.slice(0,6).map(n=>escMd(n.name)).join('、')}${thN.length>6?' 等':''}），**不计入影响统计**，需人工确认是否实质受影响`:'无（所有可达节点均经结构/需求/流/依赖链传导）'}\n\n` +
      `**3.5 补充证据（向量库检索，混合溯源）**\n\n${evLines||'暂无向量库补充证据（图谱依赖网络为主证据源；知识库覆盖不足时自动检索相关文档佐证）'}`},
    {heading:'第4章 影响分析图谱', body:
      `变更传播有向图（🔴红=变更源/高影响、🟡黄=中影响、🟢绿=低影响、灰点线=追溯提示不计入影响，边标注关系类型，深度 ${maxDepth} 层（≤5））。` +
      `图谱以 Mermaid 视图随报告分发——在 VS Code / Typora / GitLab 等支持 Mermaid 的工具中可直接渲染；平台内可切换源码 / 一键复制。交互式影响研判可使用会话摘要卡中的「🛠 模拟预演」沙箱（双图谱对比）：\n\n\`\`\`mermaid\n${impactMermaid(cd)}\n\`\`\``},
    {heading:'第5章 影响量化评估', body:
      `**5.1 受影响元素统计**\n\n| 元素类型 | 直接受影响 | 间接受影响 | 合计 |\n| --- | --- | --- | --- |\n${statRows||'无'}\n\n` +
      `**5.2 工作量估算**（涉及数量由影响分布预填；人日需人工评估后填写，不做自动估算）\n\n| 任务项 | 涉及数量 | 工作量(人日) | 估算依据/备注 |\n| --- | --- | --- | --- |\n${efRows}\n\n` +
      `**5.3 风险评估与缓解**\n\n${riskLines||'> [!注意] 未识别显著风险项'}`},
    {heading:'第6章 变更实施方案建议', body:
      `**6.1 实施步骤**（按优先级排序）\n\n${stepLines.join('\n')}\n\n` +
      `**6.2 模型一致性验证清单**\n\n- [ ] 依赖关系是否完整（无悬挂引用）\n- [ ] 需求满足/验证链是否闭环（SATISFIES / VERIFIED_BY）\n- [ ] 接口定义与连接是否一致\n- [ ] 参数约束方程是否仍然成立\n- [ ] 改名/文本引用是否全部同步（如涉及）\n\n` +
      `**6.3 建议的验证方法**\n\n- 模型仿真验证：对受影响的功能/参数元素执行仿真确认\n- 需求追溯检查：沿满足/验证关系核查需求闭环${rtRows?'\n- 重测执行：'+(rp.items||[]).map(it=>escMd(it.name)).join('、'):''}\n- 接口兼容性测试：涉及接口/端口变更时执行`},
    {heading:'第7章 结论与建议', body:
      `**核心结论**：变更「${escMd(srcName)}」影响 ${affected.length} 个元素（直接 ${cd.direct_count||0}/间接 ${cd.indirect_count||0}），覆盖率 ${ra.coverage!=null?ra.coverage+'%':'—'}，${retestN?`需重测 ${retestN} 项验证活动，`:''}传播 ${maxDepth} 层。\n\n` +
      `**明确意见**：⚖ ${dec.recommendation||'提交 CCB 评审'}${(dec.conditions||[]).length?'（条件：'+dec.conditions.map(escMd).join('；')+'）':''}\n\n` +
      `正式变更须经人工确认后执行（BR-2 人在回路）。`},
  ];
  const md = `# 变更影响分析报告 · ${srcName}\n\n` + sections.map(s=>`## ${s.heading}\n\n${s.body}`).join('\n\n');
  return {title: `变更影响分析报告 · ${srcName}`, sections, md,
          summary: `影响 ${affected.length} 个元素（直接 ${cd.direct_count||0}/间接 ${cd.indirect_count||0}），覆盖率 ${ra.coverage!=null?ra.coverage+'%':'—'}，决策建议：${dec.recommendation||'提交 CCB 评审'}`,
          graph: {impact_nodes: cd.impact_nodes, impact_edges: cd.impact_edges, change_source: src,
                  impact_levels: lv, depth: cd.depth, direction: cd.direction, risk_analysis: ra}};
}

/* 打开完整报告 = 会话产物（唯一一份；C1 版本化：分析内容变化 → 旧报告作废重建，杜绝陈旧数据） */
async function openImpactReport(cd){
  if(!cd || !(cd.impact_nodes||[]).length){ toast('暂无影响分析数据，请先触发一次变更影响分析'); return; }
  const built = impactSections(cd);
  // 内容摘要：变更类型/描述 + 关键计数变化 → digest 不同即视为新分析
  let digest = '';
  try{
    digest = JSON.stringify({t:(cd.change||{}).type, d:(cd.change||{}).desc, dc:cd.direct_count,
      ic:cd.indirect_count, pc:cd.path_count, rt:(cd.retest_plan||{}).total, bl:(cd.baseline||{}).checked_at});
  }catch(e){}
  const convId = (typeof currentConvId !== 'undefined' && currentConvId) || (typeof _artConv !== 'undefined' && _artConv) || 0;
  const openObj = (a, meta) => openPreviewTab({id:a.id||null, kind:'report', kind_label:'报告', title:a.title||built.title,
    preview_type:'markdown', preview_content:a.preview_content||built.md, content:'',
    meta: meta || {report_type:'impact', sections: built.sections, summary: built.summary}, message_id:null});
  const createNew = async () => {
    const meta = {report_type:'impact', sections: built.sections, summary: built.summary, digest};
    const res = await api('/api/artifacts', {method:'POST', body: JSON.stringify({
      conversation_id: convId, kind:'report', title: built.title,
      preview_type:'markdown', preview_content: built.md, meta})});
    if(res && res.ok){
      openObj({id:res.id, title:built.title, preview_content:built.md}, meta);
      try{ loadArtifacts(convId); }catch(e){}
      toast('📄 报告已生成并加入会话产物（对话流与文件列表为同一份）');
      return true;
    }
    toast('报告入库失败，已打开临时预览：' + ((res&&res.error)||''));
    return false;
  };
  if(convId){
    try{
      const list = await api('/api/artifacts?conversation_id=' + convId + '&kind=report');
      const found = (Array.isArray(list)?list:[]).find(a=>a.title===built.title);
      if(found){
        let fmeta = found.meta||{};
        if(typeof fmeta === 'string'){ try{ fmeta = JSON.parse(fmeta)||{}; }catch(e){ fmeta={}; } }
        if(fmeta.digest === digest){
          openObj(found, Object.assign({}, fmeta, {digest}));
          return;
        }
        // C1：分析内容已变化 → 旧报告作废重建（保持"对话流=产物清单"单一报告）
        try{ await api('/api/artifacts/' + found.id, {method:'DELETE'}); }catch(e){}
      }
      if(await createNew()) return;
    }catch(e){ /* 网络异常 → 临时预览兜底 */ }
  } else {
    openObj({id:null, title:built.title}, null);
    return;
  }
  openObj({id:null, title:built.title}, null);
}

/* ═══ CIA 沙箱变更模拟（FR-CIA-4）：场景选择 → 变更操作编辑 → 预演 → 前后对比图谱+报告 ═══ */
let _simScenes = [];          // 内置演示场景缓存（含预置变更清单）
let _simState = {scene: '', changes: []};
function openSimPanel(){
  openPreviewTab({id:null, kind:'report', kind_label:'报告', title:'🛠 变更模拟（沙箱）',
    preview_type:'html',
    preview_content:'<div id="sim-panel" style="font-size:12.5px;line-height:1.7;">加载演示场景…</div>',
    content:'', message_id:null, meta:{}});
  fetch('/api/impact/scenes').then(r=>r.json()).then(scenes=>{
    _simScenes = scenes || [];
    _simState.scene = (_simScenes[0]||{}).id || '';
    _simState.changes = [];
    simRenderPanel();
  }).catch(()=>simRenderPanel());
}
function simRenderPanel(){
  const panel = document.getElementById('sim-panel');
  if(!panel) return;
  const opts = _simScenes.map(s=>`<option value="${s.id}" ${s.id===_simState.scene?'selected':''}>${esc(s.title)}（${s.node_count}节点/${s.edge_count}边 · 深${s.depth}层）</option>`).join('');
  panel.innerHTML = `
    <div class="rc-chip b">🛠 变更模拟（沙箱 · 不影响正式模型）</div>
    <div class="note" style="margin:8px 0;">模拟原理：沙箱内复制基线子图 → 应用变更（更新/删除/新增）→ 同引擎重算影响 → 与基线 diff 输出前后对比。全程不写正式图谱。</div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
      <label style="font-size:12px;color:var(--mut);">多层级演示场景</label>
      <select id="sim-scene" style="flex:1;min-width:220px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;" onchange="simSceneChange(this.value)">${opts||'<option>无场景</option>'}</select>
      <button class="btn sm grn" onclick="simRunPre()">▶ 预演变更</button>
      <button class="btn sm ghost" onclick="simExport()">📄 导出对比报告</button>
    </div>
    <div id="sim-changes"></div>
    <div id="sim-result"></div>`;
  simRenderChanges();
}
function simSceneChange(v){
  _simState.scene = v; _simState.changes = [];
  simRenderChanges();
}
function simRenderChanges(){
  const box = document.getElementById('sim-changes');
  if(!box) return;
  const sc = _simScenes.find(s=>s.id===_simState.scene);
  if(sc && (!_simState.changes || !_simState.changes.length)){
    _simState.changes = (sc.changes||[]).map(c=>({...c}));
  }
  box.innerHTML = `<div style="font-size:12px;font-weight:600;margin-bottom:4px;">预演变更操作清单（可增删修改）</div>
    <div id="sim-change-rows"></div>
    <button class="btn sm ghost" style="margin-top:6px;" onclick="simAddChangeRow()">＋ 添加变更操作</button>
    ${sc&&sc.change_desc?`<div style="margin-top:6px;font-size:11.5px;color:var(--mut);">场景说明：${esc(sc.change_desc)}</div>`:''}`;
  simRenderChangeRows();
}
function simRenderChangeRows(){
  const rows = document.getElementById('sim-change-rows');
  if(!rows) return;
  rows.innerHTML = (_simState.changes||[]).map((ch,i)=>`
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:4px;">
      <select style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;" onchange="window.__simState.changes[${i}].op=this.value;simChangeLabel(${i})">
        <option value="modify" ${ch.op==='modify'?'selected':''}>修改</option>
        <option value="delete" ${ch.op==='delete'?'selected':''}>删除</option>
        <option value="add" ${ch.op==='add'?'selected':''}>新增</option>
      </select>
      <input style="flex:1;border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;" value="${esc(ch.op==='add'?(ch.node&&ch.node.name)||ch.target:ch.target||'')}" oninput="window.__simState.changes[${i}].target=this.value" placeholder="${ch.op==='add'?'新增元素名':'目标元素'}"/>
      <input style="flex:1;border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;" value="${esc(ch.new_value||'')}" oninput="window.__simState.changes[${i}].new_value=this.value" placeholder="新值/说明"/>
      <button class="btn sm red" onclick="simDelChangeRow(${i})">✕</button>
    </div>`).join('') || '<div style="color:var(--mut);font-size:11px;">暂无变更操作</div>';
}
function simChangeLabel(i){ /* 行内选择变化后 placeholder 自适应（add 行目标为新元素名） */ simRenderChangeRows(); }
function simAddChangeRow(){
  _simState.changes.push({op:'modify', target:'', new_value:''});
  simRenderChangeRows();
}
function simDelChangeRow(i){
  _simState.changes.splice(i,1); simRenderChangeRows();
}
function simCollectChanges(){
  const changes = [];
  (_simState.changes||[]).forEach(ch=>{
    const t = (ch.target||'').trim();
    if(ch.op === 'add'){
      // 新增操作：保留预置 node/edge（自定义新增需在场景预置中定义），否则跳过
      if(ch.node && ch.edge){ changes.push(ch); }
      else if(t){ changes.push({op:'add', target:t, new_value:ch.new_value||''}); }
    } else {
      if(!t) return;
      changes.push({op:ch.op, target:t, new_value:ch.new_value||''});
    }
  });
  return changes.length ? changes : null;
}
function simRunPre(){
  const box = document.getElementById('sim-result');
  if(!box){ toast('面板未就绪'); return; }
  box.innerHTML = '<div style="color:var(--mut);padding:12px;">⏳ 正在沙箱预演…</div>';
  const changes = simCollectChanges();
  const body = {scene: _simState.scene, depth: 5, direction: 'both'};
  if(changes) body.changes = changes;
  fetch('/api/impact/simulate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)}).then(r=>r.json()).then(res=>{
      if(!res || res.ok === false){ box.innerHTML = `<div class="rc-warn">预演失败：${esc(res.detail||'未知错误')}</div>`; return; }
      window.__simRes = res;
      box.innerHTML = simResultHtml(res);
      setTimeout(()=>{ renderImpactGraph('sim-graph-before', res.before); renderImpactGraph('sim-graph-after', res.after); }, 120);
    }).catch(e=>{ box.innerHTML = `<div class="rc-warn">预演失败：${esc(e.message||e)}</div>`; });
}
function simResultHtml(res){
  const b = res.before, a = res.after, c = res.comparison||{}, sum = c.summary||{};
  const riskHtml = (c.risk||[]).map(r=>`<div class="rc-issue"><span class="lv">${r.level==='high'?'🔴':'🟠'}</span><span><b>${esc(r.title)}</b><div style="font-size:11px;color:var(--mut);margin-top:2px;">${esc(r.desc||'')}</div></span><span class="fix">${esc(r.advice||'')}</span></div>`).join('') || '<div style="color:var(--grn);">✓ 预演未发现新增风险</div>';
  const addedHtml = (c.added||[]).map(n=>`<span class="rc-chip r"><span class="n">${n.score}</span> ${esc(n.name)} <span class="tag">${esc(n.type)}</span></span>`).join('') || '<span style="color:var(--mut);font-size:11px;">无</span>';
  const removedHtml = (c.removed||[]).map(n=>`<span class="rc-chip g"><span class="n">${n.score}</span> ${esc(n.name)} <span class="tag">${esc(n.type)}</span></span>`).join('') || '<span style="color:var(--mut);font-size:11px;">无</span>';
  const degHtml = (c.degree_changes||[]).map(d=>`<div style="font-size:11.5px;margin-bottom:2px;">${esc(d.name)} <span class="tag">${d.before}→${d.after}</span> <b style="color:${d.delta>0?'var(--red)':'var(--grn)'};">${d.delta>0?'+':''}${d.delta}</b></div>`).join('') || '<span style="color:var(--mut);font-size:11px;">无</span>';
  return `
  <h4 style="margin:14px 0 6px;">📊 预演结果对比<span class="badge">FR-CIA-4 前后对比</span></h4>
  <div class="rc-mini" style="margin-bottom:8px;">
    <span class="rc-chip b">变更前：<span class="n">${sum.before_direct||0}</span> 直接 / <span class="n">${sum.before_indirect||0}</span> 间接</span>
    <span class="rc-chip b">变更后：<span class="n">${sum.after_direct||0}</span> 直接 / <span class="n">${sum.after_indirect||0}</span> 间接</span>
    <span class="rc-chip g">变更操作 ${sum.changes||0} 项</span>
    <span class="rc-chip w">新增 ${(c.added||[]).length} / 解除 ${(c.removed||[]).length}</span>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;">
    <div style="flex:1;min-width:300px;">
      <div style="font-size:12px;font-weight:600;margin-bottom:4px;">变更前影响拓扑（基线）</div>
      <div id="sim-graph-before" style="height:340px;border:1px solid var(--line);border-radius:8px;background:#fafbfc;"></div>
    </div>
    <div style="flex:1;min-width:300px;">
      <div style="font-size:12px;font-weight:600;margin-bottom:4px;">变更后影响拓扑（预演）</div>
      <div id="sim-graph-after" style="height:340px;border:1px solid var(--line);border-radius:8px;background:#fafbfc;"></div>
    </div>
  </div>
  <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;">
    <div style="flex:1;min-width:240px;border:1px solid var(--line);border-radius:8px;padding:8px;">
      <div style="font-size:12px;font-weight:600;margin-bottom:4px;">🆕 新增影响（${(c.added||[]).length}）</div>${addedHtml}
      <div style="font-size:12px;font-weight:600;margin:8px 0 4px;">🗑 解除影响（${(c.removed||[]).length}）</div>${removedHtml}
    </div>
    <div style="flex:1;min-width:240px;border:1px solid var(--line);border-radius:8px;padding:8px;">
      <div style="font-size:12px;font-weight:600;margin-bottom:4px;">📈 影响度变化（${(c.degree_changes||[]).length}）</div>${degHtml}
    </div>
  </div>
  <div style="border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:8px;">
    <div style="font-size:12px;font-weight:600;margin-bottom:4px;">⚠ 风险提示</div>${riskHtml}
  </div>`;
}
function simExport(){
  const res = window.__simRes;
  if(!res){ toast('请先预演变更'); return; }
  const c = res.comparison||{}, sum = c.summary||{};
  const sc = _simScenes.find(s=>s.id===(res.scene||''));
  const lines = [];
  lines.push('# 变更影响分析 · 沙箱模拟对比报告（FR-CIA-4）');
  lines.push('');
  lines.push(`> 演示场景：${sc?sc.title:res.scene||'-'} ｜ 变更操作 ${sum.changes||0} 项 ｜ 沙箱预演，不影响正式模型`);
  lines.push('');
  lines.push('## 一、影响规模对比');
  lines.push('');
  lines.push('| 指标 | 变更前 | 变更后 |');
  lines.push('| --- | --- | --- |');
  lines.push(`| 直接影响 | ${sum.before_direct||0} | ${sum.after_direct||0} |`);
  lines.push(`| 间接影响 | ${sum.before_indirect||0} | ${sum.after_indirect||0} |`);
  lines.push(`| 影响深度(层) | ${(res.before&&res.before.risk_analysis&&res.before.risk_analysis.max_depth)||0} | ${(res.after&&res.after.risk_analysis&&res.after.risk_analysis.max_depth)||0} |`);
  lines.push('');
  lines.push(`## 二、新增影响节点（${(c.added||[]).length}）`);
  (c.added||[]).forEach(n=>lines.push(`- ${n.name}（${n.type}）影响度 ${n.score}，深度 ${n.depth}`));
  lines.push('');
  lines.push(`## 三、解除影响节点（${(c.removed||[]).length}）`);
  (c.removed||[]).forEach(n=>lines.push(`- ${n.name}（${n.type}）`));
  lines.push('');
  lines.push(`## 四、影响度变化（${(c.degree_changes||[]).length}）`);
  (c.degree_changes||[]).forEach(d=>lines.push(`- ${d.name}：${d.before} → ${d.after}（${d.delta>0?'+':''}${d.delta}）`));
  lines.push('');
  lines.push('## 五、风险提示');
  (c.risk||[]).forEach(r=>lines.push(`- **[${r.level}] ${r.title}**：${r.desc||''} → 建议：${r.advice||''}`));
  lines.push('');
  lines.push('---');
  lines.push('* 影响度 = 关系权重 × 层衰减（α=0.6）；模拟结果仅用于预演评估，正式变更须经人工确认（BR-2 人在回路）。');
  openPreviewReport('变更模拟 · 对比报告', renderMarkdown(lines.join('\n')));
}
/* ═══ 2026-09-15 真实图谱基线模拟（FR-CIA-4 补齐）：分析卡 → 沙箱预演真实模型 ═══ */
let _simReal = null;   // {graph, source, title, depth, direction}
async function simStartFromImpact(){
  const cd = window._lastImpactCd;
  const srcName = (cd && cd.change_source && cd.change_source.name) || '';
  if(!cd || !(cd.impact_nodes||[]).length || !srcName){ toast('暂无可预演的影响分析数据'); return; }
  openPreviewTab({id:null, kind:'report', kind_label:'报告', title:'🛠 变更模拟（沙箱 · 真实图谱）',
    preview_type:'html',
    preview_content:'<div id="sim-panel" style="font-size:12.5px;line-height:1.7;">⏳ 正在构建真实图谱基线…</div>',
    content:'', message_id:null, meta:{}});
  try{
    const r = await api('/api/impact/baseline', {method:'POST',
      body: JSON.stringify({change_source: srcName, depth: cd.depth||3, direction: cd.direction||'both'})});
    if(r && r.ok === false){ toast('基线构建失败：'+(r.reason||'未知错误')); return; }
    _simReal = {graph: r.graph, source: r.source, title: '真实图谱 · '+srcName,
                depth: r.depth||3, direction: r.direction||'both'};
    _simState.scene = '';   // 真实图谱模式：脱离演示场景（防场景说明/预置变更串扰）
    _simState.changes = [{op:'modify', target: srcName, new_value:'参数调整（示例，可编辑）'}];
    simRenderPanelReal();
  }catch(e){ toast('基线构建失败：'+(e.message||e)); }
}
function simRenderPanelReal(){
  const panel = document.getElementById('sim-panel');
  if(!panel || !_simReal) return;
  const s = _simReal.source;
  const nn = (_simReal.graph.nodes||[]).length, ne = (_simReal.graph.edges||[]).length;
  panel.innerHTML = `
    <div class="rc-chip b">🛠 变更模拟（沙箱 · 真实图谱基线）</div>
    <div class="note" style="margin:8px 0;">基线：<b>${esc(s.name)}</b>（${esc(s.entity_type||'')}）· ${nn} 节点 / ${ne} 边（release 已发布分支依赖网络）· 深 ${_simReal.depth} 层 · ${_simReal.direction==='up'?'向上游':_simReal.direction==='down'?'向下游':'双向'}。全程不写正式图谱。</div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
      <span style="font-size:12px;color:var(--mut);">变更源：${esc(s.name)}</span>
      <span style="flex:1;"></span>
      <button class="btn sm grn" onclick="simRunPreReal()">▶ 预演变更</button>
      <button class="btn sm ghost" onclick="simExport()">📄 导出对比报告</button>
    </div>
    <div id="sim-changes"></div>
    <div id="sim-result"></div>`;
  simRenderChanges();
}
function simRunPreReal(){
  const box = document.getElementById('sim-result');
  if(!box || !_simReal){ toast('面板未就绪'); return; }
  const changes = simCollectChanges();
  if(!changes){ toast('请先编辑至少一项变更操作'); return; }
  box.innerHTML = '<div style="color:var(--mut);padding:12px;">⏳ 正在沙箱预演真实图谱…</div>';
  const body = {baseline_graph: _simReal.graph, source: _simReal.source,
                depth: _simReal.depth, direction: _simReal.direction, changes,
                title: _simReal.title};
  fetch('/api/impact/simulate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)}).then(r=>r.json()).then(res=>{
      if(!res || res.ok === false){ box.innerHTML = `<div class="rc-warn">预演失败：${esc(res.detail||'未知错误')}</div>`; return; }
      window.__simRes = res;
      box.innerHTML = simResultHtml(res);
      setTimeout(()=>{ renderImpactGraph('sim-graph-before', res.before); renderImpactGraph('sim-graph-after', res.after); }, 120);
    }).catch(e=>{ box.innerHTML = `<div class="rc-warn">预演失败：${esc(e.message||e)}</div>`; });
}
function panelImpact(cd, gid) {
  window._lastImpactCd = cd;   // 供「保存到报告」读取真实分析数据（含拓扑图谱）
  // 2026-09-15 报告 2.0：改名走引用扫描专用面板（结构零传播，输出引用更新清单）
  if((cd.change||{}).type === 'rename') return panelImpactRename(cd);
  const nodes = cd.impact_nodes||[];
  const edges = cd.impact_edges||[];
  const src = cd.change_source;
  const lv = cd.impact_levels||{};
  const dirTxt = cd.direction==='up'?'向上游':cd.direction==='down'?'向下游':'双向';
  const ds = cd.depth_stats||{};
  const ts = cd.type_stats||{};
  const rs = cd.rel_type_stats||{};
  const doms = cd.domain_stats||{};
  const ra = cd.risk_analysis||{};
  const affected = nodes.filter(n=>n.impact!=='source');
  const graphKey = gid || ('impact-graph-' + Date.now());
  const lvTxt = l=>l==='high'?'高影响':(l==='mid'?'中影响':'低影响');
  const lvCls = l=>l==='high'?'st r':(l==='mid'?'st w':'st g');
  const heatBg = s=>s>=0.7?'#A32D2D':(s>=0.4?'#F5A623':'#B9C2CE');

  // ── 深度分析（FR-CIA-2 影响深度：各层直接/间接分布 + 节点徽标，清晰版）──
  const depthKeys = Object.keys(ds).sort((a,b)=>+a-+b);
  const depthHtml = depthKeys.map(d=>{
    const s = ds[d];
    const ns = nodes.filter(n=>n.depth===+d && n.impact!=='source');
    const nodesHtml = ns.map(n=>`
      <span class="rc-gnode ${n.impact==='indirect'?'ind':''}" title="${esc(n.type)} · ${lvTxt(n.level)} · 影响度 ${n.score!==undefined?n.score:''}">
        <i class="lv-dot ${n.level}"></i>${esc(n.name)}<span class="gt">${esc(n.type)}</span>
      </span>`).join('');
    return `<div class="depth-layer">
      <div class="depth-head">
        <span class="depth-no">第 ${d} 层</span>
        <span class="rc-chip r"><span class="n">${s.direct||0}</span> 直接</span>
        <span class="rc-chip a"><span class="n">${s.indirect||0}</span> 间接</span>
        <span class="rc-chip g"><span class="n">${ns.length}</span> 合计</span>
        <span style="font-size:11px;color:var(--mut);">影响度随层衰减（α=0.6）</span>
      </div>
      <div class="depth-nodes">${nodesHtml||'<span style="color:var(--mut);font-size:11px;">无元素</span>'}</div>
    </div>`;
  }).join('');

  // ── 广度分析：实体类型分布（横向条）──
  const typeMax = Math.max(1, ...Object.values(ts));
  const typeHtml = Object.entries(ts).map(([t,c])=>`
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
      <span style="flex:0 0 90px;font-size:11px;color:var(--mut);text-align:right;">${esc(t)}</span>
      <div style="flex:1;height:14px;background:#eef1f6;border-radius:7px;overflow:hidden;">
        <div style="width:${(c/typeMax*100).toFixed(1)}%;height:100%;background:#185FA5;border-radius:7px;"></div></div>
      <span style="flex:0 0 24px;font-size:11px;font-weight:600;">${c}</span>
    </div>`).join('');

  // ── 广度分析：关系类型分布 ──
  const relHtml = Object.entries(rs).map(([t,c])=>`<span class="rc-chip g"><span class="n">${c}</span> ${esc(t)}</span>`).join('');

  // ── 广度分析：跨领域影响（系统/需求/验证等工程领域）──
  const domRows = Object.entries(doms).map(([dom,o])=>`
    <tr><td><b>${esc(dom)}</b></td><td>${o.count}</td>
    <td style="font-size:11px;color:var(--mut);">${esc((o.nodes||[]).slice(0,6).join('、'))}${(o.nodes||[]).length>6?' 等':''}</td></tr>`).join('');

  // ── 影响矩阵（2026-09-15 报告 2.0：CPM 组合风险，行=变更源 列=受影响元素）──
  const rm = cd.risk_matrix||[];
  const nameOf = id => { const n = nodes.find(x=>String(x.id)===String(id)); return (n&&n.name)||id; };
  const typeOf = id => { const n = nodes.find(x=>String(x.id)===String(id)); return (n&&n.type)||''; };
  const matrixRows = (rm.length ? rm : affected.map(n=>({to:n.id, combined:n.score, path_count:1}))).slice(0, 15).map(m=>`
    <tr><td>${esc(nameOf(m.to))} <span class="tag">${esc(typeOf(m.to))}</span></td>
      <td style="text-align:center;color:#fff;background:${heatBg(m.combined||0)};font-weight:700;">${m.combined!==undefined?m.combined:'-'}</td>
      <td style="text-align:center;">${m.path_count||1} 条路径</td>
      <td style="text-align:center;">${m.direct?`<span class="st r">直接×${m.direct}</span>`:''} ${m.indirect?`<span class="st a">间接×${m.indirect}</span>`:''}</td></tr>`).join('');

  // ── 决策建议横幅（可解释规则引擎）──
  const dec = cd.decisions||{};
  const decColor = dec.recommendation==='批准' ? 'var(--grn)' : (dec.recommendation==='提交 CCB 评审' ? 'var(--blue-d)' : 'var(--amb)');
  const decBg = dec.recommendation==='批准' ? 'var(--grn-l)' : (dec.recommendation==='提交 CCB 评审' ? 'var(--blue-l)' : 'var(--amb-l)');
  const decHtml = dec.recommendation ? `<div style="margin:10px 0;border:1px solid ${decColor};background:${decBg};border-radius:8px;padding:8px 10px;">
    <b style="font-size:13px;">⚖ 决策建议：${esc(dec.recommendation)}</b>
    ${(dec.reasons||[]).map(r=>`<div style="font-size:11.5px;margin-top:2px;">· ${esc(r)}</div>`).join('')}
    ${(dec.conditions||[]).map(c=>`<div style="font-size:11.5px;color:var(--red);margin-top:2px;">▸ 条件：${esc(c)}</div>`).join('')}
  </div>` : '';

  // ── 元素关键度画像（Eckert 三分类：Absorber/Carrier/Multiplier）──
  const ep = cd.element_profile||{};
  const profCount = {absorber:0, carrier:0, multiplier:0};
  Object.values(ep).forEach(p=>{ if(profCount[p.profile]!==undefined) profCount[p.profile]++; });
  const multipliers = Object.entries(ep).filter(([,p])=>p.profile==='multiplier')
    .sort((x,y)=>y[1].out_w-x[1].out_w).slice(0,8)
    .map(([,p])=>`<span class="rc-chip r"><span class="n">${p.out_w}</span> ${esc(p.name)}</span>`).join('')
    || '<span style="color:var(--mut);font-size:11px;">无</span>';
  const profileHtml = `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:4px;">
    <span class="rc-chip g"><span class="n">${profCount.absorber}</span> 吸收型</span>
    <span class="rc-chip b"><span class="n">${profCount.carrier}</span> 传递型</span>
    <span class="rc-chip r"><span class="n">${profCount.multiplier}</span> 放大型</span></div>
    ${multipliers!=='<span style="color:var(--mut);font-size:11px;">无</span>'?`<div style="margin-top:4px;font-size:11.5px;">变更放大器（Multiplier，沿出边传播强）：${multipliers}</div>`:''}`;

  // ── 传播路径明细（CPM 全路径枚举 Top 30）──
  const pl = cd.path_list||[];
  const pathHtml = pl.map(p=>`<div style="padding:2px 0;font-size:11.5px;border-bottom:1px dashed var(--line);">
    ${p.node_names.map(esc).join(' <b style="color:var(--mut);">→</b> ')} <b style="color:${p.likelihood>=0.5?'var(--red)':'var(--mut)'};">P=${p.likelihood}</b></div>`).join('')
    || '<div style="color:var(--mut);font-size:11px;">无传播路径（该变更类型下所有传播通道权重为 0 或无可达元素）</div>';

  // ── P2 仪表板：受影响清单（分级分组 + 定位联动；graphKey 存全局供 tab/定位/保存用）──
  window._lastImpactGid = graphKey;
  const epOf = id => ((cd.element_profile||{})[String(id)]||{}).profile;
  const profTag = p => p==='multiplier'?' <span class="tag" style="color:var(--red);">放大器</span>':(p==='absorber'?' <span class="tag">吸收</span>':(p==='carrier'?' <span class="tag">传递</span>':''));
  const listRow = n=>`<tr style="cursor:pointer;" onclick="impactFocusNode('${graphKey}','${String(n.id).replace(/'/g,'')}','${esc(n.name).replace(/'/g,'')}')">
      <td>${esc(n.name)} <span class="tag">${esc(n.type)}</span>${profTag(epOf(n.id))}</td>
      <td style="text-align:center;color:#fff;background:${heatBg(n.score||0)};font-weight:700;">${n.score!==undefined?n.score:'-'}</td>
      <td style="text-align:center;">${n.depth}</td>
      <td style="text-align:center;">${n.impact==='direct'?'<span class="st r">直接</span>':'<span class="st a">间接</span>'}</td>
      <td style="text-align:center;"><button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" onclick="event.stopPropagation();impactFocusNode('${graphKey}','${String(n.id).replace(/'/g,'')}','${esc(n.name).replace(/'/g,'')}')">🎯</button></td></tr>`;
  const listGrp = (lvl, label) => {
    const rows = affected.filter(n=>n.level===lvl).sort((a,b)=>-(a.score||0)+(b.score||0)).map(listRow).join('');
    return rows ? `<tr><td colspan="5" style="background:#f2f5f9;font-size:11px;font-weight:700;">${label}（${rows?affected.filter(n=>n.level===lvl).length:0}）</td></tr>${rows}` : '';
  };
  const listHtml = (listGrp('high','🔴 高影响 — 优先复核') + listGrp('mid','🟠 中影响') + listGrp('low','🔵 低影响'))
    || '<tr><td colspan="5" style="color:var(--mut);">无受影响元素</td></tr>';

  // ── P3 重测清单 + 工作量估算表（Wiegers 面向三；勾选回写报告 ⑦）──
  const rp = cd.retest_plan||{items:[],total:0};
  const retestRows = (rp.items||[]).map((it,i)=>`<tr>
      <td><input type="checkbox" id="${graphKey}-rt-${i}" checked></td>
      <td>${esc(it.name)} <span class="tag">${esc(it.type)}</span></td>
      <td style="font-size:11.5px;">${esc(it.reason)}${it.via?`（经由 ${esc(it.via)}）`:''}</td>
      <td style="text-align:center;color:#fff;background:${heatBg(it.score||0)};font-weight:700;">${it.score!==undefined&&it.score!==null?it.score:'-'}</td>
      <td style="text-align:center;"><button class="btn sm ghost" style="font-size:10.5px;padding:1px 8px;" title="在网络图中定位该验证活动" onclick="impactFocusNode('${graphKey}','${String(it.id).replace(/'/g,'')}','${esc(it.name).replace(/'/g,'')}')">🎯</button></td></tr>`).join('');
  const ef = cd.effort_estimate||[];
  const effortRows = ef.map(r=>`<tr>
      <td>${esc(r.label)}</td>
      <td style="text-align:center;">${r.count||0}</td>
      <td style="text-align:center;"><input type="number" min="0" step="0.5" id="${graphKey}-ef-${r.key}" value="${r.effort||''}" placeholder="人日" style="width:70px;border:1px solid var(--line);border-radius:5px;padding:2px 5px;font-size:11.5px;" onchange="impactEffortSum('${graphKey}')"></td>
      <td><input type="text" id="${graphKey}-efn-${r.key}" placeholder="备注（可空）" style="width:100%;border:1px dashed var(--line);border-radius:5px;padding:2px 6px;font-size:11.5px;"></td></tr>`).join('');

  // ── 风险与建议（FR-CIA-2 影响程度分析：描述铺开 + 建议独立提示框）──
  const riskHtml = (ra.risks||[]).map(r=>`
    <div class="rc-issue"><span class="lv">${r.level==='high'?'🔴':r.level==='mid'?'🟠':'🔵'}</span>
      <div class="rc-issue-body"><b>${esc(r.title)}</b>
        <div class="rc-issue-desc">${esc(r.desc||'')}</div></div>
      <div class="fix">💡 ${esc(r.advice||'')}</div></div>`).join('') || '<div style="color:var(--grn);">✓ 未发现显著风险</div>';

  // ── 关系明细表 ──
  const nodeMap = {}; nodes.forEach(n=>{ nodeMap[String(n.id)] = n; });
  const edgeRows = edges.map(e=>`
    <tr><td>${esc((nodeMap[String(e.from)]&&nodeMap[String(e.from)].name)||e.from)}</td>
      <td>${esc(e.type)} <span class="tag">${e.score!==undefined?e.score:''}</span></td>
      <td>${esc((nodeMap[String(e.to)]&&nodeMap[String(e.to)].name)||e.to)}</td>
      <td>${e.impact==='direct'?'<span class="st r">直接</span>':'<span class="st a">间接</span>'}</td></tr>`).join('');

  const coverageTxt = ra.coverage!==undefined ? `｜ 影响占图谱 ${ra.coverage}%（广度）` : '';
  // ── 混合溯源证据（FR-CIA-1，2026-09-15）：向量库文档补充佐证 ──
  const evidence = cd.evidence||[];
  const evidenceHtml = evidence.map(e=>`
    <div style="border-bottom:1px dashed var(--line);padding:6px 0;">
      <b style="font-size:12px;">${esc(e.element)}</b> <span class="tag">${esc(e.type)}</span>
      <span style="font-size:11px;color:var(--mut);">影响度 ${e.score!==undefined?e.score:''}</span>
      ${(e.hits||[]).map(h=>`<div style="font-size:11.5px;color:var(--mut);margin:3px 0 0 12px;">
        📄 ${esc(h.source_doc)} <span class="tag">${h.score}</span>${h.origin==='ai_generated'?' <span class="tag">🤖 AI</span>':''}<br/>
        <span style="color:#5a6678;">${esc(h.snippet||'')}…</span></div>`).join('')}
    </div>`).join('') || '';
  const chg = cd.change||{};
  const chgLbl = chg.type_label || '属性/构成变更';
  return `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
    <button class="btn sm ghost" onclick="simStartFromImpact()" title="把本次影响分析的真实图谱作为基线，在沙箱中预演变更操作（FR-CIA-4）">🛠 模拟预演此变更</button>
  </div>
  <h3 style="color:var(--blue-d);margin-bottom:10px;">🔀 变更影响分析报告</h3>
    <div class="note" style="margin-bottom:8px;">报告编号：CIA-${(new Date()).toISOString().slice(0,10).replace(/-/g,'')}-001 ｜ 变更类型：<b>${esc(chgLbl)}</b> ｜ 传播深度 ${cd.depth||1} 层 · ${dirTxt} ｜ 影响度 = 关系权重 × 变更类型系数 × 层衰减</div>
    ${chg.desc?`<div class="note" style="margin-bottom:8px;"><b>变更内容：</b>${esc(chg.desc)}</div>`:`<div class="note" style="margin-bottom:8px;">⚠ 未指定具体变更内容——本报告为<b>结构敏感性分析</b>（默认按属性/构成变更传播）；补充变更描述后结论更精确</div>`}
    ${src?`<div style="margin-bottom:10px;"><b>变更源：</b>${esc(src.name)} <span class="tag">${esc(src.entity_type)}</span> <span class="tag">${esc(src.status)}</span></div>`:''}
    ${decHtml}
    <div class="rc-mini" style="margin-bottom:6px;">
      <span class="rc-chip r"><span class="n">${cd.direct_count||0}</span> 直接影响</span>
      <span class="rc-chip a"><span class="n">${cd.indirect_count||0}</span> 间接影响</span>
      <span class="rc-chip b"><span class="n">${edges.length}</span> 影响边</span>
      <span class="rc-chip r"><span class="n">${lv.high||0}</span> 高影响</span>
      <span class="rc-chip w"><span class="n">${lv.mid||0}</span> 中影响</span>
      <span class="rc-chip g"><span class="n">${lv.low||0}</span> 低影响</span>
      <span class="rc-chip b"><span class="n">${ra.max_depth||cd.depth||1}</span> 影响深度(层)</span>
    </div>
    <div style="margin-top:4px;font-size:11.5px;color:var(--mut);">
      影响概况：深度 ${ra.max_depth||(cd.depth||1)} 层、广度 ${affected.length} 个元素${coverageTxt}，
      跨 ${Object.keys(doms).length} 个工程领域，传播沿 ${Object.keys(rs).length} 类关系。${cd.indirect_count>0?'间接影响占主导时建议升级变更评审（CCB）。':''}</div>

    <h4 style="margin:16px 0 6px;display:flex;align-items:center;gap:10px;">🗺 影响拓扑图谱<span class="badge">FR-CIA-2 可视化影响路径</span>
      <button class="btn sm ghost" onclick="openImpactGraphViewer()" title="打开大屏可视化查看区：支持滚轮缩放、画布平移、节点拖动、自动布局与上下游链路高亮">🔍 大屏查看</button></h4>
    ${impactLegendHtml()}
    <div id="${graphKey}" style="width:100%;height:420px;border:1px solid var(--line);border-radius:8px;background:#fafbfc;margin-top:6px;"></div>

    <h4 style="margin:16px 0 6px;">📊 风险矩阵（CPM 组合风险）<span class="badge">行=变更源 · 列=受影响元素</span></h4>
    <div style="max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
    <table class="t" style="margin:0;"><tr><th>受影响元素</th><th style="width:80px;">组合风险</th><th style="width:80px;">传播路径</th><th style="width:130px;">直接 / 间接</th></tr>
    ${matrixRows||'<tr><td colspan="4" style="color:var(--mut);">无受影响元素</td></tr>'}
    ${rm.length>15?`<tr><td colspan="4" style="color:var(--mut);font-size:11px;">… 其余 ${rm.length-15} 个元素省略，见下方明细表</td></tr>`:''}
    </table></div>
    <div style="margin-top:6px;font-size:11px;color:var(--mut);">组合风险 = 1-∏(1-路径概率)（CPM 合并）；同元素多条路径时取合并值，直接/间接为途经该元素的路径分类。</div>

    <h4 style="margin:16px 0 6px;">📏 影响深度分析<span class="badge">各层直接/间接分布</span></h4>
    ${depthHtml||'<div style="color:var(--mut);">未构建影响传播链</div>'}

    <h4 style="margin:16px 0 6px;">🌐 影响广度分析<span class="badge">跨领域 / 类型 / 关系分布</span></h4>
    <div style="display:flex;gap:14px;flex-wrap:wrap;">
      <div style="flex:1;min-width:240px;border:1px solid var(--line);border-radius:8px;padding:10px;">
        <div style="font-size:12px;font-weight:600;margin-bottom:6px;">受影响元素类型分布（${affected.length}）</div>
        ${typeHtml||'<div style="color:var(--mut);font-size:11px;">无统计</div>'}
      </div>
      <div style="flex:1;min-width:240px;border:1px solid var(--line);border-radius:8px;padding:10px;">
        <div style="font-size:12px;font-weight:600;margin-bottom:6px;">传播关系类型（${Object.keys(rs).length}）</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">${relHtml||'<span style="color:var(--mut);font-size:11px;">无统计</span>'}</div>
        <div style="font-size:12px;font-weight:600;margin:12px 0 6px;">跨领域影响（${Object.keys(doms).length}）</div>
        <table class="t" style="margin:0;"><tr><th>工程领域</th><th>数量</th><th>受影响元素</th></tr>${domRows||'<tr><td colspan="3" style="color:var(--mut);">无统计</td></tr>'}</table>
      </div>
    </div>

    <h4 style="margin:16px 0 6px;">⚠ 风险与建议<span class="badge">FR-CIA-2 影响程度分析</span></h4>
    <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;">${riskHtml}</div>

    <h4 style="margin:16px 0 6px;">🧭 元素关键度画像<span class="badge">Eckert 吸收 / 传递 / 放大</span></h4>
    <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;">${profileHtml}
      <div style="margin-top:6px;font-size:11px;color:var(--mut);">* Multiplier（放大型）元素产生的变更多于其吸收的，是变更雪崩的潜在源头，评审时优先关注。</div></div>

    <h4 style="margin:16px 0 6px;">🧪 重测与工作量<span class="badge">Wiegers 面向三 · 勾选回写报告</span></h4>
    <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;">
      <div style="font-size:12px;font-weight:600;margin-bottom:6px;">需重测验证活动（${rp.total||0} 项，勾选项写入保存的报告）</div>
      <table class="t" style="margin:0;"><tr><th style="width:40px;">选</th><th>验证活动</th><th>受影响原因</th><th style="width:70px;">组合风险</th><th style="width:52px;">定位</th></tr>
      ${retestRows||'<tr><td colspan="5" style="color:var(--grn);font-size:12px;">✓ 无需重测——影响范围内无验证活动，受影响需求也未挂接验证活动</td></tr>'}</table>
      <div style="font-size:12px;font-weight:600;margin:12px 0 6px;">工作量估算表（涉及数量由影响分布预填；人日<b>人工编辑</b>，不做自动估算——Wiegers R1 结构化清单）</div>
      <table class="t" style="margin:0;"><tr><th>任务项</th><th style="width:80px;">涉及数量</th><th style="width:90px;">工作量</th><th>备注</th></tr>
      ${effortRows||''}
      <tr><td colspan="2" style="text-align:right;font-weight:700;">合计</td>
        <td style="text-align:center;font-weight:700;" id="${graphKey}-ef-total">—</td><td></td></tr></table>
      <div style="margin-top:6px;font-size:11px;color:var(--mut);">* 数量列口径：模型修改=受影响模型类元素、需求更新=受影响需求/约束、接口调整=受影响接口类、重测执行=上方清单勾选数、评审=高影响元素数。填完人日自动合计，保存产物时一并写入。</div>
    </div>

    <h4 style="margin:16px 0 6px;">🔀 传播路径明细<span class="badge">CPM 全路径枚举（限深 ${cd.depth||1} 层）</span></h4>
    <details style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;">
      <summary style="cursor:pointer;font-size:12px;font-weight:600;">共 ${cd.path_count||pl.length} 条传播路径（点击展开 Top ${pl.length}，按概率降序）</summary>
      <div style="margin-top:6px;max-height:320px;overflow:auto;">${pathHtml}</div>
    </details>

    <h4 style="margin:16px 0 6px;">🧾 证据补充（向量库检索）<span class="badge">混合溯源</span></h4>
    <div style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;">
      ${evidenceHtml || '<div style="color:var(--mut);font-size:12px;">暂无向量库补充证据（图谱依赖网络为主要证据源；知识库覆盖不足时自动检索相关文档佐证）</div>'}
      <div style="margin-top:6px;font-size:11px;color:var(--mut);">* 图谱依赖网络为主证据；以下为向量库（资料库分块）对高影响元素的文档佐证，用于知识库覆盖不足时的混合溯源。</div>
    </div>

    <h4 style="margin:16px 0 6px;">🔗 影响关系明细（${edges.length}）<span class="badge">FR-CIA-1~4</span></h4>
    <div style="max-height:320px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
    <table class="t" style="margin:0;"><tr><th>来源</th><th>关系</th><th>目标</th><th>影响级别</th></tr>
    ${edgeRows||'<tr><td colspan="4" style="color:var(--mut);">无影响边</td></tr>'}</table></div>
    <p style="margin-top:14px;color:var(--mut);font-size:12px;">* 变更结果须经人工确认后提交合并（BR-2 人在回路）；组合风险 = 1-∏(1-路径概率)，路径概率 = 沿途关系权重 × 变更类型系数之积（CPM 方法，限深 ${cd.depth||1} 层）；证据来自图谱依赖网络 + 向量库混合溯源。</p>`;
}
/* 2026-09-15 报告 2.0：改名专用面板 —— 结构零传播，输出「引用更新清单」 */
function panelImpactRename(cd) {
  const refs = cd.references||{};
  const src = cd.change_source||{};
  const dec = cd.decisions||{};
  const cnt = Object.values(refs).reduce((s,a)=>s+((a&&a.length)||0),0);
  const sec = (title, items, fmt) => `<h4 style="margin:14px 0 6px;">${title}（${(items||[]).length}）</h4>` +
    ((items&&items.length)? items.map(fmt).join('') : '<div style="color:var(--mut);font-size:12px;">未发现</div>');
  return `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
    <span style="font-size:11px;color:var(--mut);">改名不改结构依赖（标识不变）——本报告为引用更新清单，非影响传播链</span>
  </div>
  <h3 style="color:var(--blue-d);margin-bottom:10px;">✏️ 改名影响分析 · 引用更新清单</h3>
  <div class="note" style="margin-bottom:8px;">变更类型：<b>改名</b> ｜ 变更源：${esc(src.name||'')}（${esc(src.entity_type||'')}）</div>
  <div class="note" style="margin-bottom:10px;">✓ <b>结构影响：0</b> —— 改名不改变元素标识与依赖关系，组合/满足/验证等结构传播为零；需要处理的只有「文本引用」：以下 ${cnt} 处硬编码了旧名，需同步更新。</div>
  ${sec('📄 资料库文档引用', refs.docs||[], d=>`<div style="padding:4px 0;border-bottom:1px dashed var(--line);font-size:12px;">📄 <b>${esc(d.source_doc)}</b><div style="font-size:11.5px;color:var(--mut);margin-left:18px;">${esc(d.snippet||'')}…</div></div>`)}
  ${sec('📌 需求 / 元素文本引用', refs.requirements||[], r=>`<div style="padding:4px 0;border-bottom:1px dashed var(--line);font-size:12px;"><b>${esc(r.name)}</b> <span class="tag">${esc(r.entity_type)}</span><div style="font-size:11.5px;color:var(--mut);margin-left:18px;">${esc(r.snippet||'')}…</div></div>`)}
  ${sec('📖 词典词条', refs.glossary||[], g=>`<div style="padding:4px 0;font-size:12px;">📖 ${esc(g.term||'')}</div>`)}
  ${sec('💻 代码产物引用', refs.code||[], x=>`<div style="padding:4px 0;border-bottom:1px dashed var(--line);font-size:12px;">💻 <b>${esc(x.title)}</b><div style="font-size:11.5px;color:var(--mut);margin-left:18px;">${esc(x.snippet||'')}…</div></div>`)}
  ${dec.recommendation?`<div style="margin:12px 0;border:1px solid var(--amb);background:var(--amb-l);border-radius:8px;padding:8px 10px;">
    <b style="font-size:13px;">⚖ 决策建议：${esc(dec.recommendation)}</b>
    ${(dec.reasons||[]).map(r=>`<div style="font-size:11.5px;margin-top:2px;">· ${esc(r)}</div>`).join('')}
    ${(dec.conditions||[]).map(c=>`<div style="font-size:11.5px;color:var(--red);margin-top:2px;">▸ 条件：${esc(c)}</div>`).join('')}</div>`:''}
  <p style="color:var(--mut);font-size:12px;">* 提示：本体<b>类型</b>改名已在保存时自动迁移实例与关系引用；本清单处理的是实例<b>显示名</b>变更后的文本残留。</p>`;
}
function panelReview(cd) {
  const issues = cd.issues||[];
  return `<h3 style="color:var(--blue-d);margin-bottom:10px;">✅ 预评审校验报告</h3>
    <div class="note" style="margin-bottom:12px;">意图：预评审校验（review）｜ 评审对象 ${cd.total_elements||0} 个元素（已评审 ${cd.reviewed||0} / 候选 ${cd.candidates||0}）</div>
    <div class="rc-mini" style="margin-bottom:10px;">
      <span class="rc-chip b"><span class="n">${cd.score||0}</span> 综合评分</span>
      <span class="rc-chip g"><span class="n">${cd.reviewed||0}</span> 已评审</span>
      <span class="rc-chip a"><span class="n">${cd.candidates||0}</span> 候选待审</span>
      <span class="rc-chip r"><span class="n">${cd.syntax_errors||0}</span> 语法错误</span>
    </div>
    <h4 style="margin:10px 0 6px;">问题清单（${issues.length}）<span class="badge">FR-VR-1/FR-VC-1~5</span></h4>
    <table class="t"><tr><th>级别</th><th>类型</th><th>描述</th><th>建议处理</th></tr>
    ${issues.map(i=>`<tr><td>${i.level==='error'?'<span class="st r">错误</span>':i.level==='warning'?'<span class="st a">警告</span>':'<span class="st b">提示</span>'}</td><td>${esc(i.type)}</td><td>${esc(i.desc)}</td><td>${esc(i.fix||'-')}</td></tr>`).join('')||'<tr><td colspan="4" style="color:var(--mut);">无问题</td></tr>'}</table>
    <div style="margin-top:14px;display:flex;align-items:center;gap:8px;"><b>评审结论：</b><span class="st ${cd.conclusion==='不通过'?'r':cd.conclusion==='有条件通过'?'a':'ok'}">${esc(cd.conclusion)}</span></div>
    <p style="margin-top:10px;color:var(--mut);font-size:12px;">* 问题修复后需重新评审，通过方可进入合并流程。</p>`;
}
function dispDescr(it){
  // 2026-08-28：descr 为 URL 时显示为端点地址（工具卡片常见：descr 误存 URL），否则原样；空则占位
  // 2026-09-01：市场统一后新 API 返回 description（旧轨 descr），兼容双字段
  const d = ((it && (it.description ?? it.descr)) || '').trim();
  if(/^https?:\/\//i.test(d)) return '🔗 端点：' + d;
  return d || '暂无描述';
}
function cardMeta(it){
  const seg = [];
  if(it.version) seg.push('v'+it.version);
  if(it.category) seg.push(esc(it.category));
  if(it.install_count!=null) seg.push('安装 '+it.install_count);
  return seg.length ? '<div style="font-size:10.5px;color:var(--mut);margin-top:4px;display:flex;gap:6px;flex-wrap:wrap;"><span class="fpill gray">'+seg.join('</span><span class="fpill gray">')+'</span></div>' : '';
}
// 插件库子模块 tab 切换（Agent/技能/工具/插件市场）
function switchStLibTab(el, pane){
  // 2026-09-08 修复：各子页内嵌的是各自的 tab 条副本，此前仅同步"点击所在那一条"，
  // 切到没有 tab 条的面板（st-model）后就再也切不回来。改为全局按 data-pane 同步高亮。
  document.querySelectorAll('[data-tabgrp=stlib]').forEach(x=>x.classList.toggle('on', x.dataset.pane===pane));
  if(el && !el.dataset.pane) el.classList.add('on');
  // 复用系统 subpage 显隐机制（.on class），避免绕过导致内容/加载钩子失效
  document.querySelectorAll('.subpage[data-tabpanel="st"]').forEach(sp=>sp.classList.toggle('on', sp.id===pane));
  // 主导航高亮跟随：能力中心为单入口，只要停留在能力中心页就保持高亮（原条件 st-lib 恒为 false，导致高亮丢失）
  const _pgSt = document.getElementById('pg-studio');
  if(_pgSt && _pgSt.classList.contains('on')){
    document.querySelectorAll('#mainnav a[data-page="studio"]').forEach(a=>a.classList.add('on'));
  }
  // 2026-09-16：能力中心接管四个类型页（市场 / 我安装的双区，读 /api/plugins）
  if(typeof capMountForTab === "function" && capMountForTab(pane)){
    const _bcCap = document.getElementById("br-cur");
    if(_bcCap) _bcCap.textContent = ST_TAB_TITLES[pane] || "能力中心";
    return;
  }
  // 按页触发加载（对齐原 studio tab 钩子）
  if(pane==='st-agent') { loadAgents(); loadToolLogs(); }
  if(pane==='st-skill') loadSkills();
  if(pane==='st-mcp') switchToolTab(null, _toolTab);
  if(pane==='st-market') loadMarketManage();  // 2026-09-17 定稿：插件市场单页直出（消费视图在类型页 cap-zone）
  if(pane==='st-model') loadLLMProviders();
  // 2026-09-04 v4：能力中心内部子 tab 切换 → 同步顶栏面包屑 cur
  const _bc = document.getElementById('br-cur');
  if(_bc) _bc.textContent = ST_TAB_TITLES[pane] || '能力中心';
}
// ── 插件库（4 页合一）：Agent/技能/工具/提示词 · 本地安装区 + 公共市场区 + 管理区 ──
let _libKind = '';
function _libCard(it, zone){
  const kindTag = {agent:'Agent', skill:'Skill', mcp:'MCP', prompt:'提示词'}[it.kind] || it.kind || '';
  const stBadge = zone==='mine' ? '<span class="st" style="background:#eef4ff;color:#1c4fc4;">已安装/自有</span>' : '<span class="st" style="background:#e8f5e9;color:#2e7d32;">市场</span>';
  const ops = zone==='mine'
    ? '<button class="btn sm ghost" onclick="go(\'studio\',\'st-'+ (it.kind==='prompt'?'prompt':(it.kind||'skill')) +'\')">打开</button>'
    : ((it.kind==='skill'||it.kind==='mcp') ? '<button class="btn sm" onclick="marketInstall(\''+it.kind+'\',\''+esc(String(it.name).replace(/'/g,"\\'"))+'\',this)">＋ 安装</button>' : '');
  return '<div class="card"><div style="font-weight:600;font-size:12px;">'+esc(it.name||'')+' <span class="tag">'+esc(kindTag)+'</span> '+stBadge+'</div>'
    + '<div style="font-size:11.5px;color:var(--mut);margin-top:4px;">'+dispDescr(it)+'</div>'+cardMeta(it)
    + '<div class="bar" style="margin-top:6px;">'+ops+'</div></div>';
}
async function loadPluginLib(){
  const body = document.getElementById('lib-body'); if(!body) return;
  const q = (document.getElementById('lib-search')?.value||'').trim().toLowerCase();
  body.innerHTML = '<div class="loading">加载中…</div>';
  const k = _libKind;
  let mine = [], market = [];
  try{
    if(k==='prompt'){
      body.innerHTML = '<div class="card" style="margin-bottom:10px;">📝 提示词模板为独立管理视图：<button class="btn sm" onclick="go(\'studio\',\'st-prompt\')">打开提示词模板页</button></div>';
      return;
    }
    if(k){   // 指定类型：market + mine 并行
      const [m1, m2] = await Promise.all([
        api('/api/studio/market?kind='+k).catch(()=>({items:[]})),
        api('/api/studio/plugins/mine?kind='+k).catch(()=>({items:[]}))
      ]);
      market = (m1.items||[]).filter(x=>!x.installed && !x.builtin);
      mine = (m2.items||[]).filter(x=>!x.builtin);
    } else {  // 全部：三类并行
      const rs = await Promise.all(['agent','skill','mcp'].map(async kk=>{
        const [m1, m2] = await Promise.all([
          api('/api/studio/market?kind='+kk).catch(()=>({items:[]})),
          api('/api/studio/plugins/mine?kind='+kk).catch(()=>({items:[]}))
        ]);
        return { market: (m1.items||[]).filter(x=>!x.installed && !x.builtin).map(x=>Object.assign({kind:kk},x)),
                 mine: (m2.items||[]).filter(x=>!x.builtin).map(x=>Object.assign({kind:kk},x)) };
      }));
      rs.forEach(r=>{ market = market.concat(r.market); mine = mine.concat(r.mine); });
    }
    if(q){ mine = mine.filter(x=>((x.name||'')+(x.descr||'')).toLowerCase().includes(q)); market = market.filter(x=>((x.name||'')+(x.descr||'')).toLowerCase().includes(q)); }
    const sec = (title, arr, zone) => '<div class="grp">── '+title+'（'+arr.length+'）──</div>'
      + (arr.length ? '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;">'+arr.map(x=>_libCard(x,zone)).join('')+'</div>'
                    : '<div class="hint" style="margin-bottom:12px;">（空）</div>');
    body.innerHTML = sec('本地安装区', mine, 'mine') + sec('公共市场区', market, 'market')
      + '<div class="grp">── 插件管理区（管理员）──</div>'
      + '<div class="card" style="border-style:dashed;"><div class="bar"><span class="pill">在售清单 · 上架审核 · 分享管理</span><span style="flex:1"></span><button class="btn sm" onclick="go(\'studio\',\'st-market\')">打开插件管理</button></div></div>';
  }catch(e){ body.innerHTML = '<div style="color:var(--red);padding:12px;">插件库加载失败：'+esc(e.message||e)+'</div>'; }
}

// 一致性校验（2026-08-31 交互优化）：工具行只留摘要，完整问题清单走右侧滑窗（级别徽标 + 实体名可点击定位）
async function oeConsistency(){
  const box = document.getElementById('oe-consistency'); if(!box) return;
  box.innerHTML = '校验中…';
  try{
    const r = await api('/api/knowledge/ontology/validate');
    if(r.error){ box.innerHTML = '<span style="color:var(--red);">'+esc(r.error)+'</span>'; return; }
    const issues = r.issues || [];
    box.innerHTML = issues.length
      ? `<span style="color:var(--amb);cursor:pointer;" onclick="oeConsistency()" title="点击重新校验">⚠️ ${issues.length} 项问题（高 ${r.high} · 低 ${r.low}）</span>`
      : '<span style="color:var(--grn,#2f855a);">✅ 校验通过</span>';
    if(!issues.length){
      openPanel('🩺 一致性校验结果', `<div style="display:flex;align-items:center;gap:10px;padding:24px 16px;">
        <span style="font-size:30px;">✅</span>
        <div><b style="font-size:14px;color:var(--grn,#2f855a);">校验通过</b>
        <div style="font-size:11.5px;color:var(--mut);margin-top:4px;">无循环继承 / 悬空父类 / 孤立类 / 关系缺域值域 / 重名</div></div></div>
        <div style="text-align:right;padding:0 16px 16px;"><button class="btn ghost" onclick="closePanel()">关闭</button></div>`);
      return;
    }
    const TYPE_LABEL = {cycle:'循环继承', dangling_parent:'悬空父类', isolated:'孤立类', missing_dom_range:'关系缺定义域/值域', duplicate:'重名'};
    const sorted = [...issues].sort((a,b)=>((a.severity==='high'?0:1)-(b.severity==='high'?0:1)));
    let h = `<div style="font-size:12px;color:var(--mut);margin-bottom:10px;line-height:1.7;">
      共 <b>${issues.length}</b> 项问题 · <span style="color:var(--red);font-weight:600;">高 ${r.high}</span> · <span style="color:#c77700;font-weight:600;">低 ${r.low}</span><br>
      <span style="font-size:11px;">点击实体名可定位到该类型（自动切到对应视图）</span></div>`;
    h += sorted.map(x=>{
      const sev = x.severity==='high';
      const t = (ontData && ontData.types||[]).find(tt=>tt.name===x.name);
      const kindIcon = t ? (t.type_kind==='relation'?'🔗':t.type_kind==='attribute'?'◆':'🟦') : '📄';
      return `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px 10px;border:1px solid ${sev?'#F0C4C4':'#EEE0BF'};border-radius:8px;margin-bottom:6px;background:${sev?'#FFF6F6':'#FFFBF1'};">
        <span style="flex:none;font-size:10px;font-weight:700;padding:2px 8px;border-radius:9px;background:${sev?'var(--red)':'#c77700'};color:#fff;margin-top:1px;">${sev?'高':'低'}</span>
        <div style="flex:1;min-width:0;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
            <b style="font-size:12.5px;cursor:pointer;color:var(--blue-d);" onclick="_locateOntType('${ontJs(x.name)}')" title="点击定位到该类型">${esc(kindIcon)} ${esc(x.name)}</b>
            <span class="tag" style="font-size:10px;">${esc(TYPE_LABEL[x.type]||x.type||'问题')}</span>
          </div>
          <div style="font-size:11.5px;color:var(--txt,#222);margin-top:3px;">${esc(x.message)}</div>
        </div>
      </div>`;
    }).join('');
    h += `<div style="margin-top:10px;text-align:right;"><button class="btn ghost" onclick="closePanel()">关闭</button></div>`;
    openPanel('🩺 一致性校验结果', h);
  }catch(e){ box.innerHTML = '<span style="color:var(--red);">'+esc(e.message||e)+'</span>'; }
}
// 校验问题定位：关闭滑窗 → 切到本体模型页 → 待数据就绪后选中该类型（自动切对应视图）
function _locateOntType(name){
  closePanel();
  try{ go('kb','kb-c'); }catch(e){}
  const _try = ()=>{
    // 注意：ontData 为顶层 let 变量，不挂 window；用 typeof 判断作用域变量
    if(typeof ontData!=='undefined' && ontData && ontData.types && ontData.types.length && window.selectOntType){ selectOntType(name); }
    else setTimeout(_try, 300);
  };
  setTimeout(_try, 300);
}

// ── Cursor 式圈定 v2（2026-08-29，按用户截图样式）：选区浮动工具条（图1）+ 输入框引用 chip（图2）──
let chatRefs = [];   // 待发送引用 [{label, text}]
function injectChatRef(text, label){
  const clip = String(text||'').trim().slice(0, 4000);
  if(!clip){ toast('选中内容为空'); return; }
  chatRefs.push({label:(label||'引用')+' · '+clip.slice(0,18)+(clip.length>18?'…':''), text:clip});
  renderRefBar();
  toast('📎 已加入对话，继续输入问题后发送');
}
function renderRefBar(){
  const bar = document.getElementById('ref-bar'); if(!bar) return;
  if(!chatRefs.length){ bar.style.display='none'; bar.innerHTML=''; return; }
  bar.style.display='flex';
  bar.innerHTML = chatRefs.map((r,i)=>'<span class="attach-chip"><span style="flex:none;">📄</span><span class="an" title="'+esc(r.text.slice(0,120))+'">'+esc(r.label)+'</span><span class="rm" onclick="removeChatRef('+i+')" title="移除引用">✕</span></span>').join('');
}
function removeChatRef(i){ chatRefs.splice(i,1); renderRefBar(); }
function pvFloatBarEnsure(){
  if(window._pvBar && document.body.contains(window._pvBar)) return window._pvBar;
  const b = document.createElement('div');
  b.id = 'pv-float-bar';
  b.style.cssText = 'position:fixed;z-index:5000;display:none;align-items:center;gap:2px;background:#232833;border-radius:8px;padding:4px 6px;box-shadow:0 4px 14px rgba(0,0,0,.28);';
  b.innerHTML = '<button style="background:none;border:none;color:#fff;font-size:12px;padding:3px 8px;cursor:pointer;border-radius:6px;" onmousedown="event.preventDefault()">💬 加入对话 <span style="opacity:.6;font-size:10px;">Ctrl J</span></button>'
    + '<span style="width:1px;height:14px;background:rgba(255,255,255,.18);"></span>'
    + '<button style="background:none;border:none;color:#fff;font-size:12px;padding:3px 8px;cursor:pointer;border-radius:6px;" onmousedown="event.preventDefault()">📄 引用整文件</button>';
  b.querySelectorAll('button')[0].onclick = ()=>pvFloatAction('ref');
  b.querySelectorAll('button')[1].onclick = ()=>pvFloatAction('file');
  document.body.appendChild(b);
  window._pvBar = b;
  return b;
}
function pvFloatUpdate(){
  const pv = document.getElementById('preview-content');
  const sel = window.getSelection();
  const bar = pvFloatBarEnsure();
  const inPv = sel && !sel.isCollapsed && sel.anchorNode && pv && pv.contains(sel.anchorNode);
  if(!inPv){ bar.style.display = 'none'; return; }
  window._pvSelection = sel.toString();
  let rect = null;
  try{ rect = sel.getRangeAt(0).getBoundingClientRect(); }catch(e){}
  if(!rect || (!rect.width && !rect.height)){ bar.style.display='none'; return; }
  bar.style.display = 'flex';
  bar.style.top = Math.max(8, rect.top - 42) + 'px';
  bar.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 270)) + 'px';
}
function pvFloatAction(kind){
  const a = window._pvCurrent || {};
  const t = window._pvSelection || (window.getSelection() ? window.getSelection().toString() : '');
  const bar = window._pvBar; if(bar) bar.style.display = 'none';
  if(kind === 'ref'){ injectChatRef(t, '选中片段 · ' + (a.title || '预览')); }
  else {
    const bodyEl = document.getElementById('preview-content');
    injectChatRef((a.content!=null && String(a.content).trim()) ? String(a.content) : (bodyEl ? bodyEl.innerText : ''), '文件 · ' + (a.title || ''));
  }
  if(window.getSelection) window.getSelection().removeAllRanges();
}
document.addEventListener('selectionchange', ()=>setTimeout(pvFloatUpdate, 10));
window.addEventListener('scroll', ()=>{ const b = window._pvBar; if(b && b.style.display==='flex') b.style.display='none'; }, true);
window.addEventListener('keydown', e=>{
  if(e.ctrlKey && (e.key==='j' || e.key==='J')){
    const pv = document.getElementById('preview-content');
    const sel = window.getSelection();
    if(pv && sel && !sel.isCollapsed && pv.contains(sel.anchorNode)){ e.preventDefault(); pvFloatAction('ref'); }
  }
});
// ── AI 建模产物列表右键菜单（2026-08-29）：打开 / 加入会话 / 另存为 ──
function artContextMenu(ev, id){
  ev.preventDefault(); ev.stopPropagation();
  closeDocCtxMenu();
  const a = (_artList||[]).find(x=>x.id===id) || {};
  const m = document.createElement('div');
  m.id = 'doc-ctx-menu';
  m.style.cssText = 'position:fixed;z-index:6000;min-width:150px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.14);padding:4px;font-size:12px;';
  const item = (label, fn) => '<div class="rm-item" style="padding:6px 10px;border-radius:6px;" onclick="'+fn+'">'+label+'</div>';
  m.innerHTML = item('📖 打开', `artPreviewV3(${id})`)
    + item('📎 加入会话', `artAddToChat(${id})`)
    + item('💾 另存为', `artSaveAs(${id})`)
    + item('📥 收编入资料库', `artIngestToDocs(${id})`);
  document.body.appendChild(m);
  m.style.left = Math.min(ev.clientX, window.innerWidth - m.offsetWidth - 8) + 'px';
  m.style.top = Math.min(ev.clientY, window.innerHeight - m.offsetHeight - 8) + 'px';
}
// 2026-09-15 AI 产物收编入资料库（显式收编 = 人审门禁；见 docs/AI产物收编资料库方案.md）
// 类别为固定词表 → 多选下拉（复选列表），后端按「、」拆分校验并合并存储
async function artIngestToDocs(id){
  const a = (_artList||[]).find(x=>x.id===id) || {};
  let names = [];
  try{
    const cats = await api('/api/knowledge/categories');
    names = (Array.isArray(cats) ? cats : (cats.categories || [])).map(x => (x.name || x)).filter(Boolean);
  }catch(e){}
  const suggest = (a.kind==='code'||a.kind==='sysml') ? (names.includes('可复用构件')?['可复用构件']:[]) : [];
  const picked = await multiSelectDialog({
    title: '📥 收编入资料库 · 选择知识类别',
    message: `产物「${a.title||id}」收编入资料库（向量化 + 知识分类）。\n收编后 🤖 标记，AI 建模检索默认不消费（可在建模范围显式开启）。`,
    options: names, selected: suggest, okText: '确认收编'
  });
  if(picked === null) return;
  await _artIngestCall(id, picked.join('、'), false);
}
async function _artIngestCall(id, category, override){
  try{
    const r = await api('/api/documents/ingest-artifact', {method:'POST',
      body:JSON.stringify({artifact_id:id, knowledge_category:category, override:!!override})});
    if(r.needs_confirm){
      const ok = await confirmDialog(
        `资料库已存在相似文档「${r.similar_filename}」（相似度 ${r.similarity}）。\n\n继续收编将作为新版本并存，旧版本会被标记为已取代。确认？`,
        {title:'查重确认'});
      if(ok) await _artIngestCall(id, category, true);
      return;
    }
    if(r.error){ toast('收编失败：'+r.error); return; }
    toast(`✅ 已收编 → 文档 #${r.doc_id}（${r.chunk_count||0} 块${r.category?' · '+r.category:''}）`);
  }catch(e){ toast('收编失败：'+(e.message||e)); }
}
// SysML 视图产物（视图文件）：preview_content 为空，真实内容是 meta.sysml_views 的投影数据
// （nodes/edges/视图元数据）——序列化为结构化文本注入对话，AI 可直接推理模型元素与关系
function _sysmlViewsToText(meta){
  const sv = (meta && meta.sysml_views) || null;
  if(!sv || !sv.views) return '';
  const parts = [];
  Object.values(sv.views).forEach(vm=>{
    if(!vm) return;
    const v = vm.view || {};
    const lines = ['【SysML 视图 · '+(v.name||v.type||'未命名')+'（'+(v.type||'')+(v.kind?' · '+v.kind:'')+'）】'];
    if(v.desc) lines.push('说明：'+v.desc);
    const nodes = vm.nodes || [];
    const nameOf = id => { const n = nodes.find(x=>x.id===id); return (n && n.name) || id; };
    if(nodes.length){
      lines.push('元素（'+nodes.length+'）：');
      nodes.forEach(n=>{
        let attrs = '';
        try{
          const aa = (typeof n.attrs==='string' ? JSON.parse(n.attrs||'{}') : (n.attrs||{})) || {};
          // 过滤空值噪音（stereotype=/ports=[]/value= 等），只保留有信息量的属性
          attrs = Object.entries(aa).filter(([k2,v2])=>v2!=null && String(v2).trim()!=='' && String(v2)!=='[]').map(([k2,v2])=>k2+'='+v2).join(', ');
        }catch(e){}
        lines.push('- '+(n.name||n.id)+(n.kind?'（'+n.kind+(n.type?' · '+n.type:'')+'）':'')+(attrs?' {'+attrs+'}':''));
      });
    }
    const edges = vm.edges || [];
    if(edges.length){
      lines.push('关系（'+edges.length+'）：');
      edges.forEach(e=>lines.push('- '+nameOf(e.source)+' → '+nameOf(e.target)+(e.label?' ['+e.label+']':'')+(e.kind?'（'+e.kind+'）':'')));
    }
    (vm.warnings||[]).forEach(w=>lines.push('⚠ '+w));
    parts.push(lines.join('\n'));
  });
  return parts.join('\n\n');
}
async function artAddToChat(id){
  try{
    const a = await api('/api/artifacts/'+id);
    if(a.error){ toast('读取文件失败：'+a.error); return; }
    let text = String(a.preview_content || a.content || '').trim();
    let label = '文件 · '+(a.title||('art#'+id));
    if(!text && a.kind==='sysml'){
      text = _sysmlViewsToText(a.meta).trim();
      if(text){ label = 'SysML 视图 · '+(a.title||''); toast('视图文件已按结构化文本注入（元素/关系清单）'); }
    }
    if(!text){ toast('文件无可引用文本内容'); return; }
    injectChatRef(text.slice(0, 4000), label);
  }catch(e){ toast('加入会话失败：'+(e.message||e)); }
}
function artSaveAs(id){
  const a = (_artList||[]).find(x=>x.id===id) || {};
  const el = document.createElement('a');
  el.href = '/api/artifacts/'+id+'/download';
  el.download = a.title || ('artifact_'+id);
  document.body.appendChild(el); el.click(); el.remove();
  toast('💾 开始下载：'+(a.title||('artifact#'+id)));
}

// ── 文件列表右键菜单（2026-09-11 米爸：去 icon，打开/另存为 → 下载）──
function _docById(id){ return (_docs||[]).find(d=>d.id===id) || {}; }
function docContextMenu(ev, id){
  ev.preventDefault(); ev.stopPropagation();
  closeDocCtxMenu();
  const d = _docById(id);
  const m = document.createElement('div');
  m.id = 'doc-ctx-menu';
  m.style.cssText = 'position:fixed;z-index:6000;min-width:150px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.14);padding:4px;font-size:12px;';
  const item = (label, fn, red) => '<div class="rm-item" style="padding:6px 10px;border-radius:6px;'+(red?'color:var(--red);':'')+'" onclick="'+fn+'">'+label+'</div>';
  m.innerHTML = item('加入会话', `docAddToChat(${id})`)
    + item('下载', `downloadDoc(${id})`)
    + '<div style="border-top:1px solid var(--line);margin:3px 2px;"></div>'
    + item('追溯', `viewDocTrace(${id})`)
    + item('元数据', `editDocMeta(${id})`)
    + '<div style="border-top:1px solid var(--line);margin:3px 2px;"></div>'
    + item('删除', `deleteDoc(${id})`, true);
  document.body.appendChild(m);
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(ev.clientX, window.innerWidth - w - 8) + 'px';
  m.style.top = Math.min(ev.clientY, window.innerHeight - h - 8) + 'px';
}
function closeDocCtxMenu(){ const m = document.getElementById('doc-ctx-menu'); if(m) m.remove(); }
document.addEventListener('click', closeDocCtxMenu);
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeDocCtxMenu(); });
window.addEventListener('scroll', closeDocCtxMenu, true);

async function docAddToChat(id){
  const d = _docById(id);
  try{
    toast('读取文档内容…');
    const r = await api('/api/documents/'+id+'/source');
    if(r.error){ toast('读取失败：'+r.error); return; }
    const clip = String(r.content||'').trim().slice(0, 4000);
    if(!clip){ toast('文档内容为空'); return; }
    // 跳转 AI 建模会话并注入引用 chip（复用 Cursor 式 chatRefs 通道）
    go('ai');
    setTimeout(()=>{
      chatRefs.push({label:'文档 · '+(d.filename||r.filename||('doc#'+id)), text:clip});
      if(typeof renderRefBar === 'function') renderRefBar();
      toast('📎 已加入会话（'+(d.filename||'')+'），输入问题后发送');
    }, 450);
  }catch(e){ toast('加入会话失败：'+(e.message||e)); }
}
function downloadDoc(id){
  const d = _docById(id);
  const a = document.createElement('a');
  a.href = '/api/documents/'+id+'/download';
  a.download = d.filename || ('document_'+id);
  document.body.appendChild(a); a.click(); a.remove();
  toast('开始下载：'+(d.filename||('doc#'+id))+'（原件缺失时自动导出抽取文本 .txt）');
}

// 2026-09-04 AI 建模页附件菜单：锚定 ＋ 按钮右上方（Trae 风格），替代原底部滑动面板
