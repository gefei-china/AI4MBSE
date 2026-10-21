# -*- coding: utf-8 -*-
"""P3 replanning 行为验证探针（2026-10-21）。

**为什么需要**：门禁 verify_replan_p3.py 钉的是「纯函数正确 + 接线存在 +
静态契约」，但有三件事只有跑**真实管道**才能证明，且它们恰恰是本阶段
最容易"看起来对、实际不生效"的地方：

    A ★ 默认关零行为变更：`user_directive=False` / `user_confirm_redraw=False`
      时，followup 队列里的指令**完全不影响编排**（既不重派也不留 replan
      span），确认卡**从不出现** —— P3 的头号纪律。
    B ★ S1 DAG 级指令真的生效：`user_directive=True` + 编排执行中往 followup
      队列投「t2 改成表格」⇒ t2 被**原地重派**（retry_count+1、context 被注入
      指令）、**没有新任务行**、directive-decide span action=retask、
      plan_revised 事件发出。
    C ★ C1 分工不抢队列：DAG 级**只读不消费** ⇒ 编排结束后单 Agent 的
      follow-up 仍能 `pop` 到同一条指令（若被抢走，这里会拿到空串）。
    D ★ 确认卡否决生效：`user_confirm_redraw=True` + 用户不批准 ⇒ 重画**不发生**
      （图不变），且 TTL 超时按"维持原计划"收口（v1 R10）。
    E ★ 确认卡批准生效：批准 ⇒ 重画**真的发生**（t2_r1 新行 / t2 重派）。
    F ★ 影响评估留痕可解释：每次评估（含 T0）都落 span，basis 非空。

**纪律**：MBSE_DB_PATH 指向临时库（import 前设环境变量）；MBSE_LLM_FORCE_MOCK=1
    零网络；不碰生产 mbse.db。确认卡等待用**极短 TTL**（0.2s）以免探针挂住 ——
    等待逻辑本身由门禁 U5k/U5l 用假时钟逐格覆盖。
"""
import json
import os
import sys
import tempfile
import time

_TMP_DB = os.path.join(tempfile.gettempdir(), "_replan_p3_probe.db")
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
        ("P3-replan-探针", "design", "active"))
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
from agent import followup as _fup            # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}"
          + (f"  {detail}" if not ok and detail else ""))
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

#: 编排执行中往 followup 队列投递的指令（场景 B/C 用）。
#: 在 t2 被派发时投 —— 确保主循环迭代边界能看见它。
_DIRECTIVE = {"text": "", "at": ""}


def _run_subtask_hook(fn):
    """包一层 `_run_subtask`：在指定任务被派发瞬间投递用户指令。

    为什么用钩子而不是"编排前先 push"：编排前 push 的指令会在**第一个迭代
    边界**就被看到，但那时 t1 可能还没 done —— 影响评估的对象集合与真实场景
    不同。真实场景是"用户看到 t2 的产出后补一句"，即 t2 已在跑或刚完成。
    """
    def _wrapped(*a, **kw):
        key = str(kw.get("tk", {}).get("task_key") or
                  (a[1] if len(a) > 1 and isinstance(a[1], dict) else "") or "")
        if _DIRECTIVE["text"] and key == _DIRECTIVE["at"]:
            _fup.push(_conv_id, _DIRECTIVE["text"])
        return fn(*a, **kw)
    return _wrapped


_ORIG_RUN_SUBTASK = None


def _run_flow():
    _fup.drop(_conv_id)
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
        {"id": 1, "username": "probe", "display_name": "P3探针"}, None,
        _spans=spans))
    conn = get_db()
    try:
        spans.flush(conn)
        conn.commit()
        # ★ run_id 取法：取**该会话最新有任务行的 run**，而不是
        #   `MAX(run_id) FROM orch_checkpoints` —— 后者会拿到历史批次的
        #   checkpoint 行号（重画/恢复场景下与实际任务行不在同一 run），
        #   表现为"断言查不到新任务行"，而实际是取错了 run。
        run_id = int(conn.execute(
            "SELECT MAX(run_id) rid FROM agent_tasks WHERE conversation_id=?",
            (_conv_id,)).fetchone()["rid"] or 0)
    finally:
        conn.close()
    return events, run_id


def _rows(run_id, sql="SELECT * FROM agent_tasks WHERE run_id=? ORDER BY id"):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, (run_id,)).fetchall()]
    finally:
        conn.close()


def _tmap(run_id):
    return {r["task_key"]: r["status"] for r in _rows(run_id)}


def _trc(run_id):
    return {r["task_key"]: int(r["retry_count"] or 0) for r in _rows(run_id)}


