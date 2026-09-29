// ── 设置·意图样本（st-intent-samples tab）──
// 2026-09-26：意图识别**评测样本池**的维护入口（产品闭环 = 采样 → 标注 → 评测）。
// 数据源：GET /api/intent-samples（列表 + stats）、POST .../import|suggest|run-eval、
//         PUT/DELETE /api/intent-samples/{id}、GET .../export。
// 为什么页面必须把状态口径摆在最显眼处（这决定标注有没有意义）：
//   status = new（仅采集，系统还没给建议）/ suggested（系统给了建议，等人确认，**只是待办**）
//          / confirmed（**人工确认，才会进评测集**）/ rejected（人工判定不是有效样本）。
//   `intent` 在 suggested 阶段存的是**系统建议值**，`hit_intent`/`hit_route`/`hit_conf` 是**采集当时**的系统判定，
//   两者分列存储，才能让"人标的"与"机器猜的"同屏对照。
//   ⚠️ 把 suggested 直接当标签去跑评测 = 拿系统判定评系统自己 → 指标永远 100%，纯自我循环。
//      所以「运行评测」只消费 confirmed（后端 confirmed_cases() 同口径），本页也据此解释空池原因。
// 约束：本页只维护样本池，**不改路由规则/词表**（那只在「意图规则」配置里改），避免越权。

// ── 静态元数据（意图/状态的中文名与配色）────────────────────────────────────
// 后端 intents 列表是唯一事实源，此处只做**默认兜底**（首次请求回来前也要能渲染标签），
// 且仅承担"给人看的名字"这一件事 —— 不做任何合法性判断。
const IS_INTENTS = ['requirement_analysis', 'requirement_quality', 'design', 'impact',
  'review', 'report_generation', 'system_mgmt', 'knowledge_qa', 'chat'];
const IS_INTENT_LABEL = {
  requirement_analysis: '需求分析', requirement_quality: '需求质量', design: '设计/建模',
  impact: '影响分析', review: '评审', report_generation: '报告生成',
  system_mgmt: '系统管理', knowledge_qa: '知识问答', chat: '闲聊',
};
// cls 用既有 .st 配色（ok=绿已确认 / w=黄待确认 / g=灰新采集 / r=红已驳回），与全站状态徽章同一视觉语言
const IS_STATUS_META = {
  new: { label: '新采集', cls: 'g', hint: '仅采集进池，系统尚未给建议' },
  suggested: { label: '待确认', cls: 'w', hint: '系统已给建议标签，需人工确认后才进评测' },
  confirmed: { label: '已确认', cls: 'ok', hint: '人工确认的人工标签，参与评测' },
  rejected: { label: '已驳回', cls: 'r', hint: '人工判定不是有效样本，永不进评测' },
};

let _isIntents = IS_INTENTS.slice();   // 来自后端 intents（兜底为内置 9 个）
let _isQTimer = null;                  // 关键词输入防抖（服务端筛选，避免每敲一键都打接口）

function isIntentLabel(k) { return IS_INTENT_LABEL[k] || k || '—'; }
function isStatusBadge(s) {
  const m = IS_STATUS_META[s] || { label: s || '—', cls: 'g', hint: '' };
  return `<span class="st ${m.cls}" title="${escA(m.hint)}">${esc(m.label)}</span>`;
}
/** 后端失败响应的统一取文案顺序：message（人话）> error > reason（机器码）。 */
function _isErrMsg(r) {
  if (!r) return '无响应';
  return r.message || r.error || r.reason || '未知原因';
}

// ── 加载与渲染 ─────────────────────────────────────────────────────────────
async function loadIntentSamples() {
  const body = document.getElementById('intent-sample-body');
  if (!body) return;
  // 骨架只建一次：评测结果卡片独立占位，刷新列表时不会被清掉（否则刚跑完的分就被自己刷没了）
  if (!document.getElementById('is-list')) {
    body.innerHTML = '<div id="is-stats"></div><div id="is-eval"></div>'
      + '<div id="is-list"><div class="loading">加载中…</div></div>';
  }
  const val = id => { const el = document.getElementById(id); return el ? el.value : ''; };
  const qs = new URLSearchParams();
  if (val('is-f-status')) qs.set('status', val('is-f-status'));
  if (val('is-f-intent')) qs.set('intent', val('is-f-intent'));
  const q = String(val('is-f-q') || '').trim();
  if (q) qs.set('q', q);
  qs.set('limit', '200');
  try {
    const d = await api('/api/intent-samples?' + qs.toString());
    if (d && d.error) { toast.error('加载失败：' + d.error); return; }
    if (d && d.intents && d.intents.length) _isIntents = d.intents;
    isFillIntentFilter();
    isRenderStats(d);
    isRenderList(d);
  } catch (e) {
    const box = document.getElementById('is-list');
    if (box) box.innerHTML = '<div class="loading">加载失败：' + esc(e.message || e) + '</div>';
    toast.error('加载失败：' + (e.message || e));
  }
}

