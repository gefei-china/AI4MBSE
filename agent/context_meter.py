# -*- coding: utf-8 -*-
"""可回放上下文计量（P1-4 剩余缺口）。

对标 DSH ``dsh-token-meter/README.zh.md:36``：从持久日志 fold 出 token 用量与
上下文压力，**确定且不进行任何模型调用**。自研工程已有
``agent/context_audit.py`` 的 ``record_blocks`` 与 ``context_audit_summary``
聚合视图（本轮复测 6071 条），缺的是「计量值与注入侧声明同源相等」的判据。

因此本模块只做两件事：
  1. 从 ``context_audit`` 回放 fold（纯查询，零模型调用，零写入）；
  2. 提供同源断言 ``assert_injection_equals`` —— 断言「两侧条数相等」，
     而不是「等于某个具体数」（换个环境就假红）。

明确不改动 ``skills.py`` 的按需披露链路：实测 ``tool_result_offloads`` 里
``skill_body_fetch#*`` 315 条，该链路已生效。
"""
from __future__ import annotations

import hashlib
import json

from core import event_vocabulary as vocab


def measure(conn, trace_id=None, conversation_id=None, phase=None) -> dict:
    """回放 fold：按 block_name 汇总 tokens / chars / 条数。"""
    sql = "SELECT block_name, SUM(tokens), SUM(chars), COUNT(*) FROM context_audit WHERE 1=1"
    args = []
    if trace_id is not None:
        sql += " AND trace_id=?"; args.append(trace_id)
    if conversation_id is not None:
        sql += " AND conversation_id=?"; args.append(conversation_id)
    if phase is not None:
        sql += " AND phase=?"; args.append(phase)
    sql += " GROUP BY block_name"
    by_block, total_tokens, total_chars, total_rows = {}, 0, 0, 0
    for name, tk, ch, n in conn.execute(sql, args):
        vocab.validate_event({"block_name": name})     # 词表外类型在此即被拒
        by_block[name] = {"tokens": int(tk or 0), "chars": int(ch or 0), "rows": int(n or 0)}
        total_tokens += int(tk or 0); total_chars += int(ch or 0); total_rows += int(n or 0)
    payload = json.dumps(by_block, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=str)
    return {"total_tokens": total_tokens, "total_chars": total_chars,
            "rows": total_rows, "by_block": by_block,
            "blocks": len(by_block),
            "context_pressure": round(total_tokens / 128000, 4) if total_tokens else 0.0,
            "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]}


def assert_injection_equals(conn, expected: dict, **filters) -> dict:
    """同源判据：注入侧声明的「每个块的条数」必须与回放结果相等。"""
    m = measure(conn, **filters)
    diffs = []
    for name, want in (expected or {}).items():
        got = (m["by_block"].get(name) or {}).get("rows", 0)
        if got != want:
            diffs.append({"block": name, "expected": want, "measured": got})
    return {"ok": not diffs, "diffs": diffs, "rows": m["rows"],
            "total_tokens": m["total_tokens"], "digest": m["digest"]}


__all__ = ["measure", "assert_injection_equals"]
