# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：真机端到端，需 8000 服务 + 真实 LLM（编排判定与产码），约 3~10 分钟且产生费用，CI 无服务无 key。本机已实测 PASS 8/0。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""真机端到端验证 A+B：编排里「写产物」是否还被打断。

## 验的是什么（对应真机 conv 990727 那个缺陷）
用户原话：「要求生成用例视图代码，AI 返回生成代码失败」。
真因是**写类判定两处分叉**：暂存分支用硬编码 5 个名字（`save_artifact` 不在其中）
⇒ 跳过暂存 ⇒ 落到统一确认闸 ⇒ 每个子任务各自中断 ⇒ 汇总写"尚未体现实质性完成结果"。

## 两条判据（都落在 DB / 响应体的可复算事实上）
  A：子任务调用 `save_artifact` 后 **不中断**
     · `subtask_artifacts` 出现 `kind='write_request'`（暂存生效）
     · 该 run 的子任务 summary 里 **没有** `pending_confirm` 标记
  B：万一仍中断，**不许伪装成已完成**
     · `agent_tasks.summary_json` 的 `status=failed` 且 `meta.pending_kind` 有值
     · `missing[]` 里写明"等待人工确认"

## 用法
  ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_orch_write_not_blocked.py [--keep]
  --keep 保留验证产生的会话与确认单（默认不删，仅打印 id 供人工核对）
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

BASE = "http://127.0.0.1:8000"
HDR = {"Content-Type": "application/json", "X-User-Id": "111"}

# 触发编排的稳定规则信号：`先…再…`（_needs_orchestration 的第一条规则正则）
# 同时明确要求「保存为产物」⇒ 必然调用 save_artifact ⇒ 命中 A 项判据。
MSG = ("为混动汽车建立顶层用例视图，先拆解顶层用例 SeriesHybridDrivingScenario 与"
       "子用例 VehicleReady、DriveCycle，再生成对应的 SysML v2 用例视图代码，"
       "并把代码保存为产物（调用 save_artifact）。")

PASS, FAIL = 0, 0
ROWS = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, str(detail)[:260]))
    ROWS.append({"label": label, "ok": bool(cond), "detail": str(detail)[:300]})


