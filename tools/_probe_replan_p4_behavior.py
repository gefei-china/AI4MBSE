# -*- coding: utf-8 -*-
"""P4 replanning 行为验证探针（2026-10-21）。

**为什么需要**：门禁 verify_replan_p4.py 钉的是「纯函数正确 + 接线存在 +
静态契约 + 表结构」，但有五件事只有跑**真实管道**（注入式假 LLM）才能证明，
且它们恰恰是本阶段最容易"看起来对、实际不生效"的地方：

    A ★ 默认关零行为变更：`health_probe`/`early_stop`/`llm_actions` 全关时，
      编排**完全不巡检、不收敛**，replan_decisions 表里没有 health/early_stop
      行 —— P4 的头号纪律（与 P1~P3 同款）。
    B ★ M1 超阈**真的**触发精修：子任务连续自报 partial ⇒ 巡检到界 ⇒
      剩余任务 context 被注入巡检发现（UPDATE 不追行）+ plan_revised
      （T2-light）事件 + health 决策落库。
    C ★ 提前收敛**真的**发生：required 产出全 done + 零 failed ⇒ 剩余非必需
      任务被 cancel（subtask skipped 事件 + plan_revised skipped 清单），
      汇总内容显式标注「主动放弃」且聚合状态记 full（不是 partial）。
    D ★ LLM 三动作端到端：`llm_actions=replace,skip,insert` + 指令触发 T3 +
      注入三动作假输出 ⇒ 替代/插入/跳过**都落库**，修订卡带 inserted/skipped。
    E ★ 越界仍被拦：放开动作后 LLM 试图 skip 失效传播集之外的任务 ⇒ T4，
      不落库、留可见痕迹（"放开"不等于"放任"）。
    F ★ replan_decisions 真库可聚合：GROUP BY source/action 有行、
      signals/metrics 是可解析 JSON（阈值调参的数据源）。
    G 回归：全部默认 ⇒ 无任何 P4 痕迹。

**纪律**：MBSE_DB_PATH 指向临时库（import 前设环境变量）；MBSE_LLM_FORCE_MOCK=1
    零网络；不碰生产 mbse.db。假 LLM 只拦三类调用：planner 规划（返回计划
    JSON）、T3 重画（返回注入的三动作 JSON）、子任务执行（返回带协议 JSON 的
    文本，用于造 partial 摘要）—— 其余回落真 mock。
"""
import json
import os
import sys
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "_replan_p4_probe.db")
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
        ("P4-replan-探针", "design", "active"))
    _conv_id = int(_cur.lastrowid or 0)
    _conn0.commit()
finally:
    _conn0.close()
if not _conv_id:
    raise SystemExit("探针前置失败：无法创建测试会话")

from llm import llm_client                    # noqa: E402
from agent.pipeline import AgentPipeline      # noqa: E402
from services.orch_span import SpanCollector  # noqa: E402
from agent import followup as _fup            # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results = []


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}"
          + (f"  {detail}" if not ok and detail else ""))
    return ok


# ── 计划图（按场景可变）────────────────────────────────────────────────────
def _plan(tasks):
    return json.dumps({"tasks": tasks}, ensure_ascii=False)


#: 场景 B 用：5 任务链（t1→t2→t3→t4→t5），无 required 标注
_PLAN_B = _plan([
    {"key": "t1", "title": "需求分析", "agent": "requirement_analysis",
     "deps": [], "context": "原始上下文 t1"},
    {"key": "t2", "title": "系统设计", "agent": "design", "deps": ["t1"],
     "context": "原始上下文 t2"},
    {"key": "t3", "title": "方案评审", "agent": "review", "deps": ["t2"],
     "context": "原始上下文 t3"},
    {"key": "t4", "title": "详细设计", "agent": "design", "deps": ["t3"],
     "context": "原始上下文 t4"},
    {"key": "t5", "title": "报告撰写", "agent": "design", "deps": ["t4"],
     "context": "原始上下文 t5"},
])

