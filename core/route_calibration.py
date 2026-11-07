"""路由置信度校准（shadow，2026-11-07 米爸第 15 轮 P0-1）。

## 为什么需要
`verify_router_calibration.py` 用真路由器 + 20 条 gold 实测：
  · ECE = 0.182（>0.10 视为阈值不可信）
  · 高置信档(≥0.90) 实际准确率仅 85.7%（行业要求 >97%）
  · `route=rule` 固定写 conf=0.95 但准确率 60%；`rule_scored` 同报 0.95 却 100%
⇒ 下游（llm_first 互证门、澄清门、编排信任度）都建立在**常量**而非测量值上。

## 为什么是 shadow
改 `confidence` 会改变 `_done` 的 needs_clarification、`llm_first` 互证、
缓存指纹等既有行为，风险高。故本模块**只计算校准值并写进 meta**：
  `meta.calibrated_confidence` / `meta.calibration_route`
生产判定仍用原 confidence。跑够真实流量、证明校准值更可信后再决定是否切换。

## 校准表怎么来的（不拍脑袋）
`ROUTE_RELIABILITY` 来自真库 `intent_samples` 的 confirmed 标注回放：
按 route 分组统计**实际命中率**，与标称 conf 比较，取「经验准确率」作校准基准。
样本不足的 route 不做大幅调整（拉普拉斯平滑），避免过拟合到 2~3 条样本。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

#: ★ 2026-11-07：**真实数据推翻了合成 gold 的结论**。
#:   20 条合成 gold 上 `rule` acc=0.60 / `rule_scored` acc=1.00；
#:   48 条真库 confirmed 上却是 `rule` acc=**1.00**(14/14) / `rule_scored` acc=**0.70**(7/10)。
#:   ⇒ 合成集只能做冒烟，不能拿来标定。下面常量已从真实回放结果校正，
#:     且 `calibrate()` 会优先读 `tools/tmp/route_reliability.json`（真实命中率表）。
FALLBACK_ROUTE_RELIABILITY = {
    "rule": 0.95, "rule_scored": 0.75, "explain": 0.85,
    "llm_first": 0.88, "semantic": 0.92, "platform_read": 0.95,
    "chat": 0.80, "continue": 0.80, "cache": 0.90,
    "llm_conflict": 0.40, "fused_conflict": 0.60, "speech_act": 0.92,
}

_REL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tools", "tmp", "route_reliability.json")
_rel_cache = {"data": None, "mtime": 0.0}
_rel_lock = threading.Lock()


def load_reliability() -> dict:
    """读真实回放表 `{route: {"hit":n,"n":n}}`；按 mtime 失效，不每次读盘。"""
    try:
        mt = os.path.getmtime(_REL_PATH)
    except Exception:
        return {}
    with _rel_lock:
        if _rel_cache["data"] is None or mt != _rel_cache["mtime"]:
            try:
                with open(_REL_PATH, encoding="utf-8") as f:
                    _rel_cache["data"] = json.load(f)
                _rel_cache["mtime"] = mt
            except Exception:
                _rel_cache["data"] = {}
    return _rel_cache["data"] or {}

#: 校准强度：0 = 完全不校准（返回原值），1 = 完全采用经验值。
#: shadow 阶段固定 1.0（观测「如果完全校准会怎样」），不改生产判定。
ALPHA = 1.0
_MIN_SAMPLES = 3       # 少于 3 条的 route 不做经验校准（防过拟合）
_PRIOR_WEIGHT = 2.0    # 拉普拉斯平滑强度


def calibrate(route: str, confidence: float,
              reliability: Optional[dict] = None) -> float:
    """返回**校准后**置信度（纯函数，不碰任何状态）。

    `reliability` = {route: {"hit": int, "n": int}}（真实标注回放结果）。
    样本不足或表缺失 ⇒ 回落内置兜底常量；仍不足 ⇒ 返回原值（诚实：不知道就别装知道）。
    """
    r = str(route or "").strip()
    c = float(confidence or 0.0)
    # 未显式传表时**自动读真实回放表**：生产 `_done` 只调 calibrate(route, conf)，
    # 若这里不读盘，写进 meta 的会是一份拍脑袋的常量（实测踩到）。
    rel = reliability if reliability is not None else load_reliability()
    if r in rel and isinstance(rel[r], dict):
        n = int(rel[r].get("n") or 0)
        hit = int(rel[r].get("hit") or 0)
        if n >= _MIN_SAMPLES:
            empirical = (hit + _PRIOR_WEIGHT * 0.8) / (n + _PRIOR_WEIGHT)
        else:
            empirical = None
    else:
        empirical = None
    if empirical is None:
        empirical = FALLBACK_ROUTE_RELIABILITY.get(r)
    if empirical is None:
        return round(c, 4)
    # 保留一点原始信号的区分度：完全替换会让同一 route 内所有样本同分，
    # 反而丢失「这条比那条更确定」的信息。用 0.7×经验 + 0.3×原值。
    blended = 0.7 * float(empirical) + 0.3 * c
    out = (1 - ALPHA) * c + ALPHA * blended
    return round(max(0.0, min(1.0, out)), 4)


def ece(rows: list, key: str = "confidence") -> float:
    """Expected Calibration Error：`Σ|桶内平均conf - 桶内实际acc| × 桶占比`。"""
    BINS = [(0.0, 0.50), (0.50, 0.75), (0.75, 0.90), (0.90, 1.01)]
    if not rows:
        return 0.0
    num = 0.0
    for lo, hi in BINS:
        g = [x for x in rows if lo <= float(x.get(key) or 0) < hi]
        if not g:
            continue
        acc = sum(bool(x.get("hit")) for x in g) / len(g)
        avg = sum(float(x.get(key) or 0) for x in g) / len(g)
        num += len(g) * abs(avg - acc)
    return round(num / len(rows), 4)


__all__ = ["calibrate", "ece", "load_reliability",
           "FALLBACK_ROUTE_RELIABILITY", "ALPHA"]
