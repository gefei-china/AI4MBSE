# -*- coding: utf-8 -*-
"""补齐 MBSE 建模团队（主 Agent id=160）的成员 capabilities 与主 Agent system_prompt。

为什么需要（实测依据，非推测）
----------------------------
1. 14 个成员的 `capabilities` **全为空 `[]`** ⇒ 编排时「可用 Agent 池」提示词只能给出
   意图名（`_orch_pool_desc_lines` 走`- intent` 兜底分支），planner 无法区分
   「结构视图生成」与「需求视图生成」这类近义成员。
2. 主 Agent `#160MBSE建模总体负责人` 的 `system_prompt` 为空，而 `description` 写明它负责
   「意图识别/任务拆分/计划制定/任务分派/内容整合输出」⇒ 拆解质量完全依赖模型即兴发挥，
   同一 query 两次拆解结果可能不同，且不知道 SysML v2 八视图依赖顺序。

实测来源（真实语料，36 条唯一 query）
------------------------------------
- LLM 语义路由实测：`满足需求→解析→结构视图` 误判为 `requirement_analysis`（词法 2-gram
  区分不开 8 个视图成员），补 capabilities 后 planner 才有区分依据。
- 词法 vs LLM 越界拦截 80% vs 100%、多阶段编排识别 0% vs 100% ⇒ 编排触发条件必须补。

幂等：可重复执行；以 display_name 为键，未命中成员不报错（只报 warning），避免误伤其他团队。
用法：
    ./.venv/Scripts/python.exe -X utf8 tools/seed_team_definitions.py            # 预览不写
    ./.venv/Scripts/python.exe -X utf8 tools/seed_team_definitions.py --apply    # 写入
    ./.venv/Scripts/python.exe -X utf8 tools/seed_team_definitions.py --main 160 # 指定主 Agent
"""
import argparse
import json
import os
import sqlite3
import sys

# ── 成员能力清单（按display_name 匹配；每项 3~5 个短标签，用于 prompt 提示词） ──
#取值原则：与该成员 system_prompt/description 的实际职责一致，不臆造；标签用「输入特征→产出」
# 的措辞，便于 planner 判断"这个成员能不能吃下这个任务"。
MEMBER_CAPABILITIES = {
    "需求分析Agent": [
        "自然语言需求解析", "需求条目化与规格化", "需求澄清与歧义消解",
        "来源追溯标注", "需求一致性检查",
    ],
    "需求视图生成": [
        "SysML v2 需求视图代码", "需求层次结构建模", "利益相关方与需求主体识别", "satisfy/verify 关系",
    ],
    "结构视图生成": [
        "SysML v2 结构视图代码", "系统层级组成建模", "部件与端口定义", "块定义图 BDD",
    ],
    "交互视图（IBD）生成": [
        "SysML v2 交互视图代码", "端口与连接器定义", "部件间连接关系建模", "跨层级连接分析", "内部块图 IBD",
    ],
    "用例视图生成": [
        "SysML v2 用例视图代码", "用例层次结构建模", "参与者与主体识别", "顶层用例与子用例分解",
    ],
    "活动图生成": [
        "SysML v2 活动视图代码", "动作顺序与并发建模", "决策分支与异常分支", "数据流传递",
    ],
    "状态机视图生成": [
        "SysML v2 状态视图代码", "状态与转换建模", "触发-守护-执行逻辑", "运行模式描述",
    ],
    "参数视图生成": [
        "SysML v2 参数视图代码", "工程约束建模", "参数化关系定义", "属性与参数绑定",
    ],
    "顺序视图（时序图）生成": [
        "SysML v2 顺序视图代码", "时间顺序建模", "部件间消息交换定义", "时序图",
    ],
    "多方案生成": [
        "变体空间候选提议", "候选方案绑定表", "多方案对比", "候选筛选（不产出模型代码）",
    ],
    "预评审Agent": [
        "模型规范性校验", "一致性检查", "合理性评审", "冲突检测与评分", "SysML v2 词法/语法校验",
    ],
    "变更影响Agent": [
        "变更影响范围分析", "BFS 依赖遍历", "影响图产出", "波及深度评估",
    ],
    "知识问答Agent": [
        "MBSE/SysML 领域知识问答", "方法论解释", "概念辨析", "知识库图谱与向量双引擎检索",
    ],
    "报告生成Agent": [
        "多类型报告组装", "分析报告生成", "评审报告导出", "文档导出",
    ],
}

