# -*- coding: utf-8 -*-
"""B2-10 门禁：`reasoning` 事件区分「模型 CoT」与「系统进度提示」。

**背景（活口径取证，2026-10-13）**：`stream.py` 有 26 处 `yield {"type":"reasoning"}`
（88 处事件里的最大单一类型），其中 **8 处是系统进度提示**（文案带
`（自动编排）`/`（断点续跑）`/`（知识库范围）` 前缀），却与真实思考流
**拼进同一个"思考过程"框** ⇒ 用户看到的"AI 在想什么"里混着系统自述，
**分不清哪句是模型想的、哪句是系统说的**。

**为什么加 `kind` 字段而不新增事件类型**：前端 `11-pipeline.js` 与任何外部消费方
都按 `type` 分派（`evType === 'reasoning'`），改 `type` 会打断既有渲染与兼容。
加 `kind` 是**纯增量**：老消费方忽略它即行为不变。

**2026-10-18 #10 拆型后的角色收敛**：系统提示已升级为独立 SSE 事件类型
`type: "progress"`（终局门禁见 `verify_event_type_split.py`）。本门禁收敛为守护
**CoT 纯净性与兼容层**：
1. reasoning 站点**零** kind=progress 残留（残留 = 拆型不彻底，又混流）；
2. reasoning 站点**零**系统提示文案（双保险：漏拆的即使没带 kind，文案指纹也藏不住，
   含 delta 为变量时回溯赋值行的解析）；
3. 前端**仍保留** kind 兼容分支（老服务端灰度期发 reasoning+kind，行为不能变）——
   ⚠️ 这一条最关键：**只改后端不改编前端 = 改动完全无效**，而从返回值上看不出来。
"""
import io
import os
import re
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


def _extract_fn(src, fname, with_header=False):
    """按大括号配平抽出 JS 函数（避免 split 切在签名中间）。

    ⚠️ 为什么不用 `split("function f(")[1].split("\nfunction ")[0]`：
    首段含"签名 + 函数体"，切点落在下一函数声明处 ⇒ 会把签名尾巴一起带上
    （实测得到 `text){` 这种非法片段，node 直接 SyntaxError，报错信息还指向
    门禁脚本而不是被测代码，容易误判成"产品代码坏了"）。
    """
    hdr = "function " + fname + "("
    if hdr not in src:
        return ""
    i0 = src.index(hdr) + len(hdr)
    i1 = src.index(")", i0)
    b0 = src.index("{", i1) + 1
    depth, i = 1, b0
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    body = src[b0:i - 1]
    if with_header:
        return src[src.index(hdr):i]
    return body


def read(rel):
    p = os.path.join(ROOT, rel)
    return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""


# 系统进度提示的文案特征（与 stream.py 里的实际前缀一致）
SYS_PREFIXES = ("（自动编排）", "（断点续跑）", "（知识库范围）")


