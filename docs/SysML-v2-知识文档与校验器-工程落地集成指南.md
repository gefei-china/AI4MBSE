# SysML v2 知识文档 × 校验器 —— 工程落地集成指南

> **面向**：mbse_system 工程的 AI 建模环节。
> **回答两个问题**：① 这份知识文档在建模环节**怎么用**？② 和 `checker.jar` 校验工具**怎么配合**？
>
> **依据**：全部接入点都来自本工程实测（标注 `文件:行号`），不是设计构想。
> 配套文档：`docs/SysML-v2-AI建模知识文档.md`（v1.3，467 条实测用例）、
> `docs/V2代码自动校验与修复闭环-方案评估-20260918.md`（校验器契约与闭环设计）。

---

## 0. 先说结论

**当前工程有一个断层**：

```
LLM 生成 V2 代码 ──► 投影视图 ──► 版本入库 ──► 人工采纳 ──► 写智源（远程 check）
      ↑                                                          ↑
   无语法约束                                              唯一的语法检查在这里
```

- **生成端**只在 `prompt.py` 里写了约 200 字的笼统要求（`_build_model_code_req`），**知识文档里的 467 条实测规则一条都没进去**；
- **本地 `checker.jar` 在全部 Python 代码中零引用**（grep 实测，`core/deps.py` 里的 `checker` 是权限检查器，与 SysML 无关）；
- 唯一的语法检查在**最后一步**——写智源前调**远程** `zhiyuan_client.sysmlv2_check`（`norm_apply.py:500`）。

**后果（实测基线）**：`sysml_models/ev_thermal_mgmt/` 6 个文件在项目级合并口径下有 **ERROR 47 条（语法 33 / 语义 14）**，
根因是 **AI 在用 SysML v1 语法写 v2**（`connector … from … to`、`refines`、`traces`、`satisfaction`）。
这些错误**肉眼看起来全部像对的**。

> **这份基线已用本文档 §8.6 的脚本实测复现**（2026-09-19）：`rc=1 / ERROR 47 / 语法 33 / 语义 14 / WARN 48`，
> 与 `docs/V2代码自动校验与修复闭环-方案评估-20260918.md` §3.1 用另一套脚本得到的项目级口径**逐项一致**。
>
> **更关键的一条**：把这 14 条语义错逐条回溯后发现 —— **名字真的不存在的有 0 条**。
> 8 条是「名字明明在合并文本里、却报解析不到」，另 6 条是它们连带的。
> 即：**这 14 条语义错全是语法错的次生产物，真实的语义缺陷是 0 个。**
> 这直接决定了下面的门禁判据：**只对语法路设硬门槛。**

**落地思路**：把知识文档接到**生成端**（预防），把 `checker.jar` 接到**生成后**（检出），形成三道闸门。
不需要引入任何新依赖（只用 `subprocess` + `re`）。

| 闸门 | 位置 | 载体 | 成本 | 收益 |
|---|---|---|---|---|
| **① 生成约束** | `agent/pipeline_parts/prompt.py::_build_model_code_req` | 知识文档 **L0 硬约束卡** | 0（只加文本） | **最高**：从源头压掉 84 个语法错 —— ✅ **2026-09-19 已落地**，真机 A/B **两轮独立采样：无卡 6 / 9 ERROR → 有卡 0 / 0**（报告：《SysML-v2-生成端硬约束与向量化链路修复-实测报告-20260919》） |
| **② 生成后校验** | `agent/pipeline_parts/cards.py::_gen_sysml_views`（5 个调用点的收敛处） | 本地 `checker.jar` | 4–6 s / 次 | 高：错误在**入库前**暴露 |
| **③ 入库前留痕** | `agent/utils.py::_archive_sysml_version` | 校验结果写进 `element_summary` | 0 | 中：可追溯、可门禁 |
| （已有）**④ 写智源前** | `norm_apply.py::push_version_to_zhiyuan` | 远程 `sysmlv2_check` | 有网络/鉴权风险 | 保留，但**前置闸门应让它极少触发** |

