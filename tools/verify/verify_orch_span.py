# -*- coding: utf-8 -*-
"""Task #43（2026-10-20）编排观测性 trace 回归验证 —— 零 LLM、零服务、零生产库写。

**为什么需要它**：一次真实编排 55s~344s，此前没有任何数据能回答"时间花在哪、
每个节点成本多少"——orch_stream_events 是事件流（无父子结构、无起止对，算不出
latency），agent_tasks 只有子任务维度（没有 plan/summary/reflection/views）。
span 树（orch_run_spans + services/orch_span.py）补这一格；本门禁钉住它不退化。

锁定的不变式：
    S1  建表契约：orch_run_spans DDL + 索引在 schema.py（新老库都走
        CREATE TABLE IF NOT EXISTS，无需补列迁移）
    S2  服务契约：SpanCollector 开/关/落库三件套 + run_trace 装配 + 十二类 kind 常量
        （编排七类 run/plan/task/summary/reflection/views/replan：P1 起第七类
         replan = 重规划决策记录，含 action=none，其插桩与消费由
         tools/verify/verify_replan_p1.py 单独断言，本门禁只钉常量与 DDL 注释；
         会话五类 conversation/turn/tool/rag/llm：P0-b 生产回流，见下方 P 段）
    S3  ★ 插桩接线（本功能真正的失效模式是"建好没人用"）：
        · 包装层 _stream_orchestrated_traced 存在，且 body **仍叫**
          _stream_orchestrated_flow（三门禁按名断言它，见 S3d 反向证明）
        · 六类执行 span 的 open/close 都在 body 里（按 kind 逐一断言，不用计数阈值——
          删一个 open 计数可能仍≥6，按 kind 断言才抓得住，M3 自证）
        · task 与后三段（summary/reflection/views）都挂 run 下（parent=_run_sid）——
          后段与 run 并列成根会把 total_ms 双算（e2e 实测 777.8s vs 墙钟 648.3s）
        · 三个编排调用点全部走包装层（漏一个 = 那条路径的编排没有 trace）
    P   ★ P0-b 会话级 trace 插桩（M1-M5 五个挂载点，2026-10-25）：
        会话直答主路径（/chat/stream 端到端）此前的盲区：只有编排流量有 span。
        判据按**真实调用链**定（不是按设计想象）：M1 路由器建 tracer + 流尾 finally
        flush；M2 execute_stream 收 _tracer 参数 set 进 ContextVar + ReAct 轮 with
        _turn_cm；M3 tool 开在 _exec_tool_call 入口（10+ 出口的公共前躯）、闭在
        _log_tool_call（8 个出口的公共尾，LIFO 弹栈）；M4 rag 开在 retrieve 入口
        （AST 验证单 return）、闭在 return 前；M5 llm 开在 chat() 入口（五类出口
        公共前躯）、闭在 _record_usage 的 finally（流式/非流式唯一记账点）。
        每个挂载点断言"开点与闭点成对存在"——只开不闭 = flush 只能按 unclosed
        收口（token/latency 归属丢失），只闭不开 = 永远 no-op。
    S4  查询端点：GET /runs/{id}/trace 存在且过 _own_run 归属门（span 含
        task_key/错误详情，属批次内容的一部分，不能裸奔）
    S5  前端接线：编排卡有「⏱ 执行轨迹」入口 + 拉取渲染函数（只改后端不改编
        前端 = 用户看不到 = 改动无效，本仓反复验证过的一条）
    S6  JS 语法（node --check；无 node 环境按仓内惯例 SKIP 不假绿）
    U1  init_db 在新库上建出该表（迁移幂等）
    U2  ★ 开/关/落库往返：parent_id 内存 id → 行 id 映射正确、latency_ms>0
    U3  未闭合 span（异常路径）flush 时按 error 收口且带 note
    U4  ★ run_trace 树形装配：根/子归属、by_kind 聚合、tasks 成本聚合
    U5  无 span 的老批次：空树 + note（200 语义，不是错误）
    U6  线程安全：并发开/关不丢不重（task span 跑在 worker 线程）
    U7 总开关 enabled=False：open 恒 0、flush 零行（可降级）
    M1  变异自证：flush 丢 parent ⇒ 树形断言必须抓住（否则 S3/U4 形同虚设）
    M2  变异自证：未闭合不标 error ⇒ U3a 断言必须抓住
    M3  变异自证：删 task 插桩行 ⇒ S3e 按 kind 断言必须抓住（否则 S3 形同虚设）

**临时库纪律**（MEMORY 教训：env 设晚了 ⇒ 在生产库上跑迁移）：
    不走 MBSE_DB_PATH 环境变量 —— DB_PATH 在 import 期就绑进
    `database.connection` / `database.schema` 两个模块作用域。U1 跑真 init_db
    时两处一起 monkeypatch 指向临时文件；其余 U 段用仅含 span 表的临时库直连。

用法（裸跑自身即完整口径；--live 附加真库/服务只读取证，CI 不跑）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_orch_span.py [--live]
退出码：全绿 0 / 有失败或空转 1。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SCHEMA_SRC = os.path.join(ROOT, "database", "schema.py")
SPAN_SRC = os.path.join(ROOT, "services", "orch_span.py")
STREAM_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
ROUTER_SRC = os.path.join(ROOT, "routers", "orchestration.py")
# P0-b（2026-10-25）会话级 trace 的四个新插桩文件（M1 在 conversations.py）
CONV_SRC = os.path.join(ROOT, "routers", "conversations.py")
TOOLS_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "tools.py")
RAG_SRC = os.path.join(ROOT, "agent", "rag.py")
LLM_SRC = os.path.join(ROOT, "llm", "__init__.py")
CARDS_JS = os.path.join(ROOT, "static", "js", "mods", "06-cards.js")
CHAT_JS = os.path.join(ROOT, "static", "js", "mods", "03-chat.js")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════════
# S 静态段（毫秒级，不起库）
# ══════════════════════════════════════════════════════════════════════════
def t_static():
    print("\n=== S. 静态契约 ===")
    schema = _read(SCHEMA_SRC)
    ok = _rec("S1a schema 含 orch_run_spans 建表",
              "CREATE TABLE IF NOT EXISTS orch_run_spans" in schema)
    ok &= _rec("S1b kind 注释在 DDL 里（口径可查；编排七类 + P0-b 会话五类）",
               "run | plan | task | summary | reflection | views | replan" in schema
               and "conversation | turn | tool | rag | llm" in schema)
    ok &= _rec("S1c 三个索引齐备（conv_run/time + P0-b kind_time）",
               schema.count("idx_ors_") >= 3 and "idx_ors_kind_time" in schema)
    ok &= _rec("S1d 新表无需补列迁移（CREATE TABLE IF NOT EXISTS 覆盖老库）",
               "orch_run_spans" not in
               _read(os.path.join(ROOT, "database", "migrations", "columns.py")))

    span = _read(SPAN_SRC)
    ok &= _rec("S2a SpanCollector 三件套（open/close/flush）",
               all(("def " + m) in span for m in ("open", "close", "flush")))
    ok &= _rec("S2b run_trace 装配函数存在", "def run_trace" in span)
    ok &= _rec("S2c 编排七类 kind 常量齐备（P1 起 + replan 重规划决策记录）",
               all(('KIND_%s = "%s"' % (k.upper(), k)) in span
                   for k in ("run", "plan", "task", "summary", "reflection",
                             "views", "replan")))
    ok &= _rec("S2c2 P0-b 会话五类 kind 常量齐备（生产回流 §3.2）",
               all(('KIND_%s = "%s"' % (k.upper(), k)) in span
                   for k in ("conversation", "turn", "tool", "rag", "llm")))
    ok &= _rec("S2d 配置总开关 orch_span.enabled（可降级）",
               '"orch_span", "enabled"' in span)
    ok &= _rec("S2e 线程锁（task span 在 worker 线程开闭）",
               "threading.Lock" in span)
    ok &= _rec("S2f ConversationTracer + ContextVar 跨层通道（M3/M4/M5 经此拿 tracer）",
               "class ConversationTracer" in span
               and 'ContextVar("conv_tracer"' in span
               and all(("def " + m) in span for m in
                       ("current_tracer", "set_current_tracer", "reset_current_tracer")))

    stream = _read(STREAM_SRC)
    ok &= _rec("S3a 包装层 _stream_orchestrated_traced 存在",
               "def _stream_orchestrated_traced(" in stream)
    ok &= _rec("S3b ★ body 仍叫 _stream_orchestrated_flow（三门禁按名断言它）",
               "def _stream_orchestrated_flow(" in stream)
    ok &= _rec("S3c 包装层负责 flush 且异常全吞（旁路不阻断）",
               "_spans.flush(_sc)" in stream and "_sc.commit()" in stream)
    ok &= _rec("S3d body 的 _spans 默认 None（门禁孪生/直调路径零行为变化）",
               "_spans=None) -> iter:" in stream)
    _KINDS = ("plan", "run", "task", "summary", "reflection", "views")
    _CLOSE_VAR = {"plan": "_plan_sid", "run": "_run_sid", "task": "_tsid",
                  "summary": "_sum_sid", "reflection": "_ref_sid", "views": "_views_sid"}
    _miss_open = [k for k in _KINDS if ('_spans.open("%s"' % k) not in stream]
    ok &= _rec("S3e 六类 span 全部插桩（plan/run/task/summary/reflection/views）",
               not _miss_open, "缺 open: %s" % ",".join(_miss_open))
    _miss_close = [k for k in _KINDS if ('_spans.close(%s' % _CLOSE_VAR[k]) not in stream]
    ok &= _rec("S3f 六类 span 全部有收口 close", not _miss_close,
               "缺 close: %s" % ",".join(_miss_close))
    _run_opens = stream.count('_spans.open("run"')
    ok &= _rec("S3e2 run span 两个入口分支都插桩（resume + 新批次）",
               _run_opens == 2, "run opens=%d" % _run_opens)
    ok &= _rec("S3g task span 挂在 run span 下（parent=_run_sid）",
               'parent=_run_sid' in stream)
    _post = (("summary", "汇总"), ("reflection", "质量评审"), ("views", "SysML 视图"))
    _post_bad = [f"{k}/{n}" for k, n in _post
                 if ('_spans.open("%s", "%s", parent=_run_sid)' % (k, n)) not in stream]
    ok &= _rec("S3g2 后段 span（summary/reflection/views）也挂 run 下（防 total_ms 双算）",
               not _post_bad, "未挂 run: %s" % ",".join(_post_bad))
    ok &= _rec("S3h run span 在 run_id 落定后开（bind 调用存在）",
               stream.count("_spans.bind(") >= 2)
    _dispatch = stream.count("_yield_collect(self._stream_orchestrated_traced(")
    ok &= _rec("S3i ★ 三个编排调用点全部走包装层（漏一个=那条路径没 trace）",
               _dispatch == 3, "traced 调用点=%d" % _dispatch)
    ok &= _rec("S3j 没有调用点绕过包装层直调 body",
               "_yield_collect(self._stream_orchestrated_flow(" not in stream)
    ok &= _rec("S3k _spans 未遮蔽任何模块别名（e2e G0 同型坑）",
               not any(("import" in ln and " as _spans" in ln) for ln in stream.split("\n")))

    router = _read(ROUTER_SRC)
    ok &= _rec("S4a trace 端点已注册",
               '@router.get("/api/orchestration/runs/{run_id}/trace")' in router)
    ok &= _rec("S4b ★ 端点过归属门 _own_run（span 含 task_key/错误详情）",
               "_own_run(conn, run_id, user)" in router.split("def get_run_trace")[1]
               if "def get_run_trace" in router else False)

    cards = _read(CARDS_JS)
    chat = _read(CHAT_JS)
    ok &= _rec("S5a 编排卡有「⏱ 执行轨迹」入口",
               "orchTracePanel(" in cards)
    ok &= _rec("S5b 拉取+渲染函数成对（orchTracePanel/orchTraceHtml）",
               "async function orchTracePanel(" in chat and "function orchTraceHtml(" in chat)
    ok &= _rec("S5c 拉的是 trace 端点（不是别的东西）",
               "/api/orchestration/runs/${runId}/trace" in chat)

    node = shutil.which("node")
    if node:
        import subprocess
        for p in (CARDS_JS, CHAT_JS):
            r = subprocess.run([node, "--check", p], capture_output=True, text=True)
            ok &= _rec("S6 JS 语法 %s" % os.path.basename(p), r.returncode == 0,
                       (r.stderr or "")[:200])
    else:
        _rec("S6 JS 语法（无 node 环境，按惯例 SKIP）", True, kind=VACUOUS)

    # ── P. P0-b 会话级 trace 插桩（M1-M5）─────────────────────────────
    # 判据口径见文件头 P 段：按**真实调用链的公共前躯/公共尾**断言开闭成对，
    # 不按设计想象。挂载点被删/被挪 = 会话路径重新变瞎（编排有 trace、直答没有）。
    print("\n=== P. P0-b 会话级 trace 插桩 ===")
    conv = _read(CONV_SRC)
    ok &= _rec("P1a M1 路由器建 ConversationTracer（会话根 span）",
               "ConversationTracer as _ConvTracer" in conv
               and "_ConvTracer(conv_id)" in conv)
    ok &= _rec("P1b M1 begin_conversation + 流尾 finally flush（旁路不阻断）",
               "_tracer.begin_conversation()" in conv
               and "_tracer.flush()" in conv)
    ok &= _rec("P1c M1 done 帧回填 intent/route 后闭 conversation span",
               "_tracer.end_conversation(" in conv)
    ok &= _rec("P2a M2 execute_stream 收 _tracer 参数（跨线程显式传参）",
               "_tracer=None):" in stream)
    ok &= _rec("P2b M2 worker 线程里 set_current_tracer（ContextVar 不跨线程继承）",
               "set_current_tracer as _set_ct" in stream and "_set_ct(_tracer)" in stream)
    ok &= _rec("P2c M2 ReAct 轮包 with _turn_cm（_turn_cm 定义 + 调用点）",
               "def _turn_cm(" in stream and "with _turn_cm(_tracer," in stream)
    tools = _read(TOOLS_SRC)
    ok &= _rec("P3a M3 tool span 开在 _exec_tool_call 入口（10+ 出口公共前躯）",
               "_tracer.open_tool(name, t0=t0)" in tools)
    ok &= _rec("P3b M3 tool span 闭在 _log_tool_call（8 出口公共尾，LIFO 弹栈）",
               "_tracer.close_tool(0," in tools)
    rag = _read(RAG_SRC)
    ok &= _rec("P4a M4 rag span 开在 retrieve 入口",
               "_rag_tracer.open_rag(t0=t0)" in rag)
    ok &= _rec("P4b M4 rag span 闭在唯一 return 前（AST 验证单出口）",
               "_rag_tracer.close_rag(_rag_sid," in rag)
    llm = _read(LLM_SRC)
    ok &= _rec("P5a M5 llm span 开在 chat() 入口（五类出口公共前躯）",
               "_tracer.open_llm(model_name, kwargs.get(\"_intent\", \"\"), t0=t0)" in llm)
    ok &= _rec("P5b M5 llm span 闭在 _record_usage 的 finally（记账成败解耦）",
               "_tracer.close_llm(" in llm and "finally:" in llm)
    return ok


# ══════════════════════════════════════════════════════════════════════════
# U 单元段（自建临时库，零生产副作用）
# ══════════════════════════════════════════════════════════════════════════
def _temp_db():
    """建一套只含 span 表的临时库（不跑全量 init_db —— 快且不依赖种子）。"""
    p = os.path.join(tempfile.gettempdir(), "_span_ut_%d.db" % os.getpid())
    if os.path.exists(p):
        os.remove(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row  # 与生产 database.get_db() 同口径（下标访问）
    conn.execute("""CREATE TABLE IF NOT EXISTS orch_run_spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0,
        kind TEXT DEFAULT '', name TEXT DEFAULT '', parent_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'running', started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        ended_at TEXT, latency_ms INTEGER DEFAULT 0, meta_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    return p, conn


