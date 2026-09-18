/* SysML 视图卡 / 图例
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 3569-3728  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function openSvmPreview(el) {
  const vm = svmVmFromEl(el);
  if(!vm) return;
  const vname = (vm.view && vm.view.name) || 'SysML 视图';
  openPreviewTab({id:null, kind:'sysml', kind_label:'SysML', title:`${vname}（${(vm.view&&vm.view.v1)||''}）`,
    preview_type:'sysml', preview_content:'', message_id:null,
    meta:{sysml_views:{views:{[vname]:vm}}}});
}

/* 需求分析卡：候选条目 + 冲突（FR-HIL-1 逐条确认） */
function cardCandidates(cd) {
  const cands = cd.candidates||[];
  const confs = cd.conflicts||[];
  let items = cands.slice(0,4).map((c,i)=>`
    <div class="rc-item"><span class="st b">#${i+1}</span><span class="txt" title="${c.text||''}">${c.text||''}</span><span class="meta">来源：${c.source||'-'} · 分 ${c.score||0}</span></div>`).join('');
  if(cands.length>4) items += `<div class="rc-item" style="justify-content:center;color:var(--mut);">… 其余 ${cands.length-4} 条见完整报告</div>`;
  let confHtml = '';
  if(confs.length){
    confHtml = `<div class="rc-warn">⚠ 检测到 ${confs.length} 项冲突：${confs.slice(0,2).map(c=>`${c.new} ↔ ${c.existing}`).join('；')}${confs.length>2?' 等':''}</div>`;
  }
  return `
    <div class="rc-chip b">📋 需求分析</div>
    <div class="rc-mini">
      <span class="rc-chip b"><span class="n">${cands.length}</span> 条候选条目</span>
      <span class="rc-chip ${confs.length?'r':'g'}">${confs.length?'⚠':'✓'} ${confs.length?confs.length+' 项冲突':'无冲突'}</span>
    </div>
    <div class="rc-list">${items||'<div style="color:var(--mut);font-size:12px;">暂无候选条目</div>'}</div>
    ${confHtml}
    <button class="btn sm ghost" style="margin-top:8px;" onclick="openPreviewReport('需求分析 · 完整报告', panelCandidates(${JSON.stringify(cd).replace(/"/g,'&quot;')}))">📄 查看完整报告</button>`;
}

/* 变更影响分析卡：变更源 + 直接/间接 + 影响度分级 + 传播链 + 参数重算（FR-CIA-1~4） */
function cardImpact(cd) {
  const src = cd.change_source;
  const nodes = cd.impact_nodes||[];
  const lv = cd.impact_levels||{};
  const chain = nodes.slice(0,6).map((n,i)=>{
    const cls = n.impact==='source'?'src':(n.impact==='direct'?'':'ind');
    return `<span class="rc-gnode ${cls}"><span class="d">${n.depth||i+1}</span>${esc(n.name||('节点'+(i+1)))}</span>`;
  }).join('<span class="rc-arrow">→</span>');
  const warn = cd.warning ? `<div class="rc-warn">⚠ ${cd.warning}</div>` : '';
  const dirTxt = cd.direction==='up'?'向上游':cd.direction==='down'?'向下游':'双向';
  return `
    <div class="rc-chip b">🔀 变更影响分析</div>
    <div class="rc-mini">
      <span class="rc-chip r"><span class="n">${cd.direct_count||0}</span> 直接影响</span>
      <span class="rc-chip a"><span class="n">${cd.indirect_count||0}</span> 间接影响</span>
      <span class="rc-chip g">深度 ${cd.depth||1} 层 · ${dirTxt}</span>
    </div>
    <div class="rc-mini" style="margin-top:2px;">
      <span class="rc-chip r"><span class="n">${lv.high||0}</span> 高影响</span>
      <span class="rc-chip w"><span class="n">${lv.mid||0}</span> 中影响</span>
      <span class="rc-chip g"><span class="n">${lv.low||0}</span> 低影响</span>
      <span style="font-size:11px;color:var(--mut);">影响程度 · 关系权重×层衰减</span>
    </div>
    ${src?`<div style="margin-top:6px;font-size:12px;"><b>变更源：</b>${esc(src.name)} <span class="tag">${esc(src.entity_type)}</span> <span class="tag">${esc(src.status)}</span></div>`:''}
    <div class="rc-graph">${chain||'<span style="color:var(--mut);font-size:12px;">暂无可视化传播链</span>'}</div>
    ${warn}
    <div class="rc-mini" style="margin-top:8px;flex-wrap:wrap;gap:6px;">
      <label style="display:flex;align-items:center;gap:4px;font-size:11.5px;color:var(--mut);">深度
        <select style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;"
          onchange="impactReRun('${esc((src&&src.name)||'')}', this.value, '${cd.direction||'both'}')">
          ${[1,2,3,5,0].map(d=>`<option value="${d}" ${(cd.depth||3)===d?'selected':''}>${d===0?'全部':d+' 层'}</option>`).join('')}
        </select></label>
      <label style="display:flex;align-items:center;gap:4px;font-size:11.5px;color:var(--mut);">方向
        <select style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;"
          onchange="impactReRun('${esc((src&&src.name)||'')}', ${cd.depth||3}, this.value)">
          <option value="both" ${cd.direction!=='up'&&cd.direction!=='down'?'selected':''}>双向</option>
          <option value="up" ${cd.direction==='up'?'selected':''}>上游</option>
          <option value="down" ${cd.direction==='down'?'selected':''}>下游</option>
        </select></label>
      <button class="btn sm ghost" onclick="openImpactReport(${JSON.stringify(cd).replace(/"/g,'&quot;')})">🗺 完整报告（图谱）</button>
      <button class="btn sm ghost" onclick="openSimPanel()">🛠 变更模拟</button>
    </div>`;
}

