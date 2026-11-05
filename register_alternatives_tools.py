# -*- coding: utf-8 -*-
"""多方案设计：平台资产注册（幂等，可重复执行）。

注册五件事（全部落在 DB，**本脚本不含任何判断逻辑**）：
  1. `tools` 表       —— `alternatives_score`（多方案打分，source='local'，kind='internal'）
  2. `skills` 表     —— 「多方案设计与对比」技能（**编排规则**：何时用 / 输出契约 / 纪律）
  3. `agent_tools` 表 —— 绑定到方案类 Agent（显式名单 ∪ 规则发现）
  4. `generate_rules` 表 —— 维度清单 / 权重 / 阈值 / 别名表（**策略配置，可在前端改**）
  5. `agents` 表     —— 「多方案设计」Agent（内置能力载体，用户可直接选用）

────────────────────────────────────────────────────────────────────────────
为什么把规则放 DB，而不是写进 Python
────────────────────────────────────────────────────────────────────────────
「哪些维度参与比较、各占多少权重、什么时候该出多方案、输出什么结构」属**策略**，
会随工程实践调整。写在 pipeline 里意味着每次调整都要改代码 + 重启；
写在 DB 里则**改配置即生效**，且能被前端「技能/工具/生成规则」页面管理。

本脚本只负责"把定义灌进去"。真正的算子在 `core/alternatives.py`，
执行在 `alternatives_tools.py`，两者都与本脚本解耦。

⚠️ 本脚本**不声明** skill 的 `allowed_tools`：工程里 skill 的 allowed_tools 是
   **独占式白名单**（命中后会把该回合其它工具全部禁掉）—— 工具能否被 LLM 看到
   由 `agent_tools` 绑定 + JIT 保底决定，**不靠 skill**。
   （同 `register_sysml_check_tools.py` 的刻意选择，别"补全"它。）

用法：<repo>\\.venv\\Scripts\\python.exe -X utf8 register_alternatives_tools.py
"""
import json
import sqlite3

from core.config import DB_PATH

# ═══════════════════════════ ① 工具定义 ═══════════════════════════
# 描述会随工具定义**每次**注入 LLM 的 tools 载荷，是最高优先级的常驻提示 ⇒
# 「多方案比较场景必须调用本工具」这句话写在这里，比写在任何提示词里都可靠。
TOOL_NAME = "alternatives_score"

TOOL_DESC = (
    "给多个候选方案做**多维度归一化打分与比较**，返回可直接渲染的对比矩阵。"
    "【必用场景】任何需要比较 2 个以上方案（架构选型 / 备选方案对比 / 方案权衡）的回合，"
    "**必须**调用本工具，不要自行给出「综合得分」——分数只能由本工具算。"
    "【输入要求】alternatives 传候选方案数组，每项含 name 与 scores；"
    "scores 是各维度的**原始量**（如时延 120ms、成本 80 万元、覆盖需求 18 条），"
    "**不是** 0~100 的主观评分。"
    "⚠️【**绝对不要自己做方向变换**】每个维度的方向（越大越好 / 越小越好）由**平台的维度配置**"
    "决定，工具会自动按配置归一化。你**只管把原始量原样填进去**。"
    "实测反例（真实 E2E 抓出）：模型曾自行把「时延 120ms」换算成「时延余量 200−120=80」再传入，"
    "导致同一批方案的推荐结论**从方案A翻转成方案B** —— 数值被改写后，比较的已不是原指标。"
    "❌ 不要做：正向化/反向化、余量化、归一化、加权求和、折算成 0~100 分。"
    "✅ 只要填「时延 120 毫秒」「成本 80 万元」这种**未经加工的原始量**。"
    "无法量化的维度**省略该键**（不要编造，也不要填 0）；"
    "强烈建议同时给 rationale 说明每个维度的取值依据，供人核对。"
    "【结果读法】recommended 是推荐方案；tie=true 表示多个方案并列接近、需人工选型；"
    "coverage<1 表示该方案有维度未测量；ineligible 列出的方案因测量维度不足**不参与推荐**。"
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "alternatives": {
            "type": "array",
            "description": "候选方案数组（至少 2 个才有比较意义）。每项："
                           "{name, summary?, scores:{维度key:原始量}, rationale?:{维度key:依据}}",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "方案名"},
                    "summary": {"type": "string", "description": "一句话概述（可选）"},
                    "scores": {"type": "object",
                               "description": "各维度原始量（键=维度key，值为数值或带单位数值）"},
                    "rationale": {"type": "object",
                                  "description": "各维度取值依据（键=维度key）"},
                },
                "required": ["name", "scores"],
            },
        },
        "weights": {"type": "object",
                    "description": "可选：维度权重覆盖（不传=用平台配置）"},
        "tie_epsilon": {"type": "number",
                         "description": "可选：并列判定阈值（最高分与次高分差小于它即并列）"},
        "min_coverage": {"type": "number",
                         "description": "可选：推荐门槛，覆盖率低于它的方案只展示不参与推荐"},
    },
    "required": ["alternatives"],
}

