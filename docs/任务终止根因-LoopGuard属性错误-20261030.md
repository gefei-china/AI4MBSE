# 「任务终止 / 本轮未产出正文内容」根因盘点（2026-10-30 18:11）

## 1. 结论（一句话）

不是模型能力、不是网络、也不是本轮建议 1–6 的改动 —— 是 **2026-10-28 引入的一行调用形态错误**：
把**模块级纯函数** `is_correctable_failure` 当成 `LoopGuard` 的**实例方法**调用。
它只在「有工具调用失败」时触发，一触发就 `AttributeError` 掀翻整轮，用户侧显示「已终止」。

## 2. 现场证据（可直接复核）

**服务日志**（`logs/service.log`）：

```
[stream_error] conv=9779 type=AttributeError msg='LoopGuard' object has no attribute 'is_correctable_failure'
Traceback (most recent call last):
  File "...\agent\pipeline_parts\stream.py", line 5397, in execute_stream
    elif _lg.is_correctable_failure(result):
AttributeError: 'LoopGuard' object has no attribute 'is_correctable_failure'
[_persist_partial_stream] reason=error conv=9779 候选字符=0 force=True
[_persist_partial_stream] 无正文但 force=True ⇒ 落一条失败说明
```

**触发它的工具回执**（`tool_call_logs` id=6077，时间 10:11:59 UTC = 18:11:59 本地）：

| 字段 | 值 |
|---|---|
| tool | `query_data_tree`（MCP `zhiyuan-dev-mcp`） |
| ok | 0 |
| failure_kind | `input_invalid` |
| result | `{"code":"INVALID_ARGUMENTS","message":"参数不符合工具定义；按 fields 提示修正字段后重新调用","retryable":false}` |

**用户可见产物**（`messages` id=5262）：

> ⚠️ 本轮未产出正文内容
> **原因**：AttributeError：'LoopGuard' object has no attribute 'is_correctable_failure'

**运行 span**（`orch_run_spans` id=2892）：`status=error`，`reason=no_terminal_frame`，`last_event=error`，耗时 42s。

## 3. 为什么是这几行字

`agent/pipeline_parts/stream.py:5391-5398`（及 `execute.py:723` 同构）：

```python
# 参数类错误（INVALID_ARGUMENTS / recovery.action=CORRECT_ARGUMENTS）是
# "按清单改就能对"的信息性结果，不该扣失败轮 —— 否则整轮被判死。
if _t_ok and not (...):
    _round_had_ok = True
elif _lg.is_correctable_failure(result):     # ← 这里：_lg 是实例，函数是模块级的
    _round_had_ok = True
    _fix_hint = "⚠️ 上一次调用是**参数不合法**（不是工具坏了）……"
```

- `agent/loop_guard.py` 里 `is_correctable_failure` 是**模块级 def**（`__all__` 也导出了它），
  `LoopGuard` 类**没有**同名方法 ⇒ 属性访问必然 `AttributeError`。
- `git blame` 指向 `78dae8d`（2026-10-28），从落地那天起就是坏的。
- 它藏了 2 天不被发现，是因为 `elif` 分支**只有工具失败时才走**：
  正常的成功轮永远不经过它 —— 也就是说，**它只在最需要它救命的那一刻炸**。
- 讽刺之处：这段代码的**全部目的**就是「参数传错时给模型第二次机会」，结果它自己先把整轮打死。

## 4. 影响面（不是一次偶发）

| 维度 | 数值 |
|---|---|
| 用户可见失败 | **3 次**（conv 9780 一次、conv 9779 两次；今日 15:15 / 16:05 / 18:11） |
| 日志中实际触发 | **5 次**（另有 4 次 `conv=0`，跑在后台/续跑路径，被上层吞掉、用户看不见） |
| 触发条件 | 任意一次工具调用返回 `ok=0`（参数错、超时、网络错皆可） |
| 命中路径 | `execute_stream`（流式，主对话）+ `execute`（同步，编排/后台） |

同一行错误在两个地方各有一份，所以两条 ReAct 路径**同时**中招。

## 5. 修复

1. **`agent/pipeline_parts/stream.py`**：改直调模块函数
   `from agent.loop_guard import (LoopGuard as _LoopGuard, is_correctable_failure as _is_correctable_failure)`，
   分支改为 `elif _is_correctable_failure(result):`。
2. **`agent/pipeline_parts/execute.py`**：`from agent.loop_guard import LoopGuard, is_correctable_failure`，
   分支改为 `elif is_correctable_failure(result):`。
3. **`agent/loop_guard.py`**：类上补 `is_correctable_failure = staticmethod(is_correctable_failure)`
   作为**兜底垫片** —— 任何遗漏/第三方写法退化为"正常返回布尔值"，而不是把用户的一轮打成异常。
   它是 `staticmethod`（不是复制逻辑），判据仍只有一份。

## 6. 防复发（两道，分工明确）

| 门禁 | 证明什么 | 位置 |
|---|---|---|
| **G7（AST 级，进 CI）** | 调用点必须是**模块函数直调**；且该名字在所在函数作用域已 import。属性访问 / 漏 import 一律判红 | `tools/verify/verify_loop_guard.py` G7a–G7e |
| **M6 变异自证** | 把调用点改回属性访问 ⇒ G7 判红；同时断言"文本包含式检查仍全绿"，说明为何必须 AST | 同上 |
| **E2E（真链路，本机真库）** | 参数错误后：无异常、模型拿到第二次机会、修正提示进入模型上下文、本轮产出正文 | `tools/verify/verify_guard_arg_fix.py` F1–F4 |

**为什么原来的 G6 没挡住**：G6a/G6f 只检查"文件里出现过 `from agent.loop_guard import`"和
"模块里 `def` 过这些名字"——都**不判断怎么调用**。文本包含式检查对"调错形态"天然失明。

**为什么 G7 用 AST 而不是 grep**：M6 变异实测，改回属性访问后文本检查依然全绿（`text_check_pass=True`），
只有 AST 能看见 `Attribute(attr="is_correctable_failure")` 这个形态。

## 7. 顺带修掉的一个既存红点

`verify_loop_guard.py` 的 M5 变异**在本次改动前就已经是红的**（HEAD 上 41 断言 1 FAIL）：
2026-10-28 把 `tokens_of` 改成"计费等效口径"后，变异锚点 `return int(meta.get("token_count") or 0)`
已不在该函数里（只剩在 `tokens_total_of`），变异打偏 ⇒ 变成**空转变异**。
本次改锚到 `return charged_tokens(resp)`，M5 恢复可判红。现门禁 **47 PASS / 0 FAIL**。

## 8. 服务状态

已用修复后的代码重启（PID 127016，`127.0.0.1:8000`），
`/api/health/live` = 200、`/api/health/ready` = `{"status":"ready", ...}`。

**注意**：18:11 那一轮的失败消息已在库里（`messages` 5262），属历史事实，不回改。
重新发一次相同请求即可，本轮不会再因这条路径终止。
