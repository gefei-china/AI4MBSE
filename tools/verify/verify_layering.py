#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：限制层绕过**增量**（P1-10，2026-10-04）

## 为什么是「卡增量」而不是「清存量」

实测（本仓真实分布）：

| 项 | 数字 |
|---|---|
| `routers/` 里 `conn.execute("SELECT…")` | **425 处**（不是原估的 1034 —— 那个数把 `db_session` 等一并计入了） |
| 最集中的文件 | `glossary.py` 80 处 |
| Top 8 文件合计 |约 270 处（占63%） |
| 按表分布 | 极散：`ontology_types` 41 / `glossary_concepts` 29 / `entities` 29 / … / 142 处无 FROM |

⇒ **分布高度分散**且每一处都伴随各自的业务语义（过滤条件、权限过滤、
多表 join）。把 425 处全部搬进仓储层：
- 收益是"架构整洁"——**不产生任何用户可感知价值**；
- 风险是**行为漂移**：425 处里每处都要重新验证权限过滤/分支过滤是否保住，
  而这些过滤正是多租户隔离的边界（MEMORY 里记着一次「只看了 33 个风险端点、
  逐个核对全是误报」的教训）。

⇒ **本项的正解是卡增量，不动存量**：
1. 记录**当前基线**（425），门禁断言"不得增长"；
2. 新增端点若直连 SQL，必须**显式登记豁免**并写明理由
   （避免"为了绕过门禁随手加一行豁免"⇒ 豁免要带原因且计数）。
3. 已有 `repositories/` 的表**优先用仓储**（不改存量，但给新代码定规矩）。

这样成本 ≈ 0，而风险单调下降；等真正需要重构时（多副本 / 换ORM）再动存量。

## 为什么门禁不能只报数

只报"当前 425"没用 —— 下次变成 426 谁 care？
必须：**① 钉住基线数字**（增长即红）；② 豁免必须带reason（非空）。
这两条任一都能被"顺手绕过"，所以两条都断言。
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]

# ── 基线（2026-10-04 实测；改动此值必须同步更新并说明理由）──────────────
#
# ⚠️ **基线口径必须与 scan_direct_sql() 完全一致**，否则门禁形同虚设：
#   我第一版把基线设成 425（那是另一套口径的数：把三引号 SQL / f-string /
#   拼接 SQL 也算进去了），而门禁只认 `conn.execute("SELECT… FROM …")`
#   这一种形态 ⇒ 实测只有 292 ⇒ 基线虚高 133，**门禁一上线就是"虚设"**。
#   ⇒ 这与MEMORY 里「准确率门禁必须先证明输入在两个环境等价」同源：
#   **阈值必须由判据自己测出来，不能凭印象填。**
BASELINE_DIRECT_SQL = 292   # 由 scan_direct_sql() 实测得出（2026-10-04）
BASELINE_FILE = "tools/verify/verify_layering.py"
ALLOW_FILE = "tools/verify/layering_exemptions.txt"

# 与 verify_hyde_gate 同款：`conn.execute("…")` 里带 FROM 的才计入
# （INSERT/UPDATE/DELETE 属仓储层天然职责，不算"读取绕过"）。
SQL_RE = re.compile(r'conn\.execute\(\s*f?["\']([^"\']*)', re.I)
FROM_RE = re.compile(r"\bFROM\s+(\w+)", re.I)


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


def scan_direct_sql(root=ROOT):
    """扫描 routers/ 里的直连读SQL，返回 (总数, {文件: 条数}, {表: 条数})。"""
    total = 0
    by_file = {}
    by_table = {}
    for p in sorted(glob.glob(os.path.join(root, "routers", "**", "*.py"), recursive=True)):
        src = open(p, encoding="utf-8").read()
        n = 0
        for m in SQL_RE.finditer(src):
            sql = m.group(1)
            if not FROM_RE.search(sql):
                continue        # 只统计"读"
            n += 1
            mt = FROM_RE.search(sql)
            by_table[mt.group(1)] = by_table.get(mt.group(1), 0) + 1
        if n:
            by_file[os.path.relpath(p, root)] = n
            total += n
    return total, by_file, by_table


