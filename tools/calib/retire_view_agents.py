#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下线「视图生成 Agent」——视图能力已由 sysml_view_generation_* skill 承载。

## 为什么可以下线（2026-10-14 取证，非推断）

| 视图 | Agent 被调度 | skill 被调用 | skill 建立时间 | Agent 末次调度 | 判定 |
|---|---|---|---|---|---|
| 结构 | 16 | 1 | 2026-10-08 07:37 | 2026-10-07 15:49 | skill 已接管 |
| 用例 | 2 | 7 | 同上 | 2026-09-30 09:41 | skill 已接管 |
| 活动 | 0 | 1 | 同上 | — | skill 独跑 |
| 需求 | 10 | 0 | 同上 | 2026-10-01 17:38 | skill 可用，Agent 未再被路由 |
| IBD | 2 | 0 | 同上 | 2026-09-24 02:52 | 同上 |
| 状态/时序/参数 | 0 | 0 | 同上 | — | 两边都未被路由（纯僵尸） |

**决定性证据**：`10-11 之后视图类 Agent 调度数 = 0`，而 skill 在同期被真实调用
（structure 最近一次 `last_used=2026-10-14 09:56`，即今早的真实建模验证）。

## 额外收益：顺带修掉两个「细粒度视图 Agent 抢占主意图」错例
`agent/pipeline.py:77` 的语义索引 SQL 是
`SELECT ... FROM agents WHERE status='active' ORDER BY name`
⇒ 下线后视图 Agent 退出语义索引
⇒ `agent/intent.py:167/479` 记录的两个错例（"…生成结构视图…代码…校验" 被
「结构视图生成」抢走，期望 design）**自动消失**。

## 安全设计
1. **只改 status 一个字段**，不动 role、不删行、不动 agent_tools 绑定
   ⇒ 回滚只需 `UPDATE agents SET status='active' WHERE id IN (...)`。
2. **分批**（每批 4 个），每批后立即验证，不一刀端。
3. **不删 skill** —— skill 是能力的真正载体，只动 Agent 侧。
4. 执行前必须已备份（`backups/b4-agents-20261014/`）。

## 用法
    python tools/calib/retire_view_agents.py --dry-run   # 只看会改什么
    python tools/calib/retire_view_agents.py --apply     # 真改（需已备份）
    python tools/calib/retire_view_agents.py --rollback  # 回滚
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROD_DB = os.path.join(ROOT, "mbse.db")
SNAP = os.path.join(ROOT, "backups", "b4-agents-20261014", "agents_status_snapshot.json")

# ★ 只列**取证确认能力已由 skill 承载**的那几个。
#   不含「多方案生成」—— 它没有对应的 sysml_view_generation_* skill。
VIEW_AGENTS = [
    "结构视图生成",
    "用例视图生成",
    "活动图生成",
    "状态机视图生成",
    "顺序视图（时序图）生成",
    "交互视图（IBD）生成",
    "参数视图生成",
    "需求视图生成",
]

# 视图能力 skill（用于核对"承载方存在"）
VIEW_SKILLS = [
    "sysml_view_generation_structure",
    "sysml_view_generation_usecase",
    "sysml_view_generation_activity",
    "sysml_view_generation_state",
    "sysml_view_generation_sequence",
    "sysml_view_generation_ibd",
    "sysml_view_generation_parameter",
    "sysml_view_generation_requirement",
]


def conn_ro():
    c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    c.row_factory = sqlite3.Row
    return c


def precondition(c):
    """执行前的硬前提。任何一条不满足都拒绝执行 —— 宁可不做，不能盲改。"""
    errs = []
    # ① 承载方 skill 必须全部 published + enabled
    for sk in VIEW_SKILLS:
        r = c.execute("SELECT status, enabled FROM skills WHERE name=?", (sk,)).fetchone()
        if r is None:
            errs.append("承载方 skill 不存在：%s" % sk)
        elif r["status"] != "published" or str(r["enabled"]) in ("0", "False"):
            errs.append("承载方 skill 不可用：%s（status=%s enabled=%s）" % (sk, r["status"], r["enabled"]))
    # ② 备份必须存在
    if not os.path.exists(SNAP):
        errs.append("缺 agents 状态快照，拒绝盲改：%s" % SNAP)
    return errs


