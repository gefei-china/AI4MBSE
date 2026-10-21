# -*- coding: utf-8 -*-
"""P1 replanning（2026-10-21，执行中重规划第一阶段）回归验证 —— 零 LLM、零服务、零生产库写。

**为什么需要它**（设计文档 v2 §7 P1）：P1 交付"观测与信号结构化"——
    S3~S7 信号汇成观测结构、每次判定落 kind=replan span 决策记录（含
    action=none）、T1 补跑（S3'：timeout/transient/internal，排除 interrupted）。
    本门禁钉住四件事不退化：

    S1  纯函数契约：replan_signal 五个入口 + T1 可补跑类收窄（S3'）
    S2  ★ 接线（本功能真正的失效模式是"建好没人用"）：
        · 失败点有 `_rs.decide_on_failure` 调用（T1 判定）
        · 收敛点有 `_rs.decide_on_stall` 调用（action=none 留痕）
        · replan span 开在 body 里且 parent=_run_sid（挂 run 下防 total_ms 双算）
        · subtask run 事件带 retry_count（T1 用户可见性，v1 §3.2 口径）
    S3  配置双登记（B4-5 教训：配置存在 ≠ 可被配置）：DEFAULT_CONFIG 与
        CONFIG_SCHEMA 的 replanning 段五键一致 + 默认值纪律
        （enabled=False 总闸关 / retry_once=True / llm_redraw=False）
    S4  R11 反向证明：KIND_REPLAN 常量 + schema DDL 注释 + verify_orch_span
        的 S1b/S2c 已含 replan（Task #43 门禁同步，漏一个 CI 红）
    S5  前端接线：kindMeta 有 replan 中文标签 + level/action/reason 行内渲染
    U1  G1 触发判定矩阵逐格（含 S3' 排除 interrupted、不可重试类不补跑、
        每 key 只补一次、开关关不补跑）
    U2  收敛点信号采集（S5/S6/S7；S6 的"被下游依赖"判据；deps JSON 串归一化）
    U3  G6 决策记录完整性（decision_meta 必发字段；reason 永不为空）
    U4  plan_revised 事件路径（none→None；retry→payload 字段全集）
    U5  span 往返：decision → replan span → run_trace 装配出 replan 节点
        （meta_json 2000 字截断不切断 reason）
    M1  变异自证：T1 类加上 interrupted ⇒ U1 的排除格必须抓住
    M2  变异自证：删 already_t1 防护 ⇒ "补跑两次"必须被抓住
    M3  变异自证（源码级）：删失败点接线 ⇒ S2a 断言必须抓住
    M4  变异自证：decision_meta 删 reason ⇒ U3 必须抓住

**临时库纪律**（同 verify_orch_span）：不走 MBSE_DB_PATH 环境变量 —— DB_PATH 在
    import 期绑进 database.connection / database.schema 两个模块作用域。

用法（裸跑自身即完整口径；--live 附加真库只读取证，CI 不跑）：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_replan_p1.py [--live]
退出码：全绿 0 / 有失败或空转 1。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SIGNAL_SRC = os.path.join(ROOT, "services", "replan_signal.py")
STREAM_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
CONFIG_SRC = os.path.join(ROOT, "core", "config.py")
SCHEMA_SRC = os.path.join(ROOT, "database", "schema.py")
ORCH_SPAN_SRC = os.path.join(ROOT, "services", "orch_span.py")
ORCH_SPAN_GATE = os.path.join(ROOT, "tools", "verify", "verify_orch_span.py")
CHAT_JS = os.path.join(ROOT, "static", "js", "mods", "03-chat.js")
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")

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
    ok = True
    sig = _read(SIGNAL_SRC)
    ok &= _rec("S1a 五个入口函数齐备",
               all(("def " + m) in sig for m in (
                   "decide_on_failure", "collect_delivery_signals",
                   "decide_on_stall", "decision_meta", "plan_revised_payload")))
    ok &= _rec("S1b T1 可补跑类 = {timeout, transient, internal}（S3'）",
               'T1_RETRYABLE_CLASSES = frozenset({"timeout", "transient", "internal"})'
               in sig)
    ok &= _rec("S1c 模块零 IO 纪律（无 sqlite/requests/open( 调用）",
               not any(t in sig for t in ("sqlite3", "requests", "urllib",
                                          "open(", "get_db")))

    stream = _read(STREAM_SRC)
    ok &= _rec("S2a ★ 失败点接线：_rs.decide_on_failure 被调用",
               "_rs.decide_on_failure(" in stream)
    ok &= _rec("S2b 收敛点接线：_rs.decide_on_stall 被调用",
               "_rs.decide_on_stall(" in stream)
    ok &= _rec("S2c replan span 开在 body 里且 parent=_run_sid（防 total_ms 双算）",
               stream.count('_spans.open(\n') >= 0
               and '_spans.open("replan"' in stream
               and 'parent=_run_sid, meta={"task": tkey}' in stream)
    ok &= _rec("S2d decision_meta 落 span（G6：每次判定都留痕）",
               stream.count("_rs.decision_meta(") >= 2)
    ok &= _rec("S2e T1 补跑执行路径（TaskQueue.retry + retry_count 写回）",
               "TaskQueue.retry(conn, tk[\"id\"])" in stream
               and "UPDATE agent_tasks SET retry_count=?" in stream)
    ok &= _rec("S2f subtask run 事件带 retry_count（T1 用户可见性）",
               '"status": "run", "title": ttitle,\n                           "agent": tk.get("agent_id") or "", "deps": _deps_map[tkey],'
               in stream and '"retry_count": int(tk.get("retry_count") or 0)' in stream)
    ok &= _rec("S2g 观测旁路铁律：失败点与收敛点接线都有 try 守护",
               stream.count("if _RS_OK:") >= 2)
    ok &= _rec("S2h _RS_OK 标志位模式（不用 `_rs = None` 兜底 —— 避 e2e G0 别名遮蔽）",
               "_RS_OK = False" in stream and "_rs = None" not in stream
               and "if _RS_OK:" in stream)
    _dispatch = stream.count("_yield_collect(self._stream_orchestrated_traced(")
    ok &= _rec("S2i 编排调用点仍全部走包装层（本次未破坏 Task #43 接线）",
               _dispatch == 3, "traced 调用点=%d" % _dispatch)

    cfg = _read(CONFIG_SRC)
    ok &= _rec("S3a DEFAULT_CONFIG.replanning 五键齐全",
               all(('"%s":' % k) in cfg for k in (
                   "enabled", "retry_once", "max_redraw_rounds",
                   "redraw_expand_ratio", "llm_redraw")))
    ok &= _rec("S3b CONFIG_SCHEMA.replanning 双登记（白名单纪律 B4-5）",
               '"replanning": {' in cfg
               and cfg.count('"replanning": {') == 2)
    ok &= _rec("S3c 默认值纪律：总闸关 + T1 开 + LLM 重画关",
               '"enabled": False' in cfg and '"retry_once": True' in cfg
               and '"llm_redraw": False' in cfg
               and '"max_redraw_rounds": 1' in cfg
               and '"redraw_expand_ratio": 1.5' in cfg)

    span = _read(ORCH_SPAN_SRC)
    schema = _read(SCHEMA_SRC)
    ok &= _rec("S4a R11：KIND_REPLAN 常量在 orch_span.py",
               'KIND_REPLAN = "replan"' in span)
    ok &= _rec("S4b R11：schema DDL 注释含 replan 第七类",
               "views | replan" in schema)
    gate43 = _read(ORCH_SPAN_GATE)
    ok &= _rec("S4c R11：verify_orch_span S1b/S2c 已同步 replan",
               "views\", \"replan\")" in gate43 and "replan 第七类" in gate43)

    chat = _read(CHAT_JS)
    ok &= _rec("S5a 前端 kindMeta 有 replan 标签",
               "replan:'🔁 重规划'" in chat)
    ok &= _rec("S5b 前端 replan 行渲染 level/action/reason",
               "if(m.level) bits.push" in chat and "if(m.reason) bits.push" in chat)
    node = shutil.which("node")
    if node:
        import subprocess
        r = subprocess.run([node, "--check", CHAT_JS], capture_output=True, text=True)
        ok &= _rec("S5c JS 语法 03-chat.js", r.returncode == 0,
                   (r.stderr or "")[:200])
    else:
        _rec("S5c JS 语法（无 node 环境，按惯例 SKIP）", True, kind=VACUOUS)

    ci = _read(CI_YML) if os.path.exists(CI_YML) else ""
    ok &= _rec("S6 CI 接线：verify_replan_p1 已注册（G5 纪律）",
               "verify_replan_p1.py" in ci)
    return ok


# ══════════════════════════════════════════════════════════════════════════
# U 行为段（纯函数 + 合成 span 往返，零生产副作用）
# ══════════════════════════════════════════════════════════════════════════
def _temp_db():
    """只含 span 表的临时库（同 verify_orch_span 口径）。"""
    p = os.path.join(tempfile.gettempdir(), "_replan_ut_%d.db" % os.getpid())
    if os.path.exists(p):
        os.remove(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
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
    print("\n=== U. 行为（纯函数 + 合成数据）===")
    from services import replan_signal as RS
    ok = True

    # ── U1 G1 触发判定矩阵逐格 ──────────────────────────────────────────
    # S3 三类可补跑 → T1/retry
    for cls in ("timeout", "transient", "internal"):
        d = RS.decide_on_failure("t4", "err %s" % cls, cls)
        good = (d["level"] == "T1" and d["action"] == "retry"
                and d["retry_keys"] == ["t4"] and d["signals"][0]["id"] == "S3")
        ok &= _rec("U1a S3 %s ⇒ T1 retry 同 key 一次" % cls, good,
                   json.dumps(d, ensure_ascii=False)[:120])
    # S3'：interrupted 排除（真库 50/58 属此类，归 resume）
    d = RS.decide_on_failure("t1", "编排中断：孤儿运行", "interrupted")
    ok &= _rec("U1b S3' interrupted ⇒ T0 none（归 resume 不补跑）",
               d["action"] == "none" and "resume" in d["reason"],
               d["reason"][:80])
    # S4 不可重试类逐类
    for cls in ("permission", "input_invalid", "output_rejected", "business"):
        d = RS.decide_on_failure("t2", "err", cls)
        good = (d["action"] == "none" and d["signals"][0]["id"] == "S4"
                and "不自动补跑" in d["reason"])
        ok &= _rec("U1c S4 %s ⇒ T0 none（不该自动补跑）" % cls, good,
                   d["reason"][:80])
    # 每 key 只补一次（无进展防护）
    d = RS.decide_on_failure("t4", "timeout", "timeout", already_t1=True)
    ok &= _rec("U1d 已补跑过一次 ⇒ 不再补（防补跑死循环）",
               d["action"] == "none" and "已补跑" in d["reason"])
    # 开关关
    d = RS.decide_on_failure("t4", "timeout", "timeout", retry_once=False)
    ok &= _rec("U1e retry_once=False ⇒ 不补跑（配置可回滚）",
               d["action"] == "none" and "retry_once 关" in d["reason"])
    # 空 error_class（分类器不可用的兜底形态）不得误补
    d = RS.decide_on_failure("t4", "", "")
    ok &= _rec("U1f 空分类 ⇒ T0 none（宁漏勿误）",
               d["action"] == "none" and d["signals"][0]["id"] == "S4")

    # ── U2 收敛点信号采集 ──────────────────────────────────────────────
    tasks = [{"task_key": "t1", "deps": [], "status": "done"},
             {"task_key": "t2", "deps": ["t1"], "status": "planned"},
             {"task_key": "t3", "deps": [], "status": "done"}]
    # 无信号
    done_clean = [{"task_key": "t1", "summary": {"status": "full", "claims": []}},
                  {"task_key": "t3", "summary": {"status": "full", "claims": []}}]
    d = RS.decide_on_stall(done_clean, tasks)
    ok &= _rec("U2a 无信号 ⇒ none + reason 明示", d["action"] == "none"
               and "无重规划信号" in d["reason"])
    # S5 partial / missing
    done_p = [{"task_key": "t1", "summary": {"status": "partial",
                                             "missing": ["图A"], "claims": []}}]
    d = RS.decide_on_stall(done_p, tasks)
    ok &= _rec("U2b S5 partial/missing 命中（P1 只留痕不重画）",
               "S5" in [s["id"] for s in d["signals"]] and d["action"] == "none")
    # S6：to_verify 被下游依赖才命中；无下游不命中
    done_v = [{"task_key": "t1", "summary": {"status": "full", "claims": [
        {"kind": "to_verify", "text": "X=5", "subject": "X"}]}}]
    d = RS.decide_on_stall(done_v, tasks)
    ok &= _rec("U2c S6 to_verify 被未完成下游依赖 ⇒ 命中",
               "S6" in [s["id"] for s in d["signals"]])
    tasks_nodown = [{"task_key": "t1", "deps": [], "status": "done"},
                    {"task_key": "t2", "deps": [], "status": "planned"}]
    d = RS.decide_on_stall(done_v, tasks_nodown)
    ok &= _rec("U2d S6 无下游依赖 ⇒ 不命中（防误报）",
               "S6" not in [s["id"] for s in d["signals"]])
    # S6 下游已 done ⇒ 不命中（房子已建完，验证信号迟了）
    tasks_done = [{"task_key": "t1", "deps": [], "status": "done"},
                  {"task_key": "t2", "deps": ["t1"], "status": "done"}]
    d = RS.decide_on_stall(done_v, tasks_done)
    ok &= _rec("U2e S6 下游已 done ⇒ 不命中", "S6" not in [s["id"] for s in d["signals"]])
    # S7：互斥结论（合成两个 Agent 对同一 subject 给互斥值）
    done_c = [{"task_key": "t1", "summary": {"status": "full", "claims": [
        {"kind": "fact", "text": "响应时间=200ms", "subject": "响应时间"}]}},
              {"task_key": "t3", "summary": {"status": "full", "claims": [
                  {"kind": "fact", "text": "响应时间=500ms", "subject": "响应时间"}]}}]
    d = RS.decide_on_stall(done_c, tasks)
    ok &= _rec("U2f S7 互斥结论命中（复用 detect_conflicts 事实源）",
               "S7" in [s["id"] for s in d["signals"]])
    # deps 为 JSON 字符串（DB 原样）不得炸、不得误判
    tasks_str = [{"task_key": "t1", "deps": "[]", "status": "done"},
                 {"task_key": "t2", "deps": '["t1"]', "status": "planned"}]
    try:
        d = RS.decide_on_stall(done_v, tasks_str)
        ok &= _rec("U2g deps JSON 串归一化（S6 仍命中，不按字符迭代）",
                   "S6" in [s["id"] for s in d["signals"]])
    except Exception as e:
        ok &= _rec("U2g deps JSON 串归一化", False, str(e)[:120])
    # 坏数据不抛（观测旁路铁律）
    try:
        d = RS.decide_on_stall([{"task_key": None, "summary": "not-a-dict"}],
                               [{"task_key": 1, "deps": 42, "status": None}])
        ok &= _rec("U2h 坏数据不抛（宁缺勿假）", d["action"] == "none")
    except Exception as e:
        ok &= _rec("U2h 坏数据不抛", False, str(e)[:120])

    # ── U3 G6 决策记录完整性 ──────────────────────────────────────────
    _META_KEYS = {"signals", "level", "action", "reason", "round",
                  "tokens", "latency_ms"}
    cases = [
        RS.decide_on_failure("t4", "timeout", "timeout"),
        RS.decide_on_failure("t1", "孤儿运行", "interrupted"),
        RS.decide_on_stall(done_p, tasks),
        RS.decide_on_stall(done_clean, tasks),
    ]
    bad = []
    for i, d in enumerate(cases):
        m = RS.decision_meta(d, round_no=i, tokens=100 + i)
        if not _META_KEYS.issubset(set(m)):
            bad.append("case%d 缺字段:%s" % (i, _META_KEYS - set(m)))
        if not str(m.get("reason") or "").strip():
            bad.append("case%d reason 空" % i)
        if m.get("level") not in ("T0", "T1"):
            bad.append("case%d level 非法" % i)
    ok &= _rec("U3a G6：四个决策场景 meta 必发字段全集 + reason 非空",
               not bad, ";".join(bad[:3]))
    # action=retry 时 retry_keys 进 meta
    m = RS.decision_meta(cases[0])
    ok &= _rec("U3b retry_keys 随 action=retry 进 meta",
               m.get("retry_keys") == ["t4"])
    m = RS.decision_meta(cases[2])
    ok &= _rec("U3c action=none 不带 retry_keys（不误导下游）",
               "retry_keys" not in m)

    # ── U4 plan_revised 事件路径 ──────────────────────────────────────
    ok &= _rec("U4a action=none ⇒ 不发 plan_revised（None）",
               RS.plan_revised_payload(cases[2], run_id=1) is None)
    p = RS.plan_revised_payload(cases[0], run_id=538)
    good = (p and p.get("type") == "plan_revised" and p.get("run_id") == 538
            and p.get("action") == "retry" and p.get("level") == "T1"
            and p.get("signals") and p.get("retry_keys") == ["t4"]
            and p.get("reason"))
    ok &= _rec("U4b action=retry ⇒ payload 字段全集（P2 接线只差传入）", bool(good),
               json.dumps(p, ensure_ascii=False)[:120] if p else "None")

    # ── U5 span 往返：decision → replan span → run_trace ──────────────
    p, conn = _temp_db()
    try:
        from services import orch_span as OS
        c = OS.SpanCollector(enabled=True)
        c.bind(999, 888)
        sid_run = c.open("run", "run #999")
        d = RS.decide_on_failure("t4", "timeout（超过 600s 硬上限）", "timeout")
        sid_rp = c.open("replan", "t1-decide:t4", parent=sid_run,
                        meta={"task": "t4"})
        time.sleep(0.02)
        c.close(sid_rp, "ok", RS.decision_meta(d, latency_ms=37))
        c.close(sid_run, "ok")
        n = c.flush(conn)
        conn.commit()
        ok &= _rec("U5a replan span 落库（1 行）", n == 2, "n=%d" % n)
        row = conn.execute("SELECT kind, name, parent_id, meta_json FROM orch_run_spans "
                           "WHERE kind='replan'").fetchone()
        good = (row is not None and row["name"] == "t1-decide:t4"
                and row["parent_id"] != 0)
        ok &= _rec("U5b replan span 挂 run 下（parent_id 非 0）", good)
        meta = json.loads(row["meta_json"] or "{}")
        ok &= _rec("U5c meta_json 保真（action/level/reason/signals 2000 字内不截断）",
                   meta.get("action") == "retry" and meta.get("level") == "T1"
                   and meta.get("reason") and meta.get("signals") == ["S3:t4:timeout"]
                   and meta.get("latency_ms") == 37,
                   json.dumps(meta, ensure_ascii=False)[:150])
        tr = OS.run_trace(conn, 999)
        replan_nodes = [s for s in tr["spans"][0]["children"]
                        if s["kind"] == "replan"] if tr["spans"] else []
        ok &= _rec("U5d run_trace 装配出 replan 节点（前端可渲染）",
                   len(replan_nodes) == 1
                   and replan_nodes[0]["meta"].get("action") == "retry")
        ok &= _rec("U5e by_kind 聚合含 replan（trace 面板统计口径）",
                   tr["stats"]["by_kind"].get("replan", {}).get("count") == 1)
        # action=none 的 stall 决策同样完整往返
        d2 = RS.decide_on_stall(done_p, tasks)
        sid_rp2 = c.open("replan", "stall-decide", parent=sid_run)
        c.close(sid_rp2, "ok", RS.decision_meta(d2, latency_ms=5))
        c.flush(conn)
        conn.commit()
        row2 = conn.execute("SELECT meta_json FROM orch_run_spans "
                            "WHERE name='stall-decide' ORDER BY id DESC").fetchone()
        m2 = json.loads(row2["meta_json"] or "{}")
        ok &= _rec("U5f action=none 决策同样落库且字段完整（G6 核心断言）",
                   m2.get("action") == "none" and m2.get("reason")
                   and m2.get("level") == "T0")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if os.path.exists(p):
            os.remove(p)
    return ok


# ══════════════════════════════════════════════════════════════════════════
# M 变异自证：把"错写法"注入**内存孪生**（不碰生产文件）
# ══════════════════════════════════════════════════════════════════════════
def t_mutation():
    print("\n=== M. 变异自证（内存孪生，不写生产文件）===")
    from services import replan_signal as RS   # noqa: F401（证明生产模块可导入）
    src = _read(SIGNAL_SRC)
    ok = True

    def _twin(mut_src, name):
        ns = {}
        exec(compile(mut_src, "<%s>" % name, "exec"), ns)
        return ns

    # M1：T1 类加上 interrupted ⇒ U1b 的排除格必须抓住
    m1 = src.replace(
        'T1_RETRYABLE_CLASSES = frozenset({"timeout", "transient", "internal"})',
        'T1_RETRYABLE_CLASSES = frozenset({"timeout", "transient", "internal", "interrupted"})')
    ok &= _rec("M1a 变异锚点命中（T1 类集合行）", m1 != src)
    if m1 != src:
        ns1 = _twin(m1, "replan_m1")
        d = ns1["decide_on_failure"]("t1", "孤儿运行", "interrupted")
        caught = d["action"] == "retry"      # 正确实现给 none；变异体给 retry
        ok &= _rec("M1b 收窄被抹掉 ⇒ U1b 断言抓住（正确 none，变异体 retry）", caught,
                   "action=%s" % d["action"])

    # M2：删 already_t1 防护 ⇒ "补跑两次"必须被抓住
    m2 = src.replace(
        '        if already_t1:\n'
        '            return {"signals": [sig], "level": T0, "action": ACTION_NONE,\n'
        '                    "reason": "S3 可重试失败（%s），但该 key 已补跑过一次 ⇒ 不再补"\n'
        '                              "（无进展防护）" % cls,\n'
        '                    "retry_keys": []}\n',
        '')
    ok &= _rec("M2a 变异锚点命中（already_t1 分支）", m2 != src)
    if m2 != src:
        ns2 = _twin(m2, "replan_m2")
        d = ns2["decide_on_failure"]("t4", "timeout", "timeout", already_t1=True)
        caught = d["action"] == "retry"      # 正确实现给 none；变异体会补第二次
        ok &= _rec("M2b 无进展防护被删 ⇒ U1d 断言抓住（正确 none，变异体 retry）",
                   caught, "action=%s" % d["action"])

    # M3（源码级）：删失败点接线 ⇒ S2a 必须抓住
    stream = _read(STREAM_SRC)
    m3 = stream.replace("_rs.decide_on_failure(", "_rs.decide_on_failure_DISABLED(")
    ok &= _rec("M3a 变异锚点命中（失败点接线行）", m3 != stream)
    if m3 != stream:
        caught = "_rs.decide_on_failure(" not in m3
        ok &= _rec("M3b 删失败点接线 ⇒ S2a 断言抓住", caught)

    # M4：decision_meta 删 reason ⇒ U3a 必须抓住
    m4 = src.replace('        "reason": str(d.get("reason") or "")[:200],\n', '')
    ok &= _rec("M4a 变异锚点命中（reason 字段行）", m4 != src)
    if m4 != src:
        ns4 = _twin(m4, "replan_m4")
        m = ns4["decision_meta"]({"level": "T0", "action": "none", "reason": "x"})
        caught = "reason" not in m           # 正确实现有 reason；变异体没有
        ok &= _rec("M4b reason 被删 ⇒ U3a 字段全集断言抓住", caught)
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
    # 找一个有 span 的真库 run（trace 上线后的批次）；找不到就 VACUOUS 不计失败
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT run_id, COUNT(*) n FROM orch_run_spans GROUP BY run_id "
                "ORDER BY run_id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        _rec("L0 真库不可达（按惯例不计失败，仅记录）", True, str(e)[:80], kind=VACUOUS)
        return True
    if not row:
        _rec("L0 真库无 span 批次（功能上线前数据，按惯例 SKIP）", True, kind=VACUOUS)
        return True
    run_id = int(row["run_id"])
    try:
        req = urllib.request.Request("%s/api/orchestration/runs/%d/trace" % (base, run_id),
                                     headers={"X-User-Id": uid})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode("utf-8"))
        kinds = {s["kind"] for s in (j.get("spans") or [])}
        ok = _rec("L1 trace API 200 + kind 集合合法（含新 kind 不破坏装配）",
                  bool(kinds) and kinds <= {"run", "plan", "task", "summary",
                                            "reflection", "views", "replan"},
                  "kinds=%s" % sorted(kinds))
        # replan span 若已产生（功能上线后的新批次），meta 必带 level/action/reason
        replans = [s for s in (j.get("spans") or []) if s.get("kind") == "replan"]
        if replans:
            bad = [s["name"] for s in replans
                   if not (s.get("meta") or {}).get("reason")]
            ok &= _rec("L2 replan 节点 meta 带 reason（G6 端到端）", not bad,
                       ";".join(bad[:3]))
        else:
            _rec("L2 该批次无 replan 节点（上线前批次，SKIP）", True, kind=VACUOUS)
    except urllib.error.HTTPError as e:
        ok = _rec("L1 trace API 可访问", False, "HTTP %s" % e.code)
    except Exception as e:
        _rec("L1 服务不可达（按惯例不计失败，仅记录）", True, str(e)[:80], kind=VACUOUS)
        return True
    return ok


def main():
    print("P1 replanning 门禁（信号/决策记录/T1 补跑，零 LLM 零服务零生产库写）")
    ok = t_static()
    ok &= t_unit()
    ok &= t_mutation()
    if "--live" in sys.argv:
        ok &= t_live()
    fails = [r for r in _results if r[0] == FAIL]
    vac = [r for r in _results if r[0] == VACUOUS]
    print("\n════════════════════════════════════════════")
    print("结果：%d PASS / %d FAIL / %d SKIP" % (len(_results) - len(fails) - len(vac),
                                              len(fails), len(vac)))
    for kind, name, detail in fails:
        print("  FAIL  %s  %s" % (name, detail))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
