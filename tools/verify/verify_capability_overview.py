# -*- coding: utf-8 -*-
"""平台能力总览 + 告警跳会话（2026-10-18 v2 方案收口）真实库验证。

验证三件事：
  V0 静态挂载：index.html 含 ops-capability 面板；31-admin.js 含 loadOpsCapability 挂载
     与「跳会话」按钮代码（防止"函数写了但页面没挂"这类静默脱节）。
  V1 后端字段（真实库**只读**）：前端渲染消费的每个字段路径都真实返回——
     orphan-runs 的孤儿/TTL/active、sessions 的 flagged/total/days、
     tenant-readiness 的 mode/blockers/entities.orphan_project、
     dashboard 透传的 llm_health（口径唯一真源 core/llm_health.py）。
  V2 前端渲染冒烟（Node vm 加载真实 31-admin.js）：
     - 正常路径：四层标题齐备、无 undefined/NaN 泄漏、flagged 徽章正确；
     - 降级路径：三路补充取数全部失败（api 抛错）仍整屏渲染（单层显示「—」）；
     - 告警行：conversation_id>0 渲染「跳会话」且 onclick 绑定正确，=0 不渲染，
       ack 按钮不受影响。

用法（项目 .venv，仓库根目录执行）：
    .venv/Scripts/python.exe tools/verify/verify_capability_overview.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# Node 运行时：优先 managed 版本（环境隔离纪律），缺失回退系统 node
NODE = r"C:\Users\gefei\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
if not os.path.exists(NODE):
    NODE = "node"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  -- {detail}" if detail else ""))


# ── Node 侧渲染 harness：vm 加载真实 31-admin.js，用真实端点 payload 驱动渲染 ──
HARNESS = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = process.argv[2], PF = process.argv[3];
const P = JSON.parse(fs.readFileSync(PF, 'utf8'));
const src = fs.readFileSync(path.join(ROOT, 'static/js/mods/31-admin.js'), 'utf8');
const out = [];
const check = (n, ok, d) => { out.push((ok ? 'PASS' : 'FAIL') + '|' + n + (d ? '|' + d : '')); };
const elStub = () => ({ innerHTML: '', style: {}, textContent: '',
  classList: { add() {}, remove() {} }, value: '', insertAdjacentHTML() {} });
const boxes = {};
const sandbox = { console, JSON, Math, Number, String, Object, Array, Date, Promise,
  isNaN, parseInt, parseFloat, encodeURIComponent, decodeURIComponent, setTimeout,
  URLSearchParams,
  document: { getElementById: id => boxes[id] || elStub(), querySelectorAll: () => [] },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: {}, api: async () => ({}), toast() {}, esc: s => String(s), escMd: s => String(s),
  closePanel() {}, closeModal() {}, showModal() {}, confirmDialog: async () => false,
  currentUserId: () => 1, loadCurrentUser() {}, loginEntryClick() {}, tab() {},
  renderPagerBar() {}, uxMetricsHtml: () => '' };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
try { vm.runInContext(src, sandbox, { filename: '31-admin.js' }); }
catch (e) { console.log('LOADFAIL|' + e.message); process.exit(0); }

(async () => {
  // ── 正常路径 ──
  boxes['ops-capability'] = elStub();
  boxes['ops-alerts'] = elStub();
  let apiMode = 'ok';
  sandbox.api = async (url) => {
    if (apiMode === 'reject') throw new Error('simulated network failure');
    if (url.indexOf('/orphan-runs') >= 0) return P.orphan;
    if (url.indexOf('/monitor/sessions') >= 0) return P.sessions;
    if (url.indexOf('/tenant-readiness') >= 0) return P.tenant;
    return {};
  };
  await sandbox.loadOpsCapability(P.dashboard);
  const cap = boxes['ops-capability'].innerHTML;
  check('四层标题齐备', ['LLM 调用健康', '编排运行', '会话排查', '治理就绪']
    .every(t => cap.indexOf(t) >= 0));
  check('无 undefined 泄漏', cap.indexOf('undefined') < 0);
  check('无 NaN 泄漏', cap.indexOf('NaN') < 0);
  check('无 [object Object] 泄漏', cap.indexOf('[object Object]') < 0);
  if (P.sessions && P.sessions.flagged > 0)
    check('flagged 徽章渲染', cap.indexOf(P.sessions.flagged + ' 异常') >= 0,
          'flagged=' + P.sessions.flagged);
  else
    check('无异常时徽章为正常', cap.indexOf('>正常<') >= 0);
  if (P.orphan && P.orphan.orphan_tasks > 0)
    check('孤儿任务红色高亮', cap.indexOf('孤儿任务（超 TTL）') >= 0
          && cap.indexOf('var(--red)') >= 0);
  // ── 降级路径：三路补充取数全挂，LLM/编排层仍来自 dashboard 主数据 ──
  apiMode = 'reject';
  boxes['ops-capability'] = elStub();
  await sandbox.loadOpsCapability(P.dashboard);
  const cap2 = boxes['ops-capability'].innerHTML;
  check('降级路径四层仍渲染', ['LLM 调用健康', '编排运行', '会话排查', '治理就绪']
    .every(t => cap2.indexOf(t) >= 0));
  check('降级路径无 undefined/NaN', cap2.indexOf('undefined') < 0 && cap2.indexOf('NaN') < 0);
  check('降级路径缺失值显示占位', cap2.indexOf('—') >= 0);
  // ── 告警行：跳会话按钮 ──
  apiMode = 'ok';
  boxes['ops-alerts'] = elStub();
  // 存量告警无会话溯源（不回填纪律），用合成行覆盖「有溯源」正向路径
  const alertsSynth = (P.alerts || []).concat([
    { id: 900001, rule_name: '合成-有溯源', metric: 'success_rate', operator: '<',
      threshold: 100, actual: 50, level: 'warning', status: 'open', run_id: 4242,
      flow_name: 'verify', conversation_id: 9336, created_at: '2026-10-18 12:00:00' },
    { id: 900002, rule_name: '合成-已确认', metric: 'mock_rate', operator: '>',
      threshold: 50, actual: 80, level: 'critical', status: 'acked', run_id: 4243,
      flow_name: 'verify', conversation_id: 9360, created_at: '2026-10-18 11:00:00' }]);
  sandbox.renderOpsAlerts(alertsSynth);
  const ah = boxes['ops-alerts'].innerHTML;
  const withConv = alertsSynth.filter(a => (a.conversation_id || 0) > 0);
  const noConv = alertsSynth.filter(a => !(a.conversation_id || 0) > 0);
  check('跳会话按钮数=有会话溯源的告警数',
        (ah.match(/跳会话/g) || []).length === withConv.length,
        `buttons=${(ah.match(/跳会话/g) || []).length} expect=${withConv.length}`);
  withConv.forEach(a => check('跳会话绑定会话#' + a.conversation_id,
        ah.indexOf('openSessionDrill(' + a.conversation_id + ')') >= 0));
  noConv.forEach(a => check('无溯源告警#' + a.id + '不出跳会话按钮',
        ah.indexOf('openSessionDrill(' + String(a.conversation_id || 0) + ')') < 0));
  const openN = alertsSynth.filter(a => a.status === 'open').length;
  check('ack 确认按钮数不变', (ah.match(/ackAlert\(/g) || []).length === openN);
  check('规则单元格显示会话溯源', ah.indexOf('会话#9336') >= 0);
  console.log(out.join('\n'));
})().catch(e => console.log('HARNESS_ERR|' + e.message));
"""


