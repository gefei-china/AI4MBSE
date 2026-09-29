/* 知识库：统计 / 三元组 / 覆盖度 / 图库
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 6726-7309  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
/* ══ P0 口径统一（2026-09-26）：知识看板首屏改由**单一聚合端点**驱动 ══
   端点：GET /api/knowledge/dashboard（口径唯一来源见 metrics_core.py）
   起因：首屏此前拼 `stats?branch=`（分支口径）+ `overview`（全局口径）+ `lifecycle`（全局），
        同一屏三套口径互相打架（实测：已评审 61 vs 122+61、关系 83 vs 249、已废弃 2 vs 6）。
   现约定：每个指标都带 scope 角标（release/branch/global），前端**只渲染不计算**，
        阈值三色 status 由后端 governance._status 判定 —— 禁止在前端再写一遍阈值。 */
async function loadKBStats() { return loadKBDashboard(); }

async function loadKBDashboard(runShacl) {
  const el = document.getElementById('kb-stats');
  if (!el) return;
  try {
    const q = '?branch=' + encodeURIComponent(getCurrentBranch())
            + '&window=' + encodeURIComponent(window._kbWin || 'all')
            + (runShacl ? '&run_shacl=true' : '');
    const d = await api('/api/knowledge/dashboard' + q);
    window._kbDash = d;
    kbTipInit();             // 计算说明浮层（挂在 body 上，不被面板滚动裁剪）
    kbClusterize();          // 先把既有面板归位（幂等）
    kbRenderClusterTabs();   // 四簇 Tab
    kbRenderTopbar(d);       // 顶栏（时间窗/分支/刷新/记录快照）
    kbRenderNorthstar(d);    // 首屏北极星（健康分 + 3 项水位）
    renderKBScopes(d, el);   // 双作用域对照
    renderKBRedlines(d);     // 治理红线
    renderKBGov(d);          // 四簇指标卡（治理/质量/消费/性能）
    kbRenderAcceptance(d);   // 验收对照（甲方 7.1 × PRD §2.2）
    kbTidyHeaders();         // V7：必须在**全部渲染完成后**收口（否则新渲染面板的 badge 漏掉）
    kbLoadTrends();          // sparkline 趋势（无快照则不画线）
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);font-size:12px;">看板加载失败：${esc(e.message||e)}</div>`;
  }
  loadEngineStats();
  loadGraphDbStats();  // P2：图数据库看板（未启用时整体收起，见 loadGraphDbStats）
  loadLifecycle();
  loadKBCatOptions();
  loadKBOverview();   // FR-KG-8/11 补 G5：知识库总览一次聚合
  loadCoverage();     // FR-KG-13 补 G6：知识完整度评估
}
function kbScopeTag(scope) {
  const lg = (window._kbDash && window._kbDash.scope_legend) || {};
  return `<span class="st b" title="${esc(lg[scope] || scope)}">${esc(scope)}</span>`;
}
function kbStatusSt(status) {
  return { ok: 'ok', warn: 'w', alert: 'r' }[status] || 'g';
}
/* 指标下钻：page='kb-b' 等走 go('kb', page)；page='branch' 由其重定向到 kb-d 合并请求 Tab */
function kbDrill(page) {
  if (!page) return;
  try {
    const p = String(page);
    if (p.indexOf('kb-') === 0) go('kb', p); else go(p);
  } catch (e) {}
}

/* 双作用域 KPI 对照：权威基线(release) vs 工作分支(当前)
   —— 直接消解"61 vs 183"式困惑：两侧口径不同，故必须分列并各自标注 scope */
/* 双作用域 KPI 对照：权威基线(release) vs 工作分支(当前) vs 跨分支去重总量。
   口径修复（2026-09-27）：图谱数据**没有"全局"口径** —— entities 主键 (id, branch)，
   跨分支 COUNT(*) 是行数累加（实测 183 行 vs 逻辑去重 61，重复 3 倍）。
   故「知识总量」一律用**跨分支去重**展示，并把行数作为对账项折叠在 tooltip 里。 */
/* 作用域对照：**一张紧凑表**（2026-09-27 由 3 行×4 张卡改为表）。
   用户反馈原布局"三个分支依次排开、看不懂、太占篇幅" → 现补上位：① 表前一句引导语
   ② 列头 ③ 每行的口径说明列 ④ 作用域角标 tooltip（解释 release/branch/global 各是什么）。 */
function renderKBScopes(d, el) {
  const s = d.scopes || {}, rel = s.release || {}, br = s.branch || {};
  const re = rel.entities || {}, rr = rel.relations || {};
  const be = br.entities || {}, brr = br.relations || {};
  const gl = s.global || {}, dr = d.drift || {};
  const leak = re.candidate || 0;
  const cur = esc(((d.branch || {}).current) || '-');
  const TH = 'padding:4px 8px;text-align:left;border-bottom:1px solid var(--line);font-weight:600;white-space:nowrap;';
  const TD = 'padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;';
  const NM = 'padding:4px 8px;border-bottom:1px solid var(--line);color:var(--color-text);font-weight:600;white-space:nowrap;';
  const line = (name, scope, cells, note, tip) => `<tr>
      <td style="${TD}"><b style="color:var(--color-text);">${name}</b> ${kbScopeTag(scope)}</td>
      ${cells.map(c => `<td style="${NM}">${c}</td>`).join('')}
      <td style="${TD}color:var(--color-text-muted);" title="${esc(tip || '')}">${note}</td></tr>`;
  el.innerHTML = `
    <div style="color:var(--color-text-muted);font-size:11px;margin-bottom:4px;">
      同一份图谱的三个作用域 —— <b style="color:var(--color-text);">AI 建模只消费「权威基线」</b>；
      另两行分别是我的作业面与去重后的总量，<u>不能直接混比</u>（悬停列头/角标看口径）。</div>
    <table class="t" style="width:100%;font-size:12px;color:var(--color-text-muted);">
      <tr>${['作用域', '实体', '关系', '已评审', '已发布', '待处理 / 口径说明']
        .map(h => `<th style="${TH}">${h}</th>`).join('')}</tr>
      ${line('权威基线', 'release',
        [re.reviewed || 0, rr.total || 0, re.reviewed || 0, rel.published || 0,
         `未评审 <span class="st ${leak === 0 ? 'ok' : 'r'}">${leak === 0 ? '=0' : '应为0'}</span>`],
        'AI 建模只消费它',
        '未评审必须为 0；不为 0 说明 AI 会消费到未审核数据')}
      ${line(`工作分支 <span class="tag">${cur}</span>`, 'branch',
        [be.total || 0, brr.total || 0, be.reviewed || 0, '—',
         `候选 ${be.candidate || 0} · 已废弃 ${be.deprecated || 0}`
           + (dr.comparable ? ` · <span class="tag" title="同一逻辑 id 在工作分支与 release 上的存在性差异 ＝ 尚未进入权威基线（AI 还看不到）的增量">未发布增量 ${dr.unpublished_delta}</span>` : '')],
        '我的作业面',
        '实体数含「已废弃」，故通常大于权威基线；「未发布增量」是还没进 AI 视野的新增知识')}
      ${line('知识总量', 'global',
        [gl.entities_dedup || 0, gl.relations_dedup || 0, '—', '—',
         `文档 ${gl.documents || 0} · 分块 ${gl.chunks || 0}`],
        `跨分支去重（行数 ${gl.entities_rows || 0}/${gl.relations_rows || 0}）`,
        '实体按逻辑 id、关系按 (源,类型,目标) 三元组去重。行数是含 dev/release/personal 三份副本的原始行数，仅供对账')}
    </table>
    <div style="color:var(--color-text-muted);font-size:11px;margin-top:6px;">
      <span id="kb-graphdb-status"><span class="st g">图数据库 读取中…</span></span></div>`;
}
function renderKBRedlines(d) {
  const el = document.getElementById('kb-redline');
  if (!el) return;
  const rows = (d.redlines || []).map(m => {
    const can = m.drill && m.drill.page;
    const ttl = esc([m.threshold || '', m.detail || ''].filter(Boolean).join(' ｜ '));
    const word = m.status === 'alert' ? '红线' : (m.status === 'warn' ? '关注' : '通过');
    // ⚠️ 暗色纪律（2026-09-26 实测）：`.panel` 在暗色下**仍是白底**，而正文继承色会翻转为
    //    浅色 → 面板内"裸文本"会变成白底浅字（实测对比度仅 1.22:1，不可读）。
    //    故面板内文本必须显式使用**不随主题翻转的 token**（--mut / --blue-d），
    //    不得依赖继承 —— 与本仓既有面板（.tag=--mut / .asset .n=--blue-d / .l=--mut）同口径。
    return `<div class="todo-item" style="color:var(--mut);" ${can ? `onclick="kbDrill('${m.drill.page}','${m.drill.kind||''}','${esc(String(m.drill.value||''))}')"` : ''} title="${ttl}">
      <span><span class="st ${kbStatusSt(m.status)}">${word}</span><b style="color:var(--blue-d);">${esc(m.name)}</b>${kbInfo(m)}
        <span class="tag">目标 ${esc(m.target || '')}</span>${kbScopeTag(m.scope)}</span>
      <span style="flex:1"></span>
      <b style="color:var(--blue-d);font-size:14px;">${m.value}${esc(m.unit || '')}</b>
      ${can ? `<span class="tag">${esc(m.drill.title || '下钻')} →</span>` : ''}
    </div>`;
  }).join('');
  const s = d.summary || {};
  el.innerHTML = rows + `<div style="margin-top:8px;font-size:11px;color:var(--mut);">
    全局汇总：<span class="st r">alert ${s.alert || 0}</span> <span class="st w">warn ${s.warn || 0}</span>
    <span class="st ok">ok ${s.ok || 0}</span>　点击任一行可下钻到修复入口。</div>`;
}

/* 治理健康度簇（治理/质量/消费）：值 + 单位 + 目标 + 三色状态 + detail，整卡可下钻 */
/* 四簇指标卡：按 group.key 分发到各自容器；达标项折叠，只展示需要关注的。
   UX-4：值为 0 且 ok 的卡片大量铺开（审计实测值 0 出现 18 次）会淹没 warn/alert
   → ok 项收进 <details>，默认只展开 warn/alert/未落地。 */
const KB_GOV_TARGET = { governance: 'kb-gov', quality: 'kb-gov-quality',
                        consumption: 'kb-gov-perf', performance: 'kb-gov-perf' };