#: 场景 C 用：t1/t2 标 required，t3/t4 非 required（提前收敛的牺牲品）
_PLAN_C = _plan([
    {"key": "t1", "title": "需求分析", "agent": "requirement_analysis",
     "deps": [], "config": {"required": True}},
    {"key": "t2", "title": "系统设计", "agent": "design", "deps": ["t1"],
     "config": {"required": True}},
    {"key": "t3", "title": "方案评审", "agent": "review", "deps": ["t2"],
     "config": {"required": False}},
    {"key": "t4", "title": "报告撰写", "agent": "design", "deps": ["t3"],
     "config": {"required": False}},
])

#: 场景 D/E 用：3 任务链（与 P3 探针同款，指令在 t1 派发时投递）
_PLAN_D = _plan([
    {"key": "t1", "title": "需求分析", "agent": "requirement_analysis",
     "deps": []},
    {"key": "t2", "title": "系统设计", "agent": "design", "deps": ["t1"],
     "context": "原始设计上下文"},
    {"key": "t3", "title": "方案评审", "agent": "review", "deps": ["t2"]},
])

_ORIG_CHAT = llm_client.chat
_CUR_PLAN = {"json": _PLAN_D}
_SUB_MODE = {"partial": False, "full": False}   # 子任务输出形态（互斥）
_T3_INJECT = {"on": False, "calls": 0, "tasks": []}


def _proto_text(task_key, status, missing):
    """子任务输出 = 正文 + 尾部协议 JSON（extract_protocol_json 认这个形态）。"""
    return ("这是 %s 的执行结果正文。\n%s"
            % (task_key, json.dumps({
                "schema_version": 1, "task_key": task_key, "status": status,
                "summary": "%s 的成果摘要" % task_key,
                "artifacts": [], "claims": [], "risks": [],
                "missing": missing,
            }, ensure_ascii=False)))


def _chat(messages, *a, **kw):
    try:
        txt = " ".join(str((m or {}).get("content") or "")
                       for m in (messages or []))
    except Exception:
        txt = ""
    # ① T3 重画调用（先于 planner 判 —— 它也用 _intent=planner）
    if _T3_INJECT["on"] and ("重规划器" in txt or "redraw" in txt.lower()):
        _T3_INJECT["calls"] += 1
        return {"choices": [{"message": {"content": json.dumps(
            {"tasks": _T3_INJECT["tasks"]}, ensure_ascii=False)}}]}
    # ② planner 规划调用
    if kw.get("_intent") == "planner":
        return {"choices": [{"message": {"content": _CUR_PLAN["json"]}}]}
    # ③ 子任务执行调用（造 partial/full 摘要）
    if _SUB_MODE["partial"]:
        return {"choices": [{"message": {"content": _proto_text(
            "t", "partial", ["接口文档"])}}]}
    if _SUB_MODE["full"]:
        return {"choices": [{"message": {"content": _proto_text(
            "t", "full", [])}}]}
    return _ORIG_CHAT(messages, *a, **kw)


llm_client.chat = _chat

#: 指令投递点（与 P3 探针同款：指定任务被派发瞬间投）
_DIRECTIVE = {"text": "", "at": ""}
_ORIG_EXEC = AgentPipeline.execute_stream


def _patched_exec(self, *a, **kw):
    key = str(getattr(self, "_tool_task_key", "") or "")
    if _DIRECTIVE["text"] and key == _DIRECTIVE["at"]:
        _fup.push(_conv_id, _DIRECTIVE["text"])
        _DIRECTIVE["text"] = ""      # 只投一次
    return _ORIG_EXEC(self, *a, **kw)


AgentPipeline.execute_stream = _patched_exec


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
        {"id": 1, "username": "probe", "display_name": "P4探针"}, None,
        _spans=spans))
    conn = get_db()
    try:
        spans.flush(conn)
        conn.commit()
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


def _replans(run_id):
    out = []
    for r in _rows(run_id, "SELECT * FROM orch_run_spans WHERE run_id=? "
                          "AND kind='replan' ORDER BY id"):
        r["meta"] = json.loads(r["meta_json"] or "{}")
        out.append(r)
    return out


