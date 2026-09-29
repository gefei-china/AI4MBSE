(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = {};
  const QS = NEWQS;                 // 会话 337 / 消息 2790 的真库题目（3 题、每题 3-4 选项）
  out.qSourceCount = QS.length;

  if (typeof selectConv === 'function') selectConv(337);
  await sleep(6500);
  if (typeof go === 'function') go('ai');
  await sleep(400);

  // ── 路径① 历史还原（05-markdown.js）：挂起非空 → 用 card_data.questions 还原成可作答卡 ──
  if (typeof setPendingClarify === 'function') setPendingClarify({ questions: QS });
  if (typeof selectConv === 'function') selectConv(337);
  await sleep(6500);
  const hist = document.querySelector('.clarify-ask-card.hist');
  out.histCardRendered = !!hist;                       // 端到端路径真的挂了卡
  out.histCardStepCount = hist ? hist.querySelectorAll('.clarify-step').length : -1;

  // ── 路径② 生产入口渲染：真库题目直接喂给 clarifyCardInnerHtml（选项/题数都是真数据）──
  const host = document.getElementById('chat-area');
  if (!host) return JSON.stringify({ err: 'no chat-area' });
  const holder = document.createElement('div');
  holder.className = 'msg ai'; holder.id = 'verify-clarify';
  holder.innerHTML = '<div class="msg-inner"><div class="body"></div></div>';
  host.appendChild(holder);
  const body = holder.querySelector('.body');
  body.innerHTML = '<div class="clarify-ask-card" id="clarify-ask">' + clarifyCardInnerHtml(QS, '') + '</div>';
  await sleep(300);
  const card = holder.querySelector('.clarify-ask-card');
  if (typeof clarifyGo === 'function') clarifyGo(card, 0);

  const steps = Array.from(card.querySelectorAll('.clarify-step'));
  const visIdx = () => {
    const v = steps.filter(s => getComputedStyle(s).display !== 'none');
    return { n: v.length, i: v.length ? steps.indexOf(v[0]) : -1 };
  };
  out.stepsTotal = steps.length;                    // 3 题 + 1 补充说明 = 4
  out.qSteps = card.querySelectorAll('.clarify-q').length;
  const v0 = visIdx();
  out.visibleInit = v0.n;                           // 一次一题 → 只看见 1 步
  out.firstIdx = v0.i;
  out.progInit = (card.querySelector('.clarify-prog') || {}).textContent || '';
  const box0 = steps[0].querySelector('.cq-custom-box');
  const inp0 = box0 && box0.querySelector('.cq-custom');
  out.customHiddenInit = !!box0 && getComputedStyle(box0).display === 'none';
  out.customNoLayoutInit = !!inp0 && inp0.offsetParent === null;   // 真不可见（非只看 display 串）
  out.optCountQ1 = steps[0].querySelectorAll('input[type=radio]').length;
  out.optsAreReal = out.optCountQ1 === (QS[0].options || []).length + 1;   // 真选项 + 「其他」

  // ① 选「其他 / 自定义」→ 输入框才展开
  const cust = steps[0].querySelector('input[value="__custom__"]');
  if (cust) cust.click();
  await sleep(250);
  out.customShownAfterPick = !!box0 && getComputedStyle(box0).display !== 'none';
  out.customHasLayoutAfter = !!inp0 && inp0.offsetParent !== null;
  if (inp0) inp0.value = 'PlantUML，另附端口与值属性';

  // ② 下一个 → 第 2 题
  card.querySelector('.cq-next').click();
  await sleep(250);
  const v1 = visIdx();
  out.idxAfterNext = v1.i;
  out.visibleAfterNext = v1.n;
  out.progAfterNext = (card.querySelector('.clarify-prog') || {}).textContent || '';
  out.prevVisibleAfterNext = getComputedStyle(card.querySelector('.cq-prev')).visibility !== 'hidden';
  const opt2 = steps[1].querySelector('input[type=radio]');
  if (opt2) opt2.click();
  await sleep(150);

  // ③ 一路下一个 → 末步（补充说明）
  card.querySelector('.cq-next').click(); await sleep(180);
  card.querySelector('.cq-next').click(); await sleep(250);
  const v2 = visIdx();
  out.lastIdx = v2.i;
  out.lastIsFinal = v2.i === steps.length - 1;
  out.visibleOnLast = v2.n;
  const noteEl = card.querySelector('.cq-note');
  out.noteIsTextarea = !!noteEl && noteEl.tagName === 'TEXTAREA';
  out.noteRows = noteEl ? Number(noteEl.rows) : 0;
  out.noteResize = noteEl ? getComputedStyle(noteEl).resize : '';
  if (noteEl) noteEl.value = '补充：按 EV 乘用车热管理口径建模，冷却液为 50/50 乙二醇';
  const send = card.querySelector('.cq-send');
  out.sendVisibleOnLast = !!send && getComputedStyle(send).display !== 'none';
  out.nextHiddenOnLast = getComputedStyle(card.querySelector('.cq-next')).display === 'none';
  out.progOnLast = (card.querySelector('.clarify-prog') || {}).textContent || '';

  // ④ 上一个 → 回到第 3 题
  card.querySelector('.cq-prev').click();
  await sleep(220);
  out.idxAfterPrev = visIdx().i;

  // ⑤ 变异守卫：选了「其他」却没填 → 不允许提交，跳回该题并提示
  window.__toasts = [];
  const _toast = window.toast;
  window.toast = (m) => { window.__toasts.push(String(m)); try { _toast(m); } catch (e) {} };
  if (typeof clarifyGo === 'function') clarifyGo(card, 0);
  if (inp0) inp0.value = '';
  const _api0 = window.api; window.api = async () => { window.__badPost = 1; return {}; };
  if (typeof clarifyAnswerSend === 'function') await clarifyAnswerSend();
  await sleep(300);
  out.guardToastCount = (window.__toasts || []).length;
  out.guardJumpedToQ1 = card.dataset.step === '0';
  out.guardStepVisible = visIdx().i === 0;
  out.guardNoRequest = !window.__badPost;            // 守卫拦截 → 根本没发请求
  window.api = _api0;

  // ⑥ 正常提交：拦下 api 与 sendResume，检查真实请求体
  if (inp0) inp0.value = 'PlantUML，另附端口与值属性';
  window.__cap = null; window.__resume = null;
  const _api = window.api;
  window.api = async (u, o) => { window.__cap = { url: u, body: (o && o.body) || '' }; return { ok: true, resume_text: '【澄清补充】测试续答' }; };
  const _sr = window.sendResume;
  window.sendResume = (t) => { window.__resume = t; };
  if (typeof clarifyAnswerSend === 'function') await clarifyAnswerSend();
  await sleep(400);
  const cap = window.__cap || {};
  out.postUrl = String(cap.url || '');
  let b = {};
  try { b = JSON.parse(cap.body || '{}'); } catch (e) { b = {}; }
  out.hasAnswers = Array.isArray(b.answers);
  out.answerCount = Array.isArray(b.answers) ? b.answers.length : -1;
  out.answerValues = Array.isArray(b.answers) ? b.answers.map(a => String(a.value)) : [];
  out.noCustomPlaceholder = out.answerValues.every(v => v.indexOf('__custom__') < 0);
  out.noteSent = String(b.note || '');
  out.noteHasText = String(b.note || '').length > 5;
  out.resumeCalled = !!window.__resume;
  // 缓存自证：05-markdown.js 把落库 card_data 按消息 id 缓存为**对象**（供执行详情面板读原始数据）。
  // 这里断言它确实是对象且含真题目 —— 若解包链坏了（字符串未 parse），澄清卡历史还原会静默只读。
  out.cacheIsObject = (function () {
    const c = window._msgCardCache;
    if (!c) return null;
    const v = c[2790];
    return v === undefined ? null : (typeof v === 'object' && !!v && Array.isArray(v.questions));
  })();
  window.api = _api; window.sendResume = _sr; window.toast = _toast;
  // 清场：删掉本 payload 造的卡与挂起，避免污染真实库/界面
  holder.remove();
  if (typeof setPendingClarify === 'function') setPendingClarify(null);
  return JSON.stringify(out);
})()
