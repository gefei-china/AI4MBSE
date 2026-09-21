// P1-4b 前端自检：配置页「耦合簇」渲染逻辑 + 约束校验器 + 与后端 schema 的契约一致性
// 运行：node tools/verify/verify_ctxconfig_ui.js
// 说明：agent-browser 在本机反复被环境 SIGTERM，故改用**纯函数单测**（比截图更有逻辑覆盖）——
//       35-ctxconfig.js 里 ctxViz / ctxChecksHtml / CTX_CHECKS 都是只依赖入参的纯函数。
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const JS_PATH = path.join(REPO, 'static', 'js', 'mods', '35-ctxconfig.js');
const SCHEMA_PATH = path.join(REPO, 'tmp', 'p21', 'schema_snapshot.json');

let ok = 0, fail = 0;
const fails = [];
function check(tag, cond, detail = '') {
  if (cond) { ok++; console.log('[PASS] ' + tag); }
  else { fail++; fails.push(tag); console.log('[FAIL] ' + tag + ' —— ' + detail); }
}

// ── 载入被测模块（提供最小 document stub 让它能被解析；纯函数不触碰 DOM）──
const src = fs.readFileSync(JS_PATH, 'utf8');
const factory = new Function(
  'document',
  src + '\nreturn { CTX_CFG_CLUSTERS, CTX_CFG_FIELDS, CTX_CHECKS, ctxViz, ctxChecksHtml, ctxFieldVal };'
);
const M = factory({ getElementById: () => null, querySelector: () => null });
const { CTX_CFG_CLUSTERS, CTX_CFG_FIELDS, CTX_CHECKS, ctxViz, ctxChecksHtml } = M;

const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8')).schema || {};

console.log('='.repeat(78));
console.log('P1-4b 前端自检：耦合簇渲染 + 约束校验 + schema 契约');
console.log('='.repeat(78));

// ══ A 结构完整性 ═══════════════════════════════════════════════════════════
console.log('\n── A 结构完整性 ──');
check('A1 共 7 个参数簇', CTX_CFG_CLUSTERS.length === 7, 'got ' + CTX_CFG_CLUSTERS.length);
const clusterIds = CTX_CFG_CLUSTERS.map(c => c.id);
check('A2 簇 id 无重复', new Set(clusterIds).size === clusterIds.length);

const orphans = CTX_CFG_FIELDS.filter(f => !clusterIds.includes(f.cluster));
check('A3 无归属缺失的字段', orphans.length === 0, '孤儿: ' + orphans.map(f => f.key).join(','));
const empties = clusterIds.filter(id => !CTX_CFG_FIELDS.some(f => f.cluster === id));
check('A4 无空簇', empties.length === 0, '空簇: ' + empties.join(','));

// schema 契约：前端每个 key 必须真实存在，且类型与声明一致
console.log('\n── schema 契约（前端字段 ↔ 后端 schema）──');
const missing = [], mismatch = [];
for (const f of CTX_CFG_FIELDS) {
  const [s, k] = f.key.split('.');
  const meta = (schema[s] || {})[k];
  if (!meta) { missing.push(f.key); continue; }
  if (meta.type && meta.type !== f.type) mismatch.push(`${f.key}: 前端 ${f.type} vs schema ${meta.type}`);
}
check('A5 全部字段在 schema 中存在', missing.length === 0, '缺失: ' + missing.join(','));
check('A6 字段类型与 schema 一致', mismatch.length === 0, mismatch.join('; '));
check(`A7 字段总数 = ${CTX_CFG_FIELDS.length}（原 26 项 + 新增/补暴露）`,
      CTX_CFG_FIELDS.length >= 31, 'got ' + CTX_CFG_FIELDS.length);
const dupKeys = CTX_CFG_FIELDS.map(f => f.key).filter((k, i, a) => a.indexOf(k) !== i);
check('A8 字段 key 无重复', dupKeys.length === 0, '重复: ' + dupKeys.join(','));

