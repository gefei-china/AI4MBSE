# -*- coding: utf-8 -*-
"""P4 replanning 前端渲染验证探针（2026-10-21）。

**为什么需要**：门禁 verify_replan_p4.py 钉的是「接线存在」（源码里有没有那个
    分支），但**渲染出来的 HTML 对不对**只有真跑函数体能证明 —— 与 P1/P2/P3
    同款手法：抽 static/js/mods/11-pipeline.js 的真函数体在 Node 里执行。

本探针盯三处 P4 新增渲染：
    ① 计划修订卡的「跳过 skipped」「插入 inserted」两节清单 + early_stop
       人话徽章（内部级别号 T0 不许直接暴露给用户）；
    ② 子任务卡的 `skipped` 状态（灰徽章 + 「主动放弃」文案）——
       **绝不许染成失败红**：主动放弃是"没必要做"，不是"没做成"，
       这是 early_stop 的设计核心，颜色错了用户整个误读；
    ③ T3 路径的 `added` 字段兼容（不接的话 LLM 重画的「新增」清单
      在前端是空的）。

**为什么"颜色语义"要单独断言**：灰/红/绿在前端不只是审美 —— 它直接承载
    "这次变更是建设性的还是破坏性的"这个判断。把 skip 画成红色，用户会
    以为系统出错了；把 insert 画成灰色，用户会以为新任务可有可无。
   门禁只能断言"有这个分支"，断言不了"染对了色"，所以必须由探针兜住。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\_probe_replan_p4_frontend.py
退出码：全绿 0 / 有失败 1。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "js", "mods", "11-pipeline.js")

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}"
          + (f"  {detail}" if not ok and detail else ""))
    return ok


# ── 1. 语法检查（node --check 真解析）────────────────────────────────────
print("=== 1. JS 语法 ===")
r = subprocess.run(["node", "--check", JS], capture_output=True, text=True)
if not _rec("F1 node --check 通过（11-pipeline.js 语法合法）",
            r.returncode == 0, r.stderr[:200]):
    print("结果：0 PASS / %d FAIL" % len(_results))
    sys.exit(1)

# ── 2. 修订卡渲染（抽 procAddPlanRevised 真函数体）───────────────────────
print("\n=== 2. 计划修订卡：P4 两节清单 + early_stop 徽章 ===")
JS_HARNESS = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const m=src.match(/function procAddPlanRevised\(ev\)\{[\s\S]*?\n\}/);
if(!m){ console.log(JSON.stringify({error:'extract_failed'})); process.exit(1); }
const ups=[];
global.esc=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.procUpsert=(id,patch)=>{ ups.push({id,patch}); };
eval(m[0].replace('function procAddPlanRevised',
                  'globalThis.procAddPlanRevised = function'));

// ① P4 三动作修订卡（replace + insert + skip 全带）
globalThis.procAddPlanRevised({
  type:'plan_revised', run_id:11, level:'T3', action:'replan',
  reason:'T3 LLM 整图重画：替代 1 个失效任务（保留 1 个，跳过 1 个，过 Q1~Q5）',
  updates:['t2'], added:['t2_r1','t9'], inserted:['t9'], skipped:['t3'],
  dropped:[], signals:[]
});
// ② early_stop 修订卡（level=T0 是内部号，须映射成人话）
globalThis.procAddPlanRevised({
  type:'plan_revised', run_id:11, level:'T0', action:'early_stop',
  reason:'required 产出已全部达成（t1,t2）⇒ 提前收敛，主动放弃 2 个非必需任务（t3,t4）',
  skipped:['t3','t4'], signals:[]
});
// ③ 老服务端兼容（无 P4 新字段，不许崩）
globalThis.procAddPlanRevised({type:'plan_revised', run_id:12, level:'T2-light',
  reason:'局部重派', updates:['t2']});

const d1=ups[0].patch.detail, d2=ups[1].patch.detail, d3=ups[2].patch.detail;
const out={
  count: ups.length,
  title1: ups[0].patch.title,
  title2: ups[1].patch.title,
  id1: ups[0].id, id2: ups[1].id,
  // ① 三动作卡（多 key 用「、」拼接进同一个 span，不断言 >k< 包裹）
  hasSkip1: d1.includes('跳过') && d1.includes('>t3<'),
  hasInsert1: d1.includes('插入') && d1.includes('>t9<'),
  hasAdded1: d1.includes('新增') && d1.includes('t2_r1、t9'),
  hasDropped1: d1.includes('移除'),
  t3Label: ups[0].patch.title.includes('LLM 整图重画'),
  // ② early_stop 卡
  esBadge: ups[1].patch.title,
  esSkip: d2.includes('跳过') && d2.includes('t3、t4'),
  esReason: d2.includes('提前收敛'),
  // ③ 兼容
  noCrash3: d3.length>0 && d3.includes('局部重派'),
  hasUpd3: d3.includes('重派') && d3.includes('>t2<'),
  noSkipSec3: !d3.includes('跳过'),
};
console.log(JSON.stringify(out));
"""
js_path = os.path.join(ROOT, "tools", "_p4_frontend_harness.js")
with open(js_path, "w", encoding="utf-8") as f:
    f.write(JS_HARNESS)
