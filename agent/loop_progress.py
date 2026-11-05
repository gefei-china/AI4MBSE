# -*- coding: utf-8 -*-
"""无进展循环检测 —— 判据 = **边际新颖度**（2026-11-02，全链路实测发现并标定）。

## 现场证据（conv 10004 实测，A 组单任务 t1）

`graph_retrieve` 在一个子任务内被调用 **86 次**，`chat_probe` 的 prompt_tokens
从 12,664 单调涨到 **40,240**，`finish_reason` 恒为 `tool_calls`（从未收敛到作答）。

### 第一版判据为什么是错的（真库当场证伪）

我先按"结果雷同"写（相邻 shingle 相似度 ≥0.90 ⇒ 重复）。真库标定结果：

| 指标 | 实测 |
|---|---|
| 相邻结果相似度 中位数 | **0.213** |
| 相邻相似度 ≥0.90 的比例 | **9%**（8/85） |

⇒ 模型**并非**重复抄同一份结果，而是每次都在检索**不同内容**。
按相似度判据只能抓住 9%，**救不了这个循环**。
（错因：我只看 `tool_call_logs.result` 的**前 150 字符**——那部分每轮都是
 「知识库检索到 18 条相关片段（最高向量相似度 0.70x…）」，看起来一模一样。
 这正是本仓老教训：**别用截断样本下结论**。）

### 真数据给出的正确判据：边际新颖度

每次结果相对「此前所有结果的并集」的**新增 shingle 比例**：

| 调用序号 | 新增比例 | 解读 |
|---|---|---|
| 1~4 | 1.00 / 0.77 / 0.59 / 0.48 | 真在探索 |
| 6~9 | 0.17 / 0.10 / 0.25 / 0.26 | 开始收敛 |
| **17~64** | **0.000~0.09，其中 45 次 ≤0.01** | **零新增，纯烧钱** |
| 65~74 | 回升 0.46 / 0.23 / 0.20 / 0.34 | 换了方向，**真拿到新东西** |
| 75~86 | 又衰减到 ≤0.04 | 再次空转 |

⇒ 判据必须能同时：① 不在 1~5 次停（别砍有效探索）；② 不在 65~74 停（换方向是有效的）；
③ 在 20~64 这段停。

「连续 N 次边际新颖度 < 阈值」正好满足三条（阈值 0.05、连续 4 次 ⇒ 第 23 次停，
86 → 23，省掉 73% 的无效调用，且全程没砍掉有效探索与换方向）。

## 行业依据（本轮调研，五点一致）

| 来源 | 结论 |
|---|---|
| Agnost | 指纹要含 **result_hash**；只看 args 会误杀合法轮询 |
| BestHub「Beyond max_steps」 | 三类信号：动作指纹 + 结果指纹 + **状态差**（evidence/gap 是否推进） |
| SonarSource | 「靠行为而非计数」；连续步骤高度相似才算重复 |
| Particula / LangGraph 实践 | 哈希 (tool, canonical args, result) 元组，2~3 次重复即停 |
| OWASP LLM10 | 无界消耗是风险项：需 call budget + 进度检查 |

本仓既有 `loop_guard.dedup_hit` 只做**参数侧**精确匹配（模型同义改写即失效，
实测 86 次调用参数几乎全不同）⇒ 本模块补**结果侧**的进度判据，不替换它。

## 边界（明确不做的事）

- **不**按"工具被调两次"就停：轮询/翻页/换对象都合法，只有**零新增**才算无进展；
- **不**改工具结果原文（审计口径），只在模型侧追加提示；
- 空结果不参与"重复"判定（空 = 查不到，另有 weak 分支处理），但计入兜底上限；
- 整段可开关（`agent.progress_guard_enabled`），关掉即回到改动前行为。
"""
from __future__ import annotations

import re

__all__ = ["shingles", "similarity", "result_text_of", "ProgressTracker",
           "NOVELTY_MIN", "STREAK_STOP", "HARD_CAP_PER_TOOL"]

#: shingle 宽度（4 字符）：足够区分不同条文，又对"数字/标点微变"鲁棒
_W = 4

