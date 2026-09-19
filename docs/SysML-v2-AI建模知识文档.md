# SysML v2 / KerML 文本建模知识文档

> **用途**：供 AI 模型生成 SysML v2 文本代码时使用的权威参考。读完本文档，应能写出**可直接通过 SysML v2 校验器**的模型代码。
>
> **版本**：v1.3.1（2026-09-19 —— 在 v1.3 主体的基础上，补 1 条**真机实测**得来的规则 `[S01]`：
> `subject = …` 与 `satisfy … by …` 对同一 requirement 只能二选一，见 §4.11 / §5 第 47 条）
> **编写方式**：以官方规范为权威定义，以真实校验器实测为最终裁判。全文 **467 条**定向用例
> （第 1–5 轮 150 条、第 6–8 轮 53 条、第 9–16 轮 205 条、**第 17–19 轮 59 条**），
> 另有 **62 个文档代码块**全量复跑，逐条取证。
> 每一轮的原始输出都在 `tmp/v2spec/_spec_check*.txt|json`，可单独复跑核对。
>
> **v1.3 修订说明** —— v1.2 的附录 A.4 留了 7 条「已知局限」。本轮把其中**最影响建模写法的 6 条**逐条实测收口，
> 用第 17–19 轮 **59 条定向用例**给出确定结论（含 3 条「正确写法终于找到了」和 1 条「规范有产生式、实现全不收」）：
>
> | # | 事项 | v1.2 的状态 | v1.3 的结论 | 依据 |
> |---|---|---|---|---|
> | 1 | `use case` 的 include | 未覆盖 | **`include` 是合法关键字**，三种形式均可：`include use case <名>;` / `include use case <名> : <类型>;` / `include <引用>;`。**`extend` 在 SysML v2 里根本不存在**（§8.2.2 的 341 条产生式中**零命中**） | `A01`–`A03`/`A11`–`A14`/`G07` ✅ / `A13` ❌ |
> | 2 | `rendering def` 自定义渲染器 | 未覆盖 | **正确用法找到了**：`rendering def X;` 只定**类型**，必须再写 `rendering myR : X;` 建 **usage**，然后 `render myR;` 引用 **usage**。直接 `render X;`（引用 def）报 `Couldn't resolve reference to Feature 'X'` | `G01`/`G03`/`G04` ✅ / `B13`/`B14` ❌ |
> | 3 | `enum def` 带属性字面量 | 记为「仍未覆盖」（已知必报错） | **可用形式找到了**：父类型必须是 **`attribute def`**（不能是 `enum def`）——`attribute def W { attribute c : String; }` + `enum def L :> W { enum low { :>> c = "green"; } }` | `C04`/`C11`/`C12`/`G08` ✅ / `C01` ❌ |
> | 4 | `flow` / `message` | 未覆盖 | `flow f of P from a.p to b.q;` ✅（`of` 可省）；**只支持 `from X to Y` 成对形式**，单端 `to` 不成立 | `D01`/`D02`/`D04`–`D06` ✅ / `D03`/`D13`/`D14` ❌ |
> | 5 | `flow def` | 未覆盖 | **必须含 `end`**：`flow def F { end a : A; end b : B; }` ✅；裸 `flow def F;` 报 `Must have at least two related elements` | `D11`/`G05`/`G06` ✅ / `D12`/`D07` ❌ |
> | 6 | KerML 内核构造 | 记为「仍未覆盖」 | **实现一律不收**：`unions` / `intersects` / `differences` / `disjoint from` / `featured by` **全部语法错**（已按 KerML 原文把位置补正到特化之后仍不收）；对照组 `part def C :> A, B;` ✅ —— 证明不是用例问题 | `E11`–`E16`/`E18` ❌ / `E17` ✅ |
> | 7 | `typed by` 的实现收口 | 仅列为「规范内部笔误」 | **实测三处全拒收**：`@MD typed by MD;` ❌、`metadata m typed by MD;` ❌、`part x typed by A;` ❌；一律改用 `:` | `F01`/`F11`/`F03` ❌ / `F02`/`F12` ✅ |
> | 8 | §8 小节顺序 | 8.1→8.2→**8.5**→8.3→8.4（**错序**） | 重排为 8.1–8.5，并新增 **§8.6 项目级合并校验脚本**（可直接复制落地） | 本文档 |
>
> **v1.3 净收益**：新增 **§4.17 流与消息**、**§8.6 合并校验脚本**；改写 **§4.4 / §4.12 / §4.15**（三处「未覆盖」变为「有实证写法」）；
> §5 错误对照表 38 → **46 条**；§0.1 规范 vs 实现不一致清单 9 → **10 例**。
>
> **v1.2 修订说明（历史留存）** —— 拿到了 **OMG SysML v2 英文官方规范**（`formal/25-09-03`，691 页，Part 1），
> 其中第 **8.2.2 章「Textual Notation」含 341 条 EBNF 产生式**（原文第 163–254 页），
> 以及**附录 A「完整示例模型」**（原文第 667 页起，13 节）。这两份是比此前用的**中文译本**更高一级的依据。
> 本轮把 EBNF 与官方示例同本项目全部实测结论逐条对撞，结果如下：
>
> | # | 事项 | v1.1 的说法 | v1.2 的结论 | 依据 |
> |---|---|---|---|---|
> | 1 | 权威层级 | 中文译本 = 官方规范 | **英文原版才是一手依据**；中文译本存在**独立于英文原版的错漏**（见 §0.1） | `irst` 笔误溯源 |
> | 2 | `variation part def X :> Y` | "规范示例与实现不一致，**应报错**" | **合法** ✅。真正被拒的是**变体点引用变体定义** `variation z : 变体def` ❌ | `W02`/`W03`/`Z02` |
> | 3 | `metadata X { ...; }` 关键字形式 | 未验证 | **合法** ✅（`T03`/`F01`/`T01`）。**体内每条成员必须带分号**（漏写即语法错 `T02`） | `T01`–`T05`/`F01` |
> | 4 | 视图渲染器名 | "需库支持，未验证" | **确定** ✅：`asTreeDiagram`/`asInterconnectionDiagram`/`asElementTable`/`asTextualNotation`，**须 `private import Views::*;`** | `H01`/`H02`/`H05` |
> | 5 | `subject` 位置 | **"必须是体内第一个参数"** | **过严**。真规则：**有 `stakeholder`/`actor` 时必须有 `subject`，且 `subject` 必须写在它们之前**；`doc` 可在 `subject` 之前 | `G02`/`O04` ✅ / `U02`/`U03`/`U05`/`U06`/`X01` ❌ |
> | 6 | 需求追溯机制 | 只有 `:>` / `dependency` | 官方机制是 **`allocate X to Y;`** 与 **`#derivation connection {...}`**（需 `import RequirementDerivation::*;`） | `J01`/`J03`/`J04` ✅ / `J02` ❌ |
> | 7 | `enum def` 特化 | 未覆盖 | `enum def X :> 另一个 enum def` ❌（被判"变体不得特化变体"）；`:> 非枚举分类器` ✅ | `V14`/`V15`/`V17` |
> | 8 | `conjugates` 关键字 | 列为 `~` 的等价关键字 | **SysML v2 保留字表里没有** `conjugates`，`~` 无关键字等价写法 | §8.2.2.1.2 + `V20` |
> | 9 | `irst` 笔误归属 | "官方文档笔误" | **只存在于中文译本**；英文原版为 `first`（0 处 `irst`） | 全文正则溯源 |
> | 10 | 语法依据形态 | 只有用例试错 | 新增 **附录 B：EBNF 产生式索引**、**附录 C：标准库可导入包清单** | — |
> | 11 | **`else` 的合法性** | **"任何位置都不合法"** | **过严**。`else` 在 `if {...} else {...}` 动作块里**合法** ✅；只作为**后继兜底**非法 ❌ | `E02` ✅ / `E01`/`E03`/`E04` ❌ |
> | 12 | `rep` 的写法 | 示例里写 `rep /* … */` | **`language "<lang>"` 必填且在注释之前**：`rep r1 language "text/plain" /* … */` | `N01`/`N02` ✅ / `N03`/`K01` ❌ |
> | 13 | `doc`/`comment` 的 `locale` | 未说明位置 | **`locale "xx"` 必须写在 `/* … */` 之前** | `N04` ✅ / `N05` ❌ |
> | 14 | **共轭 `~T` 的位置** | "定义处不行，特征位置可以" | **不准确**。合法位置是**特征/端位置**（`port a : ~P;`、`end e : ~P;`、`connect` 端、`port def Q :> ~P;`）；非法位置只有**定义处 `:`**（`port def Q : ~P;`） | `W01`–`W09` ✅ / `W04` ❌ |
> | 15 | `interface` 写法 | 未覆盖 | **`interface` 用法必须有至少 2 个端**：`interface i : IF connect a.p to b.q;` 或 `interface (a.p, b.q);` | `V01`/`V02`/`V03` ✅ / `V05`/`P04` ❌ |
> | 16 | `variant` | 未列 | **`variant` 是保留关键字**，不能当普通标识符 | `M01` ❌ |
>
> **v1.2 的净收益**：
> ① 把 6 条"试错得来的经验"升级为**有原文出处 + 有实测双证**的规则（§1.7 关键字表、§1.8 保留字、§2.2 导入可见性、
> §2.4 修饰符顺序、§2.5 多重性位置、§4.15 渲染器）；
> ② **纠正 5 条 v1.1 的过度概括或误判**（上表第 2、5、11、12、14 项）——这 5 条里任何一条按 v1.1 写都会让 AI 写错；
> ③ **从官方附录 A 补齐 4 类此前完全没有的构造**：`interface`、`allocate`、`#derivation connection`、`<短名>`。

>
> **v1.1 修订说明（历史留存）** —— v1.0 写成后，我把文档里**每一个代码块**和**每一条用例编号**都拉回校验器复跑对拍，
> 发现并修正了 6 处问题。全部列在下表，便于读者判断哪些结论可信：
>
> | # | 修正项 | v1.0 的写法 | v1.1 的写法 | 依据 |
> |---|---|---|---|---|
> | 1 | 类型化关键字 | `:` ≡ `typed by` | `:` ≡ **`defined by`**（KerML 规范的 `typed by`，**校验器不收**） | `Y01` ❌ / `Y02` ✅ |
> | 2 | 共轭端口 | `port def U : ~T;` 可用 | **定义处不行**；只能在特征位置写 `port p : ~T;` | `U33`/`Z02` ❌ / `Y21` ✅ |
> | 3 | 代码里的省略号 | 示例中写 `{ ... }` | `...` **不是合法记号**，必须写真实体或注释 | `Y03` / `Y04` ❌ |
> | 4 | 个体 / 时间切片取证 | 引用 `[X07]` | `[X07]` 原文**不含**这些构造；改用 `[Y15]` 的实证形式 | X07 源码核查 |
> | 5 | 后继（succession）用例编号 | `[B1]`–`[B10]` | `[U07]`–`[U16]`（`B1`–`B10` 是它们的**描述标签**，不是文件编号） | `tmp/v2spec/_ledger.txt` |
> | 6 | §6 模板的可编译性 | "全部经实测校验通过" | 分「**可直接编译**」与「**含占位符骨架**」两级，逐模板给证据编号 | `V19`/`V22`/`V26` 系列反例 |
>
> **这次自检的价值**：第 4、5 项属于**"结论对、出处错"**——写法本身没错，但读者照编号去查会查不到，
> 属于必须消除的**可核查性缺陷**。第 1、2、3 项则是**结论本身错了**，会让 AI 写出必然报错的代码。

---

## 0. 先把三件事说清楚（否则会用错本文档）

### 0.1 权威层级：规范说什么 ≠ 实现收什么

本文档的内容来自四个来源，**冲突时按下列优先级判定**：

| 优先级 | 来源 | 规模 | 性质 |
|---|---|---|---|
| ① 最高 | **`checker.jar` 实测** | **467 条**定向用例 + 62 个文档块复跑 | **最终裁判**。能否通过校验，只由它决定 |
| ② 高 | **SysML v2 英文原版规范**（`OMG Systems Modeling Language™ V2`，`formal/25-09-03`，691 页） | 第 7 章领域定义（94 页）+ **第 8.2.2 章 EBNF 具体语法（341 条产生式）** + 第 9 章库 + **Annex A 完整示例（13 节）** | **语法与语义的一手依据**。本文档所有"产生式"引用均出自此处，标注为 `§8.2.2.x` + 印刷页码 |
| ③ 中 | **SysML v2 中文译本**（`第1-7章+Annex 初稿`，docx，1980 个官方代码块） | 第 1–7 章 + Annex | 阅读辅助。**不能当一手依据**——已证实它含有英文原版没有的笔误（见下表第 7 项） |
| ④ 基础 | **KerML v1.0 规范**（454 页） | 词法与内核语义 | 标识符、注释、类型系统、表达式。注意 **SysML v2 对词法有覆盖**（见 §1.7/§1.8） |

**为什么实测优先于规范**：规范与规范之间、规范与实现之间都存在不一致。已实测 **10 例**：

| # | 一手出处怎么写 | 校验器怎么收 | 性质 | 依据 |
|---|---|---|---|---|
| 1 | 规范 §7 示例 `package Q { import C; }`（**无可见性前缀**） | 报语法错 → 必须写 `private import` | **规范示例自相矛盾**：同一份规范 §8.2.2.5.1 的产生式是 `Import = visibility = VisibilityIndicator 'import' ...`（**可见性无 `?`，必填**）。示例错了，产生式对了 | `T02`/`T03` |
| 2 | KerML §8.2.2.7：`TYPED_BY = ':' \| 'typed' 'by'` | **`typed by` 语法错**；只收 `defined by` | **KerML 与 SysML v2 不一致**。SysML v2 §8.2.2.1.2 的权威表是 `DEFINED_BY = ':' \| 'defined' 'by'`，**根本没有 `TYPED_BY`**。实现跟 SysML v2 | `Y01` ❌ / `Y02` ✅ |
| 3 | §8.2.2.27 的 `MetadataUsageDeclaration` 里写 `( ':' \| 'typed' 'by' )` | 收 `defined by`；**`typed by` 在 3 个位置实测全拒收** | **SysML v2 规范内部笔误**：该产生式混入了 KerML 的写法，与同规范 §8.2.2.1.2 冲突 | 原文第 192 页；`F01`/`F11`/`F03` ❌ / `F02`/`F12` ✅ |
| 4 | Annex A.12 两段式变体（`variation part def X :> Y` + `variation part z : X`） | 报 `A variation must not specialize another variation` | **只有后半句非法**：`variation part def X :> Y` 本身 ✅；`variation z : 变体def`（**变体点由变体定义定型**）才 ❌ | `W02`/`W03`/`Z02` |
| 5 | **Annex A.9 分析案例**：`subject = vehicle_b;` 写在 `in attribute scenario : Scenario;` **之后** | 报 `Subject must be first parameter.` | **官方示例违反自己的实现**：只要体内出现 `stakeholder`/`actor` 或需要主体的构造，`subject` 就必须在最前 | `X01` ❌ / `X04` ✅ |
| 6 | **Annex A.11 关注点**：`concern def X { doc /*...*/ stakeholder se : SafetyEngineer; }`（**有 stakeholder、无 subject**） | 报 `Subject must be first parameter.` | **官方示例无法通过校验器**。补上 `subject` 并置于最前即通过（`O03`/`R01` ✅） | `G03` ❌ / `O03` ✅ |
| 7 | 中文译本 Annex A.6 代码块写 `irst passenger1GetInVehicle then join1;` | — | **中文译本笔误**。英文原版 A.6 为 `first passenger1GetInVehicle then join1;`（英文 `irst` 命中 **0** 次，中文译本文本命中 **1** 次） | 双语全文对拍 |
| 8 | §8.2.2.17.8 有 `DefaultTargetSuccession = 'else' ownedRelationship += TransitionSuccessionMember` | **`else` 作后继兜底一律语法错** | **实现缺此产生式**。`else` 只在 `if {...} else {...}` 动作块里被接受 | `E01`–`E04` ❌ / `E02` ✅ |
| 9 | §8.2.2.11 有 `ConjugatedPortTyping = '~' ~[QualifiedName]` | `port def Q : ~P;` **语法错**；`port def Q :> ~P;` / `port a : ~P;` ✅ | **实现只认特征/端位置的共轭**，不认定义位的 `:` 定型 | `W04` ❌ / `W01`–`W09` ✅ |
| 10 | **KerML** 有 `TypeRelationshipPart = 'disjoint' 'from' … \| 'unions' … \| 'intersects' … \| 'differences' …` 与 `TypeFeaturingPart = 'featured' 'by' …` | `part def C :> A unions B;`、`… intersects …`、`… differences …`、`… disjoint from …`、`part x featured by B;` **全部语法错** | **KerML 内核构造未被 SysML v2 文本记法透出**。已按 KerML 原文把位置补正（`TypeDeclaration = ( SpecializationPart \| ConjugationPart )+ TypeRelationshipPart*`，即**必须跟在特化之后**）仍然不收；**对照组** `part def C :> A, B;` ✅ 通过，证明**不是用例写错** | `E11`–`E16`/`E18` ❌ / `E17` ✅ |

**结论**：任何从规范（任何版本、任何语言）抄来的写法，落地前都应过一遍校验器。规范是"应该长什么样"，校验器是"实际收什么"，**二者冲突时以校验器为准**（因为你的代码最终要过的是它）。

> ⚠️ **一条实用的取证纪律**：引用官方示例时，**优先引英文原版并标印刷页码**；引中文译本时要意识到"译本可能是错的"。
> 本文档 v1.1 曾把第 7 项记成"官方文档笔误"，这属于**出处误标**——写法结论不受影响，但读者照它去英文原版查证会查不到。
>
> ⚠️ **v1.2 补一条更关键的纪律**：**"规范有产生式" ≠ "实现接受"**。第 8、9 项就是这种情形——
> EBNF 里白纸黑字的产生式，校验器**不认识**。因此**不能靠读语法「推导」出可用写法**，必须以实测为准。
>
> ⚠️ **v1.3 把这条纪律的适用范围扩到 KerML**（第 10 项）：`unions` / `intersects` / `differences` / `disjoint from` /
> `featured by` 这五个在 **KerML 规范里有完整产生式**的构造，SysML v2 的实现**一个都不收**。
> 我为此走了一轮完整排查（第 17 轮报错 → 回查 KerML 产生式 → 第 18 轮补正位置重测 → 仍报错），才敢下这个结论。
> **排查顺序很重要**：先确认自己的用例没缺陷，再判「实现不支持」—— 否则会把「我不会写」误判成「它不支持」。

### 0.2 语言分层：KerML 是内核，SysML v2 是领域层

```
┌─────────────────────────────────────────────────────────┐
│  SysML v2 领域层（你在写的层）                            │
│  requirement / part / port / action / state / calc       │
│  concern / analysis / verification / variation / view     │
├─────────────────────────────────────────────────────────┤
│  KerML 内核层（语法骨架由它定义）                          │
│  Root:  元素、关系、依赖、注解、命名空间、导入              │
│  Core:  类型、分类器、特征（typing/subsetting/redefinition）│
│  Kernel:关联、连接器、行为、函数、表达式、多重性、包         │
└─────────────────────────────────────────────────────────┘
```

**实践含义**：`part def` 的底层是 KerML 的 `class`/`structure`；`connect` 的底层是 `connector`。**关键字的"等价表"和"类型约束"都来自 KerML 内核**，见 §3.4 与 §4。

### 0.3 本文档的记法

- ✅ 合法写法（已实测通过，标注用例号如 `[T01]`）
- ❌ 非法写法（已实测报错，标注用例号）
- `⚠️` 陷阱提示
- 所有用例号可在附录 A 检索到完整源码与报错原文

---

## 1. 词法硬约束（写错 100% 报错，无商量余地）

来源：KerML v1.0 §8.2.2 Lexical Structure（规范第 75–78 页）

### 1.1 标识符：只能 ASCII，中文走单引号

KerML 原文产生式：

```
NAME                  = BASIC_NAME | UNRESTRICTED_NAME
BASIC_NAME            = BASIC_INITIAL_CHARACTER BASIC_NAME_CHARACTER*
UNRESTRICTED_NAME     = single_quote ( NAME_CHARACTER | ESCAPE_SEQUENCE )* single_quote
BASIC_INITIAL_CHARACTER = ALPHABETIC_CHARACTER | '_'
BASIC_NAME_CHARACTER  = BASIC_INITIAL_CHARACTER | DECIMAL_DIGIT
ALPHABETIC_CHARACTER  = 'a'..'z' | 'A'..'Z'        ← 只有 ASCII 字母
DECIMAL_DIGIT         = '0'..'9'
NAME_CHARACTER        = 任何可打印字符，除反斜杠和单引号
```

