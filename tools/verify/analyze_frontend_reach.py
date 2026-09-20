# -*- coding: utf-8 -*-
"""前端死代码可达性分析（S5 复检增强版，2026-09-18）。

与 analyze_frontend_dead.py 的差别：
  旧法 = 「全语料 `\\bNAME\\b` 计数 == 1」→ 只能发现**直接**无引用者，
        且会把「只被另一个死函数引用」的函数误判为活代码（漏报连锁死代码）。
  本法 = 从**真实入口**出发做调用图传递闭包（可达性）：
        入口 = ① index.html 全文出现的函数名（内联 on* 处理器 + script）
               ② 各 js 文件的**顶层代码**（不在任何函数体内的部分，加载即执行）
               ③ 非 mods 的其它 js 文件全文
               ④ `window.NAME =` / `window['NAME'] =` 显式暴露
        种子之外、调用图闭包之外的顶层函数 = 死代码候选（含连锁）。

输出：可复现的不可达清单（含行号）+ 与旧法清单的差异。
用法：python tools/verify/analyze_frontend_reach.py [--json out.json]
      # 仓库根默认取脚本上两级；可用环境变量 FE_ROOT 覆盖（便于把脚本拷到
      # 别处、对 HEAD worktree / 备份树做 A-B 对照时复用同一份分析逻辑）。
"""
import json
import os
import pathlib
import re
import sys
from collections import Counter

_env_root = os.environ.get("FE_ROOT")
ROOT = pathlib.Path(_env_root).resolve() if _env_root else pathlib.Path(__file__).resolve().parents[2]
MODS = ROOT / "static" / "js" / "mods"
STATIC = ROOT / "static"
EXCLUDE_PARTS = ("/vendor/", "/lib/", "/skill_packages/", "/uploads/", "/_archive/",
                 "/node_modules/", "/.venv/", "/backups/", "/docs/", "/screenshots/",
                 "/outputs/")

FUNC_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
CONST_FN_RE = re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M)
CONST_FUNCTION_RE = re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function", re.M)
IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def idents(text):
    """语料 → 标识符集合（单遍词元化）。

    2026-09-18 性能修正（重要）：原实现是「对 1400+ 个函数名各跑一次
    `re.search(r"\\bNAME\\b", 全仓语料)`」，即 1400+ × 4.7MB ≈ 6~7GB 字符扫描，
    实测在 Windows 上跑到分钟级被环境超时 SIGTERM 杀掉（表现为「命令无输出直接退出」，
    极易误判为脚本崩溃/沙箱拦截）。改为语料单遍词元化后取交集，语义等价（这些名字
    都是普通标识符，token 边界与 \\b 一致），耗时降到毫秒级。
    """
    return set(IDENT_RE.findall(text))


