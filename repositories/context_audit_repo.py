# -*- coding: utf-8 -*-
"""上下文归因仓储（P0-8 · 2026-10-26）：把 `context_audit` 的落库数据变成**可读的报表**。

## 为什么要有它

`context_audit` / `context_audit_summary` 自 2026-10-26 起已在真实链路落库
（`agent/context_audit.py`，由 SSE 入口 `finally` 收口 `flush()`），
但**没有任何消费方** —— `routers/` 下只有一处 `flush()` 调用，无查询接口、无报表。

后果（盘点文档 §2.2 已记录）：
  · 数据在库里，业务方看不见；
  · 每个开关该开还是关，只能靠手工 SQL 猜；
  · 长期无人查看 ⇒ 退化成"又一个没人用的日志表"。

本模块只做**只读聚合**，不含任何写操作。

## 三个视图（回答三个不同问题）

| 视图 | 回答的问题 | 用途 |
|---|---|---|
| `blocks(days)` | **每轮 token 花在哪个块**（按块聚合 tokens/chars 占比） | 找头部消耗块 |
| `daily(days)` | **趋势**：每天 system 块总量、静态/动态比、各项改动指标 | 开关前后对照 |
| `conversations(days)` | **按会话**：哪些会话最贵、归因合计 vs 实际计费差额 | 定位异常会话 |

## 口径纪律

- `context_audit` 只记 **system prompt 的块**（messages / tools payload 不在其中）⇒
  `blocks()` 的总和 **必然小于** `llm_usage_stats.prompt_tokens`，差额 = messages + tools。
  这**不是 bug**，报表里明确标注，避免误读。
- `metrics` 存的是**累加计数**（`record_blocks` 的 extra 只支持数值，字符串会被记成 1）
  ⇒ `role_manual_applied`、`jit_fallback_full`、`mcp_selected` 这类键用「出现次数」解读。
- 时间过滤用 `created_at >= datetime('now', '-N days')`（SQLite 本地时间口径）。
"""
from __future__ import annotations

import json

from repositories.base import BaseRepo

#: 块名 → 中文说明（报表可读性）。未登记的块名原样输出。
BLOCK_LABELS = {
    "role": "Agent 身份/职责",
    "tools": "可用工具清单",
    "tool_rules": "工具使用约束",
    "roster": "团队名册",
    "ontology": "本体 schema",
    "boundary": "本体边界说明",
    "output_rules": "输出规范",
    "citation_rules": "引用规范",
    "intent_line": "当前意图",
    "model_code_req": "建模输出要求",
    "skill_prompt": "技能渐进披露",
    "skill_block": "显式技能正文",
    "template": "提示词模板",
    "memory": "长期记忆",
    "model_context": "建模上下文",
    "project_memory": "项目记忆",
    "slots": "任务拆解",
    "user_ctx": "用户上下文",
    "attachment": "上传资料",
    "retrieval": "检索结果",
    "report": "报告模板",
}

#: 归因指标 → 中文说明（`metrics` JSON 的键）
METRIC_LABELS = {
    "retrieval_effective": "检索有效条数",
    "retrieval_nominal": "检索名义条数",
    "retrieval_selected": "检索入选条数",
    "retrieval_docs": "检索覆盖文档数",
    "retrieval_merged": "近邻合并次数",
    "memory_unique": "记忆去重后条数",
    "memory_injected": "记忆注入条数",
    "jit_fallback_full": "JIT 全量回退次数",
    "jit_fallback_top_n": "JIT top-N 回退时选中工具数",
    "jit_fallback_no_scores": "JIT 无分数回退次数",
    "tool_candidates": "工具候选数",
    "tool_payload_total": "工具载荷总数",
    "whitelist_exempted_mcp": "白名单豁免的 MCP 工具数",
    "whitelist_size": "技能白名单条目数",
    "mcp_servers": "MCP server 数",
    "mcp_meta_servers": "有元数据的 MCP server 数",
    "mcp_total": "MCP 工具总数",
    "mcp_selected": "MCP 选中数",
    "mcp_injected": "MCP 实际注入数",
    "mcp_semantic": "MCP 语义命中数",
    "cache_hit": "语义缓存命中次数",
    "cache_answer_chars": "命中答案字符数",
    "role_manual_applied": "role 手册裁剪生效次数",
    "role_manual_nothing_to_drop": "role 手册无可裁次数",
    "role_manual_no_exposed": "role 手册未暴露工具次数",
    "role_manual_segments": "role 手册段总数",
    "role_manual_manual": "role 手册手写段数",
    "role_manual_kept": "role 手册保留段数",
    "role_manual_dropped_chars": "role 手册裁掉字符数",
    "role_manual_exposed": "role 裁剪时暴露工具数",
    "retrieval_effective_ratio": "检索信息密度",
}

