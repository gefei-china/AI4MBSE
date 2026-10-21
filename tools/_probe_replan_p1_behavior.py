# -*- coding: utf-8 -*-
"""P1 replanning 行为验证探针（一次性，非门禁；2026-10-21）。

**为什么需要**：门禁 verify_replan_p1.py 钉的是「接线存在 + 纯函数正确」，
    但「失败真的触发 T1 补跑、replan span 真的落库、trace 真的装配出节点」
    只有跑真实管道能证明。本探针用 Mock LLM + 注入计划 JSON + 注入执行失败，
    在**临时库**上驱动 `_stream_orchestrated_flow` 真身，四场景断言：

    A 干净 run      ：3 任务全成功 ⇒ 零 replan span / 零 plan_revised（零行为变更证明）
    B T1 补跑成功   ：t2 前两次执行注入 timeout、第三次成功 ⇒ worker 重试 + T1 补跑
                      各一次；t1-decide:t2 span action=retry；retry_count=2；全 done
    C 永久失败收敛  ：t2 三连败 ⇒ 第二条 t1-decide span action=none（已补过一次）+
                      stall-decide span（action=none 留痕）+ t3 blocked
    D trace 装配    ：run_trace 装配出 replan 节点 + by_kind 聚合含 replan

**纪律**：MBSE_DB_PATH 指向临时库（conftest 同款：import 前设环境变量）；
    MBSE_LLM_FORCE_MOCK=1 零网络；不碰生产 mbse.db。
"""
import json
import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "_replan_probe.db")
for _suf in ("", "-wal", "-shm"):
    if os.path.exists(_TMP_DB + _suf):
        os.remove(_TMP_DB + _suf)
os.environ["MBSE_DB_PATH"] = _TMP_DB          # 必须在 import 项目模块前设（conftest 纪律）
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"       # 零网络确定性通道

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tools/ 的上一层 = 工程根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database import init_db, get_db          # noqa: E402
init_db()

# 造一个真实会话（messages 外键需要；conversation_id=0 会撞 FK）
_conn0 = get_db()
_conv_id = 0
try:
    _cur = _conn0.execute(
        "INSERT INTO conversations (title, intent, status) VALUES (?,?,?)",
        ("P1-replan-探针", "design", "active"))
    _conv_id = int(_cur.lastrowid or 0)
    _conn0.commit()
finally:
    _conn0.close()
if not _conv_id:
    raise SystemExit("探针前置失败：无法创建测试会话")

from llm import llm_client                    # noqa: E402
from agent.pipeline import AgentPipeline      # noqa: E402
from services.orch_span import SpanCollector, run_trace  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


# ── 注入 1：planner 返回确定性 3 任务计划（t1→t2→t3 链）───────────────────
_PLAN_JSON = json.dumps({"tasks": [
    {"key": "t1", "title": "需求分析", "agent": "requirement_analysis", "deps": []},
    {"key": "t2", "title": "系统设计", "agent": "design", "deps": ["t1"]},
    {"key": "t3", "title": "方案评审", "agent": "review", "deps": ["t2"]},
]}, ensure_ascii=False)

_orig_chat = llm_client.chat


def _patched_chat(messages, **kw):
    if kw.get("_intent") == "planner":
        return {"choices": [{"message": {"content": _PLAN_JSON}}]}
    return _orig_chat(messages, **kw)


llm_client.chat = _patched_chat

# ── 注入 2：execute_stream 按任务计数失败（探针场景控制）──────────────────
_INJECT = {"fail": {}}          # {task_key: 前 N 次调用抛 timeout}
_exec_calls = {}
_orig_exec = AgentPipeline.execute_stream


def _patched_exec(self, *a, **kw):
    tk = str(getattr(self, "_tool_task_key", "?"))
    n = _exec_calls.get(tk, 0) + 1
    _exec_calls[tk] = n
    limit = (_INJECT["fail"] or {}).get(tk)
    if limit and n <= limit:
        raise RuntimeError("timeout（探针注入失败）")
    return _orig_exec(self, *a, **kw)


AgentPipeline.execute_stream = _patched_exec


