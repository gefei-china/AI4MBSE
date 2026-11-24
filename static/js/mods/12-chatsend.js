// ── V3 Trae 式 mention：# 引用知识库文件 / / 选择技能（行首触发）
// 2026-09-28（用户反馈）：**@ 选择会话文件已移除** —— 会话文件引用与「＋ 菜单 → 当前会话的文件」
// 功能重叠，且 @ 易与普通文本混触发；知识库引用同步移入「＋ 菜单 → 引用知识库文件」（打字 # 保留作快捷方式）。
let _mention = { open:false, type:'', start:0, filter:'', items:[], sel:0 };
let _kbTagsCache = null, _skillsCache = null, _teamsCache = null, _skillMentioned = false;
let _toolsCache = [];
// 2026-09-28：_agentsCache 声明保留（原供 @ 智能体 mention，现仅服务 11-pipeline/13-reports/
// 29-flow/30-agents 的智能体面板共享缓存）——跨文件词法共享变量，声明删掉会 ReferenceError。
let _agentsCache = null;

async function ensureKbTagsCache(){
  if(_kbTagsCache) return _kbTagsCache;
  try { _kbTagsCache = await api('/api/knowledge/tags'); } catch(e){ _kbTagsCache = []; }
  return _kbTagsCache;
}
async function ensureSkillsCache(){
  if(_skillsCache) return _skillsCache;
  try { _skillsCache = await api('/api/studio/skills'); } catch(e){ _skillsCache = []; }
  return _skillsCache;
}
// 检测光标前的 mention token（#xxx / 行首 /xxx，未闭合空格；@ 已于 2026-09-28 移除）
function _detectMention(){
  const ta = document.getElementById('chat-input');
  if(!ta) return null;
  const pos = ta.selectionStart || 0;
  const before = ta.value.slice(0, pos);
  const m = before.match(/([#\/])([^\s@#\/]*)$/);
  if(!m) return null;
  const idx = before.length - m[0].length;
  if(m[1] === '/'){
    // 技能为 slash command 语义：仅行首触发（避免"和/或"等正常文本误弹）
    if(idx !== 0) return null;
  } else if(idx > 0 && !/\s/.test(before[idx-1])) return null;  // 邮箱等场景不弹
  return { type: m[1], filter: m[2], start: idx };
}
function _chatInputAutoGrow(){
  // 2026-09-29（自适应高度）：随内容变高，上限由 CSS max-height:200px 控制
  // （到顶后 overflow-y:auto 内部滚动）。支持 field-sizing:content 的浏览器
  // （Chrome 123+）由 CSS 接管，这里 scrollHeight 与实际一致时是幂等的，无冲突。
  const ta = document.getElementById('chat-input');
  if(!ta) return;
  ta.style.height = 'auto';                 // 先回缩，让 scrollHeight 反映真实内容高度
  const h = Math.min(ta.scrollHeight, 200);
  ta.style.height = h + 'px';
  ta.style.overflowY = ta.scrollHeight > 200 ? 'auto' : 'hidden';
}
async function handleChatInput(){
  _chatInputAutoGrow();   // 2026-09-29：自适应高度随输入即时生效
  // 2026-09-04 v3：草稿态输入实时暂存，下次「新建任务」恢复
  if(!currentConvId){
    const _inp = document.getElementById('chat-input');
    try{ if(_inp) localStorage.setItem('mbse_draft_input', _inp.value); }catch(e){}
  }
  const hit = _detectMention();
  if(!hit){ closeMentionPop(); return; }
  _mention.type = hit.type; _mention.start = hit.start; _mention.filter = hit.filter; _mention.sel = 0;
  let items = [];
  if(hit.type === '/'){
    const skills = await ensureSkillsCache();
    const f = hit.filter.toLowerCase();
    items = skills.filter(s => !f || (s.name||'').toLowerCase().includes(f) || (s.description||'').toLowerCase().includes(f))
      .map(s => ({ key:s.name, icon:'🧩', name:s.display_name||s.name, desc:s.description||'', tag:s.status==='published'?'技能':'技能·draft' }));
  } else {
    const tags = await ensureKbTagsCache();
    const f = hit.filter.toLowerCase();
    items = [{ key:'工程数据', icon:'📊', name:'工程数据', desc:'引用全部工程数据（控制标签）', tag:'快捷' },
             { key:'知识库', icon:'🧬', name:'知识库', desc:'引用全部已发布知识库（控制标签）', tag:'快捷' }]
      /* ★ 2026-10-20：透传 doc_id/status——pickMention 的 attachKbDocToBar 靠它挂附件栏 */
      .concat(tags.map(t => ({ key:t.name, icon:'📄', name:t.name, desc:(t.status||''), tag:'文档', doc_id:t.doc_id||0, status:t.status||'' })))
      .filter(t => !f || t.name.toLowerCase().includes(f));
  }
  _mention.items = items.slice(0, 12);
  _mention.open = true;
  renderMentionPop();
}
function renderMentionPop(){
  const pop = document.getElementById('mention-pop');
  if(!pop) return;
  if(!_mention.open){ pop.style.display = 'none'; return; }
  const head = _mention.type === '/' ? '选择技能（↑↓ 移动 · Enter 确认 · Esc 关闭）'
             : '引用知识库文件（↑↓ 移动 · Enter 确认 · Esc 关闭）';
  pop.innerHTML = `<div class="mp-head">${head}</div><div class="mp-list">` +
    (_mention.items.length ? _mention.items.map((it, i) => `
      <div class="mention-item ${i===_mention.sel?'sel':''}" onmousedown="event.preventDefault();pickMention(${i})" onmousemove="_mention.sel=${i};renderMentionPop()">
        <span class="mi-ic">${it.icon}</span><span class="mi-name">${esc(it.name)}</span>
        <span class="mi-desc">${esc(it.desc)}</span><span class="mi-tag">${esc(it.tag)}</span>
      </div>`).join('') : `<div class="mention-empty">无匹配项</div>`) + `</div>`;
  pop.style.display = 'block';
}
function closeMentionPop(){ _mention.open = false; const pop = document.getElementById('mention-pop'); if(pop) pop.style.display = 'none'; }
async function pickMention(i){
  const it = _mention.items[i];
  if(!it) return;
  const ta = document.getElementById('chat-input');
  const pos = ta.selectionStart || 0;
  // 移除已输入的 #/filter 片段
  ta.value = ta.value.slice(0, _mention.start) + ta.value.slice(pos);
  if(_mention.type === '/'){
    // 技能：不进文本，联动 quick-skill + chip（下拉懒加载时先补 option）
    const sel = document.getElementById('quick-skill');
    if(sel){
      if(![...sel.options].some(o=>o.value===it.key)) sel.add(new Option(it.name, it.key));
      sel.value = it.key;
    }
    _skillMentioned = true;
    renderChips();
    ta.focus();
  } else {
    // 知识库：插入 #文件名 文本标签
    const tag = '#' + it.key + ' ';
    ta.value = ta.value.slice(0, _mention.start) + tag + ta.value.slice(_mention.start);
    const p = _mention.start + tag.length;
    ta.focus(); ta.setSelectionRange(p, p);
    // ★ 2026-10-20：引用同时挂附件（doc_id ⇒ 后端注入文档全文）。#文本保留触发检索。
    if(typeof attachKbDocToBar === 'function' && attachKbDocToBar(it)){
      toast('已引用：' + it.name + '（已挂附件栏，模型将直接读取）');
    }
  }
  closeMentionPop();
}
function handleChatKey(e){
  if(_mention.open){
    if(e.key === 'ArrowDown'){ e.preventDefault(); _mention.sel = Math.min(_mention.sel+1, _mention.items.length-1); renderMentionPop(); return; }
    if(e.key === 'ArrowUp'){ e.preventDefault(); _mention.sel = Math.max(_mention.sel-1, 0); renderMentionPop(); return; }
    if(e.key === 'Enter'){ e.preventDefault(); pickMention(_mention.sel); return; }
    if(e.key === 'Escape'){ e.preventDefault(); closeMentionPop(); return; }
  }
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendChat(); }
}
// ★ 2026-10-16（米爸决策 R1）：发起会话的前置闸 —— 必须选定主 Agent。
//   返回 true=放行；false=已提示用户，调用方应直接 return（**不要**继续建会话/发请求）。
//   为什么单独成函数：sendChat / sendResume（澄清续答）/ 未来任何新发送入口都走同一判据，
//   避免"改了主路径漏了续答路径"（那正是本次事故的形态：主路径对、续答路径丢 team）。
function requireMainAgent(){
  const sel = document.getElementById('quick-team');
  const v = (sel && sel.value) ? String(sel.value).trim() : '';
  if(v){ window._convMainAgent = v; return true; }
  // 未选：明确告知 + 引导，不做静默放行
  toast('请先选择主 Agent 才能发起会话（输入框左侧「👑」下拉）');
  const st = document.getElementById('chat-status');
  if(st) st.textContent = '请先选择主 Agent';
  if(sel){
    sel.focus();
    // 轻量视觉提示：描边一闪，不改变布局（不弹窗、不阻断）
    sel.style.transition = 'box-shadow .18s';
    sel.style.boxShadow = '0 0 0 2px rgba(245,90,90,.55)';
    setTimeout(()=>{ try{ sel.style.boxShadow=''; }catch(e){} }, 1200);
  }
  return false;
}
// mention chip 条：与 quick-skill / quick-team 下拉双向同步（V2.7：智能体选中不再渲染 chip——
// 选定智能体只作定向偏好，意图识别照常执行）
function renderChips(){
  const bar = document.getElementById('agent-chip-bar');
  if(!bar) return;
  let h = '';
  const sV = document.getElementById('quick-skill')?.value || '';
  if(sV){
    const s = (_skillsCache || []).find(x => x.name === sV);
    h += `<span class="agent-chip" style="background:#F0FFF4;border-color:#9AE6B4;color:#276749;">🧩 ${esc(s ? (s.display_name||s.name) : sV)}<span class="x" title="取消指定技能" onclick="clearSkillChip()">✕</span></span>`;
  }
  // 2026-09-23（用户要求）：不再在输入框上方渲染团队名称 chip。
  // 团队选中状态唯一载体 = 「👑 工作流（智能体团队）」下拉；clearTeamChip() 保留供程序化清空。
  bar.innerHTML = h;
  bar.style.display = h ? 'flex' : 'none';
}
function clearSkillChip(){
  const sel = document.getElementById('quick-skill');
  if(sel) sel.value = '';
  _skillMentioned = false;
  renderChips();
}
function clearTeamChip(){
  const sel = document.getElementById('quick-team');
  if(sel) sel.value = '';
  renderChips();
}

// P0-1 澄清改选重发：全局流中止控制器（新发送前中止旧流）
let _streamAbort = null;
let _streaming = false;        // P1-1 流式进行中标记（控制发送/停止按钮切换）

// ★ 2026-11-19 P0-A：创建会话**临界区**同步锁。
//   为什么不是整个 sendChat 的全局锁：系统支持多会话并发（见下方 :300 的 _sameConv 判据），
//   `if(_streaming) return` 会把「切到别的会话发消息」也拦掉，属于误伤。
//   竞态只发生在 `if(!currentConvId)` → await POST → 赋值 这段 TOCTOU 窗口，锁这一段即可。
let _creatingConv = false;
function _newClientRequestId(){
  try{ if(window.crypto && crypto.randomUUID) return crypto.randomUUID(); }catch(e){}
  return 'c-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,10);
}
let _stoppedManually = false;  // 手动停止 vs 澄清改选自动中止
// ★ 2026-10-16 Q2：澄清改选 / 澄清续答**复用原输出盒**时，会先 abort 当前流再用同一盒重跑。
//   旧流的 catch 本会把 _streaming 置 false、并触发 _finalizeStoppedRender 把盒子 outerHTML 替换掉
//   ⇒ 新流刚启动就被旧流 cleanup 抢走状态/盒子。用此标记让"被接管的中止流"提前 return、
//   既不动全局状态也不替换盒子（新的接管流已掌管）。仅在新流接管前为 true，旧流 catch 内清回 false。
let _streamSuperseded = false;
// 从首条消息生成会话标题（草稿首次提交时使用）
function draftTitle(msg){
  const clean = String(msg||'').replace(/[#*`>\[\](){}«»"':]/g,' ').replace(/\s+/g,' ').trim();
  if(clean) return clean.length>20 ? clean.slice(0,18)+'…' : clean;
  return '任务 '+(Date.now()%1000);
}
// ★ 2026-11-19 P2-2：从 sendChat 提取的两个独立职责块（行为逐字节不变）。
//   返回 true=继续走发送主流程；false=已处理完毕，调用方应 return。
//   为什么用返回值而不是在子函数里 return：原代码这些分支直接 return 终止 sendChat，
//   提取后必须由调用方决定是否终止 —— 这是保证行为不变的关键。
async function _ensureConversation(msg){
  // 草稿态首次提交时创建会话（未提交前左侧列表不出现空会话）
  // 若由「项目 → ＋ 新建任务」发起，带走该项目归属（window._pendingProjectId）
  if(currentConvId) return true;
  // ★ P0-A：同步置锁（在任何 await 之前）——挡住双击/Enter/K-bar 并发进入的第二次创建。
  //   P0-C：立即给状态反馈，不等 pumpStream（原实现要等近 200 行之后才有反馈）。
  if(_creatingConv){ toast('正在创建会话，请稍候…'); return false; }
  _creatingConv = true;
  const _st0 = document.getElementById('chat-status');
  if(_st0) _st0.textContent = '正在创建会话…';
  let _rid = '';
  try{
    const _body = {title:draftTitle(msg)};
    const _pid = window._pendingProjectId || '';
    if(_pid) _body.project_id = _pid;
    // ★ P0-B：一次提交一个 request_id，后端据此做幂等（重放返回同一 conv_id，不再 INSERT）。
    _rid = _newClientRequestId();
    _body.client_request_id = _rid;
    const r = await api('/api/conversations', {method:'POST', body:JSON.stringify(_body)});
    currentConvId = r.id;
    if(r && r.idempotent_hit) toast('该请求已提交过，已复用原会话');
    _isDraft = false;
    loadConversations();
    if(_pid){
      window._pendingProjectId = '';
      if(typeof renderGnavProjects === 'function') renderGnavProjects();
    }
    const st = document.getElementById('chat-status');
    if(st) st.textContent = '';
    try{ localStorage.removeItem('mbse_draft_input'); }catch(e){}
  }catch(e){ toast('创建会话失败：'+(e && (e.message||e)) ); return false; }
  finally{ _creatingConv = false; }   // 所有分支（含 return）都释放，绝不留死锁
  return true;
}

async function _submitClarify(msg, input){
  // 待澄清状态下，主输入框内容即"澄清补充"——走 clarify-answer（会清空挂起）。
  // ★ G-1：带附件时绝不能走本分支（clarify-answer 不收附件，会静默丢弃）；
  //   带附件是新的建模请求 ⇒ 调用方直接走正常发送。
  if(!(window._pendingClarify && currentConvId && !(pendingAttachments && pendingAttachments.length)))
    return true;
  try{
    const r = await api(`/api/conversations/${currentConvId}/clarify-answer`,
                        {method:'POST', body: JSON.stringify({free_text: msg})});
    if(r && r.resume_text){
      input.value = '';
      _chatInputAutoGrow();
      try{ localStorage.removeItem('mbse_draft_input'); }catch(e){}
      toast('已作为澄清补充提交，正在继续…');
      setPendingClarify(null);
      _continueInSameBox(null, r.resume_text, {
        user_msg_id: r.resume_of_user_msg_id || 0,
        append: r.resume_append || '',
      }, msg.slice(0, 40));
      return false;                  // ★ 与原 return 等价：终止 sendChat
    }
    // 后端返回"无待澄清"⇒ 本地挂起态已不同步，清掉并落入正常发送（不吞输入）
    setPendingClarify(null);
    toast((r && r.error) || '澄清状态已过期，按新消息发送');
  }catch(e){
    setPendingClarify(null);
    toast('澄清提交失败，已按新消息发送');
  }
  return true;
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  // ★★ 2026-10-30（用户报「从产物加入会话后发送异常，附件消失、输入框还在、会话没发起」）：
  //   `msg` 原为 **const**，而下面「Cursor 式引用」分支要给它前置引用文本：
  //       if(_refs.length){ msg = _refs.map(...) + '\n\n' + msg; }
  //   ⇒ `TypeError: Assignment to constant variable`（严格模式/现代浏览器必抛）。
  //   抛点位置决定了症状**正好**是用户描述的那样：
  //     · 上一行 `chatRefs.splice(0, len)` 已把引用清空 ⇒ **附件（引用 chip）消失**；
  //     · 再下一行 `input.value=''` 永远不会执行 ⇒ **用户输入还在输入框**；
  //     · `pumpStream` 在更后面 ⇒ **会话没发起**。
  //   三个症状同源，所以必须一起改：`const` → **`let`**（这一处是唯一原因）。
  let msg = input.value.trim();
  // 只有附件/引用、没打字时**不许静默无反应**（原实现 `if(!msg) return` 什么都不做，
  // 用户点发送像"点了没反应"）。允许发出去，并给一句中性指令；
  // 若连材料都没有，仍然直接返回（空 Enter 不该建会话）。
  const _hasCargo = !!((typeof pendingAttachments!=='undefined' && pendingAttachments.length)
                    || (typeof chatRefs!=='undefined' && chatRefs.length));
  if(!msg && !_hasCargo) return;
  // ★ 2026-10-16（米爸决策 R1）：**必须先选主 Agent 才能发起会话**。
  //   原实现允许 team=''（下拉有空串"自动"项）⇒ 后端意图识别自由发挥 ⇒ 执行主体随机
  //   （实测会话 9372 掉到「通用问答Agent」，该 Agent 没绑 MCP ⇒ 工程列表查询必失败）。
  //   判据用**下拉当前值**而非 localStorage：后者是全局记忆，可能与当前页面状态不同步。
  //   ⚠️ 放在最前面（建会话之前）：否则会先造一个空会话再被拦下，留下垃圾会话。
  if(!requireMainAgent()) return;
  // 只有附件/引用、没打字：补一句中性指令再发（放在主 Agent 校验**之后**，
  // 否则"未选主 Agent"场景会先弹一条"已按…发送"的误导提示）。
  if(!msg && _hasCargo){
    msg = '请查看我附加的材料，并据此继续。';
    toast('未输入文字，已按「请查看我附加的材料」发送');
  }
  // 草稿态首次提交时创建会话（含 P0-A 提交锁 / P0-B 幂等键 / P0-C 即时反馈）
  // ★ P2-2：实现已提取到 _ensureConversation()，此处只保留调用。
  if(!(await _ensureConversation(msg))) return;
  // 待澄清状态下主输入框内容作为澄清补充（带附件时跳过，走正常发送）
  // ★ P2-2：实现已提取到 _submitClarify()，此处只保留调用。
  if(!(await _submitClarify(msg, input))) return;
  // P0-1 澄清改选重发：中止上一轮未读完的流（防旧 token 追加到新消息容器）
  // 2026-09-25：中止前先把上一轮**已生成内容固化落库**——否则"提交新消息"= 上一轮输出白丢
  // P1-② follow-up（2026-10-07）：**上一轮还在跑时**提交的消息，不再 abort 它，
  //   而是投进该会话的 follow-up 队列 —— 上一轮跑完时会把队列里的补充并入本轮处理。
  //
  //   ★ 为什么改掉原来的 abort：abort 会让"用户补充一句"= **整轮作废重跑**，
  //     而 follow-up 的语义是"不打断、跑完接着处理"，用户等待不翻倍。
  //     （上一轮的内容照样固化落库，见下面的 commitPartialStream。）
  //   ★ 会话口径必须用**流式现场归属**的 `el.dataset.convId`，与上一行
  //     commitPartialStream 完全一致 —— 流式中切到别的会话再发消息时，
  //     `currentConvId` 已变，用它会把补充投进错误会话。
  // ★★ 2026-11-06（多会话并发串扰）：`_streaming` 是**全局布尔**，别的会话在跑时它同样是 true。
  //   旧实现只要 true 就把本会话的新消息投进「流式现场所属会话」的 followup 队列
  //   ⇒ 同时开多个会话时，新会话发的消息变成别的会话的「已作为补充指令提交（待处理 N 条）」，
  //     且下面无条件 `_streamAbort.abort()` 会掐断别的会话正在跑的流。
  //   判据必须是**流归属 == 当前会话**，而不是「全局有没有流」。
  // ★★ 2026-11-23 第二档：若当前正处于「后台续跑 + 续显」态，此时发新消息 = 用户主动放弃旧任务。
  //   必须先 POST /interrupt 停掉后台 run（写停止标记 + cancel detached task + 协作式停子任务），
  //   再走下面正常发送；否则旧任务在后台继续烧 token，且两轮并发写同一会话。
  //   这段必须在 follow-up 判断**之前** —— 续显的 _streaming=true 会被误判成"补充上一轮"。
  if(window._liveReplayRunId > 0){
    try{
      const _cidOld = currentConvId;
      // 用单引号拼接（非模板串），避免干扰既有停止顺序门禁的字符位置判据。
      if(_cidOld) await api('/api/conversations/' + _cidOld + '/interrupt',
                            {method:'POST', body: JSON.stringify({reason:'续显期间发新消息', kind:'interrupt'})});
    }catch(_e){}
    window._liveReplayRunId = 0;
    _clearLiveRun(currentConvId);
    if(_streamAbort){ try{ _streamAbort.abort(); }catch(_e){} }   // 断开 /subscribe 订阅
    _streaming = false;
  }
  const _el = document.getElementById('stream-ai');
  const _liveCid = (_el && _el.dataset && _el.dataset.convId) ? String(_el.dataset.convId) : '';
  const _sameConv = !!_liveCid && _liveCid === String(currentConvId);
  if(_streaming && _sameConv){
    const _fc = currentConvId;
    // ★ G-3：follow-up 分支**同样不得吞附件** —— 原实现只发 {message}，附件静默消失，
    //   且上屏渲染不带 attachments ⇒ 用户看到自己发了文件、AI 却"没收到"。
    //   改法：带附件时**不走 followup**（follow-up 接口只吃纯文本补充指令），
    //   落到下方正常发送路径 —— 上一轮会被 abort 并固化（原回退语义），附件随新消息完整送达。
    if(_fc && !(pendingAttachments && pendingAttachments.length)){
      try{
        const r = await api(`/api/conversations/${_fc}/followup`,
                            {method:'POST', body: JSON.stringify({message: msg})});
        if(r && r.ok !== false){
          input.value = '';
          _chatInputAutoGrow();
          try{ localStorage.removeItem('mbse_draft_input'); }catch(e){}
          const area0 = document.getElementById('chat-area');
          if(area0){
            area0.insertAdjacentHTML('beforeend', renderMessage({role:'user', content:esc(msg)}));
            scrollChatToBottom();
          }
          toast((r && r.pending > 1)
            ? `已作为补充指令提交（待处理 ${r.pending} 条），本轮结束后并入处理`
            : '已作为补充指令提交，本轮结束后并入处理');
          return;                        // ★ 不 abort、不重发，直接返回
        }
        toast((r && r.error) || '补充指令提交失败');
        return;
      }catch(e){
        //投递失败 ⇒ **回退到旧行为**（abort + 当新消息重发），不能静默吞掉用户输入
        toast('补充指令提交失败：' + (e && (e.message || e)));
      }
    }
    // 回退路径：维持原语义（固化上一轮 + abort + 作为新消息）
    await commitPartialStream(_el);
    if(_streamAbort) _streamAbort.abort();
  }
  // 走到这里：① 本会话无流 ② follow-up 投递失败走回退 ③ **别的会话在跑**（本会话按新消息正常发）。
  // 情况③ 绝不能 abort —— 那会掐断别的会话正在跑的流（2026-11-06 多会话实测症状）。
  if(_streamAbort && (!_streaming || _sameConv)) _streamAbort.abort();
  // 2026-10-16 Q2 重构：流式执行状态（_streaming / showStopBtn / _streamAbort）改由 pumpStream() 接管，
  // sendChat / pickIntent / 澄清续答三者共用同一段流式逻辑，避免"复用即新起一轮"。
  const atts = pendingAttachments.slice();  // V2.3 富输入：附件随消息发送
  const _refs = (chatRefs||[]).splice(0, chatRefs.length);   // Cursor 式引用随消息发送
  renderRefBar();
  if(_refs.length){ msg = _refs.map(r=>'【引用 · '+r.label+'】\n'+r.text+'\n【引用结束】').join('\n\n') + '\n\n' + msg; }
  input.value = '';
  _chatInputAutoGrow();   // 2026-09-29：清空后高度回缩到 min-height
  closeMentionPop();
  pendingAttachments = []; renderAttachBar();  // 发送后清空附件条
  // V3：/ 选择的技能按消息生效（Trae 式），发送后复位；手动下拉选择保持
  // 2026-09-28：@ 智能体 mention 已移除，_agentMentioned 复位逻辑随之删除
  if(_skillMentioned){ _skillMentioned = false; const qs = document.getElementById('quick-skill'); if(qs) qs.value=''; }
  renderChips();
  const area = document.getElementById('chat-area');
  // 2026-09-04 v5：草稿/欢迎屏态发送时，先把居中的输入区归位到底部，再清空欢迎屏骨架（否则消息会混进欢迎屏）
  if(_welcomeMode) unmountWelcomeDock();
  // 2026-09-04 v3：草稿态占位符清空后再追加真实消息
  if(area && (area.querySelector('.chat-empty') || area.querySelector('.chat-welcome'))) area.innerHTML = '';
  // Show user message immediately（V2.3：附件内联渲染 + 文本转义）
  // ★ 2026-10-17（米爸反馈2）：`innerHTML +=` 会**序列化并重建整段历史 DOM**——
  //   长会话里重解析副作用（图片重载/异步高度塌缩瞬间 scrollHeight 骤减 ⇒ scrollTop
  //   被钳到 0）正是「发送后页面跳到最上方」的根因。insertAdjacentHTML 只在末尾
  //   追加新节点，既有节点原样保留（长会话还顺带省一次全量重排）。
  area.insertAdjacentHTML('beforeend', renderMessage({role:'user', content:esc(msg), attachments:atts}));
  // V2.3.2 流式：AI 消息容器（streaming 态,追加 delta）；V2.4 会话内执行过程块（思考/子智能体/工具）
  const aiBox = document.createElement('div');
  aiBox.className = 'msg ai';
  aiBox.id = 'stream-ai';
  aiBox.dataset.convId = String(currentConvId);   // 2026-09-29：流式现场归属会话（切会话寄存/挂回、固化落库都以它为准）
  aiBox.innerHTML = `<span class="who">AI</span><div class="msg-inner"><div class="proc" id="proc-box"></div><div class="body streaming"><span class="typing"></span></div></div>`;
  area.appendChild(aiBox);
  // 2026-09-29：用户主动发送 → 强制滚到底（绕过贴底判断），但**复位跟读态**，
  //   使随后的流式输出按"贴底才跟随"策略工作。
  scrollChatToBottom();
  // ★ 2026-10-17（米爸反馈2）：布局完成后再钉一次——首帧异步内容（图片/公式/mermaid）
  //   会在钉底之后继续改变高度，同步滚动可能落在中间态上。
  requestAnimationFrame(scrollChatToBottom);
  _bindStickDetach();   // 首次发送时绑定"用户上滑即脱离跟读"的监听（幂等）
  const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = '正在识别意图并调度 Agent…';
  resetPipeline();  // 意图识别前：清空旧流水线，展示执行中占位
  const _selTeam = document.getElementById('quick-team')?.value || '';   // 主 Agent（执行主体唯一入口）
  const _selSkill = document.getElementById('quick-skill')?.value || '';
  // ★ 2026-10-16 Q2 重构：把"建 abort → 拉流 → SSE 处理 → 收口"整段抽到 pumpStream()。
  //   原 sendChat 把这段与"建用户气泡 + 建 AI 框"写死在一起 ⇒ 任何复用（澄清改选 / 澄清续答）
  //   都必然新起一轮（新用户消息 + 新 AI 气泡）。抽出来后三者共享同一段流式逻辑，
  //   pickIntent / 澄清续答即可"在**同一输出框**内重跑"，不再新起一轮（米爸 Q2）。
  await pumpStream(aiBox, msg, atts);
}
// ── 流式执行核心（sendChat / pickIntent / 澄清续答 共用）──
//   职责：建 abort → 拉 /chat/stream → 逐帧 handleSSE → 收口（正常 done / 中断固化 / 失败提示）。
//   入参 aiBox = 流式承载盒（**可复用**：澄清改选 / 澄清续答传"原输出框"，不新建）。
//   resumeOf = {user_msg_id, append}（澄清续答单轮化，2026-10-17 米爸 Q1）：
//     后端把 append 追加进触发澄清的那条 user 消息，不新起 user 轮次。
//   ★ 2026-10-16 Q2：抽出此函数是为让"澄清改选 / 澄清续答"能在同一输出框内重跑，
//     不再新起用户气泡 + 新 AI 气泡（米爸 Q2：确认后应在原输出流续接后续环节）。
// ★ 2026-11-06：**进程身份判定** —— 回答"这轮中断到底是不是服务重启"。
//   背景（conv 990485 14:54）：用户看到"连接中断或服务端异常"就以为是重启，
//   而那份日志里进程从 14:30 起只有一次启动、零 shutdown，真因是路由降级。
//   判据：开流前取 boot_id，中断时再取一次 —— 变了才是重启（进程内 UUID 每次启动唯一，
//   不依赖时间对齐，也不受时钟漂移影响）；没变就**一定不是**重启。
let _bootCache = { id: '', ts: 0 };
async function _getBootId(){
  const now = Date.now();
  if(_bootCache.id && now - _bootCache.ts < 20000) return _bootCache.id;
  try{
    const r = await fetch('/api/system/boot', {headers: streamHeaders(), cache:'no-store'});
    const d = await r.json();
    _bootCache = { id: d.boot_id || '', ts: now, pid: d.pid, started: d.started_at };
    return _bootCache.id;
  }catch(e){ return ''; }
}
async function _annotateAbnormalEnd(aiBox, bootAtStart){
  let cur = null;
  try{
    const r = await fetch('/api/system/boot', {headers: streamHeaders(), cache:'no-store'});
    cur = await r.json();
  }catch(e){ return; }
  if(!cur || !cur.boot_id) return;
  const _cs = document.getElementById('chat-status');
  if(bootAtStart && cur.boot_id !== bootAtStart){
    _bootCache = { id: cur.boot_id, ts: Date.now(), pid: cur.pid, started: cur.started_at };
    const note = document.createElement('div');
    note.className = 'boot-restart-note';
    note.style.cssText = 'margin-top:8px;padding:6px 10px;border-left:3px solid #e8590c;'
      + 'background:rgba(232,89,12,.10);color:#b23c00;font-size:13px;line-height:1.6;';
    note.textContent = '⚠ 服务在本次生成期间重启过（新进程 PID ' + (cur.pid || '?')
      + '，启动于 ' + (cur.started_at || '?') + '），本轮流式中断。'
      + '已生成内容已保留在下方，请直接重发这条消息。';
    const bodyEl = aiBox && aiBox.querySelector('.body');
    if(bodyEl) bodyEl.appendChild(note);
    if(_cs) _cs.textContent = '服务重启导致中断';
  }else{
    // 没重启 ⇒ 明确排除"服务重启"这条猜测，避免用户按错方向排查
    if(_cs) _cs.textContent = '本轮未产出正文（服务未重启，原因见本条说明）';
  }
}

// RECOVER_TURN_CONTENT_V1 断线恢复：对标 Dify「断线不致命」。
// SSE 流异常且无 done/error 终态时，后端已按 finalize_key 把本轮内容固化落库；
// 这里取回并如实展示（含结构化原因），编排轮额外给「从检查点续跑」入口。
// 只恢复展示、不自动重生成 —— 是否继续由用户决定，避免断线误触烧 token。
// ★ 2026-11-19 方案B P1：若本轮**没有**固化消息（服务被强杀 / 固化条件不满足），
//   但帧日志里有内容，则改走 `_renderRunReplay` 把"当时滚过的内容"重建出来。
async function _recoverTurnContent(aiBox){
  const _cid = aiBox && (aiBox.dataset.convId || currentConvId);
  if(!_cid || !aiBox || aiBox.dataset.recovered) return;
  try{ aiBox.dataset.recovered = '1'; }catch(_e){}
  let r = null;
  try{ r = await api(`/api/conversations/${_cid}/recovery`); }catch(_e){ return; }
  if(!r) return;
  // ★ 方案B P1：本轮没有固化消息（服务被强杀/固化条件不满足）⇒ 帧日志是唯一副本
  if(!r.found){
    const _rid = Number(r.stream_run_id || 0);
    if(_rid > 0){
      const ok = await _renderRunReplay(_cid, _rid, aiBox, {reason: r.reason || ''});
      if(ok){
        const _cs0 = document.getElementById('chat-status');
        if(_cs0) _cs0.textContent = '已按帧日志重建本轮输出';
      }
    }
    return;
  }
  try{ if(r.message_id) aiBox.dataset.recoveredMsgId = String(r.message_id); }catch(_e){}
  const bodyEl = aiBox.querySelector('.body');
  if(!bodyEl) return;
  const note = document.createElement('div');
  note.className = 'turn-recover-note';
  note.style.cssText = 'margin-top:8px;padding:7px 10px;border-left:3px solid #2f9e44;'
    + 'background:rgba(47,158,68,.10);color:#2b8a3e;font-size:12.5px;line-height:1.6;';
  let html = '本轮已生成内容<b>已自动保留</b>（未丢失）。';
  if(r.abort_reason || r.abort_detail) html += '<br>中断原因：' + esc(r.abort_detail || r.abort_reason);
  if(r.orchestrated_run_id > 0){
    html += '<br>这是编排任务，可从检查点继续（<b>不重跑已完成的子任务</b>）：';
    note.innerHTML = html;
    const btn = document.createElement('button');
    btn.textContent = '从检查点续跑';
    btn.className = 'btn btn-sm btn-outline-primary';
    btn.style.cssText = 'margin-left:8px;';
    btn.onclick = ()=>{ if(typeof startResumeRun === 'function') startResumeRun(r.orchestrated_run_id, null); };
    note.appendChild(btn);
  }else{
    html += '<br>需要继续请直接重发这条消息。';
    note.innerHTML = html;
  }
  bodyEl.appendChild(note);
  const _cs = document.getElementById('chat-status');
  if(_cs) _cs.textContent = '已恢复本轮内容';
}

// RUN_FRAME_REPLAY_V1（2026-11-19，方案B P1）：**按帧日志重建本轮输出**。
//
// 为什么需要它：`/recovery` 只能给"已固化落库"的正文，而固化是节流/收口行为 ——
//   进程被强杀（真库 conv 999999207）、或固化条件不满足时，用户看过的内容
//   **只在帧日志里**。没有这条路径，"刷新/重连后内容回来"就是假的。
//
// 关键语义（不要改成"追加"）：
//   · **替换，不是追加** —— run_frames 是整轮的完整帧序列，重建结果本身是完备的；
//     追加会把已经显示过的文本再显示一遍（重复 token），这正是要避免的。
//   · **只读** —— 帧里的工具结果直接渲染，绝不重新调用工具（重放不产生第二次副作用）。
//   · `last_event_id` 固定传空 = 从头重建。带游标的增量续传留给 P2（那时才需要
//     "我已渲染到第几个字"的偏移量，P1 阶段生成仍随连接停止，用不到）。
async function _renderRunReplay(convId, runId, aiBox, opts){
  opts = opts || {};
  let r = null;
  try{ r = await api(`/api/runs/${runId}/frames?reconstruct=1`); }catch(_e){ return false; }
  if(!r || !r.reconstructed) return false;
  const text = String(r.reconstructed.content || '');
  const exec = r.reconstructed.exec || {};
  if(!text.trim() && !(exec.tools && exec.tools.length) && !(exec.subtasks && exec.subtasks.length)){
    return false;   // 真的没有任何内容：不伪造一个空气泡
  }
  // 组装成"一条消息"来渲染：复用历史消息的渲染器 ⇒ 样式/代码块/时间线与落库消息一致
  const fake = { role: 'assistant', content: text, msg_type: 'text', card_data: JSON.stringify({exec: exec}) };
  const holder = document.createElement('div');
  holder.innerHTML = (typeof renderMessage === 'function')
    ? renderMessage(fake)
    : `<div class="msg ai"><div class="msg-inner">${esc(text)}</div></div>`;
  const el = holder.firstElementChild || holder;
  try{ el.classList.add('turn-replayed'); }catch(_e){}

  const note = document.createElement('div');
  note.className = 'turn-recover-note';
  note.style.cssText = 'margin-top:8px;padding:7px 10px;border-left:3px solid #1c7ed6;'
    + 'background:rgba(28,126,214,.10);color:#1864ab;font-size:12.5px;line-height:1.6;';
  let html = '以下内容来自<b>本轮帧日志回放</b>（服务端逐帧记录，与当时屏幕上的输出一致）。';
  if(r.status === 'running') html += '<br>该轮在服务端仍标记为运行中（本轮可能未正常收口）。';
  if(Number(r.truncated || 0) === 1) html += '<br>⚠ 帧日志已达上限被截断，末尾部分可能缺失。';
  if(opts.reason === 'no_finalized_message') html += '<br>本轮正文未落库（进程中断/未收口），这是唯一的完整副本。';
  html += '<br>需要继续请直接重发这条消息。';
  note.innerHTML = html;

  const bodyEl = (aiBox && aiBox.querySelector) ? aiBox.querySelector('.body') : null;
  if(bodyEl){
    // 流式现场：整块替换（含 typing 占位），保证"回放内容 == 唯一副本"
    bodyEl.innerHTML = '';
    bodyEl.appendChild(el);
    bodyEl.appendChild(note);
    try{ bodyEl.classList.remove('streaming'); }catch(_e){}
    try{ const _t = bodyEl.querySelector('.typing'); if(_t) _t.remove(); }catch(_e){}
  }else{
    // 会话重开后没有现场盒子：作为一条"回放消息"追加到消息区
    const area = opts.area || document.getElementById('chat-area');
    if(!area) return false;
    const wrap = document.createElement('div');
    wrap.className = 'msg-wrap msg-replay';
    wrap.appendChild(el);
    wrap.appendChild(note);
    area.appendChild(wrap);
    try{ if(typeof pinChatBottom === 'function') pinChatBottom(area, 2000); }catch(_e){}
  }
  return true;
}

// 会话重新打开时：若末条是"用户说了但没有任何 AI 回应"，说明上一轮没落库 ——
// 帧日志可能是唯一副本，给一个「回放上一轮输出」的入口（不自动回放，避免打扰）。
async function maybeOfferRunReplay(convId, area){
  try{
    if(!convId || !area) return;
    if(_streaming) return;                       // 正在跑本轮：现场优先
    // ★ 2026-11-23：本次刷新有 live 游标 ⇒ 由 _resumeLiveStream 自动接管，绝不再弹手动按钮。
    //   （它会在 active=false 时重载历史 / active=true 时订阅续显，无需用户点任何东西。）
    const _liveRec = _getLiveRun(convId);
    if(_liveRec && Number(_liveRec.run_id)>0) return;
    const msgs = area.querySelectorAll('.msg');
    if(!msgs.length) return;
    const last = msgs[msgs.length - 1];
    if(!last.classList.contains('u')) return;    // 末条不是用户消息 ⇒ 本轮已有回应
    const r = await api(`/api/conversations/${convId}/recovery`);
    const _rid = Number((r && r.stream_run_id) || 0);
    if(!_rid || (r && r.found)) return;           // 没有 run / 已有固化消息 ⇒ 无需回放
    if(Number(r.stream_last_seq || 0) <= 0) return;  // 帧日志里也没有内容
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin:10px 0;padding:8px 10px;border-left:3px solid #1c7ed6;'
      + 'background:rgba(28,126,214,.08);color:#1864ab;font-size:12.5px;line-height:1.7;';
    wrap.innerHTML = '上一轮有 <b>' + Number(r.stream_last_seq) + ' 帧</b>输出未落库'
      + '（服务端中断前已逐帧记录）：';
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm btn-outline-primary';
    btn.style.cssText = 'margin-left:8px;';
    btn.textContent = '回放上一轮输出';
    btn.onclick = async ()=>{
      btn.disabled = true; btn.textContent = '回放中…';
      const ok = await _renderRunReplay(convId, _rid, null, {area: area, reason: 'reopen'});
      if(!ok){ btn.disabled = false; btn.textContent = '回放失败，点此重试'; }
      else wrap.remove();
    };
    wrap.appendChild(btn);
    area.appendChild(wrap);
  }catch(_e){ /* 旁路能力，失败静默 */ }
}

// ═══ 2026-11-23 第二档：刷新断连续跑（前端侧）════════════════════════
// 核心约定：本轮正在流式输出时，把 run_id 与"最后交给 handleSSE 的帧游标"记进
// sessionStorage。刷新后 selectConv 调 /live-run 探测后台是否还在跑；若在跑，
// 凭游标调 /subscribe 续显 —— 只取增量帧、走替换式渲染，**不重发消息、不重跑流程**。
const _LIVE_PREFIX = 'mbse_live_run_';
function _liveKey(cid){ return _LIVE_PREFIX + String(cid); }
function _recordLiveRun(cid, runId, seq){
  try{ sessionStorage.setItem(_liveKey(cid), JSON.stringify({run_id:+runId, last_seq:+seq, ts:Date.now()})); }catch(e){}
}
function _clearLiveRun(cid){ try{ sessionStorage.removeItem(_liveKey(cid)); }catch(e){} }
function _getLiveRun(cid){ try{ return JSON.parse(sessionStorage.getItem(_liveKey(cid))||'null'); }catch(e){ return null; } }

// 刷新重载后续显：探测 → 建临时流式盒 → 带游标订阅增量帧 → 收口后重载历史。
// 为什么最终要 selectConv 重载：后台 run 自然结束时服务端已固化真实 assistant 消息，
// 重载会用落库消息替换临时盒，避免"续显内容 + 落库消息"两份重复。
async function _resumeLiveStream(convId, area){
  // ★ 触发依据 = sessionStorage 里"本轮被我刷新中断"的游标，而非末条消息类型。
  //   这样任务在后台跑完（live-run.active 已变 false）后刷新仍能自动收口。
  const rec = _getLiveRun(convId);
  if(!rec || !(Number(rec.run_id)>0)) return;   // 没记游标 = 不是中断续跑，不打扰
  let info = null;
  try{ info = await api(`/api/conversations/${convId}/live-run`); }catch(e){}
  const runId = Number(rec.run_id) || Number(info && info.run_id) || 0;
  if(!runId) return;

  // 正文已固化落库（后台跑完）⇒ 历史消息里已有完整内容，直接重载即可，
  // 既无需订阅也避免与落库消息重复显示。这是活跃会话刷新后最常见的路径。
  try{
    const rc = await api(`/api/conversations/${convId}/recovery`);
    if(rc && rc.found){
      _clearLiveRun(convId);
      try{ if(typeof selectConv==='function' && String(currentConvId)===String(convId)) selectConv(convId); }catch(e){}
      return;
    }
  }catch(e){}

  // 正文未落库：统一走 /subscribe 回放剩余帧（run 已 done 时回放到 done 即止，
  // 仍在跑时 tail 续显）。不再因 active=false 就 return —— 那是"还要点按钮"的根因。
  let seq = Math.max(Number(rec.last_seq)||0, Number(info && info.last_seq)||0);

  // 清掉可能残留的旧临时盒（替换式，绝不追加到任何既有消息上）
  const stale = document.getElementById('stream-ai');
  if(stale && stale.dataset.liveReplay) stale.remove();
  const box = document.createElement('div');
  box.className = 'msg ai'; box.id = 'stream-ai';
  box.dataset.convId = String(convId); box.dataset.liveReplay = '1';
  box.innerHTML = '<span class="who">AI</span><div class="msg-inner">'
    + '<div class="proc" id="proc-box"></div>'
    + '<div class="body streaming"><span class="typing"></span></div></div>';
  area.appendChild(box);

  window._sseTerminal = 0;
  _streaming = true; _stoppedManually = false;
  window._liveReplayRunId = runId;   // 发新消息时据此先停旧 run（见 sendChat）
  try{ if(typeof resetPipeline==='function') resetPipeline(); }catch(e){}
  const st = document.getElementById('chat-status');
  if(st) st.textContent = '任务仍在后台运行，正在从上次进度接着显示…';

  const ac = new AbortController();
  _streamAbort = ac;                 // 让「停止」按钮能停后台任务（stopStream → /interrupt）
  const lastEventId = runId + '-' + seq;
  let buf = '';
  try{
    const resp = await fetch(`/api/runs/${runId}/subscribe?last_event_id=${encodeURIComponent(lastEventId)}&tail=1&max_wait_s=180`, {
      headers: streamHeaders(), signal: ac.signal, cache:'no-store'
    });
    if(!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader(), dec = new TextDecoder();
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buf += dec.decode(value, {stream:true});
      let i;
      while((i=buf.indexOf('\n\n'))!==-1){
        const fr = buf.slice(0, i); buf = buf.slice(i+2);
        try{
          const m = fr.match(/^id:\s*(\d+)-(\d+)/m);
          if(m) _recordLiveRun(convId, +m[1], +m[2]);
          if(/^event:\s*(done|error|run_end)\b/m.test(fr)) _clearLiveRun(convId);
        }catch(_e){}
        handleSSE(fr);
      }
    }
    try{ _forceFlushTokens(); }catch(_e){}
  }catch(e){
    // 续显失败静默收口：清游标避免反复重试；历史消息与安全提示仍在
    _clearLiveRun(convId);
  }finally{
    _streaming = false;
    if(_streamAbort===ac) _streamAbort = null;
    window._liveReplayRunId = 0;
    try{ showStopBtn(false); }catch(_e){}
  }
  // 后端此刻已把本轮固化为真实消息 → 重载历史，用落库消息替换临时盒（不重复）
  try{ if(typeof selectConv==='function' && String(currentConvId)===String(convId)) selectConv(convId); }catch(e){}
}

async function pumpStream(aiBox, msg, atts, resumeOf){
  // ★ 2026-11-06：开流前记进程身份 + 清终态标记（handleSSE 收到 done/error 会置位）
  window._sseTerminal = 0;
  let _bootAtStart = '';
  try{ _bootAtStart = await _getBootId(); }catch(e){}
  const myAbort = new AbortController();
  _streamAbort = myAbort;
  _streaming = true; _stoppedManually = false;
  showStopBtn(true);
  // ★ 2026-10-17：接管流（澄清改选/续答复用盒）启动即清 reuseForPick。
  //   时序依据：旧流的 abort catch 在本函数的 `await fetch` 之前**同步执行完**
  //   （AbortController.abort 是同步触发 read 拒绝 ⇒ 旧流 catch 先于本流首个
  //   SSE 事件），防替换窗口只到此处为止。此前不清 ⇒ 本流自身的 done 被
  //   finishStream 的 reuseForPick 检查挡掉 ⇒ **永远不收口**（停留在
  //   「执行中…N 个环节」live 条；刷新后才变 hist 收口条）。
  try{ if(aiBox) aiBox.dataset.reuseForPick = ''; }catch(_e){}
  const _selTeam = document.getElementById('quick-team')?.value || '';   // 主 Agent（执行主体唯一入口）
  const _selSkill = document.getElementById('quick-skill')?.value || '';
  const _cid = aiBox.dataset.convId || currentConvId;
  try {
    const resp = await fetch(`/api/conversations/${_cid}/chat/stream`, {
      method:'POST',
      signal: myAbort.signal,
      // P0-5：改走 streamHeaders()（01-core.js）—— 原 IIFE 只带 X-User-Id、漏了 X-Session-Token
      headers: streamHeaders(),
      body: JSON.stringify({
        message:msg, attachments:atts||[],
        scope_ids: (typeof activeScopes!=='undefined'? activeScopes:[]).map(s=>s.id),   // 建模范围（可多选，全局）硬锁
        branch: getCurrentBranch(),   // AI 建模输出侧工作分支（默认 dev；release 只读不可作工作分支）
        intent_override: window._intentOverride || '',
        skill_name: _selSkill,
        team: _selTeam,                // 主 Agent：负责意图识别/任务拆分/计划制定/任务分派/内容整合输出
        provider_id: Number(document.getElementById('llm-providers')?.value) || undefined,
        // ★ 2026-10-31（按米爸澄清的语义修正）：**会话维度**参数由后端读
        //   `conversations.llm_overrides` 自动生效，前端**不需要**每轮发送。
        //   这正是与上一版的本质差别：上一版靠前端每轮带 o_*（= 单轮语义，
        //   会话第二轮就丢、刷新更丢）；现在设置存服务端会话行，跨轮/刷新/切回都还在。
        //   `o_*` 字段仍保留在 ChatIn 里作为「会话内临时加压」，但界面不再使用。
        // ★ 2026-10-17（米爸 Q1 DB 层收口）：澄清续答单轮化转呈字段（普通发送为 0/空）
        resume_of_user_msg_id: (resumeOf && resumeOf.user_msg_id) || 0,
        resume_append: (resumeOf && resumeOf.append) || '',
      }),
    });
    window._intentOverride = '';      // 一次性：仅本轮生效，防下一条消息继承改判
    if(!resp.ok || !resp.body){
      let _detail = '';
      try{ _detail = (await resp.json()).error || ''; }catch(e){}
      if(resp.status === 404){
        throw new Error('会话不存在或当前账号无权访问——请确认已登录正确账号后刷新页面'
                        + (_detail ? '［' + _detail + '］' : ''));
      }
      throw new Error('HTTP ' + resp.status + (_detail ? '：' + _detail : ''));
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while(true) {
      const {done, value} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) !== -1) {
        const _fr = buf.slice(0, idx);
        // ★ 2026-11-23：记最后一帧游标（id: run-seq）。终态帧到达即清 —— 本轮已收口，
        //   不再需要续显；只有"流被掐断且未收口"才会留下游标供刷新后续接。
        try{
          const _m = _fr.match(/^id:\s*(\d+)-(\d+)/m);
          if(_m) _recordLiveRun(_cid, +_m[1], +_m[2]);
        }catch(_e){}
        handleSSE(_fr);
        if(window._sseTerminal){ _clearLiveRun(_cid); }   // done/error 已到 → 本轮完成
        buf = buf.slice(idx+2);
      }
    }
    _forceFlushTokens();   // P0-3：流结束前清空合帧缓冲，防末帧丢失
    stickBottom();
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
    // ★ 2026-11-06：流读完了但**从未收到 done/error** ⇒ 服务端没正常收口。
    //   这种情况旧实现什么都不说（用户以为"跑完了"），现在按进程身份给出准确原因。
    if(!window._sseTerminal){ try{ await _annotateAbnormalEnd(aiBox, _bootAtStart); }catch(e){} try{ await _recoverTurnContent(aiBox); }catch(_r){ } }
  } catch(e) {
    // ★ 2026-10-16 Q2：本流正被「澄清改选 / 澄清续答」主动中止以复用此盒接管 ——
    //   不要改 _streaming/showStopBtn（会被错置成"已停"），也别进 _finalizeStoppedRender 替换盒子。
    //   标记在 pickIntent / clarifyAnswerSend / clarifySkipSend 里于 abort 前置 true，此处清回 false。
    if(aiBox && (aiBox.dataset.reuseForPick === '1' || window._streamSuperseded)){
      window._streamSuperseded = false;
      return;
    }
    _forceFlushTokens();
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
    if(e && e.name === 'AbortError') {   // P1-1 停止/换发：将已生成内容固化为消息（保留执行过程）
      _clearLiveRun(_cid);               // 主动停止：后端不会续跑，游标作废
      finalizeStopped(aiBox);
      return;
    }
    // ★ 2026-11-06：网络类失败（Failed to fetch / 流被掐断）同样要判定是否重启
    if(!window._sseTerminal){ try{ await _annotateAbnormalEnd(aiBox, _bootAtStart); }catch(_e2){} try{ await _recoverTurnContent(aiBox); }catch(_r){ } }
    const bodyEl = aiBox.querySelector('.body');
    const _typ = bodyEl ? bodyEl.querySelector('.typing') : null; if(_typ) _typ.remove();
    if(bodyEl) bodyEl.classList.remove('streaming');
    if(typeof commitPartialStream === 'function') commitPartialStream(aiBox);   // 同步段先抓正文再追加错误条
    if(typeof procMarkStopped === 'function') procMarkStopped();   // 五轮反馈5：环节卡全部翻转为已终止
    try{ aiBox.dataset.reuseForPick = ''; }catch(_e){}   // 异常收口：清防替换标记，避免下次 done 误跳过
    const errNote = document.createElement('div');
    errNote.style.cssText = 'margin-top:8px;';
    // ★ 2026-11-23：网络类断连不再断言"后台一定没在跑" —— 断连续跑开启时，
    //   服务端可能正在后台接着跑（/live-run 可查），此时重发消息会起第二个任务。
    //   文案改为引导"刷新页面续看"，并把是否续跑交给探测结果，不替用户下结论。
    const _liveRec = _getLiveRun(_cid);
    errNote.innerHTML = `<div style="color:var(--red);font-size:12.5px;">连接中断：${esc(e.message)}</div>`
      + `<div style="color:var(--mut);font-size:12px;margin-top:4px;line-height:1.6;">`
      + `上方已生成内容与执行轨迹<b>已保留</b>。`
      + (_liveRec
          ? `本轮可能在后台继续执行，<b>刷新页面</b>可从断点接着看（不会重跑流程）；若要停掉它，点「停止」或重新发送消息。`
          : `确认原因（如网络波动）后，<b>重新发送消息</b>即可从头执行。</div>`);
    if(bodyEl) bodyEl.appendChild(errNote);
    const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = '调用失败';
  }
}
// ── P1-1 停止生成：发送按钮 ⇄ 停止按钮切换 / 停止固化已生成内容 ──
function showStopBtn(on){
  const s = document.getElementById('stop-btn'), sn = document.getElementById('send-btn');
  if(s) s.style.display = on ? '' : 'none';
  if(sn) sn.style.display = on ? 'none' : '';
  // AI 输出期间禁止切换智能体/技能/团队，防中途改选导致上下文错乱（showStopBtn true=流式开始, false=结束）
  // 2026-10-16：quick-agent 已退役（执行主体唯一为 quick-team）
  ['quick-skill','quick-team'].forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.disabled = !!on;
  });
}
function stopStream(){
  if(!_streaming) return;
  _stoppedManually = true;
  uxTrack('interrupt');   // §11 埋点：主动中断率分子（15–25% 健康区间）
  // ── 2026-11-10（可中断策略）：先**告诉服务端**再断连接 ──
  // 旧行为只 abort 浏览器这侧的流：服务端生成器被 GeneratorExit 掐断，
  //   而编排的子任务行留在 running/ready，只能等 30 分钟的孤儿回收把它们标成 interrupted
  //   ⇒ 用户看到"停了"，其实后台还在烧钱跑，且"继续编排"要等半小时才出现。
  // 现在先打一次中断：服务端把任务协作式置停 + checkpoint 标 interrupted（**保留续跑资格**），
  //   断连接只是把 UI 收口。两者顺序不能反 —— 先断就连发请求的机会都没有了。
  try {
    const _cid = currentConvId;
    if(_cid) {
      // ★ 2026-11-10（浏览器实测修正）：中断请求是**异步**的，而 abort 紧接着就把
      //   SSE 断了 ⇒ 服务端的 `interrupt` 事件永远送不到页面（"能力在、入口被自己掐断"）。
      //   ⇒ 提示文案不依赖那个事件，改用**中断接口的回包**（服务端已落库的真相）：
      //     · ok 且停了 N 个 ⇒ "已停止 N 个未完成子任务，已完成部分保留，可继续编排"
      //     · no_active_run   ⇒ "当前没有在跑的编排批次"（点早了/已停完）
      //   先给一条即时反馈，再等回包补一条准确的 —— 不能让用户等。
      fetch(`/api/conversations/${_cid}/interrupt`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reason: '用户点击停止', kind: 'interrupt'})
      })
      .then(r => r.json())
      .then(j => {
        if(j && j.ok){
          const _n = Number(j.cancelled_tasks || 0);
          toast(`已停止：${_n} 个未完成子任务已收下，已完成部分保留，可点「继续编排」接着跑`);
        } else if(j && j.reason === 'no_active_run'){
          toast('已停止生成（该会话当前没有在跑的编排批次）');
        }
      })
      .catch(()=>{});      // 中断请求失败不阻断本地收口（最坏退回旧行为）
    }
  } catch(e) { /* 同上：不阻断 */ }
  if(_streamAbort) _streamAbort.abort();
  toast('已请求停止生成（已完成部分会保留，可点「继续编排」接着跑）');
}
function finalizeStopped(aiBox){
  if(!aiBox) aiBox = document.getElementById('stream-ai');
  if(!aiBox) return;
  // 2026-09-25：先**落库固化**再渲染（原来只写 DOM，刷新即丢）。已固化过则复用其 id，避免重复写。
  return commitPartialStream(aiBox).then(()=>_finalizeStoppedRender(aiBox));
}
// 固化「已生成但未完成」的内容 → POST /messages/partial，返回 message_id 记在元素上（幂等）
async function commitPartialStream(box){
  try{
    const el = box || document.getElementById('stream-ai');
    if(!el) return null;
    // 2026-09-29：固化目标 = 流式现场归属的会话（el.dataset.convId），而非当前选中会话——
    // 流式中切到其他会话再发新消息时，currentConvId 已变，按 currentConvId 固化会把 A 会话内容写进 B。
    const _cid = el.dataset.convId || currentConvId;
    if(!_cid) return null;
    if(el.dataset.committedId) return el.dataset.committedId;   // 已固化过 → 幂等复用
    // 2026-10-10（米爸实测「一轮对话输出两次内容」·会话 9336）：
    //   后端 done 事件已带真实 message_id，且 `finishStream` 已用它重渲染 ⇒ 本轮**已落库**。
    //   若此后仍走固化请求，就是同一段内容被写第二遍（库里 2 条 assistant 正文逐字相同）。
    //   ⇒ 记下 done 的 message_id 并在这里短路，**从源头不再发这次请求**。
    //   注：后端 `/messages/partial` 已有内容去重兜底，但那是最后一道防线，
    //   正常完成后再发一次固化请求本身就是多余的往返 + 无谓的 200ms+ 写事务。
    if(el.dataset.finalizedId) return el.dataset.finalizedId;   // 本轮已正常落库 → 不重复固化
    const bodyEl = el.querySelector('.body');
    const content = bodyEl ? (bodyEl.textContent || '').trim() : '';
    // 极短内容（只有 typing 占位/一两个字）没有固化价值，避免制造噪声消息
    if(content.length < 4) return null;
    const r = await api(`/api/conversations/${_cid}/messages/partial`,
                        {method:'POST', body: JSON.stringify({content, stopped:true})});
    if(r && r.id){ el.dataset.committedId = String(r.id); return r.id; }
  }catch(e){ /* 固化失败不阻断 UI 收口：DOM 里仍有已生成内容 */ }
  return null;
}
function _finalizeStoppedRender(aiBox){
  if(!aiBox) aiBox = document.getElementById('stream-ai');
  if(!aiBox) return;
  // ★ 2026-10-16 Q2：若此盒正被「澄清改选 / 澄清续答」复用接管（reuseForPick 标记），
  //   不得用 outerHTML 替换它 —— 否则续答盒刚建好就被旧流 cleanup 摘掉。直接返回即可
  //   （状态已被接管的新流掌管，旧流的全局状态改动也已在 pumpStream catch 里跳过）。
  if(aiBox.dataset && aiBox.dataset.reuseForPick === '1'){
    try{ aiBox.dataset.reuseForPick = ''; }catch(_e){}   // 接管流稍后会清，这里清也无害
    return;
  }
  // 2026-09-29（用户五轮反馈5）：终止后所有 run 态环节（含"执行中…/思考中…/执行中 1/1"）
  //  先翻转为「⏹ 已终止」并重渲染，再快照进历史 —— 此前只折叠不翻转，残影永远停在执行中。
  if(typeof procMarkStopped === 'function') procMarkStopped();
  const bodyEl = aiBox.querySelector('.body');
  let content = '';
  if(bodyEl){
    const t = bodyEl.querySelector('.typing'); if(t) t.remove();
    bodyEl.classList.remove('streaming');
    content = bodyEl.textContent || '';
  }
  // 执行过程块统一收起，随消息保留
  const pb = document.getElementById('proc-box');
  if(pb) pb.querySelectorAll('.proc-block').forEach(b=>b.classList.add('collapsed'));
  const procHtml = _procBoxBlocksHtml(pb);   // 2026-09-26：只取环节块，剔除 live 收口条（防「执行中」残影）
  // V2.6：停止也走统一收口（汇总条 + 默认收起时间线）
  let histProcHtml = procHtml;
  try{
    _procS.done = true; _procS.endTime = Date.now(); _procS.summaryCollapsed = true;
    stopElapsedTimer();
    const _failN = _procS.timeline.filter(x=>x.status==='failed').length;
    const _sumTxt = `⏹ 已停止 · ${_procS.timeline.length} 环节 · 耗时 ${_procElapsedText()}${_failN?` · ${_failN} 失败`:''}`;
    histProcHtml = procHtml
      ? `<div class="hist-proc"><div class="proc-summary collapsed" onclick="histProcToggle(this)">${_sumTxt}${_procDetailBtn()}<span class="chev">▾</span></div><div class="proc-timeline">${procHtml}</div></div>`
      : '';
  }catch(e){}
  try{
    // 2026-09-25：用**已固化的真实 message id**渲染（原来恒为 0 → 刷新后与库不一致，
    //   且「执行详情」按钮拿不到对应消息）。已附带「⏹ 已停止」后缀，就不再重复补本地提示。
    const _mid = Number(aiBox.dataset.committedId) || 0;
    aiBox.outerHTML = renderMessage({role:'assistant', content, msg_type:'text',
                                     card_data:'{}', id:_mid, process_html: histProcHtml});
    if(!_mid){
      // 未固化（内容过短或落库失败）时才补本地标记，避免刷新前后不一致
      const allMsgs = document.querySelectorAll('#chat-area .msg.ai');
      const newBox = allMsgs.length ? allMsgs[allMsgs.length - 1] : null;
      const nbody = newBox ? newBox.querySelector('.body') : null;
      if(nbody) nbody.insertAdjacentHTML('beforeend', `<div class="stop-note">⏹ 已停止生成（以上为已生成内容）</div>`);
    }
  }catch(e){}
  const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = '已停止生成';
  const area = document.getElementById('chat-area'); if(area) area.scrollTop = area.scrollHeight;
}
// ── 快捷指定能力：Agent / Skill / 工作流（基于已定义数据选择）──