**规则**：

| 规则 | 正例 | 反例（实测报错） |
|---|---|---|
| 裸名只能用 `A-Z a-z _` 开头，后续只能 `A-Z a-z 0-9 _` | `BatteryPack` `_Internal2` `x2` ✅ `[V12]` | `2Wheel` ❌ `[V13]` → `no viable alternative at input '2'` |
| **含非 ASCII / 空格 / 连字符 / 点号的名字，必须用单引号包裹** | `'温度传感器'` ✅ `[T04]`、`'Fuel-Tank Assembly'` ✅ `[T06]`、`'off-starting'` ✅ `[X01]` | `part def 温度传感器` ❌ `[T05]` → `no viable alternative at character '温'` |
| 单引号内允许转义 | `\'` `\"` `\b` `\f` `\t` `\n` `\\` | — |

> ⚠️ **这是中文建模唯一的合法出路**。TMS 模型里想写"电池包""冷却液温度"，必须写成 `part def '电池包'` 或改用英文标识符 + `doc` 中文说明。**推荐后者**（英文名 + 中文 doc，可读性与工具兼容性都更好）。

### 1.2 短名（short name）：尖括号

产生式（KerML §8.2.3.1、§8.2.4.3.1）：

```
Identification        = ( '<' declaredShortName = NAME '>' )? ( declaredName = NAME )?
FeatureIdentification = ( '<' declaredShortName = NAME '>' ) ( declaredName = NAME )?
                      | declaredName = NAME
```

两种形式**都合法**（均实测通过）：

```sysml
requirement def <R1> MaxMass;            // ✅ [V09] 短名为裸名
requirement def <'1.1'> MaxMass;         // ✅ [V10] 短名为单引号名（可含点号）
attribute <kg> kilogram : MassUnit;      // ✅ [V11] 短名 + 单引号长名并存（官方库原样）
```

> 短名的作用：给元素一个**不含非法字符的短标识**，供 `[kg]` 这类引用使用。官方 SI 库就是这样把 `kilogram` 暴露为 `kg` 的。

### 1.3 编码：UTF-8，**绝不能带 BOM**

| 项 | 结论 |
|---|---|
| 文件编码 | UTF-8 |
| BOM | ❌ **禁止**。带 BOM 实测报 `no viable alternative at character '?'` `[T38]` |
| 行终止符 | LF 或 CRLF 均可，但**同一文件必须一致**（KerML §8.2.2.1 Note 1） |
| 空白 | 空格 / Tab / 换页 / 行终止符，任意缩进均可（官方库用 Tab） |

> ⚠️ 这是最隐蔽的坑之一：很多编辑器默认给 UTF-8 文件加 BOM，代码看起来完全正常，校验器直接报第 1 行第 1 列错误。

### 1.4 注释：三种形式

产生式（KerML §8.2.2.2）：

```
SINGLE_LINE_NOTE = '//' LINE_TEXT
MULTILINE_NOTE   = '//*' COMMENT_TEXT '*/'
REGULAR_COMMENT  = '/*' COMMENT_TEXT '*/'
```

| 形式 | 写法 | 实测 |
|---|---|---|
| 单行注释 | `// 说明` | ✅ `[T29]` |
| 常规块注释 | `/* 说明 */` | ✅ `[T31]`，也是 `doc` / `comment` 的**唯一合法载体** |
| 多行注释 | `//* 说明 */` | ✅ `[T30]` |

> ⚠️ **易错点**：`//* ... */` 是**多行注释**，不是"文档注释"。写 `/** ... */` 虽然能通过（因为它匹配 `/* ... */`），但 `//*` 才是规范定义的多行形式。
>
> ⚠️ **致命易错点**：`doc` 后面**只能跟 `/* ... */`**。写成 `doc "字符串"` 报 `mismatched input '"..."' expecting RULE_REGULAR_COMMENT` `[T33]`；写成 `doc //* ... */` 报 `missing RULE_REGULAR_COMMENT` `[U21]`。

### 1.5 数值与单位

产生式（KerML §8.2.2.4–8.2.2.5）：

```
DECIMAL_VALUE     = DECIMAL_DIGIT+
EXPONENTIAL_VALUE = DECIMAL_VALUE ('e'|'E') ('+'|'-')? DECIMAL_VALUE
STRING_VALUE      = '"' ( STRING_CHARACTER | ESCAPE_SEQUENCE )* '"'
```

| 写法 | 实测 | 说明 |
|---|---|---|
| `0.1` | ✅ `[T24]` | 常规小数 |
| `.1` | ✅ `[T23]` | **允许省略前导 0**（官方 A.13 大量使用） |
| `0 [kg]` | ✅ `[V01]` | **必须 `import SI::*`** 才能解析 `kg`；不导入报 `Couldn't resolve reference to Element 'kg'` `[V02]` |
| `328 [K]` | ✅ `[X07]` | 温度单位同理由 SI 库提供 |
| `"文本"` | ✅ | 双引号字符串，转义同 §1.1 |

> ⚠️ 数值**不带符号**：`-1` 是"取负运算符 + 1"，不是字面量（KerML §8.2.2.4 Note 1）。

### 1.6 符号表与"最长匹配"

KerML 定义的符号（§8.2.2.7）：

```
( ) { } [ ] ; , ~ @ # % & ^ | * ** + - / -> $ . .. :
:: :> :>> ::> => < <= = := == === != !== > >= ? ?? .?
```

**最长匹配规则**（产生式原文）：输入按从左到右分组为**尽可能长**的记号。

> 例：`a:::b` 被切成 4 个记号 `a` `::` `:` `b`，而不是 `a` `:::` `b`。
>
> 实践含义：**`::>` `:>>` `:>` 之间不能有空格**，`a ::> b` 与 `a::>b` 是不同结果。

### 1.7 关键字 ↔ 符号等价表（背下来，AI 最常在这里出错）

**权威出处**：SysML v2 规范 §8.2.2.1.2 Lexical Structure，**原文第 165 页**逐字如下（不是 7 组，是 **6 组**）：

```
DEFINED_BY  = ':'   | 'defined' 'by'
SPECIALIZES = ':>'  | 'specializes'
SUBSETS     = ':>'  | 'subsets'
REFERENCES  = '::>' | 'references'
CROSSES     = '=>'  | 'crosses'
REDEFINES   = ':>>' | 'redefines'
```

整理成表：

| 语义 | 符号 | 关键字 | 用在哪 |
|---|---|---|---|
| **类型化** | `:` | **`defined by`** | 特征的类型（定义 → 使用） |
| **特化** | `:>` | `specializes` | 分类器之间的关系 |
| **子集化** | `:>` | `subsets` | 特征之间的关系 |
| **引用** | `::>` | `references` | 引用子集化（satisfy/perform 等） |
| 交叉 | `=>` | `crosses` | 交叉子集化 |
| **重定义** | `:>>` | `redefines` | 特征重定义 |

全部等效性已实测：`:>` ≡ `specializes` `[T07]`、`:>>` ≡ `redefines` `[T41]`、`:>` ≡ `subsets` `[T42]`、`:` ≡ `defined by` `[T43]`。

> ⚠️ **`:` 的关键字是 `defined by`（两个词），不是 `typed by`** —— 这是本文档最容易出错的一处：
>
> ```sysml
> item x defined by A;    // ✅ [Y02]（也即 [T43]）0 ERROR
> item x typed by A;      // ❌ [Y01] no viable alternative at input 'typed'
> ```
>
> 原因是 **KerML 与 SysML v2 不一致**：KerML §8.2.2.7 写 `TYPED_BY = ':' | 'typed' 'by'`，
> 而 SysML v2 §8.2.2.1.2 的权威表写 `DEFINED_BY = ':' | 'defined' 'by'`（**无 `TYPED_BY`**）。
> 实现跟随 SysML v2。**结论：写 `defined by`。**

> ⚠️ **`~`（共轭）在 SysML v2 里没有关键字写法**。上表**没有** `CONJUGATES` 一项（KerML 有，SysML v2 删掉了），
> SysML v2 保留字表里也**没有** `conjugates`。实测：
>
> ```sysml
> port def B conjugates A;   // ❌ [V20] no viable alternative at input 'conjugates'
> port p : ~A;               // ✅ 只能用 `~`，且只能出现在"定型"位置
> ```
>
> 共轭的**唯一**合法位置见 §4.7。

> ⚠️ **注意 `:>` 是双重语义**：用于**类型**时是"特化"（`subtype Specific :> General`），用于**特征**时是"子集化"（`feature Specific :> General`）。`SPECIALIZES` 与 `SUBSETS` 在词法层是**同一个终结符**，由位置决定语义。

### 1.8 保留字

**权威出处**：SysML v2 规范 §8.2.2.1.2，**原文第 165 页**，原文措辞是
"The reserved keywords of SysML are the following"，**共 130 个，一次列全**（不必再去看 KerML 的 91 个——SysML v2 这份是完整清单）：

```
about abstract accept action actor after alias all allocate allocation analysis
and as assert assign assume at attribute bind binding by calc case comment
concern connect connection constant constraint crosses decide def default
defined dependency derived do doc else end entry enum event exhibit exit expose
false filter first flow for fork frame from hastype if implies import in include
individual inout interface istype item join language library locale loop merge
message meta metadata nonunique not null objective occurrence of or ordered out
package parallel part perform port private protected public redefines ref
references render rendering rep require requirement return satisfy send snapshot
specializes stakeholder standard state subject subsets succession terminate then
timeslice to transition true until use variant variation verification verify via
view viewpoint when while xor
```

> ⚠️ **注意几个"看起来像自由标识符、其实保留"的词**（实测任一作为标识符都是语法错）：
> `variant` `entry` `do` `exit` `expose` `filter` `render` `subject` `actor` `stakeholder`
> `allocate` `satisfy` `verify` `assert` `assume` `require` `include` `objective` `frame`
> `accept` `send` `assign` `terminate` `transition` `decide` `first` `then` `else` `at` `after` `when`
>
> ```sysml
> part def analysis;   // ❌ [V21] no viable alternative at input 'analysis'
> part def expose;     // ❌ [V22] no viable alternative at input 'expose'
> part def variant;    // ❌ [V23] no viable alternative at input 'variant'
> part def entry;      // ❌ [T14] no viable alternative at input 'entry'
> part def analysisEnum;   // ✅ 加前缀/后缀即可（合法标识符）
> ```
>
> **特别注意 `at` / `after` / `when` 这三个**：它们是 `accept` 的触发类型关键字，**不能直接当枚举字面量**：
>
> ```sysml
> enum def K { test; analysis; }      // ❌ [W18] no viable alternative at input 'analysis'
> enum def K { test; 'analysis'; }    // ✅ [W19] 用单引号即可
> ```

> ⚠️ **重要判断依据**：如果某个词**不在上面的 130 个里**，那它就是**臆造的**，写了必然报错。
>
> **已实测确认的臆造关键字**（在 AI 生成代码中高频出现）：
>
> | 臆造词 | 实测结果 |
> |---|---|
> | `refines` | ❌ `no viable alternative at input 'refines'` `[T08]` |
> | `traces` | ❌ `no viable alternative at input 'traces'` `[T09]` |
> | `conjugates` | ❌ `no viable alternative at input 'conjugates'` `[V20]`（SysML v2 已删除，共轭只能用 `~`） |
> | `&&` / `\|\|` | ❌ `no viable alternative at input '&'` `[T27]`（用 `and` / `or`） |
>
> 这三个词在官方标准库（`sysml.library/`，**121 个文件**）里**命中 0 次**，可直接判定为不存在。
>
> **v1.2 补测**：`variant` 也确认为保留关键字（`M01`：`part variant : P;` → `no viable alternative at input 'variant'`）。

### 1.9 两类"看起来像说明、其实是非法代码"的写法

这两类是**文档/示例转述代码**时的经典事故，AI 很容易连带抄进去：

| ❌ 非法 | 报错 | 原因 | ✅ 正确做法 |
|---|---|---|---|
| `package Defs { ... }`<br>`part def V { ... }` | `mismatched input '..' expecting '}'` | **`...` 不是 SysML 记号**（`..` 只在范围/多重性里有义，如 `[0..*]`、`1.0 .. 3.0`） | 写成**真实体**，或改用注释 `/* 其余成员省略 */` |
| `part def <需求名>Requirement { ... }` | `no viable alternative at character '需'` | `<...>` 内**只能放标识符**（裸名或单引号名），且**中文裸名一律非法** | 占位符**必须被替换**；若确实要用中文名，写 `<'需求名'>`（单引号） |

实测：`Y03`（`{ ... }` 于定义体内）、`Y04`（`{ ... }` 于包体内）均报语法错；
`Y05`（换成 `attribute mass : Real = 1.0;` 的真实体）0 ERROR；`Y22`（嵌套包真实体 + 空包）0 ERROR。

> ⚠️ **给 AI 的硬规则**：**输出的代码里不允许出现 `...`；输出的代码里不允许残留 `<...>` 占位符。**
> 二者都会让校验器在第一屏就报语法错，而且错误位置会漂移，掩盖后面的真实问题。

---

## 2. 语法骨架

### 2.1 包与成员

```
Namespace        = ( PrefixMetadataMember )* NamespaceDeclaration NamespaceBody
NamespaceDeclaration = 'namespace' Identification
NamespaceBody    = ';' | '{' NamespaceBodyElement* '}'
MemberPrefix     = ( visibility = VisibilityIndicator )?      ← 可选
VisibilityIndicator = 'public' | 'private' | 'protected'
```

SysML v2 用 `package` 关键字：

```sysml
package SimpleVehicleModel {
    private import ISQ::*;
    package Definitions {
        part def Wheel;                  // ✅ [Y22] 嵌套包写"真实体"
    }
    part w : Definitions::Wheel;
}
package EmptyPkg;                        // ✅ [U25] 空包：以分号结尾，无体
standard library package SI {            // ✅ [V14] 官方库用这个关键字
    attribute x : Real = 1.0;            //    （会产生 1 个 WARN）
}
```

> ⚠️ **v1.1 修正**：v1.0 这里写成 `package Definitions { ... }` / `standard library package SI { ... }`，
> **`...` 是非法记号**（实测 `Y03`/`Y04` 报 `mismatched input '..' expecting '}'`）。必须写真实体，
> 或改用注释 `/* 其余成员省略 */`。

**成员可见性**（`public`/`private`/`protected`）是**可选**前缀：

```sysml
part def V {
    private part hidden;
    public  part shown;
}
```

### 2.2 导入：可见性前缀是**必填**的（实测铁律）

**权威出处**：SysML v2 规范 §8.2.2.5.1，**原文第 167 页**逐字：

```
Import =
    visibility = VisibilityIndicator
    'import' ( isImportAll ?= 'all' )?
    ImportDeclaration
    RelationshipBody

ImportDeclaration : Import = MembershipImport | NamespaceImport
MembershipImport  = importedMembership = [QualifiedName] ( '::' isRecursive ?= '**' )?
NamespaceImport   = importedNamespace  = [QualifiedName] '::' '*' ( '::' isRecursive ?= '**' )?
VisibilityIndicator : VisibilityKind = 'public' | 'private' | 'protected'
```

> **为什么这是"铁律"而不是"习惯"**：`visibility = VisibilityIndicator` 后面**没有 `?`**，即语法上必填。
> 对比同一个产生式集里的 `MemberPrefix = ( visibility = VisibilityIndicator )?` —— **普通包成员**的可见性是可选的，
> **只有 `import`** 被写成必填。这不是实现偏好，是规范原文。

**四种导入形式**（全部实测通过）：

```sysml
private import ScalarValues::*;        // ✅ [T01]  命名空间导入（导入包内所有成员）
private import ScalarValues::Real;     // ✅ [T03]  成员导入（只导入具名成员，不加 ::*）
private import ScalarValues::**;       // ✅ [V16]  递归导入（含所有嵌套包）
private import ScalarValues::*::**;    // ✅ [V17]  只递归嵌套包，不含包自身
public  import ScalarValues::*;        // ✅ [V15]  public 导入对包外可见
```

**导入过滤**（`[]` 内为布尔表达式，**表达式里引用的概念来自标准库**）：

```sysml
public import Parts::**[@PartUsage];                                   // ✅ [V18] 只导入 PartUsage 元素
public import DesignModel::**[@Approval and approved and level > 1];   // 官方 §7.5.4 示例
```

> ⚠️ 过滤表达式里的 `@PartUsage`、`approved`、`level` 等都要能被解析——它们**引用的是模型库里的元类/特征**，
> 所以过滤器通常需要配合 `private import` 才能在语义上通过（语法上永远合法）。

**❌ 实测确认非法**：

```sysml
import ScalarValues::*;        // ❌ [T02] mismatched input 'import' expecting '}'
import ScalarValues::Real;     // ❌ [T03] 同上
```

> ⚠️ **这是 AI 生成代码的头号错误**。规范第 7 章的示例里出现过裸 `import C;`，但**同一份规范 §8.2.2.5.1 的产生式
> 自己规定可见性必填**——即规范示例与规范语法自相矛盾，**校验器站在产生式这一边**。
> **永远写 `private import` 或 `public import`。**

> ⚠️ **不要导入不存在的库**：可导入的包名以本地 `sysml.library/` 为准（**121 个文件，其中 59 个 `.sysml` 包**），
> 完整清单见 **附录 C**。写 `import` 前先查表，否则报 `Couldn't resolve reference to ...`。

### 2.3 定义（definition）与使用（usage）

这是 SysML v2 的**核心概念对**：

| 概念 | 写法 | 作用 | 例子 |
|---|---|---|---|
| **定义** | `X def Name` | 声明一个**类型** | `part def BatteryPack;` |
| **使用** | `X name : Type` | 声明一个**实例/成员** | `part pack : BatteryPack;` |

```sysml
part def Vehicle;                    // 定义
part vehicle1 : Vehicle;             // 使用 —— 类型必须是上面那个定义
part vehicle2 : Vehicle {            // ✅ [Y05] 带体的使用（体里写真实成员，不是 `...`）
    attribute mass : Real = 1.0;
}
```

**纯声明**（无体）合法：`part def X;` ✅ `[U26]`、`part def A;` ✅ `[T07]`

**带体**：

```sysml
part def BatteryPack {
    attribute cellTempMax :> ISQ::thermodynamicTemperature;
    port coolantIn : CoolantInletPort;
    assert constraint limit { cellTempMax <= cellTempAllowed }
}
```

> ⚠️ **体内的成员引用**：体里可以引用同包或已导入的任意元素。

### 2.4 特征修饰符的**固定顺序**（写错顺序会报错）

KerML 产生式（§8.2.4.3.1）：

```
BasicFeaturePrefix :
    ( direction = FeatureDirection )?                     ← in / out / inout
    ( isDerived ?= 'derived' )?
    ( isAbstract ?= 'abstract' )?
    ( isComposite ?= 'composite' | isPortion ?= 'portion' )?
    ( isVariable ?= 'var' | isConstant ?= 'const' )?
```

**必须按此顺序**：

```sysml
out attribute power :> ISQ::power;        // ✅ [X05]  方向 → 种类 → 名字 → 子集化
in item fuelCmd : FuelCmd;                // ✅
abstract ref part connectedDevices : Device;   // ✅ [Z11] abstract → ref → 种类
constant ref part approvedAircraft;       // ✅ [Z11]
```

**顺序错了会报错**（实测 `[Y11]`）：

```sysml
ordered part oc :>> c;    // ❌ 修饰符跑到种类关键字前面了 → no viable alternative at input 'part'
part oc ordered :>> c;    // ✅ 修饰符跟在名字后面
```

> ⚠️ `ordered` / `nonunique` 这两个**多重性修饰符的位置**和其他修饰符不同：
> 它们跟在**名字之后**（或某个特化子句之后），而 `in/out/derived/abstract/composite/var` 必须在**种类关键字之前**。

### 2.5 特征声明的完整结构

```
FeatureDeclaration =
    ( isSufficient ?= 'all' )?
    ( FeatureIdentification ( FeatureSpecializationPart | ConjugationPart )?
    | FeatureSpecializationPart
    | ConjugationPart )
    FeatureRelationshipPart*

FeatureSpecializationPart =
      FeatureSpecialization+ MultiplicityPart? FeatureSpecialization*
    | MultiplicityPart FeatureSpecialization*

FeatureSpecialization = Typings | Subsettings | References | Crosses | Redefinitions
```

**翻译成人话**——一条特征声明可以是：

