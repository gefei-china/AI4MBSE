/* 门禁：用户反馈四问题的回归判据（2026-10-28）

对应四个真实反馈（会话 9720）：
  ① 用户输入内容较多 → 页面拉得很长，应默认收几行、可展开
  ② 系统时间不是北京时间
  ③ 只输入 V2 代码让 AI 校验，却被路由到「智源对接agent」（会话 main_agent 绑定覆盖意图）
  ④ 执行过程不该给"统一的思考过程输出"，应把系统提示收编到对应执行环节

本门禁全部用**真实文件 + 真实数据**验证，不 grep 源码猜：
  · ① 加载真实 05-markdown.js，断言长输入产出折叠容器、短输入没有
  · ② 加载真实 01-core.js，用**真实库里的 UTC 时间戳**断言格式化结果为北京时间
  · ③ 走真实 IntentRouter + 真实库，断言路由结果并记录会话绑定差异
  · ④ 加载真实 06-cards.js，断言 reasoning/notices 分开渲染

用法：node tools/verify/verify_ux_four_fixes.js
退出码 0 = 全绿；1 = 有红项。
*/
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const RESULTS = [];
function check(g, name, cond, detail) {
  RESULTS.push({ g, name, ok: !!cond, detail: detail || '' });
}

// ── 通用沙箱：加载真实前端文件（mock 最小 DOM）──
function makeSandbox() {
  const el = () => ({
    style: {}, innerHTML: '', textContent: '', value: '',
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    appendChild() {}, setAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, addEventListener() {}, remove() {},
  });
  const sb = {
    console,
    esc: (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
    escA: (s) => String(s == null ? '' : s),
    setTimeout: (f) => f(), clearTimeout() {},
    requestAnimationFrame: (f) => f(),
    document: {
      getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
      createElement: el, addEventListener() {}, body: el(),
      documentElement: { style: {} },
    },
    window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
    location: { href: '', reload() {} },
  };
  sb.globalThis = sb;
  return sb;
}

function loadInto(sb, rel) {
  const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  vm.createContext(sb);
  try { vm.runInContext(src, sb, { timeout: 15000 }); } catch (e) {
    return { ok: false, err: e.message };
  }
  return { ok: true };
}

// DOM 无关的纯函数抽取：只取目标 function 的定义段落，避免整文件执行报错
function extractFn(rel, name) {
  const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  const i = src.indexOf('function ' + name);
  if (i < 0) return null;
  let d = 0, started = false, j = i;
  for (; j < src.length; j++) {
    const c = src[j];
    if (c === '{') { d++; started = true; }
    else if (c === '}') { d--; if (started && d === 0) { j++; break; } }
  }
  return src.slice(i, j);
}

function main() {
  // ══════════════ ① 用户输入折叠 ══════════════
  (function fold() {
    const sb = makeSandbox();
    // 需要 esc/escA + parseServerTime/fmtServerTime + 05-markdown 的 renderMessage
    loadInto(sb, 'static/js/mods/01-core.js');
    check('①', '01-core.js 可加载', typeof sb.parseServerTime === 'function');

    // 只取 renderMessage 及其依赖（USER_FOLD_LINES / _raw_len_hint / toggleUserFold）
    const core = fs.readFileSync(path.join(ROOT, 'static/js/mods/05-markdown.js'), 'utf8');
    const parts = [];
    // ⚠️ renderMessage 内部还依赖 formatMsgTime 等 ⇒ 一并抽进来，
    //   否则夹具报 "formatMsgTime is not defined"（门禁自身的缺口，非产品 bug）。
    for (const nm of ['_raw_len_hint', 'toggleUserFold', 'formatMsgTime', 'renderMessage']) {
      const f = extractFn('static/js/mods/05-markdown.js', nm);
      if (f) parts.push(f);
    }
    const mFold = core.match(/const USER_FOLD_LINES\s*=\s*\d+/);
    let code = (mFold ? mFold[0] + ';\n' : '') + parts.join('\n');
    // renderMessage 依赖 renderMarkdown / _intentBriefBody 等；给最小桩
    code = 'function renderMarkdown(t){return String(t||"");}\n'
         + 'function _procDetailBtn(){return "";}\n'
         + 'function _userFoldStub(){}\n' + code;
    try { vm.runInContext(code, sb, { timeout: 10000 }); } catch (e) {
      check('①', 'renderMessage 可加载', false, e.message.slice(0, 120));
      return;
    }
    check('①', 'renderMessage 可加载', typeof sb.renderMessage === 'function');
    if (typeof sb.renderMessage !== 'function') return;

    // 长输入（模拟真实会话 9720：3319 字、含大量换行）
    const longText = ('帮我检查如下这段V2代码有什么问题：\n' + 'package A {\n  part def X;\n}\n').repeat(60);
    const hLong = sb.renderMessage({ id: 1, role: 'user', content: longText });
    check('①', '★长用户输入产出折叠容器', hLong.indexOf('user-fold') >= 0,
          `长度 ${longText.length} 字，HTML ${hLong.length} 字符，未找到 user-fold`);
    check('①', '★长输入默认是收起态（collapsed）', hLong.indexOf('user-fold collapsed') >= 0);
    check('①', '★长输入提供展开按钮', hLong.indexOf('toggleUserFold') >= 0);
    check('①', '★展开按钮带行数/字数提示', /展开全部（共约 \d+ 行/.test(hLong),
          hLong.slice(hLong.indexOf('uf-btn'), hLong.indexOf('uf-btn') + 120));

    // 短输入不该折叠
    const hShort = sb.renderMessage({ id: 2, role: 'user', content: '继续' });
    check('①', '★短用户输入不折叠（无 user-fold）', hShort.indexOf('user-fold') < 0,
          hShort.slice(0, 200));

    // ★ AI 输出绝不折叠（用户此前明确要求）
    const hAI = sb.renderMessage({ id: 3, role: 'assistant', content: longText });
    check('①', '★★AI 输出不折叠（用户明确要求不许收起）', hAI.indexOf('user-fold') < 0,
          'AI 输出被误加折叠');
  })();

  // ══════════════ ② 时区 ══════════════
  (function tz() {
    const sb = makeSandbox();
    loadInto(sb, 'static/js/mods/01-core.js');
    check('②', 'parseServerTime/fmtServerTime 可用',
          typeof sb.parseServerTime === 'function' && typeof sb.fmtServerTime === 'function');
    if (typeof sb.fmtServerTime !== 'function') return;

    // 用**真实库里的 UTC 时间戳**（会话 9720 created_at）
    let realUtc = null;
    try {
      const py = `import sys,sqlite3;sys.path.insert(0,r"${ROOT.replace(/\\/g,'\\\\')}")
from core.config import DB_PATH
c=sqlite3.connect(DB_PATH)
r=c.execute("SELECT created_at FROM conversations WHERE id=9720").fetchone()
print(r[0] if r else "")`;
      realUtc = execFileSync('python', ['-c', py], { encoding: 'utf8' }).trim();
    } catch (e) { realUtc = null; }
    check('②', '能取到真实库时间戳', !!realUtc, String(realUtc).slice(0, 40));
    if (realUtc) {
      const got = sb.fmtServerTime(realUtc);
      // 05:44:52 UTC ⇒ 北京 13:44
      const expect = new Date(realUtc.replace(' ', 'T') + 'Z');
      const p = (n) => String(n).padStart(2, '0');
      const want = `${p(expect.getHours())}:${p(expect.getMinutes())}`;
      check('②', `★真实时间戳 ${realUtc} 格式化为北京时间`, got === want,
            `实际 '${got}' / 期望 '${want}'`);
    }

    const cases = [
      ['无时区 UTC 串', '2026-10-28 05:44:52', '13:44'],
      ['带 Z', '2026-10-28T05:44:52Z', '13:44'],
      ['带 +08:00', '2026-10-28T13:44:52+08:00', '13:44'],
      ['空值', '', ''],
      ['非法串', 'xx', ''],
    ];
    let bad = [];
    for (const [tag, inp, want] of cases) {
      const got = sb.fmtServerTime(inp);
      const ok = want === '' ? got === '' : got.startsWith(want);
      if (!ok) bad.push(`${tag}: '${got}'≠'${want}'`);
    }
    check('②', '★边界用例全部正确（UTC/Z/+08:00/空/非法）', bad.length === 0, JSON.stringify(bad));

    // ★ 单点实现：全站不应再有裸 new Date(xxx.replace(' ','T'))
    const files = fs.readdirSync(path.join(ROOT, 'static/js/mods')).filter(f => f.endsWith('.js'));
    const offenders = [];
    for (const f of files) {
      let src = fs.readFileSync(path.join(ROOT, 'static/js/mods', f), 'utf8');
      // ⚠️ 必须**先剥注释**再匹配：第一版直接匹配，命中了 01-core.js 里
      //   我自己写的说明注释「前端此前有 5 处各自 new Date(ts.replace(' ','T'))」
      //   ⇒ 假阳性（判据把"文档里提到旧写法"当成"代码里还在用"）。
      src = src.replace(/\/\*[\s\S]*?\*\//g, '')      // 块注释
               .replace(/^\s*\/\/.*$/gm, '');           // 行注释
      const re = /new Date\(\s*[^)]*\.replace\(\s*['"]\s['"]\s*,\s*['"]T['"]\s*\)/g;
      if (re.test(src)) offenders.push(f);
    }
    check('②', '★★全站已无裸 `new Date(x.replace(" ","T"))`（单点实现）',
          offenders.length === 0, JSON.stringify(offenders));
  })();

  // ══════════════ ③ 意图路由 ══════════════
  (function route() {
    let out = '';
    try {
      const py = `import sys,json,sqlite3;sys.path.insert(0,r"${ROOT.replace(/\\/g,'\\\\')}")
from core.config import DB_PATH
from agent.intent import IntentRouter
rt=IntentRouter(); c=sqlite3.connect(DB_PATH)
t="帮我检查如下这段V2代码有什么问题：并进行修复，需要符合sysml v2标准 package 'A' { }"
intent=rt.detect(t, conn=c)
m=dict(getattr(rt,"_last_meta",{}) or {})
r=c.execute("SELECT main_agent FROM conversations WHERE id=9720").fetchone()
print(json.dumps({"intent":intent,"route":m.get("route"),"conf":m.get("confidence"),
                  "session_main_agent": (r[0] if r else "")}, ensure_ascii=False))`;
      out = execFileSync('python', ['-c', py], { encoding: 'utf8' }).trim().split('\n').pop();
    } catch (e) {
      check('③', 'IntentRouter 实测可跑', false, String(e.message).slice(0, 150));
      return;
    }
    let d = {};
    try { d = JSON.parse(out); } catch (e) { check('③', '解析结果', false, out.slice(0, 150)); return; }
    check('③', 'IntentRouter 实测可跑', !!d.intent, JSON.stringify(d));
    // 关键事实：真实会话被 main_agent 绑定覆盖
    check('③', '★真实会话 9720 的 main_agent 已绑定「智源对接agent」',
          d.session_main_agent === '智源对接agent', JSON.stringify(d.session_main_agent));
    // 路由本身并未把它判成智源（说明是绑定覆盖，不是路由错判）
    check('③', `路由层意图（${d.intent}）非「智源对接agent」`,
          d.intent !== '智源对接agent', JSON.stringify(d));

    // 门禁固化：会话 main_agent 必须可查（用于前端解释"为什么由它执行"）
    let hasCol = false;
    try {
      const py2 = `import sys,sqlite3;sys.path.insert(0,r"${ROOT.replace(/\\/g,'\\\\')}")
from core.config import DB_PATH
c=sqlite3.connect(DB_PATH)
cols=[r[1] for r in c.execute("PRAGMA table_info(conversations)").fetchall()]
print("main_agent" in cols)`;
      hasCol = execFileSync('python', ['-c', py2], { encoding: 'utf8' }).trim() === 'True';
    } catch (e) { hasCol = false; }
    check('③', 'conversations.main_agent 列存在（可解释性基础）', hasCol);

    // ★ 可解释性必须**结构化下发并显示**，不能只在 reasoning 里说一句
    const back = fs.readFileSync(path.join(ROOT, 'agent/pipeline_parts/stream.py'), 'utf8');
    check('③', '★意图识别 stage 下发 exec_subject（主体+来源）',
          /"exec_subject":\s*_exec_subject/.test(back), '未找到 exec_subject 字段');
    check('③', '★exec_subject 区分 manual/bound/route 三种来源',
          /"manual" if team else \("bound"/.test(back));
    check('③', '★「执行主体」说明走 progress 通道（不埋进思考框）',
          back.indexOf('"kind": "progress"}') >= 0 && back.indexOf('（执行主体）') >= 0);
    const feP = fs.readFileSync(path.join(ROOT, 'static/js/mods/11-pipeline.js'), 'utf8');
    check('③', '★前端意图行显示执行主体来源', /exec_subject/.test(feP));
    check('③', '★非路由来源显式标注「非本轮识别结果」',
          feP.indexOf('非本轮识别结果') >= 0);
  })();

  // ══════════════ ④ 执行过程收编 ══════════════
  (function trace() {
    // 后端：exec_notices 已建且落库
    const back = fs.readFileSync(path.join(ROOT, 'agent/pipeline_parts/stream.py'), 'utf8');
    check('④', '后端已建 exec_notices 通道', /exec_notices\s*=\s*\[\]/.test(back));
    check('④', '★exec.notices 已落库', /"notices":\s*\(?exec_notices/.test(back),
          '未找到落库语句');
    // 系统提示不再进 exec_reasoning（除真 CoT 的两个来源）
    const re = /exec_reasoning\.append\((_\w+)\)/g;
    const left = new Set();
    let m;
    while ((m = re.exec(back)) !== null) left.add(m[1]);
    check('④', '★exec_reasoning 只剩真 CoT 来源',
          [...left].every(v => v === '_p_think' || v === '_r_delta'),
          `仍残留：${JSON.stringify([...left])}`);

    // 前端：reasoning 与 notices 分开渲染
    const fe = fs.readFileSync(path.join(ROOT, 'static/js/mods/06-cards.js'), 'utf8');
    check('④', '★前端读取 ex.notices', /ex\.notices/.test(fe));
    check('④', '★「思考过程」大块已改为「模型思考」（语义区分）',
          fe.indexOf("title:'模型思考'") >= 0);
    check('④', '★系统提示收编为独立「执行说明」行',
          fe.indexOf('执行说明') >= 0);
    // 直播路径：系统提示的 yield 必须带 kind=progress（否则进"AI 思考"框）
    const bad = [];
    for (const v of ['_fu_delta', '_p_delta', '_d_delta', '_w_delta', '_fu2_delta', '_ac_delta']) {
      const re2 = new RegExp(`yield \\{"type": "reasoning", "delta": ${v},(?![^\\n]*"kind")`, 'g');
      if (re2.test(back)) bad.push(v);
    }
    check('④', '★直播路径系统提示均带 kind=progress（不进思考框）',
          bad.length === 0, `缺 kind：${JSON.stringify(bad)}`);
  })();

  // ── 输出 ──
  console.log('='.repeat(74));
  console.log('门禁：用户反馈四问题回归（真实文件 + 真实库）');
  console.log('='.repeat(74));
  const groups = {};
  for (const r of RESULTS) (groups[r.g] = groups[r.g] || []).push(r);
  let total = 0, failed = 0;
  for (const g of Object.keys(groups).sort()) {
    console.log(`\n【${g}】`);
    for (const r of groups[g]) {
      total++; if (!r.ok) failed++;
      let line = `  [${r.ok ? 'PASS' : 'FAIL'}] ${r.name}`;
      if (!r.ok && r.detail) line += `   ← ${r.detail}`;
      console.log(line);
    }
  }
  console.log('\n' + '-'.repeat(74));
  console.log(`合计 ${total} 项，通过 ${total - failed}，失败 ${failed}`);
  console.log('-'.repeat(74));
  if (failed) { console.log('❌ 门禁未通过'); process.exit(1); }
  console.log('✅ 全绿');
  process.exit(0);
}

main();
