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
  · info  = **设计意图登记**（2026-09-23 新增）——O1-2 里 `multi_domain_range` 命中 18/24 个类型，
    而它是本工程有意为之的用法（列表=允许的类型集合），不是缺陷。若不单独开一档，
    这 18 条会把 3 条真正待补的 low 淹没（档位即信息密度）。

目录 ↔ 实现的**双向一致**由 `tools/verify/verify_ontology_dom_range.py` 的 [1]/[6] 段断言：
每条目录项都能被触发（坏样本必挂）、每个产出的 code 都在目录里。
"""

# 目录版本：新增/改档位时递增（报告里回显，便于"体检结论可复现"）
RULE_VERSION = "2026-10-15.4"

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
    # ─────────────────────────────────────────────────────────────────────
    # 2026-09-23 晚（O1-2 判据补齐）：把"一次性核查"升级为"常驻规则"
    # ─────────────────────────────────────────────────────────────────────
    "bad_attr_domain": {
        "label": "属性适用类型指向不存在的类型",
        "severity": "high",
        "dimension": "consistency",
        "why": "数据属性的 domain_classes 里写了未注册的实体类型 → 适用面判定永远匹配不到该类型，"
               "校验器对这部分实例静默失去约束（与 bad_dom_range 是同一类「两段漂移」产物："
               "换域时只换了 entity/attribute，没同步改引用它们的地方）",
        "fix": "把悬空名替换为已注册的实体类型，或先把这些类型补进本体",
    },
    "attr_domain_missing": {
        "label": "属性缺适用类型",
        "severity": "low",
        "dimension": "structure",
        "why": "数据属性未声明 domain_classes → 语义上对**所有**实体类型生效，"
               "无法表达「这个属性只属于这几类」（此前 relation 有 missing_dom_range，attribute 完全无检查）",
        "fix": "在该属性类型的编辑页补齐「适用类型（domain_classes）」",
    },
    "multi_domain_range": {
        "label": "一侧声明多个类型（设计用法）",
        "severity": "info",
        "dimension": "design",
        "why": "一个 side 声明多个类型在 OWL 里是**交集**语义（OOPS! 判为 Critical），"
               "但本工程用列表承载「允许的类型集合」（并集）——**刻意只做提示**，"
               "避免把有意设计误报成缺陷（沿用「判缺陷前先找设计声明」纪律）",
        "fix": "无需修（这是登记的设计意图）；若要表达 OWL 交集，请改用 SHACL 导出侧约束",
    },
    "rule_data_conflict": {
        "label": "声明与存量边不一致",
        "severity": "warn",
        "dimension": "consistency",
        "why": "声明的定义域/值域与**存量边端点**矛盾 —— 规则与数据打架：要么规则写错，"
               "要么存量数据是换域残留（2026-09-23 那次 141 条边正是此形态，"
               "但它当时只活在一份一次性核查里，下次还会静默发生）",
        "fix": "二选一：改规则（按提示修该关系的定义域/值域），或修数据（走实例迁移工单，"
               "不静默改存量）",
    },
    "dangling_instance": {
        "label": "实例类型未注册",
        "severity": "warn",
        "dimension": "instance",
        "why": "存量的 entities.entity_type 不在本体实体类型集合内（改名/删除类型后的历史残留）"
               "→ 校验器无法对该类实例推理，图谱里也是「无主」节点",
        "fix": "把实例改挂到现存类型，或把该类型补回本体（先看影响预览，再走迁移工单）",
    },
    "instance_constraint_violation": {
        "label": "存量实例不满足约束",
        "severity": "warn",
        "dimension": "instance",
        "why": "存量实例的属性值/端点/基数不满足当前约束（收紧约束或改数据前未做迁移）"
               "—— 该能力此前只在「改那个类型时」被调用，全局无从回答",
        "fix": "用 GET /api/knowledge/ontology/validate?instances=1 复现明细；"
               "再按影响预览 → 迁移工单处理（不静默改存量）",
    },
    # ─────────────────────────────────────────────────────────────────────
    # 2026-10-15（P0-5）：`missing_dom_range` 对「归一别名型关系」是**误报**。
    #
    # 实测根因（真库只读 + 代码走读）：
    #   · `连接` 是 core.relmap.REL_CANONICAL 登记的**输入别名**，sysml_importer
    #     落库前一律 canonical() 归一为 `CONNECTS`（props.rel_normalized_from="连接"）
    #     → relations 表里 `连接` 恒为 0 条边；
    #   · `执行` 的 canonical() 返回 **None**（relmap 注释：语义≈REALIZES/USES 未定，
    #     废弃），导入器把该边**直接剔除并计入 rejected** → 同样恒 0 条边。
    # 于是原方案「按存量端点反推 dom/range」在数据上**无从执行**（没有存量端点），
    # 硬补一份 dom/range 反而会：① 与归一后的 CONNECTS 语义各写一份、必然漂移；
    # ② 让 sysml_importer 未来的连接边被 rule_data_conflict 判违规。
    #
    # 处置：登记为设计说明（info），**明确告知"该类型在数据层恒 0 边、约束由归一后的
    # canonical 类型承载"**，不再以待补项(low)反复提示——避免噪声掩盖真正待补的 low。
    "normalized_alias_relation": {
        "label": "归一别名型关系（无存量边，约束由归一目标承载）",
        "severity": "info",
        "dimension": "design",
        "why": "该关系名是 core.relmap 登记的**归一别名/已废弃名**：写库路径一律先过 "
               "canonical()，别名要么归一到 canonical 类型（如 连接→CONNECTS），"
               "要么因无映射被剔除（如 执行，语义未定已废弃）"
               "—— 因此它在 relations 表里恒为 0 条边，"
               "「无定义域/值域」不是漏填，而是约束本就由归一后的 canonical 类型承载",
        "fix": "无需修（这是登记的归一设计意图）。若该关系将来要直接承载数据，"
               "须先在 core.relmap.py 登记 canonical 映射并停用归一，"
               "再在此为其声明定义域/值域（届时本条会自动消失）",
    },
    # ─────────────────────────────────────────────────────────────────────
    # 2026-10-15（P1-5）：OOPS! 关键陷阱补码（适用项 4 条；不适用清单见
    # docs/本体模型管理模块-…-20261014.md §11.1 —— SWRL 无触发数据（本工程
    # 无 SWRL 存储，P2-4 判孤岛）、命名类歧义（中文 schema 场景）明确不做）。
    # 档位裁定依据真库预演：inverse_of 全库 0 声明 → missing_inverse 若开 low
    # 会命中全部 7 个有边关系（噪声淹没问题清单）→ 定 info；两条「声明与
    # 矛盾/数据打架」类有真命中（ALLOCATED_TO/TRACE）→ warn，与 rule_data_conflict 同族。
    # ─────────────────────────────────────────────────────────────────────
    "char_conflict": {
        "label": "推理性质互斥声明",
        "severity": "warn",
        "dimension": "consistency",
        "why": "同一关系同时声明了互斥的特性（symmetric+asymmetric / reflexive+asymmetric）——"
               "逻辑上不可能同时成立（asymmetric 蕴含非自反），标准 DL 推理器（Pellet 实测 "
               "2026-10-15）会直接判本体 inconsistent，推理与 SHACL 导出无法判定该信哪个"
               "（OOPS! 特性矛盾类陷阱）",
        "fix": "在关系编辑页按业务语义二选一：方向性关系保留 asymmetric 并摘除 symmetric/reflexive；"
               "真对称/自反关系则摘除 asymmetric",
    },
    "symmetric_not_paired": {
        "label": "声明对称但存量边非成对",
        "severity": "warn",
        "dimension": "consistency",
        "why": "声明了 symmetric，但存在 A→B 而无 B→A 的存量边——声明与数据打架："
               "要么漏录反向边，要么该关系本就不是对称的（与 rule_data_conflict 同族）",
        "fix": "二选一：补齐反向边（走导入/迁移留痕，不静默改数据），或摘除 symmetric 声明",
    },
    "unspecified_direction": {
        "label": "关系方向语义不可判",
        "severity": "low",
        "dimension": "structure",
        "why": "定义域与值域为同一类型集合、未声明 symmetric 且有存量边——src/tgt 语义上无法区分"
               "（OOPS! 方向未指明类陷阱；如 CONNECTS 两端都是『组件』时哪边是源不可判）",
        "fix": "若两端确实无方向差别，请声明 symmetric；否则收窄一侧的定义域/值域使方向可判",
    },
    "missing_inverse": {
        "label": "有边但无反向关系声明（按需补）",
        "severity": "info",
        "dimension": "design",
        "why": "该关系有存量边但未声明 inverse_of 也非 symmetric——反向导航"
               "（如 CONTAINS ↔ CONTAINED_BY）只能靠查询侧硬编码（OOPS! 缺反向关系陷阱）。"
               "登记为设计提示而非缺陷：并非所有关系都需要反向（纯单向语义如 VERIFIED_BY 可忽略）",
        "fix": "确需反向导航时，在关系编辑页声明 inverse_of 指向反向关系"
               "（推荐反向关系同时互指回来）；纯单向语义可忽略本条",
    },
    "swrl_orphan": {
        "label": "SWRL 规则引用未注册类型",
        "severity": "warn",
        "dimension": "consistency",
        "why": "启用的 SWRL 规则引用了本体中不存在的类/属性（P2-4 实测：4 条模板种子引用 "
               "Person/hasAge/Component 等 7 个不存在类型，永久空转且推不出任何事实——"
               "推理引擎对未知 atom 静默建空类，规则'看起来在跑'实际零产出）",
        "fix": "改写规则引用本体已注册的类型/关系，或删除该规则"
               "（管理入口：本体页顶栏「⚖ SWRL」面板；历史上引用不存在类型的规则已备份至 "
               "tools/_swrl_dead_rules_backup.json）",
    },
}

# 允许的档位（对齐 SHACL severity / ingest_gate 的 off-warn-enforce 思路）
VALID_SEVERITIES = ("high", "warn", "low", "info")

# 档位**展示名**与**排序**：同样是单一真源 —— 随报告回给前端，避免前端再自持一份档位清单
# （与 2026-09-23 修掉的 TYPE_LABEL 漂移是同一条纪律：同一份语义清单只许存在一处）。
SEVERITY_LABELS = {"high": "高危", "warn": "数据冲突", "low": "待补项", "info": "设计说明"}
SEVERITY_ORDER = {"high": 0, "warn": 1, "low": 2, "info": 3}

# 每条规则必须有的字段（audit_catalog 自检用）
REQUIRED_FIELDS = ("label", "severity", "dimension", "why", "fix")

# 维度（问题出在哪一层）：前端按此分组/过滤（O1-6），目录侧登记以免出现拼错的自由字符串
DIMENSIONS = ("structure", "consistency", "design", "instance")


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


def severity_label(sev: str) -> str:
    """档位 → 中文展示名（前端渲染用；单一真源，避免前端自持清单）."""
    return SEVERITY_LABELS.get(sev, sev)


def severity_rank(sev: str) -> int:
    """档位 → 排序权重（越小越靠前）。未知档位排最后，不静默当 high."""
    return SEVERITY_ORDER.get(sev, 9)


def severity_meta() -> list:
    """档位元数据（随报告下发，前端据此渲染 + 排序）."""
    return [{"key": k, "label": SEVERITY_LABELS[k], "rank": SEVERITY_ORDER[k]}
            for k in sorted(VALID_SEVERITIES, key=lambda x: SEVERITY_ORDER[x])]


def audit_catalog() -> list:
    """目录自检：返回问题清单（空 = 目录自洽）。

    校验三件事：① 必备字段齐全且非空；② severity 在合法档位内；③ code 与 key 一致。
    被 `tools/verify/verify_ontology_dom_range.py` 直接断言 —— 目录写错时**当场报错**，
    而不是等到某次体检渲染出空标签。
    """
    bad = []
    for code, r in RULES.items():
        for f in REQUIRED_FIELDS:
            if not r.get(f):
                bad.append(f"{code}: 缺字段 {f}")
        if r.get("severity") not in VALID_SEVERITIES:
            bad.append(f"{code}: 非法 severity {r.get('severity')!r}")
        if r.get("dimension") not in DIMENSIONS:
            bad.append(f"{code}: 未登记 dimension {r.get('dimension')!r}")
    return bad