def _run_flow():
    """跑一次真编排；返回 (events, spans, run_id)。"""
    _exec_calls.clear()
    pipe = AgentPipeline()
    pipe._last_skill_hits = []        # 正常由意图路由预置；探针直调 flow 需自备
    agent_def = None
    try:
        agent_def = pipe.registry.get("design")     # flow 内 L1728 用 agent_def.name
    except Exception:
        pass
    spans = SpanCollector()
    events = list(pipe._stream_orchestrated_flow(
        "先梳理宽带通信卫星需求，再完成系统设计，最后生成评审报告",
        _conv_id, "main", "design", agent_def, 1, [], [], None,
        {"id": 1, "username": "probe", "display_name": "P1探针"}, None,
        _spans=spans))
    # flush 到临时库（与 traced 包装层同口径：独立连接一次事务）
    conn = get_db()
    try:
        spans.flush(conn)
        conn.commit()
        row = conn.execute("SELECT MAX(run_id) rid FROM orch_checkpoints").fetchone()
        run_id = int(row["rid"] or 0)
    finally:
        conn.close()
    return events, spans, run_id


def _spans_of(run_id, kind=None):
    conn = get_db()
    try:
        if kind:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM orch_run_spans WHERE run_id=? AND kind=? ORDER BY id",
                (run_id, kind)).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM orch_run_spans WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
    finally:
        conn.close()


def _tasks_of(run_id):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT task_key, status, retry_count, error FROM agent_tasks WHERE run_id=?",
            (run_id,)).fetchall()]
    finally:
        conn.close()


def _replans(run_id):
    out = []
    for s in _spans_of(run_id, "replan"):
        s["meta"] = json.loads(s["meta_json"] or "{}")
        out.append(s)
    return out


# ══════════════════════════════════════════════════════════════════════════
print("=== A. 干净 run（零行为变更证明）===")
_INJECT["fail"] = {}
evA, spA, runA = _run_flow()
ok = True
ok &= _rec("A1 三个子任务全部 done",
           sorted(t["status"] for t in _tasks_of(runA)) == ["done", "done", "done"],
           str([(t["task_key"], t["status"]) for t in _tasks_of(runA)]))
ok &= _rec("A2 零 replan span（无失败 ⇒ 无决策记录）",
           not _replans(runA), "n=%d" % len(_replans(runA)))
ok &= _rec("A3 零 plan_revised 事件", not any(e.get("type") == "plan_revised" for e in evA))
ok &= _rec("A4 零「自动补跑」progress 事件",
           not any("自动补跑" in str(e.get("delta") or "") for e in evA))
trA = run_trace(get_db(), runA)
ok &= _rec("A5 trace 树完整（run/plan/task 在，replan 不在）",
           "replan" not in trA["stats"]["by_kind"]
           and {"run", "plan", "task"} <= set(trA["stats"]["by_kind"]),
           str(sorted(trA["stats"]["by_kind"])))
ok &= _rec("A6 干净 run 的 subtask run 事件 retry_count=0（新增字段不破坏旧语义）",
           all(e.get("retry_count", 0) == 0 for e in evA
               if e.get("type") == "subtask" and e.get("status") == "run"))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== B. T1 补跑（前 2 次注入失败，第 3 次成功）===")
_INJECT["fail"] = {"t2": 2}
evB, spB, runB = _run_flow()
tkB = {t["task_key"]: t for t in _tasks_of(runB)}
ok = True
ok &= _rec("B1 t2 最终 done（补跑后成功）", tkB.get("t2", {}).get("status") == "done",
           str(tkB.get("t2")))
ok &= _rec("B2 t2 retry_count=2（worker 内重试 +1，T1 补跑 +1）",
           int(tkB.get("t2", {}).get("retry_count") or 0) == 2,
           "retry_count=%s" % tkB.get("t2", {}).get("retry_count"))
rpB = _replans(runB)
t1B = [s for s in rpB if s["name"] == "t1-decide:t2"]
ok &= _rec("B3 t1-decide:t2 replan span 恰好 1 条", len(t1B) == 1, "n=%d" % len(t1B))
if t1B:
    m = t1B[0]["meta"]
    ok &= _rec("B4 该 span action=retry / level=T1 / signals=S3:t2:timeout",
               bool(m.get("action") == "retry" and m.get("level") == "T1"
                    and m.get("signals") == ["S3:t2:timeout"]
                    and m.get("reason")),
               json.dumps(m, ensure_ascii=False)[:150])
    ok &= _rec("B5 该 span 挂 run span 下（parent 非 0）",
               int(t1B[0]["parent_id"] or 0) != 0)
