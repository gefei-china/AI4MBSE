# -*- coding: utf-8 -*-
"""#10 门禁：系统进度提示拆为独立 SSE 事件类型 `progress`（2026-10-18）。

**背景**：B2-10（2026-10-13）用 `kind: "progress"` 在 reasoning 事件**内部**区分
「模型 CoT」与「系统进度提示」，前端按 kind 分流。但那只是**一个消费点**的修补：
前端分派、`orch_stream_events` 录制、监控检索、任何外部消费方全都按 `type` 走 ——
混在 reasoning 里，意味着除前端外的每一方都把系统提示当成"AI 思考"。
**事件名即语义**才是终局：`type: "progress"` + 保留 `kind`（兼容层）。

**本门禁守八件事**：
  S1 progress 站点非空，且每处 `type=progress` 且带 `kind=progress`（兼容层不拆）；
  S2 reasoning 站点**零** kind=progress 残留（拆型彻底；与 verify_reasoning_kind.py
     的 B1 互证 —— 两个门禁从两端夹住同一个不变式）；
  S3 分类正确：所有带系统提示指纹（（自动编排）/（断点续跑）/（知识库范围）/
     （harness 持续工作），含 delta 为变量时回溯赋值行）的事件都必须是 progress，
     且每条 progress 的文案都必须带指纹（新提示种类要在此登记，同 llm_health 的
     EXPLICIT_INTENT_LIMITS 纪律）；
  S4 两处子事件转发点（降级直行 / 编排子任务透传）都认 progress —— 漏一处 =
     该路径下系统提示**静默丢失**（比混流更隐蔽：用户 simply 看不到）；
  S5 前端新增 `evType === 'progress'` 分支走 procAddSystemNotice，且 reasoning+kind
     兼容分支仍在（老服务端灰度期不能丢事件）；
  S6 `to_sse_frame` 把 `type=progress` 归一为 `event: progress`（事件名真的换了）；
  S7 E2E：vm 加载**真实** 11-pipeline.js，驱动四类事件断言路由
     （progress→notice / reasoning→thinking / reasoning+kind→notice / token 不受影响）；
  S8 变异自证：漏拆（progress 改回 reasoning）S1/S2 必须 FAIL；前端删分支 S5 必须 FAIL。
"""
import ast
import io
import os
import re
import subprocess
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def read(rel):
    p = os.path.join(ROOT, rel)
    return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""


# 系统进度提示的文案指纹（与 stream.py 实际前缀一致；**新提示种类必须在此登记**）
SYS_PREFIXES = ("（自动编排）", "（断点续跑）", "（知识库范围）", "（harness 持续工作）")
_SYS_RE = re.compile("|".join(re.escape(p) for p in SYS_PREFIXES))


def _enum_sites(src):
    """AST 枚举全部 Yield-dict 事件站点 → [(lineno, type, kind, delta字面量)]。

    不用 grep 猜：多行拼接 / 相邻站点串味 / delta 是变量三类陷阱都靠 AST + 回溯躲开。
    """
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Yield) or not isinstance(node.value, ast.Dict):
            continue
        kv = {}
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                kv[k.value] = v
        tv, kk = kv.get("type"), kv.get("kind")
        if not (isinstance(tv, ast.Constant) and isinstance(tv.value, str)):
            continue
        kind_val = kk.value if isinstance(kk, ast.Constant) else None
        delta_lit = ""
        if "delta" in kv:
            delta_lit = "".join(s.value for s in ast.walk(kv["delta"])
                                if isinstance(s, ast.Constant) and isinstance(s.value, str))
        out.append((node.lineno, tv.value, kind_val, delta_lit))
    return out


def _resolve_text(src_lines, ln, txt):
    """delta 是变量时回溯其赋值行（判据宽松：赋值行出现指纹即算数）。

    2026-10-13/14 两次踩坑的沉淀：① 只取字面量 ⇒ 漏；② 源码切片窗口太宽 ⇒ 串味；
    ③ delta 是变量 ⇒ 空串。正确做法 = AST 取本节点字面量 + 变量回溯赋值行。
    """
    if _SYS_RE.search(txt[:200]):
        return txt, "inline"
    _src_line = src_lines[ln - 1] if 0 < ln <= len(src_lines) else ""
    m = re.search(r'"delta":\s*([A-Za-z_]\w*)\s*[,}]', _src_line)
    if not m:
        return txt, "unknown"
    var = m.group(1)
    for k in range(ln - 2, max(0, ln - 16), -1):
        if re.search(r"\b%s\s*=" % re.escape(var), src_lines[k]) \
                and _SYS_RE.search(src_lines[k]):
            return src_lines[k], "via:" + var
    return txt, "no-assign-with-prefix:" + var


