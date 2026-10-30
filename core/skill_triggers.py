# -*- coding: utf-8 -*-
"""技能 triggers 格式校验（2026-10-29，P1-4）。

## 为什么需要

`skills.triggers` 是 JSON 数组，匹配逻辑是**逐元素子串命中**
（`agent/pipeline_parts/skills.py:14-41`，元素含 `+` 时为合取）。
于是「把一整串逗号分隔的清单当一个元素」是**静默失效**：

    实测（真库 2026-10-29）：技能 id=47「变更影响分析」
    triggers = ['变更、调整、更新、影响、后果、']   ← 一个元素，5 个顿号
    ⇒ 需要一个用户输入里**连续出现**这一整串才命中 ⇒ 该技能实际上永远不触发

这类脏数据不会报错、不会告警，只表现为"这个技能好像没起作用"。
所以校验必须放在**写入侧**，而不是靠事后巡检。

## 设计取舍

- **只警告不阻断**（`normalize_triggers` 默认 `strict=False`）：历史数据与
  第三方导入可能已有此形态，硬拦会让既有流程挂掉。
- 提供 `strict=True` 供新写入路径使用（API / 种子注册），从源头堵住。
- **不自动猜用户意图**：拆分只按分隔符做，不改写词义。把"影响"这种泛词
  留下来由人判断——替产品做词义决策是越权。
"""
import json
import re

#: 常见中英文分隔符（用户在输入框里习惯用任意一种）
_SEPS = ("、", "，", ",", "；", ";", "/", "|")


def split_trigger(t: str) -> list:
    """把一个可能是"清单"的 trigger 拆成原子词。已是原子词则原样返回单元素列表。

    刻意**不动 `+`**：`A+B` 是平台的合取语法（`skills.py:17-22`），
    拆开它会改变匹配语义（从"同时出现"变成"任一出现"）。
    """
    s = str(t or "").strip()
    if not s:
        return []
    if "+" in s and not any(ch in s for ch in _SEPS):
        return [s]                      # 合取式：整体保留
    if not any(ch in s for ch in _SEPS):
        return [s]
    parts = re.split("[" + re.escape("".join(_SEPS)) + "]", s)
    return [p.strip() for p in parts if p.strip()]


def analyze_triggers(triggers) -> list:
    """返回所有"疑似把清单塞进单个元素"的项 `[(原值, 拆分结果)]`。

    判据：单个元素里出现 **≥2 个**分隔符。为什么是 2 而不是 1：
    「A、B」两词也可能是有意为之的短语（少见但存在），
    而 3 个以上词串成一串几乎只能是"清单被当成了一个元素"。宁可漏报不误报。
    """
    out = []
    try:
        items = json.loads(triggers) if isinstance(triggers, str) else (triggers or [])
    except Exception:
        return out
    if not isinstance(items, list):
        return out
    for t in items:
        s = str(t or "")
        if sum(s.count(ch) for ch in _SEPS) >= 2:
            out.append((s, split_trigger(s)))
    return out


def normalize_triggers(triggers, *, strict: bool = False) -> tuple:
    """规范化 triggers → `(新列表, 警告消息列表, 是否发生了变化)`。

    `strict=True` 时若有脏项则抛 `ValueError`（供写入侧从源头堵住）。
    """
    issues = analyze_triggers(triggers)
    try:
        items = json.loads(triggers) if isinstance(triggers, str) else list(triggers or [])
    except Exception:
        items = []
    if not issues:
        return items, [], False
    warns = ["triggers 元素 %r 内含 %d 个分隔符（像是把清单塞进了一个元素）—— "
             "匹配是**逐元素子串**，这样写会导致该技能几乎永不命中"
             % (orig, sum(orig.count(ch) for ch in _SEPS))
             for orig, _ in issues]
    if strict:
        raise ValueError("；".join(warns) + "。请拆成独立元素后重试。")
    # 非严格：拆开并去重（保持首次出现顺序）
    flat = []
    for t in items:
        for p in split_trigger(t):
            if p and p not in flat:
                flat.append(p)
    return flat, warns, True
