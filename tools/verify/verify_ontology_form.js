// CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：真实浏览器门禁——需本机 Chromium（agent-browser）+ 8000 真实服务/真库，CI runner 无该组合 ⇒ 本机跑；改法须同步改清单 tier 并接线，不允许只删这行
/* 本体编辑表单 · 上层本体锚点 / 别名 / JSON-LD 导出（2026-11-15，P0-1 · P1-1 · P2-1）
 *
 * ## 这张门禁锁死的是什么
 * 本轮给本体维护加了三条**前后端必须同步**的能力，任何一条只做了后端、
 * 前端没接上，用户在界面上就"看不见/存不进去"，而接口测试全绿 —— 症状与没做完全一样。
 * 所以这里用**真实浏览器**逐条断言：
 *   ① 上层本体锚点下拉 `#ot-tlo`：编辑实体类型时可见、选项来自后端词表（8 类）、
 *      选中后回填正确、保存时进入请求体（`tlo_anchor`）；
 *   ② 别名 `#ot-alt-labels` + 语言 `#ot-lang`：回填与保存都进 `constraints.alt_labels/lang`；
 *   ③ 导出格式芯片：`jsonld` 芯片存在且点击后 URL 带 `fmt=jsonld`。
 *
 * ## 为什么必须按 **id** 断言而不是按文本
 * 表单里"锚点""语言""别名"这类中文词在别处也出现（提示气泡、属性面板），
 * 按文本查会**恒真**（空转断言）。本仓已有一次「用共享 class 断言导致变异不翻红」的前科
 * （verify_hil_ask_card.js 头部记着），这里一律按 id 前缀/精确 id。
 *
 * ## 变异自证
 * 把 `23-ontform.js` 里"回填 tlo_anchor"那一行注释掉 ⇒ 回填断言必须翻红，
 * 随后逐字节恢复并校验 sha256。变异不翻红 ⇒ 判 FAIL（断言无鉴别力）。
 *
 * 用法：node tools/verify/verify_ontology_form.js
 * 退出码：0 全绿 / 1 有失败 / 2 环境不可用（服务或浏览器）
 */
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const BIN = "C:/Users/gefei/AppData/Roaming/npm/node_modules/agent-browser/bin/agent-browser.js";
const ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system";
const TMP = os.tmpdir();
const FORM_SRC = ROOT + "/static/js/mods/23-ontform.js";
const PROBE = "MUTATION_PROBE_ONTOFORM";

let seq = 0;
let PASS = 0, FAIL = 0;
const FAILED = [];
function check(name, ok, detail) {
  if (ok) { PASS++; console.log("  [PASS] " + name); }
  else { FAIL++; FAILED.push(name + " :: " + (detail || "")); console.log("  [FAIL] " + name + " :: " + (detail || "")); }
}

