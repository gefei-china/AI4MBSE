/* 回归判据 · 能力中心 MCP 卡片渲染（2026-11-06）
 * 从真实源文件抽取 capCard 函数，在 vm 里传入一个 _from_server 的 MCP 项，
 * 断言渲染出：测试/发现入口、在线徽章、凭据掩码、工具数、MCP 服务标签。
 * 用法：node tools/verify/verify_mcp_capcard.js
 */
const fs = require('fs'), vm = require('vm'), path = require('path');
const FILE = path.resolve(__dirname, '../../static/js/mods/36-capability.js');
const SRC = fs.readFileSync(FILE, 'utf8');
const MARK = 'function capCard(';
const start = SRC.indexOf(MARK);
if (start < 0) throw new Error('未找到 capCard');
// capCard 内含嵌套模板字符串 + 文件是 CRLF，括号计数/行首 `}` 都不可靠；
// 用「下一个顶层函数声明」作为边界（中间夹的只是注释，不影响求值）。
const rest = SRC.slice(start);
const m = rest.match(/\r?\n(?:async\s+)?function\s+\w+\s*\(/);
if (!m) throw new Error('未找到 capCard 之后的顶层函数边界');
const FN = SRC.slice(start, start + m.index);

const ctx = {
  CAP_META: { mcp: { abbr: 'M', label: 'MCP', color: '#0C447C' } },
  capLabel: x => x.label || x.name,
  _capPid: x => 'mcp:' + x.name,
  esc: s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;'),
  capMoreBtn: () => '',
  _capNameByPid: () => '',
  console,
};
vm.createContext(ctx);
vm.runInContext(FN + '\n;this.__capCard = capCard;', ctx);

const x = {
  type: 'mcp', name: 'zhiyuan-dev-mcp', label: 'zhiyuan-dev-mcp',
  description: 'http://xuanyi-dev.huilingyun.com/mcp',
  installed: true, enabled: 1, is_builtin: false, is_mine: true, can_edit: true,
  status: 'online', current_version: 'v1', is_public: false,
  _from_server: true, legacy_id: 161,
  mcp_online: true, last_check: '2026-11-06 05:40:38', last_error: '',
  latency_ms: 637, tool_count: 71,
  headers_masked: { Authorization: { set: true, len: 92 } },
  can_publish_self: false, can_share: false, can_cancel_share: false,
  can_delete: false, pending_share: false,
};
const html = ctx.__capCard(x, 'mine', 'st-mcp');
const checks = {
  '测试/发现按钮': html.indexOf('测试/发现') >= 0 && html.indexOf('testMCP(161)') >= 0,
  '在线徽章': html.indexOf('● 在线') >= 0,
  '凭据掩码 ******': html.indexOf('******') >= 0 && html.indexOf('Authorization') >= 0,
  '工具数 71': html.indexOf('71 工具') >= 0,
  '巡检时间': html.indexOf('巡检 11-06 05:40') >= 0,
  'MCP 服务标签（不再显示发布态）': html.indexOf('MCP 服务') >= 0,
};
// 离线态必须显示错误摘要，且徽章变离线
const offline = ctx.__capCard({ ...x, mcp_online: false, status: 'offline', last_error: 'HTTP 401 invalid_token' }, 'mine', 'st-mcp');
checks['离线态显示 401 摘要'] = offline.indexOf('○ 离线') >= 0 && offline.indexOf('401') >= 0;
// 非 MCP 卡片不得出现 mcpMeta（防止污染其它能力类型）
const plain = ctx.__capCard({ ...x, _from_server: false }, 'mine', 'st-mcp');
checks['非 MCP 卡片不注入 mcpMeta'] = plain.indexOf('capmcpmeta') < 0;

let ok = true;
for (const k of Object.keys(checks)) { console.log((checks[k] ? 'PASS  ' : 'FAIL  ') + k); if (!checks[k]) ok = false; }
console.log('ALL_OK=' + ok);
process.exit(ok ? 0 : 1);
