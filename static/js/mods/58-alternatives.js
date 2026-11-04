/* ── 多方案对比卡（平台工具 `alternatives_score` 的呈现层） ──
 *
 * ★ 平台化说明（2026-11-03）：
 *   本模块**只负责呈现**，不认识"多方案"这个业务概念 ——
 *   它只认识「一个叫 alternatives_score 的工具返回了什么样的 JSON」。
 *   接入方式复用本仓既有的「按工具名渲染富组件」机制（与覆盖性工具 covTryCard 同款）：
 *     · 实时流式：static/js/mods/11-pipeline.js 的工具事件
 *     · 历史回放：static/js/mods/06-cards.js 的 exec.tools
 *
 *   ⇒ **新增能力不必改管线**：只要某个 Agent 绑定了该工具，
 *     它的调用结果就会自动被这里渲染成对比表。
 *   ⇒ 维度名 / 方向 / 权重 / 阈值 **全部读工具返回**，前端不硬编码。
 *
 * 数据形状（工具返回的 result 字段，JSON 字符串）：
 *   {
 *     "ok": true,
 *     "alternatives": {            // 对比矩阵
 *       "rows": [ {key,label,direction,unit,weight,cells:[{name,raw,norm,rationale}]} ],
 *       "columns": [ {name,total,rank,recommended,tie,eligible,coverage,
 *                     missing_dimensions:[],summary} ],
 *       "recommended": "方案A", "tie": false, "tie_candidates": [], "notes": []
 *     },
 *     "scoring": { version, weights, items:[...] },
 *     "dimensions": [ {key,label,direction,unit} ]
 *   }
 */
const ALT_TOOL_NAME = 'alternatives_score';

/**
 * 从 card_data.exec.tools 里按**工具名**取结果并渲染成对比卡。
 *
 * ⚠️ 平台化要点：本函数**不认识**"多方案"这个业务概念 ——
 *   它的判据只有「有一个叫 alternatives_score 的工具，其结果里带对比矩阵」。
 *   ⇒ 任何 Agent 绑定了该工具，调用结果都会自动被提升为消息级对比卡。
 *
 * 取**最后一次成功**的结果（模型可能重试多次，前几次的参数往往是错的）。
 */
function altCardFromTools(cd) {
  if (!cd) return null;
  const tools = (cd.exec && cd.exec.tools) || [];
  if (!Array.isArray(tools) || !tools.length) return null;
  let picked = null;
  for (const t of tools) {
    if (!t || t.name !== ALT_TOOL_NAME) continue;
    if (t.ok === false) continue;
    if (!t.result) continue;
    picked = t;                     // 取最后一次成功
  }
  if (!picked) return null;
  let o = null;
  try { o = JSON.parse(picked.result); } catch (e) { return null; }
  // ★ 2026-11-04：选型决策回显 —— `selected` 存在 card_data.alternatives
  //   （选型接口 `/api/alternatives/select` 回写），**工具结果里没有**；
  //   不合并的话，历史回放永远看不到"已经选过了"。
  const sel = cd && cd.alternatives && cd.alternatives.selected;
  if (sel && o.alternatives) o.alternatives.selected = sel;
  return altCardHtml(o);
}

function altTryCard(name, resultStr, args) {
  if (name !== ALT_TOOL_NAME) return null;
  if (typeof resultStr === 'string' && resultStr) {
    try {
      const o = JSON.parse(resultStr);
      return altCardHtml(o);
    } catch (e) { /* 非 JSON ⇒ 交回通用文本展示 */ }
  }
  return null;
}

/* 归一化条：norm ∈ [0,1]；缺失（null）标「未测量」 */
function altNormBar(norm){
  if(norm===null||norm===undefined) return '<div class="alt-bar alt-bar-na">未测量</div>';
  const pct = Math.max(0, Math.min(100, Math.round(norm*100)));
  const cls = norm>=0.66 ? 'alt-bar-hi' : (norm>=0.33 ? 'alt-bar-md' : 'alt-bar-lo');
  return '<div class="alt-bar"><i class="'+cls+'" style="width:'+pct+'%"></i></div>';
}

