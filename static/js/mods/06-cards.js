/* 消息卡：富卡片 / SysML 卡 / 回写
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 2526-2855  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function procBlocksHtml(ex, mid){
  if(!ex) return '';
  const now = new Date().toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const uid = (mid || 'h' + Math.random().toString(36).slice(2,8));
  let steps = [];
  if(ex.reasoning) steps.push({icon:'💭', title:'思考过程', status:'done', body: esc(ex.reasoning), cls:'thinking'});
  if(ex.agent) steps.push({icon:'🤖', title:`子智能体：${esc(ex.agent)}`, status:'done', sub:'✓ 已完成', cls:'agent', noBody: true});
  const subs = ex.subtasks||[];
  if(subs.length){
    const doneN = subs.filter(s=>s.status==='done').length;
    const failN = subs.length - doneN;
    // V2.7 文本计划清单（替代 DAG 画布）：每行 状态 + 标题 + Agent + 耗时
    const planList = '<div style="display:flex;flex-direction:column;gap:5px;padding:2px 0;">' +
      subs.map((s,i)=>{
        const run = s.status==='run';
        const stTxt = run ? '执行中' : (s.status==='failed' ? '失败' : (s.status==='partial' ? '部分完成' : '完成'));
        const stCls = run ? 'w' : (s.status==='failed' ? 'r' : 'ok');
        const spin = run ? '<span class="proc-spin"></span>' : '';
        const dep = (s.deps&&s.deps.length) ? '<span style="color:var(--mut);font-size:10px;">依赖: '+esc(s.deps.join('、'))+'</span>' : '';
        const lat = s.latency_ms ? '<span style="color:var(--mut);font-size:10px;">⏱ '+(s.latency_ms/1000).toFixed(1)+'s</span>' : '';
        return '<div style="display:flex;align-items:center;gap:6px;font-size:11px;flex-wrap:wrap;">'
          + '<span style="color:var(--mut);font-size:10px;min-width:16px;">'+(i+1)+'.</span>'
          + '<span class="st '+stCls+'" style="min-width:56px;text-align:center;">'+spin+stTxt+'</span>'
          + '<b style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+esc(s.title||s.key||('t'+(i+1)))+'</b>'
          + (s.agent?'<span style="color:var(--mut);font-size:10px;">'+esc(s.agent)+'</span>':'')
          + lat + dep + '</div>';
      }).join('') + '</div>';
    const body = planList + subs.map(s=>subtaskCardHtml(s)).join('');
    steps.push({icon:'📋', title:`计划清单（${subs.length} 任务：${doneN} 完成${failN?` / ${failN} 失败`:''}）`, status:'done', sub:`${doneN}/${subs.length}`, body, cls:'orchestrate'});
  }
  (ex.tools||[]).forEach((t, i)=>{
    const statusCls = t.ok===true ? 'ok' : (t.ok===false ? 'err' : 'run');
    const statusText = t.ok===true ? '✓ 成功' : (t.ok===false ? '✗ 失败' : '⏱ 执行中');
    const cls = 'tool' + (t.ok===true ? ' success' : (t.ok===false ? ' error' : ''));
    const res = t.error || t.result || '-';
    // 2026-09-25（对齐 Claude Code / Codex，且遵守"两条渲染路径成对维护"）：头部一行结果摘要。
    // ⚠️ ① 不加 ✓/✗ 前缀 —— 头部已有 statusText（"✓ 成功/✗ 失败"）与状态徽章，加了会三处重复；
    //    ② 这里 s.sub 在下面模板中是**未转义**插值（原有 sub 均为字面量），故必须先 esc()。
    const subTxt = (res && String(res) !== '-')
      ? ' · ' + esc(typeof _toolSumm === 'function' ? _toolSumm(res, 60) : String(res).slice(0, 60))
      : '';
    let body = (t.arguments?`<div class="proc-tool-param">参数：${esc(typeof t.arguments==='string'?t.arguments:JSON.stringify(t.arguments))}</div>`:'');
    // P0-2 历史工具失败：三段式错误面（why/next 由后端持久化或前端推断）
    if(t.ok===false && t.error){
      const what = String(t.error).slice(0,120);
      body += `<div class="errface">
        <div class="ef-row"><span class="ef-k">发生了什么：</span><span class="ef-v">${esc(what)}</span></div>
        <div class="ef-row"><span class="ef-k">为什么：</span><span class="ef-v">${esc(t.why || _toolErrWhy(t.error))}</span></div>
        <div class="ef-row"><span class="ef-k">下一步：</span><span class="ef-v">${esc(t.next || '可复制错误详情，在输入框反馈让 AI 换一种方式继续执行')}</span></div>
        <div class="ef-act"><span class="pr-act" style="color:var(--blue-d);" onclick="copyToolError(this)">📋 复制错误详情</span></div>
      </div>`;
    }
    // P0-2 历史工具结果：截断展示可展开/复制（truncated 标记随 card_data.exec 持久化）
    // V2.7：失败且已有错误面时跳过结果区（错误信息已在 errface 展示，避免重复占位）
    if(!(t.ok===false && t.error)){
      // SRS-GN-CO（2026-09-21）：覆盖性工具结果渲染为行业风格富组件（DOORS 矩阵/Cameo 链/Polarion 风险分级），
      // 内嵌「预览大图」入口；JSON 截断或非覆盖工具时回退原文本展示
      const covHtml = (typeof covTryCard === 'function') ? covTryCard(t.name, t.result, t.arguments) : null;
      if(covHtml){
        body += `<div class="proc-tool-result" style="border:none;padding:0;">${covHtml}</div>`;
      } else {
        body += `<div class="proc-tool-result ${t.truncated?'trunc':''}">
        <div class="pr-head"><span>结果</span>${t.truncated?`<span class="pr-act" onclick="toolExpand(this)">展开全文</span>`:''}<span class="pr-act" onclick="copyToolResult(this)">📋 复制</span></div>
        <div class="pr-body">${esc(String(res))}</div></div>`;
      }
    }
    // P0-1 历史工具耗时（exec 持久化 elapsed_ms 时显示）
    const elapsedSub = t.elapsed_ms ? ` · ⏱ ${(t.elapsed_ms/1000).toFixed(1)}s` : '';
    steps.push({icon:'🛠', title:`调用 ${esc(t.name)}`, status:t.ok===true?'done':(t.ok===false?'error':'run'), sub:statusText+elapsedSub+subTxt, body, cls, badge: statusText, badgeCls: statusCls});
  });
  // V2.6 历史消息执行过程：统一收口条（默认收起）+ 时间线容器（点击展开按层级回看）
  const timeline = steps.map((s,i)=>`<div class="proc-block collapsed ${s.cls||''}"><div class="proc-head" onclick="toggleProc(this)"><span>${s.icon}</span><span class="proc-title">${s.title}</span>${s.sub?`<span class="proc-sub">${s.sub}</span>`:''}${s.badge?`<span class="tool-status ${s.badgeCls}">${s.badge}</span>`:''}<span class="chev">▾</span><span class="proc-time">${now}</span></div>${s.noBody?'':`<div class="proc-body">${s.body||''}</div>`}</div>`).join('');
  if(!steps.length) return '';
  const failN = (ex.tools||[]).filter(t=>t.ok===false).length;
  const sumTxt = `✓ 已完成 · ${steps.length} 环节${failN?` · ${failN} 失败`:''}`;
  // 2026-09-25：历史还原路径也要带「🔍 详情」入口 —— 此前只有流式当时的收口条注入了按钮，
// 刷新后（走本函数从 card_data.exec 还原）按钮消失，同一份数据两套呈现（前后端一致性缺口，实测）。
  return `<div class="hist-proc"><div class="proc-summary collapsed" onclick="histProcToggle(this)">${sumTxt}${(typeof _procDetailBtn === 'function') ? _procDetailBtn() : ''}<span class="chev">▾</span></div><div class="proc-timeline">${timeline}</div></div>`;
}

// ── P0-1/P0-2：编排结果卡（子任务轨迹摘要 + 另存为/打开流程） ──
function renderOrchCard(cd){
  const plan = cd.plan||[];
  const doneN = plan.filter(p=>p.status==='done').length;
  const failN = plan.length - doneN;
  const degraded = cd.degraded ? `<span class="st w" style="margin-left:4px;">降级</span>` : '';
  // Task 14：编排汇总状态徽章（card_data.orchestrated_status：full/partial/failed）
  const osc = cd.orchestrated_status;
  const oscBadge = osc==='full' ? '<span class="st ok" style="margin-left:4px;">✅ 完整</span>'
    : osc==='partial' ? '<span class="st w" style="margin-left:4px;">⚠️ 部分完成</span>'
    : osc==='failed' ? '<span class="st r" style="margin-left:4px;">✗ 失败</span>' : '';
  // Task 14：编排部分完成 → 交付物缺失提示（N = 非 done 子任务数）
  let partialHint = '';
  if(osc === 'partial'){
    const missN = plan.filter(p=>p.status && p.status!=='done').length;
    if(missN) partialHint = `<div style="margin-top:5px;color:var(--amb);font-size:11px;">⚠️ 编排部分完成，${missN} 个交付物缺失（建议补充缺失子任务后重跑或人工补录）</div>`;
  }
  // 子任务轨迹列表（状态徽章/专长标签/摘要前 60 字/耗时/错误）
  const rows = plan.map(p=>subtaskCardHtml(p)).join('');
  // P1-2 历史编排：run 级预算进度条（done 事件 run_budget 已随 card_data 持久化）
  const rb = cd.run_budget;
  let budgetHtml = '';
  if(rb && (rb.used_tokens||0) > 0){
    const pct = Math.min(100, Math.round((rb.used_tokens||0)/(rb.budget||1)*100));
    budgetHtml = `<div style="display:flex;align-items:center;gap:6px;font-size:10.5px;color:var(--mut);margin-top:5px;">
      <span title="本运行 token 用量">🪙 ${Number(rb.used_tokens).toLocaleString()}/${Number(rb.budget||0).toLocaleString()} tokens</span>
      <span style="width:90px;height:5px;background:var(--line,#e5e8ee);border-radius:3px;overflow:hidden;display:inline-block;"><span style="display:block;height:100%;width:${pct}%;background:${pct>=100?'var(--red,#d93636)':pct>=70?'var(--amber,#c98a2e)':'var(--grn,#3b6d11)'};"></span></span>
      <span>${pct}%</span>${rb.hit?'<span class="st w">预算触顶</span>':''} · 子任务 ${rb.executed||0}/${rb.max_tasks||'-'}
    </div>`;
  }
  // P0-1（T9）：反思闭环评审展示（评分/通过/修订轮次/问题数）
  const ref = cd.reflection;
  let refHtml = '';
  if(ref && ref.enabled && ref.score !== undefined && ref.score !== null){
    const rOk = ref.passed;
    refHtml = `<div class="proc-line" style="padding:2px 0;font-size:11px;color:var(--mut);">🔍 质量评审：<b style="color:${rOk?'var(--grn)':'var(--amb)'};">${ref.score}/100</b> ${rOk?'<span class="st ok">✓ 通过</span>':'<span class="st w">未通过</span>'}${ref.rounds>1?` · 修订 ${ref.rounds-1} 轮`:''}${(ref.issues||[]).length?` · ${ref.issues.length} 项问题`:''}${ref.degraded?' · <span class="st w">降级</span>':''}${ref.issues&&ref.issues.length?`<div style="color:var(--mut);font-size:10.5px;margin-top:2px;">${esc(ref.issues.slice(0,3).join('；'))}</div>`:''}</div>`;
  }
  // 2026-09-29（用户反馈）：AI 输出完成后，最下方平铺的子任务卡片"那一坨"默认收起 ——
  //   只留一行汇总 chip（🕸 自动编排 · N 个子任务 · 完成 M + 状态徽章），点击展开明细。
  //   此前 rows 默认展开，与已收起的执行过程收口条（V2.6）设计不一致，完成后霸屏。
  return `<div class="rc">
    <div class="rc-chip b" style="cursor:pointer;user-select:none;" title="点击展开/收起子任务明细" onclick="orchDetailToggle(this)">🕸 自动编排 · ${plan.length} 个子任务 · ${doneN} 完成${failN?` / ${failN} 失败`:''}${degraded}${oscBadge}<span class="chev" style="margin-left:6px;font-size:9px;">▾</span></div>
    ${partialHint}
    ${budgetHtml}
    ${refHtml}
    <div class="proc-body orch-detail" style="display:none;">${rows}</div>
  </div>`;
}
// 编排卡子任务明细：展开/收起（chip 行点击）
function orchDetailToggle(chipEl){
  const body = chipEl.parentElement && chipEl.parentElement.querySelector('.orch-detail');
  if(!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  const ch = chipEl.querySelector('.chev');
  if(ch) ch.style.transform = open ? 'rotate(90deg)' : '';
}
/* ── 富卡片渲染（FR-HIL-1/2 中间结果逐条确认 · FR-CIA · FR-VR/VC 可视化） ── */
function renderRichCard(type, cd, content) {
  if(!cd) return '';
  // 2026-09-16：impact 卡优先于 sysml_views 路由——影响分析不带「代码/视图/归一校验」
  // （那是 AI 建模能力；历史消息 card_data 里残留的 sysml_views 不再劫持渲染）
  if(type==='card_impact' || (type==='impact' && (cd.impact_nodes||cd.impact_edges))){
    if((cd.impact_nodes||[]).length && typeof cardImpactSummary === 'function'){
      return cardImpactSummary(cd);
    }
    return cardImpact(cd);   // 兜底：异常/空数据走旧简缩卡
  }
  // CIA 异常引导：无变更源 / 歧义等结构化错误（FR-CIA 场景级三段式反馈）
  if(cd.ok === false && cd.code) return cardImpactGuide(cd);
  // SysML v2 视图预览：AI 建模生成代码时随消息即时投影（优先展示；代码+视图 tab 整合，聚焦同步查看）
  if(cd.sysml_views) return cardSysmlTabs(cd, content);
  // 2026-09-23：需求分析卡「空态抑制」——candidates 与 conflicts 均为空时不渲染。
  // 原条件里的 `cd.candidates||cd.conflicts` 在空数组下也为真（JS 里 [] 是 truthy），
  // 于是每次都挂一张「0 条候选条目 / ✓无冲突 / 暂无候选条目」的空转卡。
  // 数据源见 agent/pipeline_parts/cards.py::_card_candidates（entities 需求候选 + CONFLICTS 关系），
  // 实测全库恒 0 行；将来候选需求生产链路跑通，卡片会自动恢复渲染，无需再改这里。
  if(type==='card_candidates' || type==='requirement_analysis'){
    if(!(cd.candidates||[]).length && !(cd.conflicts||[]).length) return '';
    return cardCandidates(cd);
  }
  if(type==='card_review' || (type==='review' && cd.score!==undefined)) return cardReview(cd);
  return '';
}