try:
    r2 = subprocess.run(["node", js_path, JS], capture_output=True, text=True)
finally:
    if os.path.exists(js_path):
        os.remove(js_path)

if r2.returncode != 0:
    _rec("F2 抽真函数体执行成功", False, r2.stderr[:300])
else:
    d = {}
    try:
        d = json.loads(r2.stdout.strip().splitlines()[-1])
    except Exception:
        d = {"error": "parse_failed", "raw": r2.stdout[:200]}
    _rec("F2 抽真函数体执行成功（procAddPlanRevised 可独立渲染）",
         "error" not in d, str(d)[:200])
    _rec("F3 生成三张卡（三动作 + early_stop + 老服务端兼容）",
         d.get("count") == 3, str(d.get("count")))
    _rec("F4 ★ 三动作卡渲染「跳过」清单（key 进详情）",
         d.get("hasSkip1") is True)
    _rec("F5 ★ 三动作卡渲染「插入」清单（与新增分列，不混为一节）",
         d.get("hasInsert1") is True)
    _rec("F6 T3 路径的 added 字段被接住（LLM 重画「新增」不空）",
         d.get("hasAdded1") is True)
    _rec("F7 ★ early_stop 用人话徽章（内部级别号 T0 不暴露给用户）",
         "提前收敛" in str(d.get("esBadge"))
         and "T0" not in str(d.get("esBadge")),
         str(d.get("esBadge")))
    _rec("F8 ★ early_stop 卡的 skipped 清单进详情（用户看到放弃了谁）",
         d.get("esSkip") is True and d.get("esReason") is True)
    _rec("F9 不同修订卡用不同 id（早停卡与三动作卡不互相覆盖）",
         d.get("id1") != d.get("id2"),
         "%s vs %s" % (d.get("id1"), d.get("id2")))
    _rec("F10 老服务端兼容：无 P4 字段不崩、无「跳过」空节",
         d.get("noCrash3") is True and d.get("hasUpd3") is True
         and d.get("noSkipSec3") is True)

# ── 3. 子任务卡 skipped 渲染（抽 subtaskCardHtml 真函数体）────────────────
print("\n=== 3. 子任务卡：skipped 状态（灰，不是失败红）===")
JS_HARNESS2 = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const m=src.match(/function subtaskCardHtml\(s\)\{[\s\S]*?\r?\n  return `[\s\S]*?`;\r?\n\}/);
if(!m){ console.log(JSON.stringify({error:'extract_failed'})); process.exit(1); }
global.esc=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.capTagsFor=()=>'';
global._subChildrenHtml=()=>'';
global.ioFoldToggle=()=>{};
eval(m[0].replace('function subtaskCardHtml',
                  'globalThis.subtaskCardHtml = function'));

const hSkip=globalThis.subtaskCardHtml({
  key:'t3', title:'方案评审', agent:'review', status:'skipped',
  deps:[], latency_ms:0, error:'目标已达成提前收敛（主动放弃）',
  summary:'', ui_summary:'', output:'', retry_count:0, summary_status:''});