#: ── 主判据：边际新颖度阈值 ──────────────────────────────────────────────
#: 取值来自真库标定（conv 10004 的 86 次 graph_retrieve）：
#:   · 有效探索阶段落在 0.48~1.00；
#:   · 收敛阶段 0.10~0.26；
#:   · 空转阶段 **0.000~0.01**（占 45/86）。
#: 取 0.05 落在"空转"与"收敛"之间的空档，两侧都不贴边 ⇒ 对噪声不敏感。
NOVELTY_MIN = 0.05

#: 连续多少次低新颖度才停。取 4（行业给 2~3）：
#: 真库模拟 ⇒ 阈值 0.05+连续 4 次在第 23 次触发（86→23）；
#: 取 2 会在第 20 次就停（那时 17/18/19/20 是 0.007/0.089/0.051/0.008，
#: 中间的 0.089 说明还在拿新东西）——**宁可多给一次机会，不要砍掉真探索**。
STREAK_STOP = 4

#: 绝对兜底：单工具在一个子任务内的调用上限。
#: 只在"边际新颖度始终达标却仍狂调"的病态情况下生效（正常任务不会到这个数）。
#: 取 30：真库证据里"换方向"的有效探索出现在第 65 次 ⇒ 硬上限不能设太低，
#: 否则会把「换了问法真的拿到新内容」一并砍掉。
HARD_CAP_PER_TOOL = 30

#: 完全相同的判定阈值（用于抓"抄同一份结果"的真重复，属补充判据）
DUP_SIM = 0.95

_NOISE_TS = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
_NOISE_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """归一化：抹掉时间戳与空白差异（这两类噪声会让"同一结果"看起来不同）。"""
    t = str(text or "")
    t = _NOISE_TS.sub(" ", t)
    t = _NOISE_WS.sub("", t)      # 全去空白：换行/缩进差异不该算新信息
    return t


def shingles(text: str, k: int = _W) -> frozenset:
    """结果文本 → 字符 shingle 集合（结果指纹的载体）。"""
    t = _norm(text)
    if not t:
        return frozenset()
    if len(t) <= k:
        return frozenset([t])
    return frozenset(t[i:i + k] for i in range(len(t) - k + 1))


