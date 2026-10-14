#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_stage_pipeline_agents —— B4-6：8 阶段建模流水线的 Agent 可用性门禁。

## 米爸第 3 点（架构定义）
> 多 Agent 平台的实现逻辑：主 Agent 实现意图理解、任务拆分与规划、链路编排、内容输出；
> 子 Agent 完成自己的职责任务，**可调用自己绑定的技能与工具**，完成任务后把结果交给主 Agent。

## 取证结论（2026-10-14，纠正了「僵尸 Agent」的初始判断）
库里有一套**完整的 8 阶段建模流水线**，建于 `2026-10-08 05:45:40`：

| stage | Agent | 绑定能力 | 职责 |
|---|---|---|---|
| 0 | `methodology_resolver` | 4 | 解析工程采用的建模方法论 |
| 1 | `requirement_structuring` | 6 | 自然语言需求 → 结构化条目集 |
| 2 | `architecture_skeleton` | 9 | 条目 → SysML v2 架构骨架 |
| 3 | `view_expansion` | 15 | 按 view_type 展开具体视图 |
| 4 | `model_validation_repair` | 8 | 词法→语法→语义 定位修复复检 |
| 5 | `trace_verification` | 8 | 需求↔模型双向追溯矩阵 |
| 6 | `change_safety_gate` | 4 | 删除/批量替换类操作前的影响面分析 |
| 7 | `model_release` | 8 | 通过校验的产物落库为图谱实体 |

**⚠️ 它们不是"僵尸"**（active 但零调度的能力残留），而是「**落地了但从未被真实流量验证**」：
· 全部编排 run（527~532）都在 **10-01 ~ 10-07**，派发用的是**旧意图名**
  （`requirement_analysis` / `design` / `review` / `结构视图生成`）；
· 8 阶段 Agent 建立于 **10-08 05:45**，之后 `agent_tasks` **新增 0 行**。
⇒ 正确的处置**不是下线**（那会删掉米爸描述的架构载体），
   而是**让它们真的能被选中**，并用门禁守住。

## 本门禁锁住
S 组：8 个阶段 Agent 齐备、stage_order 0~7 无缺、都有工具绑定（子 Agent 能干活）
P 组：★**必须能被 planner 选中** —— 出现在 `_orch_pool_desc_lines` 里
      （实测：走 `AgentPipeline + _load_db_agents` 的生产构造，19 个候选，
        8 个阶段 Agent 全在池中；**漏掉 `_load_db_agents()` 会掉到 6 个**）
      ⚠️ 这就是"判据环境 ≠ 生产环境"在本门禁的具体形态，必须按生产口径构造。
G 组：GAP 记录「阶段流水线从未被真实流量验证」（`agent_tasks` 里 stage Agent 零调度）
      —— 差距**记录而非判红**，因为它是待验证状态不是缺陷；
      差距消失时（真跑过一次）门禁会主动提醒更新口径。
