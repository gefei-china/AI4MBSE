// 2026-09-28 (B-1/B-2) hover 卡浏览器验证运行器
// 用法：node tools/verify/verify_hover_card.js
// 纪律（本仓踩坑）：① spawnSync + 文件描述符 stdio（避免 daemon 管道卡死）
//                  ② payload 从文件读入（多行 argv 只有第一段生效）
//                  ③ 对照必须"同一会话"——两条不同会话比字段必然全 FAIL（夹具缺陷）
const { spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const crypto = require("crypto");

const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const OUT = ROOT + "/outputs/", TMP = os.tmpdir(), PAY = ROOT + "/tmp/payloads/";
let seq = 0;

function ab(...args) {
  const o = path.join(TMP, `hb_${++seq}.out`), e = path.join(TMP, `hb_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = r => (r.stdout + r.stderr).trim();
const md5 = p => fs.existsSync(p) ? crypto.createHash("md5").update(fs.readFileSync(p)).digest("hex") : null;

let ok = 0, fail = 0; const fails = [];
function chk(tag, cond, detail = "") {
  if (cond) { ok++; console.log("  PASS  " + tag); }
  else { fail++; fails.push(tag); console.log("  FAIL  " + tag + (detail ? "   " + detail : "")); }
}

console.log("=".repeat(78));
console.log("B-1/B-2 hover 信息卡：两入口一致性 + 真摘要");
console.log("=".repeat(78));

ab("open", "http://127.0.0.1:8000");
ab("set", "viewport", "1600", "900");
ab("set", "media", "light");
ab("wait", "--load", "networkidle");
ab("wait", "2500");

let data = null;
{
  const src = fs.readFileSync(PAY + "hb1_hover_card.js", "utf8");
  const r = ab("eval", src);
  let v = show(r);
  for (let i = 0; i < 3 && typeof v === "string"; i++) { try { v = JSON.parse(v); } catch (_) { break; } }
  data = v;
}

if (!data || typeof data !== "object" || !data.step) {
  console.log("!! eval 未返回可用 JSON：", JSON.stringify(data).slice(0, 500));
  process.exit(1);
}

const get = k => { const hit = data.step.find(s => s[0] === k); return hit ? hit[1] : null; };
console.log("entryCounts:", JSON.stringify(get("entryCounts")));
console.log("ds_ungrouped:", JSON.stringify(get("ds_ungrouped")));
console.log("ds_inProject:", JSON.stringify(get("ds_inProject")));

const A = get("card_A"), B = get("card_B"), C = get("card_C_noDataset");
const api = get("api");
console.log("\n[card_A 未分组]", JSON.stringify(A, null, 1));
console.log("[card_B 项目分组(同会话)]", JSON.stringify(B, null, 1));
console.log("[card_C 无 data-* 对照]", JSON.stringify(C, null, 1));
if (data.err && data.err.length) console.log("err:", data.err);

console.log("\n── A 卡片可用性 ──");
chk("A1 卡片可渲染且有尺寸", A && !A.missing && A.display === "block" && A.w > 100 && A.h > 40,
  A ? `display=${A.display} ${A.w}x${A.h}` : "无卡");
chk("A2 卡片在视口内（定位正常）", !!(A && A.inViewport), A ? `left=${A.left} top=${A.top}` : "");
chk("A3 卡片含标题", !!(A && A.title), A ? A.title : "");

console.log("\n── B 摘要为服务端真概括（非前端截断）──");
console.log("api: id=%s source=%s len=%s", api && api.id, api && api.source, api && api.len);
chk("B1 接口 source 合法", !!(api && ["llm", "structured"].includes(api.source)), api ? api.source : "无");
chk("B2 摘要长度合理(>=30 字)", !!(api && api.len >= 30), api ? String(api.len) : "");
chk("B3 卡片摘要非空且非占位", !!(A && A.sum && A.sum !== "正在生成摘要…" && A.sum !== "（暂无消息内容）"),
  A ? String(A.sum).slice(0, 40) : "");
chk("B4 卡片摘要含来源徽标", !!(A && A.hasSrcBadge), A ? String(A.src) : "");
// 卡上文本 = 接口摘要 + 徽标文字；去掉徽标后应完全等于接口摘要
const cardSum = A && A.sum ? A.sum.replace(/模型概括|概述（未接模型）$/, "").trim() : "";
chk("B5 卡片摘要 == 接口摘要（确证两者同源、非同前端截断）",
  !!(api && api.full && cardSum === api.full.trim()),
  `card=${JSON.stringify(cardSum.slice(0, 40))} api=${JSON.stringify(String(api && api.full).slice(0, 40))}`);

console.log("\n── C 两入口一致性（同一会话 id=%s）──", A && A.id);
const same = (k, label) => chk("C " + label, A && B && A[k] === B[k],
  A && B ? `A=${JSON.stringify(A[k])} B=${JSON.stringify(B[k])}` : "");
chk("C0 两卡来自同一会话", !!(A && B && A.id === B.id), A && B ? `${A.id} vs ${B.id}` : "");
same("title", "标题一致");
same("kind", "类型标签一致");
same("tag", "intent 标签一致");
same("cnt", "消息数一致");
same("time", "更新时间一致");
same("timeVisible", "时间行可见性一致");
same("sum", "摘要一致");
same("src", "来源徽标一致");

console.log("\n── D 反向对照：清空 data-* 后接口回填仍生效（机制自证）──");
if (C && A) {
  chk("D1 标题仍能取到", !!C.title, C.title);
  chk("D2 消息数由接口回填（非 0 条）", !!C.cnt && C.cnt !== "💬 0 条", String(C.cnt));
  chk("D3 时间行由接口回填并可见", C.timeVisible === true && !!C.time, `${C.timeVisible} / ${C.time}`);
  chk("D4 摘要仍与接口一致", (C.sum || "").replace(/模型概括|概述（未接模型）$/, "").trim() === String(api && api.full).trim(),
    String(C.sum).slice(0, 40));
} else {
  chk("D 反向对照可用", false, "card_C 缺失");
}

// 截图
const s1 = OUT + "verify_hover_card.png";
ab("screenshot", s1);
chk("E1 截图已生成", fs.existsSync(s1), s1);

console.log("\n== 汇总: %d PASS / %d FAIL ==", ok, fail);
if (fails.length) console.log("失败项:", fails);
process.exit(fail ? 1 : 0);
