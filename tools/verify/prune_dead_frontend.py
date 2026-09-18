"""前端死代码精确切除（2026-09-18 S5）。

依据 `analyze_frontend_dead.py` 产出的 `_fe_dead.tsv`（refs==1 且无长标识符子串），
对 `static/js/mods/*.js` 做**函数级整块切除**（含其上方紧邻的注释与下方空行），
切除后由调用方跑 `node --check` + 浏览器冒烟验证。

安全性设计：
- 必须传 `--apply` 才写盘；默认 dry-run 只报告。
- 支持 `--only a.js,b.js` 限定文件，便于分批。
- 花括号配平用状态机（正确处理单/双引号、模板串与 ${} 嵌套、行注释、块注释），
  不依赖朴素计数 —— 本项目模板串里大量出现 `{}`，朴素计数必然切错。
- 文件级备份：首次改写前把原文件复制为 `<name>.pres5`（与 offload 全量备份互为兜底）。
- 每个文件改完打印「删除函数数 / 字节变化 / 剩余顶层 function 数」。
"""
import argparse
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODS = ROOT / "static" / "js" / "mods"
TSV = ROOT / "tools" / "verify" / "_fe_dead.tsv"


def parse_tsv(only=None, skip_substring=True, tsv=None):
    """返回 {file: [(name, line, kind)]}。

    tsv 参数（2026-09-18 新增）：可指向 `analyze_frontend_reach.py` 产出的清单，
    以纳入「连锁死代码」（旧法 refs==1 会漏掉「只被死函数引用」的那一批）。
    """
    out = {}
    TSVP = pathlib.Path(tsv) if tsv else TSV
    if not TSVP.exists():
        print("缺少 %s —— 先运行 analyze_frontend_dead.py（或 --tsv 指定可达性清单）" % TSVP)
        sys.exit(2)
    for ln in TSVP.read_text(encoding="utf-8").splitlines()[1:]:
        parts = ln.split("\t")
        if len(parts) < 7:
            continue
        f, line, name, kind, refs, sub, _def = parts[:7]
        if int(refs) != 1:
            continue
        if skip_substring and int(sub) > 0:      # 含长标识符子串 → 人工确认，不自动删
            continue
        if only and f not in only:
            continue
        out.setdefault(f, []).append((name, int(line), kind))
    for f in out:
        out[f].sort(key=lambda x: -x[1])          # 从后往前删，避免行号漂移
    return out


