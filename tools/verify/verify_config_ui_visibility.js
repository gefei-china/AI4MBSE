/* 门禁：配置开关的**可见性**与**前后端对齐**。

为什么需要这个门禁（真实缺口）：
  2026-10-28 实施「持续自主执行」时发现一个**离线门禁全部是绿的、但用户根本用不上**的问题：
    · 后端 CONFIG_SCHEMA 里 7 个新键齐全（`core/config.py`）
    · REST 接口也能读写（`/api/system/config/static`）
    · 但前端配置页用的是**硬编码簇列表** `CTX_CFG_CLUSTERS` + `CTX_CFG_FIELDS`
      ⇒ 没有列进 CTX_CFG_FIELDS 的键，**界面上根本不显示**
    ⇒ 「配置项已声明」不等于「用户能打开」。这正是本仓已记录的
      「静默丢弃」同族：不是报错，是"你在界面上找不到它"。

本门禁验四件事：
  A. 前端字段的 key 必须**存在于后端 CONFIG_SCHEMA**（防前端写了后端没有的键）
  B. 后端**本次新增的功能开关**必须在前端可见（防"实现了但打不开"）
  C. 每个簇必须**至少有一个字段**（防空簇：标题渲染出来但没内容）
  D. 真实渲染：加载真实 JS + mock DOM，断言新簇真的进入了 innerHTML

用法：node tools/verify/verify_config_ui_visibility.js
退出码 0 = 全绿；1 = 有红项。
*/
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const UI_FILE = path.join(ROOT, 'static', 'js', 'mods', '35-ctxconfig.js');

const RESULTS = [];
function check(group, name, cond, detail) {
  RESULTS.push({ group, name, ok: !!cond, detail: detail || '' });
}

// ── 从真实 JS 文件里取出两个数组（不靠正则猜结构，直接跑 JS 拿对象）──
function loadUiArrays() {
  const src = fs.readFileSync(UI_FILE, 'utf8');
  // 只截取到 CTX_CFG_FIELDS 结束，避免执行后面的 DOM 逻辑
  const iFields = src.indexOf('const CTX_CFG_FIELDS');
  const iEnd = src.indexOf('];', src.indexOf('[', iFields)) + 2;
  const head = src.slice(0, iEnd);
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(head + '\nthis.__C = CTX_CFG_CLUSTERS; this.__F = CTX_CFG_FIELDS;',
                  sandbox, { timeout: 10000 });
  return { clusters: sandbox.__C, fields: sandbox.__F, src };
}

// ── 从后端取 CONFIG_SCHEMA（用 Python 真实 import，不读源码猜）──
function loadBackendSchema() {
  const py = `
import sys, json
sys.path.insert(0, r"${ROOT.replace(/\\/g, '\\\\')}")
from core.config import CONFIG_SCHEMA
out = {}
for sec, items in CONFIG_SCHEMA.items():
    out[sec] = sorted(items.keys())
print(json.dumps(out, ensure_ascii=False))
`;
  const raw = execFileSync('python', ['-c', py], { encoding: 'utf8' });
  return JSON.parse(raw.trim().split('\n').pop());
}

