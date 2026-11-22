/* 能力中心 · 每类型独立入口（Agent/技能/工具与MCP/提示词）
 * 2026-09-17 定稿：单区「个人可用」= 我创建的 + 我安装的（/api/plugins/mine）；
 * 市场/我安装的分组 tab 已废除，全市场目录与治理统一在「插件市场」页（28-studio.js loadMarketManage）
 * 底层统一走 /api/plugins（五类 skill/tool/mcp/agent/prompt）
 * 全局作用域（非 module），内联 onclick 依赖全局函数名
 */
(function () {
  var st = document.createElement('style');
  st.textContent = [
    '.capbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;}',
    '.captabs{display:flex;gap:2px;background:var(--bg2,#f7f9fc);border:1px solid var(--line,#d5dce6);border-radius:8px;padding:3px;}',
    '.captabs span{padding:5px 14px;border-radius:6px;cursor:pointer;font-size:13.5px;color:var(--mut,#5a6678);}',
    '.captabs span.on{background:var(--bg-card,#fff);color:var(--blue-d,#0C447C);font-weight:500;box-shadow:0 1px 2px rgba(0,0,0,.06);}',
    '.captabs span .tag{margin-left:5px;font-size:12px;}',
    '.capchips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;}',
    '.capchips span{padding:3px 10px;border-radius:20px;font-size:12.5px;cursor:pointer;border:1px solid var(--line,#d5dce6);color:var(--mut,#5a6678);background:transparent;}',
    '.capchips span.on{background:var(--blue-d,#0C447C);border-color:var(--blue-d,#0C447C);color:#fff;}',
    '.capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:10px;}',
    /* 2026-11-20 视图切换：列表态 = 真正的表格（带列头），不是压扁的卡片。 */
    '.capgrid.cap-list{display:block;}',
    '.captbl{width:100%;border-collapse:collapse;font-size:12.5px;}',
    '.captbl th{text-align:left;padding:7px 10px;border-bottom:2px solid var(--line,#d5dce6);color:var(--mut,#5a6678);font-weight:500;white-space:nowrap;background:var(--bg2,#f7f9fc);position:sticky;top:0;z-index:2;}',
    '.captbl td{padding:7px 10px;border-bottom:1px solid var(--line,#e9edf3);vertical-align:middle;}',
    '.captbl tr:hover td{background:var(--bg-hover,#f4f7fb);}',
    '.captbl .ct-name{font-weight:500;color:var(--ink,#1a2332);min-width:130px;}',
    '.captbl .ct-desc{color:var(--mut,#5a6678);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.captbl .ct-acts{white-space:nowrap;text-align:right;}',
    '.captbl .ct-acts .btn.sm{padding:2px 8px;font-size:12px;margin-left:4px;}',
    '.captbl .ct-group td{background:var(--bg2,#f7f9fc);font-weight:500;color:var(--ink,#1a2332);padding:6px 10px;}',
    /* 视图切换按钮组（胶囊二态） */
    '.capview{display:inline-flex;border:1px solid var(--line,#d5dce6);border-radius:6px;overflow:hidden;}',
    '.capview button{border:none;background:transparent;color:var(--mut,#5a6678);font-size:12.5px;padding:3px 10px;cursor:pointer;line-height:1.5;}',
    '.capview button.on{background:var(--blue-d,#0C447C);color:#fff;}',
    /* 插件市场列表态用 .captbl 表格（见 28-studio.js marketTableHtml），此处无需额外样式 */
    '.capcard{position:relative;background:var(--bg-card,#fff);border:1px solid var(--line,#d5dce6);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px;transition:border-color .15s;}',
    '.capcard:hover{border-color:var(--blue,#185FA5);}',
    '.capcard.mine{border-left:3px solid var(--green,#3B6D11);}',
    '.capcard.dis{opacity:.55;}',
    '.capcard-h{display:flex;align-items:center;gap:9px;}',
    '.capico{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:17.5px;flex-shrink:0;background:var(--bg2,#f0f3f8);}',
    '.captit{flex:1;min-width:0;font-size:14.5px;font-weight:500;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    // min-height 必须 = 2 行实际高度（2×1.55×13.5 ≈ 42）；原 37px 是按 12px 字号算的，
    // 2026-10-30 字号标准化后失配 ⇒ 短描述的卡片会比别的矮一截。
    '.capdesc{font-size:13.5px;color:var(--mut,#5a6678);line-height:1.55;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:42px;}',
    '.capmeta{display:flex;gap:10px;font-size:12.5px;color:var(--mut,#8a96ad);border-top:1px dashed var(--line,#e9edf3);padding-top:6px;flex-wrap:wrap;}',
    '.capmeta b{font-weight:500;color:var(--blue-d,#0C447C);}',
    '.capmeta .warn{color:var(--orange,#BA7517);}',
    '.capempty{grid-column:1/-1;padding:36px;text-align:center;color:var(--mut,#8a96ad);font-size:13.5px;}',
    '.cappane{display:none;} .cappane.on{display:block;}',
    '.capacts{display:flex;gap:6px;align-items:center;justify-content:flex-end;margin-top:8px;padding-top:8px;border-top:1px solid var(--line,#e9edf3);flex-wrap:wrap;}',
    '.capacts .btn.sm{padding:3px 10px;font-size:12.5px;}',
    '.capok{font-size:12.5px;color:var(--green,#3B6D11);padding:0 6px;}',
    /* 启用/停用 徽标与筛选 chips（2026-09-17：市场区此前看不到启用状态，也没有状态筛选） */
    '.capbadge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:500;line-height:1.6;}',
    '.capbadge.on{background:rgba(59,109,17,.13);color:var(--green,#3B6D11);}',
    '.capbadge.off{background:rgba(186,117,23,.15);color:var(--orange,#BA7517);}',
    '.capchips .capsep{width:1px;align-self:stretch;background:var(--line,#d5dce6);margin:2px 4px;padding:0;border:none;cursor:default;}',
    '.capmenu{position:absolute;right:0;top:26px;z-index:80;min-width:148px;background:var(--bg-card,#fff);border:1px solid var(--line,#d5dce6);border-radius:8px;box-shadow:0 6px 18px rgba(20,35,60,.14);padding:4px;}',
    '.capmenu div{padding:6px 10px;font-size:13.5px;border-radius:6px;cursor:pointer;white-space:nowrap;color:var(--ink,#1a2332);}',
    '.capmenu div:hover{background:var(--bg-hover,#eef4ff);}',
    '.capmenu .sep{height:1px;background:var(--line,#e9edf3);margin:4px 2px;padding:0;}',
    '.capmenu div.has-hint{white-space:normal;max-width:236px;}',
    '.capmenu div .hint{display:block;font-size:12px;color:var(--mut,#8a96ad);margin-top:1px;line-height:1.45;}',
    /* [2] Agent 主子分栏 */
    '.capgroup{grid-column:1/-1;}',
    '.capgroup + .capgroup{margin-top:14px;}',
    '.capgroup-h{display:flex;align-items:center;gap:8px;font-size:13.5px;font-weight:500;color:var(--ink,#1a2332);margin:0 0 8px;}',
    '.capgroup-h .cnt{font-size:12.5px;color:var(--mut,#8a96ad);font-weight:400;}',
    '.capgroup-h .ln{flex:1;height:1px;background:var(--line,#e9edf3);}',
    '.capgroup .capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:10px;}',
    '.caplegend{font-size:12.5px;color:var(--mut,#8a96ad);line-height:1.8;}',
    '.caplegend code{background:var(--bg2,#f0f3f8);padding:1px 5px;border-radius:4px;font-family:inherit;}',
  ].join('');
  document.head.appendChild(st);
})();

// 图标用「首字 + 主题色」而非 emoji —— 实测 emoji 在部分 Windows 环境渲染为方块
const CAP_META = {
  skill:  {abbr: '技', label: '技能',   color: '#5B3CD4'},
  tool:   {abbr: '具', label: '工具',   color: '#3B6D11'},
  mcp:    {abbr: 'M',  label: 'MCP',    color: '#0C447C'},
  agent:  {abbr: 'A',  label: 'Agent',  color: '#BA7517'},
  prompt: {abbr: '词', label: '提示词', color: '#5F5E5A'},
};
// 每个一级入口包含哪些能力类型（「工具与 MCP」是一个入口含两类）
const CAP_PAGES = {
  'st-skill':  ['skill'],
  'st-mcp':    ['tool', 'mcp'],
  'st-agent':  ['agent'],
  'st-prompt': ['prompt'],
};
const _capSt = {};   // pageId -> {type, tab, q, cat, market:[], mine:[]}
let _capCur = 'st-agent';   // 当前渲染页（供新建/编辑路由到对应专用表单）

function _capS(page, type) {
  if (!_capSt[page]) _capSt[page] = { type: type, q: '', cat: '', st: '', view: 'card', mine: [] };
  // 视图默认卡片：老状态对象（切页前已建、无 view 键）补默认值，避免 undefined 被当成 list 判断
  if (!_capSt[page].view) _capSt[page].view = 'card';
  if (type && !CAP_PAGES[page].includes(_capSt[page].type)) _capSt[page].type = type;
  return _capSt[page];
}
/* 2026-11-20 视图切换：只切 class + 按钮高亮，不重新拉数据（数据已在 s.mine）。
   默认 card；状态按页保存，切 tab 回来仍保持用户上次选择。 */