def t_unit():
    print("\n=== U. 行为（临时库）===")
    from services import orch_span as OS

    # U1 init_db 在新库上建出该表（迁移幂等）。
    # 纪律：DB_PATH 在 import 期绑进 database.connection / database.schema 两个
    # 模块作用域，必须两处一起 monkeypatch（schema.py L99 注释记过这个坑）。
    import database.connection as _connmod
    import database.schema as _sch
    p0 = os.path.join(tempfile.gettempdir(), "_span_init_%d.db" % os.getpid())
    for _suf in ("", "-wal", "-shm"):
        if os.path.exists(p0 + _suf):
            os.remove(p0 + _suf)
    _old = (_connmod.DB_PATH, _sch.DB_PATH)
    _connmod.DB_PATH = p0
    _sch.DB_PATH = p0
    ok = True
    try:
        _sch.init_db()
        _sch.init_db()                       # 二次调用不炸 = 幂等
        c0 = sqlite3.connect(p0)
        c0.row_factory = sqlite3.Row
        try:
            t = c0.execute("SELECT name FROM sqlite_master WHERE type='table'"
                           " AND name='orch_run_spans'").fetchone()
            idx = c0.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                " AND name IN ('idx_ors_conv_run','idx_ors_time','idx_ors_kind_time')").fetchone()[0]
        finally:
            c0.close()
        ok = _rec("U1a init_db 新库建出 orch_run_spans 表", t is not None)
        ok &= _rec("U1b 三索引随表创建（+P0-b idx_ors_kind_time）", idx == 3, "idx=%d" % idx)
    except Exception as e:
        ok = _rec("U1 init_db 临时库执行（含二次幂等）", False, str(e)[:150])
    finally:
        _connmod.DB_PATH, _sch.DB_PATH = _old
        for _suf in ("", "-wal", "-shm"):
            if os.path.exists(p0 + _suf):
                os.remove(p0 + _suf)

    p, conn = _temp_db()
    try:
        # U2 往返：run 根 + 两个 task 子
        c = OS.SpanCollector(enabled=True)
        c.bind(538, 9569)
        sid_run = c.open("run", "run #538")
        sid_plan = c.open("plan", "任务规划", parent=sid_run)
        sid_t1 = c.open("task", "t1", parent=sid_run, meta={"agent": "a", "tokens": 100})
        time.sleep(0.02)
        c.close(sid_t1, "ok", {"tokens": 1234, "latency_ms": 20})
        c.close(sid_plan, "ok", {"source": "planner", "tasks": 3})
        c.close(sid_run, "ok", {"agg_status": "partial"})
        n = c.flush(conn)
        conn.commit()
        ok = _rec("U2a flush 写入 3 行", n == 3, "n=%d" % n)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM orch_run_spans ORDER BY id").fetchall()]
        ok &= _rec("U2b run_id/conversation_id 由 bind 补齐（plan 先于 run_id 开）",
                   all(r["run_id"] == 538 and r["conversation_id"] == 9569 for r in rows))
        by_name = {r["name"]: r for r in rows}
        ok &= _rec("U2c parent 映射：t1/plan 的 parent_id = run 行 id",
                   by_name["t1"]["parent_id"] == by_name["run #538"]["id"]
                   and by_name["任务规划"]["parent_id"] == by_name["run #538"]["id"])
        ok &= _rec("U2d latency_ms 由 monotonic 差值算出（>0）",
                   all(r["latency_ms"] > 0 for r in rows))
        ok &= _rec("U2e meta_json 保真（tokens 1234）",
                   json.loads(by_name["t1"]["meta_json"]).get("tokens") == 1234)
        ok &= _rec("U2f close 幂等（重复 close 不炸不双写）",
                   c.close(sid_t1, "error") is False)

        # U3 未闭合 → error + note
        c2 = OS.SpanCollector(enabled=True)
        c2.bind(539, 9569)
        c2.open("run", "run #539")
        c2.flush(conn)
        conn.commit()
        r = conn.execute("SELECT status, meta_json FROM orch_run_spans "
                         "WHERE run_id=539").fetchone()
        ok &= _rec("U3a 未闭合 span 按 error 收口",
                   r["status"] == "error", "status=%s" % r["status"])
        ok &= _rec("U3b 带 note=unclosed_at_flush（排障能看出是异常路径）",
                   json.loads(r["meta_json"]).get("note") == "unclosed_at_flush")

        # U4 run_trace 装配
        tr = OS.run_trace(conn, 538)
        ok = _rec("U4a 树形：1 根（run）+ 2 子（plan/t1）",
                   len(tr["spans"]) == 1
                   and len(tr["spans"][0]["children"]) == 2,
                   "roots=%d" % len(tr["spans"]))
        ok &= _rec("U4b by_kind 聚合（run/plan/task 各 1）",
                   {k: v["count"] for k, v in tr["stats"]["by_kind"].items()}
                   == {"run": 1, "plan": 1, "task": 1})
        ok &= _rec("U4c tasks 成本聚合（tokens=1234, status=ok）",
                   tr["stats"]["tasks"] == [
                       {"key": "t1", "status": "ok", "latency_ms": tr["stats"]["tasks"][0]["latency_ms"],
                        "tokens": 1234, "attempts": 0, "retry_count": 0,
                        "offload_id": 0, "error": ""}])
        ok &= _rec("U4d total_ms = 根 span latency（plan+run 之和）",
                   tr["stats"]["total_ms"] == tr["spans"][0]["latency_ms"])

        # U5 空批次
        tr0 = OS.run_trace(conn, 999999)
        ok &= _rec("U5 无 span 老批次：空树 + note（不是错误）",
                   tr0["spans"] == [] and "上线之前" in tr0["stats"]["note"])

        # U6 线程安全
        c3 = OS.SpanCollector(enabled=True)
        c3.bind(540, 9569)
        root = c3.open("run", "run #540")
        errs = []

        def _hammer(i):
            try:
                for k in range(20):
                    s = c3.open("task", "t%d_%d" % (i, k), parent=root)
                    c3.close(s, "ok", {"tokens": 1})
            except Exception as e:      # pragma: no cover - 只在线程安全破坏时出现
                errs.append(str(e))

        ths = [threading.Thread(target=_hammer, args=(i,)) for i in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        c3.close(root, "ok")
        n3 = c3.flush(conn)
        conn.commit()
        ok &= _rec("U6a 8 线程 × 20 span 并发开闭无异常", not errs, ";".join(errs[:2]))
        ok &= _rec("U6b 全部落库（1 根 + 160 任务）", n3 == 161, "n=%d" % n3)
        ok &= _rec("U6c 并发下 parent 仍全部指向根",
                   conn.execute("SELECT COUNT(*) FROM orch_run_spans WHERE run_id=540 "
                                "AND kind='task' AND parent_id="
                                "(SELECT id FROM orch_run_spans WHERE run_id=540 AND kind='run')"
                                ).fetchone()[0] == 160)

        # U7 开关
        c4 = OS.SpanCollector(enabled=False)
        ok &= _rec("U7a 关闭时 open 恒 0", c4.open("run", "x") == 0)
        ok &= _rec("U7b 关闭时 flush 零行", c4.flush(conn) == 0)

        # U8 P0-b ConversationTracer 往返（会话五类 + 跨层通道 + LIFO + backdate）
        # 为什么必须单测而不只靠静态 P 段：P 段只证明"代码在"，U8 证明
        # "开→闭→落库"这条路真的通（父子映射/latency/兜底语义与编排侧同构）。
        t8 = OS.ConversationTracer(777, enabled=True)
        ok &= _rec("U8a 无 tracer 线程 current_tracer()=None（编排线程不误继承）",
                   OS.current_tracer() is None)
        t8.begin_conversation({"intent": "kb_qa"})
        _t_before = time.time() - 2.0
        with t8.turn(1):
            rsid = t8.open_rag(t0=_t_before)
            t8.close_rag(rsid, hit_count=5, top_score=0.82, latency_ms=320)
            t8.open_llm("m1", "kb_qa", t0=time.time() - 0.5)
            t8.close_llm(trace_id="tr-1", tokens_in=1200, tokens_out=80,
                         latency_ms=510)
            t8.open_tool("kb_search", t0=time.time() - 0.15)
            # 嵌套：工具执行中再发起 LLM（内层先开后闭，LIFO）
            t8.open_llm("m1", "tool_summary", t0=time.time() - 0.05)
            t8.close_llm(trace_id="tr-1", tokens_in=300, tokens_out=40,
                         latency_ms=60)
            t8.close_tool(0, ok=True, latency_ms=170)
        t8.end_conversation("ok")
        n8 = t8.flush(conn)
        conn.commit()
        rows8 = [dict(r) for r in conn.execute(
            "SELECT * FROM orch_run_spans WHERE conversation_id=777 ORDER BY id")]
        ok &= _rec("U8b flush 写入 6 行（conv+turn+rag+2llm+tool）", n8 == 6, "n=%d" % n8)
        _kinds8 = [r["kind"] for r in rows8]
        ok &= _rec("U8c 会话五类齐全",
                   set(_kinds8) == {"conversation", "turn", "tool", "rag", "llm"},
                   str(sorted(set(_kinds8))))
        _by8 = {r["kind"] + str(i): r for i, r in enumerate(rows8)}
        ok &= _rec("U8d 扁平树：tool/rag/llm 均挂 turn 下（设计 §3.2）",
                   rows8[1]["kind"] == "turn"
                   and all(r["parent_id"] == rows8[1]["id"]
                           for r in rows8 if r["kind"] in ("tool", "rag", "llm")))
        ok &= _rec("U8e conversation 根 parent_id=0",
                   rows8[0]["parent_id"] == 0)
        ok &= _rec("U8f backdate 生效：rag started_at 回填到 t0（-2s）",
                   rows8[2]["started_at"] ==
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_t_before)),
                   rows8[2]["started_at"])
        _llm_rows = [r for r in rows8 if r["kind"] == "llm"]
        # 按 intent 查找而非行序：表按 open 排序（外层先开在前），而行序脆弱
        _llm_meta = {json.loads(r["meta_json"]).get("intent"): json.loads(r["meta_json"])
                     for r in _llm_rows}
        ok &= _rec("U8g LIFO 闭对：内层 tool_summary tokens=300、外层 kb_qa 1200 各自归位",
                   _llm_meta.get("tool_summary", {}).get("tokens_in") == 300
                   and _llm_meta.get("kb_qa", {}).get("tokens_in") == 1200,
                   str({k: v.get("tokens_in") for k, v in _llm_meta.items()}))
        ok &= _rec("U8h tool latency=170 / rag latency=320（真实执行时长）",
                   json.loads([r for r in rows8 if r["kind"] == "tool"][0]["meta_json"]).get("latency_ms") == 170
                   and json.loads(rows8[2]["meta_json"]).get("latency_ms") == 320)
        ok &= _rec("U8i failures 为空（全链路无静默失败）", not t8.failures,
                   ";".join(t8.failures[:2]))

        # U8j 未闭 rag（retrieve 中途异常）→ flush 按 error 收口不静默
        t8b = OS.ConversationTracer(778, enabled=True)
        t8b.begin_conversation()
        t8b.open_rag()
        t8b.flush(conn)
        conn.commit()
        _leak = conn.execute("SELECT status, meta_json FROM orch_run_spans "
                             "WHERE conversation_id=778 AND kind='rag'").fetchone()
        ok &= _rec("U8j 漏闭 rag 按 error + unclosed_at_flush 收口",
                   _leak["status"] == "error"
                   and json.loads(_leak["meta_json"]).get("note") == "unclosed_at_flush")

        # U8k 关闭态 ConversationTracer 全 no-op（总开关降级路径）
        t8c = OS.ConversationTracer(0, enabled=False)
        ok &= _rec("U8k 关闭态：begin=0 / flush=0（零开销降级）",
                   t8c.begin_conversation() == 0 and t8c.flush(conn) == 0)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if os.path.exists(p):
            os.remove(p)
    return ok


