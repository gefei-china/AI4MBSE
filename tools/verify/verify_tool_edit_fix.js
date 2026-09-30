// 修复验证：能力中心点「编辑」→ 应显示「编辑工具 #4047」+ 完整数据
const { spawnSync } = require("child_process");
const fs = require("fs"); const path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const OUT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system/outputs";
const TMP = __dirname; let seq = 0;
function ab(...args) {
  const o = path.join(TMP, `_ab_out_${seq}.txt`), e = path.join(TMP, `_ab_err_${seq}.txt`); seq++;
  spawnSync(process.execPath, [BIN, ...args], { stdio: ["ignore", fs.openSync(o, "w"), fs.openSync(e, "w")], timeout: 180000 });
  return ((fs.existsSync(o) ? fs.readFileSync(o, "utf8") : "") + (fs.existsSync(e) ? fs.readFileSync(e, "utf8") : "")).trim();
}
const step = (l, v) => console.log(`[${l}]\n${String(v).slice(0, 3000)}\n`);

ab("open", "http://127.0.0.1:8000");
ab("wait", "--load", "networkidle");

// 用 --no-cache 强制取新 JS
const P = `(async()=>{
  window.__errs=[]; window.addEventListener('error',e=>window.__errs.push('ERR:'+e.message));
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const L=[];
  document.querySelector('a[data-page="studio"]').click(); await sleep(1200);
  document.querySelector('[data-pane="st-mcp"]').click(); await sleep(4000);
  // 断言1：_toolsCache 已预热
  let n=-1; try{ n = Array.isArray(_toolsCache)?_toolsCache.length:-1; }catch(e){}
  L.push('[T1] _toolsCache 长度 = '+n+'  (期望 >0)');
  // 断言2：点第一个「编辑」
  const eb=[...document.querySelectorAll('button')].filter(b=>/编辑/.test(b.textContent||'')&&/capEdit/.test(b.getAttribute('onclick')||''));
  L.push('[T2] capEdit 按钮数 = '+eb.length);
  if(!eb.length){ L.push('!! 无编辑按钮'); return L.join('\\n'); }
  const oc=eb[0].getAttribute('onclick');
  L.push('[T3] 点击 '+oc);
  eb[0].click(); await sleep(1800);
  const ttl=(document.getElementById('tool-modal-title')||{}).textContent;
  const g=id=>{const el=document.getElementById(id);return el?((el.value===undefined?'':el.value)):'NULL';};
  L.push('[T4] 弹窗标题 = '+JSON.stringify(ttl)+'  (期望「编辑工具 #xxxx」)');
  L.push('[T5] f-tool-name = '+JSON.stringify(g('f-tool-name')));
  L.push('[T6] f-tool-desc = '+JSON.stringify(g('f-tool-desc')).slice(0,140));
  L.push('[T7] f-tool-channel = '+JSON.stringify(g('f-tool-channel')));
  L.push('[T8] f-tool-schema 长度 = '+(g('f-tool-schema')+'').length+'  (期望 >0)');
  const cb=(document.getElementById('tool-channel-body')||{}).textContent||'';
  L.push('[T9] 执行通道区文本 = '+JSON.stringify(cb.trim().slice(0,180)));
  const pb=document.getElementById('tool-params-body');
  const rows=pb&&pb.querySelector('tbody')?pb.querySelector('tbody').rows.length:0;
  L.push('[T10] 输入参数行数 = '+rows+'  (期望 >0)');
  L.push('[T11] 输入参数区内容 = '+JSON.stringify(((pb||{}).textContent||'').trim().slice(0,200)));
  const mb=document.getElementById('tool-meta-box');
  L.push('[T12] 只读元数据 disp = '+(mb?mb.style.display:'NULL')+'  (期望 非 none)');
  L.push('[T13] 名称是否禁用(编辑态) = '+((document.getElementById('f-tool-name')||{}).disabled));
  L.push('[errs] '+JSON.stringify(window.__errs));
  return L.join('\\n');
})()`;
step("verify", ab("eval", P));
step("shot", ab("screenshot", OUT + "/verify_tool_edit_fixed.png"));
ab("close");