/* ── SysML v2 视图预览卡：按意图投影 → 缩略图网格 → 点击弹窗展示大图（不依赖知识库落库） ── */
/* 缩略图网格构建（视图 tab 与独立视图卡共用）：Cytoscape 迷你实例（可交互），点击打开大图弹窗 */
/* 时间戳使用局部变量：同一会话多条消息并发渲染时，若用共享全局会被后渲染的卡覆盖，
   导致先渲染卡片的 setTimeout 回调查不到容器而漏渲染（极端多卡场景实测暴露）。 */
/* P0 性能修复：
   1) 视图模型 JSON 不再 URI 编码内联到 data-v attribute（中文3倍膨胀、HTML 解析慢、内存翻倍），
      改存内存 store（_svmVmStore），DOM 只带轻量 data-vk 索引；
   2) 缩略图 Cytoscape 不再 setTimeout 一次性全量同步挂载（多卡场景主线程阻塞），
      改 IntersectionObserver 进入视口才挂载（对齐预览区 renderSysMLInPreview 的懒挂载模式）。 */
let _svmVmSeq = 0;
const _svmVmStore = new Map();          // key → 视图模型 vm（缩略图数据源）
function svmVmStorePrune(){ _svmVmStore.clear(); }
let _svmThumbObs = null;
function _svmThumbEnsureObs(){
  if(_svmThumbObs) return _svmThumbObs;
  _svmThumbObs = new IntersectionObserver((entries)=>{
    entries.forEach(en=>{
      if(!en.isIntersecting) return;
      const th = en.target;
      _svmThumbObs.unobserve(th);       // 只挂载一次
      svmThumbMount(th);
    });
  }, { root: null, threshold: 0 });
  return _svmThumbObs;
}
// 挂载单个缩略图的 Cytoscape（从 store 取视图数据；容器不可见/数据缺失时静默跳过）
function svmThumbMount(th){
  if(!th || !th.isConnected) return;
  const box = th.querySelector('.sv-thumb-svg');
  if(!box || box.childElementCount) return;             // 已挂载
  const vm = _svmVmStore.get(th.dataset.vk);
  if(!vm || !(vm.nodes||[]).length) return;
  if(!box.clientHeight) return;                          // 容器无尺寸（隐藏 pane）→ 由切 tab 兜底
  box.classList.add('cy-loading','cy-loading-thumb');    // v6.4：缩略图加载中显示 spinner
  // v6.4 P0：整体 idle 化（缩略图小但仍需 50-80ms init；批量挂载时不抢主线程）
  window.__cyIdleMount(box, ()=>{
    const cy = svmRenderCytoscape(box, vm);
    if(cy) window.__cyIdleRun(()=>{ try{ cy.fit(undefined, 20); }catch(e){} });
    return cy;
  }, null);
}
function svmVmFromEl(el){
  // 优先内存 store；无 data-vk 的历史 DOM（升级前渲染）回退解析 data-v
  const k = el && el.dataset ? el.dataset.vk : '';
  if(k && _svmVmStore.has(k)) return _svmVmStore.get(k);
  try{ return JSON.parse(decodeURIComponent(el.dataset.v||'')); }catch(e){ return null; }
}
function svmThumbsHtml(sv) {
  const entries = Object.entries(sv.views||{});
  if(!entries.length) return '';
  const _ts = Date.now();
  const thumbs = entries.map(([t,v])=>{
    const cnt = (v.nodes||[]).length;
    const thumbId = 'svm-thumb-'+t+'-'+_ts;
    const vk = 'vmk-' + (_svmVmSeq++);
    _svmVmStore.set(vk, v);             // 视图数据入内存 store（DOM 只带轻量 key）
    return `<div class="sv-thumb${cnt?'':' empty'}" title="${esc(v.view.name)}（${esc(v.view.v1)}）· 点击查看大图" data-vk="${vk}" onclick="openSvmPreview(this)">
      <div class="sv-thumb-svg" id="${thumbId}" style="height:170px;"></div>
      <div class="sv-thumb-cap"><b>${t}</b><span>${esc(v.view.name.split('（')[0])}</span><i class="sv-cnt">${cnt} 元素</i></div>
    </div>`;
  }).join('');
  // 懒挂载：缩略图进入视口才创建 Cytoscape（消息流内大量缩略图不再一次性全挂）
  requestAnimationFrame(()=>{
    document.querySelectorAll('.sv-thumb[data-vk]:not([data-vk=""])').forEach(th=>{
      _svmThumbEnsureObs().observe(th);
    });
  });
  return `<div class="sv-thumbs">${thumbs}</div>`;
}
function cardSysmlViews(cd) {
  const sv = cd.sysml_views;
  if(!sv || !sv.views) return '';
  const parsed = sv.parsed || {nodes:0, edges:0};
  const entries = Object.entries(sv.views);
  if(!entries.length) return '';
  return `<div class="sv-card">
    <div class="rc-chip b">🧩 SysML 视图预览<span style="margin-left:6px;font-weight:400;color:var(--mut);">按「${esc(sv.intent||'模型')}」意图即时投影 · ${parsed.nodes} 元素 / ${parsed.edges} 关系 · 点击缩略图查看大图</span></div>
    ${svmThumbsHtml(sv)}
  </div>`;
}

