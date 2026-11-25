#!/usr/bin/env python
# tier: pr
# -*- coding: utf-8 -*-
"""门禁：分层归属与依赖方向（P1，2026-11-25）

## 为什么要有这道门禁

P1 把 **75 个顶层模块**（原 1.6 MB，散在仓库根）搬进了 4 个分层包：

| 层 | 模块数 | 允许依赖 |
|---|---|---|
| `core/domain/` | 12 | 只 `core.*` |
| `core/`（纯基础设施工具） | 1 | — |
| `adapters/` | 10 | `core.*` |
| `services/` | 26 | `core.*` `adapters.*` `services.*` |
| `services/tools/` | 26 | 同上（同层，只为划分命名空间） |

搬完之后有**三个新的失守方式**，而它们全都不会让 `import main` 失败：

1. **垫片被写成业务模块** —— 根目录同名文件本来是 6 行 `sys.modules` 别名。
   只要有人"顺手在里面补个函数"，旧路径的消费者就拿到**另一份模块状态**，
   于是「改 A 文件不生效」这类 bug 重新出现（本仓历史上踩过「两份实现必然漂移」）。
2. **依赖方向被反向突破** —— `core/domain` 里 `import services.xxx` 在 Python 里完全合法，
   只在"换个部署形态/单测隔离/模块拆分"时才炸。这正是本仓 `core/__init__.py` 已经写下
   「core 内的模块不允许 import routers/services/agents」却**一直没有门禁**的原因。
3. **旧路径悄悄回潮** —— 产品代码本来已经全部改成新路径（实测 0 处旧路径），
   但下次有人写 `import graph_db` 也一样能跑（走垫片），于是"归位"逐渐失效。

## 还有一条更隐蔽的：`__file__` 当仓库根

实测有 11 个模块用 `os.path.dirname(os.path.abspath(__file__))` 拼仓库根下的资产
（`checker.jar` / `sysml.library` / `sysml_models` / `mbse.db` / `integrations/*.json` / `tmp/vec_cache`）。
搬进子目录后它们会指向 `core/domain/`，而**错误路径 + 上层 `except` = 校验器静默 unavailable**。
故 [E] 段永久禁止这个写法回到已归位模块里。

## 判据

- **[A] 归属表自洽**：75 个模块全覆盖 / 新路径存在 / 根垫片存在且是纯别名 / 根目录无新增业务模块
- **[B] 运行时身份**：`import 旧名` 得到的**就是**新模块对象（`__name__` 与 `sys.modules` 同一性）
- **[C] 依赖方向**：域层 / 适配层 / 服务层三层方向 + 越界必须登记豁免（带 reason）
- **[D] 旧路径棘轮**：产品代码旧路径 import 必须为 0（不是"不许增长"，是 0）
- **[E] `__file__` 兜底**：已归位模块里不得再出现 `dirname(abspath(__file__))` 当仓库根
- **M 系列变异自证**：M1 垫片塞业务逻辑 / M2 域层反向依赖 / M3 产品代码旧路径回潮 /
  M4 归属表指向不存在的文件 / M5 `__file__` 写法回流 —— 五条都必须被抓住

判据全部做成**纯函数**（输入:文件内容 + 归属表），变异用合成夹具跑，不落盘。
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

MANIFEST = os.path.join(ROOT, "docs/architecture/layer_ownership.json")
PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name, cond, detail=""):
    _results.append((PASS if cond else FAIL, name, detail))
    print("%s %s%s" % (PASS if cond else FAIL, name, ("  ← " + detail) if (detail and not cond) else ""))


def _read(p):
    return open(p, encoding="utf-8", errors="replace").read()


# ── 归属表 ────────────────────────────────────────────────────────────────
def load_manifest():
    return json.load(open(MANIFEST, encoding="utf-8"))


# ── [A] 归属表自洽（纯函数，便于变异）────────────────────────────────────
SHIM_MAX_LINES = 30


def shim_violations(src):
    """垫片内容校验：只许 docstring / import / sys.modules 别名 / runpy 转发。

    判据是**语句类型白名单**，不是"看起来有没有函数"—— 后者换个写法就失效。
    """
    out = []
    n = len(src.splitlines())
    if n > SHIM_MAX_LINES:
        out.append("行数 %d（>%d）" % (n, SHIM_MAX_LINES))
    if "sys.modules[__name__]" not in src:
        out.append("没有 sys.modules 别名（旧路径会拿到另一份模块）")
    for node in ast.parse(src).body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr, ast.Assign, ast.If)):
            continue
        out.append("含业务语句 %s（第 %d 行）" % (type(node).__name__, node.lineno))
    return out


def check_manifest(man, root_files, exists):
    """返回违规列表。exists(rel)->bool 便于变异注入。"""
    bad = []
    mods = man.get("modules") or {}
    if len(mods) < 70:
        bad.append("归属表只有 %d 个模块（P1 应有 75）" % len(mods))
    for mod, info in mods.items():
        if not exists(info["path"]):
            bad.append("%s 声明在 %s，文件不存在" % (mod, info["path"]))
        legacy = info.get("legacy_path") or (mod + ".py")
        if not exists(legacy):
            bad.append("%s 缺根垫片 %s（旧路径 import 会直接 ModuleNotFoundError）" % (mod, legacy))
        else:
            for v in shim_violations(_read(os.path.join(ROOT, legacy))):
                bad.append("%s 垫片：%s" % (legacy, v))
    declared = {info.get("legacy_path") or (mod + ".py") for mod, info in mods.items()}
    allow = set(man["policy"]["root_allow"]) | declared
    extra = sorted(set(root_files) - allow)
    if extra:
        bad.append("根目录有 %d 个未登记的业务模块：%s" % (len(extra), extra[:5]))
    return bad


# ── [C] 依赖方向（纯函数）─────────────────────────────────────────────────
#: 各层只允许 import 的顶层包
ALLOW = {
    "core/domain": {"core", "core.domain"},
    "core": {"core"},
    "adapters": {"core", "core.domain", "adapters", "database"},
    "services": {"core", "core.domain", "adapters", "services", "services.tools",
                 "repositories", "database", "llm", "knowledge_pipeline"},
    "services/tools": {"core", "core.domain", "adapters", "services", "services.tools",
                       "repositories", "database", "llm", "knowledge_pipeline"},
}
#: 本仓的**全部**顶层包（用于区分"本仓越界"与"标准库/三方库"）
#: ⚠️ 这张表不可省：首版只判 FORBIDDEN_TOP，于是 `core/domain` 里 `import services.x`
#: 被当成三方库**静默放过**（M2 变异抓到了这个洞）。判据必须是"本仓包 ∩ 不在允许集"。
FIRST_PARTY = {"core", "services", "adapters", "routers", "repositories", "agent",
               "workflows", "llm", "knowledge_pipeline", "database", "models", "tools"}
FORBIDDEN_TOP = {"routers", "workflows", "agent"}


def _imports_of(src):
    """返回 [(行号, 顶层包名, 完整模块名)]。函数内 import 一样抓（懒加载也受约束）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.append((n.lineno, a.name.split(".")[0], a.name))
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.append((n.lineno, n.module.split(".")[0], n.module))
    return out


