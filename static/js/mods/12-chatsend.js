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
      .concat(tags.map(t => ({ key:t.name, icon:'📄', name:t.name, desc:(t.branch||'') + ' · ' + (t.status||''), tag:'文档' })))
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
let _stoppedManually = false;  // 手动停止 vs 澄清改选自动中止
// 从首条消息生成会话标题（草稿首次提交时使用）
function draftTitle(msg){
  const clean = String(msg||'').replace(/[#*`>\[\](){}«»"':]/g,' ').replace(/\s+/g,' ').trim();
  if(clean) return clean.length>20 ? clean.slice(0,18)+'…' : clean;
  return '任务 '+(Date.now()%1000);
}
async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if(!msg) return;
  // 2026-09-04 v3：草稿态首次提交时创建会话（未提交前左侧列表不出现空会话）
  // 2026-09-24：若由「项目 → ＋ 新建任务」发起，带走该项目归属（window._pendingProjectId），
  //   创建成功后立刻刷新项目组（任务数 +1），并清除暂存避免下一次误归属。
  if(!currentConvId){
    try{
      const _body = {title:draftTitle(msg)};
      const _pid = window._pendingProjectId || '';
      if(_pid) _body.project_id = _pid;
      const r = await api('/api/conversations', {method:'POST', body:JSON.stringify(_body)});
      currentConvId = r.id;
      _isDraft = false;
      loadConversations();
      if(_pid){
        window._pendingProjectId = '';
        if(typeof renderGnavProjects === 'function') renderGnavProjects();
      }
      const st = document.getElementById('chat-status');
      if(st) st.textContent = '';
      try{ localStorage.removeItem('mbse_draft_input'); }catch(e){}
    }catch(e){ toast('创建会话失败：'+(e && (e.message||e)) ); return; }
  }
  // 2026-09-25：待澄清状态下，主输入框内容即"澄清补充"——走 clarify-answer（会清空挂起），
  // 不再当成普通新消息。此前两条路都不通：挂起永不清除（AI 反复追问）、用户也看不到"在哪作答"。
  if(window._pendingClarify && currentConvId){
    try{
      const r = await api(`/api/conversations/${currentConvId}/clarify-answer`,
                          {method:'POST', body: JSON.stringify({free_text: msg})});
      if(r && r.resume_text){
        input.value = '';
        _chatInputAutoGrow();   // 2026-09-29：清空后高度回缩
        try{ localStorage.removeItem('mbse_draft_input'); }catch(e){}
        toast('已作为澄清补充提交，正在继续…');
        setPendingClarify(null);
        sendResume(r.resume_text);
        return;
      }
      toast((r && r.error) || '澄清提交失败');
      return;
    }catch(e){ toast('澄清提交失败：' + (e.message || '')); return; }
  }
  // P0-1 澄清改选重发：中止上一轮未读完的流（防旧 token 追加到新消息容器）
  // 2026-09-25：中止前先把上一轮**已生成内容固化落库**——否则"提交新消息"= 上一轮输出白丢
  if(_streaming) await commitPartialStream(document.getElementById('stream-ai'));
  if(_streamAbort) _streamAbort.abort();
  const myAbort = new AbortController();
  _streamAbort = myAbort;
  _streaming = true; _stoppedManually = false;
  showStopBtn(true);
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
  area.innerHTML += renderMessage({role:'user', content:esc(msg), attachments:atts});
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
  _bindStickDetach();   // 首次发送时绑定"用户上滑即脱离跟读"的监听（幂等）
  const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = '正在识别意图并调度 Agent…';
  resetPipeline();  // 意图识别前：清空旧流水线，展示执行中占位
  try {
    const resp = await fetch(`/api/conversations/${currentConvId}/chat/stream`, {
      method:'POST',
      signal: myAbort.signal,
      headers: (() => { const h = {'Content-Type':'application/json'};
        const uid = localStorage.getItem('mbse_user_id'); if(uid) h['X-User-Id'] = uid;  // P2：登录态透传 → 用户上下文/Skill角色过滤
        return h; })(),
      body: JSON.stringify({
        message:msg, attachments:atts,
        scope_ids: (typeof activeScopes!=='undefined'? activeScopes:[]).map(s=>s.id),   // 建模范围（可多选，全局）硬锁
        branch: getCurrentBranch(),   // AI 建模输出侧工作分支（默认 dev；release 只读不可作工作分支）
        forced_intent: document.getElementById('quick-agent')?.value || '',
        skill_name: document.getElementById('quick-skill')?.value || '',
        team: document.getElementById('quick-team')?.value || '',   // 团队模式：AI 会话页「工作流」下拉 → 主 Agent 团队负责人统一调度
        provider_id: Number(document.getElementById('llm-providers')?.value) || undefined,
      }),
    });
    if(!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while(true) {
      const {done, value} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) !== -1) {
        handleSSE(buf.slice(0, idx));
        buf = buf.slice(idx+2);
      }
    }
    _forceFlushTokens();   // P0-3：流结束前清空合帧缓冲，防末帧丢失
    // 2026-09-29：流结束时改为贴底才跟随 —— 用户上滑在阅读上文时不被打断；
    //   仍在底部者自动对齐到最终内容。
    stickBottom();
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
  } catch(e) {
    _forceFlushTokens();
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
    if(e && e.name === 'AbortError') {   // P1-1 停止/换发：将已生成内容固化为消息（保留执行过程）
      finalizeStopped(aiBox);
      return;
    }
    const bodyEl = aiBox.querySelector('.body');
    // 2026-09-29（用户五轮反馈4）：网络错误不再整体覆盖 body —— 覆盖即"已生成内容全丢"。
    //  现保留已流出的文字，错误说明追加在下方，并把已生成部分固化落库（刷新后仍在）。
    const _typ = bodyEl ? bodyEl.querySelector('.typing') : null; if(_typ) _typ.remove();
    if(bodyEl) bodyEl.classList.remove('streaming');
    if(typeof commitPartialStream === 'function') commitPartialStream(aiBox);   // 同步段先抓正文再追加错误条
    if(typeof procMarkStopped === 'function') procMarkStopped();   // 五轮反馈5：环节卡全部翻转为已终止
    const errNote = document.createElement('div');
    // 2026-09-29（用户三轮反馈）：失败横条补"系统现状 + 下一步"
    errNote.style.cssText = 'margin-top:8px;';
    errNote.innerHTML = `<div style="color:var(--red);font-size:11.5px;">调用失败：${esc(e.message)}</div>`
      + `<div style="color:var(--mut);font-size:10.5px;margin-top:4px;line-height:1.6;">`
      + `本次执行已终止，不再有后台任务在跑。上方已生成内容与执行轨迹<b>已保留</b>；`
      + `确认原因（如网络波动）后，<b>直接重新发送消息</b>即可从头执行。</div>`;
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
  ['quick-agent','quick-skill','quick-team'].forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.disabled = !!on;
  });
}
function stopStream(){
  if(!_streaming) return;
  _stoppedManually = true;
  if(_streamAbort) _streamAbort.abort();
  toast('已请求停止生成');
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
