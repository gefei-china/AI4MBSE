"""migrate_v10_skill_bodies — 补齐 19 个 skill 的执行正文与资源（补空壳）。

问题（2026-10-08 实测）
------------------------
V9 建的 19 个 skill 只有 `description` 一句话，`content` / `frontmatter` /
`examples` / `scripts` **全空**，且 `references` 指向 7 个**磁盘上不存在**的路径
⇒ 运行时 `agent/pipeline_parts/skills.py:167-179` 装配给 LLM 的技能块里，
**「正文摘要」是空的、「参考文档」列的是不存在的文件**。
编辑页看起来简陋，是因为**库里本来就是空的**。

对照：平台内置 skill（文件操作/ 报告生成）的范式是
`content` = YAML frontmatter + Markdown 实操正文（600+ 字符），
`scripts` = ["scripts/main.py"]（真实存在的脚本）。

本脚本做什么
------------
① content：写**可执行的实操正文**（不是描述复述）：
   步骤化操作指令 + 判据 + 失败处理，且**与该 skill 的工具白名单一致**
② frontmatter：与 content 一致（库内既有做法是frontmatter 与 content 同文本）
③ references：**只登记磁盘上真实存在的文件**（先把内容写进
   static/skill_packages/<pkg>/references/，再登记路径）
④ examples：给每个 skill 一条真实可复用的示例
⑤ scripts：**不填**。脚本必须真能跑；本轮不写脚本就诚实留空，
   而不是像 V9 那样登记不存在的路径

不做的事：不编造脚本文件；不把不存在的路径登记进 references。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")
PKG_ROOT = os.path.join(ROOT, "static", "skill_packages", "mbse_modeling_skills")

# ═══════════════════════════════════════════════════════════════════
# 每个 skill 的正文（Markdown，含 frontmatter 头）
# 写法要求：① 步骤化可执行 ② 带判据 ③ 带失败处理
#        ④ 引用的工具必须在该 skill 的 allowed_tools 里
# ═══════════════════════════════════════════════════════════════════
BODIES = {
"sysml_methodology_profile_guide": (
"""## 目标
把「客户建模方法论」变成**可自动执行**的规约卡，而不是一段说明文字。

## 步骤
1. **先取规约卡**：调 `methodology_profile_load`，传 `stage`（M0/N1..N5）与 `project_id`。
   未绑定项目方法论时会返回 fallback 规约（OMG 通用），输出里会明示——此时**必须告诉用户**
   「当前按通用 SysML V2 处理，若有客户规范请补充」，不得默认按标准做法硬编。
2. **按阶段裁剪**：N1 只需要需求命名与需求必填；N2 才需要部件命名与层级约束。
   不要把整张卡塞进上下文（`stage` 参数就是为省token 设计的）。
3. **区分三类载体**（输出里的「载体层现状」段）：
   - 文档型 = 「应该怎么画」，**不可机器判定**，只能作为理解依据
   - 本体型 = 「能用什么」
   - 规则型 = 「必须怎样」，**只有带 enforce 分级的才能自动阻断**
4. **enforce 分级是能否自动执行的关键**：`lint` 提示 / `check` 阻断 / `forbid` 禁止。
   未分级的规则无法自动执行，遇到时要指出。

## 判据
- 规约卡的 `rule_count` > 0 ⇒ 可继续；= 0 ⇒ 说明该 profile 无规则，**必须如实说明**，
  不要用通用知识冒充客户规范。
- `载体层现状`里「规则型」显示为空 ⇒ 当前**没有任何可自动执行的约束**，
  须在回答中声明「本次只能按通用规则处理」。

## 失败处理
- 工具返回 `⚠️ 无法加载规约卡` ⇒ **如实说明未能加载**，禁止凭记忆假设方法论要求。
- 项目未绑定方法论 ⇒ 先问用户「本项目用哪套方法论」，不要默认。
""",
"""示例请求：「这个项目按什么方法论建模？」
→ 调 methodology_profile_load(stage="M0", project_id=<当前项目>)
→ 若 is_fallback=true：回答「当前未绑定项目方法论，以下按 OMG SysML V2 通用规则处理」
  并列出规约卡的 5 条语法强制规则，问用户是否有客户规范需要补充。
