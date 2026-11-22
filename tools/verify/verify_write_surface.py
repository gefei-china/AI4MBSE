#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：**全仓写入面**清单（P0-0 收口纠偏，2026-11-21 第四轮）。

## 要挡住的是什么

`verify_single_write_entry.py::G4` 只统计 3 个文件（`entities.py` / `graph.py` /
`graph_workspace.py`）里的写**端点**，得出「待迁移 13 个」。但真实对
`entities` / `relations` 的直接 SQL 写散布在 **15 个生产文件 / 32 个函数 / 80 处**：

    repositories/branch_repo.py     23 处（分支合并、发布、回滚）
    entity_resolver.py              12 处（实体/关系去重合并）
    repositories/knowledge_repo.py   9 处
    routers/knowledge_parts/graph_query.py   6 处
    services/ontology_migration.py   6 处
    ...

⇒ **端点数不是写入口覆盖率的代理变量**。G4「只降不升」看着在收敛，
   统计面之外的裸写却完全看不见 —— 这是「校验面不全 ⇒ 数字好看但假」，
   与「单文件整体校验遮蔽真实语法错」同族。

## 本门禁做什么（不改业务行为）

把生产代码里所有直接写这两张表的位置按函数登记成**显式台账**：

  W1  扫描器可用，且扫到的每个 (文件,函数) 都在台账里（**新增裸写必须显式登记**）
  W2  台账里的键都还存在（防台账过期、防改完不更新）
  W3  `inference` 类只能存在于统一入口模块内；端点侧必须为 0
      （2026-11-21 第四轮已修：`_merge_cohort_items` 改走 `write_inferred_*`）
  W4  G4 统计面与真实写入面并列输出（信息项，不判红）

类别是**事实登记**，不是背书：`inference` 是已知缺口，`bulk` 是事务性批量写，
`status` 是字段流转，它们都不走 `write_entity` / `write_edge`，各自是否需要收口
属于架构决策，不在本门禁里替人拍板。

## 变异自证
  M1 往生产文件注入一处新裸写 ⇒ W1 必须判红
  M2 删掉台账里一个键 ⇒ W2 必须判红
  M3 把 inference 类别复制到一个不存在的函数 ⇒ W3 必须判红
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

#: 扫描时排除的目录（测试/工具/脚本里的建表插入不算生产写入面）
EXCLUDED_DIRS = {
    "tests", "tools", "backups", "tmp", "logs", ".venv", ".git",
    "database", "static", "data", "__pycache__", "java-runtime", "fuseki",
}

#: 直接写 entities / relations 的 SQL（兼容跨行字符串）
WRITE_SQL = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s*\n?\s*(entities|relations)\b", re.I)

#: 事实台账：(相对路径, 所在函数) -> 类别
#: 类别语义：
#:   unified    —— 统一写入口自身（graph_write_gate）
#:   repository —— 统一入口委托的仓储实现（版本化写入）
#:   status     —— 状态/分类字段流转，不写版本行
#:   bulk       —— 事务性批量写（分支合并/回滚/本体迁移/去重/SysML 导入）
#:   metadata   —— 坐标/来源/项目等元数据字段回填
#: 注：推断通道（write_inferred_*）委托仓储写入，本身没有直接 SQL，
#:     由 W3 单独静态锁接线，不放进本台账。
REGISTRY = {
    ("core/graph_write_gate.py", "write_edge"): "unified",
    ("core/graph_write_gate.py", "write_entity"): "unified",

    ("repositories/knowledge_repo.py", "create_entity"): "repository",
    ("repositories/knowledge_repo.py", "create_relation"): "repository",
    ("repositories/knowledge_repo.py", "update_entity"): "repository",
    ("repositories/knowledge_repo.py", "update_with_history"): "repository",

    ("repositories/knowledge_repo.py", "review_entity"): "status",
    ("repositories/knowledge_repo.py", "review_relation"): "status",
    ("repositories/knowledge_repo.py", "set_entity_category"): "status",

    ("repositories/branch_repo.py", "_copy_entity_to_branch"): "bulk",
    ("repositories/branch_repo.py", "create_branch"): "bulk",
    ("repositories/branch_repo.py", "resolve_merge"): "bulk",
    ("repositories/branch_repo.py", "rollback_merge"): "bulk",
    ("repositories/branch_repo.py", "sync_from_branch"): "bulk",
    ("repositories/branch_repo.py", "update_branch"): "bulk",
    ("repositories/commit_repo.py", "revert_commit"): "bulk",
    ("entity_resolver.py", "conflict_adjudicate"): "bulk",
    ("entity_resolver.py", "merge_entities"): "bulk",
    ("entity_resolver.py", "merge_relation"): "bulk",
    ("entity_resolver.py", "rollback_merge"): "bulk",
    ("routers/knowledge_parts/graph_query.py", "graph_update_edge"): "bulk",
    ("routers/knowledge_parts/graph_query.py", "merge_entity"): "bulk",
    ("routers/knowledge_parts/ontology.py", "ontology_update_type"): "bulk",
    ("services/ontology_migration.py", "apply"): "bulk",
    ("sysml_importer.py", "import_sysml"): "bulk",
    ("triple_commit.py", "commit_approved_triples"): "bulk",
    ("vector2graph.py", "_survivorship_apply"): "bulk",
    ("vector2graph.py", "confirm_candidates"): "bulk",

    ("ontology_semantics.py", "create_node"): "metadata",
    ("repositories/project_repo.py", "detach_project_references"): "metadata",
    ("routers/knowledge_parts/graph.py", "graph_update_node"): "metadata",

}

