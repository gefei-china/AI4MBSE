/**
 * 验证：AI 输出期间「贴底才跟随」滚动策略（2026-09-29 Task #4）。
 *
 * 被验证对象（生产代码原文，非复刻）：
 *   static/js/mods/03-chat.js  的 _STICK_EPS/_isNearBottom/_bindStickDetach/stickBottom/scrollChatToBottom
 *   static/js/mods/12-chatsend.js 的 _chatInputAutoGrow
 *
 * 方法：从源文件按函数边界提取原文，在 mock DOM 沙箱里 eval 后跑断言；
 *      再做进程内变异（删掉 stickBottom 的跟读守卫）验证断言非空转。
 * 运行：node tools/verify/verify_chat_scroll.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log(`  [PASS] ${name}${detail ? '  ' + detail : ''}`); }
  else { fail++; console.log(`  [FAIL] ${name}${detail ? '  ' + detail : ''}`); }
}

// ── 极简 DOM mock ─────────────────────────────────────────────
function makeArea() {
  const listeners = {};
  const el = {
    scrollHeight: 1000, clientHeight: 400, // 初始不在底部（差 600）
    _st: 0,
    dataset: {},
    // 真实浏览器语义：scrollTop 被钳制在 [0, scrollHeight - clientHeight]
    get scrollTop() { return this._st; },
    set scrollTop(v) { this._st = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    _fire(type) { (listeners[type] || []).forEach(fn => fn({ type })); },
  };
  return el;
}
function makeDoc(area) {
  return { getElementById: (id) => (id === 'chat-area' ? area : null) };
}
// 同步 rAF：注册即执行（合批逻辑每帧一次的语义在同步环境下仍可断言）
function makeRafStub() {
  const st = { count: 0 };
  st.fn = (cb) => { st.count++; cb(); return st.count; };
  st.cancel = () => {};
  return st;
}

// ── 从源文件提取函数原文 ───────────────────────────────────────
function extract(src, startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  if (i < 0) throw new Error('未找到起始标记: ' + startMarker);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error('未找到结束标记: ' + endMarker);
  return src.slice(i, j);   // 结束标记**排除**在外
}

const chatSrc = fs.readFileSync(path.join(ROOT, 'static/js/mods/03-chat.js'), 'utf8');
const stickCode = extract(chatSrc, 'const _STICK_EPS', 'function scrollChatToBottom()');
// scrollChatToBottom 的函数体到它的第一个换行大括号结束 —— 手动补齐：
const scStart = chatSrc.indexOf('function scrollChatToBottom()');
const scEnd = chatSrc.indexOf('}', chatSrc.indexOf('area.scrollTop = area.scrollHeight;', scStart)) + 1;
const scCode = chatSrc.slice(scStart, scEnd);
const fullCode = stickCode + '\n' + scCode;

// ── 用例 1：不在底部 + 用户上滑 → 脱离跟读，stickBottom 不拽回 ──
console.log('用例 1：贴底才跟随');
{
  const area = makeArea();
  const raf = makeRafStub();
  const fn = new Function('document', 'requestAnimationFrame', 'cancelAnimationFrame', fullCode + `
    return {_isNearBottom, _bindStickDetach, stickBottom, scrollChatToBottom,
            getFollow: () => _stickFollow, setFollow: (v) => { _stickFollow = v; }};`);
  const api = fn(makeDoc(area), raf.fn, raf.cancel);

  area.scrollTop = 200; // 距底 400 > 48
  check('A1 距底 400px 时 _isNearBottom=false', api._isNearBottom(area) === false);
  api._bindStickDetach();
  check('A2 绑定后 dataset.stickBind 置位', area.dataset.stickBind === '1');
  area._fire('wheel'); // 用户上滑
  check('A3 wheel 后跟读态脱离', api.getFollow() === false);
  area.scrollTop = 100;
  api.stickBottom();
  check('A4 脱离跟读后 stickBottom 不改变 scrollTop', area.scrollTop === 100,
    `scrollTop=${area.scrollTop}`);
}
// ── 用例 2：在底部 → 跟随钉底 ────────────────────────────────
console.log('用例 2：底部跟随');
{
  const area = makeArea();
  area.scrollHeight = 1000; area.clientHeight = 400; area.scrollTop = 600; // 恰在底部
  const raf = makeRafStub();
  const fn = new Function('document', 'requestAnimationFrame', 'cancelAnimationFrame', fullCode + `
    return {_isNearBottom, stickBottom, scrollChatToBottom, getFollow: () => _stickFollow};`);
  const api = fn(makeDoc(area), raf.fn, raf.cancel);
  check('B1 在底部时 _isNearBottom=true', api._isNearBottom(area) === true);
  area.scrollHeight = 1200; // 内容长高 200（模拟新 token）
  api.stickBottom();
  check('B2 跟读态下 stickBottom 钉到新底部', area.scrollTop === 800, `scrollTop=${area.scrollTop}`);
}
// ── 用例 3：强制滚动 ─────────────────────────────────────────
console.log('用例 3：强制滚动（用户显式动作）');
{
  const area = makeArea();
  const raf = makeRafStub();
  const fn = new Function('document', 'requestAnimationFrame', 'cancelAnimationFrame', fullCode + `
    return {scrollChatToBottom, getFollow: () => _stickFollow};`);
  const api = fn(makeDoc(area), raf.fn, raf.cancel);
  area.scrollTop = 50;
  api.scrollChatToBottom();
  check('C1 scrollChatToBottom 强制钉底（钳制到 scrollHeight-clientHeight）',
    area.scrollTop === 600, `scrollTop=${area.scrollTop}`);
  check('C2 强制滚动复位跟读态', api.getFollow() === true);
}
// ── 用例 4：_chatInputAutoGrow 自适应高度 ─────────────────────
console.log('用例 4：输入框自适应高度');
{
  const src = fs.readFileSync(path.join(ROOT, 'static/js/mods/12-chatsend.js'), 'utf8');
  const code = extract(src, 'function _chatInputAutoGrow()', 'async function handleChatInput');
  const ta = { scrollHeight: 350, style: {} };
  const doc = { getElementById: (id) => (id === 'chat-input' ? ta : null) };
  // 声明后**立即调用**（new Function 只定义不执行）
  new Function('document', code + '\n_chatInputAutoGrow();')(doc);
  check('D1 内容 350px 时高度钳到上限 200', ta.style.height === '200px', `height=${ta.style.height}`);
  check('D2 超上限时开启内部滚动', ta.style.overflowY === 'auto');
  ta.scrollHeight = 120;
  new Function('document', code + '\n_chatInputAutoGrow();')(doc);
  check('D3 内容 120px 时高度跟随内容', ta.style.height === '120px', `height=${ta.style.height}`);
  check('D4 未超上限时隐藏内部滚动', ta.style.overflowY === 'hidden');
}
// ── 用例 5：静态断言 —— 流式路径全部接入 stickBottom ───────────
console.log('用例 5：流式路径接入检查');
{
  const pipe = fs.readFileSync(path.join(ROOT, 'static/js/mods/11-pipeline.js'), 'utf8');
  const send = fs.readFileSync(path.join(ROOT, 'static/js/mods/12-chatsend.js'), 'utf8');
  const rep = fs.readFileSync(path.join(ROOT, 'static/js/mods/13-reports.js'), 'utf8');
  // 流式渲染路径（每帧/每 token）必须走 stickBottom
  check('E1 _flushTokens 接入 stickBottom', /_flushTokens\(\)\{[\s\S]*?stickBottom\(\)/.test(pipe));
  check('E2 procRender 接入 stickBottom', /function procRender\(\)\{[\s\S]*?stickBottom\(\)/.test(pipe));
  check('E3 12-chatsend 流结束接入 stickBottom', /_forceFlushTokens\(\);[\s\S]{0,120}?stickBottom\(\)/.test(send));
  check('E4 finishStream 接入 stickBottom', /loadArtifacts\(currentConvId\);[\s\S]{0,80}?stickBottom\(\)/.test(rep));
  // 发送/挂回等用户显式动作保留强制滚动
  check('E5 sendChat 保留 scrollChatToBottom（强制）', /area\.appendChild\(aiBox\);\s*\/\/ 2026-09-29[\s\S]*?scrollChatToBottom\(\)/.test(send));
  check('E6 澄清卡插入保留 scrollChatToBottom（强制）', /clarifyGo\(card, 0\);[\s\S]{0,200}?scrollChatToBottom\(\)/.test(pipe));
  // 新增交互元素的操作入口
  check('E7 子任务内部工具有「展开全文」', /_subChildrenHtml\(key\)\{[\s\S]*?toolExpand\(this\)/.test(pipe));
  check('E8 子任务内部工具有「复制」', /_subChildrenHtml\(key\)\{[\s\S]*?copyToolResult\(this\)/.test(pipe));
  check('E9 思考过程完成时带轮数总结', /已深度思考/.test(pipe));
  check('E10 proc-head 带 hover 展开提示', /title="点击\$\{s\.collapsed\?/.test(pipe));
}
// ── 用例 6：变异测试 ─────────────────────────────────────────
console.log('变异测试：摘除 rAF 回调内的跟读守卫（ stickBottom 变"无条件钉底"）');
{
  // stickBottom 是双道防线：外层 if(!_stickFollow) return + rAF 回调内 if(!a || !_stickFollow) return。
  // 变异目标：两道**一起摘除**（只摘一道会被另一道兜住，变异无效）——
  //   命中即证明 A4 断言能抓住"用户上滑仍被拽底"的坏行为。
  const anchor1 = 'if(!_stickFollow) return;';
  const anchor2 = 'if(!a || !_stickFollow) return;';
  const mutated = fullCode.replace(anchor1, '').replace(anchor2, 'if(!a) return;');
  if (mutated === fullCode || !mutated.includes('if(!a) return;')) {
    check('[变异] 守卫语句存在（锚点命中）', false, '锚点未命中，变异无效——需同步锚点');
  } else {
    const area = makeArea();
    area.scrollTop = 100; // 不在底部、未跟读
    const raf = makeRafStub();
    const fn = new Function('document', 'requestAnimationFrame', 'cancelAnimationFrame', mutated + `
      return {stickBottom, setFollow: (v) => { _stickFollow = v; }};`);
    const api = fn(makeDoc(area), raf.fn, raf.cancel);
    api.setFollow(false);
    api.stickBottom();
    check('[变异] A4 应 FAIL —— 失去守卫后视口被强拽到底', area.scrollTop === 600,
      `scrollTop=${area.scrollTop}（期待被拽到 600；若仍是 100 说明原断言空转）`);
  }
}

console.log(`\nPASS ${pass} / ${pass + fail}`);
process.exit(fail ? 1 : 0);
