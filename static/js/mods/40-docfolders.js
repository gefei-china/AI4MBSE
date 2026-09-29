/* 文档目录树（左栏「位置区 / 结构目录 / 回收站」）—— 2026-09-27 交互重构；2026-09-28 IA 收敛
 *
 * 依据：docs/文档库基于文件的管理-评估与优化方案-20260921.md §4.3（合并版 UX）
 *   + 2026-09-28 用户决策（覆盖 §4.3 的「智能视图分组」设计）：
 *   - 左栏 220px 可折叠，分三段：
 *       位置区（常驻）：📚 全部文档 / 📥 未归类（folder_id=0 兜底入口）；
 *       「结构目录」= 人工创建，文档可移入（本模块管理）；
 *       底部吸顶：🗑 回收站（唯一保留的智能视图，独立渲染在树末尾）。
 *   - 「智能视图」分组已整体移除，未入库/解析失败/未分类三个派生视图按用户决策**不保留**
 *     （对应筛选能力在中栏工具栏：#doc-status-filter / 知识类别列）。
 *   - 计数口径统一为**含子目录累计数**（doc_count_all），目录选中默认 subtree（同口径配套）。
 *   - 中栏加「所属目录」列 + 面包屑；批量移动挂到既有 #doc-batch-bar（不另起一套多选）。
 *   - 复用清单（方案 §4.4，不重复造轮子）：
 *       renderPagerBar(15-kb.js) / openPanel(02-shell.js) / confirmDialog+toast+esc+api(01-core.js)
 *       / docContextMenu 的菜单外壳与 closeDocCtxMenu 关闭钩子(09-impact.js)
 *       / 目录树视觉沿用本体树 .ont-tnd 的层级语言（kb.css）
 *   - 铁律：只用现有 CSS 变量（--line/--blue-l/--blue/--blue-d/--mut/--red/--grn），不引任何依赖。
 *
 * ── 2026-09-27 本轮补齐的能力（此前完全缺失或与既有约定冲突）──
 *   ① 真折叠展开（此前只有 ▸/⤷ 符号 + padding 缩进，无折叠态）；
 *   ② **行内改名 / 就地新建**（此前用原生 prompt()——本仓 01-core.js 已明确"替代原生 confirm()"，
 *      prompt 同理：同步阻塞、样式脱节、自动化环境直接卡死）；
 *   ③ 右键上下文菜单；④ 文件夹移动（改父子层级）；⑤ 同级排序（sort 列此前恒 0 未被使用）；
 *   ⑥ 拖拽（文档行 → 目录、文件夹 → 文件夹）；⑦ 键盘流真实可达（见下）。
 *   ⑧（09-28）新建根目录入口唯一化：只剩标题栏 ＋（分组头 ＋ 与两处右键菜单项已移除）。
 *   ⑨（09-28）行操作主入口改为行尾「⋮」按钮（hover/聚焦显形，点击锚定展开）：
 *      右键降级为兼容通道（同一份菜单内容）；行内 ＋/✕ 图标移除（动作并入 ⋮ 菜单）。
 *
 * ── 三个关键实现决定（都是踩过坑后的选择）──
 *   1) **事件委托**，不给每个节点挂 inline onclick。目录名是用户输入，含单引号/双引号会让
 *      `onclick="fn('${name}')"` 直接语法崩（既有模块里 esc() 只转 &<> 不转引号）。
 *      委托到永不重建的 #doc-folders 容器 → 名字里有什么字符都安全，且键盘流集中一处。
 *   2) **目录筛选走服务端**（folder_id + folder_scope，含子目录展开在服务端做），
 *      **回收站视图走客户端**（deprecated 是派生谓词，列表本身已是全量客户端分页，口径一致）。
 *   3) **编辑期间禁止重建 DOM**（`renderDocFolders` 见到 _dftEdit 直接 return）：
 *      `loadDocs()` 会被勾选复选框等高频动作触发，而它是全量 `innerHTML` 重建 ——
 *      不设这道闸，行内输入框和已敲进去的字符会被**静默冲掉**（表现为"打一半字突然消失"）。
 *      ⚠️ 键盘流同理：节点是 `tabindex="-1"`，必须由 `docRestoreFocus()` 主动 focus()，
 *      否则容器上的 keydown 委托里 `ev.target.closest('[data-act]')` 恒为 null，
 *      Enter/F2/Delete 三个分支**永不命中**（旧版就是这个状态：只声明了快捷键，实际只 Escape 有效）。
 */

// ── 状态（_doc* 与 20-docs.js 同域，声明也在那里，避免 TDZ）──
// _docFolders   目录树缓存（含 doc_count / doc_count_all）
// _docFolderId  当前选中目录（'' = 全部；0 = 未归类；>0 = 具体目录）
// _docFolderScope  'self' | 'subtree'
// _docSmartView  当前智能视图 key（'' = 无），与 _docFolderId 互斥

// 智能视图（2026-09-28 收敛为**单项**）。
// 依据：用户要求「智能视图区域移除，只保留『回收站』放在最下方，吸顶展示」。
//   · 「未入库 / 解析失败 / 未分类」三项**已删除**（用户决策：不保留）。
//     对应的筛选能力并未消失，全部仍在**中栏工具栏**（功能零损失的替换入口）：
//       未入库/解析失败 → `#doc-status-filter`（stored / failed 选项）
//       未分类          → `#doc-cat-filter`（空类别）/ 列表「知识类别」列筛选
//   · 「回收站」保留，但它不再是"智能视图组"的成员，而是**底部独立常驻项**（见 renderDocFolders）。
//   · 「未归类」也是**位置区常驻项**（data-fid="0"）—— 与回收站同为"兜底入口"，
//     一个管"还没归档"、一个管"已下线"。两者都不是目录，都不可拖入/改名/删除。
// ⚠️ 本数组**只保留渲染所需的 key/icon/label/test**，不再有"智能视图分组"这一 IA 概念；
//    保留数组形态是为了 `_docSmartView` 的派生过滤链路（loadDocs/renderDocCrumb）零改动。
const DOC_SMART_VIEWS = [
  { key:'trash', icon:'🗑', label:'回收站', test:d=>(d.lifecycle_status||'')==='deprecated' },
];

let _docFoldersBound = false;
let _dftCollapsed = new Set();   // 折叠的目录 id（**默认全展开**：不落 localStorage，避免页面间状态串味）
let _dftEdit = null;             // 进行中的行内编辑 {mode:'rename'|'create', fid, parentId}
let _dftFocus = null;            // roving 焦点键：'a'（全部文档）/ 'f:<id>' / 's:<smartKey>'
let _dftDragSrc = '';            // 拖拽源（dragover 阶段读不到 dataTransfer，只能用模块变量）
let _dftDragging = false;
let _dftSuppressUntil = 0;       // 拖拽结束后的 click 抑制窗口
let _dftDocDragBound = false;

// ── 加载与渲染 ──
async function loadDocFolders() {
  const box = document.getElementById('doc-folders');
  if(!box) return;
  try {
    const t = await api('/api/doc-folders');
    _docFolders = (t && t.folders) || [];
    _docUncategorized = (t && t.uncategorized) || 0;
    _docTotalAll = (t && t.total_docs) || 0;
    renderDocFolders();
  } catch(e) {
    box.innerHTML = `<div style="padding:8px;font-size:11px;color:var(--red);">目录加载失败：${esc(e.message)}</div>`;
  }
}

function docFolderName(fid) {
  if(!fid) return '';
  const f = (_docFolders||[]).find(x=>x.id===fid);
  return f ? f.name : '';
}

