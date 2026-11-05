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
import glob
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


# ══════════════════════════════════════════════════════════════════════════
# P5 全仓（tools/ + tests/ + 应用包）不得出现本机绝对路径
# ══════════════════════════════════════════════════════════════════════════
# 为什么从「只扫 pr 层」扩到「全仓」（2026-11-05）：
#   ① 仓库是**公开的** —— `C:\Users\<name>\...` 把本机用户名与完整目录结构
#      写进每个提交，属隐私泄露，与"这层跑不跑 CI"无关；
#   ② pr 层之外的脚本（`tools/reembed_chunks.py`、`tests/manual_verify/*`）
#      正是"门禁红了怎么查"的诊断工具 —— 换机器就全废，
#      最需要它们的时候恰好不能用；
#   ③ 只扫 pr 层时它们永远无人过问（实测残留 21 处，横跨 tools/ 与 tests/）。
# P5 与 P1 的区别：P1 只查"pr 层参与取值的字符串"，P5 查**全仓任何 py 的任何字符串**。
APP_DIRS = ("tools", "tests", "agent", "core", "routers", "services",
            "database", "llm", "repositories", "knowledge_pipeline")
HOME_ABS_ANCHORED = re.compile(
    r"^[A-Za-z]:[\\/]+Users[\\/](?!<)|^/home/(?!<)[a-z0-9_]+/|^/Users/(?!<)[^/]+/", re.I)
# P6 的模式串必须定义在 scan_repo_abs **之前**（P5/P6 共用同一趟目录遍历）。
# ⚠️ 方向：真实代码形态是 `tree = ast.parse(src)` 然后 `except SyntaxError:`
#    ⇒ ast.parse 在**前**、SyntaxError 在**后**。
#    第一版把方向写反了（SyntaxError 在前）⇒ 合成自证当场抓住（P6-s FAIL），
#    而真实场景永远不命中 ⇒ **判据空转**。合成自证的价值就在这：写反了立刻红。


def scan_repo_abs():
    hits = []
    for base in APP_DIRS:
        for p in glob.glob(os.path.join(ROOT, base, "**", "*.py"), recursive=True):
            rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
            if "__pycache__" in rel or rel.endswith("verify_gate_portability.py"):
                continue                     # 判据自身含合成样本
            try:
                src = open(p, encoding="utf-8", errors="replace").read()
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for ln, val in str_constants(tree):
                if HOME_ABS_ANCHORED.match(val.strip()):
                    hits.append("%s:%d %r" % (rel, ln, val[:52]))
    return hits


_p5 = scan_repo_abs()
check("P5 全仓不得出现本机绝对路径（隐私 + 换机可用性）", not _p5,
      "命中 %d 处: %s" % (len(_p5), _p5[:6]))
# P5 判据自证：合成两例一红一绿（防判据写成恒真）
_syn_hit = 'X = r"C:' + chr(92) + 'Users' + chr(92) + 'someone' + chr(92) + 'proj"'
check("P5-s 判据自证（本机路径判红 / 相对路径判绿）",
      bool(HOME_ABS_ANCHORED.match("C:" + chr(92) + "Users" + chr(92) + "someone"))
      and not HOME_ABS_ANCHORED.match("data/x.json"),
      "自证样本异常")


# ══════════════════════════════════════════════════════════════════════════
# P6（纪律，非自动判据）语法结论必须来自 compile()，不能是 ast.parse()
# ══════════════════════════════════════════════════════════════════════════
# 2026-11-05 被咬一次：批量改写把 `import os` 插到
# `from __future__ import annotations` **之前** ——
#   · ast.parse(src)  → 通过
#   · compile(src)    → SyntaxError: from __future__ imports must occur at the
#                       beginning of the file
# ⇒ 凡"语法 0 错"的结论，都必须来自 compile()。
#
# ⚠️ 这条**不做成自动判据**（两版正则都被自证证伪后放弃）：
#   "拿 ast.parse 当语法判据"与"正常的解析容错"（ast.parse 失败 ⇒ 跳过）
#   在源码形态上几乎相同 —— 都有 `ast.parse` + `except SyntaxError`，
#   区别只在 except 之后做了什么（判红 vs 跳过），静态正则无法区分。
#   实测：v1 方向写反（SyntaxError 在前）⇒ 命中 0；v2 修正方向/缩进 ⇒ 误报 10 个
#   文件（全是正常容错写法）。误报会让门禁变噪音，而噪音门禁的下场是
#   被加豁免/被关掉 —— 等于没写。
# ⇒ 这条以**纪律**存在：docs/门禁分层-20261105.md §9 + AGENTS.md，
#    人工 review 时核对。承认"判据不可静态化"比留一个假判据诚实。

print("\n" + "=" * 72)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
print("=" * 72)
sys.exit(1 if FAIL else 0)