const hFail=globalThis.subtaskCardHtml({
  key:'t2', title:'系统设计', agent:'design', status:'failed',
  deps:[], latency_ms:0, error:'timeout', summary:'', ui_summary:'',
  output:'', retry_count:0, summary_status:''});
const hDone=globalThis.subtaskCardHtml({
  key:'t1', title:'需求分析', agent:'requirement_analysis', status:'done',
  deps:[], latency_ms:0, error:'', summary:'', ui_summary:'',
  output:'', retry_count:0, summary_status:''});
const out={
  skipBadge: hSkip.includes('<span class="st g">skipped</span>'),
  skipText: hSkip.includes('主动放弃'),
  skipSym: hSkip.includes('⊘'),
  skipNotRed: !hSkip.includes('st r') && !hSkip.includes('class="st r"'),
  failStillRed: hFail.includes('<span class="st r">failed</span>'),
  doneStillGreen: hDone.includes('<span class="st ok">done</span>'),
  doneNoSkip: !hDone.includes('主动放弃'),
  skipNoSpin: !hSkip.includes('proc-spin'),
};
console.log(JSON.stringify(out));
"""
js_path2 = os.path.join(ROOT, "tools", "_p4_frontend_harness2.js")
with open(js_path2, "w", encoding="utf-8") as f:
    f.write(JS_HARNESS2)
try:
    r3 = subprocess.run(["node", js_path2, JS], capture_output=True, text=True)
finally:
    if os.path.exists(js_path2):
        os.remove(js_path2)

if r3.returncode != 0:
    _rec("F11 抽真函数体执行成功", False, r3.stderr[:300])
else:
    d2 = {}
    try:
        d2 = json.loads(r3.stdout.strip().splitlines()[-1])
    except Exception:
        d2 = {"error": "parse_failed", "raw": r3.stdout[:200]}
    _rec("F11 抽真函数体执行成功（subtaskCardHtml 可独立渲染）",
         "error" not in d2, str(d2)[:200])
    _rec("F12 ★ skipped 徽章为灰 `.st g`（主动放弃 ≠ 失败）",
         d2.get("skipBadge") is True)
    _rec("F13 ★ skipped 文案「主动放弃」+ 符号 ⊘", d2.get("skipText") is True
         and d2.get("skipSym") is True)
    _rec("F14 ★★ skipped 卡绝不染红（红=失败，会把「没必要做」误读成"
         "「没做成」—— early_stop 的设计核心）",
         d2.get("skipNotRed") is True)
    _rec("F15 failed 仍是红（回归：没被 skipped 改动带坏）",
         d2.get("failStillRed") is True)
    _rec("F16 done 仍是绿且不显示「主动放弃」（回归）",
         d2.get("doneStillGreen") is True and d2.get("doneNoSkip") is True)
    _rec("F17 skipped 不显示执行中 spinner（它不会被执行）",
         d2.get("skipNoSpin") is True)

# ── 4. 计划清单 / DAG 的 skipped 处理（源码级断言）────────────────────────
print("\n=== 4. 计划清单与 DAG 节点 ===")
with open(JS, encoding="utf-8") as f:
    src = f.read()
_rec("F18 计划清单认 skipped（文案「主动放弃」+ 灰徽章 st g）",
     "n.status==='skipped'" in src and "skipSt ? '主动放弃'" in src
     and "(skipSt ? 'g' : 'ok')" in src)
_rec("F19 计划卡汇总单独计「主动放弃」数（不混进完成/失败）",
     "const skipN = all.filter(n=>n.status==='skipped').length;" in src
     and "${skipN} 主动放弃" in src)
_rec("F20 ★ DAG 节点有 d-skip 灰虚线样式（与 partial 的 d-warn 区分开）",
     "n.status==='skipped'?'d-skip'" in src and ".d-skip" in src
     and "line-style':'dashed'" in src)
_rec("F21 终止翻转的清单也认 skipped（收口路径不漏渲染）",
     "const skipN = all.filter(n=>n && n.status==='skipped').length;" in src)

fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for _, n, d in fails:
    print("  FAIL  %s  %s" % (n, d))
sys.exit(0 if not fails else 1)
