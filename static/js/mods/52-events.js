/* 事件驱动面板（Super-agent 落地 P2-2）—— 2026-11-01
 *
 * 为什么要有这个面板
 * ------------------
 * 事件底座的产出（observe 工单 = "本应做什么"）此前**只在 API 里**，
 * 不做界面就等于"记录了但没人看得到" —— 这是最影响实用性的缺口。
 *
 * 放在哪 / 为什么
 * --------------
 * 挂在**运行监控页（ops）最上方**，不新增左侧导航项。
 * 理由（Super-agent 原文 000-philosophy）："A panel that shows only what needs a person.
 * It lists first what waits for a person." ⇒ 需要人处理的排最前，
 * 且不为了它多占一个导航位（导航膨胀本身就是注意力成本）。
 *
 * 展示什么（四块，每块都对应一个"人的问题"）
 * ------------------------------------------
 *   ① 待审工单（observe）—— "我该处理什么"  ← 核心
 *   ② 已执行（done）      —— "自动帮我把什么做完了"
 *   ③ 事件台账            —— "这条为什么没动"（含被过滤的 + reason）
 *   ④ 来源与开关          —— "现在哪些源在观察 / 怎么放开"
 *
 * 纪律（沿用本工程既有三条）
 * --------------------------
 * · 不阻塞：每个区块独立 try/catch，任一接口挂了不影响其它块（与 loadOps 同思路）；
 * · 不打扰：**不做自动轮询**，只在进入页面与手动刷新时取数；
 * · 不越权：切 execute 是"放开自动执行"的唯一开关，**必须二次确认**并提示副作用。
 */

let _evCache = { stats: null, wi: [], trg: [], src: [], stuck: 0 };

function evFmtTime(s){
  if(!s) return '';
  // 后端时间多为 UTC（SQLite CURRENT_TIMESTAMP）；仅做展示，不做时区换算（与本仓其它面板一致）
  return String(s).replace('T',' ').slice(0,19);
}

function evBadge(mode){
  if(mode === 'execute') return '<span class="st r" title="已放行自动执行">execute</span>';
  return '<span class="st g" title="只记录不执行">observe</span>';
}

function evStateBadge(state){
  const m = {open:'b', running:'w', done:'ok', failed:'r', stopped:'g'};
  const cls = m[state] || 'g';
  return '<span class="st ' + cls + '">' + esc(state || '-') + '</span>';
}