function capSetView(page, view) {
  const s = _capS(page);
  s.view = (view === 'list') ? 'list' : 'card';
  const grid = document.getElementById('cap-' + page + '-grid');
  if (grid) grid.classList.toggle('cap-list', s.view === 'list');
  const bc = document.getElementById('cap-' + page + '-vcard');
  const bl = document.getElementById('cap-' + page + '-vlist');
  if (bc) bc.classList.toggle('on', s.view !== 'list');
  if (bl) bl.classList.toggle('on', s.view === 'list');
  // 表格/卡片是两套 DOM，必须重渲染才能切换（只切 class 无法把卡片变表格）
  capRender(page);
}

/* 渲染整个能力区骨架 */
async function capMount(page) {
  const box = document.getElementById('cap-zone-' + page);
  if (!box) return;
  const types = CAP_PAGES[page] || [];
  const s = _capS(page, types[0]);
  const typeTabs = types.length > 1
    ? `<div class="captabs" style="margin-right:6px;">` + types.map(t =>
        `<span class="${s.type === t ? 'on' : ''}" data-captype="${t}" onclick="capSwitchType('${page}','${t}')">${CAP_META[t].label}</span>`).join('') + `</div>`
    : '';
  box.innerHTML = `
      <div class="capbar">
      ${typeTabs}
      <input id="cap-${page}-q" placeholder="搜索名称 / 描述…" style="border:1px solid var(--line,#d5dce6);border-radius:6px;padding:4px 10px;font-size:13.5px;width:190px;" oninput="capSearch('${page}',this.value)">
      <!-- 2026-11-20 视图切换：卡片 / 列表，默认卡片；状态按页记在 _capSt[page].view -->
      <span class="capview" role="group" aria-label="视图切换">
        <button id="cap-${page}-vcard" class="${s.view !== 'list' ? 'on' : ''}" onclick="capSetView('${page}','card')" title="卡片视图">▦ 卡片</button>
        <button id="cap-${page}-vlist" class="${s.view === 'list' ? 'on' : ''}" onclick="capSetView('${page}','list')" title="列表视图">☰ 列表</button>
      </span>
      <button class="btn sm ghost" onclick="capRefresh('${page}')">刷新</button>
      <span style="flex:1"></span>
      <!-- 2026-10-29：能力中心 → 能力账本 的**实体级互链**。
           此前两个 tab 各用一套数据源（本页读 /api/plugins，账本读 /api/capabilities/*），
           用户在本页看到卡片却无法知道"它在账本里被判成在用还是没用"。
           这里给一个**带 KPI 的入口**：先看整体判定，再一键跳过去细查。 -->
      <button class="btn sm ghost" id="cap-${page}-ledger" onclick="capJumpLedger()"
              title="跳到「能力账本」：看同一批能力的声明集 × 调用集判定（在用 / 已声明未用 / 无声明入口 / 未使用）">
        📊 能力账本 <span id="cap-${page}-ledger-kpi" style="color:var(--mut,#8a96ad);font-size:12px;"></span>
      </button>
      <div style="position:relative;">
        <button class="btn sm" onclick="capAddToggle(event)">＋ 新建能力 ▾</button>
        <div class="capmenu" id="cap-${page}-addmenu" style="display:none;"></div>
      </div>
    </div>
    <input type="file" id="cap-zip-input" accept=".zip" style="display:none;" onchange="capDoUploadSkill(this)">
    <div class="capchips" id="cap-${page}-cats"></div>
    <div class="capgrid" id="cap-${page}-grid"></div>
    <div style="margin-top:10px;display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;">
      <div class="caplegend" id="cap-${page}-sum"></div>
      <span style="flex:1"></span>
      <!-- ★ 2026-11-06（用户：「管理能力（审核/范围/下架）这块没数据，没用的话移除」）：
           原来这里是一个按钮 + 一个空面板，展开后**只有一句"去插件市场"的提示**，
           没有任何实体数据 —— 属于占位式的假入口（用户会以为点开能看到审核队列）。
           现改为**一行轻量文字链**：功能（去插件市场）没丢，但不再伪装成"能力管理"面板。
           能力中心的治理真入口是上方的「📊 能力账本」（有 KPI，能跳到判定明细）。 -->
      <a style="font-size:12px;color:var(--mut,#8a96ad);cursor:pointer;text-decoration:none;white-space:nowrap;"
         onclick="capJumpManage()" title="上架审核 / 范围分配 / 全市场插件管理在「插件市场」页">
        治理见「插件市场」→
      </a>
    </div>`;
  capHideSiblings(box);
  // 账本 KPI 异步填充（失败静默：账本不可用不应挡住能力中心本身）
  if (typeof capLoadLedgerKpi === 'function') capLoadLedgerKpi(page);
  await capLoad(page);
}

/* ══════════════════════════════════════════════════════════════════════
   能力中心 → 能力账本 互链（2026-10-29）
   ══════════════════════════════════════════════════════════════════════
   为什么需要它：两个 tab 的数据源不同 ——
     · 本页（st-agent / st-skill / st-mcp / st-prompt / st-market）读 /api/plugins
       （"我能用哪些能力"的消费视图）
     · 账本（st-registry）读 /api/capabilities/ledger
       （"这些能力到底有没有被用"的治理视图）
   同一批能力在两个 tab 里各有一套事实，中间没有连路 ⇒ 用户看完卡片想知道
   "它到底在用吗"只能自己去另一个 tab 再找一遍。

   本处只做两件事：**显示整体判定 KPI** + **一键跳转**。
   刻意**不按页预置 kind** —— 因为 st-agent 在账本里没有对应 kind
   （Agent 是"绑定者"，不是被计数的能力），硬映射会让人以为"Agent 不在账本里"。
   ⚠️ 这个取舍是有意的：宁可让用户跳过去自己筛，也不要给一个会误导的预筛。
*/
async function capLoadLedgerKpi(page) {
  const el = document.getElementById('cap-' + page + '-ledger-kpi');
  if (!el) return;
  try {
    const r = await fetch('/api/capabilities/ledger?days=30');
    if (!r.ok) return;
    const d = await r.json();
    const m = d.matrix || {};
    const inUse = m.in_use || 0, unused = m.unused || 0, rot = m.runtime_only || 0;
    el.textContent = `在用 ${inUse} · 未用 ${unused}` + (rot ? ` · 无声明入口 ${rot}` : '');
  } catch (e) { /* 静默：能力中心不因账本异常而残缺 */ }
}

function capJumpLedger() {
  // 走既有 subpage 显隐机制（.on class），不绕过加载钩子。
  // `switchStLibTab(null, 'st-registry')` 内部已有 `if(pane==='st-registry') loadCapRegistry()`
  // 的加载钩子（见 09-impact.js），所以这里**不需要**再手动触发一次 ——
  // 重复调用会导致两遍并发请求互相覆盖（先返回的那次后渲染）。
  if (typeof switchStLibTab === 'function') {
    switchStLibTab(null, 'st-registry');
  } else if (typeof loadCapRegistry === 'function') {
    loadCapRegistry();   // 理论上不会走到（switchStLibTab 是全局函数）
  }
}

/* 能力区独占该类型页：摆正一级 tab 条位置 + 隐藏同级旧视图
 * 要点：必须白名单保留一级 tab 条，且能力区要排在 tab 条之后（否则 tab 被挤到页底）
 */
function capHideSiblings(box) {
  var p = box.parentElement; if (!p) return;
  var tabbar = null;
  Array.prototype.forEach.call(p.children, function (ch) {
    if (!tabbar && ch.classList && ch.classList.contains('subtab')) tabbar = ch;
  });
  if (tabbar && box.previousElementSibling !== tabbar) {
    tabbar.parentNode.insertBefore(box, tabbar.nextSibling);
  }
  var kids = Array.prototype.slice.call(p.children);
  kids.forEach(function (ch) {
    if (ch === box || ch === tabbar) return;
    if (ch.querySelector && ch.querySelector('[data-tabgrp="stlib"]')) return;
    if (ch.style.display !== 'none') ch.style.display = 'none';
  });
}

function capSwitchType(page, type) {
  _capS(page, type).type = type;
  capMount(page);
}
function capSearch(page, v) {
  const s = _capS(page);
  s.q = (v || '').trim();
  clearTimeout(s._t);
  s._t = setTimeout(function () { capLoad(page); }, 260);
}
function capRefresh(page) { capLoad(page); }
// 2026-11-06 删除 `capToggleManage(page)`：它只服务于已被移除的「管理能力（审核/范围/下架）」
//   空面板（展开后仅一句"去插件市场"的提示，无任何实体数据）。全仓已无调用点（含 html）。
//   若将来要做真正的治理面板，应新建带数据源的实现，而不是恢复这个只切 display 的壳。
function capJumpManage() {
  const t = document.querySelector('[data-pane="st-market"]');
  if (t) t.click();
}

/* 加载数据：个人可用 = 我创建的 + 我安装的（/api/plugins/mine 一次拉取；2026-09-17 定稿，
 * 消费区不再展示全市场目录 —— 市场目录与治理统一在「插件市场」页） */
