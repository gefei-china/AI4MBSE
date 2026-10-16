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
      .concat(tags.map(t => ({ key:t.name, icon:'📄', name:t.name, desc:(t.status||''), tag:'文档' })))
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
async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if(!msg) return;
  // ★ 2026-10-16（米爸决策 R1）：**必须先选主 Agent 才能发起会话**。
  //   原实现允许 team=''（下拉有空串"自动"项）⇒ 后端意图识别自由发挥 ⇒ 执行主体随机
  //   （实测会话 9372 掉到「通用问答Agent」，该 Agent 没绑 MCP ⇒ 工程列表查询必失败）。
  //   判据用**下拉当前值**而非 localStorage：后者是全局记忆，可能与当前页面状态不同步。
  //   ⚠️ 放在最前面（建会话之前）：否则会先造一个空会话再被拦下，留下垃圾会话。
  if(!requireMainAgent()) return;
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
  //
  // ★ G-1（2026-10-15 米爸报「携带多个产物文件建模仍异常：消息不上屏、按钮一直进行中」）：
  //   **带附件时绝不能走 clarify 分支** —— 原实现对任何输入都先判 _pendingClarify：
  //     ① clarify-answer 只发 {free_text: msg}，**附件被静默丢弃**（后端该接口本就不收附件）；
  //     ② 该分支**不渲染用户消息** ⇒ "对话流中没有展示用户发送的信息"；
  //     ③ 任一失败 → toast + return ⇒ **输入框不清空**（截图输入框还留着全文）、消息丢失；
  //     ④ 按钮红是上一轮的流/挂起态，用户看起来就是"点了发送但什么都没发生"。
  //   带附件的输入是**新的建模请求**（附件是建模输入的一部分），不是"澄清补充"
  //   ⇒ 直接走正常发送；澄清卡保持挂起，由用户在卡上作答或随本轮 done 清除。
  if(window._pendingClarify && currentConvId && !(pendingAttachments && pendingAttachments.length)){
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
      // ★ G-1：后端返回"无待澄清"等业务失败 ⇒ **本地挂起态已与后端不同步**，
      //   清掉本地状态并**落入正常发送** —— 绝不 return 吞掉用户输入。
      //   （后端 400 原文："当前无待澄清问题（可能已作答或已过期）"）
      setPendingClarify(null);
      toast((r && r.error) || '澄清状态已过期，按新消息发送');
    }catch(e){
      // ★ G-1：接口异常同理 —— 清挂起、继续正常发送，不吞输入。
      setPendingClarify(null);
      toast('澄清提交失败，已按新消息发送');
    }
  }
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
  if(_streaming){
    const _el = document.getElementById('stream-ai');
    const _fc = (_el && _el.dataset && _el.dataset.convId) || currentConvId;
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
            area0.innerHTML += renderMessage({role:'user', content:esc(msg)});
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
  // 走到这里只有两种情况：① 非流式（上一轮已结束，abort 是无害的空操作）
  //   ② follow-up 投递失败走回退（上面已 abort）—— 保持原有的无条件 abort 语义。
  if(_streamAbort) _streamAbort.abort();
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
//   ★ 2026-10-16 Q2：抽出此函数是为让"澄清改选 / 澄清续答"能在同一输出框内重跑，
//     不再新起用户气泡 + 新 AI 气泡（米爸 Q2：确认后应在原输出流续接后续环节）。
async function pumpStream(aiBox, msg, atts){
  const myAbort = new AbortController();
  _streamAbort = myAbort;
  _streaming = true; _stoppedManually = false;
  showStopBtn(true);
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
        handleSSE(buf.slice(0, idx));
        buf = buf.slice(idx+2);
      }
    }
    _forceFlushTokens();   // P0-3：流结束前清空合帧缓冲，防末帧丢失
    stickBottom();
    showStopBtn(false);
    _streaming = false;
    if(_streamAbort === myAbort) _streamAbort = null;
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
      finalizeStopped(aiBox);
      return;
    }
    const bodyEl = aiBox.querySelector('.body');
    const _typ = bodyEl ? bodyEl.querySelector('.typing') : null; if(_typ) _typ.remove();
    if(bodyEl) bodyEl.classList.remove('streaming');
    if(typeof commitPartialStream === 'function') commitPartialStream(aiBox);   // 同步段先抓正文再追加错误条
    if(typeof procMarkStopped === 'function') procMarkStopped();   // 五轮反馈5：环节卡全部翻转为已终止
    try{ aiBox.dataset.reuseForPick = ''; }catch(_e){}   // 异常收口：清防替换标记，避免下次 done 误跳过
    const errNote = document.createElement('div');
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