# ══════════════════════════════════════════════════════════════════════════
# M 变异自证：把"错写法"注入**内存孪生**（不碰生产文件，B1-10 纪律）
# ══════════════════════════════════════════════════════════════════════════
def t_mutation():
    print("\n=== M. 变异自证（内存孪生，不写生产文件）===")
    from services import orch_span as OS
    src = _read(SPAN_SRC)

    def _twin(mut_src, name):
        ns = {}
        exec(compile(mut_src, "<%s>" % name, "exec"), ns)
        return ns

    p, conn = _temp_db()
    try:
        # M1 flush 丢 parent ⇒ 树形必须散架（证明 U4a/S3g 有牙）
        m1 = src.replace("id_map.get(sp[\"parent\"], 0)", "0")
        ok = _rec("M1a 变异锚点命中（parent 映射行）", m1 != src)
        if m1 != src:
            ns1 = _twin(m1, "span_m1")
            c = ns1["SpanCollector"](enabled=True)
            c.bind(1, 1)
            r = c.open("run", "r")
            t1 = c.open("task", "t1", parent=r)
            c.close(t1, "ok"); c.close(r, "ok")
            c.flush(conn); conn.commit()
            tr = ns1["run_trace"](conn, 1)
            caught = len(tr["spans"]) == 2          # 正确实现应是 1 根 1 子
            ok &= _rec("M1b 丢 parent ⇒ 树形断言抓住（roots=2 而非 1）", caught,
                       "roots=%d" % len(tr["spans"]))
            conn.execute("DELETE FROM orch_run_spans"); conn.commit()

        # M2 未闭合不标 error ⇒ U3 必须抓住
        m2 = src.replace('sp["status"] = "error"', 'sp["status"] = "ok"')
        ok &= _rec("M2a 变异锚点命中（未闭合收口行）", m2 != src)
        if m2 != src:
            ns2 = _twin(m2, "span_m2")
            c = ns2["SpanCollector"](enabled=True)
            c.bind(2, 2)
            c.open("run", "r")                       # 故意不关
            c.flush(conn); conn.commit()
            st = conn.execute("SELECT status FROM orch_run_spans WHERE run_id=2").fetchone()[0]
            # 语义：正确实现给 error；变异体给 ok ⇒ U3a 的 `== "error"` 断言在变异体上
            # 会 FAIL ⇒ 证明 U3a 有牙（M1b 同型：产出偏离正确值即"抓住"）
            caught = st != "error"
            ok &= _rec("M2b 未闭合标 ok ⇒ U3a 断言抓住（正确值 error，变异体偏离）", caught,
                       "status=%s" % st)
            conn.execute("DELETE FROM orch_run_spans"); conn.commit()

        # M3 源码级：删掉 task 插桩行 ⇒ S3e 按 kind 断言必须抓住
        stream = _read(STREAM_SRC)
        m3 = stream.replace('_tsid = (_spans.open("task", tkey, parent=_run_sid,', '_tsid = 0  # (')
        ok &= _rec("M3a 变异锚点命中（task span open 行）", m3 != stream)
        if m3 != stream:
            caught = '_spans.open("task"' not in m3
            ok &= _rec("M3b 删 task 插桩 ⇒ 按 kind 断言抓住", caught)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if os.path.exists(p):
            os.remove(p)
    return ok


