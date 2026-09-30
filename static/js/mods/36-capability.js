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
    '.captabs span{padding:5px 14px;border-radius:6px;cursor:pointer;font-size:12px;color:var(--mut,#5a6678);}',
    '.captabs span.on{background:var(--bg-card,#fff);color:var(--blue-d,#0C447C);font-weight:500;box-shadow:0 1px 2px rgba(0,0,0,.06);}',
    '.captabs span .tag{margin-left:5px;font-size:10px;}',
    '.capchips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;}',
    '.capchips span{padding:3px 10px;border-radius:20px;font-size:11.5px;cursor:pointer;border:1px solid var(--line,#d5dce6);color:var(--mut,#5a6678);background:transparent;}',
    '.capchips span.on{background:var(--blue-d,#0C447C);border-color:var(--blue-d,#0C447C);color:#fff;}',
    '.capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:10px;}',
    '.capcard{position:relative;background:var(--bg-card,#fff);border:1px solid var(--line,#d5dce6);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px;transition:border-color .15s;}',
    '.capcard:hover{border-color:var(--blue,#185FA5);}',
    '.capcard.mine{border-left:3px solid var(--green,#3B6D11);}',
    '.capcard.dis{opacity:.55;}',
    '.capcard-h{display:flex;align-items:center;gap:9px;}',
    '.capico{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;background:var(--bg2,#f0f3f8);}',
    '.captit{flex:1;min-width:0;font-size:13px;font-weight:500;line-height:1.35;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.capdesc{font-size:12px;color:var(--mut,#5a6678);line-height:1.55;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:37px;}',
    '.capmeta{display:flex;gap:10px;font-size:11px;color:var(--mut,#8a96ad);border-top:1px dashed var(--line,#e9edf3);padding-top:6px;flex-wrap:wrap;}',
    '.capmeta b{font-weight:500;color:var(--blue-d,#0C447C);}',
    '.capmeta .warn{color:var(--orange,#BA7517);}',
    '.capempty{grid-column:1/-1;padding:36px;text-align:center;color:var(--mut,#8a96ad);font-size:12px;}',
    '.cappane{display:none;} .cappane.on{display:block;}',
    '.capacts{display:flex;gap:6px;align-items:center;justify-content:flex-end;margin-top:8px;padding-top:8px;border-top:1px solid var(--line,#e9edf3);flex-wrap:wrap;}',
    '.capacts .btn.sm{padding:3px 10px;font-size:11.5px;}',
    '.capok{font-size:11px;color:var(--green,#3B6D11);padding:0 6px;}',
    /* 启用/停用 徽标与筛选 chips（2026-09-17：市场区此前看不到启用状态，也没有状态筛选） */
    '.capbadge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:10.5px;font-weight:500;line-height:1.6;}',
    '.capbadge.on{background:rgba(59,109,17,.13);color:var(--green,#3B6D11);}',
    '.capbadge.off{background:rgba(186,117,23,.15);color:var(--orange,#BA7517);}',
    '.capchips .capsep{width:1px;align-self:stretch;background:var(--line,#d5dce6);margin:2px 4px;padding:0;border:none;cursor:default;}',
    '.capmenu{position:absolute;right:0;top:26px;z-index:80;min-width:148px;background:var(--bg-card,#fff);border:1px solid var(--line,#d5dce6);border-radius:8px;box-shadow:0 6px 18px rgba(20,35,60,.14);padding:4px;}',
    '.capmenu div{padding:6px 10px;font-size:12px;border-radius:6px;cursor:pointer;white-space:nowrap;color:var(--ink,#1a2332);}',
    '.capmenu div:hover{background:var(--bg-hover,#eef4ff);}',
    '.capmenu .sep{height:1px;background:var(--line,#e9edf3);margin:4px 2px;padding:0;}',
    '.capmenu div.has-hint{white-space:normal;max-width:236px;}',
    '.capmenu div .hint{display:block;font-size:10.5px;color:var(--mut,#8a96ad);margin-top:1px;line-height:1.45;}',
    /* [2] Agent 主子分栏 */
    '.capgroup{grid-column:1/-1;}',
    '.capgroup + .capgroup{margin-top:14px;}',
    '.capgroup-h{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:500;color:var(--ink,#1a2332);margin:0 0 8px;}',
    '.capgroup-h .cnt{font-size:11px;color:var(--mut,#8a96ad);font-weight:400;}',
    '.capgroup-h .ln{flex:1;height:1px;background:var(--line,#e9edf3);}',
    '.capgroup .capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:10px;}',
    '.caplegend{font-size:11px;color:var(--mut,#8a96ad);line-height:1.8;}',
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
  if (!_capSt[page]) _capSt[page] = { type: type, q: '', cat: '', st: '', mine: [] };
  if (type && !CAP_PAGES[page].includes(_capSt[page].type)) _capSt[page].type = type;
  return _capSt[page];
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
      <input id="cap-${page}-q" placeholder="搜索名称 / 描述…" style="border:1px solid var(--line,#d5dce6);border-radius:6px;padding:4px 10px;font-size:12px;width:190px;" oninput="capSearch('${page}',this.value)">
      <button class="btn sm ghost" onclick="capRefresh('${page}')">刷新</button>
      <span style="flex:1"></span>
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
      <button class="btn sm ghost" onclick="capToggleManage('${page}')" id="cap-${page}-mbtn">管理能力（审核 / 范围 / 下架）</button>
    </div>
    <div class="subpage" id="cap-${page}-manage" style="margin-top:12px;">
      <div style="font-size:11.5px;color:var(--mut,#8a96ad);padding:8px 0;">治理入口：上架审核、范围分配、全市场插件管理见「插件市场」。<button class="btn sm ghost" onclick="capJumpManage()">前往</button></div>
    </div>`;
  capHideSiblings(box);
  await capLoad(page);
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
function capToggleManage(page) {
  const el = document.getElementById('cap-' + page + '-manage');
  if (el) el.style.display = (el.style.display === 'block') ? 'none' : 'block';
}
function capJumpManage() {
  const t = document.querySelector('[data-pane="st-market"]');
  if (t) t.click();
}

/* 加载数据：个人可用 = 我创建的 + 我安装的（/api/plugins/mine 一次拉取；2026-09-17 定稿，
 * 消费区不再展示全市场目录 —— 市场目录与治理统一在「插件市场」页） */
async function capLoad(page) {
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
  await _pre;
  capRender(page);
}

function capRender(page) {
  _capCur = page;
  const s = _capS(page);
  const items = s.mine;
  const grid = document.getElementById('cap-' + page + '-grid');
  const cats = document.getElementById('cap-' + page + '-cats');
  const sum = document.getElementById('cap-' + page + '-sum');
  if (!grid) return;

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
  } else if (s.type === 'agent') {
    // [2] Agent 主子分栏：上下两栏
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
  // 映射键：manifest.runtime.legacy_table / legacy_id（迁移与同步桥共用的同一把钥匙）
  const rt = (x && x.manifest && x.manifest.runtime) || {};
  const route = { skills: 'editSkill', agents: 'editAgent', tools: 'showToolForm',
                  mcp_servers: 'editMCP', prompts: 'editPrompt' }[rt.legacy_table];
  const lid = parseInt(rt.legacy_id, 10);
  if (route && typeof window[route] === 'function' && lid > 0) {
    window[route](lid);
    return;
  }
  openUnifiedPluginForm(pid);
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
  actions += `<button class="btn sm ghost" onclick="capSetInstalled('${esc(pid)}',${enabled ? 'false' : 'true'},'${page}')">${enabled ? '停用' : '启用'}</button>`;
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

  return `<div class="capcard ${tab === 'mine' ? 'mine' : ''} ${tab === 'mine' && !enabled ? 'dis' : ''}">
    <div class="capcard-h">
      <div class="capico" style="background:${m.color}22;color:${m.color};font-weight:500;">${m.abbr}</div>
      <div class="captit" title="${esc(label)}">${esc(label)}</div>
    </div>
    <div class="capdesc">${esc(desc)}</div>
    <div class="capmeta">
      <span>${isBuiltin ? '内置' : (isMine ? '我创建' : '他人共享')}</span>
      <span>v${esc(x.current_version || '—')}</span>
      <span class="${x.status === 'disabled' ? 'warn' : ''}">${esc(x.status_label || x.status || '')}</span>
      ${!isBuiltin ? `<span title="可见范围 —— 与「能否被 AI 消费」无关">${pubShare ? '已上架市场' : (pendingShare ? '申请上架中' : '仅自己')}</span>` : ''}
      ${belong}
      ${installed ? `<span class="capbadge ${enabled ? 'on' : 'off'}" title="个人启用状态 —— 仅影响你自己，不影响他人">${enabled ? '已启用' : '已停用'}</span>` : ''}
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
