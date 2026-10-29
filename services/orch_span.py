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

# ── P0-b（2026-10-25）会话级 span 五类（生产回流设计 §3.2）──────────────────
# 覆盖面：编排流量（上方七类）之外的**会话直答主路径** —— /chat/stream 端到端。
# 树形：conversation（根，run_id=0）→ turn（ReAct 一轮）→ tool/rag/llm。
# 与编排 kind 的差异：conversation 的 run_id=0（非编排批次），反查走
# conversation_id + idx_ors_kind_time（S1 已建）；meta_json 键约定见
# docs/P0-b-生产回流详细设计-2026-10-25.md §3.2。
KIND_CONVERSATION = "conversation"
KIND_TURN = "turn"
KIND_TOOL = "tool"
KIND_RAG = "rag"
KIND_LLM = "llm"
#: 会话五类的 DDL 注释口径（verify_orch_span S1b 按此串断言，改 kind 必须同步）
CONVERSATION_KINDS_DDL = ("conversation | turn | tool | rag | llm"
                          "（P0-b 会话级 trace，2026-10-25）")


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

    def backdate(self, sid, t0) -> bool:
        """P0-b：把 span 的计时起点校正到 `t0`（time.time() 墙钟值）。

        为什么需要：tool/rag/llm span 的**开在挂载点函数入口**（此时才拿到
        tracer），而执行其实从更早的 t0 就开始（如 _exec_tool_call 的 t0）。
        不校正 ⇒ latency 少算一段、started_at 晚于真实起点（树形时序错位）。
        仅 P0-b ConversationTracer 调用；编排侧七类 span 开闭同点，无需校正。

        ⚠️ 时钟口径：latency 用 `time.monotonic()` 差值（文件头「时区口径」），
        而 t0 是 `time.time()` 墙钟值 ⇒ 必须先换算：
        `mono(t0) = mono(now) - (wall(now) - t0)`。直接赋值会混用两个时钟
        原点，latency 变成天文数字（1970 至今的秒数）。
        """
        if not self._enabled or not sid:
            return False
        with self._lock:
            sp = self._spans.get(int(sid))
            if not sp or sp["t1"] is not None:
                return False
            try:
                t0 = float(t0)
                sp["t0"] = time.monotonic() - (time.time() - t0)
                sp["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(t0))
            except Exception:
                return False
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


# ══════════════════════════════════════════════════════════════════════════
# P0-b（2026-10-25）会话级 trace：ConversationTracer
# 设计文档 docs/P0-b-生产回流详细设计-2026-10-25.md §4.2
# ══════════════════════════════════════════════════════════════════════════
#
# **为什么复用 SpanCollector 而不是另建收集器**：编排侧已证「内存收集 → 流尾
# 一次性 flush」在观测旁路上的正确性（失败只丢观测数据、零写锁竞争）；会话路径
# 与编排路径的 span 落同一张表，monitor 页才能做"同会话编排 vs 直答"对比查询
# （设计 §3.2「为什么不新建 run_spans 表」）。
#
# **为什么跨层访问走 ContextVar**：M3（tools.py）/M4（rag.py）/M5（llm）三个
# 挂载点深嵌调用链，逐个传参要改 8+ 处签名；而 /chat/stream 的同步生成器整体
# 跑在**同一个** daemon 线程（core/sse.py iter_with_heartbeat_async 的 worker），
# ContextVar.set 在该线程体内生效即全线可见。⚠️ 编排子任务跑在 ThreadPoolExecutor
# 线程——CPython 新线程持**空 Context**（PEP 567），不会误继承外层 tracer。
#
# **失败语义（不静默）**：所有 span 操作的异常记入 self.failures（有界）并打
# `[conv-trace]` 日志；flush 由调用方（M1 路由器 finally）用独立连接执行，
# 失败同样只丢观测数据——绝不影响用户拿结果（观测旁路铁律同编排侧）。

import contextlib as _contextlib


class ConversationTracer:
    """会话级 trace：/chat/stream 入口开 conversation span（根），提供
    turn/tool/rag/llm 四类子 span。内部持一个 SpanCollector。

    用法（M1 路由器）::

        tracer = ConversationTracer(conversation_id, trace_id=trace_id)
        tracer.begin_conversation(meta={"intent": ..., "route": ...})
        try:
            ...  # M2-M5 经 ContextVar/参数拿到 tracer 打点
        finally:
            tracer.end_conversation(status="ok" if _terminal else "error")
            tracer.flush()          # 独立连接；失败只丢观测数据

    turn 用 with（生成器挂起/关闭都能正确闭 span）；tool/rag/llm 因挂载点
    跨函数边界（开在执行入口、闭在唯一记账点）改用 open/close 配对 + 线程局部
    栈——与 tool_call_logs/_record_usage 的落点同址，latency 取真实执行时长。
    """

    #: failures 上界（防异常风暴把内存吃满；超出记计数）
    _MAX_FAILURES = 50

    def __init__(self, conversation_id: int, trace_id: str = "",
                 enabled: bool | None = None):
        self.conversation_id = int(conversation_id or 0)
        self.trace_id = str(trace_id or "")
        self._enabled = _enabled() if enabled is None else bool(enabled)
        self._spans = SpanCollector(enabled=self._enabled)
        # run_id=0（非编排批次）；conversation_id 落定供 INSERT 用
        self._spans.bind(0, self.conversation_id)
        self._conv_sid = 0
        self.failures: list[str] = []
        self._fail_n = 0
        self._local = threading.local()

    # ── 失败记录（不静默）────────────────────────────────────────────────
    def _fail(self, where: str, exc: BaseException) -> None:
        self._fail_n += 1
        if len(self.failures) < self._MAX_FAILURES:
            self.failures.append("%s: %s: %s" % (where, type(exc).__name__,
                                                 str(exc)[:160]))
        print("[conv-trace] span 失败（不影响主链路）where=%s err=%r"
              % (where, exc))

    # ── 根 span ─────────────────────────────────────────────────────────
    def begin_conversation(self, meta: dict | None = None) -> int:
        """开 conversation 根 span（run_id=0）。返回内存 sid（0=未启用）。"""
        if not self._enabled:
            return 0
        try:
            self._conv_sid = self._spans.open(
                KIND_CONVERSATION, "conversation #%d" % self.conversation_id,
                parent=0, meta=dict(meta or {}))
            return self._conv_sid
        except Exception as e:      # noqa: BLE001 —— 观测旁路绝不阻断
            self._fail("begin_conversation", e)
            return 0

    def end_conversation(self, status: str = "ok", meta: dict | None = None) -> None:
        """闭合 conversation 根 span。

        ── S1（2026-10-29）失败必须带原因，否则这条 span 没有诊断价值 ──
        实测（@75eca26）：近 7 天 `conversation` span 有 **73 条 status='error'**，
        而 `meta_json` **全为 `{}`** —— 一条都回答不了"为什么失败"。
        根因有二：
          · 调用侧 `meta=dict(_conv_meta)`，而 `_conv_meta` 只在 **done 帧**里回填
            intent/route；失败的流根本没有 done 帧 ⇒ 传进来的就是空 dict；
          · 本方法对 `status != ok` 且 meta 为空的情形**不做任何补充**，
            于是"未闭合/无终态"这条唯一线索只在 `flush()` 里以
            `note='unclosed_at_flush'` 出现，而实测 73 条连这个 note 都没有
            （因为它们被**显式** close 成 error，走不到 flush 的兜底）。

        修法：`status != 'ok'` 且 meta 里没有 `reason` 时，**补一条可观测的事实**
        （`no_reason_recorded`）。**不编造**具体原因——原因由调用侧负责提供；
        这里只保证"读的人知道原因缺失"，而不是面对一个空对象猜测。
        """
        if not self._enabled or not self._conv_sid:
            return
        _m = dict(meta or {})
        if str(status or "") != "ok" and not str(_m.get("reason") or "").strip():
            # 兜底：让"没有原因"这件事本身可见，而不是留一个空 meta 让人无从下手。
            _m.setdefault("reason", "no_reason_recorded")
            _m.setdefault("note", "调用侧未提供失败原因（S1 兜底标记）")
        try:
            self._spans.close(self._conv_sid, status, _m)
        except Exception as e:      # noqa: BLE001
            self._fail("end_conversation", e)

    # ── 父级解析（线程局部：当前 turn → conversation 根）──────────────────
    def _current_parent(self) -> int:
        stack = getattr(self._local, "turns", None)
        if stack:
            return stack[-1]
        return self._conv_sid

    @_contextlib.contextmanager
    def turn(self, turn_no: int, meta: dict | None = None):
        """M2：包住 ReAct 一轮（探测 + 工具执行）。with 语义保证挂起/关闭都闭 span。"""
        if not self._enabled:
            yield 0
            return
        sid = 0
        try:
            _m = {"turn_no": int(turn_no or 0)}
            if meta:
                _m.update(meta)
            sid = self._spans.open(KIND_TURN, "turn #%d" % int(turn_no or 0),
                                   parent=self._current_parent(), meta=_m)
        except Exception as e:      # noqa: BLE001
            self._fail("turn.open", e)
        stack = getattr(self._local, "turns", None)
        if stack is None:
            stack = []
            self._local.turns = stack
        if sid:
            stack.append(sid)
        try:
            yield sid
        finally:
            if sid:
                try:
                    stack.pop()
                except Exception:   # noqa: BLE001
                    pass
                try:
                    self._spans.close(sid, "ok")
                except Exception as e:   # noqa: BLE001
                    self._fail("turn.close", e)

    # ── tool span（M3：_exec_tool_call 开 → _log_tool_call 闭）────────────
    def open_tool(self, name: str, capability_id: str = "",
                  t0: float | None = None) -> int:
        """开 tool span。t0=执行起点（time.time()）——span 窗口覆盖真实执行时长。"""
        if not self._enabled:
            return 0
        try:
            sid = self._spans.open(
                KIND_TOOL, str(name or "")[:120],
                parent=self._current_parent(),
                meta={"capability_id": str(capability_id or "")})
            if t0:
                # 用执行起点校正 started_at/t0（收集器以 open 时刻为准）
                self._spans.backdate(sid, t0)
            stack = getattr(self._local, "tools", None)
            if stack is None:
                stack = []
                self._local.tools = stack
            stack.append(sid)
            return sid
        except Exception as e:      # noqa: BLE001
            self._fail("tool.open", e)
            return 0

    def close_tool(self, sid: int = 0, ok: bool = True,
                   latency_ms: int = 0, meta: dict | None = None) -> None:
        """闭 tool span（_log_tool_call 里调；latency 取真实执行时长）。

        sid=0 ⇒ 闭**栈顶** tool span：_log_tool_call 是 _exec_tool_call 全部
        出口的公共尾（8 个调用点），拿不到 sid；而同线程内工具顺序执行，
        LIFO 弹顶即本次工具（嵌套工具调用同样正确——内层先弹）。
        meta：capability_id 等**闭点才知**的字段（开点时 capability_id 还没算）。
        """
        if not self._enabled:
            return
        stack = getattr(self._local, "tools", None)
        if not sid:
            if not stack:
                return
            sid = stack.pop()
        elif stack and sid in stack:
            try:
                stack.remove(sid)
            except Exception:       # noqa: BLE001
                pass
        try:
            _m = {"ok": 1 if ok else 0, "latency_ms": int(latency_ms or 0)}
            if meta:
                _m.update(meta)
            self._spans.close(sid, "ok" if ok else "error", _m)
        except Exception as e:      # noqa: BLE001
            self._fail("tool.close", e)

    # ── rag span（M4：retrieve 入口开 → 唯一 return 前闭）────────────────
    def open_rag(self, t0: float | None = None) -> int:
        if not self._enabled:
            return 0
        try:
            sid = self._spans.open(KIND_RAG, "retrieve",
                                   parent=self._current_parent())
            if t0:
                # 用执行起点校正 started_at/t0（收集器以 open 时刻为准）
                self._spans.backdate(sid, t0)
            return sid
        except Exception as e:      # noqa: BLE001
            self._fail("rag.open", e)
            return 0

    def close_rag(self, sid: int, hit_count: int = 0, top_score: float = 0.0,
                  latency_ms: int = 0) -> None:
        if not self._enabled or not sid:
            return
        try:
            self._spans.close(sid, "ok", {
                "hit_count": int(hit_count or 0),
                "top_score": round(float(top_score or 0.0), 4),
                "latency_ms": int(latency_ms or 0)})
        except Exception as e:      # noqa: BLE001
            self._fail("rag.close", e)

    # ── llm span（M5：LLMClient.chat 开 → _record_usage 闭）──────────────
    def open_llm(self, model: str = "", intent: str = "",
                 t0: float | None = None) -> int:
        if not self._enabled:
            return 0
        try:
            sid = self._spans.open(
                KIND_LLM, str(model or "")[:120], parent=self._current_parent(),
                meta={"intent": str(intent or "")[:60]})
            if t0:
                # 用执行起点校正 started_at/t0（收集器以 open 时刻为准）
                self._spans.backdate(sid, t0)
            stack = getattr(self._local, "llms", None)
            if stack is None:
                stack = []
                self._local.llms = stack
            stack.append(sid)
            return sid
        except Exception as e:      # noqa: BLE001
            self._fail("llm.open", e)
            return 0

    def close_llm(self, *, trace_id: str = "", tokens_in: int = 0,
                  tokens_out: int = 0, latency_ms: int = 0,
                  ok: bool = True) -> None:
        """闭**最顶部**的 llm span（_record_usage 是流式/非流式唯一记账点）。

        为什么盲弹 llms 栈顶就是对的：本栈**只进 llm span**（tool 栈独立），
        同线程内 LLM 调用严格嵌套（外层 chat 的工具执行中再发起的内层 chat
        先开先闭）⇒ LIFO 弹顶即本次调用。异常路径漏闭的 span 不污染本栈
        （它根本没入栈；未入栈却开着的 span 由 flush 按 unclosed 收口）。
        trace_id 传 llm_usage_stats.trace_id 同源值（core.audit.trace_context）
        ⇒ 两条记录可互相 join（span 树 ↔ 用量流水）。
        """
        if not self._enabled:
            return
        stack = getattr(self._local, "llms", None)
        sid = 0
        if stack:
            sid = stack.pop()
        if not sid:
            return
        try:
            self._spans.close(sid, "ok" if ok else "error", {
                "trace_id": str(trace_id or ""),
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
                "latency_ms": int(latency_ms or 0)})
        except Exception as e:      # noqa: BLE001
            self._fail("llm.close", e)

    # ── 落库 ────────────────────────────────────────────────────────────
    def flush(self, conn=None) -> int:
        """流尾一次性落库（调用方 commit）。conn=None 时自开独立连接。

        与编排侧 `_stream_orchestrated_traced` 同款：旁路失败只丢观测数据。
        """
        if not self._enabled:
            return 0
        own = False
        try:
            if conn is None:
                from database import get_db as _gdb
                conn = _gdb()
                own = True
            n = self._spans.flush(conn)
            conn.commit()
            return n
        except Exception as e:      # noqa: BLE001
            self._fail("flush", e)
            return 0
        finally:
            if own and conn is not None:
                try:
                    conn.close()
                except Exception:   # noqa: BLE001
                    pass

    def stats(self) -> dict:
        """轻量自证（门禁/排查用）：启用态 + 已开根 span + 失败数。"""
        return {"enabled": self._enabled,
                "conversation_id": self.conversation_id,
                "conv_sid": self._conv_sid,
                "failures": len(self.failures), "fail_total": self._fail_n}


# ── 跨层访问通道（M3/M4/M5 经此拿 tracer；M1/M2 亦可）────────────────────
# 为什么需要：挂载点深嵌 agent/llm 层，逐个传参要改 8+ 处签名；而 /chat/stream
# 的同步生成器整体跑在同一个 daemon 线程（见文件头说明）。
import contextvars as _cv

_current_tracer = _cv.ContextVar("conv_tracer", default=None)


def current_tracer():
    """取当前线程/上下文的会话 tracer（无 ⇒ None，所有打点退化为 no-op）。"""
    try:
        return _current_tracer.get()
    except Exception:               # noqa: BLE001
        return None


def set_current_tracer(tracer):
    """设置当前 tracer；返回 token 供 reset（execute_stream 入口用）。"""
    return _current_tracer.set(tracer)


def reset_current_tracer(token) -> None:
    try:
        _current_tracer.reset(token)
    except Exception:               # noqa: BLE001
        pass