def _replans(run_id):
    out = []
    for r in _rows(run_id, "SELECT * FROM orch_run_spans WHERE run_id=? "
                          "AND kind='replan' ORDER BY id"):
        r["meta"] = json.loads(r["meta_json"] or "{}")
        out.append(r)
    return out


def _directive_decisions(run_id):
    return [r for r in _replans(run_id) if r["name"] == "directive-decide"]


def _set_cfg(**kw):
    from core.config import DEFAULT_CONFIG
    for k, v in kw.items():
        DEFAULT_CONFIG["replanning"][k] = v


def _reset_cfg(**over):
    _set_cfg(enabled=False, retry_once=True, max_redraw_rounds=1,
             redraw_expand_ratio=1.5, llm_redraw=False, redraw_light=True,
             skip_leaf=False, no_progress_stop=True, redraw_on_stall=True,
             heavy_redraw=False, user_directive=False,
             user_confirm_redraw=False, confirm_ttl_sec=60,
             directive_max_touch=3)
    if over:
        _set_cfg(**over)


# 定位 _run_subtask 以安装投递钩子 —— 它是主循环内部闭包，
# 因此改为在 execute_stream 上挂钩（t1 派发 ⇒ 投指令）。
_ORIG_EXEC = AgentPipeline.execute_stream


def _patched_exec(self, *a, **kw):
    key = str(getattr(self, "_tool_task_key", "") or "")
    if _DIRECTIVE["text"] and key == _DIRECTIVE["at"]:
        _fup.push(_conv_id, _DIRECTIVE["text"])
        _DIRECTIVE["text"] = ""      # 只投一次
    return _ORIG_EXEC(self, *a, **kw)


AgentPipeline.execute_stream = _patched_exec

ok = True

# ══════════════════════════════════════════════════════════════════════════
print("=== A. ★ 默认关：零行为变更（P3 头号纪律）===")
_reset_cfg()
_DIRECTIVE.update({"text": "t2 改成表格形式输出", "at": "t1"})
evA, runA = _run_flow()
ok &= _rec("A1 正常跑完：t1/t2/t3 全 done",
           all(_tmap(runA).get(k) == "done" for k in ("t1", "t2", "t3")),
           str(_tmap(runA)))
ok &= _rec("A2 ★ user_directive 关 ⇒ 无 directive-decide span（挂点是 no-op）",
           not _directive_decisions(runA),
           str([r["name"] for r in _directive_decisions(runA)]))
ok &= _rec("A3 ★ 默认关时 t2 的 retry_count 未被指令抬升（仍为首次执行的 0）",
           _trc(runA).get("t2") == 0, str(_trc(runA)))
ok &= _rec("A4 ★ user_confirm_redraw 关 ⇒ 无确认卡事件",
           not [e for e in evA if e.get("type") == "redraw_confirm"])
ok &= _rec("A5 ★ 默认关 ⇒ 零新任务行（无 replace 痕迹）",
           len(_rows(runA)) == 3, str(len(_rows(runA))))

print("\n=== B. ★ S1 DAG 级指令真的生效（原地重派，不追行）===")
_reset_cfg(enabled=True, user_directive=True)
_DIRECTIVE.update({"text": "系统设计改成表格形式输出", "at": "t1"})
evB, runB = _run_flow()
dd = _directive_decisions(runB)
ok &= _rec("B1 ★ directive-decide span 落库（G10 留痕）", bool(dd),
           "span 数=%d" % len(dd))
ok &= _rec("B2 ★ 评估级别为 T2-light（不是 T3：改产出要求≠推翻划分）",
           bool(dd) and dd[0]["meta"].get("level") == "T2-light",
           str(dd[0]["meta"].get("level")) if dd else "无 span")
ok &= _rec("B3 ★ 评估动作 retask（区别于 T3 的 redraw）",
           bool(dd) and dd[0]["meta"].get("action") == "retask",
           str(dd[0]["meta"].get("action")) if dd else "无 span")
ok &= _rec("B4 ★ 评估依据 basis 非空（G10 可解释性）",
           bool(dd) and bool(dd[0]["meta"].get("signals")),
           str(dd[0]["meta"].get("signals")) if dd else "无 span")
ok &= _rec("B5 ★ 命中 t2（未重派 t1/t3）",
           bool(dd) and dd[0]["meta"].get("targets") == ["t2"],
           str(dd[0]["meta"].get("targets")) if dd else "无 span")
ok &= _rec("B6 ★ t2 被重派：retry_count 由 0 抬升到 1"
           "（口径：schema 默认 0=首次执行，流式不重试）",
           _trc(runB).get("t2", -1) >= 1
           and _trc(runB).get("t1") == 0
           and _trc(runB).get("t3") == 0,
           str(_trc(runB)))
