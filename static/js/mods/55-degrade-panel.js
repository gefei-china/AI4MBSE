/* 静默兜底台账面板（P0：让错误可见）—— 2026-11-25
 *
 * 为什么要有这个面板
 * ------------------
 * 架构评估 P0 实测：全仓 1980 个 `except Exception` 里 **1361 个是静默吞掉（69%）**。
 * 这些兜底让系统"永远能跑通"，代价是**失败模式对所有人不可见**：
 *   · 图库 `enabled=true` 却查不到数据（`get_writer` 静默降级 Mock）；
 *   · 门禁该 enforce 却按 warn 放过（`gate_mode` 读配置失败静默返回 warn）；
 *   · 看板显示"违规 0 条"，实际是 SQL 炸了（metrics 兜底返回 0）。
 * 这三个的共同点不是"报错没打日志"，而是**界面上一切正常**。
 *
 * 与聊天里那类卡片的分工
 * ----------------------
 *   · 执行过程中（某一条回答里发生了什么）→ 由 11-pipeline.js 的 `procAddDegrade`
 *     实时展示，数据走 SSE 的 `degrade` 事件（零延迟、保序）；
 *   · 全局（这台机器整体哪里在漏）→ 本面板，数据走 `GET /api/health/degrade`。
 * 两者不是重复：前者回答"这一轮的结果能不能信"，后者回答"系统哪里坏了"。
 *
 * 纪律（与 52-events.js 同源）
 * ----------------------------
 * · **不自动轮询**：只在进入页面与手动刷新时取数（降级是常态，轮询会制造焦虑）；
 * · **不静默**：接口挂了要显示"台账不可用"，不能显示成"0 条降级"——
 *   那正是本面板要消灭的那种欺骗（0 违规 vs 门禁没跑，在日志上不可区分）；
 * · **不提供清空按钮**：清空会让"降级过"变成"从没降级过"。要清零只能重启进程。
 */

let _dgCache = null;

function dgLevelBadge(lv){
  const m = {critical:['r','结果不可信'], degraded:['w','功能降级'], cosmetic:['g','轻微']};
  const [cls, txt] = m[lv] || ['g', String(lv || '-')];
  return `<span class="st ${cls}">${esc(txt)}</span>`;
}

function dgFmtTs(ts){
  const n = Number(ts || 0);
  if(!n) return '-';
  // 后端给的是 epoch 秒（time.time()），与本仓其它面板一致按本地时间展示
  try{
    const d = new Date(n * 1000);
    const p = x => String(x).padStart(2, '0');
    return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }catch(e){ return '-'; }
}

