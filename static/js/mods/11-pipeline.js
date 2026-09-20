/* 执行流水线：阶段 / 工具 / 子任务 / DAG
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 5203-5710  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function resetPipeline(){ procReset(); }
// SSE stage 事件 → 管线阶段（内联渲染进 proc-box，随会话进程展示）
function onStageEvent(ev){
  const st = ev.status, name = ev.name;
  if(name === '意图识别'){
    if(st === 'run'){ resetPipeline(); procAddStage('意图识别','run','正在识别意图…'); }
    else { procAddStage('意图识别','done', ev.intent ? `→ ${ev.agent||ev.intent}（HIL ${ev.hil_level||'-'}）` : ''); }
  } else if(name === '知识库检索'){
    if(st === 'run'){ procAddStage('知识库检索','run', ev.retrieved===false ? '按需检索未触发（纯问答）' : '图谱 / 向量检索中…'); }
    else {
      // 2026-09-01 ArcR-5 来源徽章：图谱 / 图谱+向量 / 向量（悬浮显示路由原因）
      const _srcMap = {graph:['图谱','#185FA5'], mixed:['图谱+向量','#534AB7'], vector:['向量','#BA7517']};
      let det = ev.retrieved===false ? '未引用' : '';
      if(ev.route && _srcMap[ev.route]){
        const _lab = _srcMap[ev.route][0], _col = _srcMap[ev.route][1];
        det = `<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;color:#fff;background:${_col};" title="${esc(ev.route_reason||'')}">${_lab}</span>`;
      }
      procAddStage('知识库检索','done', det);
    }
  } else if(name === '生成与校验'){
    if(st === 'run'){ procAddStage('生成与校验','run','正在流式生成…'); }
    else { procAddStage('生成与校验','done',''); }
  } else if(name === '写入会话'){
    if(st === 'run'){ procAddStage('写入会话','run'); }
    else { procAddStage('写入会话','done',''); }
  }
}

// ── V2.4 会话内执行过程（Trae 式时间线）：按事件到达顺序依次展示，进行中展开、完成自动收起 ──
let _procS = {timeline: [], thinking: '', thinkingRounds: {}, thinkingOrder: [], dag: {nodes:{}, order:[]}, dagDirty: false,
              startTime: null, endTime: null, summaryCollapsed: false, done: false, tokens: '', subChildren: {}, keyMarked: {}};
function toggleProc(headEl){
  const blk = headEl.closest('.proc-block');
  if(!blk) return;
  blk.classList.toggle('collapsed');
  // P0-1 展开时补渲染 DAG（收起时容器隐藏不可布局；展开后重建并 fit）
  if(!blk.classList.contains('collapsed')){
    const viz = blk.querySelector('.dag-viz');
    if(viz){
      if(viz.id === 'proc-dag-live' && _procS.dag && _procS.dag.order.length) dagBuild('proc-dag-live', _procS.dag);
      else {
        const raw = viz.getAttribute('data-dag');
        if(raw){ try{ dagBuild(viz.id, JSON.parse(decodeURIComponent(raw))); }catch(e){} }
      }
    }
  }
}
function procReset(){
  _procS = {timeline: [], thinking: '', thinkingRounds: {}, thinkingOrder: [], dag: {nodes:{}, order:[]}, dagDirty: false,
            startTime: Date.now(), endTime: null, summaryCollapsed: false, done: false, tokens: '', subChildren: {}, keyMarked: {}};
  const b = document.getElementById('proc-box'); if(b) b.innerHTML='';
}
// 按 id 更新或追加时间线步骤；status 离开 run（完成/失败）时自动收起，进行中保持展开
function procUpsert(id, patch){
  let s = _procS.timeline.find(x=>x.id===id);
  if(!s){ s = Object.assign({id, type:'', icon:'•', title:'', sub:'', status:'run', statusText:'执行中…', detail:'', collapsed:false}, patch); _procS.timeline.push(s); }
  else { Object.assign(s, patch); }
  // P0-1：进行中步骤保持展开；完成/失败自动收起（编排 DAG 块新任务启动时重新展开）
  if(s.status !== 'run') s.collapsed = true;
  else s.collapsed = false;
  procRender();
}
// V2.6 统一收口条：执行中显示 spinner+动态耗时；完成后「✓ 已完成 · N 环节 · 耗时」；点击展开/收起时间线
function procSummaryToggle(){
  _procS.summaryCollapsed = !_procS.summaryCollapsed;
  procRender();
}
function _procElapsedText(){
  const end = _procS.endTime || Date.now();
  const sec = Math.max(0, (end - (_procS.startTime || end)) / 1000);
  return sec >= 60 ? `${Math.floor(sec/60)}m${Math.round(sec%60)}s` : `${sec.toFixed(1)}s`;
}
function procRender(){
  const b = document.getElementById('proc-box'); if(!b) return;
  // ── 收口条（V2.6）：时间线有内容才显示 ──
  let h = '';
  if(_procS.timeline.length){
    const runN = _procS.timeline.filter(x=>x.status==='run').length;
    const failN = _procS.timeline.filter(x=>x.status==='failed').length;
    const running = !_procS.endTime && runN >= 0 && !_procS.done;   // 流式未结束
    const tokenTxt = _procS.tokens ? ` · ⚡ ${_procS.tokens}` : '';
    if(running){
      h += `<div class="proc-summary" onclick="procSummaryToggle()"><span class="proc-spin"></span>执行中… ${_procS.timeline.length} 个环节 · <span class="proc-sum-elapsed">${_procS.elapsedText||_procElapsedText()}</span>${failN?` · ${failN} 失败`:''}<span class="chev">▾</span></div>`;
      ensureElapsedTimer();
    } else {
      h += `<div class="proc-summary ${_procS.summaryCollapsed?'collapsed':''}" onclick="procSummaryToggle()">✓ 已完成 · ${_procS.timeline.length} 环节 · 耗时 ${_procS.elapsedText||_procElapsedText()}${tokenTxt}${failN?` · ${failN} 失败`:''}<span class="chev">▾</span></div>`;
    }
  }
  const hideBody = _procS.done && _procS.summaryCollapsed;
  if(!hideBody){
    _procS.timeline.forEach(s=>{
      const run = s.status==='run';
      const statusCls = run ? 'w' : (s.status==='failed' ? 'r' : 'ok');
      const sub = s.sub ? `<span class="proc-sub">${esc(s.sub)}</span>` : '';
      const elapsed = s.elapsedStart ? `<span class="tool-elapsed" data-start="${s.elapsedStart}">0.0s</span>`
        : ((s.elapsedMs!==undefined && s.elapsedMs!==null && s.elapsedMs>=0) ? `<span class="tool-elapsed done">⏱ ${(s.elapsedMs/1000).toFixed(1)}s</span>` : '');
      // V2.6 动态效果：运行中条目 running 类（呼吸边框）+ 状态徽章前 spinner
      const spin = run ? '<span class="proc-spin"></span>' : '';
      h += `<div class="proc-block ${s.type}${run?' running':''}${s.collapsed?' collapsed':''}">
        <div class="proc-head" onclick="toggleProc(this)"><span>${s.icon}</span><span class="proc-title">${esc(s.title)}${sub}</span>${elapsed}<span class="st ${statusCls}" style="margin-left:auto;">${spin}${esc(s.statusText)}</span><span class="chev">▾</span></div>
        ${s.detail?`<div class="proc-body">${s.detail}</div>`:''}
      </div>`;
    });
  }
  b.innerHTML = h;
  // P0-1：编排 DAG 重建——数据更新（dagDirty）或展开态画布被时间线重绘清除（无 canvas）时重建
  if(!hideBody && _procS.dag && _procS.dag.order.length){
    const dagEl = document.getElementById('proc-dag-live');
    const needRebuild = _procS.dagDirty
      || (dagEl && dagEl.clientHeight > 0 && dagEl.querySelectorAll('canvas').length === 0);
    if(needRebuild){ _procS.dagDirty = false; dagBuild('proc-dag-live', _procS.dag); }
  }
  const area = document.getElementById('chat-area');
  if(area) area.scrollTop = area.scrollHeight;
}
function procAddStage(name, status, detail){
  const iconMap = {'意图识别':'🔍','知识库检索':'📚','生成与校验':'✍️','写入会话':'💾'};
  procUpsert('stage_'+name, {type:'stage', icon: iconMap[name]||'🔍', title: name,
    status, statusText: status==='run'?'执行中…':'✓ 完成', sub: detail||''});
  // 生成与校验完成 → 自动收起思考过程（思考没有单独的事件"完成"）
  if(name === '生成与校验' && status === 'done' && _procS.thinking) {
    const s = _procS.timeline.find(x=>x.id==='thinking');
    if(s && s.status === 'run') { s.status = 'done'; s.statusText = '✓ 完成'; s.collapsed = true; procRender(); }
  }
}
function procAddThinking(delta, round, key){
  if(!delta) return;
  round = round || 0;
  if(!_procS.thinkingRounds) _procS.thinkingRounds = {};
  if(!_procS.thinkingOrder) _procS.thinkingOrder = [];
  if(!_procS.keyMarked) _procS.keyMarked = {};
  if(!(round in _procS.thinkingRounds)){ _procS.thinkingRounds[round] = ''; _procS.thinkingOrder.push(round); }
  // V2.6：子任务内部思考标注归属（每个 key 只标一次，避免逐 delta 重复）
  if(key && !_procS.keyMarked[key]){ _procS.thinkingRounds[round] += '【'+key+'】'; _procS.keyMarked[key] = true; }
  _procS.thinkingRounds[round] += delta;
  _procS.thinking = _procS.thinkingOrder.map(r=>_procS.thinkingRounds[r]).join(' ');
  // P1-3：按轮次分组渲染（round>0 显示「第 N 轮」分隔，LLM 无轮次思考归入 round 0）
  const detail = _procS.thinkingOrder.map(r=>{
    const head = r>0 ? `<div class="proc-round">第 ${r} 轮</div>` : '';
    return head + esc(_procS.thinkingRounds[r]);
  }).join('');
  procUpsert('thinking', {type:'thinking', icon:'💭', title:'思考过程', status:'run', statusText:'思考中…', detail});
}
function procAddAgent(ev){
  const name = ev.display_name||ev.name||'-';
  const run = ev.status!=='done';
  procUpsert('agent_'+(ev.name||ev.intent||'agent'), {type:'agent', icon:'🤖', title:'子智能体：'+name,
    status: run?'run':'done', statusText: run?'执行中…':'✓ 已完成'});
}
// V2.5：技能执行环节（SSE type=skill）——命中技能名 + 注入说明，可展开查看
function procAddSkill(ev){
  const names = (ev.names||[]).join('、') || '-';
  procUpsert('skill_'+names, {type:'skill', icon:'🧩', title:'技能执行：'+names,
    status:'done', statusText:'✓ 已注入',
    detail: `<div style="font-size:11.5px;color:var(--mut);line-height:1.6;">${esc(ev.note||'技能指令已注入生成阶段')}</div>` +
      `<div style="font-size:10.5px;color:var(--mut);margin-top:4px;">触发方式：${ev.note&&ev.note.includes('指定')?'手动指定（/ 或 # 选择）':'触发词命中（消息含技能触发词）'}</div>`});
}
// V2.6 历史消息执行过程收口条：点击展开/收起时间线
function histProcToggle(sumEl){
  const wrap = sumEl.closest('.hist-proc');
  if(!wrap) return;
  wrap.classList.toggle('open');
  sumEl.classList.toggle('collapsed');
  // 展开时补渲染惰性 DAG
  if(wrap.classList.contains('open')){
    wrap.querySelectorAll('.dag-viz[data-dag]').forEach(viz=>{
      try{ dagBuild(viz.id, JSON.parse(decodeURIComponent(viz.getAttribute('data-dag')))); }catch(e){}
    });
  }
}
function procAddTool(ev){
  const rawName = ev.name || '';
  // V2.6：编排子任务内部工具（name 带 `t1:` 前缀）→ 归组挂到子任务卡内，不再平铺顶层
  const ci = rawName.indexOf(':');
  if(ci > 0){
    const pk = rawName.slice(0, ci);
    if(_procS.timeline.some(x=>x.id==='subtask_'+pk) || (_procS.dag.order||[]).includes(pk)){
      _procAddSubTool(pk, ev, rawName.slice(ci+1));
      return;
    }
  }
  const run = ev.status==='run';
  // V2.5：同名工具多轮调用——上一轮已完成则另起新条目（保留每轮结果，不再覆盖）
  let upsertId = 'tool_'+ev.name;
  if(run){
    const cur = _procS.timeline.find(x=>x.id===upsertId);
    if(cur && cur.status!=='run') upsertId = upsertId+'_'+Date.now();
  }
  let detail = '';
  if(ev.arguments) detail += `<div class="proc-tool-param">参数：${esc(typeof ev.arguments==='string'?ev.arguments:JSON.stringify(ev.arguments))}</div>`;
  const patch = {type:'tool', icon:'🛠', title:'调用 '+ev.name};
  if(run){
    // P0-1 工具耗时：run 记起始（同名工具重试沿用首次起点），头部徽章实时计时
    if(!_procS.toolStart) _procS.toolStart = {};
    if(!_procS.toolStart[ev.name]) _procS.toolStart[ev.name] = Date.now();
    patch.elapsedStart = _procS.toolStart[ev.name];
    ensureElapsedTimer();
  } else {
    if(_procS.toolStart && _procS.toolStart[ev.name]){
      patch.elapsedMs = Date.now() - _procS.toolStart[ev.name];
      delete _procS.toolStart[ev.name];
    } else if(ev.latency_ms){ patch.elapsedMs = ev.latency_ms; }
    patch.elapsedStart = null;   // P0-1：完成定格——清实时标记，徽章切 ⏱ 定格态
    const res = ev.error || ev.result || '-';
    const trunc = !!ev.truncated;
    // P0-2 三段式错误面：发生了什么 / 为什么 / 下一步（后端可选 why/next 结构化字段，缺省前端推断）
    if(!ev.ok && ev.error){
      const what = String(ev.error).slice(0,120);
      detail += `<div class="errface">
        <div class="ef-row"><span class="ef-k">发生了什么：</span><span class="ef-v">${esc(what)}</span></div>
        <div class="ef-row"><span class="ef-k">为什么：</span><span class="ef-v">${esc(ev.why || _toolErrWhy(ev.error))}</span></div>
        <div class="ef-row"><span class="ef-k">下一步：</span><span class="ef-v">${esc(ev.next || '可复制错误详情，在输入框反馈让 AI 换一种方式继续执行')}</span></div>
        <div class="ef-act"><span class="pr-act" style="color:var(--blue-d);" onclick="copyToolError(this)">📋 复制错误详情</span></div>
      </div>`;
    }
    // P0-2：结果默认截断展示（max-height），超出部分可展开全文 / 复制（truncated 标记由后端透传）
    // V2.7：失败且已有错误面时跳过结果区（错误信息已在 errface 展示，避免重复占位）
    if(!(ev.ok === false && ev.error)){
      detail += `<div class="proc-tool-result ${trunc?'trunc':''}">
        <div class="pr-head"><span>结果</span>${trunc?`<span class="pr-act" onclick="toolExpand(this)">展开全文</span>`:''}<span class="pr-act" onclick="copyToolResult(this)">📋 复制</span></div>
        <div class="pr-body">${esc(String(res))}</div></div>`;
    }
  }
  patch.detail = detail;
  patch.status = run?'run':(ev.ok?'done':'failed');
  patch.statusText = run?'调用中…':(ev.ok?'✓ 成功':'✗ 失败');
  procUpsert(upsertId, patch);
}
// V2.6：子任务内部工具事件归组（挂到 subtask 卡 children，随子任务卡展开查看）
function _procAddSubTool(pk, ev, displayName){
  if(!_procS.subChildren) _procS.subChildren = {};
  const arr = (_procS.subChildren[pk] = _procS.subChildren[pk] || []);
  const run = ev.status==='run';
  let item = arr.find(x=>x.name===ev.name && x.status==='run');
  if(item && !run){
    item.status='done'; item.ok=!!ev.ok;
    item.result=ev.result||''; item.error=ev.error||''; item.truncated=!!ev.truncated;
    item.latencyMs=ev.latency_ms||0; item.elapsedStart=null;
  } else if(!item){
    arr.push({name:ev.name, displayName, arguments:ev.arguments, status: run?'run':'done',
              ok: !!ev.ok, result: ev.result||'', error: ev.error||'',
              truncated: !!ev.truncated, latencyMs: ev.latency_ms||0, elapsedStart: run?Date.now():null});
  }
  // 子任务卡执行明细变化 → 重渲染（保留其展开/收起状态）
  const card = _procS.timeline.find(x=>x.id==='subtask_'+pk);
  if(card){ card.detail = subtaskCardHtml({key:pk, title:card.title, agent:card.agent||'', status:card.status, latency_ms:card.latencyMs||0}) + _subChildrenHtml(pk); }
  procRender();
}
// V2.6：子任务内部执行明细 HTML（工具参数/结果/状态/耗时）
function _subChildrenHtml(key){
  const arr = (_procS.subChildren||{})[key] || [];
  if(!arr.length) return '';
  let h = '<div style="margin-top:6px;border-top:1px dashed var(--line);padding-top:6px;">'
    + '<div style="font-size:10px;color:var(--mut);margin-bottom:4px;font-weight:600;">内部执行（'+arr.length+'）</div>';
  arr.forEach(it=>{
    const run = it.status==='run';
    const stTxt = run ? '调用中…' : (it.ok ? '✓ 成功' : '✗ 失败');
    const stCls = run ? 'w' : (it.ok ? 'ok' : 'r');
    const spin = run ? '<span class="proc-spin"></span>' : '';
    const res = it.error || it.result || '-';
    const trunc = !!it.truncated;
    h += '<div style="margin-bottom:6px;">'
      + '<div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:var(--blue-d,#3478f6);flex-wrap:wrap;">🛠 '+esc(it.displayName||it.name)
      + ' <span class="st '+stCls+'">'+spin+stTxt+'</span>'
      + (it.latencyMs?'<span style="color:var(--mut);font-size:10px;">⏱ '+(it.latencyMs/1000).toFixed(1)+'s</span>':'')
      + '</div>'
      + (it.arguments?'<div class="proc-tool-param">参数：'+esc(typeof it.arguments==='string'?it.arguments:JSON.stringify(it.arguments))+'</div>':'')
      + '<div class="proc-tool-result '+((trunc||String(res).length>200)?'trunc':'')+'"><div class="pr-head"><span>结果</span></div><div class="pr-body">'+esc(String(res))+'</div></div>'
      + '</div>';
  });
  return h + '</div>';
}
// P0-1 工具耗时计时器：运行中工具 500ms 刷新头部徽章 + 汇总条实时耗时，全部完成后自动停止
let _toolElapsedTimer = null;
function ensureElapsedTimer(){
  if(_toolElapsedTimer) return;
  if(!_procS.startTime) _procS.startTime = Date.now();
  _toolElapsedTimer = setInterval(()=>{
    document.querySelectorAll('.tool-elapsed[data-start]').forEach(el=>{
      const st = Number(el.getAttribute('data-start')); if(!st) return;
      el.textContent = ((Date.now()-st)/1000).toFixed(1)+'s';
    });
    // V2.6：汇总条实时耗时（执行中）
    const se = document.querySelector('.proc-summary .proc-sum-elapsed');
    if(se) se.textContent = _procElapsedText();
    if(!_procS.timeline.some(x=>x.status==='run')) stopElapsedTimer();
  }, 500);
}
function stopElapsedTimer(){ if(_toolElapsedTimer){ clearInterval(_toolElapsedTimer); _toolElapsedTimer=null; } }
// P0-2 错误原因推断（无结构化 why 时按错误文本归类，供三段式错误面）
function _toolErrWhy(err){
  const e = String(err||'').toLowerCase();
  if(e.includes('timeout')||e.includes('超时')) return '目标服务或模型响应超时（多为临时性故障）';
  if(e.includes('network')||e.includes('econn')||e.includes('网络')) return '到目标服务的网络连接异常';
  if(e.includes('auth')||e.includes('401')||e.includes('403')||e.includes('权限')) return '访问凭据或权限不足';
  if(e.includes('not found')||e.includes('404')) return '目标资源不存在（ID 或路径可能已变更）';
  return '工具执行返回错误（入参或目标状态可能不满足）';
}
// P0-2 复制错误详情（三段文本）
function copyToolError(btn){
  const ef = btn.closest('.errface'); if(!ef) return;
  const txt = Array.from(ef.querySelectorAll('.ef-row')).map(r=>((r.querySelector('.ef-k')||{}).textContent||'')+' '+((r.querySelector('.ef-v')||{}).textContent||'')).join('\n');
  navigator.clipboard.writeText(txt).then(()=>toast('已复制错误详情')).catch(()=>toast('复制失败'));
}
// P0-2：工具结果展开/收起 + 复制
function toolExpand(btn){
  const pr = btn.closest('.proc-tool-result'); if(!pr) return;
  const open = pr.classList.toggle('open');
  btn.textContent = open ? '收起' : '展开全文';
}
function copyToolResult(btn){
  const pr = btn.closest('.proc-tool-result');
  const body = pr ? (pr.querySelector('.pr-body')?.textContent||'') : '';
  navigator.clipboard.writeText(body).then(()=>toast('已复制工具结果')).catch(()=>toast('复制失败'));
}
// Task 14：multi_intent 事件 → 阶段序列（仅首个保留，避免重复渲染同一次分解）
function procAddMultiIntent(ev){
  if(!ev || !(ev.sequence||[]).length) return;
  if(_procS.timeline.find(x=>x.id==='multi')) return;
  const seq = ev.sequence, raw = ev.raw_subtasks||[];
  const detail = '<div style="display:flex;flex-wrap:wrap;gap:4px;">' +
    seq.map((s,i)=>`<span class="hil l0" style="font-size:10px;background:var(--blue-l);">${i+1}. ${esc(raw[i]||s)}<span style="color:var(--mut);"> → </span>${esc(s)}</span>`).join('') + '</div>';
  _procS.timeline.push({id:'multi', type:'multi', icon:'🧩', title:`多意图分解（${seq.length} 个阶段）`,
    status:'done', statusText:`${seq.length} 段`, detail, collapsed:true});
  procRender();
}
// Task 14：子任务专长标签（从 Agent 缓存匹配 name/display_name，无缓存则静默无标签）
function capTagsFor(agentId){
  if(!agentId) return '';
  const a = (_agentsCache||[]).find(x=>x.name===agentId || x.display_name===agentId);
  const caps = (a && a.capabilities) || [];
  if(!caps.length) return '';
  return caps.slice(0,3).map(c=>`<span class="hil l0" style="font-size:9.5px;margin-left:4px;">${esc(c)}</span>`).join('');
}
// Task 14：子任务卡片（状态徽章 done/failed/partial + 专长标签 + 重试标记 + 协议摘要徽章 + 耗时 + 错误）
function subtaskCardHtml(s){
  const run = s.status==='run';
  const ok = s.status==='done';
  const part = s.status==='partial';
  const badge = ok ? '<span class="st ok">done</span>'
    : part ? '<span class="st w">partial</span>'
    : run ? '<span class="st w">run</span>'
    : '<span class="st r">failed</span>';
  const caps = capTagsFor(s.agent);
  // Task 14：自动重试标记（retry_count>0 标黄显示重试次数）
  const retry = (s.retry_count||0) > 0 ? `<span class="st w" title="自动重试 ${s.retry_count} 次后完成">↻ 重试${s.retry_count}</span>` : '';
  // Task 14：协议摘要徽章（summary_status：full/partial/failed → 交付物完整性）
  const ss = s.summary_status;
  const sumBadge = ss==='full' ? '<span class="st ok" style="font-size:9.5px;">交付 full</span>'
    : ss==='partial' ? '<span class="st w" style="font-size:9.5px;">交付 partial</span>'
    : ss==='failed' ? '<span class="st r" style="font-size:9.5px;">交付 failed</span>' : '';
  // 摘要文本：后端可传 dict（协议 summary.summary）或旧字符串
  const sumText = (s.summary && typeof s.summary === 'object') ? (s.summary.summary||'') : (s.summary||'');
  const sum = sumText ? `<div style="color:var(--mut);font-size:10.5px;margin-top:2px;">${esc(String(sumText).slice(0,60))}</div>` : '';
  // P1-2：子任务 token 消耗（后端 subtask 事件 token_count 透传）
  const tk = (s.token_count||0) > 0 ? ` <span style="color:var(--mut);font-size:10.5px;" title="本子任务 token 消耗">🪙 ${Number(s.token_count).toLocaleString()}</span>` : '';
  return `<div class="proc-line" style="border:1px solid var(--line);border-radius:8px;padding:5px 8px;margin:3px 0;background:#fff;">
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">${ok?'✓':part?'◐':run?'⟳':'✗'} <b>${esc(s.title||s.key||'')}</b>${badge}${s.agent?` <span class="tag" style="font-size:10px;">${esc(s.agent)}</span>`:''}${caps}${retry}${sumBadge}${s.latency_ms?` <span style="color:var(--mut);font-size:10.5px;">⏱ ${(s.latency_ms/1000).toFixed(1)}s</span>`:''}${tk}${run?' <span class="st w">执行中…</span>':''}</div>
    ${sum}${s.error?`<div style="color:var(--red);font-size:10.5px;margin-top:2px;">${esc(s.error)}</div>`:''}</div>`;
}
// P0-1：编排 DAG 轨迹图（Cytoscape + dagre，节点=子任务，边=deps 依赖，按状态着色）
function dagBuild(containerId, data){
  if(!window.cytoscape || !data || !containerId) return;
  const el = document.getElementById(containerId);
  if(!el || el.clientWidth === 0 || el.clientHeight === 0) return;   // 容器隐藏（折叠态）跳过，展开时由 toggleProc 补渲染
  if(_cyInstances[containerId]){ try{ _cyInstances[containerId].destroy(); }catch(e){} delete _cyInstances[containerId]; }
  // v6.4 P0：elements 构造 + cytoscape init 整体推到 idle（dagre 1555ms+init 212ms 拆两帧执行）
  window.__cyIdleMount(el, ()=>{
    const nodes = [], edges = [];
    (data.order || Object.keys(data.nodes||{})).forEach(k=>{
      const n = (data.nodes||{})[k]; if(!n) return;
      nodes.push({data:{id:k, label:(n.title||k).slice(0,18)}, classes:(n.status==='done'?'d-ok':n.status==='failed'?'d-err':n.status==='partial'?'d-warn':'d-run')});
      (n.deps||[]).forEach(d=>{ if((data.nodes||{})[d]) edges.push({data:{id:'e_'+d+'_'+k, source:d, target:k}}); });
    });
    if(!nodes.length) return null;
    const cy = cytoscape({
      container: el,
      elements: {nodes, edges},
      style: [
        {selector:'node', style:{'content':'data(label)','background-color':'#E6F1FB','border-color':'#185FA5','border-width':1.5,'color':'#1F2D3D','font-size':10.5,'text-valign':'center','text-halign':'center','text-wrap':'wrap','text-max-width':100,'width':116,'height':40,'shape':'round-rectangle'}},
        {selector:'.d-run', style:{'background-color':'#E8F1FF','border-color':'#3478f6','border-width':2.2}},
        {selector:'.d-ok', style:{'background-color':'#EAF3DE','border-color':'#3B6D11'}},
        {selector:'.d-err', style:{'background-color':'#FDF0F0','border-color':'#A32D2D'}},
        {selector:'.d-warn', style:{'background-color':'#FFF7E0','border-color':'#BA7517'}},
        {selector:'edge', style:{'curve-style':'bezier','width':1.4,'line-color':'#9aa7b5','target-arrow-shape':'triangle','target-arrow-color':'#9aa7b5'}}
      ]
    });
    _cyInstances[containerId] = cy;
    // v6.3 P0 性能：layout.run + cy.fit 推迟到第二次 idle（dagre 是 CPU 重活，避免同帧 longtask 累计）
    window.__cyIdleRun(()=>{
      try{ cy.layout({name:'dagre', animate:false, rankDir:'TB', nodeSep:16, rankSep:34, padding:6}).run(); }
      catch(e){ try{ cy.layout({name:'grid', animate:false}).run(); }catch(e2){} }
      try{ cy.fit(undefined, 24); }catch(e){}
    });
    return cy;
  }, null);
}
// P0-2：编排子任务轨迹（subtask 事件 → DAG 数据 + 状态/耗时/错误实时刷新）
function procAddSubtask(ev){
  const key = ev.key||'t';
  const cur = _procS.timeline.find(x=>x.id==='subtask_'+key) || {};
  const s = {key, title: ev.title||cur.title||key, agent: ev.agent||cur.agent||'',
    deps: ev.deps||cur.deps||[], status: ev.status, latency_ms: ev.latency_ms||cur.latency_ms||0,
    error: ev.error||cur.error||'', summary: ev.summary||cur.summary||'',
    summary_status: ev.summary_status||cur.summary_status||'', retry_count: ev.retry_count||cur.retry_count||0,
    token_count: ev.token_count||cur.token_count||0};
  // 计划清单数据维护（V2.7 文本清单替代 DAG 画布；deps 驱动顺序展示）
  _procS.dag.nodes[key] = {title: s.title, status: s.status, deps: s.deps, latency_ms: s.latency_ms, agent: s.agent};
  if(!_procS.dag.order.includes(key)) _procS.dag.order.push(key);
  _procS.dagDirty = true;
  const all = _procS.dag.order.map(k=>_procS.dag.nodes[k]);
  const runN = all.filter(n=>n.status==='run').length;
  const doneN = all.filter(n=>n.status==='done').length;
  const failN = all.filter(n=>n.status==='failed').length;
  // V2.7 文本计划清单：每行 状态 + 任务标题 + Agent + 耗时 + 依赖
  const planList = '<div style="display:flex;flex-direction:column;gap:5px;padding:2px 0;">' +
    _procS.dag.order.map((k,i)=>{
      const n = _procS.dag.nodes[k]||{};
      const run = n.status==='run';
      const stTxt = run ? '执行中' : (n.status==='failed' ? '失败' : (n.status==='partial' ? '部分完成' : '完成'));
      const stCls = run ? 'w' : (n.status==='failed' ? 'r' : 'ok');
      const spin = run ? '<span class="proc-spin"></span>' : '';
      const dep = (n.deps&&n.deps.length) ? '<span style="color:var(--mut);font-size:10px;">依赖: '+esc(n.deps.join('、'))+'</span>' : '';
      const lat = n.latency_ms ? '<span style="color:var(--mut);font-size:10px;">⏱ '+(n.latency_ms/1000).toFixed(1)+'s</span>' : '';
      return '<div style="display:flex;align-items:center;gap:6px;font-size:11px;flex-wrap:wrap;">'
        + '<span style="color:var(--mut);font-size:10px;min-width:16px;">'+(i+1)+'.</span>'
        + '<span class="st '+stCls+'" style="min-width:56px;text-align:center;">'+spin+stTxt+'</span>'
        + '<b style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+esc(n.title||k)+'</b>'
        + (n.agent?'<span style="color:var(--mut);font-size:10px;">'+esc(n.agent)+'</span>':'')
        + lat + dep + '</div>';
    }).join('') + '</div>';
  procUpsert('plan', {type:'plan', icon:'📋', title:`计划清单（${all.length} 任务）`, status: runN?'run':'done',
    statusText: runN?`执行中 ${runN}/${all.length}`:`${doneN} 完成${failN?` / ${failN} 失败`:''}`, detail: planList});
  const run = s.status==='run';
  const statusText = run ? '执行中…' : (s.status==='failed' ? '✗ 失败' : (s.status==='partial' ? '◐ 部分完成' : '✓ 完成'));
  // V2.6：子任务卡 = 汇总信息 + 内部执行明细（工具参数/结果，嵌套层级查看）
  procUpsert('subtask_'+key, {type:'subtask', icon:'🕸', title: s.title||key, status: s.status,
    statusText, key,
    detail: subtaskCardHtml(s) + _subChildrenHtml(key)});
}

// V2.3.2 流式输出：解析 SSE 事件行（event:/data:）
// ── P0-3 流式渲染合帧（2026-09-17）──
// 原实现每个 token 事件都 `bodyEl.textContent += delta` 后立刻读 `scrollHeight`，
// 每次都触发一次强制同步布局（reflow）；高事件密度下（短文本加速排空可达数百事件/秒）
// 会把主线程打满，反而让输出看起来一顿一顿。
// 改为「缓冲增量 + requestAnimationFrame 每帧 flush 一次」：
// 同一帧内的所有 token 合并为一次 textContent 写入 + 一次滚动赋值。
let _tokBuf = '';
let _tokRaf = 0;
let _tokEl = null;
function _flushTokens(){
  _tokRaf = 0;
  const txt = _tokBuf; _tokBuf = '';
  if(!txt) return;
  if(_tokEl && _tokEl.isConnected){
    _tokEl.textContent += txt;
    const ca = document.getElementById('chat-area');
    if(ca) ca.scrollTop = ca.scrollHeight;
  }
}
// 强制同步 flush：流结束 / 出错 / 澄清卡插入 / 用户中断前必须调用，
// 否则最后一帧缓冲会丢（且 finalizeStopped 读到的是未刷新前的 textContent）。
function _forceFlushTokens(){
  if(_tokRaf){ cancelAnimationFrame(_tokRaf); _tokRaf = 0; }
  _flushTokens();
}

function handleSSE(raw) {
  let evType = 'message', dataStr = '';
  raw.split('\n').forEach(line=>{
    if(line.startsWith('event:')) evType = line.slice(6).trim();
    else if(line.startsWith('data:')) dataStr += line.slice(5).trim();
  });
  if(!dataStr) return;
  let ev;
  try { ev = JSON.parse(dataStr); } catch(e) { return; }
  const aiBox = document.getElementById('stream-ai');
  if(!aiBox) return;
  const bodyEl = aiBox.querySelector('.body');
  if(evType === 'stage') {
    onStageEvent(ev);
  } else if(evType === 'agent') {
    procAddAgent(ev);      // V2.4 会话内执行过程：子智能体（内联会话流）
  } else if(evType === 'reasoning') {
    procAddThinking(ev.delta || '', ev.round, ev.key);   // V2.4 思考流（V2.6：带子任务归属标记）
  } else if(evType === 'tool') {
    procAddTool(ev);       // V2.4 会话内执行过程：工具调用结果
  } else if(evType === 'subtask') {
    procAddSubtask(ev);    // P0-2：编排子任务轨迹（实时 DAG 状态）
  } else if(evType === 'skill') {
    procAddSkill(ev);      // V2.5：技能执行环节（命中技能/指令注入可见）
  } else if(evType === 'multi_intent') {
    procAddMultiIntent(ev);  // Task 14：多意图分解 → 阶段序列 chips
  } else if(evType === 'token') {
    if(bodyEl.querySelector('.typing')) bodyEl.querySelector('.typing').remove();
    // P0-3：合帧渲染（详见 _flushTokens 注释）——不再每 token 写 DOM + 读 scrollHeight
    _tokEl = bodyEl;
    _tokBuf += ev.delta || '';
    // 兜底：后台标签页 rAF 不触发，缓冲不能无限增长 → 超阈值直接同步 flush
    if(_tokBuf.length >= 2000) _forceFlushTokens();
    else if(!_tokRaf) _tokRaf = requestAnimationFrame(_flushTokens);
  } else if(evType === 'clarify') {
    _forceFlushTokens();
    renderClarify(ev);   // P0-1 置信度三级决策：中置信 → 澄清条（可改选重发）
  } else if(evType === 'clarify_ask') {
    _forceFlushTokens();
    renderClarifyAsk(ev);   // 内容级澄清：信息不清晰 → 选择题确认（回答后续答）
  } else if(evType === 'done') {
    _forceFlushTokens();
    finishStream(ev.data, aiBox);
  } else if(evType === 'error') {
    _forceFlushTokens();
    showStopBtn(false); _streaming = false;
    // V2.6：中断也收口（汇总条切「已中断」，不停留"执行中"）
    try{
      _procS.done = true; _procS.endTime = Date.now();
      _procS.timeline.forEach(x=>{ if(x.status==='run'){ x.status='failed'; x.statusText='✗ 中断'; } });
      stopElapsedTimer(); procRender();
    }catch(e){}
    bodyEl.innerHTML = `<span style="color:var(--red);">调用失败：${esc(ev.message||'未知错误')}</span>`;
    const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = '调用失败';
  }
}

// ── P0-1 置信度三级决策：中置信意图 → 澄清条（已按当前理解继续执行，可点选改选重发）──
function renderClarify(ev){
  const aiBox = document.getElementById('stream-ai');
  if(!aiBox || document.getElementById('clarify-bar')) return;
  const inner = aiBox.querySelector('.msg-inner');
  if(!inner) return;
  const pct = Math.round((ev.confidence||0) * 100);
  const _det = ev.detected ? `<b>识别为「${esc(ev.detected)}」</b>，` : '';
  const btns = (ev.candidates||[]).slice(0,4).map(c =>
    `<button onclick="pickIntent(this.dataset.m, this.dataset.i)" data-m="${esc(ev.message)}" data-i="${esc(c)}"
      style="border:1px solid var(--line);background:#fff;color:var(--pri,#3478f6);border-radius:4px;padding:1px 6px;font-size:11px;cursor:pointer;margin:2px 4px 0 0;">改选：${esc(c)}</button>`).join('');
  const bar = document.createElement('div');
  bar.id = 'clarify-bar';
  bar.style.cssText = 'border:1px dashed var(--line);background:#f7f8fa;border-radius:6px;padding:6px 8px;font-size:11px;color:var(--mut);margin-bottom:6px;';
  bar.innerHTML = `⚠ 意图置信度较低（${pct}%），${_det}已按「${esc(ev.intent)}」继续执行。理解有误？${btns}`;
  inner.insertBefore(bar, inner.firstChild);
  const st = document.getElementById('chat-status');
  if(st) st.textContent = `意图置信度 ${pct}% · 已按「${ev.intent}」继续（可点选改选重发）`;
}
// 澄清改选：切智能体下拉 + 恢复原文 → 重发（forced_intent 跳过识别直接定向）
async function pickIntent(msg, intent){
  const qa = document.getElementById('quick-agent');
  if(qa){
    // 下拉懒加载：先补 option 再设值——select.value 赋不存在的值会被浏览器重置为空串
    if(![...qa.options].some(o=>o.value===intent)) qa.add(new Option(intent, intent));
    qa.value = intent;
    qa.dispatchEvent(new Event('change'));   // V2.7：程序赋值也记录上次选择
  }
  const inp = document.getElementById('chat-input');
  if(inp) inp.value = msg;
  sendChat();
}

// ── 内容级澄清（clarify_ask）：信息不清晰 → 选择题确认（优先选择题，支持补充输入），回答后续答 ──
function renderClarifyAsk(ev, host){
  const aiBox = document.getElementById('stream-ai');
  const bodyEl = host || (aiBox ? aiBox.querySelector('.body') : null);
  if(!bodyEl || document.getElementById('clarify-ask')) return;
  const qs = ev.questions || [];
  if(!qs.length) return;
  const card = document.createElement('div');
  card.id = 'clarify-ask';
  card.style.cssText = 'border:1px solid #d3e3fb;background:#f0f6ff;border-radius:8px;padding:10px 12px;font-size:12px;margin-top:10px;';
  card.innerHTML = `<b style="color:var(--blue-d);">❓ 需要确认建模信息</b>
    <div id="clarify-qlist" style="margin-top:8px;display:flex;flex-direction:column;gap:10px;">
      ${qs.map((q,qi)=>`
        <div class="clarify-q">
          <div style="margin-bottom:4px;"><b>${qi+1}. ${esc(q.question||'')}</b>${q.allow_custom?' <span style="color:var(--mut);font-size:10px;">（可补充输入）</span>':''}</div>
          <div class="clarify-opts" style="display:flex;flex-direction:column;gap:3px;padding-left:4px;">
            ${(q.options||[]).map(o=>`<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11.5px;"><input type="radio" name="cq_${esc(q.id)}" value="${esc(o)}" style="width:auto;">${esc(o)}</label>`).join('')}
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11.5px;"><input type="radio" name="cq_${esc(q.id)}" value="__custom__" style="width:auto;">其他… <input class="cq-custom" data-qid="${esc(q.id)}" placeholder="输入补充信息" style="border:1px solid var(--line);border-radius:4px;padding:2px 6px;font-size:11px;margin-left:4px;flex:1;display:none;"></label>
          </div>
        </div>`).join('')}
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <button class="btn grn sm" onclick="clarifyAnswerSend()">✉ 发送补充信息</button>
      <button class="btn ghost sm" onclick="clarifySkipSend()">跳过（按现状继续）</button>
      <span style="color:var(--mut);font-size:10.5px;">回答后将以此继续建模</span>
    </div>`;
  bodyEl.appendChild(card);
  card.querySelectorAll('input[type=radio]').forEach(rd=>{
    rd.addEventListener('change', ()=>{
      const c = card.querySelector('.cq-custom[data-qid="' + rd.name.slice(3) + '"]');
      if(c) c.style.display = (rd.value==='__custom__') ? 'block' : 'none';
    });
  });
  const area = document.getElementById('chat-area');
  if(area) area.scrollTop = area.scrollHeight;
}
// 提交澄清答案 → 构造续答文本 → 作为新消息发送（走正常流式续答）
async function clarifyAnswerSend(){
  const card = document.getElementById('clarify-ask');
  if(!card) return;
  const answers = [];
  let pending = false;
  card.querySelectorAll('.clarify-q').forEach(qEl=>{
    const custom = qEl.querySelector('.cq-custom');
    const qid = custom ? custom.dataset.qid : '';
    const sel = qEl.querySelector('input[type=radio]:checked');
    let value = sel ? sel.value : '';
    if(value === '__custom__'){
      value = (custom && custom.value || '').trim();
      if(!value){ toast('请先输入补充信息'); pending = true; return; }
    }
    if(value) answers.push({question_id: qid, value});
  });
  if(pending) return;
  if(!answers.length){ toast('请先选择一个选项或输入补充信息'); return; }
  const btn = card.querySelector('button[onclick="clarifyAnswerSend()"]');
  if(btn){ btn.disabled = true; btn.textContent = '提交中…'; }
  try{
    const r = await api('/api/conversations/' + currentConvId + '/clarify-answer', {method:'POST', body:JSON.stringify({answers})});
    if(r && r.error){ toast(r.error); if(btn){ btn.disabled=false; btn.textContent='✉ 发送补充信息'; } return; }
    card.style.opacity = 0.55;
    const note = document.createElement('div');
    note.style.cssText = 'font-size:10.5px;color:var(--grn);margin-top:6px;';
    note.textContent = '✓ 已提交，正在继续…';
    card.appendChild(note);
    sendResume(r.resume_text);
  }catch(e){ toast('提交失败：' + (e.message||'')); if(btn){ btn.disabled=false; btn.textContent='✉ 发送补充信息'; } }
}
// 跳过澄清：按现有信息继续（含澄清标记，agent 侧不再提问，防循环）
function clarifySkipSend(){
  const card = document.getElementById('clarify-ask');
  if(!card) return;
  card.style.opacity = 0.55;
  const note = document.createElement('div');
  note.style.cssText = 'font-size:10.5px;color:var(--mut);margin-top:6px;';
  note.textContent = '已跳过澄清，按现有信息继续…';
  card.appendChild(note);
  sendResume('【澄清补充】用户选择跳过澄清，请按现有信息直接继续执行，不要再次提问。');
}
// 续答流式请求（复用 chat/stream；user 消息显示简短摘要，完整补充见澄清卡）
async function sendResume(text){
  if(!currentConvId || !text) return;
  if(_streamAbort) _streamAbort.abort();
  const myAbort = new AbortController();
  _streamAbort = myAbort;
  _streaming = true; _stoppedManually = false;
  showStopBtn(true);
  const area = document.getElementById('chat-area');
  area.innerHTML += renderMessage({role:'user', content:'📎 已补充建模信息，请继续'});
  const aiBox = document.createElement('div');
  aiBox.className = 'msg ai';
  aiBox.id = 'stream-ai';
  aiBox.innerHTML = `<span class="who">AI</span><div class="msg-inner"><div class="proc" id="proc-box"></div><div class="body streaming"><span class="typing"></span></div></div>`;
  area.appendChild(aiBox);
  area.scrollTop = area.scrollHeight;
  const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = '正在继续执行…';
  resetPipeline();
  try{
    const resp = await fetch(`/api/conversations/${currentConvId}/chat/stream`, {
      method:'POST', signal: myAbort.signal,
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message:text, attachments:[], branch:getCurrentBranch()})});
    if(!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) !== -1){
        handleSSE(buf.slice(0, idx));
        buf = buf.slice(idx+2);
      }
    }
    _forceFlushTokens();   // P0-3：流结束前清空合帧缓冲（含中断路径），防末帧丢失
    area.scrollTop = area.scrollHeight;
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
  }catch(e){
    _forceFlushTokens();
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
    if(e && e.name === 'AbortError'){ finalizeStopped(aiBox); return; }
    const bodyEl = aiBox.querySelector('.body');
    bodyEl.innerHTML = `<span style="color:var(--red);">调用失败：${esc(e.message)}</span>`;
    const _cs2 = document.getElementById('chat-status'); if(_cs2) _cs2.textContent = '调用失败';
  }
}

// ── 快捷引用标签（V3：# 前缀插入知识库标签）──
