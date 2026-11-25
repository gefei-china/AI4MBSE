/* 本体运行期语义健康度（2026-11-24 第一期 + A3 四动作）
 * 复用现有 #ont-slide 右侧滑窗（用户约定：弹窗统一右侧滑面板）。
 * 全局作用域（非 module），index.html 内联 onclick 依赖全局函数名。
 * 零外部依赖（私有化离线部署要求）。
 */
function openOntRuntime() {
  const body = document.getElementById('ont-slide-body');
  const title = document.getElementById('ont-slide-title');
  const overlay = document.getElementById('ont-overlay');
  const panel = document.getElementById('ont-slide');
  if (!body || !panel) { if (window.toast) toast('本体滑窗未就绪'); return; }
  if (title) title.textContent = '📊 运行期语义健康度';
  body.innerHTML = '<div style="color:var(--mut);font-size:13.5px;">正在加载体检、SHACL、能力问题与推理数据…</div>';
  overlay && overlay.classList.add('show');
  panel.classList.add('open');
  loadOntRuntimeMetrics(body);
}

function _ontCard(label, value, sub, tone) {
  return `<div style="flex:1;min-width:132px;border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:#fff;">
     <div style="font-size:12px;color:var(--mut);">${label}</div>
     <div style="font-size:23px;font-weight:700;color:${tone || 'var(--blue-d)'};line-height:1.3;margin:4px 0;">${value}</div>
     <div style="font-size:11.5px;color:var(--mut);">${sub || ''}</div>
   </div>`;
}

async function loadOntRuntimeMetrics(body) {
  // 五个数据源互不阻塞：单个失败不影响其它卡片（降级可见，不静默）
  const [chk, shacl, cq, cohorts, inferred] = await Promise.all([
    api('/api/knowledge/ontology/validate?deep=1&record=0&cq=0').catch(e => ({ __err: String(e) })),
    api('/api/governance/shacl-findings?limit=1').catch(e => ({ __err: String(e) })),
    api('/api/knowledge/ontology/cqs/logs?limit=100').catch(e => ({ __err: String(e) })),
    api('/api/knowledge/reasoning/cohorts').catch(e => ({ __err: String(e) })),
    api('/api/knowledge/reasoning/inferred?limit=1').catch(e => ({ __err: String(e) })),
  ]);

  const scanned = chk && !chk.__err ? (chk.instances_scanned ?? 0) : 0;
  const checked = chk && chk.instances_checked ? '已开启' : '未开启';
  const issues = chk && !chk.__err ? (chk.issue_total ?? 0) : 0;
  const degraded = chk && chk.degraded ? chk.degraded.length : 0;
  const findings = shacl && !shacl.__err ? (shacl.count ?? 0) : '—';

  // A3-bugfix：端点返回 {ok, logs}，此前误读 items 导致真库 965 条显示成 0
  const logs = cq && !cq.__err ? (cq.logs || cq.items || (Array.isArray(cq) ? cq : [])) : [];
  const pass = logs.filter(x => x.status === 'pass').length;
  const skip = logs.filter(x => x.status === 'skip').length;
  const fail = logs.filter(x => x.status === 'fail').length;
  const rate = logs.length ? Math.round(pass / logs.length * 100) : 0;

  // A3-d：推理裁决数（待审批次 + 已物化推断）
  const cohortRows = cohorts && !cohorts.__err ? (cohorts.rows || []) : [];
  const pending = cohortRows.filter(x => x.status === 'pending').length;
  const inferredN = inferred && !inferred.__err ? (inferred.count ?? 0) : '—';
  const errs = [chk, shacl, cq, cohorts, inferred].filter(x => x && x.__err).length;

  body.innerHTML = `
    <div style="font-size:13px;color:var(--mut);margin-bottom:14px;line-height:1.7;">
      以下数字来自真实体检与执行历史。体检覆盖面 &gt; 0 才说明"检查过了"；
      跳过（skip）不计入通过。${errs ? `<span style="color:var(--red);">（${errs} 项数据源读取失败）</span>` : ''}
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;">
      ${_ontCard('实例级体检', checked, 'deep 模式', scanned > 0 ? 'var(--blue-d)' : 'var(--mut)')}
      ${_ontCard('实际扫描数据', scanned, '实体+关系+已审三元组', scanned > 0 ? 'var(--blue-d)' : 'var(--red)')}
      ${_ontCard('结构问题', issues, `降级扫描 ${degraded} 项`, degraded ? 'var(--red)' : 'var(--blue-d)')}
      ${_ontCard('SHACL 发现', findings, '校验违规留痕', findings === '—' ? 'var(--mut)' : (findings ? 'var(--red)' : 'var(--blue-d)'))}
    </div>
    <div style="font-size:14px;font-weight:600;margin-bottom:10px;">❓ 能力问题（最近 ${logs.length} 次执行）</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
      ${_ontCard('通过', pass, `通过率 ${rate}%`, 'var(--blue-d)')}
      ${_ontCard('失败', fail, '需关注', fail ? 'var(--red)' : 'var(--blue-d)')}
      ${_ontCard('跳过', skip, '降级不是通过', skip ? 'var(--mut)' : 'var(--blue-d)')}
    </div>
    <div id="ont-cq-skip-detail" style="font-size:12px;color:var(--mut);margin-bottom:16px;line-height:1.6;"></div>
    <div style="font-size:14px;font-weight:600;margin-bottom:10px;">⚖ 推理裁决（A3-d）</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;">
      ${_ontCard('待审批次', pending, '推理物化等待人工裁决', pending ? 'var(--red)' : 'var(--blue-d)')}
      ${_ontCard('已物化推断', inferredN, 'status=inferred', 'var(--blue-d)')}
    </div>

    <div style="border-top:1px solid var(--line);padding-top:14px;margin-bottom:16px;">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px;">🔍 A3-a 按对象查上下文</div>
      <div style="display:flex;gap:6px;">
        <input id="ont-runtime-q" placeholder="输入需求/部件编号或名称" style="flex:1;padding:7px 10px;border:1px solid var(--line);border-radius:6px;font-size:13px;">
        <button class="btn sm" onclick="ontQueryContext()">查询</button>
      </div>
      <div id="ont-runtime-result" style="font-size:12.5px;margin-top:8px;color:var(--mut);line-height:1.7;"></div>
    </div>

    <div style="border-top:1px solid var(--line);padding-top:14px;margin-bottom:16px;">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px;">📖 A3-c 术语批量映射</div>
      <div id="ont-map-panel" style="font-size:12.5px;color:var(--mut);">正在加载候选…</div>
    </div>

    <div style="border-top:1px solid var(--line);padding-top:12px;font-size:12px;color:var(--mut);line-height:1.8;">
      · <b>实际扫描数据</b>为本次实例级体检覆盖的实体、关系与已审核三元组总数。<br>
      · <b>跳过</b>表示能力问题因图库/新鲜度原因未执行，不能当作通过。<br>
      · 本看板只读不写库（体检 record=0），不会在历史里留下机器噪声。
    </div>`;

  // A3-b：skip 原因展示（取最近 skip 的 message / freshness）
  const skipped = logs.filter(x => x.status === 'skip').slice(0, 3);
  const sd = document.getElementById('ont-cq-skip-detail');
  if (sd) sd.innerHTML = skipped.length
    ? '跳过原因：' + skipped.map(x => (x.message || '无原因')).join('；').slice(0, 300)
    : '（本次无跳过项）';

  loadOntMapCandidates();
}