# ═══════════════════════════ ② 编排规则（技能正文） ═══════════════════════════
SKILL_NAME = "多方案设计与对比"

SKILL_MD = """---
name: 多方案设计与对比
description: 产出多个候选方案并用 alternatives_score 做多维度打分比较；分数只由工具算，不自报
category: 方案设计
version: v1.0
---
# 多方案设计与对比

你负责在需要**比较方案**时，产出结构化候选并给出**可复算**的比较结论。

## 何时走本流程

- 用户明确要求**多个方案 / 方案对比 / 备选方案 / 架构选型** ⇒ **必须**走。
- 单一方案的需求 ⇒ **不要**硬造多方案（那是噪音）。
- 用户问的是"这个方案好不好"（单方案评价）⇒ 走评价，不必走对比。

## 上下文不足时怎么办（实测 E2E 抓出的卡点）

真实 E2E 第一轮实测：工程基线为空时，模型**用 `ask_user` 反问**"要比较哪三个方案"，
于是流程**停在澄清**、打分工具根本没被调用 —— 整轮白跑。

⇒ **判据：用户已经在消息里给出了候选方案 / 指标 / 约束 ⇒ 直接做，不要反问。**
- 「给出三个方案并比较」+ 用户已列指标 ⇒ **直接产出候选并调工具**；
- 用户**只说**「比较一下」但没给任何方案或指标 ⇒ 这时**才**澄清，且**一次问清**
  （要比较哪几个方案 / 关注哪些维度），不要一问一答挤牙膏；
- 若澄清仍拿不到指标 ⇒ 按领域常识**做合理假设并显式标注**（「时延按卫星测控典型值假设 200ms 门限」），
  **不要无限反问**。平台的价值是给出可比较的结论，不是把问题推回给用户。

## 流程（顺序不可颠倒）

1. **定维度**：先看工具返回的 `dimensions` 字段（平台配置给出）——那是本次比较的维度集。
   **不要自己另发明一套维度名**；维度 key 用 `dimensions` 里给的。
2. **产候选**：产出 2~`max_count` 个**有实质取舍差异**的方案。
   - 差异必须是**设计路线上的**（如集中式 vs 分布式），不是措辞变体。
   - 每个方案给 `name`、一句话 `summary`。
3. **填原始量**：每个方案给 `scores` —— 各维度的**原始量**。
   - 例如：性能 → 时延 120ms / 吞吐 8000 TPS；成本 → 研制成本 80 万元；
     风险 → 3 项未闭环风险；需求覆盖 → 覆盖 18 条需求。
   - **原始量**，不是 0~100 主观分。系统会做归一化，**不要自己加权**。
   - 无法量化 ⇒ **省略该键**。省略 = "未测量"，系统会据此降低该方案的覆盖率。
     **禁止填 0**（0 会被当作"这项表现最差"，是把"没测"说成"差"）。
   - ⚠️ **绝对不要自己做方向变换**（真实 E2E 抓出的实测反例）：
     每个维度的「越大越好 / 越小越好」由**平台维度配置**决定，**工具会自动按配置归一化**。
     你**只管把原始量原样填进去**。
     ❌ 禁止：正向化/反向化（如「时延余量 = 200 − 时延」）、余量化、归一化、加权求和、折算成 0~100 分。
     ✅ 只要「时延 120 毫秒」「成本 80 万元」这种**未经加工的原始量**。
     实测后果：模型一旦自行做了余量化，同一批方案的推荐结论**从方案A翻转成方案B**
     —— 数值被改写后，比较的已不是原指标，结论直接失真。
4. **给依据**：每个填了的维度尽量给 `rationale`（如"时延 120ms，实测吞吐 8000 TPS"）。
   依据让人能核对原始量，是本流程的信任基础。
5. **调工具**：调用 `alternatives_score`，`alternatives` 传上述数组。
   **分数只能来自这个工具** —— 本回合没调用它，就不得说任何"综合得分""最优方案"。
   ⚠️ 若工具返回 `ok=false`，**读它的错误说明并按它说的改**（它会明确指出缺什么），
   **不要**绕过它自己算分。
6. **写结论**：依据工具返回写结论，三种情形分别照实说：
   - `recommended` 有值且 `tie=false` ⇒ 说"推荐 X"，并说明它在哪些维度占优、
     代价是什么（**推荐不等于没有代价**）。
   - `tie=true` ⇒ 说"X 与 Y 评分接近"，**不要**替人选，列出各自的取舍点，请用户定。
   - `recommended` 为空 / `ineligible` 非空 ⇒ 说"测量数据不足，暂不推荐"，
     并指出缺哪些维度。**不要**在数据不足时硬推一个。

## 硬性纪律

- **不自报分数**：没有 `alternatives_score` 的调用结果，就不得给出任何综合分或"最优"。
- **不编造原始量**：无法量化的维度省略，不要估算一个像模像样的数。
  **一个诚实的"未测量"比一个编造的 85 分有用。**
- **不用等级词当数值**："性能高""成本低"不是原始量，系统不会接受。
- **不隐藏代价**：推荐方案也要说清它的代价（哪个维度最差、牺牲了什么）。
- **区分并列与推荐**：并列（tie）时**必须**说明"评分接近、需人工选型"，
  不要挑一个当"最优"。
- **不越界**：本流程只做**比较**。选定某个方案后要落库/建模，是**另一件事**，
  不要顺手做掉（入库要走平台的写前确认流程）。

## 与其它能力的关系

- 本流程只产出**比较结论**；需要把选定方案落成 SysML 模型时，走建模流程
  （生成代码后按建模技能的要求自校验）。
- 本流程的维度集、权重、阈值来自**平台配置**（`alternatives_dimensions` /
  `alternatives_weights` 等生成规则），用户可在设置页调整；你**不要**硬记维度或权重。
"""

