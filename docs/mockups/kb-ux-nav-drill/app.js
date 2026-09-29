/* app.js —— 状态 + 视图 + 交互（原生 JS，无依赖）。
   结构：state（单一状态对象）→ render()（按状态重绘）→ 事件委托。
   所有"对照/变体"控件都放在**各自视图内部**（与控制条分离），避免同一开关出现两处。 */

/* ── 内联 Lucide 图标（离线零依赖；不新增 emoji） ── */
const ICON = {
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  filter: '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>',
  from: '<path d="m15 10 5 5-5 5"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>',
};
const ic = (n, cls = 'ic') => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICON[n] || ''}</svg>`;

/* ── 知识中心 6 个子页 / 看板 4 个视图簇（与正式系统一致；emoji 为被复刻对象的既有设计语言） ── */
const HUB = [
  { key: 'kb-a', label: '📊 数据看板' },
  { key: 'kb-e', label: '📄 文档库' },
  { key: 'kb-b', label: '🛡 治理中心' },
  { key: 'kb-d', label: '🕸 知识图谱' },
  { key: 'kb-c', label: '🧬 本体模型' },
  { key: 'kb-f', label: '📖 术语词典' },
];
const CLUSTERS = [
  { key: 'gov', label: '🧭 治理总览', n: 11 },
  { key: 'quality', label: '🧪 数据质量', n: 10 },
  { key: 'perf', label: '⚡ 消费与性能', n: 14 },
  { key: 'accept', label: '🧾 验收对照', n: 11 },
];

/* ── 单一状态对象 ── */
const S = {
  view: localStorage.getItem('kbm.view') || 'nav',  // nav | drill
  navMode: 'before',                                 // before | after-a | after-b
  drillScene: 'chunks',                              // chunks | candidates
  filtered: false,                                   // 是否带筛选态
  hub: 'kb-a',
  cluster: 'gov',
  theme: localStorage.getItem('kbm.theme') || 'light',
  loading: false,
  list: [],
  total: 0,
};

/* ── 左侧导航两种形态 ── */
function sidebarHtml() {
  if (S.view === 'nav' && S.navMode === 'after-a') {
    // 主推 A：知识中心展开为二级菜单（6 子项），内容区因此只剩一排
    return `<div class="brand">AI4MBSE</div>
      <div class="navgroup">🏠 首页</div>
      <div class="navgroup is-parent">${ic('chevronDown')} 知识中心</div>
      ${HUB.map((h) => `<div class="navsub ${h.key === S.hub ? 'is-active' : ''}" data-hub="${h.key}">
          <span class="dot"></span>${h.label}</div>`).join('')}
      <div class="navgroup">💠 能力中心</div>
      <div class="navgroup">🧩 更多</div>`;
  }
  return `<div class="brand">AI4MBSE</div>
    <div class="navgroup">🏠 首页</div>
    <div class="navgroup is-active">📚 知识中心</div>
    <div class="navgroup">💠 能力中心</div>
    <div class="navgroup">🧩 更多</div>`;
}

const hubRowHtml = () => `<div class="hubrow">
  ${HUB.map((h) => `<span class="chip ${h.key === S.hub ? 'on' : ''}" data-hub="${h.key}">${h.label}</span>`).join('')}
</div>`;

/* 页内视图簇：统一用**描边式**选中（与页面级 chip 的实心区分） */
const clusterRowHtml = () => `<div class="viewrow">
  <span class="lb">看板视图</span>
  ${CLUSTERS.map((c) => `<span class="chip outline ${c.key === S.cluster ? 'on' : ''}"
     data-cluster="${c.key}">${c.label} <span class="n">${c.n}</span></span>`).join('')}
  <span class="spacer"></span>
  <span class="lb">时间窗</span><span class="chip on">全部</span>
  <span class="lb">工作分支</span><span class="chip">personal</span>