ctx = ""
for r in _rows(runB):
    if r["task_key"] == "t2":
        ctx = str(r["context"] or "")
ok &= _rec("B7 ★ t2 的 context 被注入用户指令",
           "改成表格" in ctx, ctx[:120])
ok &= _rec("B8 ★ 原上下文保留（追加而非覆盖）", "原始设计上下文" in ctx,
           ctx[:120])
ok &= _rec("B9 ★ 原地重派不追行：仍只有 3 行任务",
           len(_rows(runB)) == 3,
           str([(r["task_key"], r["status"]) for r in _rows(runB)]))
ok &= _rec("B10 ★ t2 最终 done（重派后跑通）",
           _tmap(runB).get("t2") == "done", str(_tmap(runB)))
ok &= _rec("B11 ★ plan_revised 事件发出（用户可见）",
           bool([e for e in evB if e.get("type") == "plan_revised"]))

print("\n=== C. ★ C1 分工：DAG 级只读不消费 followup 队列 ===")
left = _fup.peek_text(_conv_id)
ok &= _rec("C1 ★ 指令未被 DAG 级消费（peek 仍能看到）",
           "改成表格" in (left or ""), repr(left)[:120])
got = _fup.pop(_conv_id)
ok &= _rec("C2 ★ 单 Agent 通道仍能取到同一条指令（没被抢走）",
           "改成表格" in (got or ""), repr(got)[:120])

print("\n=== D. ★ 确认卡否决：重画不发生 ===")
from services import replan_confirm as _RCFM   # noqa: E402

_reset_cfg(enabled=True, user_directive=False)
# 让 t2 永久失败 ⇒ 收敛点触发重画；确认卡开启且**不批准** ⇒ 图不变。
_INJECT_FAIL = {"t2": 99}


def _patched_exec_fail(self, *a, **kw):
    key = str(getattr(self, "_tool_task_key", "") or "")
    if key == "t2":
        raise RuntimeError("timeout（超过 600s 硬上限）")
    return _ORIG_EXEC(self, *a, **kw)


AgentPipeline.execute_stream = _patched_exec_fail
_reset_cfg(enabled=True, user_confirm_redraw=True, confirm_ttl_sec=1)
evD, runD = _run_flow()
confirm_evs = [e for e in evD if e.get("type") == "redraw_confirm"]
ok &= _rec("D1 ★ 确认卡事件已下发（用户拿到否决权）", bool(confirm_evs),
           "确认卡数=%d" % len(confirm_evs))
ok &= _rec("D2 ★ 确认卡带三动作（批准/维持/补充）",
           bool(confirm_evs) and len(confirm_evs[0].get("actions") or []) == 3)
ok &= _rec("D3 ★ 超时语义显式下发（前端不会误写成自动批准）",
           bool(confirm_evs)
           and confirm_evs[0].get("timeout_behavior") == "keep_original")
ok &= _rec("D4 ★ TTL 超时 ⇒ 维持原计划：无新任务行",
           len(_rows(runD)) == 3, str([(r["task_key"], r["status"])
                                       for r in _rows(runD)]))
ok &= _rec("D5 ★ 维持原计划 ⇒ t3 blocked（不做静默重画）",
           _tmap(runD).get("t3") == "blocked", str(_tmap(runD)))
ok &= _rec("D6 ★ 编排给出可见提示（不是静默）",
           bool([e for e in evD if e.get("type") == "reasoning"
                 and "重画未执行" in str(e.get("delta") or "")]))

print("\n=== E. ★ 确认卡批准：重画真的发生 ===")


def _patched_exec_approve(self, *a, **kw):
    key = str(getattr(self, "_tool_task_key", "") or "")
    if key == "t2":
        raise RuntimeError("timeout（超过 600s 硬上限）")
    return _ORIG_EXEC(self, *a, **kw)


AgentPipeline.execute_stream = _patched_exec_approve
_reset_cfg(enabled=True, user_confirm_redraw=True, confirm_ttl_sec=20,
           heavy_redraw=True)
# ★ 后台线程**轮询到真的有卡才应答**。早先版本在 `_run_flow()` 之前就启动
#   线程，而卡是在编排跑到收敛点时才 `open_` 的 ⇒ 线程在 60 次×50ms=3s 内
#   扑空，之后没人应答 ⇒ 表现为"TTL 过期维持原计划"，被误读成"批准功能坏了"。
#   正确做法：轮询到卡出现为止，且 TTL 给足（20s）。
import threading                                # noqa: E402


