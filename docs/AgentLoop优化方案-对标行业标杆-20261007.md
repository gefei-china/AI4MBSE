# Agent Loop 优化方案（对标行业标杆）— 2026-10-07

> **基线**：`ec49304`（工作区干净，未提交任何改动）
> **输入**：《从 Pi 中，我领悟了 Agent Loop 的绝对奥义》（战场小包 2026-09-21）
> \+ 行业标杆调研（Claude Code / Codex / DeepSeek Harness / OpenAI SDK / Agent SDK 官方文档）
> **纪律**：本方案所有判断均经**回库取证**；未实测的一律标注「待实测」，不写凭印象值。

---

## 0. 结论先行

**"稳"已达标，"顺"与"健壮"有缺口。** 本仓三道护栏（token 预算 / 重复调用短路 / 工具超时）
与 Pi 同级甚至更强（Pi 无护栏概念），但调研发现的行业共识里有 **3 条硬约束本仓没满足**，
其中 **1 条是必现的协议级缺陷**。

| 优先级 | 项| 行业依据 | 本仓现状 | 性质 |
|---|---|---|---|---|
| **P0** | **并行 tool_calls 配对缺口** | Anthropic/OpenAI 协议硬约束 | `ptool_calls[:3]` 切片 ⇒ **第 4 笔起永久悬空 ⇒ 下一轮 400** | 🔴 必现 |
| **P0** | 截断时补`tool_result` | Pi / DeepSeek Harness / 4 篇独立文档 | `except: targs={}` 后照常执行 | 🔴 已发生 2 次 |
| **P1** | 达到上限时的收尾 | OpenAI SDK 官方：「不要静默出错，追加一次总结调用」 | 直接跳出，用户拿到半成品 | 🟡 |
| **P1** | steering / follow-up 双层Loop | Pi / Codex / Decoding AI 三家一致 | 全仓零命中 | 🟡 |
| **P2** | turn 间运行时热更新 | Pi `prepareNextTurn` | `effective_provider` 循环外定死 | 🟡 |
| **P2** | 错误结构化 + 连续计数 | n4n 工程实践 | 错误已回传但无连续计数 | 🟢 |

---

## 1. P0-①：并行 tool_calls 的配对缺口（**必现，最高优先**）

### 1.1 行业依据（三家一致，且是协议层硬约束）

| 来源 | 原文要点 |
|---|---|
| Anthropic 官方 | 「每个 `tool_use` block 必须有对应的 `tool_result` block在**紧随的下一条** user message，**中间不能有任何其他内容**」|
| Kunavo 故障指南 | 配对失败的典型报错：`HTTP 400 tool_use ids were found without tool_result blocks immediately after` |
| itsourcecode | 「**工具报错也必须发** `tool_result`（`is_error: true`）—— 模型能完美处理失败的工具，**处理不了缺失的**」 |
| OpenAI 官方模式 | `messages.append(assistant_message)` 后**必须**紧跟 `role='tool'` 且 `tool_call_id` 精确匹配 |

> ⚠️ 一篇中文技术文档特别指出：**「JSON 补全后甚至能通过 schema 验证，但语义字段可能已缺失」**
> ⇒ 这是**协议层拒绝**，不是"宽容处理"，无法靠补参数绕过。

### 1.2 本仓缺陷（回库取证）

```python
# agent/pipeline_parts/stream.py:1693
for _ti, tc in enumerate(ptool_calls[:3]):   # ★ 只执行前 3 笔
    ...
    results.append({"tool_call_id": tc.get("id", ""), "role": "tool", ...})
...
messages.append(pmsg)      # ★ pmsg 里带着【全部】ptool_calls
messages.extend(results)   # ★ 但只有前 3 笔的结果
```

**`pmsg`（assistant 消息）携带 N 笔 `tool_calls`，而 `results` 只有 3 笔**
⇒ 第 4 笔起**永久悬空** ⇒ 下一轮请求 **HTTP 400**。

`execute.py:350` 有同样的 `tool_calls[:3]` 切片 ⇒ **两条路径同病**。

