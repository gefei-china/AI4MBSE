# -*- coding: utf-8 -*-
"""P1 分层归位：把 75 个顶层模块搬进 4 个分层包 + 生成兼容垫片 + 改写产品代码 import。

⚠️ **已执行完毕、不可重跑**（2026-11-25）。本脚本是一次性迁移工具，留在仓库里是为了
让「哪个模块去了哪一层」可审计、可复核；**重跑会二次 git mv 已归位的文件并报错**。
要新增归属，改 `docs/architecture/layer_ownership.json`（唯一真源），不要跑本脚本。

## 用法（历史）

    ./.venv/Scripts/python.exe -X utf8 tools/migrate_p1_layering.py --phase 1
    ./.venv/Scripts/python.exe -X utf8 tools/migrate_p1_layering.py --phase 2
    ./.venv/Scripts/python.exe -X utf8 tools/migrate_p1_layering.py --patch-gates

## 迁移做了四件事（缺一不可）

1. **物理归位**：`git mv` 到 `core/domain/` / `adapters/` / `services/` / `services/tools/`。
2. **兼容垫片**：根目录留一个 ≤ 20 行的同名文件，把 `sys.modules[__name__]` 指向新模块。
   ⇒ 旧路径 `import X` / `from X import Y` / `__import__("X")` 全部照常工作，
   **模块身份唯一**（不会出现两份模块级状态）。这一点是必需的，因为：
   - `routers/graph_db.py:21` 用 `__import__("graph_db", fromlist=[...])` 字符串导入；
   - `tools/` 750 个脚本、85 个门禁/测试文件、`docs/` 都用旧路径；
   - `services/memory_backend.py` 用 `importlib.import_module("memory_backend_" + name)` 做插件发现。
   - 有 `__main__` 块的 17 个模块，垫片用 `runpy` 转发，保证 `python X.py` 仍能跑。
3. **产品代码改真路径**：`agent/ core/ services/ routers/ repositories/ workflows/ llm/
   knowledge_pipeline/ database/ models/ main.py` 里的 import 全改成新路径
   （AST 级定位，不做正则），使垫片只服务于历史脚本。
4. **`__file__` 兜底**：11 个模块用 `dirname(abspath(__file__))` 当仓库根去拼
   `checker.jar` / `sysml.library` / `sysml_models` / `mbse.db` / `integrations/*.json` / `tmp/vec_cache`。
   搬进子目录后这些路径会静默错位（**校验器直接变 unavailable**），统一改 `core.paths.repo_root()`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── 归属表（迁移时的唯一真源；落盘后以 JSON 为准）───────────────────────────
MAPPING = {
    # ① 语义/元模型内核：只依赖 core.*，可被任何层依赖（域模型 + 校验器 + 术语）
    "core/domain": [
        "ontology_semantics", "ontology_owl", "ontology_reasoning", "ontology_blueprint",
        "sysml_ast", "sysml_profile", "sysml_v2_check", "glossary", "text_normalize",
        "model_quality", "subgraph_views", "ingest_gate",
    ],
    # ② 纯基础设施工具（既不是域模型也不是外部适配）：并入既有 core/ 层
    "core": ["tool_resilience"],
    # ③ 对外适配：图库/向量/FTS/缓存/MCP/HTTP/智源/沙箱。只依赖 core
    "adapters": [
        "graph_db", "vector_index", "fts_search", "transcript_fts", "semantic_cache",
        "mcp_client", "zhiyuan_client", "http_tool_executor", "web_tools", "sandbox",
    ],
    # ④ 应用服务：跨表编排 / 引擎 / 任务 / 度量。可依赖 core + adapters
    "services": [
        "knowledge_engine", "entity_resolver", "staging_fuse", "triple_store", "triple_commit",
        "vector2graph", "sysml_importer", "semantic", "memory_service", "memory_backend",
        "knowledge_reflow", "task_queue", "hil_service", "metrics_core", "governance",
        "view_generator", "report_generator", "requirement_postprocess", "requirement_quality",
        "skill_improve", "capability_runtime", "norm_apply", "seed_registry",
        "seed_glossary_capability", "skill_deposit", "mcp_health",
    ],
    # ⑤ Agent 工具实现 + 注册器：与 services 同层（services 里有工具消费者，见 requirement_postprocess）
    "services/tools": [
        "alternatives_tools", "artifact_tools", "code_tools", "coverage_tools", "doc_tools",
        "file_tools", "graph_db_tools", "meta_tools", "methodology_profile_tools",
        "register_alternatives_tools", "register_graph_db_tools", "register_http_tools",
        "register_skills", "register_sysml_check_tools", "register_zhiyuan_tools",
        "report_tools", "requirement_itemize_tools", "skill_tools", "sysadmin_tools",
        "sysml_ast_tools", "sysml_autofix_tools", "sysml_check_tools",
        "sysml_import_graph_tools", "sysml_lint_tools", "sysml_project_check_tools",
        "sysml_stdlib_tools",
    ],
}

PKG_DOC = {
    "core/domain": '"""domain：语义与元模型内核（P1 分层归位，2026-11-25）。\n\n'
                   '只允许依赖 `core.*` 与标准库/三方库；**不得** import `services` / `adapters` /\n'
                   '`agent` / `routers`。含：本体（OWL/语义/推理/蓝图）、SysML v2 元模型与校验器、\n'
                   '术语与文本归一、SHACL 入库门禁。\n\n'
                   '依赖是否越界由 `tools/verify/verify_layer_ownership.py` 断言。\n"""\n',
    "adapters": '"""adapters：对外部系统/存储的适配层（P1 分层归位，2026-11-25）。\n\n'
                '图库（pyoxigraph/Fuseki）、向量索引、FTS、语义缓存、MCP、智源 HTTP、沙箱。\n'
                '只允许依赖 `core.*`；**不得** import `services` / `agent` / `routers`。\n\n'
                '⚠️ 注意与 `integrations/`（**配置数据目录**，放 `*.json` 外部集成清单）区分：\n'
                '那个是数据，本包是代码。\n"""\n',
    "services/tools": '"""tools：Agent 可调用工具的实体实现与注册器（P1 分层归位，2026-11-25）。\n\n'
                     '与 `services/` 同层（`requirement_postprocess` 这类服务会直接消费工具实现），\n'
                     '所以单独成包只为划分命名空间，不构成新的依赖约束。\n"""\n',
}