def _decisions(source=None):
    sql = "SELECT * FROM replan_decisions WHERE conversation_id=?"
    args = [_conv_id]
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY id"
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _reset_cfg(**over):
    from core.config import DEFAULT_CONFIG
    base = dict(enabled=True, retry_once=True, max_redraw_rounds=1,
                redraw_expand_ratio=1.5, llm_redraw=False, redraw_light=True,
                skip_leaf=False, no_progress_stop=True, redraw_on_stall=True,
                heavy_redraw=False, user_directive=False,
                user_confirm_redraw=False, confirm_ttl_sec=60,
                directive_max_touch=3,
                # ── P4 七键（默认全关/锁 replace）──
                health_probe=False, health_probe_llm=False, early_stop=False,
                probe_every_tasks=3, probe_every_sec=60,
                probe_time_budget_sec=1800, llm_actions="replace")
    base.update(over)
    for k, v in base.items():
        DEFAULT_CONFIG["replanning"][k] = v


def _done_ev(events):
    for e in events:
        if e.get("type") == "done":
            return e.get("data") or {}
    return {}


def _plan_revised(events):
    return [e for e in events if e.get("type") == "plan_revised"]


ok = True

# ══════════════════════════════════════════════════════════════════════════
print("=== A. ★ 默认关：零行为变更（P4 头号纪律）===")
_reset_cfg()
_CUR_PLAN["json"] = _PLAN_D
_SUB_MODE.update({"partial": False, "full": False})
_DIRECTIVE.update({"text": "", "at": ""})
evA, runA = _run_flow()
ok &= _rec("A1 正常跑完：t1/t2/t3 全 done",
           all(_tmap(runA).get(k) == "done" for k in ("t1", "t2", "t3")),
           str(_tmap(runA)))
ok &= _rec("A2 ★ 默认关 ⇒ 无 health/early_stop 决策落库"
           "（replan_decisions 表零 P4 行）",
           not [d for d in _decisions()
                if d["source"] in ("health", "early_stop")],
           str([d["source"] for d in _decisions()]))
ok &= _rec("A3 ★ 默认关 ⇒ 无 health-decide / early-stop span",
           not [r for r in _replans(runA)
                if r["name"] in ("health-decide", "early-stop")],
           str([r["name"] for r in _replans(runA)]))
ok &= _rec("A4 ★ 默认关 ⇒ 无 skipped 子任务（不主动放弃任何任务）",
           not [e for e in evA if e.get("type") == "subtask"
                and e.get("status") == "skipped"])
ok &= _rec("A5 ★ 默认关 ⇒ 计划清单行数不变（3 行）",
           len(_rows(runA)) == 3, str(len(_rows(runA))))

print("\n=== B. ★ M1 超阈 ⇒ 巡检精修真的发生（UPDATE 不追行）===")
_reset_cfg(health_probe=True, probe_every_tasks=3, probe_every_sec=3600)
_CUR_PLAN["json"] = _PLAN_B
_SUB_MODE.update({"partial": True, "full": False})   # 全部子任务自报 partial ⇒ M1 100%
evB, runB = _run_flow()
_SUB_MODE.update({"partial": False, "full": False})
hp_spans = [r for r in _replans(runB) if r["name"] == "health-decide"]
ok &= _rec("B1 ★ 巡检到界：health-decide span 落库（3 个完成后触发）",
           bool(hp_spans), "span 数=%d" % len(hp_spans))
ok &= _rec("B2 ★ 判定 action=refine / level=T2-light（S10 ⇒ 原地精修）",
           bool(hp_spans) and hp_spans[0]["meta"].get("action") == "refine"
           and hp_spans[0]["meta"].get("level") == "T2-light",
           str(hp_spans[0]["meta"].get("action")) if hp_spans else "无 span")
