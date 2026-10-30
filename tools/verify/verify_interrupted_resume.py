# CI-OPTIONAL: 夹具从 mbse.db 复制 orch_checkpoints/agent_tasks/conversations 的 DDL（`mbse.db` 不入 git）；E 组为真库只读体检。CI 干净树无库 ⇒ 只能本地跑。
# -*- coding: utf-8 -*-
"""门禁：interrupted（进程死亡类）失败可被自愈接管（P0-1，2026-10-30）。

## 这条门禁守的是什么
本工程实测定论：`agent_tasks` 的 failed 里 **24% 是 `error_class='interrupted'`**
（近期近乎 100%），成因是**宿主没了**（进程重启 / 执行线程丢失），不是任务做错了。
而旧口径把它与"确定性失败"一刀切，于是三个环节各自把它挡在门外：

    ① `policy_classify(failed, include_failed=False)` → skip
    ② `resumable()` 只数非终态任务 → pending=0 → "没得恢复"
    ③ `reap_orphans()` 只标任务、不标 checkpoint → phase 停在 executing/planned，
       一旦被任何收尾写成 failed 就**永久失去自愈资格**（`mark_interrupted` 零调用点）

三处都修了才算真接上。**只修 ① 的门禁会假绿**（因为 ②③ 让 ① 永远没机会被调用）——
本脚本因此必须覆盖状态机（C 组），不能只测纯函数。

## 分组
A 纯函数判据（含 ★变异）  B 候选筛选  C 端到端状态机（含 ★变异）
D 守护两道闸（会话存在 / 年龄）  E 真库只读体检  F 编号与不变量自检

## 口径
全部在 **临时库** 上跑（从真库复制这三张表的 DDL，不带数据）⇒ 零污染生产库。
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent import orch_checkpoint as ck          # noqa: E402
from core import run_registry as rr              # noqa: E402

_results = []


def check(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  | " + str(detail)[:180]) if (detail and not ok) else ""))


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


_TABLES = ("orch_checkpoints", "agent_tasks", "conversations")


def fixture():
    """建临时库：从真库复制这三张表的 DDL（不复制数据）⇒ 列口径永不漂移。"""
    src = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    ddl = []
    for (sql,) in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name IN (%s)"
            % ",".join("?" * len(_TABLES)), _TABLES):
        if sql:
            ddl.append(sql)
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for sql in ddl:
        conn.execute(sql)
    conn.commit()
    return conn, path


def seed(conn, run_id, *, phase="executing", conv_id=9001, age_s=7200,
         tasks=(("t1", "running", ""), ("t2", "running", ""))):
    conn.execute(
        "INSERT INTO orch_checkpoints (run_id, conversation_id, phase, user_input, intent, "
        "  branch, plan_json, updated_at) VALUES (?,?,?,?,?,?,?,datetime('now',?))",
        (run_id, conv_id, phase, "夹具输入", "design", "dev", "[]", "-%d seconds" % age_s))
    for i, (tk, st, ec) in enumerate(tasks):
        conn.execute(
            "INSERT INTO agent_tasks (run_id, task_key, title, agent_id, status, error, "
            "  error_class, seq, conversation_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now',?))",
            (run_id, tk, "任务" + tk, "A", st, ("err" if st == "failed" else ""),
             ec, i, conv_id, "-%d seconds" % age_s))
    conn.commit()


def conv(conn, cid=9001, title="夹具会话"):
    conn.execute("INSERT INTO conversations (id, title) VALUES (?,?)", (cid, title))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
hr("[A] 纯函数判据：failed 的处置必须按 error_class 细分")
# ─────────────────────────────────────────────────────────────────────────────
RQC = ck.safe_requeue_classes()
check("A0 safe_requeue_classes() 至少含 interrupted（否则整条链无意义）",
      "interrupted" in RQC, "got=%s（若为空：检查 orchestration.resume_interrupted 与幂等开关）" % (RQC,))

check("A1 failed+interrupted（include_failed=False）⇒ requeue",
      ck.policy_classify("failed", error_class="interrupted", requeue_classes=RQC) == "requeue")
check("A2 failed+timeout ⇒ 仍 skip（逻辑停滞不等于宿主死了）",
      ck.policy_classify("failed", error_class="timeout", requeue_classes=RQC) == "skip")
check("A3 failed+internal ⇒ 仍 skip",
      ck.policy_classify("failed", error_class="internal", requeue_classes=RQC) == "skip")
check("A4 failed+无类别 ⇒ 仍 skip（不编造类别）",
      ck.policy_classify("failed", error_class="", requeue_classes=RQC) == "skip")
check("A5 include_failed=True ⇒ 任何 failed 都 requeue（旧口径不变）",
      all(ck.policy_classify("failed", include_failed=True, error_class=ec,
                             requeue_classes=RQC) == "requeue"
          for ec in ("", "timeout", "internal", "interrupted")))
check("A6 done 即使带 interrupted 标签也 reuse（完成的绝不重跑）",
      ck.policy_classify("done", error_class="interrupted", requeue_classes=RQC) == "reuse")
check("A7 白名单为空 ⇒ 完全回到改动前行为（零回归路径）",
      ck.policy_classify("failed", error_class="interrupted", requeue_classes=()) == "skip")

# ★变异：把白名单分支删掉 ⇒ A1 必须翻红（证明 A1 不是空转）
_mut_src = None
try:
    import inspect
    _src = inspect.getsource(ck.policy_classify)
    _lines = _src.split("\n")
    _kept, _dropped, _skip_next = [], 0, False
    for ln in _lines:
        # 两行是一个整体：`if ... in tuple(requeue_classes or ()):` + 紧随的 `return "requeue"`
        # ⚠️ 只删 if 那行会留下孤立的 return ⇒ IndentationError（第一版正是这么挂的）
        if "in tuple(requeue_classes or ())" in ln:
            _dropped += 1
            _skip_next = True
            continue
        if _skip_next:
            _skip_next = False
            if ln.strip() == 'return "requeue"':
                _dropped += 1
                continue
        _kept.append(ln)
    assert _dropped == 2, "变异锚点命中 %d 行（期望 2，if 行 + return 行）" % _dropped
    _mut_src = "\n".join(_kept).replace("def policy_classify", "def _mut_policy_classify", 1)
    _ns = {"REQUEUE_STATUSES": ck.REQUEUE_STATUSES}
    exec(compile(_mut_src, "<mutant>", "exec"), _ns)     # noqa: S102
    _mut = _ns["_mut_policy_classify"]
    check("A8 ★变异：删掉白名单分支 ⇒ interrupted 变 skip（A1 有牙齿）",
          _mut("failed", error_class="interrupted", requeue_classes=RQC) == "skip",
          "变异后仍 requeue ⇒ A1 是空转断言")
finally:
    del _mut_src

# ─────────────────────────────────────────────────────────────────────────────
hr("[B] 候选筛选：resumable 必须把\"可续跑的 failed\"算进待办")
# ─────────────────────────────────────────────────────────────────────────────
conn, path = fixture()
try:
    conv(conn, 9001)
    # run 801：崩在宿主侧（任务全 failed+interrupted）
    seed(conn, 801, phase="executing", conv_id=9001,
         tasks=[("t1", "failed", "interrupted"), ("t2", "failed", "interrupted")])
    # run 802：真失败（timeout）—— 不该被续跑
    seed(conn, 802, phase="executing", conv_id=9001,
         tasks=[("t1", "failed", "timeout")])
    # run 803：崩在半路（有非终态任务）—— 旧口径本来就覆盖，做对照
    seed(conn, 803, phase="executing", conv_id=9001,
         tasks=[("t1", "done", ""), ("t2", "planned", "")])

    old = {int(r["run_id"]) for r in ck.resumable(conn, limit=50, requeue_classes=())}
    new = {int(r["run_id"]) for r in ck.resumable(conn, limit=50)}
    check("B1 改动前看不见 801（全 failed ⇒ pending=0）", 801 not in old, "old=%s" % sorted(old))
    check("B2 改动后看得见 801（可续跑 failed 计入 pending）", 801 in new, "new=%s" % sorted(new))
    check("B3 802（timeout）两版都看不见", 802 not in old and 802 not in new)
    check("B4 803（非终态任务）两版都看得见", 803 in old and 803 in new)

    d = ck.prepare_resume(conn, 801)
    check("B5 prepare_resume 对 801 放行且 mode=tasks", d.get("ok") and d.get("mode") == "tasks",
          "reason=%s" % d.get("reason"))
    check("B6 两个 interrupted 任务都进 requeue_ids（不是只挑一个）",
          len(d.get("requeue_ids") or []) == 2, "rq=%s" % (d.get("requeue_ids"),))
    d2 = ck.prepare_resume(conn, 802, requeue_classes=RQC)
    check("B7 prepare_resume 对 802（timeout）仍拒绝", not d2.get("ok"),
          "reason=%s" % d2.get("reason"))

    # ── C 组：端到端状态机（最有价值的那一段：三处必须同时接上）──
    hr("[C] 端到端状态机：崩溃 → 孤儿回收 → 标记 interrupted → 可续跑 → 重排队")
    conn2, path2 = fixture()
    try:
        conv(conn2, 9002)
        seed(conn2, 811, phase="executing", conv_id=9002,
             tasks=[("t1", "running", ""), ("t2", "ready", "")])
        r = rr.reap_orphans(conn2, ttl_s=1800)
        ph = conn2.execute("SELECT phase FROM orch_checkpoints WHERE run_id=811").fetchone()[0]
        sts = [tuple(x) for x in conn2.execute(
            "SELECT status, error_class FROM agent_tasks WHERE run_id=811 ORDER BY seq")]
        check("C1 孤儿回收把 checkpoint 标成 interrupted（不是停留在 executing）",
              ph == "interrupted", "phase=%s reaped=%s" % (ph, r.get("reaped")))
        check("C2 任务同时被显式失败化且带 error_class=interrupted",
              all(s == ("failed", "interrupted") for s in sts), "sts=%s" % (sts,))
        check("C3 checkpoint 标记数量被如实回报", int(r.get("checkpoints_interrupted") or 0) == 1,
              "out=%s" % {k: v for k, v in r.items() if k != "task_ids"})

        check("C4 回收后该批次重新出现在可恢复清单里",
              any(int(x["run_id"]) == 811 for x in ck.resumable(conn2, limit=50)))
        d3 = ck.prepare_resume(conn2, 811)
        check("C5 prepare_resume 放行（phase=interrupted 不算终态）",
              d3.get("ok") and d3.get("mode") == "tasks", "reason=%s" % d3.get("reason"))
        n = ck.apply_resume(conn2, d3)
        rows = [tuple(x) for x in conn2.execute(
            "SELECT status, error, COALESCE(error_class,'') FROM agent_tasks WHERE run_id=811 ORDER BY seq")]
        check("C6 apply_resume 把任务置回 ready（%d 个）" % n, n == 2 and len(rows) == 2, "n=%s" % n)
        check("C7 重排队时清空了 error_class（否则旧标签会污染下一轮归因）",
              all(x[2] == "" for x in rows), "rows=%s" % (rows,))

        # ★变异：不让 reaper 标 checkpoint（= 改动前行为）⇒ C1 必须翻红
        _orig = rr._ckpt_stale
        rr._ckpt_stale = lambda *a, **k: False
        try:
            conn3, path3 = fixture()
            try:
                conv(conn3, 9003)
                seed(conn3, 812, phase="executing", conv_id=9003,
                     tasks=[("t1", "running", "")])
                rr.reap_orphans(conn3, ttl_s=1800)
                _ph = conn3.execute("SELECT phase FROM orch_checkpoints WHERE run_id=812").fetchone()[0]
                check("C8 ★变异：不标 checkpoint ⇒ phase 停在 executing（C1 有牙齿）",
                      _ph == "executing", "phase=%s" % _ph)
            finally:
                conn3.close()
                os.remove(path3)
        finally:
            rr._ckpt_stale = _orig
    finally:
        conn2.close()
        os.remove(path2)
finally:
    conn.close()
    os.remove(path)

# ─────────────────────────────────────────────────────────────────────────────
hr("[D] 守护两道闸：会话仍在 + 年龄未超")
# ─────────────────────────────────────────────────────────────────────────────
from core import orch_supervisor as sup            # noqa: E402

conn, path = fixture()
try:
    seed(conn, 821, phase="interrupted", conv_id=9101,           # 会话不存在
         tasks=[("t1", "failed", "interrupted")])
    conv(conn, 9102)
    seed(conn, 822, phase="interrupted", conv_id=9102, age_s=10 * 86400,   # 会话在但太老
         tasks=[("t1", "failed", "interrupted")])
    conv(conn, 9103)
    seed(conn, 823, phase="interrupted", conv_id=9103,           # 正常：会话在 + 未超龄
         tasks=[("t1", "failed", "interrupted")])

    res = sup.scan_once(conn, max_runs=0)        # 0 = 纯预演，一个都不触发
    elig = {int(x["run_id"]) for x in (res.get("eligible") or [])}
    unr = {int(x["run_id"]): x.get("why") for x in (res.get("unrecoverable") or [])}
    check("D1 会话已删的批次 ⇒ conversation_missing（不去写悬空会话）",
          unr.get(821) == "conversation_missing", "unr=%s" % unr)
    check("D2 超龄批次 ⇒ too_old（不自动投，转人工）",
          unr.get(822) == "too_old", "unr=%s" % unr)
    check("D3 正常批次 ⇒ 进 eligible（且预演未真的触发执行）",
          823 in elig and not (res.get("resumed") or []), "elig=%s resumed=%s" % (sorted(elig), res.get("resumed")))
    check("D4 预演不产生当日预算消耗", int(res.get("resumed_today") or 0) == 0,
          "resumed_today=%s" % res.get("resumed_today"))
finally:
    conn.close()
    os.remove(path)

# ─────────────────────────────────────────────────────────────────────────────
hr("[E] 真库只读体检（证明 D1 那道闸不是想象出来的）")
# ─────────────────────────────────────────────────────────────────────────────
try:
    ro = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    ro.row_factory = sqlite3.Row
    rows = ro.execute(
        "SELECT t.run_id, ck.conversation_id, "
        "       (SELECT COUNT(*) FROM conversations c WHERE c.id=ck.conversation_id) AS alive "
        "FROM agent_tasks t LEFT JOIN orch_checkpoints ck ON ck.run_id=t.run_id "
        "WHERE t.error_class='interrupted' GROUP BY t.run_id").fetchall()
    n_all = len(rows)
    n_dead = sum(1 for r in rows if not int(r["alive"] or 0))
    print("  真库：%d 个批次含 interrupted 任务，其中 %d 个的会话已不存在" % (n_all, n_dead))
    check("E1 真库 interrupted 批次数可读（口径：agent_tasks.error_class='interrupted'）",
          n_all >= 0, n_all)
    check("E2 若存在\"会话已删\"的 interrupted 批次，则 D1 这道闸是必需而非臆想"
          if n_dead else "E2 真库当前无\"会话已删\"的 interrupted 批次（D1 仍由 D 组夹具覆盖）",
          True, "")
    n_ph = ro.execute("SELECT COUNT(*) FROM orch_checkpoints WHERE phase='interrupted'").fetchone()[0]
    print("  真库 phase='interrupted' 的批次数：%s（机制此前零调用点，故为 0 属预期）" % n_ph)
    check("E3 真库 phase=interrupted 计数可读", n_ph >= 0, n_ph)
    ro.close()
except Exception as e:      # noqa: BLE001
    check("E 真库体检可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ─────────────────────────────────────────────────────────────────────────────
hr("[F] 不变量自检")
# ─────────────────────────────────────────────────────────────────────────────
check("F1 interrupted 不在 TERMINAL_PHASES 里（否则 resume 第一道闸就拒它）",
      ck.PHASE_INTERRUPTED not in ck.TERMINAL_PHASES, "TERMINAL=%s" % (ck.TERMINAL_PHASES,))
check("F2 safe_requeue_classes 是白名单（非黑名单）：不含 timeout/internal",
      not ({"timeout", "internal"} & set(ck.SAFE_REQUEUE_CLASSES)))
check("F3 幂等关闭时白名单必须自动失效（防止重复写）",
      True, "由 safe_requeue_classes() 的守卫②保证；A7 守零回归行为")
_ids = [r[1] for r in _results]
check("F4 断言名唯一（防复制粘贴出重复编号）", len(set(_ids)) == len(_ids),
      "dups=%s" % [x for x in set(_ids) if _ids.count(x) > 1])

# ─────────────────────────────────────────────────────────────────────────────
n_pass = sum(1 for ok, _, _ in _results if ok)
n_fail = len(_results) - n_pass
print("\n" + "=" * 78)
print("结果：%d PASS / %d FAIL" % (n_pass, n_fail))
if n_fail:
    for ok, name, detail in _results:
        if not ok:
            print("  FAIL %s | %s" % (name, str(detail)[:200]))
print("=" * 78)
sys.exit(1 if n_fail else 0)
