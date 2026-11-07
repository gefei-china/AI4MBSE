/* 回归判据 · MCP 编辑页三处优化（2026-11-06）
 *
 * ⚠️ 本脚本**取代** `verify_mcp_headers_edit.js`（后者已删除）。映射关系：
 *   旧 A「编辑态全部留空 ⇒ 不提交 headers」  → 新 ①③（masked 行 / edit 留空）
 *   旧 B「部分留空 ⇒ fail-fast 不保存」      → **语义已变**：edit 行留空 = 放弃修改（合法），
 *                                              只有 **new** 行半填才拦截 → 新 ③⑥
 *   旧 C「✕ 删整行 ⇒ 提交空 headers」        → **语义已变**：改为精确 `header_delete`，
 *                                              避免"整体重写"丢掉其它头的明文 → 新 ④
 *   旧 D「新增态填 Bearer ⇒ 正常写入」        → 新 ⑤
 *   删掉而不是改名的理由：旧断言绑在"空值输入框 + 留空则保持原值"这套**已废弃的交互**上，
 *   保留会让它对新实现恒假红（实测 4/4 ERROR），反而掩盖真实回归。
 *   保护没丢——每条旧断言都在这份新脚本里有对应项，而且新脚本断言**实际提交的 body**，更强。
 *
 * 用户三条反馈对应的锁定项：
 *  ① 「执行通道：空着的，没作用就移除」→ MCP 类型必须隐藏 `#tool-channel-box`
 *     （根因：`renderToolChannel()` 只在工具编辑里调用，`editMCP()` 从不调用 ⇒ 永远空白）
 *  ② 「header 能加多条吗？为啥还有个『添加』；已添加了还有输入框提示『留空则保持原值』」
 *     → 支持多条（＋ 添加一条）；已保存行是**只读 masked 态**（无输入框），点「改」才出输入框
 *  ③ 「还是没看到测试入口」→ 编辑弹窗内必须有「测试连通」按钮 + 生效状态
 *     （此前只加在能力中心卡片上，用户是在编辑弹窗里找）
 *
 * 从真实源文件抽取函数在 vm 里跑，断言**实际提交的 body**（不是字符串包含）。
 * 用法：node tools/verify/verify_mcp_form_ux.js
 */
const fs = require('fs'), vm = require('vm'), path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const SRC = fs.readFileSync(path.join(ROOT, 'static/js/mods/30-agents.js'), 'utf8');
const CORE = fs.readFileSync(path.join(ROOT, 'static/js/mods/01-core.js'), 'utf8');