```
[修饰符] 种类 名字 [多重性] [关系...] [值] [体]
```

实测通过的组合：

```sysml
item x : A, B :> f :>> g;                    // ✅ [Y16] 官方形式：typing + subsetting + redefinition 一条写全
attribute mass :> ISQ::mass;                 // ✅ 子集化
part cylinders : Cylinder[4..8] ordered;     // ✅ 多重性 + ordered
part frontWheels : Wheel[2];                 // ✅ 定长多重性
part a : P[0..*];                            // ✅ [V31]
part a : P[*];                               // ✅ [V31]
part a : P[0..n];                            // ✅ [V31] 上界可以是表达式
part a[0..*] : P;                            // ✅ [Y12] 多重性也可写在"名字之后"
part redefines cylinders[4];                 // ✅ [T10] 无名重定义（有效名继承）
ref item parent[2] : Person;                 // ✅
oc ordered :>> c;                            // ✅ [Y10] 多重性修饰符跟在名字后
```

> ⚠️ **`[Y16]` 的注意事项**：完整形式**语法上合法**，但对其中每个被引用的特征都有解析要求
> （实测：少声明一个 `g` 就报 `Couldn't resolve reference to Feature 'g'`）。
> 该行仍是"官方形式的合法语法"，但**建议拆开写**，逐个声明 + 逐个 `:>` / `:>>`，可读性和可诊断性都更好
> （SysML v2 规范中文版 §7.6 也有同款编者注：避免链式写法）。

### 2.6 值：`=` / `default` / `:=`

```sysml
attribute x : Real = 1.0;                    // ✅ [T01]  初始值
attribute x : Real default 1.0;              // ✅ [U23]  default（可被覆盖的默认值）
attribute count : Natural := 0;              // ✅ [U22]  := 在行为语义中赋值
attribute :>> tempAllowed = 328 [K];         // ✅ [X07]  重定义 + 赋值（X07 第 148 行原文）
attribute mass redefines mass = dryMass + cargoMass;   // ✅ 官方 A.4
```

**赋值语句**：`assign` 不是"声明"，而是**语句**，**必须写在动作体（或状态体）里**——
裸放在包体顶层会报 `mismatched input 'assign' expecting '}'`（实测 `BLK12`）。

```sysml
action def Counter {                          // ✅ [Y06] 0 ERROR
    attribute count : Natural = 0;
    assign count := count + 1;
}

// ✅ [V32] 特征链也可作赋值目标，但注意：赋给 attribute 的链，其类型必须由 attribute def 定型
// assign sim.vehicle.position := sim.vehicle.position + sim.vehicle.velocity * deltaT;
```

### 2.7 表达式与运算符（**AI 高频翻车区**）

**运算符优先级表**（KerML §8.2.5.8.1 Table 6，从高到低）：

```
Unary:   all
         +  -  ~  not
Binary:  ^  **
         *  /  %
         +  -
         ..
         <  >  <=  >=
         istype  hastype  @  @@  as  meta
         ==  !=  ===  !==
         &  and
         xor
         |  or
         implies
         ??
Ternary: if
```

**⚠️⚠️ 最重要的一条：SysML v2 没有 `&&` 和 `||`**

| 语言习惯 | SysML v2 正确写法 | 实测 |
|---|---|---|
| `a && b` | `a & b` 或 `a and b` | ❌ `[T27]` / ✅ `[T26]` `[T28]` |
| `a \|\| b` | `a \| b` 或 `a or b` | ✅ `[T28]` |
| `!a` | `not a`（**没有 `!` 运算符**；`~` 是共轭语义，**不要当逻辑非用**） | ✅ `[V29]` |
| `a ? b : c` | `if a ? b else c` | ✅ `[V29]` |

**基本运算**（`[V29]` 原文，0 ERROR）：

```sysml
attribute a : Real = 1.0;
attribute s1 : Real = a + b * c - c / b;                    // 算术
attribute s2 : Real = (a + b) ** 2;                         // 幂（右结合）
attribute s3 : Boolean = (a < b) & (b <= c);                // 逻辑与
attribute s4 : Boolean = (a == b) or (b != c);              // 逻辑或
attribute s5 : Boolean = not (a > b);                       // 逻辑非
attribute s6 : Real = if s3 ? a else b;                     // 三元
attribute s7 : Boolean = a istype Real;                     // 类型测试
attribute s8 : Real = a ?? b;                               // 空值合并
attribute s9 : Real[1..*] = 1.0 .. 3.0;                     // 范围
```

**序列 / 集合操作**（需 `private import SequenceFunctions::*;`）：

```sysml
part def P {                                                // ✅ [Y14] 0 ERROR
    attribute xs : Real[1..*] = 1.0 .. 3.0;
    attribute n : Natural = xs->size();                     // -> 集合操作
    attribute y : Real = xs#(1);                            // #(i) 索引
}
```

> ⚠️ **v1.1 修正**：v1.0 把 `->size()` 与 `#(i)` 也归到 `[V29]` 名下，但 **V29 原文里没有这两行**。
> 它们的实测依据是 `[U24]`（`->` 操作符）、`[V04]`（`->size()`）、`[Y14]`（`#(i)`，0 ERROR）。
> 另外 `xs` **必须先声明并有值**，否则会报 `Couldn't resolve reference`。

**带单位的量纲运算**：

```sysml
attribute e :> ISQ::energy = m * v ** 2;      // ✅ [V30] 单位自动推导
```

**常见库函数**（需正确导入，见 §5.14）：

| 函数 | 所属库 | 导入写法 |
|---|---|---|
| `sum` | `RealFunctions`（Real 版） | `private import RealFunctions::*;` |
| `size` | `SequenceFunctions` / `CollectionFunctions` | `private import SequenceFunctions::*;` |
| `kg` `m` `s` `K` `W` 等单位 | `SI` | `private import SI::*;` |
| `Real` `Integer` `Boolean` `String` | `ScalarValues` | `private import ScalarValues::*;` |

---

## 3. 类型系统规则（实测得出的硬约束，AI 最易忽略）

这是**语义层**的约束：语法对了，类型不对照样报错。

### 3.1 三种"种类"及其类型要求

SysML v2 把元素分成三个正交族，**类型必须匹配**：

| 被声明的种类 | 必须由什么定型 | 实测 |
|---|---|---|
| `attribute` | **attribute def** | ❌ `attribute w : Wheel`（Wheel 是 part def）→ `An attribute must be typed by attribute definitions.` `[V32]` |
| `item` / `part` / `occurrence` | **occurrence def**（item def / part def / occurrence def） | ❌ `out item power : HeatFlow`（HeatFlow 是 attribute def）→ `An occurrence, item or part must be typed by occurrence definitions.` `[W09]` |
| `port` | **port def** | ❌ `part a : FP`（FP 是 port def）→ `A port must be typed by port definitions.` `[U34]` |

**正确写法**：

```sysml
attribute def Temperature;              // ✅ [X03]  attribute def
attribute t : Temperature;              // ✅         attribute 由 attribute def 定型

item def Fuel;                          // ✅ [X04]  item def（属于 occurrence def）
port def P { out item f : Fuel; }       // ✅         item 由 item def 定型

port def PowerPort {                    // ✅ [X05]  端口内用 attribute 表达量
    out attribute power :> ISQ::power;
}
```

> ⚠️ **这条规则价值极高**：AI 常把 `part`/`item` 和 `attribute` 混用，例如"质量"写成 `part mass : Real`，或"功率"写成 `item power : HeatFlow`（而 HeatFlow 是 attribute def）。**先问"这个东西是实物还是量值"**：实物/事件 → `part`/`item`/`occurrence`；量值 → `attribute`。

### 3.2 不能 `attribute def X :> ISQ::某量`

```sysml
attribute def Temperature :> ISQ::thermodynamicTemperature;   // ❌ [W09] Couldn't resolve reference to Classifier 'ISQ::thermodynamicTemperature'
attribute inletTemp :> ISQ::thermodynamicTemperature;         // ✅ [X06] 在 usage 上子集化
```

**原因**：`ISQ::thermodynamicTemperature` 是一个**特征（usage）**，不是分类器（definition）。`:>` 作用于已声明的特征，`attribute def` 需要的是分类器。

**正确做法**（推荐）：

```sysml
// 不要包装 ISQ 量，直接在 usage 上子集化
part def BatteryPack {
    attribute cellTempMax :> ISQ::thermodynamicTemperature;      // ✅ [X06]
    attribute cellTempAllowed :> ISQ::thermodynamicTemperature;
    attribute heatGeneration :> ISQ::power;
}
```

### 3.3 "引用"类语法必须引用 **usage**，不能引用 **definition**

实测三条同源证据：

| 写法 | 结果 |
|---|---|
| `satisfy R by v;`（`R` 是 `requirement def`） | ❌ `Must reference a requirement.` `[U02]` |
| `satisfy r by v;`（`r` 是 `requirement r : R;`） | ✅ `[U03]` |
| `perform action p references A;`（`A` 是 `action def`） | ❌ `Must reference an action.` `[V07]` |
| `perform action p references a1;`（`a1` 是 `action a1 : A;`） | ✅ `[V06]` |
| `exhibit state st references s1;`（`s1` 是 `state s1 : S;`） | ✅ `[V08]` |

> ⚠️ **记忆口诀**：`def` 是"图纸"，usage 是"实物"。`satisfy`/`perform`/`exhibit` 是在指"实物"，不能指"图纸"。

### 3.4 隐式特化：不写 `:>` 也有语义

官方规范明确列出（第 7 章"隐式特化"）：

```
attribute def A;     // 隐含特化 Attributes::AttributeValue
part def P {         // 隐含特化 Parts::Part
    ref x;           // 隐含子集化 Base::things
    attribute a : A; // 隐含子集化 Attributes::attributeValues
    part p;          // 隐含子集化 Parts::parts
    part q :> p;     // 无隐式特化（已显式指定）
}
part def Q :> P;     // 无隐式特化
```

**含义**：模型元素的语义由"种类"决定，不必手写与库的关联。但**一旦显式写了 `:>`，隐式的就不再生效**。

---

## 4. 各领域构造的正确写法（含反例）

### 4.1 依赖（dependency）

```sysml
dependency from R2 to R1;                        // ✅ [V35] 单个 client → 单个 supplier
dependency d1 from A, B to C, D;                 // ✅ [Y23] 多对多（实测 0 ERROR）
```

```
Dependency = 'dependency' ( Identification? 'from' )?
             client += [QualifiedName] ( ',' client += [QualifiedName] )* 'to'
             supplier += [QualifiedName] ( ',' supplier += [QualifiedName] )* RelationshipBody
```

> 用途：表达**无强类型的追溯关系**（如"需求 R2 依赖 R1"）。这是 `refines`/`traces` 的**合法替代**——虽然语义弱一些，但它是**真实存在**的语法。

### 4.2 注解：comment / doc / rep

```
Comment = ( 'comment' Identification ( 'about' Annotation ( ',' Annotation )* )? )?
          ( 'locale' STRING_VALUE )? body = REGULAR_COMMENT
Documentation = 'doc' Identification ( 'locale' STRING_VALUE )? body = REGULAR_COMMENT
TextualRepresentation = ( 'rep' Identification )? 'language' STRING_VALUE body = REGULAR_COMMENT
```

```sysml
// ✅ [W05] 正确写法
doc /* 包的文档说明 */
part def V {
    doc /* V 的定义说明 */
    attribute x : Real = 1.0;
}
comment c1 about V
    /* 对 V 的补充说明 */

// ✅ [V34] 文本表示（用于挂接外部描述，如 Mermaid/表格）
rep r1 language "text/plain"
/* 这张图描述了 V 的结构 */
```

> ⚠️ **`rep` 的 `language` 是必填的**（v1.2 用 `N01`–`N03` 定死）。产生式
> `TextualRepresentation = ( 'rep' Identification )? 'language' STRING_VALUE body = REGULAR_COMMENT`
> 里 `language <字符串>` **没有 `?`**，且必须**在注释之前**：
>
> ```sysml
> rep myRep language "text/plain" /* 正文 */   // ✅ [N02] 0 ERROR
> rep /* 正文 */                               // ❌ [N03] mismatched input '/*…*/' expecting 'language'
> rep { 正文 }                                  // ❌ [K03] 花括号体不存在
> ```
>
> ⚠️ **`doc` / `comment` 的 `locale` 也必须在注释之前**（`N04` ❌ `N05`）：
>
> ```sysml
> doc locale "en" /* 正文 */      // ✅ [N04]
> doc /* 正文 */ locale "en"      // ❌ [N05] missing RULE_REGULAR_COMMENT at '}'
> ```
>
> **记忆口诀：`doc` / `comment` / `rep` 的关键字参数（`locale` / `language`）一律写在 `/* … */` 之前。**

**❌ 反例**：

```sysml
doc "字符串说明"                          // ❌ [T33] 必须是 /* */
comment c1 about V { /* 说明 */ }         // ❌ [V33] body 不能包在 {} 里
doc //* 说明 */                           // ❌ [U21] 必须是 /* */
```

> ⚠️ **`doc` 是高频写法**（需求/状态/用例里都用），**永远跟 `/* ... */`**。

### 4.3 定义与使用（含"有效名称"规则）

```sysml
// ✅ [X03] 定义声明
part def Vehicle;
part def Automobile specializes Vehicle;         // ✅ [Z15] 关键字形式（实测 0 ERROR）
part def Truck :> Vehicle;                       // ✅ [T07] 符号形式（等价）

// ✅ 使用
part vehicle1 : Vehicle;
part engine : Engine {                           // 带体的使用（写真实成员，不是 `...`）
    attribute displacement : Real = 2.0;
}
```

**有效名称（effective name）规则** —— 无名重定义：

```sysml
part def Engine {
    part cylinders : Cylinder[2..*];
}
part def FourCylinderEngine :> Engine {
    part redefines cylinders[4];      // ✅ [T10] 无声明名，但"有效名称"是 cylinders
}
```

> ⚠️ 这是**重定义的标准手法**：新特征不写名字，用 `redefines <目标>`，其有效名称自动继承。AI 常写成 `part cylinders redefines cylinders[4]`（重复名字）——虽然也可能过，但 `part redefines cylinders[4]` 才是规范推荐写法。

**多重继承 + 重定义合并**（官方写法）：

```sysml
action def A { in a1 : Real; out a2 : Real; }
action def B { in b1 : Real; out b2 : Real; }
action def C :> A, B {
    in c1 : Real redefines a1 redefines b1;      // ✅ [V37] 一次重定义多个
    out c2 : Real redefines a2 redefines b2;
}
```

### 4.4 属性与枚举

```sysml
// ✅ [E02] 属性定义（0 ERROR）
attribute def SensorRecord {
    ref part sensor : Sensor;          // 引用一个 part
    attribute reading : Real;          // 量值属性
}
attribute massActual :> ISQ::mass;     // 子集化 ISQ 量

// ✅ [T47] 最小可用枚举（0 ERROR）
enum def Color { red; green; }
attribute c : Color = Color::red;      // 引用枚举字面量：枚举定义::字面量
```

**枚举字面量带属性（v1.3 找到了可用形式）**：

父类型必须是 **`attribute def`**，**不能是 `enum def`**：

```sysml
// ✅ [C04]/[C11]/[C12]/[G08] 0 ERROR —— 枚举字面量带属性的可用形式
attribute def Weighted {                    // ← 父类型：attribute def（不是 enum def）
    attribute color : String;               //   字面量要重定义的属性
    attribute rank : Integer;
}
enum def RiskLevel :> Weighted {
    enum low  { :>> color = "green"; :>> rank = 1; }
    enum high { :>> color = "red";   :>> rank = 3; }
}
attribute lv : RiskLevel = RiskLevel::high; // 引用枚举字面量：枚举定义::字面量
```

**三条边界（全部实测）**：

| 父类型的形态 | 结果 | 依据 |
|---|---|---|
| **`attribute def`** | ✅ **可用** | `C04`/`C11`/`C12`/`G08` |
| 另一个 **`enum def`** | ❌ `A variation must not specialize another variation` | `C01`/`V14` |
| 在 `enum def` 体内直接写 `attribute` | ❌ 语法错 `no viable alternative at input 'attribute'` | `C03`/`E01` + §8.2.2.10 产生式 |

> ⚠️ 第二条的报错文案**极易误导**：它说「变体不得特化变体」，但根因是**实现把 `enum def` 之间的特化
> 当成了变体（variation）关系**。遇到这条报错时**不要把模型改成 variation**，而应把父类型换成 `attribute def`。
>
> 📎 **v1.2 时这里记的是「仍未覆盖」**（当时只确认官方示例形式会报错，没找到替代写法）。
> v1.3 用 `C04` 找到了上面的可用形式，`C11`（多字面量 + 多属性）、`C12`（属性类型是另一个枚举）、
> `G08`（引用 `Level::high`）三种扩展写法均 0 ERROR。


### 4.5 发生 / 时间切片 / 快照 / 个体

```sysml
// ✅ 发生定义
occurrence def Flight {
    ref part aircraft : Aircraft;
    timeslice preflight;                   // 时间切片
    timeslice inflight;
}
part aircraft : Aircraft {
    snapshot part aircraftTakeOff;         // 快照
}

// ✅ 个体（具体实例）
individual def Flight_248 :> Flight;
individual flightRecord : Flight_248 {
    individual part redefines aircraft : TestPlane_1;
    individual timeslice redefines preflight;
}

// ✅ [Y15] 个体 / 时间切片 / 快照的嵌套（0 ERROR 实证形式）
part def Vehicle {
    attribute position :> ISQ::length;
}
part def Context {
    ref part v : Vehicle;
}
individual def Context_1 :> Context;         // ← 关键：个体必须由 individual def 定型
individual def Veh_1 :> Vehicle;             // ← 普通 part def 不行
individual a : Context_1 {
    timeslice t0_t1 {
        snapshot t0 : Veh_1 {
            attribute :>> position = 0 [m];  // 匿名重定义赋值
        }
    }
}
```

**两条硬规则**（实测）：

| 规则 | 实测 |
|---|---|
| `individual` **必须由 `individual def` 定型**（用普通 `part def` 报 `An individual must be typed by one individual definition.`） | ❌ `[Y14]` / ✅ `[Y13]` |
| `individual def X :> Y;` 之后，`individual i : X;` 才合法；`timeslice` / `snapshot` 作为 `individual` 体的成员 | ✅ `[Y15]` 0 ERROR |

> ⚠️ **v1.1 修正**：v1.0 这里把该块标注为 `✅ [X07]`，但 **X07 原文（`tests5/X07_ev_tms_full.sysml`，181 行）里
> 完全是另一个模型**，只有 `attribute :>> tempAllowed = 328 [K];` 一行相关，**根本不含 `individual` / `timeslice` / `snapshot`**。
> 那份引证是错的，已换成 `[Y15]` 的实证形式。

> ⚠️ `:>> prop = value;` 是**匿名重定义赋值**，个体/快照里大量使用。

### 4.6 项 / 部件

```sysml
item def Fuel {
    attribute pressure : PressureValue;
    ref item impurities[0..*] : Material;
}
part def Vehicle {
    ref part driver[0..1] : Person;
    part engine : Engine;
    part wheels[4] : Wheel;
    part cylinders : Cylinder[4..8] ordered;
}
```

### 4.7 端口与连接

```sysml
// ✅ [X07][V20] 端口定义
port def CoolantOutletPort {
    out attribute flowRate :> ISQ::volumeFlowRate;      // 量值出口
    out item coolant : CoolantFlow;                     // 实物出口（CoolantFlow 必须是 item def）
}
port def CoolantInletPort {
    in attribute flowRate :> ISQ::volumeFlowRate;
}

part def BatteryThermalSystem {
    part pump : CoolantPump;
    part pack : BatteryPack;
    connect pump.discharge to pack.coolantIn;           // ✅ [T13] 唯一合法的连接语句
}
```

**共轭端口**（`~`，用于"进/出方向相反"的配对）——v1.2 用 `W01`–`W09` 把**合法位置表**测全了：

