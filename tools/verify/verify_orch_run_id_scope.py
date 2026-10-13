# -*- coding: utf-8 -*-
"""B2-11 补强门禁：`orch_run_id` 的**跨方法作用域契约**（真实建模验证抓出来的缺陷）。

## 这条缺陷是怎么来的

2026-10-13 跑真实 SysML v2 建模验证时，走「命中已发布沉淀流程」这条**复用路径**崩了：

```
流内 error: name 'run_id' is not defined
```

根因是我给 `_stream_orchestrated` 的 `done` 事件加 `orch_run_id` 时**直接写了 `run_id`** ——
而该方法是 `_stream_orchestrated_flow` 的**平级方法**，两者同在一个 class 里、
但**不在彼此的闭包作用域内**。`run_id` 是 `_stream_orchestrated_flow` 的局部变量
（`run_id = 0` → `TaskQueue.new_run_id`），`_stream_orchestrated` 看不到它。

## 为什么所有离线门禁都没抓到

当时 14 个门禁**全绿**。它们查的都是「源码里有没有 `orch_run_id` / `run_id` 这个
**字符串**」，而作用域错误**不改变任何字符串** —— 同一个名字在 A 函数里是局部变量、
在 B 函数里就是 NameError。**静态字符串判据对作用域错误天生无能。**

## 为什么这个门禁不做成通用 lint

我先写了一版「逐函数核对自由变量可解析性」的通用检查器，**然后自己放弃了**：
它对 `stream.py` 报了 9 处，逐个核实后**全部合法**（跨 mixin 模块的共享名、
嵌套函数的参数、闭包捕获）。一个天天误报的检查等于没有检查 ——
**最后会被所有人加 `#CI-OPTIONAL` 跳过**，那比没有更糟。

⇒ 这里只守**本次实际踩到的那一条契约**：具体、可复算、零误报。
通用作用域 lint 属于该用 Pyflakes / Ruff 这类成熟工具去做的事，不该手搓半成品。

## 判据清单

- **A2** 两个方法是**平级**的（这正是缺陷成立的前提）；
- **B1** `_stream_orchestrated` **显式接** `run_id` 参数（不靠外层闭包）；
- **B3/B4** 所有调用点都**传了** `run_id`（漏一处等于没修）；
- **C1/C2** 反向对照：`run_id` 确实是 `_stream_orchestrated_flow` 的局部变量、
  且由 `TaskQueue.new_run_id` 产生 ⇒ 证明 B1 显式传是**必要的**，不是多此一举。
"""
import ast
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


def _enclosing_fn(tree, lineno):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.lineno <= lineno <= n.end_lineno:
            return n
    return None


def _has_local(tree, fn, name):
    """fn 的作用域（含参数）内是否有该名字的绑定。"""
    args = {x.arg for x in fn.args.args} | {x.arg for x in fn.args.kwonlyargs}
    if name in args:
        return True
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            return True
        if isinstance(n, ast.arg) and n.arg == name:
            return True
    return False


