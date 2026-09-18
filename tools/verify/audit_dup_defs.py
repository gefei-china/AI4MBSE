"""同类重复定义审计 v2（2026-09-18 S7）：用 AST 按**作用域路径**找真正的重复定义。

v1 用「缩进 + 名字」判定，把不同类里的同名方法（如 `graph_db.py` 的 Mock/Writer/Reader 各有
`write_triples`）误报为重复 —— 21 条里绝大多数是假阳性。v2 改用 ast：记录每个 def/class 的
**限定名路径**（`Class.method` / `outer.inner`），同一路径出现两次才是真正的"后者覆盖前者"。

用法：
  .venv\\Scripts\\python.exe -X utf8 tools/verify/audit_dup_defs.py
  .venv\\Scripts\\python.exe -X utf8 tools/verify/audit_dup_defs.py --all
"""
import argparse
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_DIRS = ("_archive", "backups", "tmp", "outputs", "screenshots", ".venv", "node_modules",
             "static", "data", "java-runtime", "fuseki", "docs", "__pycache__", ".git")


def iter_py(with_tests: bool):
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if any(seg in rel.split("/") for seg in SKIP_DIRS):
            continue
        if not with_tests and (rel.startswith("tests/") or rel.startswith("tools/")):
            continue
        yield p, rel


def walk(node, prefix, out):
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            path = prefix + child.name
            out.setdefault(path, []).append(child.lineno)
            walk(child, path + ".", out)
        elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            # 条件分支里的定义（如 if/else 各定义一次）也算同作用域，需一并纳入
            walk(child, prefix, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    files = list(iter_py(a.all))
    print("扫描 .py 文件: %d（%s）\n" % (len(files), "含 tests/tools" if a.all else "仅一线代码"))
    n_files = n_dups = 0
    for p, rel in sorted(files, key=lambda x: x[1]):
        try:
            tree = ast.parse(p.read_bytes().decode("utf-8", errors="replace"))
        except SyntaxError as e:
            print("!! 语法错误 %s: %s" % (rel, e))
            continue
        out = {}
        walk(tree, "", out)
        dups = {k: v for k, v in out.items() if len(v) > 1}
        if not dups:
            continue
        n_files += 1
        print("★ %s" % rel)
        for path, lines in sorted(dups.items(), key=lambda kv: kv[1][0]):
            n_dups += 1
            print("    %-40s 定义于 %s  → 生效 L%d；死代码 %s"
                  % (path, ", ".join("L%d" % x for x in lines), lines[-1],
                     ", ".join("L%d" % x for x in lines[:-1])))
    print("\n含真重复定义的文件: %d 个；重复条目: %d 条" % (n_files, n_dups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