function renderKBGov(d) {
  const sc = { ok: 'ok', warn: 'w', alert: 'r' };
  const cardOf = m => {
    const val = (m.value === null || m.value === undefined) ? '—' : m.value;
    const can = m.drill && m.drill.page;
    const ttl = esc([m.threshold || '', m.detail || ''].filter(Boolean).join(' ｜ '));
    const click = can ? `onclick="kbDrill('${m.drill.page}','${m.drill.kind||''}','${esc(String(m.drill.value||''))}')"` : '';
    return `<div class="asset" data-mkey="${esc(m.key)}" style="text-align:left;cursor:${can?'pointer':'default'};" ${click} title="${ttl}">
      <div class="l" style="display:flex;justify-content:space-between;gap:6px;align-items:center;">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(m.name)}</span>${kbInfo(m)}
        <span class="st ${sc[m.status] || 'g'}">${esc(m.status)}</span></div>
      <div class="n">${val}<small style="font-size:11px;color:var(--mut);"> ${esc(m.unit || '')}</small><span class="kb-spark"></span></div>
      <div class="l">目标 ${esc(m.target || '—')}</div>
      <div class="l" style="display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(m.detail || '')}</div>
    </div>`;
  };
  const GRID = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;';
  const bucket = {};
  (d.groups || []).forEach(g => {
    const tid = KB_GOV_TARGET[g.key];
    if (!tid) return;
    (bucket[tid] = bucket[tid] || []).push(g);
  });
  Object.keys(bucket).forEach(tid => {
    const el = document.getElementById(tid);
    if (!el) return;
    el.innerHTML = bucket[tid].map(g => {
      const items = g.items || [];
      const alertish = items.filter(m => m.status !== 'ok');
      const okItems = items.filter(m => m.status === 'ok');
      const head = `<div style="font-size:11px;color:var(--mut);margin:0 0 6px;">${esc(g.name)}
        <span class="tag">${items.length} 项</span>
        <span class="st ${alertish.length ? 'w' : 'ok'}">${alertish.length ? alertish.length + ' 项待关注' : '全部达标'}</span></div>`;
      const body = alertish.length
        ? `<div style="${GRID}margin-bottom:12px;">${alertish.map(cardOf).join('')}</div>`
        : `<div style="font-size:11.5px;color:var(--grn);margin:0 0 12px;">✅ 本组无待关注项</div>`;
      const fold = okItems.length ? `<details style="margin-bottom:12px;">
        <summary style="font-size:11px;color:var(--grn);cursor:pointer;">✅ ${okItems.length} 项达标（点击展开：${okItems.slice(0,3).map(m=>esc(m.name)).join('、')}${okItems.length>3?'…':''}）</summary>
        <div style="${GRID}margin-top:8px;">${okItems.map(cardOf).join('')}</div></details>` : '';
      return head + body + fold;
    }).join('');
  });
}
/* ⚠️ 2026-09-27 面板级合并：原「数据生命周期」面板已并入「知识资产总览」
   （两个面板曾各渲染一份「来源分布」= 同一数据出现两次；且此处曾用 s.src 取来源名，
    而 metrics_core 返回的键是 source_type → 实际渲染成 undefined）。
   本函数保留为**别名**，因首屏 / 编辑实体后 / 上传文档后共有 3 处旧调用点。 */
async function loadLifecycle() { return loadKBOverview(); }
/* 优化三：三元组统一审核（S-P-O）+ 本体就绪提示 */
async function loadOntologyReadiness(){
  const el = document.getElementById('onto-ready'); if(!el) return;
  try{
    const r = await api('/api/knowledge/ontology/check');
    el.innerHTML = r && r.ready
      ? `<span style="font-size:11px;color:var(--grn);">✅ 本体已就绪（${r.entity_types.length} 实体类型 / ${r.relation_types.length} 关系类型），AI 建模/抽取受 Schema 约束</span>`
      : `<span style="font-size:11px;color:var(--amb);">⚠️ 本体未完整定义，请先进入「本体模型」完成建模前置（实体 / 关系类型至少各 1）</span>`;
    try{
      const ac = await api('/api/knowledge/ontology/alias-coverage');
      if(ac && ac.zero_count>0){
        el.innerHTML += `<span style="font-size:11px;color:var(--amb);cursor:pointer;margin-left:10px;" onclick="ontAliasCoverage()" title="零别名类型=抽取归一盲区">⚠ 零别名类 ${ac.zero_count}/${ac.total}（点击查看）</span>`;
      } else if(ac && ac.total){
        el.innerHTML += `<span style="font-size:11px;color:var(--grn);margin-left:10px;" title="词典（词法层）覆盖检查">别名覆盖 ${ac.covered}/${ac.total}</span>`;
      }
    }catch(e){}
  }catch(e){ el.innerHTML = '<span style="font-size:11px;color:var(--mut);">本体就绪检测失败</span>'; }
}
// R3：零别名类清单面板
async function ontAliasCoverage(){
  try{
    const ac = await api('/api/knowledge/ontology/alias-coverage');
    const list = (ac.zero_alias||[]).map(x=>`<div style="display:flex;gap:6px;align-items:center;padding:4px 0;border-bottom:1px dashed var(--line);font-size:12px;"><span class="st ${x.kind==='relation'?'b':'w'}">${x.kind==='relation'?'关系':'类'}</span><b>${esc(x.name)}</b></div>`).join('');
    openPanel('⚠ 零别名类型（抽取归一盲区 · '+ac.zero_count+'/'+ac.total+'）',
      '<div style="font-size:11.5px;color:var(--mut);margin-bottom:8px;line-height:1.6;">以下本体类型在词典（词法层）中没有任何别名——抽取遇到这些概念的口语/缩写/错拼时将无法归一。可进入各类型的 Description 面板「＋ 添加别名」补齐。</div>' + (list || '<div style="color:var(--grn);font-size:12px;">✅ 全覆盖</div>'));
  }catch(e){ toast('覆盖度加载失败：'+e.message); }
}
/* ⑤ 三元组统一审核（S-P-O）：待审队列表 + 通过/驳回/全部通过（真实调用后端） */
async function loadTripleReviewPane(){
  const el = document.getElementById('triple-review-pane'); if(!el) return;
  el.innerHTML = '<div class="loading">加载三元组待审队列…</div>';
  try{
    const r = await api('/api/knowledge/triples/review-queue?status=pending&limit=200');
    const items = r.items || []; const st = r.stats || {};
    // 2026-09-22：来源文档筛选（triples 无 batch_id，以 source_doc 为批次近似维度；"全部通过"只作用于当前可见项）
    const curF = window._tripleSrcFilter || '';
    const shown = items.filter(x=>!curF || x.source_doc===curF);
    const srcs = Array.from(new Set(items.map(x=>x.source_doc).filter(Boolean)));
    const srcOpts = ['<option value="">全部来源</option>'].concat(
      srcs.map(d=>`<option value="${esc(d)}" ${d===curF?'selected':''}>来源：${esc(d)}</option>`)).join('');
    window._tripleReviewIds = shown.map(t=>t.triple_id);
    window._tripleReviewList = shown.map(t=>({triple_id:t.triple_id, subject_name:t.subject_name||'', object_type:t.object_type||''}));
    const badge = document.getElementById('triple-count-tab');
    if(badge) badge.textContent = shown.length;
    if(!shown.length){
      let _ts = {}; try{ _ts = await api('/api/knowledge/triples/stats').catch(()=>({})) || {}; }catch(e){}
      const _pending = _ts.to_store||0;
      el.innerHTML = `<div style="padding:14px;color:var(--mut);font-size:12px;">${items.length ? '当前筛选条件下无待审三元组（切换上方来源可查看其余）。' : '暂无待审三元组。'}三元组统计：已通过 ${st.approved||0} / 待审 ${st.pending||0} / 驳回 ${st.rejected||0}。<br>抽取审核确认后产生待审三元组，以 (S-P-O) 原子单元在此统一审核；实体/关系为通过后 commit 反写产物。</div>`
        + (_pending>0 ? `<div style="padding:0 14px 14px;"><div class="fempty">📦 有 <b>${_pending}</b> 条已审核三元组待落图（角标显示的即此数量入「发布」站水位）——落图后写入图谱实体。<br><button class="btn sm grn" onclick="tripleCommit()">⚡ 立即落图（${_pending} 条）</button></div></div>` : '');
      return;
    }
    const dupBadge = t => {
      const di = t.dup_info || {kind:'none'};
      if(di.kind==='triple_repeat') return `<span class="st w" title="${esc(di.dup_with||'')}">🔁 已存在</span>`;
      if(di.kind==='suspect') return `<span class="st w" title="${esc(di.dup_with||'')}">🔀 与图谱「${esc(di.dup_with||'?')}」近似</span>`;
      return `<span class="st ok" title="图库无同指实体/三元组">✅ 新建</span>`;
    };
    const row = t => `<div style="display:flex;align-items:center;gap:6px;padding:6px 8px;border-bottom:1px dashed var(--line);font-size:12px;">
      <span class="tag">${esc(t.object_type==='entity'?'🔗 关系':'🏷 属性')}</span>
      <b style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(t.label)}">${esc(t.subject_name)}</b>
      <span style="color:var(--blue);white-space:nowrap;">${esc(t.predicate)}</span>
      <span style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(t.object_value||t.object_id||'')}</span>
      ${dupBadge(t)}
      <span style="margin-left:auto;display:flex;align-items:center;gap:4px;">
        <span style="font-size:10px;color:var(--mut);">置信 ${t.confidence||0}</span>
        <button class="btn sm grn" style="font-size:10px;padding:1px 8px;" onclick="tripleAct('${t.triple_id}','approved')">通过</button>
        <button class="btn sm ghost" style="font-size:10px;padding:1px 8px;background:rgba(229,62,62,.1);color:var(--red);" onclick="tripleAct('${t.triple_id}','rejected')">驳回</button>
      </span></div>`;
    el.innerHTML = `<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);background:#f7f9fc;font-size:12px;">
      <select id="triple-src-filter" onchange="tripleSetSrc(this.value)" style="border:1px solid var(--line);border-radius:6px;padding:2px 8px;font-size:11px;max-width:180px;">${srcOpts}</select>
      <b>共 ${shown.length} 条待审</b>
      <span style="font-size:11px;color:var(--mut);">立即全部通过（慢速核对或批量放行）</span>
      <span style="flex:1"></span>
      <button class="btn sm grn" onclick="tripleApproveAll()">✅ 全部通过</button></div>` +
      shown.map(row).join('');
  }catch(e){ el.innerHTML = `<div style="color:var(--red);font-size:12px;">三元组队列加载失败：${e.message}</div>`; }
}
window.tripleSetSrc = function(v){ window._tripleSrcFilter = v; loadTripleReviewPane(); };
window.tripleAct = async function(triple_id, decision){
  const el = document.getElementById('triple-review-pane'); if(!el) return;
  try{
    const r = await api('/api/knowledge/triples/' + encodeURIComponent(triple_id) + '/review',
      {method:'POST', body:JSON.stringify({decision:decision, note: decision==='approved'?'SDK:三元组统一审核通过':'SDK:三元组统一审核驳回'})});
    if(r && r.error){ toast('操作失败：'+r.error); return; }
    toast(decision==='approved'?'已通过该三元组':'已驳回该三元组');
    if(decision==='approved'){ tripleCommit(); } else { loadTripleReviewPane(); }
  }catch(e){ toast('操作失败：'+(e.message||'')); }
};
function tripleGotoGraph(){
  const subj = (window._tripleReviewList||[]).map(t=>t.subject_name).filter(Boolean);
  try{ if(typeof go==='function') go('kb','kb-d'); }catch(e){}
  if(window.graphState){ graphState.view = graphState.view||{}; graphState.view.status = 'active'; }
  window._tripleGraphSubjects = subj;
  setTimeout(()=>{ try{ if(typeof loadGraph==='function') loadGraph(); }catch(e){} 
    if(subj.length){ try{ if(typeof toast==='function') toast('👁 图谱已按「草稿+已发布」展示；本次三元组主体 '+subj.length+' 个'); }catch(e){} } }, 600);
}
async function tripleCommit(){ 
  const r = await api('/api/knowledge/triples/commit', {method:'POST', body:'{}'});
  toast(`✅ 三元组已落图：实体 ${(r&&r.entities)||0} / 关系 ${(r&&r.relations)||0}`);
  loadTripleReviewPane();
}
async function tripleApproveAll(){
  const ids = window._tripleReviewIds || [];
  if(!ids.length){ toast('无待审三元组'); return; }
  const r = await api('/api/knowledge/triples/batch-review',
    {method:'POST', body:JSON.stringify({triple_ids:ids, decision:'approved', note:'SDK:三元组批量全部通过'})});
  toast(`已通过 ${(r&&r.reviewed)||0} 条三元组，自动落图`);
  tripleCommit();
}
/* 知识资产总览（2026-09-27 由「知识库总览」+「数据生命周期」合并而来）。
   一次请求 `/api/knowledge/overview` 供全部分区；**来源分布只渲染一次**（此前两面板各一份）。
   分区：① 资产 KPI（跨分支去重）② 生命周期分布 ③ 来源/类别分布 ④ 待评审队列 ⑤ 图谱缩略 */