"""),

"sysml_requirement_structuring_guide": (
"""## 目标
把自然语言需求变成**可追溯、可验证**的条目，并同时产出需求视图骨架。

## 步骤
1. 调 `requirement_itemize`，传 `text`（整段需求原文）、`domain`、`source_ref`。
   一次调用同时得到两份产物：`items[]`（条目数据）+ `req_view_code`（需求视图骨架）。
2. **看 `gaps[]`**：含模糊词（良好/快速/高效）或无量化判据的条目会进gaps，
   **必须向用户澄清**，不要自己猜一个数值填进去。
3. **需求视图骨架必须过校验器**：把 `req_view_code` 交给 `sysml_v2_validate` 确认
   `verdict=pass`。不过就按诊断位置修后重试。
4. **刻意不生成 satisfy**：此时无 design 元素，强行生成必报
   `Must be a valid feature`。subject（主体）也留空，交由视图展开阶段判定。

## 判据
- 每条 item 必须有唯一 `id`（REQ-XXX）与 `sysml_name`（大驼峰，视图里的 requirement 名）。
- `req_view_code` 过 `sysml_v2_lint` 不出现 NAME 类违规（命名合规）。
- gaps 非空 ⇒ 说明需求本身有质量问题，**先澄清再建模**，不要带病往下走。

## 失败处理
- 分类异常（接口需求被判成性能类等）⇒ 检查是否单位词缺词边界；**不要放宽判据迁就错误分类**。
- 校验不过 ⇒ 按诊断行号修，**不要重写整段**。
""",
"""示例：「系统应支持 30 分钟低温启动，电池温差不超过 5℃，通过 CAN 总线通信」
→ requirement_itemize →
   items: REQ-P001(performance) / REQ-C002(constraint) / REQ-I003(interface)
   gaps: []（若混入「系统应具有良好的用户体验」则该条进gaps，须澄清）
   req_view_code: package + requirement def + doc 注释 → 交 sysml_v2_validate 确认 pass
"""),

"sysml_skeleton_generation_guide": (
"""## 目标
只产**架构骨架**：package / part def / port def / requirement def 与有依据的 satisfy。
视图细节（action/state/flow）是视图展开节点的活，不要越界。

## 步骤
1. **先查标准库**：写任何属性或类型引用前，调 `sysml_stdlib_meta` 确认成员真实存在。
   臆造成员名会产生 `An attribute must be typed by attribute definitions` 之类语义错。
2. 生成骨架，遵守硬约束：
   - 顶层 import 必须带可见性前缀：`private import ScalarValues::*;`
   - `part`/`port`/`item` 分别由 `part def`/`port def`/`item def` 定型，不可混用
   - satisfy 的被满足方必须是 requirement **usage**，不能直接引用 def
3. **生成后必须校验**：调 `sysml_v2_validate`；`verdict=block` 时按诊断位置修复后重试，
   **轮次上限 3**，超出即上报人工。
4. 命名与规则合规：调 `sysml_v2_lint` 确认无 NAME 类违规（类型大驼峰 / 实例小驼峰）。
5. 若出现确定性语法错误，可调 `sysml_v2_autofix` 做**语义等价**改写；
   它只改语法，涉及建模意图的只报告不改写。

## 判据
- 骨架里**不含** action/state/flow（越界即错误）。
- `sysml_v2_validate` verdict=pass（0 词法/语法错）。
- 每个类型引用都能在标准库里查到。

## 失败处理
- 标准库查不到某个成员 ⇒ **如实说明「标准库无此成员」**，不要换个像样的名字硬写。
- 3 轮修复仍block ⇒ 上报人工，不要无限重试。
""",
"""示例需求：「EV热管理系统：电池热管理、座舱热管理、整车热管理、热泵」
→ 查 stdlib_meta 确认可用的标准类型 →
   生成分层 package + 三个 part def + 端口定义 + requirement def + satisfy 分配 →
   sysml_v2_validate（pass）→ sysml_v2_lint（无 NAME违规）→ 交 N3 展开视图
