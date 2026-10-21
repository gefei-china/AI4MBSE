# -*- coding: utf-8 -*-
"""P1 replanning 前端渲染验证（一次性，非门禁；2026-10-21）。

**为什么需要**：门禁 verify_replan_p1.py 的 S5 只做**静态**断言（kindMeta 里有
    replan 标签、行渲染分支存在）。但"喂一个含 replan 节点的 trace JSON 进
    `orchTraceHtml` 真函数体，输出里真的出现 🔁 重规划 / T1/retry / reason"——
    这要**执行真代码**才能证明（本仓 verify_doc_trace_ui.py 同款手法：
    抽函数体 → Node 执行，测的是装配不是复刻）。

测试矩阵：
    F1  by_kind 聚合行出现「🔁 重规划」
    F2  replan 行出现 level/action（T1/retry、T0/none）
    F3  reason 展示（截断 + title 全文）
    F4  普通 span（task）渲染不回归（重试徽章/tokens 照旧）
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT_JS = os.path.join(ROOT, "static", "js", "mods", "03-chat.js")

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def extract_js_fn(src, name):
    """抽出 `function name(...) { ... }` 完整源码（花括号配平，处理字符串）。
    与 verify_doc_trace_ui.py / verify_job_ui.py 同一套手法。"""
    m = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), src)
    if not m:
        return None
    i = src.index("(", m.start())
    depth = 0
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    i = src.index("{", i)
    depth = 0
    quote = ""
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == quote and src[i - 1] != "\\":
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return src[m.start():i]


with open(CHAT_JS, encoding="utf-8") as f:
    chat_src = f.read()

fn_src = extract_js_fn(chat_src, "orchTraceHtml")
if not fn_src:
    print("FATAL: 抽不出 orchTraceHtml")
    sys.exit(1)

# ── 合成 trace 数据（结构与 GET /runs/{id}/trace 的 run_trace 输出一致）────
TRACE = {
    "run_id": 999, "span_count": 5,
    "spans": [{
        "id": 2, "kind": "run", "name": "run #999", "parent_id": 0, "status": "ok",
        "latency_ms": 300000, "meta": {}, "children": [
            {"id": 3, "kind": "plan", "name": "任务规划", "parent_id": 2, "status": "ok",
             "latency_ms": 4000, "meta": {"source": "planner", "tasks": 3}, "children": []},
            {"id": 4, "kind": "task", "name": "t2", "parent_id": 2, "status": "ok",
             "latency_ms": 120000, "meta": {"tokens": 1234, "retry_count": 2},
             "children": []},
            {"id": 5, "kind": "replan", "name": "t1-decide:t2", "parent_id": 2,
             "status": "ok", "latency_ms": 12,
             "meta": {"task": "t2", "signals": ["S3:t2:timeout"], "level": "T1",
                      "action": "retry", "reason": "S3 可重试失败（timeout）⇒ 同 key 补跑一次",
                      "round": 0, "tokens": 0, "latency_ms": 12},
             "children": []},
            {"id": 6, "kind": "replan", "name": "stall-decide", "parent_id": 2,
             "status": "ok", "latency_ms": 8,
             "meta": {"pending": 1, "signals": [], "level": "T0", "action": "none",
                      "reason": "无重规划信号（S5/S6/S7 均未命中）⇒ 维持现状，partial 如实汇总——"
                                "这是一条超过五十个字符的长理由用来验证截断与title全文行为",
                      "round": 0, "tokens": 0, "latency_ms": 8},
             "children": []},
        ],
    }],
    "stats": {"total_ms": 304000,
              "by_kind": {"run": {"count": 1, "ms": 300000, "tokens": 0},
                          "plan": {"count": 1, "ms": 4000, "tokens": 0},
                          "task": {"count": 1, "ms": 120000, "tokens": 1234},
                          "replan": {"count": 2, "ms": 20, "tokens": 0}},
              "tasks": [], "note": ""},
}

# Node 代码：esc stub（03-chat.js 里 esc 是全局，来自更早加载的模块）+
# 抽出的真函数体 + 数据 + 断言。console.log 出 JSON 供 Python 判读。
_ASSERT_JS = r"""
const _fail = [];
function _has(label, cond){ if(!cond) _fail.push(label); }
const html = orchTraceHtml(%s);
_has('F1 by_kind 聚合行含「🔁 重规划」', html.includes('🔁 重规划'));
_has('F2a replan 行含 T1/retry', html.includes('<b>T1/retry</b>'));
_has('F2b replan 行含 T0/none', html.includes('<b>T0/none</b>'));
_has('F3a 短 reason 完整展示', html.includes('S3 可重试失败（timeout）⇒ 同 key 补跑一次'));
_has('F3b 长 reason 截断展示 + title 全文', html.includes('…') && html.includes('这是一条超过五十个字符的长理由用来验证截断与title全文行为'));
_has('F3c reason 在 title 属性里（全文可悬浮看）', /title="[^"]*长理由[^"]*"/.test(html));
_has('F4a 普通 task 行不回归（tokens 徽章）', html.includes('1,234 tok'));
_has('F4b 普通 task 行不回归（重试徽章）', html.includes('重试 2'));
_has('F4c plan 行不回归（来源徽章）', html.includes('来源:planner'));
_has('F4d 无 replan 时空态不炸（回归保护）', (()=>{ try { orchTraceHtml({spans:[],stats:{note:'x'}}); return true; } catch(e){ return false; } })());
_has('F5 replan 行挂在 run 下（缩进 depth>0 ⇒ 有 padding-left）', /padding-left:14px/.test(html));
console.log(JSON.stringify({fail: _fail, len: html.length}));
"""

_esc_stub = """
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
"""

node_bin = os.environ.get("NODE_BIN", "node")
code = _esc_stub + fn_src + "\n" + (_ASSERT_JS % json.dumps(TRACE, ensure_ascii=False))
proc = subprocess.run([node_bin, "-e", code], capture_output=True, text=True,
                      cwd=ROOT, timeout=60)
if proc.returncode != 0:
    print("FATAL node 失败:", (proc.stderr or "")[-400:])
    sys.exit(1)
res = json.loads(proc.stdout.strip().splitlines()[-1])
for label in res.get("fail", []):
    _rec(label, False, "HTML 缺预期内容")
if not res.get("fail"):
    _rec("F1~F5 orchTraceHtml 真函数体渲染 replan 节点全部符合预期", True,
         "html %d 字节" % res.get("len", 0))

fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
sys.exit(0 if not fails else 1)