// P0-1（2026-11-22）：工具 → 适用对象类型反查缓存（能力中心「适用类型」列）。
// 一次加载、全局复用，避免每行一次 API（N+1）。取不到就空字典，显示「通用」不破表。
let _capTypeActions = {};
async function capLoadTypeActions(){
  try{
    const r = await api('/api/knowledge/ontology/actions/by-tool');
    _capTypeActions = (r && r.by_tool) || {};
  }catch(e){ _capTypeActions = {}; }
}

async function capLoad(page) {
  // 列表视图需要「适用类型」列，预取一次（与插件列表并行，不阻塞）
  capLoadTypeActions();
  const s = _capS(page);
  // 2026-09-30：工具页顺带预热 _toolsCache —— 工具卡片的「编辑」经 capEdit(pid) 路由到
  //   showToolForm(legacy_id)，而该函数靠 _toolsCache 取上下文。此前全站没有任何「进页面」
  //   时机调用 loadTools()（其 5 个调用点都在保存/删除后的刷新里），导致 _toolsCache 恒为 []
  //   → 编辑页退化成空白「新增工具」。此处预热，showToolForm 内另有回源兜底，双保险。
  const _pre = (page === 'st-mcp')
    ? Promise.resolve(api('/api/studio/tools')).then(function (r) {
        if (Array.isArray(r)) _toolsCache = r;
      }).catch(function () {})
    : Promise.resolve();
  const mn = await api('/api/plugins/mine?kind=' + encodeURIComponent(s.type) + '&q=' + encodeURIComponent(s.q))
    .catch(function () { return { items: [] }; });
  s.mine = (mn && mn.items) || [];
  // ★ B5-3：MCP 类型下**并入 `mcp_servers` 表**。
  //   根因（实测）：本函数只读 `/api/plugins/mine?kind=mcp`，
  //   而用户在「＋ 添加 MCP 服务器」里新增的记录落在 **`mcp_servers`** 表
  //   ⇒ 实测该接口对 `dev_ai_mcp` 返回 **0 条**
  //   ⇒ **用户新增的 MCP 在能力中心界面上一条都看不到**，
  //      界面上只剩内置那一条，像是"新增根本没生效"
  //      （而 `/api/studio/mcp-servers` 里它明明存在、还 online）。
  //   这是**写 A 表、读 B 表**的跨源断层 —— 与 MEMORY「落库了 ≠ 用户看得见」同源。
  //   并入时按 `name` 去重（同名以 mcp_servers 为准：那才是真正跑 tools/call 的记录），
  //   并打上 `_from_server` 供渲染区分（编辑/删除仍走 mcp-servers 端点）。
  if (page === 'st-mcp' && s.type === 'mcp') {
    try {
      const _srv = await api('/api/studio/mcp-servers');
      const _seen = new Set(s.mine.map(function (x) { return x.name; }));
      (Array.isArray(_srv) ? _srv : []).forEach(function (it) {
        if (_seen.has(it.name)) return;
        _seen.add(it.name);
        let _toolN = 0;
        try {
          const _tl = typeof it.tools === 'string' ? JSON.parse(it.tools || '[]') : (it.tools || []);
          _toolN = Array.isArray(_tl) ? _tl.length : 0;
        } catch (e) { /* 工具清单脏数据不阻断卡片 */ }
        s.mine.push({
          name: it.name, label: it.name,
          description: it.endpoint || '',
          enabled: it.enabled, builtin: it.builtin,
          legacy_table: 'mcp_servers', legacy_id: it.id,
          status: it.status, endpoint: it.endpoint,
          call_total: it.call_total, call_last: it.call_last,
          call_scope: it.call_scope,
          // ★ 2026-11-06：卡片要能回答「生效了吗 / 凭据填上了吗」——
          //   过去只带 plugins 侧的 status_label（发布态），与 mcp_servers.status
          //   （online/offline）是两套语义，且没有任何测试入口 ⇒ 状态显示不准确。
          mcp_online: it.status === 'online',
          last_check: it.last_check, last_error: it.last_error,
          latency_ms: it.latency_ms, tool_count: _toolN,
          headers_masked: it.headers_masked || {},
          _from_server: true,
        });
      });
    } catch (e) { /* 取不到就只显示 plugins 侧，不阻断 */ }
  }
  await _pre;
  capRender(page);
}

/* ══ 2026-11-20：列表视图 = 真正的表格（带列头）══════════════════
 * 卡片视图仍走 capCard；列表视图走这里。列的语义与卡片底部 capmeta 一致：
 * 名称 / 类型 / 描述 / 来源 / 发布态 / 个人态 / AI可用 / 版本 / 操作。
 * 动作只放高频且语义明确的几个（启停/编辑/卸载/删除 + 更多），避免表格被按钮撑爆。
 */
function capTableRow(x, page) {
  const t = x.type || 'skill';
  const m = CAP_META[t] || { abbr: '?', label: t, color: '#5F5E5A' };
  const label = capLabel(x);
  const pid = _capPid(x);
  const desc = x.description || (x.manifest && x.manifest.description) || '—';
  const installed = !!x.installed;
  const enabled = x.enabled !== false;
  const isBuiltin = !!x.is_builtin, isMine = !!x.is_mine;
  const published = (x.status === 'published');
  const source = isBuiltin ? '内置' : (isMine ? '我创建' : (installed ? '他人共享' : '—'));
  const scope = !!x.is_public ? '已上架' : (x.pending_share ? '申请中' : '仅自己');
  let acts = '';
  if (x._from_server && x.legacy_id) {
    acts += `<button class="btn sm ghost" onclick="testMCP(${Number(x.legacy_id)})" title="测试连通+发现">🔌 测试</button>`;
  }
  if (x.can_publish_self) acts += `<button class="btn sm" onclick="capPublish('${esc(pid)}','${page}')">发布</button>`;
  if (published) acts += `<button class="btn sm ghost" onclick="capSetInstalled('${esc(pid)}',${enabled ? 'false' : 'true'},'${page}')">${enabled ? '停用' : '启用'}</button>`;
  if (x.can_edit) acts += `<button class="btn sm ghost" onclick="capEdit('${esc(pid)}')">编辑</button>`;
  if (installed && !x.is_builtin && !x.is_mine) acts += `<button class="btn sm ghost" onclick="capUninstall('${esc(pid)}','${page}')">卸载</button>`;
  if (x.can_delete && !isBuiltin && !(installed && !isMine)) {
    acts += `<button class="btn sm ghost" style="color:var(--red,#A32D2D);" onclick="capDelete('${esc(pid)}','${esc(String(label).replace(/'/g, ''))}','${page}')">删除</button>`;
  }
  acts += capMoreBtn(x, page);
  const badge = (on, txt, tip) => `<span class="capbadge ${on ? 'on' : 'off'}" title="${tip}">${txt}</span>`;
  // P0-1：适用对象类型列（多类型 +N 折叠；空显示通用）。仅 tool 类型有意义。
  let scopeTypesCell = '—';
  if(t === 'tool'){
    const _ts = _capTypeActions[x.name] || [];
    scopeTypesCell = _ts.length
      ? '<span title="'+esc(_ts.map(function(z){return z.name;}).join('、'))+'">'
        + esc(_ts[0].name) + (_ts.length>1 ? ' <span style="color:var(--mut);">+'+(_ts.length-1)+'</span>' : '')
        + '</span>'
      : '<span style="color:var(--mut);">通用</span>';
  }
  return '<tr>'
    + `<td class="ct-name"><span style="color:${m.color};font-weight:600;">${m.abbr}</span> ${esc(label)}</td>`
    + `<td>${esc(m.label)}</td>`
    + `<td class="ct-desc" title="${esc(desc)}">${esc(desc)}</td>`
    + `<td>${esc(source)}${!isBuiltin ? ' · ' + esc(scope) : ''}</td>`
    + `<td>${x._from_server ? 'MCP 服务' : esc(_capStatusText(x))}</td>`
    + `<td>${installed ? badge(enabled, enabled ? '个人已启用' : '个人已停用', '个人安装态，仅影响你自己') : '—'}</td>`
    + `<td>${badge(!!x.consumable, x.consumable ? 'AI 可用' : 'AI 不可用', '综合判定：已发布 且 个人已启用 且 已安装')}</td>`
    + `<td>v${esc(x.current_version || '—')}</td>`
    + `<td>${scopeTypesCell}</td>`
    + `<td class="ct-acts">${acts}</td>`
    + '</tr>';
}
function capTableHtml(list, page, groups) {
  const head = '<thead><tr><th>名称</th><th>类型</th><th>描述</th><th>来源</th><th>发布态</th>'
    + '<th>个人态</th><th>AI 判定</th><th>版本</th><th>适用类型</th><th style="text-align:right;">操作</th></tr></thead>';
  let body = '';
  if (groups && groups.length) {
    groups.forEach(function (g) {
      body += `<tr class="ct-group"><td colspan="10">${esc(g.title)} <span style="color:var(--mut);font-weight:400;">· ${g.list.length}</span></td></tr>`;
      body += g.list.map(function (x) { return capTableRow(x, page); }).join('');
    });
  } else {
    body = list.map(function (x) { return capTableRow(x, page); }).join('');
  }
  return `<table class="captbl">${head}<tbody>${body}</tbody></table>`;
}

