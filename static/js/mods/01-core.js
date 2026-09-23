/* 核心：api / toast / modal / dialog / 多选
 * 由 static/index.html 巨型 inline script 机械切分而来
 * 原行号 1-571  ·  全局作用域（非 module），内联 onclick 依赖全局函数名
 */

const API = '';
let currentConvId = null;
let currentRoleId = null;
let _isDraft = false;   // 2026-09-04 v3：新建任务草稿态（未提交前不创建会话、不自动选中）

// ── 工具函数 ──
/* 2026-09-18 S6-1：esc / escA 迁至本文件（原在 08-sysmlview.js:151/153）。
   动机：这两个是全站最强隐藏依赖 —— 35 个模块、约 1,700 处调用，却定义在第 8 个模块里；
   一旦脚本加载顺序调整或 08 出问题，全站模板渲染立即报 esc is not defined。
   01-core.js 是第一个加载的模块，放在这里即天然消除顺序耦合。函数体逐字节保持原实现。 */
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
// 属性安全转义（data-* 用于会话信息卡）
function escA(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&#39;');}

async function api(path, opts={}) {
  const uid = localStorage.getItem('mbse_user_id');
  const sess = localStorage.getItem('mbse_session');   // 2026-09-23 FR-UR-1：会话 token（/api/auth 签发）
  const headers = {'Content-Type':'application/json'};
  if(uid) headers['X-User-Id'] = uid;   // P2：登录态透传 → 后端审计归属当前用户
  if(sess) headers['X-Session-Token'] = sess;
  const r = await fetch(API + path, {...opts, headers:{...(opts.headers||{}), ...headers}});
  const data = await r.json().catch(()=>({}));
  // 统一错误契约：后端 HTTPException 返回 {detail:...}（如 403 无权限），
  // 前端各处只识别 r.error → 非 2xx 时把 detail 归一为 error，避免"操作已成功"的误提示
  if(!r.ok && data && !data.error && data.detail) data.error = data.detail;
  return data;
}
/* ── 2026-09-18 S6-4：全局 UI 服务（toast / 错误浮层 / 全局错误兜底 / alert 桥接）──
   背景：本文件原有一个指向已废弃 #toast 元素的旧版 toast（实际早已失效，只 console.warn）；
        真正生效的是 static/index.html 末尾内联块里的实现（它以 window.toast 覆盖了本函数）。
        两处实现并存期间出过一次严重事故：内联块一度把「普通对象」赋给 window.toast，
        覆盖了本文件的 function toast → 全站 700+ 处 toast('...') 抛 TypeError，
        表现为「点击保存无效、面板不关、无任何提示」。
   现把生效实现并入本文件（**全站单一实现**），并**懒查 DOM** —— 01-core.js 在 #toast-root 之前加载，
        不能在加载期取元素。对应内联块已从 index.html 删除。
   调用契约保持不变：toast(msg) / toast(msg, ms) / toast(msg, opts)，以及 toast.success|warn|error|info|show。
*/
const TOAST_ICONS = {info:{n:'info',fb:'ℹ'}, success:{n:'success',fb:'✓'}, warn:{n:'warn',fb:'⚠'}, error:{n:'error',fb:'✕'}};
function _toastIcon(name, fallback){
  return '<svg class="toast-ic" width="16" height="16" aria-hidden="true" style="flex:none;color:currentColor;"><use href="#ic-'+name+'"/></svg>'
       + (fallback ? '<span class="ti-fb" style="display:none;">'+fallback+'</span>' : '');
}
function _toastShow(type, msg, opts){
  if(!msg) return;
  const root = document.getElementById('toast-root');   // 懒查：与脚本/元素加载顺序解耦
  if(!root){ try{ console.warn('[toast]', msg); }catch(e){} return; }
  opts = opts || {};
  const ic = TOAST_ICONS[type] || TOAST_ICONS.info;
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'info');
  t.setAttribute('role','status');
  t.innerHTML = _toastIcon(ic.n, ic.fb) + '<span class="tc"></span>'
              + '<button class="tx" aria-label="关闭"><svg width="14" height="14" style="display:block;color:currentColor;"><use href="#ic-close"/></svg></button>';
  t.querySelector('.tc').textContent = msg;
  root.appendChild(t);
  const close = ()=>{ t.classList.add('toast-out'); setTimeout(()=>t.remove(),220); };
  t.querySelector('.tx').onclick = close;
  const ttl = opts.ttl == null ? (type==='error' ? 5000 : 3000) : opts.ttl;
  if(ttl > 0) setTimeout(close, ttl);
  return close;
}
const _toastFn = function(msg, ms){
  if(ms == null) return _toastShow('info', msg);
  if(typeof ms === 'number') return _toastShow('info', msg, {ttl: ms});
  return _toastShow('info', msg, ms);
};
window.toast = Object.assign(_toastFn, {
  show:(m,o)=>_toastShow('info',m,o),
  info:(m,o)=>_toastShow('info',m,o),
  success:(m,o)=>_toastShow('success',m,o),
  warn:(m,o)=>_toastShow('warn',m,o),
  error:(m,o)=>_toastShow('error',m,o),
});
// 富内容 toast（支持 HTML，视图节点/边属性浮层用）
// 2026-09-18：改指向 #toast-root —— 原实现指向已废弃的 #toast，元素不存在时会退化成「纯文本 toast」，
// 富内容（加粗/多行）能力实际从未生效。
function toastHtml(html){
  if(html == null) return;
  const root = document.getElementById('toast-root');
  if(!root) return;
  const t = document.createElement('div');
  t.className = 'toast info';
  t.setAttribute('role','status');
  t.innerHTML = String(html);
  root.appendChild(t);
  const close = ()=>{ t.classList.add('toast-out'); setTimeout(()=>t.remove(),220); };
  t.onclick = close;
  setTimeout(close, 3500);
  return close;
}
/* 致命错误浮层（比 toast 重；DOM 元素在 index.html，调用期解析 → 与加载顺序无关） */
function errOverlay(title, body, stack){
  const ov = document.getElementById('err-overlay');
  if(!ov) return;
  if(title) document.getElementById('eo-title').textContent = title;
  if(body != null) document.getElementById('eo-body').textContent = body;
  const st = document.getElementById('eo-stack');
  if(stack){ st.textContent = stack; st.style.display = 'block'; } else { st.style.display = 'none'; }
  ov.classList.add('on');
}
window.errOverlay = errOverlay;
/* 全局未捕获错误兜底 —— 防止后端 bug 或脚本加载失败让用户面对空白/原始报错 */
window.addEventListener('error', function(e){
  if(!e || !e.error) return;
  const msg = e.message || (e.error && e.error.message) || '未知异常';
  try { _toastShow('error', '运行异常: ' + msg, { ttl: 6000 }); } catch(_){}
  if(/load|fetch|script/i.test(msg) && !window.__skippedFatal){
    window.__skippedFatal = true;
    setTimeout(()=>errOverlay('脚本资源异常', msg, (e.error && e.error.stack) || ''), 100);
  }
});
window.addEventListener('unhandledrejection', function(e){
  const r = e.reason; const msg = (r && (r.message || r.toString())) || '操作失败';
  try { _toastShow('error', '请求失败: ' + msg, { ttl: 6000 }); } catch(_){}
  if(/scope_ids/.test(msg)) return;   // 已知后端 bug（ChatIn.scope_ids）：静默不打扰
});
/* alert 桥接：旧代码直接调 alert 时给柔和提示，而不是弹系统框 */
window.__origAlert = window.alert;
window.alert = function(msg, type){
  if(msg == null) return;
  _toastShow(type==='err' ? 'error' : (type==='warn' ? 'warn' : 'info'), String(msg));
};
/* 2026-09-18 S6-3/S6-4：内联 SVG 图标 sprite 载入（原 index.html 内联 <script>）
   把 /static/icons.svg 的内容 fetch 进来内联到 #svg-sprite（一次性；比图标字体轻、比 <img> 灵活）。
   #svg-sprite 在 index.html 后部 → 等 DOM 就绪再跑，与加载顺序解耦。 */
