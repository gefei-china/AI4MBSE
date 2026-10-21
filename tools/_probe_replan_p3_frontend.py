# -*- coding: utf-8 -*-
"""P3 replanning 前端渲染验证探针（2026-10-21）。

**为什么需要**：P3 新增「重画确认卡」（static/js/mods/11-pipeline.js::
    procAddRedrawConfirm）与 T3 级别标签。门禁钉的是"接线存在"，但
    **渲染出来的 HTML 对不对**（三动作按钮/超时语义文案/diff 清单/常驻展开）
    只有真跑函数体能证明 —— 与 P1/P2 同款手法：抽真函数体在 Node 里执行。

**为什么"超时文案"要单独断言**（本探针的重点）：前端**绝不能**硬编码
    "超时自动批准"。真实语义是 v1 R10 的"超时维持原计划" —— 若 UI 写成后者，
    用户会以为不点也会被重画，从而对整个计划失去控制感，而且这个误解会
    直接导致他不敢用确认卡。门禁只能断言"文案来自后端字段"，断言不了
    "渲染出来的字对不对"，所以这一项必须由探针兜住。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\_probe_replan_p3_frontend.py
退出码：全绿 0 / 有失败 1。
"""
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
    print("结果：%d PASS / %d FAIL" % (0, len(_results)))
    sys.exit(1)

# ── 2. 抽真函数体在 Node 执行 ────────────────────────────────────────────
print("\n=== 2. 重画确认卡渲染（抽真函数体）===")
JS_HARNESS = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const m=src.match(/function procAddRedrawConfirm\(ev\)\{[\s\S]*?\n\}/);
if(!m){ console.log(JSON.stringify({error:'extract_failed'})); process.exit(1); }
const ups=[];
global.esc=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
/* ★ 必须桩出 `_procS.timeline`：函数体开头用它做"同一确认卡不重复建卡"判据，
     缺了会直接抛 TypeError —— 这正是抽真函数体比"照抄一份重写"更有价值的地方：
     照抄的那份很容易把去重判据一并抄掉，而这里抄不掉。 */
global._procS={timeline:[]};
/* procUpsert 的桩必须**同步写回 timeline** —— 真实现是这么做的（时间线去重
   依赖它）。桩若只收集不写回，第二次调用会被函数体的"同一卡不重复建"判据
   直接跳过，测出来的 "count=1" 会被误读成"去重有 bug"。 */
global.procUpsert=(id,patch)=>{
  ups.push({id,patch});
  global._procS.timeline.push({id, patch});
};
global.onclick=null;
eval(m[0].replace('function procAddRedrawConfirm',
                  'globalThis.procAddRedrawConfirm = function'));

// ① 标准场景：批准重画（含 diff 三要素 + TTL）
globalThis.procAddRedrawConfirm({
  type:'redraw_confirm', confirm_id:'rr7-1', run_id:7,
  level:'T2-heavy', action_hint:'replan',
  reason:'T2-heavy 差量重画 1 个任务（换实现路径）',
  added:['t2_r1'], dropped:['t2'], retask:[], remap:{t2:'t2_r1'},
  round:1, ttl_sec:60, timeout_behavior:'keep_original',
  actions:[{id:'approve',label:'批准重画',primary:true},
           {id:'keep',label:'维持原计划',primary:false},
           {id:'supplement',label:'我来补充指令',primary:false}]
});
// ② 缺字段兜底（老服务端可能只发 confirm_id/reason）
globalThis.procAddRedrawConfirm({type:'redraw_confirm', confirm_id:'rr8-1', reason:'兜底场景'});

