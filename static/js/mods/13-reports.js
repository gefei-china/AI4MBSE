/* 报告：列表 / 查看 / 导出
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 6091-6433  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */
/* 顶栏「当前工程」标签（多工程 P0-2，2026-09-28）
 *
 * 展示口径：**本页**的当前工程（window._curProjectId，页面级），而不是平台默认值。
 * 多标签页并发时两者会不一致 —— 此时若按平台默认显示，用户会以为自己切了工程却没生效
 * （本页新建任务仍归本页工程）。平台默认值只在**本页尚未初始化**时用作初值。
 */
function renderProjectTag(){
  const tag = document.getElementById('ai-project-tag');
  if(!tag) return;
  const nm = window._curProjectName || '';
  const code = window._curProjectCode || '';
  let html;
  if(nm){
    tag.classList.remove('nomatch');
    // 2026-09-28：code 与 name 相同时不再重复显示（老数据 code 默认取 name，会显示成「测试项目（测试项目）」）
    const cc = (code && code !== nm) ? `（${esc(code)}）` : '';
    html = `<span class="dot"></span> 当前工程：${esc(nm)}${cc}`;
  }else{
    tag.classList.add('nomatch');
    html = `<span class="dot"></span> 暂未匹配工程`;
  }
  // 收敛入口：当前任务**未关联工程**（知识检索/问答类会话的合法状态），
  // 但一旦要写回建模工具等工程操作就需要归属 —— 给一个「归入」入口，而不是静默报错。
  if(currentConvId && window._curConvProjectId === ''){
    html += ` · <a class="pt-assign" href="javascript:void(0)" onclick="convAssignCurrent()"`
         +  ` title="本任务未关联工程。知识检索/问答无需工程；写回建模工具等工程操作需先归入工程">`
         +  `本任务未关联工程 · 归入</a>`;
  }
  tag.innerHTML = html;
}

// 把当前任务归入本页当前工程（收敛入口的点击动作）
async function convAssignCurrent(){
  if(!currentConvId){ toast('请先选中一个任务'); return; }
  const pid = (typeof curProjectId === 'function') ? curProjectId() : '';
  if(!pid){ toast('请先在左侧「项目」中点击一个项目作为当前工程'); return; }
  if(typeof assignConvToProject !== 'function'){ toast('归入功能未加载'); return; }
  await assignConvToProject(currentConvId, pid);
}