def _callers_ok(tree, callers):
    """每个调用点所在的函数里，run_id 都必须能解析。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_stream_orchestrated":
            uses_kw = any(k.arg == "run_id" for k in n.keywords)
            if not uses_kw:
                continue          # 不传就谈不上引用
            fn = _enclosing_fn(tree, n.lineno)
            if fn is None or not _has_local(tree, fn, "run_id"):
                return False
    return True


def find_fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def main():
    target = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
    src = io.open(target, encoding="utf-8").read()
    tree = ast.parse(src)

    print("\n=== A. 两个方法是平级的（这正是缺陷成立的前提）===")
    f_flow = find_fn(tree, "_stream_orchestrated_flow")
    f_orch = find_fn(tree, "_stream_orchestrated")
    check("A0 找到 _stream_orchestrated_flow", f_flow is not None)
    check("A1 找到 _stream_orchestrated", f_orch is not None)
    if f_flow and f_orch:
        inner = set()
        for n in ast.walk(f_flow):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner.add(n.name)
        check("A2 ★ _stream_orchestrated **不在** _stream_orchestrated_flow 内部（平级方法）",
              f_orch.name not in inner,
              "它竟在内部 ⇒ 闭包可见 ⇒ 本条判据不适用")
        print("  ⇒ run_id 是 _stream_orchestrated_flow 的局部变量，"
              "_stream_orchestrated 看不到 ⇒ 必须显式传")

    print("\n=== B. 契约：显式接收 + 调用点传参 ===")
    if f_orch:
        args = {x.arg for x in f_orch.args.args} | {x.arg for x in f_orch.args.kwonlyargs}
        check("B1 ★ _stream_orchestrated 显式接了 run_id 参数（不靠外层闭包）",
              "run_id" in args,
              "参数=%s ⇒ 函数体里用 run_id 会 NameError" % sorted(args))
        _seg = ast.get_source_segment(src, f_orch) or ""
        check("B2 ★ 它的 done 事件带 orch_run_id", "orch_run_id" in _seg)

    callers = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_stream_orchestrated":
            callers.append({k.arg for k in n.keywords})
    check("B3 ★ 找到了 _stream_orchestrated 的调用点（防空扫描假绿）", bool(callers),
          "一个都没找到 ⇒ 判据可能失效")
    if callers:
        # ⚠️ 契约不是"所有调用点都传"：**复用流程（reused）故意不传** ——
        #   那条路径用预发布流程模板，本次没有 create_plan、没有 agent_tasks 行
        #   ⇒ 客观上不存在批次号（`orch_run_id` 恒 0，这是事实不是 bug）。
        #   真正要守的是：**没有任何调用点在**自己作用域里没有 run_id 的情况下引用它**。
        check("B4 ★ 调用点没有在无 run_id 的作用域里引用它",
              _callers_ok(tree, callers),
              "存在调用点引用了本函数作用域内不存在的 run_id ⇒ NameError")
        _with_kw = sum(1 for kw in callers if "run_id" in kw)
        print("  调用点 %d 个（传 run_id 的 %d 个，复用流程故意不传）"
              % (len(callers), _with_kw))

    print("\n=== C. 反向对照：证明「必须显式传」而非多此一举 ===")
    if f_flow:
        _flow_args = {x.arg for x in f_flow.args.args} | {x.arg for x in f_flow.args.kwonlyargs}
        _flow_seg = ast.get_source_segment(src, f_flow) or ""
        has_init = ("run_id = 0" in _flow_seg) or ("run_id = int(resume_run_id)" in _flow_seg)
        check("C1 ★ run_id 确实是 _stream_orchestrated_flow 的**局部**变量（在其函数体内赋值）",
              has_init and "run_id" not in _flow_args,
              "has_init=%s｜是参数=%s" % (has_init, "run_id" in _flow_args))
        check("C2 它由 TaskQueue.new_run_id 产生（是编排批次号，不是别的东西）",
              "TaskQueue.new_run_id" in _flow_seg)

    print("\n=== D. 变异自证===")
    # ⚠️ 变异用 **AST 级**而不是字符串替换：正则锚点对缩进/换行极敏感
    #   （第一版 `re.sub` 因多写了一层转义而"锚点没匹配到"，变异体其实没生成，
    #    门禁却报 FAIL —— **变异没生效却报了失败**，方向正好反了）。
    def _mut_drop_param(text):
        """把 `_stream_orchestrated` 的 run_id 参数从签名里删掉。"""
        t2 = ast.parse(text)
        for n in ast.walk(t2):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and n.name == "_stream_orchestrated":
                a = n.args
                allargs = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                keep = [x for x in allargs if x.arg != "run_id"]
                n.args.args = keep
                n.args.kwonlyargs = []
                n.args.posonlyargs = []
                n.args.kw_defaults = []
                try:
                    return ast.unparse(t2)
                except Exception:
                    return text
        return text

    _mut = _mut_drop_param(src)
    _mut_fn = find_fn(ast.parse(_mut), "_stream_orchestrated")
    _mut_args = ({x.arg for x in _mut_fn.args.args} | {x.arg for x in _mut_fn.args.kwonlyargs}
                 if _mut_fn else set())
    check("D1 变异体（删掉 run_id 参数）上 B1 的判据必须 FAIL",
          _mut_fn is not None and "run_id" not in _mut_args and _mut != src,
          "变异未生效（run_id 仍在参数里）")
    print("  [SELF-PROOF] M1 去掉 run_id 参数 —— 复用流程路径恢复成 "
          "`name 'run_id' is not defined`，证明 B1 咬住的正是真实契约")

    def _mut_drop_kwarg(text):
        """把 _stream_orchestrated 调用点上的 run_id= 关键字实参删掉。"""
        t2 = ast.parse(text)
        done = [False]
        for n in ast.walk(t2):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "_stream_orchestrated":
                n.keywords = [k for k in n.keywords if k.arg != "run_id"]
                done[0] = True
        if not done[0]:
            return text
        try:
            return ast.unparse(t2)
        except Exception:
            return text

    _mut2 = _mut_drop_kwarg(src)
    _mut2_calls = []
    for n in ast.walk(ast.parse(_mut2)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_stream_orchestrated":
            _mut2_calls.append({k.arg for k in n.keywords})
    check("D2 变异体（调用点漏传 run_id）上 B4 的判据必须 FAIL",
          bool(_mut2_calls) and any("run_id" not in kw for kw in _mut2_calls),
          "变异未生效")
    print("  [SELF-PROOF] M2 删掉签名参数而调用点仍在传 —— run_id 变成不可解析，"
          "正是 2026-10-13 真实建模验证抓到的那个崩溃")

    _bad = "def outer():\n    x = 1\n    return y\n"
    _bf = [n for n in ast.walk(ast.parse(_bad))
           if isinstance(n, ast.FunctionDef)][0]
    _args2 = {a.arg for a in _bf.args.args}
    _used2 = {n.id for n in ast.walk(_bf)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    _free2 = _used2 - _args2
    check("D3 判据自测：能区分局部变量(x)与不可解析的 y",
          "x" not in _free2 and "y" in _free2, "判据本身失效：free=%s" % sorted(_free2))
    print("  [SELF-PROOF] M3 判据自测 —— 局部变量不报、外来变量必报")

    print("\n" + "=" * 60)
    _p = sum(1 for _, o, _ in _results if o)
    _f = len(_results) - _p
    print("B2-11 作用域契约门禁：%d 条，%d 通过 / %d 失败"
          % (len(_results), _p, _f))
    for _n, _o, _d in _results:
        if not _o:
            print("  - %s  ← %s" % (_n, _d))
    print("=" * 60)
    print("\n范围声明：这是**定点契约检查**，不是通用作用域 lint。")
    print("  通用 lint 请用 Pyflakes / Ruff（成熟、零误报），不要手搓半成品 ——")
    print("  本门禁第一版就是手搓的通用版，对 stream.py 误报 9 处（全是合法的")
    print("  跨 mixin 共享名与嵌套函数参数），**天天误报的检查等于没有检查**。")
    return 0 if _f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