def check_direction(files_map, exemptions):
    """files_map: {层: {相对路径: 源码}}；exemptions: {"文件::模块": "reason"}"""
    bad = []
    for layer, files in files_map.items():
        allow = ALLOW.get(layer)
        if allow is None:
            continue
        for rel, src in files.items():
            for lineno, top, full in _imports_of(src):
                if top in FORBIDDEN_TOP:
                    key = "%s::%s" % (rel, top)
                    if key in exemptions:
                        continue
                    bad.append("%s:%d %s 越界依赖 %s" % (rel, lineno, layer, full))
                elif top in FIRST_PARTY and top not in allow:
                    key = "%s::%s" % (rel, top)
                    if key in exemptions:
                        continue
                    bad.append("%s:%d %s 越界依赖 %s" % (rel, lineno, layer, full))
    return bad


# ── [D] 旧路径棘轮（纯函数）───────────────────────────────────────────────
def check_legacy_imports(files_map, moved):
    bad = []
    for rel, src in files_map.items():
        for lineno, top, full in _imports_of(src):
            if top in moved and "." not in full:
                bad.append("%s:%d 产品代码仍用旧路径 `%s`（应写新路径）" % (rel, lineno, full))
    return bad


# ── [E] `__file__` 当仓库根 ────────────────────────────────────────────────
SELF_ROOT_PAT = re.compile(r"dirname\s*\(\s*(?:os\.path\.)?abspath\s*\(\s*__file__\s*\)\s*\)")