function capRender(page) {
  _capCur = page;
  const s = _capS(page);
  const items = s.mine;
  const grid = document.getElementById('cap-' + page + '-grid');
  const cats = document.getElementById('cap-' + page + '-cats');
  const sum = document.getElementById('cap-' + page + '-sum');
  if (!grid) return;
  // 2026-11-20：重建/切类型后同步视图 class（grid 元素被 capMount 重建时会丢 class）
  grid.classList.toggle('cap-list', s.view === 'list');

  // [3] 筛选 chips：状态 + 分类 —— 均从当前集合实时聚合，非写死（2026-09-17 定稿：单区通用）
  if (cats) {
    if (items.length) {
      const stCnt = { on: 0, off: 0 };
      items.forEach(function (x) {
        if (!x.installed) return;
        if (x.enabled === false) stCnt.off++; else stCnt.on++;
      });
      let chips = `<span class="${!s.st ? 'on' : ''}" onclick="capFilterSt('${page}','')">全部 ${items.length}</span>`;
      if (stCnt.on) chips += `<span class="${s.st === 'on' ? 'on' : ''}" onclick="capFilterSt('${page}','on')">已启用 ${stCnt.on}</span>`;
      if (stCnt.off) chips += `<span class="${s.st === 'off' ? 'on' : ''}" onclick="capFilterSt('${page}','off')">已停用 ${stCnt.off}</span>`;
      const cnt = {};
      items.forEach(function (x) { const c = x.category || '未分类'; cnt[c] = (cnt[c] || 0) + 1; });
      const keys = Object.keys(cnt).sort(function (a, b) { return cnt[b] - cnt[a]; });
      chips += `<span class="capsep"></span>`;
      keys.forEach(function (c) {
        chips += `<span class="${s.cat === c ? 'on' : ''}" onclick="capFilterCat('${page}','${esc(c)}')">${esc(c)} ${cnt[c]}</span>`;
      });
      cats.innerHTML = chips;
      cats.style.display = '';
    } else { cats.innerHTML = ''; cats.style.display = 'none'; }
  }

  let list = items;
  if (s.st) list = list.filter(function (x) {
    if (!x.installed) return false;   // 未安装项无启停状态，不落在任一状态筛里
    return s.st === 'on' ? x.enabled !== false : x.enabled === false;
  });
  if (s.cat) list = list.filter(function (x) { return (x.category || '未分类') === s.cat; });

  if (!list.length) {
    const filtered = s.st || s.cat;
    grid.innerHTML = `<div class="capempty">${filtered ? '没有符合筛选条件的能力' :
      '还没有可用能力：去「插件市场」安装，或点「＋ 新建能力」自己创建'}</div>`;
  } else if (s.view === 'list') {
    // 2026-11-20 列表态：真正的表格（列头+行）；Agent 仍按主/子分组
    if (s.type === 'agent') {
      const mains = [], subs = [];
      list.forEach(function (x) {
        const r = (x.manifest && x.manifest.agent_role) || '';
        (r === 'main' ? mains : subs).push(x);
      });
      const groups = [];
      if (mains.length) groups.push({ title: '编排 Agent（主）', list: mains });
      if (subs.length) groups.push({ title: '专业 Agent（子）', list: subs });
      grid.innerHTML = capTableHtml(list, page, groups);
    } else {
      grid.innerHTML = capTableHtml(list, page, null);
    }
  } else if (s.type === 'agent') {
    // [2] Agent 主子分栏：上下两栏（卡片态）
    const mains = [], subs = [];
    list.forEach(function (x) {
      const r = (x.manifest && x.manifest.agent_role) || '';
      (r === 'main' ? mains : subs).push(x);
    });
    const sec = function (title, hint, arr) {
      if (!arr.length) return '';
      return `<div class="capgroup">
        <div class="capgroup-h">${title}<span class="cnt">${hint} · ${arr.length}</span><span class="ln"></span></div>
        <div class="capgrid">${arr.map(function (x) { return capCard(x, 'mine', page); }).join('')}</div>
      </div>`;
    };
    grid.innerHTML =
      sec('编排 Agent（主）', '负责任务分派与编排', mains) +
      sec('专业 Agent（子）', '承担具体建模与分析任务', subs);
  } else {
    grid.innerHTML = list.map(function (x) { return capCard(x, 'mine', page); }).join('');
  }

  // [4] 动作语义图例 —— 直接回答"卸载/停用/删除分别是什么"
  if (sum) {
    sum.innerHTML = `共 <b>${list.length}</b> 个（个人可用 = 我创建的 + 我安装的）` +
      `<br>停用=仅自己不用 · 卸载=移出我的清单（可重装） · 全局下架=对所有人生效（市场管理员） · 删除=移除该能力本体（作者/管理员）`;
  }
  // 「＋ 新建能力」按当前类型路由到原有的专用表单
  capBuildAddMenu(page);
  capBindHealth(page);
}

// ══ 绑定关系体检（2026-10-30）══════════════════════════════════════════
// 能力中心此前只回答"我有哪些能力"，不回答"这些能力跟 Agent 的关系还成立吗"。
// 真库实测 178 条绑定里 45 条挂在已停用的 Agent 上、11 条目标已停用、
// 17 条"停用了但仍会被注入"——这些都**没有任何界面提示**，
// 用户只能靠"AI 行为不对"倒推。判据在 services/capability_graph.py（单一真源）。
let _bindHealth = null, _bindHealthAt = 0, _bindHealthFilter = 'attention';
let _bindHealthKw = '', _bindHealthPage = '';
// 2026-11-20 批量操作：选中项的原始索引集合（capBindAct 用的就是 d.details 的索引）
let _bindSel = new Set();
async function capBindHealth(page) {
  // ⚠️ 插在**卡片网格之前**，不是图例之后：图例在网格下方（页面底部），
  //   实测插在那里时 top=1533px，用户根本看不到 —— "加了等于没加"。
  const grid = document.getElementById('cap-' + page + '-grid');
  if (!grid || !grid.parentNode) return;
  _bindHealthPage = page;
  if (!_bindHealth || Date.now() - _bindHealthAt > 60000) {
    try { _bindHealth = await api('/api/studio/bindings/audit'); _bindHealthAt = Date.now(); }
    catch (e) { return; }
  }
  let el = document.getElementById('cap-bind-health');
  if (!el) {
    el = document.createElement('div');
    el.id = 'cap-bind-health';
    grid.parentNode.insertBefore(el, grid);
  }
  const d = _bindHealth, bs = d.by_state || {};
  const attention = Number(d.attention != null ? d.attention : ((d.broken || 0) + (d.govern || 0)));
  const bad = Number(d.broken || 0), gov = Number(d.govern || 0);
  el.style.cssText = 'font-size:12.5px;color:var(--mut,#8a96ad);padding:2px 0 8px;line-height:1.6;';
  el.innerHTML =
    '<div onclick="capOpenBindHealth()" title="点击查看每一条绑定的原因、影响面，并就地处理"'
    + ' style="cursor:pointer;border:1px solid var(--line,#d9dfe8);border-radius:6px;'
    + 'padding:6px 10px;background:var(--panel,#fff);">'
    + '🔗 绑定关系：<b>' + d.total + '</b> 条 · 生效 <b style="color:#2e7d32">' + d.effective + '</b> · '
    + '需关注 <b style="color:' + (attention ? '#c92a2a' : '#2e7d32') + '">' + attention + '</b>'
    + '（未生效 ' + bad + '：悬空 ' + (bs.dangling || 0) + ' · 目标停用 ' + (bs.off || 0)
    + ' · MCP 离线 ' + (bs.offline || 0) + ' · 未就绪 ' + (bs.unready || 0)
    + ' · 所属 Agent 停用 ' + (bs.inactive || 0)
    + '；仍会注入 ' + gov + '：旁路 ' + (bs.bypass || 0) + ' · 下架旧表 ' + (bs.plugin_off || 0) + '）'
    + '<span style="float:right;color:var(--pri,#3478f6);font-weight:600;">查看明细并处理 →</span>'
    + '<br><span style="color:#8a96ad">生效=这一轮会进模型上下文；用量=能力卡片调用次数，二者不是一回事。</span>'
    + '</div>';
}

/* ── 绑定体检：右侧明细/处置面板（2026-11-19）──────────────────
 * 判据完全来自 /api/studio/bindings/audit（services/capability_graph 单一真源）。
 * 前端只做三件事：筛选、解释、调用后端给出的 actions；不重算状态。
 */
