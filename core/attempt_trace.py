# -*- coding: utf-8 -*-
"""attempt 结构化留痕：把「每次生成」在**产生时**打标，不再让下游从文本反推。

## 为什么要这个模块（2026-11-19，msg#7039 实证）

标杆平台每次 assistant turn 是独立 record，重跑天然分散；本系统一次用户输入
→ 一个 `while` 续跑循环 → **一条 message**（`messages` 表连 `turn`/`attempt` 列都没有）。
于是三次 attempt 的正文被拼进同一个 21,864 字的气泡，用户看到「提示词两遍、代码三遍」。

上一轮我们只能在**出口用文本判据**收敛（块级/段级/相似度），那是补偿性设计：
判据要追着语料跑（实测翻车三次：词表命中 11.8%、代码占比 67~76% 过宽、
三次重述相似度 0.39 抓不到）。本模块把信息前移到**产生时**：

  · `content`            = 收敛后的**最终版**（正文单一事实源，用户看到的就是这一份）
  · `card_data.exec.attempts` = 历次 attempt 的**原文**（过程区，供历史回放折叠展示）

## 语义（这是与标杆对齐的关键）

三种 attempt 必须**分开标记**，不能靠下游猜：

  `answer`       首次生成（一次交付的正常答复）
  `continuation` 截断续写 / 用户说「继续」——语义是 **append**
  `retry`        harness 判未完成 / 重规划 ——语义是 **replace**

本系统此前把两者混在 `merge_round_content(prev, cur)` 一个函数里靠文本特征猜，
猜错两个方向都有代价（重复 or 丢交付物）。现在上游直接给 kind。

## 向后兼容（硬约束）

  · **只有 1 次 attempt 时不写 `attempts` 字段** —— 绝大多数请求如此，
    零字段膨胀，刷新后还原与旧数据逐字节一致；
  · `read_attempts()` 对旧数据（无字段/字段非数组）返回 `[]`，不抛异常；
  · 不新增 messages 列 —— `card_data` 本就是过程区载体，零迁移风险。

## 单一事实源纪律

「有几次 attempt、各自是什么 kind」只在本模块定义一次；execute/stream 两处调用同一函数，
不允许各写一份（本仓反复踩过「同一语义两处判定必然错位」）。
"""
from __future__ import annotations

KIND_ANSWER = "answer"
KIND_CONTINUATION = "continuation"
KIND_RETRY = "retry"

_VALID_KINDS = (KIND_ANSWER, KIND_CONTINUATION, KIND_RETRY)

#: 历史回放过程区最多展示多少字节原文。超出截断并标注 ——
#: 防止一条 2 万字的 attempt 把过程卡撑爆（正文不受此限制，正文永不全截断）。
_MAX_ATTEMPT_CHARS = 20000


def kind_of(index: int, *, continuation_signal: bool = False,
            harness_retry: bool = False) -> str:
    """判定本次 attempt 的语义。优先级：首次 > 用户/截断续写 > harness 重跑。

    ⚠️ `continuation_signal` 与 `harness_retry` 同时为真时取 `continuation` ——
       用户的「继续」是显式意图，harness 的自动判定是系统意图，显式优先。
    """
    if int(index or 0) <= 0:
        return KIND_ANSWER
    if continuation_signal:
        return KIND_CONTINUATION
    if harness_retry:
        return KIND_RETRY
    # 有前序、但两个信号都没给 ⇒ 保守记 retry（"又写了一遍"）
    return KIND_RETRY


def record(attempts: list, content: str, *, index: int,
           kind=None, continuation_signal: bool = False,
           harness_retry: bool = False) -> list:
    """追加一次 attempt，返回**新列表**（不改入参，便于纯函数测试）。

    幂等：同 `index` 已存在时**更新而非追加**（重入/恢复分支不会重复留痕）。
    空内容不记（失败轮没有可展示产出）。

    ⚠️ `kind` 默认必须是 `None` 而不是 `KIND_ANSWER` —— 否则调用方只传
       `harness_retry=True` 时会被默认值盖掉，所有 attempt 都记成"首次"
       （本模块自测抓到的真 bug：kinds 全是 answer）。
    """
    out = [dict(a) for a in (attempts or []) if isinstance(a, dict)]
    text = str(content or "")
    if not text.strip():
        return out
    k = (kind if kind in _VALID_KINDS else
         kind_of(index, continuation_signal=continuation_signal,
                 harness_retry=harness_retry))
    item = {"index": int(index), "kind": k, "chars": len(text),
            "content": text[:_MAX_ATTEMPT_CHARS]}
    if len(text) > _MAX_ATTEMPT_CHARS:
        item["truncated"] = True
    for i, a in enumerate(out):
        if a.get("index") == item["index"]:
            out[i] = item
            return out
    out.append(item)
    out.sort(key=lambda a: int(a.get("index") or 0))
    return out


def attach(card_data, attempts: list):
    """把 attempts 注入 `card_data.exec.attempts`。

    · `card_data` 可以是 dict 或 JSON 字符串；返回**同类型**（调用点无需改解包方式）；
    · `attempts` 为空或仅 1 条 ⇒ 原样返回（向后兼容 + 避免字段膨胀）；
    · 单条也不写：一条 message 就是一次生成，没有"历史"可展示。
    """
    import json

    items = [a for a in (attempts or []) if isinstance(a, dict)]
    if len(items) < 2:
        return card_data
    is_str = isinstance(card_data, str)
    try:
        d = json.loads(card_data or "{}") if is_str else dict(card_data or {})
    except Exception:
        d = {}
    ex = d.get("exec") if isinstance(d.get("exec"), dict) else {}
    if not isinstance(ex, dict):
        ex = {}
    ex["attempts"] = items
    d["exec"] = ex
    return json.dumps(d, ensure_ascii=False) if is_str else d


def read_attempts(card_data) -> list:
    """从 card_data 读 attempts（dict 或 JSON 串皆可）。旧数据返回 []。"""
    import json

    try:
        d = json.loads(card_data or "{}") if isinstance(card_data, str) else (card_data or {})
        ex = d.get("exec") or {}
        a = ex.get("attempts") if isinstance(ex, dict) else None
        return [x for x in (a or []) if isinstance(x, dict)] if isinstance(a, list) else []
    except Exception:
        return []


def summary(attempts: list) -> str:
    """过程区摘要文案：给前端 step 的 `.proc-sub` 用。"""
    items = [a for a in (attempts or []) if isinstance(a, dict)]
    if not items:
        return ""
    kinds = []
    for a in items:
        k = str(a.get("kind") or KIND_ANSWER)
        kinds.append({"answer": "首次", "continuation": "续写", "retry": "重跑"}.get(k, k))
    return "%d 次：%s" % (len(items), " → ".join(kinds))
