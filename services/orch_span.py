# -*- coding: utf-8 -*-
"""编排 run 级 span 树（观测性 trace，2026-10-20，Task #43）。

**为什么需要**（对标 Manus/LangGraph 的 trace 差距）：
    一次真实编排 55s~344s，此前只能从 `orch_stream_events`（stage/tool 事件流）
    与 `agent_tasks.latency_ms` 反推"慢在哪"——事件流**没有父子结构、没有起止对**，
    算不出每段耗时；per-task latency 又只有子任务、没有 plan/summary/reflection/views。
    ⇒ "时间花在哪、每个节点成本多少"这个问题**此前无数据可答**。

**与 orch_stream_events 的分工（互补不重复）**：
    · stream_events = 事件流（stage/tool 帧）→ 回放"发生了什么"；
    · 本表 = span 树（开/关时刻对）→ 聚合"每段多久、烧多少 token"。

**为什么是"内存收集 → 流尾一次性 flush"而不是随开随写**：
    ① 插桩点在 worker 线程（task span）与主线程（其余五类）—— 随开随写要么
       给每个 worker 开独立连接（写锁竞争），要么跨线程共用连接（sqlite3 默认
       check_same_thread 直接抛）；
    ② 观测旁路失败**绝不阻断主链路**（本仓观测层铁律）—— 内存收集把 DB 交互
       压缩到流尾一次，失败只丢观测数据；
    ③ 代价：进程在流尾前崩溃会丢整棵 span 树 —— 与 orch_stream_events 的
       内存缓冲同款取舍（那层的 docstring 已记明），排障主路径是**已完成的 run**。

**时区口径**：started_at/ended_at 是 Python 本地时间串（仅展示）；latency_ms 取
    `time.monotonic()` 差值（不受墙钟跳变影响）—— 不做 SQL 内 julianday 相减，
    因为两端都是本地串、且 monotonic 对单进程内计时更准。

**失败语义**：`flush` 抛异常由调用方吞（wrapper 的 finally 里 try 包裹）；
    未闭合 span（异常路径）在 flush 时按 error 收口并带 note=unclosed_at_flush。
"""
import json
import threading
import time

KIND_RUN = "run"
KIND_PLAN = "plan"
KIND_TASK = "task"
KIND_SUMMARY = "summary"
KIND_REFLECTION = "reflection"
KIND_VIEWS = "views"
# P1 replanning（2026-10-21，执行中重规划）：**每一次**重规划判定都落一条
# replan span（含 action="none" —— "为什么不重画"与"为什么重画"同等重要，
# 对标 OpenAI Deep Research 停止原因显式 log）。parent=run span，meta_json 带
# signals/level/action/quality_gates/round/added/dropped/tokens/latency_ms。
# ⚠️ 新 kind 必须同步 tools/verify/verify_orch_span.py 的 S1b/S2c 断言与
# database/schema.py DDL 注释（Task #43 门禁 R11），否则 CI 红。
KIND_REPLAN = "replan"


def _enabled() -> bool:
    """总开关（core.config `orch_span.enabled`，默认开）。关 = 全链路零开销。"""
    try:
        from core.config import get as _get
        return bool(_get("orch_span", "enabled", True))
    except Exception:
        return True