/** 意图筛选下拉的选项来自后端 intents；只填一次并保留用户当前选中值（重复刷新不打断筛选）。 */
function isFillIntentFilter() {
  const sel = document.getElementById('is-f-intent');
  if (!sel) return;
  const cur = sel.value;
  const sig = _isIntents.join(',');
  if (sel.dataset.sig === sig) return;
  sel.innerHTML = '<option value="">意图：全部</option>'
    + _isIntents.map(k => `<option value="${escA(k)}">${esc(isIntentLabel(k))}</option>`).join('');
  sel.dataset.sig = sig;
  if (cur) sel.value = cur;
}

/** 顶部统计徽章：stats.by_status 是**全池口径**（不受筛选影响），故筛选后数字不变 —— 这是刻意的。 */
function isRenderStats(d) {
  const el = document.getElementById('is-stats');
  if (!el) return;
  const bs = (d && d.stats && d.stats.by_status) || {};
  const n = k => bs[k] || 0;
  const total = Object.keys(bs).reduce((a, k) => a + (bs[k] || 0), 0);
  el.innerHTML = `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;">
    <span class="badge a" title="new（仅采集）+ suggested（系统给了建议、待人工确认）">待确认 ${n('new') + n('suggested')}</span>
    <span class="badge g" title="人工确认过，唯一参与评测的部分">已确认 ${n('confirmed')}</span>
    <span class="badge r" title="人工判定为无效样本">已驳回 ${n('rejected')}</span>
    <span class="badge" title="样本池总行数（按不同说法去重计数）">总数 ${total}</span>
    <span style="font-size:11px;color:var(--mut);">只有「已确认」会进评测 —— 系统建议（suggested）只是待办，拿它当标签是自我循环</span>
  </div>`;
}

function isRenderList(d) {
  const el = document.getElementById('is-list');
  if (!el) return;
  const items = (d && d.items) || [];
  const total = (d && d.total) || 0;
  if (!items.length) {
    el.innerHTML = '<div class="panel"><div class="ph">样本列表 <span class="badge">0</span></div>'
      + '<div class="pb" style="text-align:center;color:var(--mut);padding:18px;font-size:12px;">'
      + '没有匹配的样本 —— 可先点上方「从内置 29 例导入」建立评测基线，或「从历史回填」把线上真实说法采进来</div></div>';
    return;
  }
  el.innerHTML = `<div class="panel">
    <div class="ph">样本列表 <span class="badge">${items.length} / ${total}</span>
      <span style="flex:1"></span>
      <span style="font-size:10.5px;font-weight:400;color:var(--mut);">按「待标注优先 + 高频优先」排序：先标真实高频说法，性价比最高</span></div>
    <div class="pb" style="padding:0;"><table class="t">
      <tr><th style="min-width:260px;">用户说法</th><th>标签（人工）</th><th>状态</th><th>来源</th>
        <th>频次</th><th>系统判定对照</th><th style="white-space:nowrap;">操作</th></tr>
      ${items.map(isRow).join('')}
    </table></div></div>`;
}