#: 指标**语义类型** —— 决定"跨轮聚合时该求和还是取均值"。默认 `gauge`。
#:
#: ★ 2026-10-27 修正（这是个会让观测工具变成误导工具的缺陷）：
#:   原 `switches()` 对**所有**指标一律累加。但两类指标语义完全不同：
#:
#:   · `count`（计数型）：一次装配里"发生了几次"，累加 = 窗口内总次数。
#:     例：`jit_fallback_full=1` 表示这轮发生了全量回退 ⇒ 累加 16 表示 16 次。
#:   · `gauge`（快照型）：一次装配里"当时是多少"，累加无意义。
#:     例：`mcp_selected=8` 表示这轮暴露了 8 个工具；累加 16 会被读成
#:     "暴露了 16 个工具"，而真实是"两轮各 8 个" —— **前端若显示 16/71，
#:     会让人误判为暴露了一半工具，实际每轮 8/71**。
#:
#:   ⇒ 默认按 `gauge` 处理（保守：不理解语义的指标宁可给均值，也不给会翻倍的和）。
#:     只有**明确列入**下表的才是计数型。新增指标时若忘了登记，最坏也只是
#:     少一个"总和"，不会产生错误的"总数"。
METRIC_KINDS = {
    # 计数型：每轮 0/1 或"发生次数"，累加有意义
    "jit_fallback_full": "count",
    "jit_fallback_no_scores": "count",
    "retrieval_merged": "count",
    "cache_hit": "count",
    "role_manual_applied": "count",
    "role_manual_nothing_to_drop": "count",
    "role_manual_no_exposed": "count",
}

#: 指标单位（供前端后缀显示；缺省为空）
METRIC_UNITS = {
    "role_manual_dropped_chars": "字",
    "cache_answer_chars": "字",
    "jit_fallback_full": "次",
    "jit_fallback_no_scores": "次",
    "cache_hit": "次",
    "retrieval_merged": "次",
    "role_manual_applied": "次",
    "role_manual_nothing_to_drop": "次",
    "role_manual_no_exposed": "次",
}


#: 动态区块顺序（**权威来源** = `agent.pipeline_parts.common.PROMPT_DYNAMIC_KEYS`）。
#: 为什么需要它：`blocks()` 的行是按 token 降序排的，**不是 prompt 拼接顺序**
#: ⇒ 不能靠"行里第一个 dynamic"来判定缓存断点（那只是"最大的动态块"）。
#: 断点必须按 prompt 顺序找：第一个"每轮可能变"的块，它之后的内容都不命中前缀缓存。
try:
    from agent.pipeline_parts.common import PROMPT_DYNAMIC_KEYS as _DYN_ORDER
except Exception:                                    # pragma: no cover
    _DYN_ORDER = ()


