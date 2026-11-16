"""P1 按意图分档阈值（2026-11-07 米爸第 15 轮）。

## 为什么分档
行业标准（Plura/Gartner、Vertext Labs）：「阈值最好**按意图**设而不是全局」。
读类问题（知识问答/闲聊）判错代价低，可放宽让 LLM 直接答；
写/平台数据操作类判错代价高，应提高门槛，不确定就走既有澄清门与 HIL 写闸。

## 为什么默认关闭
`verify_threshold_tiers.py` 用真库 48 条 confirmed 样本做 A/B 才能定夺。
在实测出净正之前，**默认 False = 逐字节零回归**（与既有 A/B 开关纪律一致）。

## 数据来源
`core.capability_clusters.cluster_of_agent()` 是唯一事实源：
族本身就区分了「读/写/平台」。新增 Agent 只要归族就自动获得分档，不用手工维护白名单。
"""
from __future__ import annotations

from typing import Optional

#: 族 → (主采纳门槛, 弱采纳下限)。None = 沿用全局默认。
#: 读类放宽（判错代价低）；写/平台收紧（判错要触发真实工具调用）。
TIERS = {
    "knowledge": (0.68, 0.45),      # 概念问答/闲聊：判错只影响答法
    "report": (0.75, 0.50),         # 报告：与全局一致
    "requirement": (0.78, 0.55),    # 需求工程：涉及生成规格，稍严
    "verify": (0.80, 0.58),         # 校验评审：可能触发修复
    "design": (0.80, 0.58),         # 设计建模：可能产码/落库
    "change": (0.82, 0.60),         # 变更影响：写操作前置
    "platform": (0.85, 0.62),       # 平台数据读写：MCP 真实写，最严
}


def thresholds_for(intent: str, global_min: float, global_weak: float,
                   enabled: bool = False,
                   cluster_of_agent=None) -> tuple:
    """返回 (min_conf, weak_conf)。enabled=False 时原样返回全局值（零回归）。"""
    if not enabled:
        return (float(global_min), float(global_weak))
    try:
        from core.capability_clusters import cluster_of_agent as _coa
        coa = cluster_of_agent or _coa
        cid = coa(str(intent or ""))
    except Exception:
        cid = ""
    tier = TIERS.get(cid)
    if not tier:
        return (float(global_min), float(global_weak))
    return (float(tier[0]), float(tier[1]))


__all__ = ["TIERS", "thresholds_for"]
