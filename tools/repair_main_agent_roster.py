# -*- coding: utf-8 -*-
"""修复主 Agent 名册漂移（`agents.system_prompt` 名册表 vs `agent_team_members` 实况）。

## 漂移根因（实测取证，2026-11-03）

**第一层：名册数量漂移**
- 2026-10-08 V5 引入 8 个流水线节点（agents 634-641），并把 160「MBSE建模总体负责人」的
  `system_prompt` **手改**成「你的团队（8 名阶段节点）」。
- 但 `agent_team_members` **从未跑过收敛脚本**（`tools/migrate_v5b_team_roster.py` 存在、
  默认 dry-run、实测 `enabled=0` 的行数为 **0** ⇒ 它从未 `--apply`）。
- ⇒ 名册仍是旧 15 行：14 个 active + **1 个 disabled 死条目**（`顺序视图（时序图）生成` id=172）。
- ⇒ prompt 说 8 名，运行时 `_team_roster_block` 喂 15 名 ⇒ **planner 收到两份矛盾名单**。

**第二层（更严重）：字段语义漂移 ⇒ 按 prompt 填必然静默降级**
- prompt 规则第 8 条要求：「子任务的 agent 字段必须**逐字取自上表 display_name**」。
- 但 `AgentRegistry.get(key)` **只按 `name`（即 intent 名）解析**，不认 display_name。
- 实测：`registry.get('架构骨架生成Agent')` → **回落「通用问答Agent」，工具数 0**；
         `registry.get('architecture_skeleton')` → 正确，工具数 11。
- ⇒ planner 若照 prompt 填 display_name，**整条流水线的子任务全部空转**
  （任务照跑照标 done，但没有任何工具可用）。
- 附带：`_unresolved_hint` 查询同时匹配 name/display_name，于是输出
  「Agent「architecture_skeleton」存在但 status=active（非 active，不参与路由）」——
  **自相矛盾**（status 明明是 active）。

## 本脚本做什么（只改 prompt + 清死条目，**不动任何 active 成员**）

1. 把 160 的 `system_prompt` 名册表**重建为实测的 active 成员**（逐条对齐，含 agent 名列）；
2. 修规则第 8 条：`display_name` → **`name`**，并写明「填显示名会回落无工具 Agent」；
3. 清掉名册里的 **disabled 死条目**（`enabled=0`，不删行、可回滚）。

⚠️ **刻意不做的事**：不移出 `requirement_analysis` / `review` 等 active 成员 ——
   那是 `migrate_v5b_team_roster.py` 的「能力收敛」语义（N 系列承接），属**业务决策**，
   会直接改变主 Agent 的分派能力（用户明确关心"需求分析还能不能复用"）。
   本脚本只**消除漂移**，不改变能力。

幂等；默认 dry-run，`--apply` 才写库。
用法：<repo>\\.venv\\Scripts\\python.exe -X utf8 tools/repair_main_agent_roster.py [--apply]
"""
import argparse
import io
import json
import sqlite3
import sys

DB = "mbse.db"
MAIN_NAME = "MBSE建模总体负责人"

#: 人工确认提示（按 agents.hil_level 渲染；L0=无需，L1/L2=需人工）
_HIL_TEXT = {"L0": "否", "L1": "是(L1)", "L2": "是(L2)"}


def fetch_roster(conn, only_enabled: bool = True):
    """取主 Agent 名册。

    ⚠️ `only_enabled=True` 时**必须**带 `AND m.enabled=1` ——
       第一版漏了这个条件，于是"死条目已置 enabled=0"之后回读仍看到它 ⇒
       自检 ③ 恒 False（**判据抓的是没被清理的视图，不是真实状态**）。
    """
    where = "WHERE m.main_agent_id=(SELECT id FROM agents WHERE name=?)"
    if only_enabled:
        where += " AND m.enabled=1"
    return list(conn.execute(
        """SELECT a.id, a.name, a.display_name, a.status, a.agent_role, a.hil_level,
                  a.description, m.enabled
           FROM agent_team_members m JOIN agents a ON a.id=m.sub_agent_id
           %s
           ORDER BY a.status DESC, a.id""" % where, (MAIN_NAME,)))