function isRow(s) {
  const label = s.intent || '';
  // 对照列只在"还没人工确认"时有意义：confirmed 之后 intent 就是人工标签，
  // 再拿系统旧判定来比只会混淆"谁是谁"（人标的就是基准，不是待核对项）。
  const pend = s.status === 'new' || s.status === 'suggested';
  let cmp = '<span style="color:var(--mut);">—</span>';
  if (pend) {
    const hit = s.hit_intent || '';
    if (!hit) cmp = '<span style="font-size:10.5px;color:var(--mut);">无历史判定</span>';
    else {
      const same = hit === label;
      cmp = `<span class="st ${same ? 'ok' : 'w'}" title="采集当时的系统判定：${escA(isIntentLabel(hit))}">`
        + `${esc(isIntentLabel(hit))}${same ? '' : ' ≠ 标签'}</span>`
        + `<div style="font-size:10.5px;color:var(--mut);margin-top:2px;">${esc(s.hit_route || '—')}`
        + ` · 置信 ${(Number(s.hit_conf) || 0).toFixed(2)}</div>`;
    }
  }
  // 标签下拉**不给空值选项**：空标签一旦提交会在后端把已有标注清空（intent='' 是有效赋值）。
  // 未标注的行用 disabled 占位项表达"还没选"，用户一旦选中就只能选到 9 个合法意图之一。
  const opts = _isIntents.map(k =>
    `<option value="${escA(k)}"${k === label ? ' selected' : ''}>${esc(isIntentLabel(k))}</option>`).join('');
  const ph = label ? '' : '<option value="" selected disabled>（未标注）</option>';
  return `<tr>
    <td style="max-width:420px;">
      <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escA(s.text)}">${esc(s.text)}</div>
      ${s.note ? `<div style="font-size:10.5px;color:var(--mut);">备注：${esc(s.note)}</div>` : ''}</td>
    <td><select id="is-int-${s.id}" onchange="isSetIntent(${s.id},this.value)"
      style="border:1px solid var(--line);border-radius:6px;padding:3px 6px;font-size:11.5px;max-width:130px;">
      ${ph}${opts}</select></td>
    <td>${isStatusBadge(s.status)}</td>
    <td style="font-size:11px;color:var(--mut);">${esc(s.source || '—')}</td>
    <td style="font-size:11.5px;text-align:center;">${Number(s.seen_count) || 0}</td>
    <td>${cmp}</td>
    <td style="white-space:nowrap;">
      <button class="btn sm" onclick="isConfirm(${s.id})"${s.status === 'confirmed' ? ' disabled' : ''}>确认</button>
      <button class="btn sm ghost" onclick="isReject(${s.id})"${s.status === 'rejected' ? ' disabled' : ''}>驳回</button>
      <button class="btn sm red ghost" onclick="isDelete(${s.id})">删除</button>
    </td></tr>`;
}

// ── 行内操作 ───────────────────────────────────────────────────────────────
/** 标签下拉变更：**只改标签、不改状态** —— 标注与确认是两件事，合并会让"确认"这个人工动作消失。 */
async function isSetIntent(id, v) {
  if (!v) return;
  try {
    const r = await api('/api/intent-samples/' + id, { method: 'PUT', body: JSON.stringify({ intent: v }) });
    if (r && r.ok === false) { toast.error('改标签失败：' + _isErrMsg(r)); return; }
    toast.success('标签已更新：' + isIntentLabel(v));
    loadIntentSamples();
  } catch (e) { toast.error('改标签失败：' + (e.message || e)); }
}

/** 确认：把下拉里选中的值一并提交，避免被后端 confirm_requires_intent 挡回时用户不知道要干什么。 */
async function isConfirm(id) {
  const sel = document.getElementById('is-int-' + id);
  const v = sel ? String(sel.value || '').trim() : '';
  if (!v) {
    toast.warn('请先在下拉里选择意图再点确认 —— 空标签进评测会产生"期望为空"的用例');
    if (sel) sel.focus();
    return;
  }
  try {
    // 只传非空 intent：后端 update() 把 intent='' 也当有效赋值，传空会把已有标注清掉
    const r = await api('/api/intent-samples/' + id, { method: 'PUT', body: JSON.stringify({ status: 'confirmed', intent: v }) });
    if (r && r.ok === false) { toast.error('确认失败：' + _isErrMsg(r)); return; }
    toast.success('已确认「' + isIntentLabel(v) + '」，进评测集');
    loadIntentSamples();
  } catch (e) { toast.error('确认失败：' + (e.message || e)); }
}

async function isReject(id) {
  try {
    const r = await api('/api/intent-samples/' + id, { method: 'PUT', body: JSON.stringify({ status: 'rejected' }) });
    if (r && r.ok === false) { toast.error('驳回失败：' + _isErrMsg(r)); return; }
    toast.success('已驳回（该说法不进评测集）');
    loadIntentSamples();
  } catch (e) { toast.error('驳回失败：' + (e.message || e)); }
}

async function isDelete(id) {
  if (!(await confirmDialog('确认删除这条样本？\n删除后不可恢复（同一说法再次被采集时会重新进池）。'))) return;
  try {
    const r = await api('/api/intent-samples/' + id, { method: 'DELETE' });
    if (r && r.ok === false) { toast.error('删除失败：' + _isErrMsg(r)); return; }
    toast.success('已删除');
    loadIntentSamples();
  } catch (e) { toast.error('删除失败：' + (e.message || e)); }
}