```sysml
item def Fuel;
port def FuelingPort {
    out item fuel : Fuel;          // ⚠️ item 必须由 item def 定型，不能写 `: Real`
}

// ✅ 合法：特征位置（part def / part usage / 顶层 port usage 都行）
part def Tank  { port inPort : ~FuelingPort; }                 // ✅ [W01]
part tank1     { port inPort : ~FuelingPort; }                 // ✅ [W02]
port topPort   : ~FuelingPort;                                 // ✅ [W03]

// ✅ 合法：接口的端
interface def FuelingInterface {
    end fuelOutPort : FuelingPort;
    end fuelInPort  : ~FuelingPort;                            // ✅ [W06]
    flow of Fuel from fuelOutPort.fuel to fuelInPort.fuel;
}

// ✅ 合法：连接的两端
part a { port p1 : FuelingPort; }
part b { port p2 : ~FuelingPort; }
connect a.p1 to b.p2;                                          // ✅ [W07]
connect b.p2 to a.p1;                                          // ✅ [W08]（共轭在左侧也行）

// ✅ 合法：定义位的「特化」写法
port def FuelingInPort :> ~FuelingPort;                        // ✅ [W05]

// ❌ 非法：定义位的「定型」写法
port def FuelingInPort2 : ~FuelingPort;                        // ❌ [W04] no viable alternative at input ':'
```

> ⚠️ **v1.2 修正（v1.1 说得不够准）**：v1.1 写的是"共轭不能在定义处、只能在特征位置"。
> 实测后更准确的说法是：**`~` 只能出现在"类型引用 / 端引用"的位置**，即
> `port x : ~P;`、`end e : ~P;`、`connect` 的端、以及 `port def Q :> ~P;`。
> **唯独 `port def Q : ~P;` 这一种（定义位 + `:`）非法** —— 因为 `port def` 的 `:` 后要求**分类器**。

**接口**（成对端口的封装）：

```sysml
// ✅ 接口定义：体内用 end 声明两端，可有 flow
interface def FuelingInterface {
    end fuelOutPort : FuelingPort;
    end fuelInPort  : ~FuelingPort;
    flow of Fuel from fuelOutPort.fuel to fuelInPort.fuel;
}

// ✅ 接口用法（三种合法形态，必须有 ≥2 个端）
interface fuelInterface : FuelingInterface connect tank.inPort to engine.inPort;   // ✅ [V01]/[V02] 二元，无括号
interface connect tank.inPort to engine.inPort;                                   // ✅ 无类型
interface (tank.inPort, engine.inPort);                                           // ✅ [V03] n 元，逗号+括号

// ✅ 写在部件体内也可以
part def V {
    interface wire : FuelingInterface
        connect tank.inPort to engine.inPort;
}

// ❌ 非法
interface bad : FuelingInterface;                         // ❌ [V05] Must have at least two related elements
interface bad2 : FuelingInterface connect (a.p to b.q);   // ❌ [V04] 括号里只能用逗号，不能写 to
```

> ⚠️ **`connect` 后的括号是"二选一"**（§8.2.2.14.2）：
> `connect A to B`（二元，**无括号**）**或** `connect (A, B, C)`（n 元，**逗号 + 括号**）。
> **不要混写** `connect (A to B)` —— 报 `mismatched input 'to' expecting ','`。

**❌ 反例（AI 高频错误）**：

```sysml
connector c1 from a.p1 to b.p2;      // ❌ [T14] no viable alternative at input 'c1' / 'from' / 'to'
connector from a.p1 to b.p2;         // ❌ 同上（SysML v2 不收 KerML 的 connector from...to）
```

> ⚠️ **必须用 `connect A to B;`**。注意：KerML 规范 §8.2.5.5.1 确实定义了 `connector from A to B` 的产生式，但 **SysML v2 实现里不接受**——这是"规范与实现不一致"的又一实例。

**绑定连接**（断言两个特征代表同一个事物）：

```sysml
bind shaftPort_d = differential.shaftPort_d;      // ✅
bind engine.fuelCmdPort = fuelCmdPort;
binding b1 bind a.x = b.y;                        // 带名字的形式
```

### 4.8 动作、流、后继

```sysml
// ✅ [X07] 动作定义
action def MonitorTemperature {
    out tempMeasured :> ISQ::thermodynamicTemperature;
}
action def ControlPump {
    in tempCmd :> ISQ::thermodynamicTemperature;
    out speedCmd :> ISQ::angularVelocity;
}

// ✅ 动作使用 + 后继 + 流
action def ThermalControlLoop {
    attribute overTemp : Boolean;
    action monitor : MonitorTemperature;
    action control : ControlPump;
    action alarm : TriggerAlarm;

    first monitor then control;                    // ✅ [T15] 顺序
    first control if overTemp then alarm;          // ✅ [B5] 带守卫的顺序
    flow monitor.tempMeasured to control.tempCmd;  // ✅ [T11] 流（无 from）
    flow from monitor.tempMeasured to control.tempCmd;  // ✅ [T12] 流（带 from，等价）
}
```

**后继（succession）的合法与非法形式**（这是实测挖得最深的一块）：

| 写法 | 实测 | 用例编号 | 结论 |
|---|---|---|---|
| `first a1 then a2;` | ✅ | `[T15]` | 合法（最常用） |
| `succession s1 first a1 then a2;` | ✅ | `[U12]` | 合法（具名 succession） |
| **`first a1 if cond then a2;`** | ✅ | `[U11]` | **合法！带守卫** |
| `succession s1 first a1 if cond then a2;` | ✅ | `[U12]` | 合法（具名 + 守卫） |
| **`if cond then a2;`** | ✅ | `[U08]` | **合法！条件触发** |
| `first a1 else a2;` | ❌ | `[U07]` | 非法 |
| `first a1 if cond then a2 else a3;` | ❌ | `[U09]` | 非法（`else` 是元凶） |
| `first a1 when cond then a2;` | ❌ | `[U10]` | 非法（`when` 不存在） |
| `then if cond then a1;` | ❌ | `[U13]` | 非法 |
| `first a1 then a2 if cond;` | ❌ | `[U14]` | 非法（守卫必须在 `then` **前**） |
| `if cond then a2 else a3;` | ❌ | `[U15]` | 非法（同样是 `else`） |
| `decide d1; first d1 if cond then a1;` | ✅ | `[U16]` | 合法（分叉 + 守卫） |

**`else` 的准确边界**（v1.2 用第十三轮的 `E01`–`E06` 定死，此前 v1.1 说"任何位置都不合法"**过严**）：

| 写法 | 实测 | 用例 | 结论 |
|---|---|---|---|
| `if c { action x1; } else { action x2; }` | ✅ | `E02` | **合法** —— `else` 唯一的合法形态：`IfActionUsage` 的 else 分支 |
| `first b1 then b2;` 后接 `else then b3;` | ❌ | `E01` | 非法（`no viable alternative at input 'else'`） |
| `first b1 if c then b2;` 后接 `else then b3;` | ❌ | `E03` | 非法 |
| `decide d1; first d1 if c then b1;` 后接 `else then b2;` | ❌ | `E04` | 非法 |
| `first b1 if c then b2;`（单条守卫后继） | ✅ | `E05` | 合法 |
| `else then b1;` 放在 `first` 之前 | ❌ | `E06` | 非法 |

> ⚠️ **结论（v1.2 收窄）**：
> 1. **`if` 守卫合法**，但**只能出现在 `first X` 与 `then Y` 之间**（`GuardedSuccession` 的 `GuardExpressionMember`）。
> 2. **`else` 只能用作 `if {...} else {...}` 动作语句的 else 分支**；**不能**用作后继（succession）的兜底分支。
>    规范 §8.2.2.17.8 里确实有 `DefaultTargetSuccession = 'else' ...` 产生式，但**实现不接受** —— 这是本校验器的
>    一处"语法有、实现无"。需要"二选一"时：用 `if/else` 动作块，或用 `decide` + 两条带 `if` 的 succession，或用约束表达。
>
> ⚠️ **v1.1 修正留存**：v1.0 本表用的是 `[B1]`–`[B10]`。**这些不是文件编号**——它们只是第二轮用例
> `U07`–`U16` 的**描述前缀**。读者按 `B5` 去 `tests*/` 目录是**找不到文件的**，已换回真实编号。核对表见 `tmp/v2spec/_ledger.txt`。
>
> ⚠️ **本条还纠正了两次误判**：① 曾把 `first a1 if cond then a2;` 判为"v1 风格语法错误"（实际**合法**）；
> ② 曾把 `else` 判为"任何位置都不合法"（实际 `if/else` 动作块里**合法**）。

**其他动作构件**（⚠️ 这些是**语句**，必须写在动作体里，不能裸放包体顶层，否则报 `mismatched input 'send'/'accept'/'assign' expecting '}'`，实测 `BLK33`）：

```sysml
item def Request;
item def Response;
item def SensorReading;
part controller;
attribute level : Real = 0.0;
attribute threshold : Real = 1.0;

action def Exchange {                                 // ✅ [Y07] 0 ERROR（send/accept 在体内）
    send new Request() via controller;                // 发送
    accept Response via controller;                    // 接收
}
action def Sensing {                                  // ✅ [Y17] 0 ERROR（三种触发形式）
    accept reading : SensorReading;                   // 具名接收
    accept when level > threshold;                    // 条件触发  ✅ [W07]
    accept after 30 [s];                              // 延时触发（需 import SI::*）  ✅ [W07]
}
action def Timed {
    // 定时触发（需 private import Time::*）
    accept at Iso8601DateTime("2024-02-01T00:00:00Z");    // ✅ [W08]
}
action def Counter2 {                                 // ✅ [Y06] 赋值
    attribute count : Natural = 0;
    assign count := count + 1;
}
action def Stopping { action stop terminate; }         // 终止
perform action p references a1;                        // ✅ [V06] 执行（必须引用 usage）
```

> ⚠️ **两处易错点**（v1.1 补测得出）：
> 1. `accept after 30 [s];` 里的单位 `[s]` **必须 `import SI::*`**；只写 `accept after s;`（裸标识符）
>    会报 `An after expression must be a DurationValue` + `Couldn't resolve reference to Element 's'`（实测 `U40`）。
> 2. `accept at Iso8601DateTime(...)` 里 `Iso8601DateTime` **必须 `import Time::*`**，
>    否则报 `Couldn't resolve reference to Element 'Iso8601DateTime'`。注意它是**类型引用**，不是普通函数调用。

### 4.9 状态与状态机

**状态定义的完整结构**（官方 A.7 形式）：

```sysml
// ✅ [X07] 实测通过
state def ThermalStates {
    attribute pumpRunning : Boolean;
    state initial;                        // ⚠️ 起点建议显式声明
    state standby;
    state cooling {
        entry action initCooling : InitCooling;      // 入口动作
        do action regulate : Regulate;               // 持续动作
        exit action stopCooling : StopCooling;       // 出口动作
        constraint { cellTemp <= 328 [K] }           // 状态约束
    }
    state faulted;

    transition i2s first initial then standby;
    transition 'standby-cooling'
        first standby
        accept IgnitionOn                 // 触发器
        if pumpRunning                    // 守卫
        then cooling;
    transition 'cooling-faulted'
        first cooling
        accept OverTempSignal
        do send new OverTempSignal() to controller   // 效果
        then faulted;
}
```

**transition 的语法骨架**（按实测归纳）：

```
transition [<名字>] [first <源状态>] [accept <触发器>] [if <守卫>] [do <效果>] then <目标状态> ;
```

| 形式 | 实测 |
|---|---|
| `transition initial then off;` | ✅ `[U01]`（源状态必须在 `first` 位置，或无 `first` 时直接置于 `transition` 后） |
| `transition t1 first off then on;` | ✅ `[T19]` |
| `transition t1 first off accept Sig then on;` | ✅ `[T19]` |
| `transition t1 first off accept Sig if armed then on;` | ✅ `[T20]` |
| `transition t1 first off accept Sig do send new Sig() to ctrl then on;` | ✅ `[T19]` |
| `transition initial toOff first off then off;` | ❌ `[V22]` 名字与源混写 |

> ⚠️ **`transition` 里 `first X` 的 `X` 必须是已声明的状态**。官方 A.7 写 `transition initial then off;` 却没在体内声明 `initial`，**直接抄会导致 `Couldn't resolve reference to Element 'initial'`**（实测 `[W03]`）。**务必显式写 `state initial;`**。
>
> ⚠️ **带连字符/空格的状态名必须用单引号**：`transition 'off-starting' ...` ✅

**其他状态构件**：

```sysml
state def VehicleStates parallel {       // ✅ `parallel` 修饰符合法
    state OperationalStates;
    state HealthStates;
}
exhibit state operatingState references st1;   // ✅ [V08] 展现（必须引用 state usage）

state def SimpleStates {                 // ✅ [Y08] 0 ERROR
    state off;
    state on;
    entry; then off;                     // 简写：入口后直接转（必须在 state 体内）
}
```

### 4.10 计算与约束

```sysml
// ✅ [X07] 计算定义
calc def PumpPower {
    in flowRate :> ISQ::volumeFlowRate;
    in pressureRise :> ISQ::pressure;
    return power :> ISQ::power = flowRate * pressureRise;
}
// 另一种写法（体作为结果表达式）
calc def Average {
    in scores[1..*] : Real;
    return : Real = sum(scores) / size(scores);     // ✅ [V03] 需 import RealFunctions + SequenceFunctions
}

// ✅ [X07] 约束定义
constraint def ThermalBalance {
    in heatGen :> ISQ::power;
    in heatRej :> ISQ::power;
    heatGen <= heatRej                               // 体即布尔表达式，无 return
}

// ✅ [X06] 约束使用
part def BatteryPack {
    attribute cellTempMax :> ISQ::thermodynamicTemperature;
    attribute cellTempAllowed :> ISQ::thermodynamicTemperature;
    assert constraint limit { cellTempMax <= cellTempAllowed }     // ✅ [U28] 无分号
}
```

**❌ 反例**：

```sysml
assert constraint { x > 0 };        // ❌ 带分号（[T35] 同类：require constraint 带分号报 no viable alternative at input ';'）
```

> ⚠️ **`assert` / `assume` / `require constraint { ... }` 后面不写分号**。

### 4.11 需求（**TMS 领域最核心**）

```sysml
// ✅ [X07] 需求定义的完整骨架
requirement def BatteryTempRequirement {
    doc /* 电池模组最高温度不得超过允许上限。 */    // ← 文档
    subject pack : BatteryPack;                    // ← 主体（需求约束的对象）
    actor operator : Person;                       // ← 参与角色
    stakeholder owner : Person;                    // ← 利益相关方
    attribute tempMeasured :> ISQ::thermodynamicTemperature;   // ← 需求参数
    attribute tempAllowed :> ISQ::thermodynamicTemperature;
    assume constraint { tempAllowed > 0 [K] }      // ← 假设（前提）
    require constraint { tempMeasured <= tempAllowed }   // ← 要求（必须满足）
}

// ✅ 需求组（含子需求 + 短名）
requirement def ThermalSystemRequirements {
    subject sys : BatteryThermalSystem;
    requirement <'1'> tempReq : BatteryTempRequirement {
        subject pack = sys.pack;                   // 绑定主体
        attribute :>> tempAllowed = 328 [K];       // 重定义参数
    }
    requirement <'2'> pumpPowerReq {
        doc /* 冷却液泵功耗不得超过 300 W。 */
        attribute powerLimit :> ISQ::power;
        require constraint { powerLimit <= 300 [W] }
    }
}

// ✅ 满足关系（必须引用 usage）
part tms : BatteryThermalSystem;
requirement sysReqs : ThermalSystemRequirements;
satisfy sysReqs by tms;                            // ✅ [W01]

// ✅ 内联声明形式
satisfy requirement massGroup : MaxMass by vehicle1;   // ✅ [W02]

// ✅ 不满足（反例需求）
not satisfy massGroup by vehicle2;                 // ✅
```

**实测确认的 satisfy 约束**：

| 写法 | 实测 |
|---|---|
| `satisfy R by v;`（`R` 是 requirement **def**） | ❌ `Must reference a requirement.` `[U02]` |
| `satisfy r by v;`（`r` 是 requirement **usage**） | ✅ `[U03]` |
| `satisfy requirement r : R by v;` | ✅ `[W02]` |
| `satisfy r : R by v { ... }`（无 `requirement` 前缀但带体） | ❌ `Couldn't resolve reference to Feature 'r'` `[T22]` |
| `requirement r : R { subject = x; }` **之后**再写 `satisfy r by x.y;` | ❌ `Cannot override a binding feature value` `[S01]` |

> ⚠️ **v1.3.1 新增（真机实测发现，2026-09-19）**：**`subject = …` 与 `satisfy … by …` 对同一个 requirement 只能二选一。**
> requirement usage 体里一旦显式绑定了 `subject`，再写 `satisfy r by <路径>;` 给它挂满足关系，就会报
> **`Cannot override a binding feature value`**（一条 `satisfy` 报一个 ERROR）。
> 危险之处在于**它看起来完全正常**：`satisfy coolingCapacityReq by thermalSystem.coldPlate;` 肉眼挑不出任何毛病，
> 只有真跑校验器才会暴露 —— 真机实测里 AI 一个模型就踩了 **7 条**，而语法错是 0（属语义路）。
>
> ```sysml
> // ❌ 冲突：subject 已绑，又用 satisfy 去绑 —— 实测 7 条 ERROR 全部出自这种写法
> requirement coolingCapacityReq : CoolingCapacityRequirement { subject = thermalSystem; }
> satisfy coolingCapacityReq by thermalSystem.coldPlate;
>
> // ✅ 推荐：让 satisfy 负责绑定，不写 subject —— 实测 0 ERROR
> requirement coolingCapacityReq : CoolingCapacityRequirement;
> satisfy coolingCapacityReq by thermalSystem.coldPlate;
>
> // ✅ 或者：只绑 subject，完全不用 satisfy
> requirement coolingCapacityReq : CoolingCapacityRequirement { subject = thermalSystem; }
> ```
>
> ⚠️ **不要靠「去掉限定名前缀」绕过**：改成 `satisfy r by coldPlate;` 会报
> `Couldn't resolve reference to Element 'coldPlate'`（`coldPlate` 只在 `part def BatteryThermalSystem { … }` 体内），
> **ERROR 不但没消，反而从 7 涨到 14**。真因不是限定名，是 subject 双绑。
> 证据文件（可复跑）：`tmp/v2docs/_ab_A_原样.sysml`（7 ERROR）/ `_ab_B_去限定名.sysml`（14）/ `_ab_D_去subject绑定.sysml`（**0**）。

> ⚠️ **`subject` / `actor` / `stakeholder` 的硬规则**（v1.2 用 `U01`–`U07`、`X01`–`X03` 定死，
> **这是 v1.1 说错、且官方 Annex A 示例本身就违反的一条**）：
>
> | 情形 | 实测 | 依据 |
> |---|---|---|
> | 只写 `doc`，无 subject（`concern def C { doc /* x */ }`） | ✅ 合法 | `U01` |
> | 有 `stakeholder` **但没有** `subject` | ❌ `Subject must be first parameter.` | `U02`/`U05` |
> | 有 `actor` **但没有** `subject` | ❌ 同上 | `U03`/`U06` |
> | `stakeholder` 写在 `subject` **之前** | ❌ 同上 | `X01` |
> | `subject` 在前，之后跟 `actor` / `stakeholder` | ✅ 合法 | `O03`/`X02`/`X03` |
> | `doc` 写在 `subject` **之前** | ✅ 合法 | `G02`/`O04` |
>
> **正确写法：只要用到 `stakeholder` 或 `actor`，就必须有一个 `subject`，且 `subject` 必须写在它们之前。**
>
> ```sysml
> // ✅ 正确
> requirement def R {
>     subject s : T;
>     actor ac : T;
>     stakeholder se : T;
>     require constraint { true }
> }
>
> // ❌ 错误：有 stakeholder 无 subject
> concern def C { doc /* x */ stakeholder se : T; }     // → Subject must be first parameter.
> // ❌ 错误：stakeholder 在 subject 之前
> requirement def R2 { stakeholder se : T; subject s : T; require constraint { true } }
> ```
>
> ⚠️ 该报错信息**措辞有误导性**：其实不是"位置"问题，而是"**有 stakeholder/actor 就必须要 subject**"。
> 官方 Annex A.11 的 `concern def VehicleSafety { doc ...; stakeholder se : SafetyEngineer; }`
> **就是因此编译不过的**（`G03`）——补上 `subject` 即通过（`O03`/`R01`）。

**需求追溯与分配（官方 Annex A.8 的两种机制，v1.2 新增）**：

```sysml
// ✅ 分配：把需求分配到部件或部件的某个特征
allocate vehicleMassRequirement to PartsTree::vehicle_b.mass;   // ✅ [J03] 到特征
allocate r1 to p1;                                              // ✅ [J04] 到部件整体

// ✅ 派生：需求之间的"由...派生"关系（官方机制，替代臆造的 traces）
#derivation connection {                       // ✅ [J01] 0 ERROR（需 private import RequirementDerivation::*;）
    end #original ::> vehicleSpecification.vehicleMassRequirement;
    end #derive    ::> engineSpecification.engineMassRequirement;
}
```

