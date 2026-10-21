# -*- coding: utf-8 -*-
"""P2 replanning 行为验证探针（2026-10-21）。

**为什么需要**：门禁 verify_replan_p2.py 钉的是「纯函数正确 + 接线存在」，
    但「重画真的改图、真的让整轮从 partial 变成 full、质量门失败真的停手」
    只有跑真实管道能证明。本探针用 Mock LLM + 注入计划 JSON + 注入执行失败，
    在**临时库**上驱动 `_stream_orchestrated_flow` 真身，四场景断言：

    A 默认关零行为变更：`replanning.enabled=False` + t2 永久失败 ⇒ 行为与 P1 逐字一致
      （t2 failed、t3 blocked、零 plan_revised、零新任务行）—— **P2 的头号纪律**
    B T2 轻量级生效    ：开 enabled + light，t2 首次失败后原地重派（context 注入失败
      原因）⇒ t2 最终 done、**没有新任务行**（同 key 不追行）、plan_revised 事件发出、
      redraw-decide span action=replan、T3 没跑第二轮仍成功 ⇒ **partial → full 转化**
    C T2 重量级生效    ：开 enabled + heavy_redraw ⇒ 出现 `t2_r1` 新任务行（带 lineage）、
      下游 deps 重映射到 t2_r1、assigned_by='replanner'
    D 无进展 → T4      ：同因失败反复 ⇒ redraw-decide action=none + level=T4，
      图不再被改（不追新行），最终 blocked（人工介入）

**纪律**：MBSE_DB_PATH 指向临时库（import 前设环境变量，conftest 同款）；
    MBSE_LLM_FORCE_MOCK=1 零网络；不碰生产 mbse.db。
"""
import json
import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "_replan_p2_probe.db")
for _suf in ("", "-wal", "-shm"):
    if os.path.exists(_TMP_DB + _suf):
        os.remove(_TMP_DB + _suf)
os.environ["MBSE_DB_PATH"] = _TMP_DB
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database import init_db, get_db          # noqa: E402
init_db()

_conn0 = get_db()
_conv_id = 0
try:
    _cur = _conn0.execute(
        "INSERT INTO conversations (title, intent, status) VALUES (?,?,?)",
        ("P2-replan-探针", "design", "active"))
    _conv_id = int(_cur.lastrowid or 0)
    _conn0.commit()
finally:
    _conn0.close()
if not _conv_id:
    raise SystemExit("探针前置失败：无法创建测试会话")

from llm import llm_client                    # noqa: E402
from agent.pipeline import AgentPipeline      # noqa: E402
from core import config as _cfg               # noqa: E402
from services.orch_span import SpanCollector  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


_PLAN_JSON = json.dumps({"tasks": [
    {"key": "t1", "title": "需求分析", "agent": "requirement_analysis", "deps": []},
    {"key": "t2", "title": "系统设计", "agent": "design", "deps": ["t1"],
     "context": "原始设计上下文"},
    {"key": "t3", "title": "方案评审", "agent": "review", "deps": ["t2"]},
]}, ensure_ascii=False)

_orig_chat = llm_client.chat


def _patched_chat(messages, **kw):
    if kw.get("_intent") == "planner":
        return {"choices": [{"message": {"content": _PLAN_JSON}}]}
    return _orig_chat(messages, **kw)


llm_client.chat = _patched_chat

_INJECT = {"fail": {}}
_exec_calls = {}
_orig_exec = AgentPipeline.execute_stream


def _patched_exec(self, *a, **kw):
    tk = str(getattr(self, "_tool_task_key", "?"))
    n = _exec_calls.get(tk, 0) + 1
    _exec_calls[tk] = n
    limit = (_INJECT["fail"] or {}).get(tk)
    if limit and n <= limit:
        raise RuntimeError("timeout（超过 600s 硬上限）")   # 与真库 error_class 口径一致
    return _orig_exec(self, *a, **kw)


AgentPipeline.execute_stream = _patched_exec


def _set_cfg(**kw):
    """改配置（探针内直接改内存态 DEFAULT_CONFIG —— 与 core.config.get 同源对象）。"""
    from core.config import DEFAULT_CONFIG
    for k, v in kw.items():
        DEFAULT_CONFIG["replanning"][k] = v


def _reset_cfg():
    _set_cfg(enabled=False, retry_once=True, max_redraw_rounds=1,
             redraw_expand_ratio=1.5, llm_redraw=False, redraw_light=True,
             skip_leaf=False, no_progress_stop=True, redraw_on_stall=True,
             heavy_redraw=False)