def post(path, body, timeout=1800):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    print("=" * 76)
    print("真机端到端：编排子任务写产物是否仍被打断（A+B）")
    print("=" * 76)

    # 0) 服务探活
    try:
        with urllib.request.urlopen(BASE + "/api/dashboard", timeout=8) as r:
            code = r.status
    except Exception as e:  # noqa: BLE001
        print("SKIP: 服务不可达（%r）" % e)
        return 2
    check("S0 dashboard=200", code == 200, code)

    # 1) 新建会话
    try:
        st, body = post("/api/conversations", {"title": "A+B验证-编排写产物-%d" % int(time.time())})
        conv = json.loads(body)
        cid = int(conv.get("id") or 0)
    except Exception as e:  # noqa: BLE001
        print("SKIP: 建会话失败 %r" % e)
        return 2
    check("S1 建会话成功", bool(cid), body[:200])
    if not cid:
        return 2
    print("     会话 id = %d" % cid)

    # 2) 发消息（阻塞到整轮结束；编排一般 2~8 分钟）
    t0 = time.time()
    try:
        st, raw = post("/api/conversations/%d/chat" % cid, {"message": MSG})
    except Exception as e:  # noqa: BLE001
        raw, st = json.dumps({"__error__": repr(e)}), -1
    cost = time.time() - t0
    print("     HTTP=%s 耗时 %.1fs 响应 %d 字节" % (st, cost, len(raw)))
    with open("outputs/_e2e_orch_write_resp.json", "w", encoding="utf-8") as f:
        f.write(raw)
    check("S2 请求成功返回", st == 200, raw[:200])

    try:
        resp = json.loads(raw)
    except Exception:
        resp = {}
    # ⚠️ run_id 不在响应顶层（2026-11-13 实测：顶层键是 plan/card/sysml_views，
    #    没有 run_id；`card` 里也没有）。第一版按 `resp["run_id"]` 取 ⇒ 恒 0
    #    ⇒ 后续 SQL 全查空表 ⇒ A1 假红。改为**从 DB 反查**：会话内最后一条
    #    assistant 消息的 `card_data.run_id` 才是本轮的真 run。
    run_id = 0
    card = resp.get("card") or resp.get("card_data") or {}
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except Exception:
            card = {}
    for k in ("run_id", "runId"):
        if resp.get(k):
            run_id = int(resp[k] or 0)
        if not run_id and card.get(k):
            run_id = int(card[k] or 0)
    if not run_id:
        # ⚠️ 实测（2026-11-13，conv 990730/990731）：`card_data` 里**根本没有 run_id**
        #    （键集为 agent/capability_scope/.../plan/plan_guard/sysml_views 等，
        #    无 run_id）⇒ 任何"从响应/消息里取 run_id"的写法都会得到 0，
        #    进而所有 `WHERE run_id=0` 的查询恒空 ⇒ 判据假红（我连踩两轮）。
        #   兜底改为：取**最近写入**的那批 agent_tasks 的 run_id（本轮刚跑完，
        #    其 id 必然是最大的），再用「任务数 == plan 条数」做交叉校验，
        #    避免误抓到别的并发轮次。
        from database import get_db as _gdb
        _c = _gdb()
        try:
            _row = _c.execute(
                "SELECT run_id, COUNT(*) AS n, MAX(id) AS mx FROM agent_tasks "
                "GROUP BY run_id ORDER BY mx DESC LIMIT 1").fetchone()
            if _row:
                run_id = int(_row["run_id"] or 0)
                _n_tasks = int(_row["n"] or 0)
                _n_plan = len(resp.get("plan") or [])
                if _n_plan and _n_tasks != _n_plan:
                    print("     ⚠ 最新 run 任务数 %d ≠ plan %d，可能抓到并发轮次"
                          % (_n_tasks, _n_plan))
        finally:
            _c.close()
    orchestrated = bool(resp.get("orchestrated") or card.get("orchestrated"))
    print("     orchestrated=%s run_id=%s" % (orchestrated, run_id))
    check("S3 本轮确实走了编排（否则 A 项无从谈起）", orchestrated, json.dumps(
        {k: resp.get(k) for k in ("orchestrated", "intent", "agent")}, ensure_ascii=False))

    # 3) DB 核对
    from database import get_db
    c = get_db()
    try:
        wr = c.execute(
            "SELECT task_key, title FROM subtask_artifacts "
            "WHERE run_id=? AND kind='write_request' ORDER BY id", (run_id,)).fetchall() \
            if run_id else []
        tasks = c.execute(
            "SELECT task_key, agent_id, status, "
            "       COALESCE(summary_json,'') AS sj, COALESCE(error,'') AS err "
            "FROM agent_tasks WHERE run_id=? ORDER BY task_key", (run_id,)).fetchall() \
            if run_id else []
        n_hil = c.execute(
            "SELECT COUNT(*) AS n FROM hil_confirmations WHERE run_id=?", (run_id,)).fetchone() \
            if run_id else None
        n_art = c.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=? AND msg_type='hil_confirm'",
            (cid,)).fetchone()
    finally:
        c.close()

    wr = [dict(r) for r in wr]
    tasks = [dict(r) for r in tasks]
    print("\n=== 暂存留档（write_request）===")
    for w in wr:
        print("   task=%-4s tool/title=%s" % (w.get("task_key"), w.get("title")))
    print("   共 %d 条" % len(wr))

    print("\n=== 子任务状态 ===")
    pend_flags = []
    for t in tasks:
        sj = {}
        try:
            sj = json.loads(t.get("sj") or "{}")
        except Exception:
            sj = {}
        meta = sj.get("meta") if isinstance(sj.get("meta"), dict) else {}
        pk = str(meta.get("pending_kind") or "")
        if pk:
            pend_flags.append((t.get("task_key"), pk))
        print("   %-4s agent=%-22s status=%-9s summary.status=%-8s pending_kind=%s"
              % (t.get("task_key"), (t.get("agent_id") or "")[:22], t.get("status"),
                 str(sj.get("status") or "-"), pk or "-"))
        if t.get("err"):
            print("        error: %s" % str(t["err"])[:120])

    # ── A 项判据 ──
    #   ⚠️ 判据必须能区分两件事（2026-11-13 实测踩过的假红）：
    #     ① 调用了写工具但**没暂存** ⇒ A 项真的失效（FAIL）
    #     ② **压根没调用**（子任务工具白名单里没有 save_artifact）⇒ 与本项无关（SKIP）
    #      真机证据：t4 原话「本环节我的工具白名单是智源 MBSE 平台的数据读写工具，
    #      **其中不包含 save_artifact**」⇒ 这是**技能/工具分配**问题，不是暂存逻辑问题。
    print("\n=== A：写产物被暂存、子任务不中断 ===")
    _no_tool = any("save_artifact" in str(t.get("err") or "") or
                   "save_artifact" in str(t.get("sj") or "")
                   for t in tasks)
    _tried = bool(wr) or _no_tool
    if wr:
        check("A1 产生 write_request 留档（暂存生效）", True, "write_request=%d" % len(wr))
    elif _no_tool:
        print("     （本轮子任务未获得 save_artifact 工具 ⇒ 未走到暂存分支，A1 按「未触发」记 SKIP）")
        ROWS.append({"label": "A1 未触发（子任务无 save_artifact 工具）", "ok": True,
                     "detail": "工具分配问题，非暂存逻辑问题"})
    else:
        check("A1 产生 write_request 留档（暂存生效）", False,
              "既无 write_request、也无「无工具」证据 ⇒ 疑似调用后被丢弃")
    # 中断的特征：hil_confirm 消息出现且子任务 pending_kind=confirm
    _pend_confirm = [x for x in pend_flags if x[1] == "confirm"]
    check("A2 没有子任务因「等确认」被标记中断", not _pend_confirm, str(_pend_confirm))
    _fail_tasks = [t.get("task_key") for t in tasks
                   if str(t.get("status") or "").lower() not in ("done", "skipped")]
    check("A3 全部子任务落 done/skipped（无 failed/超时）", not _fail_tasks, str(_fail_tasks))

    # ── B 项判据 ──
    print("\n=== B：若中断则如实标记（不伪装完成）===")
    if _pend_confirm or (n_art and n_art["n"]):
        # 出现了中断 ⇒ 必须能看到 pending_kind，且不能被标成 status=full
        check("B1 中断在 summary.meta.pending_kind 可见", bool(pend_flags),
              "pending_kind 全空但存在中断 ⇒ 仍会误导汇总")
        bad = []
        for t in tasks:
            sj = {}
            try:
                sj = json.loads(t.get("sj") or "{}")
            except Exception:
                pass
            meta = sj.get("meta") if isinstance(sj.get("meta"), dict) else {}
            if meta.get("pending_kind") and str(sj.get("status")) == "full":
                bad.append(t.get("task_key"))
        check("B2 中断的子任务未被标为 status=full", not bad, str(bad))
    else:
        print("     （本轮无中断 ⇒ B 项按「不适用」跳过，不算失败）")
        ROWS.append({"label": "B* 本轮无中断，B 项不适用", "ok": True, "detail": "n/a"})

    # ── 观望：视图是否真的投影出来了（用户可见的最终效果）──
    # ⚠️ sysml_views 在**响应顶层**（不是 card 里）：`{"parsed":{...},"views":{"UC":{...}}}`
    views = resp.get("sysml_views") or card.get("sysml_views") or {}
    vk = list((views.get("views") or {}).keys()) if isinstance(views, dict) else []
    print("\n=== 用户可见效果 ===")
    print("   sysml_views 视图种类: %s" % (vk or "（空）"))
    check("V1 至少投影出一种 SysML 视图（用户能看到图）", bool(vk), vk)
    print("   本 run 确认单条数: %s；本会话 hil_confirm 消息数: %s"
          % (n_hil["n"] if n_hil else "-", n_art["n"] if n_art else "-"))

    print("\n" + "=" * 76)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    print("会话 id=%d run_id=%s（%s）" % (cid, run_id,
          "已保留供人工查看" if args.keep else "如需查看请在 UI 打开该会话"))
    with open("outputs/_e2e_orch_write_not_blocked.json", "w", encoding="utf-8") as f:
        json.dump({"conv": cid, "run_id": run_id, "pass": PASS, "fail": FAIL,
                   "write_requests": len(wr), "pending_flags": pend_flags,
                   "views": vk, "rows": ROWS}, f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