// 同级顺序的权威口径 = 服务端 ORDER BY parent_id, sort, name（list_all 已保序，这里只按 parent 分组）
function docFolderKids(pid) { return (_docFolders||[]).filter(f=>f.parent_id===pid); }
function docFolderHasKids(fid) { return (_docFolders||[]).some(x=>x.parent_id===fid); }
function docFolderDepth(fid) {
  const f = (_docFolders||[]).find(x=>x.id===fid);
  if(!f) return 0;
  return Math.max(0, String(f.path||'').split('/').filter(Boolean).length - 1);
}

function renderDocFolders() {
  const box = document.getElementById('doc-folders');
  if(!box) return;
  docBindFolderEvents();
  docBindDocDrag();
  if(_dftEdit) return;   // ⚠️ 编辑中不重建（见文件头 §3：重建会静默冲掉输入框内容）

  const hadFocus = box.contains(document.activeElement);

  const nodeHtml = (f, depth) => {
    const kids = docFolderKids(f.id);
    const collapsed = _dftCollapsed.has(f.id);
    const sel = !_docSmartView && _docFolderId !== '' && Number(_docFolderId) === f.id;
    // 计数字段口径统一（2026-09-28 用户决策）：**一律显示含子目录累计数**。
    // 旧实现渲染 "2/4"（直接数/累计数），两个数字并列会让用户猜哪个是"里面的文件数"，
    // 且点进父目录默认 self 视图只看到直接数 → "数字 4、列表 2 条"的自相矛盾。
    // 现在只有一个数字 = doc_count_all（含子目录），并与「目录选中默认 subtree」配套（见 docSelectFolder）。
    const cnt = f.doc_count_all || 0;
    const hasKids = kids.length > 0;
    const arrow = hasKids
      ? `<span class="dft-arrow" data-act="toggle" data-fid="${f.id}" title="${collapsed?'展开':'折叠'}（也可用 ← →）">${collapsed?'▸':'▾'}</span>`
      : `<span class="dft-arrow empty" data-act="toggle" data-fid="${f.id}" title="没有子目录"></span>`;
    const cntTitle = hasKids
      ? `含子目录共 ${cnt} 份文档（本目录直接挂 ${f.doc_count} 份）`
      : `本目录挂 ${cnt} 份文档`;
    return `<div class="ont-tnd dft-row${sel?' on':''}" data-fid="${f.id}" data-act="select" data-dftkey="f:${f.id}"
        tabindex="-1" draggable="true" style="padding-left:${depth*14+22}px"
        title="${esc(f.path)}&#10;单击选中 · 双击/F2 重命名 · 拖拽可移动/排序 · Delete 删除 · 行尾 ⋮ 更多操作">`
      + arrow
      + `<span class="dft-ico">${hasKids && !collapsed ? '📂' : '📁'}</span>`
      + `<span class="dft-name">${esc(f.name)}</span>`
      + `<span class="dft-cnt" title="${esc(cntTitle)}">${cnt}</span>`
      // 行尾统一为「⋮」更多按钮（2026-09-28）：此前是 ＋/✕ 两个图标 + 右键菜单三套入口，
      // 同一批动作散在三处（＋ 建子目录 / ✕ 删目录 / 右键全集），既要记两套交互又不能发现全集。
      // 现在收敛为**唯一入口 ⋮**（hover 才显形），菜单里含全部动作。
      + `<span class="dft-more" data-act="more" data-fid="${f.id}" title="更多操作（新建子目录 / 重命名 / 移动 / 上下移 / 删除）">⋮</span>`
      + `</div>`
      + (collapsed ? '' : kids.map(c=>nodeHtml(c, depth+1)).join(''));
  };

  const structRows = docFolderKids(0).map(f=>nodeHtml(f, 0)).join('');
  const allSel = _docFolderId === '' && !_docSmartView;
  const uncSel = !_docSmartView && _docFolderId !== '' && Number(_docFolderId) === 0;

  // 「全部文档」计数（2026-09-28 口径澄清）：显示**全库总数**（服务端 total_docs），
  // 而不是当前列表条数。理由：它是"不按目录筛选"的入口，语义上就该是"库里有几份"。
  // ⚠️ 但带工具栏筛选（格式/状态/时间…）时，列表条数会**小于**这个数字 —— 这是两个口径，
  //    不是 bug。为避免用户误读为"数字对不上"，当两者不等时在计数旁加一个 `*` 与 tooltip 说明。
  const allTotal = _docTotalAll || (_docs||[]).length;
  const allShown = (_docs||[]).length;
  const allMismatch = (allTotal !== allShown);
  const allCntTitle = allMismatch
    ? `全库共 ${allTotal} 份文档；当前工具栏筛选后列表显示 ${allShown} 份（计数为全库口径，不受筛选影响）`
    : `全库共 ${allTotal} 份文档`;

  // 回收站 = 唯一保留的智能视图，**独立渲染在树末尾并吸顶**（用户要求 2026-09-28）。
  const trashView = DOC_SMART_VIEWS.find(v=>v.key==='trash');
  const trashN = trashView ? (_docs||[]).filter(trashView.test).length : 0;
  const trashSel = _docSmartView === 'trash';
  const trashRow = trashView ? `
    <div class="dft-row dft-foot${trashSel?' on':''}" data-smart="trash" data-act="smart" data-dftkey="s:trash"
        tabindex="-1" title="回收站：已下线（弃用）的文档；可在此恢复或彻底删除">
      <span class="dft-ico">🗑</span>
      <span class="dft-name">回收站</span>
      <span class="dft-cnt">${trashN}</span>
      <span class="dft-more" data-act="more" data-smart="trash" title="更多操作">⋮</span>
    </div>` : '';

  box.innerHTML = `
    <div class="ont-tnd dft-row${allSel?' on':''}" data-fid="" data-act="select" data-dftkey="a" tabindex="-1"
        style="padding-left:22px" title="显示全部文档（不按目录筛选）">
      <span class="dft-ico">📚</span><span class="dft-name">全部文档</span>
      <span class="dft-cnt" title="${esc(allCntTitle)}">${allTotal}${allMismatch?'<sup style="color:var(--amb);">*</sup>':''}</span>
      <span class="dft-more" data-act="more" data-fid="" title="更多操作">⋮</span>
    </div>
    <div class="ont-tnd dft-row${uncSel?' on':''}" data-fid="0" data-act="select" data-dftkey="f:0" tabindex="-1"
        style="padding-left:22px" title="未归类：尚未放入任何结构目录的文档&#10;（兜底入口——不整理也能被发现）">
      <span class="dft-ico">📥</span><span class="dft-name">未归类</span>
      <span class="dft-cnt" title="尚未放入任何结构目录的文档数">${_docUncategorized||0}</span>
      <span class="dft-more" data-act="more" data-fid="0" title="更多操作">⋮</span>
    </div>
    <div style="height:1px;background:var(--line);margin:4px 8px;"></div>
    <div class="dft-scroll">
      <div class="dft-group" data-dftgroup="struct" title="人工创建的结构目录：可新建子目录、改名、移动/排序、拖入文档">
        <span style="flex:1;">结构目录</span>
        <span class="dft-more" data-act="groupmore" data-dftgroup="struct" title="目录树操作（全部展开 / 全部折叠）">⋮</span>
      </div>
      ${structRows || '<div class="dft-hint">（暂无目录，点标题栏 ＋ 新建；也可以直接把文件拖进来）</div>'}
    </div>
    ${trashRow}`;

  if(hadFocus) docRestoreFocus(_dftFocus || 'a');
}

