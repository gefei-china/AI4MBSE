# -*- coding: utf-8 -*-
"""Task #43（2026-10-20）编排观测性 trace 真实编排 e2e 取证 —— 只读 + 一轮真编排。

**为什么必须跑真实编排**：verify_orch_span.py 的 U 段是临时库孪生，证明的是
「服务代码本身对」；但「插桩点真的在编排主链路里、span 真的落库、API 与库一致」
只有真编排能证明 —— 本仓反复验证过「离线全绿 ≠ 线上工作」（dbc1e42 教训）。

## 取证对象
触发一轮确定性编排（规则信号消息：先…再…并… ⇒ _needs_orchestration 规则分支），
然后**只读**断言：
  T1  落库：新 run 有 span，plan/run/task 三类必到（一次多任务编排的骨架）
  T2  树形：plan 与 run 为根，task 挂 run 下；parent_id 全部指向真实存在的行
  T3  成本：每类 latency_ms>0；task meta 带 tokens/attempts 等（per-node 成本可答）
  T4  口径：started_at 先于 ended_at；run span 覆盖整个编排（latency ≥ 各 task 之和）
  T5  API 一致：GET /runs/{id}/trace 与直库查询逐字段一致（span_count/by_kind/tasks）
  T6  不干扰：编排本身正常完成（card + 正文 + agg_status），trace 是旁路不是主路

## 纪律
  · 对生产库**零写操作**（只 SELECT；span 由被观测的编排自己写）。
  · 不复用用户正在看的会话（9569），新建独立 conv。
  · 触发前快照 max(span id)，只认新批次（防把老 run 的 span 混进来）。
  · 不起 resume / 不调 rerun（不重跑执行，省 token 也避免二次写）。

## 首轮实测结论（2026-10-20 run 541 / conv 9572，26 项过 1 项"失败"实为探针误报）
  ① 六类 span 在一轮真实编排里**全部落库**：plan/run/task×3/summary/reflection/views
     （8 行）—— 包括两个条件分支（reflection 质量评审、views SysML 视图）。
  ② 树形正确：plan 与 run 为根（plan 早于 run_id 产生故无父），3 个 task 全挂 run 下，
     parent_id 无悬空；conversation_id 与 run 同批。
     ⚠️ 取证后修正（2026-10-20 18:20）：首轮 summary/reflection/views 与 run **并列成根**，
     而 run span 时间上覆盖后三段（run 收口于最末 done yield 前）⇒ total_ms 双算
     （777832ms vs 实际墙钟 648272ms = plan 17692 + run 630580）。已把后三段改挂
     run 下（parent=_run_sid），total_ms 自此等于端到端墙钟；门禁 S3g2 锁定形状。
     首轮数据的树形快照见 tmp/trace_e2e.log / tmp/trace541.json（历史口径留痕）。
  ③ per-node 成本可答：t1/t2/t3 tokens=5819/3115/4326（合计 13260，与 run span meta 一致），
     attempts/retry_count 齐；latency 全 >0 且 run span（630580ms）覆盖 task 之和（439274ms）。
  ④ 业务结论进 meta：run span meta = {agg_status: partial, degraded: false, tasks: 3,
     tokens: 13260, budget_hit: false, executed: 3}；status 列只管步骤（全 ok）。
  ⑤ API 与库逐字段一致：span_count 8=8、by_kind 6 类全等、tasks 成本聚合相等、根数 5=5。
  ⑥ 不干扰主链路：card + 13322 字正文 + 无 error 事件，编排本身正常完成。
  ⑦ 唯一 FAIL（T6d）为**探针误报**：card 里业务状态的键名是 `orchestrated_status`
     （stream.py L1626），探针写的 `agg_status` ⇒ 已修正并用 msg 4701 验证
     （orchestrated_status=partial 确实在案）。与 eval_plan12 A4 同型：取证脚本自身的
     错误会伪装成系统缺陷。

## 顺带的真实异常路径证据（非本轮触发，只读取证）
  run 539：t1 撞 600s 硬超时 ⇒ task span status=error 且 meta 带 error 详情，
  run span 未闭合 ⇒ flush 时 error + note=unclosed_at_flush —— U3 设计的收口路径
  在真实崩溃场景下按预期工作。

用法：
    <py> -X utf8 tools/verify/eval_orch_trace_e2e.py [--base http://127.0.0.1:8000]
        [--user 112] [--timeout 1500]
退出码：0 全绿 / 1 有失败 / 2 前置条件未建立（没进编排等）。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

import httpx  # noqa: E402
import sqlite3  # noqa: E402

# 与 eval_plan12 同款规则信号消息（确定性进编排，不靠 LLM 判定）：
# 场景带「SysML V2 模型代码 + 校验」⇒ views span 也有机会触发。
MESSAGE = ("先分析电动汽车热管理系统的利益相关方与整车组成关系，"
           "再生成利益相关方 BDD 视图的 SysML V2 模型代码并进行校验")

_oks, _fails, _notes = [], [], []


def ck(name, ok, detail=""):
    (_oks if ok else _fails).append((name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def note(txt):
    _notes.append(txt)
    print("  · " + txt, flush=True)


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def snapshot():
    con = db()
    try:
        return con.execute("SELECT COALESCE(MAX(id),0) m FROM orch_run_spans").fetchone()["m"]
    finally:
        con.close()


# ── 触发真实编排（SSE） ────────────────────────────────────────────────────────
def trigger(base, user, timeout_s):
    hdr = {"X-User-Id": str(user)}
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(30.0, read=float(timeout_s))) as c:
        r = c.post(base + "/api/conversations", json={"title": "trace-e2e-probe"}, headers=hdr)
        r.raise_for_status()
        cid = int(r.json()["id"])
        note(f"会话 conv_id={cid}（user={user}）")
        note(f"消息：{MESSAGE}")

        card, content, run_id, agg = None, "", 0, ""
        errors, ev_counts = [], {}
        orch_seen = False
        t0 = time.time()
        with c.stream("POST", f"{base}/api/conversations/{cid}/chat/stream",
                      json={"message": MESSAGE}, headers=hdr) as resp:
            ck("T0 SSE 建立（200 + text/event-stream）",
               resp.status_code == 200 and "text/event-stream" in (resp.headers.get("content-type") or ""),
               f"{resp.status_code}")
            et = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    et = line[6:].strip()
                    ev_counts[et] = ev_counts.get(et, 0) + 1
                elif line.startswith("data:") and et:
                    try:
                        d = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if et == "error":
                        errors.append(d)
                    elif et == "done":
                        env = d.get("data") or {}
                        card = env.get("card") if isinstance(env.get("card"), dict) else env
                        content = str(env.get("content") or "")
                        run_id = int(env.get("orch_run_id") or (card or {}).get("run_id") or 0)
                        # 键名口径（stream.py L1626）：card_data 里叫 orchestrated_status，
                        # 不叫 agg_status —— 探针找错键 = 判据误报（eval_plan12 A4 同型坑）。
                        agg = str((card or {}).get("orchestrated_status")
                                  or env.get("agg_status") or "")
                    elif et == "subtask":
                        orch_seen = True
        note(f"SSE 消费完毕：{time.time()-t0:.0f}s，事件分布={ev_counts}")
        if errors:
            note(f"error 事件：{errors[:3]}")
        return cid, card, content, run_id, agg, orch_seen, errors


# ── 取证（只读） ───────────────────────────────────────────────────────────────
def forensics(run_id, min_id):
    con = db()
    try:
        spans = [dict(r) for r in con.execute(
            "SELECT id, kind, name, parent_id, status, started_at, ended_at, latency_ms,"
            " meta_json, conversation_id, run_id FROM orch_run_spans"
            " WHERE id>? ORDER BY id", (min_id,))]
        return spans
    finally:
        con.close()


def api_trace(base, user, run_id):
    with httpx.Client(trust_env=False, timeout=30.0) as c:
        r = c.get(f"{base}/api/orchestration/runs/{run_id}/trace",
                  headers={"X-User-Id": str(user)})
        return r.status_code, (r.json() if r.status_code == 200 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000"))
    ap.add_argument("--user", default=os.environ.get("VERIFY_UID", "112"))
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("VERIFY_TIMEOUT", "1500")))
    args = ap.parse_args()

    print("Task #43 trace 真实编排 e2e 取证")
    min_id = snapshot()
    note(f"span 表快照 max(id)={min_id}（只认新批次）")

    cid, card, content, run_id, agg, orch_seen, errors = trigger(args.base, args.user, args.timeout)
    if not run_id:
        print("\n前置条件未建立：未拿到 orch_run_id（没进编排？）", flush=True)
        return 2
    note(f"run_id={run_id} agg_status={agg or '(空)'}")

    # T6 不干扰：编排本身正常完成
    ck("T6a 编排产出 card（trace 是旁路不是主路）", isinstance(card, dict) and bool(card))
    ck("T6b 正文非空", len(content) > 50, f"len={len(content)}")
    ck("T6c SSE 无 error 事件", not errors, f"{errors[:2]}")
    ck("T6d 编排状态在案（agg_status 非空）", bool(agg), agg)

    spans = forensics(run_id, min_id)
    mine = [s for s in spans if s["run_id"] == run_id]
    note(f"新批次 span 行：{len(mine)}（快照后总新增 {len(spans)}）")
    ck("T1a span 落库（≥6 行：plan/run/3 task 起）", len(mine) >= 6, "n=%d" % len(mine))

    kinds = {}
    for s in mine:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    note(f"kind 分布：{kinds}")
    for k in ("plan", "run", "task"):
        ck(f"T1b {k} span 必到", kinds.get(k, 0) >= 1, "缺" if not kinds.get(k) else "")
    ck("T1c run span 恰 1 个（两分支互斥：resume XOR 新批次）", kinds.get("run", 0) == 1,
       f"run={kinds.get('run', 0)}")
    ck("T1d summary span 到（多任务编排收口）", kinds.get("summary", 0) >= 1,
       "缺（该批次可能未走汇总）" if not kinds.get("summary") else "")
    for k in ("reflection", "views"):
        if kinds.get(k, 0):
            note(f"T1e {k} span 也落库了（{kinds[k]} 个）—— 条件分支本轮真实触发")
        else:
            note(f"T1e {k} span 未触发（条件分支，不计失败；机制就绪证据见 S3e 按 kind 插桩断言）")

    # T2 树形
    by_id = {s["id"]: s for s in mine}
    roots = [s for s in mine if s["parent_id"] == 0 or s["parent_id"] not in by_id]
    run_span = next((s for s in mine if s["kind"] == "run"), None)
    plan_span = next((s for s in mine if s["kind"] == "plan"), None)
    ck("T2a plan 与 run 均为根（plan 早于 run_id 产生，无父）",
       plan_span is not None and run_span is not None
       and plan_span["parent_id"] == 0 and run_span["parent_id"] == 0)
    task_spans = [s for s in mine if s["kind"] == "task"]
    ck("T2b 全部 task 挂在 run span 下",
       bool(task_spans) and all(s["parent_id"] == run_span["id"] for s in task_spans),
       f"tasks={len(task_spans)}")
    ck("T2c parent_id 无悬空（全部指向本批次真实行或 0）",
       all(s["parent_id"] == 0 or s["parent_id"] in by_id for s in mine))
    ck("T2d conversation_id 与 run 同批", all(s["conversation_id"] == cid for s in mine),
       f"cid={cid}")

    # T3 成本
    ck("T3a 全部 span latency_ms>0（monotonic 差值）",
       all(s["latency_ms"] > 0 for s in mine),
       f"min={min((s['latency_ms'] for s in mine), default=0)}")
    t_tokens = 0
    t_ok_meta = True
    for s in task_spans:
        try:
            m = json.loads(s["meta_json"] or "{}")
            t_tokens += int(m.get("tokens") or 0)
            if "attempts" not in m or "retry_count" not in m:
                t_ok_meta = False
        except Exception:
            t_ok_meta = False
    ck("T3b task meta 带 tokens/attempts/retry_count（per-node 成本可答）", t_ok_meta,
       f"tokens 合计={t_tokens}")
    ck("T3c run span meta 带 agg_status（业务结论进 meta，status 只管步骤）",
       run_span is not None and bool(json.loads(run_span["meta_json"] or "{}").get("agg_status")),
       run_span["meta_json"][:120] if run_span else "")

    # T4 口径
    bad_ts = [s["name"] for s in mine if not (s["started_at"] <= (s["ended_at"] or s["started_at"]))]
    ck("T4a started_at ≤ ended_at（本地时间串仅展示）", not bad_ts, f"{bad_ts[:3]}")
    task_ms = sum(s["latency_ms"] for s in task_spans)
    ck("T4b run span 覆盖整个编排（latency ≥ task 之和）",
       run_span is not None and run_span["latency_ms"] >= task_ms,
       f"run={run_span['latency_ms'] if run_span else 0}ms task和={task_ms}ms")
    ck("T4c 全部 span status=ok（无未闭合异常收口）",
       all(s["status"] == "ok" for s in mine),
       f"非 ok：{[(s['kind'], s['status']) for s in mine if s['status'] != 'ok'][:3]}")

    # T5 API 一致
    code, j = api_trace(args.base, args.user, run_id)
    ck("T5a trace 端点 200", code == 200, f"HTTP {code}")
    if code == 200:
        ck("T5b API span_count 与库一致", j.get("span_count") == len(mine),
           f"api={j.get('span_count')} db={len(mine)}")
        api_by_kind = {k: v["count"] for k, v in (j.get("stats") or {}).get("by_kind", {}).items()}
        ck("T5c API by_kind 与库一致", api_by_kind == kinds, f"api={api_by_kind} db={kinds}")
        api_tasks = {t["key"]: t for t in (j.get("stats") or {}).get("tasks", [])}
        db_tasks = {}
        for s in task_spans:
            m = json.loads(s["meta_json"] or "{}")
            db_tasks[s["name"]] = {"status": s["status"], "tokens": int(m.get("tokens") or 0)}
        same = set(api_tasks) == set(db_tasks) and all(
            api_tasks[k]["status"] == db_tasks[k]["status"] and api_tasks[k]["tokens"] == db_tasks[k]["tokens"]
            for k in db_tasks)
        ck("T5d API tasks 成本聚合与库一致", same,
           f"api={ {k: (v['status'], v['tokens']) for k, v in api_tasks.items()} }")
        ck("T5e API 根数与库一致（plan+run）", len(j.get("spans") or []) == len(roots),
           f"api={len(j.get('spans') or [])} db={len(roots)}")

    print("\n════════════════════════════════════════")
    print("结果：%d PASS / %d FAIL" % (len(_oks), len(_fails)))
    for name, detail in _fails:
        print("  FAIL  %s  %s" % (name, detail))
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