def _run_flow():
    _exec_calls.clear()
    pipe = AgentPipeline()
    pipe._last_skill_hits = []
    agent_def = None
    try:
        agent_def = pipe.registry.get("design")
    except Exception:
        pass
    spans = SpanCollector()
    events = list(pipe._stream_orchestrated_flow(
        "先梳理宽带通信卫星需求，再完成系统设计，最后生成评审报告",
        _conv_id, "main", "design", agent_def, 1, [], [], None,
        {"id": 1, "username": "probe", "display_name": "P2探针"}, None,
        _spans=spans))
    conn = get_db()
    try:
        spans.flush(conn)
        conn.commit()
        run_id = int(conn.execute(
            "SELECT MAX(run_id) rid FROM orch_checkpoints").fetchone()["rid"] or 0)
    finally:
        conn.close()
    return events, run_id


def _rows(run_id, sql="SELECT * FROM agent_tasks WHERE run_id=? ORDER BY id"):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, (run_id,)).fetchall()]
    finally:
        conn.close()


def _tasks(run_id):
    return [(r["task_key"], r["status"], int(r["retry_count"] or 0))
            for r in _rows(run_id)]


def _tmap(run_id):
    """{key: status}（_tasks 是三元组，不能直接 dict()）。"""
    return {k: s for k, s, _ in _tasks(run_id)}


def _trc(run_id):
    """{key: retry_count}。"""
    return {k: c for k, _, c in _tasks(run_id)}


def _replans(run_id):
    out = []
    for r in _rows(run_id, "SELECT * FROM orch_run_spans WHERE run_id=? AND kind='replan' ORDER BY id"):
        r["meta"] = json.loads(r["meta_json"] or "{}")
        out.append(r)
    return out


def _redraw_decisions(run_id):
    return [r for r in _replans(run_id) if r["name"] == "redraw-decide"]


ok = True

# ══════════════════════════════════════════════════════════════════════════
print("=== A. 默认关：零行为变更（P2 头号纪律）===")
_reset_cfg()
_INJECT["fail"] = {"t2": 99}          # t2 永久失败
evA, runA = _run_flow()
ok &= _rec("A1 t2 failed / t3 blocked（P1 原逻辑）",
           _tmap(runA).get("t2") == "failed"
           and _tmap(runA).get("t3") == "blocked",
           str(_tasks(runA)))
ok &= _rec("A2 ★ 零新任务行（总闸关 ⇒ 图未被改）",
           len(_tasks(runA)) == 3, str([t[0] for t in _tasks(runA)]))
ok &= _rec("A3 ★ 零 plan_revised 事件", not any(e.get("type") == "plan_revised" for e in evA))
ok &= _rec("A4 ★ 零 redraw-decide span（连决策都不产生）",
           not _redraw_decisions(runA), "n=%d" % len(_redraw_decisions(runA)))
ok &= _rec("A5 t2 只补跑过一次（T1 行为未被 P2 改变）",
           _trc(runA).get("t2") == 2, str(_tasks(runA)))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== B. T2 轻量级：原地重派 ⇒ partial → full ===")
_reset_cfg()
_set_cfg(enabled=True, redraw_light=True, retry_once=False)   # 关 T1，纯看 T2
_INJECT["fail"] = {"t2": 2}      # 前 2 次失败（T1 关 ⇒ 不补跑），靠 T2 重派救回
evB, runB = _run_flow()
tB = _tasks(runB)
ok &= _rec("B1 ★ t2 被 T2 重派后成功（T1 关，只有 T2 能救）",
           _tmap(runB).get("t2") == "done", str(tB))
ok &= _rec("B2 ★ 整轮从 partial 变 full（t3 也跑完了）",
           _tmap(runB).get("t3") == "done", str(tB))
ok &= _rec("B3 ★ 零新任务行（轻量级原地重派，不追行 ⇒ 同 key 不会双派发）",
           len(tB) == 3, str([t[0] for t in tB]))
ctxB = [r for r in _rows(runB) if r["task_key"] == "t2"]
ok &= _rec("B4 ★ 重派后的 context 注入失败原因（防原样再撞）",
           bool(ctxB) and "重规划" in (ctxB[0]["context"] or "")
           and "换一条实现路径" in (ctxB[0]["context"] or ""),
           (ctxB[0]["context"] or "")[:80] if ctxB else "无行")
ok &= _rec("B5 ★ plan_revised 事件真的发出（P1 只建路径，P2 真发）",
           any(e.get("type") == "plan_revised" for e in evB),
           str([e.get("type") for e in evB if e.get("type") == "plan_revised"]))