# ── `__file__` 当仓库根用的 11 个模块（搬动后必须改成 repo_root()）────────────
SELF_PATH_FIXES = [
    ("sysml_v2_check", "REPO = os.path.dirname(os.path.abspath(__file__))", "REPO = repo_root()"),
    ("sysml_ast", "ROOT = os.path.dirname(os.path.abspath(__file__))", "ROOT = repo_root()"),
    ("vector_index", "_ROOT = os.path.dirname(os.path.abspath(__file__))", "_ROOT = repo_root()"),
    ("methodology_profile_tools", "ROOT = os.path.dirname(os.path.abspath(__file__))", "ROOT = repo_root()"),
    ("sysml_ast_tools", "ROOT = os.path.dirname(os.path.abspath(__file__))", "ROOT = repo_root()"),
    ("sysml_import_graph_tools", "ROOT = os.path.dirname(os.path.abspath(__file__))", "ROOT = repo_root()"),
    ("sysml_lint_tools", "ROOT = os.path.dirname(os.path.abspath(__file__))", "ROOT = repo_root()"),
    ("sysml_check_tools",
     'MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysml_models")',
     'MODELS_DIR = os.path.join(repo_root(), "sysml_models")'),
    ("sysml_project_check_tools",
     'MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysml_models")',
     'MODELS_DIR = os.path.join(repo_root(), "sysml_models")'),
    ("sysml_stdlib_tools",
     'LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysml.library")',
     'LIB_DIR = os.path.join(repo_root(), "sysml.library")'),
    ("register_http_tools",
     'INTEGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "integrations")',
     'INTEGRATIONS_DIR = os.path.join(repo_root(), "integrations")'),
]

REGISTER_SKILLS_BLOCK = """_HERE = os.path.dirname(os.path.abspath(__file__))
for _ in range(4):
    _HERE = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)"""

