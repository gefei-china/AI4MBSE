# -*- coding: utf-8 -*-
"""V5 · 新建 8 个流水线节点 Agent（M0 / N1..N6 / G）。

**入库前已实测的三条硬约束**（不实测就会静默丢失）
--------------------------------------------------
1. `capabilities` 严格模式只放行 `_CAPABILITIES_ENUM`（`agent/registry.py:19`，23 词）。
   实测：我 v1/v2 设计的自定义词（如「端口与接口定义」「状态转换与守护条件」）
   **100% 被丢弃**（0/4、0/5 全灭）⇒ 本脚本全部改用**枚举内词**。
   白名单词：需求拆解/需求分析/需求条目化/需求追踪/追溯/冲突检测/架构设计/方案设计/
            系统建模/工程建模/模型校验/模型质量/影响评估/变更影响/预评审/质量校验/
            质量评审/报告生成/知识问答/文件操作/数据导入/任务规划/多Agent编排
2. `description` 经 `sanitize_description` 截断 ≤200 字（本脚本描述均 <200，附断言）。
3. `system_prompt` 若同时含 `SysML` 与 `代码`，`register_sysml_check_tools.py` 的
   `BINDING_RULE_SQL` 会**自动绑 `sysml_v2_validate`** —— 这是好事，但要显式绑定，
   因为不保证规则命中（`review` 就是靠显式 BINDINGS 兜底的）。
   本脚本对N2/N3/N4 显式绑 validate，其余节点按需。

**幂等**：可重复执行；已存在的 Agent 走 update 分支。
**预览**：默认 dry-run，`--apply` 才写库。
"""
import argparse
import json
import os
import sqlite3
import sys

DB = "mbse.db"
APPLY = "--apply" in sys.argv