</div>`;

/* ── 看板内容预览（精简，用于体现内容区被"两排 / 一排"占据的差异） ── */
function dashContentHtml() {
  const sc = DB.scope;
  return `<div class="panel">
    <div class="ph">🎯 首屏北极星 <span class="tag">健康分 + 3 项水位</span></div>
    <div class="pb"><div class="grid4">
      <div class="asset"><div class="l">知识健康分 <span class="st warn">关注</span></div>
        <div class="n">${DB.metrics.health.value}<small> /100</small></div>
        <div class="l">ok 19 · warn 5 · alert 11</div></div>
      <div class="asset"><div class="l">未决候选（审核积压） <span class="st ok">ok</span></div>
        <div class="n">0<small> 条</small></div><div class="l">目标 持续增长需扩审核产能</div></div>
      <div class="asset"><div class="l">权威基线未评审数据 <span class="st ok">ok</span></div>
        <div class="n">0<small> 条</small></div><div class="l">目标 = 0</div></div>
      <div class="asset"><div class="l">分块追溯覆盖率 <span class="st alert">alert</span></div>
        <div class="n">0<small> %</small></div><div class="l">目标 ≥ 90%</div></div>
    </div></div></div>
    <div class="panel">
      <div class="ph">📊 作用域对照 <span class="tag">同一份图谱的三个作用域 —— AI 只消费「权威基线」</span></div>
      <div class="pb"><table class="t">
        <tr><th>作用域</th><th>实体</th><th>关系</th><th>已评审</th><th>已发布</th><th>待处理 / 口径说明</th></tr>
        <tr><td><b>权威基线</b> <span class="st info">release</span></td>
          <td class="num">${sc.release.entities}</td><td class="num">${sc.release.relations}</td>
          <td class="num">${sc.release.entities}</td><td class="num">${sc.release.published}</td>
          <td>未评审 <span class="st ok">=0</span></td></tr>
        <tr><td><b>工作分支</b> <span class="st info">personal</span></td>
          <td class="num">${sc.branch.entities}</td><td class="num">${sc.branch.relations}</td>
          <td class="num">${sc.release.entities}</td><td>—</td>
          <td>候选 ${sc.branch.candidate} · 已废弃 ${sc.branch.deprecated} · 未发布增量 ${sc.branch.unpublishedDelta}</td></tr>
        <tr><td><b>知识总量</b> <span class="st info">global</span></td>
          <td class="num">${sc.global.entitiesDedup}</td><td class="num">${sc.global.relationsDedup}</td>
          <td>—</td><td>—</td>
          <td>文档 ${sc.global.documents} · 分块 ${sc.global.chunks}</td></tr>
      </table></div></div>`;
}

/* ══════════ 视图 ① 导航减层 ══════════ */
function viewNavHtml() {
  const bar = `<div class="viewrow">
    <span class="lb">对照</span>
    <span class="chip ${S.navMode === 'before' ? 'on' : ''}" data-navmode="before">改进前（两层半）</span>
    <span class="chip outline ${S.navMode === 'after-a' ? 'on' : ''}" data-navmode="after-a">改进后 · 主推 A（左树二级菜单）</span>
    <span class="chip outline ${S.navMode === 'after-b' ? 'on' : ''}" data-navmode="after-b">备选 B（内容区单排分段条）</span>
    <span class="spacer"></span>
    <span class="lb">${S.navMode === 'before'
      ? '同屏两排同类 chip，靠"实心/描边"区分层级'
      : (S.navMode === 'after-a'
        ? '页面级导航移到左侧树；内容区只剩一排视图 chip'
        : '页面与视图合成一排，用分隔符分组')}</span>
  </div>`;
  const crumb = `<div class="crumb"><span>知识中心</span>${ic('chevronRight')}<b>数据看板</b></div>`;
  if (S.navMode === 'before') return bar + crumb + hubRowHtml() + clusterRowHtml() + dashContentHtml();
  if (S.navMode === 'after-a') return bar + crumb + clusterRowHtml() + dashContentHtml();
  return bar + crumb
    + `<div class="viewrow">
         ${HUB.map((h) => `<span class="chip ${h.key === S.hub ? 'on' : ''}" data-hub="${h.key}">${h.label}</span>`).join('')}
         <span class="sep"></span>
         ${CLUSTERS.map((c) => `<span class="chip outline ${c.key === S.cluster ? 'on' : ''}"
            data-cluster="${c.key}">${c.label} <span class="n">${c.n}</span></span>`).join('')}
       </div>` + dashContentHtml();
}

/* ══════════ 视图 ② 筛选态下钻 ══════════ */
function crumbDrillHtml(metricKey, target) {
  const m = DB.metrics[metricKey];
  return `<div class="crumb">
    <span>知识中心</span>${ic('chevronRight')}<span>数据看板</span>${ic('chevronRight')}
    <span class="from">${ic('from')} ${m.name} · ${m.value}${m.unit}
      <span class="st ${m.status}">${m.status}</span></span>
    ${ic('chevronRight')}<b>${target}</b></div>`;
}
/* 筛选条：dashed=true 表示"未筛选"占位（灰、虚线、不可清除） */
const filterBarHtml = (label, hits, dashed = false) => `<div class="filterbar"${dashed ? ' style="background:var(--surface)"' : ''}>
  <span class="lb">${ic('filter')} 下钻筛选</span>
  <span class="fchip"${dashed ? ' style="border-style:dashed;border-color:var(--border);color:var(--text-sub)"' : ''}>${label}${
    dashed ? '' : `<span class="x" data-clear="1" title="清除此筛选">${ic('x')}</span>`}</span>
  <span class="hits">命中 <b>${hits}</b></span>
  <span class="spacer"></span>
  ${dashed ? '' : '<span class="linkbtn" data-clear="1">清除筛选</span>'}
