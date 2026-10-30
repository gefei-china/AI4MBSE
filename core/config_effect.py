# -*- coding: utf-8 -*-
"""配置变更效果验证（2026-10-29，P1-4 之后的"改完有没有用"）。

═══════════════════════════════════════════════════════════════════════
补的是哪一环
═══════════════════════════════════════════════════════════════════════
`core/config_promotion.py` 已经保证"改前先沙箱 A/B 证明不改坏"，但**改完之后**
没有任何回流：

    改前：沙箱影子配置跑门禁 → 差分判回归（有）
    改后：线上指标有没有真的变好？（**此前无**）

于是两个真实问题无法回答：
  ① 这次改动到底有没有用？（可能白改，或改了跟没改一样）
  ② 如果改坏了，怎么发现？（沙箱只覆盖门禁集，覆盖不到长尾）

本模块用 `eval_metrics`（近 7 天同口径）做改前/改后对比，给出
`effective / ineffective / no_data / inconclusive` 四态判定，并落
`config_history.effect_*` 列 —— **可回滚的变更才有资格叫"可控变更"**。

═══════════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════════
1. **只读 + 只写判定列**：不改配置、不回滚（回滚是人按判定结果发起的动作）。
2. **样本不足不下结论**：窗口内该指标少于 `min_samples` 条 ⇒ no_data，
   **绝不补零**（"没数据"与"变 0 分"必须可区分，否则看板假绿/假红）。
3. **方向敏感**：`eval_metrics.direction` 是 low/high —— 检索 miss_rate 越低越好、
   faithful_rate 越高越好，同一"数值下降"在两个方向上是相反的结论。
4. **宁判 inconclusive 不硬判**：改善与劣化同时存在时，不假装权衡得了
   （权重是业务判断，不该由代码拍）。
"""
import time
from typing import Optional

#: 判定结论
VERDICT_EFFECTIVE = "effective"          # 有指标改善、无劣化
VERDICT_INEFFECTIVE = "ineffective"      # 出现劣化
VERDICT_MIXED = "mixed"                  # 有改善也有劣化（需人工权衡）
VERDICT_NO_DATA = "no_data"              # 窗口内无同口径指标
VERDICT_INCONCLUSIVE = "inconclusive"    # 样本不足 / 无法判定

#: 改善/劣化的最小相对变化（低于此视为 unchanged，避免噪声当结论）
DEFAULT_MIN_DELTA_PCT = 5.0
#: 单指标最少样本数（前后窗口各需这么多条）
DEFAULT_MIN_SAMPLES = 3
#: 默认前后窗口天数
DEFAULT_WINDOW_DAYS = 7


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _window_metrics(conn, start: str, end: str) -> dict:
    """取时间窗内各指标的样本值：{metric_key: {"values": [...], "direction": str}}。

    `direction` 取该指标在库内的**众数**（同一 metric_key 理论上方向一致；
    不一致说明埋点口径有问题，此时按最先出现的那条算，并在 detail 里标出）。
    """
    try:
        rows = conn.execute(
            "SELECT metric_key, value, COALESCE(direction,'low') d FROM eval_metrics "
            "WHERE created_at >= ? AND created_at < ?", (start, end)).fetchall()
    except Exception:
        return {}
    out = {}
    for r in rows:
        k = str(r["metric_key"] or "")
        if not k or r["value"] is None:
            continue
        slot = out.setdefault(k, {"values": [], "direction": str(r["d"] or "low")})
        try:
            slot["values"].append(float(r["value"]))
        except (TypeError, ValueError):
            continue
    return out


