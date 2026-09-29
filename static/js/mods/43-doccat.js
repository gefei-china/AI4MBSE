/* 文档知识类别（2026-09-28）：类别选择器 + 规则自动归集
 * 背景：资料库列表「知识类别」列原先只有「纯文本输入」（kbSetDocCategory → promptDialog），
 *      用户要求：① 支持自动归集；② 支持人工编辑/修改、选择已有标签、删除已有标签。
 * 边界：类别名必须已存在于 knowledge_categories（PUT /api/documents/{id}/category 会校验，
 *      不存在直接 400），故本模块只做「选已有 / 改选 / 清除」，**不新建自定义类别**。
 * 依赖：KB_CATS / kbCatLabel（15-kb.js）、#lbx 卡片（01-core.js）、_docs（20-docs.js）、api/toast/esc/escA（01-core.js）。
 * 加载位置：index.html 末尾（依赖页面 DOM，#lbx 已就位）。
 */

/* ── 类别选项兜底：直接进「资料库」时看板可能还没拉过类别 ── */
async function kbCatEnsure() {
  if (KB_CATS && KB_CATS.length) return KB_CATS;
  try { KB_CATS = (await api('/api/knowledge/categories')) || []; } catch (e) { KB_CATS = []; }
  return KB_CATS;
}

/* ── 类别选择器（单选 + 清除=删除标签）
   返回：类别名（设置）/ ''（清除）/ null（取消） ── */
async function kbCategoryPicker(current) {
  const cats = await kbCatEnsure();
  const lbx = document.getElementById('lbx'), b = document.getElementById('lbx-body');
  if (!lbx || !b) return null;
  if (!cats.length) { toast('暂无可用知识类别'); return null; }
  const cur = current || '';
  const cardEl = document.querySelector('#lbx .lbx-card');
  if (cardEl) { cardEl.classList.remove('wide'); cardEl.classList.add('wide'); }   // 9 类带说明，需要更宽
  const groups = [];
  cats.forEach(c => {
    const gn = c.group_name || '未分组';
    let g = groups.find(x => x.name === gn);
    if (!g) { g = { name: gn, items: [] }; groups.push(g); }
    g.items.push(c);
  });
  const rowOf = c => `<label class="kcp-opt${cur === c.name ? ' on' : ''}">
      <input type="radio" name="kcp-cat" value="${escA(c.name)}" ${cur === c.name ? 'checked' : ''}>
      <span class="kcp-name">${esc(c.name)}</span>
      <span class="kcp-desc">${esc(c.description || '')}</span></label>`;
  b.innerHTML = `<h3>设置知识类别</h3>
    <div style="font-size:12.5px;color:var(--mut);line-height:1.6;margin:4px 0 10px;">
      单选一个已有类别；「🗑 清除分类」= 删除该文档当前的类别标签（回到未分类）。</div>
    <div class="kcp-list">
      ${groups.map(g => `<div class="kcp-grp">
        <div class="kcp-gh">${g.name === '设计方法知识' ? '📐' : '🧩'} ${esc(g.name)}</div>
        ${g.items.map(rowOf).join('')}</div>`).join('')}
    </div>
    <div class="lbx-actions">
      <button class="btn ghost" id="kcp-clear" title="删除该文档当前的类别标签">🗑 清除分类</button>
      <span style="flex:1"></span>
      <button class="btn ghost" id="kcp-cancel">取消</button>
      <button class="btn" id="kcp-ok">保存</button>
    </div>`;
  lbx.classList.add('show');
  return new Promise(resolve => {
    const done = v => {
      lbx.classList.remove('show');
      if (cardEl) cardEl.classList.remove('wide');
      document.removeEventListener('keydown', _kd);
      resolve(v);
    };
    const sync = () => document.querySelectorAll('#lbx .kcp-opt')
      .forEach(l => l.classList.toggle('on', !!(l.querySelector('input') || {}).checked));
    document.querySelectorAll('#lbx input[name="kcp-cat"]').forEach(i => i.addEventListener('change', sync));
    document.getElementById('kcp-ok').onclick = () => {
      const r = document.querySelector('#lbx input[name="kcp-cat"]:checked');
      done(r ? r.value : '');
    };
    document.getElementById('kcp-clear').onclick = () => done('');
    document.getElementById('kcp-cancel').onclick = () => done(null);
    const _kd = e => { if (e.key === 'Escape') done(null); };
    document.addEventListener('keydown', _kd);
    lbx.onclick = e => { if (e.target === lbx) done(null); };
    const cb = document.querySelector('#lbx .lbx-close');
    if (cb) cb.onclick = () => done(null);
  });
}