> ⚠️ **`#derivation` / `#original` / `#derive` 全部来自库包 `RequirementDerivation`**。
> 不写 `private import RequirementDerivation::*;` 会报三个 `Couldn't resolve reference to Type`
> （`J02`：`'derivation'`、`'original'`、`'derive'`）。**这是 `#` 前缀元数据标注的典型用法** ——
> `#` 后面跟的是**元数据定义**，必须先导入。

> ⚠️ **`refines` / `traces` 是臆造关键字**（见 §1.8）。表达需求细化的合法替代：
>
> ```sysml
> requirement def R2 :> R1;                  // ✅ [U17] 特化（语义最强）
> requirement def R2 specializes R1;         // ✅ [U18] 等价写法
> dependency from R2 to R1;                  // ✅ [U19] 弱追溯
> requirement r2 :> r1;                      // ✅ [U20] 使用之间的细化
> #derivation connection { ... }             // ✅ [J01] 强追溯（官方机制，需导入）
> ```
>
> **实测：工程里 AI 生成的 `requirement X refines Y;` / `traces Y;` 是本项目 V2 代码出错的主因之一。**

### 4.12 用例 / 分析 / 验证

```sysml
// ✅ 用例 [V28]
use case def DriveVehicle {
    subject vehicle : Vehicle;
    actor driver : Driver;
}
use case drive : DriveVehicle {
    objective { doc /* 把乘客送达目的地。 */ }
}

// ✅ 分析 [V24]
analysis def ThermalMarginAnalysis {
    subject sys : BatteryThermalSystem;
    return marginResult :> ISQ::thermodynamicTemperature;
    objective {
        doc /* 评估热管理系统在峰值工况下的温度裕度。 */
        requirement marginReq : BatteryTempRequirement;
    }
}
analysis marginRun : ThermalMarginAnalysis {
    subject = tms;                                          // ⚠️ 绑定主体用 =，不写类型
    attribute rej :> ISQ::power = EstimateHeatRejection(tms.pack);
    return :>> marginResult = tms.pack.cellTempAllowed - tms.pack.cellTempMax;
}

// ✅ 验证 [X09]；完整可编译形式见 [E06]（0 语法错）
verification def ThermalTest;
verification thermalTests : ThermalTest {
    subject = pack1;                                        // 绑定已有实例（用 =，不写类型）
    objective {
        verify tempReq;                                     // 验证某需求（必须引用 requirement usage）
    }
    action measureTemp {
        out measured :> ISQ::thermodynamicTemperature;       // ← v1.1：真实体，不是 `...`
    }
    then action evaluate {
        in measured :> ISQ::thermodynamicTemperature;
        out verdict : Boolean = measured <= 328 [K];
    }
    flow measureTemp.measured to evaluate.measured;
    return :>> verdict = evaluate.verdict;
}
```

> ⚠️ **v1.1 修正**：v1.0 这里写成 `action measureTemp { ... }` / `then action evaluate { ... }`。
> **`...` 是非法记号**（见 §1.9 与 `[Y03]/[Y04]`），已换成真实体。`328 [K]` 需 `private import SI::*;`。

> ⚠️ **`subject = 值;` 是绑定，`subject 名 : 类型;` 是声明**。分析/验证用例里绑定已有实例用前者。
>
> ⚠️ **`subject` 必须在体内最前**（§4.11 的硬规则同样适用）：只要体内出现 `stakeholder`/`actor`，
> `subject` 就必须存在且排在其前。官方 Annex A.9 的分析案例把参数写在 `subject = vehicle_b;` 之前，
> **因此被校验器拒绝**（`X01`）；把 `subject` 提到最前即通过（`X04`）。

**`include` 与 `extend`（v1.3 补测，官方 §8.2.2.25）**：

```sysml
// ✅ [A11] 在 use case def 体内 include（三种写法均可）
use case def SubUC;
use case def MainUC {
    include use case SubUC;                 // ✅ [A11] 引用用例定义名
}

// ✅ [A01]/[A02]/[A03] 三种 include 形式
use case def AnotherUC {
    include use case sub;                   // ✅ [A01] 引入新的子用例 usage
    include use case sub2 : SubUC;          // ✅ [A02] 带类型
    include refUC;                          // ✅ [A03] 直接引用已有 usage
}
use case refUC : SubUC;

// ✅ [A12]/[A14] 在 use case usage 体内、以及多条 include
use case mainRun : MainUC {
    include use case SubUC;
    include use case SubUC2;                // ✅ 可多条
}
use case def SubUC2;

// ✅ [G07] 带特化的 include
use case def SpecialUC :> SubUC;
use case def HostUC { include use case s : SpecialUC; }
```

> ❌ **`extend` 在 SysML v2 里不存在**。`extend SubUC;` 报语法错（`A13`），且 **§8.2.2 的 341 条产生式中
> `extend` 关键字零命中** —— 它不是「写法不对」，而是**根本没有这个构造**。
> 需要表达「扩展/包含」语义时用 **`include use case`**；需要表达特化时用 **`:>`**。

> 📎 `IncludeUseCaseUsage` 的产生式（§8.2.2.25）本身就允许两种形态：
> `'include' ( OwnedReferenceSubsetting FeatureSpecializationPart? | 'use' 'case' UsageDeclaration )`
> —— 正好对应 `include <引用>;` 与 `include use case <名> [: <类型>];`。

### 4.13 变体（**实测最反直觉的一块**）

```sysml
// ✅ [X08] 唯一实测通过的结构：变体点内联
part def ThermalSystem {
    variation part cooling : CoolingSolution {          // 类型是普通 def
        variant part air : AirCooling;                  // 变体内联在花括号里
        variant part liquid : LiquidCooling;
        variant part direct : DirectCooling;
    }
}
```

**❌ 反例**：

```sysml
variation part def CoolingChoices :> CoolingSolution {   // ← 这一句本身 ✅ 合法（[W02]）
    variant part air : AirCooling;
}
part def ThermalSystem {
    variation part cooling : CoolingChoices;             // ❌ [W03]/[Z02] A variation must not specialize another variation.
}
```

> ⚠️ **v1.2 修正（v1.1 判反了）**：v1.1 把 `variation part def X :> Y { ... }` 整体判为非法，**这是错的**。
> 实测 `[W02]`：`variation part def CoolingChoices :> CoolingSolution { variant part air : AirCooling; }`
> **单独写完全合法（0 ERROR）**。
> **真正非法的只有下半句** —— **变体点由"变体定义"定型**（`variation part z : 变体def`），
> 报 `A variation must not specialize another variation.`（`W03`/`Z02`）。
>
> **记忆口诀：`variation` 的"类型"必须是普通 def，不能是另一个 variation def。**

> ⚠️ 这是**规范示例与实现不一致**的典型：官方 A.12 就是写成 `variation part def TransmissionChoices:>Transmission {...}` + `variation part transmission:TransmissionChoices;`，**但实测报错**。
>
> **实测结论**：`variation` 的**类型必须是普通定义**，变体直接内联。**照抄官方示例的"变体定义 + 变体点引用"两段式会失败。**

**变体选择约束**（合法）：

```sysml
part def System {
    variation part pump : Pump {
        variant part pumpA : PumpA;
        variant part pumpB : PumpB;
    }
    assert constraint selectionConstraint {
        (pump == pump::pumpA) xor (pump == pump::pumpB)     // ✅ 用 xor 表达互斥
    }
}
```

> ⚠️ 引用变体字面量用 `变体点名::变体名`（如 `pump::pumpA`）。

### 4.14 元数据

```sysml
// ✅ [V27]/[T01] 元数据定义与标注
metadata def ThermalMargin {
    attribute margin : Real;
    attribute unit : String;
}
part def Radiator {
    attribute area : Real = 1.0;
    @ThermalMargin {                    // 前缀标注
        margin = 0.15;
        unit = "degC";
    }
}
```

**两种写法等价**（都实测通过）：`@X { ... }` 与 `metadata X { ... }`。

```sysml
metadata def M { attribute k : String; }
part def P {
    @M        { k = "x"; }              // ✅ [T01] 前缀形式
    metadata M { k = "x"; }             // ✅ [T03] 关键字形式
}

// ✅ [T04] 顶层标注 + about
part p1;
@M about p1 { k = "x"; }

// ✅ [T05] # 前缀（# 后跟元数据定义，用于 `#derivation` 这类库元数据）
#Security part p2;

// ❌ [T02] 体内成员漏分号
part def P2 { @M { k = "x" } }          // mismatched input '}' （应为 `k = "x";`）
```

> ⚠️ **元数据用法体内的每条成员必须带分号**（`MetadataBody = '{' (…)* '}'`，成员各自以 `;` 结尾）。
> 漏写报 `mismatched input '}'`，而且**错误位置会漂移到用法块末尾**，容易误判成"标注本身不合法"。
>
> ⚠️ **`metadata` 在 verification / analysis 体内同样可用**（`F01`–`F05`、`T01`）——
> 官方 A.10 的 `metadata VerificationMethod { kind = VerificationMethodKind::test; }` 是**合法**的，
> 只需导入 `VerificationCases::*`（或自建 `metadata def`）。
>
> ⚠️ **`#derivation` / `#original` / `#derive` 也是元数据标注**（`#` + 库元数据定义），
> 必须先 `private import RequirementDerivation::*;`（见 §4.11，`J01`/`J02`）。

### 4.15 视图与视角

**关键区别：`filter` 在 view def 和 view usage 里都能用，`expose` 只能用在 view `usage` 里。**

```sysml
// ✅ 视角（关注点）—— ⚠️ 有 stakeholder 就必须有 subject 且排在其前（见 §4.11）
concern def VehicleSafety {
    subject vs : SafetyEngineer;                                     // ← 必需（[O02]/[G03]）
    doc /* Vehicle must have necessary safety features. */
    stakeholder se : SafetyEngineer;
}
viewpoint safetyViewpoint {
    subject vs : SafetyEngineer;                                     // ← 建议同样声明
    frame concern vc : VehicleSafety;
}

// ✅ 视图定义
view def EmptyView;                     // ✅ [Z07] 空体合法
view def TreeView {
    filter @Vehicle;                     // ✅ [Y19] view def 体内可以写 filter
}
view def PartsTreeView :> TreeView {
    filter @SysML::PartUsage;            // ✅ [H02] @ 用于筛选元素类型
}

// ✅ 视图使用（expose / filter / satisfy 都在这里）
view vehiclePartsTree : PartsTreeView {
    satisfy safetyViewpoint;             // ✅ [Y20] 0 ERROR
    filter @Vehicle;
    expose Vehicle::**;                  // ✅ [Y20] 暴露模型范围
}
```

**❌ 反例**：

```sysml
view def TreeView2 {
    expose Vehicle::**;    // ❌ [Y18]/[H04] mismatched input 'expose' expecting '}'
}
view def BadView {
    render asFooDiagram;   // ❌ [H05] Couldn't resolve reference to Feature 'asFooDiagram'
}
```

**`render` 的合法渲染器名（v1.2 从官方 §9.2.19 查到，共 4 个）**：

| 渲染器 | 类型 | 说明 |
|---|---|---|
| `asTreeDiagram` | `GraphicalRendering` | 树图 |
| `asInterconnectionDiagram` | `GraphicalRendering` | 互联图（内部块图） |
| `asElementTable` | `TabularRendering` | 元素表格（`columnView` 控制列） |
| `asTextualNotation` | `TextualRendering` | 文本记法 |

```sysml
private import Views::*;                 // ← 必须导入，否则渲染器名解析不到
view def TreeView {
    render asTreeDiagram;                // ✅ [H01] 0 ERROR
}
```

> ⚠️ **`render` 后必须跟"库中存在的渲染器特征名"**。写 `asFooDiagram` 报
> `Couldn't resolve reference to Feature 'asFooDiagram'`（`H05`）—— 这**不是语法错，是语义错**，
> 说明"名字可以随便写"是错的。**只写上面 4 个之一，且必须先 `import Views::*`。**

**自定义渲染器：`rendering def` + `rendering` usage（v1.3 补测，官方 §8.2.2.26.4）**

库里的 4 个渲染器只能选其一。若要定义**自己的渲染器**，规则是「**def 定类型 → usage 实例化 → render 引用 usage**」：

```sysml
private import Views::*;

rendering def TreeRenderer;                        // ✅ [B01] 只定义「类型」
rendering def TableRenderer {                      // ✅ [B02] 定义体可带属性
    attribute columns : String;
}

rendering myRenderer : TreeRenderer;               // ✅ [G03] 关键一步：建「usage」实例

part def Sys;
view def MyView {                                  // ✅ [G01]/[B11] view def 体内 render
    render myRenderer;                             //   ← render 引用的是 usage
}

view def MyViewDef;
view myView : MyViewDef {
    expose Sys;
    render Views::asTreeDiagram;                   // ✅ [G02] 限定名引用库渲染器
}
```

| 写法 | 结果 | 依据 |
|---|---|---|
| `render myRenderer;`（**usage**） | ✅ 0 ERROR | `G01`/`G03`/`G04` |
| `render Views::asTreeDiagram;`（限定名） | ✅ 0 ERROR | `G02` |
| `render TreeRenderer;`（直接引用 **`rendering def`**） | ❌ `Couldn't resolve reference to Feature 'TreeRenderer'` | `B13`/`B14` |

> ⚠️ 这与 §3.3 是**同一条底层规则**：**「引用类」语法必须引用 usage，不能引用 definition**。
> `rendering def X;` 只是一个类型，`render` 位置要的是 feature（usage）—— 所以中间那行 `rendering myR : X;` 不能省。

### 4.16 别名与包内可见性

```sysml
alias L for VeryLongName;                  // ✅ [V36] 别名
part x : L;

// ✅ [V40] 嵌套包与跨包引用
package Outer {
    package Inner {
        part def Hidden;
        part def Shown;
    }
    private import Inner::Shown;
    part x : Shown;
}
```

---

### 4.17 流与消息（`flow` / `message`）

**v1.3 新增**。官方 §8.2.2.16。基本形态是 `of <载荷>` + `from <端> to <端>`，两段都可省一部分。

```sysml
item def Coolant;
item def Msg;
port def Pt;
part def Pump { port outlet : Pt; }
part def Radiator { port inlet : Pt; }

// ✅ [D11]/[G05]/[G06] flow 定义：必须含 end（即两端）
flow def CoolantFlow {
    ref item payload : Coolant;          // 载荷（可省）
    end from_side : Pump;                // 端（必填，至少两个）
    end to_side : Radiator;
}

part def CoolingLoop {
    part pump : Pump;
    part rad : Radiator;

    // ✅ [D01] 带载荷的 flow（推荐写法）
    flow coolantFlow of Coolant from pump.outlet to rad.inlet;

    // ✅ [D02] 省 of：载荷由端类型推断
    flow plainFlow from pump.outlet to rad.inlet;

    // ✅ [D04] succession flow：带先后语义
    succession flow seqFlow of Coolant from pump.outlet to rad.inlet;

    // ✅ [D05]/[D06] message：与 flow 同族，专用于「消息」语义
    message statusMsg of Msg from pump.outlet to rad.inlet;
    message bareMsg from pump.outlet to rad.inlet;
}
```

**硬规则（实测）**：

| 规则 | 结果 | 依据 |
|---|---|---|
| 端必须成对写 `from X to Y` | ✅ | `D01`/`D02`/`D04`/`D05`/`D06` |
| `of <载荷>` 可省 | ✅ | `D02`/`D06` |
| **单端 `to`**（`flow f of P to b.q;`） | ❌ 语法错 | `D03`/`D14` |
| **裸 `flow def F;`**（无 `end`） | ❌ `Must have at least two related elements` | `D12`/`D07` |
| `flow`/`message` 只能写在**所属上下文体内**（part/action 体内） | ✅ | `D01`–`D06` |

> ⚠️ 规范 §8.2.2.16 的 `FlowDeclaration` 写了第二个分支 `FlowEndMember 'to' FlowEndMember`（看似允许「单端 to」），
> 但**实测不成立** —— 这又是一条「产生式有、实现不收」（§0.1 那类）。**成对写 `from … to …` 最稳。**

---

## 5. 错误对照表（AI 高频错误 × 正确写法 × 实测依据）

**按「发生频率」排序。第 1–6 条是 AI 生成 SysML v2 代码时的主要失分点。**
第 39–46 条为 v1.3 从第 17–19 轮补测新增。
**第 47 条为 v1.3.1 真机实测新增** —— 它不是人工构造的用例，而是 AI 在真实建模会话里**自己写出来的代码**踩的坑
（会话 350，`satisfy` 与 `subject` 双绑，一模型 7 条 ERROR，语法错 0）。详见 §4.11。

| # | ❌ 错误写法 | ✅ 正确写法 | 依据 |
|---|---|---|---|
| 1 | `import ScalarValues::*;` | `private import ScalarValues::*;` | `[T02]` 可见性前缀必填 |
| 2 | `requirement R2 refines R1;` | `requirement def R2 :> R1;` | `[T08]` `refines` 不存在 |
| 3 | `requirement R2 traces R1;` | `dependency from R2 to R1;` | `[T09]` `traces` 不存在 |
| 4 | `connector c from a to b;` | `connect a to b;` | `[T14]` |
| 5 | `satisfy SomeRequirementDef by part;` | 先 `requirement r : Def;`，再 `satisfy r by part;` | `[U02]` 必须引用 usage |
| 6 | `first A if C then B else D;` | 用 `if C { action X; } else { action Y; }` 动作块，或 `decide` + 两条带 `if` 的 succession | `[U09]` `else` **不能作后继兜底**（但 `if/else` 动作块合法，`E02`） |
| 7 | `a && b` / `a \|\| b` | `a & b` / `a or b` | `[T27]` |
| 8 | `doc "说明"` | `doc /* 说明 */` | `[T33]` |
| 9 | `require constraint { ... };` | `require constraint { ... }` | `[T35]` 无分号 |
| 10 | `part def 电池包` | `part def BatteryPack` + `doc /* 电池包 */` | `[T05]` 裸中文非法 |
| 11 | `attribute mass : Real` 用于实物 | `part mass` 或 `attribute mass :> ISQ::mass` | §3.1 类型族匹配 |
| 12 | `item power : HeatFlow`（HeatFlow 是 attribute def） | `attribute power :> ISQ::power` | `[W09]` |
| 13 | `attribute def T :> ISQ::temperature;` | `attribute t :> ISQ::temperature;` | `[W09]` ISQ 量是特征 |
| 14 | `variation part x : SomeVariationDef;` | 变体点内联 `variation part x : NormalDef { variant ... }` | `[W03]`/`[Z02]` |
| 15 | `transition initial then off;` 但未声明 `initial` | 先写 `state initial;` | `[W03]` |
| 16 | `0 [kg]` 未导库 | `private import SI::*;` | `[V02]` |
| 17 | `sum(x)` 只导 `SequenceFunctions` | 加 `private import RealFunctions::*;` | `[V03]` |
| 18 | 文件带 UTF-8 BOM | 保存为**无 BOM** UTF-8 | `[T38]` |
| 19 | `part 2Wheel;` | `part Wheel2;` | `[V13]` 数字不能打头 |
| 20 | `perform action p references SomeActionDef;` | 引用 action **usage** | `[V07]` |
| 21 | 代码里写 `part def V { ... }` | 写真实体，或用注释 `/* 省略 */` | `[Y03]` `[Y04]` `...` 非法 |
| 22 | `item x typed by A;`（照抄 KerML） | `item x defined by A;` 或 `item x : A;` | `[Y01]` 实现只收 `defined by` |
| 23 | `port def U : ~T;` | `port p : ~T;`（特征/端位）或 `port def U :> ~T;`（特化位） | `[W04]` ❌ / `[W01]`–`[W09]` ✅ |
| 24 | `accept after s;`（裸标识符当单位） | `accept after 30 [s];` + `import SI::*` | `[U40]`/`[Y17]` |
| 25 | `individual i : SomePartDef;` | 先 `individual def X :> SomePartDef;` 再 `individual i : X;` | `[Y14]`/`[Y13]` |
| 26 | `view def V { expose M::**; }` | `expose` 放到 view **usage** 体里 | `[Y18]`/`[H04]` |
| 27 | `assign x := 1;` 裸放包体顶层 | 放进 `action def` / `state` 体内 | `BLK12`/`[Y06]` |
| **28** | `concern def C { stakeholder se : T; }`（有 stakeholder 无 subject） | 加 `subject s : T;` 且写在 `stakeholder` **之前** | `[U02]`/`[U03]`/`[X01]` |
| **29** | `rep /* 文本 */`（无 `language`） | `rep r1 language "text/plain" /* 文本 */` | `[N03]`/`[K01]` |
| **30** | `doc /* 文本 */ locale "en"` | `doc locale "en" /* 文本 */`（关键字参数在前） | `[N05]` |
| **31** | `interface i : IF;`（无端） | `interface i : IF connect a.p to b.q;` 或 `interface (a.p, b.q);` | `[V05]`/`[P04]` |
| **32** | `interface i connect (a.p to b.q);` | 二元不写括号：`connect a.p to b.q`；n 元用逗号：`connect (a.p, b.q)` | `[V04]` |
| **33** | `@M { k = "x" }`（体内漏分号） | `@M { k = "x"; }` | `[T02]` |
| **34** | `#derivation connection { ... }` 未导库 | 加 `private import RequirementDerivation::*;` | `[J02]`/`[J01]` |
| **35** | `render asFooDiagram;`（自造渲染器名） | 只用 `asTreeDiagram`/`asInterconnectionDiagram`/`asElementTable`/`asTextualNotation` + `import Views::*` | `[H05]`/`[H01]` |
| **36** | `enum def X { attribute c : Color; }`（`enum def` 体内写 `attribute`） | `enum def` 体内**只能**有枚举字面量与注释类元素 | `[E01]` + §8.2.2.10 产生式 |
| **37** | `part def P { port a : ~Q; }` 里 `port def Q { out item x : Real; }` | `item` 必须由 `item def` 定型：先 `item def X;` 再 `out item x : X;` | `[W01]`/`[Z16]` 报 `An occurrence, item or part must be typed by occurrence definitions` |
| **38** | `part variant : P;`（拿保留字当名字） | 换名（如 `variantSel`） | `[M01]` |
| **39** | `enum def L :> 另一个 enum def { enum low { :>> p = v; } }` | 父类型改用 **`attribute def`**：`attribute def W { attribute p : String; }` + `enum def L :> W { enum low { :>> p = "x"; } }` | `C01` ❌ / `C04`/`C11`/`C12` ✅ |
| **40** | `extend SubUC;`（SysML v1 的用例扩展） | **v2 没有 `extend`**；包含语义用 `include use case SubUC;`，特化用 `:>` | `A13` ❌ + §8.2.2 产生式零命中 |
| **41** | `render MyRendererDef;`（引用 rendering **def**） | 先建 usage：`rendering myR : MyRendererDef;` 再 `render myR;` | `B13`/`B14` ❌ / `G01`/`G03` ✅ |
| **42** | `flow f of P to b.q;`（单端 `to`） | 端写全：`flow f of P from a.p to b.q;` | `D03`/`D14` ❌ / `D01`/`D02` ✅ |
| **43** | `flow def F;`（无端） | 定义体给端：`flow def F { end a : A; end b : B; }` | `D12`/`D07` ❌ / `D11`/`G05` ✅ |
| **44** | `part def C :> A unions B;`（照抄 KerML） | **实现不收**（见 §0.1 第 10 项）；改写成 `part def C :> A, B;` 或注释表达语义 | `E11`–`E14`/`E18` ❌ / `E17` ✅ |
| **45** | `part x featured by A;`（照抄 KerML） | **实现不收**；用 `part x : A;`（定型）或 `:>`（子集化）表达 | `E15`/`E16` ❌ |
| **46** | `metadata m typed by MD;`（照抄 KerML） | `metadata m : MD;`（一律用 `:`） | `F11`/`F01`/`F03` ❌ / `F12`/`F02` ✅ |
| **47** | `requirement r : R { subject = x; }` 之后再写 `satisfy r by x.y;` | **二选一**：要么不写 `subject`（让 `satisfy` 绑），要么只写 `subject`（不用 `satisfy`） | `[S01]` `Cannot override a binding feature value`；§4.11 |