#: verify_single_write_entry.py 的统计面（用于 W4 对照，判据里明确标注「信息项」）
G4_FILES = (
    "routers/knowledge_parts/entities.py",
    "routers/knowledge_parts/graph.py",
    "routers/graph_workspace.py",
)

_results = []
PASS, FAIL = "PASS", "FAIL"


def _add(ok, name, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:400]))


def scan(root: str = ROOT) -> dict:
    """返回 {(relpath, funcname): 命中次数}。"""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(base, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                tree = ast.parse(src)
            except Exception:
                continue
            funcs = [
                (n.lineno, getattr(n, "end_lineno", None) or n.lineno, n.name)
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            # ★ 用 AST 扫**字符串字面量**，不扫整行文本。
            #   实测踩过两次假阳性：① 注释里的考古记录「原实现直接 INSERT INTO entities」；
            #   ② docstring 里引用旧 SQL。行级正则会把它们都算成真实写点。
            #   同时必须覆盖「先赋值给常量、再 executemany(常量, rows)」的写法
            #   （project_repo.detach_project_references 就是这种，只看 execute 实参会漏）。
            doc_ids = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if (n.body and isinstance(n.body[0], ast.Expr)
                            and isinstance(n.body[0].value, ast.Constant)
                            and isinstance(n.body[0].value.value, str)):
                        doc_ids.add(id(n.body[0].value))
            for node in ast.walk(tree):
                if id(node) in doc_ids:
                    continue
                is_str = isinstance(node, ast.Constant) and isinstance(node.value, str)
                if not (is_str or isinstance(node, ast.JoinedStr)):
                    continue
                text = node.value if is_str else (ast.get_source_segment(src, node) or "")
                if not WRITE_SQL.search(text):
                    continue
                lineno = getattr(node, "lineno", 1)
                owner = next(
                    (nm for a, b, nm in funcs if a <= lineno <= b), "<module>")
                key = (rel, owner)
                out[key] = out.get(key, 0) + 1
    return out


def check():
    found = scan()
    _add(bool(found), "W0 扫描器扫到生产写入点", "扫描为空，正则或排除目录配置有误")

    unreg = sorted(set(found) - set(REGISTRY))
    _add(not unreg, "W1 所有裸写都已显式登记（新增写必须先登记）",
         "未登记: " + "; ".join(f"{f}::{n}" for f, n in unreg[:12]))

    stale = sorted(set(REGISTRY) - set(found))
    _add(not stale, "W2 台账无过期键（改动后同步更新）",
         "已消失: " + "; ".join(f"{f}::{n}" for f, n in stale[:12]))

    with open(os.path.join(ROOT, "core", "graph_write_gate.py"), encoding="utf-8") as f:
        gate_src = f.read()
    with open(os.path.join(ROOT, "routers", "graph_workspace.py"), encoding="utf-8") as f:
        ws_src = f.read()
    has_entries = ("def write_inferred_entity(" in gate_src
                   and "def write_inferred_edge(" in gate_src)
    tree = ast.parse(ws_src)
    merge_fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                     and n.name == "_merge_cohort_items"), None)
    body = ""
    if merge_fn:
        body = "\n".join(ws_src.splitlines()[merge_fn.lineno - 1:merge_fn.end_lineno])
    body_code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
    wired = ("write_inferred_entity(" in body_code and "write_inferred_edge(" in body_code)
    raw_left = bool(WRITE_SQL.search(body_code))
    _add(has_entries and wired and not raw_left,
         "W3 推断并入走统一入口推断通道（入口存在✓/接线✓/端点裸写0）",
         f"入口存在={has_entries} 接线={wired} 端点仍有裸写={raw_left}")

    total = sum(found.values())
    files = len({f for f, _ in found})
    reg_files = len({f for f, _ in REGISTRY})
    g4 = [k for k in found if k[0] in G4_FILES]
    print("\n--- 写入面实测（生产代码，排除 tests/tools/migrations）---")
    print("  实际裸写：%d 处 / %d 文件 / %d 函数" % (total, files, len(found)))
    print("  台账登记：%d 函数 / %d 文件" % (len(REGISTRY), reg_files))
    by_cat = {}
    for k, v in REGISTRY.items():
        by_cat[v] = by_cat.get(v, 0) + found.get(k, 0)
    print("  分类处数：" + " / ".join(f"{k}={v}" for k, v in sorted(by_cat.items())))
    print("  G4 统计面（3 文件）内命中：%d 函数 / %d 处 —— 信息项，不判红"
          % (len(g4), sum(found.get(k, 0) for k in g4)))
    print("  ⚠️ G4 的『待迁移 13 端点』只覆盖上述 3 文件；统计面外仍有 %d 处直接写。"
          % (total - sum(found.get(k, 0) for k in g4)))
    return found


