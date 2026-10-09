# -*- coding: utf-8 -*-
"""V5b · 团队名册收敛 —— 消除「system_prompt 名册」与「agent_team_members」不一致。

**问题（本脚本要修的）**
--------------------------------
V5 入了 8 个流水线节点并加进团队，但旧成员（8 视图 Agent + design/review 等 14 名）
仍在 `agent_team_members` 里。于是：
  · `system_prompt` 名册只有 8 个（我刚写的）
  · `_team_roster_block`（`orchestration.py:102`）会**把全部 22 名**喂给 planner
  ⇒ planner 收到**两份矛盾名单**（prompt 说"只可分派给这8 个"，数据里有 22 个）

**为什么不能简单删成员行**
--------------------------------
`agent_team_members` 里的旧成员是**现役能力**（8 视图 Agent 各自能生成对应视图）。
V5 只入了N3 `view_expansion`，但 N3 尚未接管视图生成 ⇒ 直接删会留**能力真空**。
故本脚本采取**显式取舍**：
  · 8 视图 Agent → **移出团队**（`enabled=0`，不删行，可回滚），视图能力由 N3 承接
  · design / review / requirement_analysis → 移出团队（能力已由 N2/N4/N1 承接）
  · impact / report_generation / knowledge_qa → **保留在团队**（这三个是**旁路能力**，
    N 系列不覆盖：影响分析、报告、知识问答必须仍可被主 Agent 委派）

⚠️ 若你认为「8 视图 Agent 仍需保留在团队以防N3 未就绪」，请用 `--keep-views` 参数，
   本脚本会改为只标`stage_order=NULL` 而不移出团队。

幂等；默认 dry-run。
"""
import argparse
import json
import os
import sqlite3
import sys

DB = "mbse.db"
MAIN_NAME = "MBSE建模总体负责人"

# pipeline 节点：system_prompt 名册里有的，必须在团队里
PIPELINE_NODES = [
    "methodology_resolver", "requirement_structuring", "architecture_skeleton",
    "view_expansion", "model_validation_repair", "trace_verification",
    "change_safety_gate", "model_release",
]

# 移出团队（能力已被 N 系列承接）
MOVE_OUT = [
    ("requirement_analysis", "能力由 N1 需求结构化 + N2 架构骨架承接"),
    ("design", "能力由 N2 架构骨架承接"),
    ("review", "能力由 N4 模型校验修复承接"),
    ("需求视图生成", "视图能力由 N3 view_expansion 承接（view_type=requirement）"),
    ("结构视图生成", "视图能力由 N3 承接（view_type=structure）"),
    ("交互视图（IBD）生成", "视图能力由 N3 承接（view_type=ibd）"),
    ("用例视图生成", "视图能力由 N3 承接（view_type=usecase）"),
    ("活动图生成", "视图能力由 N3 承接（view_type=activity）"),
    ("状态机视图生成", "视图能力由 N3 承接（view_type=state）"),
    ("参数视图生成", "视图能力由 N3 承接（view_type=parameter）"),
    ("顺序视图（时序图）生成", "视图能力由 N3 承接（view_type=sequence）"),
]

# 保留在团队（旁路能力，N 系列不覆盖）
KEEP = [
    ("impact", "变更影响分析是独立旁路能力，N 系列不覆盖"),
    ("report_generation", "报告生成是旁路能力，N 系列不覆盖"),
    ("knowledge_qa", "知识问答是直答能力，N 系列不覆盖"),
    ("chat", "通用入口"),
    ("多方案生成", "变体空间候选提议，独立能力"),
    ("requirement_quality", "需求质量双通道分析，独立能力"),
]

VIEW_ROWS = """| 需求视图生成 | SysML v2 需求视图（层次/主体/满足关系） | 否 |
| 结构视图生成 | SysML v2 结构视图（层级组成/端口） | 否 |
| 交互视图（IBD）生成 | SysML v2 交互视图（端口与连接器） | 否 |
| 用例视图生成 | SysML v2 用例视图（用例层次/参与者） | 否 |
| 活动图生成 | SysML v2 活动视图（并发/决策/异常分支） | 否 |
| 状态机视图生成 | SysML v2 状态视图（状态/转换/守护） | 否 |
| 参数视图生成 | SysML v2 参数视图（约束/参数化关系） | 否 |
| 顺序视图（时序图）生成 | SysML v2 时序视图（时间顺序/消息交换） | 否 |
"""

