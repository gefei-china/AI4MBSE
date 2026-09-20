# -*- coding: utf-8 -*-
"""覆盖性分析工具组注册（幂等）：tools 4 行 + agent_tools 绑定 + skills 1 行（完整提示词）。

写的是**配置表**（工具注册/Skill 沉淀），不触碰图谱模型数据；
重复执行安全（先查后插）。
"""
import sqlite3
import json
import sys

DB = "mbse.db"

TOOLS = [
    {
        "name": "coverage_matrix",
        "description": ("需求架构覆盖矩阵分析（SRS-GN-CO-XQJG，确定性只读）：需求实体×架构元素的追溯矩阵，"
                        "输出覆盖率、未覆盖需求、无需求依据的架构元素、异常追溯关系（断链/自环）。"
                        "数字可复现可审计（规则版本 cov-v1.0，随结果返回）。参数 branch/project_id 可选。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "分析分支（如 release/dev），省略=全部分支"},
                "project_id": {"type": "string", "description": "项目维度过滤，省略=全部项目"},
            },
        },
    },
    {
        "name": "trace_chain_check",
        "description": ("端到端追溯链连通性检查（SRS-GN-CO-JKJH，确定性只读）：对每条需求沿 "
                        "SATISFIES/DERIVES/ALLOCATED_TO/VERIFIED_BY 做无向 BFS，输出是否可达架构元素、"
                        "是否到达验证、链级数与断点。可传 req_name 过滤单条需求。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"}, "project_id": {"type": "string"},
                "req_name": {"type": "string", "description": "需求名称/ID 关键字，过滤单条需求"},
            },
        },
    },
    {
        "name": "scene_coverage",
        "description": ("场景链路与工况覆盖检查（SRS-GN-CO-CJGK，确定性只读）：场景/用例/模式/工况类实体的"
                        "关联链检查，识别孤立场景、缺活动/状态链、缺参与对象的场景。"),
        "input_schema": {
            "type": "object",
            "properties": {"branch": {"type": "string"}, "project_id": {"type": "string"}},
        },
    },
    {
        "name": "gap_summary",
        "description": ("缺项覆盖汇总（SRS-GN-CO-QXFG，确定性只读）：聚合矩阵/追溯链/场景三类发现，"
                        "按规则 cov-v1.0 风险分级（high=异常追溯/未覆盖系统需求；medium=未覆盖其他需求/场景断点；"
                        "low=无需求依据架构元素），输出分类统计+定位信息。解读与补全建议由你（Agent）基于结果生成。"),
        "input_schema": {
            "type": "object",
            "properties": {"branch": {"type": "string"}, "project_id": {"type": "string"}},
        },
    },
]

BIND_AGENTS = ["knowledge_qa", "design", "requirement_analysis"]

SKILL_FRONTMATTER = """---
name: 覆盖性分析
description: 需求-架构-验证覆盖性分析（SRS-GN-CO）：调用确定性工具取数 → 按方法论解读 → 给缺项补全建议；数字必须来自工具，不心算
category: 覆盖性分析
version: v1.0
allowed_tools: coverage_matrix,trace_chain_check,scene_coverage,gap_summary
---

# 覆盖性分析（SRS-GN-CO）

你负责需求-架构覆盖性分析。**数字一律来自确定性工具，你负责编排、解读、建议**——
这是本 Skill 的第一纪律：覆盖率/断链/缺项是可审计数字，评审时要能对拍复现，
你心算或估算的任何百分比都是伪造。

## 何时走本流程

用户问到：覆盖率、覆盖矩阵、哪些需求没被覆盖/满足/验证、追溯链是否完整、
场景/用例有没有模型实现、缺项/缺口/风险清单——任意一个，就走本流程。

## 工具与调用序（按用户问题裁剪，不必每次全跑）

| 工具 | 回答什么 | 何时必调 |
|---|---|---|
| `coverage_matrix` | 覆盖率、未覆盖需求、无需求依据的架构元素、断链/自环 | 问覆盖率/覆盖矩阵/缺需求 |
| `trace_chain_check` | 单条或全部需求的追溯链深度、是否到达验证 | 问某需求"落实了吗/验证了吗" |
| `scene_coverage` | 场景/用例是否缺活动链/参与对象 | 问场景、用例、模式、工况 |
| `gap_summary` | 全部缺项的分级清单（汇总前三者） | 问缺项/风险/改进清单；**正式分析报告必调** |

调用序：**矩阵 → 追溯链 → 场景 → 缺项汇总**（汇总消费前三者，单独跑会遗漏）。
分支/项目维度：用户没说就**先不传参跑全量**，发现多分支/多项目数据混杂时，
再分维度重跑对比（同库曾实测 release/dev 覆盖率差异巨大，一维混算会误导）。

## 结果解读模板

1. **先报口径**：分析范围（branch/project）、实体数、关系数、规则版本（结果 scope.rule_version）。
   没有口径的覆盖率没有意义。
2. **再报数字**：覆盖率、验证率、缺项总数（按风险分级 high/medium/low 排列）。
3. **后给解读**（LLM 的价值区）：
   - 未覆盖需求聚类：哪些缺项同属一个子系统/场景？优先补哪一个能带动最多缺项？
   - 无需求依据的架构元素：区分"顶层系统/基础设施（可接受）"与"功能部件（风险）"；
   - 断链/自环：给出关系 id，这是数据质量问题，建议修数据而非改模型；
   - 场景断点：指出缺活动链的场景名与建议补的行为元素类型。
4. **补全建议**：针对 high 风险缺项给出具体建议（建议新增什么类型的元素/关系、
   挂到哪个现有实体下），并说明这些建议**仅供参考，需设计师确认后人工操作**——
   你不直接写图谱。

## 硬性纪律

- **不调用不得声称**：本回合没调过工具，就不得报任何覆盖率/缺项数。
- **不伪造精确性**：工具返回多少就报多少；coverage_rate 为 null（无需求）就如实说"无需求实体可分析"。
- **不复述整张矩阵**：矩阵大时只讲统计+最关键的缺项，提示用户可在结果中查看全量。
- **不越权写入**：本流程全只读；任何"帮我把这个关系补上"的请求 → 引导走图谱工作区·人工确认流程。
- **口径变化要声明**：若发现结果 scope.rule_version 不是 cov-v1.0，说明口径已升级，提示用户重新分析历史结论。
"""

