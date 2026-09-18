"""实现对比器（2026-09-18 S7）：判定两处同名实现到底是「真重复」还是「薄包装/委托」。

背景：只读报告容易把「路由处理器」与「其底层实现」当成重复（例如 `routers/governance.py:90`
的 `mirror_state` 只是委托 `governance.mirror_state`）。动手前必须逐对核实，本工具给出一致判定。

判定规则：
  · 两份源码逐字节相同            → SAME（真重复，可安全删其一）
  · 一份是薄包装（body 内含 import/调用另一份的名字，且自身行数很少）→ WRAPPER（不是重复，只是分层）
  · 其它                          → DIFF（打印前若干行差异，需人工判断保留哪份）

用法示例：
  python tools/verify/compare_impls.py \
      --pair "knowledge_pipeline/search.py:reindex_document" "routers/meta.py:reindex_document" \
      --pair "plugin_system/store.py:create_plugin" "routers/plugins.py:create_plugin"
"""
import argparse
import ast
import difflib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def get_fn(rel, name):
    p = ROOT / rel
    if not p.exists():
        return None
    src = p.read_bytes().decode("utf-8", errors="replace")
    L = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return {"code": "\n".join(L[n.lineno - 1:n.end_lineno]), "rel": rel,
                    "line": n.lineno, "end": n.end_lineno}
    return None


def strip_decorators(code):
    return "\n".join(x for x in code.splitlines() if not x.strip().startswith("@"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, action="append", required=True,
                    metavar=("A:func", "B:func"))
    a = ap.parse_args()
    for spec_a, spec_b in a.pair:
        ra, na = spec_a.rsplit(":", 1)
        rb, nb = spec_b.rsplit(":", 1)
        fa, fb = get_fn(ra, na), get_fn(rb, nb)
        print("=" * 78)
        print("%s:%s   ×   %s:%s" % (ra, na, rb, nb))
        if not fa or not fb:
            print("  !! 未找到：%s%s" % ("" if fa else " A缺失", "" if fb else " B缺失"))
            continue
        ca, cb = strip_decorators(fa["code"]), strip_decorators(fb["code"])
        print("  位置：A L%d-L%d（%d 行） ；B L%d-L%d（%d 行）"
              % (fa["line"], fa["end"], len(ca.splitlines()), fb["line"], fb["end"], len(cb.splitlines())))
        if ca == cb:
            print("  判定：★ SAME —— 两份逐字节相同（真重复，可安全删其一）")
            continue
        def is_wrapper(code, other_name, other_rel):
            mod = other_rel.replace("/", ".")[:-3]
            return (other_name in code) and ("import" in code) and (len(code.splitlines()) <= 8)
        if is_wrapper(ca, nb, rb):
            print("  判定：WRAPPER —— A 只是委托 B（薄包装，非重复）")
            print("        A 正文：%s" % ca.replace("\n", " | ")[:160])
            continue
        if is_wrapper(cb, na, ra):
            print("  判定：WRAPPER —— B 只是委托 A（薄包装，非重复）")
            print("        B 正文：%s" % cb.replace("\n", " | ")[:160])
            continue
        print("  判定：DIFF —— 两份实现不同，需人工判断保留哪份。差异前 18 行：")
        for x in list(difflib.unified_diff(ca.splitlines(), cb.splitlines(), lineterm="", n=0))[:18]:
            print("      " + x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