def _approver(conv_id):
    for _ in range(1200):                      # 最多等 60s
        if _RCFM.pending_count(conv_id):
            _RCFM.resolve(conv_id, None, _RCFM.ACTION_APPROVE)
            return
        time.sleep(0.05)


_th = threading.Thread(target=_approver, args=(_conv_id,), daemon=True)
_th.start()
evE, runE = _run_flow()
_th.join(timeout=3)
ok &= _rec("E1 ★ 批准后确认卡动作被记录（approve）",
           bool([e for e in evE if e.get("type") == "reasoning"
                 and "已批准重画" in str(e.get("delta") or "")]),
           str([e.get("delta") for e in evE
                if e.get("type") == "reasoning"][-4:]))
ok &= _rec("E2 ★ 批准后重画真的发生：出现 t2_r1 新任务行",
           any(r["task_key"] == "t2_r1" for r in _rows(runE)),
           str([(r["task_key"], r["status"]) for r in _rows(runE)]))
ok &= _rec("E3 ★ 新任务带 lineage（可追溯为何存在）",
           any(r["task_key"] == "t2_r1"
               and "lineage" in (r.get("config") or "{}")
               for r in _rows(runE)))
ok &= _rec("E4 ★ assigned_by=replanner（与用户规划区分）",
           any(r["task_key"] == "t2_r1" and r.get("assigned_by") == "replanner"
               for r in _rows(runE)))

print("\n=== G. ★ T3 LLM 整图重画：默认关 / 开启后真实落库 ===")

# G1：llm_redraw 默认关 ⇒ 用户指令触发 T3 时**不得**新增任何任务行
AgentPipeline.execute_stream = _patched_exec
_reset_cfg(enabled=True, user_directive=True, llm_redraw=False)
_DIRECTIVE["text"] = "整体重做计划：换个路径重写第三部分"
_DIRECTIVE["at"] = "t2"
evG1, runG1 = _run_flow()
_rowsG1 = _rows(runG1)
ok &= _rec("G1a ★ llm_redraw 默认关 ⇒ T3 不新增任务行（零行为变更）",
           len(_rowsG1) == 3,
           "rows=%d" % len(_rowsG1))
ok &= _rec("G1b ★ llm_redraw 默认关 ⇒ 无 T3 级别的计划修订事件",
           not [e for e in evG1
                if e.get("type") == "plan_revised"
                and (e.get("level") or "") == "T3"],
           str([e.get("level") for e in evG1 if e.get("type") == "plan_revised"]))

# G2：llm_redraw 开启 + 注入假的 LLM 重画输出 ⇒ 真的新增替代任务
#   ⚠️ 注入方式：只拦 `llm_client.chat` 里**含重画提示词特征**的调用，
#   planner 的正常规划调用不受影响（否则整条编排都跑不通）。
import llm as _llm_mod                        # noqa: E402

_ORIG_CHAT = _llm_mod.llm_client.chat
_T3_INJECT = {"on": True, "calls": 0}

_T3_TASKS = [
    {"key": "t3_r1", "title": "第三部分（换路径重写）",
     "deps": ["t2"], "context": "按用户指令换路径重写",
     "task_type": "agent"},
]


def _chat_t3(messages, *a, **kw):
    try:
        _txt = " ".join(str((m or {}).get("content") or "")
                         for m in (messages or []))
    except Exception:
        _txt = ""
    if _T3_INJECT["on"] and ("替代" in _txt or "重画" in _txt
                             or "redraw" in _txt.lower()):
        _T3_INJECT["calls"] += 1
        return {"choices": [{"message": {"content": json.dumps(
            {"tasks": _T3_TASKS}, ensure_ascii=False)}}]}
    return _ORIG_CHAT(messages, *a, **kw)


_llm_mod.llm_client.chat = _chat_t3
try:
    _reset_cfg(enabled=True, user_directive=True, llm_redraw=True)
    _DIRECTIVE["text"] = "整体重做计划：换个路径重写第三部分"
    _DIRECTIVE["at"] = "t2"
    evG2, runG2 = _run_flow()
finally:
    _llm_mod.llm_client.chat = _ORIG_CHAT

_rowsG2 = _rows(runG2)
_keysG2 = [r["task_key"] for r in _rowsG2]
ok &= _rec("G2a ★ T3 真的调了 LLM（不是只判定不执行）",
           _T3_INJECT["calls"] >= 1,
           "calls=%d" % _T3_INJECT["calls"])
ok &= _rec("G2b ★ T3 新增了替代任务 t3_r1",
           "t3_r1" in _keysG2, str(_keysG2))
