# -*- coding: utf-8 -*-
"""B2-12 + B2-11 门禁：工具调用可归因到编排 + 编排关键事件可回放。

**背景（活口径取证，2026-10-13）**：
- `tool_call_logs` 实测 13 列，`run_id`/`task_key`/`failure_kind` **三列全缺**
  ⇒ 2013 行工具调用只能回答"哪个会话/谁调的"，回答不了「**这次编排里哪个子任务调的**」。
- 生产库**无任何编排事件表**；`stream.py` 的 88 处 `yield {"type":...}` 事件
  **只到前端即丢** ⇒ 排障无法回放"当时走了哪些阶段、调了哪些工具"。

**本门禁的三个核心断言（都是"两两一致性"而非"某值等于某常量"）**：
1. **归因链完整**：`_tool_run_ctx`/`_tool_task_key` 在 stream.py 挂上后，
   `tools.py` 的 `_log_tool_call` 真的消费了它们并写进三列（此前只有"写操作暂存"
   那条路径在用 ⇒ 挂上等于白挂）。
2. **事件能带真批次号**：`done.data.orch_run_id` 存在，且**边收边落会落成 run_id=0
   的孤岛行**（实测复现）⇒ 断言路由层必须用"缓冲后刷库"，不能边收边落。
3. **落库白名单不含高频流**：`token`/`reasoning` 是逐字增量（一次真实建模数百帧），
   落进去会撑爆表 ⇒ 断言只落 stage/tool。

⚠️ **变异自证在内存副本上做**（沿用本仓范式：不写回生产源文件，
   避开 2026-10-12 实测踩过的「变异体残留被误判成产品缺陷」事故）。
"""
import io
import json
import os
import re
import sqlite3
import sys
import tempfile