SKILL_TRIGGERS = [
    "多方案", "方案对比", "方案比较", "候选方案", "备选方案", "架构选型", "方案选型",
    "多套方案", "几个方案", "不同方案", "方案权衡", "对比方案", "选哪个方案",
    "alternative", "tradeoff", "权衡",
]

SKILL_DESC = ("产出多个候选方案并用 alternatives_score 做多维度归一化打分与比较；"
               "分数只由工具算，模型不自报；并列与数据不足都如实呈现。")

# ═══════════════════════════ ③ 规则配置（策略，落在 generate_rules） ═══════════════════════════
# ⚠️ 这些值是**默认值**，运营可在「AI 设计工坊 → 生成规则」里改（/api/studio/rules）。
#    改完即生效 —— **不需要改代码**。
RULE_ROWS = [
    ("alternatives_dimensions",
     json.dumps([
         {"key": "performance", "label": "性能", "direction": "higher", "unit": "分/指标值",
          "desc": "吞吐量、时延、容量等性能表现（原始值越大越好）"},
         {"key": "cost", "label": "成本", "direction": "lower", "unit": "万元/指标值",
          "desc": "研制成本、集成成本、运维成本（原始值越小越好）"},
         {"key": "risk", "label": "风险", "direction": "lower", "unit": "条/等级分",
          "desc": "技术风险、进度风险、依赖风险（原始值越小越好）"},
         {"key": "extensibility", "label": "可扩展性", "direction": "higher", "unit": "分/指标值",
          "desc": "接口开放度、模块解耦度、后续演进空间（原始值越大越好）"},
         {"key": "requirement_coverage", "label": "需求覆盖", "direction": "higher", "unit": "条/比率",
          "desc": "覆盖的需求条目数或覆盖比率（原始值越大越好）"},
     ], ensure_ascii=False),
     "多方案比较的维度清单（key/label/direction/unit 的 JSON 数组；direction=higher 越大越好，lower 越小越好）"),
    ("alternatives_weights",
     json.dumps({"performance": 0.25, "cost": 0.20, "risk": 0.20,
                 "extensibility": 0.15, "requirement_coverage": 0.20}, ensure_ascii=False),
     "多方案各维度权重（权重为 0 表示该维度不计入，coverage 分母随之排除该维度）"),
    ("alternatives_tie_epsilon", "0.02",
     "并列判定阈值：最高分与次高分差小于该值判并列推荐（不擅自宣称最优）"),
    ("alternatives_min_coverage", "0.6",
     "推荐门槛：覆盖率低于该值的方案只展示不参与推荐（防「少测多赢」）"),
    ("alternatives_max_count", "3",
     "多方案产出上限（提示词约束 + 工具建议的候选数量）"),
    ("alternatives_dimension_aliases",
     json.dumps({
         "性能": "performance", "performance": "performance", "perf": "performance",
         "时延": "performance", "延迟": "performance", "latency": "performance",
         "吞吐": "performance", "吞吐量": "performance", "throughput": "performance",
         "容量": "performance", "效率": "performance", "efficiency": "performance",
         "成本": "cost", "cost": "cost", "费用": "cost", "造价": "cost",
         "研制成本": "cost", "集成成本": "cost", "运维成本": "cost", "预算": "cost",
         "风险": "risk", "risk": "risk", "技术风险": "risk", "进度风险": "risk",
         "依赖风险": "risk", "风险等级": "risk",
         "可扩展性": "extensibility", "扩展性": "extensibility",
         "extensibility": "extensibility", "scalability": "extensibility",
         "可维护性": "extensibility", "演进空间": "extensibility",
         "模块化": "extensibility", "解耦": "extensibility",
         "需求覆盖": "requirement_coverage", "需求覆盖率": "requirement_coverage",
         "requirement_coverage": "requirement_coverage", "coverage": "requirement_coverage",
         "需求满足": "requirement_coverage", "覆盖度": "requirement_coverage",
     }, ensure_ascii=False),
     "维度别名表（模型写的维度名 → 规范维度 key 的映射；未列出的维度名会被丢弃）"),
]