### 1.3 触发条件（实测：不是边缘情况）

```
tools表 active 工具数 = 33
⇒ 一次给模型 33 个工具时，并行请求 4~6 笔是**常态**，不是异常
```
对照Pi：它对 `toolCalls` **全量遍历**（`message.content.filter(c => c.type === "toolCall")`），
**没有 `[:3]` 这种切片** —— 这正是我们与 Pi 的实质差距。

### 1.4 修法（两处，语义不同）

**不能简单改成"全量执行"** —— 那会放开单轮工具数上限（失控形态）。
正确做法：**执行仍限 3 笔，但被截断的尾部必须补"未执行"结果**：

```python
_MAX_TOOLS_PER_ROUND = 3          # 提为模块常量，两路径共用
_executing = ptool_calls[:_MAX_TOOLS_PER_ROUND]
_skipped  = ptool_calls[_MAX_TOOLS_PER_ROUND:]     # ★ 尾部

for _ti, tc in enumerate(_executing):
    ...  # 现有逻辑不动

# ★ 为每个未执行的调用补一条结果（协议配对 + 让模型知道"没执行"）
for tc in _skipped:
    results.append({
        "tool_call_id": tc.get("id", ""),
        "role": "tool",
        "name": (tc.get("function") or {}).get("name", ""),
        "content": json.dumps({
            "ok": False,
            "result": (f"本轮工具并发数已达上限（{_MAX_TOOLS_PER_ROUND}），"
                       f"「{name}」未被执行。**不要重试同一个调用**——"
                       f"请基于已有信息回答，或下轮改用其他工具。"),
            "skipped": True, "skip_reason": "round_tool_limit",
        }, ensure_ascii=False),
    })
```

⚠️ **两个细节不能省**：
1. `tool_call_id` 必须**逐字回显** `tc["id"]`，不能重新生成；
2. 补的结果**必须进 `messages`**（`messages.extend(results)` 已覆盖），
   否则配对仍缺失。

### 1.5 验收判据

-构造 **5 笔** tool_calls 的夹具 ⇒ `results` 长度 == 5，`tool_call_id` 集合与 `ptool_calls` **完全一致**；
- 两处（stream / execute）**同口径**；
- 变异自证：把补结果那段删掉 ⇒ 门禁判红（因为配对断言会 FAIL）。

---

## 2. P0-②：截断时必须补「你截断了，重发」

### 2.1 三家标杆的做法（**这里有分歧，必须按业务选**）

| 方案 | 做法 | 适用 |
|---|---|---|
| **Pi** | `stopReason==="length"` 且含toolCall ⇒ 给**所有** toolCall 造错误结果，**不执行**，交回模型 | 只读/研究型 Agent（模型下一 turn 自行重发） |
| **DeepSeek Harness** | `max-tokens` **整轮收口**，且**在工具解析之前返回**（即使响应含 toolCall 也不执行）；恢复需外部策略参与 | **有写操作的 Agent**（CSDN 原文明确推荐此项） |
| OpenAI SDK 模式 | 显式设 `max_tokens` + 检查 `finish_reason`；命中 `length` 时追加"请给最终答案" | 通用 |

> ★ **DeepSeek Harness 的 max-tokens 具有「粘性」**：某 step 触顶后，
> 即使后续 step 正常完成，最终 turn/end 仍保留 max-tokens 标记 ⇒ 运维侧可准确识别。

### 2.2 本仓现状与危害

```python
# stream.py:1701 / execute.py:354
try:
    targs = json.loads(fn.get("arguments") or "{}")
except Exception:
    targs = {}          # ★ 静默降级 → 照常以空参数执行工具
```

**三重危害**（比崩溃更坏）：
1. 工具以**空参数**执行 → 或报"缺参数"，或**误用默认行为**（更危险）；
2. 模型**收不到"参数被截断"信号** → 不会重发，把失败当结论继续推理；
3. 空参数看起来像"**用户没传**" ⇒ 静默掩盖了"模型输出不完整"这个真因。

