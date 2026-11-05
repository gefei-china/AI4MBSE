/* 门禁：会话入口「模型切换面板」的真实渲染与数据接线（2026-10-31）。

为什么必须验渲染而不是只 grep 函数名：
  本仓记录过同族事故 —— JS 里函数写好了、但对应 DOM 容器在 index.html 里不存在，
  于是线上**永远不可达**，而静态检查全绿。
  这里把 index.html 里的真实容器结构 + 真实的 13-reports.js 一起加载，
  真跑 renderLLMPicker()，断言产出的 HTML 里**逐个模型**都带出了
  描述 / 上下文档位 / 思考强度 / 标签，并且 ⚙ 能定位到该模型的 id。

同时验三件事：
  A. index.html 里确实存在 #llm-pick-btn / #llm-pick-panel / 隐藏的 #llm-providers
  B. 上下文档位换算正确（1048576 → 1M、614400 → 600K、307200 → 300K）
  C. 选中态与写回：pickLLMModel(id) 必须把值写回隐藏 select（单一事实源）

用法：node tools/verify/verify_llm_picker_ui.js
退出码 0 = 全绿；1 = 有红项。
*/
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const JS_FILE = path.join(ROOT, 'static', 'js', 'mods', '13-reports.js');
const HTML_FILE = path.join(ROOT, 'static', 'index.html');

const RESULTS = [];
function check(group, name, cond, detail) {
  RESULTS.push({ group, name, ok: !!cond, detail: detail || '' });
}

// ── A. 容器存在性（直接读真实 index.html 文本）──
const html = fs.readFileSync(HTML_FILE, 'utf8');
check('DOM', '存在 #llm-pick-btn（入口按钮）', html.includes('id="llm-pick-btn"'));
check('DOM', '存在 #llm-pick-panel（模型列表面板）', html.includes('id="llm-pick-panel"'));
check('DOM', '存在隐藏的 #llm-providers（真值单一事实源）', /id="llm-providers"/.test(html));
check('DOM', '隐藏 select 仍保留「默认模型」选项', html.includes('<option value="">默认模型</option>'));

// ── B/C. 加载真实 JS，mock 最小 DOM，跑真函数 ──
const els = {};
function mkEl(id, tag) {
  return {
    id, tagName: tag || 'DIV', style: {}, dataset: {}, _html: '', value: '',
    options: [], children: [], textContent: '', checked: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    contains() { return false; },
    appendChild() {},
    dispatchEvent() {},
  };
}
['llm-providers', 'llm-pick-panel', 'llm-pick-label', 'llm-pick-wrap'].forEach(id => { els[id] = mkEl(id); });
// select 需要 options 数组（pickLLMModel / _providerLabelOf 依赖）
els['llm-providers'].options = [
  { value: '', textContent: '默认模型' },
  { value: '71', textContent: 'deepseek-v4-flash · deepseek-v4-flash' },
  { value: '104', textContent: 'qwen3.8-max · qwen3.8-max' },
];

