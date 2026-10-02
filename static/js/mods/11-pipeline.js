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
      let det = ev.retrieved===false ? '未引用' : '', detHtml = '';
      if(ev.route && _srcMap[ev.route]){
        const _lab = _srcMap[ev.route][0], _col = _srcMap[ev.route][1];
        // 2026-09-26 修复：徽章本身是 HTML —— 必须走 subHtml。原先进 sub 会被 esc() 转义，
        // 界面上直接把 `<span style=…>` 标签源码显示出来（实测截图反馈）。
        det = '';
        detHtml = `<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;color:#fff;background:${_col};" title="${esc(ev.route_reason||'')}">${_lab}</span>`;
      }
      procAddStage('知识库检索','done', det, detHtml);
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
/** 取执行过程时间线的 HTML：只取 .proc-block，**剔除 live 收口条**。
 *
 * 2026-09-26 修复（用户反馈「各环节都完成了，却还有一个节点显示执行中」）：
 * 收口（done / 停止）时原实现直接 `pb.innerHTML` 快照，会把 live 的收口条一起收进历史时间线，
 * 而该时刻 `_procS.done` 尚未置位 → 快照里那条是「⟳ 执行中」；历史区**自己又渲染一条**「✓ 已完成」
 * 收口条 ⇒ 同一段消息出现两条收口条，其中一条永远停在执行中（计时器已停、耗时冻结）。
 * 唯一真源：收口条由历史区按 `_sumTxt` 渲染，时间线里只保留各环节块。
 */