def read(p):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def scan_structure(src, mark_at):
    """单遍 JS 感知扫描：返回 (owner_of_line, ends)。

    owner_of_line[i] = 覆盖第 i 行的「顶层定义名」；None 表示该行属**顶层非定义代码**
    （加载即执行 / 注册回调），其中的标识符一律算入口种子。
    ends[name] = 该定义 `{}` 块闭合所在行（0-based，含）。

    为什么必须这么算（2026-09-18 事故复盘）：
      旧实现用「各定义 mark 行之间的空隙」近似顶层代码，而定义 end 被记成
      「下一个定义的行号」→ 空隙恒为空 → 顶层代码几乎全漏采。后果有两类活函数被判死：
        ① 尾随在最后一个定义之后的顶层代码：`window.toast = Object.assign(_toastFn,{...})`
           → `_toastFn` 被判死，删掉后全站 toast() 抛 not a function；
        ② 顶层语句内部的**缩进**代码：`document.addEventListener('click', e => { ... closeToolPop(); })`
           → `closeToolPop` 被判死，删掉后每次点击都会 ReferenceError（弹出层关不掉）。
      本实现按 {} 深度精确划分：深度 0 = 顶层；顶层语句内部（无论缩进）也算顶层代码。
      字符串 / 模板串（含 ${} 嵌套，**支持模板串里再套模板串**）/ 行注释 / 块注释 /
      正则字面量全部跳过，避免括号计数错乱（正则字面量是本项目踩过的坑：
      `/'/g` 会被当成字符串起点；嵌套模板串是本项目 `10-chatinput.js` 的重度用法：
      `` `${a.is_image?`<img src="${x}">`:'&#128196;'}` ``，朴素实现会在这里深度漂移）。
    """
    n = len(src)
    # 帧栈：('block', owner) 普通 {} / ('tmpl',) 模板串文本态 / ('tmplsub',) ${ 进入代码态
    frames = []
    owners = []                # 每行行首的 owner
    ends = {}
    line = 0
    i = 0
    pending_mark = None        # 本行若是定义行（列 0），记其名字
    prev_sig = ""

    def cur_block_owner():
        for f in reversed(frames):
            if f[0] == "block":
                return f[1]
        return None

    def n_blocks():
        return sum(1 for f in frames if f[0] == "block")

    def in_template():
        return bool(frames) and frames[-1][0] == "tmpl"

    def advance(j):
        """消费到下标 j（不含）；沿途每个换行登记下一行的 owner。"""
        nonlocal i, line, pending_mark
        j = min(j, n)
        while i < j:
            if src[i] == "\n":
                line += 1
                if line in mark_at:
                    pending_mark = mark_at[line]
                    owners.append(pending_mark)
                else:
                    pending_mark = None
                    owners.append(cur_block_owner())
            i += 1

    # 第 0 行
    if 0 in mark_at:
        pending_mark = mark_at[0]
        owners.append(pending_mark)
    else:
        owners.append(None)

    while i < n:
        c = src[i]
        # ── 模板串文本态：只找 ${ 与结尾反引号，其余原样跳过 ──
        if in_template():
            if c == "\\":
                advance(i + 2)
                continue
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                frames.append(("tmplsub",))
                advance(i + 2)
                continue
            if c == "`":
                frames.pop()
                advance(i + 1)
                prev_sig = "x"
                continue
            advance(i + 1)
            continue
        # ── 代码态 ──
        if c == "/" and i + 1 < n and src[i + 1] == "/":            # 行注释
            j = src.find("\n", i)
            advance(n if j == -1 else j)
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":            # 块注释
            j = src.find("*/", i + 2)
            advance(n if j == -1 else j + 2)
            continue
        if c in "\"'":                                             # 普通字符串
            q = c
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q:
                    break
                j += 1
            advance(j + 1)
            prev_sig = "x"
            continue
        if c == "`":                                               # 模板串开始
            frames.append(("tmpl",))
            advance(i + 1)
            continue
        if c == "/" and prev_sig not in ")]}" and not (
                prev_sig.isalnum() or prev_sig in "_$"):           # 正则字面量
            j = i + 1
            incls = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    incls = True
                elif ch == "]":
                    incls = False
                elif ch == "/" and not incls:
                    break
                elif ch == "\n":
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            advance(j)
            prev_sig = "x"
            continue
        if c == "{":
            owner = pending_mark if n_blocks() == 0 else cur_block_owner()
            frames.append(("block", owner))
            pending_mark = None
            prev_sig = c
            advance(i + 1)
            continue
        if c == "}":
            while frames and frames[-1][0] == "tmpl":
                frames.pop()
            if frames:
                f = frames.pop()
                if f[0] == "block" and f[1] and n_blocks() == 0:
                    ends[f[1]] = line
            prev_sig = c
            advance(i + 1)
            continue
        if not c.isspace():
            prev_sig = c
        advance(i + 1)
    while len(owners) <= line:
        owners.append(None)
    return owners, ends


def build_file_model(js):
    """解析单个 mods 文件 → (defs_of_file, top_text)。

    defs_of_file: [(name, start0, end0)]，均为 0-based 行号（end 含）。
    top_text: 顶层非定义代码的全文（用于取入口种子）。
    """
    src = read(js)
    lines = src.splitlines()
    marks = []
    for rx in (FUNC_RE, CONST_FN_RE, CONST_FUNCTION_RE):
        for m in rx.finditer(src):
            marks.append((m.group(1), src[:m.start()].count("\n")))
    marks.sort(key=lambda kv: kv[1])
    mark_at = {}
    for name, ln in marks:
        mark_at.setdefault(ln, name)
    owners, ends = scan_structure(src, mark_at)
    out = []
    for i, (name, ln) in enumerate(marks):
        if name in ends:
            end = ends[name]
        elif i + 1 < len(marks):
            end = marks[i + 1][1] - 1          # 无 {} 块（单表达式箭头函数）：退化为下一个定义之前
        else:
            end = len(lines) - 1
        if end < ln:
            end = ln
        out.append((name, ln, end))
    top_text = "\n".join(lines[i] for i in range(len(lines))
                         if i < len(owners) and owners[i] is None)
    return out, top_text


def collect_js_files():
    out = []
    for p in STATIC.rglob("*.js"):
        s = str(p).replace("\\", "/")
        if any(x in s for x in EXCLUDE_PARTS) or p.name.endswith(".min.js"):
            continue
        out.append(p)
    return sorted(out)


mods_files = sorted(MODS.glob("*.js"))
other_js = [p for p in collect_js_files() if p.parent != MODS]

# ── 1) 建函数表：name -> (file, start_line, end_line, body) + 各文件顶层代码 ──
defs = {}
file_top = {}
for js in mods_files:
    items, top_text = build_file_model(js)
    lines = read(js).splitlines()
    for name, ln, end in items:
        defs[name] = {
            "file": js.name,
            "line": ln + 1,
            "end": end + 1,
            "body": "\n".join(lines[ln:end + 1]),
        }
    file_top[js.name] = top_text

