# -*- coding: utf-8 -*-
"""回填失败轮丢失的「执行过程」（card_data.exec），2026-10-30。

## 为什么需要（这是数据修复，不是新功能）
`_persist_partial_stream` 原先把 `card_data` 写死 `"{}"`（已在同一轮修复），
于是**2026-10-30 之前**所有失败/中断轮的单条消息都只剩一句说明：
刷新后前端 `_procHtmlNow` 读不到 `exec` ⇒ 意图识别 / 工具调用 / 阶段卡片**全部消失**。
用户在**修复前**已经产生的那几条消息不会自己好 —— 数据已经写空了，只能回填。

## 证据从哪来（不猜、不编）
那一轮的过程**并没有真的丢**，只是没写进 `card_data`：
  · `tool_call_logs`：逐条工具调用（name / ok / arguments / result / latency_ms / result_truncated）
  · `orch_stream_events`：stage（意图识别等）与 reasoning 增量
所以回填是**还原**，不是"补一段看起来像的东西"。

## 窗口口径（唯一容易搞错的地方）
一条消息的"那一轮"= `(上一条消息 created_at, 本条 created_at]`。
用**相邻消息**切窗，而不是"往前 N 分钟"——后者会把上一轮的工具调用算进来，
造出"这轮调了 30 个工具"的假象（同一会话里连续失败时必踩）。

## 纪律
- **只填空卡**：`card_data` 为 `{}` / 空串 / NULL / 无 `exec` 才动；已有 exec 一律不覆盖。
- **默认预演**：不加 `--apply` 只打印将写入什么。
- **幂等**：跑两次第二次无事可做（判据就是"已有 exec 则跳过"）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/_backfill_exec_card.py            # 预演
    ./.venv/Scripts/python.exe -X utf8 tools/_backfill_exec_card.py --apply    # 写入
    ./.venv/Scripts/python.exe -X utf8 tools/_backfill_exec_card.py --conv 9779 --apply
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _local_offset_hours() -> int:
    """本机相对 UTC 的整小时偏移（本机 = +8）。用于对齐两套时间口径。"""
    try:
        from datetime import datetime, timezone
        _n = datetime.now()
        _u = datetime.now(timezone.utc).replace(tzinfo=None)
        return int(round((_n - _u).total_seconds() / 3600.0))
    except Exception:
        return 0


def _shift_ts(ts: str, hours: int) -> str:
    """把 'YYYY-MM-DD HH:MM:SS' 平移 hours 小时；解析失败原样返回（宁可查不到，不要抛）。"""
    if not hours:
        return ts
    try:
        from datetime import datetime, timedelta
        return (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def _has_exec(card_data) -> bool:
    try:
        d = json.loads(card_data or "{}")
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    ex = d.get("exec")
    return isinstance(ex, dict) and any(
        ex.get(k) for k in ("reasoning", "notices", "tools", "agent"))


def _build_exec(conn, conv_id, ts_lo, ts_hi, agent_name=""):
    """在时间窗内重建 exec。返回 (exec_dict 或 None, 统计)。"""
    tools = []
    # ⚠️ `tool_call_logs` **没有** `error` 列（只有 result / failure_kind）——
    #   第一版按前端 schema 的字段名写了 `error`，实测直接 OperationalError。
    #   失败原因只能从 `result` 原文取（这正是当时落库的全部内容）。
    for r in conn.execute(
            "SELECT tool_name, ok, arguments, result, failure_kind, latency_ms, result_truncated "
            "FROM tool_call_logs WHERE conversation_id=? AND created_at>? AND created_at<=? "
            "ORDER BY id", (conv_id, ts_lo, ts_hi)):
        _ok = bool(r["ok"])
        tools.append({
            "name": r["tool_name"],
            "ok": _ok,
            # ⚠️ 失败时前端优先读 `error`（见 06-cards.js 的三段式错误面）；
            #    这里把 error 留空会导致"✗ 失败"却没有原因 ⇒ 失败就带上原文首行。
            "error": "" if _ok else str(r["result"] or "")[:300],
            "result": str(r["result"] or "")[:4000],
            "arguments": r["arguments"] or "",
            "elapsed_ms": int(r["latency_ms"] or 0),
            "truncated": bool(r["result_truncated"]),
        })

    reason_parts, notices = [], []
    # ⚠️ **时区口径不一致**（本仓现状，必须显式处理，不能想当然）：
    #   `messages` / `tool_call_logs` 的 created_at 是 **UTC**；
    #   `orch_stream_events` / `orch_run_spans` 是 **本地时间**（本机 UTC+8）。
    #   第一版直接拿同一个窗口去查 events ⇒ 恒 0 行 ⇒ 回填出来"只有工具、没有阶段"，
    #   而且看起来像"那一轮没产生 stage 事件"（完全静默，最难查的那种）。
    #   做法：算本机偏移，两窗口各查一次，**取命中更多的那个** ——
    #   这样将来若某张表改成 UTC，脚本自动跟随，不用回来改常量。
    _off = _local_offset_hours()
    _cands = []
    for _shift in ({0, _off} if _off else {0}):
        _rows = []
        try:
            _rows = list(conn.execute(
                "SELECT event_type, payload_json FROM orch_stream_events "
                "WHERE conversation_id=? AND created_at>? AND created_at<=? ORDER BY id",
                (conv_id, _shift_ts(ts_lo, _shift), _shift_ts(ts_hi, _shift))))
        except Exception:
            _rows = []
        _cands.append((len(_rows), _rows))
    _cands.sort(key=lambda x: -x[0])
    for r in (_cands[0][1] if _cands else []):
        try:
            p = json.loads(r["payload_json"] or "{}")
        except Exception:
            continue
        if r["event_type"] == "stage" and p.get("name"):
            # 阶段帧 → notices（前端按 kind 收编到对应环节，与在线渲染同构）
            notices.append({"kind": "progress",
                            "text": "环节：%s" % p.get("name")})
        elif r["event_type"] == "reasoning" and p.get("delta"):
            if p.get("kind") == "progress":
                notices.append({"kind": "progress", "text": str(p.get("delta"))[:400]})
            else:
                reason_parts.append(str(p.get("delta")))

    ex = {
        "reasoning": ("".join(reason_parts)[:8000] or None),
        "notices": (notices[:60] or None),
        "tools": (tools or None),
        "agent": (agent_name or None),
        # 标记来源：以后排查能分清"当时真存的"与"事后回填的"
        "backfilled": "2026-10-30 from tool_call_logs+orch_stream_events",
    }
    if not any(ex.get(k) for k in ("reasoning", "notices", "tools", "agent")):
        return None, {"tools": 0, "notices": 0, "reason": 0}
    return ex, {"tools": len(tools), "notices": len(notices), "reason": len("".join(reason_parts))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入（默认只预演）")
    ap.add_argument("--conv", type=int, default=0, help="只处理某个会话")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()

    from database import db_conn
    n_fill = n_skip = 0
    with db_conn() as conn:
        sql = ("SELECT id, conversation_id, created_at, card_data FROM messages "
               "WHERE role='assistant' ")
        args = []
        if a.conv:
            sql += "AND conversation_id=? "
            args.append(a.conv)
        sql += "ORDER BY conversation_id, id"
        rows = list(conn.execute(sql, args))

        by_conv = {}
        for r in rows:
            by_conv.setdefault(r["conversation_id"], []).append(r)

        for conv_id, msgs in by_conv.items():
            for i, m in enumerate(msgs):
                if _has_exec(m["card_data"]):
                    continue
                ts_hi = m["created_at"]
                ts_lo = msgs[i - 1]["created_at"] if i else "1970-01-01 00:00:00"
                # 上一条恰好同一秒 ⇒ 窗口为空，往前退 1 秒避免漏掉（同秒落库的真实情况）
                if ts_lo == ts_hi:
                    ts_lo = "1970-01-01 00:00:00" if i == 0 else ts_lo
                agent = ""
                try:
                    agent = (json.loads(m["card_data"] or "{}").get("agent") or "")
                except Exception:
                    agent = ""
                ex, st = _build_exec(conn, conv_id, ts_lo, ts_hi, agent)
                if not ex:
                    n_skip += 1
                    continue
                print("conv=%-6s msg=%-6s %s  tools=%-3d notices=%-3d reasoning=%-5d"
                      % (conv_id, m["id"], ts_hi, st["tools"], st["notices"], st["reason"]))
                n_fill += 1
                if a.apply:
                    card = {"exec": ex}
                    if agent:
                        card["agent"] = agent
                    conn.execute("UPDATE messages SET card_data=? WHERE id=?",
                                 (json.dumps(card, ensure_ascii=False), m["id"]))
                if n_fill >= a.limit:
                    break
            if n_fill >= a.limit:
                break
        if a.apply:
            conn.commit()

    print("\n%s：可回填 %d 条，无过程可还原 %d 条"
          % ("已写入" if a.apply else "[预演] 未写库", n_fill, n_skip))
    if not a.apply:
        print("加 --apply 写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