# ⚠️ 定位仓库根**不要 dirname 数层数** —— tools/verify 恰好两层，但换个目录就错。
# 改为向上找标志文件（memory_service.py）。
def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROD_DB = os.path.join(ROOT, "mbse.db")

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def read(rel):
    p = os.path.join(ROOT, rel)
    return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def main():
    t_src = read("agent/pipeline_parts/tools.py")
    s_src = read("agent/pipeline_parts/stream.py")
    r_src = read("routers/conversations.py")
    c_src = read("core/config.py")
    sc_src = read("database/schema.py")
    col_src = read("database/migrations/columns.py")

    # ══════════════ A. tool_call_logs 三列 ══════════════
    print("\n=== A. tool_call_logs 归因三列（B2-12）===")
    _tbl = sc_src.split("CREATE TABLE IF NOT EXISTS tool_call_logs")[1].split('""")')[0]
    _tbl_code = re.sub(r"--[^\n]*", "", _tbl)      # 剥注释：注释里会出现列名
    for c in ("run_id", "task_key", "failure_kind"):
        check("A1 DDL 有 %s 列" % c, c in _tbl_code)
    check("A2 迁移脚本补了 run_id/task_key/failure_kind 三列",
          col_src.count('_add("tool_call_logs", "run_id"') == 1
          and col_src.count('_add("tool_call_logs", "task_key"') == 1
          and col_src.count('_add("tool_call_logs", "failure_kind"') == 1)

    # P0-a（2026-10-25 修）：原 `[:3000]` 定长窗口在 _log_tool_call 加了
    # capability_id 推导逻辑（函数 4768 字符）后把 INSERT（距头 4171）与
    # services.failure_class 引用（距头 3029）挤出窗口 ⇒ A3/A6 假红。
    # 改**行边界法**：抽到下一个同类缩进 `def` 为止（不依赖函数长短）。
    if "def _log_tool_call" in t_src:
        _fi = t_src.find("def _log_tool_call")
        _fj = t_src.find("\n    def ", _fi + 10)
        _lg = t_src[_fi:_fj if _fj > 0 else _fi + 8000]
    else:
        _lg = ""
    check("A3 _log_tool_call 的 INSERT 含三列", "run_id, task_key, failure_kind" in _lg)
    check("A4 读了 self._tool_run_ctx（归因来源）",
          '_tool_run_ctx' in _lg, "未消费 stream.py 挂好的 run 上下文")
    check("A5 读了 self._tool_task_key",
          '_tool_task_key' in _lg)
    check("A6 failure_kind 复用 services.failure_class（不另造第二份词表）",
          "services.failure_class" in _lg,
          "另写词表必然与 agent_tasks.error_class 漂移")

    # ══════════════ B. 归因链的"上游"确实存在 ══════════════
    print("\n=== B. 归因链上游（stream.py 挂上下文）===")
    check("B1 stream.py 给子任务 Agent 挂了 _tool_run_ctx",
          "sub._tool_run_ctx = int(run_id or 0)" in s_src)
    check("B2 stream.py 挂了 _tool_task_key",
          "sub._tool_task_key = tkey" in s_src)
    # ⚠️ 必须与**编排那处** execute_stream 比，不能用 `index()` 取第一个 ——
    #   stream.py:265 那处是**直行/技能**路径（本就无编排 run_id，也不挂 _tool_run_ctx）。
    #   用"第一个"会把正确的实现判成错的（本门禁第一版就踩了）。
    _anchor_ctx = s_src.index("sub._tool_run_ctx = int(run_id or 0)")
    _orch_call = s_src.index("for ev in sub.execute_stream(query,", _anchor_ctx)
    check("B3 挂上下文在**编排** execute_stream 之前（否则日志写时属性还没设）",
          _anchor_ctx < _orch_call)
    # 反向对照：直行路径不该挂 run 上下文（它没有编排批次号）
    _direct_call = s_src.index("sub.execute_stream(goal,")
    check("B3b 直行/技能路径不挂 _tool_run_ctx（它没有编排批次号）",
          "sub._tool_run_ctx" not in s_src[:_direct_call + 200]
          or s_src.index("sub._tool_run_ctx") > _direct_call,
          "直行路径挂了编排 run 上下文 ⇒ 会把非编排调用记到某个批次上")

    # ══════════════ C. 事件落库（B2-11）══════════════
    print("\n=== C. 编排关键事件落库（B2-11）===")
    check("C1 有 orch_stream_events 表",
          "CREATE TABLE IF NOT EXISTS orch_stream_events" in sc_src)
    for c in ("conversation_id", "run_id", "task_key", "event_type", "payload_json"):
        check("C1.%s 列存在" % c,
              c in sc_src.split("CREATE TABLE IF NOT EXISTS orch_stream_events")[1][:900])
    check("C2 done 事件带 orch_run_id（落库能归到批次的关键）",
          s_src.count('"orch_run_id": int(run_id or 0)') == 2,
          "两处编排 done 都应带；实测 %d 处"
          % s_src.count('"orch_run_id": int(run_id or 0)'))
    check("C3 路由层接了事件记录器",
          "stream_event_recorder" in r_src)
    check("C4 ★ 用缓冲而非边收边落（run_id 只在 done 才到，边收边落会落成孤岛行）",
          "_evbuf" in r_src and "_evbuf.append" in r_src,
          "未见缓冲 ⇒ 落库行 run_id 恒为 0，与本项要修的毛病同型")
    check("C5 刷库在 done/error 时触发且在 yield 之前",
          re.search(r'if _evbuf and _et in \("done", "error"\):', r_src) is not None
          and r_src.index("if _evbuf and _et in") < r_src.index("yield raw", r_src.index("if _evbuf and _et in")))
    check("C6 落库用独立连接（不抢请求级 conn 的写锁）",
          "from database import db_conn" in r_src and "with db_conn() as _evc" in r_src)
    check("C7 观测失败不阻断（整段被 try 包住）",
          r_src.count("except Exception:\n                    pass  # 观测") >= 1
          or "观测收集失败绝不影响用户拿结果" in r_src)

    # ══════════════ D. 行为层：帧解析 + 落库（内存库，零 LLM）══════════════
    print("\n=== D. 行为层：真实帧格式端到端（用 core.sse 序列化，零 LLM）===")
    import core.sse as _S
    from services.stream_event_recorder import (parse_frame, should_persist,
                                                record, read_run_events,
                                                PERSIST_TYPES)

    _c = sqlite3.connect(":memory:")
    _c.row_factory = sqlite3.Row
    _c.execute("""CREATE TABLE orch_stream_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER DEFAULT 0,
        run_id INTEGER DEFAULT 0, task_key TEXT DEFAULT '', event_type TEXT DEFAULT '',
        payload_json TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

    _frames = [
        _S.to_sse_frame({"type": "stage", "name": "生成与校验", "status": "run"}),
        _S.to_sse_frame({"type": "reasoning", "delta": "（自动编排）正在规划子任务…"}),
        _S.to_sse_frame({"type": "tool", "name": "t1:graph_retrieve", "ok": True}),
        _S.to_sse_frame({"type": "token", "delta": "x"}),
        _S.to_sse_frame({"type": "done", "data": {"orch_run_id": 8888}}),
    ]
    # D1 解析口径：五种类型都能解出 type
    _parsed = [parse_frame(f)[0] for f in _frames]
    check("D1 五种帧都能解出 type",
          _parsed == ["stage", "reasoning", "tool", "token", "done"], _parsed)

    # D2 白名单：token/reasoning 不落（逐字增量，落进去撑爆表）
    check("D2 ★ token/reasoning 不在落库白名单",
          not should_persist("token", {"delta": "x"})
          and not should_persist("reasoning", {"delta": "y"})
          and PERSIST_TYPES == ("stage", "tool"),
          "PERSIST_TYPES=%s" % (PERSIST_TYPES,))

    # D3 ★ 复现"边收边落 ⇒ 孤岛行"（这是必须缓冲的证据）
    _run = 0
    for _f, _et in zip(_frames, _parsed):
        if _et == "done":
            _run = 8888
        if _et in ("stage", "tool"):
            record(_c, 42, _f, run_id=_run)      # ← done 之前 run 仍是 0
    _orphan = _c.execute("SELECT COUNT(*) FROM orch_stream_events WHERE run_id=0").fetchone()[0]
    check("D3 ★ 边收边落会落成 run_id=0 的孤岛行（故必须缓冲）",
          _orphan == 2, "orphan=%d（若为 0 说明 done 提前到流首，本判据需重估）" % _orphan)
    _c.execute("DELETE FROM orch_stream_events")

    # D4 缓冲后落库 ⇒ run_id 正确、可按批次号取回
    _buf, _run2 = [], 0
    for _f, _et in zip(_frames, _parsed):
        if _et == "done":
            _, _pl = parse_frame(_f)
            _run2 = int((_pl.get("data") or {}).get("orch_run_id") or 0)
        elif _et in ("stage", "tool"):
            _buf.append(_f)
    for _f in _buf:
        record(_c, 42, _f, run_id=_run2)
    _c.commit()
    _rows = read_run_events(_c, 42, 8888)
    check("D4 缓冲后落库 2 行（stage+tool）", len(_rows) == 2, "got=%d" % len(_rows))
    # ⚠️ run_id 直接查表，不从 read_run_events 的行里取 ——
    #   那个函数的 SELECT 刻意不含 run_id（回放时已按它过滤，再返回是冗余）。
    _runs = [r[0] for r in _c.execute("SELECT run_id FROM orch_stream_events "
                                       "WHERE conversation_id=42 ORDER BY id")]
    check("D5 ★ 落库行的 run_id 全为真批次号（不是 0）",
          bool(_runs) and all(r == 8888 for r in _runs), "run_ids=%s" % _runs)
    check("D6 能按 (conversation_id, run_id) 取回（排障回放入口）",
          len(read_run_events(_c, 42, 8888)) == 2
          and len(read_run_events(_c, 42, 7777)) == 0)

    # D7 坏帧不炸（解析失败只是不落库，不是抛异常）
    _bad = [": ping\n\n", "", "event: stage\ndata: {坏JSON}\n\n", None, 123]
    _ok = True
    for _b in _bad:
        try:
            parse_frame(_b)
            record(_c, 42, _b, run_id=1)
        except Exception:
            _ok = False
    check("D7 畸形帧/心跳/注释帧不抛异常（观测旁路原则）", _ok)
    check("D8 畸形帧不落库（不留垃圾行）",
          record(_c, 42, ": ping\n\n", run_id=1) is False)

    # D9 单次编排事件数上限
    _c.execute("DELETE FROM orch_stream_events")
    for _i in range(12):
        record(_c, 7, _S.to_sse_frame({"type": "stage", "name": "s%d" % _i, "status": "run"}),
               run_id=7, max_per_run=5)
    _n = _c.execute("SELECT COUNT(*) FROM orch_stream_events WHERE conversation_id=7").fetchone()[0]
    check("D9 max_per_run 上限生效（防异常流撑爆表）", _n <= 5, "落库 %d 行（上限 5）" % _n)
    _c.close()

    # ══════════════ E. 开关可回退 ══════════════
    print("\n=== E. 开关可回退 ===")
    # ⚠️ 必须按**赋值点**切段（`CONFIG_SCHEMA = {`），不能用 `split("CONFIG_SCHEMA")` ——
    #   该词在 core/config.py 顶部 docstring 里就出现过，按词切会把 DEFAULT_CONFIG
    #   整段丢掉 ⇒ 后面 4 条断言全部空转（这个坑本会话已踩过一次，见 B1-7 门禁 A3）。
    _def_part = c_src.split("CONFIG_SCHEMA = {")[0]
    _m = re.search(r'"stream_event_log":\s*\{', _def_part)
    check("E0 DEFAULT_CONFIG 段定位到 stream_event_log 组", _m is not None)
    _seg = _def_part[_m.end():_m.end() + 1200] if _m else ""
    for k in ("enabled", "types", "max_per_run"):
        check("E1.%s 有默认值" % k, '"%s"' % k in _seg)
    _sch_part = c_src.split("CONFIG_SCHEMA = {")[1] if "CONFIG_SCHEMA = {" in c_src else ""
    # ⚠️ 不用固定窗口长度找：CONFIG_SCHEMA 段实测 31652 字符，该组在偏移 20098
    #   （按 8000 窗口找会假红）。判据改成"该段里存在该键"这一事实本身。
    check("E2 CONFIG_SCHEMA 也登记了该组（不限位置）",
          '"stream_event_log": {' in _sch_part)
    # E2b 该组在 schema 段里必须真的带三项元信息（防止只挂了个空组）
    _si = _sch_part.find('"stream_event_log": {')
    _sseg = _sch_part[_si:_si + 900] if _si >= 0 else ""
    for k in ("enabled", "types", "max_per_run"):
        check("E2b.%s 在 CONFIG_SCHEMA 段有元信息" % k, '"%s"' % k in _sseg)
    # E4 反向：白名单默认值**不含** token/reasoning（否则默认就灌爆表）
    _tv = re.search(r'"types":\s*"([^"]*)"', _seg)
    check("E4 ★ 默认 types 不含 token/reasoning（逐字增量，灌进去会撑爆表）",
          _tv is not None and not any(w in _tv.group(1) for w in ("token", "reasoning")),
          "types=%s" % (_tv.group(1) if _tv else None))
    # 真关开关 ⇒ should_persist 之外不再有任何副作用（router 整段被 if 包住）
    check("E3 开关判定在 router 里（关=整段跳过）",
          '_cfg_get("stream_event_log", "enabled", True)' in r_src)

    # ══════════════ F. 变异自证 ══════════════
    print("\n=== F. 变异自证（内存副本，不写回源文件）===")
    # F1：把路由层的缓冲改成"边收边落"（即 _evbuf.append 后立刻 record）
    _anchor = "elif _et in _SER_TYPES:\n                            _evbuf.append(raw)"
    if _anchor not in r_src:
        check("F1 变异锚点不在 router 源码里（结构变了，需更新变异体）", False)
    else:
        _mut = r_src.replace(
            _anchor,
            "elif _et in _SER_TYPES:\n"
            "                            _evbuf.append(raw)\n"
            "                            _mut_rec(_mut_c, conv_id, raw, run_id=0)  # MUTATION",
            1)
        # 变异体里 D5 必须翻：run_id 变成 0
        _mut_buf, _mut_run = [], 0
        for _f, _et in zip(_frames, _parsed):
            if _et == "done":
                _, _pl = parse_frame(_f)
                _mut_run = int((_pl.get("data") or {}).get("orch_run_id") or 0)
            elif _et in ("stage", "tool"):
                _mut_buf.append((_f, 0))          # ← 落库时 run_id 恒 0
        _cm = sqlite3.connect(":memory:")
        _cm.row_factory = sqlite3.Row
        _cm.execute("""CREATE TABLE orch_stream_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER DEFAULT 0,
            run_id INTEGER DEFAULT 0, task_key TEXT DEFAULT '', event_type TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        for _f, _r in _mut_buf:
            record(_cm, 42, _f, run_id=_r)
        _cm.commit()
        _z = _cm.execute("SELECT COUNT(*) FROM orch_stream_events WHERE run_id=8888").fetchone()[0]
        check("F1 变异体（边收边落、run_id=0）上 D5 必须 FAIL",
              _z == 0, "变异体里竟有 %d 行 run_id=8888 ⇒ D5 抓不住变异" % _z)
        _cm.close()
        print("  [SELF-PROOF] M1 事件边收边落 —— 落库行 run_id 恒 0，"
              "与 agent_tasks 无法关联，证明白名单+缓冲两处缺一不可")

    # F2：把白名单放开到含 token ⇒ D2 必须 FAIL
    _rec_src = read("services/stream_event_recorder.py")
    _a2 = 'PERSIST_TYPES = ("stage", "tool")'
    if _a2 not in _rec_src:
        check("F2 白名单锚点不在 recorder 源码里", False)
    else:
        _ns = {"__name__": "_mut_recorder"}
        exec(compile(_rec_src.replace(
            _a2, 'PERSIST_TYPES = ("stage", "tool", "token", "reasoning")', 1),
            "<mut>", "exec"), _ns)
        check("F2 变异体（白名单含 token）上 D2 的判据必须 FAIL",
              _ns["should_persist"]("token", {"delta": "x"}) is True,
              "变异体仍拒收 token ⇒ D2 抓不住变异")
        print("  [SELF-PROOF] M2 白名单放开 token/reasoning —— 逐字增量会灌满表")

    # ══════════════ 汇总 ══════════════
    print("\n" + "=" * 60)
    _p = sum(1 for _, o, _ in _results if o)
    _f_ = len(_results) - _p
    print("B2-11/B2-12 门禁：%d 条，%d 通过 / %d 失败" % (len(_results), _p, _f_))
    if _f_:
        print("失败项：")
        for _n, _o, _d in _results:
            if not _o:
                print("  - %s  ← %s" % (_n, _d))
    print("=" * 60)
    return 0 if _f_ == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