// 用「下一个顶层函数」作边界（文件是 CRLF 且函数内含嵌套模板字符串，括号计数不可靠）
function extractFn(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('函数未找到: ' + marker);
  const rest = src.slice(start);
  const m = rest.match(/\r?\n(?:async\s+)?function\s+\w+\s*\(/);
  if (!m) throw new Error('未找到边界: ' + marker);
  return src.slice(start, start + m.index);
}

const FN_SAVE = extractFn(SRC, 'async function saveTool(');
const FN_ROW = extractFn(SRC, 'function _hdrRow(');

const VALUES = {
  'f-tool-name': 'zhiyuan-dev-mcp',
  'f-tool-desc': '',
  'f-tool-type': 'mcp',
  'f-tool-url-mcp': 'http://xuanyi-dev.huilingyun.com/mcp',
  'f-tool-transport': 'http',
};

// 假 DOM：rows 是 [{mode, key, val}]
function makeCtx(editId, rows) {
  const calls = [], toasts = [];
  const mkRow = r => ({
    dataset: { mode: r.mode, key: r.key || '' },
    querySelector: sel => {
      if (sel === '.tool-hdr-key') return r.mode === 'new' ? { value: r.key || '' } : null;
      if (sel === '.tool-hdr-val') return r.mode === 'new' || r.mode === 'edit' ? { value: r.val || '' } : null;
      return null;
    },
  });
  const ctx = {
    document: {
      getElementById: id => ({ value: VALUES[id] !== undefined ? VALUES[id] : '', disabled: false }),
      querySelectorAll: sel => sel.indexOf('hdr-row') >= 0 ? rows.map(mkRow) : [],
    },
    toolEditId: editId,
    toolFormMode: () => 'mcp',
    api: async (url, opt) => { calls.push({ url, body: opt && opt.body }); return { ok: true }; },
    toast: m => toasts.push(String(m)),
    closeModal: () => {}, loadMCPServers: () => {}, loadMCPCombined: undefined,
    loadMinePlugins: () => {}, loadMarket: () => {}, loadMarketManage: () => {},
    _hdrDeleted: rows.filter(r => r.deleted).map(r => r.key).filter(Boolean),
    console,
  };
  vm.createContext(ctx);
  vm.runInContext(FN_SAVE + '\n;this.__save = saveTool;', ctx);
  return { ctx, calls, toasts };
}

function putBody(calls, id) {
  const c = calls.find(x => x.url.indexOf('/api/studio/mcp-servers/' + id) >= 0);
  if (!c) return null;
  try { return JSON.parse(c.body); } catch (e) { return null; }
}

const results = [];
const check = (name, ok, extra) => results.push({ name, ok: !!ok, extra: extra || '' });

(async () => {
  // ① masked 行（已保存、未动）→ 不提交 headers，也不提交 header_delete
  {
    const { calls } = makeCtx(161, [{ mode: 'masked', key: 'Authorization' }]);
    await (calls, 0);
  }
  {
    const { ctx, calls } = makeCtx(161, [{ mode: 'masked', key: 'Authorization' }]);
    await ctx.__save();
    const b = putBody(calls, 161);
    check('① masked 行不动 ⇒ body 不带 headers', b && !('headers' in b), b ? Object.keys(b).join(',') : 'no put');
    check('① masked 行不动 ⇒ 不带 header_delete', b && !('header_delete' in b));
  }
  // ② edit 行填了新值 → 提交 {Authorization: 新值}，且不误删
  {
    const { ctx, calls } = makeCtx(161, [{ mode: 'edit', key: 'Authorization', val: 'Bearer NEW' }]);
    await ctx.__save();
    const b = putBody(calls, 161);
    check('② 改值 ⇒ headers 带新值', b && b.headers && b.headers.Authorization === 'Bearer NEW',
      b && b.headers ? JSON.stringify(b.headers) : 'none');
  }
  // ③ edit 行留空 → 等同未改（不提交该项）
  {
    const { ctx, calls } = makeCtx(161, [{ mode: 'edit', key: 'Authorization', val: '' }]);
    await ctx.__save();
    const b = putBody(calls, 161);
    check('③ 改但留空 ⇒ 不提交该 key', b && (!b.headers || !b.headers.Authorization),
      b && b.headers ? JSON.stringify(b.headers) : 'no headers');
  }
  // ④ 删除已保存的头 → 走 header_delete（不能整体重写，否则丢明文）
  {
    const { ctx, calls } = makeCtx(161, [{ mode: 'masked', key: 'X-Extra', deleted: true }]);
    await ctx.__save();
    const b = putBody(calls, 161);
    check('④ 删除已保存头 ⇒ 提交 header_delete', b && Array.isArray(b.header_delete) && b.header_delete.indexOf('X-Extra') >= 0,
      b ? JSON.stringify(b.header_delete || null) : 'no put');
    check('④ 删除时不得提交空 headers（防清空其它凭据）', b && !('headers' in b));
  }
  // ⑤ 新增行填全 → 提交
  {
    const { ctx, calls } = makeCtx(161, [{ mode: 'new', key: 'X-Api-Key', val: 'k123' }]);
    await ctx.__save();
    const b = putBody(calls, 161);
    check('⑤ 新增行填全 ⇒ 提交', b && b.headers && b.headers['X-Api-Key'] === 'k123');
  }
  // ⑥ 新增行只填一半 → fail-fast，不保存
  {
    const { ctx, calls, toasts } = makeCtx(161, [{ mode: 'new', key: 'X-Half', val: '' }]);
    await ctx.__save();
    check('⑥ 半填行 ⇒ 不发起保存', calls.length === 0, calls.length);
    check('⑥ 半填行 ⇒ 有提示', toasts.some(t => t.indexOf('只填了一半') >= 0), toasts.join('|'));
  }

  // ⑦ 静态结构：执行通道隐藏 + 测试入口 + 掩码文案
  const stripComments = s => s.replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\*.*$/gm, '').replace(/\/\/.*$/gm, '');
  const SRC_CODE = stripComments(SRC);

  check('⑦ toggleToolType 对 MCP 隐藏执行通道',
    /show\('tool-channel-box',\s*!builtin\s*&&\s*mode\s*!==\s*'mcp'\)/.test(SRC));
  check('⑦ 编辑弹窗有测试按钮', CORE.indexOf('f-mcp-test-btn') >= 0 && CORE.indexOf('testMcpFromForm') >= 0);
  check('⑦ 编辑弹窗有生效状态容器', CORE.indexOf('f-mcp-status') >= 0);
  // 只在**非注释代码**里查这句旧提示 —— 注释中保留历史说明是正确的
  check('⑦ 代码中不再出现「留空则保持原值」这句旧提示', SRC_CODE.indexOf('留空则保持原值') < 0);
  check('⑦ 有「＋ 添加一条」入口（支持多条）', CORE.indexOf('addToolHeader') >= 0 && CORE.indexOf('添加一条') >= 0);
  // 三态：masked / edit 是显式分支，new 是 else 兜底（不算缺失）
  check('⑦ _hdrRow 具备三态分支',
    FN_ROW.indexOf("'masked'") >= 0 && FN_ROW.indexOf("'edit'") >= 0
    && /}\s*else\s*{/.test(FN_ROW),
    'masked=' + (FN_ROW.indexOf("'masked'") >= 0) + ' edit=' + (FN_ROW.indexOf("'edit'") >= 0)
    + ' else=' + /}\s*else\s*{/.test(FN_ROW));
  check('⑦ edit 行留空不算半填（判据只在 new 行生效）',
    /if\s*\(\s*mode\s*===\s*'new'\s*&&\s*\(key\s*\|\|\s*val\)\s*\)/.test(SRC));

  let ok = true;
  for (const r of results) {
    console.log((r.ok ? 'PASS  ' : 'FAIL  ') + r.name + (r.extra ? ' ⇒ ' + r.extra : ''));
    if (!r.ok) ok = false;
  }
  console.log('ALL_OK=' + ok);
  process.exit(ok ? 0 : 1);
})();