ok &= _rec("B3 ★ metrics 带 M1 原始值（阈值调参排障用）",
           bool(hp_spans)
           and "M1_partial" in (hp_spans[0]["meta"].get("metrics") or {}),
           str(hp_spans[0]["meta"].get("metrics"))[:120] if hp_spans else "")
ok &= _rec("B4 ★ health 决策落 replan_decisions（真库 SQL 聚合用）",
           bool(_decisions("health")),
           "rows=%d" % len(_decisions("health")))
ok &= _rec("B5 ★ plan_revised（T2-light）事件发出（用户可见）",
           any((e.get("level") or "") == "T2-light" for e in _plan_revised(evB)),
           str([e.get("level") for e in _plan_revised(evB)]))
ok &= _rec("B6 ★ 剩余任务 context 被注入巡检发现（精修生效）",
           any("巡检精修" in str(r.get("context") or "")
               for r in _rows(runB) if r["task_key"] in ("t4", "t5")),
           str([(r["task_key"], str(r.get("context") or "")[:40])
                for r in _rows(runB) if r["task_key"] in ("t4", "t5")]))
ok &= _rec("B7 ★ 精修不追行：仍 5 行任务（原地 UPDATE）",
           len(_rows(runB)) == 5, str(len(_rows(runB))))
ok &= _rec("B8 ★ 编排给出可见提示（不是静默改上下文）",
           any("健康巡检 S10" in str(e.get("delta") or "")
               for e in evB if e.get("type") == "reasoning"))
ok &= _rec("B9 最终全部 done（partial 摘要不阻断执行）",
           all(v == "done" for v in _tmap(runB).values()),
           str(_tmap(runB)))

print("\n=== C. ★ 提前收敛：required 全 done ⇒ 主动放弃非必需 ===")
_reset_cfg(early_stop=True, health_probe=False)
_CUR_PLAN["json"] = _PLAN_C
# full 协议摘要：把"done 项自身质量"这个变量固定成 full ⇒ 若聚合状态
# 仍被拖成 partial，只能是被放弃的 t3/t4 被误计入缺失（本场景要抓的正是它）。
_SUB_MODE.update({"partial": False, "full": True})
_DIRECTIVE.update({"text": "", "at": ""})
evC, runC = _run_flow()
tmC = _tmap(runC)
ok &= _rec("C1 ★ required 任务全 done（t1/t2）",
           tmC.get("t1") == "done" and tmC.get("t2") == "done", str(tmC))
ok &= _rec("C2 ★ 非必需任务被 cancel（t3/t4 → cancelled）",
           tmC.get("t3") == "cancelled" and tmC.get("t4") == "cancelled",
           str(tmC))
ok &= _rec("C3 ★ subtask skipped 事件下发（前端有落点）",
           sorted(e.get("key") for e in evC if e.get("type") == "subtask"
                  and e.get("status") == "skipped") == ["t3", "t4"],
           str([e.get("key") for e in evC if e.get("type") == "subtask"
                and e.get("status") == "skipped"]))
prC = _plan_revised(evC)
ok &= _rec("C4 ★ plan_revised 带 skipped 清单（修订卡渲染「跳过」）",
           any((e.get("skipped") or []) and set(e["skipped"]) == {"t3", "t4"}
               for e in prC),
           str([(e.get("level"), e.get("skipped")) for e in prC]))
ok &= _rec("C5 ★ early_stop 决策落 replan_decisions",
           bool(_decisions("early_stop")),
           "rows=%d" % len(_decisions("early_stop")))
ok &= _rec("C6 ★ early-stop span 落库（含 cancel 数）",
           any(r["name"] == "early-stop" for r in _replans(runC)),
           str([r["name"] for r in _replans(runC)]))
dC = _done_ev(evC)
# 汇总里所有「（编排…）」段（状态标注 / 质量门禁标注）—— 被放弃的
# t3/t4 绝不许出现在其中（出现了就是把"主动放弃"写成"没做成"）。
import re as _re
re_segs_C = _re.findall(r"（编排[^）]*）", str(dC.get("content") or ""))
ok &= _rec("C7 ★ 汇总内容显式标注「提前收敛/主动放弃」"
           "（用户要知道剩下的是没必要做，不是没做成；"
           "且反思修订后仍存活—— 2026-10-21 修的真 bug）",
           "提前收敛" in str(dC.get("content") or "")
           and "主动放弃" in str(dC.get("content") or ""),
           str(dC.get("content") or "")[-120:])
