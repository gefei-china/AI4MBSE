/* HIL 确认卡验证 —— t1：渲染细节断言（破坏性批次卡 + 可逆写卡）
 *
 * ## 断言设计原则（能证伪才有价值）
 * 每一条都对应"旧实现会给不同结果"的量：
 *   · detailHasPreview / detailHasKind  —— 旧实现只塞参数 JSON，这两条必 FAIL
 *   · hasDetailsCollapse                —— 旧实现参数直接裸露，没有 <details>
 *   · argsHiddenByDefault               —— 参数必须**默认折叠**（不给 JSON 当主视图）
 *   · iconIsBlock / titleHasIrreversible —— 破坏性与可逆写必须**视觉可分**
 * 中文字面量用 \uXXXX 转义写死，避免任何编码层误差。
 */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = {};

  // ── 用例 A：破坏性 + 批次合并（3 项）──
  const evA = {
    confirmation_id: 900001,
    tool: 'delete_model_elements',
    tool_type: 'mcp',
    level: 'confirm',
    match_key: 'mcp_destructive',
    kind: 'destructive',
    preview: '\u5220\u9664\u300c\u65e7\u5305\u300d\uff1b\u5bb9\u5668 parentId=2114907195813986304',
    arguments: { dataId: '2115077874346688512', workspace: { versionId: '2114939811837575168' } },
    batch_count: 3,
    batch_merged: true,
    conversation_id: 9700,
    timeout_at: '2026-10-29 08:00:00'
  };
  procAddHilConfirm(evA);
  await sleep(250);

  const elA = document.querySelector('[data-pid="hil_900001"]');
  const tA = elA ? elA.textContent : '';
  const dA = elA ? (elA.querySelector('details') ? elA.innerHTML : elA.innerHTML) : '';
  const detA = elA ? elA.querySelector('details') : null;
  const rA = elA ? elA.getBoundingClientRect() : null;
  out.A = {
    cardExists: !!elA,
    iconIsBlock: tA.indexOf('\u26d4') >= 0,                       // ⛔
    titleHasIrreversible: tA.indexOf('\u4e0d\u53ef\u9006') >= 0,   // 不可逆
    titleHasTarget: tA.indexOf('\u65e7\u5305') >= 0,               // 旧包
    detailHasPreviewKw: tA.indexOf('\u5f71\u54cd\u9762') >= 0,     // 影响面
    detailHasKindLine: tA.indexOf('\u6027\u8d28\uff1a') >= 0,      // 性质：
    detailHasBatchLine: tA.indexOf('\u672c\u6279\u5171 3 \u9879') >= 0,   // 本批共 3 项
    statusHasBatchCount: tA.indexOf('\u540c\u6279 3 \u9879') >= 0,        // 同批 3 项
    hasDetailsCollapse: !!detA,
    collapseSummaryKw: detA ? (detA.querySelector('summary').textContent.indexOf('\u67e5\u770b\u539f\u59cb\u53c2\u6570') >= 0) : false,
    argsHiddenByDefault: detA ? !detA.open : false,
    noSideEffectHint: tA.indexOf('\u4e0d\u4f1a\u4ea7\u751f\u4efb\u4f55\u526f\u4f5c\u7528') >= 0,
    visibleH: rA ? Math.round(rA.height) : -1,
    inViewport: rA ? (rA.height > 0 && rA.bottom > 0 && rA.top < window.innerHeight) : false
  };

  // ── 用例 B：可逆写（单条，不合并）──
  const evB = {
    confirmation_id: 900002,
    tool: 'update_model_elements',
    tool_type: 'mcp',
    level: 'confirm',
    match_key: 'mcp_write',
    kind: 'write',
    preview: '\u4fee\u6539\u300c\u9700\u6c42\u5305\u300d',
    arguments: { name: '\u9700\u6c42\u5305' },
    batch_count: 1,
    batch_merged: false
  };
  procAddHilConfirm(evB);
  await sleep(250);
  const elB = document.querySelector('[data-pid="hil_900002"]');
  const tB = elB ? elB.textContent : '';
  out.B = {
    cardExists: !!elB,
    iconIsLock: tB.indexOf('\u{1F510}') >= 0 || /[\u{1F510}]/u.test(tB),
    detailHasReversible: tB.indexOf('\u53ef\u9006\u5199') >= 0,     // 可逆写
    detailNotIrreversible: tB.indexOf('\u4e0d\u53ef\u9006') < 0,   // 不应出现"不可逆"
    noBatchLine: tB.indexOf('\u672c\u6279\u5171') < 0,             // 单条不应有批次行
    visibleH: elB ? Math.round(elB.getBoundingClientRect().height) : -1
  };

  // ── 用例 C：幂等去重（同 cid 重复调不新增卡片）──
  const n1 = document.querySelectorAll('[data-pid^="hil_"]').length;
  procAddHilConfirm(evA);
  await sleep(150);
  const n2 = document.querySelectorAll('[data-pid^="hil_"]').length;
  out.C = { before: n1, after: n2, dedup: n1 === n2 };
  out.D = {
    helper_defined: (typeof _hilCardEl === 'function'),
    helper_finds_A: (typeof _hilCardEl === 'function') ? !!_hilCardEl(900001) : false,
    helper_finds_B: (typeof _hilCardEl === 'function') ? !!_hilCardEl(900002) : false,
    helper_null_on_missing: (typeof _hilCardEl === 'function') ? (_hilCardEl(999999) === null) : false,
    // 反面证据：旧写法必须**取不到**（证明这个缺陷真实存在过）
    legacyGetElementByIdReturnsNull: (document.getElementById('hil_900001') === null)
  };

  // ── 夹具自洽性（防止断言建在空壳上）──
  const allTxt = Array.from(document.querySelectorAll('[data-pid^="hil_"]')).map(e => e.textContent).join('|');
  out.selfCheck = {
    noUndefined: allTxt.indexOf('undefined') < 0,
    noNaN: allTxt.indexOf('NaN') < 0,
    noRawMarkdown: allTxt.indexOf('**') < 0,
    cardCount: document.querySelectorAll('[data-pid^="hil_"]').length,
    cardElHelperWorks: (typeof _hilCardEl === 'function') ? !!_hilCardEl(900001) : false,
    timelineLen: (typeof _procS !== 'undefined' && _procS.timeline) ? _procS.timeline.length : -1
  };

  return JSON.stringify(out);
})()