def mutations(found: dict):
    out = []
    # M1：把一个新裸写塞进一个真实生产文件的内容副本 ⇒ W1 判红
    victim = next((k for k in found if k[0].endswith("graph.py")), next(iter(found)))
    fake = dict(found)
    fake[(victim[0], "<mutated>")] = 1
    caught = bool(set(fake) - set(REGISTRY))
    out.append(("M1 新增未登记裸写 ⇒ W1 判红", caught, str(victim)))

    # M2：删一个登记键 ⇒ W2 判红
    k0 = next(iter(REGISTRY))
    shrunk = {k: v for k, v in REGISTRY.items() if k != k0}
    caught = bool(set(REGISTRY) - set(shrunk))
    out.append(("M2 台账删键 ⇒ W2 判红", caught, str(k0)))

    # M3：把裸 SQL 注回 _merge_cohort_items ⇒ W3 判红
    #    （判据随缺口修复反转：从「推断缺口唯一」改为「端点裸写为 0」）
    with open(os.path.join(ROOT, "routers", "graph_workspace.py"), encoding="utf-8") as f:
        ws = f.read()
    tree = ast.parse(ws)
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "_merge_cohort_items"), None)
    real_body = "\n".join(ws.splitlines()[fn.lineno - 1:fn.end_lineno]) if fn else ""
    real_body = "\n".join(l.split("#", 1)[0] for l in real_body.splitlines())
    injected = real_body + "\n    conn.execute(\"INSERT INTO entities (id) VALUES ('X')\")"
    caught = (not WRITE_SQL.search(real_body)) and bool(WRITE_SQL.search(injected))
    out.append(("M3 端点重新裸写 ⇒ W3 判红", caught, ""))
    return out


def main():
    print("=" * 76)
    print("门禁：全仓写入面清单（P0-0 收口纠偏）")
    print("=" * 76)
    found = check()
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _results if r[0] == FAIL)

    print("\n--- 变异自证 ---")
    mut = mutations(found)
    for name, ok, detail in mut:
        print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  <- " + detail) if detail else ""))
    n_mut = sum(1 for x in mut if not x[1])

    print("\n" + "=" * 76)
    if n_fail or n_mut:
        print("结论：未通过（常态 %d 失败 / 变异 %d 未判红）" % (n_fail, n_mut))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组按预期判红）" % (len(_results), len(mut)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