REGISTER_SKILLS_NEW = """from core.paths import repo_root as _repo_root  # 2026-11-25 P1：仓库根单点解析
_HERE = _repo_root()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)"""

#: 产品代码目录（改真路径；`tools/`、`tests/`、`static/` 保留旧路径走垫片）
PRODUCT_DIRS = ["agent", "core", "services", "adapters", "routers", "repositories",
                "workflows", "llm", "knowledge_pipeline", "database", "models"]


def _all_modules() -> dict:
    out = {}
    for pkg, mods in MAPPING.items():
        for m in mods:
            out[m] = pkg
    return out


def _new_path(mod: str, pkg: str) -> str:
    return "%s/%s.py" % (pkg, mod)


def _has_main(path: str) -> bool:
    src = open(path, encoding="utf-8", errors="replace").read()
    return "__main__" in src and "__name__" in src


def shim_text(mod: str, pkg: str, cli: bool) -> str:
    target = "%s.%s" % (pkg.replace("/", "."), mod)
    head = (
        "# -*- coding: utf-8 -*-\n"
        '"""兼容垫片：`%s` 已于 2026-11-25 迁入 `%s`（P1 分层归位）。\n'
        "\n"
        "本文件**不含任何业务逻辑**，只把旧路径在 `sys.modules` 里指向新模块：\n"
        "`import %s` / `from %s import X` / `__import__(\"%s\")` 全部照常工作，\n"
        "且**模块身份唯一**（不会出现两份模块级状态）。\n"
        "\n"
        "新代码请用 `%s`；产品代码里的旧路径由\n"
        "`tools/verify/verify_layer_ownership.py` 断言为 0。\n"
        '"""\n'
        "import sys as _sys\n"
        "\n"
        "from %s import %s as _impl\n"
        "\n"
    ) % (mod, target, mod, mod, mod, target,
         pkg.replace("/", "."), mod)
    if cli:
        body = (
            "if __name__ == \"__main__\":\n"
            "    # 保留 `python %s.py` 的 CLI 行为（实现模块里有 __main__ 块）\n"
            "    import runpy as _runpy\n"
            "    _runpy.run_module(\"%s\", run_name=\"__main__\", alter_sys=True)\n"
            "else:\n"
            "    _sys.modules[__name__] = _impl\n"
        ) % (mod, target)
    else:
        body = "_sys.modules[__name__] = _impl\n"
    return head + body


# ── AST 级 import 改写（不做正则：正则会把字符串/注释一起改坏）──────────────
def _bpos(starts, lineno, col):
    return starts[lineno - 1] + col