async function loadEventPanel(){
  const box = document.getElementById('ev-panel');
  if(!box) return;

  // 四路并行 + 各自独立降级（任一失败不影响其它块）
  const [stats, wi, trg, src, stuck] = await Promise.all([
    api('/api/events/stats').catch(()=>null),
    api('/api/events/workitems?limit=60').catch(()=>null),
    api('/api/events/triggers?limit=40').catch(()=>null),
    api('/api/events/sources').catch(()=>null),
    api('/api/events/stuck?stale_s=1800').catch(()=>null),
  ]);

  const s = (stats && stats.stats) || {};
  const obsOpen = ((wi && wi.items) || []).filter(x=>x.mode==='observe' && (x.state==='open'||x.state==='running'));
  const done = ((wi && wi.items) || []).filter(x=>x.state==='done' || x.state==='failed');
  const unreg = (stats === null);

  if(unreg){
    box.innerHTML = '<div class="mut" style="padding:10px;font-size:13.5px;">事件接口不可用（后端未启动或未迁移 ev_* 三表）</div>';
    return;
  }

  const stuckN = (stuck && stuck.count) || 0;

  // ── 头部 KPI（四卡，全部来自 /api/events/stats 与 /api/events/stuck）──
  const kpi = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px;">
      <div class="kpi"><div class="n">${s.triggers||0}</div><div class="l">事件总数 <span class="st g">${s.triggers_filtered||0} 已过滤</span></div></div>
      <div class="kpi"><div class="n" style="color:${obsOpen.length?'var(--amb)':'var(--grn)'};">${obsOpen.length}</div><div class="l">待审工单 <span class="st g">观察期只记录</span></div></div>
      <div class="kpi"><div class="n">${(s.workitems||0) - obsOpen.length}</div><div class="l">已执行/已结 <span class="st b">含失败</span></div></div>
      <div class="kpi"><div class="n" style="color:${stuckN?'var(--red)':'var(--grn)'};">${stuckN}</div><div class="l">停滞工单 <span class="st ${stuckN?'r':'ok'}">超 30 分钟未推进</span></div></div>
    </div>`;

  // ── ① 待审工单（核心：这是"本应做什么"）──
  const rowsObs = obsOpen.length ? obsOpen.map(x=>`
      <tr>
        <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escA(x.title)}">${esc(x.title)}</td>
        <td>${esc(x.source)}</td>
        <td><code style="font-size:12px;">${esc(x.action)}</code></td>
        <td>${evBadge(x.mode)}</td>
        <td>${esc(x.strength)}</td>
        <td>${esc(x.policy_version)}</td>
        <td>${evFmtTime(x.created_at)}</td>
        <td style="white-space:nowrap;">
          <button class="btn" style="font-size:12px;padding:2px 8px;" onclick="evHandoff('${escA(x.id)}')" title="挂到当前会话继续推进（并行认领，不转移）">接力</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="8" class="mut" style="padding:12px;">暂无待审工单（观察期无事件，或事件均已被过滤）</td></tr>';

  // ── ② 已执行 ──
  const rowsDone = done.length ? done.slice(0,20).map(x=>`
      <tr>
        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escA(x.title)}">${esc(x.title)}</td>
        <td><code style="font-size:12px;">${esc(x.action)}</code></td>
        <td>${evStateBadge(x.state)}</td>
        <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px;" title="${escA(x.result||x.error||'')}">${esc((x.result||x.error||'').slice(0,120))}</td>
        <td>${evFmtTime(x.updated_at||x.created_at)}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" class="mut" style="padding:12px;">暂无已执行工单</td></tr>';

  // ── ③ 事件台账（含被过滤的 —— 回答"为什么没动"）──
  const rowsTrg = ((trg && trg.items) || []).length ? trg.items.map(x=>`
      <tr>
        <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escA(x.text)}">${esc(x.text||'(无正文)')}</td>
        <td>${esc(x.source)}</td>
        <td>${esc(x.actor)}${x.actor_type==='system'?' <span class="st g">系统</span>':''}</td>
        <td>${x.disposition==='admitted'?'<span class="st ok">admitted</span>'
            :(x.disposition==='filtered'?'<span class="st g">filtered</span>'
            :'<span class="st b">'+esc(x.disposition)+'</span>')}</td>
        <td style="font-size:12.5px;color:var(--mut);">${esc(x.reason||'')}${x.hit_count>0?' <span title="幂等命中次数">×'+(x.hit_count+1)+'</span>':''}</td>
        <td>${evFmtTime(x.created_at)}</td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="mut" style="padding:12px;">暂无事件</td></tr>';

  // ── ④ 来源与开关 ──
  const rowsSrc = ((src && src.items) || []).length ? src.items.map(x=>`
      <tr>
        <td><code>${esc(x.kind)}</code></td>
        <td>${esc(x.title||'')}</td>
        <td>${evBadge(x.mode)}</td>
        <td>${esc(x.policy_version)}</td>
        <td style="font-size:12.5px;color:var(--mut);">${esc(x.listen)} · 白名单 ${esc((x.allowed_json||'[]'))}</td>
        <td style="white-space:nowrap;">
          ${x.mode==='observe'
            ? `<button class="btn" style="font-size:12px;padding:2px 8px;" onclick="evSetMode('${escA(x.kind)}','execute')" title="放开自动执行（需二次确认）">放开执行</button>`
            : `<button class="btn" style="font-size:12px;padding:2px 8px;" onclick="evSetMode('${escA(x.kind)}','observe')" title="切回只观察">收回执行</button>`}
        </td>
      </tr>`).join('')
    : '<tr><td colspan="6" class="mut" style="padding:12px;">暂无已注册来源</td></tr>';

  box.innerHTML = kpi + `
    <div class="panel" style="margin-bottom:14px;">
      <div class="ph">① 待审工单 <span class="badge">观察期：只记录不执行</span><span class="badge">${obsOpen.length} 条</span>
        <span style="flex:1"></span>
        <button class="btn" style="font-size:12.5px;padding:3px 10px;" onclick="loadEventPanel()" title="刷新（本面板不自动轮询）">刷新</button>
      </div>
      <div class="pb" style="padding:0;max-height:300px;overflow:auto;">
        <table style="width:100%;font-size:13px;">
          <thead><tr><th>工单</th><th>来源</th><th>建议动作</th><th>模式</th><th>依据</th><th>策略</th><th>创建</th><th></th></tr></thead>
          <tbody>${rowsObs}</tbody>
        </table>
      </div>
    </div>

    <div class="panel" style="margin-bottom:14px;">
      <div class="ph">② 已执行 / 已结 <span class="badge">闭环回执</span><span class="badge">${done.length} 条</span></div>
      <div class="pb" style="padding:0;max-height:260px;overflow:auto;">
        <table style="width:100%;font-size:13px;">
          <thead><tr><th>工单</th><th>动作</th><th>状态</th><th>结果 / 错误</th><th>时间</th></tr></thead>
          <tbody>${rowsDone}</tbody>
        </table>
      </div>
    </div>

    <div class="panel" style="margin-bottom:14px;">
      <div class="ph">③ 事件台账 <span class="badge">含被过滤的 —— 回答"为什么没动"</span><span class="badge">${(trg&&trg.items||[]).length} 条</span></div>
      <div class="pb" style="padding:0;max-height:280px;overflow:auto;">
        <table style="width:100%;font-size:13px;">
          <thead><tr><th>事件</th><th>来源</th><th>发起者</th><th>处置</th><th>原因</th><th>时间</th></tr></thead>
          <tbody>${rowsTrg}</tbody>
        </table>
      </div>
    </div>

    <div class="panel" style="margin-bottom:14px;">
      <div class="ph">④ 来源与开关 <span class="badge">放开执行是逐来源显式动作</span></div>
      <div class="pb" style="padding:0;max-height:240px;overflow:auto;">
        <table style="width:100%;font-size:13px;">
          <thead><tr><th>来源</th><th>名称</th><th>模式</th><th>策略版本</th><th>接收范围</th><th></th></tr></thead>
          <tbody>${rowsSrc}</tbody>
        </table>
      </div>
    </div>`;
}