async function loadKBOverview() {
  const el = document.getElementById('kb-overview');
  if (!el) return;
  try {
    const r = await api('/api/knowledge/overview');
    const k = r.kpi || {};
    const lcAll = r.lifecycle || {}, lc = lcAll.lifecycle || {};
    const total = lcAll.total || 1;
    const SEC = 'font-size:11px;color:var(--mut);margin:12px 0 6px;';
    const ASSET = 'text-align:left;';
    const card = (l, n, style) => `<div class="asset" style="${ASSET}"><div class="n" style="${style||''}">${n}</div><div class="l">${l}</div></div>`;

    // ① 资产 KPI：实体/关系用**跨分支去重**口径（行数作对账注脚，避免"知识量虚报 3 倍"）
    const kpiHtml = `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">
      ${card('🧬 实体（跨分支去重）', k.entities || 0)}
      ${card('🔗 关系（三元组去重）', k.relations || 0)}
      ${card('📄 文档（真全局）', k.documents || 0)}
      ${card('🕘 待评审', k.pending_review || 0,
             (k.pending_review || 0) ? 'color:var(--blue-d);font-weight:700;' : '')}</div>
      <div style="font-size:10.5px;color:var(--mut);margin-top:4px;"
        title="entities 主键 (id, branch)：同一逻辑对象在每个分支各存一行，故直接 COUNT(*) 会把知识量重复 3 倍。实体按 id、关系按 (源,类型,目标) 去重。">
        去重口径 · 对账：实体行数 ${k.entities_rows || 0} / 关系行数 ${k.relations_rows || 0}（含 dev/release/personal 副本）</div>`;

    // ② 生命周期分布（跨分支行数口径，scope_note 已在后端注明）
    const order = [['raw_chunk', '原始切片', 'var(--mut)'], ['candidate', '候选', 'var(--amb)'],
                   ['reviewed', '已评审(未发布)', 'var(--blue)'], ['published', '已发布(权威基线)', 'var(--grn)'],
                   ['deprecated', '已废弃', 'var(--mut)']];
    const bars = order.map(([kk, label, color]) => {
      const n = lc[kk] || 0, pct = Math.round(n * 100 / total);
      return `<div style="flex:1;min-width:70px;text-align:center;" title="${esc(label)}：${n}（${pct}%）">
        <div style="height:10px;border-radius:5px;background:${color};opacity:${n ? 1 : 0.25};"></div>
        <div style="font-size:11px;font-weight:600;margin-top:4px;color:var(--blue-d);">${n}</div>
        <div style="font-size:10px;color:var(--mut);">${label}</div>
        <div style="font-size:10px;color:var(--mut);">${pct}%</div></div>`;
    }).join('');
    const lifeHtml = `<div style="${SEC}">生命周期分布 <span class="tag" title="${esc(lcAll.scope_note || '')}">跨分支行数</span>
      <span class="tag">发布记录 ${lcAll.published_records || 0} 条</span></div>
      <div style="display:flex;gap:8px;align-items:flex-end;">${bars}</div>`;

    // ③ 来源分布（只此一处）+ 知识类别分布；键名用 source_type（此前误用 s.src → 渲染成 undefined）
    const src = r.source_dist || r.lifecycle && r.lifecycle.by_source || [];
    const maxN = src.reduce((m, s) => Math.max(m, s.n || 0), 1);
    const srcHtml = src.length ? `<div style="${SEC}">来源分布 <span class="tag" title="与上方资产 KPI 同口径：按逻辑 id 跨分支去重">跨分支去重</span></div>
      <div style="display:flex;flex-wrap:wrap;gap:8px 16px;">${src.map(s =>
        `<div style="font-size:11px;flex:1;min-width:130px;"><div style="display:flex;justify-content:space-between;">
          <span>${esc(s.source_type)}</span><b style="color:var(--blue-d);">${s.n}</b></div>
          <div style="height:6px;border-radius:3px;background:var(--line);margin-top:2px;">
            <div style="height:6px;border-radius:3px;background:var(--blue);width:${Math.round((s.n || 0) * 100 / maxN)}%;"></div></div></div>`).join('')}</div>` : '';
    const cats = lcAll.by_category || [];
    const catHtml = cats.length ? `<div style="${SEC}">知识类别分布 <span class="tag">仅已评审</span></div>
      <div style="line-height:1.9;">${cats.map(c => `<span class="tag" title="知识类别">${esc(c.cat)} ${c.n}</span>`).join(' ')}</div>` : '';

    // ④ 待评审队列（空态收口 V6）
    const q = r.review_queue || {};
    const qv = q.v2g || {}, qe = q.entities || {}, qr = q.relations || {};
    const allZero = !(qv.total || 0) && !(qe.total || 0) && !(qr.total || 0);
    const queueRow = (title, tot, items, itemFn) => `
      <div style="flex:1;min-width:210px;">
        <div style="font-size:11px;color:var(--mut);margin-bottom:4px;">${title} <span class="st w">${tot} 条</span></div>
        ${items.length ? items.map(itemFn).join('') : '<div style="font-size:11px;color:var(--mut);padding:5px 0;">暂无待评审</div>'}
      </div>`;
    const v2gItem = c => `<div class="todo-item" onclick="go('kb','kb-b')" title="点击前往治理中心处理"><b>${esc(c.name)}</b><span class="tag">${esc(c.entity_type || '候选')}</span></div>`;
    const entItem = c => `<div class="todo-item" onclick="go('kb','kb-b')" title="点击前往治理中心处理"><b>${esc(c.name)}</b><span class="tag">${esc(c.entity_type)}</span></div>`;
    const relItem = c => `<div class="todo-item" onclick="go('kb','kb-b')" title="点击前往数据整理处理"><b>${esc(c.source_name)} → ${esc(c.relation_type)} → ${esc(c.target_name)}</b><span class="tag">关系</span></div>`;
    const queueHtml = allZero
      ? `<div style="${SEC}">待评审队列</div>
         <div style="font-size:12px;color:var(--grn);">✅ 三类待评审队列均为 0（v2g 候选 / 实体候选 / 关系候选），无需处理。</div>`
      : `<div style="${SEC}">待评审队列</div>
         <div style="display:flex;gap:18px;flex-wrap:wrap;">${queueRow('🕘 v2g 候选', qv.total || 0, qv.items || [], v2gItem)}${queueRow('🧬 实体候选', qe.total || 0, qe.items || [], entItem)}${queueRow('🔗 关系候选', qr.total || 0, qr.items || [], relItem)}</div>`;

    // ⑤ 图谱缩略
    const gt = r.graph_thumb || {};
    const thumb = (gt.entities || []).slice(0, 14).map(e => `<span class="tag" title="${esc(e.entity_type)}">${esc(e.name)}</span>`).join(' ');
    const thumbHtml = `<div style="${SEC}">图谱缩略（已评审实体 <b>${(gt.entities || []).length}</b> / 关系 <b>${gt.relations || 0}</b>）</div>
      <div style="line-height:1.9;">${thumb || '<span style="color:var(--mut);font-size:11px;">暂无已评审实体</span>'}</div>`;

    // ⚠️ 容器级基础色：`#kb-overview` 内所有未显式上色的文本都吃 `--mut`。
    //    不这样做则它们继承正文色（暗色翻转为浅色）而 `.panel` 恒白底 → 白底浅字不可读
    //    （2026-09-27 实测：生命周期数字、来源分布名称在暗色下直接消失）。
    el.innerHTML = `<div style="color:var(--mut);">`
      + kpiHtml + lifeHtml + srcHtml + catHtml + queueHtml + thumbHtml + `</div>`;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);font-size:12px;">总览加载失败：${e.message}</div>`;
  }
}
async function loadCoverage() {
  const el = document.getElementById('kb-coverage');
  try {
    const r = await api('/api/knowledge/coverage');
    const tc = r.type_coverage_sysml || r.type_coverage || {};
    const dm = r.type_coverage_domain || {};
    const cl = r.chunk_linked||{};
    const cvar = st => (st === 'ok' ? 'var(--grn)' : (st === 'warn' ? 'var(--amb)' : 'var(--red)'));
    const cst = (v, ok, warn) => (v >= ok ? 'ok' : (v >= warn ? 'warn' : 'alert'));
    const covBar = (label, lvl, ok, warn, note) => {
      const v = Math.min(100, Math.round((lvl.coverage_rate||0)*100));
      const st = cst(v, ok, warn);
      return `<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
        <span style="font-size:11px;color:var(--mut);width:126px;flex:none;">${label}</span>
        <div style="flex:1;height:10px;border-radius:5px;background:var(--line);"><div style="height:10px;border-radius:5px;background:${cvar(st)};width:${v}%;"></div></div>
        <b style="font-size:15px;color:${cvar(st)};">${v}%</b>
        <span class="st ${st==='ok'?'ok':st==='warn'?'w':'r'}">${st}</span>
        <span class="tag">${lvl.covered_types||0}/${lvl.total_types||0}</span></div>
        <div style="font-size:11px;color:var(--mut);margin:0 0 8px 126px;">${note}</div>`;
    };
    const covBar2 = covBar('SysML 元素类型', tc, 82, 50,
        '规范约束源：分母＝SysML v2 元素类型 14 类（PRD §2.2 目标 ≥82%）')
      + covBar('领域扩展类型', dm, 80, 50, '领域知识点：分母＝本体中非 SysML 级的领域业务类型');
    const rate = Math.min(100, Math.round((tc.coverage_rate||0)*100));
    const unlink = Math.round((cl.unlinked_ratio||0)*100);
    // P0 修 C3：分块追溯覆盖率升级为告警级（0/5758＝100% 未链接 ⇒ 抽取链断裂，非"说明文字"）
    const traceSt = unlink >= 30 ? 'alert' : (unlink >= 10 ? 'warn' : 'ok');
    const chunkHtml = `<div style="font-size:11.5px;margin-top:8px;padding:6px 10px;border-radius:6px;background:${unlink>=30?'var(--red-l)':'var(--blue-l)'};color:${unlink>=30?'var(--red)':'var(--blue-d)'};">
      <span class="st ${traceSt==='ok'?'ok':traceSt==='warn'?'w':'r'}">${traceSt}</span>
      分块追溯覆盖率：${cl.linked_chunks||0}/${cl.total_chunks||0} 个分块已链接实体，<b>未链接 ${unlink}%</b>
      <span style="color:var(--mut);">（未链接＝抽取结果无来源，追溯链断裂；修复入口见「治理红线」）</span></div>`;
    const empties = tc.empty_types||[];
    const emptyHtml = `<div style="margin-top:10px;font-size:11px;color:var(--mut);">SysML 级空类型（无已评审实例，建议优先抽取）：</div>
      <div style="margin-top:4px;line-height:1.9;">${empties.length ? empties.map(t=>`<span class="tag" style="border-color:var(--amb);color:var(--amb);" title="无已评审实例">${esc(t.name)}</span>`).join(' ') : '<span style="font-size:11px;color:var(--grn);">✅ 全部 SysML 元素类型均有已评审实例</span>'}</div>`;
    const topics = (r.fallback_topics||[]).slice(0,10);
    const topicHtml = `<div style="margin-top:10px;font-size:11px;color:var(--mut);">建议抽取主题（检索 fallback 高频词，点击直达 v2g 手动抽取）：</div>
      <div style="margin-top:4px;line-height:1.9;">${topics.length ? topics.map(t=>`<span class="tag" style="border-color:var(--blue);color:var(--blue-d);cursor:pointer;" title="点击用该主题触发抽取" onclick="coverageExtractTopic('${esc(t.word).replace(/'/g,"\\'")}')">${esc(t.word)} ${t.count}</span>`).join(' ') : '<span style="font-size:11px;color:var(--mut);">暂无 fallback 检索记录</span>'}</div>`;
    el.innerHTML = covBar2 + chunkHtml + emptyHtml + topicHtml;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">完整度加载失败：${e.message}</div>`; }
}
/* 点击完整度主题词 → 填入检索框并触发 v2g 手动抽取（最简路径，等价 impToV2G 抽取流程） */
function coverageExtractTopic(word) {
  const q = document.getElementById('kb-chunk-q');
  if(q) q.value = word;
  v2gExtract();
}
/* P0-3: 知识类别下拉选项（设计方法知识/设计资产） */
let KB_CATS = [];
async function loadKBCatOptions() {
  try {
    const cats = await api('/api/knowledge/categories');
    KB_CATS = Array.isArray(cats)?cats:[];
    const sel = document.getElementById('kb-cat-filter');
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部类别</option>' +
      KB_CATS.map(c=>`<option value="${esc(c.name)}" data-g="${c.group_name}">${c.name}</option>`).join('');
    sel.value = cur;
  } catch(e) {}
}
function kbCatLabel(name) {
  const c = KB_CATS.find(x=>x.name===name);
  return c ? `<span class="tag" style="border-color:${c.group_name==='设计方法知识'?'var(--blue)':'var(--green)'};">${c.group_name==='设计方法知识'?'📐':'🧩'}${esc(name)}</span>` : (name?`<span class="tag">${esc(name)}</span>`:'<span style="color:var(--mut);">未分类</span>');
}
/* P0-3: 实体打知识类别标签（居中卡片输入，空=清除） */
async function kbSetEntityCategory(id, current) {
  if(!branchWritable()) return;
  const names = KB_CATS.map(c=>`${c.name}（${c.group_name}）`).join('\n');
  const v = await promptDialog({title:'设置知识类别', message:`输入类别名（设计方法知识/设计资产子类）：\n可选：${names}\n留空=清除分类`, value:current||'', okText:'保存'});
  if(v===null) return;
  const r = await api(`/api/knowledge/entities/${id}/category`, {method:'PUT', body:JSON.stringify({category:v.trim()})});
  if(r && r.error) { toast('设置失败：' + r.error); return; }
  toast(`✅ 已设置类别：${v.trim()||'未分类'}`);
  loadKBEntities(); loadLifecycle(); loadGraph && loadGraph();
}
/* P0-3: 文档打知识类别标签 —— 2026-09-28 由「纯文本输入」升级为**类别选择器**
   （选择已有类别 / 改选 / 清除＝删除标签），实现见 43-doccat.js::kbCategoryPicker。
   注：**不再做分支只读门禁** —— documents 是全局资产（服务端 branch 硬编码 'global'，
   PUT /api/documents/{id}/category 本身无权限依赖），与 doUploadDoc 同理；
   旧分支门禁属分支隔离模型的残留，会在 release 分支上误拦文档分类。 */