// roving 焦点：重渲染会销毁所有节点，必须主动把焦点放回同一行，否则键盘流一次操作就断
function docRestoreFocus(key) {
  const box = document.getElementById('doc-folders');
  if(!box || !key) return;
  const el = box.querySelector(`[data-dftkey="${key}"]`);
  if(el) el.focus();
}

// ── 事件委托（唯一入口，名字含引号也不会崩）──
function _dftSuppressing() { return _dftDragging || Date.now() < _dftSuppressUntil; }
function _dftKeys(box) { return Array.prototype.slice.call(box.querySelectorAll('[data-dftkey]')); }
function _dftRowKey(row) {
  if(!row) return '';
  if(row.hasAttribute('data-smart')) return 's:' + row.getAttribute('data-smart');
  const raw = row.getAttribute('data-fid');
  if(raw === '' || raw === null) return 'a';
  return 'f:' + raw;
}

function docBindFolderEvents() {
  if(_docFoldersBound) return;
  const box = document.getElementById('doc-folders');
  if(!box) return;
  _docFoldersBound = true;

  box.addEventListener('click', ev=>{
    if(_dftSuppressing()) return;
    // ⚠️ 'more' 必须**先于**通用的 `[data-act]` 分支判断，但它本身也带 data-act="more"，
    //    所以这里用同一次 closest 取到元素后按 act 分派即可（不需要两轮查询）。
    const t = ev.target.closest('[data-act]');
    if(!t || !box.contains(t)) return;
    const act = t.getAttribute('data-act');
    ev.stopPropagation();
    if(act === 'toggle') { docToggleNode(Number(t.getAttribute('data-fid'))); return; }
    if(act === 'smart')  { _dftFocus = 's:' + t.getAttribute('data-smart'); docSelectSmartView(t.getAttribute('data-smart')); return; }
    // ⋮ 更多：菜单锚定到按钮本身（不再依赖鼠标坐标）
    if(act === 'more')   { _dftFocus = _dftRowKey(t.closest('[data-dftkey]') || t); docOpenRowMenu(t); return; }
    if(act === 'groupmore') { docGroupContextMenu(t, t.getAttribute('data-dftgroup')); return; }
    if(act === 'select'){
      _dftFocus = _dftRowKey(t);
      const raw = t.getAttribute('data-fid');
      docSelectFolder(raw === '' || raw === null ? '' : Number(raw));
      return;
    }
  });

  box.addEventListener('dblclick', ev=>{
    if(_dftSuppressing()) return;
    if(ev.target.closest('[data-act="toggle"],[data-act="more"]')) return;
    const t = ev.target.closest('[data-act="select"]');
    if(!t) return;
    const raw = t.getAttribute('data-fid');
    // 「全部文档」「未归类」是固定入口，不可改名（它们的名字是语义标识，不是用户资产）
    if(raw === '' || raw === null || raw === '0') return;
    docFolderRenameInline(Number(raw));
  });

  box.addEventListener('keydown', docFolderKeydown);

  // 右键菜单（2026-09-28 起为**兼容通道**）：主入口已改为行尾「⋮」按钮（hover 显形）。
  // 右键与 ⋮ 调用同一个 `docOpenRowMenu`，菜单内容单一来源，不会出现两套菜单。
  box.addEventListener('contextmenu', ev=>{
    const node = ev.target.closest('[data-act="select"],[data-act="smart"]');
    if(node && box.contains(node)) {
      ev.preventDefault(); ev.stopPropagation();
      docOpenRowMenu(node, ev);
      return;
    }
    const grp = ev.target.closest('[data-dftgroup]');
    if(grp && box.contains(grp)) {
      ev.preventDefault(); ev.stopPropagation();
      docGroupContextMenu(ev, grp.getAttribute('data-dftgroup'));
    }
  });

  // ── 拖拽：文件夹行作为拖拽源 ──
  box.addEventListener('dragstart', ev=>{
    // ⋮ 按钮不接受拖拽：否则「按住 ⋮ 想点菜单」的轻微位移会被判成拖动手势，
    // 把整个文件夹拖走（行是 draggable=true，子元素的拖拽会冒泡成行的拖拽）。
    if(ev.target.closest && ev.target.closest('[data-act="more"],[data-act="groupmore"]')) {
      ev.preventDefault(); return;
    }
    const row = ev.target.closest('[data-act="select"][data-fid]');
    if(!row) return;
    const raw = row.getAttribute('data-fid');
    if(raw === '' || raw === '0') { ev.preventDefault(); return; }   // 位置项不是可移动对象
    _dftDragSrc = 'folder:' + raw;
    _dftDragging = true;
    ev.dataTransfer.effectAllowed = 'copyMove';
    ev.dataTransfer.setData('text/plain', _dftDragSrc);
  });
  box.addEventListener('dragend', docDftEndDrag);
  // ⚠️ 兜底（2026-09-28 实测补）：拖拽可能在本容器之外结束（Escape 取消 / 落到窗口外 /
  //    dragstart 后没有配套 dragend）。只清容器级 dragend 时 _dftDragging 会**卡在 true**，
  //    于是 _dftSuppressing() 恒真 → 整棵树后续所有点击都被吞掉（现象是"树突然点不动"）。
  document.addEventListener('dragend', docDftEndDrag, true);

  box.addEventListener('dragover', ev=>{
    const row = ev.target.closest('[data-act="select"],[data-smart]');
    if(!row || !box.contains(row)) return;
    const zone = _dftZoneOf(row, ev.clientY);
    const ok = docFolderDropAllowed(_dftDragSrc, row, zone);
    docDftMarkZone(ok ? row : null, ok ? zone : '');
    if(!ok) { ev.dataTransfer.dropEffect = 'none'; return; }
    ev.preventDefault();   // 必须 preventDefault 才允许 drop
    ev.dataTransfer.dropEffect = String(_dftDragSrc).startsWith('folder:') ? 'move' : 'copy';
  });
  box.addEventListener('dragleave', ev=>{ if(!box.contains(ev.relatedTarget)) docDftClearZones(); });
  box.addEventListener('drop', ev=>{
    const row = ev.target.closest('[data-act="select"],[data-smart]');
    // ⚠️ 必须先读、后清：docDftClearZones() 会 delete row.dataset.dftZone，
    //    先清后读会让"dragover 记录的落点区"恒为 undefined，只能退回按坐标重算，
    //    而 drop 事件的 clientY 在部分浏览器被改写为 0（该机制等于白做）。
    const zone = row ? (row.dataset.dftZone || _dftZoneOf(row, ev.clientY)) : '';
    const key = (ev.dataTransfer && ev.dataTransfer.getData('text/plain')) || _dftDragSrc;
    docDftClearZones();
    if(!row || !box.contains(row)) { docDftEndDrag(); return; }
    ev.preventDefault();
    docFolderApplyDrop(key, _dftRowKey(row), zone);
    docDftEndDrag();   // 手势已结束，立即复位（不等 dragend）
  });
}

