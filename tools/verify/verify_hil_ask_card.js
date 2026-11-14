// CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：真实浏览器门禁——需本机 Chromium（agent-browser）+ 8000 真实服务/真库，CI runner 无该组合 ⇒ 本机跑；改法须同步改清单 tier 并接线，不允许只删这行
/* HIL 确认卡 · **三载体**门禁（通用 Ask 层，2026-11-13）
 *
 * ## 这张门禁锁死的是什么
 * 真机 conv 990727 的缺陷是「用户**根本没法确认**」：后端把 `hil_confirm` 事件发出去了、
 * 消息也落库了（`card_data.confirm` 数据完整），但前端只有「执行过程时间线」里那张卡，
 * 而它**默认折叠在"详情"后面** ⇒ 用户只看到 done 帧那句**不可点击**的文本
 * 「写操作已进入人工确认队列」⇒ 确认单一直挂 pending（当时全库 19 条）。
 *
 * 修法是把 confirm 接进**与澄清卡同一套**消费流程（`handleAskEvent` 统一入口），
 * 于是同一张确认单会有**三处载体**，本门禁逐处断言：
 *   ① 消息流卡   id 前缀 `hilmsg-`  （`hilAppendMsgCard`，刷新后由 `renderRichCard` 还原）
 *   ② dock 卡    id 前缀 `hildock-` （输入框上方，`updateConfirmHint`，刷新后 `loadPendingConfirms`）
 *   ③ 时间线卡   id 前缀 `hil-`     （`procAddHilConfirm`，老实现）
 *
 * ## 为什么断言必须按 **id 前缀** 判别（这条踩过，别改回去）
 * 三处载体共用 `class="hil-card"` 与 `data-cid` ⇒ 用 `.hil-card[data-cid]` 判断
 * "消息流卡存在"会**恒真**（dock 卡也命中）。第一版就是这么写的，
 * 变异掉渲染分支后断言仍然全绿 —— 典型的**空转断言**。
 *
 * ## 变异自证（本脚本必跑，不是可选）
 * 把 `06-cards.js` 里 `hil_confirm` 渲染分支注释掉 ⇒ `msgCardExists` 必须翻红，
 * 而 `dockCardExists` 必须**仍绿**（证明不是环境全崩）。随后逐字节恢复并校验 sha256。
 * 若变异阶段不翻红 ⇒ 本门禁的断言无鉴别力，判 FAIL。
 *
 * 用法：node tools/verify/verify_hil_ask_card.js
 * 退出码：0 = 全绿 / 1 = 有断言失败 / 2 = 环境不可用（服务或浏览器）
 */