---

## 1. 知识文档怎么"喂"给 AI —— 关键是**分层裁剪**

**不要整篇塞进 prompt。** 知识文档 v1.3 有 142 KB / 2400+ 行，全文进 prompt 会：
① 挤掉检索上下文与历史；② 稀释注意力（模型会挑最显眼的几条，而不是最相关的几条）。

按 **L0 / L1 / L2 三层**消费：

| 层 | 内容 | 体量 | 投放方式 |
|---|---|---|---|
| **L0 硬约束卡** | §1.3 编码 + §2.2 导入 + §1.7 关键字等价 + §5 前 6 条高频错误 + 类型族匹配 | **约 1.5 KB** | **每轮生成都注入**（写死在 `_build_model_code_req`） |
| **L1 领域速查** | 按意图取章节：需求→§4.11、结构→§4.6/§4.7、行为→§4.8/§4.9、变体→§4.13、用例→§4.12 | 3–5 KB / 意图 | 按 `intent` 选择性拼接 |
| **L2 全文** | 整份文档 | 142 KB | **不进 prompt**；走 RAG 检索或作为工具按需读取 |

### L0 硬约束卡（可直接复制进 `_build_model_code_req`）

```
【SysML v2 语法硬约束 —— 违反即无法通过校验器，必须逐条遵守】

■ 编码
  · UTF-8 无 BOM；标识符只能 ASCII（中文只能放进 doc /* */ 注释）
  · 文件头必须写包：package <Name> { ... }

■ 导入（可见性前缀是必填的）
  private import ScalarValues::*;      // Boolean / Integer / Real / String / Natural
  private import SI::*;                // [kg] [m] [s] [K] [W]
  private import ISQ::*;               // ISQ::mass / power / thermodynamicTemperature
  ※ 裸写 import X::*; 是语法错。

■ 高频错误对照（左错 → 右对）
  import X::*;                    →  private import X::*;
  requirement R2 refines R1;      →  requirement R2 :> R1;
  requirement R2 traces R1;       →  dependency from R2 to R1;
  connector c from a to b;        →  connect a to b;
  satisfaction satisfy R by x;    →  satisfy R by x;
  doc "文本"                       →  doc /* 文本 */
  a && b                          →  a & b
  part def 电池包                  →  part def BatteryPack   （中文放 doc /* 电池包 */）
  part def V { ... }              →  写真实体（`...` 不是合法记号）
  render MyRendererDef;           →  先 rendering r : MyRendererDef; 再 render r;
  extend X;                       →  v2 没有 extend，用 include use case X;

■ 类型族必须匹配（写错必报语义错）
  · part / port / item 必须分别由 part def / port def / item def 定型
  · 物理量写成 attribute x :> ISQ::xxx，不要写成 attribute def
  · satisfy 的 by 后面必须是 usage（不能是 def）
  · 需求细化用 :>，需求满足用 satisfy … by …，需求分配用 allocate … to …

■ TMS 常用骨架
  requirement def ReqName { subject s : SomeSys; doc /* 说明 */ }
  requirement r1 : ReqName;
  satisfy r1 by somePart;          // ✅
  allocate r1 to somePart;         // ✅
  requirement r2 :> r1;            // ✅ 细化
```

**这一段约 1.5 KB，但覆盖了实测 84 个语法错里的绝大多数类型。**

### L1 领域速查（按意图拼接）

```python
# agent/pipeline_parts/prompt.py 内可加：
_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "docs", "SysML-v2-AI建模知识文档.md")

_INTENT_SECTIONS = {
    "design":              ["§4.6", "§4.7", "§4.13"],   # 结构 / 端口连接 / 变体
    "requirement_analysis": ["§4.11"],                  # 需求
    "review":              ["§4.11", "§4.12"],
    "impact":              ["§3.3", "§4.11"],
}
```