function main() {
  const { clusters, fields, src } = loadUiArrays();
  const schema = loadBackendSchema();

  check('A', '前端文件加载成功', Array.isArray(clusters) && Array.isArray(fields),
        `簇 ${clusters && clusters.length} 个 / 字段 ${fields && fields.length} 条`);
  check('A', '后端 schema 加载成功', Object.keys(schema).length > 0,
        `${Object.keys(schema).length} 个 section`);

  // ══ A. 前端字段 key 必须存在于后端 schema ══
  const missingInBackend = [];
  for (const f of fields) {
    const parts = String(f.key || '').split('.');
    const sec = parts[0], k = parts.slice(1).join('.');
    if (!parts[1]) { missingInBackend.push(`${f.key}（无 section）`); continue; }
    if (!schema[sec] || schema[sec].indexOf(k) < 0) {
      missingInBackend.push(f.key);
    }
  }
  check('A', '★前端每个字段的 key 都在后端 schema 里', missingInBackend.length === 0,
        `后端不存在的键：${JSON.stringify(missingInBackend)}`);

  // ══ B. 本次新增的功能开关必须前端可见 ══
  // 界面可见的 6 个（todo_require_verify 已从界面移除 —— 它是死开关：
  //   没有任何产品代码读取它，拨了不会生效。留着比不显示更糟。）
  const NEW_KEYS = [
    'agent.todo_enabled',
    'agent.harness_enabled',
    'agent.harness_max_blocks',
    'agent.harness_max_stall',
    'agent.budget_degrade_ratio',
    'agent.obstacle_recovery_enabled',
  ];
  const uiKeys = new Set(fields.map((f) => f.key));
  const invisible = NEW_KEYS.filter((k) => !uiKeys.has(k));
  check('B', '★★本次新增的 7 个开关**全部**在配置页可见', invisible.length === 0,
        `界面上看不到：${JSON.stringify(invisible)}`);
  // 反向：后端有的新键，前端不应有拼写漂移（如 agent.harness_max_block 少 s）
  const nearMiss = NEW_KEYS.filter((k) => {
    if (uiKeys.has(k)) return false;
    const kk = k.split('.')[1];
    return [...uiKeys].some((u) => {
      const uk = String(u).split('.')[1];
      return u.split('.')[0] === 'agent' && uk && uk !== kk
        && (uk.indexOf(kk) >= 0 || kk.indexOf(uk) >= 0);
    });
  });
  check('B', '无近似拼写漂移（如 max_block 少 s）', nearMiss.length === 0,
        `可能有拼写差异：${JSON.stringify(nearMiss)}`);

  // ══ C. 每个簇至少一个字段（防空簇）══
  const emptyClusters = clusters
    .filter((c) => fields.filter((f) => f.cluster === c.id).length === 0)
    .map((c) => c.id + '：' + c.title);
  check('C', '★无空簇（标题渲染但无内容）', emptyClusters.length === 0,
        JSON.stringify(emptyClusters));
  // 字段的 cluster 必须存在
  const cids = new Set(clusters.map((c) => c.id));
  const orphan = fields.filter((f) => !cids.has(f.cluster)).map((f) => f.key);
  check('C', '★每个字段的 cluster 归属都存在', orphan.length === 0,
        JSON.stringify(orphan));

  // 新簇存在且含 7 项
  const autoexec = clusters.find((c) => c.id === 'autoexec');
  check('C', '★新簇 autoexec 存在', !!autoexec, autoexec ? autoexec.title : '未找到');
  const nAuto = fields.filter((f) => f.cluster === 'autoexec').length;
  check('C', '★新簇 autoexec 含 6 个字段', nAuto >= 6, `实际 ${nAuto} 个`);

  // ══ E. ★★反死开关：界面上每个键都必须被**产品代码**真实读取 ══
  //    为什么这条最硬：一个"拨了没反应"的开关比不显示更糟 ——
  //    用户以为改了行为，实际什么都没发生，且没有任何报错。
  //    判据：除 core/config.py（声明处）与 tools/verify（测试）外，
  //    必须能在其他 .py 里搜到该键名（说明真的被读了）。
  (function antiDeadSwitch() {
    const { execFileSync } = require('child_process');
    // ⚠️ 已知既有死开关白名单（**不是本轮引入的**，但要如实登记而不是假装没有）。
    //    加白必须写明「为什么是死的」+「谁引入的」，且数量受上限约束（见下一条判据），
    //    防止这个表变成"以后新死开关往里一塞就过"的垃圾桶。
    const KNOWN_DEAD = {
      'context.budget_retrieval_ratio':
        '既有（非本轮）：实际读的是绝对值键 context.budget_retrieval_tokens'
        + '（agent/pipeline_parts/history.py），占比制这个键全仓无产品代码读取。'
        + '界面上标著"占比制覆盖绝对值"，但拨了不生效。',
    };
    const dead = [];
    for (const f of fields) {
      const k = String(f.key || '').split('.')[1];
      if (!k) continue;
      // ⚠️ 必须在**整个仓库**搜，不能只搜子目录 ——
      //   第一版只搜 agent/ workflows/ routers/ services/，漏掉了仓库根目录的
      //   knowledge_engine.py / memory_service.py 等，误报了 22 个"死开关"
      //   （实际 rag.recall_k 在 knowledge_engine.py:352 被读）。
      //   ⇒ 这是**判据的假阳性**，按纪律先证伪自己再报问题。
      let hit = '';
      try {
        hit = execFileSync('git', ['grep', '-l', '-F', k], { cwd: ROOT, encoding: 'utf8' });
      } catch (e) {
        hit = '';   // grep 无结果时退出码 1
      }
      const files = String(hit).split('\n').filter(Boolean)
        .filter((f) => !f.endsWith('core/config.py'))       // 声明处不算"读取"
        .filter((f) => !f.startsWith('tools/verify/'))      // 门禁脚本不算
        .filter((f) => !f.startsWith('docs/'))              // 文档不算
        .filter((f) => f.endsWith('.py') || f.endsWith('.js'))
        .filter((f) => !f.endsWith('35-ctxconfig.js'));      // 配置页自身不算
      if (files.length === 0 && !KNOWN_DEAD[f.key]) dead.push(f.key);
    }
    check('E', '★★界面上每个开关都被产品代码真实读取（无死开关）',
          dead.length === 0, `以下开关拨了不会生效：${JSON.stringify(dead)}`);
    // ★ 白名单防腐：登记项必须有理由，且总数受上限约束
    const noReason = Object.keys(KNOWN_DEAD).filter((k) => !KNOWN_DEAD[k]);
    check('E', '已知死开关白名单每项都写明理由', noReason.length === 0,
          JSON.stringify(noReason));
    check('E', '★已知死开关白名单不超过 2 项（防变垃圾桶）',
          Object.keys(KNOWN_DEAD).length <= 2,
          `当前 ${Object.keys(KNOWN_DEAD).length} 项：${JSON.stringify(Object.keys(KNOWN_DEAD))}`);
  })();

  // ══ D. 真实渲染：mock DOM + 真实 loadCtxConfig ══
  // 让真实函数跑起来，断言新簇标题进入 innerHTML
  (function realRender() {
    const fullSrc = fs.readFileSync(UI_FILE, 'utf8');
    // 后端真实 schema（转成前端期望的形状 {sec:{key:{current,default}}}）
    const fullSchema = {};
    const py = `
import sys, json
sys.path.insert(0, r"${ROOT.replace(/\\/g, '\\\\')}")
from core import config as c
out = {}
for sec, items in c.CONFIG_SCHEMA.items():
    out[sec] = {}
    for k in items:
        v = c.get(sec, k)
        out[sec][k] = {"current": v, "default": items[k].get("default")}
print(json.dumps(out, ensure_ascii=False))
`;
    const raw = execFileSync('python', ['-c', py], { encoding: 'utf8' });
    const sch = JSON.parse(raw.trim().split('\n').pop());
    Object.assign(fullSchema, sch);

    const bodyEl = { innerHTML: '' };
    const sandbox = {
      console,
      document: { getElementById: (id) => (id === 'ctx-cfg-body' ? bodyEl : null) },
      window: {},
      esc: (s) => String(s == null ? '' : s),
      // 真实文件依赖的 api()：返回 mock schema
      api: async () => ({ schema: fullSchema }),
      toast: () => {},
      setTimeout: (f) => f(),
      CTX_LV_STYLE: {},
    };
    try {
      vm.createContext(sandbox);
      vm.runInContext(fullSrc, sandbox, { timeout: 15000 });
    } catch (e) {
      check('D', '真实文件在 mock 环境可加载', false, e.message.slice(0, 150));
      return;
    }
    if (typeof sandbox.loadCtxConfig !== 'function') {
      check('D', '找到 loadCtxConfig 函数', false, typeof sandbox.loadCtxConfig);
      return;
    }
    // 同步执行（我们的 api mock 是 async，用 Promise 链等结果）
    Promise.resolve(sandbox.loadCtxConfig()).then(() => {
      const html = String(bodyEl.innerHTML || '');
      check('D', '★真实渲染产出了 HTML', html.length > 1000, `${html.length} 字符`);
      check('D', '★渲染结果含新簇标题「持续自主执行」',
            html.indexOf('持续自主执行') >= 0);
      // 每个新键的 input id 必须出现在 HTML 里
      for (const k of NEW_KEYS) {
        check('D', `★渲染结果含开关 ${k} 的输入控件`,
              html.indexOf('ctx-' + k) >= 0,
              html.indexOf('ctx-' + k) >= 0 ? '' : '未找到 id=ctx-' + k);
      }
      finish();
    }).catch((e) => {
      check('D', '真实渲染执行', false, String(e && e.message).slice(0, 150));
      finish();
    });
  })();

  // 若 D 组没有异步启动（提前 return），也要收尾
  setImmediate(() => { if (!printed) finish(); });
}

let printed = false;
function finish() {
  if (printed) return;
  printed = true;
  console.log('='.repeat(74));
  console.log('门禁：配置开关可见性与前后端对齐');
  console.log('='.repeat(74));
  const groups = {};
  for (const r of RESULTS) (groups[r.group] = groups[r.group] || []).push(r);
  let total = 0, failed = 0;
  for (const g of Object.keys(groups).sort()) {
    console.log(`\n【${g} 组】`);
    for (const r of groups[g]) {
      total++;
      if (!r.ok) failed++;
      let line = `  [${r.ok ? 'PASS' : 'FAIL'}] ${r.name}`;
      if (!r.ok && r.detail) line += `   ← ${r.detail}`;
      console.log(line);
    }
  }
  console.log('\n' + '-'.repeat(74));
  console.log(`合计 ${total} 项，通过 ${total - failed}，失败 ${failed}`);
  console.log('-'.repeat(74));
  if (failed) { console.log('❌ 门禁未通过'); process.exit(1); }
  console.log('✅ 全绿');
  process.exit(0);
}

main();