/* 切来源模式 —— 放开自动执行的唯一开关，必须二次确认 */
async function evSetMode(kind, mode){
  if(mode === 'execute'){
    const ok = confirm(
      '放开「' + kind + '」的自动执行？\n\n' +
      '· 之后该来源的新事件会**自动执行动作**（可能写入候选表/图谱）\n' +
      '· 已建的观察期工单不受影响（模式是快照）\n' +
      '· 建议先开观察期 3~5 天、确认"本应做什么"清单合理后再放开\n\n' +
      '确定放开？');
    if(!ok) return;
  }
  try{
    const r = await api('/api/events/sources/' + encodeURIComponent(kind) + '/mode', {
      method:'POST', body: JSON.stringify({mode: mode})
    });
    if(r && r.ok){ toast('来源 ' + kind + ' 已切为 ' + mode); loadEventPanel(); }
    else { toast('切换失败：' + ((r && r.reason) || '未知'), 3000); }
  }catch(e){ toast('切换失败：' + e.message, 3000); }
}

/* 跨会话接力：挂到当前会话（并行认领，不转移） */
async function evHandoff(wid){
  const cid = (typeof currentConvId !== 'undefined' && currentConvId) || 0;
  if(!cid){ toast('请先打开一个会话再接力', 2500); return; }
  try{
    const r = await api('/api/events/workitems/' + encodeURIComponent(wid) + '/handoff', {
      method:'POST', body: JSON.stringify({conversation_id: cid})
    });
    if(r && r.ok){
      const ctx = r.context || {};
      const m = ctx.mandate || {};
      toast('已挂到当前会话：' + (ctx.title || '').slice(0,40), 3000);
      // 把接力上下文注入输入框（不改会话历史，只给人看"当初凭什么做"）
      try{
        const ta = document.getElementById('chat-input') || document.getElementById('msg-input');
        if(ta && ctx.refs){
          const line = '【事件工单接力】' + (ctx.title||'') +
                       '｜动作 ' + (ctx.action||'') +
                       '｜依据 ' + (m.strength||'') + '/' + (m.policy_version||'') +
                       '｜线程 ' + (ctx.refs.thread_key||'');
          ta.value = (ta.value ? ta.value + '\n' : '') + line;
        }
      }catch(_){}
      loadEventPanel();
    }else{ toast('接力失败：' + ((r && r.reason)||'未知'), 3000); }
  }catch(e){ toast('接力失败：' + e.message, 3000); }
}