def show(c):
    print("=== 当前视图 Agent 状态 ===")
    for a in VIEW_AGENTS:
        r = c.execute("SELECT id, name, status, agent_role FROM agents WHERE name=?", (a,)).fetchone()
        if r is None:
            print("  %-24s **不存在**" % a)
        else:
            print("  id=%-4s %-24s status=%-9s role=%s" % (r["id"], r["name"], r["status"], r["agent_role"]))


def apply():
    c = conn_ro()
    errs = precondition(c)
    if errs:
        print("❌ 前置条件不满足，拒绝执行：")
        for e in errs:
            print("   - %s" % e)
        return 1
    show(c)
    conn = sqlite3.connect(PROD_DB)
    ids = []
    for a in VIEW_AGENTS:
        r = conn.execute("SELECT id FROM agents WHERE name=? AND status='active'", (a,)).fetchone()
        if r:
            ids.append(r[0])
    if not ids:
        print("\n（无需变更：已全部是 disabled）")
        return 0
    BATCH = 4
    print("\n=== 分批置 disabled（每批 %d 个）===" % BATCH)
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        q = "UPDATE agents SET status='disabled' WHERE id IN (%s)" % ",".join(map(str, batch))
        conn.execute(q)
        conn.commit()
        names = [r[0] for r in conn.execute(
            "SELECT name FROM agents WHERE id IN (%s)" % ",".join(map(str, batch)))]
        print("  批次 %d：已下线 %s" % (i // BATCH + 1, "、".join(names)))
    conn.close()

    # 复验
    c2 = conn_ro()
    left = [a for a in VIEW_AGENTS
            if (c2.execute("SELECT status FROM agents WHERE name=?", (a,)).fetchone() or {"status": "?"})["status"] == "active"]
    n_sub = c2.execute("SELECT COUNT(*) FROM agents WHERE status='active' AND agent_role='sub'").fetchone()[0]
    # ⚠️ 判据用 `IN (...)` 逐个点名，**不用 `LIKE 'sysml_view_generation_%'`** ——
    #   实测 `LIKE` 会把 `_guide` 也数进去，报出「9/8」这种像模像样的错数字。
    #   （与 MEMORY 里「查错对象会给出一个撑不住的数字」同族，这里是判据多算了。）
    n_skill = sum(
        1 for sk in VIEW_SKILLS
        if (c2.execute("SELECT status, enabled FROM skills WHERE name=?", (sk,)).fetchone()
            or {"status": None, "enabled": None})["status"] == "published")
    print("\n=== 复验 ===")
    print("  仍 active 的视图 Agent：%s" % (left or "无 ✅"))
    print("  active sub Agent：%d" % n_sub)
    print("  承载方 skill 仍 published：%d/8 %s"
          % (n_skill, "✅" if n_skill == len(VIEW_SKILLS) else "❌"))
    c2.close()
    return 0


def rollback():
    if not os.path.exists(SNAP):
        print("❌ 无快照，无法回滚：%s" % SNAP)
        return 1
    snap = json.load(open(SNAP, encoding="utf-8"))
    ids = [int(k) for k, v in snap.items() if v.get("name") in VIEW_AGENTS]
    conn = sqlite3.connect(PROD_DB)
    for i in range(0, len(ids), 4):
        batch = ids[i:i + 4]
        conn.execute("UPDATE agents SET status='active' WHERE id IN (%s)" % ",".join(map(str, batch)))
        conn.commit()
    conn.close()
    print("✅ 已回滚 %d 个视图 Agent 为 active" % len(ids))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.rollback:
        return rollback()
    if a.apply:
        return apply()
    c = conn_ro()
    errs = precondition(c)
    show(c)
    print("\n=== 前置条件 ===")
    if errs:
        for e in errs:
            print("  ❌ %s" % e)
    else:
        print("  ✅ 承载方 skill 全部可用 + 快照存在")
    print("\n（dry-run：未做任何修改。加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())