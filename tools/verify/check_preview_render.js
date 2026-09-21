// 校验 preview_ctxconfig_page.py 产物的**真实可渲染性**：不是看文件大小，
// 而是把页面里的脚本抽出来、在 DOM 桩上跑一遍，断言渲染结果。
//
// 用法: node tools/verify/check_preview_render.js <产物.html>
// 断言：脚本段数 / 簇数与字段数 / 每字段都归属已定义簇 / 取值无 NaN /
//       逐簇可视化与校验文案无 undefined / 真调一次 loadCtxConfig 得到 7 个面板 /
//       渲染产物里没有未转换的 markdown 粗体 **（本工程无 markdown 解析器）
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length !== 3) { console.error('❌ 期望 3 段脚本，实际 ' + scripts.length); process.exit(1); }

// 极小 DOM 桩：只实现本页用到的 id 查找 + scrollIntoView
const stores = {};
const mkEl = () => ({
  innerHTML: '', value: '', textContent: '', style: {},
  scrollIntoView() {}, getAttribute: () => null,
});
global.document = {
  getElementById(id) { return stores[id] || (stores[id] = mkEl()); },
  styleSheets: [],
};
global.setTimeout = () => 0; global.clearTimeout = () => {};

const fn = new Function(scripts.join('\n')
  + '\nreturn {CTX_CFG_CLUSTERS, CTX_CFG_FIELDS, CTX_CHECKS, ctxViz, ctxReadVals, loadCtxConfig};');
const M = fn();

const fails = [];
const ck = (cond, msg) => { if (!cond) fails.push(msg); };

ck(M.CTX_CFG_CLUSTERS.length === 7, '簇数应为 7，实际 ' + M.CTX_CFG_CLUSTERS.length);
ck(M.CTX_CFG_FIELDS.length === 32, '字段数应为 32，实际 ' + M.CTX_CFG_FIELDS.length);

const ids = new Set(M.CTX_CFG_CLUSTERS.map(c => c.id));
for (const f of M.CTX_CFG_FIELDS) ck(ids.has(f.cluster), '字段 ' + f.key + ' 的簇不存在: ' + f.cluster);

(async () => {
  // 先跑真实入口：它会把 schema 存进模块内的 _ctxSchema，取值才不是空
  await M.loadCtxConfig();
  const body = stores['ctx-cfg-body'];
  const panels = (body.innerHTML.match(/class="panel"/g) || []).length;
  ck(panels === 7, '渲染面板数应为 7，实际 ' + panels);
  ck(!/undefined|NaN|加载失败/.test(body.innerHTML), '面板 HTML 含 undefined/NaN/加载失败');
  ck(!/\*\*/.test(body.innerHTML), '面板 HTML 含未转换的 markdown 粗体 **');

  const v = M.ctxReadVals();
  for (const k of Object.keys(v)) {
    ck(v[k] !== undefined && v[k] !== null && !Number.isNaN(v[k]), '取值异常 ' + k + '=' + v[k]);
  }

  let htmlOut = '';
  for (const c of M.CTX_CFG_CLUSTERS) {
    // viz:'none' 的簇（融合公式与补召）有意不画耦合图，只要不抛错即可
    const viz = M.ctxViz(c, v);
    ck(typeof viz === 'string', '簇 ' + c.id + ' 可视化返回非字符串');
    ck(!!viz || c.viz === 'none', '簇 ' + c.id + '（viz=' + c.viz + '）未产出可视化');
    htmlOut += viz;
    for (const k of (c.checks || [])) {
      const r = M.CTX_CHECKS[k](v);
      ck(Array.isArray(r) && r.length > 0, '校验器 ' + k + ' 无返回');
      for (const x of r) {
        ck(['err', 'warn', 'ok', 'info'].includes(x.level), '校验等级非法: ' + x.level);
        htmlOut += x.text;
        ck(!/undefined|NaN/.test(x.text), '校验文案含 undefined/NaN: ' + x.text);
      }
    }
  }
  ck(!/undefined|NaN/.test(htmlOut), '渲染产物含 undefined/NaN');

  if (fails.length) {
    console.error('❌ ' + fails.length + ' 项失败：\n  ' + fails.join('\n  '));
    process.exit(1);
  }
  console.log('✅ 预览渲染校验通过：7 簇 / ' + M.CTX_CFG_FIELDS.length + ' 字段 / ' + panels + ' 面板 / 无 undefined');
})();
