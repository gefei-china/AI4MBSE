// CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：真实浏览器门禁——需本机 Chromium（agent-browser）+ 8000 真实服务/真库，CI runner 无该组合 ⇒ 本机跑；改法须同步改清单 tier 并接线，不允许只删这行
/* 回归判据 · 会话输入框 Ctrl+V 贴图/文件、拖放文件（2026-11-06）
 *
 * 背景：输入框原先只有 <input type=file> 的按钮上传，没有 paste/drop 监听。
 * 本脚本从真实源文件抽取新增代码块，在 vm 沙箱里模拟剪贴板与拖放事件，
 * 断言：有文件才拦截、纯文本不拦截、粘贴图片会被规范化成带扩展名的文件名、
 * 拖拽走的仍是同一条 uploadFileObjects 链路。
 *
 * 用法：node tools/verify/verify_chat_paste_drop.js
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const FILE = path.resolve(__dirname, '../../static/js/mods/10-chatinput.js');
const SRC = fs.readFileSync(FILE, 'utf8');
const MARK = '// ── 2026-11-06 富输入补齐';
const start = SRC.indexOf(MARK);
if (start < 0) throw new Error('未找到富输入代码块');
const CODE = SRC.slice(start);

class FakeFile {
  constructor(parts, name, opts) { this.parts = parts; this.name = name; this.type = (opts && opts.type) || ''; }
}

function makeCtx() {
  const handlers = { ta: {}, dock: {} };
  const el = (bag) => ({
    dataset: {}, style: {},
    addEventListener: (t, fn) => { bag[t] = fn; },
    contains: () => false,
  });
  const ta = el(handlers.ta), dock = el(handlers.dock);
  const uploads = [], toasts = [];
  const ctx = {
    document: {
      readyState: 'complete',
      getElementById: (id) => id === 'chat-input' ? ta : (id === 'chat-input-dock' ? dock : null),
      addEventListener: () => {},
    },
    File: FakeFile,
    uploadFileObjects: (files) => { uploads.push(files); },
    toast: (m) => toasts.push(String(m)),
    Date, console,
  };
  vm.createContext(ctx);
  vm.runInContext(CODE, ctx);
  return { ctx, handlers, uploads, toasts };
}

const results = [];
function check(name, cond, extra) { results.push({ name, ok: !!cond, extra: extra || '' }); }

// ① 粘贴图片：必须拦截默认粘贴，并规范化成 .png 文件名
{
  const { handlers, uploads } = makeCtx();
  let prevented = false;
  handlers.ta.paste({
    clipboardData: {
      items: [{ kind: 'file', getAsFile: () => ({ name: 'image.png', type: 'image/png' }) }],
      files: [],
    },
    preventDefault: () => { prevented = true; },
  });
  check('粘贴图片→拦截默认粘贴', prevented);
  check('粘贴图片→进入上传链路', uploads.length === 1 && uploads[0].length === 1);
  check('粘贴图片→文件名规范化为 pasted-*.png',
    uploads.length && /^pasted-\d+-\d+\.png$/.test(uploads[0][0].name),
    uploads.length ? uploads[0][0].name : 'no upload');
}
// ② 纯文本粘贴：不得拦截、不得上传
{
  const { handlers, uploads } = makeCtx();
  let prevented = false;
  handlers.ta.paste({
    clipboardData: { items: [{ kind: 'string' }], files: [] },
    preventDefault: () => { prevented = true; },
  });
  check('纯文本粘贴→不拦截', !prevented);
  check('纯文本粘贴→不上传', uploads.length === 0);
}
// ③ 粘贴多个文件（含带正常名字的文档）
{
  const { handlers, uploads } = makeCtx();
  handlers.ta.paste({
    clipboardData: {
      items: [
        { kind: 'file', getAsFile: () => ({ name: 'spec.docx', type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }) },
        { kind: 'file', getAsFile: () => ({ name: '', type: 'image/jpeg' }) },
      ],
      files: [],
    },
    preventDefault: () => {},
  });
  check('多文件粘贴→一次进链路', uploads.length === 1 && uploads[0].length === 2);
  check('文档保留原名', uploads.length && uploads[0][0].name === 'spec.docx');
  check('空名图片→生成 .jpg', uploads.length && /\.jpg$/.test(uploads[0][1].name),
    uploads.length ? uploads[0][1].name : 'no upload');
}
// ④ 拖放：拖文件接管、拖纯文本不接管
{
  const { handlers, uploads } = makeCtx();
  let overText = false, overFile = false;
  handlers.dock.dragover({ dataTransfer: { types: ['text/plain'] }, preventDefault: () => { overText = true; } });
  handlers.dock.dragover({ dataTransfer: { types: ['Files'] }, preventDefault: () => { overFile = true; } });
  check('拖纯文本→不接管', !overText);
  check('拖文件→接管 dragover', overFile);
  let dropped = false;
  handlers.dock.drop({
    dataTransfer: { files: [{ name: 'model.sysml', type: 'text/plain' }] },
    preventDefault: () => { dropped = true; },
  });
  check('drop→拦截默认行为', dropped);
  check('drop→进入同一上传链路', uploads.length === 1 && uploads[0][0].name === 'model.sysml');
}

let allOk = true;
for (const r of results) {
  console.log((r.ok ? 'PASS  ' : 'FAIL  ') + r.name + (r.extra ? ' ⇒ ' + r.extra : ''));
  if (!r.ok) allOk = false;
}
console.log('ALL_OK=' + allOk);
process.exit(allOk ? 0 : 1);
