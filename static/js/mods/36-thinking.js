// ── 设置·任务级思考（st-think tab，2026-10-10）─────────────────────────────
// ★ **为什么单开一个 tab，而不是并进「AI 上下文」面板**
//   该面板是**硬编码字段表** `CTX_CFG_FIELDS`（cluster: budget/conf/dual/funnel/
//   fuse/mem/ocr/route/split/vision），实测**不含任何 `llm.*` 字段**
//   ⇒ thinking 的配置挂进去也不会显示。
//   ⇒ 与其改那个表（牵连 10 个 cluster 的渲染逻辑），不如单开，定位也更准。
//
// ★ **与「模型管理」里的思考开关不是一回事**（最容易混的地方）：
//   模型管理 `f-thinking` → `llm_providers.model_params.thinking_mode` = **模型级**
//     ⇒ 对该 provider 的**所有**调用统一开/关，管不了"这次要不要想"。
//   本页→ `llm.thinking_*` + `thinking_policy` = **任务级**
//     ⇒ 按 intent / 任务形态 / 实测数据，决定"**这一次**调用要不要思考"。
//
// 优先级（后端 llm/providers/openai_compat.py 实际实现）：
//   Agent 级 model_params > 本页名单 on/off > 形态规则 > 不发字段（= 模型默认）
const THINK_FIELDS = [
  {key:'llm.thinking_policy', type:'str', label:'按意图分流（off/on 名单）',
   hint:'JSON：{"off":[...关思考的意图...],"on":[...开思考的...]}。★ 未命中的意图**不发** thinking 字段（=保持模型默认），切勿配"默认关闭"——那会把重产码任务的思考一起关掉。示例：{"off":["routing","doc_summary","chat_probe"],"on":[]}',
   ph:'{"off": ["routing", "doc_summary", "chat_probe"], "on": []}'},
  {key:'llm.thinking_shape_enabled', type:'bool', label:'★ 启用形态规则（新增意图零配置被覆盖）',
   hint:'按**任务形态**自动判定，而不是靠人维护名单。★ 为什么需要：意图是动态的（编排层直接用 Agent 名当意图，库里有 27 个 active Agent 含中文名），实测 off 名单对 Agent 名覆盖率 0/27 ⇒ 纯名单制必然随系统演进而漂移。规则：带工具 + 输出上限小 + 名字带探针语义 ⇒ 关思考。已实测关思考后工具调用不丢（tool_calls 保留）。',
   ph:''},
  {key:'llm.shape_probe_max_tokens', type:'int', label:'形态规则·探针轮输出上限',
   hint:'带工具且 max_tokens ≤ 此值才判为「探针轮」⇒ 关思考。默认 1200。⚠️ 三重条件缺一不可 —— 单看此项会误伤视图生成（实测其 max_tokens 仅 76，但它是重产码任务）。不要调太大：工具调用本身需要推理。', ph:''},
  {key:'llm.shape_cheap_max_tokens', type:'int', label:'形态规则·廉价轮输出上限',
   hint:'无工具且 max_tokens ≤ 此值 ⇒ 关思考。0=不启用（默认关：无工具的调用里混着大量重产码任务，误判风险高）。', ph:''},
  {key:'llm.thinking_budget', type:'int', label:'思考 token 上限',
   hint:'给推理思考的**独立**上限。思考与正文共享 max_tokens，不设限时思考可吃掉 63% 输出（实测），并把耗时线性拉长。0=不启用。', ph:''},
  {key:'llm.thinking_reserve_tokens', type:'int', label:'正文保底额度',
   hint:'思考不得吃光这部分正文额度。实际压算 max_tokens = min(max_tokens, 思考上限 + 正文保底)。0=不启用。', ph:''},
  {key:'llm.thinking_warn_tokens', type:'int', label:'思考超限告警阈值',
   hint:'reasoning ≥ 此值时打 WARNING 日志（流式/非流式唯一共同点）。**只留痕不拦截**。0=不启用。实测 310/6890 次调用超 2000tok，极端例 13430/13535=99.2% 耗 4017秒。', ph:''},
  {key:'llm.autotune_enabled', type:'bool', label:'★ 启用数据自校正（按实测数据建议）',
   hint:'按真库 `llm_usage_stats` 实测聚合，产出「建议关思考」的意图清单（思考占比 ≥ 阈值 且 平均产出 ≤ 上限）。⚠️ **只产出建议，不自动改配置**（避免统计口径错误直接变成线上行为）。需配合下方阈值使用。', ph:''},
  {key:'llm.autotune_min_ratio', type:'float', label:'自校正·思考占比阈值',
   hint:'默认 0.5。依据实测分界：廉价任务思考占 58%~98%，重产码 0%~2.5%，中间几乎无重叠。', ph:''},
  {key:'llm.autotune_max_output', type:'int', label:'自校正·平均产出上限',
   hint:'默认 1200token。实测廉价任务 ≤1224，重产码 ≥4336。', ph:''},
  {key:'llm.autotune_min_samples', type:'int', label:'自校正·样本量下限（安全阀）',
   hint:'默认 5。★ 低于此数**不判** —— 2 次调用的均值没有代表性，而误判代价（关掉重产码的思考）远大于收益。', ph:''},
];

