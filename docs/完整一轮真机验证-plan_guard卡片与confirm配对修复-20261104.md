# 完整一轮真机验证：plan_guard live 卡片值达成 + confirm 配对 bug 修复

> 日期：2026-11-04
> 前情：`计划语义校验-实施与验证-20261104.md`（遗留：plan_guard 的 live 卡片值需完整跑一轮 >480s 编排）
> 本文 = 完整一轮真机编排 + 由此挖出并修复的一个潜伏 P1 bug。

---

## 0. 结论先行

| 结论 | 依据 |
|---|---|
| ★ **遗留项关闭**：`card_data.plan_guard` live 卡片值真实存在 | conv 990403（真实 LLM + 真实 SSE 通道，1059s / 2120 事件） |
| ★ 完整一轮**挖出并修复一个潜伏 P1**：写前确认闸（HITL）与其它工具同轮时，悬空 tool_call ⇒ 后续所有 LLM 调用 400 ⇒ 子任务零产出失败 | t2 两次尝试死于完全相同的位置；根因链完整闭合 |
| 编排级 ReAct 全链路真机留痕 | `replan_decisions` 6 条：health×3 / T1 retry / 无进展防护 / stall 观测 |
| 修复后全量回归绿灯 | 13 门禁全绿（含新 gate 12/0）+ pytest 183 通过 + 服务已带修复重启 + 冒烟两路径通过 |

---

## 1. 完整一轮真机编排（conv 990403）

目标：`对某型无人机地面控制站做需求条目化和架构骨架两步，完成后可选给一份评审建议（非必需）。`
配置：replanning 阶段 B（enabled + health_probe + early_stop，redraw_on_stall=false）、`pipeline_guard_mode=warn`、阶段序 8 阶。

| 项 | 值 |
|---|---|
| 耗时 / 事件 | 1059s / 2120 个 SSE 事件，正常收尾（done） |
| 计划 | t1 需求条目化(必需) / t2 架构骨架(必需) / t3 可选评审(非必需) |
| t1 | **done**（301s，一次失败经 T1 自动补跑成功） |
| t2 | **failed**（两次尝试同点死亡 → §2 根因） |
| t3 | blocked（依赖 t2 失败，连带——正确行为） |
| plan_guard 卡片 | **非 null**：`mode=warn, ok=true, violations=[stage.required_missing(warn)]`——规划少排了 requirement_structuring，warn 如实上报不阻断，**这正是设计语义** |
| replan_decisions | 6 条（改造前同类决策为 0）：health T0/none ×3、**T1/retry**（t1 失败自动补跑）、T0/none（已补跑过不再补——无进展防护）、stall 观测留痕 |

### 门禁

新脚本 `tools/verify/run_full_round_live.py`（run / inspect 两段式）：
基线 7 判据 + 落库后验 A~F 12 判据。本轮 8/12 过，2 个失败项即 §2 的真 bug（t2 failed），已修。

---

## 2. ★ 挖出的 P1：confirm_required 悬空 tool_call

### 现象

t2（架构骨架生成 Agent）两次尝试都在**最后一个工具轮**后死亡：`todo_write + save_artifact` 同轮，`save_artifact` 命中**写前确认闸**（confirm_required），随后子任务零产出失败，错误文案只有兜底的「子任务未产生输出」。服务日志里同一 400 错误反复出现：

```
LLM API 400: An assistant message with 'tool_calls' must be followed by
tool messages responding to each 'tool_call_id'
```

### 根因链（三环叠加）

1. **confirm 无轮级 break**（主因，stream.py ~5988）：`clarify_required` 有轮级
   `if _clarify_req: break`（防悬空的既定手法），`confirm_required` 只有内层 for 的
   break —— 轮级回填 `if results: messages.append(pmsg)` 仍执行，pmsg 带
   save_artifact 的 tool_call 但没有配对 tool_result ⇒ **悬空**。
2. 悬空不可恢复：messages 已永久带毒 ⇒ 下一轮探测 400，且**后续每一次**调用都 400。
3. **error 事件无人认领**（归因丢失，stream.py _run_subtask 事件循环）：顶层 except 把
   异常转成 `{"type":"error"}` 事件，子任务事件循环不认识这个类型 ⇒ 静默丢弃 ⇒
   DB 里只剩「子任务未产生输出」，真因（400 配对缺失）完全不可见。

### 为什么以前没暴露

确认工具**独占一轮**时 results 为空 ⇒ `if results:` 整块跳过 ⇒ 无悬空 ⇒
侥幸走到确认卡收口。只有确认工具与其它工具**同轮并发**才触发——低概率组合，
完整一轮真机编排的价值正在于此。

### 修复（3 处 + 判据 1 处）