def main():
    s_src = read("agent/pipeline_parts/stream.py")
    j_src = read("static/js/mods/11-pipeline.js")
    s_lines = s_src.split("\n")

    # ══════════════ S0. 两侧可解析（语法层前置，防后面全在分析碎片）══════════════
    print("\n=== S0. 源码可解析 ===")
    try:
        sites = _enum_sites(s_src)
        check("S0a stream.py AST 可解析", len(sites) > 0, "枚举到 %d 个事件站点" % len(sites))
    except SyntaxError as e:
        check("S0a stream.py AST 可解析", False, str(e))
        return _summary()
    node = None
    for _c in (r"C:\Users\gefei\.workbuddy\binaries\node\versions\22.22.2-6\node.exe", "node"):
        try:
            subprocess.run([_c, "--version"], capture_output=True, timeout=30)
            node = _c
            break
        except Exception:
            continue
    if node:
        _r = subprocess.run([node, "--check", os.path.join(ROOT, "static/js/mods/11-pipeline.js")],
                            capture_output=True, text=True, timeout=60)
        check("S0b 11-pipeline.js node --check 通过", _r.returncode == 0, _r.stderr[:200])
    else:
        check("S0b 找到 node 运行时", False, "未找到 node，跳过 JS 语法检查")

    prog = [(ln, k, t) for ln, tp, k, t in sites if tp == "progress"]
    reas = [(ln, k, t) for ln, tp, k, t in sites if tp == "reasoning"]
    print("  progress 站点 %d 处：%s" % (len(prog), [ln for ln, _, _ in prog]))
    print("  reasoning 站点 %d 处（纯 CoT）" % len(reas))

    # ══════════════ S1. progress 站点非空且带兼容 kind ══════════════
    print("\n=== S1. progress 站点结构（type + 兼容 kind）===")
    check("S1a ★ progress 站点非空（防空集合恒真）", len(prog) >= 1,
          "0 处意味着拆型没发生或被整体回退")
    bad_kind = [ln for ln, k, _ in prog if k != "progress"]
    check("S1b ★ 每处 progress 都带 kind=progress（兼容层不拆：老消费方按 kind 分流）",
          not bad_kind, "缺 kind 的行号：%s" % bad_kind)

    # ══════════════ S2. reasoning 零 kind 残留 ══════════════
    print("\n=== S2. reasoning 无 kind=progress 残留（拆型彻底）===")
    resid = [ln for ln, k, _ in reas if k == "progress"]
    check("S2 ★ reasoning 站点零 kind=progress 残留", not resid,
          "残留行号：%s —— 应迁往 type=progress" % resid)

    # ══════════════ S3. 分类正确（指纹归属，双向）══════════════
    print("\n=== S3. 分类正确：系统提示指纹 ↔ progress 事件 ===")
    # 正向：任何带指纹的事件（不论 type）必须是 progress —— 漏拆的藏不住
    mis = []
    for ln, tp, k, t in sites:
        _txt, _how = _resolve_text(s_lines, ln, t)
        if _SYS_RE.search(_txt[:200]) and tp != "progress":
            mis.append((ln, tp, _how))
    check("S3a ★ 带系统提示指纹的事件全部是 progress 类型", not mis,
          "漏拆：%s" % mis)
    # 反向：每条 progress 的文案必须带指纹（防"把普通 CoT 误拆成 progress"）
    unfp = []
    for ln, k, t in prog:
        _txt, _how = _resolve_text(s_lines, ln, t)
        if not _SYS_RE.search(_txt[:200]):
            unfp.append((ln, _how))
    check("S3b ★ 每条 progress 文案都带系统提示指纹（防误拆 CoT）", not unfp,
          "无指纹的 progress 行号：%s —— 若是新提示种类，请在 SYS_PREFIXES 登记" % unfp)

    # ══════════════ S4. 两处转发点透传 progress ══════════════
    print("\n=== S4. 子事件转发点认 progress（漏一处 = 静默丢失）===")
    fwd = [i + 1 for i, l in enumerate(s_lines) if '_et in ("reasoning", "progress")' in l]
    check("S4a 转发点共两处（降级直行 + 编排子任务）", len(fwd) == 2,
          "实测 %d 处：%s" % (len(fwd), fwd))
    for ln in fwd:
        win = "\n".join(s_lines[max(0, ln - 30):ln + 5])
        check("S4b L%d 转发点在 execute_stream 子流上下文里" % ln,
              "execute_stream(" in win,
              "窗口内未见 execute_stream —— 可能改错了地方")

    # ══════════════ S5. 前端分支（新 type + 老 kind 兼容）══════════════
    print("\n=== S5. 前端分派（新 progress 分支 + reasoning+kind 兼容）===")
    m_new = re.search(r"} else if\(evType === 'progress'\)\s*\{[\s\S]{0,400}?procAddSystemNotice",
                      j_src)
    check("S5a ★ evType==='progress' 分支存在且走 procAddSystemNotice", m_new is not None,
          "未见 progress 分支 ⇒ 后端拆了前端没接，系统提示整条丢失")
    m_compat = re.search(r"} else if\(evType === 'reasoning'\)\s*\{[\s\S]{0,800}?"
                         r"if\(ev\.kind === 'progress'\)\s*\{[\s\S]{0,240}?procAddSystemNotice",
                         j_src)
    check("S5b ★ reasoning+kind 兼容分支仍在（老服务端灰度期不丢事件）",
          m_compat is not None,
          "兼容分支被删 ⇒ 老实例发的 reasoning+kind 事件将静默丢弃")
    m_cot = re.search(r"} else if\(evType === 'reasoning'\)\s*\{[\s\S]{0,1200}?procAddThinking",
                      j_src)
    check("S5c ★ CoT 仍走 procAddThinking（未破坏既有渲染）", m_cot is not None)

    # ══════════════ S6. to_sse_frame 事件名归一 ══════════════
    print("\n=== S6. SSE 帧事件名（type → event:）===")
    try:
        from core.sse import to_sse_frame
        frame = to_sse_frame({"type": "progress", "kind": "progress", "delta": "x"})
        check("S6 ★ to_sse_frame 归一为 event: progress",
              frame.startswith("event: progress\n") and '"delta": "x"' in frame,
              repr(frame[:80]))
    except Exception as e:
        check("S6 ★ to_sse_frame 归一为 event: progress", False,
              "%s %s" % (type(e).__name__, str(e)[:120]))

    # ══════════════ S7. E2E：vm 加载真实 11-pipeline.js 驱动四类事件 ══════════════
    print("\n=== S7. E2E：真实前端分派行为（vm + 真实文件，零 LLM/零服务）===")
    if not node:
        check("S7 找到 node 运行时", False, "未找到 node，跳过行为层")
    else:
        _js = r"""
var calls = [];
var _mkEl = function(){ return { querySelector: function(){ return _mkEl(); },
  classList: {add:function(){}, remove:function(){}, contains:function(){return false;}},
  style: {}, dataset: {}, remove: function(){}, appendChild: function(){}, insertBefore: function(){},
  textContent: '', scrollIntoView: function(){}, parentNode: null }; };
var sandbox = {
  console: console, JSON: JSON, Math: Math, Number: Number, String: String, Object: Object,
  Array: Array, Date: Date, Promise: Promise, isNaN: isNaN, parseInt: parseInt,
  parseFloat: parseFloat, encodeURIComponent: encodeURIComponent,
  decodeURIComponent: decodeURIComponent, setTimeout: setTimeout, clearTimeout: clearTimeout,
  requestAnimationFrame: function(){ return 1; }, cancelAnimationFrame: function(){},
  document: { getElementById: function(id){ return id === 'stream-ai' ? _mkEl() : null; },
              createElement: function(){ return _mkEl(); },
              querySelector: function(){ return null; }, querySelectorAll: function(){ return []; } },
  localStorage: { getItem: function(){ return null; }, setItem: function(){}, removeItem: function(){} },
  window: {}, navigator: {userAgent: 'node'}, location: {href: ''}
};
sandbox.globalThis = sandbox;
require('vm').createContext(sandbox);
require('vm').runInContext(require('fs').readFileSync(process.argv[2], 'utf8'), sandbox,
                           {filename: '11-pipeline.js'});
// 顶层函数声明是 global 的可变绑定 ⇒ 运行期替换即探针（handleSSE 内按名解析）
sandbox.procAddSystemNotice = function(t){ calls.push(['notice', String(t)]); };
sandbox.procAddThinking = function(d, r, k){ calls.push(['thinking', String(d), r, k]); };
sandbox._scheduleSubOutputFlush = function(k){ calls.push(['subout', String(k)]); };
var H = sandbox.handleSSE;
H('event: progress\ndata: {"delta":"（自动编排）门禁测试提示"}\n\n');
H('event: reasoning\ndata: {"delta":"模型真实思考：先看需求条目"}\n\n');
H('event: reasoning\ndata: {"delta":"（自动编排）老服务端兼容提示","kind":"progress"}\n\n');
H('event: token\ndata: {"delta":"正文片段","key":"t1:kb_retrieve"}\n\n');
console.log(JSON.stringify(calls));
"""
        _hf = os.path.join(ROOT, "tmp", "verify_event_split_harness.js")
        os.makedirs(os.path.dirname(_hf), exist_ok=True)
        with open(_hf, "w", encoding="utf-8") as f:
            f.write(_js)
        try:
            import json as _j
            _r = subprocess.run([node, _hf, os.path.join(ROOT, "static/js/mods/11-pipeline.js")],
                                capture_output=True, text=True, timeout=60)
            if _r.returncode != 0:
                check("S7a harness 执行", False, _r.stderr[:300])
            else:
                calls = _j.loads(_r.stdout.strip().split("\n")[-1])
                # 期望调用序列（严格保序：路由顺序即用户看到的时间线顺序）。
                # ⚠️ round/key 在 JSON 里不存在 ⇒ JS 收到 undefined ⇒ 序列化为 null
                #   （期望写 null 不写 0：断言的是"原样透传"，不是"前端补默认值"）。
                expect = [["notice", "（自动编排）门禁测试提示"],
                          ["thinking", "模型真实思考：先看需求条目", None, None],
                          ["notice", "（自动编排）老服务端兼容提示"],
                          ["subout", "t1:kb_retrieve"]]
                check("S7a ★ 四类事件路由序列完全符合预期",
                      calls == expect, "got=%s" % calls)
                check("S7b ★ progress 不产生任何 thinking 调用",
                      all(c[0] != "thinking" or "（自动编排）" not in str(c[1])
                          for c in calls if c[0] == "thinking"),
                      "系统提示漏进了 CoT 流")
                check("S7c ★ reasoning+kind（老服务端）仍路由到 notice",
                      any(c[0] == "notice" and "老服务端兼容提示" in str(c[1]) for c in calls))
        except Exception as e:
            check("S7a harness 执行", False, "%s %s" % (type(e).__name__, str(e)[:150]))

    # ══════════════ S8. 变异自证 ══════════════
    print("\n=== S8. 变异自证===")
    # M1：progress 改回 reasoning（模拟#10 漏拆）⇒ S1a 与 S2 必须 FAIL
    _mut = s_src.replace('"type": "progress"', '"type": "reasoning"')
    _ms = _enum_sites(_mut)
    _mp = [ln for ln, tp, k, t in _ms if tp == "progress"]
    _mr = [ln for ln, tp, k, t in _ms if tp == "reasoning" and k == "progress"]
    check("M1a 变异体已无 progress 站点（变异生效）", not _mp,
          "还有 %d 处" % len(_mp))
    check("M1b ★ 变异体（漏拆）上 S2 必须 FAIL（AST 实跑抓到 %d 处残留）" % len(_mr),
          len(_mr) > 0, "S2 在变异体上竟没抓到残留 ⇒ 判据失效")
    print("  [SELF-PROOF] M1 系统提示改回 reasoning —— 与 CoT 同框混流，"
          "S2 抓到 %d 处" % len(_mr))
    # M2：前端删掉 progress 分支 ⇒ S5a 必须 FAIL
    _jmut = j_src.replace("} else if(evType === 'progress') {",
                          "} else if(evType === 'progress_removed') {")
    _m2 = re.search(r"} else if\(evType === 'progress'\)\s*\{[\s\S]{0,400}?procAddSystemNotice",
                    _jmut)
    check("M2 ★ 变异体（前端删 progress 分支）上 S5a 必须 FAIL",
          _m2 is None, "S5a 在变异体上仍通过 ⇒ 判据失效")
    print("  [SELF-PROOF] M2 前端不接 progress 事件 —— 后端拆了也白拆，"
          "系统提示整条丢失（比混流更隐蔽）")

    return _summary()


def _summary():
    print("\n" + "=" * 60)
    _p = sum(1 for _, o, _ in _results if o)
    _f = len(_results) - _p
    print("#10 事件拆型门禁：%d 条，%d 通过 / %d 失败" % (len(_results), _p, _f))
    if _f:
        print("失败项：")
        for _n, _o, _d in _results:
            if not _o:
                print("  - %s  ← %s" % (_n, _d))
    print("=" * 60)
    return 0 if _f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