async function kbSetDocCategory(id, current) {
  const picked = await kbCategoryPicker(current || '');
  if(picked === null) return;                    // 用户取消
  const r = await api(`/api/documents/${id}/category`, {method:'PUT', body:JSON.stringify({category:picked})});
  if(r && r.error) { toast('设置失败：' + r.error); return; }
  toast(picked ? `✅ 文档已设置类别：${picked}` : '🗑 已清除该文档的知识类别');
  loadDocs(); loadLifecycle();
}
/* P1: 双引擎消费统计（图/向量/混合路由占比） */
async function loadEngineStats() {
  const el = document.getElementById('engine-stats');
  try {
    const s = await api('/api/knowledge/engine-stats');
    const routes = s.routes||{};
    const bars = ['graph','mixed','vector'].filter(k=>routes[k]).map(k=>{
      const r = routes[k];
      const nm = {graph:'纯图检索',mixed:'图+向量混合',vector:'纯向量检索'}[k]||k;
      return `<div style="flex:1;min-width:130px;border:1px solid var(--line);border-radius:8px;padding:10px;background:#fff;">
        <div style="font-size:11px;color:var(--mut);">${nm} <b style="float:right;">${r.pct}%</b></div>
        <div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${r.count}<small style="font-size:11px;color:var(--mut);"> 次</small></div>
        <div style="font-size:11px;color:var(--mut);">平均置信 ${r.avg_confidence||0} · ${r.avg_latency_ms||0}ms</div>
      </div>`;
    }).join('');
    const recent = (s.recent||[]).slice(0,4).map(x=>`<span class="tag">${x.route}「${x.query}」</span>`).join(' ');
    el.innerHTML = `
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <div style="flex:1;min-width:130px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
          <div style="font-size:11px;color:var(--blue-d);">累计查询</div>
          <div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${s.total_queries}<small style="font-size:11px;"> 次</small></div>
          <div style="font-size:11px;color:var(--blue-d);">每次检索自动落库统计</div>
        </div>
        ${bars}
      </div>
      ${recent?`<div style="margin-top:8px;font-size:11px;color:var(--mut);">最近：${recent}</div>`:''}`;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
/* 2026-09-01 P2：图数据库看板（ArcR-5 查询镜像：stats / SPARQL 只读查询 / 同步入图 / N-Quads 导出） */
async function loadGraphDbStats(){
  const el = document.getElementById('graphdb-stats');
  const panel = document.getElementById('graphdb-panel');
  const qbox = document.getElementById('graphdb-query-box');
  const badge = document.getElementById('kb-graphdb-status');
  try {
    const s = await api('/api/graph-db/status');
    if(!s.ok || !s.enabled){
      // P0 修 C7：未启用时**整体收起面板**（此前留一块"未启用"常量空洞，约占 1/3 纵向空间）
      if(panel) panel.style.display = 'none';
      if(qbox) qbox.style.display = 'none';
      if(badge) badge.innerHTML = '<span class="st g" title="配置 graph_db.enabled=true 后重启生效">图数据库 未启用</span>';
      if(el) el.innerHTML = '';
      return;
    }
    if(panel) panel.style.display = '';
    if(qbox) qbox.style.display = '';
    if(badge) badge.innerHTML = `<span class="st ok">图数据库 已启用 · ${esc(String(s.stats && s.stats.backend || '-'))}</span>`;
    const st = s.stats||{};
    const graphs = (st.graphs||[]).map(g=>`<span class="tag">${esc(String(g).replace(/^(urn:mbse:graph:|http:\/\/www\.xingwang\.mbse\/graph:)/,'').replace(/%2F/gi,'/'))}</span>`).join(' ');
    el.innerHTML = `<div style="display:flex;gap:10px;flex-wrap:wrap;">
      <div style="flex:1;min-width:120px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
        <div style="font-size:11px;color:var(--blue-d);">后端</div><div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${esc(st.backend||'-')}</div></div>
      <div style="flex:1;min-width:120px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
        <div style="font-size:11px;color:var(--blue-d);">三元组</div><div style="font-size:18px;font-weight:700;color:var(--blue-d);margin:4px 0;">${st.triple_count||0}</div></div>
      <div style="flex:2;min-width:200px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--blue-l);">
        <div style="font-size:11px;color:var(--blue-d);">命名图（分支）</div><div style="margin:6px 0 0;">${graphs||'<span style="color:var(--mut)">空</span>'}</div></div>
    </div>`;
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
async function runGraphDbQuery(){
  const q = (document.getElementById('graphdb-q')||{}).value;
  if(!q || !q.trim()) return;
  const out = document.getElementById('graphdb-result');
  out.innerHTML = '<span style="color:var(--mut)">执行中…</span>';
  const r = await api('/api/graph-db/query', {method:'POST', body:JSON.stringify({query:q.trim(), limit:20})});
  if(r.error || r.ok===false){ out.innerHTML = `<span style="color:var(--red)">${esc(r.error||'查询失败')}</span>`; return; }
  if(!r.rows || !r.rows.length){ out.innerHTML = '<span style="color:var(--mut)">图谱无命中——可调整查询、切换分支命名图，或先执行「同步入图」</span>'; return; }
  const keys = Object.keys(r.rows[0]);
  const rowsHtml = '<table class="t" style="width:100%;font-size:12px;"><tr>' +
    keys.map(k=>`<th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">${esc(k)}</th>`).join('') + '</tr>' +
    r.rows.map(row=>'<tr>'+keys.map(k=>`<td style="padding:5px 8px;border-bottom:1px solid var(--line);word-break:break-all;">${esc(String(row[k]??''))}</td>`).join('')+'</tr>').join('') + '</table>';
  out.innerHTML = `<div style="color:var(--mut);margin-bottom:6px;">命中 ${r.count} 行（显示前 ${r.rows.length}）：${r.note||''}</div>` + rowsHtml;
}
async function syncGraphDb(){
  const r = await api('/api/graph-db/sync', {method:'POST'});
  if(r.error || r.ok===false) toast('同步失败：'+(r.error||''));
  else toast(`✅ 已同步 ${r.synced||0} 条（剩余待入图 ${r.pending_left||0}）`);
  loadGraphDbStats();
}
async function exportGraphDb(){
  const r = await api('/api/graph-db/export');
  if(r.error || r.ok===false) toast('导出失败：'+(r.error||''));
  else toast(`✅ 已导出 ${r.quads||0} 条四元组 → ${r.path||''}`);
}
async function searchChunks() {
  const q = document.getElementById('kb-chunk-q').value.trim();
  if(!q) { toast('请输入查询'); return; }
  const mode = document.getElementById('kb-chunk-mode').value;
  const el = document.getElementById('chunk-hits');
  el.innerHTML = '<div class="loading">检索中…</div>';
  try {
    const r = await api('/api/knowledge/chunks/search', {method:'POST', body:JSON.stringify({query:q, hybrid: mode==='hybrid'})});
    const hits = r.hits||[];
    if(!hits.length) { el.innerHTML = '<div style="color:var(--mut);font-size:12px;">无命中分块（请先上传并解析文档）</div>'; return; }
    el.innerHTML = hits.map(h=>`
      <div style="border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px;background:#fff;">
        <div style="font-size:11px;color:var(--mut);margin-bottom:4px;">
          <span class="st b">命中 ${h.score}</span>
          ${h.confidence_level?`<span class="st ${h.confidence_level==='高'?'ok':h.confidence_level==='中'?'a':'w'}">置信 ${h.confidence_level}</span>`:''}
          ${h.vec_score!==undefined?`<span class="tag">vec ${h.vec_score}</span><span class="tag">bm25 ${h.bm25_score}</span>`:''}
          <span class="tag">${esc(h.source_doc)}</span>
          ${h.section?`<span class="tag">${esc(h.section)}</span>`:''}
          <span class="tag">chunk#${h.chunk_index}</span>
          <span class="st g">${h.embed_version}</span>
        </div>
        ${h.recall_reason?`<div style="font-size:11px;color:var(--blue-d);margin-bottom:4px;">🔗 ${esc(h.recall_reason)}</div>`:''}
        <div style="font-size:12.5px;line-height:1.6;">${esc(h.content)}</div>
      </div>`).join('') + (r.mode==='hybrid'?`<div style="font-size:11px;color:var(--mut);">混合检索：BM25 ${r.bm25_count} / 向量 ${r.vec_count}</div>`:'');
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">检索失败：${e.message}</div>`; }
}
// ── O-1：向量→图谱半自动转化（抽取候选 → 勾选确认入库 → chunk 溯源）──
let v2gBatchId = null;
async function v2gExtract() {
  const q = document.getElementById('kb-chunk-q').value.trim();
  if(!q) { toast('请先输入查询'); return; }
  const el = document.getElementById('v2g-panel');
  el.innerHTML = '<div class="loading">抽取候选（LLM 受本体 schema 约束）…</div>';
  try {
    const r = await api('/api/knowledge/v2g/extract', {method:'POST', body:JSON.stringify({query:q, top_k:5})});
    v2gBatchId = r.batch_id;
    toast(`抽取完成：候选 ${r.node_count+r.edge_count}（拒绝 ${r.rejected.length}）`);
    loadV2GCandidates();
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">抽取失败：${e.message}</div>`; }
}
// B2：候选来源徽章（source_type：ai_model=AI建模 / doc_extract=文档抽取 / 空=旧数据）
function v2gSrcBadge(c){
  const st = c.source_type || '';
  if(st === 'ai_model') return '<span class="tag" style="background:#eaf2ff;color:#185FA5;" title="AI 建模/SysML 通道（统一闸门暂存）">🤖 AI建模</span>';
  if(st === 'doc_extract') return '<span class="tag" style="background:#f0f7f0;color:#2f855a;" title="文档抽取通道">📄 文档抽取</span>';
  return '<span style="font-size:11px;color:var(--mut);">' + esc(c.source_doc||'-') + '</span>';
}
// B2：候选面板批量驳回（勾选 .v2g-check）
async function v2gCandReject() {
  const ids = Array.from(document.querySelectorAll('.v2g-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要驳回的候选'); return; }
  const reason = await promptDialog({title:'批量驳回候选', message:`驳回 ${ids.length} 条候选，请填写原因：`, value:'', placeholder:'如：重复 / 信息不完整 / 超出本体范围…', multiline:true, okText:'驳回'});
  if(reason===null) return;
  if(!String(reason).trim()){ toast('驳回必须填写原因'); return; }
  const r = await api('/api/knowledge/v2g/reject', {method:'POST', body:JSON.stringify({candidate_ids:ids, reason})});
  toast(`🚫 已驳回 ${r.rejected||0} 条`);
  loadV2GCandidates();
}
async function loadV2GCandidates() {
  const el = document.getElementById('v2g-panel');
  try {
    // B2：批次筛选下拉（SYSM- 前缀=AI建模批次）
    let batchOpts = '';
    try {
      const batches = await api('/api/knowledge/v2g/batches');
      if(Array.isArray(batches) && batches.length){
        batchOpts = `<select id="v2g-batch-filter" style="border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:11.5px;" onchange="v2gBatchId=this.value||null;loadV2GCandidates();">
          <option value="">全部批次</option>` + batches.map(b=>`<option value="${esc(b.batch_id||'')}" ${(v2gBatchId&&v2gBatchId===b.batch_id)?'selected':''}>${esc(b.batch_id||'')}${String(b.batch_id||'').startsWith('SYSM-')?' ·AI建模':''}${(b.total!=null)?`（${b.total}）`:''}</option>`).join('') + `</select>`;
      }
    } catch(e) {}
    const cands = await api('/api/knowledge/v2g/candidates?limit=200' + (v2gBatchId?`&batch_id=${v2gBatchId}`:''));
    if(!cands.length) { el.innerHTML = (batchOpts?`<div style="margin-bottom:8px;display:flex;align-items:center;gap:8px;">批次：${batchOpts}<span style="font-size:11px;color:var(--mut);">（v2g_candidates.source_type 已启用来源区分）</span></div>`:'') + '<div style="color:var(--mut);font-size:12px;">暂无候选（先抽取，或刷新查看最新批次）</div>'; return; }
    const pendN = cands.filter(c=>c.status==='pending').length;
    el.innerHTML = (batchOpts?`<div style="margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">批次：${batchOpts}<span style="font-size:11px;color:var(--mut);">共 ${cands.length} 条 · 待处理 ${pendN}</span></div>`:'') +
      `<table class="t"><tr><th>选</th><th>候选</th><th>类型</th><th>来源</th><th>批次</th><th>状态</th><th>校验错误</th></tr>` +
      cands.map(c=>`<tr>
        <td>${c.status==='pending'?`<input type="checkbox" class="v2g-check" value="${c.id}">`:'-'}</td>
        <td><b>${esc(c.entity_name)}</b></td>
        <td><span class="tag">${esc(c.entity_type)}</span></td>
        <td style="font-size:11px;">${v2gSrcBadge(c)}</td>
        <td style="font-size:10.5px;color:var(--mut);">${esc(c.batch_id||'-')}</td>
        <td><span class="st ${c.status==='confirmed'?'ok':c.status==='rejected'?'r':'w'}">${c.status}</span></td>
        <td style="font-size:11px;color:var(--red);">${esc(c.errors||'')}</td>
      </tr>`).join('') + '</table>' +
      (pendN?`<div style="padding:8px;display:flex;gap:8px;align-items:center;"><button class="btn sm" onclick="v2gCandReject()" style="color:var(--red);">🚫 批量驳回勾选</button><span style="font-size:11px;color:var(--mut);">勾选待处理候选后驳回（留痕原因）</span></div>`:'') +
      (v2gBatchId ? `<div style="padding:8px;text-align:right;"><button class="btn sm" onclick="gotoV2GBatch('${v2gBatchId}')">🗂 去抽取治理中心批量确认 →</button></div>` : '');
  } catch(e) { el.innerHTML = `<div style="color:var(--red);font-size:12px;">加载失败：${e.message}</div>`; }
}
async function v2gConfirm() {
  if(!branchWritable()) return;
  if(!v2gBatchId) { toast('请先抽取候选'); return; }
  const ids = Array.from(document.querySelectorAll('.v2g-check:checked')).map(x=>parseInt(x.value));
  if(!ids.length) { toast('请勾选要入库的候选'); return; }
  const r = await api('/api/knowledge/v2g/confirm', {method:'POST', body:JSON.stringify({batch_id:v2gBatchId, selected_ids:ids})});
  toast(`✅ 入库 ${r.confirmed} 条 / 拒绝 ${r.rejected.length} 条（chunk 溯源已链接）`);
  loadV2GCandidates(); loadGraph();
}
let _kbEnt = { page:1, size:15 };
async function loadKBEntities() {
  const status = document.getElementById('kb-status-filter').value;
  const search = document.getElementById('kb-search').value;
  const cat = document.getElementById('kb-cat-filter').value;
  let url = '/api/knowledge/entities?';
  url += 'branch=' + encodeURIComponent(getCurrentBranch()) + '&';   // KB分支隔离：实体浏览按当前工作分支
  if(status) url += `status=${status}&`;
  if(search) url += `search=${encodeURIComponent(search)}&`;
  let entities = await api(url);
  // P0-3: 知识类别前端二次过滤（后端接口无类别参数）
  if(cat) entities = entities.filter(e=>(e.knowledge_category||'')===cat);
  const total = entities.length;
  const pages = Math.max(1, Math.ceil(total/_kbEnt.size));
  if(_kbEnt.page > pages) _kbEnt.page = pages;
  const items = entities.slice((_kbEnt.page-1)*_kbEnt.size, _kbEnt.page*_kbEnt.size);
  const tbl = document.getElementById('kb-entity-table');
  tbl.innerHTML = `<tr><th>ID</th><th>名称</th><th>类型</th><th>知识类别</th><th>状态</th><th>分支</th><th>来源</th><th>操作</th></tr>` +
    (items.length ? items.map(e=>`<tr>
      <td>${e.id}</td><td><b>${e.name}</b></td><td>${e.entity_type}</td>
      <td>${kbCatLabel(e.knowledge_category)}</td>
      <td><span class="st ${e.status==='reviewed'?'ok':e.status==='candidate'?'w':'g'}">${e.status}</span></td>
      <td>${e.branch}</td><td>${e.source_type||'-'}</td>
      <td><button class="btn sm ghost" onclick="viewEntity('${e.id}')">查看</button>
          <button class="btn sm ghost" onclick="kbSetEntityCategory('${e.id}', '${esc(e.knowledge_category||'')}')" title="知识类别">🏷</button></td>
    </tr>`).join('') : '<tr><td colspan="8" style="text-align:center;color:var(--mut);padding:16px;">无匹配实体</td></tr>');
  renderPagerBar({
    el: document.getElementById('kb-ent-pager'), total, page: _kbEnt.page, size: _kbEnt.size,
    onPage: p => { _kbEnt.page = p; loadKBEntities(); },
    onSize: s => { _kbEnt.size = s; _kbEnt.page = 1; loadKBEntities(); }
  });
}
// 提交 kind 元数据（修改历史/分支时间线共用）：徽章中文标签 + st 颜色类
const COMMIT_KIND_META = {
  import:  {label:'导入',     cls:'b'},
  review:  {label:'审核',     cls:'ok'},
  merge:   {label:'合并',     cls:'g'},
  manual:  {label:'手动编辑', cls:'w'},
  rollback:{label:'回滚',     cls:'r'},
};
function commitKindBadge(kind) {
  const m = COMMIT_KIND_META[kind] || {};
  return `<span class="st ${m.cls||'g'}">${m.label||esc(kind||'')}</span>`;
}
async function viewEntity(id) {
  const e = await api(`/api/knowledge/entities/${id}`);
  const props = e.properties ? JSON.parse(e.properties) : {};
  // 一键追溯（创建/审核人/时间/来源文档元数据）
  const trace = `
    <h4 style="margin:12px 0 6px;font-size:13px;">📌 一键追溯</h4>
    <table class="t"><tr><th>元数据</th><th>值</th></tr>
      <tr><td>创建人</td><td>${esc(e.created_by||'-')}</td></tr>
      <tr><td>创建时间</td><td>${esc(e.created_at||'-')}</td></tr>
      <tr><td>审核人</td><td>${esc(e.reviewed_by||'-')}</td></tr>
      <tr><td>审核时间</td><td>${esc(e.reviewed_at||'-')}</td></tr>
      <tr><td>来源文档</td><td>${esc(e.source_doc||'-')}</td></tr>
      <tr><td>来源类型</td><td>${esc(e.source_type||'-')}</td></tr>
      <tr><td>图谱来源</td><td>${esc(e.graph_source||'manual')}</td></tr>
    </table>`;
  // 修改历史（分支版本管理 FR-KG-11：含该实体的提交时间线，id DESC）
  const khRows = (e.commit_history||[]).map(c=>{
    const f = c.fields||{};
    const fsum = (f.name||f.entity_type||f.status)
      ? ` <span style="color:var(--mut);font-size:11px;">（${esc(f.name||'')} · ${esc(f.entity_type||'')} · ${esc(f.status||'')}）</span>` : '';
    return `<div style="padding:5px 0;border-bottom:1px dashed var(--line);font-size:12px;">
      ${commitKindBadge(c.kind)} ${esc(c.message||'')}${fsum}
      <div style="color:var(--mut);font-size:11px;">${esc((c.created_at||'').slice(0,16))} · ${esc(c.created_by||'-')}</div>
    </div>`;
  }).join('');
  const hist = `<h4 style="margin:12px 0 6px;font-size:13px;">📜 修改历史</h4>` + (khRows
    ? `<div style="border:1px solid var(--line);border-radius:8px;padding:4px 10px;">${khRows}</div>`
    : '<div style="color:var(--mut);font-size:12px;">暂无修改记录（新实体一般只有基线）</div>');
  let html = `<h4 style="color:var(--blue-d);margin-bottom:10px;">${e.name} (${e.id})</h4>
    <div class="kv"><span>类型</span><b>${e.entity_type}</b></div>
    <div class="kv"><span>状态</span><b><span class="st ${e.status==='reviewed'?'ok':'w'}">${e.status}</span></b></div>
    <div class="kv"><span>分支</span><b>${e.branch}</b></div>
    <div class="kv"><span>知识类别</span><b>${kbCatLabel(e.knowledge_category)}
      <button class="btn sm ghost" style="margin-left:6px;" onclick="kbSetEntityCategory('${e.id}', '${esc(e.knowledge_category||'')}')">🏷 打标</button></b></div>
    <div class="kv"><span>发布时间</span><b>${e.published_at?`<span class="st ok">已发布 ${e.published_at}</span>`:'<span style="color:var(--mut);">未发布（未进入 release 基线）</span>'}</b></div>
    <div class="kv"><span>来源</span><b>${e.source_doc||e.source_type||'-'}</b></div>
    <h4 style="margin:12px 0 6px;">属性</h4>
    <table class="t"><tr><th>键</th><th>值</th></tr>${Object.entries(props).map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table>${trace}${hist}`;
  if(e.relations && e.relations.length) {
    html += `<h4 style="margin:12px 0 6px;">关系</h4><table class="t"><tr><th>源</th><th>关系</th><th>目标</th></tr>${e.relations.map(r=>`<tr><td>${r.source_name||r.source_id}</td><td>${r.relation_type}</td><td>${r.target_name||r.target_id}</td></tr>`).join('')}</table>`;
  }
  openPanel(`实体详情 · ${e.name}`, html);
}
function loadKBTab(id) {
  // 2026-09-18：先把 kb-c 的语境落定（术语词典是**显式子态**，由入口置 _kbCtxTerms=true 触发），
  // 必须在下方 chip 高亮之前完成 —— 否则高亮读到的是上一次导航残留的 _kbCtx。
  if(id === 'kb-c'){
    window._kbCtx = (window._kbCtxTerms === true) ? 'terms' : 'model';
    window._kbCtxTerms = false;   // 一次性消费，随即复位（深链/侧栏/角色快捷默认落「本体模型」）
  }
  // 2026-09-17 知识中心整合：资料库(kb-e)/图谱工作区(kb-d) 顶层双 Tab 切换器——
  // 显隐 + 高亮同步。chip 点击走 go('kb', tabId) 完整路由，此处只做状态回写。
  // 2026-09-18 知识中心收敛：知识域顶层 Tab 为 5 个
  // （数据看板 kb-a / 资料库 kb-e / 图谱工作区 kb-d / 本体模型 kb-c / 术语词典 kb-c+terms）；
  // 术语词典是 kb-c 的显式子态，按 _kbCtx 决定高亮哪一个 chip。
  // 2026-09-22 恢复 kb-b「标注审核」为正式 Tab（此前按用户要求暂不动；现按米爸要求恢复
  // 文档实体抽取与治理入口 —— 页面 HTML 与 fus-nav 四站流水线一直都在，缺的只是入口与高亮回写）。
  const hubTabs = document.getElementById('kbhub-tabs');
  if(hubTabs){
    // 2026-09-18：知识浏览(kb-a) 迁入后更名「数据看板」，知识域共 5 个顶层 Tab；
    // kb-a 也从"隐藏入口"变为正式 Tab。2026-09-22：kb-b（标注审核/治理中心）恢复为第 6 个 Tab。
    const isHub = (id==='kb-a'||id==='kb-d'||id==='kb-e'||id==='kb-c'||id==='kb-b');
    hubTabs.style.display = isHub ? 'flex' : 'none';
    const _isTerms = (window._kbCtx === 'terms');
    const tA = document.getElementById('kbhub-tab-a');
    const tE = document.getElementById('kbhub-tab-e'), tD = document.getElementById('kbhub-tab-d');
    const tC = document.getElementById('kbhub-tab-c'), tT = document.getElementById('kbhub-tab-t');
    const tB = document.getElementById('kbhub-tab-b');
    if(tA) tA.classList.toggle('on', id==='kb-a');
    if(tE) tE.classList.toggle('on', id==='kb-e');
    if(tD) tD.classList.toggle('on', id==='kb-d');
    if(tC) tC.classList.toggle('on', id==='kb-c' && !_isTerms);
    if(tT) tT.classList.toggle('on', id==='kb-c' && _isTerms);
    if(tB) tB.classList.toggle('on', id==='kb-b');
  }
  // 知识库顶部模块标题行：已全部停用（2026-09-18）
  // - 该行只剩一个模块标题（分支切换早已下沉到图谱数据行），与上方 Tab 栏信息重复；
  // - 原仅知识浏览(kb-a)保留，但 kb-a（数据看板）现已成为知识中心第一个 Tab 兼默认落点 → 一并隐藏，避免标题与 Tab 双份。
  const bar = document.getElementById('kb-toolbar');
  if(bar) bar.style.display = 'none';
  const mt = document.getElementById('kb-module-title');
  if(mt) mt.textContent = KB_TAB_TITLES[id] || '知识库';
  if(id==='kb-a') { loadKBStats(); loadKBEntities(); }
  if(id==='kb-e') loadDocs();
  if(id==='kb-b') { loadKBFlowBar(); v2gReviewLoad(); loadFusion(); loadTripleReviewPane(); }  // 2026-09-22 入口恢复：v2g-review-panel/fus-nav/三元组审核面板(triple-review-pane) 均在（index.html #kb-b）
  if(id==='kb-c') {
    // 2026-09-02 P0-2/P0-4：顶部 Tab=实体维度（类/对象属性/数据属性），图谱降为中栏视图；进入默认「类 · 编辑」
    // 2026-09-18：语境（terms/model）已在 loadKBTab 开头落定并消费掉入口标记，此处只读取结果。
    const _termsEntry = (window._kbCtx === 'terms');
    ontPaneMode = 'edit';
    const _row = document.getElementById('ont-subtab-row'); if(_row) _row.style.display = '';
    const _ontDef = document.querySelector('#ont-subtab-row [data-tabgrp="ont"][onclick*="\'classes\'"]');
    if(_ontDef) switchOntTab(_ontDef, 'classes');
    loadOntology(); loadOntologyReadiness();
    // 2026-09-17 R6：术语词典在同一次 loadKBTab 内同步切到术语视图，替代 02-shell.js 原先的
    // setTimeout(showOntTerms,500) 硬等待 —— 消除「500ms 内改点其它入口被强制拉回术语视图 + 面包屑被覆盖」的竞态。
    if(_termsEntry && typeof showOntTerms === 'function') showOntTerms();
  }
  if(id==='kb-d') loadGraph();
}
// ══ 统一分页组件（全局列表页共用）：右下角对齐，内容 = 总数 · 当前页/总页数 · 页码切换 · 每页条数下拉(15/20/50/100) ══
function renderPagerBar(o) {
  const el = o.el;
  if (!el) return;
  if (!o.total) { el.innerHTML = ''; return; }
  const size = o.size || 15;
  const pages = Math.max(1, Math.ceil(o.total / size));
  const page = Math.max(1, Math.min(o.page || 1, pages));
  const sizes = o.sizes || [15, 20, 50, 100];
  const pgBtn = (label, p, disabled) =>
    `<button class="btn sm ghost" style="padding:2px 8px;font-size:11px;" ${disabled?'disabled':''} data-pg-page="${p}" title="第 ${p} 页">${label}</button>`;
  // 页码窗口：首尾 + 当前页±2，间隔用省略号
  const numSet = new Set([1, pages]);
  for (let p = page - 2; p <= page + 2; p++) if (p >= 1 && p <= pages) numSet.add(p);
  const nums = [...numSet].sort((a,b)=>a-b).map((p,i,arr)=>
    (i>0 && p-arr[i-1]>1 ? '<span style="color:var(--mut);padding:0 1px;">…</span>' : '') +
    `<button class="btn sm ghost" style="padding:2px 7px;font-size:11px;${p===page?'background:var(--blue);color:#fff;border-color:var(--blue);':''}" data-pg-page="${p}">${p}</button>`
  ).join('');
  const sel = `<select class="pg-size" title="每页条数" style="border:1px solid var(--line);border-radius:6px;padding:2px 6px;font-size:11.5px;color:var(--mut);">` +
    sizes.map(s=>`<option value="${s}" ${s===size?'selected':''}>${s}</option>`).join('') + '</select>';
  el.innerHTML =
    `<span style="color:var(--mut);">共 <b style="color:var(--blue-d);">${o.total}</b> 条</span>` +
    `<span style="color:var(--mut);">第 <b style="color:var(--blue-d);">${page}</b>/<b>${pages}</b> 页</span>` +
    `<span style="flex:1"></span>` +
    pgBtn('«', 1, page<=1) + pgBtn('‹', page-1, page<=1) + nums + pgBtn('›', page+1, page>=pages) + pgBtn('»', pages, page>=pages) +
    `<span style="color:var(--mut);">每页 ${sel} 条</span>`;
  // 页码点击委派（data-pg-page 属性寻址）
  el.onclick = ev => {
    const t = ev.target;
    if (t && t.dataset && t.dataset.pgPage !== undefined && o.onPage) o.onPage(parseInt(t.dataset.pgPage, 10));
  };
  const selEl = el.querySelector('.pg-size');
  if (selEl) selEl.onchange = () => { if (o.onSize) o.onSize(parseInt(selEl.value, 10)); };
}
// ── 实体审核队列（标准审核：candidate → reviewed / deprecated；对齐行业质检工作台：待办优先 + 分区视图 + 分页）──
let _rev = { list:[], reviewed:[], deprecated:[], kw:'', type:'', confMin:0, seg:'candidate', page:1, size:15 };


/* ══════════════════════════════════════════════════════════════════════════
   P1 信息架构（2026-09-26）：四簇 Tab + 面板归位 + 首屏北极星 + 趋势 + 验收对照 + 筛选态下钻
   ══════════════════════════════════════════════════════════════════════════ */

/* 四簇定义：key 与后端 groups[].key / .kb-cluster[data-cluster] 一一对应 */
const KB_CLUSTERS = [
  {key:'gov',     name:'🧭 治理总览',   desc:'双作用域水位 · 治理红线 · 健康度 · 待评审队列'},
  {key:'quality', name:'🧪 数据质量',   desc:'类型覆盖 · 别名 · 消歧 · 属性 · 陈旧 · 本体漂移'},
  {key:'perf',    name:'⚡ 消费与性能', desc:'双引擎路由 · 知识获取 P/R/F1 · RT 分位 · 服务健康'},
  {key:'accept',  name:'🧾 验收对照',   desc:'甲方 7.1 六项 + PRD §2.2 五项同屏'},
];

/* 既有面板 → 簇（迁移后新增面板时，只在此表登记，不再改 index.html） */
const KB_CLUSTER_PANELS = {
  // 2026-09-27 合并：原「数据生命周期」面板已并入「知识资产总览」，故不再登记于此
  gov:     ['kb-stats','kb-redline-panel','kb-gov-panel','4ZRV0U97L2f38PKOWBIGbi'],
  quality: ['kb-quality-panel','uhgOBx7NfeTPELilhXOSMe'],
  perf:    ['kb-perf-panel','iug26oDGiHEfF0S8TvGmug','graphdb-panel','XuSdjBW2sLff9hupUja1NR','JW5MgT0MfYDwUg4DGsCbQn','QgL9kXACY7psuT06VjoRa8'],
  accept:  ['kb-accept-panel'],
};
let _kbClusterCur = 'gov';
let _kbWin = 'all';

function kbPanelEl(id){
  return document.getElementById(id) || document.querySelector('#kb-a [data-page-node-id="' + id + '"]');
}
/* 面板归位（幂等）：已在目标容器内则不动；否则 appendChild。
   这样不必对 1500 行 index.html 做手术，且删掉调用即可回到原布局。 */
function kbClusterize(){
  KB_CLUSTERS.forEach(c=>{
    const box = document.querySelector('.kb-cluster[data-cluster="' + c.key + '"]');
    if(!box) return;
    (KB_CLUSTER_PANELS[c.key]||[]).forEach(id=>{
      const el = kbPanelEl(id);
      if(el && el.parentElement !== box) box.appendChild(el);
    });
  });
  kbSetCluster(_kbClusterCur, true);
  kbApplyDetailPanels();
  kbTidyHeaders();
}
function kbRenderClusterTabs(){
  const el = document.getElementById('kb-cluster-tabs');
  if(!el) return;
  // UX-1：加「看板视图」前置标签，并把选中态改为**描边式**（页面级导航 #kbhub-tabs 是实心蓝）
  //   —— 两排 chip 形态一致时用户分不清层级，靠"实心=页面 / 描边=页内分簇"区分。
  const chips = KB_CLUSTERS.map(c=>{
    const on = _kbClusterCur === c.key;
    const n = kbClusterCount(c.key);
    // 选中态＝**描边式**（与页面级导航 #kbhub-tabs 的实心蓝区分）。
    // ⚠️ 底色必须与既有 .kbhub-chip 一致用白底：若用 transparent 会露出暗色页面底，
    //    而 --blue-d 是深蓝 → 暗色下对比度仅 1.84:1 不可读（2026-09-27 实测）。
    const style = on ? 'background:#fff;border-color:var(--blue);color:var(--blue-d);font-weight:600;' : '';
    return `<span class="kbhub-chip ${on?'on':''}" data-cluster="${c.key}" style="${style}"
      title="${esc(c.desc)}" onclick="kbSetCluster('${c.key}')">${c.name} <small style="font-size:10px;">${n}</small></span>`;
  }).join('');
  // UX-2：冗余明细面板的展开开关（数据不丢，只是默认收起）
  const dc = kbDetailCount(_kbClusterCur);
  const detail = dc ? `<span class="kbhub-chip" data-kb-detail-toggle style="border-style:dashed;"
      title="该簇下有 ${dc} 个明细面板与上方指标重复，默认收起"
      onclick="kbToggleDetail('${_kbClusterCur}')">${_kbDetailShown[_kbClusterCur]?'▾ 收起明细':'▸ 明细面板 '+dc}</span>` : '';
  el.innerHTML = `<span style="font-size:11px;color:var(--color-text-muted);">看板视图</span>${chips}
    <span style="flex:1"></span>${detail}`;
}
function kbClusterCount(key){
  const d = window._kbDash; if(!d) return '';
  const map = {gov:['governance'], quality:['quality'], perf:['consumption','performance']};
  const ks = map[key]; if(!ks) return (key==='accept' ? (d.acceptance||[]).length : '');
  let n = 0; (d.groups||[]).forEach(g=>{ if(ks.indexOf(g.key)>=0) n += (g.items||[]).length; });
  return n;
}
function kbSetCluster(key, silent){
  _kbClusterCur = key;
  document.querySelectorAll('#kb-a .kb-cluster').forEach(b=>{
    b.style.display = (b.dataset.cluster === key) ? '' : 'none';
  });
  document.querySelectorAll('#kb-cluster-tabs .kbhub-chip').forEach(ch=>{
    ch.classList.toggle('on', ch.dataset.cluster === key);
  });
  if(key === 'accept') kbRenderAcceptance(window._kbDash);
  if(!silent) kbPaintSparklines();
  // 必须重渲染 Tab 行：右侧「▸ 明细面板 N」开关是按**当前簇**渲染的，
  // 不重渲染则切到 perf/quality 簇后看不到开关、被收起的明细面板无从展开（2026-09-27 实测修）
  kbRenderClusterTabs();
}
function kbSetWindow(w){ _kbWin = w; loadKBDashboard(); }
function kbResetWindow(){ _kbWin = 'all'; loadKBDashboard(); }

/* 顶栏：时间窗 + 工作分支 + 刷新 + 记录快照 */
/* 顶栏：时间窗 + 刷新 + 记录快照 + 时间戳。
   注：2026-09-27 按用户要求**移除**了此处的「跳转子页」入口 ——
   从 #kbhub-tabs 派生子页跳转虽然是"永不漂移"的写法，但一个页面里出现两处子页入口属于重复导航；
   子页跳转统一由左侧「知识中心」菜单承担。 */
function kbRenderTopbar(d){
  const el = document.getElementById('kb-dash-topbar'); if(!el) return;
  const win = _kbWin || 'all';
  el.innerHTML = `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
    <span style="font-size:11px;color:var(--color-text-muted);">时间窗</span>
    ${[['7d','近 7 天'],['30d','近 30 天'],['all','全部']].map(([k,t])=>
      `<span class="kbhub-chip ${win===k?'on':''}" data-kb-win="${k}" onclick="kbSetWindow('${k}')">${t}</span>`).join('')}
    <span style="flex:1"></span>
    <button class="btn sm ghost" onclick="loadKBDashboard(true)" title="现场跑一次 SHACL 门禁（约 1s）">刷新（含 SHACL）</button>
    <button class="btn sm ghost" onclick="kbSnapshot()" title="把当前有值的指标写入快照表（按天幂等），供 sparkline 趋势">📌 记录快照</button>
    <span style="font-size:11px;color:var(--color-text-muted);">${esc(d.generated_at||'')}</span>
  </div>`;
}
async function kbSnapshot(){
  try{
    const r = await api('/api/knowledge/dashboard?branch=' + encodeURIComponent(getCurrentBranch())
      + '&window=' + encodeURIComponent(_kbWin||'all') + '&persist=true');
    window._kbDash = r;
    toast('✅ 已记录 ' + ((r.summary&&r.summary.scored)||'') + ' 项指标快照');
    kbLoadTrends();
  }catch(e){ toast('快照失败：' + (e.message||e)); }
}

/* 首屏北极星：健康分 + 3 项水位（健康分口径由后端给出，前端不自己算） */
function kbRenderNorthstar(d){
  const el = document.getElementById('kb-northstar'); if(!el) return;
  const ns = d.northstar||{}, sc = {ok:'ok',warn:'w',alert:'r'};
  const hs = ns.health_score;
  const hsSt = (hs===null||hs===undefined) ? 'g' : (hs>=80?'ok':(hs>=60?'w':'r'));
  const hsWord = (hs===null||hs===undefined) ? '—' : (hs>=80?'良好':(hs>=60?'关注':'告警'));
  const cards = (ns.keys||[]).map(k=>(d.metrics||{})[k]).filter(Boolean).map(m=>{
    const can = m.drill && m.drill.page;
    const click = can ? `onclick="kbDrill('${m.drill.page}','${m.drill.kind||''}','${esc(String(m.drill.value||''))}')"` : '';
    return `<div class="asset" data-mkey="${esc(m.key)}" style="text-align:left;cursor:${can?'pointer':'default'};" ${click} title="${esc(m.threshold||'')}">
      <div class="l" style="display:flex;justify-content:space-between;gap:6px;"><span>${esc(m.name)}${kbInfo(m)}</span><span class="st ${sc[m.status]||'g'}">${esc(m.status)}</span></div>
      <div class="n">${m.value===null?'—':m.value}<small style="font-size:11px;color:var(--mut);"> ${esc(m.unit||'')}</small><span class="kb-spark"></span></div>
      <div class="l">目标 ${esc(m.target||'—')}</div></div>`;
  }).join('');
  el.innerHTML = `<div class="panel" style="margin-bottom:14px;">
    <div class="ph">🎯 首屏北极星 <span class="badge">P1</span><span class="tag">健康分 + 3 项水位；未落地 ${ns.na||0} 项不计入健康分（不拿假 0 充数）</span><span style="flex:1"></span></div>
    <div class="pb"><div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">
      <div class="asset" style="text-align:left;"><div class="l" style="display:flex;justify-content:space-between;"><span>知识健康分</span><span class="st ${hsSt}">${hsWord}</span></div>
        <div class="n" style="font-size:24px;">${hs===null?'—':hs}<small style="font-size:11px;color:var(--mut);"> /100</small></div>
        <div class="l">ok ${d.summary.ok} · warn ${d.summary.warn} · alert ${d.summary.alert}（计分 ${ns.scored||0} 项）</div></div>
      ${cards}</div></div></div>`;
}

/* 验收对照（甲方 7.1 × PRD §2.2）：值是后端按 metric_key 引用的同一口径 */
function kbRenderAcceptance(d){
  const el = document.getElementById('kb-accept'); if(!el || !d) return;
  const sc = {ok:'ok',warn:'w',alert:'r'};
  const list = d.acceptance||[];
  const word = a => a.status==='ok' ? '达标' : (a.value===null||a.value===undefined ? '无数据' : (a.status==='warn'?'未达标':'不达标'));
  const rows = list.map(a=>`<tr>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;">${esc(a.clause)}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);">${esc(a.requirement)}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;">${esc(a.target)}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;"><b style="color:var(--blue-d);">${a.value===null||a.value===undefined?'—':a.value}${esc(a.unit||'')}</b>${kbInfo(a.calc, a.clause)}</td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;"><span class="st ${sc[a.status]||'g'}">${word(a)}</span></td>
    <td style="padding:6px 8px;border-bottom:1px solid var(--line);color:var(--mut);">${esc(a.evidence||'')}</td></tr>`).join('');
  const cnt = s => list.filter(a=>a.status===s).length;
  el.innerHTML = `<div style="font-size:11.5px;color:var(--mut);margin-bottom:8px;line-height:1.7;">
      合计 ${list.length} 条：<span class="st ok">达标 ${cnt('ok')}</span>
      <span class="st w">未达标/无数据 ${cnt('warn')}</span> <span class="st r">不达标 ${cnt('alert')}</span>
      —— 「实测」列与上方指标<b>同源</b>（后端按 metric_key 引用，不重复计算）；无实测数据的行留空并注明，不预置假值。</div>
    <table class="t" style="width:100%;font-size:12px;color:var(--mut);">
      <tr><th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">条款</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">要求</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">目标</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">实测</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">判定</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--line);">证据 / 口径</th></tr>
      ${rows}</table>`;
}

/* 趋势：拉快照 → 画 sparkline（无 2 个点以上不画线，绝不伪造趋势） */
async function kbLoadTrends(){
  try{
    const r = await api('/api/knowledge/dashboard/trend?branch=' + encodeURIComponent(getCurrentBranch()));
    window._kbTrends = r.trends || {};
    kbPaintSparklines();
  }catch(e){ window._kbTrends = {}; }
}
function kbSpark(points){
  const vs = (points||[]).map(p=>Number(p.value)).filter(v=>!isNaN(v));
  if(vs.length < 2) return '';
  const w=64, h=16, mn=Math.min.apply(null,vs), mx=Math.max.apply(null,vs), span=(mx-mn)||1;
  const pts = vs.map((v,i)=>((i*(w/(vs.length-1))).toFixed(1)) + ',' + ((h-((v-mn)/span)*(h-5)-2.5).toFixed(1))).join(' ');
  return `<svg width="${w}" height="${h}" style="vertical-align:middle;margin-left:6px;"
    title="近 ${vs.length} 个快照：${vs.join(' → ')}"><polyline points="${pts}" fill="none" stroke="var(--blue)" stroke-width="1.5"/></svg>`;
}
function kbPaintSparklines(){
  const T = window._kbTrends || {};
  document.querySelectorAll('#kb-a [data-mkey]').forEach(el=>{
    const holder = el.querySelector('.kb-spark'); if(!holder) return;
    const pts = T[el.dataset.mkey];
    holder.innerHTML = (pts && pts.length > 1) ? kbSpark(pts) : '';
  });
}

/* 下钻：先跳转，再对**支持筛选的落点**施加筛选态。
   ⚠️ 只支持三条真能落地的路径，其余明确提示"暂无筛选落点"——不假装支持：
     · entity_status  → 本页「知识实体浏览」按状态过滤
     · entity_keyword → 本页「知识实体浏览」按关键字过滤
     · graph_isolated → 知识图谱工作区把 hideIsolated 置 false（显示孤立节点待清理） */
function kbDrill(page, kind, value){
  if(!page) return;
  try{
    const p = String(page);
    // 落点要落在**看得见目标面板**的簇：entity_* 类下钻的目标是「知识实体浏览」，
    // 该面板在 perf 簇 —— 若切到 gov 簇，用户跳过去却看不到表格（2026-09-26 修正）
    if(p.indexOf('kb-') === 0){
      const k = String(kind||'');
      const cluster = (p === 'kb-a' && k.indexOf('entity_') === 0) ? 'perf'
                    : (p === 'kb-a' ? 'gov' : (_kbClusterCur || 'gov'));
      kbSetCluster(cluster, true); go('kb', p);
    }
    else go(p);
  }catch(e){}
  if(kind === 'entity_status'){
    kbAfterDrill(()=>{ const s=document.getElementById('kb-status-filter');
      if(s){ s.value = value || 'candidate'; loadKBEntities(); toast('已按状态「' + s.value + '」筛选实体'); } });
  } else if(kind === 'entity_keyword'){
    kbAfterDrill(()=>{ const q=document.getElementById('kb-search');
      if(q){ q.value = value || ''; loadKBEntities(); toast('已按关键字「' + q.value + '」筛选实体'); } });
  } else if(kind === 'graph_isolated'){
    // ⚠️ graphState 由 23-ontform.js 以 `let` 声明 → 顶层 let 不挂 window，
    //    必须用 typeof 守卫（`window.graphState` 恒为 undefined，2026-09-26 实测）
    kbAfterDrill(()=>{ if(typeof graphState !== 'undefined'){ graphState.view = graphState.view||{}; graphState.view.hideIsolated = false; }
      if(typeof loadGraph === 'function') loadGraph();
      toast('图谱已显示孤立节点（hideIsolated=false）'); });
  } else {
    toast('已跳转（该指标暂无筛选落点）');
  }
}
function kbAfterDrill(fn){
  setTimeout(()=>{ try{ fn(); }catch(e){ toast('筛选失败：' + (e.message||e)); } }, 800);
}

/* V7 标题行减负：内部需求编号 badge 收进 tooltip（隐藏但保留 DOM） */
/* UX-3 标题瘦身（V7 扩展）：内部编号 badge + 过长说明 tag 一律收进 tooltip。
   审计实测：14 个面板标题平均 52 字、最长 93 字 —— 标题行被说明文字淹没，扫读困难。
   目标：标题只留面板名（≤16 字），说明与编号进 title（可回溯、不丢信息）。 */
function kbTidyHeaders(){
  document.querySelectorAll('#kb-a .ph .badge').forEach(b=>{
    if(b.dataset.kbTidied) return;
    const t = (b.textContent||'').trim();
    const ph = b.closest('.ph');
    if(ph && t) ph.title = (ph.title ? ph.title + ' ｜ ' : '') + '内部编号：' + t;
    b.dataset.kbTidied = '1';
    b.style.display = 'none';
  });
  document.querySelectorAll('#kb-a .ph .tag').forEach(t=>{
    if(t.dataset.kbTidied) return;
    const s = (t.textContent||'').trim();
    const ph = t.closest('.ph');
    if(ph && s.length > 10){        // 短标签（如计数）保留，长说明才收
      ph.title = (ph.title ? ph.title + ' ｜ ' : '') + s;
      t.dataset.kbTidied = '1';
      t.style.display = 'none';
    }
  });
}
const KB_DETAIL_PANELS = {
  perf:    ['iug26oDGiHEfF0S8TvGmug'],      // 知识引擎双引擎消费
  quality: ['uhgOBx7NfeTPELilhXOSMe'],      // 知识完整度
};
let _kbDetailShown = {};
function kbDetailCount(cl){ return (KB_DETAIL_PANELS[cl]||[]).length; }
function kbApplyDetailPanels(){
  Object.keys(KB_DETAIL_PANELS).forEach(cl=>{
    const shown = !!_kbDetailShown[cl];
    KB_DETAIL_PANELS[cl].forEach(id=>{
      const el = kbPanelEl(id);
      if(el){ el.dataset.kbDetail = '1'; el.style.display = shown ? '' : 'none'; }
    });
  });
}
function kbToggleDetail(cl){
  _kbDetailShown[cl] = !_kbDetailShown[cl];
  kbApplyDetailPanels();
  kbRenderClusterTabs();
}

/* ══════════════════════════════════════════════════════════════════════════
   计算说明（calc）：默认不展示，指标名旁一个 ⓘ，hover / 键盘聚焦时弹出。
   · 文本来自后端 `metric.calc`（与指标同源，前端不手写口径）
   · 浮层 position:fixed 且挂在 body → 不受面板滚动容器裁剪
   · ⓘ 的 onclick 阻止冒泡：指标卡可点击下钻，点图标不应触发下钻
   ══════════════════════════════════════════════════════════════════════════ */
function kbInfo(x, name) {
  const calc = (typeof x === 'string') ? x : (x && x.calc);
  const nm = name || (x && x.name) || (x && x.key) || '';
  if (!calc) return '';
  return `<span class="kb-i" tabindex="0" data-calc="${esc(calc)}" data-kb-name="${esc(nm)}"
    style="cursor:help;font-size:11px;color:var(--blue);margin-left:3px;vertical-align:1px;"
    onclick="event.stopPropagation()" title="悬停查看计算说明">ⓘ</span>`;
}
let _kbTipReady = false;
function kbTipInit() {
  if (_kbTipReady) return;
  _kbTipReady = true;
  const tip = document.createElement('div');
  tip.id = 'kb-tip';
  tip.className = 'panel';
  tip.style.cssText = 'position:fixed;z-index:9999;display:none;max-width:440px;'
    + 'padding:8px 10px;font-size:11.5px;line-height:1.7;color:var(--mut);'
    + 'white-space:pre-wrap;word-break:break-word;';
  document.body.appendChild(tip);
  const show = (el) => {
    const nm = el.dataset.kbName || '';
    tip.innerHTML = `<b style="color:var(--blue-d);">计算说明${nm ? ' · ' + esc(nm) : ''}</b><br>`
      + esc(el.dataset.calc || '');
    tip.style.display = 'block';
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - w - 8));
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  };
  const hide = () => { tip.style.display = 'none'; };
  document.addEventListener('mouseover', e => {
    const el = e.target.closest && e.target.closest('[data-calc]');
    if (el) show(el);
  });
  document.addEventListener('mouseout', e => {
    const el = e.target.closest && e.target.closest('[data-calc]');
    if (el && !el.contains(e.relatedTarget)) hide();
  });
  document.addEventListener('focusin', e => {
    const el = e.target.closest && e.target.closest('[data-calc]');
    if (el) show(el);
  });
  document.addEventListener('focusout', hide);
  window.kbTipHide = hide;   // 供脚本/测试显式收起
}