function altCardHtml(o){
  if(!o || !o.alternatives) return '';
  const alt = o.alternatives;
  const rows = alt.rows || [];
  const cols = alt.columns || [];
  if(!cols.length || !rows.length) return '';
  const esc = (typeof escapeHtml === 'function') ? escapeHtml : (s => String(s==null?'':s));

  /* 表头：方案名 + 排名 + 推荐标记（数据全来自工具返回） */
  const head = cols.map(c=>{
    let badge='';
    if(c.recommended){
      badge = c.tie
        ? '<span class="rc-chip a" title="与其它方案评分接近，建议人工选型">并列推荐</span>'
        : '<span class="rc-chip g" title="综合得分最高">★ 推荐</span>';
    } else if(c.eligible===false){
      badge = '<span class="rc-chip alt-chip-warn" title="测量维度不足，不参与推荐">数据不足</span>';
    }
    return '<th class="'+(c.recommended?'alt-col-rec':'')+'">'
         + '<div class="alt-colname">'+esc(c.name)+'</div>'
         + '<div class="alt-colmeta">第 '+esc(c.rank)+' 名 · 覆盖率 '+Math.round((c.coverage||0)*100)+'%</div>'
         + badge + '</th>';
  }).join('');

  /* 维度行：label/direction/weight 全读工具返回 */
  const body = rows.map(r=>{
    const dirTip = r.direction==='lower' ? '（越小越好）' : '（越大越好）';
    const wTxt = (r.weight!==undefined && r.weight!==null) ? ('权重 '+r.weight) : '';
    const cells = (r.cells||[]).map(c=>{
      const rat = c.rationale ? ' title="'+esc(c.rationale)+'"' : '';
      const raw = (c.raw===null||c.raw===undefined)
        ? '<span class="alt-na">—</span>'
        : '<span class="alt-raw">'+esc(c.raw)+'</span>';
      return '<td'+rat+'>'+raw+altNormBar(c.norm)+'</td>';
    }).join('');
    return '<tr><th class="alt-dim" scope="row">'
         + '<span class="alt-dimname">'+esc(r.label)+'</span>'
         + '<span class="alt-dimtip">'+esc(dirTip)+(wTxt?' · '+esc(wTxt):'')+'</span>'
         + '</th>'+cells+'</tr>';
  }).join('');

  /* 汇总行：综合得分 */
  const totalRow = '<tr class="alt-total"><th scope="row">综合得分</th>'
    + cols.map(c=>{
        const t = (c.total===null||c.total===undefined) ? '—' : (Number(c.total).toFixed(3));
        return '<td class="alt-total-val'+(c.recommended?' alt-total-rec':'')+'">'+esc(t)+'</td>';
      }).join('')
    + '</tr>';

  /* 推荐结论：并列 / 无推荐 / 正常推荐 —— 三种都要说清楚，不能含糊 */
  let verdict;
  if(alt.tie){
    verdict = '<div class="rc-chip a" style="margin-top:8px;">⚖ 并列推荐：'
            + esc((alt.tie_candidates||[]).join('、'))
            + '（评分接近，需人工选型）</div>';
  } else if(alt.recommended){
    verdict = '<div class="rc-chip g" style="margin-top:8px;">★ 推荐方案：'+esc(alt.recommended)+'</div>';
  } else {
    verdict = '<div class="rc-chip alt-chip-warn" style="margin-top:8px;">'
            + '⚠ 无推荐：测量数据不足，补齐维度后再选型</div>';
  }

  /* 缺失维度提示（按方案列） */
  const missList = (cols||[]).filter(c=>(c.missing_dimensions||[]).length).map(c=>
    '<span class="rc-chip alt-chip-warn">'+esc(c.name)+' 未测 '+(c.missing_dimensions||[]).length+' 维</span>').join(' ');

  /* 口径说明（notes，来自工具返回） */
  const notes = (alt.notes||[]).length
    ? '<div class="alt-notes">'+alt.notes.map(n=>'<div>'+esc(n)+'</div>').join('')+'</div>' : '';

  /* ★ 2026-11-04 选型闭环：已选定态 / 未选定的选型控件
   *   数据源：card_data.alternatives.selected（选型接口回写，历史回放可见）。
   *   会话 id 在**点击时**读取（currentConvId）—— 渲染时卡片可能先于会话切换生成。 */
  const sel = alt.selected;
  let selectCtl = '';
  if(sel && sel.name){
    selectCtl = '<div class="rc-chip g" style="margin-top:8px;">✅ 已选定：<b>'+esc(sel.name)+'</b>'
      + (sel.reason ? ' · 理由：'+esc(sel.reason) : '')
      + '<span style="font-weight:normal;font-size:12px;color:var(--mut);">（'+esc(sel.by||'')+' · '+esc(sel.at||'')+'）</span></div>';
  } else if(cols.length){
    const opts = cols.map(c=>'<option value="'+esc(c.name)+'">'+esc(c.name)
      + (c.recommended ? (c.tie ? '（并列推荐）' : '（★推荐）') : '') + '</option>').join('');
    selectCtl = '<div class="alt-select" style="margin-top:8px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
      + '<span style="font-size:12.5px;color:var(--mut);">人工选型：</span>'
      + '<select class="alt-sel-name" style="padding:3px 6px;border:1px solid var(--line,#d8dee8);border-radius:4px;background:var(--bg,#fff);color:var(--ink,#333);font-size:12.5px;">'+opts+'</select>'
      + '<input class="alt-sel-reason" placeholder="选型理由（可空）" style="flex:1;min-width:120px;padding:3px 8px;border:1px solid var(--line,#d8dee8);border-radius:4px;font-size:12.5px;background:var(--bg,#fff);color:var(--ink,#333);"/>'
      + '<button class="alt-sel-btn" onclick="altSubmitSelect(this)" style="padding:3px 12px;border:1px solid var(--pri,#3478f6);border-radius:4px;background:var(--pri,#3478f6);color:#fff;font-size:12.5px;cursor:pointer;">✔ 选定</button>'
      + '</div>';
  }

  const dimCount = rows.length;
  return '<div class="alt-wrap">'
    + '<div class="rc-chip b">🧭 多方案对比 · '+cols.length+' 个候选 · '+dimCount+' 个维度</div>'
    + '<div class="alt-scroll"><table class="alt-table"><thead><tr><th scope="col">评估维度</th>'+head+'</tr></thead>'
    + '<tbody>'+body+totalRow+'</tbody></table></div>'
    + verdict
    + selectCtl
    + (missList ? '<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;">'+missList+'</div>' : '')
    + notes
    + '</div>';
}