async function loadCurrentProject(){
  const tag = document.getElementById('ai-project-tag');
  if(!tag) return;
  try{
    const p = await api('/api/projects/default');
    // 本页首次初始化：以平台默认值为初值（此后只由本页切换 / URL 反查改变 —— 见 41-projects.js）
    if(window._curProjectId === undefined) window._curProjectId = (p && p.id) || '';
    const cid = window._curProjectId || '';
    let cur = (p && String(p.id||'') === cid) ? p : null;
    if(!cur && cid){
      try{ cur = await api('/api/projects/' + encodeURIComponent(cid)); }catch(e){ cur = null; }
    }
    window._curProjectName = (cur && cur.name) || '';
    window._curProjectCode = (cur && cur.code) || '';
    renderProjectTag();
  }catch(e){
    window._curProjectName = '';
    renderProjectTag();
  }
}
async function loadQuickBar(){
  try{
    const [agents, skills, teams] = await Promise.all([
      api('/api/studio/agents'), api('/api/studio/skills'), api('/api/studio/agent-teams')
    ]);
    _agentsCache = agents || _agentsCache;  // V3：供 @智能体弹窗/chip 复用
    _skillsCache = skills || _skillsCache;  // V3：供 /技能弹窗/chip 复用
    _teamsCache = teams || _teamsCache;     // 团队模式：主 Agent 团队（工作流下拉）数据源
    // ★ 2026-10-16（米爸决策 R1）：**quick-agent 下拉整体退役**，执行主体唯一由「主 Agent」
    //   （quick-team）承载。事故背景：quick-agent 是 display:none 的隐藏 select，却仍被
    //   sendChat 无条件当作 forced_intent 发送，且从 localStorage.mbse_last_agent 恢复历史值
    //   ⇒ 残留的 requirement_analysis 静默压过用户所选的主 Agent（会话 9367 / 消息 4029 实测：
    //   选「智源对接agent」却以「需求分析Agent」作答）。
    //   现在不再填充、不再记录、不再发送；下方先清理历史残留键，避免旧值复活。
    try{ localStorage.removeItem('mbse_last_agent'); }catch(e){}
    const sSel = document.getElementById('quick-skill');
    if(sSel && skills && skills.length){
      sSel.innerHTML = '<option value="">自动匹配</option>' + skills.map(s=>`<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('');
    }
    const tSel = document.getElementById('quick-team');
    if(tSel){
      if(teams && teams.length){
        // ★ 2026-10-16（米爸决策 R1 第二步）：**取消「自动（不指定团队）」选项**。
        //   原实现有空串 option，选中后 team='' ⇒ 后端意图识别自由发挥 ⇒ 执行主体随机
        //   （实测会话 9372 就这么掉到「通用问答Agent」，MCP 工具全不可用）。
        //   米爸口径："必须选择主 agent 才能发起会话" ⇒ 下拉**只列主 Agent**，无空串项；
        //   默认值见下方（会话绑定 > localStorage 记录 > 排序第一）。
        tSel.innerHTML = teams.map(t=>`<option value="${esc(t.name)}">👑 ${esc(t.display_name||t.name)}${t.team_count?`（${t.team_count}人）`:''}</option>`).join('');
        tSel.title = '选择主 Agent（执行主体）：每次会话都必须指定；其负责意图识别/任务拆分/计划制定/任务分派/内容整合输出，也可直接用自己绑定的技能/工具/MCP 完成任务';
        // 默认值三级优先：
        //   ① 当前会话已绑定的主 Agent（window._convMainAgent，由 loadConversations 详情带出）
        //      —— 保证"这个会话一直由同一个主 Agent 执行"，换标签页/刷新都不漂移；
        //   ② localStorage 上次选择（跨会话的记忆）；
        //   ③ 排序第一的团队（/api/studio/agent-teams 按 agents.id 升序）。
        // ⚠️ 不再认空串记录：用户显式选"自动"这条路已被产品上移除，
        //   历史 localStorage 里的 '' 一律忽略，否则会把下拉打回"无主 Agent"状态。
        let lastTeam = '';
        try{ lastTeam = localStorage.getItem('mbse_last_team') || ''; }catch(e){}
        const convBound = (typeof window._convMainAgent==='string') ? window._convMainAgent : '';
        const teamNames = teams.map(t=>t.name);
        const pick = (convBound && teamNames.includes(convBound)) ? convBound
                   : ((lastTeam && teamNames.includes(lastTeam)) ? lastTeam : (teamNames[0] || ''));
        tSel.value = pick;
        if(pick) window._convMainAgent = pick;   // 供 _requireMainAgent() 判定
        if(!tSel.dataset.lastBind){
          tSel.dataset.lastBind = '1';
          tSel.addEventListener('change', ()=>{
            try{ localStorage.setItem('mbse_last_team', tSel.value || ''); }catch(e){}
            if(typeof renderChips === 'function') renderChips();
          });
        }
      } else {
        // 无任何主 Agent：给一个禁用的占位（不是空串 option —— 空串会被当成"未选择"放行）
        tSel.innerHTML = '<option value="" disabled selected>暂无主 Agent</option>';
        tSel.title = '暂无主 Agent，请到「Agent 管理」创建主 Agent 并启用';
      }
    }
  }catch(e){ /* 候选加载失败静默：快捷条仍可用（可手输） */ }
}
let _chatProvidersLoaded = false;
let _chatProvidersCache = [];   // 2026-09-04 缓存模型列表，供附件上传时判断当前模型能力（vision 等）
async function loadChatProviders(){
  try{
    const providers = await api('/api/llm/providers');
    _chatProvidersCache = providers || [];
    const sel = document.getElementById('llm-providers');
    const chatOnes = (providers||[]).filter(p=>(p.model_type==='chat'||p.model_type==='multimodal') && p.status!=='disabled');  // 对话/多模态模型：向量模型不进选择器
    if(sel){
      if(chatOnes.length){
        const def = chatOnes.find(p=>p.is_default) || chatOnes[0];
        sel.innerHTML = '<option value="">默认模型</option>' + chatOnes.map(p=>`<option value="${p.id}" ${p.id===def.id?'selected':''}>${esc((p.name||'')+' · '+(p.model_name||'-'))}</option>`).join('');
      } else {
        sel.innerHTML = '<option value="">未配置模型</option>';
      }
      _chatProvidersLoaded = true;
    }
    renderLLMPicker();
    // 会话就绪后补拉一次该会话的参数（新建会话时 openConv 还没跑，此时才拿到 id）
    try{ if(typeof loadConvLLMSettings === 'function') loadConvLLMSettings(_currentConvId()); }catch(e){}
  }catch(e){ /* 加载失败静默：保留「默认模型」选项 */ }
}

// ── 会话入口模型切换面板（2026-10-31）──────────────────────────────────────
// 设计要点：
//   ① **真值仍在隐藏的 #llm-providers**。面板只负责展示与写回，避免出现
//      "面板显示 A、实际发给后端 B" 的双事实源（这类错位最难查，历史已踩过）。
//   ② 每个模型显示：描述 / 能力标签 / 上下文档位 / 思考强度 / 默认标记，
//      并带 ⚙ 直达「模型配置」弹窗 —— 满足"在会话入口逐个模型分别设置"。
//   ③ 上下文用**档位化**展示（1M/600K/300K…），因为 1048576 这种数字
//      在界面上没人能一眼判断大小；档位名与模型配置页的下拉同源（fmtCtxTier）。
const CTX_TIERS = [
  {v:1048576, label:'1M'},
  {v:614400,  label:'600K'},
  {v:307200,  label:'300K'},
  {v:262144,  label:'256K'},
  {v:131072,  label:'128K'},
  {v:65536,   label:'64K'},
  {v:32768,   label:'32K'},
  {v:16384,   label:'16K'},
  {v:8192,    label:'8K'}
];
function fmtCtxTier(n){
  n = Number(n)||0;
  if(!n) return '—';
  const hit = CTX_TIERS.find(t=>t.v===n);
  if(hit) return hit.label;
  // 非标准档位：按 K 取整展示，便于一眼比较（如 112000 → 109K）
  return (n>=1024 ? Math.round(n/1024)+'K' : n+'');
}
function _thinkLevelOf(p){
  const mp = (p && p.model_params) || {};
  return mp.thinking_level || mp.thinking_mode || '模型默认';
}
function _providerLabelOf(id){
  const sel = document.getElementById('llm-providers');
  if(!sel) return '默认模型';
  const opt = Array.from(sel.options).find(o=>String(o.value)===String(id||''));
  return opt ? opt.textContent : '默认模型';
}
function syncLLMPickerLabel(){
  const el = document.getElementById('llm-pick-label');
  const sel = document.getElementById('llm-providers');
  if(!el || !sel) return;
  const cur = sel.value;
  if(!cur){ el.textContent = '默认模型'; return; }
  const p = (_chatProvidersCache||[]).find(x=>String(x.id)===String(cur));
  el.textContent = p ? (p.name||p.model_name||'模型') : _providerLabelOf(cur);
}
function renderLLMPicker(){
  const panel = document.getElementById('llm-pick-panel');
  if(!panel) return;
  syncLLMPickerLabel();
  const sel = document.getElementById('llm-providers');
  const cur = sel ? sel.value : '';
  const list = (_chatProvidersCache||[]).filter(p=>(p.model_type==='chat'||p.model_type==='multimodal') && p.status!=='disabled');
  if(!list.length){
    panel.innerHTML = '<div class="llm-pick-empty">当前没有可用的对话模型，请到「能力中心 → 模型配置」添加</div>';
    return;
  }

  // ★★ 2026-11-01（米爸）：设置改为**二级树** —— 挂在每个模型行后面，
  //   不再是列表上方一块公共区。理由：
  //     ① 语义上设置就是「该模型」的，放在列表上方会被误读成"整个会话共用"；
  //     ② 模型越多，公共区离具体模型越远，用户要来回对照。
  //   交互：每行右侧「⚙ 设置」按钮 → 就地展开该行的二级面板（不跳页、不改系统默认）。
  //   ⚠️ 展开态存在 `_expandedPid`，render 后要保留：保存一次就收起会让人以为没生效。
  const rows = list.map(p=>{
    const tags = (p.tags||[]).filter(Boolean);
    const lv = _thinkLevelOf(p);
    const lvCls = lv==='关闭' ? 'think' : (lv==='模型默认' ? '' : 'think');
    const ov = _convOverridesForModel(p.id);
    const hasCustom = !!(ov.thinking_level || ov.context_window);
    const expanded = String(_expandedPid) === String(p.id);
    const cap = _capabilityOf(p);
    return `<div class="llm-pick-item">
      <div class="llm-pick-row ${String(p.id)===String(cur)?'on':''}" onclick="pickLLMModel(${p.id})">
        <div class="lp-main">
          <div class="lp-name">${esc(p.name||'-')}
            ${p.is_default?'<span class="llm-pick-tag def">默认</span>':''}
            <span style="font-weight:400;font-size:11.5px;color:var(--mut);">${esc(p.model_name||'')}</span>
            ${hasCustom?'<span class="llm-pick-tag custom" title="该模型在本会话有独立设置">本会话已设</span>':''}
          </div>
          ${p.description ? `<div class="lp-desc">${esc(p.description)}</div>` : ''}
          <div class="lp-meta">
            <span class="llm-pick-tag ctx" title="上下文窗口（输入）">🗂 ${fmtCtxTier((ov.context_window)||p.context_window)}</span>
            <span class="llm-pick-tag ${lvCls}" title="思考强度">🧠 思考：${esc(lv)}</span>
            ${tags.slice(0,4).map(t=>`<span class="llm-pick-tag">${esc(t)}</span>`).join('')}
          </div>
        </div>
        <button type="button" class="lp-set ${expanded?'open':''}"
                title="展开本会话 · 该模型的参数设置（思考强度 / 上下文）"
                onclick="event.stopPropagation();toggleModelSettings(${p.id})">⚙ 设置</button>
      </div>
      ${expanded?`<div class="lp-sub">${buildModelSettingsHtml(p)}</div>`:''}
    </div>`;
  }).join('');

  panel.innerHTML = `<div class="llm-pick-head"><span>选择对话模型 · ${list.length} 个可用</span>
      <span style="cursor:pointer;color:var(--mut);font-weight:400;"
            title="这里是「会话 + 该模型」的设置，不回写系统默认。系统默认（所有会话共用）在「能力中心 → 模型配置」里改"
            onclick="event.stopPropagation();gotoModelConfig();">改系统默认 ↗</span></div>`
      + `<div class="llm-pick-list">${rows}</div>`;
}

// 展开/收起某个模型的二级设置。只允许一个展开：同时展开多个会把面板撑很高，
// 也会让"我在改哪个模型"变得不清楚（这是二级树的常见问题，故限单选）。
function toggleModelSettings(pid){
  _expandedPid = (String(_expandedPid) === String(pid)) ? null : pid;
  renderLLMPicker();
}

// 会话参数区：与模型列表用分隔线+底色区分，明确"这是本会话的，不是模型的"
// ★ 2026-10-31（米爸要求）：**按当前模型的真实能力**决定能不能设。
//   不支持的 → 屏蔽下拉并给出原因；部分支持的 → 只列真有效的档位 + 说明。
//   能力数据来自后端 `capability`（唯一真源），前端不按模型名猜。
function _currentModel(){
  const sel = document.getElementById('llm-providers');
  const pid = sel ? sel.value : '';
  return ((_chatProvidersCache||[]).find(x=>String(x.id)===String(pid))) || null;
}
// 能力判定：传模型对象（二级树里要判断"那一行"的模型，不是当前选中的）
function _capabilityOf(p){
  if(p && p.capability) return p.capability;
  // 取不到（老接口/缓存未就绪）⇒ 当"可设"处理，避免误屏蔽；但不编造档位
  return {supported:true, level:'full', levels:['关闭','低','中','高'],
          default_level:'模型默认', reason:'', context:{supported:true, reason:''}};
}
function _capabilityOfCurrentModel(){ return _capabilityOf(_currentModel()); }
// 读**指定模型**的会话桶（二级树按行取，不能用"当前选中"代替）
function _convOverridesForModel(pid){
  const bm = (_convLlm && _convLlm.byModel) || {};
  return bm[String(pid)] || bm['__legacy__'] || {};
}

// ── 二级区：某个模型在本会话的参数设置 ──────────────────────────────────────
// ★★ 2026-11-01（米爸）：从"列表上方公共块"改为"挂在每个模型行后面的二级面板"。
//   ⚠️ 数据链路关键点：所有控件的 onchange **必须显式传该行的 p.id**。
//      若沿用 `_currentProviderId()`，就会出现「在 A 行展开设置，但当前选中的是 B
//      ⇒ 存进了 B 的桶」—— 界面看着对、数据落错桶，是最难查的那类错。
function buildModelSettingsHtml(p){
  const pid = p.id;
  const ov = _convOverridesForModel(pid);
  const cap = _capabilityOf(p);
  const ctxCap = cap.context || {supported:true, reason:''};
  // 「系统默认」= **该模型**在 llm_providers 里的值（不是会话绑定模型的值）
  const sd = { thinking_level: _thinkLevelOf(p), context_window: p.context_window };
  const lv = ov.thinking_level || '';
  const ctx = ov.context_window || '';
  const hasEffective = !!(lv || ctx || ov.max_tokens || (ov.temperature != null));

  // 档位集合 = 「该模型配置的控制粒度」∩「该模型客观能力」
  const ctl = (p.model_params && p.model_params.thinking_control) || 'tier';
  let levels;
  if(ctl === 'toggle'){
    levels = ['开启', '关闭'];          // 两态永远有效，不受 capability.levels 影响
  }else{
    levels = (cap.levels && cap.levels.length) ? cap.levels : ['关闭','低','中','高'];
  }
  const thinkOpt = (v, label) => `<option value="${v}" ${String(v)===String(lv)?'selected':''}>${label}</option>`;
  let thinkEl;
  if(!cap.supported){
    thinkEl = `<span class="llm-turn-off" title="${esc(cap.reason||'')}">不可设置</span>`;
  }else{
    thinkEl = `<select onchange="saveConvLLM('thinking_level', this.value, ${pid})"
        title="${ctl==='toggle'?'该模型在模型配置里选择了「按开关」控制，故只提供 开启/关闭':'该模型按挡位控制'}">
        ${thinkOpt('', '跟随系统默认')}${levels.map(v=>thinkOpt(v, v)).join('')}</select>`
      + `<span class="llm-turn-def">${ctl==='toggle'?'按开关':'按挡位'}</span>`;
  }

  const tiers = [['','跟随系统默认'],['1048576','1M'],['614400','600K'],['307200','300K'],
                 ['262144','256K'],['131072','128K'],['65536','64K'],['32768','32K'],
                 ['16384','16K'],['8192','8K']];
  let ctxEl;
  if(!ctxCap.supported){
    ctxEl = `<span class="llm-turn-off" title="${esc(ctxCap.reason||'')}">不可设置</span>`;
  }else{
    ctxEl = `<select onchange="saveConvLLM('context_window', this.value, ${pid})">${
      tiers.map(([v,l])=>`<option value="${v}" ${String(v)===String(ctx)?'selected':''}>${l}</option>`).join('')
    }</select>`;
  }

  const why = [];
  if(!cap.supported && cap.reason) why.push('🧠 ' + cap.reason);
  else if(cap.level === 'partial' && cap.reason) why.push('🧠 ' + cap.reason);
  else if(cap.level === 'unverified' && cap.reason) why.push('⚠ ' + cap.reason);
  if(!ctxCap.supported && ctxCap.reason) why.push('🗂 ' + ctxCap.reason);

  return `<div class="llm-turn-box"${hasEffective?' style="border-color:var(--amb);"':''}>
    <div class="llm-turn-head">🗂 本会话 · ${esc(p.name||'模型')} 的参数
      <span class="llm-turn-note">只影响「本会话 + 该模型」· 不回写模型配置</span></div>
    <div class="llm-turn-row">
      <label>思考强度 <span class="llm-turn-def">${esc(String(sd.thinking_level||'—'))}</span></label>
      ${thinkEl}
      <label>上下文 <span class="llm-turn-def">${esc(fmtCtxTier(sd.context_window))}</span></label>
      ${ctxEl}
    </div>
    ${why.length ? `<div class="llm-turn-why">${why.map(esc).join('<br>')}</div>` : ''}
    ${hasEffective
      ? `<div class="llm-turn-reset"><span onclick="clearConvLLM(${pid})">✕ 该模型恢复系统默认（仅本会话）</span></div>`
      : '<div class="llm-turn-def" style="margin-top:6px;">当前沿用系统默认；改动只存到「本会话 + 该模型」</div>'}
  </div>`;
}

// ── 会话 × 模型 维度的参数（2026-11-01 再修正）──────────────────────────────
// ★★ 米爸澄清：「会话中的设置是按照（会话+模型）维度来设置的，
//    不是一个会话中所有模型按照同一套设置控制」。
// 三层语义（别自行合并）：
//   · **模型维度 = 系统默认**：`llm_providers`，所有会话共用，在「模型配置」页改；
//   · **会话 × 模型**：`conversations.llm_overrides.by_model[<providerId>]`，
//     只影响「本会话 + 该模型」，**绝不回写模型配置**；
//   · 切到另一个模型 ⇒ 用的是那个模型自己的桶（或系统默认），**不继承**上一个模型的值。
// 存储结构：{"by_model": {"71": {...}, "104": {...}}}
let _convLlm = { byModel: {}, systemDefault: {}, providerId: null, loadedFor: null };
// 二级树展开态：存"哪个模型被展开"。render 后要保留（保存一次就收起会让人以为没生效）。
let _expandedPid = null;

function _currentProviderId(){
  const sel = document.getElementById('llm-providers');
  return sel && sel.value ? Number(sel.value) : null;
}

async function loadConvLLMSettings(convId, force){
  convId = convId || _currentConvId();
  const pid = _currentProviderId();
  if(!convId) { _convLlm = { byModel:{}, systemDefault:{}, providerId:null, loadedFor:null }; return; }
  // 同一会话 + 同一模型 ⇒ 不重复拉（但 force=true 时强制刷新，保存后要用）
  if(!force && String(_convLlm.loadedFor) === String(convId)) return;
  try{
    const q = pid ? `?provider_id=${pid}` : '';
    const r = await api(`/api/conversations/${convId}/llm-settings${q}`);
    _convLlm = { byModel: (r && r.all_overrides) || {},
                 systemDefault: (r && r.system_default) || {},
                 providerId: (r && r.provider_id) || pid,
                 loadedFor: convId };
  }catch(e){
    _convLlm = { byModel:{}, systemDefault:{}, providerId: pid, loadedFor: convId };
  }
  renderLLMPicker();
}
function _currentConvId(){
  try{
    if(typeof currentConvId !== 'undefined' && currentConvId) return currentConvId;
    const box = document.getElementById('ai-box');
    return (box && box.dataset && box.dataset.convId) || null;
  }catch(e){ return null; }
}
async function saveConvLLM(key, value, pid){
  const convId = _currentConvId();
  // ★★ 数据链路关键（2026-11-01 二级树）：pid 必须由**调用方传入**（= 展开的那一行），
  //   不能回落到 `_currentProviderId()`。二级树允许「展开 A 的二级面板，但选中的是 B」，
  //   回落就会把 A 的设置存进 B 的桶 —— 界面看着对、数据落错桶，最难查那类。
  pid = (pid != null && pid !== '') ? Number(pid) : _currentProviderId();
  if(!convId){ toast('请先创建会话再设置（当前会话未就绪）'); return; }
  if(!pid){ toast('请先选择一个模型（设置按「会话 + 模型」保存）'); return; }
  const body = { provider_id: pid };
  body[key] = value;
  try{
    const r = await api(`/api/conversations/${convId}/llm-settings`, {method:'PUT', body:JSON.stringify(body)});
    const bm = Object.assign({}, (_convLlm && _convLlm.byModel) || {});
    if(r && r.overrides && Object.keys(r.overrides).length) bm[String(pid)] = r.overrides;
    else delete bm[String(pid)];
    _convLlm.byModel = bm;
    _convLlm.providerId = pid;
    _convLlm.loadedFor = convId;
    renderLLMPicker();
    toast(value === '' || value === 0 || value === null
          ? '已恢复为系统默认（本会话 + 该模型）'
          : '已保存到本会话的该模型（不影响模型配置）');
  }catch(e){
    toast('保存失败：' + (e && e.message ? e.message : '未知错误'));
  }
}
async function clearConvLLM(pid){
  const convId = _currentConvId();
  // 同上：必须用传进来的 pid（该行模型），不回落当前选中
  pid = (pid != null && pid !== '') ? Number(pid) : _currentProviderId();
  if(!convId){ toast('当前会话未就绪'); return; }
  if(!pid){ toast('请先选择模型'); return; }
  try{
    await api(`/api/conversations/${convId}/llm-settings`,
              {method:'PUT', body:JSON.stringify({provider_id: pid, clear: true})});
    const bm = Object.assign({}, (_convLlm && _convLlm.byModel) || {});
    delete bm[String(pid)];
    _convLlm.byModel = bm;
    renderLLMPicker();
    toast('该模型已恢复为系统默认（本会话）');
  }catch(e){ toast('清除失败'); }
}
// 从会话页跳到「能力中心 → 模型配置」
// 只调 switchStLibTab 是不够的：它仅切 subpage 显隐，若当前不在 studio 页
// 用户看到的仍是原页面（点了"没反应"）。必须先 go() 切主页面。
function gotoModelConfig(){
  closeLLMPicker();
  // go('studio','st-model') 是已有路由收口（等同于 go('modelcfg')），会做完导航高亮、
  // subpage 切换与 st-model 的 loadLLMProviders 钩子；不要自己拼两步（会漏钩子）。
  try{
    if(typeof go === 'function'){ go('studio', 'st-model'); return; }
  }catch(e){}
  try{ if(typeof switchStLibTab === 'function') switchStLibTab(null, 'st-model'); }catch(e){}
}
function toggleLLMPicker(e){
  if(e) e.stopPropagation();
  const panel = document.getElementById('llm-pick-panel');
  if(!panel) return;
  const willShow = panel.style.display === 'none' || !panel.style.display;
  // ★ 2026-11-01 真机实测修的 bug（静态门禁抓不到，必须量像素才发现）：
  //   这里原写 `display='block'`，而**内联样式优先级高于样式表** ⇒
  //   把 CSS 里的 `.llm-pick-panel{display:flex;flex-direction:column}` 覆盖掉了。
  //   后果：面板不是 flex 容器 ⇒ `flex:1;overflow-y:auto` 的模型列表**不收缩也不滚动**，
  //   实测 list 高 451px 溢出面板底边 111px（panel bottom=605, list bottom=716）。
  //   会话设置块虽然排在了前面（能看见），但整个面板的滚动约束失效。
  //   ⇒ 必须写 'flex'，与样式表同值。别图省事写 'block'。
  if(willShow){ renderLLMPicker(); panel.style.display = 'flex'; }
  else panel.style.display = 'none';
}
function closeLLMPicker(){
  const panel = document.getElementById('llm-pick-panel');
  if(panel) panel.style.display = 'none';
}
function pickLLMModel(id){
  const sel = document.getElementById('llm-providers');
  if(sel){ sel.value = String(id); }
  closeLLMPicker();
  syncLLMPickerLabel();
  renderLLMPicker();
  // ★ 2026-11-01（会话×模型）：切换模型后**必须重新拉该模型的设置**。
  //   为什么不能只靠本地 byModel 缓存：
  //     ① 会话里给的 by_model 只含"已设置过的"模型，未设置的模型要拿 system_default；
  //     ② 别的标签页/设备可能刚改过该模型的会话设置。
  //   force=true 绕开"同会话不重复拉"的短路。失败时静默（保留旧显示，不阻断切模型）。
  try{
    const cid = _currentConvId();
    if(cid && typeof loadConvLLMSettings === 'function') loadConvLLMSettings(cid, true);
  }catch(e){}
  // 通知监听者（若有）——沿用原生 select 的 change 语义，避免破坏既有外部依赖
  try{ sel && sel.dispatchEvent(new Event('change')); }catch(e){}
}
// ★ 2026-10-31：`openLLMConfigFromPicker()` **已删除**（米爸纠正）。
//   它把"会话里想调参数"变成"打开模型配置弹窗"⇒ 改的是系统默认，所有会话一起变。
//   保留一个空壳只会被后来人重新接上，所以直接不留。
//   会话内调参统一走 `saveConvLLM()`（写 conversations.llm_overrides）。
// 点击面板外关闭（capture=false，避免抢走面板内按钮的点击）
document.addEventListener('click', function(ev){
  const wrap = document.getElementById('llm-pick-wrap');
  const panel = document.getElementById('llm-pick-panel');
  if(!wrap || !panel || panel.style.display === 'none') return;
  if(!wrap.contains(ev.target)) closeLLMPicker();
});
// ── 报告下载（通用报告服务 → 多格式导出 md/docx/pdf）──
// 从当前激活预览 tab 导出报告（分节卡片视图上的导出按钮）
function reportTabExport(fmt){
  const tb = _previewTabs.find(t=>t.key===_previewActiveKey);
  if(!tb || !tb.meta || !tb.meta.sections){ toast('暂无报告可导出'); return; }
  window._lastReport = {title:tb.title||'报告', sections:tb.meta.sections, summary:tb.meta.summary||'', report_type:tb.meta.report_type||'analysis', meta:tb.meta.meta||{}};
  downloadReport(fmt);
}
async function downloadReport(fmt){
  const rep = window._lastReport;
  if(!rep){ toast('暂无可下载的报告'); return; }
  fmt = fmt || 'md';
  if(fmt === 'md'){
    let md = `# ${rep.title || '报告'}\n\n`;
    const meta = rep.meta || {};
    if(meta.doc_no) md += `| 文档编号 | 版本 | 日期 | 密级 | 编制 |\n| --- | --- | --- | --- | --- |\n| ${meta.doc_no||'-'} | ${meta.version||'-'} | ${meta.date||'-'} | ${meta.classification||'-'} | ${meta.author||'-'} |\n\n`;
    (rep.sections || []).forEach(s=>{
      md += `## ${s.heading}\n\n${s.body || ''}\n\n`;
      if(s.table) md += s.table + '\n\n';
    });
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (rep.title || 'report').replace(/[\\/:*?"<>|]/g, '_') + '.md';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 300);
    toast('报告已下载（Markdown）');
    return;
  }
  // docx / pdf：调后端报告导出服务生成二进制文件流
  try{
    const resp = await fetch('/api/report/export', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title: rep.title||'报告', sections: rep.sections||[], summary: rep.summary||'', report_type: rep.report_type||'', meta: rep.meta||{}, fmt})
    });
    if(!resp.ok){ const err = await resp.json().catch(()=>({})); toast('导出失败：' + (err.detail || resp.status)); return; }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (rep.title || 'report').replace(/[\\/:*?"<>|]/g, '_') + '.' + fmt;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 300);
    toast('报告已下载（' + fmt.toUpperCase() + '）');
  }catch(e){ toast('导出失败：' + (e.message||e)); }
}
// ── 报告（AI 建模报告统一归档 / 检索 / 导出）──
let _rpt = { page:1, size:15 };
async function loadReports() {
  const type = document.getElementById('rpt-type')?.value || '';
  const kw = (document.getElementById('rpt-search')?.value || '').trim();
  try{
    const rows = await api(`/api/reports?report_type=${encodeURIComponent(type)}&keyword=${encodeURIComponent(kw)}`);
    const el = document.getElementById('reports-table');
    const cnt = document.getElementById('rpt-count');
    if(cnt) cnt.textContent = rows.length ? `共 ${rows.length} 份` : '';
    const total = rows.length;
    const pages = Math.max(1, Math.ceil(total/_rpt.size));
    if(_rpt.page > pages) _rpt.page = pages;
    const items = rows.slice((_rpt.page-1)*_rpt.size, _rpt.page*_rpt.size);
    const typeCls = {impact:'b', review:'w', analysis:'ok', other:'g'};
    el.innerHTML = `<table class="t">
      <tr><th>标题</th><th>类型</th><th>来源</th><th>生成人</th><th>生成时间</th><th>状态</th><th>操作</th></tr>` +
      (items.length ? items.map(r=>`<tr>
        <td><b style="cursor:pointer;color:var(--blue-d);" onclick="viewReport(${r.id})">${esc(r.title)}</b></td>
        <td><span class="st ${typeCls[r.report_type]||'g'}">${esc(r.report_type_label||r.report_type)}</span></td>
        <td style="font-size:12.5px;color:var(--mut);">${esc(r.source||'-')}</td>
        <td style="font-size:12.5px;">${esc(r.created_by||'-')}</td>
        <td style="font-size:12.5px;color:var(--mut);">${esc((r.created_at||'').slice(0,16).replace('T',' '))}</td>
        <td><span class="st ${r.status==='final'?'ok':'g'}">${r.status==='final'?'已定稿':'草稿'}</span></td>
        <td style="white-space:nowrap;">
          <button class="btn sm ghost" onclick="viewReport(${r.id})">查看</button>
          <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'md')">⬇MD</button>
          <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'docx')">⬇Word</button>
          <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'pdf')">⬇PDF</button>
          ${r.status==='draft'?`<button class="btn sm ghost" onclick="finalizeReport(${r.id},'${esc(r.title).replace(/'/g,"\\'")}')">定稿</button>`:''}
          <button class="btn sm red" onclick="deleteStoredReport(${r.id})">删除</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="7" style="text-align:center;color:var(--mut);padding:18px;">暂无报告 — 在 AI 建模对话中生成「变更影响分析 / 预评审 / 模型分析」报告后将自动归档至此</td></tr>') +
    '</table>';
    renderPagerBar({
      el: document.getElementById('reports-pager'), total, page: _rpt.page, size: _rpt.size,
      onPage: p => { _rpt.page = p; loadReports(); },
      onSize: s => { _rpt.size = s; _rpt.page = 1; loadReports(); }
    });
  }catch(e){ toast('加载报告失败'); }
}
async function viewReport(id){
  try{
    const r = await api('/api/reports/' + id);
    if(r.error || r.detail){ toast(r.error || r.detail || '加载失败'); return; }
    const meta = {doc_no: r.doc_no||'', version:r.version||'', date:(r.created_at||'').slice(0,10), classification:'', author:r.created_by||''};
    let html = `<div class="note" style="margin-bottom:12px;">类型：${esc(r.report_type_label||r.report_type)} ｜ 来源：${esc(r.source||'-')} ｜ 生成人：${esc(r.created_by||'-')} ｜ ${esc((r.created_at||'').slice(0,16).replace('T',' '))} ｜ 状态：${r.status==='final'?'已定稿':'草稿'}</div>`;
    if(r.summary) html += `<div class="report-sec" style="margin-bottom:12px;"><div class="report-sec-h"><span class="report-sec-n">📌</span><span class="report-sec-t">摘要</span></div><div class="report-sec-b">${esc(r.summary)}</div></div>`;
    html += reportSectionsHtml(r.sections||[], meta);
    html += `<div style="display:flex;gap:6px;margin-top:14px;flex-wrap:wrap;">
      <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'md')">⬇ Markdown</button>
      <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'docx')">⬇ Word</button>
      <button class="btn sm ghost" onclick="exportStoredReport(${r.id},'pdf')">⬇ PDF</button>
      ${r.status==='draft'?`<button class="btn sm" onclick="finalizeReport(${r.id},'${esc(r.title).replace(/'/g,"\\'")}')">✅ 定稿</button>`:''}
    </div>`;
    openPanel('📄 ' + (r.title||'报告详情'), html);
  }catch(e){ toast('加载报告详情失败'); }
}
async function exportStoredReport(id, fmt){
  try{
    const resp = await fetch(`/api/reports/${id}/export?fmt=${fmt}`, {method:'POST'});
    if(!resp.ok){ const err = await resp.json().catch(()=>({})); toast('导出失败：' + (err.detail || resp.status)); return; }
    const blob = await resp.blob();
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
    const fname = m ? decodeURIComponent(m[1]) : ('report.' + fmt);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 300);
    toast('报告已导出（' + fmt.toUpperCase() + '）');
  }catch(e){ toast('导出失败：' + (e.message||e)); }
}
async function finalizeReport(id, title){
  if(!(await confirmDialog(`确认将报告「${title}」定稿？\n定稿后不可再编辑。`, {title:'报告定稿', okText:'定稿'}))) return;
  const r = await api('/api/reports/' + id);
  if(r.error || r.detail){ toast(r.error || r.detail || '加载失败'); return; }
  const res = await api('/api/reports/' + id, {method:'PUT', body:JSON.stringify({title:r.title, report_type:r.report_type, summary:r.summary||'', sections:r.sections||[], status:'final'})});
  if(res.error){ toast(res.error); return; }
  toast('已定稿'); await loadReports();
}
async function deleteStoredReport(id){
  if(!(await confirmDialog('确认删除该报告？\n删除后不可恢复。', {title:'删除报告', okText:'删除'}))) return;
  const r = await api('/api/reports/' + id, {method:'DELETE'});
  if(r.error){ toast(r.error); return; }
  toast('报告已删除'); await loadReports();
}
function finishStream(r, aiBox) {
  // P1-1 流式结束：恢复发送按钮
  showStopBtn(false); _streaming = false; _stoppedManually = false;
  // ★ 2026-10-16 Q2：本盒正被「澄清改选 / 澄清续答」复用接管（reuseForPick 标记）——
  //   这是上一轮被 abort 后遗留的 cleanup 触发，不该把接管流的盒替换掉。直接返回。
  //   （正常续答流在自身 done 时此标记已由其接管逻辑清掉，不会命中这里。）
  if(aiBox && aiBox.dataset && aiBox.dataset.reuseForPick === '1') return;
  // 2026-10-10（米爸实测「一轮对话输出两次内容」·会话 9336）：
  //   done 事件带真实 message_id ⇒ 本轮 assistant 消息**已落库**。
  //   在流式容器上打 `finalizedId` 标记，`commitPartialStream` 见到它就直接短路，
  //   避免"正常完成之后又发一次固化请求"把同一段内容写第二遍。
  //   ⚠️ 必须在 `aiBox.outerHTML = ...` 之前写：那一行会把 aiBox 从 DOM 摘掉。
  try{ if(aiBox && r && r.message_id) aiBox.dataset.finalizedId = String(r.message_id); }catch(e){}
  // 内容级澄清收尾：done 用 renderMessage 重建会清掉交互卡 → 重建后重新插入澄清卡
  if(r && r.msg_type === 'clarify'){
    const _pb0 = document.getElementById('proc-box');
    if(_pb0) _pb0.querySelectorAll('.proc-block').forEach(b=>b.classList.add('collapsed'));
    const procHtml = _procBoxBlocksHtml(_pb0);
    const aiMsg = {role:'assistant', content:r.content||'', msg_type:'text',
                   card_data:'{}', id:r.message_id, process_html: procHtml};
    aiBox.outerHTML = renderMessage(aiMsg);
    const newEl = r.message_id ? document.getElementById('msg-' + r.message_id) : null;
    const nb = newEl ? newEl.querySelector('.body') : null;
    if(nb) renderClarifyAsk({questions: r.questions||[]}, nb);
    const _a2 = document.getElementById('chat-area'); if(_a2) _a2.scrollTop = _a2.scrollHeight;
    return;
  }
  // 流式结束：替换为完整 renderMessage（含执行过程块 + 卡片 + HIL 按钮 + 来源信息）
  const cardTypeMap = {impact:'card_impact', review:'card_review', requirement_analysis:'card_candidates'};
  // 流式结束：全部步骤已完成 → 统一收口（V2.6：默认收起时间线，只留「✓ 已完成」汇总条，可展开按层级回看）
  const _pb = document.getElementById('proc-box');
  if(_pb) _pb.querySelectorAll('.proc-block').forEach(b=>b.classList.add('collapsed'));
  // V2.4 保留会话内执行过程（完整时间线，历史展开时还原）；2026-09-26：**剔除 live 收口条**，
  // 否则会把「⟳ 执行中」那条快照进历史，与历史区自己的「✓ 已完成」收口条重复（残影 bug）
  const procHtml = _procBoxBlocksHtml(_pb);
  // V2.6：标记完成 → 收口条切「✓ 已完成 · N 环节 · 耗时 · token」
  let _sumTxt = '';
  try{
    _procS.done = true; _procS.endTime = Date.now();
    _procS.summaryCollapsed = true;
    const _llm = r.llm || {};
    const _tk = _llm.tokens || {};
    const _used = (r.card && r.card.run_budget && r.card.run_budget.used_tokens) || 0;
    let _tokTxt = '';
    if(_tk.prompt || _tk.completion) _tokTxt = ` · ⚡ 入 ${_tk.prompt||0} / 出 ${_tk.completion||0} tok`;
    else if(_used) _tokTxt = ` · ⚡ ≈${_used} tok`;
    const _failN = _procS.timeline.filter(x=>x.status==='failed').length;
    _sumTxt = `✓ 已完成 · ${_procS.timeline.length} 环节 · 耗时 ${_procElapsedText()}${_tokTxt}${_failN?` · ${_failN} 失败`:''}`;
    stopElapsedTimer();
  }catch(e){}
  // 历史消息执行过程：收口条 + 默认收起的时间线容器（点击展开按层级回看）
  const histProcHtml = procHtml
    ? `<div class="hist-proc"><div class="proc-summary collapsed" onclick="histProcToggle(this)">${_sumTxt||'✓ 已完成'}${_procDetailBtn()}<span class="chev">▾</span></div><div class="proc-timeline">${procHtml}</div></div>`
    : '';
  const aiMsg = {role:'assistant', content:r.content, msg_type:cardTypeMap[r.intent]||'text', card_data:JSON.stringify(r.card||r.retrieval||{}), id:r.message_id, process_html: histProcHtml};
  aiBox.outerHTML = renderMessage(aiMsg);
  // 闭环：附件解析状态 + 匹配工作流卡片（插入 AI 消息后）
  let extra = '';
  if(r.attachments_info && r.attachments_info.parsed > 0){
    let atag = `📎 已解析 ${r.attachments_info.parsed} 份上传资料并注入分析`;
    /* ★ 2026-10-20（用户报两处片段数不一致）：口径必须分开写清——
       consumed = 附件全文按提问匹配出的片段（top5），与正文引用区的
       「检索引用 N 条 chunk」（RAG chunk_hits 中被回答标注引用的）是两个数。
       旧文案「本次消费 N 条资料片段」与引用区并排显示，用户以为是同一口径。 */
    if(r.retrieval && r.retrieval.attachment_used) atag += `，附件匹配片段 ${r.attachments_info.consumed||0} 条（按提问相关度选取）`;
    atag += (r.attachments_info.skipped||[]).length ? `（${r.attachments_info.skipped.length} 份无法解析）` : '';
    extra += `<div style="margin:2px 0 6px 44px;font-size:12.5px;color:var(--grn);">${atag}</div>`;
  }
  if(r.report && r.report.sections){
    window._lastReport = r.report;
    // 报告已由后端在落库时自动归档到报告（artifacts + reports 表），前端仅提供导出入口
    extra += `<div style="margin:2px 0 6px 44px;display:flex;gap:6px;flex-wrap:wrap;">
      <button class="btn sm ghost" style="font-size:12px;padding:1px 8px;" onclick="downloadReport('md')">⬇ Markdown</button>
      <button class="btn sm ghost" style="font-size:12px;padding:1px 8px;" onclick="downloadReport('docx')">⬇ Word</button>
      <button class="btn sm ghost" style="font-size:12px;padding:1px 8px;" onclick="downloadReport('pdf')">⬇ PDF</button>
      <span class="st ok" style="align-self:center;" title="AI 建模产出报告已自动归档，可在「报告」统一检索/导出">📄 已归档</span>
    </div>`;
  }
  // P0 需求质量分析：V2.6 视觉降级——默认折叠为一行次要摘要（不再喧宾夺主），点击展开明细
  if(r.quality_report && r.quality_report.issues && r.quality_report.issues.length){
    const q = r.quality_report;
    const stats = q.stats||{};
    const sevColor = {high:'var(--red)', medium:'var(--amber)', low:'var(--grn)'};
    const qHtml = q.issues.map(it=>`<div style="padding:4px 0;border-bottom:1px solid var(--line);">
      <div style="font-size:12.5px;"><span style="color:${sevColor[it.severity]||'var(--mut)'};font-size:12px;margin-right:4px;">[${(it.severity||'').toUpperCase()}]</span>${esc(it.problem)}</div>
      <div style="color:var(--mut);font-size:12px;margin-top:2px;">📌 ${esc(it.evidence||'')}</div>
      <div style="color:var(--blue-d);font-size:12px;margin-top:2px;">💡 ${esc(it.suggestion||'')}</div></div>`).join('');
    const _qsum = `📋 需求质量参考：${q.issues.length} 条建议 · 评分 ${q.score||0}/100（需求 ${stats.total||0} 条 · 模糊 ${stats.fuzzy||0} · 缺量化 ${stats.no_measure||0}）`;
    extra += `<div style="margin:2px 0 6px 44px;">
      <div class="qr-summary" onclick="qrToggle(this)" style="display:flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:#fafbfc;font-size:12.5px;color:var(--mut);cursor:pointer;user-select:none;">
        <span class="chev" style="font-size:11.5px;transition:transform .2s;">▸</span>${_qsum}<span style="font-size:12px;color:var(--blue-d);text-decoration:underline;">明细</span></div>
      <div class="qr-detail" style="display:none;border:1px solid var(--line);border-radius:6px;padding:6px 10px;background:#fafbfc;font-size:12.5px;margin-top:4px;max-height:260px;overflow:auto;">${qHtml}</div>
    </div>`;
  }
  if(extra) document.getElementById('chat-area').insertAdjacentHTML('beforeend', extra);
  // 本次会话信息
  document.getElementById('chat-intent').textContent = r.intent;
  document.getElementById('chat-agent').textContent = r.agent||'-';
  document.getElementById('chat-hil').textContent = r.hil_level||'-';
  const _cs = document.getElementById('chat-status'); if(_cs) _cs.textContent = `${r.agent||''} 已响应 ｜ 意图：${r.intent} ｜ HIL ${r.hil_level||'-'} ｜ 来源：${r.retrieval?.source||'-'}（图谱${r.retrieval?.graph_count||0}/向量${r.retrieval?.vector_count||0}）`;
  if(r.retrieval) {
    const kbHtml = (r.kb_tags||[]).length ? `<div class="src"><span class="dot b"></span>知识库引用：${r.kb_tags.map(t=>'#'+t).join(' ')}</div>` : '';
    document.getElementById('chat-sources').innerHTML = `
      ${kbHtml}
      <div class="src"><span class="dot g"></span>图谱检索：${r.retrieval.graph_count} 条</div>
      <div class="src"><span class="dot v"></span>向量补充：${r.retrieval.vector_count} 条</div>
      <div style="font-size:12.5px;color:var(--mut);">置信度：${(r.retrieval.confidence||0).toFixed(2)}</div>`;
  }
  // 会话产物分栏增量刷新（后端已归档本消息产物）
  if(currentConvId) loadArtifacts(currentConvId);
  // ★ 2026-10-29（用户反馈2）：正文来源标注 + 限高内滚（详见 js/mods/51-answer-view.js）。
  //   必须在 **outerHTML 替换之后**——替换前的流式 DOM 已被丢弃，加在那儿等于没加。
  //   不折叠、不隐藏（遵守 B5-8「最终输出结果不要收起来」），只限容器高度。
  try{
    if(r && r.message_id){
      const _el = document.getElementById('msg-' + r.message_id);
      if(_el && typeof applyAnswerFrame === 'function'){
        applyAnswerFrame(_el, {agent: r.agent, hil: r.hil_level});
      }
    }
  }catch(e){}
  // 2026-09-29：流结束改贴底才跟随（用户上滑阅读上文时不被打断）
  stickBottom();
  // ★ 2026-10-17（米爸反馈2）：收口替换（outerHTML 重渲染 + 卡片/报告附加块）之后
  //   布局仍会异步长高（mermaid/图片/代码高亮），再钉一次底，保证视口停在最新内容区。
  requestAnimationFrame(function(){ stickBottom(); });
  // ★ 2026-10-22（米爸反馈「会话输出完成后总是定位在最上面」）：
  //   `aiBox.outerHTML = renderMessage(aiMsg)` 是**整块替换**，而 `.msg` 带
  //   `content-visibility:auto`（屏外按 132px 估算）⇒ 新节点先以估算高度占位，
  //   真实高度随后冲高（实测同一会话估算 3680px / 真实 28170px）。
  //   单次 rAF 追不上这种异步长高：实测钉底之后最后一条消息仍在视口下方 3190px
  //   （视口仅 659px）= 用户看到的是「上面」，正是本条反馈的现场。
  //   ⇒ 跟读态下开一个**短窗口**持续锚定（150ms 步进，内置 wheel/touchmove 让位，
  //     用户一旦上滑立刻停止，不打扰阅读）。
  try{
    const _pa = document.getElementById('chat-area');
    if(_pa && (typeof _stickFollow === 'undefined' || _stickFollow)) pinChatBottom(_pa, 3000);
  }catch(e){}
}
// V2.6 需求质量参考：折叠明细切换
function qrToggle(sumEl){
  const wrap = sumEl.closest('div[style*="margin:2px 0 6px 44px"]') || sumEl.parentElement;
  const det = wrap ? wrap.querySelector('.qr-detail') : null;
  if(!det) return;
  const open = det.style.display !== 'none';
  det.style.display = open ? 'none' : 'block';
  const ch = sumEl.querySelector('.chev');
  if(ch) ch.style.transform = open ? '' : 'rotate(90deg)';
}

// ── 问答可解释性：答案内 [n] 引用链接 → 来源详情面板（文档段落 + 关联模型元素）──
// 依赖 renderMessage 缓存到 window._msgCites[msgId] 的 citations（后端 chunk_hits 透传）
async function openCitation(sup){
  const msg = sup.closest('.msg');
  const mid = msg ? (msg.dataset.mid || '') : '';
  const i = parseInt(sup.dataset.i || '1', 10) - 1;
  const cites = (window._msgCites && mid && window._msgCites[mid]) || [];
  const c = cites[i];
  if(!c){ toast('引用来源不可用'); return; }
  window._citeLast = c;   // P1-5：缓存当前引用，供「在源文件中定位」按钮使用
  const num = i + 1;
  const confCls = c.confidence_level==='高' ? 'ok' : (c.confidence_level==='中' ? 'a' : 'w');
  let html = `
    <div style="font-size:12.5px;color:var(--mut);margin-bottom:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
      <span class="st b">引用 ${num}</span>
      <span class="tag">${esc(c.source_doc||'-')}</span>
      ${c.section?`<span class="tag">${esc(c.section)}</span>`:''}
      <span class="tag">chunk#${c.chunk_index!==undefined?c.chunk_index:'-'}</span>
      ${c.confidence_level?`<span class="st ${confCls}">置信 ${esc(c.confidence_level)}</span>`:''}
      ${c.score!==undefined?`<span class="tag">score ${c.score}</span>`:''}
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">
      <button class="btn sm" onclick="citeOpenInPreview()" title="打开源文件预览，自动高亮并滚动到引用片段">📖 在源文件中定位</button>
    </div>
    <h4 style="margin:8px 0 6px;font-size:14.5px;">📄 命中段落</h4>
    <div style="border:1px solid var(--line);border-radius:8px;padding:10px;background:#fffdf5;font-size:13.5px;line-height:1.7;max-height:200px;overflow:auto;white-space:pre-wrap;">${esc(c.content||'')}</div>`;
  openPanel(`引用来源 [${num}] · ${c.source_doc||'未知文档'}`, html + '<div class="loading">正在加载来源详情…</div>');
  // 拉取文档详情：定位命中段落上下文 + 关联模型元素（chunk↔实体溯源）
  if(c.document_id){
    try{
      const d = await api('/api/documents/' + c.document_id);
      let extra = '';
      const chunks = (d.chunks||[]).filter(x=>x.chunk_index!==undefined);
      const idx = chunks.findIndex(x=>Number(x.chunk_index)===Number(c.chunk_index));
      if(chunks.length){
        const from = Math.max(0, idx-1), to = Math.min(chunks.length, idx+2);
        const ctx = chunks.slice(from, to).map(x=>{
          const isCur = Number(x.chunk_index)===Number(c.chunk_index);
          return `<div style="margin-top:6px;padding:6px 9px;border-radius:6px;${isCur?'border:1px solid var(--blue-d);background:var(--blue-l);':'border:1px solid var(--line);background:#fafafa;'}" ${isCur?'id="cite-chunk"':''}>
            <div style="font-size:12px;color:var(--mut);margin-bottom:3px;">§${x.chunk_index}${isCur?' · 命中引用':' · 上下文'}</div>
            <div style="font-size:13.5px;line-height:1.65;color:${isCur?'var(--txt)':'var(--mut)'};">${esc((x.content||'').slice(0,260))}${(x.content||'').length>260?'…':''}</div>
          </div>`;
        }).join('');
        extra += `<h4 style="margin:14px 0 6px;font-size:14.5px;">📍 文档原文定位（§${c.chunk_index}）</h4><div style="max-height:300px;overflow:auto;">${ctx}</div>`;
      }
      // 模型元素：chunk 溯源链接的实体（查看=viewEntity 详情；定位=图谱工作台高亮定位，P1-5）
      const ents = d.linked_entities||[];
      if(ents.length){
        extra += `<h4 style="margin:14px 0 6px;font-size:14.5px;">🧬 关联模型元素（${ents.length}）</h4>
          <div style="display:flex;flex-direction:column;gap:4px;">` + ents.slice(0,20).map(e=>
          `<div style="display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:13.5px;">
            <span class="st ${e.status==='reviewed'?'ok':e.status==='candidate'?'w':'g'}">${esc(e.status||'')}</span>
            <b>${esc(e.name)}</b><span class="tag">${esc(e.entity_type||'')}</span>
            <span style="margin-left:auto;display:flex;gap:4px;">
              <button class="btn sm ghost" style="font-size:12px;padding:0 8px;" onclick="locateEntityInGraph('${e.id}')" title="跳转知识图谱，高亮并居中该实体">🎯 定位</button>
              <button class="btn sm ghost" style="font-size:12px;padding:0 8px;" onclick="viewEntity('${e.id}')">查看</button>
            </span>
          </div>`).join('') + '</div>';
      }
      const body = document.getElementById('panel-body');
      if(body) body.innerHTML = html + extra;
      const cur = document.getElementById('cite-chunk');
      if(cur) cur.scrollIntoView({block:'center'});
    }catch(e){
      const body = document.getElementById('panel-body');
      if(body) body.innerHTML = html + `<div style="color:var(--mut);font-size:12.5px;margin-top:10px;">来源详情加载失败：${esc(e.message||'')}</div>`;
    }
  }
}

// ── P1-5（2026-09-21）：引用端到端定位 ─────────────────────────────────
// ① 引用 → 源文件预览锚点：打开 38-filepreview 的文本视图，自动高亮并滚动到引用片段
function citeOpenInPreview(){
  const c = window._citeLast;
  if(!c || !c.document_id){ toast('该引用没有对应的入库文档，无法定位源文件'); return; }
  openFilePreview({doc_id: c.document_id, filename: c.source_doc || '文档',
                   find: String(c.content||'').slice(0, 200), tab: 'text'});
}
// ② 模型元素 → 图谱工作台定位高亮：非图谱页先跳转（go('kb','kb-d')），等图谱数据加载后
//    复用 34-graphtabs.js 的 gwtLocateEntity（选中四件套：画布高亮+居中+右栏详情+左栏树定位）。
//    任何一步组件缺失都降级到 viewEntity 详情弹窗，不静默失败。
async function locateEntityInGraph(id){
  id = String(id||'').trim();
  if(!id){ toast('缺少实体标识'); return; }
  if(typeof gwtLocateEntity !== 'function'){
    if(typeof viewEntity === 'function') viewEntity(id);
    else toast('图谱定位组件未加载');
    return;
  }
  const pg = document.querySelector('.page.on');
  if(!pg || pg.id !== 'pg-kb'){
    go('kb','kb-d');
    // 等图谱数据到位（loadGraph 异步；上限 5s，超时也继续——gwtLocateEntity 内部还有兜底）
    const t0 = Date.now();
    while(Date.now() - t0 < 5000){
      let loaded = false;
      try{ loaded = !!((graphState.all && graphState.all.nodes) || []).length; }catch(e){}
      if(loaded) break;
      await new Promise(r=>setTimeout(r, 150));
    }
  }else if(typeof wsTab === 'function'){
    wsTab('content');
  }
  gwtLocateEntity(id);
}

// ── V3.0 AI 建模 SysML 版本链 / 入库（统一 v2g 候选治理入口）──