// ══ B 约束校验器（正例 / 反例）══════════════════════════════════════════════
console.log('\n── B 约束校验器 ──');
const OKV = {
  'rag.recall_k': 20, 'rag.top_k': 20, 'rag.rerank_max_candidates': 10, 'rag.inject_k': 3,
  'rag.rerank_enabled': true, 'rag.route_threshold': 0.75,
  'rag.w_coverage': 0.5, 'rag.w_relations': 0.3, 'rag.w_typing': 0.2,
  'rag.confidence_high': 0.7, 'rag.confidence_mid': 0.45,
  'rag.hyde_enabled': true, 'rag.hyde_weight': 0.05,
  'context.budget_window_tokens': 65536, 'context.budget_retrieval_tokens': 5000,
  'context.budget_history_tokens': 3000, 'context.budget_retrieval_ratio': 0, 'context.budget_history_ratio': 0,
  'context.topic_group_match_dense': 0.5, 'context.topic_retrieve_threshold_dense': 0.5,
  'context.semantic_fallback_gate_dense': 0.79, 'context.topic_sim_threshold': 0.15,
  'context.topic_retrieve_threshold': 0.15,
  'embedding.enabled': true,
};
const lv = (rows) => rows.map(r => r.level);
const hasErr = (rows) => rows.some(r => r.level === 'err');

// B1 合规组合 → 全 ok，无 err
check('B1 默认组合 → 漏斗无 err', !hasErr(CTX_CHECKS.ladder(OKV)), JSON.stringify(CTX_CHECKS.ladder(OKV)));
check('B2 默认组合 → 权重 ok', lv(CTX_CHECKS.weights(OKV)).includes('ok'));
check('B3 默认组合 → 序关系 ok', lv(CTX_CHECKS.order(OKV)).includes('ok'));
check('B4 默认组合 → 双路 ok（0.5 不再触发偏松告警）',
      lv(CTX_CHECKS.dual(OKV)).includes('ok'), JSON.stringify(CTX_CHECKS.dual(OKV)));

// B5 反例：融合池 > 每路召回
let bad = { ...OKV, 'rag.top_k': 50 };
check('B5 top_k > recall_k → err', hasErr(CTX_CHECKS.ladder(bad)));

// B6 反例：注入 > 池子
bad = { ...OKV, 'rag.inject_k': 30 };
check('B6 inject_k > top_k → err', hasErr(CTX_CHECKS.ladder(bad)));

// B7 反例：倒挂（重排窗口 > 池子）—— 这正是本轮修掉的缺陷形态
bad = { ...OKV, 'rag.top_k': 4, 'rag.rerank_max_candidates': 10 };
check('B7 重排窗口 > 融合池（倒挂）→ err', hasErr(CTX_CHECKS.ladder(bad)));

// B8 反例：重排窗口 < 注入数 → warn（非阻断）
bad = { ...OKV, 'rag.rerank_max_candidates': 2, 'rag.inject_k': 3 };
check('B8 重排窗口 < 注入数 → warn', lv(CTX_CHECKS.ladder(bad)).includes('warn') && !hasErr(CTX_CHECKS.ladder(bad)));

// B9 反例：权重合计偏离 1.0
bad = { ...OKV, 'rag.w_typing': 0.5 };
check('B9 三因子合计 1.3 → err', hasErr(CTX_CHECKS.weights(bad)));

// B10 反例：序关系倒置
bad = { ...OKV, 'rag.confidence_high': 0.3 };
check('B10 高分界 ≤ 中分界 → err', hasErr(CTX_CHECKS.order(bad)));

// B11 反例：HyDE 开关关但权重 >0 → warn
bad = { ...OKV, 'rag.hyde_enabled': false };
check('B11 开关关 + 权重>0 → warn', lv(CTX_CHECKS.hyde(bad)).includes('warn'));

// B12 反例：预算合计超窗口
bad = { ...OKV, 'context.budget_window_tokens': 6000, 'context.budget_retrieval_tokens': 5000,
        'context.budget_history_tokens': 3000 };
check('B12 检索+历史 > 窗口 → err', hasErr(CTX_CHECKS.budget(bad)));

