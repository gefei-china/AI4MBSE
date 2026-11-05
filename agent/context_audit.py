# -*- coding: utf-8 -*-
"""上下文分块归因（P0-2 · 2026-10-26）—— 让「每轮 prompt 的 token 花在哪」可观测。

## 为什么必须有这个模块

`llm_usage_stats` 只记**整包** `prompt_tokens`（生产实测 `chat_probe` 均值 12,511 /
峰值 64,552 token），**无分块归因** ⇒ 无法回答"这轮 12.5k 到底花在哪"。

后果是历史性的、且已造成两次真实损失：
  ① 2026-10-11：总闸在生产**静默空转**（pressure=1.37 却"裁掉 0 tok"）——因为看不见；
  ② 同日：流式路径**腰斩 82% 的 Agent prompt**（把最关键的 `## 约束`/`## 输出示例` 整段切掉）
     ——同样因为看不见。

旧方案（`docs/上下文组装机制-标杆调研与优化方案-20261011.md` §4）已把"上下文可观测"列为
**P1-2**，但至今未实施。本模块是该 P1-2 的落地，**是后续所有上下文调优的前提**。

## 设计要点（零额外计算成本）

归因点在 `agent/pipeline_parts/common.py::assemble_system_prompt` —— 该函数**本来就按块遍历**
`PROMPT_BLOCK_ORDER`，块边界是已知量，**不需要重新切分、不需要二次分词**。
因此记录每块 token 的边际成本 ≈ 一次 `count_tokens` 调用（可配置开关关闭）。

## 与已有 trace 体系的衔接

`core/audit.py` 已有 `trace_context()` / `begin_trace()`，与 `llm_usage_stats.trace_id`
同源（`begin_trace` 用 `request_context().request_id` 或随机 hex）。本模块的
`context_audit.trace_id` 与 `llm_usage_stats.trace_id` **可直接 JOIN**，
于是「这一轮花了多少 token」与「这些 token 分别花在哪个块」可按同一 trace 聚合。

## 落库口径

一行 = 一个 (trace, 块) 对：

| 列 | 含义 |
|---|---|
| `trace_id` | 链路号（与 `llm_usage_stats.trace_id` 同源） |
| `conversation_id` | 所属会话（0=会话外） |
| `block_name` | 块 key（`role`/`retrieval`/`memory`/…） |
| `layer` | `static`（可缓存区）/ `dynamic`（动态区）—— 对应 `PROMPT_CACHEABLE_KEYS` 分界 |
| `tokens` | 该块 token 估算 |
| `chars` | 该块字符数（供"token/字符比"诊断，中英混排时二者不成比例） |
| `phase` | 装配阶段（`system`=system prompt 装配） |

另有一张**聚合视图** `context_audit_summary`：每 (trace, phase) 一行，存总量与
`retrieval_effective` / `memory_unique` 等**去重后**指标 —— 这两个指标直接量化
"检索 3 槽实际只换到 1.14 条"与"同一条记忆注入两次"两个 P0 缺陷。

## 纪律

- **观测绝不阻断主流程**：所有写库异常一律吞掉（对齐本仓「降级必须留痕但不阻断」惯例）。
- **默认开启但可关**：配置 `context_audit.enabled`（默认 True）；关闭时零开销。
- **不存 prompt 正文**：只存块名与 token 数，**不落任何用户内容**（合规）。
"""

from __future__ import annotations

import json
import threading

# 进程内聚合缓冲：一次装配可能产出 22 行，直接逐行 INSERT 太碎 ⇒ 先聚合再落库。
_BUF: dict = {}
_BUF_LOCK = threading.Lock()

#: 单次请求内累计的块级 token（供调用方读取"这轮拼了多少"而不必查库）
_LAST: dict = {}


def _enabled() -> bool:
    try:
        from core import config as _cfg
        return bool(_cfg.get("context_audit", "enabled", True))
    except Exception:
        return False


def _layer_of(block_name: str) -> str:
    """块 → 层归属（static=可缓存区 / dynamic=动态区）。

    与 `common.PROMPT_CACHEABLE_KEYS` 的分界保持一致；未知块归 dynamic（保守：宁可标成
    动态区提醒"它会破坏缓存前缀"，也不要误标成静态区掩盖问题）。
    """
    try:
        from agent.pipeline_parts.common import PROMPT_CACHEABLE_KEYS
        return "static" if block_name in PROMPT_CACHEABLE_KEYS else "dynamic"
    except Exception:
        return "dynamic"