async function capOpenBindHealth(filter) {
  if (typeof filter === 'string') _bindHealthFilter = filter;
  openPanel('🔗 绑定关系体检 · 明细与处理', '<div class="loading">加载中…</div>');
  try {
    if (!_bindHealth || Date.now() - _bindHealthAt > 60000) {
      _bindHealth = await api('/api/studio/bindings/audit');
      _bindHealthAt = Date.now();
    }
    capRenderBindHealthPanel();
  } catch (e) {
    const b = document.getElementById('panel-body');
    if (b) b.innerHTML = '<div class="fempty">加载失败：' + esc(String(e && e.message || e)) + '</div>';
  }
}
function capBindSetFilter(f) { capOpenBindHealth(f); }
function capBindSearch(v) { _bindHealthKw = String(v || '').trim().toLowerCase(); capRenderBindHealthPanel(); }
function capRenderBindHealthPanel() {
  const body = document.getElementById('panel-body');
  if (!body || !_bindHealth) return;
  const d = _bindHealth;
  const f = _bindHealthFilter;
  const kw = _bindHealthKw;
  const counts = { attention: Number(d.attention || 0), broken: Number(d.broken || 0),
                   govern: Number(d.govern || 0) };
  const chips = [['attention', '全部需关注'], ['broken', '未生效'], ['govern', '仍会注入']]
    .map(function (x) {
      const on = f === x[0];
      return '<button class="btn sm ' + (on ? '' : 'ghost') + '" onclick="capBindSetFilter(\'' + x[0] + '\')"'
        + ' style="margin-right:6px;' + (on ? 'background:#3478f6;color:#fff;border-color:#3478f6;' : '') + '">'
        + x[1] + ' ' + counts[x[0]] + '</button>';
    }).join('');
  let rows = (d.details || []).filter(function (x) {
    if (f === 'broken' && x.effective) return false;
    if (f === 'govern' && !x.effective) return false;
    if (!kw) return true;
    return [x.agent, x.name, x.state, x.state_label, x.reason, x.hint]
      .some(function (v) { return String(v || '').toLowerCase().indexOf(kw) >= 0; });
  });
  // 2026-11-20：记录当前筛选下可见项的原始索引，供"全选本页"用
  _bindVisibleIdx = rows.map(function (x) { return (d.details || []).indexOf(x); });
  const color = { bad: '#c92a2a', warn: '#c9821a', muted: '#8a96ad', info: '#3478f6', ok: '#2e7d32' };
  const cards = rows.length ? rows.map(function (x) {
    const i = (d.details || []).indexOf(x);
    const lv = color[x.level] || color.muted;
    const chk = (x.actions || []).some(function (a) { return a.id === 'unbind'; })
      ? '<input type="checkbox" class="cap-bind-cb" data-i="' + i + '" ' + (_bindSel.has(i) ? 'checked' : '')
        + ' onclick="capBindToggleSel(' + i + ',this.checked)" title="勾选以批量解绑"'
        + ' style="width:15px;height:15px;flex:none;margin-right:8px;cursor:pointer;">'
      : '';
    const acts = (x.actions || []).map(function (a, ai) {
      return '<button class="btn sm ' + (a.id === 'unbind' ? 'ghost' : '') + '"'
        + ' onclick="capBindAct(' + i + ',' + ai + ')"'
        + ' title="' + esc(a.risk || '') + '" style="margin:2px 4px 0 0;font-size:12px;padding:1px 8px;">'
        + esc(a.label) + '</button>';
    }).join('') || '<span style="color:#8a96ad">无可用动作</span>';
    const usage = x.usage || {};
    const tgt = x.target || {};
    const plug = x.plugin || {};
    return '<div style="border:1px solid var(--line,#d9dfe8);border-radius:6px;padding:8px 10px;margin-bottom:8px;">'
      + '<div style="font-size:13.5px;font-weight:600;color:' + lv + ';display:flex;align-items:center;">'
      + chk
      + '<span style="flex:1;">' + esc(x.agent) + ' → ' + esc(x.kind_label || x.kind) + '「' + esc(x.name) + '」</span>'
      + '<span style="font-size:12px;font-weight:400;">' + esc(x.state_label || x.state) + '</span></div>'
      + '<div style="font-size:12.5px;line-height:1.65;margin-top:4px;">'
      + '<b>原因</b>：' + esc(x.reason)
      + '<br><b>怎么处理</b>：' + esc(x.hint)
      + '<br><b>用量</b>：调用 ' + (usage.call_count || 0) + ' 次'
      + (usage.last_used_at ? '（最近 ' + esc(usage.last_used_at) + '）' : '')
      + ' · 生效=会进上下文，用量=真被调用，两者不同'
      + '<br><b>目标</b>：' + (tgt.id ? esc(tgt.table) + '#' + esc(tgt.id) + ' · ' + esc(tgt.status || '') : '已不存在')
      + (plug.plugin_id ? ' · 插件 ' + esc(plug.plugin_id) + '(' + esc(plug.status || '') + ')' : '')
      + '</div><div style="margin-top:4px;">' + acts + '</div></div>';
  }).join('') : '<div class="fempty">当前筛选下没有需关注项</div>';
  // 2026-11-20 批量工具栏：全选 / 计数 / 动作下拉（启用Agent·启用目标·测试MCP·解绑）/ 批量执行
  const allChecked = rows.length > 0 && _bindVisibleIdx.every(function (i) { return _bindSel.has(i); });
  const bulkbar = '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;'
    + 'padding:7px 10px;margin-bottom:10px;border:1px solid var(--line,#d9dfe8);border-radius:6px;background:var(--bg2,#f7f9fc);">'
    + '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12.5px;">'
    + '<input type="checkbox" id="cap-bind-all" ' + (allChecked ? 'checked' : '')
    + ' onchange="capBindSelAll(this.checked)" style="width:15px;height:15px;cursor:pointer;">全选本页</label>'
    + '<span id="cap-bind-selcnt" style="font-size:12.5px;color:var(--mut);">已选 <b>' + _bindSel.size + '</b> 条</span>'
    + '<span style="flex:1;"></span>'
    + '<select id="cap-bind-bulk-act" style="border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:12.5px;">'
    + '<option value="enable_agent">▶ 启用所属 Agent</option>'
    + '<option value="enable_target">▶ 启用目标能力</option>'
    + '<option value="test_mcp">🔌 测试 MCP 连通</option>'
    + '<option value="unbind">🗑 解绑</option>'
    + '</select>'
    + '<button class="btn sm" onclick="capBindBatch()" style="font-size:12px;">批量执行</button>'
    + '<button class="btn sm ghost" onclick="capBindSelClear()" style="font-size:12px;">取消选择</button>'
    + '</div>';
  body.innerHTML = '<div style="padding:14px 16px 24px;font-size:13px;line-height:1.7;">'
    + '<div style="margin-bottom:8px;">' + chips + '</div>'
    + '<input id="cap-bind-kw" placeholder="搜索 Agent / 能力 / 原因" value="' + esc(kw) + '"'
    + ' oninput="capBindSearch(this.value)"'
    + ' style="width:100%;box-sizing:border-box;padding:6px 8px;margin-bottom:8px;border:1px solid var(--line,#d9dfe8);border-radius:5px;">'
    + bulkbar
    + '<div style="font-size:12px;color:#8a96ad;margin-bottom:8px;">共 ' + rows.length
    + ' 条。<b style="color:#c92a2a;">未生效</b>（失效/停用/离线/Agent停用）= 可修复，执行对应动作即可；'
    + '<b style="color:#c9821a;">仍会注入</b>（旁路/插件下架但仍生效）= 结构性提示，'
    + '因内置工具按名字注入、插件自动消费通道绕过 status，这类<b>不会因"处理"消失</b>，'
    + '只能解绑或改变能力本身的状态。动作由后端按绑定状态生成；解绑只删关系，不删能力本体。</div>'
    + cards + '</div>';
}
/* ── 批量选择 / 批量解绑（2026-11-20）──────────────────────── */
let _bindVisibleIdx = [];
function capBindToggleSel(i, checked) {
  if (checked) _bindSel.add(Number(i)); else _bindSel.delete(Number(i));
  const c = document.getElementById('cap-bind-selcnt');
  if (c) c.innerHTML = '已选 <b>' + _bindSel.size + '</b> 条';
  const all = document.getElementById('cap-bind-all');
  if (all) all.checked = _bindVisibleIdx.length > 0 && _bindVisibleIdx.every(function (x) { return _bindSel.has(x); });
}
function capBindSelAll(checked) {
  _bindVisibleIdx.forEach(function (i) { if (checked) _bindSel.add(i); else _bindSel.delete(i); });
  capRenderBindHealthPanel();   // 重绘以同步所有复选框与计数
}
function capBindSelClear() { _bindSel.clear(); capRenderBindHealthPanel(); }
const _BATCH_ACT_LABEL = { enable_agent: '启用所属 Agent', enable_target: '启用目标能力',
                           test_mcp: '测试 MCP 连通', unbind: '解绑' };