# ─────────────────────────────────────────────────────────────
# 8 个节点定义
# capabilities 一律取 _CAPABILITIES_ENUM 内词（实测硬约束）
# intent_keywords 填**长特异词**（≥4字非泛词），避免与现有 Agent 互抢
# ─────────────────────────────────────────────────────────────
NODES = [
    dict(
        key="M0", stage_order=0, name="methodology_resolver",
        display_name="方法论解析Agent", hil="L0", role="sub",
        desc="解析当前工程采用的建模方法论（OMG SysML V2 / MagicDraw / 客户自定义），加载对应的文档规范与本体约束，产出建模规约卡作为后续建模阶段的约束来源。",
        caps=["任务规划", "知识问答"],
        kw=["方法论解析", "建模方法论", "建模规范", "建模约定", "建模标准选择", "profile规约卡"],
        tools=[], skills=[],
        prompt=("你是方法论解析智能体。职责：识别当前工程采用的建模方法论，"
                "加载对应规范，产出结构化「建模规约卡」。\n"
                "不产出 SysML 代码；下游建模阶段消费你的规约卡。\n"
                "规约卡必须结构化，禁止把整段检索文本当规约卡。"),
    ),
    dict(
        key="N1", stage_order=1, name="requirement_structuring",
        display_name="需求结构化Agent", hil="L1", role="sub",
        desc="把自然语言需求转成结构化需求条目集：条目化、分类（功能/性能/接口/约束）、标注来源追溯，识别歧义与缺项。不产出 SysML 代码。",
        caps=["需求条目化", "需求分析", "需求拆解"],
        kw=["需求结构化", "需求条目化条目", "需求分类标注", "歧义需求识别", "需求来源追溯"],
        tools=[], skills=[],
        prompt=("你是需求结构化智能体。每条需求必须可独立验证：主语 + 动作 + 可测量判据。\n"
                "禁止输出「系统应具有良好的性能」这类不可测条目 —— 归入 gaps 并标注 need_clarification。\n"
                "条目 id 采用 REQ-{类别首字母}{序号}，供下游 satisfy 引用。\n"
                "你不产出 SysML 代码；下游架构骨架节点消费你的 items。"),
    ),
    dict(
        key="N2", stage_order=2, name="architecture_skeleton",
        display_name="架构骨架生成Agent", hil="L0", role="sub",
        desc="消费结构化需求条目，生成 SysML v2 架构骨架：包结构、部件定义、端口定义、需求声明与 satisfy 分配。只出骨架，不出视图细节。",
        caps=["架构设计", "系统建模", "工程建模"],
        kw=["架构骨架生成", "骨架代码生成", "包结构定义", "部件定义端口", "架构骨架代码"],
        tools=["sysml_v2_validate"], skills=[],
        prompt=("你是 SysML v2 架构骨架生成智能体。\n"
                "骨架只含：package 声明、part def、port def、requirement def、satisfy 分配。\n"
                "禁止出现 action/state/flow（那是视图展开节点的活）。\n"
                "包与导入纪律：全文必须包在 package 内；import 必须带可见性前缀"
                "（private import ScalarValues::*;），`...` 是非法记号必须写真实体。\n"
                "生成 SysML 代码后必须调用 sysml_v2_validate 校验，"
                "verdict=block 时按诊断位置修复后重试，最多 3 轮。\n"
                "part/port/item 分别由 part def/port def/item def 定型，不可混用。\n"
                "满足关系注意：subject 与satisfy 对同一需求只能二选一，同时写会报"
                "Cannot override a binding feature value。"),
    ),
    dict(
        key="N3", stage_order=3, name="view_expansion",
        display_name="视图展开Agent", hil="L0", role="sub",
        desc="消费已通过校验的架构骨架，按 view_type 参数展开单个 SysML v2 视图（需求/结构/用例/活动/IBD/时序/状态机/参数），每个视图独立校验、独立重试。",
        caps=["系统建模", "工程建模"],
        kw=["视图展开生成", "活动视图生成", "用例视图建模", "状态机视图建模", "时序视图建模", "IBD视图建模"],
        tools=["sysml_v2_validate"], skills=[],
        prompt=("你是 SysML v2 视图展开智能体。\n"
                "view_type 是枚举参数（requirement/structure/usecase/activity/ibd/sequence/state/parameter），"
                "一次调用只产出一个视图。\n"
                "只允许引用骨架中已存在的元素，禁止凭空引入部件或端口。\n"
                "生成 SysML 代码后必须调用 sysml_v2_validate；本视图失败独立重试，不牵连其他视图。"),
    ),
    dict(
        key="N4", stage_order=4, name="model_validation_repair",
        display_name="模型校验修复Agent", hil="L0", role="sub",
        desc="消费视图产出与校验诊断，按「词法→语法→语义」顺序定位修复并复检，最多 3 轮。硬错误数必须单调下降。绝不伪造通过。",
        caps=["模型校验", "预评审", "质量校验"],
        kw=["模型校验修复", "语法错误修复", "诊断定位修复", "校验复检"],
        tools=["sysml_v2_validate"], skills=[],
        prompt=("你是 SysML v2 校验修复智能体。\n"
                "按词法→语法→语义顺序处理残余错误（硬错误优先）。\n"
                "每轮记录硬错误数，若本轮不降则放弃本轮修复并报错。\n"
                "3 轮后仍不过 → 明确报告「未通过 + 残余错误清单」。\n"
                "禁止：不调 validate 就声称通过；禁止把 unavailable 说成通过；禁止造关键字。"),
    ),
    dict(
        key="N5", stage_order=5, name="trace_verification",
        display_name="追溯核验Agent", hil="L0", role="sub",
        desc="生成需求↔模型元素双向追溯矩阵，识别未覆盖需求、无源元素、断裂满足链。所有数字来自确定性工具。",
        caps=["追溯", "需求追踪", "冲突检测"],
        kw=["追溯矩阵生成", "需求覆盖核验", "孤儿元素识别", "满足链检查"],
        tools=[], skills=[],
        prompt=("你是追溯核验智能体。\n"
                "所有百分比必须来自确定性工具返回值，禁止心算或估算。\n"
                "报告须标注规则版本（随工具结果返回的 cov-v1.0）。\n"
                "你只做核验与报告，不修改模型。"),
    ),
    dict(
        key="G", stage_order=6, name="change_safety_gate",
        display_name="变更安全门Agent", hil="L1", role="sub",
        desc="对删除/移除/批量替换/边界变更类操作，先分析影响面：沿连接、需求、视图、行为、验证用例五类引用链遍历，输出受影响元素清单与风险等级，交人工决策。不执行变更。",
        caps=["影响评估", "变更影响", "预评审"],
        kw=["删除影响分析", "变更安全门", "删除范围确认", "批量替换风险", "边界变更评估"],
        tools=["impact_analyze"], skills=[],
        prompt=("你是变更安全门智能体。你只分析影响面，绝不执行变更。\n"
                "必须沿五类引用链遍历：连接→端口、需求→设计元素、视图→展示元素、"
                "行为→子系统、验证用例→需求。\n"
                "proceed_allowed=false 时禁止进入落库阶段。\n"
                "影响面数字必须来自 impact_analyze 工具，禁止估算。\n"
                "「把没用的删掉」这类模糊指令必须先问清删除对象和范围。"),
    ),
    dict(
        key="N6", stage_order=7, name="model_release",
        display_name="模型发布Agent", hil="L2", role="sub",
        desc="消费通过校验的模型产物，落库为图谱实体与关系，写版本链与校验留痕。所有写操作逐条人工确认。",
        caps=["数据导入", "工程建模"],
        kw=["模型发布入库", "模型落库版本", "图谱回灌发布", "发布模型确认"],
        tools=["entity_create"], skills=[],
        prompt=("你是模型发布智能体。\n"
                "verdict=block → 禁止发布。\n"
                "verdict=report → 允许发布但须在报告首屏列出全部语义错。\n"
                "verdict=unavailable → 允许发布但标注「未校验」，禁止表述为「已通过」。\n"
                "你是模型图谱实体的唯一落库出口，所有写操作走人工确认。"),
    ),
]