---

## 6. 可直接复制的模板集

模板分**两级**，用之前先看清是哪一级（v1.1 修正：v1.0 曾笼统宣称"全部实测通过"，这个说法不准确）：

| 级别 | 特征 | 能否直接编译 | 本文档位置 |
|---|---|---|---|
| **A 级 · 完整可编译** | 无占位符、自带全部导入 | ✅ 直接过校验器 | §6.1、§7 |
| **B 级 · 骨架** | 含 `<...>` 占位符 | ❌ **必须先把占位符换成真实标识符** | §6.2 – §6.6 |

> ⚠️ **B 级模板不能直接送校验器**：`<需求名>` 这类占位符会报
> `no viable alternative at character '需'`（实测 `BLK47`–`BLK51`，共 5 个模板块全部命中）。
>
> 但每个 B 级模板都有**"占位符替换完之后的真实版本"**躺在 `tests*/` 里，见每节末尾的证据编号 ——
> 想抄可直接编译的版本，就去看那些文件。

### 6.1 包与导入骨架（每个文件的开头）

```sysml
package MyModel {
    private import ScalarValues::*;        // 基础类型：Real/Integer/Boolean/String
    private import RealFunctions::*;       // sum 等实数函数
    private import SequenceFunctions::*;   // size 等序列函数
    private import SI::*;                  // 单位：[kg] [m] [s] [K] [W]

    // ... 模型内容
}
```

> ⚠️ 按需增删导入。**不要导入不存在的库**（会报 `Couldn't resolve`）。
>
> 📎 **实证**：此骨架 0 语法错（`BLK46`）。但它内容是空的，只是"导入区"，**别把 `// ... 模型内容` 换成 `...`**（见 §1.9）。

### 6.2 需求模板

```sysml
requirement def <需求名>Requirement {
    doc /* 需求的自然语言描述。 */
    subject <主体名> : <主体类型>;
    stakeholder <相关方名> : <相关方类型>;
    attribute <参数名> :> <ISQ量>;
    assume constraint { <前提条件> }
    require constraint { <必须满足的条件> }
}
```

> 📎 **实证**：占位符替换后的真实版本 → `tests4/W01_req_template.sysml`（0 ERROR）。
> ⚠️ 别忘了配套的**满足关系**必须引用 **usage**：`requirement r : <需求名>Requirement; satisfy r by <部件使用>;`（§4.11、`[W01]`）。
> ⚠️ 只依赖 `subject`/`stakeholder` 还不够 —— `actor` 也是合法成员（见 §4.11）。
> ⚠️ **顺序不能改**：**有 `stakeholder`/`actor` 时，`subject` 必须存在且写在它们之前**（`[U02]`/`[U03]`/`[X01]`）。
> `doc` 放在 `subject` 之前是允许的（`[G02]`/`[O04]`）。

### 6.3 部件 + 端口 + 连接模板

```sysml
port def <设备名>OutPort {
    out attribute <量名> :> <ISQ量>;
}
port def <设备名>InPort {
    in attribute <量名> :> <ISQ量>;
}
part def <设备名> {
    attribute <量名> :> <ISQ量>;
    port <出端口名> : <设备名>OutPort;
    port <入端口名> : <设备名>InPort;
}
part def <系统名> {
    part <实例A> : <设备A>;
    part <实例B> : <设备B>;
    connect <实例A>.<出端口> to <实例B>.<入端口>;
}
```

> 📎 **实证**：占位符替换后的真实版本 → `tests3/V20_tmpl_part_port_connect.sysml`（0 ERROR）。
> ⚠️ 连接语句只有 `connect A to B;` 一种（§4.7）。**不要写 `connector ... from ... to ...`**（`[T14]`）。

**接口模板（v1.2 新增，官方 Annex A.5 形式）**：

```sysml
item def <物料名>;                              // ⚠️ item 的定型必须是 item def
port def <设备名>Port {
    out item <物料名>Item : <物料名>;
}
interface def <接口名> {
    end <出端名> : <设备名>Port;
    end <入端名> : ~<设备名>Port;                // 反向端用共轭
    flow of <物料名> from <出端名>.<物料名>Item to <入端名>.<物料名>Item;
}
part <设备A> { port <端口A> : <设备名>Port; }
part <设备B> { port <端口B> : ~<设备名>Port; }
interface <接口实例> : <接口名> connect <设备A>.<端口A> to <设备B>.<端口B>;
```

> 📎 **实证**：占位符替换后的真实版本 → `tests14/R02` 的接口定义部分 + `tests15/V02`（0 ERROR）。
> ⚠️ 接口**用法必须有 ≥2 个端**（`[V05]`）；`connect` 后二元的**不写括号**、n 元的**用逗号**（`[V04]` 反证）。

### 6.4 动作模板（含顺序与守卫）

```sysml
action def <动作名> {
    in <输入名> :> <ISQ量>;
    out <输出名> :> <ISQ量>;
}
action def <流程名> {
    attribute <条件名> : Boolean;
    action <步骤A> : <动作A>;
    action <步骤B> : <动作B>;
    action <异常处理> : <动作C>;

    first <步骤A> then <步骤B>;
    first <步骤B> if <条件名> then <异常处理>;
    flow <步骤A>.<输出名> to <步骤B>.<输入名>;
}
```

> 📎 **实证**：占位符替换后的真实版本 → `tests3/V21_tmpl_action.sysml`（0 ERROR）。
> ⚠️ `first ... if ... then ...` 合法；**`else` 不能作后继兜底**（§4.8、`[E01]`）——
> 要"二选一"就用 `if <条件> { action X; } else { action Y; }` 动作块（`[E02]` 合法）。
> `<步骤X>` 必须是**已声明的 action usage**。

### 6.5 状态机模板

```sysml
state def <状态机名> {
    attribute <条件名> : Boolean;
    state initial;                  // ⚠️ 必须显式声明
    state <状态A>;
    state <状态B> {
        entry action <入口动作>;
        do action <持续动作>;
        exit action <出口动作>;
    }

    transition i2a first initial then <状态A>;
    transition 'a-b'
        first <状态A>
        accept <触发信号>
        if <条件名>
        do send new <信号>() to <目标>
        then <状态B>;
}
```

> 📎 **实证**：占位符替换后的真实版本 → `tests5/X01_state_fixed.sysml`（0 ERROR）。
> ⚠️ **`state initial;` 必须显式声明**，否则 `transition ... first initial ...` 报
> `Couldn't resolve reference to Element 'initial'`（`[W03]`/`[T18]`）。
> ⚠️ **`transition` 的第一个词是"名字"，不是"源状态"**：`transition initial toOff first off then off;` 会报语法错（`[V22]`）。
> 正确形式是 `transition [<名字>] first <源状态> [accept ...] [if ...] [do ...] then <目标>;`
> ⚠️ 状态名带连字符/空格时用单引号：`transition 'a-b' ...`。

### 6.6 计算 / 约束 / 分析 / 验证模板

```sysml
calc def <计算名> {
    in <输入> :> <ISQ量>;
    return <输出> :> <ISQ量> = <表达式>;
}
constraint def <约束名> {
    in <参数> :> <ISQ量>;
    <布尔表达式>
}
analysis def <分析名> {
    subject <主体> : <类型>;
    return <结果> :> <ISQ量>;
    objective {
        doc /* 分析目的 */
        requirement <需求实例> : <需求定义>;
    }
}
verification def <验证名>;
verification <验证实例> : <验证名> {
    subject = <被测实例>;
    objective { verify <需求实例>; }
    action <测量动作> { out <测量值> :> <ISQ量>; }
    then action <判定动作> { in <测量值> :> <ISQ量>; out verdict : Boolean = <判据>; }
    return :>> verdict = <判定动作>.verdict;
}
```

> 📎 **实证**（占位符替换后的真实版本）：
> `tests3/V23_tmpl_calc_constraint.sysml`（计算+约束）、`tests3/V24_tmpl_analysis.sysml`（分析）、
> `tests3/V25_tmpl_verification.sysml` 与 `tests8/E06_verification_full.sysml`（验证）—— 均 0 语法错。
> ⚠️ `<判据>` 里用到的单位需 `import SI::*`（`[E06]` 少导入时报 `Couldn't resolve reference to Element 'K'`）。
> ⚠️ `verify` 后面必须跟 **requirement usage**，不能跟 requirement def（§3.3）。
> ⚠️ **`subject` 必须在 `objective` / 其他参数之前**（`[X01]` 反证 / `[X04]` 正证）。
> ⚠️ 验证体内可以写 `metadata <元数据名> { <成员>; }`（官方 A.10 形式，`[F01]`/`[T01]`），
> 但**必须先导入**（`private import VerificationCases::*;`）或自建 `metadata def`。

---

## 7. 端到端示例：纯电热管理系统（EV TMS）完整模型

以下模型**已实测通过（0 ERROR 0 WARN）**，覆盖需求 / 部件 / 端口 / 连接 / 动作 / 状态 / 计算 / 约束 / 分析 全链路，可直接作为建模起点。

```sysml
package EVTMS {
    private import ScalarValues::*;
    private import RealFunctions::*;
    private import SequenceFunctions::*;
    private import SI::*;

    // ===== 1. 端口定义 =====
    port def CoolantOutletPort {
        out attribute flowRate :> ISQ::volumeFlowRate;
        out attribute temperature :> ISQ::thermodynamicTemperature;
    }
    port def CoolantInletPort {
        in attribute flowRate :> ISQ::volumeFlowRate;
        in attribute temperature :> ISQ::thermodynamicTemperature;
    }

    // ===== 2. 部件定义 =====
    part def BatteryPack {
        attribute cellTempMax :> ISQ::thermodynamicTemperature;
        attribute cellTempAllowed :> ISQ::thermodynamicTemperature;
        attribute heatGeneration :> ISQ::power;
        port coolantIn : CoolantInletPort;
        port coolantOut : CoolantOutletPort;
        assert constraint cellTempLimit { cellTempMax <= cellTempAllowed }
    }

    part def Radiator {
        attribute heatRejection :> ISQ::power;
        attribute airFlowRate :> ISQ::volumeFlowRate;
        port coolantIn : CoolantInletPort;
        port coolantOut : CoolantOutletPort;
    }

    part def CoolantPump {
        attribute speed :> ISQ::angularVelocity;
        attribute powerConsumption :> ISQ::power;
        port suction : CoolantInletPort;
        port discharge : CoolantOutletPort;
    }

    part def Chiller {
        attribute coolingCapacity :> ISQ::power;
        port coolantIn : CoolantInletPort;
        port coolantOut : CoolantOutletPort;
    }

    // ===== 3. 系统装配与连接 =====
    part def BatteryThermalSystem {
        part pump : CoolantPump;
        part chiller : Chiller;
        part pack : BatteryPack;
        part radiator : Radiator;

        connect pump.discharge to chiller.coolantIn;
        connect chiller.coolantOut to pack.coolantIn;
        connect pack.coolantOut to radiator.coolantIn;
        connect radiator.coolantOut to pump.suction;
    }

    // ===== 4. 动作定义 =====
    action def MonitorTemperature {
        out tempMeasured :> ISQ::thermodynamicTemperature;
    }
    action def ControlPump {
        in tempCmd :> ISQ::thermodynamicTemperature;
        out speedCmd :> ISQ::angularVelocity;
    }
    action def TriggerAlarm {
        in tempFault :> ISQ::thermodynamicTemperature;
    }

    action def ThermalControlLoop {
        attribute overTemp : Boolean;
        action monitor : MonitorTemperature;
        action control : ControlPump;
        action alarm : TriggerAlarm;

        first monitor then control;
        first control if overTemp then alarm;
        flow monitor.tempMeasured to control.tempCmd;
        flow monitor.tempMeasured to alarm.tempFault;
    }

    // ===== 5. 状态定义 =====
    item def IgnitionOn;
    item def IgnitionOff;
    item def OverTempSignal;
    action def InitCooling;
    action def Regulate;
    action def StopCooling;
    part controller;

    state def ThermalStates {
        attribute pumpRunning : Boolean;
        state initial;
        state standby;
        state cooling {
            entry action initCooling : InitCooling;
            do action regulate : Regulate;
            exit action stopCooling : StopCooling;
        }
        state faulted;

        transition i2s first initial then standby;
        transition 'standby-cooling'
            first standby
            accept IgnitionOn
            if pumpRunning
            then cooling;
        transition 'cooling-faulted'
            first cooling
            accept OverTempSignal
            do send new OverTempSignal() to controller
            then faulted;
        transition 'faulted-standby'
            first faulted
            accept IgnitionOff
            then standby;
    }

    // ===== 6. 计算与约束 =====
    calc def PumpPower {
        in flowRate :> ISQ::volumeFlowRate;
        in pressureRise :> ISQ::pressure;
        return power :> ISQ::power = flowRate * pressureRise;
    }

    constraint def ThermalBalance {
        in heatGen :> ISQ::power;
        in heatRej :> ISQ::power;
        heatGen <= heatRej
    }

    // ===== 7. 需求 =====
    requirement def BatteryTempRequirement {
        doc /* 电池模组最高温度不得超过允许上限。 */
        subject pack : BatteryPack;
        attribute tempMeasured :> ISQ::thermodynamicTemperature;
        attribute tempAllowed :> ISQ::thermodynamicTemperature;
        assume constraint { tempAllowed > 0 [K] }
        require constraint { tempMeasured <= tempAllowed }
    }

    requirement def ThermalSystemRequirements {
        subject sys : BatteryThermalSystem;
        requirement <'1'> tempReq : BatteryTempRequirement {
            subject pack = sys.pack;
            attribute :>> tempAllowed = 328 [K];
        }
        requirement <'2'> pumpPowerReq {
            doc /* 冷却液泵功耗不得超过 300 W。 */
            attribute powerLimit :> ISQ::power;
            require constraint { powerLimit <= 300 [W] }
        }
    }

    part tms : BatteryThermalSystem;
    requirement sysReqs : ThermalSystemRequirements;
    satisfy sysReqs by tms;

    // ===== 8. 分析 =====
    calc def EstimateHeatRejection {
        in pack : BatteryPack;
        return :> ISQ::power;
    }

    analysis def ThermalMarginAnalysis {
        subject sys : BatteryThermalSystem;
        return marginResult :> ISQ::thermodynamicTemperature;
        objective {
            doc /* 评估热管理系统在峰值工况下的温度裕度。 */
            requirement marginReq : BatteryTempRequirement;
        }
    }

    analysis marginRun : ThermalMarginAnalysis {
        subject = tms;
        attribute rej :> ISQ::power = EstimateHeatRejection(tms.pack);
        return :>> marginResult = tms.pack.cellTempAllowed - tms.pack.cellTempMax;
    }
}
```

**变体扩展（冷却方案选择）**：

```sysml
package EVTMSVariant {
    private import ScalarValues::*;
    private import SI::*;

    part def CoolingSolution;
    part def AirCooling :> CoolingSolution;
    part def LiquidCooling :> CoolingSolution;
    part def DirectCooling :> CoolingSolution;

    part def ThermalSystem {
        attribute ambientTemp :> ISQ::thermodynamicTemperature;
        variation part cooling : CoolingSolution {
            variant part air : AirCooling;
            variant part liquid : LiquidCooling;
            variant part direct : DirectCooling;
        }
    }
}
```

---

## 8. 自检闭环：生成代码后怎么验

### 8.1 调用校验器

```bash
# 在 mbse_system 工程根目录下执行（sysml.library 是相对路径，cwd 必须是工程根）
java -jar checker.jar -i <模型文件.sysml> [sysml.library路径]
```

**调用契约**（从字节码常量池还原，非猜测）：

```
if (!args[0].equals("-i")) → 用法错误，exit(2)
content = Files.readString(Path.of(args[1]))
if (new File("sysml.library").isDirectory()) → 加载库
parse(content) → validate() → 输出 [ERROR|WARN] stdin:<行>:<列>: <消息>
```

### 8.2 判读规则（**这四条是我踩过坑才总结出来的**）

**① 退出码可直接做门禁**

| 情况 | 退出码 |
|---|---|
| ERROR > 0 | `1` |
| ERROR = 0 | `0` |
| 参数错误 | `2` |

**② 不能用 ERROR 总数判断修复效果**

语法错会**遮蔽**语义错：删掉一处非法前缀后，原本被遮蔽的语义错会**冒出来**，导致 ERROR 总数**上升**（实测：某文件 19 → 37）。

> **必须分"语法错 / 语义错"两路计数**。语法错的典型特征是 `no viable alternative` / `mismatched input` / `extraneous input` / `missing`；语义错的典型特征是 `Couldn't resolve reference` / `Must be` / `must be`。

**③ 单文件口径有大量跨文件伪错**

单文件校验时，跨文件的引用会报 `Couldn't resolve reference to Namespace 'XXX'` —— 那些包可能就在**另一个文件里**。