(function(){
  const _loadSprite = ()=>{
    const el = document.getElementById('svg-sprite');
    if(!el) return;
    fetch('/static/icons.svg').then(r=>r.text()).then(html=>{ el.innerHTML = html; })
      .catch(()=>{ /* 静默失败，回落到 emoji */ });
  };
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _loadSprite);
  else _loadSprite();
})();
function showModal(type) {
  const m = document.getElementById('modal');
  const b = document.getElementById('modal-body');
  const forms = MODAL_FORMS;
  b.innerHTML = forms[type] || '<h3>表单</h3>';
  m.classList.add('show');
  m.onclick = e => { if(e.target===m) closeModal(); };  // 点击遮罩关闭
  if(type==='user'){ _urEdit={mode:null,id:null}; loadRoleOptions(); loadDepartmentOptions(''); }
  if(type==='department'){ _urEdit={mode:null,id:null}; const t=document.getElementById('dept-form-title'); if(t) t.textContent='新增部门'; }
  if(type==='role'){ _urEdit={mode:null,id:null}; const t=document.getElementById('role-form-title'); if(t) t.textContent='新建角色'; }
  if(type==='agent') loadProviderOptions();
  if(type==='branch') initBranchForm();
  if(type==='merge') initMergeForm();
}
const MODAL_FORMS = {
  // 统一插件（/api/plugins）：manifest 为唯一管理契约，按 type 联动 SKILL.md / server.json 载荷行
  unifiedPlugin: `<h3 id="up-form-title">🧩 新建统一插件</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📋</span><span class="fs-title">清单 manifest <span class="fs-hint">（JSON，唯一管理契约）</span></span></div>
        <div class="form-row"><label>manifest <span class="req">*</span> <span class="info-tip" title="必填：id（反向域名）/ name（小写连字符）/ version（semver 三段）/ type / description / capabilities">ⓘ</span></label><textarea id="up-manifest" rows="12" placeholder='{"id":"com.example.demo","name":"demo","version":"1.0.0","type":"skill","description":"触发描述…","capabilities":{"skills":[{"path":"SKILL.md"}]}}'></textarea></div>
      </div>
      <div class="form-section" id="up-skill-row">
        <div class="form-section-head"><span class="fs-icon">🧩</span><span class="fs-title">SKILL.md 正文</span></div>
        <div class="form-row"><label>正文（type=skill 时填写；frontmatter + 指令）</label><textarea id="up-skill-md" rows="7" placeholder="--- 换行 name: demo 换行 description: … 换行 --- 换行 换行 正文指令…"></textarea></div>
      </div>
      <div class="form-section" id="up-mcp-row" style="display:none;">
        <div class="form-section-head"><span class="fs-icon">🔌</span><span class="fs-title">server.json</span></div>
        <div class="form-row"><label>MCP 服务配置（type=mcp 时填写）</label><textarea id="up-mcp-server" rows="7" placeholder='{"base_url":"http://127.0.0.1:8080","transport":"streamable_http","tools":[{"name":"demo_tool"}]}'></textarea></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveUnifiedPlugin()">保存</button></div>`,
  prompt: `<h3>新建提示词</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📝</span><span class="fs-title">基础信息</span></div>
        <div class="form-row"><label>名称 <span class="req">*</span> <span class="info-tip" title="如：需求分析-宽带通信；建议带上「用途-场景」便于检索">ⓘ</span></label><input id="f-name" placeholder="如：需求分析-宽带通信"></div>
        <div class="form-row"><label>适用场景 <span class="info-tip" title="该提示词适用的业务场景，如 BR-3 需求分析 / FR-UI-5 系统提示词">ⓘ</span></label><input id="f-scenario" placeholder="如：BR-3 需求分析"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📄</span><span class="fs-title">提示词正文 <span class="fs-hint">（支持 {{变量}} 插值）</span></span></div>
        <div class="form-row"><label>内容</label><textarea id="f-content" placeholder="系统提示词内容…"></textarea></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="savePrompt()">保存</button></div>`,
  onttype: `<h3 id="ont-form-title">🧬 新增本体类型</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🔖</span><span class="fs-title">基础信息</span></div>
        <div class="form-row"><label>类型名称 <span id="ot-name-dup" style="color:var(--red);font-size:11px;"></span></label><input id="ot-name" placeholder="如：需求 / 部件 / 载荷" oninput="otCheckDup()"></div>
        <details style="margin:2px 0 6px;"><summary style="cursor:pointer;font-size:11px;color:var(--mut);user-select:none;">高级：自定义 IRI（留空=按命名空间策略自动生成）</summary>
          <div class="form-row" style="margin-top:6px;"><input id="ot-iri" placeholder="如：http://www.xingwang.mbse/ontology#需求"></div></details>
        <div class="form-row"><label>类型种类</label>
          <select id="ot-kind" onchange="ontKindChanged()"><option value="entity">实体类型（Class）</option><option value="relation">关系类型（Object Property）</option><option value="attribute">属性类型（Data Property）</option></select></div>
        <div class="form-row"><label>描述</label><textarea id="ot-desc" placeholder="类型用途/定义说明（可空）" style="min-height:44px;"></textarea></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🧬</span><span class="fs-title">类型特征</span></div>
        <div class="form-row" id="ot-parent-row" style="display:none;"><label id="ot-parent-label">父类型</label>
          <select id="ot-parent" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;" onchange="otParentHint()"></select>
          <div id="ot-parent-hint" style="font-size:11px;color:var(--mut);margin-top:3px;"></div></div>
        <div class="form-row" id="ot-abstract-row" style="display:none;"><label>抽象类型 <span class="info-tip" title="勾选后该类型不可直接创建实例，仅作分类节点">ⓘ</span></label>
          <input type="checkbox" id="ot-abstract" style="width:16px;height:16px;"></div>
        <div class="form-row" id="ot-disjoint-row" style="display:none;"><label>互斥类型 <span class="info-tip" title="disjointWith：本类型实例与所选类型实例无交集（如 人 互斥 部件）。多选=对多互斥">ⓘ</span></label>
          <select id="ot-disjoint" multiple size="4" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;width:100%;"></select><small class="multi-hint"><b>按住 Ctrl/Shift 多选</b> = 与所有选中类型互斥（disjointWith）</small></div>
        <div class="form-row" id="ot-status-row"><label>生命周期 <span class="info-tip" title="draft=草稿（仅自用）/ review=评审中（待发布）/ released=已发布（生效）/ deprecated=已弃用（被新类型替代）">ⓘ</span></label>
          <select id="ot-status" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
            <option value="draft">draft（草稿）</option>
            <option value="review">review（评审中）</option>
            <option value="released">released（已发布）</option>
            <option value="deprecated">deprecated（已弃用）</option>
          </select></div>
      </div>
      <div class="form-section" id="ot-props-section">
        <!-- 2026-09-07 方案A（Protégé/OWL 对齐）：属性全局定义（数据属性），类视图=只读引用＋局部约束覆盖 -->
        <div class="form-section-head"><span class="fs-icon">📋</span><span class="fs-title">属性（引用） <span class="fs-hint">（属性在「数据属性」统一定义，此处只做 绑定/继承 + 局部覆盖）</span></span><span style="flex:1;"></span><button class="btn sm" style="padding:1px 8px;font-size:11px;" onclick="ontEntBindExisting()" title="把已有的数据属性绑定到本类（写入其 适用类型/domain）">⇄ 绑定已有</button><button class="btn sm" style="padding:1px 8px;font-size:11px;" onclick="ontEntCreateAttr()" title="新建数据属性并预填 适用类型=本类">＋ 新建属性</button></div>
        <div id="ot-props-rows" style="display:flex;flex-direction:column;gap:4px;"></div>
      </div>
      <div class="form-section" id="ot-cons-section" style="display:none;">
        <!-- 2026-09-07 方案A：必填/唯一/白名单已内联到属性行（局部覆盖），该 section 仅保留 DOM 兼容，不再展示 -->
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">约束规则 <span class="fs-hint">（已内联到属性行）</span></span></div>
        <div class="form-row"><label>必填属性 <span class="info-tip" title="勾选为创建实例时必填（缺则校验报错）">ⓘ</span></label><select id="ot-req" multiple size="3" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:100%;"></select><small class="multi-hint"><b>按住 Ctrl/Shift 多选</b>，候选与上方「属性」行联动</small></div>
        <div class="form-row"><label>唯一属性 <span class="info-tip" title="勾选为值在所有实例中唯一（防重；OWL 导出为 sh:maxCount 1）">ⓘ</span></label><select id="ot-uniq" multiple size="3" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:100%;"></select><small class="multi-hint"><b>按住 Ctrl/Shift 多选</b>，候选与上方「属性」行联动</small></div>
        <div class="form-row"><label>取值白名单 <span class="info-tip" title="按属性填逗号分隔值；留空=不约束；实例取值将在此白名单内校验">ⓘ</span></label><div id="ot-allowed-rows" style="display:flex;flex-direction:column;gap:4px;"></div></div>
      </div>
      <div class="form-section" id="ot-relation-section" style="display:none;">
        <div class="form-section-head"><span class="fs-icon">🔗</span><span class="fs-title">关系特性</span></div>
        <div id="ot-relation-hint" style="font-size:11.5px;color:var(--amb);margin-bottom:8px;background:var(--amb-l);padding:6px 8px;border-radius:6px;line-height:1.7;">
          <b>💡 多选语义</b>：FROM/TO 多选 = 多域/多值域，SHACL 按 <code>sh:or</code> 并集校验。<br>
          <b>例</b>：CONTAINS 选 FROM={部件,载荷}，TO={系统元素,功能}，表示「部件/载荷 包含 系统元素/功能」全部合法。<br>
          <b>多选操作</b>：在 select 列表内 <kbd>Ctrl</kbd>+点击 离散多选 / <kbd>Shift</kbd>+点击 范围多选 / <kbd>Ctrl+A</kbd> 全选。
        </div>
        <div class="form-row" id="ot-domain-row"><label>源类型 FROM <span class="info-tip" title="关系起点约束的多选实体类型（按住 Ctrl/Shift 多选=多域；SHACL sh:or 并集）">ⓘ</span></label>
          <select id="ot-domain" multiple size="4" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;width:100%;"></select><small class="multi-hint"><b>按住 Ctrl/Shift 多选</b> = 多域（SHACL sh:or 并集）</small></div>
        <div class="form-row" id="ot-range-row"><label>目标类型 TO <span class="info-tip" title="关系终点约束的多选实体类型（按住 Ctrl/Shift 多选=多值域；SHACL sh:or 并集）">ⓘ</span></label>
          <select id="ot-range" multiple size="4" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;width:100%;"></select><small class="multi-hint"><b>按住 Ctrl/Shift 多选</b> = 多值域（SHACL sh:or 并集）</small></div>
        <div class="form-row" id="ot-char-row"><label>关系特性 OWL 公理 <span class="info-tip" title="OWL 公理可多选：transitive=传递（含 派生）、symmetric=对称（朋友=朋友）、reflexive=自反（属于 自己属于自己）等">ⓘ</span></label>
          <div style="display:flex;flex-wrap:wrap;gap:10px;font-size:12px;">
            <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="ot-char" value="transitive">传递</label>
            <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="ot-char" value="symmetric">对称</label>
            <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="ot-char" value="asymmetric">反对称</label>
            <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="ot-char" value="reflexive">自反</label>
            <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="ot-char" value="functional">函数型</label>
            <label style="display:flex;align-items:center;gap:4px;"><input type="checkbox" class="ot-char" value="inverse_functional">反函数型</label>
          </div></div>
        <div class="form-row" id="ot-func-row"><label>函数型关系 1:1 <span class="info-tip" title="每个源实例最多一个目标；与勾选「函数型」特性等价（保留旧 UI 入口）">ⓘ</span></label>
          <input type="checkbox" id="ot-func" style="width:16px;height:16px;"></div>
        <div class="form-row" id="ot-card-row"><label>端点基数 源 / 目标 <span class="info-tip" title="关系两端可出现实例数的允许范围（留空=0/不限）。写入后 SHACL 校验：源端出边 sh:minCount/maxCount，目标端经 sh:inversePath 基数校验。">ⓘ</span></label>
          <div style="display:flex;gap:8px;align-items:center;font-size:12px;flex-wrap:wrap;">
            <span style="color:var(--mut);">源 min</span><input type="number" id="ot-card-src-min" min="0" placeholder="0=不限" style="width:66px;border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px;">
            <span style="color:var(--mut);">源 max</span><input type="number" id="ot-card-src-max" min="0" placeholder="0=不限" style="width:66px;border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px;">
            <span style="color:var(--mut);">目标 min</span><input type="number" id="ot-card-tgt-min" min="0" placeholder="0=不限" style="width:66px;border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px;">
            <span style="color:var(--mut);">目标 max</span><input type="number" id="ot-card-tgt-max" min="0" placeholder="0=不限" style="width:66px;border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px;">
          </div></div>
      </div>
      <div class="form-section" id="ot-attr-section" style="display:none;">
        <div class="form-section-head"><span class="fs-icon">📐</span><span class="fs-title">数据属性配置</span></div>
        <div class="form-row" id="ot-dtype-row"><label>数据类型 <span class="info-tip" title="数据属性的值类型；OWL 导出 rdfs:range xsd:*；int/decimal/boolean/date/dateTime 按词表 pattern 强校验">ⓘ</span></label>
          <select id="ot-dtype" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
            <option value="string">string（字符串）</option>
            <option value="text">text（长文本）</option>
            <option value="int">int（整数）</option>
            <option value="decimal">decimal（数值）</option>
            <option value="boolean">boolean（布尔）</option>
            <option value="date">date（日期）</option>
            <option value="dateTime">dateTime（日期时间）</option>
            <option value="enum">enum（枚举）</option>
          </select></div>
        <div class="form-row" id="ot-attr-allowed-row"><label>允许值 <span class="info-tip" title="逗号分隔；留空=不约束；实例取值将按此白名单校验（仅适用类型命中时）">ⓘ</span></label>
          <input id="ot-attr-allowed" placeholder="如：V, Ka, Ku"></div>
        <div class="form-row" id="ot-attr-domain-row"><label>适用类型 <span class="info-tip" title="数据属性绑定的实体类型（OWL rdfs:domain，多选=合取）；留空=对所有类型生效；不命中时不参与 xsd/白名单校验">ⓘ</span></label>
          <select id="ot-attr-domain" multiple size="4" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;width:100%;"></select><small class="multi-hint"><b>按住 Ctrl/Shift 多选</b> = 多个适用类型（合取语义）；留空=对所有类型生效</small></div>
        <div class="form-row" id="ot-unit-row"><label>单位 / 量纲 <span class="info-tip" title="ISO 80000/SI 标准：先选「量纲类别」（如 频率），单位下拉随之级联出 SI 一贯单位与常用倍数单位（如 Hz/kHz/MHz/GHz）；也可选「自定义…」自由输入">ⓘ</span></label>
          <div style="display:flex;gap:6px;align-items:center;">
            <select id="ot-qkind" style="flex:1;min-width:120px;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;" onchange="otUnitSyncQkind()"></select>
            <input id="ot-qkind-custom" placeholder="自定义量纲类别，如 亮度" style="flex:1;display:none;"></div>
          <div style="display:flex;gap:6px;align-items:center;margin-top:4px;">
            <select id="ot-unit" style="flex:1;min-width:120px;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;" disabled></select>
            <input id="ot-unit-custom" placeholder="自定义单位，如 nT" style="flex:1;display:none;"></div></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🗂</span><span class="fs-title">外部标准映射 <span class="fs-hint">（可选）</span></span></div>
        <div class="form-row" id="ot-profile-row"><label>来源 / 引用 <span class="info-tip" title="profile 来源与引用，如 SysML V2 元类 / ISO 15288 条款 / GB-T 标准">ⓘ</span></label>
          <div style="display:flex;gap:6px;"><input id="ot-profile-src" placeholder="来源，如 SysML V2" style="flex:1;"><input id="ot-profile-ref" placeholder="引用，如 Part::PartDefinition" style="flex:1;"></div></div>
      </div>
      <!-- 2026-09-14 本体编辑影响预览（保存前由 saveOntType 调 impact-preview 填充） -->
      <div id="ot-impact-box" style="display:none;"></div>
      <div class="form-actions"><button class="btn ghost" onclick="closeOntSlide()">取消</button><button class="btn" onclick="saveOntType()">保存</button></div>`,
  docmeta: `<h3>📝 编辑文档元数据</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📄</span><span class="fs-title">文件信息</span></div>
        <div class="form-row"><label>文件</label><span id="dm2-file" style="font-size:12px;color:var(--mut);"></span></div>
        <div class="form-row"><label>上传人 / 上传时间</label><span id="dm2-uploader" style="font-size:12px;color:var(--mut);"></span></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📝</span><span class="fs-title">元数据</span></div>
        <div class="form-row"><label>标题 <span class="req">*</span></label><input id="dm2-title" placeholder="文档标题"></div>
        <div class="form-row"><label>作者</label><input id="dm2-author" placeholder="文档作者"></div>
        <div class="form-row"><label>版本 <span class="info-tip" title="建议遵循 SemVer（如 v1.0 / v1.2.3），便于追溯">ⓘ</span></label><input id="dm2-version" placeholder="v1.0"></div>
        <div class="form-row"><label>标签 <span class="info-tip" title="逗号分隔；用于检索与分类">ⓘ</span></label><input id="dm2-tags" placeholder="总体, 宽带, 方案"></div>
        <div class="form-row"><label>描述</label><textarea id="dm2-desc" placeholder="文档内容概述…"></textarea></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveDocMeta()">保存</button></div>`,
  role: `<h3 id="role-form-title">新建角色</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🎭</span><span class="fs-title">角色定义</span></div>
        <div class="form-row"><label>角色名 <span class="req">*</span></label><input id="f-name" placeholder="如：载荷审评专家"></div>
        <div class="form-row"><label>描述</label><input id="f-desc" placeholder="角色职责描述"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🛡</span><span class="fs-title">权限配置</span></div>
        <div class="form-row"><label>权限 <span class="info-tip" title="保存后自动跳转到「权限配置」页勾选操作级权限">ⓘ</span></label><span style="font-size:12px;color:var(--mut);">保存后可在「权限配置」页勾选操作级权限</span></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveRole()">保存</button></div>`,
  user: `<h3 id="user-form-title">新增用户</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">👤</span><span class="fs-title">账号信息</span></div>
        <div class="form-row"><label>用户名 <span class="req">*</span> <span class="info-tip" title="登录名（英文/数字/下划线），创建后不可修改">ⓘ</span></label><input id="f-username" placeholder="登录名"></div>
        <div class="form-row"><label>显示名 <span class="req">*</span> <span class="info-tip" title="姓名/昵称，UI 上展示用">ⓘ</span></label><input id="f-display" placeholder="姓名"></div>
        <div class="form-row"><label>工作空间 <span class="info-tip" title="留空默认 ws-{用户名}；多用户隔离工作空间">ⓘ</span></label><input id="f-ws" placeholder="留空默认 ws-{用户名}"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🏢</span><span class="fs-title">组织归属</span></div>
        <div class="form-row"><label>部门</label><select id="f-dept" onchange="if(this.value==='__manage__'){go('users');this.value='';toast('请到「部门设置」Tab 维护部门');}"><option value="">未分配</option></select> <a style="cursor:pointer;color:var(--blue-d);text-decoration:underline;font-size:11px;margin-left:6px;" onclick="showModal('department')">＋ 新增部门</a></div>
        <div class="form-row"><label>角色 <span class="req">*</span></label><select id="f-role"></select></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">账号状态</span></div>
        <div class="form-row"><label>状态 <span class="info-tip" title="启用：可登录与操作；停用：保留账号但禁止登录">ⓘ</span></label><select id="f-status"><option value="active">启用</option><option value="disabled">停用</option></select></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveUser()">保存</button></div>`,
  department: `<h3 id="dept-form-title">新增部门</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🏢</span><span class="fs-title">部门信息</span></div>
        <div class="form-row"><label>部门名称 <span class="req">*</span></label><input id="f-dname" placeholder="如：系统总体室"></div>
        <div class="form-row"><label>描述</label><input id="f-ddesc" placeholder="部门职责（可空）"></div>
        <div class="form-row"><label>排序 <span class="info-tip" title="数字越小越靠前；同级排序生效">ⓘ</span></label><input id="f-dorder" type="number" value="0" style="width:120px;"> <span style="font-size:11px;color:var(--mut);margin-left:6px;">数字越小越靠前</span></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">状态</span></div>
        <div class="form-row"><label>状态 <span class="info-tip" title="启用：在用户/角色下拉可选；停用：保留但不再可分配">ⓘ</span></label><select id="f-dstatus"><option value="active">启用</option><option value="disabled">停用</option></select></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveDepartment()">保存</button></div>`,
  llm: `<h3 id="llm-modal-title">添加模型</h3>
      <input type="hidden" id="f-edit-llm" value="">
      <input type="hidden" id="f-ptype" value="openai">
      <input type="hidden" id="f-priority" value="0">
      <input type="hidden" id="f-budget" value="0">
      <input type="hidden" id="f-tags" value="">
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🤖</span><span class="fs-title">基础信息</span></div>
        <div class="form-row"><label>模型类型 <span class="info-tip" title="对话模型=LLM（生成文本）；向量模型=Embedding（生成向量用于 RAG/检索）。⚠️ 多模态/视觉模型（如 qwen-vl-max、gpt-4o）请选「对话模型」，再勾选下方「支持图片理解」——不要在这里找 vision 类型：会话模型选择器只收对话模型，标成独立 vision 类型会导致该模型在会话里选不到。">ⓘ</span></label><select id="f-model-type" onchange="onLLMTypeChange()">
          <option value="chat">对话模型（LLM）</option>
          <option value="embedding">向量模型（Embedding）</option>
        </select></div>
        <div id="f-embed-hint" style="display:none;font-size:11.5px;color:var(--amb);background:var(--amb-l);padding:6px 8px;border-radius:6px;margin-bottom:10px;line-height:1.7;">💡 <b>BGE-M3 接入示例</b>：先用 Xinference / TEI / Ollama / vLLM 本地部署 BGE-M3（暴露 OpenAI 兼容 /embeddings），再在此配置：<br>Base URL = 服务地址，如 <code>http://localhost:9997/v1</code><br>模型名 = BGE-M3 服务名，如 <code>text-embedding-bge-m3</code></div>
        <div class="form-row"><label>名称 <b class="req">*</b> <span class="info-tip" title="展示用名，便于在模型列表中辨识">ⓘ</span></label><input id="f-name" placeholder="如：DeepSeek-V3 / text-embedding-v3"></div>
        <div class="form-row"><label>Base URL <b class="req">*</b> <span class="info-tip" title="OpenAI 兼容接口地址（结尾 /v1），如 https://api.deepseek.com/v1">ⓘ</span></label><input id="f-url" placeholder="https://api.deepseek.com/v1"></div>
        <div class="form-row"><label>API Key <b class="req">*</b> <span class="info-tip" title="服务端 API Key；保存后只显示脱敏（••••），不可再次查看明文">ⓘ</span></label><input id="f-key" type="password" placeholder="sk-..." autocomplete="off"> <span id="f-key-hint" style="display:none;font-size:11px;color:var(--mut);">已配置（••••），保存后不可修改</span></div>
        <div class="form-row"><label>模型名 <b class="req">*</b> <span class="info-tip" title="服务端真实模型标识，如 deepseek-chat / gpt-4o / text-embedding-v3">ⓘ</span></label><input id="f-model" placeholder="deepseek-chat / text-embedding-v3"></div>
        <div class="form-row"><label>图片理解 <span class="info-tip" title="勾选后，该模型可在「AI 建模」会话里接收上传的图片（架构图/连线图/截图），图片会作为多模态内容随消息一起送给模型。⚠️ 仅当模型确实支持图像输入时才勾选：勾错会把图片发给纯文本模型，上游直接报错。勾选写入能力标签 tags=vision（与工程 2026-09-04 起的前端能力判据同源）。">ⓘ</span></label><label style="display:flex;align-items:center;gap:6px;font-weight:400;"><input type="checkbox" id="f-vision" style="width:auto;"> 支持图片理解（多模态 / VL），如 qwen-vl-max、gpt-4o</label></div>
        <div class="form-row"><label>设为默认 <span class="info-tip" title="该类型的默认模型（对话/向量各自独立一个默认）">ⓘ</span></label><label style="display:flex;align-items:center;gap:6px;font-weight:400;"><input type="checkbox" id="f-is-default" style="width:auto;"> 该类型的默认模型（对话/向量各自独立一个默认）</label></div>
      </div>
      <div class="adv-box collapsed" id="f-adv">
        <div class="adv-head" onclick="document.getElementById('f-adv').classList.toggle('collapsed')"><span class="chev">▾</span>高级设置<span style="font-weight:400;color:var(--mut);font-size:11px;">上下文窗口 · 温度 · TopP · TopK · 思考模式</span></div>
        <div class="adv-body">
          <div class="form-row"><label>上下文窗口（输入）</label><input id="f-ctx" type="number" min="512" step="512" style="width:150px;" value="8192"> <span style="font-size:11px;color:var(--mut);">tokens</span></div>
          <div class="form-row"><label>上下文窗口（输出）</label><input id="f-maxt" type="number" min="1" step="128" style="width:150px;" value="8192"> <span style="font-size:11px;color:var(--mut);">tokens（≤ 输入）</span></div>
          <div class="form-row"><label>温度</label><input id="f-temp" type="number" min="0" max="2" step="0.1" style="width:150px;" value="0.3"> <span style="font-size:11px;color:var(--mut);">0~2，越小越确定</span></div>
          <div class="form-row"><label>TopP</label><input id="f-top-p" type="number" min="0" max="1" step="0.01" style="width:150px;" placeholder="留空不设置"> <span style="font-size:11px;color:var(--mut);">0~1 核采样</span></div>
          <div class="form-row"><label>TopK</label><input id="f-top-k" type="number" min="1" max="100" step="1" style="width:150px;" placeholder="1~100 整数"> <span style="font-size:11px;color:var(--mut);">整数 1~100</span></div>
          <div class="form-row"><label>思考模式</label><div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;">
            ${['模型默认','开启','关闭'].map(v=>`<label style="display:flex;align-items:center;gap:4px;font-size:12.5px;font-weight:400;margin:0;cursor:pointer;"><input type="radio" name="f-thinking" value="${v}" ${v==='模型默认'?'checked':''} style="width:auto;"> ${v}</label>`).join('')}
          </div></div>
        </div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn ghost" onclick="testLLMFromForm()" title="用当前表单配置发起连通性测试（未保存也可测）">🔌 测试</button><button class="btn" onclick="saveLLM()">保存</button></div>`,
  tool: `<h3 id="tool-modal-title">新增工具</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🔧</span><span class="fs-title">基础信息</span></div>
        <div class="form-row"><label>类型 <b class="req">*</b> <span class="info-tip" title="MCP 工具=Model Context Protocol 标准协议；HTTP 工具=自定义 REST 接口">ⓘ</span></label><select id="f-tool-type" onchange="toggleToolType()"><option value="mcp">MCP工具</option><option value="http">HTTP工具</option></select></div>
        <div class="form-row"><label>名称 <b class="req">*</b> <span class="info-tip" title="将作为运行时工具标识名">ⓘ</span></label><input id="f-tool-name" placeholder="my-mcp-server"></div>
        <div class="form-row"><label>描述</label><textarea id="f-tool-desc" rows="2" placeholder="工具功能描述…" style="min-height:44px;resize:vertical;"></textarea></div>
      </div>
      <div class="form-section" id="tool-mcp-fields">
        <div class="form-section-head"><span class="fs-icon">📡</span><span class="fs-title">MCP Server 配置</span></div>
        <div class="form-row"><label>传输方式 <b class="req">*</b> <span class="info-tip" title="生产环境请使用公网 https 端点；鉴权开启后私网/localhost URL 会被拒绝">ⓘ</span></label><select id="f-tool-transport"><option value="streamable_http">streamable_http（推荐）</option><option value="sse">SSE</option><option value="stdio">stdio</option></select></div>
        <div class="form-row"><label>URL <b class="req">*</b> <span class="info-tip" title="示例：https://mcp.example.com/mcp（勿填内网/元数据地址）">ⓘ</span></label><input id="f-tool-url-mcp" placeholder="https://mcp.example.com/mcp"></div>
      </div>
      <div class="form-section" id="tool-http-fields" style="display:none;">
        <div class="form-section-head"><span class="fs-icon">🌐</span><span class="fs-title">HTTP 接口配置</span></div>
        <div class="form-row"><label>Method <b class="req">*</b></label><select id="f-tool-method"><option value="GET">GET</option><option value="POST">POST</option><option value="PUT">PUT</option><option value="DELETE">DELETE</option><option value="PATCH">PATCH</option></select></div>
        <div class="form-row"><label>URL <b class="req">*</b> <span class="info-tip" title="路径参数写成 /users/{id}；query 不要写成 ?q={q}，用下方 schema + 默认 query/body">ⓘ</span></label><input id="f-tool-url-http" placeholder="https://api.example.com/weather/{city}"></div>
        <div class="form-row"><label>input_schema（JSON）<b class="req">*</b> <span class="info-tip" title="必须是 type=object；字段名给模型填，执行时再映射到 path/query/body">ⓘ</span></label><textarea id="f-tool-schema" style="min-height:120px;font-family:monospace;font-size:11.5px;resize:vertical;" placeholder='{\n  "type": "object",\n  "properties": {\n    "city": { "type": "string", "description": "城市名" }\n  },\n  "required": ["city"]\n}'></textarea></div>
        <div class="form-row"><label>param_in（可选 JSON） <span class="info-tip" title="如 {city:path,units:query}。留空则：URL 占位符→path，GET/DELETE 其余→query，其它→body">ⓘ</span></label><textarea id="f-tool-param-in" rows="2" style="min-height:36px;font-family:monospace;font-size:11px;resize:vertical;" placeholder='{"city":"path","units":"query"}'></textarea></div>
        <div class="form-row"><label>超时（秒）<b class="req">*</b></label><input id="f-tool-timeout" type="number" value="15" min="1" max="300"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🛡</span><span class="fs-title">容错策略 <span class="fs-hint">（仅 MCP/HTTP 集成工具生效）</span></span></div>
        <div class="form-row"><label>最大重试次数 <span class="info-tip" title="0=不重试；推荐 1-3 次，避免雪崩">ⓘ</span></label><input id="f-tool-retries" type="number" min="0" max="5" value="0"></div>
        <div class="form-row"><label>退避基数 ms / 倍数 <span class="info-tip" title="指数退避：基数 500ms × 倍数 2 → 500/1000/2000ms">ⓘ</span></label><div style="display:flex;gap:8px;"><input id="f-tool-backoff" type="number" min="100" value="500" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"> <input id="f-tool-backoff-mult" type="number" min="1" max="5" value="2" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"></div></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📨</span><span class="fs-title">Headers <span class="fs-hint">（可选）</span></span><span style="flex:1;"></span><button class="btn sm ghost" onclick="addToolHeader()" style="padding:1px 8px;font-size:11px;">＋ 添加</button></div>
        <div id="tool-headers-list" style="display:flex;flex-direction:column;gap:4px;"><div style="display:flex;gap:6px;align-items:center;"><input class="tool-hdr-key" placeholder="Key" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><input class="tool-hdr-val" placeholder="Value" style="flex:1.5;border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;"><button class="btn sm ghost" onclick="this.parentElement.remove()" style="padding:2px 8px;font-size:11px;">✕</button></div></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveTool()">保存</button></div>`,
  'mcp-detail': `<h3>🔌 MCP 服务器详情</h3>
      <div class="form-section" style="padding:14px 16px;">
        <div id="mcp-detail-body"><div class="loading">加载中…</div></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">关闭</button></div>`,
  evsub: `<h3>🔔 事件回调订阅 <span style="font-size:11px;color:var(--mut);font-weight:400;">D11 A2A 协议互通</span></h3>
      <div id="evsub-filter" style="font-size:11px;color:var(--mut);margin-bottom:8px;background:var(--blue-l);border-radius:6px;padding:6px 8px;"></div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📜</span><span class="fs-title">已订阅列表</span></div>
        <div style="max-height:260px;overflow:auto;">
          <table class="t"><tr><th>#</th><th>事件类型</th><th>范围</th><th>Webhook URL</th><th>签名</th><th>最近回调</th><th>状态</th><th>操作</th></tr>
          <tbody id="evsub-rows"></tbody></table>
        </div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">➕</span><span class="fs-title">注册新订阅</span></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;">
          <div style="display:flex;gap:6px;align-items:center;"><label style="min-width:64px;font-size:11.5px;">事件类型</label>
            <select id="ev-type" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
              <option value="node_done">node_done 节点完成</option>
              <option value="node_error">node_error 节点出错</option>
              <option value="run_completed">run_completed 运行完成</option>
            </select></div>
          <div style="display:flex;gap:6px;align-items:center;"><label style="min-width:64px;font-size:11.5px;">节点范围</label>
            <input id="ev-node" placeholder="空=全部节点" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;"></div>
          <div style="display:flex;gap:6px;align-items:center;"><label style="min-width:64px;font-size:11.5px;">Webhook URL</label>
            <input id="ev-url" placeholder="https://your-server/callback" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;"></div>
          <div style="display:flex;gap:6px;align-items:center;"><label style="min-width:64px;font-size:11.5px;">签名密钥</label>
            <input id="ev-secret" placeholder="HMAC-SHA256 密钥（可空）" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;"></div>
        </div>
        <div style="font-size:10.5px;color:var(--mut);margin-top:8px;">执行器在节点完成/出错、运行完成时，向匹配的 Webhook URL POST 标准化 A2A 事件（带 HMAC 签名）；回调失败标记订阅 failed，不阻断运行。</div>
      </div>
      <div class="form-actions"><button class="btn" onclick="addEventSub()">＋ 注册订阅</button><button class="btn ghost" onclick="closeModal()">关闭</button></div>`,
  skill: `<h3>新建 Skill</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📚</span><span class="fs-title">基础信息</span></div>
        <div class="form-row"><label>名称 <b class="req">*</b> <span class="info-tip" title="技能唯一标识，会话触发匹配的 key">ⓘ</span></label><input id="f-name" placeholder="如：get-weather"></div>
        <div class="form-row"><label>描述</label><input id="f-desc" placeholder="技能用途说明"></div>
        <div class="form-row"><label>启用 <span class="info-tip" title="启用（停用后不参与触发匹配与绑定注入）">ⓘ</span></label><label class="sw"><input type="checkbox" id="f-enabled" checked><span class="sl"></span></label></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📄</span><span class="fs-title">指令正文 <span class="fs-hint">（SKILL.md content）</span></span></div>
        <div class="form-row"><label>内容 <span class="info-tip" title="指令正文，注入到 Agent system prompt；支持 Markdown">ⓘ</span></label><textarea id="f-content" style="min-height:240px;font-family:monospace;font-size:11.5px;resize:vertical;" placeholder="当用户询问天气时，调用天气接口并返回结构化结果…"></textarea></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveSkill()">保存</button></div>`,
  agent: `<h3>新建 Agent</h3>
      <input type="hidden" id="f-name" value="">
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🤖</span><span class="fs-title">基础信息</span></div>
        <div class="form-row"><label>名称 <b class="req">*</b></label><input id="f-disp" placeholder="如：需求分析Agent"></div>
        <div class="form-row"><label>Agent 角色 <span class="info-tip" title="主 Agent 可从已设置好的子 Agent 中选择成员，组成多 Agent 团队；子 Agent 不支持再添加子 Agent">ⓘ</span></label><select id="f-role" onchange="onAgentRoleChange()">
          <option value="sub">子 Agent（团队成员）</option>
          <option value="main">主 Agent（团队负责人）</option>
        </select></div>
        <div class="form-row" id="f-team-row" style="display:none;"><label>👥 团队成员（子 Agent） <span class="info-tip" title="从子 Agent 中逐个添加（可添加多个）；主 Agent 不可作为子 Agent 添加">ⓘ</span></label>
          <div style="display:flex;gap:6px;"><select id="f-team" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;"></select><button type="button" class="btn sm" onclick="bindTeamAdd()">＋ 添加</button></div>
          <div id="f-team-chips" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;"></div>
        </div>
        <div class="form-row"><label>描述</label><textarea id="f-desc" rows="2" style="min-height:64px;resize:vertical;" placeholder="Agent 职责描述"></textarea></div>
        <div class="form-row"><label>指定模型 <span class="info-tip" title="可空=使用全局默认模型；指定后强制使用">ⓘ</span></label><select id="f-provider"><option value="">（全局默认）</option></select></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🧠</span><span class="fs-title">系统提示词</span></div>
        <div class="form-row"><label>自定义 System Prompt <span class="info-tip" title="留空使用默认模板；自定义会覆盖默认">ⓘ</span></label><textarea id="f-sp" style="min-height:136px;resize:vertical;" placeholder="留空使用默认模板"></textarea></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🔗</span><span class="fs-title">能力绑定</span></div>
        <div class="form-row"><label>绑定技能 Skills <span class="info-tip" title="选择技能后点「＋ 添加」（可多个）；Skill=配方（指令注入），会话按绑定列表消费">ⓘ</span></label>
          <div style="display:flex;gap:6px;"><select id="f-bind-skills" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;"></select><button type="button" class="btn sm" onclick="bindAdd('skill')">＋ 添加</button></div>
          <div id="f-bind-skills-chips" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;"></div>
        </div>
        <div class="form-row"><label>绑定 MCP 服务器 <span class="info-tip" title="MCP=工具目录（catalog 调用），需先测试在线">ⓘ</span></label>
          <div style="display:flex;gap:6px;"><select id="f-bind-mcps" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;"></select><button type="button" class="btn sm" onclick="bindAdd('mcp')">＋ 添加</button></div>
          <div id="f-bind-mcps-chips" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;"></div>
        </div>
        <div class="form-row"><label>绑定工具 Tools <span class="info-tip" title="tool=内置处理器">ⓘ</span></label>
          <div style="display:flex;gap:6px;"><select id="f-bind-tools" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;"></select><button type="button" class="btn sm" onclick="bindAdd('tool')">＋ 添加</button></div>
          <div id="f-bind-tools-chips" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;"></div>
        </div>
        <div class="form-row"><label>绑定插件 Plugins <span class="info-tip" title="插件市场能力（Skill/MCP 统一），绑定后运行时自动展开为可触发技能/MCP">ⓘ</span></label>
          <div style="display:flex;gap:6px;"><select id="f-bind-plugins" style="flex:1;border:1px solid var(--line);border-radius:6px;padding:4px;font-size:12px;"></select><button type="button" class="btn sm" onclick="bindAdd('plugin')">＋ 添加</button></div>
          <div id="f-bind-plugins-chips" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;"></div>
        </div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveAgent()">保存</button></div>`,
  projmem: `<h3 id="pm-form-title">新增项目记忆</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📌</span><span class="fs-title">分类与标题</span></div>
        <div class="form-row"><label>类别 <span class="info-tip" title="📐 规范=约定规则；📌 基线=已确认的事实；🧭 决策=关键选择及理由；💡 经验=踩坑教训">ⓘ</span></label><select id="f-pm-category"><option value="规范">📐 规范</option><option value="基线">📌 基线</option><option value="决策">🧭 决策</option><option value="经验">💡 经验</option></select></div>
        <div class="form-row"><label>标题 <span class="req">*</span></label><input id="f-pm-title" placeholder="如：SysML v2 建模规范"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📄</span><span class="fs-title">内容</span></div>
        <div class="form-row"><label>内容 <span class="info-tip" title="项目规范/基线/决策/经验内容；AI 会话每次自动注入 system prompt">ⓘ</span></label><textarea id="f-pm-content" style="min-height:120px;font-family:monospace;font-size:11px;" placeholder="项目规范/基线/决策/经验内容…（AI 会话每次自动注入 system prompt）"></textarea></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">启用状态</span></div>
        <div class="form-row"><label>启用 <span class="info-tip" title="启用后该条目自动注入到 AI 会话 system prompt；停用则不再注入">ⓘ</span></label><select id="f-pm-enabled"><option value="1">启用（会话自动注入）</option><option value="0">停用</option></select></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveProjectMemory()">保存</button></div>`,
  hook: `<h3 id="hook-form-title">新增工具钩子</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🎣</span><span class="fs-title">基础配置</span></div>
        <div class="form-row"><label>名称 <span class="req">*</span> <span class="info-tip" title="钩子唯一标识，便于在审计日志中检索">ⓘ</span></label><input id="f-hook-name" placeholder="如：禁止删除知识实体"></div>
        <div class="form-row"><label>匹配工具 <span class="req">*</span> <span class="info-tip" title="精确名、前缀（file_*）、后缀（*_delete）或包含（*xxx*）">ⓘ</span></label><input id="f-hook-pattern" placeholder="如：entity_delete / file_* / *_delete" title="精确名、前缀（file_*）、后缀（*_delete）或包含（*xxx*）"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">触发行为</span></div>
        <div class="form-row"><label>动作 <span class="info-tip" title="⛔ block=拦截拒绝；🙋 require_confirm=进入人工确认队列；⚠️ warn=放行+提示">ⓘ</span></label><select id="f-hook-action"><option value="block">⛔ block 拦截</option><option value="require_confirm">🙋 require_confirm 人工确认</option><option value="warn">⚠️ warn 放行+提示</option></select></div>
        <div class="form-row"><label>参数条件 <span class="info-tip" title="可选：校验 arguments[key] 是否包含/等于指定值，空=不过滤">ⓘ</span></label><input id="f-hook-condition" placeholder='{"key":"path","contains":".sysml"}' title="可选：校验 arguments[key] 是否包含/等于指定值，空=不过滤"></div>
        <div class="form-row"><label>提示文案 <span class="info-tip" title="命中钩子时的提示文案（block/确认队列展示）">ⓘ</span></label><input id="f-hook-message" placeholder="命中钩子时的提示（block/确认队列展示）"></div>
        <div class="form-row"><label>描述</label><input id="f-hook-desc" placeholder="钩子用途说明（可空）"></div>
        <div class="form-row"><label>启用 <span class="info-tip" title="启用：触发检查；停用：跳过不执行">ⓘ</span></label><select id="f-hook-enabled"><option value="1">启用</option><option value="0">停用</option></select></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveToolHook()">保存</button></div>`,
  branch: `<h3 id="branch-form-title">🌿 创建分支</h3>
      <input type="hidden" id="f-edit-branch" value="">
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🌿</span><span class="fs-title">分支基础</span></div>
        <div class="form-row"><label>分支名 <span class="req">*</span> <span class="info-tip" title="字母/数字/_/-/.；如 dev/zhangsan-bugfix">ⓘ</span></label><input id="f-name" placeholder="如：dev/feature-x"></div>
        <div class="form-row"><label>类型 <span class="info-tip" title="personal=个人分支（从 dev/release 拉取基线，可合并回 dev）；local=本地分支（离线/实验，不可合并）">ⓘ</span></label><select id="f-type" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;">
          <option value="personal">个人（从 dev/release 拉取基线，可合并回 dev）</option>
          <option value="local">本地（离线/实验，不可合并）</option>
        </select></div>
        <div class="form-row"><label>描述</label><input id="f-desc" placeholder="分支用途说明（可空）"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📥</span><span class="fs-title">基线来源 <span class="fs-hint">（拉取数据）</span></span></div>
        <div class="form-row" id="f-parent-row"><label>基线来源 <span class="info-tip" title="dev=主开发分支；release=已发布分支">ⓘ</span></label><select id="f-parent" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;"><option value="dev">dev（主开发分支）</option><option value="release">release（发布分支）</option></select> <span style="font-size:11px;color:var(--mut);margin-left:6px;">创建时将复制基线实体/关系到个人分支</span></div>
      </div>
      <div class="form-section" style="display:none;">
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">状态</span></div>
        <div class="form-row" id="f-status-row"><label>状态</label><select id="f-status"><option value="active">active（使用中）</option><option value="archived">archived（已归档）</option></select></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveBranch()">创建</button></div>`,
  merge: `<h3>🔀 新建合并请求</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🔀</span><span class="fs-title">合并请求</span></div>
        <div class="form-row"><label>标题 <span class="info-tip" title="可空；留空将使用源分支名作默认名；对标 GitHub PR title">ⓘ</span></label><input id="f-mr-title" placeholder="合并请求标题（对标 GitHub PR title）"></div>
        <div class="form-row"><label>源分支 <span class="req">*</span> <span class="info-tip" title="数据从哪来（个人分支 → 合并到 dev/release）">ⓘ</span></label><select id="f-src" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;"></select></div>
        <div class="form-row"><label>目标分支 <span class="req">*</span> <span class="info-tip" title="合并到哪去（dev=主开发；release=正式发布）">ⓘ</span></label><select id="f-tgt" style="border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;"></select></div>
        <div class="form-row"><label>发布版本号 <span class="info-tip" title="仅当目标=release 时生效；留空自动生成 v1/v2…">ⓘ</span></label><input id="f-release-version" placeholder="留空自动生成 v1/v2…"><span id="f-rv-hint" style="font-size:11px;color:var(--mut);margin-left:6px;">（仅目标=release 时生效）</span></div>
        <div class="form-row"><label class="fs-mode"><input type="checkbox" id="f-mr-draft"> 创建为草稿 <span class="info-tip" title="draft=草稿（稍后转正式评审；不会触发合并门禁）">ⓘ</span></label></div>
      </div>
      <div id="merge-preview" style="font-size:11.5px;color:var(--mut);background:var(--blue-l);padding:8px 10px;border-radius:6px;display:none;">选择分支后显示合并预览</div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveMerge()">创建</button></div>`,
  approval_type: `<h3 id="apt-form-title">＋ 新增审批类型</h3>
      <input type="hidden" id="apt-id" value="">
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📋</span><span class="fs-title">类型信息</span></div>
        <div class="form-row"><label>类型名称 <span class="req">*</span></label><input id="apt-name" placeholder="如：报销审批"></div>
        <div class="form-row"><label>图标 <span class="info-tip" title="emoji 图标；用于列表与卡片展示">ⓘ</span></label><input id="apt-icon" placeholder="如：📦" style="width:90px;"></div>
        <div class="form-row"><label>适用场景 <span class="info-tip" title="该审批类型适用的业务场景（如 报销/采购/发布等）">ⓘ</span></label><textarea id="apt-scenario" placeholder="描述该审批类型适用的业务场景…"></textarea></div>
        <div class="form-row"><label>说明</label><input id="apt-desc" placeholder="补充说明（可空）"></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">⚙️</span><span class="fs-title">状态</span></div>
        <div class="form-row"><label>启用 <span class="info-tip" title="启用：流程模板可关联；停用：保留但不可选用">ⓘ</span></label><select id="apt-enabled"><option value="1">启用</option><option value="0">停用</option></select></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveApprovalType()">保存</button></div>`,
  approval_tpl: `<h3 id="aptpl-form-title">＋ 新建流程模板</h3>
      <input type="hidden" id="aptpl-id" value="">
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📜</span><span class="fs-title">模板信息</span></div>
        <div class="form-row"><label>模板名称 <span class="req">*</span></label><input id="aptpl-name" placeholder="如：发布审批·两级"></div>
        <div class="form-row"><label>关联审批类型 <span class="req">*</span> <span class="info-tip" title="该模板对应的审批类型（来自审批类型列表）">ⓘ</span></label><select id="aptpl-type"></select></div>
        <div class="form-row"><label>描述</label><input id="aptpl-desc" placeholder="模板用途说明（可空）"></div>
        <div class="form-row"><label>启用 <span class="info-tip" title="启用：可被发起审批；停用：保留但不可选用">ⓘ</span></label><select id="aptpl-enabled"><option value="1">启用</option><option value="0">停用</option></select></div>
      </div>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">🛤</span><span class="fs-title">流程节点 <span class="fs-hint">（按顺序执行，首节点自动进入审批流）</span></span><span style="flex:1;"></span><button class="btn sm" style="padding:1px 8px;font-size:11px;" onclick="apTplAddNode()">＋ 添加节点</button></div>
        <div id="aptpl-nodes" style="display:flex;flex-direction:column;gap:6px;"></div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveApprovalTpl()">保存</button></div>`,
  owlImport: `<h3 id="oi-title">⬆ 导入 OWL · RDF/XML</h3>
      <div class="form-section">
        <div class="form-section-head"><span class="fs-icon">📥</span><span class="fs-title">导入内容</span></div>
        <div class="form-row"><label id="oi-tip" style="display:block;color:var(--mut);font-size:11.5px;margin-bottom:4px;">粘贴 OWL/RDF-XML 内容（同名类型跳过幂等），或点击下方「📤 上传文件」载入 .owl/.rdf/.xml</label>
          <textarea id="oi-text" rows="10" placeholder="&lt;rdf:RDF xmlns:owl=&quot;http://www.w3.org/2002/07/owl#&quot; ...&gt;"></textarea>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin:-4px 0 0;">
          <input type="file" id="oi-file" style="display:none;" onchange="oiFileChosen(this)">
          <button class="btn sm ghost" onclick="document.getElementById('oi-file').click()" title="选择 OWL/RDF 文件，内容自动载入输入框">📤 上传文件</button>
          <span id="oi-file-name" style="font-size:11px;color:var(--mut);"></span>
        </div>
      </div>
      <div class="form-actions">
        <button class="btn ghost" onclick="closeModal()">取消</button>
        <button class="btn" onclick="oiDoImport()">⬆ 开始导入</button>
      </div>`,
  blueprint: `<h3>📝 本体蓝图（Schema 冷启动统一入口）</h3>      <div class="form-row"><label>输入方式</label>
        <span style="display:flex;gap:6px;align-items:center;">
          <span class="st on" id="bp-src-text" onclick="bpSetSource('text')" style="cursor:pointer;background:var(--blue-l);color:var(--blue-d);padding:2px 10px;border-radius:10px;">📄 业务文本</span>
          <span class="st" id="bp-src-profile" onclick="bpSetSource('profile')" style="cursor:pointer;padding:2px 10px;border-radius:10px;">🧬 SysML Profile</span>
          <span style="font-size:11px;color:var(--mut);">文本走 LLM 提炼（规则兜底）；Profile 走结构化解析（1.x XMI / 2.x KerML）——统一草案 → 勾选 → 应用</span>
        </span>
      </div>
      <div id="bp-profile-fmt" style="display:none;">
        <div class="form-row"><label>Profile 格式</label>
          <select id="bp-fmt" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;">
            <option value="1x">SysML 1.x（XMI .profile/.xmi/.uml）</option>
            <option value="v2">SysML 2.x（KerML .kerml）</option>
          </select>
        </div>
      </div>
      <div class="form-row"><label id="bp-input-label">业务文档 / 模型描述文本</label>
        <textarea id="bp-text" rows="6" placeholder="粘贴业务文档、DDL 或模型描述文本，LLM 提炼实体/关系/属性草案（Mock 模式按「X 是 Y 的子类」「定义 X 类型」规则提取）…"></textarea>
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin:-6px 0 8px;">
        <input type="file" id="bp-file" style="display:none;" onchange="bpFileChosen(this)">
        <button class="btn sm ghost" onclick="document.getElementById('bp-file').click()" title="选择文本类文件（.txt/.md/.csv/.json/.xml/.yaml/.log；Profile 模式支持 .profile/.xmi/.uml/.kerml），内容自动载入上方输入框">📤 上传文件</button>
        <span id="bp-file-name" style="font-size:11px;color:var(--mut);"></span>
      </div>
      <div class="form-row">
        <button class="btn" onclick="blueprintExtract()">✨ 提取草案</button>
        <button class="btn ghost" onclick="blueprintApply()">✅ 应用勾选项</button>
        <span style="font-size:11px;color:var(--mut);margin-left:6px;">应用校验：重名类型 / 父类型不存在会被拦截</span>
      </div>
      <div id="bp-drafts" style="max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:8px;">
        <div style="padding:10px;color:var(--mut);font-size:12px;">粘贴内容并「✨ 提取草案」后，在此勾选确认应用</div>
      </div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">关闭</button></div>`,
  unifiedPlugin: `<h3 id="up-form-title">🧩 新建统一插件</h3>
      <div class="form-row"><label>manifest.json <span style="color:var(--mut);font-size:11px;">唯一契约：id(反向域名)/name/version(semver)/type(skill|mcp|bundle)</span></label>
        <textarea id="up-manifest" rows="14" style="font-family:monospace;font-size:11.5px;" placeholder='{
  "id": "com.yourcompany.requirement-checker",
  "name": "requirement-checker",
  "version": "0.1.0",
  "type": "skill",
  "description": "需求条目规范性检查",
  "label": { "name": "需求检查器", "category": "需求工程" },
  "capabilities": { "skills": [{ "path": "SKILL.md" }] },
  "permissions": { "tools": [], "network": { "domains": [] } }
}'></textarea></div>
      <div class="form-row"><label>分类 <span style="color:var(--mut);font-size:11px;">写回 label.category；填新分类时市场 chip 会自动出现</span></label>
        <input id="up-category" list="up-category-list" placeholder="如：需求工程 / 内置工具 / 我的能力">
        <datalist id="up-category-list"></datalist></div>
      <div class="form-row" id="up-agent-role-row" style="display:none;"><label>Agent 角色 <span style="color:var(--mut);font-size:11px;">决定市场里的「编排 Agent（主）/ 专业 Agent（子）」上下分栏</span></label>
        <select id="up-agent-role"><option value="sub">子 Agent（专业，承担具体任务）</option><option value="main">主 Agent（编排，分派子任务）</option></select></div>
      <div id="up-skill-row" class="form-row"><label>SKILL.md（skill 类型载荷，可选）</label>
        <textarea id="up-skill-md" rows="6" style="font-family:monospace;font-size:11.5px;" placeholder="---&#10;name: requirement-checker&#10;description: 需求条目规范性检查&#10;---&#10;&#10;检查需求条目是否满足 EARS 规范…"></textarea></div>
      <div id="up-mcp-row" class="form-row" style="display:none;"><label>server.json（mcp 类型载荷，可选；base_url 与凭证解耦）</label>
        <textarea id="up-mcp-server" rows="6" style="font-family:monospace;font-size:11.5px;" placeholder='{"base_url": "https://mcp.example.com", "headers": {}}'></textarea></div>
      <div class="form-actions"><button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" onclick="saveUnifiedPlugin()">💾 保存</button></div>`,
};
function closeModal() { _mmCreate = null; document.getElementById('modal').classList.remove('show'); }
// 非阻塞确认对话框（Promise）——替代原生 confirm()（同步阻塞会卡死页面/自动化环境）
// 轻量级操作确认：居中卡片弹窗（#lbx），区别于右侧 mbox 业务表单
function confirmDialog(msg, opts={}) {
  const lbx = document.getElementById('lbx');
  const b = document.getElementById('lbx-body');
  b.innerHTML = `<h3>${opts.title||'操作确认'}</h3>
    <div style="font-size:13px;color:var(--txt);line-height:1.7;margin:4px 0 6px;max-height:200px;overflow:auto;white-space:pre-wrap;">${esc(msg)}</div>
    <div class="lbx-actions"><button class="btn ghost" id="cd-cancel">取消</button><button class="btn red" id="cd-ok">${opts.okText||'确认'}</button></div>`;
  lbx.classList.add('show');
  return new Promise(resolve => {
    const done = v => { lbx.classList.remove('show'); document.removeEventListener('keydown', _kd); resolve(v); };
    document.getElementById('cd-ok').onclick = () => done(true);
    document.getElementById('cd-cancel').onclick = () => done(false);
    // G4 键盘可达：Enter 确认 / Esc 取消
    const _kd = e => {
      if(e.key === 'Enter') done(true);
      else if(e.key === 'Escape') done(false);
    };
    document.addEventListener('keydown', _kd);
    setTimeout(()=>{ const ok = document.getElementById('cd-ok'); if(ok) ok.focus(); }, 30);
    // 遮罩点击 / 右上角 ✕ 均视为取消
    lbx.onclick = e => { if(e.target===lbx) done(false); };
    const cb = document.querySelector('#lbx .lbx-close');
    if(cb) cb.onclick = () => done(false);
  });
}
// ── 非阻塞多选列表对话框（Promise）——固定选项的多选场景（知识类别等）──
// 返回选中值数组；取消返回 null
function multiSelectDialog({title='选择', message='', options=[], selected=[], okText='确定'}={}) {
  const lbx = document.getElementById('lbx');
  const b = document.getElementById('lbx-body');
  b.innerHTML = `<h3>${esc(title)}</h3>
    ${message?`<div style="font-size:12.5px;color:var(--mut);line-height:1.6;margin:4px 0 10px;">${esc(message)}</div>`:''}
    <div style="max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px 12px;display:flex;flex-direction:column;gap:6px;">
      ${options.map(o=>`<label style="display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;user-select:none;">
        <input type="checkbox" class="msd-opt" value="${esc(o)}" ${selected.includes(o)?'checked':''} style="width:15px;height:15px;">${esc(o)}</label>`).join('')
      || '<span style="color:var(--mut);font-size:12px;">无可选项（可取消后留空收编）</span>'}
    </div>
    <div style="margin-top:6px;font-size:11px;color:var(--mut);">可多选；全部不勾选 = 未分类</div>
    <div class="lbx-actions"><button class="btn ghost" id="msd-cancel">取消</button><button class="btn" id="msd-ok">${okText}</button></div>`;
  lbx.classList.add('show');
  return new Promise(resolve => {
    const done = v => { lbx.classList.remove('show'); resolve(v); };
    document.getElementById('msd-ok').onclick = () => {
      done([...document.querySelectorAll('#lbx .msd-opt:checked')].map(x=>x.value));
    };
    document.getElementById('msd-cancel').onclick = () => done(null);
    lbx.onclick = e => { if(e.target===lbx) done(null); };
    const cb = document.querySelector('#lbx .lbx-close');
    if(cb) cb.onclick = () => done(null);
  });
}
// ── 非阻塞输入对话框（Promise）——替代原生 prompt()（同步阻塞会卡死页面/自动化环境）──
// 轻量级输入：居中卡片弹窗（#lbx），单行 input / 多行 textarea
function promptDialog({title='输入', message='', value='', placeholder='', okText='确定', multiline=false, rows=4}={}) {
  const lbx = document.getElementById('lbx');
  const b = document.getElementById('lbx-body');
  const inputId = 'pd-input';
  b.innerHTML = `<h3>${esc(title)}</h3>
    ${message?`<div style="font-size:12.5px;color:var(--mut);line-height:1.6;margin:4px 0 10px;white-space:pre-wrap;max-height:140px;overflow:auto;">${esc(message)}</div>`:''}
    ${multiline
      ? `<textarea id="${inputId}" placeholder="${esc(placeholder)}" style="width:100%;min-height:${rows*22}px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12.5px;font-family:inherit;box-sizing:border-box;resize:vertical;">${esc(value)}</textarea>`
      : `<input id="${inputId}" value="${esc(value)}" placeholder="${esc(placeholder)}" style="width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12.5px;box-sizing:border-box;">`}
    <div class="lbx-actions"><button class="btn ghost" id="pd-cancel">取消</button><button class="btn" id="pd-ok">${okText}</button></div>`;
  lbx.classList.add('show');
  return new Promise(resolve => {
    const done = v => { lbx.classList.remove('show'); document.removeEventListener('keydown', _kd); resolve(v); };
    const val = () => document.getElementById(inputId).value;
    document.getElementById('pd-ok').onclick = () => done(val());
    document.getElementById('pd-cancel').onclick = () => done(null);
    // 键盘可达：Enter 确认（多行时换行）/ Esc 取消
    const _kd = e => {
      if(e.key === 'Escape') done(null);
      else if(e.key === 'Enter' && !multiline) done(val());
    };
    document.addEventListener('keydown', _kd);
    setTimeout(()=>{ const i = document.getElementById(inputId); if(i) i.focus(); }, 30);
    lbx.onclick = e => { if(e.target===lbx) done(null); };
    const cb = document.querySelector('#lbx .lbx-close');
    if(cb) cb.onclick = () => done(null);
  });
}
// ── 本体类型/关系：右侧滑动窗（取代居中的 onttype modal）──

