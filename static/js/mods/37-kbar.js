/* 2026-09-18 S6-3/S6-4：由 static/index.html 内联 <script> 迁出（原位置见 git 历史）
 * 注意：本文件必须在该块原本引用的 DOM 之后加载（见 index.html 中的 <script src>）。
 */

(function(){
  // ── 注册全局快捷键（不冲突：与已有监听器并存，仅在 capture 阶段不拦截） ──
  // Cmd/Ctrl+K → 打开命令面板
  // Cmd/Ctrl+Enter → 聊天聚焦时发送
  // Esc → 关闭命令面板 + 顶层抽屉
  const isMac = /Mac|iPod|iPhone|iPad/.test(navigator.platform);

  function _k(e, key){
    const cmd = isMac ? e.metaKey : e.ctrlKey;
    if(cmd && (e.key === 'k' || e.key === 'K') && !e.altKey && !e.shiftKey){
      e.preventDefault();
      openKbar();
      return true;
    }
    if(cmd && e.key === 'Enter'){
      const inp = document.getElementById('chat-input');
      if(document.activeElement === inp){
        e.preventDefault();
        try { sendChat(); } catch(err){ console.warn('sendChat failed', err); }
        return true;
      }
    }
    if(e.key === 'Escape'){
      if(_kbar && _kbar.classList.contains('on')){
        closeKbar(); return true;
      }
    }
    return false;
  }
  document.addEventListener('keydown', _k, true);

  // ── 命令面板（KBar） ──
  let _kbar, _klist, _kinput;
  const _commands = [
    {ic:'💬', label:'新建任务',    hint:'进入 AI 建模页并创建草稿',      act:()=>{ go('ai'); setTimeout(newTask, 200); }},
    {ic:'🏭', label:'能力中心',    hint:'Agent / 技能 / 工具与 MCP',      act:()=>go('studio')},
    {ic:'🧬', label:'本体模型',    hint:'知识中心 · Schema 定义',          act:()=>{ window._kbCtxTerms=false; go('kb', 'kb-c'); }},
    {ic:'📖', label:'术语词典',    hint:'词条 ↔ 本体类型归一映射',        act:()=>{ window._kbCtxTerms=true; go('kb', 'kb-c'); }},
    {ic:'🕸', label:'知识中心',    hint:'资料库 + 图谱工作区 + 本体 + 词典', act:()=>{ go('kb', 'kb-e'); }},
    {ic:'📄', label:'报告',        hint:'生成设计报告',                  act:()=>go('reports')},
    {ic:'👥', label:'用户与权限',  hint:'管理员专属',                    act:()=>go('users')},
    {ic:'✅', label:'审批管理',    hint:'管理员专属',                    act:()=>go('approval')},
    {ic:'🛡', label:'审计日志',    hint:'管理员专属',                    act:()=>go('audit')},
    {ic:'📈', label:'运行监控',    hint:'管理员专属',                    act:()=>go('ops')},
    {ic:'🏠', label:'总览工作台',  hint:'回到首页 Dashboard',            act:()=>go('home')},
    // 操作类（无目标页，但常用）
    {ic:'🎯', label:'切换主题',    hint:'浅色 / 暗色（点账号菜单可精细控制）', act:()=>{
      const cur = (()=>{ try{return localStorage.getItem('mbse_theme')||'auto';}catch(_){return 'light';} })();
      const next = cur === 'dark' ? 'light' : 'dark';
      window.setTheme(next);
    }},
    {ic:'⊞', label:'折叠左侧任务区', hint:'展开或收起任务分组', act:()=>{ if(typeof toggleTaskGroup === 'function') toggleTaskGroup(); }},
    {ic:'⌘', label:'帮助（所有快捷键）', hint:'展示快捷键一览', act:()=>showShortcutsHelp()},
    {ic:'⚡', label:'刷新页面',     hint:'重新加载当前页',                 act:()=>location.reload()},
  ];
  function _render(q){
    q = (q||'').trim().toLowerCase();
    const items = q ? _commands.filter(c =>
      c.label.toLowerCase().includes(q) || (c.hint||'').toLowerCase().includes(q)
    ) : _commands;
    if(!items.length){
      _klist.innerHTML = '<div class="kb-empty">没找到匹配的命令</div>';
      return;
    }
    _klist.innerHTML = items.map((c,i)=>`
      <div class="kb-item${i===0?' on':''}" data-i="${i}">
        <span class="ki">${c.ic}</span>
        <span class="kt"><b>${c.label}</b><span>${c.hint||''}</span></span>
        <kbd class="kb-kc">↵</kbd>
      </div>`).join('');
    _klist._items = items;
  }
  function openKbar(){
    if(!_kbar){
      _kbar = document.getElementById('kbar');
      _kinput = document.getElementById('kbar-input');
      _klist = document.getElementById('kbar-list');
      _kinput.addEventListener('input', e=>_render(e.target.value));
      _kinput.addEventListener('keydown', e=>{
        if(e.key === 'ArrowDown'){ e.preventDefault(); _move(1); }
        else if(e.key === 'ArrowUp'){ e.preventDefault(); _move(-1); }
        else if(e.key === 'Enter'){ e.preventDefault(); const sel = _klist.querySelector('.kb-item.on'); if(sel){ sel.click(); } }
      });
      _klist.addEventListener('click', e=>{
        const it = e.target.closest('.kb-item'); if(!it) return;
        const i = +it.dataset.i;
        const cmd = _klist._items && _klist._items[i];
        if(cmd && cmd.act){ cmd.act(); closeKbar(); }
      });
    }
    _kbar.classList.add('on');
    _kinput.value = '';
    _render('');
    setTimeout(()=>_kinput.focus(), 30);
  }
  function closeKbar(){ _kbar.classList.remove('on'); }
  function _move(d){
    const items = _klist.querySelectorAll('.kb-item');
    if(!items.length) return;
    let cur = [...items].findIndex(x=>x.classList.contains('on'));
    if(cur < 0) cur = 0; else cur = (cur + d + items.length) % items.length;
    items.forEach(x=>x.classList.remove('on'));
    items[cur].classList.add('on');
    items[cur].scrollIntoView({block:'nearest'});
  }
  window.openKbar = openKbar;
  window.closeKbar = closeKbar;

  // 命令面板外部点击关闭
  document.getElementById('kbar').addEventListener('click', e=>{
    if(e.target.id === 'kbar') closeKbar();
  });

  // 启动时恢复主题偏好（来自 P1-3 暗色主题）
  try {
    const th = localStorage.getItem('mbse_theme') || 'auto';
    if(th === 'auto') applyAutoTheme();
    else document.documentElement.dataset.theme = th;
  } catch(_){}

  // 帮助浮层
  window.showShortcutsHelp = function(){
    toast.info('快捷键：⌘/Ctrl+K 打开命令面板 · ⌘/Ctrl+Enter 在输入区发送 · Esc 关闭顶层面板', {ttl: 6000});
  };

// ──────────────────────────────────────────────────────────────
// Q1：全局主题切换函数 setTheme('light' | 'dark' | 'auto')
// ──────────────────────────────────────────────────────────────
function applyAutoTheme(){
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = prefersDark ? 'dark' : '';
}
function _refreshThemeToggle(){
  // 同步顶栏 #theme-toggle 按钮图标（显示当前生效模式）
  const cur = (()=>{ try { return localStorage.getItem('mbse_theme') || 'auto'; } catch(_) { return 'auto'; } })();
  const icon = cur === 'dark' ? '🌙' : cur === 'light' ? '☀️' : '🖥️';
  const btn = document.getElementById('theme-toggle');
  if(btn) btn.textContent = icon;
}
window.cycleTheme = function(){
  // 单击循环：auto -> light -> dark -> auto
  const cur = (()=>{ try { return localStorage.getItem('mbse_theme') || 'auto'; } catch(_) { return 'auto'; } })();
  const next = cur === 'auto' ? 'light' : cur === 'light' ? 'dark' : 'auto';
  window.setTheme(next);
  _refreshThemeToggle();
};
  window.setTheme = function(v){
    try { localStorage.setItem('mbse_theme', v); } catch(_){}
    if(v === 'dark')   document.documentElement.dataset.theme = 'dark';
    else if(v === 'light') document.documentElement.dataset.theme = '';
    else /* auto */    applyAutoTheme();
    // 高亮当前选中（账号抽屉三按钮 + 顶栏按钮图标）
    document.querySelectorAll('#um-theme-opts button').forEach(b=>{
      b.classList.toggle('on', b.dataset.themeVal === v);
      b.setAttribute('aria-pressed', b.dataset.themeVal === v ? 'true' : 'false');
    });
    try { _refreshThemeToggle(); } catch(_){}
    toast.success(v==='dark' ? '已切换到暗色主题' : v==='light' ? '已切换到浅色主题' : '已切换到跟随系统');
  };
  // 启动时高亮并初始化顶栏图标
  try {
    const cur = localStorage.getItem('mbse_theme') || 'auto';
    document.querySelectorAll('#um-theme-opts button').forEach(b=>{
      b.classList.toggle('on', b.dataset.themeVal === cur);
    });
    _refreshThemeToggle();
  } catch(_){}
  // 监听系统主题变化（auto 模式自动响应）
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', ()=>{
      try {
        const stored = localStorage.getItem('mbse_theme');
        if(stored === 'auto') applyAutoTheme();
      } catch(_){}
    });
  } catch(_){}
})();