// ── 键盘流（VS Code 级手感；编译顺序：先修好"能聚焦"，快捷键才有意义）──
function docFolderKeydown(ev) {
  const box = document.getElementById('doc-folders');
  if(!box) return;
  if(_dftEdit) return;                     // 编辑中：键盘全部让给输入框
  const active = document.activeElement;
  if(!active || !box.contains(active)) return;
  const cur = active.closest('[data-dftkey]');
  const keys = _dftKeys(box);
  const i = cur ? keys.indexOf(cur) : -1;
  const raw = cur ? cur.getAttribute('data-fid') : null;
  const isFixed = (raw === '' || raw === null || raw === '0');   // 位置项 / 智能视图：不可改名删除
  const move = j => {
    const n = keys[Math.max(0, Math.min(keys.length - 1, j))];
    if(n) { n.focus(); _dftFocus = n.getAttribute('data-dftkey'); }
    ev.preventDefault();
  };
  if(ev.key === 'Escape') { docSelectFolder(''); docSelectSmartView(''); return; }
  if(ev.key === 'ArrowDown') { move(i + 1); return; }
  if(ev.key === 'ArrowUp')   { move(i - 1); return; }
  if(ev.key === 'Home')      { move(0); return; }
  if(ev.key === 'End')       { move(keys.length - 1); return; }
  if(ev.key === 'ArrowRight') {
    const fid = Number(raw);
    if(fid > 0 && _dftCollapsed.has(fid)) { docToggleNode(fid); return; }
    if(fid > 0 && docFolderHasKids(fid)) { move(i + 1); return; }   // 已展开 → 进首个可见子项
    return;
  }
  if(ev.key === 'ArrowLeft') {
    const fid = Number(raw);
    if(fid > 0 && !_dftCollapsed.has(fid) && docFolderHasKids(fid)) { docToggleNode(fid); return; }
    const f = (_docFolders||[]).find(x=>x.id===fid);
    if(f && f.parent_id) { docRestoreFocus('f:' + f.parent_id); _dftFocus = 'f:' + f.parent_id; }
    ev.preventDefault();
    return;
  }
  if(ev.key === 'Enter') { ev.preventDefault(); if(cur) { cur.click(); } return; }
  if(ev.key === 'F2') {
    if(!cur || cur.hasAttribute('data-smart') || isFixed) return;
    ev.preventDefault(); docFolderRenameInline(Number(raw)); return;
  }
  if(ev.key === 'Delete') {
    if(!cur || cur.hasAttribute('data-smart') || isFixed) return;
    ev.preventDefault(); docFolderDelete(Number(raw)); return;
  }
  if((ev.key === 'F10' && ev.shiftKey) || ev.key === 'ContextMenu') {
    // 键盘打开「更多」菜单（2026-09-28 起 ⋮ 是主入口）：锚定到该行的 ⋮ 按钮，
    // 与鼠标点 ⋮ 完全同一定位与同一份菜单内容（找不到按钮时退回行左上角）。
    if(!cur) return;
    ev.preventDefault();
    const btn = cur.querySelector('.dft-more');
    if(btn) docGroupOrRowMore(btn, cur);
    else {
      const r = cur.getBoundingClientRect();
      docFolderContextMenu({ clientX: r.left + 14, clientY: r.bottom }, cur);
    }
  }
}

// Shift+F10 / ContextMenu 键的统一分派：分组头 ⋮ 走分组菜单，行 ⋮ 走行菜单。
// 抽出来的原因：⋮ 按钮有两种（.dft-group 的 groupmore / 行的 more），
// 键盘流若照抄 click 委托的分支判断就会与 DOM 结构耦合两处，后续加行类型必漏一处。
function docGroupOrRowMore(btn, rowEl) {
  const act = btn.getAttribute('data-act');
  if(act === 'groupmore') { docGroupContextMenu(btn, btn.getAttribute('data-dftgroup')); return; }
  if(rowEl) docOpenRowMenu(btn);
}

function docToggleNode(fid) {
  if(!fid) return;
  if(_dftCollapsed.has(fid)) _dftCollapsed.delete(fid); else _dftCollapsed.add(fid);
  _dftFocus = 'f:' + fid;
  renderDocFolders();
  docRestoreFocus(_dftFocus);
}

// ── 选择态 ──
function docSelectFolder(fid) {
  const prev = _docFolderId;
  _docFolderId = (fid === '' || fid === null || fid === undefined) ? '' : Number(fid);
  _docSmartView = '';                    // 与智能视图互斥（两种筛选口径不叠加，避免"筛完什么都没有"）
  _docSelected.clear();
  // 目录选中默认 **含子目录**（2026-09-28 用户决策：左栏数字统一为含子目录累计数）。
  // 数字与列表必须同口径 —— 默认 self 会让「FE-热管理 显示 2」点进去只看到 1 条（自相矛盾）。
  // 进入具体目录（>0）时强制 subtree；切到「全部文档」('')或「未归类」(0) 时 scope 无意义，
  // 但一并复位为 subtree，避免上次的手工选择残留到下一次进目录（"为什么又只剩一条"的经典困惑）。
  if(String(prev) !== String(_docFolderId)) _docFolderScope = 'subtree';
  loadDocs();                            // loadDocs 内部会刷新目录树与面包屑
}

function docSelectSmartView(key) {
  _docSmartView = (_docSmartView === key) ? '' : (key || '');
  _docFolderId = '';
  _docSelected.clear();
  loadDocs();
}

function docSetFolderScope(scope) {
  _docFolderScope = (scope === 'subtree') ? 'subtree' : 'self';
  loadDocs();
}

function docToggleLeft() {
  const left = document.getElementById('doc-left');
  const bt = document.getElementById('doc-left-collapse');
  if(!left) return;
  const collapsed = left.getAttribute('data-collapsed') === '1';
  const next = collapsed ? '0' : '1';
  left.setAttribute('data-collapsed', next);
  // ⚠️ 关键坑：min/max 必须跟着 width 一起改。只写 width=40px 而 minWidth 仍是 220px 时，
  // min-width 会把宽度**夹回** 220px —— DOM 属性与按钮文本都对了，用户却看不到任何变化
  // （实测：折叠后 getBoundingClientRect().width 仍是 220，肉眼完全没反应）。
  if(next === '1'){
    left.style.width = '40px';
    left.style.minWidth = '40px';
    left.style.maxWidth = '40px';
    left.style.overflow = 'hidden';   // 折叠后内容会溢出，必须裁掉
  }else{
    left.style.width = '220px';
    left.style.minWidth = '40px';
    left.style.maxWidth = '220px';
    left.style.overflow = '';
  }
  // 折叠后隐藏标题/新建/刷新/目录树，避免 40px 宽里挤出一堆溢出文字
  left.querySelectorAll('[data-page-node-id="docleft-title"],[data-page-node-id="docleft-new"],'
      + '[data-page-node-id="docleft-refresh"],#doc-folders')
      .forEach(el => { el.style.display = (next === '1') ? 'none' : ''; });
  if(bt) bt.textContent = collapsed ? '«' : '»';
}

