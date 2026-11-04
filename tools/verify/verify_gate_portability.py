# -*- coding: utf-8 -*-
"""门禁：**pr 层可移植性** —— 这些脚本在"全新克隆 + 空库"里必须能跑。

── 为什么要有这个门禁（2026-11-05 CI 实测）────────────────────────────────
CI 终于逐步执行之后（252 步全跑完），37 条红里有 8 条与门禁逻辑**毫无关系**：

| 形态 | 实例 | CI 上的报错 |
|---|---|---|
| 写死本机绝对路径 | 7 个门禁 `os.chdir("C:/Users/<user>/...")` | `FileNotFoundError` |
| 平台特定解释器 | `.venv/Scripts/python.exe`（Windows 布局） | `No such file or directory` |
| 读未入库的配置 | `data/modeling_nodes.json` 被 `data/*` 忽略 | `FileNotFoundError` |
| 回溯 git 历史 | `git show <sha>` + 默认浅克隆 | `invalid object name` |

这类红的特性是**开发机永远绿**（目录、venv、配置、全历史都在），
所以三年都没人发现 —— 直到 CI 不再"跑到第 8 条就停"。
⇒ 判据必须从「开发机绿」换成「**全新克隆绿**」。本门禁是这条判据的静态部分
（动态部分由 gate_runner 在克隆里跑 pr 层来证明）。

── 本门禁守什么 ─────────────────────────────────────────────────────────
  P1  pr 层脚本里**真正参与取值**的字符串不含本机绝对路径
      （`C:\\Users\\<name>`、`/home/<name>/`、`/Users/<name>/`）
  P2  pr 层脚本不写平台特定解释器路径（`.venv/Scripts/python`、`.venv/bin/python`）
  P3  pr 层脚本引用的 `data/**` 文件必须**已被 git 跟踪**（否则克隆里没有）
  P4  判据自证：合成样本必须一绿一红（防判据写成恒真/恒假）

── 为什么按 AST 而不是 grep ─────────────────────────────────────────────
本仓大量注释/文档字符串写着
    `python .venv\\Scripts\\python.exe tools/verify/x.py`
那只是"给人看的用法说明"，不是平台依赖。第一版用 grep 扫出 69 个命中，
剥注释后仍有 69 个（docstring 未被剥）⇒ **假阳性会把这个门禁变成噪音**，
而噪音门禁的下场是被加 `# CI-OPTIONAL` 关掉。故：
  ① 用 `ast` 取 Constant 字符串节点；
  ② 剔除所有 docstring 与"裸字符串表达式语句"（`ast.Expr` 的 value 是串）；
  ③ 只对**剩下真正参与取值**的字符串做判定。
"""
import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(ROOT, "tools", "gates_manifest.json")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append((name, detail))
        print("  FAIL %s  <- %s" % (name, detail))


# 本机绝对路径：三段都要能匹配，但**排除占位写法**（`<user>`）——
# 本文件与其它门禁的注释里会出现 `C:\Users\<user>` 这种示例
HOME_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]Users[\\/](?!<)[^\\/\s\"']+|/home/(?!<)[a-z0-9_]+|/Users/(?!<)[^/\s\"']+)"
)
VENV_PY = re.compile(r"\.venv[\\/](?:Scripts[\\/]python|bin[\\/]python)")
DATA_REF = re.compile(r"^data[\\/][^\\/]+\.(?:json|jsonl|csv|txt|md)$")