def main():
    print("== V0 静态挂载检查 ==")
    idx = open(os.path.join(ROOT, "static/index.html"), encoding="utf-8").read()
    admin = open(os.path.join(ROOT, "static/js/mods/31-admin.js"), encoding="utf-8").read()
    check("index.html 含 ops-capability 面板", 'id="ops-capability"' in idx)
    check("31-admin.js loadOps 挂载 loadOpsCapability", "loadOpsCapability(d);" in admin)
    check("31-admin.js 含跳会话按钮", "跳会话" in admin and "openSessionDrill(${a.conversation_id})" in admin)

    print("== V1 后端字段核对（真实库只读）==")
    from database import get_db                                    # noqa: E402
    from routers.monitor import (monitor_dashboard, monitor_sessions,  # noqa: E402
                                 orphan_runs, tenant_readiness)
    from core.llm_health import health_report                      # noqa: E402

    conn = get_db()
    orph = orphan_runs(limit=20, conn=conn)
    check("orphan-runs 返回 ok", bool(orph.get("ok")))
    for k in ("orphan_tasks", "orphan_runs", "oldest_age_s", "ttl_s"):
        check(f"orphan-runs 含 {k}", k in orph, f"={orph.get(k)}")
    check("orphan-runs active 为列表", isinstance(orph.get("active"), list),
          f"{len(orph.get('active') or [])} 个非终态批次")

    sess = monitor_sessions(days=7, limit=1, q="", conn=conn)
    check("sessions 返回 ok", bool(sess.get("ok")))
    for k in ("total", "flagged", "days"):
        check(f"sessions 含 {k}", k in sess, f"={sess.get(k)}")
    print(f"  近7天会话 {sess.get('total')} 个 / 异常 {sess.get('flagged')} 个")

    ten = tenant_readiness(conn=conn)
    check("tenant-readiness 返回 ok 字段", "ok" in ten and "mode" in ten,
          f"mode={ten.get('mode')} blockers={len(ten.get('blockers') or [])}")
    check("tenant-readiness 含 entities.orphan_project",
          isinstance((ten.get("entities") or {}).get("orphan_project"), int),
          f"={((ten.get('entities') or {}).get('orphan_project'))}")

    dash = monitor_dashboard(days=7, conn=conn)
    lh = dash.get("llm_health") or {}
    check("dashboard 透传 llm_health.status", bool(lh.get("status")), f"={lh.get('status')}")
    s = lh.get("summary") or {}
    for k in ("calls", "real_calls", "mock_calls", "truncated", "reasoning_eaten",
              "reasoning_share", "cache_hit_rate", "finish_coverage"):
        check(f"llm_health.summary 含 {k}", k in s, f"={s.get(k)}")
    c = lh.get("contract") or {}
    check("llm_health.contract 含 ok/violations", "ok" in c and "violations" in c,
          f"ok={c.get('ok')} violations={len(c.get('violations') or [])}")
    check("dashboard 含 runs/open_alerts", "runs" in dash and "open_alerts" in dash,
          f"runs={dash.get('runs')} open_alerts={dash.get('open_alerts')}")
    print(f"  LLM 健康总状态={lh.get('status')} 调用={s.get('calls')} "
          f"（真实 {s.get('real_calls')} / Mock {s.get('mock_calls')}） 开放告警={dash.get('open_alerts')}")

    # 与 llm_health 直调结果一致性（dashboard 只透传不重算）
    direct = health_report(conn, days=7)
    check("dashboard.llm_health 与 health_report 直调一致",
          lh.get("status") == direct.get("status")
          and (lh.get("summary") or {}).get("calls") == (direct.get("summary") or {}).get("calls"))
    conn.close()

    print("== V2 前端渲染冒烟（Node vm + 真实 payload）==")
    os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
    pf = os.path.join(ROOT, "tmp", "verify_capability_payloads.json")
    with open(pf, "w", encoding="utf-8") as f:
        json.dump({"dashboard": dash, "orphan": orph, "sessions": sess,
                   "tenant": ten, "alerts": dash.get("recent_alerts") or []},
                  f, ensure_ascii=False, default=str)
    hf = os.path.join(ROOT, "tmp", "verify_capability_harness.js")
    with open(hf, "w", encoding="utf-8") as f:
        f.write(HARNESS)
    r = subprocess.run([NODE, hf, ROOT, pf], capture_output=True, text=True, timeout=120)
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    if r.returncode != 0 or not lines:
        check("harness 执行", False, (r.stderr or r.stdout or "no output")[:300])
    else:
        for l in lines:
            parts = l.split("|")
            check(parts[1], parts[0] == "PASS", parts[2] if len(parts) > 2 else "")

    print(f"\n结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项:", "; ".join(FAIL))
        sys.exit(1)
    print("全部通过 —— 平台能力总览四层取数真实、渲染无泄漏、降级路径健壮、告警可跳会话。")


if __name__ == "__main__":
    main()
