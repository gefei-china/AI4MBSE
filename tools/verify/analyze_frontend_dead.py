"""前端死代码分析（2026-09-18 S5）：找出 `static/js/mods/*.js` 中「定义了但全站无引用」的顶层函数。

判定法（保守）：
1. 语料 = static/index.html + static/js/**/*.js + static/css/**/*.css
   （排除 vendor/lib/第三方 min、.bak、skill_packages、uploads、_archive、tmp、docs）
2. 顶层定义 = 行首 `function NAME(` 或 `const NAME = (…)=>` / `const NAME = function`
3. 用 `\\bNAME\\b` 在全语料计数；== 1（只有定义处）→ 死代码候选
4. 附加护栏：若 NAME 还以「长标识符子串」形式出现（如 xxxNAMEyyy / NAME_suffix），
   说明可能是动态拼接或别名，**不算死代码**，单独列出供人工判断
5. 动态调用风险：若存在 `window[` / `globalThis[` / `eval(` / `new Function(`，把所有候选
   额外标注为「需人工确认」，不做自动删除依据

输出：TSV 到 tools/verify/_fe_dead.tsv + 控制台摘要
"""
import os
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODS = ROOT / "static" / "js" / "mods"
OUT = ROOT / "tools" / "verify" / "_fe_dead.tsv"

EXCLUDE_PARTS = ("/vendor/", "/lib/", "/skill_packages/", "/uploads/", "/_archive/",
                 "/node_modules/", "/.venv/", "/backups/", "/docs/", "/screenshots/", "/outputs/")
CORPUS_EXT = {".js", ".html", ".css"}


def corpus_files():
    files = []
    idx = ROOT / "static" / "index.html"
    if idx.exists():
        files.append(idx)
    for base in (ROOT / "static",):
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in CORPUS_EXT:
                continue
            s = str(p).replace("\\", "/")
            if any(x in s for x in EXCLUDE_PARTS) or s.endswith((".bak", ".bak2")):
                continue
            # 2026-09-18 修正：原写法 `"min.js" in p.name` 会把 `31-admin.js` 误判为压缩文件
            # （"ad**min.js**" 含该子串）而整文件排除 → 该文件的 51 条候选全是假阳性。
            # 必须用后缀判断。
            if p.name.endswith(".min.js"):
                continue
            files.append(p)
    return files


def read(p):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


FUNC_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
CONST_FN_RE = re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M)
CONST_FUNCTION_RE = re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function", re.M)

files = corpus_files()
texts = {p: read(p) for p in files}
whole = "\n".join(texts.values())
DYN = bool(re.search(r"window\[|globalThis\[|\beval\s*\(|new\s+Function", whole))

rows = []
for js in sorted(MODS.glob("*.js")):
    src = read(js)
    lines = src.splitlines()
    defs = []
    for m in FUNC_RE.finditer(src):
        defs.append((m.group(1), src[:m.start()].count("\n") + 1, "function"))
    for rx in (CONST_FN_RE, CONST_FUNCTION_RE):
        for m in rx.finditer(src):
            defs.append((m.group(1), src[:m.start()].count("\n") + 1, "const-fn"))

    for name, line, kind in defs:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        hits = len(pat.findall(whole))
        if hits > 1:
            continue
        # 长标识符子串护栏
        sub = re.compile(r"[\w$]" + re.escape(name) + r"|" + re.escape(name) + r"[\w$]")
        substring_hits = len(sub.findall(whole))
        # 该行原文（便于人工核对）
        txt = lines[line - 1].strip()[:100] if line - 1 < len(lines) else ""
        rows.append((js.name, line, name, kind, hits, substring_hits, txt))

with OUT.open("w", encoding="utf-8") as f:
    f.write("file\tline\tname\tkind\trefs\tsubstring_hits\tdefinition\n")
    for r in rows:
        f.write("\t".join(str(x) for x in r) + "\n")

print("语料文件数:", len(files), "| 存在动态调用模式(window[/eval/new Function):", DYN)
print("死代码候选总数:", len(rows))
print("其中含「长标识符子串」需人工确认的:", sum(1 for r in rows if r[5] > 1))
by_file = {}
for r in rows:
    by_file.setdefault(r[0], []).append(r)
print("\n按文件分布（候选数降序）：")
for fn, rs in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
    print("  %-26s %3d   %s" % (fn, len(rs), ", ".join(x[2] for x in rs[:6]) + ("…" if len(rs) > 6 else "")))
print("\n明细见:", OUT)
