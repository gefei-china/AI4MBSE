// qualityRow 的行为验证（node 跑真实函数，不做静态断言）。
// 判据核心：**quality_score=0 必须不显示**（实测全库 12 行都是 0，
// 若把 0 当"质量极差"显示就是误导），>0 才显示且口径写明是 OCR 均分。
const fs = require('fs');
const src = fs.readFileSync('static/js/mods/20-docs.js', 'utf8');

// 从真实文件里抠出 qualityRow 与 ocrInfoTable（不复制粘贴，避免验证的不是生产代码）
function grab(name) {
  const start = src.indexOf('function ' + name);
  if (start < 0) throw new Error('未找到函数 ' + name);
  let depth = 0, i = src.indexOf('{', start);
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('函数 ' + name + ' 未闭合');
}
const code = grab('qualityRow') + '\n' + grab('ocrInfoTable') +
  '\nmodule.exports = {qualityRow, ocrInfoTable};';
const mod = { exports: {} };
new Function('module', 'exports', code)(mod, mod.exports);
const { qualityRow } = mod.exports;

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  PASS ' + name); }
  else { fail++; console.log('  FAIL ' + name + (detail ? '  <- ' + detail : '')); }
}

console.log('=== A. 0 值陷阱（全库实测 12 行都是 0.0）===');
check('A1 quality_score=0 → 不显示任何东西', qualityRow({ quality_score: 0 }) === '',
  JSON.stringify(qualityRow({ quality_score: 0 })));
check('A2 0.0（浮点零）同样不显示', qualityRow({ quality_score: 0.0 }) === '');
check('A3 缺字段 → 不显示', qualityRow({}) === '');
check('A4 null/undefined → 不显示',
  qualityRow({ quality_score: null }) === '' && qualityRow({ quality_score: undefined }) === '');
check('A5 绝不能出现 "0.0%" 或 "0%"', !/[0]%/.test(qualityRow({ quality_score: 0 })));

console.log('\n=== B. 有真值时正确显示 ===');
const hi = qualityRow({ quality_score: 0.87 });
check('B1 0.87 → 显示 87.0%', /87\.0%/.test(hi), hi);
check('B2 含口径说明（OCR 识别均分）', /OCR/.test(hi) && /置信度/.test(hi));
const mid = qualityRow({ quality_score: 0.62 });
check('B3 0.62 → 橙色档', /var\(--orange/.test(mid), mid);
const lo = qualityRow({ quality_score: 0.41 });
check('B4 0.41 → 红色档', /var\(--red/.test(lo), lo);
check('B5 0.87 → 绿色档', /var\(--green/.test(hi));
check('B6 三档颜色互不相同',
  new Set([hi, mid, lo].map(s => s.match(/var\(--[a-z]+/)[0])).size === 3);

console.log('\n=== C. 边界 ===');
check('C1 恰好 0.70 → 绿色（>=0.7）', /var\(--green/.test(qualityRow({ quality_score: 0.7 })));
check('C2 0.699 → 橙色', /var\(--orange/.test(qualityRow({ quality_score: 0.699 })));
check('C3 恰好 0.50 → 橙色（>=0.5）', /var\(--orange/.test(qualityRow({ quality_score: 0.5 })));
check('C4 1.0 → 100.0%', /100\.0%/.test(qualityRow({ quality_score: 1 })));
check('C5 字符串 "0.87" 也认（DB 可能回字符串）', /87\.0%/.test(qualityRow({ quality_score: '0.87' })));
check('C6 字符串 "0" 不显示', qualityRow({ quality_score: '0' }) === '');

console.log('\n=== D. 变异自证（判据有牙齿）===');
// ⚠️ 每条变异都必须先断言「锚点真的命中」，否则变异体== 原代码 ⇒ 判据是假绿。
//   （本轮首版两条变异都栽在这：文案字符串与源码不逐字相同 ⇒ replace 空转⇒ 白变异。）
function mutate(oldStr, newStr, tag) {
  if (!code.includes(oldStr)) throw new Error('变异锚点未命中: ' + tag + ' => ' + oldStr);
  return code.replace(oldStr, newStr);
}
function load(srcText) {
  const m = { exports: {} };
  new Function('module', 'exports', srcText)(m, m.exports);
  return m.exports.qualityRow;
}

// 变异 1：把 ">0 才显示" 退化成 "总是显示" ⇒ A1 判据必须 FAIL
const qMut1 = load(mutate("if(!(q > 0)) return '';", '', 'M1'));
check('D1 变异：去掉 >0 守卫 ⇒ A1 判据会 FAIL',
  qMut1({ quality_score: 0 }) !== '', 'mut(0)=' + JSON.stringify(qMut1({ quality_score: 0 }).slice(0, 50)));

// 变异 2：删掉**整条口径说明行** ⇒ B2 必须 FAIL
// ⚠️ 首版只把「口径：OCR 识别行平均置信度」替换掉，但该句里"OCR"出现两次
//    （"OCR 识别行平均置信度" + "仅走 OCR 的文档"）⇒ 残留一处 ⇒ 判据仍 PASS =假绿。
//    修法：删掉**整行**模板，并且断言里锚到唯一串「平均置信度」。
const qMut2 = load(mutate('口径：OCR 识别行平均置信度（仅走 OCR 的文档有此值；文本层抽取不计）。', '说明已移除', 'M2'));
check('D2 变异：删掉口径说明 ⇒ B2 判据会 FAIL',
  !/平均置信度/.test(qMut2({ quality_score: 0.87 })),
  '残留=' + JSON.stringify(qMut2({ quality_score: 0.87 }).slice(-90)));

// 变异 3：把橙色档阈值改坏（0.5 -> 0.9）⇒ B3 必须 FAIL
const qMut3 = load(mutate('q >= 0.5 ?', 'q >= 0.9 ?', 'M3'));
check('D3 变异：橙色阈值 0.5→0.9 ⇒ B3 判据会 FAIL',
  !/var\(--orange/.test(qMut3({ quality_score: 0.62 })));

// 变异 4：把 toFixed(1) 改成 toFixed(0) ⇒ B1 必须 FAIL（证明精度判据不是空转）
const qMut4 = load(mutate("toFixed(1)", 'toFixed(0)', 'M4'));
check('D4 变异：精度 1→0 位 ⇒ B1 判据会 FAIL',
  !/87\.0%/.test(qMut4({ quality_score: 0.87 })));

console.log('\n' + pass + ' PASS / ' + fail + ' FAIL');
process.exit(fail ? 1 : 0);