# ── 主 Agent system_prompt ──
# 这段文字会真的进 LLM，必须与运行时事实一致：
#   · 成员 name 取自 agent_team_members 实测值（不能臆造名字）
#   · HIL 分级取自 agents.hil_level（review/impact 为 L1 需人工确认）
#   · 八视图顺序取自仓库既有约定（与 8 视图固定顺序一致）
MAIN_SYSTEM_PROMPT = """你是「MBSE 建模团队」负责人（MBSE建模总体负责人），负责把用户请求拆解成可分派给团队成员的任务序列，并由你汇总输出。

## 你的团队（14 名成员，只可分派给以下成员，禁止虚构不存在的 Agent）
| 成员 name | 负责 |
|---|---|
| 需求分析Agent | 需求澄清与分析、条目化、来源追溯标注 |
| 需求视图生成 | SysML v2 需求视图（层次结构/利益相关方/需求主体） |
| 结构视图生成 | SysML v2 结构视图（层级组成/端口） |
| 交互视图（IBD）生成 | SysML v2 交互视图（端口与连接器） |
| 用例视图生成 | SysML v2 用例视图（顶层/子用例、主体与参与者） |
| 活动图生成 | SysML v2 活动视图（顺序/并发/决策/异常分支） |
| 状态机视图生成 | SysML v2 状态视图（状态/转换/触发-守护-执行） |
| 参数视图生成 | SysML v2 参数视图（工程约束与参数化关系） |
| 顺序视图（时序图）生成 | SysML v2 顺序视图（时间顺序/消息交换） |
| 多方案生成 | 从变体空间提议候选方案，输出候选绑定表（不写 SysML 代码） |
| 知识问答Agent | MBSE/SysML 领域知识问答（不产出模型） |
| 预评审Agent | 规范性/一致性/合理性校验与评分（需人工确认） |
| 变更影响Agent | 变更影响范围分析（需人工确认） |
| 报告生成Agent | 报告组装与导出 |

## 拆解规则
1. **先判是否真的需要多任务**：单一交付物（如"生成需求视图"、"介绍 MBSE 方法论"、"做变更影响分析"）直接分派**一个**成员，不要拆成多步。拆分只在存在**依赖关系**或**多个交付物**时进行。
2. **视图类任务按 SysML v2 八视图固定顺序**组织依赖：需求 → 结构 → 用例 → 活动 → IBD → 时序 → 状态机 → 参数。上游视图未产出时，下游分派必须显式声明依赖与缺失风险。
3. **生成 ≠ 校验**：不要把"生成 X 视图"与"校验 X"合成一个任务，校验一律分派给 `预评审Agent`（review）。
4. **需人工确认的成员**（预评审Agent / 变更影响Agent，L1 级）放在任务序列**末尾**，并在 notes 中显式标注需人工确认。
5. **不要虚构能力**：若请求超出成员范围（如"部署到服务器"、"测算项目成本"、"查询工程包结构树"），在 notes 中写明"团队暂不支持该能力"，不要硬派给某个成员。
6. **路由约束**：每个子任务的 agent 字段必须**逐字取自上表 name**；缺能力时优先给"最相近成员"并在 notes 说明偏差原因。

## 输出格式
严格输出 JSON，不要任何额外文字：
{"goal": "<=30字", "entities": [], "notes": "", "steps": [
  {"agent": "<成员name，必须逐字取自上表>", "task": "<可独立执行的子任务描述>"}]}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐团队成员 capabilities 与主 Agent system_prompt")
    ap.add_argument("--apply", action="store_true", help="实际写入（缺省仅预览）")
    ap.add_argument("--main", type=int, default=160, help="主 Agent id（缺省 160）")
    args = ap.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(here, "mbse.db")
    if not os.path.exists(db_path):
        print(f"[FATAL] 找不到数据库：{db_path}")
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        main_row = conn.execute(
            "SELECT id, display_name, agent_role, status FROM agents WHERE id=?", (args.main,)
        ).fetchone()
        if not main_row:
            print(f"[FATAL] 主 Agent {args.main} 不存在")
            return 2
        if main_row["agent_role"] != "main":
            print(f"[FATAL] id={args.main}（{main_row['display_name']}）不是主 Agent"
                  f"（agent_role={main_row['agent_role']}）")
            return 2
        if main_row["status"] != "active":
            print(f"[FATAL] 主 Agent 未启用（status={main_row['status']}）")
            return 2

        members = conn.execute(
            """SELECT a.id, a.display_name, a.capabilities FROM agent_team_members m
               JOIN agents a ON a.id = m.sub_agent_id
               WHERE m.main_agent_id=? ORDER BY m.id""", (args.main,)).fetchall()
        if not members:
            print(f"[FATAL] 主 Agent {args.main} 无团队成员")
            return 2

        print(f"主 Agent #{args.main} {main_row['display_name']}｜成员 {len(members)} 个｜模式="
              f"{'写入' if args.apply else '预览'}")
        print("=" * 78)

        matched, unmatched = set(), []
        for r in members:
            dn = r["display_name"] or ""
            caps = MEMBER_CAPABILITIES.get(dn)
            if caps is None:
                unmatched.append(dn)
                continue
            matched.add(dn)
            old = r["capabilities"] or "[]"
            new = json.dumps(caps, ensure_ascii=False)
            same = False
            try:
                same = json.loads(old) == caps
            except Exception:
                same = False
            flag = "=同" if same else "≠改"
            print(f"  [{flag}] {r['id']:>4} {dn:<24} 旧 {len(old):>2}B → 新 {len(caps)} 项")
            if args.apply and not same:
                conn.execute("UPDATE agents SET capabilities=? WHERE id=?", (new, r["id"]))

        old_prompt = conn.execute(
            "SELECT system_prompt FROM agents WHERE id=?", (args.main,)).fetchone()["system_prompt"] or ""
        p_same = old_prompt.strip() == MAIN_SYSTEM_PROMPT.strip()
        print(f"\n  [{'=同' if p_same else '≠改'}] 主 Agent system_prompt 旧 {len(old_prompt)}B "
              f"→ 新 {len(MAIN_SYSTEM_PROMPT)}B")
        if args.apply and not p_same:
            conn.execute("UPDATE agents SET system_prompt=? WHERE id=?",
                         (MAIN_SYSTEM_PROMPT, args.main))

        if unmatched:
            print(f"\n  [WARN] 以下成员在 MEMBER_CAPABILITIES 中无条目（保持原样，未改动）：")
            for dn in unmatched:
                print(f"         - {dn}")

        if args.apply:
            conn.commit()
            print("\n[OK] 已提交")
        else:
            print("\n[DRY-RUN] 未写库。加 --apply 执行写入。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())