**实战已发生 2 次**：`llm_usage_stats` 里`finish_reason='length'` 两行
（`#7843 ct=4096`、`#7918 ct=32768`）。

### 2.3 修法（本仓取 Pi 方案，但加一道 DeepSeek 式兜底）

**为什么不直接抄 DeepSeek 的"整轮收口"**：本仓 Agent **有写操作**
（`side_effect='write'` 7 个 / `destructive` 1 个），
但也有大量只读工具；整轮收口会让"只是输出超长"的场景**也要人工重试**。
⇒ 采用**分级策略**：

```python
# 新增：判别"参数是否真被截断"而非"只要解析失败就算"
def _is_truncated_args(fn) -> bool:
    """模型输出被 max_tokens 截断 ⇒ tool_call.arguments 是残缺 JSON。"""
    a = (fn or {}).get("arguments") or ""
    if not a.strip():
        return True# 空 = 残缺
    try:
        json.loads(a)
        return False          # ★ 合法 JSON ⇒ 不是截断（可能是模型真发了空参数）
    except Exception:
        return True           # 解析失败 ⇒ 残缺

# 命中截断 ⇒ 不执行，造「你截断了，重发」
if _is_truncated_args(fn):
    result = {"ok": False, "error": "args_truncated",
              "result": "本次工具调用的参数因模型输出超出长度上限而被**截断**，"
                        "已拒绝执行（避免用不完整参数产生错误副作用）。"
                        "请**缩短描述后重新发起一次完整调用**。"}
    # ★ 关键：仍然要回喂 tool_result（协议配对），见 P0-①
```

⚠️ **必须区分两种失败**（这是 Pi/DeepSeek 都没讲透的一点）：
- `arguments` **非法 JSON** ⇒ 截断 ⇒ 拒绝 + 告知重发；
- `arguments` **合法但为空 `{}`** ⇒ **可能是模型真发了空参数**（合法请求）
  ⇒ 这时**不该**当成截断，应交给工具自己的参数校验报错处理。

⚠️ 再加一道 **DeepSeek 式收尾**：本轮若出现过截断，
在循环结束前 yield 一条 `reasoning` 事件说明"本轮因输出上限被截断"，
让运维在 `llm_usage_stats` 之外还有前端可见的痕迹。

---

## 3. P1-①：达到上限时的收尾（别静默返回半成品）

### 3.1 行业依据

OpenAI SDK 文档原文：
> 「When you hit the cap, **do not silently error out** — append a message asking for a
> best-effort summary and **make one final call without tools**. The model gets to synthesise
> whatever it gathered rather than returning nothing.」

本仓现状：`max_tool_rounds=3` 用尽后 → `if tool_injected and not llm_content: 兜底流式生成`。
⇒ **只有"一个字都没产出"才兜底**；若已有部分内容但未收尾，**直接返回半成品**。

### 3.2 修法（最小改动）

```python
# 循环用尽后：若本轮发生过工具调用且已有部分输出 → 追加一次「无工具」收尾
if _rnd >= max_tool_rounds - 1 and ptool_calls:
    _msgs = messages + [{
        "role": "user",
        "content": "本轮工具调用次数已达上限。请**基于已获得的信息**给出完整结论，"
                   "不要再调用任何工具。",
    }]
    #★ 只补一次，不再进循环（防"收尾又触发工具"的死循环）
```

⚠️ 成本控制：这次调用**不带 `tools`** ⇒ 模型物理上无法再请求工具。

---

## 4. P1-②：steering / follow-up 双层 Loop

### 4.1 三家标杆的实现要点（**这是本方案里最难的一块，务必看清**）

| 来源 | 关键设计 |
|---|---|
| **Pi** | 内层循环在**每个 turn 后**轮询 steering；外层循环在**准备退出时**读 follow-up |
| **Codex** | 三层嵌套状态机（`run_turn` / `run_sampling_request` / SSE 事件循环）；`needs_follow_up` + `has_pending_input` 双向制衡；`steer_input` **不含 cancel**（真正打断是独立的 `Op::Interrupt`） |
| **Decoding AI** | **只在下边界注入**（`MODEL_REQUEST`前注 steering，`WOULD_STOP` 前注 follow-up）—— 立即注入会破坏当前 tool_call |
| **Codex（防滥用）** | 停止 hook 续跑请求带"**已否决过一次**"标志，防死循环；**只喊否决不给续跑指令的，警告后忽略** |

