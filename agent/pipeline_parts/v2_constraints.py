# -*- coding: utf-8 -*-
"""SysML v2 L0 硬约束卡（P0：把语法硬约束前置到生成端）。

## 为什么有这个模块

生成端此前的 `_build_model_code_req`（`prompt.py`）只有约 200 字，**一条语法规则都没有**，
于是 LLM 每轮都在重复犯同样的错（真机实测：一个 156 行的 TMS 模型踩了 7 条语义错，
而语法错是 0）。知识文档 v1.3.1（`docs/SysML-v2-AI建模知识文档.md`，467 条实测用例）
里的规则一条也没进提示词。

按《SysML-v2-知识文档与校验器-工程落地集成指南》§1 的三层消费法，只把 **L0 硬约束卡**写死
在提示词里（每轮生成都注入）；L1 领域速查按意图拼、L2 全文走 RAG，都不在本模块范围内。

## 出处纪律

**卡里每一条都对应知识文档里一个实测用例编号**，不是凭记忆写的：

| 依据 ID | 规则 |
|---|---|
| `T38` | UTF-8 禁止 BOM |
| `V12`/`V13`/`T04`/`T05` | 标识符 ASCII；含非 ASCII/空格的名字必须单引号 |
| `Y03`/`Y04` | `...` 是非法记号，必须写真实体 |
| `T02` | import 可见性前缀必填（**成员**可见性前缀才是可选） |
| `Y01`/`Y02` | `:` 的关键字是 `defined by`，`typed by` 语法错 |
| `T07`/`T41`/`T42`/`T43` | `:`/`:>`/`:>>` 与关键字等价性 |
| `V20` | `~` 共轭无关键字写法（`conjugates` 不存在） |
| `T08`/`T09`/`T14`/`U02`/`U09`/`T27`/`T33` | §5 高频错误前 6 条 + `&&`/`doc` |
| `A13` | `extend` 在 v2 不存在（341 条产生式零命中） |
| `B13`/`B14`/`G01`/`G03` | `render` 只能引用 usage，不能引用 `rendering def` |
| `U01`–`U07`/`X01`–`X03` | `subject`/`actor`/`stakeholder` 顺序硬规则 |
| **`S01`** | **`subject = x` 与 `satisfy r by …` 对同一需求二选一**（2026-09-19 真机实测新增） |
| `J03`/`J04` | `allocate r to <usage>;` |
| §3.2 / §3.3 | 类型族匹配；"引用类"语法只能引用 usage |

改卡内容时**必须同步知识文档**，并把新规则补进上面的出处表——否则本卡会慢慢与实测脱节。

## 开关

`core/config.py` → `sysml.l0_card_enabled`（默认 `True`）/ `sysml.l0_card_extra`（现场追加约束）。
关掉即回到改动前的行为（只输出原来那段建模输出要求），便于 A/B 与故障回退。

## 体量

约 2.0 KB / 约 1.3 K token，每轮建模都注入。`_apply_context_budget` 只裁检索区与历史区，
**不会裁本卡**；因此体量要自觉控制，加规则前先想清楚值不值这个固定成本。
"""

L0_CARD = """
【SysML v2 语法硬约束 —— 违反即无法通过官方校验器，必须逐条遵守】
（以下每条均由 checker.jar 实测得出；格式为「左错 → 右对」）

■ 编码 / 标识符
· UTF-8 禁止 BOM；裸名只能 A-Z a-z _ 开头，后续只 A-Z a-z 0-9 _
· 含中文/空格/连字符的名字必须单引号包裹：part def '电池包'; ✅ ｜ part def 电池包; ❌
  （推荐英文标识符 + 中文 doc 注释）

■ 包与导入
· 全文必须包在 package <Name> { … } 内；`...` 是非法记号，必须写真实体
· import 的可见性前缀是**必填**的（成员可见性前缀才可选）：
    private import ScalarValues::*;   // Boolean Integer Real String Natural
    private import SI::*;             // [kg] [m] [s] [K] [W]
    private import ISQ::*;            // ISQ::mass / power / thermodynamicTemperature

■ 关键字与符号（`:` 的关键字是 defined by，**不是** typed by）
· :  ≡ defined by    :> ≡ specializes / subsets
· :>> ≡ redefines    ::> ≡ references
· 共轭只能写 port p : ~A;（conjugates 不存在）

■ 高频错误对照
import X::*;                     → private import X::*;
item x typed by A;               → item x defined by A;   或  item x : A;
requirement R2 refines R1;       → requirement r2 :> r1;
requirement R2 traces R1;        → dependency from R2 to R1;
connector c from a to b;         → connect a to b;
satisfy SomeRequirementDef by p; → 先 requirement r : Def; 再 satisfy r by p;
doc "文本"                        → doc /* 文本 */
a && b   /   a || b              → a & b   /   a or b
first A if C then B else D;      → if C { action X; } else { action Y; }
render MyRendererDef;            → 先 rendering r : MyRendererDef; 再 render r;
extend SubUC;                    → v2 没有 extend → include use case SubUC;

■ 需求与满足（TMS 领域最核心，实测翻车最多）
· 细化 requirement r2 :> r1; ｜ 满足 satisfy r by <usage>; ｜ 分配 allocate r to <usage>;
· ★ subject 与 satisfy 对同一个需求**只能二选一**：
    ❌ requirement r : R { subject = x; }   同时   satisfy r by x.y;
       → Cannot override a binding feature value（肉眼挑不出毛病，只有跑校验器才暴露）
    ✅ requirement r : R;                   同时   satisfy r by x.y;
    ✅ requirement r : R { subject = x; }   （只绑 subject，完全不写 satisfy）
  ※ 靠「去掉限定名前缀」绕过无效：会报 Couldn't resolve reference，错误反而更多。
· 体内出现 stakeholder 或 actor 时，subject 必须写、且置于体内**第一个**参数位。

■ 类型族必须匹配（写错必报语义错）
· part / port / item 分别由 part def / port def / item def 定型，不可混用；
· 物理量写 attribute x :> ISQ::xxx；不要写成 attribute def X :> ISQ::某量；
· 所有"引用类"语法（satisfy / perform / allocate / include / render …）
  只能引用 **usage**，不能引用 **definition**。
"""


def build_l0_card() -> str:
    """返回待拼进建模提示词的 L0 硬约束卡文本。

    - `sysml.l0_card_enabled=False` → 返回空串（完全回到改动前行为）
    - `sysml.l0_card_extra` 非空 → 追加到卡尾（现场补充约束，不动代码）
    - 配置读取失败 → 用内置默认（不因配置异常丢约束）
    纯字符串拼接，无 DB / 无 IO，异常安全。
    """
    try:
        from core import config
        if not config.as_bool("sysml", "l0_card_enabled", True):
            return ""
        extra = config.get("sysml", "l0_card_extra", "") or ""
    except Exception:
        extra = ""
    extra = str(extra).strip()
    if not extra:
        return L0_CARD
    return L0_CARD + "\n■ 补充约束\n  " + extra.replace("\n", "\n  ") + "\n"