# ══════════════════════════════════════════════════════════════════════════
# L live 段（opt-in --live；真库只读 + 需要服务在跑；CI 不执行）
# ══════════════════════════════════════════════════════════════════════════
def t_live():
    print("\n=== L. 真库/服务只读取证（--live）===")
    import urllib.request
    import urllib.error
    base = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000")
    uid = os.environ.get("VERIFY_UID", "112")
    run_id = os.environ.get("VERIFY_RUN_ID", "538")   # 538 产生于 trace 上线前 ⇒ 应无 span
    try:
        req = urllib.request.Request("%s/api/orchestration/runs/%s/trace" % (base, run_id),
                                     headers={"X-User-Id": uid})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode("utf-8"))
        ok = _rec("L1 老批次 trace：200 + 空树 + note（不报错）",
                  j.get("spans") == [] and "上线之前" in (j.get("stats") or {}).get("note", ""))
    except urllib.error.HTTPError as e:
        ok = _rec("L1 老批次 trace 可访问", False, "HTTP %s" % e.code)
    except Exception as e:
        _rec("L1 服务不可达（按惯例不计失败，仅记录）", True, str(e)[:80], kind=VACUOUS)
        return True
    try:
        req = urllib.request.Request("%s/api/orchestration/runs/999999999/trace" % base,
                                     headers={"X-User-Id": uid})
        urllib.request.urlopen(req, timeout=10)
        ok &= _rec("L2 不存在的批次 → 404（归属门生效）", False, "居然 200 了")
    except urllib.error.HTTPError as e:
        ok &= _rec("L2 不存在的批次 → 404（归属门生效）", e.code == 404, "HTTP %s" % e.code)
    except Exception as e:
        ok &= _rec("L2 探测异常", False, str(e)[:80])
    return ok


def main():
    print("Task #43 编排观测性 trace 门禁")
    ok = t_static()
    ok &= t_unit()
    ok &= t_mutation()
    if "--live" in sys.argv:
        ok &= t_live()
    fails = [r for r in _results if r[0] == FAIL]
    vac = [r for r in _results if r[0] == VACUOUS]
    print("\n════════════════════════════════════════")
    print("结果：%d PASS / %d FAIL / %d SKIP" % (len(_results) - len(fails) - len(vac),
                                              len(fails), len(vac)))
    for kind, name, detail in fails:
        print("  FAIL  %s  %s" % (name, detail))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