> ★ **Codex 的哲学值得抄**：「把"任务做完了吗"的判断权交给模型，
> 把"还有没处理的输入"的否决权交给 harness。**模型提议，harness 制衡。**」

### 4.2 本仓现状

`grep -rn "steering\|follow_up\|followup" agent/` → **零命中**。

### 4.3 分阶段修法（**不要一次做完**）

**阶段 1（最小可用）：只做 follow-up，不做 steering**

理由：follow-up 只影响"下一轮做什么"，不触碰正在执行的 tool_call，
**风险最低、收益最直接**（用户不用等Agent 干完才能追问）。

```python
# 落点：agent/pipeline_parts/stream.py 的 SSE 生成器外层
# 现状：SSE 是一次性生成器，跑完即结束 ⇒ 需要一个"待处理输入"队列
self._pending_followup: list[str] = []

# 路由侧：POST /api/agent/followup 把消息投进队列（按 conversation_id 索引）
# 循环退出前：
if self._pending_followup:
    messages.append({"role": "user", "content": "\n".join(self._pending_followup)})
    # ★ 重启内层循环（不是 continue 工具调用，是开启新一轮）
```

**阶段 2：steering（需谨慎，先确认注入时机）**

⚠️ **本仓是 SSE 流式 + 工具执行在同一个生成器里**，
注入时机比 Pi 复杂（Pi 的 tool_call 是挂起 future，边界清晰）。
**建议**：先只在**工具执行完成后**（`results` 已append 之后）注入 steering，
不要在工具执行**中间**注入。

**阶段 3：中断（abort）**

Codex 的做法值得抄：中断时**正在跑的工具各自体面收场**，
被中止的工具把 `aborted by user` **写回历史**，中断本身只追加一条标记
⇒ 轮次进入 `Interrupted`（**可恢复的非终态**）。

本仓现状：需确认是否有中断入口。**待实测**（见 §6）。

---

## 5. P2：运行时热更新 + 错误结构化

### 5.1 turn 间热更新

Pi 的 `prepareNextTurn` 每轮刷新 `model` / `thinkingLevel` / `tools` 快照
（⚠️ 它特别强调 `tools.slice()` —— 不复制的话后续对 `state.tools` 的增删会泄漏进已发出的快照）。

本仓：`effective_provider` 在 `stream.py:1267`（**循环外**）赋值，循环内只读
⇒ 即使配置里有 provider 选择，**运行中也改不了**。

**修法（低成本部分）**：把 provider 解析挪进循环内，每轮重新读
「本轮覆盖值 → 会话默认 → 意图路由」，并支持运行中通过 SSE 控制指令写入覆盖值。
⚠️ **暂不做** `tools` 热更新（工具集变更涉及前端节点复用，改动面大，见 §6待定项）。

### 5.2 错误结构化 + 连续失败计数

n4n 工程实践：**连续 3 次相同错误 ⇒ 强制收尾**（而不是让模型无限重试）。

本仓现状：错误已回传给模型（`{"ok": False, "result": ...}`），
但 `loop_guard` 的 `dedup_hit` **只对"成功结果"计数**（`mark_done(weak=...)`），
**没有"连续错误"这一维**。

**修法**：`LoopGuard` 增加 `error_streak`；连续同类错误达 3 次 ⇒ 注入
`"该工具已连续失败 N 次，请停止重试并改用其他方式"`。

---

## 6. 明确不做 / 待实测（**避免误操作**）

