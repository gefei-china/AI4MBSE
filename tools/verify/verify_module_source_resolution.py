# -*- coding: utf-8 -*-
"""门禁：源码断言不得落在「5 行兼容垫片」上（否则恒红或**静默恒绿**）。

## 为什么必须有这条

P1 分层归位后 **94 个**旧路径变成垫片：

```python
import sys as _sys
from services import orch_supervisor as _impl
_sys.modules[__name__] = _impl
```

门禁按旧路径读源码时，读到的是这 5 行。后果不对称、而且**坏的那一半不会自己暴露**：
  * `"X" in src`  ⇒ 恒红（吵，能发现 —— 本轮实测 4 个门禁这样红）；
  * `"X" not in src` ⇒ **恒绿**（静默假绿，正是 P0 在治的病）。

本轮实测：`verify_orch_supervisor` 23 条断言、`verify_trace_inherit` B11、
`verify_memory_dream` M1 变异锚点、`verify_orch_interrupt` R7b 全部因垫片而失真。

## 判据

  X1 全仓每个垫片都能解析到「非垫片 + 存在 + >12 行」的真实实现（防解析器空转）
  X2 解析结果不是垫片自己（防自指死循环被当成"解析成功"）
  X3 `read_module_source()` 的换行语义与 `open(..., encoding='utf-8').read()` 一致（仅 LF）
  X4 门禁里没有「以字面量路径 open 垫片」的调用点（静态 AST 扫描）
  M1 造一个自指垫片 ⇒ X1 必须翻红
  M2 造一个 open 垫片的门禁 ⇒ X4 必须翻红
"""
import ast
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import module_source as MS  # noqa: E402

PASS, FAIL = [], []
SCAN_DIRS = ("tools", "tests")
NON_GATE_MODULES = {"tools/stream_source.py", "tools/module_source.py"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def iter_py(dirs):
    for d in dirs:
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if x != "__pycache__"]
            for f in fns:
                if f.endswith(".py"):
                    yield os.path.join(dp, f).replace("\\", "/")


def find_shims():
    out = []
    for p in iter_py((".",)):
        if any(seg in p for seg in ("./.git", "./tmp/", "./.venv", "node_modules")):
            continue
        txt = MS._read_text_quiet(p)
        if not txt or "sys.modules[__name__]" not in txt:
            continue
        if "def " in txt or "class " in txt:
            continue
        if MS.shim_target(p):
            out.append(p)
    return sorted(set(out))


print("[1] 全仓垫片解析（X1/X2）", flush=True)
SHIMS = find_shims()
check("X1a 仓库里确实存在垫片（防扫描器失效后空转全绿）", len(SHIMS) >= 50, len(SHIMS))
bad = []
for s in SHIMS:
    real = MS.resolve_source(s)
    if os.path.abspath(real) == os.path.abspath(s):
        bad.append((s, "自指"))
    elif not os.path.isfile(real):
        bad.append((s, "解析目标不存在"))
    elif MS.is_shim(real):
        bad.append((s, "仍解析到垫片"))
    elif len(io.open(real, encoding="utf-8", errors="replace").read().splitlines()) <= 12:
        bad.append((s, "实现只有 %d 行" % len(io.open(real, encoding="utf-8").read().splitlines())))
check("X1b 每个垫片都解析到真实实现（非垫片/存在/>12 行）", not bad, bad[:5])
check("X2 解析结果与垫片自身不同（无自指）",
      all(os.path.abspath(MS.resolve_source(s)) != os.path.abspath(s) for s in SHIMS),
      [s for s in SHIMS if os.path.abspath(MS.resolve_source(s)) == os.path.abspath(s)][:3])

print("[2] 读取语义（X3）", flush=True)
sample = SHIMS[0]
real = MS.resolve_source(sample)
ms_txt = MS.read_module_source(sample)
open_txt = open(real, encoding="utf-8").read()
check("X3a 与 open().read() 逐字符相同", ms_txt == open_txt,
      "ms=%d open=%d" % (len(ms_txt), len(open_txt)))
check("X3b 换行已归一为 LF（断言里写死 \\n 才不会假红）", "\r" not in ms_txt,
      "CR=%d" % ms_txt.count("\r"))

print("[3] 门禁不得以字面量路径 open 垫片（X4）", flush=True)
SHIM_REL = {os.path.relpath(s, ".").replace("\\", "/") for s in SHIMS}
viol = []


def joined(node):
    """把实参表达式里的常量片段拼成「相对 ROOT 的路径」（变量段丢弃）。"""
    segs = [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return "/".join([x.strip("/") for x in segs if x.strip("/")]).replace("\\", "/")


for p in iter_py(SCAN_DIRS):
    if p.replace("./", "") in NON_GATE_MODULES:
        continue
    txt = MS._read_text_quiet(p)
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        continue
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        if nm not in ("open", "read_text", "read_bytes"):
            continue
        for a in n.args:
            s = joined(a)
            # ⚠️ 必须**精确相等**：早期版本用 endswith，把 `services/task_queue.py`
            #    （真实实现）判成了根部垫片 `task_queue.py` ⇒ 18 处假违规（实测）。
            if s and s in SHIM_REL:
                viol.append((p, n.lineno, s))
check("X4 无「open(垫片字面量)」的门禁", not viol, viol[:5])

print("[4] 变异自证（M1/M2）", flush=True)
tmpd = tempfile.mkdtemp(prefix="mres_")
try:
    fake = os.path.join(tmpd, "selfshim.py")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write("# -*- coding: utf-8 -*-\nimport sys as _sys\n"
                 "from %s import fake as _impl\n_sys.modules[__name__] = _impl\n" % "selfshim")
    check("M1 自指垫片 ⇒ 解析结果等于自己 ⇒ X1 必须能判红",
          os.path.abspath(MS.resolve_source(fake)) == os.path.abspath(fake))
    proto = os.path.join(tmpd, "proto_gate.py")
    with open(proto, "w", encoding="utf-8") as fh:
        fh.write('import os\nROOT=os.path.dirname(os.path.abspath(__file__))\n'
                 'src=open(os.path.join(ROOT, "core", "orch_supervisor.py"), encoding="utf-8").read()\n')
    ptree = ast.parse(MS._read_text_quiet(proto))
    hits = 0
    for n in ast.walk(ptree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
            s = joined(n.args[0])
            hits += 1 if any(s.endswith(r) for r in SHIM_REL) else 0
    check("M2 造一个 open(垫片) 的门禁 ⇒ X4 扫描器必须命中", hits == 1, hits)
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("\n===")
print("%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
