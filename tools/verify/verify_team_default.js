/**
 * 验证：AI 会话入口默认选中排序第一的团队（2026-09-29 Task #2）。
 *
 * 被验证对象：static/js/mods/13-reports.js loadQuickBar() 内的团队下拉决策逻辑（原文提取）。
 * 决策规则：
 *   ① localStorage 无记录（null）→ 默认选 teams[0]（/api/studio/agent-teams 按 agents.id 升序）；
 *   ② 有记录且是有效团队名 → 尊重用户选择；
 *   ③ 有记录且是空串（用户主动选过"自动"）→ 尊重，保持不指定；
 *   ④ 有记录但名字已失效（团队被删）→ 回落 teams[0]。
 * 运行：node tools/verify/verify_team_default.js
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..', '..');
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log(`  [PASS] ${name}${detail ? '  ' + detail : ''}`); }
  else { fail++; console.log(`  [FAIL] ${name}${detail ? '  ' + detail : ''}`); }
}

const src = fs.readFileSync(path.join(ROOT, 'static/js/mods/13-reports.js'), 'utf8');
const i = src.indexOf('let lastTeam = null;');
const j = src.indexOf('if(!tSel.dataset.lastBind)');
if (i < 0 || j < 0 || j <= i) { console.log('[FAIL] 决策块提取失败——源码结构变了，同步锚点'); process.exit(1); }
const code = src.slice(i, j);

const TEAMS = [{ name: 'team-alpha', display_name: 'Alpha 团队' }, { name: 'team-beta', display_name: 'Beta 团队' }];
function decide(lastVal) {
  const tSel = { value: '', title: '', dataset: {} };
  const ls = { getItem: () => lastVal };
  new Function('teams', 'tSel', 'localStorage', code)(TEAMS, tSel, ls);
  return tSel.value;
}

console.log('团队下拉默认选中决策');
check('① 无记录 → 默认选第一个团队', decide(null) === 'team-alpha', `got=${decide(null)}`);
check('② 有效记录 → 尊重用户选择', decide('team-beta') === 'team-beta', `got=${decide('team-beta')}`);
check('③ 空串记录（主动选过"自动"）→ 保持不指定', decide('') === '', `got=${decide('')}`);
check('④ 失效记录 → 回落第一个团队', decide('deleted-team') === 'team-alpha', `got=${decide('deleted-team')}`);
check('⑤ teams 为空时决策块不崩（value 保持空）', (() => {
  const tSel = { value: '', dataset: {} };
  new Function('teams', 'tSel', 'localStorage', code)([], tSel, { getItem: () => null });
  return tSel.value === '';
})());

// 静态断言：change 监听持久化用户选择（幂等绑定）
check('⑥ 用户改动会持久化到 mbse_last_team', src.includes("localStorage.setItem('mbse_last_team'"));
check('⑦ 绑定幂等（dataset.lastBind 防重复）', /if\(!tSel\.dataset\.lastBind\)\{[\s\S]{0,200}dataset\.lastBind = '1'/.test(src));

console.log(`\nPASS ${pass} / ${pass + fail}`);
process.exit(fail ? 1 : 0);