> ⚠️ **实现提示**：按锚点 `### 4.11` 到下一个 `###` 之间切片即可（章节结构稳定）。
> 若不想动 prompt 代码，退一步的方案是**只上 L0**——它一项就能覆盖大部分语法错，且零解析成本。

---

## 2. 与校验工具的配合流程

### 2.1 全链路时序

```
① 用户提需求（AI 建模）
        │
        ├─► 知识文档 L0（+L1）：注入 system prompt ── 预防
        ▼
② LLM 输出 ```sysml 代码块
        │
        ├─► _extract_sysml_code()                    [cards.py:127]
        ▼
③ ★ 新增闸门：本地 checker.jar 项目级合并校验        ← 本指南的核心增量
        │
        ├─ rc=0 ────────────────► 投影视图（正常）
        ├─ rc=1 且 n_syntax=0 ──► 投影视图 + 语义错提示（不阻断）
        └─ rc=1 且 n_syntax>0 ──► 探测性修复 1 轮 ─┬─ 收敛 → 回 ③
                                                   └─ 未收敛 → 标记「待人工」
        ▼
④ _gen_sysml_views() → view_generator              [cards.py:242]
        ▼
⑤ _archive_sysml_version()：写 sysml_versions 版本链 [utils.py:67]
   └─ 把校验摘要写进 element_summary.check
        ▼
⑥ 人工采纳 /adopt → 自动收编                      [sysml_versions.py:75]
        ▼
⑦ /import-candidates 候选化                       [sysml_versions.py:106]
        ▼
⑧ /push-zhiyuan：远程 sysmlv2_check → 覆盖导入     [norm_apply.py:471]
   └─ 前置闸门若已生效，此处应当「一次通过」
```

### 2.2 三个接入点的具体改法

#### 接入点 ①（生成端，收益最高）

> ✅ **已落地（2026-09-19）**：实际实现与本指南的设想略有不同 —— 卡文本放在**独立模块**
> `agent/pipeline_parts/v2_constraints.py`（自带逐条出处表），`_build_model_code_req` 只做
> `+ build_l0_card()`；并加了配置开关 `sysml.l0_card_enabled` / `sysml.l0_card_extra`。
> 真机 A/B：**6 ERROR → 0 ERROR**（消除 `[S01]` 那类错）。
> 报告：`docs/SysML-v2-生成端硬约束与向量化链路修复-实测报告-20260919.md`

**改**：`agent/pipeline_parts/prompt.py::_build_model_code_req`

**做什么**：把上面 L0 硬约束卡作为返回值的一部分追加。

```python
def _build_model_code_req(self, intent: str) -> str:
    if not intent:
        return ""
    if intent in ("design", "requirement_analysis", "impact", "review"):
        pass
    elif "建模" in str(intent) or "sysml" in str(intent).lower():
        pass
    else:
        return ""
    return (原来那段建模输出要求
            + _V2_HARD_CONSTRAINTS)      # ← 新增：L0 硬约束卡
