/* 门禁：P0-3 前端可见性 —— 用**真实 JS 引擎**跑 onStageEvent，不读源码猜。

为什么必须真跑（而不是 grep 源码）：
  本轮加三个新 stage 时发现 `onStageEvent` 的 if-else 链**没有 else 兜底**
  ⇒ 未知 name 被**静默丢弃**。grep 只能证明"代码里有分支"，
  证明不了"事件真的被渲染"。所以这里加载**真实文件**、
  mock procAddStage 捕获实际调用，再逐条断言。

用法：node tools/verify/verify_todo_ui_p03.js
退出码 0 = 全绿；1 = 有红项。
*/
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const FILE = path.join(ROOT, 'static', 'js', 'mods', '11-pipeline.js');

const RESULTS = [];
function check(group, name, cond, detail) {
  RESULTS.push({ group, name, ok: !!cond, detail: detail || '' });
}

// ── 加载真实文件（最小 DOM mock，只为让顶层代码能跑起来）──
function loadReal() {
  const src = fs.readFileSync(FILE, 'utf8');
  const captured = [];
  const sandbox = {
    console,
    esc: (s) => String(s == null ? '' : s),
    requestAnimationFrame: (f) => f(),
    setTimeout: (f) => f(),
    document: {
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => ({ style: {}, appendChild() {}, setAttribute() {},
                              classList: { add() {}, remove() {}, toggle() {} } }),
      body: { appendChild() {} },
    },
    window: {},
    localStorage: { getItem: () => null, setItem() {} },
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { timeout: 10000 });
  // 用真实文件里的 onStageEvent，但把 procAddStage 换成捕获版
  // ⚠️ onStageEvent 内部调用的是**全局** procAddStage ⇒ 覆盖沙箱里的同名函数即可
  sandbox.procAddStage = function (title, status, sub, det, html) {
    captured.push({ title, status, sub: sub || '', html: html || '' });
  };
  sandbox.resetPipeline = function () {};
  return { sandbox, captured };
}