// ── 面包屑 ──
function renderDocCrumb() {
  const el = document.getElementById('doc-crumb');
  if(!el) return;
  const parts = [];
  if(_docSmartView) {
    const v = DOC_SMART_VIEWS.find(x=>x.key===_docSmartView);
    if(v) parts.push(`<span style="color:var(--blue-d);font-weight:600;">${v.icon} ${v.label}</span>
      <span style="color:var(--mut);" title="已下线（弃用）的文档；可在此恢复">（回收站 · 只读，可与上方筛选叠加）</span>`);
  } else if(_docFolderId !== '' && Number(_docFolderId) === 0) {
    parts.push(`<span style="cursor:pointer;color:var(--blue-d);" onclick="docSelectFolder('')" title="返回全部文档">全部文档</span>
      <span style="color:var(--mut);">/</span><b>📥 未归类</b>
      <span style="color:var(--mut);" title="尚未放入任何结构目录的文档；把文件拖到左栏目录即可归类">（兜底入口，可直接拖入目录归类）</span>`);
  } else if(_docFolderId !== '') {
    // 面包屑由 path 反推（'/规范/热管理/' → [规范, 热管理]），不额外发请求
    const f = (_docFolders||[]).find(x=>x.id===Number(_docFolderId));
    const seg = f ? (f.path||'').split('/').filter(Boolean) : [];
    parts.push(`<span style="cursor:pointer;color:var(--blue-d);" onclick="docSelectFolder('')" title="返回全部文档">全部文档</span>`);
    seg.forEach((name,i)=>{
      parts.push('<span style="color:var(--mut);">/</span>');
      const isLast = i === seg.length-1;
      parts.push(isLast
        ? `<b>${esc(name)}</b>`
        : `<span style="color:var(--blue-d);">${esc(name)}</span>`);
    });
    // 2026-09-28：默认 **含子目录**（与左栏累计计数同口径）。勾选框语义不变，
    // 取消勾选 = 只看本目录直接挂载的文档，此时列表条数会**小于**左栏显示的数字（这是预期，不是 bug）。
    parts.push(`<label style="margin-left:10px;font-size:11px;color:var(--mut);cursor:pointer;" title="默认勾选：与左栏目录数字（含子目录）同口径&#10;取消勾选 = 只看本目录直接挂载的文档">&#10;<input type="checkbox" ${_docFolderScope==='subtree'?'checked':''} onchange="docSetFolderScope(this.checked?'subtree':'self')"> 含子目录</label>`);
  } else {
    // ⚠️ 必须显式隐藏 + 清空：只 return 会把上一条面包屑（含「含子目录」勾选框）留在中栏，
    //    切回「全部文档」后看起来仍筛着某个目录（2026-09-28 截图复核实测到的既有缺陷）。
    el.style.display = 'none';
    el.innerHTML = '';
    return;   // 无筛选 → 不占位
  }
  el.style.display = 'flex';
  el.innerHTML = parts.join('');
}

// ── 行内改名（替换原生 prompt；照搬 21-ontology.js::ontInlineRename 的既有惯例）──
// 语义：Enter 提交 / Esc 取消 / blur 提交（对齐本仓既有行内编辑），失败保留编辑态并回填原输入
function docFolderRenameInline(fid, seed) {
  if(_dftEdit) return;
  const f = (_docFolders||[]).find(x=>x.id===fid);
  if(!f) return;
  const box = document.getElementById('doc-folders');
  const row = box && box.querySelector(`[data-act="select"][data-fid="${fid}"]`);
  const nameEl = row && row.querySelector('.dft-name');
  if(!nameEl) return;
  const oldName = f.name;
  const inp = document.createElement('input');
  inp.className = 'dft-inp';
  inp.value = (seed === undefined || seed === null) ? oldName : String(seed);
  inp.setAttribute('data-dft-inp', 'rename');
  inp.title = 'Enter 保存 · Esc 取消';
  nameEl.replaceWith(inp);
  _dftEdit = { mode:'rename', fid: fid, parentId: f.parent_id };
  _dftFocus = 'f:' + fid;
  inp.focus(); inp.select();
  let fired = false;
  const done = async (save) => {
    if(fired) return;
    fired = true;
    _dftEdit = null;
    const nv = (inp.value || '').trim();
    const bad = _dftNameError(nv);
    if(!save || !nv || nv === oldName) { renderDocFolders(); docRestoreFocus(_dftFocus); return; }
    if(bad) { toast(bad); renderDocFolders(); docFolderRenameInline(fid, nv); return; }
    try {
      const r = await api(`/api/doc-folders/${fid}`, { method:'PUT', body: JSON.stringify({ name: nv }) });
      if(!r.ok) { toast('重命名失败：' + (r.error || '未知原因')); renderDocFolders(); docFolderRenameInline(fid, nv); return; }
      toast(`📁 已重命名为「${nv}」`);
      _docTreeStale = true;
      loadDocs();
    } catch(e) {
      toast('重命名失败：' + e.message); renderDocFolders(); docFolderRenameInline(fid, nv);
    }
  };
  inp.addEventListener('keydown', e=>{
    e.stopPropagation();   // 别让容器的 Escape/Esc 清除筛选逻辑抢走按键
    if(e.key === 'Enter') { e.preventDefault(); done(true); }
    else if(e.key === 'Escape') { e.preventDefault(); done(false); }
  });
  inp.addEventListener('blur', ()=>done(true));
}

// 与服务端 create()/rename() 的口径对齐，少一次无谓往返（服务端仍是最终裁决）
function _dftNameError(nv) {
  if(!nv) return '目录名不能为空';
  if(nv.length > 80) return '目录名过长（≤80 字符）';
  if(nv.indexOf('/') >= 0 || nv.indexOf('\\') >= 0) return "目录名不能包含 '/' 或 '\\'";
  return '';
}

// ── 就地新建（替换原生 prompt）──
function docFolderCreateInline(parentId, seed) {
  if(_dftEdit) return;
  const box = document.getElementById('doc-folders');
  if(!box) return;
  const pid = Number(parentId) || 0;
  if(pid) { _dftCollapsed.delete(pid); renderDocFolders(); }   // 先展开父级，否则输入行会被折叠藏掉
  const tmp = document.createElement('div');
  tmp.className = 'ont-tnd dft-row';
  tmp.id = 'dft-new-row';
  tmp.style.paddingLeft = ((pid ? docFolderDepth(pid) + 1 : 0) * 14 + 22) + 'px';
  const ico = document.createElement('span');
  ico.className = 'dft-ico'; ico.textContent = '📁';
  const inp = document.createElement('input');
  inp.className = 'dft-inp';
  inp.placeholder = '新目录名，Enter 创建';
  inp.value = (seed === undefined || seed === null) ? '' : String(seed);
  inp.setAttribute('data-dft-inp', 'create');
  tmp.appendChild(ico); tmp.appendChild(inp);
  if(pid) {
    const prow = box.querySelector(`[data-act="select"][data-fid="${pid}"]`);
    if(prow) prow.after(tmp); else box.appendChild(tmp);
  } else {
    // 根级：插在「结构目录」组标题之后（该组第一项），不能插在「未归类」之前——那是位置区
    const grp = box.querySelector('[data-dftgroup="struct"]');
    if(grp) grp.after(tmp); else box.appendChild(tmp);
  }
  _dftEdit = { mode:'create', parentId: pid };
  inp.focus();
  let fired = false;
  const done = async (save) => {
    if(fired) return;
    fired = true;
    _dftEdit = null;
    const nv = (inp.value || '').trim();
    if(tmp.parentNode) tmp.remove();
    if(!save || !nv) { renderDocFolders(); docRestoreFocus(_dftFocus); return; }
    const bad = _dftNameError(nv);
    if(bad) { toast(bad); renderDocFolders(); docFolderCreateInline(pid, nv); return; }
    try {
      const r = await api('/api/doc-folders', { method:'POST', body: JSON.stringify({ name: nv, parent_id: pid }) });
      if(!r.ok) { toast('新建失败：' + (r.error || '未知原因')); renderDocFolders(); docFolderCreateInline(pid, nv); return; }
      toast(`📁 已创建「${nv}」`);
      _docTreeStale = true;
      _dftFocus = r.id ? ('f:' + r.id) : 'a';
      if(r.id) docSelectFolder(r.id); else loadDocs();   // 建完直接选中，少一次点击
    } catch(e) {
      toast('新建失败：' + e.message); renderDocFolders(); docFolderCreateInline(pid, nv);
    }
  };
  inp.addEventListener('keydown', e=>{
    e.stopPropagation();
    if(e.key === 'Enter') { e.preventDefault(); done(true); }
    else if(e.key === 'Escape') { e.preventDefault(); done(false); }
  });
  inp.addEventListener('blur', ()=>done(true));
}
// 兼容既有内联调用点 / 验证脚本里的旧函数名（AGENTS 铁律 1：改名字会静默失效）
function docFolderCreate(parentId) { return docFolderCreateInline(parentId); }
function docFolderRename(fid) { return docFolderRenameInline(fid); }