/* CIA 异常引导卡：原因+影响+怎么做 三段式 + 引导动作（FR-CIA 场景级错误反馈） */
function cardImpactGuide(cd) {
  const isAmb = cd.code==='SOURCE_AMBIGUOUS';
  const cands = cd.candidates||[];
  let candHtml = '';
  if(isAmb && cands.length){
    candHtml = `<div style="margin-top:8px;">
      <div style="font-size:11px;color:var(--mut);margin-bottom:4px;">匹配到 ${cands.length} 个候选变更源，请选择：</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;">${cands.map(c=>`<button class="btn sm" onclick="impactRunSource('${esc(c.name)}')">${esc(c.name)} <span class="tag">${esc(c.type||'')}</span></button>`).join('')}</div></div>`;
  }
  const actBtns = (cd.actions||[]).map(a=>{
    if(a==='select_source') return `<button class="btn sm" onclick="toast('请在输入框用元素名称重述变更源，或进入知识库图谱选择实体')">从图谱选择变更源</button>`;
    if(a==='retry') return `<button class="btn sm ghost" onclick="document.getElementById('chat-input')&&document.getElementById('chat-input').focus()">换个说法重试</button>`;
    return '';
  }).join('');
  return `
    <div class="rc-chip b">🔀 变更影响分析</div>
    <div class="rc-warn">
      <div><b>⚠ 分析未完成：${esc(cd.reason||'未知原因')}</b></div>
      <div style="margin-top:4px;color:var(--mut);font-size:12px;">${esc(cd.impact||'')}</div>
    </div>
    ${candHtml}
    <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;">${actBtns}</div>`;
}

/* 会话入口重算：按新参数构造一句话重新发起分析（复用对话链路，可追溯） */
function impactReRun(srcName, depth, direction){
  if(!currentConvId || !srcName){ toast('缺少变更源，无法重算'); return; }
  let t = '变更影响分析：' + srcName;
  if(depth) t += '，' + (Number(depth)===0?'全部层':(depth+' 层'));
  if(direction && direction!=='both') t += '，' + (direction==='up'?'上游':'下游');
  const input = document.getElementById('chat-input');
  if(input){ input.value = t; sendChat(); }
}

/* 从候选/直接指定变更源发起分析 */
function impactRunSource(name){
  const input = document.getElementById('chat-input');
  if(!input || !currentConvId){ toast('请先进入会话'); return; }
  input.value = '变更影响分析：' + name;
  sendChat();
}

/* 预评审校验卡：评分环 + 四类问题 + 结论（FR-VR-1/FR-VC-1~5） */
function cardReview(cd) {
  const sc = Math.max(0, Math.min(100, cd.score||0));
  const pct = sc;
  const color = sc>=80?'var(--grn)':sc>=60?'var(--amb)':'var(--red)';
  const issues = cd.issues||[];
  const counts = {语法:cd.syntax_errors||0, 逻辑:cd.logic_issues||0, 规范:cd.spec_deviations||0, 提示:cd.info_hints||0};
  const issueList = issues.slice(0,3).map(i=>`
    <div class="rc-issue"><span class="lv">${i.level==='error'?'🔴':i.level==='warning'?'🟠':'🔵'}</span><span>${i.type}：${i.desc}</span><span class="fix">${i.fix||''}</span></div>`).join('');
  const conclusion = cd.conclusion==='有条件通过'||cd.conclusion==='通过' ? 'rc-okbar' : 'rc-warn';
  return `
    <div class="rc-chip b">✅ 预评审校验</div>
    <div style="display:flex;gap:14px;align-items:center;margin-top:8px;">
      <div class="rc-score" style="--p:${pct};background:conic-gradient(${color} calc(${pct}*1%),#eef1f6 0);"><i><b>${sc}</b><small>分 / 100</small></i></div>
      <div style="flex:1;display:grid;grid-template-columns:1fr 1fr;gap:6px;">
        <span class="rc-chip r"><span class="n">${counts.语法}</span> 语法</span>
        <span class="rc-chip r"><span class="n">${counts.逻辑}</span> 逻辑</span>
        <span class="rc-chip a"><span class="n">${counts.规范}</span> 规范</span>
        <span class="rc-chip b"><span class="n">${counts.提示}</span> 提示</span>
      </div>
    </div>
    <div style="margin-top:8px;">${issueList||'<div style="color:var(--mut);font-size:12px;">未发现问题</div>'}</div>
    <div class="${conclusion}">${cd.conclusion==='不通过'?'✖':'✔'} 评审结论：${cd.conclusion||'-'}</div>
    <button class="btn sm ghost" style="margin-top:8px;" onclick="openPreviewReport('预评审校验 · 完整报告', panelReview(${JSON.stringify(cd).replace(/"/g,'&quot;')}))">📄 查看完整报告</button>`;
}

/* ── 滑出面板完整报告 ── */
// 2026-09-18 S6-1：esc / escA 已迁至 01-core.js（第一个加载的模块）——
// 它们是全站最强隐藏依赖（35 文件约 1,700 处调用），留在本模块会造成加载顺序耦合。
// 此处不再定义，调用方无需改动（仍为全局函数）。
// UTC 时间串（SQLite CURRENT_TIMESTAMP）转本地显示
function fmtLocalTime(s){
  if(!s) return '';
  // 兼容 'YYYY-MM-DD HH:MM:SS'（SQLite UTC）与 ISO
  let iso = s.trim().replace(' ', 'T');
  if(!/[TZ]|\+/.test(iso)) iso += 'Z';
  const d = new Date(iso);
  if(isNaN(d.getTime())) return s;
  const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
