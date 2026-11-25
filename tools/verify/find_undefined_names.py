# -*- coding: utf-8 -*-
"""未定义名字扫描（NameError 静态发现）—— 治「注释写了、代码漏了」这类静默失效。

## 为什么要这个工具（2026-11-19 实测事故）

`agent/final_content.py` 的 `_is_interim` 里写着：

    return _similar(ne, nl) >= _SIM_OTHER      # ← _SIM_OTHER 从未定义

而上方注释明写「门槛取 0.75 高位」—— **注释写了，赋值漏了**。
调用点 `dedupe_interim_code_blocks` 外层是 `except Exception: return text, 0`
⇒ `NameError` 被静默吞掉 ⇒ **代码块去重从上线那天起一次都没真正跑过**。
用户反馈"代码/提示词重复"多次，每轮都在调判据，而函数根本没跑通。

并且它只在**特定分支**触发：只有含 `text`（非 sysml）代码块时才走到那行
⇒ 只测 sysml 样本永远测不出来（真库 #5135 是 7 个 sysml 块，能收敛；
   #7039 有 2 个 text 块，整函数放弃）。所以必须**静态**发现，不能靠样本。

## 判据

用 `symtable` 逐作用域检查：某符号在函数里被**引用**且解析到模块/全局作用域，
但模块级既没有定义、也不是 builtins ⇒ 一旦该分支执行就是 NameError。

刻意**不报**的（避免噪音）：
  · 模块级 `import *` 引入的名字（无法静态确定）；
  · `__all__`/`__name__` 等 dunder；
  · 在 try/except ImportError 里定义的名字（运行时可能不存在，但那是设计行为）；
  · 函数内 `global X` 声明后赋值的（业务上是有意为之）。

用法：
  .venv\\Scripts\\python.exe -X utf8 tools/verify/find_undefined_names.py [路径...]
  默认扫 agent/ core/ services/ workflows/ routers/ 与根目录关键模块。
退出码：发现问题 1，干净 0。
"""
import builtins
import os
import symtable
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_TARGETS = [
    # 2026-11-25 P1 分层归位后：原列在根目录的 4 个文件已迁入 services/ 与 services/tools/，
    # 而 "services" 目录本身递归覆盖它们 ⇒ 只列目录即可（列文件名反而会因不存在而**静默漏扫**）。
    # 另补 adapters（新增的对外适配层）；原 "final_content.py" 早已不存在（零覆盖却不报错）。
    "agent", "core", "services", "adapters", "workflows", "routers", "database",
]

SKIP_DIR = {".venv", "__pycache__", "node_modules", ".git", "tmp", "backups", "logs"}

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__",
                                  "__spec__", "__loader__", "__builtins__", "__debug__"}


def _iter_py(target):
    p = target if os.path.isabs(target) else os.path.join(ROOT, target)
    if os.path.isfile(p):
        yield p
        return
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _module_defined(st):
    """模块级「已定义」的名字集合（赋值/函数/类/import/参数化）。"""
    out = set()
    for sym in st.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            out.add(sym.get_name())
    # 有 `import *` ⇒ 无法静态确定，整体放弃该文件（报出来也没意义）
    return out


def _check_scope(st, module_names, path, out, has_star):
    if has_star:
        return
    for sym in st.get_symbols():
        n = sym.get_name()
        if n in _BUILTINS or n.startswith("__") and n.endswith("__"):
            continue
        if not sym.is_referenced():
            continue
        # 本作用域自己解析得了（局部/参数/本作用域赋值）⇒ 不是全局引用
        if sym.is_assigned() or sym.is_parameter() or sym.is_imported() or sym.is_namespace():
            continue
        if sym.is_global():
            # `global X` 声明：可能是模块级定义，也可能真的没定义
            if n not in module_names:
                out.append((path, n, st.get_name() or "<module>", "global 声明但模块无定义"))
            continue
        if sym.is_free():
            # 闭包里来自外层函数 ⇒ 由外层作用域负责
            continue
        # 其余：解析到模块/内置作用域
        if n not in module_names:
            out.append((path, n, st.get_name() or "<module>", "引用但模块级无定义"))
    for child in st.get_children():
        _check_scope(child, module_names, path, out, has_star)


def scan_file(path):
    try:
        src = open(path, "rb").read().decode("utf-8", errors="replace")
        st = symtable.symtable(src, path, "exec")
    except SyntaxError:
        return []
    except Exception:                                    # noqa: BLE001
        return []
    has_star = "import *" in src
    module_names = _module_defined(st)
    out = []
    _check_scope(st, module_names, path, out, has_star)
    return out


def main():
    targets = sys.argv[1:] or DEFAULT_TARGETS
    files = []
    for t in targets:
        files.extend(_iter_py(t))
    files = sorted(set(files))
    print("=" * 84)
    print("未定义名字扫描（NameError 静态发现）")
    print("  文件数=%d  repo=%s" % (len(files), ROOT))
    print("=" * 84)
    found = []
    for f in files:
        found.extend(scan_file(f))
    if not found:
        print("\n✅ 未发现「引用但模块级无定义」的名字")
        return 0
    by_file = {}
    for path, name, scope, why in found:
        by_file.setdefault(os.path.relpath(path, ROOT), []).append((name, scope, why))
    print("\n发现 %d 处（分布在 %d 个文件）：" % (len(found), len(by_file)))
    for f, items in sorted(by_file.items()):
        print("\n  %s" % f)
        seen = set()
        for name, scope, why in items:
            k = (name, scope)
            if k in seen:
                continue
            seen.add(k)
            print("    · %-28s in %-24s %s" % (name, scope, why))
    return 1


if __name__ == "__main__":
    sys.exit(main())