"""),

"sysml_view_generation_guide": (
"""## 目标
按**固定顺序**展开八视图：需求 → 结构 → 用例 → 活动 → IBD → 时序 → 状态机 → 参数。
`view_type` 是参数，不是节点。

## 步骤
1. **先抽结构**（不要肉眼读代码）：调 `sysml_ast_extract` 拿到元素清单与关系清单。
   它会同时告诉你**本次未覆盖哪些元类**。
2. 按 `view_type` 生成对应视图，遵守各视图硬约束：
   - 需求视图：requirement + stakeholder + 有依据的 refine；subject 与satisfy **二选一**
   - 结构视图：层级组成 + 端口；连接用 `connect A to B`（无 `connector...from...to`）
   - 活动视图：**必须包含异常分支**，不能只画正常路径；分支必须写判定条件
   - 参数视图：约束与参数化关系
3. **AST 覆盖缺口必须如实告知**：时序/状态/参数三视图受导出器元类映射限制
   （MessageUsage/TransitionUsage/ParameterUsage 未映射），抽不出边时
   **禁止断言「模型里没有转换/消息」**——那是解析器没覆盖，不是模型没有。
4. 生成后 `sysml_v2_validate` → 有确定性语法错可 `sysml_v2_autofix` → `sysml_v2_lint` 查命名。

## 判据
- 八视图顺序固定，缺前序视图会导致后续引用不到元素。
- 活动图有异常分支。
- 每个视图生成后过校验器。

## 失败处理
- AST 工具返回「解析器不可用」⇒ **如实说明未能解析**，不要用正则假装解析成功。
- 校验不过 ⇒ 先 autofix（规则级），语义级问题上报人工。
""",
"""示例：「生成活动图」
→ ast_extract(files=[该工程全部 .sysml], view_type="activity")
   （注意输出里的「未覆盖元类」声明）
→ 生成含 if/else 判定 + 异常分支的活动视图
→ validate → autofix（若有connector/import 等确定性错）→ lint
"""),

"sysml_validation_repair_loop": (
"""## 目标
「诊断 → 修复 → 复核」闭环，**每轮修复后必须重跑校验确认**，不靠"应该好了"判断。

## 步骤
1. **先诊断**：调 `sysml_v2_validate` 拿三路诊断（词法/语法/语义）+ 行号。
2. **分类处理**：
   - 词法/语法错（`n_hard>0`）⇒ 可调 `sysml_v2_autofix` 做确定性改写
   - 语义错（`n_hard=0` 但有semantic）⇒ **涉及建模决策**（类型族不匹配、
     satisfy 引用谁、subject与 satisfy 二选一），**禁止自动改写**，须人工判断
3. **规范类问题**（命名不合规、用了 v1 关键字）⇒ 调 `sysml_v2_lint`，
   拿到每条违反的 enforce 分级：`check`/`forbid` 视为必须修。
4. **每轮修完重跑 validate**，比对 `n_error`/`n_hard` 是否真下降。
5. **发布前必须用工程级口径**：调 `sysml_v2_project_check`（多文件合并）。
   单文件口径会把跨文件引用报成伪错（实测同批文件单文件 n_hard=10、工程级 n_hard=2）。

## 判据
- 每轮修复都必须有 `n_error` 或 `n_hard` 的**实测下降**，否则该轮无效。
- **轮次上限 3**：超出即上报人工，不要无限重试。
- `sysml_v2_autofix` 若返回 `ineffective_rules` ⇒ 规则匹配到了但没修好，
  须如实说明「未能自动修复」，不要声称已修。

## 失败处理
- autofix 回滚某条规则（标 `rolled_back_rules`）⇒ 该规则在当前上下文有害，
  **不要重复调用**，改为人工修。