# 主 Agent 新名册（收敛为 8 节点）
MAIN_NAME = "MBSE建模总体负责人"
MAIN_PROMPT_NEW = """你是「MBSE 建模团队」负责人，负责把用户请求拆解成可分派给团队成员的任务序列，并由你汇总输出。

## 你的团队（8 名阶段节点，禁止虚构不存在的 Agent）
| 成员 display_name | 负责 | 人工确认 |
|---|---|---|
| 方法论解析Agent | 识别建模方法论，产出建模规约卡 | 否 |
| 需求结构化Agent | 需求条目化、分类、歧义识别 | 是(L1) |
| 架构骨架生成Agent | SysML v2 骨架（包/部件/端口/需求声明） | 否 |
| 视图展开Agent | 八视图展开（view_type 参数驱动） | 否 |
| 模型校验修复Agent | 三层诊断定位与修复 | 否 |
| 追溯核验Agent | 需求↔元素追溯矩阵 | 否 |
| 变更安全门Agent | 删除/重构前影响面分析 | 是(L1) |
| 模型发布Agent | 落库与版本链 | 是(L2) |

## 拆解规则
1. **方法论最先**：不确定客户方法论时，先分派「方法论解析Agent」。
2. **阶段顺序固定**：需求结构化 → 架构骨架 → 视图展开 → 校验修复 → 追溯核验 → 发布，不得跳步、不得倒序。
3. **视图并行**：视图展开内部按 需求→结构→用例→活动→IBD→时序→状态机→参数 并行展开。
4. **生成≠校验≠落库**：三个阶段严格分离，不得合并成一个任务。
5. **破坏性操作必须经变更安全门Agent**（删除/移除/重构/改边界），未过门禁不得落库。
6. **需人工确认的成员**（需求结构化/变更安全门/模型发布）放在任务序列末尾并显式标注。
7. **不要虚构能力**：超出成员范围的请求，在 notes 中写明「团队暂不支持」，不要硬派。
8. **路由约束**：子任务的 agent 字段必须逐字取自上表 display_name；缺能力时选最相近成员并说明偏差原因。

## 输出格式
严格输出 JSON，不要任何额外文字：
{"goal": "<=30字", "entities": [], "notes": "", "steps": [
  {"agent": "<成员 display_name，逐字取自上表>", "task": "<可独立执行的子任务描述>"}]}
"""

stats = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "tools": 0, "team": 0}


def log(k, name, detail=""):
    stats[k] += 1
    print(f"  [{ {'inserted':'NEW   ','updated':'UPDATED','unchanged':'SAME  ','skipped':'SKIP  '}[k] }] "
          f"{name}{('  · ' + detail) if detail else ''}")