def rewrite_imports(path: str, mapping: dict) -> int:
    import ast
    data = open(path, "rb").read()
    src = data.decode("utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return -1
    b = src.encode("utf-8")
    starts = [0]
    for i, byte in enumerate(b):
        if byte == 0x0A:
            starts.append(i + 1)

    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root not in mapping:
                continue
            # ⚠️ 必须是 `<包>.<模块>`，不能只写包名：
            #   2026-11-25 首跑漏了 root 段，`from web_tools import X` 被改成
            #   `from adapters import X`（包 __init__ 里没有 X）⇒ `import main` 直接炸。
            new_mod = "%s.%s%s" % (mapping[root].replace("/", "."), root, node.module[len(root):])
            seg = b[_bpos(starts, node.lineno, node.col_offset):
                    _bpos(starts, node.end_lineno, node.end_col_offset)].decode("utf-8")
            new = seg.replace("from " + node.module, "from " + new_mod, 1)
            if new == seg:
                raise SystemExit("改写失败（from）：%s:%d %r" % (path, node.lineno, seg[:80]))
            edits.append((node.lineno, node.col_offset, node.end_lineno,
                          node.end_col_offset, new))
        elif isinstance(node, ast.Import):
            moved = [a for a in node.names if a.name.split(".")[0] in mapping]
            if not moved:
                continue
            if len(node.names) > 1:
                raise SystemExit("混合 import 需人工处理：%s:%d" % (path, node.lineno))
            a = moved[0]
            root = a.name.split(".")[0]
            if "." in a.name:
                raise SystemExit("`import 包.子模块` 需人工处理：%s:%d" % (path, node.lineno))
            # `import X` 绑定的是名字 X ⇒ 必须写成 `from <pkg> import X as X`，
            # 写 `from <pkg>.X import X` 会去找模块内的同名属性（错）。
            pkg_dotted = mapping[root].replace("/", ".")
            alias = a.asname or root          # `import X` 绑定名是 X，必须保名
            seg = b[_bpos(starts, node.lineno, node.col_offset):
                    _bpos(starts, node.end_lineno, node.end_col_offset)].decode("utf-8")
            new = "from %s import %s as %s" % (pkg_dotted, root, alias)
            if new == seg:
                raise SystemExit("改写失败（import）：%s:%d" % (path, node.lineno))
            edits.append((node.lineno, node.col_offset, node.end_lineno,
                          node.end_col_offset, new))

    if not edits:
        return 0
    edits.sort(key=lambda e: (_bpos(starts, e[0], e[1])), reverse=True)
    for sl, sc, el, ec, new in edits:
        s, e = _bpos(starts, sl, sc), _bpos(starts, el, ec)
        b = b[:s] + new.encode("utf-8") + b[e:]
    open(path, "wb").write(b)
    return len(edits)


def _iter_py(dirs):
    for d in dirs:
        base = os.path.join(ROOT, d)
        if os.path.isfile(base):
            yield base
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [x for x in dns if x not in ("__pycache__",)]
            for fn in fns:
                if fn.endswith(".py"):
                    yield os.path.join(dp, fn)


def ensure_packages():
    for pkg, doc in PKG_DOC.items():
        d = os.path.join(ROOT, pkg)
        os.makedirs(d, exist_ok=True)
        init = os.path.join(d, "__init__.py")
        if not os.path.exists(init):
            open(init, "w", encoding="utf-8", newline="\n").write(doc)
            print("  建包 %s/__init__.py" % pkg)


def apply(phase: int):
    mapping = _all_modules()
    if phase == 1:
        keep = {m for m, p in mapping.items() if p in ("core/domain", "core", "adapters")}
    else:
        keep = {m for m, p in mapping.items() if p in ("services", "services/tools")}
    todo = {m: mapping[m] for m in keep}

    ensure_packages()
    moved, skipped = [], []
    for mod, pkg in sorted(todo.items()):
        src = os.path.join(ROOT, mod + ".py")
        dst_rel = _new_path(mod, pkg)
        dst = os.path.join(ROOT, dst_rel)
        if not os.path.exists(src):
            if os.path.exists(dst):
                skipped.append(mod)
                continue
            raise SystemExit("找不到源文件：%s" % src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        cli = _has_main(src)
        r = subprocess.run(["git", "mv", src, dst], cwd=ROOT,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("git mv 失败 %s: %s" % (mod, r.stderr[:300]))
        open(src, "w", encoding="utf-8", newline="\n").write(shim_text(mod, pkg, cli))
        moved.append((mod, pkg, cli))
    print("  搬迁 %d 个（已是垫片跳过 %d 个）" % (len(moved), len(skipped)))

    # ② 改写 import：新包内的模块 + 产品代码
    n_edit, n_file = 0, 0
    targets = list(_iter_py(sorted({p for p in todo.values()}))) + list(_iter_py(PRODUCT_DIRS)) \
        + [os.path.join(ROOT, "main.py")]
    seen = set()
    for p in targets:
        if p in seen or not os.path.exists(p):
            continue
        seen.add(p)
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel.split("/")[0] in ("tools", "tests"):
            continue
        k = rewrite_imports(p, todo)
        if k < 0:
            raise SystemExit("语法解析失败：%s" % rel)
        if k:
            n_file += 1
            n_edit += k
    print("  改写 import：%d 处 / %d 文件" % (n_edit, n_file))

    # ③ __file__ 当仓库根用 → repo_root()
    fixed = 0
    for mod, old, new in SELF_PATH_FIXES:
        if mod not in todo:
            continue
        p = os.path.join(ROOT, _new_path(mod, todo[mod]))
        txt = open(p, encoding="utf-8", errors="replace").read()
        if old not in txt:
            print("  ⚠ %s 未命中（可能已改过）：%s" % (mod, old[:50]))
            continue
        assert txt.count(old) == 1, "%s 命中 %d 次" % (mod, txt.count(old))
        ins = "from core.paths import repo_root  # 2026-11-25 P1：仓库根单点解析\n"
        txt = txt.replace(old, ins + new, 1)
        open(p, "w", encoding="utf-8", newline="\n").write(txt)
        fixed += 1
    if "register_skills" in todo:
        p = os.path.join(ROOT, _new_path("register_skills", todo["register_skills"]))
        txt = open(p, encoding="utf-8", errors="replace").read()
        assert txt.count(REGISTER_SKILLS_BLOCK) == 2, "register_skills 块命中 %d 次" % txt.count(REGISTER_SKILLS_BLOCK)
        txt = txt.replace(REGISTER_SKILLS_BLOCK, REGISTER_SKILLS_NEW)
        open(p, "w", encoding="utf-8", newline="\n").write(txt)
        fixed += 1
    print("  __file__ 兜底：%d 个模块" % fixed)
    return moved


def emit_manifest():
    mapping = _all_modules()
    mods = {}
    for m, pkg in sorted(mapping.items()):
        dst = os.path.join(ROOT, _new_path(m, pkg))
        mods[m] = {
            "layer": pkg,
            "path": _new_path(m, pkg),
            "legacy_path": m + ".py",
            "shim": os.path.exists(os.path.join(ROOT, m + ".py")),
            "cli": os.path.exists(dst) and _has_main(dst),
            "bytes": os.path.getsize(dst) if os.path.exists(dst) else 0,
        }
    doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "P1 分层归位单一真源。改归属只改本文件；根目录同名文件是 sys.modules 垫片。",
        "policy": {
            "layers": {
                "core/domain": {"may_import": ["core"]},
                "core": {"may_import": []},
                "adapters": {"may_import": ["core"]},
                "services": {"may_import": ["core", "adapters", "services"]},
                "services/tools": {"may_import": ["core", "adapters", "services"]},
            },
            "root_allow": ["main.py", "conftest.py"],
            "product_dirs": PRODUCT_DIRS,
        },
        "modules": mods,
    }
    p = os.path.join(ROOT, "docs/architecture/layer_ownership.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(doc, open(p, "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=1)
    print("  归属表 → %s（%d 模块）" % (os.path.relpath(p, ROOT), len(mods)))


# ── 门禁/测试里的路径式引用批量改路径 ────────────────────────────────────────
def patch_gates(apply_it: bool):
    mapping = _all_modules()
    targets = []
    for d in ("tools", "tests", ".github"):
        base = os.path.join(ROOT, d)
        for dp, dns, fns in os.walk(base):
            dns[:] = [x for x in dns if x not in ("__pycache__",)]
            for fn in fns:
                if fn.endswith((".py", ".js", ".yml", ".sh")):
                    targets.append(os.path.join(dp, fn))
    total_files, total_hits = 0, 0
    detail = {}
    for p in targets:
        # ⚠️ 必须 newline="" 读写：默认模式会把 CRLF 归一成 LF，106 个文件会被整体改行尾
        #   （本仓 core.autocrlf=true，行尾污染会淹掉真实 diff，见 AGENTS.md 坑 14）
        txt = open(p, encoding="utf-8", errors="replace", newline="").read()
        new = txt
        hits = 0
        for m, pkg in mapping.items():
            pat = re.compile(r"(?<![\w/.\-])" + re.escape(m) + r"\.py")
            new, k = pat.subn(pkg + "/" + m + ".py", new)
            hits += k
        if new != txt:
            total_files += 1
            total_hits += hits
            detail[os.path.relpath(p, ROOT).replace("\\", "/")] = hits
            if apply_it:
                open(p, "w", encoding="utf-8", newline="").write(new)
    print("  路径式引用：%d 处 / %d 文件%s" % (total_hits, total_files, "" if apply_it else "（dry-run）"))
    return detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, choices=[1, 2])
    ap.add_argument("--manifest", action="store_true")
    ap.add_argument("--patch-gates", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.phase:
        apply(a.phase)
    if a.manifest:
        emit_manifest()
    if a.patch_gates:
        patch_gates(not a.dry_run)


if __name__ == "__main__":
    main()
