// 变异注入/还原：stream.py 入口 user 落库开关（verify_stream_user_persist 的变异自证用）
// 用法: node mut_stream_entry.js mutate|restore|check   （需先 cp stream.py <repo>/tmp/stream.py.keep）
const fs = require('fs');
const p = 'agent/pipeline_parts/stream.py';
const BAK = 'tmp/stream.py.keep';   // ⚠️ 备份必须放 repo/tmp 下 —— node 把 '/tmp' 解析成 C:\tmp，与 Git Bash 的 /tmp 不是同一处（2026-09-29 实测踩坑）
const mode = process.argv[2] || 'check';
let s = fs.readFileSync(p, 'utf8');
const GOOD = 'if not dry_run:';                       // 修复态
const BAD  = 'if False and not dry_run:';             // 变异态（入口落库被禁用）
const anchorRe = /if (?:False and )?not dry_run:(\r?\n\s+try:\r?\n\s+with db_conn\(\) as _conn0:)/;
if (mode === 'mutate') {
  const hits = (s.match(new RegExp(anchorRe.source, 'g')) || []).length;
  if (hits !== 1) { console.error('ANCHOR!=' + hits); process.exit(1); }
  s = s.replace(GOOD + '\r\n            try:', BAD + '\r\n            try:');
  if (s === fs.readFileSync(p, 'utf8')) {           // CRLF 替换失败再试 LF
    s = fs.readFileSync(p, 'utf8').replace(GOOD + '\n            try:', BAD + '\n            try:');
  }
  fs.writeFileSync(p, s);
  console.log('MUTATED');
} else if (mode === 'restore') {
  const b = fs.readFileSync(BAK, 'utf8');
  fs.writeFileSync(p, b);
  console.log('RESTORED from backup');
} else {
  const ok = s.includes(BAD) ? 'MUTATED' : (s.includes(GOOD) ? 'GOOD' : 'UNKNOWN');
  console.log(ok);
}
// 自证：改完必须验内容真的变了
if (mode === 'mutate') {
  const now = fs.readFileSync(p, 'utf8');
  if (!now.includes(BAD)) { console.error('MUTATE_FAILED: pattern not present after write'); process.exit(1); }
}