const d1=ups[0].patch.detail, d2=ups[1].patch.detail;
const out={
  count: ups.length,
  title1: ups[0].patch.title,
  status1: ups[0].patch.status,
  statusText1: ups[0].patch.statusText,
  collapsed1: ups[0].patch.collapsed,
  id1: ups[0].id,
  hasReason1: d1.includes('换实现路径'),
  hasAdded: d1.includes('>t2_r1<'),
  hasDropped: d1.includes('>t2<'),
  btnApprove: d1.includes('批准重画'),
  btnKeep: d1.includes('维持原计划'),
  btnSupplement: d1.includes('我来补充指令'),
  hasConfirmId: d1.includes('rr7-1'),
  hasTtl: d1.includes('60'),
  timeoutKeep: d1.includes('维持原计划') && d1.includes('不会自动重画'),
  noCrash2: d2.length>0,
  idUnique: (ups[0].id!==ups[1].id)
};
console.log(JSON.stringify(out));
"""
js_path = os.path.join(ROOT, "tools", "_p3_frontend_harness.js")
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
    import json as _j
    try:
        d = _j.loads(r2.stdout.strip().splitlines()[-1])
    except Exception:
        d = {"error": "parse_failed", "raw": r2.stdout[:200]}
    _rec("F2 抽真函数体执行成功（procAddRedrawConfirm 可独立渲染）",
         "error" not in d, str(d)[:200])
    _rec("F3 生成两张卡（标准 + 缺字段兜底）",
         d.get("count") == 2, str(d.get("count")))
    _rec("F4 标题点明「待你确认」且状态为 run（常驻展开等用户）",
         "待你确认" in str(d.get("title1"))
         and d.get("status1") == "run"
         and "待确认" in str(d.get("statusText1")),
         "%s / %s / %s" % (d.get("title1"), d.get("status1"), d.get("statusText1")))
    _rec("F5 ★ 确认卡默认展开（不是折叠 —— 折叠等于藏起否决权）",
         d.get("collapsed1") is False, str(d.get("collapsed1")))
    _rec("F6 原因全文进卡", d.get("hasReason1") is True)
    _rec("F7 diff 三要素渲染（新增 t2_r1 / 移除 t2）",
         d.get("hasAdded") is True and d.get("hasDropped") is True)
    _rec("F8 三动作按钮齐全（批准重画 / 维持原计划 / 我来补充指令）",
         d.get("btnApprove") is True and d.get("btnKeep") is True
         and d.get("btnSupplement") is True)
    _rec("F9 按钮带 confirm_id（应答能定位到具体那张卡）",
         d.get("hasConfirmId") is True)
    _rec("F10 ★ 超时文案来自后端字段且语义正确"
         "（显示 TTL + 「维持原计划」+ 明说「不会自动重画」）",
         d.get("hasTtl") is True and d.get("timeoutKeep") is True)
    _rec("F11 缺字段不崩（老服务端兼容）", d.get("noCrash2") is True)
    _rec("F12 不同确认卡用不同 id（不互相覆盖）", d.get("idUnique") is True)

# ── 3. T3 级别标签 ───────────────────────────────────────────────────────
print("\n=== 3. T3 级别标签与事件接线 ===")
with open(JS, encoding="utf-8") as f:
    src = f.read()
_rec("F13 SSE 分支识别 redraw_confirm 并调用渲染函数",
     "evType === 'redraw_confirm'" in src and "procAddRedrawConfirm(ev)" in src)
_rec("F14 ★ 计划修订卡补上 T3 级别标签（LLM 整图重画可辨识）",
     "'T3':'LLM 整图重画" in src)
_rec("F15 应答失败时落终态并提示（不静默、不卡流程）",
     "procMarkRedrawConfirmed" in src and "应答失败" in src)

# ── 4. 后端端点存在性（前端按钮要有落点）────────────────────────────────
print("\n=== 4. REST 端点 ===")
rt = os.path.join(ROOT, "routers", "conversations.py")
with open(rt, encoding="utf-8") as f:
    rsrc = f.read()
_rec("F16 redraw-confirm 端点已注册",
     '/redraw-confirm' in rsrc and "def redraw_confirm(" in rsrc)
_rec("F17 ★ 端点做归属校验（越权代批他人重画 = 越权写操作）",
     rsrc[rsrc.index("def redraw_confirm("):][:600].count("_own_conv") == 1)

fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for _, n, d in fails:
    print("  FAIL  %s  %s" % (n, d))
sys.exit(0 if not fails else 1)