ok &= _rec("C8 ★ 被放弃的任务不进缺失/失败明细（skipped ≠ missing —— "
           "early_stop 的设计核心；mock 反思若压 partial 也不许冤到 t3/t4）",
           (not re_segs_C and str((dC.get("card") or {}).get(
               "orchestrated_status") or "") in ("full", ""))
           or not any(("t3" in s or "t4" in s) for s in re_segs_C),
           "agg=%s segs=%s" % (str((dC.get("card") or {}).get(
               "orchestrated_status") or ""), re_segs_C[:1]))
ok &= _rec("C9 ★ 编排给出可见提示（不是静默砍任务）",
           any("目标已达成提前收敛" in str(e.get("delta") or "")
               for e in evC if e.get("type") == "reasoning"))

print("\n=== D. ★ LLM 三动作端到端（replace + insert + skip 都落库）===")
_reset_cfg(user_directive=True, llm_redraw=True,
           llm_actions="replace,skip,insert")
_CUR_PLAN["json"] = _PLAN_D
_SUB_MODE.update({"partial": False, "full": False})
_T3_INJECT.update({"on": True, "calls": 0, "tasks": [
    {"action": "replace", "key": "t2_r1", "title": "系统设计（换路径）",
     "deps": ["t1"], "task_type": "agent"},
    {"action": "insert", "key": "t9", "title": "补充性能评审",
     "deps": ["t2_r1"], "task_type": "agent"},
    {"action": "skip", "key": "t3", "reason": "已被新方案覆盖，无需再评"},
]})
_DIRECTIVE.update({"text": "整体重做计划：系统设计换个实现路径，"
                           "再补一个性能评审，原方案评审不用做了", "at": "t1"})
evD, runD = _run_flow()
keysD = [r["task_key"] for r in _rows(runD)]
ok &= _rec("D1 ★ T3 真的调了 LLM（注入式假 LLM 被消费）",
           _T3_INJECT["calls"] >= 1, "calls=%d" % _T3_INJECT["calls"])
ok &= _rec("D2 ★ replace 落库：t2_r1 新行（assigned_by=replanner）",
           any(r["task_key"] == "t2_r1"
               and str(r.get("assigned_by") or "") == "replanner"
               for r in _rows(runD)), str(keysD))
ok &= _rec("D3 ★ insert 落库：t9 新行（每个新任务都证明自己为什么存在）",
           "t9" in keysD, str(keysD))
ok &= _rec("D4 ★ insert 任务挂 lineage.directive（指令原文）",
           any(r["task_key"] == "t9"
               and "整体重做计划" in str(r.get("config") or "")
               for r in _rows(runD)),
           str([r.get("config") for r in _rows(runD)
                if r["task_key"] == "t9"])[:120])
ok &= _rec("D5 ★ skip 落库：t3 被 cancel（带 skip 理由）",
           _tmap(runD).get("t3") == "cancelled", str(_tmap(runD)))
prD = _plan_revised(evD)
ok &= _rec("D6 ★ 修订卡带 inserted + skipped 清单（前端两节渲染）",
           any(set(e.get("inserted") or []) == {"t9"}
               and set(e.get("skipped") or []) == {"t3"} for e in prD),
           str([(e.get("inserted"), e.get("skipped")) for e in prD]))
ok &= _rec("D7 ★ t3_redraw 决策落 replan_decisions（action=replan）",
           any(d["source"] == "t3_redraw" and d["action"] == "replan"
               for d in _decisions()),
           str([(d["source"], d["action"]) for d in _decisions()
                if d["source"] == "t3_redraw"]))