class ContextAuditRepo(BaseRepo):
    """只读聚合。所有方法均不写库。"""

    # ── 就绪自检 ──────────────────────────────────────────────
    def ready(self) -> tuple:
        """检查归因表是否存在。返回 `(ok, hint)`。

        ⚠️ 为什么必须显式检查（而不是让 SQL 直接抛错）：
          `context_audit` 由 `database/schema.py` 的 `init_db()` 建表，
          而 `init_db()` 只在**应用启动**时跑。⇒ 任何"离线读库"的场景
          （命令行报表 / 运维脚本 / 指向旧快照的只读连接）都会遇到
          `sqlite3.OperationalError: no such table`。
          本方法把它变成**可解释的提示**，而不是一堆 traceback。
        """
        try:
            names = {r["name"] for r in self.rows(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except Exception as e:
            return False, f"数据库不可读：{type(e).__name__}: {e}"
        missing = [t for t in ("context_audit", "context_audit_summary") if t not in names]
        if missing:
            return False, (
                f"归因表不存在（缺 {', '.join(missing)}）。"
                "原因：建表在 init_db() 里，只在应用启动时执行。"
                "处理：启动一次服务（自动迁移），或先跑 "
                "`./.venv/Scripts/python.exe -X utf8 -c \"from database import init_db; init_db()\"`。"
            )
        return True, ""

    def _guard(self) -> tuple:
        return self.ready()

    # ══════════════════════════════════════════════════════════
    # 视图 1：按块聚合（找出头部消耗块）
    # ══════════════════════════════════════════════════════════
    def blocks(self, days: int = 7, limit: int = 40) -> dict:
        """按块聚合 token / 字符 / 出现次数，并给出占比。

        返回：
          {
            "window_days": 7,
            "rows": [{block, label, layer, tokens, chars, hits, token_share, tok_per_hit}],
            "total_tokens": N, "total_chars": N, "total_hits": N,
            "layers": {"static": N, "dynamic": N},
            "note": "..."
          }
        """
        days = max(1, int(days or 7))
        ok, hint = self._guard()
        if not ok:
            return {"window_days": days, "rows": [], "total_tokens": 0, "total_chars": 0,
                    "total_hits": 0, "layers": {}, "ready": False, "hint": hint}
        rows = self.rows(
            "SELECT block_name AS block, layer, "
            "       SUM(tokens) AS tokens, SUM(chars) AS chars, COUNT(*) AS hits "
            "FROM context_audit "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY block_name, layer ORDER BY tokens DESC LIMIT ?",
            (f"-{days} days", max(1, int(limit))),
        )
        total_tokens = sum(int(r["tokens"] or 0) for r in rows)
        total_chars = sum(int(r["chars"] or 0) for r in rows)
        total_hits = sum(int(r["hits"] or 0) for r in rows)
        # ★ 2026-10-27：`total_hits` 是**块行数之和**（每个块在每轮记一行），
        #   不是装配次数。实测 106 行只对应 8 轮 ⇒ 用它当分母算"每轮均值"会小 13 倍。
        #   真正的装配次数必须 `COUNT(DISTINCT trace|conv|phase)` 单独查。
        #   （前端 KPI 用它当分母，所以这个字段算错会让整个面板的首屏数字失真。）
        try:
            _asm = self.one(
                "SELECT COUNT(*) AS n FROM (SELECT DISTINCT trace_id, conversation_id, phase "
                "FROM context_audit WHERE created_at >= datetime('now', ?))",
                (f"-{days} days",))
            assemblies = int((_asm or {}).get("n") or 0) if isinstance(_asm, dict) else 0
        except Exception:
            assemblies = 0
        layers = {}
        out = []
        for r in rows:
            tk = int(r["tokens"] or 0)
            hits = int(r["hits"] or 0)
            layers[r["layer"]] = layers.get(r["layer"], 0) + tk
            out.append({
                "block": r["block"],
                "label": BLOCK_LABELS.get(r["block"], r["block"]),
                "layer": r["layer"],
                "tokens": tk,
                "chars": int(r["chars"] or 0),
                "hits": hits,
                "token_share": round(tk / total_tokens * 100, 1) if total_tokens else 0.0,
                # 单次装配的平均 token —— 比总量更能反映"每轮成本"
                "tok_per_hit": round(tk / hits, 1) if hits else 0.0,
            })
        # ★ 缓存断点：按 `PROMPT_DYNAMIC_KEYS` 的**拼接顺序**找第一个出现的动态块。
        #   ⚠️ 不能用 `out[0]`（那是 token 最大的块，不是顺序最靠前的块）。
        #   它之后的所有内容都不命中 prompt 前缀缓存 ⇒ 这个块名 + 其 token 是
        #   "还有多少可以靠调整顺序救回来"的直接依据。
        _present = {r["block"] for r in out}
        _boundary = next((k for k in _DYN_ORDER if k in _present), "")
        _b_tok = next((r["tokens"] for r in out if r["block"] == _boundary), 0) if _boundary else 0

        return {
            "ready": True,
            "window_days": days,
            "rows": out,
            "total_tokens": total_tokens,
            "total_chars": total_chars,
            # ⚠️ `total_hits` = 块行数之和（**不是**装配次数，保留仅为兼容既有调用方）
            "total_hits": total_hits,
            # ✅ 真正可当分母的装配次数（每轮 system 装配算 1 次）
            "assemblies": assemblies,
            "avg_system_tokens": round(total_tokens / assemblies, 1) if assemblies else 0.0,
            # ✅ 缓存断点（prompt 顺序上的第一个动态块）
            "cache_boundary": _boundary,
            "cache_boundary_tokens": _b_tok,
            "dynamic_order": [k for k in _DYN_ORDER if k in _present],
            "layers": layers,
            "note": ("context_audit 只记 system prompt 的块；messages 与 tools payload "
                     "不在其中。与 llm_usage_stats.prompt_tokens 的差额 = messages + tools，"
                     "不是 bug。`total_hits` 是块行数之和，算「每轮均值」请用 `assemblies`；"
                     "`cache_boundary` 按 prompt 拼接顺序判定，不按 token 大小。"),
        }

    # ══════════════════════════════════════════════════════════
    # 视图 2：按日趋势 + 归因指标（开关前后对照）
    # ══════════════════════════════════════════════════════════
    def daily(self, days: int = 14) -> dict:
        """按日聚合：system 块总量、静态/动态比、各项改动指标累计。

        返回：
          {
            "window_days": 14,
            "rows": [{date, traces, block_rows, tokens, chars,
                      static_tokens, dynamic_tokens, static_ratio, metrics:{...}}],
            "metrics_legend": {...}
          }
        """
        days = max(1, int(days or 14))
        ok, hint = self._guard()
        if not ok:
            return {"ready": False, "hint": hint, "window_days": days, "rows": [],
                    "metrics_legend": METRIC_LABELS}
        blk = self.rows(
            "SELECT substr(created_at, 1, 10) AS d, "
            "       COUNT(DISTINCT trace_id || '|' || conversation_id || '|' || phase) AS traces, "
            "       COUNT(*) AS block_rows, SUM(tokens) AS tokens, SUM(chars) AS chars, "
            "       SUM(CASE WHEN layer='static' THEN tokens ELSE 0 END) AS static_tokens, "
            "       SUM(CASE WHEN layer='dynamic' THEN tokens ELSE 0 END) AS dynamic_tokens "
            "FROM context_audit WHERE created_at >= datetime('now', ?) "
            "GROUP BY d ORDER BY d",
            (f"-{days} days",),
        )
        # 指标按日累加（metrics 是 JSON，SQLite 不能直接聚合 ⇒ 拉到 Python 里汇）
        mrows = self.rows(
            "SELECT substr(created_at, 1, 10) AS d, metrics FROM context_audit_summary "
            "WHERE created_at >= datetime('now', ?) AND metrics IS NOT NULL AND metrics != '' "
            "  AND metrics != '{}'",
            (f"-{days} days",),
        )
        by_day = {}
        for m in mrows:
            try:
                kv = json.loads(m["metrics"] or "{}")
            except Exception:
                continue
            if not isinstance(kv, dict):
                continue
            bucket = by_day.setdefault(m["d"], {})
            for k, v in kv.items():
                if isinstance(v, (int, float)):
                    bucket[k] = bucket.get(k, 0) + v
        out = []
        for r in blk:
            st = int(r["static_tokens"] or 0)
            dy = int(r["dynamic_tokens"] or 0)
            tot = st + dy
            out.append({
                "date": r["d"],
                "traces": int(r["traces"] or 0),
                "block_rows": int(r["block_rows"] or 0),
                "tokens": int(r["tokens"] or 0),
                "chars": int(r["chars"] or 0),
                "static_tokens": st,
                "dynamic_tokens": dy,
                "static_ratio": round(st / tot * 100, 1) if tot else 0.0,
                "metrics": by_day.get(r["d"], {}),
            })
        return {"ready": True, "window_days": days, "rows": out,
                "metrics_legend": METRIC_LABELS}

    # ══════════════════════════════════════════════════════════
    # 视图 3：按会话明细（定位异常会话 + 归因 vs 计费差额）
    # ══════════════════════════════════════════════════════════
    def conversations(self, days: int = 7, limit: int = 20, conversation_id: int = 0) -> dict:
        """按会话（或单个会话的按 trace 明细）聚合。

        `conversation_id > 0` 时返回该会话的逐 trace 明细（含与
        `llm_usage_stats.prompt_tokens` 的差额），否则返回最贵会话 TOP N。
        """
        days = max(1, int(days or 7))
        cid = int(conversation_id or 0)
        ok, hint = self._guard()
        if not ok:
            return {"ready": False, "hint": hint, "window_days": days,
                    "conversation_id": cid, "traces": [], "conversations": []}
        if cid > 0:
            tr = self.rows(
                "SELECT trace_id, phase, SUM(tokens) AS tokens, SUM(chars) AS chars, "
                "       COUNT(*) AS block_count, MIN(created_at) AS at "
                "FROM context_audit WHERE conversation_id = ? "
                "GROUP BY trace_id, phase ORDER BY at DESC LIMIT ?",
                (cid, max(1, int(limit))),
            )
            # 关联实际计费：同 trace 的 prompt_tokens 合计（含缓存命中/未命中）
            billed = {}
            for b in self.rows(
                "SELECT trace_id, SUM(prompt_tokens) AS pt, "
                "       SUM(prompt_cache_hit_tokens) AS hit, "
                "       SUM(prompt_cache_miss_tokens) AS miss, COUNT(*) AS calls "
                "FROM llm_usage_stats WHERE conversation_id = ? AND used_mock = 0 "
                "  AND trace_id IS NOT NULL AND trace_id != '' "
                "GROUP BY trace_id", (cid,)
            ):
                billed[b["trace_id"]] = {
                    "prompt_tokens": int(b["pt"] or 0),
                    "cache_hit": int(b["hit"] or 0),
                    "cache_miss": int(b["miss"] or 0),
                    "llm_calls": int(b["calls"] or 0),
                }
            out = []
            for r in tr:
                tk = int(r["tokens"] or 0)
                b = billed.get(r["trace_id"]) or {}
                pt = int(b.get("prompt_tokens") or 0)
                out.append({
                    "trace_id": r["trace_id"],
                    "phase": r["phase"],
                    "at": r["at"],
                    "system_tokens": tk,
                    "chars": int(r["chars"] or 0),
                    "block_count": int(r["block_count"] or 0),
                    "billed_prompt_tokens": pt,
                    "llm_calls": int(b.get("llm_calls") or 0),
                    # 差额 = messages + tools payload（>0 才符合预期）
                    "delta_messages_tools": (pt - tk) if pt else None,
                    "cache_hit": b.get("cache_hit"),
                    "cache_miss": b.get("cache_miss"),
                })
            return {"ready": True, "conversation_id": cid, "window_days": days, "traces": out}

        # ★ 2026-10-27：LEFT JOIN conversations 取 title —— 否则列表里全是「会话 9613」，
        #   无法判断"最贵的那几个到底在干什么"，最贵 TOP N 就失去了行动价值。
        rows = self.rows(
            "SELECT a.conversation_id, COUNT(DISTINCT a.trace_id) AS traces, "
            "       SUM(a.tokens) AS tokens, COUNT(*) AS block_rows, "
            "       MAX(a.created_at) AS last_at, "
            "       COALESCE(c.title, '') AS title "
            "FROM context_audit a "
            "LEFT JOIN conversations c ON c.id = a.conversation_id "
            "WHERE a.created_at >= datetime('now', ?) AND a.conversation_id > 0 "
            "GROUP BY a.conversation_id ORDER BY tokens DESC LIMIT ?",
            (f"-{days} days", max(1, int(limit))),
        )
        return {
            "ready": True,
            "window_days": days,
            "conversations": [{
                "conversation_id": int(r["conversation_id"] or 0),
                "title": r["title"] or "",
                "traces": int(r["traces"] or 0),
                # `tokens` 与 `system_tokens` 同值：前者对齐前端习惯，后者保持既有字段兼容
                "tokens": int(r["tokens"] or 0),
                "system_tokens": int(r["tokens"] or 0),
                "block_rows": int(r["block_rows"] or 0),
                "last_at": r["last_at"],
            } for r in rows],
        }

    # ══════════════════════════════════════════════════════════
    # 视图 4：开关效果自查（直接回答"改动生效了吗"）
    # ══════════════════════════════════════════════════════════
    def switches(self, days: int = 7) -> dict:
        """汇总各优化项/开关的**实际运行情况** —— 回答"改了但到底生效了吗"。

        只读 `context_audit_summary.metrics`，不读配置（配置说明"打算怎么配"，
        这里说明"实际跑成什么样"，两者必须分开看）。

        ★ 2026-10-27：**按指标语义分开聚合**（此前一律累加，会把快照型指标算错）。
        每项返回：

        | 字段 | 含义 |
        |---|---|
        | `kind` | `count`（计数型）/ `gauge`（快照型） |
        | `value` | **前端应直接显示的值**：`count`→`sum`，`gauge`→`avg` |
        | `sum` | 窗口内累加（仅 `count` 型有意义） |
        | `avg` / `max` / `last` | 快照型的均值/峰值/最近值 |
        | `samples` | 该指标实际出现的装配次数（不是窗口内总轮次） |
        | `unit` | 单位后缀（字/次/条…，可为空） |

        `value` 与 `kind` 一起下发，前端**不必自己判断**该显示和还是均值 ——
        判断逻辑只存一份，避免前后端各写一遍必然漂移。
        """
        days = max(1, int(days or 7))
        ok, hint = self._guard()
        if not ok:
            return {"ready": False, "hint": hint, "window_days": days,
                    "summary_rows": 0, "metrics": [],
                    "legend": {"labels": METRIC_LABELS, "kinds": METRIC_KINDS}}
        rows = self.rows(
            "SELECT metrics FROM context_audit_summary "
            "WHERE created_at >= datetime('now', ?) AND metrics IS NOT NULL "
            "  AND metrics != '' AND metrics != '{}'", (f"-{days} days",)
        )
        # 每指标收集全部取值（窗口内该指标出现过的每一轮都收一条）
        series = {}
        for r in rows:
            try:
                kv = json.loads(r["metrics"] or "{}")
            except Exception:
                continue
            if not isinstance(kv, dict):
                continue
            for k, v in kv.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    series.setdefault(k, []).append(float(v))

        out = []
        for k, vals in series.items():
            kind = METRIC_KINDS.get(k, "gauge")
            n = len(vals)
            s = sum(vals)
            avg = s / n if n else 0.0
            # count → 显示总和；gauge → 显示均值（快照型求和是错的）
            value = s if kind == "count" else avg
            out.append({
                "metric": k,
                "label": METRIC_LABELS.get(k, k),
                "kind": kind,
                "value": int(value) if float(value).is_integer() else round(value, 2),
                "sum": int(s) if float(s).is_integer() else round(s, 2),
                "avg": int(avg) if float(avg).is_integer() else round(avg, 2),
                "max": int(max(vals)) if float(max(vals)).is_integer() else round(max(vals), 2),
                "last": int(vals[-1]) if float(vals[-1]).is_integer() else round(vals[-1], 2),
                "samples": n,
                "unit": METRIC_UNITS.get(k, ""),
            })
        # 排序：计数型在前（它们是"是否生效"的直接答案），再按显示值降序
        out.sort(key=lambda x: (0 if x["kind"] == "count" else 1, -float(x["value"])))
        return {"ready": True, "window_days": days, "summary_rows": len(rows),
                "metrics": out,
                "legend": {"labels": METRIC_LABELS, "kinds": METRIC_KINDS}}