"""
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")

STAGES = [
    ("methodology_resolver", 0, 4),
    ("requirement_structuring", 1, 6),
    ("architecture_skeleton", 2, 9),
    ("view_expansion", 3, 15),
    ("model_validation_repair", 4, 8),
    ("trace_verification", 5, 8),
    ("change_safety_gate", 6, 4),
    ("model_release", 7, 8),
]

_PASS, _FAIL, _GAP = [], [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                         ("  —— %s" % detail) if detail else ""))


def gap(name, detail):
    _GAP.append(name)
    print("  [GAP ] %s  —— %s" % (name, detail))


def conn_ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    return c


def s_group(c):
    print("\n=== S 组 · 8 阶段 Agent 齐备性 ===")
    found = {}
    for name, stage, min_binds in STAGES:
        r = c.execute(
            "SELECT id, stage_order, status, agent_role, description FROM agents WHERE name=?",
            (name,)).fetchone()
        if r is None:
            check("S1 %s 存在" % name, False, "库里查不到")
            continue
        n_binds = c.execute(
            "SELECT COUNT(*) FROM agent_tools WHERE agent_id=?", (r["id"],)).fetchone()[0]
        found[name] = dict(id=r["id"], stage=r["stage_order"], status=r["status"],
                           role=r["agent_role"], binds=n_binds,
                           desc=r["description"] or "", min_binds=min_binds)
    check("S1 8 个阶段 Agent 全部存在", len(found) == 8,
          "实测 %d/8：缺 %s" % (len(found), [n for n, _, _ in STAGES if n not in found]))
    if len(found) < 8:
        return
    # stage_order 0~7 连续
    orders = sorted(v["stage"] for v in found.values())
    check("S2 stage_order 恰为 0~7 无缺无重", orders == list(range(8)),
          "实测 %s" % orders)
    # 状态
    bad_st = [n for n, v in found.items() if v["status"] != "active"]
    check("S3 8 个阶段 Agent 全部 active", not bad_st, "非 active：%s" % bad_st)
    # 角色
    bad_role = [n for n, v in found.items() if v["role"] != "sub"]
    check("S4 全部 role=sub（可被主 Agent 委派）", not bad_role, "非 sub：%s" % bad_role)
    # ★子 Agent 必须有绑定能力（米爸第 3 点：「可调用自己绑定的技能与工具」）
    no_bind = [n for n, v in found.items() if v["binds"] < v["min_binds"]]
    check("S5 ★每个阶段 Agent 都绑定了工具（子 Agent 能干活）", not no_bind,
          "绑定不足：%s" % [(n, found[n]["binds"], found[n]["min_binds"]) for n in no_bind])
    # 描述完整（planner 靠它区分）
    no_desc = [n for n, v in found.items() if len(v["desc"]) < 15]
    check("S6 全部有可用于 planner 的描述（≥15 字）", not no_desc, "描述过短：%s" % no_desc)
    print("  绑定数明细：%s"
          % {n: v["binds"] for n, v in sorted(found.items(), key=lambda x: x[1]["stage"])})


def p_group():
    print("\n=== P 组 · 必须能被 planner 选中（生产构造口径）===")
    from agent.pipeline import AgentPipeline
    from database import get_db
    p = AgentPipeline()
    # ⚠️ 必须调 `_load_db_agents()` —— 它**不在 __init__ 里**（pipeline.py:53），
    #    而是「每次执行前」调。漏掉 ⇒ 语义索引 0 条 / db 意图 0 个
    #    ⇒ _orch_pool 掉到 fallback 的 6 个 ⇒ 8 个阶段 Agent 全部消失。
    #    这不是假设，是本门禁首版实测到的（详见模块 docstring）。
    try:
        p._load_db_agents()
    except Exception as e:                                     # noqa: BLE001
        check("P0 _load_db_agents 可执行", False, repr(e)[:90])
        return
    check("P0 _load_db_agents 已执行（语义索引非空）",
          len(p.router._semantic_index or []) > 0,
          "索引 %d 条" % len(p.router._semantic_index or []))

    conn = get_db()
    lines = p._orch_pool_desc_lines(conn)
    pool = "\n".join(lines)
    check("P1 候选池非空（防空集合恒真）", len(lines) >= 6, "实测 %d 行" % len(lines))

    missing = [n for n, _, _ in STAGES if n not in pool]
    check("P2 ★8 个阶段 Agent 全部出现在 planner 候选池", not missing,
          "不在池中：%s" % missing)
    # ★防空转：候选池必须有区分度，否则"都在池中"是因为池里啥都有
    n_distinct = len(set(lines))
    check("P3 候选池有区分度（≥8 行，否则 P2 可能是恒真）", n_distinct >= 8,
          "实测 %d 行不同" % n_distinct)
    # 每个阶段 Agent 的专长描述也要出现（planner 靠它区分）
    no_desc = []
    for n, _, _ in STAGES:
        if ("- %s:" % n) not in pool:
            no_desc.append(n)
    check("P4 每个阶段 Agent 都有专长描述行（- intent: 名称（专长：…））",
          not no_desc, "缺描述行：%s" % no_desc)


def g_group(c):
    print("\n=== G 组 · GAP（记录差距，不判红）===")
    used = {x[0] for x in c.execute(
        "SELECT DISTINCT agent_id FROM agent_tasks "
        "WHERE agent_id IS NOT NULL AND agent_id!=''")}
    never = [n for n, _, _ in STAGES if n not in used]
    if never:
        last_orch = c.execute(
            "SELECT MAX(created_at) FROM agent_tasks WHERE run_id BETWEEN 527 AND 532"
        ).fetchone()[0]
        gap("G1 8 阶段流水线从未被真实流量验证",
            "%d/8 零调度（%s）；最后编排 run=%s，而 8 阶段 Agent 建于 10-08 05:45 "
            "⇒ 它们上线后一次编排都没跑过。"
            "**这不是缺陷，是待验证状态** ⇒ 处置是『让它们能被选中 + 真跑一次』，"
            "**不是下线**。" % (len(never), never[:3], str(last_orch)[:16]))
    else:
        print("  [INFO] 8 阶段 Agent 已全部被真实流量调度过 ⇒ "
              "本 GAP 口径可关闭（请更新门禁：改为断言『至少被调度过一次』）")


def main():
    print("=== verify_stage_pipeline_agents · B4-6 ===")
    c = conn_ro()
    try:
        s_group(c)
        p_group()
        g_group(c)
    finally:
        c.close()
    print("\n%d pass / %d fail / %d gap" % (len(_PASS), len(_FAIL), len(_GAP)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())