/* ── 2026-09-23 FR-UR-1：登录态管理（登录页 /static/login.html 配套）──
   顶栏「登录/登出」按钮逻辑：未登录→跳登录页；已登录→调登出 API 并清会话。 */
window.loginEntryClick = async function() {
  const st = localStorage.getItem('mbse_session');
  if (!st) { location.href = '/static/login.html'; return; }
  try { await fetch(API + '/api/auth/logout', { method: 'POST', headers: { 'X-Session-Token': st } }); } catch (e) {}
  localStorage.removeItem('mbse_session');
  localStorage.removeItem('mbse_user_id');
  location.href = '/static/login.html';
};
// 启动时刷新按钮文案（登录/登出）与问候行
(async function refreshLoginEntry() {
  const st = localStorage.getItem('mbse_session');
  const el = document.getElementById('login-entry');
  if (!el) return;
  if (!st) { el.textContent = '登录'; return; }
  try {
    const me = await (await fetch(API + '/api/auth/me', { headers: { 'X-Session-Token': st } })).json();
    if (me.authenticated && me.user) {
      el.textContent = '登出（' + (me.user.display_name || '') + '）';
    } else {
      localStorage.removeItem('mbse_session'); el.textContent = '登录';
    }
  } catch (e) { el.textContent = '登录'; }
})();