/* ── 代码/视图 tab 整合卡：V2 代码与对应视图在同一卡片内 tab 切换（聚焦同步查看） ── */
/* 提取消息内容中的 SysML v2 代码块（与后端 _extract_sysml_code 逻辑对齐，返回拼接代码或 null）
   2026-09-17 修复：两遍策略——先精确匹配带 sysml/kerml 标签的围栏；无标签兜底时逐块校验 V2 语态，
   避免前文 mermaid 等围栏的闭合 ``` 被误判为开启围栏、吞掉真正的 sysml 块（与后端同步修复） */
function extractSysmlCode(text) {
  if(!text) return null;
  const featRe = /\b(?:part|requirement|action|attribute|interface|package|state|constraint|port)\s+(?:def|usage)\b| satisfies /;
  let m, code = [];
  const reLabeled = /```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)\s*\n([\s\S]*?)```/gi;
  while((m = reLabeled.exec(text))){ if(m[1] && m[1].trim()) code.push(m[1].trim()); }
  if(!code.length && featRe.test(text)){
    const rePlain = /```\s*\n([\s\S]*?)```/g;
    while((m = rePlain.exec(text))){
      if(m[1] && m[1].trim() && featRe.test(m[1])) code.push(m[1].trim());
    }
  }
  return code.length ? code.join('\n\n') : null;
}
/* 消息正文剥离代码块（仅剥离与 extractSysmlCode 捕获一致的 sysml/kerml/无标签块；其他语言代码块保留正文展示） */
function stripFencedCode(text) {
  if(!text) return text;
  return String(text).replace(/```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n[\s\S]*?```/gi, '');
}
/* 整合卡渲染：tab = 代码 | 视图（默认聚焦代码，视图一键切换查看） */
function cardSysmlTabs(cd, content) {
  const sv = cd.sysml_views;
  if(!sv || !sv.views) return '';
  const entries = Object.entries(sv.views);
  if(!entries.length) return '';
  const parsed = sv.parsed || {nodes:0, edges:0};
  const code = extractSysmlCode(content) || '';
  const codeCnt = content ? Math.floor((String(content).match(/```/g)||[]).length / 2) : 0;
  const codePane = code
    ? `<pre class="svm-code"><code>${escMd(code)}</code></pre>`
    : `<div style="color:var(--mut);font-size:12px;padding:14px;text-align:center;">未检测到 SysML v2 代码块</div>`;
  return `<div class="sv-card svm-card-wrap">
    <div class="svm-tabs">
      <span class="svm-tab active" data-tab="code" onclick="svmTabSwitch(this)" title="SysML v2 代码">代码<i class="sv-cnt">${codeCnt>0?codeCnt:''}</i></span>
      <span class="svm-tab" data-tab="view" onclick="svmTabSwitch(this)" title="按「${esc(sv.intent||'模型')}」意图即时投影">视图<i class="sv-cnt">${entries.length}</i></span>
      <span style="flex:1;display:flex;align-items:center;justify-content:flex-end;font-size:10.5px;color:var(--mut);padding-right:6px;">按「${esc(sv.intent||'模型')}」意图投影 · ${parsed.nodes} 元素 / ${parsed.edges} 关系</span>
      <button class="btn sm ghost" style="padding:1px 8px;font-size:10.5px;flex:none;" onclick="svmCopyCode(this)" title="复制本消息全部 SysML v2 代码">📋 复制</button>
      <button class="btn sm ghost" style="padding:1px 8px;font-size:10.5px;flex:none;color:var(--grn,#2f855a);border-color:#C0DD97;" onclick="openNormReport(this)" title="查看本次生成的归一校验报告（术语词典对齐 / 同名消歧 / 未命中建议）">🧹 归一校验</button>
    </div>
    <div class="svm-pane" data-pane="code">${codePane}</div>
    <div class="svm-pane" data-pane="view" style="display:none;">${svmThumbsHtml(sv)}</div>
  </div>`;
}
/* 2026-09-29（用户反馈）：AI 建模输出代码支持一键复制 —— 正文里的 sysml 代码块会被
   stripFencedCode 剥离进本卡（extractSysmlCode 提取），代码 tab 是唯一展示入口，故复制按钮挂这里。
   剪贴板 API 不可用（http 部署/旧内核）时回退 execCommand，不静默失败。 */
function svmCopyCode(btn){
  const card = btn.closest('.sv-card');
  const codeEl = card && card.querySelector('.svm-pane[data-pane="code"] code');
  const text = codeEl ? (codeEl.textContent||'') : '';
  if(!text.trim()){ toast('未检测到可复制的 SysML 代码'); return; }
  const done = ()=>{ const t = btn.textContent; btn.textContent = '✓ 已复制';
    setTimeout(()=>{ btn.textContent = t; }, 1500); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(()=>_svmCopyFallback(text, done));
  } else {
    _svmCopyFallback(text, done);
  }
}
function _svmCopyFallback(text, done){
  try{
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.cssText = 'position:fixed;opacity:0;';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if(ok) done(); else toast('复制失败，请手动选择代码复制');
  }catch(e){ toast('复制失败，请手动选择代码复制'); }
}
/* tab 切换：仅切换卡片内 .svm-tab / .svm-pane（不影响其他消息） */
function svmTabSwitch(tabEl) {
  const card = tabEl.closest('.sv-card');
  if(!card) return;
  const name = tabEl.dataset.tab;
  card.querySelectorAll('.svm-tab').forEach(t=>t.classList.toggle('active', t===tabEl));
  card.querySelectorAll('.svm-pane').forEach(p=>{ p.style.display = (p.dataset.pane===name) ? '' : 'none'; });
  // 切到视图 tab 时兜底渲染：若缩略图尚未挂载（懒加载观察器未触发或 pane 此前隐藏），按 store 补渲染
  if(name==='view'){
    card.querySelectorAll('.sv-thumb').forEach(th=>{ svmThumbMount(th); });
  }
}
function wbModalHtml(vCount) {
  return `<div style="font-size:12px;line-height:1.9;color:var(--mut);margin-bottom:8px;">
    通过<b>标准写回接口</b>将本消息的 <b>${vCount||0} 个 SysML 视图</b> 写回外部建模软件（MagicDraw / Cameo 等）。<br>
    写回流程内置两道校验：<b>① 归一校验</b>（术语词典对齐 + 同名消歧，确保元素符合本体规范）→ <b>② 一致性检查</b>（重名 / 悬空引用 / 循环）→ 校验通过才写入目标。</div>
  <div style="font-size:10.5px;color:var(--mut);margin-top:6px;line-height:1.7;">写回方式：仅通过接口（无手工导入通道）。校验未通过时接口返回问题清单，不执行写入。此处为前端交互演示，未连接真实工具。</div>
  <div id="wb-progress" style="margin-top:10px;font-size:12px;line-height:2;min-height:24px;"></div>
  <div style="margin-top:12px;display:flex;justify-content:flex-end;gap:8px;">
    <button class="btn ghost" onclick="closePanel()">取消</button>
    <button class="btn" id="wb-start" onclick="wbStart()">🚀 通过接口写回</button>
  </div>`;
}
function wbStart() {
  const box = document.getElementById('wb-progress'); if(!box) return;
  const start = document.getElementById('wb-start'); if(start) start.disabled = true;
  const vN = (window._wb && window._wb.vCount) || 0;
  const steps = [
    '① 调用写回接口（POST /api/model/writeback）',
    '② 归一校验：术语词典对齐 '+vN+' 个元素 · 同名消歧合并',
    '③ 一致性检查：重名 0 · 悬空引用 0 · 循环 0',
    '④ 校验通过，写入目标工具…',
    '✅ 写回完成'
  ];
  box.innerHTML = '<div style="color:var(--blue-d);">⏳ 执行中…</div>';
  let i = 0;
  if(window._wbTimer) clearInterval(window._wbTimer);
  window._wbTimer = setInterval(()=>{
    if(i >= steps.length){ clearInterval(window._wbTimer); wbDone(); return; }
    box.innerHTML = steps.slice(0, i+1).map((s,k)=>
      `<div style="color:${k<i?'var(--grn,#2f855a)':'var(--ink,#223)'};">${k<i?'✅':(k===i?'⏳':'')} ${s}</div>`).join('');
    i++;
  }, 620);
}
function wbDone() {
  const box = document.getElementById('wb-progress'); if(!box) return;
  box.innerHTML = `<div style="margin-top:6px;border:1px solid #C0DD97;background:#f7fbee;border-radius:8px;padding:12px;font-size:12px;">
    <b style="color:var(--grn,#2f855a);">✅ 写回完成（前端演示）</b>
    <div style="margin-top:6px;line-height:1.9;">
      <div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">写回接口</span><span>/api/model/writeback</span></div>
      <div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">归一校验</span><span style="color:var(--grn,#2f855a);">通过（对齐 5 · 消歧 2）</span></div>
      <div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">一致性检查</span><span style="color:var(--grn,#2f855a);">通过（重名 0 · 悬空 0）</span></div>
      <div style="display:flex;justify-content:space-between;padding:3px 0;"><span style="color:var(--mut);">写入目标</span><span>MagicDraw / Cameo（SysML v2）</span></div>
    </div>
    <div style="margin-top:8px;display:flex;gap:6px;">
      <button class="btn sm ghost" onclick="wbSimulateFail()">⚠️ 模拟校验失败分支</button>
    </div>
  </div>`;
  const start = document.getElementById('wb-start'); if(start){ start.disabled=false; start.textContent='↻ 重新写回'; }
}
function wbSimulateFail() {
  if(window._wbTimer) clearInterval(window._wbTimer);   // 失败分支先停定时器，防被 wbDone 覆盖
  const box = document.getElementById('wb-progress'); if(!box) return;
  box.innerHTML = `<div style="margin-top:6px;border:1px solid #F3C1C1;background:#fff5f5;border-radius:8px;padding:12px;font-size:12px;">
    <b style="color:var(--red);">✕ 校验未通过，接口拒绝写回</b>
    <div style="margin-top:6px;line-height:1.9;">
      <div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">归一校验</span><span style="color:var(--red);">2 个元素未命中词典</span></div>
      <div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">一致性检查</span><span style="color:var(--red);">1 处悬空引用（TWTA → ?）</span></div>
      <div style="display:flex;justify-content:space-between;padding:3px 0;"><span style="color:var(--mut);">写入</span><span style="color:var(--red);">未执行（校验阻断）</span></div>
    </div>
    <div style="color:var(--mut);margin-top:6px;">请先修正问题（可在「本体模型维护」补齐类型 / 术语词典添加词条），再重新发起写回。</div>
  </div>`;
}
// ── 🧹 归一校验报告（2026-09-08 P0-1/P0-2 真实后端版）：
//    归一/消歧报告 → 人在回路确认 → 应用回 V2 代码与视图（新版本，不覆盖）→ 闸口② 写入建模软件
// 行匹配状态 → 色块（与后端 sysml_importer.build_normalize_report 输出对齐）
const _NR_STATUS_META = {
  matched_high: { color:'#2f855a', bg:'#f7fbee', bd:'#C0DD97', label:'已对齐', icon:'✅' },
  matched_low:  { color:'#c98a2e', bg:'#fffaf0', bd:'#FAC775', label:'低置信', icon:'⚠️' },
  dup_high:     { color:'#7c3aed', bg:'#f5f3ff', bd:'#DDD6FE', label:'建议合并', icon:'🔀' },
  dup_suspect:  { color:'#d97706', bg:'#fef3c7', bd:'#FCD9A1', label:'批内疑似', icon:'❓' },
  none:         { color:'#1d4ed8', bg:'#eff6ff', bd:'#93C5FD', label:'未命中', icon:'＋' },
  rejected:     { color:'#6b7280', bg:'#f3f4f6', bd:'#d1d5db', label:'校验拒', icon:'⛔' },
};
// 建议类型（AI 给出的人工可覆盖建议）：合并 / 新增 / 采用 / 不入图库 / 忽略
const _NR_SUGGEST = {
  matched_high: { l:'采用',   c:'#2f855a', bg:'#eaf7ec', bd:'#C0DD97' },
  matched_low:  { l:'采用?',  c:'#c98a2e', bg:'#fffaf0', bd:'#FAC775' },
  dup_high:     { l:'合并',   c:'#7c3aed', bg:'#f5f3ff', bd:'#DDD6FE' },
  dup_suspect:  { l:'合并?',  c:'#d97706', bg:'#fef3c7', bd:'#FCD9A1' },
  none:         { l:'新增',   c:'#1d4ed8', bg:'#eff6ff', bd:'#93C5FD' },
  rejected:     { l:'不入图库', c:'#6b7280', bg:'#f3f4f6', bd:'#d1d5db' },
  generic:      { l:'忽略',   c:'#6b7280', bg:'#f8fafc', bd:'#e2e8f0' },
};
const _NR_BLOCKING = ['matched_low','dup_high','dup_suspect','none','rejected'];

// P0 AI summary panel
function renderSummaryPanel(cd) {
  if (!cd) return '';
  var summary = cd.summary;
  var mid = (window._lastRenderedMsgId || cd.__mid || '');
  if (!summary || !summary.headline) {
    if (!mid) return '';
    return '<div class="summary-panel summary-empty" data-mid="' + esc(mid) + '">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;">' +
      '<span style="font-size:11.5px;color:var(--mut);">💡 暂无结论速览</span>' +
      '<button class="btn sm ghost" onclick="generateMessageSummary(' + "'" + esc(mid) + "'" + ', this)" title="根据本次输出的子任务/工具调用规则生成结论速览">📝 生成结论</button>' +
      '</div></div>';
  }
  var s = summary;
  var headline = esc(s.headline || '任务已执行');
  var bullets = (s.bullets || []).slice(0, 5);
  var artifacts = (s.artifacts || []);
  var stats = s.stats || {};
  var bulletsHtml = bullets.map(function(b) { return '<li>' + esc(b) + '</li>'; }).join('');
  var artifactsHtml = artifacts.length
    ? artifacts.map(function(a) { return '<span class="tag" style="margin-right:4px;font-size:10.5px;background:var(--color-primary-bg,#e8f0fe);color:var(--color-primary,#2f6fed);" title="' + esc(a.ref || '') + '">' + esc(a.kind || '产物') + ': ' + esc(a.label || '') + '</span>'; }).join('')
    : '';
  var statsParts = [];
  if (stats.tokens) statsParts.push('🪙 ' + Number(stats.tokens).toLocaleString() + ' tokens');
  if (stats.elapsed_ms) statsParts.push('⏱ ' + (stats.elapsed_ms / 1000).toFixed(1) + 's');
  statsParts.push('🛠 ' + (stats.tool_calls || 0) + ' 工具调用');
  statsParts.push('📊 ' + (stats.subtasks || '0/0') + ' 子任务');
  return '<div class="summary-panel" data-mid="' + esc(mid) + '">' +
    '<div class="summary-head" onclick="toggleSummaryPanel(this)" title="点击折叠/展开">' +
    '<span class="summary-icon">💡</span>' +
    '<span class="summary-headline">' + headline + '</span>' +
    '<span class="summary-stats">' + statsParts.map(esc).join(' · ') + '</span>' +
    '<span class="summary-chev">▼</span>' +
    '</div>' +
    '<div class="summary-body">' +
    (bulletsHtml ? '<ul class="summary-bullets">' + bulletsHtml + '</ul>' : '') +
    (artifactsHtml ? '<div class="summary-artifacts" style="margin-top:6px;">' + artifactsHtml + '</div>' : '') +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:6px;border-top:1px dashed var(--line);">' +
    '<span style="font-size:10px;color:var(--mut);">来源：' + esc(s.source || 'rule') + ' · 生成于 ' + esc(s.generated_at || '-') + '</span>' +
    '<span style="font-size:10.5px;color:var(--color-muted,#888);">▼ 折叠展开下方推理过程</span>' +
    '</div>' +
    '</div></div>';
}

function toggleSummaryPanel(head) {
  var panel = head.closest('.summary-panel');
  if (!panel) return;
  var body = panel.querySelector('.summary-body');
  var chev = panel.querySelector('.summary-chev');
  if (!body) return;
  var collapsed = panel.classList.toggle('summary-collapsed');
  body.style.display = collapsed ? 'none' : '';
  if (chev) chev.textContent = collapsed ? '▶' : '▼';
}

async function generateMessageSummary(msgId, btn) {
  if (!msgId) return;
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    var r = await api('/api/messages/' + msgId + '/summary', {method:'POST'});
    if (r.error) { toast('生成失败：' + r.error); return; }
    toast('✅ 结论速览已生成');
    if (typeof loadMessages === 'function') loadMessages();
  } catch (e) {
    toast('生成失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📝 生成结论'; }
  }
}