> **门禁必须用"项目级合并校验"**：把所有文件合并成一个临时文件再校验，口径才准确（实测：同一工程，单文件口径 137 错，项目级合并口径只有 47 错，其中约 2/3 是伪错）。

> 📎 ② 和 ③ 里的具体数字（19→37、137→47）来自本轮 V2 校验闭环评估的工程实测，
> 原始记录见 `docs/V2代码自动校验与修复闭环-方案评估-20260918.md`。它们说明的是**方法论**，
> 具体数值会随工程变化，**不要当常数用**。

**④ 语法错未清零时，语义错基本不可信（v1.3 新增，有量化证据）**

实测（2026-09-19，`sysml_models/ev_thermal_mgmt/` 6 文件，**项目级合并**口径共 47 ERROR）：

| 语义错的构成 | 条数 |
|---|---|
| 报 **解析不到**，但该名字**确实存在于合并文本里** | **8** |
| 因上一条的类型解析失败而**连带**报出的（`A usage must be typed by definitions` 等） | **6** |
| 名字**真的不存在** | **0** |
| **合计** | **14** |

**这 14 条语义错里，没有一条是真实的语义缺陷。** 机制是这样的：

```
03_use_cases.sysml 的 3 个语法错（extend X : Y;）
   └─► 破坏了该文件的包结构
         └─► 包内声明的类型未被建立（如 L21 的 part def EVThermalManagementSystem;）
               └─► 同包内 L287 的 subject system : EVThermalManagementSystem; 报「解析不到」
                     └─► 连带触发 A usage must be typed by definitions.
```

> ⚠️ **实践含义：语法错没清零之前，不要去「修语义错」** —— 修一个消失一个，纯属浪费。
> 这也是「**语法修复是语义校验的前置条件**」的量化证据：先让 `n_syntax` 归零，再谈语义。
>
> ⚠️ 另外注意该工程里还有一处**同名**：`00_master.sysml:8` 的 `package EVThermalManagementSystem`
> 与 `02_architecture.sysml:19` 的 `part def EVThermalManagementSystem` 同名。
> 这类「包名与类型名撞车」会让人工排查更费劲，**建议包名统一加工程前缀**（如 `EVTM_*`）。


### 8.3 输出噪音的处理

诊断行会被大量 `Reading ...` 淹没（加载 121 个库文件的日志）。**只提取以 `[ERROR]` / `[WARN]` 开头的行**。

> ⚠️ 另有一个坑：诊断行里的文件名被**硬编码为 `stdin`**（不是真实文件名）。批量校验时需自行按 `<行>:<列>` 映射回源文件。

### 8.4 一条务实的修复策略

| 错误类别 | 能否确定性修复 | 手段 |
|---|---|---|
| 臆造关键字（`refines`/`traces`） | ✅ | 正则改写为 `:>` / `dependency` |
| `connector X from A to B;` | ✅ | 改写为 `connect A to B;` |
| `&&` / `\|\|` | ✅ | 改写为 `&` / `or` |
| 导入缺可见性前缀 | ✅ | 补 `private ` |
| 类型族不匹配（§3.1） | ⚠️ | 需理解语义，宜交 LLM |
| 跨文件引用未解析 | ✅ | 用项目级合并口径消除 |

### 8.5 文档自检：怎么证明"文档里写的都是真的"

本文档 v1.1 与 v1.2 各做过一次**自我对拍**，方法可以复用（脚本：`%TEMP%\doc_check2.py`）：

```
① 用正则把文档里所有 ```sysml 代码块连同"在文档中的行号"一起抽出来
② 判断每个块的预期：上文/块内出现 ❌ → 预期报错；出现 ✅ → 预期不报错
③ 片段自动包壳：不在 package 里的，套一层 package W_nn { + 标准导入 +} 再送校验器
④ 按「语法错 / 语义错」两路计数：
     · 语法错 > 0  → 文档写的语法本身有问题（**必须改文档**）
     · 语法错 = 0  → 语法没问题；语义错多为"片段缺上下文"的伪错（人工判读）
⑤ 对 ❌ 块反向检查：如果预期报错却没报错，说明**文档的反例是假的**
```

**v1.2 复跑结果（文档 62 个 `sysml` 块）**：14 个报语法错，逐个判读后**全部有据、0 个意外**：

| 类别 | 数量 | 说明 |
|---|---|---|
| 纯反例块（块内全是 ❌） | 7 | `BLK06`（裸 `import`）、`BLK22`（`doc "…"` / `comment { }`）、`BLK32`（`connect (A to B)` 混写）、`BLK33`（`connector … from … to`）、`BLK40`（`assert constraint … ;` 带分号）、`BLK48`（元数据体内漏分号）、`BLK50`（`expose` 在 view def 里） |
| ❌ + ✅ 对照块 | 1 | `BLK10`（`ordered` 位置对照）。脚本的"预期"启发式只看块内首个标记，故这里标成 legal 却报错 —— **属于脚本启发式的边界，不是文档缺陷** |
| B 级占位符模板 | 6 | `BLK54`–`BLK59`（含 `<中文名>`，本就不能编译，已在 §6 分级说明） |

**对比 v1.1**：当时 57 个块里 17 个报语法错，其中 **4 类是真缺陷**（`...` 占位符、共轭端口、
`assign` 裸放、`expose` 位置）。**v1.2 的 14 个里已无真缺陷。**

> ⚠️ **最关键的一条经验**：**"文档自洽"不等于"文档正确"。**
> v1.0 里 `[X07]` 被引用了 10 次，看起来毫无破绽 —— 直到我把 X07 的源文件打开，
> 发现那 181 行里根本没有 `individual` / `timeslice` / `snapshot`。
> **凡是"具体到编号"的引用，都要回到源处核一遍**（本项目的台账：`tmp/v2spec/_ledger.txt`）。
>
> ⚠️ **v1.2 补第二条经验（更要紧）**：**"语法里有产生式"不等于"实现接受"**。
> 本轮实测到 2 例（`DefaultTargetSuccession` 的 `else`、定义位共轭 `port def Q : ~P;`），
> EBNF 白纸黑字有产生式，校验器却不认。**别用"读语法"替代"跑校验器"。**
>
> ⚠️ **v1.2 补第三条经验**：**报错行号必须逐条对齐源码**。第十三～十五轮我把同一个用例缺陷
> （`port def P { out item x : Real; }`，`item` 必须由 `item def` 定型）犯了 3 次，
> 5 个共轭用例的报错行全部指向第 7 行，我却先按"共轭被拒"解读。**同一条行号反复出现时，先查自己的用例。**

### 8.6 项目级合并校验脚本（可直接复制落地）

§8.1–§8.5 讲的是「怎么调、怎么判读」。这里给一份**可以直接放进工程的实现**——
零新依赖（只用 `subprocess` + `re`），落在工程根 `sysml_v2_check.py`，与 `model_quality.py` 同级。

**每个设计点都对应前面的一条实测教训**：

| 设计点 | 对应的教训 |
|---|---|
| **合并成一个文件再校验** | 单文件口径约 2/3 是跨文件伪错（§8.2 ③） |
| **先过滤 `Reading ...` 噪声再提诊断** | 否则诊断被 100+ 行日志淹没（§8.3） |
| **语法错 / 语义错双路计数** | ERROR 总数会因「语法错遮蔽语义错」而**反向上升**（§8.2 ②） |
| **用退出码做门禁** | `rc=1` 即存在 ERROR（§8.1） |
| **临时文件 UTF-8 无 BOM** | 带 BOM 直接触发词法错（§1.3） |
| **按合并行号反查源文件** | 诊断里的文件名被硬编码为 `stdin`（§8.3） |

```python
# sysml_v2_check.py —— 放在工程根，与 model_quality.py 同级
import os, re, subprocess, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
JAVA = os.path.join(REPO, "java-runtime", "bin", "java.exe")
JAR  = os.path.join(REPO, "checker.jar")

# 语法错的典型特征（§8.2 ②）—— 命中即归「语法路」
SYNTAX_SIGNS = re.compile(
    r"no viable alternative|mismatched input|extraneous input|"
    r"mismatched character|missing ")
DIAG = re.compile(r"^\[(ERROR|WARN)\]\s+stdin:(\d+):(\d+):\s*(.*)$")


def check_project(paths, *, timeout=120):
    """项目级合并校验。paths 按顺序传入；诊断行号可按 merge_map 反查回源文件。"""
    parts, merge_map, line = [], [], 0
    for p in paths:
        src = open(p, "rb").read().decode("utf-8").lstrip("\ufeff")   # 去 BOM
        parts.append(src)
        n = src.count("\n") + 1
        merge_map.append((p, line + 1, line + n))
        line += n
    merged = "\n".join(parts)

    fd, tmp = tempfile.mkstemp(suffix=".sysml")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:              # 无 BOM 写出
            f.write(merged.encode("utf-8"))
        proc = subprocess.run([JAVA, "-jar", JAR, "-i", tmp], cwd=REPO,
                              capture_output=True, timeout=timeout)
    finally:
        os.unlink(tmp)

    diags = []
    for ln in proc.stdout.decode("utf-8", "replace").splitlines():
        m = DIAG.match(ln.strip())              # ← 先过滤 Reading 噪声
        if not m:
            continue
        sev, no, col, msg = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        diags.append({"sev": sev, "line": no, "col": col, "msg": msg,
                      "file": _which(merge_map, no),
                      "is_syntax": bool(SYNTAX_SIGNS.search(msg))})
    errs = [d for d in diags if d["sev"] == "ERROR"]
    return {
        "rc": proc.returncode,                  # 1 = 有 ERROR，门禁直接用
        "errors": errs,
        "warns": [d for d in diags if d["sev"] == "WARN"],
        "n_error": len(errs),
        "n_syntax": len([d for d in errs if d["is_syntax"]]),      # 双路计数
        "n_semantic": len([d for d in errs if not d["is_syntax"]]),
    }


def _which(merge_map, line_no):
    for p, a, b in merge_map:
        if a <= line_no <= b:
            return os.path.basename(p)
    return "?"
```

**调用与门禁**：

```python
r = check_project(["sysml_models/ev_thermal_mgmt/00_master.sysml",
                   "sysml_models/ev_thermal_mgmt/01_requirements.sysml",
                   # … 其余文件
                   ])
print(r["rc"], r["n_error"], "语法", r["n_syntax"], "语义", r["n_semantic"])
```

| 判定 | 动作 | 理由 |
|---|---|---|
| `rc == 0` | **放行** | 无 ERROR |
| `rc == 1` 且 `n_syntax == 0` | **报告但不阻断** | 只剩语义错，需人判断（很多是「引用 usage 而非 def」这类改文本解决不了的问题） |
| `rc == 1` 且 `n_syntax > 0` | **阻断，先修语法** | 语法错会级联污染命名空间（§8.2 铁律） |

> ⚠️ **预算门控（防止反复重跑）**：校验次数上限 1 次/轮；确定性修复最多 1 轮，
> 且要求 `n_syntax` **严格下降**，否则立即停；代码 hash 未变则短路不重跑。
> 单次耗时 4–6 s（其中库加载固定约 2.3 s），合并校验比逐文件更省。

> ✅ **这段脚本已实测跑通**（2026-09-19）：把上面代码**原样抽出执行**，对本工程
> `sysml_models/ev_thermal_mgmt/` 的 6 个文件跑一遍，得到
> `rc=1 / ERROR 47 / 语法 33 / 语义 14 / WARN 48` —— 与
> `docs/V2代码自动校验与修复闭环-方案评估-20260918.md` §3.1 用**另一套脚本**得到的项目级口径
> **逐项一致**。两套独立实现互证，说明脚本可直接用。
>
> 📎 这份脚本的骨架来自该文 §5 的闭环设计与 §2 的实测契约，
> 已按其 §2.3 的行为矩阵逐条对齐（`-i` 参数、退出码、`stdin` 文件名、噪声过滤）。

---

## 附录 A：实测方法与证据索引

### A.1 实测环境

| 项 | 值 |
|---|---|
| 校验器 | `checker.jar`（132,948,123 B，`Main-Class: SysMLValidator`，内嵌 OMG 官方实现 `org/omg/sysml/xtext/**`） |
| 运行时 | `java-runtime/`（JDK 25.0.1，Microsoft Build，含 javac + jmods） |
| 标准库 | `sysml.library/`（**121 个文件 / 7.69 MB**：Kernel Libraries 36 + Systems Library 21 + Domain Libraries 38 = **95 个可导入包**，见附录 C） |
| 单次耗时 | 4–6 s（其中库加载固定约 2.3 s） |

### A.2 实测规模

**总计 467 条定向用例 + 62 个文档代码块复跑**（= 529 次校验器调用），分 19 轮：

| 轮次 | 用例数 | 目的 | 结果文件 |
|---|---|---|---|
| 第一轮 | 50 | 规范写法普查（`T01`–`T50`） | `tmp/v2spec/_spec_check.{txt,json}` |
| 第二轮 | 40 | 修正用例缺陷 + 冲突点判定（`U01`–`U40`，含 `B1`–`B10` 标签组） | `_spec_check2.*` |
| 第三轮 | 40 | 可复用模板验证（`V01`–`V40`） | `_spec_check3.*` |
| 第四轮 | 11 | 端到端模型（`W01`–`W11`） | `_spec_check4.*` |
| 第五轮 | 9 | 修正后终验（`X01`–`X09`，全通过） | `_spec_check5.*` |
| — | **150** | **小计：v1.0 的取证基础** | |
| 第六轮 | 24 | **文档自检**：把文档里全部 **57 个 ```` ```sysml ```` 代码块**拉回复跑 + 24 个补测项（`Z01`–`Z24`、`BLK01`–`BLK57`） | `_doccheck2.*` |
| 第七轮 | 23 | 收口未取证断言（`Y01`–`Y23`）：`typed by`、`...`、共轭、`ordered` 位置、`individual` 定型、`expose` 位置 | `_spec_check7.txt` |
| 第八轮 | 6 | 官方示例原样核对（`E01`–`E06`）：枚举、导入过滤、包级注解、验证用例 | `_spec_check8.txt` |
| — | **53** | **小计：v1.1 的补测** | |
| 第九轮 | 50 | **官方英文英文规范写法普查**（`Z01`–`Z50`）：照 Annex A 逐条抄 | `_spec_check9.*` |
| 第十轮 | 20 | 补测与归因（`X01`–`X40`，前 27 例因脚本缺陷中断，取 `10b` 的 20 例） | `_spec_check10b.*` |
| 第十一轮 | 24 | 定向确认（`V01`–`V24`）：`@X` 元数据、`protected import`、保留字 | `_spec_check11.*` |
| 第十二轮 | 20 | 知识文档片段校验（`T01`–`T20`） | `_spec_check12.*` |
| 第十三轮 | 37 | **Annex A 逐字转录 + 争议点定性**（`E`/`F`/`G`/`H`/`I`/`J`/`K`/`M` 组） | `_spec_check13.*` |
| 第十四轮 | 29 | `rep` / concern-subject / interface / 共轭位置（`N`/`O`/`P`/`Q`/`R`/`T` 组） | `_spec_check14.*` |
| 第十五轮 | 21 | **修正用例缺陷后重测**（`U`/`V`/`W` 组） | `_spec_check15.*` |
| 第十六轮 | 4 | `subject` 顺序确认 + 官方 A.9 转录（`X01`–`X04`） | `_spec_check16.*` |
| — | **205** | **小计：v1.2 的补测** | |
| 第十七轮 | 27 | **§A.4 已知局限逐条收口**（`A`–`F` 组）：use case include、rendering、枚举带属性、flow/message、KerML 集合关系、metadata 定型 | `_spec_check17.*` |
| 第十八轮 | 24 | **修正用例缺陷后重测**（`E11`–`E18` 按 KerML 原文补正特化位置）+ include/flow/rendering 扩展 | `_spec_check18.*` |
| 第十九轮 | 8 | 收口：rendering usage 引用、`flow def` 的 `end`、枚举字面量属性引用 | `_spec_check19.*` |
| — | **59** | **小计：v1.3 的补测** | |
| **合计** | **467** | | |

用例源码保留在 `tmp/v2spec/tests{,2,3,4,5,7,8,12,13,14,15,16,17,18,19}/`，第六轮的包壳文件在 `tmp/v2spec/doccheck2/`，
可单独复跑。用例总台账见 `tmp/v2spec/_ledger.txt`。

> ⚠️ **v1.3 的一条方法论教训（与 v1.2 那条正好互补）**：第 17 轮里 `E` 组（KerML 集合关系）**全报语法错**，
> 我差点直接写成「实现不支持」。回查 KerML 原文才发现 `TypeRelationshipPart` 的语法位置是
> `TypeDeclaration = ( SpecializationPart | ConjugationPart )+ TypeRelationshipPart*` —— **必须跟在特化之后**，
> 我的用例漏了 `:> A` 前缀。第 18 轮补正后**仍然报错**，这时才敢下「实现不支持」的结论，
> 并加了对照组 `part def C :> A, B;` ✅ 来证明不是用例问题。
> **这与 v1.2 的教训是一对**：v1.2 是「同一行号反复出现，先怀疑自己的用例」；
> v1.3 是「**下『实现不支持』结论前，必须先把『我可能写错了』排除干净，并给出对照组**」。

> ⚠️ **编号说明**：`BLK01`–`BLK57` 是**文档里的代码块序号**（按出现顺序），不是手写用例；
> `Z01`–`Z24` 是第六轮的补测项；`Y01`–`Y23` 是第七轮；`E01`–`E06` 是第八轮。
> **v1.2 新增的九–十六轮各用了独立字母前缀**（`E`/`F`/`G`/`H`/`I`/`J`/`K`/`M`/`N`/`O`/`P`/`Q`/`R`/`T`/`U`/`V`/`W`/`X`），
> 引用时**务必带上结果文件轮次**（如"第十三轮 `E01`"），否则前缀会跨轮撞名。
> 文档正文中 `[B1]`–`[B10]` 的旧标法已全部替换为 `[U07]`–`[U16]`。

> ⚠️ **v1.2 的一条方法论教训（值得单独记下）**：第十三～十五轮我**同一个用例缺陷犯了 3 次**——
> 在 `port def P { out item x : Real; }` 里用 `Real` 给 `item` 定型（`item` 必须由 `item def` 定型）。
> 结果 **5 个共轭用例的报错行全部指向第 7 行**，我却先按"共轭被拒"解读了。
> **教训：报错行号必须逐条对齐到源码行；同一错误行号反复出现时，先怀疑自己的用例，而不是被测对象。**

### A.3 引用文档

| 优先级 | 文档 | 路径 | 用途 |
|---|---|---|---|
| ① 一手 | **SysML v2 英文原版规范**（691 页，`formal/25-09-03`，Part 1） | `D:\广汽项目资料\sysml-V2规范\OMG Systems Modeling Language™ V2 (1).pdf` | **§8.2.2 EBNF 具体语法（341 条产生式，印刷 p.163–254）**、§8.2.2.1.2 保留字表、§9 库元素、**Annex A 完整示例（13 节）** |
| ② 一手 | KerML v1.0 规范（454 页） | `D:\广汽项目资料\sysml-V2规范\Kernel Modeling Language™ (KerML™).pdf` | 词法 / 语法骨架 / 类型系统（**注意 SysML v2 在 `:`/`~` 上有覆盖**） |
| ③ 辅助 | SysML v2 规范（中文译本，docx） | `D:\广汽项目资料\sysml-V2规范\SysML+V+2.0+formal-25-09-03_zh-CN+-+第1-7章+Annex 初稿 20260120.docx` | 中文语境理解；**含英文原版没有的笔误，不作依据** |
| — | 提取文本（供检索） | `tmp/v2spec/sysml_en_paged.txt`（691 页带页标）、`sysml_ebnf_raw.txt`（§8.2.2 纯净区间 38–1432 行）、`annex_a.txt`（4.1 万字符）、`kerml.txt`、`sysmlzh_code.txt` | 全文检索与逐字核对 |

> **页码换算**：英文原版 PDF **PDF 页码 = 印刷页码 + 32**（已用 `tmp/v2spec/page_index.json` 验证全篇一致）。
> 本文档引用 EBNF 时给出的 "印刷 p.xxx" 均可按此换算回 PDF 页。

### A.4 本文档的已知局限

> **v1.3 更新**：v1.2 在这里列的 7 条局限，**第 1、2 条已用第 17–19 轮实测收口**。
> **已解决的条目不删除，只标注状态** —— 便于读者判断哪些结论是新近取得的、哪些还是老的。

| 原 # | 局限 | v1.3 状态 | 处置 / 依据 |
|---|---|---|---|
| 1 | 官方「枚举字面量带属性」继承形式 | ✅ **已解决** | 父类型换成 **`attribute def`** 即可用，见 §4.4；`C04`/`C11`/`C12`/`G08` |
| 2a | `use case` 的 `include` / `extend` | ✅ **已解决** | `include` 三种形式均可，**`extend` 在 v2 不存在**，见 §4.12；`A01`–`A03`/`A11`–`A14`/`G07` |
| 2b | `flow` / `message` 的方向与载荷 | ✅ **已解决** | 见新增 §4.17；`D01`–`D06`/`D11`/`G05`/`G06` |
| 2c | `rendering def` 自定义渲染器 | ✅ **已解决** | def 定类型 + `rendering` 建 usage + `render` 引用 usage，见 §4.15；`G01`/`G03`/`G04` |
| 2d | KerML `featured by` / `unions` / `intersects` / `differences` / `disjoint from` | ⛔ **实现不支持（结论性）** | 五个构造**全部语法错**；已按 KerML 原文补正位置重测仍不收，且对照组 `part def C :> A, B;` ✅ —— 见 §0.1 第 10 项；`E11`–`E18` |
| 3 | 只做**单文件口径**验证 | ⏳ 仍成立 | 本文档用例都是「单包 + 必要导入」的**最小可编译单元**。真实工程是**多文件/多包**，正确门禁口径是**项目级合并校验** —— 可直接用 §8.6 的脚本 |
| 4 | 未探测 `checker.jar` 的**常驻/服务化入口** | ⏳ 仍成立 | jar 内含 `org/omg/sysml/api/*`、jupyter、zeromq、okhttp，**暗示可能存在**。若支持，可摊薄每次约 2.3 s 的库加载开销 |
| 5 | **版本敏感** | ⏳ 仍成立 | 结论基于**当前** `checker.jar`。规范（`formal/25-09-03`）与实现存在 **10 处已知不一致**（§0.1 全表）。**换版本后必须重跑用例集**——脚本见 §A.2 所列结果文件 |
| 6 | **不覆盖「模型对不对」** | ⏳ 仍成立 | 校验器只检查**词法 / 语法 / 语义**。100% 通过校验的模型，**仍可能是业务上错误的系统设计** —— 本闭环不能替代工程评审 |
| 7 | **不保证穷尽** | ⏳ 仍成立 | 467 条用例覆盖的是**AI 高频写法**，不是语法全集（341 条产生式）。若某写法既不在本文档、也未被校验器拒绝，**不要假定它合法** —— 先过校验器 |

### A.5 v1.2 相对 v1.1 的净变化（速览）

**结论被改（最需要关注，共 5 条）**：

| 变化 | v1.1 的说法 | v1.2 的结论 | 位置 |
|---|---|---|---|
| `else` | "任何位置都不合法" | **`if {...} else {...}` 动作块里合法**；只作后继兜底非法 | §4.8 |
| `subject` 位置 | "必须是体内第一个参数" | **有 `stakeholder`/`actor` 时必须有 `subject` 且在其前**；`doc` 可在 `subject` 前 | §4.11、§4.12 |
| `rep` | 示例写 `rep /* … */` | **`language "<lang>"` 必填且在注释之前** | §4.2 |
| `doc`/`comment` 的 `locale` | 未说明 | **必须写在注释之前** | §4.2 |
| 共轭 `~T` | "定义处不行，特征位置可以" | **定义处的 `:` 不行；`~T` 可用于特征/端位、`:>` 特化位** | §4.7 |

**新增规则 / 事实**：

| 变化 | 位置 |
|---|---|
| `interface def` / `interface usage` 写法（用法至少 2 个端） | §4.7 |
| `allocate X to Y;`（需求→部件/特征分配） | §4.11 |
| `#derivation connection {...}`（官方追溯机制，需 `import RequirementDerivation::*`） | §4.11 |
| `<短名>` 形式（`requirement <'1'> X : T {...}`） | §4.11 |
| `variant` 是保留关键字 | §1.8 |
| `enum def` 体内不允许 `attribute`（§8.2.2.10 产生式 + `E01` 双证） | §4.4 |
| 视图渲染器名 4 个 + 必须 `import Views::*` | §4.15 |
| `accept after 30 [s]` 需 `import SI::*`、`accept at Iso8601DateTime(...)` 需 `import Time::*` | §4.8 |
| 修饰符顺序链的 EBNF 原文出处（`RefPrefix` → `BasicUsagePrefix` → `OccurrenceUsagePrefix`） | §2.4 |
| 导入可见性必填的 EBNF 原文出处（`Import = visibility = VisibilityIndicator 'import'`，无 `?`） | §2.2 |
| **附录 B：EBNF 产生式索引** | 新增 |
| **附录 C：标准库 95 个可导入包清单** | 新增 |
| §0.1 不一致清单从 7 例扩到 **9 例** | §0.1 |