def check_self_root(files_map):
    bad = []
    for rel, src in files_map.items():
        for m in SELF_ROOT_PAT.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            seg = src.splitlines()[line - 1] if line <= len(src.splitlines()) else ""
            if "repo_root" in seg:
                continue
            bad.append("%s:%d 用 __file__ 当仓库根：%s" % (rel, line, seg.strip()[:70]))
    return bad


# ── [F] 新层必须登记进各处**目录白名单** ────────────────────────────────────
# 为什么这条是硬门禁：2026-11-25 P1 把 10 个模块搬进 `adapters/`，而四个扫描器
# （scan_silent_except / verify_gate_portability / verify_no_fake_actor /
#   verify_no_mutation_residue）的目录白名单都没它 ⇒ `adapters/` 从账本里**静默消失**，
# 静默兜底总数从 2564 掉到 2508，而差的那 56 条**正好等于** adapters 单独扫出的
# silent 数 —— 看上去像「治理见效」，实际是统计口径缩小、一条都没少。
# 这类「覆盖悄悄缩小 + 门禁仍绿」是分层搬迁最隐蔽的失败形态，只能靠白名单自证。
SCANNER_WHITELISTS = (
    ("tools/scan_silent_except.py", "PRODUCT_DIRS", "静默兜底账本"),
    ("tools/verify/verify_gate_portability.py", "APP_DIRS", "绝对路径门禁 P5/P6"),
    ("tools/verify/verify_no_fake_actor.py", "SCAN_DIRS", "假人物串门禁"),
    ("tools/verify/verify_no_mutation_residue.py", "_scan_dirs", "变异残留 A3"),
)


