# -*- coding: utf-8 -*-
"""多方案打分工具（平台工具 `alternatives_score` 的执行模块）。

对齐 `sysml_ast_tools` / `methodology_profile_tools` 的既有范式：
`tools.py` 的 handler 只做「按名分发 + 异常兜底」，真正的逻辑在本模块。

## 为什么是「工具」而不是「管线分支」

平台化原则（`register_sysml_check_tools.py` 文件头）：

> 策略写在 pipeline 里意味着每次调整都要改代码 + 重启；写在 DB 里则**改文本即生效**。

本模块承担**算子**（归一化/加权/推荐判定，纯函数），而**策略**在 DB：
- 维度清单、权重、阈值、别名表 ⇒ `generate_rules` 表（`/api/studio/rules` 可改）；
- 何时产出多方案、输出契约、纪律 ⇒ `skills` 表（技能正文，改文本即生效）；
- 哪些 Agent 具备该能力 ⇒ `agent_tools` 绑定（新建 Agent 可自动获得）。

⇒ 换维度集 / 改权重 / 改编排规则**都不必改代码**。

## 工具契约

**输入**（`input_schema` 见 `register_alternatives_tools.py`）：
- `alternatives`：**必填**，候选方案数组。每项：
  - `name`：方案名（必填）
  - `summary`：一句话概述（可选）
  - `scores`：各维度**原始量**字典（键用维度 key）
  - `rationale`：各维度取值依据（可选，但强烈建议 —— 供人核对）
- `weights` / `tie_epsilon` / `min_coverage`：可选覆盖（不传 ⇒ 走 DB 配置 ⇒ 再兜底默认值）

**输出**：`{"ok": true, "result": "<JSON 字符串>"}`
JSON 内含：`alternatives`（对比矩阵，前端渲染）、`scoring`（打分明细）、
`dimensions`（维度目录）、`config`（本次实际生效的配置，便于审计）。

⚠️ **模型不得自报综合分**：本工具是分数的**唯一来源**。模型只提供原始量。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from core.alternatives import (
    build_comparison_table,
    load_config_from_db,
    score_alternatives,
)

#: 工具注册名（与 tools 表 / agent_tools 绑定名一致）
TOOL_NAME = "alternatives_score"


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "result": msg}


def exec_tool(name: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """执行多方案打分。

    返回 `{"ok": bool, "result": str}`（result 为 JSON 字符串，便于模型解析与前端渲染）。
    """
    if name != TOOL_NAME:
        return _err("未知工具：%s" % name)
    args = arguments or {}

    # ── 1) 取配置（DB 优先；缺失则用兜底默认值） ──
    try:
        cfg = load_config_from_db()
    except Exception:
        cfg = {}

    # ── 2) 取候选方案（必填） ──
    alts = args.get("alternatives")
    if isinstance(alts, str):
        # 容忍模型把数组当 JSON 字符串传
        try:
            alts = json.loads(alts)
        except Exception:
            alts = None
    if not isinstance(alts, list) or not alts:
        return _err(
            "参数 alternatives 必填：候选方案数组（至少 1 个）。"
            "每项形如 {\"name\":\"方案A\",\"scores\":{\"<维度key>\": 原始量},"
            "\"rationale\":{\"<维度key>\":\"取值依据\"}}。"
            "维度 key 见 dimensions 字段；无法量化的维度**省略该键**（不要编造，也不要填 0）。")
    alts = [a for a in alts if isinstance(a, dict)]
    if not alts:
        return _err("参数 alternatives 中没有有效的方案对象（每项须为含 name 的对象）。")

    # ── 3) 覆盖参数（模型显式传参优先于 DB 配置） ──
    weights = args.get("weights")
    if not isinstance(weights, dict) or not weights:
        weights = cfg.get("weights")
    tie_epsilon = args.get("tie_epsilon")
    if tie_epsilon is None:
        tie_epsilon = cfg.get("tie_epsilon")
    min_coverage = args.get("min_coverage")
    if min_coverage is None:
        min_coverage = cfg.get("min_coverage")

    try:
        tie_epsilon = float(tie_epsilon)
    except (TypeError, ValueError):
        tie_epsilon = 0.02
    try:
        min_coverage = float(min_coverage)
    except (TypeError, ValueError):
        min_coverage = 0.6

    # ── 4) 打分 ──
    try:
        scored = score_alternatives(
            alts,
            weights=weights,
            tie_epsilon=tie_epsilon,
            min_coverage=min_coverage,
            dimensions=cfg.get("dimensions"),
            aliases=cfg.get("aliases"),
        )
    except Exception as exc:  # noqa: BLE001
        return _err("打分失败：%s: %s。⚠️ 未完成打分 —— 请如实说明『打分未完成』，"
                    "不要自行编造综合分。" % (type(exc).__name__, str(exc)[:200]))

    if not scored.get("items"):
        return _err("没有可打分的方案：每个方案至少要有一个**可量化的维度原始量**"
                    "（等级词如「高/中/低」不算量化值）。⚠️ 请补齐原始量后再调用。")

    # ⚠️ 2026-11-03 实测发现的真实缺陷：底层算子对「无可量化维度」的方案给
    #    total=None / coverage=0 / 不参与推荐（**算法是对的**），但只要 items 非空，
    #    本工具就会回 ok=True ⇒ 对模型宣称「打分完成」，而实际**一个分数都没算出来**。
    #    ⇒ 必须显式判据：**至少一个方案算出了总分**（total 非 None）。
    _scored_items = [i for i in scored.get("items") or [] if i.get("total") is not None]
    if not _scored_items:
        return _err(
            "**没有任何方案算出分数**：所有方案的维度值都不是可量化数值"
            "（等级词如「高/中/低」、空串、非法值都不可量化）。"
            "请把每个维度填成**原始量**（如时延 120ms、成本 80 万元、覆盖 18 条）后重试。"
            "⚠️ 打分**未完成** —— 请如实说明『打分未完成』，不要自行编造综合分。")

    payload = {
        "ok": True,
        "alternatives": build_comparison_table(scored),
        "scoring": {
            "version": scored.get("version"),
            "weights": scored.get("weights"),
            "items": scored.get("items"),
            "notes": scored.get("notes") or [],
        },
        "dimensions": scored.get("dimensions") or [],
        "config": {
            "tie_epsilon": tie_epsilon,
            "min_coverage": min_coverage,
            "weights_source": "调用参数" if isinstance(args.get("weights"), dict) and args.get("weights")
                              else "平台配置",
            "dimensions_source": "平台配置" if cfg.get("dimensions") else "默认维度集",
        },
    }
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return _err("结果序列化失败：%s: %s" % (type(exc).__name__, str(exc)[:200]))
    return {"ok": True, "result": text}


#: 工具别名（防止命名漂移导致「注册了却路由不到」—— 本仓 2026-10-08 实测踩过）
ALIASES = {TOOL_NAME}