async function capBindBatch() {
  if (!_bindSel.size) { toast('请先勾选要处理的绑定'); return; }
  const sel = document.getElementById('cap-bind-bulk-act');
  const actId = sel ? sel.value : 'unbind';
  const rows = (_bindHealth && _bindHealth.details || []);
  // 只取勾选且确实带该动作的项；不具备该动作的项跳过（统计后提示）
  const targets = [], skipped = [];
  _bindSel.forEach(function (i) {
    const x = rows[i];
    if (!x) return;
    const a = (x.actions || []).find(function (v) { return v.id === actId; });
    if (a) targets.push({ i: i, a: a, label: x.agent + ' → ' + x.name });
    else skipped.push(x.agent + ' → ' + x.name);
  });
  if (!targets.length) {
    toast('勾选的 ' + _bindSel.size + ' 条都不支持「' + (_BATCH_ACT_LABEL[actId] || actId) + '」，请换动作或改勾选');
    return;
  }
  const risk = actId === 'unbind' ? '\n\n解绑只删关系，不删能力本体，其他 Agent 的绑定保留。'
    : (actId === 'enable_target' ? '\n\n启用目标对所有引用它的 Agent 都会恢复。'
    : (actId === 'enable_agent' ? '\n\n启用 Agent 会让它重新参与路由。' : '\n\n只读探测，不改动配置。'));
  const ok = await confirmDialog(
    '将对 ' + targets.length + ' 条绑定执行「' + (_BATCH_ACT_LABEL[actId] || actId) + '」：\n'
    + targets.slice(0, 8).map(function (t) { return '· ' + t.label; }).join('\n')
    + (targets.length > 8 ? '\n· 其余 ' + (targets.length - 8) + ' 条…' : '')
    + (skipped.length ? '\n\n另 ' + skipped.length + ' 条不具备该动作，本次跳过。' : '')
    + risk + '确定继续？',
    { title: '批量处理确认', danger: actId === 'unbind', okText: '确认' + (_BATCH_ACT_LABEL[actId] || actId) + ' ' + targets.length + ' 条' });
  if (!ok) return;
  let done = 0, fail = 0;
  for (const t of targets) {
    try {
      const r = await api(t.a.api, { method: t.a.method });
      if (r && r.error) fail++; else done++;
    } catch (e) { fail++; }
  }
  toast('批量' + (_BATCH_ACT_LABEL[actId] || actId) + '完成：成功 ' + done + ' 条'
    + (fail ? '，失败 ' + fail + ' 条' : '') + (skipped.length ? '，跳过 ' + skipped.length + ' 条' : ''));
  _bindSel.clear(); _bindHealth = null; _bindHealthAt = 0;
  if (_bindHealthPage) await capBindHealth(_bindHealthPage);
  await capOpenBindHealth(_bindHealthFilter);
}
// 兼容旧调用
async function capBindBatchUnbind() {
  const sel = document.getElementById('cap-bind-bulk-act');
  if (sel) sel.value = 'unbind';
  return capBindBatch();
}
async function capBindAct(i, ai) {
  const x = (_bindHealth && _bindHealth.details || [])[i];
  const a = x && (x.actions || [])[ai];
  if (!a) { toast('动作已失效，请刷新面板'); return; }
  // 2026-11-20：操作确认一律用自定义 confirmDialog，不用浏览器原生 confirm（系统默认样式）
  if (a.confirm && !(await confirmDialog(a.confirm, { title: '绑定关系处理', danger: a.id === 'unbind' }))) return;
  try {
    const r = await api(a.api, { method: a.method });
    if (r && r.error) { toast('处理失败：' + r.error); return; }
    toast('✓ 已执行：' + a.label);
    _bindHealth = null; _bindHealthAt = 0;
    if (typeof capBindHealth === 'function' && _bindHealthPage) await capBindHealth(_bindHealthPage);
    await capOpenBindHealth(_bindHealthFilter);
  } catch (e) { toast('处理失败：' + String(e && e.message || e)); }
}

function capFilterCat(page, c) {
  _capS(page).cat = c;
  capRender(page);
}

/* 状态筛选：'' 全部 / 'on' 已启用 / 'off' 已停用（两区通用，实时聚合） */
function capFilterSt(page, st) {
  _capS(page).st = st || '';
  capRender(page);
}

/* ── 动作矩阵（[4] 去冗余的核心）─────────────────────────────
 * 四种动作语义正交，按「能力归属 × 是否安装」裁剪，避免同质按钮堆叠：
 *   停用/启用  plugin_installs.enabled  个人级，仅影响自己
 *   全 局 下 架  plugins.status          管理员，影响所有用户
 *   卸    载  删除 plugin_installs 行    移出我的清单，能力本体仍在，可重装
 *   删    除  plugins.status='removed'  移除能力本体，仅作者/管理员；内置能力禁止
 */
function capLabel(x) {
  return (x.manifest && x.manifest.label && (x.manifest.label.zh_CN || x.manifest.label.name)) || x.name || x.plugin_id;
}
function _capPid(x) { return x.plugin_id || x.id; }

/* 页内按 pid 反查显示名（用于"隶属"等交叉引用） */
function _capNameByPid(page, pid) {
  const s = _capS(page);
  const all = s.mine || [];
  for (let i = 0; i < all.length; i++) {
    if (_capPid(all[i]) === pid) return capLabel(all[i]);
  }
  return pid;
}

function capMoreBtn(x, page) {
  const pid = _capPid(x);
  const label = capLabel(x);
  const menuId = 'capmenu-' + String(pid).replace(/[^a-zA-Z0-9]/g, '_');
  const items = [];

  items.push(`<div onclick="capCopy('${esc(pid)}','${page}')">复制副本</div>`);
  items.push(`<div onclick="unifiedRun('${esc(pid)}')">沙箱试运行</div>`);
  items.push(`<div onclick="unifiedCalls('${esc(pid)}')">调用日志</div>`);

  // ── P1-7：状态（我能用）与可见范围（别人能否看到）是**互不牵连**的两个维度 ──
  //   状态组（status）：发布 / 取消发布 —— 免审核，只影响「能否被 AI 消费」
  //
  // ⚠ 入口归位（2026-09-17）：本页是「我的能力清单」，只放**个人维度**动作。
  //   以下**市场治理**动作已全部移出本菜单，统一收归「插件市场」页：
  //     · 全局上架 / 全局下架（set_enabled，影响所有用户）
  //     · 撤回上架（withdraw_share，public → personal，仅市场管理员）
  //     · 通过上架审核（review_share，仅市场管理员）
  //   此前这些混在个人能力卡片里，既让普通作者看到点了必 403 的按钮，
  //   也让「上下架该在哪操作」变得含糊（市场治理散落在消费侧）。
  //   可见范围的高频动作（分享到市场 / 撤回申请）已提到卡片主区，此处不再重复。
  const stItems = [];
  if (x.can_unpublish_self) stItems.push(`<div onclick="capUnpublish('${esc(pid)}','${page}')">取消发布（回到草稿）</div>`);
  // 2026-09-17：停用后的恢复入口（此前个人能力停用即成"死胡同"）。
  //   个人能力被停用（status=disabled）：作者可 publish_self 恢复（disabled→published 合法流转）。
  //   已上架能力被全局停用 → 属市场治理，去「插件市场」恢复（本页不再提供）。
  if (x.status === 'disabled' && x.scope === 'personal' && x.is_mine)
    stItems.push(`<div onclick="capPublish('${esc(pid)}','${page}')">▶ 发布（恢复可用）</div>`);
  if (stItems.length) { items.push('<div class="sep"></div>'); Array.prototype.push.apply(items, stItems); }

  return `<div style="position:relative;">
    <button class="btn sm ghost" onclick="capMenuToggle(event,'${menuId}')">更多 ▾</button>
    <div class="capmenu" id="${menuId}" style="display:none;">${items.join('')}</div>
  </div>`;
}

function capMenuToggle(ev, menuId) {
  if (ev) { ev.stopPropagation(); }
  const m = document.getElementById(menuId);
  if (!m) return;
  const wasOpen = m.style.display !== 'none';
  document.querySelectorAll('.capmenu').forEach(function (x) { x.style.display = 'none'; });
  m.style.display = wasOpen ? 'none' : 'block';
}
document.addEventListener('click', function () {
  document.querySelectorAll('.capmenu').forEach(function (m) { m.style.display = 'none'; });
});

/* ── 新建 / 编辑：路由回原有的按类型专用表单（2026-09-16 恢复）──
 * 背景：原设计各类型有专属表单 —— 技能（含 zip 上传）、工具/MCP、Agent（角色 /
 * 模型 / 能力绑定 / 团队编排）、提示词。此前被统一成裸 JSON 清单表单，是功能退化。
 * 这里按类型接回原表单，并把「编辑」经 manifest.legacy_ref 映射到旧表记录，
 * 从而复用那一整套交互与校验。
 */
function capBuildAddMenu(page) {
  const box = document.getElementById('cap-' + page + '-addmenu');
  if (!box) return;
  const t = _capS(page).type || 'skill';
  const items = [];
  if (t === 'agent') {
    items.push(`<div class="has-hint" onclick="capDirect('openAgentForm')">＋ 新建 Agent<span class="hint">角色 / 模型 / 能力绑定 / 团队编排</span></div>`);
  } else if (t === 'skill') {
    items.push(`<div class="has-hint" onclick="capDirect('openSkillForm')">＋ 创建技能<span class="hint">表单编辑：名称 / 描述 / 指令正文</span></div>`);
    items.push(`<div class="has-hint" onclick="capZipClick()">📦 上传技能包（.zip）<span class="hint">解析 frontmatter → 预览确认 → 入库</span></div>`);
  } else if (t === 'tool' || t === 'mcp') {
    items.push(`<div class="has-hint" onclick="capDirect('showToolForm',null)">＋ 新增工具<span class="hint">MCP / HTTP 端点</span></div>`);
  } else if (t === 'prompt') {
    items.push(`<div onclick="capDirect('showModal','prompt')">＋ 新建提示词</div>`);
  }
  items.push('<div class="sep"></div>');
  items.push(`<div class="has-hint" onclick="capDirect('openUnifiedPluginForm')">＋ 高级：插件清单（JSON）<span class="hint">id / version / 权限 / 依赖声明</span></div>`);
  box.innerHTML = items.join('');
}