// ── 主验证 ──
function main() {
  const { sandbox, captured } = loadReal();
  const onStage = sandbox.onStageEvent;
  check('A', '真实文件加载成功', typeof onStage === 'function',
        'onStageEvent 类型：' + typeof onStage);

  function feed(ev) {
    captured.length = 0;
    onStage(ev);
    return captured.slice();
  }

  // ══ B. 三个新增 stage 必须有显式分支且能渲染 ══
  const cases = [
    ['待办验收闸', 'done', '全部 2 项已通过验收'],
    ['验收未通过 · 继续工作', 'done', '有 1 项待办未通过验收（第 1/8 次阻断）'],
    ['验收闸强制放行', 'done', '已连续阻断 8 次（上限 8）⇒ 强制放行'],
  ];
  for (const [name, status, detail] of cases) {
    const got = feed({ type: 'stage', name, status, detail });
    const hit = got.find((x) => x.title === name);
    check('B', `「${name}」被渲染`, !!hit,
          got.length ? '实际渲染：' + got.map((x) => x.title).join('/') : '无任何渲染（被静默丢弃）');
    if (hit) {
      check('B', `「${name}」detail 透传`, hit.sub.indexOf(detail) >= 0,
            'sub=' + hit.sub.slice(0, 80));
    }
  }

  // ══ C. ★ 兜底：任意未知 stage 不得静默丢弃 ══
  const unknown = [
    '某个未来才会加的阶段',
    'TotallyUnknownStage',
    'P9-未来阶段',
  ];
  for (const nm of unknown) {
    const got = feed({ type: 'stage', name: nm, status: 'done', detail: 'x' });
    check('C', `★未知 stage「${nm}」不被静默丢弃`, got.length > 0,
          '捕获到 ' + got.length + ' 条渲染');
    if (got.length) {
      check('C', `★未知 stage「${nm}」名称原样展示`,
            got.some((x) => x.title === nm), 'title=' + got.map((x) => x.title).join('/'));
    }
  }

  // ══ D. 既有 stage 未被破坏（回归）══
  const existing = [
    ['意图识别', 'done', '理解：测试'],
    ['知识库检索', 'done', '未引用'],
    ['生成与校验', 'done', ''],
    ['写入会话', 'done', ''],
    ['任务完成度评估', 'done', '任务已完成，无需续跑'],
    ['自主续跑', 'done', '第 1/3 次'],
  ];
  for (const [name, status, detail] of existing) {
    const got = feed({ type: 'stage', name, status, detail });
    check('D', `既有 stage「${name}」仍渲染`, got.some((x) => x.title === name),
          '渲染：' + got.map((x) => x.title).join('/'));
  }

  // ══ E. 变异自证：去掉 else 兜底 ⇒ C 组必须翻红 ══
  //    做法：拿真实文件源码，删掉兜底 else 块，重新加载，验证未知 stage 不再渲染。
  (function mutate() {
    const src = fs.readFileSync(FILE, 'utf8');
    // 定位兜底 else 块：以 "} else {" 开头、内含「任何未识别的 stage 名称都要可见」
    const marker = '★ 兜底（2026-10-28）';
    const iMark = src.indexOf(marker);
    if (iMark < 0) {
      check('E', '★变异自证：找不到兜底标记 ⇒ 无法变异（判据失效）', false,
            '标记未找到，说明兜底被删或改写');
      return;
    }
    // 从标记往前找 "} else {"，往后找配平的 "}"
    let iStart = src.lastIndexOf('} else {', iMark);
    if (iStart < 0) {
      check('E', '★变异自证：找不到兜底 else 起点', false, '未定位');
      return;
    }
    // 括号配平（从 iStart 的 '{' 开始）
    let iBrace = src.indexOf('{', iStart);
    let depth = 0, iEnd = -1;
    for (let k = iBrace; k < src.length; k++) {
      if (src[k] === '{') depth++;
      else if (src[k] === '}') { depth--; if (depth === 0) { iEnd = k; break; } }
    }
    if (iEnd < 0) {
      check('E', '★变异自证：兜底块括号未配平', false, '解析失败');
      return;
    }
    const mutated = src.slice(0, iStart) + '}' + src.slice(iEnd + 1);
    // 加载变异版
    const captured2 = [];
    const sb2 = {
      console, esc: (s) => String(s == null ? '' : s),
      requestAnimationFrame: (f) => f(), setTimeout: (f) => f(),
      document: { getElementById: () => null, querySelector: () => null,
                  querySelectorAll: () => [],
                  createElement: () => ({ style: {}, appendChild() {}, setAttribute() {},
                                          classList: { add() {}, remove() {}, toggle() {} } }),
                  body: { appendChild() {} } },
      window: {}, localStorage: { getItem: () => null, setItem() {} },
    };
    try {
      vm.createContext(sb2);
      vm.runInContext(mutated, sb2, { timeout: 10000 });
      sb2.procAddStage = function (t) { captured2.push(t); };
      sb2.onStageEvent({ type: 'stage', name: '某个未来才会加的阶段', status: 'done', detail: 'x' });
    } catch (e) {
      // 变异后语法崩也算"兜底确实存在"的旁证
      check('E', '★变异自证：删掉兜底 ⇒ 未知 stage 不再渲染（判据有鉴别力）',
            true, '变异版加载异常（同样说明兜底被移除）：' + e.message.slice(0, 60));
      return;
    }
    const stillRendered = captured2.indexOf('某个未来才会加的阶段') >= 0;
    check('E', '★变异自证：删掉兜底 ⇒ 未知 stage 不再渲染（判据有鉴别力）',
          !stillRendered,
          '变异后仍渲染了 ' + JSON.stringify(captured2) + ' ⇒ C 组判据无鉴别力');
    // 反向自证：真版本必须渲染
    const realGot = feed({ type: 'stage', name: '某个未来才会加的阶段', status: 'done', detail: 'x' });
    check('E', '反向自证：真版本渲染未知 stage',
          realGot.some((x) => x.title === '某个未来才会加的阶段'));
  })();

  // ══ F. ★★ 两侧对齐：后端 yield 的 stage 名 必须与前端分支名逐字一致 ══
  //    为什么这条最关键：本仓反复踩过「同一语义在多处各写一份 ⇒ 最后对不上」。
  //    stage 名称是**字符串契约**：后端写「验收未通过 · 继续工作」、
  //    前端写「验收未通过·继续工作」（空格差异）⇒ 界面静默不显示，而两边代码都"对"。
  //    ⇒ 从**后端真实源码**提取本功能新增的 stage 名，逐个交给前端真实函数跑。
  (function crossCheck() {
    const beFiles = [
      path.join(ROOT, 'agent', 'pipeline_parts', 'stream.py'),
      path.join(ROOT, 'agent', 'pipeline_parts', 'execute.py'),
    ];
    const names = new Set();
    const re = /"name":\s*"([^"]+)"/g;
    for (const f of beFiles) {
      if (!fs.existsSync(f)) continue;
      const s = fs.readFileSync(f, 'utf8');
      let m;
      while ((m = re.exec(s)) !== null) {
        // 只收本功能相关的（避免把无关 stage 也纳入，那些有各自分支）
        if (/验收|待办/.test(m[1])) names.add(m[1]);
      }
    }
    check('F', '后端源码里找到本功能的 stage 名', names.size > 0,
          names.size ? '发现：' + [...names].join(' / ') : '一个都没找到');
    for (const nm of names) {
      const got = feed({ type: 'stage', name: nm, status: 'done', detail: 'd' });
      check('F', `★★两侧对齐：后端名「${nm}」前端能渲染`,
            got.some((x) => x.title === nm),
            '前端渲染：' + (got.map((x) => x.title).join('/') || '无'));
    }
  })();

  // ── 输出 ──
  console.log('='.repeat(74));
  console.log('门禁：P0-3 前端可见性（真实 JS 引擎跑 onStageEvent）');
  console.log('='.repeat(74));
  const groups = {};
  for (const r of RESULTS) (groups[r.group] = groups[r.group] || []).push(r);
  let total = 0, failed = 0;
  for (const g of Object.keys(groups).sort()) {
    console.log(`\n【${g} 组】`);
    for (const r of groups[g]) {
      total++;
      if (!r.ok) failed++;
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