| # | 位置 | 修复 |
|---|---|---|
| 1 | stream.py 轮级 | 补 `if _hil_confirm_req: break`（与 clarify 同构，落在 results 回填之前） |
| 2 | stream.py `_write_decision` | source 由硬编码 `"t1_fail"` 改为 `"%s_fail" % tkey`（t2 的失败决策此前也被记成 t1_fail，审计失真） |
| 3 | stream.py `_run_subtask` | 事件循环补 `_et == "error"` 分支：记 `_sub_err` 并经返回值第 6 位带出 ⇒ DB error 列 / 失败分类器 / replan 决策拿到真因 |
| 判据 | verify_replan_p4.py | U12d 随动态 source 更新；S6 系列改读 **DEFAULT_CONFIG**（原判据读运行期值，真机验证开启配置后恒红——"配置被开启"不是回归）；U13a 字面量随双路径统一重构更新 |

### 新门禁

`tools/verify/verify_confirm_pairing.py` **12/0**：
① 轮级 break 存在且顺序正确（clarify break < confirm break < results 回填）；
② 动态 source 替换硬编码；
③ error 分支挂在 done 之后、`_sub_err` 全链路贯通。

### 行为影响

修复后，t2 这类轮次会**正常弹出写前确认卡**（HITL 本来的语义：写操作等用户批准），
而不是静默 400 死掉。确认前不写平台，确认后继续——这正是 2026-10-28 引入确认闸时
注释里声称的行为，现在才真正成立。

---

## 3. 回归与冒烟

| 项 | 结果 |
|---|---|
| 13 个门禁 | confirm_pairing 12/0 · plan_guard 61/0 · agent_catalog 37/0 · react_delegation 33/0 · replan_loop 46/0 · replan_nonstream 13/0 · **replan_p4 135/0** · impact_archive 14/0 · defer_cost 9/0 · plan_block_live 7/0 · alternatives_select 14/0 · prompt_layering 28/0 · py_compile + import main ✅ |
| pytest | **183 通过**（exit 0） |
| 服务 | 已带修复重启（logs/restart_fix_boot.log），live/ready 200 |
| 冒烟 | 显式 provider 71 ✅ done；默认解析路径 ✅ done（conv 990405/990406） |
| 瞬态记录 | 重启后 40s 内的首次冒烟（conv 990404）报一次 `LLMUnavailableError(provider=未配置)`，两条路径复测均不可复现，登记为启动期瞬态，如再现已知方向：查 `chat()` 入口 provider=None 的解析时序 |

---

## 4. 遗留

- conv 990403 留档（前端可查）：t1 产出完整需求条目化成果；t2/t3 是本轮挖出 bug 的现场。
- 多方案选型闭环（58-alternatives.js）本轮未覆盖（目标未触发多方案编排），
  此前 verify_alternatives_select 14/0 + 真机 HTTP 链路已验。

---

## 5. 修复后复跑一轮（conv 990407）：10/10 全绿 ★

> 同一目标、同一配置、带修复代码重跑。这是**首个全判据通过**的完整真机编排。

| 项 | 值 |
|---|---|
| 耗时 / 事件 | **366s / 647 事件**（上一轮 1059s / 2120——早停+无重试显著更省） |
| t1 需求条目化 | **done**（34s，零重试；经一次澄清卡与用户路径正常交互） |
| t2 架构骨架 | **done**（146s，交付 6491 字 SysML v2 架构骨架——上一轮同位置两次 400 死亡） |
| t3 可选评审 | **cancelled**——★ **早停真机触发**：「required 产出已全部达成（t1,t2）⇒ 提前收敛，主动放弃非必需任务」，`plan_revised` 事件 + 卡片 replan 留痕齐备 |
| plan_guard 卡片 | 非 null，**0 违规**（本轮规划阶段序正确：requirement_structuring → architecture_skeleton，与 8 阶清单一致） |
| replan_decisions | 2 条：health T0/none（计划健康）+ **early_stop T0**（提前收敛） |
| 日志 | `insufficient tool messages` 0 次；stream_error 仅重启期已登记的 990404 瞬态 |
| inspect 判据 | **PASS 10 / FAIL 0**（A 流程 / B plan_guard 卡片 / C 重规划留痕 / D 任务结局 / E plan_revised / F 无异常，全过） |

### 两条真机曲线的对照

| | 修复前（990403） | 修复后（990407） |
|---|---|---|
| 必需任务 | 1/2（t2 两次 400 死亡） | **2/2** |
| 失败任务 | 1（错误不可见） | **0** |
| 非必需任务 | blocked（连带失败） | **被早停主动放弃（省整段执行）** |
| 墙钟 | 1059s | **366s** |
| 错误归因 | 「子任务未产生输出」 | 无错误发生（且 error 事件已可归因） |

conv 990407 已留档供前端查看；`run_full_round_live.py` 可随时重复此验证。