function capAddToggle(ev) {
  if (ev) ev.stopPropagation();
  const m = document.getElementById('cap-' + _capCur + '-addmenu');
  if (!m) return;
  const wasOpen = m.style.display !== 'none';
  document.querySelectorAll('.capmenu').forEach(function (x) { x.style.display = 'none'; });
  m.style.display = wasOpen ? 'none' : 'block';
}

/* 调用项目原有的全局表单函数（保持其交互与校验不变） */
function capDirect(fn) {
  const args = Array.prototype.slice.call(arguments, 1);
  const f = window[fn];
  document.querySelectorAll('.capmenu').forEach(function (x) { x.style.display = 'none'; });
  if (typeof f !== 'function') { toast('该入口不可用：' + fn); return; }
  f.apply(null, args);
}

/* 技能包上传：复用原有的 uploadSkill（解析 → 预览确认 → 入库） */
function capZipClick() {
  document.querySelectorAll('.capmenu').forEach(function (x) { x.style.display = 'none'; });
  const el = document.getElementById('cap-zip-input');
  if (!el) { toast('上传入口不可用'); return; }
  el.value = '';
  el.click();
}
async function capDoUploadSkill(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const page = _capCur;
  try {
    if (typeof uploadSkill === 'function') {
      // uploadSkill 仅读 files[0] 与 value，传入同形对象即可复用其全流程
      await uploadSkill({ files: [file], value: '' });
    } else {
      toast('上传功能不可用');
    }
  } catch (e) {
    toast('上传失败：' + (e.message || e));
  }
  input.value = '';
  // 原有流程结束后刷新能力中心，让新技能立刻出现在「我安装的」
  setTimeout(function () { capLoad(page); }, 900);
}

/* 编辑：优先走原有专用表单（经 legacy_ref 映射到旧表记录），
 * 无映射（市场安装的副本 / 纯插件能力）则回退到插件清单表单。 */
function capEdit(pid) {
  const s = _capSt[_capCur] || {};
  const all = s.mine || [];
  let x = null;
  for (let i = 0; i < all.length; i++) {
    if (_capPid(all[i]) === pid) { x = all[i]; break; }
  }
  const _route = { skills: 'editSkill', agents: 'editAgent', tools: 'showToolForm',
                   mcp_servers: 'editMCP', prompts: 'editPrompt' };
  // ★ 2026-11-20：当前视图没加载该条目时（例如从「Skills 列表」卡进来的），
  //   现取插件详情再路由。旧实现在这种情况下 rt 为空 ⇒ 掉到「插件清单 JSON 表单」，
  //   用户点"编辑技能"却要填 manifest —— 同一动作两种表单，属状态-动作不闭环。
  if (!x) {
    api('/api/plugins/' + encodeURIComponent(pid)).then(function (d) {
      const rt = (d && d.manifest && d.manifest.runtime) || {};
      const route = _route[rt.legacy_table];
      const lid = parseInt(rt.legacy_id, 10);
      if (route && typeof window[route] === 'function' && lid > 0) window[route](lid);
      else openUnifiedPluginForm(pid);
    }).catch(function () { openUnifiedPluginForm(pid); });
    return;
  }
  // 映射键：manifest.runtime.legacy_table / legacy_id（迁移与同步桥共用的同一把钥匙）
  const rt = (x && x.manifest && x.manifest.runtime) || {};
  const route = _route[rt.legacy_table];
  const lid = parseInt(rt.legacy_id, 10);
  if (route && typeof window[route] === 'function' && lid > 0) {
    window[route](lid);
    return;
  }
  openUnifiedPluginForm(pid);
}

/* 发布态文案（唯一来源）。
 * ★ 2026-11-20：必须与「个人安装态」措辞区分开。旧实现两处都用「已启用/已停用」，
 *   于是同一条草稿卡片上出现「已停用（发布态）… 已启用（个人态）」两个相反的词，
 *   用户无法判断到底能不能用。分开写之后，再加一个 consumable 综合结论。
 */
function _capStatusText(x) {
  const m = { draft: '草稿（未发布）', rejected: '已驳回', submitted: '待审核',
              published: '已发布', disabled: '已停用（不可消费）', removed: '已删除' };
  return m[x.status] || x.status_label || x.status || '';
}