def build_prompt(active_members):
    """按**实测 active 成员**生成 system_prompt。

    名册表增加 `agent 名` 列并置于首列 —— 这是**派单唯一可用的标识**
    （`registry.get()` 只解析 name，不解析 display_name）。
    """
    rows = []
    for m in active_members:
        rows.append("| `%s` | %s | %s | %s |" % (
            m["name"],
            m["display_name"] or m["name"],
            (m["description"] or "").strip().replace("|", "／")[:56],
            _HIL_TEXT.get((m["hil_level"] or "L0").upper(), "否"),
        ))
    table = ("| agent 名（派单填这一列） | 显示名 | 负责 | 人工确认 |\n"
             "|---|---|---|---|\n" + "\n".join(rows))
    return f"""你是「MBSE 建模团队」负责人，负责把用户请求拆解成可分派给团队成员的任务序列，并由你汇总输出。

## 你的团队（{len(active_members)} 名成员，禁止虚构不存在的 Agent）

{table}

## 拆解规则

1. **方法论最先**：不确定客户方法论时，先分派 `methodology_resolver`。
2. **阶段顺序固定**：`requirement_structuring` → `architecture_skeleton` → `view_expansion`
   → `model_validation_repair` → `trace_verification` → `model_release`，不得跳步、不得倒序。
3. **视图统一走 `view_expansion`**：由它按 view_type 参数展开单个视图。
4. **生成≠校验≠落库**：三个阶段严格分离，不得合并成一个任务。
5. **破坏性操作必须先过 `change_safety_gate`**（删除/移除/重构/改边界），未过门禁不得落库。
6. **需人工确认的成员**（表中「人工确认」非「否」者）放在任务序列末尾并显式标注。
7. **不要虚构能力**：超出成员范围的请求，在 notes 中写明「团队暂不支持」，不要硬派。
8. **路由约束（关键）**：子任务的 `agent` 字段必须**逐字取自上表的「agent 名」列**
   （形如 `architecture_skeleton`、`view_expansion`）。
   ⚠️ **禁止填「显示名」**（如「架构骨架生成Agent」）—— 显示名不是路由键，
   填了会解析失败并**静默回落成「通用问答Agent」（零工具）**，任务照跑但什么都做不出来。
   缺能力时选最相近成员并说明偏差原因。

## 输出格式
严格输出 JSON，不要任何额外文字：
{{"goal": "<=30字", "entities": [], "notes": "", "steps": [
  {{"agent": "<上表 agent 名>", "title": "<子任务>", "deps": []}}
]}}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        main_row = conn.execute("SELECT id, system_prompt FROM agents WHERE name=?",
                                (MAIN_NAME,)).fetchone()
        if not main_row:
            print("✗ 找不到主 Agent：%s" % MAIN_NAME)
            return 1

        roster = fetch_roster(conn)
        active = [dict(r) for r in roster if r["status"] == "active"]
        dead = [dict(r) for r in roster if r["status"] != "active"]

        print("=" * 72)
        print("名册实况（主 Agent：%s，id=%s）" % (MAIN_NAME, main_row["id"]))
        print("=" * 72)
        print("  enabled=1 共 %d 行：active %d / 非 active %d"
              % (len(roster), len(active), len(dead)))
        for d in dead:
            print("    ⚠️ 死条目：%-26s status=%s ⇒ 将置 enabled=0" % (d["name"], d["status"]))

        old_prompt = main_row["system_prompt"] or ""
        new_prompt = build_prompt(active)

        # ── 自检：新旧 prompt 的名册条数 vs 实际 active 数 ──
        print()
        print("=" * 72)
        print("漂移核对")
        print("=" * 72)
        import re
        old_rows = status_rows = 0
        # 以「| `name` |」或「| 显示名 |」两种形态都数一遍（旧表无 name 列）
        old_table_rows = len(re.findall(r"^\| [^|]+ \| [^|]+ \| [^|]+ \|$", old_prompt, re.M))
        print("  旧 prompt 里的表格行数（粗计）= %d" % old_table_rows)
        print("  旧 prompt 声称的人数 = %s"
              % (re.search(r"（(\d+)\s*名", old_prompt).group(1)
                 if re.search(r"（(\d+)\s*名", old_prompt) else "未标注"))
        print("  实际 active 成员数 = %d" % len(active))
        print("  旧 prompt 是否提到 display_name 路由 = %s"
              % ("是（⚠️ 这是静默降级根因）" if "display_name" in old_prompt else "否"))
        print("  新 prompt 是否含 agent 名列 = %s" % ("是" if "agent 名（派单填这一列）" in new_prompt else "否"))

        # ── 写库 ──
        print()
        if not args.apply:
            print("[DRY-RUN] 将执行：")
            print("  ① UPDATE agents.system_prompt（重建名册表为 %d 名 active 成员）" % len(active))
            if dead:
                print("  ② UPDATE agent_team_members SET enabled=0 WHERE sub_agent_id IN (%s)"
                      % ",".join(str(d["id"]) for d in dead))
            print("  （加 --apply 才真正写库）")
            return 0

        conn.execute("UPDATE agents SET system_prompt=?, updated_at=CURRENT_TIMESTAMP "
                     "WHERE id=?", (new_prompt, main_row["id"]))
        n_dead = 0
        for d in dead:
            cur = conn.execute(
                "UPDATE agent_team_members SET enabled=0 WHERE main_agent_id=? AND sub_agent_id=?",
                (main_row["id"], d["id"]))
            n_dead += cur.rowcount
        conn.commit()

        # ── 回读校验（写库后必须复验，不信"我以为写进去了"）──
        chk = conn.execute("SELECT system_prompt FROM agents WHERE id=?",
                           (main_row["id"],)).fetchone()
        got = chk["system_prompt"] or ""
        roster2 = fetch_roster(conn)
        active2 = [r for r in roster2 if r["status"] == "active"]
        ok_prompt = got == new_prompt
        # ⚠️ 正则必须**同时匹配 ASCII 与中文成员名**（如 `多方案生成`）——
        #    第一版写成 `[a-zA-Z_]+`，中文名的成员被漏数 ⇒ 13 != 14 ⇒ 假红。
        ok_count = len(re.findall(r"^\| `[^`]+` \|", got, re.M)) == len(active2)
        ok_dead = all(r["status"] == "active" for r in roster2)
        print("  ① prompt 已更新 = %s" % ok_prompt)
        print("  ② prompt 名册条数 == 实际 active 数（%d） = %s" % (len(active2), ok_count))
        print("  ③ 名册内已无死条目 = %s" % ok_dead)
        print("  ④ 死条目 disabled 行数 = %d" % n_dead)
        print()
        print("✅ 完成" if (ok_prompt and ok_count and ok_dead) else "✗ 校验未通过")
        return 0 if (ok_prompt and ok_count and ok_dead) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