def read_exemptions(root=ROOT):
    """读豁免清单，返回 {原因非空: 条数} 的统计与空原因的条目。"""
    path = os.path.join(root, ALLOW_FILE)
    if not os.path.exists(path):
        return [], []
    items, bad = [], []
    for i, ln in enumerate(open(path, encoding="utf-8"), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        # 格式：<路径>:<行号> <理由>
        m = re.match(r"^(\S+):(\d+)\s*(.*)$", ln)
        if not m:
            bad.append((i, ln[:60], "格式不符（应为 路径:行号 理由）"))
            continue
        reason = m.group(3).strip()
        if not reason:
            bad.append((i, ln[:60], "缺理由"))
            continue
        items.append((m.group(1), int(m.group(2)), reason))
    return items, bad


def t_baseline(root=ROOT):
    print("\n=== L1 直连读 SQL 未增长（卡增量）===")
    total, by_file, by_table = scan_direct_sql(root)
    ok = _rec("L1a 基线文件存在（改动它必须说明理由）",
              os.path.exists(os.path.join(root, BASELINE_FILE)), BASELINE_FILE)
    # ⚠️ 基线必须**由判据自己测出来**：现值 vs 实测值若差很多，
    #   说明基线是另一套口径填的 ⇒ 门禁虚设（第一版就踩了：填 425、实测 292）。
    ok &= _rec("L1b 基线与实测一致（差值 ≤ 3，防止口径漂移导致门禁虚设）",
               abs(total - BASELINE_DIRECT_SQL) <= 3,
               "基线=%d 实测=%d ⇒ 口径不一致，请用 scan_direct_sql() 重新测基线"
               % (BASELINE_DIRECT_SQL, total))
    ok &= _rec("L1c routers 里直连读 SQL = %d，未超过基线 %d"
               % (total, BASELINE_DIRECT_SQL),
               total <= BASELINE_DIRECT_SQL,
               "超出 %d 处；分布 Top5=%s"
               % (total - BASELINE_DIRECT_SQL,
                  sorted(by_file.items(), key=lambda kv: -kv[1])[:5]))
    # 关键反证：判据不能恒真 —— 若扫描器一个都扫不到，基线检查就形同虚设
    ok &= _rec("L1d 扫描器确实扫到了东西（不是 0 ⇒ 判据非空转）",
               total > 100, "total=%d" % total)
    print("     Top5 文件：%s"
          % [(f, n) for f, n in sorted(by_file.items(), key=lambda kv: -kv[1])[:5]])
    print("     Top5 表：%s"
          % [(t, n) for t, n in sorted(by_table.items(), key=lambda kv: -kv[1])[:5]])
    return ok


def t_exemptions(root=ROOT):
    print("\n=== L2 豁免清单必须带理由 ===")
    ok = True
    items, bad = read_exemptions(root)
    ok &= _rec("L2a 豁免清单条目都有理由（防随手加一行绕过门禁）",
               not bad, bad[:3])
    if items:
        ok &= _rec("L2b 豁免条目数合理（%d 条）" % len(items), len(items) <= 60,
                   len(items))
        print("     豁免示例：%s" % [(a, b) for a, b, _c in items[:3]])
    else:
        print("     （当前无豁免条目）")
    return ok


def t_repo_layer_exists(root=ROOT):
    print("\n=== L3 仓储层确实存在且被使用 ===")
    ok = True
    repos = glob.glob(os.path.join(root, "repositories", "*.py"))
    ok &= _rec("L3a repositories/ 有模块", len(repos) >= 3, "n=%d" % len(repos))
    used = 0
    for p in glob.glob(os.path.join(root, "routers", "**", "*.py"), recursive=True):
        src = open(p, encoding="utf-8").read()
        used += len(re.findall(r"from repositories\.|import repositories\.", src))
    ok &= _rec("L3b routers 里有代码在用仓储层（不是空架子）", used >= 3, "used=%d" % used)
    return ok


# ── 变异自证 ─────────────────────────────────────────────────────────────
def mutations(root=ROOT):
    print("\n--- M1：注入 1 处新的直连 SQL ⇒ L1 判红 ---")
    _IN_MUT[0] = True
    fake_dir = os.path.join(root, "routers", "_mut_probe")
    os.makedirs(fake_dir, exist_ok=True)
    f = os.path.join(fake_dir, "z_probe.py")
    open(f, "w", encoding="utf-8").write(
        "# -*- coding: utf-8 -*-\n"
        "def probe(conn):\n"
        "    return conn.execute(\"SELECT id FROM entities WHERE id=?\").fetchall()\n")
    try:
        _rec("M1 注入新直连 SQL ⇒ L1 判红", not t_baseline(root))
    finally:
        import shutil
        shutil.rmtree(fake_dir, ignore_errors=True)
        sys.modules.pop("verify_layering", None)
    _IN_MUT[0] = False

    print("\n--- M2：豁免条目缺理由 ⇒ L2 判红 ---")
    _IN_MUT[0] = True
    allow = os.path.join(root, ALLOW_FILE)
    backup = None
    if os.path.exists(allow):
        backup = open(allow, encoding="utf-8").read()
    open(allow, "w", encoding="utf-8").write("routers/meta.py:123\n")
    try:
        _rec("M2 豁免缺理由 ⇒ L2 判红", not t_exemptions(root))
    finally:
        if backup is not None:
            open(allow, "w", encoding="utf-8").write(backup)
        else:
            os.remove(allow)
    _IN_MUT[0] = False

    print("\n--- M3：扫描器啥都扫不到（判据空转）⇒ L1c 判红 ---")
    _IN_MUT[0] = True
    empty_root = os.path.join(root, "_mut_empty")
    os.makedirs(os.path.join(empty_root, "routers"), exist_ok=True)
    open(os.path.join(empty_root, "routers", "a.py"), "w").write("x=1\n")
    try:
        _rec("M3 扫描器空转 ⇒ L1d 判红", not t_baseline(empty_root))
    finally:
        import shutil
        shutil.rmtree(empty_root, ignore_errors=True)
    _IN_MUT[0] = False


def main():
    ok = True
    ok &= t_baseline()
    ok &= t_exemptions()
    ok &= t_repo_layer_exists()

    print("\n" + "=" * 68)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (len(_results), len(_results) - n_fail, n_fail))
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    if n_fail:
        print("结论：门禁未通过")
        return 1

    print("\n--- 变异自证 ---")
    _MUT_ROWS.clear()
    mutations()
    verdicts = [r for r in _MUT_ROWS if re.match(r"^M\d", r[1])]
    subs = [r for r in _MUT_ROWS if not re.match(r"^M\d", r[1])]
    n_red = sum(1 for r in verdicts if r[0] == PASS)
    print("变异组：%d，判红成功：%d（变异期子断言 %d 条，其中 %d 条转红）"
          % (len(verdicts), n_red, len(subs), sum(1 for r in subs if r[0] == FAIL)))
    print("-" * 68)
    for st, name, detail in verdicts:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    print("=" * 68)
    if n_red != len(verdicts) or not verdicts:
        print("结论：门禁未通过（%d/%d 变异未判红）" % (len(verdicts) - n_red, len(verdicts)))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）"
          % (len(_results), len(verdicts)))
    print("注：本项**卡增量不卡存量** —— 实测 425 处高度分散（原估 1034 是把 db_session"
          "也计入了），清存量的收益是「架构整洁」而非用户可感知价值，"
          "而 425 处里每一处都带着权限/分支过滤（多租户边界），重写风险 > 收益。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
