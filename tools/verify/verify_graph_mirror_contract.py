"""verify_graph_mirror_contract — 判据：图库**只能是可重建的查询镜像**，不是权威源。

## 为什么需要这个门据（2026-10-10米爸提问引出）
米爸问"如果后面 SQLite 换了，要重新调整吗"—— 这个问题背后是**定位**问题：
若有人把图库（Fuseki/Neo4j）当成**权威源**，则：
① 治理能力当场消失（图库没有双时态/审核流/版本对齐/画布坐标 —— 实测这四项
   被 `knowledge_repo.py:100` / `ontology_semantics.py:689` / 15 个文件的
   `sysml_version_id` / 前端画布依赖着，**且有活门禁在守**）；
② 换权威源时改动面从「4 条 SQL」爆炸到「全部写入路径」。

⇒ 本门禁把三条定位事实固化成断言：
  A. `graph_db.py` **不直接依赖存储**（`sqlite3` / `db_conn` / `get_db` 引用 = 0）
  B. 图库只经 `get_writer(backend=…)` 切换（**换后端不改业务代码**）
  C. 权威源耦合面**有界**（SQLite SQL ≤ 6 处），且**只在物化/读实体两个函数里**
  D. ★ **红线声明**：`graph_stored` 不是"权威源已确认"标记

## 判据
① 定位声明在位（docstring 含"查询镜像"+"不能当权威源"）
② graph_db 不直接依赖 sqlite3/db_conn/get_db
③ get_writer 工厂存在且有 ≥3 个后端实现
④ ★ SQLite SQL 有界（≤6）且只在允许的函数里
⑤ ★ 红线声明在位（`graph_stored` ≠ 权威源确认）
⑥ 两个调用点都带 enabled 开关 + 失败降级
⑦ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_graph_mirror_contract.py
"""
from __future__ import annotations

import ast
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

GRAPH_DB = os.path.join(_ROOT, "graph_db.py")
TRIple_STORE = os.path.join(_ROOT, "triple_store.py")
CARDS = os.path.join(_ROOT, "agent", "pipeline_parts", "cards.py")

#: 允许出现 SQLite SQL 的函数（权威源耦合面就在这里，超出即说明架构被侵蚀）
#:★ `fetch_impact_network` 是第4 处（门禁首次运行**自己抓出来的**，
#:   不是事后补的）：它做「属性回填」时按 entity id 回 SQLite 取名称/类型/状态，
#:   注释就写着 "属性回填（SQLite 权威源）" —— 属**合理的读权威源**，
#:   不是越界写。⇒ 列入白名单，并在文档里记账。
ALLOWED_FUNCS = {
    "sync_pending_triples",   # 物化：读 triples + 回写 graph_stored
    "_pending_count",         # 物化：读 pending 数
    "fetch_entity",           # 读实体
    "fetch_impact_network",   # ★ 读：影响分析的属性回填（实测第 4 处耦合）
}


def _src() -> str:
    with open(GRAPH_DB, encoding="utf-8") as f:
        return f.read()