BYPASS_ROWS = """| 多方案生成 | 变体空间候选提议（不产出模型代码） | 否 |
| 变更影响Agent | 变更影响范围分析 | 是(L1) |
| 报告生成Agent | 分析/评审报告组装与导出 | 否 |
| 知识问答Agent | MBSE/SysML 领域知识问答 | 否 |
"""

PIPE_ROWS = """| 方法论解析Agent | 识别建模方法论，产出建模规约卡 | 否 |
| 需求结构化Agent | 需求条目化、分类、歧义识别 | 是(L1) |
| 架构骨架生成Agent | SysML v2 骨架（包/部件/端口/需求声明） | 否 |
| 视图展开Agent | 八视图统一展开（view_type 参数驱动） | 否 |
| 模型校验修复Agent | 三层诊断定位与修复 | 否 |
| 追溯核验Agent | 需求↔元素追溯矩阵 | 否 |
| 变更安全门Agent | 删除/重构前影响面分析 | 是(L1) |
| 模型发布Agent | 落库与版本链 | 是(L2) |
"""


def build_prompt(keep_views: bool) -> str:
    """名册 = 流水线节点 + （可选）8 视图节点 + 旁路能力。
    必须与 agent_team_members 的 active 成员一致，否则 planner 收到两份矛盾名单。
    """
    header = ("你的团队（8 名阶段节点" + (" + 8 名视图节点" if keep_views else "")
              + " + 旁路能力，禁止虚构不存在的 Agent）")
    view_rule = ("3. **视图节点二选一**：优先用「视图展开Agent」（统一、顺序可控）；\n"
                 "   仅当用户明确点名某个视图（如「生成活动图」）时，才派对应的视图节点。\n"
                 if keep_views else
                 "3. **视图统一走「视图展开Agent」**：由它按 view_type 参数展开单个视图。\n")
    rows = PIPE_ROWS + (VIEW_ROWS if keep_views else "") + BYPASS_ROWS
    return f"""你是「MBSE 建模团队」负责人，负责把用户请求拆解成可分派给团队成员的任务序列，并由你汇总输出。

## {header}
{rows}
## 拆解规则
1. **方法论最先**：不确定客户方法论时，先分派「方法论解析Agent」。
2. **阶段顺序固定**：需求结构化 → 架构骨架 → 视图展开 → 校验修复 → 追溯核验 → 发布，不得跳步、不得倒序。
{view_rule}4. **生成≠校验≠落库**：三个阶段严格分离，不得合并成一个任务。
5. **破坏性操作必须经变更安全门Agent**（删除/移除/重构/改边界），未过门禁不得落库。
6. **需人工确认的成员**（需求结构化/变更安全门/变更影响/模型发布）放在任务序列末尾并显式标注。
7. **不要虚构能力**：超出成员范围的请求，在 notes 中写明「团队暂不支持」，不要硬派。
8. **路由约束**：子任务的 agent 字段必须逐字取自上表 display_name；缺能力时选最相近成员并说明偏差原因。

## 输出格式
严格输出 JSON，不要任何额外文字：
{{"goal": "<=30字", "entities": [], "notes": "", "steps": [
  {{"agent": "<成员 display_name，逐字取自上表>", "task": "<可独立执行的子任务描述>"}}]}}
"""


stats = {"disabled": 0, "kept": 0, "abnormal": 0, "prompt": 0}


def log(k, name, detail=""):
    stats[k] += 1
    tag = {"disabled": "MOVED ", "kept": "KEPT  ", "abnormal": "CHECK ",
           "prompt": "PROMPT"}[k]
    print(f"  [{tag}] {name}{('  · ' + detail) if detail else ''}")