// ── 工具栏动作（长耗时的禁用 + 文案切换在此统一处理）────────────────────────
/** 请求期间禁用按钮并改文案：suggest/run-eval 单次可跑数十秒，不给反馈会被当成"点了没反应"。 */
function _isBtn(id, busy, text) {
  const b = document.getElementById(id);
  if (!b) return;
  if (busy) {
    // 用 dataset 暂存原文案：用 "undefined 判定"而非真值判定 —— 否则原文案恰好为空串时会还原成空按钮
    if (b.dataset.orig === undefined) b.dataset.orig = b.innerHTML;
    b.disabled = true;
    b.style.opacity = '.65';
    b.innerHTML = text || '处理中…';
  } else {
    b.disabled = false;
    b.style.opacity = '';
    if (b.dataset.orig !== undefined) b.innerHTML = b.dataset.orig;
  }
}
function _isNum(id, dft) {
  const el = document.getElementById(id);
  const n = el ? parseInt(el.value, 10) : NaN;
  return (isFinite(n) && n > 0) ? n : dft;
}

/** 导入：builtin（内置 29 例，以 confirmed 入库，可直接评测）/ messages / query_trace（回填，落 suggested/new）。 */
async function isImport(src) {
  const isBuiltin = src === 'builtin';
  const btnId = isBuiltin ? 'is-btn-builtin' : 'is-btn-import';
  const body = { source: isBuiltin ? 'builtin' : (document.getElementById('is-import-src') || {}).value || 'messages' };
  if (!isBuiltin) body.limit = _isNum('is-import-limit', 200);
  _isBtn(btnId, true, isBuiltin ? '⏳ 导入中…' : '⏳ 回填中…');
  try {
    const r = await api('/api/intent-samples/import', { method: 'POST', body: JSON.stringify(body) });
    if (!r || r.ok === false) { toast.error('导入失败：' + _isErrMsg(r) + (r && r.path ? '（' + r.path + '）' : '')); return; }
    const extra = isBuiltin ? ''
      : `，扫描 ${r.scanned || 0} 条，命中历史判定 ${r.hint_hit || 0} 条`;
    toast.success(`${isBuiltin ? '内置 29 例' : '历史回填（' + body.source + '）'}：新增 ${r.added || 0} / 更新 ${r.updated || 0}${extra}`);
    await loadIntentSamples();
  } catch (e) { toast.error('导入失败：' + (e.message || e)); }
  finally { _isBtn(btnId, false); }
}

/** 补建议：对 status=new 的前 N 条跑**真实识别**写建议值（status→suggested），仍等人确认。 */
async function isSuggest() {
  const n = _isNum('is-suggest-limit', 40);
  _isBtn('is-btn-suggest', true, '⏳ 正在识别…');
  try {
    const r = await api('/api/intent-samples/suggest', { method: 'POST', body: JSON.stringify({ limit: n }) });
    if (!r || r.ok === false) { toast.error('补建议失败：' + _isErrMsg(r)); return; }
    const k = r.updated || 0;
    if (k) toast.success(`本次为 ${k} 条样本给出建议标注（还需人工确认才进评测）`);
    else toast('没有待建议的样本（status=new 已空）—— 可先「从历史回填」再补建议');
    await loadIntentSamples();
  } catch (e) { toast.error('补建议失败：' + (e.message || e)); }
  finally { _isBtn('is-btn-suggest', false); }
}

async function isRunEval() {
  const box = document.getElementById('is-eval');
  _isBtn('is-btn-eval', true, '⏳ 评测中…');
  // 先在卡片位置给出"在被处理"的说明：逐条真实识别、每条都可能走 embedding，秒数不好预估
  if (box) box.innerHTML = '<div class="panel"><div class="ph">评测结果</div>'
    + '<div class="pb" style="font-size:12px;color:var(--mut);">⏳ 正在用「已确认」样本逐条真实识别（并绕开意图缓存，避免测到历史结论）…请稍候。</div></div>';
  try {
    const m = await api('/api/intent-samples/run-eval', { method: 'POST', body: '{}' });
    if (!m || m.ok === false) {
      if (box) box.innerHTML = isEvalHtml(m);
      toast.warn((m && m.message) || '评测未执行');
      return;
    }
    if (box) box.innerHTML = isEvalHtml(m);
    toast.success(`评测完成：n=${m.n} 准确率 ${(Number(m.accuracy) * 100).toFixed(1)}% · macro-F1 ${m.macro_f1}`);
  } catch (e) {
    if (box) box.innerHTML = '';
    toast.error('评测失败：' + (e.message || e));
  } finally { _isBtn('is-btn-eval', false); }
}