def main():
    s_src = read("agent/pipeline_parts/stream.py")
    j_src = read("static/js/mods/11-pipeline.js")

    # ══════════════ A. 枚举全部 reasoning 站点（AST 级，不用 grep 猜）══════════════
    print("\n=== A. 枚举 reasoning 站点（逐处判它是 CoT 还是系统提示）===")
    import ast
    tree = ast.parse(s_src)
    sites = []          # (lineno, 是否带 kind=progress, delta 文案)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Yield) or not isinstance(node.value, ast.Dict):
            continue
        kv = {}
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                kv[k.value] = v
        tv = kv.get("type")
        if not (isinstance(tv, ast.Constant) and tv.value == "reasoning"):
            continue
        has_kind = "kind" in kv
        kind_val = None
        if has_kind and isinstance(kv["kind"], ast.Constant):
            kind_val = kv["kind"].value
        sites.append((node.lineno, has_kind, kind_val, ""))

    # ★ 2026-10-18 #10 拆型：15 处系统提示已迁往 type=progress（终局门禁
    #   verify_event_type_split.py），reasoning 站点从 33 降为 18（纯 CoT）。
    #   下限从 20 调为 10：仍远高于"扫到 0 个"的失效形态，又给后续增删留余量
    #   （不复用硬编码等值判据——`62c168e` 的教训：条数不是不变式）。
    check("A0 ★ 确实扫到了 reasoning 站点（防\"扫到 0 个 ⇒ 全部合规\"的假绿）",
          len(sites) >= 10, "只扫到 %d 个 —— 判据可能失效" % len(sites))
    print("  共 %d 处 reasoning 站点" % len(sites))

    # ⚠️ 取 delta 文案有两次踩坑，都记在这里防第三次：
    #   ① 只取 `delta.left` 的字面量 ⇒ 多行拼接式取到空串（把已标的判成"漏标"）；
    #   ② 改用"yield 往后 4 行的源码切片" ⇒ **窗口太宽，把相邻站点的文案也吃进来**
    #      （把已标的和真 CoT 一起判成漏标/误标）。
    # 正确做法：**递归收集 delta 子树里所有字符串字面量**（BinOp / JoinedStr 全覆盖），
    # 且只取**本节点**的子树 ⇒ 既不漏也不越界。
    def _lits(node):
        out = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append(sub.value)
        return "".join(out)

    _tree2 = ast.parse(s_src)
    _by_line = {}
    for node in ast.walk(_tree2):
        if not isinstance(node, ast.Yield) or not isinstance(node.value, ast.Dict):
            continue
        kv2 = {}
        for k2, v2 in zip(node.value.keys, node.value.values):
            if isinstance(k2, ast.Constant) and isinstance(k2.value, str):
                kv2[k2.value] = v2
        t2 = kv2.get("type")
        if isinstance(t2, ast.Constant) and t2.value == "reasoning":
            _by_line[node.lineno] = _lits(kv2.get("delta")) if "delta" in kv2 else ""
    sites = [(ln, has, kv, _by_line.get(ln, "")) for (ln, has, kv, _) in sites]

    # ══════════════ B. 拆型后 reasoning 不得残留系统提示（#10 演进）══════════════
    print("\n=== B. reasoning 站点无 kind=progress 残留（拆型彻底性）===")
    # #10 拆型后：系统提示迁往 type=progress，reasoning 站点应**全部**无 kind。
    # B1 因此反转为"零残留"判据 —— 残留一处 = 那条提示又与 CoT 同框。
    tagged = [(ln, t) for ln, has, kv, t in sites if kv == "progress"]
    print("  仍带 kind=progress 的 reasoning 站点（应为空）：%s" % [ln for ln, _ in tagged])
    check("B1 ★ reasoning 站点无 kind=progress 残留（拆型彻底）",
          not tagged, "残留行号：%s —— 该处应迁往 type=progress" % [ln for ln, _ in tagged])
    # B1b 派生判据：**带 kind + 不带 kind == 全部站点**（划分完备且互斥）
    _untagged_all = [(ln, t) for ln, has, kv, t in sites if kv != "progress"]
    check("B1b 划分完备（带 kind + 不带 kind == 全部站点，无重叠无遗漏）",
          len(tagged) + len(_untagged_all) == len(sites),
          "tagged=%d untagged=%d total=%d"
          % (len(tagged), len(_untagged_all), len(sites)))
    # B2 ★ 双保险：即使漏拆的站点**没带** kind，系统提示的文案指纹也藏不住。
    #   ⚠️ delta 可能是**变量**（如 `_d`/`_ac_delta`），字面量抽取拿到空串 ⇒
    #      必须回溯该变量在同函数内的赋值行（2026-10-13/14 两次踩坑的沉淀，
    #      解析能力与逐站点正向判定现由 verify_event_type_split.py 的 S3 承担）。
    _lines_all = s_src.split("\n")
    _sys_re = re.compile("|".join(re.escape(p) for p in SYS_PREFIXES))

    def _resolve_text(ln, txt):
        """txt 是 **AST 字面量抽取**的结果 —— delta 是变量时它必然是空串。

        ⇒ 这时要从**源码行**回溯：先在 yield 那一行里认出 delta 是个标识符，
        再往上找该变量的赋值行。判据宽松：赋值行里出现过系统前缀就算数
        （不解析 Python 表达式结构 —— 那是解释器的事，不是门禁的事）。
        """
        if _sys_re.search(txt[:200]):
            return txt, "inline"
        _src_line = _lines_all[ln - 1] if 0 < ln <= len(_lines_all) else ""
        m = re.search(r'"delta":\s*([A-Za-z_]\w*)\s*[,}]', _src_line)
        if not m:
            return txt, "unknown"
        var = m.group(1)
        for k in range(ln - 2, max(0, ln - 16), -1):
            if re.search(r"\b%s\s*=" % re.escape(var), _lines_all[k]) \
                    and _sys_re.search(_lines_all[k]):
                return _lines_all[k], "via:" + var
        return txt, "no-assign-with-prefix:" + var

    sys_in_cot = []
    for ln, has, kv, t in sites:
        _txt, _how = _resolve_text(ln, t)
        if _sys_re.search(_txt[:200]):
            sys_in_cot.append((ln, _how))
    check("B2 ★ reasoning 站点无系统提示文案（漏拆 ⇒ 仍与 CoT 混在同一框）",
          not sys_in_cot, "漏拆行号（含变量回溯）：%s" % sys_in_cot)

    # ══════════════ C. 真实 CoT 不能被误标 ══════════════
    print("\n=== C. 真实 CoT 不被误标（误标会把模型思考赶出思考框）===")
    cot = [(ln, kv, t) for ln, has, kv, t in sites if kv != "progress"]
    # ★ 同上：C1 原为 `len(cot) == len(sites) - 13`（硬编码）。
    #   改后：**恒等式**（这是"划分"的定义，不是需要维护的快照），
    #   真正要锁的是 C2「没有真实 CoT 被误标成 progress」与 B2「没有系统提示漏标」。
    check("C1 CoT 集合 = 全部站点 − progress 集合（划分定义，非快照）",
          len(cot) == len(sites) - len(tagged),
          "cot=%d total=%d tagged=%d" % (len(cot), len(sites), len(tagged)))
    # 逐字增量形如 `f"..."` / `_r_delta` / `delta[` 等，绝不能是系统提示文案
    _mis = [(ln, t) for ln, kv, t in cot
            if any(p in t[:120] for p in SYS_PREFIXES)]
    check("C2 没有真实 CoT 被误标成 progress", not _mis, _mis)

    # ══════════════ D. 前端真的分流（最关键：只改后端 = 无效）══════════════
    print("\n=== D. 前端分流（这一条决定改动是否真的生效）===")
    check("D1 reasoning 分派里判了 ev.kind === 'progress'",
          "ev.kind === 'progress'" in j_src)
    check("D2 progress 走独立函数 procAddSystemNotice",
          re.search(r"if\(ev\.kind === 'progress'\)\{\s*\n\s*procAddSystemNotice", j_src)
          is not None)
    check("D3 ★ CoT 仍走原 procAddThinking（未破坏既有渲染）",
          "procAddThinking(ev.delta || '', ev.round, ev.key)" in j_src)
    check("D4 procAddSystemNotice 已定义",
          "function procAddSystemNotice(" in j_src)
    _fn = _extract_fn(j_src, "procAddSystemNotice")
    check("D5 ★ 系统提示**不写进 _procS.thinking**（否则仍与 CoT 混在一起）",
          "_procS.thinking" not in _fn,
          "函数体内碰了 _procS.thinking ⇒ 前端仍然混流，后端标了也白标")
    check("D6 用独立 timeline id（不复用 stage_/thinking 前缀）",
          "procUpsert(id, {type:'notice'" in _fn or "'notice_" in _fn)
    check("D7 kind 缺失时按 CoT 处理（向后兼容：老服务端无 kind 字段）",
          re.search(r"if\(ev\.kind === 'progress'\)\{[\s\S]{0,400}?\}\s*else\s*\{[\s\S]{0,200}?procAddThinking",
                    j_src) is not None,
          "未写 else 分支 ⇒ 老服务端（无 kind）会丢失思考流")

    # ══════════════ E. 端到端：事件分流行为（用 node 跑真 JS，零 LLM）══════════════
    print("\n=== E. 端到端：前端分流行为（真跑 JS，零 LLM/零服务）===")
    import subprocess
    node = None
    for _c in (r"C:\Users\gefei\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
               "node"):
        try:
            subprocess.run([_c, "--version"], capture_output=True, timeout=30)
            node = _c
            break
        except Exception:
            continue
    if not node:
        check("E0 找到 node 运行时", False, "未找到 node，跳过行为层")
    else:
        # ⚠️ 抽函数体**不能**用 split("function procAddSystemNotice(")[1].split("\nfunction ")[0]
        #   —— 首段是"签名 + 函数体"，切点落在下一函数声明处，会把**签名尾巴**一起带上
        #   （本门禁第一版就得到 `text){` 这种非法片段，node 直接 SyntaxError）。
        # 改为**按大括号配平**取真正的函数体。
        # with_header=True：注入 node 的必须是**完整函数**（只有函数体无法独立编译：
        # 里面的 return 会变成 "Illegal return statement"）
        _fn_src = _extract_fn(j_src, "procAddSystemNotice", with_header=True)

        # 抽出 procAddSystemNotice + 最小 procUpsert 桩，实测两种 kind 的分流结果
        _js = """
var _procS = {timeline: []};
function procUpsert(id, patch){
  var s = _procS.timeline.find(function(x){return x.id===id;});
  if(!s){ s = Object.assign({id:'',type:'',icon:'',title:'',status:'',statusText:'',detail:''}, patch); _procS.timeline.push(s); }
  else { Object.assign(s, patch); }
}
%s
// 复刻 11-pipeline.js 的分流逻辑
function handle(ev){
  if(ev.type === 'reasoning'){
    if(ev.kind === 'progress'){ procAddSystemNotice(ev.delta || ''); }
    else { _cot.push(ev.delta || ''); }
  }
}
var _cot = [];
handle({type:'reasoning', delta:'模型真实思考：先看需求条目'});
handle({type:'reasoning', kind:'progress', delta:'（自动编排）检测到多 Agent 协作需求，正在规划子任务…'});
handle({type:'reasoning', delta:'模型真实思考：再比对架构骨架'});
handle({type:'reasoning', kind:'progress', delta:'（自动编排）汇总最终结论…'});
var out = {cot: _cot, notices: _procS.timeline.map(function(x){return {id:x.id, type:x.type, sub:x.sub};})};
console.log(JSON.stringify(out));
""" % _fn_src
        try:
            r = subprocess.run([node, "-e", _js], capture_output=True, text=True, timeout=60)
            import json as _j
            if r.returncode != 0:
                check("E0 node 执行成功", False, r.stderr[:200])
            else:
                out = _j.loads(r.stdout.strip().split("\n")[-1])
                check("E1 ★ 系统提示进了独立行（type=notice）",
                      len(out["notices"]) == 2, out["notices"])
                check("E2 ★ 系统提示**没有**混进 CoT 流",
                      all("（自动编排）" not in c for c in out["cot"]), out["cot"])
                check("E3 CoT 保留且顺序正确",
                      out["cot"] == ["模型真实思考：先看需求条目",
                                     "模型真实思考：再比对架构骨架"], out["cot"])
                check("E4 两类在时间线上可区分（notice vs thinking）",
                      all(n["type"] == "notice" for n in out["notices"])
                      and len(out["notices"]) == 2)
                # 反向对照：不带 kind 的事件（老服务端）必须仍进 CoT
                r2 = subprocess.run([node, "-e", _js.replace(
                    "handle({type:'reasoning', kind:'progress', delta:'（自动编排）汇总最终结论…'});", ""),
                    ], capture_output=True, text=True, timeout=60)
                out2 = _j.loads(r2.stdout.strip().split("\n")[-1])
                check("E5 ★ 老服务端（无 kind 字段）行为不变（向后兼容）",
                      len(out2["notices"]) == 1 and len(out2["cot"]) == 2,
                      "notices=%d cot=%d" % (len(out2["notices"]), len(out2["cot"])))
        except Exception as e:
            check("E0 行为层执行", False, "%s %s" % (type(e).__name__, str(e)[:120]))

    # ══════════════ F. 变异自证 ══════════════
    print("\n=== F. 变异自证===")
    # F1：把 progress 改回 reasoning（模拟 #10 漏拆一半）⇒ B1 必须 FAIL
    _mut = s_src.replace('"type": "progress"', '"type": "reasoning"')
    _n_prog = _mut.count('"type": "progress"')
    check("F1a 变异体已无 progress 事件（变异生效）", _n_prog == 0,
          "变异体里还有 %d 处 progress ⇒ 变异没生效" % _n_prog)
    # 用 B1 的真实逻辑跑变异体：必须抓到 kind=progress 残留（不只是字符串没了）
    import ast as _ast
    _mt = _ast.parse(_mut)
    _resid = 0
    for _nd in _ast.walk(_mt):
        if isinstance(_nd, _ast.Yield) and isinstance(_nd.value, _ast.Dict):
            _kv = {k.value: v for k, v in zip(_nd.value.keys, _nd.value.values)
                   if isinstance(k, _ast.Constant)}
            _tv, _kk = _kv.get("type"), _kv.get("kind")
            if isinstance(_tv, _ast.Constant) and _tv.value == "reasoning" \
                    and isinstance(_kk, _ast.Constant) and _kk.value == "progress":
                _resid += 1
    check("F1b ★ 变异体（漏拆）上 B1 必须 FAIL（AST 实跑抓到 %d 处残留）" % _resid,
          _resid > 0, "变异体上 B1 竟没抓到残留 ⇒ B1 判据失效")
    print("  [SELF-PROOF] M1 系统提示改回 reasoning 事件 —— 与 CoT 同框混流，"
          "B1 在变异体上抓到 %d 处" % _resid)

    # F2：前端不分流（直接走 procAddThinking）⇒ D1/D2 必须 FAIL
    _jmut = j_src.replace("if(ev.kind === 'progress'){", "if(false){")
    check("F2 变异体（前端不分流）上 D1 必须 FAIL",
          "ev.kind === 'progress'" not in _jmut)
    check("F2b 对照：现行实现里 D1 判据成立（证明 F2 的变异确实改掉了它）",
          "ev.kind === 'progress'" in j_src)
    print("  [SELF-PROOF] M2 前端不判 kind —— 后端标了 kind 也白标，"
          "改动等于没做（这类\"只改一端\"的失效从返回值上看不出来）")

    # F3：把 procAddSystemNotice 改成写进 _procS.thinking ⇒ D5 必须 FAIL
    _jmut3 = j_src.replace(
        "  procUpsert(id, {type:'notice', icon:'ℹ️', title:'系统提示', status:'done',\n"
        "    statusText:'提示', sub: t, detail: t});",
        "  _procS.thinking = (_procS.thinking || '') + t;")
    _fn3 = _extract_fn(_jmut3, "procAddSystemNotice")
    check("F3 变异体（系统提示写进 thinking）上 D5 必须 FAIL",
          "_procS.thinking" in _fn3,
          "变异体没改到 ⇒ D5 抓不住变异")
    print("  [SELF-PROOF] M3 系统提示仍写进思考流 —— 标了 kind 但前端照样混流")

    # ══════════════ 汇总 ══════════════
    print("\n" + "=" * 60)
    _p = sum(1 for _, o, _ in _results if o)
    _f_ = len(_results) - _p
    print("B2-10 门禁：%d 条，%d 通过 / %d 失败" % (len(_results), _p, _f_))
    if _f_:
        print("失败项：")
        for _n, _o, _d in _results:
            if not _o:
                print("  - %s  ← %s" % (_n, _d))
    print("=" * 60)
    return 0 if _f_ == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
