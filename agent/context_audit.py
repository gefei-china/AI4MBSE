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

#: 已上报过的"词表外 block_name"（每个名字只报一次，避免热路径刷屏）
_VOCAB_UNKNOWN_SEEN: set = set()
_VOCAB_LOCK = threading.Lock()


def _audit_block_vocab(blocks: dict) -> list:
    """校验即将写入 `context_audit` 的 `block_name` 是否都已在事件词表内。

    ## 为什么要在**写路径**校验（2026-11-16）

    `context_audit.block_name` 的消费方是 `core.event_vocabulary.validate_event` ——
    读路径遇到词表外类型且无 `ignorable` 会**抛 `UnknownEventError` ⇒ 会话回放失败**。
    而写路径此前**从不校验**，于是"漏登记"只会在很久之后以"回放失败"的形式暴露。

    实测发生过**三次**，全都是同一个形态（新增 prompt 块时忘了登记）：
      · `artifact_facts` / `output_contract`（2026-11-15 由门禁抓到）
      · `artifact_baseline`（2026-11-16 由门禁抓到，common.py:326/698 早就有了）

    ⇒ 根因不是"记性不好"，而是**"加块"与"登记"分属两个动作且无强耦合**。
      门禁能抓，但**抓在事后**（要等有人跑全量）。这里改成运行期即时上报：
      漏登记的那一刻就进观测台账，不用等到门禁/回放失败。

    ## 三条纪律
    1. **绝不阻断主流程**：整个函数包在 try 里，任何异常吞掉。
       校验的价值是"看见"，不是"拦截"——拦住会让一次拼写错误变成整轮对话不可用，
       代价远大于收益（与 `context_audit` 整体"观测绝不阻断"一致）。
    2. **每个名字只报一次**：`record_blocks` 是每轮热路径，若每次都记会让台账被同一个
       名字刷爆、反而盖住别的问题。去重后"首次出现即报警"。
    3. **上报不落库、不加表**：走 `core.observe_fail`（既有观测通道），
       与 `rag.route_stat` / `trace_query` 同族，不新增 schema。

    :returns: 本次新发现的未登记名字列表（供门禁/单测断言；正常为空列表）
    """
    try:
        # ⚠️ 必须是 dict：若传进来字符串，`for n in blocks` 会**逐字符**迭代
        #   ⇒ 把 'a'/'c'/'x' 当成 block_name 上报（实测踩到）。
        #   `record_blocks` 的调用方都传 dict，但这是热路径上的公共函数，
        #   不该假设调用方永远正确 —— 类型不符直接当"无可校验"返回。
        if not isinstance(blocks, dict) or not blocks:
            return []
        from core.event_vocabulary import KNOWN_EVENT_TYPES
        unknown = [n for n in blocks if n and n not in KNOWN_EVENT_TYPES]
        if not unknown:
            return []
        with _VOCAB_LOCK:
            fresh = [n for n in unknown if n not in _VOCAB_UNKNOWN_SEEN]
            _VOCAB_UNKNOWN_SEEN.update(unknown)
        for name in fresh:
            try:
                from core.observe_fail import note
                note("prompt_block_vocab",
                     ValueError("block_name 未登记进 core/event_vocabulary.py 的 "
                                "KNOWN_EVENT_TYPES: %r（读路径 validate_event 会拒绝它）"
                                % name),
                     block=name)
            except Exception:
                pass
        return fresh
    except Exception:
        return []


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
    # ★ 2026-11-16：写路径即时校验 block_name 词表覆盖（只上报，不阻断）——
    #   把这个反复出现三次的"漏登记"从事后（门禁/回放失败）提前到**写入那一刻**。
    #   放在 _enabled() 之后：审计关掉时不产生观测噪音（关审计就是不要这些）。
    _audit_block_vocab(blocks or {})
    try:
        from core.token_counter import count_tokens
        from core.audit import trace_context

        ctx = trace_context() or {}
        trace_id = str(ctx.get("trace_id") or "")
        conv_id = int(ctx.get("conversation_id") or 0)
        # ★ 2026-11-10（run 维度下沉）：块级归因必须带批次坐标。
        #   下沉前 `context_audit` 只有 trace_id + conversation_id ⇒ 一个会话跑过多轮编排时
        #   **无法回答"这一批 40 个块属于哪个 run、哪个子任务"**；而 `task_queue.run_id` /
        #   `tool_call_logs.run_id` / `orch_run_spans.run_id` 早已齐备，唯独判据面缺这一列。
        #   键名与 `core/audit._TRACE_KEYS` 对齐（run_id / sub_task_key），不另造口径。
        try:
            run_id = int(ctx.get("run_id") or 0)
        except (TypeError, ValueError):
            run_id = 0
        sub_task_key = str(ctx.get("sub_task_key") or "")

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
            # ⚠️ 键里必须含 run_id：同一 trace 下若先后跑了两批编排（恢复/重跑都会复用 trace），
            #    只按 (trace, conv, phase) 聚合会把两批的块**相加**，产出"这批花了 8k"的假数。
            key = (trace_id, conv_id, run_id, phase)
            cur = _BUF.get(key)
            if cur is None:
                cur = {"blocks": {}, "extra": {}, "total": 0, "chars": 0,
                       "sub_task_key": sub_task_key}
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
                          "run_id": run_id, "sub_task_key": sub_task_key,
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
            # ★ 2026-11-10：老库（迁移未跑）没有 run_id/sub_task_key 列时必须**降级写旧列**。
            #   若直接写新列，`no such column` 会让整批归因**一条不落**，
            #   而本函数整体被 `except: return 0` 吞掉 ⇒ 观测静默归零（本仓反复踩的形态）。
            #   判据用 PRAGMA 实测列，不按版本猜。
            _cols = set()
            try:
                _cols = {r[1] for r in cur.execute(
                    "PRAGMA table_info(context_audit_summary)").fetchall()}
            except Exception:
                _cols = set()
            _new = ("run_id" in _cols) and ("sub_task_key" in _cols)
            for (trace_id, conv_id, run_id, phase), data in items:
                blocks = data.get("blocks") or {}
                # 纯指标归因（blocks 空、extra 非空）也要写 summary 行
                if not blocks and not (data.get("extra") or {}):
                    continue
                for name, v in blocks.items():
                    if _new:
                        cur.execute(
                            "INSERT INTO context_audit "
                            "(trace_id, conversation_id, run_id, block_name, layer, tokens, chars, phase) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (trace_id, conv_id, int(run_id or 0), name, v.get("layer", "dynamic"),
                             int(v.get("tokens") or 0), int(v.get("chars") or 0), phase),
                        )
                    else:
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
                if _new:
                    cur.execute(
                        "INSERT INTO context_audit_summary "
                        "(trace_id, conversation_id, run_id, phase, total_tokens, total_chars, "
                        " block_count, metrics, sub_task_key) VALUES (?,?,?,?,?,?,?,?,?)",
                        (trace_id, conv_id, int(run_id or 0), phase, int(data.get("total") or 0),
                         int(data.get("chars") or 0), len(blocks),
                         json.dumps(data.get("extra") or {}, ensure_ascii=False),
                         str(data.get("sub_task_key") or "")),
                    )
                else:
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