# ═══════════════════════════ ④ Agent 绑定 ═══════════════════════════
# 绑定 = 该 Agent 的工具候选里出现本工具（`tools.py:_build_tools_def` 从 agent_tools 联表注入）。
#
# ⚠️ 2026-11-03（对齐 2026-09-20 修的同类缺口）：**不写死名单**。
#   规则发现让「新建的方案类 Agent」自动获得能力 —— 用户自建 Agent 不必改本文件。
#   显式名单覆盖 prompt 无判别词的职责（如 design 的 prompt 未含"多方案"字样）。
BINDINGS = {
    "design": [TOOL_NAME],   # 内置方案设计 Agent
    "多方案设计": [TOOL_NAME],  # 本脚本新建的 Agent（主 Agent，可单独选用）
    # ★ 复用落点（2026-11-03）：让**原主 Agent**（如「MBSE建模总体负责人」）的团队成员
    #   也能用多方案比较能力 ⇒ 选原主 Agent 时，多方案是**该阶段的一个能力**，而不是必须换工作流。
    #   判据（职责贴合 + 已在名册内 + 不与既有语义冲突）：
    #     · `architecture_skeleton` = 产出 SysML v2 架构骨架 ⇒ "给出几个架构骨架并比较"天然贴合；
    #       它已在 160 的名册里 ⇒ **不新增名册成员** ⇒ 主 Agent 提示词里那张
    #       「你的团队（8 名阶段节点）」写死表**不会因此漂移**（既有漂移是别的原因，见文档）。
    #     · ⚠️ **不绑 `多方案生成`(173)**：它的提示词明写「**不计算任何 MoE 数值**」，
    #       给它绑打分工具是**语义冲突**。
    "architecture_skeleton": [TOOL_NAME],
}
# 规则发现：active 且**职责**是产方案的 Agent。
#
# ⚠️ 第一版写成 `system_prompt LIKE '%方案%' OR description LIKE '%方案%'` ——
#    实测**过宽**：`requirement_analysis`（"提取条目化需求"）、`review`（"模型校验与评分"）
#    都因描述里偶然出现「方案」二字被绑上 ⇒ 给**无关 Agent 注入工具面、稀释工具集**
#    （本仓既有纪律：路由/问答类不该被无关工具污染）。
#
# 收紧后的判据 = **职责是"产出/比较方案"**，用强特征词：
#   ① 描述含「多方案」「候选方案」「方案对比」「架构选型」等**复合词**；
#   ② 或 Agent 名为「多方案设计」这类专名。
# 单字「方案」不再作为判据（它太常见）。
BINDING_RULE_SQL = (
    "SELECT id, name FROM agents WHERE status='active' AND ("
    " description LIKE '%多方案%' OR description LIKE '%候选方案%'"
    " OR description LIKE '%方案对比%' OR description LIKE '%架构选型%'"
    " OR description LIKE '%方案选型%' OR description LIKE '%备选方案%'"
    " OR name LIKE '%多方案%' OR name LIKE '%方案设计%'"
    ") AND name NOT LIKE '%多方案生成%'"
)
BINDING_EXCLUDE = {"chat", "knowledge_qa"}

