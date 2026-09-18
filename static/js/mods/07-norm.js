/* 归一化：归一确认卡 / 闸口 / 推送
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 2856-3568  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
const _NR_TABS = [
  {k:'entity',   l:'实体'},
  {k:'attr',     l:'属性'},
  {k:'relation', l:'关系'},
  {k:'cons',     l:'一致性'},
];
window._nrState = window._nrState || {
  report: null, decisions: {}, tab: 'entity',
  onlyPending: false, writeGlossary: false, applied: null, msgId: null, convId: null,
  evRow: null,      // 展开证据的行 idx
  v2Open: false,    // V2 代码预览区
  v2Focus: 0,       // 高亮行
  confirming: false,// 提交二次确认（卡内 inline，不跳面板）
  applying: null,   // 'ai' = AI 续写中
  newCode: '',      // 应用后的 V2 代码
  collapsed: false, // 归一确认区收起态（卡头点击切换；决策保留）
  fullscreen: false,// 2026-09-17：全屏查看态（卡头「⛶ 全屏」，Esc 恢复）
  _nrFullAnchor: null, // 全屏时宿主被搬到 <body>，这里记原父节点与后继兄弟，退出时原位归还
  hostId: '',
};

/* ── 入口：在对话消息内展开归一确认卡（人在回路，不跳独立页） ── */
function openNormReport(btn) {
  const card = btn && btn.closest ? btn.closest('.sv-card') : null;
  const msg = (card && card.closest) ? card.closest('.msg[data-mid]') : null;
  if(!msg){ toast('未找到消息容器'); return; }
  const mid = msg.getAttribute('data-mid');
  if(!mid){ toast('未找到消息 ID'); return; }
  if(!currentConvId){ toast('缺少当前会话 ID'); return; }
  const hostId = 'nr-inline-' + mid;
  let host = document.getElementById(hostId);
  if(!host){
    host = document.createElement('div');
    host.id = hostId;
    host.className = 'nr-inline';
    // .msg 是横向 flex（who + msg-inner），确认卡必须挂进消息正文容器才会在消息下方展开
    const bodyEl = msg.querySelector('.body') || msg.querySelector('.msg-inner') || msg;
    bodyEl.appendChild(host);
  }
  if(window._nrState.hostId === hostId && window._nrState.report && window._nrState.msgId === mid){
    // 已展开 → 切换展开/收起（不销毁状态与决策；不再提供关闭入口）
    window._nrState.collapsed = !window._nrState.collapsed;
    _nrRender();
    return;
  }
  window._nrState.hostId = hostId;
  host.innerHTML = `<div style="padding:22px;text-align:center;color:var(--mut);">
    <div style="font-size:13px;">⏳ 正在生成归一确认清单…</div>
    <div style="font-size:11px;margin-top:6px;">实例 → 本体类型对齐 · 词典归一 · 同形消歧 · 证据与置信度计算</div></div>`;
  api(`/api/conversations/${currentConvId}/messages/${mid}/normalize-report`, {method:'POST'})
    .then(r => {
      if(!r || r.error){ throw new Error((r && r.error) || '归一清单生成失败'); }
      window._nrState.report = r;
      window._nrState.decisions = {};
      window._nrState.tab = 'entity';
      window._nrState.onlyPending = false;
      window._nrState.writeGlossary = false;
      window._nrState.applied = null;
      window._nrState.newCode = '';
      window._nrState.evRow = null;
      window._nrState.v2Open = false;
      window._nrState.confirming = false;
      window._nrState.msgId = mid;
      window._nrState.convId = currentConvId;
      _nrRender();
      setTimeout(()=>{ try{ host.scrollIntoView({behavior:'smooth', block:'nearest'}); }catch(e){} }, 60);
    })
    .catch(e => {
      host.innerHTML = `<div style="padding:16px;color:var(--red);font-size:12px;">✕ 生成失败：${esc(e.message || String(e))}</div>`;
    });
}
function _nrHost() {
  return window._nrState.hostId ? document.getElementById(window._nrState.hostId) : null;
}
/* ── 数据辅助 ── */
function _nrAllRows() {
  const rows = (window._nrState.report && window._nrState.report.rows) || [];
  rows.forEach((r, i) => { r._i = i; });
  return rows;
}
function _nrIsBlocking(r) { return _NR_BLOCKING.indexOf(r.matching_status) >= 0; }
function _nrTabRows(k) { return _nrAllRows().filter(r => (r.kind || 'entity') === k); }
function _nrPendingTotal() {
  return _nrAllRows().filter(r => _nrIsBlocking(r) && !window._nrState.decisions[r._i]).length;
}
function _nrVisibleRows() {
  const k = window._nrState.tab;
  let rs = _nrTabRows(k);
  if(window._nrState.onlyPending) rs = rs.filter(r => _nrIsBlocking(r) && !window._nrState.decisions[r._i]);
  return rs;
}
function _nrSuggestOf(r) {
  if(r.kind === 'attr' && r.generic_key) return _NR_SUGGEST.generic;
  return _NR_SUGGEST[r.matching_status] || _NR_SUGGEST.none;
}
function _nrOntoKindLabel(k) {
  return {entity:'类', attribute:'属性', relation:'关系'}[k] || k || '';
}
/* ── 主渲染 ── */
function _renderNormReport() {
  const host = _nrHost();
  if(!host) return;
  const st = window._nrState, r = st.report || {};
  if(!st.sel) st.sel = new Set();
  const s = r.summary || {}, c = r.consistency || {};
  const all = _nrAllRows();
  const cnt = {entity:0, attr:0, relation:0};
  all.forEach(x => { cnt[x.kind] = (cnt[x.kind] || 0) + 1; });
  const needDecide = all.filter(_nrIsBlocking).length;
  const pending = _nrPendingTotal();

  let h = `<div style="margin-top:9px;border:1px solid var(--line);border-radius:9px;background:var(--card);overflow:hidden;">`;
  const collapsed = !!st.collapsed;
  // ① 卡头（点击整行 = 展开/收起；不提供关闭入口，决策保留）
  h += `<div style="display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--line);background:var(--bg);font-size:12.5px;cursor:pointer;user-select:none;"
      onclick="_nrToggleCollapse()" title="${collapsed?'展开':'收起'}归一确认区（决策保留）">
      <span style="font-weight:600;">🧹 归一确认（人在回路）</span>
      <span style="font-size:10.5px;padding:1px 8px;border-radius:9px;${pending?'background:#fff5f5;border:1px solid #F3C1C1;color:#b91c1c;':'background:#f7fbee;border:1px solid #C0DD97;color:#2f855a;'}">${pending?`待确认 ${pending} 项`:'已全部确认'}</span>
      <span style="font-size:10.5px;padding:1px 8px;border-radius:9px;background:#f3f4f6;border:1px solid #d1d5db;color:#6b7280;">实体 ${cnt.entity||0} · 属性 ${cnt.attr||0} · 关系 ${cnt.relation||0}</span>
      <span style="flex:1"></span>
      <span style="font-size:10.5px;color:var(--mut);">决策暂存于本页，刷新后清空</span>
      <button class="btn sm ghost" onclick="event.stopPropagation();_nrToggleFullscreen()"
        title="${st.fullscreen?'恢复为消息内嵌视图（Esc）':'全屏查看归一确认清单，便于逐条裁决与 V2 对照（Esc 恢复）'}"
        style="flex:none;padding:2px 9px;font-size:11px;">${st.fullscreen?'⤡ 恢复':'⛶ 全屏'}</button>
      <span style="color:var(--mut);font-size:11px;flex:none;">${collapsed?'▸ 展开':'▾ 收起'}</span>
    </div>`;
  // ②~⑤ 主体（收起时仅隐藏，状态与决策保留）
  h += `<div style="display:${collapsed?'none':''};">`;
  // ② 筛选区（tab + 只看未决；字段说明与 V2 入口已按反馈移除）
  h += `<div style="display:flex;gap:4px;align-items:center;padding:0 12px;border-bottom:1px solid var(--line);">`;
  h += _NR_TABS.map(t => {
    const n = t.k === 'cons' ? '' : (cnt[t.k] || 0);
    const p = t.k === 'cons' ? 0 : _nrTabRows(t.k).filter(x => _nrIsBlocking(x) && !st.decisions[x._i]).length;
    return `<div onclick="_nrTab('${t.k}')" style="padding:6px 12px;font-size:12px;cursor:pointer;border-bottom:2px solid ${st.tab===t.k?'var(--blue-d,#1d4ed8)':'transparent'};color:${st.tab===t.k?'var(--blue-d,#1d4ed8)':'var(--mut)'};font-weight:${st.tab===t.k?'600':'400'};">
      ${t.l}${n!==''?` <span style="font-size:10.5px;color:var(--mut);">${n}</span>`:''}${p?` <span style="font-size:10px;color:#b91c1c;font-weight:700;">·${p}待决</span>`:''}</div>`;
  }).join('');
  h += `<label style="margin-left:auto;font-size:11.5px;cursor:pointer;user-select:none;white-space:nowrap;padding:0 2px;">
          <input type="checkbox" ${st.onlyPending?'checked':''} onchange="_nrTogglePending(this.checked)"
            style="vertical-align:-1px;"> 只看未决（${pending}）</label>`;
  h += `</div>`;
  if(st.tab === 'cons'){ h += _nrConsHtml(s, c); }
  else { h += _nrTableHtml(); }
  // ④ 提交区
  h += _nrFooterHtml(pending, needDecide);
  // ⑤ V2 代码预览区
  h += _nrV2PaneHtml();
  h += `</div>`;
  h += `</div>`;
  host.innerHTML = h;
  _nrSyncV2Pane();
  _nrUpdateSel();
}
/* 展开/收起归一确认区主体（卡头点击；状态保留，不销毁决策） */
function _nrToggleCollapse() {
  window._nrState.collapsed = !window._nrState.collapsed;
  _nrRender();
}
/* 2026-09-17 新增：归一确认区「全屏查看 / 恢复」
   动机：归一确认是逐条裁决作业（实体/属性/关系/一致性四类 + 批量勾选 + V2 代码对照），
   内嵌在消息流里可用高度只有几百 px（#nr-rows 写死 340px），大清单要反复滚动、对照困难。
   实现要点（实测踩坑）：消息节点 `.msg` 带 content-visibility（→ 隐式 contain），**会成为 fixed 的包含块**，
   只靠 CSS 的 position:fixed 无法真正铺满视口（实测覆盖层被限制在消息框内）。
   因此全屏时把宿主搬到 <body> 直下（记锚点，退出时原位归还），DOM 位置对渲染无影响；
   决策状态（window._nrState.decisions）与 _nrRender 重绘全程不受影响。 */
