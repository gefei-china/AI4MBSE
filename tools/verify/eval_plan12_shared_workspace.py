# -*- coding: utf-8 -*-
"""方案 1+2 真实编排效果评估（2026-10-20，用户要求「先评估再优化」）。

评估对象（commit 99f2d8b）：
  方案 1  共享产物工作区：子任务交付物 >800 字落 offload（task_result:{tkey}），
          下游上下文快照注入「摘要 + 引用块」，下游可调 tool_result_fetch 取全文。
  方案 1b 汇总三态文案（同 commit 顺手修的「编排大量失败」误报）：
          partial ≠ failed，正文必须写「自报部分完成（交付物已产出，存在未验证环节）」。
  方案 2  单点重跑：POST /api/orchestration/runs/{run_id}/rerun（指定 task_key +
          传递下游闭包清状态，终态 checkpoint 回退 executing）。

## 为什么必须跑真实编排（而不是只跑单元/离线检查）
真库取证（2026-10-20 只读）：commit 99f2d8b 提交于 15:48，而此前唯一一条
5 任务链（run 537）执行于 01:29~01:35 —— **早于本 commit**；且全库
`tool_result_offloads` 无一条 `task_result:*`（max id=2137），run 537 的 58 次
tool_result_fetch 取的都是 file_read 文件 offload（4753 字符的 ev_thermal 文件）。
⇒ 方案 1 的「交付物共享区 + 下游取全文」链路**从未被真实编排跑过**，
   必须补一轮端到端实测，否则后续优化（trace/replanning）建立在未验证的地基上。

## 四阶段
  P1 触发：新会话 + 规则信号消息（先…再…并…，_needs_orchestration 确定性分支）
     → 消费 SSE → 拿 run_id / card / 正文。
  P2 取证（只读）：新 offload（task_result:*）/ 下游 fetch 调用参数是否指向它们 /
     三态文案 / 聚合状态口径。
  P3 重跑实测（**写操作，带备份-恢复纪律**）：全字段备份 agent_tasks + checkpoint
     → rerun → 断言闭包/清状态/回退 → 恢复 → 逐字段校验恢复。
  P4 结论：PASS / PARTIAL（机制就绪但未触发）/ FAIL，附证据。

## 纪律（承自上一轮真库事故）
  · 本脚本对生产库的唯一写操作 = P3 的 rerun，且**前后都有全字段备份与恢复校验**。
  · P3 恢复失败 ⇒ 立即停止并报警（不继续 P4），绝不留半清状态。
  · 不调用 resume（不重跑执行，省 token 也避免二次写）。

## 首轮实测结论（2026-10-20 run 538 / conv 9569，15 项过 1 项"失败"实为误报）
  ① 共享区真实建立：t1/t2/t3 交付物 14076/16990/11735 字符 → offload #2138/#2139/#2164
     （`task_result:*`，全库此前 0 条）。
  ② 引用块注入（注入探针，确定性）：t3 快照含「t2 完整交付物（16990 字符）已存档为
     offload #2139……调用 tool_result_fetch 取全文」，且**不注入非依赖的 t1**（最小充分上下文）。
  ③ 消费按需未触发：t2/t3 均判定 400 字摘要够用 —— t2 交付物明确引用 t1 的分析内容
     （"t1 分析给出了两条主线……"），t3 的 23 次 fetch 全在取**自己的**校验结果 offload。
     ⇒ "下游没调 fetch" ≠ 机制破损，A4 已改为注入探针判据（见下方注释）。
  ④ 三态文案生效：正文头「（编排状态：partial——1 个子任务自报部分完成（交付物已产出，
     存在未验证环节）：t3）」，不再出现「交付物缺失/失败」误报。
  ⑤ 单点重跑全链路：闭包计算/清状态/闭包外不变/checkpoint 回退 executing+attempt=0/
     恢复逐字段一致，全部通过。

用法：
    <py> -X utf8 tools/verify/eval_plan12_shared_workspace.py [--base http://127.0.0.1:8000]
        [--user 112] [--timeout 1500] [--rerun-task t3] [--skip-rerun]
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

# 规则信号消息：先…再…并… ⇒ _needs_orchestration 规则分支确定性为真（不靠 LLM 判定）。
# 场景选「利益相关方」——用户当初报「编排大量失败」的正是这个场景（run 537 同型）。
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


# ── P1 触发真实编排 ────────────────────────────────────────────────────────────
def trigger(base, user, timeout_s):
    """建会话（复用既有空会话或新建）→ 消费 SSE → 返回 (conv_id, card, content, stats)。"""
    hdr = {"X-User-Id": str(user)}
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(30.0, read=float(timeout_s))) as c:
        r = c.post(base + "/api/conversations", json={"title": "probe-auth"}, headers=hdr)
        r.raise_for_status()
        cid = int(r.json()["id"])
        note(f"会话 conv_id={cid}（user={user}）")
        note(f"消息：{MESSAGE}")

        card, content, run_id = None, "", 0
        errors, ev_counts = [], {}
        orch_seen, degraded = False, None
        t0 = time.time()
        last_ev = None
        with c.stream("POST", f"{base}/api/conversations/{cid}/chat/stream",
                      json={"message": MESSAGE}, headers=hdr) as resp:
            ck("A1 SSE 建立（200 + text/event-stream）",
               resp.status_code == 200 and "text/event-stream" in (resp.headers.get("content-type") or ""),
               f"{resp.status_code}")
            et = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    et = line[6:].strip()
                    last_ev = et
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
                    elif et == "subtask":
                        orch_seen = True
                        if d.get("status") == "done" and d.get("key"):
                            pass
        elapsed = time.time() - t0
        note(f"SSE 消费完毕：{elapsed:.0f}s，事件分布={ev_counts}")
        if errors:
            note(f"error 事件：{errors[:3]}")
        return cid, card, content, run_id, orch_seen, errors


# ── P2 落库取证（只读） ────────────────────────────────────────────────────────
def forensics(run_id, snap):
    con = db()
    try:
        # A2 上游交付物落共享区
        new_offs = [dict(r) for r in con.execute(
            "SELECT id, tool_name, length(content) clen, conversation_id FROM tool_result_offloads "
            "WHERE id>? AND tool_name LIKE 'task_result:%' ORDER BY id", (snap["max_off"],))]
        # A4 下游 fetch 调用
        fetches = [dict(r) for r in con.execute(
            "SELECT id, task_key, arguments, ok, substr(result,1,120) res FROM tool_call_logs "
            "WHERE run_id=? AND tool_name='tool_result_fetch' ORDER BY id", (run_id,))]
        tasks = [dict(r) for r in con.execute(
            "SELECT task_key, title, agent_id, status, deps, latency_ms, token_count, "
            "retry_count, length(result) rlen, summary_json FROM agent_tasks "
            "WHERE run_id=? ORDER BY seq, id", (run_id,))]
        evs = con.execute("SELECT COUNT(*) n FROM orch_stream_events WHERE run_id=?",
                          (run_id,)).fetchone()["n"] if run_id else 0
        return new_offs, fetches, tasks, evs
    finally:
        con.close()


def parse_fetch_targets(fetches):
    """fetch 调用参数 → 该 offload 的 tool_name（判断取的是交付物共享区还是文件）。"""
    out = []
    con = db()
    try:
        for f in fetches:
            try:
                oid = json.loads(f.get("arguments") or "{}").get("id")
            except Exception:
                oid = None
            tn = ""
            if oid:
                row = con.execute("SELECT tool_name FROM tool_result_offloads WHERE id=?",
                                  (int(oid),)).fetchone()
                tn = str(row["tool_name"]) if row else "<offload不存在>"
            out.append({"task_key": f["task_key"], "offload_id": oid,
                        "offload_tool": tn, "ok": f["ok"], "res": f.get("res") or ""})
    finally:
        con.close()
    return out


# ── P3 单点重跑实测（备份 → rerun → 断言 → 恢复 → 校验） ───────────────────────
def backup_run(run_id):
    con = db()
    try:
        tcols = [r[1] for r in con.execute("PRAGMA table_info(agent_tasks)")]
        rows = [dict(r) for r in con.execute(
            f"SELECT {','.join(tcols)} FROM agent_tasks WHERE run_id=? ORDER BY id", (run_id,))]
        ccols = [r[1] for r in con.execute("PRAGMA table_info(orch_checkpoints)")]
        row = con.execute(f"SELECT {','.join(ccols)} FROM orch_checkpoints WHERE run_id=?",
                          (run_id,)).fetchone()
        ck_row = dict(row) if row else None
        return {"tcols": tcols, "tasks": rows, "ccols": ccols, "ck": ck_row}
    finally:
        con.close()


def restore_run(run_id, bak):
    con = db()
    try:
        for row in bak["tasks"]:
            sets = ",".join(f"{c}=?" for c in bak["tcols"])
            con.execute(f"UPDATE agent_tasks SET {sets} WHERE id=?",
                        [row[c] for c in bak["tcols"]] + [row["id"]])
        if bak["ck"]:
            sets = ",".join(f"{c}=?" for c in bak["ccols"])
            con.execute(f"UPDATE orch_checkpoints SET {sets} WHERE run_id=?",
                        [bak["ck"][c] for c in bak["ccols"]] + [run_id])
        con.commit()
    finally:
        con.close()


def verify_restore(run_id, bak):
    con = db()
    try:
        cur_tasks = [dict(r) for r in con.execute(
            f"SELECT {','.join(bak['tcols'])} FROM agent_tasks WHERE run_id=? ORDER BY id", (run_id,))]
        same_t = cur_tasks == bak["tasks"]
        same_c = True
        if bak["ck"]:
            row = con.execute(
                f"SELECT {','.join(bak['ccols'])} FROM orch_checkpoints WHERE run_id=?",
                (run_id,)).fetchone()
            same_c = (dict(row) if row else None) == bak["ck"]
        return same_t, same_c
    finally:
        con.close()


def rerun_test(base, user, run_id, task_key):
    """P3：备份 → rerun → 断言 → 恢复 → 校验恢复。返回 rerun 响应 dict。"""
    bak = backup_run(run_id)
    n_before = len(bak["tasks"])
    ck_before = bak["ck"] or {}
    note(f"备份完成：{n_before} 个任务行 + checkpoint（phase={ck_before.get('phase')}, "
         f"attempt={ck_before.get('attempt_count')}）")
    hdr = {"X-User-Id": str(user)}
    body = {"task_keys": [task_key], "include_downstream": True}
    with httpx.Client(trust_env=False, timeout=60) as c:
        r = c.post(f"{base}/api/orchestration/runs/{run_id}/rerun", json=body, headers=hdr)
    if r.status_code != 200:
        ck("C0 rerun 端点 200", False, f"got={r.status_code} body={r.text[:200]}")
        restore_run(run_id, bak)   # 端点失败也要恢复（理论上未改数据，双保险）
        return None
    resp = r.json()
    ck("C0 rerun 端点 200 + ok", bool(resp.get("ok")), f"keys={list(resp)[:6]}")

    # C1 下游闭包
    want = set(resp.get("rerun_keys") or [])
    by_key = {t["task_key"]: t for t in bak["tasks"]}
    closure = {task_key}
    changed = True
    while changed:
        changed = False
        for t in bak["tasks"]:
            try:
                deps = json.loads(t.get("deps") or "[]")
            except Exception:
                deps = []
            if t["task_key"] not in closure and any(str(d) in closure for d in deps):
                closure.add(t["task_key"])
                changed = True
    ck("C1 rerun_keys = 指定 + 传递下游闭包", want == closure,
       f"got={sorted(want)} want={sorted(closure)}")
    ck("C1b 指定任务本身在闭包内", task_key in want, f"{sorted(want)}")

    # C2 清状态（只清闭包内；闭包外保持原状）
    con = db()
    try:
        rows = {r["task_key"]: dict(r) for r in con.execute(
            "SELECT task_key, status, result, summary_json, error FROM agent_tasks WHERE run_id=?",
            (run_id,))}
    finally:
        con.close()
    bad = [k for k, v in rows.items()
           if k in want and not (v["status"] == "ready" and not (v["result"] or "")
                                  and v["summary_json"] is None)]
    ck("C2 闭包内任务已清（status=ready/result 空/summary_json=NULL）", not bad,
       f"bad={bad}")
    outside = [k for k in rows if k not in want]
    untouched = all(rows[k]["status"] == (by_key[k]["status"] if k in by_key else rows[k]["status"])
                    for k in outside)
    ck("C2b 闭包外任务状态不变", untouched, f"outside={sorted(outside)}")

    # C3 终态 checkpoint 回退
    con = db()
    try:
        ckrow = con.execute("SELECT phase, attempt_count FROM orch_checkpoints WHERE run_id=?",
                            (run_id,)).fetchone()
    finally:
        con.close()
    ck("C3 终态 checkpoint 回退 executing + attempt=0",
       ckrow and ckrow["phase"] == "executing" and int(ckrow["attempt_count"] or 0) == 0,
       f"phase={ckrow['phase'] if ckrow else None} attempt={ckrow['attempt_count'] if ckrow else None}"
       f"（before phase={ck_before.get('phase')}）")

    # C4 恢复 + 逐字段校验
    restore_run(run_id, bak)
    same_t, same_c = verify_restore(run_id, bak)
    ck("C4 恢复后 agent_tasks 逐字段一致", same_t)
    ck("C4b 恢复后 checkpoint 逐字段一致", same_c)
    return resp


# ── 主流程 ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="112")
    ap.add_argument("--timeout", type=int, default=1500)
    ap.add_argument("--rerun-task", default="t3")
    ap.add_argument("--skip-rerun", action="store_true")
    args = ap.parse_args()

    # 清理代理（沙箱内 HTTP_PROXY 会让 127.0.0.1 走代理 502）
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"

    print("=" * 72)
    print("方案 1+2 真实编排效果评估（P1 触发 → P2 取证 → P3 重跑实测 → P4 结论）")
    print("=" * 72, flush=True)

    # 前置快照（只读）
    con = db()
    snap = {"max_off": con.execute("SELECT COALESCE(MAX(id),0) m FROM tool_result_offloads").fetchone()["m"],
            "max_tcl": con.execute("SELECT COALESCE(MAX(id),0) m FROM tool_call_logs").fetchone()["m"],
            "max_task": con.execute("SELECT COALESCE(MAX(run_id),0) m FROM agent_tasks").fetchone()["m"]}
    con.close()
    note(f"前置快照：max_offload_id={snap['max_off']} max_tool_call_log_id={snap['max_tcl']} "
         f"max_run_id={snap['max_task']}")

    # ── P1 触发 ──
    print("\n=== P1 触发真实编排（规则信号消息，预计数分钟） ===", flush=True)
    cid, card, content, run_id, orch_seen, errors = trigger(args.base, args.user, args.timeout)
    if not card:
        ck("A0 收到 done 卡片", False, f"errors={errors[:2]}")
        return 2
    orchestrated = bool(card.get("orchestrated"))
    degraded = bool(card.get("degraded"))
    ck("A2 进入编排（orchestrated=True 且未降级）", orchestrated and not degraded,
       f"orchestrated={orchestrated} degraded={degraded}")
    if not (orchestrated and not degraded):
        note("未进入编排 ⇒ 方案 1/2 无从评估（前置条件不成立）")
        return 2
    note(f"run_id={run_id} orchestrated_status={card.get('orchestrated_status')} "
         f"tasks={len(card.get('plan') or [])}")
    ck("A2b done 事件带 orch_run_id（B2-11）", run_id > 0, f"run_id={run_id}")

    # 会话改名（便于用户在 UI 里找到这轮评估）
    try:
        with httpx.Client(trust_env=False, timeout=20) as c:
            c.patch(f"{args.base}/api/conversations/{cid}",
                    json={"title": f"E2E-方案1+2效果评估 run{run_id}"},
                    headers={"X-User-Id": str(args.user)})
    except Exception:
        pass

    # ── P2 取证 ──
    print("\n=== P2 落库取证（只读） ===", flush=True)
    new_offs, fetches, tasks, n_evs = forensics(run_id, snap)
    note(f"编排事件落库 {n_evs} 条（orch_stream_events）")
    big = [t for t in tasks if int(t["rlen"] or 0) > 800]
    note(f"任务交付物 >800 字的：{[(t['task_key'], t['rlen']) for t in big]}")
    has_deps = any(json.loads(t["deps"] or "[]") for t in tasks)
    note(f"deps 链存在：{has_deps}"
         + (f"（{[ (t['task_key'], json.loads(t['deps'] or '[]')) for t in tasks if json.loads(t['deps'] or '[]') ]}）"
            if has_deps else ""))

    # A3 交付物共享区
    ck("A3 上游交付物落入共享区（task_result:* offload）", len(new_offs) >= 1,
       f"new={[(o['id'], o['tool_name'], o['clen']) for o in new_offs][:8]}")
    off_ids = {o["id"] for o in new_offs}

    # A4 注入探针（确定性、无 LLM）：用本 run 真实输入形状复现 build_subtask_context，
    #   验证下游快照里**真有**依赖上游的引用块（offload id + 取全文指引）。
    #   为什么必须有这一步（2026-10-20 run 538 实测教训）：下游"没调 fetch"有两种完全
    #   不同的原因 —— ① 引用块没注入（机制破损，必须 FAIL）；② 注入成功但摘要够用、
    #   按需未触发（设计使然，只能记 NOTE）。DB 里看不见 query，不跑探针就无法区分，
    #   而把 ② 报成 FAIL 是**误报**（run 538：t2 交付物明确引用 t1 分析内容，
    #   摘要承载了足够信号，23 次 fetch 全在下游取自己的校验结果）。
    probe_ok, probe_detail = False, ""
    if has_deps and big and new_offs:
        try:
            sys.path.insert(0, ROOT)
            from workflows.planner import build_subtask_context as _bsc
            _rows = {t["task_key"]: t for t in tasks}
            _dep_task = next((t for t in tasks if json.loads(t["deps"] or "[]")), None)
            if _dep_task:
                _up = str(json.loads(_dep_task["deps"] or "[]")[0])
                _up_row = _rows.get(_up)
                _up_off = next((o for o in new_offs if o["tool_name"] == f"task_result:{_up}"), None)
                if _up_row and _up_off:
                    _ctx = _bsc({"task_key": _dep_task["task_key"], "title": _dep_task["title"],
                                 "deps": _dep_task["deps"]}, "评估目标",
                                [{"task_key": k, "title": v["title"], "result": "x" * 900,
                                  "status": "done"} for k, v in _rows.items()],
                                artifact_digest="", offload_map={_up: (_up_off["id"], 16990)})
                    probe_ok = (f"offload #{_up_off['id']}" in _ctx
                                and "tool_result_fetch" in _ctx)
                    probe_detail = (f"{_dep_task['task_key']} 快照含 {_up} 引用块"
                                    f"（offload #{_up_off['id']}）+ 取全文指引")
        except Exception as _e:
            probe_detail = f"探针异常：{_e}"
    ck("A4 注入探针：下游快照含依赖上游的引用块（确定性）", probe_ok, probe_detail)

    # A5 消费观测（**按需**语义，不是必须触发）
    targets = parse_fetch_targets(fetches)
    hit = [t for t in targets if t["offload_id"] in off_ids and t["ok"]]
    if hit:
        ck("A5 下游对交付物共享区发起 tool_result_fetch（端到端消费实证）", True,
           f"hit={[(t['task_key'], t['offload_id'], t['offload_tool']) for t in hit][:6]}")
        # A6 取回内容与上游交付物同源（前缀比对）
        con = db()
        src = {}
        for o in new_offs:
            tk = o["tool_name"].split(":", 1)[1]
            row = con.execute("SELECT result FROM agent_tasks WHERE run_id=? AND task_key=?",
                              (run_id, tk)).fetchone()
            if row:
                src[o["id"]] = str(row["result"] or "")
        con.close()
        mism = []
        for t in hit[:5]:
            body = src.get(t["offload_id"], "")
            head = (t["res"] or "").strip()
            if body and head and body[:40].strip() != head[:40]:
                mism.append(t["offload_id"])
        ck("A6 fetch 取回内容与上游交付物同源（前缀一致）", not mism, f"mismatch={mism}")
    elif probe_ok:
        note("A5 消费未触发：下游判定摘要够用（按需取全文的设计语义）——"
             "不计失败；机制就绪证据 = A3（共享区）+ A4（注入）+ 工具可用"
             "（本 run 其余 fetch 均成功）。")
    else:
        ck("A5 消费观测", False, "注入也未通过（见 A4），消费无从谈起")

    # B 三态文案（同 commit 的「编排大量失败」误报修复）
    st_all = [str(t["status"]) for t in tasks]
    partial_tasks = [t for t in tasks if (t["summary_json"] or "{}").find('"partial"') >= 0]
    agg = str(card.get("orchestrated_status") or "")
    note(f"任务状态分布：done={st_all.count('done')} failed={st_all.count('failed')} "
         f"partial={st_all.count('partial')} blocked={st_all.count('blocked')}；"
         f"自报 partial 的 summary_json：{[t['task_key'] for t in partial_tasks]}")
    if partial_tasks or agg in ("partial", "failed"):
        ck("B1 三态文案：含「自报部分完成」口径", "自报部分完成" in content,
           f"agg={agg} partial_tasks={[t['task_key'] for t in partial_tasks]}")
        ck("B1b 不再出现「交付物缺失/失败」误报措辞",
           "交付物缺失" not in content and "交付物失败" not in content)
        ck("B1c card_data.orchestrated_status 与正文口径一致",
           agg == (card.get("orchestrated_status") or ""), f"agg={agg}")
    else:
        note(f"全部 full（agg={agg}）⇒ 三态文案本次未 exercised（不计失败）")

    # ── P3 重跑实测 ──
    rerun_resp = None
    if not args.skip_rerun and run_id:
        # 动态选重跑点：优先选「有下游依赖者」的最早任务（把传递闭包跑出来）；
        # 没有依赖链则退回首个任务（闭包=自身，仍验证清状态与回退）。
        dep_owner = None
        for t in tasks:
            try:
                deps = json.loads(t["deps"] or "[]")
            except Exception:
                deps = []
            if deps:
                dep_owner = str(deps[0])
                break
        rtask = args.rerun_task if args.rerun_task in {t["task_key"] for t in tasks} else (dep_owner or (tasks[0]["task_key"] if tasks else "t1"))
        note(f"重跑点选择：{rtask}（--rerun-task={args.rerun_task}，"
             f"该任务有下游={dep_owner is not None}）")
        print("\n=== P3 单点重跑实测（备份 → rerun → 断言 → 恢复 → 校验） ===", flush=True)
        rerun_resp = rerun_test(args.base, args.user, run_id, rtask)

    # ── P4 结论 ──
    print("\n" + "=" * 72)
    n_pass = sum(1 for ok, _ in _oks if ok)
    verdict = "PASS" if not _fails else "FAIL"
    print(f"P4 结论：{verdict}（{n_pass} 项通过 / {len(_fails)} 项失败）")
    for name, detail in _fails:
        print(f"  FAIL  {name}  | {detail}")
    verdict_path = os.path.join(ROOT, "tmp", "eval_plan12_verdict.json")
    os.makedirs(os.path.dirname(verdict_path), exist_ok=True)
    with open(verdict_path, "w", encoding="utf-8") as f:
        json.dump({"verdict": verdict, "run_id": run_id, "conv_id": cid,
                   "message": MESSAGE, "oks": _oks, "fails": _fails, "notes": _notes,
                   "rerun": rerun_resp,
                   "new_offloads": new_offs, "fetch_hits": [t for t in targets
                                                            if t["offload_id"] in off_ids]},
                  f, ensure_ascii=False, indent=1)
    note(f"结论已写 {verdict_path}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