```

**为什么在这里**：`_build_model_code_req` 是**流式与非流式两条路径的唯一共用注入点**
（`stream.py:992`、`execute.py:215` 两处调用同一个方法）——改一处，两条路径同时生效。
这也正是该文件注释里说的「收敛为单一实现，避免口径漂移」。

**风险**：低。纯文本追加，无副作用；若担心 token，可把 L0 卡压到 1 KB 以内。

---

#### 接入点 ②（生成后校验，核心增量）

> ✅ **已落地（2026-09-19）**：实际实现与本指南设想**基本一致**，差异有三处 ——
> ① 校验模块落在工程根 `sysml_v2_check.py`（**已在仓库内，非一次性脚本**），
>    骨架取自知识文档 §8.6，并补了 hash 短路、可用性降级、三档 `verdict()`、`summarize()` 与 CLI；
> ② 调用点不是「`cards.py:259` 附近裸调」而是收敛成 `CardMixin._check_generated_sysml()`
>    （静态方法，**任何异常/缺件/超时都降级为 `None`**，保证不阻断主链路）；
> ③ 结果挂到 `views["check"]`（与既有 `quality_check` **同级**），并在接入点③ 写进版本留痕。
> 新增回退开关 `sysml.check_enabled` / `sysml.check_timeout`（默认开 / 90 s）。
>
> **实测（`tools/verify/verify_sysml_check_gate.py`，7 段 / 84 项断言全绿）**：
> 基线互证 `rc=1 / ERROR 47（语法 33 / 语义 14）/ WARN 48` 与知识文档 §8.6、方案评估 §3.1 一致；
> hash 短路 3.11 s → **0.0 ms**；`check_enabled=False` 时产物与改动前**逐字节一致**；
> AST 级证据：剔除注入的 **2 条语句**后新 `_gen_sysml_views` **AST == 旧 AST**。
>
> ⭐ **本指南没写到、但落地时必须知道的一条**：接入点② 用的是**单产物口径**，
> 而单文件口径有约 2/3 跨文件伪错。实测证明 **伪错几乎全落在「语义路」**（语法错与兄弟文件无关），
> 而门禁只认语法路 → **伪错不影响判定**。这正是「接入点②用单产物口径」与
> 「只对语法路设硬门槛」两条结论能同时成立的原因（实测：跨文件引用用例 `n_syntax=0` → `report` 不阻断）。
>
> 报告：`docs/SysML-v2-生成端硬约束与向量化链路修复-实测报告-20260919.md` §5.2

**改**：新增 `sysml_v2_check.py`（工程根），并在 `cards.py::_gen_sysml_views` 内调用。

**新增文件**：完整实现见 **知识文档 §8.6**（可直接复制）。

**接入位置**：`agent/pipeline_parts/cards.py:259` 附近，取出 `code` 之后、投影视图之前。

```python
code = self._extract_sysml_code(llm_content)      # [cards.py:259]
if not code:
    return None
# ★ 新增：校验（失败不阻断，只挂载诊断；保证建模主链路不受影响）
try:
    from sysml_v2_check import check_project          # 新增模块
    chk = check_project([tmp_write(code)])            # 单产物 → 单文件口径
    # 若有已入库的同工程模型，改为把那些文件一并传入 → 项目级合并口径
except Exception:
    chk = None