# ═══════════════════════════ ⑤ 内置 Agent ═══════════════════════════
AGENT_NAME = "多方案设计"
AGENT_DESC = "产出多个候选架构方案并按多维度打分比较，给出推荐或并列结论（分数由 alternatives_score 工具计算）"
AGENT_PROMPT = (
    "你是网络总体MBSE设计助手，担任**多方案比较**专家。\n"
    "职责：针对同一需求/变更，产出多个有实质取舍差异的候选方案，按多维度（性能/成本/风险/"
    "可扩展性/需求覆盖等，实际维度以工具返回的 dimensions 为准）打分比较，给出推荐或并列结论。\n"
    "行为准则：分数**只能**来自 alternatives_score 工具的计算结果，本 Agent **不得自报综合分**；"
    "无法量化的维度省略该键而非填 0；并列时如实说明需人工选型；数据不足时如实说明暂不推荐；"
    "推荐方案必须同时说明其代价。\n"
    "协作：产出结论后若需把选定方案落成模型，交给建模流程（生成代码后按建模技能自校验），"
    "本 Agent 不擅自落库。"
)


def resolve_bindings(conn) -> dict:
    """显式名单 ∪ 规则发现（排除路由/问答类）→ {agent_name: [tool_name, ...]}。

    规则发现让「新加的方案类 Agent」自动获得多方案比较能力；显式名单覆盖
    prompt 无判别词的职责。两者都幂等。
    """
    out = {k: list(v) for k, v in BINDINGS.items()}
    try:
        for r in conn.execute(BINDING_RULE_SQL):
            name = r["name"]
            if name in BINDING_EXCLUDE:
                continue
            out.setdefault(name, [TOOL_NAME])
    except Exception:
        pass
    return out


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # ── ① 工具（幂等：先 INSERT OR IGNORE，再 UPDATE 刷新描述/schema）──
        ins = upd = 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO tools "
            "(name, source, description, status, input_schema, version, side_effect, risk_level, "
            " kind, scope, builtin) "
            "VALUES (?,?,?,?,?,?,?,?,'internal','public',0)",
            (TOOL_NAME, "local", TOOL_DESC, "active",
             json.dumps(TOOL_SCHEMA, ensure_ascii=False), "v1.0", "read", "low"))
        ins += cur.rowcount
        conn.execute(
            "UPDATE tools SET description=?, input_schema=?, status='active', "
            "side_effect='read', risk_level='low', version='v1.0', "
            "kind='internal', source='local', scope='public' WHERE name=?",
            (TOOL_DESC, json.dumps(TOOL_SCHEMA, ensure_ascii=False), TOOL_NAME))
        upd += 1
        conn.commit()
        print(f"[register] tools：新增 {ins} 条，刷新 {upd} 条（{TOOL_NAME}）")

        # ── ② 生成规则（策略配置；INSERT OR IGNORE + UPDATE 刷新）──
        r_ins = r_upd = 0
        for key, value, desc in RULE_ROWS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO generate_rules (rule_key, rule_value, description) "
                "VALUES (?,?,?)", (key, value, desc))
            r_ins += cur.rowcount
            conn.execute("UPDATE generate_rules SET rule_value=?, description=? WHERE rule_key=?",
                         (value, desc, key))
            r_upd += 1
        conn.commit()
        print(f"[register] generate_rules：新增 {r_ins} 条，刷新 {r_upd} 条"
              f"（{', '.join(k for k, _, _ in RULE_ROWS)}）")

        # ── ③ 内置 Agent（幂等 upsert）──
        # ⚠️ `agent_role='main'`（**主 Agent**）—— 不是 'sub'。
        #   判据（实测）：`team=` 入口只认主 Agent，`agent/pipeline_parts/stream.py:4320`
        #   与 `execute.py:120` 都会报「智能体团队不存在或未启用：{team}」。
        #   ⇒ 设为 'sub' 的话，用户**无法在会话页直接选它当工作流**，
        #      多方案能力就多了一道门槛（要靠自然语言命中才能触发）。
        #   本 Agent 是「多方案比较」这一**独立工作流**的承载者 ⇒ 必须是 main。
        row = conn.execute("SELECT id FROM agents WHERE name=?", (AGENT_NAME,)).fetchone()
        if row:
            conn.execute(
                "UPDATE agents SET description=?, system_prompt=?, status='active', "
                "hil_level='L1', kb_required=0, builtin=1, task_domain='modeling', "
                "agent_role='main' WHERE name=?",
                (AGENT_DESC, AGENT_PROMPT, AGENT_NAME))
            print(f"[register] agents：已更新「{AGENT_NAME}」(id={row['id']})")
        else:
            conn.execute(
                "INSERT INTO agents (name, display_name, description, system_prompt, status, "
                "hil_level, kb_required, builtin, agent_role, task_domain, intent_keywords, "
                "created_at, updated_at) "
                "VALUES (?,?,?,?, 'active','L1',0,1,'main','modeling',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (AGENT_NAME, AGENT_NAME, AGENT_DESC, AGENT_PROMPT,
                 json.dumps(["多方案", "方案对比", "架构选型"], ensure_ascii=False)))
            print(f"[register] agents：已创建「{AGENT_NAME}」(主 Agent)")
        conn.commit()

        # ── ④ 技能（幂等 upsert）──
        trg = json.dumps(SKILL_TRIGGERS, ensure_ascii=False)
        row = conn.execute("SELECT id FROM skills WHERE name=?", (SKILL_NAME,)).fetchone()
        if row:
            conn.execute(
                "UPDATE skills SET description=?, skill_type='package', triggers=?, category=?, "
                "content=?, frontmatter=?, status='published', version='v1.0', source='manual', "
                "enabled=1, scope='public' WHERE name=?",
                (SKILL_DESC, trg, "方案设计", SKILL_MD, SKILL_MD, SKILL_NAME))
            print(f"[register] skills：已更新「{SKILL_NAME}」(id={row['id']})")
        else:
            conn.execute(
                "INSERT INTO skills (name, description, skill_type, triggers, category, content, "
                "frontmatter, status, version, source, enabled, scope, builtin) "
                "VALUES (?,?,'package',?,?,?,?,'published','v1.0','manual',1,'public',0)",
                (SKILL_NAME, SKILL_DESC, trg, "方案设计", SKILL_MD, SKILL_MD))
            print(f"[register] skills：已创建「{SKILL_NAME}」")
        conn.commit()

        # ── ⑤ Agent 绑定（幂等）──
        # ⚠️ 先**解绑不合规**的历史绑定：规则改过（第一版过宽）后，旧绑定会留在库里
        #    ⇒ 必须按「本次解析出的应绑集合」做差集清理，否则「改配置即生效」是假的。
        #    ⚠️ 差集清理要覆盖 **tool 与 skill 两种 tool_type** ——
        #    第一版只清 tool ⇒ skill 绑定残留（实测 requirement_analysis/review 的
        #    skill 绑定仍在）。两类共用同一「应绑 Agent 集合」。
        _resolved = resolve_bindings(conn)
        _should = {str(n) for n in _resolved.keys()}
        _unbound = []
        for _tt, _tn in (("tool", TOOL_NAME), ("skill", SKILL_NAME)):
            _existing = [r["name"] for r in conn.execute(
                "SELECT a.name FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
                "WHERE t.tool_name=? AND t.tool_type=?", (_tn, _tt)).fetchall()]
            for _en in _existing:
                if _en not in _should:
                    conn.execute(
                        "DELETE FROM agent_tools WHERE tool_type=? AND tool_name=? "
                        "AND agent_id=(SELECT id FROM agents WHERE name=?)", (_tt, _tn, _en))
                    _unbound.append("%s:%s" % (_tt, _en))
        if _unbound:
            conn.commit()
            print(f"[register] agent_tools：解绑不再符合规则的历史绑定 {len(_unbound)} 条：{_unbound}")

        bound, skipped = 0, []
        print(f"[register] 待绑定 Agent {len(_resolved)} 个：{sorted(_resolved)}")
        for agent_name, tool_names in _resolved.items():
            arow = conn.execute("SELECT id FROM agents WHERE name=?", (agent_name,)).fetchone()
            if not arow:
                skipped.append(agent_name)
                continue
            for tn in tool_names:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?, 'tool', ?, 1)", (arow["id"], tn))
                bound += cur.rowcount
                conn.execute(
                    "UPDATE agent_tools SET enabled=1 "
                    "WHERE agent_id=? AND tool_type='tool' AND tool_name=?",
                    (arow["id"], tn))
            # 技能绑定（tool_type='skill'）—— 让 Agent 的技能面里出现本技能
            srow = conn.execute("SELECT id FROM skills WHERE name=?", (SKILL_NAME,)).fetchone()
            if srow:
                conn.execute(
                    "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?, 'skill', ?, 1)", (arow["id"], SKILL_NAME))
                conn.execute(
                    "UPDATE agent_tools SET enabled=1 "
                    "WHERE agent_id=? AND tool_type='skill' AND tool_name=?",
                    (arow["id"], SKILL_NAME))
        conn.commit()
        print(f"[register] agent_tools：新增绑定 {bound} 条"
              + (f"；⚠️ 跳过不存在的 Agent：{skipped}" if skipped else ""))

        # ── ⑥ 复核 ──
        tool_ok = conn.execute(
            "SELECT COUNT(*) FROM tools WHERE name=? AND status='active'", (TOOL_NAME,)).fetchone()[0]
        skill_ok = conn.execute(
            "SELECT COUNT(*) FROM skills WHERE name=? AND status='published'", (SKILL_NAME,)).fetchone()[0]
        rule_ok = conn.execute(
            "SELECT COUNT(*) FROM generate_rules WHERE rule_key LIKE 'alternatives_%'").fetchone()[0]
        bind_ok = conn.execute(
            "SELECT COUNT(*) FROM agent_tools WHERE tool_name=?", (TOOL_NAME,)).fetchone()[0]
        print(f"[register] 复核：工具 {tool_ok} / 技能 {skill_ok} / 规则 {rule_ok} / 绑定 {bind_ok}")
    finally:
        conn.close()
    print("[register] 完成")


if __name__ == "__main__":
    main()