rdB = _redraw_decisions(runB)
ok &= _rec("B6 ★ redraw-decide span action=replan / level=T2-light",
           len(rdB) >= 1 and rdB[0]["meta"].get("action") == "replan"
           and rdB[0]["meta"].get("level") == "T2-light",
           json.dumps(rdB[0]["meta"], ensure_ascii=False)[:120] if rdB else "无 span")
ok &= _rec("B7 决策 span 带质量门结果（Q1~Q5 可查）",
           bool(rdB) and isinstance(rdB[0]["meta"].get("gates"), dict)
           and rdB[0]["meta"]["gates"].get("Q1") == "pass",
           json.dumps((rdB[0]["meta"] or {}).get("gates"), ensure_ascii=False) if rdB else "")
ok &= _rec("B8 用户可见：发出重规划 progress 事件",
           any("重规划" in str(e.get("delta") or "") for e in evB))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== C. T2 重量级：新 key + lineage + 下游 deps 重映射 ===")
_reset_cfg()
_set_cfg(enabled=True, heavy_redraw=True, retry_once=False)
_INJECT["fail"] = {"t2": 99}     # 永久失败 ⇒ 只有重画一条路
evC, runC = _run_flow()
rowsC = _rows(runC)
keysC = [r["task_key"] for r in rowsC]
ok &= _rec("C1 ★ 出现新 key 行 t2_r1（重量级换 key，不覆盖旧行）",
           "t2_r1" in keysC, str(keysC))
row_r1 = next((r for r in rowsC if r["task_key"] == "t2_r1"), None)
ok &= _rec("C2 ★ 新行带 config.lineage（from=t2，可追溯为什么有这条任务）",
           bool(row_r1) and "lineage" in (row_r1["config"] or "")
           and json.loads(row_r1["config"])["lineage"].get("from") == "t2",
           (row_r1["config"] or "")[:100] if row_r1 else "无行")
ok &= _rec("C3 ★ 新行 assigned_by='replanner'（审计可归因）",
           bool(row_r1) and row_r1["assigned_by"] == "replanner",
           row_r1["assigned_by"] if row_r1 else "")
t3row = next((r for r in rowsC if r["task_key"] == "t3"), None)
ok &= _rec("C4 ★ 下游 t3 的 deps 重映射到 t2_r1（不悬空、不丢任务）",
           bool(t3row) and "t2_r1" in json.loads(t3row["deps"] or "[]"),
           (t3row["deps"] if t3row else "无行"))
ok &= _rec("C5 旧行 t2 保留在库（失败记录不清除 —— 可审计）",
           "t2" in keysC and _tmap(runC).get("t2") == "failed",
           str(_tasks(runC)))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== D. 无进展检测 ⇒ T4 停手 ===")
_reset_cfg()
_set_cfg(enabled=True, redraw_light=True, retry_once=False,
         no_progress_stop=True, max_redraw_rounds=1)
_INJECT["fail"] = {"t2": 99}      # 同因反复失败
evD, runD = _run_flow()
rdD = _redraw_decisions(runD)
tD = _tasks(runD)
ok &= _rec("D1 ★ redraw-decide 判为 none / level=T4（无进展停手）",
           bool(rdD) and rdD[0]["meta"].get("action") == "none"
           and rdD[0]["meta"].get("level") == "T4",
           json.dumps(rdD[0]["meta"], ensure_ascii=False)[:130] if rdD else "无 span")
ok &= _rec('D2 decision reason 写明"无进展"（可解释，不是黑箱）',
           bool(rdD) and "无进展" in str(rdD[0]["meta"].get("reason") or ""),
           str(rdD[0]["meta"].get("reason"))[:80] if rdD else "")
ok &= _rec("D3 ★ T4 后不追新行（停手 = 图不再变）",
           len(tD) == 3, str([t[0] for t in tD]))
ok &= _rec("D4 剩余任务落到 blocked/blocked 态（人工介入），非静默消失",
           _tmap(runD).get("t2") == "failed" and _tmap(runD).get("t3") == "blocked", str(tD))
ok &= _rec("D5 无 plan_revised（没重画就不发修订事件）",
           not any(e.get("type") == "plan_revised" for e in evD))

_reset_cfg()

# ══════════════════════════════════════════════════════════════════════════
fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for _, n, d in fails:
    print("  FAIL  %s  %s" % (n, d))
sys.exit(0 if not fails else 1)