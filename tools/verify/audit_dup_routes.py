"""重复路由审计 v2（2026-09-18 S7）：**静态扫描装饰器**找重复注册的 (path, method)。

v1 想直接读 `app.routes`，但 FastAPI 0.141 把 include_router 的结果包成 `_IncludedRouter`，
不再把子路由摊平，导致枚举不到；且 OpenAPI schema 本身按 path 去重，查不出重复。
静态扫描装饰器最可靠：所有路由都写在 `@router.<method>("<path>")` 上。

背景（为什么这个审计重要）：`routers/studio_parts/` 是「shared.py 持有唯一 router，各分片 import * 后挂路由」，
若分片与 shared 里都写了同一路径，FastAPI 只命中**先注册**的那个，后者静默失效 ——
表现为「改了 prompts.py 却没生效，改了 shared.py 才生效」。

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/audit_dup_routes.py
"""
import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DECO_RE = re.compile(r'@(\w+)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']')
SKIP = ("_archive", "backups", "tmp", "outputs", "tests", "tools", ".venv", "__pycache__", "docs")


def router_prefixes(src: str) -> dict:
    """解析文件里 `router = APIRouter(prefix="/api/xxx", ...)` 的前缀。

    2026-09-18 修正：原版只看装饰器字面路径，会把 `/api/sparql/dependency` 与
    `/api/swrl/dependency`（两边装饰器都写 `"/dependency"` + 各自 APIRouter 前缀）
    误判为重复。这里按 ast 取 prefix 做归一化，与运行时的真实路径一致。
    """
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Call):
                    fn = n.value.func
                    fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
                    if fname != "APIRouter":
                        continue
                    pref = ""
                    for kw in n.value.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            pref = str(kw.value.value or "")
                    out[t.id] = pref
    return out


def main():
    hits = collections.OrderedDict()      # (method, path) -> [(file, line, router_var, func)]
    files = 0
    for p in sorted(ROOT.joinpath("routers").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if any(seg in rel.split("/") for seg in SKIP):
            continue
        files += 1
        try:
            src = p.read_bytes().decode("utf-8", errors="replace")
        except Exception:
            continue
        prefs = router_prefixes(src)
        lines = src.splitlines()
        for i, ln in enumerate(lines, 1):
            m = DECO_RE.search(ln)
            if not m:
                continue
            rv, method, raw_path = m.group(1), m.group(2).upper(), m.group(3)
            pref = prefs.get(rv, "")
            path = (pref + raw_path) if (pref and not raw_path.startswith(pref)) else raw_path
            key = (method, path)
            # 找紧随其后的 def 名
            fn = "-"
            for j in range(i, min(i + 4, len(lines))):
                dm = re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[j])
                if dm:
                    fn = dm.group(1)
                    break
            hits.setdefault(key, []).append((rel, i, rv, fn))

    dups = {k: v for k, v in hits.items() if len([x for x in v]) > 1}
    print("扫描 routers/ 下 %d 个文件；路由装饰器 %d 条（不同 path+method: %d）"
          % (files, sum(len(v) for v in hits.values()), len(hits)))
    if not dups:
        print("未发现重复注册 ✅")
        return 0
    print("\n重复注册 %d 处（先 import 者生效，后者静默失效）：\n" % len(dups))
    for (method, path), items in sorted(dups.items(), key=lambda kv: kv[0][1]):
        print("%-6s %s" % (method, path))
        for k, (f, ln, rv, fn) in enumerate(items):
            print("        %s  %s:%d  %s()  router=%s" % ("生效?" if k == 0 else "失效?", f, ln, fn, rv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