function _thinkRow(f, v){
  const id = 'tk_' + f.key.replace(/\./g,'_');
  const val = (v===undefined||v===null) ? '' : v;
  let ctrl;
  if(f.type === 'bool'){
    ctrl = `<select id="${id}" data-k="${f.key}" data-t="bool">
      <option value="1"${String(val)==='1'?' selected':''}>启用</option>
      <option value="0"${String(val)!=='1'?' selected':''}>关闭</option></select>`;
  } else {
    ctrl = `<input id="${id}" data-k="${f.key}" data-t="${f.type}" value="${String(val).replace(/"/g,'&quot;')}"
             placeholder="${f.ph||''}" style="${f.type==='str'?'width:100%;':'width:110px;'}">`;
  }
  return `<div class="form-row" style="align-items:flex-start">
    <label style="min-width:230px;padding-top:4px;">${f.label}
      <div class="info-tip" title="${f.hint.replace(/"/g,'&quot;')}">ⓘ</div></label>
    <div style="flex:1;">${ctrl}
      <div style="font-size:10.5px;color:var(--mut);margin-top:3px;line-height:1.5;">${f.hint}</div></div>
  </div>`;
}

async function loadThinkCfg(){
  const box = document.getElementById('think-cfg-body');
  if(!box) return;
  box.innerHTML = '<div class="loading">加载中…</div>';
  let st = {};
  try{
    const d = await api('/api/system/config');
    st = (d && d.static) || {};
  }catch(e){
    box.innerHTML = `<div style="color:var(--danger);">加载失败：${(e&&e.message)||e}</div>`;
    return;
  }
  const val = k => { const [s,key] = k.split('.'); return (st[s]||{})[key]; };
  // 附上「自校正建议清单」——让"要不要采纳"有据可依
  let sugHtml = '<div style="font-size:11px;color:var(--mut);margin:10px 0;">正在计算自校正建议…</div>';
  box.innerHTML = `<div class="panel" style="padding:12px;">
      ${THINK_FIELDS.map(f => _thinkRow(f, val(f.key))).join('')}
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line);">
        <div style="font-size:12px;font-weight:500;margin-bottom:6px;">📊 自校正建议清单（按真库实测数据）</div>
        <div id="think-suggest">${sugHtml}</div>
      </div>
    </div>`;
  loadThinkSuggestions();
}

async function loadThinkSuggestions(){
  const box = document.getElementById('think-suggest');
  if(!box) return;
  try{
    const r = await api('/api/system/config/think-suggest');
    const list = (r && r.suggestions) || [];
    if(!list.length){ box.innerHTML = '<div style="font-size:11px;color:var(--mut);">当前无满足「建议关思考」条件的意图（样本量或占比未达阈值）。</div>'; return; }
    box.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:11.5px;">
      <tr style="color:var(--mut);"><td style="padding:3px 4px;">意图</td><td>样本</td><td>思考占比</td><td>平均产出</td><td></td></tr>
      ${list.map(s => `<tr style="border-top:1px solid var(--line);">
        <td style="padding:3px 4px;">${s.intent}</td>
        <td>${s.n}</td>
        <td style="color:var(--warn);font-weight:500;">${(s.ratio*100).toFixed(1)}%</td>
        <td>${Math.round(s.avg_completion)} tok</td>
        <td><button class="btn sm" onclick='addThinkOff("${s.intent}")'>加入 off 名单</button></td>
      </tr>`).join('')}
    </table>
    <div style="font-size:10.5px;color:var(--mut);margin-top:5px;">
      判据：思考占比 ≥ <code>autotune_min_ratio</code> <b>且</b>平均产出 ≤ <code>autotune_max_output</code>，且样本量 ≥ <code>autotune_min_samples</code>。
      ⚠️ 点击只是<b>追加到下方输入框</b>，仍需点「保存配置」才生效。
    </div>`;
  }catch(e){
    box.innerHTML = `<div style="font-size:11px;color:var(--danger);">建议清单不可用：${(e&&e.message)||e}</div>`;
  }
}

function addThinkOff(intent){
  const el = document.getElementById('tk_llm_thinking_policy');
  if(!el) return;
  let obj = {off:[], on:[]};
  try{ obj = JSON.parse(el.value || '{}'); }catch(e){ toast('上方 JSON 格式有误，请先修正'); return; }
  obj.off = obj.off || []; obj.on = obj.on || [];
  if(obj.off.indexOf(intent) < 0) obj.off.push(intent);
  el.value = JSON.stringify(obj);
  toast('已追加 ' + intent + '，记得点「保存配置」');
}

async function saveThinkCfg(){
  const updates = {};
  THINK_FIELDS.forEach(f => {
    const id = 'tk_' + f.key.replace(/\./g,'_');
    const el = document.getElementById(id);
    if(!el) return;
    let v = el.value;
    if(f.type === 'bool') v = (v === '1') ? 1 : 0;
    updates[f.key] = v;
  });
  if(!Object.keys(updates).length){ toast('没有修改的配置项'); return; }
  // JSON 字段先本地校验，避免整批保存失败
  if(updates['llm.thinking_policy']){
    try{ JSON.parse(updates['llm.thinking_policy'] || '{}'); }
    catch(e){ toast('「按意图分流」不是合法 JSON，请检查引号与逗号'); return; }
  }
  try{
    const r = await api('/api/system/config/static', {method:'PUT', body:JSON.stringify({updates})});
    if(r && r.ok === false) throw new Error(r.error || '保存失败');
    toast('✅ 任务级思考配置已保存并即时生效（' + Object.keys(updates).length + ' 项）');
    loadThinkCfg();
  }catch(e){ toast('保存失败：' + ((e&&e.message)||e)); }
}
