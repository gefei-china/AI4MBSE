# -*- coding: utf-8 -*-
"""一次性回填：`tool_call_logs.failure_kind` 历史空值（P0-2，2026-10-30）。

## 为什么这是"历史"而不是"在跑的 bug"
真库取证：`ok=0 且 failure_kind=''` 共 **273 行，全部 ≤ 2026-10-18**；10-19 起填充率 **100%**。
根因是写入侧 `_log_tool_call` 把分类器**嵌套调用了两次**
（`classify(classify(text, ok=False))` —— 外层 ok 默认 True 直接返 `''`），
该 bug 已于 2026-10-18 修掉。所以本脚本做的事只有一件：**把历史行的归因补齐**，
让 `core/attribution.py` 的 kind 维度对全量数据可用（此前它自己 docstring 都写着"历史全空"）。

## 三条纪律
1. **只填空值**：`WHERE ok=0 AND COALESCE(failure_kind,'')=''`。已有分类**一字不动**
   （它们来自当时真实的写入路径，属事实记录；用今天的词表去改历史结论 = 篡改证据）。
2. **用同一个分类器**：`services.failure_class.classify(result, ok=False)`。
   本脚本**不新增任何词表** —— 词表缺口已在 `services/failure_class.py`（单一真源）里补过。
3. **可重跑**：第二次跑应报 0 行（幂等）。

## 用法
    python tools/_failure_kind_backfill.py            # 预演（默认，只报告不写）
    python tools/_failure_kind_backfill.py --apply    # 真正写库（写前自动备份计数）
"""
import argparse
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.failure_class import classify          # noqa: E402

_WHERE = "ok=0 AND COALESCE(failure_kind,'')=''"


def _rows(conn, since="", until=""):
    q = "SELECT id, tool_name, COALESCE(result,'') AS result FROM tool_call_logs WHERE " + _WHERE
    args = []
    if since:
        q += " AND date(created_at) >= ?"
        args.append(since)
    if until:
        q += " AND date(created_at) <= ?"
        args.append(until)
    q += " ORDER BY id"
    return [dict(r) for r in conn.execute(q, tuple(args)).fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--since", default="", help="只处理 date(created_at) >= 该日期（YYYY-MM-DD）")
    ap.add_argument("--until", default="", help="只处理 date(created_at) <= 该日期")
    a = ap.parse_args()

    from database import db_conn
    with db_conn() as conn:
        rows = _rows(conn, a.since, a.until)
        print("待回填 %d 行（口径：tool_call_logs.%s）" % (len(rows), _WHERE))
        if not rows:
            print("无需处理（幂等：可能已回填过）")
            return 0

        dist = collections.Counter()
        plan = []
        for r in rows:
            k = classify(r["result"], ok=False)
            assert k, "分类器返回空串 —— 违背'失败必有类别'的不变量（id=%s）" % r["id"]
            dist[k] += 1
            plan.append((r["id"], k, r["tool_name"], (r["result"] or "")[:48].replace("\n", " ")))

        print("\n回填后分布：")
        for k, v in dist.most_common():
            print("  %-16s %3d  (%.1f%%)" % (k, v, 100.0 * v / len(rows)))

        print("\n前 8 条样例：")
        for pid, k, tool, txt in plan[:8]:
            print("  #%-6d %-14s %-26s %s" % (pid, k, str(tool)[:26], txt))

        if not a.apply:
            print("\n[预演] 未写库。加 --apply 执行。")
            return 0

        # 写库（只改空值行；不覆盖任何既有分类）
        n = 0
        for pid, k, _tool, _txt in plan:
            n += conn.execute(
                "UPDATE tool_call_logs SET failure_kind=? WHERE id=? AND " + _WHERE,
                (k, pid)).rowcount
        print("\n已更新 %d 行" % n)

        # 审计留痕（可追溯：谁在什么时候把历史归因补成了什么分布）
        try:
            from core.audit import audit
            audit("system", "tool_failure_kind_backfill",
                  "回填 tool_call_logs.failure_kind 历史空值 %d 行；分布=%s；"
                  "口径=ok=0 且 kind 为空；分类器=services.failure_class"
                  % (n, dict(dist)), conn=conn)
            print("已写审计（tool_failure_kind_backfill）")
        except Exception as e:      # noqa: BLE001
            print("审计写入失败（不阻断）：%s" % str(e)[:120])

        left = conn.execute("SELECT COUNT(*) FROM tool_call_logs WHERE " + _WHERE).fetchone()[0]
        print("回填后剩余空值行：%d（期望 0）" % left)
        return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