def write_prompt(conn, mid, apply_, keep_views):
    """名册必须与团队实际成员一致 —— 否则 planner 收到两份矛盾名单。"""
    want = build_prompt(keep_views)
    cur = conn.execute("SELECT system_prompt FROM agents WHERE id=?", (mid,)).fetchone()
    if cur["system_prompt"] == want:
        log("kept", "主 Agent 名册（已一致）")
        return
    if not apply_:
        log("prompt", "主 Agent 名册", "dry-run")
        return
    conn.execute("UPDATE agents SET system_prompt=? WHERE id=?", (want, mid))
    log("prompt", "主 Agent 名册已更新")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep-views", action="store_true",
                    help="保留 8 个视图 Agent 在团队（只标 stage_order=NULL）")
    args = ap.parse_args()
    apply_ = args.apply

    print("=" * 70)
    print(f"V5b 团队名册收敛  mode={'APPLY' if apply_ else 'DRY-RUN'}")
    if args.keep_views:
        print("  [MODE] --keep-views：8 个视图 Agent 保留在团队内")
    print("=" * 70)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id FROM agents WHERE name=?", (MAIN_NAME,)).fetchone()
        if not row:
            print(f"  [ABORT] 主 Agent {MAIN_NAME} 不存在")
            return 2
        mid = row["id"]

        members = conn.execute(
            """SELECT a.id, a.name, a.display_name, a.stage_order, tm.enabled
               FROM agent_team_members tm JOIN agents a ON a.id=tm.sub_agent_id
               WHERE tm.main_agent_id=? AND a.status='active'""", (mid,)).fetchall()
        cur = {m["name"]: m for m in members}
        print(f"\n  当前 active 成员：{len(members)} 名\n")

        print("── 移出团队（能力由 N 系列承接）──")
        move_list = list(MOVE_OUT)
        if args.keep_views:
            move_list = [x for x in move_list if "视图" not in x[0] and "视图" not in x[1]]
        for name, why in move_list:
            m = cur.get(name)
            if not m:
                log("abnormal", name, "不在团队里（可能已移出）")
                continue
            if m["enabled"] == 0:
                log("kept", name, "已 disabled，无需处理")
                continue
            if not apply_:
                log("disabled", name, f"dry-run  · {why}")
                continue
            conn.execute("UPDATE agent_team_members SET enabled=0 "
                         "WHERE main_agent_id=? AND sub_agent_id=?", (mid, m["id"]))
            log("disabled", name, why)

        print("\n── 保留在团队（旁路能力，N 系列不覆盖）──")
        for name, why in KEEP:
            m = cur.get(name)
            if not m:
                log("abnormal", name, "不在团队里（本来就未入团队）")
                continue
            if m["enabled"] == 1:
                log("kept", name, why)
            elif not apply_:
                log("disabled", name, f"dry-run 重新启用 · {why}")
            else:
                conn.execute("UPDATE agent_team_members SET enabled=1 "
                             "WHERE main_agent_id=? AND sub_agent_id=?", (mid, m["id"]))
                log("kept", name, f"重新启用 · {why}")

        print("\n── 主 Agent 名册（与团队实际成员对齐）──")
        write_prompt(conn, mid, apply_, args.keep_views)

        if apply_:
            conn.commit()
        else:
            conn.rollback()

        # 回读自检
        print("\n" + "=" * 70)
        print("收敛后自检")
        ok = True
        if apply_:
            rows = conn.execute(
                """SELECT a.name, a.display_name, a.stage_order
                   FROM agent_team_members tm JOIN agents a ON a.id=tm.sub_agent_id
                   WHERE tm.main_agent_id=? AND tm.enabled=1 AND a.status='active'
                   ORDER BY a.stage_order is null, a.stage_order""", (mid,)).fetchall()
            names = [r["name"] for r in rows]
            print(f"  收敛后 active 成员：{len(names)} 名")
            for r in rows:
                so = r["stage_order"]
                tag = f"stage={so}" if so is not None else "旁路"
                print(f"    {tag:8} {r['name'][:26]:28}{r['display_name'][:18]}")

            # 名册一致性：system_prompt 的成员必须都在团队里
            # 正则要能捕到**纯中文名**（如「活动图生成」）—— 只写 \S+Agent 会漏掉它们，
            # 造成「以为一致其实漏了 9 个」的自欺。
            import re
            sp = conn.execute("SELECT system_prompt FROM agents WHERE id=?",
                              (mid,)).fetchone()["system_prompt"]
            roster = re.findall(r"^\|\s*([^|]+?)\s*\|[^|]+\|[^|]+\|$", sp, re.M)
            roster = [x.strip() for x in roster
                      if x.strip() and not x.startswith("成员")
                      and "人工确认" not in x]
            db_disp = [r["display_name"] for r in rows]
            missing = [x for x in roster if x not in db_disp]
            extra = [x for x in db_disp if x not in roster]
            print(f"\n  名册（system_prompt）{len(roster)} 个：{roster}")
            print(f"  团队 active {len(db_disp)} 个：{db_disp}")
            if missing:
                print(f"  [FAIL] 名册有但不在团队：{missing}")
                ok = False
            else:
                print("  [OK  ] 名册全部在团队内")
            if extra:
                print(f"  [WARN] 团队有但不在名册（旁路能力，允许）：{extra}")
            if not missing and not extra:
                print("  [OK  ] 名册与团队完全一致")
        else:
            print("  [DRY-RUN] 跳过回读校验")
        print("=" * 70)
        print("✅ 通过" if ok else "❌ 有 FAIL 项")
        print(f"\n统计：{stats}")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