def find_def_start(lines, name, kind, near_line):
    """在 near_line 附近定位定义行（返回 0-based index）。"""
    if kind == "function":
        pat = re.compile(r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(")
    else:
        pat = re.compile(r"^const\s+" + re.escape(name) + r"\s*=")
    lo = max(0, near_line - 6)
    hi = min(len(lines), near_line + 6)
    for i in range(lo, hi):
        if pat.match(lines[i]):
            return i
    return None


def block_end(text, start_idx):
    """从 start_idx（'{' 之前）开始，用状态机找到匹配的结束 '}'，返回其下标（含）。"""
    depth = 0
    i = start_idx
    n = len(text)
    started = False
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":       # 行注释
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":       # 块注释
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c in "\"'":                                          # 普通字符串
            q = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    break
                i += 1
            i += 1
            continue
        if c == "`":                                            # 模板串（含 ${} 嵌套）
            i += 1
            tdepth = 0
            while i < n:
                ch = text[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "$" and i + 1 < n and text[i + 1] == "{":
                    tdepth += 1
                    i += 2
                    continue
                if tdepth and ch == "}":
                    tdepth -= 1
                    i += 1
                    continue
                if ch == "`" and tdepth == 0:
                    break
                i += 1
            i += 1
            continue
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return i
        i += 1
    return -1


TOP_DEF_RE = re.compile(
    r"^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(|^const\s+[A-Za-z_$][\w$]*\s*=")


def top_def_starts(lines):
    """顶层定义行下标（function 或 const 赋值）。"""
    return [i for i, l in enumerate(lines) if TOP_DEF_RE.match(l)]


def comment_block_start(lines, di):
    """返回从 di 往上紧邻的注释块起始行下标。

    2026-09-18 修正：原 prev_comment_start 对**多行块注释**判据不足
    （中间行既不以 * 也不以 /* 开头时提前 break），会把注释截断。
    这里遇到 `*/` 结尾就回溯到该注释的 `/*` 起始行。
    """
    i = di - 1
    while i >= 0:
        s = lines[i].strip()
        if s.startswith("//"):
            i -= 1
            continue
        if s.endswith("*/"):
            j = i
            while j >= 0 and "/*" not in lines[j]:
                j -= 1
            i = j - 1 if j >= 0 else -1
            continue
        if s.startswith("/*"):
            i -= 1
            continue
        break
    return i + 1


def prune_file(path, items, apply=False):
    # 2026-09-18 修正：必须按**字节**读写。原先用 read_text/write_text，
    # Windows 下 Python 会把 \n 翻译成 \r\n（newline=None 的行尾转换），
    # 导致整个文件换行风格被改写 —— 表现为"删了代码文件反而变大"（每行 +1 字节），
    # 并会污染后续 diff。这里显式绕过换行翻译。
    #
    # 2026-09-18 二次修正（定位法重写）：原先用花括号状态机 block_end 找函数尾，
    # 但该状态机**不识别正则字面量** —— 遇到属性转义常量里的 `/'/g`（21-ontology.js）
    # 或函数体内的正则时，会把 `'` 当作字符串起始，吞掉后续内容导致括号计数错乱 → 切错位置，
    # 实测已造成 21-ontology.js、25-graphview.js **语法损坏**（已 git checkout 回滚）。
    # 改为「下一个顶层定义行」定界：不解析 JS 语法，天然免疫正则/模板串/注释干扰。
    #
    # 注意（2026-09-18 三次修正）：上一段注释原本**写出了那个常量的标识符**，
    # 而 analyze_frontend_reach.py 会把 .py 全文当「前端引用来源」采样 →
    # 一句注释就把该常量在可达性分析里"复活"了（实测导致 live/HEAD 可达数差 1）。
    # 因此本工具注释里**不要出现任何前端函数/常量标识符**，必要时用描述性说法代替。
    text = path.read_bytes().decode("utf-8", errors="replace")
    removed, miss = [], []
    for name, line, kind in items:                 # 已按行号倒序
        lines = text.splitlines(keepends=True)
        plain = [l.rstrip("\r\n") for l in lines]
        starts = top_def_starts(plain)
        di = None
        for cand in starts:                        # 在报备行号附近定位定义行
            if abs(cand + 1 - line) <= 8 and re.search(r"\b" + re.escape(name) + r"\b", plain[cand]):
                di = cand
                break
        if di is None:
            miss.append((name, "定义行未找到"))
            continue
        nxt = next((s for s in starts if s > di), None)
        # 结束位置 = 下一个顶层函数「文档注释」之前（保住它的注释）
        end = comment_block_start(lines, nxt) if nxt is not None else len(lines)
        s = comment_block_start(lines, di)          # 一起吃掉本函数上方注释
        if end <= s:                               # 防御：跨度非法则不删
            miss.append((name, "跨度非法"))
            continue
        text = "".join(lines[:s]) + "".join(lines[end:])
        removed.append(name)

    if apply and removed:
        bak = path.with_suffix(path.suffix + ".pres5")
        if not bak.exists():
            shutil.copy2(path, bak)
        path.write_bytes(text.encode("utf-8"))
    return removed, miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--tsv", default="", help="清单路径（默认 _fe_dead.tsv；可指向可达性清单）")
    args = ap.parse_args()
    only = set(x.strip() for x in args.only.split(",") if x.strip()) or None

    groups = parse_tsv(only=only, tsv=args.tsv or None)
    if not groups:
        print("无候选（检查 _fe_dead.tsv / --only）")
        return
    total_r = total_m = 0
    for fname, items in sorted(groups.items()):
        path = MODS / fname
        if not path.exists():
            print("跳过（文件不存在）:", fname)
            continue
        before = path.stat().st_size
        removed, miss = prune_file(path, items, apply=args.apply)
        after = path.stat().st_size if args.apply else before
        total_r += len(removed)
        total_m += len(miss)
        print("%-24s 候选 %2d  删除 %2d  %s%s" % (
            fname, len(items), len(removed),
            ("字节 %d → %d (-%d)" % (before, after, before - after)) if args.apply else "(dry-run)",
            ("  未命中: " + ", ".join(n for n, _ in miss)) if miss else ""))
    print("\n合计删除 %d 个函数，未命中 %d 个；模式=%s" % (total_r, total_m, "APPLY" if args.apply else "DRY-RUN"))


if __name__ == "__main__":
    main()