def _whitelist_of(rel_path, var_name):
    """AST 取某个白名单变量的字符串常量集合（不 import，避免执行门禁主体）。"""
    try:
        tree = ast.parse(_read(os.path.join(ROOT, rel_path)))
    except (OSError, SyntaxError):
        return None
    found = None
    for node in ast.walk(tree):
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not value:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if var_name not in names:
            continue
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            found = {e.value for e in value.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return found


def check_scanner_coverage(man):
    """归属表里模块所在的最顶层目录，必须在每个扫描器白名单里都出现。

    判据取自归属表本身（不是硬编码目录名）⇒ 以后新建 `integration/` 之类的层，
    只要往里放了模块就会自动被要求登记，无需改这条门禁。
    """
    tops = set()
    for info in man["modules"].values():
        rel = info["path"].replace("\\", "/")
        tops.add(rel.split("/")[0])
    bad = []
    for rel_path, var, label in SCANNER_WHITELISTS:
        got = _whitelist_of(rel_path, var)
        if got is None:
            bad.append("%s 找不到 %s（白名单改名或文件被删，门禁已空转）" % (rel_path, var))
            continue
        missing = sorted(t for t in tops if t not in got)
        if missing:
            bad.append("%s %s 未登记：%s" % (label, var, ",".join(missing)))
    return sorted(tops), bad


def real_files(man):
    """采集真实树：根 py 列表 / 各层源码 / 产品代码源码。返回 dict，便于门禁与排查脚本共用。"""
    root_files = sorted(f for f in os.listdir(ROOT) if f.endswith(".py"))
    by_layer = {}
    for rel_dir in ("core/domain", "adapters", "services", "services/tools"):
        bucket = {}
        for dp, dns, fns in os.walk(os.path.join(ROOT, rel_dir)):
            dns[:] = [x for x in dns if x != "__pycache__"]
            for f in fns:
                if f.endswith(".py"):
                    p = os.path.join(dp, f)
                    bucket[os.path.relpath(p, ROOT).replace("\\", "/")] = _read(p)
        by_layer[rel_dir] = bucket
    by_layer["core"] = {("core/%s" % f): _read(os.path.join(ROOT, "core", f))
                        for f in os.listdir(os.path.join(ROOT, "core"))
                        if f.endswith(".py")}
    prod = {}
    for d in man["policy"]["product_dirs"]:
        for dp, dns, fns in os.walk(os.path.join(ROOT, d)):
            dns[:] = [x for x in dns if x != "__pycache__"]
            for f in fns:
                if f.endswith(".py"):
                    p = os.path.join(dp, f)
                    prod[os.path.relpath(p, ROOT).replace("\\", "/")] = _read(p)
    prod["main.py"] = _read(os.path.join(ROOT, "main.py"))
    moved_files = {i["path"]: _read(os.path.join(ROOT, i["path"]))
                   for i in man["modules"].values()
                   if os.path.exists(os.path.join(ROOT, i["path"]))}
    return {"root_files": root_files, "by_layer": by_layer, "prod": prod,
            "moved_files": moved_files}


def main():
    man = load_manifest()
    mods = man["modules"]
    moved = set(mods)
    R = real_files(man)
    root_files, files_by_layer, prod = R["root_files"], R["by_layer"], R["prod"]

    # A
    bad_a = check_manifest(man, root_files, lambda r: os.path.exists(os.path.join(ROOT, r)))
    check("[A1] 归属表自洽：%d 模块全覆盖 / 新路径存在 / 根垫片是纯别名 / 根无新增业务模块"
          % len(mods), not bad_a, " | ".join(bad_a[:6]))

    # B 运行时身份（同一对象，不是两份）
    bad_b = []
    for mod, info in mods.items():
        want = info["path"][:-3].replace("/", ".")
        # ⚠️ 旧路径**不一定在根目录**：P1 收敛批 #3 的 9 个模块原本在 `core/`，
        #    垫片也留在 `core/X.py` ⇒ 旧模块名是 `core.X` 而不是 `X`。
        #    必须从归属表的 legacy_path 推导，不能拿模块名硬猜。
        old_mod = (info.get("legacy_path") or (mod + ".py"))[:-3].replace("/", ".")
        try:
            # `__import__("core.X")` 只返回顶层 `core` 包 ⇒ 必须用 importlib
            m = importlib.import_module(old_mod)
        except Exception as e:                                # noqa: BLE001
            bad_b.append("%s import 失败 %s" % (old_mod, type(e).__name__))
            continue
        if m.__name__ != want:
            bad_b.append("%s 旧路径拿到的是 %s，不是 %s" % (mod, m.__name__, want))
        try:
            new = __import__(want, fromlist=["_"])
        except Exception:                                     # noqa: BLE001
            bad_b.append("%s 新路径 import 失败" % want)
            continue
        if m is not new:
            bad_b.append("%s 旧/新路径是两个不同模块对象" % mod)
    check("[B1] %d 个垫片与实现是**同一个模块对象**（杜绝两份状态）" % len(mods), not bad_b,
          " | ".join(bad_b[:5]))

    # C 依赖方向：分两段
    #   C1 = **P1 归位模块**（归属表 path 指向的模块）必须零越界 —— 新代码硬约束
    #   C2 = **全仓**（含 core/ 与 services/ 的存量）只卡增量 —— 基线现为 45，
    #        是按批归位/下沉后下调的；要降必须真改代码后同步下调基线。
    ex = man.get("exemptions", {})
    moved_paths = {i["path"] for i in mods.values()}
    sub = {k: {p: s for p, s in v.items() if p in moved_paths} for k, v in files_by_layer.items()}
    bad_c1 = check_direction(sub, ex)
    check("[C1] %d 个归位模块的层方向零越界（豁免 %d 条，均为存量且带 reason）"
          % (len(moved_paths), len(ex)), not bad_c1, " | ".join(bad_c1[:6]))
    # ⚠️ 垫片位于分层目录内时（如 `core/action_binding_runtime.py` 指向
    #    `services.action_binding_runtime`），它的 `from services import X` 是
    #    **兼容机制本身**，不能被当成 core→services 越界。否则 C2 会虚报 16 处。
    shim_paths = {i["legacy_path"] for i in mods.values() if i.get("shim")}
    no_shim_files = {k: {p: s for p, s in v.items() if p not in shim_paths}
                     for k, v in files_by_layer.items()}
    bad_c2 = check_direction(no_shim_files, ex)
    base_dir = man["policy"].get("direction_baseline", 0)
    check("[C2] 全仓层间越界不许增长（基线 %d 处，现测 %d 处）" % (base_dir, len(bad_c2)),
          len(bad_c2) <= base_dir,
          "新增：%s" % [x for x in bad_c2 if x not in check_direction(sub, {})][:4])

    # D 旧路径棘轮（产品代码）
    bad_d = check_legacy_imports(prod, moved)
    check("[D1] 产品代码对已归位模块的**旧路径 import = 0**（现测 %d 处）" % len(bad_d),
          not bad_d, " | ".join(bad_d[:5]))

    # E __file__ 兜底
    bad_e = check_self_root(R["moved_files"])
    check("[E1] 已归位模块里没有 `dirname(abspath(__file__))` 当仓库根（现测 %d 处）" % len(bad_e),
          not bad_e, " | ".join(bad_e[:4]))

    # F 新层必须登记进扫描器白名单（否则覆盖悄悄缩小、门禁仍绿）
    tops_f, bad_f = check_scanner_coverage(man)
    check("[F1] 归属层（%s）在 %d 个扫描器白名单里全部登记"
          % ("/".join(tops_f), len(SCANNER_WHITELISTS)),
          not bad_f, " | ".join(bad_f[:4]))

    # ── M 变异自证（合成夹具，不落盘）──────────────────────────────────────
    base_shim = "import sys as _sys\n\nfrom adapters import graph_db as _impl\n\n_sys.modules[__name__] = _impl\n"

    def _mut_manifest(src):
        m = json.loads(json.dumps(man))
        m["modules"]["graph_db"]["path"] = "adapters/__not_exist__.py"
        return m

    def _shim_violations(src):
        return shim_violations(src)

    muts = []
    muts.append(("M1 垫片塞业务函数 → 被抓",
                 bool(_shim_violations(base_shim + "\n\ndef sneaky(x):\n    return x\n")),
                 _shim_violations(base_shim)))
    muts.append(("M1b 干净垫片 → 不误报", not _shim_violations(base_shim), []))
    bad_m2 = check_direction(
        {"core/domain": {"core/domain/ontology_owl.py": "import services.task_queue\n"}}, {})
    muts.append(("M2 域层 import services → 被抓", bool(bad_m2), bad_m2))
    bad_m2b = check_direction(
        {"core/domain": {"core/domain/ontology_owl.py": "import core.paths\n"}}, {})
    muts.append(("M2b 域层 import core → 不误报", not bad_m2b, bad_m2b))
    bad_m3 = check_legacy_imports({"services/x.py": "import graph_db\n"}, moved)
    muts.append(("M3 产品代码旧路径回潮 → 被抓", bool(bad_m3), bad_m3))
    bad_m3b = check_legacy_imports({"services/x.py": "import adapters.graph_db\n"}, moved)
    muts.append(("M3b 新路径 → 不误报", not bad_m3b, bad_m3b))
    bad_m4 = check_manifest(_mut_manifest(man), root_files, lambda r: os.path.exists(os.path.join(ROOT, r)))
    muts.append(("M4 归属表指向不存在文件 → 被抓", bool(bad_m4), bad_m4))
    bad_m5 = check_self_root({"core/domain/x.py": "ROOT = os.path.dirname(os.path.abspath(__file__))\n"})
    muts.append(("M5 __file__ 当仓库根回流 → 被抓", bool(bad_m5), bad_m5))
    bad_m5b = check_self_root({"core/domain/x.py": "ROOT = repo_root()\n"})
    muts.append(("M5b 用 repo_root → 不误报", not bad_m5b, bad_m5b))

    # M6：归属表里出现**未登记进白名单的新层** ⇒ F1 必须变红
    #     （复刻 adapters/ 那次的真实失败形态：模块搬过去了、扫描器白名单没跟）
    _m6 = json.loads(json.dumps(man))
    _m6["modules"]["graph_db"]["path"] = "integration/graph_db.py"
    _, bad_m6 = check_scanner_coverage(_m6)
    muts.append(("M6 新层未登记进扫描器白名单 → 被抓", bool(bad_m6), bad_m6[:2]))
    _, bad_m6b = check_scanner_coverage(man)
    muts.append(("M6b 现有层全部已登记 → 不误报", not bad_m6b, bad_m6b[:2]))

    for label, ok, got in muts:
        check("[M] " + label, ok, str(got)[:200])

    p = sum(1 for r in _results if r[0] == PASS)
    f = sum(1 for r in _results if r[0] == FAIL)
    print("\n===")
    print("%d PASS / %d FAIL" % (p, f))
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
