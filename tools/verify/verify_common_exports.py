# -*- coding: utf-8 -*-
"""verify_common_exports.py —— 守卫：common.py 新增顶层名字必须能被 execute.py/stream.py 看到。

背景（2026-10-10 实测事故）：并行批次往 common.py 加了 CHAT_TASK_PROBE/REPAIR/ANSWER
与 _lg_* 辅助函数，但漏加进 `__all__` ⇒ `from .common import *` 拿不到 ⇒
execute.py / stream.py 运行时 `NameError: CHAT_TASK_PROBE is not defined` ⇒
**流式与非流式两条主路径全部对话一轮即挂**（会话 9336 msg 3895/3896 实证）。
当时 verify_chat_task_intent.py 只做**源码字符串断言**（grep 到 `_intent=CHAT_TASK_PROBE` 就绿），
静态断言抓不到"名字未导入"这类执行期缺陷（教训与 §6.9a 同族）。

判据（三层，全部行为级）：
  [1] import * 语义：真正执行 `from agent.pipeline_parts.common import *`，
      断言 common 顶层每个公共名字（及被两路径引用的私有名字）都可取到。
  [2] AST 交叉引用：execute.py / stream.py 引用 Load 态名字中，属于 common 顶层定义
      且非本地定义/导入的，必须能从两路径模块命名空间取到（覆盖漏 __all__ 与漏定义两种）。
  [3] 变异自证：临时把 __all__ 里删掉 CHAT_TASK_PROBE（exec 变异孪生体，不改磁盘），
      [2] 的判据必须翻红 —— 证明守卫有牙齿。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_common_exports.py
退出码：0 = 全绿；1 = 有失败。
"""
import ast
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

COMMON = os.path.join(ROOT, "agent", "pipeline_parts", "common.py")
CONSUMERS = [
    os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"),
    os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"),
]

_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def top_defs(src):
    tree = ast.parse(src)
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name):
                            names.add(e.id)
    return names


def all_list(src):
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets):
            return set(ast.literal_eval(n.value))
    return None


def local_bindings(src):
    """消费方文件内自带的名字（定义/赋值/参数/导入）——这些不依赖 common。"""
    tree = ast.parse(src)
    local = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local.add(n.id)
        elif isinstance(n, ast.arg):
            local.add(n.arg)
        elif isinstance(n, ast.Import):
            for a in n.names:
                local.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                local.add(a.asname or a.name)
    return local


def load_names(src):
    tree = ast.parse(src)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def foreign_refs(common_defs, src, path):
    """该消费方引用了、属于 common 顶层定义、且非本地绑定的名字 → (名字, 首处行号)。"""
    local = local_bindings(src)
    loaded = load_names(src)
    hits = sorted(loaded & common_defs - local)
    out = []
    lines = src.splitlines()
    for h in hits:
        for i, line in enumerate(lines, 1):
            if h in line and "import" not in line:
                out.append((h, i))
                break
    return out


def run_checks(mutated_all=None):
    """跑 [1][2]；mutated_all 非空时用 exec 孪生体替换 common 的 __all__（变异自证用）。"""
    src = io.open(COMMON, encoding="utf-8").read()
    common_defs = top_defs(src)
    export_list = mutated_all if mutated_all is not None else all_list(src)

    # [1] import * 语义（行为级）：真执行 star-import，逐个断言
    ns = {}
    if mutated_all is None:
        exec(compile("from agent.pipeline_parts.common import *", "<star>", "exec"), ns)
        missing_star = [d for d in sorted(common_defs)
                        if d not in ("__all__",) and not d.startswith("_") and d not in ns]
        check("[1] common 公共顶层名字全部可被 import * 取到", not missing_star,
              "缺: %s" % missing_star)

    # [2] 消费方引用的 common 名字必须可达
    fails = []
    for path in CONSUMERS:
        csrc = io.open(path, encoding="utf-8").read()
        refs = foreign_refs(common_defs, csrc, path)
        modname = os.path.basename(path)[:-3]
        if mutated_all is None:
            import importlib
            mod = importlib.import_module("agent.pipeline_parts." + modname)
        else:
            # 变异体：exec common 源码（__all__ 被改）后模拟 star-import 再检查。
            # ⚠️ 语义必须与 Python 一致：定义了 __all__ 时，**只有 __all__ 里的名字**被导出
            #（下划线规则只在未定义 __all__ 时生效——首版写成
            #  `k in export_list or not k.startswith("_")`，把变异体放跑了 ⇒ 假绿）。
            exec(compile(src, COMMON, "exec"), (ns := {}))
            mutated_ns = {k: v for k, v in ns.items() if k in export_list}
            mod = type("M", (), mutated_ns)
        for name, lineno in refs:
            if not hasattr(mod, name):
                fails.append("%s.py:%d NameError: %s" % (modname, lineno, name))
    tag = "（变异体）" if mutated_all is not None else ""
    ok = not fails
    if mutated_all is None:
        _results.append(ok)   # 变异运行不进总账（它的 FAIL 是期望行为，由 [3] 记）
    print("[%s] [2]%s execute/stream 引用的 common 名字全部运行时可达"
          % ("OK  " if ok else "FAIL", tag),
          ("" if ok else "　— " + "; ".join(fails[:6])))
    return fails


def main():
    print("=== common.py 导出完整性守卫 ===")
    fails = run_checks()

    # [3] 变异自证：exec 孪生体把 __all__ 删掉 CHAT_TASK_PROBE → [2] 必须翻红（磁盘零改动）
    src = io.open(COMMON, encoding="utf-8").read()
    cur_all = all_list(src)
    mutated = cur_all - {"CHAT_TASK_PROBE"}
    mfails = run_checks(mutated_all=mutated)
    check("[3] 变异自证：__all__ 漏 CHAT_TASK_PROBE 时判据必须翻红", bool(mfails),
          "（变异体未被抓到 ⇒ 守卫空转）")

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if (n_fail or fails) else 0


if __name__ == "__main__":
    sys.exit(main())