ok &= _rec("B6 「自动补跑一次」progress 事件恰好 1 条",
           sum(1 for e in evB if "自动补跑" in str(e.get("delta") or "")) == 1)
ok &= _rec("B7 全链最终 3 done（补跑不破坏后续）",
           sorted(t["status"] for t in _tasks_of(runB)) == ["done", "done", "done"])
ok &= _rec("B8 补跑后 t2 的 done 事件带 retry_count=2（前端徽章可见）",
           any(e.get("type") == "subtask" and e.get("key") == "t2"
               and e.get("status") == "done" and e.get("retry_count") == 2 for e in evB))
trB = run_trace(get_db(), runB)
_run_root = [s for s in trB["spans"] if s["kind"] == "run"]
ok &= _rec("B9 run_trace 装配出 replan 节点 + by_kind 计数=1",
           trB["stats"]["by_kind"].get("replan", {}).get("count") == 1
           and bool(_run_root)
           and any(s["kind"] == "replan" for s in _run_root[0]["children"]))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== C. 永久失败（3 连败 ⇒ 收敛留痕 + 下游 blocked）===")
_INJECT["fail"] = {"t2": 99}
evC, spC, runC = _run_flow()
tkC = {t["task_key"]: t for t in _tasks_of(runC)}
rpC = _replans(runC)
t1C = [s for s in rpC if s["name"] == "t1-decide:t2"]
stallC = [s for s in rpC if s["name"] == "stall-decide"]
ok = True
ok &= _rec("C1 t2 failed + t3 blocked（失效传播原样，未被补跑掩盖）",
           tkC.get("t2", {}).get("status") == "failed"
           and tkC.get("t3", {}).get("status") == "blocked",
           str([(t["task_key"], t["status"]) for t in _tasks_of(runC)]))
ok &= _rec("C2 t2 retry_count=3（worker 重试×2 + T1×1，3 次重试事件）",
           int(tkC.get("t2", {}).get("retry_count") or 0) == 3,
           "retry_count=%s" % tkC.get("t2", {}).get("retry_count"))
ok &= _rec("C2b t2 共执行 4 次（首Worker 2 次 + T1 派发后 Worker 2 次，补跑一次后不再补）",
           _exec_calls.get("t2") == 4, "exec=%s" % _exec_calls.get("t2"))
ok &= _rec("C3 t1-decide:t2 两条：retry + none（已补过一次）",
           len(t1C) == 2 and t1C[0]["meta"].get("action") == "retry"
           and t1C[1]["meta"].get("action") == "none"
           and "已补跑" in (t1C[1]["meta"].get("reason") or ""),
           json.dumps([s["meta"].get("action") for s in t1C]))
ok &= _rec("C4 stall-decide span 存在且 action=none + reason 非空（G6 收敛留痕）",
           bool(len(stallC) == 1 and stallC[0]["meta"].get("action") == "none"
                and stallC[0]["meta"].get("reason")),
           json.dumps(stallC[0]["meta"], ensure_ascii=False)[:150] if stallC else "无")
ok &= _rec("C5 零 plan_revised 事件（P1 不发计划变更）",
           not any(e.get("type") == "plan_revised" for e in evC))
ok &= _rec("C6 「自动补跑」progress 恰好 1 条（第二次失败不再补）",
           sum(1 for e in evC if "自动补跑" in str(e.get("delta") or "")) == 1)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== D. 汇总 ===")
fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for kind, name, detail in fails:
    print("  FAIL  %s  %s" % (name, detail))
# 清理注入
llm_client.chat = _orig_chat
AgentPipeline.execute_stream = _orig_exec
# Windows 下 sqlite 连接未显式 close 时文件被占用（WinError 32）⇒ 先强制 GC
# 回收悬空连接再重试；仍删不掉也只警告不撒谎（退出码必须只反映断言结果）。
import gc
import time
for _suf in ("", "-wal", "-shm"):
    for _try in range(5):
        if not os.path.exists(_TMP_DB + _suf):
            break
        try:
            gc.collect()
            os.remove(_TMP_DB + _suf)
            break
        except PermissionError:
            if _try == 4:
                print("  [warn] 临时库清理失败（文件被占用）：%s" % (_TMP_DB + _suf))
            else:
                time.sleep(0.2)
sys.exit(0 if not fails else 1)