/* 紧凑能力卡片（对齐 WorkBuddy：图标 + 名称 + 动作 + 2 行描述 + 底部元信息） */
function capCard(x, tab, page) {
  const t = x.type || 'skill';
  const m = CAP_META[t] || { abbr: '?', label: t, color: '#5F5E5A' };
  const label = capLabel(x);
  const desc = x.description || (x.manifest && x.manifest.description) || '—';
  const pid = _capPid(x);
  const installed = !!x.installed;
  const enabled = x.enabled !== false;
  const isBuiltin = !!x.is_builtin;
  const isMine = !!x.is_mine;
  const canEdit = !!x.can_edit;
  // P1-7：可见范围（scope）与可用性（status）正交 —— 分开呈现，避免用户混淆
  const pubShare = !!x.is_public;
  const pendingShare = !!x.pending_share;

  // [1] 动作区（2026-09-17 定稿：消费区只有「个人可用」单区，市场目录分支已删除）
  //   入口归位（2026-09-17 第二轮）：本区只放**个人维度**动作，按状态动态裁剪 ——
  //     发布 · 分享到市场 · 撤回申请 · 停用/启用 · 卸载 · 编辑 · 删除
  //   此前「分享/卸载/删除」统一埋在「更多 ▾」菜单里，用户在主区看不到入口，
  //   反而菜单里混着一堆市场管理员才有的治理动作（全局上下架/撤回上架/审核）。
  let actions = '';
  // ★ 2026-11-06：合并进来的 mcp_servers 卡片过去**没有测试入口**，用户无从判定是否生效。
  //   testMCP（30-agents.js）已改为走 /discover：initialize + tools/list + 刷新工具元数据。
  if (x._from_server && x.legacy_id) {
    actions += `<button class="btn sm ghost" onclick="testMCP(${Number(x.legacy_id)})" title="连通测试 + 动态发现：刷新在线状态、工具清单与参数契约">🔌 测试/发现</button>`;
  }
  // 草稿/驳回态优先给「发布」——不发布就不可被 AI 消费，这是最需要的下一步
  if (x.can_publish_self) {
    actions = `<button class="btn sm" onclick="capPublish('${esc(pid)}','${page}')">发布</button>`;
  }
  // 已发布 + 未上架：分享入口提到主区（需审核，审核期间照常可用）
  if (x.can_share) {
    actions += `<button class="btn sm" title="提交给管理员审核；通过后全员可见、可安装" onclick="capShare('${esc(pid)}','${page}')">⬆ 分享到市场</button>`;
  }
  // 申请上架中：给出撤回出口（此前只能等管理员裁决）
  if (x.can_cancel_share) {
    actions += `<button class="btn sm ghost" title="撤回后仅自己可见，能力照常可用" onclick="capCancelShare('${esc(pid)}','${page}')">↩ 撤回申请</button>`;
  }
  // ★ 2026-11-20 修状态-动作闭环：个人启停**只在「已发布」时才有意义**。
  //   旧实现无条件渲染 `${enabled ? '停用' : '启用'}`：
  //   草稿态/被下架态（status≠published）的条目根本不可消费，却挂一个「停用」按钮 ——
  //   用户点下去什么也不会变（它本来就不可用），而真正需要的"发布"入口又按权限藏着。
  //   实测样本：新建技能 status=disabled、installed_enabled=True ⇒
  //   卡片写「已停用」+ 按钮写「停用」+ 没有任何「启用/发布」入口（用户本轮投诉）。
  const published = (x.status === 'published');
  if (published) {
    actions += `<button class="btn sm ghost" title="只影响你自己：停用后你不再消费它，他人不受影响" onclick="capSetInstalled('${esc(pid)}',${enabled ? 'false' : 'true'},'${page}')">${enabled ? '停用' : '启用'}</button>`;
  }
  // 2026-09-17：从市场安装的副本直接给卸载入口（内置除外——后端拒绝卸载内置，引导用停用）；
  // 卸载后插件市场对应条目的「安装」入口自动恢复，可随时重装。
  // 2026-09-29（动作语义二轮定稿）：来源决定按钮 —— 市场安装的（非自己创建）→ 卸载；
  // 自己创建的 → 删除（不走卸载，避免"作者卸载自己的作品"这种别扭路径）。
  if (x.installed && !x.is_builtin && !x.is_mine) {
    actions += `<button class="btn sm ghost" onclick="capUninstall('${esc(pid)}','${page}')">卸载</button>`;
  }
  if (canEdit) actions += `<button class="btn sm ghost" onclick="capEdit('${esc(pid)}')">编辑</button>`;
  // 自己创建的能力：主区给删除入口（此前只能从「更多」里找到）。
  // 2026-09-29：市场安装的（installed 且非本人创建）**不提供删除** —— 本体属作者/市场，
  // 想移除用「卸载」；删除留给作者本人与管理员治理（can_delete 本身已含这层权限）。
  const fromMarket = x.installed && !x.is_mine;
  if (x.can_delete && !isBuiltin && !fromMarket) {
    actions += `<button class="btn sm ghost" style="color:var(--red,#A32D2D);" title="移除该能力本体（软删除，审计保留）" onclick="capDelete('${esc(pid)}','${esc(String(label).replace(/'/g, ''))}','${page}')">删除</button>`;
  }
  actions += capMoreBtn(x, page);

  // 子 Agent 标注隶属的主 Agent
  let belong = '';
  if (t === 'agent' && (x.manifest && x.manifest.agent_role) === 'sub') {
    const ps = (x.manifest.parent_agents) || [];
    if (ps.length) {
      belong = `<span title="${esc(ps.join('\n'))}">隶属 ${esc(_capNameByPid(page, ps[0]))}${ps.length > 1 ? ' 等 ' + ps.length + ' 个' : ''}</span>`;
    }
  }

  // ★ 2026-11-06：MCP 服务卡片的「生效判据」一次给全 —— 在线状态 / 工具数 / 巡检时间 / 凭据掩码。
  //   凭据只显示 ****** 与长度（后端 headers_masked 已脱敏），既能确认「填上了」，又不泄露明文。
  let mcpMeta = '';
  if (x._from_server) {
    const hm = x.headers_masked || {};
    const hk = Object.keys(hm);
    const cred = hk.length
      ? hk.map(k => esc(k) + ': ******(' + ((hm[k] && hm[k].len) || 0) + '位)').join(' · ')
      : '未配置认证头';
    const online = !!x.mcp_online;
    const lc = String(x.last_check || '').slice(5, 19) || '未巡检';
    const err = String(x.last_error || '').replace(/[<>"']/g, '').slice(0, 80);
    mcpMeta = `<div class="capmcpmeta" style="margin:2px 0 6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:var(--mut);">
      <span class="capbadge ${online ? 'on' : 'off'}" title="来自 mcp_servers.status（健康巡检写入）">${online ? '● 在线' : '○ 离线'}</span>
      <span title="最近一次健康巡检">巡检 ${esc(lc)}</span>
      <span>${Number(x.tool_count || 0)} 工具</span>
      <span title="认证头已脱敏；保存成功后仍以掩码确认已填写">🔑 ${cred}</span>
      ${online ? '' : `<span style="color:var(--red,#A32D2D)" title="${esc(err)}">${esc(err || '鉴权/连通未通过')}</span>`}
    </div>`;
  }

  return `<div class="capcard ${tab === 'mine' ? 'mine' : ''} ${tab === 'mine' && !enabled ? 'dis' : ''}">
    <div class="capcard-h">
      <div class="capico" style="background:${m.color}22;color:${m.color};font-weight:500;">${m.abbr}</div>
      <div class="captit" title="${esc(label)}">${esc(label)}</div>
    </div>
    <div class="capdesc">${esc(desc)}</div>
    ${mcpMeta}
    <div class="capmeta">
      <span>${isBuiltin ? '内置' : (isMine ? '我创建' : '他人共享')}</span>
      <span>v${esc(x.current_version || '—')}</span>
      <span class="${x.status === 'disabled' ? 'warn' : ''}" title="发布态：决定「能否被 AI 消费」">${x._from_server ? 'MCP 服务' : esc(_capStatusText(x))}</span>
      ${!isBuiltin ? `<span title="可见范围 —— 与「能否被 AI 消费」无关">${pubShare ? '已上架市场' : (pendingShare ? '申请上架中' : '仅自己')}</span>` : ''}
      ${belong}
      ${installed ? `<span class="capbadge ${enabled ? 'on' : 'off'}" title="个人安装态 —— 仅影响你自己，不影响他人；与上面的发布态是两个维度">${enabled ? '个人已启用' : '个人已停用'}</span>` : ''}
      <span class="capbadge ${x.consumable ? 'on' : 'off'}" title="综合判定 = 已发布 且 个人已启用 且 已安装；这是 AI 会不会真的用它的唯一判据">${x.consumable ? 'AI 可用' : 'AI 不可用'}</span>
    </div>
    <div class="capacts">${actions}</div>
  </div>`;
}

/* ── 动作实现 ───────────────────────────────────────────── */

/* 复制副本：走统一插件接口（旧 copySkill/copyMCP 依赖旧表 id，新轨不可用） */
async function capCopy(pid, page) {
  const d = await api('/api/plugins/' + encodeURIComponent(pid)).catch(function () { return {}; });
  if (!d || !d.manifest) { toast('读取能力信息失败'); return; }
  const mf = JSON.parse(JSON.stringify(d.manifest));
  const sfx = String(Date.now()).slice(-6);
  mf.name = (mf.name || 'copy') + '-' + sfx;
  mf.id = (mf.id || 'com.mbse.copy') + '-' + sfx;
  const body = { manifest: mf };
  if (d.skill_md) body.skill_md = d.skill_md;
  if (d.mcp_server) body.mcp_server = d.mcp_server;
  const r = await api('/api/plugins', { method: 'POST', body: JSON.stringify(body) })
    .catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('复制失败：' + r.error); return; }
  toast('已复制为副本（草稿）');
  capLoad(page);
}

/* ── 发布 / 取消发布（P1-7：只改「能否被 AI 消费」，**不动可见范围**，免审核）── */
async function capPublish(pid, page) {
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/publish',
    { method: 'POST', body: '{}' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('发布失败：' + r.error); return; }
  toast('已发布，可被 AI 消费' + (r.scope === 'public' ? '（已上架市场）' : '（仅自己可见）'));
  capLoad(page);
}

async function capUnpublish(pid, page) {
  if (!(await confirmDialog('取消发布？\n\n取消后该能力回到草稿，AI 将不再消费它。\n（可见范围不受影响）'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/unpublish',
    { method: 'POST', body: '{}' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('取消失败：' + r.error); return; }
  toast('已取消发布（回到草稿）');
  capLoad(page);
}

/* ── 分享 / 撤回上架（P1-7：只改可见范围，**不影响可用性**）── */
async function capShare(pid, page) {
  if (!(await confirmDialog('分享到公共市场？\n\n· 提交后需管理员审核\n· 审核期间该能力对你仍照常可用\n· 通过后全团队可见、可安装'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/share',
    { method: 'POST', body: '{}' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('提交失败：' + r.error); return; }
  toast('已提交上架申请（审核期间照常可用）');
  capLoad(page);
}

async function capWithdrawShare(pid, page) {
  if (!(await confirmDialog('撤回上架？\n\n撤回后仅自己可见、可安装；\n该能力仍可被你正常使用（可用性不受影响）。'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/unshare',
    { method: 'POST', body: '{}' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('撤回失败：' + r.error); return; }
  toast('已撤回上架（仅自己可见）');
  capLoad(page);
}

/* 撤回上架申请：pending_public → personal（作者本人，免审核）。
 * 与 capWithdrawShare 打同一个 /unshare 端点，后端按 scope 分派到 cancel_share / withdraw_share ——
 * 分开命名只为让确认文案贴合语境（前者「还没上架，撤销申请」，后者「已上架，收回可见性」）。 */
async function capCancelShare(pid, page) {
  if (!(await confirmDialog('撤回上架申请？\n\n· 撤回后仅自己可见、可安装\n· 该能力对你仍照常可用（可用性不受影响）\n· 之后可随时重新提交申请'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/unshare',
    { method: 'POST', body: '{}' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('撤回失败：' + r.error); return; }
  toast('已撤回上架申请（可随时重新申请）');
  capLoad(page);
}

/* 卸载：移出我的清单（能力本体仍在市场，可重装） */
async function capUninstall(pid, page) {
  if (!(await confirmDialog('卸载 = 把该能力移出你的清单（能力本体仍在市场，随时可重装）。\n确定卸载？'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/uninstall',
    { method: 'POST', body: '{}' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('卸载失败：' + r.error); return; }
  toast('已卸载');
  capLoad(page);
}

/* 个人级启停：只影响自己（不同于"全局下架"） */
async function capSetInstalled(pid, enabled, page) {
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/installed-enabled',
    { method: 'PUT', body: JSON.stringify({ enabled: !!enabled }) }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('操作失败：' + r.error); return; }
  toast(enabled ? '已为你启用' : '已为你停用（不影响他人）');
  capLoad(page);
}

/* 全局上架/下架（管理员）：影响所有用户 */
async function capGlobalToggle(pid, enabled, page) {
  if (!(await confirmDialog((enabled ? '全局上架' : '全局下架') + '会立即对所有用户生效。\n确定继续？'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid) + '/enabled',
    { method: 'PUT', body: JSON.stringify({ enabled: !!enabled }) }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('操作失败：' + r.error); return; }
  toast(enabled ? '已全局上架' : '已全局下架');
  capLoad(page);
}

/* 删除：移除能力本体（软删，审计保留）。内置能力由后端与 UI 双重禁止。 */
async function capDelete(pid, name, page) {
  if (!(await confirmDialog('删除 = 移除该能力本体（软删除，审计保留），所有已安装者将不可用。\n确定删除「' + name + '」？'))) return;
  const r = await api('/api/plugins/' + encodeURIComponent(pid),
    { method: 'DELETE' }).catch(function (e) { return { error: e.message }; });
  if (r.error) { toast('删除失败：' + r.error); return; }
  toast('已删除');
  capLoad(page);
}

/* 一级页切换时挂载（由 loadStudioTab / switchStLibTab 调用） */
function capMountForTab(id) {
  const page = id === 'st-mcp' ? 'st-mcp' : id;
  if (CAP_PAGES[page]) { capMount(page); return true; }
  return false;
}