function _nrToggleFullscreen(force) {
  const st = window._nrState;
  const host = _nrHost();
  if(!host) return;
  const next = (typeof force === 'boolean') ? force : !st.fullscreen;
  st.fullscreen = next;
  if(next){
    st._nrFullAnchor = { parent: host.parentNode, next: host.nextSibling };  // 退出时的原位锚点
    document.body.appendChild(host);       // 逃出 .msg 的包含块
    if(st.collapsed) st.collapsed = false; // 全屏时必定展开主体
  } else if(st._nrFullAnchor){
    const a = st._nrFullAnchor;
    if(a.parent && a.parent.isConnected){
      if(a.next && a.next.parentNode === a.parent) a.parent.insertBefore(host, a.next);
      else a.parent.appendChild(host);
    } else {
      host.remove();   // 原消息已被重渲染（如切换会话）→ 宿主无归处，直接摘除
    }
    st._nrFullAnchor = null;
  }
  host.classList.toggle('nr-full', next);
  document.body.classList.toggle('nr-full-lock', next);
  _nrRender();
  if(next){ try{ host.scrollIntoView({block:'start'}); }catch(e){} }
}
// Esc 恢复（capture 阶段，不影响其它 Esc 处理器：如命令面板关闭）
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape' && window._nrState && window._nrState.fullscreen){ _nrToggleFullscreen(false); }
}, true);
/* ── 表格：本体类型 / 实例名称(本次生成) / 比对目标(词典/图库) / 建议类型 / 置信度 / V2代码 / 证据 / 操作 ──
   v7：实例名称与比对目标拆为并排两列（行业惯例：OpenRefine reconciliation / MDM 黄金记录评审的
   side-by-side 对照范式），差异字符高亮 + 状态色徽标，让「改到哪/合到哪」一眼可判 */