// ── 目录写操作 ──
async function docFolderDelete(fid) {
  const name = docFolderName(fid);
  if(!name) return;
  // 先取真实数字做确认文案（子目录数/文档数），别让用户凭感觉点
  const f = (_docFolders||[]).find(x=>x.id===fid) || {};
  // 后代数：物化路径前缀法（与后端 _descendant_ids 同口径），不递归、不发请求
  const _base = f.path || '';
  const sub = _base
    ? (_docFolders||[]).filter(x=>x.id!==fid && String(x.path||'').startsWith(_base)).length
    : 0;
  const ok = await confirmDialog(
    `删除目录「${name}」？\n\n· 其下 ${f.doc_count||0} 份文档（含子目录共 ${f.doc_count_all||0} 份）将回到「未归类」\n· **不会删除任何文档**\n· 子目录也会一并删除${sub?`（${sub} 个）`:''}`);
  if(!ok) return;
  try {
    const r = await api(`/api/doc-folders/${fid}`, { method:'DELETE' });
    if(!r.ok) { toast('删除失败：' + (r.error || '未知原因')); return; }
    toast(`🗑 已删除「${r.deleted}」· ${r.moved_to_uncategorized||0} 份文档回到未归类`);
    if(Number(_docFolderId) === fid) _docFolderId = '';
    _dftCollapsed.delete(fid);
    _dftFocus = 'a';
    _docTreeStale = true;
    loadDocs();
  } catch(e) { toast('删除失败：' + e.message); }
}

// ── 移动 / 排序（同一后端端点：POST /api/doc-folders/{id}/move）──
function docFolderMoveTargetsHtml(excludeId) {
  const rows = [`<option value="0">🏠 根目录（最外层）</option>`];
  const bad = new Set();
  const mark = (id) => {
    bad.add(id);
    docFolderKids(id).forEach(c=>mark(c.id));
  };
  if(excludeId) mark(Number(excludeId));
  const walk = (pid, depth) => docFolderKids(pid).forEach(f=>{
    rows.push(`<option value="${f.id}" ${bad.has(f.id)?'disabled':''}>`
      + `${'　'.repeat(depth)}${esc(f.name)}${bad.has(f.id)?'（自身或其子目录，不可选）':''}</option>`);
    walk(f.id, depth+1);
  });
  walk(0, 0);
  return rows.join('');
}

function docFolderMovePrompt(fid) {
  const f = (_docFolders||[]).find(x=>x.id===fid) || {};
  const html = `<div style="font-size:12.5px;">
    <div style="margin-bottom:6px;color:var(--mut);">把目录「<b>${esc(f.name || ('#'+fid))}</b>」移动到：</div>
    <select id="dft-move-sel" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
      ${docFolderMoveTargetsHtml(fid)}
    </select>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button class="btn sm ghost" onclick="closePanel()">取消</button>
      <button class="btn sm" onclick="docFolderMoveConfirm(${fid})">移动</button>
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--mut);">只改目录树的归属层级：其下的文档与子目录一起跟随；不移动磁盘文件、不重算向量。</div>
  </div>`;
  openPanel('📁 移动目录', html);
}

async function docFolderMoveConfirm(fid) {
  const sel = document.getElementById('dft-move-sel');
  const pid = sel ? Number(sel.value) : 0;
  const f = (_docFolders||[]).find(x=>x.id===fid) || {};
  if(Number(f.parent_id || 0) === pid) { closePanel(); toast('目标层级与当前位置相同'); return; }
  await docFolderMoveBy(fid, { parent_id: pid }, true);
}

async function docFolderMoveBy(fid, payload, fromPanel) {
  try {
    const r = await api(`/api/doc-folders/${fid}/move`, { method:'POST', body: JSON.stringify(payload || {}) });
    if(!r.ok) { toast('移动失败：' + (r.error || '未知原因')); return false; }
    if(r.unchanged) {
      toast('位置未变化');
    } else if(payload && (payload.before_id || payload.after_id)) {
      toast('↕ 已调整同级顺序');
    } else {
      toast(`📁 已移动到「${r.parent_id ? (docFolderName(r.parent_id) || ('#' + r.parent_id)) : '根目录'}」`);
    }
    if(fromPanel) closePanel();
    _docTreeStale = true;
    _dftFocus = 'f:' + fid;
    loadDocs();
    return true;
  } catch(e) { toast('移动失败：' + e.message); return false; }
}

// ── 行操作菜单（复用 09-impact.js 的 #doc-ctx-menu 外壳与 closeDocCtxMenu 关闭钩子）──
// 2026-09-28：主入口从「右键」改为行尾「⋮」按钮（hover 显形）。
// `anchor` 的三种形态统一在此收敛，调用方不用关心定位细节：
//   · HTMLElement（⋮ 按钮）→ 贴按钮右下角展开
//   · {clientX, clientY}（右键事件）→ 以鼠标为左上角
//   · 缺省 → 视口左上兜底
function _dftCtxMenu(anchor, items) {
  if(typeof closeDocCtxMenu === 'function') closeDocCtxMenu();
  const m = document.createElement('div');
  m.id = 'doc-ctx-menu';
  m.style.cssText = 'position:fixed;z-index:6000;min-width:158px;background:#fff;border:1px solid var(--line);'
    + 'border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.14);padding:4px;font-size:12px;';
  // ⚠️ 菜单项文本一律走 esc() 的**文本节点**，绝不拼进 onclick 串（目录名可含引号）；
  // 动作靠 data-dftmi 索引 + 事件委托取，因此用户输入永远不会进入任何代码串。
  m.innerHTML = items.map((it, i)=>{
    if(it.sep) return '<div style="border-top:1px solid var(--line);margin:3px 2px;"></div>';
    const cls = 'rm-item' + (it.red ? ' red' : '');
    const st = it.disabled ? ' style="opacity:.45;cursor:not-allowed;"' : '';
    const ti = it.disabled ? ` title="${esc(it.why || '当前不可用')}"` : '';
    return `<div class="${cls}" data-dftmi="${i}"${st}${ti}>${esc(it.label)}</div>`;
  }).join('');
  m.addEventListener('click', e=>{
    const el = e.target.closest('[data-dftmi]');
    if(!el) return;
    const it = items[Number(el.getAttribute('data-dftmi'))];
    if(!it || it.disabled) return;
    if(typeof closeDocCtxMenu === 'function') closeDocCtxMenu();
    if(typeof it.act === 'function') it.act();
  });
  document.body.appendChild(m);
  const w = m.offsetWidth, h = m.offsetHeight;
  let x, y;
  if(anchor && anchor.nodeType === 1) {
    // ⋮ 按钮：左对齐按钮左缘、展开在按钮**下方**；底部越界则翻到上方
    const r = anchor.getBoundingClientRect();
    x = r.left; y = r.bottom + 2;
    if(y + h > window.innerHeight - 4) y = Math.max(4, r.top - h - 2);
  } else if(anchor && typeof anchor.clientX === 'number') {
    x = anchor.clientX; y = anchor.clientY;
  } else {
    x = 4; y = 4;
  }
  m.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 8)) + 'px';
  m.style.top  = Math.max(4, Math.min(y, window.innerHeight - h - 8)) + 'px';
}