- 3 轮仍 block ⇒ 上报，并附上三轮的诊断对比。
""",
"""示例：validate 返回 block（12 处语法错，集中在 connector 旧记法）
→ autofix() → applied_rules=[R02_connector_to_connect ×10]，
  rolled_back=[]，after n_hard=0
→ 重跑 validate 确认 verdict=pass → 再 project_check（工程级）确认
→ 若出现 semantic 错（如 Must be a valid feature）⇒ 不自动改，上报人工
"""),

"sysml_trace_coverage_analysis": (
"""## 目标
需求↔设计元素双向追溯：**既查「有需求无实现」，也查「有实现无需求」**。

## 步骤
1. 取结构：调 `sysml_ast_extract` 拿到全部元素与 `满足` 关系。
2. 取图谱数据：调 `graph_db_query` / `graph_retrieve` 读 entities/relations。
3. 双向比对，**两侧都要报**：
   - 缺失满足：requirement 没有对应 design 元素 ⇒ 需求未落地
   - 孤儿元素：有 part/port 没有任何需求指向 ⇒ 可能是冗余建模
4. 汇总：调 `coverage_matrix` 产出覆盖矩阵。
5. **最后**才调 `gap_summary`，且必须已产出前三类结果
   （**skill铁律：gap_summary 必须最后调且要消费前三者**，单独调用它属违规）。

## 判据
- 覆盖率数字**必须来自查询结果**，禁止估算。
- 两侧缺失都必须报，**不能只报一侧**（只报一侧会让问题看起来比实际小）。
- 本技能在问答/报告场景**只读取值**，不得触发建模流水线。

## 失败处理
- 图谱查询为空 ⇒ 说明还没落库，应先走发布流程，不要报「覆盖率 0%」。
- 若某需求确实无法追溯（如纯文档要求），**标注为「有意不实现」并说明理由**，
  不要混进缺失列表。
""",
"""示例：「检查需求覆盖情况」
→ ast_extract（拿需求与满足关系）→ graph_db_query（拿图谱实体）
→ coverage_matrix（矩阵）
→ gap_summary（汇总，此时必须已消费上面三者的结果）
→ 报告：缺失满足 N条 / 孤儿元素 M 条 / 有意不实现 K 条（附理由）
"""),

"sysml_release_checklist": (
"""## 目标
发布是**不可逆动作**，必须按清单逐项过，且留痕。

## 步骤（顺序不可跳）
1. **工程级门禁**：调 `sysml_v2_project_check`（合并口径）。**不过不得发布**——
   单文件口径通过不算数（会把跨文件引用报成伪错，也会漏掉真错）。
2. **规范检查**：调 `sysml_v2_lint`，`check`/`forbid` 级违反必须清零。
3. **追溯核验**：完成追溯与覆盖性分析，确认无「缺失满足」。
4. **预演落库**：调 `sysml_import_graph` 但**不要传 confirm=true**（默认 dry-run），
   检查实体候选/关系候选/本体拒绝项是否与预期一致。
   ⚠️ 当前**没有撤销导入的工具**，误落库需人工清理。
5. **正式落库**：确认无误后再传 `confirm=true`，记录 batch_id。
6. **留痕**：记录批次号、模型名、校验结论、追溯覆盖率，交给报告生成。

## 判据
- 四项全过才能发布：工程级校验 pass / lint 无阻断违反 / 追溯无缺失 / 预演无异常拒绝。
- 落库后 `entity_count`/`relation_count` 与预演一致。

## 失败处理
- 任一项不过 ⇒ **停下上报**，不要"先发了再说"。
- 预演出现本体拒绝项 ⇒ 说明模型用了本体外的类型，先修模型再发。
""",
"""示例：发布 EV 热管理模型
→ project_check(files=[全部 6 个 .sysml]) → verdict=pass（工程级）
→ lint → 无 check/forbid 违反
→ import_graph(files=[...])（dry-run）→ 实体候选 N个 / 拒绝 0
→ import_graph(..., confirm=true) → 记录 batch_id
→ 报告留痕
"""),

"sysml_change_safety_analysis": (
"""## 目标
删除/移除/重构/批量替换/改边界**之前**算清影响面，**交人工决策**，不自行执行。