const sandbox = {
  console,
  setTimeout, clearTimeout,
  document: {
    getElementById: (id) => els[id] || null,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    createElement: (t) => mkEl('tmp', t),
    body: { appendChild() {} },
  },
  window: {},
  esc: (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
  api: async () => { throw new Error('本门禁不触网'); },
  toast: () => {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
  Blob: function () {},
  Event: function () {},
};
vm.createContext(sandbox);
const src = fs.readFileSync(JS_FILE, 'utf8');
vm.runInContext(src, sandbox, { filename: '13-reports.js' });

// ══ 通用 helper（所有分组都要用，故定义在最前）════════════════════════════════
// ⚠️ 必须用 runInContext 赋缓存：`let _chatProvidersCache` 是 context 的**词法绑定**，
//    `sandbox._chatProvidersCache = ...` 只写到 sandbox 属性上，被测函数读到仍是旧值
//    ⇒ 症状"面板渲染 0 行"，与"功能没写"完全一致（本仓踩过多次）。
function setCache(arr) {
  sandbox.__CACHE__ = arr;
  vm.runInContext('_chatProvidersCache = __CACHE__;', sandbox);
}
// ★★ 2026-11-01（二级树）：设置默认**收起**，凡要看设置控件的断言都必须先展开该模型。
//   不展开会得到"面板里没有下拉"的假红 —— 那是结构变了，不是功能没了。
function peekModel(pid, cache, convModel) {
  if (cache) setCache(cache);
  els['llm-providers'].value = String(pid);
  sandbox.__EP__ = pid;
  vm.runInContext('_expandedPid = __EP__;', sandbox);
  if (convModel) { sandbox.__CM__ = convModel; vm.runInContext('_convLlm = __CM__;', sandbox); }
  sandbox.renderLLMPicker();
  return els['llm-pick-panel'].innerHTML;
}
// 收起态渲染（用于验证"默认收起"与行级信息，如「本会话已设」标记）
function peekCollapsed(pid, cache, convModel) {
  if (cache) setCache(cache);
  els['llm-providers'].value = String(pid);
  vm.runInContext('_expandedPid = null;', sandbox);
  if (convModel) { sandbox.__CM__ = convModel; vm.runInContext('_convLlm = __CM__;', sandbox); }
  sandbox.renderLLMPicker();
  return els['llm-pick-panel'].innerHTML;
}

// 构造两个模型：一个 1M + 高思考 + 有描述标签；一个 600K + 关闭思考 + 无描述
// ⚠️ 必须用 runInContext 赋值，不能 `sandbox._chatProvidersCache = [...]`：
//    JS 文件里的 `let _chatProvidersCache` 是**context 的词法绑定**，
//    而 sandbox 对象属性是另一套名字空间 ⇒ 直接赋值会让被测函数读到空数组，
//    症状是"面板渲染出 0 行"，与"功能没写"完全一致（本仓踩过的验证器陷阱）。
const MODELS = [
  {
    id: 71, name: 'deepseek-v4-flash', model_name: 'deepseek-v4-flash', model_type: 'chat',
    status: 'active', is_default: 1, context_window: 1048576,
    description: '通用对话，响应快、成本低，适合日常问答',
    tags: ['code', 'long-ctx'],
    model_params: { thinking_level: '高', thinking_mode: '开启' },
  },
  {
    id: 104, name: 'qwen3.8-max', model_name: 'qwen3.8-max', model_type: 'chat',
    status: 'active', is_default: 0, context_window: 614400,
    description: '', tags: [],
    model_params: { thinking_mode: '关闭' },   // 老数据只有 thinking_mode ⇒ 应回退显示「关闭」
  },
];
sandbox.__MODELS__ = MODELS;
vm.runInContext('_chatProvidersCache = __MODELS__;', sandbox);
els['llm-providers'].value = '71';            // 模拟当前选中

sandbox.renderLLMPicker();
const out = els['llm-pick-panel'].innerHTML;

check('渲染', '面板输出了两个模型行', (out.match(/llm-pick-row/g) || []).length === 2, 'rows=' + (out.match(/llm-pick-row/g) || []).length);
check('渲染', '带出模型描述', out.includes('通用对话，响应快、成本低'));
check('渲染', '上下文档位换算：1M', out.includes('🗂 1M'));
check('渲染', '上下文档位换算：600K', out.includes('🗂 600K'));
check('渲染', '思考强度显示「高」（新字段 thinking_level）', out.includes('🧠 思考：高'));
check('渲染', '老数据回退显示「关闭」（仅 thinking_mode）', out.includes('🧠 思考：关闭'));
check('渲染', '能力标签以徽章展示', out.includes('>code<') && out.includes('>long-ctx<'));
check('渲染', '默认模型打了「默认」标记', out.includes('llm-pick-tag def'));
check('渲染', '当前选中行有 on 类', /llm-pick-row on/.test(out));
// ★ 判据反转（2026-10-31）：原为「每行都有 ⚙ 设置入口且带该模型 id」。
//   该入口跳 editLLM(id) = 打开模型配置弹窗 ⇒ 在会话里点一下改的是**系统默认**，
//   与会话设置语义相反，已物理删除。判据随之反转（而不是删掉这条）：
//   删掉 = 这个维度不再受保护，下次又会有人加回来。
check('渲染', '**每行不再有 ⚙ 编辑入口**（防止会话内误改系统默认）',
      !out.includes('llm-pick-cfg') && !out.includes('editLLM'));
check('渲染', '点行即选该模型（pickLLMModel 带 id）', out.includes('pickLLMModel(71)'));

// 档位换算函数本身
check('档位', 'fmtCtxTier(1048576)=1M', sandbox.fmtCtxTier(1048576) === '1M');
check('档位', 'fmtCtxTier(614400)=600K', sandbox.fmtCtxTier(614400) === '600K');
check('档位', 'fmtCtxTier(307200)=300K', sandbox.fmtCtxTier(307200) === '300K');
check('档位', 'fmtCtxTier(112000) 归一到 K', sandbox.fmtCtxTier(112000) === '109K', sandbox.fmtCtxTier(112000));

// 写回单一事实源
sandbox.pickLLMModel(104);
check('写回', 'pickLLMModel 把值写回隐藏 select', String(els['llm-providers'].value) === '104', 'value=' + els['llm-providers'].value);

// 标签文本
sandbox.syncLLMPickerLabel();
check('写回', '按钮文案跟随当前模型名', els['llm-pick-label'].textContent === 'qwen3.8-max', els['llm-pick-label'].textContent);

// ── 会话 × 模型 设置（二级树，2026-11-01）────────────────────────────────
// 判据沿革（别改回去）：
//   ① 2026-10-31 第一版验「本轮临时 / 仅对下一次生效」= 单轮语义，已废弃；
//   ② 同日改为「会话维度」；③ 2026-11-01 再改为「**会话 × 模型**」，且设置改成
//      挂在每个模型行后面的**二级树**（米爸：不要放在模型列表上方）。
//   ⇒ 判据必须跟着结构走：现在要"先展开该行"才看得到设置控件。
const SESS_MODELS = [
  { id: 71, name: 'deepseek-v4-flash', model_name: 'deepseek-v4-flash', model_type: 'chat',
    status: 'active', is_default: 1, context_window: 131072, description: '', tags: [],
    model_params: { thinking_control: 'tier', thinking_level: '模型默认' },
    capability: { supported: true, level: 'full', levels: ['关闭','低','中','高'],
                  default_level: '模型默认', reason: '', context: { supported: true, reason: '' } } },
];
const out2 = peekModel(71, SESS_MODELS, { byModel: {}, systemDefault: {}, providerId: 71, loadedFor: 1 });
check('会话', '二级面板标明是「本会话 · 该模型」的设置', out2.includes('本会话'));
check('会话', '明示「不回写模型配置」', out2.includes('不回写模型配置'));
check('会话', '**不再**出现「仅对下一次发送生效」（单轮语义已废弃）',
      !out2.includes('仅对下一次发送生效'));
check('会话', '思考强度可选项含 关闭/低/中/高',
      ['>关闭<', '>低<', '>中<', '>高<'].every(s => out2.includes(s)));
check('会话', '上下文档位含 1M/600K/300K', ['>1M<', '>600K<', '>300K<'].every(s => out2.includes(s)));
check('会话', '每项都有「跟随系统默认」选项',
      (out2.match(/跟随系统默认/g) || []).length >= 2,
      '出现 ' + (out2.match(/跟随系统默认/g) || []).length + ' 次');
check('会话', '展示当前系统默认值（供对照）',
      out2.includes('128K') || out2.includes('模型默认'), '');

// 已在会话里设了值 → 界面应回显 + 出现「恢复默认」入口
const out3 = peekModel(71, SESS_MODELS,
  { byModel: { "71": { thinking_level: '关闭', context_window: 8192 } },
    systemDefault: {}, providerId: 71, loadedFor: 1 });
check('会话', '回显**当前模型桶**里的已存值（关闭 被选中 + 8192）',
      /value="关闭"[^>]*selected/.test(out3) && /value="8192"[^>]*selected/.test(out3), '');
check('会话', '设置存在时出现「该模型恢复系统默认」入口', out3.includes('clearConvLLM'));
check('会话', '会话有设置时整块高亮（border-color:var(--amb)）',
      out3.includes('border-color:var(--amb)'));

// ★★ 会话 × 模型隔离的**界面层**判据：
//   给两个模型各存一套，**分别展开**各自的行，必须显示各自的，不能串。
const BOTH = { byModel: { "71": { thinking_level: '关闭', context_window: 8192 },
                          "104": { thinking_level: '高', context_window: 131072 } },
               systemDefault: {}, providerId: 71, loadedFor: 1 };
const MODEL_PAIR = [
  { id: 71, name: 'deepseek-v4-flash', model_name: 'deepseek-v4-flash', model_type: 'chat',
    status: 'active', is_default: 1, context_window: 131072, description: '', tags: [],
    model_params: { thinking_control: 'tier', thinking_level: '模型默认' },
    capability: { supported: true, level: 'full', levels: ['关闭','低','中','高'],
                  default_level: '模型默认', reason: '', context: { supported: true, reason: '' } } },
  { id: 104, name: 'qwen3.8-max', model_name: 'qwen3.8-max', model_type: 'chat',
    status: 'active', is_default: 0, context_window: 131072, description: '', tags: [],
    model_params: { thinking_control: 'tier', thinking_level: '模型默认' },
    capability: { supported: true, level: 'full', levels: ['关闭','低','中','高'],
                  default_level: '模型默认', reason: '', context: { supported: true, reason: '' } } },
];
const outP71 = peekModel(71, MODEL_PAIR, BOTH);
const outP104 = peekModel(104, MODEL_PAIR, BOTH);
check('会话×模型', '展开模型 71 的行 → 显示「关闭 + 8192」',
      /value="关闭"[^>]*selected/.test(outP71) && /value="8192"[^>]*selected/.test(outP71));
check('会话×模型', '展开模型 104 的行 → 显示「高 + 131072」，**不继承 71 的值**',
      /value="高"[^>]*selected/.test(outP104) && /value="131072"[^>]*selected/.test(outP104)
      && !/value="关闭"[^>]*selected/.test(outP104));
check('会话×模型', '两行展开后的面板内容不同（不是一套设置套所有模型）',
      outP71 !== outP104);

// ★★ 2026-10-31（米爸纠正）：会话内**不得**有跳转"模型编辑页"的入口。
//   原每行 ⚙ 调 editLLM(id) ⇒ 在会话里点一下改的是**系统默认**（所有会话共用）。
//   这是"看起来调本会话、实际改全局"，必须物理删除；此处用**反向断言**守住，
//   防止以后有人把入口加回来。
check('纠偏', '**不存在**每行 ⚙ 编辑按钮（llm-pick-cfg）', !out3.includes('llm-pick-cfg'), '');
check('纠偏', '**不存在** openLLMConfigFromPicker 调用', !out3.includes('openLLMConfigFromPicker'), '');
check('纠偏', '面板不再出现「⚙ 模型配置」（改为明确的「改系统默认 ↗」）',
      !out3.includes('⚙ 模型配置'));
check('纠偏', '系统默认入口明确标注为「改系统默认」', out3.includes('改系统默认'));
check('纠偏', '明确告知会话调整不会写回系统默认',
      out3.includes('不回写模型配置'));

// ── 能力驱动：不支持时屏蔽入口 + 给原因；未验证时允许但警告 ──
// ⚠️ 必须用 runInContext 赋缓存（这是本仓踩过的第 N 次同一个坑）：
//    `let _chatProvidersCache` 是 context 的**词法绑定**，`sandbox._chatProvidersCache = ...`
//    只写到 sandbox 对象属性上，被测函数读到的仍是旧值 ⇒ 症状是"能力判定完全没生效"，
//    与"功能没写"一模一样。上一轮就是这个错误导致 6 项假红。
// （setCache / peekModel 已在文件前部定义，此处不再重复）
// ① 确定不支持（非对话模型 / 无法识别且明确不适用）⇒ 屏蔽
const outNoCap = peekModel(900, [
  { id: 900, name: '非对话模型', model_name: 'my-model', model_type: 'chat', status: 'active',
    is_default: 0, context_window: 128000, description: '', tags: [], model_params: {},
    capability: { supported: false, level: 'none', levels: [], default_level: '模型默认',
                  reason: '该模型不是对话模型，无思考过程可调',
                  context: { supported: false, reason: '该模型不是对话模型，上下文设置不起作用' } } },
]);
check('能力', '确定不支持 ⇒ 显示「不可设置」而不是给一个假下拉', outNoCap.includes('不可设置'));
check('能力', '确定不支持 ⇒ 界面给出原因', outNoCap.includes('无思考过程可调'));
check('能力', '确定不支持 ⇒ 思考下拉被移除', !/saveConvLLM\('thinking_level'/.test(outNoCap));
check('能力', '确定不支持 ⇒ 上下文入口也被屏蔽', !/saveConvLLM\('context_window'/.test(outNoCap));

// ② 未验证（私有端点/换名）⇒ **允许设 + 警告**，不能当作"不支持"屏蔽
//    实测背景：库里 ds-v4-flash-neibu 是 deepseek，只是识别不出
const outUnv = peekModel(901, [
  { id: 901, name: 'ds-v4-flash-neibu', model_name: 'ds', model_type: 'chat',
    status: 'active', is_default: 0, context_window: 128000, description: '', tags: [],
    model_params: {}, capability: { supported: true, level: 'unverified',
      levels: ['关闭', '低', '中', '高'], default_level: '模型默认',
      reason: '该端点未识别出厂商，思考能力未经验证：设置可能不生效（不会报错）',
      context: { supported: true, reason: '' } } },
]);
check('能力', '未验证 ⇒ **仍然允许设置**（不拿"不知道"当"不支持"）',
      /saveConvLLM\('thinking_level'/.test(outUnv));
check('能力', '未验证 ⇒ 明确警告"可能不生效"', outUnv.includes('未经验证'));
check('能力', '未验证 ⇒ 不显示「不可设置」', !outUnv.includes('不可设置'));

// 模拟"部分支持"（deepseek）：只列 关闭/低/高，且不出现「中」
const outPartial = peekModel(71, [
  { id: 71, name: 'deepseek-v4-flash', model_name: 'deepseek-v4-flash', model_type: 'chat',
    status: 'active', is_default: 1, context_window: 128000, description: '', tags: [],
    model_params: { thinking_level: '模型默认' },
    capability: { supported: true, level: 'partial', levels: ['关闭', '低', '高'],
                  default_level: '模型默认',
                  reason: '该厂商不支持按预算控制思考深度：「关闭」完全生效，「低/高」通过提示词生效，「中」与默认无差别故不提供',
                  context: { supported: true, reason: '' } } },
]);
check('能力', '部分支持 ⇒ 只提供 关闭/低/高 三档', /saveConvLLM\('thinking_level'/.test(outPartial));
check('能力', '部分支持 ⇒ 不提供「中」档（实测与默认无差别）',
      !/<option value="中"/.test(outPartial), '');
check('能力', '部分支持 ⇒ 界面说明限制原因', outPartial.includes('与默认无差别'));

// 全部支持（qwen）：四档齐全
const outFull = peekModel(104, [
  { id: 104, name: 'qwen3.8-max', model_name: 'qwen3.8-max', model_type: 'chat',
    status: 'active', is_default: 0, context_window: 131072, description: '', tags: [],
    model_params: {}, capability: { supported: true, level: 'full',
      levels: ['关闭', '低', '中', '高'], default_level: '模型默认', reason: '',
      context: { supported: true, reason: '' } } },
]);
check('能力', '完全支持 ⇒ 四档齐全（含「中」）', /<option value="中"/.test(outFull));
check('能力', '完全支持 ⇒ 不显示限制说明', !outFull.includes('llm-turn-why'));

// 清空后回到"全部沿用系统默认"的提示（二级树：需展开该模型才能看到）
vm.runInContext('_convLlm = {byModel:{}, systemDefault:{thinking_level:"模型默认", context_window:131072}, providerId:71, loadedFor:1};', sandbox);
// ⚠️ 必须显式传空桶：否则沿用上一次的 _convLlm（里面已有 71 的设置）
//   ⇒ 会渲染成「恢复默认」而断言的是「沿用系统默认」，假红。
const out4 = peekModel(71, MODEL_PAIR, { byModel:{}, systemDefault:{}, providerId:71, loadedFor:1 });
check('会话', '无设置时提示「当前沿用系统默认」',
      out4.includes('沿用系统默认'), '');

// 提前读取源码：后面多组判据都要用（原先定义在 CSS 判据附近，位置在引用之后 ⇒ 会 TDZ 报错）
const jsSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'mods', '13-reports.js'), 'utf8');
const css = fs.readFileSync(path.join(ROOT, 'static', 'css', 'chat.css'), 'utf8');

// ★★★ 2026-11-01（米爸）：设置改为**二级树** —— 挂在每个模型行后面，
//   不再有列表上方的公共块。判据随之整组替换（不是删掉）：
//     · 旧判据「会话设置在模型列表之前」现在必须**反转**成「不得有独立的上方公共块」；
//     · 新增「每个模型行都有自己的设置入口」「展开后才出现该模型的设置」；
//     · 新增「二级树保存必须带**该行**的 pid」（数据链路完整性的核心）。
//   为什么反转而不是删除：删掉 = 这个维度不再受保护，下次又会被加回公共块。
check('二级树', '**不再有**列表上方的公共设置块（llm-pick-conv）',
      !out4.includes('llm-pick-conv'));
check('二级树', '设置不再由 buildConvSettingsHtml 渲染（已改为按模型）',
      !out4.includes('buildConvSettingsHtml') && !/buildConvSettingsHtml/.test(jsSrc));
check('二级树', '每个模型行都有「⚙ 设置」入口（lp-set）',
      (out4.match(/class="lp-set/g) || []).length === (out4.match(/llm-pick-item/g) || []).length
      && (out4.match(/llm-pick-item/g) || []).length > 0,
      'set=' + (out4.match(/class="lp-set/g) || []).length + ' item=' + (out4.match(/llm-pick-item/g) || []).length);
check('二级树', '每行的设置入口带**该行自己的** pid',
      (out4.match(/toggleModelSettings\(\d+\)/g) || []).length >= 2,
      (out4.match(/toggleModelSettings\(\d+\)/g) || []).slice(0, 4).join(','));
// 判据必须用**收起态**渲染结果 —— out4 是展开态，拿它验"默认收起"会自相矛盾。
const outCollapsed = peekCollapsed(71, MODEL_PAIR, { byModel:{}, systemDefault:{}, providerId:71, loadedFor:1 });
check('二级树', '默认全部收起（二级面板不预渲染）', !outCollapsed.includes('lp-sub'));
check('二级树', '收起态仍显示每行的「⚙ 设置」入口',
      (outCollapsed.match(/class="lp-set/g) || []).length === 2,
      'n=' + (outCollapsed.match(/class="lp-set/g) || []).length);

// ── 展开后：二级面板出现在**该行内部**，且只展开一个 ──
const TWO_MODELS = [
  { id: 71, name: 'deepseek-v4-flash', model_name: 'deepseek-v4-flash', model_type: 'chat',
    status: 'active', is_default: 1, context_window: 128000, description: '', tags: [],
    model_params: { thinking_control: 'tier', thinking_level: '模型默认' },
    capability: { supported: true, level: 'partial', levels: ['关闭','低','高'],
                  default_level: '模型默认', reason: '该厂商不支持按预算控制',
                  context: { supported: true, reason: '' } } },
  { id: 104, name: 'qwen3.8-max', model_name: 'qwen3.8-max', model_type: 'chat',
    status: 'active', is_default: 0, context_window: 131072, description: '', tags: [],
    model_params: { thinking_control: 'tier', thinking_level: '模型默认' },
    capability: { supported: true, level: 'full', levels: ['关闭','低','中','高'],
                  default_level: '模型默认', reason: '',
                  context: { supported: true, reason: '' } } },
];
const outExp = peekModel(71, TWO_MODELS,
  { byModel: { "71": { thinking_level: '关闭', context_window: 8192 } },
    systemDefault: {}, providerId: 71, loadedFor: 1 });
check('二级树', '展开后出现该模型的二级面板（lp-sub）', outExp.includes('lp-sub'));
check('二级树', '二级面板标题写明是**哪个模型**的参数',
      outExp.includes('deepseek-v4-flash 的参数'));
check('二级树', '只展开一个：另一个模型没有二级面板',
      (outExp.match(/class="lp-sub"/g) || []).length === 1,
      'n=' + (outExp.match(/class="lp-sub"/g) || []).length);
check('二级树', '二级面板里是该模型已存的值（关闭 + 8192）',
      /value="关闭"[^>]*selected/.test(outExp) && /value="8192"[^>]*selected/.test(outExp));

// ★★ 数据链路完整性（本轮最关键的一条，纯静态很难发现）：
//   二级树的 onchange **必须显式传该行 pid**。若沿用"当前选中模型"，
//   出现「展开 A 但不选中 A」时会把 A 的设置存进 B 的桶 —— 界面对、数据错。
check('二级树', '思考强度的保存回调带**该行 pid**',
      /saveConvLLM\('thinking_level', this\.value, \d+\)/.test(outExp),
      (outExp.match(/saveConvLLM\('thinking_level'[^)]*\)/) || [''])[0]);
check('二级树', '上下文的保存回调带**该行 pid**',
      /saveConvLLM\('context_window', this\.value, \d+\)/.test(outExp),
      (outExp.match(/saveConvLLM\('context_window'[^)]*\)/) || [''])[0]);
check('二级树', '恢复默认入口带**该行 pid**',
      /clearConvLLM\(\d+\)/.test(outExp),
      (outExp.match(/clearConvLLM\([^)]*\)/) || [''])[0]);
// 反面：不得出现**不带 pid** 的调用（那就是回落"当前选中"的老写法）
check('二级树', '**不得**出现不带 pid 的 saveConvLLM 调用',
      !/saveConvLLM\('(thinking_level|context_window)', this\.value\)/.test(outExp));
check('二级树', '**不得**出现不带 pid 的 clearConvLLM 调用',
      !/clearConvLLM\(\)/.test(outExp));

// ── 一级行显示「本会话已设」标记：收起状态下也能看出哪个模型被单独调过 ──
check('二级树', '有自定义设置的模型行显示「本会话已设」标记',
      outExp.includes('本会话已设'));
const outNoSet = peekModel(104, TWO_MODELS,
  { byModel: {}, systemDefault: {}, providerId: 104, loadedFor: 1 });
check('二级树', '未设置的模型行**不显示**该标记', !outNoSet.includes('本会话已设'));
vm.runInContext('_expandedPid = null;', sandbox);

// CSS 层判据：panel 必须是纵向 flex，list 自己滚动；二级面板有从属视觉（左竖线缩进）。
const cssPanel = (css.match(/\.llm-pick-panel\{[^}]*\}/) || [''])[0];
check('可见性', 'CSS：panel 为纵向 flex 且限高',
      /flex-direction:column/.test(cssPanel) && /max-height/.test(cssPanel), cssPanel.slice(0, 60));
check('可见性', 'CSS：panel 自身不再 overflow-y:auto（改由列表滚动）',
      !/\.llm-pick-panel\{[^}]*overflow-y:auto/.test(css));
check('可见性', 'CSS：.llm-pick-list 内部滚动',
      /\.llm-pick-list\{[^}]*overflow-y:auto/.test(css));
check('二级树', 'CSS：二级面板有左侧竖线+缩进（体现从属关系）',
      /\.lp-sub\{[^}]*border-left/.test(css) && /\.lp-sub\{[^}]*padding-left/.test(css));
check('二级树', 'CSS：一级行的「⚙ 设置」按钮有展开态样式',
      /\.lp-set\.open\{/.test(css));

// ★★ 2026-11-01 真机实测抓到的 bug（纯静态断言完全测不出，必须量像素）：
//   JS 里写 `panel.style.display='block'`，**内联样式压过样式表** ⇒
//   上面那条 "panel 为纵向 flex" 的 CSS 断言全绿，但真机 computed display 是 block，
//   于是 `flex:1;overflow-y:auto` 的列表既不收缩也不滚动 ——
//   实测 list 高 451px、溢出面板底边 111px（panel bottom=605 / list bottom=716）。
//   ⇒ 必须有一条**盯住 JS 内联值**的哨兵。这是本仓"坑 1：内联样式压过样式表"的同族。
check('可见性', 'JS 打开面板时必须用 display=flex（不能写 block，会压掉 flex 布局）',
      /panel\.style\.display\s*=\s*'flex'/.test(jsSrc)
      && !/panel\.style\.display\s*=\s*'block'/.test(jsSrc),
      /panel\.style\.display\s*=\s*'block'/.test(jsSrc) ? "仍存在 ='block'" : "ok");

// ★★ 2026-11-01：思考控制方式（按挡位 / 按开关）—— 由**模型配置页**决定粒度。
//   判据：同一模型能力下，control=toggle 只给 开启/关闭；control=tier 给挡位。
//   为什么要两套：厂商暴露程度不同，有的只有 enable/disable。粒度由用户按模型选，
//   会话入口必须跟着变 —— 否则会出现"给 5 档但底层只认开关"的错位。
function setCacheWithCtl(ctl, levels) {
  // 二级树：必须展开该模型才看得到设置控件
  return peekModel(55, [
    { id: 55, name: 'ctl-model', model_name: 'ctl-model', model_type: 'chat', status: 'active',
      is_default: 0, context_window: 128000, description: '', tags: [],
      model_params: { thinking_control: ctl, thinking_level: '模型默认' },
      capability: { supported: true, level: 'full', levels: levels || ['关闭','低','中','高'],
                    default_level: '模型默认', reason: '',
                    context: { supported: true, reason: '' } } },
  ]);
}

const outToggle = setCacheWithCtl('toggle');
check('控制方式', '模型配置=按开关 ⇒ 会话只给 开启/关闭',
      /<option value="开启"/.test(outToggle) && /<option value="关闭"/.test(outToggle));
check('控制方式', '模型配置=按开关 ⇒ **不出现** 低/中/高',
      !/<option value="低"/.test(outToggle) && !/<option value="中"/.test(outToggle)
      && !/<option value="高"/.test(outToggle));
check('控制方式', '模型配置=按开关 ⇒ 界面标注「按开关」', outToggle.includes('按开关'));

const outTier = setCacheWithCtl('tier');
check('控制方式', '模型配置=按挡位 ⇒ 出现 低/中/高',
      /<option value="低"/.test(outTier) && /<option value="中"/.test(outTier)
      && /<option value="高"/.test(outTier));
check('控制方式', '模型配置=按挡位 ⇒ **不出现**「开启」（开启只属开关模式）',
      !/<option value="开启"/.test(outTier));
check('控制方式', '模型配置=按挡位 ⇒ 界面标注「按挡位」', outTier.includes('按挡位'));

// 粒度 ∩ 能力：tier 模式下 deepseek 仍应只给 关闭/低/高（能力裁剪不被粒度取消）
const outTierPartial = peekModel(56, [
  { id: 56, name: 'deepseek-x', model_name: 'deepseek-x', model_type: 'chat', status: 'active',
    is_default: 0, context_window: 128000, description: '', tags: [],
    model_params: { thinking_control: 'tier', thinking_level: '模型默认' },
    capability: { supported: true, level: 'partial', levels: ['关闭','低','高'],
                  default_level: '模型默认', reason: '该厂商不支持按预算控制',
                  context: { supported: true, reason: '' } } },
], { byModel:{}, systemDefault:{}, providerId:56, loadedFor:1 });
check('控制方式', '粒度 × 能力 相乘：tier+partial ⇒ 仍只 关闭/低/高',
      /<option value="关闭"/.test(outTierPartial) && /<option value="低"/.test(outTierPartial)
      && !/<option value="中"/.test(outTierPartial));

// ── 输出 ──
let fail = 0;
let lastGroup = '';
for (const r of RESULTS) {
  if (r.group !== lastGroup) { console.log('\n[' + r.group + ']'); lastGroup = r.group; }
  console.log((r.ok ? '  [ok] ' : '  [FAIL] ') + r.name + (r.detail ? '  (' + r.detail + ')' : ''));
  if (!r.ok) fail++;
}
console.log('\n' + (RESULTS.length - fail) + '/' + RESULTS.length + ' 通过' + (fail ? '（' + fail + ' 项失败）' : ''));
process.exit(fail ? 1 : 0);