// 行菜单统一入口：⋮ 点击 与 右键 都走这里（菜单内容单一来源 = docFolderContextMenu）
function docOpenRowMenu(triggerEl, ev) {
  const row = triggerEl && triggerEl.closest ? triggerEl.closest('[data-act="select"],[data-act="smart"]') : null;
  if(!row) return;
  const anchor = (ev && typeof ev.clientX === 'number') ? ev : triggerEl;
  docFolderContextMenu(anchor, row);
}

function docFolderContextMenu(anchor, row) {
  const raw = row.getAttribute('data-fid');
  const isSmart = row.hasAttribute('data-smart');
  const isAll = (raw === '' || raw === null);
  const isUnc = (raw === '0');
  const fid = isAll ? null : Number(raw);
  // 2026-09-28：⋮ 成为主入口后，菜单不再靠"列出禁用项"来解释"这里为什么不能改名"——
  // 常量节点（全部文档/未归类/回收站）的菜单**只放真正可做的动作**。
  // 理由：旧菜单里"重命名/移动到/删除"三项全灰，占了 3/4 的版面却零可用性，
  // 用户点开一个大半灰的菜单只会困惑；不可用的原因已写在这些行自己的 title 里。
  const items = [];
  if(isSmart) {
    const k = row.getAttribute('data-smart');
    const v = DOC_SMART_VIEWS.find(x=>x.key===k) || {};
    items.push({ label: '查看「' + (v.label || k) + '」', act: ()=>docSelectSmartView(k) });
    items.push({ label: '刷新列表', act: ()=>loadDocs() });
  } else if(isAll) {
    items.push({ label: '查看全部文档', act: ()=>docSelectFolder('') });
    items.push({ label: '刷新列表', act: ()=>loadDocs() });
  } else if(isUnc) {
    items.push({ label: '查看未归类文档', act: ()=>docSelectFolder(0) });
    items.push({ sep: true });
    // 「未归类」唯一有意义的批量动作：把其中文档分配出去（选中态由用户在多选后触发）
    items.push({ label: '移动到目录…（需先勾选文档）', act: ()=>batchMoveSelected() });
  } else {
    const f = (_docFolders||[]).find(x=>x.id===fid) || {};
    const sib = docFolderKids(Number(f.parent_id) || 0);   // 已保序（list_all 的 parent_id,sort,name）
    const idx = sib.findIndex(x=>x.id===fid);
    items.push({ label: '新建子目录', act: ()=>docFolderCreateInline(fid) });
    items.push({ label: '重命名（F2）', act: ()=>docFolderRenameInline(fid) });
    items.push({ label: '移动到…', act: ()=>docFolderMovePrompt(fid) });
    items.push({ sep: true });
    items.push({ label: '上移', disabled: idx <= 0, why: '已经是同级第一项',
                 act: ()=>docFolderMoveBy(fid, { before_id: sib[idx-1].id }) });
    items.push({ label: '下移', disabled: idx < 0 || idx >= sib.length - 1, why: '已经是同级最后一项',
                 act: ()=>docFolderMoveBy(fid, { after_id: sib[idx+1].id }) });
    items.push({ label: docFolderHasKids(fid) ? '折叠/展开' : '（无子目录）',
                 disabled: !docFolderHasKids(fid), why: '该目录没有子目录',
                 act: ()=>docToggleNode(fid) });
    items.push({ sep: true });
    items.push({ label: '删除目录（Delete）', red: true, act: ()=>docFolderDelete(fid) });
  }
  _dftCtxMenu(anchor, items);
}

function docGroupContextMenu(anchor, kind) {
  // 2026-09-28：'smart' 分组已随智能视图区移除 → 只剩「结构目录」一种分组菜单。
  // 注意：这里**不再提供「新建根目录」**（去冗余，唯一入口 = 标题栏 ＋）——
  //   此前分组右键里也有一份，与标题栏 ＋ 重复。
  const items = [ { label: '全部展开', act: ()=>{ _dftCollapsed.clear(); renderDocFolders(); } },
        { label: '全部折叠', act: ()=>{
            (_docFolders||[]).forEach(f=>{ if(docFolderHasKids(f.id)) _dftCollapsed.add(f.id); });
            renderDocFolders();
          } } ];
  _dftCtxMenu(anchor, items);
}

// ── 拖拽：三区命中 + 落点判定 ──
// into = 行中间 1/3（移入该目录）/ before = 上 1/3（排到它前面）/ after = 下 1/3（排到它后面）
function _dftZoneOf(row, clientY) {
  const r = row.getBoundingClientRect();
  const t = (clientY - r.top) / Math.max(1, r.height);
  const zone = t < 1/3 ? 'before' : (t > 2/3 ? 'after' : 'into');
  row.dataset.dftZone = zone;
  return zone;
}
function docDftClearZones() {
  document.querySelectorAll('#doc-folders .dft-into,#doc-folders .dft-before,#doc-folders .dft-after')
    .forEach(el=>{ el.classList.remove('dft-into','dft-before','dft-after'); delete el.dataset.dftZone; });
}
/** 一次拖拽手势的收尾：复位拖拽态 + 清高亮。dragend 与 drop 都调它（幂等）。 */
function docDftEndDrag() {
  _dftDragging = false;
  _dftDragSrc = '';
  _dftSuppressUntil = Date.now() + 300;   // 拖完 300ms 内不接收 click/dblclick，避免误触选中或改名
  docDftClearZones();
}
function docDftMarkZone(row, zone) {
  docDftClearZones();
  if(!row || !zone) return;
  if(zone === 'into') row.classList.add('dft-into');
  else if(zone === 'before') row.classList.add('dft-before');
  else row.classList.add('dft-after');
}

/** 落点是否合法（纯判定，不发请求；验证脚本可直接调它做"非法目标"断言）。 */
function docFolderDropAllowed(srcKey, row, zone) {
  if(!srcKey || !row) return false;
  if(row.hasAttribute('data-smart')) return false;          // 智能视图只读
  const raw = row.getAttribute('data-fid');
  if(raw === '' || raw === null) return false;              // 「全部文档」是筛选入口，不是落点
  const src = String(srcKey);
  if(src.indexOf('doc:') === 0 || src.indexOf('docs:') === 0) return true;   // 文档 → 目录（raw='0' 即移出）
  if(src.indexOf('folder:') !== 0) return false;
  if(raw === '0') return false;                             // 「未归类」不是目录，不能被文件夹移入
  const srcId = Number(src.slice(7));
  const dstId = Number(raw);
  if(!srcId || !dstId || srcId === dstId) return false;
  const cur = (_docFolders||[]).find(x=>x.id===srcId);
  const dst = (_docFolders||[]).find(x=>x.id===dstId);
  if(!cur || !dst) return false;
  if(zone === 'into') return !String(dst.path||'').startsWith(String(cur.path||''));  // 不能进自己或后代
  // 上/下 1/3 = 插到「目标所在层级」的那一行：
  //   跨层级也允许（拖到根级某行的上/下 1/3 = 移到根级并插在该位置），这样"移动"与"排序"是同一个手势。
  //   唯一约束：目标层级的父不能是自己，也不能落在自己的子树里（否则等于移进自己的后代）。
  const pid = Number(dst.parent_id) || 0;
  if(pid === 0) return true;                               // 目标是根级 → 插到根级永远合法
  if(pid === Number(srcId)) return false;                   // 目标层级就是自己 → 非法
  const prow = (_docFolders||[]).find(x=>x.id===pid);
  if(!prow) return false;
  return !String(prow.path||'').startsWith(String(cur.path||''));
}