function dgRender(snap){
  const by = (snap && snap.by_level) || {};
  const worst = (snap && snap.worst) || [];
  const total = Number((snap && snap.total) || 0);

  // 三档计数：先给结论（这机器到底脏不脏），再给明细
  let h = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;">';
  const card = (lv, cls) => {
    const n = Number(by[lv] || 0);
    return `<div class="kpi" style="padding:10px 12px;">`
      + `<div class="n" style="font-size:20.5px;color:${n ? ('var(--' + cls + ')') : 'var(--mut)'};">${n}</div>`
      + `<div class="l">${dgLevelBadge(lv)}</div></div>`;
  };
  h += card('critical', 'red');
  h += card('degraded', 'amb');
  h += card('cosmetic', 'mut');
  h += '</div>';

  if(!total){
    // 关键：0 的含义必须写清楚 —— "真的零降级"，而不是"没统计"
    h += '<div class="mut" style="padding:12px;font-size:13.5px;line-height:1.7;">'
      + '本进程自启动以来<b>没有记录到任何降级</b>（不是"没统计"——台账读取正常，'
      + '计数器为 0）。若你怀疑系统在静默降级，说明那条路径还没接入台账，'
      + '应把它登记到 <code>core/degrade.py</code> 的调用点。'
      + '</div>';
    return h;
  }

  h += '<div style="max-height:400px;overflow:auto;">'
    + '<table style="width:100%;border-collapse:collapse;font-size:13.5px;">'
    + '<thead><tr style="text-align:left;color:var(--mut);">'
    + '<th style="padding:6px 8px;width:26%;">位置</th>'
    + '<th style="padding:6px 8px;width:14%;">级别</th>'
    + '<th style="padding:6px 8px;width:8%;">次数</th>'
    + '<th style="padding:6px 8px;">影响 / 最近错误</th>'
    + '<th style="padding:6px 8px;width:14%;">最近发生</th></tr></thead><tbody>';
  worst.forEach(w=>{
    h += '<tr style="border-top:1px solid var(--line);">'
      + `<td style="padding:6px 8px;"><code style="font-size:12px;">${esc(w.where || '-')}</code></td>`
      + `<td style="padding:6px 8px;">${dgLevelBadge(w.level)}</td>`
      + `<td style="padding:6px 8px;font-weight:600;">${Number(w.count || 0)}</td>`
      + `<td style="padding:6px 8px;line-height:1.6;">`
      + (w.impact ? `<div>${esc(w.impact)}</div>` : '')
      + (w.last ? `<div class="mut" style="font-size:12px;margin-top:2px;">${esc(w.last)}</div>` : '')
      + `</td>`
      + `<td style="padding:6px 8px;" class="mut">${dgFmtTs(w.last_ts)}</td></tr>`;
  });
  h += '</tbody></table></div>';
  h += '<div class="mut" style="margin-top:8px;font-size:12px;line-height:1.7;">'
    + `共 ${Number(snap.where_n || 0)} 个降级点 / ${total} 次。`
    + '计数为<b>进程内累计</b>（重启清零）；点开任意一条回答的执行过程，可看到该轮具体降级。'
    + '</div>';
  return h;
}

async function loadDegradePanel(){
  const box = document.getElementById('dg-panel');
  if(!box) return;
  let snap = null, err = '';
  try{
    snap = await api('/api/health/degrade?limit=40');
  }catch(e){
    err = (e && e.message) ? e.message : String(e || '未知错误');
  }
  _dgCache = snap;
  if(!snap){
    // ★ 不可用 ≠ 0 条：必须显示成"台账不可用"，否则正好复刻了本面板要消灭的欺骗
    box.innerHTML = '<div class="panel" style="margin-bottom:14px;">'
      + '<div class="ph">静默兜底台账 <span class="badge">P0 让错误可见</span>'
      + '<span class="st r" style="margin-left:auto;">台账不可用</span></div>'
      + '<div class="pb"><div style="padding:10px;font-size:13.5px;color:var(--red);">'
      + '读取 /api/health/degrade 失败：' + esc(String(err).slice(0, 300))
      + '<div class="mut" style="margin-top:6px;">这不是"零降级" —— 是读不到。'
      + '请检查服务是否正常运行。</div></div></div></div>';
    return;
  }
  const total = Number(snap.total || 0);
  const by = snap.by_level || {};
  const headBadge = Number(by.critical || 0) > 0
    ? `<span class="badge r">${by.critical} 处结果不可信</span>`
    : (total ? `<span class="badge w">${total} 次降级</span>` : '<span class="badge g">0 降级</span>');
  box.innerHTML = '<div class="panel" style="margin-bottom:14px;">'
    + '<div class="ph">静默兜底台账 <span class="badge">P0 让错误可见</span>'
    + '<span class="badge">进程内累计</span>' + headBadge
    + '<span style="flex:1;"></span>'
    + '<button class="btn sm" onclick="loadDegradePanel()">刷新</button></div>'
    + '<div class="pb">' + dgRender(snap) + '</div></div>';
}

// 页面切到「统一监控」时由 loadOps 调用；此处只做一次轻量兜底绑定，
// 防止有人在别的入口直接调 loadDegradePanel 时容器还没渲染。
document.addEventListener('DOMContentLoaded', function(){
  try{
    if(document.getElementById('dg-panel') && !document.getElementById('dg-panel').dataset.loaded){
      document.getElementById('dg-panel').dataset.loaded = '1';
    }
  }catch(e){}
});
