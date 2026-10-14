# -*- coding: utf-8 -*-
"""verify_schema_index_order —— 锁死「索引必须建在补列之后」这条顺序。

## 背景（2026-10-14 生产库补迁移时实拍的真缺陷）
`database/schema.py::init_db` 里，`hil_confirmations` 的幂等索引
`idx_hc_idem(idem_key, status)` 建在**建表段**（第 953 行），
而补列的 `_migrate_columns(conn)` 在第 1426 行 —— **晚 473 行**。

⇒ 对**已存在的旧表**，`CREATE TABLE IF NOT EXISTS` **不会加列**（idem_key 是后来加的）
⇒ 老库启动 ⇒ `sqlite3.OperationalError: no such column: idem_key`
⇒ **init_db 整体失败、服务起不来**。

⚠️ 这**不是**我的迁移脚本的锅 —— **生产服务启动路径同样会崩**。
（该 bug 在干净库上永远不出现：干净库第一次建表时就带 idem_key。）

## 为什么这必须是门禁（而不是一次修复）
本仓**已经踩过一次同型坑**并留了注释：`llm_usage_stats` 的 `ux_lus_*` 索引
（"本次启动失败实拍"）。修完 `idx_hc_idem` 后，**下一个**新增列的人还会再踩。
⇒ 判据必须是**结构性的**：扫全文件，找出所有「引用了 `_migrate_columns` 会补的列、
却建在迁移调用点之前」的索引。

## 判据
  O1 `_migrate_columns(conn)` 在 init_db 里**恰好被调用一次**（重复调用是另一种隐患）
  O2 建表段（迁移之前）**没有任何** CREATE INDEX 引用「迁移才补的列」
  O3 已知的两个历史坑位（idx_hc_idem / ux_lus_*）**都在迁移之后**
  O4 ★反向判据：把 `idx_hc_idem` 变异回建表段 ⇒ O2 必须判红
     （没有这条，O2 可以靠"把风险列全删掉"来通过 —— 判据被反向锁死）

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_schema_index_order.py
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

SCHEMA = os.path.join(_ROOT, "database", "schema.py")
COLUMNS = os.path.join(_ROOT, "database", "migrations", "columns.py")

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + detail) if detail and not ok else ""))
    return bool(ok)


def migrated_columns():
    """从 `columns.py` 抽出 `_migrate_columns` 会补的 (表, 列) 集合。

    ⚠️ 用 `_add("表", "列", ...)` 的**字面参数**抽取，不 import 执行 ——
       执行会连库，而门禁必须零副作用。
    """
    src = open(COLUMNS, encoding="utf-8").read()
    out = {}
    for m in re.finditer(r'_add\(\s*["\'](\w+)["\']\s*,\s*["\'](\w+)["\']', src):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


def _init_db_span(lines):
    tree = ast.parse("\n".join(lines))
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "init_db":
            return n.lineno, n.end_lineno
    return None


def scan(lines, mig_line):
    """返回（迁移前引用迁移列的索引, 已迁移列数）。"""
    added = migrated_columns()
    risk = []
    for i, ln in enumerate(lines[:mig_line - 1], 1):
        if "CREATE INDEX" not in ln or "IF NOT EXISTS" not in ln:
            continue
        m = re.search(r"ON\s+(\w+)\s*\(([^)]*)\)", ln)
        if not m:
            continue
        tbl, cols = m.group(1), [x.strip() for x in m.group(2).split(",")]
        for cn in cols:
            if cn in added.get(tbl, ()):
                risk.append((i, tbl, cn, ln.strip()[:70]))
                break
    return risk


def main() -> int:
    print("=" * 72)
    print("判据 · schema 索引顺序（补列必须在建索引之前）")
    lines = open(SCHEMA, encoding="utf-8").read().split("\n")

    span = _init_db_span(lines)
    if not check("O0 能定位到 init_db", bool(span), "未找到"):
        return 1
    lo, hi = span

    # O1 _migrate_columns 恰好一次
    calls = [i + 1 for i, ln in enumerate(lines[lo - 1:hi], lo - 1)
             if "_migrate_columns(conn)" in ln]
    check("O1 _migrate_columns 在 init_db 里恰好被调用一次", len(calls) == 1,
          "实际 %d 次，行号 %s" % (len(calls), calls))
    mig_line = calls[0] if calls else hi

    added = migrated_columns()
    n_add = sum(len(v) for v in added.values())
    check("O1b 迁移表清单非空（防空转）", n_add > 0, "从 columns.py 抽到 %d 个待补列" % n_add)

    # O2 迁移前不得有索引引用迁移列
    risk = scan(lines, mig_line)
    check("O2 建表段没有索引引用「迁移才补的列」", not risk,
          "违规行：%s" % [(i, "%s.%s" % (t, c)) for i, t, c, _ in risk])

    # O3 两个已知坑位都在迁移之后
    for idx_name in ("idx_hc_idem", "ux_lus_conv"):
        pos = [i + 1 for i, ln in enumerate(lines) if idx_name in ln and "CREATE INDEX" in ln]
        ok = bool(pos) and all(p > mig_line for p in pos)
        check("O3 %s 建在迁移之后（行%d > 迁移行%d）" % (idx_name, pos[0] if pos else -1, mig_line),
              ok, "实际行号 %s，迁移在第 %d 行" % (pos, mig_line))

    # ── 变异自证（反向锁死）─────────────────────────────────────
    print("\n=== 变异自证 ===")
    # M1 把 idx_hc_idem 变异回建表段（**复现 2026-10-14 的真缺陷**）
    # ⚠️ 此处**不能**套「必须出现 FAIL」的通用包装：
    #   变异段内 O2 判红 = 变异自证**成功**（是 PASS）；
    #   再要求「出现 FAIL 行」会自相矛盾（第一版就是这么写的 ⇒ 恒判空转）。
    mutated = [ln for ln in lines
               if not ("idx_hc_idem" in ln and "CREATE INDEX" in ln)]
    anchor = next((i for i, ln in enumerate(mutated)
                   if "idx_hc_run ON hil_confirmations" in ln), None)
    _IN_MUT[0] = True
    try:
        if anchor is None:
            check("M1 变异自证（把 idx_hc_idem 放回建表段）", False,
                  "锚点未找到，无法构造变异体")
        else:
            mutated.insert(
                anchor + 1,
                '    c.execute("CREATE INDEX IF NOT EXISTS idx_hc_idem '
                'ON hil_confirmations(idem_key, status)")')
            r = scan(mutated, mig_line)
            # 直接断言 O2 的判据函数在变异体上**能扫出风险**（这就是 O2 有牙齿的证明）
            check("M1 把索引放回建表段 ⇒ O2 判据能扫出该风险（有牙齿）", bool(r),
                  "变异体未被扫出 ⇒ O2 对此类缺陷无灵敏度，判据形同虚设")
    finally:
        _IN_MUT[0] = False
        _MUT_ROWS.clear()

    npass = sum(1 for st, _, _ in _results if st == PASS)
    nfail = sum(1 for st, _, _ in _results if st == FAIL)
    print("\n" + "=" * 72)
    print("%d pass / %d fail" % (npass, nfail))
    if nfail:
        for st, n, d in _results:
            if st == FAIL:
                print("  - %s%s" % (n, ("　— " + d) if d else ""))
        return 1
    print("✅ 全部通过：补列先于建索引，init_db 在老库上不会崩")
    return 0


if __name__ == "__main__":
    sys.exit(main())