/** 执行落点（前端唯一入口；右键上移/下移与拖拽复用同一后端端点）。 */
async function docFolderApplyDrop(srcKey, dstKey, zone) {
  const src = String(srcKey || ''), dst = String(dstKey || '');
  if(!src || !dst || dst === 'a' || dst.indexOf('s:') === 0) return false;
  const dstRaw = dst.indexOf('f:') === 0 ? dst.slice(2) : '';
  if(dstRaw === '') return false;
  const dstId = Number(dstRaw) || 0;                        // 0 = 未归类（文档移出）
  if(docFolderDropAllowed(src, document.querySelector(`#doc-folders [data-dftkey="${dst}"]`), zone) === false) {
    // 兜底：DOM 查不到时也按同一判定函数复核一次（拖拽落点在自动化环境下常拿不到目标行）
    if(!(src.indexOf('doc:') === 0 || src.indexOf('docs:') === 0)) return false;
  }
  if(src.indexOf('doc:') === 0 || src.indexOf('docs:') === 0) {
    const ids = src.indexOf('docs:') === 0
      ? src.slice(5).split(',').map(s=>Number(s)).filter(Boolean)
      : [Number(src.slice(4))];
    if(!ids.length || ids.some(isNaN)) return false;
    try {
      if(ids.length === 1) {
        const r = await api(`/api/documents/${ids[0]}/move`, { method:'POST', body: JSON.stringify({ folder_id: dstId }) });
        if(!r.ok) { toast('移动失败：' + (r.error || '未知原因')); return false; }
      } else {
        const r = await api('/api/documents/batch', { method:'POST', body: JSON.stringify({ action:'move', ids, folder_id: dstId }) });
        const ok = r.ok || 0, failed = (r.failed || []).length;
        if(!ok) { toast('移动失败：' + (r.error || '未知原因')); return false; }
        toast(`📁 移动完成：成功 ${ok}${failed?` · 失败 ${failed}`:''}`);
        _docSelected.clear();
      }
      if(ids.length === 1) toast(dstId ? `📁 已移动到「${docFolderName(dstId) || dstId}」` : '📥 已移出到「未归类」');
      _docTreeStale = true;
      loadDocs();
      return true;
    } catch(e) { toast('移动失败：' + e.message); return false; }
  }
  if(src.indexOf('folder:') === 0) {
    const fid = Number(src.slice(7));
    if(!fid || fid === dstId) return false;
    const dstF = (_docFolders||[]).find(x=>x.id===dstId);
    if(!dstF) return false;
    if(zone === 'into') return docFolderMoveBy(fid, { parent_id: dstId });
    if(zone === 'before') return docFolderMoveBy(fid, { parent_id: dstF.parent_id, before_id: dstId });
    return docFolderMoveBy(fid, { parent_id: dstF.parent_id, after_id: dstId });
  }
  return false;
}

/** 中栏文档行作为拖拽源（事件委托挂永不重建的 #doc-list）。 */
function docBindDocDrag() {
  const list = document.getElementById('doc-list');
  if(!list || _dftDocDragBound) return;
  _dftDocDragBound = true;
  list.addEventListener('dragstart', ev=>{
    const tr = ev.target.closest('[data-drag-doc]');
    if(!tr) return;
    const id = Number(tr.getAttribute('data-drag-doc'));
    // 已多选时拖一个 = 拖整批（与批量条「移动到…」同一语义，少一次弹窗）
    const ids = (_docSelected && _docSelected.has(id)) ? Array.from(_docSelected) : [id];
    _dftDragSrc = ids.length > 1 ? ('docs:' + ids.join(',')) : ('doc:' + ids[0]);
    _dftDragging = true;
    ev.dataTransfer.effectAllowed = 'copyMove';
    ev.dataTransfer.setData('text/plain', _dftDragSrc);
  });
  list.addEventListener('dragend', ()=>{
    _dftDragging = false; _dftDragSrc = ''; _dftSuppressUntil = Date.now() + 300; docDftClearZones();
  });
}

// ── 文档 → 目录（行内「移动到…」下拉；批量版挂在既有 #doc-batch-bar）──
function docMoveTargetsHtml() {
  const rows = [`<option value="0">📥 未归类（不放入任何目录）</option>`];
  const walk = (pid, depth) => {
    docFolderKids(pid).forEach(f=>{
      rows.push(`<option value="${f.id}">${'　'.repeat(depth)}${esc(f.name)}</option>`);
      walk(f.id, depth+1);
    });
  };
  walk(0, 0);
  return rows.join('');
}

async function docMovePrompt(docId) {
  const html = `<div style="font-size:12.5px;">
    <div style="margin-bottom:6px;color:var(--mut);">把文档移动到：</div>
    <select id="doc-move-sel" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
      ${docMoveTargetsHtml()}
    </select>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button class="btn sm ghost" onclick="closePanel()">取消</button>
      <button class="btn sm" onclick="docMoveConfirm(${docId})">移动</button>
    </div>
    <div style="margin-top:10px;font-size:11px;color:var(--mut);">提示：移动只改归属目录，不重算向量、不改文件名、不动磁盘副本。</div>
  </div>`;
  openPanel('📁 移动文档到目录', html);
}

async function docMoveConfirm(docId) {
  const sel = document.getElementById('doc-move-sel');
  const fid = sel ? Number(sel.value) : 0;
  try {
    const r = await api(`/api/documents/${docId}/move`, { method:'POST', body: JSON.stringify({ folder_id: fid }) });
    if(!r.ok) { toast('移动失败：' + (r.error || '未知原因')); return; }
    closePanel();
    toast(fid ? `📁 已移动到「${docFolderName(fid) || fid}」` : '📥 已移出到「未归类」');
    _docTreeStale = true;   // 目录计数变了
    loadDocs();
  } catch(e) { toast('移动失败：' + e.message); }
}

async function batchMoveSelected() {
  const ids = [..._docSelected];
  if(!ids.length) { toast('请先勾选文档'); return; }
  const html = `<div style="font-size:12.5px;">
    <div style="margin-bottom:6px;color:var(--mut);">把选中的 <b>${ids.length}</b> 份文档移动到：</div>
    <select id="doc-move-sel" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12.5px;">
      ${docMoveTargetsHtml()}
    </select>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button class="btn sm ghost" onclick="closePanel()">取消</button>
      <button class="btn sm" onclick="batchMoveConfirm()">移动 ${ids.length} 份</button>
    </div>
  </div>`;
  openPanel('📁 批量移动到目录', html);
}

async function batchMoveConfirm() {
  const ids = [..._docSelected];
  if(!ids.length) { toast('请先勾选文档'); return; }
  const sel = document.getElementById('doc-move-sel');
  const fid = sel ? Number(sel.value) : 0;
  try {
    const r = await api('/api/documents/batch', {
      method:'POST', body: JSON.stringify({ action:'move', ids, folder_id: fid })
    });
    const ok = r.ok || 0, failed = (r.failed || []).length;
    toast(`📁 移动完成：成功 ${ok}${failed?` · 失败 ${failed}`:''}`);
    closePanel();
    _docSelected.clear();
    _docTreeStale = true;   // 目录计数变了
    loadDocs();
  } catch(e) { toast('批量移动失败：' + e.message); }
}