# -*- coding: utf-8 -*-
"""子任务「必含规则」单一真源（★ 2026-11-15 P0-6）。

为什么要有这一份
----------------
方案 P0-6 要求「把必含规则显式拼进子任务上下文」。落地前先查了三条既有链路：
  · `workflows/planner.py::build_subtask_context` 的「交付规范」只有一句软约束；
  · `agent/pipeline_parts/stream.py` 有约 40 行「交付格式」契约（**已注入**子任务 query）；
  · `docs/评测/datasets/golden_e2e_v0.json` 12 例各带 `inviolable_rules`，
    但那是**评测层事后判据**，生产链路零 import、评分器也不读取它。
⇒ 三者互相不知道对方存在。再写第四份规则只会多一份漂移源。

本模块的做法：**不重复注入交付契约**，只把 Golden Set 里跨 12 例反复出现的
「通用红线」抽成一份常量，由 `build_subtask_context` 以开关方式拼进快照；
并在 `tools/verify/verify_p0_6_failure_attribution.py` 里断言它与 Golden Set
12 例逐例对齐。数据集本身**不改**（改了会动 P0-1 的 dataset_id 指纹）。

⚠️ 三条纪律（都是本仓踩过的形状）：
  1. 规则文本**不得含三个反引号围栏** —— `verify_subtask_context.py` A3c 与
     `verify_subtask_isolation.py` F1b 都断言快照里围栏数为偶数。
  2. 不写 `kind` 枚举 —— 仓里 `subtask_protocol.KIND_VALUES` / `stream.py`
     契约 / `output_contracts.ARTIFACT_KINDS` 是三套不一致的枚举，写进来会与
     同一 query 里的契约直接打架。
  3. 规则是**纯文本原则句**，不是 schema / L0 语法卡（后者约 1.3K token，
     且只对建模类子任务有意义）。
"""

RULES_VERSION = "2026-11-15.1"

#: 顺序即编号（1-based），`GOLDEN_ALIGNMENT` 引用这里的编号。
INVIOLABLE_RULES = (
    "交付物必须自包含：只依据本快照与上游交付物，禁止虚构上下文外的事实、编号或数据",
    "不得伪造校验通过：涉及 SysML v2 校验必须真实调用校验工具并如实回报结论与诊断",
    "上游交付物须显式引用：摘要不足以判断时取全文，不得凭摘要猜测上游结论",
    "未完成 / 失败 / 有缺口必须如实标注，不得把有缺口的结果报成完整或已通过",
    "子任务是独立上下文：看不到父会话历史，不要假设能看到，也不要向用户索要历史",
)

#: 与 Golden Set v0 的 12 例逐例对齐（**单向登记**：数据集不改，只在这里声明覆盖关系）。
#: 常驻门禁断言：key 集合 == 数据集 case_id 集合；每条规则至少被 1 例引用；
#: 每例至少引用 1 条规则。删规则 / 增例 / 改 case_id 都会立刻判红。
GOLDEN_ALIGNMENT = {
    "G01": (1, 2),
    "G02": (1, 2),
    "G03": (1, 2),
    "G04": (1, 2),
    "G05": (4, 5),
    "G06": (3, 4),
    "G07": (1,),
    "G08": (4,),
    "G09": (2,),
    "G10": (1, 2),
    "G11": (2, 4),
    "G12": (1, 5),
}


def render_rules() -> str:
    """渲染成 `build_subtask_context` 可直接 append 的一段（零围栏、纯文本）。"""
    lines = ["- 必含规则（P0-6 / %s；违反即视为交付失败）：" % RULES_VERSION]
    lines.extend("  · %d) %s" % (i, r) for i, r in enumerate(INVIOLABLE_RULES, 1))
    return "\n".join(lines)


def audit_alignment(case_ids) -> list:
    """返回对齐问题清单（空列表 = 自洽）。供常驻门禁直接调用，避免两份实现漂移。"""
    problems = []
    declared = set(GOLDEN_ALIGNMENT)
    actual = set(case_ids)
    if declared != actual:
        problems.append("case_id 集合不一致：仅本模块=%s / 仅数据集=%s"
                        % (sorted(declared - actual), sorted(actual - declared)))
    for cid, refs in GOLDEN_ALIGNMENT.items():
        if not refs:
            problems.append("%s 没有引用任何必含规则" % cid)
        for n in refs:
            if not (1 <= n <= len(INVIOLABLE_RULES)):
                problems.append("%s 引用了不存在的规则编号 %s" % (cid, n))
    used = {n for refs in GOLDEN_ALIGNMENT.values() for n in refs}
    for i in range(1, len(INVIOLABLE_RULES) + 1):
        if i not in used:
            problems.append("规则 %d 没有任何 Golden Set 用例覆盖（可能是死规则）" % i)
    block = render_rules()
    if "```" in block:
        problems.append("规则块含围栏，会破坏快照围栏奇偶门禁")
    if len(INVIOLABLE_RULES) != 5 or RULES_VERSION != "2026-11-15.1":
        problems.append("规则数量或版本与登记值不符（改动请同步 GOLDEN_ALIGNMENT 与门禁）")
    return problems