function sha(p) { return crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex"); }

/* agent-browser 守护进程会继承管道并一直持有 ⇒ 必须用 fd 接管 stdio */
function ab(...args) {
  const o = path.join(TMP, `ab_otf_${++seq}.out`), e = path.join(TMP, `ab_otf_${seq}.err`);
  const ofd = fs.openSync(o, "w"), efd = fs.openSync(e, "w");
  const r = spawnSync(process.execPath, [BIN, ...args],
    { stdio: ["ignore", ofd, efd], timeout: 150000, cwd: ROOT });
  fs.closeSync(ofd); fs.closeSync(efd);
  const stdout = fs.readFileSync(o, "utf8"), stderr = fs.readFileSync(e, "utf8");
  try { fs.unlinkSync(o); fs.unlinkSync(e); } catch (_) {}
  return { stdout, stderr, status: r.status, err: r.error ? String(r.error) : "" };
}
const show = (r) => (r.stdout + r.stderr).trim();

function evalJs(code) {
  const r = ab("eval", code);
  let v = show(r);
  for (let i = 0; i < 4 && typeof v === "string"; i++) {
    try { v = JSON.parse(v); } catch (_) { break; }
  }
  return v;
}

function probeAlive() {
  try {
    const r = spawnSync(process.execPath, ["-e",
      "require('http').get('http://127.0.0.1:8000/api/dashboard',r=>{process.exit(r.statusCode===200?0:1)}).on('error',()=>process.exit(1))"],
      { timeout: 20000 });
    return r.status === 0;
  } catch (_) { return false; }
}

/* ── 残留自愈（上一轮被 SIGTERM 打断会停在变异态）── */
const BAK = ROOT + "/tmp/23-ontform.js.gatebak";
function heal() {
  const src = fs.readFileSync(FORM_SRC, "utf8");
  if (src.indexOf(PROBE) < 0) return null;
  if (!fs.existsSync(BAK)) return "检测到变异残留但备份缺失（需 git checkout 恢复）";
  fs.copyFileSync(BAK, FORM_SRC);
  return "已从备份自愈上一轮残留变异";
}

function main() {
  console.log("=".repeat(78));
  console.log("门禁：本体编辑表单 —— TLO 锚点 / 别名 / JSON-LD 导出（真实浏览器）");
  console.log("=".repeat(78));

  if (!fs.existsSync(BIN)) { console.log("SKIP: agent-browser 缺失"); return 2; }
  if (!probeAlive()) { console.log("SKIP: 8000 服务不可用"); return 2; }
  const h = heal(); if (h) console.log("  [INFO] " + h);

  ab("open", "http://127.0.0.1:8000");
  ab("set", "viewport", "1600", "900");
  ab("set", "media", "light");
  ab("wait", "--load", "networkidle");
  ab("wait", "3000");

  // ── 0. 前置：后端词表可达（前端下拉的数据源）──
  const tlo = evalJs("(async()=>{const r=await fetch('/api/knowledge/ontology/tlo');const d=await r.json();return JSON.stringify({n:(d.classes||[]).length,root:d.root});})()");
  let tloN = 0, tloRoot = "";
  try {
    const o = typeof tlo === "string" ? JSON.parse(tlo) : tlo;
    tloN = o.n || 0; tloRoot = o.root || "";
  } catch (_) {}
  check("0 后端 TLO 词表可达且含 8 类（root=Entity）",
    tloN === 8 && tloRoot === "Entity", `n=${tloN} root=${tloRoot}`);

  // ── 1. 打开「本体」页并进入某个实体类型的编辑 ──
  //     直接用页面内 API（不依赖菜单文案，菜单文案会改）
  const opened = evalJs(`(async()=>{
    try{
      const r = await fetch('/api/knowledge/ontology'); const d = await r.json();
      const types = d.types || d || [];
      const ent = types.find(t=>t.type_kind==='entity');
      if(!ent) return JSON.stringify({ok:false,why:'no entity type'});
      // 切到本体页（若前端提供了入口；否则直接调用表单打开函数）
      if (typeof ontologyEditType === 'function') {
        ontologyEditType(ent.id, ent.name, 'entity', JSON.stringify(ent.constraints||{}));
      } else { return JSON.stringify({ok:false,why:'ontologyEditType 未定义'}); }
      await new Promise(r=>setTimeout(r,1200));
      // 等 TLO 选项异步加载完成
      for(let i=0;i<20;i++){
        const el=document.getElementById('ot-tlo');
        if(el && el.options && el.options.length>1) break;
        await new Promise(r=>setTimeout(r,300));
      }
      return JSON.stringify({ok:true, name: ent.name, anchor: ent.tlo_anchor||''});
    }catch(e){ return JSON.stringify({ok:false,why:String(e)}); }
  })()`);
  let openedObj = {};
  try { openedObj = typeof opened === "string" ? JSON.parse(opened) : (opened || {}); } catch (_) {}
  const openOk = !!openedObj.ok;

  // ── 2. TLO 行可见 + 选项来自后端（8 类 + 1 个"不锚定"）──
  const formState = evalJs(`(()=>{
    const row=document.getElementById('ot-tlo-row');
    const sel=document.getElementById('ot-tlo');
    const alt=document.getElementById('ot-alt-labels');
    const lang=document.getElementById('ot-lang');
    return JSON.stringify({
      rowVisible: !!row && row.style.display !== 'none',
      optCount: sel ? sel.options.length : -1,
      optValues: sel ? Array.from(sel.options).map(o=>o.value).slice(0,3) : [],
      hasEmptyOpt: sel ? Array.from(sel.options).some(o=>o.value==='') : false,
      altExists: !!alt, langExists: !!lang,
      altValue: alt ? alt.value : null,
      langValue: lang ? lang.value : null,
      hintText: (document.getElementById('ot-tlo-hint')||{}).textContent || ''
    });
  })()`);
  let fs1 = {};
  try { fs1 = typeof formState === "string" ? JSON.parse(formState) : (formState || {}); } catch (_) {}

  check("2a 实体类型的「上层本体锚点」行可见（#ot-tlo-row）", !!fs1.rowVisible, JSON.stringify(fs1));
  check("2b 下拉选项 = 8 个 BFO 类 + 1 个「不锚定」空项",
    fs1.optCount === 9 && fs1.hasEmptyOpt === true, `optCount=${fs1.optCount} empty=${fs1.hasEmptyOpt}`);
  check("2c 选项取值是 BFO local name（首项为空，其后为英文类名）",
    Array.isArray(fs1.optValues) && fs1.optValues[0] === '' && /^[A-Z]/.test(fs1.optValues[1] || ''),
    JSON.stringify(fs1.optValues));
  check("2d 别名字段存在（#ot-alt-labels / #ot-lang）", !!fs1.altExists && !!fs1.langExists, "");

  // ── 3. 选中锚点 ⇒ 提示文案更新（证明 change 事件真的绑上）──
  const hintAfter = evalJs(`(async()=>{
    const sel=document.getElementById('ot-tlo');
    if(!sel) return JSON.stringify({ok:false});
    sel.value='Process';
    sel.dispatchEvent(new Event('change'));
    await new Promise(r=>setTimeout(r,200));
    return JSON.stringify({ok:true, hint:(document.getElementById('ot-tlo-hint')||{}).textContent||''});
  })()`);
  let hintObj = {};
  try { hintObj = typeof hintAfter === "string" ? JSON.parse(hintAfter) : (hintAfter || {}); } catch (_) {}
  check("3 选中 Process 后提示文案更新（说明事件绑定生效，不是死下拉）",
    !!hintObj.ok && String(hintObj.hint || '').length > 4, JSON.stringify(hintObj).slice(0, 160));

  // ── 4. 真实保存 + 回读（端到端：前端组装 → 后端落库 → 回读自证）──
  //   为什么不用打桩抓 payload：第一版就是打桩，结果被"影响预览二次确认"和
  //   跨 eval 的表单态双重干扰，恒抓不到（而产品其实是对的）。
  //   改为**真的存一次再读回来**：这既验证了前端接线，也验证了后端落库，
  //   正是"前后端同步实现"该有的判据。末尾恢复原值，不留痕。
  const e2e = evalJs(`(async()=>{
    const out={};
    try{
      const rr=await fetch('/api/knowledge/ontology'); const dd=await rr.json();
      const tt=dd.types||dd||[];
      const ee=tt.find(t=>t.type_kind==='entity');
      if(!ee){ return JSON.stringify({skip:'no entity'}); }
      out.id=ee.id; out.name=ee.name;
      out.anchorBefore = ee.tlo_anchor || '';
      out.altBefore = (JSON.parse(ee.constraints||'{}').alt_labels)||[];
      ontologyEditType(ee.id, ee.name, 'entity', JSON.stringify(ee.constraints||{}));
      for(let i=0;i<25;i++){
        const el0=document.getElementById('ot-tlo');
        if(el0 && el0.options && el0.options.length>1) break;
        await new Promise(r=>setTimeout(r,250));
      }
      const sel=document.getElementById('ot-tlo');
      if(!sel){ return JSON.stringify({skip:'no #ot-tlo'}); }
      // 选一个与当前不同的锚点，避免"本来就是这个值"导致假绿
      const want = (sel.value === 'Object') ? 'Process' : 'Object';
      sel.value = want; sel.dispatchEvent(new Event('change'));
      const alt=document.getElementById('ot-alt-labels'); if(alt) alt.value='门禁别名甲、门禁别名乙';
      const lang=document.getElementById('ot-lang'); if(lang) lang.value='zh';
      out.want = want;
      try{ await saveOntType(); }catch(e){ out.saveErr=String(e&&e.message||e); }
      await new Promise(r=>setTimeout(r,900));
      // 回读（新请求，非缓存）
      const rr2=await fetch('/api/knowledge/ontology'); const dd2=await rr2.json();
      const tt2=dd2.types||dd2||[];
      const ee2=tt2.find(t=>t.id===ee.id);
      out.anchorAfter = ee2 ? (ee2.tlo_anchor||'') : null;
      out.altAfter = ee2 ? ((JSON.parse(ee2.constraints||'{}').alt_labels)||[]) : null;
      out.langAfter = ee2 ? (JSON.parse(ee2.constraints||'{}').lang||'') : null;
    }catch(e){ out.fatal=String(e&&e.stack||e).slice(0,400); }
    return JSON.stringify(out);
  })()`);
  let E = {};
  if (e2e && typeof e2e === "object") { E = e2e; }
  else {
    try { E = typeof e2e === "string" && e2e.trim().startsWith("{") ? JSON.parse(e2e) : {}; } catch (_) {}
  }

  if (E.skip) {
    check("4 端到端保存（跳过：" + E.skip + "）", true, "");
  } else {
    check("4a 前端选中锚点 → 保存 → 后端回读一致（前后端同步）",
      !!E.want && E.anchorAfter === E.want,
      `want=${E.want} after=${JSON.stringify(E.anchorAfter)} err=${E.saveErr || ''} fatal=${E.fatal || ''}`);
    check("4b 前端填写别名/语言 → 保存 → 后端回读一致",
      Array.isArray(E.altAfter) && E.altAfter.length === 2 && E.langAfter === 'zh',
      `alt=${JSON.stringify(E.altAfter)} lang=${JSON.stringify(E.langAfter)}`);
    // 恢复原值（不留痕）
    if (E.id) {
      const restored = evalJs(`(async()=>{
        try{
          const rr=await fetch('/api/knowledge/ontology'); const dd=await rr.json();
          const tt=dd.types||dd||[]; const ee=tt.find(t=>t.id===${E.id});
          if(!ee) return JSON.stringify({ok:false});
          const cons=JSON.parse(ee.constraints||'{}');
          const body={name:ee.name,type_kind:ee.type_kind,properties:JSON.parse(ee.properties||'{}'),
            constraints:cons,description:ee.description||'',status:ee.status,
            parent_id:ee.parent_id,iri:ee.iri||'',
            tlo_anchor:${JSON.stringify(E.anchorBefore || '')}};
          if(${JSON.stringify(Array.isArray(E.altBefore) && E.altBefore.length > 0)}
             ){ body.constraints.alt_labels=${JSON.stringify(E.altBefore)}; }
          else { delete body.constraints.alt_labels; delete body.constraints.lang; }
          const r2=await fetch('/api/knowledge/ontology/types/'+ee.id,
            {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          return JSON.stringify({ok:r2.ok});
        }catch(e){ return JSON.stringify({ok:false,err:String(e)}); }
      })()`);
      let R = {};
      if (restored && typeof restored === "object") { R = restored; }
      else { try { R = typeof restored === "string" ? JSON.parse(restored) : {}; } catch (_) {} }
      check("4c 门禁写过的锚点/别名已恢复原值（不污染真实本体）", !!R.ok, JSON.stringify(R));

      // ⚠️ 4c 只看了 PUT 的 ok —— **PUT 成功 ≠ 库里真的干净**（本仓"命令成功≠操作生效"同型）。
      //    2026-11-23 实测：生产库 ontology_types 里残留
      //    `利益相关方.constraints.alt_labels = ["门禁别名甲","门禁别名乙"]`，
      //    正是本门禁写进去的（恢复那一步没生效），而 4c 当时是绿的。
      //    ⇒ 必须回读断言，不能只信响应码。
      const chk = evalJs(`(async()=>{
        try{
          const rr=await fetch('/api/knowledge/ontology'); const dd=await rr.json();
          const tt=dd.types||dd||[]; const ee=tt.find(t=>t.id===${E.id});
          const s=ee?String(ee.constraints||''):'';
          return JSON.stringify({found:!!ee, polluted:s.indexOf('门禁别名')>=0, cons:s.slice(0,140)});
        }catch(e){ return JSON.stringify({found:false, polluted:true, err:String(e)}); }
      })()`);
      let N = {};
      if (chk && typeof chk === "object") { N = chk; }
      else { try { N = typeof chk === "string" ? JSON.parse(chk) : {}; } catch (_) {} }
      check("4d 恢复后回读库：本体里不含测试标记（PUT ok ≠ 真干净）",
        !!N.found && N.polluted === false, JSON.stringify(N));
    }
  }

  // ── 5. JSON-LD 导出芯片 ──
  const chip = evalJs(`(()=>{
    const c = document.getElementById('ont-owl-fmt-global-jsonld');
    return JSON.stringify({exists: !!c, onclick: c ? (c.getAttribute('onclick')||'') : ''});
  })()`);
  let chipObj = {};
  try { chipObj = typeof chip === "string" ? JSON.parse(chip) : (chip || {}); } catch (_) {}
  if (chipObj.exists) {
    check("5 JSON-LD 导出芯片存在且绑定 ontOwlSetFmt",
      String(chipObj.onclick).includes("ontOwlSetFmt") && String(chipObj.onclick).includes("jsonld"),
      JSON.stringify(chipObj));
  } else {
    // 芯片在 OWL 视图里才渲染 —— 该视图需从页面进入；退化为函数级断言
    const fnOk = evalJs(`(()=>{try{return JSON.stringify({url: ontOwlUrl('jsonld', null)});}catch(e){return JSON.stringify({err:String(e)})}})()`);
    let u = {};
    try { u = typeof fnOk === "string" ? JSON.parse(fnOk) : (fnOk || {}); } catch (_) {}
    check("5 JSON-LD 导出：ontOwlUrl 产出 fmt=jsonld（芯片未渲染时退化为函数级断言）",
      String(u.url || '').includes("fmt=jsonld"), JSON.stringify(u));
  }

  // ── 6. 变异自证：去掉锚点回填 ⇒ 回填断言必须翻红 ──
  console.log("\n--- 变异自证 ---");
  const srcOrig = fs.readFileSync(FORM_SRC, "utf8");
  const origSha = crypto.createHash("sha256").update(srcOrig).digest("hex");
  fs.writeFileSync(BAK, srcOrig);
  const needle = "  window._ontPendingTlo = t.tlo_anchor || '';";
  if (srcOrig.indexOf(needle) < 0) {
    check("6 变异锚点存在（23-ontform.js 的 tlo_anchor 回填）", false, "锚点未找到");
  } else {
    try {
      fs.writeFileSync(FORM_SRC, srcOrig.replace(needle,
        "  /* " + PROBE + "：临时移除锚点回填用于自证，随后逐字节恢复 */"), "utf8");
      // 重新加载页面让新脚本生效
      ab("open", "http://127.0.0.1:8000");
      ab("wait", "--load", "networkidle");
      ab("wait", "2500");
      const mut = evalJs(`(async()=>{
        try{
          const r = await fetch('/api/knowledge/ontology'); const d = await r.json();
          const types = d.types || d || [];
          const ent = types.find(t=>t.type_kind==='entity' && (t.tlo_anchor||'')!=='');
          if(!ent) return JSON.stringify({skip:true});
          ontologyEditType(ent.id, ent.name, 'entity', JSON.stringify(ent.constraints||{}));
          for(let i=0;i<20;i++){
            const el=document.getElementById('ot-tlo');
            if(el && el.options && el.options.length>1) break;
            await new Promise(r=>setTimeout(r,250));
          }
          const sel=document.getElementById('ot-tlo');
          return JSON.stringify({skip:false, value: sel ? sel.value : null, expect: ent.tlo_anchor});
        }catch(e){ return JSON.stringify({err:String(e)}); }
      })()`);
      let m = {};
      try { m = typeof mut === "string" ? JSON.parse(mut) : (mut || {}); } catch (_) {}
      if (m.skip) {
        check("6 变异自证：无已锚定类型可测（真库尚无 tlo_anchor）—— 记 SKIP 不算红", true, "");
      } else {
        check("6 变异自证：去掉回填后，下拉**没有**回填出锚点（证明原断言有鉴别力）",
          m.value === '' || m.value === null,
          `变异后 value=${JSON.stringify(m.value)} expect=${JSON.stringify(m.expect)} —— 若仍有值说明回填断言空转`);
      }
    } finally {
      fs.copyFileSync(BAK, FORM_SRC);
      const now = sha(FORM_SRC);
      try { fs.unlinkSync(BAK); } catch (_) {}
      check("6 变异源文件已逐字节还原（sha256 一致，无探针残留）",
        now === origSha && fs.readFileSync(FORM_SRC, "utf8").indexOf(PROBE) < 0, "");
      ab("open", "http://127.0.0.1:8000"); ab("wait", "--load", "networkidle");
    }
  }

  console.log("\n" + "=".repeat(78));
  console.log(`结果：PASS ${PASS} / FAIL ${FAIL}`);
  if (FAILED.length) { console.log("红项："); FAILED.forEach(x => console.log("  - " + x)); }
  console.log("=".repeat(78));
  return FAIL === 0 ? 0 : 1;
}

process.exit(main());