function _procBoxBlocksHtml(pb){
  if(!pb) return '';
  return [...pb.querySelectorAll('.proc-block')].map(b=>b.outerHTML).join('');
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
      h += `<div class="proc-summary" onclick="procSummaryToggle()"><span class="proc-spin"></span>执行中… ${_procS.timeline.length} 个环节 · <span class="proc-sum-elapsed">${_procS.elapsedText||_procElapsedText()}</span>${failN?` · ${failN} 失败`:''}${_procDetailBtn()}<span class="chev">▾</span></div>`;
      ensureElapsedTimer();
    } else {
      h += `<div class="proc-summary ${_procS.summaryCollapsed?'collapsed':''}" onclick="procSummaryToggle()">✓ 已完成 · ${_procS.timeline.length} 环节 · 耗时 ${_procS.elapsedText||_procElapsedText()}${tokenTxt}${failN?` · ${failN} 失败`:''}${_procDetailBtn()}<span class="chev">▾</span></div>`;
    }
  }
  const hideBody = _procS.done && _procS.summaryCollapsed;
  if(!hideBody){
    _procS.timeline.forEach(s=>{
      const run = s.status==='run';
      // 五轮反馈5：stopped（已终止）用黄色警示但不转圈；只有 run 才有 spinner/呼吸
      const statusCls = run ? 'w' : (s.status==='failed' ? 'r' : (s.status==='stopped' ? 'w' : 'ok'));
      const sub = s.sub ? `<span class="proc-sub">${esc(s.sub)}</span>` : '';
      // subHtml：**白名单构造的可信 HTML**（当前仅「知识库检索」来源徽章）——不经过 esc，
      // 注意：普通文本一律走 sub（会被转义），切勿把后端字符串直接塞进这里。
      const subHtml = s.subHtml ? `<span class="proc-sub">${s.subHtml}</span>` : '';
      const elapsed = s.elapsedStart ? `<span class="tool-elapsed" data-start="${s.elapsedStart}">0.0s</span>`
        : ((s.elapsedMs!==undefined && s.elapsedMs!==null && s.elapsedMs>=0) ? `<span class="tool-elapsed done">⏱ ${(s.elapsedMs/1000).toFixed(1)}s</span>` : '');
      // V2.6 动态效果：运行中条目 running 类（呼吸边框）+ 状态徽章前 spinner
      const spin = run ? '<span class="proc-spin"></span>' : '';
      h += `<div class="proc-block ${s.type}${run?' running':''}${s.collapsed?' collapsed':''}">
        <div class="proc-head" onclick="toggleProc(this)" title="点击${s.collapsed?'展开':'收起'}详情"><span>${s.icon}</span><span class="proc-title">${esc(s.title)}${sub}${subHtml}</span>${elapsed}<span class="st ${statusCls}" style="margin-left:auto;">${spin}${esc(s.statusText)}</span><span class="chev">▾</span></div>
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
  // 2026-09-29：改为「贴底才跟随」——执行过程时间线每帧长高时，只有视口本来就在
  //   底部附近才钉底；用户上滑查看上文后不再被强行拽回（原为无条件 scrollTop=scrollHeight）。
  stickBottom();
}
function procAddStage(name, status, detail, detailHtml){
  const iconMap = {'意图识别':'🔍','知识库检索':'📚','生成与校验':'✍️','写入会话':'💾'};
  procUpsert('stage_'+name, {type:'stage', icon: iconMap[name]||'🔍', title: name,
    status, statusText: status==='run'?'执行中…':'✓ 完成',
    sub: detail||'', subHtml: detailHtml||''});
  // 生成与校验完成 → 自动收起思考过程（思考没有单独的事件"完成"）
  if(name === '生成与校验' && status === 'done' && _procS.thinking) {
    const s = _procS.timeline.find(x=>x.id==='thinking');
    if(s && s.status === 'run') {
      // 2026-09-29（WorkBuddy 式环节总结）：完成时头部切为**首句总结**（浅色），
      //   状态文案带轮数；点击仍可展开完整思考流（轮次分隔保留）。
      const _head = _procSumText(String(_procS.thinking || '').replace(/\s+/g, ' ').trim());
      s.status = 'done';
      s.statusText = '✓ 已深度思考' + (_procS.thinkingOrder.length > 1 ? ` ${_procS.thinkingOrder.length} 轮` : '');
      s.sub = _head;
      s.collapsed = true; procRender();
    }
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
  // 2026-09-29（WorkBuddy 式环节总结）：头部一行**浅色实时摘要** —— 显示思考流的末句，
  //   用户不展开也能看到"AI 此刻在想什么"。原实现头部只有静态标题"思考过程"，
  //   内容全部埋在展开体里，运行期间等于黑盒（用户反馈"AI 思考内容无法查看"）。
  const _tail = String(_procS.thinking || '').replace(/\s+/g, ' ').trim();
  const _liveSum = _tail ? _toolSumm(_tail.slice(-90), 56) : '';
  procUpsert('thinking', {type:'thinking', icon:'💭', title:'思考过程', status:'run',
    statusText:'思考中…', sub: _liveSum ? ('…' + _liveSum) : '', detail});
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
  // UX规范·硬规则1（2026-10-02）：一动作一节点 —— 后端每笔调用带稳定 call_id（协议 tool_call id），
  // 同一 call_id 的 run/done 原地迁移同一节点；同名并发两笔调用是两个节点，不再互相覆盖结果。
  // 无 call_id 的旧事件/历史路径回退原逻辑（同名键 + 完成后再来另起新条）。
  let upsertId;
  if(ev.call_id){ upsertId = 'tool_'+ev.call_id; }
  else {
    upsertId = 'tool_'+ev.name;
    if(run){
      const cur = _procS.timeline.find(x=>x.id===upsertId);
      if(cur && cur.status!=='run') upsertId = upsertId+'_'+Date.now();
    }
  }
  let detail = '';
  if(ev.arguments) detail += `<div class="proc-tool-param">参数：${esc(typeof ev.arguments==='string'?ev.arguments:JSON.stringify(ev.arguments))}</div>`;
  // UX规范·硬规则2（2026-10-02）：标题=「动词+宾语」完整活动短语（命中映射表不带「调用」前缀）；
  // 结果摘要只出现在 sub 槽位（下方 '· xxx'），与标题互斥、永不拼接成一句。
  const patch = {type:'tool', icon:'🛠', title: _toolActivity(ev.name)};
  // 2026-09-25（对齐 Claude Code / Codex）：头部加**一行结果摘要**，折叠状态下也能一眼看出
  // "这个工具干了什么、成没成"；完整参数/结果仍在展开体里（信息密度与细节两者兼顾）。
  if(!run){
    const _res = String(ev.error || ev.result || '').replace(/\s+/g, ' ').trim();
    if(_res) patch.sub = ' · ' + _toolSumm(_res, 60);   // 不加 ✓/✗：头部已有状态徽章，避免重复标记
  }
  if(run){
    // P0-1 工具耗时：run 记起始，头部徽章实时计时。UX规范·硬规则1：键用 call_id（并发同名
    // 调用各记各的起点）；无 call_id 回退工具名（旧行为：同名重试沿用首次起点）。
    if(!_procS.toolStart) _procS.toolStart = {};
    const _tsk = ev.call_id || ev.name;
    if(!_procS.toolStart[_tsk]) _procS.toolStart[_tsk] = Date.now();
    patch.elapsedStart = _procS.toolStart[_tsk];
    ensureElapsedTimer();
  } else {
    const _tsk = ev.call_id || ev.name;
    // 耗时取值：优先后端回报的精确 latency_ms；缺省才用前端 run→done 测量值兜底
    if(ev.latency_ms){ patch.elapsedMs = ev.latency_ms; }
    else if(_procS.toolStart && _procS.toolStart[_tsk]){
      patch.elapsedMs = Date.now() - _procS.toolStart[_tsk];
    }
    if(_procS.toolStart) delete _procS.toolStart[_tsk];
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
      // SRS-GN-CO（2026-09-21）：覆盖性工具结果 → 行业风格富组件（与历史回放 06-cards 同一套 covTryCard）
      const covHtml = (typeof covTryCard === 'function') ? covTryCard(ev.name, ev.result, ev.arguments) : null;
      if(covHtml){
        detail += `<div class="proc-tool-result" style="border:none;padding:0;">${covHtml}</div>`;
      } else {
        detail += `<div class="proc-tool-result ${trunc?'trunc':''}">
        <div class="pr-head"><span>结果</span>${trunc?`<span class="pr-act" onclick="toolExpand(this)">展开全文</span>`:''}<span class="pr-act" onclick="copyToolResult(this)">📋 复制</span></div>
        <div class="pr-body">${esc(String(res))}</div></div>`;
      }
    }
  }
  patch.detail = detail;
  patch.status = run?'run':(ev.ok?'done':'failed');
  patch.statusText = run?'调用中…':(ev.ok?'✓ 成功':'✗ 失败');
  procUpsert(upsertId, patch);
  // UX规范 B3（2026-10-03）确认闸门内联化：工具结果含「确认单 #N」→ 时间线内联渲染确认卡
  // （批准/拒绝），复用工作流监控面板同一 decide API——不再强迫用户切面板找入口。
  // 放在工具卡 procUpsert 之后，保证时间线顺序 = 实际时序（工具入队 → 确认卡）。
  if(!run && ev.ok){
    const _cm = String(ev.result||'').match(/确认单\s*#(\d+)/);
    if(_cm) procAddConfirmGate(parseInt(_cm[1], 10), ev);
  }
}

// ── UX规范 D1（2026-10-03）上下文压缩三件套 ──
// 系统流单元（与模型产出区隔）：可展开详情（各档做了什么）+ 用量指示器（压缩前后 vs 预算）
// + 回退入口。回退语义：压缩只发生在 prompt 组装层，历史原文全部在库（档3 占位文案本身
// 就写"追问即可恢复"）→ 「追问恢复」把恢复性提问填入输入框，不伪造"撤销压缩"。
const _TIER_DESC = {1:'检索/历史降密度重裁（scale 0.6）',
                    2:'追加：附件块截断至 1200 tok',
                    3:'追加：更早对话摘要置一行占位（追问可恢复）',
                    4:'全量压缩：仅保 system 本体与当前输入'};
function procAddCompact(ev){
  const tier = ev.tier || 0, before = ev.before || 0, after = ev.after || before, cap = ev.cap || 0;
  const pct = cap ? Math.min(999, Math.round(before / cap * 100)) : 0;
  const afterPct = cap ? Math.min(100, Math.round(after / cap * 100)) : 0;
  const bar = `<div style="margin:6px 0 2px;">
    <div style="display:flex;align-items:center;gap:8px;font-size:10.5px;color:var(--mut);margin-bottom:3px;">
      <span style="min-width:44px;">压缩前</span>
      <span style="flex:1;height:6px;border-radius:3px;background:var(--line);overflow:hidden;display:inline-block;">
        <i style="display:block;height:100%;width:${Math.min(100, pct)}%;${pct > 100 ? 'background:var(--red);' : 'background:var(--blue-d);'}"></i></span>
      <span style="min-width:110px;text-align:right;${pct > 100 ? 'color:var(--red);font-weight:600;' : ''}">${before} tok · ${pct}% 预算</span></div>
    <div style="display:flex;align-items:center;gap:8px;font-size:10.5px;color:var(--mut);">
      <span style="min-width:44px;">压缩后</span>
      <span style="flex:1;height:6px;border-radius:3px;background:var(--line);overflow:hidden;display:inline-block;">
        <i style="display:block;height:100%;width:${afterPct}%;background:var(--green,#2F6F4F);"></i></span>
      <span style="min-width:110px;text-align:right;">${after} tok · ${afterPct}% 预算</span></div></div>`;
  let tiers = '';
  for(let i = 1; i <= tier; i++){
    tiers += `<div style="font-size:11px;color:var(--mut);line-height:1.8;"><b style="color:var(--fg);">档${i}</b>　${_TIER_DESC[i] || ''}${i === tier ? '　<span style="color:var(--green,#2F6F4F);">← 实际收敛档</span>' : ''}</div>`;
  }
  if(ev.tiers_planned && ev.tiers_planned > tier){
    tiers += `<div style="font-size:10.5px;color:var(--mut);">（计划档${ev.tiers_planned}，档${tier} 裁完已入预算，提前收敛）</div>`;
  }
  const detail = `<div style="font-size:11.5px;line-height:1.7;">`
    + `<div style="color:var(--mut);margin-bottom:2px;">输入估算 ${before} tok 超出总预算 ${cap} tok，自动分层压缩（system 本体/建模规则任何档不裁）：</div>`
    + bar + tiers
    + `<div style="margin-top:8px;"><span class="pr-act" style="color:var(--blue-d);" onclick="compactRecoveryAsk()">↩ 追问恢复被压缩的内容</span>`
    + `<span style="font-size:10px;color:var(--mut);margin-left:8px;">历史原文全部在库，压缩只影响本次 prompt 组装</span></div></div>`;
  procUpsert('compact', {type:'compact', icon:'🗜', title:'上下文已压缩 · 档' + tier,
    sub: `省 ${ev.trimmed || Math.max(0, before - after)} tok · 压后 ${afterPct}% 预算`,
    status:'done', statusText:'✓ 已压缩', detail});
}
// 回退入口：填入恢复性提问（用户可改写后发送），聚焦输入框——真正恢复靠下一轮
// _load_history 从库里拉全量原文，压缩占位文本本身也写着"追问即可恢复"
function compactRecoveryAsk(){
  const inp = document.getElementById('chat-input');
  const ask = '请把被压缩的更早对话摘要与关键上下文展开恢复，再继续当前任务。';
  if(inp){
    inp.value = inp.value ? inp.value + ' ' + ask : ask;
    try{ inp.focus(); }catch(e){}
  }
  if(window.toast && toast.info) toast.info('已填入恢复性提问，发送后 AI 将从会话库拉回完整上下文');
}

// ── UX规范 B3（2026-10-03）内联确认闸门 ──
// 同一确认单在时间线上只允许一个节点（硬规则1）；确认卡 = 需要用户行动的单元，
// 保持 run 态常驻展开，直至批准/拒绝落终态。
function procAddConfirmGate(cid, ev){
  const id = 'hil_' + cid;
  if(_procS.timeline.some(x=>x.id === id)) return;   // 同一确认单不重复建卡
  const name = ev.name || '';
  const args = ev.arguments ? (typeof ev.arguments === 'string' ? ev.arguments : JSON.stringify(ev.arguments)) : '';
  const detail = `<div style="font-size:11.5px;line-height:1.7;">`
    + `<div><b>工具：</b>${esc(name)}</div>`
    + (args ? `<div style="max-height:88px;overflow:auto;white-space:pre-wrap;background:var(--bg,#f6f7f9);border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin-top:4px;font-size:10.5px;">${esc(args)}</div>` : '')
    + `<div style="color:var(--mut);margin-top:6px;">这是<strong style="color:var(--orange,#B96A00);">写操作 / 高风险动作</strong>：批准后自动执行并回写结果；拒绝则该动作不生效。</div>`
    + `<div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`
    + `<button class="btn sm grn" id="hil-ok-${cid}" onclick="hilDecide(${cid}, true)">✓ 批准并执行</button>`
    + `<button class="btn sm ghost" id="hil-no-${cid}" onclick="hilDecide(${cid}, false)">✕ 拒绝</button>`
    + `<span style="font-size:10px;color:var(--mut);">确认单 #${cid} · 也可在 工作流 → 执行监控 处理</span></div></div>`;
  procUpsert(id, {type:'confirm', icon:'🔐', title:'等待确认：' + _toolActivity(name),
                  status:'run', statusText:'⏸ 待人工确认', detail});
}
// 批准/拒绝：与工作流监控面板（29-flow.js）同一 decide API；失败恢复按钮可重试
async function hilDecide(cid, approve){
  const okBtn = document.getElementById('hil-ok-' + cid), noBtn = document.getElementById('hil-no-' + cid);
  if(okBtn) okBtn.disabled = true;
  if(noBtn) noBtn.disabled = true;
  try{
    await api('/api/studio/hil-confirmations/' + cid + '/decide',
              {method:'POST', body:JSON.stringify({approve:!!approve, decided_by:'王工'})});
    procUpsert('hil_' + cid, approve ? {status:'done', statusText:'✓ 已批准 · 自动执行中'}
                                     : {status:'failed', statusText:'✗ 已拒绝 · 动作不生效'});
    if(window.toast && toast.success) toast.success(approve ? '已批准，确认单 #' + cid + ' 将自动执行' : '已拒绝确认单 #' + cid);
  }catch(e){
    if(okBtn) okBtn.disabled = false;
    if(noBtn) noBtn.disabled = false;
    if(window.toast && toast.error) toast.error('确认操作失败：' + (e && e.message || e));
  }
}
// V2.6：子任务内部工具事件归组（挂到 subtask 卡 children，随子任务卡展开查看）
function _procAddSubTool(pk, ev, displayName){
  if(!_procS.subChildren) _procS.subChildren = {};
  const arr = (_procS.subChildren[pk] = _procS.subChildren[pk] || []);
  const run = ev.status==='run';
  // UX规范·硬规则1（2026-10-02）：子任务内工具同样按 call_id 复用条目（并发同名调用各自成项）；
  // 无 call_id 回退「同名+运行中」匹配。
  let item = ev.call_id ? arr.find(x=>x.call_id===ev.call_id)
                        : arr.find(x=>x.name===ev.name && x.status==='run');
  if(item && !run){
    item.status='done'; item.ok=!!ev.ok;
    item.result=ev.result||''; item.error=ev.error||''; item.truncated=!!ev.truncated;
    item.latencyMs=ev.latency_ms||0; item.elapsedStart=null;
  } else if(!item){
    arr.push({name:ev.name, displayName, call_id:ev.call_id||'', arguments:ev.arguments, status: run?'run':'done',
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
    // 2026-09-29（细粒度补齐）：子任务内部工具结果此前只有截断展示，**没有展开/复制入口**
    //   （顶层工具卡有，这里漏了）—— 长结果（如 V2 代码校验返回）被截断后无法查看全文。
    //   现对齐顶层：>200 字或 truncated 时给「展开全文」，一律给「复制」。
    const _long = trunc || String(res).length > 200;
    h += '<div style="margin-bottom:6px;">'
      + '<div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:var(--blue-d,#3478f6);flex-wrap:wrap;">🛠 '+esc(it.displayName||it.name)
      + ' <span class="st '+stCls+'">'+spin+stTxt+'</span>'
      + (it.latencyMs?'<span style="color:var(--mut);font-size:10px;">⏱ '+(it.latencyMs/1000).toFixed(1)+'s</span>':'')
      + '</div>'
      + (it.arguments?'<div class="proc-tool-param">参数：'+esc(typeof it.arguments==='string'?it.arguments:JSON.stringify(it.arguments))+'</div>':'')
      + '<div class="proc-tool-result '+(_long?'trunc':'')+'"><div class="pr-head"><span>结果</span>'
      + (_long?'<span class="pr-act" onclick="toolExpand(this)">展开全文</span>':'')
      + '<span class="pr-act" onclick="copyToolResult(this)">📋 复制</span></div><div class="pr-body">'+esc(String(res))+'</div></div>'
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
    // 2026-09-26：由 `collapsed:true` 改为**默认展开** —— 多意图分解是"识别结果对用户可见"的关键信息，
    // 折叠状态下用户只看得到"多意图分解（3 个阶段）· 3 段"，根本不知道系统把话拆成了哪三步、
    // 各交给哪个 Agent（识别出来却看不见 ≈ 没识别）。代价是每次多占约 3 行高度。
    status:'done', statusText:`${seq.length} 段`, detail, collapsed:false});
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
// UX规范·硬规则2（2026-10-02）：工具卡标题=「动词+宾语」完整活动短语。
// 命中映射表 → 直接用短语；前缀族（file_* / zhiyuan_*）→ 按族生成；都未命中 → 回退「调用 工具名」。
// 与头部结果摘要（sub 槽）互斥：标题描述动作，摘要描述结果，二者永不拼接成一句。
function _toolActivity(name){
  const n = String(name||'');
  const MAP = {
    graph_retrieve:'检索知识图谱', conflict_check:'检测模型冲突', impact_analyze:'分析变更影响',
    validate:'校验模型规范性', entity_create:'创建知识实体', sysml_v2_validate:'校验 SysML V2 代码',
    mbse_pull_ingest:'拉取并入库工程数据', report_export:'导出报告文档',
    fetch_tool_result:'取回工具结果全文',
    file_list:'列出目录内容', file_read:'读取文件', file_write:'写入文件',
    file_append:'追加写入文件', file_mkdir:'创建目录', file_delete:'删除文件'
  };
  if(MAP[n]) return MAP[n];
  if(n.indexOf('file_')===0) return '文件操作 '+n.slice(5);
  if(n.indexOf('zhiyuan_')===0) return '同步智源平台 '+n.slice(8);
  return '调用 '+n;
}
// 2026-09-26：卡片摘要行**按句子收口**（原实现 `slice(0,60)` 硬切，实测断在词中间："职责是**产"）。
// 展示优先用后端 `ui_summary`（已剔除"承接上游/职责是"这类内部交接语）；老历史数据无此字段 →
// 回退到原 summary，此处只做「去 markdown + 首句收口」（内部黑话的剔除规则留在后端一处，避免两份词表漂移）。
function _procSumText(t){
  let s = String(t||'').replace(/\*\*(.+?)\*\*/g,'$1').replace(/`([^`]*)`/g,'$1').replace(/^\s*#{1,6}\s*/gm,'').trim();
  if(!s) return '';
  const m = s.match(/[。！？；!?;]/);
  if(m) s = s.slice(0, m.index + 1);
  if(s.length > 52){
    const cut = Math.max.apply(null, ['，',',','、','；',';','。','！','？','!','?',' '].map(c=>s.lastIndexOf(c, 52)));
    s = cut > 26 ? s.slice(0, cut+1).replace(/[，,、；;]$/,'') + '…' : s.slice(0, 52) + '…';
  }
  return s;
}
// Task 14：子任务卡片（状态徽章 done/failed/partial + 专长标签 + 重试标记 + 协议摘要徽章 + 耗时 + 错误）
function subtaskCardHtml(s){
  const run = s.status==='run';
  const stopped = s.status==='stopped';   // 五轮反馈5：用户终止 → 不再算"执行中"，也不误标失败
  const ok = s.status==='done' && !s.error;   // 2026-09-29：done 但带 error → 不算干净完成，避免"✓完成 + 红字失败"自相矛盾
  const part = s.status==='partial' || (s.status==='done' && !!s.error);
  const badge = ok ? '<span class="st ok">done</span>'
    : part ? '<span class="st w">partial</span>'
    : run ? '<span class="st w">run</span>'
    : stopped ? '<span class="st w">stopped</span>'
    : '<span class="st r">failed</span>';
  const caps = capTagsFor(s.agent);
  // Task 14：自动重试标记（retry_count>0 标黄显示重试次数）
  const retry = (s.retry_count||0) > 0 ? `<span class="st w" title="自动重试 ${s.retry_count} 次后完成">↻ 重试${s.retry_count}</span>` : '';
  // Task 14：协议摘要徽章（summary_status：full/partial/failed → 交付物完整性）
  const ss = s.summary_status;
  const sumBadge = ss==='full' ? '<span class="st ok" style="font-size:9.5px;">交付 full</span>'
    : ss==='partial' ? '<span class="st w" style="font-size:9.5px;">交付 partial</span>'
    : ss==='failed' ? '<span class="st r" style="font-size:9.5px;">交付 failed</span>' : '';
  // 摘要文本：优先后端**面向用户**的 ui_summary（2026-09-26；剔除内部交接语），回退旧 summary 字段
  const sumRaw = s.ui_summary || ((s.summary && typeof s.summary === 'object') ? (s.summary.summary||'') : (s.summary||''));
  // 2026-09-29（用户三轮反馈）：不再单行省略 + hover 看全文 —— 摘要**直接展示在框内**，
  // 左对齐、限高 96px、框内滚动看完整内容（title 弹层交互废弃）。
  const sum = sumRaw ? `<div style="color:var(--mut);font-size:10.5px;margin-top:3px;text-align:left;max-height:96px;overflow-y:auto;line-height:1.6;white-space:pre-wrap;word-break:break-word;">${esc(String(sumRaw))}</div>` : '';
  // 2026-09-29（用户三轮反馈"失败后系统在做什么"）：失败态补一行**系统行为说明**，
  // 语义与后端编排器一致（stream.py Task 7：自动重试 1 次 → 仍失败跳过 → 依赖任务受阻 → 已完成部分继续汇总）。
  const failNote = (s.status==='failed' && s.error) ? `<div style="color:var(--mut);font-size:10px;margin-top:2px;line-height:1.5;">系统已自动重试 1 次仍失败 → 本步跳过，依赖它的后续步骤受阻；编排会用已完成的部分继续汇总输出，不会卡住。</div>` : '';
  // P1-2：子任务 token 消耗（后端 subtask 事件 token_count 透传）
  const tk = (s.token_count||0) > 0 ? ` <span style="color:var(--mut);font-size:10.5px;" title="本子任务 token 消耗">🪙 ${Number(s.token_count).toLocaleString()}</span>` : '';
  // 2026-09-29（用户三轮反馈）：外层阶段卡头部已展示标题，展开详情内**不再重复渲染名称**
  // （此前第一行 <b>title</b> 与外层 head 完全一样，用户指出冗余）。本行只保留状态符号与徽章。
  return `<div class="proc-line" style="border:1px solid var(--line);border-radius:8px;padding:5px 8px;margin:3px 0;background:#fff;">
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">${ok?'✓':part?'◐':run?'⟳':stopped?'⏹':'✗'}${badge}${s.agent?` <span class="tag" style="font-size:10px;">${esc(s.agent)}</span>`:''}${caps}${retry}${sumBadge}${s.latency_ms?` <span style="color:var(--mut);font-size:10.5px;">⏱ ${(s.latency_ms/1000).toFixed(1)}s</span>`:''}${tk}${run?' <span class="st w">执行中…</span>':''}${stopped?' <span class="st w">已终止</span>':''}</div>
    ${sum}${s.error?`<div style="color:var(--red);font-size:10.5px;margin-top:2px;">${esc(s.error)}</div>`:''}${failNote}</div>`;
}
// ── 五轮反馈5：终止/中断后，所有 run 态环节统一翻转为「⏹ 已终止」──
//  此前 finalizeStopped 只把块折叠，"执行中…/思考中…/执行中 1/1" 徽章原样进历史，
//  用户终止后界面仍显示一堆在跑的任务（截图实证）。现在：
//    ① timeline 各 run 块 → stopped（thinking 特判"思考已中断"），冻结实时计时；
//    ② 子任务卡重建内层 detail（原 detail HTML 里的"执行中…"徽章不会自己变）；
//    ③ 计划清单块整体重算（执行中 N/M → 已终止，未完成数可见）。
function procMarkStopped(){
  try{
    let flipped = 0;
    (_procS.timeline||[]).forEach(x=>{
      if(x.status !== 'run') return;
      x.status = 'stopped';
      x.statusText = (x.id === 'thinking') ? '⏹ 思考已中断' : '⏹ 已终止';
      x.elapsedStart = null;
      x.collapsed = true;
      if(x.type === 'subtask' && x.key !== undefined){
        x.detail = subtaskCardHtml({key:x.key, title:x.title, agent:x.agent||'', status:'stopped',
                                    latency_ms:x.elapsedMs, error:''}) + _subChildrenHtml(x.key);
      }
      flipped++;
    });
    // 计划清单/子任务 DAG 节点同步翻转
    if(_procS.dag && _procS.dag.order && _procS.dag.order.length){
      let runN = 0;
      _procS.dag.order.forEach(k=>{
        const n = _procS.dag.nodes[k];
        if(n && n.status === 'run'){ n.status = 'stopped'; runN++; }
      });
      if(_procS.dag.order.length){
        const all = _procS.dag.order.map(k=>_procS.dag.nodes[k]);
        const doneN = all.filter(n=>n && n.status==='done').length;
        const stopN = all.filter(n=>n && n.status==='stopped').length;
        const failN = all.filter(n=>n && n.status==='failed').length;
        const planList = '<div style="display:flex;flex-direction:column;gap:5px;padding:2px 0;">' +
          _procS.dag.order.map((k,i)=>{
            const n = _procS.dag.nodes[k]||{};
            const isStop = n.status==='stopped';
            const stTxt = isStop ? '已终止' : (n.status==='failed' ? '失败' : (n.status==='partial' ? '部分完成' : '完成'));
            const stCls = isStop ? 'w' : (n.status==='failed' ? 'r' : 'ok');
            const dep = (n.deps&&n.deps.length) ? '<span style="color:var(--mut);font-size:10px;">依赖: '+esc(n.deps.join('、'))+'</span>' : '';
            const lat = n.latency_ms ? '<span style="color:var(--mut);font-size:10px;">⏱ '+(n.latency_ms/1000).toFixed(1)+'s</span>' : '';
            return '<div style="display:flex;align-items:center;gap:6px;font-size:11px;flex-wrap:wrap;">'
              + '<span style="color:var(--mut);font-size:10px;min-width:16px;">'+(i+1)+'.</span>'
              + '<span class="st '+stCls+'" style="min-width:56px;text-align:center;">'+stTxt+'</span>'
              + '<b style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+esc(n.title||k)+'</b>'
              + (n.agent?'<span style="color:var(--mut);font-size:10px;">'+esc(n.agent)+'</span>':'')
              + lat + dep + '</div>';
          }).join('') + '</div>';
        procUpsert('plan', {type:'plan', icon:'📋', title:`计划清单（${all.length} 任务）`, status:'stopped',
          statusText:`⏹ 已终止${stopN?` · ${stopN} 项未完成`:''}${doneN?` · ${doneN} 完成`:''}${failN?` · ${failN} 失败`:''}`,
          detail: planList, collapsed: true});
      }
      flipped++;
    }
    _procS.done = true;
    _procS.endTime = _procS.endTime || Date.now();
    stopElapsedTimer();
    if(flipped) procRender();
  }catch(e){ /* 收口翻转失败不阻断主收口流程 */ }
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
  // 2026-09-29：done 但带 error → "◐ 完成（有异常）"，不再标"✓ 完成"（与卡内红字失败自相矛盾）
  const statusText = run ? '执行中…'
    : (s.status==='failed' ? '✗ 失败'
    : (s.status==='partial' ? '◐ 部分完成'
    : (s.status==='done' && s.error ? '◐ 完成（有异常）' : '✓ 完成')));
  // V2.6：子任务卡 = 汇总信息 + 内部执行明细（工具参数/结果，嵌套层级查看）
  // 2026-09-26（用户反馈"占用太多区域"）：**默认折叠成一行**，点开才看明细。
  //  摘要句同时上提到 head 的 `sub` 位（`.proc-title` 已具备单行省略）→ 折叠态仍能看到
  //  "这一步交付了什么"，不会因为折叠把上一轮刚修好的摘要藏掉。
  const _stId = 'subtask_'+key;
  const _stNew = !_procS.timeline.some(x=>x.id===_stId);
  const _stPatch = {type:'subtask', icon:'🕸', title: s.title||key, status: s.status,
    statusText, key,
    sub: _procSumText(s.ui_summary || ((s.summary&&typeof s.summary==='object')?(s.summary.summary||''):(s.summary||''))),
    detail: subtaskCardHtml(s) + _subChildrenHtml(key)};
  // ⚠️ 只在**首次创建**时置 collapsed —— 否则每来一次 run/done 事件都会把用户手动展开的卡重新折叠
  if(_stNew) _stPatch.collapsed = true;
  procUpsert(_stId, _stPatch);
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
    // 2026-09-29：贴底才跟随（原为无条件钉底 → AI 输出期间用户上滑被强行拽回）。
    stickBottom();
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
  } else if(evType === 'compact') {
    procAddCompact(ev);    // UX规范 D1：上下文压缩系统标记（详情/用量/追问恢复）
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
    // UX规范§8.4（2026-10-02）：需要用户行动的单元出现 = 唯一允许抢夺滚动的情况 ——
    // 澄清条插在消息体顶部，用户若停留在长输出中段会完全看不到它 → 这里强制滚到可见。
    const _cb = document.getElementById('clarify-bar');
    if(_cb && _cb.scrollIntoView){ try{ _cb.scrollIntoView({block:'center'}); }catch(e){ _cb.scrollIntoView(); } }
  } else if(evType === 'clarify_ask') {
    _forceFlushTokens();
    setPendingClarify(ev);   // 2026-09-25：登记挂起 → 输入框上方出现「AI 正在等待确认」提示（可在此直接作答）
    renderClarifyAsk(ev);   // 内容级澄清：信息不清晰 → 选择题确认（回答后续答）
  } else if(evType === 'done') {
    _forceFlushTokens();
    finishStream(ev.data, aiBox);
  } else if(evType === 'error') {
    _forceFlushTokens();
    showStopBtn(false); _streaming = false;
    // V2.6：中断也收口（汇总条切「已中断」，不停留"执行中"）
    if(typeof procMarkStopped === 'function') procMarkStopped();
    else {
      try{
        _procS.done = true; _procS.endTime = Date.now();
        _procS.timeline.forEach(x=>{ if(x.status==='run'){ x.status='failed'; x.statusText='✗ 中断'; } });
        stopElapsedTimer(); procRender();
      }catch(e){}
    }
    const bodyEl = aiBox ? aiBox.querySelector('.body') : document.querySelector('#stream-ai .body');
    // 2026-09-29（用户五轮反馈4）：不再整体覆盖 body —— 后端已在 error 路径固化已生成内容，
    //  前端保留流出的文字、把失败说明**追加**在下方（覆盖 = 刷新前内容先从 DOM 消失，观感即"输出全丢"）。
    const _typ = bodyEl ? bodyEl.querySelector('.typing') : null; if(_typ) _typ.remove();
    if(bodyEl) bodyEl.classList.remove('streaming');
    const errNote = document.createElement('div');
    errNote.style.cssText = 'margin-top:8px;';
    // 2026-09-29（用户三轮反馈"失败后系统在做什么"）：失败横条补齐三问 —— 发生了什么 / 系统现状 / 下一步
    errNote.innerHTML = `<div style="color:var(--red);font-size:11.5px;">调用失败：${esc(ev.message||'未知错误')}</div>`
      + `<div style="color:var(--mut);font-size:10.5px;margin-top:4px;line-height:1.6;">`
      + `本次执行已终止，不再有后台任务在跑。上方已生成内容与执行轨迹<b>已保留</b>（刷新不丢）；`
      + `确认原因（如网络波动）后，<b>直接重新发送消息</b>即可从头执行。</div>`;
    if(bodyEl) bodyEl.appendChild(errNote);
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
//  2026-09-25：卡内 HTML 抽成 clarifyCardInnerHtml()，供**流式当时的卡**与**历史消息还原的卡**
//  共用（此前历史只渲染只读文字 → 刷新后完全没有输入入口）。查询容器 id 也改为 class，
//  避免"流式卡 + 历史卡"同时存在时 id 冲突。
function clarifyCardInnerHtml(qs, title){
  // 2026-09-26：标题可由事件带（`clarify_ask.title`）—— 同一张卡现在也用于「意图确认」，
  //   硬编码"确认建模信息"会让"我不确定你想做什么"这类提问挂着错误的标题。缺省保留原文案。
  // 2026-09-28：改为「一次一题」向导式（stepper）。此前把所有题目一次铺开：
  //   · 题目一多卡片纵向撑得很长，提交按钮要滚动才看得见（用户反馈"占用较大篇幅"）；
  //   · 每题的输入框默认常显 —— 那是 2026-09-25 为修"没有输入入口"做的反向补偿，
  //     现在入口明确收敛为「选「其他 / 自定义」才展开该题输入框」，不再默认铺开。
  //   结构：题目 1..N 各一步 → 末步「补充说明（选填）」多行输入 → 提交。
  const n = (qs || []).length;
  // 卡级序号：radio 的 name 必须**卡内唯一**。此前 name 只按 question_id 生成，
  // 「历史卡 + 流式卡」同时存在时两卡 radio 同组 → 点选一张会顶掉另一张的选中项。
  const seq = (window._clarifySeq = (window._clarifySeq || 0) + 1);
  const steps = (qs || []).map((q, qi)=>`
      <div class="clarify-step clarify-q" data-idx="${qi}" data-qid="${esc(q.id)}" style="${qi===0?'':'display:none;'}">
        <div class="cq-h"><span class="cq-n">第 ${qi+1} / ${n} 题</span>${q.allow_custom?'<span class="cq-tag">可补充输入</span>':''}</div>
        <div class="cq-q">${esc(q.question||'')}</div>
        <div class="clarify-opts">
          ${(q.options||[]).map(o=>`<label class="cq-opt"><input type="radio" name="cq${seq}_${esc(q.id)}" value="${esc(o)}" onchange="clarifyToggleCustom(this)"><span>${esc(o)}</span></label>`).join('')}
          <label class="cq-opt cq-opt-custom"><input type="radio" name="cq${seq}_${esc(q.id)}" value="__custom__" onchange="clarifyToggleCustom(this)"><span>其他 / 自定义<span class="cq-hint2">（选中后展开输入框）</span></span></label>
        </div>
        <div class="cq-custom-box" style="display:none;">
          <input class="cq-custom" data-qid="${esc(q.id)}" placeholder="✎ 请填写你的答案（填写后以此为准）">
        </div>
      </div>`).join('');
  // 末步：补充说明（选填）—— 选项不足以表达时的兜底入口（多行）
  const noteIdx = n;
  const noteStep = `
      <div class="clarify-step clarify-note-step" data-idx="${noteIdx}" style="display:none;">
        <div class="cq-h"><span class="cq-n">补充说明</span><span class="cq-tag">选填</span></div>
        <div class="cq-q">上面的选项如果不足以表达你的意思，可以在这里补充说明（也可以直接跳过）。</div>
        <textarea class="cq-note" rows="3" placeholder="✎ 额外补充说明（选填）…"></textarea>
      </div>`;
  const total = n + 1;
  const dots = Array.from({length: total}, (_, i)=>
    `<span class="clarify-dot${i===0?' on':''}" data-idx="${i}" onclick="clarifyGoTo(this,${i})" title="跳到第 ${i+1} 步"></span>`).join('');
  return `
    <div class="clarify-card-head">
      <b class="cq-title">${esc(title || '❓ 需要确认建模信息')}</b>
      <span class="clarify-prog">第 1 / ${n} 题</span>
    </div>
    <div class="clarify-prog-bar"><i style="width:${Math.round(100/total)}%"></i></div>
    <div class="clarify-dots">${dots}</div>
    <div class="clarify-steps">${steps}${noteStep}</div>
    <div class="clarify-foot">
      <button class="btn ghost sm cq-prev" style="visibility:hidden;" onclick="clarifyGoStep(this,-1)">← 上一个</button>
      <button class="btn grn sm cq-next" onclick="clarifyGoStep(this,1)">下一个 →</button>
      <button class="btn grn sm cq-send" style="display:none;" onclick="clarifyAnswerSend()">✉ 发送补充信息</button>
      <button class="btn ghost sm" onclick="clarifySkipSend()">跳过（按现状继续）</button>
      <span class="cq-tail">共 ${n} 题 · 回答后继续执行</span>
    </div>`;
}
// ── stepper 导航：一次只显示一步 ──
function _clarifyCardOf(el){ return el ? el.closest('.clarify-ask-card') : null; }
function clarifyGoStep(el, delta){
  const card = _clarifyCardOf(el);
  if(!card) return;
  const cur = parseInt(card.dataset.step || '0', 10) || 0;
  clarifyGo(card, cur + delta);
}
function clarifyGoTo(el, idx){
  const card = _clarifyCardOf(el);
  if(card) clarifyGo(card, idx);
}
function _clarifyStepAnswered(step){
  if(!step) return false;
  const c = step.querySelector('.cq-custom');
  if(c && (c.value || '').trim()) return true;
  const r = step.querySelector('input[type=radio]:checked');
  return !!(r && r.value);
}
function clarifyGo(card, idx){
  if(!card) return;
  const steps = card.querySelectorAll('.clarify-step');
  const total = steps.length;
  if(!total) return;
  let i = idx | 0;
  if(i < 0) i = 0;
  if(i > total - 1) i = total - 1;
  steps.forEach((s, si)=>{ s.style.display = (si === i) ? '' : 'none'; });
  card.dataset.step = String(i);
  const n = total - 1;   // 末步是「补充说明」，题目数为 total-1
  const prog = card.querySelector('.clarify-prog');
  if(prog) prog.textContent = (i >= n) ? '补充说明（选填）' : `第 ${i + 1} / ${n} 题`;
  const bar = card.querySelector('.clarify-prog-bar > i');
  if(bar) bar.style.width = Math.round((i + 1) / total * 100) + '%';
  card.querySelectorAll('.clarify-dot').forEach((d, di)=>{
    d.classList.toggle('on', di === i);
    d.classList.toggle('done', di < i || (di < n && _clarifyStepAnswered(steps[di])));
  });
  const prev = card.querySelector('.cq-prev');
  const next = card.querySelector('.cq-next');
  const send = card.querySelector('.cq-send');
  if(prev) prev.style.visibility = (i === 0) ? 'hidden' : 'visible';
  if(next) next.style.display = (i >= total - 1) ? 'none' : '';
  if(send) send.style.display = (i >= total - 1) ? '' : 'none';
  // 2026-09-29：澄清步骤切换属内容高度变化（切换题目时卡片高度可能变），
  //   改为贴底才跟随 —— 用户在读上文时不被拽回；读题时（在底部）仍自动跟随。
  stickBottom();
}
// 选中「其他 / 自定义」才展开该题输入框（默认不展示）
function clarifyToggleCustom(radio){
  const step = radio.closest('.clarify-step');
  const card = radio.closest('.clarify-ask-card');
  const box = step ? step.querySelector('.cq-custom-box') : null;
  const show = (radio.value === '__custom__') && radio.checked;
  if(box){
    box.style.display = show ? '' : 'none';
    if(show){
      const inp = box.querySelector('.cq-custom');
      if(inp) setTimeout(()=>{ try{ inp.focus(); }catch(e){} }, 30);
    } else {
      const inp = box.querySelector('.cq-custom');
      if(inp) inp.value = '';   // 取消「其他」时清掉残留，避免误当成答案提交
    }
  }
  // 同步进度圆点（已答/未答）
  if(card) clarifyGo(card, parseInt(card.dataset.step || '0', 10));
}
function renderClarifyAsk(ev, host){
  // 2026-09-29（用户五轮反馈2）：确认卡不再整卡塞进会话流（此前"文字上下空旷区域太多"
  //  且常驻流中把输出顶得很长）。流内只留**一行指引**，可作答的紧凑卡挂在输入框上方
  //  （updateClarifyHint 已随 setPendingClarify 渲染，此处不重复渲染）。
  const aiBox = document.getElementById('stream-ai');
  const bodyEl = host || (aiBox ? aiBox.querySelector('.body') : null);
  if(!bodyEl) return;
  const qs = ev.questions || [];
  if(!qs.length) return;
  if(bodyEl.querySelector('.clarify-inline-note')) return;
  const note = document.createElement('div');
  note.className = 'clarify-inline-note';
  note.innerHTML = `❓ AI 发起了 <b>${qs.length}</b> 项确认（${esc(String(ev.title || '需要确认建模信息')).replace(/❓\s*/,'')}）—— 请在<b>输入框上方</b>的确认卡中作答，或在输入框直接补充。`;
  bodyEl.appendChild(note);
  stickBottom();
}
// 提交澄清答案 → 构造续答文本 → 作为新消息发送（走正常流式续答）
async function clarifyAnswerSend(){
  // 2026-09-25：优先取流式卡；没有则取历史消息还原的卡（刷新后作答路径）
  const card = document.getElementById('clarify-ask') || document.querySelector('.clarify-ask-card');
  if(!card) return;
  const answers = [];
  // 2026-09-28：选了「其他 / 自定义」却没填内容 → 定位到该题要求补填（此前会被静默丢弃，
  //   用户点了提交却什么都没提交，AI 继续追问 → 死循环感）
  let pendingIdx = -1;
  card.querySelectorAll('.clarify-q').forEach((qEl, qi)=>{
    const custom = qEl.querySelector('.cq-custom');
    const qid = custom ? custom.dataset.qid : (qEl.dataset.qid || '');
    const typed = ((custom && custom.value) || '').trim();
    if(typed){ answers.push({question_id: qid, value: typed}); return; }
    const sel = qEl.querySelector('input[type=radio]:checked');
    if(sel && sel.value === '__custom__'){ if(pendingIdx < 0) pendingIdx = qi; return; }
    if(sel && sel.value) answers.push({question_id: qid, value: sel.value});
  });
  const noteEl = card.querySelector('.cq-note');
  const note = ((noteEl && noteEl.value) || '').trim();
  if(pendingIdx >= 0){
    clarifyGo(card, pendingIdx);
    toast(`第 ${pendingIdx + 1} 题选了「其他 / 自定义」，请先填写具体内容（或改选其它选项）`);
    return;
  }
  if(!answers.length && !note){ toast('请至少选择一个选项，或在「补充说明」里写下你的答复'); return; }
  const btn = card.querySelector('.cq-send');
  if(btn){ btn.disabled = true; btn.textContent = '提交中…'; }
  try{
    const r = await api('/api/conversations/' + currentConvId + '/clarify-answer', {method:'POST', body:JSON.stringify({answers, note})});
    if(r && r.error){ toast(r.error); if(btn){ btn.disabled=false; btn.textContent='✉ 发送补充信息'; } return; }
    // 2026-09-29（五轮反馈2）：卡已挂 dock，setPendingClarify(null) 会直接撤卡 → 反馈走 toast
    toast('✓ 已提交，正在继续…');
    setPendingClarify(null);   // 2026-09-25：挂起已由后端清空 → 前端同步（撤掉输入框上方确认卡）
    sendResume(r.resume_text);
  }catch(e){ toast('提交失败：' + (e.message||'')); if(btn){ btn.disabled=false; btn.textContent='✉ 发送补充信息'; } }
}
// 跳过澄清：按现有信息继续（含澄清标记，agent 侧不再提问，防循环）
function clarifySkipSend(){
  const card = document.getElementById('clarify-ask') || document.querySelector('.clarify-ask-card');
  if(!card) return;
  // 2026-09-29（五轮反馈2）：卡已挂 dock，setPendingClarify(null) 会直接撤卡 → 反馈走 toast
  toast('已跳过澄清，按现有信息继续…');
  setPendingClarify(null);
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
  aiBox.dataset.convId = String(currentConvId);   // 2026-09-29：流式现场归属会话（与 sendChat 同步，切会话寄存/挂回依赖此标记）
  aiBox.innerHTML = `<span class="who">AI</span><div class="msg-inner"><div class="proc" id="proc-box"></div><div class="body streaming"><span class="typing"></span></div></div>`;
  area.appendChild(aiBox);
  scrollChatToBottom();
  _bindStickDetach();   // 2026-09-29：绑定"上滑即脱离跟读"（幂等，见 03-chat.js）
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
    stickBottom();         // 2026-09-29：贴底才跟随（用户上滑阅读时不拽回）
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
    // 2026-09-29（用户五轮反馈4）：网络错误不再整体覆盖 body，保留已生成内容 + 追加错误条 + 固化落库
    const _typ = bodyEl ? bodyEl.querySelector('.typing') : null; if(_typ) _typ.remove();
    if(bodyEl) bodyEl.classList.remove('streaming');
    if(typeof commitPartialStream === 'function') commitPartialStream(aiBox);
    if(typeof procMarkStopped === 'function') procMarkStopped();
    const errNote = document.createElement('div');
    errNote.style.cssText = 'margin-top:8px;';
    // 2026-09-29（用户三轮反馈）：失败横条补"系统现状 + 下一步"
    errNote.innerHTML = `<div style="color:var(--red);font-size:11.5px;">调用失败：${esc(e.message)}</div>`
      + `<div style="color:var(--mut);font-size:10.5px;margin-top:4px;line-height:1.6;">`
      + `本次执行已终止，不再有后台任务在跑。上方已生成内容与执行轨迹<b>已保留</b>；`
      + `确认原因（如网络波动）后，<b>直接重新发送消息</b>即可从头执行。</div>`;
    if(bodyEl) bodyEl.appendChild(errNote);
    const _cs2 = document.getElementById('chat-status'); if(_cs2) _cs2.textContent = '调用失败';
  }
}

// ── 快捷引用标签（V3：# 前缀插入知识库标签）──

// ══════════════════════════════════════════════════════════════════════════
// 2026-09-25 新增：①「待澄清」提示条（给用户明确的输入入口）
//                ②「🔍 执行详情」面板（把 Agent/技能/工具/思考/结果汇到一处查看）
// ══════════════════════════════════════════════════════════════════════════

// ── ⓪ AI 活动状态栏：让"AI 正在做什么"真正可见 ──
//  背景（2026-09-25 实测）：`#chat-status` / `#chat-conv-status` 这两个 id 在 index.html 里
//  **根本不存在**，而 03/11/12/13 等模块十余处往它们写文本（"正在识别意图并调度 Agent…"、
//  "已停止生成"、"调用失败"、意图置信度…）→ 全是空操作，对标 Codex/Claude Code 的实时状态行缺失。
//  修法：index.html 补真实元素 + 这里观察文本自动显隐（**不改动十余处既有写入点**，一处收口）。
function initChatStatusBar(){
  const el = document.getElementById('chat-status');
  const el2 = document.getElementById('chat-conv-status');
  const bar = document.getElementById('chat-status-bar');
  if(!el || !bar) return;
  const sync = ()=>{
    const t1 = (el.textContent || '').trim();
    const t2 = el2 ? (el2.textContent || '').trim() : '';
    bar.style.display = (t1 || t2) ? '' : 'none';
    // ⚠️ _streaming 定义在 12-chatsend.js（加载晚于本模块）：用 try 包住，
    //    避免 readyState 非 loading 时本模块立即执行、读到 TDZ 抛 ReferenceError
    let busy = false;
    try{ busy = (typeof _streaming !== 'undefined') && !!_streaming; }catch(e){ busy = false; }
    bar.classList.toggle('busy', busy);
    if(el2) el2.style.display = t2 ? '' : 'none';
  };
  new MutationObserver(sync).observe(el, {childList:true, characterData:true, subtree:true});
  if(el2) new MutationObserver(sync).observe(el2, {childList:true, characterData:true, subtree:true});
  sync();
}
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initChatStatusBar);
else initChatStatusBar();

// ── ① 待澄清状态：登记/撤销 + 输入框上方的「确认卡」──
//  为什么需要：澄清卡在被流式重建后会出现、刷新后又只读；而用户在**主输入框**打字时
//  既不知道"AI 正在等我确认"，也不清楚打进去算不算答复。这里把状态显式化：
//  pending 非空 → 提示条出现，且 sendChat 会把输入内容当作澄清补充走 clarify-answer。
//  2026-09-29（用户五轮反馈2）：确认卡**不再常驻会话输出流** —— 改挂输入框上方与输入框
//  衔接（dock 卡），并整体紧凑化。流内只留一行指引（renderClarifyAsk），交互集中在 dock 卡。
function setPendingClarify(p){
  window._pendingClarify = (p && (p.questions || []).length) ? p : null;
  updateClarifyHint();
}
function updateClarifyHint(){
  const dock = document.getElementById('chat-input-dock');
  if(!dock) return;
  let card = document.getElementById('clarify-ask');
  const oldBar = document.getElementById('clarify-hint');
  if(oldBar) oldBar.remove();   // 旧提示条被 dock 卡取代（一次性清理）
  const p = window._pendingClarify;
  if(!p){
    if(card) card.remove();
    return;
  }
  const qs = p.questions || [];
  const n = qs.length;
  // 已有同规模的卡 → 不重建（防重复事件把用户已选的选项抹掉）
  if(card && card.dataset.qcount === String(n)) { clarifyGo(card, parseInt(card.dataset.step||'0',10)); return; }
  if(!card){
    card = document.createElement('div');
    card.id = 'clarify-ask';
    card.className = 'clarify-ask-card dock';
    card.dataset.qcount = String(n);
    dock.insertBefore(card, dock.firstChild);
  }
  card.dataset.qcount = String(n);
  card.innerHTML = clarifyCardInnerHtml(qs, p.title);
  clarifyGo(card, 0);
}
// ── ② 执行详情面板：从该条 AI 消息已持久化的执行过程块还原结构化视图 ──
//  数据来源：消息内的 .proc 块（在线=实时时间线，历史=card_data.exec 还原），
//  因此**刷新后依然可查**，无需新增接口/新表。
// 工具结果一行摘要（截断 + 去换行）：**流式与刷新两条渲染路径共用**（见 AGENTS.md 坑 22）
function _toolSumm(s, n){
  const t = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  return t.length > (n || 60) ? t.slice(0, n || 60) + '…' : t;
}
function _procDetailBtn(){
  return `<span class="proc-x" onclick="event.stopPropagation();openExecDetail(this)" title="查看本次执行详情（Agent / 技能 / 工具 / 思考 / 结果）">🔍 详情</span>`;
}
function _execCollect(msgEl){
  const out = {meta: [], stages: [], agents: [], skills: [], tools: [], thinking: [], plans: [], others: [], card: null};
  if(!msgEl) return out;
  // 2026-09-25：优先用**落库的原始 card_data**（renderMessage 时已按消息 id 缓存）。
  // 只从 DOM 刮会漏字段：技能命中/意图/来源/置信度/provider/是否 Mock 都在 card_data 里，
  // 而 exec 只含 reasoning/agent/tools(/subtasks) —— 实测真实一轮 skill_hits=1 却显示技能 0。
  const mid = msgEl.dataset ? msgEl.dataset.mid : null;
  if(mid && window._msgCardCache && window._msgCardCache[mid]) out.card = window._msgCardCache[mid];
  const sum = msgEl.querySelector('.hist-proc .proc-summary, .proc .proc-summary');
  if(sum) out.meta.push(sum.textContent.replace(/▾|🔍 详情/g, '').trim());
  // 同时覆盖两种历史形状：process_html 路径包在 .proc 内；card_data.exec 还原路径直接是 .hist-proc。
  // 此前只写 `.proc .proc-block` → 刷新后还原的执行块**一个都取不到**，面板静默不打开（实测缺口）。
  msgEl.querySelectorAll('.proc-block').forEach(blk => {
    const titleEl = blk.querySelector('.proc-title');
    const title = titleEl ? (titleEl.textContent || '').trim() : '';
    const statusEl = blk.querySelector('.st');
    const status = statusEl ? (statusEl.textContent || '').trim() : '';
    const elapsedEl = blk.querySelector('.tool-elapsed');
    const elapsed = elapsedEl ? (elapsedEl.textContent || '').trim() : '';
    const bodyEl = blk.querySelector('.proc-body');
    const body = bodyEl ? bodyEl.innerHTML : '';
    const item = {title, status, elapsed, body};
    if(blk.classList.contains('thinking')) out.thinking.push(item);
    else if(blk.classList.contains('tool')) out.tools.push(item);
    else if(blk.classList.contains('skill')) out.skills.push(item);
    else if(blk.classList.contains('agent')) out.agents.push(item);
    else if(blk.classList.contains('orchestrate')) out.plans.push(item);
    else if(blk.classList.contains('stage')) out.stages.push(item);
    else out.others.push(item);
  });
  return out;
}
function openExecDetail(el){
  const msgEl = el ? el.closest('.msg') : document.querySelector('#chat-area .msg.ai:last-of-type');
  const d = _execCollect(msgEl);
  const total = d.stages.length + d.agents.length + d.skills.length + d.tools.length
              + d.plans.length + d.thinking.length + d.others.length;
  if(!total){
    toast('本条回复没有可展示的执行过程（可能是直接问答或历史数据较早）');
    return;
  }
  const failN = d.tools.filter(t=>/✗|失败|error/i.test(t.status)).length;
  // 2026-09-25：以**落库 card_data** 为准补齐"调用与模型"一栏（技能/意图/来源/置信度/模型/Mock/HIL）。
  // 这些字段 exec 里没有，只从 DOM 刮必然漏（实测真实一轮 skill_hits=1 显示为 0）。
  const cd = d.card || {};
  const sk = (cd.skill_hits || []).length;
  const metaRows = [
    ['意图', cd.intent ? `${cd.intent}${cd.intent_confidence != null ? `（置信度 ${Math.round((cd.intent_confidence||0)*100)}%）` : ''}` : ''],
    ['智能体', cd.agent || d.agents.map(a=>a.title.replace('子智能体：','')).join('、')],
    ['技能命中', sk ? `${sk} 个` : '无'],
    ['检索来源', cd.source ? `${cd.source}${cd.graph_count || cd.vector_count ? `（图谱 ${cd.graph_count||0} / 向量 ${cd.vector_count||0}）` : ''}` : ''],
    ['模型', cd.provider ? `${cd.provider}${cd.used_mock ? '（Mock 回落）' : ''}` : ''],
    ['HIL 等级', cd.hil_level != null ? String(cd.hil_level) : ''],
    ['引用来源', (cd.citations || []).length ? `${(cd.citations||[]).length} 条` : ''],
  ].filter(r => r[1]);
  const sec = (icon, name, arr, cls) => !arr.length ? '' :
    `<div class="xd-sec"><div class="xd-h">${icon} ${name}<span class="xd-n">${arr.length}</span></div>
      ${arr.map(t=>`<div class="xd-item ${cls}">
        <div class="xd-t"><b>${esc(t.title)}</b>${t.elapsed?`<span class="xd-e">${esc(t.elapsed)}</span>`:''}${t.status?`<span class="xd-s">${esc(t.status)}</span>`:''}</div>
        ${t.body?`<div class="xd-b">${t.body}</div>`:''}</div>`).join('')}</div>`;
  const html = `
    <div class="xd-ov">
      <div class="xd-ov-row"><span>执行概览</span><b>${esc(d.meta.join(' · ') || (total + ' 个环节'))}</b></div>
      <div class="xd-kv">
        <span>工具调用 <b>${d.tools.length}</b>${failN?`<i class="xd-bad">（${failN} 失败）</i>`:''}</span>
        <span>技能 <b>${sk || d.skills.length}</b></span>
        <span>子智能体 <b>${d.agents.length || (cd.agent?1:0)}</b></span>
        <span>阶段 <b>${d.stages.length}</b></span>
      </div>
      ${metaRows.length?`<div class="xd-meta">${metaRows.map(r=>`<div class="xd-mr"><span>${r[0]}</span><b>${esc(String(r[1]))}</b></div>`).join('')}</div>`:''}
      <div class="xd-tip">数据来自本条回复已落库的 card_data / exec（刷新后仍可查看）。跨会话/跨时间的统计请在「运行监控中心」「审计日志」查看。</div>
    </div>
    ${(!d.agents.length && cd.agent)?sec('🤖','子智能体',[{title:'子智能体：'+cd.agent,status:'✓ 已完成',body:''}],'agent'):''}
    ${sec('🤖','子智能体', d.agents, 'agent')}
    ${sec('🧩','技能注入', d.skills, 'skill')}
    ${sec('📋','任务计划/子任务', d.plans, 'plan')}
    ${sec('🛠','工具调用', d.tools, 'tool')}
    ${sec('💭','LLM 思考', d.thinking, 'think')}
    ${sec('🔍','阶段', d.stages, 'stage')}
    ${sec('•','其它环节', d.others, 'other')}
    <div class="form-actions" style="justify-content:flex-start;gap:8px;">
      <button class="btn sm ghost" onclick="copyExecDetail()">📋 复制为 Markdown</button>
    </div>`;
  openPanel('🔍 执行详情', html);
  window._execLast = d;
}
function copyExecDetail(){
  const d = window._execLast;
  if(!d){ toast('无可复制内容'); return; }
  const L = [];
  if(d.meta.length) L.push('## 执行概览\n' + d.meta.join(' · '));
  const cd = d.card || {};
  const mv = [['意图', cd.intent], ['智能体', cd.agent],
              ['技能命中', (cd.skill_hits||[]).length ? `${(cd.skill_hits||[]).length} 个` : ''],
              ['检索来源', cd.source], ['模型', cd.provider ? `${cd.provider}${cd.used_mock?'（Mock 回落）':''}` : ''],
              ['HIL 等级', cd.hil_level]].filter(r=>r[1]!=null && r[1]!=='');
  if(mv.length) L.push('\n## 调用与模型\n' + mv.map(r=>`- ${r[0]}：${r[1]}`).join('\n'));
  const dump = (name, arr) => { if(arr.length){ L.push(`\n## ${name}`); arr.forEach(t=>{
    L.push(`- ${t.title}${t.status?' | '+t.status:''}${t.elapsed?' | '+t.elapsed:''}`); }); } };
  dump('子智能体', d.agents); dump('技能', d.skills); dump('任务计划', d.plans);
  dump('工具调用', d.tools); dump('阶段', d.stages); dump('其它', d.others);
  if(d.thinking.length){ L.push('\n## LLM 思考');
    d.thinking.forEach(t=>{ const div=document.createElement('div'); div.innerHTML=t.body||''; L.push(div.textContent||''); }); }
  const txt = L.join('\n');
  try{
    navigator.clipboard.writeText(txt).then(()=>toast('已复制执行详情'), ()=>fallbackCopy(txt));
  }catch(e){ fallbackCopy(txt); }
}
function fallbackCopy(txt){
  const ta = document.createElement('textarea');
  ta.value = txt; ta.style.position='fixed'; ta.style.left='-9999px';
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand('copy'); toast('已复制执行详情'); }catch(e){ toast('复制失败，请手动选择'); }
  ta.remove();
}