def similarity(a: frozenset, b: frozenset) -> float:
    """Jaccard 相似度。任一为空 ⇒ 0（空结果不参与"重复"判定）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


def result_text_of(tool_result) -> str:
    """从工具返回体里取出用于比较的文本（不含 ok/note 这类元字段）。

    ⚠️ 只比较 `result` 正文：`note`/`truncated` 是框架加的说明，
       它们每轮都可能不同（如 offload 编号），带上会污染指纹。
    """
    if isinstance(tool_result, dict):
        v = tool_result.get("result")
        if v is None:
            v = tool_result.get("content")
    else:
        v = tool_result
    return str(v or "")


class ProgressTracker:
    """子任务级的「检索还有没有新信息」跟踪器（每个子任务一个实例）。

    用法::

        tr = ProgressTracker()
        ...
        v = tr.observe(tool_name, result)
        if v["stop"]:
            # 用 v["hint"] 回喂模型，并结束本轮工具循环（转收尾作答）
        elif not v["novel"]:
            # 把 v["hint"] 追加进回填给模型的结果里，继续
    """

    def __init__(self, novelty_min: float = NOVELTY_MIN, streak_stop: int = STREAK_STOP,
                 hard_cap: int = HARD_CAP_PER_TOOL, dup_sim: float = DUP_SIM,
                 window: int = 8):
        self.novelty_min = float(novelty_min)
        self.streak_stop = max(2, int(streak_stop))
        self.hard_cap = max(2, int(hard_cap))
        self.dup_sim = float(dup_sim)
        self.window = max(1, int(window))
        self.calls = {}            # tool -> 次数
        self.seen = {}             # tool -> 累积 shingle 并集（"已看过的内容"）
        self.recent = {}           # tool -> [frozenset]（用于"完全相同"补充判据）
        self.streak = {}           # tool -> 连续低新颖度次数
        self.novelty_log = {}      # tool -> [ratio]（排障/门禁用）
        self.stops = 0             # 触发停止次数
        self.stop_reason = ""

    def observe(self, tool: str, result) -> dict:
        """登记一次工具结果，返回判定。

        返回 {"novel","ratio","sim","streak","stop","stop_reason","hint","calls"}
        · novel=False ⇒ 本次结果几乎没有新增内容（ratio < novelty_min）；
        · stop=True  ⇒ 应结束本轮工具循环（连续无新增 / 单工具超硬上限）。
        """
        tool = str(tool or "-")
        txt = result_text_of(result)
        sig = shingles(txt)
        n = self.calls.get(tool, 0) + 1
        self.calls[tool] = n

        # 空结果：不参与新颖度判定（空 = 查不到），但计入硬上限兜底
        if not sig:
            out = {"novel": True, "ratio": 1.0, "sim": 0.0,
                   "streak": self.streak.get(tool, 0), "stop": False,
                   "stop_reason": "", "hint": "", "calls": n}
            if n > self.hard_cap:
                out.update(stop=True, stop_reason="tool_call_cap",
                           hint=self._cap_hint(tool, n))
                self.stops += 1
                self.stop_reason = "tool_call_cap"
            return out

        pool = self.seen.setdefault(tool, set())
        new = sig - pool
        ratio = len(new) / float(len(sig))
        # 完全相同判定（补充判据：抄同一份结果）
        best = 0.0
        for p in self.recent.get(tool, []):
            s = similarity(sig, p)
            if s > best:
                best = s
        exact = best >= self.dup_sim

        pool |= sig
        rec = self.recent.setdefault(tool, [])
        rec.append(sig)
        if len(rec) > self.window:
            del rec[0:len(rec) - self.window]
        self.novelty_log.setdefault(tool, []).append(round(ratio, 4))

        low = (ratio < self.novelty_min) or exact
        if low:
            self.streak[tool] = self.streak.get(tool, 0) + 1
        else:
            self.streak[tool] = 0          # 有新内容 ⇒ 计数清零（换方向是有效的）

        stop, reason, hint = False, "", ""
        if low:
            hint = ("\n\n【本轮检索无新增】这次的 %s 结果与本次子任务内**已看过的内容**"
                    "重复度很高（新增 %.0f%%，低于 %.0f%% 视为无新信息）。"
                    "请**不要**再换问法重复检索：要么直接基于上面已有的材料作答，"
                    "要么把查不到的部分如实写进 missing[]。"
                    % (tool, ratio * 100, self.novelty_min * 100))
            if self.streak[tool] >= self.streak_stop:
                stop = True
                reason = "no_progress"
                hint = ("\n\n【系统已中止重复检索】%s 已连续 %d 次**没有带来新信息**"
                        "（新增量均低于 %.0f%%）。系统判定继续检索不会改善答案。"
                        "请**立即**基于已有材料给出结论；未覆盖的部分如实写进 missing[]，"
                        "**不要**再发起同类检索。"
                        % (tool, self.streak[tool], self.novelty_min * 100))
        if not stop and n > self.hard_cap:
            stop, reason, hint = True, "tool_call_cap", self._cap_hint(tool, n)
        if stop:
            self.stops += 1
            self.stop_reason = reason
        return {"novel": not low, "ratio": round(ratio, 4), "sim": round(best, 4),
                "streak": self.streak.get(tool, 0), "stop": stop,
                "stop_reason": reason, "hint": hint, "calls": n}

    def _cap_hint(self, tool: str, n: int) -> str:
        return ("【系统】工具 %s 在本子任务内已调用 %d 次（上限 %d），**停止调用它**，"
                "基于已有信息给出结论并如实说明缺口。" % (tool, n, self.hard_cap))

    def summary(self) -> dict:
        """排障用摘要：各工具的调用次数、最低新颖度、最终停止原因。"""
        mins = {}
        for t, arr in self.novelty_log.items():
            mins[t] = min(arr) if arr else None
        return {"calls": dict(self.calls), "min_novelty": mins,
                "stops": self.stops, "stop_reason": self.stop_reason}