class SpanCollector:
    """线程安全的内存 span 收集器。

    - `open()` 返回**内存 sid**（从 1 递增）；parent 参数传内存 sid。
    - `close()` 幂等（重复 close / 对已闭合 span 返回 False）。
    - `flush(conn)` 按 open 顺序一次性落库（父先于子 ⇒ parent_id 映射成立），
      返回写入行数；调用方负责 commit。
    - 全部方法在 `enabled=False` 时退化为 no-op（open 返回 0）。
    """

    def __init__(self, enabled: bool = None):
        self._enabled = _enabled() if enabled is None else bool(enabled)
        self._lock = threading.Lock()
        self._spans = {}          # sid -> span dict（插入序 = open 序）
        self._seq = 0
        self._run_id = 0
        self._conversation_id = 0

    # ── 采集面 ──────────────────────────────────────────────────────────────
    def bind(self, run_id, conversation_id=0):
        """run_id 落定后调用一次（plan span 早于 run_id 产生，落库时统一补齐）。"""
        with self._lock:
            self._run_id = int(run_id or 0)
            self._conversation_id = int(conversation_id or 0)

    def open(self, kind, name, parent=0, meta=None) -> int:
        if not self._enabled:
            return 0
        with self._lock:
            self._seq += 1
            sid = self._seq
            self._spans[sid] = {
                "id": sid,
                "kind": str(kind or "")[:32],
                "name": str(name or "")[:120],
                "parent": int(parent or 0),
                "status": "running",
                "t0": time.monotonic(),
                "t1": None,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "meta": dict(meta or {}),
            }
            return sid

    def close(self, sid, status="ok", meta=None) -> bool:
        if not self._enabled or not sid:
            return False
        with self._lock:
            sp = self._spans.get(int(sid))
            if not sp or sp["t1"] is not None:
                return False
            sp["t1"] = time.monotonic()
            sp["status"] = str(status or "ok")[:16]
            if meta:
                sp["meta"].update(meta)
            return True

    # ── 落库面 ──────────────────────────────────────────────────────────────
    def flush(self, conn) -> int:
        """一次性落库（调用方 commit）。返回写入行数；空收集器返回 0。"""
        if not self._enabled:
            return 0
        with self._lock:
            spans = [self._spans[k] for k in sorted(self._spans)]
        if not spans:
            return 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        id_map = {}
        n = 0
        for sp in spans:
            if sp["t1"] is None:                       # 异常路径未闭合 → error 收口
                sp["t1"] = time.monotonic()
                sp["status"] = "error"
                sp["meta"].setdefault("note", "unclosed_at_flush")
            lat = max(0, int((sp["t1"] - sp["t0"]) * 1000))
            cur = conn.execute(
                "INSERT INTO orch_run_spans (conversation_id, run_id, kind, name, parent_id,"
                " status, started_at, ended_at, latency_ms, meta_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self._conversation_id, self._run_id, sp["kind"], sp["name"],
                 id_map.get(sp["parent"], 0), sp["status"], sp["started_at"], now,
                 lat, json.dumps(sp["meta"], ensure_ascii=False)[:2000], now))
            id_map[sp["id"]] = int(cur.lastrowid or 0)
            n += 1
        return n


def run_trace(conn, run_id) -> dict:
    """装配某次编排的 span 树 + per-node 成本聚合（查询 API 用）。

    返回结构：
        {run_id, spans: [树], span_count, stats: {total_ms, by_kind, tasks, note}}
    - `spans` 是**根列表**（plan span 与 run span 均为根；task/summary/reflection/
      views 挂在 run 下 —— 后三段在时间上被 run 覆盖（run 收口于最末 done yield 前），
      挂 run 下 total_ms=plan+run 才等于端到端墙钟，并列成根会双算后段）。
    - `stats.total_ms` = 各根 span latency 之和（plan+run ≈ 端到端墙钟）；
      缺 span 的历史批次返回空树 + note（200，不报错——"没有 trace"不是错误）。
    """
    try:
        rows = conn.execute(
            "SELECT id, kind, name, parent_id, status, started_at, ended_at,"
            " latency_ms, meta_json FROM orch_run_spans WHERE run_id=? ORDER BY id",
            (int(run_id),)).fetchall()
    except Exception:
        rows = []
    spans = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        spans.append({"id": r["id"], "kind": r["kind"] or "", "name": r["name"] or "",
                      "parent_id": int(r["parent_id"] or 0), "status": r["status"] or "",
                      "started_at": r["started_at"], "ended_at": r["ended_at"],
                      "latency_ms": int(r["latency_ms"] or 0),
                      "meta": meta, "children": []})
    by_id = {s["id"]: s for s in spans}
    roots = []
    for s in spans:
        p = by_id.get(s["parent_id"])
        if p is not None and s["parent_id"] != s["id"]:
            p["children"].append(s)
        else:
            roots.append(s)
    by_kind = {}
    for s in spans:
        k = s["kind"] or "?"
        agg = by_kind.setdefault(k, {"count": 0, "ms": 0, "tokens": 0})
        agg["count"] += 1
        agg["ms"] += s["latency_ms"]
        try:
            agg["tokens"] += int((s["meta"] or {}).get("tokens") or 0)
        except Exception:
            pass
    tasks = []
    for s in spans:
        if s["kind"] == KIND_TASK:
            m = s["meta"] or {}
            try:
                tasks.append({"key": s["name"], "status": s["status"],
                              "latency_ms": s["latency_ms"],
                              "tokens": int(m.get("tokens") or 0),
                              "attempts": int(m.get("attempts") or 0),
                              "retry_count": int(m.get("retry_count") or 0),
                              "offload_id": int(m.get("offload_id") or 0),
                              "error": str(m.get("error") or "")[:120]})
            except Exception:
                pass
    return {"run_id": int(run_id), "spans": roots, "span_count": len(spans),
            "stats": {"total_ms": sum(s["latency_ms"] for s in roots),
                      "by_kind": by_kind, "tasks": tasks,
                      "note": "" if spans else
                      "该批次无 span 记录（可能产生于 trace 功能上线之前）"}}