</div>`;

function listHtml() {
  if (S.loading) {
    return `<div class="sk" style="width:52%"></div><div class="sk" style="width:78%"></div>
      <div class="sk" style="width:64%"></div>`;
  }
  if (!S.list.length) {
    return `<div class="empty"><div>${ic('check')}</div>
      <div class="t1">当前没有符合条件的分块</div>
      <div class="t2">筛选「未链接实体」后无结果 —— 说明追溯链已全部补齐</div>
      <div style="margin-top:8px"><span class="linkbtn" data-clear="1">清除筛选，查看全部分块</span></div></div>`;
  }
  return S.list.map((c) => `<div class="row">
    <span class="mono" style="color:var(--text-sub)">#${c.id}</span>
    <span class="ex" title="${c.section || ''}">${c.excerpt}</span>
    <span class="meta"><span>${c.doc.length > 26 ? c.doc.slice(0, 26) + '…' : c.doc}</span>
      <span class="mono">chunk ${c.i}</span>
      <span class="st ${c.linked ? 'ok' : 'alert'}">链接 ${c.linked}</span></span>
  </div>`).join('');
}

function sceneChunksHtml() {
  const body = S.filtered
    ? filterBarHtml('未链接实体', `${S.total} / ${DB.scope.global.chunks} 个分块`) + listHtml()
    : filterBarHtml('（未筛选：显示全部分块）', `${S.list.length} 条`, true) + listHtml();
  return crumbDrillHtml('chunk_trace', '文档库')
    + `<div class="panel">
         <div class="ph">📄 文档库 <span class="tag">上传 · 解析分块 · 向量化 · 抽取管道</span></div>
         ${body}</div>
       <div class="panel"><div class="ph">口径提示</div><div class="pb">${S.filtered
         ? '已由看板指标「分块追溯覆盖率」下钻带入筛选 <b>linked=none</b>；列表与命中数即该指标口径（5758 个分块全部未链接）。'
         : '现状：下钻只跳转页面，目标页拿不到筛选条件 —— 用户需自己再找一遍（此即本项要补的能力）。'}</div></div>`;
}

function sceneCandidatesHtml() {
  const empty = `<div class="empty"><div>${ic('check')}</div>
      <div class="t1">没有待审核的候选</div>
      <div class="t2">triples 0 + v2g 0 —— 与看板指标「未决候选（审核积压）」一致</div>
      <div style="margin-top:8px"><span class="linkbtn" data-clear="1">清除筛选，查看全部候选</span></div></div>`;
  const rows = S.list.map((c) => `<div class="row"><span class="mono">#${c.id}</span>
      <span class="ex">${c.name}</span>
      <span class="meta"><span>${c.type}</span><span class="st warn">${c.status}</span></span></div>`).join('');
  return crumbDrillHtml('pending_candidates', '治理中心')
    + `<div class="panel">
         <div class="ph">🛡 治理中心 <span class="tag">抽取审核 → 重复消歧 → 属性融合 → 三元组审核 → 发布</span></div>
         ${S.filtered ? filterBarHtml('状态：候选', `${S.total} 条`) : ''}
         <div class="pb" style="padding:0">
           <div class="row" style="background:var(--primary-soft)">
             <span class="ex"><b>分区：v2g 候选</b>（下钻自动定位到此分区）</span>
             <span class="meta"><span class="st info">已定位</span></span></div>
           ${S.loading ? listHtml()
             : (S.list.length ? rows
                : (S.filtered ? empty : `<div class="empty plain"><div>${ic('inbox')}</div><div class="t1">暂无候选</div></div>`))}
         </div></div>
       <div class="panel"><div class="ph">口径提示</div><div class="pb">${S.filtered
         ? '已由看板指标「未决候选（审核积压）」下钻带入筛选 <b>status=candidate</b> 并定位到 v2g 候选分区；命中 0 条即真实空态。'
         : '现状：下钻只跳到治理中心首页，用户还需自己切分区、自己设状态筛选。'}</div></div>`;
}

function viewDrillHtml() {
  return `<div class="viewrow">
      <span class="lb">场景</span>
      <span class="chip ${S.drillScene === 'chunks' ? 'on' : ''}" data-scene="chunks">A · 分块追溯覆盖率 → 文档库</span>
      <span class="chip ${S.drillScene === 'candidates' ? 'on' : ''}" data-scene="candidates">B · 未决候选 → 治理中心</span>
      <span class="spacer"></span>
      <span class="lb">对照</span>
      <span class="chip ${!S.filtered ? 'on' : ''}" data-filtered="0">改进前（无筛选态）</span>
      <span class="chip outline ${S.filtered ? 'on' : ''}" data-filtered="1">改进后（带筛选态）</span>
    </div>`
    + (S.drillScene === 'chunks' ? sceneChunksHtml() : sceneCandidatesHtml());
}

/* ══════════ 渲染 / 交互 ══════════ */
async function loadList() {
  S.loading = true; render();
  const r = S.drillScene === 'chunks'
    ? await fetchChunks({ unlinkedOnly: S.filtered })
    : await fetchCandidates({ status: S.filtered ? 'candidate' : '' });
  S.list = r.data; S.total = r.total; S.loading = false; render();
}

function render() {
  document.documentElement.setAttribute('data-theme', S.theme);
  document.getElementById('sidebar').innerHTML = sidebarHtml();
  document.getElementById('main').innerHTML = `<div class="view on">${
    S.view === 'nav' ? viewNavHtml() : viewDrillHtml()}</div>`;
  document.querySelectorAll('.ctrl .chip[data-view]').forEach((c) =>
    c.classList.toggle('on', c.dataset.view === S.view));
}

document.addEventListener('click', (e) => {
  const t = e.target.closest(
    '[data-view],[data-navmode],[data-scene],[data-filtered],[data-hub],[data-cluster],[data-clear]');
  if (!t) return;
  const d = t.dataset;
  if (d.view) {
    S.view = d.view; localStorage.setItem('kbm.view', S.view);
    if (S.view === 'drill' && !S.list.length) return loadList();
  }
  if (d.navmode) S.navMode = d.navmode;
  if (d.scene) { S.drillScene = d.scene; S.filtered = d.scene === 'candidates' ? true : S.filtered; return loadList(); }
  if (d.filtered) { S.filtered = d.filtered === '1'; return loadList(); }
  if (d.hub) S.hub = d.hub;
  if (d.cluster) S.cluster = d.cluster;
  if (d.clear) { S.filtered = false; return loadList(); }
  render();
});

document.getElementById('themeBtn').addEventListener('click', () => {
  S.theme = S.theme === 'light' ? 'dark' : 'light';
  localStorage.setItem('kbm.theme', S.theme);
  render();
});

render();
loadList();