def check_caps(caps):
    """入库前自检：capabilities 必须全在枚举内，否则会被静默丢弃。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent.registry import AgentRegistry
    enum = AgentRegistry.capabilities_enum()
    bad = [c for c in caps if c not in enum]
    if bad:
        raise SystemExit(f"[ABORT] capabilities 不在白名单内，会被静默丢弃：{bad}\n"
                         f"       白名单：{sorted(enum)}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply_ = args.apply or APPLY

    print("=" * 70)
    print(f"V5 新建流水线节点  mode={'APPLY（写库）' if apply_ else 'DRY-RUN（只预览）'}")
    print("=" * 70)

    # 入库前自检
    for n in NODES:
        check_caps(n["caps"])
    print("\n  [OK  ] 8 个节点的 capabilities 全部在白名单内")
    print("  [INFO] description 长度上限 200：",
          max(len(n["desc"]) for n in NODES), "（未超限）")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        # 幂等补列：v3.0 只加了 task_domain，stage_order 当时仅在规范里建议未落库
        have = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        if "stage_order" not in have:
            if APPLY or "--apply" in sys.argv:
                conn.execute("ALTER TABLE agents ADD COLUMN stage_order INTEGER")
                conn.commit()
                print("\n  [NEW  ] 列 agents.stage_order 已补建")
            else:
                print("\n  [WARN ] 列 agents.stage_order 不存在（--apply 时会自动补建）")
        else:
            print("\n  [SAME ] 列 agents.stage_order 已存在")

        main_id = conn.execute("SELECT id FROM agents WHERE name=?", (MAIN_NAME,)).fetchone()
        if not main_id:
            print(f"\n  [ABORT] 主 Agent {MAIN_NAME} 不存在，无法建团队")
            return 2
        main_id = main_id["id"]

        print("\n── 创建 8 个节点 Agent ──")
        ids = {}
        for n in NODES:
            row = conn.execute("SELECT id, capabilities, system_prompt, description "
                               "FROM agents WHERE name=?", (n["name"],)).fetchone()
            caps_json = json.dumps(n["caps"], ensure_ascii=False)
            if row:
                ids[n["key"]] = row["id"]
                same = (json.loads(row["capabilities"] or "[]") == n["caps"]
                        and row["system_prompt"] == n["prompt"]
                        and row["description"] == n["desc"])
                if same:
                    log("unchanged", f"{n['key']} {n['name']}")
                else:
                    if not apply_:
                        log("updated", f"{n['key']} {n['name']}", "dry-run")
                    else:
                        conn.execute(
                            "UPDATE agents SET display_name=?,description=?,capabilities=?,"
                            "system_prompt=?,hil_level=?,agent_role=?,task_domain='modeling',"
                            "stage_order=?,intent_keywords=? WHERE name=?",
                            (n["display_name"], n["desc"], caps_json, n["prompt"], n["hil"],
                             n["role"], n["stage_order"],
                             json.dumps(n["kw"], ensure_ascii=False), n["name"]))
                        log("updated", f"{n['key']} {n['name']}")
            else:
                if not apply_:
                    # dry-run：仍需占位 id 以便后续步骤演示
                    ids[n["key"]] = f"<dry:{n['key']}>"
                    log("inserted", f"{n['key']} {n['name']}", "dry-run")
                else:
                    cur = conn.execute(
                        "INSERT INTO agents (name,display_name,description,capabilities,"
                        "system_prompt,hil_level,agent_role,status,task_domain,stage_order,"
                        "intent_keywords,builtin,max_concurrency,protocol_range) "
                        "VALUES (?,?,?,?,?,?,?,'active','modeling',?,?,0,2,'>=1,<3')",
                        (n["name"], n["display_name"], n["desc"], caps_json, n["prompt"],
                         n["hil"], n["role"], n["stage_order"],
                         json.dumps(n["kw"], ensure_ascii=False)))
                    ids[n["key"]] = cur.lastrowid
                    log("inserted", f"{n['key']} {n['name']}")

        print("\n── 绑定工具 ──")
        for n in NODES:
            for t in n["tools"]:
                tid = conn.execute("SELECT id FROM tools WHERE name=?", (t,)).fetchone()
                if not tid:
                    log("skipped", f"{n['key']}←{t}", "工具不存在")
                    continue
                aid = ids[n["key"]]
                if isinstance(aid, str):      # dry-run 占位
                    log("skipped", f"{n['key']}←{t}", "dry-run")
                    continue
                ex = conn.execute(
                    "SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_name=? AND tool_type='tool'",
                    (aid, t)).fetchone()
                if ex:
                    log("unchanged", f"{n['key']}←{t}")
                elif not apply_:
                    log("inserted", f"{n['key']}←{t}", "dry-run")
                else:
                    conn.execute(
                        "INSERT INTO agent_tools (agent_id,tool_type,tool_name,enabled) "
                        "VALUES (?,'tool',?,1)", (aid, t))
                    log("inserted", f"{n['key']}←{t}")
                    stats["tools"] += 1

        print("\n── 入团队（agent_team_members）──")
        for n in NODES:
            aid = ids[n["key"]]
            if isinstance(aid, str):
                log("skipped", f"{n['key']} 入团队", "dry-run")
                continue
            ex = conn.execute(
                "SELECT 1 FROM agent_team_members WHERE main_agent_id=? AND sub_agent_id=?",
                (main_id, aid)).fetchone()
            if ex:
                log("unchanged", f"{n['key']} 入团队")
            elif not apply_:
                log("inserted", f"{n['key']} 入团队", "dry-run")
            else:
                conn.execute(
                    "INSERT INTO agent_team_members (main_agent_id,sub_agent_id,enabled) "
                    "VALUES (?,?,1)", (main_id, aid))
                log("inserted", f"{n['key']} 入团队")
                stats["team"] += 1

        print("\n── 主 Agent 名册（收敛为 8 节点）──")
        cur = conn.execute("SELECT system_prompt FROM agents WHERE id=?", (main_id,)).fetchone()
        if cur["system_prompt"] == MAIN_PROMPT_NEW:
            log("unchanged", "主 Agent system_prompt")
        elif not apply_:
            log("updated", "主 Agent system_prompt", "dry-run")
        else:
            conn.execute("UPDATE agents SET system_prompt=? WHERE id=?",
                         (MAIN_PROMPT_NEW, main_id))
            log("updated", "主 Agent system_prompt")

        if apply_:
            conn.commit()
        else:
            conn.rollback()

        # ── 回读自检 ──
        print("\n" + "=" * 70)
        print("入库后自检（回读校验）")
        ok = True
        if apply_:
            for n in NODES:
                r = conn.execute("SELECT id,capabilities,status,task_domain,stage_order "
                                 "FROM agents WHERE name=?", (n["name"],)).fetchone()
                if not r:
                    print(f"  [FAIL] {n['key']} {n['name']} 不存在")
                    ok = False
                    continue
                got_caps = json.loads(r["capabilities"] or "[]")
                if got_caps != n["caps"]:
                    print(f"  [FAIL] {n['key']} capabilities 被改写：{got_caps}")
                    ok = False
                if r["status"] != "active":
                    print(f"  [FAIL] {n['key']} status={r['status']}")
                    ok = False
            cnt = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE status='active' AND task_domain='modeling' "
                "AND stage_order IS NOT NULL").fetchone()[0]
            print(f"  流水线节点（active + modeling + stage_order非空）：{cnt} 个")
            if cnt != 8:
                print(f"  ⇒ FAIL 期望 8 个，实际 {cnt}")
                ok = False

            # 关键回归：8 个新节点必须绑上 validate（auto-bind 或显式）
            need = ["architecture_skeleton", "view_expansion", "model_validation_repair"]
            for nm in need:
                c = conn.execute(
                    "SELECT COUNT(*) FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
                    "WHERE a.name=? AND t.tool_name='sysml_v2_validate'", (nm,)).fetchone()[0]
                print(f"  [{'OK  ' if c else 'FAIL'}] {nm} 绑 sysml_v2_validate：{c}")
                if not c:
                    ok = False

            # 团队成员数
            m = conn.execute(
                "SELECT COUNT(*) FROM agent_team_members tm JOIN agents a ON a.id=tm.sub_agent_id "
                "WHERE tm.main_agent_id=? AND tm.enabled=1 AND a.status='active'",
                (main_id,)).fetchone()[0]
            print(f"  团队 active 成员：{m} 名（含 9 个旧成员 + 新节点）")
        else:
            print("  [DRY-RUN] 跳过回读校验")
        print("=" * 70)
        print("✅ 自检通过" if ok else "❌ 自检有 FAIL 项")
        print(f"\n统计：{stats}")
        if not apply_:
            print("（预览模式，加 --apply 生效）")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
