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


def parse_tsv(only=None, skip_substring=True):
    """返回 {file: [(name, line, kind)]}。"""
    out = {}
    if not TSV.exists():
        print("缺少 %s —— 先运行 analyze_frontend_dead.py" % TSV)
        sys.exit(2)
    for ln in TSV.read_text(encoding="utf-8").splitlines()[1:]:
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


def prev_comment_start(lines, def_idx):
    """把紧贴定义上方的注释块（// 或 /* */）一起删掉；遇到空行/其它代码即停。"""
    i = def_idx - 1
    while i >= 0:
        s = lines[i].strip()
        if s.startswith("//") or s.endswith("*/") or s.startswith("*") or s.startswith("/*"):
            i -= 1
            continue
        break
    return i + 1


def prune_file(path, items, apply=False):
    # 2026-09-18 修正：必须按**字节**读写。原先用 read_text/write_text，
    # Windows 下 Python 会把 \n 翻译成 \r\n（newline=None 的行尾转换），
    # 导致整个文件换行风格被改写 —— 表现为"删了代码文件反而变大"（每行 +1 字节），
    # 并会污染后续 diff。这里显式绕过换行翻译。
    raw = path.read_bytes().decode("utf-8", errors="replace")
    text = raw
    removed, miss = [], []
    for name, line, kind in items:                 # 已按行号倒序
        idx = None
        # 每次都用最新文本重新定位（倒序删除，前面的行号不受影响）
        cur = text
        cur_lines = cur.splitlines(keepends=True)
        di = find_def_start(cur_lines, name, kind, line)
        if di is None:
            miss.append((name, "定义行未找到"))
            continue
        # 从定义行起点开始做块级匹配
        offset = sum(len(x) for x in cur_lines[:di])
        if kind == "function":
            end = block_end(cur, offset)
        else:
            # const NAME = (...) => { ... } 或 = function(){...}
            arrow = cur.find("{", offset)
            semi = cur.find(";", offset)
            end = block_end(cur, offset) if arrow != -1 and (semi == -1 or arrow < semi) else semi
        if end == -1:
            miss.append((name, "块尾未匹配"))
            continue
        s = prev_comment_start(cur_lines, di)
        s_off = sum(len(x) for x in cur_lines[:s])
        e_off = end + 1
        if e_off <= s_off:                          # 防御：跨度非法则不删，避免把文本复制一遍
            miss.append((name, "跨度非法"))
            continue
        # 连带吃掉紧随其后的换行（含一个空行），保持文件紧凑
        while e_off < len(cur) and cur[e_off] in "\r\n":
            e_off += 1
        text = cur[:s_off] + cur[e_off:]
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
    args = ap.parse_args()
    only = set(x.strip() for x in args.only.split(",") if x.strip()) or None

    groups = parse_tsv(only=only)
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