/* ── 规则自动归集（离线关键词打分：不引依赖、不调大模型、不上传内容）──
   权重 3=强特征词 / 2=中等 / 1=弱；同类别命中累加，取最高分；
   全 0 视为「无法判断」→ 不给建议（宁可不分类，也不乱归类）。 */
const DOC_CAT_RULES = [
  { cat: '设计准则', kws: { '准则': 3, '规范': 3, '强制性': 3, '标准': 2, '约束': 2, '规则': 2, '合规': 2, '要求': 1 } },
  { cat: '设计流程', kws: { '流程': 3, '步骤': 3, '工作流': 3, '过程': 2, '阶段': 2, '程序': 2, '方法': 1 } },
  { cat: '最佳实践', kws: { '最佳实践': 3, '经验': 3, '教训': 3, '复盘': 3, '实践': 2, '总结': 2, '心得': 2 } },
  { cat: '模板方法', kws: { '模板': 3, '骨架': 3, '脚手架': 3, '样板': 3, 'template': 3 } },
  { cat: '已有模型', kws: { '模型库': 3, '建模': 3, 'sysml': 3, '模型': 2, '架构': 2 } },
  { cat: '可复用构件', kws: { '构件': 3, '组件': 3, '复用': 3, '模块': 2, '部件': 2, '接口': 2 } },
  { cat: '设计方案', kws: { '设计方案': 3, '选型': 3, '概念设计': 3, '方案': 2, '总体设计': 2 } },
  { cat: '案例库', kws: { '案例': 3, '用例': 3, '样例': 3, '实例': 2, '示例': 2, 'case': 2 } },
  { cat: '参数设计', kws: { '参数': 3, '取值': 3, '标定': 3, '阈值': 2, '指标': 2, '配置': 1 } },
];

/* 单篇文档 → 建议类别；无命中返回 null。
   打分语料 = 文件名 + 标题 + 标签（**不含 domain**：实测 domain 是全库统一值
   （如 sysml_norm），'sysml' 会给每篇文档恒加分，把一切都推向「已有模型」，
   并让「用例→案例库」这类真信号在并列时被压掉）。均取自列表已有字段，无需额外请求。 */
function docCatSuggest(d) {
  if (!d) return null;
  const hay = [d.filename, d.title, d.tags].filter(Boolean).join(' ').toLowerCase();
  if (!hay) return null;
  let best = null;
  DOC_CAT_RULES.forEach(r => {
    let score = 0; const hits = [];
    Object.keys(r.kws).forEach(kw => {
      if (hay.indexOf(kw) >= 0) { score += r.kws[kw]; hits.push(kw); }
    });
    if (score > 0 && (!best || score > best.score)) best = { cat: r.cat, score: score, hits: hits };
  });
  if (!best) return null;
  return { cat: best.cat, score: best.score, reason: '命中「' + best.hits.join('」「') + '」' };
}

/* ── 自动归集：先试算列表（文档 → 建议类别 + 命中依据）→ 用户确认 → 逐条落库 ──
   默认只勾选「当前未分类」的文档；已分类文档不会被静默覆盖（需手动勾选）。 */