function isEvalHtml(m) {
  if (!m || m.ok === false) {
    return `<div class="panel"><div class="ph">评测结果</div>
      <div class="pb" style="font-size:12px;color:var(--amb);">⚠️ ${esc((m && m.message) || '评测未执行')}
        <div style="color:var(--mut);font-size:11px;margin-top:6px;line-height:1.65;">
          评测只消费「已确认」样本。可先点「从内置 29 例导入」—— 那 29 例本就是人工整理的标注，入库即 confirmed、直接可评测；
          或给列表里的待确认样本选好标签后逐条「确认」。</div></div></div>`;
  }
  const per = m.per_class || [];
  const cm = m.confusion || {};
  const rows = Object.keys(cm);
  const cols = [];
  rows.forEach(a => Object.keys(cm[a] || {}).forEach(b => { if (cols.indexOf(b) < 0) cols.push(b); }));
  const wrong = m.wrong || [];
  const cell = (a, b) => {
    const v = (cm[a] || {})[b] || 0;   // 只列非零：全零格留空，一眼看出误差集中在哪
    const bg = a === b ? 'background:var(--grn-l);font-weight:600;' : (v ? 'background:var(--red-l);' : '');
    return `<td style="text-align:center;${bg}">${v || ''}</td>`;
  };
  return `<div class="panel">
    <div class="ph">评测结果
      <span class="badge">n=${m.n}</span>
      <span class="badge g">准确率 ${(Number(m.accuracy) * 100).toFixed(1)}%</span>
      <span class="badge a">macro-F1 ${m.macro_f1}</span>
      <span style="flex:1"></span>
      <span style="font-size:10.5px;font-weight:400;color:var(--mut);">口径与 tests/manual_verify/eval_intent_routing.py 一致，并与命令行复跑可对齐</span></div>
    <div class="pb">
      <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px;">分意图指标（n=该意图的期望样本数）</div>
      <table class="t" style="margin-bottom:12px;">
        <tr><th>意图</th><th>precision</th><th>recall</th><th>f1</th><th>n</th></tr>
        ${per.map(p => `<tr><td>${esc(isIntentLabel(p.intent))}
          <span style="font-size:10.5px;color:var(--mut);font-family:monospace;">${esc(p.intent)}</span></td>
          <td>${p.precision}</td><td>${p.recall}</td>
          <td style="font-weight:600;">${p.f1}</td><td>${p.n}</td></tr>`).join('')}
      </table>
      <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px;">混淆矩阵（行 = 期望，列 = 实得；只列非零）</div>
      <table class="t" style="margin-bottom:12px;">
        <tr><th>期望 ＼ 实得</th>${cols.map(c => `<th style="text-align:center;">${esc(isIntentLabel(c))}</th>`).join('')}</tr>
        ${rows.map(a => `<tr><td>${esc(isIntentLabel(a))}</td>${cols.map(b => cell(a, b)).join('')}</tr>`).join('')}
      </table>
      <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px;">错例（${wrong.length} 条；route/置信 是判定当时的实际路径）</div>
      ${wrong.length ? `<table class="t">
        <tr><th style="min-width:240px;">用户说法</th><th>期望</th><th>实得</th><th>route</th><th>置信</th></tr>
        ${wrong.map(w => `<tr>
          <td style="max-width:360px;"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escA(w.text)}">${esc(w.text)}</div></td>
          <td><span class="st ok">${esc(isIntentLabel(w.want))}</span></td>
          <td><span class="st r">${esc(isIntentLabel(w.got))}</span></td>
          <td style="font-size:11px;color:var(--mut);">${esc(w.route || '—')}</td>
          <td style="font-size:11.5px;">${(Number(w.confidence) || 0).toFixed(2)}</td></tr>`).join('')}
      </table>` : '<div style="font-size:12px;color:var(--grn);">✅ 没有错例</div>'}
    </div></div>`;
}

/** 导出评测集：只含 confirmed（可直接进版本库做回归基线），页面直接落盘 JSON。 */
async function isExport() {
  _isBtn('is-btn-export', true, '⏳ 导出中…');
  try {
    const d = await api('/api/intent-samples/export');
    if (!d || d.error) { toast.error('导出失败：' + _isErrMsg(d)); return; }
    const blob = new Blob([JSON.stringify(d, null, 2)], { type: 'application/json;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `intent-samples-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    toast.success(`已导出 ${((d && d.cases) || []).length} 条已确认用例`);
  } catch (e) { toast.error('导出失败：' + (e.message || e)); }
  finally { _isBtn('is-btn-export', false); }
}

// ── 筛选 ───────────────────────────────────────────────────────────────────
function isFilterChanged() { loadIntentSamples(); }
/** 关键词走服务端 LIKE：加防抖，免得每敲一个字符就打一次接口。 */
function isFilterQ() {
  clearTimeout(_isQTimer);
  _isQTimer = setTimeout(loadIntentSamples, 300);
}