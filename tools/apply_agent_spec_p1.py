# -*- coding: utf-8 -*-
"""重写 6 个 Agent 的 description / capabilities / 清理泛词关键词（2026-11-07，P1）。

## 为什么只改这三项、不碰 system_prompt

`tools/verify/verify_agent_spec.py` 的实测判据：**语义路由文本 = display_name + description
+ keywords，system_prompt 不参与路由**（`agent/pipeline.py:88`）。
⇒ 「路由不准」的修法在 description/keywords，不在 prompt。
prompt 决定的是"接活之后怎么干"，改动风险高得多（会改模型输出行为），不在本轮范围。

## 三条写作规则（与自检器 A1/A2/A3 一一对应）

1. **80~200 字符**：太短 ⇒ 语义层无信号（实测现有 17~36 字符，全部落到 chat）
2. **三段式**：`<动作> <对象>，<输出物>` + `用户说「话术」时用我` + `不负责 X（交给 Y）`
3. **边界必须写**：路由是**互斥选择**，不说"不接什么"，就会被邻近 Agent 抢

## 泛词为什么要清

`agent/intent.py::_GENERIC_KW` 里的词（设计/建模/检查/校验/报告/需求/知识库/资料/方法论…）
在关键词层得分被**硬压到 0**（`_strong >= 1.0` 才认），但它们仍是**竞争候选**，
会把路由拉成 `fused_conflict`。⇒ 留着不加分、只添乱。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/apply_agent_spec_p1.py --dry-run
    ./.venv/Scripts/python.exe -X utf8 tools/apply_agent_spec_p1.py --apply
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

#: name → (新 description, 新 capabilities, 要删的关键词, 要补的关键词)
#:
#: ⚠️ 为什么必须"补"而不能只"删"：关键词层打分是 `min(1.0, len(k)/4.0)`
#:    （`agent/intent.py::_scored_keyword_pick`），门槛 `_DB_KW_PRIORITY_MIN_SCORE=1.0`
#:    ⇒ **只有 4 字及以上的词才能构成强特异信号**，3 字词（如"预评审"）得 0.75 不够。
#:    只删泛词的结果是"关键词层彻底无信号"，全靠语义层兜 —— 那不是提升，是丢掉一层判据。
#:    所以删掉泛词的同时要补**该 Agent 独有的 4 字词**，且补之前必须查重（同词多 Agent
#:    会让路由 fused_conflict，实测 `需求条目` 已被 requirement_structuring 占用）。
SPEC = {
    "design": (
        "基于需求与知识库做**系统架构方案设计**：给出多套候选架构，从性能/成本/风险/"
        "可扩展性权衡比较，输出推荐方案与选型理由，可按需产出 SysML v2 建模代码。"
        "用户说「设计架构」「出个方案」「系统怎么划分」「建模」时用我。"
        "不负责单视图代码展开（交给视图 Agent），不做预评审打分（交给预评审 Agent）。",
        ["多方案架构设计", "设计权衡与选型", "系统组成与接口设计", "SysML v2 建模代码生成"],
        ["设计", "架构", "方案", "建模", "建模、方案"],   # 泛词 + 含顿号的脏词
        ["架构设计", "方案选型", "系统架构"],             # 4 字独有词（各得 1.0 分）
    ),
    "impact": (
        "分析一处设计变更会**波及哪些模型元素**：沿依赖链正/反向 BFS 追溯，"
        "按层级输出受影响元素清单、传播路径、关键节点、风险等级与缓解建议。"
        "用户说「改了 X 会影响什么」「影响范围多大」「波及面」「这个改动安全吗」时用我。"
        "只做影响分析、不改模型；要改模型请交给建模 Agent。",
        ["变更影响范围分析", "依赖链 BFS 遍历", "影响深度分级", "风险节点标注"],
        ["变更", "影响", "change"],                       # 泛词
        ["变更影响分析", "影响范围分析", "受影响元素"],
    ),
    "review": (
        "对 SysML v2 模型做**预评审**：语法/逻辑/规范/合理性四类校验，"
        "输出可评审性评分（满分 100，按错误严重度扣分）、问题分类与可定位的修复建议，"
        "并支持导出预评审报告。用户说「校验这个模型」「预评审」「检查有没有问题」"
        "「打个分」时用我。不负责改模型（交给建模 Agent），不做正式评审签批。",
        ["模型规范性校验", "语法/逻辑/规范/合理性四类检查", "可评审性评分",
         "问题定位与修复建议", "预评审报告导出"],
        ["校验", "评审", "检查", "review"],               # 泛词
        ["模型校验", "语法校验", "一致性检查", "可评审性评分"],
    ),
    "report_generation": (
        "把各专家的分析结果**组装成规范工程报告并导出**：支持模型分析/变更影响/"
        "预评审/自定义四类，按章节模板组织并导出 md/docx/pdf。"
        "用户说「生成一份报告」「导出报告」「整理成文档」「写份评审报告」时用我。"
        "只做组装与排版，不产出新的分析结论（那要先由对应专家 Agent 分析）。",
        ["多类型工程报告组装", "章节模板编排", "md/docx/pdf 导出", "分析结果整合"],
        ["报告", "文档", "report"],                       # 泛词
        ["报告生成", "文档导出", "导出报告"],
    ),
    # ⚠️ 需求域有 **3 个 Agent**（analysis / quality / structuring），实测关键词已互撞
    #   （`需求条目`/`条目化需求` 被 requirement_structuring 占用）。
    #   本轮不合并（合并是架构决策），只**划清边界**：
    #     · analysis  = 澄清 + 利益相关方 + 规格化（面向"从模糊诉求到可验收需求"）
    #     · structuring = 批量条目化 + 分类 + 追溯（面向"已有一堆文字，拆成条目"）
    #     · quality   = 质量评审（面向"已有需求，评它写得好不好"）
    "requirement_analysis": (
        "把模糊诉求转成**可验收的需求规格**：先澄清信息缺口、识别利益相关方与关注点，"
        "再拆功能/非功能需求，每条带唯一 ID、MoSCoW 优先级、来源追溯与 "
        "Given-When-Then 验收标准。用户说「需求不清楚，帮我理一下」「这需求怎么定」"
        "「写需求规格」「验收标准怎么定」时用我。不做批量条目化拆解（交给需求结构化 Agent），"
        "不做需求质量评分（交给需求质量 Agent），不做系统设计。",
        ["自然语言需求解析", "需求澄清与歧义消解", "利益相关方分析",
         "需求规格化（ID/MoSCoW/验收标准）", "来源追溯标注"],
        ["需求", "解析"],                                 # 泛词
        ["需求澄清", "需求规格", "利益相关方"],
    ),
    "knowledge_qa": (
        "基于知识库图谱与向量的**领域知识问答**：解释 MBSE/SysML v2 概念、"
        "给操作指引、做问题诊断与规范条款解读，答案标注来源。"
        "用户说「什么是 X」「X 和 Y 有什么区别」「怎么创建某类视图」「规范怎么写的」时用我。"
        "只解释不执行操作——要改模型、跑校验、出报告请交给对应执行 Agent。",
        ["MBSE/SysML v2 概念解释", "操作指引", "问题诊断", "规范条款解读", "来源可溯答题"],
        ["知识库", "资料", "方法论"],                     # 泛词
        ["概念解释", "知识问答", "规范条款解读"],
    ),
    # chat 是**兜底入口**：它的 description 要让路由知道"没人认领时才归我"，
    # 否则它会与专门 Agent 抢（实测大量请求落到 chat 就是这个原因之一：
    # 它的关键词里塞了「平台操作、功能导航、无法归类」这种"什么都能算"的说法）。
    # ⚠️ 补词为 0：兜底 Agent **不该有专属关键词** —— 一旦它有词能命中，
    #    就会从专门 Agent 手里抢走本应精确路由的请求。它靠"无人认领"接活。
    "chat": (
        "**通用会话兜底入口**：承接未被其它专门 Agent 认领的请求——闲聊、平台功能怎么用、"
        "跨域综合问答、无法归类的问题，直接作答不打断用户。"
        "只在这些情形用我：话题不属于建模/需求/评审/报告/变更任一域，或用户在问平台本身怎么操作。"
        "不替代专门 Agent：能明确归到上述任一域的，请路由过去。",
        ["通用问答兜底", "平台功能与操作指引", "跨域综合问答"],
        ["平台操作、功能导航、无法归类"],                  # 含顿号的脏词（整条无效）
        [],
    ),
}


def main() -> int:
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        from agent.intent import IntentRouter
        gen = set(IntentRouter._GENERIC_KW)
    except Exception:
        gen = set()

    changed = 0
    for name, (desc, caps, drop_kw, add_kw) in SPEC.items():
        row = conn.execute("SELECT id, display_name, description, capabilities, "
                           "intent_keywords FROM agents WHERE name=?", (name,)).fetchone()
        if not row:
            print("⚠️ 找不到 Agent：%s（跳过）" % name)
            continue
        try:
            kws = json.loads(row["intent_keywords"] or "[]")
        except Exception:
            kws = []
        # 只删：① 显式列出的 ② 在泛词表里的（双重保险，不误删独有词）
        kept = [k for k in kws if k not in drop_kw and k not in gen]
        dropped = [k for k in kws if k not in kept]
        # 补词前查重：同词被多个 Agent 用会让路由 fused_conflict（实测 `需求条目` 已撞）
        added, skipped = [], []
        for k in (add_kw or []):
            if k in kept:
                continue
            if k in gen:
                skipped.append(k + "(泛词)")
                continue
            owner = conn.execute(
                "SELECT name FROM agents WHERE status='active' AND name!=? "
                "AND intent_keywords LIKE ?", (name, '%"' + k + '"%')).fetchone()
            if owner:
                skipped.append("%s(已属 %s)" % (k, owner["name"]))
                continue
            kept.append(k)
            added.append(k)

        print("\n■ %s（%s）" % (name, row["display_name"]))
        print("  desc %d → %d 字符" % (len(row["description"] or ""), len(desc)))
        print("  caps %s → %d 项" % (
            row["capabilities"] or "[]", len(caps)))
        print("  kw   %d → %d（删：%s；补：%s%s）" % (
            len(kws), len(kept), "、".join(dropped) or "无",
            "、".join(added) or "无",
            ("；跳过：" + "、".join(skipped)) if skipped else ""))
        if not dry:
            conn.execute(
                "UPDATE agents SET description=?, capabilities=?, intent_keywords=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (desc, json.dumps(caps, ensure_ascii=False),
                 json.dumps(kept, ensure_ascii=False), row["id"]))
            changed += 1

    if not dry:
        conn.commit()
        print("\n已提交 %d 个 Agent" % changed)
    else:
        print("\n（dry-run，未写库；加 --apply 执行）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