```

**为什么在 `_gen_sysml_views` 里而不是在 5 个调用点**：该方法有 **5 个调用点**
（`stream.py:598`、`stream.py:686`、`stream.py:1170`、`execute.py:363`、`orchestration.py:239`），
在方法内部改一处即可全覆盖，避免重复与遗漏（本项目有过「两函数同名并存被静默覆盖」的事故）。

**口径选择（重要）**：
- **单产物校验**（只校验刚生成的代码）→ 用它做**生成质量反馈**，快且轻；
- **项目级合并校验**（把 `sysml_models/<project>/*.sysml` 一起合并）→ 用它做**工程门禁**。
  ⚠️ 单文件口径会产生大量**跨文件伪错**（实测：同一工程单文件口径 137 错，合并口径只有 47 错，**约 2/3 是伪错**）。

**性能**：单次 4–6 s（库加载固定约 2.3 s）。建议 **hash 短路**：代码内容未变则复用上次结果。

**门禁判据（为什么只对语法路设硬门槛）**：

| 情形 | 处置 | 依据 |
|---|---|---|
| `rc == 0` | **放行** | 无 ERROR |
| `rc == 1` 且 `n_syntax == 0` | **报告但不阻断**，随产出交付给人 | 语法清零后语义错才**可信**；但很多语义错（如 `satisfy R by <part def>`）改文本解决不了，属建模决策，必须人判断 |
| `rc == 1` 且 `n_syntax > 0` | **阻断，先修语法** | 语法未清零时语义错**全部是假的**（实测 14 条里真实缺陷 0 条）；且语法错会**级联污染命名空间** |

> ⚠️ **不要用「ERROR 总数下降」判定修复有效**。实测反例：某文件删掉非法前缀后 ERROR **19 → 37（变多）**——
> 因为语法修好后，原本被**遮蔽**的语义错才暴露出来。**只对 `n_syntax` 要求单调严格下降。**

---

#### 接入点 ③（入库留痕）

> ✅ **已落地（2026-09-19）**：`agent/utils.py::_archive_sysml_version` 里把 `views["check"]`
> 写进 `summary["check"]`（即 `element_summary.check`）；字段除本文设想的前 5 个外，
> 另加 `verdict`（`pass|report|block|unavailable`）/ `blocked` / `n_warn` / `scope` / `top`（前 5 条诊断）/ `at`。
> 摘要由 `sysml_v2_check.summarize()` 产出（**截断到 5 条**，避免 `element_summary` 膨胀）。
> 实测：`/api/sysml-versions/{id}` 与列表接口都能读出来；语法错版本 `check.blocked is True` 可追溯。
> ⚠️ 本轮**只留痕、不阻断**：`blocked` 是给下游（采纳 / 推送前闸门）消费的**标记**，
> 实际阻断点仍按接入点④ 的分工留在「写智源前」——因为生成端阻断会丢产物。

**改**：`agent/utils.py::_archive_sysml_version`（`utils.py:67`）。

**做什么**：把校验摘要写进 `element_summary` 的 `check` 字段，随版本链一起走。

```python
es["check"] = {"rc": chk["rc"], "n_error": chk["n_error"],
               "n_syntax": chk["n_syntax"], "n_semantic": chk["n_semantic"],
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
```

**价值**：版本详情接口 `/api/sysml-versions/{id}` 本来就会 `json.loads(element_summary)` 返回，
**前端零改动即可显示**；也让「这个版本当时合不合法」可追溯。

---

#### 接入点 ④（已有，保留）

`norm_apply.py::push_version_to_zhiyuan`（`norm_apply.py:471`）在写智源前调**远程** `sysmlv2_check`，
且已区分 `CALL_FAILED`（鉴权/网络）与 `CHECK_FAILED`（真的不合法），这块设计是好的。

**建议**：**不要用本地 checker 替换它**，两者职责不同：

| | 本地 `checker.jar` | 智源 `sysmlv2_check` |
|---|---|---|
| 时机 | 生成后立刻 | 写入前 |
| 依赖 | 离线、零配置 | 需要 `base_url` + `token` |
| 作用 | **早发现** | **最后一道保险**（也校验智源侧的上下文） |

前置闸门做好后，第 ⑧ 步应当**几乎不再失败**——那正是前置闸门的价值。

---

## 3. 落地清单（按优先级）

| 顺序 | 动作 | 文件 | 改动量 | 预期效果 | 状态（2026-09-19） |
|---|---|---|---|---|---|
| **P0** | 注入 L0 硬约束卡 | `agent/pipeline_parts/prompt.py` | +1 常量 +1 行拼接 | **语法错从源头下降**（本项目实测：AI 主因是用了 v1 语法，L0 卡直接点名这些写法） | ✅ 已落地（实际落在 `v2_constraints.py`） |
| **P1** | 新增校验模块 | 新建 `sysml_v2_check.py`（抄 §8.6） | +1 文件 | 获得可复用的门禁能力 | ✅ 已落地 |
| **P2** | 接到视图生成前 | `agent/pipeline_parts/cards.py` | +6 行 | 错误在入库前可见 | ✅ 已落地（注入 2 条语句） |
| **P3** | 校验摘要入版本 | `agent/utils.py` | +5 行 | 可追溯、前端可显示 | ✅ 已落地 |
| **P4** | （可选）确定性修复 | 新增规则模块 | 中 | 实测 3 条规则可消 91% 语法错 | ⏸ 未做（见下注） |
| **P5** | （可选）L1 按意图注入 | `agent/pipeline_parts/prompt.py` | +20 行 | 领域写法更准 | ⏸ 按报告建议先观察 |

> ⚠️ **P0 / P1 是性价比最高的两步**，建议先只做这两步并观察一段时间，再决定是否上 P4 的自动修复。
>
> 📌 **实际节奏（2026-09-19）**：P0 落地当天就把 **P1/P2/P3 一并做了**（三者加起来约 200 行 + 1 个 84 项断言的自检脚本），
> 因为「让错误在入库前暴露」这一件事单独做 P1 没有意义；**P4 自动修复仍刻意不做** ——
> 它会自动改写代码、掩盖真实建模缺陷（边界见 §5），且 §8.6 的预算门控（`n_syntax` 必须严格下降、
> 否则立即停）需要有 P2/P3 的留痕才能观测效果。**P5 按报告建议继续观察。**

---

## 4. 怎么验证接入真的有效（A/B 对拍）

**不要用「感觉变好了」当结论。** 用同一批输入做对拍：

```
① 固定一批建模需求（建议直接用 sysml_models/ev_thermal_mgmt/ 那 6 个文件的原始需求）
② 关闭 L0 卡跑一遍 → 收集产出的 V2 代码 → check_project → 记 (n_syntax, n_semantic)
③ 开启 L0 卡跑同一批 → 同样记录
④ 对比 n_syntax：必须显著下降；若没降，说明卡没写对（而不是"AI 不行"）
```

**三条判读纪律（来自实测教训，别踩）**：

1. **不能用 ERROR 总数判断改善** —— 语法错会**遮蔽**语义错。实测：某文件删掉非法前缀后 ERROR **19 → 37（变多）**，
   因为语法修好后引用解析才真正开始执行。**必须分语法/语义两路计数，只对语法路要求下降。**
2. **必须用项目级合并口径** —— 单文件口径有约 2/3 是跨文件伪错。
3. **不能只测一两个样本** —— 至少要跑一遍完整的 `sysml_models/` 目录，才有统计意义。

---

## 5. 边界：什么**不要**做

| 边界 | 理由 |
|---|---|
| **不改写已入库的 `sysml_models/`** | 那是模型资产。自动改写等于篡改——校验对已入库模型**只报告**。（本指南所有实验都在 `tmp/` 副本上做） |
| **语义错不自动修** | 很多语义错（如 `satisfy R by <part def>` 需改成 usage）**改文本解决不了**，得引入新元素——那属于建模决策，必须交人 |
| **校验器不能替代工程评审** | 100% 通过校验的模型**仍可能是业务上错误的热管理设计**。本闭环只管「代码合不合法」，不管「设计对不对」 |
| **不要靠 prompt 里写「请注意语法正确」** | 实测反证：现有代码 84 个语法错，**肉眼看起来全部像对的**。必须是**具名的具体规则**（L0 卡就是干这个的） |

---

## 6. 一页速查

```
知识文档（v1.3，467 条实测）
   ├─ L0 硬约束卡  ──► 注入 _build_model_code_req        【每轮生成】
   ├─ L1 领域速查  ──► 按 intent 拼 §4.x                  【可选】
   └─ L2 全文      ──► 不进 prompt，走检索                 【按需】

checker.jar（本地，离线，4–6 s）
   ├─ sysml_v2_check.check_project()  ── 项目级合并 + 双路计数
   ├─ 接 _gen_sysml_views  ──────────► 生成后立刻校验
   ├─ 接 _archive_sysml_version ─────► 校验摘要进版本链
   └─ 门禁：rc=1 且 n_syntax>0 → 阻断；只有语义错 → 报告不阻断

智源 sysmlv2_check（远程）
   └─ 保留在 push_version_to_zhiyuan 作为最后一道保险

铁律
   ① 分语法/语义两路计数（ERROR 总数会反向上升）
   ② 门禁用项目级合并口径（单文件 2/3 是伪错）
   ③ 已入库模型只报告不改写
   ④ 校验器不替代工程评审
```

---

*本指南的接入点行号取自 2026-09-19 当前工作区（未提交状态）。若相关文件已重构，请以实际代码为准重新定位。*