def str_constants(tree):
    """真正参与取值的字符串常量（剔除 docstring 与裸字符串语句）。"""
    skip = set()
    for node in ast.walk(tree):
        # 所有「裸字符串表达式语句」—— 含模块/类/函数的 docstring
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            skip.add(id(node.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                out.append((node.lineno, node.value))
    return out


def load_pr():
    with open(MANIFEST, encoding="utf-8") as f:
        m = json.load(f)
    return sorted(g["script"] for g in m.get("gates") or [] if g.get("tier") == "pr")


def tracked_files():
    r = subprocess.run(["git", "ls-files"], cwd=ROOT,
                       capture_output=True, text=True)
    return {x.replace("/", os.sep) for x in r.stdout.split("\n") if x.strip()}


def scan(pr, tracked):
    """返回三类问题清单。纯函数式，便于 P4 用合成样本自证。"""
    home_hits, venv_hits, data_missing = [], [], []
    for s in pr:
        if s == os.path.basename(__file__):
            continue                      # 判据自身含模式串，自排除（同 verify_gate_manifest G8）
        p = os.path.join(ROOT, "tools", "verify", s)
        try:
            src = open(p, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
        except Exception as e:            # noqa: BLE001
            home_hits.append("%s(无法解析: %s)" % (s, e))
            continue
        for ln, val in str_constants(tree):
            if HOME_PATH.search(val):
                home_hits.append("%s:%d %r" % (s, ln, val[:60]))
            if VENV_PY.search(val):
                venv_hits.append("%s:%d %r" % (s, ln, val[:60]))
            v = val.strip().replace("\\", "/")
            if DATA_REF.match(v) and v.replace("/", os.sep) not in tracked:
                data_missing.append("%s:%d %s" % (s, ln, v))
    return home_hits, venv_hits, data_missing


print("=" * 72)
print("门禁：pr 层可移植性（全新克隆 + 空库必须能跑）")
print("=" * 72)

pr = load_pr()
print("  pr 层脚本 %d 个\n" % len(pr))

tr = tracked_files()
home_hits, venv_hits, data_missing = scan(pr, tr)

check("P1 pr 层脚本不含本机绝对路径（%d 个脚本）" % len(pr), not home_hits,
      "命中 %d 处: %s" % (len(home_hits), home_hits[:5]))
check("P2 pr 层脚本不使用平台特定解释器路径", not venv_hits,
      "命中 %d 处: %s" % (len(venv_hits), venv_hits[:5]))
check("P3 pr 层脚本引用的 data/** 文件都已被 git 跟踪", not data_missing,
      "未跟踪 %d 处: %s（克隆里不存在 ⇒ CI 恒红；请入库或改成运行时生成）"
      % (len(data_missing), data_missing[:5]))

# ── P4 判据自证：合成样本一绿一红 ────────────────────────────────────────
# 用真实字符串 + ast.parse 走同一条判定路径，证明 P1/P2 不是恒真。
_syn_bad = ast.parse(
    "_R = 'C:/Users/someone/proj'\n"
    "_PY = '.venv/Scripts/python.exe'\n"
)
_syn_ok = ast.parse(
    '"""文档里写 .venv/Scripts/python.exe 是本机用法，不算依赖。"""\n'
    "# 注释 C:/Users/someone/proj 也不算\n"
    "_R = os.path.dirname(os.path.abspath(__file__))\n"
    "_PY = sys.executable\n"
)
_syn_doc = ast.parse('"""C:/Users/someone/x 只在文档里"""\n_v = 1\n')


def _probe(tree):
    h = v = 0
    for _ln, val in str_constants(tree):
        if HOME_PATH.search(val):
            h += 1
        if VENV_PY.search(val):
            v += 1
    return h, v


_bh, _bv = _probe(_syn_bad)
_gh, _gv = _probe(_syn_ok)
_dh, _dv = _probe(_syn_doc)
check("P4a 判据自证：本机路径样本被判红、干净样本判绿",
      _bh == 1 and _bv == 1 and _gh == 0 and _gv == 0,
      "bad=(%d,%d) good=(%d,%d)" % (_bh, _bv, _gh, _gv))
check("P4b 判据自证：docstring 里的路径不算依赖（不误报）",
      _dh == 0 and _dv == 0, "doc=(%d,%d)" % (_dh, _dv))

print("\n" + "=" * 72)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
print("=" * 72)
sys.exit(1 if FAIL else 0)