const { spawnSync, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/";
const PAY = ROOT + "/tools/verify/ui_payloads/";
const CARD_SRC = ROOT + "/static/js/mods/06-cards.js";
const PROBE = "MUTATION_PROBE";
const TMP = os.tmpdir();
const CONV = 990727;          // 有 2 张 pending 确认单（#100/#101）的真机会话
let seq = 0;

const rows = [];
function rec(label, ok, detail) {
  rows.push({ label, ok: !!ok, detail: detail === undefined ? "" : String(detail).slice(0, 300) });
  return !!ok;
}

function sha(p) {
  return crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex");
}

/* ⚠️ 必须用文件描述符接管 stdio：agent-browser 守护进程会继承管道并一直持有，
   用 {encoding:"utf8"} 的管道会让 spawnSync 永远等不到 EOF（每次空等到 timeout）。 */
function ab(...args) {
  const o = path.join(TMP, `ab_ach_${++seq}.out`), e = path.join(TMP, `ab_ach_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = (r) => (r.stdout + r.stderr).trim();

function runPayload(name) {
  const r = ab("eval", fs.readFileSync(PAY + name, "utf8"));
  let v = show(r);
  for (let i = 0; i < 4 && typeof v === "string"; i++) {
    try { v = JSON.parse(v); } catch (_) { break; }
  }
  return v;
}

function openPage(tag) {
  // ⚠️ 不要带 `?gate=xxx`：服务对 static 已是 no-cache（无需 cache-bust），
  //    而查询串会干扰前端初始化 —— 实测带 query 时历史消息的 rich card 不渲染
  //    （H1 恒红，但实时路径与 dock 全绿），改回裸地址即恢复。
  ab("open", "http://127.0.0.1:8000");
  ab("set", "viewport", "1600", "900");
  ab("set", "media", "light");
  ab("wait", "--load", "networkidle");
  ab("wait", "3000");
}

/* ── 残留自愈：上一轮若被 SIGTERM 打断，源码会停在变异态 ──
   判据是「主文件含探针标记 且 备份存在」，两个同时成立才恢复（避免误伤真改动）。 */
const BAK = ROOT + "/tmp/06-cards.js.gatebak";
function healLeftoverMutation() {
  const src = fs.readFileSync(CARD_SRC, "utf8");
  if (src.indexOf(PROBE) < 0) return null;
  if (!fs.existsSync(BAK)) return "检测到变异残留但备份缺失，无法自愈（需人工 git checkout）";
  fs.copyFileSync(BAK, CARD_SRC);
  return "已从备份自愈上一轮残留变异";
}

/* ── 变异：注释掉 hil_confirm 渲染分支；返回 {origSha, mutSha} ── */
function applyMutation() {
  const orig = fs.readFileSync(CARD_SRC, "utf8");
  fs.writeFileSync(BAK, orig);
  const needle = "  if(type==='hil_confirm' && cd.confirm) return hilConfirmCardHtml(Object.assign({}, cd.confirm, {idPrefix:'hilmsg'}));";
  if (orig.indexOf(needle) < 0) throw new Error("变异锚点未命中（06-cards.js 的 hil_confirm 分支被别人改过）");
  const mutated = orig.replace(needle,
    "  /* " + PROBE + "：临时移除 hil_confirm 渲染分支用于自证，随后逐字节恢复 */");
  fs.writeFileSync(CARD_SRC, mutated, "utf8");
  return { origSha: sha(CARD_SRC), savedOrig: crypto.createHash("sha256").update(orig).digest("hex") };
}

function restoreMutation(savedOrigSha) {
  fs.copyFileSync(BAK, CARD_SRC);
  const now = sha(CARD_SRC);
  try { fs.unlinkSync(BAK); } catch (_) {}
  return { shaNow: now, identical: now === savedOrigSha,
           probeLeft: fs.readFileSync(CARD_SRC, "utf8").split(PROBE).length - 1 };
}

/* ══════════════ 主流程 ══════════════ */
const t0 = Date.now();

// 0) 环境探活
//   ⚠️ 两个坑都踩过：
//     ① 不要用 agent-browser 的 open 输出判断可达性 —— 它的输出里可能出现 "000"
//        这类子串，第一版据此把**活着的服务**误判成不可达（假 SKIP）；
//     ② 不要用 `curl -o /dev/null` —— Windows 自带 curl.exe 下 /dev/null 不可用，
//        execFileSync 直接失败 ⇒ 同样假 SKIP。
//   改用一个 node 子进程发 http 请求、只回状态码，跨平台且判据单一。
function probeCode() {
  const script =
    "const http=require('http');" +
    "const r=http.get('http://127.0.0.1:8000/api/dashboard',res=>{console.log(res.statusCode);process.exit(0)});" +
    "r.on('error',()=>{console.log(0);process.exit(0)});" +
    "r.setTimeout(5000,()=>{console.log(0);process.exit(0)});";
  const r = spawnSync(process.execPath, ["-e", script], { encoding: "utf8", timeout: 12000 });
  return parseInt(String((r && r.stdout) || "0").trim(), 10) || 0;
}
const healMsg = healLeftoverMutation();
rec("E0a 无变异残留（或已自愈）", healMsg === null || healMsg.indexOf("已从备份自愈") === 0, healMsg || "干净");
const code = probeCode();
if (code !== 200) {
  fs.writeFileSync(OUT + "_verify_hil_ask_card.json",
    JSON.stringify({ verdict: "ENV_UNAVAILABLE", http: code, rows }, null, 1), "utf8");
  process.stdout.write("SKIP: 服务不可达（dashboard=" + code + "，先起 main.py）\n");
  process.exit(2);
}

// 1) 历史还原路径（刷新后仍可处置 = 修复前完全缺失的那条）
openPage("hist");
const hist = runPayload("hil_ask_hist.js");
rec("H1 消息流卡存在（hilmsg- 前缀）", hist && hist.msgCardExists === true, hist && JSON.stringify(hist).slice(0, 200));
rec("H2 消息流卡按钮可见（像素高>0）", hist && hist.msgCardVisible === true);
rec("H3 消息流卡在消息流里、不在输入框 dock 内", hist && hist.msgCardNotInDock === true);
rec("H4 消息流卡含「批准并执行」", hist && hist.msgCardTextHasApprove === true);
rec("H5 消息流卡含动作名 save_artifact", hist && hist.msgCardTextHasTool === true);
rec("H6 dock 卡存在（hildock- 前缀）", hist && hist.dockCardExists === true, hist && (hist.pendingCount + " 张待办"));
rec("H7 dock 卡按钮可见", hist && hist.dockCardVisible === true);
rec("H8 dock 卡显示影响面（执行「…」或 写入产物：…）", hist && hist.dockCardTextHasPreview === true);
rec("H9 统一入口函数就位（handleAskEvent）", hist && hist.askLayerExists === true);
rec("H10 刷新还原函数就位（loadPendingConfirms）", hist && hist.dockLoadExists === true);
ab("screenshot", OUT + "verify_hil_ask_hist.png");

// 2) 实时路径（SSE `hil_confirm` → handleAskEvent）
openPage("live");
const live = runPayload("hil_ask_live.js");
rec("L1 实时：消息流卡出现", live && live.liveMsgCard === true, live && JSON.stringify(live).slice(0, 200));
rec("L2 实时：消息流卡按钮可见", live && live.liveMsgBtnVisible === true);
rec("L3 实时：卡是独立消息块（不在流式气泡里，done 帧冲不掉）", live && live.liveMsgIsOwnMessageBlock === true);
rec("L4 实时：dock 卡出现且在 #confirm-ask 内", live && live.liveDockInsideConfirmDock === true);
rec("L5 实时：dock 复用澄清卡样式（.clarify-ask-card.dock）", live && live.dockIsClarifyStyle === true);
rec("L6 实时：批次文案「本批共 N 项」渲染", live && live.batchTextShown === true);
rec("L7 实时：影响面文案渲染", live && live.previewShown === true);
rec("L8 实时：待办登记成功", live && live.livePendingRegistered === true);
rec("L9 实时：清理后无残留（不污染用户待办）", live && live.cleanupDomLeft === 0 && live.cleanupPendingLeft === false);
ab("screenshot", OUT + "verify_hil_ask_live.png");

// 3) 变异自证（断言必须有鉴别力）
let mutInfo = null, afterInfo = null;
try {
  const m = applyMutation();
  mutInfo = m;
  openPage("mut");
  const mut = runPayload("hil_ask_hist.js");
  rec("M1 变异（去掉 hil_confirm 渲染分支）⇒ 消息流卡必须消失", mut && mut.msgCardExists === false,
      mut && ("msgCardExists=" + mut.msgCardExists));
  rec("M2 同一变异下 dock 卡**仍然存在**（证明不是环境全崩）", mut && mut.dockCardExists === true);
} catch (e) {
  rec("M0 变异可施加", false, String(e && e.message || e));
} finally {
  try {
    if (mutInfo) {
      afterInfo = restoreMutation(mutInfo.savedOrig);
      rec("M3 变异已逐字节恢复（sha256 一致）", afterInfo.identical === true, afterInfo.shaNow.slice(0, 16));
      rec("M4 恢复后无探针残留", afterInfo.probeLeft === 0, "残留 " + afterInfo.probeLeft);
    }
  } catch (e) {
    rec("M3 变异恢复成功", false, String(e && e.message || e));
  }
}

// 4) 恢复后再跑一次历史路径（证明恢复的是可用状态，不是"改坏了但没人发现"）
if (afterInfo && afterInfo.identical) {
  openPage("restored");
  const again = runPayload("hil_ask_hist.js");
  rec("R1 恢复后消息流卡重新出现（端到端回到绿态）", again && again.msgCardExists === true);
}

const pageErrors = show(ab("errors")).trim();
rec("P1 全程无 JS 运行时错误", pageErrors === "", pageErrors.slice(0, 200));
ab("close");

const nFail = rows.filter(r => !r.ok).length;
const verdict = nFail ? "FAIL" : "PASS";
fs.writeFileSync(OUT + "_verify_hil_ask_card.json", JSON.stringify({
  verdict, pass: rows.length - nFail, fail: nFail, elapsedMs: Date.now() - t0,
  conv: CONV, mutation: mutInfo ? { applied: true, restored: afterInfo && afterInfo.identical } : { applied: false },
  rows, pageErrors
}, null, 1), "utf8");

// 控制台摘要（UTF-8 由 node 直接写 stdout，shell 回传中文可能乱码 —— 以 JSON 文件为准）
process.stdout.write("\n=== verify_hil_ask_card ===\n");
for (const r of rows) {
  process.stdout.write((r.ok ? "  [PASS] " : "  [FAIL] ") + r.label + (r.ok ? "" : "  <- " + r.detail) + "\n");
}
process.stdout.write(`\n${verdict}  ${rows.length - nFail}/${rows.length}  (${Date.now() - t0}ms)\n`);
process.exit(nFail ? 1 : 0);
