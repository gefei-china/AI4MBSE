/* 渲染：Markdown / 表格 / 图表
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 2300-2525  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
function escMd(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function inlineMd(s, cites){
  let o = escMd(s);
  o = o.replace(/`([^`]+)`/g, '<code class="md-ic">$1</code>');
  o = o.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // 问答可解释性：将 [n] / [n,m] 引用标注渲染为可点击来源链接（需 message 附带 citations 数据）
  if(cites && cites.length){
    o = o.replace(/\[(\d+(?:[,，]\s*\d+)*)\]/g, (mm, idxs)=>{
      const nums = idxs.split(/[,，]\s*/).map(n=>parseInt(n,10)).filter(n=>n>=1 && n<=cites.length);
      if(!nums.length) return mm;
      return nums.map(n=>`<sup class="cite" data-i="${n}" onclick="openCitation(this)" title="查看引用来源 [${n}]">[${n}]</sup>`).join('');
    });
  }
  o = o.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  o = o.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<i>$2</i>');
  return o;
}
function renderMdLine(line, cites){
  const t = line.trim();
  if(!t) return '';
  const hm = t.match(/^(#{1,6})\s+(.*)$/);
  if(hm){ const lv = Math.min(hm[1].length,4); return `<h${lv} class="md-h">${inlineMd(hm[2], cites)}</h${lv}>`; }
  if(t.startsWith('>')){
    const qt = t.replace(/^>\s?/,'');
    // 提示块（GitHub Alerts 风格）：[!信息]/[!警告]/[!注意]/[!错误]/[!成功]
    const am = qt.match(/^\[!(INFO|NOTE|信息|注意|WARNING|警告|错误|DANGER|成功|SUCCESS)\]\s*(.*)$/i);
    if(am){
      const k = am[1].toUpperCase();
      const clsMap = {INFO:'qi',NOTE:'qi',信息:'qi',警告:'qw',注意:'qw',WARNING:'qw',错误:'qe',DANGER:'qe',成功:'qo',SUCCESS:'qo'};
      const icoMap = {INFO:'ℹ️',NOTE:'📝',信息:'ℹ️',注意:'📌',警告:'⚠️',WARNING:'⚠️',错误:'🚫',DANGER:'🚫',成功:'✅',SUCCESS:'✅'};
      return `<blockquote class="md-q ${clsMap[k]||''}"><b>${icoMap[k]||'ℹ️'} ${esc(am[1])}：</b>${inlineMd(am[2], cites)}</blockquote>`;
    }
    return `<blockquote class="md-q">${inlineMd(qt, cites)}</blockquote>`;
  }
  if(/^(-{3,}|\*{3,})$/.test(t)) return '<hr class="md-hr">';
  if(/^[-*•]\s+/.test(t)) return `<div class="md-li">• ${inlineMd(t.replace(/^[-*•]\s+/,''), cites)}</div>`;
  if(/^\d+[.、)]\s+/.test(t)) return `<div class="md-li">${t.match(/^\d+/)[0]}. ${inlineMd(t.replace(/^\d+[.、)]\s+/,''), cites)}</div>`;
  return `<p class="md-p">${inlineMd(t, cites)}</p>`;
}
function _mdCells(line){ return line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim()); }
// 2026-09-29（用户反馈）：移除表格下方「📊 柱状图」切换按钮——数字表格自动配图表入口
// 属于过度设计（多数工程表格的"数字列"是编号/严重程度，画柱状图毫无意义），预览页共用同一
// 渲染函数一并生效。svgBarChart / toggleMdChart / _findNumericColumn 已随之删除（无其他调用者）。
function renderTable(rows){
  const head = rows[0]||[];
  const data = rows.slice(2).filter(r=>r.some(c=>c!==''));
  let t = '<div class="md-table-wrap"><table class="md-table"><thead><tr>'+head.map(c=>`<th>${escMd(c)}</th>`).join('')+'</tr></thead><tbody>';
  t += data.map(r=>'<tr>'+r.map(c=>`<td>${escMd(c)}</td>`).join('')+'</tr>').join('');
  t += '</tbody></table></div>';
  return t;
}
function renderMarkdown(text, cites){
  if(!text) return '';
  // V2.6 代码块折叠卡：默认收起（头部=语言+行数+复制），点击展开，避免长代码霸屏
  function _codeFold(lines){
    const lang = (lines[0]||'').match(/^```(\w+)/);
    const body = lines.slice(1, lines.length && /^```\s*$/.test(lines[lines.length-1]) ? -1 : undefined);
    const n = body.length;
    const langName = (lang&&lang[1])||'code';
    // 2026-09-16：mermaid 代码块 → 视图渲染（懒加载 mermaid 库）+ 源码切换，不再是纯文本
    if(langName === 'mermaid' && body.length <= 120){
      const mid = 'mmd-view-' + Math.floor(Math.random()*1e6);
      setTimeout(mermaidAutoRender, 400); setTimeout(mermaidAutoRender, 1500);
      return `<div class="code-fold open">`
        + `<div class="cf-head"><span class="cf-lang">mermaid 图</span>`
        + `<span class="cf-meta">${n} 行</span>`
        + `<span class="cf-act" onclick="event.stopPropagation();mermaidToggle(this)">🔀 视图/源码</span>`
        + `<span class="cf-act" onclick="event.stopPropagation();codeFoldCopy(this)">📋 复制</span></div>`
        + `<div class="mermaid-view" id="${mid}"><div style="padding:10px;font-size:11px;color:var(--mut);">Mermaid 视图渲染中…</div></div>`
        + `<pre class="md-code" style="display:none;"><code>${escMd(body.join('\n'))}</code></pre></div>`;
    }
    return `<div class="code-fold">`
      + `<div class="cf-head" onclick="codeFoldToggle(this)"><span class="cf-lang">${escMd(langName)}</span>`
      + `<span class="cf-meta">${n} 行</span>`
      + `<span class="cf-act" onclick="event.stopPropagation();codeFoldCopy(this)">📋 复制</span>`
      + `<span class="chev">▸</span></div>`
      + `<pre class="md-code"><code>${escMd(body.join('\n'))}</code></pre></div>`;
  }
  const lines = String(text).replace(/\r\n/g,'\n').split('\n');
  let html='', i=0, inCode=false, codeBuf=[];
  while(i<lines.length){
    const line = lines[i];
    if(/^```/.test(line.trim())){
      if(!inCode){ inCode=true; codeBuf=[line]; }
      else { codeBuf.push(line); inCode=false; html += _codeFold(codeBuf); }
      i++; continue;
    }
    if(inCode){ codeBuf.push(line); i++; continue; }
    if(/^\s*\|/.test(line) && i+1<lines.length && /^\s*\|[\s:|-]+\|?\s*$/.test(lines[i+1]) && lines[i+1].includes('-')){
      const rows=[];
      while(i<lines.length && /^\s*\|/.test(lines[i])){ rows.push(_mdCells(lines[i])); i++; }
      if(rows.length>=2){ html += renderTable(rows); continue; }
    }
    html += renderMdLine(line, cites);
    i++;
  }
  if(inCode) html += _codeFold(codeBuf);
  return html;
}
/* ── 2026-09-16 Mermaid 视图渲染：```mermaid 代码块 → 平台内渲染成图（懒加载 mermaid 库）── */
let _mermaidLoading = false;
function ensureMermaid(cb){
  if(window.mermaid) return cb();
  if(_mermaidLoading){
    const t = setInterval(()=>{ if(window.mermaid){ clearInterval(t); cb(); } }, 200);
    setTimeout(()=>clearInterval(t), 15000);
    return;
  }
  _mermaidLoading = true;
  const s = document.createElement('script');
  s.src = '/static/lib/mermaid.min.js';
  s.onload = ()=>{ try{ window.mermaid.initialize({startOnLoad:false, securityLevel:'loose', theme:'neutral'}); }catch(e){} cb(); };
  s.onerror = ()=>{ _mermaidLoading = false; toast('Mermaid 库加载失败（/static/lib/mermaid.min.js）'); };
  document.head.appendChild(s);
}
function mermaidRenderInto(container, code){
  ensureMermaid(()=>{
    try{
      const id = 'mmd-' + Date.now() + '-' + Math.floor(Math.random()*1e4);
      const out = window.mermaid.render(id, code);
      if(out && typeof out.then === 'function'){          // mermaid v10：Promise<{svg}>
        out.then(r=>{ container.innerHTML = (r && r.svg) || r; })
           .catch(e=>{ container.innerHTML = '<div style="padding:8px;font-size:11px;color:var(--red);">Mermaid 渲染失败：'+escMd(e && e.message || e)+'</div>'; });
      } else {                                            // mermaid v8/9：同步返回 html/svg
        container.innerHTML = (out && out.svg) || out || '';
      }
    }catch(e){
      container.innerHTML = '<div style="padding:8px;font-size:11px;color:var(--red);">Mermaid 渲染失败：'+escMd(e && e.message || e)+'</div>';
    }
  });
}
function mermaidAutoRender(root){
  (root||document).querySelectorAll('.mermaid-view').forEach(v=>{
    if(v.dataset.rendered) return;
    const fold = v.closest('.code-fold');
    const pre = fold && fold.querySelector('pre.md-code code');
    const code = pre && pre.textContent.trim();
    if(!code) return;
    v.dataset.rendered = '1';
    mermaidRenderInto(v, code);
  });
}
function mermaidToggle(actEl){
  const fold = actEl.closest('.code-fold');
  if(!fold) return;
  const showingSrc = fold.classList.toggle('show-src');
  const view = fold.querySelector('.mermaid-view');
  const pre = fold.querySelector('pre.md-code');
  if(view) view.style.display = showingSrc ? 'none' : '';
  if(pre) pre.style.display = showingSrc ? '' : 'none';
  if(!showingSrc){ delete fold.querySelector('.mermaid-view').dataset.rendered; mermaidAutoRender(fold); }
}
// V2.6 代码折叠卡：展开/收起（非 mermaid 块沿用）
function codeFoldToggle(headEl){
  const fold = headEl.closest('.code-fold');
  if(!fold) return;
  const open = fold.classList.toggle('open');
  headEl.querySelector('.chev').textContent = open ? '▾' : '▸';
}
// V2.6 引用内容折叠区切换（条目点击走 openCitation 看 chunk 详情）
function citeSrcToggle(headEl){
  const body = headEl.parentElement ? headEl.parentElement.querySelector('.cite-src-body') : null;
  if(!body) return;
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  const ch = headEl.querySelector('.chev');
  if(ch){ ch.style.transform = open ? '' : 'rotate(90deg)'; }
}
function codeFoldCopy(actEl){
  const code = actEl.closest('.code-fold')?.querySelector('.md-code code');
  if(!code) return;
  navigator.clipboard.writeText(code.textContent||'').then(()=>toast('代码已复制')).catch(()=>toast('复制失败'));
}

function renderMessage(m) {
  const cls = m.role==='user' ? 'u' : 'ai';
  const who = m.role==='user' ? '王' : 'AI';
  let cd = null;
  if(m.card_data) {
    try { cd = typeof m.card_data==='string' ? JSON.parse(m.card_data) : m.card_data; } catch(e){ cd = null; }
  }
  // AI 消息 → markdown 渲染（标题/表格/代码块/列表/图表）；user 消息保持原样（调用方已转义）
  // 问答可解释性：card_data.citations → 答案内 [n] 引用链接（按消息 id 缓存供 openCitation 跳转）
  let cites = (cd && Array.isArray(cd.citations)) ? cd.citations : [];
  if(m.role==='assistant' && m.id && cites.length) {
    (window._msgCites = window._msgCites || {})[m.id] = cites;
  }
  // 建模输出（含 sysml_views）：正文剥离代码块（代码收敛到下方「代码/视图」整合卡 tab 内，聚焦同步查看）
  const hasViews = !!(cd && cd.sysml_views && cd.sysml_views.views && Object.keys(cd.sysml_views.views).length);
  const codeFound = (hasViews && m.role==='assistant') ? extractSysmlCode(m.content||'') : null;
  const bodySrc = codeFound ? stripFencedCode(m.content||'') : (m.content||'');
  const contentHtml = m.role==='assistant' ? renderMarkdown(bodySrc, cites) : bodySrc;
  let html = `<div class="msg ${cls}"${m.id?` id="msg-${m.id}" data-mid="${m.id}"`:''}><span class="who">${who}</span><div class="msg-inner">`;
  // V2.4 会话内执行过程：思考 → 子智能体 → 工具调用（流式当时传入 process_html；历史消息从 card_data.exec 还原）
  // 2026-09-25：同时把 card_data 按消息 id 缓存 —— 「🔍 执行详情」面板据此读**落库的原始执行数据**
  //  （intent/agent/skill_hits/source/confidence/provider/used_mock/tools/reasoning），
  //   而不是只从 DOM 过程块里刮（DOM 只有 exec 的子集，会漏掉技能/意图/模型等字段）。
  if(m.id && cd) { (window._msgCardCache = window._msgCardCache || {})[m.id] = cd; }
  if(m.process_html) html += `<div class="proc">${m.process_html}</div>`;
  else if(cd && cd.exec) html += procBlocksHtml(cd.exec, m.id);
  html += `<div class="body" style="min-width:0;">${contentHtml}`;
  // V2.3 富输入：user 消息附件渲染（图片内联 / 文件链接）
  if(m.attachments) {
    let atts = [];
    try { atts = typeof m.attachments==='string' ? JSON.parse(m.attachments) : m.attachments; } catch(e){ atts = []; }
    if(atts.length) {
      // 2026-09-18：附件统一可点即预览源文件。
      // 此前：图片只是 target=_blank 打开原图、非图片仅展示文件名（完全无预览入口）。
      // 现统一走 openFilePreview：有 doc_id 走原件直出 /raw，纯图片/未入库附件走其落盘 url。
      html += '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;">' + atts.map(a=>{
        const _at = pvDataAttrs({doc_id:a.doc_id, filename:a.filename||'', url:a.url||''});
        const _tip = '点击预览源文件：' + esc(a.filename||'附件');
        if(a.is_image) return `<img src="${esc(a.url)}" alt="${esc(a.filename||'image')}"${_at} onclick="pvOpenFromEl(this)" title="${_tip}" style="max-width:160px;max-height:120px;border-radius:8px;border:1px solid var(--line);cursor:zoom-in;">`;
        return `<span class="attach-chip"${_at} onclick="pvOpenFromEl(this)" title="${_tip}" style="cursor:pointer;"><span class="an">📄 ${esc(a.filename||'附件')}</span></span>`;
      }).join('') + '</div>';
    }
  }
  if(cd) {
      // V2.6 参考来源折叠区：citations 存在即展示（不依赖正文 [n] 标注），点击条目打开 chunk 详情
      if(m.role==='assistant' && cites.length){
        const items = cites.map((c,i)=>`<div class="cite-row" data-i="${i+1}" onclick="openCitation(this)" style="cursor:pointer;padding:5px 8px;border-top:1px solid var(--line,#e5e8ee);font-size:11px;"
          onmouseover="this.style.background='#f4f8ff'" onmouseout="this.style.background=''">
          <b style="color:var(--blue-d,#3478f6);">[${i+1}]</b> ${esc(c.source_doc||'')}${c.section?` · ${esc(c.section)}`:''}
          <span style="color:var(--mut);font-size:10px;">· 相关度 ${c.score}${c.confidence_level?`（${esc(c.confidence_level)}）`:''}${c.reranked?' · LLM重排':''}</span>
          <div style="color:var(--mut);font-size:10.5px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc((c.content||'').slice(0,120))}</div>
        </div>`).join('');
        html += `<div style="margin-top:8px;border:1px solid var(--line,#e5e8ee);border-radius:8px;overflow:hidden;">
          <div style="display:flex;align-items:center;gap:6px;padding:5px 10px;background:#fafbfc;font-size:11px;color:var(--mut);cursor:pointer;user-select:none;" onclick="citeSrcToggle(this)">
          <span class="chev" style="font-size:9px;">▸</span>📚 引用内容（${cites.length} 条 chunk，点击条目查看详情）</div>
          <div class="cite-src-body" style="display:none;max-height:280px;overflow:auto;background:#fff;">${items}</div></div>`;
      }
      if(cd.intent) {
        const hl = (cd.hil_level||'').toLowerCase();
        const hilBadge = cd.hil_level ? `<span class="hil ${hl.startsWith('l2')?'l2':hl.startsWith('l1')?'l1':'l0'}">HIL ${cd.hil_level}</span>` : '';
        const kb = (cd.kb_tags||[]).length ? ` ｜ 知识库：${cd.kb_tags.map(t=>'#'+t).join(' ')}` : '';
        html += `<div style="margin-top:8px;font-size:11px;color:var(--mut);">${cd.agent?`<b>${cd.agent}</b> `:''}${hilBadge} 意图：${cd.intent} ｜ 来源：${cd.source}（图谱${cd.graph_count||0} / 向量${cd.vector_count||0}）置信度 ${(cd.confidence||0).toFixed(2)}${kb}</div>`;
      }
      // P0：结论速览面板（资料库与AI建模优化 §2）—— 默认展开，集中阅读最终结论
      const summaryHtml = renderSummaryPanel(cd);
      if(summaryHtml) html += summaryHtml;
      // 富卡片：按 msg_type 渲染结构化数据（传入消息正文，供代码/视图整合卡提取 V2 代码）
      const cardHtml = renderRichCard(m.msg_type || cd.intent, cd, m.content);
      if(cardHtml) html += `<div class="rc">${cardHtml}</div>`;
      // P0-1/P0-2：编排卡（子任务轨迹摘要 + 沉淀流程入口）
      if(cd.orchestrated && (cd.plan||[]).length){
        html += renderOrchCard(cd);
      }
  }
  // P0-3 会话产物（规范06 产物即界面）：产物卡片网格（图标+标题+类型+操作），≥3 横滑
  if(m.role==='assistant' && cd) {
    html += artGridHtml(m, cd);
  }
  // 内容级澄清消息（msg_type='clarify'）：历史还原
  //  2026-09-25：**仍待澄清时还原为可作答的卡**（复用 clarifyCardInnerHtml），此前一律渲染成
  //  只读文字（"已作答后继续"）→ 刷新/重开会话后用户找不到任何作答入口（实测反馈）。
  //  已作答/已跳过（window._pendingClarify 为空）则保持只读摘要，避免重复提交。
  if(m.role==='assistant' && m.msg_type==='clarify'){
    const cqs = (cd && cd.questions) || [];
    if(window._pendingClarify && cqs.length && typeof clarifyCardInnerHtml === 'function'){
      html += `<div class="clarify-ask-card hist" style="margin-top:8px;border:1px solid #d3e3fb;
        background:#f0f6ff;border-radius:8px;padding:10px 12px;font-size:12px;">${clarifyCardInnerHtml(cqs)}</div>`;
    } else {
      html += `<div style="margin-top:8px;border:1px solid #d3e3fb;background:#f0f6ff;border-radius:8px;padding:8px 10px;font-size:11.5px;">
      <b style="color:var(--blue-d);">❓ 需要确认建模信息</b>
      ${cqs.map((q,i)=>`<div style="margin-top:4px;">${i+1}. ${esc(q.question||'')} <span style="color:var(--mut);font-size:10px;">（已作答后继续）</span></div>`).join('') || ''}
    </div>`;
    }
  }
  if(m.feedback) {
    const fbMap = {approve:'已采纳', reject:'已拒绝', modify:'已修订'};
    const fbCls = m.feedback==='approve'?'ok':m.feedback==='reject'?'r':'w';
    html += `<div style="margin-top:6px;"><span class="st ${fbCls} hil-fb">${fbMap[m.feedback]||m.feedback}</span></div>`;
  }
  // V3.0 去消息级采纳/拒绝按钮：确认动作收敛到关键环节（确认写入/审核通过）。
  // 2026-09-17 移除「⚠ 需要确认 · SysML 产物入库」审批卡（用户确认移除）：
  //   该卡早已与架构脱节——14-sysml.js 顶部即注明「旧单版本入库入口已移除，AI 建模代码不再直接入库，
  //   归档收敛为工程级一次性操作」，卡内「批准」实际只写 localStorage + 提示去版本历史点「📦 工程入库」，
  //   属于滞留的伪闸门（点了没有真实入库动作），留在消息流里造成「必须确认才能入库」的误导。
  //   入库真实入口保留在：图谱工作区 · 版本历史 →「📦 工程入库」。
  // 无 SysML 产物 → 直出，不渲染任何操作按钮（需修订直接在输入框提出，AI 生成新版本）。
  html += '</div>';  // 关闭 body
  // 用户消息：时间 + 复制按钮（body 外部，hover 时显示；按钮默认仅 icon，hover 展开文字）
  if(m.role==='user'){
    const timeStr = formatMsgTime(m.created_at);
    html += `<div class="msg-footer"><span class="msg-time">${timeStr}</span><button class="msg-copy-btn" onclick="copyMsg(this)" title="复制"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" style="flex:none;"><rect x="6" y="6" width="8" height="8" rx="1.5"></rect><path d="M4 10 H3.5 A1.5 1.5 0 0 1 2 8.5 v-5 A1.5 1.5 0 0 1 3.5 2 h5 A1.5 1.5 0 0 1 10 3.5 V4"></path></svg><span class="cp-txt">复制</span></button></div>`;
  }
  html += '</div></div>';  // 关闭 msg-inner 和 msg
  return html;
}

// ── 消息时间格式化：当日的 HH:MM，之前的 MM-DD HH:MM ──
function formatMsgTime(ts){
  if(!ts) return '';
  const d = new Date(ts);
  const now = new Date();
  const isToday = d.getFullYear()===now.getFullYear() && d.getMonth()===now.getMonth() && d.getDate()===now.getDate();
  const hh = String(d.getHours()).padStart(2,'0');
  const mm = String(d.getMinutes()).padStart(2,'0');
  const MM = String(d.getMonth()+1).padStart(2,'0');
  const DD = String(d.getDate()).padStart(2,'0');
  return isToday ? `${hh}:${mm}` : `${MM}-${DD} ${hh}:${mm}`;
}
// ── 复制消息内容（按钮默认仅 icon，hover 展开文字；成功反馈显示 ✓ 已复制）──
function copyMsg(btn){
  const body = btn.closest('.body');
  if(!body) return;
  const clone = body.cloneNode(true);
  const footer = clone.querySelector('.msg-footer');
  if(footer) footer.remove();
  const text = clone.textContent.trim();
  navigator.clipboard.writeText(text).then(()=>{
    btn.innerHTML = '<span class="cp-txt" style="display:inline;">✓ 已复制</span>';
    btn.classList.add('copied');
    setTimeout(()=>{
      btn.classList.remove('copied');
      btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" style="flex:none;"><rect x="6" y="6" width="8" height="8" rx="1.5"></rect><path d="M4 10 H3.5 A1.5 1.5 0 0 1 2 8.5 v-5 A1.5 1.5 0 0 1 3.5 2 h5 A1.5 1.5 0 0 1 10 3.5 V4"></path></svg><span class="cp-txt">复制</span>';
    }, 1500);
  }).catch(()=>{ toast('复制失败'); });
}

// ── V3 会话内执行过程：持久化数据（card_data.exec）→ 时间线顺序 HTML ──