async function kbAutoCollectPreview() {
  const cats = await kbCatEnsure();
  if (!cats.length) { toast('暂无可用知识类别'); return; }
  const docs = (_docs || []).slice();
  if (!docs.length) { toast('当前列表没有文档'); return; }
  const rows = docs.map(d => {
    const s = docCatSuggest(d);
    return { id: d.id, filename: d.filename, cur: d.knowledge_category || '',
             sug: s ? s.cat : '', reason: s ? s.reason : '关键词规则无命中' };
  }).filter(r => r.sug);
  if (!rows.length) { toast('未匹配到可自动归集的文档（关键词规则无命中）'); return; }

  const preselect = new Set(rows.filter(r => !r.cur).map(r => r.id));
  const nUn = preselect.size;
  const lbx = document.getElementById('lbx'), b = document.getElementById('lbx-body');
  b.innerHTML = `<h3>🤖 自动归集知识类别（试算）</h3>
    <div style="font-size:12.5px;color:var(--mut);line-height:1.6;margin:4px 0 10px;">
      按「文件名 / 标题 / 标签」的关键词规则试算：<b>${rows.length}</b> 篇有建议${nUn
        ? `，其中 <b>${nUn}</b> 篇当前未分类（已默认勾选）`
        : '（当前均已有类别，需手动勾选才会覆盖）'}。试算结果**不会自动写库**，确认后逐条落库。</div>
    <div style="max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
      <table class="t kac-tbl" style="width:100%;font-size:12px;">
        <tr><th style="width:34px;"><input type="checkbox" id="kac-all"></th>
          <th style="text-align:left;">文档</th><th style="text-align:left;">当前类别</th>
          <th style="text-align:left;">建议类别</th><th style="text-align:left;">命中依据</th></tr>
        ${rows.map(r => `<tr>
          <td><input type="checkbox" class="kac-ck" value="${r.id}" ${preselect.has(r.id) ? 'checked' : ''}></td>
          <td class="kac-doc" title="${escA(r.filename)}">${esc(r.filename)}</td>
          <td>${r.cur ? kbCatLabel(r.cur) : '<span style="color:var(--mut);">未分类</span>'}</td>
          <td>${kbCatLabel(r.sug)}</td>
          <td style="color:var(--mut);">${esc(r.reason)}</td></tr>`).join('')}
      </table>
    </div>
    <div class="lbx-actions">
      <span id="kac-count" style="flex:1;align-self:center;font-size:11.5px;color:var(--mut);"></span>
      <button class="btn ghost" id="kac-cancel">取消</button>
      <button class="btn" id="kac-ok">应用归集</button>
    </div>`;
  const kacCard = document.querySelector('#lbx .lbx-card');
  if (kacCard) kacCard.classList.add('wide');    // 5 列试算表，420px 会被挤成竖排字
  lbx.classList.add('show');

  const cks = () => Array.from(document.querySelectorAll('#lbx .kac-ck'));
  const upd = () => {
    const n = cks().filter(c => c.checked).length;
    document.getElementById('kac-count').textContent = `已勾选 ${n} / ${rows.length} 篇`;
    const all = document.getElementById('kac-all');
    if (all) all.checked = n === rows.length && rows.length > 0;
  };
  cks().forEach(c => c.addEventListener('change', upd));
  document.getElementById('kac-all').onchange = e => {
    cks().forEach(c => { c.checked = e.target.checked; });
    upd();
  };
  upd();

  const close = () => {
    lbx.classList.remove('show');
    if (kacCard) kacCard.classList.remove('wide');
    document.removeEventListener('keydown', _kd);
  };
  const _kd = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', _kd);
  document.getElementById('kac-cancel').onclick = close;
  lbx.onclick = e => { if (e.target === lbx) close(); };
  const cb = document.querySelector('#lbx .lbx-close');
  if (cb) cb.onclick = close;

  document.getElementById('kac-ok').onclick = async () => {
    const picked = new Set(cks().filter(c => c.checked).map(c => Number(c.value)));
    const sel = rows.filter(r => picked.has(r.id));
    if (!sel.length) { toast('请先勾选要归集的文档'); return; }
    close();
    let ok = 0, fail = 0;
    for (const r of sel) {
      try {
        const res = await api(`/api/documents/${r.id}/category`, { method: 'PUT', body: JSON.stringify({ category: r.sug }) });
        if (res && res.error) fail++; else ok++;
      } catch (e) { fail++; }
    }
    toast(`🤖 自动归集完成：成功 ${ok} 篇${fail ? ` · 失败 ${fail} 篇` : ''}`);
    loadDocs();
  };
}