/* v9：V2代码列移除（入口保留在应用回执）；证据列内联直显（完整内容 hover tooltip） */
const _NR_COLS = '26px repeat(6, minmax(96px,1fr)) 124px';
const _NR_TBL_MIN = 760;   // 表格最小宽度：低于此值横向滚动，避免列被挤压变形
function _nrTableHtml() {
  const rs = _nrVisibleRows();
  // 批量操作入口常驻表格上方（勾选后按钮解禁），不再藏在列表底部
  let h = `<div id="nr-sel-bar" style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;
      margin-bottom:5px;padding:6px 9px;background:#f8fafc;border:1px solid var(--line);border-radius:6px;font-size:11.5px;">
      <label style="cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px;">
        <input type="checkbox" id="nr-cb-all" onchange="_nrToggleAll(this)"
          style="cursor:pointer;vertical-align:-1px;" title="全选/取消本页可见行"> 全选本页</label>
      <span style="color:var(--line);">|</span>
      <span>已选 <b id="nr-sel-count" style="color:#1d4ed8;">0</b> 项</span>
      <span class="nr-bulk-wrap" style="display:none;align-items:center;gap:7px;">
        <span style="color:var(--line);">|</span>
        <button class="nr-bulk" data-b="accept" onclick="_nrSelAction('accept')"
          style="padding:2px 9px;font-size:11px;border-radius:5px;border:1px solid #1e7e34;background:#1e7e34;color:#fff;cursor:pointer;">✓ 接纳</button>
        <button class="nr-bulk" data-b="rename" onclick="_nrSelAction('rename')"
          style="padding:2px 9px;font-size:11px;border-radius:5px;border:1px solid #c98a2e;background:#c98a2e;color:#fff;cursor:pointer;">✎ 改为规范名</button>
        <button class="nr-bulk" data-b="merge" onclick="_nrSelAction('merge')"
          style="padding:2px 9px;font-size:11px;border-radius:5px;border:1px solid #7c3aed;background:#7c3aed;color:#fff;cursor:pointer;">🔀 合并</button>
        <button class="nr-bulk" data-b="clear" onclick="_nrSelClear()"
          style="padding:2px 9px;font-size:11px;border-radius:5px;border:1px solid var(--line);background:#fff;color:var(--mut);cursor:pointer;">⏮ 撤销所选决策</button>
      </span>
      <span style="flex:1"></span>
      <span id="nr-sel-tip" style="font-size:10.5px;color:var(--mut);">勾选左侧复选框后可批量操作</span>
    </div>
    <div style="overflow-x:auto;border:1px solid var(--line);border-radius:6px;">`;
  h += `<div style="display:grid;grid-template-columns:${_NR_COLS};gap:6px;align-items:center;min-width:${_NR_TBL_MIN}px;
      padding:6px 8px;background:var(--bg);border-bottom:1px solid var(--line);font-size:11px;color:var(--mut);font-weight:600;">
      <span></span><span>本体类型</span><span title="本次生成的原始数据名称">实例名称（本次生成）</span><span title="词典/图库中已存在的对齐对象">比对目标（词典/图库）</span><span>建议类型</span><span>置信度</span>
      <span>证据</span><span>操作</span>
    </div>
    <div id="nr-rows" style="max-height:340px;overflow-y:auto;">`;
  h += rs.length ? rs.map(r => _nrRowHtml(r)).join('')
    : `<div style="padding:22px;text-align:center;color:var(--mut);font-size:12px;">无匹配行</div>`;
  h += `</div></div>`;
  return h;
}
/* 比对目标列（独立列，与实例名称并排对照）：借鉴 OpenRefine 可见 diff + MDM 黄金记录评审范式。
   数据源：matched_entity_name(图库实体/词典词条) · normalized_name(词典规范名)
         · batch_dup_of(批内近似) · matched_entity_id(关系行=已有关系id) */
/* 字符级差异高亮：公共前后缀保留原样，差异段加琥珀底（借鉴 OpenRefine 可见 diff） */
function _nrDiffHtml(src, tgt) {
  src = String(src || ''); tgt = String(tgt || '');
  let a = 0, e = src.length, f = tgt.length;
  while (a < e && a < f && src[a] === tgt[a]) a++;
  while (e > a && f > a && src[e - 1] === tgt[f - 1]) { e--; f--; }
  return esc(tgt.slice(0, a)) + '<span style="background:#FDE9C8;border-radius:2px;padding:0 1px;" title="与本次名称的差异部分">' + esc(tgt.slice(a, f)) + '</span>' + esc(tgt.slice(f));
}
function _nrTargetCellHtml(r) {
  const men = r.matched_entity_name || '';
  const nn = r.normalized_name || '';
  const bd = r.batch_dup_of || '';
  let tag = '', name = '';
  if (men) { tag = r.kind === 'attr' ? '词典词条' : '图库已有'; name = men; }
  else if (nn && nn !== (r.name || '') && r.kind !== 'relation') { tag = '词典规范名'; name = nn; }
  if (bd && !name) { tag = '批内近似'; name = bd; }
  if (r.kind === 'relation' && !name && r.matched_entity_id) { tag = '图库已有关系'; name = '#' + r.matched_entity_id; }
  if (!name) return `<div style="min-width:0;font-size:10.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.45;"
    title="词典/图库无对齐对象">—</div>`;
  const stt = r.matching_status;
  const isDup = stt === 'dup_high' || stt === 'dup_suspect';
  const c = isDup ? (stt === 'dup_high' ? '#b91c1c' : '#c98a2e') : '#1e7e34';
  const bg = isDup ? (stt === 'dup_high' ? '#fef2f2' : '#fffbeb') : '#f0fdf4';
  const bdc = isDup ? (stt === 'dup_high' ? '#F3C1C1' : '#F0D9A6') : '#C0DD97';
  // 名称对照：实体/属性做字符级 diff，关系目标(端点串)不做
  const cmpSrc = r.kind === 'attr' ? (r.attr_name || r.instance_name || '') : (r.instance_name || '');
  const nameHtml = (r.kind === 'relation') ? esc(name) : _nrDiffHtml(cmpSrc, name);
  return `<div style="min-width:0;line-height:1.45;" title="比对目标（${tag}）——即改名/合并的去向">
    <div style="display:inline-flex;align-items:center;gap:4px;max-width:100%;background:${bg};border:1px solid ${bdc};border-radius:4px;padding:1px 6px;">
      <span style="flex:none;font-size:9.5px;color:${c};font-weight:600;">${tag}</span>
      <span style="min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;color:${c};">${nameHtml}</span>
    </div></div>`;
}
/* 实例名称按类型呈现（对齐 v1.3 原型样式）：
   实体=实例名（改名决策后：原名删除线 → 新名）；
   属性=所属实体徽标+属性名+属性值（单行）；
   关系=源端—[类型]→目标端（箭头琥珀色带方括号） */