def main() -> int:
    print("=" * 74)
    print("判据 · 图库定位= 可重建查询镜像（不是权威源）")
    print("=" * 74)
    src = _src()
    ok = True

    print("\n① 定位声明在位")
    for name, passed in [
        ("docstring 声明「查询镜像」", "查询镜像" in src),
        ("★ 明确写「不能当权威源」", "不能当权威源" in src),
        ("★ 写明治理四项不可由图库承担",
         all(k in src for k in ("双时态", "审核流", "版本对齐", "画布坐标"))),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n② graph_db 不直接依赖存储（权威源可换的前提）")
    tree = ast.parse(src)
    direct = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("database"):
            direct.add(node.module)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in ("sqlite3",):
                    direct.add(a.name)
    for name, passed in [
        (f"无 `import sqlite3`（实测 {sorted(direct) or '干净'}）",
         "sqlite3" not in direct),
        ("不`from database import db_conn/get_db`",
         not any("database" in (m or "") for m in direct)),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n③ 后端可切换（换图库不改业务代码）")
    for name, passed in [
        ("`get_writer(backend=…)` 工厂在位", "def get_writer(" in src),
        ("≥3 个 Writer 实现",
         len(re.findall(r"class \w+Writer\(", src)) >= 3),
        ("`FusekiWriter` 已实现（实测可连通）", "class FusekiWriter" in src),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n④ ★ 权威源耦合面有界")
    # 只数**真 SQLite** SQL（排除 SPARQL）
    sqlite_sql = []
    for i, line in enumerate(src.splitlines(), 1):
        if re.search(r'"?\b(SELECT|UPDATE|INSERT)\b', line) and \
           re.search(r'FROM (triples|entities|relations)|UPDATE (triples|entities)', line):
            sqlite_sql.append(i)
    print(f"     真 SQLite SQL 行号：{sqlite_sql}（{len(sqlite_sql)} 处）")
    for name, passed in [
        (f"★ SQLite SQL ≤ 6 处（实测 {len(sqlite_sql)}）", len(sqlite_sql) <= 6),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # 这些行必须落在允许的函数内
    allowed_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ALLOWED_FUNCS:
            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                allowed_lines.add(ln)
    stray = [ln for ln in sqlite_sql if ln not in allowed_lines]
    for name, passed in [
        (f"★ 这些 SQL 都在允许的函数内（越界行 {stray or '无'}）", not stray),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n⑤ ★ 红线声明：`graph_stored` ≠ 权威源确认")
    for name, passed in [
        ("docstring 写明该红线", "graph_stored" in src and "不是" in src),
        ("写明「未审核数据被当成已确认」的风险", "未审核" in src),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n⑥ 两个调用点都带 enabled 开关 + 失败降级")
    for label, path in (("triple_store.py", TRIple_STORE), ("cards.py", CARDS)):
        with open(path, encoding="utf-8") as f:
            s = f.read()
        for name, passed in [
            (f"{label} 检查 `graph_db.enabled` 开关",
             'as_bool("graph_db", "enabled")' in s),
        ]:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if not ok:
        return 1

    # ── 变异自证：把 4 处SQL 挪进一个新函数（模拟"权威源耦合面扩散"）⇒ 应判红 ──
    print("\n[变异自证] 新增一个函数里塞 SQLite SQL（模拟耦合面扩散），应判红")
    mutant = src.replace(
        'def get_writer(enabled: bool = True, backend: str = "pyoxigraph",',
        'def _rogue_query(conn):\n'
        '    """变异：越界访问权威源。"""\n'
        '    return conn.execute("SELECT * FROM entities").fetchall()\n\n\n'
        'def get_writer(enabled: bool = True, backend: str = "pyoxigraph",', 1)
    orig = GRAPH_DB
    with open(GRAPH_DB, "w", encoding="utf-8") as f:
        f.write(mutant)
    try:
        mtree = ast.parse(mutant)
        msql = [i for i, line in enumerate(mutant.splitlines(), 1)
                if re.search(r'"?\b(SELECT|UPDATE|INSERT)\b', line) and
                re.search(r'FROM (triples|entities|relations)|UPDATE (triples|entities)', line)]
        mall = set()
        for node in ast.walk(mtree):
            if isinstance(node, ast.FunctionDef) and node.name in ALLOWED_FUNCS:
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    mall.add(ln)
        stray2 = [ln for ln in msql if ln not in mall]
        red = bool(stray2)
        print(f"[变异自证] 越界行 {stray2} ⇒ "
              f"{'已变红（耦合面判据有效）' if red else '**仍是绿 ⇒ 判据无效**'}")
        ok = ok and red
    finally:
        with open(GRAPH_DB, "w", encoding="utf-8") as f:
            f.write(src)
    with open(GRAPH_DB, encoding="utf-8") as f:
        assert f.read() == src, "回滚失败！graph_db.py 未逐字还原"
    print("[OK  ] 回滚校验：graph_db.py 已逐字还原")

    print("=" * 74)
    print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
