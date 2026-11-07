"""意图改选 → 样本池回流（2026-11-07 米爸第 15 轮 P0-2）。

## 契约
用户在澄清卡点「改选：X」时，`pickIntent()` 会把 `intent_override` 随原消息重发。
后端在**两处**聊天入口（非流式 / 流式）调用本函数，把这条真实样本写进
`intent_samples(status='suggested', source='override')`，`note` 里记
「系统原判 / 用户改选」。

## 纪律（一条都不能破）
1. **绝不自动 confirmed**：`IntentSampleRepo.upsert` 已保证 confirmed 行不被自动采集覆盖；
   本函数显式传 `status='suggested'`，且只在「系统原判 ≠ 用户改选」时写。
2. **不拖慢请求**：整段 try 包裹，任何异常静默返回 False（与既有 `collect()` 同口径）。
3. **不采系统包装文本 / 超长文本**：复用 `_is_system_wrapper` 与 4~200 字符过滤。
4. **原判从哪来**：调用方传入 `_detected`（本轮识别结果）与 `intent_override`（用户改选）。
   若两者相同，说明用户只是确认，不是改判 ⇒ 不写（避免把「同意」当成纠错信号）。
5. **note 可机读**：JSON 字符串，含 override / detected / route / route_conf，
   管理页可按 `source='override'` 筛选后批量审核转 confirmed。

## 为什么落在 routers 而不是 stream
`intent_override` 只在 HTTP 请求体里可见；stream 内部拿到的是已被 team 逻辑
覆写后的 `forced_intent`（见 stream.py:4323 的 R1 注释），无法区分「用户改选」
与「编排内部定向」。故接线点必须在 **routers/conversations.py 的两个入口**。
"""
from __future__ import annotations

import json
from typing import Optional


def record_override(text: str, override: str, detected: str = "",
                    route: str = "", conf: float = 0.0) -> dict:
    """记录一次真实的用户改判 → 返回 {"ok", "reason", "id"}。绝不抛异常。"""
    try:
        t = (text or "").strip()
        ov = (override or "").strip()
        det = (detected or "").strip()
        if not ov or not t or not (4 <= len(t) <= 200):
            return {"ok": False, "reason": "empty_or_length"}
        from repositories.intent_sample_repo import _is_system_wrapper
        if _is_system_wrapper(t):
            return {"ok": False, "reason": "system_wrapper"}
        # 用户只是确认原判 ⇒ 不是纠错信号，不写
        if det and det == ov:
            return {"ok": False, "reason": "same_as_detected"}
        from core import config as cfg
        if not bool(cfg.get("intent", "sample_collect", True)):
            return {"ok": False, "reason": "collect_disabled"}
        note = json.dumps({"override": ov, "detected": det,
                           "route": route or "", "route_conf": float(conf or 0)},
                          ensure_ascii=False)
        from database import get_db
        from repositories.intent_sample_repo import IntentSampleRepo
        conn = get_db()
        try:
            # status 固定 suggested：人工审核才转 confirmed（见模块头纪律 1）
            repo = IntentSampleRepo(conn)
            r = repo.upsert(
                t, intent="", status="suggested", source="override",
                hit_intent=det or "", hit_route=route or "",
                hit_conf=float(conf or 0), note=note, created_by="intent_override")
            # ⚠️ `upsert` 的 UPDATE 分支**不更新 source**。真实 HTTP 路径里
            #    常规采集 `collect()` 与回流都写同一 text；若采集先落行，
            #    该行会永远是 source='prod' ⇒ 管理页按 source='override' 筛不到。
            #    ⇒ 这里显式把非 confirmed 的行改回 override（confirmed 行是人工标签，绝不碰）。
            conn.execute(
                "UPDATE intent_samples SET source='override' "
                "WHERE text=? AND status!='confirmed'", (t,))
            conn.commit()
            return {"ok": True, "id": r.get("id"), "updated": r.get("updated"),
                    "override": ov, "detected": det}
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:90])}


def override_candidates(conn, limit: int = 50) -> list:
    """管理页用：列出待人工审核的改选样本（含解析后的原判/改判）。"""
    rows = conn.execute(
        "SELECT id,text,status,source,note,seen_count,updated_at "
        "FROM intent_samples WHERE source='override' ORDER BY seen_count DESC, id DESC "
        "LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else dict(r)
        try:
            d["parsed"] = json.loads(d.get("note") or "{}")
        except Exception:
            d["parsed"] = {}
        out.append(d)
    return out


__all__ = ["record_override", "override_candidates"]