/* ★ 2026-11-04 选型提交：POST /api/alternatives/select → 后端写审计行 + 回写卡片，
 *   成功后就地把控件替换为"已选定"态（不刷新整页）。 */
async function altSubmitSelect(btn){
  const _esc = (typeof escapeHtml === 'function') ? escapeHtml : (s => String(s==null?'':s));
  const wrap = btn.closest('.alt-wrap');
  const nameEl = wrap && wrap.querySelector('.alt-sel-name');
  const reasonEl = wrap && wrap.querySelector('.alt-sel-reason');
  if(!nameEl) return;
  const name = nameEl.value;
  const reason = reasonEl ? reasonEl.value.trim() : '';
  const cid = (typeof currentConvId !== 'undefined' && currentConvId) ? currentConvId : null;
  if(!cid){ alert('未定位到当前会话，无法记录选型'); return; }
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const resp = await fetch('/api/alternatives/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: cid, selected_name: name, reason: reason })
    });
    const d = await resp.json();
    if(!d.ok) throw new Error(d.error || ('HTTP ' + resp.status));
    const ctl = wrap.querySelector('.alt-select');
    if(ctl) ctl.outerHTML = '<div class="rc-chip g" style="margin-top:8px;">✅ 已选定：<b>'+_esc(name)+'</b>'
      + (reason ? ' · 理由：'+_esc(reason) : '')
      + '<span style="font-weight:normal;font-size:12px;color:var(--mut);">（'+_esc(d.selection.by)+'）</span></div>';
  } catch(e) {
    btn.disabled = false; btn.textContent = '✔ 选定';
    alert('选型失败：' + (e.message || e));
  }
}
