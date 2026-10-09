"""migrate_v14_pipeline_wiring — 接通建模流水线 N0~N6：**路由词 + skill 绑定**。

## 背景（2026-10-08 实测，本轮的核心发现）

上一轮只给 N3 `view_expansion` 绑了 8 个视图 skill，并宣布"链路修好了"。
用户追问"技能没绑定到 agent，流程怎么跑通的"⇒ 实测发现**整条链路在真实入口是断的**：

    用户："生成架构骨架"     →意图路由 → chat（闲聊）      ❌
    用户："生成活动图视图"   → 意图路由 → design（通用设计）❌
    用户："需求条目化"       → 意图路由 → knowledge_qa      ❌

两个断点：

**断点1 · 路由词与用户口语语序相反**
`agents.intent_keywords` 登记的是内部黑话「架构骨架生成」，
而用户说的是「生成架构骨架」⇒ `_kw_score` 走**无边界子串匹配**（`k in text_low`）
⇒ 必然不命中 ⇒ 落到 `chat` / `design` / `knowledge_qa` 等通用 Agent。

**断点 2 · 8 个节点里 7 个 skill 绑定为 0**
实测只有 N3 绑了 8 个；N0/N1/N2/N4/N5/G/N6 全是 0，
其中 **N2 架构骨架是所有视图的输入源**，它没有 skill ⇒
模型生成骨架时没有任何规范正文 ⇒ 这正是上一轮"模型言不由衷"
（sequence 的自述与代码不符）的深层原因之一。

## 为什么上一轮的验证没发现

`verify_n3_e2e_real.py` 用 `forced_intent="view_expansion"` **直连**，
绕过意图识别与编排；骨架又是脚本预先造好塞进去的，绕过了 N2。
⇒ 那条链路只有验证脚本自己在用，**用户从 UI 走不到**。
（教训见 MEMORY：验证必须走真实入口，`forced_intent` 直连只能证明"能力存在"，
不能证明"链路可达"。）

## 关键词设计约束（从 `agent/intent.py` 实读，不是猜的）

`_db_kw_priority` → `_kw_score` 的真实规则：
1. **只认 ≥4 字的非泛词**（`_DB_KW_PRIORITY_MIN_SCORE = 1.0`）；
2. `_GENERIC_KW` 里的词（视图/需求/模型/生成/校验/架构/设计/代码/建模…）**不计分**；
3. 两个例外会跳过整块 db 层：`SP-R 报告命令`、`_is_composite_task`
   （输入含 建模/代码/校验/并/同时/然后/并且/再/以及/还有）
   ⇒ **复合任务里细粒度节点不会抢路由**（走 design 编排，这是 P1-14 的既定设计）。
4. 同分时先出现的意图胜出⇒ 各节点词条**必须互斥**（本脚本有重复检查）。

⚠️ 因此词条按**用户真实语序**写（"生成架构骨架" 而非 "架构骨架生成"）。

## 只改两张表

- `agents.intent_keywords`（路由）
- `agent_tools`（skill 绑定，走 `AgentRepo.add_tool`）

**不改 skills / plugins 内容** ⇒ 无需 `legacy_sync` 同步桥。

## 幂等 / 回滚

- 绑定用 `INSERT OR IGNORE`（`AgentRepo.add_tool`），重复跑不重复插；
- 路由词是整体覆盖式更新，脚本会打印改动前原值，照抄即可回滚；
- 断言：8 个节点**都**至少绑 1 个 skill，且词条**无跨节点重复**。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── 路由关键词：按用户真实语序写；≥4 字非泛词；跨节点互斥（脚本内断言）──
INTENT_KEYWORDS = {
    "methodology_resolver": ["方法论解析", "建模方法论", "建模规范选择",
                             "建模约定", "建模标准选择"],
    "requirement_structuring": ["需求条目化", "生成需求条目", "需求结构化",
                                "条目化需求"],
    "architecture_skeleton": ["生成架构骨架", "架构骨架", "骨架代码生成",
                              "生成骨架代码", "建立架构骨架"],
    "view_expansion": ["生成活动图", "生成状态机视图", "生成用例视图", "生成时序图",
                       "生成参数视图", "生成需求视图", "生成结构视图",
                       "生成交互视图", "展开视图"],
    "model_validation_repair": ["模型校验修复", "校验修复模型", "修复模型校验",
                                "生成校验诊断"],
    "trace_verification": ["生成追溯矩阵", "追溯矩阵生成", "需求覆盖核验",
                           "覆盖性分析"],
    "change_safety_gate": ["变更安全门", "删除影响分析", "变更影响评估",
                           "批量替换风险"],
    "model_release": ["模型发布入库", "发布模型", "模型落库版本",
                      "模型回灌发布"],
}

# ── skill 绑定：节点 → skills 表里的 skill name ──
#    依据 skills 表 description 逐条核对归属（2026-10-08 实测列过全表）
SKILL_BINDINGS = {
    "methodology_resolver": [
        "sysml_methodology_profile_guide",      # 方法论规约卡解析
    ],
    "requirement_structuring": [
        "sysml_requirement_structuring_guide",  # 需求条目化（句切分/分类/reqId）
        "sysml_requirement_to_model_method",    # 需求→SysML 元素映射方法论
    ],
    "architecture_skeleton": [
        "sysml_skeleton_generation_guide",      # 只产 package/part def/port def
        "sysml_stdlib_reference_guide",         # 查标准库，禁止臆造成员名
        "sysml_requirement_to_model_method",    # 满足关系怎么建
    ],
    "view_expansion": [
        # 8 个视图 skill 本轮上一批（migrate_v12）已绑；这里补统一指南
        "sysml_view_generation_guide",          # 八视图顺序 + view_type 是参数
    ],
    "model_validation_repair": [
        "sysml_validation_repair_loop",         # 先 diagnose 再 fix，轮次预算
        "SysML v2 校验与修复",                    # 三路诊断 + 最多 3 轮
    ],
    "trace_verification": [
        "sysml_trace_coverage_analysis",        # 双向追溯矩阵 + gap_summary
    ],
    "change_safety_gate": [
        "sysml_change_safety_analysis",         # 五类引用链影响面
    ],
    "model_release": [
        "sysml_release_checklist",              # 发布门禁集合 + 留痕
    ],
}


def _validate_keywords():
    """用 intent.py 的**真实规则**校验词条（不靠肉眼判断）。"""
    from agent.intent import IntentRouter
    gen = IntentRouter._GENERIC_KW
    problems = []
    seen = {}
    for agent, kws in INTENT_KEYWORDS.items():
        for k in kws:
            if len(k) < 4:
                problems.append(f"{agent}/{k}: 长度 {len(k)} <4，不计分")
            if k in gen:
                problems.append(f"{agent}/{k}: 属泛词，不计分")
            if k in seen:
                problems.append(f"{agent}/{k}: 与 {seen[k]} 词条重复（会互抢）")
            seen[k] = agent
    if problems:
        for p in problems:
            print("  [FAIL]", p)
        return False
    print(f"  关键词校验通过：{len(seen)} 个词条，全部 ≥4 字非泛词且跨节点互斥")
    return True


def main() -> int:
    from database import db_conn
    from repositories.agent_repo import AgentRepo

    print("=" * 72)
    print("① 关键词校验（按 agent/intent.py 的真实打分规则）")
    print("=" * 72)
    if not _validate_keywords():
        print("\n关键词不合规，拒绝写入（避免路由错乱）")
        return 1

    with db_conn() as conn:
        repo = AgentRepo(conn)

        # ── 前置校验：所有 Agent 与 skill 都必须真实存在 ──
        print("\n" + "=" * 72)
        print("② 前置校验（Agent / skill 是否存在且可用）")
        print("=" * 72)
        missing = []
        for agent in list(INTENT_KEYWORDS) + list(SKILL_BINDINGS):
            if not repo.one("SELECT id FROM agents WHERE name=?", (agent,)):
                missing.append(f"Agent 不存在：{agent}")
        seen_sk = set()
        for agent, names in SKILL_BINDINGS.items():
            for nm in names:
                if nm in seen_sk:
                    continue          # 同一 skill 可服务多个节点（如需求→模型映射）
                seen_sk.add(nm)
                s = repo.one("SELECT id, status, enabled, length(content) ln "
                             "FROM skills WHERE name=?", (nm,))
                if not s:
                    missing.append(f"skill 不存在：{nm}（拟绑给 {agent}）")
                elif s["status"] != "published" or not s["enabled"]:
                    missing.append(f"skill 不可用：{nm} status={s['status']} "
                                   f"enabled={s['enabled']}")
                elif not (s["ln"] or 0):
                    missing.append(f"skill 正文为空：{nm}")
        if missing:
            print("[FAIL] 以下缺失，拒绝写入：")
            for m in missing:
                print("   -", m)
            return 1
        print(f"  通过：{len(INTENT_KEYWORDS)} 个 Agent、{len(seen_sk)} 个 skill 均可用")

        # ── 写路由词 ──
        print("\n" + "=" * 72)
        print("③ 更新 intent_keywords（改动前原值，供回滚）")
        print("=" * 72)
        for agent, kws in INTENT_KEYWORDS.items():
            old = repo.one("SELECT intent_keywords FROM agents WHERE name=?",
                           (agent,))["intent_keywords"]
            print(f"  {agent}\n     旧: {old}\n     新: {kws}")
            repo.execute("UPDATE agents SET intent_keywords=? WHERE name=?",
                         (json.dumps(kws, ensure_ascii=False), agent))
        conn.commit()

        # ── 写skill 绑定 ──
        print("\n" + "=" * 72)
        print("④ 绑定 skill")
        print("=" * 72)
        for agent, names in SKILL_BINDINGS.items():
            aid = repo.one("SELECT id FROM agents WHERE name=?", (agent,))["id"]
            for nm in names:
                repo.add_tool(aid, "skill", nm)      # INSERT OR IGNORE
            now = repo.rows("SELECT tool_name FROM agent_tools "
                            "WHERE agent_id=? AND tool_type='skill'", (aid,))
            print(f"  {agent:28} skill 绑定 {len(now)} 个: "
                  f"{[x['tool_name'] for x in now]}")
        conn.commit()

        # ── 出口断言 ──
        print("\n" + "=" * 72)
        print("⑤ 出口断言")
        print("=" * 72)
        bad = []
        for agent in INTENT_KEYWORDS:
            aid = repo.one("SELECT id FROM agents WHERE name=?", (agent,))["id"]
            n = repo.one("SELECT count(*) n FROM agent_tools "
                         "WHERE agent_id=? AND tool_type='skill'", (aid,))["n"]
            if n < 1:
                bad.append(f"{agent} 仍无 skill 绑定")
            else:
                print(f"  [OK  ] {agent:28} skill={n}")
        assert not bad, "；".join(bad)

    # ── 真消费方验证：registry 能否读回（绑定 ≠ 生效）──
    print("\n" + "=" * 72)
    print("⑥ registry 读回验证")
    print("=" * 72)
    from agent.registry import AgentRegistry
    reg = AgentRegistry()
    with db_conn() as c2:
        reg.load_from_db(c2)
    for agent in INTENT_KEYWORDS:
        bs = reg.get_bound_skills(agent)
        empty = [b["name"] for b in bs if not (b.get("content") or "").strip()]
        assert bs, f"{agent} 读不到绑定 skill"
        assert not empty, f"{agent} 这些绑定 skill 正文为空：{empty}"
        print(f"  [OK  ] {agent:28} 读回 {len(bs)} 个，正文均非空")
    print("\n✅ 全部通过：8 个节点均已绑定 skill，路由词合规")
    return 0


if __name__ == "__main__":
    sys.exit(main())