ok &= _rec("D8 ★ 新任务最终都跑到终态（编排没被重画卡死）",
           _tmap(runD).get("t2_r1") in ("done", "cancelled", "failed")
           and _tmap(runD).get("t9") in ("done", "cancelled", "failed"),
           str(_tmap(runD)))

print("\n=== E. ★ 越界仍被拦：skip 失效传播集之外的任务 ⇒ T4 ===")
_T3_INJECT.update({"on": True, "calls": 0, "tasks": [
    {"action": "skip", "key": "t1", "reason": "不需要需求分析了"},
]})
_DIRECTIVE.update({"text": "整体重做计划：需求分析不用做了，直接设计", "at": "t1"})
evE, runE = _run_flow()
keysE = [r["task_key"] for r in _rows(runE)]
ok &= _rec("E1 ★ 越界 skip 被拦：无新任务行、t1 未被 cancel",
           len(keysE) == 3 and _tmap(runE).get("t1") == "done",
           str([(k, _tmap(runE).get(k)) for k in keysE]))
ok &= _rec("E2 ★ 拦截留可见痕迹（T3 重画被拦）",
           any("T3 重画被拦" in str(e.get("delta") or "")
               for e in evE if e.get("type") == "reasoning"),
           str([str(e.get("delta") or "")[:40] for e in evE
                if e.get("type") == "reasoning"][-3:]))
ok &= _rec("E3 ★ 拦截决策落库（action=none / level=T4）",
           any(d["source"] == "t3_redraw" and d["action"] == "none"
               and d["level"] == "T4" for d in _decisions()),
           str([(d["source"], d["action"], d["level"]) for d in _decisions()
                if d["source"] == "t3_redraw"]))

print("\n=== F. ★ replan_decisions 真库可聚合 ===")
_T3_INJECT["on"] = False
conn = get_db()
try:
    agg = [dict(r) for r in conn.execute(
        "SELECT source, action, COUNT(*) n FROM replan_decisions "
        "WHERE conversation_id=? GROUP BY source, action ORDER BY n DESC",
        (_conv_id,)).fetchall()]
finally:
    conn.close()
ok &= _rec("F1 GROUP BY source/action 有行（不是空表摆设）",
           bool(agg), str([(r["source"], r["action"], r["n"]) for r in agg]))
ok &= _rec("F2 ★ signals 是可解析 JSON（不是裸文本）",
           all(isinstance(json.loads(d["signals"] or "[]"), list)
               for d in _decisions()),
           str([d["signals"] for d in _decisions()][:2]))
ok &= _rec("F3 ★ metrics 是可解析 JSON（巡检原始值可查）",
           all(isinstance(json.loads(d["metrics"] or "{}"), dict)
               for d in _decisions()))
ok &= _rec("F4 ★ reason 非空（每个决策都可解释）",
           all(str(d.get("reason") or "").strip() for d in _decisions()),
           str([d["reason"][:30] for d in _decisions()][:3]))

print("\n=== G. 回归：P4 全默认 ⇒ 无任何 P4 痕迹 ===")
_reset_cfg()
_CUR_PLAN["json"] = _PLAN_D
_SUB_MODE.update({"partial": False, "full": False})
_DIRECTIVE.update({"text": "", "at": ""})
evG, runG = _run_flow()
ok &= _rec("G1 默认关正常跑完且零 P4 痕迹",
           all(_tmap(runG).get(k) == "done" for k in ("t1", "t2", "t3"))
           and not [e for e in evG if e.get("type") == "subtask"
                    and e.get("status") == "skipped"]
           and not [e for e in _plan_revised(evG)
                    if (e.get("level") or "") in ("T2-light", "T3")]
           and len(_rows(runG)) == 3,
           str(_tmap(runG)))

# ══════════════════════════════════════════════════════════════════════════
fails = [r for r in _results if r[0] == FAIL]
print("\n════════════════════════════════════════════")
print("结果：%d PASS / %d FAIL" % (len(_results) - len(fails), len(fails)))
for _k, _n, _d in fails:
    print("  FAIL  %s  %s" % (_n, _d))
sys.exit(0 if not fails else 1)