// A3-a：对象 ID → 统一上下文
async function ontQueryContext() {
  const el = document.getElementById('ont-runtime-q');
  const out = document.getElementById('ont-runtime-result');
  const q = el && el.value.trim();
  if (!out) return;
  if (!q) { out.innerHTML = '<span style="color:var(--red);">请输入对象编号或名称</span>'; return; }
  out.innerHTML = '查询中…';
  try {
    const r = await api('/api/knowledge/ontology/context', {
      method: 'POST', body: JSON.stringify({ id: q, depth: 1 })
    });
    if (!r.found) {
      out.innerHTML = `<span style="color:var(--red);">对象不存在：${q}（系统未编造任何关系）</span>`;
      return;
    }
    const o = r.object || {};
    const rels = (r.relations || []).slice(0, 8).map(x =>
      `${x.direction === 'out' ? '→' : '←'} ${x.relation_type} ${x.other_name}`).join('；');
    out.innerHTML = `<b>${o.name}</b>（${o.entity_type}）｜属性 ${Object.keys(r.attributes || {}).length} 项｜`
      + `关系 ${(r.relations || []).length} 条：${rels || '无'}`;
  } catch (e) {
    out.innerHTML = `<span style="color:var(--red);">查询失败：${String(e)}</span>`;
  }
}

// A3-c：候选清单 + 勾选 + 批量确认（人工确认制，不自动写入）
async function loadOntMapCandidates() {
  const box = document.getElementById('ont-map-panel');
  if (!box) return;
  try {
    const r = await api('/api/glossary/map-candidates?limit=3');
    const items = (r.items || []).filter(x => x.candidates && x.candidates.length).slice(0, 8);
    if (!items.length) { box.innerHTML = '无可自动建议的候选（需人工定义映射）'; return; }
    box.innerHTML = `<div style="margin-bottom:6px;">${r.total_empty} 条未映射概念中 ${r.with_candidates} 条有候选，列前 ${items.length}：</div>`
      + items.map((x, i) => `<div style="display:flex;align-items:center;gap:6px;margin:4px 0;">
           <input type="checkbox" id="ont-map-${i}" checked>
           <span>${x.pref_label}</span>
           <select id="ont-map-sel-${i}" style="flex:1;padding:3px;border:1px solid var(--line);border-radius:4px;">
             ${x.candidates.map(c => `<option value="${c.name}">${c.name} (${c.score})</option>`).join('')}
           </select></div>`).join('')
      + `<button class="btn sm" style="margin-top:6px;" onclick="ontApplyMapping(${items.length})">确认选中映射</button>
         <span id="ont-map-result" style="margin-left:8px;"></span>`;
    window.__ontMapItems = items;
  } catch (e) {
    box.innerHTML = `候选加载失败：${String(e)}`;
  }
}

async function ontApplyMapping(n) {
  const out = document.getElementById('ont-map-result');
  if (!out) return;
  const pairs = [];
  for (let i = 0; i < n; i++) {
    const cb = document.getElementById('ont-map-' + i);
    const sel = document.getElementById('ont-map-sel-' + i);
    if (cb && cb.checked && sel && (window.__ontMapItems || [])[i]) {
      pairs.push({ concept_id: window.__ontMapItems[i].concept_id, class_name: sel.value });
    }
  }
  if (!pairs.length) { out.textContent = '未选中任何项'; return; }
  out.textContent = '提交中…';
  try {
    const r = await api('/api/glossary/apply-mapping', {
      method: 'POST', body: JSON.stringify({ pairs })
    });
    out.textContent = `已写入 ${r.applied} 条 / 当前覆盖 ${r.mapped_total}`;
    loadOntMapCandidates();
  } catch (e) {
    out.textContent = `失败：${String(e)}`;
  }
}