## 步骤
1. 调 `impact_analyze` 拿影响面（**数字必须来自该工具，禁止估算**）。
2. 沿五类引用链逐一核查，确认无悬空引用：
   - connection → port
   - requirement → design element
   - view → 展示元素
   - behavior → subsystem
   - verification case → requirement
3. 结合 `graph_retrieve` / `graph_db_query` 补充跨表影响。
4. 输出风险等级（低/中/高）+ 受影响元素清单 + 悬空引用清单。
5. `proceed_allowed=false` 时**禁止进入落库阶段**，必须先输出确认话术交人工。

## 判据
- 影响面数字来自 `impact_analyze`，不是估计。
- 悬空引用数 > 0 ⇒ 一律 `proceed_allowed=false`。
- 「把没用的删掉」这类**模糊指令必须先问清删除对象和范围**，
  不得自行推测删除目标。

## 失败处理
- `impact_analyze` 不可用 ⇒ **如实说明未能算出影响面**，禁止凭猜测放行删除。
- 无法确定删除范围 ⇒ 停下来问用户，不要挑一个看起来像废物的删。
""",
"""示例：「把通信子系统删掉」
→ impact_analyze(target="通信子系统") → 受影响 12 个元素 / 悬空引用 3 个
→ 沿五类引用链核查，确认 3 个悬空引用（2 个 port + 1 个 verify case）
→ risk_level=高，proceed_allowed=false
→ 输出确认话术：「删除将导致 X、Y 失去端口连接，Z 验证用例失效。确认请回复『确认删除』」
"""),

"sysml_stdlib_reference_guide": (
"""## 目标
生成任何属性/类型引用前**先查标准库**，杜绝臆造成员名。

## 步骤
1. 调 `sysml_stdlib_meta`：
   - 不带参数 → 返回全部 51 个包清单（先知道有什么）
   - `package="ISQ"` → 该包的成员列表
   - `name="ElectricChargeValue"` → 精确查询，返回所属包 / 类型 / **继承链** / 源码行号
2. 按返回的事实写代码：有什么、在哪、继承谁。
3. 继承链要跟着走到根（`ElectricChargeValue → ScalarQuantityValue → 根`），
   才能判断该类型能否用于当前建模目的。

## 判据
- **只取声明事实**（有什么/在哪/继承谁）。标准库是 KerML 元模型，
  **静态解析不可靠** ⇒ 必填特性、可选特性以校验器与官方规范为准，不要自行推断。
- 查不到 ⇒ **如实说明「标准库无此成员」**，不要换个"看起来对"的��字硬写。
  臆造会产生 `An attribute must be typed by attribute definitions` 之类语义错。

## 失败处理
- 工具返回「标准库模块不可用」⇒ **禁止臆造元类**，如实说明未能查询。
- 需要标准库里没有的类型 ⇒ 走本体扩展流程（`methodology_profile_load` 的本体约束段），
  不要在模型里硬造。
""",
"""示例：想写 `attribute mass : ISQ::mass;`
→ stdlib_meta(name="Mass") → 未找到
→ ⇒ 标准库没有 mass 成员，不能这么写
→ 改用标准库真实存在的类型（如 `MassValue`），或查 item def 定义自己的质量类型
"""),

"sysml_requirement_to_model_method": (
"""## 目标
需求条目 → SysML 建模元素的**映射方法**，保证每条需求在模型里都有对应实现且可追溯。

## 步骤
1. **需求侧**：用 `requirement_itemize` 得到条目（含 id / 分类 / 主体线索）。
2. **元素侧**：`sysml_ast_extract` 抽出现有部件/端口，判断该需求应挂在哪个元素上。
3. **建立满足关系**：`satisfy requirement <usage> :<需求def> by <元素usage>;`
   ⚠️ 被满足方必须是 **usage**，不能直接引用 def（否则报
   `Must reference a requirement`）。