| 项 | 状态 | 理由 |
|---|---|---|
| **改 `max_tool_rounds=3`** | ❌ **不做** | 与编排侧 6 任务 × 20 万 token 不对称，但换来失控形态可控。**改了会同时放大 P0-① 的影响面** |
| **放开单轮工具数上限** | ❌ **不做** | 方案 1.4 明确"执行仍限 3 笔，只补尾部结果" |
| **`tools` 热更新** | ⏸ **待定** | 涉及前端节点复用（`_call_id` 机制），改动面未量化 |
| **中断（abort）现状** | 🔍 **待实测** | 需确认 SSE 层是否已有中断入口；若无需先做 |
| **参数化记忆 / 记忆 OS** | ❌ 不做 | 见记忆方案 §8 |
| **DeepSeek 式整轮收口** | ❌ 不做 | 本仓只读/写工具混合，采用**分级**（§2.3）而非一刀切 |

⚠️ **误操作防范（三条硬要求）**：
1. **P0-①的 `[:3]` 切片不能直接删** —— 会放开单轮上限。必须"限执行 + 补结果"。
2. **截断判定不能只看"解析失败"** —— 合法空参数不是截断（§2.3）。
3. **每个阶段独立提交 + 独立门禁** —— 不要把 4 个改动塞一个提交，
   否则出问题无法二分定位（这也是本仓 10-07 两笔提交的做法）。

---

## 7. 实施顺序与验收

| 批次 | 内容 | 验收门禁 |
|---|---|---|
| **第1 批** | P0-① 并行配对 + P0-② 截断拒绝 | 新增 `verify_agent_loop_protocol.py`：<br>① 5 笔调用 ⇒ 5 条结果、id 集合完全一致<br>② 尾部结果带 `skipped` 标记<br>③ 截断夹具 ⇒ 不执行工具 + 回喂截断提示<br>④ 合法空参数**不**判为截断（防过度拒绝）<br>⑤ 变异：删掉补结果段 ⇒ 判红 |
| **第 2 批** | P1-① 上限收尾 | 追加：轮次用尽时有一次**无 tools** 的收尾调用 |
| **第 3 批** | P1-② follow-up（阶段 1） | 追加：队列有消息时循环重启；无消息时行为逐字不变 |
| **第 4 批** | P2 错误结构化 | 追加：`error_streak` 达3 ⇒ 注入停止重试提示 |
| （可选） | P2 热更新 / steering 阶段 2-3 / 中断 | 先量化改动面再决定 |

**每批的通用验证**（本仓既有基线，2026-10-07 实测）：
```
门禁 263/263 · pytest 34 passed + 1 skipped (rc=0) · 记忆评测 15/15
```

---

## 附 · 关键落点索引（已机器抽验）

| 改动 | 文件:行 |
|---|---|
| 并行切片（stream） | `agent/pipeline_parts/stream.py:1693` |
| 并行切片（execute） | `agent/pipeline_parts/execute.py:350` |
| 截断降级（stream） | `agent/pipeline_parts/stream.py:1701` |
| 截断降级（execute） | `agent/pipeline_parts/execute.py:354` |
| 工具结果回填 | `agent/pipeline_parts/stream.py:1760-1764` |
| 轮次上限 | `agent/pipeline_parts/stream.py:1649` |
| 护栏状态机 | `agent/loop_guard.py:144`（`LoopGuard`） |
| provider 赋值（循环外） | `agent/pipeline_parts/stream.py:1267` |

## 附 · 调研来源

- 《从 Pi 中，我领悟了 Agent Loop 的绝对奥义》战场小包 2026-09-21（原始输入）
- Anthropic Agent SDK 官方文档 · agent-loop 页（turn/预算/消息类型）
- Anthropic 官方：`stop_reason` 三值与 `max_tokens` 的生产处理
- DeepSeek Harness vs Pi 的循环结束判断对比（CSDN 2026）
- Codex 源码系列：turn 生命周期 / 三层嵌套状态机 / needs_follow_up
- Decoding AI：The Bare-Bones Coding Agent Loop（steering/follow-up 注入时机）
- Kunavo：Claude API 400 `tool_use ids were found without tool_result blocks`
- n4n：Debugging a ReAct agent that won't stop reasoning（错误结构化 + 连续计数）
