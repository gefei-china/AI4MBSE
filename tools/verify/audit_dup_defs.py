"""同类重复定义审计 v2（2026-09-18 S7）：用 AST 按**作用域路径**找真正的重复定义。

v1 用「缩进 + 名字」判定，把不同类里的同名方法（如 `graph_db.py` 的 Mock/Writer/Reader 各有
`write_triples`）误报为重复 —— 21 条里绝大多数是假阳性。v2 改用 ast：记录每个 def/class 的
**限定名路径**（`Class.method` / `outer.inner`），同一路径出现两次才是真正的"后者覆盖前者"。

## P1-6 收口（2026-10-04）：让它真能当门禁

原版**永远 `return 0`** —— 即使发现 100 条重复定义，CI 也是绿的。
把它接进 `.github/workflows/ci.yml` 之前必须先修这个，否则接进去的是
**一道永远不会红的门禁**（本项目已多次吃过"能力齐备但没人用/不生效"的亏）。

三处改动：
1. **有重复 ⇒ exit 1**（`--allow` 可放宽为仅报告）。
2. **语法错误 ⇒ exit 2**。原版遇到 SyntaxError 只 `print` 后 `continue` ——
   一个打不开的 .py 会被**静默跳过**，等于给重复定义开了后门
   （把有重复的文件改出语法错就能绕过门禁）。
3. **`--selftest` 自检**：造一份含重复的临时文件，断言审计器**能判红**。
   没有自检就无法区分"确实没重复"和"审计器坏了/漏扫了" ——
   **空结果和恒绿是一回事**。

用法：
  .venv\\Scripts\\python.exe -X utf8 tools/verify/audit_dup_defs.py            # 门禁模式
  .venv\\Scripts\\python.exe -X utf8 tools/verify/audit_dup_defs.py --selftest  # 判红能力自检
  .venv\\Scripts\\python.exe -X utf8 tools/verify/audit_dup_defs.py --all      # 含 tests/tools
"""
import argparse
import ast
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_DIRS = ("_archive", "backups", "tmp", "outputs", "screenshots", ".venv", "node_modules",
             "static", "data", "java-runtime", "fuseki", "docs", "__pycache__", ".git")


def iter_py(with_tests: bool, root: pathlib.Path = None):
    base = root or ROOT
    for p in base.rglob("*.py"):
        rel = p.relative_to(base).as_posix()
        if any(seg in rel.split("/") for seg in SKIP_DIRS):
            continue
        if not with_tests and (rel.startswith("tests/") or rel.startswith("tools/")):
            continue
        yield p, rel


def walk(node, prefix, out):
    """收集 `node` 体内（递归）的定义，限定名 = prefix + name。

    ⚠️ 2026-10-04 修：原版只进 `child.body`，**漏掉 `orelse` / `finalbody` /
    `handlers`**。实测自检 S4 直接判红 —— 下面这段原版抓不到：

        if sys.platform == 'win32':
            def g(): ...      # ← 原版抓到（body）
        else:
            def g(): ...      # ← 原版**漏掉**（orelse）

    这个漏法的危险在于：它不是"少报几条噪音"，而是**给了绕过门禁的后门**
    —— 把重复定义挪进 else 分支就查不到了。而门禁最怕的就是"看起来绿、
    实际能被人为绕过"。

    :param node: AST 节点（Module / If / Try / For …）。传节点而非 stmt 列表，
        这样才能在递归里访问 `orelse`/`finalbody`/`handlers` 这些兄弟字段。
    """
    body = getattr(node, "body", None)
    if body is None:
        # 调用方传进来的是 **stmt 列表**（orelse / handlers / finalbody 都是这种形态）。
        # ⚠️ 实测踩过：这里原本直接 `return`，于是 `walk(node.orelse)` 拿到 list、
        #    `getattr(list, "body")` 恒 None ⇒ 整条else 分支被跳过（自检 S4 判红）。
        body = node if isinstance(node, list) else []
    for child in body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            path = prefix + child.name
            out.setdefault(path, []).append(child.lineno)
            walk(child, path + ".", out)
        elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            walk(child, prefix, out)          # 进入分支体
            for sibling in _siblings(child):  # else / except / finally / for-else
                walk(sibling, prefix, out)


def _siblings(node):
    """返回与 `node.body` 同作用域的兄弟分支（orelse / handlers / finalbody）。

    实测确认：Python AST 里 `ast.If.orelse`、`ast.Try.handlers`、
    `ast.Try.finalbody`、`ast.For.orelse` 都是 **stmt / ExceptHandler 的列表**，
    不是单个节点 —— 所以返回列表让 `walk` 统一处理。
    """
    out = []
    if getattr(node, "orelse", None):
        out.append(node.orelse)
    for h in getattr(node, "handlers", None) or []:
        out.append(h)
    if getattr(node, "finalbody", None):
        out.append(node.finalbody)
    return out