**取证深化（结论未变，但依据从"实测"升级为"实测 + EBNF 原文"）**：
`:>` 的特化/子集化双重语义、`defined by`、导入可见性、修饰符顺序、`ordered` 必须跟在名字后、
`expose` 只能用于 view usage、`entry; then off;` 的合法性、`enum def` 体内元素限制。

<details>
<summary>v1.1 相对 v1.0 的净变化（历史留存，点击展开）</summary>

| 变化 | 类型 | 位置 |
|---|---|---|
| `:` 的等价关键字从 `typed by` 改为 `defined by` | **改结论** | §0.1、§1.7 |
| 共轭端口"定义处"写法判为非法（v1.2 已进一步精确化） | **改结论** | §4.7 |
| 新增"`...` / 占位符非法"规则 | **新增规则** | §1.9、§5 #21 |
| `X07` 对 `individual`/`timeslice`/`snapshot` 的引证作废，改用 `Y15` | **改取证** | §4.5 |
| `B1`–`B10` → `U07`–`U16` | **改编号** | §4.8、§5 #6 |
| `V29` 不再为 `->size()`/`#(i)` 背书（改 `[U24]/[V04]/[Y14]`） | **改取证** | §2.7 |
| `attribute :>> massRequired = 2000 [kg]` → `:>> tempAllowed = 328 [K]` | **改取证** | §2.6 |
| §6 模板分 A/B 两级，逐模板给证据 ID | **改表述** | §6 |
| 新增 `expose` 只能用于 view usage 的规则 | **新增规则** | §4.15 |
| 新增 `ordered` 位置、`individual` 定型、`part a[0..*] : P` 等实测 | **新增事实** | §2.4、§2.5、§4.5 |
| 新增 §8.5 文档自检方法与"引用要回源核对"经验 | **新增方法** | §8.5 |

</details>

### A.6 v1.3 相对 v1.2 的净变化（速览）

**新增可写构造（4 类，v1.2 标的「未覆盖」现在有实证写法了）**：

| 构造 | 写法 | 位置 | 依据 |
|---|---|---|---|
| 用例包含 | `include use case X;` / `include use case x : X;` / `include x;` | §4.12 | `A01`–`A03`/`A11`–`A14`/`G07` |
| 枚举字面量带属性 | `enum def L :> <attribute def> { enum low { :>> p = v; } }` | §4.4 | `C04`/`C11`/`C12`/`G08` |
| 自定义渲染器 | `rendering def X;` → `rendering r : X;` → `render r;` | §4.15 | `G01`/`G03`/`G04` |
| 流与消息 | `flow f of P from a to b;` / `message m of P from a to b;` / `flow def F { end a; end b; }` | **§4.17（新增）** | `D01`–`D06`/`D11` |

**新增结论性否定（写之前就该知道「这条路不通」）**：

| 构造 | 结论 | 位置 | 依据 |
|---|---|---|---|
| `extend` | **v2 没有这个关键字**（341 条产生式零命中） | §4.12 | `A13` ❌ |
| KerML `unions`/`intersects`/`differences`/`disjoint from` | **实现不收** | §0.1 第 10 项 | `E11`–`E14`/`E18` ❌ |
| KerML `featured by` | **实现不收** | §0.1 第 10 项 | `E15`/`E16` ❌ |
| `render <rendering def>;` | 语义错，必须引用 usage | §4.15 | `B13`/`B14` ❌ |
| 单端 `flow … to x;` / 裸 `flow def F;` | 均不成立 | §4.17 | `D03`/`D12`/`D14` ❌ |
| `metadata m typed by X;` | 语法错，一律用 `:` | §0.1 第 3 项 | `F11`/`F01` ❌ |

**文档结构变更**：新增 **§4.17**、**§8.6**；**§8 小节顺序修正**（v1.2 里 8.5 错插在 8.2 与 8.3 之间）；
§5 错误对照表 **38 → 46 条**；§A.4 改为**带状态**的局限表。

---

## 附录 B：EBNF 产生式索引（官方 §8.2.2）

下表是本文档引用过的产生式，**均为英文原版规范原文**。印刷页码 = PDF 页码 − 32。
范围：§8.2.2 文本语法的纯净区间是 `sysml_ebnf_raw.txt` 第 38–1432 行（第 1433 行起是 §8.2.3 图形语法）。

**词法层**（§8.2.2.1，印刷 p.164–166）

| 产生式 | 原文 | 要点 |
|---|---|---|
| `BASIC_NAME` | `[a-zA-Z_][a-zA-Z0-9_]*` | **仅 ASCII**，中文名必须单引号包裹 |
| `UNRESTRICTED_NAME` | `'\'' ( printable - '\'' \| '\\' \| '\b' \| '\f' )* '\''` | 单引号名可含任意可打印字符 |
| `DEFINED_BY` | `':' \| 'defined' 'by'` | **没有 `TYPED_BY`**（那是 KerML 的） |
| `SPECIALIZES` / `SUBSETS` | 都是 `':>'` | 同一终结符，由位置决定语义 |
| `REDEFINES` | `':>>'` | |
| `REFERENCES` | `'::>'` | |
| 保留字 | **130 个** | 见 §1.8 |

**结构层**

| 产生式 | 位置 | 要点 |
|---|---|---|
| `Import = visibility = VisibilityIndicator 'import' ...` | §8.2.2.5.1 | **可见性无 `?`，必填** |
| `ConjugatedPortTyping = '~' ~[QualifiedName]` | §8.2.2.11 | 实现只在**特征/端位**接受（`W01`–`W09`） |
| `EnumerationBody = ';' \| '{' (AnnotatingMember \| EnumerationUsageMember)* '}'` | §8.2.2.10 | **只有注释类元素 + 枚举字面量**，故 `attribute` 非法（`E01`） |
| `InterfaceUsageDeclaration = UsageDeclaration ValuePart? ( 'connect' InterfacePart )? \| InterfacePart` | §8.2.2.14.2 | 用法**至少要 2 个端**（`V05`） |
| `BinaryInterfacePart = InterfaceEndMember 'to' InterfaceEndMember` | §8.2.2.14.2 | 二元形式**无括号** |
| `NaryInterfacePart = '(' InterfaceEndMember ',' … ')'` | §8.2.2.14.2 | n 元形式**用逗号、要括号**（混用即 `V04` 报错） |
| `DefaultInterfaceEnd : PortUsage = isEnd ?= 'end' Usage` | §8.2.2.14.1 | `end x : T;` |
| `SubjectMember : SubjectMembership = MemberPrefix ownedRelatedElement += SubjectUsage` | §8.2.2.21.1 | 位置约束由**实现**施加，产生式未写 |
| `StakeholderUsage : PartUsage = 'stakeholder' UsageExtensionKeyword* Usage` | §8.2.2.21.1 | 类型是 **PartUsage**（不是 ReferenceUsage） |
| `ActorUsage : PartUsage = 'actor' ...` | §8.2.2.21.1 | 同上 |
| `RequirementConstraintMember = MemberPrefix? RequirementKind ...`，`RequirementKind = 'assume' \| 'require'` | §8.2.2.21.1 | `assume`/`require` 前缀可省 |
| `ConcernDefinition = ... 'concern' 'def' DefinitionDeclaration RequirementBody` | §8.2.2.21.3 | 用 `RequirementBody` → 可含 `subject`/`stakeholder` |
| `ViewpointUsage = ... 'viewpoint' ConstraintUsageDeclaration RequirementBody` | §8.2.2.26.3 | 同上 |
| `ViewBodyItem` 含 `Expose`；`ViewDefinitionBodyItem` **不含** | §8.2.2.26 | **`expose` 只能用于 view usage**（`H04` 反证） |
| `MetadataUsage = UsageExtensionKeyword* ( '@' \| 'metadata' ) MetadataUsageDeclaration ('about' Annotation (',' …)* )? MetadataBody` | §8.2.2.27 | `@X` 与 `metadata X` 等价 |
| `MetadataBody = ';' \| '{' (DefinitionMember \| MetadataBodyUsageMember \| AliasMember \| Import)* '}'` | §8.2.2.27 | 体内成员**必须带分号**（`T02` 反证） |
| `TextualRepresentation = ( 'rep' Identification )? 'language' STRING_VALUE body = REGULAR_COMMENT` | §8.2.2.4.3 | **`language` 必填且在注释前**（`N01`–`N03`） |
| `Documentation = 'doc' Identification ('locale' STRING_VALUE)? body = REGULAR_COMMENT` | §8.2.2.4.2 | `locale` **在注释前**（`N04`/`N05`） |
| `DefaultTargetSuccession : TransitionUsage = 'else' ownedRelationship += TransitionSuccessionMember` | §8.2.2.17.8 | **实现未支持**（`E01`–`E04`） |
| `GuardedSuccession = ( 'succession' UsageDeclaration )? 'first' FeatureChainMember GuardExpressionMember 'then' TransitionSuccessionMember UsageBody` | §8.2.2.17.8 | `first X if c then Y;` 的出处（`E05`） |
| `EntryTransitionMember = (GuardedTargetSuccession \| 'then' TargetSuccession) ';'` | §8.2.2.18.1 | `entry; then off;` 的出处 |
| `ActionBodyItem` 的分支 | §8.2.2.17.1 | `ActionTargetSuccessionMember` 只能跟在 `first` 或行为成员之后 |
| 派生 / 分配 | `RequirementDerivation::<original>`、`RequirementDerivation::<derive>` | 官方追溯用 `#derivation connection`（`J01`） |
| `IncludeUseCaseUsage = OccurrenceUsagePrefix 'include' ( OwnedReferenceSubsetting FeatureSpecializationPart? \| 'use' 'case' UsageDeclaration ) ValuePart? CaseBody` | §8.2.2.25 | 产生式本身就给了两种形态：`include <引用>;` 与 `include use case <名>[: 类型];`（`A01`–`A03`） |
| `FlowDefinition : OccurrenceDefinitionPrefix 'flow' 'def' Definition` | §8.2.2.16 | `flow def F { end a : A; end b : B; }` —— **必须含端**（`D11` ✅ / `D12` ❌） |
| `FlowDeclaration : FlowUsage = UsageDeclaration ValuePart? ( 'of' … )? ( 'from' … 'to' … )?` | §8.2.2.16 | `of <载荷>` 可省；**`from…to` 要成对**（`D03` ❌ 单端 `to`） |
| `MessageDeclaration : FlowUsage = UsageDeclaration ValuePart? ( 'of' … )? ( 'from' … 'to' … )?` | §8.2.2.16 | `message` 与 `flow` 同族（`D05`/`D06`） |
| `RenderingDefinition = OccurrenceDefinitionPrefix 'rendering' 'def' Definition` | §8.2.2.26.4 | 只定**类型**（`B01`/`B02`） |
| `RenderingUsage = OccurrenceUsagePrefix 'rendering' Usage` | §8.2.2.26.4 | 建 **usage** 实例 `rendering myR : X;`（`G03`） |
| `ViewRenderingMember : ViewRenderingMembership = MemberPrefix 'render' ownedRelatedElement += ViewRenderingUsage` | §8.2.2.26.1 | `render` 位置要 **usage**，不能是 def（`B13` ❌ / `G01` ✅） |
| `EnumerationBody = ';' \| '{' ( AnnotatingMember \| EnumerationUsageMember )* '}'` 的**父类型**约束 | §8.2.2.10 | 父类型是 `enum def` → 被判变体特化（`C01` ❌）；是 `attribute def` → 可用（`C04` ✅） |
| **KerML** `TypeRelationshipPart = DisjoiningPart \| UnioningPart \| IntersectingPart \| DifferencingPart` | KerML §8.2.4.1 | 位置在 `( SpecializationPart \| ConjugationPart )+` **之后**；即便如此 **SysML v2 实现仍不收**（`E11`–`E14`/`E18` ❌） |
| **KerML** `TypeFeaturingPart = 'featured' 'by' …` | KerML §8.2.4.1 | **SysML v2 实现不收**（`E15`/`E16` ❌） |

---

## 附录 C：标准库可导入包清单（95 个）

来源：本地 `sysml.library/`（**121 个文件，其中 95 个可导入包**：36 个 Kernel `.kerml` + 59 个 Domain/Systems `.sysml`）。
`import` 的包名必须在此表内，否则报 `Couldn't resolve reference to ...`。

**Kernel 层（36 个，`.kerml`）** —— 位于 `sysml.library/Kernel Libraries/`，部分为**隐式可用**

| 子目录 | 包名 |
|---|---|
| Kernel Semantic Library | `Base` `Clocks` `ControlPerformances` `FeatureReferencingPerformances` `KerML` `Links` `Metaobjects` `Objects` `Observation` `Occurrences` `Performances` `SpatialFrames` `StatePerformances` `Transfers` `TransitionPerformances` `Triggers` |
| Kernel Function Library | `BaseFunctions` `BooleanFunctions` `CollectionFunctions` `ComplexFunctions` `ControlFunctions` `DataFunctions` `IntegerFunctions` `NaturalFunctions` `NumericalFunctions` `OccurrenceFunctions` `RationalFunctions` `RealFunctions` `ScalarFunctions` `SequenceFunctions` `StringFunctions` `TrigFunctions` `VectorFunctions` |
| Kernel Data Type Library | `Collections` `ScalarValues` `VectorValues` |

**Systems Library（21 个，`.sysml`）** —— 位于 `sysml.library/Systems Library/`

```
Actions  Allocations  AnalysisCases  Attributes  Calculations  Cases  Connections
Constraints  Flows  Interfaces  Items  Metadata  Parts  Ports  Requirements
StandardViewDefinitions  States  SysML  UseCases  VerificationCases  Views
```

**Domain Libraries（38 个，`.sysml`）**

| 子目录 | 包名 |
|---|---|
| Quantities and Units | `ISQ` `ISQAcoustics` `ISQAtomicNuclear` `ISQBase` `ISQCharacteristicNumbers` `ISQChemistryMolecular` `ISQCondensedMatter` `ISQElectromagnetism` `ISQInformation` `ISQLight` `ISQMechanics` `ISQSpaceTime` `ISQThermodynamics` `MeasurementRefCalculations` `MeasurementReferences` `Quantities` `QuantityCalculations` `SI` `SIPrefixes` `TensorCalculations` `Time` `USCustomaryUnits` `VectorCalculations` |
| Requirement Derivation | `DerivationConnections` `RequirementDerivation` |
| Analysis | `AnalysisTooling` `SampledFunctions` `StateSpaceRepresentation` `TradeStudies` |
| Cause and Effect | `CausationConnections` `CauseAndEffect` |
| Metadata | `ImageMetadata` `ModelingMetadata` `ParametersOfInterestMetadata` `RiskMetadata` |
| Geometry | `ShapeItems` `SpatialItems` |
| （根） | `DS_Views`（短名 `DSViewsAlias`） |

**高频组合（可直接抄进文件头）**：

```sysml
private import ScalarValues::*;          // Boolean / Integer / Real / String / Natural
private import SI::*;                    // [kg] [m] [s] [K] [W] …（单位）
private import ISQ::*;                   // 物理量（ISQ::mass / power / thermodynamicTemperature …）
private import Views::*;                 // 视图渲染器（asTreeDiagram 等）
private import RequirementDerivation::*; // #derivation / #original / #derive
private import VerificationCases::*;     // VerificationMethod 等元数据
private import Time::*;                  // Iso8601DateTime（accept at … 用）
```

> ⚠️ **`private import ScalarValues::*;` 不等于万能**：`Real`/`Boolean` 等标量在里面，但
> `->size()` 需要 `SequenceFunctions` 或 `RealFunctions`（实测 `U24`），
> `kg` 需要 `SI::*`（实测 `U05`）。**导入什么由你实际用到的符号决定。**

---

*文档结束。如有与实测不符之处，以 §A.2 的实测用例复跑结果为准。*