function _nrInstanceHtml(r) {
  const d = (window._nrState.decisions || {})[r._i];
  if(r.kind === 'attr'){
    const ent = r.attr_entity || r.parent || '';
    const nm = r.attr_name || r.instance_name || r.name || '';
    const val = r.attr_value || '';
    return `<div style="min-width:0;line-height:1.45;">
      <div style="display:flex;gap:4px;align-items:baseline;min-width:0;">
        <span style="flex:none;font-size:9.5px;color:#0C447C;background:#E6F1FB;border:1px solid #B5D4F4;
          padding:0 5px;border-radius:3px;" title="所属实体">${esc(ent||'—')}</span>
        <span style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
          title="属性名：${esc(nm)}">${esc(nm)}</span>
        <span style="flex:none;font-size:10.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px;"
          title="属性值：${esc(val||'—')}">${val?('= '+esc(val)):'—'}</span>
      </div></div>`;
  }
  if(r.kind === 'relation'){
    return `<div style="min-width:0;line-height:1.45;">
      <div style="display:flex;gap:4px;align-items:baseline;min-width:0;">
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;"
          title="源端：${esc(r.rel_source||'')}">${esc(r.rel_source||'—')}</span>
        <span style="flex:none;font-size:10.5px;color:#c98a2e;font-weight:600;white-space:nowrap;"
          title="关系类型：${esc(r.rel_type||'')}">—[${esc(r.rel_type||'?')}]→</span>
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;"
          title="目标端：${esc(r.rel_target||'')}">${esc(r.rel_target||'—')}</span>
      </div></div>`;
  }
  // 实体：改名决策后 原名(删除线) → 新名（绿色），直观呈现改名去向
  if(d && d.action === 'rename' && d.new_name && d.new_name !== r.instance_name){
    return `<div style="min-width:0;line-height:1.45;" title="改名：${esc(r.instance_name||'')} → ${esc(d.new_name)}">
      <s style="color:var(--mut);font-weight:400;">${esc(r.instance_name||'')}</s>
      <span style="color:#1e7e34;font-weight:600;">→ ${esc(d.new_name)}</span></div>`;
  }
  return `<div style="min-width:0;line-height:1.45;">
    <div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
      title="实例名称：${esc(r.instance_name||'')}">${esc(r.instance_name||'')}</div></div>`;
}
function _nrRowHtml(r) {
  const i = r._i, st = window._nrState;
  const d = st.decisions[i];
  const decBg = d ? (d.action==='accept'?'#f0fdf4':d.action==='reject'?'#fef2f2':'#fffbeb') : 'transparent';
  const decLbl = !d ? '' : d.action==='accept' ? '✓ 接纳' : d.action==='reject' ? '✕ 不入图库'
    : d.action==='rename' ? '✎ 已改' : '🔀 已合并';
  const conf = r.confidence != null ? Number(r.confidence).toFixed(2) : '—';
  const confColor = conf==='—' ? 'var(--mut)' : (Number(conf)>=.85?'#1e7e34':Number(conf)>=.7?'#c98a2e':'#b91c1c');
  const sg = _nrSuggestOf(r);
  const ev = r.evidence || {};
  const evSrcs = ev.sources || [];
  const evFirst = evSrcs[0] || (ev.rule ? '规则：' + ev.rule : '');
  const evTitle = [ev.rule ? '判定规则：' + ev.rule : ''].concat(evSrcs,
    ev.context ? ['上下文：' + ev.context] : []).filter(Boolean).join('\n');
  const ontoDisp = (r.kind === 'attr' && r.attr_onto_display) ? r.attr_onto_display : (r.ontology_type || '—');
  const ontoOk = r.ontology_matched;
  let h = `<div style="display:grid;grid-template-columns:${_NR_COLS};gap:6px;align-items:center;min-width:${_NR_TBL_MIN}px;
      padding:4px 8px;border-top:1px solid var(--line);font-size:11.5px;background:${decBg};">
    <input type="checkbox" class="nr-cb" value="${i}" onchange="_nrUpdateSel()" style="cursor:pointer;">
    <div style="min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.45;"
      title="本体类型：${esc(ontoDisp)}（${esc(_nrOntoKindLabel(r.ontology_type_kind))}）${ontoOk?'':' · 类型名未在本体模型中命中（实例名与词典/图库的对齐不受影响）'}">
      <span style="font-weight:600;color:${ontoOk?'var(--ink)':'var(--mut)'};">${esc(ontoDisp)}</span></div>
    ${_nrInstanceHtml(r)}
    ${_nrTargetCellHtml(r)}
    <span><span style="font-size:10.5px;padding:1px 6px;border-radius:9px;border:1px solid ${sg.bd};background:${sg.bg};color:${sg.c};">${sg.l}</span></span>
    <span style="font-size:11px;color:${confColor};">${conf}</span>
    <div title="${esc(evTitle)}" style="min-width:0;font-size:10.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.45;${(r.attr_type_check==='mismatch')?'color:#b91c1c;':''}">${evFirst ? esc(evFirst) : '—'}</div>
    <div style="display:flex;gap:3px;align-items:center;white-space:nowrap;">
      <button style="padding:1px 6px;font-size:10.5px;border:1px solid #1e7e34;color:#fff;background:#1e7e34;border-radius:4px;cursor:pointer;"
        onclick="_nrDecide(${i},'accept')" title="接纳：按 AI 建议处理">✓</button>
      <button style="padding:1px 6px;font-size:10.5px;border:1px solid #c98a2e;color:#fff;background:#c98a2e;border-radius:4px;cursor:pointer;"
        onclick="_nrRename(${i})" title="修改：改为规范名 / 手工改名">✎</button>
      <button style="padding:1px 6px;font-size:10.5px;border:1px solid #7c3aed;color:#fff;background:#7c3aed;border-radius:4px;cursor:pointer;"
        onclick="_nrMerge(${i})" title="合并：合并到已有元素">🔀</button>
      <span style="font-size:9.5px;color:${d?'#1e7e34':'var(--mut)'};">${decLbl}</span>
    </div>
  </div>`;
  return h;
}
/* ── 一致性 tab（只读） ── */
function _nrConsHtml(s, c) {
  const bits = [];
  if((c.batch_dups||[]).length) bits.push(`批内疑似重复 <b style="color:#d97706;">${c.batch_dups.length}</b> 对`);
  if((c.dangling||[]).length) bits.push(`悬空引用 <b style="color:#b91c1c;">${c.dangling.length}</b> 处`);
  if(s.props_total) bits.push(`属性键 <b>${s.props_total}</b>（已入表 ${s.attrs||0} 行，命中词典 ${s.attrs_matched||0}）`);
  if(s.props_failed) bits.push(`属性校验失败 <b style="color:#b91c1c;">${s.props_failed}</b>`);
  if(s.cross_view_dups) bits.push(`跨视图重复 <b>${s.cross_view_dups}</b> 个（已合并为一行）`);
  let h = `<div style="padding:10px;border:1px dashed var(--line);border-radius:6px;font-size:11.5px;line-height:1.9;">
    🔍 <b>一致性</b>：${bits.length?bits.join(' · '):'无异常'}`;
  if((c.dangling||[]).length){
    h += `<div style="margin-top:6px;color:var(--mut);">悬空：${c.dangling.slice(0,5).map(d=>`${esc(d[0])} --${esc(d[2])}→ ${esc(d[1])}`).join('；')}${(c.dangling||[]).length>5?'…':''}</div>`;
  }
  if((c.batch_dups||[]).length){
    h += `<div style="margin-top:4px;color:var(--mut);">疑似重复：${c.batch_dups.slice(0,5).map(d=>`${esc(d[0])} ≈ ${esc(d[1])}`).join('；')}</div>`;
  }
  h += `</div><div style="margin-top:8px;font-size:11px;color:var(--mut);line-height:1.8;">
    说明：结构字段（stereotype / ports / value 等）标记为「忽略」，不进词库、不需拍板。</div>`;
  return h;
}
/* ── 提交区（inline 二次确认，不跳面板） ── */
function _nrFooterHtml(pending, needDecide) {
  const st = window._nrState;
  if(st.applying) return `<div style="margin-top:12px;padding:16px;text-align:center;color:var(--mut);font-size:12.5px;">
      <div>🤖 AI 正在按确认结果更新 V2 代码…</div>
      <div style="font-size:11px;margin-top:5px;">改写元素名称 · 重渲染视图 · 直接更新原代码文件与视图（不新建版本）</div></div>`;
  if(st.applied && st.applied.no_change){
    return `<div style="margin-top:12px;padding:11px 13px;border:1px solid #93C5FD;background:#eff6ff;border-radius:8px;font-size:12px;line-height:1.9;color:#1e40af;">
      ℹ️ ${esc(st.applied.message || '本次确认未产生名称改写，V2 代码无需更新')}
      <div style="margin-top:6px;color:var(--mut);">建议类型若为「新增 / 忽略 / 已对齐」，本就不需要改写代码。可继续发起新一轮建模。</div>
      <div style="margin-top:8px;"><button class="btn ghost" onclick="window._nrState.applied=null;_nrRender()">返回修改</button></div></div>`;
  }
  if(st.applied) return _nrAppliedHtml();
  if(st.confirming) {
    const dec = st.decisions, keys = Object.keys(dec);
    const ch = {renamed:0, merged:0, accepted:0, rejected:0};
    keys.forEach(k => {
      const a = dec[k].action;
      if(a === 'rename') ch.renamed++; else if(a === 'merge') ch.merged++;
      else if(a === 'reject') ch.rejected++; else ch.accepted++;
    });
    const gl = !!st.writeGlossary;
    return `<div style="margin-top:12px;padding:11px 13px;border:1px solid #93c5fd;background:#eff6ff;border-radius:8px;font-size:12px;line-height:1.95;">
      <b>确认应用归一决策？</b><br>
      改名 <b>${ch.renamed}</b> · 合并 <b>${ch.merged}</b> · 接纳 <b>${ch.accepted}</b> · 不入图库 <b>${ch.rejected}</b><br>
      同步写入词库：<b style="color:${gl?'#1e7e34':'#6b7280'};">${gl?'是（原名→规范名写入概念层）':'否（仅本次生效）'}</b>　
      提交后由 <b>AI 续写 V2 代码</b>，<b>直接更新原来的代码文件与视图</b>（不新建版本）。<br>
      <span style="color:var(--mut);">此步骤只改本地草稿，不会写入建模软件。</span>
      <div style="margin-top:9px;display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn ghost" onclick="window._nrState.confirming=false;_nrRender()">返回修改</button>
        <button class="btn primary" onclick="_nrApply()">确认更新 V2</button>
      </div></div>`;
  }
  const canSubmit = pending === 0 && needDecide > 0;
  return `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line);">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <label style="font-size:11.5px;cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:4px;line-height:24px;" title="勾选后把「原名 → 规范名」写入概念层词库；不勾选则仅本次生效">
          <input type="checkbox" id="nr-glossary" ${st.writeGlossary?'checked':''}
            onchange="window._nrState.writeGlossary=this.checked"> 同步写入词库</label>
        <span style="font-size:10.5px;color:var(--mut);">默认不选：仅本次生效</span>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:var(--mut);">已决策 ${Object.keys(st.decisions).length} 项</span>
        <button class="btn ghost" onclick="_nrExport()" style="padding:2px 10px;font-size:11px;line-height:18px;">📤 导出决策</button>
        <button class="btn primary" ${canSubmit?'':'disabled'}
          style="padding:3px 12px;font-size:11.5px;line-height:18px;${canSubmit?'':'opacity:.5;cursor:not-allowed;'}"
          onclick="window._nrState.confirming=true;_nrRender()">✓ 提交并更新 V2 代码</button>
      </div>
      ${needDecide>0&&pending>0?`<div style="margin-top:4px;font-size:11px;color:#b91c1c;">⛔ 还有 ${pending} 项未确认，全部拍板后才能提交。</div>`:''}
      ${needDecide===0?`<div style="margin-top:4px;font-size:11px;color:#1e7e34;">✅ 本次归一无需人工确认，可直接提交。</div>`:''}
    </div>`;
}
/* ── V2 代码预览区（点击行内「📄 Lxx」在此查看） ── */
function _nrV2PaneHtml() {
  const st = window._nrState;
  if(!st.v2Open) return '';
  return `<div id="nr-v2pane" style="margin-top:10px;border:1px solid var(--line);border-radius:8px;overflow:hidden;">
      <div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--bg);border-bottom:1px solid var(--line);font-size:11.5px;">
        <b>📄 V2 代码预览</b>
        <span id="nr-v2tip" style="color:var(--mut);"></span>
        <span style="flex:1"></span>
        <a href="javascript:;" onclick="_nrCloseV2()" style="color:var(--mut);">收起 ✕</a>
      </div>
      <div id="nr-v2code" style="max-height:280px;overflow:auto;background:#0f172a;position:relative;"></div>
    </div>`;
}
function _nrOpenV2(i) {
  const st = window._nrState;
  st.v2Open = true;
  const rows = _nrAllRows();
  const r = rows[i];
  st.v2Focus = (r && r.v2_ref && r.v2_ref.found) ? r.v2_ref.line : 0;
  if(!document.getElementById('nr-v2pane')){
    _nrRender();
  } else {
    _nrSyncV2Pane();
  }
}
function _nrCloseV2() {
  window._nrState.v2Open = false;
  _nrRender();
}
function _nrSyncV2Pane() {
  const st = window._nrState;
  const box = document.getElementById('nr-v2code');
  const tip = document.getElementById('nr-v2tip');
  if(!box) return;
  const code = st.newCode || (st.report && st.report.code_text) || '';
  const isNew = !!st.newCode;
  if(tip) tip.textContent = isNew ? '更新后的 V2 代码' : '本次生成的 V2 代码';
  if(!code){ box.innerHTML = `<div style="padding:16px;color:#94a3b8;font-size:12px;">无可预览的 V2 代码</div>`; return; }
  const lines = String(code).split('\n');
  box.innerHTML = lines.map((ln, n) => {
    const on = (n + 1) === st.v2Focus;
    return `<div id="nrl-${n+1}" ${on?'data-hl="1"':''} style="display:flex;font-family:ui-monospace,Consolas,monospace;font-size:11px;line-height:1.65;background:${on?'#3b2f0b':'transparent'};">
      <span style="flex:none;width:46px;text-align:right;padding-right:8px;color:#64748b;user-select:none;">${n+1}</span>
      <span style="flex:1;white-space:pre-wrap;word-break:break-all;color:${on?'#fbbf24':'#e2e8f0'};">${esc(ln)}</span></div>`;
  }).join('');
  if(st.v2Focus){
    const el = document.getElementById('nrl-' + st.v2Focus);
    if(el && el.scrollIntoView) el.scrollIntoView({block:'center'});
  }
}
/* ── 交互 ── */
function _nrTab(k) { window._nrState.tab = k; _nrRender(); }
function _nrTogglePending(v) { window._nrState.onlyPending = !!v; _nrRender(); }
function _nrDecide(i, action) {
  const rows = _nrAllRows(), r = rows[i];
  if(!r) return;
  const d = {action, ts: Date.now(), row_name: r.name, row_status: r.matching_status};
  if(action === 'rename') d.new_name = r.matched_entity_name || r.normalized_name || r.name;
  if(action === 'merge') d.merge_to = r.matched_entity_name || r.matched_entity_id || r.name;
  window._nrState.decisions[i] = d;
  _nrRender();
}
function _nrRename(i) {
  const rows = _nrAllRows(), r = rows[i]; if(!r) return;
  const cur = r.matched_entity_name || r.normalized_name || r.name;
  const v = prompt(`修改名称（实例：${r.instance_name || r.name}）`, cur);
  if(v === null) return;
  if(!v.trim() || v.trim() === r.name){ _nrDecide(i, 'accept'); return; }
  window._nrState.decisions[i] = {action:'rename', new_name:v.trim(), ts:Date.now(),
    row_name:r.name, row_status:r.matching_status};
  _nrRender();
}
function _nrMerge(i) {
  const rows = _nrAllRows(), r = rows[i]; if(!r) return;
  const cand = rows.filter(o => o.kind==='entity' && o.name === r.name && o._i !== i);
  const v = prompt(`合并到（同名元素：${cand.length ? cand.map(c=>c.name).join(' / ') : '无同名，填目标元素名'}）`,
    r.matched_entity_name || r.name);
  if(v === null) return;
  window._nrState.decisions[i] = {action:'merge', merge_to:v.trim(), ts:Date.now(),
    row_name:r.name, row_status:r.matching_status};
  _nrRender();
}
function _nrSelected() {
  const st = window._nrState;
  return st.sel ? Array.from(st.sel) : [];
}
function _nrUpdateSel() {
  const cnt = document.getElementById('nr-sel-count');
  const tip = document.getElementById('nr-sel-tip');
  const host = _nrHost();
  const cbs = host ? Array.from(host.querySelectorAll('.nr-cb')) : [];
  const n = cbs.filter(c => c.checked).length;
  if(cnt) cnt.textContent = n;
  // 批量操作按钮组：未勾选时整体隐藏（勾选后出现），分隔线随组显隐
  document.querySelectorAll('#nr-sel-bar .nr-bulk-wrap').forEach(w => {
    w.style.display = n > 0 ? 'inline-flex' : 'none';
  });
  document.querySelectorAll('#nr-sel-bar .nr-bulk').forEach(b => {
    const isClear = b.getAttribute('data-b') === 'clear';
    if(n === 0){
      b.disabled = true;
      b.style.opacity = '.45';
      b.style.cursor = 'not-allowed';
    } else {
      b.disabled = false;
      b.style.opacity = '1';
      b.style.cursor = 'pointer';
    }
    if(!isClear && n === 0 && tip) tip.textContent = '勾选左侧复选框后可批量操作';
  });
  if(n > 0 && tip) tip.textContent = `可对所选 ${n} 项批量执行`;
  const all = document.getElementById('nr-cb-all');
  if(all) all.checked = cbs.length > 0 && n === cbs.length;
}
function _nrToggleAll(cb) {
  const host = _nrHost();
  const st = window._nrState;
  if(!st.sel) st.sel = new Set();
  if(!host) return;
  host.querySelectorAll('.nr-cb').forEach(c => {
    c.checked = cb.checked;
    const i = Number(c.value);
    if(cb.checked) st.sel.add(i); else st.sel.delete(i);
  });
  _nrUpdateSel();
}
function _nrSelClear() {
  const idxs = _nrSelected();
  if(!idxs.length){ toast('请先勾选行'); return; }
  idxs.forEach(i => { delete window._nrState.decisions[i]; });
  _nrRender();
  toast(`已撤销 ${idxs.length} 项决策`);
}
function _nrSelAction(action) {
  const idxs = _nrSelected();
  if(!idxs.length){ toast('请先勾选行'); return; }
  const rows = _nrAllRows();
  const st = window._nrState;
  idxs.forEach(i => {
    const r = rows[i]; if(!r) return;
    if(action === 'rename'){
      const canon = r.matched_entity_name || r.normalized_name;
      window._nrState.decisions[i] = (canon && canon !== r.name)
        ? {action:'rename', new_name:canon, ts:Date.now(), row_name:r.name, row_status:r.matching_status}
        : {action:'accept', ts:Date.now(), row_name:r.name, row_status:r.matching_status};
    } else if(action === 'merge'){
      const canon = rows.find(o => o._i !== i && o.name === r.name && o.matching_status !== 'none');
      window._nrState.decisions[i] = {action:'merge', merge_to:(canon ? (canon.matched_entity_name || canon.name) : r.name),
        ts:Date.now(), row_name:r.name, row_status:r.matching_status};
    } else {
      window._nrState.decisions[i] = {action, ts:Date.now(), row_name:r.name, row_status:r.matching_status};
    }
  });
  if(st.sel) st.sel.clear();
  _nrRender();
  toast(`已批量处理 ${idxs.length} 项`);
}
/* ── 渲染：保持滚动位置，避免任何操作后页面乱跳 ── */
function _nrRender() {
  const host = _nrHost();
  const before = host ? host.getBoundingClientRect().top : null;
  const sy = window.scrollY || window.pageYOffset || 0;
  const box = document.getElementById('nr-rows');
  const rowsTop = box ? box.scrollTop : 0;
  _renderNormReport();
  const after = _nrHost();
  if(host && after && before !== null){
    const delta = after.getBoundingClientRect().top - before;
    if(Math.abs(delta) > 1) window.scrollTo(0, Math.max(0, sy + delta));
  }
  const box2 = document.getElementById('nr-rows');
  if(box2 && rowsTop) box2.scrollTop = rowsTop;
}
function _nrApply() {
  const st = window._nrState;
  if(!st.report || !st.msgId || !st.convId) return;
  st.applying = 'ai';
  st.confirming = false;
  _nrRender();
  api(`/api/conversations/${st.convId}/messages/${st.msgId}/normalize-apply`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({decisions: st.decisions, write_glossary: !!st.writeGlossary, use_ai: true})
  }).then(res => {
    st.applying = null;
    if(!res || res.error){
      st.applyError = (res && res.error) || '未知错误';
      _nrRender();
      return;
    }
    st.applied = res;
    st.newCode = res.code_text || '';
    st.onlyPending = false;
    st.tab = 'entity';
    if(!res.no_change){ st.v2Open = true; st.v2Focus = 0; }
    _nrRender();
    if(res.no_change){
      toast('本次确认无需改写 V2 代码');
      return;
    }
    const rg = res.v2_regen || {};
    toast(rg.used_llm ? `AI 已续写 V2 代码 → ${res.version_label}` : `已更新至 ${res.version_label}（确定性替换）`);
    // 不自动刷新会话：否则本确认卡会被消息重渲染清掉，用户看不到回执与更新后代码
  }).catch(e => {
    st.applying = null;
    st.applyError = e.message || String(e);
    _nrRender();
  });
}
function _nrRefreshConv() {
  const st = window._nrState;
  try { if(typeof selectConv === 'function' && st.convId) selectConv(st.convId); } catch(e) {}
  toast('会话已刷新，代码/视图卡展示新版本');
}
function _nrAppliedHtml() {
  const st = window._nrState, a = st.applied || {};
  const ch = a.changes || {}, gl = a.glossary || {}, rg = a.v2_regen || {};
  if(st.applyError){
    return `<div style="margin-top:12px;padding:11px 13px;border:1px solid #F3C1C1;background:#fff5f5;border-radius:8px;font-size:12px;color:#b91c1c;">
      ✕ 应用失败：${esc(st.applyError)}
      <div style="margin-top:8px;"><button class="btn ghost" onclick="window._nrState.applyError=null;_nrRender()">返回修改</button></div></div>`;
  }
  const inplace = a.inplace !== false;
  return `<div style="margin-top:12px;padding:11px 13px;border:1px solid #C0DD97;background:#f7fbee;border-radius:8px;font-size:12px;line-height:1.95;">
      <b style="color:#2f855a;">✅ 已应用：V2 代码与视图已就地更新至 ${esc(a.version_label||'')}</b>
      ${inplace
        ? `（直接改写原代码文件与视图，版本 #${a.new_version_id}，未新建版本）`
        : `（新版本 #${a.new_version_id}，源版本 #${a.parent_version_id||'-'} 保留为 superseded）`}<br>
      ${rg.used_llm
        ? `🤖 <b style="color:#1d4ed8;">AI 已按确认结果续写 V2 代码</b>${rg.model?`（${esc(rg.model)}）`:''}`
        : `⚙️ ${esc(rg.note || '按确定性替换更新 V2 代码')}`}<br>
      改名 <b>${ch.renamed||0}</b> · 合并 <b>${ch.merged||0}</b> · 接纳 <b>${ch.accepted||0}</b> · 不入图库 <b>${ch.rejected||0}</b>
      ｜移除重复节点 <b>${a.removed_nodes||0}</b> · 重复边 <b>${a.removed_edges||0}</b><br>
      词库：<b style="color:${(gl.written||0)?'#2f855a':'#6b7280'};">${(gl.written||0)?`已同步写入 ${gl.written} 条新词条（新建概念 ${gl.concepts_created||0}）`:'未写入（仅本次生效）'}</b>
      ${(gl.skipped||0)?`（跳过 ${gl.skipped} 条：术语已存在）`:''}
      <div style="margin-top:9px;display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn" onclick="_nrOpenV2(0)">📄 查看更新后 V2 代码</button>
        <button class="btn" onclick="_nrPushModal()" title="确认完成后才提供写回入口：先语法检测，通过后才写入">🔗 写回建模软件（版本 #${a.new_version_id}）</button>
        <button class="btn ghost" onclick="_nrRefreshConv()" title="刷新会话后，上方代码/视图卡将展示更新后的代码与视图">🔄 刷新会话查看更新后代码/视图</button>
      </div>
      <div style="margin-top:6px;font-size:10.5px;color:var(--mut);">写回建模软件仅在归一确认完成后开放，且需再次点击确认才会执行。</div></div>`;
}
/* ── 闸口②：写入建模软件（overlay modal，非独立页） ── */
function _nrPushModal() {
  const a = window._nrState.applied || {};
  const ov = document.createElement('div');
  ov.id = 'nr-push-modal';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = `<div style="width:520px;background:#fff;border-radius:10px;box-shadow:0 10px 34px rgba(20,35,60,.2);overflow:hidden;font-size:12.5px;">
      <div style="padding:12px 16px;border-bottom:1px solid var(--line);font-weight:600;">🔗 写入建模软件（智源）</div>
      <div style="padding:14px 16px;line-height:1.95;">
        目标版本：<b>${esc(a.version_label||'')}</b>（#${a.new_version_id}）<br>
        <div style="margin-top:8px;">版本控制串 vc（留空则用上次配置）：</div>
        <input id="nr-push-vc" style="width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:5px;font-size:12px;" placeholder="projectId,branchId">
        <div style="margin-top:6px;color:var(--mut);font-size:11px;">先执行语法检测（只读），通过后才写入；检测不通过或调用失败均不会写入。</div>
        <div id="nr-push-msg" style="margin-top:8px;"></div>
      </div>
      <div style="padding:10px 16px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn ghost" onclick="_nrClosePush()">取消</button>
        <button class="btn primary" onclick="_nrPush()">确认写入</button>
      </div></div>`;
  document.body.appendChild(ov);
}
function _nrClosePush() {
  const ov = document.getElementById('nr-push-modal');
  if(ov) ov.remove();
}
function _nrPush() {
  const st = window._nrState, a = st.applied || {};
  const vc = (document.getElementById('nr-push-vc') || {}).value || '';
  const box = document.getElementById('nr-push-msg');
  if(box) box.innerHTML = `<span style="color:var(--mut);">⏳ 正在检测并写入…</span>`;
  api(`/api/sysml-versions/${a.new_version_id}/push-zhiyuan`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vc: vc})
  }).then(r => {
    if(!box) return;
    const ok = !!(r && r.ok);
    box.innerHTML = `<div style="padding:8px 10px;border-radius:6px;border:1px solid ${ok?'#C0DD97':'#F3C1C1'};background:${ok?'#f7fbee':'#fff5f5'};color:${ok?'#2f855a':'#b91c1c'};">
      ${ok ? '✅ 已写入建模软件' : '✕ ' + esc((r && (r.error || r.detail || r.check)) || '写入失败')}
      ${(!ok && r && r.stage==='check')?'<div style="margin-top:4px;color:#6b7280;">语法检测未通过时不会执行写入，请修正后重试。</div>':''}
      </div>`;
    if(ok) setTimeout(_nrClosePush, 1500);
  }).catch(e => {
    if(box) box.innerHTML = `<span style="color:#b91c1c;">✕ ${esc(e.message||String(e))}</span>`;
  });
}
function _nrExport() {
  const st = window._nrState;
  const data = {source_message_id: st.msgId, conversation_id: st.convId,
    write_glossary: !!st.writeGlossary, decisions: st.decisions};
  const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `norm-decisions-${st.msgId}.json`;
  a.click();
  toast('决策已导出');
}

function svmLegendHtml() {
  return `
    <span class="lg"><span class="lsw lbl fill"></span>块/部件</span>
    <span class="lg"><span class="lsw lbl req"></span>需求</span>
    <span class="lg"><span class="lsw lbl act"></span>用例/动作</span>
    <span class="lg"><span class="lsw lbl st"></span>状态</span>
    <span class="lg"><span class="lsw" style="border-color:#3B6D11;"></span>组合 composition</span>
    <span class="lg"><span class="lsw" style="border-color:#185FA5;"></span>满足/连接 satisfy</span>
    <span class="lg"><span class="lsw dash" style="border-color:#BA7517;"></span>派生/依赖 derive</span>
    <span class="lg"><span class="lsw dash" style="border-color:#A32D2D;"></span>冲突/迁移</span>`;
}
// 点击缩略图 → 在右侧预览区以 tab 打开该视图大图（替代原 svm-viewer 居中弹窗）