all_names = set(defs)

# ── 2) 入口种子 ──
seeds = set()
idx = STATIC / "index.html"
idx_text = read(idx)
seeds |= idents(idx_text) & all_names

# 顶层代码（不属任何被跟踪函数的部分 = 加载即执行 / 注册回调）
for js in mods_files:
    seeds |= idents(file_top[js.name]) & all_names

# 非 mods 的其它 js（含 static/js/*.js 顶层模块）
for js in other_js:
    seeds |= idents(read(js)) & all_names

# window 显式暴露
for js in mods_files + other_js:
    for m in re.finditer(r"window\.([A-Za-z_$][\w$]*)\s*=", read(js)):
        if m.group(1) in all_names:
            seeds.add(m.group(1))

# Python 侧引用（保守方向）：后端可能把含 on* 处理器的 HTML 片段拼进响应，
# 此时前端函数名只出现在 .py 里，若不采集会误判为死代码。
#
# ⚠️ 已知局限（2026-09-18 实测）：这是**字面量**匹配，.py 里的注释/文档字符串同样算引用。
#    实测踩过：prune_dead_frontend.py 的一句注释写出了某前端常量的名字，
#    该常量就在分析结果里"复活"，导致两棵树可达数差 1、集合对拍不自洽。
#    规则：**仓库内的 .py 注释里不要写前端函数/常量标识符**；对拍出现 ±1 时先查这个。
py_files = [p for p in ROOT.rglob("*.py")
            if "__pycache__" not in str(p)
            and not any(x in str(p).replace("\\", "/")
                        for x in ("/.venv/", "/node_modules/", "/_archive/", "/backups/", "/tmp/"))]
py_text = "\n".join(read(p) for p in py_files)
seeds |= idents(py_text) & all_names

# ── 3) BFS 传递闭包 ──
reach = set()
stack = [n for n in seeds if n in all_names]
while stack:
    n = stack.pop()
    if n in reach:
        continue
    reach.add(n)
    for c in idents(defs[n]["body"]) & all_names:
        if c not in reach:
            stack.append(c)

dead = sorted(all_names - reach)

# ── 4) 旧法清单（引用计数==1）用于对比 ──
whole = "\n".join(read(p) for p in mods_files)
whole += "\n" + idx_text
for js in other_js:
    whole += "\n" + read(js)
counts = Counter(IDENT_RE.findall(whole))
old_dead = {n for n in all_names if counts.get(n, 0) == 1}

# 未跟踪的旧 tsv（若存在）用于交叉核对
tsv = ROOT / "tools" / "verify" / "_fe_dead.tsv"
tsv_names = set()
if tsv.exists():
    for line in read(tsv).splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3:
            tsv_names.add(parts[2])

by_file = {}
for n in dead:
    by_file.setdefault(defs[n]["file"], []).append((defs[n]["line"], n))

print(f"mods 顶层函数总数: {len(all_names)}")
print(f"入口种子数: {len(seeds)}")
print(f"可达: {len(reach)}  |  不可达（死代码）: {len(dead)}")
print(f"旧法（引用计数==1）候选: {len(old_dead)}")
print(f"旧 tsv 候选: {len(tsv_names)}")
print(f"\n新法独有（旧法漏报的连锁死代码）: {len(set(dead) - old_dead)}")
print("  " + ", ".join(sorted(set(dead) - old_dead))[:1800])
print(f"\n旧法独有（新法判为可达，即被入口/顶层/其它 js 引用）: {len(old_dead - set(dead))}")
print("  " + ", ".join(sorted(old_dead - set(dead))))

print("\n=== 不可达函数按文件分布 ===")
for fn in sorted(by_file, key=lambda k: -len(by_file[k])):
    rows = sorted(by_file[fn])
    print(f"--- {fn} ({len(rows)}) ---")
    for ln, n in rows:
        print(f"    L{ln:<6} {n}")

if "--dump-top" in sys.argv:
    # 调试用：打印指定 mods 文件的「顶层代码」全文，用于核对某函数是否被正确采为种子。
    _f = sys.argv[sys.argv.index("--dump-top") + 1]
    print("=" * 70)
    print("TOP TEXT OF", _f)
    print("=" * 70)
    print(file_top.get(_f, "<无此文件>"))
    print("=" * 70)
    for _n in sorted(all_names):
        if _n in file_top.get(_f, "") and _f in (defs[_n]["file"],):
            print("  [顶层出现] %s" % _n)
    sys.exit(0)

if "--json" in sys.argv:
    out = sys.argv[sys.argv.index("--json") + 1]
    pathlib.Path(out).write_text(json.dumps({
        "total": len(all_names), "reach": len(reach), "dead": dead,
        "old_dead": sorted(old_dead), "tsv_names": sorted(tsv_names),
        "by_file": {k: [[ln, n] for ln, n in v] for k, v in by_file.items()},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nJSON ->", out)
