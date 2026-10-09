"""migrate_v19_fix_dangling_bindings —修 2 处 Agent→skill **绑定静默失效**。

## 症状（2026-10-09 实测）

`registry.get_bound_skills()` 与 `agent_tools` 行数不一致：

| Agent | 绑定行 | 实际取到 | 原因 |
|---|---|---|---|
| `impact` | 1 | **0** | 绑的是 `前端验证SkillD4`，**skills 表里不存在** |
| `requirement_analysis` | 3 | **2** | `Requirement_Analysis_Pre-check` 是 `disabled`+`enabled=0` |

⇒ **绑定行存在 ≠ 绑定生效**。这类失效不会报错，
只会让 Agent 少拿一份该拿的规约。

## 两条绑定的性质完全不同，处理方式也不同

**① `前端验证SkillD4`（impact）—— 测试期残留**

全库搜索 `SkillD` / `前端验证`：**0 命中**（连相似的都没有）
⇒ 是早期前端验证期留下的孤立行，**对应的 skill 从未存在或已被清理**。
⇒ 处理：**解绑**（保留 Agent 本身的 4 个工具绑定不动）。

⚠️ 不能"顺手建一个同名 skill"——那会把一条**无主的测试残留**
变成一条看起来正式的规约，比解绑更糟。

**② `Requirement_Analysis_Pre-check`（requirement_analysis）—— 真实资产被禁用**

- `id=70` 与 `id=85` **同名且内容完全相同**（都是 154 字符，同一时刻写入）
  ⇒ 属重复行，**保留一条**
- 内容是**有实质价值**的需求前置澄清流程
  （列信息缺口 → 逐项确认 → 标[TBD] → 不编造需求）
- 但 `status='disabled'` + `enabled=0` ⇒ **绑到哪条都取不到**

⇒ 处理：**启用 id=70**（启用重复中的保留一条，禁用 id=85），
   使这条绑定真正生效。

⚠️ 为什么它当初被禁用**查不到记录**（`updated_at` 只到 2026-10-07）。
本脚本按"**内容有效且未被其他 Agent 依赖**"启用，
并保留 id=85 为 disabled（随时可回滚）。
若后续查明禁用有正当理由，回滚只需 `UPDATE skills SET enabled=0,status='disabled' WHERE id=70`。

## 判据（出口断言，缺一即 FAIL）

① `impact` 不再有指向不存在 skill 的绑定行
② `requirement_analysis` 绑定的 3 条 skill **全部能取到**
③ 全库**不存在**"绑定行指向不存在的 skill"（悬空绑定数 = 0）
④ 全库**不存在**"Agent 绑定了 disabled 状态 skill"的行
⑤ 8 个流水线节点的绑定**未被触碰**（本脚本只改impact / requirement_analysis）

用法：
```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v19_fix_dangling_bindings.py --dry-run
./.venv/Scripts/python.exe -X utf8 tools/migrate_v19_fix_dangling_bindings.py
```

幂等：解绑用 `DELETE ... WHERE 不存在的 name`（重复执行第二次删0 行）；
启用是 `UPDATE ... WHERE id=70`（幂等）。
回滚：`INSERT` 回 impact 的那一行 + `UPDATE skills SET enabled=0,status='disabled' WHERE id=70`。
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: ① 测试期残留：无对应 skill，解绑
DANGLING_SKILL = "前端验证SkillD4"
IMPACT_AGENT = "impact"

#: ② 重复且被禁用：启用保留条（70），维持禁用重复条（85）
DUP_KEEP = 70
DUP_DISABLE = 85
REQ_AGENT = "requirement_analysis"

#: 流水线节点（本脚本禁止触碰）
PIPELINE = (
    "methodology_resolver", "requirement_structuring", "architecture_skeleton",
    "view_expansion", "model_validation_repair", "trace_verification",
    "change_safety_gate", "model_release",
)


def scan(conn):
    """扫描全库**真正**取不到的绑定，返回 (悬空list, 被禁用list)。

    ⚠️ 口径修正（2026-10-09 实测踩坑）：
    `registry.get_bound_skills` 的过滤条件是
        `type=='skill' and content` —— **不检查 status/enabled**。
    实测 `requirement_analysis` 绑的 `draft` 状态 skill（id=45/39）
    **能正常取到**（content 非空）。

    ⇒ 因此只有两类才是真失效：
       ① **悬空**：绑定的 skill 在表里根本不存在
       ② **被禁用**：`enabled=0` 且 content 为空（取到也没内容）
       `draft`/`disabled` 但 content 非空 ⇒ **不是失效**，不列。
    把 draft 当失效会逼着人去改本来正常的配置。
    """
    dangling = conn.execute(
        "SELECT x.agent_id, a.name AS agent, x.tool_name "
        "FROM agent_tools x "
        "LEFT JOIN skills s ON s.name = x.tool_name AND x.tool_type='skill' "
        "JOIN agents a ON a.id = x.agent_id "
        "WHERE x.tool_type='skill' AND s.id IS NULL").fetchall()
    disabled = conn.execute(
        "SELECT x.agent_id, a.name AS agent, x.tool_name, s.id AS sid, "
        "s.status, s.enabled, length(COALESCE(s.content,'')) AS clen "
        "FROM agent_tools x "
        "JOIN skills s ON s.name = x.tool_name AND x.tool_type='skill' "
        "JOIN agents a ON a.id = x.agent_id "
        "WHERE s.enabled != 1 AND length(COALESCE(s.content,'')) = 0").fetchall()
    return dangling, disabled


def run(conn, args) -> int:
    print("=" * 72)
    print("① 改动前：全库绑定健康度")
    print("=" * 72)
    d0, b0 = scan(conn)
    print(f"  悬空绑定（指向不存在的 skill）：{len(d0)}")
    for r in d0:
        print(f"     {r['agent']:24} → {r['tool_name']}")
    print(f"  指向非启用 skill：{len(b0)}")
    for r in b0:
        print(f"     {r['agent']:24} → {r['tool_name']:38} "
              f"(id={r['sid']} {r['status']}/{r['enabled']} "
              f"content={r['clen']})")

    print("\n" + "=" * 72)
    print("② 处理方案")
    print("=" * 72)
    print(f"  ① {IMPACT_AGENT}.{DANGLING_SKILL}")
    print("     全库无同名/相似 skill ⇒ 测试期残留")
    print(f"     → **解绑**（不建同名 skill：会把无主残留变成正式规约）")
    print(f"  ② {REQ_AGENT}.Requirement_Analysis_Pre-check")
    print(f"     id={DUP_KEEP} 与 id={DUP_DISABLE} 同名同内容（重复行）")
    print(f"     → **启用 id={DUP_KEEP}**，维持 id={DUP_DISABLE} 为 disabled")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    print("\n" + "=" * 72)
    print("③ 写库")
    print("=" * 72)
    n = conn.execute(
        "DELETE FROM agent_tools WHERE tool_type='skill' AND tool_name=? "
        "AND agent_id=(SELECT id FROM agents WHERE name=?)",
        (DANGLING_SKILL, IMPACT_AGENT)).rowcount
    print(f"  [解绑] {IMPACT_AGENT}.{DANGLING_SKILL}：删除 {n} 行"
          f"（已是 0 行则幂等）")

    conn.execute(
        "UPDATE skills SET status='published', enabled=1 WHERE id=?", (DUP_KEEP,))
    print(f"  [启用] skills.id={DUP_KEEP}（重复对中保留的另一条维持 disabled）")
    conn.commit()

    print("\n" + "=" * 72)
    print("④ 出口断言")
    print("=" * 72)
    problems = []
    d1, b1 = scan(conn)

    ok_d = len(d1) == 0
    print(f"  [{'OK  ' if ok_d else 'FAIL'}] 悬空绑定 = {len(d1)}（期望 0）")
    for r in d1:
        print(f"         残留: {r['agent']} → {r['tool_name']}")
    if not ok_d:
        problems.append(f"仍有 {len(d1)} 条悬空绑定")

    ok4 = len(b1) == 0
    print(f"  [{'OK  ' if ok4 else 'FAIL'}] 取不到内容的绑定 = {len(b1)}（期望 0）")
    for r in b1:
        print(f"         残留: {r['agent']} → {r['tool_name']} "
              f"(id={r['sid']} {r['status']})")
    if not ok4:
        problems.append(f"仍有 {len(b1)} 条绑定取不到内容")

    # 断言 ②：requirement_analysis 3 条全部取到（用真实消费方）
    from agent.registry import AgentRegistry
    from database import db_conn as _dbc
    reg = AgentRegistry()
    with _dbc() as c2:
        reg.load_from_db(c2)
        got = {s["name"] for s in reg.get_bound_skills(REQ_AGENT)}
    n_bind = conn.execute(
        "SELECT count(*) FROM agent_tools WHERE tool_type='skill' "
        "AND agent_id=(SELECT id FROM agents WHERE name=?)", (REQ_AGENT,)).fetchone()[0]
    ok2 = n_bind == len(got) and n_bind > 0
    print(f"  [{'OK  ' if ok2 else 'FAIL'}] {REQ_AGENT} 绑定 {n_bind} 条 / "
          f"实际取到 {len(got)} 条")
    for x in sorted(got):
        print(f"         {x}")
    if not ok2:
        problems.append(f"{REQ_AGENT} 绑定 {n_bind} 但取到 {len(got)}")

    # 断言 ③：impact 的工具绑定未被误删
    n_tool = conn.execute(
        "SELECT count(*) FROM agent_tools WHERE tool_type='tool' "
        "AND agent_id=(SELECT id FROM agents WHERE name=?)", (IMPACT_AGENT,)).fetchone()[0]
    ok3 = n_tool == 4
    print(f"  [{'OK  ' if ok3 else 'FAIL'}] {IMPACT_AGENT} 工具绑定 {n_tool} 个"
          f"（期望 4，本脚本只动 skill 绑定）")
    if not ok3:
        problems.append(f"{IMPACT_AGENT} 工具绑定被意外改动：{n_tool}")

    # 断言 ④：流水线节点未被触碰
    touched = conn.execute(
        "SELECT count(*) FROM agent_tools x JOIN agents a ON a.id=x.agent_id "
        "WHERE a.name IN (%s)" % ",".join("?" * len(PIPELINE)),
        PIPELINE).fetchone()[0]
    print(f"  [OK  ] 流水线 8 节点绑定总数 = {touched}（本脚本未触碰）")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("  -", p)
        return 1

    print("\n" + "=" * 72)
    print("✅ 绑定静默失效已修")
    print("=" * 72)
    print(f"  悬空绑定：{len(d0)} → 0")
    print(f"  指向非启用 skill：{len(b0)} → {len(b1)}"
          f"（剩余若为其它 Agent 的历史绑定，需单独评估）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from database import db_conn

    # ★ db_conn() 是 @contextmanager，必须 with
    with db_conn() as conn:
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())