def evaluate_effect(conn, history_id: int, *,
                    window_days: int = DEFAULT_WINDOW_DAYS,
                    min_samples: int = DEFAULT_MIN_SAMPLES,
                    min_delta_pct: float = DEFAULT_MIN_DELTA_PCT) -> dict:
    """判定某次配置变更的效果。只读（除了由调用方决定是否落库）。

    返回 {ok, history_id, verdict, checked_at, metrics:[...], detail, reason}。
    """
    row = conn.execute(
        "SELECT id, section, key, old_value, new_value, applied_at, applied "
        "FROM config_history WHERE id=?", (history_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "history_not_found", "history_id": history_id}
    applied_at = str(row["applied_at"] or "").strip()
    if not applied_at:
        return {"ok": False, "reason": "no_applied_at", "history_id": history_id}

    # 前后等长窗口，避免"时间越长指标越好看"的自证偏差
    d = max(int(window_days), 1)
    before_start = _shift(applied_at, -d)
    after_end = _shift(applied_at, d)
    before = _window_metrics(conn, before_start, applied_at)
    after = _window_metrics(conn, applied_at, after_end)

    metrics, improved, degraded = [], 0, 0
    for key in sorted(set(before) & set(after)):
        b, a = before[key], after[key]
        if len(b["values"]) < min_samples or len(a["values"]) < min_samples:
            metrics.append({"metric_key": key, "verdict": "insufficient_samples",
                            "before_n": len(b["values"]), "after_n": len(a["values"])})
            continue
        bm, am = _mean(b["values"]), _mean(a["values"])
        if bm in (None, 0):
            metrics.append({"metric_key": key, "verdict": "no_baseline",
                            "before_mean": bm, "after_mean": am})
            continue
        delta_pct = (am - bm) / abs(bm) * 100.0
        direction = b["direction"] or "low"
        # low（越低越好）：下降 = 改善；high（越高越好）：上升 = 改善
        better = (delta_pct <= -min_delta_pct) if direction == "low" \
            else (delta_pct >= min_delta_pct)
        worse = (delta_pct >= min_delta_pct) if direction == "low" \
            else (delta_pct <= -min_delta_pct)
        v = "improved" if better else ("degraded" if worse else "unchanged")
        improved += 1 if better else 0
        degraded += 1 if worse else 0
        metrics.append({"metric_key": key, "direction": direction,
                        "before_mean": round(bm, 4), "after_mean": round(am, 4),
                        "delta_pct": round(delta_pct, 2), "verdict": v,
                        "before_n": len(b["values"]), "after_n": len(a["values"])})

    if not metrics:
        verdict, reason = VERDICT_NO_DATA, "窗口内无同口径指标（或前后无交集）"
    elif improved and degraded:
        verdict, reason = VERDICT_MIXED, "同时存在改善与劣化，需人工权衡"
    elif degraded:
        verdict, reason = VERDICT_INEFFECTIVE, "出现指标劣化，建议回滚"
    elif improved:
        verdict, reason = VERDICT_EFFECTIVE, "指标改善且无劣化"
    else:
        verdict, reason = VERDICT_INCONCLUSIVE, "样本不足或无显著变化"

    return {"ok": True, "history_id": int(history_id),
            "section": row["section"], "key": row["key"],
            "old_value": row["old_value"], "new_value": row["new_value"],
            "applied_at": applied_at, "window_days": d,
            "before_start": before_start, "after_end": after_end,
            "verdict": verdict, "reason": reason, "metrics": metrics,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def _shift(ts: str, days: int) -> str:
    """时间戳平移 N 天（输入/输出均为 'YYYY-MM-DD HH:MM:SS'）。解析失败返回原值。"""
    try:
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t + days * 86400))
    except Exception:
        return ts


def evaluate_pending(conn, *, limit: int = 20, window_days: int = DEFAULT_WINDOW_DAYS,
                     min_samples: int = DEFAULT_MIN_SAMPLES, apply: bool = False) -> dict:
    """批量判定"已应用但尚未判定"的配置变更（默认 dry-run）。

    默认 dry-run 与全仓口径一致；`apply=True` 才写 `config_history.effect_*`。
    """
    try:
        rows = conn.execute(
            "SELECT id FROM config_history WHERE COALESCE(applied,0)=1 "
            "AND COALESCE(effect_verdict,'')='' ORDER BY id DESC LIMIT ?",
            (max(int(limit), 1),)).fetchall()
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:160]),
                "items": [], "counts": {}}
    items = [evaluate_effect(conn, int(r["id"]), window_days=window_days,
                             min_samples=min_samples) for r in rows]
    counts = {}
    for it in items:
        v = it.get("verdict") or "error"
        counts[v] = counts.get(v, 0) + 1
    written = 0
    if apply:
        for it in items:
            if not it.get("ok"):
                continue
            try:
                conn.execute(
                    "UPDATE config_history SET effect_verdict=?, effect_detail=?, "
                    "effect_checked_at=? WHERE id=?",
                    (it["verdict"], it["reason"], it["checked_at"], it["history_id"]))
                written += 1
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:
            pass
    return {"ok": True, "dry_run": not apply, "count": len(items),
            "written": written, "counts": counts, "items": items}
