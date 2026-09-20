/* 覆盖性分析页真机验证：五视图逐一截图（短 eval + node 侧等待） */
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir();
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `abp_${++seq}.out`), e = path.join(TMP, `abp_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 60000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const out = (fs.readFileSync(o, "utf8") + fs.readFileSync(e, "utf8")).trim();
  fs.unlinkSync(o); fs.unlinkSync(e);
  return out;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const JS = (code) => {
  let v = ab("eval", code);
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  return v;
};

(async () => {
  const log = { steps: [] };
  ab("open", "http://127.0.0.1:8000");
  ab("set", "viewport", "1600", "900");
  ab("wait", "--load", "networkidle");
  ab("wait", "2500");

  // 进入覆盖性分析页
  const enter = JS(`(async()=>{ const s=ms=>new Promise(r=>setTimeout(r,ms));
    go('coverage'); await s(3000);
    return JSON.stringify({
      pageOn: Array.from(document.querySelectorAll('.page.on')).map(d=>d.id),
      tabs: Array.from(document.querySelectorAll('#cov-tabs button')).map(b=>b.textContent.trim()),
      hasMatrix: !!document.querySelector('#cov-body table'),
      bodyHead: (document.getElementById('cov-body')||{innerText:''}).innerText.slice(0,150)
    }); })()`);
  log.steps.push({ step: "enter", enter });

  // 逐工具截图
  for (const t of ["coverage_matrix", "trace_chain_check", "scene_coverage", "gap_summary", "modeling_coverage"]) {
    JS(`covSwitchTool('${t}'); "ok"`);
    await sleep(2200);
    const shot = OUT + `cov_${t}.png`;
    ab("screenshot", shot);
    const stat = JS(`(function(){
      const b = document.getElementById('cov-body');
      return JSON.stringify({ textLen: (b.innerText||'').length, head: (b.innerText||'').slice(0,120) });
    })()`);
    log.steps.push({ tool: t, shot, stat });
  }
  fs.writeFileSync(OUT + "_verify_coverage_page.json", JSON.stringify(log, null, 1), "utf8");
  ab("close");
  console.log("DONE");
})();
