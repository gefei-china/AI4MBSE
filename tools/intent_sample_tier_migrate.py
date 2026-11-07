# -*- coding: utf-8 -*-
"""样本档位一次性归位（第 19 轮）：把 `suggested` 里已被定档的 silver / synth 拆出来。

## 为什么必须有这一步
`intent_samples.status` 原本只有 `new/suggested/confirmed/rejected` 四值，而**标签来源**
（双视图一致 / 手写改写 / 路由器弱建议）散落在 `created_by` 与 `source` 两个隐式字段里。
后果（真库实测 2026-11-07）：

    491 条 suggested = 347 条银标(created_by='dual_label_v1') + 17 条合成(source='aug') + 127 条真待审

页面把 491 全算成「待确认」⇒ **人工待办数虚高 3.8 倍**；而评测跑批器
（`tools/intent_eval_snapshot.py`）本来就按 gold/silver/synth 三集分报 ⇒ 状态与评测口径不同构。

本脚本按 `repositories.intent_sample_repo.tier_of()` 的**同一判据**归位，不另写一套规则
（判据两处写必然漂移：这类"一次性迁移"最容易在下一次新增来源时失效）。

## 纪律
1. **默认 dry-run**；`--apply` 才写。写前打印每档变化明细。
2. **只动 `new` / `suggested`**：`confirmed`（人工金标）与 `rejected`（人工驳回）**永不改**。
3. **幂等**：跑第二次 0 变更。
4. 不修改任何 `intent` / `hit_*` / `note` 字段 —— 本脚本只做档位归位，**不动标签内容**。

## 用法
    python tools/intent_sample_tier_migrate.py            # 预演
    python tools/intent_sample_tier_migrate.py --apply    # 写库
"""
from __future__ import annotations

import collections
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(argv: list) -> int:
    apply = "--apply" in argv
    db = os.path.join(ROOT, "mbse.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        from repositories.intent_sample_repo import tier_of
        rows = conn.execute(
            "SELECT id, status, source, created_by FROM intent_samples "
            "WHERE status IN ('new','suggested')").fetchall()
        before = collections.Counter(r["status"] for r in conn.execute(
            "SELECT status FROM intent_samples").fetchall())

        moves = []
        for r in rows:
            tgt = tier_of(r["source"] or "", r["created_by"] or "")
            if tgt != r["status"]:
                moves.append((r["id"], r["status"], tgt))
        dist = collections.Counter((a, b) for _, a, b in moves)

        print("参与归位的行：%d（仅 new/suggested）；需变更 %d" % (len(rows), len(moves)))
        for (a, b), n in sorted(dist.items()):
            print("  %-10s → %-10s %d" % (a, b, n))
        if not apply:
            print("\n[预演] 未写库。加 --apply 执行。")
            return 0

        # 逐行按 id 更新，不写"按状态批量 UPDATE" —— 那会把判据复制成第二份 SQL 逻辑，
        # 与 tier_of() 漂移后无人察觉（本仓"同一条规则两处副本"的老坑）。
        for sid, _a, b in moves:
            conn.execute("UPDATE intent_samples SET status=?, updated_at=CURRENT_TIMESTAMP "
                         "WHERE id=? AND status IN ('new','suggested')", (b, sid))
        conn.commit()

        after = collections.Counter(r["status"] for r in conn.execute(
            "SELECT status FROM intent_samples").fetchall())
        print("\n归位完成。状态分布（前 → 后）：")
        for k in sorted(set(before) | set(after)):
            print("  %-10s %5d → %5d" % (k, before.get(k, 0), after.get(k, 0)))
        pend = after.get("new", 0) + after.get("suggested", 0)
        print("\n人工待办（new+suggested）= %d（归位前 %d）"
              % (pend, before.get("new", 0) + before.get("suggested", 0)))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