SKILL_CONTENT = SKILL_FRONTMATTER.split('---\n', 2)[2].lstrip('\n')


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    added_tools, bound, skill_action = [], [], ""

    # 1) tools 表（幂等）
    for t in TOOLS:
        row = cur.execute("SELECT id FROM tools WHERE name=?", (t["name"],)).fetchone()
        if row:
            cur.execute(
                "UPDATE tools SET description=?, input_schema=?, status='active', side_effect='read', source='coverage' WHERE id=?",
                (t["description"], json.dumps(t["input_schema"], ensure_ascii=False), row["id"]))
        else:
            cur.execute(
                "INSERT INTO tools (name, source, description, status, input_schema, side_effect, risk_level, builtin) "
                "VALUES (?, 'coverage', ?, 'active', ?, 'read', 'low', 1)",
                (t["name"], t["description"], json.dumps(t["input_schema"], ensure_ascii=False)))
            added_tools.append(t["name"])

    # 2) agent_tools 绑定（幂等）
    for agent_name in BIND_AGENTS:
        a = cur.execute("SELECT id FROM agents WHERE name=?", (agent_name,)).fetchone()
        if not a:
            print(f"  ! agent 不存在，跳过绑定: {agent_name}")
            continue
        for t in TOOLS:
            ex = cur.execute("SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_name=?",
                             (a["id"], t["name"])).fetchone()
            if not ex:
                cur.execute("INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) VALUES (?, 'tool', ?, 1)",
                            (a["id"], t["name"]))
                bound.append(f"{agent_name}:{t['name']}")

    # 3) skills 表（幂等：按 name 更新/插入）
    ex = cur.execute("SELECT id FROM skills WHERE name='覆盖性分析'").fetchone()
    if ex:
        cur.execute(
            "UPDATE skills SET frontmatter=?, content=?, status='published', enabled=1, "
            "skill_type='package', allowed_tools=?, updated_at=datetime('now','localtime') WHERE id=?",
            (SKILL_FRONTMATTER.split('---\n')[2].rsplit('---', 1)[0].strip('\n'),
             SKILL_CONTENT,
             "coverage_matrix,trace_chain_check,scene_coverage,gap_summary", ex["id"]))
        skill_action = "updated"
    else:
        cur.execute(
            "INSERT INTO skills (name, skill_type, description, status, enabled, frontmatter, content, "
            "category, allowed_tools, created_at, updated_at) VALUES "
            "('覆盖性分析', 'package', '需求-架构-验证覆盖性分析（SRS-GN-CO）：确定性工具取数+方法论解读', "
            "'published', 1, ?, ?, '覆盖性分析', ?, datetime('now','localtime'), datetime('now','localtime'))",
            (SKILL_FRONTMATTER.split('---\n')[2].rsplit('---', 1)[0].strip('\n'),
             SKILL_CONTENT,
             "coverage_matrix,trace_chain_check,scene_coverage,gap_summary"))
        skill_action = "inserted"

    conn.commit()
    conn.close()
    print(f"tools added={added_tools} (others updated in place)")
    print(f"agent_tools bound={len(bound)}: {bound}")
    print(f"skill: {skill_action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
