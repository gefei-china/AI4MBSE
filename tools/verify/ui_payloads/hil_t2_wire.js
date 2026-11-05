/* HIL 确认卡验证 —— t2：**接线验证**（走真实 handleSSE 的 SSE 解析路径）
 *
 * ## 为什么必须单独验这一层
 * t1 直接调 `procAddHilConfirm()` 只证明了"渲染函数本身对"，
 * **证明不了事件分支真的接上了** —— 如果 `handleSSE` 里漏了 `else if(evType === 'hil_confirm')`，
 * t1 依然全绿，而真机用户永远看不到卡。
 * ⇒ 必须喂一段**真实格式的 SSE 文本**（与后端 `routers/conversations.py` 的封装一致：
 *   `event: {type}\ndata: {json}\n\n`）进 `handleSSE`，断言卡片出现。
 *
 * ## 同时验证"老前端兼容"
 * 喂一个**未知事件类型**（`future_unknown_event`），断言：
 *   · 不抛异常（仓内 if/else-if 链末尾无 else 兜底）
 *   · 不新增卡片、不清空已有时间线
 */
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = {};

  const before = document.querySelectorAll('[data-pid^="hil_"]').length;

  // ── ① 真实 SSE 文本 → 应产出卡片 ──
  const ev = {
    confirmation_id: 900003,
    tool: 'import_sysml_text',
    tool_type: 'mcp',
    level: 'confirm',
    match_key: 'mcp_destructive',
    kind: 'destructive',
    preview: '\u5bfc\u5165 SysML v2 \u6e90\u7801\uff08\u8986\u76d6\uff09',
    arguments: { text: 'package X;' },
    batch_count: 2,
    batch_merged: true,
    conversation_id: 9700
  };
  const sse = 'event: hil_confirm\ndata: ' + JSON.stringify(ev) + '\n\n';
  let threw = false, errMsg = '';
  try {
    handleSSE(sse);
  } catch (e) {
    threw = true;
    errMsg = String(e && e.message || e).slice(0, 160);
  }
  await sleep(300);
  const el = document.querySelector('[data-pid="hil_900003"]');
  out.viaSSE = {
    threw,
    errMsg,
    cardCreated: !!el,
    cardCountGrew: document.querySelectorAll('[data-pid^="hil_"]').length > before,
    hasTarget: el ? (el.textContent.indexOf('\u8986\u76d6') >= 0) : false
  };

  // ── ② 未知事件类型 → 静默忽略（老前端兼容）──
  const nBefore = document.querySelectorAll('[data-pid^="hil_"]').length;
  let threw2 = false, err2 = '';
  try {
    handleSSE('event: future_unknown_event\ndata: {"x":1}\n\n');
  } catch (e) {
    threw2 = true;
    err2 = String(e && e.message || e).slice(0, 160);
  }
  await sleep(200);
  out.unknownEvent = {
    threw: threw2,
    errMsg: err2,
    cardCountUnchanged: document.querySelectorAll('[data-pid^="hil_"]').length === nBefore,
    timelineAlive: (typeof _procS !== 'undefined' && _procS.timeline) ? _procS.timeline.length > 0 : false
  };

  // ── ③ 事件分支存在性（静态反查，防止"只有函数没有分支"）──
  try {
    const src = await (await fetch('/static/js/mods/11-pipeline.js', { cache: 'no-store' })).text();
    out.staticBranch = {
      branchPresent: src.indexOf("evType === 'hil_confirm'") >= 0,
      fnPresent: src.indexOf('function procAddHilConfirm') >= 0,
      // 反面证据：旧的"正则抠确认单号"路径应仍在（兼容期双写），但新事件已是一等公民
      legacyRegexStillThere: src.indexOf('\\u786e\\u8ba4\\u5355') >= 0 || /确认单/.test(src)
    };
  } catch (e) {
    out.staticBranch = { err: String(e && e.message || e).slice(0, 120) };
  }

  return JSON.stringify(out);
})()
