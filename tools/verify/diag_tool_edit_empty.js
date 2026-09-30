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

const P = `(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const L=[];
  document.querySelector('a[data-page="studio"]').click(); await sleep(1200);
  document.querySelector('[data-pane="st-mcp"]').click(); await sleep(3500);
  // A) _toolsCache 是否加载（顶层 let/const 不挂 window，必须用 eval 直接访问词法作用域）
  let tc=null, tcType='unreachable';
  try{ tc = _toolsCache; tcType = Array.isArray(tc)?('array n='+tc.length):('type='+typeof tc); }catch(e){ tcType='THROW: '+e.message; }
  L.push('A._toolsCache = '+tcType);
  if(Array.isArray(tc)&&tc.length){ L.push('   ids='+JSON.stringify(tc.map(x=>x.id).slice(0,40))); }
  // B) showToolForm(4047) 能否命中
  let hit=null;
  try{ hit = (Array.isArray(tc)?tc.find(x=>x.id===4047):null); }catch(e){}
  L.push('B.find(id=4047) = '+(hit?('OK name='+hit.name+' desc='+hit.description):'NOT-FOUND'));
  // C) 手动调 showToolForm(4047) 看结果
  try{ showToolForm(4047); }catch(e){ L.push('C.showToolForm threw: '+e.message); }
  await sleep(900);
  const g=id=>{const el=document.getElementById(id);return el?((el.value===undefined?'':el.value)):'NULL';};
  L.push('C.after title='+JSON.stringify((document.getElementById('tool-modal-title')||{}).textContent));
  L.push('C.f-tool-name='+JSON.stringify(g('f-tool-name'))+' desc='+JSON.stringify(g('f-tool-desc')));
  L.push('C.f-tool-schema.len='+(g('f-tool-schema')+'').length);
  L.push('C.channel='+JSON.stringify(g('f-tool-channel')));
  L.push('C.params-body.len='+(document.getElementById('tool-params-body')||{}).innerHTML?.length);
  // D) 运行时探测：loadTools 是否被调用过
  L.push('D.loadTools='+typeof loadTools);
  return L.join('\\n');
})()`;
step("probe-toolsCache", ab("eval", P));
step("shot", ab("screenshot", OUT + "/diag_toolform_4047.png"));
ab("close");