ok &= _rec("G2c ★ 新任务 lineage 指向被替代的旧 key（可追溯）",
           any(r["task_key"] == "t3_r1"
               and "t3" in json.dumps(json.loads(r["config"] or "{}"))
               for r in _rowsG2),
           str([r["config"] for r in _rowsG2 if r["task_key"] == "t3_r1"]))
ok &= _rec("G2d ★ 新任务被标记为 replanner 派发",
           any(r["task_key"] == "t3_r1"
               and str(r["assigned_by"] or "") == "replanner"
               for r in _rowsG2),
           str([(r["task_key"], r["assigned_by"]) for r in _rowsG2]))
ok &= _rec("G2e ★ T3 产出走 plan_revised 通道（用户可见修订卡）",
           any(e.get("type") == "plan_revised"
               and (e.get("level") or "") == "T3" for e in evG2),
           str([(e.get("type"), e.get("level")) for e in evG2
                if e.get("type") == "plan_revised"]))

# G2f：done 任务永不被替代（不变量 1）—— 范围锁定应把"替代 t2"的产出拦下
_T3_INJECT["on"] = True
_T3_INJECT["calls"] = 0
_T3_TASKS[:] = [{"key": "t2_r1", "title": "重写已完成的 t2",
                 "deps": ["t1"], "task_type": "agent"}]
_llm_mod.llm_client.chat = _chat_t3
try:
    _reset_cfg(enabled=True, user_directive=True, llm_redraw=True)
    _DIRECTIVE["text"] = "整体重做计划：换个路径重写第三部分"
    _DIRECTIVE["at"] = "t2"
    evG2f, runG2f = _run_flow()
finally:
    _llm_mod.llm_client.chat = _ORIG_CHAT

_rowsG2f = _rows(runG2f)
ok &= _rec("G2f ★ done 任务不被替代（不变量 1：done 永不重跑）",
           "t2_r1" not in [r["task_key"] for r in _rowsG2f],
           str([r["task_key"] for r in _rowsG2f]))
ok &= _rec("G2g ★ 越界替代 done 被拦时留下可见痕迹",
           any("T3 重画被拦" in str(e.get("delta") or "")
               for e in evG2f if e.get("type") == "reasoning"),
           str([str(e.get("delta") or "")[:40] for e in evG2f
                if e.get("type") == "reasoning"][-3:]))

# G3：LLM 越界（凭空加任务，非替代）⇒ 质量门拦下，**不落库**
_T3_INJECT["on"] = True
_T3_INJECT["calls"] = 0
_T3_TASKS[:] = [{"key": "perf_review", "title": "凭空插入的性能评审",
                 "deps": [], "task_type": "agent"}]
_llm_mod.llm_client.chat = _chat_t3
try:
    _reset_cfg(enabled=True, user_directive=True, llm_redraw=True)
    _DIRECTIVE["text"] = "整体重做计划：换个路径重写第三部分"
    _DIRECTIVE["at"] = "t2"
    evG3, runG3 = _run_flow()
finally:
    _llm_mod.llm_client.chat = _ORIG_CHAT

_rowsG3 = _rows(runG3)
ok &= _rec("G3a ★ LLM 越界插入 ⇒ 质量门拦下，不落库（无 perf_review 行）",
           "perf_review" not in [r["task_key"] for r in _rowsG3],
           str([r["task_key"] for r in _rowsG3]))
ok &= _rec("G3b ★ 越界时留下 T3 拦截的可见痕迹",
           any("T3 重画被拦" in str(e.get("delta") or "")
               for e in evG3 if e.get("type") == "reasoning"),
           str([str(e.get("delta") or "")[:40] for e in evG3
                if e.get("type") == "reasoning"][-3:]))

# G3 段的注入状态复位（避免影响 F 段回归）
_T3_INJECT["on"] = False
_T3_TASKS[:] = []

print("\n=== F. ★ F1 重规划默认关时确认卡与指令通道全关（回归）===")
AgentPipeline.execute_stream = _patched_exec          # 恢复无失败版本
_reset_cfg()
evF, runF = _run_flow()
ok &= _rec("F1 默认关：正常跑完且无重规划痕迹",
           all(_tmap(runF).get(k) == "done" for k in ("t1", "t2", "t3"))
           and not [e for e in evF if e.get("type") == "redraw_confirm"]
           and not _directive_decisions(runF)
           and len(_rows(runF)) == 3,
           str(_tmap(runF)))

# ══════════════════════════════════════════════════════════════════════════
fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for _k, _n, _d in fails:
    print("  FAIL  %s  %s" % (_n, _d))
sys.exit(0 if not fails else 1)