def record_blocks(blocks: dict, phase: str = "system", extra: dict | None = None) -> int:
    """记录一组块的 token 归因。返回记录的总 token（失败返回 0）。

    `blocks` 是 `build_prompt_blocks()` 的产物（key → 文本）。
    空块跳过（与 `assemble_system_prompt` 的跳过口径一致 —— 否则归因会虚高）。

    `extra` 可携带本轮的附加指标（例：`{"retrieval_effective": 2, "memory_unique": 3}`），
    这些指标进聚合表，用于量化"名义条数 vs 有效条数"。
    ★ 2026-10-26 修正：允许 `blocks` 为空而 `extra` 非空 —— 这是「纯指标归因」的合法形态
      （例：JIT 回退、MCP 暴露、role 手册裁剪 都只有指标没有块）。
      此前 `if not blocks: return 0` **在入口就把这类调用丢弃**，
      导致这些指标从未落库（"记录了但查不到"，与本仓反复出现的
      "看起来绿实际空转"同族）。现改为 `blocks` 与 `extra` 皆空才返回。
    """
    if not blocks and not extra:
        return 0
    if not _enabled():
        return 0
    try:
        from core.token_counter import count_tokens
        from core.audit import trace_context

        ctx = trace_context() or {}
        trace_id = str(ctx.get("trace_id") or "")
        conv_id = int(ctx.get("conversation_id") or 0)

        rows = []
        total = 0
        for name, val in (blocks or {}).items():
            text = val if isinstance(val, str) else (json.dumps(val, ensure_ascii=False)
                                                    if val is not None else "")
            if not text:
                continue
            try:
                tk = int(count_tokens(text) or 0)
            except Exception:
                tk = 0
            rows.append((name, _layer_of(name), tk, len(text)))
            total += tk

        if not rows and not extra:
            return 0

        with _BUF_LOCK:
            key = (trace_id, conv_id, phase)
            cur = _BUF.get(key)
            if cur is None:
                cur = {"blocks": {}, "extra": {}, "total": 0, "chars": 0}
                _BUF[key] = cur
            for name, layer, tk, ch in rows:
                b = cur["blocks"].get(name)
                if b is None:
                    cur["blocks"][name] = {"layer": layer, "tokens": tk, "chars": ch}
                else:
                    b["tokens"] += tk
                    b["chars"] += ch
            cur["total"] += total
            cur["chars"] += sum(ch for _, _, _, ch in rows)
            for k, v in (extra or {}).items():
                try:
                    cur["extra"][k] = cur["extra"].get(k, 0) + (v if isinstance(v, (int, float)) else 1)
                except Exception:
                    pass
            _LAST.clear()
            _LAST.update({"trace_id": trace_id, "conversation_id": conv_id,
                          "phase": phase, "total_tokens": total,
                          "blocks": {n: {"tokens": t, "layer": l}
                                     for n, (l, t, _c) in
                                     ((n, (v["layer"], v["tokens"], v["chars"]))
                                      for n, v in cur["blocks"].items())}})
        return total
    except Exception:
        return 0


def last_summary() -> dict:
    """本进程最近一次装配的块级归因摘要（供调试/测试直接断言）。"""
    with _BUF_LOCK:
        return dict(_LAST)


def flush(force: bool = False) -> int:
    """把聚合缓冲落库。返回写入行数。

    调用时机：一次请求结束（stream/execute 的收口处）。`force=False` 时若缓冲为空则跳过。
    落库失败**不抛**（观测不阻断主流程）。
    """
    with _BUF_LOCK:
        if not _BUF:
            return 0
        items = list(_BUF.items())
        _BUF.clear()
    if not items:
        return 0
    try:
        from database import get_db
        conn = get_db()
        try:
            cur = conn.cursor()
            n = 0
            for (trace_id, conv_id, phase), data in items:
                blocks = data.get("blocks") or {}
                # 纯指标归因（blocks 空、extra 非空）也要写 summary 行
                if not blocks and not (data.get("extra") or {}):
                    continue
                for name, v in blocks.items():
                    cur.execute(
                        "INSERT INTO context_audit "
                        "(trace_id, conversation_id, block_name, layer, tokens, chars, phase) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (trace_id, conv_id, name, v.get("layer", "dynamic"),
                         int(v.get("tokens") or 0), int(v.get("chars") or 0), phase),
                    )
                    n += 1
                # 聚合行**始终写**（即便 metrics 为空）—— summary 表是"一次装配一行"的
                # 事实表，缺行会让"按 trace 查这轮总量"出现空洞。
                cur.execute(
                    "INSERT INTO context_audit_summary "
                    "(trace_id, conversation_id, phase, total_tokens, total_chars, "
                    " block_count, metrics) VALUES (?,?,?,?,?,?,?)",
                    (trace_id, conv_id, phase, int(data.get("total") or 0),
                     int(data.get("chars") or 0), len(blocks),
                     json.dumps(data.get("extra") or {}, ensure_ascii=False)),
                )
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception:
        return 0
