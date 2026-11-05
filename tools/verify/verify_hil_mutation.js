/* HIL 确认卡 —— **变异自证**（证明断言有鉴别力，不是空转）
 *
 * 技能纪律：「前端断言也要做变异自证（把 bug 还原 → 断言必须 FAIL，否则是空转断言）」
 *
 * 两个变异，每个都必须让**特定断言**翻红，且**不能全红**（全红说明夹具/环境坏了）：
 *   变异 A：detail 去掉「影响面」+「性质」两行（模拟旧版只给参数 JSON）
 *           ⇒ detailHasPreviewKw / detailHasKindLine 必须 false
 *           ⇒ hasDetailsCollapse 必须**仍 true**（只有目标项翻，不是全崩）
 *   变异 B：`_hilCardEl` 改回 `getElementById('hil_' + cid)`（还原真实缺陷）
 *           ⇒ D.helper_finds_A / helper_finds_B 必须 false
 *           ⇒ D.legacyGetElementByIdReturnsNull 必须**仍 true**
 */
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path"), crypto = require("crypto");

const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const JS = ROOT + "/static/js/mods/11-pipeline.js";
const OUT = ROOT + "/outputs/";
const PAY = ROOT + "/tools/verify/ui_payloads/";   // 2026-10-28 沉淀：从 tmp/ 移入 tools/verify（可复跑）
const TMP = os.tmpdir();
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `ab_mut_${++seq}.out`), e = path.join(TMP, `ab_mut_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status };
}
const show = (r) => (r.stdout + r.stderr).trim();
function run(name) {
  let v = show(ab("eval", fs.readFileSync(PAY + name, "utf8")));
  for (let i = 0; i < 4 && typeof v === "string"; i++) {
    try { v = JSON.parse(v); } catch (_) { break; }
  }
  return v;
}
function sha(p) { return crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex"); }

const orig = fs.readFileSync(JS, "utf8");
const h0 = sha(JS);
const log = [];
const T = (k, v) => log.push({ [k]: v });

function bootAndRun() {
  ab("open", "http://127.0.0.1:8000");
  ab("set", "viewport", "1600", "900");
  ab("set", "media", "light");
  ab("wait", "--load", "networkidle");
  ab("wait", "2500");
  run("hil_t0_fixture.js");
  const r = run("hil_t1_render.js");
  ab("close");
  return r;
}

// ── 基线 ──
T("baseline", bootAndRun());

// ── 变异 A：去掉「影响面」+「性质」两行 ──
let mutA_ok = false;
try {
  let s = orig;
  const before = s.length;
  // 把这两行从模板里删掉（精确匹配我写的那两行）
  const linePrev = "    + (prev ? `<div><b>\u5f71\u54cd\u9762\uff1a</b>${esc(prev)}</div>` : '')\n";
  const lineKind = "    + `<div><b>\u6027\u8d28\uff1a</b>${_kindTxt}</div>`\n";
  if (s.indexOf(linePrev) < 0 || s.indexOf(lineKind) < 0) {
    throw new Error("锚点未命中，无法变异（linePrev=" + (s.indexOf(linePrev) >= 0)
      + " lineKind=" + (s.indexOf(lineKind) >= 0) + "）");
  }
  s = s.replace(linePrev, "").replace(lineKind, "");
  fs.writeFileSync(JS, s, "utf8");
  const r = run && bootAndRun();
  mutA_ok = (r && r.A && r.A.detailHasPreviewKw === false
             && r.A.detailHasKindLine === false
             && r.A.hasDetailsCollapse === true);
  T("mutA", { applied: true, lenDelta: s.length - before,
    detailHasPreviewKw: r && r.A && r.A.detailHasPreviewKw,
    detailHasKindLine: r && r.A && r.A.detailHasKindLine,
    hasDetailsCollapse: r && r.A && r.A.hasDetailsCollapse,
    // 关键：只有目标项翻红，其余仍绿
    otherStillGreen: !!(r && r.A && r.A.iconIsBlock === true && r.A.argsHiddenByDefault === true),
    caught: mutA_ok });
} catch (e) {
  T("mutA", { applied: false, error: String(e.message || e).slice(0, 200), caught: false });
} finally {
  fs.writeFileSync(JS, orig, "utf8");
}

// ── 变异 B：_hilCardEl 改回 getElementById ──
let mutB_ok = false;
try {
  // 让 helper 内部退回旧写法（保留函数名，只改实现）
  const anchor = "  return document.querySelector('[data-pid=\"hil_' + n + '\"]');";
  if (orig.indexOf(anchor) < 0) throw new Error("_hilCardEl 实现锚点未命中");
  const s = orig.replace(anchor, "  return document.getElementById('hil_' + n);");
  fs.writeFileSync(JS, s, "utf8");
  const r = bootAndRun();
  mutB_ok = !!(r && r.D && r.D.helper_finds_A === false && r.D.helper_finds_B === false
               && r.D.legacyGetElementByIdReturnsNull === true);
  T("mutB", { applied: true,
    helper_finds_A: r && r.D && r.D.helper_finds_A,
    helper_finds_B: r && r.D && r.D.helper_finds_B,
    legacyGetElementByIdReturnsNull: r && r.D && r.D.legacyGetElementByIdReturnsNull,
    // 渲染仍应正常（缺陷只影响"取元素"，不影响卡片生成）
    cardStillRendered: !!(r && r.A && r.A.cardExists === true),
    caught: mutB_ok });
} catch (e) {
  T("mutB", { applied: false, error: String(e.message || e).slice(0, 200), caught: false });
} finally {
  fs.writeFileSync(JS, orig, "utf8");
}

// ── 还原校验（逐字节）──
const h1 = sha(JS);
T("restore", { sha_before: h0.slice(0, 16), sha_after: h1.slice(0, 16),
               identical: h0 === h1, bytes: fs.statSync(JS).size });

// ── 还原后再跑一次，确认回到全绿 ──
T("afterRestore", (() => { const r = bootAndRun();
  return { detailHasPreviewKw: r && r.A && r.A.detailHasPreviewKw,
           detailHasKindLine: r && r.A && r.A.detailHasKindLine,
           helper_finds_A: r && r.D && r.D.helper_finds_A,
           allBackToGreen: !!(r && r.A && r.A.detailHasPreviewKw && r.A.detailHasKindLine
                              && r.D && r.D.helper_finds_A) }; })());

T("verdict", { mutA_caught: mutA_ok, mutB_caught: mutB_ok,
               bothCaught: mutA_ok && mutB_ok });

fs.writeFileSync(OUT + "_verify_hil_mutation.json", JSON.stringify(log, null, 1), "utf8");
process.stdout.write("DONE\n");