def audit(root: pathlib.Path = None, with_tests: bool = False):
    """返回 (命中列表, 语法错误列表)。命中元素 = (rel_path, {限定名: 行号表})。"""
    files = list(iter_py(with_tests, root))
    hits, syntax_errs = [], []
    for p, rel in sorted(files, key=lambda x: x[1]):
        try:
            tree = ast.parse(p.read_bytes().decode("utf-8", errors="replace"))
        except SyntaxError as e:
            # ⚠️ 不能只 print 后跳过 —— 那等于"把文件改坏就能绕过门禁"
            syntax_errs.append((rel, str(e)))
            continue
        out = {}
        walk(tree, "", out)
        dups = {k: v for k, v in out.items() if len(v) > 1}
        if dups:
            hits.append((rel, dups))
    return hits, syntax_errs, len(files)


def report(hits) -> int:
    n_dups = 0
    for rel, dups in hits:
        print("★ %s" % rel)
        for path, lines in sorted(dups.items(), key=lambda kv: kv[1][0]):
            n_dups += 1
            print("    %-40s 定义于 %s  → 生效 L%d；死代码 %s"
                  % (path, ", ".join("L%d" % x for x in lines), lines[-1],
                     ", ".join("L%d" % x for x in lines[:-1])))
    return n_dups


def selftest() -> int:
    """判红能力自检：造三种样本，断言审计器分别能/不能抓到。

    为什么必须做这个：**"没发现问题" 和 "审计器坏了" 在输出上长得一模一样**
    （都是 0 条）。只有主动注入已知样本，才能证明这份代码真的会红。
    这与 MEMORY 里"变异自证的红灯必须来自被测判据"同源。
    """
    ok = True

    def rec(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               ("  <- " + detail) if detail and not cond else ""))

    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        # ① 同作用域同名def ⇒ 真重复（必须抓到）
        (base / "dup.py").write_text(
            "def f():\n    return 1\n\n\ndef f():\n    return 2\n", encoding="utf-8")
        hits, errs, _ = audit(base)
        rec("S1 同作用域重复 def 能抓到",
            len(hits) == 1 and "f" in hits[0][1],
            "hits=%s" % [(h[0], list(h[1])) for h in hits])

        # ② 不同类里的同名方法 ⇒ 不是重复（v2 的核心修正，必须放过）
        (base / "dup.py").write_text(
            "class A:\n    def run(self):\n        return 1\n\n\n"
            "class B:\n    def run(self):\n        return 2\n", encoding="utf-8")
        hits, errs, _ = audit(base)
        rec("S2 不同类的同名方法不误报", not hits,
            "误报=%s" % [(h[0], list(h[1])) for h in hits])

        # ③ 语法错误必须被单独报出（不能静默跳过）
        (base / "dup.py").write_text("def broken(:\n", encoding="utf-8")
        hits, errs, _ = audit(base)
        rec("S3 语法错误被单独捕获", len(errs) == 1 and not hits,
            "hits=%s errs=%s" % ([h[0] for h in hits], errs))

        # ④ 条件分支里的重复定义也算重复（老版本容易漏）
        (base / "dup.py").write_text(
            "import sys\nif True:\n    def g():\n        return 1\n"
            "else:\n    def g():\n        return 2\n", encoding="utf-8")
        hits, errs, _ = audit(base)
        rec("S4 条件分支里的重复定义能抓到",
            len(hits) == 1 and "g" in hits[0][1],
            "hits=%s" % [(h[0], list(h[1])) for h in hits])

    print("自检结论：%s" % ("全部通过（审计器具备判红能力）" if ok else "存在失效断言"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="含 tests/ 与 tools/")
    ap.add_argument("--allow", action="store_true",
                    help="只报告不失败（用于记录存量基线；**不要**在 CI 里用）")
    ap.add_argument("--selftest", action="store_true", help="判红能力自检")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    hits, syntax_errs, n_files = audit(with_tests=a.all)
    print("扫描 .py 文件: %d（%s）\n" % (n_files, "含 tests/tools" if a.all else "仅一线代码"))
    n_dups = report(hits)

    if syntax_errs:
        print("\n⚠️ 语法错误 %d 个（这些文件**未被审计**，等同给了重复定义开后门）："
              % len(syntax_errs))
        for rel, e in syntax_errs:
            print("    %s: %s" % (rel, e))

    print("\n含真重复定义的文件: %d 个；重复条目: %d 条；未审计(语法错): %d 个"
          % (len(hits), n_dups, len(syntax_errs)))

    # ── 退出码（2026-10-04 P1-6 收口）──────────────────────────────────
    #原版无条件 return 0 ⇒ 这道门禁永远不会红。必须按"是否发现问题"给码。
    if syntax_errs:
        return 2                      # 有文件没被审计到 ⇒ 覆盖不全，最严重
    if n_dups:
        if a.allow:
            print("[allow] 发现 %d 条重复定义，但 --allow 已开启 ⇒ exit 0" % n_dups)
            return 0
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