// B13 占比制生效时被忽略的绝对值不应触发误判（5000+3000 > 6000，但占比>0 时按占比算）
bad = { ...OKV, 'context.budget_window_tokens': 6000, 'context.budget_retrieval_tokens': 99999,
        'context.budget_history_tokens': 99999, 'context.budget_retrieval_ratio': 0.05,
        'context.budget_history_ratio': 0.03 };
check('B13 占比制生效 → 忽略绝对值、不误报超窗',
      !hasErr(CTX_CHECKS.budget(bad)), JSON.stringify(CTX_CHECKS.budget(bad)));

// B14 语义出口关闭 → warn 提示 dense 列失效
bad = { ...OKV, 'embedding.enabled': false };
check('B14 总开关关闭 → warn', lv(CTX_CHECKS.dual(bad)).includes('warn'));

// B15 dense 阈值偏低 → warn（回归：标定前 0.30/0.35 会被提示）
bad = { ...OKV, 'context.topic_group_match_dense': 0.3, 'context.topic_retrieve_threshold_dense': 0.35 };
check('B15 dense 阈值 <0.5 → warn（旧值会被告警）', lv(CTX_CHECKS.dual(bad)).includes('warn'));

// ══ C 渲染输出 ═════════════════════════════════════════════════════════════
console.log('\n── C 渲染输出 ──');
for (const c of CTX_CFG_CLUSTERS) {
  const html = ctxViz(c, OKV) + ctxChecksHtml(c, OKV);
  check(`C-${c.id} 渲染非空且为 HTML 片段`, typeof html === 'string' && html.length > 40,
        `len=${html ? html.length : 0}`);
  check(`C-${c.id} 无未替换占位（undefined/NaN/null）`,
        !/undefined|NaN|\[object Object\]/.test(html),
        (html.match(/undefined|NaN|\[object Object\]/) || []).join(','));
}
const funnelHtml = ctxViz(CTX_CFG_CLUSTERS.find(c => c.viz === 'funnel'), OKV);
check('C1 漏斗视图含四层容量数字', funnelHtml.includes('20') && funnelHtml.includes('10') && funnelHtml.includes('3'));
const dualHtml = ctxViz(CTX_CFG_CLUSTERS.find(c => c.viz === 'dual'), OKV);
check('C2 双路表格含各阈值（bigram 0.15 / dense 0.5 / 标定锚 0.79）',
      dualHtml.includes('0.15') && dualHtml.includes('0.5') && dualHtml.includes('0.79'),
      dualHtml.slice(0, 200));
const budgetHtml = ctxViz(CTX_CFG_CLUSTERS.find(c => c.viz === 'budget'), OKV);
check('C3 预算视图给出生效口径与占比', budgetHtml.includes('检索区 5000') || budgetHtml.includes('检索区'));
const weightsHtml = ctxViz(CTX_CFG_CLUSTERS.find(c => c.viz === 'weights'), OKV);
check('C4 路由视图显示权重合计', /合计/.test(weightsHtml) && weightsHtml.includes('1.00'));

// C5 渲染随值改变（不是静态字符串）
const changed = ctxViz(CTX_CFG_CLUSTERS.find(c => c.viz === 'funnel'), { ...OKV, 'rag.top_k': 40 });
check('C5 输入变化 → 渲染输出随之变化', changed !== funnelHtml);

// ══ D saveCtxConfig 的校验拦截（静态检查：确实调用了 ctxValidate）══
console.log('\n── D 保存前拦截 ──');
check('D1 saveCtxConfig 调用 ctxValidate 且失败即 return',
      /async function saveCtxConfig\(\)\s*\{\s*if \(!ctxValidate\(\)\) return;/.test(src));
check('D2 存在 ctxValidate 定义并遍历各簇 checks',
      /function ctxValidate\(\)/.test(src) && /CTX_CHECKS\[k\]\(v\)\.filter\(x => x\.level === 'err'\)/.test(src));

console.log('\n' + '='.repeat(78));
console.log(`结果：${ok} pass / ${fail} fail`);
if (fail) console.log('失败项：\n  - ' + fails.join('\n  - '));
console.log('='.repeat(78));
process.exit(fail ? 1 : 0);