4. **subject 与 satisfy 二选一**，不同时出现（同时出现语义冲突，校验器会报）。
5. 映射完成后进入追溯核验，确认无「有需求无实现」。

## 判据
- 每条 item 要么有对应 design 元素并建立 satisfy，要么显式标注「有意不实现 + 理由」。
- satisfy 的 usage 名唯一（同一 usage 不能satisfy 两条需求，除非确实一对一）。
- 映射后 `sysml_v2_validate` 通过。

## 失败处理
- 需求描述不足以判断主体 ⇒ **回问用户**，不要随便挂到一个元素上（错挂比不挂更糟）。
- 需求与已有元素冲突 ⇒ 上报，不要自动改需求。
""",
"""示例：需求 REQ-C002「电池温差不超过 5℃」
→ 判断主体 = 电池热管理子系统的冷却回路
→ 满足关系：satisfy requirement rTempDelta : ReqBatteryTempDelta by coolantLoop;
→ 校验通过 → 进入追溯核验
"""),
}


# ── 8 个视图 skill 的正文（模板化：每个视图有自己的硬约束与判据）──────────────
VIEW_META = {
    "requirement": (
        "需求视图（BDD 侧）",
        ["stakeholder（涉众）声明", "requirement def（id + text/doc）",
         "refine 细化关系（:>）仅在有依据时生成", "satisfy 分配：requirement usage ← design usage"],
        "subject 与 satisfy **二选一**，同时出现语义冲突，校验器会报",
        "refine 关系必须有依据（文本含细分/其中/之一等关系词），否则**不生成** —— "
        "凭空生成细化关系等于断言了不存在的派生关系",
        "satisfy 的被满足方必须是 usage：`satisfy requirement r :R by v;`"),
    "structure": (
        "结构视图（层级组成）",
        ["part def 类型层次", "part usage 实例", "port def 端口定义"],
        "part def（类型）用**大驼峰**，part usage（实例）用**小驼峰** —— "
        "规范相反，混淆会被 sysml_v2_lint 判违规",
        "顶层 import 须带可见性前缀：`private import X::*;`"),
    "usecase": (
        "用例视图",
        ["use case def 用例定义", "actor 参与方（用 part def 表达，SysML v2 规则）",
         "参与方-用例关联（用例内用 actor 特性）"],
        "SysML v2 **无 include 之外的老写法**：`extend SubUC` 是 v1 关键字，"
        "须写成 `include use case SubUC`",
        "参与方必须用 part def（v2 规则），不能用独立的 actor 关键字"),
    "activity": (
        "活动视图",
        ["action 动作", "if 判定分支", "**异常分支（必须！）**", "start/end 节点"],
        "**活动图必须包含异常分支**，只画正常路径是不合格的",
        "分支必须写判定条件：SysML v2 **无 `first` / `if-then-else` 关键字**，"
        "写成 `if <条件> { action A; } else { action B; }`",
        "条件缺失时不要凭空补条件 —— 就地标注缺口交建模方确认"),
    "ibd": (
        "交互视图（IBD）",
        ["port def / port usage 端口", "connect 连接（不是 connector）", "连接器语义"],
        "连接写法必须是 `connect A to B;` —— **无 `connector ... from ... to` 旧记法**",
        "连接端点用点号访问（`vehicle1.engine`），不是双冒号"),
    "sequence": (
        "时序（顺序）视图",
        ["消息交换 message usage", "时间顺序", "生命线"],
        "⚠️ **已知覆盖缺口**：AST 导出器的 NODE_KINDS 未映射 `MessageUsage` ⇒ "
        "`sysml_ast_extract` 抽不出消息边。**抽不出边不等于模型里没有消息**，"
        "遇到时如实说明是解析器未覆盖，不要下「模型无消息」的结论",
        "生成后必须过校验器确认语法正确"),
    "state": (
        "状态机视图",
        ["state 状态", "transition 转换（含守护条件 guard）", "初态/终态"],
        "⚠️ **已知覆盖缺口**：`TransitionUsage` 未映射 ⇒ 抽不出转换边。"
        "**同样禁止断言「模型里没有转换」**",
        "转换必须写触发事件与守护条件；**缺条件就标注，不要凭空补**"),
    "parameter": (
        "参数视图",
        ["约束 constraint", "参数化关系", "属性类型与量纲"],
        "⚠️ **已知覆盖缺口**：`ParameterUsage` 未映射 ⇒ 抽不出参数化边",
        "属性类型引用前先查标准库（`sysml_stdlib_meta`），**臆造成员名会产生语义错**"),
}


def _build_view_body(vtype: str):
    title, elements, *rules = VIEW_META[vtype]
    L = [f"## 目标\n生成{title}，遵守该视图的硬约束与 SysML v2 语法纪律。\n",
         "## 通用步骤\n"
         "1. **先抽结构**：调 `sysml_ast_extract`（`view_type` 传本视图类型）"
         "拿到现有元素与关系，不要肉眼读代码猜。\n"
         "2. 先查标准库（`sysml_stdlib_meta`）确认要引用的类型真实存在。\n"
         "3. 按下列要素生成视图代码。\n"
         f"4. 调 `sysml_v2_validate` 校验；`verdict=block` 时按诊断行号修复后重试（上限 3 轮）。\n"
         f"5. 调 `sysml_v2_lint` 确认命名合规（类型大驼峰 / 实例小驼峰）。\n",
         "## 本视图必备要素"]
    L += [f"- {e}" for e in elements]
    L.append("\n## 本视图硬约束（违反即不合格）")
    L += [f"{i+1}. {r}" for i, r in enumerate(rules)]
    L.append("\n## 判据\n"
             "- `sysml_v2_validate` verdict=pass（0 词法/语法错）\n"
             "- `sysml_v2_lint` 无 NAME 类违规\n"
             "- AST 工具输出的「未覆盖元类」声明已如实转达给用户\n")
    L.append("## 失败处理\n"
             "- AST 返回「解析器不可用」⇒ **如实说明未能解析**，不要用正则假装解析成功\n"
             "- 校验不过 ⇒ 先 autofix（规则级），语义级问题上报人工\n"
             "- 要素缺失（如活动图无异常分支）⇒ **不要悄悄略过**，补齐或明确说明缺口\n")
    example = (f"示例：生成{title}\n"
               f"→ ast_extract(view_type=\"{vtype}\")（注意输出里的「未覆盖元类」声明）\n"
               f"→ stdlib_meta 确认类型 → 生成视图代码 → validate → lint")
    return "\n".join(L), example


def build_frontmatter(name: str, desc: str, triggers: list, category: str) -> str:
    """生成与既有 skill 一致的 YAML frontmatter（平台内置 skill 的做法）。"""
    return "\n".join([
        "---",
        f"name: {name}",
        f"description: {desc}",
        f"triggers: [{', '.join(triggers)}]",
        f"category: {category}",
        "author: MBSE建模节点体系(V3-V10)",
        "version: v1.0",
        "---",
    ])


def main(apply_: bool):
    print("=" * 74)
    print("补齐 skill 执行正文与资源" + ("（APPLY）" if apply_ else "（DRY-RUN）"))
    print("=" * 74)
    if not os.path.isfile(DB):
        print(f"[ABORT] 库不存在：{DB}")
        return 2
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        nobj = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        if nobj < 50:
            print(f"[ABORT] 目标库可疑：对象数={nobj}")
            return 2

        # 8 个视图 skill 用模板生成正文（每视图有各自的硬约束与已知缺口）
        for _vt in VIEW_META:
            BODIES.setdefault(f"sysml_view_generation_{_vt}", _build_view_body(_vt))

        # 找到要补的 skill（只补 content 为空的，即 V9 建的空壳）
        targets = []
        for name in BODIES:
            row = conn.execute(
                "SELECT id,name,description,triggers,category,content,frontmatter,"
                "allowed_tools,`references` FROM skills WHERE name=?",
                (name,)).fetchone()
            if not row:
                print(f"  [SKIP ] {name} —— 库里不存在")
                continue
            if (row["content"] or "").strip():
                print(f"  [SKIP ] {name[:40]} —— 已有正文，不覆盖")
                continue
            targets.append(row)

        print(f"\n待补正文 {len(targets)} 个（共 {len(BODIES)} 个定义）")
        if not targets:
            print("  无需补齐")
            return 0

        # 写 references 文件到磁盘（只登记真实存在的）
        os.makedirs(PKG_ROOT, exist_ok=True)
        written = {}
        for r in targets:
            nm = r["name"]
            body, example = BODIES[nm]
            trig = json.loads(r["triggers"] or "[]")
            desc = r["description"] or ""
            cat = r["category"] or "AI建模"
            content = build_frontmatter(nm, desc, trig, cat) + "\n" + body
            # 资源目录：本 skill 专属
            rdir = os.path.join(PKG_ROOT, nm, "references")
            os.makedirs(rdir, exist_ok=True)
            # 把正文同时落一份到 references（供「按需加载」用，路径真实存在）
            ref_rel = f"static/skill_packages/mbse_modeling_skills/{nm}/references/{nm}.md"
            with open(os.path.join(rdir, f"{nm}.md"), "w", encoding="utf-8") as fh:
                fh.write(content)
            written[nm] = (content, ref_rel, body, example)
            print(f"  [WRITE] {nm[:38]:40} 正文 {len(content)} 字符 + 资源 {ref_rel[-34:]}")

        if not apply_:
            print("\n（dry-run，未写库）加 --apply 执行")
            return 0

        for r in targets:
            nm = r["name"]
            content, ref_rel, body, example = written[nm]
            examples = json.dumps([{
                "title": f"{nm} 典型调用示例",
                "content": example.strip(),
            }], ensure_ascii=False)
            refs = json.dumps([{
                "title": f"{nm} 完整正文与判据",
                "path": ref_rel,
            }], ensure_ascii=False)
            # scripts 保持 []：本轮不写脚本，**不登记不存在的路径**
            conn.execute(
                "UPDATE skills SET content=?, frontmatter=?, examples=?, "
                "`references`=?, scripts='[]', updated_at=datetime('now','localtime') "
                "WHERE id=?",
                (content, content, examples, refs, r["id"]))
        conn.commit()
        print(f"\n→ 已更新 {len(targets)} 个 skill 的正文与资源")

        # 回读自检
        print("\n── 自检 ──")
        ok = True
        import os as _os
        for r in targets:
            nm = r["name"]
            now = conn.execute(
                "SELECT content, frontmatter, `references`, scripts FROM skills WHERE id=?",
                (r["id"],)).fetchone()
            prob = []
            if len(now["content"] or "") < 400:
                prob.append("正文过短")
            if (now["frontmatter"] or "") != (now["content"] or ""):
                prob.append("frontmatter 与 content 不一致")
            try:
                rf = json.loads(now["references"] or "[]")
            except Exception:                # noqa: BLE001
                rf = []
            miss = [x.get("path") for x in rf if isinstance(x, dict)
                    and not _os.path.isfile(x.get("path") or "")]
            if miss:
                prob.append(f"references 指向不存在的文件: {miss}")
            if json.loads(now["scripts"] or "[]"):
                prob.append("scripts 非空（本轮不写脚本，应为空）")
            print(f"  {'[OK  ]' if not prob else '[FAIL]'} {nm[:40]:42} "
                  f"content {len(now['content'] or '')} 字符")
            if prob:
                ok = False
                for x in prob:
                    print(f"         {x}")

        print("\n" + "=" * 74)
        print("✅ 正文补齐完成" if ok else "⚠️ 有未通过项")
        print("=" * 74)
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.apply))