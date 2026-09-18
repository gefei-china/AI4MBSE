"""删除被覆盖的同名定义（2026-09-18 S7）。

安全前提（脚本内置断言，不通过就拒绝写盘）：
  1) 目标文件里同名模块级 def/class 恰好出现 2 次以上；
  2) **被删的那份与生效的那份逐字节相同**（difflib 无差异）——即确认是复制粘贴重复，
     删除后行为必然不变。若两份实现不同，脚本报错退出，交由人工判断（可能是有意覆盖的旧实现）。

用法：
  python tools/verify/dedupe_first_def.py --file ontology_reasoning.py --name cardinality_check
  加 --apply 才写盘；默认 dry-run。
"""
import argparse
import ast
import difflib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    p = ROOT / a.file
    if not p.exists():
        print("文件不存在: %s" % p)
        return 2
    raw = p.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw[:2000] else "\n"
    lines = raw.splitlines(keepends=True)
    tree = ast.parse(raw)
    fns = [n for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == a.name]
    print("文件 %s：%s 的模块级定义 %d 处，行号 %s" % (a.file, a.name, len(fns), [f.lineno for f in fns]))
    if len(fns) < 2:
        print("无需处理（定义少于 2 处）")
        return 0

    def body(f):
        return "".join(lines[f.lineno - 1:f.end_lineno])

    first, effective = fns[0], fns[-1]
    b1, b2 = body(first), body(effective)
    same = (b1 == b2)
    d = list(difflib.unified_diff(b1.splitlines(), b2.splitlines(), lineterm="", n=0))
    print("被覆盖的那份: L%d-L%d ；生效的那份: L%d-L%d" % (first.lineno, first.end_lineno, effective.lineno, effective.end_lineno))
    print("两份是否逐字节相同: %s（diff 行数 %d）" % (same, len(d)))
    if not same:
        print("!! 两份实现不同 —— 拒绝自动删除，请人工确认哪份该保留（可能是有意覆盖）")
        for x in d[:30]:
            print("   " + x)
        return 3

    start = first.lineno - 1
    end = first.end_lineno
    while end < len(lines) and lines[end].strip() == "":   # 连带删掉紧随的空行，避免留下多行空白
        end += 1
        if end - 1 > first.end_lineno:                      # 最多多吃 2 行空白
            break
    if a.apply:
        del lines[start:end]
        p.write_bytes("".join(lines).encode("utf-8"))
        print("已删除 L%d-L%d（%d 行）；文件字节 %d → %d" % (first.lineno, end, end - start,
                                                          len(raw.encode("utf-8")), len("".join(lines).encode("utf-8"))))
        # 复核：删后同名定义应只剩 1 处
        t2 = ast.parse(p.read_bytes().decode("utf-8"))
        left = [n for n in t2.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == a.name]
        print("删后 %s 定义数: %d（应为 1）" % (a.name, len(left)))
    else:
        print("（dry-run）将删除 L%d-L%d" % (first.lineno, end))
    return 0


if __name__ == "__main__":
    sys.exit(main())
