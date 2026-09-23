# -*- coding: utf-8 -*-
"""本体一致性检查「规则目录」——code → 中文标签 / 严重度 / 维度 / 为什么 / 怎么修。

**单一真源**：本文件只依赖标准库，被
  · 后端唯一产出方 `routers/knowledge_parts/shared.py::_ontology_check`（issue 带 code+label），
  · 前端（经接口 `label` 字段渲染，不再自持清单），
  · 常驻验证脚本 `tools/verify/verify_ontology_dom_range.py`（目录 ↔ 实现双向一致）
共同消费。沿用 `core/branch_rules.py` 的成功范式（标准库、零循环导入）。

为什么单独成文件（2026-09-23 实测）：
  此前「6 种 issue type 的中文标签」只存在于前端 `static/js/mods/09-impact.js` 的 `TYPE_LABEL`，
  后端产 `bad_dom_range` 却不给中文 → 前端**漏映射一种**就静默回退渲染英文
  （真库当时 9 条高危**全部显示英文** `bad_dom_range`）。与 P0-2 那次"两处只读判定副本"
  是**同型问题**：同一份语义清单被两个文件各持一份，必然漂移。→ 一处声明、两处消费。

severity 语义（对齐 SHACL 三档 + SAIC「语言类 vs 风格类」分层）：
  · high  = 硬错（阻断发布：本体自相矛盾/结构破损）
  · low   = 风格/待补项（**刻意不阻断发布**，否则误锁）
  · warn  = 规则与数据不一致（命中面广，先观察）
"""

# 目录版本：新增/改档位时递增（报告里回显，便于"体检结论可复现"）
RULE_VERSION = "2026-09-23.1"

# code → 规则元数据
RULES = {
    "cycle": {
        "label": "循环继承",
        "severity": "high",
        "dimension": "structure",
        "why": "类型继承链回到自身，类层级无解（子类既是父类又是后代）",
        "fix": "在本体页断开该类型的 parent，使其继承链收敛到根类型",
    },
    "dangling_parent": {
        "label": "悬空父类",
        "severity": "high",
        "dimension": "structure",
        "why": "parent_id 指向的类型已不存在（删除/改名后未同步子类）",
        "fix": "把该类型的父类改为现存类型，或清空父类使其成为根类型",
    },
    "isolated": {
        "label": "孤立类",
        "severity": "low",
        "dimension": "structure",
        "why": "无实例且无子类——已声明但从未被使用",
        "fix": "确为占位则保留；不再需要可删除该类型",
    },
    "missing_dom_range": {
        "label": "关系缺定义域/值域",
        "severity": "low",
        "dimension": "structure",
        "why": "对象属性未声明允许的来源/目标类型 → 该关系对两端类型无约束",
        "fix": "在该关系类型的编辑页补齐「定义域(src) / 值域(tgt)」（可多选）",
    },
    "bad_dom_range": {
        "label": "定义域/值域指向不存在的类型",
        "severity": "high",
        "dimension": "consistency",
        "why": "src/tgt 里写了未注册的实体类型 → 该校验器对存量边判非法，新边也写不进",
        "fix": "把悬空名替换为已注册的实体类型（或先注册这些类型）",
    },
    "duplicate": {
        "label": "重名类型",
        "severity": "high",
        "dimension": "structure",
        "why": "同 type_kind 下同名类型出现多次 → 按名索引时行为不确定",
        "fix": "合并或重命名重复类型",
    },
}

# 允许的档位（对齐 SHACL severity / ingest_gate 的 off-warn-enforce 思路）
VALID_SEVERITIES = ("high", "warn", "low")


def rule(code: str) -> dict:
    """取规则元数据；未知 code 回退为「未登记规则」（显式暴露，不静默）."""
    r = RULES.get(code)
    if r:
        return dict(r, code=code)
    return {"code": code, "label": code, "severity": "warn", "dimension": "unknown",
            "why": "该问题码未登记进 core.ontology_rules.RULES", "fix": "补充规则目录"}


def label(code: str) -> str:
    """问题码 → 中文标签（前端渲染用；保证后端产出即可读）."""
    return rule(code)["label"]


def severity_of(code: str) -> str:
    """问题码 → 严重度（目录为唯一真源，避免"档位写死在分支里"）."""
    return rule(code)["severity"]


def all_rules() -> list:
    """全部规则（供前端/文档/验证脚本列举）."""
    return [rule(c) for c in RULES]
